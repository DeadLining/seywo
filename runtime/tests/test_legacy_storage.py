"""Lossless legacy retirement, synthetic sources only, no provider calls."""
import json

import pytest

from sessiongraph import legacy_storage as legacy, source_text, source_locator, source_retention
from sessiongraph.core import Store
from sessiongraph.maintenance import optimize_legacy
from test_input_framework import legacy_fixture as fixture, msg


def migrate_all(s, phase, limit=1):
    cursor = 0
    total = 0
    while True:
        r = legacy.migrate(s, phase=phase, apply=True, limit=limit, after=cursor)
        total += r['migrated']
        if not r['more']:
            return total
        assert r['next_cursor'] > cursor
        cursor = r['next_cursor']


def seed_events(s, p):
    lines = p.read_text().splitlines()
    with s.db:
        for e in s.db.execute('SELECT * FROM entries').fetchall():
            s.db.execute('INSERT INTO raw_events VALUES(?,?,?)',
                         (e['session_id'], e['line'], lines[e['line']-1]))
    return {r['line']: r['event_json'] for r in s.db.execute('SELECT * FROM raw_events')}


def test_readonly_preview_budget_and_legacy_missing_sources_stay_searchable(tmp_path):
    s, p = fixture(tmp_path, [msg('alpha original 中文'), msg('beta original 中文')])
    ids = [r['id'] for r in s.db.execute('SELECT * FROM entries ORDER BY line')]
    p.unlink()
    ro = Store(s.config, readonly=True)
    result = legacy.migrate(ro, phase='search')
    assert result['examined'] == 2 and result['migrated'] == 0
    assert ro.db.total_changes == 0
    ro.close()
    result = legacy.migrate(s, phase='search', apply=True, max_bytes=1)
    assert result['budget_exhausted'] and result['migrated'] == 0
    assert not source_text.unified(s)
    assert legacy.migrate(s, phase='search', apply=True, limit=1)['migrated'] == 1
    assert s.search('alpha', mode='raw')['hits'][0]['id'] == ids[0]
    assert s.search('beta', mode='raw')['hits'][0]['id'] == ids[1]
    migrate_all(s, 'search')
    assert source_text.unified(s)
    queries = []
    s.db.set_trace_callback(queries.append)
    assert s.search('中文', mode='raw')['hits']
    assert not any('FROM docs' in q for q in queries)
    s.db.set_trace_callback(None)
    assert s.db.execute('SELECT count(*) FROM docs').fetchone()[0] == 0
    assert s.db.execute('SELECT count(*) FROM entries WHERE text<>\'\'').fetchone()[0] == 2
    assert s.db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] == '4'
    assert all(r[0] is None for r in s.db.execute('SELECT body FROM sg_source_search'))
    assert legacy.migrate(s, phase='search', apply=True)['migrated'] == 0


def test_mixed_external_inline_index_vacuum_and_future_writers(tmp_path):
    s, p = fixture(tmp_path, [msg('alpha original'), msg('beta original')])
    assert source_text.migrate(s, apply=True, limit=1)['migrated'] == 1
    ids = [r['id'] for r in s.db.execute('SELECT * FROM entries ORDER BY line')]
    migrate_all(s, 'search')
    assert s.db.execute('SELECT count(*) FROM sg_search_keys').fetchone()[0] == 2
    with s.db:
        s.db.execute('UPDATE entries SET rowid=rowid+1000')
    s.db.execute('VACUUM')
    assert s.search('alpha', mode='raw')['hits'][0]['id'] == ids[0]
    assert s.search('beta', mode='raw')['hits'][0]['id'] == ids[1]
    assert source_text.migrate(s, apply=True)['migrated'] == 1
    assert s.db.execute('SELECT count(*) FROM sg_search_keys').fetchone()[0] == 2
    with p.open('a') as f:
        f.write(json.dumps(dict(type='response_item', timestamp='new', payload=msg('gamma original')))+'\n')
    assert s.ingest(p)['added'] == 1
    assert s.search('gamma', mode='raw')['hits']
    s.remember(kind='finding', title='gamma', content='original',
               source_entry_id=ids[0], source_quote='alpha')
    assert s.db.execute('SELECT count(*) FROM docs').fetchone()[0] == 0
    assert s.recall('gamma')['memories']
    config = s.config
    s.close()
    s = Store(config)
    assert s.db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] == '4'
    assert s.search('beta', mode='raw')['hits'][0]['id'] == ids[1]


def test_events_roundtrip_metadata_and_retention_without_original(tmp_path):
    s, p = fixture(tmp_path, [msg('alpha evidence ' * 100), msg('beta evidence')])
    before = seed_events(s, p)
    eid = s.db.execute('SELECT id FROM entries ORDER BY line').fetchone()[0]
    ro = Store(s.config, readonly=True)
    assert legacy.migrate(ro, phase='events')['examined'] == 2
    assert ro.db.total_changes == 0
    ro.close()
    assert legacy.migrate(s, phase='events', apply=True, max_bytes=1)['migrated'] == 0
    assert migrate_all(s, 'events') == 2
    assert s.db.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 0
    assert legacy.migrate(s, phase='events', apply=True)['migrated'] == 0
    for line, text in before.items():
        assert legacy.read_raw(s, 'input-chat', line) == text
        assert legacy.raw_size(s, 'input-chat', line) == len(text.encode())
    p.unlink()
    with s.db:
        s.db.execute('DELETE FROM sg_source_locations WHERE entry_id=?', (eid,))
        assert source_locator.metadata(s, s.db.execute('SELECT * FROM entries WHERE id=?', (eid,)).fetchone())
        source_retention.setup(s)
        source_retention.capture(s, eid)
    assert source_retention.read(s, eid)['text'] == 'alpha evidence ' * 100


@pytest.mark.parametrize('phase', ['search', 'events'])
def test_failed_batch_rolls_back_all_entries(tmp_path, monkeypatch, phase):
    s, p = fixture(tmp_path, [msg('alpha'), msg('beta')])
    seed_events(s, p)
    calls = []
    owner, name = (source_text, 'index_body') if phase == 'search' else (legacy, '_unpack')
    original = getattr(owner, name)
    def fail_second(*a, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise ValueError('injected_failure')
        return original(*a, **kw)
    monkeypatch.setattr(owner, name, fail_second)
    with pytest.raises(ValueError, match='injected_failure'):
        legacy.migrate(s, phase=phase, apply=True)
    assert s.db.execute('SELECT count(*) FROM docs').fetchone()[0] == 2
    assert s.db.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 2
    assert not source_text.unified(s)


def test_archive_corruption_and_identity_conflict_keep_original(tmp_path):
    s, p = fixture(tmp_path, [msg('alpha')])
    before = seed_events(s, p)
    migrate_all(s, 'events')
    with s.db:
        s.db.execute("UPDATE sg_raw_archive SET hash='bad'")
        s.db.execute('INSERT INTO raw_events VALUES(?,?,?)', ('input-chat', next(iter(before)), next(iter(before.values()))))
    with pytest.raises(ValueError, match='raw_archive_hash_mismatch'):
        legacy.migrate(s, phase='events', apply=True)
    assert s.db.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 1


def test_real_cleanup_deletes_unified_index_and_raw_archive(tmp_path):
    from sessiongraph.chat_cleanup import cleanup
    s, p = fixture(tmp_path, [msg('alpha')])
    seed_events(s, p)
    migrate_all(s, 'events')
    migrate_all(s, 'search')
    config_path = s.config['_config_path']
    from pathlib import Path
    config = json.loads(Path(config_path).read_text())
    config['hook'].update(cleanup_missing_chats=True, allowed_transcript_roots=[str(tmp_path)])
    Path(config_path).write_text(json.dumps(config))
    p.unlink()
    assert cleanup(config_path, 'input-chat', str(p))['cleaned']
    for table in ('entries', 'sg_raw_archive', 'sg_search_keys', 'sg_source_search'):
        assert s.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
    assert s.db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_cli_and_maintenance_preview_apply(tmp_path, capsys):
    from sessiongraph.cli import main
    s, _ = fixture(tmp_path, [msg('alpha')])
    args = ['--config', s.config['_config_path'], 'legacy-optimize', '--phase', 'search']
    assert main(args) == 0
    assert not json.loads(capsys.readouterr().out)['applied']
    assert main(args + ['--apply']) == 0
    assert json.loads(capsys.readouterr().out)['search_retired']
    assert optimize_legacy(s.config, phase='search', apply=True)['migrated'] == 0


def test_reviewed_records_migrate_keep_scope_and_future_replacement(tmp_path):
    from sessiongraph.reviewed import publish_reviewed
    from sessiongraph.retrieval import _candidates, query_terms
    s, _ = fixture(tmp_path, [msg('alpha evidence'), msg('beta evidence')])
    batch = [dict(r) for r in s.db.execute('SELECT * FROM entries ORDER BY line')]
    def publish(entries, replace=()):
        output = dict(summary='fixture', relations=[], records=[dict(
            id=str(i), kind='Constraint', content='restriction ' + e['text'], scope='test',
            aliases=['reviewedlabel'], evidence=[dict(entry_id=e['id'], quote=e['text'])])
            for i, e in enumerate(entries)])
        return publish_reviewed(s, entries, output, {}, replace_job_ids=list(replace))
    first = publish(batch[:1])
    before = _candidates(s, query_terms('reviewedlabel'), 'record', 'input-chat')[0]
    assert len(before) == 1
    migrate_all(s, 'search')
    assert _candidates(s, query_terms('reviewedlabel'), 'record', 'input-chat')[0][0][0] == before[0][0]
    assert not _candidates(s, query_terms('reviewedlabel'), 'record', 'other-chat')[0]
    second = publish(batch, [first['job_id']])
    assert second['records'] == 2
    after = _candidates(s, query_terms('reviewedlabel'), 'record', 'input-chat')[0]
    assert len(after) == 2 and before[0][0] not in {r[0] for r in after}
    assert s.db.execute("SELECT count(*) FROM sg_search_keys WHERE kind='record'").fetchone()[0] == 2
    assert s.db.execute('SELECT count(*) FROM docs').fetchone()[0] == 0


def test_search_activation_rebuild_fails_atomically(tmp_path, monkeypatch):
    s, _ = fixture(tmp_path, [msg('alpha')])
    # An unknown owner must keep the original legacy index and activation off.
    with s.db:
        s.db.execute("INSERT INTO docs VALUES('alien','unknown','alpha')")
    with pytest.raises(ValueError, match='legacy_search_kind_unknown'):
        legacy.migrate(s, phase='search', apply=True)
    assert not source_text.unified(s)
    assert s.db.execute('SELECT count(*) FROM docs').fetchone()[0] == 2
    assert s.db.execute('SELECT count(*) FROM sg_search_keys').fetchone()[0] == 0
