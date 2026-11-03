"""Source-backed transcript text. Metadata lives here; original bytes stay in Codex.

Legacy inline rows remain readable. Externalization is per entry and atomic with
its locator and search index, so interrupted migrations never produce ambiguity.
"""
import json
from pathlib import Path
import sqlite3

from .core import digest

SCHEMA = """
CREATE TABLE IF NOT EXISTS sg_external_entries(
 entry_id TEXT PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE,
 chars INTEGER NOT NULL,bytes INTEGER NOT NULL,preview TEXT NOT NULL,
 search_rowid INTEGER NOT NULL
) WITHOUT ROWID;
CREATE VIRTUAL TABLE IF NOT EXISTS sg_source_search USING fts5(
 body,content='',contentless_delete=1);
CREATE TABLE IF NOT EXISTS sg_search_keys(
 kind TEXT NOT NULL,id TEXT NOT NULL,search_rowid INTEGER NOT NULL UNIQUE,
 PRIMARY KEY(kind,id)) WITHOUT ROWID;
"""
POLICY = 'codex-source-v1'


def enabled(store):
    row = store.db.execute("SELECT value FROM metadata WHERE key='source_text_policy'").fetchone()
    return row is not None and row[0] == POLICY


def available(store):
    return bool(store.db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='sg_external_entries'").fetchone())


def unified(store):
    return store.db.execute("SELECT 1 FROM metadata WHERE key='unified_search_v1'").fetchone() is not None


def schema_floor(store, version):
    store.db.execute("UPDATE metadata SET value=? WHERE key='schema_version' AND CAST(value AS INTEGER)<?",
                     (str(version), version))


def setup(store):
    if sqlite3.sqlite_version_info < (3, 43, 0):
        raise ValueError('source_backed_storage_requires_sqlite_3_43')
    store.db.executescript(SCHEMA)
    with store.db:
        # Schema inspection and replacement must share one writer reservation.
        store.db.execute('BEGIN IMMEDIATE')
        columns = {r['name'] for r in store.db.execute('PRAGMA table_info(sg_external_entries)')}
        if 'search_rowid' not in columns:
            store.db.execute('ALTER TABLE sg_external_entries ADD COLUMN search_rowid INTEGER')
            store.db.execute('''UPDATE sg_external_entries SET search_rowid=(
                SELECT rowid FROM entries WHERE id=sg_external_entries.entry_id)''')
        store.db.execute('CREATE UNIQUE INDEX IF NOT EXISTS sg_external_search_rowid ON sg_external_entries(search_rowid)')
        if not store.db.execute("SELECT 1 FROM metadata WHERE key='search_keys_backfilled_v1'").fetchone():
            store.db.execute("INSERT INTO sg_search_keys SELECT 'entry',entry_id,search_rowid FROM sg_external_entries")
            store.db.execute("INSERT INTO metadata VALUES('search_keys_backfilled_v1','1')")
        source_trigger = '''CREATE TRIGGER sg_source_search_delete BEFORE DELETE ON entries BEGIN
            DELETE FROM sg_source_search WHERE rowid=(
                SELECT search_rowid FROM sg_search_keys WHERE kind='entry' AND id=old.id);
            DELETE FROM sg_search_keys WHERE kind='entry' AND id=old.id; END'''
        prior = store.db.execute("SELECT sql FROM sqlite_master WHERE name='sg_source_search_delete'").fetchone()
        if prior is None or prior[0] != source_trigger:
            store.db.execute('DROP TRIGGER IF EXISTS sg_source_search_delete')
            store.db.execute(source_trigger)
        store.db.execute('''CREATE TRIGGER IF NOT EXISTS sg_record_search_delete BEFORE DELETE ON records BEGIN
            DELETE FROM sg_source_search WHERE rowid=(
                SELECT search_rowid FROM sg_search_keys WHERE kind='record' AND id=old.id);
            DELETE FROM sg_search_keys WHERE kind='record' AND id=old.id; END''')
        if enabled(store):
            schema_floor(store, 3)


def activate_defaults(store):
    """Switch future writes only. Never scan sources or migrate historical rows."""
    store._ensure_writable()
    if store.db.execute("SELECT 1 FROM metadata WHERE key='storage_defaults_v1'").fetchone():
        return
    from .run_receipts import setup as receipt_setup
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        receipt_setup(store)
        store.db.execute("INSERT INTO metadata VALUES('source_text_policy',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (POLICY,))
        # Mixed reads must continue until the last historical search row is migrated.
        if not store.db.execute('SELECT 1 FROM docs LIMIT 1').fetchone():
            store.db.execute("INSERT OR IGNORE INTO metadata VALUES('unified_search_v1','1')")
        schema_floor(store, 4)
        store.db.execute("INSERT OR IGNORE INTO metadata VALUES('storage_defaults_v1','1')")


def external(store, entry_id):
    return available(store) and store.db.execute(
        'SELECT 1 FROM sg_external_entries WHERE entry_id=?', (entry_id,)).fetchone() is not None


def hydrate(store, row):
    if row is None:
        return None
    item = dict(row)
    if item.get('text') == '' and external(store, item['id']):
        from .source_locator import read_original
        original = read_original(store, item['id'], prefer_retained=False)
        if original['status'] != 'verified':
            from .source_retention import read
            retained = read(store, item['id'])
            expected = source_hash(store, item['id'], '')
            if retained and digest(retained['text']) == expected:
                original = retained
            else:
                raise ValueError('source_' + original['status'])
        item['text'] = original['text']
    return item


def source_hash(store, entry_id, inline):
    if inline == '' and external(store, entry_id):
        row = store.db.execute('SELECT text_hash FROM sg_source_locations WHERE entry_id=?',
                               (entry_id,)).fetchone()
        if row is None:
            raise ValueError('source_locator_missing')
        return row[0]
    return digest(inline)


def register(store):
    def original(entry_id, inline):
        try:
            return hydrate(store, dict(id=entry_id, text=inline))['text']
        except (ValueError, OSError):
            # SQL evidence filters fail closed. Missing files are not deletion signals.
            return None
    def size(entry_id, inline):
        row = store.db.execute('SELECT bytes FROM sg_external_entries WHERE entry_id=?',
                               (entry_id,)).fetchone() if available(store) else None
        return row[0] if row else len(inline.encode('utf-8'))
    store.db.create_function('sg_source_text', 2, original)
    store.db.create_function('sg_source_bytes', 2, size)


def index(store, entry_id, text):
    from .core import tokens
    return index_body(store, entry_id, ' '.join(tokens(text)), 'entry')


def index_body(store, object_id, body, kind):
    if kind not in {'entry', 'record'}:
        raise ValueError('invalid_search_kind')
    prior = store.db.execute('SELECT search_rowid FROM sg_search_keys WHERE kind=? AND id=?', (kind, object_id)).fetchone()
    if prior:
        store.db.execute('DELETE FROM sg_source_search WHERE rowid=?', (prior[0],))
        store.db.execute('INSERT INTO sg_source_search(rowid,body) VALUES(?,?)',
                         (prior[0], body))
        return prior[0]
    rowid = store.db.execute('INSERT INTO sg_source_search(body) VALUES(?)', (body,)).lastrowid
    store.db.execute('INSERT INTO sg_search_keys VALUES(?,?,?)', (kind, object_id, rowid))
    return rowid


def write_doc(store, object_id, kind, body):
    if enabled(store) or unified(store):
        if kind != 'memory':
            index_body(store, object_id, body, kind)
    else:
        store.db.execute('INSERT INTO docs VALUES(?,?,?)', (object_id, kind, body))


def mark(store, entry_id, text, search_rowid):
    store.db.execute('INSERT INTO sg_external_entries VALUES(?,?,?,?,?)',
                     (entry_id, len(text), len(text.encode('utf-8')), text[:32], search_rowid))
    store.db.execute('UPDATE sg_entry_sizes SET chars=? WHERE entry_id=?', (len(text), entry_id))


def recover_location(store, entry_id, budget):
    """Recover a legacy locator from its nearest known predecessor, within budget."""
    from .core import normalize
    from .ingestion import MAX_RECORD_BYTES
    row = store.db.execute('SELECT e.*,s.path FROM entries e JOIN sessions s ON s.id=e.session_id WHERE e.id=?',
                           (entry_id,)).fetchone()
    # A missing source cannot yield a locator. Do not search its historical
    # predecessors for every entry before discovering the same missing file.
    try:
        Path(row['path']).stat()
    except FileNotFoundError:
        return None, 0, 'missing'
    except OSError:
        return None, 0, 'unavailable'
    previous = store.db.execute('''SELECT e.line,l.byte_offset,l.byte_length FROM entries e
        JOIN sg_source_locations l ON l.entry_id=e.id WHERE e.session_id=? AND e.line<?
        AND l.byte_offset IS NOT NULL AND l.byte_offset>=0 AND l.byte_length>0
        ORDER BY e.line DESC LIMIT 1''', (row['session_id'], row['line'])).fetchone()
    offset = previous['byte_offset'] + previous['byte_length'] if previous else 0
    first_line = previous['line'] + 1 if previous else 1
    used = 0
    try:
        with Path(row['path']).open('rb') as stream:
            stream.seek(offset)
            for line in range(first_line, row['line'] + 1):
                remaining = budget - used
                if remaining <= 0:
                    return None, used, 'byte_budget'
                raw = stream.readline(min(MAX_RECORD_BYTES + 1, remaining))
                used += len(raw)
                if not raw.endswith(b'\n'):
                    return None, used, 'byte_budget' if len(raw) == remaining else 'changed'
                if len(raw) > MAX_RECORD_BYTES:
                    return None, used, 'source_record_too_large'
                if line < row['line']:
                    offset += len(raw)
        record = raw[:-1].removesuffix(b'\r')
        event = json.loads(record)
        normalized = normalize(event['payload'])
        expected = 'e_' + digest(f"{row['session_id']}:{row['line']}:{digest(record)}")[:32]
        if (expected != entry_id or event.get('type') != 'response_item'
                or normalized != (row['kind'], row['role'], row['text'], row['call_id'])
                or str(event.get('timestamp', '')) != row['timestamp']):
            return None, used, 'changed'
        prior = store.db.execute('SELECT record_hash,text_hash FROM sg_source_locations WHERE entry_id=?',
                                 (entry_id,)).fetchone()
        if prior and (prior['record_hash'] != digest(record) or prior['text_hash'] != digest(row['text'])):
            return None, used, 'changed'
        loc = (entry_id, event['payload'].get('channel', ''), event['payload'].get('name', ''),
               offset, len(raw), digest(record), digest(row['text']))
        return loc, used, 'verified'
    except FileNotFoundError:
        return None, used, 'missing'
    except OSError:
        return None, used, 'unavailable'
    except (ValueError, TypeError, KeyError):
        return None, used, 'changed'


def migrate(store, *, limit=256, max_bytes=16 * 1024 * 1024, after=0, apply=False):
    """Bounded, resumable point verification. Failed sources keep their inline copy."""
    from .source_locator import read_original
    if type(limit) is not int or not 1 <= limit <= 2000:
        raise ValueError('invalid_source_migration_limit')
    if type(max_bytes) is not int or not 1 <= max_bytes <= 64 * 1024 * 1024:
        raise ValueError('invalid_source_migration_byte_budget')
    if type(after) is not int or after < 0:
        raise ValueError('invalid_source_migration_cursor')
    if apply:
        store._ensure_writable()
    report = dict(examined=0, verified=0, migrated=0, locators_backfilled=0, inline_bytes_removed=0,
                  legacy_search_bytes_removed=0, raw_bytes_removed=0, retained=[],
                  next_cursor=after, more=False, applied=apply, model_calls=0)
    # Fetch metadata first; a large entry cannot bypass the byte budget.
    exclusion = 'AND NOT EXISTS(SELECT 1 FROM sg_external_entries x WHERE x.entry_id=e.id)' if available(store) else ''
    rows = store.db.execute(f'''SELECT e.rowid rid,e.id,length(cast(e.text AS blob)) size
        FROM entries e WHERE e.rowid>? {exclusion}
        ORDER BY e.rowid LIMIT ?''', (after, limit + 1)).fetchall()
    if apply and not getattr(store, '_externalize_doc_map', False):
        # docs.id is UNINDEXED. Build a temporary ID map once per maintenance
        # connection, not a full FTS-content scan for every migrated entry.
        store.db.executescript('''CREATE TEMP TABLE sg_externalize_docs(
            docid INTEGER PRIMARY KEY,entry_id TEXT NOT NULL);
            INSERT INTO sg_externalize_docs SELECT rowid,id FROM docs WHERE type='entry';
            CREATE INDEX sg_externalize_docs_id ON sg_externalize_docs(entry_id);''')
        store._externalize_doc_map = True
    from contextlib import nullcontext
    with store.db if apply else nullcontext():
        if apply:
            store.db.execute('BEGIN IMMEDIATE')
            store.db.execute("INSERT INTO metadata VALUES('source_text_policy',?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (POLICY,))
            schema_floor(store, 3)
        used = 0
        for row in rows[:limit]:
            loc = store.db.execute('SELECT byte_length,byte_offset,record_hash FROM sg_source_locations WHERE entry_id=?',
                                   (row['id'],)).fetchone()
            cost = max(row['size'], loc['byte_length'] or 0) if loc else row['size']
            if cost > max_bytes - used:
                report['more'] = True
                report['budget_exhausted'] = True
                break
            recovered = None
            if not loc or loc['byte_offset'] is None:
                recovered, read_bytes, reason = recover_location(store, row['id'], max_bytes - used)
                used += read_bytes
                if reason == 'byte_budget':
                    report.update(more=True, budget_exhausted=True)
                    break
                original = dict(status=reason)
                if recovered:
                    original['text'] = store.db.execute('SELECT text FROM entries WHERE id=?', (row['id'],)).fetchone()[0]
                    loc = dict(record_hash=recovered[5])
            else:
                used += cost
                original = read_original(store, row['id'], prefer_retained=False)
            report['examined'] += 1
            report['next_cursor'] = row['rid']
            if original['status'] != 'verified':
                report['retained'].append(dict(entry_id=row['id'], reason=original['status']))
                continue
            report['verified'] += 1
            if not apply:
                continue
            current = store.db.execute('SELECT * FROM entries WHERE id=?', (row['id'],)).fetchone()
            if current is None or external(store, row['id']):
                continue
            if current['text'] != original['text']:
                raise ValueError('source_migration_entry_changed')
            if recovered:
                store.db.execute('INSERT OR REPLACE INTO sg_source_locations VALUES(?,?,?,?,?,?,?)', recovered)
                report['locators_backfilled'] += 1
            search_rowid = index(store, row['id'], original['text'])
            docids = [r[0] for r in store.db.execute('''SELECT d.rowid FROM sg_externalize_docs m
                JOIN docs d ON d.rowid=m.docid WHERE m.entry_id=? AND d.id=? AND d.type='entry' ''',
                (row['id'], row['id']))]
            search_bytes = sum(store.db.execute('SELECT length(cast(body AS blob)) FROM docs WHERE rowid=?',
                                                 (docid,)).fetchone()[0] for docid in docids)
            from .legacy_storage import read_raw, delete_raw
            raw = read_raw(store, current['session_id'], current['line'])
            # Unnormalized/foreign records are not discarded as a side effect.
            raw_size = len(raw.encode('utf-8')) if raw is not None and digest(raw) == loc['record_hash'] else 0
            if raw_size:
                delete_raw(store, current['session_id'], current['line'])
            store.db.executemany('DELETE FROM docs WHERE rowid=?', [(docid,) for docid in docids])
            store.db.execute("UPDATE entries SET text='' WHERE id=?", (row['id'],))
            mark(store, row['id'], original['text'], search_rowid)
            report['migrated'] += 1
            report['inline_bytes_removed'] += row['size']
            report['legacy_search_bytes_removed'] += search_bytes
            report['raw_bytes_removed'] += raw_size
        report['more'] = report['more'] or len(rows) > limit
        report['bytes_examined'] = used
    return report
