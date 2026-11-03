"""Finite, event-driven historical tier migration. No model calls or read-side writes.

App startup and eligible hooks wake a single low-priority worker. Each transaction
projects at most 100 missing/stale rows, under the normal writer/idle locks. The
tier table itself checkpoints committed progress; the tiny queue survives a crash.
"""
from __future__ import annotations

from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

from .core import Store, load_config
from . import tiering
from .stage_leases import locked

BATCH_SIZE = 100
BUSY_WAITS = (1, 2, 4, 8, 16, 30)


def _pending(store, limit):
    if not tiering.available(store):
        return [r[0] for r in store.db.execute(
            'SELECT id FROM work_memory WHERE project_id=? ORDER BY id LIMIT ?',
            (store.config['project_id'], limit))]
    return [r[0] for r in store.db.execute('''SELECT m.id FROM work_memory m
        WHERE m.project_id=? AND NOT EXISTS(SELECT 1 FROM sg_node_tiers t
        WHERE t.node=m.id AND t.project=m.project_id AND t.revision=?)
        ORDER BY m.id LIMIT ?''', (store.config['project_id'], tiering.VERSION, limit))]


def batch(config_path, limit=BATCH_SIZE):
    """Only derived tier rows and the completion revision may change."""
    if type(limit) is not int or not 1 <= limit <= BATCH_SIZE:
        raise ValueError('invalid tier backfill batch size')
    config_path = Path(config_path)
    config = load_config(config_path)
    if (config_path.parent / 'disabled').exists():
        return dict(processed=0, more=False, skipped=True, model_calls=0)
    # First inspect read-only: completed/empty projects never open a writer.
    try:
        store = Store(config, readonly=True)
        try:
            if not _pending(store, 1):
                return dict(processed=0, more=False, model_calls=0)
        finally:
            store.close()
    except sqlite3.OperationalError as exc:
        if not any(word in str(exc).lower() for word in ('locked', 'busy')):
            raise
        return dict(processed=0, more=True, busy=True, model_calls=0)
    from .maintenance import project_lock
    folder = Path(config['database']).parent
    try:
        with ExitStack() as stack:
            stack.enter_context(locked(folder / 'hook-state/update.lock', nonblocking=True))
            stack.enter_context(project_lock(config))
            store = Store(config, projection_only=True)
            stack.callback(store.close)
            store.db.execute('PRAGMA busy_timeout=100')
            deadline = time.monotonic() + 2
            store.db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            with store.db:
                store.db.execute('BEGIN IMMEDIATE')
                tiering.setup(store)
                ids = _pending(store, limit)
                processed = tiering.project(store, ids)
                more = bool(_pending(store, 1))
                if not more:
                    store.db.execute("INSERT OR REPLACE INTO metadata VALUES('tiering_revision',?)", (tiering.VERSION,))
            return dict(processed=processed, more=more, model_calls=0)
    except BlockingIOError:
        return dict(processed=0, more=True, busy=True, model_calls=0)
    except ValueError as exc:
        if str(exc) != 'maintenance_busy':
            raise
        return dict(processed=0, more=True, busy=True, model_calls=0)
    except sqlite3.OperationalError as exc:
        if not any(word in str(exc).lower() for word in ('locked', 'busy', 'interrupted')):
            raise
        return dict(processed=0, more=True, busy=True, model_calls=0)


def _folder(state):
    return Path(state) / 'tier-backfill'


def _read(folder):
    path = folder / 'queue.json'
    if not path.exists():
        return {}
    if path.stat().st_size > 256 * 1024:
        raise ValueError('tier queue too large')
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or len(value) > 1000:
        raise ValueError('invalid tier queue')
    return value


def _write(folder, queue):
    from .auto_update import _atomic
    _atomic(folder / 'queue.json', queue)


def enqueue(state, paths):
    folder = _folder(state)
    paths = [str(Path(p).resolve()) for p in paths]
    if not paths:
        return
    with locked(folder / 'queue.lock'):
        queue = _read(folder)
        for path in paths:
            queue[path] = dict(token=uuid.uuid4().hex, state='ready')
        _write(folder, queue)


def start(config_path, state=None):
    """Hook wakeup only; never called by MCP/UI read operations."""
    if state is None:
        from .provider_credentials import runtime_state
        state = runtime_state()
    # Completed projects cause no queue writes or child processes on future hooks.
    config = load_config(Path(config_path))
    if not Path(config['database']).is_file() or (Path(config_path).parent / 'disabled').exists():
        return
    try:
        store = Store(config, readonly=True)
        try:
            if not _pending(store, 1):
                return
        finally:
            store.close()
    except sqlite3.OperationalError:
        pass  # A temporary reader lock still needs a durable wakeup.
    enqueue(state, [config_path])
    folder = _folder(state)
    # Coalesce normal hook bursts without spawning another active worker.
    with (folder / 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
    source = Path(__file__).resolve().parents[1]
    subprocess.Popen([sys.executable, '-B', '-m', 'sessiongraph.tier_backfill', str(state)],
        cwd=source, env={**os.environ, 'PYTHONPATH':str(source), 'PYTHONDONTWRITEBYTECODE':'1'},
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)


def enqueue_registered(state):
    """Use the same registered-project identity checks as native settings."""
    from .native_settings import _json, _project
    from .core import digest
    paths = []
    for item in _json(Path(state) / 'projects.json').get('projects', []):
        try:
            root = Path(item['root']).resolve()
            path = _project(Path(state), digest(str(root))[:20])
            if path and (path.parent / 'config.json').is_file():
                paths.append(path)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    enqueue(state, paths)


def run(state, *, wait=time.sleep):
    """Round-robin batches; busy projects yield, completed queues terminate."""
    folder = _folder(state)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.nice(10)
    except OSError:
        pass
    processed, retries = 0, 0
    with (folder / 'worker.lock').open('a') as singleton:
        try:
            fcntl.flock(singleton, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(processed=0, running=True, model_calls=0)
        while True:
            with locked(folder / 'queue.lock'):
                jobs = {p:v for p,v in _read(folder).items() if v.get('state') == 'ready'}
                if not jobs:
                    # Release under queue lock, closing enqueue-vs-exit wakeup race.
                    fcntl.flock(singleton, fcntl.LOCK_UN)
                    return dict(processed=processed, model_calls=0)
            progress = False
            for path, claimed in jobs.items():
                try:
                    result = batch(path)
                except (OSError, ValueError, sqlite3.Error):
                    result = dict(processed=0, more=True, failed=True)
                processed += result['processed']
                progress |= bool(result['processed'])
                with locked(folder / 'queue.lock'):
                    queue = _read(folder)
                    if queue.get(path, {}).get('token') != claimed['token']:
                        continue
                    if result.get('failed'):
                        queue[path]['state'] = 'blocked'
                    elif not result['more']:
                        queue.pop(path, None)
                    _write(folder, queue)
            if progress:
                retries = 0
                wait(0.05)  # Yield between bounded transactions, with actual work pending.
            else:
                with locked(folder / 'queue.lock'):
                    queue = _read(folder)
                    ready = [p for p,v in queue.items() if v.get('state') == 'ready']
                    if ready and retries >= len(BUSY_WAITS):
                        for p in ready:
                            queue[p]['state'] = 'waiting'
                        _write(folder, queue)
                        ready = []
                if ready:
                    wait(BUSY_WAITS[retries])
                    retries += 1


if __name__ == '__main__':
    print(json.dumps(run(Path(sys.argv[1]))))
