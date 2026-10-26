"""Cross-boundary regressions: accounting, restart, audit isolation and wire policy."""
import json
from types import SimpleNamespace
from urllib.request import Request
import pytest
from sessiongraph import graph_v2 as g, stages, request_audit
from sessiongraph.core import Store
from sessiongraph.graph_contract import policy, prepare_evidence_spans, prompt_policy
from test_memories import make_store, entries, FakeExtractor
from test_graph_v2 import output, Judge


def test_wire_ids_match_schema_and_no_conflicting_range():
    _, wire, _ = prepare_evidence_spans([{'id':'e1','text':'训练 A 生成报告 B'}], policy([]))
    prompt = prompt_policy(wire)
    assert 'n999' not in prompt['instructions']
    assert 'n1..n12' in prompt['instructions']


def test_deepseek_attempt_counts_include_actual_request(tmp_path):
    _, s, _ = make_store(tmp_path, [('assistant', '训练 A 得到模型 B')])
    result = g.run_chat(s, FakeExtractor([output(entries(s))]), 'chat-1', jev=Judge())
    attempts = [json.loads(r[0]) for r in s.db.execute("SELECT stats_json FROM sg2_attempts WHERE stage='extract'")]
    assert sum(a['deepseek_calls'] for a in attempts) == result['graph_v2']['deepseek_calls'] == 1


def test_restart_after_extract_checkpoint_uses_no_second_deepseek(tmp_path, monkeypatch):
    _, s, _ = make_store(tmp_path, [('assistant', '训练 A 得到模型 B')])
    real = stages.execute
    def crash(store, job, part, stage, *args, **kw):
        if stage == 'validate': raise KeyboardInterrupt('simulated process exit after extraction')
        return real(store, job, part, stage, *args, **kw)
    monkeypatch.setattr(stages, 'execute', crash)
    with pytest.raises(KeyboardInterrupt):
        g.run_chat(s, FakeExtractor([output(entries(s))]), 'chat-1', jev=Judge())
    config=s.config; s.close(); s=Store(config)
    monkeypatch.setattr(stages, 'execute', real)
    cached=FakeExtractor([]);cached.last_usage={'input_tokens':999999,'output_tokens':999999}
    result=g.run_chat(s, cached, 'chat-1', pending_only=True, jev=Judge())
    assert not result['failed'] and result['graph_v2']['deepseek_calls']==0
    assert len(result['saved_relations'])==1
    assert all(u['provider']!='deepseek' for u in result['graph_v2']['usage'])
    assert g.run_chat(s, FakeExtractor([]), 'chat-1', jev=Judge())['model_calls']==0


@pytest.mark.parametrize('payload', [
    {'choices': {'wrong': 'shape'}, 'output': None},
    {'choices': [{'message': None}]},
    {'output': [None, {'type':'message','content':None}]},
    {'output': [{'type':'message','content':[{'type':'output_text','text':123}]}]},
])
def test_diagnostic_reader_preserves_malformed_protocol_bytes(tmp_path, monkeypatch, payload):
    _, s, _=make_store(tmp_path, [('assistant','完成')])
    raw=json.dumps(payload).encode()
    class Response:
        status=200; headers={}
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def read(self,*a): return raw
    monkeypatch.setattr('urllib.request.build_opener', lambda *a:SimpleNamespace(open=lambda *a,**k:Response()))
    adapter=SimpleNamespace(config=s.config,key='',model='fixture',max_output=12000,timeout=10,traces=[])
    req=Request('http://127.0.0.1/v1/responses',data=b'{}')
    assert request_audit.read_response(adapter,req,[])==raw
    record=json.loads(__import__('pathlib').Path(adapter.traces[0]['path']).read_text())
    assert record['http_status']==200 and record['state']!='transport_failed'


def test_validation_cannot_relabel_an_earlier_request(tmp_path):
    from pathlib import Path
    path=tmp_path/'prior.json'
    path.write_text(json.dumps({'state':'validated','job_id':'older','part':'old-part'}))
    adapter=SimpleNamespace(traces=[{'path':str(path)}],key='')
    request_audit.validation_result(adapter, {'code':'graph_validation'}, job_id='current', part='current-part')
    assert json.loads(path.read_text())['state']=='validated'


def test_secret_only_invalid_response_is_never_checkpointed(tmp_path, monkeypatch):
    _, s, _=make_store(tmp_path,[('assistant','训练 A 得到模型 B')])
    candidate=output(entries(s));candidate['nodes'][0]['content']='api_key=fixture-secret-123456'
    real=stages.execute
    def crash(store,job,part,stage,*a,**kw):
        if stage=='validate':raise KeyboardInterrupt()
        return real(store,job,part,stage,*a,**kw)
    monkeypatch.setattr(stages,'execute',crash)
    with pytest.raises(KeyboardInterrupt):
        g.run_chat(s,FakeExtractor([candidate]),'chat-1',jev=Judge())
    assert s.db.execute("SELECT count(*) FROM sg2_checkpoints WHERE stage='extract'").fetchone()[0]==0


def test_real_judge_adapter_receipt_is_validated(tmp_path,monkeypatch):
    from sessiongraph.jev import JevClient
    _,s,_=make_store(tmp_path,[('assistant','训练 A 得到模型 B')])
    class Response:
        status=200;headers={}
        def __init__(self,req):
            body=json.loads(req.data)
            answers={k:{'choice':list(q['criteria'])[0], 'probabilities':{c:float(i==0) for i,c in enumerate(q['criteria'])}} for k,q in body['questions'].items()}
            self.raw=json.dumps({'answers':answers,'usage':{'input_tokens':10,'output_tokens':2}}).encode()
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self,*a):return self.raw
    monkeypatch.setattr('urllib.request.build_opener',lambda *a:SimpleNamespace(open=lambda req,**k:Response(req)))
    monkeypatch.setattr('sessiongraph.jev.verified_tls_context',lambda:None)
    client=JevClient({});client.key='fixture-secret'
    result=g.run_chat(s,FakeExtractor([output(entries(s))]),'chat-1',jev=client)
    assert not result['failed'] and client.calls==1
    record=json.loads(__import__('pathlib').Path(client.traces[-1]['path']).read_text())
    assert record['state']=='validated' and record['pipeline_revision']==request_audit.PIPELINE_REVISION


def test_worker_handoff_budget_is_per_invocation_not_per_segment(tmp_path):
    from test_mvp import write_session
    root,s,_=make_store(tmp_path,[('user','训练 A 得到模型 B')])
    for i in range(2):
        s.ingest(write_session(root/f'segment-{i}.jsonl',root,'chat-1',[('user',f'训练 C{i} 得到模型 D{i}')]))
    assert s.db.execute('select count(*) from session_chats').fetchone()[0]==3
    class Extract:
        calls=0
        def __call__(self,batch,contract):
            self.calls+=1
            return output(batch,quote=batch[0]['text'],title='执行 '+batch[0]['id'][-5:],artifact='产物 '+batch[0]['id'][-5:])
    ex=Extract()
    for expected in (1,2,3):
        result=g.run_chat(s,ex,'chat-1',jev=Judge(),step_budget=1)
        assert not result['failed'] and ex.calls==expected
        assert result['graph_v2']['deepseek_calls']==1
    assert not result['graph_v2']['source_remaining']
    assert s.db.execute("select count(*) from sg2_jobs where state='complete'").fetchone()[0]==3
    assert g.run_chat(s,ex,'chat-1',jev=Judge(),step_budget=1)['model_calls']==0
