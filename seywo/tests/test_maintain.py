"""Detached compact maintainer: imports only after compaction, publishes, resumes."""

import json
from pathlib import Path

import pytest

from test_s2 import message, reply
from test_s3 import layout
from seywo import inbox, maintain
from seywo.narrative import Narrative
from seywo.config import initialize, load_config


class FakeTransport:
    def __init__(self, *args, **kwargs):
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        if request["phase"] == "organize":
            output = layout(request)
        elif request["phase"] == "extract":
            output = {"items": [{"type": "observation", "title": f"事项{n}",
                                 "source_indexes": [n]}
                                for n in request["primary_source_indexes"]],
                      "reads": [], "unresolved": []}
        else:
            output = reply(request)
        return {"output": output, "finish": "complete",
                "usage": {"input_tokens": 500, "output_tokens": 300}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    config = initialize(tmp_path / "project")
    path = Path(config["config"])
    data = json.loads(path.read_text())
    data["hook"] = {"enabled": True}
    path.write_text(json.dumps(data))
    monkeypatch.setattr(maintain, "read_provider", lambda config=None: {
        "endpoint": "http://127.0.0.1:1/openai/v1/responses", "model": "fake",
        "timeout": 5, "max_output_tokens": 4000})
    monkeypatch.setattr(maintain, "load_key", lambda endpoint: "key")
    monkeypatch.setattr(maintain, "Transport", FakeTransport)
    return load_config(path), tmp_path


def source(tmp_path, chat, turns):
    path = tmp_path / f"{chat}-{len(list(tmp_path.glob('*.jsonl')))}.jsonl"
    records = [{"type": "session_meta", "payload": {"id": chat, "cwd": str(tmp_path / "project")}}]
    for user, assistant in turns:
        records += [{"type": "turn_context", "payload": {"turn_id": f"t-{user}", "root_turn_id": f"t-{user}"}},
                    message(user), message(assistant, "assistant")]
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
    return path


def test_compact_publishes_and_marks_done(env, tmp_path):
    config, _ = env
    path = source(tmp_path, "chat-1", [("先完成索引", "已核对")])
    inbox.record(config, "chat-1", "PreCompact", str(path))  # not enabled by fixture yet
    report = maintain.run_chat(config, "chat-1")
    assert report["state"] == "done", report
    assert report["calls"] >= 1
    assert inbox.pending(config) == []
    state = json.loads((Path(config["_path"]).parent / "maintain" / "chat-1.json").read_text())
    assert state["version"] == 1 and state["cursor"]


def test_postcompact_boundary_only_imports(env, tmp_path):
    config, _ = env
    path = source(tmp_path, "chat-1", [("前一轮", "收到")])
    inbox.record(config, "chat-1", "PreCompact", str(path))
    maintain.run_chat(config, "chat-1")
    second = source(tmp_path, "chat-1", [("compact 之后的轮次", "继续")])
    inbox.record(config, "chat-1", "PostCompact", str(second))
    report = maintain.run_chat(config, "chat-1")
    assert report["state"] == "done"
    from seywo.store import Store
    with Store(config, read_only=True) as store:
        count = store.db.execute("SELECT count(*) FROM source_segments").fetchone()[0]
    assert count == 2
    # The turn the compact introduced is understood right away, not deferred.
    assert report["windows"] and report["windows"][0]["version"] == 2


def test_reused_snapshot_name_after_a_postcompact_becomes_a_new_segment(env, tmp_path):
    """The hook reuses one snapshot name per chat; a published post-compact
    segment owns a newer ordinal, so reusing the old path must not merge into it."""
    config, _ = env
    first = source(tmp_path, "chat-1", [("第一段", "收到")])
    inbox.record(config, "chat-1", "PreCompact", str(first))
    maintain.run_chat(config, "chat-1")
    second = source(tmp_path, "chat-1", [("post compact", "继续")])
    inbox.record(config, "chat-1", "PostCompact", str(second))
    maintain.run_chat(config, "chat-1")
    # A later PreCompact of the grown transcript lands on chat-1.jsonl again.
    third = source(tmp_path, "chat-1", [("第三段", "继续")])
    inbox.record(config, "chat-1", "PreCompact", str(third))
    report = maintain.run_chat(config, "chat-1")
    assert report["state"] == "done", report
    from seywo.store import Store
    with Store(config, read_only=True) as store:
        rows = store.db.execute(
            "SELECT ordinal,count(*) FROM current_sources GROUP BY ordinal ORDER BY ordinal").fetchall()
        segments = store.db.execute("SELECT count(*) FROM source_segments").fetchone()[0]
    assert segments == 3
    assert [tuple(r) for r in rows] == [(0, 4), (1, 4), (2, 4)]


def test_repeated_compact_prefix_never_reaches_the_model(env, tmp_path, monkeypatch):
    """Every compact file restates the whole transcript; only the added turn
    may be sent, otherwise the same turns are extracted again."""
    seen = []

    class Recording(FakeTransport):
        def complete(self, request):
            if request["phase"] == "extract":
                seen.append([(s.get("turn_id"), s.get("text","")[:12]) for s in request["sources"]])
            return super().complete(request)

    monkeypatch.setattr(maintain, "Transport", Recording)
    grown = tmp_path / "grown.jsonl"
    original = source

    config, _ = env
    first = original(tmp_path, "chat-1", [("第一段", "收到")])
    inbox.record(config, "chat-1", "PreCompact", str(first))
    maintain.run_chat(config, "chat-1")
    # The compact file repeats the whole transcript and adds one turn.
    grown.write_text(first.read_text() + "".join(
        json.dumps(r, ensure_ascii=False) + "\n" for r in [
            {"type": "turn_context", "payload": {"turn_id": "t-第三段", "root_turn_id": "t-第三段"}},
            message("第三段"), message("继续", "assistant")]))
    inbox.record(config, "chat-1", "PreCompact", str(grown))
    assert maintain.run_chat(config, "chat-1")["state"] == "done"
    assert seen, seen
    for turns in seen:
        assert {t for t, _ in turns} <= {"t-第三段", None}, turns


def test_compact_prefix_is_skipped_by_position_not_just_by_hash(env, tmp_path, monkeypatch):
    """A compact restarts at the head, so repeated records sit after the cursor.
    They must be excluded from the window while the added turn still arrives."""
    orders = []

    class Recording(FakeTransport):
        def complete(self, request):
            if request["phase"] == "extract":
                orders.append(sorted({str(s.get("turn")) for s in request["sources"]}))
            return super().complete(request)

    monkeypatch.setattr(maintain, "Transport", Recording)
    config, _ = env
    first = source(tmp_path, "chat-1", [("第一段", "收到")])
    inbox.record(config, "chat-1", "PreCompact", str(first))
    maintain.run_chat(config, "chat-1")
    orders.clear()
    grown = tmp_path / "grown2.jsonl"
    grown.write_text(first.read_text() + "".join(
        json.dumps(r, ensure_ascii=False) + "\n" for r in [
            {"type": "turn_context", "payload": {"turn_id": "t-新", "root_turn_id": "t-新"}},
            message("新的一轮"), message("好的", "assistant")]))
    inbox.record(config, "chat-1", "PreCompact", str(grown))
    assert maintain.run_chat(config, "chat-1")["state"] == "done"
    assert orders
    for window in orders:
        assert "t-新" in window and "t-第一段" not in window, window


def test_parked_job_is_resumed_by_the_next_run(env, tmp_path, monkeypatch):
    """A window that hit a transport ceiling keeps its inbox record done, so the
    next run has to pick the job up from persisted state."""
    config, _ = env
    path = source(tmp_path, "chat-1", [("一", "二")])
    inbox.record(config, "chat-1", "PreCompact", str(path))
    maintain.run_chat(config, "chat-1")
    state_path = Path(config["_path"]).parent / "maintain" / "chat-1.json"
    state = json.loads(state_path.read_text())
    job = state["job"]
    from seywo.store import Store
    with Store(config) as store:
        store.db.execute("UPDATE understanding_jobs SET state='waiting_retry_limit' WHERE id=?", (job,))
    report = maintain.run_chat(config, "chat-1")
    assert report["state"] in {"done", "partial"}, report
    with Store(config, read_only=True) as store:
        assert store.db.execute("SELECT state FROM understanding_jobs WHERE id=?", (job,)).fetchone()[0] != "waiting_retry_limit"
    # Finishing later still advances the cursor, so the next compact does not
    # re-send the window this job covered.
    assert json.loads(state_path.read_text())["cursor"]


def test_done_record_keeps_its_snapshot_so_a_resume_rebinds_the_same_bytes(env, tmp_path):
    """Retrying a parked job must not fall back to a rewritten live transcript."""
    config, _ = env
    path = source(tmp_path, "chat-1", [("一", "二")])
    item = inbox.record(config, "chat-1", "PreCompact", str(path))
    snapshot = Path(item["snapshot"])
    maintain.run_chat(config, "chat-1")
    assert snapshot.is_file()
    path.write_text("")  # the live transcript is gone/rewritten
    state_path = Path(config["_path"]).parent / "maintain" / "chat-1.json"
    state = json.loads(state_path.read_text())
    from seywo.store import Store
    with Store(config) as store:
        store.db.execute("UPDATE understanding_jobs SET state='waiting_retry_limit' WHERE id=?", (state["job"],))
    report = maintain.run_chat(config, "chat-1")
    assert report["state"] == "done", report
    with Store(config, read_only=True) as store:
        assert store.db.execute("SELECT count(*) FROM source_segments").fetchone()[0] == 1


def test_parked_job_finishes_before_a_new_window_starts(env, tmp_path):
    """A compact arriving while a window is parked must complete that window
    first, otherwise the newer job builds on a half-organized base."""
    config, _ = env
    first = source(tmp_path, "chat-1", [("一", "二")])
    inbox.record(config, "chat-1", "PreCompact", str(first))
    maintain.run_chat(config, "chat-1")
    state_path = Path(config["_path"]).parent / "maintain" / "chat-1.json"
    state = json.loads(state_path.read_text())
    from seywo.store import Store
    with Store(config) as store:
        store.db.execute("UPDATE understanding_jobs SET state='waiting_retry_limit' WHERE id=?", (state["job"],))
    grown = source(tmp_path, "chat-1", [("三", "四")])
    inbox.record(config, "chat-1", "PreCompact", str(grown))
    report = maintain.run_chat(config, "chat-1")
    assert report["state"] == "done", report
    with Store(config, read_only=True) as store:
        jobs = [tuple(r) for r in store.db.execute(
            "SELECT id,state FROM understanding_jobs ORDER BY rowid")]
        pending = store.db.execute(
            "SELECT count(*) FROM understanding_tasks WHERE state='pending'").fetchone()[0]
    # Immutable compact boundaries may retire an already published old target;
    # no pending work may be stranded when the newer target becomes ready.
    assert jobs and jobs[-1][1] == "ready"
    assert all(state in {"ready", "superseded"} for _, state in jobs)
    with Store(config, read_only=True) as store:
        for job, state in jobs:
            if state == "superseded":
                assert store.db.execute("SELECT error_code FROM understanding_jobs WHERE id=?", (job,)).fetchone()[0] == "window_superseded"
                assert store.db.execute("SELECT 1 FROM narrative_versions v JOIN understanding_tasks t ON t.id=v.task_id WHERE t.job_id=?", (job,)).fetchone()
    assert pending == 0


def test_killed_worker_job_is_resumed_despite_state_running(env, tmp_path):
    """A worker killed mid-request leaves state='running' plus an inflight
    attempt whose lease runs out. A status-only check would call that "busy"
    and abandon the window, so the next compact fails with orphan_window."""
    config, _ = env
    first = source(tmp_path, "chat-1", [("一", "二")])
    inbox.record(config, "chat-1", "PreCompact", str(first))
    maintain.run_chat(config, "chat-1")
    state_path = Path(config["_path"]).parent / "maintain" / "chat-1.json"
    state = json.loads(state_path.read_text())
    job = state["job"]
    from seywo.store import Store
    with Store(config) as store:
        store.db.execute("UPDATE understanding_jobs SET state='running' WHERE id=?", (job,))
        first_task = store.db.execute(
            "SELECT id FROM understanding_tasks WHERE job_id=? ORDER BY ordinal LIMIT 1", (job,)).fetchone()[0]
        store.db.execute("UPDATE understanding_tasks SET state='pending' WHERE id=?", (first_task,))
        store.db.execute(
            "INSERT INTO understanding_attempts VALUES('dead',?,?,0,'inflight','fake',1,1,1,"
            "'reserved_estimate',NULL,NULL,0)", (first_task, job))
    grown = source(tmp_path, "chat-1", [("三", "四")])
    inbox.record(config, "chat-1", "PreCompact", str(grown))
    report = maintain.run_chat(config, "chat-1")
    assert report.get("error_code") != "orphan_window", report
    with Store(config, read_only=True) as store:
        pending = store.db.execute(
            "SELECT count(*) FROM understanding_tasks WHERE state='pending'").fetchone()[0]
    assert pending == 0


def test_failed_window_already_covered_by_a_newer_version_is_retired(env, tmp_path):
    """A window that exhausted retries and was re-planned under a newer base
    leaves an open task nobody drives again. It must not stay a phantom
    waiting_retry_limit once the narrative has published past it."""
    config, _ = env
    first = source(tmp_path, "chat-1", [("一", "二")])
    inbox.record(config, "chat-1", "PreCompact", str(first))
    maintain.run_chat(config, "chat-1")
    state_path = Path(config["_path"]).parent / "maintain" / "chat-1.json"
    job = json.loads(state_path.read_text())["job"]
    from seywo.store import Store
    with Store(config) as store:
        store.db.execute(
            "INSERT INTO understanding_jobs(id,chat_id,target_hash,source_heads,options_json,"
            "limits_json,state,created_at) VALUES('old',?,'old','[]',?,'{}','waiting_retry_limit',0)",
            ("chat-1", json.dumps({"base_version": 0, "through_order": [0, 1]})))
        store.db.execute(
            "INSERT INTO understanding_tasks VALUES('old-task','old',0,'extract','pending','{}','h',NULL,'{}',0)",
            ())
        store.db.commit()
    maintain.run_chat(config, "chat-1")
    with Store(config, read_only=True) as store:
        row = store.db.execute("SELECT state,error_code FROM understanding_jobs WHERE id='old'").fetchone()
        task = store.db.execute("SELECT state FROM understanding_tasks WHERE id='old-task'").fetchone()
    assert row["state"] == "superseded" and row["error_code"] == "window_superseded"
    assert task["state"] == "stale"
    assert job


def test_covered_partial_job_with_only_stale_tasks_is_retired(env, tmp_path):
    """prepare() stales leftover organize slices when a window is re-planned on a
    newer base. The old job then sits in `partial` with nothing driveable, and a
    RETIRED_STATES-only sweep would keep it open for the rest of the chat."""
    config, _ = env
    first = source(tmp_path, "chat-1", [("一", "二")])
    inbox.record(config, "chat-1", "PreCompact", str(first))
    maintain.run_chat(config, "chat-1")
    state_path = Path(config["_path"]).parent / "maintain" / "chat-1.json"
    job = json.loads(state_path.read_text())["job"]
    from seywo.store import Store
    with Store(config) as store:
        store.db.execute(
            "INSERT INTO understanding_jobs(id,chat_id,target_hash,source_heads,options_json,"
            "limits_json,state,created_at) VALUES('old',?,'old','[]',?,'{}','partial',0)",
            ("chat-1", json.dumps({"base_version": 0, "through_order": [0, 1]})))
        store.db.execute(
            "INSERT INTO understanding_tasks VALUES('old-task','old',1,'organize','stale','{}','h',NULL,'{}',0)",
            ())
        store.db.commit()
    maintain.run_chat(config, "chat-1")
    with Store(config, read_only=True) as store:
        row = store.db.execute("SELECT state,error_code FROM understanding_jobs WHERE id='old'").fetchone()
    assert row["state"] == "superseded" and row["error_code"] == "window_superseded"
    assert job


def test_foreign_chat_record_is_rerouted_by_transcript_identity(env, tmp_path, monkeypatch):
    """A record queued under a root thread id must be re-homed to the chat the
    transcript names, not imported into the parent (which trips the identity
    guard and burns an attempt on every compact)."""
    config, _ = env
    root = source(tmp_path, "root", [("一", "二")])
    child = source(tmp_path, "child", [("三", "四")])
    inbox.record(config, "root", "PreCompact", str(child))
    spawned = []
    monkeypatch.setattr(maintain, "spawn_worker", lambda *a: spawned.append(a))
    report = maintain.run_chat(config, "root")
    assert report["state"] in {"done", "idle"}, report
    assert spawned, "the child chat must be driven by its own worker"
    assert spawned[0][1] == "child"
    assert [i["chat_id"] for i in inbox.pending(config)] == ["child"]
    from seywo.store import Store
    with Store(config, read_only=True) as store:
        segments = {r[0] for r in store.db.execute("SELECT chat_id FROM source_segments")}
    assert "root" not in segments
    assert root.exists()


def test_record_arriving_mid_run_is_picked_up_by_the_same_run(env, tmp_path, monkeypatch):
    """The hook spawns a worker per boundary. If a compact lands while a worker
    is busy, the second worker loses the lock and exits at once; the running
    worker must re-scan the inbox instead of leaving the newer record waiting
    for some future compact."""
    config, _ = env
    first = source(tmp_path, "chat-1", [("一", "二")])
    inbox.record(config, "chat-1", "PreCompact", str(first))
    later = source(tmp_path, "chat-1", [("三", "四")])
    seen = []

    class SlowOnce(FakeTransport):
        def complete(self, request):
            if not seen:
                seen.append(1)
                inbox.record(config, "chat-1", "PreCompact", str(later))
            return super().complete(request)

    monkeypatch.setattr(maintain, "Transport", SlowOnce)
    report = maintain.run_chat(config, "chat-1")
    assert report["state"] == "done", report
    assert all(item["state"] == "done" for item in inbox.pending(config)), inbox.pending(config)
    from seywo.store import Store
    with Store(config, read_only=True) as store:
        assert store.db.execute("SELECT count(*) FROM understanding_tasks WHERE state='pending'").fetchone()[0] == 0


def test_reused_snapshot_name_keeps_the_older_segment_readable(env, tmp_path):
    """PreCompact reuses one snapshot filename per chat. A later compact that
    finds the name already bound to an older segment must copy it aside and
    leave that segment's recorded path intact, not rename it away."""
    config, _ = env
    first = source(tmp_path, "chat-1", [("一", "二")])
    inbox.record(config, "chat-1", "PreCompact", str(first))
    maintain.run_chat(config, "chat-1")
    from seywo.store import Store
    with Store(config) as store:
        import shutil
        old = Path(store.db.execute("SELECT path FROM source_segments ORDER BY ordinal LIMIT 1").fetchone()[0])
        # Rebind the on-disk name to a newer segment, as a reused hook name does.
        moved_aside = old.with_name(old.stem + "-x" + old.suffix)
        store.db.execute("UPDATE source_segments SET path=? WHERE path=?", (str(moved_aside), str(old)))
        shutil.copyfile(old, moved_aside)
        store.db.commit()
    # Same chat id: the session_meta must keep naming the bound chat.
    second = source(tmp_path, "chat-1", [(f"三{n}", "四") for n in range(3)])
    inbox.record(config, "chat-1", "PreCompact", str(second))
    report = maintain.run_chat(config, "chat-1")
    assert report["state"] == "done", report
    with Store(config, read_only=True) as store:
        paths = [r[0] for r in store.db.execute("SELECT path FROM source_segments ORDER BY ordinal")]
    assert len(paths) == 2
    assert all(Path(p).is_file() for p in paths), paths


def test_lost_state_file_still_adopts_an_open_job(env, tmp_path):
    """The state file can be missing (fresh install, manual cleanup, another
    machine) while a job from an earlier hook is still half-published. Planning
    from scratch then collides with the frozen target and fails with
    orphan_window, so the newest open job must be adopted first."""
    config, _ = env
    path = source(tmp_path, "chat-1", [("一", "二")])
    inbox.record(config, "chat-1", "PreCompact", str(path))
    maintain.run_chat(config, "chat-1")
    state_path = Path(config["_path"]).parent / "maintain" / "chat-1.json"
    job = json.loads(state_path.read_text())["job"]
    from seywo.store import Store
    with Store(config) as store:
        store.db.execute("UPDATE understanding_jobs SET state='waiting_retry_limit' WHERE id=?", (job,))
        task = store.db.execute("SELECT id FROM understanding_tasks WHERE job_id=? ORDER BY ordinal LIMIT 1", (job,)).fetchone()[0]
        store.db.execute("UPDATE understanding_tasks SET state='pending' WHERE id=?", (task,))
        store.db.commit()
    state_path.unlink()
    grown = source(tmp_path, "chat-1", [("三", "四")])
    inbox.record(config, "chat-1", "PreCompact", str(grown))
    report = maintain.run_chat(config, "chat-1")
    assert report.get("error_code") != "orphan_window", report
    assert report["state"] == "done", report


def test_parked_job_on_a_retired_alias_moves_to_the_current_model(env, tmp_path):
    """An alias can vanish from the gateway after a job froze it. Retrying it to
    the ceiling parks the chat forever, so a parked window must be re-based on
    the configured model instead."""
    config, _ = env
    path = source(tmp_path, "chat-1", [("一", "二")])
    inbox.record(config, "chat-1", "PreCompact", str(path))
    maintain.run_chat(config, "chat-1")
    state_path = Path(config["_path"]).parent / "maintain" / "chat-1.json"
    job = json.loads(state_path.read_text())["job"]
    from seywo.store import Store
    with Store(config) as store:
        old = json.loads(store.db.execute(
            "SELECT options_json FROM understanding_jobs WHERE id=?", (job,)).fetchone()[0])
        last = store.db.execute(
            "SELECT id FROM understanding_tasks WHERE job_id=? ORDER BY ordinal DESC LIMIT 1", (job,)).fetchone()[0]
        store.db.execute("UPDATE understanding_tasks SET state='pending' WHERE id=?", (last,))
        store.db.execute("UPDATE understanding_jobs SET state='waiting_retry_limit',options_json=? WHERE id=?",
                         (json.dumps({**old, "model": "retired-alias"}), job))
        store.db.commit()
    report = maintain.run_chat(config, "chat-1")
    assert report["state"] == "done", report
    with Store(config, read_only=True) as store:
        retired = store.db.execute("SELECT state,error_code FROM understanding_jobs WHERE id=?", (job,)).fetchone()
        open_tasks = store.db.execute(
            "SELECT count(*) FROM understanding_tasks WHERE state IN ('pending','inflight','blocked')").fetchone()[0]
    # The published narrative already carries the work, so the dead window is
    # just closed out rather than re-run against the retired model.
    assert (retired["state"], retired["error_code"]) == ("superseded", "model_alias_changed")
    assert open_tasks == 0


def test_waiting_context_does_not_spin_without_spending_calls(env, tmp_path, monkeypatch):
    """waiting_context returns before measuring, and raising token budgets adds
    no capacity. Re-queuing it blindly looped for minutes with calls=0."""
    config, tmp_path = env
    path = source(tmp_path, "chat-1", [("一", "二")])
    inbox.record(config, "chat-1", "PreCompact", str(path))
    maintain.run_chat(config, "chat-1")
    state_path = Path(config["_path"]).parent / "maintain" / "chat-1.json"
    job = json.loads(state_path.read_text())["job"]
    from seywo.store import Store
    from seywo.understanding import Options, Understanding
    with Store(config) as store:
        task = store.db.execute(
            "SELECT id FROM understanding_tasks WHERE job_id=? ORDER BY ordinal LIMIT 1", (job,)).fetchone()[0]
        store.db.execute("UPDATE understanding_tasks SET state='pending' WHERE id=?", (task,))
        store.db.execute("UPDATE understanding_attempts SET state='failed' WHERE task_id=?", (task,))
        store.db.execute("UPDATE understanding_jobs SET state='waiting_context' WHERE id=?", (job,))
        store.db.commit()
        engine = Understanding(store)
        options = Options.from_dict(json.loads(store.db.execute(
            "SELECT options_json FROM understanding_jobs WHERE id=?", (job,)).fetchone()[0]))
        monkeypatch.setattr(Understanding, "_fits", lambda *a, **k: False)
        outcome = maintain._run_job(engine, Narrative(store), FakeTransport(), job, options,
                                    {"windows": []})
    assert outcome["state"] == "waiting_context"
    assert outcome["error_code"] == "context_ceiling"
    assert outcome["calls"] == 0


def test_missing_credential_blocks_without_losing_record(env, tmp_path, monkeypatch):
    config, _ = env
    monkeypatch.setattr(maintain, "load_key", lambda endpoint: "")
    path = source(tmp_path, "chat-1", [("一", "二")])
    inbox.record(config, "chat-1", "PreCompact", str(path))
    assert maintain.run_chat(config, "chat-1")["error_code"] == "missing_provider_credential"
    assert len(inbox.pending(config)) == 1


def test_missing_snapshot_marks_failed_and_keeps_record(env, tmp_path):
    config, _ = env
    path = source(tmp_path, "chat-1", [("一", "二")])
    item = inbox.record(config, "chat-1", "PreCompact", str(path))
    Path(item["snapshot"]).unlink()
    path.unlink()
    assert maintain.run_chat(config, "chat-1")["state"] == "failed"
    assert inbox.pending(config) == []
    assert inbox.failed(config, "chat-1")[0]["error_code"] == "source_missing"


def test_hook_disabled_skips_work(env):
    config, _ = env
    path = Path(config["_path"])
    data = json.loads(path.read_text())
    data["hook"] = {"enabled": False}
    path.write_text(json.dumps(data))
    assert maintain.main(["--config", str(path), "--chat", "chat-1"]) == 0


def test_unchanged_source_spends_nothing(env, tmp_path):
    config, _ = env
    path = source(tmp_path, "chat-1", [("一", "二")])
    inbox.record(config, "chat-1", "PreCompact", str(path))
    assert maintain.run_chat(config, "chat-1")["state"] == "done"
    # The same bytes arrive again through a second PreCompact.
    inbox.record(config, "chat-1", "PreCompact", str(path))
    report = maintain.run_chat(config, "chat-1")
    assert report["state"] == "done" and report["calls"] == 0


def test_failed_record_is_retried_by_the_next_run_until_the_limit(env, tmp_path):
    config, _ = env
    path = source(tmp_path, "chat-1", [("一", "二")])
    inbox.record(config, "chat-1", "PreCompact", str(path))
    claimed = inbox.claim(config, inbox.pending(config)[0])
    inbox.finish(config, claimed, state="failed", error="transient")
    assert maintain.run_chat(config, "chat-1")["state"] == "done"
    assert inbox.failed(config, "chat-1") == []


def test_stale_organize_task_is_recomputed_not_reused(env, tmp_path):
    """prepare() reused a stale task verbatim; claim() only picks pending tasks,
    so _run_job saw neither done nor pending and spun forever."""
    config, tmp_path = env
    path = source(tmp_path, "chat-1", [("一", "二")])
    inbox.record(config, "chat-1", "PreCompact", str(path))
    maintain.run_chat(config, "chat-1")
    state_path = Path(config["_path"]).parent / "maintain" / "chat-1.json"
    job = json.loads(state_path.read_text())["job"]
    from seywo.store import Store
    from seywo.understanding import Options, Understanding
    with Store(config) as store:
        task = store.db.execute(
            "SELECT id FROM understanding_tasks WHERE job_id=? AND phase='organize' "
            "ORDER BY ordinal DESC LIMIT 1", (job,)).fetchone()[0]
        store.db.execute("UPDATE understanding_tasks SET state='stale' WHERE id=?", (task,))
        store.db.execute("UPDATE understanding_jobs SET state='pending' WHERE id=?", (job,))
        store.db.commit()
        engine = Understanding(store)
        options = Options.from_dict(json.loads(store.db.execute(
            "SELECT options_json FROM understanding_jobs WHERE id=?", (job,)).fetchone()[0]))
        outcome = maintain._run_job(engine, Narrative(store), FakeTransport(), job, options,
                                    {"windows": []})
    assert outcome["state"] == "published", outcome
    assert outcome["version"] == 2


def test_blocked_organize_task_parks_instead_of_spinning(env, tmp_path):
    config, tmp_path = env
    path = source(tmp_path, "chat-1", [("一", "二")])
    inbox.record(config, "chat-1", "PreCompact", str(path))
    maintain.run_chat(config, "chat-1")
    state_path = Path(config["_path"]).parent / "maintain" / "chat-1.json"
    job = json.loads(state_path.read_text())["job"]
    from seywo.store import Store
    from seywo.understanding import Options, Understanding
    with Store(config) as store:
        task = store.db.execute(
            "SELECT id FROM understanding_tasks WHERE job_id=? AND phase='organize' "
            "ORDER BY ordinal DESC LIMIT 1", (job,)).fetchone()[0]
        store.db.execute("UPDATE understanding_tasks SET state='blocked' WHERE id=?", (task,))
        store.db.execute("UPDATE understanding_jobs SET state='pending' WHERE id=?", (job,))
        store.db.commit()
        engine = Understanding(store)
        options = Options.from_dict(json.loads(store.db.execute(
            "SELECT options_json FROM understanding_jobs WHERE id=?", (job,)).fetchone()[0]))
        # Nothing can be sliced small enough any more.
        engine._fits = lambda *a, **k: False
        outcome = maintain._run_job(engine, Narrative(store), FakeTransport(), job, options,
                                    {"windows": []})
    # A blocked window parks with zero calls instead of spinning on a task that
    # claim() no longer hands out.
    assert outcome["state"] in {"blocked", "waiting_context"}
    assert outcome["error_code"] == "context_ceiling"
    assert outcome["calls"] == 0


def test_state_cursor_follows_published_window(env, tmp_path):
    """A stale cache must not make the planner reopen an already-read window."""
    config, tmp_path = env
    first = source(tmp_path, "chat-1", [("一", "二")])
    inbox.record(config, "chat-1", "PreCompact", str(first))
    maintain.run_chat(config, "chat-1")
    state_path = Path(config["_path"]).parent / "maintain" / "chat-1.json"
    state = json.loads(state_path.read_text())
    published, cursor = state["job"], state["cursor"]
    assert published and cursor
    # Rewind the cache while the narrative stays ahead of it.
    state_path.write_text(json.dumps({**state, "job": None, "cursor": None}))
    report = maintain.run_chat(config, "chat-1")
    assert report["state"] == "done", report
    after = json.loads(state_path.read_text())
    assert after["job"] == published and after["cursor"] == cursor


def test_initial_model_failure_reports_partial_and_actual_calls(env, tmp_path, monkeypatch):
    config, _ = env
    path = source(tmp_path, 'bad-chat', [('检查未完成事项', '检查中')])
    monkeypatch.setattr(FakeTransport, 'complete', lambda self, request: {
        'output': {'invalid': 'delta'}, 'finish': 'complete',
        'usage': {'input_tokens': 11, 'output_tokens': 7}})
    monkeypatch.setattr(maintain, 'RETRY_WAIT_BUDGET', 0)
    inbox.record(config, 'bad-chat', 'PreCompact', str(path))
    report = maintain.run_chat(config, 'bad-chat')
    assert report['state'] == 'partial'
    assert report['calls'] == 3
    assert report['pending_job']['state'] == 'waiting_retry_limit'
