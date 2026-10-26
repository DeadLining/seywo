import json
from pathlib import Path
from sessiongraph.core import load_config, Store, init_project
from sessiongraph.memories import run_chat, _state_path
from sessiongraph.extraction import ProviderError
from sessiongraph.global_hooks import ensure_project
from test_input_framework import fixture, msg


def test_legacy_pipeline_filters_noise_but_keeps_results_and_user_json(tmp_path):
    s,_=fixture(tmp_path,[msg('hidden reasoning '*2000,channel='analysis'),msg('{"run_id":"r42","accuracy":0.91}',role='user'),msg('<in-app-browser-context source="ambient">noise</in-app-browser-context>\n需求：记住结果',role='user'),{'type':'function_call','name':'exec_command','call_id':'tool','arguments':json.dumps({'cmd':'python train.py '+'x'*10000})},{'type':'function_call_output','call_id':'tool','output':'progress 10%\n'*1000+'run_id=r42 accuracy=0.91 artifact=/tmp/result.json\n'},msg('结论：实验有效')])
    sent=[]
    def ex(batch,policy):sent.extend(batch);return {'memories':[]}
    r=run_chat(s,ex,'input-chat',lookback_chars=100000)
    text='\n'.join(x['text'] for x in sent)
    assert not r['failed']
    assert 'hidden reasoning' not in text and 'progress 10%' not in text and 'ambient' not in text
    assert 'x'*1000 not in text
    assert '"accuracy":0.91' in text and '/tmp/result.json' in text and '需求：记住结果' in text
    assert r['input_stats']['excluded_chars']>20000
    s.close()


def test_output_limit_shrinks_persistently_without_repeating_failed_request(tmp_path):
    s,_=fixture(tmp_path,[msg('A'*1800,role='user'),msg('B'*1800)])
    class Extractor:
        def __init__(self):self.sizes=[]
        def __call__(self,batch,policy):
            self.sizes.append(sum(len(e['text']) for e in batch))
            if self.sizes[-1]>2000:raise ProviderError('output exhausted','provider_output_limit')
            return {'memories':[]}
    first=Extractor();r=run_chat(s,first,'input-chat',call_budget=8)
    assert first.sizes==[3600] and r['pending_retry_count']==1 and r['blocked_retry_count']==0
    assert json.loads(_state_path(s,'input-chat').read_text())['batch_caps']
    cfg=s.config;s.close();s=Store(cfg)
    second=Extractor();r=run_chat(s,second,'input-chat',pending_only=True,call_budget=8)
    assert not r['failed'] and max(second.sizes)<=1800
    assert run_chat(s,second,'input-chat')['model_calls']==0
    s.close()


def test_single_entry_output_limit_and_call_cap_keep_tail_pending(tmp_path):
    s,_=fixture(tmp_path,[msg('A'*6000,role='user')])
    def failed(batch,policy):raise ProviderError('output exhausted','provider_output_limit')
    r=run_chat(s,failed,'input-chat',call_budget=1)
    assert r['model_calls']==1 and r['pending_retry_count']==1
    sent=[]
    def good(batch,policy):sent.extend(batch);return {'memories':[]}
    r=run_chat(s,good,'input-chat',call_budget=1)
    assert r['model_calls']==1 and r['failure_codes']==['graph_call_budget_pending']
    assert max(e['source_end'] for e in sent)<6000
    r=run_chat(s,good,'input-chat',call_budget=8)
    assert not r['failed'] and max(e['source_end'] for e in sent)==6000
    s.close()


def test_graph_setting_inherited_without_secret_or_explicit_optout_override(tmp_path):
    cfg=init_project(tmp_path/'template');raw=json.loads(cfg.read_text())
    raw['graph_v2']={'enabled':True,'jev':{'model':'jev-latest','api_key':'DO_NOT_COPY'}};cfg.write_text(json.dumps(raw))
    settings={'enabled':True,'template_config':str(cfg),'registry':str(tmp_path/'registry.json'),'enabled_since':'2026-10-08T00:00:00.000Z','transcript_roots':[]}
    root=tmp_path/'new';root.mkdir();p=ensure_project(str(root),settings)
    assert json.loads(p.read_text())['graph_v2']=={'enabled':True,'jev':{'model':'jev-latest'}}
    raw=json.loads(p.read_text());raw['graph_v2']={'enabled':False};p.write_text(json.dumps(raw));ensure_project(str(root),settings)
    assert json.loads(p.read_text())['graph_v2']=={'enabled':False}


def test_failed_request_trace_preserves_actual_budget_usage_and_body(tmp_path,monkeypatch):
    from sessiongraph.extraction import ResponsesExtractor
    cfg=load_config(init_project(tmp_path));cfg['provider'].update(protocol='responses',endpoint='http://127.0.0.1:4317/openai/v1/responses',model='fixture',auth='none',max_output_tokens=4000)
    response={'status':'incomplete','id':'resp1','incomplete_details':{'reason':'max_output_tokens'},'usage':{'input_tokens':1200,'output_tokens':4000,'output_tokens_details':{'reasoning_tokens':3900}},'output':[]}
    class Response:
        headers={'x-request-id':'upstream1'};status=200
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,*args):return json.dumps(response).encode()
    class Opener:
        def open(self,req,timeout):return Response()
    monkeypatch.setattr('sessiongraph.extraction.request.build_opener',lambda *a:Opener())
    ex=ResponsesExtractor(cfg)
    import pytest
    with pytest.raises(ProviderError,match='output token budget'):ex([{'id':'e','text':'test'}],{})
    record=json.loads(Path(ex.traces[0]['path']).read_text())
    assert record['request']['max_output_tokens']==4000
    assert record['usage']['output_tokens_details']['reasoning_tokens']==3900
    assert record['provider_request_id']=='upstream1'
    assert record['incomplete_details']['reason']=='max_output_tokens'
    assert record['request']['input'][0]['content'][0]['text']


def test_historical_output_limit_block_is_shrunk_once(tmp_path):
    s,_=fixture(tmp_path,[msg('x'*6000,role='user')])
    segment=s.db.execute('select segment_id from session_chats').fetchone()[0]
    line=s.db.execute('select line from entries').fetchone()[0]
    path=_state_path(s,'input-chat');path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({'segments':{segment:line-1},'pending_batches':{segment:{'from_line':line,'through_line':line,'since_time':None,'status':'blocked','retryable':False,'error_code':'provider_output_limit'}}}))
    batches=[]
    def ex(batch,policy):batches.append(batch);return {'memories':[]}
    result=run_chat(s,ex,'input-chat',pending_only=True,call_budget=8)
    assert not result['failed'] and batches
    assert max(sum(len(e['text']) for e in b) for b in batches)<=4000
    s.close()


def test_audit_redacts_keys_and_excludes_reasoning(tmp_path,monkeypatch):
    from sessiongraph.request_audit import read_response
    from urllib.request import Request
    class Extractor:
        config={'database':str(tmp_path/'index.sqlite3')};key='opaque-credential';model='fixture';max_output=4000;timeout=5;traces=[]
    envelope={'status':'completed','usage':{'output_tokens':5},'output':[{'type':'reasoning','text':'private_thought'},{'type':'message','content':[{'type':'output_text','text':'secret=hiddencredential'}]}]}
    class Response:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self,*a):return json.dumps(envelope).encode()
    class Opener:
        def open(self,*a,**k):return Response()
    monkeypatch.setattr('urllib.request.build_opener',lambda *a:Opener())
    req=Request('http://127.0.0.1/test',data=json.dumps({'text':'opaque-credential','api_key':'somecredential'}).encode(),headers={'Authorization':'Bearer opaque-credential'})
    read_response(Extractor,req,[])
    text=Path(Extractor.traces[-1]['path']).read_text()
    assert all(x not in text for x in ['opaque-credential','somecredential','hiddencredential','private_thought','Authorization'])
