import json
from sessiongraph import continuation as c
from sessiongraph.core import init_project,load_config
from sessiongraph.import_diagnostics import summarize_import,source_issue


def test_import_retry_contract_reaches_scheduler(tmp_path,monkeypatch):
    monkeypatch.setenv('SESSIONGRAPH_WORKER_LOCK',str(tmp_path/'runtime/extract.lock'))
    path=init_project(tmp_path/'project')
    diagnostics,error,_=summarize_import({'imports':[{'added':0}], 'skipped':[source_issue('/missing','source_missing',known=True)]})
    c.record(path,dict(run_id='r',chat_id='chat',event='AutoContinue',state='failed',error_code=error,import_diagnostics=diagnostics,updated_at=10),launch=False,now=10)
    row=c.snapshot(load_config(path))[0]
    assert row['state']=='retry_wait'


def test_continuation_preserves_source_and_frozen_boundary(tmp_path,monkeypatch):
    monkeypatch.setenv('SESSIONGRAPH_WORKER_LOCK',str(tmp_path/'runtime/extract.lock'))
    path=init_project(tmp_path/'project')
    c.record(path,dict(run_id='r',chat_id='chat',event='PostCompact',state='pending',error_code='graph_continuation_pending',source_transcript='/allowed/segment.jsonl',target_rowid=42,updated_at=10),launch=False,now=10)
    queue=json.loads((c.root(load_config(path))/'queue.json').read_text())
    row=next(iter(queue.values()))
    assert row['source_transcript']=='/allowed/segment.jsonl'
    assert row['target']==42


def test_import_mixed_permanent_failure_is_not_retried(tmp_path,monkeypatch):
    monkeypatch.setenv('SESSIONGRAPH_WORKER_LOCK',str(tmp_path/'runtime/extract.lock'))
    path=init_project(tmp_path/'project')
    diagnostics,error,_=summarize_import({'imports':[], 'skipped':[source_issue('/a','source_missing',known=True),source_issue('/b','source_changed',known=True)]})
    c.record(path,dict(run_id='r',chat_id='chat',event='AutoContinue',state='failed',error_code=error,import_diagnostics=diagnostics,updated_at=10),launch=False,now=10)
    assert c.snapshot(load_config(path))[0]['state']=='blocked'


def test_frozen_continuation_never_reimports_historical_sources(tmp_path):
    from sessiongraph.core import Store
    from sessiongraph.auto_update import run_update
    path=init_project(tmp_path/'project');Store(load_config(path)).close()
    seen=[]
    def execute(argv,timeout):
        seen.append(argv)
        if 'refresh-chat' in argv:
            return {'imports':[], 'skipped':[source_issue('/old/missing','source_missing',known=True)]}
        return {'saved':[], 'failed':[], 'model_calls':0}
    # Explicitly enable only this synthetic test project.
    raw=json.loads(path.read_text());raw['hook']['enabled']=True;path.write_text(json.dumps(raw))
    from unittest.mock import patch
    with patch('sessiongraph.continuation.automatic_enabled',return_value=True):
        result=run_update(path,'chat','AutoContinue',execute=execute,through_rowid=0)
    assert not any('refresh-chat' in argv for argv in seen)
    assert result['source_selection']=='indexed_boundary'


def test_indexed_continuation_does_not_claim_a_new_source_import():
    diagnostics,error,_=summarize_import({'imports':[{'added':0,'indexed_boundary':0}],'skipped':[]})
    assert error is None
    assert diagnostics['imported_source_count']==0
