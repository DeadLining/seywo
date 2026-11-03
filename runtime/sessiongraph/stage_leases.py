"""Opt-in stage ownership and short graph-write windows, with no heartbeat loop."""
from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
from functools import wraps
import hashlib
import os
from pathlib import Path
import time
import uuid

from .failures import Failure
from .resource_policy import AdmissionDenied

SESSION = ContextVar('sessiongraph_write_session', default=None)
CLAIM = ContextVar('sessiongraph_stage_claim', default=None)
SCHEMA = '''CREATE TABLE IF NOT EXISTS sg2_stage_lease(
 project TEXT PRIMARY KEY,generation INTEGER NOT NULL,owner TEXT NOT NULL,
 job TEXT NOT NULL,part TEXT NOT NULL,stage TEXT NOT NULL,attempt TEXT NOT NULL,
 pid INTEGER NOT NULL,state TEXT NOT NULL,acquired REAL NOT NULL,finished REAL);
'''


def enabled(config):
    value = config.get('graph_v2', {}).get('stage_policy', {})
    if (not isinstance(value, dict) or set(value) - {'enabled'}
            or type(value.get('enabled', False)) is not bool):
        raise ValueError('invalid graph stage_policy')
    if not value.get('enabled', False):
        return False
    from .resource_policy import settings
    if settings(config) is None:
        raise ValueError('stage_policy requires explicit cumulative resource_policy')
    return True


def root(config):
    return Path(config['database']).resolve().parent


@contextmanager
def locked(path, *, shared=False, nonblocking=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a') as handle:
        operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(handle, operation | (fcntl.LOCK_NB if nonblocking else 0))
        try:
            yield handle
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def quiescent(config, message='maintenance_busy'):
    """Check independently of config: a draining opted-in worker still owns SH."""
    try:
        context = locked(root(config) / 'stage-activity.lock', nonblocking=True)
        context.__enter__()
    except BlockingIOError:
        raise ValueError(message) from None
    try:
        yield
    finally:
        context.__exit__(None, None, None)


@contextmanager
def coordinator(config, chat):
    if not enabled(config):
        yield False
        return
    name = hashlib.sha256(chat.encode()).hexdigest()
    with locked(root(config) / 'stage-activity.lock', shared=True), \
            locked(root(config) / 'hook-state' / ('coordinator-' + name + '.lock')):
        yield True


@contextmanager
def child_window(handle, active):
    if active:
        fcntl.flock(handle, fcntl.LOCK_UN)
    try:
        yield
    finally:
        if active:
            fcntl.flock(handle, fcntl.LOCK_EX)


def managed(function):
    @wraps(function)
    def wrapper(store, extractor, chat_id, **kwargs):
        if not enabled(store.config):
            return function(store, extractor, chat_id, **kwargs)
        if SESSION.get() is not None:
            raise Failure('stage_session_nested')
        name = hashlib.sha256(chat_id.encode()).hexdigest()
        with locked(root(store.config) / 'stage-activity.lock', shared=True), \
                locked(root(store.config) / 'memory-extraction' / (name + '.stage.lock')), \
                locked(root(store.config) / 'graph-v2.lock') as handle:
            token = SESSION.set(dict(store=store, handle=handle))
            try:
                return function(store, extractor, chat_id, **kwargs)
            finally:
                SESSION.reset(token)
    return wrapper


def setup(store):
    store.db.executescript(SCHEMA)
    # Unlike graph_revision_v2, appending unrelated input must not invalidate an
    # in-flight decision. Any existing-source mutation or graph change does.
    store.db.execute("INSERT OR IGNORE INTO metadata VALUES('stage_dependency_revision_v1','0')")
    tables = ('work_memory', 'work_memory_relations', 'sg2_nodes', 'sg2_keys', 'sg2_terms',
              'sg2_relations', 'sg2_unresolved', 'sg2_evidence', 'sg2_relation_evidence',
              'sg_revision_items', 'sg_revision_sources', 'sg_revision_reviews', 'sg_revision_review_controls',
              'entries', 'session_chats')
    for table in tables:
        if not store.db.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?', (table,)).fetchone():
            continue
        for action in ('INSERT', 'UPDATE', 'DELETE'):
            if action == 'INSERT' and table in {'entries', 'session_chats'}:
                continue
            store.db.execute(f'''CREATE TRIGGER IF NOT EXISTS sg_stage_rev_{table}_{action.lower()}
                AFTER {action} ON {table} BEGIN UPDATE metadata SET value=CAST(value AS INTEGER)+1
                WHERE key='stage_dependency_revision_v1'; END''')
    store.db.commit()


def revision(store):
    row = store.db.execute("SELECT value FROM metadata WHERE key='stage_dependency_revision_v1'").fetchone()
    if row is None:
        raise Failure('stage_revision_unavailable')
    return row[0]


def ensure(store, *, allow_complete=False, check_attempt=True):
    claim = CLAIM.get()
    if not claim or claim['store'] is not store:
        raise Failure('stage_lease_missing')
    row = store.db.execute('''SELECT l.*,j.state AS job_state,a.state AS attempt_state
        FROM sg2_stage_lease l JOIN sg2_jobs j ON j.id=l.job AND j.project=l.project
        LEFT JOIN sg2_attempts a ON a.id=l.attempt AND a.job=l.job AND a.part=l.part AND a.stage=l.stage
        WHERE l.project=?''', (store.config['project_id'],)).fetchone()
    if (not row or row['owner'] != claim['owner'] or row['generation'] != claim['generation']
            or any(row[k] != claim[k] for k in ('job', 'part', 'stage', 'attempt'))
            or row['state'] != 'running' or row['job_state'] in {'superseded', 'dismissed'}
            or check_attempt and row['attempt_state'] != 'running'
            or row['job_state'] == 'complete' and not allow_complete):
        raise Failure('stage_lease_lost')
    return claim


@contextmanager
def stage(store, job, part, name):
    if not enabled(store.config):
        yield None
        return
    session = SESSION.get()
    if session is None or session['store'] is not store:
        raise AdmissionDenied('stage_session_missing')
    try:
        permit = locked(root(store.config) / 'stage-execution.lock', nonblocking=True)
        permit.__enter__()
    except BlockingIOError:
        error = AdmissionDenied('stage_lease_busy', retry_at=time.time() + 30)
        error.stage = name
        from .recovery_policy import retry_scope
        error.retry_scope = retry_scope(store.config, job, part, name)
        raise error from None
    claim = dict(store=store, owner=uuid.uuid4().hex, attempt=uuid.uuid4().hex,
                 job=job, part=part, stage=name)
    token = None
    try:
        with store.db:
            store.db.execute('BEGIN IMMEDIATE')
            row = store.db.execute('SELECT * FROM sg2_stage_lease WHERE project=?', (store.config['project_id'],)).fetchone()
            claim['generation'] = row['generation'] + 1 if row else 1
            if row and row['state'] == 'running':
                # Acquiring the kernel permit, not elapsed time/PID guesses,
                # proves that its previous execution owner has released/died.
                store.db.execute("UPDATE sg2_attempts SET state='interrupted',finished=? WHERE id=? AND state='running'",
                                 (time.time(), row['attempt']))
            store.db.execute('INSERT OR REPLACE INTO sg2_stage_lease VALUES(?,?,?,?,?,?,?,?,?,?,NULL)',
                (store.config['project_id'], claim['generation'], claim['owner'], job, part, name,
                 claim['attempt'], os.getpid(), 'running', time.time()))
        token = CLAIM.set(claim)
        ensure(store, check_attempt=False)
        yield claim
    finally:
        try:
            with store.db:
                store.db.execute("UPDATE sg2_attempts SET state='interrupted',finished=? WHERE id=? AND state='running'",
                                 (time.time(), claim['attempt']))
                store.db.execute("UPDATE sg2_stage_lease SET state='released',finished=? WHERE project=? AND owner=? AND generation=? AND state='running'",
                    (time.time(), store.config['project_id'], claim['owner'], claim.get('generation', -1)))
        finally:
            if token is not None:
                CLAIM.reset(token)
            permit.__exit__(None, None, None)


@contextmanager
def transport_window(client):
    if not enabled(client.config):
        yield
        return
    session = SESSION.get()
    if session is None or session['store'].config is not client.config:
        if hasattr(client, 'calls'):
            client.calls = max(0, client.calls - 1)
        raise AdmissionDenied('stage_session_missing')
    store = session['store']
    try:
        ensure(store)
        if store.db.in_transaction:
            raise Failure('stage_transport_transaction_open')
        before = revision(store)
    except Failure as error:
        if hasattr(client, 'calls'):
            client.calls = max(0, client.calls - 1)
        raise AdmissionDenied(error.code) from None
    fcntl.flock(session['handle'], fcntl.LOCK_UN)
    try:
        yield
    finally:
        fcntl.flock(session['handle'], fcntl.LOCK_EX)
        ensure(store)
        if revision(store) != before:
            error = Failure('stage_snapshot_changed', retryable=True)
            error.retry_at = time.time() + 1
            raise error
