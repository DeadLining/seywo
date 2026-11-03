"""Original-record locations. Reads verify bytes; missing sources never become evidence.

New imports store byte locators without copying event JSON. Existing entries stay
searchable; explicit verified maintenance removes legacy raw event duplicates.
"""

import json
from pathlib import Path
from .core import digest

SCHEMA = """
CREATE TABLE IF NOT EXISTS sg_source_locations(
 entry_id TEXT PRIMARY KEY REFERENCES entries(id),channel TEXT NOT NULL,
 tool_name TEXT NOT NULL,byte_offset INTEGER,byte_length INTEGER,
 record_hash TEXT NOT NULL,text_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sg_compact_events(segment TEXT NOT NULL,line INTEGER NOT NULL,event_hash TEXT NOT NULL,PRIMARY KEY(segment,line));
"""


def setup(store):
    store.db.executescript(SCHEMA)
    # Text length is a small derived index; coverage reads must not load every
    # transcript body. Built-in triggers also cover older MCP writers.
    store.db.executescript("""
      CREATE TABLE IF NOT EXISTS sg_entry_sizes(entry_id TEXT PRIMARY KEY REFERENCES entries(id),
        chars INTEGER NOT NULL) WITHOUT ROWID;
      CREATE TRIGGER IF NOT EXISTS sg_size_insert AFTER INSERT ON entries BEGIN
        INSERT INTO sg_entry_sizes VALUES(new.id,length(new.text)); END;
      CREATE TRIGGER IF NOT EXISTS sg_size_update AFTER UPDATE OF text ON entries BEGIN
        INSERT INTO sg_entry_sizes VALUES(new.id,length(new.text))
        ON CONFLICT(entry_id) DO UPDATE SET chars=excluded.chars; END;
      CREATE TRIGGER IF NOT EXISTS sg_size_delete BEFORE DELETE ON entries BEGIN
        DELETE FROM sg_entry_sizes WHERE entry_id=old.id; END;
    """)
    if not store.db.execute("SELECT 1 FROM metadata WHERE key='source_sizes_backfilled_v1'").fetchone():
        with store.db:
            store.db.execute("""INSERT INTO sg_entry_sizes SELECT id,length(text) FROM entries e
                WHERE NOT EXISTS (SELECT 1 FROM sg_entry_sizes z WHERE z.entry_id=e.id)""")
            store.db.execute("INSERT INTO metadata VALUES('source_sizes_backfilled_v1','1')")


def record(store, eid, payload, raw, text, offset=None, length=None):
    store.db.execute(
        "INSERT OR IGNORE INTO sg_source_locations VALUES(?,?,?,?,?,?,?)",
        (
            eid,
            payload.get("channel", ""),
            payload.get("name", ""),
            offset,
            length,
            digest(raw),
            digest(text),
        ),
    )


def metadata(store, row):
    saved = store.db.execute(
        "SELECT * FROM sg_source_locations WHERE entry_id=?", (row["id"],)
    ).fetchone()
    if saved:
        return dict(saved)
    # Original indexed JSON, not an inferred assistant role. Do not scan whole files.
    from .legacy_storage import read_raw
    raw = read_raw(store, row['session_id'], row['line'])
    if raw is not None:
        event = json.loads(raw)
        payload = event.get("payload", {})
        record(store, row["id"], payload, raw.encode(), row["text"])
        return dict(
            store.db.execute(
                "SELECT * FROM sg_source_locations WHERE entry_id=?", (row["id"],)
            ).fetchone()
        )
    # A damaged/missing locator must not turn analysis text into a final message.
    # Recover once from a verified original; normal indexed reads never scan files.
    from .ingestion import MAX_RECORD_BYTES
    source = store.db.execute("SELECT path FROM sessions WHERE id=?", (row["session_id"],)).fetchone()
    try:
        offset = 0
        with Path(source[0]).open("rb") as stream:
            for line in range(1, row["line"] + 1):
                data = stream.readline(MAX_RECORD_BYTES + 1)
                if not data.endswith(b"\n") or len(data) > MAX_RECORD_BYTES:
                    raise ValueError("graph_source_metadata_missing")
                if line < row["line"]: offset += len(data)
        raw_record = data[:-1].removesuffix(b"\r")
        expected = "e_" + digest(f"{row['session_id']}:{row['line']}:{digest(raw_record)}")[:32]
        if expected != row["id"]: raise ValueError("graph_source_metadata_changed")
        payload = json.loads(raw_record)["payload"]
        record(store, row["id"], payload, raw_record, row["text"], offset, len(data))
        return dict(store.db.execute("SELECT * FROM sg_source_locations WHERE entry_id=?", (row["id"],)).fetchone())
    except (OSError, KeyError, TypeError) as exc:
        raise ValueError("graph_source_metadata_missing") from exc


def read_original(store, entry_id, *, prefer_retained=True):
    """Read one record by byte location. Fallback scans only a legacy source on demand."""
    from .core import normalize
    from .source_retention import read
    retained = read(store, entry_id) if prefer_retained else None
    if retained:
        return {k: v for k, v in retained.items() if k != 'record'}

    row = store.db.execute(
        "SELECT e.*,s.path FROM entries e JOIN sessions s ON s.id=e.session_id WHERE e.id=?",
        (entry_id,),
    ).fetchone()
    if not row:
        raise ValueError("unknown source entry")
    exists = store.db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='sg_source_locations'"
    ).fetchone()
    loc = (
        store.db.execute(
            "SELECT * FROM sg_source_locations WHERE entry_id=?", (entry_id,)
        ).fetchone()
        if exists
        else None
    )
    from .ingestion import MAX_RECORD_BYTES

    try:
        with Path(row["path"]).open("rb") as stream:
            if loc and loc["byte_offset"] is not None:
                if (type(loc['byte_offset']) is not int or loc['byte_offset'] < 0
                        or type(loc['byte_length']) is not int
                        or not 0 < loc["byte_length"] <= MAX_RECORD_BYTES):
                    return {"status": "invalid_locator"}
                stream.seek(loc["byte_offset"])
                raw = stream.read(loc["byte_length"])
                if len(raw) != loc['byte_length'] or not raw.endswith(b'\n'):
                    return {'status': 'changed', 'entry_id': entry_id}
            else:
                raw = b""
                for _ in range(row["line"]):
                    raw = stream.readline(MAX_RECORD_BYTES + 1)
                    if len(raw) > MAX_RECORD_BYTES:
                        return {"status": "source_record_too_large"}
                    if not raw:
                        break
    except FileNotFoundError:
        return {"status": "missing", "entry_id": entry_id}
    except OSError:
        return {"status": "unavailable", "entry_id": entry_id}
    try:
        record_bytes = raw.rstrip(b"\r\n")
        event = json.loads(record_bytes)
        expected = loc["record_hash"] if loc else None
        if expected and digest(record_bytes) != expected:
            return {"status": "changed", "entry_id": entry_id}
        normalized = normalize(event["payload"])
        from .source_text import source_hash
        if (not normalized or event.get('type') != 'response_item'
                or normalized[:2] != (row['kind'], row['role'])
                or normalized[3] != row['call_id']
                or str(event.get('timestamp', '')) != row['timestamp']
                or digest(normalized[2]) != source_hash(store, entry_id, row['text'])):
            return {"status": "changed", "entry_id": entry_id}
        if (
            not expected
            and "e_" + digest(f"{row['session_id']}:{row['line']}:{digest(record_bytes)}")[:32]
            != entry_id
        ):
            return {"status": "changed", "entry_id": entry_id}
        return {
            "status": "verified",
            "entry_id": entry_id,
            "text": normalized[2],
            "line": row["line"],
        }
    except (ValueError, KeyError, TypeError):
        return {"status": "changed", "entry_id": entry_id}


def describe(store, entry_id, verify=False):
    table = store.db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='sg_source_locations'"
    ).fetchone()
    row = store.db.execute(
        "SELECT e.line,s.path FROM entries e JOIN sessions s ON s.id=e.session_id WHERE e.id=?",
        (entry_id,),
    ).fetchone()
    if not row:
        return None
    loc = (
        store.db.execute(
            "SELECT * FROM sg_source_locations WHERE entry_id=?", (entry_id,)
        ).fetchone()
        if table
        else None
    )
    result = {
        "path": row["path"],
        "line": row["line"],
        "storage": "indexed_snapshot",
        "status": "indexed_unverified",
        "byte_offset": loc["byte_offset"] if loc else None,
        "byte_length": loc["byte_length"] if loc else None,
    }
    from .source_text import external
    if external(store, entry_id):
        result['storage'] = 'codex_source'
    if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='sg_retained_sources'").fetchone():
        retained = store.db.execute('SELECT policy,record_hash,snapshot_hash,redactions_json '
                                    'FROM sg_retained_sources WHERE entry_id=?', (entry_id,)).fetchone()
        if retained:
            result.update(storage='retained_snapshot', status='retained_unverified',
                          policy=retained['policy'], record_hash=retained['record_hash'],
                          snapshot_hash=retained['snapshot_hash'],
                          redactions=json.loads(retained['redactions_json']))
            if verify:
                result['status'] = read_original(store, entry_id)['status']
            return result
    # New locations support an O(record-size) verification. Old indexes stay explicitly
    # unverified until an explicit read_original; no O(file-size) work on normal recall.
    if verify and loc and loc["byte_offset"] is not None:
        result["status"] = read_original(store, entry_id)["status"]
    return result
