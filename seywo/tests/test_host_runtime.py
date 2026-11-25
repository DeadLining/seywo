"""Host-runtime contract: no shell-PATH Python fallback and no shared writes."""
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

import pytest

ENTRY = Path(__file__).resolve().parents[1] / 'bin' / 'seywo-host.py'


def module():
    spec = importlib.util.spec_from_file_location('seywo_host', ENTRY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fixture_bundle(tmp_path):
    root = tmp_path / 'runtime 中文'
    root.mkdir()
    (root / 'runtime.json').write_text(json.dumps({
        'bundleFormatVersion': 2, 'bundleVersion': 'fixture',
        'targetPlatform': sys.platform, 'targetArch': platform.machine(),
        'pythonVersion': platform.python_version(),
    }))
    destination = root / 'dependencies/python'
    destination.parent.mkdir()
    destination.symlink_to(sys.base_prefix, target_is_directory=True)
    return root


def test_existing_unrelated_python_is_not_a_runtime(tmp_path):
    mod = module()
    with pytest.raises(mod.RuntimeFailure, match='runtime_metadata_missing'):
        mod.validate_runtime(tmp_path)


@pytest.mark.skipif(os.name != 'nt', reason='requires Windows CMD and Windows PowerShell')
@pytest.mark.parametrize('layout', ['', 'python', 'bin'])
def test_windows_discovers_selected_framework_and_propagates_exit(tmp_path, layout):
    root = fixture_bundle(tmp_path)
    if layout:
        python = root / 'dependencies/python'
        python.unlink() if not python.is_dir() else python.rmdir()
        python.mkdir()
        (python / layout).symlink_to(sys.base_prefix, target_is_directory=True)
    profile = tmp_path / 'profile'
    default = profile / '.cache/codex-runtimes/codex-primary-runtime'
    default.mkdir(parents=True)
    (default / 'selected-framework').write_text(str(root), encoding='utf-8')
    env = {**os.environ, 'USERPROFILE': str(profile), 'CODEX_HOME': str(profile / '.codex'),
           'PATH': os.path.join(os.environ['SystemRoot'], 'System32')}
    env.pop('SEYWO_CODEX_RUNTIME', None)
    command = str(ENTRY.with_name('seywo-host.cmd'))
    result = subprocess.run([command, '--doctor'], cwd=tmp_path, env=env,
                            capture_output=True, encoding='utf-8', timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['bundle'] == 'fixture'
    result = subprocess.run([command, '--cli', 'unknown-command'], cwd=tmp_path, env=env,
                            capture_output=True, encoding='utf-8', timeout=15)
    assert result.returncode == 2 and 'invalid choice' in result.stderr
    assert not (profile / '.codex').exists()


def test_cli_arguments_are_opaque_to_bootstrap(tmp_path, monkeypatch, capsys):
    mod = module()
    root = fixture_bundle(tmp_path)
    monkeypatch.setattr(mod, 'prepare_dependencies', lambda: pytest.fail('unexpected SDK preparation'))
    for command in (['search', '--help'], ['mcp', '--help']):
        with pytest.raises(SystemExit) as result:
            mod.main(['--runtime-root', str(root), '--cli', *command])
        assert result.value.code == 0
        assert 'seywo ' + command[0] in capsys.readouterr().out
    # Even an unsuccessful search of an uninitialized project must stay offline.
    assert mod.main(['--runtime-root', str(root), '--cli', '--config', str(tmp_path/'missing'),
                     'search', 'mcp']) == 2


def test_sdk_cache_uses_same_filtered_home_as_settings(tmp_path, monkeypatch):
    mod = module()
    from seywo.plugin_context import catalog_home
    home = tmp_path / 'custom'
    monkeypatch.delenv('CODEX_HOME', raising=False)
    monkeypatch.setenv('HOME', str(tmp_path / 'other'))
    monkeypatch.setenv('USERPROFILE', str(tmp_path / 'other'))
    monkeypatch.setenv('PLUGIN_DATA', str(home / 'plugins/data/agent-plugins/seywo'))
    assert mod.cache_root() == catalog_home() / 'seywo/runtime'
    monkeypatch.delenv('PLUGIN_DATA')
    monkeypatch.setattr(mod, 'PLUGIN', home / 'plugins/cache/market/seywo/1')
    assert mod.cache_root() == home / 'seywo/runtime'


def test_old_cache_cleanup_failure_does_not_block_start_or_other_gc(tmp_path, monkeypatch, capsys):
    mod = module()
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'home'))
    root = mod.cache_root()
    key = mod.dependency_key(mod.LOCK.read_bytes())
    final = root / key
    final.mkdir()
    (final / 'ready.json').write_text(json.dumps({'key': key}))
    monkeypatch.setattr(mod, 'validate_dependencies', lambda p: True)
    blocked, idle = root / ('1' * 32), root / ('2' * 32)
    blocked.mkdir()
    idle.mkdir()
    remove = mod.shutil.rmtree
    def sometimes_busy(path):
        if path == blocked:
            raise PermissionError('busy')
        remove(path)
    monkeypatch.setattr(mod.shutil, 'rmtree', sometimes_busy)
    path, lease = mod.prepare_dependencies()
    lease.close()
    assert path == final and blocked.exists() and not idle.exists()
    assert 'dependencies_cleanup_deferred' in capsys.readouterr().err
    monkeypatch.setattr(mod.shutil, 'rmtree', remove)
    _, lease = mod.prepare_dependencies()
    lease.close()
    assert not blocked.exists()


def test_wrong_platform_metadata_is_rejected(tmp_path):
    root = fixture_bundle(tmp_path)
    data = json.loads((root / 'runtime.json').read_text())
    data['targetPlatform'] = 'wrong-os'
    (root / 'runtime.json').write_text(json.dumps(data))
    mod = module()
    with pytest.raises(mod.RuntimeFailure, match='runtime_platform_mismatch'):
        mod.validate_runtime(root)


def test_valid_bundle_has_sqlite_fts_and_host_identity(tmp_path):
    root = fixture_bundle(tmp_path)
    actual = module().validate_runtime(root)
    assert actual['python'] == platform.python_version()
    assert actual['bundle'] == 'fixture'


def test_dependency_cache_identity_includes_lock_and_abi(tmp_path):
    mod = module()
    assert mod.dependency_key(b'a') != mod.dependency_key(b'b')
    assert '/' not in mod.dependency_key(b'a')


def test_missing_runtime_fails_before_project_write(tmp_path):
    env = {**os.environ, 'SEYWO_CODEX_RUNTIME': str(tmp_path / 'absent')}
    launcher = ENTRY.with_name('seywo-host.cmd' if os.name == 'nt' else 'seywo-host')
    result = subprocess.run([str(launcher), '--hook'], input='{}', env=env, cwd=tmp_path,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode != 0
    assert 'runtime_missing' in result.stderr
    assert not (tmp_path / '.seywo').exists()


def test_dependency_failure_does_not_publish_or_touch_existing(tmp_path, monkeypatch):
    mod = module()
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'home'))
    def fail(*args, **kwargs):
        raise mod.RuntimeFailure('dependencies_prepare_failed')
    monkeypatch.setattr(mod, 'install_dependencies', fail)
    with pytest.raises(mod.RuntimeFailure, match='dependencies_prepare_failed'):
        mod.prepare_dependencies()
    root = tmp_path / 'home/seywo/runtime'
    assert not list(root.glob('*/ready.json'))
    assert not list(root.glob('.staging-*'))


def test_hook_does_not_prepare_dependencies(tmp_path, monkeypatch):
    mod = module()
    root = fixture_bundle(tmp_path)
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('SEYWO_INTERNAL_MODEL', '1')
    monkeypatch.setattr(mod, 'prepare_dependencies', lambda: pytest.fail('hook downloaded dependencies'))
    assert mod.main(['--runtime-root', str(root), '--hook']) == 0
    assert not (tmp_path / 'home').exists()


def test_corrupt_dependencies_rebuilt_and_old_idle_cache_collected(tmp_path, monkeypatch):
    mod = module()
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'home'))
    root = mod.cache_root()
    old = root / ('a' * 32)
    old.mkdir()
    key = mod.dependency_key(mod.LOCK.read_bytes())
    broken = root / key
    broken.mkdir()
    (broken / 'ready.json').write_text(json.dumps({'key': key}))
    monkeypatch.setattr(mod, 'validate_dependencies', lambda p: (p / 'fixture').is_file())
    calls = []
    def install(target, lock, **kwargs):
        calls.append(target)
        (target / 'fixture').write_text('ready')
    monkeypatch.setattr(mod, 'install_dependencies', install)
    path, lease = mod.prepare_dependencies()
    lease.close()
    assert path == broken and (path / 'fixture').exists()
    assert not old.exists()
    again, lease = mod.prepare_dependencies()
    lease.close()
    assert len(calls) == 1 and again == path


def test_in_use_old_dependencies_are_kept(tmp_path, monkeypatch):
    mod = module()
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'home'))
    root = mod.cache_root()
    old = root / ('a' * 32)
    old.mkdir()
    with mod.FileLease(root / (old.name + '.lock')) as active:
        assert active.acquire(exclusive=False)
        mod.collect_unused(root, 'b' * 32)
        assert old.is_dir()
    mod.collect_unused(root, 'b' * 32)
    assert not old.exists()


def test_cache_symlink_is_rejected(tmp_path, monkeypatch):
    mod = module()
    target = tmp_path / 'elsewhere'
    target.mkdir()
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'seywo').symlink_to(target, target_is_directory=True)
    monkeypatch.setenv('CODEX_HOME', str(home))
    with pytest.raises(mod.RuntimeFailure, match='dependencies_unsafe_path'):
        mod.cache_root()
    assert list(target.iterdir()) == []


def test_manifest_rejects_wrong_python_patch_version(tmp_path):
    mod = module()
    root = fixture_bundle(tmp_path)
    data = json.loads((root / 'runtime.json').read_text())
    data['pythonVersion'] = '3.12.0'
    (root / 'runtime.json').write_text(json.dumps(data))
    with pytest.raises(mod.RuntimeFailure, match='runtime_python_incompatible'):
        mod.validate_runtime(root)


@pytest.mark.parametrize('field,value', [('targetArch', []), ('bundleVersion', 3),
                                        ('bundleFormatVersion', 1), ('pythonVersion', None)])
def test_malformed_manifest_has_bounded_diagnostic(tmp_path, field, value):
    mod = module()
    root = fixture_bundle(tmp_path)
    data = json.loads((root / 'runtime.json').read_text())
    data[field] = value
    (root / 'runtime.json').write_text(json.dumps(data))
    with pytest.raises(mod.RuntimeFailure, match='runtime_metadata_missing'):
        mod.validate_runtime(root)


def test_missing_installer_does_not_modify_host(tmp_path, monkeypatch):
    mod = module()
    monkeypatch.setitem(sys.modules, 'ensurepip', None)
    with pytest.raises(mod.RuntimeFailure, match='runtime_installer_unavailable'):
        mod.install_dependencies(tmp_path / 'target', mod.LOCK)
    assert not (tmp_path / 'target').exists()


def test_failed_install_can_retry_and_concurrent_starts_share_one_cache(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    mod = module()
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(mod, 'validate_dependencies', lambda p: (p / 'fixture').is_file())
    calls = []
    def install(target, lock, **kwargs):
        calls.append(target)
        (target / 'partial').write_text('partial')
        if len(calls) == 1:
            raise mod.RuntimeFailure('dependencies_prepare_failed')
        (target / 'fixture').write_text('ready')
    monkeypatch.setattr(mod, 'install_dependencies', install)
    with pytest.raises(mod.RuntimeFailure):
        mod.prepare_dependencies()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: mod.prepare_dependencies(), range(4)))
    try:
        assert len({path for path, lease in results}) == 1
        assert len(calls) == 2
        assert not list(mod.cache_root().glob('.staging-*'))
    finally:
        for _, lease in results:
            lease.close()


def test_hook_starts_isolated_worker_without_sdk_or_model(tmp_path):
    import time
    from seywo.config import initialize
    root = fixture_bundle(tmp_path)
    project = tmp_path / 'project 中文'
    config = Path(initialize(project)['config'])
    data = json.loads(config.read_text())
    data['hook'] = {'enabled': True}
    config.write_text(json.dumps(data))
    source = tmp_path / 'source.jsonl'
    source.write_text('\n'.join(json.dumps(row) for row in [
        {'type': 'session_meta', 'payload': {'id': 'cold-hook', 'cwd': str(project)}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
          'content': [{'type': 'input_text', 'text': '等待回答的用户目标'}]}},
    ]) + '\n')
    home = tmp_path / 'codex home'
    env = {**os.environ, 'SEYWO_CODEX_RUNTIME': str(root), 'CODEX_HOME': str(home),
           'PYTHONHOME': str(tmp_path / 'invalid-python'),
           'PYTHONPATH': str(tmp_path / 'invalid-packages')}
    env.pop('SEYWO_INTERNAL_MODEL', None)
    env['PATH'] = os.path.join(os.environ['SystemRoot'], 'System32') if os.name == 'nt' else '/usr/bin:/bin'
    command = ENTRY.with_name('seywo-host.cmd' if os.name == 'nt' else 'seywo-host')
    result = subprocess.run([str(command), '--hook'], input=json.dumps({
        'hook_event_name': 'PreCompact', 'session_id': 'cold-hook', 'cwd': str(project),
        'transcript_path': str(source)}), env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    receipt = config.parent / 'maintenance/cold-hook.json'
    state = {}
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if receipt.exists():
            state = json.loads(receipt.read_text())
            if state.get('state') not in {'queued', 'running'}:
                break
        time.sleep(.05)
    assert state.get('error_code') == 'awaiting_turn_complete', state
    assert state['calls'] == 0
    assert not (home / 'seywo/runtime').exists()
