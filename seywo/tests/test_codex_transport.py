"""Codex app-server transport keeps semantic calls isolated and bounded."""
import json
from pathlib import Path
import sys

import pytest

from seywo.config import initialize, load_config, enable_plugin_hook
from seywo.settings import read_provider


def test_plugin_enable_selects_codex_and_preserves_existing_provider(tmp_path):
    initialize(tmp_path)
    path = tmp_path / '.seywo/config.json'
    data = json.loads(path.read_text())
    data['provider'] = {'endpoint': 'https://example.test/v1/responses', 'model': 'old-model'}
    path.write_text(json.dumps(data))
    enable_plugin_hook(path)
    saved = load_config(path)
    assert 'model_runtime' not in saved
    assert saved['provider'] == data['provider']
    assert read_provider()['backend'] == 'codex'


def fake_app_server(tmp_path, *, mode='ok', model='fixture'):
    script = tmp_path / 'fake-codex-app-server.py'
    script.write_text(r'''
import json, os, sys, time
mode = os.environ.get('FAKE_APP_SERVER_MODE', 'ok')
model = os.environ.get('FAKE_APP_SERVER_MODEL', 'fixture')
thread_id = 'thread-fixture'
turn_id = 'turn-fixture'
log = os.environ.get('FAKE_APP_SERVER_LOG')
def emit(value):
    if log:
        with open(log, 'a', encoding='utf-8') as f: f.write(json.dumps(value) + '\n')
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    emit({'seen': request})
    method = request.get('method')
    rid = request.get('id')
    if method == 'initialize':
        emit({'id': rid, 'result': {'userAgent': 'fake'}})
    elif method == 'model/list':
        emit({'id': rid, 'result': {'data': [{'model': model, 'displayName': model, 'isDefault': True,
            'supportedReasoningEfforts': [{'reasoningEffort': 'low'}, {'reasoningEffort': 'high'}],
            'defaultReasoningEffort': 'low'}], 'nextCursor': None}})
    elif method == 'account/read':
        print(json.dumps({'id': rid, 'result': {'requiresOpenaiAuth': False}}), flush=True)
    elif method == 'thread/start':
        if mode == 'auth':
            emit({'id': rid, 'error': {'code': -32000, 'message': 'Unauthorized secret-token'}})
        else:
            emit({'id': rid, 'result': {'thread': {'id': thread_id}}})
    elif method == 'turn/start':
        if mode == 'hang':
            emit({'id': rid, 'result': {'turn': {'id': turn_id, 'status': 'inProgress'}}})
            time.sleep(30)
            continue
        emit({'id': rid, 'result': {'turn': {'id': turn_id, 'status': 'inProgress'}}})
        if mode == 'tool':
            emit({'method': 'item/completed', 'params': {'threadId': thread_id, 'turnId': turn_id,
                'item': {'id': 'tool-1', 'type': 'commandExecution', 'command': 'cat secret'}}})
            continue
        input_text = request.get('params', {}).get('input', [{}])[0].get('text', '')
        text = '{"ok":true}' if 'ok' in input_text and 'segments' not in input_text else '{"segments":[],"changes":[],"current":{"goal_index":null,"priority_indexes":[]}}'
        emit({'method': 'item/agentMessage/delta', 'params': {'threadId': thread_id, 'turnId': turn_id,
            'itemId': 'agent-1', 'delta': text}})
        emit({'method': 'item/completed', 'params': {'threadId': thread_id, 'turnId': turn_id,
            'item': {'id': 'agent-1', 'type': 'agentMessage', 'text': text}}})
        emit({'method': 'turn/completed', 'params': {'threadId': thread_id,
            'turn': {'id': turn_id, 'status': 'completed', 'items': []}}})
    elif method == 'thread/unsubscribe':
        print(json.dumps({'id': rid, 'result': {}}), flush=True)
    elif method == 'turn/interrupt':
        emit({'id': rid, 'result': {}})
''', encoding='utf-8')
    return [sys.executable, str(script)]


def configure_fake(monkeypatch, tmp_path, *, mode='ok', model='fixture'):
    monkeypatch.setenv('FAKE_APP_SERVER_MODE', mode)
    monkeypatch.setenv('FAKE_APP_SERVER_MODEL', model)
    monkeypatch.setenv('FAKE_APP_SERVER_LOG', str(tmp_path / 'app-server.log'))
    return fake_app_server(tmp_path, mode=mode, model=model)


def requests(tmp_path):
    path = tmp_path / 'app-server.log'
    return [row['seen'] for row in (json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()) if 'seen' in row] if path.exists() else []


def test_codex_app_server_uses_structured_turn_and_disables_discovery(tmp_path, monkeypatch):
    from seywo.codex_transport import CodexTransport
    cli = configure_fake(monkeypatch, tmp_path)
    home = tmp_path / 'codex-home'
    home.mkdir()
    (home / 'config.toml').write_text('[mcp_servers.test]\ncommand="test"\n')
    result = CodexTransport(command=cli, home=home).complete_compact(
        model='codex-default', instructions='compact-fixture', payload={'turns': [], 'toolSecret': 'drop'}, max_output_tokens=768)
    assert result['finish'] == 'complete', result
    assert result['output']['changes'] == []
    seen = requests(tmp_path)
    start = next(row for row in seen if row.get('method') == 'thread/start')
    turn = next(row for row in seen if row.get('method') == 'turn/start')
    assert start['params']['ephemeral'] is True
    assert start['params']['sandbox'] == 'read-only'
    assert 'runtimeWorkspaceRoots' not in start['params']
    assert turn['params']['outputSchema']['properties']['segments']['type'] == 'array'
    assert 'toolSecret' in turn['params']['input'][0]['text']
    assert all('exec' not in str(row) for row in seen)


@pytest.mark.parametrize('mode,error', [('auth', 'codex_auth_required'), ('tool', 'codex_unexpected_tool_use'), ('hang', 'codex_timeout')])
def test_codex_app_server_failure_does_not_publish_or_echo_process_text(tmp_path, monkeypatch, mode, error):
    from seywo.codex_transport import CodexTransport
    cli = configure_fake(monkeypatch, tmp_path, mode=mode)
    # The deadline includes process startup and the handshake, not just turn execution.
    result = CodexTransport(command=cli, home=tmp_path, timeout=3).complete_compact(
        model='codex-default', instructions='test', payload={}, max_output_tokens=768)
    assert result['error'] == error, result
    if mode == 'hang':
        assert any(row['method'] == 'turn/start' for row in requests(tmp_path))
    assert result['output'] is None
    assert 'secret-token' not in json.dumps(result)


@pytest.mark.parametrize('effort', ['auto', 'high'])
def test_maintainer_routes_compact_through_codex_without_provider_credentials(tmp_path, monkeypatch, effort):
    from seywo import inbox, maintain
    from test_compact_maintain import CompactTransport, make_source
    initialize(tmp_path)
    path = tmp_path / '.seywo/config.json'
    enable_plugin_hook(path)
    from seywo.settings import use_codex
    use_codex(reasoning_effort=effort)
    config = load_config(path)
    fake = CompactTransport()
    fake.model = 'codex-default'
    runtime_options = []
    def factory(**kwargs):
        runtime_options.append(kwargs)
        return fake
    monkeypatch.setattr(maintain, 'CodexTransport', factory)
    chat = 'native-compact-fixture'
    inbox.record(config, chat, 'PreCompact', str(make_source(tmp_path, chat)))
    report = maintain.run_chat(config, chat)
    assert report['state'] == 'done', report
    assert report['calls'] == 1
    assert len(fake.payloads[0]['turns']) == 2
    assert runtime_options[0]['reasoning_effort'] == effort


def test_native_model_is_frozen_and_auditable_without_duplicate_schema_prompt(tmp_path, monkeypatch):
    from seywo.codex_transport import CodexTransport
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.toml').write_text('model="configured-model"\nmodel_provider="fixture-provider"\n')
    cli = configure_fake(monkeypatch, tmp_path, model='configured-model')
    result = CodexTransport(command=cli, home=home).complete_compact(
        model='codex-default', instructions='rules', payload={'turns': []})
    assert result['error'] is None, result
    assert result['model_info']['selected_model'] == 'configured-model'
    assert result['model_info']['provider'] == 'fixture-provider'
    turn = next(row for row in requests(tmp_path) if row.get('method') == 'turn/start')
    assert 'additionalProperties' not in turn['params']['input'][0]['text']


def test_model_worker_does_not_edit_user_configuration(tmp_path, monkeypatch):
    from seywo.codex_transport import CodexTransport
    home = tmp_path / 'home'
    skill = home / 'skills/private-skill/SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('private skill')
    config = home / 'config.toml'
    config.write_text('model="fixture"\n')
    cli = configure_fake(monkeypatch, tmp_path)
    result = CodexTransport(command=cli, home=home)._execute('rules', {}, {'type': 'object'})
    assert result['error'] is None, result
    assert config.read_text() == 'model="fixture"\n'
    assert skill.read_text() == 'private skill'
