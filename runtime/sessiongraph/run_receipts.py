"""Project-opted-in, lossless terminal Hook receipts in the existing SQLite file.

Only explicit maintenance creates the index. Readers never migrate or write.
Commit and verify the archived bytes before removing a duplicate hot file.
"""
from contextlib import contextmanager
import hashlib
import heapq
import json
import math
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from . import storage

TERMINAL = frozenset({'succeeded', 'prepared', 'coalesced', 'failed', 'stalled', 'cancelled'})
MAX_BYTES = 8 * 1024 * 1024
SCHEMA = '''
CREATE TABLE IF NOT EXISTS sg_run_receipts(
 project TEXT NOT NULL,run_id TEXT NOT NULL,chat TEXT NOT NULL,state TEXT NOT NULL,
 created REAL NOT NULL,updated REAL NOT NULL,modified REAL NOT NULL,
 source_hash TEXT NOT NULL,payload TEXT NOT NULL,hidden INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(project,run_id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS sg_run_receipts_recent ON sg_run_receipts(project,hidden,modified DESC,run_id);
CREATE INDEX IF NOT EXISTS sg_run_receipts_chat ON sg_run_receipts(project,chat,hidden,modified DESC,run_id);
'''


@contextmanager
def connection(config, *, readonly=True):
    path = Path(config['database']).resolve()
    db = sqlite3.connect(path.as_uri() + ('?mode=ro' if readonly else '?mode=rw'),
                         uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def enabled(db):
    return storage.exists(db, 'sg_run_receipts')


def validate(text, run_id):
    run = json.loads(text)
    if (not isinstance(run, dict) or run.get('run_id') != run_id
            or not isinstance(run.get('chat_id'), str) or not run['chat_id']
            or run.get('state') not in TERMINAL):
        raise ValueError('run_receipt_not_terminal')
    for key in ('created_at', 'updated_at'):
        value = run.get(key)
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError('run_receipt_invalid_time')
    return run


def _text(db, row):
    payload = row['payload']
    if payload.startswith('{"' + storage.ARCHIVE_MARKER + '":'):
        reference = json.loads(payload)
        info = db.execute('SELECT size FROM sg_payload_archive WHERE hash=?',
                          (reference.get(storage.ARCHIVE_MARKER),)).fetchone()
        if info is not None and not 0 <= info['size'] <= MAX_BYTES:
            raise ValueError('run_receipt_too_large')
    text = storage.payload_text(SimpleNamespace(db=db), row['payload'])
    if hashlib.sha256(text.encode()).hexdigest() != row['source_hash']:
        raise ValueError('run_receipt_hash_mismatch')
    run = validate(text, row['run_id'])
    if (run['chat_id'], run['state'], run['created_at'], run['updated_at']) != (
            row['chat'], row['state'], row['created'], row['updated']):
        raise ValueError('run_receipt_metadata_mismatch')
    return text


def read(config, run_id):
    """Return exact UTF-8 receipt text, including dismissed history; never a model call."""
    if not isinstance(run_id, str) or not run_id or len(run_id) > 200 or any(
            not (c.isalnum() or c in '-_') for c in run_id):
        raise ValueError('invalid_run_id')
    from .auto_update import _folder
    for folder in (_folder(config), _folder(config).parent / 'cleared-runs'):
        path = folder / (run_id + '.json')
        try:
            if path.is_symlink():
                raise ValueError('run_receipt_symlink_not_allowed')
            with path.open('rb') as stream:
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError('run_receipt_too_large')
            text = raw.decode('utf-8')
            run = json.loads(text)
            if not isinstance(run, dict) or run.get('run_id') != run_id:
                raise ValueError('run_receipt_identity_mismatch')
            return text
        except FileNotFoundError:
            continue
    with connection(config) as db:
        row = db.execute('SELECT * FROM sg_run_receipts WHERE project=? AND run_id=?',
                         (config['project_id'], run_id)).fetchone() if enabled(db) else None
        if row is None:
            raise ValueError('unknown_run_receipt')
        return _text(db, row)


def recent(db, project, limit=100):
    if not enabled(db):
        return []
    return db.execute('SELECT * FROM sg_run_receipts WHERE project=? AND hidden=0 '
                      'ORDER BY modified DESC,run_id LIMIT ?', (project, limit)).fetchall()


def archive_file(config, path, db):
    """Caller holds update.lock. Active and pending receipts are never archived."""
    if path.is_symlink():
        raise ValueError('run_receipt_symlink_not_allowed')
    with path.open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
        import os
        info = os.fstat(stream.fileno())
    if len(raw) > MAX_BYTES:
        raise ValueError('run_receipt_too_large')
    text = raw.decode('utf-8')
    run = validate(text, path.stem)
    source_hash = hashlib.sha256(raw).hexdigest()
    project = config['project_id']
    with db:
        prior = db.execute('SELECT * FROM sg_run_receipts WHERE project=? AND run_id=?',
                           (project, run['run_id'])).fetchone()
        if prior:
            if _text(db, prior) != text:
                raise ValueError('run_receipt_identity_conflict')
        else:
            payload = storage.archive_text(SimpleNamespace(db=db), text)
            db.execute('INSERT INTO sg_run_receipts VALUES(?,?,?,?,?,?,?,?,?,0)',
                       (project, run['run_id'], run['chat_id'], run['state'], run['created_at'],
                        run['updated_at'], info.st_mtime, source_hash, payload))
        row = db.execute('SELECT * FROM sg_run_receipts WHERE project=? AND run_id=?',
                         (project, run['run_id'])).fetchone()
        if _text(db, row).encode('utf-8') != raw:
            raise ValueError('run_receipt_roundtrip_failed')
    # A crash before unlink leaves two verified copies; readers deduplicate by ID.
    current = path.stat()
    if (current.st_ino, current.st_size, current.st_mtime_ns) != (
            info.st_ino, info.st_size, info.st_mtime_ns):
        raise ValueError('run_receipt_changed_during_archive')
    path.unlink()
    return len(raw)


def archive_if_enabled(config, path, run):
    if run.get('state') not in TERMINAL:
        return
    with connection(config, readonly=False) as db:
        if enabled(db):
            archive_file(config, path, db)


def maintain(config, *, apply=False, limit=100, max_bytes=16 * 1024 * 1024, after=''):
    """Bound payload work, not directory enumeration; cursor orders legacy filenames."""
    from .auto_update import _folder
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('invalid_receipt_limit')
    if type(max_bytes) is not int or not 1 <= max_bytes <= 64 * 1024 * 1024:
        raise ValueError('invalid_receipt_byte_budget')
    if not isinstance(after, str):
        raise ValueError('invalid_receipt_cursor')
    folder = _folder(config)
    paths = heapq.nsmallest(limit + 1, (p for p in folder.glob('*.json') if p.name > after),
                           key=lambda p: p.name)
    report = dict(examined=0, archived=0, eligible=0, raw_bytes=0, skipped=0, errors=[],
                  next_cursor=after, more=len(paths) > limit, model_calls=0, applied=apply)
    with connection(config, readonly=not apply) as db:
        if apply:
            db.executescript(SCHEMA)
        used = 0
        for path in paths[:limit]:
            try:
                size = path.stat().st_size
                if path.is_symlink() or size > MAX_BYTES:
                    report['examined'] += 1
                    report['next_cursor'] = path.name
                    raise ValueError('run_receipt_symlink_or_too_large')
                if used + size > max_bytes:
                    report['more'] = True
                    report['budget_exhausted'] = True
                    break
                used += size
                report['examined'] += 1
                report['next_cursor'] = path.name
                text = path.read_text(encoding='utf-8')
                run = json.loads(text)
                if not isinstance(run, dict):
                    raise ValueError('run_receipt_invalid_shape')
                if run.get('state') not in TERMINAL:
                    report['skipped'] += 1
                    continue
                validate(text, path.stem)
                report['eligible'] += 1
                if apply:
                    report['raw_bytes'] += archive_file(config, path, db)
                    report['archived'] += 1
            except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
                # No source contents or provider error strings in maintenance reports.
                report['errors'].append(dict(file=path.name, error_type=type(exc).__name__))
        report['payload_bytes_examined'] = used
        report['archive_enabled'] = enabled(db)
        report['archived_total'] = db.execute('SELECT count(*) FROM sg_run_receipts WHERE project=?',
                                              (config['project_id'],)).fetchone()[0] if enabled(db) else 0
    return report


def dismiss_failures(db, project):
    if not enabled(db):
        return 0
    return db.execute("UPDATE sg_run_receipts SET hidden=1 WHERE project=? AND hidden=0 "
                      "AND state IN ('failed','stalled','cancelled')", (project,)).rowcount
