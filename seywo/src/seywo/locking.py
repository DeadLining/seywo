"""Process/thread locks with explicit, same-context nesting ownership."""

from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import os
from pathlib import Path

_held = ContextVar("seywo_locks", default={})


def owns_exclusive(path):
    return _held.get().get(str(Path(path).absolute())) == "exclusive"


@contextmanager
def file_lock(path, *, exclusive=True):
    path = Path(path).absolute()
    key = str(path)
    held = _held.get()
    if key in held:
        if exclusive and held[key] != "exclusive":
            raise RuntimeError("cannot upgrade an active shared lock")
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        token = _held.set({**held, key: "exclusive" if exclusive else "shared"})
        try:
            yield
        finally:
            _held.reset(token)
    finally:
        os.close(fd)


def lifecycle(config, *, exclusive=False):
    return file_lock(
        Path(config["_path"]).parent / "storage-migration.lock", exclusive=exclusive
    )
