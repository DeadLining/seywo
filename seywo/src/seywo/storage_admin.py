"""Lossless, resumable Seywo storage migration and verification.

The normal Store path migrates the SQLite schema. This module owns the project
wide boundary around it: a guard stops maintenance, a compressed rollback copy
is made, legacy raw captures are converted to shared immutable blocks, database
content is hashed before and after, and only then are old raw captures removed.
"""

from __future__ import annotations


from contextlib import closing, contextmanager
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import tempfile
from uuid import uuid4

from .runtime_platform import FileLease, replace_file, sync_directory

from .sqlite_paths import sqlite_uri

from .archive import archived, capture, garbage_collect, inspect_archives, publication, source_digest, reblock
from .locking import lifecycle
from .config import load_config
from .store import SCHEMA_VERSION, Store

GUARD = "storage-migration.pending"
LOCK = "storage-migration.lock"
CHUNK = 1024 * 1024


def _state(config):
    return Path(config["_path"]).parent


def _write_json(path: Path, data: dict):
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding='utf-8')
        with tmp.open("r+b") as handle:
            os.fsync(handle.fileno())
        replace_file(tmp, path)
        sync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _backup_database(source: Path, target: Path):
    """SQLite backup includes committed WAL frames; never overwrite a receipt."""
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # A crash after publication but before journaling can safely reuse this image.
    if target.exists():
        digest, size = hashlib.sha256(), 0
        with tempfile.TemporaryDirectory(dir=target.parent, prefix=".check-backup-") as temp:
            restored = Path(temp) / "image.sqlite3"
            with gzip.open(target, "rb") as handle, restored.open("wb") as output:
                while data := handle.read(CHUNK):
                    digest.update(data)
                    size += len(data)
                    output.write(data)
            with closing(sqlite3.connect(restored)) as db:
                if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("invalid SQLite rollback image")
        return {"path": str(target), "sha256": digest.hexdigest(), "bytes": size}
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=".backup-") as temp:
        image = Path(temp) / "image.sqlite3"
        with closing(sqlite3.connect(sqlite_uri(source, mode="ro"), uri=True, timeout=60)) as incoming:
            with closing(sqlite3.connect(image)) as outgoing:
                incoming.backup(outgoing)
                if outgoing.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("invalid SQLite rollback image")
        digest, size = hashlib.sha256(), 0
        compressed = Path(temp) / "image.gz"
        with image.open("rb") as incoming, gzip.open(compressed, "wb", compresslevel=3) as outgoing:
            while data := incoming.read(CHUNK):
                digest.update(data)
                size += len(data)
                outgoing.write(data)
        with compressed.open("r+b") as handle:
            os.fsync(handle.fileno())
        replace_file(compressed, target)
        os.chmod(target, 0o600)
        sync_directory(target.parent)
    return {"path": str(target), "sha256": digest.hexdigest(), "bytes": size}


def _check_backup(metadata):
    digest, size = hashlib.sha256(), 0
    with gzip.open(metadata["path"], "rb") as handle:
        while data := handle.read(CHUNK):
            digest.update(data)
            size += len(data)
    if (digest.hexdigest(), size) != (metadata["sha256"], metadata["bytes"]):
        raise ValueError("rollback image identity changed; migration stopped")


def _seed_progress(config):
    """One-time transfer of pre-v7 cursor caches into transactional ownership."""
    changed = 0
    with Store(config) as store, store.db:
        for path in sorted((_state(config) / "maintain").glob("*.json")):
            chat = path.stem
            if store.db.execute("SELECT 1 FROM delta_progress WHERE chat_id=?", (chat,)).fetchone():
                continue
            head = store.db.execute("SELECT version FROM narrative_heads WHERE chat_id=?", (chat,)).fetchone()
            if not head:
                continue
            data = json.loads(path.read_text(encoding='utf-8'))
            cursor, version, windows = data.get("cursor"), data.get("version"), data.get("windows", 0)
            if cursor is None:
                continue
            if (not isinstance(cursor, list) or len(cursor) != 2
                    or any(type(v) is not int or v < 0 for v in cursor)
                    or type(version) is not int or not 0 <= version <= head[0]
                    or type(windows) is not int or windows < 0):
                raise ValueError("invalid pre-v7 cursor cache; preserve for recovery")
            bound = store.db.execute(
                "SELECT max(ss.records) FROM source_segments s JOIN source_snapshots ss ON ss.segment_id=s.id "
                "WHERE s.chat_id=? AND s.ordinal=?", (chat, cursor[0])).fetchone()[0]
            if bound is None or cursor[1] > bound:
                raise ValueError("pre-v7 cursor outside retained source history")
            store.db.execute("INSERT INTO delta_progress VALUES(?,?,?,?)", (chat, json.dumps(cursor), version, windows))
            changed += 1
    return changed


def _raw_inventory(live: Path):
    result = {}
    for path in sorted(live.glob("*.jsonl")):
        digest, size = source_digest(path)
        result[str(path)] = {"sha256": digest, "bytes": size, "archived": archived(path)}
    return result


def _db_digest(config):
    """Hash logical source occurrences and narrative bodies, independent of v5/v6 storage."""
    with Store(config, read_only=True) as store:
        store.db.execute("BEGIN")
        version = store.db.execute("PRAGMA user_version").fetchone()[0]
        if version >= 6:
            source_table = "stored_fragments"
        else:
            source_table = "source_fragments"
        columns = [r[1] for r in store.db.execute("PRAGMA table_info(source_fragments)")]
        fields = [name for name in columns if name != "payload_id"]
        select = ",".join(f"f.{name}" for name in fields)
        h = hashlib.sha256()
        count = 0
        rows = store.db.execute(
            f"SELECT {select},c.state,c.reason FROM {source_table} f "
            "JOIN coverage_ranges c ON c.fragment_id=f.row_id ORDER BY f.row_id")
        for row in rows:
            h.update(json.dumps([row[name] for name in fields] + [row["state"], row["reason"]],
                                ensure_ascii=False, separators=(",", ":"), sort_keys=False).encode())
            h.update(b"\n")
            count += 1
        source = {"rows": count, "sha256": h.hexdigest()}

        nh = hashlib.sha256()
        ncount = 0
        for row in store.db.execute(
                "SELECT chat_id,version,body_json,source_heads,task_id,origin,created_at "
                "FROM narrative_versions ORDER BY chat_id,version"):
            nh.update(json.dumps(list(row), ensure_ascii=False, separators=(",", ":")).encode())
            nh.update(b"\n")
            ncount += 1
        narrative = {"rows": ncount, "sha256": nh.hexdigest()}
        th = hashlib.sha256()
        tcount = 0
        if version >= 2:
            for row in store.db.execute('SELECT * FROM understanding_tasks ORDER BY id'):
                th.update(json.dumps(list(row), ensure_ascii=False, separators=(',', ':')).encode())
                th.update(b'\n')
                tcount += 1
        tasks = {'rows': tcount, 'sha256': th.hexdigest()}
    return {"schema": version, "source": source, "narrative": narrative, "tasks": tasks}


def _manifest_target(path: Path, digest: str):
    # Keep a per-source locator so source_segments.path remains unique while the
    # 256 KiB blocks underneath are shared across all sources.
    return path.with_name(f"{path.stem}-{digest}.jsonl")


def _convert_raw(live: Path, inventory):
    mapping = {}
    created = 0
    for old, meta in inventory.items():
        source = Path(old)
        if meta["archived"]:
            mapping[old] = old
            continue
        target = _manifest_target(source, meta["sha256"])
        if target.exists():
            got = source_digest(target)
            if got != (meta["sha256"], meta["bytes"]):
                raise ValueError(f"archive target collision: {target.name}")
        else:
            with publication(target):
                manifest = capture(source, target)
                if (manifest["sha256"], manifest["bytes"]) != (meta["sha256"], meta["bytes"]):
                    raise ValueError(f"archive capture changed source: {source.name}")
            created += 1
        mapping[old] = str(target)
    return mapping, created


def _rewrite_paths(config, mapping):
    changed = 0
    unavailable = 0
    with Store(config) as store:
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            for old, new in mapping.items():
                if old == new:
                    continue
                store.db.execute("UPDATE source_segments SET path=? WHERE path=?", (new, old))
                store.db.execute("UPDATE source_archives SET path=? WHERE path=?", (new, old))
                changed += 1
            # A segment path identifies the latest physical capture. Older
            # snapshots may predate a capture that was overwritten before v2.
            # Record an archive only when its logical digest really matches;
            # never manufacture provenance by pointing it at newer bytes.
            for row in store.db.execute(
                    "SELECT ss.id,ss.sha256,ss.byte_count,coalesce(a.path,s.path) path FROM source_snapshots ss "
                    "JOIN source_segments s ON s.id=ss.segment_id LEFT JOIN source_archives a ON a.snapshot_id=ss.id"):
                try:
                    matches = source_digest(row["path"]) == (row["sha256"], row["byte_count"])
                except (OSError, ValueError):
                    matches = False
                if matches:
                    store.db.execute("INSERT OR REPLACE INTO source_archives(snapshot_id,path) VALUES(?,?)",
                                     (row["id"], row["path"]))
                else:
                    store.db.execute("DELETE FROM source_archives WHERE snapshot_id=?", (row["id"],))
                    unavailable += 1
    # Pending records are durable inputs too; move only their immutable snapshot
    # locator. Their native transcript path remains the external source identity.
    pending = _state(config) / "inbox" / "pending"
    if pending.is_dir():
        for path in pending.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError):
                continue
            old = data.get("snapshot")
            if old in mapping and mapping[old] != old:
                data["snapshot"] = mapping[old]
                _write_json(path, data)
    return {"changed": changed, "unavailable_historical_archives": unavailable}


def _remove_legacy(mapping):
    removed = 0
    for old, new in mapping.items():
        if old == new:
            continue
        path = Path(old)
        if path.exists() and not archived(path):
            path.unlink()
            removed += 1
    return removed


@contextmanager
def _read_lease(config):
    """Inspection may lock an existing inode, but never creates a file."""
    path = _state(config) / LOCK
    try:
        handle = FileLease(path, create=False)
    except FileNotFoundError:
        yield
        return
    with handle:
        handle.acquire()
        yield


def _integrity(config):
    with Store(config, read_only=True) as store:
        store.db.execute("BEGIN")
        result = [r[0] for r in store.db.execute("PRAGMA integrity_check")]
        foreign = store.db.execute("PRAGMA foreign_key_check").fetchall()
        if result != ["ok"] or foreign:
            raise ValueError(f"database integrity failed: {result}, foreign keys={len(foreign)}")
        if store.db.execute("PRAGMA user_version").fetchone()[0] >= 6:
            identities = {}
            for row in store.db.execute(
                    "SELECT a.path,ss.sha256,ss.byte_count FROM source_archives a "
                    "JOIN source_snapshots ss ON ss.id=a.snapshot_id"):
                if row[0] not in identities:
                    identities[row[0]] = source_digest(row[0])
                if identities[row[0]] != (row[1], row[2]):
                    raise ValueError("snapshot archive identity mismatch")
    return {"integrity": "ok", "foreign_key_violations": 0}


def _verify(config):
    db = _db_digest(config)
    checks = _integrity(config)
    raw = _raw_inventory(_state(config) / "inbox" / "live")
    refs, blocks = inspect_archives(_state(config) / "inbox")
    return {"state": "verified", "database": db, "raw_files": len(raw), **checks,
            "archive_inventory": {"referenced_blocks": len(refs),
                                  "unreferenced_blocks": sum(p.stem not in refs for p in blocks)}}


def verify(config):
    with _read_lease(config):
        return _verify(config)


def _plan(config):
    baseline = _db_digest(config)
    raw = _raw_inventory(_state(config) / "inbox" / "live")
    return {"state": "planned", "schema_before": baseline["schema"],
            "database_before": baseline, "raw_before": raw, "source_files": len(raw),
            "estimated_raw_bytes": sum(x["bytes"] for x in raw.values())}


def migrate(config, *, dry_run=False):
    if dry_run:
        with _read_lease(config):
            return _plan(config)
    state = _state(config)
    live = state / "inbox" / "live"
    maintenance = state / "maintenance" / "storage-migration"
    with lifecycle(config, exclusive=True):
        guard = state / GUARD
        if guard.exists():
            marker = json.loads(guard.read_text(encoding='utf-8'))
            if not isinstance(marker, dict) or not marker.get("run_id"):
                raise ValueError("invalid migration journal; preserve it for recovery")
            receipt = maintenance / (marker["run_id"] + ".json")
            if "database_before" not in marker or "raw_before" not in marker:
                raise ValueError("incomplete migration baseline; preserve existing backup and journal")
        else:
            plan = _plan(config)
            if plan["schema_before"] >= SCHEMA_VERSION and all(m["archived"] for m in plan["raw_before"].values()):
                return {**_verify(config), "state": "already_migrated"}
            marker = {"run_id": f"storage-{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}",
                      "state": "prepared", "started_at": time.time(),
                      "database_before": plan["database_before"], "raw_before": plan["raw_before"]}
        maintenance.mkdir(parents=True, exist_ok=True)
        live.mkdir(parents=True, exist_ok=True)
        receipt = maintenance / (marker["run_id"] + ".json")

        def persist(stage, **fields):
            marker.update(fields, state=stage)
            # The guard remains durable until the complete receipt is durable.
            _write_json(guard, marker)
            _write_json(receipt, marker)

        persist(marker["state"])
        if marker["state"] == "complete":
            _verify(config)
            guard.unlink()
            return marker["result"]
        baseline, raw_before = marker["database_before"], marker["raw_before"]
        if "backup" in marker:
            _check_backup(marker["backup"])
        else:
            backup = _backup_database(Path(config["_database"]), maintenance / (marker["run_id"] + ".sqlite3.gz"))
            persist("backup_created", backup=backup)
        if "mapping" not in marker:
            mapping, created = _convert_raw(live, raw_before)
            persist("raw_durable", mapping=mapping, created_manifests=created)
        mapping = marker["mapping"]
        with Store(config):
            pass
        reconciliation = _rewrite_paths(config, mapping)
        seeded = _seed_progress(config)
        after = _db_digest(config)
        if any(after[k] != baseline[k] for k in ("source", "narrative", "tasks")):
            raise ValueError("logical database digest changed during migration")
        for old, meta in raw_before.items():
            if source_digest(mapping[old]) != (meta["sha256"], meta["bytes"]):
                raise ValueError(f"logical raw digest changed: {Path(old).name}")
        _integrity(config)
        persist("validated", archive_reconciliation=reconciliation)
        removed = _remove_legacy(mapping)
        reblocked = 0
        for path in sorted(live.glob('*.jsonl')):
            reblocked += bool(reblock(path))
        with Store(config) as store, store.db:
            from .storage_lifecycle import collect_database
            reclaimed = collect_database(store.db)
        gc = garbage_collect(state / "inbox")
        persist("reclaimed")
        with Store(config) as store:
            store.db.execute("VACUUM")
        final = _verify(config)
        if any(final["database"][k] != baseline[k] for k in ("source", "narrative", "tasks")):
            raise ValueError("logical database digest changed after vacuum")
        result = {"state": "migrated", "schema": final["database"]["schema"],
                  "database_before": baseline, "database_after": final["database"],
                  "raw_before_bytes": sum(m["bytes"] for m in raw_before.values()),
                  "raw_legacy_removed": removed, "created_manifests": marker["created_manifests"],
                  "garbage_collect": gc, "archive_reconciliation": reconciliation,
                  "backup": marker["backup"], "integrity": final["integrity"], "foreign_key_violations": 0,
                  "seeded_delta_cursors": seeded, "archives_reblocked": reblocked, "database_reclaimed": reclaimed}
        persist("complete", result=result)
        guard.unlink()
        return result


def run(config_path, command, *, dry_run=False):
    config = load_config(config_path)
    if command == "migrate":
        return migrate(config, dry_run=dry_run)
    if command == "verify":
        return verify(config)
    if command == "gc":
        from .storage_lifecycle import collect
        return collect(config, force=True)
    raise ValueError("unknown storage command")
