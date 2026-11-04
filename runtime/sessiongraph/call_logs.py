"""Bounded metadata-only model call ledger. Best-effort writes; read-side stays read-only."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit, urlunsplit

from .provider_credentials import runtime_state

MAX_BYTES = 20 * 1024 * 1024
RETENTION = 7 * 86400
PAGE_SIZE = 4096
LIMIT = 50
FAILED = ('transport_failed', 'response_failed', 'invalid_json', 'invalid_envelope', 'validation_failed', 'probe_failed')
SUCCEEDED = ('validated', 'probe_ok')
SCHEMA = '''CREATE TABLE IF NOT EXISTS calls(
 id TEXT PRIMARY KEY,project TEXT NOT NULL,job TEXT NOT NULL,created REAL NOT NULL,
 updated REAL NOT NULL,model TEXT NOT NULL,state TEXT NOT NULL,data TEXT NOT NULL);
 CREATE INDEX IF NOT EXISTS calls_time ON calls(created DESC,id DESC);
 CREATE INDEX IF NOT EXISTS calls_project ON calls(project,created DESC,id DESC);
 CREATE INDEX IF NOT EXISTS calls_job ON calls(project,job);
 CREATE INDEX IF NOT EXISTS calls_model ON calls(project,model,created DESC);
'''


def _text(value, limit=256):
    return ''.join(c for c in value if c.isprintable())[:limit] if isinstance(value, str) else ''


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def summary(record, project=''):
    """Allowlist fields only. Never persist headers, bodies, excerpts or error text."""
    item = {key: _text(record.get(key), cap) for key, cap in (
        ('id', 96), ('model', 256), ('response_model', 256), ('chat_id', 128),
        ('run_id', 96), ('job_id', 96), ('attempt_id', 96), ('stage', 64),
        ('state', 32), ('provider_request_id', 128))}
    item['project'] = _text(project, 20)
    for key in ('created_at', 'elapsed_ms', 'http_status', 'part', 'retry_count'):
        item[key] = _number(record.get(key))
    try:
        url = urlsplit(record.get('endpoint', ''))
        # No userinfo, query or fragment can reach the ledger.
        host = url.hostname or ''
        authority = f'[{host}]' if ':' in host else host
        if url.port: authority += f':{url.port}'
        item['endpoint'] = _text(urlunsplit((url.scheme, authority, url.path, '', '')), 512)
    except (ValueError, TypeError):
        item['endpoint'] = ''
    usage = record.get('usage')
    usage = usage if isinstance(usage, dict) else {}
    item['input_tokens'] = _number(usage.get('input_tokens', usage.get('prompt_tokens')))
    item['output_tokens'] = _number(usage.get('output_tokens', usage.get('completion_tokens')))
    failure = record.get('failure')
    code = failure.get('code') if isinstance(failure, dict) else record.get('error_code')
    item['error_code'] = code if isinstance(code, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,79}', code) else ''
    return item


def _path(state):
    return Path(state if state is not None else runtime_state()) / 'call-logs' / 'calls.sqlite3'


def _connect_write(state):
    path = _path(state)
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError('unsafe call log path')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            os.close(fd)
        except FileExistsError:
            pass
    if not path.is_file() or path.is_symlink(): raise ValueError("unsafe call log file")
    db = sqlite3.connect(path, timeout=0.05)
    try:
        db.execute('PRAGMA busy_timeout=50')
        db.execute('PRAGMA journal_mode=TRUNCATE')
        db.execute('PRAGMA synchronous=NORMAL')
        db.execute(f'PRAGMA max_page_count={MAX_BYTES // PAGE_SIZE}')
        db.executescript(SCHEMA)
        return db
    except Exception:
        db.close()
        raise


def record(value, *, project='', state=None, secret=''):
    """One row per actual request, updated through transport and validation."""
    db = None
    try:
        item = summary(value, project)
        if secret:
            item = {k: v.replace(secret, '[redacted]') if isinstance(v, str) else v for k,v in item.items()}
        if not item['id'] or item['created_at'] is None:
            return False
        db = _connect_write(state)
        now = time.time()
        with db:
            db.execute('DELETE FROM calls WHERE created<?', (now - RETENTION,))
            pages = db.execute('PRAGMA page_count').fetchone()[0]
            free = db.execute('PRAGMA freelist_count').fetchone()[0]
            # Leave room for the next bounded row and its indexes, reusing pages.
            if pages - free >= MAX_BYTES // PAGE_SIZE - 32:
                db.execute('DELETE FROM calls WHERE id IN (SELECT id FROM calls ORDER BY created,id LIMIT 512)')
            db.execute('''INSERT INTO calls VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                updated=excluded.updated,model=excluded.model,state=excluded.state,data=excluded.data''',
                (item['id'], item['project'], item['job_id'], item['created_at'], now,
                 item['model'], item['state'], json.dumps(item, ensure_ascii=False, separators=(',', ':'))))
        return True
    except Exception:
        # A locked, full, unavailable or corrupt diagnostics ledger cannot fail a call.
        return False
    finally:
        if db is not None: db.close()


def read(*, project=None, model='', status='all', offset=0, state=None):
    if project is not None and (not isinstance(project, str) or not re.fullmatch(r'[a-fA-F0-9]{20}', project)):
        raise ValueError('invalid log project')
    if not isinstance(model, str) or len(model) > 256 or status not in ('all', 'failed', 'succeeded', 'pending'):
        raise ValueError('invalid log filter')
    if type(offset) is not int or not 0 <= offset <= 50_000_000:
        raise ValueError('invalid log offset')
    result = dict(items=[], models=[], offset=offset, has_more=False, retention_days=7,
                  max_bytes=MAX_BYTES, model_calls=0)
    path = _path(state)
    if not path.is_file(): return result
    if path.is_symlink() or path.parent.is_symlink(): raise ValueError('unsafe call log path')
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.1)
    try:
        db.execute('PRAGMA query_only=ON')
        deadline = time.monotonic() + 1
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        where, args = ['created>=?'], [time.time() - RETENTION]
        if project is not None:
            where.append('project=?'); args.append(project)
        result['models'] = [r[0] for r in db.execute('SELECT DISTINCT model FROM calls WHERE ' + ' AND '.join(where) + ' ORDER BY model LIMIT 200', args) if r[0]]
        if model: where.append('model=?'); args.append(model)
        if status in ('failed', 'succeeded'):
            states = FAILED if status == 'failed' else SUCCEEDED
            where.append('state IN (' + ','.join('?' for _ in states) + ')'); args.extend(states)
        elif status == 'pending':
            states = FAILED + SUCCEEDED
            where.append('state NOT IN (' + ','.join('?' for _ in states) + ')'); args.extend(states)
        rows = db.execute('SELECT data FROM calls WHERE ' + ' AND '.join(where) + ' ORDER BY created DESC,id DESC LIMIT ? OFFSET ?', [*args, LIMIT + 1, offset]).fetchall()
        result['items'] = [json.loads(row[0]) for row in rows[:LIMIT]]
        result['has_more'] = len(rows) > LIMIT
        return result
    finally:
        db.close()


def remove_jobs(config, job_ids):
    """Follow existing permanent-chat deletion; never touch recovery audit files."""
    db = None
    try:
        path = _path(None)
        if not path.is_file() or path.is_symlink() or path.parent.is_symlink(): return
        db = sqlite3.connect(path, timeout=0.05)
        with db:
            db.executemany('DELETE FROM calls WHERE project=? AND job=?',
                           [(config['project_id'], job) for job in job_ids])
    except Exception:
        pass
    finally:
        if db is not None: db.close()
