"""Real file locks, DB ownership and process death; providers remain synthetic."""
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from sessiongraph import graph_v2 as graph, stages, stage_leases as leases
from sessiongraph import request_audit, maintenance
from sessiongraph.auto_update import run_update
from sessiongraph.core import Store
from sessiongraph.extraction import create_extractor
from sessiongraph.failures import Failure
from test_graph_v2 import Judge, output
from test_memories import make_store, entries, FakeExtractor
from test_mvp import write_session
from test_resource_policy import enable, add_job, req, transport


def configure(store):
    enable(store.config)
    store.config['graph_v2']['stage_policy'] = {'enabled': True}
    store.config['provider'].update(protocol='responses', endpoint='http://127.0.0.1:4317/v1/responses',
                                    model='synthetic', auth='none')


@pytest.fixture
def env(tmp_path):
    root, store, source = make_store(tmp_path, [('user', 'synthetic training A produced model B')])
    configure(store)
    graph.setup(store)
    add_job(store)
    client = create_extractor(store.config)
    yield root, store, source, client
    store.close()


@leases.managed
def invoke(store, extractor, chat_id, *, operation, stage='extract', part='part', job='job'):
    return stages.execute(store, job, part, stage, operation, {}, cache=False)


@contextmanager
def available(path):
    with path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def lease(store):
    return dict(store.db.execute('SELECT * FROM sg2_stage_lease').fetchone())


def test_real_transport_releases_graph_writer_not_stage_or_maintenance_permit(env, monkeypatch):
    _, store, _, client = env
    base = Path(store.config['database']).parent
    def callback():
        with available(base / 'graph-v2.lock'):
            db = sqlite3.connect(store.config['database'], timeout=.1)
            try:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT state FROM sg2_stage_lease').fetchone()
                assert row[0] == 'running'
                db.rollback()
            finally:
                db.close()
        with pytest.raises(BlockingIOError), available(base / 'stage-execution.lock'):
            pass
        with pytest.raises(ValueError, match='maintenance_busy'), maintenance.project_lock(store.config):
            pass
    transport(monkeypatch, callback=callback)
    def operation():
        raw = request_audit.read_response(client, req(), [])
        with pytest.raises(BlockingIOError), available(base / 'graph-v2.lock'):
            pass
        return bool(raw)
    assert invoke(store, client, chat_id='chat-1', operation=operation)
    assert lease(store)['state'] == 'released'
    with maintenance.project_lock(store.config):
        pass
    assert leases.SESSION.get() is leases.CLAIM.get() is None


def test_new_input_can_import_during_network_without_invalidating_frozen_snapshot(env, monkeypatch):
    _, store, source, client = env
    before = leases.revision(store)
    def callback():
        with source.open('a') as stream:
            stream.write(json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                'content': [{'type': 'input_text', 'text': 'new unrelated turn'}]}}) + '\n')
        other = Store(store.config)
        try:
            assert other.ingest(source)['added'] == 1
        finally:
            other.close()
    transport(monkeypatch, callback=callback)
    invoke(store, client, 'chat-1', operation=lambda: bool(request_audit.read_response(client, req(), [])))
    assert leases.revision(store) == before
    assert store.db.execute('SELECT count(*) FROM entries').fetchone()[0] == 2


@pytest.mark.parametrize('column,value', [('text', 'changed original'), ('role', 'assistant'), ('timestamp', 'changed-time')])
def test_existing_source_changes_discard_response_before_stage_checkpoint(env, monkeypatch, column, value):
    _, store, _, client = env
    def callback():
        db = sqlite3.connect(store.config['database'])
        with db:
            db.execute(f'UPDATE entries SET {column}=?', (value,))
        db.close()
    transport(monkeypatch, callback=callback)
    with pytest.raises(Failure, match='stage_snapshot_changed') as error:
        invoke(store, client, 'chat-1', operation=lambda: request_audit.read_response(client, req(), []))
    assert error.value.retry_at
    assert store.db.execute('SELECT count(*) FROM sg2_checkpoints').fetchone()[0] == 0
    assert store.db.execute('SELECT state FROM sg2_attempts').fetchone()[0] == 'deferred'
    assert store.db.execute('SELECT requests FROM sg_resource_scopes').fetchone()[0] == 1
    assert lease(store)['state'] == 'released'


def test_external_generation_fence_cannot_publish_checkpoint_or_overwrite_successor(env, monkeypatch):
    _, store, _, client = env
    def callback():
        db = sqlite3.connect(store.config['database'])
        with db:
            db.execute("UPDATE sg2_stage_lease SET generation=generation+1,owner='successor'")
        db.close()
    transport(monkeypatch, callback=callback)
    with pytest.raises(Failure, match='stage_lease_lost'):
        invoke(store, client, 'chat-1', operation=lambda: request_audit.read_response(client, req(), []))
    assert lease(store)['owner'] == 'successor' and lease(store)['state'] == 'running'
    assert store.db.execute('SELECT count(*) FROM sg2_checkpoints').fetchone()[0] == 0


def test_no_write_transaction_may_escape_to_transport(env):
    _, store, _, client = env
    def operation():
        store.db.execute('BEGIN IMMEDIATE')
        try:
            with leases.transport_window(client):
                pytest.fail('uncommitted transaction reached transport')
        finally:
            store.db.rollback()
    with pytest.raises(Failure, match='stage_transport_transaction_open'):
        invoke(store, client, 'chat-1', operation=operation)


def test_transport_exception_reacquires_graph_lock(env, monkeypatch):
    _, store, _, client = env
    def callback(): raise TimeoutError('synthetic timeout')
    transport(monkeypatch, callback=callback)
    with pytest.raises(TimeoutError):
        invoke(store, client, 'chat-1', operation=lambda: request_audit.read_response(client, req(), []))
    with available(Path(store.config['database']).parent / 'graph-v2.lock'):
        pass
    assert lease(store)['state'] == 'released'


@pytest.mark.parametrize('value', [True, {'enabled': 'yes'}, {'enabled': True, 'timeout': 1}])
def test_stage_policy_is_explicit_and_strict(env, value):
    _, store, _, _ = env
    store.config['graph_v2']['stage_policy'] = value
    with pytest.raises(ValueError, match='stage_policy'):
        leases.enabled(store.config)


def test_stage_policy_requires_real_cumulative_budget_and_managed_session(env):
    _, store, _, client = env
    with pytest.raises(Failure, match='stage_session_missing'):
        stages.execute(store, 'job', 'part', 'extract', lambda: None, {})
    store.config['graph_v2'].pop('resource_policy')
    with pytest.raises(ValueError, match='requires explicit cumulative'):
        leases.enabled(store.config)


def test_default_graph_path_has_no_stage_schema_or_lock_files(tmp_path):
    _, store, _ = make_store(tmp_path, [('user', 'synthetic training A produced model B')])
    result = graph.run_chat(store, FakeExtractor([output(entries(store))]), 'chat-1', jev=Judge())
    assert len(result['saved_relations']) == 1
    assert not store.db.execute("SELECT name FROM sqlite_master WHERE name='sg2_stage_lease'").fetchone()
    assert not list(Path(store.config['database']).parent.glob('stage-*.lock'))
    store.close()


def test_update_lock_is_free_only_during_child_and_maintenance_stays_excluded(env):
    _, store, source, _ = env
    config_path = Path(store.config['_config_path'])
    config = json.loads(config_path.read_text())
    config.update({key: store.config[key] for key in ('provider', 'graph_v2')})
    config_path.write_text(json.dumps(config))
    base = Path(store.config['database']).parent
    seen = []
    def execute(argv, timeout):
        if 'refresh-chat' in argv:
            with pytest.raises(BlockingIOError), available(base / 'hook-state/update.lock'):
                pass
            return {'added_entries': 0, 'skipped': []}
        with available(base / 'hook-state/update.lock'):
            seen.append(True)
        with pytest.raises(ValueError, match='maintenance_busy'), maintenance.project_lock(store.config):
            pass
        return dict(saved=[], failed=[], model_calls=0, graph_v2=dict(durable_stages=True, source_remaining=False))
    result = run_update(config_path, 'chat-1', 'AutoContinue', str(source), execute=execute)
    assert result['state'] == 'succeeded' and seen == [True], result
    with maintenance.project_lock(store.config): pass


CHILD = r'''
import json, sys
from unittest.mock import patch
sys.path.insert(0, 'tests')
from test_graph_v2 import Judge, output
from test_memories import entries
from sessiongraph.core import Store
from sessiongraph import graph_v2 as graph
from sessiongraph.extraction import create_extractor
config=json.loads(sys.argv[1])
store=Store(config)
selected=output(entries(store))
for item in selected['nodes']+selected['relations']:item['evidence']=[{'anchor_id':'a1'}]
selected['coverage'].pop('entry_ids')
raw=json.dumps({'status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':json.dumps(selected)}]}]}).encode()
class Response:
    status,headers=200,{}
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,limit):return raw
class Opener:
    def open(self,*args,**kwargs):
        print('NETWORK',flush=True)
        sys.stdin.readline()
        return Response()
with patch('urllib.request.build_opener',lambda *args:Opener()):
    result=graph.run_chat(store,create_extractor(config),'chat-1',jev=Judge())
print(json.dumps(result),flush=True)
store.close()
'''


@contextmanager
def blocked_child(config):
    process = subprocess.Popen([sys.executable, '-c', CHILD, json.dumps(config)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ready, _, _ = select.select([process.stdout], [], [], 15)
        assert ready, 'child never entered transport'
        line = process.stdout.readline()
        assert line.strip() == 'NETWORK', (line, process.poll())
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def test_live_process_is_not_interrupted_by_another_chat(env):
    root, store, _, _ = env
    with store.db: store.db.execute("DELETE FROM sg2_jobs WHERE id='job'")
    with blocked_child(store.config) as child:
        running = dict(store.db.execute("SELECT * FROM sg2_attempts WHERE state='running'").fetchone())
        path = write_session(root / 'second.jsonl', root, 'chat-2', [('user', 'another independent source')])
        store.ingest(path)
        result = graph.run_chat(store, FakeExtractor([output(entries(store)[-1:])]), 'chat-2', jev=Judge())
        assert result['failure_codes'] == ['stage_lease_busy'] and result['model_calls'] == 0
        assert store.db.execute('SELECT state FROM sg2_attempts WHERE id=?', (running['id'],)).fetchone()[0] == 'running'
        control = dict(store.db.execute("SELECT * FROM sg2_control").fetchone())
        assert control['state'] == 'retry_wait' and control['failures'] == 0
        with pytest.raises(ValueError, match='maintenance_busy'), maintenance.project_lock(store.config): pass
        stdout, stderr = child.communicate('continue\n', timeout=15)
        assert child.returncode == 0, stderr
        assert len(json.loads(stdout)['saved_relations']) == 1
    assert lease(store)['state'] == 'released'


def test_process_death_reclaims_execution_but_never_refunds_request(env):
    _, store, _, client = env
    with store.db: store.db.execute("DELETE FROM sg2_jobs WHERE id='job'")
    store.config['graph_v2']['resource_policy']['max_requests'] = 1
    with blocked_child(store.config) as child:
        old = lease(store)
        assert old['state'] == 'running'
        child.kill()
        child.communicate(timeout=10)
    # Death releases kernel locks without updating the last durable lease row.
    assert lease(store)['state'] == 'running'
    with maintenance.project_lock(store.config): pass
    result = graph.run_chat(store, client, 'chat-1', jev=Judge())
    assert result['failure_codes'] == ['resource_budget_exhausted'] and result['model_calls'] == 0
    assert store.db.execute('SELECT state FROM sg2_attempts WHERE id=?', (old['attempt'],)).fetchone()[0] == 'interrupted'
    assert lease(store)['generation'] > old['generation']
    assert store.db.execute('SELECT requests FROM sg_resource_scopes').fetchone()[0] == 1
    assert store.db.execute('SELECT count(*) FROM sg2_nodes').fetchone()[0] == 0


def test_snapshot_change_in_real_graph_keeps_job_identity_and_cost_but_not_derived_output(tmp_path, monkeypatch):
    _, store, _ = make_store(tmp_path, [('user', 'synthetic training A produced model B')])
    configure(store)
    selected = output(entries(store))
    for item in selected['nodes'] + selected['relations']: item['evidence'] = [{'anchor_id': 'a1'}]
    selected['coverage'].pop('entry_ids')
    raw = json.dumps({'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant',
        'content': [{'type': 'output_text', 'text': json.dumps(selected)}]}]}).encode()
    def callback():
        db = sqlite3.connect(store.config['database'])
        with db: db.execute("UPDATE entries SET role='assistant'")
        db.close()
    transport(monkeypatch, raw, callback)
    first = graph.run_chat(store, create_extractor(store.config), 'chat-1', jev=Judge())
    assert first['failure_codes'] == ['stage_snapshot_changed'] and not first['saved']
    job = dict(store.db.execute('SELECT * FROM sg2_jobs').fetchone())
    assert job['output_json'] is None
    assert store.db.execute('SELECT count(*) FROM sg2_checkpoints').fetchone()[0] == 0
    assert store.db.execute('SELECT failures FROM sg2_control').fetchone()[0] == 0
    transport(monkeypatch, raw)
    second = graph.run_chat(store, create_extractor(store.config), 'chat-1', jev=Judge())
    assert len(second['saved_relations']) == 1
    assert store.db.execute('SELECT id FROM sg2_jobs').fetchone()[0] == job['id']
    assert store.db.execute('SELECT requests FROM sg_resource_scopes').fetchone()[0] == 2
    store.close()


def test_generation_loss_in_real_graph_cannot_rewrite_successor_control(tmp_path, monkeypatch):
    _, store, _ = make_store(tmp_path, [('user', 'synthetic source')])
    configure(store)
    def callback():
        db = sqlite3.connect(store.config['database'])
        with db:
            row = db.execute('SELECT job FROM sg2_stage_lease').fetchone()
            db.execute("UPDATE sg2_stage_lease SET generation=generation+1,owner='successor'")
            db.execute('INSERT OR REPLACE INTO sg2_control VALUES(?,?,?,?,?)',
                       (row[0], 'ready', 17, 2, '{"code":"successor_marker"}'))
        db.close()
    transport(monkeypatch, callback=callback)
    with pytest.raises(Failure, match='stage_lease_lost'):
        graph.run_chat(store, create_extractor(store.config), 'chat-1', jev=Judge())
    control = dict(store.db.execute('SELECT * FROM sg2_control').fetchone())
    assert control['due'] == 17 and control['failures'] == 2
    assert control['error_json'] == '{"code":"successor_marker"}'
    assert store.db.execute('SELECT state FROM sg2_jobs').fetchone()[0] == 'extracting'
    assert store.db.execute('SELECT count(*) FROM sg2_nodes').fetchone()[0] == 0
    assert lease(store)['owner'] == 'successor'
    store.close()


def test_stale_relation_response_keeps_already_published_nodes(tmp_path, monkeypatch):
    from sessiongraph.model_judge import ModelJudge
    _, store, _ = make_store(tmp_path, [('user', 'synthetic training A produced model B')])
    configure(store)
    def callback():
        db = sqlite3.connect(store.config['database'])
        with db: db.execute("UPDATE entries SET timestamp='new observed time'")
        db.close()
    transport(monkeypatch, callback=callback)
    result = graph.run_chat(store, FakeExtractor([output(entries(store))]), 'chat-1', jev=ModelJudge(store.config))
    assert result['failure_codes'] == ['stage_snapshot_changed']
    assert len(result['saved']) == 2 and not result['saved_relations']
    assert store.db.execute('SELECT count(*) FROM sg2_nodes').fetchone()[0] == 2
    assert store.db.execute("SELECT state FROM sg2_attempts WHERE stage='relations'").fetchone()[0] == 'deferred'
    store.close()


def test_preflight_ownership_loss_counts_no_transport_and_no_provider_trace(env, monkeypatch):
    _, store, _, extractor = env
    client = SimpleNamespace(**vars(extractor), calls=1)
    seen = transport(monkeypatch)
    def operation():
        with store.db:
            store.db.execute("UPDATE sg2_stage_lease SET generation=generation+1,owner='successor'")
        return request_audit.read_response(client, req(), [])
    with pytest.raises(Failure, match='stage_lease_lost'):
        invoke(store, client, 'chat-1', operation=operation)
    assert not seen and not client.traces and client.calls == 0
    assert store.db.execute('SELECT state FROM sg_resource_requests').fetchone()[0] == 'local_failure'


def test_quiescence_is_checked_even_if_reader_config_disables_stage_policy(env, monkeypatch):
    _, store, _, client = env
    disabled = {**store.config, 'graph_v2': {**store.config['graph_v2'], 'stage_policy': {'enabled': False}}}
    def callback():
        with pytest.raises(ValueError, match='maintenance_busy'), maintenance.project_lock(disabled): pass
    transport(monkeypatch, callback=callback)
    invoke(store, client, 'chat-1', operation=lambda: bool(request_audit.read_response(client, req(), [])))


def test_baseexception_releases_permits_and_marks_attempt_interrupted(env):
    _, store, _, client = env
    def operation(): raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        invoke(store, client, 'chat-1', operation=operation)
    assert lease(store)['state'] == 'released'
    assert store.db.execute('SELECT state FROM sg2_attempts').fetchone()[0] == 'interrupted'
    assert leases.CLAIM.get() is None and leases.SESSION.get() is None
    assert stages.CURRENT.get() == {}
    with maintenance.project_lock(store.config): pass


def test_failed_child_releases_coordinator_and_update_lock(env):
    _, store, _, _ = env
    path = Path(store.config['_config_path'])
    config = json.loads(path.read_text())
    config.update({key: store.config[key] for key in ('provider', 'graph_v2')})
    path.write_text(json.dumps(config))
    def execute(argv, timeout):
        if 'refresh-chat' in argv: return {'added_entries': 0, 'skipped': []}
        raise OSError('synthetic child failure')
    result = run_update(path, 'chat-1', 'AutoContinue', execute=execute)
    assert result['state'] == 'failed'
    with available(Path(store.config['database']).parent / 'hook-state/update.lock'):
        with maintenance.project_lock(store.config): pass


def test_rehearsal_runs_real_adapters_with_synthetic_outputs_only():
    from rehearse_stage_leases import rehearse
    result = rehearse()
    assert result['mocked_dispatches'] == 2
    assert result['real_network_calls'] == result['production_accesses'] == 0
    assert result['saved_nodes'] == 2 and result['saved_relations'] == 1
    assert result['quick_check'] == 'ok' and result['frozen_frontier_replay_calls'] == 0


@pytest.mark.parametrize('sql', [
    "UPDATE work_memory SET title='changed graph fact'",
    'DELETE FROM sg2_terms',
    'DELETE FROM sg2_evidence',
])
def test_graph_mutation_during_judgment_rejects_stale_relation(tmp_path, monkeypatch, sql):
    from sessiongraph.model_judge import ModelJudge
    _, store, _ = make_store(tmp_path, [('user', 'synthetic training A produced model B')])
    configure(store)
    def callback():
        db = sqlite3.connect(store.config['database'])
        try:
            with db:
                assert db.execute(sql).rowcount > 0
        finally:
            db.close()
    transport(monkeypatch, callback=callback)
    result = graph.run_chat(store, FakeExtractor([output(entries(store))]), 'chat-1', jev=ModelJudge(store.config))
    assert result['failure_codes'] == ['stage_snapshot_changed']
    assert len(result['saved']) == 2 and not result['saved_relations']
    assert store.db.execute('SELECT count(*) FROM sg2_relations').fetchone()[0] == 0
    assert store.db.execute('SELECT failures FROM sg2_control').fetchone()[0] == 0
    assert store.db.execute('SELECT requests FROM sg_resource_scopes').fetchone()[0] == 1
    store.close()


def test_repeated_snapshot_invalidation_cannot_reset_cumulative_request_budget(env, monkeypatch):
    _, store, _, client = env
    store.config['graph_v2']['resource_policy']['max_requests'] = 2
    def callback():
        db = sqlite3.connect(store.config['database'])
        try:
            with db:
                db.execute("UPDATE entries SET timestamp=coalesce(timestamp,'') || 'x'")
        finally:
            db.close()
    dispatched = transport(monkeypatch, callback=callback)
    for _ in range(2):
        with pytest.raises(Failure, match='stage_snapshot_changed'):
            invoke(store, client, 'chat-1', operation=lambda: request_audit.read_response(client, req(), []))
    with pytest.raises(Failure, match='resource_budget_exhausted'):
        invoke(store, client, 'chat-1', operation=lambda: request_audit.read_response(client, req(), []))
    assert len(dispatched) == 2
    assert store.db.execute('SELECT requests FROM sg_resource_scopes').fetchone()[0] == 2
    assert store.db.execute('SELECT count(*) FROM sg2_checkpoints').fetchone()[0] == 0


def test_enabling_stage_policy_reuses_preexisting_job_and_successful_checkpoints(tmp_path):
    _, store, _ = make_store(tmp_path, [('user', 'synthetic training A produced model B')])
    configure(store)
    store.config['graph_v2'].pop('stage_policy')
    initial = graph.run_chat(store, FakeExtractor([output(entries(store))]), 'chat-1', jev=Judge(), call_budget=1)
    assert not initial['failure_codes']
    assert initial['graph_v2']['deferred_reason'] == 'graph_call_budget_pending'
    job = store.db.execute('SELECT id FROM sg2_jobs').fetchone()[0]
    assert store.db.execute('SELECT count(*) FROM sg2_checkpoints').fetchone()[0] > 0
    store.config['graph_v2']['stage_policy'] = {'enabled': True}
    recovered = graph.run_chat(store, FakeExtractor([]), 'chat-1', jev=Judge())
    assert not recovered['failure_codes'] and len(recovered['saved_relations']) == 1
    assert recovered['graph_v2']['deepseek_calls'] == 0
    assert [r[0] for r in store.db.execute('SELECT id FROM sg2_jobs')] == [job]
    assert lease(store)['state'] == 'released'
    store.close()


def test_managed_same_chat_guards_remain_owned_during_network(env, monkeypatch):
    import hashlib
    _, store, _, client = env
    base = Path(store.config['database']).parent
    name = hashlib.sha256(b'chat-1').hexdigest()
    def callback():
        for path in (base / 'memory-extraction' / (name + '.stage.lock'),
                     base / 'hook-state' / ('coordinator-' + name + '.lock')):
            with pytest.raises(BlockingIOError), available(path): pass
        with available(base / 'graph-v2.lock'): pass
    transport(monkeypatch, callback=callback)
    with leases.coordinator(store.config, 'chat-1'):
        invoke(store, client, 'chat-1', operation=lambda: request_audit.read_response(client, req(), []))


def test_read_only_opted_in_project_does_not_migrate_or_launch(tmp_path, monkeypatch):
    from sessiongraph import memory
    from sessiongraph.auto_update import update_status
    _, store, _ = make_store(tmp_path, [('user', 'synthetic source')])
    configure(store)
    config = store.config
    schema = list(store.db.execute('SELECT sql FROM sqlite_master ORDER BY name'))
    store.close()
    def forbidden(*args, **kwargs): raise AssertionError('read attempted migration or work')
    monkeypatch.setattr(leases, 'setup', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    with_store = Store(config, readonly=True)
    try:
        assert memory.recall(with_store, 'synthetic')['model_calls'] == 0
        assert memory.resume(with_store)['model_calls'] == 0
        assert update_status(config)['model_calls'] == 0
        assert [r[0] for r in with_store.db.execute('SELECT sql FROM sqlite_master ORDER BY name')] == [r[0] for r in schema]
        assert not with_store.db.execute("SELECT name FROM sqlite_master WHERE name='sg2_stage_lease'").fetchone()
        assert not list(Path(config['database']).parent.glob('stage-*.lock'))
    finally:
        with_store.close()
