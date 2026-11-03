"""Portable global write defaults, without provider calls or project fanout."""
from copy import deepcopy
import json
from pathlib import Path
import socket
import sqlite3
import sys

import pytest

from sessiongraph.core import init_project, load_config
from sessiongraph.global_hooks import ensure_project
from sessiongraph.hook_ingress import enabled as ingress_enabled
from sessiongraph.pipeline_settings import graph_template
from sessiongraph.resource_policy import settings as resource_settings
from sessiongraph.stage_leases import enabled as stage_enabled
from sessiongraph.revision_ledger import enabled as revision_enabled


@pytest.fixture
def shared(tmp_path, monkeypatch):
    state = tmp_path / 'shared'
    state.mkdir()
    monkeypatch.setenv('SESSIONGRAPH_RUNTIME_HOME', str(state))
    template = {
        'provider': {'model': 'shared-model', 'protocol': 'responses',
                     'endpoint': 'https://provider.example/v1/responses'},
        'extraction': {'max_batch_chars': 24000, 'max_input_chars': 32000,
                       'context_entries': 2, 'lease_seconds': 600},
        'hook': {'durable_ingress': True},
        'graph_v2': {
            'enabled': True, 'judge_backend': 'single_token',
            'condensation': {'enabled': True},
            'auto_compaction': {'enabled': True, 'node_threshold': 256,
                                'cooldown_seconds': 3600, 'max_model_calls': 4},
            'recovery_policy': {'enabled': True, 'max_calls_per_run': 3},
            'revision_policy': {'enabled': True, 'mode': 'effective'},
            'stage_policy': {'enabled': True},
            'resource_policy': {'enabled': True, 'max_requests': 64,
                'max_input_bytes': 2097152, 'max_output_tokens': 32000,
                'max_transport_seconds': 300, 'circuit_failure_threshold': 2,
                'circuit_cooldown_seconds': 60},
            'single_token_judge': {'request_logprobs': False, 'reasoning_effort': 'none',
                                   'expected_model': 'not-portable', 'route_revision': 'old'},
            'database': '/do-not-copy', 'project_id': 'do-not-copy', 'api_key': 'do-not-copy',
        },
    }
    (state / 'template.json').write_text(json.dumps(template))
    settings = dict(enabled=True, template_config=str(state / 'template.json'),
        registry=str(state / 'projects.json'), enabled_since='2026-10-08T00:00:00.000Z',
        transcript_roots=[str(tmp_path / 'sessions')])
    return state, template, settings


def test_existing_project_inherits_without_writes_network_or_database(shared, tmp_path, monkeypatch):
    state, template, _ = shared
    path = init_project(tmp_path / 'existing')
    before = path.read_bytes()
    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: pytest.fail('database opened'))
    monkeypatch.setattr(socket, 'socket', lambda *a, **kw: pytest.fail('network opened'))
    config = load_config(path)
    assert config['graph_v2'] == graph_template(template)
    assert ingress_enabled(config) and stage_enabled(config) and revision_enabled(config)
    assert resource_settings(config)['max_requests'] == 64
    assert config['provider']['model'] == 'shared-model'
    assert path.read_bytes() == before
    assert not Path(config['database']).exists()
    assert not (state / 'projects.json').exists()


def test_new_hook_project_keeps_pipeline_and_isolation(shared, tmp_path):
    _, template, settings = shared
    root = tmp_path / 'new'
    root.mkdir()
    path = ensure_project(str(root), settings)
    raw = json.loads(path.read_text())
    config = load_config(path)
    assert raw['graph_v2'] == graph_template(template)
    assert config['roots'] == [str(root)]
    assert config['hook']['allowed_transcript_roots'] == settings['transcript_roots']
    assert ingress_enabled(config) and stage_enabled(config)
    assert config['project_id'] != 'do-not-copy'
    assert config['database'] == str(root / '.sessiongraph/index.sqlite3')
    assert 'do-not-copy' not in path.read_text()
    assert 'expected_model' not in config['graph_v2']['single_token_judge']


@pytest.mark.parametrize('section,key', [
    ('hook', 'enabled'), ('hook', 'durable_ingress'), ('graph_v2', 'enabled'),
    ('condensation', 'enabled'), ('recovery_policy', 'enabled'),
    ('resource_policy', 'enabled'), ('stage_policy', 'enabled'), ('revision_policy', 'enabled'),
])
def test_explicit_opt_out_preserved_and_dependencies_stay_valid(shared, tmp_path, section, key):
    path = init_project(tmp_path / 'off')
    raw = json.loads(path.read_text())
    target = raw.setdefault(section, {}) if section in ('hook', 'graph_v2') else (
        raw.setdefault('graph_v2', {}).setdefault(section, {}))
    target[key] = False
    path.write_text(json.dumps(raw))
    before = path.read_bytes()
    config = load_config(path)
    target = config[section] if section in ('hook', 'graph_v2') else config['graph_v2'][section]
    assert target[key] is False
    # Validators must accept inherited defaults even when a prerequisite is off.
    ingress_enabled(config)
    resource_settings(config)
    stage_enabled(config)
    revision_enabled(config)
    assert path.read_bytes() == before


def test_local_budget_wins_and_template_not_mutated(shared, tmp_path):
    _, template, _ = shared
    before = deepcopy(template)
    portable = graph_template(template)
    portable['resource_policy']['max_requests'] = 2
    assert template == before
    path = init_project(tmp_path / 'limited')
    raw = json.loads(path.read_text())
    raw['graph_v2'] = {'resource_policy': {'max_requests': 7}}
    path.write_text(json.dumps(raw))
    assert resource_settings(load_config(path))['max_requests'] == 7


def test_reinstall_keeps_global_pipeline_not_selected_projects_opt_out(shared, tmp_path):
    from sessiongraph.gateway_install import install
    state, template, _ = shared
    home = tmp_path / 'codex'
    home.mkdir()
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'entrypoint.py').write_text('')
    path = init_project(tmp_path / 'project')
    raw = json.loads(path.read_text())
    raw['graph_v2'] = {'enabled': False}
    path.write_text(json.dumps(raw))
    install(codex_home=home, state=state, app=tmp_path / 'app', python=Path(sys.executable),
            legacy=tmp_path / 'legacy', project_config=path, source_root=source,
            install_hooks=False, install_mcp=False)
    installed = json.loads((state / 'template.json').read_text())
    assert installed['graph_v2'] == graph_template(template)
    assert installed['hook'] == {'durable_ingress': True}
    assert load_config(path)['graph_v2']['enabled'] is False


def test_no_template_retains_standalone_behavior(tmp_path, monkeypatch):
    monkeypatch.setenv('SESSIONGRAPH_RUNTIME_HOME', str(tmp_path / 'absent'))
    path = init_project(tmp_path / 'standalone')
    config = load_config(path)
    assert 'graph_v2' not in config
    assert not ingress_enabled(config)


@pytest.mark.parametrize('graph', [None, {'recovery_policy': None}, {'resource_policy': False}])
def test_malformed_local_options_fail_explicitly(shared, tmp_path, graph):
    path = init_project(tmp_path / 'invalid')
    raw = json.loads(path.read_text())
    raw['graph_v2'] = graph
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match='must be an object'):
        load_config(path)


def test_stale_compaction_settings_are_not_inherited_or_effective(shared,tmp_path):
    _,template,_=shared
    assert 'auto_compaction' not in graph_template(template)
    path=init_project(tmp_path/'legacy')
    raw=json.loads(path.read_text())
    raw['graph_v2']={'auto_compaction':{'enabled':True}}
    path.write_text(json.dumps(raw))
    before=path.read_bytes()
    config=load_config(path)
    assert 'auto_compaction' not in config['graph_v2']
    assert config['graph_v2']['condensation']['enabled'] is True
    assert path.read_bytes()==before
