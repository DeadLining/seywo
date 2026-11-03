from sessiongraph.source_text import hydrate
import hashlib
import json
import socket

import pytest
from sessiongraph.core import Store
from sessiongraph.graph_v2 import run_chat
from sessiongraph.native_bridge import read, catalog
from sessiongraph.summary_materials import validate_query
from test_memories import make_store, entries, FakeExtractor
from test_graph_v2 import output, Judge


def fixture(tmp_path):
    root, store, _ = make_store(tmp_path, [('assistant', '训练 A 得到模型 B')], timestamp='2026-10-23T01:00:00Z')
    batch = entries(store)
    result = output(batch)
    for n in result['nodes']:
        n['context_kind'] = 'work'
    run_chat(store, FakeExtractor([result]), 'chat-1', jev=Judge())
    cfg = store.config
    store.close()
    registry = tmp_path / 'registry.json'
    registry.write_text(json.dumps({'projects': [{'root': str(root)}]}))
    project = catalog(registry)['projects'][0]['id']
    query = {'operation': 'summary_candidates', 'project': project, 'materials': {'start': '2026-10-22T16:00:00Z', 'end': '2026-10-23T16:00:00Z'}}
    return cfg, registry, query


def test_read_is_source_backed_readonly_no_network(tmp_path, monkeypatch):
    cfg, registry, query = fixture(tmp_path)
    from pathlib import Path
    before = hashlib.sha256(Path(cfg['database']).read_bytes()).hexdigest()
    monkeypatch.setattr(socket, 'socket', lambda *a, **k: pytest.fail('network called'))
    result = read(registry, query)
    assert result['model_calls'] == 0
    assert result['assertions'] and result['relations']
    assert result['assertions'][0]['quote'] == '训练 A 得到模型 B'
    assert before == hashlib.sha256(Path(cfg['database']).read_bytes()).hexdigest()


def test_historical_assertions_not_latest_memory(tmp_path):
    cfg, registry, query = fixture(tmp_path)
    store = Store(cfg)
    store.db.execute("UPDATE work_memory SET title='later changed',status='blocked'")
    store.db.commit(); store.close()
    result = read(registry, query)
    assert all(a['title'] != 'later changed' and a['status'] == 'completed' for a in result['assertions'])
    query['materials']['start'] = '2026-10-23T16:00:00Z'
    query['materials']['end'] = '2026-10-24T16:00:00Z'
    assert read(registry, query)['assertions'] == []


def test_revision_mismatch_and_invalid_quotes(tmp_path):
    cfg, registry, query = fixture(tmp_path)
    first = read(registry, query)
    store = Store(cfg)
    store.db.execute("UPDATE sg2_evidence SET quote='not in source'")
    store.db.commit(); store.close()
    query['materials']['revision'] = first['revision']
    with pytest.raises(ValueError, match='revision_changed'):
        read(registry, query)
    query['materials'].pop('revision')
    result = read(registry, query)
    assert not result['assertions']
    assert result['coverage']['invalid_evidence'] > 0
    assert result['coverage']['state'] == 'partial'
    assert result['coverage']['read_state'] == 'partial'


def test_sparse_graph_coverage_does_not_imply_incomplete_read(tmp_path):
    cfg, registry, query = fixture(tmp_path)
    store = Store(cfg)
    seed = hydrate(store, store.db.execute('SELECT * FROM entries LIMIT 1').fetchone())
    store.db.execute('INSERT INTO entries VALUES(?,?,?,?,?,?,?,?)',
        ('unreferenced', seed['session_id'], seed['line'] + 1000, seed['timestamp'],
         seed['kind'], seed['role'], '一条没有图谱引用的原文', None))
    store.db.commit(); store.close()
    result = read(registry, query)
    assert result['coverage']['state'] == 'partial'
    assert result['coverage']['read_state'] == 'complete'
    assert result['coverage']['source_entries'] > result['coverage']['covered_entries']
    assert result['assertions']


@pytest.mark.parametrize('patch', [{'start': '2026-10-23'}, {'offset': -1}, {'offset': 200}, {'check_only': 'yes'}, {'end': '2027-10-24T00:00:00Z'}])
def test_rejects_invalid_ranges(patch):
    with pytest.raises(ValueError):
        validate_query({'start': '2026-10-22T16:00:00Z', 'end': '2026-10-23T16:00:00Z', **patch})


def test_pagination_is_revision_bound_and_no_duplicate_assertions(tmp_path, monkeypatch):
    _, registry, query = fixture(tmp_path)
    monkeypatch.setattr('sessiongraph.summary_materials.PAGE_SIZE', 1)
    first = read(registry, query)
    assert first['next_offset'] == 1
    query['materials'].update(offset=1, revision=first['revision'])
    second = read(registry, query)
    assert first['revision'] == second['revision']
    assert {a['id'] for a in first['assertions']}.isdisjoint(a['id'] for a in second['assertions'])
    assert second['next_offset'] is None


def test_proposed_relations_do_not_merge_work(tmp_path):
    cfg, registry, query = fixture(tmp_path)
    store = Store(cfg)
    store.db.execute("UPDATE sg2_relations SET mode='proposed'")
    store.db.commit(); store.close()
    result = read(registry, query)
    assert result['assertions']
    assert not result['relations']


def test_historical_parent_is_context_not_new_work(tmp_path):
    cfg, registry, query = fixture(tmp_path)
    store = Store(cfg)
    original = hydrate(store, store.db.execute('SELECT * FROM entries LIMIT 1').fetchone())
    store.db.execute('INSERT INTO entries VALUES(?,?,?,?,?,?,?,?)',
        ('historical', original['session_id'], original['line'] + 1000,
         '2026-10-20T00:00:00Z', original['kind'], original['role'], original['text'], None))
    node = store.db.execute("SELECT id FROM sg2_nodes WHERE type='Execution'").fetchone()[0]
    store.db.execute("UPDATE sg2_evidence SET entry_id='historical' WHERE node=?", (node,))
    store.db.commit(); store.close()
    result = read(registry, query)
    parent = next(a for a in result['assertions'] if a['node_id'] == node)
    assert parent['in_range'] is False
    assert any(a['in_range'] for a in result['assertions'])


def test_packaged_resources_support_material_query(tmp_path):
    """Exercise the release resource map without falling back to repository imports."""
    import os
    from pathlib import Path
    import shutil
    import subprocess
    import sys

    _, registry, query = fixture(tmp_path)
    repo = Path(__file__).resolve().parents[2]
    tauri = repo / 'src-tauri'
    resources = json.loads((tauri / 'tauri.conf.json').read_text())['bundle']['resources']
    bundle = tmp_path / 'bundle'
    for source, destination in resources.items():
        if destination.startswith('sessiongraph/'):
            target = bundle / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(tauri / source, target)
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    shutil.copyfile(registry, runtime / 'projects.json')
    env = {**os.environ, 'SESSIONGRAPH_RUNTIME_HOME': str(runtime), 'PYTHONDONTWRITEBYTECODE': '1'}
    env.pop('PYTHONPATH', None)
    result = subprocess.run(
        [sys.executable, '-B', str(bundle / 'sessiongraph/entrypoint.py'), 'native-read'],
        input=json.dumps(query), text=True, capture_output=True, cwd=tmp_path, env=env, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload['model_calls'] == 0
    assert payload['assertions'][0]['quote'] == '训练 A 得到模型 B'
    query['materials']['revision'] = '0' * 64
    changed = subprocess.run(
        [sys.executable, '-B', str(bundle / 'sessiongraph/entrypoint.py'), 'native-read'],
        input=json.dumps(query), text=True, capture_output=True, cwd=tmp_path, env=env, timeout=10,
    )
    assert changed.returncode == 1
    assert json.loads(changed.stdout) == {'error': 'material_revision_changed', 'model_calls': 0}


def test_coverage_without_entry_index_has_bounded_work(tmp_path):
    """Legacy schema coverage must not scan all evidence once per message."""
    from sessiongraph.summary_materials import candidates
    cfg, _, query = fixture(tmp_path)
    store = Store(cfg)
    seed = hydrate(store, store.db.execute('SELECT * FROM entries LIMIT 1').fetchone())
    ev = dict(store.db.execute('SELECT * FROM sg2_evidence LIMIT 1').fetchone())
    store.db.executemany('INSERT INTO entries VALUES (?,?,?,?,?,?,?,?)', [
        (f'bulk-{i}', seed['session_id'], i + 100, seed['timestamp'], seed['kind'], seed['role'], seed['text'], None)
        for i in range(4000)
    ])
    # Two assertions per covered message: coverage counts messages, not assertions.
    store.db.executemany('INSERT INTO sg2_evidence VALUES (?,?,?,?,?,?,?)', [
        (f'bulk-evidence-{i}', ev['node'], f'bulk-{i // 2}', ev['quote'], ev['chat'], ev['assertion_json'], ev['job'])
        for i in range(4000)
    ])
    store.db.execute('DROP INDEX sg2_evidence_source')
    store.db.execute('DROP INDEX entries_materials_window')
    store.db.commit()
    store.db.close()
    reader = Store(cfg, readonly=True)
    steps = 0
    def budget():
        nonlocal steps
        steps += 1000
        return int(steps > 1_000_000)
    reader.db.set_progress_handler(budget, 1000)
    try:
        result = candidates(reader, query['materials'])
        assert result['coverage']['source_entries'] == 4001
        assert result['coverage']['covered_entries'] == 2001
        assert result['coverage']['state'] == 'partial'
        assert len(result['assertions']) == 200
        assert result['next_offset'] == 200
    finally:
        reader.close()


def test_page_does_not_load_original_text_before_limit(tmp_path):
    """Reverse chronology forces the old wide sorter to touch every large source."""
    from datetime import datetime, timedelta, timezone
    from sessiongraph.summary_materials import candidates
    cfg, _, query = fixture(tmp_path)
    store = Store(cfg)
    seed = hydrate(store, store.db.execute('SELECT * FROM entries LIMIT 1').fetchone())
    ev = dict(store.db.execute('SELECT * FROM sg2_evidence LIMIT 1').fetchone())
    store.db.execute('DELETE FROM sg2_relation_evidence')
    store.db.execute('DELETE FROM sg2_relations')
    origin = datetime(2026, 10, 23, 4, tzinfo=timezone.utc)
    for i in range(600):
        store.db.execute('INSERT INTO entries VALUES (?,?,?,?,?,?,?,?)',
            (f'wide-{i:04}', seed['session_id'], i + 100, (origin - timedelta(seconds=i)).isoformat(),
             seed['kind'], seed['role'], seed['text'] + 'x' * 8192, None))
        store.db.execute('INSERT INTO sg2_evidence VALUES (?,?,?,?,?,?,?)',
            (f'wide-evidence-{i:04}', ev['node'], f'wide-{i:04}', ev['quote'], ev['chat'], ev['assertion_json'], ev['job']))
    store.db.commit()
    fetched = []
    def touch(text):
        fetched.append(len(text))
        return text
    store.db.create_function('material_payload', 1, touch)
    # Instrument source hydration without changing any stored evidence or text.
    store.db.execute('CREATE TEMP VIEW entries AS SELECT id,session_id,line,timestamp,kind,role,material_payload(text) text,call_id FROM main.entries')
    try:
        result = candidates(store, query['materials'])
        assert len(result['assertions']) == 200
        assert len(fetched) <= 200, 'unselected source messages were loaded into the sort'
        assert result['next_offset'] == 200
        first_ids = [a['id'] for a in result['assertions']]
        expected = [r[0] for r in store.db.execute('SELECT x.id FROM sg2_evidence x JOIN main.entries e ON e.id=x.entry_id ORDER BY e.timestamp,x.id LIMIT 200')]
        assert first_ids == expected
        fetched.clear()
        query['materials'].update(offset=200, revision=result['revision'])
        second = candidates(store, query['materials'])
        assert len(fetched) <= 200
        assert set(first_ids).isdisjoint(a['id'] for a in second['assertions'])
    finally:
        store.db.close()


def test_empty_window_does_not_walk_historical_evidence(tmp_path):
    """Many assertions about one old message must not cost one source fetch each."""
    from sessiongraph.summary_materials import candidates
    cfg, _, query = fixture(tmp_path)
    store = Store(cfg)
    evidence = dict(store.db.execute('SELECT * FROM sg2_evidence LIMIT 1').fetchone())
    store.db.executemany('INSERT INTO sg2_evidence VALUES (?,?,?,?,?,?,?)', [
        (f'historical-{i}', evidence['node'], evidence['entry_id'], evidence['quote'],
         evidence['chat'], evidence['assertion_json'], evidence['job']) for i in range(5000)
    ])
    store.db.commit(); store.close()
    query['materials'].update(start='2026-10-23T16:00:00Z', end='2026-10-24T16:00:00Z')
    reader = Store(cfg, readonly=True)
    steps = 0
    def budget():
        nonlocal steps
        steps += 1000
        return int(steps > 25000)
    reader.db.set_progress_handler(budget, 1000)
    try:
        result = candidates(reader, query['materials'])
        assert result['assertions'] == []
        assert result['relations'] == []
        assert result['next_offset'] is None
        assert result['coverage']['source_entries'] == 0
        assert result['coverage']['read_state'] == 'complete'
    finally:
        reader.close()


@pytest.mark.parametrize('legacy', [False, True])
def test_range_first_preserves_offsets_boundaries_order_and_coverage(tmp_path, monkeypatch, legacy):
    """Do not replace instant comparison with lexicographic timestamp filtering."""
    cfg, registry, query = fixture(tmp_path)
    store = Store(cfg)
    seed = hydrate(store, store.db.execute('SELECT * FROM entries LIMIT 1').fetchone())
    ev = dict(store.db.execute('SELECT * FROM sg2_evidence LIMIT 1').fetchone())
    store.db.execute('DELETE FROM sg2_relation_evidence')
    store.db.execute('DELETE FROM sg2_relations')
    stamps = [
        '2026-10-23T00:00:00+08:00',  # inclusive start
        '2026-10-22T23:59:59.999+08:00',  # before start
        '2026-10-23T23:59:59.999+08:00',
        '2026-10-24T00:00:00+08:00',  # exclusive end
        '2026-10-22T12:00:00-04:00',  # same instant as inclusive start
        '2026-10-23T01:00:00Z',  # tie with fixture: stable evidence ID order
        'not-a-date',
    ]
    for i, stamp in enumerate(stamps):
        store.db.execute('INSERT INTO entries VALUES (?,?,?,?,?,?,?,?)',
            (f'dated-{i}', seed['session_id'], 100+i, stamp, seed['kind'], seed['role'], seed['text'], None))
        store.db.execute('INSERT INTO sg2_evidence VALUES (?,?,?,?,?,?,?)',
            (f'dated-evidence-{i}', ev['node'], f'dated-{i}', ev['quote'], ev['chat'], ev['assertion_json'], ev['job']))
    # Duplicate evidence counts as two assertions but one covered source.
    store.db.execute('INSERT INTO sg2_evidence VALUES (?,?,?,?,?,?,?)',
        ('duplicate', ev['node'], 'dated-0', ev['quote'], ev['chat'], ev['assertion_json'], ev['job']))
    window = 'julianday(e.timestamp)>=julianday(?) AND julianday(e.timestamp)<julianday(?)'
    params = (query['materials']['start'], query['materials']['end'], cfg['project_id'])
    expected = [r[0] for r in store.db.execute(f'''SELECT x.id FROM sg2_evidence x
        JOIN entries e ON e.id=x.entry_id JOIN sg2_nodes n ON n.id=x.node
        WHERE {window} AND n.project=? ORDER BY e.timestamp,x.id''', params)]
    if legacy:
        store.db.execute('DROP INDEX entries_materials_window')
        store.db.execute('DROP INDEX sg2_evidence_source')
    store.db.commit(); store.close()
    monkeypatch.setattr('sessiongraph.summary_materials.PAGE_SIZE', 2)
    actual = []
    while True:
        result = read(registry, query)
        actual.extend(a['id'] for a in result['assertions'])
        assert result['coverage']['source_entries'] == 5
        assert result['coverage']['covered_entries'] == 5
        if result['next_offset'] is None:
            break
        query['materials'].update(offset=result['next_offset'], revision=result['revision'])
    assert actual == expected
    if legacy:
        reader = Store(cfg, readonly=True)
        try:
            names = {r[0] for r in reader.db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            assert 'entries_materials_window' not in names
            assert 'sg2_evidence_source' not in names
        finally:
            reader.close()


def test_writer_upgrades_covering_indexes_without_changing_facts(tmp_path):
    from sessiongraph.graph_v2 import setup
    cfg, _, _ = fixture(tmp_path)
    store = Store(cfg)
    tables = ('entries', 'sg2_nodes', 'sg2_evidence', 'sg2_relations', 'sg2_relation_evidence')
    before = {table: [tuple(r) for r in store.db.execute(f'SELECT * FROM {table} ORDER BY id')] for table in tables}
    store.db.execute('DROP INDEX entries_materials_window')
    store.db.execute('DROP INDEX sg2_evidence_source')
    store.db.commit(); store.close()
    for _ in range(2):
        store = Store(cfg)
        setup(store)
        after = {table: [tuple(r) for r in store.db.execute(f'SELECT * FROM {table} ORDER BY id')] for table in tables}
        assert before == after
        names = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {'entries_materials_window', 'sg2_evidence_source'} <= names
        assert [r[0] for r in store.db.execute('PRAGMA quick_check')] == ['ok']
        store.close()


def test_date_and_evidence_lookups_use_covering_indexes(tmp_path):
    from sessiongraph.summary_materials import candidates
    cfg, _, query = fixture(tmp_path)
    reader = Store(cfg, readonly=True)
    statements = []
    reader.db.set_trace_callback(statements.append)
    try:
        candidates(reader, query['materials'])
        sql = next(s for s in statements if s.startswith('WITH recent AS MATERIALIZED'))
        plan = [r[3] for r in reader.db.execute('EXPLAIN QUERY PLAN ' + sql)]
        assert any('COVERING INDEX entries_materials_window' in step for step in plan), plan
        assert any('SEARCH x USING COVERING INDEX sg2_evidence_source' in step for step in plan), plan
        assert not any(step.startswith('SCAN x') for step in plan), plan
    finally:
        reader.close()
