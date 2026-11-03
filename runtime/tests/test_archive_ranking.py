import json
import random
import pytest
from sessiongraph import storage, memory, graph_v2 as g
from test_input_framework import fixture, msg
from test_lifecycle_cleanup import completed, resolve


def test_sql_ranking_matches_legacy_including_dirty_unicode_filters_and_pages(tmp_path,monkeypatch):
    s,_=fixture(tmp_path,[msg('fixture source')]);eid=s.db.execute('SELECT id FROM entries').fetchone()[0]
    rng=random.Random(41)
    fragments=['step30','step300','实验数据','数据路径','alpha beta','İstanbul','Straße','中文','ALPHA','a-b','*','汉字','ABC_DEF']
    with s.db:
        for i in range(180):
            title=' '.join(rng.sample(fragments,3));content=' '.join(rng.sample(fragments,3))
            s.db.execute('INSERT INTO work_memory VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
             (f'w{i}',s.config['project_id'] if i%13 else 'other','chat'+str(i%2),'finding',title,content,'project',
              'superseded' if i%7==0 else 'active',json.dumps(rng.sample(fragments,2)),json.dumps(rng.sample(fragments,2)),eid,'fixture source',None,'now',str(i%3)))
    storage.flush_search(s.db)
    with s.db:s.db.execute("UPDATE work_memory SET title='newneedle 数据' WHERE id='w3'")
    original=storage.exists
    for query in fragments+['newneedle','alpha 数据','missingneedle','!!!']:
        for chat in [None,'chat1']:
            for offset in [0,3,500]:
                got=s.recall(query,chat_id=chat,offset=offset,max_bytes=50000,view='full',include_superseded=True)
                with monkeypatch.context() as m:
                    m.setattr(storage,'exists',lambda db,name:False if name=='memory_search' else original(db,name))
                    expected=s.recall(query,chat_id=chat,offset=offset,max_bytes=50000,view='full',include_superseded=True)
                assert [x['id'] for x in got['memories']]==[x['id'] for x in expected['memories']],(query,chat,offset)
                assert [x['match'] for x in got['memories']]==[x['match'] for x in expected['memories']]
                assert got['retrieval']['matched_count']==expected['retrieval']['matched_count']


def test_archive_roundtrip_dedup_rollback_and_pending_resolution(tmp_path):
    s,o=completed(tmp_path,'insufficient_evidence');job=s.db.execute('SELECT * FROM sg2_jobs').fetchone()
    raw=g.dumps(o);large=g.dumps({**o,'padding':'重复证据'*500})
    with s.db:
        s.db.execute('UPDATE sg2_jobs SET output_json=?,candidates_json=?',(large,large))
    s.db.execute('BEGIN IMMEDIATE');r=storage.archive_completed_payloads(s);assert r['payloads']==2 and r['deduplicated']==1
    assert storage.payload_text(s,s.db.execute('SELECT output_json FROM sg2_jobs').fetchone()[0])==large
    s.db.rollback()
    assert s.db.execute('SELECT output_json FROM sg2_jobs').fetchone()[0]==large
    with s.db:
        storage.archive_completed_payloads(s)
        # Legacy relation exercises archive reading, rather than only bound endpoints.
        s.db.execute('UPDATE sg2_unresolved SET payload_json=?',(json.dumps(o['relations'][0]),))
        s.db.execute('UPDATE sg2_jobs SET candidates_json=?',('[]',))
    resolve(s,o)
    assert s.db.execute('SELECT revision FROM sg2_unresolved').fetchone()[0]=='resolver'
    assert storage.archive_completed_payloads(s)['payloads']==0


@pytest.mark.parametrize('state',['pending','extracting','superseded'])
def test_archive_never_touches_unfinished_or_superseded(tmp_path,state):
    s,o=completed(tmp_path)
    raw=json.dumps({'large':'x'*5000})
    with s.db:s.db.execute('UPDATE sg2_jobs SET state=?,output_json=?',(state,raw))
    with s.db:assert storage.archive_completed_payloads(s)['payloads']==0
    assert s.db.execute('SELECT output_json FROM sg2_jobs').fetchone()[0]==raw


@pytest.mark.parametrize('damage',['missing','corrupt','hash'])
def test_archive_corruption_never_becomes_valid_evidence(tmp_path,damage):
    s,o=completed(tmp_path)
    with s.db:
        s.db.execute('UPDATE sg2_jobs SET output_json=?',(json.dumps({'large':'x'*5000}),))
        storage.archive_completed_payloads(s)
    ref=s.db.execute('SELECT output_json FROM sg2_jobs').fetchone()[0]
    with s.db:
        if damage=='missing':s.db.execute('DELETE FROM sg_payload_archive')
        elif damage=='corrupt':s.db.execute("UPDATE sg_payload_archive SET data=x'00'")
        else:s.db.execute('UPDATE sg_payload_archive SET size=size+1')
    with pytest.raises(ValueError,match='graph_archive_'):storage.payload_text(s,ref)


def test_judgment_context_deduplicates_windows_and_keeps_endpoint_sources(tmp_path):
    s,_=fixture(tmp_path,[msg('Original execution produced report and measured accuracy 95%.'),msg('Separate endpoint evidence')])
    from sessiongraph.source_text import hydrate
    rows=[hydrate(s, row) for row in s.db.execute('SELECT * FROM entries ORDER BY line')]
    objects=[{'evidence':[{'entry_id':rows[0]['id'],'quote':q}]} for q in ['execution','report','accuracy']]
    objects.append({'content':'invented secret relation','evidence':[{'entry_id':rows[1]['id'],'quote':'endpoint'}]})
    context=g._evidence_context(s,objects)
    assert len(context)==2
    assert context[0]['context']==rows[0]['text'] and context[1]['context']==rows[1]['text']
    assert 'invented' not in json.dumps(context)
