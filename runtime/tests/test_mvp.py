import json
import socket

import pytest

from sessiongraph.core import Store, init_project, load_config
from sessiongraph.extraction import run_one


def write_session(path, cwd, sid, texts):
    rows = [{"type": "session_meta", "payload": {"id": sid, "cwd": str(cwd)}}]
    for role, text in texts:
        rows.append(
            {
                "timestamp": "2026-09-30T00:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": role,
                    "content": [
                        {"type": "input_text" if role == "user" else "output_text", "text": text}
                    ],
                },
            }
        )
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return path


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    cfg_path = init_project(root)
    store = Store(load_config(cfg_path))
    yield root, store
    store.close()


def seed(project):
    root, s = project
    a = write_session(
        root / "a.jsonl",
        root,
        "a",
        [
            ("user", "检索的时候不能有大模型参与"),
            ("assistant", "可以用离线提取和全文搜索。"),
        ],
    )
    b = write_session(
        root / "b.jsonl",
        root,
        "b",
        [
            ("user", "先别上 Redis，单机够用了，别增加运维负担。"),
            ("assistant", "建议后台异步提取，检索不调用大模型。"),
        ],
    )
    s.ingest(a)
    s.ingest(b)
    return a, b


def test_cross_chat_chinese_search_without_network(project, monkeypatch):
    _, s = project
    seed(project)

    def forbidden(*a, **kw):
        raise AssertionError("search must not use network")

    monkeypatch.setattr(socket, "socket", forbidden)
    result = s.search("检索 大模型")
    assert {x["session_id"] for x in result["hits"]} == {"a", "b"}
    assert result["model_calls"] == 0
    assert result["coverage"]["raw_entries"] == 4
    assert result["coverage"]["semantic_entries"] == 0
    assert s.search("Redis")["hits"][0]["session_id"] == "b"
    assert s.search('" OR * ; DROP TABLE entries; --')["model_calls"] == 0


def test_incremental_partial_and_changed_source(project):
    root, s = project
    a, _ = seed(project)
    assert s.ingest(a)["added"] == 0
    row = json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "新的决定"}],
            },
        },
        ensure_ascii=False,
    )
    with a.open("a") as f:
        f.write(row[:20])
    assert s.ingest(a)["added"] == 0
    with a.open("a") as f:
        f.write(row[20:] + "\n")
    assert s.ingest(a)["added"] == 1
    hit = s.search("新的决定")["hits"][0]
    expanded = s.expand(hit["id"])
    assert any(e["text"] == "新的决定" for e in expanded["entries"])
    a.write_text(a.read_text().replace("新的决定", "新的方案"))
    with pytest.raises(ValueError, match="changed"):
        s.ingest(a)
    assert s.status()["raw_entries"] == 5


def test_project_isolation_and_invalid_inputs(project, tmp_path):
    _, s = project
    other = write_session(tmp_path / "other.jsonl", tmp_path, "other", [("user", "secret")])
    with pytest.raises(ValueError, match="project"):
        s.ingest(other)
    with pytest.raises(ValueError):
        s.search("")
    with pytest.raises(ValueError):
        s.search("a", limit=101)
    with pytest.raises(ValueError):
        s.expand("not-here")
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"oops":}\n')
    with pytest.raises(ValueError):
        s.ingest(bad)


def test_atomic_ingest_and_tool_pairing(project):
    root, s = project
    p = write_session(root / "tools.jsonl", root, "tools", [])
    rows = [
        {
            "type": "function_call",
            "call_id": "call1",
            "name": "exec_command",
            "arguments": '{"cmd":"pytest"}',
        },
        {"type": "function_call_output", "call_id": "call1", "output": "3 passed"},
    ]
    with p.open("a") as f:
        for row in rows:
            f.write(json.dumps({"type": "response_item", "payload": row}) + "\n")
    s.ingest(p)
    hit = s.search("passed")["hits"][0]
    view = s.expand(hit["id"], radius=0)
    assert {e["kind"] for e in view["entries"]} == {"tool_call", "tool_result"}
    before = s.status()["raw_entries"]
    with p.open("a") as f:
        f.write(json.dumps({"type": "response_item", "payload": rows[0]}) + "\n{bad}\n")
    with pytest.raises(ValueError):
        s.ingest(p)
    assert s.status()["raw_entries"] == before


def good_extract(batch, policy):
    entry = next(e for e in batch if "Redis" in e["text"])
    return {
        "summary": "本阶段讨论缓存部署。",
        "records": [
            {
                "id": "c1",
                "kind": "Constraint",
                "content": "避免增加运维负担",
                "scope": "当前任务",
                "aliases": ["部署复杂度"],
                "evidence": [{"entry_id": entry["id"], "quote": "别增加运维负担"}],
            },
            {
                "id": "d1",
                "kind": "Decision",
                "content": "暂不采用 Redis",
                "scope": "当前任务",
                "aliases": ["缓存选型"],
                "evidence": [{"entry_id": entry["id"], "quote": "先别上 Redis"}],
            },
        ],
        "relations": [
            {
                "source": "d1",
                "target": "c1",
                "kind": "MOTIVATED_BY",
                "evidence": [
                    {"entry_id": entry["id"], "quote": "先别上 Redis，单机够用了，别增加运维负担。"}
                ],
            }
        ],
    }


def test_queue_graph_evidence_and_coverage(project):
    root, s = project
    p = write_session(
        root / "b.jsonl", root, "b", [("user", "先别上 Redis，单机够用了，别增加运维负担。")]
    )
    s.ingest(p)
    assert s.enqueue() == 1
    assert s.enqueue() == 0
    assert run_one(s, good_extract)["state"] == "done"
    assert run_one(s, good_extract)["state"] == "idle"
    assert s.status()["semantic_entries"] == 1
    assert s.enqueue() == 0
    res = s.search("缓存选型")
    record = next(h for h in res["hits"] if h["type"] == "record")
    expanded = s.expand(record["id"])
    assert expanded["record"]["kind"] == "Decision"
    assert expanded["related"][0]["kind"] == "Constraint"
    assert expanded["evidence"][0]["quote"] == "先别上 Redis"
    assert expanded["entries"][0]["session_id"] == "b"


def test_invalid_graph_cannot_advance_coverage(project):
    root, s = project
    s.ingest(
        write_session(
            root / "b.jsonl", root, "b", [("user", "先别上 Redis，单机够用了，别增加运维负担。")]
        )
    )
    s.enqueue()

    def bad(batch, policy):
        out = good_extract(batch, policy)
        out["records"][0]["evidence"][0]["quote"] = "fabricated"
        return out

    assert run_one(s, bad)["state"] == "failed"
    assert s.status()["semantic_entries"] == 0
    assert s.status()["records"] == 0
    assert s.retry_failed() == 1
    assert run_one(s, good_extract)["state"] == "done"


def test_budget_and_retrieval_truncation(project):
    root, s = project
    s.ingest(write_session(root / "long.jsonl", root, "long", [("user", "needle " + "x" * 40000)]))
    s.enqueue()

    def forbidden(*args):
        raise AssertionError("over-budget entry must not go to provider")

    assert run_one(s, forbidden)["state"] == "failed"
    assert s.status()["semantic_entries"] == 0
    result = s.search("needle", max_chars=1000)
    assert len(json.dumps(result, ensure_ascii=False)) < 4000
    view = s.expand(result["hits"][0]["id"], max_chars=1000)
    assert view["entries"][0]["truncated"]
    assert len(view["entries"][0]["text"]) <= 1000


@pytest.mark.parametrize("header", [[], "not an object", 7, None])
def test_nonobject_metadata_rejected_cleanly(project, header):
    root, s = project
    path = root / "malformed.jsonl"
    path.write_text(json.dumps(header) + "\n")
    with pytest.raises(ValueError, match="metadata"):
        s.ingest(path)


def test_parallel_imports_are_idempotent(project):
    from concurrent.futures import ThreadPoolExecutor

    root, s = project
    path = write_session(root / "parallel.jsonl", root, "parallel", [("user", "并发写入")])

    def ingest_once(_):
        store = Store(s.config)
        try:
            return store.ingest(path)["added"]
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(ingest_once, range(12))) == 1
    assert s.status()["raw_entries"] == 1


def test_parallel_enqueue_and_no_match(project):
    from concurrent.futures import ThreadPoolExecutor

    root, s = project
    seed(project)

    def enqueue_once(_):
        store = Store(s.config)
        try:
            return store.enqueue()
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(enqueue_once, range(12))) == 2
    assert s.search("XYZ_UNSEEN_TERM")["hits"] == []
    assert s.search("Redis", session_id="a")["hits"] == []


def test_entry_paging(project):
    root, s = project
    s.ingest(
        write_session(root / "pages.jsonl", root, "pages", [("user", "needle " + "abcd" * 300)])
    )
    eid = s.search("needle")["hits"][0]["id"]
    view = s.expand(eid, radius=0, max_chars=256)
    assert view["entries"][0]["next_offset"] == 256
    view2 = s.expand(eid, radius=0, max_chars=256, offset=256)
    assert view2["entries"][0]["offset"] == 256
    assert view2["entries"][0]["text"] == ("needle " + "abcd" * 300)[256:512]


def test_multiple_rollouts_for_one_chat(project):
    root, s = project
    a = write_session(root / "segment-a.jsonl", root, "chat-one", [("user", "beforecompact alpha")])
    b = write_session(root / "segment-b.jsonl", root, "chat-one", [("user", "aftercompact beta")])
    s.ingest(a)
    s.ingest(b)
    assert s.ingest(b)["added"] == 0
    assert s.status()["sessions"] == 1
    assert s.status()["source_segments"] == 2
    hit = s.search("aftercompact", session_id="chat-one")["hits"][0]
    assert hit["chat_id"] == "chat-one"
    assert s.expand(hit["id"])["entries"][0]["chat_id"] == "chat-one"
    assert s.enqueue("chat-one") == 2
    assert s.enqueue("chat-one") == 0
    assert s.search("aftercompact", session_id="other")["hits"] == []


def test_source_identity_change_is_rejected(project):
    root, s = project
    path = write_session(root / "same-path.jsonl", root, "a", [("user", "one")])
    s.ingest(path)
    write_session(path, root, "b", [("user", "two")])
    with pytest.raises(ValueError, match="different chat id"):
        s.ingest(path)
    assert s.status()["sessions"] == 1


def test_existing_database_backfills_chat_mapping(project):
    _, s = project
    seed(project)
    with s.db:
        s.db.execute("DROP TABLE session_chats")
    reopened = Store(s.config)
    try:
        assert reopened.status()["sessions"] == 2
        assert reopened.search("Redis")["hits"][0]["chat_id"] == "b"
    finally:
        reopened.close()


def test_expand_does_not_leak_edges_from_an_old_policy(project):
    root, s = project
    s.ingest(
        write_session(
            root / "current.jsonl",
            root,
            "current",
            [("user", "先别上 Redis，单机够用了，别增加运维负担。")],
        )
    )
    s.enqueue()
    assert run_one(s, good_extract)["state"] == "done"
    current = s.db.execute("SELECT id FROM records ORDER BY id LIMIT 1").fetchone()[0]
    session_id = s.db.execute("SELECT id FROM sessions LIMIT 1").fetchone()[0]
    with s.db:
        s.db.execute(
            "INSERT INTO jobs(id,session_id,start_line,end_line,policy_hash,state) VALUES (?,?,?,?,?,?)",
            ("old-job", session_id, 2, 2, "old-policy", "failed"),
        )
        s.db.execute(
            "INSERT INTO records VALUES (?,?,?,?,?,?)",
            ("old-record", "old-job", "Decision", "旧策略记录", "旧策略", "[]"),
        )
        s.db.execute(
            "INSERT INTO edges VALUES (?,?,?,?,?)",
            ("old-edge", "old-job", current, "old-record", "RELATED_TO"),
        )
    expanded = s.expand(current)
    assert all(item["id"] != "old-edge" for item in expanded["edges"])
    assert all(item["id"] != "old-record" for item in expanded["related"])
