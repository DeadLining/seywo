import concurrent.futures
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from seywo import config
from seywo.config import initialize, load_config
from seywo.provider import Capacity, measure_request, reserve_request
from seywo.store import Store


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    initialize(root)
    return root, load_config(root / ".seywo/config.json")


def message(text, role="user", channel=None):
    return {"type": "response_item", "payload": {"type": "message", "role": role,
            "channel": channel, "content": [{"type": "input_text", "text": text}]}}


def tool(kind, call_id="call-1", **kwargs):
    return {"type": "response_item", "payload": {"type": kind, "call_id": call_id, **kwargs}}


def write_source(path, root, records, chat="chat-1", suffix=b""):
    events = [{"type": "session_meta", "payload": {"id": chat, "cwd": str(root)}}, *records]
    path.write_bytes(b"".join((json.dumps(r, ensure_ascii=False) + "\n").encode() for r in events) + suffix)
    return path


def import_records(project, records, chat="chat-1", name="source.jsonl"):
    root, cfg = project
    path = write_source(root / name, root, records, chat)
    with Store(cfg) as store:
        result = store.import_source(path, chat, 0)
    return path, result


def test_init_is_independent_idempotent_and_git_ignores_nested_data(tmp_path):
    root = tmp_path / "project"
    first = initialize(root)
    cfg = root / ".seywo/config.json"
    original = cfg.read_bytes()
    assert initialize(root)["project_id"] == first["project_id"]
    assert cfg.read_bytes() == original
    assert (root / ".seywo/.gitignore").read_text() == "*\n!.gitignore\n"
    assert not (root / ".sessiongraph").exists()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    nested = root / ".seywo/cache/secret.txt"
    nested.parent.mkdir()
    nested.write_text("private")
    for path in [".seywo/config.json", ".seywo/index.sqlite3", ".seywo/cache/secret.txt"]:
        assert subprocess.run(["git", "-C", str(root), "check-ignore", "-q", path]).returncode == 0
    assert subprocess.run(["git", "-C", str(root), "check-ignore", "-q", ".seywo/.gitignore"]).returncode == 1
    status = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"], text=True)
    assert status.strip() == "?? .seywo/.gitignore"


def test_gitignore_preserves_existing_exceptions_and_repairs_missing_file(project):
    root, _ = project
    path = root / ".seywo/.gitignore"
    path.write_text("# custom\n!share.json\n")
    initialize(root)
    content = path.read_text()
    initialize(root)
    assert path.read_text() == content
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    assert subprocess.run(["git", "-C", str(root), "check-ignore", "-q", ".seywo/share.json"]).returncode == 1
    path.unlink()
    initialize(root)
    assert path.read_text() == "*\n!.gitignore\n"


def test_ignore_failure_precedes_config_or_database(tmp_path, monkeypatch):
    def fail(_):
        raise PermissionError("fixture")
    monkeypatch.setattr(config, "ensure_ignore", fail)
    with pytest.raises(PermissionError):
        initialize(tmp_path)
    assert not (tmp_path / ".seywo/config.json").exists()
    assert not (tmp_path / ".seywo/index.sqlite3").exists()


def test_parallel_initialization_has_one_project_identity(tmp_path):
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: initialize(tmp_path), range(4)))
    assert len({r["project_id"] for r in results}) == 1


def test_symlinked_state_and_configuration_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / ".seywo").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        initialize(root)
    assert list(outside.iterdir()) == []


def test_source_coverage_unicode_location_and_tool_pairing(project):
    root, cfg = project
    path, imported = import_records(project, [
        message("初始化中文索引🙂"), message("开始检查", "assistant", "commentary"),
        tool("function_call", name="exec", arguments='{"cmd":"pytest"}'),
        tool("function_call_output", output="exit code 0; 1 test failed"),
        {"type": "event_msg", "payload": {"type": "user_message", "message": "镜像"}},
        {"type": "response_item", "payload": {"type": "reasoning", "summary": "hidden"}},
        {"type": "compacted", "replacement_history": ["not original"]},
    ])
    with Store(cfg) as store:
        events = store.list_events("chat-1")["events"]
        item = store.read(events[1]["event_id"], start=3, end=5)
        assert item["text"] == "中文"
        raw = path.read_bytes()[item["source"]["byte_start"]:item["source"]["byte_end"]]
        assert hashlib.sha256(raw).hexdigest() == item["source"]["raw_sha256"]
        assert json.loads(raw)["payload"]["content"][0]["text"] == "初始化中文索引🙂"
        assert item["projection"]["text_fields"][0]["json_path"] == "payload.content[0].text"
        assert {"turn_id", "root_turn_id", "turn_seq"} <= set(item)
        assert "text" not in item["projection"]
        assert store.read(events[3]["event_id"])["pairing"]["state"] == "paired"
        status = store.status()
        assert status["coverage"] == {"projected": 4, "excluded_mechanical": 3, "pending": 1}
        assert not status["semantic_done"] and status["model_calls"] == 0
        assert store.search("中文索引")["matches"][0]["event_id"] == item["event_id"]
        assert not store.search("hidden")["matches"]
        assert not store.search("镜像")["matches"]
        assert not imported["semantic_done"]
        assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_turn_context_identity_is_retained_while_body_stays_mechanical(project):
    _, cfg = project
    secret = "sk-" + "b" * 30
    import_records(project, [
        {"type": "turn_context", "payload": {
            "turn_id": "turn-1", "root_turn_id": "root-1", "api_key": secret,
        }},
        message("工作"),
    ])
    with Store(cfg) as store:
        events = store.list_events("chat-1")["events"]
        context = store.read(events[1]["event_id"])
        assert context["turn_id"] == "turn-1"
        assert context["root_turn_id"] == "root-1"
        assert context["coverage"] == {
            "state": "excluded_mechanical", "reason": "turn_context"
        }
        assert secret not in context["text"] and "[REDACTED]" in context["text"]
        assert context["projection"]["redactions"]
        assert secret.encode() not in Path(cfg["_database"]).read_bytes()
        field = context["projection"]["text_fields"][0]
        assert field["json_path"] == "payload" and field["source_start"] == 0
        assert field["source_end"] == len(json.dumps(
            {"api_key": secret, "root_turn_id": "root-1", "turn_id": "turn-1"},
            ensure_ascii=False, sort_keys=True,
        ))


def test_same_text_different_executions_not_merged_and_ambiguity_is_visible(project):
    _, cfg = project
    import_records(project, [tool("function_call", name="exec", arguments="same"),
                             tool("function_call", name="exec", arguments="same"),
                             tool("function_call_output", output="same")])
    with Store(cfg) as store:
        events = store.list_events("chat-1")["events"]
        assert len({r["event_id"] for r in events}) == 4
        assert store.read(events[1]["event_id"])["pairing"]["state"] == "ambiguous"


def test_append_retry_and_old_snapshot_survive_source_deletion(project):
    _, cfg = project
    path, first = import_records(project, [message("first")])
    with Store(cfg) as store:
        event_id = store.list_events("chat-1")["events"][1]["event_id"]
        retry = store.import_source(path, "chat-1", 0)
        assert retry["snapshot_id"] == first["snapshot_id"] and retry["reused_snapshot"]
        with path.open("ab") as f:
            f.write((json.dumps(message("second")) + "\n").encode())
        newer = store.import_source(path, "chat-1", 0)
        assert newer["snapshot_id"] != first["snapshot_id"]
        assert store.list_events("chat-1")["events"][1]["event_id"] == event_id
        path.unlink()
        assert store.read(event_id, snapshot_id=first["snapshot_id"])["text"] == "first"
        store.reindex()
        assert store.search("second")["matches"]
        assert store.db.execute("SELECT count(*) FROM source_snapshots").fetchone()[0] == 2


def test_changed_content_keeps_revision_and_searches_only_current(project):
    root, cfg = project
    path, first = import_records(project, [message("obsolete")])
    with Store(cfg) as store:
        eid = store.list_events("chat-1")["events"][1]["event_id"]
        write_source(path, root, [message("current")])
        store.import_source(path, "chat-1", 0)
        assert store.read(eid)["text"] == "current"
        assert store.read(eid, snapshot_id=first["snapshot_id"])["text"] == "obsolete"
        assert not store.search("obsolete")["matches"]


@pytest.mark.parametrize("problem", ["wrong_chat", "wrong_project", "foreign_database", "metadata_twice"])
def test_wrong_bindings_roll_back(project, problem):
    root, cfg = project
    path = write_source(root / "source.jsonl", root, [message("never indexed")])
    if problem == "wrong_project":
        write_source(path, root / "other", [message("never indexed")])
    if problem == "foreign_database":
        path = root / "old.sqlite3"
        path.write_bytes(b"SQLite format 3\0")
    if problem == "metadata_twice":
        with path.open("ab") as f:
            f.write(b'{"type":"session_meta","payload":{"id":"foreign"}}\n')
    with Store(cfg) as store:
        with pytest.raises(ValueError):
            store.import_source(path, "foreign" if problem == "wrong_chat" else "chat-1", 0)
        assert store.db.execute("SELECT count(*) FROM chats").fetchone()[0] == 0
        assert store.db.execute("SELECT count(*) FROM source_snapshots").fetchone()[0] == 0


def test_segment_order_explicit_and_fork_not_merged(project):
    root, cfg = project
    path, _ = import_records(project, [message("parent")])
    second = write_source(root / "segment.jsonl", root, [message("continuation")])
    fork = write_source(root / "fork.jsonl", root, [message("fork")], chat="fork-id")
    with Store(cfg) as store:
        with pytest.raises(sqlite3.IntegrityError):
            store.import_source(second, "chat-1", 0)
        store.import_source(second, "chat-1", 1)
        with pytest.raises(ValueError):
            store.import_source(path, "chat-1", 2)
        with pytest.raises(ValueError):
            store.import_source(fork, "chat-1", 2)
        store.import_source(fork, "fork-id", 0)
        assert len(store.list_events("chat-1")["events"]) == 4
        assert len(store.list_events("fork-id")["events"]) == 2
        page = store.list_events("chat-1", limit=2)["events"]
        after = store.list_events("chat-1", after_order=page[-1]["ordinal"], after_record=page[-1]["record_no"])
        assert after["events"][0]["ordinal"] == 1


def test_unknown_media_partial_line_and_host_envelope_stay_pending(project):
    root, cfg = project
    media = message("visible")
    media["payload"]["content"].append({"type": "input_image", "image_url": "data:image/png;base64,PRIVATE"})
    path, _ = import_records(project, [media, message("# AGENTS.md instructions\nhost instructions"),
                                       {"type": "unknown", "payload": {"data": "unknown"}}])
    with path.open("ab") as f:
        f.write(b'not valid json\n{"type":"response_item"')
    with Store(cfg) as store:
        store.import_source(path, "chat-1", 0)
        rows = store.list_events("chat-1")["events"]
        assert all(r["state"] == "pending" for r in rows[1:])
        assert not store.search("PRIVATE")["matches"]
        assert store.search("visible")["matches"][0]["state"] == "pending"
        assert not store.search("instructions")["matches"]


def test_credentials_redacted_before_persistence_and_read_only_does_not_write(project):
    root, cfg = project
    secret = "sk-" + "a" * 30
    import_records(project, [message(f"api_key={secret} deployment done")])
    with Store(cfg) as store:
        eid = store.search("deployment")["matches"][0]["event_id"]
        result = store.read(eid)
        assert secret not in result["text"] and "[REDACTED]" in result["text"]
        assert result["projection"]["redactions"]
    database = root / ".seywo/index.sqlite3"
    assert secret.encode() not in database.read_bytes()
    before = database.read_bytes()
    with Store(cfg, read_only=True) as store:
        store.search("deployment")
        store.read(eid)
        store.status()
        with pytest.raises(sqlite3.OperationalError):
            store.reindex()
    assert database.read_bytes() == before


def test_delete_removes_versions_fts_and_blocks_reimport(project):
    root, cfg = project
    path, _ = import_records(project, [message("erase me")])
    with Store(cfg) as store:
        eid = store.search("erase")["matches"][0]["event_id"]
        store.delete_chat("chat-1")
        assert not store.search("erase")["matches"]
        assert store.db.execute("SELECT count(*) FROM source_fragments").fetchone()[0] == 0
        with pytest.raises(ValueError):
            store.read(eid)
        with pytest.raises(ValueError):
            store.import_source(path, "chat-1", 0)
        assert path.exists()
        assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_foreign_sqlite_and_config_cannot_be_loaded(project):
    root, cfg = project
    wrong = root / "foreign.sqlite3"
    with sqlite3.connect(wrong) as db:
        db.execute("CREATE TABLE work_memory(id TEXT)")
    foreign = {**cfg, "_database": str(wrong)}
    before = wrong.read_bytes()
    with pytest.raises(ValueError):
        Store(foreign, create=True)
    assert wrong.read_bytes() == before
    with pytest.raises(ValueError):
        load_config(root / ".sessiongraph/config.json")


def test_failure_mid_import_rolls_back_to_published_head(project, monkeypatch):
    import seywo.store as module
    root, cfg = project
    path, original = import_records(project, [message("stable")])
    write_source(path, root, [message("new text")])
    real = module.project
    def fail(record):
        if record.get("type") == "response_item":
            raise RuntimeError("fixture crash")
        return real(record)
    with Store(cfg) as store:
        monkeypatch.setattr(module, "project", fail)
        with pytest.raises(RuntimeError):
            store.import_source(path, "chat-1", 0)
        assert store.search("stable")["matches"][0]["snapshot_id"] == original["snapshot_id"]
        assert not store.search("new")["matches"]


def test_capacity_checks_full_request_and_keeps_estimation_explicit():
    capacity = Capacity(context_limit=10000, input_limit=9000, output_limit=4000, protocol_margin=512)
    assert capacity.input_cap(24000, 4000) == 5488
    request = {"instructions": "rules", "input": "中文", "schema": {"description": "x" * 100}}
    measured = measure_request(request)
    assert measured["kind"] == "estimated" and not measured["provider_verified"]
    assert measure_request({**request, "schema": {"description": "x" * 10000}})["tokens"] > measured["tokens"]
    assert reserve_request(request, capacity, application_cap=24000, output_reserve=4000, tokenizer=lambda _: 5489)["action"] == "split_or_wait"
    assert reserve_request(request, capacity, application_cap=24000, output_reserve=4000, tokenizer=lambda _: 5488)["allowed"]
    with pytest.raises(ValueError):
        capacity.input_cap(24000, 5000)
    with pytest.raises(ValueError):
        Capacity(True, 100, 100)


def test_cli_end_to_end_without_sessiongraph_or_installation(project):
    modules_before = set(sys.modules)
    root, cfg = project
    path = write_source(root / "raw.jsonl", root, [message("cli smoke 中文")])
    import os
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    def run(*args):
        return subprocess.run([sys.executable, "-m", "seywo", "--config", cfg["_path"], *args],
                              capture_output=True, text=True, env=env, cwd=root)
    imported = run("import", str(path), "--chat", "chat-1", "--order", "0")
    assert imported.returncode == 0, imported.stderr
    result = run("search", "中文")
    assert result.returncode == 0, result.stderr
    eid = json.loads(result.stdout)["matches"][0]["event_id"]
    assert json.loads(run("read", eid).stdout)["text"] == "cli smoke 中文"
    failed = run("import", str(path), "--chat", "wrong", "--order", "0")
    assert failed.returncode == 2 and "Traceback" not in failed.stderr
    assert not any(m.startswith("sessiongraph") for m in set(sys.modules) - modules_before)


def test_oversize_record_errors_without_partial_publication(project, monkeypatch):
    import seywo.store as module
    root, cfg = project
    path, original = import_records(project, [message("original")])
    write_source(path, root, [message("x" * 4000)])
    monkeypatch.setattr(module, "MAX_LINE_BYTES", 1000)
    with Store(cfg) as store:
        with pytest.raises(ValueError, match="without truncation"):
            store.import_source(path, "chat-1", 0)
        assert store.search("original")["matches"][0]["snapshot_id"] == original["snapshot_id"]
        assert store.db.execute("SELECT count(*) FROM source_snapshots").fetchone()[0] == 1


def test_source_changed_during_read_rolls_back(project, monkeypatch):
    import seywo.store as module
    root, cfg = project
    path, _ = import_records(project, [message("stable")])
    write_source(path, root, [message("new")])
    real = module.project
    changed = False
    def mutate(record):
        nonlocal changed
        if not changed:
            changed = True
            with path.open("ab") as handle:
                handle.write(b'{}\n')
        return real(record)
    monkeypatch.setattr(module, "project", mutate)
    with Store(cfg) as store:
        with pytest.raises(ValueError, match="changing source"):
            store.import_source(path, "chat-1", 0)
        assert store.search("stable")["matches"]
        assert not store.search("new")["matches"]


def test_binary_and_private_key_never_reach_database(project):
    root, cfg = project
    binary = "data:image/png;base64," + "ABCD" * 30
    key = "-----BEGIN PRIVATE KEY-----\nCONFIDENTIAL\n-----END PRIVATE KEY-----"
    import_records(project, [tool("custom_tool_call", name="image", input=binary),
                             tool("custom_tool_call_output", output=key)])
    with Store(cfg) as store:
        rows = store.list_events("chat-1")["events"]
        for row in rows[1:]:
            assert store.read(row["event_id"])["text"] == "[REDACTED]"
    data = (root / ".seywo/index.sqlite3").read_bytes()
    assert binary.encode() not in data and b"CONFIDENTIAL" not in data


def test_corrupt_source_bytes_are_a_gap_not_dropped(project):
    root, cfg = project
    path, _ = import_records(project, [message("visible")])
    with path.open("ab") as handle:
        handle.write(b'\xff\xfe\n')
    with Store(cfg) as store:
        store.import_source(path, "chat-1", 0)
        row = store.list_events("chat-1")["events"][-1]
        assert row["state"] == "pending"
        assert store.read(row["event_id"])["source"]["byte_end"] - store.read(row["event_id"])["source"]["byte_start"] == 3


def test_missing_call_is_unknown_not_assumed_success(project):
    _, cfg = project
    import_records(project, [tool("function_call_output", output="result without call")])
    with Store(cfg) as store:
        row = store.list_events("chat-1")["events"][-1]
        assert store.read(row["event_id"])["pairing"]["state"] == "missing"


def test_initialization_failure_preserves_custom_ignore_and_no_runtime_data(tmp_path, monkeypatch):
    state = tmp_path / ".seywo"
    state.mkdir()
    custom = state / ".gitignore"
    custom.write_text("# do not lose this\n!export.json\n")
    before = custom.read_bytes()
    def fail(*_):
        raise PermissionError("cannot publish ignore")
    monkeypatch.setattr(config.os, "replace", fail)
    with pytest.raises(PermissionError):
        initialize(tmp_path)
    assert custom.read_bytes() == before
    assert not (state / "config.json").exists()
    assert not (state / "index.sqlite3").exists()
    assert sorted(p.name for p in state.iterdir()) == [".gitignore"]


def test_historical_tool_pairing_does_not_use_later_results(project):
    root, cfg = project
    path, first = import_records(project, [tool("function_call", name="exec", arguments="run")])
    with Store(cfg) as store:
        eid = store.list_events("chat-1")["events"][1]["event_id"]
        with path.open("ab") as handle:
            handle.write((json.dumps(tool("function_call_output", output="late")) + "\n").encode())
        store.import_source(path, "chat-1", 0)
        assert store.read(eid)["pairing"]["state"] == "paired"
        old = store.read(eid, snapshot_id=first["snapshot_id"])
        assert old["pairing"]["state"] == "missing"
        assert old["pairing"]["scope"] == "physical_snapshot"
