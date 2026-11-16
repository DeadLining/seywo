"""Contiguous original-source windows, shared costs, and partial-catalog authority."""

from copy import deepcopy
from dataclasses import replace
import json

import pytest

from test_s2 import setup as setup, message
from test_s3 import Narrative, layout
from seywo.model_contract import compile_request, freeze, hydrate, validate_wire
from seywo.provider import Capacity
from seywo.understanding import Limits, Options
from seywo.windows import check_visible_edits, in_window, plan_windows, row_bounds


def finish(engine, service, job):
    claim = engine.claim(job, backend="fixture-A")
    assert claim["state"] == "request"
    indexes = claim["request"]["primary_source_indexes"]
    out = {
        "items": [
            {"type": "goal", "title": "核对当前工作", "source_indexes": [indexes[0]]}
        ]
    }
    assert (
        engine.submit(
            claim["attempt_id"],
            backend="fixture-A",
            output=out,
            usage={"input_tokens": 100, "output_tokens": 40},
        )["error_code"]
        is None
    )
    task = service.prepare(job)
    claim = engine.claim(job, backend="fixture-A")
    assert claim["state"] == "request"
    assert (
        engine.submit(
            claim["attempt_id"],
            backend="fixture-A",
            output=layout(claim["request"]),
            usage={"input_tokens": 100, "output_tokens": 40},
        )["error_code"]
        is None
    )
    return service.publish(task["task_id"])


def rows(store):
    return store.db.execute(
        "SELECT * FROM current_sources ORDER BY ordinal,record_no"
    ).fetchall()


def test_window_plan_covers_every_projected_character_and_compact(setup):
    store, engine, options, path, write = setup
    write(
        [
            message("甲乙丙丁\n" * 41),
            {"type": "compacted", "payload": {"message": "summary"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "x",
                    "output": "NEVER_SEND_TOOL_BODY",
                },
            },
            message("结束"),
        ]
    )
    store.import_source(path, "chat", 0)
    source = rows(store)
    windows = plan_windows(source, 37, stop_records={(0, 3)})
    covered = {
        r["row_id"]: []
        for r in source
        if r["role"] in {"user", "assistant"} and r["kind"] == "message"
    }
    for window in windows:
        opts = replace(options, **window)
        for r in source:
            if (
                in_window(r, opts.after_order, opts.through_order, opts.after_char)
                and r["row_id"] in covered
            ):
                a, b = row_bounds(r, opts)
                covered[r["row_id"]].extend(range(a, b))
    for r in source:
        if r["row_id"] in covered:
            assert covered[r["row_id"]] == list(range(len(r["safe_text"])))
    assert any(
        w["through_order"] == (0, 3) and w["through_char"] is None for w in windows
    )
    assert windows[-1]["through_order"] == (0, 5)


def test_window_chain_uses_new_ranges_shared_budget_and_immutable_source(setup):
    store, engine, options, path, write = setup
    text = "第一部分\n第二部分\n第三部分"
    write(
        [
            message(text),
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "x",
                    "output": "NEVER_SEND_TOOL_BODY",
                },
            },
        ]
    )
    store.import_source(path, "chat", 0)
    before = path.read_bytes()
    service = Narrative(store)
    first = replace(options, through_order=(0, 2), through_char=6, bounded_history=True)
    job = engine.prepare("chat", first, Limits(call_count=4))["job_id"]
    view = finish(engine, service, job)
    child = replace(
        options,
        after_order=(0, 2),
        after_char=6,
        through_order=(0, 3),
        parent_job_id=job,
        base_version=view["version"],
        bounded_history=True,
    )
    next_job = engine.prepare("chat", child, Limits(call_count=99))["job_id"]
    assert json.loads(engine._job(next_job)["limits_json"])["call_count"] == 4
    claim = engine.claim(next_job, backend="fixture-A")
    assert claim["request"]["sources"][0]["text"] == text[6:]
    assert "NEVER_SEND_TOOL_BODY" not in json.dumps(claim["request"])
    assert engine.status(next_job)["usage"]["call_count"] == 3
    assert engine.prepare("chat", child)["reused"]
    engine.submit(
        claim["attempt_id"],
        backend="fixture-A",
        output={"items": []},
        usage={"input_tokens": 100, "output_tokens": 20},
    )
    task = service.prepare(next_job)
    claim = engine.claim(next_job, backend="fixture-A")
    engine.submit(
        claim["attempt_id"],
        backend="fixture-A",
        output={},
        usage={"input_tokens": 100, "output_tokens": 20},
    )
    service.publish(task["task_id"])
    assert engine.status(next_job)["usage"]["call_count"] == 4
    assert path.read_bytes() == before
    assert (
        engine.status(next_job)["excluded_policy"][0]["reason"]
        == "tool_response_excluded_by_policy"
    )


@pytest.mark.parametrize(
    "changes,error",
    [
        ({"after_order": (0, 2)}, "window_requires_published_parent"),
        ({"through_char": 999}, "invalid_source_window_offset"),
        ({"after_char": True}, "invalid_source_window_offset"),
        ({"through_order": (0, 999)}, "source_window_end_not_found"),
        ({"base_version": 5}, "window_narrative_base_changed"),
    ],
)
def test_invalid_window_is_transactionally_rejected(setup, changes, error):
    store, engine, options, *_ = setup
    before = store.db.execute("SELECT count(*) FROM understanding_jobs").fetchone()[0]
    with pytest.raises(ValueError, match=error):
        engine.prepare(
            "chat", replace(options, through_order=(0, 3), **changes)
        ) if "through_order" not in changes else engine.prepare(
            "chat", replace(options, **changes)
        )
    assert (
        store.db.execute("SELECT count(*) FROM understanding_jobs").fetchone()[0]
        == before
    )


def test_unpublished_or_discontinuous_parent_cannot_start_next_window(setup):
    store, engine, options, *_ = setup
    first = engine.prepare("chat", replace(options, through_order=(0, 2)))["job_id"]
    with pytest.raises(ValueError, match="invalid_window_parent"):
        engine.prepare(
            "chat",
            replace(
                options, after_order=(0, 2), through_order=(0, 3), parent_job_id=first
            ),
        )
    view = finish(engine, Narrative(store), first)
    with pytest.raises(ValueError, match="invalid_window_parent"):
        engine.prepare(
            "chat",
            replace(
                options,
                after_order=(0, 1),
                through_order=(0, 3),
                parent_job_id=first,
                base_version=view["version"],
            ),
        )


@pytest.mark.parametrize(
    "out",
    [
        {
            "bindings": [
                {
                    "item_index": 2,
                    "target_index": 1,
                    "action": "keep",
                    "reason": "guessed",
                }
            ]
        },
        {"placements": [{"item_index": 1, "phase_index": 0, "branch_index": None}]},
        {"overview": {"goal_item_indexes": [1]}},
        {"transitions": [{"unresolved": [1]}]},
        {
            "overview": {
                "unresolved_resolve": [
                    {"unresolved_index": 0, "item_indexes": [1], "reason": "guessed"}
                ]
            }
        },
    ],
)
def test_hidden_history_cannot_be_guessed_or_erased(out):
    request = {
        "visible_item_indexes": [0, 2],
        "previous": {
            "items": [{"id": "a"}, {"id": "b"}],
            "phases": [{"goal_item_ids": ["a", "b"]}],
            "branches": [],
        },
    }
    with pytest.raises(ValueError, match="hidden_item_edit_requires_readback"):
        check_visible_edits(out, request)


def test_hidden_structural_reference_is_kept_not_erased(setup):
    """A phase update names only visible items; the concealed one must survive
    hydration instead of being dropped by the subset the model could see."""
    from test_s3 import publish

    store, engine, job, service, task, claim, view = publish(setup)
    private = json.loads(
        store.db.execute("SELECT request_json FROM understanding_tasks WHERE id=?",
                         (task["task_id"],)).fetchone()[0])
    from seywo.model_contract import hydrate

    private["previous"] = deepcopy(view["narrative"])
    private["incoming"] = []
    total = len(private["previous"]["items"])
    visible = [n for n in range(total) if n % 2 == 0]
    private["visible_item_indexes"] = visible
    ids = [i["id"] for i in private["previous"]["items"]]
    hidden = [ids[n] for n in range(total) if n not in visible]
    private["previous"]["phases"][0]["goal_item_ids"] = hidden[:2]
    private.pop("indexed", None)
    private.pop("input_fingerprint", None)
    private = freeze(private)
    doc = hydrate({"phase_updates": [{"index": 0, "goal_item_indexes": [visible[0]]}]}, private)
    goals = doc["phases"][0]["goal_item_ids"]
    assert goals == [*hidden[:2], ids[visible[0]]]


def test_sparse_wire_retains_global_indexes_and_private_history(setup):
    from test_s3 import publish

    store, engine, job, service, task, claim, view = publish(setup)
    private = json.loads(
        store.db.execute(
            "SELECT request_json FROM understanding_tasks WHERE id=?",
            (task["task_id"],),
        ).fetchone()[0]
    )
    # Use actual immutable items as a historical catalog for compiler protection.
    private["previous"] = deepcopy(view["narrative"])
    private["incoming"] = []
    private["visible_item_indexes"] = [0]
    original = deepcopy(private)
    wire = compile_request(private)["wire"]
    validate_wire(wire)
    assert [i["index"] for i in wire["items"]] == [0]
    assert (
        wire["catalog_scope"]["archived_item_count"]
        == len(view["narrative"]["items"]) - 1
    )
    assert private == original
    bad = deepcopy(wire)
    bad["items"][0]["index"] = 10
    with pytest.raises(ValueError, match="invalid_catalog_scope"):
        validate_wire(bad)
    with pytest.raises(ValueError, match="hidden_item_edit_requires_readback"):
        hydrate(
            {"placements": [{"item_index": 1, "phase_index": 0, "branch_index": None}]},
            freeze(private),
        )


@pytest.mark.parametrize("dependent", [False, True])
def test_large_organization_resumes_without_losing_candidates_or_costs(
    setup, monkeypatch, dependent
):
    from seywo.understanding import Understanding

    store, engine, options, path, write = setup
    write([message("核对六个独立工作对象，并逐一保留引用。"), message("后续工作")])
    store.import_source(path, "chat", 0)
    job = engine.prepare(
        "chat", replace(options, through_order=(0, 2), bounded_history=True)
    )["job_id"]
    claim = engine.claim(job, backend="fixture-A")
    engine.submit(
        claim["attempt_id"],
        backend="fixture-A",
        output={
            "items": [
                {
                    "type": "goal",
                    "title": f"核对第{n}个对象",
                    "source_indexes": [0],
                    "dependencies": [n - 1] if dependent and n else [],
                }
                for n in range(6)
            ]
        },
    )
    actual_fit = Understanding._fits

    def bounded(self, request, opts, feedback=None):
        return actual_fit(self, request, opts, feedback) and (
            request["phase"] != "organize" or len(request["incoming"]) <= 2
        )

    monkeypatch.setattr(Understanding, "_fits", bounded)
    service = Narrative(store)
    views = []
    for part in range(3):
        task = service.prepare(job)
        claim = engine.claim(job, backend="fixture-A")
        assert claim["state"] == "request"
        wire = claim["request"]
        new = [i["index"] for i in wire["items"] if i["origin"] == "incoming"]
        assert len(new) == 2
        out = {
            "placements": [
                {"item_index": i, "phase_index": 0, "branch_index": None} for i in new
            ]
        }
        if part == 0:
            out["phases"] = [{"title": "逐一核对对象", "goal_item_indexes": [new[0]]}]
            out["overview"] = {
                "focus": {"phase_index": 0, "branch_index": None},
                "goal_item_indexes": [new[0]],
            }
        out.setdefault("overview", {})["goal_item_indexes"] = [
            i["index"] for i in wire["items"] if i["type"] == "goal"
        ]
        status = engine.submit(claim["attempt_id"], backend="fixture-A", output=out)
        assert status["error_code"] is None
        view = service.publish(task["task_id"])
        views.append(view)
        assert view["narrative"]["pending_organization_count"] == 4 - 2 * part
        assert len(view["narrative"]["applied_candidates"]) == 2 * (part + 1)
        if part < 2:
            assert view["state"] == "partial"
            with pytest.raises(
                ValueError, match="window_parent_organization_incomplete"
            ):
                engine.prepare(
                    "chat",
                    replace(
                        options,
                        after_order=(0, 2),
                        through_order=(0, 3),
                        parent_job_id=job,
                        base_version=view["version"],
                    ),
                )
    assert len(views[-1]["narrative"]["items"]) == 6
    if dependent:
        final_items = views[-1]["narrative"]["items"]
        assert all(
            item["dependencies"] == ([final_items[n - 1]["id"]] if n else [])
            for n, item in enumerate(final_items)
        )
    assert engine.status(job)["usage"]["call_count"] == 4
    assert [v["version"] for v in views] == [1, 2, 3]


def test_dependencies_are_ordered_before_dependents_for_smaller_batches():
    from seywo.windows import incoming_groups

    def item(cid, deps):
        return {"candidate_id": cid, "item": {"dependencies": deps}}

    incoming = [item("a", ["c"]), item("b", []), item("c", []), item("d", ["b"])]
    groups = incoming_groups(incoming)
    assert [[r["candidate_id"] for r in g] for g in groups] == [
        ["b"],
        ["c"],
        ["a"],
        ["d"],
    ]
    prior = set()
    for group in groups:
        assert set(group[0]["item"]["dependencies"]) <= prior
        prior.add(group[0]["candidate_id"])


def test_same_turn_items_stay_in_one_batch_and_turns_keep_dependency_order():
    from seywo.windows import incoming_groups

    def row(cid, deps=(), turn_id=None):
        return {
            "candidate_id": cid,
            "turn_id": turn_id,
            "item": {"dependencies": list(deps)},
        }

    groups = incoming_groups(
        [
            row("later-turn", ["first-turn"], "t2"),
            row("same-b", ["same-a"], "t1"),
            row("same-a", [], "t1"),
            row("first-turn", [], "t1"),
        ]
    )
    assert [sorted(r["candidate_id"] for r in group) for group in groups] == [
        ["first-turn", "same-a", "same-b"],
        ["later-turn"],
    ]


def test_b_batch_never_sees_future_unbound_changes():
    from seywo.windows import batch_sources

    source = {
        "event_id": "e",
        "snapshot_id": "s",
        "start": 0,
        "end": 12,
        "text": "甲乙丙丁戊己庚辛壬癸子丑",
        "role": "user",
        "purpose": "primary",
    }
    incoming = [
        {
            "item": {
                "evidence": [
                    {
                        "event_id": "e",
                        "snapshot_id": "s",
                        "start": 2,
                        "end": 5,
                        "quote": "丙丁戊",
                    }
                ]
            }
        }
    ]
    selected = batch_sources(incoming, [source])
    assert selected == [{**source, "start": 2, "end": 5, "text": "丙丁戊"}]
    assert source["text"] == "甲乙丙丁戊己庚辛壬癸子丑"


def test_new_window_does_not_reset_an_exhausted_chain_budget(setup):
    store, engine, options, path, write = setup
    write([message("先核对范围"), message("再核对引用"), message("最后核对发布")])
    store.import_source(path, "chat", 0)
    service = Narrative(store)
    first = engine.prepare(
        "chat", replace(options, through_order=(0, 2)), Limits(call_count=4)
    )["job_id"]
    view = finish(engine, service, first)
    second = engine.prepare(
        "chat",
        replace(
            options,
            after_order=(0, 2),
            through_order=(0, 3),
            parent_job_id=first,
            base_version=view["version"],
        ),
        Limits(call_count=50),
    )["job_id"]
    view = finish(engine, service, second)
    third = engine.prepare(
        "chat",
        replace(
            options,
            after_order=(0, 3),
            through_order=(0, 4),
            parent_job_id=second,
            base_version=view["version"],
        ),
        Limits(call_count=100),
    )["job_id"]
    assert engine.claim(third, backend="fixture-A")["state"] == "waiting_budget"
    assert engine.status(third)["usage"]["call_count"] == 4


def test_source_rewrite_invalidates_the_window_chain(setup):
    store, engine, options, path, write = setup
    service = Narrative(store)
    first = engine.prepare("chat", replace(options, through_order=(0, 2)))["job_id"]
    view = finish(engine, service, first)
    write([message("原文已改写"), message("工具结果还没有验证", "assistant")])
    store.import_source(path, "chat", 0)
    with pytest.raises(ValueError, match="invalid_window_parent"):
        engine.prepare(
            "chat",
            replace(
                options,
                after_order=(0, 2),
                through_order=(0, 3),
                parent_job_id=first,
                base_version=view["version"],
            ),
        )
    assert engine.status(first)["state"] == "stale_source"


@pytest.mark.parametrize(
    "mode",
    [
        "mainline",
        "branch",
        "manual_lock",
        "shared_request",
        "decision",
        "decision_keeps_goal",
        "standalone_action",
        "branch_goal",
    ],
)
def test_stop_choice_supports_decisions_navigation_and_human_scope(setup, mode):
    store, engine, options, path, write = setup
    write([message("先整理compact主线"), message("帮我设计一个V2版本")])
    store.import_source(path, "chat", 0)
    service = Narrative(store)
    first = engine.prepare(
        "chat", replace(options, through_order=(0, 2), bounded_history=True)
    )["job_id"]
    view = finish(engine, service, first)
    if mode == "manual_lock":
        view = service.edit(
            "chat",
            base_version=view["version"],
            override_revision=0,
            actor="human",
            patch={
                "entity": "overview",
                "id": "overview",
                "fields": {
                    "goal_item_ids": view["narrative"]["overview"]["goal_item_ids"]
                },
            },
        )
    child = engine.prepare(
        "chat",
        replace(
            options,
            after_order=(0, 2),
            through_order=(0, 3),
            parent_job_id=first,
            base_version=view["version"],
            override_revision=view["override_revision"],
            bounded_history=True,
        ),
    )["job_id"]
    claim = engine.claim(child, backend="fixture-A")
    engine.submit(
        claim["attempt_id"],
        backend="fixture-A",
        output={
            "items": [
                {
                    "type": "decision"
                    if mode in {"decision", "decision_keeps_goal"}
                    else "action"
                    if mode == "standalone_action"
                    else "goal",
                    "title": "设计V2版本",
                    "source_indexes": [0],
                }
            ]
            + (
                [{"type": "action", "title": "起草V2方案", "source_indexes": [0]}]
                if mode == "shared_request"
                else []
            )
        },
    )
    task = service.prepare(child)
    claim = engine.claim(child, backend="fixture-A")
    out = {
        "placements": [
            {
                "item_index": 1,
                "phase_index": 0,
                "branch_index": 0 if mode in {"branch", "branch_goal"} else None,
            }
        ]
    }
    if mode == "shared_request":
        out["placements"].append(
            {"item_index": 2, "phase_index": 0, "branch_index": None}
        )
    if mode in {"branch", "branch_goal"}:
        out["branches"] = [
            {
                "phase_index": 0,
                "title": "V2局部方案",
                "goal": "核对V2方案",
                "resolve_when": "明确设计后返回",
                "state": "active",
            }
        ]
        out["overview"] = {"focus": {"phase_index": 0, "branch_index": 0}}
        if mode == "branch_goal":
            out["overview"]["goal_item_indexes"] = [1]
    if mode in {"decision", "mainline", "shared_request"}:
        out["overview"] = {"goal_item_indexes": [2 if mode == "shared_request" else 1]}
    status = engine.submit(claim["attempt_id"], backend="fixture-A", output=out)
    assert status["error_code"] is None
    result = service.publish(task["task_id"])
    goals = result["narrative"]["overview"]["goal_item_ids"]
    assert (result["narrative"]["items"][1]["id"] in goals) == (
        mode in {"mainline", "decision", "branch_goal"}
    )
    if mode == "shared_request":
        assert result["narrative"]["items"][2]["id"] in goals


def test_scoped_frozen_request_survives_python_hash_seeds(setup, tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    from test_s3 import publish

    store, engine, job, service, task, claim, view = publish(setup)
    request = json.loads(
        store.db.execute(
            "SELECT request_json FROM understanding_tasks WHERE id=?",
            (task["task_id"],),
        ).fetchone()[0]
    )
    request["previous"] = deepcopy(view["narrative"])
    request["incoming"] = []
    request["visible_item_indexes"] = [0]
    # A history row contains several independently filtered fields.
    request["previous"]["transitions"] = [
        {
            "kind": "focus",
            "from_phase_id": None,
            "from_branch_id": None,
            "to_phase_id": request["previous"]["phases"][0]["id"],
            "to_branch_id": None,
            "result_item_ids": [i["id"] for i in request["previous"]["items"]],
            "unresolved": ["仍需核验"],
            "reason": "保持历史",
            "evidence_item_ids": [i["id"] for i in request["previous"]["items"]],
        }
    ]
    path = tmp_path / "private.json"
    path.write_text(json.dumps(request))
    script = "import json,sys;from seywo.model_contract import compile_request;print(json.dumps(compile_request(json.load(open(sys.argv[1]))),sort_keys=True,ensure_ascii=False))"
    root = str(Path(__file__).resolve().parents[1] / "src")
    outputs = [
        subprocess.check_output(
            [sys.executable, "-c", script, str(path)],
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": root},
        )
        for seed in ["12", "345", "6789"]
    ]
    assert len(set(outputs)) == 1
    wire = json.loads(outputs[0])["wire"]
    assert wire["recent_transitions"][0]["unresolved"] == ["仍需核验"]


@pytest.mark.parametrize("change", ["exact", "different", "user", "no_envelope"])
def test_compact_summary_alias_requires_exact_host_evidence(setup, change):
    from seywo.projection import COMPACT_HANDOFF_PREFIX
    from seywo.model_contract import model_eligible

    store, engine, options, path, write = setup
    text = "## Active task\n历史目标：设计V2；这段是交接材料，不是新的任务。"
    msg = message(text, "user" if change == "user" else "assistant")
    body = text + "。" if change == "different" else text
    compact = {
        "type": "compacted",
        "payload": {
            "message": ("" if change == "no_envelope" else COMPACT_HANDOFF_PREFIX)
            + body
        },
    }
    write([msg, compact, message("继续核对当前目标")])
    original = path.read_bytes()
    store.import_source(path, "chat", 0)
    row = store.db.execute("SELECT * FROM current_sources WHERE record_no=2").fetchone()
    assert (not model_eligible(row)) == (change == "exact")
    assert row["safe_text"] == text
    if change == "exact":
        assert row["reason"] == "compact_summary_mirror"
        assert store.search("历史目标")["matches"] == []
        job = engine.prepare("chat", replace(options, through_order=(0, 4)))["job_id"]
        claim = engine.claim(job, backend="fixture-A")
        assert text not in json.dumps(claim["request"], ensure_ascii=False)
        assert any(
            r["reason"] == "compact_summary_mirror_excluded_by_policy"
            for r in engine.status(job)["excluded_policy"]
        )
    assert path.read_bytes() == original


@pytest.mark.parametrize("reduce_stop", [True, False])
def test_one_stop_maintenance_pass_preserves_work_and_cannot_loop(
    setup, monkeypatch, reduce_stop
):
    from seywo.understanding import Understanding

    store, engine, options, path, write = setup
    write(
        [
            message("整理当前目标"),
            message("检查引用与界面", "assistant"),
            message("进入新的设计目标"),
        ]
    )
    store.import_source(path, "chat", 0)
    service = Narrative(store)
    first = engine.prepare(
        "chat", replace(options, through_order=(0, 3), bounded_history=True)
    )["job_id"]
    c = engine.claim(first, backend="fixture-A")
    engine.submit(
        c["attempt_id"],
        backend="fixture-A",
        output={
            "items": [
                {"type": "goal", "title": "整理当前目标", "source_indexes": [0]},
                {"type": "action", "title": "检查引用界面", "source_indexes": [1]},
            ]
        },
    )
    t = service.prepare(first)
    c = engine.claim(first, backend="fixture-A")
    out = layout(c["request"])
    out["phases"][0]["goal_item_indexes"] = [0, 1]
    out["overview"]["goal_item_indexes"] = [0, 1]
    assert (
        engine.submit(c["attempt_id"], backend="fixture-A", output=out)["error_code"]
        is None
    )
    view = service.publish(t["task_id"])
    child = engine.prepare(
        "chat",
        replace(
            options,
            after_order=(0, 3),
            through_order=(0, 4),
            parent_job_id=first,
            base_version=view["version"],
            bounded_history=True,
        ),
    )["job_id"]
    c = engine.claim(child, backend="fixture-A")
    engine.submit(
        c["attempt_id"],
        backend="fixture-A",
        output={
            "items": [
                {"type": "goal", "title": "进入新设计目标", "source_indexes": [0]}
            ]
        },
    )
    actual_fit = Understanding._fits

    def fit(self, request, opts, feedback=None):
        return actual_fit(self, request, opts, feedback) and (
            request["phase"] != "organize"
            or not request["incoming"]
            or len(request["previous"]["overview"]["goal_item_ids"]) <= 1
        )

    monkeypatch.setattr(Understanding, "_fits", fit)
    t = service.prepare(child)
    c = engine.claim(child, backend="fixture-A")
    assert not any(i["origin"] == "incoming" for i in c["request"]["items"])
    assert "MAINTENANCE ONLY" in c["request"]["rules"]
    out = {"overview": {"goal_item_indexes": [0]}} if reduce_stop else {}
    assert (
        engine.submit(c["attempt_id"], backend="fixture-A", output=out)["error_code"]
        is None
    )
    interim = service.publish(t["task_id"])
    assert (
        interim["state"] == "partial"
        and interim["narrative"]["pending_organization_count"] == 1
    )
    assert len(interim["narrative"]["items"]) == 2
    t = service.prepare(child)
    if not reduce_stop:
        assert t["state"] == "blocked"
        assert engine.claim(child, backend="fixture-A")["state"] == "waiting_context"
        assert engine.status(child)["usage"]["call_count"] == 4
    else:
        c = engine.claim(child, backend="fixture-A")
        assert [
            i["index"] for i in c["request"]["items"] if i["origin"] == "incoming"
        ] == [2]
        out = {
            "placements": [{"item_index": 2, "phase_index": 0, "branch_index": None}],
            "overview": {"goal_item_indexes": [2]},
        }
        assert (
            engine.submit(c["attempt_id"], backend="fixture-A", output=out)[
                "error_code"
            ]
            is None
        )
        final = service.publish(t["task_id"])
        assert final["narrative"]["pending_organization_count"] == 0
        assert len(final["narrative"]["items"]) == 3
        assert engine.status(child)["usage"]["call_count"] == 5


@pytest.mark.parametrize("copies,partial", [(2, False), (1, True)])
def test_identical_historical_source_bodies_are_factored_losslessly(
    setup, copies, partial
):
    from seywo.model_contract import measured_payload
    from seywo.provider import measure_request
    from seywo.windows import factor_copied_history, batch_sources

    store, engine, options, path, write = setup
    write(
        [
            message("核对引用设计"),
            message("引用边界必须保持一致。\n" * 60, "assistant"),
            message("继续核对引用范围"),
        ]
    )
    store.import_source(path, "chat", 0)
    service = Narrative(store)
    first = engine.prepare(
        "chat", replace(options, through_order=(0, 3), bounded_history=True)
    )["job_id"]
    c = engine.claim(first, backend="fixture-A")
    engine.submit(
        c["attempt_id"],
        backend="fixture-A",
        output={
            "items": [
                {"type": "goal", "title": "核对引用设计", "source_indexes": [0]},
                {"type": "option", "title": "核对引用边界", "source_indexes": [1]},
                *(
                    [{"type": "option", "title": "核对引用范围", "source_indexes": [1]}]
                    if copies == 2
                    else []
                ),
            ]
        },
    )
    t = service.prepare(first)
    c = engine.claim(first, backend="fixture-A")
    assert (
        engine.submit(
            c["attempt_id"], backend="fixture-A", output=layout(c["request"])
        )["error_code"]
        is None
    )
    view = service.publish(t["task_id"])
    child = engine.prepare(
        "chat",
        replace(
            options,
            after_order=(0, 3),
            through_order=(0, 4),
            parent_job_id=first,
            base_version=view["version"],
            bounded_history=True,
        ),
    )["job_id"]
    c = engine.claim(child, backend="fixture-A")
    engine.submit(
        c["attempt_id"],
        backend="fixture-A",
        output={
            "items": [{"type": "goal", "title": "核对引用范围", "source_indexes": [0]}]
        },
    )
    t = service.prepare(child)
    private = json.loads(
        store.db.execute(
            "SELECT request_json FROM understanding_tasks WHERE id=?", (t["task_id"],)
        ).fetchone()[0]
    )
    private.pop("indexed")
    private["visible_item_indexes"] = list(range(copies + 2))
    private["sources"] = batch_sources(private["incoming"], private["sources"])
    if partial:
        from seywo.understanding import Understanding

        ev = private["previous"]["items"][1]["evidence"][0]
        source = store.db.execute(
            "SELECT * FROM current_sources WHERE event_id=? AND snapshot_id=?",
            (ev["event_id"], ev["snapshot_id"]),
        ).fetchone()
        midpoint = (ev["start"] + ev["end"]) // 2
        private["sources"].append(
            Understanding._source(source, ev["start"], midpoint, "context")
        )
    before = measure_request(measured_payload(private))["tokens"]
    saved = deepcopy(private["previous"]["items"])
    factor_copied_history(private, store.db)
    # With a bridge or a larger exact primary already present, factoring can be
    # break-even in tokens while still removing duplicate copied bodies.
    assert measure_request(measured_payload(private))["tokens"] <= before
    wire = compile_request(private)["wire"]
    assert all(
        i["text"] == ""
        for i in wire["items"]
        if i["index"] in set(range(1, copies + 1))
    )
    assert (
        len([s for s in private["sources"] if s["purpose"] == "evidence_readback"]) == 1
    )
    assert private["previous"]["items"] == saved
    if partial:
        readback = next(
            s for s in private["sources"] if s["purpose"] == "evidence_readback"
        )
        assert (readback["start"], readback["end"]) == (midpoint, ev["end"])
        assert readback["text"] == source["safe_text"][midpoint : ev["end"]]


def test_latest_work_visibility_pins_focused_phase_only():
    from seywo.windows import latest_phase_goals

    request = {
        "previous": {
            "items": [{"id": "old"}, {"id": "current"}, {"id": "side"}],
            "placements": [
                {"item_id": "old", "phase_id": "past", "branch_id": None},
                {"item_id": "current", "phase_id": "now", "branch_id": None},
                {"item_id": "side", "phase_id": "now", "branch_id": "side"},
            ],
            "overview": {"focus": {"phase_id": "now", "branch_id": None}},
        },
        "user_goal_order": [
            {"item_index": 0, "order": [0, 2, 0, 20]},
            {"item_index": 1, "order": [0, 8, 0, 20]},
            {"item_index": 2, "order": [0, 9, 0, 20]},
        ],
    }
    assert latest_phase_goals(request) == [1]
    assert request["previous"]["items"][0]["id"] == "old"


def test_organization_policy_upgrade_retires_inflight_b_without_resetting_spend(
    setup, monkeypatch
):
    import seywo.narrative as narrative_module

    store, engine, options, *_ = setup
    service = Narrative(store)
    job = engine.prepare("chat", options)["job_id"]
    from test_s2 import drain

    drain(engine, job)
    first = service.prepare(job)
    lease = engine.claim(job, backend=options.backend)
    assert lease["request"]["phase"] == "organize"
    spent = engine.status(job)["usage"]
    monkeypatch.setattr(narrative_module, "ORGANIZATION_POLICY", "test-next-policy")
    replacement = service.prepare(job)
    assert replacement["task_id"] != first["task_id"]
    assert service.prepare(job)["task_id"] == replacement["task_id"]
    assert engine.status(job)["usage"] == spent
    assert (
        store.db.execute(
            "SELECT state FROM understanding_tasks WHERE id=?", (first["task_id"],)
        ).fetchone()[0]
        == "stale"
    )
    assert (
        store.db.execute(
            "SELECT state FROM understanding_attempts WHERE id=?",
            (lease["attempt_id"],),
        ).fetchone()[0]
        == "superseded"
    )
    assert (
        store.db.execute(
            "SELECT count(DISTINCT owner_id) FROM model_budget_owners"
        ).fetchone()[0]
        == 1
    )


def test_legacy_rows_without_turn_keep_character_fallback_and_no_bridge(setup):
    store, _, _, path, write = setup
    text = "旧来源开头" + "甲" * 100 + "\n" + "乙" * 100 + "\n旧来源结尾"
    write([message(text)])
    store.import_source(path, "chat", 0)
    source = rows(store)
    windows = plan_windows(source, 70)
    assert len(windows) > 1
    assert all(not window["bridge"] for window in windows)
    options = Options("fixture-A", "test-model-v1", Capacity(64000, 32000, 8000))
    covered = []
    for window in windows:
        opts = replace(options, **window)
        for row in source:
            if in_window(row, opts.after_order, opts.through_order, opts.after_char):
                start, end = row_bounds(row, opts)
                covered.extend(range(start, end))
    assert covered == list(range(len(text)))


def turn(turn_id, root=None):
    return {
        "type": "turn_context",
        "payload": {"turn_id": turn_id, "root_turn_id": root or turn_id},
    }


def tool_output(text="NEVER_SEND_TOOL_BODY"):
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "call_id": "call-" + str(len(text)),
            "output": text,
        },
    }


def test_native_turn_identity_is_inherited_but_mechanical_rows_stay_excluded(setup):
    store, _, _, path, write = setup
    write(
        [
            turn("t1", "root"),
            message("第一轮用户"),
            tool_output(),
            message("第一轮助手", "assistant"),
            turn("t2", "root"),
            message("第二轮用户"),
        ]
    )
    store.import_source(path, "chat", 0)
    source = rows(store)
    by_record = {row["record_no"]: row for row in source}
    assert {by_record[n]["turn_id"] for n in (3, 4, 5)} == {"t1"}
    assert by_record[3]["turn_seq"] == by_record[5]["turn_seq"] == 1
    assert by_record[4]["turn_seq"] == 1 and by_record[4]["role"] == "tool"
    assert by_record[7]["turn_id"] == "t2" and by_record[7]["turn_seq"] == 2
    assert all(row["root_turn_id"] == "root" for row in source[2:])
    assert by_record[1]["turn_seq"] is None  # session_meta precedes turn_context


def test_native_turn_continues_across_ordered_segments(setup):
    store, _, _, path, write = setup
    write([turn("t1", "root"), message("第一段")])
    store.import_source(path, "chat", 0)
    second = path.with_name("second.jsonl")
    second.write_bytes(
        b"".join(
            (json.dumps(record, ensure_ascii=False) + "\n").encode()
            for record in [
                {"type": "session_meta", "payload": {"id": "chat", "cwd": str(path.parent)}},
                turn("t1", "root"),
                message("第二段"),
                turn("t2", "root"),
                message("新轮次"),
            ]
        )
    )
    store.import_source(second, "chat", 1)
    source = rows(store)
    assert [
        (row["ordinal"], row["record_no"], row["kind"], row["turn_id"], row["turn_seq"])
        for row in source if row["turn_id"]
    ] == [
        (0, 2, "turn_context", "t1", 1),
        (0, 3, "message", "t1", 1),
        (1, 1, "session_meta", "t1", 1),
        (1, 2, "turn_context", "t1", 1),
        (1, 3, "message", "t1", 1),
        (1, 4, "turn_context", "t2", 2),
        (1, 5, "message", "t2", 2),
    ]
    windows = plan_windows(source, 1000)
    assert len(windows) == 1


def test_repeated_turn_context_does_not_create_a_new_turn(setup):
    store, _, _, path, write = setup
    write(
        [
            turn("t1", "root"),
            message("压缩前"),
            message("压缩摘要", "assistant"),
            turn("t1", "root"),
            message("压缩后"),
        ]
    )
    store.import_source(path, "chat", 0)
    source = rows(store)
    turns = [row["turn_seq"] for row in source if row["turn_id"]]
    assert turns == [1, 1, 1, 1, 1]
    windows = plan_windows(source, 1000)
    assert len(windows) == 1


def test_small_native_turns_stay_in_the_same_window(setup):
    store, _, _, path, write = setup
    write(
        [
            turn("t1"),
            message("第一项工作"),
            message("第一项说明", "assistant"),
            turn("t2"),
            message("第二项工作"),
            message("第二项说明", "assistant"),
        ]
    )
    store.import_source(path, "chat", 0)
    windows = plan_windows(rows(store), 1000)
    assert len(windows) == 1
    assert windows[0]["through_order"] == (0, 7)


def test_oversized_turn_splits_and_bridges_user_opening_without_tool_body(setup):
    store, _, _, path, write = setup
    text = "用户开头" + "甲" * 120 + "\n" + "乙" * 120 + "\n" + "结尾"
    write([turn("t1"), message(text), tool_output("TOOL-SHOULD-NOT-BRIDGE")])
    store.import_source(path, "chat", 0)
    source = rows(store)
    windows = plan_windows(source, 90)
    assert len(windows) > 1
    first = windows[0]
    second = windows[1]
    assert first["through_char"] is not None
    assert second["bridge"]
    ordinal, record, start, end = second["bridge"][0]
    row = next(
        r for r in source if (r["ordinal"], r["record_no"]) == (ordinal, record)
    )
    assert row["kind"] == "message" and start == 0
    assert row["safe_text"][start:end].startswith("用户开头")
    assert all(
        "TOOL-SHOULD-NOT-BRIDGE"
        not in next(
            r["safe_text"]
            for r in source
            if (r["ordinal"], r["record_no"]) == tuple(span[:2])
        )
        for span in second["bridge"]
    )


def test_extract_bridge_is_context_and_rejects_context_only_item(setup):
    store, engine, options, path, write = setup
    user = "开头" + "甲" * 90 + "\n" + "乙" * 90 + "\n" + "结尾"
    write([turn("t1"), message(user)])
    store.import_source(path, "chat", 0)
    source = rows(store)
    windows = plan_windows(source, 80)
    first, needed = windows[0], next(w for w in windows[1:] if w["bridge"])
    parent = engine.prepare(
        "chat",
        replace(
            options,
            through_order=tuple(first["through_order"]),
            through_char=first["through_char"],
        ),
        Limits(call_count=4),
    )["job_id"]
    claim = engine.claim(parent, backend="fixture-A")
    engine.submit(
        claim["attempt_id"],
        backend="fixture-A",
        output={
            "items": [
                {"type": "goal", "title": "开始本轮", "source_indexes": [0]}
            ]
        },
    )
    service = Narrative(store)
    task = service.prepare(parent)
    organize_claim = engine.claim(parent, backend="fixture-A")
    assert organize_claim["request"]["phase"] == "organize"
    organized = engine.submit(
        organize_claim["attempt_id"],
        backend="fixture-A",
        output=layout(organize_claim["request"]),
    )
    assert organized["error_code"] is None
    view = service.publish(task["task_id"])
    job = engine.prepare(
        "chat",
        replace(
            options,
            through_order=tuple(needed["through_order"]),
            through_char=needed["through_char"],
            after_order=tuple(needed["after_order"]),
            after_char=needed["after_char"],
            bridge=tuple(tuple(x) for x in needed["bridge"]),
            parent_job_id=parent,
            base_version=view["version"],
        ),
        Limits(call_count=8),
    )["job_id"]
    claim = engine.claim(job, backend="fixture-A")
    assert claim["state"] == "request"
    request = claim["request"]
    context = [
        n for n, row in enumerate(request["sources"]) if row["purpose"] == "context"
    ]
    primary = request["primary_source_indexes"]
    assert context and not set(context) & set(primary)
    assert request["sources"][context[0]]["text"].startswith("开头")
    assert request["sources"][primary[0]]["turn_continuation"] == "continued"
    bad = engine.submit(
        claim["attempt_id"],
        backend="fixture-A",
        output={
            "items": [
                {"type": "goal", "title": "仅上下文", "source_indexes": [context[0]]}
            ]
        },
    )
    assert bad["error_code"] == "invalid_model_output"
    claim = engine.claim(job, backend="fixture-A")
    ok = engine.submit(
        claim["attempt_id"],
        backend="fixture-A",
        output={
            "items": [
                {"type": "goal", "title": "继续当前轮", "source_indexes": [primary[0]]}
            ]
        },
    )
    assert ok["error_code"] is None


def test_bridge_is_context_only_and_coverage_starts_after_it(setup):
    store, engine, options, path, write = setup
    user = "开头" + "甲" * 90 + "\n" + "乙" * 90 + "\n" + "结尾"
    write([turn("t1"), message(user), message("助手收尾", "assistant")])
    store.import_source(path, "chat", 0)
    source = rows(store)
    windows = plan_windows(source, 80)
    first, needed = windows[0], next(w for w in windows[1:] if w["bridge"])
    service = Narrative(store)
    parent = engine.prepare(
        "chat",
        replace(options, through_order=tuple(first["through_order"]),
                through_char=first["through_char"], bounded_history=True),
        Limits(call_count=6),
    )["job_id"]
    view = finish(engine, service, parent)
    child = replace(
        options,
        after_order=tuple(needed["after_order"]),
        after_char=needed["after_char"],
        through_order=tuple(needed["through_order"]),
        through_char=needed["through_char"],
        parent_job_id=parent,
        base_version=view["version"],
        bounded_history=True,
        bridge=tuple(tuple(x) for x in needed["bridge"]),
    )
    job = engine.prepare("chat", child, Limits(call_count=6))["job_id"]
    claim = engine.claim(job, backend="fixture-A")
    request = claim["request"]
    bridged = [i for i, row in enumerate(request["sources"]) if row["purpose"] == "context"]
    assert bridged
    assert all(i not in request["primary_source_indexes"] for i in bridged)
    assert request["sources"][bridged[0]]["text"].startswith("开头")
    primary = request["sources"][request["primary_source_indexes"][0]]
    assert primary["turn_continuation"] == "continued"
    assert primary["text"] == user[needed["after_char"]:][: len(primary["text"])]
    submitted = engine.submit(
        claim["attempt_id"],
        backend="fixture-A",
        output={
            "items": [
                {
                    "type": "goal",
                    "title": "继续当前轮",
                    "source_indexes": request["primary_source_indexes"][:1],
                }
            ]
        },
    )
    assert submitted["error_code"] is None
    task = service.prepare(job)
    private = json.loads(
        store.db.execute(
            "SELECT request_json FROM understanding_tasks WHERE id=?", (task["task_id"],)
        ).fetchone()[0]
    )
    indexed = freeze({k: v for k, v in private.items() if k != "indexed"})
    # The B catalog also accepts item indexes internally; an empty delta is valid.
    assert hydrate({}, indexed)
    assert indexed["indexed"]["sources"][bridged[0]]["purpose"] == "context"


def test_gaps_collapse_to_one_row_per_reason():
    from seywo.windows import collapse_gaps

    gaps = [{"event_id": "a", "reason": "unsupported_event"},
            {"event_id": "b", "reason": "unsupported_event"},
            {"event_id": "c", "reason": "compact_reference_not_original"}]
    assert collapse_gaps(gaps) == [
        {"reason": "compact_reference_not_original", "count": 1},
        {"reason": "unsupported_event", "count": 2},
    ]


def test_retry_hint_is_dropped_when_it_alone_overflows(setup):
    """A task that fits exactly at the ceiling must still be retryable: the
    repair hint is optional context, so dropping it beats parking the window."""
    store, engine, options, *_ = setup
    job = engine.prepare("chat", options)["job_id"]
    while True:
        claim = engine.claim(job, backend="fixture-A")
        if claim["state"] != "request":
            break
        indexes = claim["request"]["primary_source_indexes"]
        engine.submit(claim["attempt_id"], backend="fixture-A", finish="complete",
                      usage={"input_tokens": 1, "output_tokens": 1},
                      output={"items": [{"type": "observation", "title": "核对事项",
                                         "source_indexes": [indexes[0]]}]})
    task = store.db.execute("SELECT id FROM understanding_tasks WHERE job_id=? AND phase='extract' "
                            "ORDER BY ordinal DESC LIMIT 1", (job,)).fetchone()[0]
    with store.db:
        store.db.execute("UPDATE understanding_tasks SET state='pending' WHERE id=?", (task,))
        store.db.execute("UPDATE understanding_jobs SET state='pending',limits_json=? WHERE id=?",
                         (json.dumps({"total_input_tokens": 900000, "total_output_tokens": 300000,
                                      "call_count": 100, "wall_seconds": 10 ** 6,
                                      "retry_limit": 20}), job))
        store.db.execute(
            "INSERT INTO understanding_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("a" * 32, task, job, 0.0, "failed", "fixture-A", 1, 1, 0.0, "estimated", None,
             "invalid_model_output:hidden_item_edit_requires_readback", 0.0))
    real = type(engine)._fits
    type(engine)._fits = lambda self, request, opts, feedback=None: feedback is None
    try:
        again = engine.claim(job, backend="fixture-A")
    finally:
        type(engine)._fits = real
    assert again["state"] == "request"
    assert "retry_feedback" not in again["request"]
