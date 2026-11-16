"""Project-local MCP registration; preserve global and other server settings."""

import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib


def install(config, *, python=None):
    root = Path(config["project_root"])
    entry = Path(__file__).resolve().parents[1] / "seywo" / "__main__.py"
    package_root = str(entry.parent.parent)
    python = str(Path(python or sys.executable).resolve())
    path = root / ".codex" / "config.toml"
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("unsafe_codex_config")
    original = path.read_text() if path.exists() else ""
    data = tomllib.loads(original)
    server = {
        "command": python,
        "args": ["-B", "-m", "seywo", "--config", config["_path"], "mcp"],
        "env": {"PYTHONPATH": package_root},
        "tool_timeout_sec": 60,
    }
    previous = data.get("mcp_servers", {}).get("seywo")
    if previous is not None and previous != server:
        raise ValueError("custom_seywo_registration_exists")
    if previous == server:
        return {"path": str(path), "changed": False, "model_calls": 0}
    addition = (
        "\n\n[mcp_servers.seywo]\ncommand = "
        + json.dumps(python)
        + "\nargs = "
        + json.dumps(server["args"])
        + "\ntool_timeout_sec = 60\n[mcp_servers.seywo.env]\nPYTHONPATH = "
        + json.dumps(package_root)
        + "\n"
    )
    expected = json.loads(json.dumps(data))
    expected.setdefault("mcp_servers", {})["seywo"] = server
    content = original.rstrip() + addition
    if tomllib.loads(content) != expected:
        raise ValueError("codex_config_not_preserved")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, delete=False, encoding="utf-8"
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"path": str(path), "changed": True, "model_calls": 0}
