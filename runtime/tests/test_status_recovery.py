import json
import time
from pathlib import Path
import pytest
from sessiongraph import graph_v2 as g, recovery, stages
from sessiongraph.core import Store, digest
from sessiongraph.extraction import create_extractor, ProviderError
from test_memories import make_store, entries, FakeExtractor
from test_graph_v2 import Judge, output
from test_input_framework import fixture, msg, Empty


@pytest.fixture(autouse=True)
def isolate_automatic_registration(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'isolated-codex'))
    monkeypatch.delenv('SESSIONGRAPH_WORKER_LOCK', raising=False)


def test_v2_status_real_counts_and_read_only(tmp_path):
    _, s, _ = make_store(tmp_path, [('user', '训练 A 得到模型 B')])
    s.config.setdefault('graph_v2', {})['enabled'] = True
    g.run_chat(s, FakeExtractor([output(entries(s))]), 'chat-1', jev=Judge())
    ro = Store(s.config, readonly=True)
    before = ro.db.total_changes
    info = ro.status()
    assert info['engine'] == 'graph_v2'
    assert info['records'] == 2 and info['edges'] == 1
    assert info['jobs']['complete'] == 1
    assert info['coverage']['completed_entries'] == 1 and info['coverage']['ratio'] == 1
    assert info['semantic_entries'] == 1 and info['model_calls'] == 0
    assert ro.coverage()['records'] == 2 and ro.db.total_changes == before
    ro.close()


def test_coverage_excluded_is_not_semantic(tmp_path):
    s, _ = fixture(tmp_path, [msg('thinking', channel='analysis'), msg('important result')])
    s.config.setdefault('graph_v2', {})['enabled'] = True
    g.run_chat(s, Empty(), 'input-chat', jev=Judge())
    c = s.status()['coverage']
    assert c['completed_entries'] == 2 and c['semantic_entries'] == 1 and c['excluded_entries'] == 1
    assert c['ratio'] == 1


def test_coverage_gaps_overlap_stale_revision_and_blocked(tmp_path):
    s, _ = fixture(tmp_path, [msg('abcdefghij')])
    s.config.setdefault('graph_v2', {})['enabled'] = True
    g.run_chat(s, Empty(), 'input-chat', jev=Judge())
    row = s.db.execute('SELECT * FROM sg_input_coverage').fetchone()
    s.db.execute('DELETE FROM sg_input_coverage')
    def add(a,b,rev='input-v1',h=None):
        s.db.execute('INSERT INTO sg_input_coverage VALUES(?,?,?,?,?,?,?,?)',
            (row['entry_id'],rev,h or row['source_hash'],a,b,'selected','fixture',row['job']))
    add(0,4); add(2,6); add(7,10); add(0,10,'old'); add(0,10,h='wronghash')
    assert s.status()['coverage']['partial_entries'] == 1
    assert s.status()['coverage']['completed_entries'] == 0
    add(6,7)
    assert s.status()['coverage']['completed_entries'] == 1
    s.db.execute("UPDATE sg2_jobs SET state='pending'")
    assert s.status()['coverage']['completed_entries'] == 0


def blocked(s, job='blocked', code='jev_http_451', retry=False):
    s.config.setdefault('graph_v2', {})['enabled'] = True
    g.setup(s)
    s.db.execute('INSERT INTO sg2_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (job,s.config['project_id'],'chat-1','fixture','pending','{}','{"keep":true}','[]','{}',code,
         '2026-10-15T00:00:00+00:00','2026-10-15T00:00:00+00:00'))
    info=dict(code=code,retryable=retry,stage='relations')
    s.db.execute('INSERT INTO sg2_control VALUES(?,?,?,?,?)',(job,'blocked',0,1,json.dumps(info)))
    s.db.commit()
    return job


def receipt(s, identity='a'*32, **changes):
    p=Path(s.config['database']).parent/'request-audit';p.mkdir(exist_ok=True)
    data=dict(id=identity,job_id='blocked',provider='jev',state='validated',http_status=200,
        endpoint='https://api.typesafe.ai/v1/systemone',model='jev-latest',created_at=time.time())
    data.update(changes);(p/f'request-{identity}.json').write_text(json.dumps(data))
    return identity


def test_recovery_preview_apply_once_preserves_history_and_output(tmp_path):
    _, s, _=make_store(tmp_path,[('user','source')]);blocked(s)
    request_id=receipt(s)
    changes=s.db.total_changes
    assert recovery.recover(s,request_id=request_id)['candidates'][0]['eligible']
    assert s.db.total_changes==changes
    assert recovery.recover(s,request_id=request_id,apply=True,launch=False)['recovered']==1
    row=s.db.execute('SELECT * FROM sg2_jobs').fetchone()
    assert row['state']=='pending' and row['error']=='jev_http_451' and row['output_json']=='{"keep":true}'
    s.db.execute("UPDATE sg2_control SET state='blocked'");s.db.commit()
    assert recovery.recover(s,request_id=request_id,apply=True,launch=False)['recovered']==0
    assert s.db.execute('SELECT count(*) FROM sg2_recoveries').fetchone()[0]==1


def test_old_policy_migrates_only_known_false_retry(tmp_path):
    _, s, _=make_store(tmp_path,[('user','source')])
    blocked(s,'old','jev_malformed_response',False)
    blocked(s,'exhausted','jev_malformed_response',True)
    blocked(s,'ref','graph_validation',False)
    blocked(s,'unauthorized','jev_http_401',False)
    blocked(s,'network','jev_http_451',False)
    r=recovery.recover(s,apply=True,launch=False)
    assert r['recovered']==1 and next(c for c in r['candidates'] if c['eligible'])['job_id']=='old'
    info=s.status()
    assert info['progress']['blocked']==4 and info['progress']['ready']==1


@pytest.mark.parametrize('change',[
    {'state':'received'}, {'http_status':451}, {'model':'other'}, {'job_id':'other'},
    {'endpoint':'https://other.example'}, {'created_at':time.time()-90000}, {'provider':'deepseek'},
])
def test_invalid_proof_cannot_release(tmp_path,change):
    _,s,_=make_store(tmp_path,[('user','source')]);blocked(s)
    with pytest.raises(ValueError):recovery.recover(s,request_id=receipt(s,**change),apply=True,launch=False)
    assert s.db.execute('SELECT state FROM sg2_control').fetchone()[0]=='blocked'


def test_cross_project_and_complete_untouched(tmp_path):
    _,s,_=make_store(tmp_path,[('user','source')]);blocked(s)
    s.db.execute("UPDATE sg2_jobs SET project='elsewhere'");s.db.commit()
    assert recovery.recover(s)['candidates']==[]
    with pytest.raises(ValueError):recovery.recover(s,job_ids=['blocked'],apply=True)


@pytest.mark.parametrize('protocol',['responses','chat-completions'])
@pytest.mark.parametrize('kind',['json','outer','limit'])
def test_generated_json_vs_protocol_vs_limit(tmp_path,monkeypatch,protocol,kind):
    _,s,_=make_store(tmp_path,[('user','source')])
    cfg=s.config; cfg['provider'].update(protocol=protocol,auth='none',api_key_env='TEST_API_KEY',
        endpoint='http://127.0.0.1:4317/openai/v1/responses',base_url='http://127.0.0.1:4317/openai/v1',model='fixture')
    monkeypatch.setenv('TEST_API_KEY','fixture')
    monkeypatch.setenv(cfg['provider']['base_url_env'],'http://127.0.0.1:4317/openai/v1')
    monkeypatch.setenv(cfg['provider']['model_env'],'fixture')
    extractor=create_extractor(cfg)
    text='{"secret":"private content", bad}'
    if protocol=='responses':
        envelope={'status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':text}]}]}
    else:envelope={'choices':[{'finish_reason':'stop','message':{'content':text}}]}
    if kind=='limit':envelope['usage']={'output_tokens':extractor.max_output,'completion_tokens':extractor.max_output}
    raw=json.dumps(envelope).encode() if kind!='outer' else b'not an API envelope'
    monkeypatch.setattr('sessiongraph.request_audit.read_response',lambda *args:raw)
    with pytest.raises(ProviderError) as caught:extractor([],cfg['policy'])
    assert caught.value.code=={'json':'provider_output_json_invalid','outer':'provider_malformed_response','limit':'provider_output_limit'}[kind]
    assert 'private content' not in str(caught.value)
    if kind=='json':assert 'offset=' in caught.value.detail and caught.value.stage=='parse_output'


def test_generated_json_failure_uses_feedback_not_blind_retry(tmp_path):
    _,s,_=make_store(tmp_path,[('user','训练 A 得到模型 B')])
    class Extractor:
        calls=0
        def __call__(self,batch,policy):
            self.calls+=1
            if self.calls==1:
                raise ProviderError('provider_output_json:line=1:column=6:offset=5:chars=8','provider_output_json_invalid')
            assert policy['validation_feedback']['error'].startswith('provider_output_json:')
            return output(entries(s))
    ex=Extractor();r=g.run_chat(s,ex,'chat-1',jev=Judge())
    assert not r['failed'] and ex.calls==2 and len(r['saved_relations'])==1
    assert r['graph_v2']['schema_repairs']==1


def test_size_index_tracks_old_writer_and_read_does_not_migrate(tmp_path):
    s,_=fixture(tmp_path,[msg('hello')])
    eid=s.db.execute('SELECT id FROM entries').fetchone()[0]
    assert s.db.execute('SELECT chars FROM sg_entry_sizes WHERE entry_id=?',(eid,)).fetchone()[0]==5
    s.db.execute('UPDATE entries SET text=? WHERE id=?',('中文🙂',eid));s.db.commit()
    assert s.db.execute('SELECT chars FROM sg_entry_sizes WHERE entry_id=?',(eid,)).fetchone()[0]==3


def test_recovery_wakes_only_target_chat_preserves_frontier(tmp_path,monkeypatch):
    from sessiongraph import continuation as c
    _,s,_=make_store(tmp_path,[('user','source')]);blocked(s,'old','jev_malformed_response',False)
    monkeypatch.setattr(c,'automatic_enabled',lambda *a:True)
    monkeypatch.setattr(c,'root',lambda *a:tmp_path/'queue')
    folder=c.root();folder.mkdir()
    config=str(Path(s.config['_config_path']).resolve())
    identity=c.key(config,'chat-1')
    untouched=dict(config=config,chat='other',state='blocked',due=0)
    c.write(folder/'queue.json',{identity:dict(target=999,run_id='original'), 'other':untouched})
    result=recovery.recover(s,apply=True,launch=False)
    q=c.read(folder/'queue.json',{})
    assert result['scheduled_chats']==['chat-1'] and q[identity]['target']==999
    assert q[identity]['state']=='ready' and q[identity]['run_id']=='original'
    assert q['other']==untouched


def test_success_before_failure_cannot_release_and_completed_stays_completed(tmp_path):
    _,s,_=make_store(tmp_path,[('user','source')]);blocked(s)
    req=receipt(s,created_at=time.time()-100)
    s.db.execute('UPDATE sg2_jobs SET updated_at=?',(g.now(),));s.db.commit()
    assert not recovery.recover(s,request_id=req)['candidates'][0]['eligible']
    s.db.execute("UPDATE sg2_jobs SET state='complete'");s.db.commit()
    assert recovery.recover(s,request_id=req,apply=True)['recovered']==0


def test_real_fresh_success_recovers_once_without_deepseek(tmp_path,monkeypatch):
    from sessiongraph.jev import JevClient
    from types import SimpleNamespace
    _,s,_=make_store(tmp_path,[('user','source')]);blocked(s)
    class Response:
        status=200;headers={}
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self,*a):return json.dumps({'answers':{'p1':{'choice':'yes','probabilities':{'yes':1.,'no':0.}}}}).encode()
    monkeypatch.setattr('urllib.request.build_opener',lambda *a:SimpleNamespace(open=lambda *a,**k:Response()))
    monkeypatch.setattr('sessiongraph.jev.verified_tls_context',lambda:None)
    client=JevClient({});client.key='fixture';client.audit_config=s.config
    token=stages.CURRENT.set(dict(job_id='blocked',stage='identity'))
    try:
        stats=g._stats()
        g.judge(s,client,'identity','state',{'p1':{'criteria':['yes','no']}},stats)
        assert stats['jev_calls']==1 and stats['deepseek_calls']==0
        assert s.db.execute('SELECT state FROM sg2_control').fetchone()[0]=='ready'
        assert s.db.execute('SELECT count(*) FROM sg2_recoveries').fetchone()[0]==1
        s.db.execute("UPDATE sg2_control SET state='blocked'");s.db.commit()
        g.judge(s,client,'identity','state',{'p1':{'criteria':['yes','no']}},stats)
        assert s.db.execute('SELECT state FROM sg2_control').fetchone()[0]=='blocked'
    finally:stages.CURRENT.reset(token)


def test_recovery_outbox_retries_wake_without_reopening(tmp_path,monkeypatch):
    _,s,_=make_store(tmp_path,[('user','source')]);blocked(s,'old','jev_malformed_response',False)
    def fail(*a,**k):raise OSError('fixture queue failure')
    monkeypatch.setattr(recovery,'wake',fail)
    r=recovery.recover(s,apply=True,launch=False)
    assert r['recovered']==1 and r['scheduling_error']=='OSError'
    assert s.db.execute('SELECT woken FROM sg2_recoveries').fetchone()[0]==0
    monkeypatch.setattr(recovery,'wake',lambda *a,**k:['chat-1'])
    r=recovery.recover(s,apply=True,launch=False)
    assert r['recovered']==0 and r['scheduled_chats']==['chat-1']
    assert s.db.execute('SELECT woken FROM sg2_recoveries').fetchone()[0]==1


def test_recovered_451_resumes_relation_without_reextracting(tmp_path):
    from sessiongraph.jev import JevUnavailable
    _,s,_=make_store(tmp_path,[('assistant','训练 A 得到模型 B')])
    class Denied(Judge):
        def evaluate(self,*a):
            self.calls+=1
            raise JevUnavailable('jev_http_451')
    first=g.run_chat(s,FakeExtractor([output(entries(s))]),'chat-1',jev=Denied(),respect_schedule=True)
    assert first['failure_codes']==['jev_http_451'] and len(first['saved'])==2
    job=s.db.execute('SELECT id FROM sg2_jobs').fetchone()[0]
    request_id=receipt(s,job_id=job)
    assert recovery.recover(s,request_id=request_id,apply=True,launch=False)['recovered']==1
    restored=g.run_chat(s,FakeExtractor([]),'chat-1',jev=Judge(),pending_only=True,respect_schedule=True)
    assert not restored['failed'] and restored['graph_v2']['deepseek_calls']==0
    assert len(restored['saved_relations'])==1 and s.db.execute('SELECT count(*) FROM sg2_nodes').fetchone()[0]==2
    assert s.db.execute('SELECT state FROM sg2_jobs').fetchone()[0]=='complete'
