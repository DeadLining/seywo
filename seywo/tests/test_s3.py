import copy
import json
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from test_s2 import setup as setup, drain, message
from seywo.narrative import Narrative
from seywo.narrative_contract import validate_output
from seywo.model_contract import hydrate
from seywo.reader import make_server
from seywo.store import Store


def layout(request):
    old = [i for i in request["items"] if i["origin"] == "existing"]
    incoming = [i for i in request["items"] if i["origin"] == "incoming"]
    bindings = [{"item_index": row["index"], "target_index": n, "action": "keep", "reason": "fixture same work item"}
                for n, row in enumerate(incoming) if n < len(old)]
    targets = {b["item_index"]: b["target_index"] for b in bindings}
    items = list(dict.fromkeys([i["index"] for i in old] + [targets.get(i["index"], i["index"]) for i in incoming]))
    if request["phases"]:
        return {"bindings": bindings, "placements": [{"item_index": i, "phase_index": 0, "branch_index": None}
                                                     for i in items if i >= len(old)]}
    return {"bindings": bindings, "phases": [{"title": "核对范围", "goal_item_indexes": items[:1]}] if items else [],
            "placements": [{"item_index": i, "phase_index": 0, "branch_index": None} for i in items],
            "overview": {"goal_item_indexes": items[:1], "focus": {"phase_index": 0 if items else None, "branch_index": None}}}


def organize(setup):
    store, engine, options, *_ = setup
    job = engine.prepare("chat", options)["job_id"]
    drain(engine, job)
    service = Narrative(store)
    task = service.prepare(job)
    claim = engine.claim(job, backend="fixture-A")
    assert claim["request"]["phase"] == "organize"
    return store, engine, job, service, task, claim


def organize_two_objectives(setup):
    store, engine, options, path, write = setup
    write([message("先核对范围"), message("暂停范围核对，现在改为验证权限报告")])
    store.import_source(path, "chat", 0)
    job = engine.prepare("chat", options)["job_id"]
    while True:
        claim = engine.claim(job, backend=options.backend)
        if claim["state"] != "request":
            break
        request = claim["request"]
        output = {"items": [{"type": "goal", "title": source["text"][:30], "source_indexes": [source["index"]]}
                            for source in request["sources"] if source["index"] in request["primary_source_indexes"]]} if request["phase"] == "extract" else {"items": []}
        assert engine.submit(claim["attempt_id"], backend=options.backend, output=output)["error_code"] is None
    service = Narrative(store)
    task = service.prepare(job)
    claim = engine.claim(job, backend=options.backend)
    return store, engine, job, service, task, claim


def goal_change(source_index=1):
    return {"kind": "goal_change", "from_phase_index": 0, "from_branch_index": None,
            "to_phase_index": 1, "to_branch_index": None, "reason": "用户明确改变目标",
            "evidence_source_indexes": [source_index]}


def publish(setup, transform=None):
    store, engine, job, service, task, claim = organize(setup)
    output = layout(claim["request"])
    if transform:
        transform(output)
    result = engine.submit(claim["attempt_id"], backend="fixture-A", output=output)
    assert result["error_code"] is None
    return store, engine, job, service, task, claim, service.publish(task["task_id"])


def amend(service, view, entity, identity, fields):
    return service.edit(
        "chat",
        base_version=view["version"],
        override_revision=view["override_revision"],
        actor="fixture-user",
        patch={"entity": entity, "id": identity, "fields": fields},
    )


def test_full_reading_publication_ids_versions_and_no_model_reads(setup):
    store, engine, job, service, task, claim, view = publish(setup)
    assert (
        view["version"] == 1
        and not view["task_completed"]
        and not view["user_confirmed"]
    )
    assert view["state"] == "published_unreviewed"
    assert all(not p["id"].startswith("new:") for p in view["narrative"]["phases"])
    assert view["narrative"]["items"][0]["evidence"]
    assert engine.status(job)["usage"]["call_count"] == 4  # 2 extracts, 1 merge, 1 B
    before = store.db.total_changes
    assert service.read("chat") == view
    assert store.db.total_changes == before
    assert service.publish(task["task_id"]) == view
    assert (
        store.db.execute("SELECT count(*) FROM narrative_versions").fetchone()[0] == 1
    )
    assert engine.status(job)["result"]["candidate_items"]  # B never replaces A output


def test_return_to_mainline_keeps_branch_active_and_records_repeat_visits(setup):
    def branch(out):
        out["branches"] = [{"phase_index": 0, "title": "连接排查", "goal": "确认连接", "resolve_when": "实测成功且核对正文", "state": "active", "outcome_item_indexes": [], "evidence_item_indexes": []}]
        out["placements"][0]["branch_index"] = 0
        out["transitions"] = [{"kind": kind, "from_phase_index": 0, "from_branch_index": None if kind == "enter" else 0,
                               "to_phase_index": 0, "to_branch_index": 0 if kind == "enter" else None,
                               "result_item_indexes": [], "unresolved": [0], "reason": "有依据的进入/返回", "evidence_source_indexes": [0]}
                              for kind in ["enter", "return", "enter", "return"]]
    *_, view = publish(setup, branch)
    assert view["narrative"]["branches"][0]["state"] == "active"
    assert len(view["narrative"]["transitions"]) == 4
    assert view["narrative"]["overview"]["focus"]["branch_id"] is None


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_binding",
        "missing_placement",
        "unknown_phase",
        "invented_id",
        "collision",
    ],
)
def test_invalid_b_candidates_do_not_publish_or_corrupt_a(setup, mutation):
    store, engine, job, service, task, claim = organize(setup)
    out = layout(claim["request"])
    if mutation == "missing_binding":
        out["bindings"] = [{"item_index": 0, "target_index": 0, "action": "keep", "reason": "unknown existing target"}]
    elif mutation == "missing_placement":
        out["placements"][0]["item_index"] = 9999
    elif mutation == "unknown_phase":
        out["placements"][0]["phase_index"] = 9999
    elif mutation == "invented_id":
        out["placements"][0]["item_index"] = "invented-item"
    elif mutation == "drop_unresolved":
        out["overview"]["unresolved"] = []  # copying/full-state replacement is forbidden
    elif mutation == "collision":
        out["phases"][0]["id"] = "new:invented"
    else:
        out["branches"] = [{"phase_index": 0, "title": "排查", "goal": "排查", "resolve_when": "实测", "state": "resolved", "outcome_item_indexes": [], "evidence_item_indexes": []}]
    result = engine.submit(claim["attempt_id"], backend="fixture-A", output=out)
    assert result["error_code"] == "invalid_model_output"
    assert store.db.execute("SELECT count(*) FROM narrative_versions").fetchone()[0] == 0
    assert engine.status(job)["result"]["candidate_items"]
    with pytest.raises(ValueError):
        service.publish(task["task_id"])


@pytest.mark.parametrize("mutation", ["false_resolution", "drop_unresolved"])
def test_tolerated_deltas_publish_a_consistent_snapshot(setup, mutation):
    """Two shapes models produce constantly and the runtime can settle itself:
    a resolved branch without evidence (use the work placed inside it) and an
    overview.unresolved echo (accept only when unchanged)."""
    store, engine, job, service, task, claim = organize(setup)
    out = layout(claim["request"])
    if mutation == "drop_unresolved":
        out["overview"]["unresolved"] = []
    else:
        out["branches"] = [{"phase_index": 0, "title": "排查", "goal": "排查", "resolve_when": "实测",
                            "state": "resolved", "outcome_item_indexes": [], "evidence_item_indexes": []}]
    result = engine.submit(claim["attempt_id"], backend="fixture-A", output=out)
    assert result["error_code"] is None
    view = service.publish(task["task_id"])
    if mutation == "false_resolution":
        # Nothing was placed under it, so an unevidenced "resolved" is downgraded.
        assert view["narrative"]["branches"][0]["state"] == "waiting"
    else:
        assert store.db.execute("SELECT count(*) FROM narrative_versions").fetchone()[0] == 1


def test_manual_edit_history_protection_conflict_and_review_resolution(setup):
    store, engine, job, service, task, claim, view = publish(setup)
    item = view["narrative"]["items"][0]
    changed = amend(
        service, view, "item", item["id"], {"content": "人工修正后的明确范围"}
    )
    assert changed["version"] == 2 and changed["override_revision"] == 1
    assert (
        service.read("chat", version=1)["narrative"]["items"][0]["content"]
        == item["content"]
    )
    # A genuinely new source target can propose a revision; the already-applied
    # immutable A target is no longer resent after a manual edit.
    write = setup[4]
    write([message("用户补充了新的范围"), message("工具结果还没有验证", "assistant")])
    store.import_source(setup[3], "chat", 0)
    job = engine.prepare("chat", setup[2])["job_id"]
    drain(engine, job)
    task2 = service.prepare(job)
    claim2 = engine.claim(job, backend="fixture-A")
    output = layout(claim2["request"])
    output["bindings"][0]["action"] = "propose"
    bad = copy.deepcopy(output)
    bad["bindings"][0]["action"] = "revise"
    with pytest.raises(ValueError, match="manual_item_revision_conflict"):
        validate_output(hydrate(bad, json.loads(store.db.execute("SELECT request_json FROM understanding_tasks WHERE id=?", (claim2["task_id"],)).fetchone()[0])), json.loads(store.db.execute("SELECT request_json FROM understanding_tasks WHERE id=?", (claim2["task_id"],)).fetchone()[0]))
    engine.submit(claim2["attempt_id"], backend="fixture-A", output=output)
    current = service.publish(task2["task_id"])
    assert current["narrative"]["items"][0]["content"] == "人工修正后的明确范围"
    assert current["state"] == "needs_review"
    pending = current["narrative"]["pending_revisions"][0]
    reviewed = amend(
        service, current, "review", pending["id"], {"decision": "keep_current"}
    )
    assert reviewed["narrative"]["pending_revisions"][0]["state"] == "keep_current"
    assert not reviewed["user_confirmed"]
    assert (
        store.db.execute("SELECT count(*) FROM narrative_overrides").fetchone()[0] == 2
    )


def test_manual_change_during_b_refuses_old_publication_and_retires_lease(setup):
    store, engine, job, service, task, claim, view = publish(setup)
    next_task = service.prepare(job)
    running = engine.claim(job, backend="fixture-A")
    phase = view["narrative"]["phases"][0]
    newer = amend(service, view, "phase", phase["id"], {"title": "人工阶段名"})
    replacement = service.prepare(job)
    assert replacement["task_id"] != next_task["task_id"]
    late = engine.submit(
        running["attempt_id"], backend="fixture-A", output=layout(running["request"])
    )
    assert late["attempts"][-1]["state"] == "late"
    assert service.read("chat")["version"] == newer["version"]
    with pytest.raises(ValueError):
        service.publish(next_task["task_id"])
    fresh = engine.claim(job, backend="fixture-A")
    bad = layout(fresh["request"])
    bad["phase_updates"] = [{"index": 0, "title": "模型覆盖人工"}]
    with pytest.raises(ValueError, match="manual_override_conflict"):
        validate_output(hydrate(bad, json.loads(store.db.execute("SELECT request_json FROM understanding_tasks WHERE id=?", (fresh["task_id"],)).fetchone()[0])), json.loads(store.db.execute("SELECT request_json FROM understanding_tasks WHERE id=?", (fresh["task_id"],)).fetchone()[0]))


def test_compare_and_swap_prevents_two_writers_and_old_completed_model_result(setup):
    store, engine, job, service, task, claim, view = publish(setup)
    next_task = service.prepare(job)
    running = engine.claim(job, backend="fixture-A")
    engine.submit(
        running["attempt_id"], backend="fixture-A", output=layout(running["request"])
    )
    changed = amend(
        service, view, "item", view["narrative"]["items"][0]["id"], {"hidden": True}
    )
    with pytest.raises(ValueError, match="version_conflict"):
        service.publish(next_task["task_id"])
    with pytest.raises(ValueError, match="version_conflict"):
        amend(
            service,
            view,
            "overview",
            "overview",
            {"focus": view["narrative"]["overview"]["focus"]},
        )
    assert changed["version"] == 2


def test_hidden_move_manual_item_and_branch_resolution_requirements(setup):
    *_, service, task, claim, view = publish(setup)
    item = view["narrative"]["items"][0]
    hidden = amend(service, view, "item", item["id"], {"hidden": True})
    assert hidden["narrative"]["items"][0]["hidden"]
    deferred = amend(
        service, hidden, "placement", item["id"], {"phase_id": None, "branch_id": None}
    )
    assert deferred["narrative"]["placements"][0]["phase_id"] is None
    manual = amend(
        service,
        deferred,
        "manual_item",
        "new",
        {
            "title": "核对人工问题", "content": "人工补充的问题",
            "scope": "当前工作",
            "type": "question",
            "phase_id": None,
            "branch_id": None,
        },
    )
    extra = manual["narrative"]["items"][-1]
    assert (
        extra["origin"] == "manual"
        and extra["actor"] == "fixture-user"
        and extra["evidence"] == []
    )
    assert not extra["user_confirmed"]


def test_source_change_marks_all_dependent_views_and_blocks_old_candidate(setup):
    store, engine, job, service, task, claim, view = publish(setup)
    next_task = service.prepare(job)
    running = engine.claim(job, backend="fixture-A")
    engine.submit(
        running["attempt_id"], backend="fixture-A", output=layout(running["request"])
    )
    _, _, _, path, write = setup
    write([message("原结论存在反证，需要重新核对")])
    store.import_source(path, "chat", 0)
    latest = service.read("chat")
    assert latest["state"] == "needs_review" and latest["source_changed"]
    assert all(
        i["review_state"] == "needs_review" for i in latest["narrative"]["items"]
    )
    with pytest.raises(ValueError, match="source_changed"):
        service.publish(next_task["task_id"])
    assert service.read("chat", version=1)["source_changed"]
    store.delete_chat("chat")
    assert (
        store.db.execute("SELECT count(*) FROM narrative_versions").fetchone()[0] == 0
    )
    assert store.db.execute("SELECT count(*) FROM narrative_heads").fetchone()[0] == 0
    with pytest.raises(ValueError):
        service.read("chat")


def test_organizer_budget_uses_same_ledger_and_reuses_pending_request(setup):
    store, engine, job, service, task, claim = organize(setup)
    before = engine.status(job)["usage"].copy()
    reused = service.prepare(job)
    assert reused["task_id"] == task["task_id"] and reused["reused"]
    assert engine.status(job)["usage"] == before
    assert "base_version" not in claim["request"] and "input_fingerprint" not in claim["request"]


def test_read_only_v2_open_does_not_migrate_and_schema3_upgrade_rehearsal(tmp_path):
    import sqlite3
    from pathlib import Path
    from seywo.config import load_config
    from seywo.store import DDL, APPLICATION_ID
    from seywo.s2_schema import DDL as S2
    root = tmp_path / "project"
    state = root / ".seywo"
    state.mkdir(parents=True)
    config_path = state / "config.json"
    config_path.write_text(json.dumps({"product": "seywo", "schema_version": 1,
        "project_id": "v2", "project_root": str(root), "database": "index.sqlite3"}))
    config = load_config(config_path)
    with sqlite3.connect(config["_database"]) as db:
        db.executescript(DDL + S2)
        db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        db.execute("PRAGMA user_version=2")
        db.execute("INSERT INTO projects VALUES(?,?)", ("v2", str(root)))
    before = Path(config["_database"]).read_bytes()
    with Store(config, read_only=True) as opened:
        assert opened.status()["schema_version"] == 2
    assert Path(config["_database"]).read_bytes() == before
    with Store(config) as opened:
        assert opened.status()["schema_version"] == 7
        assert opened.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_loopback_ui_read_edit_authentication_and_history(setup):
    store, engine, job, service, task, claim, view = publish(setup)
    from pathlib import Path

    server = make_server(Path(store.config["_path"]), "chat")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        html = urlopen(url).read().decode()
        assert "看清你的工作" in html and "textContent" in html
        token = json.loads(html.split("const token=", 1)[1].split(", chat=", 1)[0])
        current = json.loads(urlopen(url + "/api/read").read())
        assert current["version"] == 1
        body = {
            "base_version": 1,
            "override_revision": 0,
            "actor": "ui-user",
            "patch": {
                "entity": "phase",
                "id": view["narrative"]["phases"][0]["id"],
                "fields": {"title": "页面人工修正"},
            },
        }
        request = Request(
            url + "/api/edit",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as denied:
            urlopen(request)
        assert denied.value.code == 403
        request.add_header("X-Seywo-Token", token)
        saved = json.loads(urlopen(request).read())
        assert (
            saved["version"] == 2
            and saved["narrative"]["phases"][0]["title"] == "页面人工修正"
        )
        with pytest.raises(HTTPError) as stale:
            urlopen(request)
        assert stale.value.code == 409
        historical = json.loads(urlopen(url + "/api/read?version=1").read())
        assert historical["narrative"]["phases"][0]["title"] != "页面人工修正"
        assert not historical["is_current"]
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_organizer_over_capacity_pauses_with_all_candidates_preserved(setup):
    store, engine, options, path, write = setup
    from dataclasses import replace

    options = replace(options, chunk_chars=20000)
    write([message("x" * 13000)])
    store.import_source(path, "chat", 0)
    job = engine.prepare("chat", options)["job_id"]
    before = drain(engine, job)
    assert before["state"] == "ready"
    count = len(before["result"]["candidate_items"])
    task = Narrative(store).prepare(job)
    # A set too large for one request is now sliced inside the turn first; only a
    # single item that still does not fit parks the job.
    assert task["state"] in {"pending", "blocked"}
    blocked = engine.status(job)
    row = store.db.execute(
        "SELECT request_json FROM understanding_tasks WHERE id=?", (task["task_id"],)
    ).fetchone()
    request = json.loads(row[0])
    assert len(request["incoming"]) < count
    assert request["pending_organization_count"] == count - len(request["incoming"])
    if task["state"] == "blocked":
        assert blocked["state"] == "waiting_context"
    assert len(blocked["result"]["candidate_items"]) == count
    assert blocked["usage"] == before["usage"]


def test_identity_binding_keeps_stable_item_and_phase_ids_across_revision(setup):
    store, engine, job, service, task, claim, first = publish(setup)
    next_task = service.prepare(job)
    running = engine.claim(job, backend="fixture-A")
    out = layout(running["request"])
    for binding in out["bindings"]:
        binding["action"] = "revise"
    out["phase_updates"] = [{"index": 0, "title": "改名后仍是同一阶段"}]
    engine.submit(running["attempt_id"], backend="fixture-A", output=out)
    second = service.publish(next_task["task_id"])
    assert [i["id"] for i in second["narrative"]["items"]] == [
        i["id"] for i in first["narrative"]["items"]
    ]
    assert (
        second["narrative"]["phases"][0]["id"] == first["narrative"]["phases"][0]["id"]
    )
    assert (
        second["narrative"]["phases"][0]["title"]
        != first["narrative"]["phases"][0]["title"]
    )


def test_source_correction_and_manual_amendment_do_not_impersonate_approval(setup):
    *_, service, task, claim, first = publish(setup)
    item = first["narrative"]["items"][0]
    second = amend(
        service,
        first,
        "item",
        item["id"],
        {"content": "这是人工改写，不是原文逐字陈述"},
    )
    changed = second["narrative"]["items"][0]
    assert changed["amended_by"] == "fixture-user"
    assert changed["evidence"] == item["evidence"]
    assert not changed["user_confirmed"] and not second["task_completed"]


def test_readback_only_cited_pinned_sources_and_bounded_ranges(setup):
    store, engine, job, service, task, claim, view = publish(setup)
    evidence = view["narrative"]["items"][0]["evidence"][0]
    result = service.source(
        "chat",
        version=1,
        event_id=evidence["event_id"],
        snapshot_id=evidence["snapshot_id"],
        start=evidence["start"],
        end=evidence["end"],
    )
    assert result["text"] == evidence["quote"]
    with pytest.raises(ValueError, match="not_in_published_version"):
        service.source(
            "chat",
            version=1,
            event_id="foreign",
            snapshot_id=evidence["snapshot_id"],
            start=0,
            end=10,
        )
    with pytest.raises(ValueError, match="invalid_reading_range"):
        service.source(
            "chat",
            version=1,
            event_id=evidence["event_id"],
            snapshot_id=evidence["snapshot_id"],
            start=0,
            end=10000,
        )


def test_manual_split_preserves_source_identity_and_old_version(setup):
    store, engine, job, service, task, claim, view = publish(setup)
    original = view["narrative"]["items"][0]
    split = amend(
        service,
        view,
        "item_split",
        original["id"],
        {
            "pieces": [
                {"title": "核对来源", "content": "核对来源", "scope": "来源定位"},
                {"title": "核对范围", "content": "核对范围", "scope": "适用边界"},
            ]
        },
    )
    changes = split["narrative"]["identity_changes"]
    assert changes[-1]["from_ids"] == [original["id"]]
    new_ids = changes[-1]["to_ids"]
    assert len(new_ids) == 2
    assert next(i for i in split["narrative"]["items"] if i["id"] == original["id"])[
        "hidden"
    ]
    for identity in new_ids:
        piece = next(i for i in split["narrative"]["items"] if i["id"] == identity)
        assert piece["origin"] == "manual" and piece["evidence"] == original["evidence"]
        assert not piece["user_confirmed"]
    assert service.read("chat", version=1)["narrative"]["items"][0] == original
    assert original["id"] not in split["narrative"]["overview"]["goal_item_ids"]


def test_manual_phase_merge_retargets_placements_and_keeps_history(setup):
    def two_phases(out):
        out["phases"].append(
            {"title": "验证范围", "goal_item_indexes": [1]}
        )
        out["placements"][-1]["phase_index"] = 1
        out["overview"]["focus"] = {"phase_index": 1, "branch_index": None}
        out["overview"]["goal_item_indexes"] = [out["placements"][-1]["item_index"]]

    store, engine, job, service, task, claim = organize_two_objectives(setup)
    output = layout(claim["request"])
    two_phases(output)
    output["transitions"] = [goal_change()]
    assert engine.submit(claim["attempt_id"], backend="fixture-A", output=output)["error_code"] is None
    view = service.publish(task["task_id"])
    first, second = view["narrative"]["phases"]
    merged = amend(
        service,
        view,
        "phase_merge",
        first["id"],
        {"source_phase_id": second["id"], "title": "核对并验证范围"},
    )
    assert len(merged["narrative"]["phases"]) == 1
    assert merged["narrative"]["phases"][0]["id"] == first["id"]
    assert all(p["phase_id"] == first["id"] for p in merged["narrative"]["placements"])
    assert merged["narrative"]["overview"]["focus"]["phase_id"] == first["id"]
    assert merged["narrative"]["identity_changes"][-1]["from_ids"] == [second["id"]]
    assert len(service.read("chat", version=1)["narrative"]["phases"]) == 2


def test_append_dialogue_waits_for_update_without_invalidating_published_items(setup):
    store, engine, job, service, task, claim, view = publish(setup)
    setup[4]([message("先完成索引"), message("工具结果还没有验证", "assistant"), message("现在检查可视化")])
    store.import_source(setup[3], "chat", 0)
    changes, usage = store.db.total_changes, engine.status(job)["usage"]
    latest = service.read("chat")
    assert latest["source_changed"] and latest["update_pending"]
    assert latest["state"] == "update_pending" and latest["review_count"] == 0
    assert all(i["review_state"] == "unreviewed" for i in latest["narrative"]["items"])
    assert store.db.total_changes == changes and engine.status(job)["usage"] == usage
    assert service.read("chat", version=1)["review_count"] == 0


@pytest.mark.parametrize("extra", [
    {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c", "output": "tool result"}},
    {"type": "response_item", "payload": {"type": "function_call", "name": "read", "arguments": "{}"}},
    {"type": "event_msg", "payload": {"type": "token_count"}},
    {"type": "response_item", "payload": {"type": "reasoning", "summary": []}},
])
def test_mechanical_appends_do_not_request_update_or_review(setup, extra):
    store, engine, job, service, task, claim, view = publish(setup)
    setup[4]([message("先完成索引"), message("工具结果还没有验证", "assistant"), extra])
    store.import_source(setup[3], "chat", 0)
    latest = service.read("chat")
    assert latest["source_changed"]
    assert not latest["update_pending"] and latest["review_count"] == 0
    assert latest["state"] != "needs_review" and latest["state"] != "update_pending"


def test_rewrite_only_marks_affected_items_and_transitive_dependencies(setup):
    store, engine, job, service, task, claim, view = publish(setup)
    doc = json.loads(store.db.execute("SELECT body_json FROM narrative_versions WHERE chat_id='chat' AND version=1").fetchone()[0])
    affected, stable = doc["items"]
    for identity, dependency in [("dependent", affected["id"]), ("transitive", "dependent")]:
        doc["items"].append({**copy.deepcopy(stable), "id": identity, "dependencies": [dependency]})
        doc["placements"].append({**doc["placements"][0], "item_id": identity})
    heads = store.db.execute("SELECT source_heads FROM narrative_versions WHERE chat_id='chat' AND version=1").fetchone()[0]
    with store.db:
        service._write("chat", 2, 0, doc, heads, origin="fixture")
    setup[4]([message("原结论存在反证，需要重新核对"), message("工具结果还没有验证", "assistant")])
    store.import_source(setup[3], "chat", 0)
    latest = service.read("chat")
    items = {i["id"]: i for i in latest["narrative"]["items"]}
    assert latest["state"] == "needs_review" and latest["review_count"] == 3
    assert items[stable["id"]]["review_state"] == "unreviewed"
    assert items[affected["id"]]["review_reason"] == "source_changed"
    assert items["dependent"]["review_reason"] == items["transitive"]["review_reason"] == "dependency_changed"


def test_change_outside_cited_range_does_not_invalidate_item(setup):
    store, engine, job, service, task, claim, view = publish(setup)
    doc = json.loads(store.db.execute("SELECT body_json FROM narrative_versions WHERE chat_id='chat' AND version=1").fetchone()[0])
    ref = doc["items"][0]["evidence"][0]
    ref.update(start=0, end=2, quote="先完")
    heads = store.db.execute("SELECT source_heads FROM narrative_versions WHERE chat_id='chat' AND version=1").fetchone()[0]
    with store.db:
        service._write("chat", 2, 0, doc, heads, origin="fixture")
    setup[4]([message("先完成可视化"), message("工具结果还没有验证", "assistant")])
    store.import_source(setup[3], "chat", 0)
    latest = service.read("chat")
    assert latest["state"] == "update_pending" and latest["review_count"] == 0


def test_removed_cited_event_requires_review(setup):
    store, engine, job, service, task, claim, view = publish(setup)
    setup[4]([message("先完成索引")])
    store.import_source(setup[3], "chat", 0)
    latest = service.read("chat")
    assert latest["state"] == "needs_review" and latest["review_count"] == 1
    assert latest["narrative"]["items"][0]["review_state"] == "unreviewed"


def test_role_change_requires_review_even_when_cited_text_is_identical(setup):
    store, engine, job, service, task, claim, view = publish(setup)
    setup[4]([message("先完成索引", "assistant"), message("工具结果还没有验证", "assistant")])
    store.import_source(setup[3], "chat", 0)
    latest = service.read("chat")
    assert latest["state"] == "needs_review" and latest["review_count"] == 1


def test_unavailable_evidence_requires_review_without_snapshot_change(setup):
    store, engine, job, service, task, claim, view = publish(setup)
    ref = view["narrative"]["items"][0]["evidence"][0]
    with store.db:
        store.db.execute("UPDATE coverage_ranges SET state='unavailable' WHERE fragment_id IN "
                         "(SELECT row_id FROM snapshot_fragments WHERE event_id=? AND snapshot_id=?)",
                         (ref["event_id"], ref["snapshot_id"]))
    latest = service.read("chat")
    assert not latest["source_changed"]
    assert latest["state"] == "needs_review" and latest["review_count"] == 1
