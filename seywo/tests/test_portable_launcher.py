"""One marketplace entry for every OS; Hooks reuse an offline prepared runtime."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_mcp_and_hooks_share_host_runtime_without_uv():
    server = json.loads((ROOT / 'mcp.json').read_text())['mcpServers']['seywo']
    assert server['command'] == './bin/seywo-host'
    assert server['args'] == ['--mcp']
    assert server['cwd'] == '.'
    assert (ROOT / 'bin/seywo-host.cmd').is_file()
    hooks = json.loads((ROOT / 'hooks.json').read_text())['hooks']
    for entries in hooks.values():
        command = entries[0]['hooks'][0]['command']
        assert command == '"${PLUGIN_ROOT}/bin/seywo-host" --hook'
        assert 'uv ' not in command


def test_launcher_hook_is_relocatable_and_skips_recursive_model_work(tmp_path):
    import shutil
    installed = tmp_path / '中文 plugin cache'
    shutil.copytree(ROOT, installed, ignore=shutil.ignore_patterns('__pycache__', '.venv', 'node_modules'))
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    result = subprocess.run([sys.executable, '-B', str(installed / 'bin/seywo-launch.py'), '--hook'],
        cwd=workspace, input='{}', text=True, capture_output=True, timeout=15,
        env={**os.environ, 'SEYWO_INTERNAL_MODEL': '1'})
    assert result.returncode == 0, result.stderr
    assert result.stdout == '' and not (workspace / '.seywo').exists()


def test_cli_uses_utf8_for_non_ascii_project_paths(tmp_path):
    project = tmp_path / '中文 project'
    result = subprocess.run([sys.executable, '-B', str(ROOT / 'bin/seywo-launch.py'),
                             '--cli', 'init', str(project)],
        capture_output=True, encoding='utf-8', timeout=15,
        env={**os.environ, 'PYTHONUTF8': '0', 'PYTHONIOENCODING': 'ascii'})
    assert result.returncode == 0, result.stderr
    assert (project / '.seywo/config.json').is_file()
