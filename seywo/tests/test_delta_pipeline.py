import json
from seywo.delta_model import validate, request_payload
from seywo.delta_pipeline import apply_delta
from seywo.narrative import empty


def source(index, role, text, turn="t1"):
    return {"event_id": f"e{index}", "snapshot_id": "s1", "role": role,
            "kind": "message", "channel": "final", "state": "projected",
            "reason": "projected", "turn_id": turn, "text": text,
            "start": 0, "end": len(text)}


def test_delta_contract_has_titles_and_indexes_only():
    output = {"items": [{"title": "确认采用原生轮次", "type": "decision", "attribution": "user", "source_indexes": [0]}],
              "line": {"action": "continue", "title": "主线维护"}, "focus_item": 0,
              "open_item_indexes": [], "resolve_titles": [], "no_work_source_indexes": []}
    assert validate(output, 1)["items"][0]["source_indexes"] == [0]
    payload = request_payload([source(0, "user", "采用原生轮次")], {"phase": "主线", "focus": {}, "open": [], "recent": []})
    assert "event_id" not in json.dumps(payload)
    assert "e0" not in json.dumps(payload)
    assert payload["sources"][0]["text"] == "采用原生轮次"


def test_delta_rejects_quotes_and_out_of_range_indexes():
    output = {"items": [{"title": "标题", "type": "question", "attribution": "user", "source_indexes": [1]}],
              "line": {"action": "continue", "title": "主线"}, "focus_item": None,
              "open_item_indexes": [], "resolve_titles": [], "no_work_source_indexes": []}
    try:
        validate(output, 1)
    except ValueError as exc:
        assert str(exc) == "delta_unknown_source_index"
    else:
        raise AssertionError("out of range index accepted")


def test_apply_delta_program_owns_evidence_and_mainline():
    sources = [source(0, "user", "为什么不用原生对话轮次划分"), source(1, "assistant", "按原生轮次维护主线")]
    output = {"items": [{"title": "采用原生轮次划分", "type": "decision", "attribution": "user", "source_indexes": [0]},
                        {"title": "按轮次维护主线", "type": "action", "attribution": "assistant", "source_indexes": [1]}],
              "line": {"action": "continue", "title": "主线维护"}, "focus_item": 0,
              "open_item_indexes": [], "resolve_titles": [], "no_work_source_indexes": []}
    doc, count = apply_delta(empty(), output, sources, "delta-run:test")
    assert count == 2
    assert len(doc["items"]) == 2 and len(doc["phases"]) == 1
    assert doc["items"][0]["title"] == "采用原生轮次划分"
    assert "quote" not in json.dumps(doc)
    assert doc["items"][0]["evidence"][0]["event_id"] == "e0"
    assert doc["overview"]["goal_item_ids"] == [doc["items"][0]["id"]]


def test_runtime_coverage_adds_missing_primary_user_source():
    sources = [source(0, "user", "为什么必须按原生轮次划分？"), source(1, "assistant", "因为轮次是语义边界")]
    output = {"items": [{"title": "轮次是语义边界", "type": "decision", "attribution": "assistant", "source_indexes": [1]}],
              "line": {"action": "continue", "title": "轮次边界"}, "focus_item": 0,
              "open_item_indexes": [], "resolve_titles": [], "no_work_source_indexes": []}
    doc, count = apply_delta(empty(), output, sources, "delta-run:coverage")
    assert count == 2
    assert any(i["attribution"] == "user_statement" and i["type"] == "question" for i in doc["items"])
