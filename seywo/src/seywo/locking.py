"""Process/thread locks with explicit, same-context nesting ownership."""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


from .runtime_platform import FileLease

_held = ContextVar("seywo_locks", default={})


def _canonical(path):
    # Resolve ancestor aliases while retaining no-follow protection on the leaf.
    path = Path(path).absolute()
    return path.parent.resolve() / path.name


def owns_exclusive(path):
    return _held.get().get(str(_canonical(path))) == "exclusive"


@contextmanager
def file_lock(path, *, exclusive=True):
    path = _canonical(path)
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


@contextmanager
def try_exclusive(path, *, blocking=False):
    """Acquire global reclamation ownership without upgrading a writer's lease."""
    path = _canonical(path)
    key = str(path)
    held = _held.get()
    if key in held:
        yield held[key] == 'exclusive'
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLease(path) as lease:
        if not lease.acquire(exclusive=True, blocking=blocking):
            yield False
            return
        token = _held.set({**held, key: 'exclusive'})
        try:
            yield True
        finally:
            _held.reset(token)
