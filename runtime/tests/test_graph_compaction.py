"""Historical identity compatibility after standalone compaction retirement."""
from sessiongraph.source_text import hydrate
import json
import pytest
from sessiongraph import graph_v2 as graph, graph_compaction as compact, revision_ledger as ledger
from sessiongraph.memory import recall, resume
from sessiongraph.work_map import work_map
from sessiongraph.views import memory_detail
from test_memories import make_store, entries


def publish(s, node, entry, *, value='1', conditions=None, identity=None, kind='Claim', alias='', provenance='direct', atomic=True, status='completed'):
    p=dict(id='local', type=kind,title='metric '+node,content='metric report '+node,status=status,
           aliases=[alias] if alias else [],identity=identity or [],context_kind='work',provenance=provenance,
           evidence=[dict(entry_id=entry['id'],quote=entry['text'])])
    if atomic and kind in {'Claim','Decision'}:
        p['assertion']=[dict(subject='metric',predicate='value',value=value,unit='none',conditions=conditions or [])]
    else:
        p['content']='metric shared object'
    jid='job-'+node+'-'+entry['id']+'-'+value
    with s.db:
        s.db.execute('INSERT OR IGNORE INTO sg2_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (jid,s.config['project_id'],s.chat_id(entry['session_id']) if 'session_id' in entry else 'chat-1','chat-1','judging','{}',None,'[]','{}',None,'',''))
    graph._commit(s,dict(id=jid,chat=s.chat_id(entry['session_id']) if 'session_id' in entry else 'chat-1'),dict(nodes=[p],relations=[],unresolved=[]),
                  [],{'local':node},[],[],[],graph._stats(),(99,0),finalize=False)
    return p


@pytest.fixture
def env(tmp_path):
    root,s,path=make_store(tmp_path,[('assistant','metric value 1 none run-a run-b'),('assistant','metric value 2 none run-a run-b')])
    s.config['graph_v2']={'enabled':True,'revision_policy':{'enabled':True,'mode':'effective'}}
    config=root/'.sessiongraph/config.json'
    raw=json.loads(config.read_text());raw['graph_v2']=s.config['graph_v2'];config.write_text(json.dumps(raw))
    graph.setup(s)
    yield config,s,entries(s)
    s.close()


def seed_legacy_mapping(s):
    """Simulate a previously installed release, not a new compaction command."""
    run = 'a' * 32
    receipt = json.dumps(dict(previous=[], inserted=['b']))
    with s.db:
        s.db.execute('INSERT INTO sg_graph_redirects VALUES(?,?,?,?)',
                     ('b', 'a', s.config['project_id'], run))
        s.db.execute('INSERT INTO sg_graph_compactions VALUES(?,?,?,?,?,?)',
                     (run, s.config['project_id'], receipt, compact.revision(s), 'applied', 0))


def snapshots(s):
    return {t:[tuple(r) for r in s.db.execute('SELECT * FROM '+t+' ORDER BY 1')] for t in
        ['entries','work_memory','sg2_nodes','sg2_evidence','sg2_relations','sg_revision_items','sg_revision_sources']}


def test_legacy_mapping_reads_preserve_backing_and_never_call_models(env,monkeypatch):
    _,s,e=env
    publish(s,'a',e[0]);publish(s,'b',e[0],alias='uniquebkeyword')
    seed_legacy_mapping(s)
    before=snapshots(s)
    monkeypatch.setattr('socket.socket',lambda *a,**k:pytest.fail('network'))
    assert graph.diagnostics(s)['nodes']==1 and graph.diagnostics(s)['raw_nodes']==2
    assert len(graph.candidates(s,'metric'))==1
    assert len(resume(s)['memories'])==1
    detail=memory_detail(s,'b')['graph_v2']
    assert detail['requested_id']=='b' and detail['canonical_id']=='a'
    assert set(detail['member_ids'])=={'a','b'} and len(detail['assertions'])==2
    assert len(ledger.inspect(s,'b')['revisions'])==2
    assert recall(s,'uniquebkeyword',view='full',max_bytes=100000)['memories'][0]['id']=='a'
    assert work_map(s,query='uniquebkeyword')['page']['matched']==1
    assert recall(s,'*',view='full',max_bytes=100000)['retrieval']['matched_count']==1
    graph.setup(s)
    assert compact.canonical(s,'b')=='a' and snapshots(s)==before
    assert s.db.execute('SELECT count(*) FROM sg_graph_compactions').fetchone()[0]==1


def test_standalone_scanning_and_writing_apis_are_removed():
    from sessiongraph import condensation
    for name in ('preview','_apply','_undo','_assess_locked','run'):
        assert not hasattr(compact,name)
    for name in ('pairs','snapshot','assess','reviewed_groups'):
        assert not hasattr(condensation,name)


def test_upgrade_retires_growth_trigger_without_rewriting_existing_graph(env):
    _,s,e=env
    publish(s,'a',e[0]);publish(s,'b',e[0]);seed_legacy_mapping(s)
    with s.db:
        s.db.execute("INSERT INTO metadata VALUES('auto_compaction_node_inserts_v1','8')")
        s.db.execute('''CREATE TRIGGER sg_auto_compaction_insert AFTER INSERT ON sg2_nodes
            BEGIN UPDATE metadata SET value=CAST(value AS INTEGER)+1
            WHERE key='auto_compaction_node_inserts_v1'; END''')
    before=snapshots(s)
    graph.setup(s)
    assert snapshots(s)==before and compact.canonical(s,'b')=='a'
    assert not s.db.execute("SELECT 1 FROM sqlite_master WHERE name='sg_auto_compaction_insert'").fetchone()
    assert not s.db.execute("SELECT 1 FROM sqlite_master WHERE name='sg_graph_checks'").fetchone()
    publish(s,'c',e[1],value='2')
    assert s.db.execute("SELECT value FROM metadata WHERE key='auto_compaction_node_inserts_v1'").fetchone()[0]=='8'
    assert s.db.execute('SELECT count(*) FROM sg_graph_compactions').fetchone()[0]==1


@pytest.mark.parametrize('event',['manual','PostCompact','PreCompact'])
def test_update_never_invokes_retired_maintenance_even_with_stale_config(env,monkeypatch,event):
    import sys
    from types import SimpleNamespace
    from sessiongraph.auto_update import run_update
    config,_,_=env
    raw=json.loads(config.read_text())
    raw['graph_v2']['auto_compaction']={'enabled':True,'node_threshold':2}
    config.write_text(json.dumps(raw))
    monkeypatch.setitem(sys.modules,'sessiongraph.auto_compaction',SimpleNamespace(
        maybe_run=lambda *a,**k:pytest.fail('retired maintenance invoked')))
    def execute(argv,timeout):
        return {'added_entries':0,'skipped':[]} if 'refresh-chat' in argv else {
            'saved':[],'failed':[],'model_calls':0}
    result=run_update(config,'chat-1',event,execute=execute)
    assert result['state']==('prepared' if event=='PreCompact' else 'succeeded')
    assert result['reported_model_calls']==0 and 'auto_compaction' not in result


def test_hook_identity_routes_to_canonical_and_new_value_retains_history(env):
    _,s,e=env
    p=publish(s,'a',e[0]);publish(s,'b',e[0]);seed_legacy_mapping(s)
    stats=graph._stats()
    class NoModel:
        def evaluate(self,*a,**kw): pytest.fail('exact identity should not call a model')
    bindings,pending=graph._identities(s,dict(nodes=[p],relations=[]),graph.candidates(s,'metric'),NoModel(),stats)
    assert not pending and bindings['local']=='a'
    publish(s,'a',e[1],value='2')
    assert len(ledger.inspect(s,'b')['revisions'])==3
    assert ledger.projected(s,'b')['state']=='needs_review' # no newest-wins
    assert s.db.execute('SELECT count(*) FROM sg2_nodes').fetchone()[0]==2


def test_evidence_pagination_retains_each_original_owner(env):
    _,s,e=env
    publish(s,'a',e[0]);publish(s,'b',e[0]);seed_legacy_mapping(s)
    one=graph.node_details(s,'b',limit=1)
    two=graph.node_details(s,'b',limit=1,offset=one['evidence_page']['next_offset'])
    assert one['evidence_page']['has_more'] and not two['evidence_page']['has_more']
    assert {one['assertions'][0]['original_node_id'],two['assertions'][0]['original_node_id']}=={'a','b'}


def test_canonical_status_filters_alias_search_and_page_totals(env):
    _,s,e=env
    publish(s,'a',e[0]);publish(s,'b',e[0],alias='onlyaliasword');publish(s,'c',e[1],value='2');seed_legacy_mapping(s)
    assert recall(s,'*',limit=1,offset=1,view='full',max_bytes=100000)['retrieval']['matched_count']==2
    with s.db:s.db.execute("UPDATE work_memory SET status='superseded' WHERE id='a'")
    assert not recall(s,'onlyaliasword',view='full',max_bytes=100000)['memories']
    assert len(recall(s,'onlyaliasword',include_superseded=True,view='full',max_bytes=100000)['memories'])==1


def test_source_change_invalidates_canonical_validity_not_only_hidden_alias(env):
    _,s,e=env
    publish(s,'a',e[0]);publish(s,'b',e[0]);seed_legacy_mapping(s)
    assert ledger.projected(s,'a')['state']=='current'
    with s.db:s.db.execute('UPDATE entries SET timestamp=? WHERE id=?',('2026-10-28T00:00:00Z',e[0]['id']))
    assert ledger.projected(s,'b')['state']=='needs_review'


from test_chat_cleanup import env as cleanup_env


def test_source_deletion_dissolves_group_preserves_other_chat_and_never_undo_resurrects(cleanup_env):
    from sessiongraph import chat_cleanup
    config,s,a,b,_=cleanup_env
    for path in (a,b):
        with path.open('a') as stream:
            stream.write(json.dumps(dict(timestamp='2026-10-27T00:00:00Z',type='response_item',payload=dict(type='message',role='user',content=[dict(type='input_text',text='metric value 1 none')])))+'\n')
        s.ingest(path)
    def entry(chat):return hydrate(s, s.db.execute('SELECT e.* FROM entries e JOIN session_chats c ON c.segment_id=e.session_id WHERE c.chat_id=? AND instr(sg_source_text(e.id,e.text),?)>0 ORDER BY line LIMIT 1',(chat,'metric')).fetchone())
    ea,eb=entry('chat-a'),entry('chat-b')
    publish(s,'a',ea);publish(s,'b',ea);publish(s,'b',eb)
    seed_legacy_mapping(s)
    assert work_map(s,chat='chat-b')['page']['matched']==1
    assert recall(s,'metric',chat_id='chat-b',view='full',max_bytes=100000)['memories'][0]['id']=='a'
    a.unlink()
    result=chat_cleanup.cleanup(config,'chat-a',str(a))
    assert result['cleaned']
    assert s.db.execute('SELECT count(*) FROM sg_graph_redirects').fetchone()[0]==0
    assert not s.db.execute("SELECT 1 FROM sg2_nodes WHERE id='a'").fetchone()
    assert s.db.execute("SELECT 1 FROM sg2_nodes WHERE id='b'").fetchone()
    assert s.db.execute('PRAGMA foreign_key_check').fetchall()==[]
    assert len(graph.node_details(s,'b')['assertions'])==1
    assert not hasattr(compact,'run')


def test_review_of_alias_revision_updates_one_canonical_current_state(env):
    _,s,e=env
    publish(s,'a',e[0]);publish(s,'b',e[0]);seed_legacy_mapping(s)
    publish(s,'a',e[1],value='2')
    old=s.db.execute("SELECT id FROM sg_revision_items WHERE node='b'").fetchone()[0]
    new=s.db.execute("SELECT r.id FROM sg_revision_items r JOIN sg2_evidence e ON e.id=r.payload_evidence WHERE r.node='a' AND json_extract(e.assertion_json,'$.assertion[0].value')='2'").fetchone()[0]
    evidence=s.db.execute('SELECT evidence FROM sg_revision_sources WHERE revision=?',(new,)).fetchone()[0]
    manifest=dict(version=ledger.VERSION,project_id=s.config['project_id'],source=new,target=old,effect='corrects',facet='value',conditions={'qualifiers':[]},evidence=evidence,authority='source_report',effective_from='2026-10-26T00:00:00Z')
    with s.db:
        s.db.execute('BEGIN IMMEDIATE');ledger.assess(s,manifest,apply=True)
    assert ledger.projected(s,'b')['state']=='current'
    assert s.db.execute("SELECT content FROM work_memory WHERE id='a'").fetchone()[0]=='metric report a'
    # Both raw duplicate revisions remain, plus the new explicitly corrected value.
    assert len(ledger.inspect(s,'b')['revisions'])==3
    assert s.db.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_relation_projection_rewires_and_groups_without_deleting_evidence(env):
    _,s,e=env
    publish(s,'a',e[0]);publish(s,'b',e[0]);publish(s,'c',e[1],value='2')
    project=s.config['project_id']
    with s.db:
        for rid,source in [('r1','a'),('r2','b')]:
            s.db.execute('INSERT INTO sg2_relations VALUES(?,?,?,?,?,?,?,?,?,?)',(rid,project,source,'c','SUPPORTS','same scope','asserted','supported','{}',''))
            s.db.execute('INSERT INTO sg2_relation_evidence VALUES(?,?,?,?,?)',(rid+'e',rid,e[0]['id'],e[0]['text'],'job-'+source+'-'+e[0]['id']+'-1'))
    before=snapshots(s)
    seed_legacy_mapping(s)
    edges=graph.read_relations(s,['a','c'],both=True)
    assert len(edges)==1 and set(edges[0]['relation_ids'])=={'r1','r2'}
    assert edges[0]['source']=='a' and edges[0]['target']=='c'
    assert graph.read_relations(s,['b','c'],both=True)==edges
    assert snapshots(s)==before
