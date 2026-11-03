import fcntl
import json
import socket
from pathlib import Path

import pytest
from sessiongraph import continuation, maintenance, graph_v2 as g
from test_graph_v2 import Judge, output
from test_memories import make_store, entries, FakeExtractor


@pytest.fixture
def completed(tmp_path, monkeypatch):
    monkeypatch.delenv('SESSIONGRAPH_WORKER_LOCK',raising=False)
    monkeypatch.setenv('CODEX_HOME',str(tmp_path/'codex'))
    root,s,_ = make_store(tmp_path,[('user','训练 A 得到模型 B')])
    path=root/'.sessiongraph/config.json'
    s.config.setdefault('graph_v2',{})['enabled']=True
    g.run_chat(s,FakeExtractor([output(entries(s))]),'chat-1',jev=Judge())
    s.db.execute("UPDATE sg2_attempts SET run_id='old-run'")
    s.db.commit()
    s.config['_config_path']=str(path)
    target=s.db.execute('SELECT max(rowid) FROM entries').fetchone()[0]
    item=dict(config=str(path),chat='chat-1',state='blocked',target=target,run_id='old-run',error_code='jev_http_451')
    folder=continuation.root(s.config);folder.mkdir(parents=True,exist_ok=True)
    continuation.write(folder/'queue.json',{'old':item,'other':dict(item,config=str(tmp_path/'other/config.json'))})
    yield path,s,item
    s.close()


def test_read_reconciles_without_writes_and_cleanup_is_project_scoped(completed):
    path,s,item=completed
    q=continuation.root(s.config)/'queue.json';before=q.read_bytes()
    changed=s.db.total_changes
    assert continuation.snapshot(s.config)==[]
    assert q.read_bytes()==before and s.db.total_changes==changed
    result=maintenance.run(path,'clean_notices')
    assert result['removed']==1 and result['model_calls']==0
    assert set(json.loads(q.read_text()))=={'other'}
    assert s.db.execute("SELECT count(*) FROM sg2_attempts WHERE run_id='old-run'").fetchone()[0]>0
    assert maintenance.run(path,'clean_notices')['removed']==0


@pytest.mark.parametrize('condition',['no_target','unfinished','partial_cursor','unknown_run','import_error','running_attempt','newer_target'])
def test_never_hides_unproven_completion(completed,condition):
    path,s,item=completed
    if condition=='no_target':item.pop('target')
    if condition=='unfinished':s.db.execute("UPDATE sg2_jobs SET state='pending'")
    if condition=='partial_cursor':s.db.execute('UPDATE sg2_cursors SET line=0,start=1')
    if condition=='unknown_run':item['run_id']='unknown'
    if condition=='import_error':item['error_code']='import_failed'
    if condition=='running_attempt':s.db.execute("UPDATE sg2_attempts SET state='running'")
    if condition=='newer_target':
        s.db.execute('UPDATE sg2_cursors SET line=0,start=0');item['target']+=10
    s.db.commit()
    assert not continuation.completed_boundary(s,item)
    continuation.write(continuation.root(s.config)/'queue.json',{'old':item})
    assert continuation.snapshot(s.config)==[item]


def test_compaction_is_lossless_idempotent_model_free(completed,monkeypatch):
    path,s,_=completed
    monkeypatch.setattr(socket,'socket',lambda *a,**k:pytest.fail('network call'))
    before={name:[tuple(r) for r in s.db.execute(f'SELECT * FROM {name}')] for name in ('sg2_nodes','sg2_relations','sg2_evidence','sg_input_coverage','sg2_attempts')}
    value=json.dumps({'retained':'payload'*4000})
    s.db.execute('UPDATE sg2_jobs SET candidates_json=?',(value,));s.db.commit()
    preview=maintenance.run(path,'storage_preview')
    assert preview['before']['database_bytes']>0 and preview['model_calls']==0
    result=maintenance.run(path,'storage_compact')
    assert result['integrity']=='ok' and result['model_calls']==0
    assert result['archive']['payloads']>=1
    from sessiongraph.storage import payload_text
    assert payload_text(s,s.db.execute('SELECT candidates_json FROM sg2_jobs').fetchone()[0])==value
    for name,rows in before.items():assert [tuple(r) for r in s.db.execute(f'SELECT * FROM {name}')]==rows
    assert maintenance.run(path,'storage_compact')['archive']['payloads']==0


def test_maintenance_respects_active_batch_lock(completed):
    path,s,_=completed
    with (Path(s.config['database']).parent/'graph-v2.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(ValueError,match='maintenance_busy'):maintenance.run(path,'storage_compact')
        with pytest.raises(ValueError,match='maintenance_busy'):maintenance.run(path,'clean_notices')
        assert maintenance.run(path,'storage_preview')['model_calls']==0


def test_compaction_checks_only_payload_tables_and_skips_tiny_freelist(completed, monkeypatch):
    path, s, _ = completed
    statements = []
    original = maintenance.Store
    def traced(*args, **kwargs):
        store = original(*args, **kwargs)
        store.db.set_trace_callback(statements.append)
        return store
    monkeypatch.setattr(maintenance, 'Store', traced)
    result = maintenance.run(path, 'storage_compact')
    assert result['integrity'] == 'ok'
    assert set(result['integrity_scope']) == set(maintenance.PAYLOAD_TABLES)
    assert result['vacuum_performed'] is False
    assert result['vacuum_skipped_reason'] == 'small_freelist'
    assert 'VACUUM' not in statements
    assert 'PRAGMA quick_check' not in statements  # No 4GB source/FTS scan.
    assert 'PRAGMA wal_checkpoint(PASSIVE)' in statements
    assert 'PRAGMA wal_checkpoint(TRUNCATE)' in statements


def test_4gb_database_with_44kb_free_does_not_rewrite_all_pages(completed, monkeypatch):
    import time
    _, s, _ = completed
    statements = []
    s.db.set_trace_callback(statements.append)
    monkeypatch.setattr(maintenance, 'footprint', lambda _: dict(logical_bytes=4*1024**3, reclaimable_bytes=44*1024))
    result = maintenance.reclaim_space(s, time.monotonic()+30)
    assert result['vacuum_skipped_reason'] == 'small_freelist'
    assert not result['vacuum_performed'] and 'VACUUM' not in statements


def test_reclamation_expired_budget_returns_warning_without_losing_data(completed):
    import time
    _, s, _ = completed
    before = maintenance.counts(s)
    old_timeout = s.db.execute('PRAGMA busy_timeout').fetchone()[0]
    result = maintenance.reclaim_space(s, time.monotonic()-1)
    assert result['vacuum_skipped_reason'] == 'reclamation_deferred' and result['warning']
    assert not result['vacuum_performed']
    assert maintenance.counts(s) == before
    assert s.db.execute('PRAGMA busy_timeout').fetchone()[0] == old_timeout


def test_vacuum_interrupt_rolls_back_only_reclamation_and_restores_connection(completed, monkeypatch):
    _, s, _ = completed
    s.db.execute('CREATE TABLE maintenance_padding(data BLOB)')
    s.db.execute('INSERT INTO maintenance_padding VALUES(zeroblob(1048576))')
    s.db.commit()
    before = maintenance.counts(s)
    clock = [0.0]
    monkeypatch.setattr(maintenance.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(maintenance, 'footprint', lambda _: dict(logical_bytes=100*1024**2, reclaimable_bytes=50*1024**2))
    s.db.set_trace_callback(lambda sql: clock.__setitem__(0, 2.0) if sql == 'VACUUM' else None)
    result = maintenance.reclaim_space(s, 1.0)
    assert not result['vacuum_performed'] and result['vacuum_skipped_reason'] == 'reclamation_deferred'
    assert result['warning']
    assert maintenance.counts(s) == before
    assert s.db.execute('SELECT length(data) FROM maintenance_padding').fetchone()[0] == 1048576
    assert s.db.execute('PRAGMA busy_timeout').fetchone()[0] == 10000
    assert s.db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'


def test_active_reader_defers_reclamation_without_waiting_or_removing_facts(completed):
    import sqlite3, time
    _, s, _ = completed
    s.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    reader = sqlite3.connect(s.config['database'])
    try:
        reader.execute('BEGIN')
        reader.execute('SELECT count(*) FROM entries').fetchone()
        s.db.execute("INSERT OR REPLACE INTO metadata VALUES('maintenance_test','1')")
        s.db.commit()
        before = maintenance.counts(s)
        result = maintenance.reclaim_space(s, time.monotonic()+30)
        assert result['vacuum_skipped_reason'] == 'active_reader' and result['warning']
        assert maintenance.counts(s) == before
    finally:
        reader.close()


def test_large_reclaimable_space_still_runs_atomic_vacuum(completed, monkeypatch):
    import time
    _, s, _ = completed
    before = maintenance.counts(s)
    statements = []
    s.db.set_trace_callback(statements.append)
    monkeypatch.setattr(maintenance, 'footprint', lambda _: dict(logical_bytes=100*1024**2, reclaimable_bytes=50*1024**2))
    result = maintenance.reclaim_space(s, time.monotonic()+30)
    assert result['vacuum_performed'] and 'VACUUM' in statements
    assert maintenance.counts(s) == before


def test_disk_failure_is_not_silenced_as_reclamation_warning(completed, monkeypatch):
    import sqlite3, time
    _, s, _ = completed
    original = s.db
    failure = sqlite3.OperationalError('database or disk is full')
    failure.sqlite_errorcode = sqlite3.SQLITE_FULL
    class FailingVacuum:
        def execute(self, sql, *args):
            if sql == 'VACUUM':
                raise failure
            return original.execute(sql, *args)
        def set_progress_handler(self, *args):
            return original.set_progress_handler(*args)
    monkeypatch.setattr(maintenance, 'footprint', lambda _: dict(logical_bytes=100*1024**2, reclaimable_bytes=50*1024**2))
    try:
        s.db = FailingVacuum()
        with pytest.raises(sqlite3.OperationalError, match='disk is full'):
            maintenance.reclaim_space(s, time.monotonic()+30)
    finally:
        s.db = original
    assert s.db.execute('PRAGMA busy_timeout').fetchone()[0] == 10000


def test_validation_failure_rolls_back_payload_changes(completed, monkeypatch):
    path, s, _ = completed
    # This value would be archived in the maintenance transaction.
    value = json.dumps({'retained': 'do-not-lose-this' * 4000})
    s.db.execute('UPDATE sg2_jobs SET candidates_json=?', (value,))
    s.db.commit()
    before = maintenance.counts(s)
    def invalid(_):
        raise ValueError('maintenance_integrity_failed')
    monkeypatch.setattr(maintenance, 'check_payload_tables', invalid)
    with pytest.raises(ValueError, match='maintenance_integrity_failed'):
        maintenance.run(path, 'storage_compact')
    assert s.db.execute('SELECT candidates_json FROM sg2_jobs').fetchone()[0] == value
    assert maintenance.counts(s) == before
