"""Incremental work-memory extraction with a deterministic fake extractor."""

import json

import pytest

from sessiongraph.core import Store, init_project, load_config
from sessiongraph.memories import policy_for, run_chat, validate_memories
from test_mvp import write_session


class FakeExtractor:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.last_usage = {"total_tokens": 12}

    def __call__(self, batch, policy):
        self.calls.append((batch, policy))
        if not self.outputs:
            return {"memories": []}
        return self.outputs.pop(0)


def make_store(tmp_path, texts, sid="chat-1"):
    root = tmp_path / "project"
    root.mkdir()
    cfg = init_project(root)
    store = Store(load_config(cfg))
    path = write_session(root / "chat.jsonl", root, sid, texts)
    store.ingest(path)
    return root, store, path


def entries(store):
    return [dict(r) for r in store.db.execute(
        "SELECT id, text FROM entries ORDER BY line").fetchall()]


def test_policy_is_deterministic(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    cfg = load_config(init_project(root))
    assert policy_for(cfg) == policy_for(cfg)
    assert "work_memory_extraction" in json.dumps(policy_for(cfg))


def test_validate_rejects_foreign_and_inexact_quotes(tmp_path):
    _, store, _ = make_store(tmp_path, [("user", "目标：搭建测试"), ("assistant", "已开始")])
    batch = entries(store)
    eid = batch[0]["id"]
    ok = {"memories": [{
        "kind": "goal", "title": "搭建测试", "content": "搭建测试", "scope": "project",
        "status": "active", "aliases": [], "entry_id": eid, "quote": "目标：搭建测试",
    }]}
    assert len(validate_memories(ok, batch)) == 1
    bad = json.loads(json.dumps(ok))
    bad["memories"][0]["quote"] = "不在原文里的话"
    with pytest.raises(ValueError, match="exact source substring"):
        validate_memories(bad, batch)
    bad2 = json.loads(json.dumps(ok))
    bad2["memories"][0]["entry_id"] = "e_foreign"
    with pytest.raises(ValueError, match="outside input batch"):
        validate_memories(bad2, batch)
    store.close()


def test_run_chat_saves_only_new_entries_and_dedupes(tmp_path):
    texts = [("user", "目标是完成论文调研"),
             ("assistant", "建议使用 SessionGraph 保存关键事项"),
             ("user", "好的，决定采用 SessionGraph")]
    _, store, path = make_store(tmp_path, texts)
    batch = entries(store)
    user_goal, user_decision = batch[0], batch[2]
    out = {"memories": [
        {"kind": "goal", "title": "论文调研", "content": "完成论文调研", "scope": "project",
         "status": "active", "aliases": ["调研"], "entry_id": user_goal["id"],
         "quote": "目标是完成论文调研"},
        {"kind": "decision", "title": "采用SessionGraph", "content": "用SessionGraph保存关键事项",
         "scope": "project", "status": "active", "aliases": [],
         "entry_id": user_decision["id"], "quote": "决定采用 SessionGraph"},
    ]}
    extractor = FakeExtractor([out])
    result = run_chat(store, extractor, "chat-1")
    assert len(result["saved"]) == 2
    assert result["model_calls"] == 1
    memories = store.resume(chat_id="chat-1")["memories"]
    assert {m["kind"] for m in memories} == {"goal", "decision"}
    for m in memories:
        assert m["source"]["entry_id"] in {user_goal["id"], user_decision["id"]}

    # Second run with no new entries: extractor must not be called.
    extractor2 = FakeExtractor([out])
    again = run_chat(store, extractor2, "chat-1")
    assert again["saved"] == [] and again["model_calls"] == 0
    assert extractor2.calls == []

    # Append new text; only the tail is sent, duplicates are skipped.
    with path.open("a") as f:
        for role, text in [("user", "目标是完成论文调研"), ("user", "下一步是写实验章节")]:
            f.write(json.dumps({
                "timestamp": "2026-09-30T00:01:00Z", "type": "response_item",
                "payload": {"type": "message", "role": role,
                            "content": [{"type": "input_text", "text": text}]},
            }, ensure_ascii=False) + "\n")
    store.ingest(path)
    new_batch = entries(store)[3:]
    out2 = {"memories": [
        {"kind": "goal", "title": "论文调研", "content": "完成论文调研", "scope": "project",
         "status": "active", "aliases": [], "entry_id": new_batch[0]["id"],
         "quote": "目标是完成论文调研"},
        {"kind": "next_step", "title": "写实验章节", "content": "下一步写实验章节",
         "scope": "project", "status": "active", "aliases": [],
         "entry_id": new_batch[1]["id"], "quote": "下一步是写实验章节"},
    ]}
    extractor3 = FakeExtractor([out2])
    third = run_chat(store, extractor3, "chat-1")
    sent_texts = [e["text"] for e in extractor3.calls[0][0]]
    assert sent_texts == ["目标是完成论文调研", "下一步是写实验章节"]
    assert len(third["saved"]) == 1  # duplicate goal skipped
    assert third["skipped"][0]["reason"] == "duplicate"
    store.close()


def test_run_chat_requires_known_chat(tmp_path):
    _, store, _ = make_store(tmp_path, [("user", "你好")])
    with pytest.raises(ValueError, match="unknown chat_id"):
        run_chat(store, FakeExtractor([]), "no-such-chat")
    store.close()


def test_run_chat_chunks_oversize_history(tmp_path):
    big = "长" * 9000  # every character must be covered by bounded fragments
    texts = [("user", "第一句"), (("user"), big), ("user", "最后一句")]
    _, store, _ = make_store(tmp_path, texts)
    extractor = FakeExtractor([{"memories": []}])
    result = run_chat(store, extractor, "chat-1", max_chars=4000)
    assert result["model_calls"] == 5  # two short batches + three long fragments
    sent = [e for batch, _ in extractor.calls for e in batch]
    assert sent[0]["text"] == "第一句" and sent[-1]["text"] == "最后一句"
    assert result["dropped_oversize"] == 0
    assert all(len(e["text"]) <= 4000 for e in sent)
    assert sent[-2]["source_end"] == len(big)
    # Second run: nothing new.
    again = run_chat(store, FakeExtractor([]), "chat-1", max_chars=4000)
    assert again["model_calls"] == 0
    store.close()


class FlakyExtractor:
    """Fails large batches the way a reasoning-heavy local model does."""

    def __init__(self, fail_above):
        self.fail_above = fail_above
        self.calls = []
        self.last_usage = None

    def __call__(self, batch, policy):
        self.calls.append(len(batch))
        if len(batch) > self.fail_above:
            raise ValueError("Responses contained no assistant output_text")
        first = batch[0]
        return {"memories": [{
            "kind": "finding", "title": f"f{batch[0]['line']}",
            "content": "找到一个结论", "scope": "project", "status": "active",
            "aliases": [], "entry_id": first["id"], "quote": first["text"],
        }]}


def test_split_retry_recovers_from_empty_output(tmp_path):
    texts = [("user", f"结论{i}是有效的") for i in range(8)]
    _, store, _ = make_store(tmp_path, texts)
    extractor = FlakyExtractor(fail_above=3)
    result = run_chat(store, extractor, "chat-1", max_chars=100000)
    # 8 entries: first call fails -> split 4/4 -> each fails -> split 2/2 each.
    assert sorted(extractor.calls, reverse=True)[0] == 8
    assert result["model_calls"] > 1
    assert len(result["saved"]) == 4  # one memory per successful 2-entry batch
    store.close()


def test_non_retryable_error_is_reported_not_raised(tmp_path):
    """A bad batch must not abort the run; it is reported for the next attempt."""
    _, store, _ = make_store(tmp_path, [("user", "一句话")])

    class Bad:
        last_usage = None

        def __call__(self, batch, policy):
            raise ValueError("output requires exactly the memories field")

    result = run_chat(store, Bad(), "chat-1")
    assert result["saved"] == []
    assert result["failed"][0]["error_code"] == "extraction_validation"
    assert result["failed"][0]["retryable"] is False
    assert "error" not in result["failed"][0]  # do not persist arbitrary exception text
    # Cursor did not advance, so a later run retries the same range.
    again = run_chat(store, FakeExtractor([{"memories": []}]), "chat-1")
    assert again["model_calls"] == 1
    store.close()


def test_segment_scope_limits_scan(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    cfg = init_project(root)
    store = Store(load_config(cfg))
    from test_mvp import write_session

    a = write_session(root / "a.jsonl", root, "chat-1", [("user", "第一段")])
    b = write_session(root / "b.jsonl", root, "chat-1", [("user", "第二段")])
    store.ingest(a)
    store.ingest(b)
    segments = [r[0] for r in store.db.execute("SELECT segment_id FROM session_chats")]
    extractor = FakeExtractor([{"memories": []}])
    run_chat(store, extractor, "chat-1", segment_id=segments[0])
    sent = [e["text"] for e in extractor.calls[0][0]]
    assert sent == ["第一段"]
    store.close()


def test_cursor_advances_per_batch_when_interrupted(tmp_path):
    """A batch that succeeds must persist its high-water mark immediately.

    Regression: state was written once after every segment finished, so an
    interrupted run saved memories but rewound the cursor, re-extracting the
    same range on the next run.
    """
    from sessiongraph.memories import _state_path

    texts = [("user", f"结论{i}是有效的") for i in range(6)]
    _, store, _ = make_store(tmp_path, texts)

    class FailOnSecondBatch:
        last_usage = None

        def __init__(self):
            self.calls = 0

        def __call__(self, batch, policy):
            self.calls += 1
            if self.calls == 2:
                raise ValueError("output requires exactly the memories field")
            first = batch[0]
            return {"memories": [{
                "kind": "finding", "title": f"结论 {first['line']}",
                "content": "找到一个结论", "status": "active",
                "aliases": [], "entry_id": first["id"], "quote": first["text"],
            }]}

    state_path = _state_path(store, "chat-1")
    first_lines = [e["id"] for e in entries(store)]
    result = run_chat(store, FailOnSecondBatch(), "chat-1", max_chars=7)
    # max_chars=7 forces one entry per batch, so the second batch fails.
    assert result["failed"], "expected the failing batch to be reported"
    assert len(result["saved"]) == 1, "the first batch should have been saved"

    # The cursor must have advanced past the first batch, and state must be valid JSON.
    state = json.loads(state_path.read_text())
    marks = list(state["segments"].values())
    assert marks, "cursor must be persisted even though the run failed mid-way"
    assert marks[0] > 1, "cursor must cover the batch that already succeeded"
    assert first_lines, "sanity"
    store.close()


def test_work_memory_is_visible_across_chats(tmp_path):
    """Work memory is project-scoped: a fact saved in one chat is resume-visible
    from another chat in the same project."""
    root = tmp_path / "project"
    root.mkdir()
    cfg = init_project(root)
    store = Store(load_config(cfg))
    a = write_session(root / "a.jsonl", root, "chat-a", [("user", "目标是完成论文调研")])
    store.ingest(a)
    batch = entries(store)
    extractor = FakeExtractor([{"memories": [{
        "kind": "goal", "title": "论文调研", "content": "完成论文调研",
        "status": "active", "aliases": [], "entry_id": batch[0]["id"],
        "quote": "目标是完成论文调研",
    }]}])
    run_chat(store, extractor, "chat-a")

    # A different chat in the same project sees it.
    b = write_session(root / "b.jsonl", root, "chat-b", [("user", "另一个会话")])
    store.ingest(b)
    resumed = store.resume()["memories"]
    assert [m["title"] for m in resumed] == ["论文调研"]

    # Scoping to the other chat still filters, when explicitly requested.
    assert store.resume(chat_id="chat-b")["memories"] == []

    # And it is tagged with the project, not left implicit.
    row = store.db.execute("SELECT project_id, scope FROM work_memory").fetchone()
    assert row["project_id"] == cfg_project_id(cfg)
    assert row["scope"] == str(root), "scope is derived from config, not the model"
    store.close()


def cfg_project_id(cfg_path):
    return load_config(cfg_path)["project_id"]


def test_scope_is_not_model_controlled(tmp_path):
    """The model cannot influence scope: it is derived from configured roots."""
    _, store, _ = make_store(tmp_path, [("user", "目标：搭建测试")])
    batch = entries(store)
    item = {"memories": [{
        "kind": "goal", "title": "搭建测试", "content": "搭建测试",
        "scope": "模型编造的范围", "status": "active", "aliases": [],
        "entry_id": batch[0]["id"], "quote": "目标：搭建测试",
    }]}
    validated = validate_memories(item, batch)
    assert "scope" not in validated[0], "model-supplied scope must be dropped"
    run_chat(store, FakeExtractor([item]), "chat-1")
    row = store.db.execute("SELECT scope FROM work_memory").fetchone()
    assert row["scope"] != "模型编造的范围"
    assert str(tmp_path) in row["scope"]
    store.close()


def test_unknown_tool_results_remain_evidence(tmp_path):
    """Unknown output is preserved; known process noise is filtered separately."""
    root = tmp_path / "project"
    root.mkdir()
    cfg = init_project(root)
    store = Store(load_config(cfg))
    path = root / "chat.jsonl"
    raw = [
        {"timestamp": "2026-09-30T00:00:00Z", "type": "session_meta",
         "payload": {"id": "chat-1", "cwd": str(root)}},
        {"timestamp": "2026-09-30T00:00:01Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "shell", "arguments": "echo hi",
                     "call_id": "c1"}},
        {"timestamp": "2026-09-30T00:00:02Z", "type": "response_item",
         "payload": {"type": "function_call_output", "output": "Chunk ID: deadbeef " + "x" * 300,
                     "call_id": "c1"}},
        {"timestamp": "2026-09-30T00:00:03Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "结论：需要扩大批次"}]}},
    ]
    path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in raw) + "\n")
    store.ingest(path)

    extractor = FakeExtractor([{"memories": []}])
    run_chat(store, extractor, "chat-1")
    kinds = [e["kind"] for e in extractor.calls[0][0]]
    assert "tool_result" in kinds, "unknown output must not be silently discarded"
    assert "tool_call" in kinds, "the agent's own call is still evidence"
    assert "message" in kinds
    store.close()


def test_near_duplicate_titles_are_skipped(tmp_path):
    """Re-runs across different chats produce reworded titles; those are
    duplicates for a project-scoped store."""
    root = tmp_path / "project"
    root.mkdir()
    cfg = init_project(root)
    store = Store(load_config(cfg))
    a = write_session(root / "a.jsonl", root, "chat-a", [("user", "目标是完成论文调研")])
    store.ingest(a)
    batch = entries(store)
    base = {"kind": "goal", "title": "完成论文调研", "content": "完成论文调研",
            "status": "active", "aliases": [], "entry_id": batch[0]["id"],
            "quote": "目标是完成论文调研"}
    run_chat(store, FakeExtractor([{"memories": [base]}]), "chat-a")

    b = write_session(root / "b.jsonl", root, "chat-b", [("user", "目标是完成论文调研")])
    store.ingest(b)
    batch_b = [e for e in entries(store) if e["id"] != batch[0]["id"]]
    reworded = dict(base, title="完成论文调研（重述）", entry_id=batch_b[0]["id"])
    result = run_chat(store, FakeExtractor([{"memories": [reworded]}]), "chat-b")
    assert result["saved"] == [], "a reworded duplicate must not be stored again"
    assert result["skipped"][0]["reason"] == "duplicate"
    store.close()
