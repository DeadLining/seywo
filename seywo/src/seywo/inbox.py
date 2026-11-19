"""Compact boundary inbox: every hook writes a small durable record, then exits.

No database, no model and no source rewriting happen in a hook process. A record
only names a chat, an event and a source; a PreCompact event may additionally
carry a bounded verbatim snapshot, because compaction can rewrite the source
after the hook returns. Duplicate delivery coalesces on the immutable capture
and source fingerprint within the same chat, event and transcript path.
"""

from functools import wraps
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from .runtime_platform import replace_file, sync_directory

from .locking import file_lock, lifecycle
from .store import digest

EVENTS = {"PreCompact", "PostCompact", "SessionEnd", "Stop"}
SNAPSHOT_LIMIT = 64 * 1024 * 1024
KEEP_RECORDS = 200


def root(config):
    return Path(config["_path"]).parent / "inbox"


def _write_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_name("." + path.name + "." + uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode())
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def _fingerprint(path: Path) -> str:
    try:
        info = path.stat()
    except OSError:
        return "missing"
    return digest(f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}")


def _serialized(function):
    @wraps(function)
    def call(config, *args, **kwargs):
        with lifecycle(config), file_lock(root(config) / ".lock"):
            return function(config, *args, **kwargs)
    return call


def snapshot(config, source: Path, chat_id: str, unique: bool = False) -> str | None:
    """Capture exact shared blocks and return an immutable content locator."""
    from .archive import content_capture
    folder = root(config) / "live"
    try:
        target, _ = content_capture(source, folder, chat_id)
        return str(target)
    except (OSError, ValueError):
        return None


def record(config, chat_id: str, event: str, transcript: str) -> dict:
    with lifecycle(config):
        if (Path(config["_path"]).parent / "storage-migration.pending").exists():
            raise ValueError("storage migration pending")
        return _record(config, chat_id, event, transcript)


def _record(config, chat_id: str, event: str, transcript: str) -> dict:
    """Persist before returning. Raises only for programming errors."""
    if event not in EVENTS:
        raise ValueError("unsupported hook event")
    source = Path(transcript)
    folder = root(config) / "pending"
    folder.mkdir(parents=True, exist_ok=True)
    with file_lock(root(config) / ".lock"):
        # Capture before deduplication: metadata can be restored after a rewrite,
        # and a source path alone identifies neither the bytes nor the boundary.
        boundary = {
            "chat_id": chat_id, "event": event, "transcript": str(source),
            "fingerprint": _fingerprint(source),
            "snapshot": snapshot(config, source, chat_id),
        }
        key = digest(json.dumps(boundary, ensure_ascii=False, sort_keys=True))[:32]
        path = folder / f"{key}.json"
        retry_path = None
        # Match fields as well as the filename so legacy path-only records and
        # a pending redelivery beside a claimed record retain their identities.
        for candidate in sorted(folder.glob("*.json")):
            previous = _read(candidate)
            if not previous or not all(previous.get(k) == v for k, v in boundary.items()):
                continue
            if previous.get("state") == "pending":
                return {**previous, "_path": str(candidate), "coalesced": True}
            if previous.get("state") in {"failed", "done"}:
                retry_path = candidate
        if retry_path is not None:
            # Re-delivery retries a terminal record as before: clear the error
            # and start a new attempt allowance. Explicit retry is unchanged.
            path = retry_path
        else:
            # A claimed record stays immutable. Keep one pending redelivery;
            # subsequent copies find and coalesce it above. Never replace any
            # other boundary, including an unreadable pre-existing record.
            while path.exists():
                path = folder / f"{digest(key + uuid4().hex)[:32]}.json"
        item = {
            **boundary, "version": 1, "id": path.stem,
            "state": "pending", "attempts": 0, "error_code": None,
            "created_at": time.time(), "updated_at": time.time(),
        }
        _write_atomic(path, item)
        _prune(folder)
        return {**item, "_path": str(path), "coalesced": False}



def _open_for(config, chat_id: str) -> list[dict]:
    """Records a worker may still be reading, claimed ones included.

    pending() hides claimed records, but their snapshot path must not be
    overwritten while the worker still holds it.
    """
    folder = root(config) / "pending"
    if not folder.is_dir():
        return []
    items = []
    for path in folder.glob("*.json"):
        item = _read(path)
        if item and item.get("chat_id") == chat_id and item.get("state") in {"pending", "claimed"}:
            items.append(item)
    return items


def _prune(folder: Path) -> None:
    files = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in files[KEEP_RECORDS:]:
        item = _read(stale)
        if item and item.get("state") in {"pending", "claimed"}:
            continue
        stale.unlink(missing_ok=True)


CLAIM_TTL = 3600


def pending(config, chat_id: str | None = None) -> list[dict]:
    """Pending records; a claim older than an hour is treated as crashed."""
    folder = root(config) / "pending"
    if not folder.is_dir():
        return []
    items = []
    for path in sorted(folder.glob("*.json"), key=lambda p: p.name):
        item = _read(path)
        if not item:
            continue
        if item.get("state") == "claimed" and time.time() - item.get("updated_at", 0) > CLAIM_TTL:
            item["state"] = "pending"
        if item.get("state") != "pending":
            continue
        if chat_id is not None and item.get("chat_id") != chat_id:
            continue
        item["_path"] = str(path)
        items.append(item)
    return sorted(items, key=lambda i: (i.get("created_at", 0), i["id"]))


@_serialized
def claim(config, item: dict) -> dict | None:
    path = Path(item["_path"])
    with file_lock(root(config) / ".lock"):
        current = _read(path)
        if not current or current.get("state") != "pending":
            return None
        current.update(state="claimed", attempts=current.get("attempts", 0) + 1,
                       updated_at=time.time())
        _write_atomic(path, current)
        return {**current, "_path": str(path)}



@_serialized
def requeue_failed(config, chat_id: str, *, limit: int = 5) -> int:
    """Bound source-import retries; semantic retries have their own call budget."""
    folder = root(config) / "pending"
    if not folder.is_dir():
        return 0
    count = 0
    for path in folder.glob("*.json"):
        item = _read(path)
        if (item and item.get("state") == "failed" and item.get("chat_id") == chat_id
                and (item.get("imported_snapshot_id") or item.get("attempts", 0) < limit)):
            # Imported sources need no further I/O attempt. A new worker run
            # may retry semantics; compact_pipeline bounds its model calls.
            item.update(state="pending", updated_at=time.time())
            _write_atomic(path, item)
            count += 1
    return count


@_serialized
def reclaim(config, chat_id: str) -> int:
    """Return this chat's abandoned claims to pending.

    Call only while holding the per-chat maintainer lock: no other worker can own
    a claim then, so any remaining claim belongs to a killed process.
    """
    folder = root(config) / "pending"
    if not folder.is_dir():
        return 0
    count = 0
    for path in folder.glob("*.json"):
        item = _read(path)
        if item and item.get("state") == "claimed" and item.get("chat_id") == chat_id:
            item.update(state="pending", updated_at=time.time())
            _write_atomic(path, item)
            count += 1
    return count


@_serialized
def finish(config, item: dict, *, state: str, error: str | None = None) -> None:
    path = Path(item["_path"])
    with file_lock(root(config) / ".lock"):
        current = _read(path)
        if not current:
            return
        current.update(state=state, error_code=error, updated_at=time.time())
        if state == "pending":
            current["attempts"] = 0
        # The snapshot stays after done: it is the exact source the segment
        # was bound to, so resuming a parked job or re-delivering the same
        # boundary re-reads the same bytes instead of the (possibly
        # rewritten) live transcript. prune_live reclaims it later.
        _write_atomic(path, current)



@_serialized
def imported(config, item: dict, *, snapshot_id: str, cursor) -> None:
    """Record source durability; the claim still awaits semantic publication."""
    path = Path(item["_path"])
    current = _read(path)
    if current and current.get("state") == "claimed":
        current.update(imported_snapshot_id=snapshot_id, imported_cursor=cursor, updated_at=time.time())
        _write_atomic(path, current)


@_serialized
def settle_imported(config, chat_id: str, cursor, *, error: str | None = None) -> None:
    """Acknowledge only imported claims covered by the committed watermark.

    Failed claims retain import identity for retry. An open trailing turn stays
    claimed until a later boundary closes it; successful prefixes cannot ack it.
    """
    for path in (root(config) / "pending").glob("*.json"):
        item = _read(path)
        if (not item or item.get("chat_id") != chat_id or item.get("state") != "claimed"
                or not item.get("imported_snapshot_id")):
            continue
        end = item.get("imported_cursor")
        if error or end is None or (cursor is not None and tuple(end) <= tuple(cursor)):
            item.update(state="failed" if error else "done", error_code=error, updated_at=time.time())
            _write_atomic(path, item)


@_serialized
def relocate(config, item: dict, snapshot: Path) -> dict:
    """Rebind a claimed record to a moved snapshot before anything is imported."""
    path = Path(item["_path"])
    with file_lock(root(config) / ".lock"):
        current = _read(path)
        if not current:
            return item
        current.update(snapshot=str(snapshot), updated_at=time.time())
        _write_atomic(path, current)
        return {**item, "snapshot": str(snapshot)}



KEEP_SNAPSHOTS = 20


def prune_live(config, chat_id: str, referenced: set[str]) -> int:
    from .archive import publication
    with lifecycle(config), file_lock(root(config) / ".lock"), publication(root(config) / "live" / "owner.jsonl"):
        # Caller lists are only hints: query the canonical historical bindings.
        from .store import Store
        with Store(config, read_only=True) as store:
            protected = {r[0] for r in store.db.execute(
                "SELECT path FROM source_segments UNION SELECT path FROM source_archives")}
        for path in (root(config) / "pending").glob("*.json"):
            item = _read(path)
            if not item:
                return 0  # ambiguous durable references: fail closed
            if item.get("snapshot"):
                protected.add(item["snapshot"])
        return _prune_live(config, chat_id, referenced | protected)


def _prune_live(config, chat_id: str, referenced: set[str]) -> int:
    """Delete unreferenced snapshots, keeping the newest few for open records."""
    folder = root(config) / "live"
    if not folder.is_dir():
        return 0
    files = sorted((p for p in folder.glob(f"{chat_id}*.jsonl") if str(p) not in referenced),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    count = 0
    for stale in files[KEEP_SNAPSHOTS:]:
        stale.unlink(missing_ok=True)
        count += 1
    return count


def failed(config, chat_id: str) -> list[dict]:
    """Failed records, optionally for one chat; retry keeps the original order."""
    folder = root(config) / "pending"
    if not folder.is_dir():
        return []
    items = []
    for path in sorted(folder.glob("*.json"), key=lambda p: p.name):
        item = _read(path)
        if item and item.get("state") == "failed" and (not chat_id or item.get("chat_id") == chat_id):
            item["_path"] = str(path)
            items.append(item)
    return items


def retry(config, item: dict) -> None:
    finish(config, item, state="pending")


def snapshot_dir(config) -> Path:
    return root(config) / "live"


@_serialized
def resume_turn(config, chat_id: str, transcript: str | None = None) -> bool:
    """Capture completion only for a compact boundary still awaiting publication.

    Never alter the frozen boundary or its imported snapshot. A new content-addressed
    capture supplies final/terminal rows; the turn ledger skips committed dialogue.
    Holding the inbox lock serializes Stop with the worker's final settlement.
    """
    sources = set()
    for path in (root(config) / 'pending').glob('*.json'):
        item = _read(path)
        if (item and item.get('chat_id') == chat_id
                and item.get('state') in {'pending', 'claimed'}):
            if item.get('transcript'):
                sources.add(item['transcript'])
    if not sources:
        return False
    if transcript:
        sources = {transcript}
    from .hook import chat_identity
    for source in sorted(sources):
        # Do not recover a foreign/subagent transcript or silently freeze a
        # missing path in place of the original compact evidence.
        if chat_identity(source, chat_id) != chat_id or not Path(source).is_file():
            raise ValueError('source_missing')
        record(config, chat_id, 'Stop', source)
    return True
