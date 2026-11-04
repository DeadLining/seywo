"""Explicit, local, lossless maintenance. No provider or extraction entry points."""
from contextlib import contextmanager
from . import file_lock as fcntl
from pathlib import Path
import time

from .core import Store, load_config
from . import storage, continuation


@contextmanager
def project_lock(config):
    from .stage_leases import quiescent
    with quiescent(config), (Path(config['database']).parent / 'graph-v2.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('maintenance_busy') from None
        yield lock


@contextmanager
def task_cleanup_lock(config):
    """Selective orchestration cleanup needs a short writer window, not idle AI.

    Leased workers release graph-v2.lock during transport. Holding it prevents
    selecting/changing stages while the transaction dismisses only blocked jobs.
    We leave active attempts/leases and graph dependencies untouched, so an
    in-flight response can still commit. Compaction keeps its quiescent guard.
    Legacy workers lack these fences and retain the original exclusion.
    """
    from .stage_leases import enabled, locked
    if enabled(config):
        with locked(Path(config['database']).parent / 'graph-v2.lock'):
            yield
        return
    runs = Path(config['database']).parent / 'hook-state'
    runs.mkdir(parents=True, exist_ok=True)
    with (runs / 'update.lock').open('a') as worker_lock:
        try:
            fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('maintenance_busy') from None
        with project_lock(config):
            yield


def footprint(store):
    db = store.db
    page_size = db.execute('PRAGMA page_size').fetchone()[0]
    page_count = db.execute('PRAGMA page_count').fetchone()[0]
    free = db.execute('PRAGMA freelist_count').fetchone()[0]
    path = Path(store.config['database'])
    sizes = {name: path.with_name(path.name+suffix).stat().st_size if path.with_name(path.name+suffix).exists() else 0
             for name,suffix in [('database_bytes',''),('wal_bytes','-wal')]}
    return dict(sizes, logical_bytes=page_size*page_count, reclaimable_bytes=free*page_size)


def counts(store):
    # Facts and provenance must survive. Caches/physical layout may change.
    names = ('entries','work_memory','sg2_nodes','sg2_relations','sg2_evidence',
             'sg2_relation_evidence','sg2_jobs','sg2_attempts','sg2_cursors','sg_input_coverage')
    return {table:store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
            for table in names if storage.exists(store.db,table)}


# Do not rewrite gigabytes to reclaim a handful of pages. Payload compression is
# independent of VACUUM; the free pages remain reusable by subsequent writes.
MIN_VACUUM_BYTES = 8 * 1024 * 1024
COMPACTION_SECONDS = 240  # Reclamation budget below the desktop's 300-second deadline.
PAYLOAD_TABLES = ('sg2_jobs', 'sg2_job_parts', 'sg_payload_archive', 'sg2_checkpoints')


def check_payload_tables(store):
    """Verify the modified B-trees, not every source/FTS page in the database.

    archive_text additionally verifies exact decompression and SHA256 for every
    changed value; fact/provenance counts are checked in the same transaction.
    This is deliberately not a claim of a full-database integrity audit.
    """
    checked = []
    for table in PAYLOAD_TABLES:
        if storage.exists(store.db, table):
            if any(row[0] != 'ok' for row in store.db.execute(f"PRAGMA quick_check('{table}')")):
                raise ValueError('maintenance_integrity_failed')
            checked.append(table)
    return checked


def reclaim_space(store, deadline):
    """Best-effort physical reclamation after the lossless transaction commits."""
    import sqlite3
    db = store.db
    timeout = db.execute('PRAGMA busy_timeout').fetchone()[0]
    db.execute('PRAGMA busy_timeout=1000')
    db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    result = dict(warning='', vacuum_performed=False, vacuum_skipped_reason='')
    try:
        if time.monotonic() >= deadline:
            result.update(warning='载荷处理已完成；本次磁盘回收时限已到，可稍后再次压缩。',
                          vacuum_skipped_reason='reclamation_deferred')
            return result
        # PASSIVE never waits for a long reader to release its snapshot.
        checkpoint = db.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
        if checkpoint[0] or checkpoint[1] != checkpoint[2]:
            result.update(warning='载荷处理已完成；索引正在被读取，日志空间将在读事务结束后回收。',
                          vacuum_skipped_reason='active_reader')
            return result
        footprint_now = footprint(store)
        minimum = max(MIN_VACUUM_BYTES, footprint_now['logical_bytes'] // 100)
        if footprint_now['reclaimable_bytes'] >= minimum:
            if time.monotonic() >= deadline:
                result.update(warning='载荷处理已完成；本次磁盘回收时限已到，可稍后再次压缩。',
                              vacuum_skipped_reason='reclamation_deferred')
                return result
            db.execute('VACUUM')  # SQLite-atomic; interruption retains committed data.
            result['vacuum_performed'] = True
        else:
            result['vacuum_skipped_reason'] = 'small_freelist'
        checkpoint = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if checkpoint[0]:
            result['warning'] = '载荷处理已完成；索引正在被读取，日志空间将在读事务结束后回收。'
    except sqlite3.OperationalError as exc:
        # Busy readers/deadlines are deferrals, not proof of success or corruption.
        # Disk-full, malformed database and unexpected errors must remain errors.
        if (getattr(exc, 'sqlite_errorcode', 0) & 255) not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_INTERRUPT}:
            raise
        result.update(warning='载荷处理已完成；磁盘回收达到本次时限或索引正忙，可稍后再次压缩。',
                      vacuum_skipped_reason='reclamation_deferred')
    finally:
        db.set_progress_handler(None, 0)
        db.execute(f'PRAGMA busy_timeout={timeout}')
    return result


def run(config_path, action):
    config = load_config(Path(config_path))
    started = time.monotonic()
    if action == 'storage_preview':
        store = Store(config,readonly=True)
        try:
            return dict(before=footprint(store),counts=counts(store),model_calls=0)
        finally:
            store.close()
    if action == 'clear_tasks':
        return clear_tasks(config)
    if action not in {'storage_compact','clean_notices'}:
        raise ValueError('invalid_maintenance_action')
    with project_lock(config):
        if action == 'clean_notices':
            return continuation.clean_completed(config)
        store = Store(config)
        try:
            before = footprint(store)
            with store.db:
                store.db.execute('BEGIN IMMEDIATE')
                preserved = counts(store)
                cache = storage.prune_completed_checkpoints(store)
                archive = storage.archive_completed_payloads(store)
                if counts(store) != preserved:
                    raise ValueError('maintenance_fact_count_changed')
                checked = check_payload_tables(store)
            reclamation = reclaim_space(store, started + COMPACTION_SECONDS)
            after = footprint(store)
            return dict(before=before,after=after,counts=preserved,archive=archive,cache=cache,
                        reclaimed_bytes=max(0,before['database_bytes']+before['wal_bytes']-after['database_bytes']-after['wal_bytes']),
                        integrity='ok',integrity_scope=checked,**reclamation,
                        model_calls=0,elapsed_seconds=round(time.monotonic()-started,3))
        finally:
            store.close()


def dismiss_graph_batches(config, now):
    """Archive blocked V2 orchestration, including pre-control legacy failures.

    Never delete sources, facts or coverage.

    Caller holds the project writer locks. Keep reservations so later hooks
    cannot recreate the discarded input; new input remains eligible. The small
    before receipt permits restoration without copying large saved payloads.
    """
    import json
    if not Path(config['database']).is_file():
        return 0
    from .run_receipts import connection
    from .stages import CONTROL_STATE_SQL
    with connection(config, readonly=False) as db:
        if not all(storage.exists(db, name) for name in ('sg2_jobs', 'sg2_control')):
            return 0
        project = db.execute("SELECT value FROM metadata WHERE key='project_id'").fetchone()
        if not project or project[0] != config['project_id']:
            raise ValueError('database project_id does not match configuration')
        active = ("AND NOT EXISTS (SELECT 1 FROM sg2_attempts a WHERE a.job=j.id AND a.state='running')"
                  if storage.exists(db, 'sg2_attempts') else '')
        if storage.exists(db, 'sg2_stage_lease'):
            # A lease exists before the attempt row is inserted. Protect that
            # window as well, including explicitly retried formerly blocked jobs.
            active += " AND NOT EXISTS (SELECT 1 FROM sg2_stage_lease l WHERE l.job=j.id AND l.state='running')"
        with db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute(f"""SELECT j.id,j.state job_state,j.updated_at,c.* FROM sg2_jobs j
                LEFT JOIN sg2_control c ON c.job=j.id WHERE j.project=? AND {CONTROL_STATE_SQL}='blocked'
                AND j.state NOT IN ('complete','superseded','dismissed') {active}""",
                (config['project_id'],)).fetchall()
            if not rows:
                return 0
            db.execute('''CREATE TABLE IF NOT EXISTS sg2_task_dismissals(
                job TEXT PRIMARY KEY REFERENCES sg2_jobs(id) ON DELETE CASCADE,
                project TEXT NOT NULL,dismissed_at REAL NOT NULL,before_json TEXT NOT NULL)''')
            for row in rows:
                db.execute('INSERT INTO sg2_task_dismissals VALUES(?,?,?,?)',
                           (row['id'],config['project_id'],now,json.dumps(dict(row),ensure_ascii=False)))
                db.execute("UPDATE sg2_jobs SET state='dismissed' WHERE id=?", (row['id'],))
                db.execute("UPDATE sg2_control SET state='dismissed' WHERE job=?", (row['id'],))
            return len(rows)


def clear_tasks(config):
    """Dismiss obsolete orchestration, retain facts and exact historical receipts.

    Selective writer and queue locks serialize dismissal with dispatch/record;
    leased network requests may continue. Dismissed entries fence late receipts,
    not future events. Legacy workers still require quiescence.
    """
    from .auto_update import _folder, _pid_alive, ACTIVE, DEADLINE
    import json
    import os
    runs = _folder(config)
    runs.parent.mkdir(parents=True, exist_ok=True)
    with task_cleanup_lock(config):
        with continuation.locked(continuation.root(config)):
            folder = continuation.root(config)
            path = folder / 'queue.json'
            queue = continuation.read(path, {})
            now = time.time()
            dismissed_batches = dismiss_graph_batches(config, now)
            dismissed = archived = unreadable = 0
            for item in queue.values():
                if (Path(item['config']).resolve() != Path(config['_config_path']).resolve()
                        or item.get('state') != 'blocked'):
                    continue
                item.update(state='dismissed', dismissed_at=now, dismissal_reason='user_cleanup')
                dismissed += 1
            if dismissed:
                continuation.write(path, queue)
            archive = runs.parent / 'cleared-runs'
            for receipt in runs.glob('*.json'):
                try:
                    data = json.loads(receipt.read_text())
                    state = data.get('state')
                    stale = (state in ACTIVE and now-float(data['updated_at']) > DEADLINE+60
                             and not _pid_alive(data.get('pid')))
                    if state not in {'failed','stalled','cancelled'} and not stale:
                        continue
                    archive.mkdir(parents=True, exist_ok=True, mode=0o700)
                    target = archive / receipt.name
                    if target.exists():
                        # Never overwrite a different audit record on a retry.
                        if target.read_bytes() != receipt.read_bytes():
                            unreadable += 1
                            continue
                        receipt.unlink()
                    else:
                        os.replace(receipt, target)
                    archived += 1
                except (OSError, ValueError, TypeError, KeyError, AttributeError):
                    unreadable += 1
            from . import run_receipts
            if Path(config['database']).is_file():
                with run_receipts.connection(config, readonly=False) as db:
                    with db:
                        archived += run_receipts.dismiss_failures(db, config['project_id'])
            return dict(dismissed_tasks=dismissed, archived_runs=archived,
                        dismissed_batches=dismissed_batches, unreadable_runs=unreadable, model_calls=0)


def maintain_receipts(config, **options):
    """Explicit, bounded archival; keep active import/extraction out of the migration."""
    from . import run_receipts
    if not options.get('apply'):
        return run_receipts.maintain(config, **options)
    path = Path(config['database']).parent / 'hook-state' / 'update.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('maintenance_busy') from None
        with project_lock(config):
            return run_receipts.maintain(config, **options)


def maintain_sources(config, **options):
    """No Store startup migrations: only explicit retention schema may change."""
    from . import source_retention
    if not options.get('apply'):
        store = Store(config, readonly=True)
        try:
            return source_retention.maintain(store, **options)
        finally:
            store.close()
    path = Path(config['database']).parent / 'hook-state' / 'update.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('maintenance_busy') from None
        with project_lock(config):
            # Use the read-only Store's validation without unrelated backfills.
            store = Store(config, readonly=True)
            store.db.close()
            from .run_receipts import connection
            with connection(config, readonly=False) as db:
                project = db.execute("SELECT value FROM metadata WHERE key='project_id'").fetchone()
                if not project or project[0] != config['project_id']:
                    raise ValueError('database project_id does not match configuration')
                db.execute('PRAGMA foreign_keys=ON')
                store.db, store.readonly = db, False
                from .source_text import register
                register(store)
                return source_retention.maintain(store, **options)


def externalize_sources(config, **options):
    """Explicit source-verified batches; ordinary Hooks never migrate history."""
    from .source_text import migrate
    if not options.get('apply', False):
        store = Store(config, readonly=True)
        try:
            return migrate(store, **options)
        finally:
            store.close()
    with project_lock(config):
        store = Store(config)
        try:
            return migrate(store, **options)
        finally:
            store.close()


def optimize_legacy(config, **options):
    from .legacy_storage import migrate
    if not options.get('apply', False):
        store = Store(config, readonly=True)
        try:
            return migrate(store, **options)
        finally:
            store.close()
    path = Path(config['database']).parent / 'hook-state' / 'update.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as lock, project_lock(config):
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = Store(config)
        try:
            return migrate(store, **options)
        finally:
            store.close()
