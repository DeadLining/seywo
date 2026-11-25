"""Failure-first acceptance of storage ownership and monotonic progress."""

import json
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from seywo import archive
from seywo.storage_verify import verify
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
    result = verify(cfg)
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


def test_same_text_is_not_suppressed_as_an_old_occurrence(project):
    from seywo.compact_sources import _fresh_rows

    root, cfg = project
    first = source(root, root / "first.jsonl", [message("继续")])
    second = source(root, root / "second.jsonl", [message("继续")])
    with Store(cfg) as store:
        store.import_source(first, "chat", 0)
        store.import_source(second, "chat", 1)
        fresh = _fresh_rows(store, "chat", [0, 2])
        assert [r["safe_text"] for r in fresh if r["role"] == "user"] == ["继续"]


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
    from seywo.compact_sources import _fresh_rows

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
    from seywo.compact_sources import _fresh_rows

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
