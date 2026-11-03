"""Source-backed and mixed-storage contracts; synthetic data, no provider calls."""
import json
from pathlib import Path

import pytest

from sessiongraph import source_locator, source_retention, source_text, storage
from sessiongraph.core import Store
from sessiongraph.graph_v2 import run_chat, read_relations, _restore_batch
from sessiongraph.input_pipeline import describe, plan, commit, units
from test_input_framework import legacy_fixture as fixture, msg, Empty
from test_graph_v2 import Judge, output


def originals(store):
    return [source_text.hydrate(store, r) for r in store.db.execute('SELECT * FROM entries ORDER BY line')]


def activate(store):
    result = source_text.migrate(store, apply=True)
    assert result['migrated'] == result['verified'] and not result['retained']
    return result


def test_new_import_has_no_body_or_search_body_copy_and_reads_exactly(tmp_path):
    s, p = fixture(tmp_path, [msg('original evidence alpha 中文')])
    before = originals(s)
    activate(s)
    # Append a second source record after activation, not a second transcript scan.
    with p.open('a') as stream:
        stream.write(json.dumps(dict(type='response_item', timestamp='new', payload=msg('new beta 中文')), ensure_ascii=False)+'\n')
    assert s.ingest(p)['added'] == 1
    assert s.db.execute("SELECT count(*) FROM entries WHERE text<>''").fetchone()[0] == 0
    assert s.db.execute("SELECT count(*) FROM docs WHERE type='entry'").fetchone()[0] == 0
    assert all(r[0] is None for r in s.db.execute('SELECT body FROM sg_source_search'))
    assert originals(s)[0] == before[0]
    second = originals(s)[1]
    assert second['text'] == 'new beta 中文'
    sql = []
    s.db.set_trace_callback(sql.append)
    assert s.search('beta', mode='raw')['hits'][0]['id'] == second['id']
    s.db.set_trace_callback(None)
    assert not any("SELECT body FROM docs WHERE id=" in q for q in sql)
    expanded = s.expand(second['id'], radius=0)
    assert expanded['entries'][0]['text'] == second['text']
    assert expanded['entries'][0]['source_location']['storage'] == 'codex_source'
    assert expanded['model_calls'] == 0
    assert s.db.execute('SELECT chars FROM sg_entry_sizes WHERE entry_id=?', (second['id'],)).fetchone()[0] == len(second['text'])


def test_mixed_search_migration_readonly_preview_and_idempotency(tmp_path):
    s, _ = fixture(tmp_path, [msg('alpha 真实结果'), msg('beta 真实结果')])
    before = [h['id'] for h in s.search('真实结果', mode='raw')['hits']]
    ro = Store(s.config, readonly=True)
    changes = ro.db.total_changes
    preview = source_text.migrate(ro, limit=1)
    assert preview['verified'] == 1 and preview['migrated'] == 0 and preview['more']
    assert ro.db.total_changes == changes and not source_text.enabled(ro)
    ro.close()
    report = source_text.migrate(s, apply=True, limit=1)
    assert report['migrated'] == 1 and report['more']
    assert [h['id'] for h in s.search('真实结果', mode='raw')['hits']] == before
    assert source_text.migrate(s, apply=True, after=report['next_cursor'])['migrated'] == 1
    assert source_text.migrate(s, apply=True)['migrated'] == 0
    assert s.db.execute('PRAGMA foreign_key_check').fetchall() == []


@pytest.mark.parametrize('damage', ['missing', 'changed', 'offset', 'metadata'])
def test_migration_never_discards_unverifiable_inline_evidence(tmp_path, damage):
    s, p = fixture(tmp_path, [msg('original evidence')])
    old = originals(s)[0]
    if damage == 'missing':
        p.unlink()
    elif damage == 'changed':
        p.write_text(p.read_text().replace('original evidence', 'modified evidence'))
    elif damage == 'offset':
        with s.db:
            s.db.execute('UPDATE sg_source_locations SET byte_offset=-1')
    else:
        with s.db:
            s.db.execute("UPDATE entries SET role='user'")
    report = source_text.migrate(s, apply=True)
    assert report['migrated'] == 0 and len(report['retained']) == 1
    assert s.db.execute('SELECT text FROM entries').fetchone()[0] == old['text']
    assert s.db.execute("SELECT count(*) FROM docs WHERE type='entry'").fetchone()[0] == 1


def test_source_absence_reports_gap_does_not_delete_graph_and_retained_exact_fallback(tmp_path):
    s, p = fixture(tmp_path, [msg('measured result alpha')])
    e = originals(s)[0]
    mid = s.remember(kind='finding', title='alpha', content='measured result',
                     source_entry_id=e['id'], source_quote=e['text'])['id']
    activate(s)
    p.unlink()
    result = s.search('alpha', mode='raw')
    assert not result['hits'] and result['unavailable_sources'][0]['status'] == 'source_missing'
    with pytest.raises(ValueError, match='source_missing'):
        s.expand(e['id'], radius=0)
    assert s.db.execute('SELECT id FROM work_memory WHERE id=?', (mid,)).fetchone()
    recalled = s.recall('alpha', view='full')['memories'][0]
    assert recalled['verification'] == 'source_unavailable'
    assert recalled['source']['status'] == 'source_missing'


def test_critical_retained_snapshot_remains_readable_without_source(tmp_path):
    s, p = fixture(tmp_path, [msg('published exact source')])
    eid = originals(s)[0]['id']
    activate(s)
    with s.db:
        source_retention.setup(s)
        source_retention.capture(s, eid)
    p.unlink()
    assert s.expand(eid, radius=0)['entries'][0]['text'] == 'published exact source'
    assert source_retention.read(s, eid)['status'] == 'retained_verified'


def test_graph_extraction_relation_evidence_restore_and_noop_after_externalization(tmp_path):
    s, _ = fixture(tmp_path, [msg('training A produced artifact B')])
    batch = originals(s)
    activate(s)
    expected = output(batch, title='training A', artifact='artifact B')
    result = run_chat(s, lambda b, p: expected, 'input-chat', jev=Judge())
    assert not result['failed'], result
    assert len(result['saved']) == 2 and len(result['saved_relations']) == 1
    assert read_relations(s)[0]['kind'] == 'PRODUCES'
    assert s.recall('training A')['memories']
    repeat = run_chat(s, Empty(), 'input-chat', jev=Judge())
    assert repeat['model_calls'] == 0


def test_boundary_and_covered_cursor_checks_do_not_open_source(tmp_path, monkeypatch):
    s, _ = fixture(tmp_path, [msg('run A', role='user'), msg('result'), msg('yes', role='user')])
    before = units(s, 'input-chat')
    activate(s)
    assert units(s, 'input-chat') == before
    selected = plan(s, 'input-chat', 1, 0, 8000)
    assert selected and selected['entries']
    assert _restore_batch(s, [describe(e) for e in selected['entries']])
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', lambda *a, **k: pytest.fail('covered cursor read opened source'))
        with s.db:
            commit(s, selected, 'test-job', 'input-chat', s.config['project_id'])
        assert plan(s, 'input-chat', 1, 0, 8000) is None


def test_batch_budget_atomic_failure_and_raw_dedup(tmp_path, monkeypatch):
    s, p = fixture(tmp_path, [msg('first evidence'), msg('second evidence')])
    first = originals(s)[0]
    raw = p.read_text().splitlines()[first['line']-1]
    with s.db:
        s.db.execute('INSERT INTO raw_events VALUES(?,?,?)', (first['session_id'], first['line'], raw))
    assert source_text.migrate(s, apply=True, max_bytes=1)['migrated'] == 0
    original_index = source_text.index
    calls = []
    def fail_second(*args):
        calls.append(1)
        if len(calls) == 2:
            raise ValueError('injected_failure')
        return original_index(*args)
    with monkeypatch.context() as patch:
        patch.setattr(source_text, 'index', fail_second)
        with pytest.raises(ValueError, match='injected_failure'):
            source_text.migrate(s, apply=True)
    assert s.db.execute('SELECT count(*) FROM sg_external_entries').fetchone()[0] == 0
    assert s.db.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 1
    result = activate(s)
    assert result['raw_bytes_removed'] == len(raw.encode())


def test_source_backed_entry_delete_cleans_index_without_original(tmp_path):
    s, p = fixture(tmp_path, [msg('alpha evidence')])
    activate(s)
    p.unlink()
    with s.db:
        # The real cascade removes dependent source locations before entries.
        s.db.execute('DELETE FROM sg_source_locations')
        s.db.execute('DELETE FROM entries')
    assert s.db.execute('SELECT count(*) FROM sg_external_entries').fetchone()[0] == 0
    assert s.db.execute("SELECT count(*) FROM sg_source_search WHERE sg_source_search MATCH 'alpha'").fetchone()[0] == 0


def test_legacy_event_migration_does_not_change_source_backed_hash(tmp_path):
    s, p = fixture(tmp_path, [msg('alpha evidence')])
    activate(s)
    row = originals(s)[0]
    with s.db:
        s.db.execute('INSERT INTO raw_events VALUES(?,?,?)',
                     (row['session_id'], row['line'], p.read_text().splitlines()[row['line']-1]))
    report = storage.migrate_sources(s)
    assert report['removed_events'] == 1
    assert source_locator.read_original(s, row['id'])['text'] == row['text']


def test_real_source_cleanup_cascades_external_index_and_graph(tmp_path):
    from sessiongraph.chat_cleanup import cleanup
    s, p = fixture(tmp_path, [msg('training A produced artifact B')])
    batch = originals(s)
    activate(s)
    result = run_chat(s, lambda b, p: output(batch), 'input-chat', jev=Judge())
    assert not result['failed']
    config_path = Path(s.config['_config_path'])
    config = json.loads(config_path.read_text())
    config['hook'].update(cleanup_missing_chats=True, allowed_transcript_roots=[str(tmp_path)])
    config_path.write_text(json.dumps(config))
    p.unlink()
    result = cleanup(config_path, 'input-chat', str(p))
    assert result['cleaned'], result
    assert s.db.execute('SELECT count(*) FROM entries').fetchone()[0] == 0
    assert s.db.execute('SELECT count(*) FROM sg_external_entries').fetchone()[0] == 0
    assert s.db.execute('SELECT count(*) FROM sg2_nodes').fetchone()[0] == 0
    assert s.db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_cli_preview_activation_and_old_id_reads(tmp_path, capsys):
    from sessiongraph.cli import main
    s, _ = fixture(tmp_path, [msg('source-backed cli evidence')])
    eid = originals(s)[0]['id']
    args = ['--config', s.config['_config_path']]
    assert main(args + ['source-externalize']) == 0
    preview = json.loads(capsys.readouterr().out)
    assert not preview['applied'] and preview['verified'] == 1
    assert main(args + ['source-externalize', '--apply']) == 0
    assert json.loads(capsys.readouterr().out)['migrated'] == 1
    assert main(args + ['expand', eid, '--radius', '0']) == 0
    assert json.loads(capsys.readouterr().out)['entries'][0]['text'] == 'source-backed cli evidence'


def test_missing_legacy_locators_recover_incrementally_without_event_copies(tmp_path):
    s, _ = fixture(tmp_path, [msg('first evidence'), msg('second evidence')])
    before = originals(s)
    with s.db:
        s.db.execute('DELETE FROM sg_source_locations')
    report = activate(s)
    assert report['locators_backfilled'] == 2
    assert originals(s) == before
    assert s.db.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 0
    assert s.db.execute('SELECT count(*) FROM sg_source_locations WHERE byte_offset IS NULL').fetchone()[0] == 0


def test_legacy_locator_recovery_honors_actual_scan_budget(tmp_path):
    s, p = fixture(tmp_path, [msg('a' * 3000), msg('short evidence')])
    second = originals(s)[1]
    with s.db:
        s.db.execute('DELETE FROM sg_source_locations')
    report = source_text.migrate(s, apply=True, after=s.db.execute('SELECT rowid FROM entries WHERE id=?', (originals(s)[0]['id'],)).fetchone()[0], max_bytes=512)
    assert report['budget_exhausted'] and report['bytes_examined'] <= 512
    assert not source_text.external(s, second['id'])
    assert s.db.execute('SELECT text FROM entries WHERE id=?', (second['id'],)).fetchone()[0] == second['text']


def test_missing_source_never_scans_historical_locator_predecessors(tmp_path):
    s, p = fixture(tmp_path, [msg('missing source')])
    eid = originals(s)[0]['id']
    with s.db:
        s.db.execute('DELETE FROM sg_source_locations')
    p.unlink()
    queries = []
    s.db.set_trace_callback(queries.append)
    result = source_text.migrate(s, apply=True)
    assert result['retained'][0]['reason'] == 'missing'
    assert not any('ORDER BY e.line DESC' in q for q in queries)


def test_stable_search_mapping_survives_rowid_changes_vacuum_append_and_delete(tmp_path):
    s, p = fixture(tmp_path, [msg('alpha evidence'), msg('beta evidence')])
    activate(s)
    before = originals(s)
    with s.db:
        s.db.execute('UPDATE entries SET rowid=rowid+1000')
    s.db.execute('VACUUM')
    for e in before:
        term = e['text'].split()[0]
        assert s.search(term, mode='raw')['hits'][0]['id'] == e['id']
        assert s.expand(e['id'], radius=0)['entries'][0]['text'] == e['text']
    with p.open('a') as stream:
        stream.write(json.dumps(dict(type='response_item', timestamp='new', payload=msg('gamma evidence')))+'\n')
    assert s.ingest(p)['added'] == 1
    assert s.search('gamma', mode='raw')['hits']
    with s.db:
        s.db.execute('DELETE FROM sg_source_locations WHERE entry_id=?', (before[0]['id'],))
        s.db.execute('DELETE FROM entries WHERE id=?', (before[0]['id'],))
    assert not s.search('alpha', mode='raw')['hits']
    assert s.search('beta', mode='raw')['hits'][0]['id'] == before[1]['id']
    assert s.db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_v2_readonly_search_then_writable_upgrade_preserves_search_identity(tmp_path):
    s, _ = fixture(tmp_path, [msg('alpha evidence'), msg('beta evidence')])
    activate(s)
    before = originals(s)
    with s.db:
        s.db.execute('DROP TRIGGER sg_source_search_delete')
        s.db.execute('''CREATE TABLE legacy_external(
            entry_id TEXT PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE,
            chars INTEGER NOT NULL,bytes INTEGER NOT NULL,preview TEXT NOT NULL) WITHOUT ROWID''')
        s.db.execute('INSERT INTO legacy_external SELECT entry_id,chars,bytes,preview FROM sg_external_entries')
        s.db.execute('DROP TABLE sg_external_entries')
        s.db.execute('ALTER TABLE legacy_external RENAME TO sg_external_entries')
        s.db.execute("UPDATE metadata SET value='2' WHERE key='schema_version'")
    config = s.config
    s.close()
    ro = Store(config, readonly=True)
    assert ro.search('alpha', mode='raw')['hits'][0]['id'] == before[0]['id']
    assert ro.db.total_changes == 0
    ro.close()
    s = Store(config)
    assert s.db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] == '4'
    assert s.db.execute('SELECT count(*) FROM sg_external_entries WHERE search_rowid IS NULL').fetchone()[0] == 0
    with s.db:
        s.db.execute('UPDATE entries SET rowid=rowid+1000')
    s.db.execute('VACUUM')
    assert s.search('beta', mode='raw')['hits'][0]['id'] == before[1]['id']
