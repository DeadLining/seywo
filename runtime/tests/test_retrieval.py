"""Deterministic retrieval regressions: never invoke a provider."""

import json
import socket

import pytest

from sessiongraph.core import Store, init_project, load_config
from sessiongraph.extraction import run_one
from test_mvp import write_session


@pytest.fixture
def indexed(tmp_path):
    cfg = load_config(init_project(tmp_path))
    s = Store(cfg)
    s.ingest(
        write_session(
            tmp_path / "chat.jsonl",
            tmp_path,
            "chat",
            [
                ("user", "检索的时候不能有大模型参与，检索是最频繁的事情。"),
                ("user", "是不是可以用 compact hook 触发整理？"),
                ("user", "为了降低日常维护开销。"),
                ("user", "旧版本运行在服务器。"),
            ],
        )
    )
    entries = [dict(e) for e in s.db.execute("SELECT * FROM entries ORDER BY line")]

    def extract(batch, policy):
        return {
            "summary": "fixture, not model-quality evidence",
            "records": [
                {
                    "id": "c",
                    "kind": "Constraint",
                    "content": "检索时不得调用大模型",
                    "scope": "当前项目",
                    "aliases": ["查询成本约束"],
                    "evidence": [{"entry_id": entries[0]["id"], "quote": entries[0]["text"]}],
                },
                {
                    "id": "p",
                    "kind": "Proposal",
                    "content": "考虑 compact hook 整理",
                    "scope": "当前阶段",
                    "aliases": ["后台触发机制"],
                    "evidence": [{"entry_id": entries[1]["id"], "quote": entries[1]["text"]}],
                },
                {
                    "id": "reason",
                    "kind": "Constraint",
                    "content": "减少维护开销",
                    "scope": "当前项目",
                    "aliases": ["管理负担"],
                    "evidence": [{"entry_id": entries[2]["id"], "quote": entries[2]["text"]}],
                },
            ],
            "relations": [
                {
                    "source": "p",
                    "target": "reason",
                    "kind": "MOTIVATED_BY",
                    "evidence": [{"entry_id": entries[2]["id"], "quote": entries[2]["text"]}],
                }
            ],
        }

    s.enqueue()
    assert run_one(s, extract)["state"] == "done"
    yield s, entries
    s.close()


def sources(hit):
    return {hit["id"]} if hit["type"] == "entry" else {e["entry_id"] for e in hit["evidence"]}


def test_function_word_cannot_outrank_model_constraint(indexed):
    s, entries = indexed
    h = s.search("查询阶段的模型成本限制")["hits"][0]
    assert entries[0]["id"] in sources(h)


def test_same_evidence_not_returned_twice(indexed):
    s, _ = indexed
    hits = s.search("检索 大模型")["hits"]
    seen = set()
    for hit in hits:
        assert not sources(hit) & seen
        seen |= sources(hit)


def test_stop_words_not_search_signals_but_meaningful_single_han_survives(indexed):
    s, _ = indexed
    assert s.search("的 了 是")["hits"] == []
    assert s.search("旧")["hits"]


def test_raw_and_hybrid_modes_alias_only(indexed):
    s, entries = indexed
    assert s.search("查询成本约束", mode="raw")["model_calls"] == 0
    alias = s.search("管理负担", mode="hybrid")["hits"][0]
    assert alias["type"] == "record"
    assert entries[2]["id"] in sources(alias)
    assert all(h["type"] == "entry" for h in s.search("检索", mode="raw")["hits"])
    with pytest.raises(ValueError):
        s.search("检索", mode="bogus")


def test_graph_adds_evidence_not_present_in_text_seed(indexed, monkeypatch):
    s, entries = indexed

    def blocked(*a, **k):
        raise AssertionError("network prohibited")

    monkeypatch.setattr(socket, "socket", blocked)
    b = s.search("compact hook", limit=1, mode="hybrid")
    c = s.search("compact hook", limit=1, mode="graph")
    assert b["hits"][0]["id"] == c["hits"][0]["id"]
    assert not b["related"]
    assert any(
        entries[2]["id"] in {e["entry_id"] for e in item["evidence"]} for item in c["related"]
    )
    assert c["model_calls"] == 0
    assert c["related"][0]["relation"] == "MOTIVATED_BY"
    assert c["related"][0]["edge_evidence"]
    assert c["budget"]["returned_text_chars"] <= c["budget"]["max_text_chars"]


def test_search_size_and_graph_budget(indexed):
    s, _ = indexed
    result = s.search("compact hook", mode="graph", max_chars=256)
    assert sum(len(h["snippet"]) for h in result["hits"] + result["related"]) <= 256
    assert len(json.dumps(result, ensure_ascii=False)) < 6000
    assert result["retrieval"]["version"] == "evidence-v2"


def test_alias_flood_does_not_steal_raw_channel(indexed):
    s, entries = indexed
    job = s.db.execute("SELECT id FROM jobs LIMIT 1").fetchone()[0]
    # Simulate many generated aliases without another model call.
    with s.db:
        for i in range(220):
            rid = f"alias-noise-{i}"
            s.db.execute(
                "INSERT INTO records VALUES (?,?,?,?,?,?)",
                (rid, job, "Proposal", "候选记录", "待定", '["检索 大模型"]'),
            )
            s.db.execute(
                "INSERT INTO evidence VALUES (?,?,?,?,?)",
                (rid, entries[1]["id"], entries[1]["text"], 0, len(entries[1]["text"])),
            )
            s.db.execute("INSERT INTO docs VALUES (?,?,?)", (rid, "record", "检索 大模型"))
    result = s.search("检索 大模型", limit=3)
    assert entries[0]["id"] in sources(result["hits"][0])
    assert result["retrieval"]["candidate_limit_reached"]
    raw = s.search("检索 大模型", mode="raw")
    assert raw["hits"][0]["id"] == result["hits"][0]["id"]


def test_graph_never_uses_old_policy_or_leaks_other_chat(indexed):
    s, _ = indexed
    assert not s.search("compact hook", session_id="different", mode="graph")["related"]
    s.config["policy_hash"] = "unbuilt-policy"
    r = s.search("compact hook", mode="graph")
    assert r["hits"] and not r["related"]
    assert all(h["type"] == "entry" for h in r["hits"])


def test_same_text_different_speakers_not_collapsed(indexed, tmp_path):
    s, _ = indexed
    text = "角色归属必须区分"
    s.ingest(
        write_session(
            tmp_path / "speakers.jsonl", tmp_path, "speakers", [("user", text), ("assistant", text)]
        )
    )
    r = s.search(text, mode="raw", session_id="speakers")
    assert {h["role"] for h in r["hits"]} == {"user", "assistant"}


def test_cjk_terms_are_not_lost_to_unigram_first_cap():
    from sessiongraph.retrieval import query_terms

    terms = query_terms("查询阶段的模型成本限制")
    assert "的" not in terms
    assert "模型" in terms
    assert query_terms("") == []
    assert len(query_terms(" ".join(f"identifier_{i}" for i in range(150)))) == 80


def test_preindexed_tokens_score_equals_original_text():
    from sessiongraph.core import tokens
    from sessiongraph.retrieval import lexical, query_terms

    for text in [
        "检索不调用模型",
        "compile_error TASK_ID /src/module.py",
        "multi " + "重复记录" * 1000,
    ]:
        terms = query_terms("检索 模型 compile_error 重复记录")
        assert lexical(text, terms, present=set(tokens(text))) == lexical(text, terms)


def test_indexed_token_membership_respects_boundaries():
    from sessiongraph.retrieval import IndexedTokens

    indexed = IndexedTokens("modulex 检索 检 索")
    assert "module" not in indexed
    assert "modulex" in indexed
    assert "检索" in indexed
    assert "索引" not in indexed


def test_graph_scope_does_not_cross_chat_on_current_policy_edge(indexed, tmp_path):
    s, entries = indexed
    other_path = write_session(tmp_path / "other.jsonl", tmp_path, "other-chat", [("user", "foreign evidence")])
    s.ingest(other_path)
    other_entry = s.db.execute(
        "SELECT id FROM entries WHERE session_id != ? LIMIT 1", (entries[0]["session_id"],)
    ).fetchone()[0]
    with s.db:
        s.db.execute(
            "INSERT INTO jobs(id,session_id,start_line,end_line,policy_hash,state) VALUES (?,?,?,?,?,?)",
            ("foreign-job", "other-chat", 2, 2, s.config["policy_hash"], "done"),
        )
        s.db.execute(
            "INSERT INTO records VALUES (?,?,?,?,?,?)",
            ("foreign-record", "foreign-job", "Proposal", "跨聊天不应带出", "other-chat", "[]"),
        )
        s.db.execute(
            "INSERT INTO docs VALUES (?,?,?)", ("foreign-record", "record", "跨聊天 不应 带出")
        )
        s.db.execute(
            "INSERT INTO evidence VALUES (?,?,?,?,?)",
            ("foreign-record", other_entry, "foreign evidence", 0, len("foreign evidence")),
        )
        s.db.execute(
            "INSERT INTO edges VALUES (?,?,?,?,?)",
            (
                "foreign-edge",
                "foreign-job",
                s.db.execute("SELECT id FROM records WHERE content LIKE ? LIMIT 1", ("%compact hook%",)).fetchone()[0],
                "foreign-record",
                "RELATED_TO",
            ),
        )
    result = s.search("compact hook", limit=1, mode="graph", chat_id="chat")
    assert all(item["id"] != "foreign-record" for item in result["related"])
