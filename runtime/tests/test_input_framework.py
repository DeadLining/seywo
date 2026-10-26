import json
from sessiongraph.core import Store, init_project, load_config
from sessiongraph.graph_v2 import run_chat, setup
from test_graph_v2 import Judge


def fixture(tmp_path, payloads):
    config = load_config(init_project(tmp_path))
    path = tmp_path / "chat.jsonl"
    events = [{"type": "session_meta", "payload": {"id": "input-chat", "cwd": str(tmp_path)}}]
    events += [
        {"type": "response_item", "timestamp": f"2026-10-14T00:00:{i:02d}Z", "payload": p}
        for i, p in enumerate(payloads)
    ]
    path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in events))
    store = Store(config)
    store.ingest(path)
    setup(store)
    return store, path


def msg(text, role="assistant", channel="final"):
    return {
        "type": "message",
        "role": role,
        "channel": channel,
        "content": [{"type": "output_text", "text": text}],
    }


class Empty:
    def __init__(self):
        self.calls = []

    def __call__(self, batch, policy):
        from sessiongraph.graph_contract import VERSION

        self.calls.append(batch)
        return {
            "schema_version": VERSION,
            "nodes": [],
            "relations": [],
            "unresolved": [],
            "coverage": {"entry_ids": [x["id"] for x in batch], "outcome": "no_durable_item"},
        }


def test_channels_excluded_and_audited_with_no_model(tmp_path):
    s, _ = fixture(tmp_path, [msg("private speculation", channel="analysis")])
    ex = Empty()
    r = run_chat(s, ex, "input-chat", jev=Judge())
    assert r["model_calls"] == 0 and not ex.calls
    assert not r["graph_v2"]["source_remaining"]
    assert (
        s.db.execute(
            "select count(*) from sg_input_coverage where disposition='excluded'"
        ).fetchone()[0]
        == 1
    )


def test_tool_evidence_and_short_user_confirmation_remain(tmp_path):
    s, _ = fixture(
        tmp_path,
        [
            msg("运行实验 A", "user"),
            {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "c",
                "arguments": json.dumps({"cmd": "python train.py " + "x" * 8000}),
            },
            {
                "type": "function_call_output",
                "call_id": "c",
                "output": "progress 1%\n" * 3000
                + "run_id=run_042 accuracy=0.913 artifact=/tmp/metrics.json\n",
            },
            msg("结果已生成"),
            msg("是的", "user"),
        ],
    )
    ex = Empty()
    r = run_chat(s, ex, "input-chat", jev=Judge(), max_chars=24000)
    text = "\n".join(e["text"] for b in ex.calls for e in b)
    assert not r["failed"], r
    assert all(x in text for x in ["run_042", "0.913", "/tmp/metrics.json", "是的"])
    assert len(text) < 5000
    assert r["graph_v2"]["input_excluded_chars"] > 20000


def test_verified_byte_locator_detects_mutation_and_missing(tmp_path):
    from sessiongraph.source_locator import read_original

    s, path = fixture(tmp_path, [msg("中文结果🙂")])
    eid = s.db.execute("select id from entries").fetchone()[0]
    r = read_original(s, eid)
    assert r["status"] == "verified" and r["text"] == "中文结果🙂"
    path.write_text(path.read_text().replace("中文结果🙂", "伪造结果🙂"))
    assert read_original(s, eid)["status"] == "changed"
    path.unlink()
    assert read_original(s, eid)["status"] == "missing"


def test_old_database_backfills_channel_without_reimport(tmp_path):
    s, _ = fixture(tmp_path, [msg("analysis secret", channel="analysis"), msg("有效结论")])
    s.db.execute("delete from sg_source_locations")
    s.db.commit()
    ex = Empty()
    r = run_chat(s, ex, "input-chat", jev=Judge())
    assert not r["failed"]
    assert "analysis secret" not in "\n".join(e["text"] for b in ex.calls for e in b)


def test_latest_first_keeps_old_backlog_and_replay_zero(tmp_path):
    s, _ = fixture(
        tmp_path, [msg("旧任务", "user"), msg("旧结果"), msg("新任务", "user"), msg("新结果")]
    )
    ex = Empty()
    r = run_chat(s, ex, "input-chat", jev=Judge(), step_budget=1)
    assert not r["failed"], r
    assert any("新结果" in e["text"] for e in ex.calls[0])
    assert r["graph_v2"]["source_remaining"]
    cursor = s.db.execute("select line from sg2_cursors").fetchone()
    assert cursor is None or cursor[0] < 4
    r2 = run_chat(s, ex, "input-chat", jev=Judge())
    assert not r2["graph_v2"]["source_remaining"]
    assert run_chat(s, Empty(), "input-chat", jev=Judge())["model_calls"] == 0


def test_unknown_large_output_is_not_silently_dropped(tmp_path):
    s, _ = fixture(
        tmp_path,
        [
            {
                "type": "function_call_output",
                "call_id": "unknown",
                "output": "unknown payload " * 4000,
            }
        ],
    )
    ex = Empty()
    r = run_chat(s, ex, "input-chat", jev=Judge(), step_budget=1, max_chars=1000)
    assert r["graph_v2"]["source_remaining"]
    assert len(ex.calls[0][0]["text"]) == 1000


def test_projection_anchors_are_exact_original_substrings(tmp_path):
    from sessiongraph.graph_contract import prepare_evidence_spans, policy

    s, _ = fixture(
        tmp_path,
        [
            {
                "type": "function_call_output",
                "call_id": "x",
                "output": "start\n" + "progress 1%\n" * 2000 + "accuracy=0.95\n",
            }
        ],
    )
    ex = Empty()
    run_chat(s, ex, "input-chat", jev=Judge())
    originals = {r["id"]: r["text"] for r in s.db.execute("select * from entries")}
    for batch in ex.calls:
        _, _, anchors = prepare_evidence_spans(batch, policy([]))
        assert all(a["quote"] in originals[a["entry_id"]] for a in anchors.values())


def test_precompact_only_imports_and_duplicate_post_does_not_extract(tmp_path):
    from sessiongraph.auto_update import run_update

    cfg = init_project(tmp_path)
    data = json.loads(cfg.read_text())
    data["graph_v2"] = {"enabled": True}
    cfg.write_text(json.dumps(data))
    Store(load_config(cfg)).close()
    calls = []

    def execute(argv, timeout):
        calls.append(argv)
        if "refresh-chat" in argv:
            return {"added_entries": 0, "skipped": []}
        return {
            "saved": [],
            "failed": [],
            "model_calls": 1,
            "graph_v2": {"deepseek_calls": 1, "source_remaining": False},
        }

    first = run_update(cfg, "input-chat", "PreCompact", execute=execute)
    assert first["state"] == "prepared" and len(calls) == 1
    post = run_update(cfg, "input-chat", "PostCompact", execute=execute)
    worker = next(x for x in calls if 'sessiongraph.gateway_worker' in x)
    assert '--memories-call-budget' not in worker
    assert post["state"] == "succeeded" and post["reported_model_calls"] == 1
    again = run_update(cfg, "input-chat", "PostCompact", execute=execute)
    assert again["state"] == "coalesced" and again["reported_model_calls"] == 0
    assert len([x for x in calls if "sessiongraph.gateway_worker" in x]) == 1


def test_model_budget_interruption_reuses_projected_source(tmp_path):
    from test_graph_v2 import output
    from test_memories import FakeExtractor

    s, _ = fixture(tmp_path, [msg("训练 A 得到模型 B")])
    rows = [dict(x) for x in s.db.execute("select * from entries")]
    first = run_chat(s, FakeExtractor([output(rows)]), "input-chat", jev=Judge(), call_budget=1)
    assert first["model_calls"] == 1 and first["failed"]
    assert s.db.execute("select count(*) from sg_input_coverage").fetchone()[0] == 0
    second = run_chat(s, FakeExtractor([]), "input-chat", jev=Judge(), call_budget=1)
    assert not second["failed"] and len(second["saved_relations"]) == 1
    assert second["graph_v2"]["deepseek_calls"] == 0
    assert not second["graph_v2"]["source_remaining"]


def test_projection_split_never_reintroduces_noise(tmp_path):
    from sessiongraph import input_pipeline
    from sessiongraph.graph_v2 import _split_job, _restore_batch, _stats

    s, _ = fixture(
        tmp_path,
        [
            {
                "type": "function_call_output",
                "call_id": "x",
                "output": "A" * 800 + "\n" + "progress 5%\n" * 4000 + "B" * 800,
            }
        ],
    )
    p = input_pipeline.plan(s, "input-chat", 0, 0, 24000)
    desc = {**p, "entries": [input_pipeline.describe(e) for e in p["entries"]]}
    s.db.execute(
        "INSERT INTO sg2_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "job",
            s.config["project_id"],
            "input-chat",
            "input-chat",
            "pending",
            json.dumps(desc),
            None,
            "[]",
            "{}",
            None,
            "",
            "",
        ),
    )
    s.db.commit()
    job = dict(s.db.execute("select * from sg2_jobs where id='job'").fetchone())
    assert _split_job(s, job, desc, _stats(), "provider_output_limit")
    split = json.loads(s.db.execute("select input_json from sg2_jobs where id='job'").fetchone()[0])
    allparts = [split["entries"]] + [r["entries"] for r in split["remaining"]]
    text = "".join(e["text"] for part in allparts for e in _restore_batch(s, part))
    assert "progress" not in text and text.count("A") == 800 and text.count("B") == 800


def test_reversed_relation_requires_judge_support(tmp_path):
    from test_graph_v2 import output
    from test_memories import FakeExtractor

    s, _ = fixture(tmp_path, [msg("训练 A 得到模型 B")])
    rows = [dict(r) for r in s.db.execute("select * from entries")]
    value = output(rows)
    edge = value["relations"][0]
    edge["source"], edge["target"] = edge["target"], edge["source"]
    r = run_chat(s, FakeExtractor([value]), "input-chat", jev=Judge())
    assert not r["failed"] and len(r["saved_relations"]) == 1
    judgment = json.loads(s.db.execute("select judgment_json from sg2_relations").fetchone()[0])
    assert judgment["direction_repair"]["source"] == "n2"


def test_reversed_relation_without_support_stays_unresolved(tmp_path):
    from test_graph_v2 import output
    from test_memories import FakeExtractor

    s, _ = fixture(tmp_path, [msg("训练 A 和模型 B，仅列出名字")])
    value = output([dict(r) for r in s.db.execute("select * from entries")])
    edge = value["relations"][0]
    edge["source"], edge["target"] = edge["target"], edge["source"]
    r = run_chat(
        s, FakeExtractor([value]), "input-chat", jev=Judge(support="insufficient_evidence")
    )
    assert not r["failed"] and not r["saved_relations"]
    assert s.db.execute("select count(*) from sg2_unresolved").fetchone()[0] > 0


def test_code_reads_do_not_discard_mixed_test_results(tmp_path):
    from sessiongraph.input_pipeline import projection

    payloads = []
    for i, cmd in enumerate(
        [
            "sed -n 1,100p file.py",
            "sed -n 1,100p file.py; tail -10 test.log",
            "cat report.json",
            "python train.py",
        ]
    ):
        payloads += [
            {
                "type": "function_call",
                "name": "exec_command",
                "call_id": str(i),
                "arguments": json.dumps({"cmd": cmd}),
            },
            {
                "type": "function_call_output",
                "call_id": str(i),
                "output": "Process exited with code 0\n418 passed in 21.93s\n",
            },
        ]
    s, _ = fixture(tmp_path, payloads)
    rows = [
        dict(r)
        for r in s.db.execute("select * from entries where kind='tool_result' order by line")
    ]
    spans, reason = projection(s, rows[0])
    assert reason == "code_read_context"
    for r in rows[1:]:
        spans, _ = projection(s, r)
        assert "418 passed" in "".join(r["text"][a:b] for a, b in spans)


def test_failed_legacy_input_replans_without_erasing_failure(tmp_path):
    from sessiongraph.core import digest

    s, _ = fixture(tmp_path, [msg("internal-only", channel="analysis")])
    row = s.db.execute("select * from entries").fetchone()
    desc = {
        "entries": [
            {
                "id": row["id"],
                "source_start": 0,
                "source_end": len(row["text"]),
                "source_hash": digest(row["text"]),
            }
        ],
        "end": [row["line"] + 1, 0],
    }
    with s.db:
        s.db.execute(
            "INSERT INTO sg2_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "old",
                s.config["project_id"],
                "input-chat",
                "input-chat",
                "pending",
                json.dumps(desc),
                None,
                "[]",
                "{}",
                "graph_validation",
                "",
                "",
            ),
        )
    r = run_chat(s, Empty(), "input-chat", jev=Judge())
    assert not r["failed"] and r["model_calls"] == 0 and not r["graph_v2"]["source_remaining"]
    old = s.db.execute("select state,error from sg2_jobs where id='old'").fetchone()
    assert tuple(old) == ("superseded", "graph_validation")


def test_environment_prefix_cannot_swallow_user_request(tmp_path):
    s, _ = fixture(
        tmp_path,
        [msg("<environment_context>cwd=x</environment_context>\n请保留实验 run_9", "user")],
    )
    ex = Empty()
    run_chat(s, ex, "input-chat", jev=Judge())
    assert any("run_9" in e["text"] for b in ex.calls for e in b)
