"""One-shot, read-only Gateway IPC. No socket, provider, import, or polling loop."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

from .core import Store, digest, load_config
from .auto_update import update_status
from .views import memory_detail
from .work_map import work_map

MAX_BYTES = 8 * 1024 * 1024


def catalog(registry: Path):
    # Only configuration and file availability are inspected, never every DB.
    if not registry.is_file():
        return {"projects": [], "default_project": "", "model_calls": 0}
    if registry.stat().st_size > 256 * 1024:
        raise ValueError("registry too large")
    data = json.loads(registry.read_text())
    entries = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(entries, list) or len(entries) > 100:
        raise ValueError("invalid registry")
    items, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("root"), str):
            raise ValueError("invalid project root")
        root = Path(entry["root"]).expanduser()
        if not root.is_absolute():
            raise ValueError("project root must be absolute")
        root = root.resolve()
        if root in seen:
            continue
        seen.add(root)
        item = {
            "id": digest(str(root))[:20],
            "name": str(entry.get("name") or root.name),
            "root": str(root),
            "state": "not_initialized",
        }
        config = root / ".sessiongraph/config.json"
        if config.is_file():
            try:
                cfg = load_config(config)
                if str(root) not in cfg["roots"]:
                    raise ValueError("project root mismatch")
                item["state"] = "ready" if Path(cfg["database"]).is_file() else "not_initialized"
            except (ValueError, OSError, KeyError, TypeError):
                item["state"] = "unavailable"
        items.append(item)
    return {"projects": items, "default_project": items[0]["id"] if items else "", "model_calls": 0}


def read(registry: Path, request: dict):
    from .read_diagnostics import ReadTrace
    with ReadTrace() as trace:
        payload = _read(registry, request, trace)
        payload['read_diagnostics'] = trace.summary()
        return payload


def _read(registry: Path, request: dict, trace):
    if not isinstance(request, dict) or set(request) - {"operation", "project", "id", "offset", "query", "chat", "materials", "view", "log_model", "log_status"}:
        raise ValueError("invalid request")
    op = request.get("operation")
    if op not in {"projects", "work_map", "status", "memory", "evidence", "summary_candidates", "call_logs"}:
        raise ValueError("unsupported operation")
    project, object_id, offset = request.get("project"), request.get("id"), request.get("offset")
    if (op not in {"projects", "call_logs"} or project is not None) and (
        not isinstance(project, str) or not re.fullmatch("[0-9a-fA-F]{20}", project)
    ):
        raise ValueError("invalid project")
    if op in {"memory", "evidence"} and (
        not isinstance(object_id, str) or not re.fullmatch("[A-Za-z0-9_-]{1,256}", object_id)
    ):
        raise ValueError("invalid id")
    if offset is not None and (
        op not in {"evidence", "work_map", "memory", "call_logs"} or type(offset) is not int or not 0 <= offset <= 50_000_000
    ):
        raise ValueError("invalid offset")
    view = request.get("view", "all")
    if op != "work_map" or view not in {"all", "knowledge", "episode"}:
        if "view" in request: raise ValueError("invalid view")
    for key, cap in (("query", 1000), ("chat", 256)):
        value = request.get(key)
        if value is not None and (op != "work_map" or not isinstance(value,str) or len(value)>cap):
            raise ValueError("invalid search")
    if op == "summary_candidates":
        from .summary_materials import validate_query
        validate_query(request.get("materials"))
    elif request.get("materials") is not None:
        raise ValueError("unexpected material query")
    if op == "call_logs":
        from .call_logs import read as read_calls
        return read_calls(project=project, model=request.get("log_model", ""),
                          status=request.get("log_status", "all"), offset=offset or 0,
                          state=registry.parent)
    if request.get("log_model") is not None or request.get("log_status") is not None:
        raise ValueError("unexpected log filter")
    with trace.phase("catalog"):
        projects = catalog(registry)
    if op == "projects":
        return projects
    selected = next((p for p in projects["projects"] if p["id"] == project), None)
    if selected is None:
        raise ValueError("unknown project")
    if selected["state"] != "ready":
        raise ValueError("project index unavailable")
    cfg = load_config(Path(selected["root"]) / ".sessiongraph/config.json")
    if selected["root"] not in cfg["roots"]:
        raise ValueError("project root mismatch")
    with trace.phase("database.open"):
        store = Store(cfg, readonly=True)
    store.db.execute("PRAGMA busy_timeout=1500")
    store.db.set_progress_handler(lambda: int(__import__("time").monotonic() > trace.deadline), 10000)
    try:
        identity = store.db.execute("SELECT value FROM metadata WHERE key='project_id'").fetchone()
        if identity is None or identity[0] != cfg["project_id"]:
            raise ValueError("database project mismatch")
        store.db.execute("BEGIN")
        if op == "summary_candidates":
            from .summary_materials import candidates
            with trace.phase("summary.candidates"):
                return candidates(store, request["materials"])
        if op == "work_map":
            result = work_map(store, query=request.get("query") or "", chat=request.get("chat") or "",
                              offset=offset or 0, view=request.get("view", "all"))
            # The canvas is a preview; full original evidence is fetched by memory/evidence.
            for node in result['nodes']:
                if isinstance(node.get('content'), str):
                    node['content_truncated'] = len(node['content']) > 180
                    node['content'] = node['content'][:180]
            for edge in result['edges']:
                evidence = edge.pop('evidence', [])
                edge['evidence_count'] = len(evidence)
            result['projection'] = 'canvas_preview_v1'
            return result
        if op == "status":
            return update_status(cfg)
        if op == "memory":
            return memory_detail(store, object_id, offset=offset or 0)
        return store.expand(object_id, radius=0, max_chars=10000, offset=offset or 0)
    finally:
        store.close()


def main(registry: Path):
    try:
        raw = sys.stdin.buffer.read(4097)
        if len(raw) > 4096:
            raise ValueError("request too large")
        payload = read(registry, json.loads(raw))
        body = json.dumps(payload, ensure_ascii=False).encode()
        if len(body) > MAX_BYTES:
            raise ValueError("response too large")
        sys.stdout.buffer.write(body)
        return 0
    except Exception as exc:
        from .read_diagnostics import ReadTimeout
        code = ('material_revision_changed' if isinstance(exc, ValueError) and str(exc) == 'material_revision_changed' else
                'local_read_timeout' if isinstance(exc, ReadTimeout) or
                isinstance(exc, sqlite3.OperationalError) and str(exc) == 'interrupted'
                else 'local_read_locked' if isinstance(exc, sqlite3.OperationalError) and 'locked' in str(exc)
                else 'local_read_failed')
        sys.stdout.write(json.dumps({'error':code,'model_calls':0}))
        return 1
