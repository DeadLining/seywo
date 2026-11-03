import copy
import hashlib
import json
from pathlib import Path

import pytest

from sessiongraph.classifier_shadow import run
from sessiongraph.core import Store
from sessiongraph.failures import Failure
from test_memories import make_store
from test_single_token_judge import QUESTION, response


@pytest.fixture
def fixture(tmp_path):
    _, store, _ = make_store(tmp_path, [('user', 'Keep the old result; this is not approval.'), ('assistant', 'Only a proposal.')])
    store.config['provider'].update(protocol='responses', endpoint='http://127.0.0.1:4317/openai/v1/responses', model='fixture', auth='none')
    from sessiongraph.source_text import hydrate
    row = hydrate(store, store.db.execute('SELECT id,text FROM entries ORDER BY line').fetchone())
    item = dict(id='q1', question=copy.deepcopy(QUESTION), state={'candidate': 'This is approval.'},
                evidence=[dict(entry_id=row['id'], source_hash=hashlib.sha256(row['text'].encode()).hexdigest(), start=0, end=len(row['text']))])
    manifest = dict(project_id=store.config['project_id'], run_id='shadow1',
                    budget=dict(max_requests=6, max_input_bytes=128 * 1024, max_seconds=30), questions=[item])
    yield store, manifest
    store.close()


def fake_network(monkeypatch, outputs=None):
    seen = []
    outputs = list(outputs or [])
    def read(client, req, handlers):
        seen.append((client.timeout, json.loads(req.data)))
        output = outputs.pop(0) if outputs else response()
        if isinstance(output, Exception):
            raise output
        return json.dumps(output).encode()
    monkeypatch.setattr('sessiongraph.single_token_judge.read_response', read)
    return seen


def test_preview_is_readonly_and_never_loads_credentials(fixture, monkeypatch):
    store, manifest = fixture
    monkeypatch.setattr('sessiongraph.single_token_judge.create_extractor', lambda *a: pytest.fail('credentials'))
    before = store.db.total_changes
    result = run(store, manifest)
    assert result['state'] == 'preview' and result['model_calls'] == 0
    assert store.db.total_changes == before
    assert not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='sg_classifier_runs'").fetchone()
    assert not list(Path(store.config['database']).parent.glob('classifier-*.lock'))
    readonly = Store(store.config, readonly=True)
    try:
        assert run(readonly, manifest)['state'] == 'preview'
        with pytest.raises(Failure, match='readonly'):
            run(readonly, manifest, allow_network=True)
    finally:
        readonly.close()


def test_checkpoints_survive_yield_without_new_calls_or_facts(fixture, monkeypatch):
    store, manifest = fixture
    seen = fake_network(monkeypatch)
    manifest['questions'] = [{**manifest['questions'][0], 'id': f'q{i}'} for i in range(4)]
    protected = {table: store.db.execute(f'SELECT * FROM {table}').fetchall() for table in ('entries', 'work_memory', 'work_memory_relations')}
    first = run(store, manifest, allow_network=True)
    assert first['state'] == 'yielded' and first['model_calls'] == 3
    second = run(store, manifest, allow_network=True)
    assert second['state'] == 'complete' and second['model_calls'] == 1
    assert sum(v['checkpoint_replay'] for v in second['results'].values()) == 3
    store.db.close()
    store.db = Store(store.config).db
    third = run(store, manifest, allow_network=True)
    assert third['model_calls'] == 0 and third['cumulative']['requests'] == 4
    assert len(seen) == 4 and all(body['max_output_tokens'] == 1 for _, body in seen)
    for table, rows in protected.items():
        assert store.db.execute(f'SELECT * FROM {table}').fetchall() == rows


@pytest.mark.parametrize('field,value', [('max_requests', 1), ('max_input_bytes', 1), ('max_seconds', 1)])
def test_cumulative_budgets_cannot_reset_on_resume(fixture, monkeypatch, field, value):
    store, manifest = fixture
    seen = fake_network(monkeypatch)
    manifest['questions'] *= 2
    manifest['questions'][1] = {**manifest['questions'][1], 'id': 'q2'}
    manifest['budget'][field] = value
    if field == 'max_seconds':
        ticks = iter([100., 102.])
        monkeypatch.setattr('sessiongraph.classifier_shadow.time.monotonic', lambda: next(ticks))
    first = run(store, manifest, allow_network=True)
    assert first['state'] == 'budget_exhausted'
    before = len(seen)
    second = run(store, manifest, allow_network=True)
    assert second['state'] == 'budget_exhausted' and len(seen) == before
    if field != 'max_input_bytes':
        assert before == 1
    else:
        assert before == 0


def test_manifest_or_route_change_cannot_reuse_checkpoints(fixture, monkeypatch):
    store, manifest = fixture
    seen = fake_network(monkeypatch)
    run(store, manifest, allow_network=True)
    changed = copy.deepcopy(manifest)
    changed['budget']['max_requests'] += 1
    with pytest.raises(Failure, match='manifest_changed'):
        run(store, changed, allow_network=True)
    store.config['provider']['model'] = 'different-backend'
    with pytest.raises(Failure, match='manifest_changed'):
        run(store, manifest, allow_network=True)
    assert len(seen) == 1


@pytest.mark.parametrize('mutation,code', [
    (lambda m: m.update(project_id='synthetic-foreign-project'), 'project_or_manifest'),
    (lambda m: m.update(run_id='../unsafe'), 'run_id'),
    (lambda m: m['questions'][0]['evidence'][0].update(source_hash='stale'), 'source_changed'),
    (lambda m: m['questions'][0]['evidence'][0].update(entry_id='foreign-entry'), 'source_changed'),
    (lambda m: m['questions'][0]['evidence'][0].update(end=100000), 'source_range'),
    (lambda m: m['questions'][0]['evidence'][0].update(start=True), 'source_reference'),
    (lambda m: m['questions'].append(m['questions'][0]), 'question_id'),
    (lambda m: m['questions'][0].update(evidence=[]), 'evidence'),
    (lambda m: m['budget'].update(max_requests=True), 'budget'),
    (lambda m: m['budget'].update(max_seconds=float('nan')), 'budget'),
    (lambda m: m['questions'][0]['state'].update(api_key='secret'), 'sensitive_input'),
])
def test_invalid_manifest_never_calls_provider_or_writes(fixture, monkeypatch, mutation, code):
    store, manifest = fixture
    seen = fake_network(monkeypatch)
    before = store.db.total_changes
    mutation(manifest)
    with pytest.raises(Failure, match='classifier_shadow_' + code):
        run(store, manifest, allow_network=True)
    assert not seen and store.db.total_changes == before


def test_failed_deterministic_route_stays_blocked_without_retries(fixture, monkeypatch):
    store, manifest = fixture
    seen = fake_network(monkeypatch, [response(text='not a label')])
    first = run(store, manifest, allow_network=True)
    assert first['state'] == 'blocked' and first['failure']['code'] == 'single_token_label_invalid'
    second = run(store, manifest, allow_network=True)
    assert second['state'] == 'blocked' and second['model_calls'] == 0
    assert len(seen) == 1


def test_explicit_retry_preserves_success_and_charges_failure(fixture, monkeypatch):
    store, manifest = fixture
    seen = fake_network(monkeypatch, [response(), TimeoutError(), response()])
    manifest['questions'].append({**manifest['questions'][0], 'id': 'q2'})
    first = run(store, manifest, allow_network=True)
    assert first['state'] == 'retry_wait' and first['model_calls'] == 2
    second = run(store, manifest, allow_network=True)
    assert second['state'] == 'complete' and second['model_calls'] == 1
    assert second['results']['q1']['checkpoint_replay']
    assert second['cumulative']['requests'] == 3 and len(seen) == 3


def test_crash_keeps_reservation_and_releases_run_lock(fixture, monkeypatch):
    store, manifest = fixture
    class Crash(Exception):
        pass
    seen = fake_network(monkeypatch, [Crash()])
    with pytest.raises(Crash):
        run(store, manifest, allow_network=True)
    spent = store.db.execute('SELECT * FROM sg_classifier_runs').fetchone()
    assert spent['requests'] == 1 and spent['seconds'] == 30
    result = run(store, manifest, allow_network=True)
    assert result['state'] == 'budget_exhausted' and len(seen) == 1


def test_run_lock_does_not_hold_database_transaction(fixture, monkeypatch):
    store, manifest = fixture
    original = fake_network(monkeypatch)
    def inspect(client, req, handlers):
        assert not store.db.in_transaction
        with pytest.raises(Failure, match='run_active'):
            run(store, manifest, allow_network=True)
        return json.dumps(response()).encode()
    monkeypatch.setattr('sessiongraph.single_token_judge.read_response', inspect)
    assert run(store, manifest, allow_network=True)['state'] == 'complete'
    assert not original


def test_source_change_invalidates_historical_checkpoint(fixture, monkeypatch):
    store, manifest = fixture
    seen = fake_network(monkeypatch)
    run(store, manifest, allow_network=True)
    with store.db:
        store.db.execute("UPDATE entries SET text='changed source' WHERE id=?", (manifest['questions'][0]['evidence'][0]['entry_id'],))
    with pytest.raises(Failure, match='source_changed'):
        run(store, manifest, allow_network=True)
    assert len(seen) == 1


def test_cli_preview_uses_explicit_source_manifest(fixture, tmp_path, capsys):
    from sessiongraph.cli import main
    store, manifest = fixture
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest))
    # Runtime endpoint/model overrides belong to this fixture's config only.
    config_path = Path(store.config['database']).parent / 'config.json'
    config = json.loads(config_path.read_text())
    config['provider'] = store.config['provider']
    config_path.write_text(json.dumps(config))
    assert main(['--config', str(config_path), 'judge-shadow', str(path)]) == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'preview'


def test_concurrent_source_rewrite_cannot_checkpoint_a_valid_label(fixture, monkeypatch):
    store, manifest = fixture
    def rewrite(client, req, handlers):
        with store.db:
            store.db.execute("UPDATE entries SET role='assistant' WHERE id=?", (manifest['questions'][0]['evidence'][0]['entry_id'],))
        return json.dumps(response()).encode()
    monkeypatch.setattr('sessiongraph.single_token_judge.read_response', rewrite)
    result = run(store, manifest, allow_network=True)
    assert result['state'] == 'blocked' and result['model_calls'] == 1
    saved = store.db.execute('SELECT state,result_json FROM sg_classifier_results').fetchone()
    assert saved['state'] == 'failed' and saved['result_json'] is None


def test_no_credentials_is_not_counted_as_a_transmitted_request(fixture, monkeypatch):
    store, manifest = fixture
    def missing(*a):
        raise ValueError('missing credential')
    monkeypatch.setattr('sessiongraph.single_token_judge.create_extractor', missing)
    result = run(store, manifest, allow_network=True)
    assert result['state'] == 'blocked' and result['model_calls'] == 0
    assert result['cumulative']['requests'] == 1  # Admission reservation, not actual transport.
