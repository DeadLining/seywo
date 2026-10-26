"""Explicit local operations; no global Codex configuration is modified."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import Store, init_project, load_config


def handle_hook(store: Store, payload: dict) -> dict:
    if not isinstance(payload, dict) or payload.get("hook_event_name") not in {
        "PreCompact",
        "PostCompact",
        "Stop",
        "SessionEnd",
    }:
        raise ValueError("unsupported hook event")
    raw_path = payload.get("transcript_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("hook requires a transcript_path")
    path = Path(raw_path).expanduser().resolve()
    roots = store.config["hook"]["allowed_transcript_roots"]
    if not roots or not any(path.is_relative_to(Path(r).expanduser().resolve()) for r in roots):
        raise ValueError(
            "transcript path is outside hook allowlist; configure explicit roots first"
        )
    with path.open("rb") as f:
        first = f.readline(1024 * 1024)
    try:
        sid = json.loads(first)["payload"]["id"]
    except (ValueError, KeyError, TypeError):
        raise ValueError("invalid hook transcript metadata") from None
    if not isinstance(payload.get("session_id"), str) or sid != payload["session_id"]:
        raise ValueError("hook session identity does not match transcript")
    result = store.ingest(path)
    return {"import": result, "enqueued": store.enqueue(sid)}


def parser():
    p = argparse.ArgumentParser(
        description="Project-local session evidence index. Retrieval never calls a model."
    )
    p.add_argument("--config", type=Path, default=Path(".sessiongraph/config.json"))
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="create local config, policy and SQLite index")
    init.add_argument("--project", type=Path, default=Path.cwd())
    ingest = sub.add_parser("ingest", help="import explicitly supplied Codex rollout JSONL files")
    ingest.add_argument("paths", nargs="+", type=Path)
    ingest.add_argument("--enqueue", action="store_true")
    refresh = sub.add_parser("refresh-chat", help="import selected chat segments from allowed transcript roots; no extraction")
    refresh.add_argument("--chat-id", required=True)
    refresh.add_argument("--transcript-path", default=None, help="validate and import the Hook-supplied source without scanning historical segments")
    mem = sub.add_parser("memories", help="extract durable work memories from new chat entries using the configured provider")
    mem.add_argument("--chat-id", required=True)
    mem.add_argument("--allow-network", action="store_true",
                     help="required; new chat text will be sent to your configured provider")
    mem.add_argument("--pending-only", action="store_true",
                     help="retry only durable unfinished windows; no new/history extraction")
    mem.add_argument("--repair-oversize", action="store_true",
                     help="explicitly repair long-entry candidates behind the existing cursor")
    mem.add_argument("--dry-run", action="store_true",
                     help="preview repair candidates locally; no provider and no state writes")
    mem.add_argument("--max-chars", type=int, default=None)
    mem.add_argument("--call-budget", type=int, default=None)
    mem.add_argument("--step-budget", type=int, default=None)
    mem.add_argument("--through-rowid", type=int, default=None)
    mem.add_argument("--since-time", default=None,
                     help="only extract from entries at/after this ISO timestamp")
    mem.add_argument("--segment-id", default=None,
                     help="restrict extraction to one rollout segment")
    mem.add_argument("--transcript-path", default=None,
                     help="resolve the segment id from a Codex transcript path")
    mem.add_argument("--lookback-chars", type=int, default=None,
                     help="first-run scan budget over the newest entries")
    search = sub.add_parser("search", help="deterministic lexical search; no network")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=8)
    search.add_argument("--max-chars", type=int, default=6000)
    search.add_argument("--session", help="legacy alias for --chat-id")
    search.add_argument("--chat-id", help="logical chat ID; includes all rollout segments")
    search.add_argument("--segment-id", help="exact rollout segment ID")
    search.add_argument("--mode", choices=["raw", "hybrid", "graph"], default="hybrid")
    expand = sub.add_parser("expand", help="expand evidence, context and one-hop graph neighbors")
    expand.add_argument("object_id")
    expand.add_argument("--radius", type=int, default=2)
    expand.add_argument("--max-chars", type=int, default=10000)
    expand.add_argument("--offset", type=int, default=0)
    remember = sub.add_parser("remember", help="save one source-backed work-memory item")
    remember.add_argument("--kind", required=True, choices=["goal", "constraint", "decision", "finding", "artifact", "progress", "next_step"])
    remember.add_argument("--title", required=True)
    remember.add_argument("--content", required=True)
    remember.add_argument("--status", default="active")
    remember.add_argument("--scope", default="project")
    remember.add_argument("--path", dest="paths", action="append", default=[])
    remember.add_argument("--alias", dest="aliases", action="append", default=[])
    remember.add_argument("--source-entry-id")
    remember.add_argument("--source-quote")
    remember.add_argument("--supersedes-id")
    remember.add_argument("--memory-id")
    remember.add_argument("--chat-id")
    recall = sub.add_parser("recall", help="recall current work memory without model calls")
    recall.add_argument("query")
    recall.add_argument("--limit", type=int, default=3)
    recall.add_argument("--view", choices=["brief", "full"], default="brief")
    recall.add_argument("--max-bytes", type=int, default=4096)
    recall.add_argument("--offset", type=int, default=0)
    recall.add_argument("--include-superseded", action="store_true")
    recall.add_argument("--chat-id")
    resume = sub.add_parser("resume", help="show current work handoff without model calls")
    resume.add_argument("--limit", type=int, default=20)
    resume.add_argument("--include-completed", action=argparse.BooleanOptionalAction, default=True)
    resume.add_argument("--include-superseded", action="store_true")
    resume.add_argument("--chat-id")
    maintain = sub.add_parser("storage-maintain", help="verify sources, remove duplicate events and compact indexes; no models")
    maintain.add_argument("--apply", action="store_true")
    maintain.add_argument("--vacuum", action="store_true")
    receipts = sub.add_parser("receipt-maintain", help="preview or archive terminal Hook receipts losslessly; no models")
    receipts.add_argument("--apply", action="store_true")
    receipts.add_argument("--limit", type=int, default=100)
    receipts.add_argument("--max-bytes", type=int, default=16 * 1024 * 1024)
    receipts.add_argument("--after", default="", help="exclusive filename cursor from the previous result")
    receipt = sub.add_parser("receipt-read", help="expand one hot or losslessly archived Hook receipt; no models")
    receipt.add_argument("run_id")
    sources = sub.add_parser('source-retain', help='preview or retain published evidence snapshots; no models')
    sources.add_argument('--apply', action='store_true')
    policy = sources.add_mutually_exclusive_group()
    policy.add_argument('--enable-publication', action='store_true', help='require snapshots for future publications in this project')
    policy.add_argument('--disable-publication', action='store_true', help='stop requiring new snapshots; retain all existing snapshots')
    sources.add_argument('--limit', type=int, default=100)
    sources.add_argument('--max-bytes', type=int, default=16 * 1024 * 1024)
    sources.add_argument('--after', default='', help='exclusive entry ID cursor')
    source = sub.add_parser('source-read', help='read one verified retained record, without external source access')
    source.add_argument('entry_id')
    classifier = sub.add_parser('judge-shadow', help='preview or execute bounded, source-checked single-token classification; never publish graph facts')
    classifier.add_argument('manifest', type=Path)
    classifier.add_argument('--allow-network', action='store_true', help='explicitly send the selected project evidence to the configured provider')
    classifier.add_argument('--call-budget', type=int, default=3, help='1..3 requests this invocation; cumulative manifest budgets still apply')
    migration = sub.add_parser('legacy-migrate', help='preview or explicitly replay bounded old Claim/Decision source windows; never delete history')
    migration.add_argument('--node-id', action='append', default=[])
    migration.add_argument('--limit', type=int, default=1, help='1..16 old nodes per invocation')
    migration.add_argument('--after', default='')
    migration.add_argument('--max-chars', type=int, default=8000)
    migration.add_argument('--apply', action='store_true', help='freeze manual-only jobs; no calls without --allow-network')
    migration.add_argument('--allow-network', action='store_true')
    migration.add_argument('--call-budget', type=int, default=3)
    migration.add_argument('--refresh-contract', action='store_true', help='explicitly reprocess selected completed manual jobs once under a newer extraction contract; preserves cumulative usage')
    revision = sub.add_parser('revision-read', help='inspect bounded immutable assertion history and shadow scope effects; no models')
    revision.add_argument('node_id')
    revision.add_argument('--at', default=None, help='timezone-aware effective time, not ingestion time')
    revision.add_argument('--limit', type=int, default=50)
    revision.add_argument('--after', default='')
    review = sub.add_parser('revision-assess', help='preview or append an explicitly reviewed shadow correction; never changes normal readers')
    review.add_argument('manifest', type=Path)
    review.add_argument('--apply', action='store_true')
    control = sub.add_parser('revision-control', help='preview or explicitly retract/restore/amend a shadow review; never changes normal readers')
    control.add_argument('manifest', type=Path)
    control.add_argument('--apply', action='store_true')
    review_read = sub.add_parser('revision-review-read', help='read one shadow review and paginated immutable control history; no models')
    review_read.add_argument('review_id')
    review_read.add_argument('--at', default=None)
    review_read.add_argument('--limit', type=int, default=20)
    review_read.add_argument('--after', default='')
    sub.add_parser("status", help="show raw/semantic coverage and failed tasks")
    enqueue = sub.add_parser(
        "enqueue", help="freeze new ranges for extraction without invoking a model"
    )
    enqueue.add_argument("--session")
    recovery = sub.add_parser("recover-blocked", help="preview selective V2 recovery; no model calls")
    recovery.add_argument("--job-id", action="append", default=[])
    recovery.add_argument("--request-id", help="validated project-local Jev receipt proving service recovery")
    recovery.add_argument("--apply", action="store_true", help="requeue eligible jobs and wake installed automatic worker")
    sub.add_parser("retry", help="explicitly requeue failed legacy jobs under the current policy")
    worker = sub.add_parser("worker", help="extract queued ranges using configured provider")
    worker.add_argument(
        "--allow-network",
        action="store_true",
        help="consent to send indexed transcript batches to configured provider",
    )
    worker.add_argument("--max-jobs", type=int, default=1)
    sub.add_parser("hook", help="read lifecycle payload from stdin; import and enqueue only")
    sub.add_parser("mcp", help="serve three read-only retrieval tools over stdio")
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    store = None
    try:
        if args.command == "init":
            config = init_project(args.project)
            store = Store(load_config(config))
            result = {"config": str(config), "database": store.config["database"]}
        elif args.command == "mcp":
            from .mcp_server import create_server

            create_server(args.config).run(transport="stdio")
            return 0
        elif args.command == 'receipt-maintain':
            from .maintenance import maintain_receipts
            result = maintain_receipts(load_config(args.config), apply=args.apply, limit=args.limit,
                                       max_bytes=args.max_bytes, after=args.after)
        elif args.command == 'receipt-read':
            from .run_receipts import read
            result = json.loads(read(load_config(args.config), args.run_id))
        elif args.command == 'source-retain':
            from .maintenance import maintain_sources
            result = maintain_sources(load_config(args.config), apply=args.apply, enable=args.enable_publication,
                                      disable=args.disable_publication, limit=args.limit, max_bytes=args.max_bytes, after=args.after)
        elif args.command == 'source-read':
            from .source_retention import read
            store = Store(load_config(args.config), readonly=True)
            result = read(store, args.entry_id)
            if result is None:
                raise ValueError('source_snapshot_not_retained')
        elif args.command == 'judge-shadow':
            from .classifier_shadow import run
            with args.manifest.open('rb') as stream:
                raw = stream.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                raise ValueError('classifier_shadow_manifest_too_large')
            manifest = json.loads(raw)
            store = Store(load_config(args.config), readonly=not args.allow_network)
            result = run(store, manifest, allow_network=args.allow_network, call_budget=args.call_budget)
        elif args.command == 'legacy-migrate':
            from .legacy_migration import run
            result = run(args.config,node_ids=args.node_id,limit=args.limit,after=args.after,
                         max_chars=args.max_chars,apply=args.apply,allow_network=args.allow_network,
                         call_budget=args.call_budget,refresh_contract=args.refresh_contract)
        elif args.command == 'revision-read':
            from .revision_ledger import inspect
            store = Store(load_config(args.config), readonly=True)
            with store.db:
                store.db.execute('BEGIN')
                result = inspect(store, args.node_id, at=args.at, limit=args.limit, after=args.after)
        elif args.command == 'revision-review-read':
            from .review_control import read
            store = Store(load_config(args.config), readonly=True)
            result = read(store, args.review_id, at=args.at, limit=args.limit, after=args.after)
        elif args.command in {'revision-assess', 'revision-control'}:
            from .revision_ledger import assess
            from .review_control import control
            from .maintenance import project_lock
            from contextlib import nullcontext
            with args.manifest.open('rb') as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError('revision_manifest_too_large')
            manifest = json.loads(raw)
            config = load_config(args.config)
            with project_lock(config) if args.apply else nullcontext():
                store = Store(config, readonly=not args.apply)
                with store.db:
                    store.db.execute('BEGIN IMMEDIATE' if args.apply else 'BEGIN')
                    result = (control if args.command=='revision-control' else assess)(store, manifest, apply=args.apply)
        else:
            preview = (args.command == "memories" and args.dry_run) or (args.command == "recover-blocked" and not args.apply)
            store = Store(load_config(args.config), readonly=preview or args.command in {"recall", "resume", "search", "expand", "status", "packet"})
            if args.command == "recover-blocked":
                from .recovery import recover
                if not args.apply:
                    result = recover(store, request_id=args.request_id, job_ids=args.job_id)
                else:
                    import fcntl
                    from .stage_leases import quiescent
                    with quiescent(store.config, 'recover-blocked: project batch active; retry after completion'), (Path(store.config["database"]).parent / "graph-v2.lock").open("a") as lock:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            raise ValueError("recover-blocked: project batch active; retry after completion") from None
                        result = recover(store, request_id=args.request_id, job_ids=args.job_id, apply=True)
            elif args.command == "storage-maintain":
                import fcntl
                from .storage import migrate_sources, compact_term_tables, prune_completed_checkpoints, archive_completed_payloads
                if not args.apply:
                    raise ValueError("storage-maintain requires --apply; take a verified database backup first")
                lock_path = Path(store.config["database"]).parent / "graph-v2.lock"
                from .stage_leases import quiescent
                with quiescent(store.config, 'storage-maintain: project batch active; retry after it completes'), lock_path.open("a") as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        raise ValueError("storage-maintain: project batch active; retry after it completes") from None
                    result = migrate_sources(store)
                    result["term_tables"] = compact_term_tables(store)
                    with store.db:
                        result["completed_cache"] = prune_completed_checkpoints(store)
                        result["archived_payloads"] = archive_completed_payloads(store)
                    if args.vacuum:
                        store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        store.db.execute("VACUUM")
                    result["integrity"] = store.db.execute("PRAGMA quick_check").fetchone()[0]
            elif args.command == "ingest":
                imports = [store.ingest(path) for path in args.paths]
                result = {"imports": imports, "enqueued": store.enqueue() if args.enqueue else 0}
            elif args.command == "refresh-chat":
                from .continuity import refresh_chat

                result = refresh_chat(store, args.chat_id, args.transcript_path)
            elif args.command == "memories":
                if not args.allow_network and not args.dry_run:
                    raise ValueError(
                        "memories requires --allow-network; new chat text will be sent to your configured provider"
                    )
                from .extraction import create_extractor
                from .memories import run_chat

                from .memories import resolve_segment

                extractor = None if args.dry_run else create_extractor(store.config)
                segment_id = args.segment_id
                if args.transcript_path:
                    segment_id = resolve_segment(store, args.chat_id, args.transcript_path)
                    if segment_id is None:
                        raise ValueError(
                            "transcript_path is not indexed for this chat; run refresh-chat first"
                        )
                result = run_chat(
                    store, extractor, args.chat_id, max_chars=args.max_chars,
                    since_time=args.since_time, segment_id=segment_id,
                    lookback_chars=args.lookback_chars,
                    repair_oversize=args.repair_oversize, dry_run=args.dry_run,
                    pending_only=args.pending_only, through_rowid=args.through_rowid, step_budget=args.step_budget, call_budget=args.call_budget,
                )
            elif args.command == "search":
                result = store.search(
                    args.query,
                    args.limit,
                    args.max_chars,
                    args.session,
                    args.mode,
                    chat_id=args.chat_id,
                    segment_id=args.segment_id,
                )
            elif args.command == "expand":
                result = store.expand(args.object_id, args.radius, args.max_chars, args.offset)
            elif args.command == "remember":
                result = store.remember(
                    kind=args.kind, title=args.title, content=args.content, status=args.status,
                    scope=args.scope, paths=args.paths, aliases=args.aliases,
                    source_entry_id=args.source_entry_id, source_quote=args.source_quote,
                    supersedes_id=args.supersedes_id, memory_id=args.memory_id,
                    chat_id=args.chat_id,
                )
            elif args.command == "recall":
                result = store.recall(args.query, args.limit, args.include_superseded, args.chat_id,
                                      view=args.view, max_bytes=args.max_bytes, offset=args.offset)
            elif args.command == "resume":
                result = store.resume(args.limit, args.include_completed, args.include_superseded, args.chat_id)
            elif args.command == "status":
                result = store.status()
            elif args.command == "enqueue":
                result = {"enqueued": store.enqueue(args.session)}
            elif args.command == "retry":
                result = {"requeued": store.retry_failed()}
            elif args.command == "worker":
                if not args.allow_network:
                    raise ValueError(
                        "worker requires --allow-network; chat content will be sent to your configured provider"
                    )
                if not 1 <= args.max_jobs <= 20:
                    raise ValueError("max-jobs must be between 1 and 20")
                from .extraction import create_extractor, run_one

                extractor = create_extractor(store.config)
                jobs = []
                for _ in range(args.max_jobs):
                    job = run_one(store, extractor)
                    jobs.append(job)
                    if job["state"] in {"idle", "failed"}:
                        break
                result = {"jobs": jobs}
            elif args.command == "hook":
                raw = sys.stdin.read(65537)
                if len(raw) > 65536:
                    raise ValueError("hook payload exceeds 64 KiB")
                result = handle_hook(store, json.loads(raw))
        if args.command == "recall":
            from .recall_output import serialize
            print(serialize(result))  # Only the trailing CLI newline is outside the budget.
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "worker" and any(j["state"] == "failed" for j in result["jobs"]):
            return 1
        return 0
    except (ValueError, OSError, KeyError, ImportError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        if store:
            store.close()
