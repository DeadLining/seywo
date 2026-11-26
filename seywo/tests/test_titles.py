"""Short labels stay distinct from source evidence, details and manual history."""
import copy
import json

import pytest

from test_s2 import setup as setup, message, drain
from test_s3 import publish, amend, layout
from seywo.model_contract import POLICY, hydrate
from seywo.narrative_contract import validate_output
from seywo.store import encode


@pytest.mark.parametrize("title", [None, 42, True, "", " ", " 前后空白 ", "字" * 41,
                                   "第一行\n第二行", "横\t线", "段\u2028落", "前\x00后"])
def test_invalid_model_title_is_rejected_before_semantic_progress(setup, title):
    store, engine, options, *_ = setup
    job = engine.prepare("chat", options)["job_id"]
    claim = engine.claim(job, backend=options.backend)
    result = engine.submit(claim["attempt_id"], backend=options.backend,
                           output={"items": [{"type": "goal", "title": title, "source_indexes": [0]}]},
                           usage={"input_tokens": 41, "output_tokens": 17})
    assert result["error_code"] == "invalid_model_output"
    assert result["usage"]["input_tokens"] == 41
    assert result["usage"]["output_tokens"] == 17
    assert store.db.execute("SELECT count(*) FROM semantic_ranges WHERE state='semantic_done'").fetchone()[0] == 0


def test_missing_title_cannot_fall_back_to_source_prefix(setup):
    _, engine, options, *_ = setup
    job = engine.prepare("chat", options)["job_id"]
    claim = engine.claim(job, backend=options.backend)
    result = engine.submit(claim["attempt_id"], backend=options.backend,
                           output={"items": [{"type": "goal", "source_indexes": [0]}]})
    assert result["error_code"] == "invalid_model_output"


def test_title_detail_and_quote_remain_independent(setup):
    store, engine, options, path, write = setup
    source = "现在只生成迁移预览。只有测试通过，而且我核对预览并明确确认后，才允许执行生产迁移。"
    write([message(source)])
    store.import_source(path, "chat", 0)
    job = engine.prepare("chat", options)["job_id"]
    claim = engine.claim(job, backend=options.backend)
    result = engine.submit(claim["attempt_id"], backend=options.backend, output={"items": [{
        "type": "decision", "title": "生产迁移须经确认", "source_indexes": [0],
        "detail": "当前只做预览；执行需同时满足测试通过与用户明确确认。"}]})
    item = result["result"]["candidate_items"][0]
    assert item["title"] == "生产迁移须经确认"
    assert item["content"].startswith("当前只做预览") and item["content"] != source
    assert item["evidence"][0]["quote"] == source
    assert item["evidence"][0]["start"] == 0 and item["evidence"][0]["end"] == len(source)
    from seywo.narrative import Narrative
    service = Narrative(store)
    task = service.prepare(job)
    b = engine.claim(job, backend=options.backend)
    assert b["request"]["protocol"] == POLICY == "seywo-index-5"
    assert b["request"]["items"][0]["title"] == item["title"]
    assert b["request"]["items"][0]["text"] == item["content"]
    assert engine.submit(b["attempt_id"], backend=options.backend, output=layout(b["request"]))["error_code"] is None
    assert service.publish(task["task_id"])["narrative"]["items"][0]["title"] == item["title"]


def test_manual_title_is_versioned_locked_and_does_not_change_evidence(setup):
    store, engine, job, service, _, _, original = publish(setup)
    item = original["narrative"]["items"][0]
    changed = amend(service, original, "item", item["id"], {"title": "人工确认当前目标"})
    assert changed["narrative"]["items"][0]["title"] == "人工确认当前目标"
    assert changed["narrative"]["items"][0]["content"] == item["content"]
    assert changed["narrative"]["items"][0]["evidence"] == item["evidence"]
    assert service.read("chat", version=1)["narrative"]["items"][0]["title"] == item["title"]
    # An identical immutable batch is not reintroduced after title edits.
    task = service.prepare(job)
    claim = engine.claim(job, backend="fixture-A")
    assert not [i for i in claim["request"]["items"] if i["origin"] == "incoming"]
    assert any("title" in lock["fields"] for lock in claim["request"]["locks"])
    assert engine.submit(claim["attempt_id"], backend="fixture-A", output={})["error_code"] is None
    again = service.publish(task["task_id"])
    assert again["narrative"]["items"][0]["title"] == "人工确认当前目标"
    assert again["narrative"]["pending_revisions"] == []
    # A genuinely new A target may propose, but cannot overwrite the manual title.
    write = setup[4]
    write([message("继续核对新来源"), message("补充结果待验证", "assistant")])
    store.import_source(setup[3], "chat", 0)
    next_job = engine.prepare("chat", setup[2])["job_id"]
    drain(engine, next_job)
    service.prepare(next_job)
    claim = engine.claim(next_job, backend="fixture-A")
    output = layout(claim["request"])
    output["bindings"][0]["action"] = "revise"
    private = json.loads(store.db.execute("SELECT request_json FROM understanding_tasks WHERE id=?", (claim["task_id"],)).fetchone()[0])
    with pytest.raises(ValueError, match="manual_item_revision_conflict"):
        validate_output(hydrate(output, private), private)


def test_bad_manual_title_and_split_are_atomic(setup):
    _, _, _, service, _, _, view = publish(setup)
    identity = view["narrative"]["items"][0]["id"]
    for fields in [{"title": "字" * 41}, {"title": "换\n行"}]:
        with pytest.raises(ValueError, match="invalid_item_title"):
            amend(service, view, "item", identity, fields)
        assert service.read("chat") == view
    with pytest.raises(ValueError):
        amend(service, view, "item_split", identity, {"pieces": [
            {"title": "第一事项", "content": "第一段详情", "scope": "第一范围"},
            {"content": "第二段详情", "scope": "第二范围"}]})
    assert service.read("chat") == view


def test_legacy_snapshot_without_title_remains_read_only(setup):
    store, _, _, service, _, _, view = publish(setup)
    legacy = copy.deepcopy(view["narrative"])
    for item in legacy["items"]:
        item.pop("title")
    legacy["model_policy"] = "seywo-index-1"
    with store.db:
        import hashlib
        import zlib
        text = encode(legacy)
        store.db.execute("UPDATE narrative_revisions SET base_version=NULL,depth=0,body=?,body_hash=? WHERE chat_id=? AND version=?",
                         (zlib.compress(text.encode()), hashlib.sha256(text.encode()).hexdigest(), 'chat', 1))
        from seywo.document_store import register
        register(store.db)
    before = store.db.total_changes
    with pytest.raises(ValueError, match='invalid_item_title'):
        service.read('chat', version=1)
    assert store.db.total_changes == before


def test_title_generated_secret_is_not_persisted(setup):
    store, engine, options, *_ = setup
    job = engine.prepare("chat", options)["job_id"]
    claim = engine.claim(job, backend=options.backend)
    secret = "sk-" + "b" * 32
    result = engine.submit(claim["attempt_id"], backend=options.backend,
                           output={"items": [{"type": "goal", "title": secret, "source_indexes": [0]}]})
    assert result["error_code"] == "invalid_model_output"
    from pathlib import Path
    assert secret.encode() not in Path(store.config["_database"]).read_bytes()
