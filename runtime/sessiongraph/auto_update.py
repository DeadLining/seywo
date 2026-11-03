"""Observable, serialized background updates. Status/revision reads never call a model.

A hook_input receipt proves the launcher received an event string, not that Codex
actually compacted. Real acceptance additionally needs the original compact event.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import fcntl
import hashlib
import heapq
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .core import load_config
from .import_diagnostics import summarize_import

ACTIVE = {"queued", "importing", "extracting", "retry_wait"}
DEADLINE = 1800
DEFERRED_CODES = {"graph_call_budget_pending", "graph_continuation_pending", "daily_call_budget_pending", "stage_lease_busy", "stage_snapshot_changed", "resource_circuit_open"}


def revision(db, project_id):
    """Read an incremental change token; legacy stores retain the old fallback."""
    version = db.execute("SELECT value FROM metadata WHERE key='graph_revision_v2'").fetchone()
    if version:
        return f"v2:{project_id}:{version[0]}"
    h = hashlib.sha256()
    for row in db.execute(
        "SELECT * FROM work_memory WHERE project_id=? ORDER BY id", (project_id,)
    ):
        h.update(json.dumps(list(row), ensure_ascii=False, separators=(",", ":")).encode())
    # Quote validity is also part of the derived graph.
    for row in db.execute(
        "SELECT e.id,e.text FROM entries e WHERE e.id IN "
        "(SELECT source_entry_id FROM work_memory WHERE project_id=?) ORDER BY e.id",
        (project_id,),
    ):
        h.update(json.dumps(list(row), ensure_ascii=False, separators=(",", ":")).encode())
    from .work_relations import exists
    if exists(db):
        for row in db.execute(
            "SELECT r.*,e.text FROM work_memory_relations r "
            "LEFT JOIN entries e ON e.id=r.source_entry_id WHERE r.project_id=? ORDER BY r.id",
            (project_id,),
        ):
            h.update(json.dumps(list(row), ensure_ascii=False, separators=(",", ":")).encode())
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='sg2_nodes'").fetchone():
        queries = [
            "SELECT * FROM sg2_nodes WHERE project=? ORDER BY id",
            "SELECT * FROM sg2_relations WHERE project=? ORDER BY id",
            "SELECT * FROM sg2_unresolved WHERE project=? ORDER BY id",
            "SELECT x.*,e.text FROM sg2_evidence x JOIN sg2_nodes n ON n.id=x.node LEFT JOIN entries e ON e.id=x.entry_id WHERE n.project=? ORDER BY x.id",
            "SELECT x.*,e.text FROM sg2_relation_evidence x JOIN sg2_relations r ON r.id=x.relation LEFT JOIN entries e ON e.id=x.entry_id WHERE r.project=? ORDER BY x.id",
        ]
        for query in queries:
            for row in db.execute(query, (project_id,)):
                h.update(json.dumps(list(row), ensure_ascii=False, separators=(",", ":")).encode())
    return h.hexdigest()


def _folder(config):
    return Path(config["database"]).parent / "hook-state" / "runs"


def _atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".run-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(data, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _pid_alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def update_status(config, chat=""):
    """Small read-only polling response; never starts or retries extraction."""
    items = []
    unreadable = 0
    folder = _folder(config)
    # Bound parsing to recent run files; older files remain audit history.
    hot = []
    for path in folder.glob('*.json'):
        try:
            hot.append((path.stat().st_mtime, path.stem, path, None))
        except FileNotFoundError:
            continue  # A concurrent archiver has already committed the cold copy.
    hot = heapq.nlargest(100, hot, key=lambda item: (item[0], item[1]))
    from . import run_receipts
    with run_receipts.connection(config) as receipts_db:
        hot_ids = {item[1] for item in hot}
        cold = [row for row in run_receipts.recent(receipts_db, config['project_id'])
                if row['run_id'] not in hot_ids]
        candidates = hot
        candidates.extend((row['modified'], row['run_id'], None, row) for row in cold)
        selected = sorted(candidates, key=lambda item: (item[0], item[1]), reverse=True)[:100]
        for _, run_id, path, row in selected:
            try:
                if path is None:
                    text = run_receipts._text(receipts_db, row)
                else:
                    try:
                        with path.open('rb') as stream:
                            raw = stream.read(run_receipts.MAX_BYTES + 1)
                        if len(raw) > run_receipts.MAX_BYTES:
                            raise ValueError('run_receipt_too_large')
                        text = raw.decode('utf-8')
                    except FileNotFoundError:
                        text = run_receipts.read(config, run_id)
                run = json.loads(text)
                if not isinstance(run, dict) or not isinstance(run.get("updated_at"), (float, int)):
                    raise ValueError("invalid run")
                if chat and run.get("chat_id") != chat:
                    continue
                # Read the marker through the same project snapshot as the
                # receipt. A late old worker may rewrite a hot file, but its
                # deleted chat must not reappear in status or counts.
                if run_receipts.chat_deleted(receipts_db, run.get("chat_id")):
                    continue
                items.append(run)
            except (OSError, ValueError, TypeError):
                unreadable += 1
    normalized = []
    for run in items:
        try:
            if (run.get("state") in ACTIVE and time.time() - run["updated_at"] > DEADLINE + 60
                    and not _pid_alive(run.get("pid"))):
                run = {**run, "state": "stalled", "error_code": "worker_no_terminal_status"}
            # Legacy runs used dropped_oversize as a hard failure even when every
            # eligible batch succeeded. Normalize them at read time so audit files
            # remain immutable while the UI reports the actual outcome.
            if (run.get("state") == "failed"
                    and run.get("error_code") == "extraction_partial_failure"
                    and not run.get("failed_batches")
                    and run.get("dropped_oversize", 0) > 0):
                run = {**run, "state": "succeeded", "error_code": None,
                       "warning_code": "oversize_entries_skipped",
                       "warning_count": run["dropped_oversize"],
                       "historical_state": "failed"}
            normalized.append(run)
        except (OSError, ValueError, TypeError):
            unreadable += 1
    items = normalized
    items.sort(key=lambda r: r.get("created_at", 0), reverse=True)
    db = sqlite3.connect(Path(config["database"]).as_uri() + "?mode=ro", uri=True)
    try:
        db.execute("BEGIN")
        rev = revision(db, config["project_id"])
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='sg2_attempts'").fetchone() and "run_id" in {r[1] for r in db.execute("PRAGMA table_info(sg2_attempts)")}:
            for run in items:
                if run.get("state") not in {"failed","pending"}:continue
                jobs=db.execute("SELECT DISTINCT j.id,j.state FROM sg2_attempts a JOIN sg2_jobs j ON j.id=a.job WHERE a.run_id=? AND j.project=?",(run["run_id"],config["project_id"])).fetchall()
                # This is recovery of this run's attempted batches, not completion
                # of all transcript backlog. Keep the original run event immutable.
                if (run.get("attempt_outcome") != "succeeded" and jobs
                        and all(row[1]=="complete" for row in jobs)):
                    run["batches_recovered"]=True
    finally:
        db.close()
    from .continuation import snapshot
    return {
        "continuations": snapshot(config, chat),
        "revision": rev,
        "latest": items[0] if items else None,
        "runs": items[:8],
        "active_count": sum(r.get("state") in ACTIVE for r in items),
        "unreadable_runs": unreadable,
        "model_calls": 0,
    }


def _execute(argv, timeout):
    # Do not persist raw provider output/errors, which can contain source text or credentials.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=errors,
            timeout=timeout,
            cwd=Path(__file__).resolve().parents[1],
        )
        if result.returncode:
            from .failures import Failure
            from .request_audit import scrub
            errors.seek(0)
            import re
            raw_error=errors.read(65536).decode("utf-8",errors="replace")
            # CLI intentionally emits JSON errors without tracebacks. Preserve
            # known safe codes rather than hiding all such failures as WorkerExit.
            # Never persist arbitrary stderr, which may contain private evidence.
            safe_errors = {
                "transcript_path is not indexed for this chat; run refresh-chat first":
                    "extraction_source_not_indexed",
            }
            try:
                envelope = json.loads(raw_error)
                message = envelope.get("error") if isinstance(envelope, dict) else None
                code = safe_errors.get(message) if isinstance(message, str) else None
            except ValueError:
                code = None
            if code:
                raise Failure(code, stage="worker", detail=code, exception_type="WorkerExit")
            frames=re.findall(r'File "([^"\n]+)", line (\d+), in ([\w<>]+)',raw_error)[-12:]
            kinds=re.findall(r'^([\w.]+(?:Error|Exception)):',raw_error,re.M)
            detail=json.dumps(scrub(dict(exit_code=result.returncode,frames=frames,exception_type=kinds[-1] if kinds else "WorkerExit")))
            raise Failure("worker_failed",stage="worker",detail=detail,exception_type="WorkerExit")
        output.seek(0)
        raw = output.read(8_000_001)
        if len(raw) > 8_000_000:
            raise ValueError("output_too_large")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("invalid_output")
        return data


def run_update(config_path, chat, event="manual", transcript="", execute=None, through_rowid=None):
    config_path = Path(config_path).resolve()
    config = load_config(config_path)
    if not isinstance(chat, str) or not chat or len(chat) > 200:
        raise ValueError("invalid chat")
    if event not in {"manual", "PreCompact", "PostCompact", "AutoContinue"}:
        raise ValueError("invalid event")
    execute = execute or _execute
    start = time.time()
    state = {
        "run_id": uuid.uuid4().hex,
        "chat_id": chat,
        "event": event,
        "origin": "manual" if event == "manual" else "scheduler" if event == "AutoContinue" else "hook_input",
        "created_at": start,
        "updated_at": start,
        "state": "queued",
        "source_selection": "hook_path" if transcript else "full_scan",
        "source_transcript": transcript,
        "saved_count": 0,
        "saved_relation_count": 0,
        "imported_entries": 0,
        "error_code": None,
        "pid": os.getpid(),
    }
    path = _folder(config) / (state["run_id"] + ".json")

    def save(**fields):
        state.update(fields)
        state["updated_at"] = time.time()
        _atomic(path, state)
        if state['state'] in {'succeeded', 'prepared', 'coalesced', 'failed', 'cancelled'}:
            from .run_receipts import archive_if_enabled
            try:
                archive_if_enabled(config, path, state)
            except (OSError, ValueError, sqlite3.Error):
                # The original file remains authoritative if archival cannot finish.
                pass

    save()
    # Legacy updates serialize the whole run. Stage-policy coordinators serialize
    # each chat while the project update lock covers only local coordinator work.
    lock_path = _folder(config).parent / "update.lock"
    with ExitStack() as stack:
        stage = "queue"
        try:
            # A shared runtime lease allows independent projects to run together.
            # An installer may take EX to replace files safely; this is not a slot.
            global_lock = os.environ.get("SESSIONGRAPH_WORKER_LOCK")
            if global_lock:
                global_path = Path(global_lock)
                global_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                runtime_lease = stack.enter_context(global_path.open("a"))
                fcntl.flock(runtime_lease, fcntl.LOCK_SH)
            from .stage_leases import coordinator, child_window
            leased = stack.enter_context(coordinator(config, chat))
            lock = stack.enter_context(lock_path.open("a"))
            # Kernel wait, no polling/deadline for another chat in this project.
            fcntl.flock(lock, fcntl.LOCK_EX)
            from .chat_cleanup import excluded
            from .core import Store
            if Path(config['database']).is_file():
                with_store = Store(config, readonly=True)
                try:
                    cleaned = excluded(with_store, chat)
                finally:
                    with_store.close()
                if cleaned:
                    save(state='cancelled', reported_model_calls=0, error_code='source_cleanup')
                    return state
            if event == "AutoContinue":
                from .continuation import automatic_enabled
                if not automatic_enabled(config_path, load_config(Path(config_path))):
                    save(state="cancelled", reported_model_calls=0)
                    return state
            stage = "import"
            save(state="importing")
            if event == "AutoContinue" and through_rowid is not None:
                # Continue an already imported immutable boundary. New turns belong
                # to their own hook event; a historical path cannot block this work.
                if type(through_rowid) is not int or through_rowid < 0:
                    raise ValueError("invalid continuation boundary")
                imported = {"imports": [{"added": 0, "indexed_boundary": through_rowid}],
                            "skipped": [], "added_entries": 0}
                save(source_selection="indexed_boundary")
            else:
                imported = execute(
                    [
                        sys.executable,
                        "-m",
                        "sessiongraph",
                        "--config",
                        str(config_path),
                        "refresh-chat",
                        "--chat-id",
                        chat,
                    ] + (["--transcript-path", transcript] if transcript else []),
                    60,
                )
            diagnostics, import_error, import_warnings = summarize_import(imported)
            save(
                imported_entries=imported.get("added_entries", 0),
                skipped_sources=diagnostics["skipped_source_count"],
                import_diagnostics=diagnostics,
                warning_codes=import_warnings,
                warning_code=import_warnings[0] if import_warnings else None,
                warning_count=diagnostics["excluded_count"] + diagnostics["incomplete_tail_count"],
            )
            if import_error:
                save(state="failed", error_code=import_error)
                return state
            # Import at PreCompact; extraction is dispatched once after the event.
            # No second model budget is spent by the other half of the lifecycle.
            graph_enabled = config.get("graph_v2", {}).get("enabled")
            from .recovery_policy import settings as recovery_settings
            recovery = recovery_settings(config) if graph_enabled and (event != "manual" or leased) else None
            if event == "PreCompact":
                save(state="prepared", reported_model_calls=0, awaiting_event="PostCompact")
                return state
            if event == "PostCompact":
                with sqlite3.connect(Path(config["database"]).as_uri()+"?mode=ro",uri=True) as db:
                    table=db.execute("SELECT 1 FROM sqlite_master WHERE name='sg_compact_events'").fetchone()
                    compact=db.execute("SELECT e.segment,e.line,e.event_hash FROM sg_compact_events e JOIN session_chats c ON c.segment_id=e.segment WHERE c.chat_id=? ORDER BY e.rowid DESC LIMIT 1",(chat,)).fetchone() if table else None
                    boundary=db.execute("SELECT coalesce(max(e.rowid),0) FROM entries e JOIN session_chats c ON c.segment_id=e.session_id WHERE c.chat_id=?",(chat,)).fetchone()[0]
                cycle=hashlib.sha256(json.dumps([chat,compact or ["import-boundary",boundary]]).encode()).hexdigest()
                receipt=_folder(config).parent / "compact-events" / (cycle+".json")
                if receipt.exists():
                    prior=json.loads(receipt.read_text())
                    save(state="coalesced", reported_model_calls=0, coalesced_run_id=prior["run_id"], compact_cycle=cycle)
                    return state
                # Claim before dispatch, while holding project lock and shared runtime lease. A crashed run
                # stays visible and cannot silently spend a second event budget on replay.
                _atomic(receipt,{"run_id":state["run_id"],"cycle":cycle,"created_at":time.time()})
                save(compact_cycle=cycle)
            stage = "extraction"
            save(state="extracting")
            argv = [
                sys.executable,
                "-m",
                "sessiongraph.gateway_worker",
                "--request-run-id", state["run_id"],
                "--config",
                str(config_path),
                "--allow-network",
                "--memories",
                chat,
                "--memories-max-chars",
                "24000",
                "--memories-lookback-chars",
                "24000",
            ]
            if event != "manual":argv += ["--respect-graph-schedule"]
            since = config.get("hook", {}).get("automatic_since")
            if event != "manual" and since:
                argv += ["--memories-since", since]
            if transcript:
                argv += ["--memories-transcript-path", transcript]
            # Freeze this event's imported input; later turns cannot extend its target.
            with sqlite3.connect(Path(config["database"]).as_uri() + "?mode=ro", uri=True) as db:
                target = db.execute(
                    "SELECT coalesce(max(e.rowid),0) FROM entries e JOIN session_chats c "
                    "ON c.segment_id=e.session_id WHERE c.chat_id=?", (chat,)
                ).fetchone()[0]
            if through_rowid is not None:
                target = min(target, through_rowid)
            argv += ["--memories-through-rowid", str(target), "--memories-step-budget", "1"]
            if recovery is not None:
                argv += ["--memories-call-budget", str(recovery["max_calls_per_run"])]
            save(target_rowid=target)
            # A detached runner handles bounded delayed recovery itself; it does not
            # depend on the next Compact. Every child reopens durable checkpoints.
            totals = dict(saved_count=0, saved_relation_count=0, rejected_relation_count=0,
                          reported_model_calls=0, retry_count=0, fragment_batches=0)
            delays = (15.0, 60.0)
            recovery_rounds = 0
            continuation_rounds = 0
            pending_only = False
            permanent_failures = {}
            graph_totals = {}
            historical_codes = []
            round_receipts = []
            request_traces = []
            # The baseline drains continuously. Opted-in automatic runs yield
            # after one bounded child so the queue can fairly choose the next chat.
            save(model_call_limit=recovery["max_calls_per_run"] if recovery else None,
                 scheduling_policy="bounded_recovery_v1" if recovery else "continuous")
            no_progress_rounds = 0
            while True:
                worker_argv = argv + (["--memories-pending-only"] if pending_only else [])
                with child_window(lock, leased):
                    extracted = execute(worker_argv, DEADLINE)
                if not isinstance(extracted.get("saved"), list) or not isinstance(
                    extracted.get("failed"), list
                ):
                    raise ValueError("invalid_extraction_result")
                totals["saved_count"] += len(extracted["saved"])
                totals["saved_relation_count"] += len(extracted.get("saved_relations", []))
                totals["rejected_relation_count"] += sum(r.get("count", 1) for r in extracted.get("rejected_relations", []))
                for key, field in (("reported_model_calls", "model_calls"),
                                   ("retry_count", "retry_count"),
                                   ("fragment_batches", "fragment_batches")):
                    totals[key] += extracted.get(field) or 0
                request_traces.extend(extracted.get("request_traces", []))
                current_graph = extracted.get("graph_v2") or {}
                for key, value in current_graph.items():
                    if type(value) in (int, float) and key not in {"target_rowid","next_retry_at","blocked_jobs","pending_jobs","ready_jobs","retry_wait_jobs"}:
                        graph_totals[key] = graph_totals.get(key, 0) + value
                    elif key == "usage":
                        graph_totals.setdefault(key, []).extend(value)
                    elif key == "reasons":
                        bucket = graph_totals.setdefault(key, {})
                        for reason, count in value.items():
                            bucket[reason] = bucket.get(reason, 0) + count
                    else:
                        graph_totals[key] = value
                codes = [f.get("error_code", "extraction_partial_failure") for f in extracted["failed"]]
                historical_codes = list(dict.fromkeys([*historical_codes, *codes]))
                round_receipts.append({"round": len(round_receipts) + 1,
                    "pending_only": pending_only, "model_calls": extracted.get("model_calls", 0),
                    "failure_codes": codes, "graph_v2": current_graph})
                for failure in extracted["failed"]:
                    if failure.get("retryable") is not True:
                        identity = (failure.get("segment_id"), failure.get("from_line"),
                                    failure.get("error_code"))
                        permanent_failures[identity] = failure
                effective_failures = list(permanent_failures.values()) + [
                    failure for failure in extracted["failed"] if failure.get("retryable") is True
                ]
                effective_codes = list(dict.fromkeys(
                    failure.get("error_code", "extraction_partial_failure")
                    for failure in effective_failures
                ))
                fields = dict(
                    graph_v2=dict(graph_totals),
                    last_round_graph_v2=current_graph,
                    historical_failure_codes=historical_codes,
                    attempt_outcome="failed" if effective_failures else "succeeded",
                    backlog_failures=extracted.get("backlog_failures", []),
                    rounds=list(round_receipts),
                    request_traces=list(request_traces),
                    input_stats=extracted.get("input_stats", {}),
                    relation_rejection_reasons=extracted.get("rejected_relations", []),
                    **totals,
                    failed_batches=sum(f.get('error_code') not in DEFERRED_CODES for f in effective_failures),
                    dropped_oversize=extracted.get("dropped_oversize", 0),
                    failure_codes=effective_codes,
                    pending_retry_count=extracted.get("pending_retry_count", 0),
                    blocked_retry_count=extracted.get("blocked_retry_count", 0),
                    recovery_rounds=recovery_rounds,
                    continuation_rounds=continuation_rounds,
                )
                # Durable graph jobs own per-stage failure state. Only the scheduler
                # waits/retries; this process yields after real failure.
                if current_graph.get("durable_stages") and effective_failures:
                    fields["recovery_exhausted"]=False
                    save(**fields)
                    break
                # Permanent errors, unknown legacy failures and validation without
                # an explicit retryable classification never start a delayed round.
                can_recover = bool(extracted["failed"]) and bool(fields["pending_retry_count"]) and any(
                    item.get("retryable") is True for item in extracted["failed"]
                )
                fields["recovery_exhausted"] = can_recover
                save(**fields)
                if "graph_call_budget_pending" in effective_codes:
                    fields.update(
                        failed_batches=sum(f.get("error_code") not in DEFERRED_CODES for f in effective_failures),
                        recovery_exhausted=False,
                    )
                    break
                # Retry completion is not completion of the event's source frontier.
                # Legacy workers without source_remaining receive one normal check.
                remaining_graph = bool(current_graph.get("source_remaining") or
                                       current_graph.get("budget_exhausted") or (pending_only and bool(current_graph)))
                # Only delayed/blocked work remains: persist its status and let
                # the durable scheduler choose the next wakeup, without empty rounds.
                if (current_graph.get("durable_stages") and remaining_graph
                        and current_graph.get("runnable_remaining") is False
                        and not effective_failures):
                    break
                if recovery is not None and not effective_failures:
                    # The durable continuation queue owns the next round. Release
                    # update.lock after this single bounded child, including when
                    # it only replayed checkpoints or yielded before publishing.
                    fields["recovery_exhausted"] = False
                    if remaining_graph:
                        fields["deferred_reason"] = current_graph.get("deferred_reason", "recovery_round_budget")
                    break
                if remaining_graph and not effective_failures:
                    progressed = bool(extracted["saved"] or extracted.get("saved_relations") or
                        any(current_graph.get(k, 0) for k in ("input_source_chars", "work_units", "adaptive_splits")) or
                        (extracted.get("input_stats") or {}).get("selected_chars", 0))
                    if current_graph.get('progress_tracked'):
                        progressed = any(current_graph.get(k, 0) for k in
                            ('durable_progress', 'adaptive_splits', 'schema_repairs'))
                    no_progress_rounds = 0 if progressed else no_progress_rounds + 1
                    if no_progress_rounds >= 3:
                        effective_failures.append({"error_code": "continuation_no_progress", "retryable": False})
                        effective_codes.append("continuation_no_progress")
                        fields.update(failed_batches=1, failure_codes=effective_codes,
                                      blocked_retry_count=1, recovery_exhausted=True)
                        break
                    continuation_rounds += 1
                    pending_only = False
                    save(state="extracting", continuation_rounds=continuation_rounds)
                    continue
                if not can_recover or recovery_rounds >= len(delays):
                    break
                delay = delays[recovery_rounds]
                save(state="retry_wait", next_retry_at=time.time() + delay,
                     recovery_exhausted=False)
                time.sleep(delay)
                recovery_rounds += 1
                pending_only = True
                save(state="extracting", next_retry_at=None, recovery_rounds=recovery_rounds)
            problem = bool(effective_failures)
            actual_errors = [code for code in effective_codes if code not in DEFERRED_CODES]
            fields["attempt_outcome"] = "failed" if actual_errors else "deferred" if problem else "succeeded"
            warning_count = extracted.get("dropped_oversize", 0)
            if warning_count:
                fields.update(warning_code="oversize_entries_skipped", warning_count=warning_count)
            # PostCompact may have injected before slow extraction finished. Re-arm
            # restoration after newly saved items, for the next eligible prompt.
            if event != "manual" and (totals["saved_count"] or totals["saved_relation_count"]):
                from .restoration import mark
                mark(config, chat)
            save(
                **fields,
                state=("failed" if actual_errors else "pending" if problem or
                       (current_graph.get("durable_stages") and current_graph.get("source_remaining"))
                       else "succeeded"),
                error_code=(
                    (actual_errors or effective_codes or ["extraction_partial_failure"])[0]
                    if problem else None
                ),
            )
        except Exception as exc:
            from .failures import describe
            save(state="failed", error_code=getattr(exc,"code",None) or stage + "_failed",error_details=describe(exc,stage))
        # ExitStack closes both lock descriptors, including on queue failure.
    return state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--chat", required=True)
    parser.add_argument(
        "--event", default="manual", choices=["manual", "PreCompact", "PostCompact", "AutoContinue"]
    )
    parser.add_argument("--transcript", default="")
    args = parser.parse_args()
    try:
        os.nice(10)  # Background extraction yields CPU to interactive work.
    except OSError:
        pass
    run = run_update(args.config, args.chat, args.event, args.transcript)
    from .continuation import record
    record(args.config, run)
    print(json.dumps({"run_id": run["run_id"], "state": run["state"]}))
    return 0 if run["state"] in {"succeeded", "prepared", "coalesced"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
