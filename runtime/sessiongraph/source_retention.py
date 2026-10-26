"""Explicit, project-local retention of published evidence. Never deletes sources.

Snapshots exclude known credentials and inline media bytes. Hashes distinguish the
verified original record from its retained form; external paths are not backups.
"""
import json
from pathlib import Path
import re
import sqlite3

from . import storage
from .core import digest, normalize
from .failures import Failure

POLICY = 'critical-record-v1'
FLAG = 'source_retention_policy'
MAX_BYTES = 8 * 1024 * 1024
BATCH_BYTES = 16 * 1024 * 1024
SENSITIVE = re.compile(r'(?i)(authorization|api.?key|access.?token|password|secret)')
SCHEMA = '''CREATE TABLE IF NOT EXISTS sg_retained_sources(
 entry_id TEXT PRIMARY KEY REFERENCES entries(id),project TEXT NOT NULL,
 record_hash TEXT NOT NULL,text_hash TEXT NOT NULL,snapshot_hash TEXT NOT NULL,
 snapshot_text_hash TEXT NOT NULL,policy TEXT NOT NULL,redactions_json TEXT NOT NULL,
 record_bytes INTEGER NOT NULL,snapshot_bytes INTEGER NOT NULL,payload TEXT NOT NULL
) WITHOUT ROWID;'''


def fail(code):
    raise Failure('source_retention_' + code, stage='retention')


class _Budget:
    def __init__(self, limit):
        self.limit, self.used = limit, 0

    def reserve(self, size):
        if size > self.limit - self.used:
            fail('byte_budget')
        self.used += size


def setup(store):
    """Explicit migration only; neither setup nor backfill enables publication."""
    store._ensure_writable()
    store.db.execute(SCHEMA)
    for table, field in (('work_memory', 'source_entry_id'),
                         ('work_memory_relations', 'source_entry_id'),
                         ('sg2_evidence', 'entry_id'),
                         ('sg2_relation_evidence', 'entry_id')):
        if storage.exists(store.db, table):
            indexes = store.db.execute(f'PRAGMA index_list({table})').fetchall()
            covered = False
            for index in indexes:
                name = index['name'].replace('"', '""')
                columns = store.db.execute(f'PRAGMA index_info("{name}")').fetchall()
                if columns and columns[0]['name'] == field and not index['partial']:
                    covered = True
                    break
            if not covered:
                store.db.execute(f'CREATE INDEX IF NOT EXISTS sg_retention_{table}_source ON {table}({field})')
            # Old SQL writers cannot silently publish new unretained evidence.
            # Disabled projects retain exactly their previous write behavior.
            for action in ('INSERT', f'UPDATE OF {field}'):
                suffix = 'insert' if action == 'INSERT' else 'update'
                store.db.execute(f'''CREATE TRIGGER IF NOT EXISTS sg_retention_{table}_{suffix}
                    BEFORE {action} ON {table}
                    WHEN EXISTS(SELECT 1 FROM metadata WHERE key='{FLAG}')
                    AND NOT EXISTS(SELECT 1 FROM sg_retained_sources
                                   WHERE entry_id=new.{field})
                    BEGIN SELECT RAISE(ABORT,'source_retention_snapshot_required'); END''')


def enabled(store):
    row = store.db.execute('SELECT value FROM metadata WHERE key=?', (FLAG,)).fetchone()
    if not row:
        return False
    if row[0] != POLICY or not storage.exists(store.db, 'sg_retained_sources'):
        fail('policy_unavailable')
    return True


def _entry(store, entry_id):
    row = store.db.execute('''SELECT e.id,e.session_id,e.line,e.timestamp,e.kind,e.role,e.call_id,
        length(cast(e.text AS blob)) text_bytes,s.path FROM entries e
        JOIN sessions s ON s.id=e.session_id WHERE e.id=?''', (entry_id,)).fetchone()
    if row is None:
        fail('unknown_entry')
    return row


def _original(store, row, meter):
    """Bounded point reads only: an exact legacy DB record or a byte locator."""
    if row['text_bytes'] > MAX_BYTES:
        fail('record_too_large')
    loc = store.db.execute('SELECT * FROM sg_source_locations WHERE entry_id=?',
                           (row['id'],)).fetchone() if storage.exists(store.db, 'sg_source_locations') else None
    raw = None
    saved = store.db.execute('SELECT length(cast(event_json AS blob)) FROM raw_events '
                             'WHERE session_id=? AND line=?', (row['session_id'], row['line'])).fetchone()
    if saved:
        if saved[0] > MAX_BYTES:
            fail('record_too_large')
        meter.reserve(saved[0])
        raw = store.db.execute('SELECT event_json FROM raw_events WHERE session_id=? AND line=?',
                                (row['session_id'], row['line'])).fetchone()[0].encode('utf-8')
    else:
        if not loc or loc['byte_offset'] is None or loc['byte_length'] is None:
            fail('locator_missing')
        if (type(loc['byte_offset']) is not int or type(loc['byte_length']) is not int
                or loc['byte_offset'] < 0 or loc['byte_length'] <= 0):
            fail('locator_invalid')
        if loc['byte_length'] > MAX_BYTES:
            fail('record_too_large')
        meter.reserve(loc['byte_length'])
        try:
            with Path(row['path']).open('rb') as stream:
                stream.seek(loc['byte_offset'])
                raw = stream.read(loc['byte_length'])
            if len(raw) != loc['byte_length'] or not raw.endswith(b'\n'):
                fail('source_changed')
            raw = raw[:-1].removesuffix(b'\r')
        except FileNotFoundError:
            fail('source_missing')
        except OSError:
            fail('source_unavailable')
    try:
        event = json.loads(raw)
        normalized = normalize(event['payload'])
        text = store.db.execute('SELECT text FROM entries WHERE id=?', (row['id'],)).fetchone()[0]
        identity = 'e_' + digest(f"{row['session_id']}:{row['line']}:{digest(raw)}")[:32]
        if (event.get('type') != 'response_item' or identity != row['id']
                or normalized != (row['kind'], row['role'], text, row['call_id'])
                or str(event.get('timestamp', '')) != row['timestamp']
                or (loc and (loc['record_hash'] != digest(raw) or loc['text_hash'] != digest(text)))):
            fail('source_changed')
    except Failure:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError, RecursionError):
        fail('source_invalid')
    return raw.decode('utf-8'), text, event


def _sanitize(event):
    from .content_boundary import BINARY_KEYS, DATA_URI, MEDIA
    from .graph_contract import clean
    changes = set()

    def walk(value, media=False):
        if isinstance(value, dict):
            kind = value.get('type', value.get('kind', ''))
            media = media or (isinstance(kind, str) and kind.lower() in MEDIA) or value.get('encoding') == 'base64' or value.get('type') == 'base64'
            result = {}
            for key, child in value.items():
                if SENSITIVE.search(key):
                    result[key] = '[redacted]'
                    changes.add('credentials')
                elif media and key in BINARY_KEYS:
                    result[key] = '[media_not_retained]'
                    changes.add('inline_media')
                else:
                    result[key] = walk(child, media)
            return result
        if isinstance(value, list):
            return [walk(child, media) for child in value]
        if isinstance(value, str):
            # Tool arguments/output can themselves be serialized JSON envelopes.
            if value.lstrip().startswith(('{', '[')):
                try:
                    parsed = json.loads(value)
                except ValueError:
                    pass
                else:
                    safe = walk(parsed)
                    if safe != parsed:
                        return json.dumps(safe, ensure_ascii=False, separators=(',', ':'))
            safe = clean(value)
            if safe != value:
                changes.add('credentials')
            media_safe = DATA_URI.sub('[media_not_retained]', safe)
            if media_safe != safe:
                changes.add('inline_media')
            return media_safe
        return value

    return walk(event), changes


def read(store, entry_id, *, max_bytes=MAX_BYTES, _meter=None):
    """Return one independently verified retained record, without source-file I/O."""
    if not storage.exists(store.db, 'sg_retained_sources'):
        return None
    row = store.db.execute('SELECT * FROM sg_retained_sources WHERE entry_id=?', (entry_id,)).fetchone()
    if row is None:
        return None
    if row['project'] != store.config['project_id'] or row['policy'] != POLICY:
        fail('metadata_mismatch')
    if type(row['snapshot_bytes']) is not int or not 0 < row['snapshot_bytes'] <= MAX_BYTES:
        fail('snapshot_size')
    if type(row['record_bytes']) is not int or not 0 < row['record_bytes'] <= MAX_BYTES:
        fail('metadata_mismatch')
    if max(row['record_bytes'], row['snapshot_bytes']) > max_bytes:
        fail('byte_budget')
    if _meter is not None:
        _meter.reserve(max(row['record_bytes'], row['snapshot_bytes']))
    try:
        text = storage.payload_text(store, row['payload'], max_bytes=max_bytes)
    except (ValueError, UnicodeError, sqlite3.Error):
        fail('snapshot_corrupt')
    if len(text.encode('utf-8')) != row['snapshot_bytes'] or digest(text) != row['snapshot_hash']:
        fail('snapshot_hash_mismatch')
    try:
        event = json.loads(text)
        normalized = normalize(event['payload'])
        redactions = json.loads(row['redactions_json'])
        entry = _entry(store, entry_id)
        if entry['text_bytes'] > MAX_BYTES:
            fail('indexed_text_changed')
        if (not normalized or event.get('type') != 'response_item'
                or normalized[0:2] != (entry['kind'], entry['role']) or normalized[3] != entry['call_id']
                or str(event.get('timestamp', '')) != entry['timestamp']
                or digest(normalized[2]) != row['snapshot_text_hash']
                or not isinstance(redactions, list) or set(redactions) - {'credentials', 'inline_media'}
                or (not redactions and row['record_hash'] != row['snapshot_hash'])
                or 'e_' + digest(f"{entry['session_id']}:{entry['line']}:{row['record_hash']}")[:32] != entry_id):
            fail('metadata_mismatch')
        current_hash = digest(store.db.execute('SELECT text FROM entries WHERE id=?', (entry_id,)).fetchone()[0])
        if current_hash != row['text_hash']:
            fail('indexed_text_changed')
    except Failure:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError, RecursionError):
        fail('snapshot_invalid')
    return dict(entry_id=entry_id, status='retained_sanitized' if redactions else 'retained_verified',
                storage='retained_snapshot', policy=POLICY, record_hash=row['record_hash'],
                snapshot_hash=row['snapshot_hash'], indexed_text_hash=row['text_hash'],
                snapshot_text_hash=row['snapshot_text_hash'], redactions=redactions,
                record_bytes=row['record_bytes'], snapshot_bytes=row['snapshot_bytes'],
                text=normalized[2], record=text, line=entry['line'])


def capture(store, entry_id, *, budget=MAX_BYTES, _meter=None):
    """Caller owns the transaction. Existing immutable snapshots are never replaced."""
    store._ensure_writable()
    meter = _meter if _meter is not None else _Budget(budget)
    prior = read(store, entry_id, _meter=meter)
    if prior:
        return dict(added=False, bytes=max(prior['record_bytes'], prior['snapshot_bytes']), redactions=prior['redactions'])
    raw, text, event = _original(store, _entry(store, entry_id), meter)
    safe, changes = _sanitize(event)
    snapshot = json.dumps(safe, ensure_ascii=False, separators=(',', ':')) if changes else raw
    if len(snapshot.encode('utf-8')) > MAX_BYTES:
        fail('snapshot_too_large')
    meter.reserve(max(0, len(snapshot.encode('utf-8')) - len(raw.encode('utf-8'))))
    snapshot_text = normalize(safe['payload'])[2]
    payload = storage.archive_text(store, snapshot)
    store.db.execute('INSERT INTO sg_retained_sources VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                     (entry_id, store.config['project_id'], digest(raw), digest(text), digest(snapshot),
                      digest(snapshot_text), POLICY, json.dumps(sorted(changes)), len(raw.encode('utf-8')),
                      len(snapshot.encode('utf-8')), payload))
    if read(store, entry_id)['record'] != snapshot:
        fail('roundtrip')
    return dict(added=True, bytes=max(len(raw.encode('utf-8')), len(snapshot.encode('utf-8'))), redactions=sorted(changes))


def capture_publication(store, entry_ids):
    if not enabled(store):
        return
    meter = _Budget(BATCH_BYTES)
    for entry_id in sorted(set(entry_ids)):
        capture(store, entry_id, _meter=meter)


def candidates(store, after, limit):
    selects = []
    for table, field in (('work_memory', 'source_entry_id'), ('work_memory_relations', 'source_entry_id'),
                         ('sg2_evidence', 'entry_id'), ('sg2_relation_evidence', 'entry_id')):
        if storage.exists(store.db, table):
            selects.append(f'SELECT {field} entry_id FROM {table} WHERE {field}>?')
    return [r[0] for r in store.db.execute(' UNION '.join(selects) + ' ORDER BY entry_id LIMIT ?',
                                          [after] * len(selects) + [limit])]


def maintain(store, *, apply=False, enable=False, disable=False, limit=100, max_bytes=BATCH_BYTES, after=''):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('invalid_retention_limit')
    if type(max_bytes) is not int or not 1 <= max_bytes <= 64 * 1024 * 1024:
        raise ValueError('invalid_retention_byte_budget')
    if not isinstance(after, str) or (after and not re.fullmatch(r'e_[a-f0-9]{32}', after)):
        raise ValueError('invalid_retention_cursor')
    if (enable or disable) and not apply:
        raise ValueError('retention_enable_requires_apply')
    if enable and disable:
        raise ValueError('retention_conflicting_policy_change')
    if apply:
        with store.db:
            setup(store)
            if disable:
                store.db.execute('DELETE FROM metadata WHERE key=?', (FLAG,))
    if disable:
        return dict(applied=True, examined=0, added=0, existing=0, eligible=0, gaps=[],
                    payload_bytes=0, sanitized=0, next_cursor=after, more=False,
                    model_calls=0, publication_enabled=False)
    ids = candidates(store, after, limit + 1)
    report = dict(applied=apply, examined=0, added=0, existing=0, eligible=0, gaps=[],
                  payload_bytes=0, sanitized=0, next_cursor=after, more=len(ids) > limit, model_calls=0)
    meter = _Budget(max_bytes)
    for entry_id in ids[:limit]:
        try:
            if apply:
                with store.db:
                    store.db.execute('BEGIN IMMEDIATE')
                    result = capture(store, entry_id, _meter=meter)
            else:
                prior = read(store, entry_id, _meter=meter)
                if prior:
                    result = dict(added=False, bytes=max(prior['record_bytes'], prior['snapshot_bytes']), redactions=prior['redactions'])
                else:
                    raw, _, event = _original(store, _entry(store, entry_id), meter)
                    safe, changes = _sanitize(event)
                    size = len(json.dumps(safe, ensure_ascii=False, separators=(',', ':')).encode()) if changes else len(raw.encode())
                    if size > MAX_BYTES:
                        fail('snapshot_too_large')
                    meter.reserve(max(0, size - len(raw.encode())))
                    result = dict(added=True, bytes=max(len(raw.encode()), size), redactions=changes)
            report['eligible'] += 1
            report['sanitized'] += bool(result['redactions'])
            report['added'] += int(apply and result['added'])
            report['existing'] += int(not result['added'])
        except Failure as exc:
            if exc.code == 'source_retention_byte_budget':
                report.update(more=True, budget_exhausted=True, blocked_entry_id=entry_id)
                break
            report['gaps'].append(dict(entry_id=entry_id, code=exc.code))
        finally:
            report['payload_bytes'] = meter.used
        report['examined'] += 1
        report['next_cursor'] = entry_id
    if enable:
        with store.db:
            store.db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', (FLAG, POLICY))
    report['publication_enabled'] = enabled(store)
    return report
