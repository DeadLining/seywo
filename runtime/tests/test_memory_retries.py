"""Bounded automatic retry and durable, same-chat unfinished-batch recovery."""

import json

import pytest

from sessiongraph.extraction import ProviderError
from sessiongraph.memories import run_chat, _state_path
from sessiongraph.auto_update import run_update
from sessiongraph.core import Store, init_project
from test_memories import FakeExtractor, make_store
from test_mvp import write_session


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    waits = []
    monkeypatch.setattr("sessiongraph.memories.time.sleep", waits.append)
    return waits


@pytest.mark.parametrize(
    "code",
    [
        "provider_timeout",
        "provider_transport",
        "provider_http_429",
        "provider_http_503",
        "provider_empty_output",
    ],
)
def test_single_fragment_retries_then_continues(tmp_path, no_wait, code):
    _, store, _ = make_store(tmp_path, [("user", "可保留信息" * 2500)])
    calls = []

    def provider(batch, policy):
        calls.append(batch)
        if len(calls) <= 2:
            raise ProviderError("private server error", code)
        return {"memories": []}

    result = run_chat(store, provider, "chat-1")
    assert not result["failed"]
    assert calls[0] == calls[1] == calls[2]
    assert no_wait == [1.0, 2.0]
    assert result["retry_count"] == 2 and result["model_calls"] == len(calls)
    assert result["pending_retry_count"] == 0
    store.close()


@pytest.mark.parametrize(
    "code",
    [
        "provider_http_401",
        "provider_http_403",
        "provider_http_400",
        "provider_refused",
        "provider_response_too_large",
    ],
)
def test_permanent_failure_not_retried(tmp_path, no_wait, code):
    _, store, _ = make_store(tmp_path, [("user", "记住目标")])
    calls = []

    def provider(batch, policy):
        calls.append(batch)
        raise ProviderError("private credentials", code)

    result = run_chat(store, provider, "chat-1")
    assert len(calls) == 1 and not no_wait
    assert result["failed"][0]["retryable"] is False
    assert result["pending_retry_count"] == 0
    assert "private credentials" not in json.dumps(result)
    store.close()


def test_exhausted_old_segment_is_recovered_before_new_segment_without_history_backfill(tmp_path):
    root, store, _ = make_store(tmp_path, [("user", "失败片段目标")])

    def fail(batch, policy):
        raise ProviderError("timeout", "provider_timeout")

    result = run_chat(store, fail, "chat-1", segment_id="chat-1")
    assert result["model_calls"] == 3 and result["pending_retry_count"] == 1
    state = json.loads(_state_path(store, "chat-1").read_text())
    assert state["pending_batches"]["chat-1"]["status"] == "pending"
    other = write_session(
        root / "never-selected.jsonl", root, "chat-1", [("user", "不要回填旧历史")]
    )
    store.ingest(other)
    new = write_session(root / "new.jsonl", root, "chat-1", [("user", "当前新段")])
    new_segment = store.ingest(new)["session_id"]
    extractor = FakeExtractor([])
    result = run_chat(
        store, extractor, "chat-1", segment_id=new_segment, since_time="2099-01-01T00:00:00Z"
    )
    assert [r["text"] for batch, _ in extractor.calls for r in batch] == ["失败片段目标"]
    assert result["recovered_segment_ids"] == ["chat-1"]
    assert not json.loads(_state_path(store, "chat-1").read_text())["pending_batches"]
    assert result["pending_retry_count"] == 0
    store.close()


def test_inflight_checkpoint_survives_process_crash(tmp_path):
    _, store, _ = make_store(tmp_path, [("user", "不可遗忘")])

    def crash(batch, policy):
        raise RuntimeError("process interruption")

    with pytest.raises(RuntimeError):
        run_chat(store, crash, "chat-1")
    state = json.loads(_state_path(store, "chat-1").read_text())
    assert state["pending_batches"]["chat-1"]["status"] == "in_flight"
    ext = FakeExtractor([])
    assert not run_chat(store, ext, "chat-1")["failed"]
    assert len(ext.calls) == 1
    store.close()


def test_pending_other_chat_is_rejected_without_provider_call(tmp_path):
    root, store, _ = make_store(tmp_path, [("user", "当前chat")])
    other = write_session(root / "other.jsonl", root, "other-chat", [("user", "禁止跨chat")])
    store.ingest(other)
    path = _state_path(store, "chat-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "segments": {},
                "pending_batches": {
                    "other-chat": {
                        "from_line": 2,
                        "through_line": 2,
                        "since_time": None,
                        "status": "pending",
                        "retryable": True,
                    }
                },
            }
        )
    )
    ext = FakeExtractor([])
    with pytest.raises(ValueError, match="pending"):
        run_chat(store, ext, "chat-1")
    assert ext.calls == []
    store.close()


def test_runner_schedules_recovery_without_another_compact(tmp_path, no_wait):
    config = init_project(tmp_path)
    from sessiongraph.core import load_config

    Store(load_config(config)).close()
    workers = []

    def execute(argv, timeout):
        if "refresh-chat" in argv:
            return {"imports": [{}], "added_entries": 0, "skipped": []}
        workers.append(argv)
        if len(workers) == 1:
            return {
                "saved": ["first"],
                "saved_relations": [],
                "failed": [{"error_code": "provider_timeout", "retryable": True}],
                "failure_codes": ["provider_timeout"],
                "pending_retry_count": 1,
                "model_calls": 3,
                "retry_count": 2,
            }
        return {
            "saved": ["second"],
            "saved_relations": ["edge"],
            "failed": [],
            "pending_retry_count": 0,
            "model_calls": 1,
            "retry_count": 0,
        }

    result = run_update(config, "chat", "PostCompact", execute=execute)
    assert len(workers) == 2 and no_wait == [15.0]
    assert "--memories-pending-only" in workers[1]
    assert "--memories-pending-only" not in workers[0]
    assert result["state"] == "succeeded" and result["recovery_rounds"] == 1
    assert result["saved_count"] == 2 and result["reported_model_calls"] == 4
    assert result["saved_relation_count"] == 1


def test_runner_recovery_is_bounded(tmp_path, no_wait):
    config = init_project(tmp_path)
    from sessiongraph.core import load_config

    Store(load_config(config)).close()
    calls = []

    def execute(argv, timeout):
        if "refresh-chat" in argv:
            return {"imports": [{}], "added_entries": 0, "skipped": []}
        calls.append(argv)
        return {
            "saved": [],
            "failed": [{"error_code": "provider_timeout", "retryable": True}],
            "failure_codes": ["provider_timeout"],
            "pending_retry_count": 1,
            "model_calls": 3,
            "retry_count": 2,
        }

    result = run_update(config, "chat", "PostCompact", execute=execute)
    assert len(calls) == 3 and no_wait == [15.0, 60.0]
    assert result["state"] == "failed" and result["pending_retry_count"] == 1
    assert result["recovery_exhausted"] is True and result["reported_model_calls"] == 9


def test_pending_only_does_not_retry_permanent_or_new_messages(tmp_path):
    root, store, _ = make_store(tmp_path, [("user", "永久失败")])

    def refused(batch, policy):
        raise ProviderError("refused", "provider_refused")

    run_chat(store, refused, "chat-1")
    new = write_session(root / "new.jsonl", root, "chat-1", [("user", "还未授权的本轮新内容")])
    store.ingest(new)
    ext = FakeExtractor([])
    result = run_chat(store, ext, "chat-1", pending_only=True)
    assert ext.calls == [] and result["blocked_retry_count"] == 1
    store.close()


def test_retry_budget_cannot_grow_with_number_of_batches(no_wait):
    from sessiongraph.memory_retry import RetryBudget

    budget = RetryBudget()

    def fail():
        raise ProviderError("timeout", "provider_timeout")

    for _ in range(10):
        with pytest.raises(ProviderError):
            budget.call(fail)
    assert budget.retries == 8 and budget.attempts == 18 and len(no_wait) == 8


def test_recovered_transient_does_not_hide_permanent_failure(tmp_path):
    from sessiongraph.core import load_config

    config = init_project(tmp_path)
    Store(load_config(config)).close()
    workers = []

    def execute(argv, timeout):
        if "refresh-chat" in argv:
            return {"imports": [{}], "skipped": []}
        workers.append(argv)
        if len(workers) == 1:
            return {
                "saved": [],
                "failed": [
                    {"error_code": "provider_http_401", "retryable": False},
                    {"error_code": "provider_timeout", "retryable": True},
                ],
                "pending_retry_count": 1,
                "blocked_retry_count": 1,
                "model_calls": 4,
            }
        return {
            "saved": ["recovered"],
            "failed": [],
            "pending_retry_count": 0,
            "blocked_retry_count": 1,
            "model_calls": 1,
        }

    result = run_update(config, "chat", "PostCompact", execute=execute)
    assert len(workers) == 2 and result["saved_count"] == 1
    assert result["state"] == "failed" and result["failed_batches"] == 1
    assert result["error_code"] == "provider_http_401"


def test_gateway_forwards_pending_only_flag(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from sessiongraph import gateway_worker

    config = init_project(tmp_path)
    forwarded = []
    monkeypatch.setattr(gateway_worker, "gateway_credentials", lambda *args: nullcontext())
    monkeypatch.setattr(gateway_worker, "cli_main", lambda argv: forwarded.append(argv) or 0)
    monkeypatch.setattr(
        "sys.argv",
        [
            "gateway_worker",
            "--config",
            str(config),
            "--allow-network",
            "--memories",
            "chat-1",
            "--memories-pending-only",
        ],
    )
    assert gateway_worker.main() == 0
    assert "--pending-only" in forwarded[0]


def test_cli_pending_recovery_respects_network_gate(tmp_path, monkeypatch, capsys):
    from sessiongraph import cli

    root, store, _ = make_store(tmp_path, [("user", "之前失败的内容")])

    def fail(batch, policy):
        raise ProviderError("timeout", "provider_timeout")

    run_chat(store, fail, "chat-1")
    store.close()
    ext = FakeExtractor([])
    monkeypatch.setattr("sessiongraph.extraction.create_extractor", lambda config: ext)
    argv = [
        "--config",
        str(root / ".sessiongraph/config.json"),
        "memories",
        "--chat-id",
        "chat-1",
        "--pending-only",
    ]
    assert cli.main(argv) == 2
    capsys.readouterr()
    assert ext.calls == []
    assert cli.main(argv + ["--allow-network"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["pending_retry_count"] == 0 and len(ext.calls) == 1
