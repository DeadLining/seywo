import json
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sessiongraph import call_logs

P='a'*20
Q='b'*20

def row(id='one', **extra):
    return dict(id=id, created_at=time.time(), model='fixture-model', state='started',
                endpoint='https://user:password@provider.test/v1/responses?key=secret#secret',
                request={'input':'private-chat'}, final_output='private-output', Authorization='private-key',
                **extra)


def test_only_metadata_is_persisted_and_start_finish_validation_share_one_row(tmp_path):
    item=row()
    assert call_logs.record(item,project=P,state=tmp_path)
    item.update(state='received', usage={'prompt_tokens':9,'completion_tokens':3}, elapsed_ms=20, http_status=200)
    assert call_logs.record(item,project=P,state=tmp_path)
    item.update(state='validated')
    assert call_logs.record(item,project=P,state=tmp_path)
    page=call_logs.read(project=P,state=tmp_path)
    assert len(page['items'])==1 and page['items'][0]['state']=='validated'
    assert page['items'][0]['input_tokens']==9 and page['items'][0]['output_tokens']==3
    assert page['items'][0]['endpoint']=='https://provider.test/v1/responses'
    raw=b''.join(p.read_bytes() for p in (tmp_path/'call-logs').iterdir())
    for secret in [b'private-chat',b'private-output',b'private-key',b'password',b'key=secret']:
        assert secret not in raw
    assert page['model_calls']==0


def test_reads_do_not_create_a_database_and_existing_reads_do_not_modify_it(tmp_path):
    assert call_logs.read(state=tmp_path)['items']==[] and not list(tmp_path.iterdir())
    assert call_logs.record(row(),project=P,state=tmp_path)
    before={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    call_logs.read(state=tmp_path)
    assert before=={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_filters_pagination_and_unknown_usage(tmp_path):
    for i in range(53):
        data=row(str(i));data['state']='probe_ok' if i%2 else 'probe_failed'
        data['model']='a' if i%2 else 'b'
        assert call_logs.record(data,project=P,state=tmp_path)
    assert call_logs.record(row('other'),project=Q,state=tmp_path)
    assert len(call_logs.read(project=P,state=tmp_path)['items'])==50
    assert call_logs.read(project=P,state=tmp_path)['has_more']
    assert len(call_logs.read(project=P,offset=50,state=tmp_path)['items'])==3
    page=call_logs.read(project=P,model='b',status='failed',state=tmp_path)
    assert len(page['items'])==27
    assert all(item['input_tokens'] is None for item in page['items'])
    assert len(call_logs.read(project=P,status='succeeded',state=tmp_path)['items'])==26
    assert len(call_logs.read(status='pending',state=tmp_path)['items'])==1


def test_time_and_size_retention_only_change_metadata_ledger(tmp_path,monkeypatch):
    monkeypatch.setattr(call_logs,'MAX_BYTES',256*1024)
    old=row('old');old['created_at']-=8*86400
    call_logs.record(old,project=P,state=tmp_path)
    for i in range(450):
        item=row(str(i));item.update(model='m'*256,job_id='j'*96,response_model='r'*256)
        assert call_logs.record(item,project=P,state=tmp_path)
    assert sum(p.stat().st_size for p in (tmp_path/'call-logs').iterdir())<=call_logs.MAX_BYTES
    with sqlite3.connect(tmp_path/'call-logs/calls.sqlite3') as db:
        assert not db.execute("SELECT 1 FROM calls WHERE id='old'").fetchone()
        assert db.execute("SELECT 1 FROM calls WHERE id='449'").fetchone()
        assert db.execute('SELECT COUNT(*) FROM calls').fetchone()[0]<450


def test_log_lock_or_storage_failure_is_best_effort(tmp_path,monkeypatch):
    assert call_logs.record(row(),project=P,state=tmp_path)
    db=sqlite3.connect(tmp_path/'call-logs/calls.sqlite3');db.execute('BEGIN EXCLUSIVE')
    started=time.monotonic()
    assert not call_logs.record(row('locked'),project=P,state=tmp_path)
    assert time.monotonic()-started<0.5
    db.rollback();db.close()
    monkeypatch.setattr(call_logs,'_connect_write',lambda *a: (_ for _ in ()).throw(OSError('disk unavailable')))
    assert not call_logs.record(row('full'),project=P,state=tmp_path)


def test_permanent_deletion_is_scoped_to_project_and_job(tmp_path,monkeypatch):
    monkeypatch.setenv('SESSIONGRAPH_RUNTIME_HOME',str(tmp_path))
    assert call_logs.record(row('p',job_id='job'),project=P)
    assert call_logs.record(row('q',job_id='job'),project=Q)
    call_logs.remove_jobs({'project_id':P},['job'])
    assert call_logs.read(project=P)['items']==[]
    assert len(call_logs.read(project=Q)['items'])==1


def test_native_log_read_never_opens_graph_or_resolves_credentials(tmp_path,monkeypatch):
    from sessiongraph import native_bridge
    assert call_logs.record(row(),project=P,state=tmp_path)
    monkeypatch.setattr(native_bridge,'catalog',lambda *a:pytest.fail('catalog read'))
    monkeypatch.setattr(native_bridge,'Store',lambda *a,**k:pytest.fail('graph opened'))
    assert native_bridge.read(tmp_path/'projects.json',{'operation':'call_logs','project':P})['items']
    for request in ({'operation':'call_logs','project':'../outside'},
                    {'operation':'call_logs','log_status':'bad'},
                    {'operation':'call_logs','log_model':{}},
                    {'operation':'status','project':P,'log_status':'all'}):
        with pytest.raises(ValueError): native_bridge.read(tmp_path/'projects.json',request)


def test_audit_transport_and_validation_feed_same_ledger_without_content(tmp_path,monkeypatch):
    from sessiongraph import request_audit
    from sessiongraph.core import init_project,load_config
    from urllib.request import Request
    import io
    config=load_config(init_project(tmp_path))
    audit=SimpleNamespace(config=config,key='fixture-key',model='fixture-model',max_output=100,
                          timeout=10,traces=[],provider_name='selected_model')
    class Response(io.BytesIO):
        status=200
        headers={'x-request-id':'provider-request'}
    class Opener:
        def open(self,*a,**k):
            return Response(json.dumps({'status':'completed','model':'actual-model','usage':{'input_tokens':12,'output_tokens':4},'output':[{'type':'message','content':[{'type':'output_text','text':'private-output'}]}]}).encode())
    monkeypatch.setattr('urllib.request.build_opener',lambda *a:Opener())
    request_audit.read_response(audit,Request('https://provider.test/v1/responses',data=b'{"input":"private-chat"}'),[])
    request_audit.validation_result(audit,{'code':'graph_validation','message':'private-error'})
    page=call_logs.read(project=config['project_id'])
    assert len(page['items'])==1
    item=page['items'][0]
    assert item['state']=='validation_failed' and item['http_status']==200
    assert item['response_model']=='actual-model' and item['input_tokens']==12
    assert item['error_code']=='graph_validation'
    assert all(text not in json.dumps(page) for text in ['private-output','private-chat','private-error','fixture-key'])


def test_logging_failures_do_not_change_successful_transport(tmp_path,monkeypatch):
    from sessiongraph import request_audit
    from sessiongraph.core import init_project,load_config
    from urllib.request import Request
    import io
    config=load_config(init_project(tmp_path))
    client=SimpleNamespace(config=config,key='',model='fixture',max_output=10,timeout=10,traces=[])
    class Response(io.BytesIO): status=200;headers={}
    class Opener:
        def open(self,*a,**k):return Response(b'{"status":"completed","output":[]}')
    monkeypatch.setattr('urllib.request.build_opener',lambda *a:Opener())
    monkeypatch.setattr(call_logs,'_connect_write',lambda *a:(_ for _ in ()).throw(OSError('disk full')))
    assert json.loads(request_audit.read_response(client,Request('https://provider.test/v1/responses',data=b'{}'),[]))['status']=='completed'
    request_audit.validation_result(client)
