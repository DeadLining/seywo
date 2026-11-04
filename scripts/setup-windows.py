"""Explicit Windows setup after installing the desktop app; no model calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone


def immutable_copy(source: Path, state: Path) -> Path:
    files = sorted(p for p in source.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    if any(path.is_symlink() for path in files):
        raise ValueError("runtime resources contain a symlink")
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(source).as_posix().encode())
        digest.update(path.read_bytes())
    target = state / "runtimes" / digest.hexdigest()[:20]
    if target.exists():
        existing = hashlib.sha256()
        for path in sorted(p for p in target.rglob("*") if p.is_file()):
            existing.update(path.relative_to(target).as_posix().encode())
            existing.update(path.read_bytes())
        if existing.hexdigest() != digest.hexdigest():
            raise ValueError("existing runtime differs from installed app")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.with_name(target.name + ".stage-" + uuid.uuid4().hex[:8])
    try:
        shutil.copytree(source, stage, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        if not (stage / "entrypoint.py").is_file():
            raise ValueError("copied runtime is incomplete")
        os.replace(stage, target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return target


def main() -> int:
    if os.name != "nt":
        raise ValueError("setup-windows.py runs only on Windows")
    if not sys.flags.utf8_mode:
        return subprocess.call([sys.executable, "-X", "utf8", __file__, *sys.argv[1:]])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path, help="installed SessionGraph.exe or its folder")
    parser.add_argument("--project", required=True, type=Path, help="project to index and show")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--import-existing", action="store_true", help="index existing project chats locally")
    parser.add_argument("--enable-hooks", action="store_true", help="register future-chat hooks")
    args = parser.parse_args()
    app = args.app.expanduser().resolve()
    app_dir = app.parent if app.is_file() else app
    source = app_dir / "sessiongraph"
    project = args.project.expanduser().resolve()
    python = args.python.expanduser().resolve()
    if not app_dir.is_dir() or not source.joinpath("entrypoint.py").is_file():
        raise ValueError("installed app resources are missing")
    if not project.is_dir() or not python.is_file():
        raise ValueError("project or Python executable is missing")
    probe = subprocess.run([str(python), "-B", "-c",
                            "import sys,mcp,importlib.metadata as md; "
                            "v=tuple(int(x) for x in md.version('mcp').split('.')[:2]); "
                            "assert sys.version_info >= (3,11) and (1,28) <= v < (2,0)"],
                           capture_output=True, timeout=10)
    if probe.returncode != 0:
        raise ValueError("Python 3.11+ and mcp>=1.28,<2 are required before setup")
    sys.path.insert(0, str(source))
    from sessiongraph.core import Store, init_project, load_config
    from sessiongraph.gateway_install import install
    from sessiongraph.global_hooks import atomic_write
    from sessiongraph.platform_paths import runtime_home
    from sessiongraph.project_binding import inside_project, normalized_path

    state = runtime_home()
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    config = project / ".sessiongraph/config.json"
    if not config.exists():
        init_project(project)
        raw = json.loads(config.read_text())
        raw["hook"].update(allowed_transcript_roots=[
            str(codex_home / "sessions"), str(codex_home / "archived_sessions")],
            automatic_since=datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            enabled=True)
        atomic_write(config, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
    if args.enable_hooks and not load_config(config).get("hook", {}).get("allowed_transcript_roots"):
        raise ValueError("project Hook transcript roots are not configured")
    store = Store(load_config(config))
    store.close()
    runtime = immutable_copy(source, state)
    result = install(codex_home=codex_home, state=state, app=app_dir, python=python,
                     legacy=project / "sessiongraph", project_config=config,
                     source_root=runtime, install_hooks=args.enable_hooks, install_mcp=True)
    imported = {"files": 0, "entries": 0}
    if args.import_existing:
        index = codex_home / "state_5.sqlite"
        if not index.is_file():
            raise ValueError("Codex chat index is missing")
        with sqlite3.connect(index.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            rows = conn.execute("SELECT DISTINCT rollout_path, cwd FROM threads").fetchall()
        allowed = [codex_home / "sessions", codex_home / "archived_sessions"]
        store = Store(load_config(config))
        try:
            for source_path, cwd in rows:
                if not isinstance(source_path, str):
                    continue
                try:
                    if not inside_project(store.config, cwd):
                        continue
                    path = normalized_path(source_path)
                except (OSError, ValueError):
                    continue
                if not path.is_file() or not any(path.is_relative_to(normalized_path(root)) for root in allowed):
                    continue
                outcome = store.ingest(path)
                imported["files"] += 1
                imported["entries"] += outcome.get("added", 0)
        finally:
            store.close()
    print(json.dumps({"project": str(project), "runtime": str(runtime),
                      "changed": result["changed"], "backup": result["backup"],
                      "hook_trusted": False, "imported": imported}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
