"""Explicit, local, lossless maintenance. No provider or extraction entry points."""
from contextlib import contextmanager
import fcntl
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
                integrity = store.db.execute('PRAGMA quick_check').fetchone()[0]
                if integrity != 'ok':
                    raise ValueError('maintenance_integrity_failed')
            # VACUUM is SQLite-atomic. A busy reader is harmless; committed
            # payload compression stays valid even if reclamation is deferred.
            import sqlite3
            warning = ''
            try:
                store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                if store.db.execute('PRAGMA freelist_count').fetchone()[0]:
                    store.db.execute('VACUUM')
                checkpoint = store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                if checkpoint[0]:warning = '索引正在被读取，日志空间将在读事务结束后回收。'
            except sqlite3.OperationalError:
                warning = '载荷已压缩；磁盘空间回收暂未完成，可稍后再次压缩。'
            after = footprint(store)
            return dict(before=before,after=after,counts=preserved,archive=archive,cache=cache,
                        reclaimed_bytes=max(0,before['database_bytes']+before['wal_bytes']-after['database_bytes']-after['wal_bytes']),
                        integrity=integrity,warning=warning,model_calls=0,elapsed_seconds=round(time.monotonic()-started,3))
        finally:
            store.close()


def clear_tasks(config):
    """Dismiss obsolete orchestration, retain facts and exact historical receipts.

    update.lock excludes an importer/extractor; queue.lock makes dismissal atomic
    with dispatch/record. Dismissed entries fence late receipts, not future events.
    """
    from .auto_update import _folder, _pid_alive, ACTIVE, DEADLINE
    import json
    import os
    runs = _folder(config)
    runs.parent.mkdir(parents=True, exist_ok=True)
    with (runs.parent / 'update.lock').open('a') as worker_lock:
        try:
            fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('maintenance_busy') from None
        with project_lock(config), continuation.locked(continuation.root(config)):
            folder = continuation.root(config)
            path = folder / 'queue.json'
            queue = continuation.read(path, {})
            now = time.time()
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
                        unreadable_runs=unreadable, model_calls=0)


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
                return source_retention.maintain(store, **options)
