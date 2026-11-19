"""Recovery and resource invariants for a process-owned Codex worker."""
import concurrent.futures
import json
from pathlib import Path
import sys
import time

import pytest
from seywo.codex_app_server import CodexAppServerWorker, close_workers


@pytest.fixture
def service(tmp_path, monkeypatch):
    log = tmp_path / 'rpc.jsonl'
    monkeypatch.setenv('SEYWO_TEST_LOG', str(log))
    command = [sys.executable, str(Path(__file__).with_name('fake_codex_app_server.py'))]
    created = []
    def make(mode='ok', timeout=2):
        monkeypatch.setenv('SEYWO_TEST_MODE', mode)
        w = CodexAppServerWorker(command=command, home=tmp_path, timeout=timeout)
        created.append(w)
        return w
    yield make, log, command
    for w in created:
        w.close()
    close_workers()


def rows(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


def probe(w, root):
    return w.probe(model='fixture', provider='fixture-provider', cwd=root)


@pytest.mark.parametrize('mode', ['early', 'retry_error'])
def test_events_before_ack_and_retrying_errors_do_not_lose_success(service, tmp_path, mode):
    make, _, _ = service
    result = probe(make(mode), tmp_path)
    assert result['finish'] == 'complete', result
    assert result['output'] == {'ok': True}
    assert result['usage'] == {'input_tokens': 120, 'output_tokens': 30}
    assert result['cached_input_tokens'] == 40


def test_reuse_process_but_release_each_isolated_thread(service, tmp_path):
    make, log, _ = service
    w = make()
    assert probe(w, tmp_path)['finish'] == 'complete'
    assert probe(w, tmp_path)['finish'] == 'complete'
    records = rows(log)
    assert len({r['pid'] for r in records}) == 1
    assert sum(r['request']['method'] == 'initialize' for r in records) == 1
    starts = [r['request'] for r in records if r['request']['method'] == 'thread/start']
    released = [r['request']['params']['threadId'] for r in records if r['request']['method'] == 'thread/unsubscribe']
    assert len(starts) == 2 and released == ['thread-1', 'thread-2']
    assert all(s['params']['ephemeral'] for s in starts)


@pytest.mark.parametrize('mode, error', [('tool', 'codex_unexpected_tool_use'),
    ('terminal_tool', 'codex_unexpected_tool_use'), ('server_request', 'codex_unexpected_tool_use'),
    ('invalid_json', 'invalid_json'), ('wrong_schema', 'invalid_json'),
    ('oversize', 'codex_output_missing_or_large'), ('interrupted', 'codex_interrupted')])
def test_failed_turn_never_returns_success(service, tmp_path, mode, error):
    make, _, _ = service
    result = probe(make(mode), tmp_path)
    assert result['error'] == error, result
    assert result['output'] is None


def test_dead_worker_restarts_next_request_without_replaying_generation(service, tmp_path, monkeypatch):
    make, _, _ = service
    w = make('hang', timeout=.15)
    result = probe(w, tmp_path)
    assert result['error'] == 'codex_timeout', result
    monkeypatch.setenv('SEYWO_TEST_MODE', 'ok')
    result = probe(w, tmp_path)
    assert result['finish'] == 'complete', result


def test_startup_failure_has_zero_model_calls_and_safe_auth_reason(service, tmp_path):
    make, _, _ = service
    result = probe(make('crash'), tmp_path)
    assert result['error'] == 'codex_auth_required', result
    assert result['model_calls'] == 0
    assert 'secret-token' not in json.dumps(result)


def test_concurrent_requests_cannot_mix_threads(service, tmp_path):
    make, log, _ = service
    w = make()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: probe(w, tmp_path), range(2)))
    assert all(r['finish'] == 'complete' for r in results), results
    methods = [r['request']['method'] for r in rows(log)]
    assert methods.count('initialize') == 1
    assert methods.count('thread/unsubscribe') == 2


def test_timeout_interrupts_active_turn_and_closes_process(service, tmp_path):
    make, log, _ = service
    w = make('hang', timeout=.15)
    began = time.monotonic()
    result = probe(w, tmp_path)
    assert result['error'] == 'codex_timeout'
    assert time.monotonic() - began < 2
    assert not w.alive
    assert 'turn/interrupt' in [r['request']['method'] for r in rows(log)]


def test_openai_account_missing_auth_is_reported_before_model_turn(service, tmp_path):
    make, log, _ = service
    result = make('no_auth').probe(model='fixture', provider='openai')
    assert result['error'] == 'codex_auth_required'
    assert result['model_calls'] == 0
    assert 'turn/start' not in [r['request']['method'] for r in rows(log)]


def test_catalog_generation_share_worker_and_cache_without_persistent_thread(service, tmp_path):
    from seywo.codex_transport import CodexTransport
    from seywo.codex_runtime import list_models
    from seywo.codex_app_server import get_worker
    make, log, command = service
    make()  # configure fixture environment
    (tmp_path / 'config.toml').write_text('model="fixture"\nmodel_provider="fixture-provider"\n')
    assert list_models(command=command, home=tmp_path)['models'][0]['model'] == 'fixture'
    transport = CodexTransport(command=command, home=tmp_path, timeout=2)
    assert transport.probe()['finish'] == 'complete'
    assert transport.probe()['finish'] == 'complete'
    records = rows(log)
    assert len({r['pid'] for r in records}) == 1
    assert sum(r['request']['method'] == 'model/list' for r in records) == 1
    old = get_worker(command=command, home=tmp_path)
    (tmp_path / 'config.toml').write_text('model="fixture"\nmodel_provider="other-provider"\n')
    new = get_worker(command=command, home=tmp_path)
    assert new is not old and not old.alive


def test_account_flow_exposes_no_email_or_tokens_and_reuses_login(service):
    make, log, _ = service
    w = make()
    assert w.account() == {'state': 'ready', 'requires_auth': False, 'account_type': 'chatgpt', 'model_calls': 0}
    first, second = w.account(action='login'), w.account(action='login')
    assert first == second and first['auth_url'].startswith('https://auth.openai.com/')
    assert 'private@example' not in json.dumps([first, w.account()])
    assert sum(r['request']['method'] == 'account/login/start' for r in rows(log)) == 1
    assert 'turn/start' not in [r['request']['method'] for r in rows(log)]


def test_malformed_event_resets_worker_before_next_request(service, tmp_path, monkeypatch):
    make, log, _ = service
    w = make('malformed_event')
    result = probe(w, tmp_path)
    assert result['finish'] == 'error'
    assert result['error'] == 'codex_app_server_invalid'
    assert not w.alive
    monkeypatch.setenv('SEYWO_TEST_MODE', 'ok')
    assert probe(w, tmp_path)['finish'] == 'complete'
    assert len({r['pid'] for r in rows(log)}) == 2


def test_buffer_is_bounded_even_without_an_active_consumer(service, tmp_path):
    from seywo.codex_rpc import RpcConnection
    make, _, command = service
    make('idle_flood')
    rpc = RpcConnection(command, tmp_path)
    try:
        deadline = time.monotonic() + 3
        rpc.request('initialize', {}, deadline)
        rpc.send({'method': 'initialized'}, deadline)
        while rpc.failure is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert rpc.failure == 'codex_output_limit'
        assert not rpc.alive
    finally:
        rpc.close()


def test_busy_worker_returns_without_starting_a_second_process(service, tmp_path):
    make, log, _ = service
    w = make(timeout=.1)
    w._lock.acquire()
    try:
        began = time.monotonic()
        result = probe(w, tmp_path)
        assert result['error'] == 'codex_worker_busy' and result['model_calls'] == 0
        assert time.monotonic() - began < 1
        assert not log.exists()
    finally:
        w._lock.release()


def test_probe_before_authorization_finishes_keeps_pending_browser_login(service):
    make, log, _ = service
    w = make('no_auth')
    first = w.account(action='login')
    result = w.probe(model='fixture', provider='openai')
    assert result['error'] == 'codex_auth_required' and result['model_calls'] == 0
    assert w.account(action='login') == first
    assert sum(r['request']['method'] == 'account/login/start' for r in rows(log)) == 1
    assert w.account(action='cancel') == {'state': 'cancelled', 'model_calls': 0}
