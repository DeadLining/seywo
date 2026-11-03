"""Single owner of local restoration opportunities, not host-consumption receipts.

Every writer and acknowledgement uses the same short file lock. Never perform
database reads, subprocess work, network requests or output while holding it.
"""
from contextlib import contextmanager
import fcntl
import json
import math
from pathlib import Path
import time
import uuid


def flag_path(config, chat):
    safe = ''.join(c for c in chat if c.isalnum() or c in '-_')[:128]
    return Path(config['database']).parent / 'hook-state' / (safe + '.needs-restore')


@contextmanager
def locked(config):
    folder = Path(config['database']).parent / 'hook-state'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / 'restore.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def mark(config, chat):
    from .global_hooks import atomic_write
    value = json.dumps(dict(created_at=time.time(), revision=uuid.uuid4().hex))
    with locked(config):
        atomic_write(flag_path(config, chat), value)


def pending(config, chat, *, ttl=6 * 3600):
    with locked(config):
        try:
            with flag_path(config, chat).open() as stream:
                token = stream.read(513)
            if len(token) > 512:
                return None
            value = json.loads(token)
            stamp = value.get('created_at') if isinstance(value, dict) else value
            if type(stamp) not in (int, float) or not math.isfinite(stamp):
                return None
            return token if 0 <= time.time() - stamp <= ttl else None
        except (OSError, ValueError):
            return None


def discard(config, chats):
    """Remove chat-owned restore flags after a committed source cleanup."""
    chats = set(chats)
    removed = errors = 0
    with locked(config):
        for chat in sorted(chats):
            if not isinstance(chat, str) or not chat:
                continue
            path = flag_path(config, chat)
            try:
                if path.is_symlink() or path.is_file():
                    path.unlink()
                    removed += 1
            except OSError:
                errors += 1
    return removed, errors


def count(config, chats):
    chats = set(chats)
    return sum(flag_path(config, chat).is_file() for chat in chats if isinstance(chat, str) and chat)


def acknowledge(config, chat, token):
    with locked(config):
        flag = flag_path(config, chat)
        try:
            with flag.open() as stream:
                current = stream.read(513)
            if current != token:
                return False
            flag.unlink()
            return True
        except OSError:
            return False
