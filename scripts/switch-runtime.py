"""Switch only the shared runtime owner, preserving registration, locks and data."""
import argparse
import json
import os
from pathlib import Path
import shlex
import sys

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--state", required=True, type=Path)
p.add_argument("--source", required=True, type=Path)
p.add_argument("--python", required=True, type=Path)
a = p.parse_args()
sys.path.insert(0, str(a.source))
from sessiongraph.gateway_install import apply_changes
from sessiongraph.global_hooks import locked

if not (a.source / "entrypoint.py").is_file() or not a.python.is_file():
    raise ValueError("missing runtime")
a.state.mkdir(parents=True, exist_ok=True, mode=0o700)
with locked(a.state / "install.lock"):
    changes = {a.state / "runtime.json": json.dumps({"python": str(a.python), "source": str(a.source)}, indent=2) + "\n"}
    if os.name != "nt":
        changes[a.state / "bin/sessiongraph"] = ("#!/bin/sh\nexec " + shlex.quote(str(a.python))
            + " -B " + shlex.quote(str(a.source / "entrypoint.py")) + ' "$@"\n')
    print(json.dumps(apply_changes(a.state, changes), ensure_ascii=False))
