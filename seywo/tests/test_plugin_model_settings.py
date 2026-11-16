"""The installed plugin owns model settings inside the Seywo project state."""
import json

from seywo.config import initialize, load_config
from seywo.plugin import create_plugin_server
from seywo.settings import read_provider


def test_save_provider_stores_model_and_credential_under_project_state(tmp_path):
    initialize(tmp_path)
    config_path = tmp_path / '.seywo/config.json'
    server = create_plugin_server(tmp_path)

    import asyncio
    result = asyncio.run(server.call_tool('seywo_model_save', {
        'endpoint': 'http://127.0.0.1:4317/openai/v1',
        'model': 'deepseek-v4-flash-0731',
        'api_key': 'sk-test-only',
    }))
    data = json.loads(result[0].text)
    assert data['saved'] is True
    assert data['credential_configured'] is True
    assert data['endpoint'].endswith('/responses')
    assert 'sk-test-only' not in (tmp_path / '.seywo/config.json').read_text()
    config = load_config(config_path)
    provider = read_provider(config)
    assert provider['model'] == 'deepseek-v4-flash-0731'
    assert provider['_api_key'] == 'sk-test-only'
    assert (tmp_path / '.seywo/credentials').is_dir()


def test_model_status_is_app_only_and_does_not_call_model(tmp_path):
    initialize(tmp_path)
    server = create_plugin_server(tmp_path)
    import asyncio
    result = asyncio.run(server.call_tool('seywo_model_status', {}))
    data = json.loads(result[0].text)
    assert data['configured'] is False
    assert data['model_calls'] == 0
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert tools['seywo_model_status'].meta['ui']['visibility'] == ['app']
    assert tools['seywo_model_save'].meta['ui']['visibility'] == ['app']


def test_explicit_model_test_makes_one_call_without_persisting_key(tmp_path, monkeypatch):
    initialize(tmp_path)
    from seywo import plugin_ui
    calls = []

    class FakeTransport:
        def __init__(self, endpoint, key, model, *, timeout):
            calls.append(('init', endpoint, key, model, timeout))

        def _complete_json(self, **kwargs):
            calls.append(('request', kwargs))
            return {'output': {'ok': True}, 'error': None, 'usage': {'input_tokens': 3, 'output_tokens': 2}, 'seconds': 0.01}

    monkeypatch.setattr(plugin_ui, 'Transport', FakeTransport)
    import asyncio
    result = asyncio.run(create_plugin_server(tmp_path).call_tool('seywo_model_test', {
        'endpoint': 'http://127.0.0.1:4317/openai/v1',
        'model': 'deepseek-v4-flash-0731',
        'api_key': 'one-call-only',
    }))
    data = json.loads(result[0].text)
    assert data['ok'] is True and data['model_calls'] == 1
    assert calls[0][2] == 'one-call-only'
    assert len(calls) == 2
    assert 'one-call-only' not in (tmp_path / '.seywo/config.json').read_text()


def test_plugin_default_uses_codex_without_api_key_and_can_switch_back(tmp_path):
    import asyncio
    server = create_plugin_server(tmp_path)
    asyncio.run(server.call_tool('seywo_initialize_project', {}))
    status = json.loads(asyncio.run(server.call_tool('seywo_model_status', {}))[0].text)
    assert status['backend'] == 'codex'
    assert status['credential_required'] is False
    assert status['model_calls'] == 0
    result = json.loads(asyncio.run(server.call_tool('seywo_model_save', {'backend': 'codex'}))[0].text)
    assert result['saved'] and result['backend'] == 'codex'
    assert read_provider(load_config(tmp_path / '.seywo/config.json'))['backend'] == 'codex'


def test_changed_endpoint_never_reuses_old_credential(tmp_path, monkeypatch):
    import asyncio
    from seywo import plugin_ui
    server = create_plugin_server(tmp_path)
    asyncio.run(server.call_tool('seywo_initialize_project', {}))
    asyncio.run(server.call_tool('seywo_model_save', {
        'endpoint': 'https://first.test/v1', 'model': 'test', 'api_key': 'first-private-key'}))
    monkeypatch.setattr(plugin_ui, 'Transport', lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not send')))
    from mcp.server.fastmcp.exceptions import ToolError
    import pytest
    with pytest.raises(ToolError, match='provider_not_configured'):
        asyncio.run(server.call_tool('seywo_model_test', {'endpoint': 'https://other.test/v1', 'model': 'test'}))


def test_plugin_codex_never_reads_standalone_template(tmp_path, monkeypatch):
    from seywo.config import enable_plugin_hook
    app = tmp_path / 'old-app'
    app.mkdir()
    (app / 'template.json').write_text('invalid JSON')
    monkeypatch.setenv('SEYWO_APP_STATE', str(app))
    root = tmp_path / 'plugin-project'
    initialize(root)
    path = root / '.seywo/config.json'
    enable_plugin_hook(path)
    assert read_provider(load_config(path))['backend'] == 'codex'


def test_standalone_saved_settings_remain_compatible(tmp_path, monkeypatch):
    app = tmp_path / 'old-app'
    app.mkdir()
    (app / 'template.json').write_text(json.dumps({'provider': {
        'endpoint': 'https://old.test/v1/responses', 'model': 'old-model'}}))
    monkeypatch.setenv('SEYWO_APP_STATE', str(app))
    assert read_provider()['model'] == 'old-model'


def test_settings_preserve_other_changes_with_stale_config(tmp_path):
    from seywo.settings import save_provider, use_codex
    path = tmp_path / '.seywo/config.json'
    initialize(tmp_path)
    stale = load_config(path)
    data = json.loads(path.read_text())
    data['hook'] = {'enabled': True, 'pipeline': 'seywo-compact-v1'}
    data['unrelated_preference'] = 'keep'
    path.write_text(json.dumps(data))
    save_provider(stale, endpoint='https://example.test/v1', model='test', api_key='test-only')
    assert load_config(path)['unrelated_preference'] == 'keep'
    use_codex(stale)
    current = load_config(path)
    assert current['hook']['enabled']
    assert current['provider']['model'] == 'test'
    assert current['unrelated_preference'] == 'keep'
