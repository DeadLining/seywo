import json
from pathlib import Path

import pytest

from test_s2 import message
from seywo import inbox, maintain
from seywo.config import initialize, load_config


class CompactTransport:
    model = "compact-test-model"

    def __init__(self, *args, **kwargs):
        self.calls = 0
        self.payloads = []

    def complete_compact(self, *, model, instructions, payload, max_output_tokens):
        self.calls += 1
        self.payloads.append(payload)
        indexes = [turn["index"] for turn in payload["turns"]]
        return {
            "output": {
                "segments": [{"title": "片段级主线", "turn_indexes": indexes, "action": "continue", "anchor_index": None}],
                "changes": [{"operation": "new", "title": "压缩当前对话", "type": "goal",
                              "attribution": "user", "turn_indexes": indexes,
                              "target_index": None, "open": True, "merge_indexes": []}],

                "current": {"goal_index": max((row["index"] for row in payload["active_items"]), default=-1) + 1, "priority_indexes": []},
            },
            "finish": "complete",
            "error": None,
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }


def make_source(root, chat_id):
    path = root / "compact-source.jsonl"
    records = [{"type": "session_meta", "payload": {"id": chat_id, "cwd": str(root)}}]
    for turn_id, user, assistant in [
        ("turn-1", "先整理当前对话", "先按多轮片段归并"),
        ("turn-2", "要求模型消耗低", "模型只返回短标题和索引"),
    ]:
        records.append({"type": "turn_context", "payload": {"turn_id": turn_id, "root_turn_id": turn_id}})
        records.extend([message(user), message(assistant, "assistant")])
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records), encoding="utf-8")
    return path


def test_compact_route_calls_model_once_for_one_multi_turn_compact(tmp_path, monkeypatch):
    root = tmp_path / "project"
    initialize(root)
    config_path = root / ".seywo" / "config.json"
    data = json.loads(config_path.read_text())
    data["hook"] = {"enabled": True, "pipeline": "seywo-compact-v1", "model": "compact-test-model"}
    config_path.write_text(json.dumps(data))
    config = load_config(config_path)
    transport = CompactTransport()
    monkeypatch.setattr(maintain, "Transport", lambda *args, **kwargs: transport)
    monkeypatch.setattr(maintain, "read_provider", lambda config=None: {
        "endpoint": "http://127.0.0.1:1/responses", "model": "compact-test-model",
        "timeout": 5, "max_output_tokens": 1200, "_api_key": "key",
    })

    chat_id = "compact-chat"
    source = make_source(root, chat_id)
    inbox.record(config, chat_id, "PreCompact", str(source))
    report = maintain.run_chat(config, chat_id)

    assert report["state"] == "done", report
    assert report["pipeline"] == "seywo-compact-v1"
    assert report["calls"] == 1
    assert len(transport.payloads) == 1
    assert len(transport.payloads[0]["turns"]) == 2
    assert report["compact_segments"] == 1


@pytest.mark.parametrize("previously_failed", [False, True])
def test_large_compact_capture_and_retry_preserve_original_evidence(tmp_path, monkeypatch, previously_failed):
    import hashlib

    from seywo.archive import open_source
    from seywo.retrieval import Retrieval
    from seywo.store import Store

    root = tmp_path / "project"
    config = load_config(Path(initialize(root)["config"]))
    config["hook"] = {"enabled": True, "pipeline": "seywo-compact-v1"}
    transport = CompactTransport()
    monkeypatch.setattr(maintain, "read_provider", lambda *_: {
        "endpoint": "http://127.0.0.1:1/responses", "model": transport.model,
        "timeout": 5, "max_output_tokens": 1200, "_api_key": "fixture",
    })
    monkeypatch.setattr(maintain, "Transport", lambda *_args, **_kwargs: transport)
    chat = "large-compact"
    source = make_source(root, chat)
    image = "data:image/png;base64," + "VGVzdEltYWdl" * (512 * 1024)
    with source.open("a", encoding="utf-8") as stream:
        for index in range(6):
            tool = {"type": "response_item", "payload": {
                "type": "function_call_output", "call_id": str(index), "output": image,
            }}
            for row in (tool, {"type": "compacted", "payload": {"replacement_history": [tool["payload"]]}}):
                stream.write(json.dumps(row) + "\n")
    size = source.stat().st_size
    assert size > 64 * 1024 * 1024
    with source.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()

    if previously_failed:
        path = inbox.root(config) / "pending" / "legacy-oversized.json"
        path.parent.mkdir(parents=True)
        inbox._write_atomic(path, {"id": path.stem, "chat_id": chat, "event": "PreCompact",
            "transcript": str(source), "snapshot": None, "state": "failed", "attempts": 1,
            "error_code": "snapshot_too_large"})
    else:
        boundary = inbox.record(config, chat, "PreCompact", str(source))
        assert boundary["snapshot"]
        with open_source(boundary["snapshot"]) as stream:
            saved = stream.read(32768)
            assert stream.read(1) == b""
        assert b"VGVzdEltYWdl" not in saved and b"replacement_history" not in saved
        assert "先整理当前对话" in saved.decode()
        # The worker must use the frozen boundary, not whatever is now at the live path.
        source.write_text('{"type":"rewritten"}\n')

    report = maintain.run_chat(config, chat)
    assert report["state"] == "done", report
    assert report["calls"] == 1
    assert "先整理当前对话" in json.dumps(transport.payloads, ensure_ascii=False)
    assert "VGVzdEltYWdl" not in json.dumps(transport.payloads)
    assert not inbox.failed(config, chat)
    assert not inbox.pending(config, chat)
    assert maintain.run_chat(config, chat)["calls"] == 0
    with Store(config, read_only=True) as store:
        assert store.db.execute("SELECT count(*) FROM source_archives").fetchone()[0] == 0
        assert not list((inbox.root(config) / "objects").glob("*.z"))
        hit = Retrieval(store).search("先整理当前对话")["results"][0]
        assert Retrieval(store).expand(hit["id"])["text"] == "先整理当前对话"
        assert store.db.execute("SELECT count(*) FROM narrative_versions").fetchone()[0] == 1
    if previously_failed:
        with source.open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == sha


class FailingCompactTransport(CompactTransport):
    def complete_compact(self, *, model, instructions, payload, max_output_tokens):
        self.calls += 1
        return {"output": None, "finish": "error", "error": "invalid_json", "usage": {"input_tokens": 100, "output_tokens": 0}}


def test_compact_failure_keeps_cursor_before_and_records_failure(tmp_path, monkeypatch):
    root = tmp_path / "project"
    initialize(root)
    config_path = root / ".seywo" / "config.json"
    data = json.loads(config_path.read_text())
    data["hook"] = {"enabled": True, "pipeline": "seywo-compact-v1", "model": "compact-test-model"}
    config_path.write_text(json.dumps(data))
    config = load_config(config_path)
    transport = FailingCompactTransport()
    monkeypatch.setattr(maintain, "Transport", lambda *args, **kwargs: transport)
    monkeypatch.setattr(maintain, "read_provider", lambda config=None: {
        "endpoint": "http://127.0.0.1:1/responses", "model": "compact-test-model",
        "timeout": 5, "max_output_tokens": 1200, "_api_key": "key",
    })
    chat_id = "compact-failed"
    inbox.record(config, chat_id, "PreCompact", str(make_source(root, chat_id)))
    report = maintain.run_chat(config, chat_id)
    assert report["state"] == "failed"
    assert report["error_code"] == "invalid_json"
    state_path = Path(config["_path"]).parent / "maintain" / f"{chat_id}.json"
    state = json.loads(state_path.read_text())
    assert state.get("cursor") is None
    from seywo.store import Store
    with Store(config, read_only=True) as store:
        receipt = store.db.execute("SELECT state,error_code FROM compact_batches WHERE chat_id=?", (chat_id,)).fetchone()
    assert tuple(receipt) == ("failed", "invalid_json")

class SequentialCompactTransport(CompactTransport):
    def complete_compact(self, *, model, instructions, payload, max_output_tokens):
        self.calls += 1
        self.payloads.append(payload)
        indexes = [turn["index"] for turn in payload["turns"]]
        if self.calls == 1:
            change = {"operation": "new", "title": "跨批次事项", "type": "goal",
                      "attribution": "user", "turn_indexes": indexes,
                      "target_index": None, "open": True, "merge_indexes": []}
        else:
            change = {"operation": "update", "title": "跨批次事项已补充", "type": "goal",
                      "attribution": "user", "turn_indexes": indexes,
                      "target_index": 0, "open": True, "merge_indexes": []}
        return {"output": {"segments": [{"title": "跨批次主线", "turn_indexes": indexes, "action": "continue", "anchor_index": None}],
                            "changes": [change],
                            "current": {"goal_index": 0, "priority_indexes": []}},
                "finish": "complete", "error": None,
                "usage": {"input_tokens": 100, "output_tokens": 20}}


def test_oversized_compact_batches_reuse_the_in_memory_catalog(tmp_path, monkeypatch):
    root = tmp_path / "project"
    initialize(root)
    config_path = root / ".seywo" / "config.json"
    data = json.loads(config_path.read_text())
    data["hook"] = {"enabled": True, "pipeline": "seywo-compact-v1", "model": "compact-test-model"}
    config_path.write_text(json.dumps(data))
    config = load_config(config_path)
    transport = SequentialCompactTransport()
    monkeypatch.setattr(maintain, "Transport", lambda *args, **kwargs: transport)
    monkeypatch.setattr(maintain, "read_provider", lambda config=None: {
        "endpoint": "http://127.0.0.1:1/responses", "model": "compact-test-model",
        "timeout": 5, "max_output_tokens": 1200, "_api_key": "key",
    })
    chat_id = "compact-batched"
    source = root / "many-turns.jsonl"
    records = [{"type": "session_meta", "payload": {"id": chat_id, "cwd": str(root)}}]
    for index in range(25):
        turn_id = f"turn-{index}"
        records.append({"type": "turn_context", "payload": {"turn_id": turn_id, "root_turn_id": turn_id}})
        records.extend([message(f"继续推进事项 {index}"), message(f"已处理事项 {index}", "assistant")])
    source.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records))
    inbox.record(config, chat_id, "PreCompact", str(source))
    report = maintain.run_chat(config, chat_id)
    assert report["state"] == "done", report
    assert report["calls"] == 2
    assert any(row["title"] == "跨批次事项" for row in transport.payloads[1]["active_items"])
    from seywo.store import Store
    with Store(config, read_only=True) as store:
        body = json.loads(store.db.execute(
            "SELECT body_json FROM narrative_versions WHERE chat_id=? ORDER BY version DESC LIMIT 1", (chat_id,)
        ).fetchone()[0])
    assert len(body["items"]) == 1
    assert body["items"][0]["title"] == "跨批次事项已补充"


def test_current_only_publication_and_model_identity_receipt(tmp_path, monkeypatch):
    from seywo.store import Store
    root = tmp_path / 'project'
    initialize(root)
    path = root / '.seywo/config.json'
    data = json.loads(path.read_text())
    data['hook'] = {'enabled': True, 'pipeline': 'seywo-compact-v1'}
    path.write_text(json.dumps(data))
    config = load_config(path)
    fake = CompactTransport()
    original = fake.complete_compact
    def complete(**kwargs):
        result = original(**kwargs)
        result['model_info'] = {'backend': 'codex', 'selected_model': 'actual-fixture', 'provider': 'fixture'}
        result['cached_input_tokens'] = 60
        return result
    fake.complete_compact = complete
    monkeypatch.setattr(maintain, 'Transport', lambda *a, **kw: fake)
    monkeypatch.setattr(maintain, 'read_provider', lambda config=None: {
        'endpoint': 'http://127.0.0.1:1/responses', 'model': fake.model,
        'timeout': 5, 'max_output_tokens': 1200, '_api_key': 'fixture'})
    source = make_source(root, 'identity-chat')
    inbox.record(config, 'identity-chat', 'PreCompact', str(source))
    report = maintain.run_chat(config, 'identity-chat')
    assert report['state'] == 'done'
    with Store(config, read_only=True) as store:
        row = store.db.execute('SELECT model_runs_json FROM compact_batches').fetchone()
        runs = json.loads(row[0])
        assert runs[0]['selected_model'] == 'actual-fixture'
        assert runs[0]['calls'] == 1
        assert runs[0]['cached_input_tokens'] == 60
        assert 'endpoint' not in runs[0]


def test_current_only_change_gets_a_new_revision(tmp_path, monkeypatch):
    from seywo.store import Store
    from seywo.document_store import register
    from test_compact_boundaries import configured, write_source, run, native_turn
    config, fake = configured(tmp_path, monkeypatch)
    rows = native_turn(tmp_path)
    rows.extend([message('还需进行验收', 'assistant'),
                 {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 't1'}}])
    first = run(config, write_source(tmp_path, rows))
    assert first['version'] == 1
    def view_only(**kwargs):
        return {'output': {'segments': [{'title': '当前工作', 'turn_indexes': [0], 'action': 'continue', "anchor_index": None}],
            'changes': [],
            'current': {"goal_index": None, "priority_indexes": []}},
            'usage': {'input_tokens': 10, 'output_tokens': 10}, 'error': None}
    fake.complete_compact = view_only
    rows.extend([{'type': 'turn_context', 'payload': {'turn_id': 't2'}},
                 message('先清空当前重点，历史保留'), message('保留历史，清空当前重点', 'assistant')])
    second = run(config, write_source(tmp_path, rows))
    assert second['version'] == 2
    with Store(config, read_only=True) as store:
        body = json.loads(register(store.db)('live-chat', 2))
        assert len(body['items']) == 1
        assert body['overview']['goal_item_ids'] == []
        assert body['overview']['unresolved'] == []
