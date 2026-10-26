"""Cross-layer failure acceptance: actual adapter -> graph -> durable scheduling."""
import copy
import http.client
import json
import ssl
import time
import pytest
from sessiongraph import graph_v2 as g, jev, stages, continuation
from sessiongraph.graph_contract import policy, normalize_references
from test_memories import make_store, entries, FakeExtractor
from test_graph_v2 import output, Judge


class BrokenResponse:
    def __init__(self, exc): self.exc=exc
    def __enter__(self): return self
    def __exit__(self,*a): pass
    def read(self,*a): raise self.exc


@pytest.mark.parametrize('exc,code',[
    (ConnectionResetError(54,'connection reset'),'jev_transport'),
    (ssl.SSLEOFError(8,'EOF'),'jev_tls_handshake'),
    (http.client.IncompleteRead(b'private-partial',100),'jev_response_truncated'),
])
def test_read_failure_keeps_nodes_and_resumes_only_relations(tmp_path,monkeypatch,exc,code):
    _,s,_=make_store(tmp_path,[('assistant','训练 A 得到模型 B')])
    client=jev.JevClient({});client.key='synthetic-secret'
    monkeypatch.setattr(jev.request,'build_opener',lambda *a:type('Opener',(),{'open':lambda *a,**k:BrokenResponse(exc)})())
    monkeypatch.setattr(jev,'verified_tls_context',lambda:None)
    result=g.run_chat(s,FakeExtractor([output(entries(s))]),'chat-1',jev=client)
    assert result['failure_codes']==[code]
    assert result['pending_retry_count']==1 and result['blocked_retry_count']==0
    assert len(result['saved'])==2 and not result['saved_relations']
    assert s.db.execute('SELECT count(*) FROM sg2_cursors').fetchone()[0]==0
    failed=s.db.execute("SELECT * FROM sg2_attempts WHERE state='failed'").fetchall()
    assert len(failed)==1 and failed[0]['stage']=='relations'
    assert json.loads(failed[0]['stats_json'])['jev_calls']==1
    assert 'private-partial' not in failed[0]['error_json']
    assert s.db.execute('SELECT state FROM sg2_control').fetchone()[0]=='retry_wait'
    traces=result['request_traces'];assert traces and traces[-1]['path']
    record=json.loads(__import__('pathlib').Path(traces[-1]['path']).read_text())
    assert record['job_id'] and record['attempt_id'] and record['stage']=='relations'
    assert record['failure']['code']==code and 'synthetic-secret' not in json.dumps(record)
    recovered=g.run_chat(s,FakeExtractor([]),'chat-1',jev=Judge())
    assert recovered['model_calls']==1 and not recovered['saved'] and len(recovered['saved_relations'])==1
    assert s.db.execute('SELECT count(*) FROM sg2_nodes').fetchone()[0]==2
    assert s.db.execute('SELECT error FROM sg2_jobs').fetchone()[0] is None
    assert 'failure_reason' not in json.loads(s.db.execute('SELECT stats_json FROM sg2_jobs').fetchone()[0])
    replay=g.run_chat(s,FakeExtractor([]),'chat-1',jev=Judge())
    assert replay['model_calls']==0


def test_declared_labels_are_not_database_ids_and_dangling_edges_stay_invalid(tmp_path):
    _,s,_=make_store(tmp_path,[('assistant','训练 A 得到模型 B')]);o=output(entries(s))
    o['nodes'][0]['id']='wmv2_unoffered';o['relations'][0]['source']='wmv2_unoffered'
    r=g.run_chat(s,FakeExtractor([o]),'chat-1',jev=Judge())
    assert len(r['saved'])==2 and 'wmv2_unoffered' not in r['saved']
    assert len(r['saved_relations'])==1 and r['graph_v2']['schema_repairs']==0
    malformed=copy.deepcopy(o);malformed['relations'][0]['source']='n1'
    fixed=normalize_references(malformed,[])
    assert fixed['nodes'][0]['id']!='n1' and fixed['relations'][0]['source']=='n1'
    schema=policy([])['json_schema']
    assert schema['properties']['nodes']['items']['properties']['id']['enum']==[f'n{i}' for i in range(1,13)]


def test_blocked_batch_does_not_prevent_an_independent_turn(tmp_path):
    _,s,_=make_store(tmp_path,[('user','训练 A 得到模型 B'),('assistant','已保存。'),('user','评估 C 生成报告 D')])
    class Extract:
        def __init__(self):self.calls=0
        def __call__(self,batch,policy):
            self.calls+=1
            # Fixed bounded evidence, even when the planner adds context-only entries.
            chosen=next(e for e in batch if not e.get('context_only'))
            o=output([chosen],title='运行 '+str(self.calls),artifact='产物 '+str(self.calls))
            o['coverage']['entry_ids']=[e['id'] for e in batch]
            return o
    class Flaky(Judge):
        def evaluate(self,state,questions):
            if self.calls==0:
                self.calls+=1
                raise jev.JevUnavailable('jev_http_451',False)
            return super().evaluate(state,questions)
    ex=Extract();r=g.run_chat(s,ex,'chat-1',jev=Flaky(),respect_schedule=True,step_budget=4)
    assert ex.calls>=2
    assert s.db.execute("SELECT count(*) FROM sg2_jobs WHERE state='complete'").fetchone()[0]>=1
    assert r['graph_v2']['blocked_jobs']==1 and r['graph_v2']['source_remaining']
    assert not r['graph_v2']['runnable_remaining']
    # Cursor must not pass reserved but unfinished old input.
    assert s.db.execute('SELECT count(*) FROM sg2_claims').fetchone()[0]>0
    recovery=g.run_chat(s,FakeExtractor([]),'chat-1',jev=Judge(),respect_schedule=False)
    assert recovery['graph_v2']['deepseek_calls']==0
    assert s.db.execute('SELECT count(*) FROM sg2_claims').fetchone()[0]==0


def test_scheduler_prefers_runnable_work_and_other_job_retry(tmp_path):
    _,s,_=make_store(tmp_path,[('user','目标')]);path=__import__('pathlib').Path(s.config['database']).parent/'config.json'
    run=dict(run_id='fixture',chat_id='chat-1',event='PostCompact',state='failed',error_code='jev_http_451',updated_at=100,
        blocked_retry_count=1,pending_retry_count=1,graph_v2=dict(durable_stages=True,source_remaining=True,runnable_remaining=True,next_retry_at=500,blocked_jobs=1))
    continuation.record(path,run,launch=False,now=100)
    assert continuation.snapshot(s.config)[0]['state']=='ready'
    run['updated_at']=200;run['graph_v2']['runnable_remaining']=False
    continuation.record(path,run,launch=False,now=200)
    state=continuation.snapshot(s.config)[0]
    assert state['state']=='retry_wait' and state['due']==500


def test_ambiguous_duplicate_ids_are_not_silently_merged(tmp_path):
    _,s,_=make_store(tmp_path,[('user','训练 A 得到模型 B')]);bad=output(entries(s));bad['nodes'][1]['id']='n1'
    r=g.run_chat(s,FakeExtractor([bad,output(entries(s))]),'chat-1',jev=Judge())
    assert r['graph_v2']['schema_repairs']==1 and len(r['saved_relations'])==1
    assert s.db.execute("SELECT count(*) FROM sg2_attempts WHERE state='failed' AND stage='validate'").fetchone()[0]==1


def test_historical_run_recovery_is_a_read_only_projection(tmp_path,monkeypatch):
    from sessiongraph.auto_update import update_status, _folder
    _,s,_=make_store(tmp_path,[('user','训练 A 得到模型 B')])
    monkeypatch.setenv('SESSIONGRAPH_RUN_ID','failedrun')
    class Fail(Judge):
        def evaluate(self,*a):raise jev.JevUnavailable('jev_transport',True)
    g.run_chat(s,FakeExtractor([output(entries(s))]),'chat-1',jev=Fail())
    folder=_folder(s.config);folder.mkdir(parents=True,exist_ok=True)
    path=folder/'failedrun.json';path.write_text(json.dumps(dict(run_id='failedrun',chat_id='chat-1',state='failed',error_code='jev_transport',created_at=1,updated_at=2)))
    assert not update_status(s.config)['runs'][0].get('batches_recovered')
    monkeypatch.setenv('SESSIONGRAPH_RUN_ID','recovery')
    g.run_chat(s,FakeExtractor([]),'chat-1',jev=Judge())
    before=path.read_bytes()
    status=update_status(s.config)
    assert status['runs'][0]['batches_recovered'] and status['runs'][0]['state']=='failed'
    assert path.read_bytes()==before and status['model_calls']==0


def test_nonretryable_io_is_not_mislabeled_as_network(tmp_path,monkeypatch):
    _,s,_=make_store(tmp_path,[('user','训练 A 得到模型 B')])
    monkeypatch.setattr(g,'_commit',lambda *a,**k:(_ for _ in ()).throw(PermissionError(13,'permission denied')))
    r=g.run_chat(s,FakeExtractor([output(entries(s))]),'chat-1',jev=Judge())
    assert r['failure_codes']==['graph_io'] and r['blocked_retry_count']==1
    info=json.loads(s.db.execute('SELECT error_json FROM sg2_control').fetchone()[0])
    assert info['exception_type']=='PermissionError' and info['stage']=='publish_nodes'


def test_missing_endpoint_has_one_evidence_preserving_correction(tmp_path):
    _,s,_=make_store(tmp_path,[('user','训练 A 得到模型 B')]);bad=output(entries(s));bad['relations'][0]['target']='n12'
    r=g.run_chat(s,FakeExtractor([bad,output(entries(s))]),'chat-1',jev=Judge())
    assert r['graph_v2']['schema_repairs']==1 and len(r['saved_relations'])==1
    assert r['graph_v2']['deepseek_calls']==2


def test_pending_job_from_a_newer_frontier_is_not_replayed(tmp_path):
    _,s,_=make_store(tmp_path,[('user','训练 A 得到模型 B')]);o=output(entries(s))
    r=g.run_chat(s,FakeExtractor([o]),'chat-1',jev=Judge(),call_budget=1)
    assert r['failure_codes']==['graph_call_budget_pending']
    # All actual entry rowids are > 0; the frozen empty frontier must not resume that job.
    older=g.run_chat(s,FakeExtractor([]),'chat-1',jev=Judge(),through_rowid=0)
    assert older['model_calls']==0 and not older['graph_v2']['source_remaining']
    assert s.db.execute("SELECT count(*) FROM sg2_jobs WHERE state='pending'").fetchone()[0]==1


def test_old_blocked_failure_is_backlog_not_this_round_failure(tmp_path):
    _,s,_=make_store(tmp_path,[('user','训练 A 得到模型 B')])
    class Fail(Judge):
        def evaluate(self,*a):raise jev.JevUnavailable('jev_http_451',False)
    initial=g.run_chat(s,FakeExtractor([output(entries(s))]),'chat-1',jev=Fail())
    assert initial['failure_codes']==['jev_http_451']
    result=g.run_chat(s,FakeExtractor([]),'chat-1',jev=Judge(),respect_schedule=True,pending_only=True)
    assert result['model_calls']==0
    assert result['failed']==[] and result['failure_codes']==[]
    assert result['blocked_retry_count']==0
    assert result['backlog_failures'][0]['error_code']=='jev_http_451'
    assert result['graph_v2']['blocked_jobs']==1
    assert not result['graph_v2']['runnable_remaining']


def test_successful_new_work_does_not_inherit_blocked_batch_error(tmp_path):
    _,s,_=make_store(tmp_path,[('user','训练 A 得到模型 B'),('assistant','已保存。'),('user','评估 C 生成报告 D')])
    class Extract:
        calls=0
        def __call__(self,batch,policy):
            self.calls+=1
            chosen=next(e for e in batch if not e.get('context_only'))
            o=output([chosen],title='运行 '+str(self.calls),artifact='产物 '+str(self.calls))
            o['coverage']['entry_ids']=[e['id'] for e in batch]
            return o
    class Fail(Judge):
        def evaluate(self,*a):raise jev.JevUnavailable('jev_http_451',False)
    ex=Extract()
    first=g.run_chat(s,ex,'chat-1',jev=Fail(),respect_schedule=True,step_budget=1)
    assert first['failure_codes']==['jev_http_451']
    second=g.run_chat(s,ex,'chat-1',jev=Judge(),respect_schedule=True,step_budget=4)
    assert second['saved_relations'] and second['model_calls']>0
    assert not second['failed'] and not second['failure_codes']
    assert second['backlog_failures'][0]['error_code']=='jev_http_451'
    assert second['graph_v2']['blocked_jobs']==1


@pytest.mark.parametrize('retryable,expected',[(False,'blocked'),(True,'retry_wait')])
def test_backlog_only_status_keeps_scheduler_reason_and_due(tmp_path,retryable,expected):
    _,s,_=make_store(tmp_path,[('user','目标')]);path=__import__('pathlib').Path(s.config['database']).parent/'config.json'
    run=dict(run_id='fixture',chat_id='chat-1',event='PostCompact',state='pending',error_code=None,updated_at=100,
        failed_batches=0,pending_retry_count=0,blocked_retry_count=0,
        backlog_failures=[dict(job_id='old',error_code='jev_transport' if retryable else 'jev_http_451',retryable=retryable)],
        graph_v2=dict(durable_stages=True,source_remaining=True,runnable_remaining=False,next_retry_at=500 if retryable else None))
    continuation.record(path,run,launch=False,now=100)
    state=continuation.snapshot(s.config)[0]
    assert state['state']==expected
    assert state['error_code']==run['backlog_failures'][0]['error_code']
    if retryable:assert state['due']==500
