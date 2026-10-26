"""Storage migrations preserve source evidence, retrieval and recovery state."""
import json
import sqlite3
import pytest
from sessiongraph import storage, source_locator
from sessiongraph.auto_update import revision
from sessiongraph.work_map import work_map
from test_input_framework import fixture, msg
from test_work_map import setup_map


def legacy(store,path):
    sid=store.db.execute('SELECT id FROM sessions').fetchone()[0]
    with store.db:
        store.db.executemany('INSERT INTO raw_events VALUES(?,?,?)',[(sid,i,line) for i,line in enumerate(path.read_text().splitlines(),1)])
        store.db.execute('DELETE FROM sg_source_locations')


def test_new_import_keeps_locators_without_raw_event_copy(tmp_path):
    s,p=fixture(tmp_path,[msg('原文证据🙂'),msg('内部推理',channel='analysis')])
    assert s.db.execute('SELECT count(*) FROM raw_events').fetchone()[0]==0
    for row in s.db.execute('SELECT id,text FROM entries'):
        assert source_locator.read_original(s,row['id'])==dict(status='verified',entry_id=row['id'],text=row['text'],line=s.db.execute('SELECT line FROM entries WHERE id=?',(row['id'],)).fetchone()[0])


def test_verified_migration_backfills_all_locators_and_is_idempotent(tmp_path):
    s,p=fixture(tmp_path,[msg('中文🙂 精确证据')]);legacy(s,p)
    before=[tuple(r) for r in s.db.execute('SELECT * FROM entries')]
    frontier=[tuple(r) for r in s.db.execute('SELECT * FROM sessions')]
    result=storage.migrate_sources(s)
    assert result['removed_events']==2 and not result['retained_sessions']
    assert storage.migrate_sources(s)['removed_events']==0
    assert before==[tuple(r) for r in s.db.execute('SELECT * FROM entries')]
    assert frontier==[tuple(r) for r in s.db.execute('SELECT * FROM sessions')]
    assert source_locator.read_original(s,before[0][0])['status']=='verified'


@pytest.mark.parametrize('change',['missing','changed'])
def test_migration_retains_unverifiable_raw_data(tmp_path,change):
    s,p=fixture(tmp_path,[msg('original')]);legacy(s,p)
    before=[tuple(r) for r in s.db.execute('SELECT * FROM raw_events')]
    if change=='missing':p.unlink()
    else:p.write_text(p.read_text().replace('original','modified'))
    result=storage.migrate_sources(s)
    assert result['removed_events']==0 and len(result['retained_sessions'])==1
    assert before==[tuple(r) for r in s.db.execute('SELECT * FROM raw_events')]


def test_term_compaction_preserves_all_terms_and_foreign_keys(tmp_path):
    s,p=fixture(tmp_path,[msg('source')])
    # Recreate old rowid schema, including multiple keys per item.
    with s.db:
        s.db.execute('DROP TABLE sg2_pending_terms')
        s.db.execute('CREATE TABLE sg2_pending_terms(project TEXT NOT NULL,term TEXT NOT NULL,item TEXT NOT NULL,PRIMARY KEY(project,term,item))')
        s.db.executemany('INSERT INTO sg2_pending_terms VALUES(?,?,?)',[('p','word','a'),('p','word','b'),('p','中','a')])
    before=[tuple(r) for r in s.db.execute('SELECT * FROM sg2_pending_terms ORDER BY 1,2,3')]
    assert storage.compact_term_tables(s)['sg2_pending_terms']==3
    assert before==[tuple(r) for r in s.db.execute('SELECT * FROM sg2_pending_terms ORDER BY 1,2,3')]
    assert not storage.compact_term_tables(s)
    assert not s.db.execute('PRAGMA foreign_key_check').fetchall()


def test_index_tracks_mutation_and_rollback_without_scan(tmp_path):
    s,_,add=setup_map(tmp_path)
    mid=add('c','报告位于 /tmp/run_42.json','实验报告')
    assert s.recall('run_42')['memories'][0]['id']==mid
    old=revision(s.db,s.config['project_id'])
    with s.db:s.db.execute('UPDATE work_memory SET title=?,content=? WHERE id=?',('newneedle','revised',mid))
    assert revision(s.db,s.config['project_id'])!=old
    assert s.recall('newneedle')['memories'][0]['id']==mid
    assert not s.recall('报告')['memories']
    version=revision(s.db,s.config['project_id'])
    s.db.execute('UPDATE work_memory SET title=? WHERE id=?',('rollbackneedle',mid));s.db.rollback()
    assert revision(s.db,s.config['project_id'])==version
    assert not s.recall('rollbackneedle')['memories']
    queries=[];s.db.set_trace_callback(queries.append);s.recall('newneedle');s.db.set_trace_callback(None)
    assert any('memory_search MATCH' in q for q in queries)


def test_search_finds_older_pages_paths_and_chat(tmp_path):
    s,_,add=setup_map(tmp_path)
    old=add('old-chat','产物在 /tmp/run_042.json','早期实验')
    add('new-chat','当前记录','新工作')
    assert old not in {n['id'] for n in work_map(s,limit=1)['nodes']}
    found=work_map(s,limit=1,query='run_042')
    assert old in {n['id'] for n in found['nodes']}
    assert found['page']['matched']==1 and found['page']['next_offset'] is None
    first=work_map(s,limit=1)
    second=work_map(s,limit=1,offset=first['page']['next_offset'])
    assert {n['id'] for n in first['nodes'] if n['type']=='memory'}.isdisjoint({n['id'] for n in second['nodes'] if n['type']=='memory'})
    assert old in {n['id'] for n in work_map(s,chat='old-chat',limit=1)['nodes']}
    assert not work_map(s,query='run_042',chat='new-chat')['nodes']


def test_recall_index_matches_legacy_ranking(tmp_path,monkeypatch):
    s,_,add=setup_map(tmp_path)
    add('a','编号 step30，实验结果在 /tmp/实验.json','实验结果')
    add('b','编号 step300，无关指标','另一个实验')
    for q in ['step30','实验','/tmp/实验.json','另一个','无关','missing','*']:
        actual=[m['id'] for m in s.recall(q,limit=50,max_bytes=50000)['memories']]
        original=storage.exists
        with monkeypatch.context() as patch:
            patch.setattr(storage,'exists',lambda db,name:False if name=='memory_search' else original(db,name))
            expected=[m['id'] for m in s.recall(q,limit=50,max_bytes=50000)['memories']]
        assert actual==expected,q


def test_http_error_audit_keeps_bounded_redacted_proxy_body(tmp_path,monkeypatch):
    from io import BytesIO
    from types import SimpleNamespace
    from urllib.error import HTTPError
    from urllib.request import Request
    from sessiongraph.request_audit import read_response
    s,_=fixture(tmp_path,[msg('source')])
    def fail(*a,**kw):
        raise HTTPError('https://example.invalid/judge',451,'Denied',{'Content-Type':'text/html','x-request-id':'fixture-id'},BytesIO(b'<html>proxy denied key fixture-secret '+b'x'*9000))
    monkeypatch.setattr('urllib.request.build_opener',lambda *a:SimpleNamespace(open=fail))
    adapter=SimpleNamespace(config=s.config,key='fixture-secret',model='fixture',max_output=None,timeout=1,traces=[],provider_name='jev')
    with pytest.raises(HTTPError):read_response(adapter,Request('https://example.invalid/judge',data=b'{}'),[])
    from pathlib import Path
    record=json.loads(Path(adapter.traces[0]['path']).read_text())
    assert record['http_status']==451 and record['error_body_truncated']
    assert 'fixture-secret' not in record['error_body_text']
    assert 'proxy denied' in record['error_body_text']
    assert record['provider_request_id']=='fixture-id'
    assert record['failure']['retryable'] is False


def test_progress_counts_distinguish_running_retry_and_blocked(tmp_path):
    from sessiongraph.graph_v2 import diagnostics
    s,_=fixture(tmp_path,[msg('source')])
    project=s.config['project_id']
    with s.db:
        for name,state,control in [('a','judging',None),('b','pending','retry_wait'),('c','pending','blocked'),('d','pending',None),('e','complete','blocked')]:
            s.db.execute('INSERT INTO sg2_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(name,project,'input-chat','input-chat',state,'{}',None,'[]','{}',None,'now','now'))
            if control:s.db.execute('INSERT INTO sg2_control VALUES(?,?,?,?,?)',(name,control,0,1,'{}'))
    assert diagnostics(s)['progress']=={'running':1,'retry_wait':1,'blocked':1,'ready':1}


def test_old_writer_without_sql_functions_and_readonly_search_before_flush(tmp_path):
    from sessiongraph.core import Store
    s,_,add=setup_map(tmp_path)
    mid=add('c','source evidence','oldtitle');storage.flush_search(s.db)
    connection=sqlite3.connect(s.config['database'])
    connection.execute('UPDATE work_memory SET title=?,content=? WHERE id=?',('freshneedle','updated content',mid));connection.commit();connection.close()
    readonly=Store(s.config,readonly=True)
    assert readonly.recall('freshneedle')['memories'][0]['id']==mid
    assert not readonly.recall('oldtitle')['memories']
    assert work_map(readonly,query='freshneedle')['page']['matched']==1
    assert readonly.db.total_changes==0
    readonly.close()
    storage.flush_search(s.db)
    assert not s.db.execute('SELECT * FROM memory_search_dirty').fetchall()
    assert s.recall('freshneedle')['memories'][0]['id']==mid


def test_pending_resolution_uses_project_term_index(tmp_path):
    from sessiongraph.graph_v2 import _resolve_pending
    s,_=fixture(tmp_path,[msg('pendingneedle')])
    queries=[];s.db.set_trace_callback(queries.append)
    _resolve_pending(s,{}, {'nodes':[{'id':'n1','type':'Activity','evidence':[{'entry_id':'e','quote':'pendingneedle'}]}]},[],{'n1':'saved'},[])
    s.db.set_trace_callback(None)
    query=next(q for q in queries if q.startswith('SELECT DISTINCT u.*'))
    plan=[r[3] for r in s.db.execute('EXPLAIN QUERY PLAN '+query)]
    assert any('SEARCH t USING PRIMARY KEY (project=? AND term=?)' in step for step in plan)
    assert not any('SCAN t' in step for step in plan)
