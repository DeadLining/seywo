#!/usr/bin/env python3
"""Gateway-owned dispatcher; no installation, network, daemon or idle polling."""

import json
import os
from pathlib import Path
import runpy
import sys
from sessiongraph.platform_paths import runtime_home

ROOT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True  # Never mutate signed application resources.
sys.path.insert(0, str(ROOT))
os.environ["PYTHONPATH"] = str(ROOT)
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ["SESSIONGRAPH_PYTHON"] = sys.executable
STATE = runtime_home()
os.environ.setdefault("SESSIONGRAPH_WORKER_LOCK", str(STATE / "extract.lock"))
os.environ.setdefault("SESSIONGRAPH_LAUNCHER", str(STATE / "bin/sessiongraph"))


def discover_config(cwd):
    for root in [cwd, *cwd.parents]:
        candidate = root / ".sessiongraph/config.json"
        if candidate.is_file():
            return candidate
    raise ValueError(
        "No SessionGraph project at current working directory; pass --config explicitly"
    )


def main(args):
    # A signed desktop bundle is a bootstrap, not a second runtime owner.
    if ROOT.parts[-3:] == ('Contents', 'Resources', 'sessiongraph') and (STATE / 'runtime.json').is_file():
        manifest = STATE / 'runtime.json'
        if manifest.stat().st_size > 16384:
            raise ValueError('runtime manifest too large')
        saved = Path(json.loads(manifest.read_text()).get('source', ''))
        if saved != ROOT:
            if (not saved.is_absolute() or saved.parent != STATE / 'runtimes'
                    or not (saved / 'entrypoint.py').is_file()):
                raise ValueError('active runtime missing; refusing stale bundle fallback')
            os.environ['SESSIONGRAPH_APP_SOURCE'] = str(ROOT)
            sys.argv = [str(saved / 'entrypoint.py'), *args]
            runpy.run_path(str(saved / 'entrypoint.py'), run_name='__main__')
            return 0
    if args and args[0] == "native-settings":
        from sessiongraph.native_settings import main as settings_main

        return settings_main(STATE, Path(os.environ.get('SESSIONGRAPH_APP_SOURCE', str(ROOT))))
    if args and args[0] == "native-read":
        from sessiongraph.native_bridge import main as read_main

        return read_main(STATE / "projects.json")
    if args and args[0] == "native-flow":
        from sessiongraph.flow_generation import main as flow_main
        return flow_main(STATE)
    if args and args[0] == "native-flow-edit":
        from sessiongraph.flow_generation import edit_main
        return edit_main(STATE)
    if args and args[0] == "hook-event":
        runpy.run_path(str(ROOT / "hooks/sg_codex_hook.py"), run_name="__main__")
        return 0
    if args and args[0] == "update":
        sys.argv = ["sessiongraph.auto_update", *args[1:]]
        runpy.run_module("sessiongraph.auto_update", run_name="__main__")
        return 0
    if args and args[0] == "observe-deletions":
        sys.argv = ["sessiongraph.deletion_observer", *args[1:]]
        runpy.run_module("sessiongraph.deletion_observer", run_name="__main__")
        return 0
    if args and args[0] == "backfill-tiers":
        from sessiongraph.tier_backfill import enqueue_registered, run
        enqueue_registered(STATE)
        print(json.dumps(run(STATE)))
        return 0
    if args and args[0] == "continue-pending":
        from sessiongraph.continuation import start
        start()
        return 0
    if args and args[0] == "runtime-status":
        from importlib.metadata import version, PackageNotFoundError

        try:
            mcp_version = version("mcp")
        except PackageNotFoundError:
            mcp_version = None
        print(
            json.dumps(
                {
                    "source": str(ROOT),
                    "python": sys.executable,
                    "mcp_version": mcp_version,
                    "http_daemon": False,
                    "idle_polling": False,
                    "model_calls": 0,
                }
            )
        )
        return 0
    from sessiongraph.cli import main as cli_main

    if "--config" not in args and args and args[0] != "init":
        args = ["--config", str(discover_config(Path.cwd().resolve())), *args]
    return cli_main(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
