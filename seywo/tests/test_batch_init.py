"""Explicit multi-root setup previews all writes before any confirmation."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from seywo.cli import main
from seywo.config import initialize, load_config


def roots(tmp_path, *names):
    result = [tmp_path / name for name in names]
    for root in result:
        root.mkdir()
    return result


def planned_project(preview, root):
    data = json.loads(preview)
    return next(project for project in data['projects']
                if project['project_root'] == str(root))


def test_preview_and_cancel_never_write_and_do_not_discover_projects(tmp_path, monkeypatch, capsys):
    first, second, parent = roots(tmp_path, 'first', 'second', 'parent')
    child = parent / 'hidden child'
    child.mkdir()
    def no_scan(*args, **kwargs):
        pytest.fail('batch setup must not enumerate directories')
    monkeypatch.setattr(Path, 'glob', no_scan)
    monkeypatch.setattr(Path, 'rglob', no_scan)
    monkeypatch.setattr(Path, 'iterdir', no_scan)
    assert main(['init-batch', str(first), str(second), '--dry-run']) == 0
    preview = capsys.readouterr().out
    for root in (first, second):
        plan = planned_project(preview, root)
        for name in ('.gitignore', 'config.json', 'index.sqlite3', '.init.lock', 'storage-migration.lock'):
            assert str(root / '.seywo' / name) in plan['create']
        assert not (root / '.seywo').exists()
    assert 'modify' in preview
    monkeypatch.setattr('builtins.input', lambda _: 'no')
    assert main(['init-batch', str(first), str(second)]) == 0
    assert not (first / '.seywo').exists()
    assert not (second / '.seywo').exists()
    assert not (child / '.seywo').exists()


def test_confirmation_happens_after_preview_then_initializes_each_project(tmp_path, monkeypatch, capsys):
    first, second = roots(tmp_path, 'first project', 'second project')
    def confirm(_):
        preview = capsys.readouterr().out
        assert str(first / '.seywo/config.json') in planned_project(preview, first)['create']
        assert str(second / '.seywo/index.sqlite3') in planned_project(preview, second)['create']
        assert not (first / '.seywo').exists()
        assert not (second / '.seywo').exists()
        return 'yes'
    def no_model(*args, **kwargs):
        pytest.fail('batch setup must not call models or import history')
    monkeypatch.setattr('builtins.input', confirm)
    monkeypatch.setattr('seywo.store.Store.import_source', no_model)
    assert main(['init-batch', str(first), str(second)]) == 0
    for root in (first, second):
        config = load_config(root / '.seywo/config.json')
        assert config['hook']['enabled'] is True
        assert 'model_runtime' not in config
        assert Path(config['_database']).is_file()
    assert 'initialized' in capsys.readouterr().out


def test_existing_config_is_skipped_verbatim_even_if_invalid(tmp_path, monkeypatch, capsys):
    existing, malformed, fresh = roots(tmp_path, 'existing', 'malformed', 'fresh')
    initialize(existing)
    config = existing / '.seywo/config.json'
    data = json.loads(config.read_text())
    data.update(hook={'enabled': False}, provider={'model': 'manual-model'})
    config.write_text(json.dumps(data))
    before = {p.name: p.read_bytes() for p in config.parent.iterdir() if p.is_file()}
    (malformed / '.seywo').mkdir()
    (malformed / '.seywo/config.json').write_text('{broken')
    monkeypatch.setattr('builtins.input', lambda _: 'yes')
    assert main(['init-batch', str(existing), str(malformed), str(fresh), str(fresh)]) == 0
    for name, content in before.items():
        assert (config.parent / name).read_bytes() == content
    assert (malformed / '.seywo/config.json').read_text() == '{broken'
    output = capsys.readouterr().out
    assert 'skipped' in output
    assert output.count('"status": "initialized"') == 1
    assert (fresh / '.seywo/config.json').exists()
    monkeypatch.setattr('builtins.input', lambda _: pytest.fail('all skipped must not ask for confirmation'))
    assert main(['init-batch', str(existing), str(fresh)]) == 0


def test_one_project_failure_does_not_block_later_projects(tmp_path, monkeypatch, capsys):
    first, bad, last = roots(tmp_path, 'first', 'bad', 'last')
    from seywo.store import Store
    original = Store.__init__
    def fail_one(self, config, **kwargs):
        if config['project_root'] == str(bad):
            raise PermissionError('index database is read-only')
        return original(self, config, **kwargs)
    monkeypatch.setattr(Store, '__init__', fail_one)
    monkeypatch.setattr('builtins.input', lambda _: 'yes')
    assert main(['init-batch', str(first), str(bad), str(last)]) == 1
    assert (first / '.seywo/index.sqlite3').is_file()
    assert (last / '.seywo/index.sqlite3').is_file()
    assert 'index database is read-only' in capsys.readouterr().out


def test_unsafe_or_unspecified_roots_do_not_write_or_block_good_project(tmp_path, monkeypatch, capsys):
    outside, good = roots(tmp_path, 'outside', 'good')
    link = tmp_path / 'link'
    link.symlink_to(outside, target_is_directory=True)
    missing = tmp_path / 'missing'
    state_link, config_link, orphan = roots(tmp_path, 'state-link', 'config-link', 'orphan')
    (state_link / '.seywo').symlink_to(outside, target_is_directory=True)
    (config_link / '.seywo').mkdir()
    (config_link / '.seywo/config.json').symlink_to(outside / 'missing')
    (orphan / '.seywo').mkdir()
    (orphan / '.seywo/index.sqlite3').write_bytes(b'preserve database')
    monkeypatch.setattr('builtins.input', lambda _: 'yes')
    args = [link, state_link, config_link, orphan, missing, 'relative-parent', good]
    assert main(['init-batch', *map(str, args)]) == 1
    assert (good / '.seywo/index.sqlite3').exists()
    assert list(outside.iterdir()) == []
    assert not missing.exists()
    assert not (orphan / '.seywo/config.json').exists()
    assert (orphan / '.seywo/index.sqlite3').read_bytes() == b'preserve database'
    assert 'symlink' in capsys.readouterr().out


def test_config_created_during_confirmation_is_skipped_without_rewriting(tmp_path, monkeypatch):
    root, = roots(tmp_path, 'project')
    captured = {}
    def confirm(_):
        initialize(root)
        config = root / '.seywo/config.json'
        captured.update({p.name: p.read_bytes() for p in config.parent.iterdir() if p.is_file()})
        return 'yes'
    monkeypatch.setattr('builtins.input', confirm)
    assert main(['init-batch', str(root)]) == 0
    for name, content in captured.items():
        assert (root / '.seywo' / name).read_bytes() == content


def test_existing_custom_files_are_preserved(tmp_path, monkeypatch, capsys):
    root, = roots(tmp_path, 'project')
    state = root / '.seywo'
    state.mkdir()
    custom = state / '.gitignore'
    custom.write_text('!manual.md\n')
    (state / 'manual.md').write_text('keep me')
    def confirm(_):
        output = capsys.readouterr().out
        assert str(custom) not in output
        assert '"modify": []' in output
        return 'yes'
    monkeypatch.setattr('builtins.input', confirm)
    assert main(['init-batch', str(root)]) == 0
    assert custom.read_text() == '!manual.md\n'
    assert (state / 'manual.md').read_text() == 'keep me'


def test_cli_eof_cancels_and_explicit_yes_runs(tmp_path):
    first, second = roots(tmp_path, 'first', 'second')
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    args = [sys.executable, '-m', 'seywo', 'init-batch', str(first), str(second)]
    cancelled = subprocess.run(args, input='', capture_output=True, text=True, env=env, timeout=20)
    assert cancelled.returncode == 0, cancelled.stderr
    assert 'cancelled' in cancelled.stdout
    assert not (first / '.seywo').exists()
    initialized = subprocess.run(args, input='yes\n', capture_output=True, text=True, env=env, timeout=20)
    assert initialized.returncode == 0, initialized.stderr
    assert (second / '.seywo/config.json').is_file()


def test_skip_existing_is_rechecked_inside_initializer_before_database_access(tmp_path, monkeypatch):
    import seywo.config as config_module
    root, = roots(tmp_path, 'project')
    (root / '.seywo').mkdir()
    config = root / '.seywo/config.json'
    config.write_text('{manually configured, do not parse or repair}')
    def forbidden(*args, **kwargs):
        pytest.fail('skip-existing must never open a database')
    monkeypatch.setattr('seywo.store.Store.__init__', forbidden)
    assert config_module.initialize(root, plugin_hook=True, skip_existing=True)['skipped'] is True
    assert config.read_text() == '{manually configured, do not parse or repair}'
    assert sorted(p.name for p in config.parent.iterdir()) == ['config.json']


def test_config_racing_with_initializer_is_skipped_under_lock(tmp_path, monkeypatch):
    import seywo.config as config_module
    root, = roots(tmp_path, 'project')
    original = config_module._initialize_locked
    contents = '{created by another initializer, do not touch}'
    def race(root, state, **kwargs):
        (state / 'config.json').write_text(contents)
        return original(root, state, **kwargs)
    monkeypatch.setattr(config_module, '_initialize_locked', race)
    assert config_module.initialize(root, plugin_hook=True, skip_existing=True)['skipped'] is True
    assert (root / '.seywo/config.json').read_text() == contents
    assert not (root / '.seywo/index.sqlite3').exists()


def test_batch_requires_at_least_one_explicit_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(['init-batch', '--dry-run'])
    assert exc.value.code == 2
    assert not (tmp_path / '.seywo').exists()


def test_changes_after_preview_cannot_add_unreviewed_files(tmp_path, monkeypatch, capsys):
    root, = roots(tmp_path, 'project')
    state = root / '.seywo'
    state.mkdir()
    custom = state / '.gitignore'
    custom.write_text('!manual.md\n')
    def confirm(_):
        custom.unlink()
        return 'yes'
    monkeypatch.setattr('builtins.input', confirm)
    assert main(['init-batch', str(root)]) == 1
    assert 'changed after preview' in capsys.readouterr().out
    assert not (state / 'config.json').exists()
    assert not custom.exists()


def test_symlink_loop_in_one_root_does_not_abort_other_projects(tmp_path, monkeypatch):
    good, = roots(tmp_path, 'good')
    loop = tmp_path / 'loop'
    loop.symlink_to(loop)
    monkeypatch.setattr('builtins.input', lambda _: 'yes')
    assert main(['init-batch', str(loop / 'child'), str(good)]) == 1
    assert (good / '.seywo/index.sqlite3').is_file()
