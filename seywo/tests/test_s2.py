from dataclasses import replace
import json

import pytest

from seywo.config import initialize, load_config
from seywo.provider import Capacity
from seywo.store import Store
from seywo.understanding import Limits, Options, Understanding


def message(text, role="user"):
    return {"type": "response_item", "payload": {"type": "message", "role": role,
            "content": [{"type": "input_text", "text": text}]}}


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "project"
    initialize(root)
    cfg = load_config(root / ".seywo/config.json")
    path = root / "source.jsonl"
    def write(records):
        events = [{"type": "session_meta", "payload": {"id": "chat", "cwd": str(root)}}, *records]
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in events))
    write([message("先完成索引"), message("工具结果还没有验证", "assistant")])
    store = Store(cfg)
    store.import_source(path, "chat", 0)
    options = Options("fixture-A", "test-model-v1", Capacity(64000, 32000, 8000), context_chars=0)
    engine = Understanding(store)
    yield store, engine, options, path, write
    store.__exit__()


def reply(request, *, items=None, reads=None, unresolved=None):
    if items is None:
        items = [{"type": "observation", "title": "核对工作事项", "source_indexes": request["primary_source_indexes"]}] if request["phase"] == "extract" else []
    indexes = []
    for concern in unresolved or []:
        catalog = request["items"] + items
        match = next((n for n, i in enumerate(catalog) if i["title"] == concern), None)
        if match is None:
            if items and items[0]["title"] == "核对工作事项":
                items[0]["title"] = concern
                match = len(request["items"])
            else:
                items.append({"type": "question", "title": concern,
                              "source_indexes": request["primary_source_indexes"][:1]})
                match = len(request["items"]) + len(items) - 1
        indexes.append(match)
    return {"items": items, "reads": reads or [], "unresolved": indexes}


def drain(engine, job_id, *, maximum=100):
    for _ in range(maximum):
        claim = engine.claim(job_id, backend="fixture-A")
        if claim["state"] != "request":
            return engine.status(job_id)
        request = claim["request"]
        result = engine.submit(claim["attempt_id"], backend="fixture-A", output=reply(request),
                               usage={"input_tokens": 500, "output_tokens": 400})
        if result["state"] in {"ready", "partial", "needs_review", "waiting_context"}:
            return result
    pytest.fail("unexpected unbounded scheduling")


def test_extract_merge_attribution_ranges_and_read_only_no_calls(setup):
    store, engine, options, _, _ = setup
    job = engine.prepare("chat", options)
    result = drain(engine, job["job_id"])
    assert result["state"] == "ready"
    assert len(result["result"]["candidate_items"]) == 2
    assert {i["attribution"] for i in result["result"]["candidate_items"]} == {"user_statement", "assistant_statement"}
    assert result["candidate_only"] and not result["user_confirmed"] and not result["task_completed"]
    assert result["usage"]["call_count"] == 3
    assert store.db.execute("SELECT count(*) FROM semantic_ranges WHERE state='semantic_done'").fetchone()[0] == 2
    assert engine.status(job["job_id"])["usage"] == result["usage"]
    assert store.status()["semantic_done"] is False  # S1 search remains source-only.


def test_long_material_all_characters_accounted_and_middle_counterevidence(setup):
    store, engine, options, path, write = setup
    text = "a" * 900 + "关键反证：中间仍然失败" + "z" * 900
    write([{"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c", "output": text}}])
    store.import_source(path, "chat", 0)
    job = engine.prepare("chat", replace(options, chunk_chars=400, overlap_chars=50))
    result = drain(engine, job["job_id"])
    assert result["state"] == "ready" and result["usage"]["call_count"] == 0
    assert result["result"]["candidate_items"] == []
    assert store.db.execute("SELECT count(*) FROM semantic_ranges").fetchone()[0] == 0
    excluded = next(r for r in result["excluded_policy"] if r["reason"] == "tool_response_excluded_by_policy")
    assert excluded["characters"] == len(text)
    assert text == store.db.execute("SELECT safe_text FROM current_sources WHERE role='tool'").fetchone()[0]


@pytest.mark.parametrize("mutation", ["quote", "range", "identity", "role", "source_accounting", "unknown_field", "no_change"])
def test_unverified_model_outputs_cannot_advance_coverage(setup, mutation):
    store, engine, options, _, _ = setup
    job = engine.prepare("chat", options)
    claim = engine.claim(job["job_id"], backend=options.backend)
    result = reply(claim["request"])
    if mutation == "quote":
        result["items"][0]["quote"] = "fabricated"
    elif mutation == "range":
        result["items"][0]["source_indexes"] = [99999]
    elif mutation == "role":
        result["items"][0]["attribution"] = "tool_observation"
    else:
        result[{"identity": "input_fingerprint", "source_accounting": "source_ranges", "unknown_field": "approved", "no_change": "no_change"}[mutation]] = True
    status = engine.submit(claim["attempt_id"], backend=options.backend, output=result)
    assert status["error_code"] == "invalid_model_output" and not status["result"]
    assert store.db.execute("SELECT count(*) FROM semantic_ranges WHERE state='semantic_done'").fetchone()[0] == 0


def test_no_change_is_still_explicit_semantic_processing(setup):
    store, engine, options, _, write = setup
    write([message("嗯")])
    store.import_source(setup[3], "chat", 0)
    job = engine.prepare("chat", options)
    claim = engine.claim(job["job_id"], backend=options.backend)
    status = engine.submit(claim["attempt_id"], backend=options.backend, output=reply(claim["request"], items=[]))
    assert status["state"] == "ready" and status["result"]["no_change"]
    assert store.db.execute("SELECT state FROM semantic_ranges").fetchone()[0] == "semantic_done"


def test_budget_pause_cannot_reset_by_prepare_or_new_backend(setup):
    _, engine, options, _, _ = setup
    limits = Limits(total_input_tokens=1)
    job = engine.prepare("chat", options, limits)
    assert engine.claim(job["job_id"], backend=options.backend)["state"] == "waiting_budget"
    again = engine.prepare("chat", options, Limits())
    assert again["job_id"] == job["job_id"]
    assert engine.status(job["job_id"])["limits"]["total_input_tokens"] == 1
    with pytest.raises(ValueError):
        engine.prepare("chat", replace(options, backend="other"))
    engine.replenish(job["job_id"], Limits())
    assert drain(engine, job["job_id"])["state"] == "ready"


def test_usage_idempotence_unknown_costs_leases_and_retry_limit(setup):
    _, engine, options, _, _ = setup
    clock = [100.0]
    engine.clock = lambda: clock[0]
    job = engine.prepare("chat", options, Limits(retry_limit=2))
    first = engine.claim(job["job_id"], backend=options.backend)
    assert engine.claim(job["job_id"], backend=options.backend)["state"] == "inflight"
    clock[0] += options.lease_seconds + 1
    second = engine.claim(job["job_id"], backend=options.backend)
    assert first["attempt_id"] != second["attempt_id"]
    before = engine.status(job["job_id"])["usage"]["input_tokens"]
    late = engine.submit(first["attempt_id"], backend=options.backend, output=reply(first["request"]),
                         usage={"input_tokens": 10, "output_tokens": 20})
    assert late["usage"]["call_count"] == 2 and late["attempts"][0]["state"] == "late"
    assert late["usage"]["input_tokens"] == before - first["reserved_input"] + 10
    engine.submit(second["attempt_id"], backend=options.backend, finish="length")
    assert engine.claim(job["job_id"], backend=options.backend)["state"] == "waiting_retry_limit"
    same = engine.submit(first["attempt_id"], backend=options.backend, output=reply(first["request"]), usage={"input_tokens": 10, "output_tokens": 20})
    assert same["usage"] == engine.status(job["job_id"])["usage"]
    with pytest.raises(ValueError):
        engine.submit(first["attempt_id"], backend=options.backend, output={})


def test_output_truncation_retries_without_marking_source_done(setup):
    store, engine, options, _, _ = setup
    job = engine.prepare("chat", options)
    first = engine.claim(job["job_id"], backend=options.backend)
    state = engine.submit(first["attempt_id"], backend=options.backend, output={"partial": True}, finish="length")
    assert state["error_code"] == "output_truncated"
    assert store.db.execute("SELECT count(*) FROM semantic_ranges WHERE state='semantic_done'").fetchone()[0] == 0
    second = engine.claim(job["job_id"], backend=options.backend)
    assert first["task_id"] == second["task_id"]
    assert state["usage"]["call_count"] == 1


def test_readback_is_same_target_only_and_charged_as_new_request(setup):
    store, engine, options, path, write = setup
    write([message("前文。" * 300)])
    store.import_source(path, "chat", 0)
    job = engine.prepare("chat", replace(options, chunk_chars=300, overlap_chars=0))
    first = engine.claim(job["job_id"], backend=options.backend)
    read = {"source_index": 0, "direction": "after", "reason": "核对相邻正文"}
    result = engine.submit(first["attempt_id"], backend=options.backend, output=reply(first["request"], reads=[read]))
    assert result["state"] == "pending"
    tasks = [json.loads(r[0]) for r in store.db.execute("SELECT request_json FROM understanding_tasks")]
    assert any(any(s["purpose"] == "readback" for s in r["sources"]) for r in tasks)
    assert drain(engine, job["job_id"])["state"] == "ready"
    assert engine.status(job["job_id"])["usage"]["call_count"] > 1


def test_arbitrary_readback_and_cross_chat_citations_rejected(setup):
    _, engine, options, _, _ = setup
    job = engine.prepare("chat", options)
    first = engine.claim(job["job_id"], backend=options.backend)
    read = {"event_id": "foreign", "snapshot_id": "foreign", "start": 0, "end": 100, "reason": "read all"}
    result = engine.submit(first["attempt_id"], backend=options.backend, output=reply(first["request"], reads=[read]))
    assert result["error_code"] == "invalid_model_output"


def test_source_mutation_and_deletion_block_inflight_publication(setup):
    store, engine, options, path, write = setup
    job = engine.prepare("chat", options)
    first = engine.claim(job["job_id"], backend=options.backend)
    write([message("changed")])
    store.import_source(path, "chat", 0)
    status = engine.submit(first["attempt_id"], backend=options.backend, output=reply(first["request"]))
    assert status["state"] == "stale_source" and status["result"] is None
    newer = engine.prepare("chat", options)
    inflight = engine.claim(newer["job_id"], backend=options.backend)
    store.delete_chat("chat")
    assert store.db.execute("SELECT count(*) FROM understanding_jobs").fetchone()[0] == 0
    with pytest.raises(ValueError):
        engine.submit(inflight["attempt_id"], backend=options.backend, output=reply(inflight["request"]))


def test_merge_requires_dispositions_for_every_item(setup):
    _, engine, options, _, _ = setup
    job = engine.prepare("chat", options)
    for _ in range(2):
        claim = engine.claim(job["job_id"], backend=options.backend)
        engine.submit(claim["attempt_id"], backend=options.backend, output=reply(claim["request"]))
    merge = engine.claim(job["job_id"], backend=options.backend)
    assert merge["request"]["phase"] == "merge"
    status = engine.submit(merge["attempt_id"], backend=options.backend, output={"items": []})
    assert status["error_code"] is None and len(status["result"]["candidate_items"]) == 2
    assert len(status["result"]["dispositions"]) == 2


def test_unresolved_and_media_gaps_do_not_turn_into_complete_understanding(setup):
    store, engine, options, _, write = setup
    record = message("可见但带图像")
    record["payload"]["content"].append({"type": "input_image", "image_url": "data:private"})
    write([record])
    store.import_source(setup[3], "chat", 0)
    job = engine.prepare("chat", options)
    first = engine.claim(job["job_id"], backend=options.backend)
    assert first["request"]["gaps"] == [{"reason": "partial_content", "count": 1}]
    status = engine.submit(first["attempt_id"], backend=options.backend, output=reply(first["request"], unresolved=["图像未解析"]))
    assert status["state"] == "partial"
    assert not status["task_completed"]


def test_bounded_judgment_abstention_and_no_silent_backend_fallback(setup):
    _, engine, options, _, _ = setup
    job = engine.prepare("chat", options)
    final = drain(engine, job["job_id"])
    item_id = final["result"]["candidate_items"][0]["id"]
    engine.queue_judgment(job["job_id"], kind="support", question="当前证据支持这个结论吗？", item_ids=[item_id])
    with pytest.raises(ValueError):
        engine.claim(job["job_id"], backend="pretend-jev")
    claim = engine.claim(job["job_id"], backend=options.backend)
    assert claim["request"]["phase"] == "judgment"
    choices = claim["request"]["options"]
    response = {"choice": "insufficient",
                "scores": {c: 1.0 if c == "insufficient" else 0.0 for c in choices},
                "score_kind": "probabilities", "reason": "材料不足"}
    status = engine.submit(claim["attempt_id"], backend=options.backend, output=response)
    assert status["judgments"][0]["choice"] == "insufficient"
    assert not status["user_confirmed"] and not status["task_completed"]
    assert status["attempts"][-1]["backend"] == "fixture-A"


def test_injected_backend_error_preserves_usage_and_recovery(setup):
    _, engine, options, _, _ = setup
    job = engine.prepare("chat", options)
    class Broken:
        identity = "fixture-A"
        def complete(self, _):
            raise TimeoutError("must not persist secret payload")
    result = engine.run_one(job["job_id"], Broken())
    assert result["error_code"] == "backend_failure"
    assert result["attempts"][0]["usage_kind"] == "reserved_estimate"
    assert "secret" not in json.dumps(result)


def test_fixed_schema_capacity_failure_rolls_back_job_creation(setup):
    store, engine, options, _, _ = setup
    small = replace(options, capacity=Capacity(5000, 2000, 4000), application_cap=300)
    with pytest.raises(ValueError, match="schema_exceeds_capacity"):
        engine.prepare("chat", small)
    assert store.db.execute("SELECT count(*) FROM understanding_jobs").fetchone()[0] == 0


def test_jev_adapter_uses_explicit_protocol_and_can_abstain(setup):
    from seywo.backends import JevBackend
    _, engine, options, _, _ = setup
    job = engine.prepare("chat", options)
    final = drain(engine, job["job_id"])
    calls = []
    def evaluate(state, questions):
        calls.append((state, questions))
        assert set(questions["q"]) == {"type", "instructions", "criteria"}
        assert questions["q"]["type"] == "choice"
        assert questions["q"]["instructions"] == "是否有明确批准？"
        criteria = questions["q"]["criteria"]
        return {"model": "test-jev", "answers": {"q": {"choice": "unclear", "probabilities": {
            c: 1.0 if c == "unclear" else 0.0 for c in criteria}}},
            "usage": {"input_tokens": 100, "output_tokens": 50}}
    adapter = JevBackend("test-jev", evaluate)
    engine.queue_judgment(job["job_id"], kind="approval", question="是否有明确批准？",
                          item_ids=[final["result"]["candidate_items"][0]["id"]],
                          backend=adapter.identity, model=adapter.model)
    result = engine.run_one(job["job_id"], adapter)
    assert result["judgments"][0]["choice"] == "unclear"
    assert result["attempts"][-1]["backend"] == "jev:test-jev"
    assert calls[0][0]["sources"] and "unclear" in calls[0][1]["q"]["criteria"]
    assert not result["user_confirmed"]


def test_general_adapter_and_crash_reopen_resumes_done_tasks(setup):
    from seywo.backends import FunctionBackend
    store, engine, options, _, _ = setup
    job = engine.prepare("chat", options)
    backend = FunctionBackend("fixture-A", lambda request: {"output": reply(request),
                                                           "usage": {"input_tokens": 300, "output_tokens": 200}})
    first = engine.run_one(job["job_id"], backend)
    done = next(t["id"] for t in first["tasks"] if t["state"] == "done")
    with Store(store.config) as reopened:
        other = Understanding(reopened)
        while True:
            result = other.run_one(job["job_id"], backend)
            if result["state"] == "ready":
                break
        assert len([a for a in result["attempts"] if a["task_id"] == done]) == 1
        assert result["usage"]["call_count"] == 3


def test_capacity_overrun_is_charged_but_not_accepted(setup):
    _, engine, options, _, _ = setup
    job = engine.prepare("chat", options)
    claim = engine.claim(job["job_id"], backend=options.backend)
    result = engine.submit(claim["attempt_id"], backend=options.backend,
                           output=reply(claim["request"]), usage={"input_tokens": 999999, "output_tokens": 999999})
    assert result["usage"]["input_tokens"] == 999999
    assert result["error_code"] == "provider_usage_exceeds_capacity"
    assert engine.claim(job["job_id"], backend=options.backend)["state"] == "waiting_budget"


def test_generated_secret_not_persisted(setup):
    store, engine, options, _, _ = setup
    job = engine.prepare("chat", options)
    claim = engine.claim(job["job_id"], backend=options.backend)
    output = reply(claim["request"])
    secret = "sk-" + "a" * 32
    output["items"][0]["detail"] = secret
    result = engine.submit(claim["attempt_id"], backend=options.backend, output=output)
    assert result["error_code"] == "invalid_model_output"
    assert secret.encode() not in __import__("pathlib").Path(store.config["_database"]).read_bytes()


def test_cyclic_dependencies_rejected(setup):
    _, engine, options, _, _ = setup
    job = engine.prepare("chat", options)
    claim = engine.claim(job["job_id"], backend=options.backend)
    output = {"items": [{"type": "action", "title": "核对工作事项", "source_indexes": [0], "dependencies": [1]},
                        {"type": "action", "title": "核对工作事项", "source_indexes": [0], "dependencies": [0]}]}
    result = engine.submit(claim["attempt_id"], backend=options.backend, output=output)
    assert result["error_code"] == "invalid_model_output"


def test_read_round_limit_keeps_candidates_pending_not_published(setup):
    store, engine, options, path, write = setup
    write([message("需要补资料。" * 300)])
    store.import_source(path, "chat", 0)
    options = replace(options, max_read_rounds=1, chunk_chars=600)
    job = engine.prepare("chat", options)
    first_task = None
    for _ in range(15):
        claim = engine.claim(job["job_id"], backend=options.backend)
        if claim["state"] != "request":
            break
        if first_task is None:
            first_task = claim["task_id"]
        read = {"source_index": 0, "direction": "after", "reason": "仍需材料"}
        # Focus this test on the read-round cap, not EOF availability.
        if claim["task_id"] != first_task and len(claim["request"]["sources"]) == 1:
            engine.submit(claim["attempt_id"], backend=options.backend, output=reply(claim["request"]))
            continue
        result = engine.submit(claim["attempt_id"], backend=options.backend, output=reply(claim["request"], reads=[read]))
        if result["state"] == "needs_review":
            break
    assert result["state"] == "needs_review" and result["result"] is None


def test_cli_outbox_end_to_end(setup, capsys, tmp_path):
    from dataclasses import asdict
    from seywo.cli import main
    store, _, options, _, write = setup
    write([message("cli bounded understanding")])
    store.import_source(setup[3], "chat", 0)
    optfile = tmp_path / "options.json"
    optfile.write_text(json.dumps(asdict(options)))
    prefix = ["--config", store.config["_path"], "understand"]
    def invoke(*args):
        assert main([*prefix, *args]) == 0
        return json.loads(capsys.readouterr().out)
    job = invoke("prepare", "--chat", "chat", "--options", str(optfile))
    claim = invoke("claim", job["job_id"], "--backend", options.backend)
    response = tmp_path / "response.json"
    response.write_text(json.dumps({"output": reply(claim["request"]), "usage": {"input_tokens": 100, "output_tokens": 100}}))
    result = invoke("submit", claim["attempt_id"], "--backend", options.backend, "--response", str(response))
    assert result["state"] == "ready"
    assert invoke("show", job["job_id"])["result"]["candidate_items"]


def test_claim_serializes_workers_and_never_reserves_twice(setup):
    store, engine, options, _, _ = setup
    job = engine.prepare("chat", options)
    first = engine.claim(job["job_id"], backend=options.backend)
    with Store(store.config) as second_store:
        second = Understanding(second_store)
        assert second.claim(job["job_id"], backend=options.backend)["state"] == "inflight"
        assert second.status(job["job_id"])["usage"]["call_count"] == 1
    assert first["state"] == "request"


def test_merge_over_capacity_waits_without_dropping_extracted_items(setup):
    store, engine, options, _, write = setup
    write([message("x" * 6000), message("y" * 6000)])
    store.import_source(setup[3], "chat", 0)
    options = replace(options, application_cap=18000, chunk_chars=6000)
    job = engine.prepare("chat", options, Limits(total_input_tokens=200000, total_output_tokens=40000))
    for _ in range(2):
        claim = engine.claim(job["job_id"], backend=options.backend)
        assert claim["request"]["phase"] == "extract"
        # Long exact evidence is intentionally too big to combine under this cap.
        result = engine.submit(claim["attempt_id"], backend=options.backend,
                               output={"items": [{"type": "observation", "title": "核对工作事项", "source_indexes": list(range(len(claim["request"]["sources"])))}]}, usage={"input_tokens": 1000, "output_tokens": 1000})
    assert result["state"] == "waiting_context"
    assert result["result"] is None
    assert len([t for t in result["tasks"] if t["state"] == "merged"]) == 2
    assert engine.claim(job["job_id"], backend=options.backend)["state"] == "waiting_context"
    usage_before = result["usage"]
    raised = replace(options, application_cap=180000, capacity=Capacity(220000, 200000, 8000))
    expanded = engine.expand_context(job["job_id"], raised)
    assert expanded["usage"] == usage_before and expanded["state"] == "pending"
    resumed = engine.claim(job["job_id"], backend=options.backend)
    assert resumed["request"]["phase"] == "merge"
    assert len(resumed["request"]["items"]) == 2


def test_unresolved_merge_cannot_silently_forget_a_cross_chunk_question(setup):
    _, engine, options, _, _ = setup
    job = engine.prepare("chat", options)
    for _ in range(2):
        claim = engine.claim(job["job_id"], backend=options.backend)
        engine.submit(claim["attempt_id"], backend=options.backend,
                      output=reply(claim["request"], unresolved=["跨片指代未解"]))
    merge = engine.claim(job["job_id"], backend=options.backend)
    output = reply(merge["request"], unresolved=[])
    result = engine.submit(merge["attempt_id"], backend=options.backend, output=output)
    assert result["error_code"] is None
    assert result["result"]["unresolved"] == ["跨片指代未解"]


def test_source_status_never_claims_zero_calls_after_extraction_attempt(setup):
    store, engine, options, _, _ = setup
    job = engine.prepare("chat", options)
    engine.claim(job["job_id"], backend=options.backend)
    status = store.status()
    assert status["model_calls"] is None and status["model_attempts"] == 1
    assert status["understanding_jobs"] == {"running": 1}


def test_jev_concrete_version_is_required_when_provider_resolves_an_alias():
    from seywo.backends import JevBackend
    request = {"phase": "judgment", "backend": "jev:jev-latest", "question": "Can we tell?",
               "options": ["same", "different", "insufficient"], "items": [], "sources": [],
               "unresolved": [], "gaps": [], "rules": {}, "protocol": "seywo-index-5", "model": "jev-latest", "primary_source_indexes": [], "max_output_tokens": 4000, "output_schema": {}, "kind": "identity"}
    def evaluate(state, questions):
        return {"model": "jev-1.14.0", "answers": {"q": {"choice": "insufficient",
            "probabilities": {"same": 0.0, "different": 0.0, "insufficient": 1.0}}}}
    with pytest.raises(ValueError, match="unexpected_jev_model"):
        JevBackend("jev-latest", evaluate).complete(request)
    request["backend"] = "jev:jev-1.14.0"
    request["model"] = "jev-1.14.0"
    accepted = JevBackend("jev-1.14.0", evaluate).complete(request)
    assert accepted["output"]["choice"] == "insufficient"
    with pytest.raises(ValueError, match="unexpected_jev_model"):
        JevBackend("jev-1.14.0", lambda *a: evaluate(*a) | {"model": "jev-other"}).complete(request)
