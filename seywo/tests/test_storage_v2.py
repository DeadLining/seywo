import json
from seywo.archive import capture, garbage_collect, open_source
from seywo.config import initialize, load_config
from seywo.store import Store


def source(root, path, records, chat="chat"):
    rows = [{"type": "session_meta", "payload": {"id": chat, "cwd": str(root)}}, *records]
    path.write_bytes(b"".join((json.dumps(row, ensure_ascii=False) + "\n").encode() for row in rows))
    return path


def message(text, role="user"):
    return {"type": "response_item", "payload": {"type": "message", "role": role,
            "content": [{"type": "input_text", "text": text}]}}


def test_archive_deduplicates_blocks_and_round_trips_untrusted_bytes(tmp_path):
    inbox = tmp_path / "inbox"
    live = inbox / "live"
    live.mkdir(parents=True)
    original = (b'{"type":"session_meta"}\n' + b"\xff\x00" + b"unterminated\n") * 10000
    source_path = tmp_path / "native.jsonl"
    source_path.write_bytes(original)
    first = live / "first.jsonl"
    second = live / "second.jsonl"
    capture(source_path, first)
    capture(source_path, second)
    assert open_source(first).read() == original
    assert open_source(second).read() == original
    blocks = list((inbox / "objects").glob("*.z"))
    assert blocks
    first.unlink()
    assert garbage_collect(inbox)["removed_blocks"] == 0
    second.unlink()
    assert garbage_collect(inbox)["removed_blocks"] == len(blocks)


def test_compact_stream_reuses_occurrences_and_noop_is_zero_growth(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    initialize(root)
    config = load_config(root / ".seywo/config.json")
    first = source(root, root / "first.jsonl", [message("same"), message("same")])
    second = source(root, root / "second.jsonl", [message("same"), message("same"), message("new")])
    with Store(config) as store:
        initial = store.import_compact(first, "chat", str(root / "transcript.jsonl"))
        fragments = store.db.execute("SELECT count(*) FROM source_fragments").fetchone()[0]
        payloads = store.db.execute("SELECT count(*) FROM source_payloads").fetchone()[0]
        fts = store.db.execute("SELECT count(*) FROM source_fts").fetchone()[0]
        appended = store.import_compact(second, "chat", str(root / "transcript.jsonl"))
        after_append = {
            "fragments": store.db.execute("SELECT count(*) FROM source_fragments").fetchone()[0],
            "payloads": store.db.execute("SELECT count(*) FROM source_payloads").fetchone()[0],
            "fts": store.db.execute("SELECT count(*) FROM source_fts").fetchone()[0],
        }
        replay = store.import_compact(second, "chat", str(root / "transcript.jsonl"))
        after_replay = {
            "fragments": store.db.execute("SELECT count(*) FROM source_fragments").fetchone()[0],
            "payloads": store.db.execute("SELECT count(*) FROM source_payloads").fetchone()[0],
            "fts": store.db.execute("SELECT count(*) FROM source_fts").fetchone()[0],
        }
        current = store.list_events("chat", limit=20)["events"]
    assert initial["inserted_fragments"] == 3
    assert appended["inserted_fragments"] == 1
    assert after_append["fragments"] == fragments + 1
    assert after_append["payloads"] == payloads + 1
    assert after_append["fts"] == fts + 1
    assert replay["reused_snapshot"] is True
    assert after_replay == after_append
    assert len(current) == 4
    assert sum(1 for item in current if item["kind"] == "message") == 3


def test_same_text_in_distinct_occurrences_keeps_two_evidence_rows(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    initialize(root)
    config = load_config(root / ".seywo/config.json")
    path = source(root, root / "source.jsonl", [message("repeat"), message("repeat")])
    with Store(config) as store:
        store.import_source(path, "chat", 0)
        rows = store.db.execute(
            "SELECT row_id,payload_id FROM current_sources WHERE chat_id=? AND safe_text=? ORDER BY row_id",
            ("chat", "repeat")).fetchall()
        assert len(rows) == 2
        assert rows[0]["row_id"] != rows[1]["row_id"]
        assert rows[0]["payload_id"] == rows[1]["payload_id"]


def test_storage_verify_does_not_leave_a_migration_guard(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    initialize(root)
    from seywo.storage_admin import run
    config_path = root / ".seywo/config.json"
    result = run(config_path, "verify")
    assert result["state"] == "verified"
    assert not (root / ".seywo/storage-migration.pending").exists()
