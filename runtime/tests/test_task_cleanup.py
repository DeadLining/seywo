import json
import time
from pathlib import Path

import pytest
from sessiongraph import continuation, maintenance
from sessiongraph.auto_update import _atomic, _folder, update_status
from test_memories import make_store


def setup_case(tmp_path, monkeypatch):
    monkeypatch.delenv('SESSIONGRAPH_WORKER_LOCK', raising=False)
    root, store, _ = make_store(tmp_path, [('user', '保留实验结果与原文')])
    config = root / '.sessiongraph/config.json'
    store.config['_config_path'] = str(config)
    folder = continuation.root(store.config)
    folder.mkdir(parents=True, exist_ok=True)
    now = time.time()
    key = continuation.key(config, 'chat-1')
    blocked = dict(config=str(config), chat='chat-1', state='blocked', target=12,
                   run_id='old', updated_at=now-4000, due=0, error_code='import_source_missing')
    continuation.write(folder/'queue.json', {key: blocked, 'other':dict(blocked, config=str(tmp_path/'other/config.json')),
                                           'active':dict(blocked, chat='chat-active', state='running')})
    runs = _folder(store.config)
    for name, state, age, pid in [('failed','failed',4000,None),('dead','importing',4000,None),
                                 ('live','extracting',4000,__import__('os').getpid()),
                                 ('new','queued',0,None),('success','succeeded',4000,None)]:
        _atomic(runs/f'{name}.json', dict(run_id=name, chat_id='chat-1', state=state,
                created_at=now-age, updated_at=now-age, pid=pid))
    return config, store, folder, key


def test_cleanup_scoped_reversible_and_never_success(tmp_path, monkeypatch):
    config, store, folder, key = setup_case(tmp_path, monkeypatch)
    before = [tuple(r) for r in store.db.execute('SELECT * FROM entries')]
    receipts = {p.name:p.read_bytes() for p in _folder(store.config).glob('*.json')}
    result = maintenance.run(config, 'clear_tasks')
    assert result['dismissed_tasks'] == 1 and result['archived_runs'] == 2
    assert result['model_calls'] == 0
    queue = continuation.read(folder/'queue.json', {})
    assert queue[key]['state'] == 'dismissed' and queue[key]['error_code'] == 'import_source_missing'
    assert queue['other']['state'] == 'blocked' and queue['active']['state'] == 'running'
    assert not any(v['chat']=='chat-1' for v in continuation.snapshot(store.config))
    status = update_status(store.config)
    assert {r['run_id'] for r in status['runs']} == {'live','new','success'}
    for name in ('failed.json','dead.json'):
        assert (_folder(store.config).parent/'cleared-runs'/name).read_bytes() == receipts[name]
    assert [tuple(r) for r in store.db.execute('SELECT * FROM entries')] == before
    again = maintenance.run(config, 'clear_tasks')
    assert again['dismissed_tasks'] == again['archived_runs'] == 0
    store.close()


def test_dismissal_cannot_be_resurrected_by_old_receipt_but_new_event_allowed(tmp_path, monkeypatch):
    config, store, folder, key = setup_case(tmp_path, monkeypatch)
    maintenance.run(config, 'clear_tasks')
    cleared = continuation.read(folder/'queue.json', {})[key]['dismissed_at']
    old = dict(chat_id='chat-1', run_id='late', event='PostCompact', state='failed',
               error_code='import_source_missing', created_at=cleared-10, updated_at=cleared+1, target_rowid=12)
    continuation.record(config, old, launch=False)
    assert continuation.read(folder/'queue.json', {})[key]['state'] == 'dismissed'
    continuation.record(config, dict(old, run_id='fresh', created_at=cleared+2, updated_at=cleared+3), launch=False)
    assert continuation.read(folder/'queue.json', {})[key]['run_id'] == 'fresh'
    store.close()


def test_dismissed_is_never_dispatched(tmp_path, monkeypatch):
    config, store, folder, key = setup_case(tmp_path, monkeypatch)
    maintenance.run(config, 'clear_tasks')
    queue=continuation.read(folder/'queue.json', {})
    continuation.write(folder/'queue.json', {key:queue[key]})
    monkeypatch.setattr(continuation.subprocess,'Popen',lambda *a,**kw:pytest.fail('dispatched dismissed task'))
    continuation.dispatch(folder)
    assert continuation.process_one(folder) is False
    store.close()


def test_cleanup_refuses_active_writer(tmp_path, monkeypatch):
    import fcntl
    config, store, folder, key = setup_case(tmp_path, monkeypatch)
    lock = _folder(store.config).parent/'update.lock'
    with lock.open('a') as f:
        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(ValueError,match='maintenance_busy'):
            maintenance.run(config,'clear_tasks')
    assert continuation.read(folder/'queue.json', {})[key]['state']=='blocked'
    store.close()


def test_native_action_is_explicit_project_scoped_and_model_free(tmp_path, monkeypatch):
    from sessiongraph.native_settings import handle
    from test_native_settings import setup
    config, args, project = setup(tmp_path)
    monkeypatch.delenv('SESSIONGRAPH_WORKER_LOCK', raising=False)
    import socket
    monkeypatch.setattr(socket, 'socket', lambda *a, **k: pytest.fail('network call'))
    result = handle({'action':'clear_tasks', 'project':project}, **args)
    assert result['ok'] and result['maintenance']['dismissed_tasks']==0
    assert result['maintenance']['archived_runs']==0 and result['model_calls']==0
    with pytest.raises(ValueError):
        handle({'action':'clear_tasks'}, **args)


def test_cleanup_dismisses_v2_batches_without_losing_progress_or_replaying(tmp_path, monkeypatch):
    from sessiongraph.graph_v2 import run_chat, diagnostics
    from sessiongraph import stages
    from test_memories import FakeExtractor, entries
    from test_graph_v2 import Missing, output
    config, store, folder, key = setup_case(tmp_path, monkeypatch)
    run_chat(store, FakeExtractor([output(entries(store))]), 'chat-1', jev=Missing())
    job = dict(store.db.execute('SELECT * FROM sg2_jobs').fetchone())
    control = dict(store.db.execute('SELECT * FROM sg2_control').fetchone())
    assert control['state'] == 'blocked'
    preserved = maintenance.counts(store)
    claims = [tuple(r) for r in store.db.execute('SELECT * FROM sg2_claims')]
    assert claims
    result = maintenance.run(config, 'clear_tasks')
    assert result['dismissed_batches'] == 1 and result['model_calls'] == 0
    row = dict(store.db.execute('SELECT * FROM sg2_jobs WHERE id=?', (job['id'],)).fetchone())
    assert row == {**job, 'state':'dismissed'}
    assert dict(store.db.execute('SELECT * FROM sg2_control').fetchone()) == {**control, 'state':'dismissed'}
    receipt = json.loads(store.db.execute('SELECT before_json FROM sg2_task_dismissals').fetchone()[0])
    assert receipt['job_state'] == job['state'] and receipt['error_json'] == control['error_json']
    assert maintenance.counts(store) == preserved
    assert not diagnostics(store)['blocked_jobs']
    assert not diagnostics(store)['progress'].get('blocked')
    assert not stages.pending(store, store.config['project_id'], 'chat-1', include_manual=True)
    stages.setup(store)
    assert [tuple(r) for r in store.db.execute('SELECT * FROM sg2_claims')] == claims
    assert maintenance.run(config, 'clear_tasks')['dismissed_batches'] == 0
    # Neither an explicit old job selection nor a fresh Hook can replay it.
    result = run_chat(store, FakeExtractor([]), 'chat-1', pending_only=True,
                      job_ids=[job['id']], respect_schedule=False)
    assert result['model_calls'] == 0
    result = run_chat(store, FakeExtractor([]), 'chat-1', respect_schedule=True)
    assert result['model_calls'] == 0
    assert store.db.execute('SELECT state FROM sg2_jobs WHERE id=?',(job['id'],)).fetchone()[0] == 'dismissed'
    # A later source append remains runnable; dismissal is not a chat-wide ban.
    with (config.parent.parent / 'chat.jsonl').open('a') as f:
        f.write(json.dumps(dict(timestamp='2026-10-27T00:00:00Z', type='response_item',
            payload=dict(type='message',role='user',content=[dict(type='input_text',text='新实验 B 已保存检查点 C')]))) + '\n')
    store.ingest(config.parent.parent / 'chat.jsonl')
    from test_graph_v2 import Judge
    class NewInputExtractor(FakeExtractor):
        def __call__(self, batch, policy):
            self.calls.append((batch, policy))
            result = output(batch, title='新实验 B', artifact='检查点 C')
            # Related historical context may be appended after current input.
            # This fixture claims a NEW result, so cite actual current evidence.
            current = next(e for e in batch if not e.get('context_only'))
            for item in [*result['nodes'], *result['relations']]:
                item['evidence'] = [dict(entry_id=current['id'], quote=current['text'])]
            return result
    ex = NewInputExtractor([])
    result = run_chat(store, ex, 'chat-1', jev=Judge(), respect_schedule=True)
    assert len(ex.calls) == 1 and result['saved']
    assert store.db.execute('SELECT state FROM sg2_jobs WHERE id=?',(job['id'],)).fetchone()[0] == 'dismissed'
    store.close()


def test_v2_cleanup_leaves_waiting_and_running_batches_alone(tmp_path, monkeypatch):
    from sessiongraph.graph_v2 import run_chat
    from test_memories import FakeExtractor, entries
    from test_graph_v2 import Missing, output
    config, store, _, _ = setup_case(tmp_path, monkeypatch)
    run_chat(store, FakeExtractor([output(entries(store))]), 'chat-1', jev=Missing())
    job = store.db.execute('SELECT id FROM sg2_jobs').fetchone()[0]
    for state in ['ready', 'retry_wait']:
        with store.db:
            store.db.execute('UPDATE sg2_control SET state=? WHERE job=?',(state,job))
        assert maintenance.run(config,'clear_tasks')['dismissed_batches'] == 0
    with store.db:
        store.db.execute("UPDATE sg2_control SET state='blocked' WHERE job=?",(job,))
        store.db.execute("UPDATE sg2_attempts SET state='running' WHERE job=?",(job,))
    assert maintenance.run(config,'clear_tasks')['dismissed_batches'] == 0
    assert store.db.execute('SELECT state FROM sg2_jobs').fetchone()[0] != 'dismissed'
    store.close()


def test_v2_cleanup_is_project_scoped_even_with_a_foreign_job(tmp_path, monkeypatch):
    from sessiongraph.graph_v2 import run_chat
    from test_memories import FakeExtractor, entries
    from test_graph_v2 import Missing, output
    config, store, _, _ = setup_case(tmp_path, monkeypatch)
    run_chat(store, FakeExtractor([output(entries(store))]), 'chat-1', jev=Missing())
    job = dict(store.db.execute('SELECT * FROM sg2_jobs').fetchone())
    with store.db:
        values = {**job, 'id':'foreign-job', 'project':'other-project'}
        store.db.execute('INSERT INTO sg2_jobs VALUES('+','.join('?' for _ in values)+')', tuple(values.values()))
        store.db.execute("INSERT INTO sg2_control VALUES('foreign-job','blocked',0,7,'{}')")
    assert maintenance.run(config, 'clear_tasks')['dismissed_batches'] == 1
    assert store.db.execute("SELECT state FROM sg2_jobs WHERE id='foreign-job'").fetchone()[0] == job['state']
    assert store.db.execute("SELECT failures FROM sg2_control WHERE job='foreign-job'").fetchone()[0] == 7
    store.close()


def test_legacy_error_without_control_is_blocked_and_clearable(tmp_path, monkeypatch):
    from sessiongraph import stages
    from sessiongraph.graph_v2 import run_chat, diagnostics
    from test_memories import FakeExtractor, entries
    from test_graph_v2 import Missing, output
    config, store, folder, _ = setup_case(tmp_path, monkeypatch)
    run_chat(store, FakeExtractor([output(entries(store))]), 'chat-1', jev=Missing())
    # Exact old on-disk shape: pending + graph_io, no durable control/attempts.
    with store.db:
        store.db.execute('DELETE FROM sg2_control')
        store.db.execute('DELETE FROM sg2_attempts')
        store.db.execute("UPDATE sg2_jobs SET error='graph_io',stats_json=?",
                         (json.dumps({'failure_reason':'graph_io'}),))
    continuation.write(folder/'queue.json', {})
    job = dict(store.db.execute('SELECT * FROM sg2_jobs').fetchone())
    claims = [tuple(r) for r in store.db.execute('SELECT * FROM sg2_claims')]
    before = maintenance.counts(store)
    diag = diagnostics(store)
    assert diag['progress'] == {'blocked':1}
    assert diag['blocked_jobs'][0]['state'] == 'blocked'
    assert stages.pending(store, store.config['project_id'], 'chat-1')[0]['state'] == 'blocked'
    # Automatic hooks must not turn the missing control into an implicit retry.
    result = run_chat(store, FakeExtractor([]), 'chat-1', pending_only=True, respect_schedule=True)
    assert result['model_calls'] == 0 and result['graph_v2']['blocked_jobs'] == 1
    assert maintenance.run(config, 'clear_tasks')['dismissed_batches'] == 1
    assert dict(store.db.execute('SELECT * FROM sg2_jobs').fetchone()) == {**job, 'state':'dismissed'}
    receipt = json.loads(store.db.execute('SELECT before_json FROM sg2_task_dismissals').fetchone()[0])
    assert receipt['state'] is None and receipt['job_state'] == 'pending'
    assert maintenance.counts(store) == before
    assert [tuple(r) for r in store.db.execute('SELECT * FROM sg2_claims')] == claims
    assert not diagnostics(store)['blocked_jobs'] and not diagnostics(store)['progress']
    assert maintenance.run(config, 'clear_tasks')['dismissed_batches'] == 0
    assert run_chat(store, FakeExtractor([]), 'chat-1', respect_schedule=True)['model_calls'] == 0
    store.close()


@pytest.mark.parametrize('error', [None, '', 'graph_call_budget_pending', 'graph_continuation_pending'])
def test_legacy_normal_pending_is_not_dismissed(tmp_path, monkeypatch, error):
    from sessiongraph.graph_v2 import run_chat, diagnostics
    from test_memories import FakeExtractor, entries
    from test_graph_v2 import Missing, output
    config, store, folder, _ = setup_case(tmp_path, monkeypatch)
    run_chat(store, FakeExtractor([output(entries(store))]), 'chat-1', jev=Missing())
    with store.db:
        store.db.execute('DELETE FROM sg2_control')
        store.db.execute('DELETE FROM sg2_attempts')
        store.db.execute('UPDATE sg2_jobs SET error=?', (error,))
    continuation.write(folder/'queue.json', {})
    diag = diagnostics(store)
    assert diag['progress'] == {'ready':1}
    assert not diag['blocked_jobs']
    assert maintenance.run(config,'clear_tasks')['dismissed_batches'] == 0
    store.close()


def test_legacy_error_with_active_attempt_is_not_dismissed(tmp_path, monkeypatch):
    from sessiongraph.graph_v2 import run_chat
    from test_memories import FakeExtractor, entries
    from test_graph_v2 import Missing, output
    config, store, _, _ = setup_case(tmp_path, monkeypatch)
    run_chat(store, FakeExtractor([output(entries(store))]), 'chat-1', jev=Missing())
    with store.db:
        store.db.execute('DELETE FROM sg2_control')
        store.db.execute("UPDATE sg2_jobs SET error='graph_io'")
        store.db.execute("UPDATE sg2_attempts SET state='running'")
    assert maintenance.run(config, 'clear_tasks')['dismissed_batches'] == 0
    store.close()
