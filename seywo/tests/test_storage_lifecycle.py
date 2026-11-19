"""Failure-first acceptance of storage ownership and monotonic progress."""

import json
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from seywo import archive, storage_admin
from seywo.config import initialize, load_config
from seywo.store import Store
from test_storage_v2 import message, source


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    initialize(root)
    return root, load_config(root / ".seywo/config.json")


def current(store):
    return [
        r[0]
        for r in store.db.execute(
            "SELECT safe_text FROM current_sources WHERE role='user' ORDER BY ordinal,record_no"
        )
    ]


def test_late_replay_preserves_head_payloads_and_history(project):
    root, cfg = project
    a = source(root, root / "a.jsonl", [message("first")])
    b = source(root, root / "b.jsonl", [message("first"), message("second")])
    with Store(cfg) as store:
        old = store.import_compact(a, "chat", "native")
        new = store.import_compact(b, "chat", "native")
        count = store.db.execute("SELECT count(*) FROM source_fragments").fetchone()[0]
        store.import_compact(a, "chat", "native")
        assert current(store) == ["first", "second"]
        assert (
            store.db.execute("SELECT head_snapshot FROM source_segments").fetchone()[0]
            == new["snapshot_id"]
        )
        assert (
            store.db.execute("SELECT count(*) FROM source_fragments").fetchone()[0]
            == count
        )
        event = store.db.execute(
            "SELECT event_id FROM snapshot_fragments WHERE snapshot_id=? AND role=?",
            (old["snapshot_id"], "user"),
        ).fetchone()[0]
        assert store.read(event, snapshot_id=old["snapshot_id"])["text"] == "first"


def test_rewrite_starts_new_segment_so_new_record_is_after_cursor(project):
    root, cfg = project
    a = source(root, root / "a.jsonl", [message("old " + str(i)) for i in range(5)])
    b = source(root, root / "b.jsonl", [message("new compacted turn")])
    with Store(cfg) as store:
        store.import_compact(a, "chat", "native")
        rewritten = store.import_compact(b, "chat", "native")
        tail = store.db.execute(
            "SELECT ordinal,record_no FROM current_sources WHERE snapshot_id=? ORDER BY record_no DESC LIMIT 1",
            (rewritten["snapshot_id"],),
        ).fetchone()
        assert tuple(tail) > (0, 6)
        assert current(store)[-1] == "new compacted turn"


def test_replay_across_streams_does_not_create_segments(project):
    root, cfg = project
    a = source(root, root / "a.jsonl", [message("first")])
    b = source(root, root / "b.jsonl", [message("other stream")])
    with Store(cfg) as store:
        store.import_compact(a, "chat", "stream-a")
        store.import_compact(b, "chat", "stream-b")
        store.import_compact(a, "chat", "stream-a")
        assert (
            store.db.execute("SELECT count(*) FROM source_segments").fetchone()[0] == 2
        )


def test_dry_run_is_byte_for_byte_read_only(project):
    root, cfg = project
    state = root / ".seywo"
    before = {
        str(p.relative_to(state)): p.read_bytes()
        for p in state.rglob("*")
        if p.is_file()
    }
    result = storage_admin.migrate(cfg, dry_run=True)
    after = {
        str(p.relative_to(state)): p.read_bytes()
        for p in state.rglob("*")
        if p.is_file()
    }
    assert result["state"] == "planned"
    assert after == before


def test_verify_is_read_only_and_checks_integrity(project):
    root, cfg = project
    live = root / ".seywo/inbox/live"
    live.mkdir(parents=True)
    native = root / "raw"
    native.write_bytes(b"exact source")
    archive.capture(native, live / "one.jsonl")
    orphan = live.parent / "objects" / ("a" * 64 + ".z")
    orphan.write_bytes(b"not referenced")
    before = {
        str(p): p.read_bytes() for p in (root / ".seywo").rglob("*") if p.is_file()
    }
    result = storage_admin.verify(cfg)
    after = {
        str(p): p.read_bytes() for p in (root / ".seywo").rglob("*") if p.is_file()
    }
    assert after == before
    assert result["integrity"] == "ok"
    assert result["foreign_key_violations"] == 0


@pytest.mark.parametrize("damage", ["json", "schema", "chunk", "missing"])
def test_gc_fails_closed_for_unknown_references(project, damage):
    root, cfg = project
    live = root / ".seywo/inbox/live"
    live.mkdir(parents=True)
    raw = root / "raw"
    raw.write_bytes(b"evidence" * 1000)
    path = live / "one.jsonl"
    archive.capture(raw, path)
    obj = live.parent / "objects"
    orphan = obj / ("b" * 64 + ".z")
    orphan.write_bytes(b"orphan")
    data = json.loads(path.read_text())
    if damage == "json":
        path.write_text('{"seywo_archive":1,broken')
    elif damage == "schema":
        data["chunks"] = None
        path.write_text(json.dumps(data, separators=(",", ":")))
    elif damage == "chunk":
        data["chunks"] = ["../../bad"]
        path.write_text(json.dumps(data, separators=(",", ":")))
    else:
        (obj / (data["chunks"][0] + ".z")).unlink()
    before = sorted(p.name for p in obj.glob("*.z"))
    with pytest.raises((ValueError, OSError)):
        archive.garbage_collect(live.parent)
    assert sorted(p.name for p in obj.glob("*.z")) == before


def test_collection_fails_closed_when_database_archive_is_missing(project):
    from seywo.storage_lifecycle import safe_collect
    root, cfg = project
    raw = source(root, root / "raw.jsonl", [message("registered source")])
    with Store(cfg) as store:
        store.import_compact(raw, "chat", str(raw))
        archive_path = Path(store.db.execute("SELECT path FROM source_archives LIMIT 1").fetchone()[0])
    orphan = root / ".seywo/inbox/objects" / ("c" * 64 + ".z")
    orphan.write_bytes(b"orphan")
    archive_path.unlink()
    result = safe_collect(cfg)
    assert result["state"] == "failed"
    assert result["error_code"] == "storage_maintenance_failed"
    assert orphan.exists()


def test_gc_waits_for_capture_to_publish(project, monkeypatch):
    root, cfg = project
    live = root / ".seywo/inbox/live"
    live.mkdir(parents=True)
    raw = root / "raw"
    raw.write_bytes(b"concurrent source")
    reached = threading.Event()
    release = threading.Event()
    gc_started = threading.Event()
    atomic = archive._atomic

    def paused(path, data):
        atomic(path, data)
        if path.suffix == ".z":
            reached.set()
            assert release.wait(5)

    monkeypatch.setattr(archive, "_atomic", paused)

    def collect():
        gc_started.set()
        return archive.garbage_collect(live.parent)

    with ThreadPoolExecutor(max_workers=2) as pool:
        write = pool.submit(archive.capture, raw, live / "one.jsonl")
        assert reached.wait(5)
        gc = pool.submit(collect)
        assert gc_started.wait(5)
        # The collector cannot pass the publisher before a manifest is durable.
        try:
            assert not gc.done()
        finally:
            release.set()
        write.result(timeout=5)
        assert gc.result(timeout=5)["removed_blocks"] == 0
    with archive.open_source(live / "one.jsonl") as stream:
        assert stream.read() == raw.read_bytes()


def legacy_five(root, cfg):
    """Build an actual schema-5 database, including old physical source bodies."""
    import sqlite3
    from seywo.store import APPLICATION_ID, DDL, digest, encode
    from seywo.projection import VERSION, project as project_record
    from seywo.narrative import empty
    from seywo import s2_schema, s3_schema, model_schema, turn_schema

    live = root / ".seywo/inbox/live"
    live.mkdir(parents=True)
    raw = source(root, live / "chat-old.jsonl", [message("preserved evidence")])
    Path(cfg["_database"]).unlink()
    with sqlite3.connect(cfg["_database"]) as db:
        db.executescript(DDL)
        db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        db.execute("PRAGMA user_version=1")
        db.execute(
            "INSERT INTO projects VALUES(?,?)", (cfg["project_id"], cfg["project_root"])
        )
        db.commit()
        for module in (s2_schema, s3_schema, model_schema, turn_schema):
            module.migrate(db)
        db.execute("INSERT INTO chats VALUES(?,?)", ("chat", cfg["project_id"]))
        db.execute(
            "INSERT INTO source_segments VALUES(?,?,?,?,?)",
            ("seg", "chat", str(raw), 0, "snap"),
        )
        data = raw.read_bytes()
        db.execute(
            "INSERT INTO source_snapshots VALUES(?,?,?,?,?,?)",
            ("snap", "seg", digest(data), len(data), 2, VERSION),
        )
        offset = 0
        for n, line in enumerate(data.splitlines(keepends=True), 1):
            projected = project_record(json.loads(line))
            projected.update(turn_id=None, root_turn_id=None, turn_seq=None)
            event = "event" + str(n)
            db.execute("INSERT INTO source_events VALUES(?,?,?)", (event, "seg", n))
            db.execute(
                "INSERT INTO source_fragments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    n,
                    event,
                    "snap",
                    offset,
                    offset + len(line),
                    digest(line),
                    projected["kind"],
                    projected["role"],
                    projected["channel"],
                    projected["call_id"],
                    projected["tool"],
                    projected["timestamp"],
                    projected["text"],
                    digest(projected["text"]),
                    encode(projected),
                    None,
                    None,
                    None,
                ),
            )
            db.execute(
                "INSERT INTO coverage_ranges VALUES(?,?,?)",
                (n, projected["state"], projected["reason"]),
            )
            offset += len(line)
        db.execute("INSERT INTO narrative_heads VALUES(?,?,?)", ("chat", 1, 0))
        db.execute(
            "INSERT INTO narrative_versions VALUES(?,?,?,?,?,?,?,?)",
            ("chat", 1, 0, encode(empty()), "[]", "t", "model", 1.0),
        )
    return raw


@pytest.mark.parametrize(
    "interruption", ["raw_durable", "schema_committed", "validated", "reclaimed"]
)
def test_migration_resumes_after_every_durable_boundary(
    project, monkeypatch, interruption
):
    root, cfg = project
    raw = legacy_five(root, cfg)
    baseline = storage_admin._db_digest(cfg)
    write_json = storage_admin._write_json
    rewrite = storage_admin._rewrite_paths
    fired = False

    def interrupted_write(path, data):
        nonlocal fired
        write_json(path, data)
        if (
            not fired
            and path.name == storage_admin.GUARD
            and data["state"] == interruption
        ):
            fired = True
            raise RuntimeError("power loss")

    def interrupted_rewrite(*args):
        nonlocal fired
        result = rewrite(*args)
        if not fired and interruption == "schema_committed":
            fired = True
            raise RuntimeError("power loss")
        return result

    monkeypatch.setattr(storage_admin, "_write_json", interrupted_write)
    monkeypatch.setattr(storage_admin, "_rewrite_paths", interrupted_rewrite)
    with pytest.raises(RuntimeError, match="power loss"):
        storage_admin.migrate(cfg)
    backups = list((root / ".seywo/maintenance/storage-migration").glob("*.gz"))
    assert len(backups) == 1
    backup = backups[0].read_bytes()
    with pytest.raises(ValueError, match="migration pending"):
        Store(cfg)
    result = storage_admin.migrate(cfg)
    assert result["state"] == "migrated"
    assert result["schema"] == 8
    assert backups[0].read_bytes() == backup
    assert not raw.exists()
    assert not (root / ".seywo" / storage_admin.GUARD).exists()
    assert storage_admin._db_digest(cfg)["source"] == baseline["source"]
    assert storage_admin._db_digest(cfg)["narrative"] == baseline["narrative"]
    assert storage_admin.verify(cfg)["integrity"] == "ok"
    with Store(cfg, read_only=True) as store:
        assert store.read("event2", snapshot_id="snap")["text"] == "preserved evidence"
    receipts = list((root / ".seywo/maintenance/storage-migration").glob("*.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())["state"] == "complete"


def test_backup_includes_committed_wal_and_is_immutable(tmp_path):
    import gzip
    import sqlite3

    dbpath = tmp_path / "wal.sqlite"
    with sqlite3.connect(dbpath) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("CREATE TABLE proof(value TEXT)")
        db.commit()
        db.execute("INSERT INTO proof VALUES('committed WAL')")
        db.commit()
        assert Path(str(dbpath) + "-wal").stat().st_size > 0
        target = tmp_path / "backup.gz"
        first = storage_admin._backup_database(dbpath, target)
        original = target.read_bytes()
        restored = tmp_path / "restore.sqlite"
        restored.write_bytes(gzip.decompress(original))
        with sqlite3.connect(restored) as check:
            assert (
                check.execute("SELECT value FROM proof").fetchone()[0]
                == "committed WAL"
            )
        db.execute("INSERT INTO proof VALUES('later')")
        db.commit()
        assert storage_admin._backup_database(dbpath, target) == first
        assert target.read_bytes() == original


def test_pruning_preserves_all_historical_and_pending_bindings(project, monkeypatch):
    from seywo import inbox

    root, cfg = project
    monkeypatch.setattr(inbox, "KEEP_SNAPSHOTS", 0)
    live = root / ".seywo/inbox/live"
    live.mkdir(parents=True)
    raw = source(root, root / "raw.jsonl", [message("first")])
    first = Path(inbox.snapshot(cfg, raw, "chat"))
    with Store(cfg) as store:
        old = store.import_compact(first, "chat", "native")
    source(root, raw, [message("first"), message("second")])
    second = Path(inbox.snapshot(cfg, raw, "chat"))
    with Store(cfg) as store:
        store.import_compact(second, "chat", "native")
    source(root, raw, [message("pending")])
    pending = inbox.record(cfg, "chat", "PreCompact", str(raw))
    assert inbox.prune_live(cfg, "chat", set()) == 0
    assert first.exists() and second.exists() and Path(pending["snapshot"]).exists()
    with Store(cfg, read_only=True) as store:
        event = store.db.execute(
            "SELECT event_id FROM snapshot_fragments WHERE snapshot_id=? AND role='user'",
            (old["snapshot_id"],),
        ).fetchone()[0]
        assert store.read(event, snapshot_id=old["snapshot_id"])["source"][
            "path"
        ] == str(first)


def test_delta_commit_recovers_without_repeating_model_after_cache_write_crash(
    project, monkeypatch
):
    from seywo import delta_pipeline
    from seywo.narrative import Narrative

    root, cfg = project
    raw = source(root, root / "raw.jsonl", [message("请修复存储")])
    with Store(cfg) as store:
        store.import_compact(raw, "chat", "native")
    state = {"cursor": None, "version": 0, "windows": 0}
    statepath = root / ".seywo/maintain/chat.json"
    statepath.parent.mkdir()

    class Transport:
        model = "fake"
        calls = 0

        def complete_delta(self, **kwargs):
            self.calls += 1
            return {
                "output": {
                    "items": [
                        {
                            "title": "修复存储",
                            "type": "action",
                            "attribution": "user",
                            "source_indexes": [0],
                        }
                    ],
                    "line": {"action": "continue", "title": "存储维护"},
                    "focus_item": 0,
                    "open_item_indexes": [],
                    "resolve_titles": [],
                    "no_work_source_indexes": [],
                }
            }

    transport = Transport()
    write = delta_pipeline._write_state

    def crash(path, state):
        if state["version"] == 1:
            raise RuntimeError("cache write crash")
        write(path, state)

    monkeypatch.setattr(delta_pipeline, "_write_state", crash)
    with pytest.raises(RuntimeError, match="cache write crash"):
        delta_pipeline.run_delta(cfg, "chat", state, statepath, transport, {})
    with Store(cfg, read_only=True) as store:
        assert Narrative(store)._head("chat")[0] == 1
        assert (
            store.db.execute("SELECT count(*) FROM delta_progress").fetchone()[0] == 1
        )
    monkeypatch.setattr(delta_pipeline, "_write_state", write)
    fresh = {"cursor": None, "version": 0, "windows": 0}
    report = delta_pipeline.run_delta(cfg, "chat", fresh, statepath, transport, {})
    assert report["calls"] == 0
    assert transport.calls == 1
    assert fresh["version"] == 1


def test_mutable_native_rewrite_gets_immutable_historical_captures(project):
    root, cfg = project
    raw = source(root, root / "native.jsonl", [message("before")])
    with Store(cfg) as store:
        before = store.import_compact(raw, "chat", "native")
        source(root, raw, [message("after")])
        after = store.import_compact(raw, "chat", "native")
        assert before["segment_id"] != after["segment_id"]
        for result, text in ((before, "before"), (after, "after")):
            event = store.db.execute(
                "SELECT event_id FROM snapshot_fragments WHERE snapshot_id=? AND role='user'",
                (result["snapshot_id"],),
            ).fetchone()[0]
            evidence = store.read(event, snapshot_id=result["snapshot_id"])
            assert evidence["text"] == text
            with archive.open_source(evidence["source"]["path"]) as stream:
                assert text.encode() in stream.read()


def test_old_data_cannot_upgrade_without_admin_backup(project):
    root, cfg = project
    legacy_five(root, cfg)
    before = Path(cfg["_database"]).read_bytes()
    with pytest.raises(ValueError, match="storage migrate"):
        Store(cfg)
    assert Path(cfg["_database"]).read_bytes() == before


def test_same_text_is_not_suppressed_as_an_old_occurrence(project):
    from seywo.delta_pipeline import _fresh_rows

    root, cfg = project
    first = source(root, root / "first.jsonl", [message("继续")])
    second = source(root, root / "second.jsonl", [message("继续")])
    with Store(cfg) as store:
        store.import_source(first, "chat", 0)
        store.import_source(second, "chat", 1)
        fresh = _fresh_rows(store, "chat", [0, 2])
        assert [r["safe_text"] for r in fresh if r["role"] == "user"] == ["继续"]


def test_migration_waits_for_existing_store_writer(project, monkeypatch):
    root, cfg = project
    opened = threading.Event()
    release = threading.Event()
    attempted = threading.Event()
    baseline = threading.Event()
    original = storage_admin._plan

    def plan(*args):
        baseline.set()
        return original(*args)

    monkeypatch.setattr(storage_admin, "_plan", plan)

    def writer():
        with Store(cfg):
            opened.set()
            assert release.wait(5)

    def migration():
        attempted.set()
        return storage_admin.migrate(cfg)

    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(writer)
        assert opened.wait(5)
        changing = pool.submit(migration)
        assert attempted.wait(5)
        try:
            assert not baseline.wait(0.05)
        finally:
            release.set()
        writing.result(timeout=5)
        assert changing.result(timeout=5)["state"] == "already_migrated"


def test_archive_api_rejects_replacing_a_published_locator(project):
    root, cfg = project
    live = root / ".seywo/inbox/live"
    raw = root / "raw"
    raw.write_bytes(b"old exact bytes")
    target = live / "one.jsonl"
    archive.capture(raw, target)
    manifest = target.read_bytes()
    raw.write_bytes(b"new exact bytes")
    with pytest.raises(ValueError, match="immutable archive"):
        archive.capture(raw, target)
    assert target.read_bytes() == manifest
    with archive.open_source(target) as stream:
        assert stream.read() == b"old exact bytes"


def test_current_source_locator_tracks_appended_head(project):
    root, cfg = project
    first = source(root, root / "one.jsonl", [message("first")])
    later = source(root, root / "two.jsonl", [message("first"), message("last")])
    with Store(cfg) as store:
        store.import_compact(first, "chat", "native")
        new = store.import_compact(later, "chat", "native")
        event = store.db.execute(
            "SELECT event_id FROM current_sources WHERE safe_text='last'"
        ).fetchone()[0]
        got = store.read(event)
        locator = store.db.execute(
            "SELECT path FROM source_archives WHERE snapshot_id=?",
            (new["snapshot_id"],),
        ).fetchone()[0]
        assert got["source"]["path"] == locator
        with archive.open_source(locator) as stream:
            assert stream.read() == later.read_bytes()


def test_native_turn_identity_deduplicates_copied_prefix_but_retains_new_turn(project):
    from seywo.delta_pipeline import _fresh_rows

    root, cfg = project

    def turn(identity):
        return {"type": "turn_context", "payload": {"turn_id": identity}}

    first = source(root, root / "one.jsonl", [turn("t1"), message("继续")])
    later = source(
        root,
        root / "two.jsonl",
        [turn("t1"), message("继续"), turn("t2"), message("继续")],
    )
    with Store(cfg) as store:
        store.import_source(first, "chat", 0)
        store.import_source(later, "chat", 1)
        rows = _fresh_rows(store, "chat", [0, 3])
        users = [r for r in rows if r["role"] == "user"]
        assert len(users) == 1 and users[0]["turn_id"] == "t2"


def test_schema6_cursor_is_seeded_before_removing_the_cache(project, monkeypatch):
    import sqlite3
    from seywo.narrative import Narrative, empty
    from seywo.delta_pipeline import run_delta

    root, cfg = project
    raw = source(root, root / "raw.jsonl", [message("already consumed")])
    # Build an actual pre-v8 database; lowering only user_version on a v8
    # physical layout does not rehearse a schema-6 upgrade.
    from seywo import store as store_module, storage_v8
    with monkeypatch.context() as old_runtime:
        old_runtime.setattr(store_module, "SCHEMA_VERSION", 7)
        old_runtime.setattr(storage_v8, "migrate", lambda db: None)
        def old_insert(db, values):
            names = list(values)
            return db.execute("INSERT INTO source_fragments(" + ",".join(names) + ") VALUES(" + ",".join("?" for _ in names) + ")", list(values.values())).lastrowid
        old_runtime.setattr(storage_v8, "insert_fragment", old_insert)
        Path(cfg["_database"]).unlink()
        with Store(cfg, create=True) as store:
            # Only this fixture runs the current importer against the prior
            # schema; its compatibility membership table is not part of v7.
            store.db.execute('CREATE TABLE search_documents(id INTEGER PRIMARY KEY)')
            store.import_compact(raw, "chat", "native")
            store.db.execute('DROP TABLE search_documents')
            with store.db:
                Narrative(store)._write(
                    "chat", 1, 0, empty(), "[]", origin="model_delta_v1"
                )
    folder = root / ".seywo/maintain"
    folder.mkdir()
    cache = folder / "chat.json"
    cache.write_text(json.dumps({"cursor": [0, 2], "version": 1, "windows": 1}))
    with sqlite3.connect(cfg["_database"]) as db:
        db.execute("DROP TABLE delta_progress")
        db.execute("PRAGMA user_version=6")
    result = storage_admin.migrate(cfg)
    assert result["seeded_delta_cursors"] == 1
    cache.unlink()

    class NoModel:
        def complete_delta(self, **kwargs):
            raise AssertionError("already published source must not call model")

    report = run_delta(cfg, "chat", {"cursor": None}, cache, NoModel(), {})
    assert report["calls"] == 0 and report["version"] == 1
    assert json.loads(cache.read_text())["cursor"] == [0, 2]


def test_resume_stops_if_rollback_image_is_missing(project, monkeypatch):
    root, cfg = project
    raw = legacy_five(root, cfg)
    original = storage_admin._write_json
    fired = False

    def crash(path, data):
        nonlocal fired
        original(path, data)
        if not fired and data["state"] == "raw_durable":
            fired = True
            raise RuntimeError("power loss")

    monkeypatch.setattr(storage_admin, "_write_json", crash)
    with pytest.raises(RuntimeError):
        storage_admin.migrate(cfg)
    marker = json.loads((root / ".seywo" / storage_admin.GUARD).read_text())
    Path(marker["backup"]["path"]).unlink()
    with pytest.raises(OSError):
        storage_admin.migrate(cfg)
    assert raw.exists()
    assert (root / ".seywo" / storage_admin.GUARD).exists()


def test_manual_maintainer_cannot_reclaim_an_active_worker(project):
    from seywo import maintain

    root, cfg = project
    handle = maintain._lock(cfg, "chat")
    assert handle is not None
    try:
        result = maintain.run_chat(cfg, "chat")
        assert result["state"] == "busy"
        assert result["calls"] == 0
    finally:
        handle.close()


def test_delta_planning_decodes_only_fresh_eligible_bodies(project):
    import zlib
    from seywo.delta_pipeline import _fresh_rows

    root, cfg = project
    first = source(root, root / "old.jsonl", [message("old body must stay unloaded")])
    later = source(
        root,
        root / "new.jsonl",
        [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "tool",
                    "output": "tool body must stay unloaded",
                },
            },
            message("fresh dialogue"),
        ],
    )
    with Store(cfg) as store:
        store.import_source(first, "chat", 0)
        store.import_source(later, "chat", 1)
        decoded = []

        def read_body(data):
            text = json.loads(zlib.decompress(data))["text"]
            decoded.append(text)
            assert text == "fresh dialogue"
            return text

        store.db.create_function("seywo_source_text", 1, read_body)
        rows = _fresh_rows(store, "chat", [0, 2])
        assert decoded == ["fresh dialogue"]
        assert (
            next(r for r in rows if r["role"] == "user")["safe_text"]
            == "fresh dialogue"
        )
        assert _fresh_rows(store, "chat", [1, 3]) == []
        assert decoded == ["fresh dialogue"]


def test_maintenance_upgrades_legacy_storage_before_worker_and_preserves_retry(project, monkeypatch):
    from seywo import maintain
    root, cfg = project
    legacy_five(root, cfg)
    def worker(config, chat):
        with Store(config, read_only=True) as store:
            assert store.db.execute('PRAGMA user_version').fetchone()[0] == 8
            assert current(store) == ['preserved evidence']
        return {'state': 'done', 'calls': 0}
    monkeypatch.setattr(maintain, '_run_chat_owned', worker)
    assert maintain.run_chat(cfg, 'chat')['state'] == 'done'
    assert len(list((root / '.seywo/maintenance/storage-migration').glob('*.sqlite3.gz'))) == 1
    assert maintain.run_chat(cfg, 'chat')['state'] == 'done'
    assert len(list((root / '.seywo/maintenance/storage-migration').glob('*.sqlite3.gz'))) == 1


def test_maintenance_upgrade_failure_is_visible_and_does_not_run_model(project, monkeypatch):
    from seywo import maintain, maintenance_status
    root, cfg = project
    legacy_five(root, cfg)
    def failed(*args, **kwargs):
        raise OSError('private filesystem detail')
    monkeypatch.setattr(storage_admin, 'migrate', failed)
    monkeypatch.setattr(maintain, '_run_chat_owned', lambda *args: pytest.fail('worker cannot run before upgrade'))
    report = maintain.run_chat(cfg, 'chat')
    assert report['state'] == 'blocked' and report['calls'] == 0
    assert report['error_code'] == 'storage_upgrade_failed'
    assert 'private filesystem detail' not in json.dumps(report)
    assert maintenance_status.read({**cfg, 'hook': {'enabled': True}}, 'chat')['state'] == 'blocked'
