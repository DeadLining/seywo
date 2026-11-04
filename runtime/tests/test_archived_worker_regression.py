"""Exercise the real refresh -> worker CLI boundary without provider calls."""
import json
import os
import sys
import pytest
from sessiongraph.auto_update import _execute, run_update
from sessiongraph.continuity import refresh_chat
from sessiongraph.core import Store, load_config
from sessiongraph.failures import Failure
from sessiongraph.memories import resolve_segment
from test_memories import make_store


def archived(tmp_path):
    root,store,old=make_store(tmp_path,[('user','preserved source evidence')])
    archive=tmp_path/'archive';archive.mkdir()
    store.config['hook']['allowed_transcript_roots']=[str(root),str(archive)]
    path=root/'.sessiongraph/config.json';cfg=json.loads(path.read_text())
    cfg['hook']['allowed_transcript_roots']=[str(root),str(archive)];path.write_text(json.dumps(cfg))
    moved=archive/old.name;old.rename(moved)
    assert not refresh_chat(store,'chat-1',str(old))['skipped']
    return root,store,old,moved


def test_old_path_resolves_after_verified_archive_import_readonly(tmp_path):
    root,store,old,moved=archived(tmp_path)
    segment=resolve_segment(store,'chat-1',str(moved));store.close()
    read=Store(load_config(root/'.sessiongraph/config.json'),readonly=True)
    try:
        assert resolve_segment(read,'chat-1',str(old))==segment
        assert read.db.total_changes==0
    finally:read.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows extended paths')
def test_windows_extended_path_resolves_only_indexed_chat(tmp_path):
    root, store, path = make_store(tmp_path, [('user', 'source evidence')])
    try:
        normal = resolve_segment(store, 'chat-1', str(path))
        assert normal is not None
        assert resolve_segment(store, 'chat-1', '\\\\?\\' + str(path)) == normal
        assert resolve_segment(store, 'different-chat', '\\\\?\\' + str(path)) is None
        assert resolve_segment(store, 'chat-1', '\\\\?\\' + str(root / 'other.jsonl')) is None
    finally:
        store.close()


@pytest.mark.parametrize('mode',['changed','missing','outside','ambiguous','foreign','old_recreated'])
def test_alias_rejects_unsafe_or_unproven_sources(tmp_path,mode):
    root,store,old,moved=archived(tmp_path)
    if mode=='changed':moved.write_text(moved.read_text().replace('preserved','rewritten'))
    elif mode=='missing':moved.unlink()
    elif mode=='outside':old=tmp_path/'outside'/old.name
    elif mode=='ambiguous':
        row=store.db.execute('SELECT * FROM sessions').fetchone();cols=row.keys();values=list(row)
        values[cols.index('id')]='other';values[cols.index('path')]=str(tmp_path/'another'/old.name)
        store.db.execute(f"INSERT INTO sessions ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",values)
        store.db.execute('INSERT INTO session_chats VALUES(?,?)',('other','chat-1'));store.db.commit()
    elif mode=='old_recreated':old.write_text('different evidence')
    assert resolve_segment(store,'different-chat' if mode=='foreign' else 'chat-1',str(old)) is None
    store.close()


@pytest.mark.parametrize('event',['manual','AutoContinue'])
def test_refresh_and_real_worker_cli_accept_old_path(tmp_path,monkeypatch,event):
    root,store,old,moved=archived(tmp_path)
    target=store.db.execute('SELECT max(rowid) FROM entries').fetchone()[0];store.close()
    monkeypatch.setattr('sessiongraph.continuation.automatic_enabled',lambda *a:True)
    calls=[]
    def execute(argv,timeout):
        if 'refresh-chat' in argv:return _execute(argv,timeout)
        calls.append(argv)
        return _execute([sys.executable,'-B','-m','sessiongraph','--config',str(root/'.sessiongraph/config.json'),
            'memories','--chat-id','chat-1','--dry-run','--repair-oversize','--transcript-path',argv[argv.index('--memories-transcript-path')+1],
            '--through-rowid',str(target)],timeout)
    result=run_update(root/'.sessiongraph/config.json','chat-1',event,transcript=str(old),
        through_rowid=target if event=='AutoContinue' else None,execute=execute)
    assert calls
    assert result['state']=='succeeded',result


def test_known_cli_failure_keeps_safe_error_code():
    message='transcript_path is not indexed for this chat; run refresh-chat first'
    code='import sys,json;print(json.dumps({"error":'+repr(message)+'}),file=sys.stderr);sys.exit(2)'
    with pytest.raises(Failure) as caught:_execute([sys.executable,'-c',code],5)
    assert caught.value.code=='extraction_source_not_indexed'
    assert caught.value.stage=='worker'


def test_unknown_cli_error_does_not_leak_source_or_credentials():
    code='import sys,json;print(json.dumps({"error":"secret source and credential"}),file=sys.stderr);sys.exit(2)'
    with pytest.raises(Failure) as caught:_execute([sys.executable,'-c',code],5)
    assert caught.value.code=='worker_failed'
    assert 'secret' not in caught.value.detail


@pytest.mark.parametrize('event',['manual','AutoContinue'])
def test_scheduler_to_real_gateway_worker_with_fixture_provider(tmp_path,monkeypatch,event):
    root,store,old,moved=archived(tmp_path)
    target=store.db.execute('SELECT max(rowid) FROM entries').fetchone()[0];store.close()
    config=root/'.sessiongraph/config.json';cfg=json.loads(config.read_text())
    cfg['graph_v2']={'enabled':True};config.write_text(json.dumps(cfg))
    monkeypatch.setattr('sessiongraph.continuation.automatic_enabled',lambda *a:True)
    shim='''import sys
from contextlib import nullcontext
sys.path.insert(0, "tests")
from test_graph_v2 import Judge, output
import sessiongraph.extraction as extraction
import sessiongraph.graph_v2 as graph_v2
import sessiongraph.gateway_worker as worker
extraction.create_extractor=lambda config: (lambda batch, policy: output(batch))
class FixtureJudge(Judge):
    cache_identity=["fixture"]
    def __init__(self,config):super().__init__()
graph_v2.ModelJudge=FixtureJudge
worker.gateway_credentials=lambda *args: nullcontext()
raise SystemExit(worker.main())
'''
    def execute(argv,timeout):
        if 'refresh-chat' in argv:return _execute(argv,timeout)
        return _execute([sys.executable,'-B','-c',shim,*argv[3:]],timeout)
    result=run_update(config,'chat-1',event,transcript=str(old),
        through_rowid=target if event=='AutoContinue' else None,execute=execute)
    assert result['state']=='succeeded',result
    assert result['saved_count']>0
    s=Store(load_config(config),readonly=True)
    try:
        assert s.db.execute("SELECT count(*) FROM sg2_jobs WHERE state='complete'").fetchone()[0]>0
        assert s.db.execute('SELECT count(*) FROM sessions').fetchone()[0]==1
    finally:s.close()
