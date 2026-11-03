"""Actual SQLite migration, locks, crash/restart and event-driven queue checks."""
import fcntl
import json
from pathlib import Path
import socket

import pytest

from sessiongraph import tier_backfill as b, tiering, graph_v2
from sessiongraph.core import Store, load_config
from test_tiering import publish
from test_memories import make_store, entries


@pytest.fixture
def historical(tmp_path):
    root, store, _ = make_store(tmp_path, [('assistant', '误差数值为1。')])
    store.config['graph_v2'] = dict(enabled=True, condensation=dict(enabled=True))
    graph_v2.setup(store)
    entry = entries(store)[0]
    for i in range(5):
        publish(store, f'node-{i}', entry, retention='knowledge',
                assertion=[dict(subject='误差',predicate='数值',value='1',unit='none',conditions=[])])
    store.db.execute('DELETE FROM sg_node_tiers')
    store.db.commit()
    config_path = root / '.sessiongraph/config.json'
    yield config_path, store
    store.close()


def snapshot(store):
    return {row[0]:[tuple(v) for v in store.db.execute(f'SELECT * FROM "{row[0]}"')]
            for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            if row[0] not in {'sg_node_tiers','metadata'}}


def test_bounded_batches_resume_and_only_change_projection(historical, monkeypatch):
    path, store = historical
    before = snapshot(store)
    monkeypatch.setattr(socket, 'socket', lambda *a,**k: pytest.fail('network called'))
    calls = []
    original = tiering.classify
    monkeypatch.setattr(tiering, 'classify',lambda row:(calls.append(row['id']) or original(row)))
    assert b.batch(path,2) == dict(processed=2,more=True,model_calls=0)
    assert b.batch(path,2) == dict(processed=2,more=True,model_calls=0)
    assert b.batch(path,2) == dict(processed=1,more=False,model_calls=0)
    assert len(calls)==len(set(calls))==5
    assert snapshot(store)==before
    assert tiering.summary(store)['current']
    assert b.batch(path)==dict(processed=0,more=False,model_calls=0)
    assert len(calls)==5


def test_old_rule_revision_is_upgraded_and_transaction_failure_rolls_back(historical, monkeypatch):
    path, store = historical
    tiering.run(store,apply=True)
    store.db.execute("UPDATE sg_node_tiers SET revision='tiering-v1'");store.db.commit()
    before = [tuple(r) for r in store.db.execute('SELECT * FROM sg_node_tiers')]
    original = tiering.project
    def crash(s,ids):
        original(s, ids[:1]);raise ValueError('simulate interruption')
    monkeypatch.setattr(tiering,'project',crash)
    with pytest.raises(ValueError,match='simulate interruption'): b.batch(path)
    assert [tuple(r) for r in store.db.execute('SELECT * FROM sg_node_tiers')]==before
    monkeypatch.setattr(tiering,'project',original)
    assert b.batch(path)['processed']==5
    assert tiering.summary(store)['current']


@pytest.mark.parametrize('name',['hook-state/update.lock','stage-activity.lock','graph-v2.lock'])
def test_busy_project_yields_without_modifying_data(historical,name):
    path, store=historical
    lock_path=Path(store.config['database']).parent/name
    lock_path.parent.mkdir(parents=True,exist_ok=True)
    with lock_path.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert b.batch(path)==dict(processed=0,more=True,busy=True,model_calls=0)
        assert not store.db.execute('SELECT * FROM sg_node_tiers').fetchall()
    assert b.batch(path)['processed']==5


def test_projection_writer_skips_general_setup_and_requires_existing_db(historical, monkeypatch):
    path, store=historical
    monkeypatch.setattr(Store,'_add_missing_columns',lambda *a:pytest.fail('general schema migration'))
    monkeypatch.setattr('sessiongraph.storage.setup',lambda *a:pytest.fail('storage migration'))
    assert b.batch(path)['processed']==5
    cfg=load_config(path);cfg['database']=str(path.parent/'missing.sqlite')
    with pytest.raises(ValueError,match='database does not exist'):Store(cfg,projection_only=True)
    assert not Path(cfg['database']).exists()


def test_worker_drains_persistent_queue_and_does_not_sleep_when_empty(historical,tmp_path,monkeypatch):
    path, store=historical
    state=tmp_path/'state'
    b.enqueue(state,[path])  # Simulate crash before a worker is started.
    waits=[]
    result=b.run(state,wait=waits.append)
    assert result==dict(processed=5,model_calls=0)
    assert b._read(b._folder(state))=={}
    waits.clear()
    assert b.run(state,wait=waits.append)['processed']==0
    assert waits==[]
    monkeypatch.setattr(b.subprocess,'Popen',lambda *a,**k:pytest.fail('completed project spawned a process'))
    queue=(b._folder(state)/'queue.json');before=queue.read_bytes()
    b.start(path,state=state)
    assert queue.read_bytes()==before


def test_busy_queue_defers_then_restarts_on_new_event(historical,tmp_path,monkeypatch):
    path, _=historical;state=tmp_path/'state'
    b.enqueue(state,[path]);original=b.batch
    monkeypatch.setattr(b,'batch',lambda p:dict(processed=0,more=True,busy=True,model_calls=0))
    waits=[]
    assert b.run(state,wait=waits.append)['processed']==0
    assert waits==list(b.BUSY_WAITS)
    assert b._read(b._folder(state))[str(path.resolve())]['state']=='waiting'
    monkeypatch.setattr(b,'batch',original)
    b.enqueue(state,[path])
    assert b.run(state,wait=lambda _:None)['processed']==5


def test_event_during_final_batch_is_not_lost(historical,tmp_path,monkeypatch):
    path, _=historical;state=tmp_path/'state';b.enqueue(state,[path])
    original=b.batch;calls=[]
    def during(p):
        calls.append(p)
        result=original(p)
        if len(calls)==1:b.enqueue(state,[path])
        return result
    monkeypatch.setattr(b,'batch',during)
    assert b.run(state,wait=lambda _:None)['processed']==5
    assert len(calls)==2 and b._read(b._folder(state))=={}


def test_one_blocked_project_does_not_prevent_other_project_progress(tmp_path,monkeypatch):
    state=tmp_path/'state';bad=tmp_path/'bad.json';good=tmp_path/'good.json'
    b.enqueue(state,[bad,good])
    def batch(path):
        if path==str(bad):raise ValueError('malformed project')
        return dict(processed=1,more=False,model_calls=0)
    monkeypatch.setattr(b,'batch',batch)
    assert b.run(state,wait=lambda _:None)['processed']==1
    assert list(b._read(b._folder(state)))==[str(bad)]
    assert b._read(b._folder(state))[str(bad)]['state']=='blocked'


def test_registry_only_enrolls_initialized_identity_matched_projects(historical,tmp_path):
    path,_=historical;state=tmp_path/'state';state.mkdir()
    (state/'projects.json').write_text(json.dumps(dict(projects=[dict(root=str(path.parent.parent)),dict(root=str(tmp_path/'missing'))])))
    b.enqueue_registered(state)
    assert list(b._read(b._folder(state)))==[str(path.resolve())]


def test_missing_tier_table_is_created_without_touching_existing_facts(historical):
    path,store=historical
    before=snapshot(store)
    store.db.execute('DROP TABLE sg_node_tiers');store.db.commit()
    assert b.batch(path)['processed']==5
    assert snapshot(store)==before
    assert tiering.summary(store)['current']


def test_disabled_project_does_not_run_or_enqueue(historical,tmp_path,monkeypatch):
    path,store=historical
    (path.parent/'disabled').touch()
    assert b.batch(path)==dict(processed=0,more=False,skipped=True,model_calls=0)
    monkeypatch.setattr(b.subprocess,'Popen',lambda *a,**k:pytest.fail('disabled project spawned'))
    state=tmp_path/'disabled-state';b.start(path,state=state)
    assert not state.exists()


def test_singleton_and_empty_exit_dont_leave_background_polling(historical,tmp_path):
    path,_=historical;state=tmp_path/'state';b.enqueue(state,[path])
    with (b._folder(state)/'worker.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert b.run(state,wait=lambda _:pytest.fail('singleton waited'))==dict(processed=0,running=True,model_calls=0)
    assert b.run(state,wait=lambda _:None)['processed']==5


def test_app_dispatcher_finishes_205_rows_from_63_without_model(historical,tmp_path):
    import os
    import subprocess
    import sys
    path,store=historical
    template=dict(store.db.execute('SELECT * FROM work_memory LIMIT 1').fetchone())
    columns=list(template)
    for i in range(200):
        row=dict(template,id=f'legacy-{i:04d}')
        store.db.execute('INSERT INTO work_memory('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',[row[c] for c in columns])
    ids=[r[0] for r in store.db.execute('SELECT id FROM work_memory ORDER BY id LIMIT 63')]
    tiering.project(store,ids);store.db.commit()
    assert tiering.summary(store)['covered']==63 and tiering.summary(store)['total']==205
    before=snapshot(store)
    state=tmp_path/'app-state';state.mkdir()
    (state/'projects.json').write_text(json.dumps(dict(projects=[dict(root=str(path.parent.parent))])))
    entrypoint=Path(__file__).resolve().parents[1]/'entrypoint.py'
    # Real dispatcher process with all sockets prohibited: no provider or server.
    code="import socket,ssl,runpy,sys; socket.socket=lambda *a,**k: (_ for _ in ()).throw(AssertionError('network forbidden')); sys.argv=[sys.argv[1],'backfill-tiers']; runpy.run_path(sys.argv[0],run_name='__main__')"
    result=subprocess.run([sys.executable,'-B','-c',code,str(entrypoint)],
        env={**os.environ,'SESSIONGRAPH_RUNTIME_HOME':str(state)},capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)==dict(processed=142,model_calls=0)
    assert tiering.summary(store)['covered']==205 and tiering.summary(store)['current']
    assert snapshot(store)==before
    assert b._read(b._folder(state))=={}


@pytest.mark.parametrize('event',['SessionStart','PostCompact','Stop','SessionEnd'])
def test_existing_hooks_wake_backfill_without_replacing_normal_behavior(historical,monkeypatch,event):
    import io
    from test_global_hooks import hook_module
    path,_=historical
    hook=hook_module();calls=[]
    monkeypatch.setattr(hook,'GLOBAL_SETTINGS',path.parent/'absent-settings.json')
    monkeypatch.setattr(hook,'_extract_context',lambda *a:None)
    monkeypatch.setattr(hook,'_spawn_cleanup',lambda *a:None)
    monkeypatch.setattr('sessiongraph.deletion_observer.start',lambda *a:None)
    monkeypatch.setattr('sessiongraph.hook_ingress.enabled',lambda *a:True)
    monkeypatch.setattr('sessiongraph.hook_ingress.notify',lambda *a:None)
    monkeypatch.setattr(b,'start',lambda p:calls.append(p))
    monkeypatch.setattr('sys.stdin',io.StringIO(json.dumps(dict(hook_event_name=event,session_id='chat-1',cwd=str(path.parent.parent)))))
    assert hook.main()==0
    assert calls==[path]


def test_pending_hook_schedules_a_single_local_worker(historical,tmp_path,monkeypatch):
    path,_=historical;state=tmp_path/'state';commands=[]
    monkeypatch.setattr(b.subprocess,'Popen',lambda argv,**kw:commands.append((argv,kw)))
    b.start(path,state=state)
    assert len(commands)==1 and commands[0][0][-3:]==['-m','sessiongraph.tier_backfill',str(state)]
    assert list(b._read(b._folder(state)))==[str(path.resolve())]
    assert commands[0][1]['start_new_session']
