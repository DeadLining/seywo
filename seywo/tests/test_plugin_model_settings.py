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
    assert data['backend'] == 'codex'
    assert data['credential_required'] is False
    assert data['model_calls'] == 0
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert tools['seywo_model_status'].meta['ui']['visibility'] == ['app']
    assert tools['seywo_model_save'].meta['ui']['visibility'] == ['app']


def test_uninitialized_project_reports_default_codex_status(tmp_path):
    import asyncio
    server = create_plugin_server(tmp_path)
    result = asyncio.run(server.call_tool('seywo_model_status', {}))
    data = json.loads(result[0].text)
    assert data['backend'] == 'codex'
    assert data['credential_required'] is False
    assert data['model'] == 'codex-default'
    assert data['model_calls'] == 0
    assert not (tmp_path / '.seywo').exists()


def test_missing_runtime_in_existing_project_keeps_codex_default(tmp_path):
    import asyncio
    initialize(tmp_path)
    path = tmp_path / '.seywo/config.json'
    config = json.loads(path.read_text())
    config.pop('model_runtime', None)
    path.write_text(json.dumps(config))
    data = json.loads(asyncio.run(create_plugin_server(tmp_path).call_tool(
        'seywo_model_status', {}))[0].text)
    assert data['backend'] == 'codex'
    assert data['credential_required'] is False


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


def test_uninitialized_project_can_test_and_select_codex_without_writes(tmp_path, monkeypatch):
    import asyncio
    from seywo import plugin_ui

    calls = []

    class FakeTransport:
        def __init__(self, *, model, timeout, reasoning_effort="auto"):
            calls.append((model, timeout))

        def probe(self):
            return {'output': {'ok': True}, 'finish': 'complete', 'error': None,
                    'usage': {'input_tokens': 1, 'output_tokens': 1}, 'seconds': 0.01}

    monkeypatch.setattr(plugin_ui, 'CodexTransport', FakeTransport)
    server = create_plugin_server(tmp_path)
    tested = json.loads(asyncio.run(server.call_tool('seywo_model_test', {}))[0].text)
    selected = json.loads(asyncio.run(server.call_tool('seywo_model_save', {'backend': 'codex'}))[0].text)
    assert tested['ok'] and tested['model_calls'] == 1
    assert selected['saved'] and selected['persisted'] is False
    assert selected['backend'] == 'codex'
    assert calls == [('codex-default', 60)]
    assert not (tmp_path / '.seywo').exists()


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


def test_plugin_codex_uses_project_local_runtime(tmp_path):
    from seywo.config import enable_plugin_hook
    root = tmp_path / 'plugin-project'
    initialize(root)
    path = root / '.seywo/config.json'
    enable_plugin_hook(path)
    assert read_provider(load_config(path))['backend'] == 'codex'



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


def test_model_catalog_is_app_only_and_zero_generation(tmp_path, monkeypatch):
    import asyncio
    from seywo import plugin_ui
    monkeypatch.setattr(plugin_ui, 'list_codex_models', lambda **kwargs: {
        'models': [{'model': 'fixture-model', 'label': 'Fixture'}], 'error': None, 'model_calls': 0})
    home = tmp_path / 'host'
    home.mkdir()
    (home / 'config.toml').write_text('model="host-configured-model"\n')
    monkeypatch.setenv('CODEX_HOME', str(home))
    server = create_plugin_server(tmp_path)
    data = json.loads(asyncio.run(server.call_tool('seywo_model_list', {}))[0].text)
    assert data['default_model'] == 'host-configured-model'
    assert data['models'][0]['model'] == 'fixture-model'
    assert data['model_calls'] == 0
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert tools['seywo_model_list'].meta['ui']['visibility'] == ['app']
    assert not (tmp_path / '.seywo').exists()


def test_codex_probe_reports_safe_reason_and_actual_selection(tmp_path, monkeypatch):
    import asyncio
    from seywo import plugin_ui

    class Failure:
        def __init__(self, **kwargs): pass
        def probe(self):
            return {'output': None, 'finish': 'error', 'error': 'codex_auth_required',
                    'model_info': {'selected_model': 'fixture', 'provider': 'openai'}, 'seconds': .01}

    monkeypatch.setattr(plugin_ui, 'CodexTransport', Failure)
    data = json.loads(asyncio.run(create_plugin_server(tmp_path).call_tool(
        'seywo_model_test', {'backend': 'codex', 'model': 'fixture'}))[0].text)
    assert not data['ok']
    assert '授权' in data['message']
    assert data['model_info']['selected_model'] == 'fixture'



def test_explicit_codex_model_is_not_silently_discarded_before_initialization(tmp_path):
    import asyncio
    import pytest
    from mcp.server.fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match='initialization_required'):
        asyncio.run(create_plugin_server(tmp_path).call_tool('seywo_model_save', {
            'backend': 'codex', 'model': 'explicit-model'}))
    assert not (tmp_path / '.seywo').exists()


def test_codex_selection_persists_and_default_removes_previous_override(tmp_path):
    import asyncio
    initialize(tmp_path)
    server = create_plugin_server(tmp_path)
    asyncio.run(server.call_tool('seywo_model_save', {'backend': 'codex', 'model': 'chosen-model'}))
    assert read_provider(load_config(tmp_path / '.seywo/config.json'))['model'] == 'chosen-model'
    asyncio.run(server.call_tool('seywo_model_save', {'backend': 'codex', 'model': 'codex-default'}))
    assert load_config(tmp_path / '.seywo/config.json')['model_runtime'] == {'backend': 'codex'}


def test_codex_reasoning_round_trip_and_auto_reset(tmp_path):
    import asyncio
    initialize(tmp_path)
    path = tmp_path / '.seywo/config.json'
    server = create_plugin_server(tmp_path)
    saved = json.loads(asyncio.run(server.call_tool('seywo_model_save', {
        'backend': 'codex', 'model': 'chosen', 'reasoning_effort': 'high'}))[0].text)
    assert saved['reasoning_effort'] == 'high'
    assert read_provider(load_config(path))['reasoning_effort'] == 'high'
    asyncio.run(server.call_tool('seywo_model_save', {
        'backend': 'codex', 'model': 'chosen', 'reasoning_effort': 'auto'}))
    assert load_config(path)['model_runtime'] == {'backend': 'codex', 'model': 'chosen'}
    assert read_provider(load_config(path))['reasoning_effort'] == 'auto'


def test_codex_probe_uses_entered_effort_or_saved_effort_without_saving(tmp_path, monkeypatch):
    import asyncio
    from seywo import plugin_ui
    from seywo.settings import use_codex
    initialize(tmp_path)
    path = tmp_path / '.seywo/config.json'
    use_codex(load_config(path), model='chosen', reasoning_effort='medium')
    before = path.read_bytes()
    calls = []

    class Probe:
        def __init__(self, **kwargs): calls.append(kwargs)
        def probe(self): return {'output': {'ok': True}, 'finish': 'complete'}

    monkeypatch.setattr(plugin_ui, 'CodexTransport', Probe)
    server = create_plugin_server(tmp_path)
    for args in ({'backend': 'codex'}, {'backend': 'codex', 'reasoning_effort': 'high'},
                 {'backend': 'codex', 'reasoning_effort': 'auto'}):
        asyncio.run(server.call_tool('seywo_model_test', args))
    assert [c['reasoning_effort'] for c in calls] == ['medium', 'high', 'auto']
    assert path.read_bytes() == before


def test_invalid_reasoning_does_not_mutate_configuration(tmp_path):
    import pytest
    from seywo.settings import use_codex
    initialize(tmp_path)
    path = tmp_path / '.seywo/config.json'
    before = path.read_bytes()
    with pytest.raises(ValueError, match='invalid_reasoning_selection'):
        use_codex(load_config(path), reasoning_effort='extreme')
    assert path.read_bytes() == before


def test_account_action_is_explicit_app_only_and_never_initializes_project(tmp_path, monkeypatch):
    import asyncio
    from seywo import plugin_ui
    calls = []
    monkeypatch.setattr(plugin_ui, 'codex_account_status', lambda **kwargs:
        calls.append(kwargs) or {'state': 'login_pending', 'auth_url': 'https://auth.openai.com/authorize', 'model_calls': 0})
    server = create_plugin_server(tmp_path)
    data = json.loads(asyncio.run(server.call_tool('seywo_codex_account', {'action': 'login'}))[0].text)
    assert data['state'] == 'login_pending' and data['model_calls'] == 0
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert tools['seywo_codex_account'].meta['ui']['visibility'] == ['app']
    assert calls == [{'action': 'login'}]
    assert not (tmp_path / '.seywo').exists()
