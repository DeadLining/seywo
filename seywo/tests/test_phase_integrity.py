"""Phase lifecycle acceptance: actual writers, source binding, CAS and history."""

from copy import deepcopy

import pytest

from test_s2 import setup as setup
from test_s3 import goal_change, layout, organize_two_objectives, publish


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
