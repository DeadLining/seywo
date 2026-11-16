"""Native Codex execution is isolated, bounded and reuses the Compact contract."""
import json
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
    assert saved['model_runtime'] == {'backend': 'codex'}
    assert saved['provider'] == data['provider']
    assert read_provider(saved)['backend'] == 'codex'


def fake_cli(tmp_path, body):
    script = tmp_path / 'fake-codex.py'
    script.write_text('import json,sys,time\nfrom pathlib import Path\n' + body)
    return [sys.executable, str(script)]


def test_codex_exec_uses_stdin_schema_and_disables_hooks_and_tools(tmp_path):
    from seywo.codex_transport import CodexTransport
    cli = fake_cli(tmp_path, '''
args=sys.argv[1:]
prompt=sys.stdin.read()
assert '--ephemeral' in args and '--output-schema' in args
assert '--sandbox' in args and args[args.index('--sandbox')+1]=='read-only'
assert 'hooks' in args and 'plugins' in args and 'shell_tool' in args
assert 'mcp_servers.test.enabled=false' in args
assert 'compact-fixture' in prompt and 'toolSecret' not in prompt
schema=json.loads(Path(args[args.index('--output-schema')+1]).read_text())
assert 'segments' in schema['properties']
Path(args[args.index('--output-last-message')+1]).write_text(json.dumps({'segments': [], 'changes': [], 'focus_segment': None}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':120,'cached_input_tokens':40,'output_tokens':30}}))
''')
    home = tmp_path / 'codex-home'
    home.mkdir()
    (home / 'config.toml').write_text('[mcp_servers.test]\ncommand="test"\n')
    result = CodexTransport(command=cli, home=home).complete_compact(
        model='codex-default', instructions='compact-fixture', payload={'turns': []}, max_output_tokens=768)
    assert result['finish'] == 'complete', result
    assert result['usage'] == {'input_tokens': 120, 'output_tokens': 30}
    assert result['output']['changes'] == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ['codex-home', 'fake-codex.py']


@pytest.mark.parametrize('body,error', [
    ('print("provider secret-token",file=sys.stderr);sys.exit(1)', 'codex_execution_failed'),
    ("Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('invalid')", 'invalid_json'),
    ('time.sleep(10)', 'codex_timeout'),
])
def test_codex_failure_does_not_publish_output_or_echo_process_text(tmp_path, body, error):
    from seywo.codex_transport import CodexTransport
    result = CodexTransport(command=fake_cli(tmp_path, body), timeout=0.1).complete_compact(
        model='codex-default', instructions='test', payload={}, max_output_tokens=768)
    assert result['error'] == error
    assert result['output'] is None
    assert 'secret-token' not in json.dumps(result)


def test_maintainer_routes_compact_through_codex_without_provider_credentials(tmp_path, monkeypatch):
    from seywo import inbox, maintain
    from test_compact_maintain import CompactTransport, make_source
    initialize(tmp_path)
    path = tmp_path / '.seywo/config.json'
    enable_plugin_hook(path)
    config = load_config(path)
    fake = CompactTransport()
    fake.model = 'codex-default'
    monkeypatch.setattr(maintain, 'CodexTransport', lambda **kwargs: fake)
    monkeypatch.setattr(maintain, 'load_key', lambda endpoint: pytest.fail('API credentials must not be loaded'))
    chat = 'native-compact-fixture'
    inbox.record(config, chat, 'PreCompact', str(make_source(tmp_path, chat)))
    report = maintain.run_chat(config, chat)
    assert report['state'] == 'done', report
    assert report['calls'] == 1
    assert len(fake.payloads[0]['turns']) == 2


def test_native_model_is_frozen_and_auditable_without_duplicate_schema_prompt(tmp_path):
    from seywo.codex_transport import CodexTransport
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.toml').write_text('model="configured-model"\nmodel_provider="fixture-provider"\n')
    cli = fake_cli(tmp_path, '''
args=sys.argv[1:]
assert args[args.index('--model')+1]=='configured-model'
assert 'model_provider="fixture-provider"' in args
prompt=sys.stdin.read()
assert 'additionalProperties' not in prompt
Path(args[args.index('--output-last-message')+1]).write_text(json.dumps({'segments': [], 'changes': [], 'focus_segment': None, 'current': {'goal_indexes': [], 'question_indexes': [], 'next_step_indexes': []}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':120,'cached_input_tokens':40,'output_tokens':30}}))
''')
    result = CodexTransport(command=cli, home=home).complete_compact(
        model='codex-default', instructions='rules', payload={'turns': []})
    assert result['error'] is None, result
    assert result['model_info'] == {'backend': 'codex', 'requested_model': 'codex-default',
        'selected_model': 'configured-model', 'provider': 'fixture-provider', 'selection': 'config'}
    assert result['cached_input_tokens'] == 40


def test_model_subprocess_excludes_skill_catalog_without_editing_user_configuration(tmp_path):
    from seywo.codex_transport import CodexTransport
    home = tmp_path / 'home'
    skill = home / 'skills/private-skill/SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('private skill')
    config = home / 'config.toml'
    config.write_text('model="fixture"\n')
    cli = fake_cli(tmp_path, '''
args=sys.argv[1:]
overrides=[args[i+1] for i,a in enumerate(args[:-1]) if a=='-c']
skills=next(v for v in overrides if v.startswith('skills.config='))
assert 'private-skill/SKILL.md' in skills and 'enabled=false' in skills
assert 'view_image' in args and 'image_generation' in args
Path(args[args.index('--output-last-message')+1]).write_text('{}')
''')
    result = CodexTransport(command=cli, home=home)._execute('rules', {}, {'type': 'object'})
    assert result['error'] is None, result
    assert config.read_text() == 'model="fixture"\n'
    assert skill.read_text() == 'private skill'
