from sessiongraph.source_text import hydrate
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from sessiongraph.cli import main, handle_hook
from sessiongraph.core import Store, init_project, load_config
from sessiongraph.extraction import ChatCompletionsExtractor, run_one, validate_output
from sessiongraph.mcp_server import create_server
from test_mvp import good_extract, write_session


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "demo"
    root.mkdir()
    path = init_project(root)
    config = load_config(path)
    store = Store(config)
    transcript = write_session(
        root / "session.jsonl",
        root,
        "one",
        [("user", "先别上 Redis，单机够用了，别增加运维负担。")],
    )
    yield root, path, store, transcript
    store.close()


def test_cli_end_to_end(setup, capsys):
    root, cfg, store, transcript = setup
    assert main(["--config", str(cfg), "ingest", str(transcript), "--enqueue"]) == 0
    assert json.loads(capsys.readouterr().out)["enqueued"] == 1
    assert main(["--config", str(cfg), "search", "Redis"]) == 0
    result = json.loads(capsys.readouterr().out)
    eid = result["hits"][0]["id"]
    assert main(["--config", str(cfg), "expand", eid]) == 0
    assert json.loads(capsys.readouterr().out)["entries"][0]["text"].startswith("先别")
    assert main(["--config", str(cfg), "worker"]) == 2
    assert "allow-network" in capsys.readouterr().err
    assert main(["--config", str(cfg), "status"]) == 0
    assert json.loads(capsys.readouterr().out)["jobs"]["pending"] == 1
    assert main(["--config", str(cfg), "enqueue"]) == 0
    capsys.readouterr()
    assert main(["--config", str(cfg), "retry"]) == 0
    capsys.readouterr()
    assert main(["--config", str(cfg), "search", ""]) == 2
    assert main(["init", "--project", str(root / "new")]) == 0


def test_hook_allowlist_and_session_identity(setup):
    root, cfg, store, transcript = setup
    payload = {
        "session_id": "one",
        "transcript_path": str(transcript),
        "hook_event_name": "PreCompact",
    }
    with pytest.raises(ValueError, match="allowlist"):
        handle_hook(store, payload)
    store.config["hook"]["allowed_transcript_roots"] = [str(root)]
    with pytest.raises(ValueError, match="identity"):
        handle_hook(store, dict(payload, session_id="wrong"))
    assert store.status()["raw_entries"] == 0
    assert handle_hook(store, payload)["enqueued"] == 1
    assert handle_hook(store, payload)["enqueued"] == 0
    with pytest.raises(ValueError):
        handle_hook(store, dict(payload, transcript_path=None))
    with pytest.raises(ValueError):
        handle_hook(store, dict(payload, hook_event_name="unknown"))


def test_mcp_registered_tools_and_calls(setup):
    _, cfg, store, transcript = setup
    store.ingest(transcript)

    async def exercise():
        server = create_server(cfg)
        tools = await server.list_tools()
        assert {x.name for x in tools} == {"session_search", "session_expand", "session_status", "session_packet", "session_remember", "session_recall", "session_resume"}
        result = await server.call_tool("session_search", {"query": "Redis"})
        assert "Redis" in str(result)
        eid = store.search("Redis")["hits"][0]["id"]
        assert "one" in str(await server.call_tool("session_expand", {"object_id": eid}))
        assert "raw_entries" in str(await server.call_tool("session_status", {}))

    asyncio.run(exercise())


def test_module_cli_subprocess(setup):
    _, cfg, _, _ = setup
    proc = subprocess.run(
        [sys.executable, "-m", "sessiongraph", "--config", str(cfg), "status"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["raw_entries"] == 0


def test_provider_requires_explicit_environment(setup, monkeypatch):
    _, _, s, _ = setup
    for name in ("GRAPH_LLM_MODEL", "GRAPH_LLM_API_KEY", "GRAPH_LLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="environment"):
        ChatCompletionsExtractor(s.config)
    monkeypatch.setenv("GRAPH_LLM_MODEL", "fixture-model")
    monkeypatch.setenv("GRAPH_LLM_API_KEY", "not-a-real-key")
    monkeypatch.setenv("GRAPH_LLM_BASE_URL", "http://example.com/v1")
    with pytest.raises(ValueError, match="HTTPS"):
        ChatCompletionsExtractor(s.config)
    monkeypatch.setenv("GRAPH_LLM_BASE_URL", "https://example.com/v1")
    extractor = ChatCompletionsExtractor(s.config)
    assert extractor.provenance["model"] == "fixture-model"


def test_bad_endpoint_and_policy_types(setup):
    _, _, s, transcript = setup
    s.ingest(transcript)
    batch = [hydrate(s, r) for r in s.db.execute("SELECT * FROM entries")]
    output = good_extract(batch, s.config["policy"])
    output["relations"][0]["target"] = "absent"
    with pytest.raises(ValueError, match="endpoint"):
        validate_output(output, batch, s.config["policy"])
    output = good_extract(batch, s.config["policy"])
    output["relations"][0]["kind"] = "ACCEPTS"
    with pytest.raises(ValueError, match="endpoint types"):
        validate_output(output, batch, s.config["policy"])


def test_stale_lease_recovery_and_policy_filter(setup):
    _, _, s, transcript = setup
    s.ingest(transcript)
    s.enqueue()
    with s.db:
        s.db.execute("UPDATE jobs SET state='running',lease_until=0,lease_token='old'")
    assert run_one(s, good_extract)["state"] == "done"
    old = s.search("缓存选型")["hits"][0]["id"]
    s.config["policy_hash"] = "new-policy"
    assert not any(h["type"] == "record" for h in s.search("缓存选型")["hits"])
    assert s.status()["semantic_entries"] == 0
    with pytest.raises(ValueError):
        s.expand(old)
    assert s.enqueue() == 1


def test_fenced_worker_cannot_commit_after_lease_taken(setup):
    _, _, s, transcript = setup
    s.ingest(transcript)
    s.enqueue()

    def stolen(batch, policy):
        with s.db:
            s.db.execute("UPDATE jobs SET lease_token='new-worker'")
        return good_extract(batch, policy)

    assert run_one(s, stolen)["state"] == "superseded"
    assert s.status()["records"] == 0


def test_provider_http_roundtrip_and_failures(setup, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from sessiongraph.cli import main

    root, cfg, s, transcript = setup
    s.ingest(transcript)
    s.enqueue()
    requests = []
    response_mode = ["ok"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request_body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, self.headers["Authorization"], request_body))
            if response_mode[0] == "http-error":
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"secret-should-never-be-recorded")
                return
            batch = json.loads(request_body["messages"][1]["content"])["untrusted_transcript"]
            extracted = good_extract(batch, s.config["policy"])
            envelope = {
                "choices": [
                    {"finish_reason": "stop", "message": {"content": json.dumps(extracted)}}
                ]
            }
            if response_mode[0] == "malformed":
                envelope = {"wrong": True}
            if response_mode[0] == "truncated":
                envelope["choices"][0]["finish_reason"] = "length"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(envelope).encode())

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("GRAPH_LLM_BASE_URL", f"http://127.0.0.1:{http.server_port}/v1")
    monkeypatch.setenv("GRAPH_LLM_MODEL", "fixture-model")
    monkeypatch.setenv("GRAPH_LLM_API_KEY", "fixture-key")
    try:
        assert main(["--config", str(cfg), "worker", "--allow-network", "--max-jobs", "2"]) == 0
        assert requests[0][0] == "/v1/chat/completions"
        assert requests[0][1] == "Bearer fixture-key"
        assert s.status()["records"] == 2
        provider = ChatCompletionsExtractor(s.config)
        batch = [hydrate(s, r) for r in s.db.execute("SELECT * FROM entries")]
        for mode, expected in [
            ("http-error", "HTTP 401"),
            ("malformed", "malformed"),
            ("truncated", "budget exhausted"),
        ]:
            response_mode[0] = mode
            with pytest.raises(ValueError, match=expected) as exc:
                provider(batch, s.config["policy"])
            assert "secret-should" not in str(exc.value)
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


def test_real_mcp_stdio_handshake(setup):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    _, cfg, s, transcript = setup
    s.ingest(transcript)

    async def exercise():
        import sessiongraph
        params = StdioServerParameters(
            command=sys.executable, args=["-B", "-m", "sessiongraph", "--config", str(cfg), "mcp"],
            env={**os.environ, 'PYTHONPATH':str(Path(sessiongraph.__file__).resolve().parent.parent)},
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                initialized = await client.initialize()
                assert "not on every turn" in initialized.instructions
                tools = await client.list_tools()
                assert {t.name for t in tools.tools} == {"session_search", "session_expand", "session_status", "session_packet", "session_remember", "session_recall", "session_resume"}
                result = await client.call_tool("session_search", {"query": "Redis"})
                assert not result.isError
                assert "Redis" in str(result.content)
                entry_id = s.search("Redis")["hits"][0]["id"]
                saved = await client.call_tool("session_remember", {
                    "kind": "finding", "title": "MCP stdio记忆", "content": "写入后可被新查询找回",
                    "source_entry_id": entry_id, "source_quote": "Redis",
                })
                assert not saved.isError
                recalled = await client.call_tool("session_recall", {"query": "MCP stdio记忆"})
                assert not recalled.isError
                assert "MCP stdio记忆" in str(recalled.content)
                assert "returned_bytes" in str(recalled.content)
                assert "brief" in str(recalled.content)

    asyncio.run(exercise())


def test_hook_cli_input(setup, monkeypatch, capsys):
    import io

    root, cfg, s, transcript = setup
    data = json.loads(cfg.read_text())
    data["hook"]["allowed_transcript_roots"] = [str(root)]
    cfg.write_text(json.dumps(data))
    payload = {
        "hook_event_name": "PreCompact",
        "session_id": "one",
        "transcript_path": str(transcript),
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert main(["--config", str(cfg), "hook"]) == 0
    assert json.loads(capsys.readouterr().out)["enqueued"] == 1
    monkeypatch.setattr(sys, "stdin", io.StringIO("x" * 65537))
    assert main(["--config", str(cfg), "hook"]) == 2


def test_large_graph_payload_respects_text_budget(setup):
    _, _, s, transcript = setup
    s.ingest(transcript)
    s.enqueue()

    def large(batch, policy):
        out = good_extract(batch, policy)
        for record in out["records"]:
            record["content"] += "x" * 1500
            record["evidence"] *= 8
        return out

    assert run_one(s, large)["state"] == "done"
    rid = next(h["id"] for h in s.search("缓存选型")["hits"] if h["type"] == "record")
    view = s.expand(rid, max_chars=1000)
    assert view["budget"]["returned_text_chars"] <= 1000
    assert view["truncated_fields"]


def test_database_identity_mismatch_is_clear_error(setup):
    _, _, s, _ = setup
    cfg = dict(s.config, project_id="wrong-project")
    with pytest.raises(ValueError, match="project_id"):
        Store(cfg)


def test_hook_enqueues_all_segments_of_same_chat(setup):
    root, _, s, transcript = setup
    s.ingest(transcript)
    extra = write_session(root / "continued.jsonl", root, "one", [("user", "第二段讨论")])
    s.config["hook"]["allowed_transcript_roots"] = [str(root)]
    result = handle_hook(
        s, {"hook_event_name": "PreCompact", "session_id": "one", "transcript_path": str(extra)}
    )
    assert result["enqueued"] == 2
    assert s.status()["sessions"] == 1
    assert s.status()["source_segments"] == 2


def test_mcp_scope_contract_distinguishes_chat_and_rollout_segment(setup):
    root, cfg, s, transcript = setup
    first = s.ingest(transcript)
    second = s.ingest(
        write_session(root / "continued.jsonl", root, "one", [("user", "second segment needle")])
    )
    assert first["session_id"] != second["session_id"]

    async def exercise():
        server = create_server(cfg)
        tools = {tool.name: tool for tool in await server.list_tools()}
        props = tools["session_search"].inputSchema["properties"]
        assert {"chat_id", "segment_id", "session_id"} <= set(props)

        by_chat = await server.call_tool("session_search", {"query": "needle", "chat_id": "one"})
        by_segment = await server.call_tool(
            "session_search", {"query": "needle", "segment_id": second["session_id"]}
        )
        by_legacy_alias = await server.call_tool(
            "session_search", {"query": "needle", "session_id": "one"}
        )
        chat_payload = json.loads(by_chat[0].text)
        segment_payload = json.loads(by_segment[0].text)
        alias_payload = json.loads(by_legacy_alias[0].text)
        assert len(chat_payload["hits"]) == 1
        assert segment_payload["scope"] == {
            "chat_id": None,
            "segment_id": second["session_id"],
            "chat_exists": None,
            "segment_exists": True,
        }
        assert segment_payload["hits"][0]["session_id"] == second["session_id"]
        assert alias_payload["scope"]["chat_id"] == "one"

    asyncio.run(exercise())


def test_mcp_opens_sqlite_read_only_and_reports_unambiguous_coverage(setup):
    _, cfg, s, transcript = setup
    s.ingest(transcript)
    db_path = cfg.parent / cfg.name.replace("config.json", "index.sqlite3")
    before = db_path.read_bytes()
    readonly = Store(load_config(cfg), readonly=True)
    try:
        assert readonly.db.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(ValueError, match="read-only"):
            readonly.enqueue()
    finally:
        readonly.close()
    assert db_path.read_bytes() == before

    async def exercise():
        payload = await create_server(cfg).call_tool("session_status", {})
        status = json.loads(payload[0].text)
        assert status["chats"] == status["sessions"] == 1
        assert status["source_segments"] == 1
        assert status["semantic_entries_total"] >= status["semantic_entries"]
        assert {"raw_entries", "completed_entries", "pending_entries", "ratio"} <= set(
            status["coverage"]
        )

    asyncio.run(exercise())


def test_mcp_parameter_errors_are_structured(setup):
    _, cfg, s, transcript = setup
    s.ingest(transcript)

    async def exercise():
        server = create_server(cfg)
        from mcp.server.fastmcp.exceptions import ToolError

        with pytest.raises(ToolError, match="limit must be between 1 and 100"):
            await server.call_tool("session_search", {"query": "x", "limit": 0})
        with pytest.raises(ToolError, match="unknown entry or current-policy record id"):
            await server.call_tool("session_expand", {"object_id": "missing"})

    asyncio.run(exercise())
