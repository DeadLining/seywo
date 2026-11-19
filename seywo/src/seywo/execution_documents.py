"""Immutable task bodies: exact text, shared compression and bounded JSON deltas."""
from functools import lru_cache
from collections import OrderedDict
import hashlib
import json
import zlib

from .document_store import _apply, _changes, encode

MAX_DEPTH = 31


def setup(db):
    db.execute('''CREATE TABLE execution_documents(
        hash TEXT PRIMARY KEY, base_hash TEXT REFERENCES execution_documents(hash),
        body BLOB NOT NULL, depth INTEGER NOT NULL, family TEXT NOT NULL,
        logical_bytes INTEGER NOT NULL, encoding TEXT NOT NULL)''')
    db.execute('CREATE INDEX execution_family ON execution_documents(family)')
    db.execute('CREATE INDEX execution_base ON execution_documents(base_hash)')


def read(db, ref):
    if ref is None:
        return None
    chain, seen = [], set()
    while ref is not None:
        if ref in seen or len(chain) > MAX_DEPTH:
            raise ValueError('execution document chain invalid')
        seen.add(ref)
        row = db.execute('SELECT base_hash,body,encoding FROM execution_documents WHERE hash=?', (ref,)).fetchone()
        if row is None:
            raise ValueError('missing execution document')
        chain.append((ref, row[0], zlib.decompress(row[1]).decode(), row[2]))
        ref = row[0]
    text = None
    for identity, base, body, encoding in reversed(chain):
        text = encode(_apply(json.loads(text), json.loads(body))) if base else body
        if hashlib.sha256(text.encode()).hexdigest() != identity:
            raise ValueError('execution document checksum mismatch')
    return text


def put(db, text, *, family, reader=None):
    if text is None:
        return None
    identity = hashlib.sha256(text.encode()).hexdigest()
    if db.execute('SELECT 1 FROM execution_documents WHERE hash=?', (identity,)).fetchone():
        return identity
    base, depth, data, encoding = None, 0, zlib.compress(text.encode(), 3), 'text'
    try:
        value = json.loads(text)
        canonical = encode(value) == text
    except ValueError:
        canonical = False
    if canonical:
        encoding = 'json'
        previous = db.execute('SELECT hash,depth FROM execution_documents WHERE family=? AND encoding=? ORDER BY rowid DESC LIMIT 1',
                              (family, 'json')).fetchone()
        if previous and previous[1] < MAX_DEPTH:
            old = json.loads((reader or (lambda ref: read(db, ref)))(previous[0]))
            patch = zlib.compress(encode(_changes(old, value)).encode(), 3)
            if len(patch) < len(data):
                base, depth, data = previous[0], previous[1] + 1, patch
    db.execute('INSERT INTO execution_documents VALUES(?,?,?,?,?,?,?)',
               (identity, base, data, depth, family, len(text.encode()), encoding))
    return identity



class Writer:
    """A small per-connection cache avoids reconstructing a delta base on every write."""
    def __init__(self, db):
        self.db, self.cache = db, OrderedDict()

    def read(self, ref):
        return self.cache[ref] if ref in self.cache else read(self.db, ref)

    def put(self, text, *, family):
        ref = put(self.db, text, family=family, reader=self.read)
        if ref is not None:
            self.cache[ref] = text
            self.cache.move_to_end(ref)
            while len(self.cache) > 8:
                self.cache.popitem(last=False)
        return ref


def register(db):
    @lru_cache(maxsize=4)
    def cached(ref):
        return read(db, ref)

    writer = Writer(db)

    def save(text, job, phase, kind):
        row = db.execute('SELECT chat_id FROM understanding_jobs WHERE id=?', (job,)).fetchone()
        return writer.put(text, family=f'{row[0] if row else job}:{phase}:{kind}')

    db.create_function('seywo_execution_read', 1, cached)
    db.create_function('seywo_execution_put', 4, save)
