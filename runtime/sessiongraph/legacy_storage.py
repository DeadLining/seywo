"""Bounded, lossless retirement of duplicate legacy search and event bodies."""
from contextlib import nullcontext
import zlib

from .core import digest
from . import source_text, storage

SCHEMA = '''CREATE TABLE IF NOT EXISTS sg_raw_archive(
 session_id TEXT NOT NULL REFERENCES sessions(id),line INTEGER NOT NULL,
 hash TEXT NOT NULL,size INTEGER NOT NULL,data BLOB NOT NULL,codec TEXT NOT NULL,
 PRIMARY KEY(session_id,line)) WITHOUT ROWID;'''


def raw_size(store, segment, line):
    row = store.db.execute('SELECT length(cast(event_json AS blob)) FROM raw_events '
                           'WHERE session_id=? AND line=?', (segment, line)).fetchone()
    if row:
        return row[0]
    if storage.exists(store.db, 'sg_raw_archive'):
        row = store.db.execute('SELECT size FROM sg_raw_archive WHERE session_id=? AND line=?',
                               (segment, line)).fetchone()
    return row[0] if row else None


def _unpack(row):
    from .ingestion import MAX_RECORD_BYTES
    if not 0 <= row['size'] <= MAX_RECORD_BYTES:
        raise ValueError('raw_archive_size_invalid')
    if row['codec'] == 'zlib':
        try:
            decoder = zlib.decompressobj()
            raw = decoder.decompress(row['data'], row['size'] + 1)
        except zlib.error as exc:
            raise ValueError('raw_archive_corrupt') from exc
        if not decoder.eof or decoder.unused_data:
            raise ValueError('raw_archive_stream_invalid')
    elif row['codec'] == 'raw':
        raw = row['data']
    else:
        raise ValueError('raw_archive_codec_invalid')
    if len(raw) != row['size'] or digest(raw) != row['hash']:
        raise ValueError('raw_archive_hash_mismatch')
    return raw.decode('utf-8')


def read_raw(store, segment, line):
    row = store.db.execute('SELECT event_json FROM raw_events WHERE session_id=? AND line=?',
                           (segment, line)).fetchone()
    if row:
        return row[0]
    if storage.exists(store.db, 'sg_raw_archive'):
        row = store.db.execute('SELECT * FROM sg_raw_archive WHERE session_id=? AND line=?',
                               (segment, line)).fetchone()
        if row:
            return _unpack(row)
    return None


def delete_raw(store, segment, line):
    store.db.execute('DELETE FROM raw_events WHERE session_id=? AND line=?', (segment, line))
    if storage.exists(store.db, 'sg_raw_archive'):
        store.db.execute('DELETE FROM sg_raw_archive WHERE session_id=? AND line=?', (segment, line))


def migrate(store, *, phase, apply=False, limit=256, max_bytes=16 * 1024 * 1024, after=0):
    if phase not in {'search', 'events'}:
        raise ValueError('invalid_legacy_phase')
    if type(limit) is not int or not 1 <= limit <= 2000:
        raise ValueError('invalid_legacy_limit')
    if type(max_bytes) is not int or not 1 <= max_bytes <= 64 * 1024 * 1024:
        raise ValueError('invalid_legacy_budget')
    if type(after) is not int or after < 0:
        raise ValueError('invalid_legacy_cursor')
    if apply:
        store._ensure_writable()
    table, field = ('docs', 'body') if phase == 'search' else ('raw_events', 'event_json')
    rows = store.db.execute(f'''SELECT rowid rid,length(cast({field} AS blob)) size
        FROM {table} WHERE rowid>? ORDER BY rowid LIMIT ?''', (after, limit + 1)).fetchall()
    report = dict(phase=phase, applied=apply, examined=0, migrated=0, original_bytes=0,
                  stored_bytes=0, more=len(rows) > limit, next_cursor=after, model_calls=0,
                  search_retired=source_text.unified(store))
    if apply and phase == 'events':
        store.db.executescript(SCHEMA)
    with store.db if apply else nullcontext():
        if apply:
            store.db.execute('BEGIN IMMEDIATE')
            source_text.schema_floor(store, 4)
        for r in rows[:limit]:
            if r['size'] > max_bytes - report['original_bytes']:
                report.update(more=True, budget_exhausted=True)
                break
            row = store.db.execute(f'SELECT rowid rid,* FROM {table} WHERE rowid=?', (r['rid'],)).fetchone()
            if not row:
                raise ValueError('legacy_migration_frontier_changed')
            report['examined'] += 1
            report['original_bytes'] += r['size']
            report['next_cursor'] = r['rid']
            if not apply:
                continue
            if phase == 'search':
                if row['type'] in {'entry', 'record'}:
                    parent = 'entries' if row['type'] == 'entry' else 'records'
                    if not store.db.execute(f'SELECT 1 FROM {parent} WHERE id=?', (row['id'],)).fetchone():
                        raise ValueError('legacy_search_orphan')
                    source_text.index_body(store, row['id'], row['body'], row['type'])
                elif row['type'] != 'memory':
                    raise ValueError('legacy_search_kind_unknown')
                store.db.execute('DELETE FROM docs WHERE rowid=?', (row['rid'],))
            else:
                raw = row['event_json'].encode('utf-8')
                packed = zlib.compress(raw, 6)
                data, codec = (packed, 'zlib') if len(packed) < len(raw) else (raw, 'raw')
                values = (row['session_id'], row['line'], digest(raw), len(raw), data, codec)
                prior = store.db.execute('SELECT * FROM sg_raw_archive WHERE session_id=? AND line=?', values[:2]).fetchone()
                if prior and _unpack(prior) != row['event_json']:
                    raise ValueError('raw_archive_identity_conflict')
                if not prior:
                    store.db.execute('INSERT INTO sg_raw_archive VALUES(?,?,?,?,?,?)', values)
                saved = store.db.execute('SELECT * FROM sg_raw_archive WHERE session_id=? AND line=?', values[:2]).fetchone()
                if _unpack(saved) != row['event_json']:
                    raise ValueError('raw_archive_roundtrip_failed')
                store.db.execute('DELETE FROM raw_events WHERE rowid=?', (row['rid'],))
                report['stored_bytes'] += len(saved['data'])
            report['migrated'] += 1
        if apply and phase == 'search' and not store.db.execute('SELECT 1 FROM docs LIMIT 1').fetchone():
            if not source_text.unified(store):
                # Rebuild only the now-empty legacy index, never original texts.
                store.db.execute("INSERT INTO docs(docs) VALUES('rebuild')")
                store.db.execute("INSERT INTO metadata VALUES('unified_search_v1','1')")
            report['search_retired'] = True
    return report
