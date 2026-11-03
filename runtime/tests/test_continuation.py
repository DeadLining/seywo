import json
import os
import subprocess
import sys
import time
from pathlib import Path
import pytest
from sessiongraph import continuation as c
from sessiongraph.auto_update import run_update
from sessiongraph.core import Store, init_project, load_config


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv('SESSIONGRAPH_WORKER_LOCK',str(tmp_path/'runtime/extract.lock'))
    codex_home=tmp_path/'codex';codex_home.mkdir()
    monkeypatch.setenv('CODEX_HOME',str(codex_home))
    (codex_home/'sessiongraph.json').write_text(json.dumps({'enabled':True}))
    path=init_project(tmp_path/'project')
    config=load_config(path)
    Store(config).close()
    raw=json.loads(path.read_text());raw['graph_v2']={'enabled':True};path.write_text(json.dumps(raw))
    return path,load_config(path)


def receipt(**extra):
    return dict({'run_id':'run1','chat_id':'chat','event':'PostCompact','state':'pending','error_code':'graph_call_budget_pending','target_rowid':10,'updated_at':100,'graph_v2':{'source_remaining':True}},**extra)


def test_coalesce_immediate_complete_and_read_is_side_effect_free(configured):
    path,config=configured
    c.record(path,receipt(),launch=False,now=100)
    c.record(path,receipt(),launch=False,now=100)
    rows=c.snapshot(config)
    assert len(rows)==1 and rows[0]['due']==100 and rows[0]['state']=='ready'
    assert not c.process_one(c.root(config),now=99)
    c.record(path,receipt(state='succeeded',error_code=None,graph_v2={'source_remaining':False},updated_at=300),launch=False,now=300)
    assert c.snapshot(config)==[]
    assert c.read(c.root(config)/'queue.json',{})=={}


def test_retries_backoff_then_block_without_losing_queue(configured):
    path,config=configured
    for i in range(1,5):
        c.record(path,receipt(event='AutoContinue',state='failed',error_code='extraction_failed',updated_at=i*100),launch=False,now=i*100)
        item=c.snapshot(config)[0]
        assert item['failures']==i
        assert item['state']==('blocked' if i==4 else 'retry_wait')
    assert not c.process_one(c.root(config),now=99999)


@pytest.mark.parametrize('deadline', [50, 0, None])
def test_circuit_wait_overrides_expired_batch_and_fresh_work(configured, deadline):
    path, config = configured
    c.record(path, receipt(chat_id='other'), launch=False, now=100)
    for stamp in (100, 101, 102):
        c.record(path, receipt(event='AutoContinue', state='pending',
            updated_at=stamp, error_code='resource_circuit_open',
            graph_v2={'durable_stages':True, 'source_remaining':True,
                      'runnable_remaining':True, 'next_retry_at':deadline},
            backlog_failures=[{'error_code':'resource_circuit_open', 'retryable':True,
                               'failure':{'retry_at':160}}]), launch=False, now=stamp)
        items = {item['chat']:item for item in c.snapshot(config)}
        assert items['chat']['state'] == 'retry_wait' and items['chat']['due'] == 160
        assert items['chat']['failures'] == 0 and items['chat']['no_progress'] == 0
        assert items['chat']['target'] == 10 and items['other']['state'] == 'ready'
    c.record(path, receipt(chat_id='other', state='succeeded', error_code=None,
        graph_v2={'source_remaining':False}, updated_at=103), launch=False, now=103)
    assert not c.process_one(c.root(config), now=159, project=str(path),
                             execute=lambda *_:pytest.fail('cooldown sent a request'))


def test_circuit_receipt_without_valid_deadline_never_requeues_immediately(configured):
    path, config = configured
    c.record(path, receipt(event='AutoContinue', state='failed', error_code='resource_circuit_open',
        graph_v2={'durable_stages':True, 'source_remaining':True,
                  'runnable_remaining':True, 'next_retry_at':50}), launch=False, now=100)
    item = c.snapshot(config)[0]
    assert item['state'] == 'retry_wait' and item['due'] == 220
    assert not c.process_one(c.root(config), now=219,
                             execute=lambda *_:pytest.fail('cooldown sent a request'))


@pytest.mark.parametrize('deadline', [50, 160, None])
def test_provider_failure_yields_despite_fresh_runnable_source(configured, deadline):
    path, config = configured
    c.record(path, receipt(chat_id='other'), launch=False, now=100)
    c.record(path, receipt(event='AutoContinue', state='failed',
        error_code='provider_http_502', pending_retry_count=1,
        graph_v2={'durable_stages': True, 'source_remaining': True,
                  'runnable_remaining': True, 'next_retry_at': deadline}), launch=False, now=100)
    items = {item['chat']: item for item in c.snapshot(config)}
    assert items['chat']['state'] == 'retry_wait'
    assert items['chat']['due'] == (160 if deadline == 160 else 220)
    assert items['chat']['failures'] == 1 and items['chat']['target'] == 10
    assert items['other']['state'] == 'ready'


def test_permanent_label_failure_does_not_extract_fresh_source(configured):
    path, config = configured
    c.record(path, receipt(event='AutoContinue', state='failed',
        error_code='single_token_label_invalid', pending_retry_count=0,
        graph_v2={'durable_stages': True, 'source_remaining': True,
                  'runnable_remaining': True}), launch=False, now=100)
    item = c.snapshot(config)[0]
    assert item['state'] == 'blocked' and item['target'] == 10
    assert not c.process_one(c.root(config), now=1000,
                             execute=lambda *_:pytest.fail('invalid route sent another request'))


def test_one_due_continuation_uses_fixed_target_and_finishes(configured):
    path,config=configured
    c.record(path,receipt(target_rowid=0),launch=False,now=100)
    seen=[]
    def execute(argv,timeout):
        seen.append(argv)
        if 'refresh-chat' in argv:return {'added_entries':0,'skipped':[]}
        return {'saved':['n'],'saved_relations':[],'failed':[],'model_calls':2,'graph_v2':{'source_remaining':False}}
    assert c.process_one(c.root(config),now=220,execute=execute)
    assert c.snapshot(config)==[]
    worker=seen[-1]
    assert worker[worker.index('--memories-through-rowid')+1]=='0'
    assert '--memories-call-budget' not in worker
    assert not (c.root(config)/'budget.json').exists()


@pytest.mark.parametrize('old_ledger', ['{"used":64,"reservations":{"old":64}}', 'corrupt historical ledger'])
def test_automatic_dispatch_continues_beyond_64_calls_without_daily_ledger(configured, old_ledger):
    path,config=configured
    folder=c.root(config);folder.mkdir(parents=True,exist_ok=True)
    ledger=folder/'budget.json';ledger.write_text(old_ledger)
    dispatches=[]
    def execute(argv,timeout):
        if 'refresh-chat' in argv:return {'added_entries':0,'skipped':[]}
        dispatches.append(argv)
        return {'saved':[], 'failed':[], 'model_calls':8, 'graph_v2':{'source_remaining':False}}
    for _ in range(10):
        result=run_update(path,'chat','AutoContinue',execute=execute)
        assert result['state']=='succeeded'
        assert result['reported_model_calls']==8
    assert len(dispatches)==10  # 80 calls in one day, no daily admission limit.
    assert ledger.read_text()==old_ledger


def test_legacy_daily_wait_resumes_now_and_preserves_target(configured, monkeypatch):
    path,config=configured
    folder=c.root(config);folder.mkdir(parents=True,exist_ok=True)
    c.write(folder/'queue.json',{c.key(path,'chat'):{
        'config':str(path),'chat':'chat','state':'daily_limit','due':9999999999,
        'error_code':'daily_call_budget_pending','target':42,'updated_at':100,'run_id':'old',
    }})
    before=(folder/'queue.json').read_bytes()
    assert c.snapshot(config)[0]['state']=='daily_limit'
    assert (folder/'queue.json').read_bytes()==before  # UI read never migrates/dispatches.
    seen=[]
    def update(config_path,chat,event,**kwargs):
        seen.append(kwargs['through_rowid'])
        return receipt(event=event,state='succeeded',error_code=None,target_rowid=42,updated_at=201,graph_v2={'source_remaining':False})
    monkeypatch.setattr('sessiongraph.auto_update.run_update',update)
    assert c.process_one(folder,now=200)
    assert seen==[42]
    assert c.snapshot(config)==[]


def test_releasing_daily_limits_preserves_other_queue_states():
    jobs={'old':{'state':'daily_limit','due':9999,'target':12},
          'failed':{'state':'blocked','due':99,'error_code':'provider_http_451'},
          'active':{'state':'running','due':88},'cool':{'state':'cooldown','due':900}}
    before=json.loads(json.dumps(jobs))
    assert c.release_legacy_waits(jobs,200)
    assert jobs['old']['due']==200 and jobs['old']['target']==12
    for key in ('failed','active'):assert jobs[key]==before[key]
    assert jobs['cool']=={**before['cool'],'state':'retry_wait'}
    assert not c.release_legacy_waits(jobs,201)


def test_disabling_hook_cancels_due_work(configured):
    path,config=configured
    c.record(path,receipt(),launch=False,now=100)
    raw=json.loads(path.read_text());raw['hook']={'enabled':False};path.write_text(json.dumps(raw))
    assert c.process_one(c.root(config),now=220,execute=lambda *_:pytest.fail('model called'))
    assert c.snapshot(config)==[]


def test_empty_queue_does_not_leave_a_resident_runner(configured):
    if not hasattr(c.select,'kqueue'):pytest.skip('macOS kqueue')
    path,config=configured
    folder=c.root(config);folder.mkdir(parents=True)
    c.write(folder/'queue.json',{})
    result=subprocess.run([sys.executable,'-m','sessiongraph.continuation','--root',str(folder),'--project',str(path)],timeout=5,capture_output=True)
    assert result.returncode==0,result.stderr


def test_kernel_wait_wakes_on_queue_write_and_exits(configured):
    if not hasattr(c.select,'kqueue'):pytest.skip('macOS kqueue')
    path,config=configured
    c.record(path,receipt(state='failed',error_code='extraction_failed'),launch=False,now=time.time()+3600)
    folder=c.root(config)
    child=subprocess.Popen([sys.executable,'-m','sessiongraph.continuation','--root',str(folder),'--project',str(path)],stderr=subprocess.PIPE)
    try:
        # Wait for the singleton lock rather than timing a provider or polling production.
        import fcntl
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            with c.project_lock(folder, path).open('a') as lock:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:break
            time.sleep(.01)
        else:pytest.fail('scheduler did not start')
        c.write(folder/'queue.json',{})
        assert child.wait(timeout=5)==0
    finally:
        if child.poll() is None:child.kill();child.wait()


def test_newer_target_survives_old_continuation_completion(configured):
    path,config=configured
    c.record(path,receipt(target_rowid=20,updated_at=200),launch=False,now=200)
    c.record(path,receipt(event='AutoContinue',state='succeeded',target_rowid=10,updated_at=300,graph_v2={'source_remaining':False}),launch=False,now=300)
    assert c.snapshot(config)[0]['target']==20


def test_zero_progress_is_bounded_and_other_chats_survive(configured):
    path,config=configured
    c.record(path,receipt(chat_id='other',graph_v2={'source_remaining':True,'input_source_chars':100}),launch=False,now=100)
    for i in range(1,4):
        c.record(path,receipt(event='AutoContinue',updated_at=i*100),launch=False,now=i*100)
    by_chat={item['chat']:item for item in c.snapshot(config)}
    assert by_chat['chat']['state']=='blocked'
    assert by_chat['chat']['error_code']=='continuation_no_progress'
    assert by_chat['other']['state']=='ready'


def test_uninstalled_global_hook_cancels_due_work(configured):
    path, config = configured
    c.record(path, receipt(), launch=False, now=100)
    (Path(os.environ['CODEX_HOME'])/'sessiongraph.json').write_text('{"enabled":false}')
    assert c.process_one(c.root(config), now=220, execute=lambda *_: pytest.fail('dispatch after uninstall'))
    assert c.snapshot(config) == []
    result = run_update(path, 'chat', 'AutoContinue', execute=lambda *_: pytest.fail('dispatch after uninstall'))
    assert result['state'] == 'cancelled' and result['reported_model_calls'] == 0


def test_next_round_is_ready_when_execution_finishes(configured, monkeypatch):
    path, config = configured
    c.record(path, receipt(), launch=False, now=100)
    clock = [220]
    monkeypatch.setattr(c.time, 'time', lambda: clock[0])
    def execute(*args, **kwargs):
        clock[0] = 1000
        return receipt(event='AutoContinue', updated_at=1000, graph_v2={'source_remaining':True,'nodes_saved':1})
    monkeypatch.setattr('sessiongraph.auto_update.run_update', execute)
    assert c.process_one(c.root(config))
    assert c.snapshot(config)[0]['due'] == 1000
    assert c.snapshot(config)[0]['state'] == 'ready'


def test_runner_startup_wakes_legacy_daily_limit_without_new_compact(configured):
    if not hasattr(c.select,'kqueue'):pytest.skip('macOS kqueue')
    path,config=configured
    raw=json.loads(path.read_text());raw['hook']={'enabled':False};path.write_text(json.dumps(raw))
    folder=c.root(config);folder.mkdir(parents=True,exist_ok=True)
    c.write(folder/'queue.json',{c.key(path,'chat'):{
        'config':str(path),'chat':'chat','state':'daily_limit','due':time.time()+86400,
        'error_code':'daily_call_budget_pending','target':42,'updated_at':100,'run_id':'old',
    }})
    # Disabled fixture must be dequeued immediately, never dispatching a provider.
    result=subprocess.run([sys.executable,'-m','sessiongraph.continuation','--root',str(folder),'--project',str(path)],timeout=5,capture_output=True)
    assert result.returncode==0,result.stderr
    assert c.snapshot(config)==[]


def test_normal_legacy_cooldown_is_released_but_error_retry_time_is_kept():
    jobs={'normal':{'state':'cooldown','due':9999,'error_code':'graph_call_budget_pending','target':42},
          'failed':{'state':'cooldown','due':9999,'error_code':'extraction_failed'}}
    assert c.release_legacy_waits(jobs,200)
    assert jobs['normal']['state']=='ready' and jobs['normal']['due']==200
    assert jobs['normal']['target']==42
    assert jobs['failed']['state']=='retry_wait' and jobs['failed']['due']==9999


def test_ready_rounds_yield_to_other_chats_without_sleep(configured,monkeypatch):
    path,config=configured
    for chat in ('a','b'):
        c.record(path,receipt(chat_id=chat,graph_v2={'source_remaining':True,'input_source_chars':1}),launch=False,now=100)
    seen=[]
    clock=[200]
    monkeypatch.setattr(c.time,'time',lambda:clock[0])
    def update(config_path,chat,event,**kwargs):
        seen.append(chat);clock[0]+=1
        return receipt(chat_id=chat,event=event,updated_at=clock[0],graph_v2={'source_remaining':True,'input_source_chars':1})
    monkeypatch.setattr('sessiongraph.auto_update.run_update',update)
    assert c.process_one(c.root(config))
    assert c.process_one(c.root(config))
    assert set(seen)=={'a','b'}
    assert c.process_one(c.root(config))  # No timer advance beyond execution itself.
    assert seen[2]==seen[0]


def test_success_with_remaining_input_does_not_spin_without_progress(configured):
    path,config=configured
    for i in range(3):
        c.record(path,receipt(event='AutoContinue',state='succeeded',error_code=None,
            updated_at=100+i,input_stats=None),launch=False,now=100+i)
    assert c.snapshot(config)[0]['state']=='blocked'
    assert c.snapshot(config)[0]['error_code']=='continuation_no_progress'


def test_project_runner_never_claims_another_projects_job(configured, tmp_path, monkeypatch):
    path, config = configured
    other = init_project(tmp_path / 'other')
    Store(load_config(other)).close()
    c.record(other, receipt(chat_id='other'), launch=False, now=50)
    c.record(path, receipt(), launch=False, now=100)
    seen = []
    def update(config_path, chat, event, **kwargs):
        seen.append(str(config_path))
        return receipt(chat_id=chat, event=event, state='succeeded', error_code=None,
                       updated_at=300, graph_v2={'source_remaining':False})
    monkeypatch.setattr('sessiongraph.auto_update.run_update', update)
    assert c.process_one(c.root(config), project=str(path.resolve()), now=200)
    assert seen == [str(path.resolve())]
    assert c.snapshot(config) == []
    assert len(c.snapshot(load_config(other))) == 1


def test_dispatch_starts_all_projects_without_waiting(configured, tmp_path, monkeypatch):
    path, config = configured
    other = init_project(tmp_path / 'other')
    Store(load_config(other)).close()
    for cfg in (path, other):
        c.record(cfg, receipt(), launch=False, now=100)
    launched = []
    class Child:
        def wait(self, *a, **kw):
            pytest.fail('dispatcher waited for another project')
    def launch(argv, **kw):
        launched.append(argv[argv.index('--project') + 1])
        return Child()
    monkeypatch.setattr(c.subprocess, 'Popen', launch)
    c.run(c.root(config))
    assert set(launched) == {str(path.resolve()), str(other.resolve())}
    assert c.project_lock(c.root(config), path) != c.project_lock(c.root(config), other)


def test_real_project_runners_overlap_and_duplicate_runner_exits(configured, tmp_path, monkeypatch):
    if not hasattr(c.select, 'kqueue'):
        pytest.skip('macOS kqueue')
    path, config = configured
    other = init_project(tmp_path / 'other')
    Store(load_config(other)).close()
    for cfg in (path, other):
        c.record(cfg, receipt(target_rowid=0), launch=False, now=time.time())
    folder = c.root(config)
    signals = tmp_path / 'signals'
    signals.mkdir()
    script = '''
import sys,time
from pathlib import Path
from sessiongraph import continuation as c, auto_update as a
folder, project, signals = map(Path, sys.argv[1:])
original = a.run_update
calls = 0
def execute(argv, timeout):
    global calls
    if "sessiongraph.gateway_worker" in argv:
        marker = signals / c.key(project, "")
        marker.write_text("entered")
        deadline = time.monotonic() + 8
        while not (signals / "release").exists():
            if time.monotonic() > deadline: raise TimeoutError("barrier")
            time.sleep(.01)
    if 'refresh-chat' in argv:
        return {"added_entries":0,"skipped":[]}
    return {"saved":[],"failed":[],"model_calls":0,"graph_v2":{"source_remaining":False}}
def update(*args, **kwargs):
    kwargs["execute"] = execute
    return original(*args, **kwargs)
a.run_update = update
c.run(folder, project)
'''
    popen = subprocess.Popen
    children = []
    def launch(argv, **kwargs):
        project = argv[argv.index('--project') + 1]
        child = popen([sys.executable, '-c', script, str(folder), project, str(signals)],
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        children.append(child)
        return child
    monkeypatch.setattr(c.subprocess, 'Popen', launch)
    try:
        c.dispatch(folder)
        deadline = time.monotonic() + 5
        while not all((signals / c.key(cfg, '')).exists() for cfg in (path, other)):
            assert time.monotonic() < deadline, 'projects did not enter concurrently'
            time.sleep(.01)
        duplicate = popen([sys.executable, '-c', script, str(folder), str(path), str(signals)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        children.append(duplicate)
        assert duplicate.wait(timeout=3) == 0
        (signals / 'release').touch()
        for child in children:
            assert child.wait(timeout=5) == 0, child.stderr.read().decode()
        assert c.snapshot(config) == []
        assert c.snapshot(load_config(other)) == []
    finally:
        (signals / 'release').touch()
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


def test_durable_zero_progress_failure_is_not_requeued(configured):
    path, config = configured
    c.record(path, receipt(state='failed', error_code='continuation_no_progress',
        graph_v2={'durable_stages':True,'source_remaining':True,'runnable_remaining':True}), launch=False)
    assert c.snapshot(config)[0]['state'] == 'blocked'


def test_missing_import_does_not_spin_on_ready_graph_jobs(configured):
    path,config=configured
    c.record(path,receipt(event='AutoContinue',state='failed',error_code='import_source_missing',
        graph_v2={'durable_stages':True,'source_remaining':True,'runnable_remaining':True}),launch=False,now=100)
    item=c.snapshot(config)[0]
    assert item['state']=='blocked' and item['error_code']=='import_source_missing'
    assert not c.process_one(c.root(config),now=100000)
