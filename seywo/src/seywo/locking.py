"""Process/thread locks with explicit, same-context nesting ownership."""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


from .runtime_platform import FileLease

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
    with FileLease(path) as lease:
        lease.acquire(exclusive=exclusive)
        token = _held.set({**held, key: "exclusive" if exclusive else "shared"})
        try:
            yield
        finally:
            _held.reset(token)


def lifecycle(config, *, exclusive=False):
    return file_lock(
        Path(config["_path"]).parent / "storage-migration.lock", exclusive=exclusive
    )
