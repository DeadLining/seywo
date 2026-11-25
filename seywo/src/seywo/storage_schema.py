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
    from .storage_v8 import register as register_v8
    register_v8(db)


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
