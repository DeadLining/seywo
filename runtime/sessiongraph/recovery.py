"""Selective write-side recovery with immutable receipts; previews never call providers."""
import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from .storage import exists

REVISION = 'recovery-v1'
SCHEMA = '''CREATE TABLE IF NOT EXISTS sg2_recoveries(
 id TEXT PRIMARY KEY,job TEXT NOT NULL,reason TEXT NOT NULL,created REAL NOT NULL,
 before_json TEXT NOT NULL,proof_id TEXT,automatic_key TEXT UNIQUE,woken INTEGER NOT NULL DEFAULT 0);'''


def proof(store, request_id):
    if not request_id:
        return None
    if not re.fullmatch(r'[a-f0-9]{32}', request_id):
        raise ValueError('invalid recovery request id')
    folder = Path(store.config['database']).parent / 'request-audit'
    path = folder / f'request-{request_id}.json'
    if path.is_symlink():
        raise ValueError('recovery evidence must be project local')
    data = json.loads(path.read_text())
    conf = store.config.get('graph_v2', {}).get('jev', {})
    job = store.db.execute('SELECT project FROM sg2_jobs WHERE id=?', (data.get('job_id'),)).fetchone()
    if (not job or job[0] != store.config['project_id'] or data.get('id') != request_id
        or data.get('provider') != 'jev' or data.get('state') != 'validated'
        or data.get('http_status') != 200
        or data.get('endpoint') != conf.get('endpoint', 'https://api.typesafe.ai/v1/systemone')
        or data.get('model') != conf.get('model', 'jev-latest')):
        raise ValueError('recovery requires a validated Jev request from this project and current endpoint/model')
    stamp = data.get('created_at')
    if not isinstance(stamp, (int, float)) or not 0 <= time.time()-stamp <= 86400:
        raise ValueError('recovery evidence is stale or in the future')
    return data


def plan(store, *, request_id=None, job_ids=None):
    evidence = proof(store, request_id)
    if not exists(store.db, 'sg2_control'):
        return []
    selected = set(job_ids or [])
    if selected:
        owned = {r[0] for r in store.db.execute('SELECT id FROM sg2_jobs WHERE project=?', (store.config['project_id'],))}
        if not selected <= owned:
            raise ValueError('recovery job does not belong to this project')
    results = []
    for row in store.db.execute('''SELECT j.id,j.chat,j.updated_at,c.* FROM sg2_jobs j
      JOIN sg2_control c ON c.job=j.id WHERE j.project=? AND c.state='blocked'
      AND j.state NOT IN ('complete','superseded')
      AND NOT EXISTS (SELECT 1 FROM sg2_attempts a WHERE a.job=j.id AND a.state='running')
      ORDER BY j.updated_at,j.id''', (store.config['project_id'],)):
        if selected and row['id'] not in selected:
            continue
        info = json.loads(row['error_json'] or '{}')
        code = info.get('code')
        reason = None
        if code == 'jev_malformed_response' and info.get('retryable') is False:
            reason = 'retry_policy_changed'
        elif code == 'jev_http_451' and evidence:
            if evidence['created_at'] > datetime.fromisoformat(row['updated_at']).timestamp():
                reason = 'provider_recovered'
        key = f'{REVISION}:{row["id"]}:{code}'
        used = exists(store.db, 'sg2_recoveries') and store.db.execute(
            'SELECT 1 FROM sg2_recoveries WHERE automatic_key=?', (key,)).fetchone()
        results.append(dict(job_id=row['id'], chat=row['chat'], code=code,
            eligible=bool(reason and not used), reason='already_recovered' if used else reason or 'requires_explicit_diagnosis',
            proof_id=evidence['id'] if reason == 'provider_recovered' else None,
            automatic_key=key, before=dict(row)))
    return results


def recover(store, *, request_id=None, job_ids=None, apply=False, launch=True):
    items = plan(store, request_id=request_id, job_ids=job_ids)
    ready = [item for item in items if item['eligible']]
    result = dict(model_calls=0, applied=False, recovered=0,
        candidates=[{k:v for k,v in item.items() if k not in ('before','automatic_key')} for item in items])
    if not apply:
        return result
    store._ensure_writable()
    if not ready and not exists(store.db, 'sg2_recoveries'):
        return result
    store.db.executescript(SCHEMA)
    with store.db:
        for item in ready:
            # Caller owns graph-v2.lock; the predicate additionally rejects stale controls.
            old = item['before']
            n = store.db.execute("UPDATE sg2_control SET state='ready',due=0,failures=0 WHERE job=? AND state='blocked' AND error_json IS ?",
                (item['job_id'], old['error_json'])).rowcount
            if not n:
                continue
            store.db.execute('INSERT INTO sg2_recoveries(id,job,reason,created,before_json,proof_id,automatic_key) VALUES(?,?,?,?,?,?,?)',
                (uuid.uuid4().hex,item['job_id'],item['reason'],time.time(),json.dumps(old,ensure_ascii=False),item['proof_id'],item['automatic_key']))
            result['recovered'] += 1
    result['applied'] = True
    # Preserve error history, inputs, cached validated outputs, claims and coverage.
    # A ready control only grants another attempt; publication still validates everything.
    # Durable outbox: if the process dies between commit and queue write, the next
    # write-side reconciliation completes the wake without reopening the job again.
    pending = store.db.execute('''SELECT r.id,j.chat FROM sg2_recoveries r JOIN sg2_jobs j ON j.id=r.job
      WHERE r.woken=0 AND j.project=?''', (store.config['project_id'],)).fetchall()
    try:
        result['scheduled_chats'] = wake(store, {r['chat'] for r in pending}, launch=launch) if pending else []
    except (OSError, ValueError) as exc:
        result['scheduling_error'] = type(exc).__name__
        result['scheduled_chats'] = []
    with store.db:
        store.db.executemany('UPDATE sg2_recoveries SET woken=1 WHERE id=?',
            [(r['id'],) for r in pending if r['chat'] in result['scheduled_chats']])
    return result


def wake(store, chats, *, launch=True):
    from . import continuation as c
    config_path = store.config.get('_config_path')
    if not config_path or not c.automatic_enabled(config_path, store.config):
        return []
    folder = c.root(store.config)
    with c.locked(folder):
        queue = c.read(folder/'queue.json', {})
        for chat in sorted(chats):
            identity = c.key(config_path, chat)
            prior = queue.get(identity, {})
            target = store.db.execute('''SELECT max(e.rowid) FROM entries e
              JOIN session_chats s ON s.segment_id=e.session_id WHERE s.chat_id=?''', (chat,)).fetchone()[0]
            queue[identity] = {**prior, 'config':str(Path(config_path).resolve()),'chat':chat,
                'target':max(prior.get('target') or 0,target or 0),'run_id':prior.get('run_id',''),
                'updated_at':time.time(),'failures':0,'no_progress':0,'state':'ready','due':0,
                'error_code':'blocked_task_recovered'}
            # Explicit retry is a new scheduling generation, even at the same
            # input boundary. An older in-flight result cannot consume it.
            queue[identity].pop('lease', None)
            queue[identity].pop('ingress_source', None)
        c.write(folder/'queue.json',queue)
        if launch:
            c.start(folder)
    return sorted(chats)
