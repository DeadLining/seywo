"""Read-only integrity checks for the current storage format."""

from contextlib import contextmanager
from pathlib import Path

from .archive import inspect_archives
from .runtime_platform import FileLease
from .storage_lifecycle import _validate_database_archives
from .store import SCHEMA_VERSION, Store


@contextmanager
def _read_lease(config):
    """Inspection may lock an existing inode, but never creates a file."""
    path = Path(config["_path"]).parent / "storage-migration.lock"
    try:
        handle = FileLease(path, create=False)
    except FileNotFoundError:
        yield
        return
    with handle:
        handle.acquire()
        yield


def verify(config):
    with _read_lease(config), Store(config, read_only=True) as store:
        store.db.execute("BEGIN")
        if [row[0] for row in store.db.execute("PRAGMA integrity_check")] != ["ok"]:
            raise ValueError("database integrity check failed")
        if store.db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("database foreign key check failed")
        _validate_database_archives(store.db)
        inbox = Path(config["_path"]).parent / "inbox"
        refs, blocks = inspect_archives(inbox)
        return {"state": "verified", "schema": SCHEMA_VERSION,
                "integrity": "ok", "foreign_key_violations": 0,
                "archive_inventory": {"referenced_blocks": len(refs),
                                      "unreferenced_blocks": sum(path.stem not in refs for path in blocks)}}
