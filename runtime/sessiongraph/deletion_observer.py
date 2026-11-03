"""Project-scoped permanent-delete events; kernel waits, no history sweeps/models.

SessionEnd is a runtime lifecycle event, not a universal deletion event. Desktop
deletion markers supply the missing signal; they never authorize erasure alone.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import select
import sqlite3
import subprocess
import sys
import time

from . import chat_cleanup
from .core import Store, load_config
from .failures import describe, safe_detail

PREFIX = 'codex-writing-block-deleted-thread-v1:'
CHAT = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')
MAX_STATE_BYTES = 8 * 1024 * 1024


def home():
    return Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))


def deleted_ids(path):
    with path.open('rb') as stream:
        data = stream.read(MAX_STATE_BYTES + 1)
    if len(data) > MAX_STATE_BYTES:
        raise ValueError('desktop state exceeds observer limit')
    atoms = json.loads(data)['electron-persisted-atom-state']
    if not isinstance(atoms, dict):
        raise ValueError('invalid desktop deletion state')
    return {key[len(PREFIX):] for key, value in atoms.items()
            if key.startswith(PREFIX) and value is True and CHAT.fullmatch(key[len(PREFIX):])}


def active(config_path, config):
    registration = home() / 'sessiongraph.json'
    if registration.is_file() and json.loads(registration.read_text()).get('enabled') is not True:
        return False
    return (chat_cleanup.enabled(config)
            and not (Path(config_path).parent / 'disabled').exists())


def check(config_path, chat, ids, *, codex_home=None):
    """Explicit tombstone + absent backend row + original cleanup safety checks."""
    result = {'chat_id': chat, 'cleaned': False, 'model_calls': 0}
    if not CHAT.fullmatch(chat) or chat not in ids:
        return {**result, 'reason': 'no_delete_marker'}
    cfg = load_config(Path(config_path))
    if not active(config_path, cfg):
        return {**result, 'reason': 'disabled'}
    # A desktop marker is not sufficient when the backend still has this chat.
    state = (codex_home or home()) / 'state_5.sqlite'
    db = sqlite3.connect(state.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        if db.execute('SELECT 1 FROM threads WHERE id=?', (chat,)).fetchone():
            return {**result, 'reason': 'codex_chat_present'}
    finally:
        db.close()
    reader = Store(cfg, readonly=True)
    try:
        row = reader.db.execute('''SELECT s.path FROM sessions s JOIN session_chats c
            ON c.segment_id=s.id WHERE c.chat_id=? ORDER BY s.id LIMIT 1''', (chat,)).fetchone()
    finally:
        reader.close()
    if row is None:
        return {**result, 'reason': 'unindexed_source'}
    return chat_cleanup.cleanup(config_path, chat, row['path'])


def folder(config_path):
    return Path(config_path).resolve().parent / 'hook-state'


def receipt(config_path, name, value):
    from .auto_update import _atomic
    _atomic(folder(config_path) / name, value)


def failure_result(chat, exc):
    info = describe(exc)
    detail = safe_detail(info.get('detail')) or info['code']
    return {'chat_id': chat, 'cleaned': False, 'reason': detail,
            'error': info, 'model_calls': 0}


def start(config_path):
    """Hook admission only; no enrollment, trust changes or synchronous cleanup."""
    config_path = Path(config_path).resolve()
    if not hasattr(select, 'kqueue') or not (home() / '.codex-global-state.json').is_file():
        return
    if not active(config_path, load_config(config_path)):
        return
    location = folder(config_path)
    location.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (location / 'deletion-observer.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
    source = Path(__file__).resolve().parents[1]
    subprocess.Popen([sys.executable, '-B', '-m', 'sessiongraph.deletion_observer',
        '--config', str(config_path)], cwd=source,
        env={**os.environ, 'PYTHONPATH': str(source), 'PYTHONDONTWRITEBYTECODE': '1'},
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)


def confirmed_many(config_path, chats, ids):
    """Batch only explicit tombstones with absent backend rows and sources."""
    result = {'cleaned': False, 'chat_ids': chats, 'model_calls': 0}
    if any(not CHAT.fullmatch(chat) or chat not in ids for chat in chats):
        return {**result, 'reason': 'no_delete_marker'}
    cfg = load_config(Path(config_path))
    if not active(config_path, cfg):
        return {**result, 'reason': 'disabled'}
    state = (home() / 'state_5.sqlite').resolve().as_uri() + '?mode=ro'
    db = sqlite3.connect(state, uri=True, timeout=2)
    try:
        if any(db.execute('SELECT 1 FROM threads WHERE id=?', (chat,)).fetchone() for chat in chats):
            return {**result, 'reason': 'codex_chat_present'}
    finally:
        db.close()
    return chat_cleanup.cleanup_many(config_path, chats)


def run(config_path):
    config_path = Path(config_path).resolve()
    location = folder(config_path)
    location.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = home() / '.codex-global-state.json'
    with (location / 'deletion-observer.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        watcher = select.kqueue()
        directory_fd = os.open(state.parent, os.O_RDONLY)
        state_fd = None
        signature = None
        pending = set()
        due = None
        def watch(fd):
            watcher.control([select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME)], 0, 0)
        try:
            watch(directory_fd)
            state_fd = os.open(state, os.O_RDONLY)
            watch(state_fd)
            seen = deleted_ids(state)  # No startup/history deletion sweep.
            receipt(config_path, 'deletion-observer.json', {'pid': os.getpid(),
                'source': str(Path(__file__).resolve()), 'started_at': time.time(),
                'idle_polling': False, 'model_calls': 0})
            while True:
                try:
                    current = state.stat()
                    stamp = (current.st_ino, current.st_mtime_ns, current.st_size)
                    if stamp != signature:
                        # Atomic replacement invalidates the old vnode watch.
                        if state_fd is None or signature is None or stamp[0] != signature[0]:
                            if state_fd is not None:
                                os.close(state_fd)
                            state_fd = None
                            state_fd = os.open(state, os.O_RDONLY)
                            watch(state_fd)
                        now = deleted_ids(state)
                        added = now - seen
                        pending.update(added)
                        if added:
                            due = time.monotonic() + 1
                        seen, signature = now, stamp
                except (OSError, ValueError, KeyError):
                    # Partial/unavailable state preserves the last good baseline.
                    pass
                if due is not None and time.monotonic() >= due:
                    cfg = load_config(config_path)
                    if not active(config_path, cfg):
                        return
                    for chat in sorted(pending):
                        try:
                            result = check(config_path, chat, deleted_ids(state))
                        except (OSError, ValueError, sqlite3.Error) as exc:
                            result = failure_result(chat, exc)
                        if result['reason'] != 'unindexed_source':
                            receipt(config_path, 'last-delete-check.json', result)
                    pending.clear()
                    due = None
                delay = None if due is None else max(0, due - time.monotonic())
                watcher.control(None, 16, delay)
        finally:
            watcher.close()
            if state_fd is not None:
                os.close(state_fd)
            os.close(directory_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--chat', action='append', default=[],
        help='Explicitly reconcile already-confirmed permanent deletion(s)')
    args = parser.parse_args()
    if args.chat:
        try:
            ids = deleted_ids(home() / '.codex-global-state.json')
            if len(args.chat) == 1:
                result = check(args.config, args.chat[0], ids)
            else:
                result = confirmed_many(args.config, args.chat, ids)
        except (OSError, ValueError, sqlite3.Error) as exc:
            result = failure_result(','.join(args.chat), exc)
        print(json.dumps(result))
    else:
        run(args.config)


if __name__ == '__main__':
    main()
