import hashlib
import json
from pathlib import Path

import pytest

from sessiongraph.core import Store, init_project, load_config
from sessiongraph.native_bridge import read, catalog


def setup(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    cfg = load_config(init_project(root))
    Store(cfg).close()
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"projects": [{"root": str(root)}]}))
    return cfg, registry


def test_read_only_and_no_implicit_provider(tmp_path, monkeypatch):
    cfg, registry = setup(tmp_path)
    import socket

    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("no network"))
    before = hashlib.sha256(Path(cfg["database"]).read_bytes()).hexdigest()
    pid = catalog(registry)["projects"][0]["id"]
    for op in ["projects", "work_map", "status"]:
        result = read(registry, {"operation": op, "project": pid})
        assert result["model_calls"] == 0
    assert before == hashlib.sha256(Path(cfg["database"]).read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "payload",
    [
        {"operation": "extract"},
        {"operation": "work_map"},
        {"operation": "work_map", "project": "../../outside"},
        {"operation": "evidence", "project": "a" * 20, "id": "x", "offset": 50_000_001},
        {"operation": "memory", "project": "a" * 20, "id": "../x"},
        {"operation": "work_map", "project": "a" * 20, "path": "/outside"},
    ],
)
def test_rejects_invalid_input(tmp_path, payload):
    _, registry = setup(tmp_path)
    with pytest.raises(ValueError):
        read(registry, payload)


def test_catalog_does_not_open_databases(tmp_path, monkeypatch):
    _, registry = setup(tmp_path)
    import sqlite3

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("catalog DB scan"))
    assert catalog(registry)["projects"][0]["state"] == "ready"


def test_unknown_project_does_not_fallback(tmp_path):
    _, registry = setup(tmp_path)
    with pytest.raises(ValueError, match="unknown project"):
        read(registry, {"operation": "work_map", "project": "a" * 20})


def test_mismatched_database_fails_closed(tmp_path):
    cfg, registry = setup(tmp_path)
    store = Store(cfg)
    store.db.execute("UPDATE metadata SET value='wrong' WHERE key='project_id'")
    store.db.commit()
    store.close()
    pid = catalog(registry)["projects"][0]["id"]
    with pytest.raises(ValueError):
        read(registry, {"operation": "work_map", "project": pid})


def test_memory_evidence_and_project_isolation(tmp_path):
    from sessiongraph.memory import remember
    from test_mvp import write_session

    cfg, registry = setup(tmp_path)
    store = Store(cfg)
    root = Path(cfg["roots"][0])
    store.ingest(
        write_session(root / "s.jsonl", root, "chat", [("user", "保留实验数据 result.csv")])
    )
    eid = store.db.execute("SELECT id FROM entries LIMIT 1").fetchone()[0]
    mid = remember(
        store,
        kind="artifact",
        title="实验数据",
        content="保留实验数据",
        source_entry_id=eid,
        source_quote="保留实验数据 result.csv",
    )["id"]
    store.close()
    pid = catalog(registry)["projects"][0]["id"]
    assert (
        read(registry, {"operation": "memory", "project": pid, "id": mid})["memory"]["source"][
            "entry_id"
        ]
        == eid
    )
    assert (
        read(registry, {"operation": "evidence", "project": pid, "id": eid, "offset": 0})[
            "model_calls"
        ]
        == 0
    )
    beta = tmp_path / "beta"
    beta.mkdir()
    cfg2 = load_config(init_project(beta))
    Store(cfg2).close()
    registry.write_text(json.dumps({"projects": [{"root": str(root)}, {"root": str(beta)}]}))
    other = catalog(registry)["projects"][1]["id"]
    with pytest.raises(ValueError, match="unknown memory"):
        read(registry, {"operation": "memory", "project": other, "id": mid})


def test_catalog_missing_corrupt_duplicate_and_unavailable(tmp_path):
    registry = tmp_path / "projects.json"
    assert catalog(registry)["projects"] == []
    for content in [
        "[]",
        '{"projects":{}}',
        '{"projects":[{"root":"relative"}]}',
        '{"projects":[{}]}',
    ]:
        registry.write_text(content)
        with pytest.raises(ValueError):
            catalog(registry)
    cfg, registry = setup(tmp_path)
    root = Path(cfg["roots"][0])
    registry.write_text(json.dumps({"projects": [{"root": str(root)}, {"root": str(root)}]}))
    assert len(catalog(registry)["projects"]) == 1
    (root / ".sessiongraph/config.json").write_text("invalid")
    assert catalog(registry)["projects"][0]["state"] == "unavailable"
    with pytest.raises(ValueError, match="unavailable"):
        read(registry, {"operation": "work_map", "project": catalog(registry)["projects"][0]["id"]})
    registry.write_text("x" * 262145)
    with pytest.raises(ValueError, match="large"):
        catalog(registry)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"operation": "projects", "offset": True},
        {"operation": "evidence", "project": "a" * 20, "id": "x", "offset": -1},
    ],
)
def test_rejects_invalid_envelopes(tmp_path, payload):
    with pytest.raises(ValueError):
        read(tmp_path / "missing", payload)
