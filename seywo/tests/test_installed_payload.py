"""Installed acceptance must detect stale payloads even at the same version."""
import importlib.util
from pathlib import Path
import shutil

import pytest


def verifier(monkeypatch):
    scripts = Path(__file__).resolve().parents[2] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('installed_verifier', scripts / 'verify-installed-seywo-plugin.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def payload(tmp_path):
    repository = tmp_path / 'repository'
    source = repository / 'seywo'
    for name, text in {
        '.codex-plugin/plugin.json': '{"name":"seywo","version":"0.5.2"}',
        'src/seywo/settings.py': 'user level',
        'src/seywo/__init__.py': '__version__="0.5.2"',
        'ui/panel.html': 'global settings UI',
        'bin/seywo-launch.py': 'launcher',
        'runtime/requirements.lock': 'pinned hashed dependencies',
        'skills/history/SKILL.md': 'history',
        'pyproject.toml': 'version="0.5.2"', 'hooks.json': '{}', 'mcp.json': '{}',
    }.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    installed = tmp_path / 'installed'
    shutil.copytree(source, installed)
    return repository, installed


def test_installed_payload_matches_current_source(tmp_path, monkeypatch):
    repository, installed = payload(tmp_path)
    checked = verifier(monkeypatch).verify_installed_payload(repository, installed)
    assert checked == 10


@pytest.mark.parametrize('name', ['src/seywo/settings.py', 'ui/panel.html', 'bin/seywo-launch.py', 'runtime/requirements.lock'])
def test_same_version_with_old_code_is_rejected(tmp_path, monkeypatch, name):
    repository, installed = payload(tmp_path)
    (installed / name).write_text('old version of this file')
    with pytest.raises(AssertionError, match='installed_payload_mismatch'):
        verifier(monkeypatch).verify_installed_payload(repository, installed)


def test_missing_installed_file_is_rejected(tmp_path, monkeypatch):
    repository, installed = payload(tmp_path)
    (installed / 'ui/panel.html').unlink()
    with pytest.raises(AssertionError, match='installed_payload_mismatch'):
        verifier(monkeypatch).verify_installed_payload(repository, installed)
