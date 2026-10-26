import json
import tomllib
from pathlib import Path
import sys

from sessiongraph.gateway_install import install
from sessiongraph.core import init_project


def test_install_migrates_only_owned_registration_and_is_idempotent(tmp_path):
    home = tmp_path / "codex"
    home.mkdir()
    state = tmp_path / "state"
    app = tmp_path / "Gateway.app"
    source = app / "Contents/Resources/sessiongraph"
    source.mkdir(parents=True)
    (source / "entrypoint.py").write_text("")
    old = tmp_path / "legacy"
    root = tmp_path / "project"
    root.mkdir()
    config = init_project(root)
    (root / ".codex").mkdir()
    local = root / ".codex/config.toml"
    local.write_text(
        f'[mcp_servers.sessiongraph]\ncommand = {json.dumps(str(old / ".venv/bin/python"))}\nargs=["-m","sessiongraph","mcp"]\n\n[mcp_servers.unrelated]\ncommand="keep"\n'
    )
    (home / "config.toml").write_text("[features]\nkeep=true\n")
    hooks = {
        "hooks": {
            "PreCompact": [
                {
                    "hooks": [
                        {"type": "command", "command": str(old / "hooks/sg_codex_hook.py")},
                        {"type": "command", "command": "keep"},
                    ]
                }
            ]
        }
    }
    (home / "hooks.json").write_text(json.dumps(hooks))
    settings = {
        "enabled": True,
        "enabled_since": "2026-10-08T11:31:48.162Z",
        "template_config": str(config),
        "registry": str(root / ".sessiongraph/ui-projects.json"),
        "transcript_roots": [],
    }
    (home / "sessiongraph.json").write_text(json.dumps(settings))
    (root / ".sessiongraph/ui-projects.json").write_text(
        json.dumps({"projects": [{"root": str(root)}]})
    )
    kwargs = dict(
        codex_home=home,
        state=state,
        app=app,
        python=Path(sys.executable),
        legacy=old,
        project_config=config,
    )
    result = install(**kwargs)
    assert result["changed"] and result["trusted"] is False
    assert tomllib.loads((home / "config.toml").read_text())["features"]["keep"]
    assert "sessiongraph" not in tomllib.loads(local.read_text())["mcp_servers"]
    assert tomllib.loads(local.read_text())["mcp_servers"]["unrelated"]["command"] == "keep"
    assert (
        json.loads((home / "hooks.json").read_text())["hooks"]["PreCompact"][0]["hooks"][1][
            "command"
        ]
        == "keep"
    )
    assert (
        json.loads((home / "sessiongraph.json").read_text())["enabled_since"]
        == settings["enabled_since"]
    )
    assert json.loads((state / "template.json").read_text())["provider"]
    assert not install(**kwargs)["changed"]
    assert (state / "bin/sessiongraph").stat().st_mode & 0o100


def test_custom_mcp_registration_is_not_overwritten(tmp_path):
    from sessiongraph.gateway_install import _mcp
    import pytest

    original = '[mcp_servers.sessiongraph]\ncommand="custom"\n'
    with pytest.raises(ValueError, match="custom"):
        _mcp(original, tmp_path / "launcher", tmp_path / "old")


def test_mcp_subtables_removed_without_changing_other_sections(tmp_path):
    from sessiongraph.gateway_install import _mcp

    old = tmp_path / "old"
    original = (
        "[mcp_servers.sessiongraph]\ncommand="
        + json.dumps(str(old / ".venv/bin/python"))
        + '\n[mcp_servers.sessiongraph.env]\nPYTHONPATH="old"\n[features]\nx=true\n'
    )
    assert tomllib.loads(_mcp(original, tmp_path / "launcher", old, remove=True)) == {
        "features": {"x": True}
    }
