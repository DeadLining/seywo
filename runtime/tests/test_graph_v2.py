import json
import pytest
from sessiongraph.graph_v2 import run_chat, diagnostics, read_relations, candidates
from sessiongraph.graph_contract import VERSION, validate, policy
from sessiongraph.jev import JevUnavailable
from test_memories import make_store, entries, FakeExtractor


class Judge:
    model = "fixture"
    endpoint = "fixture"
    calls = 0

    def __init__(self, identity="different", support="supports", mode="asserted"):
        self.identity, self.support, self.mode = identity, support, mode
        self.calls = 0

    def evaluate(self, state, questions):
        self.calls += 1
        answers = {}
        for key, q in questions.items():
            choice = (
                self.identity
                if key.startswith("p")
                else self.support
                if key.startswith("s")
                else self.mode
            )
            answers[key] = {
                "choice": choice,
                "probabilities": {c: float(c == choice) for c in q["criteria"]},
            }
        return {
            "answers": answers,
            "model": self.model,
            "usage": {"input_tokens": 10, "output_tokens": 4},
        }


class ContextExtractor(FakeExtractor):
    def __call__(self, batch, policy):
        result = super().__call__(batch, policy)
        result["coverage"]["entry_ids"] = [e["id"] for e in batch]
        return result


class Missing(Judge):
    def evaluate(self, *args):
        raise JevUnavailable("jev_not_configured")


def output(batch, title="训练 A", artifact="模型 B", quote=None, identity=None):
    ev = {"entry_id": batch[0]["id"], "quote": quote or batch[0]["text"]}

    def n(i, t, name):
        return {
            "id": i,
            "type": t,
            "title": name,
            "content": name,
            "status": "completed",
            "aliases": [],
            "identity": identity or [],
            "evidence": [ev],
        }

    return {
        "schema_version": VERSION,
        "nodes": [n("n1", "Execution", title), n("n2", "Artifact", artifact)],
        "relations": [
            {
                "source": "n1",
                "target": "n2",
                "kind": "PRODUCES",
                "statement_mode": "asserted",
                "scope": "该次训练",
                "evidence": [ev],
            }
        ],
        "unresolved": [],
        "coverage": {"entry_ids": [e["id"] for e in batch], "outcome": "candidates_found"},
    }


def test_relation_without_fixed_keyword_and_replay(tmp_path):
    _, s, _ = make_store(tmp_path, [("assistant", "训练 A 得到模型 B")])
    b = entries(s)
    ex = FakeExtractor([output(b)])
    j = Judge()
    r = run_chat(s, ex, "chat-1", jev=j)
    assert len(r["saved"]) == 2 and len(r["saved_relations"]) == 1 and j.calls == 1
    edge = read_relations(s)[0]
    assert edge["kind"] == "PRODUCES" and edge["state"] == "supported"
    r = run_chat(s, FakeExtractor([]), "chat-1", jev=j)
    assert r["model_calls"] == 0 and not r["saved"] and j.calls == 1
    assert diagnostics(s)["nodes"] == 2
    assert candidates(s, "模型 B")


def test_missing_judge_keeps_output_and_does_not_approve(tmp_path):
    _, s, _ = make_store(tmp_path, [("assistant", "训练 A 得到模型 B")])
    ex = FakeExtractor([output(entries(s))])
    r = run_chat(s, ex, "chat-1", jev=Missing())
    assert r["failure_codes"] == ["jev_not_configured"] and not r["saved_relations"]
    assert s.db.execute("select count(*) from sg2_cursors").fetchone()[0] == 0
    assert s.db.execute("select output_json from sg2_jobs").fetchone()[0]
    r = run_chat(s, FakeExtractor([]), "chat-1", jev=Judge())
    assert r["graph_v2"]["deepseek_calls"] == 0 and len(r["saved_relations"]) == 1


@pytest.mark.parametrize(
    "support,mode,state",
    [
        ("supports", "proposed", "proposed"),
        ("supports", "conditional", "conditional"),
        ("contradicts", "negated", None),
        ("insufficient_evidence", "unclear", None),
    ],
)
def test_qualified_and_unjustified_relations(tmp_path, support, mode, state):
    _, s, _ = make_store(tmp_path, [("user", "计划通过训练 A 得到模型 B")])
    ex = FakeExtractor([output(entries(s))])
    r = run_chat(s, ex, "chat-1", jev=Judge(support=support, mode=mode))
    assert not r["failed"]
    edges = read_relations(s)
    if state:
        assert edges[0]["state"] == state
    else:
        assert not edges and diagnostics(s)["unresolved"]


def test_schema_missing_relations_not_silent_empty(tmp_path):
    _, s, _ = make_store(tmp_path, [("user", "训练 A 得到模型 B")])
    o = output(entries(s))
    del o["relations"]
    r = run_chat(s, FakeExtractor([o]), "chat-1", jev=Judge())
    assert r["failure_codes"] == ["graph_validation"] and diagnostics(s)["nodes"] == 0


def test_foreign_or_fabricated_evidence_rejected(tmp_path):
    _, s, _ = make_store(tmp_path, [("user", "训练 A 得到模型 B")])
    o = output(entries(s))
    o["nodes"][0]["evidence"][0] = {"entry_id": "foreign", "quote": "训练 A"}
    r = run_chat(s, FakeExtractor([o]), "chat-1", jev=Judge())
    assert r["failed"] and not r["saved"]


def test_tool_results_are_extracted(tmp_path):
    _, s, path = make_store(tmp_path, [("user", "运行训练")])
    with path.open("a") as f:
        f.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "c1",
                        "output": "Run R17: F1=0.91; report=/tmp/r17.json",
                    },
                }
            )
            + "\n"
        )
    s.ingest(path)

    class Extract:
        def __call__(self, b, p):
            assert any(e["role"] == "tool" and "F1=0.91" in e["text"] for e in b)
            return {
                "schema_version": VERSION,
                "nodes": [],
                "relations": [],
                "unresolved": [],
                "coverage": {"entry_ids": [e["id"] for e in b], "outcome": "no_durable_item"},
            }

    r = run_chat(s, Extract(), "chat-1", jev=Judge())
    assert not r["failed"] and r["model_calls"] == 1


def test_rollback_graph_and_cursor_together(tmp_path):
    _, s, _ = make_store(tmp_path, [("user", "训练 A 得到模型 B")])
    o = output(entries(s))
    from sessiongraph.graph_v2 import setup

    setup(s)
    s.db.execute(
        "CREATE TRIGGER fail_cursor BEFORE INSERT ON sg2_cursors BEGIN SELECT RAISE(ABORT,'forced'); END"
    )
    import sqlite3

    failed = run_chat(s, FakeExtractor([o]), "chat-1", jev=Judge())
    assert failed["failure_codes"] == ["graph_database"]
    # Validated nodes have their own commit. Relations and coverage still roll back together.
    assert s.db.execute("select count(*) from sg2_nodes").fetchone()[0] == 2
    assert s.db.execute("select count(*) from sg2_relations").fetchone()[0] == 0
    assert s.db.execute("select count(*) from sg2_cursors").fetchone()[0] == 0
    s.db.execute("DROP TRIGGER fail_cursor")
    s.db.commit()
    r = run_chat(s, FakeExtractor([]), "chat-1", jev=Judge())
    assert len(r["saved"]) == 0 and len(r["saved_relations"]) == 1
    assert r["model_calls"] == 0  # both extraction and judgments survived the commit failure


def test_legacy_dispatch_unmodified_unless_enabled(tmp_path):
    _, s, _ = make_store(tmp_path, [("user", "训练 A 得到模型 B")])
    s.config["graph_v2"] = {"enabled": True}
    # Zero relations require no credential; useful nodes still write.
    o = output(entries(s))
    o["relations"] = []
    from sessiongraph.memories import run_chat as dispatch

    assert len(dispatch(s, FakeExtractor([o]), "chat-1")["saved"]) == 2


def test_cross_chat_same_identity_adds_evidence(tmp_path):
    root, s, _ = make_store(tmp_path, [("user", "项目 P 的训练 R17 得到模型 B")])
    o = output(entries(s))
    o["nodes"][0]["identity"] = [{"namespace": "run_id", "value": "R17", "scope": "项目 P"}]
    run_chat(s, FakeExtractor([o]), "chat-1", jev=Judge())
    from test_mvp import write_session

    path = write_session(
        root / "second.jsonl", root, "chat-2", [("user", "项目 P 的训练 R17 的输出是模型 B")]
    )
    s.ingest(path)
    b = [dict(r) for r in s.db.execute("select * from entries where session_id='chat-2'")]
    o2 = output(b)
    o2["nodes"][0]["identity"] = o["nodes"][0]["identity"]
    r = run_chat(s, ContextExtractor([o2]), "chat-2", jev=Judge(identity="same"))
    assert not r["failed"] and not r["saved"] and not r["saved_relations"]
    assert s.db.execute("select count(*) from sg2_evidence").fetchone()[0] == 4


def test_same_title_different_run_not_merged(tmp_path):
    root, s, _ = make_store(tmp_path, [("user", "项目 P 的训练 R17 得到模型 B")])
    o = output(entries(s))
    o["nodes"][0]["identity"] = [{"namespace": "run_id", "value": "R17", "scope": "项目 P"}]
    run_chat(s, FakeExtractor([o]), "chat-1", jev=Judge())
    from test_mvp import write_session

    s.ingest(
        write_session(
            root / "second.jsonl", root, "chat-2", [("user", "项目 P 的训练 R18 得到模型 B")]
        )
    )
    b = [dict(r) for r in s.db.execute("select * from entries where session_id='chat-2'")]
    o2 = output(b)
    o2["nodes"][0]["identity"] = [{"namespace": "run_id", "value": "R18", "scope": "项目 P"}]
    r = run_chat(s, ContextExtractor([o2]), "chat-2", jev=Judge(identity="same"))
    assert len(r["saved"]) == 1
    assert s.db.execute("select count(*) from sg2_nodes where type='Execution'").fetchone()[0] == 2


def test_job_keeps_both_provider_costs_after_recovery(tmp_path):
    _, s, _ = make_store(tmp_path, [("user", "训练 A 得到模型 B")])
    run_chat(s, FakeExtractor([output(entries(s))]), "chat-1", jev=Missing())
    run_chat(s, FakeExtractor([]), "chat-1", jev=Judge())
    stats = json.loads(s.db.execute("select stats_json from sg2_jobs").fetchone()[0])
    assert stats["deepseek_calls"] == 1 and stats["jev_calls"] == 1
    assert {u["provider"] for u in stats["usage"]} == {"deepseek", "jev"}


def test_graph_reads_never_invoke_a_judge(tmp_path, monkeypatch):
    _, s, _ = make_store(tmp_path, [("user", "训练 A 得到模型 B")])
    run_chat(s, FakeExtractor([output(entries(s))]), "chat-1", jev=Judge())
    from sessiongraph.jev import JevClient

    monkeypatch.setattr(JevClient, "evaluate", lambda *a: pytest.fail("model called on read"))
    from sessiongraph.work_map import work_map
    from sessiongraph.views import memory_detail

    graph = work_map(s)
    assert graph["stats"]["semantic_relations"] == 1 and graph["graph_v2"]["nodes"] == 2
    node = next(n for n in graph["nodes"] if n.get("object_type"))
    assert memory_detail(s, node["id"])["graph_v2"]["assertions"]
    assert s.recall("模型")["model_calls"] == 0


def test_all_context_coverage_is_required(tmp_path):
    _, s, _ = make_store(tmp_path, [("user", "训练 A 得到模型 B")])
    b = entries(s)
    o = output(b)
    o["coverage"]["entry_ids"] = []
    with pytest.raises(ValueError):
        validate(o, b, [])


def test_schema_prompt_is_application_owned(tmp_path):
    from sessiongraph.graph_contract import SCHEMA

    p = policy([])
    assert p["json_schema"]["additionalProperties"] is False
    assert p["json_schema"]["properties"]["nodes"]["items"]["properties"]["id"]["enum"] == [f"n{i}" for i in range(1,13)]
    assert "relations" in p["json_schema"]["required"]


def test_sensitive_transcript_not_sent_to_model(tmp_path):
    _, s, _ = make_store(
        tmp_path, [("user", "api_key=sk-abcdefghijklmnopqrstuvwxyz；普通工作记录")]
    )

    class Extract:
        def __call__(self, b, p):
            assert "sk-abcdefghijklmnopqrstuvwxyz" not in str(b)
            return {
                "schema_version": VERSION,
                "nodes": [],
                "relations": [],
                "unresolved": [],
                "coverage": {"entry_ids": [e["id"] for e in b], "outcome": "no_durable_item"},
            }

    assert not run_chat(s, Extract(), "chat-1", jev=Judge())["failed"]


def test_cross_chat_scoped_recall_after_merge(tmp_path):
    root, s, _ = make_store(tmp_path, [("user", "项目 P 的训练 R17 得到模型 B")])
    o = output(entries(s))
    o["nodes"][0]["identity"] = [{"namespace": "run_id", "value": "R17", "scope": "项目 P"}]
    run_chat(s, FakeExtractor([o]), "chat-1", jev=Judge())
    from test_mvp import write_session

    s.ingest(
        write_session(root / "c2.jsonl", root, "chat-2", [("user", "项目 P 的训练 R17 得到模型 B")])
    )
    b = [dict(r) for r in s.db.execute("select * from entries where session_id='chat-2'")]
    o2 = output(b)
    o2["nodes"][0]["identity"] = o["nodes"][0]["identity"]
    run_chat(s, ContextExtractor([o2]), "chat-2", jev=Judge(identity="same"))
    assert len(s.resume(chat_id="chat-2")["memories"]) == 2
    assert s.recall("模型", chat_id="chat-2")["memories"]


def test_unrelated_same_title_is_pending_not_destroyed(tmp_path):
    root, s, _ = make_store(tmp_path, [("user", "训练 A 得到模型 B")])
    run_chat(s, FakeExtractor([output(entries(s))]), "chat-1", jev=Judge())
    from test_mvp import write_session

    s.ingest(
        write_session(root / "c2.jsonl", root, "chat-2", [("user", "另一次训练 A 得到模型 B")])
    )
    b = [dict(r) for r in s.db.execute("select * from entries where session_id='chat-2'")]
    r = run_chat(
        s, ContextExtractor([output(b)]), "chat-2", jev=Judge(identity="insufficient_evidence")
    )
    assert not r["saved"] and diagnostics(s)["unresolved"]["identity_uncertain"] == 2
    assert s.db.execute("select count(*) from sg2_nodes").fetchone()[0] == 2


def test_batch_budget_resumes_without_repeating_extraction(tmp_path):
    _, s, _ = make_store(tmp_path, [("assistant", "A" * 60), ("assistant", "B" * 60)])
    s.config["graph_v2"] = {"max_batches": 1}

    class Empty:
        calls = 0

        def __call__(self, batch, contract):
            self.calls += 1
            return {
                "schema_version": VERSION,
                "nodes": [],
                "relations": [],
                "unresolved": [],
                "coverage": {"entry_ids": [e["id"] for e in batch], "outcome": "no_durable_item"},
            }

    ex = Empty()
    first = run_chat(s, ex, "chat-1", max_chars=60, jev=Judge())
    assert not first["failed"] and first["graph_v2"]["budget_exhausted"]
    second = run_chat(s, ex, "chat-1", max_chars=60, jev=Judge())
    assert not second["failed"] and not second["graph_v2"]["budget_exhausted"]
    assert ex.calls == 2
    assert run_chat(s, ex, "chat-1", jev=Judge())["model_calls"] == 0


def test_relation_requires_entire_judged_evidence_group(tmp_path):
    _, s, _ = make_store(tmp_path, [("assistant", "训练 A 得到模型 B"), ("user", "我确认这个结果")])
    b = entries(s)
    out = output(b)
    out["relations"][0]["evidence"].append({"entry_id": b[1]["id"], "quote": b[1]["text"]})
    result = run_chat(s, FakeExtractor([out]), "chat-1", jev=Judge())
    assert not result["failed"] and len(read_relations(s)) == 1
    s.db.execute("UPDATE entries SET text='source changed' WHERE id=?", (b[1]["id"],))
    s.db.commit()
    assert read_relations(s) == []


def test_relation_hides_archived_endpoint(tmp_path):
    _, s, _ = make_store(tmp_path, [("assistant", "训练 A 得到模型 B")])
    r = run_chat(s, FakeExtractor([output(entries(s))]), "chat-1", jev=Judge())
    s.db.execute("UPDATE work_memory SET status='archived' WHERE id=?", (r["saved"][0],))
    s.db.commit()
    assert read_relations(s) == []


def test_pending_relation_closes_only_after_new_supported_evidence(tmp_path):
    root, s, _ = make_store(tmp_path, [("user", "训练 A 得到模型 B")])
    original = entries(s)
    first = output(original)
    run_chat(
        s,
        FakeExtractor([first]),
        "chat-1",
        jev=Judge(support="insufficient_evidence", mode="unclear"),
    )
    assert diagnostics(s)["unresolved"] and not read_relations(s)
    from test_mvp import write_session

    s.ingest(
        write_session(
            root / "next.jsonl", root, "chat-2", [("assistant", "确认训练 A 已产出模型 B")]
        )
    )
    new = [dict(r) for r in s.db.execute("SELECT * FROM entries WHERE session_id='chat-2'")]
    second = output(original)
    second["relations"][0]["evidence"].append({"entry_id": new[0]["id"], "quote": new[0]["text"]})
    result = run_chat(s, ContextExtractor([second]), "chat-2", jev=Judge(identity="same"))
    assert not result["failed"] and read_relations(s)
    assert diagnostics(s)["unresolved"] == {}
    assert s.db.execute("SELECT revision FROM sg2_unresolved").fetchone()[0]


def test_run_identity_is_case_sensitive_and_claim_url_is_not_unique():
    from sessiongraph.graph_v2 import _conflict, _identity_keys

    a = {"type": "Execution", "identity": [{"namespace": "run_id", "scope": "P", "value": "runA"}]}
    b = {"type": "Execution", "identity": [{"namespace": "run_id", "scope": "P", "value": "runa"}]}
    assert _conflict(a, b) and _identity_keys(a).isdisjoint(_identity_keys(b))
    assert not _identity_keys(
        {
            "type": "Claim",
            "identity": [{"namespace": "url", "scope": "P", "value": "https://example.org/paper"}],
        }
    )


def test_revision_tracks_graph_only_changes(tmp_path):
    from sessiongraph.auto_update import revision

    _, s, _ = make_store(tmp_path, [("assistant", "训练 A 得到模型 B")])
    run_chat(s, FakeExtractor([output(entries(s))]), "chat-1", jev=Judge())
    before = revision(s.db, s.config["project_id"])
    s.db.execute("UPDATE sg2_relations SET scope='new scope'")
    s.db.commit()
    assert revision(s.db, s.config["project_id"]) != before


def test_compact_prompt_keeps_contract_constraints_without_mutation():
    from sessiongraph.graph_contract import SCHEMA, prompt_policy
    original = policy([])
    before = json.dumps(original, sort_keys=True)
    compact = prompt_policy(original)
    assert json.dumps(original, sort_keys=True) == before
    assert "json_schema" not in compact
    assert len(json.dumps(compact)) < len(json.dumps(original))
    def verify(spec, description):
        if spec["type"] == "object":
            assert set(description) == set(spec["properties"])
            assert set(spec["required"]) <= set(description)
            assert spec["additionalProperties"] is False
            for key, value in spec["properties"].items(): verify(value, description[key])
        elif spec["type"] == "array":
            assert description["length"] == f"{spec['minItems']}..{spec['maxItems']}"
            verify(spec["items"], description["items"])
        elif "enum" in spec:
            assert description.split("|") == spec["enum"]
        else:
            assert description == f"nonempty string, max {spec['maxLength']} chars"
    verify(original["json_schema"], compact["output_contract"])


def test_identity_quote_can_expand_only_to_literal_bounded_source(tmp_path):
    from sessiongraph.graph_contract import anchor_identity_evidence
    _, store, _ = make_store(tmp_path, [("user", "项目 Alpha 中执行训练 R17，产出模型 B")])
    batch = entries(store)
    candidate = output(batch, quote="执行训练 R17")
    candidate["nodes"][0]["identity"] = [{"namespace":"run_id", "value":"R17", "scope":"项目 Alpha"}]
    before = json.dumps(candidate)
    repaired, count = anchor_identity_evidence(candidate, batch)
    assert count == 1 and json.dumps(candidate) == before
    assert repaired["nodes"][0]["evidence"][0]["quote"] == "项目 Alpha 中执行训练 R17"
    validate(repaired, batch, [])
    candidate["nodes"][0]["identity"][0]["scope"] = "invented scope"
    bad, count = anchor_identity_evidence(candidate, batch)
    assert count == 0
    with pytest.raises(ValueError, match="unanchored_identity"): validate(bad, batch, [])
    candidate["nodes"][0]["identity"][0]["scope"] = "项目 Alpha"
    candidate["nodes"][0]["evidence"][0]["quote"] = "fabricated quote"
    bad, count = anchor_identity_evidence(candidate, batch)
    assert count == 0
    with pytest.raises(ValueError, match="quote_not_located"): validate(bad, batch, [])
    store.close()


def test_run_persists_literal_identity_quote_repair_and_replays_without_models(tmp_path):
    _, store, _ = make_store(tmp_path, [("user", "项目 Alpha 中执行训练 R17，产出模型 B")])
    batch = entries(store)
    candidate = output(batch, quote="执行训练 R17")
    candidate["nodes"][0]["identity"] = [{"namespace":"run_id", "value":"R17", "scope":"项目 Alpha"}]
    result = run_chat(store, FakeExtractor([candidate]), "chat-1", jev=Judge())
    assert not result["failed"] and result["graph_v2"]["identity_evidence_repairs"] == 1
    replay = run_chat(store, FakeExtractor([]), "chat-1", jev=Judge())
    assert replay["model_calls"] == 0
    store.close()


def test_format_only_quotes_restore_original_without_relaxing_evidence(tmp_path):
    from sessiongraph.graph_contract import restore_source_quotes, _format_projection
    original = "**图谱 v2 目前仍未启用**，剩余阻塞是此前 DeepSeek 完整抽取超时。"
    _, store, _ = make_store(tmp_path, [("assistant", original)])
    batch = entries(store)
    candidate = json.loads(json.dumps(output(batch, quote=original.replace("**", ""))))
    restored, count = restore_source_quotes(candidate, batch)
    assert count == 3
    assert restored["nodes"][0]["evidence"][0]["quote"] in original
    validate(restored, batch, [])
    candidate["nodes"][0]["evidence"][0]["quote"] = "图谱 v2 已启用"
    bad, _ = restore_source_quotes(candidate, batch)
    with pytest.raises(ValueError, match="quote_not_located"): validate(bad, batch, [])
    assert _format_projection("2**3**4")[0] == "2**3**4"
    assert _format_projection("/tmp/a b")[0] != _format_projection("/tmp/ab")[0]
    assert _format_projection("读取 `file.txt` 完成")[0] == "读取 file.txt 完成"
    store.close()


def test_format_restoration_rejects_foreign_or_ambiguous_sources(tmp_path):
    from sessiongraph.graph_contract import restore_source_quotes
    _, store, _ = make_store(tmp_path, [("user", "读取 `a` 完成；读取 `a` 完成")])
    candidate = output(entries(store), quote="读取 a 完成")
    restored, count = restore_source_quotes(candidate, entries(store))
    assert count == 0
    candidate["nodes"][0]["evidence"][0]["entry_id"] = "e_foreign"
    restored, _ = restore_source_quotes(candidate, entries(store))
    with pytest.raises(ValueError, match="quote_not_located"): validate(restored, entries(store), [])
    store.close()


def test_evidence_spans_preserve_large_input_and_resolve_exact_quotes():
    from sessiongraph.graph_contract import prepare_evidence_spans, resolve_evidence_spans
    text = "原始 **格式** 与 `文件`。" + "长消息不丢内容" * 1000 + "末尾指标 F1=0.91"
    batch = [{"id":"e_real", "role":"tool", "kind":"tool_result", "text":text}]
    prepared, contract, anchors = prepare_evidence_spans(batch, policy([]))
    spans = prepared[0]["source_spans"]
    assert "".join(s["text"] for s in spans) == text
    assert all(len(a["quote"]) <= 480 for a in anchors.values())
    assert set(contract["json_schema"]["properties"]["nodes"]["items"]["properties"]["evidence"]["items"]["properties"]) == {"anchor_id"}
    candidate = output(batch)
    # Models select IDs; every persisted quote is constructed by code.
    for item in candidate["nodes"] + candidate["relations"]:
        item["evidence"] = [{"anchor_id": spans[-1]["anchor_id"]}]
    restored = resolve_evidence_spans(candidate, anchors)
    validate(restored, batch, [])
    assert "F1=0.91" in restored["nodes"][0]["evidence"][0]["quote"]
    candidate["nodes"][0]["evidence"] = [{"anchor_id":"a_foreign"}]
    with pytest.raises(ValueError, match="unknown_source_anchor"): resolve_evidence_spans(candidate, anchors)


def test_secret_spans_are_not_citable_but_surrounding_text_is_kept():
    from sessiongraph.graph_contract import prepare_evidence_spans
    text = "前面的研究结果。████████后面的文件位置。"
    prepared, _, anchors = prepare_evidence_spans([{"id":"e1","text":text}], policy([]))
    assert "".join(span["text"] for span in prepared[0]["source_spans"]) == text
    assert all("█" not in a["quote"] for a in anchors.values())
    assert len(anchors) == 2


def test_unanchored_identity_is_preserved_pending_without_blocking_valid_nodes(tmp_path):
    _, store, _ = make_store(tmp_path, [("user", "训练 A 得到模型 B")])
    batch = entries(store)
    candidate = output(batch)
    candidate["nodes"][0]["identity"] = [{"namespace": "run_id", "value": "invented@R17", "scope": "made-up scope"}]
    with pytest.raises(ValueError, match="unanchored_identity"):
        validate(candidate, batch, [])
    result = run_chat(store, FakeExtractor([candidate]), "chat-1", jev=Judge())
    assert not result["failed"]
    assert len(result["saved"]) == 1 and not result["saved_relations"]
    rows = store.db.execute("SELECT payload_json,reason FROM sg2_unresolved").fetchall()
    deferred = next(json.loads(row[0]) for row in rows if row[1] == "unanchored_identity")
    assert deferred == candidate["nodes"][0]
    assert any(row[1] == "identity_pending" for row in rows)
    assert store.db.execute("SELECT count(*) FROM sg2_keys").fetchone()[0] == 0
    assert run_chat(store, FakeExtractor([]), "chat-1", jev=Judge())["model_calls"] == 0
    store.close()


def test_legacy_memory_column_order_does_not_corrupt_v2_commit(tmp_path):
    _, store, _ = make_store(tmp_path, [("user", "训练 A 得到模型 B")])
    # Older databases added project_id at the end; new databases put it second.
    # Test a real SQLite column-order difference, not a mocked execute call.
    store.db.execute("DROP INDEX work_memory_project")
    store.db.execute("DROP INDEX work_memory_page")
    store.db.execute("ALTER TABLE work_memory DROP COLUMN project_id")
    store.db.execute("ALTER TABLE work_memory ADD COLUMN project_id TEXT")
    store.db.commit()
    result = run_chat(store, FakeExtractor([output(entries(store))]), "chat-1", jev=Judge())
    assert not result["failed"] and len(result["saved"]) == 2
    assert len(result["saved_relations"]) == 1
    row = store.db.execute("SELECT project_id,chat_id,title,created_at FROM work_memory WHERE id=?", (result["saved"][0],)).fetchone()
    assert row["project_id"] == store.config["project_id"]
    assert row["chat_id"] == "chat-1" and row["title"] == "训练 A"
    assert row["created_at"]
    store.close()


def test_span_evidence_limit_accepts_eight_but_remains_bounded():
    from sessiongraph.graph_contract import MAX_EVIDENCE
    batch = [{"id": "e1", "text": "训练 A 得到模型 B"}]
    candidate = output(batch)
    evidence = candidate["nodes"][0]["evidence"][0]
    candidate["nodes"][0]["evidence"] = [dict(evidence) for _ in range(MAX_EVIDENCE)]
    validate(candidate, batch, [])
    candidate["nodes"][0]["evidence"].append(dict(evidence))
    with pytest.raises(ValueError, match="evidence:array"):
        validate(candidate, batch, [])


def test_dependency_judge_receives_source_evidence_not_generated_descriptions(tmp_path):
    _, store, _ = make_store(tmp_path, [("user", "R17 finished. R18 has not started.")])
    candidate = output(entries(store), title="R17", artifact="R18")
    candidate["nodes"][1]["type"] = "Execution"
    candidate["nodes"][1]["content"] = "invented dependency on R17"
    candidate["relations"][0].update(source="n2", target="n1", kind="DEPENDS_ON")
    class InspectJudge(Judge):
        def evaluate(self, state, questions):
            if "s0" in questions:
                assert "prerequisite" in questions["s0"]["instructions"]
                assert "consecutive run numbers" in questions["s0"]["instructions"]
                assert "content" not in state["relations"][0]["source"]
                assert state["original_context"][0]["context"] == "R17 finished. R18 has not started."
            return super().evaluate(state, questions)
    result = run_chat(store, FakeExtractor([candidate]), "chat-1", jev=InspectJudge(support="insufficient_evidence"))
    assert not result["failed"] and not result["saved_relations"]
    assert diagnostics(store)["unresolved"]["relation_insufficient_evidence"] == 1
    store.close()


def test_wire_missing_fixed_version_recovers_without_changing_claims(tmp_path):
    import copy
    from sessiongraph.graph_contract import prepare_evidence_spans, resolve_evidence_spans
    _, store, _ = make_store(tmp_path, [("user", "训练 A 得到模型 B")])
    batch = entries(store)
    original = output(batch)
    wire = copy.deepcopy(original)
    del wire["schema_version"]
    for item in wire["nodes"] + wire["relations"]:
        item["evidence"] = [{"anchor_id": "a1"}]
    _, _, anchors = prepare_evidence_spans(batch, policy([]))
    decoded = resolve_evidence_spans(wire, anchors)
    assert decoded == original and "schema_version" not in wire
    validate(decoded, batch, [])
    result = run_chat(store, FakeExtractor([decoded, decoded]), "chat-1", jev=Judge())
    assert not result["failed"] and len(result["saved_relations"]) == 1


@pytest.mark.parametrize("defect", ["version", "missing_relations", "foreign_id", "extra"])
def test_wire_metadata_recovery_does_not_accept_invalid_graph(tmp_path, defect):
    from sessiongraph.graph_contract import prepare_evidence_spans, resolve_evidence_spans
    _, store, _ = make_store(tmp_path, [("user", "训练 A 得到模型 B")])
    batch = entries(store)
    candidate = output(batch)
    for item in candidate["nodes"] + candidate["relations"]:
        item["evidence"] = [{"anchor_id": "a1"}]
    del candidate["schema_version"]
    if defect == "version": candidate["schema_version"] = "unknown-version"
    elif defect == "missing_relations": del candidate["relations"]
    elif defect == "foreign_id": candidate["nodes"][0]["id"] = "wmv2_invented"
    else: candidate["private-model-field"] = "not diagnostic text"
    _, _, anchors = prepare_evidence_spans(batch, policy([]))
    decoded = resolve_evidence_spans(candidate, anchors)
    result = run_chat(store, FakeExtractor([decoded, decoded]), "chat-1", jev=Judge())
    assert result["failure_codes"] == ["graph_validation"]
    assert store.db.execute("SELECT count(*) FROM sg2_cursors").fetchone()[0] == 0
    blocked = diagnostics(store)["blocked_jobs"]
    assert len(blocked) == 1
    assert blocked[0]["reason"] == result["graph_v2"]["failure_reason"]
    assert "private-model-field" not in blocked[0]["reason"]
    assert "not diagnostic text" not in blocked[0]["reason"]
    if defect == "missing_relations": assert "missing=relations" in blocked[0]["reason"]
    result = run_chat(store, FakeExtractor([output(batch)]), "chat-1", jev=Judge())
    assert not result["failed"] and diagnostics(store)["blocked_jobs"] == []


def test_diagnostics_separates_budget_deferral_before_limiting_real_failures(tmp_path):
    _, store, _ = make_store(tmp_path, [("user", "训练 A 得到模型 B")])
    result = run_chat(store, FakeExtractor([output(entries(store))]), "chat-1", jev=Judge(), call_budget=1)
    assert result["failure_codes"] == ["graph_call_budget_pending"]
    first = diagnostics(store)
    assert first["blocked_jobs"] == []
    assert len(first["deferred_jobs"]) == 1
    original = dict(store.db.execute("SELECT * FROM sg2_jobs").fetchone())
    # More than the UI limit of deferred jobs must not hide an older real error.
    for i in range(8):
        clone = dict(original, id=f"fixture-{i}", updated_at=f"2026-10-{i+1:02}")
        if i == 0:
            clone.update(error="graph_validation", stats_json=json.dumps({"failure_reason": "invalid_endpoint"}))
        elif i == 1:
            clone.update(state="failed")  # An explicit failure is not reclassified.
        elif i in (2, 3):
            clone.update(state="complete" if i == 2 else "superseded")
        store.db.execute(f"INSERT INTO sg2_jobs ({','.join(clone)}) VALUES ({','.join('?' for _ in clone)})", tuple(clone.values()))
    store.db.commit()
    before = store.db.total_changes
    data = diagnostics(store)
    assert data["model_calls"] == 0 and store.db.total_changes == before
    assert len(data["deferred_jobs"]) == 5
    assert {j["id"] for j in data["blocked_jobs"]} == {"fixture-0", "fixture-1"}
    assert all(j["state"] == "pending" for j in data["deferred_jobs"])
    assert data["jobs"]["complete"] == data["jobs"]["superseded"] == 1
