import json
import subprocess

from sessiongraph.auto_update import run_update, update_status
from sessiongraph.core import Store, init_project, load_config
from sessiongraph.work_map import work_map


def test_success_has_stages_and_distinguishes_hook_from_manual(tmp_path):
    config = init_project(tmp_path)
    Store(load_config(config)).close()
    calls = []

    def execute(argv, timeout):
        calls.append(argv)
        state = update_status(load_config(config))["latest"]
        assert state["state"] in {"importing", "extracting"}
        return (
            {"added_entries": 3, "skipped": []}
            if "refresh-chat" in argv
            else {"saved": ["a"], "failed": [], "model_calls": 1}
        )

    result = run_update(config, "chat", "PostCompact", execute=execute)
    assert result["state"] == "succeeded" and result["saved_count"] == 1
    assert result["event"] == "PostCompact" and result["origin"] == "hook_input"
    assert len(calls) == 2
    assert (tmp_path / ".sessiongraph/hook-state/chat.needs-restore").exists()
    assert update_status(load_config(config))["latest"]["run_id"] == result["run_id"]


def test_import_failure_does_not_extract_or_leak_error(tmp_path):
    config = init_project(tmp_path)
    Store(load_config(config)).close()
    calls = []

    def execute(argv, timeout):
        calls.append(argv)
        raise RuntimeError("secret credential and chat text")

    result = run_update(config, "chat", "manual", execute=execute)
    assert result["state"] == "failed" and result["error_code"] == "import_failed"
    assert result["origin"] == "manual" and len(calls) == 1
    assert "secret" not in json.dumps(update_status(load_config(config)))


def test_partial_failure_and_timeout_not_success(tmp_path):
    config = init_project(tmp_path)
    Store(load_config(config)).close()

    def execute(argv, timeout):
        if "refresh-chat" in argv:
            return {"added_entries": 0, "skipped": []}
        return {"saved": ["one"], "failed": [{"error": "secret"}], "model_calls": 1}

    result = run_update(config, "chat", "PostCompact", execute=execute)
    assert result["state"] == "failed" and result["saved_count"] == 1
    assert result["error_code"] == "extraction_partial_failure"

    def timeout(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    assert run_update(config, "chat", "manual", execute=timeout)["state"] == "failed"


def test_corrupt_state_and_stale_jobs_are_read_only(tmp_path):
    config = load_config(init_project(tmp_path))
    Store(config).close()
    folder = tmp_path / ".sessiongraph/hook-state/runs"
    folder.mkdir(parents=True)
    (folder / "bad.json").write_text("bad")
    (folder / "old.json").write_text(
        json.dumps(
            {
                "run_id": "old",
                "chat_id": "c",
                "state": "extracting",
                "updated_at": 1,
                "created_at": 1,
            }
        )
    )
    result = update_status(config)
    assert result["latest"]["state"] == "stalled"
    assert json.loads((folder / "old.json").read_text())["state"] == "extracting"
    assert result["unreadable_runs"] == 1


def test_revision_changes_for_content_not_just_counts(tmp_path):
    config = load_config(init_project(tmp_path))
    Store(config).close()
    store = Store(config)
    from test_mvp import write_session

    p = write_session(tmp_path / "a.jsonl", tmp_path, "c", [("user", "记录这件事")])
    store.ingest(p)
    eid = store.db.execute("select id from entries").fetchone()[0]
    item = store.remember(
        kind="goal",
        title="目标",
        content="记录这件事",
        source_entry_id=eid,
        source_quote="记录这件事",
    )
    before = update_status(config)["revision"]
    assert before == work_map(store)["revision"]
    store.db.execute("update work_memory set content=? where id=?", ("新内容", item["id"]))
    store.db.commit()
    after = update_status(config)["revision"]
    assert before != after and after == work_map(store)["revision"]
    store.close()


def test_skipped_import_is_visible_and_empty_extraction_is_noop(tmp_path):
    config = init_project(tmp_path)
    Store(load_config(config)).close()

    def execute(argv, timeout):
        return (
            {"skipped": ["denied"]}
            if "refresh-chat" in argv
            else {"saved": [], "failed": [], "model_calls": 0}
        )

    result = run_update(config, "chat", "manual", execute=execute)
    assert result["state"] == "failed" and result["error_code"] == "import_sources_skipped"

    def empty(argv, timeout):
        return (
            {"added_entries": 0, "skipped": []}
            if "refresh-chat" in argv
            else {"saved": [], "failed": [], "model_calls": 0}
        )

    result = run_update(config, "chat", "manual", execute=empty)
    assert result["state"] == "succeeded" and result["saved_count"] == 0


def test_pre_and_post_are_serialized_and_post_reimports(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    config = init_project(tmp_path)
    Store(load_config(config)).close()
    entered = threading.Event()
    release = threading.Event()
    calls = []
    both_entered = threading.Event()

    def execute(argv, timeout):
        calls.append(argv)
        if len(calls) == 2:
            both_entered.set()
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
        return (
            {"added_entries": 0, "skipped": []}
            if "refresh-chat" in argv
            else {"saved": [], "failed": [], "model_calls": 0}
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(run_update, config, "chat", "PreCompact", "", execute)
        assert entered.wait(2)
        b = pool.submit(run_update, config, "chat", "PostCompact", "", execute)
        release.set()
        assert a.result()["state"] == "prepared"
        assert b.result()["state"] == "succeeded"
    assert ["refresh-chat" in argv for argv in calls] == [True, True, False]


def test_runner_subprocess_invalid_json_failure_timeout(tmp_path):
    import sys
    import pytest
    from sessiongraph.auto_update import _execute

    assert _execute([sys.executable, "-c", 'print("{}")'], 2) == {}
    from sessiongraph.failures import Failure
    with pytest.raises(Failure) as failure:
        _execute([sys.executable, "-c", "raise SystemExit(1)"], 2)
    assert failure.value.code == "worker_failed" and failure.value.stage == "worker"
    with pytest.raises(ValueError):
        _execute([sys.executable, "-c", 'print("[]")'], 2)
    with pytest.raises(subprocess.TimeoutExpired):
        _execute([sys.executable, "-c", "import time;time.sleep(2)"], 0.05)


def test_fragment_coverage_is_reported_without_oversize_warning(tmp_path):
    config = init_project(tmp_path)
    Store(load_config(config)).close()

    def execute(argv, timeout):
        if "refresh-chat" in argv:
            return {"added_entries": 1, "skipped": []}
        return {
            "saved": ["one"],
            "failed": [],
            "model_calls": 3,
            "fragment_batches": 3,
            "dropped_oversize": 0,
        }

    result = run_update(config, "chat", "PostCompact", execute=execute)
    assert result["state"] == "succeeded"
    assert result["fragment_batches"] == 3
    assert not result.get("warning_code")


def test_relation_only_update_rearms_restore_and_reports_rejections(tmp_path):
    config = init_project(tmp_path)
    Store(load_config(config)).close()

    def execute(argv, timeout):
        if "refresh-chat" in argv:
            return {"added_entries": 1, "skipped": []}
        return {
            "saved": [],
            "saved_relations": ["wr_one"],
            "rejected_relations": [{"reason": "invalid_endpoint"}],
            "failed": [],
            "model_calls": 1,
        }

    result = run_update(config, "chat", "PostCompact", execute=execute)
    assert result["state"] == "succeeded" and result["saved_relation_count"] == 1
    assert result["rejected_relation_count"] == 1
    assert (tmp_path / ".sessiongraph/hook-state/chat.needs-restore").exists()


def test_independent_projects_overlap_under_shared_runtime_lease(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    monkeypatch.setenv("SESSIONGRAPH_WORKER_LOCK", str(tmp_path / "gateway-extract.lock"))
    roots = [tmp_path / "a", tmp_path / "b"]
    for root in roots:
        root.mkdir()
    configs = [init_project(root) for root in roots]
    for config in configs:
        Store(load_config(config)).close()
    entered = threading.Event()
    release = threading.Event()
    calls = []
    both_entered = threading.Event()

    def execute(argv, timeout):
        calls.append(argv)
        if len(calls) == 2:
            both_entered.set()
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
        return (
            {"added_entries": 0, "skipped": []}
            if "refresh-chat" in argv
            else {"saved": [], "failed": [], "model_calls": 0}
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(run_update, configs[0], "one", "PreCompact", "", execute)
        assert entered.wait(2)
        b = pool.submit(run_update, configs[1], "two", "PreCompact", "", execute)
        import time

        assert both_entered.wait(2), "independent project was blocked"
        assert len(calls) == 2
        release.set()
        assert a.result()["state"] == b.result()["state"] == "prepared"


def test_graph_budget_continues_without_pending_only_filter(tmp_path):
    config = init_project(tmp_path)
    Store(load_config(config)).close()
    calls = []
    def execute(argv, timeout):
        if "refresh-chat" in argv:
            return {"added_entries": 3, "skipped": []}
        calls.append(argv)
        return {"saved": [str(len(calls))], "failed": [], "model_calls": 1,
                "graph_v2": {"budget_exhausted": len(calls) == 1}}
    result = run_update(config, "chat", execute=execute)
    assert result["state"] == "succeeded" and result["saved_count"] == 2
    assert result["continuation_rounds"] == 1 and result["reported_model_calls"] == 2
    assert all("--memories-pending-only" not in args for args in calls)


def test_no_progress_cannot_spin_or_report_success(tmp_path):
    config = init_project(tmp_path)
    Store(load_config(config)).close()
    def execute(argv, timeout):
        if "refresh-chat" in argv:
            return {"added_entries": 3, "skipped": []}
        return {"saved": [], "failed": [], "graph_v2": {"budget_exhausted": True}}
    result = run_update(config, "chat", execute=execute)
    assert result["state"] == "failed"
    assert result["error_code"] == "continuation_no_progress"
    assert result["blocked_retry_count"] == 1 and result["continuation_rounds"] == 2


def test_call_budget_receipt_and_mixed_failure_priority(tmp_path):
    config = init_project(tmp_path)
    data = json.loads(config.read_text())
    data["graph_v2"] = {"enabled": True}
    data.setdefault("hook", {})["max_model_calls"] = 2
    config.write_text(json.dumps(data))
    Store(load_config(config)).close()
    budget = {"error_code": "graph_call_budget_pending", "retryable": True}
    for failures, expected_state, expected_code in [
        ([budget], "pending", "graph_call_budget_pending"),
        ([budget, {"error_code": "graph_validation", "retryable": False}], "failed", "graph_validation"),
        ([budget, {"error_code": "provider_timeout", "retryable": True}], "failed", "provider_timeout"),
    ]:
        calls = []
        def execute(argv, timeout):
            calls.append(argv)
            if "refresh-chat" in argv:
                return {"added_entries": 0, "skipped": []}
            current = update_status(load_config(config))["latest"]
            assert current["model_call_limit"] is None
            return {"saved": ["node"], "failed": failures, "model_calls": 2,
                    "pending_retry_count": 1, "graph_v2": {"source_remaining": True}}
        result = run_update(config, "chat", "manual", execute=execute)
        assert result["state"] == expected_state
        assert result["error_code"] == expected_code
        assert result["model_call_limit"] is None
        assert result["reported_model_calls"] == 2
        assert result["failed_batches"] == len(failures) - 1
        assert result["saved_count"] == 1 and result["graph_v2"]["source_remaining"]
        assert len(calls) == 2  # Respect an explicit legacy worker deferral; don't hide accompanying failures.


def test_continuous_run_crosses_old_call_round_and_time_limits(tmp_path, monkeypatch):
    from sessiongraph import auto_update as a
    config = init_project(tmp_path)
    raw = json.loads(config.read_text())
    raw['hook'] = {'max_model_calls': 1}
    config.write_text(json.dumps(raw))
    Store(load_config(config)).close()
    clock = [0]
    monkeypatch.setattr(a.time, 'monotonic', lambda: clock[0])
    calls = []
    def execute(argv, timeout):
        if 'refresh-chat' in argv:
            return {'added_entries': 80, 'skipped': []}
        assert '--memories-call-budget' not in argv
        calls.append(argv)
        clock[0] += 100  # Total healthy work exceeds former 30-minute task deadline.
        return {'saved': ['node-' + str(len(calls))], 'failed': [], 'model_calls': 3,
                'graph_v2': {'source_remaining': len(calls) < 70, 'input_source_chars': 100}}
    result = run_update(config, 'chat', 'PostCompact', execute=execute)
    assert result['state'] == 'succeeded'
    assert result['reported_model_calls'] == 210
    assert result['saved_count'] == 70 and result['continuation_rounds'] == 69
    assert result['model_call_limit'] is None
    assert result['scheduling_policy'] == 'continuous'


def test_same_project_keeps_writer_exclusion(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    monkeypatch.setenv('SESSIONGRAPH_WORKER_LOCK', str(tmp_path / 'runtime.lock'))
    config = init_project(tmp_path)
    Store(load_config(config)).close()
    entered, release, second = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def execute(argv, timeout):
        calls.append(argv)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
        else:
            second.set()
        return {'added_entries': 0, 'skipped': []}
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(run_update, config, 'a', 'PreCompact', '', execute)
        assert entered.wait(2)
        b = pool.submit(run_update, config, 'b', 'PreCompact', '', execute)
        try:
            assert not second.wait(.15)
        finally:
            release.set()
        assert a.result()['state'] == b.result()['state'] == 'prepared'
        assert second.is_set()


def test_backlog_only_yields_without_failure_or_no_progress_loop(tmp_path):
    config=init_project(tmp_path);Store(load_config(config)).close()
    calls=[]
    def execute(argv,timeout):
        calls.append(argv)
        assert len(calls)<=3, 'backlog-only work must yield'
        if 'refresh-chat' in argv:return {'added_entries':1,'skipped':[]}
        return dict(saved=['node'],saved_relations=['edge'],failed=[],model_calls=4,
            backlog_failures=[dict(job_id='old',error_code='jev_http_451',retryable=False)],
            graph_v2=dict(durable_stages=True,source_remaining=True,runnable_remaining=False,
                          blocked_jobs=1,pending_jobs=1,ready_jobs=0,retry_wait_jobs=0))
    result=run_update(config,'chat','PostCompact',execute=execute)
    assert len(calls)==2
    assert result['state']=='pending' and result['error_code'] is None
    assert result['attempt_outcome']=='succeeded'
    assert result['failure_codes']==[] and result['failed_batches']==0
    assert result['reported_model_calls']==4
    assert result['backlog_failures'][0]['error_code']=='jev_http_451'


def test_circuit_wait_is_deferred_and_does_not_spin(tmp_path):
    config=init_project(tmp_path);Store(load_config(config)).close()
    calls=[]
    def execute(argv,timeout):
        calls.append(argv)
        assert len(calls)<=2, 'circuit wait must yield without another dispatch'
        if 'refresh-chat' in argv:return {'added_entries':1,'skipped':[]}
        return dict(saved=[],saved_relations=[],failed=[{'error_code':'resource_circuit_open'}],
            model_calls=0,graph_v2=dict(durable_stages=True,source_remaining=True,
                runnable_remaining=True,next_retry_at=9999999999))
    result=run_update(config,'chat','PostCompact',execute=execute)
    assert len(calls)==2 and result['reported_model_calls']==0
    assert result['state']=='pending' and result['attempt_outcome']=='deferred'
    assert result['error_code']=='resource_circuit_open' and result['failed_batches']==0
