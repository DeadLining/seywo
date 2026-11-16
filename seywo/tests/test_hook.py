"""Compact boundary hook: durable record, no work in the hook process."""

from seywo.archive import open_source

import io
import json
from pathlib import Path


from seywo import hook, inbox
from seywo.config import initialize, load_config


def project(
    tmp_path, *, enabled=True, event="PreCompact", chat="chat-1", body="第一轮"
):
    config = initialize(tmp_path / "project")
    path = Path(config["config"])
    data = json.loads(path.read_text())
    data["hook"] = {"enabled": enabled}
    path.write_text(json.dumps(data))
    source = tmp_path / "rollout.jsonl"
    source.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": chat, "cwd": str(tmp_path / "project")},
            }
        )
        + "\n"
        + json.dumps(message(body))
        + "\n"
    )
    payload = {
        "hook_event_name": event,
        "session_id": chat,
        "cwd": str(tmp_path / "project"),
        "transcript_path": str(source),
    }
    return load_config(path), source, payload


def message(text, role="user"):
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": "input_text", "text": text}],
        },
    }


def run(monkeypatch, payload):
    spawned = []
    monkeypatch.setattr(hook.subprocess, "Popen", lambda *a, **k: spawned.append(a[0]))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert hook.main() == 0
    assert out.getvalue() == ""
    return spawned


def test_precompact_records_snapshot_and_spawns_worker(tmp_path, monkeypatch):
    config, source, payload = project(tmp_path)
    spawned = run(monkeypatch, payload)
    items = inbox.pending(config, "chat-1")
    assert len(items) == 1 and items[0]["event"] == "PreCompact"
    snapshot = Path(items[0]["snapshot"])
    assert open_source(snapshot).read() == source.read_bytes()
    assert spawned and spawned[0][-2:] == ["--chat", "chat-1"]


def test_duplicate_delivery_coalesces_on_source_fingerprint(tmp_path, monkeypatch):
    config, _, payload = project(tmp_path)
    run(monkeypatch, payload)
    run(monkeypatch, payload)
    assert len(inbox.pending(config)) == 1


def test_disabled_or_uninitialized_project_is_untouched(tmp_path, monkeypatch):
    config, _, payload = project(tmp_path, enabled=False)
    assert run(monkeypatch, payload) == []
    assert inbox.pending(config) == []
    other = tmp_path / "other"
    other.mkdir()
    payload["cwd"] = str(other)
    assert run(monkeypatch, payload) == []


def test_injection_events_emit_seywo_resume_context(tmp_path, monkeypatch):
    config, _, payload = project(tmp_path, event="SessionStart")
    payload.pop("transcript_path")

    class FakeRetrieval:
        def __init__(self, store):
            pass

        def resume(self, **kwargs):
            return {
                "results": [
                    {
                        "kind": "chat_resume",
                        "focus": "主线焦点",
                        "goals": [{"title": "完成切换"}],
                        "open_questions": [],
                        "next_steps": [{"title": "重连验证"}],
                    }
                ]
            }

    monkeypatch.setattr(hook, "Retrieval", FakeRetrieval)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert hook.main() == 0
    result = json.loads(out.getvalue())
    assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "主线焦点" in result["hookSpecificOutput"]["additionalContext"]
    assert "完成切换" in result["hookSpecificOutput"]["additionalContext"]


def test_other_events_and_bad_payload_exit_zero(tmp_path, monkeypatch):
    _, _, payload = project(tmp_path)
    payload["hook_event_name"] = "Stop"
    assert run(monkeypatch, payload) == []
    payload["session_id"] = "bad id"
    assert run(monkeypatch, payload) == []


def test_postcompact_snapshots_its_own_segment(tmp_path, monkeypatch):
    config, source, payload = project(tmp_path, event="PostCompact")
    run(monkeypatch, payload)
    first = Path(inbox.pending(config)[0]["snapshot"])
    assert open_source(first).read() == source.read_bytes()
    # A later compact must never overwrite the segment a worker may still read.
    inbox.claim(config, inbox.pending(config)[0])
    run(monkeypatch, payload)
    second = Path(inbox.pending(config)[0]["snapshot"])
    # Exact content has one immutable manifest; a second boundary may share it.
    assert second == first
    assert open_source(second).read() == source.read_bytes()


def test_claim_then_record_writes_a_second_record(tmp_path, monkeypatch):
    config, _, payload = project(tmp_path)
    run(monkeypatch, payload)
    claimed = inbox.claim(config, inbox.pending(config)[0])
    assert claimed["state"] == "claimed"
    run(monkeypatch, payload)
    items = inbox.pending(config)
    assert len(items) == 1 and items[0]["state"] == "pending"


def test_failed_record_is_retryable(tmp_path, monkeypatch):
    config, _, payload = project(tmp_path)
    run(monkeypatch, payload)
    item = inbox.pending(config)[0]
    inbox.finish(config, item, state="failed", error="source_missing")
    fail = inbox.failed(config, "chat-1")
    assert fail[0]["error_code"] == "source_missing"
    inbox.retry(config, fail[0])
    assert len(inbox.pending(config)) == 1


def test_cli_hook_subcommand_uses_stdin(tmp_path, monkeypatch):
    from seywo.cli import main

    config, _, payload = project(tmp_path)
    monkeypatch.setattr(hook.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert main(["hook"]) == 0
    assert out.getvalue() == ""
    assert len(inbox.pending(config)) == 1


def test_forked_chat_uses_transcript_identity_not_payload_session_id(
    monkeypatch, tmp_path
):
    """Codex reports the *root* thread in session_id; the transcript's first
    record names the actual chat. Trusting the payload binds a fork's turns to
    its parent and never creates the child chat."""
    config, source, payload = project(tmp_path, chat="child")
    payload["session_id"] = "root"
    # The source already carries the child's identity; make the metadata explicit.
    lines = source.read_text().splitlines()
    record = json.loads(lines[0])
    record["payload"] = {
        "session_id": "root",
        "id": "child",
        "parent_thread_id": "root",
        "cwd": str(tmp_path / "project"),
    }
    source.write_text("\n".join([json.dumps(record), *lines[1:]]) + "\n")
    run(monkeypatch, payload)
    items = inbox.pending(config)
    assert [i["chat_id"] for i in items] == ["child"]


def test_hook_falls_back_to_payload_when_identity_is_unreadable(monkeypatch, tmp_path):
    config, source, payload = project(tmp_path)
    payload["session_id"] = "payload-chat"
    source.write_text("not json\n")
    run(monkeypatch, payload)
    assert [i["chat_id"] for i in inbox.pending(config)] == ["payload-chat"]
