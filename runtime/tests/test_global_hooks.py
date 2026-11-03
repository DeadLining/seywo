"""Global registration and project routing, without provider calls."""
import importlib.util
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from sessiongraph.core import Store, init_project, load_config
from sessiongraph.global_hooks import ensure_project, merge_hooks, remove_local_hooks


@pytest.fixture
def settings(tmp_path):
    central = tmp_path / 'central'
    cfg = init_project(central)
    raw = json.loads(cfg.read_text())
    raw['provider'].update(endpoint='http://127.0.0.1:4317/openai/v1/responses',
                           model='deepseek-v4.1-flash', protocol='responses', auth='bearer')
    cfg.write_text(json.dumps(raw))
    return {'enabled': True, 'template_config': str(cfg),
            'registry': str(central / '.sessiongraph/ui-projects.json'),
            'enabled_since': '2026-10-08T00:00:00.000Z',
            'transcript_roots': [str(tmp_path / 'sessions')]}


def test_new_projects_isolated_and_registry_merged(tmp_path, settings):
    roots = [tmp_path / name for name in ('alpha', 'beta')]
    for root in roots:
        (root / '.git').mkdir(parents=True)
        (root / 'src').mkdir()
    with ThreadPoolExecutor(max_workers=2) as pool:
        paths = list(pool.map(lambda r: ensure_project(str(r / 'src'), settings), roots))
    for root, path in zip(roots, paths):
        assert path == root / '.sessiongraph/config.json'
        cfg = load_config(path)
        assert cfg['roots'] == [str(root.resolve())]
        assert cfg['database'] == str(root / '.sessiongraph/index.sqlite3')
        assert cfg['provider']['model'] == 'deepseek-v4.1-flash'
        assert cfg['hook']['automatic_since'] == settings['enabled_since']
        assert cfg['hook']['allowed_transcript_roots'] == settings['transcript_roots']
        with_store = Store(cfg, readonly=True)
        assert with_store.resume()['memories'] == []
        from sessiongraph import source_text, run_receipts
        assert source_text.enabled(with_store) and source_text.unified(with_store)
        assert run_receipts.enabled(with_store.db)
        with_store.close()
    registry = json.loads(Path(settings['registry']).read_text())
    assert {p['root'] for p in registry['projects']} == {str(r) for r in roots}
    old = paths[0].read_bytes()
    assert ensure_project(str(roots[0]), settings) == paths[0]
    assert paths[0].read_bytes() == old


def test_existing_project_preserved_and_nested_git_not_parent(tmp_path, settings):
    parent = tmp_path / 'parent'
    existing = init_project(parent)
    before = existing.read_bytes()
    sub = parent / 'child'
    (sub / '.git').mkdir(parents=True)
    assert ensure_project(str(sub), settings) == sub / '.sessiongraph/config.json'
    assert ensure_project(str(parent), settings) == existing
    assert existing.read_bytes() == before


def test_existing_and_new_projects_inherit_global_cleanup(tmp_path, settings, monkeypatch):
    from sessiongraph.chat_cleanup import enabled
    settings = {**settings, 'cleanup_missing_chats': True}
    home = tmp_path / 'codex'
    home.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(home))
    (home / 'sessiongraph.json').write_text(json.dumps(settings))
    existing = init_project(tmp_path / 'existing')
    before = existing.read_bytes()
    new_root = tmp_path / 'new'
    new_root.mkdir()
    for root in (existing.parent.parent, new_root):
        path = ensure_project(str(root), settings)
        config = load_config(path)
        assert 'cleanup_missing_chats' not in config['hook']
        assert enabled(config) is True
    assert existing.read_bytes() == before


def test_disabled_and_invalid_cwd_do_not_create(tmp_path, settings):
    root = tmp_path / 'off'
    (root / '.sessiongraph').mkdir(parents=True)
    (root / '.sessiongraph/disabled').touch()
    for cwd in (str(root), '/', str(Path.home()), '', 'relative', str(tmp_path / 'missing')):
        assert ensure_project(cwd, settings) is None
    assert not (root / '.sessiongraph/config.json').exists()
    assert ensure_project(str(tmp_path), {**settings, 'enabled': False}) is None


def test_same_project_concurrent_init_and_config_disable(tmp_path, settings):
    root = tmp_path / 'shared'
    root.mkdir()
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(lambda _: ensure_project(str(root), settings), range(4)))
    assert len(set(paths)) == 1
    data = json.loads(paths[0].read_text())
    data['hook']['enabled'] = False
    paths[0].write_text(json.dumps(data))
    assert ensure_project(str(root), settings) is None


def test_registration_preserves_other_hooks_and_is_idempotent():
    original = {'extra': 3, 'hooks': {'SessionStart': [{'hooks': [
        {'type': 'command', 'command': '/promlight SessionStart'}]}], 'Stop': []}}
    merged = merge_hooks(original, '/sg/hook.py')
    assert original['hooks']['SessionStart'] == merged['hooks']['SessionStart'][:1]
    assert merged['extra'] == 3
    assert merged == merge_hooks(merged, '/sg/hook.py')
    assert len(merged['hooks']['SessionStart']) == 2
    assert merged['hooks']['Stop'] == [{'hooks': [{'type': 'command', 'command': '/sg/hook.py'}]}]
    assert set(merged['hooks']) == {'SessionStart', 'UserPromptSubmit', 'PreCompact', 'PostCompact', 'Stop', 'SessionEnd'}


def test_remove_local_hooks_preserves_unrelated_config():
    text = '[mcp_servers.test]\ncommand = "keep"\n\n[[hooks.PreCompact]]\n[[hooks.PreCompact.hooks]]\ntype = "command"\ncommand = "/sg/hook.py"\n\n[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\ncommand = "other"\n'
    new = remove_local_hooks(text, '/sg/hook.py')
    assert '"/sg/hook.py"' not in new
    assert '[mcp_servers.test]' in new and 'command = "other"' in new
    assert remove_local_hooks(new, '/sg/hook.py') == new


def test_stop_does_not_initialize_unenrolled_project(tmp_path, settings, monkeypatch):
    hook = hook_module()
    root = tmp_path / 'unenrolled'
    root.mkdir()
    global_settings = tmp_path / 'global.json'
    global_settings.write_text(json.dumps(settings))
    monkeypatch.setattr(hook, 'GLOBAL_SETTINGS', global_settings)
    monkeypatch.setattr(hook, 'ensure_project', lambda *_: pytest.fail('Stop must not enroll'))
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({
        'hook_event_name': 'Stop', 'cwd': str(root), 'session_id': 'chat'})))
    assert hook.main() == 0
    assert not (root / '.sessiongraph').exists()


def hook_module():
    path = Path(__file__).resolve().parents[1] / 'hooks/sg_codex_hook.py'
    spec = importlib.util.spec_from_file_location('sg_hook_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_restore_flags_are_project_scoped(tmp_path):
    hook = hook_module()
    a = init_project(tmp_path / 'a')
    b = init_project(tmp_path / 'b')
    hook._mark_needs_restore(a, 'same-chat')
    assert hook._pending_restore(b, 'same-chat') is None
    token = hook._pending_restore(a, 'same-chat')
    assert token and hook._pending_restore(a, 'same-chat') == token
    assert hook._ack_restore(a, 'same-chat', token)
    assert hook._pending_restore(a, 'same-chat') is None


def test_hook_routes_initialization_spawn_and_restore(tmp_path, settings, monkeypatch):
    hook = hook_module()
    root = tmp_path / 'new'
    root.mkdir()
    settings_path = tmp_path / 'global.json'
    settings_path.write_text(json.dumps(settings))
    monkeypatch.setattr(hook, 'GLOBAL_SETTINGS', settings_path)
    spawned = []
    monkeypatch.setattr(hook, '_spawn_extraction', lambda *args: spawned.append(args))
    monkeypatch.setattr(hook, '_extract_context', lambda c: 'context:' + str(c))
    output = io.StringIO()
    monkeypatch.setattr('sys.stdout', output)
    for event in ('SessionStart', 'PreCompact', 'PostCompact', 'UserPromptSubmit'):
        monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({
            'hook_event_name': event, 'cwd': str(root), 'session_id': 'chat'})))
        assert hook.main() == 0
    config = root / '.sessiongraph/config.json'
    assert [s[0] for s in spawned] == [config, config]
    assert len(output.getvalue().splitlines()) == 2
    assert all(str(config) in json.loads(line)['hookSpecificOutput']['additionalContext']
               for line in output.getvalue().splitlines())


def test_automatic_since_passed_only_to_automatic_worker(tmp_path):
    from sessiongraph.auto_update import run_update
    path = init_project(tmp_path)
    cfg = json.loads(path.read_text())
    cfg['hook']['automatic_since'] = '2026-10-08T01:02:03.000Z'
    path.write_text(json.dumps(cfg))
    Store(load_config(path)).close()
    calls = []
    def execute(argv, timeout):
        calls.append(argv)
        return {'added_entries': 0, 'skipped': []} if 'refresh-chat' in argv else {'saved': [], 'failed': [], 'model_calls': 0}
    for event in ('PreCompact', 'PostCompact', 'manual'):
        result = run_update(path, 'chat', event, execute=execute)
        if event == 'PreCompact':
            assert result['state'] == 'prepared' and 'refresh-chat' in calls[-1]
            continue
        assert result['state'] == 'succeeded'
        worker = calls[-1]
        assert ('--memories-since' in worker) == (event != 'manual')
        if event != 'manual':
            assert worker[worker.index('--memories-since') + 1] == cfg['hook']['automatic_since']


def test_installer_backups_idempotence_and_keeps_trust(tmp_path, settings):
    from sessiongraph.global_hooks import install
    home = tmp_path / 'codex'
    home.mkdir()
    prom = {'hooks': {'PreCompact': [{'hooks': [{'type': 'command', 'command': 'PromLight'}]}]}}
    (home / 'hooks.json').write_text(json.dumps(prom))
    (home / 'config.toml').write_text('[hooks.state.keep]\ntrusted_hash="do-not-touch"\n')
    local = tmp_path / 'local.toml'
    local.write_text('[mcp_servers.keep]\ncommand="keep"\n\n[[hooks.PreCompact]]\n[[hooks.PreCompact.hooks]]\ntype="command"\ncommand="/sg/hook.py"\n')
    result = install(home, local, '/sg/hook.py', settings)
    assert len(result['changed']) == 3
    assert len(result['backups']) == 2
    assert (home / 'config.toml').read_text().endswith('trusted_hash="do-not-touch"\n')
    assert json.loads((home / 'hooks.json').read_text())['hooks']['PreCompact'][0] == prom['hooks']['PreCompact'][0]
    assert 'hooks.PreCompact' not in local.read_text()
    assert install(home, local, '/sg/hook.py', settings)['changed'] == []
    assert json.loads((home / 'sessiongraph.json').read_text())['enabled_since'] == settings['enabled_since']
    path = home / 'sessiongraph.json'
    current = json.loads(path.read_text())
    current['cleanup_missing_chats'] = True
    path.write_text(json.dumps(current))
    install(home, local, '/sg/hook.py', settings)
    assert json.loads(path.read_text())['cleanup_missing_chats'] is True
    assert install(home, local, '/sg/hook.py', settings)['changed'] == []


def test_failed_initialization_does_not_publish_default_config(tmp_path, settings, monkeypatch):
    import sessiongraph.global_hooks as global_hooks
    root = tmp_path / 'fail'
    root.mkdir()
    write = global_hooks.atomic_write
    def fail_policy(path, text):
        if path.name == 'policy.json':
            raise OSError('disk error')
        write(path, text)
    monkeypatch.setattr(global_hooks, 'atomic_write', fail_policy)
    with pytest.raises(OSError):
        ensure_project(str(root), settings)
    assert not (root / '.sessiongraph/config.json').exists()
