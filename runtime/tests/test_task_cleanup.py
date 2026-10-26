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
