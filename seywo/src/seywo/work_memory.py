"""Explicit source-backed work memory; read paths never create its optional table."""

import json
import time
from uuid import uuid4

from .projection import sanitize
from .store import encode

KINDS = {
    "goal",
    "constraint",
    "decision",
    "finding",
    "artifact",
    "progress",
    "next_step",
}
DDL = """CREATE TABLE IF NOT EXISTS work_memories(
 id TEXT PRIMARY KEY,chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
 kind TEXT NOT NULL,title TEXT NOT NULL,content TEXT NOT NULL,status TEXT NOT NULL,
 scope TEXT NOT NULL,aliases_json TEXT NOT NULL,paths_json TEXT NOT NULL,
 event_id TEXT NOT NULL REFERENCES source_events(id) ON DELETE CASCADE,
 snapshot_id TEXT NOT NULL REFERENCES source_snapshots(id) ON DELETE CASCADE,start INTEGER NOT NULL,end INTEGER NOT NULL,
 quote TEXT NOT NULL,supersedes_id TEXT UNIQUE REFERENCES work_memories(id) ON DELETE SET NULL,created_at REAL NOT NULL)"""


def exists(db):
    return bool(
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_memories'"
        ).fetchone()
    )


def text(value, name, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"invalid_{name}")
    safe, redactions = sanitize(value)
    if redactions or safe != value:
        raise ValueError(f"sensitive_{name}")
    return value


def list_rows(db, chat_ids, include_superseded=False):
    if not exists(db):
        return []
    where, params = "", []
    if chat_ids:
        where = " AND m.chat_id IN (" + ",".join("?" for _ in chat_ids) + ")"
        params.extend(chat_ids)
    if not include_superseded:
        where += (
            " AND NOT EXISTS(SELECT 1 FROM work_memories n WHERE n.supersedes_id=m.id)"
        )
    return [
        dict(r)
        for r in db.execute(
            "SELECT m.* FROM work_memories m WHERE 1=1"
            + where
            + " ORDER BY m.created_at DESC,m.id",
            params,
        )
    ]


def remember(
    store,
    *,
    chat_id,
    event_id,
    snapshot_id,
    start,
    end,
    source_quote,
    kind,
    title,
    content,
    status="active",
    scope="project",
    aliases=None,
    paths=None,
    supersedes_id=None,
    memory_id=None,
):
    """Append only after checking exact source identity and excerpt in one transaction."""
    if kind not in KINDS or scope not in {"project", "chat"}:
        raise ValueError("invalid_memory_kind_or_scope")
    values = {
        "kind": kind,
        "title": text(title, "title", 40),
        "content": text(content, "content", 2000),
        "status": text(status, "status", 40),
        "scope": scope,
    }
    for name, entries in [("aliases", aliases or []), ("paths", paths or [])]:
        if not isinstance(entries, list) or len(entries) > 16:
            raise ValueError("invalid_memory_list")
        values[name + "_json"] = encode([text(e, name, 512) for e in entries])
    if any(path not in source_quote for path in paths or []):
        raise ValueError("memory_path_not_in_source")
    identity = memory_id or "wm_" + uuid4().hex
    text(identity, "memory_id", 128)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        store.require_human_chat(chat_id)
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start < end
            or end - start > 8000
        ):
            raise ValueError("invalid_source_range")
        row = store.db.execute(
            "SELECT f.role,f.kind,c.state FROM snapshot_fragments f "
            "JOIN coverage_ranges c ON c.fragment_id=f.row_id "
            "JOIN source_events e ON e.id=f.event_id JOIN source_segments s ON s.id=e.segment_id "
            "WHERE f.event_id=? AND f.snapshot_id=? AND s.chat_id=?",
            (event_id, snapshot_id, chat_id),
        ).fetchone()
        if (
            not row
            or row["state"] in {"unavailable", "excluded_mechanical"}
            or row["role"] not in {"user", "assistant"}
            or row["kind"] != "message"
        ):
            raise ValueError("memory_source_not_visible_dialogue")
        source = store.read(event_id, snapshot_id=snapshot_id, start=start, end=end)
        if source["text"] != source_quote or not source_quote:
            raise ValueError("memory_quote_mismatch")
        text(source_quote, "source_quote", 8000)
        values.update(
            id=identity,
            chat_id=chat_id,
            event_id=event_id,
            snapshot_id=snapshot_id,
            start=start,
            end=end,
            quote=source_quote,
            supersedes_id=supersedes_id,
        )
        store.db.execute(DDL)
        prior = store.db.execute(
            "SELECT * FROM work_memories WHERE id=?", (identity,)
        ).fetchone()
        if prior:
            if any(prior[k] != v for k, v in values.items()):
                raise ValueError("memory_id_conflict")
            return {
                "memory_id": identity,
                "reused": True,
                "model_calls": 0,
                "user_confirmed": False,
            }
        if supersedes_id:
            previous = store.db.execute(
                "SELECT * FROM work_memories WHERE id=?", (supersedes_id,)
            ).fetchone()
            if (
                not previous
                or previous["scope"] != scope
                or (scope == "chat" and previous["chat_id"] != chat_id)
            ):
                raise ValueError("memory_supersedes_scope_mismatch")
            if store.db.execute(
                "SELECT 1 FROM work_memories WHERE supersedes_id=?", (supersedes_id,)
            ).fetchone():
                raise ValueError("memory_already_superseded")
        values["created_at"] = time.time()
        store.db.execute(
            "INSERT INTO work_memories ("
            + ",".join(values)
            + ") VALUES ("
            + ",".join("?" for _ in values)
            + ")",
            list(values.values()),
        )
    return {
        "memory_id": identity,
        "reused": False,
        "model_calls": 0,
        "user_confirmed": False,
    }


def decode(row):
    row = dict(row)
    row["aliases"] = json.loads(row.pop("aliases_json"))
    row["paths"] = json.loads(row.pop("paths_json"))
    return row
