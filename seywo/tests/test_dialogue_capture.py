"""Automatic captures are evidence records, not raw rollout backups."""

import hashlib
import json
from pathlib import Path

from seywo import inbox
from seywo.archive import open_source
from seywo.config import initialize, load_config
from seywo.projection import COMPACT_HANDOFF_PREFIX
from seywo.store import Store


def wire(event, payload):
    return json.dumps({"payload": payload, "type": event}, ensure_ascii=False).encode() + b"\n"


def message(role, text):
    return wire("response_item", {"type": "message", "role": role,
                                   "content": [{"type": "input_text", "text": text}]})


def test_oversized_media_does_not_discard_neighbouring_text_or_change_coordinates(tmp_path):
    config = load_config(Path(initialize(tmp_path / "project")["config"]))
    source = tmp_path / "rollout.jsonl"
    meta = wire("session_meta", {"id": "chat"})
    start = wire("event_msg", {"type": "task_started", "turn_id": "turn"})
    media = "A" * (17 * 1024 * 1024)
    mixed = wire("response_item", {"content": [
        {"image_url": "data:image/png;base64," + media, "type": "input_image"},
        {"type": "input_text", "text": '保留这句，含引号 " 和换行\n'},
    ], "role": "user", "type": "message"})
    mirror = message("assistant", "derived summary")
    copied = wire("compacted", {"replacement_history": [{"content": media}],
                                "message": COMPACT_HANDOFF_PREFIX + "derived summary"})
    finish = wire("event_msg", {"type": "task_complete", "turn_id": "turn", "last_agent_message": media})
    source.write_bytes(meta + start + mixed + mirror + copied + finish)
    item = inbox.record(config, "chat", "PreCompact", str(source))
    assert item["snapshot"]
    with open_source(item["snapshot"]) as archive:
        contents = archive.read(4096)
        assert archive.read(1) == b""
    assert b"base64" not in contents and b"replacement_history" not in contents
    assert b"derived summary" in contents  # The assistant record is preserved for mirror checking.
    with Store(config) as store:
        imported = store.import_compact(item["snapshot"], "chat", str(source), dialogue=True)
        user = store.db.execute("SELECT event_id FROM current_sources WHERE role='user'").fetchone()[0]
        evidence = store.read(user, snapshot_id=imported["snapshot_id"])
        assert evidence["text"] == '保留这句，含引号 " 和换行\n'
        assert evidence["turn_id"] == "turn"
        assert evidence["source"]["byte_space"] == "original_transcript"
        assert evidence["source"]["record_no"] == 3
        assert evidence["source"]["byte_start"] == len(meta + start)
        assert evidence["source"]["byte_end"] == len(meta + start) + len(mixed)
        assert evidence["source"]["raw_sha256"] == hashlib.sha256(mixed).hexdigest()
        assert evidence["projection"]["text_fields"][0]["json_path"] == "payload.content[1].text"
        assert store.db.execute("SELECT reason FROM current_sources WHERE role='assistant'").fetchone()[0] == "compact_summary_mirror"
        assert store.db.execute("SELECT json_extract(projection_json, '$.turn_event') FROM current_sources WHERE record_no=6").fetchone()[0] == "task_complete"


def test_existing_raw_prefix_keeps_evidence_and_is_not_reimported(tmp_path):
    config = load_config(Path(initialize(tmp_path / "project")["config"]))
    source = tmp_path / "rollout.jsonl"
    first = wire("session_meta", {"id": "chat"}) + message("user", "original request") + wire(
        "response_item", {"type": "function_call_output", "output": "previously indexed tool evidence"})
    source.write_bytes(first)
    with Store(config) as store:
        previous = store.import_compact(source, "chat", str(source))
        old_rows = [tuple(r) for r in store.db.execute("SELECT event_id,row_id,safe_text FROM current_sources ORDER BY record_no")]
        replay = store.import_compact(source, "chat", str(source), dialogue=True)
        assert replay["snapshot_id"] == previous["snapshot_id"]
        assert replay["inserted_fragments"] == 0

        source.write_bytes(first + message("user", "new request") + wire(
            "response_item", {"type": "function_call_output", "output": "new excluded tool evidence"}))
        added = store.import_compact(source, "chat", str(source), dialogue=True)
        assert added["segment_id"] == previous["segment_id"]
        assert added["inserted_fragments"] == 2
        current = [tuple(r) for r in store.db.execute("SELECT event_id,row_id,safe_text FROM current_sources ORDER BY record_no")]
        assert current[:3] == old_rows
        assert store.read(old_rows[-1][0], snapshot_id=previous["snapshot_id"])["text"] == old_rows[-1][-1]
        assert [r[2] for r in current[3:]] == ["new request", ""]
        assert store.import_compact(source, "chat", str(source), dialogue=True)["snapshot_id"] == added["snapshot_id"]
        assert store.db.execute("SELECT count(*) FROM source_snapshots").fetchone()[0] == 2
