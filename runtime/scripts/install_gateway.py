#!/usr/bin/env python3
"""Explicit install only; no implicit startup, pip, model calls, or history backfill."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True  # Never mutate signed application resources.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sessiongraph.gateway_install import install

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--apply", action="store_true", required=True)
p.add_argument("--python", type=Path, required=True)
p.add_argument("--app", type=Path, default=Path("/Applications/SessionGraph.app"))
p.add_argument("--legacy-root", type=Path, required=True)
p.add_argument("--project-config", type=Path, required=True)
p.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
p.add_argument(
    "--state",
    type=Path,
    default=Path.home() / "Library/Application Support/local-ai-gateway/sessiongraph",
)
a = p.parse_args()
subprocess.run(
    [str(a.python), "-c", "import sys,mcp; assert sys.version_info >= (3,11)"],
    check=True,
    timeout=15,
)
print(
    json.dumps(
        install(
            codex_home=a.codex_home,
            state=a.state,
            app=a.app,
            python=a.python,
            legacy=a.legacy_root,
            project_config=a.project_config,
        ),
        ensure_ascii=False,
        indent=2,
    )
)
