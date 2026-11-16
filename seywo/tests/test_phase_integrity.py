"""Phase lifecycle acceptance: actual writers, source binding, CAS and history."""

from copy import deepcopy

import pytest

from test_s2 import setup as setup
from test_s3 import goal_change, layout, organize_two_objectives, publish
from test_delta_pipeline import source
from seywo.delta_pipeline import apply_delta
from seywo.narrative import empty
from seywo.phase_integrity import validate_phase_changes


def delta(title="确认范围", stage="核对范围", action="continue", attribution="user"):
    return {"items": [{"title": title, "type": "goal", "attribution": attribution, "source_indexes": [0]}],
            "line": {"action": action, "title": stage}, "focus_item": 0,
            "open_item_indexes": [], "resolve_titles": [], "no_work_source_indexes": []}


def baseline():
    return apply_delta(empty(), delta(), [source(0, "user", "先核对范围")], "initial")[0]


def test_delta_goal_change_has_own_user_goal_and_keeps_prior_branch_endpoint():
    previous = baseline()
    branch = delta("排查连接", "连接支线", "branch")
    previous, _ = apply_delta(previous, branch, [source(1, "user", "排查连接")], "branch")
    branch_id = previous["overview"]["focus"]["branch_id"]
    doc, n = apply_delta(previous, delta("改为验证报告", "验证报告", "new_phase"),
                         [source(2, "user", "暂停核对，现在改为验证报告")], "next")
    assert n == 1 and len(doc["phases"]) == 2
    transition = doc["transitions"][-1]
    assert transition["from_branch_id"] == branch_id
    assert transition["to_branch_id"] is None
    assert doc["phases"][-1]["goal_item_ids"] == [doc["items"][-1]["id"]]
    validate_phase_changes(previous, doc)


@pytest.mark.parametrize("role,attribution,error", [
    ("assistant", "assistant", "new_phase_requires_user_objective"),
    ("assistant", "user", "delta_user_requires_user_source"),
    ("user", "assistant", "new_phase_requires_user_objective"),
])
def test_assistant_progress_cannot_create_new_phase(role, attribution, error):
    old = baseline()
    snapshot = deepcopy(old)
    with pytest.raises(ValueError, match=error):
        apply_delta(old, delta("复测完成", "同步结果", "new_phase", attribution),
                    [source(1, role, "复测完成")], "invalid")
    assert old == snapshot


def test_adjacent_same_title_requires_specific_goal_name_instead_of_duplicate_stage():
    with pytest.raises(ValueError, match="new_phase_repeats_previous_title"):
        apply_delta(baseline(), delta("继续核对", "核对范围", "new_phase"),
                    [source(1, "user", "继续核对")], "repeated")


@pytest.mark.parametrize("action", ["new_phase", "branch", "return"])
def test_replayed_evidence_cannot_create_structure_or_change_focus(action):
    old = baseline()
    doc, n = apply_delta(old, delta(stage="另一个标题", action=action),
                         [source(0, "user", "先核对范围")], "replay")
    assert n == 0 and doc == old


def test_pure_no_work_turn_does_not_initialize_a_phase():
    output = delta()
    output.update(items=[], focus_item=None, no_work_source_indexes=[0])
    doc, n = apply_delta(empty(), output, [source(0, "user", "谢谢")], "social")
    assert n == 0 and doc["phases"] == []


def new_phase_output(claim):
    out = layout(claim["request"])
    out["phases"].append({"title": "验证权限报告", "goal_item_indexes": [1]})
    out["placements"][1]["phase_index"] = 1
    out["overview"] = {"focus": {"phase_index": 1, "branch_index": None}, "goal_item_indexes": [1]}
    out["transitions"] = [goal_change()]
    return out


@pytest.mark.parametrize("mutation,error", [
    ("empty", "new_phase_requires_work"),
    ("repeat", "new_phase_repeats_previous_title"),
    ("borrow_goal", "new_phase_requires_user_objective"),
    ("no_change", "new_phase_requires_goal_change"),
    ("wrong_evidence", "new_phase_requires_goal_change"),
])
def test_bulk_writer_rejects_invalid_phase_without_publishing_and_can_retry(setup, mutation, error):
    store, engine, job, service, task, claim = organize_two_objectives(setup)
    out = new_phase_output(claim)
    if mutation == "empty":
        out["phases"].append({"title": "空壳阶段", "goal_item_indexes": [0]})
    elif mutation == "repeat":
        out["phases"][1]["title"] = out["phases"][0]["title"]
    elif mutation == "borrow_goal":
        out["phases"][1]["goal_item_indexes"] = [0]
    elif mutation == "no_change":
        out.pop("transitions")
    elif mutation == "wrong_evidence":
        out["transitions"][0]["evidence_source_indexes"] = [0]
    result = engine.submit(claim["attempt_id"], backend="fixture-A", output=out)
    assert result["error_code"] == "invalid_model_output"
    assert store.db.execute("SELECT count(*) FROM narrative_versions").fetchone()[0] == 0
    retry = engine.claim(job, backend="fixture-A")
    assert retry["request"]["retry_feedback"] == error
    assert engine.submit(retry["attempt_id"], backend="fixture-A", output=new_phase_output(retry))["error_code"] is None
    assert len(service.publish(task["task_id"])["narrative"]["phases"]) == 2


def test_publication_defense_also_rejects_a_bypassed_empty_proposal(setup):
    store, _, _, service, _, _, view = publish(setup)
    before = deepcopy(view["narrative"])
    broken = deepcopy(before)
    broken["phases"].append({"id": "bad", "title": "孤立阶段", "goal_item_ids": []})
    with pytest.raises(ValueError, match="new_phase_requires_work"), store.db:
        service._write("chat", view["version"] + 1, view["override_revision"], broken, "[]", origin="model_delta_v1")
    assert service.read("chat")["narrative"] == before


def test_equal_historical_labels_stay_distinct_until_explicit_repair():
    old = baseline()
    doc, _ = apply_delta(old, delta("验证报告", "报告验证", "new_phase"),
                         [source(1, "user", "现在改为验证报告")], "second")
    # A later, distinct objective can use an earlier label; labels are not IDs.
    doc, _ = apply_delta(doc, delta("核对另一租户范围", "核对范围", "new_phase"),
                         [source(2, "user", "现在核对另一租户范围")], "third")
    assert len(doc["phases"]) == 3
    assert doc["phases"][0]["title"] == doc["phases"][2]["title"]
    assert doc["phases"][0]["id"] != doc["phases"][2]["id"]


def test_batch_repair_is_atomic_preserves_nodes_history_and_manual_locks(setup):
    store, engine, job, service, task, claim = organize_two_objectives(setup)
    assert engine.submit(claim["attempt_id"], backend="fixture-A", output=new_phase_output(claim))["error_code"] is None
    view = service.publish(task["task_id"])
    first, second = view["narrative"]["phases"]
    merge = {"entity": "phase_merge", "id": first["id"], "fields": {"source_phase_id": second["id"], "title": "合并核对"}}
    invalid = {"entity": "phase_merge", "id": first["id"], "fields": {"source_phase_id": "missing", "title": "不存在"}}
    args = dict(base_version=view["version"], override_revision=view["override_revision"], actor="phase-repair")
    with pytest.raises(ValueError, match="invalid_phase_merge"):
        service.edit_many("chat", **args, patches=[merge, invalid])
    assert service.read("chat") == view
    assert store.db.execute("SELECT count(*) FROM narrative_overrides").fetchone()[0] == 0
    fixed = service.edit_many("chat", **args, patches=[merge, {"entity": "phase", "id": first["id"], "fields": {"title": "最终核对"}}])
    assert fixed["version"] == view["version"] + 1
    assert fixed["override_revision"] == view["override_revision"] + 2
    assert fixed["narrative"]["items"] == view["narrative"]["items"]
    assert len(fixed["narrative"]["phases"]) == 1
    assert service.read("chat", version=view["version"])["narrative"] == view["narrative"]
    assert next(lock for lock in service._locks("chat") if lock["entity"] == "phase" and lock["field"] == "title")["value"] == "最终核对"
    with pytest.raises(ValueError, match="narrative_version_conflict"):
        service.edit_many("chat", **args, patches=[merge])


def test_one_user_turn_can_supply_two_distinct_items_and_anchor_later_goal():
    output = delta("暂停当前核验", "设计可视化", "new_phase")
    output["items"].append({"title": "设计可视化界面", "type": "goal", "attribution": "user", "source_indexes": [0]})
    output["focus_item"] = 1
    doc, n = apply_delta(baseline(), output, [source(1, "user", "暂停当前核验，现在改为设计可视化")], "multi-item")
    assert n == 2
    assert doc["items"][-1]["title"] == "设计可视化界面"
    assert doc["items"][-1]["id"] in doc["phases"][-1]["goal_item_ids"]


def test_partial_replay_does_not_collide_with_runtime_coverage_identity():
    output = delta()
    output["items"].append({"title": "新的助手说明", "type": "observation", "attribution": "assistant", "source_indexes": [1]})
    sources = [source(0, "user", "先核对范围"), source(1, "assistant", "新的助手说明"), source(2, "user", "补充验收要求")]
    doc, n = apply_delta(baseline(), output, sources, "partial-replay")
    assert n == 2
    assert len(doc["items"]) == len({i["id"] for i in doc["items"]})
    assert doc["items"][-1]["origin"] == "runtime_coverage"


def test_invalid_phase_keeps_delta_cursor_then_recovers_without_replaying_published_turns(tmp_path, monkeypatch):
    import json
    from seywo import inbox, maintain
    from seywo.config import initialize, load_config
    from seywo.store import Store
    from seywo.narrative import Narrative
    from test_maintain import source as transcript
    initialize(tmp_path / 'project')
    path = tmp_path / 'project/.seywo/config.json'
    data = json.loads(path.read_text())
    data['hook'] = {'enabled': True, 'pipeline': 'seywo-delta-v1'}
    path.write_text(json.dumps(data))
    config = load_config(path)
    class Transport:
        model = 'fixture-model'
        repaired = False
        def __init__(self, *args, **kwargs):
            pass
        def complete_delta(self, *, payload, **kwargs):
            title = payload['sources'][0]['text']
            change = title == '现在验证权限'
            stage = '权限验证' if change and self.repaired else '核对范围'
            return {'output': delta(title, stage, 'new_phase' if change else 'continue'),
                    'error': None, 'usage': {'input_tokens': 100, 'output_tokens': 40}}
    monkeypatch.setattr(maintain, 'read_provider', lambda config: {'endpoint': 'http://127.0.0.1:1/responses', 'model': 'fixture-model', 'timeout': 1, 'max_output_tokens': 900, '_api_key': 'fixture'})
    monkeypatch.setattr(maintain, 'Transport', Transport)
    ingress = transcript(tmp_path, 'cursor-chat', [('先核对范围', '进行中'), ('现在验证权限', '收到')])
    inbox.record(config, 'cursor-chat', 'PreCompact', str(ingress))
    result = maintain.run_chat(config, 'cursor-chat')
    assert result['state'] == 'partial'
    assert result['error_code'] == 'new_phase_repeats_previous_title'
    assert result['calls'] == 2
    with Store(config, read_only=True) as store:
        before = Narrative(store).read('cursor-chat')
        progress = dict(store.db.execute('SELECT * FROM delta_progress').fetchone())
        assert progress['version'] == 1
    Transport.repaired = True
    result = maintain.run_chat(config, 'cursor-chat')
    assert result['state'] == 'done' and result['calls'] == 1
    with Store(config, read_only=True) as store:
        after = Narrative(store).read('cursor-chat')
    assert after['version'] == 2 and len(after['narrative']['phases']) == 2
    assert after['narrative']['items'][:len(before['narrative']['items'])] == before['narrative']['items']
