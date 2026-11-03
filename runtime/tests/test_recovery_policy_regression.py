"""Project-gated recovery: real graph state transitions, no provider network."""
import json
import os
from pathlib import Path

import pytest

from sessiongraph import graph_v2 as g
from sessiongraph.auto_update import run_update
from sessiongraph.extraction import ProviderError
from test_memories import make_store, entries, FakeExtractor
from test_graph_v2 import Judge, output


@pytest.fixture(autouse=True)
def automatic_registration(tmp_path, monkeypatch):
    home = tmp_path / 'codex-registration'
    home.mkdir()
    (home / 'sessiongraph.json').write_text(json.dumps({'enabled':True}))
    monkeypatch.setenv('CODEX_HOME', str(home))
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join([str(Path(g.__file__).resolve().parents[1]),
        str(Path(__file__).resolve().parent), os.environ.get('PYTHONPATH','')]))


def enable(store):
    store.config.setdefault('graph_v2', {})['recovery_policy'] = {'enabled': True}


class TimeoutJudge(Judge):
    def evaluate(self, state, questions):
        self.calls += 1
        raise ProviderError('timed out', 'provider_timeout')


class TimeoutExtract:
    def __call__(self, batch, policy):
        raise ProviderError('timed out', 'provider_timeout')


def control(store):
    return dict(store.db.execute('SELECT * FROM sg2_control').fetchone())


@pytest.mark.parametrize('active', [True, False])
def test_extraction_failures_do_not_exhaust_first_relation_attempt(tmp_path, active):
    _, store, _ = make_store(tmp_path, [('assistant', '训练 A 得到模型 B')])
    if active:
        enable(store)
    for _ in range(3):
        g.run_chat(store, TimeoutExtract(), 'chat-1', jev=Judge(), step_budget=1)
    assert control(store)['failures'] == 3
    result = g.run_chat(store, FakeExtractor([output(entries(store))]), 'chat-1',
                        jev=TimeoutJudge(), step_budget=1)
    assert result['failure_codes'] == ['provider_timeout']
    assert control(store)['failures'] == (1 if active else 4)
    assert control(store)['state'] == ('retry_wait' if active else 'blocked')
    # Replaying successful extraction/identity must not reset relation failures.
    if active:
        for expected in (2, 3, 4):
            g.run_chat(store, FakeExtractor([]), 'chat-1', jev=TimeoutJudge(), step_budget=1)
            assert control(store)['failures'] == expected
        assert control(store)['state'] == 'blocked'
        before = store.db.execute('SELECT count(*) FROM sg2_attempts').fetchone()[0]
        g.run_chat(store, FakeExtractor([]), 'chat-1', jev=Judge(), respect_schedule=True)
        assert store.db.execute('SELECT count(*) FROM sg2_attempts').fetchone()[0] == before


def test_legacy_unknown_scope_preserves_retry_exhaustion(tmp_path):
    _, store, _ = make_store(tmp_path, [('assistant', '训练 A 得到模型 B')])
    for _ in range(3):
        g.run_chat(store, TimeoutExtract(), 'chat-1', jev=Judge(), step_budget=1)
    enable(store)
    g.run_chat(store, TimeoutExtract(), 'chat-1', jev=Judge(), step_budget=1)
    assert control(store)['state'] == 'blocked'
    assert control(store)['failures'] == 4


def test_budget_yield_is_not_a_failed_stage_or_job(tmp_path):
    _, store, _ = make_store(tmp_path, [('assistant', '训练 A 得到模型 B')])
    enable(store)
    first = g.run_chat(store, FakeExtractor([output(entries(store))]), 'chat-1',
                       jev=Judge(), call_budget=1)
    assert first['model_calls'] == 1
    assert first['failed'] == []
    assert first['failure_codes'] == []
    assert first['graph_v2']['budget_exhausted'] is True
    assert first['graph_v2']['deferred_reason'] == 'graph_call_budget_pending'
    assert store.db.execute("SELECT count(*) FROM sg2_attempts WHERE state='failed'").fetchone()[0] == 0
    assert store.db.execute('SELECT count(*) FROM sg2_control').fetchone()[0] == 0
    assert store.db.execute('SELECT error FROM sg2_jobs').fetchone()[0] is None
    second = g.run_chat(store, FakeExtractor([]), 'chat-1', jev=Judge(), call_budget=1)
    assert second['model_calls'] == 1
    assert len(second['saved_relations']) == 1
    assert store.db.execute('SELECT state FROM sg2_jobs').fetchone()[0] == 'complete'


def test_enabling_policy_reuses_successful_stage_checkpoint(tmp_path):
    _, store, _ = make_store(tmp_path, [('assistant', '训练 A 得到模型 B')])
    g.run_chat(store, FakeExtractor([output(entries(store))]), 'chat-1', jev=TimeoutJudge(), call_budget=1)
    part_before = store.db.execute("SELECT part FROM sg2_checkpoints WHERE stage='identity'").fetchone()[0]
    enable(store)
    g.run_chat(store, FakeExtractor([]), 'chat-1', jev=Judge())
    identities = store.db.execute("SELECT part FROM sg2_attempts WHERE stage='identity'").fetchall()
    assert [r[0] for r in identities] == [part_before]


@pytest.mark.parametrize('active,event,expected', [(True, 'AutoContinue', 1), (False, 'AutoContinue', 4), (True, 'manual', 4)])
def test_automatic_runner_releases_project_lock_after_bounded_round(tmp_path, active, event, expected):
    config, store, _ = make_store(tmp_path, [('assistant', '训练 A 得到模型 B')])
    path = __import__('pathlib').Path(store.config['database']).parent / 'config.json'
    settings = json.loads(path.read_text())
    settings['graph_v2'] = {'enabled': True, 'recovery_policy': {'enabled': active}}
    path.write_text(json.dumps(settings))
    calls = []
    def execute(argv, timeout):
        if 'refresh-chat' in argv:
            return {'added_entries': 0, 'skipped': []}
        calls.append(argv)
        remaining = len(calls) < 4
        return dict(saved=['node'], failed=[], model_calls=1,
                    graph_v2=dict(durable_stages=True, source_remaining=remaining,
                                  runnable_remaining=remaining, work_units=1))
    result = run_update(path, 'chat-1', event, execute=execute)
    assert len(calls) == expected
    if active and event != 'manual':
        assert result['state'] == 'pending'
        assert result['error_code'] is None
        assert result['scheduling_policy'] == 'bounded_recovery_v1'
        assert result['model_call_limit'] == 3
        assert '--memories-call-budget' in calls[0]
        assert calls[0][calls[0].index('--memories-call-budget') + 1] == '3'
    else:
        assert result['state'] == 'succeeded'
        assert '--memories-call-budget' not in calls[0]


def test_two_projects_do_not_share_policy(tmp_path):
    (tmp_path / 'one').mkdir()
    (tmp_path / 'two').mkdir()
    _, first, _ = make_store(tmp_path / 'one', [('assistant', '训练 A 得到模型 B')])
    _, second, _ = make_store(tmp_path / 'two', [('assistant', '训练 A 得到模型 B')])
    enable(first)
    for store, expected in [(first, []), (second, ['graph_call_budget_pending'])]:
        result = g.run_chat(store, FakeExtractor([output(entries(store))]), 'chat-1', jev=Judge(), call_budget=1)
        assert result['failure_codes'] == expected


def test_successful_stage_closes_its_retry_wait_before_budget_yield(tmp_path):
    _, store, _ = make_store(tmp_path, [('assistant', '训练 A 得到模型 B')])
    enable(store)
    g.run_chat(store, TimeoutExtract(), 'chat-1', jev=Judge(), step_budget=1)
    assert control(store)['failures'] == 1
    result = g.run_chat(store, FakeExtractor([output(entries(store))]), 'chat-1',
                        jev=Judge(), call_budget=1)
    assert result['failed'] == []
    assert store.db.execute('SELECT count(*) FROM sg2_control').fetchone()[0] == 0


@pytest.mark.parametrize('calls,expected', [(0, 'blocked'), (1, 'ready')])
def test_bounded_continuation_tracks_actual_progress_across_runs(tmp_path, calls, expected):
    from pathlib import Path
    from sessiongraph import continuation
    _, store, _ = make_store(tmp_path, [('assistant', '训练 A 得到模型 B')])
    path = Path(store.config['database']).parent / 'config.json'
    config = json.loads(path.read_text())
    config['graph_v2'] = {'enabled': True, 'recovery_policy': {'enabled': True}}
    path.write_text(json.dumps(config))
    for i in range(1, 4):
        run = dict(run_id=str(i), chat_id='chat-1', event='AutoContinue', state='pending',
                   target_rowid=1, updated_at=i, error_code=None, reported_model_calls=calls,
                   graph_v2=dict(durable_stages=True, source_remaining=True, runnable_remaining=True,
                                 deferred_reason='graph_call_budget_pending'))
        continuation.record(path, run, launch=False, now=i)
    state = continuation.snapshot(store.config)[0]
    assert state['state'] == expected
    assert state['no_progress'] == (3 if calls == 0 else 0)


def test_real_worker_restarts_resume_under_one_call_budget(tmp_path):
    import sys
    from sessiongraph.auto_update import _execute
    from sessiongraph.core import Store, load_config
    from test_archived_worker_regression import archived
    root, store, old, moved = archived(tmp_path)
    target = store.db.execute('SELECT max(rowid) FROM entries').fetchone()[0]
    store.close()
    path = root / '.sessiongraph/config.json'
    cfg = json.loads(path.read_text())
    cfg['graph_v2'] = {'enabled': True, 'recovery_policy': {'enabled': True, 'max_calls_per_run': 1}}
    path.write_text(json.dumps(cfg))
    shim = '''import sys
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
    def execute(argv, timeout):
        if 'refresh-chat' in argv:
            return _execute(argv, timeout)
        return _execute([sys.executable, '-B', '-c', shim, *argv[3:]], timeout)
    first = run_update(path, 'chat-1', 'AutoContinue', transcript=str(old),
                       through_rowid=target, execute=execute)
    assert first['state'] == 'pending', first
    assert first['reported_model_calls'] == 1
    assert first['failed_batches'] == 0
    second = run_update(path, 'chat-1', 'AutoContinue', transcript=str(old),
                        through_rowid=target, execute=execute)
    assert second['state'] == 'succeeded', second
    assert second['reported_model_calls'] == 1
    final = Store(load_config(path), readonly=True)
    try:
        assert final.db.execute("SELECT count(*) FROM sg2_jobs WHERE state='complete'").fetchone()[0] == 1
        assert final.db.execute("SELECT count(*) FROM sg2_attempts WHERE stage='extract'").fetchone()[0] == 1
        assert final.db.execute("SELECT count(*) FROM sg2_attempts WHERE state='failed'").fetchone()[0] == 0
    finally:
        final.close()


@pytest.mark.parametrize('policy', [None, {'enabled': 'yes'}, {'enabled': True, 'max_calls_per_run': 0},
                                    {'enabled': True, 'max_calls_per_run': 4},
                                    {'enabled': True, 'max_calls_per_run': True}])
def test_invalid_policy_fails_closed(policy):
    from sessiongraph.recovery_policy import settings
    with pytest.raises(ValueError):
        settings({'graph_v2': {'recovery_policy': policy}})


def test_retry_scope_uses_effective_provider_without_credentials(tmp_path, monkeypatch):
    from sessiongraph.recovery_policy import retry_scope
    _, store, _ = make_store(tmp_path, [('assistant', '训练 A 得到模型 B')])
    enable(store)
    provider = store.config['provider']
    provider.update(protocol='chat-completions', model_env='SCOPE_TEST_MODEL', base_url_env='SCOPE_TEST_URL')
    monkeypatch.setenv('SCOPE_TEST_MODEL', 'model-a')
    monkeypatch.setenv('SCOPE_TEST_URL', 'http://127.0.0.1:4317/v1')
    provider['api_key'] = 'synthetic-private-credential'
    first = retry_scope(store.config, 'job', 'part', 'extract')
    monkeypatch.setenv('SCOPE_TEST_MODEL', 'model-b')
    second = retry_scope(store.config, 'job', 'part', 'extract')
    assert first['provider_fingerprint'] != second['provider_fingerprint']
    assert 'synthetic-private-credential' not in json.dumps(first)
    assert 'http://' not in json.dumps(first)
