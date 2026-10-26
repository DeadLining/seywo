import json
from pathlib import Path

import pytest

from sessiongraph import graph_v2 as graph, legacy_migration as migration, stages
from sessiongraph.core import Store
from test_effective_revisions import document
from test_graph_v2 import ContextExtractor, Judge
from test_memories import make_store, entries, FakeExtractor
from test_revision_ledger import publish


@pytest.fixture
def env(tmp_path):
    root, store, _ = make_store(tmp_path, [('assistant', 'runA success heldout seed42: pass.')])
    store.config['graph_v2'] = {'enabled': True}
    graph.setup(store)
    publish(store, entries(store)[0], 'old compound finding')
    with store.db:
        store.db.execute("UPDATE sg2_jobs SET state='complete'")
        store.db.execute('INSERT INTO sg2_cursors VALUES(?,?,?,?)',
                         (store.config['project_id'], 'chat-1', 999, 0))
    store.config['graph_v2']['revision_policy'] = {'enabled': True, 'mode': 'effective'}
    graph.setup(store)
    cfg = root / '.sessiongraph/config.json'
    raw = json.loads(cfg.read_text())
    raw['graph_v2'] = store.config['graph_v2']
    cfg.write_text(json.dumps(raw))
    yield store, cfg
    store.close()


def rows(store, table):
    return [tuple(r) for r in store.db.execute(f'SELECT * FROM {table} ORDER BY 1')]


def invoke(env, **kwargs):
    return migration.run(env[1], node_ids=['subject'], **kwargs)


def test_preview_is_readonly_and_no_provider_or_schema(env, monkeypatch):
    store, cfg = env
    monkeypatch.setattr('socket.create_connection', lambda *a, **k: pytest.fail('network'))
    readonly = Store(store.config, readonly=True)
    try:
        before = rows(readonly, 'sqlite_master')
        candidate = migration.plan(readonly, node_ids=['subject'])[0]
        assert candidate['eligible'] and candidate['selected_chars'] < 8000
        assert readonly.db.total_changes == 0
        assert before == rows(readonly, 'sqlite_master')
    finally:
        readonly.close()
    result = invoke(env)
    assert result['model_calls'] == 0 and not result['applied']
    assert result['candidates'][0]['migration'] is None
    assert not store.db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (migration.TABLE,)).fetchone()


def test_freeze_is_manual_only_deduplicated_and_does_not_reserve_live_ranges(env):
    store, cfg = env
    before = rows(store, 'sg2_cursors')
    first = invoke(env, apply=True)
    second = invoke(env, apply=True)
    assert first['model_calls'] == second['model_calls'] == 0
    assert first['candidates'][0]['job_id'] == second['candidates'][0]['job_id']
    assert store.db.execute('SELECT count(*) FROM sg_legacy_migrations').fetchone()[0] == 1
    assert not stages.pending(store, store.config['project_id'], 'chat-1')
    assert len(stages.pending(store, store.config['project_id'], 'chat-1', include_manual=True)) == 1
    result = graph.run_chat(store, FakeExtractor([]), 'chat-1', pending_only=True, jev=Judge())
    assert result['model_calls'] == 0 and result['graph_v2']['pending_jobs'] == 0
    assert not store.db.execute('SELECT * FROM sg2_claims').fetchall()
    assert rows(store, 'sg2_cursors') == before


def test_actual_graph_replay_retains_legacy_cursor_and_evidence_and_replay_is_free(env):
    store, cfg = env
    old = tuple(store.db.execute('SELECT * FROM work_memory WHERE id="subject"').fetchone())
    before = rows(store, 'sg2_cursors')
    extractor = ContextExtractor([document(entries(store)[0], 'pass')])
    result = invoke(env, apply=True, allow_network=True, extractor=extractor, judge=Judge())
    detail = result['candidates'][0]['migration']
    assert result['model_calls'] == 1 and len(extractor.calls) == 1
    assert detail['state'] == 'source_reprocessed' and not detail['equivalence_proven']
    assert len(detail['source_nodes']) == 1 and detail['source_nodes'] != ['subject']
    assert old == tuple(store.db.execute('SELECT * FROM work_memory WHERE id="subject"').fetchone())
    assert rows(store, 'sg2_cursors') == before
    assert store.db.execute('SELECT count(*) FROM sg_input_coverage').fetchone()[0] == 0
    assert store.db.execute('SELECT count(*) FROM sg_revision_items').fetchone()[0] == 1
    assert graph.node_details(store, 'subject')['legacy_migration'] == detail
    replay = invoke(env, apply=True, allow_network=True, extractor=ContextExtractor([]), judge=Judge())
    assert replay['model_calls'] == 0
    assert store.db.execute('SELECT count(*) FROM sg2_nodes').fetchone()[0] == 2
    assert rows(store, 'sg2_cursors') == before
    assert store.db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'


@pytest.mark.parametrize('field,value', [('text', 'source replaced'), ('role', 'user')])
def test_changed_source_or_attribution_cannot_replay(env, field, value):
    store, cfg = env
    invoke(env, apply=True)
    with store.db:
        store.db.execute(f'UPDATE entries SET {field}=?', (value,))
    result = invoke(env, apply=True, allow_network=True, extractor=ContextExtractor([]), judge=Judge())
    assert result['model_calls'] == 0
    assert result['candidates'][0]['reason'] == 'source_changed'


def test_selection_and_network_consent_are_explicit(env):
    with pytest.raises(ValueError, match='requires --apply'):
        invoke(env, allow_network=True)
    with pytest.raises(ValueError, match='belong to this project'):
        migration.run(env[1], node_ids=['foreign'])
    for limit in (0, 17, True):
        with pytest.raises(ValueError, match='bounded'):
            migration.run(env[1], limit=limit)


def test_frozen_descriptor_rejects_attribution_change_inside_actual_graph(env):
    store, cfg = env
    frozen = invoke(env, apply=True)['candidates'][0]['job_id']
    with store.db:
        store.db.execute("UPDATE entries SET role='user'")
    result = graph.run_chat(store, ContextExtractor([]), 'chat-1', pending_only=True,
                            job_ids=[frozen], respect_schedule=True, jev=Judge())
    assert result['model_calls'] == 0 and result['failure_codes'] == ['graph_validation']
    assert result['failed'][0]['failure']['detail'] == 'graph_source_changed'


def test_attribution_change_during_provider_call_cannot_publish(env):
    store, cfg = env
    class Changed(ContextExtractor):
        def __call__(self, batch, policy):
            output = super().__call__(batch, policy)
            with store.db:
                store.db.execute("UPDATE entries SET role='user'")
            return output
    result = invoke(env, apply=True, allow_network=True,
                    extractor=Changed([document(entries(store)[0], 'pass')]), judge=Judge())
    assert result['model_calls'] == 1
    assert result['runs'][0]['failure_codes'] == ['graph_validation']
    assert store.db.execute('SELECT count(*) FROM sg2_nodes').fetchone()[0] == 1


def test_manual_replay_does_not_prevent_completed_live_boundary_cleanup(env):
    from sessiongraph.continuation import completed_boundary
    store, cfg = env
    invoke(env, apply=True)
    job = store.db.execute("SELECT id FROM sg2_jobs WHERE state='complete' LIMIT 1").fetchone()[0]
    with store.db:
        store.db.execute("INSERT INTO sg2_attempts VALUES('live-attempt',?,'part','extract','complete',0,1,NULL,NULL,'live-run')", (job,))
    target = store.db.execute('SELECT max(rowid) FROM entries').fetchone()[0]
    assert completed_boundary(store,dict(state='blocked',target=target,error_code='provider_timeout',
                                         run_id='live-run',chat='chat-1'))


def test_preview_missing_metadata_does_not_repair_or_scan_source_files(env):
    store, cfg = env
    with store.db:
        store.db.execute('DELETE FROM sg_source_locations')
    readonly = Store(store.config, readonly=True)
    try:
        item = migration.plan(readonly,node_ids=['subject'])[0]
        assert not item['eligible'] and item['reason'] == 'source_metadata_missing'
        assert readonly.db.total_changes == 0
    finally:
        readonly.close()


def test_compound_candidates_are_not_fed_back_as_migration_identities(env, monkeypatch):
    store, _ = env
    compound = dict(id='subject', type='Claim', title='old summary', content='many facts')
    atomic = document(entries(store)[0], 'pass')['nodes'][0]
    execution = dict(id='old-execution', type='Execution', title='old summary', content='many facts')
    monkeypatch.setattr(graph, 'candidates', lambda *args: [compound, atomic, execution])
    result = invoke(env, apply=True)
    job = result['candidates'][0]['job_id']
    offered = json.loads(store.db.execute('SELECT candidates_json FROM sg2_jobs WHERE id=?', (job,)).fetchone()[0])
    assert offered == [atomic]


def test_unatomic_migration_output_stays_unresolved_without_touching_legacy(env):
    store, _ = env
    old = tuple(store.db.execute('SELECT * FROM work_memory WHERE id="subject"').fetchone())
    output = document(entries(store)[0], 'pass')
    output['nodes'][0]['assertion'] = []
    result = invoke(env, apply=True, allow_network=True,
                    extractor=ContextExtractor([output]), judge=Judge())
    assert result['model_calls'] == 1
    job = result['candidates'][0]['migration']['job_id']
    assert store.db.execute('SELECT reason FROM sg2_unresolved WHERE job=?', (job,)).fetchone()[0] == 'atomic_assertion_required'
    assert tuple(store.db.execute('SELECT * FROM work_memory WHERE id="subject"').fetchone()) == old
    assert result['candidates'][0]['migration']['source_nodes'] == []


def test_publication_rejects_cached_unatomic_migration_binding(env):
    store, _ = env
    job_id = invoke(env, apply=True)['candidates'][0]['job_id']
    job = dict(store.db.execute('SELECT * FROM sg2_jobs WHERE id=?', (job_id,)).fetchone())
    output = document(entries(store)[0], 'pass')
    node = output['nodes'][0]
    node['assertion'] = []
    with pytest.raises(ValueError, match='atomic_assertion_required'):
        graph._commit(store, job, output, [], {node['id']: 'subject'}, [], [], [], graph._stats(), [0, 0])


def test_publication_rejects_atomic_migration_bound_to_old_compound(env):
    store, _ = env
    job_id = invoke(env, apply=True)['candidates'][0]['job_id']
    job = dict(store.db.execute('SELECT * FROM sg2_jobs WHERE id=?', (job_id,)).fetchone())
    output = document(entries(store)[0], 'pass')
    node = output['nodes'][0]
    with pytest.raises(ValueError, match='legacy_binding_forbidden'):
        graph._commit(store, job, output, [], {node['id']: 'subject'}, [], [], [], graph._stats(), [0, 0])


def test_apply_takes_project_locks_before_opening_writable_store(env, monkeypatch):
    from contextlib import contextmanager
    original_lock, original_store = migration.locked, migration.Store
    held = set()

    @contextmanager
    def tracked(path, **kwargs):
        with original_lock(path, **kwargs):
            held.add(path.name)
            try:
                yield
            finally:
                held.remove(path.name)

    def opened(config, readonly=False):
        if not readonly:
            assert {'stage-activity.lock', 'graph-v2.lock'} <= held
        return original_store(config, readonly=readonly)

    monkeypatch.setattr(migration, 'locked', tracked)
    monkeypatch.setattr(migration, 'Store', opened)
    assert invoke(env, apply=True)['applied']


def test_contract_refresh_is_explicit_once_and_preserves_job_history(env):
    store, _ = env
    candidate = document(entries(store)[0], 'pass')
    original = invoke(env, apply=True, allow_network=True, extractor=ContextExtractor([candidate]), judge=Judge())
    job = original['candidates'][0]['job_id']
    row = store.db.execute('SELECT input_json,stats_json FROM sg2_jobs WHERE id=?', (job,)).fetchone()
    desc = json.loads(row['input_json'])
    desc.pop('atomic_source_revision')  # simulate a completed older contract
    with store.db:
        store.db.execute('UPDATE sg2_jobs SET input_json=? WHERE id=?', (json.dumps(desc), job))
    attempts = rows(store, 'sg2_attempts')
    evidence = rows(store, 'sg2_evidence')
    cursors = rows(store, 'sg2_cursors')
    # Normal replay does not upgrade or spend calls.
    assert invoke(env, apply=True, allow_network=True, extractor=ContextExtractor([]), judge=Judge())['model_calls'] == 0
    frozen = invoke(env, apply=True, refresh_contract=True)
    assert frozen['model_calls'] == 0 and frozen['candidates'][0]['job_id'] == job
    assert store.db.execute('SELECT state FROM sg2_jobs WHERE id=?', (job,)).fetchone()[0] == 'pending'
    assert rows(store, 'sg2_attempts') == attempts and rows(store, 'sg2_evidence') == evidence
    assert store.db.execute('SELECT stats_json FROM sg2_jobs WHERE id=?', (job,)).fetchone()[0] == row['stats_json']
    result = invoke(env, apply=True, refresh_contract=True, allow_network=True,
                    extractor=ContextExtractor([candidate]), judge=Judge())
    assert result['model_calls'] == 1
    assert result['candidates'][0]['migration']['state'] == 'source_reprocessed'
    assert {r[0] for r in attempts} <= {r[0] for r in rows(store, 'sg2_attempts')}
    assert rows(store, 'sg2_cursors') == cursors
    assert invoke(env, apply=True, refresh_contract=True, allow_network=True,
                  extractor=ContextExtractor([]), judge=Judge())['model_calls'] == 0


def test_refresh_cannot_be_implicit_or_bulk(env):
    with pytest.raises(ValueError, match='explicit'):
        invoke(env, refresh_contract=True)
    with pytest.raises(ValueError, match='explicit'):
        migration.run(env[1], apply=True, refresh_contract=True)


def test_deictic_subject_remains_pending_even_when_literally_anchored(env):
    store, _ = env
    output = document(entries(store)[0], 'pass')
    # The original contains 'runA'; make the same source-backed but unsafe key
    # a pronoun to exercise the scope gate without any fuzzy matching.
    with store.db:
        store.db.execute("UPDATE entries SET text='this run success heldout seed42: pass.'")
    output['nodes'][0]['evidence'][0]['quote'] = entries(store)[0]['text']
    output['nodes'][0]['assertion'][0]['subject'] = 'this run'
    stats = graph._stats()
    bindings, pending = graph._identities(store, output, [], Judge(), stats, require_atomic=True)
    assert not bindings
    assert pending[0][1] == 'atomic_scope_unresolved'
    assert not graph._identity_keys(output['nodes'][0])
