import json

from sessiongraph.graph_v2 import run_chat
from sessiongraph.memory import _record
from sessiongraph.graph_contract import VERSION
from test_graph_v2 import Judge, ContextExtractor
from test_memories import make_store, entries
from test_mvp import write_session
from sessiongraph.source_text import hydrate


def claim(batch, content, scope_token):
    quote = batch[0]["text"]
    return {
        "schema_version": VERSION,
        "nodes": [{
            "id": "n1", "type": "Claim", "title": f"{scope_token} 结论",
            "content": content, "status": "completed", "aliases": [],
            "identity": [], "evidence": [{"entry_id": batch[0]["id"], "quote": quote}],
        }],
        "relations": [], "unresolved": [],
        "coverage": {"entry_ids": [e["id"] for e in batch], "outcome": "candidates_found"},
    }


def test_identical_dated_conclusion_merges_across_chats(tmp_path):
    text = "decoder_x_20261018 留出集准确率为 82.4%。"
    root, store, _ = make_store(tmp_path, [("assistant", text)])
    out = claim(entries(store), text, "decoder_x_20261018")
    first = run_chat(store, ContextExtractor([out]), "chat-1", jev=Judge())
    assert len(first["saved"]) == 1
    node = json.loads(store.db.execute(
        "SELECT identity_json FROM sg2_nodes WHERE id=?", (first["saved"][0],)).fetchone()[0])
    assert len(node) == 1 and node[0]["namespace"] == "claim_conclusion"
    assert node[0]["value"].endswith("|" + text)
    assert node[0]["scope"] == "decoder_x_20261018"

    store.ingest(write_session(root / "second.jsonl", root, "chat-2", [("assistant", text)]))
    batch = [hydrate(store, r) for r in store.db.execute(
        "select * from entries where session_id='chat-2'")]
    second = run_chat(store, ContextExtractor([claim(batch, text, "decoder_x_20261018")]),
                      "chat-2", jev=Judge())
    assert second["saved"] == []
    assert store.db.execute("select count(*) from sg2_nodes where type='Claim'").fetchone()[0] == 1


def test_different_value_or_scope_is_not_merged(tmp_path):
    root, store, _ = make_store(tmp_path, [("assistant", "decoder_x_20261018 留出集准确率为 82.4%。")])
    run_chat(store, ContextExtractor([claim(entries(store), "decoder_x_20261018 留出集准确率为 82.4%。",
                                            "decoder_x_20261018")]), "chat-1", jev=Judge())

    changed = "decoder_x_20261018 留出集准确率为 91.2%。"
    store.ingest(write_session(root / "b.jsonl", root, "chat-2", [("assistant", changed)]))
    batch = [hydrate(store, r) for r in store.db.execute(
        "select * from entries where session_id='chat-2'")]
    assert len(run_chat(store, ContextExtractor([claim(batch, changed, "decoder_x_20261018")]),
                        "chat-2", jev=Judge())["saved"]) == 1

    other = "decoder_y_20261019 留出集准确率为 82.4%。"
    store.ingest(write_session(root / "c.jsonl", root, "chat-3", [("assistant", other)]))
    batch = [hydrate(store, r) for r in store.db.execute(
        "select * from entries where session_id='chat-3'")]
    assert len(run_chat(store, ContextExtractor([claim(batch, other, "decoder_y_20261019")]),
                        "chat-3", jev=Judge())["saved"]) == 1
    assert store.db.execute("select count(*) from sg2_nodes where type='Claim'").fetchone()[0] == 3


def test_shape_prevents_adopted_plan_merging_with_rejected_plan(tmp_path):
    positive = "decoder_x_20261018 下一步采用带保护的有界续训。"
    negative = "decoder_x_20261018 下一步采用带保护的有界续训，但该方案未通过，不采用。"
    root, store, _ = make_store(tmp_path, [("assistant", positive)])
    a = claim(entries(store), positive, "decoder_x_20261018")
    assert len(run_chat(store, ContextExtractor([a]), "chat-1", jev=Judge())["saved"]) == 1
    store.ingest(write_session(root / "n.jsonl", root, "chat-2", [("assistant", negative)]))
    batch = [hydrate(store, r) for r in store.db.execute(
        "select * from entries where session_id='chat-2'")]
    b = claim(batch, negative, "decoder_x_20261018")
    assert len(run_chat(store, ContextExtractor([b]), "chat-2", jev=Judge())["saved"]) == 1
    rows = [json.loads(r[0]) for r in store.db.execute(
        "select identity_json from sg2_nodes where type='Claim'")]
    shapes = {i["value"].split("|", 1)[0] for row in rows for i in row
              if i.get("namespace") == "claim_conclusion"}
    assert {s.split(":", 1)[0] for s in shapes} == {"positive", "negative"}


def test_alphanumeric_quantity_blocks_derived_identity(tmp_path):
    text = "decoder_x_20261018 训练使用 100Hz 控制频率，留出集准确率为 82.4%。"
    _, store, _ = make_store(tmp_path, [("assistant", text)])
    out = claim(entries(store), "decoder_x_20261018 训练使用 200Hz 控制频率，留出集准确率为 82.4%。",
                "decoder_x_20261018")
    run_chat(store, ContextExtractor([out]), "chat-1", jev=Judge())
    row = store.db.execute("select identity_json from sg2_nodes where type='Claim'").fetchone()
    assert json.loads(row[0]) == []


def test_shared_literal_sentence_with_different_wording_needs_judge(tmp_path):
    first_text = "decoder_x_20261018 留出集准确率为 82.4%，仍未达到验收线。"
    second_text = "decoder_x_20261018 留出集准确率为 82.4%，但训练集也改善了。"
    root, store, _ = make_store(tmp_path, [("assistant", first_text)])
    a = claim(entries(store), "decoder_x_20261018 留出集准确率为 82.4%，仍未达到验收线。",
              "decoder_x_20261018")
    assert len(run_chat(store, ContextExtractor([a]), "chat-1", jev=Judge())["saved"]) == 1
    store.ingest(write_session(root / "b.jsonl", root, "chat-2", [("assistant", second_text)]))
    batch = [hydrate(store, r) for r in store.db.execute(
        "select * from entries where session_id='chat-2'")]
    b = claim(batch, "decoder_x_20261018 留出集准确率为 82.4%，但训练集也改善了。",
              "decoder_x_20261018")
    result = run_chat(store, ContextExtractor([b]), "chat-2", jev=Judge(identity="different"))
    assert len(result["saved"]) == 1
    assert store.db.execute("select count(*) from sg2_nodes where type='Claim'").fetchone()[0] == 2


def test_two_dated_experiments_make_scope_ambiguous(tmp_path):
    text = "decoder_x_20261018 与 decoder_y_20261019 结论均为留出集准确率 82.4%。"
    _, store, _ = make_store(tmp_path, [("assistant", text)])
    run_chat(store, ContextExtractor([claim(entries(store), text, "decoder_x_20261018")]),
             "chat-1", jev=Judge())
    row = store.db.execute("select identity_json from sg2_nodes where type='Claim'").fetchone()
    assert json.loads(row[0]) == []


def test_model_authored_conclusion_identity_is_stripped_before_derivation():
    from sessiongraph.conclusion import enrich

    text = "decoder_x_20261018 留出集准确率为 82.4%。"
    forged = {"namespace": "claim_conclusion", "scope": "decoder_x_20261018",
              "value": "positive:result|invented merge key"}
    output = {
        "nodes": [{
            "id": "n1", "type": "Claim", "title": "结论", "content": text,
            "status": "completed", "aliases": [], "identity": [forged],
            "evidence": [{"entry_id": "e1", "quote": text}],
        }],
        "relations": [], "unresolved": [],
    }
    enriched, added, stripped = enrich(output)
    assert stripped == 1 and added == 1
    identities = enriched["nodes"][0]["identity"]
    assert len(identities) == 1
    assert identities[0] != forged
    assert identities[0]["value"].endswith("|" + text)


def test_extended_clause_blocks_derived_identity(tmp_path):
    _, store, _ = make_store(tmp_path, [("assistant", "decoder_x_20261018 留出集准确率为 82.4%。")])
    out = claim(entries(store), "decoder_x_20261018 留出集准确率为 82.4%，但训练集仅 61%。",
                "decoder_x_20261018")
    run_chat(store, ContextExtractor([out]), "chat-1", jev=Judge())
    row = store.db.execute("select identity_json from sg2_nodes where type='Claim'").fetchone()
    assert json.loads(row[0]) == []



def test_cited_paths_are_persisted_and_bounded(tmp_path):
    from test_graph_v2 import output as base_output

    text = ("audit 写入 /gpu3/kongsz/microduck-fly/audit.json 与 "
            "decoder_x_20261018/results.json；JSON 字段 /decision 不是路径。")
    _, store, _ = make_store(tmp_path, [("assistant", text)])
    batch = entries(store)
    out = base_output(batch, title="审计结果", artifact="报告")
    for node in out["nodes"]:
        node["evidence"] = [{"entry_id": batch[0]["id"], "quote": text}]
    for relation in out["relations"]:
        relation["evidence"] = [{"entry_id": batch[0]["id"], "quote": text}]
    result = run_chat(store, ContextExtractor([out]), "chat-1", jev=Judge())
    assert len(result["saved"]) == 2
    for node_id in result["saved"]:
        paths = json.loads(store.db.execute(
            "SELECT paths_json FROM work_memory WHERE id=?", (node_id,)).fetchone()[0])
        assert paths == ["/gpu3/kongsz/microduck-fly/audit.json",
                         "decoder_x_20261018/results.json"]


def test_path_extraction_rejects_bare_and_escaped_names():
    from sessiongraph.evidence_paths import from_text

    text = ("工具输出：\\naudit_progress.json 与 protocol.json；"
            "真实产物 decoder_x_20261018/results.json 和 /tmp/audit.json。")
    assert from_text(text) == ["decoder_x_20261018/results.json", "/tmp/audit.json"]


def test_path_backfill_is_additive_and_previews_without_writes(tmp_path):
    from test_graph_v2 import output as base_output
    from sessiongraph.evidence_paths import backfill

    text = "产物 /gpu3/kongsz/microduck-fly/protocol.json 通过校验"
    _, store, _ = make_store(tmp_path, [("assistant", text)])
    batch = entries(store)
    out = base_output(batch)
    for node in out["nodes"]:
        node["evidence"] = [{"entry_id": batch[0]["id"], "quote": text}]
    for relation in out["relations"]:
        relation["evidence"] = [{"entry_id": batch[0]["id"], "quote": text}]
    saved = run_chat(store, ContextExtractor([out]), "chat-1", jev=Judge())["saved"]
    store.db.execute("UPDATE work_memory SET paths_json='[]' WHERE id IN (?,?)", tuple(saved))
    store.db.commit()
    preview = backfill(store, apply=False)
    assert preview["applied"] is False and preview["filled"] == 2
    assert store.db.execute("SELECT count(*) FROM work_memory WHERE paths_json!='[]'").fetchone()[0] == 0
    applied = backfill(store, apply=True)
    assert applied["applied"] is True and applied["filled"] == 2
    again = backfill(store, apply=False)
    assert again["filled"] == 0


def test_path_backfill_publishes_read_projection(tmp_path):
    from test_graph_v2 import output as base_output
    from sessiongraph.evidence_paths import backfill
    from sessiongraph.read_projection import flush

    text = "产物 /gpu3/kongsz/microduck-fly/protocol.json 通过校验"
    _, store, _ = make_store(tmp_path, [("assistant", text)])
    batch = entries(store)
    out = base_output(batch)
    for node in out["nodes"]:
        node["evidence"] = [{"entry_id": batch[0]["id"], "quote": text}]
    for relation in out["relations"]:
        relation["evidence"] = [{"entry_id": batch[0]["id"], "quote": text}]
    saved = run_chat(store, ContextExtractor([out]), "chat-1", jev=Judge())["saved"]
    store.db.execute("UPDATE work_memory SET paths_json='[]' WHERE id IN (?,?)", tuple(saved))
    store.db.commit()
    flush(store)
    before = json.loads(store.db.execute(
        "SELECT paths_json FROM sg_read_nodes WHERE id=?", (saved[0],)).fetchone()[0])
    assert before == []
    backfill(store, apply=True)
    flush(store)
    after = json.loads(store.db.execute(
        "SELECT paths_json FROM sg_read_nodes WHERE id=?", (saved[0],)).fetchone()[0])
    assert any(item[1] == "/gpu3/kongsz/microduck-fly/protocol.json" for item in after)


def test_read_projection_uses_every_validated_evidence_quote(tmp_path):
    from sessiongraph.read_projection import derive

    text = "产物 /gpu3/kongsz/microduck-fly/protocol.json 通过校验"
    _, store, _ = make_store(tmp_path, [("assistant", text), ("assistant", "另一条审计")])
    batch = entries(store)
    run_chat(store, ContextExtractor([claim(batch, text, "decoder_x_20261018")]),
             "chat-1", jev=Judge())
    node = store.db.execute("select id,source_quote from work_memory").fetchone()
    quote = "产物 /gpu3/kongsz/microduck-fly/protocol.json 通过校验"
    store.db.execute("update work_memory set source_quote=? where id=?", ("另一条审计", node["id"]))
    store.db.execute("update sg2_evidence set quote=? where node=?", (quote, node["id"]))
    store.db.commit()
    projected = derive(store, list(store.db.execute("select * from work_memory where id=?", (node["id"],))))
    paths = projected[node["id"]]["paths"]
    assert any(item[1] == "/gpu3/kongsz/microduck-fly/protocol.json" for item in paths)

def test_read_projection_keeps_evidence_path_only_when_source_is_missing(tmp_path):
    from test_graph_v2 import output as base_output
    from sessiongraph.read_projection import derive

    text = "产物 /gpu3/kongsz/microduck-fly/protocol.json 通过校验"
    _, store, _ = make_store(tmp_path, [("assistant", text)])
    batch = entries(store)
    out = base_output(batch)
    for node in out["nodes"]:
        node["evidence"] = [{"entry_id": batch[0]["id"], "quote": text}]
    for relation in out["relations"]:
        relation["evidence"] = [{"entry_id": batch[0]["id"], "quote": text}]
    node = run_chat(store, ContextExtractor([out]), "chat-1", jev=Judge())["saved"][0]
    # Simulate a historical external source: only the immutable quote remains.
    store.db.execute("update entries set text=''")
    store.db.execute("update work_memory set source_quote='' where id=?", (node,))
    store.db.execute("delete from sg_external_entries")
    store.db.commit()
    row = dict(store.db.execute("select * from work_memory where id=?", (node,)).fetchone())
    projected = derive(store, [row])[node]
    assert projected["valid"] is False
    assert any(item[1] == "/gpu3/kongsz/microduck-fly/protocol.json" for item in projected["paths"])


def test_identity_backfill_writes_only_identity_and_keys(tmp_path):
    from sessiongraph.conclusion import backfill

    text = "decoder_x_20261018 留出集准确率为 82.4%。"
    _, store, _ = make_store(tmp_path, [("assistant", text)])
    run_chat(store, ContextExtractor([claim(entries(store), text, "decoder_x_20261018")]),
             "chat-1", jev=Judge())
    node = store.db.execute("select id from sg2_nodes where type='Claim'").fetchone()[0]
    store.db.execute("update sg2_nodes set identity_json='[]' where id=?", (node,))
    store.db.execute("delete from sg2_keys where node=?", (node,))
    store.db.commit()
    before = dict(store.db.execute(
        "select title,content,status,source_entry_id,source_quote from work_memory where id=?",
        (node,)).fetchone())
    evidence = store.db.execute("select count(*) from sg2_evidence where node=?", (node,)).fetchone()[0]
    preview = backfill(store, apply=False)
    assert preview["derived"] == 1 and preview["written"] == 1
    assert json.loads(store.db.execute(
        "select identity_json from sg2_nodes where id=?", (node,)).fetchone()[0]) == []
    backfill(store, apply=True)
    identity = json.loads(store.db.execute(
        "select identity_json from sg2_nodes where id=?", (node,)).fetchone()[0])
    assert identity[0]["namespace"] == "claim_conclusion"
    assert store.db.execute("select count(*) from sg2_keys where node=?", (node,)).fetchone()[0] == 1
    assert dict(store.db.execute(
        "select title,content,status,source_entry_id,source_quote from work_memory where id=?",
        (node,)).fetchone()) == before
    assert store.db.execute("select count(*) from sg2_evidence where node=?", (node,)).fetchone()[0] == evidence


def test_tiering_groups_derived_conclusion_without_changing_tier():
    from sessiongraph import tiering

    identity = [{"namespace": "claim_conclusion", "value": "positive:result|V", "scope": "s_20261018"}]
    hint = tiering.conclusion_hint(identity, "Claim")
    assert hint and hint.startswith("cc_")
    assert tiering.conclusion_hint(identity, "Artifact") is None
    row = {"type": "Claim", "kind": "finding", "title": "结论", "content": "结论",
           "status": "completed", "identity_json": json.dumps(identity),
           "assertion_json": "{}", "evidence_text": "", "linked": 1}
    tier, reason, hint2 = tiering.classify(row)
    assert hint2 == hint
    # The identity only groups; it never upgrades or downgrades the tier rule.
    assert tier in {"canonical", "episode"}
