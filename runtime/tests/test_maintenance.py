import fcntl
import json
import socket
from pathlib import Path

import pytest
from sessiongraph import continuation, maintenance, graph_v2 as g
from test_graph_v2 import Judge, output
from test_memories import make_store, entries, FakeExtractor


@pytest.fixture
def completed(tmp_path, monkeypatch):
    monkeypatch.delenv('SESSIONGRAPH_WORKER_LOCK',raising=False)
    monkeypatch.setenv('CODEX_HOME',str(tmp_path/'codex'))
    root,s,_ = make_store(tmp_path,[('user','训练 A 得到模型 B')])
    path=root/'.sessiongraph/config.json'
    s.config.setdefault('graph_v2',{})['enabled']=True
    g.run_chat(s,FakeExtractor([output(entries(s))]),'chat-1',jev=Judge())
    s.db.execute("UPDATE sg2_attempts SET run_id='old-run'")
    s.db.commit()
    s.config['_config_path']=str(path)
    target=s.db.execute('SELECT max(rowid) FROM entries').fetchone()[0]
    item=dict(config=str(path),chat='chat-1',state='blocked',target=target,run_id='old-run',error_code='jev_http_451')
    folder=continuation.root(s.config);folder.mkdir(parents=True,exist_ok=True)
    continuation.write(folder/'queue.json',{'old':item,'other':dict(item,config=str(tmp_path/'other/config.json'))})
    yield path,s,item
    s.close()


def test_read_reconciles_without_writes_and_cleanup_is_project_scoped(completed):
    path,s,item=completed
    q=continuation.root(s.config)/'queue.json';before=q.read_bytes()
    changed=s.db.total_changes
    assert continuation.snapshot(s.config)==[]
    assert q.read_bytes()==before and s.db.total_changes==changed
    result=maintenance.run(path,'clean_notices')
    assert result['removed']==1 and result['model_calls']==0
    assert set(json.loads(q.read_text()))=={'other'}
    assert s.db.execute("SELECT count(*) FROM sg2_attempts WHERE run_id='old-run'").fetchone()[0]>0
    assert maintenance.run(path,'clean_notices')['removed']==0


@pytest.mark.parametrize('condition',['no_target','unfinished','partial_cursor','unknown_run','import_error','running_attempt','newer_target'])
def test_never_hides_unproven_completion(completed,condition):
    path,s,item=completed
    if condition=='no_target':item.pop('target')
    if condition=='unfinished':s.db.execute("UPDATE sg2_jobs SET state='pending'")
    if condition=='partial_cursor':s.db.execute('UPDATE sg2_cursors SET line=0,start=1')
    if condition=='unknown_run':item['run_id']='unknown'
    if condition=='import_error':item['error_code']='import_failed'
    if condition=='running_attempt':s.db.execute("UPDATE sg2_attempts SET state='running'")
    if condition=='newer_target':
        s.db.execute('UPDATE sg2_cursors SET line=0,start=0');item['target']+=10
    s.db.commit()
    assert not continuation.completed_boundary(s,item)
    continuation.write(continuation.root(s.config)/'queue.json',{'old':item})
    assert continuation.snapshot(s.config)==[item]


def test_compaction_is_lossless_idempotent_model_free(completed,monkeypatch):
    path,s,_=completed
    monkeypatch.setattr(socket,'socket',lambda *a,**k:pytest.fail('network call'))
    before={name:[tuple(r) for r in s.db.execute(f'SELECT * FROM {name}')] for name in ('sg2_nodes','sg2_relations','sg2_evidence','sg_input_coverage','sg2_attempts')}
    value=json.dumps({'retained':'payload'*4000})
    s.db.execute('UPDATE sg2_jobs SET candidates_json=?',(value,));s.db.commit()
    preview=maintenance.run(path,'storage_preview')
    assert preview['before']['database_bytes']>0 and preview['model_calls']==0
    result=maintenance.run(path,'storage_compact')
    assert result['integrity']=='ok' and result['model_calls']==0
    assert result['archive']['payloads']>=1
    from sessiongraph.storage import payload_text
    assert payload_text(s,s.db.execute('SELECT candidates_json FROM sg2_jobs').fetchone()[0])==value
    for name,rows in before.items():assert [tuple(r) for r in s.db.execute(f'SELECT * FROM {name}')]==rows
    assert maintenance.run(path,'storage_compact')['archive']['payloads']==0


def test_maintenance_respects_active_batch_lock(completed):
    path,s,_=completed
    with (Path(s.config['database']).parent/'graph-v2.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(ValueError,match='maintenance_busy'):maintenance.run(path,'storage_compact')
        with pytest.raises(ValueError,match='maintenance_busy'):maintenance.run(path,'clean_notices')
        assert maintenance.run(path,'storage_preview')['model_calls']==0
