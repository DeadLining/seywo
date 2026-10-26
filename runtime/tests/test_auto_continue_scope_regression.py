"""Automatic chat continuation must drain older source segments in its frontier."""
import json
import sys
import pytest
from sessiongraph.core import Store,load_config
from sessiongraph.auto_update import run_update,_execute
from sessiongraph import graph_v2 as g
from test_memories import make_store,entries,FakeExtractor
from test_graph_v2 import output,Judge
from test_mvp import write_session


@pytest.mark.parametrize('event',['manual','AutoContinue'])
def test_real_worker_old_pending_segment_and_frozen_frontier(tmp_path,monkeypatch,event):
    root,s,first=make_store(tmp_path,[('assistant','训练 A 得到模型 B')])
    config=root/'.sessiongraph/config.json'
    cfg=json.loads(config.read_text());cfg['graph_v2']={'enabled':True}
    cfg['hook']['allowed_transcript_roots']=[str(root)]
    config.write_text(json.dumps(cfg))
    s.config.update(load_config(config))
    old_segment=s.db.execute('SELECT id FROM sessions').fetchone()[0]
    r=g.run_chat(s,FakeExtractor([output(entries(s))]),'chat-1',jev=Judge(),segment_id=old_segment,call_budget=1)
    old_job=s.db.execute('SELECT id FROM sg2_jobs').fetchone()[0]
    assert s.db.execute('SELECT state FROM sg2_jobs').fetchone()[0]=='pending'
    newer=write_session(root/'newer.jsonl',root,'chat-1',[('assistant','评估 C 得到报告 D')]);s.ingest(newer)
    target=s.db.execute('SELECT max(rowid) FROM entries').fetchone()[0]
    future=write_session(root/'future.jsonl',root,'chat-1',[('assistant','将来尚未纳入边界的记录')]);s.ingest(future)
    future_segment=s.db.execute('SELECT id FROM sessions WHERE path=?',(str(future.resolve()),)).fetchone()[0]
    s.close()
    monkeypatch.setattr('sessiongraph.continuation.automatic_enabled',lambda *a:True)
    shim='''import sys
from contextlib import nullcontext
sys.path.insert(0,"tests")
from test_graph_v2 import Judge,output
import sessiongraph.extraction as ex
import sessiongraph.graph_v2 as g
import sessiongraph.gateway_worker as worker
ex.create_extractor=lambda config:(lambda batch,policy:output(batch))
class FixtureJudge(Judge):
    cache_identity=["fixture"]
    def __init__(self,config):super().__init__()
g.ModelJudge=FixtureJudge
worker.gateway_credentials=lambda *a:nullcontext()
raise SystemExit(worker.main())
'''
    calls=[]
    def execute(argv,timeout):
        if 'refresh-chat' in argv:return _execute(argv,timeout)
        calls.append(argv)
        return _execute([sys.executable,'-B','-c',shim,*argv[3:]],timeout)
    result=run_update(config,'chat-1',event,transcript=str(newer),through_rowid=target,execute=execute)
    s=Store(load_config(config),readonly=True)
    try:
        state=s.db.execute('SELECT state FROM sg2_jobs WHERE id=?',(old_job,)).fetchone()[0]
        assert state==('complete' if event=='AutoContinue' else 'pending'),result
        assert s.db.execute('SELECT count(*) FROM sg2_jobs WHERE segment=?',(future_segment,)).fetchone()[0]==0
        assert all('--memories-transcript-path' in argv for argv in calls)
        if event=='AutoContinue':assert result['state']=='succeeded',result
    finally:s.close()
