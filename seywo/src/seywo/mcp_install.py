"""Project-local MCP registration; preserve global and other server settings."""

import json
import os
from pathlib import Path
import tempfile
import tomllib


from .runtime_platform import replace_file

def install(config):
    root = Path(config["project_root"])
    package_root = Path(__file__).resolve().parents[2]
    host = package_root / "bin" / ("seywo-host.cmd" if os.name == "nt" else "seywo-host")
    if not host.is_file():
        raise ValueError("seywo_host_launcher_missing")
    command = str(host.resolve())
    path = root / ".codex" / "config.toml"
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("unsafe_codex_config")
    original = path.read_text(encoding='utf-8') if path.exists() else ""
    data = tomllib.loads(original)
    server = {
        "command": command,
        "args": ["--cli", "--config", config["_path"], "mcp"],
        "startup_timeout_sec": 240,
        "tool_timeout_sec": 60,
    }
    previous = data.get("mcp_servers", {}).get("seywo")
    if previous is not None and previous != server:
        raise ValueError("custom_seywo_registration_exists")
    if previous == server:
        return {"path": str(path), "changed": False, "model_calls": 0}
    addition = (
        "\n\n[mcp_servers.seywo]\ncommand = "
        + json.dumps(command)
        + "\nargs = "
        + json.dumps(server["args"])
        + "\nstartup_timeout_sec = 240\ntool_timeout_sec = 60\n"
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
        replace_file(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"path": str(path), "changed": True, "model_calls": 0}
