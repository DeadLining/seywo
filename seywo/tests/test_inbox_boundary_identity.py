"""Boundary delivery preserves every immutable source until it is consumed."""

import json
import os
from pathlib import Path

import pytest

from seywo import inbox
from seywo.archive import open_source
from seywo.config import initialize, load_config
from seywo.store import Store, digest


@pytest.fixture
def boundary(tmp_path):
    initialized = initialize(tmp_path / "project")
    config = load_config(Path(initialized["config"]))
    source = tmp_path / "live.jsonl"
    write_source(source, "A")
    return config, source


def write_source(path, text):
    records = [
        {"type": "session_meta", "payload": {"id": "chat"}},
        {"type": "turn_context", "payload": {"turn_id": f"turn-{text}"}},
        {"type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": text}],
        }},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in records))


def records(config):
    return [json.loads(path.read_text())
            for path in (inbox.root(config) / "pending").glob("*.json")]


def capture_bytes(item):
    with open_source(Path(item["snapshot"])) as handle:
        return handle.read()


def test_failed_source_records_are_bounded(boundary, monkeypatch):
    config, source = boundary
    monkeypatch.setattr(inbox, "KEEP_RECORDS", 3)
    for index in range(6):
        path = source.parent / f"failed-{index}.jsonl"
        write_source(path, str(index))
        item = inbox.record(config, "chat", "PreCompact", str(path))
        inbox.finish(config, inbox.claim(config, item), state="failed", error="source_missing")
    records = list((inbox.root(config) / "pending").glob("*.json"))
    assert len(records) <= 3
    assert len(inbox.failed(config, "chat")) <= 3


def test_snapshot_limit_is_enforced_and_failure_is_durable(boundary, monkeypatch):
    config, source = boundary
    monkeypatch.setattr(inbox, "SNAPSHOT_LIMIT", 1)
    write_source(source, "more than one byte")
    item = inbox.record(config, "chat", "PreCompact", str(source))
    saved = inbox._read(Path(item["_path"]))
    assert saved["snapshot"] is None
    assert saved["snapshot_error"] == "snapshot_unavailable"
    assert saved["state"] == "pending"


@pytest.mark.parametrize("event", ["PreCompact", "PostCompact"])
def test_changed_pending_boundary_keeps_the_original_record(boundary, event):
    config, source = boundary
    first_bytes = source.read_bytes()
    first = inbox.record(config, "chat", event, str(source))
    record_path = Path(first["_path"])
    original_record = record_path.read_bytes()

    write_source(source, "second boundary")
    second = inbox.record(config, "chat", event, str(source))

    assert second["id"] != first["id"]
    assert record_path.read_bytes() == original_record
    assert capture_bytes(first) == first_bytes
    assert capture_bytes(second) == source.read_bytes()
    assert [item["id"] for item in inbox.pending(config, "chat")] == [first["id"], second["id"]]


def test_changed_claimed_boundary_cannot_be_overwritten_by_finish(boundary):
    config, source = boundary
    first = inbox.claim(config, inbox.record(config, "chat", "PreCompact", str(source)))
    original_record = Path(first["_path"]).read_bytes()
    write_source(source, "second boundary")
    second = inbox.record(config, "chat", "PreCompact", str(source))
    assert Path(first["_path"]).read_bytes() == original_record

    inbox.finish(config, first, state="done")

    assert second["id"] != first["id"]
    assert [item["id"] for item in inbox.pending(config, "chat")] == [second["id"]]
    assert capture_bytes(second) == source.read_bytes()


def test_duplicate_pending_delivery_preserves_attempts_and_order(boundary):
    config, source = boundary
    first = inbox.record(config, "chat", "PreCompact", str(source))
    first = inbox.claim(config, first)
    inbox.finish(config, first, state="failed", error="fixture_error")
    assert inbox.requeue_failed(config, "chat") == 1
    record_path = Path(first["_path"])
    original_record = record_path.read_bytes()

    duplicate = inbox.record(config, "chat", "PreCompact", str(source))

    assert duplicate["coalesced"] is True
    assert duplicate["id"] == first["id"]
    assert duplicate["attempts"] == 1
    assert record_path.read_bytes() == original_record
    assert len(records(config)) == 1


def test_duplicate_delivery_while_claimed_has_only_one_pending_redelivery(boundary):
    config, source = boundary
    first = inbox.claim(config, inbox.record(config, "chat", "PostCompact", str(source)))
    original_record = Path(first["_path"]).read_bytes()
    redelivery = inbox.record(config, "chat", "PostCompact", str(source))
    duplicate = inbox.record(config, "chat", "PostCompact", str(source))

    assert duplicate["coalesced"] is True
    assert duplicate["id"] == redelivery["id"] != first["id"]
    assert len(records(config)) == 2
    assert Path(first["_path"]).read_bytes() == original_record
    inbox.finish(config, first, state="done")
    assert [item["id"] for item in inbox.pending(config, "chat")] == [redelivery["id"]]


def test_identical_capture_with_changed_fingerprint_is_a_distinct_boundary(boundary):
    config, source = boundary
    first = inbox.record(config, "chat", "PreCompact", str(source))
    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    second = inbox.record(config, "chat", "PreCompact", str(source))

    assert first["fingerprint"] != second["fingerprint"]
    assert first["snapshot"] == second["snapshot"]
    assert first["id"] != second["id"]
    assert len(inbox.pending(config, "chat")) == 2


def test_rewritten_bytes_with_restored_metadata_are_not_deduplicated(boundary):
    config, source = boundary
    first = inbox.record(config, "chat", "PreCompact", str(source))
    stat = source.stat()
    original_record = Path(first["_path"]).read_bytes()
    write_source(source, "B")
    assert source.stat().st_size == stat.st_size
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    second = inbox.record(config, "chat", "PreCompact", str(source))

    assert first["fingerprint"] == second["fingerprint"]
    assert first["snapshot"] != second["snapshot"]
    assert first["id"] != second["id"]
    assert Path(first["_path"]).read_bytes() == original_record
    assert len(inbox.pending(config, "chat")) == 2


def test_pre_and_post_compact_remain_distinct_on_the_same_capture(boundary):
    config, source = boundary
    first = inbox.record(config, "chat", "PreCompact", str(source))
    second = inbox.record(config, "chat", "PostCompact", str(source))
    assert first["snapshot"] == second["snapshot"]
    assert first["id"] != second["id"]
    assert len(inbox.pending(config, "chat")) == 2


def test_two_queued_compactions_import_all_original_segments(boundary):
    config, source = boundary
    inbox.record(config, "chat", "PreCompact", str(source))
    write_source(source, "B")
    inbox.record(config, "chat", "PostCompact", str(source))
    inbox.record(config, "chat", "PreCompact", str(source))
    write_source(source, "C")
    inbox.record(config, "chat", "PostCompact", str(source))
    queued = inbox.pending(config, "chat")
    assert len(queued) == 4

    from seywo.archive import content_capture
    with Store(config) as store:
        for item in queued:
            claimed = inbox.claim(config, item)
            path, manifest = content_capture(Path(claimed["snapshot"]), inbox.snapshot_dir(config), "chat")
            store._import_compact_capture(path, manifest, "chat", claimed["transcript"])
            inbox.finish(config, claimed, state="done")
        texts = [row[0] for row in store.db.execute(
            "SELECT safe_text FROM current_sources WHERE role='user' ORDER BY ordinal,record_no")]
    assert texts == ["A", "B", "C"]
    assert inbox.pending(config, "chat") == []


def test_redelivering_failed_boundary_keeps_existing_retry_semantics(boundary):
    config, source = boundary
    first = inbox.claim(config, inbox.record(config, "chat", "PreCompact", str(source)))
    inbox.finish(config, first, state="failed", error="fixture_error")

    retry = inbox.record(config, "chat", "PreCompact", str(source))

    assert retry["id"] == first["id"]
    assert retry["state"] == "pending"
    assert retry["attempts"] == 0
    assert retry["error_code"] is None
    assert len(records(config)) == 1


def test_new_boundary_preserves_failed_record_and_its_retry_limit(boundary):
    config, source = boundary
    first = inbox.record(config, "chat", "PreCompact", str(source))
    for attempt in range(5):
        claimed = inbox.claim(config, inbox.pending(config, "chat")[0])
        inbox.finish(config, claimed, state="failed", error="fixture_error")
        assert inbox.requeue_failed(config, "chat") == (1 if attempt < 4 else 0)
    exhausted = Path(first["_path"]).read_bytes()

    write_source(source, "new boundary")
    second = inbox.record(config, "chat", "PreCompact", str(source))

    assert second["id"] != first["id"]
    assert Path(first["_path"]).read_bytes() == exhausted
    assert inbox.requeue_failed(config, "chat") == 0
    assert len(inbox.failed(config, "chat")) == 1
    inbox.retry(config, inbox.failed(config, "chat")[0])
    assert len(inbox.pending(config, "chat")) == 2
    assert json.loads(Path(first["_path"]).read_text())["attempts"] == 0


@pytest.mark.parametrize("state", ["pending", "claimed", "failed"])
def test_legacy_path_records_are_preserved_and_can_be_redelivered(boundary, state):
    config, source = boundary
    first = inbox.record(config, "chat", "PreCompact", str(source))
    if state != "pending":
        first = inbox.claim(config, first)
    if state == "failed":
        inbox.finish(config, first, state="failed", error="fixture_error")
    legacy_path = inbox.root(config) / "pending" / (digest(f"chat\0PreCompact\0{source}")[:32] + ".json")
    item = json.loads(Path(first["_path"]).read_text())
    item.pop("boundary_id", None)
    item["id"] = legacy_path.stem
    Path(first["_path"]).unlink()
    inbox._write_atomic(legacy_path, item)
    original_record = legacy_path.read_bytes()

    duplicate = inbox.record(config, "chat", "PreCompact", str(source))
    if state == "pending":
        assert duplicate["coalesced"] is True
        assert legacy_path.read_bytes() == original_record
    elif state == "claimed":
        assert duplicate["id"] != item["id"]
        assert legacy_path.read_bytes() == original_record
    else:
        assert duplicate["id"] == item["id"]
        assert duplicate["state"] == "pending" and duplicate["attempts"] == 0

    write_source(source, "next legacy boundary")
    newer = inbox.record(config, "chat", "PreCompact", str(source))
    assert newer["id"] != duplicate["id"]
    assert {row["id"] for row in records(config)} >= {duplicate["id"], newer["id"]}


def test_concurrent_duplicates_leave_one_pending_redelivery(boundary):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    config, source = boundary

    def deliveries():
        barrier = Barrier(4)
        def deliver(_):
            barrier.wait(timeout=10)
            return inbox.record(config, "chat", "PreCompact", str(source))
        with ThreadPoolExecutor(max_workers=4) as workers:
            return list(workers.map(deliver, range(4)))

    initial = deliveries()
    assert len({item["id"] for item in initial}) == 1
    claimed = inbox.claim(config, initial[0])
    claimed_bytes = Path(claimed["_path"]).read_bytes()
    repeated = deliveries()

    assert len({item["id"] for item in repeated}) == 1
    assert repeated[0]["id"] != claimed["id"]
    assert Path(claimed["_path"]).read_bytes() == claimed_bytes
    assert len(records(config)) == 2
    assert len(inbox.pending(config, "chat")) == 1
