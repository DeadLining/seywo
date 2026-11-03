"""Explicit, source-checked classification rehearsal; never publishes graph facts.

The immutable run manifest owns cumulative admission budgets. Per-question
receipts live in the existing SQLite database; no second queue or daemon exists.
"""
from __future__ import annotations

from .source_text import hydrate

import fcntl
import hashlib
import json
import math
from pathlib import Path
import time

from .failures import Failure, describe
from .graph_contract import fingerprint
from .request_audit import scrub
from .single_token_judge import SingleTokenJudge

SCHEMA = '''
CREATE TABLE IF NOT EXISTS sg_classifier_runs(
 project TEXT NOT NULL,id TEXT NOT NULL,manifest_hash TEXT NOT NULL,budget_json TEXT NOT NULL,
 requests INTEGER NOT NULL DEFAULT 0,input_bytes INTEGER NOT NULL DEFAULT 0,
 seconds REAL NOT NULL DEFAULT 0,PRIMARY KEY(project,id));
CREATE TABLE IF NOT EXISTS sg_classifier_results(
 project TEXT NOT NULL,run_id TEXT NOT NULL,question TEXT NOT NULL,key TEXT NOT NULL,
 state TEXT NOT NULL,result_json TEXT,error_json TEXT,request_id TEXT,
 PRIMARY KEY(project,run_id,question));
'''


def invalid(reason):
    return Failure('classifier_shadow_' + reason, retryable=False, stage='classifier_shadow')


def _sources(store, references, cache=None):
    if not isinstance(references, list) or not 1 <= len(references) <= 8:
        raise invalid('evidence')
    sources = []
    cache = {} if cache is None else cache
    for ref in references:
        if not isinstance(ref, dict) or set(ref) != {'entry_id', 'source_hash', 'start', 'end'}:
            raise invalid('source_reference')
        if (not isinstance(ref['entry_id'], str) or not isinstance(ref['source_hash'], str)
                or type(ref['start']) is not int or type(ref['end']) is not int):
            raise invalid('source_reference')
        if ref['entry_id'] not in cache:
            size = store.db.execute('SELECT sg_source_bytes(id,text) FROM entries WHERE id=?', (ref['entry_id'],)).fetchone()
            if not size or size[0] is None:
                raise invalid('source_changed')
            if cache.get(None, 0) + size[0] > 8 * 1024 * 1024:
                raise invalid('source_read_budget')
            cache[None] = cache.get(None, 0) + size[0]
            row = store.db.execute('SELECT id,role,timestamp,text FROM entries WHERE id=? AND sg_source_bytes(id,text)<=?',
                                   (ref['entry_id'], size[0])).fetchone()
            try:
                row = hydrate(store, row)
            except (ValueError, OSError):
                raise invalid('source_changed')
            cache[ref['entry_id']] = (row, hashlib.sha256(row['text'].encode()).hexdigest() if row else None)
        row, source_hash = cache[ref['entry_id']]
        if not row or source_hash != ref['source_hash']:
            raise invalid('source_changed')
        if not 0 <= ref['start'] < ref['end'] <= len(row['text']):
            raise invalid('source_range')
        sources.append({**ref, 'role': row['role'], 'timestamp': row['timestamp'],
                        'text': row['text'][ref['start']:ref['end']]})
    return sources


def prepare(store, manifest, client):
    if (not isinstance(manifest, dict) or set(manifest) != {'project_id', 'run_id', 'budget', 'questions'}
            or manifest['project_id'] != store.config['project_id']):
        raise invalid('project_or_manifest')
    run_id = manifest['run_id']
    if not isinstance(run_id, str) or not run_id or len(run_id) > 64 or not all(c.isascii() and (c.isalnum() or c in '_-') for c in run_id):
        raise invalid('run_id')
    budget = manifest['budget']
    if not isinstance(budget, dict) or set(budget) != {'max_requests', 'max_input_bytes', 'max_seconds'}:
        raise invalid('budget')
    if (type(budget['max_requests']) is not int or not 1 <= budget['max_requests'] <= 32
            or type(budget['max_input_bytes']) is not int or not 1 <= budget['max_input_bytes'] <= 2 * 1024 * 1024
            or type(budget['max_seconds']) not in (int, float) or not math.isfinite(budget['max_seconds'])
            or not 1 <= budget['max_seconds'] <= 300):
        raise invalid('budget')
    questions = manifest['questions']
    if not isinstance(questions, list) or not 1 <= len(questions) <= 32:
        raise invalid('questions')
    prepared, seen, source_cache = [], set(), {}
    for item in questions:
        if not isinstance(item, dict) or set(item) != {'id', 'question', 'state', 'evidence'}:
            raise invalid('question')
        identity = item['id']
        if not isinstance(identity, str) or not identity or len(identity) > 128 or identity in seen:
            raise invalid('question_id')
        seen.add(identity)
        references = item['evidence']
        sources = _sources(store, references, source_cache)
        state = {'candidate': item['state'], 'original_sources': sources}
        # Refuse known secrets rather than silently classify altered evidence.
        if scrub(state) != state or scrub(item['question']) != item['question']:
            raise invalid('sensitive_input')
        packet = client.prepare(state, item['question'])
        packet['source_refs'] = references
        packet['source_snapshot'] = fingerprint(sources)
        prepared.append((identity, packet))
    # Request identity includes routing, sampling, exact evidence and labels.
    digest = fingerprint([manifest, client.cache_identity, [p['key'] for _, p in prepared]])
    return prepared, digest


def run(store, manifest, *, allow_network=False, call_budget=3, client=None):
    if type(call_budget) is not int or not 1 <= call_budget <= 3:
        raise invalid('call_budget')
    client = client or SingleTokenJudge(store.config)
    prepared, digest = prepare(store, manifest, client)
    report = dict(mode='shadow', run_id=manifest['run_id'], manifest_hash=digest,
                  state='preview', model_calls=0, results={}, budget=manifest['budget'],
                  planned_request_bytes=sum(len(p['body']) for _, p in prepared),
                  publication_enabled=False, automatic_retry=False,
                  timing_scope='cumulative measured time and admission; socket timeout is not a hard wall deadline')
    if not allow_network:
        # No schema setup, audit writes, credential loading or network on preview.
        return report
    if store.readonly:
        raise invalid('readonly')
    project, run_id = store.config['project_id'], manifest['run_id']
    lock_path = Path(store.config['database']).parent / ('classifier-' + fingerprint([project, run_id]) + '.lock')
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise invalid('run_active') from None
        store.db.executescript(SCHEMA)
        with store.db:
            old = store.db.execute('SELECT manifest_hash FROM sg_classifier_runs WHERE project=? AND id=?', (project, run_id)).fetchone()
            if old and old[0] != digest:
                raise invalid('manifest_changed')
            store.db.execute('INSERT OR IGNORE INTO sg_classifier_runs(project,id,manifest_hash,budget_json) VALUES(?,?,?,?)',
                             (project, run_id, digest, json.dumps(manifest['budget'], sort_keys=True)))
        report['state'] = 'complete'
        for question_id, packet in prepared:
            prior = store.db.execute('SELECT * FROM sg_classifier_results WHERE project=? AND run_id=? AND question=?',
                                     (project, run_id, question_id)).fetchone()
            if prior and prior['key'] != packet['key']:
                raise invalid('checkpoint_changed')
            if prior and prior['state'] == 'complete':
                report['results'][question_id] = {'checkpoint_replay': True, **json.loads(prior['result_json'])}
                continue
            if prior and prior['state'] == 'failed' and not json.loads(prior['error_json'])['retryable']:
                report.update(state='blocked', failure=json.loads(prior['error_json']))
                break
            if report['model_calls'] >= call_budget:
                report['state'] = 'yielded'
                break
            spent = store.db.execute('SELECT requests,input_bytes,seconds FROM sg_classifier_runs WHERE project=? AND id=?',
                                     (project, run_id)).fetchone()
            budget = manifest['budget']
            remaining = budget['max_seconds'] - spent['seconds']
            request_bytes = len(packet['body'])
            if (spent['requests'] >= budget['max_requests']
                    or spent['input_bytes'] + request_bytes > budget['max_input_bytes'] or remaining < 1):
                report['state'] = 'budget_exhausted'
                break
            reservation = min(client.timeout, remaining)
            # Charge before I/O. A crash consumes its reservation, so restarting
            # or yielding cannot reset cumulative cost or grant free retries.
            with store.db:
                store.db.execute('UPDATE sg_classifier_runs SET requests=requests+1,input_bytes=input_bytes+?,seconds=seconds+? WHERE project=? AND id=?',
                                 (request_bytes, reservation, project, run_id))
                store.db.execute('INSERT OR REPLACE INTO sg_classifier_results VALUES(?,?,?,?,?,NULL,NULL,NULL)',
                                 (project, run_id, question_id, packet['key'], 'running'))
            start = time.monotonic()
            original_timeout = client.timeout
            client.timeout = reservation
            result, info = None, None
            before_calls = client.calls
            try:
                result = client.classify(packet)
                result['source_refs'] = packet['source_refs']
                # A shadow result may describe an older snapshot, but it must not
                # be checkpointed as current after a concurrent source rewrite.
                if fingerprint(_sources(store, packet['source_refs'])) != packet['source_snapshot']:
                    raise invalid('source_changed')
            except Failure as exc:
                info = describe(exc, 'classifier_shadow')
                result = None
            finally:
                client.timeout = original_timeout
            elapsed = time.monotonic() - start
            request_id = client.traces[-1]['id'] if client.traces else None
            with store.db:
                store.db.execute('UPDATE sg_classifier_runs SET seconds=seconds-?+? WHERE project=? AND id=?',
                                 (reservation, elapsed, project, run_id))
                store.db.execute('UPDATE sg_classifier_results SET state=?,result_json=?,error_json=?,request_id=? WHERE project=? AND run_id=? AND question=?',
                                 ('failed' if info else 'complete', json.dumps(result) if result is not None else None,
                                  json.dumps(info) if info else None, request_id, project, run_id, question_id))
            report['model_calls'] += max(0, client.calls - before_calls)
            if info:
                report.update(state='retry_wait' if info['retryable'] else 'blocked', failure=info)
                break
            report['results'][question_id] = {'checkpoint_replay': False, **result}
        spent = store.db.execute('SELECT requests,input_bytes,seconds FROM sg_classifier_runs WHERE project=? AND id=?',
                                 (project, run_id)).fetchone()
        if report['state'] == 'retry_wait' and (spent['requests'] >= manifest['budget']['max_requests']
                                               or spent['seconds'] >= manifest['budget']['max_seconds']):
            report['state'] = 'budget_exhausted'
        report['cumulative'] = dict(spent)
        report['pending_questions'] = len(prepared) - len(report['results'])
        return report
