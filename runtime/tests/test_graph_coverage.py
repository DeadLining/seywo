from sessiongraph.storage import load_payload
"""Application-owned input receipts; no change to semantic or cursor gates."""
import copy
import json
import pytest
from sessiongraph.extraction import create_extractor
from sessiongraph.graph_contract import policy, prepare_evidence_spans, resolve_evidence_spans, validate, SCHEMA
from sessiongraph.graph_v2 import run_chat, diagnostics
from test_graph_v2 import output, Judge
from test_memories import make_store, entries, FakeExtractor


def wire_output(batch):
    value = output(batch)
    for item in value["nodes"] + value["relations"]:
        item["evidence"] = [{"anchor_id": "a1"}]
    value["coverage"] = {"outcome": "candidates_found"}
    return value


def test_receipt_includes_uncited_and_fully_redacted_entries():
    batch = [{"id":"e_run", "text":"训练 A 得到模型 B"},
             {"id":"e_routine", "text":"好的"}, {"id":"e_secret", "text":"████████"}]
    original_schema = copy.deepcopy(SCHEMA)
    prepared, contract, anchors = prepare_evidence_spans(batch, policy([]))
    assert len(prepared) == 3 and not any(a["entry_id"] == "e_secret" for a in anchors.values())
    assert set(contract["json_schema"]["properties"]["coverage"]["properties"]) == {"outcome"}
    assert "coverage must name EVERY input entry" not in contract["instructions"]
    candidate = wire_output(batch)
    result = resolve_evidence_spans(candidate, anchors, entry_ids=[e["id"] for e in batch])
    validate(result, batch, [])
    assert result["coverage"]["entry_ids"] == [e["id"] for e in batch]
    assert "entry_ids" not in candidate["coverage"] and SCHEMA == original_schema
    # No receipt context: old internal validation still rejects incomplete coverage.
    bad = copy.deepcopy(result); bad["coverage"]["entry_ids"] = ["e_run"]
    with pytest.raises(ValueError, match="incomplete_coverage"): validate(bad, batch, [])


@pytest.mark.parametrize("protocol", ["responses", "chat-completions"])
@pytest.mark.parametrize("strict", [False, True])
def test_real_adapters_recover_old_pending_batch_with_complete_input_receipt(tmp_path, monkeypatch, protocol, strict):
    from sessiongraph import extraction
    _, store, _ = make_store(tmp_path, [("user","训练 A 得到模型 B"), ("user","本次没有其他新增事项")])
    batch = entries(store)
    bad = output(batch); bad["coverage"]["entry_ids"] = [batch[0]["id"]]
    first = run_chat(store, FakeExtractor([bad]), "chat-1", jev=Judge())
    assert first["failure_codes"] == ["graph_validation"]
    assert store.db.execute("SELECT count(*) FROM sg2_cursors").fetchone()[0] == 0
    job_id = store.db.execute("SELECT id FROM sg2_jobs").fetchone()[0]
    sent = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit):
            text = json.dumps(wire_output(batch))
            envelope = ({"status":"completed", "output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":text}]}]}
                        if protocol == "responses" else {"choices":[{"finish_reason":"stop","message":{"content":text}}]})
            return json.dumps(envelope).encode()
    class Opener:
        def open(self, req, **kwargs): sent.append(json.loads(req.data)); return Response()
    monkeypatch.setattr(extraction.request, "build_opener", lambda *args: Opener())
    cfg = store.config
    cfg["provider"].update(protocol=protocol, endpoint="http://127.0.0.1:4317/openai/v1/responses",model="fixture",auth="none")
    for key, value in [("base_url_env","http://127.0.0.1:4317/openai/v1"),("api_key_env","fixture-key"),("model_env","fixture")]:
        monkeypatch.setenv(cfg["provider"][key], value)
    cfg["graph_v2"] = {"enabled":True,"strict_json_schema":strict}
    result = run_chat(store, create_extractor(cfg), "chat-1", pending_only=True, jev=Judge())
    assert not result["failed"] and len(result["saved"]) == 2 and len(result["saved_relations"]) == 1
    row = store.db.execute("SELECT id,state,output_json FROM sg2_jobs").fetchone()
    assert row["id"] == job_id and row["state"] == "complete"
    assert load_payload(store,row["output_json"])["coverage"]["entry_ids"] == [e["id"] for e in batch]
    assert diagnostics(store)["blocked_jobs"] == []
    body = sent[0]
    instruction = body["instructions"] if protocol == "responses" else body["messages"][0]["content"]
    prompt = json.loads(instruction.split("\n",1)[1])
    assert set(prompt["output_contract"]["coverage"]) == {"outcome"}
    if strict:
        schema = body["text"]["format"]["schema"] if protocol == "responses" else body["response_format"]["json_schema"]["schema"]
        assert set(schema["properties"]["coverage"]["properties"]) == {"outcome"}
    replay = run_chat(store, FakeExtractor([]), "chat-1", jev=Judge())
    assert replay["model_calls"] == 0
    store.close()


@pytest.mark.parametrize("defect", ["missing_outcome","missing_coverage","foreign_coverage","partial_legacy","duplicate_legacy","unknown_anchor","fabricated_id","inconsistent_outcome"])
def test_receipt_does_not_accept_bad_evidence_or_semantic_shape(defect):
    batch = [{"id":"e1", "text":"训练 A 得到模型 B"}, {"id":"e2", "text":"没有新增"}]
    _, _, anchors = prepare_evidence_spans(batch, policy([]))
    candidate = wire_output(batch)
    if defect == "missing_outcome": candidate["coverage"] = {}
    elif defect == "missing_coverage": del candidate["coverage"]
    elif defect == "foreign_coverage": candidate["coverage"]["entry_ids"] = ["other-project"]
    elif defect == "partial_legacy": candidate["coverage"]["entry_ids"] = ["e1"]
    elif defect == "duplicate_legacy": candidate["coverage"]["entry_ids"] = ["e1","e1","e2"]
    elif defect == "unknown_anchor": candidate["nodes"][0]["evidence"] = [{"anchor_id":"foreign"}]
    elif defect == "fabricated_id": candidate["nodes"][0]["id"] = "wmv2_invented"
    else: candidate["coverage"]["outcome"] = "no_durable_item"
    with pytest.raises(ValueError):
        result = resolve_evidence_spans(candidate, anchors, entry_ids=[e["id"] for e in batch])
        validate(result, batch, [])


def test_exact_legacy_receipt_still_validates():
    batch = [{"id":"e1", "text":"训练 A 得到模型 B"}]
    _, _, anchors = prepare_evidence_spans(batch, policy([]))
    candidate = wire_output(batch);candidate["coverage"]["entry_ids"] = ["e1"]
    result = resolve_evidence_spans(candidate, anchors, entry_ids=["e1"])
    validate(result, batch, [])
