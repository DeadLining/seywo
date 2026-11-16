"""The plugin package owns lifecycle triggering; the compact engine remains shared."""
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_plugin_declares_compact_lifecycle_hooks_in_package():
    hooks_path = ROOT / "hooks.json"
    assert hooks_path.is_file()
    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    hooks = data["hooks"]
    expected = {"SessionStart", "UserPromptSubmit", "PreCompact", "PostCompact"}
    assert set(hooks) == expected
    for event in expected:
        entries = hooks[event]
        assert len(entries) == 1
        command_hooks = entries[0]["hooks"]
        assert len(command_hooks) == 1
        hook = command_hooks[0]
        assert hook == {"type": "command", 'command': '"${PLUGIN_ROOT}/bin/seywo-hook"'}


def test_plugin_hook_entrypoint_is_bundled_and_executable():
    entry = ROOT / "bin" / "seywo-hook"
    assert entry.is_file()
    assert entry.stat().st_mode & 0o111
    assert "seywo-runtime" in entry.read_text(encoding="utf-8")


def test_plugin_manifest_describes_owned_compact_trigger():
    manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    from seywo import __version__
    assert manifest["version"] == __version__
    text = manifest["interface"]["longDescription"]
    assert "Compact Hook" in text
    assert "自动整理" in text


def test_agent_plugins_manifest_registers_hooks_and_uses_absolute_plugin_entry():
    manifest = json.loads((ROOT / '.codex-plugin/plugin.json').read_text())
    assert manifest['hooks'] == './hooks.json'
    hooks = json.loads((ROOT / 'hooks.json').read_text())['hooks']
    assert set(hooks) == {'SessionStart', 'UserPromptSubmit', 'PreCompact', 'PostCompact'}
    assert all(entry['hooks'][0]['command'] == '"${PLUGIN_ROOT}/bin/seywo-hook"'
               for entries in hooks.values() for entry in entries)


def test_first_run_has_time_to_provision_private_runtime():
    config = json.loads((ROOT / 'mcp.json').read_text())['mcpServers']['seywo']
    assert config['startup_timeout_sec'] >= 180
    assert config['command'] == 'sh'
    assert config['cwd'] == '.'
