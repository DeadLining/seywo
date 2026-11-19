"""Portable host executable discovery and zero-generation model catalog."""
import json
import sys

import pytest

from seywo import codex_transport, runtime_platform


def cli(tmp_path, body):
    path = tmp_path / 'runtime.py'
    path.write_text('import json, sys, time\n' + body)
    return [sys.executable, str(path)]


def test_host_runtime_precedes_unrelated_path_cli(tmp_path, monkeypatch):
    host = tmp_path / ('Host App/codex.exe' if sys.platform == 'win32' else 'Host App.app/Contents/MacOS/codex')
    host.parent.mkdir(parents=True)
    host.write_text('#!/bin/sh\n')
    host.chmod(0o755)
    monkeypatch.setenv('CODEX_APP_RUNTIME_CLI_PATH', str(host))
    monkeypatch.setattr(runtime_platform.shutil, 'which', lambda name: '/old/bin/codex')
    assert codex_transport.codex_command() == [str(host)]


def test_unusable_host_runtime_falls_back_to_path(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_APP_RUNTIME_CLI_PATH', str(tmp_path / 'missing'))
    executable = tmp_path / ('codex.exe' if sys.platform == 'win32' else 'codex')
    executable.write_bytes(b'fixture')
    executable.chmod(0o755)
    monkeypatch.setattr(runtime_platform.shutil, 'which', lambda name: str(executable))
    assert codex_transport.codex_command() == [str(executable)]


def test_catalog_handshake_paginates_without_starting_thread_or_exposing_text(tmp_path):
    from seywo.codex_runtime import list_models
    command = cli(tmp_path, '''
assert sys.argv[1] == 'app-server'
for raw in sys.stdin:
    msg = json.loads(raw)
    method = msg['method']
    assert method in {'initialize', 'initialized', 'model/list'}
    if method == 'initialized': continue
    if method == 'initialize': result = {'userAgent':'fixture'}
    elif msg['params'].get('cursor'):
        result = {'data':[{'model':'second','displayName':'Second','hidden':False}], 'nextCursor':None}
    else:
        result = {'data':[{'model':'first','displayName':'First','hidden':False},
                          {'model':'hidden','displayName':'Hidden','hidden':True}], 'nextCursor':'page2'}
        print(json.dumps({'method':'notice','params':{'private':'secret-token'}}),flush=True)
    print(json.dumps({'id':msg['id'],'result':result}),flush=True)
''')
    data = list_models(command=command, home=tmp_path, timeout=3)
    assert data['error'] is None, data
    assert [m['model'] for m in data['models']] == ['first', 'second']
    assert data['model_calls'] == 0
    assert 'secret-token' not in json.dumps(data)


@pytest.mark.parametrize('body, expected', [
    ('print("Unauthorized: Bearer secret-token",file=sys.stderr);sys.exit(1)', 'codex_auth_required'),
    ('print("error: unexpected argument --ephemeral",file=sys.stderr);sys.exit(2)', 'codex_app_server_incompatible'),
    ('print("Model missing is not supported",file=sys.stderr);sys.exit(1)', 'codex_model_unavailable'),
    ('time.sleep(3)', 'codex_timeout'),
])
def test_catalog_errors_are_safe_and_bounded(tmp_path, body, expected):
    from seywo.codex_runtime import list_models
    result = list_models(command=cli(tmp_path, body), home=tmp_path, timeout=0.2)
    assert result['error'] == expected, result
    assert result['models'] == []
    assert result['model_calls'] == 0
    assert 'secret-token' not in json.dumps(result)



def test_app_server_probe_uses_selected_reasoning_effort(tmp_path, monkeypatch):
    from seywo.codex_transport import CodexTransport
    from test_codex_transport import configure_fake, requests
    monkeypatch.setattr(codex_transport, 'list_models', lambda **kw: {'models': [
        {'model': 'fixture', 'reasoning_efforts': ['low', 'high'], 'default_reasoning_effort': 'low'}],
        'error': None})
    (tmp_path / 'config.toml').write_text('model="fixture"\n')
    command = configure_fake(monkeypatch, tmp_path)
    result = CodexTransport(command=command, home=tmp_path, reasoning_effort='high').probe()
    assert result['error'] is None, result
    assert result['model_info']['reasoning_effort'] == 'high'
    assert result['model_calls'] == 1
    turn = next(row for row in requests(tmp_path) if row.get('method') == 'turn/start')
    assert turn['params']['effort'] == 'high'


@pytest.mark.parametrize('catalog,error', [
    ({'models': [{'model': 'fixture', 'reasoning_efforts': ['low']}], 'error': None}, 'codex_reasoning_unsupported'),
    ({'models': [], 'error': 'codex_network_error'}, 'codex_network_error'),
    ({'models': [{'model': 'other', 'reasoning_efforts': ['high']}], 'error': None}, 'codex_reasoning_unsupported'),
])
def test_unverified_explicit_reasoning_never_starts_generation(tmp_path, monkeypatch, catalog, error):
    monkeypatch.setattr(codex_transport, 'list_models', lambda **kw: catalog)
    (tmp_path / 'config.toml').write_text('model="fixture"\n')
    marker = tmp_path / 'generated'
    command = cli(tmp_path, f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
    result = codex_transport.CodexTransport(command=command, home=tmp_path, reasoning_effort='high').probe()
    assert result['error'] == error
    assert result['model_calls'] == 0
    assert not marker.exists()
