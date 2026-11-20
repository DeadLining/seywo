"""A user's model choice is shared across projects and worker processes."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from seywo.config import initialize, load_config
from seywo.plugin import create_plugin_server
from seywo.settings import provider_status, read_provider, save_provider, use_codex


def call(server, name, **args):
    return json.loads(asyncio.run(server.call_tool(name, args))[0].text)


def test_selection_is_shared_across_initialized_and_uninitialized_projects(tmp_path):
    a, b, c = [tmp_path / name for name in ('a', 'b', 'c')]
    initialize(a, plugin_hook=True)
    initialize(b, plugin_hook=True)
    c.mkdir()
    paths = [root / '.seywo/config.json' for root in (a, b)]
    before = [p.read_bytes() for p in paths]
    servers = [create_plugin_server(root) for root in (a, b, c)]
    call(servers[0], 'seywo_model_save', backend='codex', model='gpt-6.1', reasoning_effort='high')
    for server in servers:
        status = call(server, 'seywo_model_status')
        assert (status['model'], status['reasoning_effort'], status['scope']) == ('gpt-6.1', 'high', 'user')
    assert read_provider()['model'] == 'gpt-6.1'
    assert [p.read_bytes() for p in paths] == before
    assert not (c / '.seywo').exists()


def test_uninitialized_project_saves_persistently_and_probes_global_choice(tmp_path, monkeypatch):
    from seywo import plugin_ui
    calls = []
    class Probe:
        def __init__(self, **kwargs): calls.append(kwargs)
        def probe(self): return {'output': {'ok': True}, 'finish': 'complete'}
    monkeypatch.setattr(plugin_ui, 'CodexTransport', Probe)
    server = create_plugin_server(tmp_path)
    saved = call(server, 'seywo_model_save', backend='codex', model='gpt-6.1', reasoning_effort='medium')
    assert saved['persisted'] is True
    assert call(server, 'seywo_model_test')['ok']
    assert calls[0]['model'] == 'gpt-6.1'
    assert calls[0]['reasoning_effort'] == 'medium'
    assert not (tmp_path / '.seywo').exists()


def test_project_overrides_never_shadow_user_selection(tmp_path):
    use_codex(model='gpt-6.1', reasoning_effort='high')
    stale = {'model_runtime': {'backend': 'responses'},
             'provider': {'endpoint': 'https://old.test/responses', 'model': 'old'}}
    initialize(tmp_path)
    path = tmp_path / '.seywo/config.json'
    path.write_text(json.dumps({**json.loads(path.read_text()), **stale}))
    status = call(create_plugin_server(tmp_path), 'seywo_model_status')
    assert status['model'] == 'gpt-6.1'
    assert status['backend'] == 'codex'


def test_custom_provider_and_key_are_shared_without_project_initialization(tmp_path, user_settings_home, monkeypatch):
    from seywo import plugin_ui
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir(); b.mkdir()
    call(create_plugin_server(a), 'seywo_model_save', backend='responses',
         endpoint='https://example.test/v1', model='custom', api_key='private-test-key')
    status = call(create_plugin_server(b), 'seywo_model_status')
    assert status['model'] == 'custom' and status['credential_configured']
    assert status['scope'] == 'user' and 'private-test-key' not in json.dumps(status)
    assert 'private-test-key' not in (user_settings_home / 'settings.json').read_text()
    calls = []
    class Probe:
        def __init__(self, endpoint, key, model, **kwargs): calls.append((endpoint, key, model))
        def _complete_json(self, **kwargs): return {'output': {'ok': True}}
    monkeypatch.setattr(plugin_ui, 'Transport', Probe)
    assert call(create_plugin_server(b), 'seywo_model_test')['ok']
    assert calls == [('https://example.test/v1/responses', 'private-test-key', 'custom')]
    assert not (a / '.seywo').exists() and not (b / '.seywo').exists()


def test_default_reset_is_global_and_reads_do_not_create_storage(user_settings_home):
    assert provider_status()['model'] == 'codex-default'
    assert not user_settings_home.exists()
    use_codex(model='gpt-6.1', reasoning_effort='high')
    use_codex(model='codex-default', reasoning_effort='auto')
    assert read_provider()['model'] == 'codex-default'
    assert read_provider()['reasoning_effort'] == 'auto'


def test_invalid_save_does_not_change_global_settings(user_settings_home):
    use_codex(model='gpt-6.1')
    before = (user_settings_home / 'settings.json').read_bytes()
    with pytest.raises(ValueError, match='invalid_reasoning_selection'):
        use_codex(model='other', reasoning_effort='extreme')
    assert (user_settings_home / 'settings.json').read_bytes() == before


def test_concurrent_saves_publish_complete_settings_and_matching_keys():
    def save(index):
        save_provider(endpoint=f'https://{index}.test/v1', model=f'model-{index}', api_key=f'key-{index}')
        current = read_provider()
        assert current['_api_key'] == current['model'].replace('model-', 'key-')
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(save, range(18)))


def test_corrupt_settings_fail_instead_of_silently_using_default(user_settings_home):
    use_codex(model='gpt-6.1')
    (user_settings_home / 'settings.json').write_text('{broken')
    with pytest.raises(ValueError):
        read_provider()


def test_symlink_settings_directory_is_rejected(tmp_path, user_settings_home):
    target = tmp_path / 'target'; target.mkdir()
    user_settings_home.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        use_codex(model='gpt-6.1')
    assert not list(target.iterdir())
