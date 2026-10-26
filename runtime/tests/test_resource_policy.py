"""Synthetic admission tests use committed stages and the shared audited transport."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from urllib.request import Request

import pytest

from sessiongraph import graph_v2 as graph, resource_policy as resources, stages
from sessiongraph import request_audit
from sessiongraph.failures import Failure, describe
from test_graph_v2 import output
from test_memories import make_store, entries, FakeExtractor


def enable(config, **overrides):
    config.setdefault('graph_v2', {}).update(enabled=True, recovery_policy={'enabled': True},
        resource_policy=dict(enabled=True, max_requests=12, max_input_bytes=1024 * 1024,
            max_output_tokens=65536, max_transport_seconds=180,
            circuit_failure_threshold=2, circuit_cooldown_seconds=120, **overrides))


def add_job(store, job='job'):
    with store.db:
        store.db.execute('INSERT INTO sg2_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (job, store.config['project_id'], 'chat-1', 'segment', 'pending', '{}', None,
             '[]', '{}', None, 'now', 'now'))


@pytest.fixture
def env(tmp_path):
    _, store, _ = make_store(tmp_path, [('user', 'synthetic-source-private')])
    enable(store.config)
    graph.setup(store)
    add_job(store)
    client = SimpleNamespace(config=store.config, key='synthetic-secret', model='fixture',
        max_output=10, timeout=30, traces=[], calls=0, provider_name='fixture')
    yield store, client
    store.close()


def req(cap=10, **fields):
    body = {'model': 'fixture', 'input': 'synthetic-source-private', **fields}
    if cap is not None:
        body['max_output_tokens'] = cap
    return Request('http://127.0.0.1:4317/v1/responses', data=json.dumps(body).encode(),
                   headers={'Authorization': 'Bearer synthetic-secret'})


def run_stage(store, operation, *, job='job', stage='extract', part='part'):
    return stages.execute(store, job, part, stage, operation, {}, cache=False)


def reserve(store, client, *, request=None, now=100, **scope):
    return run_stage(store, lambda: resources.reserve(client, request or req(),
        resources.settings(client.config), now=now), **scope)


def spent(store, job='job'):
    return dict(store.db.execute('SELECT * FROM sg_resource_scopes WHERE job=?', (job,)).fetchone())


def circuit(store):
    return dict(store.db.execute('SELECT * FROM sg_resource_circuits').fetchone())


def finish(store, ticket, *, failed=False, elapsed=1, now=100):
    ticket.update(sent=True, received=not failed, transport_failure=failed)
    resources.settle(store.config, ticket, elapsed, now=now)


def transport(monkeypatch, result=b'{"status":"completed","output":[]}', callback=None):
    seen = []
    class Response:
        status = 200
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return result
    class Opener:
        def open(self, request, timeout):
            seen.append((request, timeout))
            if callback:
                callback()
            return Response()
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: Opener())
    return seen


def test_disabled_transport_has_no_resource_schema(env, monkeypatch):
    store, client = env
    store.config['graph_v2'].pop('resource_policy')
    seen = transport(monkeypatch)
    assert request_audit.read_response(client, req(), [])
    assert len(seen) == 1 and seen[0][1] == 30
    assert not store.db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'sg_resource_%'").fetchall()


@pytest.mark.parametrize('change', [
    {'max_requests': True}, {'max_requests': 0}, {'max_input_bytes': 67108865},
    {'max_output_tokens': 1.5}, {'max_transport_seconds': 0},
    {'circuit_failure_threshold': 17}, {'circuit_cooldown_seconds': 3601},
    {'extra': 1}, {'enabled': 'yes'},
])
def test_strict_policy_rejects_before_network(env, monkeypatch, change):
    store, client = env
    store.config['graph_v2']['resource_policy'].update(change)
    seen = transport(monkeypatch)
    with pytest.raises(resources.AdmissionDenied, match='resource_policy_invalid'):
        run_stage(store, lambda: request_audit.read_response(client, req(), []))
    assert not seen


@pytest.mark.parametrize('mode', ['missing_limit', 'no_graph', 'no_recovery', 'no_scope', 'bad_stage', 'uncommitted', 'wrong_project', 'finished'])
def test_admission_requires_bounded_graph_and_live_committed_scope(env, mode):
    store, client = env
    request = req()
    if mode == 'missing_limit': request = req(None)
    if mode == 'no_graph': store.config['graph_v2']['enabled'] = False
    if mode == 'no_recovery': store.config['graph_v2']['recovery_policy'] = {'enabled': False}
    if mode == 'wrong_project': client.config = {**store.config, 'project_id': 'different-project'}
    if mode == 'finished':
        with store.db: store.db.execute("UPDATE sg2_jobs SET state='complete'")
    def operation():
        if mode == 'uncommitted':
            token = stages.CURRENT.set({**stages.CURRENT.get(), 'attempt_id': 'uncommitted'})
            try:
                return resources.reserve(client, request, resources.settings(client.config))
            finally:
                stages.CURRENT.reset(token)
        return resources.reserve(client, request, resources.settings(client.config))
    with pytest.raises(resources.AdmissionDenied):
        if mode == 'no_scope': operation()
        else: run_stage(store, operation, stage='publish_nodes' if mode == 'bad_stage' else 'extract')


@pytest.mark.parametrize('limit', ['max_requests', 'max_input_bytes', 'max_output_tokens', 'max_transport_seconds'])
def test_each_cumulative_budget_survives_stage_part_and_backend_changes(env, limit):
    store, client = env
    value = {'max_requests': 1, 'max_input_bytes': len(req().data),
             'max_output_tokens': 10, 'max_transport_seconds': 1}[limit]
    store.config['graph_v2']['resource_policy'][limit] = value
    first = reserve(store, client)
    finish(store, first, elapsed=1)
    client.provider_name, client.model = 'different-backend', 'different-model'
    before = spent(store)
    with pytest.raises(resources.AdmissionDenied, match='resource_budget_exhausted'):
        reserve(store, client, part='repair-or-split-part', stage='relations')
    assert spent(store) == before
    assert first['reserved_seconds'] == (1 if limit == 'max_transport_seconds' else 30)


def test_policy_change_cannot_renew_existing_job(env):
    store, client = env
    reserve(store, client)
    store.config['graph_v2']['resource_policy']['max_requests'] += 1
    with pytest.raises(resources.AdmissionDenied, match='resource_policy_changed'):
        reserve(store, client)
    assert spent(store)['requests'] == 1


def test_other_job_has_own_budget_but_circuit_rules_are_frozen(env):
    store, client = env
    store.config['graph_v2']['resource_policy']['max_requests'] = 1
    ticket = reserve(store, client)
    finish(store, ticket)
    add_job(store, 'other-job')
    reserve(store, client, job='other-job')
    assert spent(store, 'other-job')['requests'] == 1
    add_job(store, 'third-job')
    store.config['graph_v2']['resource_policy']['circuit_failure_threshold'] = 3
    with pytest.raises(resources.AdmissionDenied, match='resource_circuit_policy_changed'):
        reserve(store, client, job='third-job')


def test_settlement_idempotent_usage_has_no_fabricated_numbers_or_payloads(env):
    store, client = env
    ticket = reserve(store, client)
    resources.usage(ticket, {'prompt_tokens': 7, 'completion_tokens': 3, 'secret': client.key})
    finish(store, ticket, elapsed=2)
    finish(store, ticket, elapsed=99)
    assert spent(store)['seconds'] == 2 and spent(store)['output_tokens'] == 3
    record = dict(store.db.execute('SELECT * FROM sg_resource_requests').fetchone())
    assert json.loads(record['usage_json']) == {'input_tokens': 7, 'output_tokens': 3}
    text = json.dumps([dict(r) for r in store.db.execute('SELECT * FROM sg_resource_scopes')]) + json.dumps(record) + json.dumps(circuit(store))
    assert 'synthetic-source-private' not in text and client.key not in text
    empty = reserve(store, client)
    resources.usage(empty, {'input_tokens': True, 'output_tokens': -1, 'total_tokens': 42})
    finish(store, empty)
    assert empty['usage'] is None


def test_successful_usage_releases_output_reservation_for_next_stage(env):
    store, client = env
    store.config['graph_v2']['resource_policy']['max_output_tokens'] = 15
    first = reserve(store, client)
    resources.usage(first, {'output_tokens': 3})
    finish(store, first)
    second = reserve(store, client, stage='relations')
    assert spent(store)['output_tokens'] == 13
    resources.usage(second, {'output_tokens': 2})
    finish(store, second)
    finish(store, first)
    assert spent(store)['output_tokens'] == 5
    assert spent(store)['requests'] == 2
    with pytest.raises(resources.AdmissionDenied, match='resource_budget_exhausted'):
        reserve(store, client, request=req(11))


@pytest.mark.parametrize('failure', ['transport', 'provider'])
def test_failed_response_cannot_refund_output_reservation(env, failure):
    store, client = env
    ticket = reserve(store, client)
    resources.usage(ticket, {'output_tokens': 3})
    if failure == 'provider':
        ticket['code'] = 'provider_http_503'
    finish(store, ticket, failed=failure == 'transport')
    assert spent(store)['output_tokens'] == 10


def test_provider_output_overshoot_is_charged_and_trips_circuit(env, monkeypatch):
    store, client = env
    store.config['graph_v2']['resource_policy']['circuit_failure_threshold'] = 1
    seen = transport(monkeypatch, b'{"usage":{"input_tokens":4,"output_tokens":11}}')
    with pytest.raises(Failure, match='resource_provider_budget_violation'):
        run_stage(store, lambda: request_audit.read_response(client, req(), []))
    assert len(seen) == 1 and spent(store)['output_tokens'] == 11
    assert circuit(store)['failures'] == 1
    audit = json.loads(Path(client.traces[-1]['path']).read_text())
    assert audit['state'] == 'response_failed'


def test_reservation_is_visible_without_holding_transaction_during_io(env, monkeypatch):
    store, client = env
    def callback():
        other = sqlite3.connect(store.config['database'], timeout=.1)
        try:
            other.execute('BEGIN IMMEDIATE')
            assert other.execute('SELECT requests FROM sg_resource_scopes').fetchone()[0] == 1
            assert other.execute('SELECT state FROM sg_resource_requests').fetchone()[0] == 'reserved'
        finally:
            other.rollback()
            other.close()
    transport(monkeypatch, callback=callback)
    run_stage(store, lambda: request_audit.read_response(client, req(), []))
    assert store.db.execute('SELECT state FROM sg_resource_requests').fetchone()[0] == 'received'


def test_concurrent_reservations_cannot_exceed_budget(env):
    store, client = env
    store.config['graph_v2']['resource_policy'].update(max_requests=3, max_transport_seconds=300)
    def operation():
        context = copy.deepcopy(stages.CURRENT.get())
        def worker(_):
            token = stages.CURRENT.set(context)
            try:
                try:
                    return resources.reserve(client, req(), resources.settings(client.config))['id']
                except resources.AdmissionDenied as exc:
                    assert exc.code == 'resource_budget_exhausted'
                    return None
            finally:
                stages.CURRENT.reset(token)
        with ThreadPoolExecutor(max_workers=8) as pool:
            return list(pool.map(worker, range(8)))
    assert sum(x is not None for x in run_stage(store, operation)) == 3
    assert spent(store)['requests'] == 3 and spent(store)['seconds'] == 90


def test_process_exit_preserves_reservation_across_restart(env):
    store, client = env
    store.config['graph_v2']['resource_policy']['max_requests'] = 1
    def operation():
        script = '''import json, os, sys
from types import SimpleNamespace
from urllib.request import Request
from sessiongraph import resource_policy as r, stages
config, context = json.loads(sys.argv[1]), json.loads(sys.argv[2])
stages.CURRENT.set(context)
client = SimpleNamespace(config=config, model='fixture', timeout=30)
r.reserve(client, Request('http://127.0.0.1/v1/responses', data=b'{"max_tokens":10}'), r.settings(config))
os._exit(23)
'''
        result = subprocess.run([sys.executable, '-c', script, json.dumps(store.config),
            json.dumps(stages.CURRENT.get())], timeout=15, capture_output=True)
        assert result.returncode == 23, result.stderr.decode()
    run_stage(store, operation)
    assert spent(store)['seconds'] == 30
    assert store.db.execute('SELECT state FROM sg_resource_requests').fetchone()[0] == 'reserved'
    with pytest.raises(resources.AdmissionDenied, match='resource_budget_exhausted'):
        reserve(store, client)


def test_circuit_cooldown_single_probe_and_stale_generation_fence(env, monkeypatch):
    store, client = env
    tickets = [reserve(store, client) for _ in range(3)]
    finish(store, tickets[0], failed=True)
    finish(store, tickets[1], failed=True)
    assert circuit(store)['open_until'] == 220
    before = spent(store)
    with pytest.raises(resources.AdmissionDenied) as error:
        reserve(store, client, now=150)
    assert error.value.code == 'resource_circuit_open' and error.value.retry_at == 220
    assert spent(store) == before
    probe = reserve(store, client, now=221)
    assert probe['probe'] and probe['generation'] > tickets[2]['generation']
    with pytest.raises(resources.AdmissionDenied, match='resource_circuit_open'):
        reserve(store, client, now=221)
    finish(store, tickets[2], now=222)
    assert circuit(store)['probe'] == probe['id']
    monkeypatch.setattr(resources, '_alive', lambda pid: False)
    replacement = reserve(store, client, now=222)
    finish(store, probe, failed=True, now=223)
    assert circuit(store)['probe'] == replacement['id']
    finish(store, replacement, now=224)
    assert circuit(store)['failures'] == 0 and circuit(store)['probe'] is None


def test_failed_half_open_probe_reopens(env):
    store, client = env
    store.config['graph_v2']['resource_policy']['circuit_failure_threshold'] = 1
    finish(store, reserve(store, client), failed=True)
    probe = reserve(store, client, now=221)
    finish(store, probe, failed=True, now=222)
    assert circuit(store)['open_until'] == 342


def test_circuit_wait_preserves_retry_debt_and_checkpoint(env):
    store, client = env
    stages.execute(store, 'job', 'part', 'identity', lambda: {'bound': 'object'}, {})
    original = dict(code='provider_timeout', retryable=True, stage='relations',
                    retry_scopes={'old-scope': 2})
    with store.db:
        store.db.execute('INSERT INTO sg2_control VALUES(?,?,?,?,?)',
                         ('job', 'retry_wait', 0, 2, json.dumps(original)))
    finish(store, reserve(store, client), failed=True)
    finish(store, reserve(store, client), failed=True)
    with pytest.raises(resources.AdmissionDenied) as error:
        reserve(store, client, stage='relations')
    info = describe(error.value)
    with store.db:
        stages.fail(store, 'job', info, scope=error.value.retry_scope)
    control = dict(store.db.execute('SELECT * FROM sg2_control').fetchone())
    assert control['state'] == 'retry_wait' and control['failures'] == 2
    assert json.loads(control['error_json'])['retry_scopes'] == {'old-scope': 2}
    assert control['due'] >= error.value.retry_at
    assert store.db.execute("SELECT state FROM sg2_attempts ORDER BY rowid DESC LIMIT 1").fetchone()[0] == 'deferred'
    assert store.db.execute("SELECT result_json FROM sg2_checkpoints WHERE stage='identity'").fetchone()


def test_model_judge_denial_counts_no_network_and_does_not_rewrite_old_trace(env, monkeypatch):
    from sessiongraph.model_judge import ModelJudge
    from test_model_judge import QUESTIONS
    store, _ = env
    store.config['provider'].update(protocol='responses', endpoint='http://127.0.0.1:4317/v1/responses',
                                    model='fixture', auth='none')
    client = ModelJudge(store.config)
    seen = transport(monkeypatch)
    with pytest.raises(resources.AdmissionDenied, match='resource_scope_missing'):
        client.evaluate({}, QUESTIONS)
    assert client.calls == 0 and client.traces == [] and seen == []


def test_jev_with_unknown_output_limit_fails_closed_without_call(env, monkeypatch):
    from sessiongraph import jev
    from test_model_judge import QUESTIONS
    store, _ = env
    client = jev.JevClient({})
    client.key, client.audit_config = 'synthetic-secret', store.config
    monkeypatch.setattr(jev, 'verified_tls_context', lambda: None)
    seen = transport(monkeypatch)
    with pytest.raises(resources.AdmissionDenied, match='resource_output_limit_unknown'):
        run_stage(store, lambda: client.evaluate({}, QUESTIONS), stage='relations')
    assert client.calls == 0 and seen == []


def test_real_graph_budget_blocks_later_stage_preserves_nodes_and_replays_no_network(tmp_path, monkeypatch):
    from sessiongraph.extraction import create_extractor
    from sessiongraph.model_judge import ModelJudge
    _, store, _ = make_store(tmp_path, [('assistant', 'synthetic training A produced model B')])
    enable(store.config)
    store.config['graph_v2']['resource_policy']['max_requests'] = 1
    store.config['provider'].update(protocol='responses', endpoint='http://127.0.0.1:4317/v1/responses',
                                   model='fixture', auth='none')
    selected = output(entries(store))
    for item in selected['nodes'] + selected['relations']:
        item['evidence'] = [{'anchor_id': 'a1'}]
    selected['coverage'].pop('entry_ids')
    raw = json.dumps({'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant',
        'content': [{'type': 'output_text', 'text': json.dumps(selected)}]}]}).encode()
    seen = transport(monkeypatch, raw)
    first = graph.run_chat(store, create_extractor(store.config), 'chat-1', jev=ModelJudge(store.config))
    assert first['failure_codes'] == ['resource_budget_exhausted']
    assert first['model_calls'] == 1 and len(first['saved']) == 2 and not first['saved_relations']
    assert store.db.execute('SELECT state FROM sg2_control').fetchone()[0] == 'blocked'
    job = store.db.execute('SELECT id FROM sg2_jobs').fetchone()[0]
    assert spent(store, job)['requests'] == 1
    for respect_schedule in (True, False):
        again = graph.run_chat(store, create_extractor(store.config), 'chat-1',
                               jev=ModelJudge(store.config), respect_schedule=respect_schedule)
        assert again['model_calls'] == 0
    assert len(seen) == 1 and store.db.execute('SELECT count(*) FROM sg2_nodes').fetchone()[0] == 2
    store.close()


def test_real_graph_circuit_wait_round_trips_without_failure_debt_or_network(tmp_path, monkeypatch):
    from sessiongraph.model_judge import ModelJudge
    _, store, _ = make_store(tmp_path, [('assistant', 'synthetic training A produced model B')])
    enable(store.config)
    store.config['graph_v2']['resource_policy']['circuit_failure_threshold'] = 1
    store.config['provider'].update(protocol='responses', endpoint='http://127.0.0.1:4317/v1/responses',
                                   model='fixture', auth='none')
    def failed(): raise TimeoutError('synthetic transport')
    seen = transport(monkeypatch, callback=failed)
    first = graph.run_chat(store, FakeExtractor([output(entries(store))]), 'chat-1',
                           jev=ModelJudge(store.config))
    assert first['failure_codes'] == ['provider_timeout'] and len(first['saved']) == 2
    assert store.db.execute('SELECT failures FROM sg2_control').fetchone()[0] == 1
    second = graph.run_chat(store, FakeExtractor([]), 'chat-1', jev=ModelJudge(store.config))
    assert second['failure_codes'] == ['resource_circuit_open'] and second['model_calls'] == 0
    control = dict(store.db.execute('SELECT * FROM sg2_control').fetchone())
    assert control['failures'] == 1 and control['due'] >= circuit(store)['open_until']
    assert json.loads(control['error_json'])['retry_at']
    assert store.db.execute('SELECT state FROM sg2_attempts ORDER BY rowid DESC LIMIT 1').fetchone()[0] == 'deferred'
    scheduled = graph.run_chat(store, FakeExtractor([]), 'chat-1', jev=ModelJudge(store.config), respect_schedule=True)
    assert scheduled['model_calls'] == 0 and len(seen) == 1
    assert store.db.execute('SELECT count(*) FROM sg2_nodes').fetchone()[0] == 2
    store.close()


def test_settlement_failure_prevents_checkpoint_and_retains_precharge(env, monkeypatch):
    store, client = env
    transport(monkeypatch)
    def unavailable(*args, **kwargs): raise sqlite3.OperationalError('synthetic ledger unavailable')
    monkeypatch.setattr(resources, 'settle', unavailable)
    with pytest.raises(sqlite3.OperationalError):
        run_stage(store, lambda: request_audit.read_response(client, req(), []))
    assert spent(store)['requests'] == 1 and spent(store)['seconds'] == 30
    assert store.db.execute('SELECT count(*) FROM sg2_checkpoints').fetchone()[0] == 0
    assert store.db.execute('SELECT state FROM sg_resource_requests').fetchone()[0] == 'reserved'


@pytest.mark.parametrize('mode', ['before_io', 'bad_opener'])
def test_local_failures_do_not_trip_transport_circuit(env, monkeypatch, mode):
    store, client = env
    if mode == 'before_io':
        monkeypatch.setattr(request_audit, 'prune_diagnostics', lambda *args: (_ for _ in ()).throw(OSError('synthetic local failure')))
    else:
        monkeypatch.setattr('urllib.request.build_opener', lambda *args: (_ for _ in ()).throw(ValueError('synthetic local failure')))
    with pytest.raises((OSError, ValueError)):
        run_stage(store, lambda: request_audit.read_response(client, req(), []))
    assert spent(store)['seconds'] == 0 and spent(store)['requests'] == 1
    assert circuit(store)['failures'] == 0


def test_single_token_preflight_denial_preserves_previous_validated_trace(env, monkeypatch):
    from sessiongraph.single_token_judge import SingleTokenJudge
    store, _ = env
    store.config['provider'].update(protocol='responses', endpoint='http://127.0.0.1:4317/v1/responses',
                                   model='fixture', auth='none')
    client = SingleTokenJudge(store.config)
    old = Path(store.config['database']).parent / 'previous-trace.json'
    old.write_text('{"state":"validated"}')
    client.traces.append({'path': str(old)})
    prepared = client.prepare({}, dict(revision='fixture-v1', prompt='same?',
        criteria={'same': 'same object', 'unknown': 'insufficient evidence'}, abstain='unknown'))
    seen = transport(monkeypatch)
    with pytest.raises(resources.AdmissionDenied, match='resource_scope_missing'):
        client.classify(prepared)
    assert client.calls == 0 and seen == []
    assert old.read_text() == '{"state":"validated"}'


@pytest.mark.parametrize('mode', ['absent_db', 'bad_schema', 'unknown_cap', 'endpoint_query'])
def test_preflight_faults_are_bounded_safe_denials(env, monkeypatch, mode):
    store, client = env
    request = req()
    if mode == 'absent_db':
        client.config = {**store.config, 'database': str(Path(store.config['database']).parent / 'missing.sqlite3')}
    elif mode == 'bad_schema':
        with store.db: store.db.execute('CREATE TABLE sg_resource_scopes(bad TEXT)')
    elif mode == 'unknown_cap':
        request = req(max_tokens=1)
    else:
        request = Request(request.full_url + '?api_key=synthetic-secret', data=request.data)
    seen = transport(monkeypatch)
    with pytest.raises(resources.AdmissionDenied):
        run_stage(store, lambda: request_audit.read_response(client, request, []))
    assert seen == [] and client.traces == []
    if mode == 'absent_db': assert not Path(client.config['database']).exists()


@pytest.mark.parametrize('status,trips', [(401, True), (403, True), (429, True), (503, True), (400, False), (451, False)])
def test_http_failures_trip_only_selected_circuit_classes(env, monkeypatch, status, trips):
    from urllib.error import HTTPError
    store, client = env
    def fail(): raise HTTPError(req().full_url, status, 'synthetic', {}, None)
    transport(monkeypatch, callback=fail)
    with pytest.raises(HTTPError):
        run_stage(store, lambda: request_audit.read_response(client, req(), []))
    assert circuit(store)['failures'] == int(trips)
    assert store.db.execute('SELECT code FROM sg_resource_requests').fetchone()[0] == f'provider_http_{status}'


def test_automatic_graph_repair_cannot_reset_cumulative_budget(tmp_path, monkeypatch):
    from sessiongraph.extraction import create_extractor
    from sessiongraph.model_judge import ModelJudge
    _, store, _ = make_store(tmp_path, [('assistant', 'synthetic training A produced model B')])
    enable(store.config)
    store.config['graph_v2']['resource_policy']['max_requests'] = 1
    store.config['provider'].update(protocol='responses', endpoint='http://127.0.0.1:4317/v1/responses',
                                   model='fixture', auth='none')
    # Valid transport JSON, invalid candidate structure triggers the real repair path.
    raw = b'{"status":"completed","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"{}"}]}]}'
    seen = transport(monkeypatch, raw)
    result = graph.run_chat(store, create_extractor(store.config), 'chat-1', jev=ModelJudge(store.config))
    assert result['graph_v2']['schema_repairs'] == 1
    assert result['failure_codes'] == ['resource_budget_exhausted']
    assert result['model_calls'] == 1 and len(seen) == 1
    assert store.db.execute('SELECT count(*) FROM sg2_jobs').fetchone()[0] == 1
    assert store.db.execute('SELECT requests FROM sg_resource_scopes').fetchone()[0] == 1
    store.close()


def test_rehearsal_report_is_synthetic_and_separates_reserved_from_observed():
    from rehearse_resource_policy import rehearse
    report = rehearse()
    assert report['admitted_requests'] == report['mocked_dispatches'] == 12
    assert report['blocked_preflights'] == 100
    assert report['output_tokens_reserved'] == 120 and report['observed_output_tokens'] == 12
    assert report['output_tokens_charged'] == 12
    assert report['production_accesses'] == report['real_network_calls'] == 0
    assert report['quick_check'] == 'ok'
