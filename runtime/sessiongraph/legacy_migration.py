"""Explicit bounded source replay, not an assertion of old/new equivalence.

The normal graph pipeline owns extraction, identity, revision and edge validation.
Only references are stored here; old objects/edges and live input cursors survive.
"""
from .source_text import hydrate
from contextlib import ExitStack
import json
import os
from pathlib import Path

from . import graph_v2 as graph, input_pipeline, revision_ledger as ledger
from .core import Store, load_config
from .graph_contract import ATOMIC_SOURCE_REVISION, atomic_key, fingerprint
from .stage_leases import locked
from .storage import exists

VERSION = 'legacy-source-replay-v1'
TABLE = 'sg_legacy_migrations'
SCHEMA = '''CREATE TABLE IF NOT EXISTS sg_legacy_migrations(
 project TEXT NOT NULL,node TEXT NOT NULL REFERENCES sg2_nodes(id),
 job TEXT NOT NULL REFERENCES sg2_jobs(id),evidence TEXT NOT NULL REFERENCES sg2_evidence(id),
 snapshot TEXT NOT NULL,created TEXT NOT NULL,PRIMARY KEY(project,node));
 CREATE INDEX IF NOT EXISTS sg_legacy_migrations_job ON sg_legacy_migrations(job,node);'''


def _selection(store, node_ids, limit, after):
    if (type(limit) is not int or not 1 <= limit <= 16 or not isinstance(after, str)
            or not isinstance(node_ids, (list, tuple)) or len(node_ids) > 16
            or any(not isinstance(n, str) or not n for n in node_ids)):
        raise ValueError('invalid bounded legacy migration selection')
    if node_ids and after:
        raise ValueError('node_ids and after are mutually exclusive')
    if not graph.exists(store):
        return []
    where, args = 'n.project=? AND n.type IN (\'Claim\',\'Decision\')', [store.config['project_id']]
    if node_ids:
        where += ' AND n.id IN (' + ','.join('?' for _ in node_ids) + ')'
        args.extend(node_ids)
    else:
        where += ' AND n.id>?'
        args.append(after)
    rows = store.db.execute(f'''SELECT n.id,n.type,m.title,m.content,m.status,
        m.source_entry_id,m.source_quote FROM sg2_nodes n JOIN work_memory m ON m.id=n.id
        WHERE {where} ORDER BY n.id LIMIT ?''', (*args, len(set(node_ids)) if node_ids else limit)).fetchall()
    if node_ids and {r['id'] for r in rows} != set(node_ids):
        raise ValueError('selected legacy nodes must belong to this project and be Claim/Decision')
    return rows


def _candidate(store, row, max_chars):
    result = dict(node_id=row['id'], eligible=False, reason='baseline_unavailable')
    evidence = store.db.execute('''SELECT id,assertion_json FROM sg2_evidence
        WHERE node=? AND entry_id=? AND quote=? ORDER BY id LIMIT 65''',
        (row['id'], row['source_entry_id'], row['source_quote'])).fetchall()
    exact = next((e for e in evidence if all(json.loads(e['assertion_json']).get(k) == row[k]
                 for k in ('type', 'title', 'content', 'status'))), None)
    if exact is None:
        return result
    payload = json.loads(exact['assertion_json'])
    if atomic_key(payload) is not None:
        return {**result, 'reason': 'already_atomic'}
    snapshot = fingerprint([row['id'], exact['id'], exact['assertion_json']])
    prior = store.db.execute('SELECT * FROM sg_legacy_migrations WHERE project=? AND node=?',
        (store.config['project_id'], row['id'])).fetchone() if exists(store.db, TABLE) else None
    if prior:
        if snapshot != prior['snapshot']:
            return {**result, 'reason': 'legacy_snapshot_changed'}
    size = store.db.execute('SELECT sg_source_bytes(id,text) FROM entries WHERE id=?',
                            (row['source_entry_id'],)).fetchone()
    if not size or size[0] > 8 * 1024 * 1024:
        return {**result, 'reason': 'source_missing_or_oversize'}
    if not exists(store.db, 'sg_source_locations') or not store.db.execute(
            'SELECT 1 FROM sg_source_locations WHERE entry_id=?', (row['source_entry_id'],)).fetchone():
        return {**result, 'reason': 'source_metadata_missing'}
    try:
        source = hydrate(store, store.db.execute('SELECT * FROM entries WHERE id=?', (row['source_entry_id'],)).fetchone())
    except (ValueError, OSError):
        return {**result, 'reason': 'source_changed'}
    quote = row['source_quote']
    at = source['text'].find(quote) if quote else -1
    if at < 0:
        return {**result, 'reason': 'source_changed'}
    if prior:
        job = store.db.execute('SELECT input_json FROM sg2_jobs WHERE id=?', (prior['job'],)).fetchone()
        old = json.loads(job['input_json'])['entries'][0]
        if old['source_hash'] != graph.digest(source['text']) or old['source_metadata_hash'] != ledger.metadata(source):
            return {**result, 'reason': 'source_changed'}
        return {**result, 'eligible': True, 'reason': 'existing_replay', 'job_id': prior['job']}
    if len(quote) + 400 > max_chars:
        return {**result, 'reason': 'source_window_oversize'}
    lower, upper = max(0, at - 200), min(len(source['text']), at + len(quote) + 200)
    safe, _ = input_pipeline.projection(store, source)
    spans = [[max(lower, a), min(upper, b)] for a, b in safe if max(lower, a) < min(upper, b)]
    if not spans or input_pipeline.subtract([[at, at + len(quote)]], spans):
        return {**result, 'reason': 'source_excluded'}
    desc = input_pipeline.row_descriptor(source, spans)
    desc['source_metadata_hash'] = ledger.metadata(source)
    job_id = fingerprint([VERSION, store.config['project_id'], desc])
    return dict(result, eligible=True, reason='ready', job_id=job_id, evidence_id=exact['id'],
                snapshot=snapshot, descriptor=desc, segment_id=source['session_id'],
                chat_id=store.chat_id(source['session_id']), selected_chars=sum(b-a for a,b in spans))


def plan(store, *, node_ids=(), limit=1, after='', max_chars=8000):
    if type(max_chars) is not int or not 512 <= max_chars <= 16000:
        raise ValueError('migration max_chars must be 512..16000')
    if not ledger.effective(store.config):
        raise ValueError('legacy migration requires effective revision policy')
    rows = _selection(store, node_ids, limit, after)
    return [_candidate(store, row, max_chars) for row in rows]


def details(store, node_id):
    if not exists(store.db, TABLE):
        return None
    row = store.db.execute('''SELECT m.job,j.state,c.state AS control_state,j.error
        FROM sg_legacy_migrations m JOIN sg2_jobs j ON j.id=m.job
        LEFT JOIN sg2_control c ON c.job=j.id WHERE m.project=? AND m.node=?''',
        (store.config['project_id'], node_id)).fetchone()
    if row is None:
        return None
    nodes = [r[0] for r in store.db.execute('SELECT DISTINCT node FROM sg2_evidence WHERE job=? ORDER BY node LIMIT 33', (row['job'],))]
    relations = [r[0] for r in store.db.execute('SELECT DISTINCT relation FROM sg2_relation_evidence WHERE job=? ORDER BY relation LIMIT 33', (row['job'],))]
    return dict(job_id=row['job'], state='source_reprocessed' if row['state']=='complete' else row['control_state'] or row['state'],
                error_code=row['error'], source_nodes=nodes[:32], source_relations=relations[:32],
                has_more=len(nodes)>32 or len(relations)>32, equivalence_proven=False,
                legacy_preserved=True, scope='primary_source_window')


def _freeze(store, items):
    store.db.executescript(SCHEMA)
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        for item in items:
            if not item['eligible'] or item['reason'] == 'existing_replay':
                continue
            batch = graph._restore_batch(store, [item['descriptor']])
            offered = [n for n in graph.candidates(store, '\n'.join(e['text'] for e in batch))
                       if atomic_key(n) is not None]
            desc = dict(entries=[item['descriptor']], end=[0, 0], manual_only=True,
                        migration_revision=VERSION, atomic_source_revision=ATOMIC_SOURCE_REVISION)
            timestamp = graph.now()
            store.db.execute('INSERT OR IGNORE INTO sg2_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (item['job_id'],store.config['project_id'],item['chat_id'],item['segment_id'],
                 'pending',graph.dumps(desc),None,graph.dumps(offered),graph.dumps(graph._stats()),
                 None,timestamp,timestamp))
            store.db.execute('INSERT OR IGNORE INTO sg_legacy_migrations VALUES(?,?,?,?,?,?)',
                (store.config['project_id'], item['node_id'], item['job_id'], item['evidence_id'],
                 item['snapshot'], timestamp))


def _refresh_contract(store, items):
    """Explicit, once-per-contract replay; never reset cumulative job resources."""
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        for job_id in sorted({i['job_id'] for i in items if i['eligible']}):
            job = store.db.execute('SELECT state,input_json FROM sg2_jobs WHERE id=? AND project=?',
                                   (job_id, store.config['project_id'])).fetchone()
            desc = json.loads(job['input_json'])
            if (job['state'] != 'complete' or not desc.get('manual_only')
                    or desc.get('migration_revision') != VERSION
                    or desc.get('atomic_source_revision') == ATOMIC_SOURCE_REVISION):
                continue
            graph._restore_batch(store, desc['entries'])
            control = store.db.execute('SELECT state FROM sg2_control WHERE job=?', (job_id,)).fetchone()
            if control and control['state'] not in {'complete', 'ready'}:
                raise ValueError('migration refresh cannot override a blocked retry control')
            desc['atomic_source_revision'] = ATOMIC_SOURCE_REVISION
            store.db.execute("UPDATE sg2_jobs SET state='pending',input_json=?,output_json=NULL,error=NULL,updated_at=? WHERE id=?",
                             (graph.dumps(desc), graph.now(), job_id))


def run(config_path, *, node_ids=(), limit=1, after='', max_chars=8000,
        apply=False, allow_network=False, call_budget=3, extractor=None, judge=None,
        refresh_contract=False):
    if allow_network and not apply:
        raise ValueError('network migration requires --apply')
    if type(call_budget) is not int or not 1 <= call_budget <= 16:
        raise ValueError('migration call_budget must be 1..16')
    if refresh_contract and (not apply or not node_ids):
        raise ValueError('contract refresh requires --apply and explicit --node-id selections')
    config = load_config(Path(config_path))
    with ExitStack() as stack:
        runtime_lock = os.environ.get('SESSIONGRAPH_WORKER_LOCK')
        if apply and runtime_lock:
            stack.enter_context(locked(Path(runtime_lock), shared=True))
        if apply:
            root = Path(config['database']).parent
            with locked(root/'stage-activity.lock'), locked(root/'graph-v2.lock'):
                store = Store(config)
                stack.callback(store.close)
                graph.setup(store)
                items = plan(store,node_ids=node_ids,limit=limit,after=after,max_chars=max_chars)
                _freeze(store, items)
                if refresh_contract:
                    _refresh_contract(store, items)
        else:
            store = Store(config, readonly=True)
            stack.callback(store.close)
            with store.db:
                store.db.execute('BEGIN')
                items = plan(store,node_ids=node_ids,limit=limit,after=after,max_chars=max_chars)
        results, calls = [], 0
        if allow_network:
            from .extraction import create_extractor
            worker = extractor or create_extractor(config)
            jobs = sorted({item['job_id'] for item in items if item['eligible']})
            for job_id in jobs:
                if calls >= call_budget:
                    break
                job = store.db.execute('SELECT chat FROM sg2_jobs WHERE id=? AND project=?',
                                       (job_id, config['project_id'])).fetchone()
                outcome = graph.run_chat(store,worker,job['chat'],pending_only=True,
                    job_ids=[job_id],step_budget=1,call_budget=call_budget-calls,
                    respect_schedule=True,jev=judge)
                calls += outcome['model_calls']
                results.append(dict(job_id=job_id,model_calls=outcome['model_calls'],
                    failure_codes=outcome['failure_codes'],saved_nodes=outcome['saved'],
                    saved_relations=outcome['saved_relations']))
        public = [{k:v for k,v in item.items() if k not in {'descriptor','snapshot'}} for item in items]
        for item in public:
            item['migration'] = details(store,item['node_id'])
        return dict(version=VERSION, applied=apply, model_calls=calls, candidates=public,
                    runs=results,next_after=items[-1]['node_id'] if items else None,
                    legacy_preserved=True,live_cursors_unchanged=True,
                    note='Source replay is not proof of complete legacy equivalence; no old node or relation is hidden or deleted.')
