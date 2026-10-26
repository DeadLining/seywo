"""Regressions from the project backlog; no network or live database writes."""
import json
from pathlib import Path

from sessiongraph import continuation as c, input_pipeline as ip, graph_v2 as g
from sessiongraph.core import Store, init_project, load_config
from test_memories import make_store
from test_graph_v2 import Judge, output
from test_mvp import write_session


def configured(tmp_path, monkeypatch):
    monkeypatch.setenv('SESSIONGRAPH_WORKER_LOCK', str(tmp_path/'runtime/extract.lock'))
    path = init_project(tmp_path/'project')
    cfg = load_config(path)
    Store(cfg).close()
    return path, cfg


def receipt(**kw):
    return dict(dict(run_id='old', chat_id='chat', event='PostCompact', state='pending',
        error_code='graph_continuation_pending', target_rowid=12345, updated_at=1,
        graph_v2={'source_remaining': True}), **kw)


def test_failed_new_import_preserves_and_drains_prior_boundary(tmp_path, monkeypatch):
    path, cfg = configured(tmp_path, monkeypatch)
    c.record(path, receipt(), launch=False, now=1)
    failed = receipt(run_id='missing', state='failed', error_code='import_source_missing',
        target_rowid=None, updated_at=2, graph_v2={},
        import_diagnostics={'retryable':True}, source_transcript='/missing.jsonl')
    c.record(path, failed, launch=False, now=2)
    row = c.snapshot(cfg)[0]
    assert row['target'] == 12345
    assert row['state'] == 'ready'
    assert row['pending_import']['error_code'] == 'import_source_missing'
    c.record(path, receipt(event='AutoContinue', state='succeeded', error_code=None,
        updated_at=3, graph_v2={'source_remaining':False}), launch=False, now=3)
    row = c.snapshot(cfg)[0]
    assert row['state'] == 'blocked' and row['error_code'] == 'import_source_missing'
    assert row['target'] is None  # do not spin on the completed boundary


def test_subtract_empty_intervals_are_not_pending_work():
    assert ip.subtract([[0,0],[4,4]], []) == []
    assert ip.subtract([[0,4]], [[1,3]]) == [[0,1],[3,4]]


def test_empty_indexed_entry_does_not_consume_budget_forever(tmp_path):
    _, s, _ = make_store(tmp_path, [('assistant','placeholder'),('user','useful evidence')])
    g.setup(s)
    s.db.execute("UPDATE entries SET text='' WHERE role='assistant'")
    s.db.commit()
    calls=[]
    class Extract:
        def __call__(self,batch,policy):
            calls.append(batch)
            result = output([e for e in batch if e["text"].strip() and not e.get("context_only")])
            result["coverage"]["entry_ids"] = [e["id"] for e in batch]
            return result
    r=g.run_chat(s,Extract(),'chat-1',jev=Judge(),respect_schedule=True,step_budget=1)
    assert calls
    for _ in range(3):
        r=g.run_chat(s,Extract(),'chat-1',jev=Judge(),respect_schedule=True,step_budget=1)
    assert not r['graph_v2']['source_remaining']
    s.close()


def test_due_old_segment_precedes_fresh_segment_with_bounded_budget(tmp_path):
    root,s,_=make_store(tmp_path,[('user','newer segment evidence')],sid='chat-1')
    old=write_session(root/'old.jsonl',root,'chat-old',[('user','older pending evidence')])
    s.ingest(old)
    s.db.execute("UPDATE session_chats SET chat_id='chat-1' WHERE segment_id='chat-old'")
    s.db.commit()
    class Missing(Judge):
        def evaluate(self,*a):
            from sessiongraph.jev import JevUnavailable
            raise JevUnavailable('jev_tls_handshake',True)
    class Extract:
        def __call__(self,batch,policy):return output(batch)
    g.run_chat(s,Extract(),'chat-1',segment_id='chat-old',jev=Missing())
    s.db.execute('UPDATE sg2_control SET due=0');s.db.commit()
    old_job=s.db.execute("SELECT id FROM sg2_jobs WHERE segment='chat-old'").fetchone()[0]
    g.run_chat(s,Extract(),'chat-1',jev=Judge(),respect_schedule=True,step_budget=1)
    assert s.db.execute('SELECT state FROM sg2_jobs WHERE id=?',(old_job,)).fetchone()[0]=='complete'
    s.close()


def test_migrated_job_reports_current_backend_and_parent_block_without_writes(tmp_path, monkeypatch):
    root,s,_=make_store(tmp_path,[('user','test evidence')])
    monkeypatch.setenv('SESSIONGRAPH_WORKER_LOCK',str(tmp_path/'runtime/extract.lock'))
    class Missing(Judge):
        def evaluate(self,*a):
            from sessiongraph.jev import JevUnavailable
            raise JevUnavailable('jev_tls_handshake',True)
    class Extract:
        def __call__(self,batch,policy):return output(batch)
    g.run_chat(s,Extract(),'chat-1',jev=Missing())
    from sessiongraph.model_judge import reopen_legacy_judgments
    reopen_legacy_judgments(s,'chat-1')
    before=s.db.total_changes
    view=g.diagnostics(s)
    job=view['deferred_jobs'][0]
    assert job['error_code']=='graph_continuation_pending'
    assert job['historical_error_code']=='jev_tls_handshake'
    assert view['progress'].get('retry_wait',0)==0
    assert view['progress']['ready']==1
    assert s.db.total_changes==before
    path=root/'.sessiongraph/config.json'
    c.record(path,receipt(chat_id='chat-1',state='failed',error_code='import_source_missing',
        target_rowid=None,graph_v2={}),launch=False)
    view=g.diagnostics(s)
    assert view['blocked_jobs'][0]['error_code']=='import_source_missing'
    assert view['progress']['blocked']==1
    assert s.db.execute('SELECT error FROM sg2_jobs').fetchone()[0]=='jev_tls_handshake'
    s.close()


def test_import_failure_without_prior_boundary_stays_visible(tmp_path,monkeypatch):
    path,cfg=configured(tmp_path,monkeypatch)
    c.record(path,receipt(state='failed',error_code='import_source_missing',target_rowid=None,
        graph_v2={}),launch=False)
    assert c.snapshot(cfg)[0]['state']=='blocked'


def test_cursor_at_exact_end_is_not_work():
    assert ip.subtract([[17,17]],[])==[]


def test_archived_transcript_rebinds_same_segment_without_duplicate_evidence(tmp_path):
    root,s,path=make_store(tmp_path,[('user','original evidence')])
    archive=tmp_path/'archive';archive.mkdir()
    s.config['hook']['allowed_transcript_roots']=[str(root),str(archive)]
    before=[tuple(r) for r in s.db.execute('SELECT id,session_id,line,text FROM entries')]
    moved=archive/path.name;path.rename(moved)
    from sessiongraph.continuity import refresh_chat
    result=refresh_chat(s,'chat-1',str(path))
    assert not result['skipped']
    assert result['added_entries']==0
    assert [tuple(r) for r in s.db.execute('SELECT id,session_id,line,text FROM entries')]==before
    assert s.db.execute('SELECT path FROM sessions').fetchone()[0]==str(moved.resolve())
    assert s.db.execute('SELECT count(*) FROM sessions').fetchone()[0]==1
    s.close()


def test_changed_archive_does_not_rebind_existing_evidence(tmp_path):
    root,s,path=make_store(tmp_path,[('user','original evidence')])
    archive=tmp_path/'archive';archive.mkdir()
    s.config['hook']['allowed_transcript_roots']=[str(root),str(archive)]
    moved=archive/path.name;path.rename(moved)
    moved.write_text(moved.read_text().replace('original evidence','modified evidence'))
    from sessiongraph.continuity import refresh_chat
    result=refresh_chat(s,'chat-1',str(path))
    assert result['skipped']
    assert s.db.execute('SELECT path FROM sessions').fetchone()[0]==str(path)
    assert s.db.execute('SELECT count(*) FROM sessions').fetchone()[0]==1
    s.close()


import pytest
@pytest.mark.parametrize('changed',[False,True])
def test_full_discovery_never_duplicates_relocated_known_source(tmp_path, changed):
    root,s,path=make_store(tmp_path,[('user','original evidence')])
    archive=tmp_path/'aaa-archive';archive.mkdir()
    # Include the chat id so the manual discovery path sees the archived copy.
    renamed=path.with_name('chat-1.jsonl');path.rename(renamed)
    s.db.execute('UPDATE sessions SET path=?',(str(renamed),));s.db.commit();path=renamed
    s.config['hook']['allowed_transcript_roots']=[str(root),str(archive)]
    moved=archive/path.name;path.rename(moved)
    if changed:moved.write_text(moved.read_text().replace('original evidence','modified evidence'))
    from sessiongraph.continuity import refresh_chat
    result=refresh_chat(s,'chat-1')
    assert bool(result['skipped'])==changed
    assert s.db.execute('SELECT count(*) FROM sessions').fetchone()[0]==1
    assert s.db.execute('SELECT count(*) FROM entries').fetchone()[0]==1
    s.close()


def test_repeated_hook_using_pre_archive_path_still_resolves_without_duplicates(tmp_path):
    root,s,path=make_store(tmp_path,[('user','original evidence')])
    archive=tmp_path/'archive';archive.mkdir()
    s.config['hook']['allowed_transcript_roots']=[str(root),str(archive)]
    moved=archive/path.name;path.rename(moved)
    from sessiongraph.continuity import refresh_chat
    assert not refresh_chat(s,'chat-1',str(path))['skipped']
    assert not refresh_chat(s,'chat-1',str(path))['skipped']
    assert s.db.execute('SELECT count(*) FROM sessions').fetchone()[0]==1
    s.close()
