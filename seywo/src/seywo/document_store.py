"""Immutable compressed JSON changes with bounded reconstruction checkpoints."""

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import zlib

CHECKPOINT_INTERVAL = 32


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _pack(value):
    return zlib.compress(encode(value).encode(), 3)


def _changes(old, new, path=()):
    if old == new:
        return []
    if isinstance(old, dict) and isinstance(new, dict):
        result = [["delete", list(path + (key,))] for key in old.keys() - new.keys()]
        for key, value in new.items():
            if key not in old:
                result.append(["put", list(path + (key,)), value])
            else:
                result.extend(_changes(old[key], value, path + (key,)))
        return result
    if isinstance(old, list) and isinstance(new, list) and len(new) >= len(old):
        result = []
        for i, value in enumerate(old):
            result.extend(_changes(value, new[i], path + (i,)))
        if len(new) > len(old):
            result.append(["append", list(path), new[len(old):]])
        return result
    return [["put", list(path), new]]


def _apply(doc, changes):
    doc = deepcopy(doc)
    for change in changes:
        op, path = change[:2]
        if not path:
            if op == "put":
                doc = deepcopy(change[2])
            elif op == "append":
                doc.extend(deepcopy(change[2]))
            else:
                raise ValueError("invalid document delta")
            continue
        target = doc
        for part in path[:-1]:
            target = target[part]
        key = path[-1]
        if op == "delete":
            del target[key]
        elif op == "put":
            target[key] = deepcopy(change[2])
        elif op == "append":
            target[key].extend(deepcopy(change[2]))
        else:
            raise ValueError("invalid document delta")
    return doc


def register(db):
    @lru_cache(maxsize=8)
    def body(chat, version):
        chain, current = [], version
        for _ in range(CHECKPOINT_INTERVAL):
            row = db.execute("SELECT base_version,body,body_hash FROM narrative_revisions WHERE chat_id=? AND version=?",
                             (chat, current)).fetchone()
            if row is None:
                raise ValueError("missing narrative revision")
            decoded = json.loads(zlib.decompress(row[1]))
            chain.append((row[0], decoded, row[2]))
            if row[0] is None:
                break
            if row[0] >= current:
                raise ValueError("invalid narrative revision chain")
            current = row[0]
        else:
            raise ValueError("narrative checkpoint distance exceeded")
        doc = None
        for base, data, checksum in reversed(chain):
            doc = data if base is None else _apply(doc, data)
            text = encode(doc)
            if hashlib.sha256(text.encode()).hexdigest() != checksum:
                raise ValueError("narrative checksum mismatch")
        return text

    db.create_function("seywo_document", 2, body)
    return body


def write(db, chat, version, override, doc, heads, task_id, origin, created_at):
    text = encode(doc)
    full = _pack(doc)
    base, payload = None, full
    previous = db.execute("SELECT version,depth FROM narrative_revisions WHERE chat_id=? ORDER BY version DESC LIMIT 1",
                          (chat,)).fetchone()
    depth = 0
    if previous and previous[1] < CHECKPOINT_INTERVAL - 1:
        old = json.loads(db.execute("SELECT seywo_document(?,?)", (chat, previous[0])).fetchone()[0])
        patch = _pack(_changes(old, doc))
        if len(patch) < len(full):
            base, payload, depth = previous[0], patch, previous[1] + 1
    db.execute("INSERT INTO narrative_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
               (chat, version, override, base, payload, hashlib.sha256(text.encode()).hexdigest(),
                depth, heads, task_id, origin, created_at, len(text.encode())))


def migrate(db):
    db.execute("""CREATE TABLE narrative_revisions(
        chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
        version INTEGER NOT NULL, override_revision INTEGER NOT NULL,
        base_version INTEGER, body BLOB NOT NULL, body_hash TEXT NOT NULL,
        depth INTEGER NOT NULL, source_heads TEXT NOT NULL, task_id TEXT UNIQUE,
        origin TEXT NOT NULL, created_at REAL NOT NULL, logical_bytes INTEGER NOT NULL,
        PRIMARY KEY(chat_id,version))""")
    for row in db.execute("SELECT * FROM narrative_versions ORDER BY chat_id,version"):
        write(db, row["chat_id"], row["version"], row["override_revision"], json.loads(row["body_json"]),
              row["source_heads"], row["task_id"], row["origin"], row["created_at"])
    db.execute("DROP TABLE narrative_versions")
    db.execute("""CREATE VIEW narrative_versions AS SELECT chat_id,version,override_revision,
        seywo_document(chat_id,version) AS body_json,source_heads,task_id,origin,created_at
        FROM narrative_revisions""")
