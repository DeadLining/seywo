import json

import pytest

from seywo.compact_model import request_payload, validate
from seywo.compact_pipeline import apply_compact, _batch_turns, _turn_packets
from seywo.narrative import empty


def source(index, role, text, *, turn="t1", kind="message", channel="final"):
    return {
        "event_id": f"event-{index}", "snapshot_id": "snapshot-1", "role": role,
        "kind": kind, "channel": channel, "state": "projected", "reason": "projected",
        "turn_id": turn, "turn_seq": index, "text": text, "safe_text": text,
        "start": 0, "end": len(text), "ordinal": 0, "record_no": index,
    }


def turn(turn_id, user, assistant, *sources):
    return {"turn_id": turn_id, "user": user, "assistant": assistant, "sources": list(sources)}


def output(*, action="continue", operation="new", title="确定Compact主线", item_type="decision",
           attribution="user", target_index=None, open=False, turn_indexes=None):
    indexes = [0] if turn_indexes is None else turn_indexes
    return {
        "segments": [{"title": "Compact主线", "turn_indexes": indexes, "action": action}],
        "changes": [{"operation": operation, "title": title, "type": item_type,
                      "attribution": attribution, "turn_indexes": indexes,
                      "target_index": target_index, "open": open}],
        "focus_segment": 0,
    }


def test_payload_is_compact_and_never_contains_source_identity_or_tool_body():
    payload = request_payload(
        [{"index": 0, "user": "用户目标", "assistant": "助手结论"}],
        {"focus": "主线", "goals": ["目标"], "open": []},
        [{"index": 0, "id": "secret-id", "title": "旧事项", "type": "goal"}],
    )
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "secret-id" not in encoded
    assert "shape" not in payload
    assert payload["turns"] == [{"index": 0, "user": "用户目标", "assistant": "助手结论"}]


def test_tool_records_are_removed_before_turn_assembly():
    rows = [
        source(0, "user", "修复导出", turn="t1"),
        source(1, "assistant", "已修复并验证", turn="t1"),
        source(2, "tool", '{"secret":"must not enter model"}', turn="t1",
               kind="function_call_output", channel="tool"),
    ]
    packets = _turn_packets(rows)
    assert len(packets) == 1
    assert packets[0]["user"] == "修复导出"
    assert packets[0]["assistant"] == "已修复并验证"
    assert "must not enter model" not in json.dumps(packets, ensure_ascii=False)


def test_acknowledgement_turn_does_not_consume_a_semantic_segment():
    rows = [source(0, "user", "好的", turn="t1"), source(1, "assistant", "收到", turn="t1")]
    assert _turn_packets(rows) == []


def test_one_compact_can_cover_multiple_turns_and_create_one_condensed_item():
    turns = [
        turn("t1", "目标是按片段整理对话", "可以设计片段级Compact",
             source(0, "user", "目标是按片段整理对话"), source(1, "assistant", "可以设计片段级Compact")),
        turn("t2", "要求低token并保留定位", "运行时保存来源坐标",
             source(2, "user", "要求低token并保留定位", turn="t2"), source(3, "assistant", "运行时保存来源坐标", turn="t2")),
    ]
    semantic = {
        "segments": [{"title": "片段级Compact", "turn_indexes": [0, 1], "action": "continue"}],
        "changes": [{"operation": "new", "title": "低token片段整理", "type": "goal",
                      "attribution": "user", "turn_indexes": [0, 1], "target_index": None, "open": True}],
        "focus_segment": 0,
    }
    doc, added, touched = apply_compact(empty(), semantic, turns, [], "compact-test")
    assert (added, touched) == (1, 0)
    assert len(doc["items"]) == 1
    assert len(doc["phases"]) == 1
    assert doc["items"][0]["title"] == "低token片段整理"
    assert len(doc["items"][0]["evidence"]) == 2


def test_update_resolves_existing_item_without_creating_duplicate():
    first_turn = turn("t1", "建立一个待解决目标", "记录为开放事项",
                       source(0, "user", "建立一个待解决目标"), source(1, "assistant", "记录为开放事项"))
    doc, _, _ = apply_compact(empty(), output(title="待解决目标", item_type="goal", open=True),
                              [first_turn], [], "first")
    item = doc["items"][0]
    second_turn = turn("t2", "目标已完成", "确认完成",
                       source(2, "user", "目标已完成", turn="t2"), source(3, "assistant", "确认完成", turn="t2"))
    updated, added, touched = apply_compact(
        doc,
        output(operation="resolve", title="待解决目标", item_type="goal", target_index=0, open=False),
        [second_turn],
        [{"index": 0, "id": item["id"], "title": item["title"], "type": item["type"]}],
        "second",
    )
    assert added == 0 and touched == 1
    assert len(updated["items"]) == 1
    assert updated["items"][0]["status"] == "resolved"
    assert len(updated["items"][0]["evidence"]) == 2


def test_chunking_is_bounded_and_preserves_turn_order():
    turns = [turn(str(i), "用户" + ("x" * 1200), "助手" + ("y" * 2200),
                  source(i * 2, "user", "u", turn=str(i)),
                  source(i * 2 + 1, "assistant", "a", turn=str(i))) for i in range(25)]
    batches = _batch_turns(turns)
    assert len(batches) >= 2
    assert [t["turn_id"] for batch in batches for t in batch] == [str(i) for i in range(25)]
    assert all(len(batch) <= 24 for batch in batches)


def test_validate_requires_complete_turn_coverage_and_short_schema():
    valid = output(turn_indexes=[0, 1])
    valid["segments"] = [
        {"title": "第一段", "turn_indexes": [0], "action": "continue"},
        {"title": "第二段", "turn_indexes": [1], "action": "continue"},
    ]
    valid["changes"][0]["turn_indexes"] = [0]
    assert validate(valid, [0, 1], set())["segments"][1]["title"] == "第二段"
    broken = dict(valid)
    broken["segments"] = [valid["segments"][0]]
    with pytest.raises(ValueError, match="compact_turn_coverage"):
        validate(broken, [0, 1], set())


def test_same_compact_can_finalize_new_item_without_duplicate_node():
    turns = [
        turn("t1", "提出一个目标", "记录目标", source(10, "user", "提出一个目标")),
        turn("t2", "目标已经完成", "确认完成", source(11, "user", "目标已经完成", turn="t2")),
    ]
    semantic = {
        "segments": [{"title": "当前主线", "turn_indexes": [0, 1], "action": "continue"}],
        "changes": [
            {"operation": "new", "title": "目标事项", "type": "goal", "attribution": "user",
             "turn_indexes": [0], "target_index": None, "open": True},
            {"operation": "resolve", "title": "目标事项已完成", "type": "goal", "attribution": "user",
             "turn_indexes": [1], "target_index": 0, "open": False},
        ],
        "focus_segment": 0,
    }
    doc, added, touched = apply_compact(empty(), semantic, turns, [], "same-batch-final")
    assert (added, touched) == (1, 1)
    assert len(doc["items"]) == 1
    assert doc["items"][0]["status"] == "resolved"
    assert doc["items"][0]["title"] == "目标事项已完成"


def test_merge_preserves_history_and_evidence_on_surviving_item():
    turns = [
        turn("t1", "事项一", "记录一", source(20, "user", "事项一")),
        turn("t2", "事项二", "记录二", source(21, "user", "事项二", turn="t2")),
    ]
    semantic = {
        "segments": [{"title": "当前主线", "turn_indexes": [0, 1], "action": "continue"}],
        "changes": [
            {"operation": "new", "title": "重复事项一", "type": "goal", "attribution": "user",
             "turn_indexes": [0], "target_index": None, "open": True},
            {"operation": "new", "title": "重复事项二", "type": "goal", "attribution": "user",
             "turn_indexes": [1], "target_index": None, "open": True},
        ],
        "focus_segment": 0,
    }
    doc, _, _ = apply_compact(empty(), semantic, turns, [], "merge-source")
    first, second = doc["items"]
    merge = {
        "segments": [{"title": "当前主线", "turn_indexes": [0], "action": "continue"}],
        "changes": [{"operation": "merge", "title": "合并后的事项", "type": "goal", "attribution": "user",
                      "turn_indexes": [0], "target_index": 0, "open": True,
                      "merge_indexes": [1]}],
        "focus_segment": 0,
    }
    merged, added, touched = apply_compact(
        doc, merge, [turn("t3", "合并事项", "已合并", source(22, "user", "合并事项", turn="t3"))],
        [{"index": 0, "id": first["id"], "title": first["title"], "type": first["type"]},
         {"index": 1, "id": second["id"], "title": second["title"], "type": second["type"]}],
        "merge-result",
    )
    assert (added, touched) == (0, 1)
    assert merged["items"][0]["hidden"] is False
    assert merged["items"][1]["hidden"] is True
    assert len(merged["items"][0]["evidence"]) == 3
    assert merged["identity_changes"][-1]["kind"] == "item_merge"


def test_first_batch_can_record_already_resolved_item_without_target():
    turns = [
        turn("t1", "旧目标已经完成", "确认已完成", source(30, "user", "旧目标已经完成")),
        turn("t2", "开始新的团队权限目标", "记录新目标", source(31, "user", "开始新的团队权限目标", turn="t2")),
    ]
    semantic = {
        "segments": [
            {"title": "旧目标收尾", "turn_indexes": [0], "action": "continue"},
            {"title": "团队权限新目标", "turn_indexes": [1], "action": "new_phase"},
        ],
        "changes": [
            {"operation": "resolve", "title": "旧目标已完成", "type": "goal", "attribution": "user",
             "turn_indexes": [0], "target_index": None, "open": False, "merge_indexes": []},
            {"operation": "new", "title": "团队权限目标", "type": "goal", "attribution": "user",
             "turn_indexes": [1], "target_index": None, "open": True, "merge_indexes": []},
        ],
        "focus_segment": 1,
    }
    # The model boundary allows the first closed item to have no historical target.
    from seywo.compact_model import validate
    validated = validate(semantic, [0, 1], set())
    doc, added, touched = apply_compact(empty(), validated, turns, [], "first-batch-resolve")
    assert (added, touched) == (2, 0)
    assert [item["status"] for item in doc["items"]] == ["resolved", "open"]
    assert len(doc["phases"]) == 2


def test_return_segment_updates_focus_without_a_new_item():
    turns = [
        turn("t1", "主线目标", "记录主线", source(40, "user", "主线目标")),
        turn("t2", "进入支线", "开始排查", source(41, "user", "进入支线", turn="t2")),
        turn("t3", "支线完成", "排查完成", source(42, "user", "支线完成", turn="t3")),
        turn("t4", "回到主线", "继续主线", source(43, "user", "回到主线", turn="t4")),
    ]
    semantic = {
        "segments": [
            {"title": "主线", "turn_indexes": [0], "action": "continue"},
            {"title": "支线", "turn_indexes": [1, 2], "action": "branch"},
            {"title": "回到主线", "turn_indexes": [3], "action": "return"},
        ],
        "changes": [
            {"operation": "new", "title": "主线目标", "type": "goal", "attribution": "user",
             "turn_indexes": [0], "target_index": None, "open": True, "merge_indexes": []},
            {"operation": "new", "title": "支线问题", "type": "observation", "attribution": "user",
             "turn_indexes": [1, 2], "target_index": None, "open": False, "merge_indexes": []},
        ],
        "focus_segment": 2,
    }
    doc, _, _ = apply_compact(empty(), semantic, turns, [], "return-focus")
    assert doc["overview"]["focus"]["branch_id"] is None
    assert doc["branches"][0]["state"] == "resolved"
    assert doc["branches"][0]["outcome_item_ids"]
    assert doc["transitions"][-1]["kind"] == "return"


def test_first_batch_update_without_target_materializes_one_item():
    turns = [turn("t1", "连续核验同一目标", "已验证正常", source(50, "user", "连续核验同一目标"), source(51, "assistant", "已验证正常"))]
    semantic = {
        "segments": [{"title": "连续核验", "turn_indexes": [0], "action": "continue"}],
        "changes": [{"operation": "update", "title": "连续核验正常", "type": "observation", "attribution": "assistant",
                      "turn_indexes": [0], "target_index": None, "open": False, "merge_indexes": []}],
        "focus_segment": 0,
    }
    from seywo.compact_model import validate
    validated = validate(semantic, [0], set())
    doc, added, touched = apply_compact(empty(), validated, turns, [], "first-batch-update")
    assert (added, touched) == (1, 0)
    assert len(doc["items"]) == 1
    assert doc["items"][0]["status"] == "resolved"


def test_completed_assistant_statement_cannot_remain_unresolved():
    turns = [turn("t1", "继续处理", "已实现流式写出，验证通过", source(51, "assistant", "已实现流式写出，验证通过"))]
    semantic = {
        "segments": [{"title": "流式写出", "turn_indexes": [0], "action": "continue"}],
        "changes": [{"operation": "new", "title": "已实现流式写出", "type": "action", "attribution": "assistant",
                      "turn_indexes": [0], "target_index": None, "open": True, "merge_indexes": []}],
        "focus_segment": 0,
    }
    doc, _, _ = apply_compact(empty(), semantic, turns, [], "completed-assistant")
    assert doc["items"][0]["status"] == "resolved"
    assert doc["overview"]["unresolved"] == []


def test_new_with_target_is_canonicalized_to_update_without_duplicate():
    semantic = {
        "segments": [{"title": "当前主线", "turn_indexes": [0, 1], "action": "continue"}],
        "changes": [
            {"operation": "new", "title": "初始事项", "type": "goal", "attribution": "user",
             "turn_indexes": [0], "target_index": None, "open": True, "merge_indexes": []},
            {"operation": "new", "title": "事项已更新", "type": "goal", "attribution": "user",
             "turn_indexes": [1], "target_index": 0, "open": False, "merge_indexes": []},
        ],
        "focus_segment": 0,
    }
    validated = validate(semantic, [0, 1], set())
    assert validated["changes"][1]["operation"] == "update"
    turns = [
        turn("t1", "提出事项", "记录", source(60, "user", "提出事项")),
        turn("t2", "事项完成", "确认", source(61, "user", "事项完成", turn="t2")),
    ]
    doc, added, touched = apply_compact(empty(), validated, turns, [], "new-target-update")
    assert (added, touched) == (1, 1)
    assert len(doc["items"]) == 1
    assert doc["items"][0]["title"] == "事项已更新"
    assert doc["items"][0]["status"] == "resolved"
