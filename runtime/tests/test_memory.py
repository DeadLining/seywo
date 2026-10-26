import json
import time

import pytest

from sessiongraph.core import Store, init_project, load_config
from test_mvp import write_session


def seed_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    cfg = init_project(root)
    store = Store(load_config(cfg))
    path = write_session(root / "chat.jsonl", root, "chat-1", [
        ("user", "目标是完成论文调研，报告放在 /tmp/report.md"),
        ("assistant", "决定采用原文证据，不采用未验证的摘要。"),
    ])
    store.ingest(path)
    entry = store.search("论文调研", mode="raw")["hits"][0]["id"]
    return root, store, entry


def test_remember_recall_and_resume_are_traceable(tmp_path):
    _, store, entry = seed_store(tmp_path)
    item = store.remember(
        kind="goal", title="论文调研目标", content="完成论文调研并形成报告",
        scope="当前项目", paths=["/tmp/report.md"], aliases=["论文研究"],
        source_entry_id=entry, source_quote="目标是完成论文调研，报告放在 /tmp/report.md",
    )
    assert item["id"].startswith("wm_")
    assert item["status"] == "active"
    assert item["source"]["entry_id"] == entry
    assert item["paths"] == ["/tmp/report.md"]
    found = store.recall("论文研究")
    assert found["model_calls"] == 0
    assert found["memories"][0]["id"] == item["id"]
    resumed = store.resume()
    assert resumed["model_calls"] == 0
    assert resumed["memories"][0]["title"] == "论文调研目标"
    store.close()


def test_supersede_hides_old_from_default_resume(tmp_path):
    _, store, entry = seed_store(tmp_path)
    old = store.remember(kind="decision", title="存储方案", content="先用文件", source_entry_id=entry, source_quote="目标是完成论文调研")
    new = store.remember(
        kind="decision", title="存储方案", content="改用 SQLite", source_entry_id=entry, source_quote="目标是完成论文调研",
        supersedes_id=old["id"],
    )
    old_row = store.db.execute("SELECT status FROM work_memory WHERE id=?", (old["id"],)).fetchone()
    assert old_row[0] == "superseded"
    assert [m["id"] for m in store.resume()["memories"]] == [new["id"]]
    assert old["id"] in {m["id"] for m in store.resume(include_superseded=True)["memories"]}
    store.close()


def test_invalid_source_and_missing_fields_are_rejected(tmp_path):
    _, store, entry = seed_store(tmp_path)
    with pytest.raises(ValueError, match="source quote"):
        store.remember(kind="finding", title="x", content="y", source_entry_id=entry, source_quote="nope")
    with pytest.raises(ValueError, match="kind"):
        store.remember(kind="unknown", title="x", content="y")
    with pytest.raises(ValueError, match="title"):
        store.remember(kind="finding", title="", content="y")
    store.close()


def test_resume_is_fast_and_does_not_depend_on_chat_context(tmp_path):
    _, store, entry = seed_store(tmp_path)
    for i in range(12):
        store.remember(kind="finding", title=f"结论{i}", content=f"结果{i}", source_entry_id=entry, source_quote="目标是完成论文调研")
    start = time.perf_counter()
    result = store.resume(limit=8)
    elapsed = time.perf_counter() - start
    assert len(result["memories"]) == 8
    assert elapsed < 0.1
    assert result["memories"][0]["source"]["entry_id"] == entry
    store.close()


def test_mcp_memory_write_then_recall(tmp_path):
    import asyncio
    from sessiongraph.mcp_server import create_server

    root, store, entry = seed_store(tmp_path)
    cfg = root / ".sessiongraph" / "config.json"
    store.close()

    async def exercise():
        server = create_server(cfg)
        saved = await server.call_tool("session_remember", {
            "kind": "goal", "title": "MCP目标", "content": "从聊天工具写入",
            "source_entry_id": entry, "source_quote": "目标是完成论文调研",
        })
        assert saved and "MCP目标" in saved[0].text
        recalled = await server.call_tool("session_recall", {"query": "MCP目标"})
        assert recalled and "MCP目标" in recalled[0].text

    asyncio.run(exercise())
