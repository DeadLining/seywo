"""Durable stage receipts and source reservations. No provider access on reads."""
from contextvars import ContextVar
import json
import time
import uuid
import os
from .failures import describe

CURRENT = ContextVar('sessiongraph_attempt', default={})
RETRY_DELAYS = (120, 600, 1800)
SCHEMA = '''
CREATE TABLE IF NOT EXISTS sg2_attempts(id TEXT PRIMARY KEY,job TEXT NOT NULL,part TEXT NOT NULL,
 stage TEXT NOT NULL,state TEXT NOT NULL,started REAL NOT NULL,finished REAL,error_json TEXT,stats_json TEXT,run_id TEXT);
CREATE INDEX IF NOT EXISTS sg2_attempts_job ON sg2_attempts(job,stage);
CREATE TABLE IF NOT EXISTS sg2_checkpoints(job TEXT NOT NULL,part TEXT NOT NULL,stage TEXT NOT NULL,
 result_json TEXT NOT NULL,PRIMARY KEY(job,part,stage));
CREATE TABLE IF NOT EXISTS sg2_control(job TEXT PRIMARY KEY,state TEXT NOT NULL,due REAL NOT NULL,
 failures INTEGER NOT NULL,error_json TEXT);
CREATE TABLE IF NOT EXISTS sg2_claims(job TEXT NOT NULL,entry_id TEXT NOT NULL,source_hash TEXT NOT NULL,
 start INTEGER NOT NULL,end INTEGER NOT NULL,PRIMARY KEY(job,entry_id,start,end));
CREATE INDEX IF NOT EXISTS sg2_claims_entry ON sg2_claims(entry_id,source_hash);
'''


def setup(store):
    store.db.executescript(SCHEMA)
    if "run_id" not in {r[1] for r in store.db.execute("PRAGMA table_info(sg2_attempts)")}:
        store.db.execute("ALTER TABLE sg2_attempts ADD COLUMN run_id TEXT")
    store.db.execute("CREATE INDEX IF NOT EXISTS sg2_attempts_run ON sg2_attempts(run_id)")
    # Backfill only this project's unfinished jobs. Sources and completion ledger stay intact.
    with store.db:
        for row in store.db.execute("SELECT id,input_json FROM sg2_jobs WHERE project=? AND state NOT IN ('complete','superseded')", (store.config['project_id'],)).fetchall():
            reserve(store, row['id'], json.loads(row['input_json']))
        store.db.execute("DELETE FROM sg2_claims WHERE job IN (SELECT id FROM sg2_jobs WHERE state IN ('complete','superseded'))")


def reserve(store, job, desc):
    for eid, h, a, b, *_ in desc.get('coverage', []):
        store.db.execute('INSERT OR IGNORE INTO sg2_claims VALUES(?,?,?,?,?)', (job,eid,h,a,b))


def _resolve_scope(store, job, scope):
    if scope is None:
        return
    row = store.db.execute('SELECT error_json FROM sg2_control WHERE job=?', (job,)).fetchone()
    if row and json.loads(row[0] or '{}').get('retry_scope', {}).get('id') == scope['id']:
        # Only this exact successful stage can close its own retry cycle.
        # Cached earlier stages cannot clear a later stage's failures.
        store.db.execute('DELETE FROM sg2_control WHERE job=?', (job,))


def execute(store, job, part, stage, operation, stats, *, cache=True, cache_if=None):
    from .stage_leases import stage as execution_lease
    with execution_lease(store, job, part, stage) as claim:
        return _execute(store, job, part, stage, operation, stats,
                        cache=cache, cache_if=cache_if, claim=claim)


def _execute(store, job, part, stage, operation, stats, *, cache, cache_if, claim):
    from .recovery_policy import retry_scope
    scope = retry_scope(store.config, job, part, stage)
    if cache:
        row = store.db.execute('SELECT result_json FROM sg2_checkpoints WHERE job=? AND part=? AND stage=?', (job,part,stage)).fetchone()
        if row:
            if scope is not None:
                with store.db:
                    _resolve_scope(store, job, scope)
            return json.loads(row[0])
    identity = claim['attempt'] if claim else uuid.uuid4().hex
    # The legacy outer writer lock or opt-in kernel permit proves exclusivity.
    with store.db:
        store.db.execute("UPDATE sg2_attempts SET state='interrupted',finished=? WHERE job=? AND state='running'", (time.time(),job))
        store.db.execute("INSERT INTO sg2_attempts VALUES(?,?,?,?,'running',?,NULL,NULL,NULL,?)", (identity,job,part,stage,time.time(),os.environ.get("SESSIONGRAPH_RUN_ID")))
    token = CURRENT.set(dict(attempt_id=identity, job_id=job, part=part, stage=stage))
    before = {k:stats.get(k,0) for k in ('deepseek_calls','jev_calls','judge_calls')}
    try:
        result = operation()
        with store.db:
            if claim:
                from .stage_leases import ensure
                if not store.db.in_transaction:
                    store.db.execute('BEGIN IMMEDIATE')
                ensure(store, allow_complete=stage == 'publish_relations')
            _resolve_scope(store, job, scope)
            if cache and (cache_if is None or cache_if(result)):
                store.db.execute('INSERT OR REPLACE INTO sg2_checkpoints VALUES(?,?,?,?)', (job,part,stage,json.dumps(result,ensure_ascii=False)))
            store.db.execute("UPDATE sg2_attempts SET state='complete',finished=? WHERE id=?", (time.time(),identity))
            if stage == 'publish_relations':
                from .storage import prune_completed_checkpoints, archive_completed_payloads
                prune_completed_checkpoints(store, job)
                archive_completed_payloads(store, job)
        return result
    except Exception as exc:
        info = describe(exc, stage)
        deferred = scope is not None and info['code'] in {'graph_call_budget_pending', 'resource_circuit_open', 'stage_snapshot_changed', 'stage_lease_busy'}
        if scope is not None:
            info['retry_scope'] = scope
        try:
            if not getattr(exc, 'stage', ''): exc.stage = stage
            if scope is not None:
                exc.retry_scope = scope
        except (AttributeError, TypeError): pass
        with store.db:
            store.db.execute("UPDATE sg2_attempts SET state=?,finished=?,error_json=? WHERE id=?", ("deferred" if deferred else "failed",time.time(),json.dumps(info,ensure_ascii=False),identity))
        raise
    finally:
        try:
            with store.db:
                store.db.execute('UPDATE sg2_attempts SET stats_json=? WHERE id=?',
                    (json.dumps({k:stats.get(k,0)-v for k,v in before.items()}),identity))
        finally:
            CURRENT.reset(token)


def fail(store, job, info, *, scope=None):
    """One retry policy. Yielding for call budget is not a failed attempt."""
    if info['code'] == 'graph_call_budget_pending':
        return
    previous = store.db.execute('SELECT failures,error_json FROM sg2_control WHERE job=?',(job,)).fetchone()
    if info['code'] in {'resource_circuit_open', 'stage_snapshot_changed', 'stage_lease_busy'}:
        old = json.loads(previous['error_json'] or '{}') if previous else {}
        info = {**old, **info}
        store.db.execute('INSERT OR REPLACE INTO sg2_control VALUES(?,?,?,?,?)',
            (job, 'retry_wait', max(time.time() + 1, info['retry_at']),
             previous['failures'] if previous else 0, json.dumps(info, ensure_ascii=False)))
        return
    failures = (previous[0] if previous else 0) + 1
    if scope is not None:
        old = json.loads(previous['error_json'] or '{}') if previous else {}
        counts = dict(old.get('retry_scopes', {}))
        # An unscoped legacy failure remains a debt until its stage is proven.
        # Enabling the feature must not silently grant exhausted jobs new calls.
        inherited = previous['failures'] if previous and not old.get('retry_scope') else 0
        failures = counts.get(scope['id'], inherited) + 1
        counts[scope['id']] = failures
        info = {**info, 'retry_scope': scope, 'retry_scopes': counts}
    retry = info['retryable'] and failures <= len(RETRY_DELAYS)
    due = time.time()+RETRY_DELAYS[min(failures-1,len(RETRY_DELAYS)-1)] if retry else 0
    store.db.execute('INSERT OR REPLACE INTO sg2_control VALUES(?,?,?,?,?)',
        (job,'retry_wait' if retry else 'blocked',due,failures,json.dumps(info,ensure_ascii=False)))


def within_frontier(store, desc, through_rowid):
    if through_rowid is None:return True
    ids={c[0] for c in desc.get("coverage",[])} or {e["id"] for e in desc.get("entries",[]) if not e.get("context_only")}
    if not ids:return True
    marks=','.join('?' for _ in ids)
    row=store.db.execute(f"SELECT max(rowid) FROM entries WHERE id IN ({marks})",tuple(ids)).fetchone()
    return row[0] is not None and row[0]<=through_rowid


def pending(store, project, chat, through_rowid=None, *, include_manual=False):
    rows=store.db.execute("""SELECT j.id,j.segment,j.error,j.input_json,c.state,c.due,c.error_json
        FROM sg2_jobs j LEFT JOIN sg2_control c ON c.job=j.id
        WHERE j.project=? AND j.chat=? AND j.state NOT IN ('complete','superseded')""",(project,chat)).fetchall()
    return [{k:row[k] for k in row.keys() if k!="input_json"} for row in rows
            if (include_manual or not json.loads(row['input_json']).get('manual_only'))
            and within_frontier(store,json.loads(row["input_json"]),through_rowid)]
