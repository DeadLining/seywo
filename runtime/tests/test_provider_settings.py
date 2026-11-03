import json
import sqlite3
import socket

import pytest

from sessiongraph.core import init_project, load_config
from sessiongraph.native_settings import handle
from sessiongraph.provider_credentials import load_key
from sessiongraph.extraction import ResponsesExtractor
from sessiongraph.model_judge import ModelJudge
from sessiongraph.single_token_judge import SingleTokenJudge


@pytest.fixture
def app(tmp_path, monkeypatch):
    state = tmp_path / 'app'
    state.mkdir()
    home = tmp_path / 'codex'
    home.mkdir()
    monkeypatch.setenv('SESSIONGRAPH_RUNTIME_HOME', str(state))
    monkeypatch.setenv('GRAPH_LLM_API_KEY', 'fixture-env-key')
    return dict(state=state, codex_home=home, source=tmp_path / 'source')


def save(app, model='shared-model', **extra):
    return handle(dict(action='save_provider', model=model,
        endpoint='https://provider.example/v1/responses', **extra), **app)


def test_no_project_global_settings_never_open_database_or_network(app, monkeypatch):
    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **k: pytest.fail('database opened'))
    monkeypatch.setattr(socket, 'socket', lambda *a, **k: pytest.fail('network opened'))
    result = save(app, api_key='fixture-saved-key')
    assert result['model'] == 'shared-model' and result['model_calls'] == 0
    assert handle({'action':'status'}, **app)['model'] == 'shared-model'
    assert load_key(app['state'], result['endpoint']) == 'fixture-saved-key'
    assert 'fixture-saved-key' not in json.dumps(result)
    assert 'fixture-saved-key' not in (app['state'] / 'template.json').read_text()
    save(app)
    assert load_key(app['state'], result['endpoint']) == 'fixture-saved-key'


def test_old_and_new_projects_share_provider_without_config_fanout(app, tmp_path):
    first = init_project(tmp_path / 'first')
    raw = json.loads(first.read_text())
    raw['provider'].update(protocol='responses', endpoint='https://old.example/responses',
        model='stale', timeout_seconds=180, max_output_tokens=6000)
    raw['graph_v2'] = {'enabled':True, 'judge_backend':'single_token',
        'single_token_judge':{'request_logprobs':False, 'expected_model':'stale', 'route_revision':'old'}}
    first.write_text(json.dumps(raw))
    before = first.read_bytes()
    save(app)
    second = init_project(tmp_path / 'second')
    for path in (first, second):
        config = load_config(path)
        clients = (ResponsesExtractor(config), ModelJudge(config), SingleTokenJudge(config))
        assert {client.model for client in clients} == {'shared-model'}
        assert {client.endpoint for client in clients} == {'https://provider.example/v1/responses'}
        assert clients[2].max_output == 1 and not clients[2].reusable
        assert config['provider']['timeout_seconds'] == (180 if path == first else 90)
    assert load_config(first)['provider']['max_output_tokens'] == 6000
    save(app, model='changed-model')
    assert load_config(first)['provider']['model'] == 'changed-model'
    assert load_config(second)['provider']['model'] == 'changed-model'
    assert first.read_bytes() == before


def test_matching_route_keeps_pins_and_missing_template_keeps_standalone_config(app, tmp_path):
    path = init_project(tmp_path / 'standalone')
    raw = json.loads(path.read_text())
    raw['provider'].update(protocol='responses', endpoint='https://provider.example/v1/responses',
        model='shared-model', auth='bearer')
    raw['graph_v2'] = {'single_token_judge':{'expected_model':'real-backend','route_revision':'verified'}}
    path.write_text(json.dumps(raw))
    assert load_config(path)['provider'] == raw['provider']
    save(app)
    # The canonical save does not seed env-only identity fields; unrelated env
    # differences also invalidate old route pins conservatively.
    canonical = json.loads((app['state'] / 'template.json').read_text())
    canonical['provider'] = raw['provider']
    (app['state'] / 'template.json').write_text(json.dumps(canonical))
    assert SingleTokenJudge(load_config(path)).reusable


@pytest.mark.parametrize('body', ['bad-json', '[]', '{"provider":[]}'])
def test_invalid_global_settings_never_fall_back_to_stale_model(app, tmp_path, body):
    path = init_project(tmp_path / 'fixture')
    (app['state'] / 'template.json').write_text(body)
    with pytest.raises(ValueError):
        load_config(path)


def test_registration_reinstall_preserves_global_provider(app, tmp_path):
    from sessiongraph.gateway_install import install
    import sys
    path = init_project(tmp_path / 'project')
    source = app['source']
    source.mkdir()
    (source / 'entrypoint.py').write_text('')
    save(app)
    install(codex_home=app['codex_home'], state=app['state'], app=tmp_path / 'app.bundle',
        python=__import__('pathlib').Path(sys.executable), legacy=tmp_path / 'old',
        project_config=path, source_root=source, install_hooks=False, install_mcp=False)
    assert load_config(path)['provider']['model'] == 'shared-model'
    assert handle({'action':'status'}, **app)['model'] == 'shared-model'
