"""Durable, per-project single-flight continuation. Timed kernel waits, no periodic polling.

Only write-side entry points start this runner. Reads merely inspect its queue.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import time
import uuid

DEFERRED_CODES = {'graph_call_budget_pending', 'graph_continuation_pending',
                  'daily_call_budget_pending', 'runtime_upgrade_pending',
                  'resource_circuit_open'}
RETRY_DELAYS = (120, 600, 1800)


def root(config=None):
    if os.environ.get('SESSIONGRAPH_WORKER_LOCK'):
        return Path(os.environ['SESSIONGRAPH_WORKER_LOCK']).parent / 'continuations'
    # Isolated library/test use must never write the user's global runtime.
    if config:
        return Path(config['database']).parent / 'hook-state' / 'continuations'
    return Path(os.environ.get('SESSIONGRAPH_RUNTIME_HOME', str(Path.home() / 'Library/Application Support/local-ai-gateway/sessiongraph'))) / 'continuations'


@contextmanager
def locked(folder):
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder/'queue.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def read(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def write(path, data):
    from .auto_update import _atomic
    _atomic(path, data)


def release_legacy_waits(queue, now):
    """Migrate old normal delays to ready; retain actual failure backoff.

    Write-side scheduler only, under queue.lock. Reads never launch work.
    """
    changed = False
    for job in queue.values():
        state = job.get('state')
        if state == 'daily_limit' or (state == 'cooldown' and job.get('error_code') in DEFERRED_CODES):
            job.update(state='ready', due=now)
            if state == 'daily_limit':job['error_code']='runtime_upgrade_pending'
            changed = True
        elif state == 'cooldown':
            job['state']='retry_wait'
            changed = True
    return changed


def key(config_path, chat):
    return hashlib.sha256(f'{Path(config_path).resolve()}\0{chat}'.encode()).hexdigest()


def provider_identity(config):
    provider = config['provider']
    values = [provider.get('protocol'), provider.get('endpoint'),
              provider.get('model') or os.environ.get(provider.get('model_env', ''), ''),
              os.environ.get(provider.get('base_url_env', ''), '')]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def import_retryable(run):
    """Consume import classification instead of guessing from the first error code."""
    if not str(run.get('error_code') or '').startswith('import_'):
        return False
    diagnostic = run.get('import_diagnostics') or {}
    if type(diagnostic.get('retryable')) is bool:
        return diagnostic['retryable']
    # Old receipts may have truncated issue arrays: uncertainty is not permission
    # to retry forever. New receipts always carry the aggregate classification.
    issues = [i for i in diagnostic.get('sources',[]) if i.get('blocking')]
    return (bool(issues) and not diagnostic.get('omitted_sources')
            and all(i.get('retryable') is True for i in issues))


def record(config_path, run, *, launch=True, now=None, claimed=None):
    """Coalesce by project/chat; newer triggers cannot overwrite a newer target."""
    from .core import load_config
    config=load_config(Path(config_path))
    from .recovery_policy import settings as recovery_settings
    recovery = recovery_settings(config)
    input_imported = (type(run.get('target_rowid')) is int
                      and not str(run.get('error_code') or '').startswith('import_'))
    if run['event']=='PreCompact' or run['state']=='coalesced':
        return
    folder=root(config)
    stamp=time.time() if now is None else now
    with locked(folder):
        path=folder/'queue.json'
        queue=read(path,{})
        identity=key(config_path,run['chat_id'])
        prior=queue.get(identity,{})
        # A claimed execution may finish after cleanup or a replacement runner.
        # Wall-clock ordering alone cannot protect new input arriving mid-flight.
        if claimed is not None and prior.get('lease') != claimed.get('lease'):
            return
        if claimed is None and prior.get('lease'):
            return
        if (prior.get('state') == 'dismissed' and
                run.get('created_at', 0) <= prior.get('dismissed_at', 0)):
            return
        if claimed is None and (prior.get('updated_at',0)>run.get('updated_at',0) or
            (run['event']=='AutoContinue' and (prior.get('target') or 0)>(run.get('target_rowid') or 0))):
            return
        error=run.get('error_code')
        pending_import = prior.get('pending_import') if run['event'] == 'AutoContinue' else None
        # An import failure is a separate work item from the immutable boundary
        # already admitted to the graph worker. Drain that boundary first, then
        # retain the import failure visibly without an automatic retry loop.
        if (str(error or '').startswith('import_') and run.get('target_rowid') is None
                and type(prior.get('target')) is int):
            pending_import = {'error_code':error, 'run_id':run['run_id'],
                              'source_transcript':run.get('source_transcript',''),
                              'updated_at':run.get('updated_at',stamp)}
            run = {**run, 'target_rowid':prior['target'], 'state':'pending',
                   'error_code':'graph_continuation_pending',
                   'graph_v2':{'source_remaining':True}}
            error = run['error_code']
        more=bool((run.get('graph_v2') or {}).get('source_remaining'))
        deferred=error in DEFERRED_CODES or (not error and more and run['state'] in {'pending','succeeded'})
        if run['state']=='cancelled' or (run['state']=='succeeded' and not more):
            if pending_import and run['state'] != 'cancelled':
                queue[identity] = {**prior, **pending_import, 'target':None,
                    'state':'blocked', 'due':0, 'updated_at':run.get('updated_at',stamp)}
                queue[identity].pop('pending_import', None)
            else:
                queue.pop(identity,None)
        elif run['state'] in {'pending','failed'} or more:
            failures=prior.get('failures',0) if run['event']=='AutoContinue' else 0
            if not deferred:
                failures+=1
            transient=(run.get('pending_retry_count',0)>0 or error in {'queue_failed','extraction_failed','import_failed'} or import_retryable(run)) and not run.get('blocked_retry_count')
            no_progress=prior.get('no_progress',0) if run['event']=='AutoContinue' else 0
            graph=run.get('graph_v2') or {}
            if error in {'graph_call_budget_pending','graph_continuation_pending'} or (more and not error):
                progressed=any(graph.get(k,0) for k in ('input_source_chars','nodes_saved','relations_saved','adaptive_splits')) or run.get('saved_count',0) or run.get('saved_relation_count',0) or (run.get('input_stats') or {}).get('selected_chars',0)
                if graph.get('progress_tracked'):
                    progressed = bool(graph.get('durable_progress', 0)
                                      or graph.get('adaptive_splits', 0)
                                      or graph.get('schema_repairs', 0))
                elif recovery and graph.get('deferred_reason') == 'graph_call_budget_pending':
                    # Successful judgments can advance cached work before any
                    # complete relation is published. A zero-call spin cannot.
                    progressed = progressed or run.get('reported_model_calls', 0) > 0
                no_progress=0 if progressed else no_progress+1
            blocked=(not deferred and (not transient or failures>len(RETRY_DELAYS))) or no_progress>=3
            if no_progress>=3:error='continuation_no_progress'
            due=stamp if deferred else stamp+RETRY_DELAYS[min(max(0,failures-1),len(RETRY_DELAYS)-1)]
            if graph.get('durable_stages') and error != 'continuation_no_progress' and not (error or '').startswith('import_'):
                # Import failure prevents graph execution; runnable graph rows cannot override it.
                # A blocked batch cannot suppress unrelated runnable work or another
                # batch's scheduled retry. Per-job controls own retry exhaustion.
                deferred=bool(graph.get('runnable_remaining'))
                next_retry=graph.get('next_retry_at')
                blocked=not deferred and not next_retry
                due=stamp if deferred else max(stamp,next_retry or stamp)
                if deferred and not error:error='graph_continuation_pending'
                if (not recovery and not graph.get('progress_tracked')) or not deferred:
                    no_progress=0
                if not deferred and not error:
                    backlog=run.get('backlog_failures') or []
                    error=next((f.get('error_code') for f in backlog if f.get('retryable') == bool(next_retry)),None)
            if (graph.get('durable_stages') and error
                    and error.startswith(('provider_', 'single_token_'))):
                # New source is not evidence that the shared provider recovered.
                # Yield the project while preserving each job's retry controls.
                deferred = False
                blocked = not run.get('pending_retry_count')
                next_retry = graph.get('next_retry_at')
                due = (next_retry if type(next_retry) in (int, float)
                       and stamp < next_retry < float('inf') else
                       stamp + RETRY_DELAYS[min(max(0, failures - 1), len(RETRY_DELAYS) - 1)])
            if error == 'single_token_budget_incompatible':
                blocked, deferred = True, False
            if error == 'resource_circuit_open':
                # An expired batch deadline or fresh input cannot bypass the
                # current backend's cooldown. Old receipts lack a single deadline.
                deadlines = [graph.get('next_retry_at'), run.get('next_retry_at')]
                deadlines.extend((item.get('failure') or {}).get('retry_at')
                    for item in run.get('backlog_failures') or []
                    if item.get('error_code') == error and item.get('retryable') is True)
                cooldown = config.get('graph_v2', {}).get('resource_policy', {}).get('circuit_cooldown_seconds', 120)
                if type(cooldown) is not int or cooldown < 1:
                    cooldown = 120
                due = max((value for value in deadlines
                    if type(value) in (int, float) and stamp < value < float('inf')),
                    default=stamp + cooldown)
                blocked, deferred, no_progress = False, False, 0
                failures = prior.get('failures', 0)
            queue[identity]={'config':str(Path(config_path).resolve()),'chat':run['chat_id'],
                'target':run.get('target_rowid'), 'run_id':run['run_id'],
                'source_transcript':run.get('source_transcript') or prior.get('source_transcript',''),
                'updated_at':run.get('updated_at',stamp),'failures':failures,'no_progress':no_progress,
                'state':'blocked' if blocked else 'ready' if deferred else 'retry_wait',
                'due':due,'error_code':error}
            if error == 'single_token_budget_incompatible':
                queue[identity]['provider_identity'] = provider_identity(config)
            if pending_import:
                queue[identity]['pending_import'] = pending_import
        from .hook_ingress import reconcile
        reconcile(queue, identity, prior, run, claimed, input_imported=input_imported)
        if identity in queue:
            queue[identity].pop('lease', None)
            queue[identity].pop('ingress_source', None)
        write(path,queue)
        # Launch while holding queue lock. Runner releases singleton under this
        # same lock on empty, closing the enqueue-vs-exit lost-wakeup race.
        if launch and any(v['state'] not in {'blocked', 'dismissed'} for v in queue.values()):
            start(folder)


def start(folder=None):
    folder=folder or root()
    if not (folder/'queue.json').exists():return
    # Dispatcher starts independent project runners; each owns a project lease.
    source=Path(__file__).resolve().parents[1]
    subprocess.Popen([sys.executable,'-B','-m','sessiongraph.continuation','--root',str(folder)],
        cwd=source,env={**os.environ,'PYTHONPATH':str(source),'PYTHONDONTWRITEBYTECODE':'1'},
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)


def snapshot(config, chat=''):
    # Read-only: must not create files or launch workers.
    try:queue=read(root(config)/'queue.json',{})
    except (OSError, ValueError):return [{'chat':chat,'state':'blocked','due':0,'run_id':'','error_code':'continuation_state_unreadable'}]
    items = [dict(v) for v in queue.values() if v.get('state') != 'dismissed' and (not chat or v['chat']==chat)
            and str(Path(v['config']).resolve())==str(Path(config['_config_path']).resolve())] if '_config_path' in config else [dict(v) for v in queue.values() if v.get('state') != 'dismissed' and (not chat or v['chat']==chat) and Path(v['config']).parent==Path(config['database']).parent]


    if not any(item.get("state") == "blocked" for item in items):
        return items
    # Presentation reconciles evidence without writing or waking a worker.
    try:
        from .core import Store
        with_store = Store(config, readonly=True)
        try:
            with_store.db.execute("BEGIN")
            return [item for item in items if not completed_boundary(with_store, item)]
        finally:
            with_store.close()
    except (OSError, ValueError, sqlite3.Error):
        return items


def completed_boundary(store, item):
    """Conservative proof for an obsolete blocked notice, never a retry policy.

    Require completed attempted jobs AND a durable cursor through the trigger's
    finite input boundary. Merely having zero blocked jobs proves neither.
    """
    if item.get('state') != 'blocked' or type(item.get('target')) is not int:
        return False
    from .hook_ingress import pending
    if pending(item):
        return False  # Completed old graph input says nothing about new notifications.
    code = item.get('error_code') or ''
    if not (code.startswith(('provider_', 'jev_', 'graph_')) or code == 'extraction_partial_failure'):
        return False
    db, project, chat = store.db, store.config['project_id'], item.get('chat')
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'sg2_attempts','sg2_jobs','sg2_cursors','session_chats'} <= tables:
        return False
    if 'run_id' not in {r[1] for r in db.execute('PRAGMA table_info(sg2_attempts)')}:
        return False
    jobs = db.execute("SELECT DISTINCT j.state FROM sg2_attempts a JOIN sg2_jobs j ON j.id=a.job WHERE a.run_id=? AND j.project=? AND j.chat=?",
                      (item.get('run_id'), project, chat)).fetchall()
    if not jobs or any(r[0] != 'complete' for r in jobs):
        return False
    if db.execute("SELECT 1 FROM sg2_jobs WHERE project=? AND chat=? AND state NOT IN ('complete','superseded','dismissed') AND NOT coalesce(json_extract(input_json,'$.manual_only'),0) LIMIT 1", (project,chat)).fetchone():
        return False
    if db.execute("SELECT 1 FROM sg2_attempts a JOIN sg2_jobs j ON j.id=a.job WHERE j.project=? AND j.chat=? AND a.state='running' AND NOT coalesce(json_extract(j.input_json,'$.manual_only'),0) LIMIT 1", (project,chat)).fetchone():
        return False
    if not db.execute("SELECT 1 FROM entries e JOIN session_chats sc ON sc.segment_id=e.session_id WHERE sc.chat_id=? AND e.rowid<=? LIMIT 1",(chat,item["target"])).fetchone():
        return False
    # No guess for missing segments/cursors. Partial line coverage stays visible.
    rows = db.execute("""SELECT e.line,coalesce((SELECT chars FROM sg_entry_sizes WHERE entry_id=e.id),length(e.text)),c.line,c.start FROM entries e
        JOIN session_chats sc ON sc.segment_id=e.session_id AND sc.chat_id=?
        LEFT JOIN sg2_cursors c ON c.project=? AND c.segment=e.session_id
        WHERE e.rowid<=? AND (c.line IS NULL OR e.line>c.line OR
              (e.line=c.line AND coalesce((SELECT chars FROM sg_entry_sizes WHERE entry_id=e.id),length(e.text))>c.start)) LIMIT 1""", (chat,project,item['target'])).fetchone()
    return rows is None


def clean_completed(config):
    """Explicit maintenance under graph lock, queue lock, then one read snapshot.

    Re-read the queue under lock; never remove another project's entry. Historic
    run/attempt receipts remain unchanged. No network or scheduler invocation.
    """
    import sqlite3
    from .core import Store
    folder = root(config)
    if not (folder/'queue.json').exists():
        return {'removed':0,'remaining':0,'model_calls':0}
    with locked(folder):
        queue = read(folder/'queue.json',{})
        selected = {k:v for k,v in queue.items() if str(Path(v['config']).resolve()) == str(Path(config['_config_path']).resolve())}
        store = Store(config, readonly=True)
        try:
            store.db.execute('BEGIN')
            removed = [k for k,v in selected.items() if completed_boundary(store,v)]
        except sqlite3.Error:
            removed = []  # Missing legacy schema is not completion evidence.
        finally:
            store.close()
        if removed:
            for k in removed:queue.pop(k)
            write(folder/'queue.json',queue)
    return {'removed':len(removed),'remaining':len(selected)-len(removed),'model_calls':0}


def automatic_enabled(config_path, config):
    settings = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'sessiongraph.json'
    # Uninstall writes enabled=false. Missing/corrupt registration fails closed.
    global_enabled = read(settings, {}).get('enabled') is True
    return (global_enabled and not (Path(config_path).parent / 'disabled').exists()
            and config.get('hook', {}).get('enabled') is not False
            )


def process_one(folder, *, now=None, execute=None, project=None):
    """One due job, always releases queue lock before expensive work."""
    stamp=time.time() if now is None else now
    with locked(folder):
        queue=read(folder/'queue.json',{})
        if release_legacy_waits(queue, stamp):
            write(folder/'queue.json',queue)
        ready=sorted(((k,v) for k,v in queue.items() if v['state'] not in {'blocked', 'dismissed'} and v['due']<=stamp and (project is None or str(Path(v['config']).resolve())==project)),key=lambda item:(item[1]['due'],item[0]))
        if not ready:return False
        identity, job=ready[0]
        from .hook_ingress import pending
        inputs = [] if job.get('pending_import') else pending(job)
        job={**job,'state':'running','lease':uuid.uuid4().hex,
             'ingress_source': inputs[0][0] if inputs else None}
        queue[identity]=job
        write(folder/'queue.json',queue)
    from .core import load_config
    from .auto_update import run_update
    try:
        config=load_config(Path(job['config']))
        if not automatic_enabled(job['config'], config):
            with locked(folder):
                queue=read(folder/'queue.json',{})
                current = queue.get(identity, {})
                if current.get('lease') == job['lease']:
                    if current.get('ingress'):
                        current.update(state='blocked', error_code='ingress_cancelled')
                        current.pop('lease', None)
                        current.pop('ingress_source', None)
                    else:
                        queue.pop(identity,None)
                write(folder/'queue.json',queue)
            return True
        from .hook_ingress import enabled
        if inputs and not enabled(config):
            raise ValueError('durable ingress disabled after admission')
        selected = inputs[0][1] if inputs else None
        run=run_update(job['config'],job['chat'],'AutoContinue',
            transcript=selected['path'] if selected else job.get('source_transcript',''),
            execute=execute,through_rowid=None if selected else job.get('target'))
        record(job['config'],run,launch=False,now=now,claimed=job)
    except Exception:
        # Fail closed, no permanent spin on broken configs or unknown exceptions.
        with locked(folder):
            queue=read(folder/'queue.json',{})
            current = queue.get(identity, {})
            if current.get('lease') == job['lease']:
                queue[identity]={**current,'state':'blocked','error_code':'continuation_failed'}
                queue[identity].pop('lease', None)
                queue[identity].pop('ingress_source', None)
                write(folder/'queue.json',queue)
    return True


def project_lock(folder, project):
    return folder / ("runner-" + hashlib.sha256(str(Path(project).resolve()).encode()).hexdigest() + ".lock")


def dispatch(folder):
    """Launch every queued project without waiting for another project's worker."""
    with locked(folder):
        queue = read(folder / 'queue.json', {})
        projects = sorted({str(Path(v['config']).resolve()) for v in queue.values() if v['state'] not in {'blocked', 'dismissed'}})
        source = Path(__file__).resolve().parents[1]
        for project in projects:
            subprocess.Popen([sys.executable, '-B', '-m', 'sessiongraph.continuation',
                '--root', str(folder), '--project', project], cwd=source,
                env={**os.environ, 'PYTHONPATH': str(source), 'PYTHONDONTWRITEBYTECODE': '1'},
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)


def run(folder, project=None):
    if project is None:
        dispatch(folder)
        return
    project = str(Path(project).resolve())
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    with project_lock(folder, project).open('a') as singleton:
        try:fcntl.flock(singleton,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        try:os.nice(10)
        except OSError:pass
        # macOS kqueue wakes only on an atomic queue write or the next due time.
        # There is no periodic scan, model call or CPU loop during retry backoff.
        fd=os.open(folder,os.O_RDONLY)
        watcher=select.kqueue()
        watcher.control([select.kevent(fd,filter=select.KQ_FILTER_VNODE,flags=select.KQ_EV_ADD|select.KQ_EV_CLEAR,fflags=select.KQ_NOTE_WRITE)],0,0)
        try:
            while True:
                with locked(folder):
                    queue=read(folder/'queue.json',{})
                    if release_legacy_waits(queue, time.time()):
                        write(folder/'queue.json',queue)
                    pending=[v for v in queue.values() if v['state'] not in {'blocked', 'dismissed'} and str(Path(v['config']).resolve())==project]
                    if not pending:
                        fcntl.flock(singleton,fcntl.LOCK_UN)
                        return
                    delay=max(0,min(v['due'] for v in pending)-time.time())
                if delay:
                    watcher.control(None,1,delay)
                else:
                    process_one(folder, project=project)
        finally:
            watcher.close()
            os.close(fd)


def main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,default=None)
    parser.add_argument('--project', type=Path, default=None)
    args=parser.parse_args()
    run(args.root or root(), args.project)


if __name__=='__main__':main()
