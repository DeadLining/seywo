"""Identity regressions at the compact model/runtime boundary; no provider calls."""
from copy import deepcopy

import pytest

from seywo.compact_model import SCHEMA, instructions, validate
from seywo.compact_pipeline import apply_compact
from seywo.narrative import empty
from test_compact_pipeline import source, turn


def change(operation, title, target=None, *, opened=True, merges=()):
    return {"operation": operation, "title": title, "type": "question",
            "attribution": "user", "turn_indexes": [0], "target_index": target,
            "open": opened, "merge_indexes": list(merges)}


def batch(changes, *, questions=(), anchor=None):
    return {"segments": [{"title": "账单审阅", "turn_indexes": [0],
                          "action": "continue", "anchor_index": anchor}],
            "changes": changes, "focus_segment": 0,
            "current": {"goal_indexes": [], "question_indexes": list(questions),
                        "next_step_indexes": []}}


def materialize(semantic, *, previous=None, catalog=(), task="index-regression"):
    text = "邮件调查仍待查。税额疑问已明确解决。"
    turns = [turn("indexes", text, "", source(99, "user", text, turn="indexes"))]
    checked = validate(semantic, [0], {row["index"] for row in catalog}, require_current=True)
    doc, added, touched = apply_compact(previous or empty(), checked, turns, list(catalog), task)
    return checked, doc, added, touched


@pytest.mark.parametrize("first,first_open", [("update", True), ("resolve", False)])
def test_unbound_first_net_state_does_not_shift_later_virtual_target(first, first_open):
    semantic = batch([change(first, "邮件调查待查", opened=first_open),
                      change("new", "税额疑问待查"),
                      change("resolve", "税额疑问已解决", 1, opened=False)])
    # Start with the old segment shape so this exposes identity corruption
    # independently of adding anchor_index support.
    semantic["segments"][0].pop("anchor_index")
    checked, doc, added, touched = materialize(semantic)
    assert checked["changes"][2]["target_index"] == 1
    assert checked["changes"][0]["operation"] == "new"
    assert [(i["title"], i["status"]) for i in doc["items"]] == [
        ("邮件调查待查", "open" if first_open else "resolved"),
        ("税额疑问已解决", "resolved")]
    assert (added, touched) == (2, 1)


@pytest.mark.parametrize("operation", ["update", "resolve"])
def test_normalized_first_item_has_one_shared_current_and_anchor_identity(operation):
    semantic = batch([change(operation, "邮件调查", opened=operation == "update"),
                      change("new", "税额疑问")], questions=[1], anchor=1)
    checked, doc, _, _ = materialize(semantic)
    assert checked["changes"][0]["operation"] == "new"
    assert checked["segments"][0]["anchor_index"] == 1
    assert doc["overview"]["open_question_ids"] == [doc["items"][1]["id"]]


def test_later_unbound_update_is_rejected_instead_of_creating_or_merging_an_item():
    semantic = batch([change("update", "邮件调查"), change("update", "税额调查")])
    semantic["segments"][0].pop("anchor_index")
    with pytest.raises(ValueError, match="compact_target_required"):
        validate(semantic, [0], set())


def test_resolve_open_true_is_rejected_before_first_item_normalization():
    semantic = batch([change("resolve", "邮件调查", opened=True)])
    semantic["segments"][0].pop("anchor_index")
    with pytest.raises(ValueError, match="compact_resolve_must_close"):
        validate(semantic, [0], set())


def test_change_position_is_not_an_alias_for_a_virtual_item():
    # Change position 1 creates virtual item 5 after an update of historical item 4.
    semantic = batch([change("update", "邮件调查进展", 4), change("new", "税额疑问"),
                      change("resolve", "税额疑问已解决", 1, opened=False)])
    semantic["segments"][0].pop("anchor_index")
    with pytest.raises(ValueError, match="compact_target_required"):
        validate(semantic, [0], {4})


@pytest.mark.parametrize("bad_target", [True, False, -1, 1.0, "1", 3])
def test_target_must_be_an_offered_integer_identity(bad_target):
    semantic = batch([change("new", "邮件调查"),
                      change("resolve", "调查已解决", bad_target, opened=False)])
    semantic["segments"][0].pop("anchor_index")
    with pytest.raises(ValueError, match="compact_target_required"):
        validate(semantic, [0], set())


def test_explicit_same_batch_new_resolve_remains_valid():
    checked, doc, added, touched = materialize(batch([
        change("new", "邮件调查"), change("new", "税额疑问"),
        change("resolve", "税额疑问已解决", 1, opened=False)], questions=[0]))
    assert checked["changes"][2]["target_index"] == 1
    assert [i["status"] for i in doc["items"]] == ["open", "resolved"]
    assert (added, touched) == (2, 1)


def test_history_merge_resolve_and_new_virtual_indexes_keep_their_identity():
    _, previous, _, _ = materialize(batch([
        change("new", "邮件调查甲"), change("new", "邮件调查乙")]), task="history")
    catalog = [{"index": 4, "id": previous["items"][0]["id"]},
               {"index": 9, "id": previous["items"][1]["id"]}]
    checked, doc, added, touched = materialize(batch([
        change("merge", "邮件调查归并", 4, merges=[9]),
        change("new", "税额疑问"),
        change("resolve", "税额疑问已解决", 10, opened=False),
        change("resolve", "邮件调查已解决", 4, opened=False)], anchor=4),
        previous=previous, catalog=catalog)
    assert [c["target_index"] for c in checked["changes"]] == [4, None, 10, 4]
    assert (added, touched) == (1, 3)
    assert [(i["status"], i.get("hidden")) for i in doc["items"]] == [
        ("resolved", False), ("open", True), ("resolved", False)]
    assert doc["identity_changes"][-1]["kind"] == "item_merge"


def test_existing_history_does_not_permit_an_unbound_update():
    with pytest.raises(ValueError, match="compact_target_required"):
        validate(batch([change("update", "邮件调查")]), [0], {4})


@pytest.mark.parametrize("bad_current", [True, -1, 1, "0"])
def test_current_cannot_reference_an_unoffered_or_noninteger_identity(bad_current):
    with pytest.raises(ValueError, match="compact_invalid_current_indexes"):
        validate(batch([change("update", "邮件调查")], questions=[bad_current]), [0], set())


@pytest.mark.parametrize("action", ["continue", "branch", "new_phase", "return"])
@pytest.mark.parametrize("anchor", [None, 0, 4])
def test_anchor_accepts_null_historical_or_new_identity(action, anchor):
    semantic = batch([change("new", "新调查")], anchor=anchor)
    semantic["segments"][0]["action"] = action
    active = {4} if anchor == 4 else set()
    result = validate(semantic, [0], active)
    assert result["segments"][0]["anchor_index"] == anchor


@pytest.mark.parametrize("anchor", [True, False, -1, 1.0, "0", [], {}, 3])
def test_anchor_rejects_bool_wrong_type_and_unknown_identity(anchor):
    with pytest.raises(ValueError, match="compact_invalid_anchor_index"):
        validate(batch([change("new", "新调查")], anchor=anchor), [0], set())


def test_branch_can_anchor_history_without_any_changes():
    semantic = batch([], anchor=4)
    semantic["segments"][0]["action"] = "branch"
    result = validate(semantic, [0], {4})
    assert result["changes"] == []
    assert result["segments"][0]["anchor_index"] == 4


def test_legacy_segments_remain_accepted_without_mutating_the_caller():
    semantic = batch([change("update", "邮件调查")])
    semantic["segments"][0].pop("anchor_index")
    original = deepcopy(semantic)
    result = validate(semantic, [0], set())
    assert result["changes"][0]["operation"] == "new"
    assert semantic == original


def test_schema_requires_nullable_anchor_and_instructions_remain_bounded():
    segment = SCHEMA["properties"]["segments"]["items"]
    assert "anchor_index" in segment["required"]
    assert segment["properties"]["anchor_index"]["type"] == ["integer", "null"]
    assert segment["properties"]["anchor_index"]["minimum"] == 0
    assert "new_phase" in segment["properties"]["anchor_index"]["description"]
    assert len(instructions()) < 1800



def test_instructions_distinguish_independent_investigation_from_ordinary_progress():
    prompt = instructions()
    # Prompt contract only; real-model semantic acceptance is evaluated separately.
    assert "branch 暂时离开主目标开展独立调查" in prompt
    assert "普通子任务或故障进展不自动构成 branch" in prompt
    assert "用户继续主线不证明此前进入过支线" in prompt
    assert len(prompt) < 1800


def test_live_schema_leaves_final_focus_to_ordered_structural_replay():
    assert "focus_segment" not in SCHEMA["properties"]
    assert "focus_segment" not in SCHEMA["required"]


def test_new_output_without_legacy_focus_is_accepted_and_materializes_current():
    semantic = batch([change("new", "邮件调查")], questions=[0])
    semantic.pop("focus_segment")
    checked, doc, added, touched = materialize(semantic)
    assert "focus_segment" not in checked
    assert doc["overview"]["open_question_ids"] == [doc["items"][0]["id"]]
    assert (added, touched) == (1, 0)


@pytest.mark.parametrize("legacy_focus", [None, 0, 999, -1])
def test_legacy_focus_is_only_type_checked_not_interpreted_as_an_index(legacy_focus):
    semantic = batch([change("new", "邮件调查")])
    semantic["focus_segment"] = legacy_focus
    checked = validate(semantic, [0], set(), require_current=True)
    assert checked["changes"][0]["title"] == "邮件调查"


@pytest.mark.parametrize("legacy_focus", [True, False, 0.0, "0", [], {}])
def test_legacy_focus_still_rejects_noninteger_nonnull_values(legacy_focus):
    semantic = batch([change("new", "邮件调查")])
    semantic["focus_segment"] = legacy_focus
    with pytest.raises(ValueError, match="compact_invalid_focus"):
        validate(semantic, [0], set())


@pytest.mark.parametrize("parts", [
    [[1], [0], [2, 3]],
    [[0, 2], [1, 3]],
    [[1, 0], [2, 3]],
    [[0], [2, 1, 3]],
])
def test_segments_cannot_reorder_or_interleave_the_source_turns(parts):
    semantic = batch([])
    semantic["segments"] = [{"title": "调查过程", "action": "continue",
                             "turn_indexes": indexes, "anchor_index": None}
                            for indexes in parts]
    with pytest.raises(ValueError, match="compact_turn_order"):
        validate(semantic, [0, 1, 2, 3], set())


@pytest.mark.parametrize("parts", [[[0, 1, 2, 3]], [[0], [1], [2, 3]], [[0, 1], [2, 3]]])
def test_segments_are_an_ordered_contiguous_partition_of_provided_turns(parts):
    semantic = batch([])
    semantic.pop("focus_segment")
    semantic["segments"] = [{"title": "调查过程", "action": "continue",
                             "turn_indexes": indexes, "anchor_index": None}
                            for indexes in parts]
    assert validate(semantic, [0, 1, 2, 3], set())["segments"] == semantic["segments"]


def test_segment_order_is_relative_to_the_provided_turns_not_numeric_gaps():
    semantic = batch([])
    semantic["segments"] = [{"title": "调查过程", "action": "continue",
                             "turn_indexes": [i], "anchor_index": None} for i in [2, 4, 8]]
    assert len(validate(semantic, [8, 2, 4], set())["segments"]) == 3


def test_live_current_schema_selects_goal_and_ordered_priorities_only():
    current = SCHEMA["properties"]["current"]
    assert set(current["properties"]) == {"goal_index", "priority_indexes"}
    assert set(current["required"]) == {"goal_index", "priority_indexes"}
    assert current["properties"]["goal_index"] == {"type": ["integer", "null"], "minimum": 0}
    assert current["properties"]["priority_indexes"]["maxItems"] == 6
    assert "goal_index" in instructions() and "priority_indexes" in instructions()
    assert "question_indexes" not in instructions()
    assert len(instructions()) < 1800


@pytest.mark.parametrize("goal", [None, 0, 4])
def test_current_identity_shape_preserves_order_duplicates_and_caller(goal):
    semantic = batch([change("new", "上传故障")])
    semantic["current"] = {"goal_index": goal, "priority_indexes": [4, 0, 4, 0]}
    original = deepcopy(semantic)
    checked = validate(semantic, [0], {0, 4}, require_current=True)
    assert checked["current"] == original["current"]
    assert semantic == original


@pytest.mark.parametrize("field,bad", [
    ("goal_index", True), ("goal_index", False), ("goal_index", -1),
    ("goal_index", 1), ("goal_index", 0.0), ("goal_index", "0"),
    ("goal_index", []), ("goal_index", {}),
    ("priority_indexes", [True]), ("priority_indexes", [False]),
    ("priority_indexes", [-1]), ("priority_indexes", [1]),
    ("priority_indexes", [0.0]), ("priority_indexes", ["0"]),
    ("priority_indexes", [[]]), ("priority_indexes", [{}]),
    ("priority_indexes", None), ("priority_indexes", 0),
    ("priority_indexes", [0] * 7),
])
def test_current_identity_shape_rejects_invalid_or_unknown_references(field, bad):
    semantic = batch([change("new", "上传故障")])
    semantic["current"] = {"goal_index": None, "priority_indexes": []}
    semantic["current"][field] = bad
    with pytest.raises(ValueError, match="compact_invalid_current_indexes"):
        validate(semantic, [0], set(), require_current=True)


@pytest.mark.parametrize("current", [
    {}, {"goal_index": None}, {"priority_indexes": []},
    {"goal_index": None, "priority_indexes": [], "question_indexes": []},
    {"goal_index": None, "goal_indexes": [], "question_indexes": [], "next_step_indexes": []},
])
def test_current_shapes_cannot_be_partial_or_mixed(current):
    semantic = batch([])
    semantic["current"] = current
    with pytest.raises(ValueError, match="compact_invalid_current_fields"):
        validate(semantic, [0], set(), require_current=True)


@pytest.mark.parametrize("legacy", [False, True])
def test_both_current_shapes_preserve_same_batch_targets_and_resolved_references(legacy):
    semantic = batch([change("update", "原图故障"), change("new", "缩略图故障"),
                      change("resolve", "缩略图已修复", 1, opened=False)])
    current = ({"goal_indexes": [0], "question_indexes": [1, 0], "next_step_indexes": [1]}
               if legacy else {"goal_index": 0, "priority_indexes": [1, 0, 1]})
    semantic["current"] = current
    checked = validate(semantic, [0], set(), require_current=True)
    assert checked["current"] == current  # Classification and resolved filtering belong to runtime.
    assert checked["changes"][2]["target_index"] == 1
    assert checked["changes"][0]["operation"] == "new"


@pytest.mark.parametrize("kind", ["goal", "question", "decision", "action"])
def test_current_goal_identity_does_not_impose_a_goal_item_type(kind):
    item = change("new", "当前主目标")
    item["type"] = kind
    semantic = batch([item])
    semantic["current"] = {"goal_index": 0, "priority_indexes": []}
    assert validate(semantic, [0], set(), require_current=True)["current"]["goal_index"] == 0


def test_live_action_schema_uses_mainline_without_changing_internal_actions():
    from seywo.compact_model import ACTIONS
    actions = SCHEMA["properties"]["segments"]["items"]["properties"]["action"]["enum"]
    assert actions == ["continue", "new_phase", "branch", "mainline"]
    assert "return" not in actions
    assert ACTIONS == ("continue", "new_phase", "branch", "return")


@pytest.mark.parametrize("legacy_segment", [False, True])
def test_live_mainline_canonicalizes_to_internal_return_without_mutating_caller(legacy_segment):
    semantic = batch([change("new", "仍待调查")])
    semantic["segments"][0]["action"] = "mainline"
    if legacy_segment:
        semantic["segments"][0].pop("anchor_index")
    original = deepcopy(semantic)
    checked = validate(semantic, [0], set(), require_current=True)
    assert checked["segments"][0]["action"] == "return"
    assert semantic == original


def test_prompt_distinguishes_returning_to_branch_from_switching_to_mainline():
    prompt = instructions()
    assert "含任何重入或回到支线" in prompt
    assert "mainline 仅切回主线" in prompt
    assert "branch/mainline" in prompt
    assert "continue/mainline" in prompt
    assert "return" not in prompt
    assert len(prompt) < 1800
