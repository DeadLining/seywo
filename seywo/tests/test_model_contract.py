"""Acceptance for the indexed model protocol, using the production outbox API."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from test_s2 import setup as setup, drain, message, reply
from test_s3 import layout, publish
from seywo.backends import FunctionBackend, JevBackend
from seywo.model_contract import (POLICY, UNIT_CHARS, compile_request, freeze, hydrate,
                                  measured_payload, model_request, responses_body)
from seywo.narrative import Narrative
from seywo.provider import measure_request
from seywo.store import digest, encode
from seywo.understanding import Limits

SENTINEL = "TOOL_BODY_MUST_NEVER_ENTER_MODEL_835e"


def stored(store, claim):
    return json.loads(store.db.execute("SELECT request_json FROM understanding_tasks WHERE id=?", (claim["task_id"],)).fetchone()[0])


@pytest.mark.parametrize("payload", [
    {"type": "function_call_output", "output": SENTINEL * 3000},
    {"type": "custom_tool_call_output", "output": {"nested": [{"text": SENTINEL}]}},
    {"type": "tool_result", "output": [SENTINEL, {"k": SENTINEL}]},
    {"type": "tool_response", "output": {"text": SENTINEL}},
    {"type": "message", "role": "tool", "content": [{"type": "text", "text": SENTINEL}]},
    {"type": "function_call", "name": "run", "arguments": json.dumps({"cmd": SENTINEL})},
    {"type": "custom_tool_call", "name": "run", "input": SENTINEL},
    {"type": "function_call_output", "role": "user", "output": SENTINEL},
])
def test_all_tool_forms_excluded_from_primary_context_merge_readback_b_and_jev(setup, payload):
    store, engine, options, path, write = setup
    write([message("先核对迁移方案。" * 30), {"type": "response_item", "payload": payload}, message("助手报告完成，仍需用户核对。", "assistant")])
    store.import_source(path, "chat", 0)
    options = replace(options, context_chars=800, chunk_chars=200, overlap_chars=0)
    job = engine.prepare("chat", options)["job_id"]
    requests = []
    # Exercise a real indexed adjacent read on the first source, once.
    first = engine.claim(job, backend=options.backend)
    requests.append(first["request"])
    result = engine.submit(first["attempt_id"], backend=options.backend,
                          output=reply(first["request"], reads=[{"source_index": 0, "direction": "after", "reason": "核对相邻段落"}]))
    assert result["error_code"] is None
    while True:
        claim = engine.claim(job, backend=options.backend)
        if claim["state"] != "request":
            break
        requests.append(claim["request"])
        result = engine.submit(claim["attempt_id"], backend=options.backend, output=reply(claim["request"]))
        assert result["error_code"] is None
    assert result["state"] == "ready"
    Narrative(store).prepare(job)
    claim = engine.claim(job, backend=options.backend)
    requests.append(claim["request"])
    engine.submit(claim["attempt_id"], backend=options.backend, output=layout(claim["request"]))
    selected = result["result"]["candidate_items"][0]
    engine.queue_judgment(job, kind="approval", question="是否存在无条件执行批准？", item_ids=[selected["id"]], backend="jev:pin", model="pin")
    judge = engine.claim(job, backend="jev:pin")
    requests.append(judge["request"])
    captures = []
    def evaluate(state, questions):
        captures.append({"state": state, "questions": questions})
        return {"model": "pin", "answers": {"q": {"choice": "unclear", "probabilities": {"confirms": 0, "conditional": 0, "proposes": 0, "unclear": 1}}}}
    adapter = JevBackend("pin", evaluate)
    status = engine.submit(judge["attempt_id"], backend=adapter.identity, **adapter.complete(judge["request"]))
    assert status["error_code"] is None
    assert all(SENTINEL not in encode(r) for r in [*requests, *captures])
    assert {r["phase"] for r in requests} == {"extract", "merge", "organize", "judgment"}
    assert any(s["purpose"] == "readback" for r in requests for s in r["sources"])
    assert status["excluded_policy"]
    assert store.db.execute("SELECT count(*) FROM semantic_ranges r JOIN source_fragments s ON s.row_id=r.fragment_id WHERE s.role='tool' OR s.kind!='message'").fetchone()[0] == 0
    # Human source lookup/storage is preserved independently of model policy.
    assert SENTINEL in store.db.execute("SELECT safe_text FROM current_sources WHERE kind=? AND role=?", (payload["type"], payload.get("role", "tool"))).fetchone()[0]


def test_reference_units_are_bounded_exact_and_not_copied_by_model(setup):
    store, engine, options, path, write = setup
    text = "开头段落。\n" + "中间正文" * 400 + "\n结束段。"
    write([message(text)])
    store.import_source(path, "chat", 0)
    job = engine.prepare("chat", options)["job_id"]
    claim = engine.claim(job, backend=options.backend)
    wire = claim["request"]
    private = stored(store, claim)
    assert "input_fingerprint" not in wire and "base_version" not in wire and "indexed" not in wire
    assert "event_id" not in encode(wire) and "snapshot_id" not in encode(wire)
    assert "".join(s["text"] for s in wire["sources"]) == text
    assert max(len(s["text"]) for s in wire["sources"]) <= UNIT_CHARS
    n = len(wire["sources"]) // 2
    minimal = {"items": [{"type": "observation", "title": "核对工作事项", "source_indexes": [n]}]}
    result = engine.submit(claim["attempt_id"], backend=options.backend, output=minimal)
    item = result["result"]["candidate_items"][0]
    source = private["indexed"]["sources"][n]
    assert item["content"] == source["text"] == text[source["start"]:source["end"]]
    assert item["evidence"][0]["quote"] == source["text"]
    assert result["usage"]["call_count"] == 1


@pytest.mark.parametrize("bad", [True, False, 0.0, 1.1, -1, 10000, "0", None, {}, []])
def test_invalid_source_index_rejected_and_usage_kept(setup, bad):
    store, engine, options, *_ = setup
    job = engine.prepare("chat", options)["job_id"]
    claim = engine.claim(job, backend=options.backend)
    result = engine.submit(claim["attempt_id"], backend=options.backend,
                          output={"items": [{"type": "goal", "title": "核对工作事项", "source_indexes": [bad]}]},
                          usage={"input_tokens": 41, "output_tokens": 17})
    assert result["error_code"] == "invalid_model_output"
    assert result["usage"]["input_tokens"] == 41
    assert result["usage"]["output_tokens"] == 17
    assert store.db.execute("SELECT count(*) FROM semantic_ranges WHERE state='semantic_done'").fetchone()[0] == 0


@pytest.mark.parametrize("bad", [True, 0.0, -1, 999, "0", None])
def test_invalid_narrative_index_rejected_without_mutating_candidates(setup, bad):
    store, engine, options, *_ = setup
    job = engine.prepare("chat", options)["job_id"]
    candidates = drain(engine, job)["result"]
    service = Narrative(store)
    service.prepare(job)
    claim = engine.claim(job, backend=options.backend)
    output = layout(claim["request"])
    output["placements"][0]["item_index"] = bad
    result = engine.submit(claim["attempt_id"], backend=options.backend, output=output)
    assert result["error_code"] == "invalid_model_output" and result["result"] == candidates
    assert store.db.execute("SELECT count(*) FROM narrative_versions").fetchone()[0] == 0


def test_sources_and_item_indexes_are_separate_namespaces(setup):
    store, engine, options, *_ = setup
    job = engine.prepare("chat", options)["job_id"]
    for _ in range(2):
        claim = engine.claim(job, backend=options.backend)
        engine.submit(claim["attempt_id"], backend=options.backend, output=reply(claim["request"]))
    claim = engine.claim(job, backend=options.backend)
    assert claim["request"]["phase"] == "merge"
    # Many source units can exist while item catalog has just two entries.
    raw = stored(store, claim)
    raw["sources"][0]["text"] *= 200
    raw["sources"][0]["end"] = raw["sources"][0]["start"] + len(raw["sources"][0]["text"])
    raw = freeze({k: v for k, v in raw.items() if k != "indexed"})
    assert len(raw["indexed"]["sources"]) > len(raw["items"])
    with pytest.raises(ValueError, match="unknown_index"):
        hydrate({"items": [], "edits": [{"op": "relate", "item_index": 2, "source_index": 0, "relation": "contradicts"}]}, raw)


def test_private_map_mutation_or_wrong_request_identity_rejected(setup):
    store, engine, options, *_ = setup
    claim = engine.claim(engine.prepare("chat", options)["job_id"], backend=options.backend)
    private = stored(store, claim)
    damaged = deepcopy(private)
    damaged["indexed"]["sources"][0]["text"] = "different request"
    with pytest.raises(ValueError, match="map_mismatch"):
        model_request(damaged)
    damaged = deepcopy(private)
    damaged["input_fingerprint"] = "another request"
    with pytest.raises(ValueError, match="map_mismatch"):
        hydrate({"items": []}, damaged)
    assert model_request(private) == claim["request"]


def test_merge_retains_unresolved_and_items_without_echo_and_can_add_counterevidence(setup):
    store, engine, options, path, write = setup
    write([message("先前报告通过。", "assistant"), message("更正：集成测试失败。", "assistant")])
    store.import_source(path, "chat", 0)
    job = engine.prepare("chat", options)["job_id"]
    for _ in range(2):
        claim = engine.claim(job, backend=options.backend)
        engine.submit(claim["attempt_id"], backend=options.backend, output=reply(claim["request"], unresolved=["仍待实测"]))
    merge = engine.claim(job, backend=options.backend)
    source = next(s["index"] for s in merge["request"]["sources"] if "失败" in s["text"])
    result = engine.submit(merge["attempt_id"], backend=options.backend, output={"items": [], "edits": [{"op": "relate", "item_index": 0, "source_index": source, "relation": "contradicts"}]})
    assert result["error_code"] is None
    assert result["result"]["unresolved"] == ["仍待实测"]
    assert len(result["result"]["candidate_items"]) == 2
    assert result["result"]["candidate_items"][0]["evidence"][-1]["relation"] == "contradicts"
    assert all(i["attribution"] == "assistant_statement" for i in result["result"]["candidate_items"])


def test_canonical_wire_is_measured_instead_of_private_storage(setup):
    store, engine, options, *_ = setup
    job = engine.prepare("chat", options)["job_id"]
    claim = engine.claim(job, backend=options.backend)
    private = stored(store, claim)
    actual = responses_body(claim["request"])
    assert measured_payload(private) == actual
    assert claim["reserved_input"] == measure_request(actual)["tokens"]
    expected = {k: v for k, v in claim["request"].items() if k not in {"backend", "model", "max_output_tokens"}}
    from seywo.wire_schema import response_schema
    expected["output_schema"] = response_schema(claim["request"])
    assert actual["input"] == encode(expected)
    assert "input_fingerprint" not in actual["input"]
    assert len(encode(actual)) < len(encode(private))


def test_default_function_backend_receives_only_indexed_wire(setup):
    _, engine, options, *_ = setup
    seen = []
    def transport(request):
        seen.append(request)
        assert request["protocol"] == POLICY
        assert "indexed" not in request and "input_fingerprint" not in request
        return {"output": reply(request)}
    job = engine.prepare("chat", options)["job_id"]
    engine.run_one(job, FunctionBackend(options.backend, transport))
    assert len(seen) == 1
    with pytest.raises(ValueError, match="indexed_model_request_required"):
        FunctionBackend(options.backend, transport).complete({"backend": options.backend})


def test_new_target_does_not_inherit_an_obsolete_jobs_limits_or_usage(setup):
    store, engine, options, *_ = setup
    old = engine.prepare("chat", options, Limits(call_count=1))["job_id"]
    claim = engine.claim(old, backend=options.backend)
    private = stored(store, claim)
    private.pop("indexed")
    target = digest(encode({"chat": "chat", "heads": engine._heads("chat"), "base": options.base_version,
                            "override": options.override_revision, "model": options.model}))
    with store.db:
        store.db.execute("UPDATE understanding_jobs SET target_hash=?,state='pending' WHERE id=?", (target, old))
        store.db.execute("UPDATE understanding_tasks SET request_json=? WHERE id=?", (encode(private), claim["task_id"]))
    old_json = store.db.execute("SELECT request_json FROM understanding_tasks WHERE id=?", (claim["task_id"],)).fetchone()[0]
    new = engine.prepare("chat", options)["job_id"]
    assert old != new
    assert engine.status(new)["usage"]["call_count"] == 0
    assert engine.status(old)["usage"]["call_count"] == 1
    assert engine.claim(new, backend=options.backend)["state"] == "request"
    assert store.db.execute("SELECT request_json FROM understanding_tasks WHERE id=?", (claim["task_id"],)).fetchone()[0] == old_json


def test_empty_incremental_b_keeps_history_locks_and_creates_no_spurious_review(setup):
    store, engine, job, service, task, claim, first = publish(setup)
    task = service.prepare(job)
    claim = engine.claim(job, backend="fixture-A")
    wire = claim["request"]
    old = [i for i in wire["items"] if i["origin"] == "existing"]
    incoming = [i for i in wire["items"] if i["origin"] == "incoming"]
    output = {"bindings": [{"item_index": i["index"], "target_index": n, "action": "keep", "reason": "same exact work item"} for n, i in enumerate(incoming)]}
    result = engine.submit(claim["attempt_id"], backend="fixture-A", output=output)
    assert result["error_code"] is None
    second = service.publish(task["task_id"])
    assert len(second["narrative"]["items"]) == len(old)
    assert second["narrative"]["phases"] == first["narrative"]["phases"]
    assert second["narrative"]["transitions"] == first["narrative"]["transitions"]
    assert second["narrative"]["pending_revisions"] == []
    assert second["state"] == "published_unreviewed"
    assert service.read("chat", version=1)["narrative"] == first["narrative"]


def test_legacy_reading_snapshot_cannot_reinsert_tool_derived_text(setup):
    store, _, _, _, _, claim, _ = publish(setup)
    private = stored(store, claim)
    private["previous"]["items"] = [{"content": SENTINEL}]
    with pytest.raises((ValueError, KeyError)):
        compile_request(private)


def test_merge_optional_items_and_explicit_evidenced_concern_resolution(setup):
    store, engine, options, path, write = setup
    write([message("还没有核对预览。"), message("预览已经核对；只允许保存这个预览，不执行迁移。")])
    store.import_source(path, "chat", 0)
    job = engine.prepare("chat", options)["job_id"]
    for _ in range(2):
        claim = engine.claim(job, backend=options.backend)
        engine.submit(claim["attempt_id"], backend=options.backend, output=reply(claim["request"], unresolved=["预览待核对"]))
    claim = engine.claim(job, backend=options.backend)
    source = next(s["index"] for s in claim["request"]["sources"] if "已经核对" in s["text"])
    # No items array and no copying the existing concern text.
    output = {"resolve": [{"unresolved_index": 0, "source_indexes": [source], "reason": "用户明确说明预览核对完成"}]}
    result = engine.submit(claim["attempt_id"], backend=options.backend, output=output)
    assert result["error_code"] is None and result["result"]["unresolved"] == []
    assert result["result"]["resolutions"][0]["evidence"][0]["quote"].startswith("预览已经核对")
    assert len(result["result"]["candidate_items"]) == 2
    assert not result["user_confirmed"] and not result["task_completed"]


@pytest.mark.parametrize("index,anchors", [(True, [0]), (0.0, [0]), (50, [0]), (0, []), (0, [False])])
def test_invalid_resolution_cannot_clear_unresolved(setup, index, anchors):
    store, engine, options, *_ = setup
    job = engine.prepare("chat", options)["job_id"]
    for _ in range(2):
        claim = engine.claim(job, backend=options.backend)
        engine.submit(claim["attempt_id"], backend=options.backend, output=reply(claim["request"], unresolved=["验收待核对"]))
    claim = engine.claim(job, backend=options.backend)
    result = engine.submit(claim["attempt_id"], backend=options.backend, output={"resolve": [{"unresolved_index": index, "source_indexes": anchors, "reason": "fixture"}]})
    assert result["error_code"] == "invalid_model_output" and result["result"] is None
    assert store.db.execute("SELECT count(*) FROM understanding_tasks WHERE phase='merge' AND state='done'").fetchone()[0] == 0


def test_b_resolves_only_explicit_concern_and_preserves_history(setup):
    store, engine, options, *_ = setup
    job = engine.prepare("chat", options)["job_id"]
    for _ in range(3):
        claim = engine.claim(job, backend=options.backend)
        engine.submit(claim["attempt_id"], backend=options.backend, output=reply(claim["request"], unresolved=["来源待核对", "验收未完成"]))
    service = Narrative(store)
    task = service.prepare(job)
    claim = engine.claim(job, backend=options.backend)
    output = layout(claim["request"])
    output["overview"]["unresolved_resolve"] = [{"unresolved_index": 0, "item_indexes": [0], "reason": "fixture source-based concern resolution"}]
    result = engine.submit(claim["attempt_id"], backend=options.backend, output=output)
    assert result["error_code"] is None
    view = service.publish(task["task_id"])
    assert view["narrative"]["overview"]["unresolved"] == ["验收未完成"]
    assert view["narrative"]["resolution_log"][0]["concern"] == "来源待核对"
    assert not view["task_completed"] and not view["user_confirmed"]
    repeated = service.prepare(job)
    claim = engine.claim(job, backend=options.backend)
    assert claim["request"]["unresolved"] == ["验收未完成"]
    assert engine.submit(claim["attempt_id"], backend=options.backend, output={})["error_code"] is None
    again = service.publish(repeated["task_id"])
    assert again["narrative"]["overview"]["unresolved"] == ["验收未完成"]
    assert again["narrative"]["resolution_log"] == view["narrative"]["resolution_log"]
    # A later extraction is a new occurrence, even if the human-facing title matches.
    later = engine.prepare("chat", replace(options, base_version=again["version"]))["job_id"]
    for _ in range(3):
        claim = engine.claim(later, backend=options.backend)
        engine.submit(claim["attempt_id"], backend=options.backend, output=reply(claim["request"], unresolved=["来源待核对"]))
    service.prepare(later)
    claim = engine.claim(later, backend=options.backend)
    assert "来源待核对" in claim["request"]["unresolved"]


def test_multiple_proposals_cannot_overwrite_existing_item(setup):
    store, engine, job, service, task, claim, first = publish(setup)
    setup[4]([message("另一个新表述"), message("工具结果还没有验证", "assistant")])
    store.import_source(setup[3], "chat", 0)
    job = engine.prepare("chat", setup[2])["job_id"]
    drain(engine, job)
    task = service.prepare(job)
    claim = engine.claim(job, backend="fixture-A")
    incoming = [i for i in claim["request"]["items"] if i["origin"] == "incoming"]
    output = {"bindings": [{"item_index": i["index"], "target_index": 0, "action": "propose", "reason": "fixture material new evidence needs review"} for i in incoming]}
    result = engine.submit(claim["attempt_id"], backend="fixture-A", output=output)
    assert result["error_code"] is None
    view = service.publish(task["task_id"])
    # review_state is derived at read time from the current source snapshot.
    # Compare stored bodies to prove no historical item was overwritten.
    old_body = json.loads(store.db.execute("SELECT body_json FROM narrative_versions WHERE chat_id=? AND version=?",
                                         ("chat", first["version"])).fetchone()[0])
    new_body = json.loads(store.db.execute("SELECT body_json FROM narrative_versions WHERE chat_id=? AND version=?",
                                         ("chat", view["version"])).fetchone()[0])
    assert new_body["items"][:len(old_body["items"])] == old_body["items"]
    assert len(view["narrative"]["pending_revisions"]) == 1  # same user identity retains reviewable new evidence
    assert len(view["narrative"]["items"]) == len(first["narrative"]["items"]) + 1  # assistant evidence cannot alias user work
    # Multiple revising bindings to one identity are still rejected.
    private = stored(store, claim)
    output["bindings"][1]["action"] = "revise"
    from seywo.narrative_contract import validate_output
    with pytest.raises(ValueError, match="identity_binding"):
        validate_output(hydrate(output, private), private)


def test_reduction_preserves_original_chronology_with_odd_leaf_count(setup):
    store, engine, options, path, write = setup
    write([message("第一步进入支线。"), message("第二步回到主线。"), message("第三步再次进入支线。")])
    store.import_source(path, "chat", 0)
    job = engine.prepare("chat", options)["job_id"]
    rounds = []
    while True:
        claim = engine.claim(job, backend=options.backend)
        if claim["state"] != "request":
            break
        if claim["request"]["phase"] == "merge":
            rounds.append([s["text"] for s in claim["request"]["sources"]])
        engine.submit(claim["attempt_id"], backend=options.backend, output=reply(claim["request"]))
    assert len(rounds) == 2
    assert rounds[-1] == ["第一步进入支线。", "第二步回到主线。", "第三步再次进入支线。"]


def test_applied_candidates_use_task_identity_instead_of_equal_text(setup):
    store, engine, job, service, _, _, view = publish(setup)
    from test_s3 import amend
    changed = amend(service, view, "item", view["narrative"]["items"][0]["id"], {"content": "人工限定范围"})
    task = service.prepare(job)
    claim = engine.claim(job, backend="fixture-A")
    assert all(i["origin"] == "existing" for i in claim["request"]["items"])
    assert engine.submit(claim["attempt_id"], backend="fixture-A", output={})["error_code"] is None
    next_view = service.publish(task["task_id"])
    assert next_view["narrative"]["items"][0]["content"] == "人工限定范围"
    assert not next_view["narrative"]["pending_revisions"]
    assert service.read("chat", version=changed["version"])["narrative"]["items"][0]["content"] == "人工限定范围"
    # Identical visible text in a NEW physical segment is distinct provenance.
    extra = setup[3].with_name("another-segment.jsonl")
    extra.write_bytes(setup[3].read_bytes())
    store.import_source(extra, "chat", 1)
    newer = engine.prepare("chat", setup[2])["job_id"]
    drain(engine, newer)
    service.prepare(newer)
    fresh = engine.claim(newer, backend="fixture-A")
    assert any(i["origin"] == "incoming" for i in fresh["request"]["items"])


def test_sparse_b_delta_fills_empty_arrays_and_mainline_location(setup):
    store, engine, options, *_ = setup
    job = engine.prepare("chat", options)["job_id"]
    drain(engine, job)
    service = Narrative(store)
    task = service.prepare(job)
    claim = engine.claim(job, backend=options.backend)
    output = {"phases": [{"title": "当前主线", "goal_item_indexes": [0]}],
              "placements": [{"item_index": n, "phase_index": 0} for n in range(len(claim["request"]["items"]))],
              "overview": {"focus": {"phase_index": 0}},
              "transitions": [{"kind": "focus", "from_phase_index": None, "from_branch_index": None,
                               "to_phase_index": 0, "to_branch_index": None, "evidence_source_indexes": [0], "reason": "确定当前主线"}]}
    result = engine.submit(claim["attempt_id"], backend=options.backend, output=output)
    assert result["error_code"] is None
    doc = service.publish(task["task_id"])["narrative"]
    assert doc["transitions"][0]["result_item_ids"] == doc["transitions"][0]["unresolved"] == []
    assert doc["overview"]["focus"]["branch_id"] is None
    assert all(p["branch_id"] is None for p in doc["placements"])


def test_merging_dependent_child_into_parent_removes_only_internal_edges(setup):
    store, engine, options, *_ = setup
    job = engine.prepare("chat", options)["job_id"]
    first = engine.claim(job, backend=options.backend)
    engine.submit(first["attempt_id"], backend=options.backend, output={"items": [
        {"type": "action", "title": "核对工作事项", "source_indexes": [0]},
        {"type": "action", "title": "核对工作事项", "source_indexes": [0], "dependencies": [0]},
        {"type": "next_step", "title": "核对工作事项", "source_indexes": [0], "dependencies": [1]},
    ]})
    second = engine.claim(job, backend=options.backend)
    engine.submit(second["attempt_id"], backend=options.backend, output=reply(second["request"]))
    merge = engine.claim(job, backend=options.backend)
    output = {"edits": [{"op": "merge", "item_indexes": [0, 1], "into_index": 0, "reason": "同一工作事项的细分内容"}]}
    result = engine.submit(merge["attempt_id"], backend=options.backend, output=output)
    assert result["error_code"] is None
    items = result["result"]["candidate_items"]
    assert len(items) == 3 and items[0]["dependencies"] == []
    assert items[1]["dependencies"] == [items[0]["id"]]  # external edge is redirected


@pytest.mark.parametrize("different", ["type", "attribution"])
def test_merge_cannot_collapse_requests_and_observed_results(setup, different):
    store, engine, options, *_ = setup
    job = engine.prepare("chat", options)["job_id"]
    first = engine.claim(job, backend=options.backend)
    engine.submit(first["attempt_id"], backend=options.backend, output={"items": [
        {"type": "action", "title": "进入重试调查", "source_indexes": [0]},
        {"type": "action", "title": "继续重试调查", "source_indexes": [0]},
    ]})
    second = engine.claim(job, backend=options.backend)
    engine.submit(second["attempt_id"], backend=options.backend, output=reply(second["request"]))
    merge = engine.claim(job, backend=options.backend)
    private = stored(store, merge)
    private["items"][1][different] = "observation" if different == "type" else "assistant_statement"
    private = freeze({k: v for k, v in private.items() if k != "indexed"})
    output = {"edits": [{"op": "merge", "item_indexes": [0, 1], "into_index": 0, "reason": "同一重试对象"}]}
    with pytest.raises(ValueError, match="merge_requires_same_type_and_attribution"):
        hydrate(output, private)


@pytest.mark.parametrize("old_policy", ["seywo-stop-4", "seywo-stop-5", "seywo-stop-6"])
def test_only_current_organization_policy_can_freeze_a_request(setup, old_policy):
    store, engine, job, service, _, _, _ = publish(setup)
    service.prepare(job)
    claim = engine.claim(job, backend=setup[2].backend)
    from seywo.model_contract import freeze
    assert model_request(stored(store, claim))["output_schema"]["overview"]["next_step_indexes"] == "action/next_step/goal int[] only"
    old = {k: v for k, v in stored(store, claim).items()
           if k not in {"indexed", "input_fingerprint"}}
    old.update(organization_policy=old_policy, semantic_stop_policy="seywo-stop-intent-2")
    with pytest.raises(ValueError, match='organization_policy_unsupported'):
        freeze(old)
    current = freeze({**old, 'organization_policy': 'seywo-stop-8'})
    assert "omission retains the old list" in model_request(current)["output_schema"]["overview"]["next_step_indexes"]
    assert "same work identity" in model_request(current)["rules"]
    # Explicit [] clears a stale step; absence still preserves a delta by contract.
    output = hydrate({"overview": {"next_step_indexes": []}}, current)
    from seywo.narrative_contract import validate_output
    assert validate_output(output, current)["overview"]["next_step_ids"] == []


def organize_input(setup, policy="seywo-stop-8"):
    store, engine, options, path, write = setup
    store, engine, job, service, task, claim, view = publish(setup)
    write([message("继续调查新的动作")])
    segment = path.with_name("segment-1.jsonl")
    segment.write_bytes(path.read_bytes())
    store.import_source(segment, "chat", 1)
    newer = engine.prepare("chat", options)["job_id"]
    drain(engine, newer)
    service.prepare(newer)
    claim = engine.claim(newer, backend=options.backend)
    private = stored(store, claim)
    private["semantic_stop_policy"] = "seywo-stop-intent-2"
    private["organization_policy"] = policy
    return private, len(private["previous"]["items"])


def frozen(private):
    return freeze({k: v for k, v in private.items() if k not in {"indexed", "input_fingerprint"}})


def keep_output(request, incoming_index):
    from seywo.narrative_contract import validate_output
    return validate_output(hydrate({"bindings": [{
        "item_index": incoming_index, "target_index": 0, "action": "keep", "reason": "同一事项"
    }]}, request), request)


def test_current_semantic_policy_allows_keep_for_refreshed_detail_and_scope(setup):
    private, incoming_index = organize_input(setup)
    incoming = next(i for i in private["incoming"])
    incoming["item"]["content"] = "刷新后的详情"
    incoming["item"]["scope"] = "仅限本轮"
    request = frozen(private)
    doc = keep_output(request, incoming_index)
    assert len(doc["items"]) == len(private["previous"]["items"]) + len(private["incoming"]) - 1
    assert doc["items"][0] == private["previous"]["items"][0]


@pytest.mark.parametrize("changed", ["title", "type", "attribution", "dependencies"])
def test_changed_identity_axis_keeps_old_identity_and_allocates_new_work(setup, changed):
    """A guessed keep cannot overwrite distinct historical work."""
    private, incoming_index = organize_input(setup)
    incoming = next(i for i in private["incoming"])
    if changed == "title":
        incoming["item"]["title"] = "继续调查新的动作"
    elif changed == "type":
        incoming["item"]["type"] = "action"
    elif changed == "attribution":
        incoming["item"]["attribution"] = "assistant_statement"
    else:
        incoming["item"]["dependencies"] = [private["incoming"][1]["candidate_id"]]
    from seywo.narrative_contract import validate_output
    request = frozen(private)
    hydrated = hydrate({"bindings": [{
        "item_index": incoming_index, "target_index": 0, "action": "keep", "reason": "同一条工作线"
    }]}, request)
    assert hydrated["bindings"][0]["action"] == "revise"
    doc = validate_output(hydrated, request)
    assert len(doc["items"]) == len(private["previous"]["items"]) + len(private["incoming"])
    assert doc["items"][0] == private["previous"]["items"][0]
    assert next(i for i in doc["items"] if i["id"] == incoming["new_id"])["title"] == incoming["item"]["title"]


def test_locked_item_still_rejects_mismatched_keep(setup):
    private, incoming_index = organize_input(setup)
    incoming = next(i for i in private["incoming"])
    incoming["item"]["title"] = "继续调查新的动作"
    private["locks"] = [{"entity": "item", "id": private["previous"]["items"][0]["id"],
                         "field": "content", "value": "人工锁定"}]
    request = frozen(private)
    from seywo.narrative_contract import validate_output
    with pytest.raises(ValueError, match="manual_item_revision_conflict"):
        validate_output(hydrate({"bindings": [{
            "item_index": incoming_index, "target_index": 0, "action": "keep", "reason": "同一条工作线"
        }]}, request), request)


@pytest.mark.parametrize("policy", ["seywo-stop-6", "seywo-stop-7"])
def test_old_organization_policy_is_rejected(setup, policy):
    private, incoming_index = organize_input(setup, policy=policy)
    incoming = next(i for i in private["incoming"])
    incoming["item"]["title"] = "继续调查新的动作"
    incoming["item"]["content"] = "另一项动作"
    with pytest.raises(ValueError, match='organization_policy_unsupported'):
        frozen(private)


def test_organize_accepts_verbatim_echoed_preamble():
    from seywo.echo import strip_echo
    request = {"phase": "organize", "protocol": "seywo-index-4", "rules": "r",
               "sources": [], "primary_source_indexes": [], "unresolved": [], "gaps": [],
               "locks": [], "phases": [], "branches": [], "placements": [], "overview": {},
               "recent_transitions": [], "target_hash": "x"}
    echoed = {"phase": "organize", "protocol": "seywo-index-4", "rules": "r",
              "sources": [], "primary_source_indexes": [], "unresolved": [], "gaps": [],
              "locks": [], "recent_transitions": [], "phases": [{"title": "a", "goal_item_indexes": [0]}]}
    stripped = strip_echo(echoed, request)
    assert set(stripped) == {"phases"}


def test_organize_rejects_a_diverging_echo():
    import pytest
    from seywo.echo import strip_echo
    request = {"phase": "organize", "rules": "r", "items": [], "gaps": []}
    with pytest.raises(ValueError, match="echoed_request_field_differs"):
        strip_echo({"rules": "different"}, request)
    with pytest.raises(ValueError, match="invalid_indexed_fields"):
        strip_echo({"items": [{"index": 0}]}, request)


def test_organize_accepts_a_rewrapped_echo_but_not_changed_wording():
    import pytest
    from seywo.echo import strip_echo
    request = {"phase": "organize", "rules": "one two\nthree   four", "items": [], "gaps": []}
    assert strip_echo({"rules": "one two three four"}, request) == {}
    assert strip_echo({"rules": "one two\nthree four"}, request) == {}
    with pytest.raises(ValueError, match="echoed_request_field_differs"):
        strip_echo({"rules": "one two three five"}, request)


def test_mixed_role_validation_has_retry_guidance():
    from seywo.model_contract import RETRY_HINTS
    for code in ("mixed_statement_origin_use_relations", "invalid_item_anchor", "invalid_relation"):
        assert code in RETRY_HINTS and RETRY_HINTS[code]


@pytest.mark.parametrize('change,allowed', [({}, True), ({'phase': 'organize'}, False),
                                           ({'sources': []}, False), ({'unknown': 1}, False)])
def test_extraction_preamble_only_accepts_identical_input_fields(setup, change, allowed):
    store, engine, options, *_ = setup
    job = engine.prepare('chat', options)['job_id']
    claim = engine.claim(job, backend=options.backend)
    output = {'phase': claim['request']['phase'], 'sources': claim['request']['sources'],
              'items': [{'type': 'question', 'title': '核验未决事项',
                         'source_indexes': [claim['request']['primary_source_indexes'][0]]}],
              'unresolved': [0], **change}
    result = engine.submit(claim['attempt_id'], backend=options.backend, output=output)
    assert (result['error_code'] is None) is allowed
    if allowed:
        extracted = json.loads(store.db.execute('SELECT result_json FROM understanding_tasks WHERE id=?', (claim['task_id'],)).fetchone()[0])
        assert extracted['unresolved'] == ['核验未决事项']
        assert len(extracted['candidate_items']) == 1
    else:
        assert result['usage']['call_count'] == 1


@pytest.mark.parametrize('direction', ['before', 'after'])
def test_readback_past_body_boundary_preserves_extraction_without_fake_evidence(setup, direction):
    store, engine, options, path, write = setup
    write([message('检查剩余门禁')])
    store.import_source(path, 'chat', 0)
    job = engine.prepare('chat', options)['job_id']
    claim = engine.claim(job, backend=options.backend)
    result = engine.submit(claim['attempt_id'], backend=options.backend, output={
        'items': [{'type': 'question', 'title': '核验剩余门禁', 'source_indexes': [0]}],
        'reads': [{'source_index': 0, 'direction': direction, 'reason': '核对相邻上下文'}]})
    assert result['error_code'] is None
    assert len(result['result']['candidate_items']) == 1
    assert result['result']['read_requests'] == []
    assert len(result['result']['candidate_items'][0]['evidence']) == 1


def test_proposed_different_question_cannot_swallow_current_user_question(setup):
    private, incoming_index = organize_input(setup)
    incoming = private['incoming'][0]
    incoming['item'].update(type='question', title='为什么采用原生轮次',
                            attribution='user_statement', dependencies=[])
    request = frozen(private)
    output = hydrate({'bindings': [{'item_index': incoming_index, 'target_index': 0,
                                    'action': 'propose', 'reason': '另一个问题的追问'}],
                      'placements': [{'item_index': incoming_index, 'phase_index': 0}],
                      'overview': {'goal_item_indexes': [incoming_index], 'next_step_indexes': []}}, request)
    from seywo.narrative_contract import validate_output
    doc = validate_output(output, request)
    assert doc['items'][0] == private['previous']['items'][0]
    assert incoming['new_id'] in doc['overview']['goal_item_ids']
    assert next(i for i in doc['items'] if i['id'] == incoming['new_id'])['title'] == '为什么采用原生轮次'
    assert doc['pending_revisions'] == private['previous'].get('pending_revisions', [])


def test_new_unused_phase_copy_does_not_add_an_empty_stage(setup):
    private, _ = organize_input(setup)
    request = frozen(private)
    output = hydrate({'phases': [{'title': private['previous']['phases'][0]['title'],
                                   'goal_item_indexes': []}]}, request)
    assert output['phases'] == private['previous']['phases']


@pytest.mark.parametrize('clear,locked,allowed', [(False, False, False), (True, False, True), (False, True, True)])
def test_explanatory_stop_cannot_inherit_an_old_assistant_action_as_pending(setup, clear, locked, allowed):
    from seywo.windows import goal_orders
    private, incoming_index = organize_input(setup)
    incoming = private['incoming'][0]
    incoming['item'].update(type='question', title='为什么采用这个校验',
                            attribution='user_statement', dependencies=[])
    old_action = private['previous']['items'][1]
    old_action['type'] = 'action'
    private['previous']['overview']['next_step_ids'] = [old_action['id']]
    private['user_goal_order'] = goal_orders(private, setup[0].db)
    if locked:
        private['locks'].append({'entity': 'overview', 'id': 'overview', 'field': 'next_step_ids',
                                 'value': [old_action['id']]})
    request = frozen(private)
    delta = {'placements': [{'item_index': incoming_index, 'phase_index': 0}],
             'overview': {'goal_item_indexes': [incoming_index]}}
    if clear:
        delta['overview']['next_step_indexes'] = []
    if allowed:
        assert hydrate(delta, request)['overview']['goal_item_ids']
    else:
        with pytest.raises(ValueError, match='stale_assistant_action_as_next_step'):
            hydrate(delta, request)


@pytest.mark.parametrize('locked', [False, True])
def test_latest_user_question_pointer_is_rule_maintained_not_assistant_question(setup, locked):
    from seywo.windows import goal_orders
    private, incoming_index = organize_input(setup)
    incoming = private['incoming'][0]
    incoming['item'].update(type='question', title='为何按原生轮次划分',
                            attribution='user_statement', dependencies=[])
    assistant = private['previous']['items'][1]
    assistant.update(type='question', title='是否按建议修改方案')
    private['user_goal_order'] = goal_orders(private, setup[0].db)
    old_id = private['previous']['items'][0]['id']
    if locked:
        private['locks'].append({'entity': 'overview', 'id': 'overview', 'field': 'goal_item_ids',
                                 'value': [old_id]})
    request = frozen(private)
    output = hydrate({'placements': [{'item_index': incoming_index, 'phase_index': 0}],
                      'overview': {'goal_item_indexes': [0] if locked else [1],
                                   'next_step_indexes': []}}, request)
    assert output['overview']['goal_item_ids'] == ([old_id] if locked else [incoming['new_id']])
    from seywo.narrative_contract import validate_output
    doc = validate_output(output, request)
    assert doc['overview']['goal_item_ids'] == output['overview']['goal_item_ids']
    assert next(i for i in doc['items'] if i['id'] == old_id)['content'] == private['previous']['items'][0]['content']


def test_bounded_organizer_cannot_turn_unflagged_passed_observation_into_blocker(setup):
    private, incoming_index = organize_input(setup)
    incoming = private['incoming'][0]
    incoming['item'].update(type='observation', title='交付检查通过', attribution='assistant_statement')
    private['unresolved'] = []
    request = frozen(private)
    with pytest.raises(ValueError, match='unresolved_not_extracted'):
        hydrate({'overview': {'unresolved_add': [incoming_index]}}, request)
    # An issue explicitly detected by A may carry through organization.
    incoming['item']['title'] = '验收签字未完成'
    private['unresolved'] = ['验收签字未完成']
    request = frozen(private)
    assert hydrate({'overview': {'unresolved_add': [incoming_index]}}, request)['overview']['unresolved'] == ['验收签字未完成']
