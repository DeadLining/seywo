"""Read-only graph payload and HTTP access boundaries."""

from sessiongraph.core import Store, init_project, load_config
from sessiongraph.views import graph_data
from test_mvp import write_session
from test_retrieval import indexed  # noqa: F401


def test_graph_raw_and_read_only(tmp_path):
    config = load_config(init_project(tmp_path))
    with_store = Store(config)
    with_store.ingest(write_session(tmp_path / "s.jsonl", tmp_path, "chat", [("user", "原始证据")]))
    before = with_store.db.total_changes
    data = graph_data(with_store, "", "")
    assert {n["type"] for n in data["nodes"]} == {"chat", "entry"}
    assert data["edges"][0]["kind"] == "CONTAINS"
    assert data["status"]["records"] == 0
    assert with_store.db.total_changes == before
    assert graph_data(with_store, "不存在的词", "")["nodes"] == []
    with_store.close()


def test_semantic_edges_and_evidence(indexed):  # noqa: F811
    store, entries = indexed
    data = graph_data(store, "compact hook", "")
    ids = {n["id"] for n in data["nodes"]}
    assert entries[1]["id"] in ids
    assert any(e["kind"] == "MOTIVATED_BY" and e.get("evidence") for e in data["edges"])
    assert any(e["kind"] == "EVIDENCED_BY" for e in data["edges"])
    assert all(e["source"] in ids and e["target"] in ids for e in data["edges"])


def test_graph_includes_work_memory_and_marks_truncation(tmp_path):
    """work_memory must appear in the graph; over-limit draws set truncated."""
    from sessiongraph.core import Store, init_project, load_config
    from sessiongraph.views import graph_data, memory_detail
    from sessiongraph.memory import remember
    from test_mvp import write_session

    root = tmp_path / "project"
    root.mkdir()
    cfg = init_project(root)
    store = Store(load_config(cfg))
    path = write_session(root / "a.jsonl", root, "chat-a", [("user", "目标是完成论文调研")])
    store.ingest(path)
    eid = store.db.execute("SELECT id FROM entries LIMIT 1").fetchone()[0]
    mid = remember(
        store,
        kind="goal",
        title="论文调研",
        content="完成论文调研",
        status="active",
        source_entry_id=eid,
        source_quote="目标是完成论文调研",
    )["id"]

    data = graph_data(store)
    mem = [n for n in data["nodes"] if n["type"] == "memory"]
    assert [n["id"] for n in mem] == [mid]
    assert data["memory_stats"]["active"] == 1
    assert data["truncated"] is False
    # The memory links to its source entry when sources are shown.
    assert any(e["source"] == mid and e["kind"] == "EVIDENCED_BY" for e in data["edges"])

    detail = memory_detail(store, mid)
    assert detail["memory"]["title"] == "论文调研"
    assert detail["memory"]["source"]["quote"] == "目标是完成论文调研"
    assert detail["memory"]["scope"] == str(root)
    assert detail["memory"]["project_id"] == store.config["project_id"]
    store.close()
