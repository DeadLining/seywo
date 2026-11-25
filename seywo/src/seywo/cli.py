"""Independent local CLI. Every import names its chat, file and segment order."""

import argparse
from contextlib import nullcontext
import json
from pathlib import Path
import sqlite3
import sys

from .config import initialize, load_config
from .store import Store



def _read_json(path):
    with path.open("rb") as handle:
        data = handle.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise ValueError("JSON input exceeds 4 MiB")
    return json.loads(data)


def _compact(args):
    from . import inbox, maintain
    config = load_config(args.config)
    if args.compact_command == "enable":
        data = json.loads(Path(config["_path"]).read_text(encoding='utf-8'))
        data["hook"] = {"enabled": not args.disable}
        Path(config["_path"]).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding='utf-8')
        return {"config": config["_path"], "hook": data["hook"]}
    if args.compact_command == "status":
        return {"pending": [{k: i[k] for k in ("id", "chat_id", "event", "state", "attempts", "error_code")}
                            for i in inbox.pending(config)],
                "failed": [{k: i[k] for k in ("id", "chat_id", "event", "attempts", "error_code")}
                           for i in inbox.failed(config, "")],
                "model_calls": 0}
    if args.compact_command == "retry":
        items = inbox.failed(config, args.chat)
        for item in items:
            inbox.retry(config, item)
        return {"chat_id": args.chat, "retried": len(items), "model_calls": 0}
    return maintain.run_chat(config, args.chat)


def main(argv=None, *, sdk=nullcontext):
    parser = argparse.ArgumentParser(prog="seywo", description="Seywo — See Your Work")
    parser.add_argument("--config", type=Path, default=Path.cwd() / ".seywo/config.json")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook", help="stdin Codex hook payload (no config discovery)")
    init = sub.add_parser("init")
    init.add_argument("root", type=Path, nargs="?", default=Path.cwd())
    batch = sub.add_parser("init-batch", help="preview and confirm initialization of explicit project roots")
    batch.add_argument("roots", type=Path, nargs="+", help="existing absolute project directories; no scanning")
    batch.add_argument("--dry-run", action="store_true", help="print the plan without prompting or writing")
    imp = sub.add_parser("import")
    imp.add_argument("source", type=Path)
    imp.add_argument("--chat", required=True)
    imp.add_argument("--order", type=int, required=True)
    events = sub.add_parser("events")
    events.add_argument("--chat", required=True)
    events.add_argument("--limit", type=int, default=50)
    events.add_argument("--after-order", type=int, default=-1)
    events.add_argument("--after-record", type=int, default=0)
    read = sub.add_parser("read")
    read.add_argument("event_id")
    read.add_argument("--snapshot")
    read.add_argument("--start", type=int, default=0)
    read.add_argument("--end", type=int)
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--chat")
    search.add_argument("--limit", type=int, default=20)
    status = sub.add_parser("status")
    status.add_argument("--chat")
    storage = sub.add_parser("storage", help="verify or reclaim Seywo storage")
    storage_actions = storage.add_subparsers(dest="storage_command", required=True)
    storage_actions.add_parser("verify")
    storage_actions.add_parser("gc")
    compact = sub.add_parser("compact", help="automatic compact maintenance")
    actions = compact.add_subparsers(dest="compact_command", required=True)
    enable = actions.add_parser("enable")
    enable.add_argument("--disable", action="store_true")
    actions.add_parser("status")
    retry = actions.add_parser("retry")
    retry.add_argument("--chat", required=True)
    run = actions.add_parser("run")
    run.add_argument("--chat", required=True)

    sub.add_parser("mcp-install", help="register Seywo in this project .codex/config.toml")
    sub.add_parser("mcp", help="project-bound stdio tools for Codex; retrieval is read-only")
    sub.add_parser("reindex")
    delete = sub.add_parser("delete-chat")
    delete.add_argument("--chat", required=True)
    understand = sub.add_parser("understand", help="durable model outbox; no implicit API calls")
    ops = understand.add_subparsers(dest="understand_command", required=True)
    prepare = ops.add_parser("prepare")
    prepare.add_argument("--chat", required=True)
    prepare.add_argument("--options", type=Path, required=True, help="explicit backend/model/capacity JSON")
    prepare.add_argument("--limits", type=Path)
    claim = ops.add_parser("claim")
    claim.add_argument("job_id")
    claim.add_argument("--backend", required=True)
    submit = ops.add_parser("submit")
    submit.add_argument("attempt_id")
    submit.add_argument("--backend", required=True)
    submit.add_argument("--response", type=Path, required=True, help="output/usage/finish JSON envelope")
    show = ops.add_parser("show")
    show.add_argument("job_id")
    budget = ops.add_parser("budget")
    budget.add_argument("job_id")
    budget.add_argument("--limits", type=Path, required=True)
    context = ops.add_parser("context")
    context.add_argument("job_id")
    context.add_argument("--options", type=Path, required=True)
    judge = ops.add_parser("judge")
    judge.add_argument("job_id")
    judge.add_argument("--kind", choices=["identity", "support", "approval"], required=True)
    judge.add_argument("--question", required=True)
    judge.add_argument("--item", action="append", required=True)
    judge.add_argument("--backend")
    judge.add_argument("--model")
    narrative = sub.add_parser("narrative", help="versioned chat reading and protected manual corrections")
    views = narrative.add_subparsers(dest="narrative_command", required=True)
    organize = views.add_parser("prepare")
    organize.add_argument("job_id")
    publish = views.add_parser("publish")
    publish.add_argument("task_id")
    read_narrative = views.add_parser("show")
    read_narrative.add_argument("--chat", required=True)
    read_narrative.add_argument("--version", type=int)
    edit = views.add_parser("edit")
    edit.add_argument("--chat", required=True)
    edit.add_argument("--amendment", type=Path, required=True)
    serve = views.add_parser("serve")
    serve.add_argument("--chat", required=True)
    serve.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        if args.command == "init-batch":
            from .batch_init import run
            return run(args.roots, dry_run=args.dry_run)
        if args.command == "narrative" and args.narrative_command == "serve":
            from .reader import make_server
            server = make_server(args.config, args.chat, port=args.port)
            print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}", "chat_id": args.chat}), flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return 0
        if args.command == "hook":
            from . import hook
            return hook.main()
        if args.command == "init":
            result = initialize(args.root)
        elif args.command == "mcp-install":
            from .mcp_install import install
            result = install(load_config(args.config))
        elif args.command == "mcp":
            with sdk():
                from .mcp_server import create_server
                create_server(args.config).run(transport="stdio")
            return 0
        elif args.command == "compact":
            result = _compact(args)
        elif args.command == "storage":
            config = load_config(args.config)
            if args.storage_command == "verify":
                from .storage_verify import verify
                result = verify(config)
            else:
                from .storage_lifecycle import collect
                result = collect(config, force=True, retire_dialogue=True)
        else:
            with Store(load_config(args.config), read_only=args.command in {"events", "read", "search", "status"} or (args.command == "understand" and args.understand_command == "show") or (args.command == "narrative" and args.narrative_command == "show")) as store:
                if args.command == "import":
                    result = store.import_source(args.source, args.chat, args.order)
                elif args.command == "events":
                    result = store.list_events(args.chat, limit=args.limit,
                                               after_order=args.after_order, after_record=args.after_record)
                elif args.command == "read":
                    result = store.read(args.event_id, snapshot_id=args.snapshot, start=args.start, end=args.end)
                elif args.command == "search":
                    result = store.search(args.query, chat_id=args.chat, limit=args.limit)
                elif args.command == "status":
                    result = store.status(args.chat)
                elif args.command == "reindex":
                    result = store.reindex()
                elif args.command == "narrative":
                    from .narrative import Narrative
                    service = Narrative(store)
                    operation = args.narrative_command
                    if operation == "prepare":
                        result = service.prepare(args.job_id)
                    elif operation == "publish":
                        result = service.publish(args.task_id)
                    elif operation == "show":
                        result = service.read(args.chat, version=args.version)
                    else:
                        result = service.edit(args.chat, **_read_json(args.amendment))
                elif args.command == "understand":
                    from .understanding import Limits, Options, Understanding
                    engine = Understanding(store)
                    operation = args.understand_command
                    if operation == "prepare":
                        result = engine.prepare(args.chat, Options.from_dict(_read_json(args.options)),
                                                Limits(**_read_json(args.limits)) if args.limits else Limits())
                    elif operation == "claim":
                        result = engine.claim(args.job_id, backend=args.backend)
                    elif operation == "submit":
                        response = _read_json(args.response)
                        if not isinstance(response, dict) or not set(response) <= {"output", "usage", "finish"}:
                            raise ValueError("invalid_response_envelope")
                        result = engine.submit(args.attempt_id, backend=args.backend, **response)
                    elif operation == "show":
                        result = engine.status(args.job_id)
                    elif operation == "budget":
                        result = engine.replenish(args.job_id, Limits(**_read_json(args.limits)))
                    elif operation == "context":
                        result = engine.expand_context(args.job_id, Options.from_dict(_read_json(args.options)))
                    else:
                        result = engine.queue_judgment(args.job_id, kind=args.kind, question=args.question,
                                                       item_ids=args.item, backend=args.backend, model=args.model)
                else:
                    result = store.delete_chat(args.chat)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, sqlite3.Error, TypeError, KeyError):
        # Input/OS errors may contain original source payload or sensitive paths.
        print(json.dumps({"error": "seywo_operation_failed", "command": args.command,
                          "detail": "Check project config, source binding, input format and permissions."}),
              file=sys.stderr)
        return 2
