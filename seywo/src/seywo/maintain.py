"""Detached compact maintainer: one writer per chat, ordered by compact boundary.

Only compact boundaries reach here. The source was already made durable by the
hook (verbatim snapshot for PreCompact); this worker imports it, plans windows on
native turn identity, then runs extraction and main-line organization per window
and publishes each window before moving on.

Every model request is a claim/submit pair on the durable outbox, so a kill at
any point leaves a resumable state instead of a partial version.
"""

import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from uuid import uuid4

from .runtime_platform import FileLease, replace_file

from . import inbox, maintenance_status
from .admission import NonHumanSource
from .config import load_config
from .codex_transport import CodexTransport
from .hook import chat_identity, spawn as spawn_worker
from .narrative import Narrative
from .provider import Capacity
from .settings import read_provider
from .store import Store
from .transport import Transport
from .understanding import Limits, Options, Understanding
from .windows import plan_windows

# Budget is measured in UTF-8 bytes, so CJK text needs roughly a third of
# the character count of the 24k application cap.
WINDOW_CHARS = 3000
# Reasoning tokens count against max_output_tokens, so the reply budget must be
# well above the JSON body itself; the saved settings default is too small.
MIN_OUTPUT_RESERVE = 16000
CALL_BUDGET = 40
RETRY_ATTEMPTS = 3
RETRY_CEILING = 24
RETRY_BACKOFF = 5
RETRY_BACKOFF_CAP = 120
# A single run should not sit in recovery for longer than this; the next compact
# (or an explicit retry) picks the job up again from persisted state.
RETRY_WAIT_BUDGET = 600


def _state_path(config, chat_id):
    folder = Path(config["_path"]).parent / "maintain"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{chat_id}.json"


def _read_state(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 0, "cursor": None, "windows": 0, "writer": None}


def _write_state(path, state):
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    replace_file(temporary, path)


def _after(cursor):
    return tuple(cursor) if cursor else None


def run_chat(config, chat_id):
    # Upgrade only in the detached worker, before acquiring shared writer leases.
    # Hooks stay fast and read-only retrieval never implicitly starts migration.
    try:
        from .store import SCHEMA_VERSION
        with Store(config, read_only=True) as store:
            needs_upgrade = store.db.execute('PRAGMA user_version').fetchone()[0] < SCHEMA_VERSION
        if needs_upgrade or (Path(config['_path']).parent / 'storage-migration.pending').exists():
            from .storage_admin import migrate
            maintenance_status.safe_record(config, chat_id, 'running', error_code='storage_migration_in_progress')
            migrate(config)
    except (OSError, ValueError, sqlite3.Error):
        maintenance_status.safe_record(config, chat_id, 'blocked', error_code='storage_upgrade_failed')
        return {'state': 'blocked', 'error_code': 'storage_upgrade_failed', 'calls': 0}
    report = _run_chat_owned(config, chat_id)
    if report.get('state') in {'done', 'idle'}:
        from .storage_lifecycle import safe_collect
        report['storage'] = safe_collect(config)
    return report


def _run_chat_owned(config, chat_id):
    from .locking import lifecycle, file_lock
    with lifecycle(config):
        handle = _lock(config, chat_id)
        if handle is None:
            return {"state": "busy", "calls": 0, "chat_id": chat_id}
        totals, windows = {}, []
        maintenance_status.safe_record(config, chat_id, "running")
        try:
            while True:
                report = _run_chat(config, chat_id)
                for key in ('calls', 'input_chars', 'output_chars', 'input_tokens', 'output_tokens', 'compact_segments'):
                    if key in report:
                        totals[key] = totals.get(key, 0) + report[key]
                windows.extend(report.get('windows', []))
                # The Hook publishes under the same inbox lock. Checking for
                # work and releasing ownership must be one serialized step:
                # arrivals before it are drained here; arrivals after it can
                # acquire the chat lock in their own spawned worker.
                with file_lock(inbox.root(config) / '.lock'):
                    if report.get('state') in {'done', 'idle'} and inbox.pending(config, chat_id):
                        continue
                    completed = {**report, **totals, 'windows': windows}
                    receipt_state = report.get('state')
                    failed = inbox.failed(config, chat_id)
                    if receipt_state in {'done', 'idle'} and failed:
                        receipt_state = 'failed'
                        completed.update(state='failed', error_code=failed[-1].get('error_code') or 'operation_failed')
                    if receipt_state in {'done', 'idle'} and report.get('deferred_turn'):
                        receipt_state = 'partial'
                        completed['error_code'] = 'awaiting_turn_complete'
                    if receipt_state in {'done', 'idle'}:
                        receipt_state = 'done'
                    elif receipt_state == 'excluded':
                        receipt_state = 'idle'
                    elif receipt_state not in {'partial', 'failed', 'blocked'}:
                        receipt_state = 'partial'
                    maintenance_status.safe_record(config, chat_id, receipt_state,
                                                   error_code=completed.get('error_code'), report=completed)
                    handle.close()
                    handle = None
                    return completed
        except Exception:
            maintenance_status.safe_record(config, chat_id, 'failed', error_code='maintainer_exception')
            raise
        finally:
            if handle is not None:
                handle.close()


def _run_chat(config, chat_id):
    """Process every pending compact record for one chat. Returns a small report."""
    if (Path(config["_path"]).parent / "storage-migration.pending").exists():
        return {"state": "blocked", "error_code": "storage_migration_in_progress"}
    try:
        provider = read_provider(config)
    except (OSError, ValueError):
        return {"state": "blocked", "error_code": "provider_not_configured", "calls": 0}
    native = provider.get("backend") == "codex"
    model = provider["model"]
    timeout = max(provider["timeout"], 420)
    if native:
        if (config.get("hook") or {}).get("pipeline") != "seywo-compact-v1":
            return {"state": "blocked", "error_code": "codex_requires_compact_pipeline", "calls": 0}
        transport = CodexTransport(model=model, timeout=timeout,
                                   reasoning_effort=provider["reasoning_effort"])
    else:
        key = provider.get("_api_key", "")
        if not key:
            return {"state": "blocked", "error_code": "missing_provider_credential", "calls": 0}
        transport = Transport(provider["endpoint"], key, model, timeout=timeout)
    output_reserve = min(32000, max(MIN_OUTPUT_RESERVE, provider["max_output_tokens"]))
    options_base = dict(backend="codex" if native else "app-settings", model=model,
                        capacity=Capacity(200000, 120000, 32000), output_reserve=output_reserve,
                        chunk_chars=3000, context_chars=300, lease_seconds=timeout + 60)
    state_path = _state_path(config, chat_id)
    state = _read_state(state_path)
    stamp = time.time()
    state["writer"] = stamp
    _write_state(state_path, state)
    calls = 0
    report = {"chat_id": chat_id, "windows": [], "calls": 0, "state": "idle"}

    def still_ours():
        return _read_state(state_path).get("writer") == stamp

    # The per-chat lock is already held: nothing else can own a claim, and a
    # failure from the previous compact is worth one more attempt.
    inbox.requeue_failed(config, chat_id)
    inbox.reclaim(config, chat_id)
    with Store(config) as store:
        try:
            store.require_human_chat(chat_id)
        except NonHumanSource:
            return {**report, "state": "excluded", "error_code": "excluded_nonhuman_source"}
        pipeline = (config.get("hook") or {}).get("pipeline")
        if pipeline == "seywo-compact-v1":
            from .compact_pipeline import run_compact
            return run_compact(config, chat_id, state, state_path, transport, report)
        if pipeline == "seywo-delta-v1":
            from .delta_pipeline import run_delta
            return run_delta(config, chat_id, state, state_path, transport, report)
        engine, service = Understanding(store), Narrative(store)
        # The state file is a cursor cache, not the only memory of in-flight
        # work: it can be missing or stale while jobs from earlier hooks still
        # hold a half-published window. Adopt the newest one before planning,
        # or the fresh plan collides with its frozen target (orphan_window).
        # The state cursor is only a cache. A window can publish while this file
        # still names an older job (a sibling run finished after the stamp was
        # written), so the published narrative decides where the next window
        # starts. Adopt its job and window end before planning, otherwise the
        # plan begins behind the parent and prepare() rejects it as discontinuous.
        published_job = _published_parent(store, chat_id)
        if published_job and published_job != state.get("job"):
            through = _job_through(store, published_job)
            if through:
                state.update(job=published_job, cursor=list(through),
                             version=max(state.get("version") or 0,
                                         _published_version(store, published_job)))
                _write_state(state_path, state)
        if not state.get("job"):
            adopted = _newest_open(store, chat_id)
            if adopted:
                state["job"], recovered = adopted
                # Only trust the recovered cursor once that window actually
                # published; until then the parked job owns those rows.
                if recovered is not None:
                    state["cursor"] = list(recovered)
                state["version"] = max(state.get("version") or 0, _published_version(store, adopted[0]))
                _write_state(state_path, state)
        # A frozen model alias can disappear from the gateway while the window
        # is parked. Base the window on the current model instead of retrying a
        # dead alias to the ceiling and keeping the chat stuck forever.
        job = state.get("job")
        if job and _model_moved(store, job, model) and _job_state(store, job) in OPEN_STATES:
            # If it never published, hand its window back to the planner instead
            # of silently skipping rows the dead alias was supposed to read.
            after = _retire_model(store, job)
            if after is not None:
                state["cursor"] = list(after) if after else None
            state.update(job=_live_parent(store, job))
            _write_state(state_path, state)
        # A record can land while this run is already working (a hook fires
        # during a long model call, or its sibling maintainer lost the lock and
        # exited). Re-scan until the inbox is quiet instead of only reading the
        # list once, otherwise a fresh compact waits for the *next* compact.
        quiet = 0
        while True:
            batch = inbox.pending(config, chat_id)
            if not batch:
                # The hook writes the record and spawns us; a sibling worker that
                # lost the lock exits immediately, so an empty list can be a
                # momentary gap rather than "nothing left". Treat it as drained
                # only after a few quiet polls.
                quiet += 1
                if quiet >= 2:
                    break
                time.sleep(0.5)
                continue
            quiet = 0
            for item in batch:
                claimed = inbox.claim(config, item)
                if claimed is None:
                    continue
                source = claimed.get("snapshot") or claimed["transcript"]
                path = Path(source)
                # Records queued before the hook read the transcript identity, and
                # any payload whose session_id names a root thread, arrive under
                # the wrong chat. Re-home them instead of importing a foreign
                # transcript (which fails the identity guard) or, worse, binding a
                # fork's turns to its parent.
                try:
                    owner = chat_identity(str(path), chat_id)
                except NonHumanSource:
                    # Terminal exclusion: old queued agent records must neither
                    # reroute into another chat nor enter model retry loops.
                    inbox.finish(config, claimed, state="done", error="excluded_nonhuman_source")
                    continue
                if owner != chat_id:
                    try:
                        inbox.record(config, owner, claimed["event"], str(path))
                    except (OSError, ValueError):
                        inbox.finish(config, claimed, state="failed", error="chat_identity_unresolved")
                        continue
                    inbox.finish(config, claimed, state="done", error="rerouted_chat")
                    spawn_worker(Path(config["_path"]), owner)
                    continue
                if not path.is_file():
                    inbox.finish(config, claimed, state="failed", error="source_missing")
                    report.update(state="failed", error_code="source_missing")
                    return report
                row = store.db.execute("SELECT id,ordinal FROM source_segments WHERE path=?", (str(path),)).fetchone()
                if row is not None:
                    # A hook reuses one snapshot name per chat, but that name may
                    # already belong to an earlier segment: a post-compact segment
                    # was published in between. Only the chat's newest segment can
                    # absorb an extension; anything else becomes its own segment.
                    last_ordinal = store.db.execute(
                        "SELECT coalesce(max(ordinal),-1) FROM source_segments WHERE chat_id=?", (chat_id,)
                    ).fetchone()[0]
                    if row["ordinal"] != last_ordinal:
                        # Copy, don't move: the older segment keeps a real file
                        # at its recorded path instead of dangling after the
                        # rename, and pruning still sees it as bound.
                        moved = path.with_name(f"{path.stem}-{uuid4().hex[:8]}{path.suffix}")
                        try:
                            shutil.copyfile(path, moved)
                            os.chmod(moved, 0o600)
                        except OSError:
                            moved.unlink(missing_ok=True)
                            inbox.finish(config, claimed, state="failed", error="source_missing")
                            report.update(state="failed", error_code="source_missing")
                            return report
                        claimed = inbox.relocate(config, claimed, moved)
                        path, row = moved, None
                if row is None:
                    ordinal = store.db.execute(
                        "SELECT coalesce(max(ordinal),-1)+1 FROM source_segments WHERE chat_id=?", (chat_id,)
                    ).fetchone()[0]
                else:
                    # The newest segment is reimported on every compact: the same
                    # path holds a grown transcript, so the snapshot is replaced and
                    # the leading already-covered records are dropped downstream.
                    ordinal = row["ordinal"]
                try:
                    store.import_source(path, chat_id, ordinal)
                except ValueError as exc:
                    inbox.finish(config, claimed, state="failed", error=str(exc)[:120] or "import_failed")
                    continue
                # Finish a window an earlier run had to park *before* opening the
                # next one: a new window would build on a job whose remaining
                # organize task is still pending and orphan it.
                calls = _drain_open_job(store, engine, service, transport, chat_id,
                                        state, state_path, report, calls)
                if report.get("pending_job"):
                    # Parked window: keep the record retryable and say partial, so a
                    # caller can tell this apart from "nothing to do".
                    inbox.finish(config, claimed, state="failed", error="pending_job")
                    inbox.prune_live(config, chat_id, _bound_paths(store, chat_id))
                    report["state"] = "partial"
                    return report
                calls, status = _advance(store, engine, service, transport, chat_id,
                                         state, state_path, report, calls, options_base)
                if status == "failed":
                    inbox.finish(config, claimed, state="failed", error=report.get("error_code"))
                    return report
                if status == "parked":
                    inbox.finish(config, claimed, state="failed",
                                 error=report.get("error_code") or "pending_job")
                    return report
                inbox.finish(config, claimed, state="done", error=None)
        # A window can be parked after its own record was already consumed: the
        # inbox is then empty, so the loop above never resumes it and every later
        # compact reports a clean done while the work sits one window behind. Adopt
        # and drive it, but only when it really covers ground past the newest
        # published window, so a superseded leftover is not spent on twice.
        if not report.get("pending_job"):
            unfinished = _unfinished_beyond(store, chat_id)
            if unfinished and unfinished != state.get("job"):
                state["job"] = unfinished
                _write_state(state_path, state)
            # Finish the open window first: its organize pass owns the stop the
            # next window would otherwise build on.
            calls = _drain_open_job(store, engine, service, transport, chat_id,
                                    state, state_path, report, calls)
        # The source can also simply be longer than what the last run's call
        # budget covered (a huge window split, or rows appended mid-run). Nothing
        # is left in the inbox then, and the plan would otherwise never be
        # re-derived, so resume the tail here instead of waiting for a compact.
        if not report.get("pending_job"):
            calls, status = _advance(store, engine, service, transport, chat_id,
                                     state, state_path, report, calls, options_base)
            if status == "failed":
                return report
        # Even a run with nothing to import may still have stale ledger rows.
        _retire_covered(store, chat_id, state.get("version") or 0)
        inbox.prune_live(config, chat_id, _bound_paths(store, chat_id))
    # A job parked on a ceiling is still open: say so instead of reporting a
    # clean done, otherwise the caller cannot tell a resumable run from a no-op.
    # A call-budget stop is resumable work, not a clean finish: say partial so a
    # caller/UI retries instead of believing the chat is fully understood.
    report.update(state="partial" if report.get("pending_job") or report.get("error_code") == "call_budget"
                  else "done", calls=calls)
    return report


RETIRED_STATES = {"pending", "running", "waiting_retry_limit", "waiting_budget", "waiting_context"}
# A job can also be parked as `ready`/`partial` with only stale tasks left: the
# candidates finished, then a newer base re-planned the same window and nothing
# drives the old organize slices again. Retirement must see those states too, or
# they linger as phantom open jobs for the rest of the chat.
OPEN_STATES = RETIRED_STATES | {"ready", "partial"}


def _retire_covered(store, chat_id, version):
    """Retire windows a newer published version already read past.

    A window that failed and was later re-planned under a new base leaves its
    first job parked with open tasks forever; nobody drives it again because the
    chat state already points at the newer job. Its work is in the narrative, so
    close the ledger entry instead of keeping a phantom "waiting_retry_limit".
    """
    if not version:
        return
    marks = ",".join("?" * len(OPEN_STATES))
    # The job behind the newest version is the parent of the next window even
    # when it still has organize passes left. Retiring it would strand its
    # pending work behind a base that prepare() then rejects.
    newest = store.db.execute(
        "SELECT t.job_id FROM narrative_versions v JOIN understanding_tasks t ON t.id=v.task_id "
        "WHERE v.chat_id=? ORDER BY v.version DESC LIMIT 1", (chat_id,)).fetchone()
    keep = newest["job_id"] if newest else None
    # Published windows: a parked job is redundant once a published window ends
    # at or beyond it. Either another job published the same/older window on a
    # newer base, or a newer narrative already exists and this job lined up as a
    # continuation that nobody drove. A genuinely new window ends later and stays.
    published, latest_base, latest_end = [], 0, ()
    for job, opt in store.db.execute(
            "SELECT t.job_id, j.options_json FROM narrative_versions v "
            "JOIN understanding_tasks t ON t.id=v.task_id "
            "JOIN understanding_jobs j ON j.id=t.job_id WHERE v.chat_id=?", (chat_id,)).fetchall():
        options = json.loads(opt)
        through, base = tuple(options.get("through_order") or ()), options.get("base_version") or 0
        if not through:
            continue
        published.append((base, through, job))
        latest_base, latest_end = max(latest_base, base), max(latest_end, through)
    with store.db:
        for row in store.db.execute(
                f"SELECT id,options_json FROM understanding_jobs WHERE chat_id=? AND state IN ({marks})",
                (chat_id, *sorted(OPEN_STATES))).fetchall():
            if row["id"] == keep:
                continue
            options = json.loads(row["options_json"])
            if not options.get("through_order"):
                continue
            base = options.get("base_version") or 0
            through = tuple(options["through_order"])
            covered = any(other != row["id"] and pbase >= base and pthrough >= through
                          for pbase, pthrough, other in published)
            if not covered and through > latest_end:
                continue
            if not covered and base < latest_base - 0:
                continue
            store.db.execute("UPDATE understanding_attempts SET state='superseded',"
                             "error_code='window_superseded' WHERE job_id=? AND state='inflight'", (row["id"],))
            store.db.execute("UPDATE understanding_tasks SET state='stale' "
                             "WHERE job_id=? AND state IN ('pending','inflight','blocked')", (row["id"],))
            store.db.execute("UPDATE understanding_jobs SET state='superseded',"
                             "error_code='window_superseded' WHERE id=?", (row["id"],))


def _job_state(store, job):
    row = store.db.execute("SELECT state FROM understanding_jobs WHERE id=?", (job,)).fetchone()
    return row["state"] if row else None


def _model_moved(store, job, model):
    row = store.db.execute("SELECT options_json FROM understanding_jobs WHERE id=?", (job,)).fetchone()
    return bool(row) and json.loads(row["options_json"]).get("model") != model


def _retire_model(store, job):
    """Retire a window frozen on a changed alias; returns its window start.

    Every job over the same source window goes too: they are the same work at
    different model aliases, and leaving one behind makes the re-planned target
    collide with it and fail as an orphan window.
    """
    row = store.db.execute("SELECT options_json,target_hash FROM understanding_jobs WHERE id=?", (job,)).fetchone()
    options = json.loads(row["options_json"])
    published = _published_version(store, job)
    window = (options.get("after_order"), options.get("through_order"),
              options.get("after_char"), options.get("through_char"))
    with store.db:
        siblings = [r["id"] for r in store.db.execute(
            "SELECT id,options_json FROM understanding_jobs WHERE state NOT IN "
            "('superseded','retired_policy','ready')").fetchall()
            if (lambda o: (o.get("after_order"), o.get("through_order"),
                           o.get("after_char"), o.get("through_char")) == window)(json.loads(r["options_json"]))]
        for target in {job, *siblings}:
            store.db.execute("UPDATE understanding_attempts SET state='superseded',"
                             "error_code='model_alias_changed' WHERE job_id=? AND state='inflight'", (target,))
            store.db.execute("UPDATE understanding_tasks SET state='stale' "
                             "WHERE job_id=? AND state IN ('pending','inflight','blocked')", (target,))
            store.db.execute("UPDATE understanding_jobs SET state='superseded',"
                             "error_code='model_alias_changed' WHERE id=?", (target,))
    return None if published else options.get("after_order")


def _unfinished_beyond(store, chat_id):
    """Newest open job whose window reaches past the newest published one.

    Only such a job carries work nobody else will read; anything at or behind
    the published end is redundant and belongs to `_retire_covered`.
    """
    latest = store.db.execute(
        "SELECT v.version, j.options_json FROM narrative_versions v "
        "JOIN understanding_tasks t ON t.id=v.task_id JOIN understanding_jobs j ON j.id=t.job_id "
        "WHERE v.chat_id=? ORDER BY v.version DESC LIMIT 1", (chat_id,)).fetchone()
    if latest is None:
        return None
    published_end = tuple(json.loads(latest["options_json"]).get("through_order") or ())
    for row in store.db.execute(
            "SELECT id, options_json FROM understanding_jobs WHERE chat_id=? AND state IN "
            "('pending','running','waiting_retry_limit','waiting_budget','waiting_context') "
            "ORDER BY created_at DESC", (chat_id,)).fetchall():
        options = json.loads(row["options_json"])
        through = tuple(options.get("through_order") or ())
        if options.get("base_version") == latest["version"] and through > published_end:
            return row["id"]
    return None


def _newest_open(store, chat_id):
    """Newest unfinished job plus the window end it already published over."""
    row = store.db.execute(
        "SELECT j.id, j.options_json, (SELECT 1 FROM narrative_versions v "
        "JOIN understanding_tasks t ON t.id=v.task_id WHERE t.job_id=j.id LIMIT 1) published "
        "FROM understanding_jobs j WHERE j.chat_id=? AND j.state IN "
        "('pending','running','waiting_retry_limit','waiting_budget','waiting_context') "
        "ORDER BY j.created_at DESC LIMIT 1", (chat_id,)).fetchone()
    if row is None:
        return None
    through = json.loads(row["options_json"]).get("through_order")
    return row["id"], (through if row["published"] else None)


def _published_parent(store, chat_id):
    """Job that published the newest narrative version, if any."""
    row = store.db.execute(
        "SELECT t.job_id FROM narrative_versions v JOIN understanding_tasks t ON t.id=v.task_id "
        "WHERE v.chat_id=? ORDER BY v.version DESC LIMIT 1", (chat_id,)).fetchone()
    return row["job_id"] if row else None


def _job_through(store, job):
    row = store.db.execute("SELECT options_json FROM understanding_jobs WHERE id=?", (job,)).fetchone()
    return row and json.loads(row["options_json"]).get("through_order")


def _published_version(store, job):
    row = store.db.execute(
        "SELECT v.version FROM narrative_versions v JOIN understanding_tasks t ON t.id=v.task_id "
        "WHERE t.job_id=? ORDER BY v.version DESC LIMIT 1", (job,)).fetchone()
    return row["version"] if row else 0


def _live_parent(store, job):
    """The job that published the current narrative, falling back to the cursor.

    A parked job is not necessarily the newest one: an earlier window can still
    be waiting on capacity while a later published version already carries the
    chat. prepare() rejects any parent that did not publish the latest version,
    so resolve from the narrative rather than trusting the state cursor.
    """
    chat_id = _chat_of(store, job)
    if not chat_id:
        return None
    row = store.db.execute(
        "SELECT t.job_id FROM narrative_versions v JOIN understanding_tasks t ON t.id=v.task_id "
        "WHERE v.chat_id=? ORDER BY v.version DESC LIMIT 1", (chat_id,)).fetchone()
    if row is not None:
        return row["job_id"]
    return None


def _chat_of(store, job):
    if not job:
        return None
    row = store.db.execute("SELECT chat_id FROM understanding_jobs WHERE id=?", (job,)).fetchone()
    return row["chat_id"] if row else None


def _latest_ready(store, chat_id):
    """Job owning the newest version: the only legal parent for a next window."""
    return _published_parent(store, chat_id)


def _bound_paths(store, chat_id):
    return {r[0] for r in store.db.execute(
        "SELECT path FROM source_segments WHERE chat_id=? UNION "
        "SELECT a.path FROM source_archives a JOIN source_snapshots ss ON ss.id=a.snapshot_id "
        "JOIN source_segments s ON s.id=ss.segment_id WHERE s.chat_id=?", (chat_id, chat_id))}


def _organization_left(store, job):
    """Open tasks, or organize slices the published stop still advertises."""
    if _open_tasks(store, job):
        return True
    row = store.db.execute(
        "SELECT 1 FROM narrative_versions v JOIN understanding_tasks t ON t.id=v.task_id "
        "WHERE t.job_id=? AND json_extract(v.body_json,'$.pending_organization_count')>0 LIMIT 1",
        (job,)).fetchone()
    return row is not None


def _open_tasks(store, job):
    return store.db.execute(
        "SELECT 1 FROM understanding_tasks WHERE job_id=? AND state IN ('pending','inflight','blocked') LIMIT 1",
        (job,)).fetchone() is not None


def _drain_open_job(store, engine, service, transport, chat_id, state, state_path, report, calls):
    """Resume an unfinished job left by an earlier run, or return untouched."""
    for _ in range(CALL_BUDGET):
        job = state.get("job")
        if not job or calls >= CALL_BUDGET:
            return calls
        status = engine.status(job)["state"]
        if status in {"stale_source", "retired_policy", "needs_review"}:
            return calls
        if status == "superseded":
            # It can still own the newest version with organize passes left; a
            # retirement race then only flipped the flag. Drive it again.
            if job == _published_parent(store, chat_id) and _organization_left(store, job):
                with store.db:
                    store.db.execute("UPDATE understanding_jobs SET state='pending',"
                                     "error_code=NULL WHERE id=?", (job,))
            else:
                # Its window was replaced while this record waited. Keep the last
                # published version as the base and let the planner re-issue it.
                state.update(job=_latest_ready(store, chat_id))
                _write_state(state_path, state)
                return calls
        # Decide by outstanding work, not by the state string: a worker killed
        # mid-request leaves "running" behind with an inflight attempt, and a
        # status-only check would abandon that window forever. Ceiling states
        # still need driving because _run_job owns their replenish/backoff.
        if (status not in {"waiting_retry_limit", "waiting_budget", "waiting_context"}
                and not _organization_left(store, job)):
            return calls
        row = store.db.execute("SELECT options_json FROM understanding_jobs WHERE id=?", (job,)).fetchone()
        if row is None:
            return calls
        options = Options.from_dict(json.loads(row[0]))
        outcome = _run_job(engine, service, transport, job, options, report)
        calls += outcome["calls"]
        report["calls"] = calls
        if outcome["state"] == "published":
            # Advance the cursor with the window: a parked job that finished
            # later must not make the next compact re-send what it just read.
            state.update(version=outcome.get("version"),
                         cursor=list(options.through_order) if options.through_order else state.get("cursor"),
                         windows=state.get("windows", 0) + 1)
            _write_state(state_path, state)
            if not report["windows"] or report["windows"][-1].get("pending") == 0:
                return calls
            continue
        report.setdefault("pending_job", {"job": job, "state": outcome["state"],
                                          "error_code": outcome.get("error_code")})
        report["state"] = "partial"
        return calls
    return calls


def _open_job(store, chat_id, field):
    rows = store.db.execute(
        "SELECT id,options_json FROM understanding_jobs WHERE chat_id=? AND state NOT IN "
        "('retired_policy','superseded') ORDER BY created_at DESC LIMIT 20", (chat_id,)
    ).fetchall()
    for row in rows:
        saved = json.loads(row["options_json"])
        if (saved.get("after_order"), saved.get("through_order"),
                saved.get("after_char"), saved.get("through_char")) == (
                field["after_order"], field["through_order"],
                field["after_char"], field["through_char"]):
            return row["id"]
    return None


def _fresh_rows(store, chat_id, cursor):
    """Split new physical rows into model work and already-understood rows.

    A compact writes a whole new physical file that begins with the transcript
    already understood, so every later segment repeats prior records and seats
    them *after* the global cursor. Each segment's leading run of already-covered
    raw records is therefore reported as `skipped` instead of being sent again;
    anything after that run is fresh (deeper history, or a rewritten tail).
    Records emitted from an earlier pending segment also join the covered set, so
    two compact files queued together cannot deliver the same turn twice. Raw
    hashes are only consulted for that leading run, so a user genuinely repeating
    the same sentence in a later turn still counts as new work.
    """
    rows = store.db.execute(
        "SELECT * FROM current_sources WHERE chat_id=? ORDER BY ordinal,record_no", (chat_id,)
    ).fetchall()
    if cursor is None:
        return rows, []
    seen = {r["raw_sha256"] for r in rows if (r["ordinal"], r["record_no"]) <= cursor}
    fresh, skipped = [], []
    leading, segment = True, None
    for row in rows:
        if (row["ordinal"], row["record_no"]) <= cursor:
            continue
        if row["segment_id"] != segment:
            segment, leading = row["segment_id"], True
        if leading and row["raw_sha256"] in seen:
            skipped.append((row["ordinal"], row["record_no"]))
            continue
        leading = False
        seen.add(row["raw_sha256"])
        fresh.append(row)
    return fresh, skipped


def _advance(store, engine, service, transport, chat_id, state, state_path, report, calls, options_base):
    """Read every fresh source row past the cursor and publish it window by window.

    Runs both for a just-imported record and, with no record at all, for a tail a
    previous run left behind (call budget, kill). Publication is per window, so a
    stop keeps each finished window readable and the cursor points at it.
    """
    cursor = _after(state.get("cursor"))
    fresh, skipped = _fresh_rows(store, chat_id, cursor)
    if not fresh:
        return calls, "idle"
    parent = _live_parent(store, state.get("job"))
    # Whole native turns per window; a window is published before the next one
    # starts, so a failure keeps every finished window readable.
    plan = plan_windows(fresh, WINDOW_CHARS)
    # Compact files restart at the transcript head, so the understood prefix sits
    # at positions *after* the cursor. A window starts just past the repeated run
    # and carries the skipped rows explicitly.
    floor = cursor
    first = (fresh[0]["ordinal"], fresh[0]["record_no"])
    earlier = [p for p in skipped if p < first]
    if earlier and (floor is None or earlier[-1] > floor):
        floor = earlier[-1]
    queued = list(plan)
    while queued:
        window = queued[0]
        if calls >= CALL_BUDGET:
            report.update(state="partial", error_code="call_budget")
            return calls, "calls"
        head = store.db.execute(
            "SELECT version,override_revision FROM narrative_heads WHERE chat_id=?", (chat_id,)
        ).fetchone()
        field = dict(window)
        if field["after_order"] is None:
            field["after_order"] = floor
        start = tuple(field["after_order"]) if field["after_order"] else None
        skip = tuple(p for p in skipped
                     if (start is None or p > start) and p <= tuple(field["through_order"]))
        options = Options(
            **options_base, base_version=head[0] if head else 0,
            override_revision=head[1] if head else 0,
            parent_job_id=parent, after_order=field["after_order"],
            through_order=field["through_order"], after_char=field["after_char"],
            through_char=field["through_char"], bridge=tuple(field["bridge"]),
            skip=skip, bounded_history=field["after_order"] is not None,
        )
        limits = Limits(total_input_tokens=900000, total_output_tokens=300000,
                        call_count=CALL_BUDGET * 2, wall_seconds=6 * 3600, retry_limit=RETRY_ATTEMPTS)
        try:
            job = engine.prepare(chat_id, options, limits)["job_id"]
        except ValueError as exc:
            if str(exc) == "window_parent_organization_incomplete" and parent:
                # The parent still owes organize passes (a long turn published in
                # several slices). Finish those, then plan this window again.
                state["job"] = parent
                _write_state(state_path, state)
                calls = _drain_open_job(store, engine, service, transport, chat_id,
                                        state, state_path, report, calls)
                if report.get("pending_job"):
                    return calls, "parked"
                parent = _live_parent(store, state.get("job"))
                continue
            if str(exc) != "target_already_has_options_use_existing_job":
                report.update(state="failed", error_code=str(exc)[:80])
                return calls, "failed"
            # A previous run froze this exact window with different options (for
            # example another model). Its budget and progress are authoritative,
            # so resume that job.
            job = _open_job(store, chat_id, field)
            if job is None:
                report.update(state="failed", error_code="orphan_window")
                return calls, "failed"
            options = Options.from_dict(json.loads(
                store.db.execute("SELECT options_json FROM understanding_jobs WHERE id=?", (job,)).fetchone()[0]
            ))
        outcome = _run_job(engine, service, transport, job, options, report)
        calls += outcome["calls"]
        report["calls"] = calls
        if outcome["state"] != "published":
            report.setdefault("pending_job", {"job": job, "state": outcome["state"],
                                              "error_code": outcome.get("error_code")})
            if outcome.get("error_code"):
                report["error_code"] = outcome["error_code"]
            report["state"] = "partial"
            return calls, "parked"
        parent = job
        # Advance to what this window actually read, not to the tail of the file:
        # a compact file can hold more rows than one plan covered (a long turn
        # split into chunks, or records appended while the window ran). Jumping
        # to the tail silently skips the rest because the next run sees nothing
        # fresh.
        state.update(cursor=list(field["through_order"]), job=job,
                     version=outcome.get("version"),
                     windows=state.get("windows", 0) + 1)
        _write_state(state_path, state)
        queued.pop(0)
    return calls, "done"


def _run_job(engine, service, transport, job, options, report):
    """Drive one window to a published version, or return the stopping state."""
    calls = 0
    retried = 0
    options_budget = {"total_input_tokens": 900000, "total_output_tokens": 300000,
                      "call_count": CALL_BUDGET * 2, "wall_seconds": 6 * 3600,
                      "retry_limit": RETRY_ATTEMPTS}
    waited = 0
    waited_retry = 0
    context_passes = 0
    blocked_passes = 0
    while True:
        claim = engine.claim(job, backend=options.backend)
        if claim["state"] == "inflight":
            # Another worker (or a run that was killed inside the lease) owns the
            # request. Wait it out instead of failing the compact immediately.
            if waited >= options.lease_seconds:
                return {"state": "inflight", "error_code": "lease_still_held", "calls": calls}
            time.sleep(5)
            waited += 5
            continue
        if claim["state"] == "request":
            response = transport.complete(claim["request"])
            calls += 1
            if response.get("usage") is None:
                response["usage"] = None
            engine.submit(claim["attempt_id"], backend=options.backend,
                          output=response.get("output"), usage=response.get("usage"),
                          finish=response.get("finish"))
            if response.get("error"):
                report.setdefault("errors", []).append(response["error"])
                report["last_error_at"] = time.time()
            continue
        if claim["state"] == "waiting_retry_limit":
            # Each worker run gets its own bounded retry allowance, so a compact
            # that once exhausted its retries is not parked permanently; the
            # ceiling stops a broken route from spending without bound.
            current = engine.status(job)["limits"]
            if retried >= RETRY_CEILING or waited_retry >= RETRY_WAIT_BUDGET:
                return {"state": "waiting_retry_limit", "error_code": "retry_ceiling", "calls": calls}
            # A gateway 502/cooldown recovers on its own; without this pause the
            # whole retry allowance burns in a second and the compact is parked.
            pause = min(RETRY_BACKOFF * (2 ** retried), RETRY_BACKOFF_CAP)
            time.sleep(pause)
            waited_retry += pause
            retried += 1
            engine.replenish(job, Limits(**{**current, "retry_limit": current["retry_limit"] + 1}))
            continue
        if claim["state"] == "waiting_budget":
            # A worker run owns its own ceiling: raise it to the run budget so an
            # older frozen job (from a cheaper model) can finish. The source
            # target and every consumed token stay on the same ledger.
            current = engine.status(job)["limits"]
            # Add a full run budget: a resumed job may already have spent the
            # previous ceiling, so max() alone would never admit another call.
            raised = {key: current[key] + options_budget[key] for key in options_budget}
            engine.replenish(job, Limits(**raised))
            continue
        if claim["state"] == "waiting_context":
            # waiting_context returns before the request is measured, and raising
            # token budgets does not create capacity. Re-queue it so claim
            # re-measures, and park after a couple of passes that spend nothing —
            # otherwise a window that genuinely does not fit loops forever.
            context_passes += 1
            if context_passes > 2:
                return {"state": "waiting_context", "error_code": "context_ceiling",
                        "calls": calls}
            engine.repend_context(job)
            continue
        # "pending" here means no *pending task* is left: either extraction is
        # done and organization has not been planned, or the only task is
        # blocked and was just re-queued. Both need prepare(), not a return.
        if claim["state"] not in {"ready", "partial", "pending", "blocked"}:
            return {"state": claim["state"], "error_code": claim.get("error_code"), "calls": calls}
        try:
            task = service.prepare(job)
        except ValueError as exc:
            return {"state": "blocked", "error_code": str(exc)[:80], "calls": calls}
        if task["state"] == "blocked":
            # prepare() already sliced to its smallest admissible batch and it
            # still did not fit, so a second pass cannot shrink it further. Park
            # instead of spinning: claim() keeps reporting the job as pending
            # because the blocked task is not a pending one.
            blocked_passes += 1
            if blocked_passes > 1:
                return {"state": "blocked", "error_code": "context_ceiling", "calls": calls}
            continue
        if task["state"] != "done":
            continue
        view = service.publish(task["task_id"])
        report["windows"].append({"job": job, "version": view["version"],
                                  "pending": view["narrative"].get("pending_organization_count", 0)})
        if view["narrative"].get("pending_organization_count", 0):
            continue
        return {"state": "published", "version": view["version"], "calls": calls}


def _lock(config, chat_id):
    folder = Path(config["_path"]).parent / "maintain"
    folder.mkdir(parents=True, exist_ok=True)
    handle = FileLease(folder / f"{chat_id}.lock")
    if not handle.acquire(blocking=False):
        handle.close()
        return None
    return handle


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="seywo.maintain")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--chat", required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except (OSError, ValueError):
        maintenance_status.safe_record(args.config, args.chat, 'blocked', error_code='config_invalid')
        return 1
    if (config.get("hook") or {}).get("enabled") is not True:
        return 0
    try:
        report = run_chat(config, args.chat)
    except Exception:  # run_chat records failure while it still owns the chat lock
        report = {"state": "failed", "error_code": "maintainer_exception"}
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get("state") in {"done", "idle", "partial", "busy"} else 1


if __name__ == "__main__":
    sys.exit(main())
