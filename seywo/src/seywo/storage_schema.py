"""Schema 6: shared source payloads, sparse snapshot memberships and FTS."""

from functools import lru_cache
import hashlib
import json
import zlib

from . import document_store


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def register(db):
    @lru_cache(maxsize=128)
    def unpack(data):
        return json.loads(zlib.decompress(data))

    def projection(data, turn, root, seq):
        value = dict(unpack(data)["projection"])
        # Preserve exactly which native-turn fields existed before migration.
        for key, item in (("turn_id", turn), ("root_turn_id", root), ("turn_seq", seq)):
            if key in unpack(data)["turn_fields"]:
                value[key] = item
        return encode(value)

    db.create_function("seywo_source_text", 1, lambda data: unpack(data)["text"])
    db.create_function("seywo_source_projection", 4, projection)
    document_store.register(db)


def payload(db, text, projection):
    projection = dict(projection)
    fields = [key for key in ("turn_id", "root_turn_id", "turn_seq") if key in projection]
    for key in fields:
        projection.pop(key)
    data = encode({"text": text, "projection": projection, "turn_fields": fields}).encode()
    key = hashlib.sha256(data).hexdigest()
    row = db.execute("SELECT id FROM source_payloads WHERE hash=?", (key,)).fetchone()
    if row:
        return row[0]
    return db.execute("INSERT INTO source_payloads(hash,data) VALUES(?,?)", (key, zlib.compress(data, 3))).lastrowid


def membership(db, snapshot_id, row_ids):
    ranges, start, end = [], None, None
    for row_id in row_ids:
        if end is not None and row_id == end + 1:
            end = row_id
        else:
            if start is not None:
                ranges.append((snapshot_id, start, end))
            start = end = row_id
    if start is not None:
        ranges.append((snapshot_id, start, end))
    db.executemany("INSERT INTO snapshot_members VALUES(?,?,?)", ranges)


def migrate(db):
    if db.execute("PRAGMA user_version").fetchone()[0] != 5:
        return
    try:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("PRAGMA user_version").fetchone()[0] != 5:
            db.rollback()
            return
        db.execute("CREATE TABLE source_payloads(id INTEGER PRIMARY KEY,hash TEXT NOT NULL UNIQUE,data BLOB NOT NULL)")
        db.execute("ALTER TABLE source_fragments ADD COLUMN payload_id INTEGER REFERENCES source_payloads(id)")
        db.execute("CREATE TABLE compact_streams(chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,transcript TEXT NOT NULL,segment_id TEXT NOT NULL REFERENCES source_segments(id) ON DELETE CASCADE,PRIMARY KEY(chat_id,transcript))")
        db.execute("CREATE TABLE source_archives(snapshot_id TEXT PRIMARY KEY REFERENCES source_snapshots(id) ON DELETE CASCADE,path TEXT NOT NULL)")
        db.execute("""CREATE TABLE snapshot_members(
            snapshot_id TEXT NOT NULL REFERENCES source_snapshots(id) ON DELETE CASCADE,
            first_row INTEGER NOT NULL,last_row INTEGER NOT NULL,
            CHECK(first_row<=last_row),PRIMARY KEY(snapshot_id,first_row))""")
        for row in db.execute("SELECT row_id,safe_text,projection_json FROM source_fragments ORDER BY row_id"):
            identity = payload(db, row["safe_text"], json.loads(row["projection_json"]))
            db.execute("UPDATE source_fragments SET payload_id=?,safe_text='',projection_json='{}' WHERE row_id=?",
                       (identity, row["row_id"]))
        for snapshot in db.execute("SELECT id FROM source_snapshots"):
            membership(db, snapshot[0], (r[0] for r in db.execute(
                "SELECT row_id FROM source_fragments WHERE snapshot_id=? ORDER BY row_id", (snapshot[0],))))
        columns = [r[1] for r in db.execute("PRAGMA table_info(source_fragments)")]
        projection = []
        for name in columns:
            if name == "safe_text":
                projection.append("seywo_source_text(p.data) AS safe_text")
            elif name == "projection_json":
                projection.append("seywo_source_projection(p.data,f.turn_id,f.root_turn_id,f.turn_seq) AS projection_json")
            else:
                projection.append("f." + name)
        db.execute("CREATE VIEW stored_fragments AS SELECT " + ",".join(projection) +
                   " FROM source_fragments f JOIN source_payloads p ON p.id=f.payload_id")
        projection = ["m.snapshot_id AS snapshot_id" if name == "snapshot_id" else "f." + name for name in columns]
        db.execute("CREATE VIEW snapshot_fragments AS SELECT " + ",".join(projection) +
                   " FROM snapshot_members m JOIN stored_fragments f ON f.row_id BETWEEN m.first_row AND m.last_row")
        db.execute("DROP VIEW current_sources")
        db.execute("""CREATE VIEW current_sources AS
            SELECT f.*,e.record_no,s.chat_id,s.ordinal,s.path,s.id AS segment_id,c.state,c.reason
            FROM snapshot_fragments f JOIN source_events e ON e.id=f.event_id
            JOIN source_segments s ON s.id=e.segment_id AND s.head_snapshot=f.snapshot_id
            JOIN coverage_ranges c ON c.fragment_id=f.row_id""")
        db.execute("CREATE INDEX fragments_payload ON source_fragments(payload_id)")
        document_store.migrate(db)
        db.execute("DELETE FROM source_fts")
        from .projection import search_tokens
        for r in db.execute("SELECT DISTINCT payload_id,safe_text FROM current_sources WHERE safe_text<>'' AND state<>'excluded_mechanical'"):
            db.execute("INSERT INTO source_fts(rowid,tokens) VALUES(?,?)", (r[0], search_tokens(r[1])))
        db.execute("PRAGMA user_version=6")
        db.commit()
    except BaseException:
        db.rollback()
        raise
