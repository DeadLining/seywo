"""Schema 8: stable occurrence identities, shared descriptors and compact FTS."""
from functools import lru_cache
import hashlib
import json
import zlib

from . import execution_documents
from .projection import search_tokens

DETAILS = ('raw_sha256', 'kind', 'role', 'channel', 'tool', 'occurred_at',
           'safe_text', 'safe_sha256', 'projection_json', 'turn_id', 'root_turn_id')


def descriptor(db, values):
    body = json.dumps(values, ensure_ascii=False, separators=(',', ':')).encode()
    identity = hashlib.sha256(body).hexdigest()
    row = db.execute('SELECT id FROM fragment_descriptors WHERE hash=?', (identity,)).fetchone()
    if row:
        return row[0]
    return db.execute('INSERT INTO fragment_descriptors(hash,data) VALUES(?,?)',
                      (identity, zlib.compress(body, 3))).lastrowid


def register(db):
    @lru_cache(maxsize=256)
    def unpack(data):
        return json.loads(zlib.decompress(data))
    db.create_function('seywo_fragment_field', 2, lambda data, i: unpack(data)[i])
    db.create_function('seywo_descriptor_put', len(DETAILS), lambda *v: descriptor(db, v))
    db.create_function('seywo_search_tokens', 1, search_tokens)
    execution_documents.register(db)


def insert_fragment(db, values):
    keys = [k for k in values if k not in DETAILS]
    columns = keys + ['descriptor_id']
    params = [values[k] for k in keys] + [descriptor(db, [values.get(k) for k in DETAILS])]
    return db.execute('INSERT INTO fragment_records(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in params) + ')', params).lastrowid
