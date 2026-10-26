import json
from urllib.error import HTTPError
import pytest
from test_responses import cfg
from sessiongraph.model_judge import ModelJudge, reopen_legacy_judgments
from sessiongraph.failures import Failure
from sessiongraph.graph_v2 import judge, _stats, setup
from test_memories import make_store

QUESTIONS = {'p0': {'criteria': {'same': 'same object', 'different': 'different object', 'insufficient_evidence': 'unknown'}}}
SCORES = {'p0': {'same': .99, 'different': .005, 'insufficient_evidence': .005}}


def envelope(scores=SCORES, **overrides):
    return json.dumps(dict(status='completed', usage={'input_tokens': 30, 'output_tokens': 20},
        output=[{'type':'message','role':'assistant','content':[{'type':'output_text','text':json.dumps(scores)}]}], **overrides)).encode()


def test_selected_config_and_batch_request(cfg, monkeypatch):
    seen=[]
    monkeypatch.setattr('sessiongraph.model_judge.read_response', lambda client,req,handlers: seen.append((client,req,handlers)) or envelope())
    client=ModelJudge(cfg)
    answer=client.evaluate({'original_context':'untrusted'},QUESTIONS)
    body=json.loads(seen[0][1].data)
    assert seen[0][1].full_url == cfg['provider']['endpoint']
    assert body['model'] == cfg['provider']['model']
    assert 'q0' not in body['instructions']
    assert '"p0": ["same", "different", "insufficient_evidence"]' in body['instructions']
    assert body['max_output_tokens'] == client.output_ceiling
    assert body['text']['format']['schema']['required']==['p0']
    assert answer['answers']['p0']['choice']=='same'
    assert answer['score_kind']=='model_assessed'
    assert client.calls==1
    changed={**cfg,'provider':{**cfg['provider'],'model':'next-selected-model'}}
    assert ModelJudge(changed).cache_identity != client.cache_identity


@pytest.mark.parametrize('scores', [{}, {'p0':{}}, {'p0':{'same':1}},
    {'p0':{'same':True,'different':0,'insufficient_evidence':0}},
    {'p0':{'same':.9,'different':.9,'insufficient_evidence':0}},
    {'p0':{'same':float('nan'),'different':0,'insufficient_evidence':0}},
    {'p0':{'same':-.1,'different':1.1,'insufficient_evidence':0}},
    {'p0':{'same':1,'different':0,'insufficient_evidence':0},'extra':{}}])
def test_invalid_distribution_never_publishes(cfg,monkeypatch,scores):
    monkeypatch.setattr('sessiongraph.model_judge.read_response',lambda *a:envelope(scores))
    with pytest.raises(Failure) as err:ModelJudge(cfg).evaluate({},QUESTIONS)
    assert err.value.code=='judge_schema_invalid'
    assert err.value.retryable


def test_distinct_generated_json_protocol_and_output_limit(cfg,monkeypatch):
    client=ModelJudge(cfg)
    samples=[(b'{','judge_protocol_invalid'),
        (b'{"status":"completed","output":[]}','judge_empty_output'),
        (b'{"status":"incomplete","incomplete_details":{"reason":"max_output_tokens"}}','judge_output_limit'),
        (envelope().replace(json.dumps(json.dumps(SCORES)).encode(),b'"{bad"'),'judge_generated_json_invalid')]
    for raw,code in samples:
        monkeypatch.setattr('sessiongraph.model_judge.read_response',lambda *a:raw)
        with pytest.raises(Failure) as err:client.evaluate({},QUESTIONS)
        assert err.value.code==code
    assert client.calls==len(samples)


def test_transport_does_not_fallback_to_jev(cfg,monkeypatch):
    def fail(*a):raise HTTPError('http://localhost',502,'bad',{},None)
    monkeypatch.setattr('sessiongraph.model_judge.read_response',fail)
    client=ModelJudge(cfg)
    with pytest.raises(Failure) as err:client.evaluate({},QUESTIONS)
    assert err.value.code=='provider_http_502' and err.value.provider=='selected_model'
    assert client.calls==1


def test_model_cache_and_counters(tmp_path,cfg,monkeypatch):
    _,store,_=make_store(tmp_path,[("user","evidence")]);setup(store)
    monkeypatch.setattr('sessiongraph.model_judge.read_response',lambda *a:envelope())
    monkeypatch.setattr('sessiongraph.graph_v2.validation_result',lambda *a,**kw:None)
    client=ModelJudge(cfg);stats=_stats()
    judge(store,client,'identity',{},QUESTIONS,stats)
    judge(store,client,'identity',{},QUESTIONS,stats)
    assert stats['deepseek_calls']==stats['judge_calls']==1
    assert stats['jev_calls']==0 and stats['judge_cache_hits']==1
    client.model='changed'
    judge(store,client,'identity',{},QUESTIONS,stats)
    assert stats['judge_calls']==2
    assert stats['usage'][0]['provider']=='selected_model'


def test_legacy_recovery_preserves_success_and_is_once_only(tmp_path):
    _,store,_=make_store(tmp_path,[("user","evidence")]);setup(store)
    # Minimal fixtures derive the actual table columns without changing its schema.
    columns=[r[1] for r in store.db.execute('pragma table_info(sg2_jobs)')]
    defaults=dict(project=store.config['project_id'],chat='c',segment='s',state='pending',
        input_json='{}',output_json='{"saved":"extraction"}',stats_json='{}',error='jev_http_451',created_at='now',updated_at='now')
    for jid,stage,code,state in [('old','relations','jev_http_451','pending'),('done','relations','jev_http_451','complete'),('extract','extract','provider_output_limit','pending')]:
        values={**defaults,'id':jid,'state':state}
        store.db.execute('INSERT INTO sg2_jobs('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',[values.get(k,0) for k in columns])
        info=json.dumps(dict(stage=stage,code=code))
        store.db.execute('INSERT INTO sg2_control VALUES(?,?,?,?,?)',(jid,'blocked',0,3,info))
    store.db.commit()
    assert reopen_legacy_judgments(store,'c')==['old']
    assert store.db.execute("select output_json from sg2_jobs where id='old'").fetchone()[0]=='{"saved":"extraction"}'
    assert store.db.execute("select state from sg2_control where job='extract'").fetchone()[0]=='blocked'
    store.db.execute("update sg2_control set state='blocked' where job='old'");store.db.commit()
    assert reopen_legacy_judgments(store,'c')==[]
    assert store.db.execute('select count(*) from sg2_judge_migrations').fetchone()[0]==1


def test_decimal_rounding_boundary_is_normalized_conservatively(cfg,monkeypatch):
    scores={'p0':{'same':.98,'different':.02,'insufficient_evidence':.02}}
    monkeypatch.setattr('sessiongraph.model_judge.read_response',lambda *a:envelope(scores))
    answer=ModelJudge(cfg).evaluate({},QUESTIONS)['answers']['p0']
    assert sum(answer['probabilities'].values()) == pytest.approx(1)
    assert answer['probabilities']['same'] < .98
