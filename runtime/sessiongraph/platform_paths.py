"""Keep the existing macOS state location and use local app data on Windows."""

from __future__ import annotations

import os
from pathlib import Path


def runtime_home() -> Path:
    override = os.environ.get("SESSIONGRAPH_RUNTIME_HOME")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise OSError("LOCALAPPDATA is required for SessionGraph on Windows")
        return Path(base) / "SessionGraphData"
    return Path.home() / "Library/Application Support/local-ai-gateway/sessiongraph"
