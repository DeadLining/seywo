"""Adversarial S1–S3 checks; synthetic data, no Hook or external provider."""

import copy

import pytest

from test_s2 import setup as setup, message, reply
from test_s3 import amend, layout, organize, publish
from seywo.narrative import Narrative
from seywo.store import Store


@pytest.mark.parametrize("field,value", [
    ("base_version", False),
    ("base_version", 0.0),
    ("override_revision", False),
    ("override_revision", 0.0),
    ("source_start", False),
    ("source_start", 0.0),
    ("source_end", 7.0),
])
def test_wrong_json_scalar_types_cannot_advance_coverage_and_can_retry(setup, field, value):
    store, engine, options, path, write = setup
    write([message("1234567")])
    store.import_source(path, "chat", 0)
    job = engine.prepare("chat", options)["job_id"]
    claim = engine.claim(job, backend=options.backend)
    output = reply(claim["request"])
    if field.startswith("source_"):
        output["items"][0]["source_indexes"] = [value]
    else:
        output[field] = value
    failed = engine.submit(claim["attempt_id"], backend=options.backend, output=output)
    assert failed["error_code"] == "invalid_model_output"
    assert failed["result"] is None
    assert store.db.execute("SELECT count(*) FROM semantic_ranges WHERE state='semantic_done'").fetchone()[0] == 0
    assert failed["usage"]["call_count"] == 1
    retry = engine.claim(job, backend=options.backend)
    recovered = engine.submit(retry["attempt_id"], backend=options.backend, output=reply(retry["request"]))
    assert recovered["state"] == "ready"
    assert recovered["usage"]["call_count"] == 2
    assert [a["state"] for a in recovered["attempts"]] == ["failed", "accepted"]


@pytest.mark.parametrize("field,value", [
    ("bindings", [None]),
    ("phases", [None]),
    ("placements", [False]),
    ("overview", {"focus": []}),
])
def test_malformed_organizer_preserves_a_and_retries_without_publication(setup, field, value):
    store, engine, job, service, task, claim = organize(setup)
    extracted = copy.deepcopy(engine.status(job)["result"])
    output = layout(claim["request"])
    output[field] = value
    failed = engine.submit(claim["attempt_id"], backend="fixture-A", output=output)
    assert failed["error_code"] == "invalid_model_output"
    assert failed["result"] == extracted
    with pytest.raises(ValueError, match="narrative_not_published"):
        service.read("chat")
    retry = engine.claim(job, backend="fixture-A")
    engine.submit(retry["attempt_id"], backend="fixture-A", output=layout(retry["request"]))
    assert service.publish(task["task_id"])["version"] == 1
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_independent_connections_cannot_overwrite_manual_changes(setup):
    store, _, _, service, _, _, view = publish(setup)
    with Store(store.config) as other_store:
        other = Narrative(other_store)
        before = other.read("chat")
        changed = amend(service, view, "phase", view["narrative"]["phases"][0]["id"], {"title": "人工确认的阶段名"})
        with pytest.raises(ValueError, match="narrative_version_conflict"):
            amend(other, before, "overview", "overview", {"open_question_ids": []})
        assert other.read("chat")["version"] == changed["version"]
        assert other.read("chat", version=1)["narrative"] == view["narrative"]
    assert store.db.execute("SELECT count(*) FROM narrative_overrides").fetchone()[0] == 1


@pytest.mark.parametrize("relation", ["contradicts", "qualifies", "supports"])
def test_cross_role_evidence_preserves_statement_origin_with_own_anchor(setup, relation):
    from dataclasses import replace
    store, engine, options, _, _ = setup
    options = replace(options, context_chars=200)
    job = engine.prepare("chat", options)["job_id"]
    claim = engine.claim(job, backend=options.backend)
    output = reply(claim["request"])
    context = claim["request"]["sources"][1]
    output["items"][0]["relations"] = [{"source_index": context["index"], "relation": relation}]
    result = engine.submit(claim["attempt_id"], backend=options.backend, output=output)
    assert result["error_code"] is None
    saved = store.db.execute("SELECT result_json FROM understanding_tasks WHERE id=?", (claim["task_id"],)).fetchone()[0]
    import json
    item = json.loads(saved)["candidate_items"][0]
    assert item["attribution"] == "user_statement"
    assert item["evidence"][1]["relation"] == relation


def test_foreign_counterevidence_cannot_supply_missing_statement_origin(setup):
    from dataclasses import replace
    store, engine, options, _, _ = setup
    job = engine.prepare("chat", replace(options, context_chars=200))["job_id"]
    claim = engine.claim(job, backend=options.backend)
    output = reply(claim["request"])
    output["items"][0]["source_indexes"] = [1]  # context cannot supply a standalone item
    result = engine.submit(claim["attempt_id"], backend=options.backend, output=output)
    assert result["error_code"] == "invalid_model_output"
