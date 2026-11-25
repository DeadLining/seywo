"""MCP contract tests use isolated project data and the real SDK/stdio protocol."""

import asyncio
import json
from pathlib import Path
import sqlite3
import sys

import pytest

from test_s2 import setup as setup, message
from test_s3 import publish
from seywo.config import initialize, load_config
from seywo.mcp_server import create_server
from seywo.retrieval import Retrieval, identity, serialize, snapshot
from seywo.store import Store


@pytest.fixture
def project(setup):
    store, engine, job, service, task, claim, view = publish(setup)
    other = Path(store.config["project_root"]) / "other.jsonl"
    other.write_text(
        "\n".join(
            json.dumps(e, ensure_ascii=False)
            for e in [
                {
                    "type": "session_meta",
                    "payload": {"id": "other", "cwd": store.config["project_root"]},
                },
                message("另一个chat的索引验证"),
                message("其他聊天的结果", "assistant"),
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "c",
                        "output": "toolneedle",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "system",
                        "content": [{"type": "input_text", "text": "systemneedle"}],
                    },
                },
            ]
        )
        + "\n"
    )
    store.import_source(other, "other", 0)
    return store, Path(store.config["_path"]), setup, view


def call(cfg, tool, **args):
    async def invoke():
        response = await create_server(cfg).call_tool(tool, args)
        assert len(response) == 1
        return json.loads(response[0].text)

    return asyncio.run(invoke())


def first_source(store, chat="chat"):
    return store.db.execute(
        "SELECT * FROM current_sources WHERE chat_id=? AND role='user' AND kind='message' ORDER BY ordinal,record_no LIMIT 1",
        (chat,),
    ).fetchone()


def memory_args(store, **changes):
    row = first_source(store)
    return dict(
        chat_id="chat",
        event_id=row["event_id"],
        snapshot_id=row["snapshot_id"],
        start=0,
        end=len(row["safe_text"]),
        source_quote=row["safe_text"],
        kind="goal",
        title="索引约束",
        content="先完成索引",
        memory_id="fixture-memory",
        **changes,
    )


def test_all_nine_tools_annotations_and_readonly_startup(project):
    store, cfg, _, _ = project

    async def check():
        tools = await create_server(cfg).list_tools()
        assert {t.name for t in tools} == {
            "seywo_" + n
            for n in [
                "search",
                "read_source",
                "read_narrative",
                "expand",
                "packet",
                "recall",
                "resume",
                "status",
                "remember",
            ]
        }
        for tool in tools:
            assert tool.annotations.readOnlyHint == (tool.name != "seywo_remember")
            assert not tool.annotations.openWorldHint

    asyncio.run(check())
    assert not store.db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='work_memories'"
    ).fetchone()
    assert call(cfg, "seywo_status")["model_calls"] == 0
    assert not store.db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='work_memories'"
    ).fetchone()


def test_cross_chat_search_and_selected_multiple_chats(project):
    store, cfg, _, _ = project
    all_hits = call(cfg, "seywo_search", query="索引")
    assert {r["chat_id"] for r in all_hits["results"]} == {"chat", "other"}
    one = call(cfg, "seywo_search", query="索引", chat_ids=["other"])
    assert {r["chat_id"] for r in one["results"]} == {"other"}
    both = call(cfg, "seywo_search", query="索引", chat_ids=["chat", "other"])
    assert len(both["results"]) == len(all_hits["results"])
    empty = call(cfg, "seywo_search", query="absentneedle")
    assert not empty["results"]
    with pytest.raises(Exception, match="unknown_chat_id"):
        call(cfg, "seywo_search", query="索引", chat_ids=["foreign"])


def test_tool_content_opt_in_and_instruction_content_never_visible(project):
    store, cfg, _, _ = project
    assert not call(cfg, "seywo_search", query="toolneedle")["results"]
    assert call(cfg, "seywo_search", query="toolneedle", include_tools=True)["results"]
    assert not call(cfg, "seywo_search", query="systemneedle", include_tools=True)[
        "results"
    ]
    row = store.db.execute(
        "SELECT * FROM current_sources WHERE role='system'"
    ).fetchone()
    with pytest.raises(Exception, match="source_not_available"):
        call(
            cfg,
            "seywo_read_source",
            chat_id="other",
            event_id=row["event_id"],
            snapshot_id=row["snapshot_id"],
        )


def test_source_identity_pin_range_and_old_snapshot(project):
    store, cfg, setup, _ = project
    row = first_source(store)
    args = dict(
        chat_id="chat", event_id=row["event_id"], snapshot_id=row["snapshot_id"]
    )
    source = call(cfg, "seywo_read_source", **args, start=0, end=3)
    assert (
        source["text"] == "先完成"
        and source["char_end"] == 3
        and source["source_current"]
    )
    with pytest.raises(Exception, match="source_not_available"):
        call(cfg, "seywo_read_source", **{**args, "chat_id": "other"})
    setup[4]([message("索引的要求已更改")])
    store.import_source(setup[3], "chat", 0)
    old = call(cfg, "seywo_read_source", **args, start=0, end=3)
    assert old["text"] == "先完成" and not old["source_current"]


def test_source_expand_uses_search_handle_and_context_is_same_snapshot(project):
    _, cfg, _, _ = project
    hit = call(cfg, "seywo_search", query="索引", chat_ids=["chat"])["results"][0]
    expanded = call(cfg, "seywo_expand", object_id=hit["id"])
    assert "先完成索引" == expanded["text"]
    context = call(cfg, "seywo_expand", object_id=hit["id"], radius=2)
    assert context["results"] and all(
        r["snapshot_id"] == hit["snapshot_id"] for r in context["results"]
    )


def test_narrative_recall_resume_and_node_expand(project):
    _, cfg, _, _ = project
    view = call(cfg, "seywo_read_narrative", chat_id="chat", max_bytes=16000)
    assert view["phases"] and view["overview"] and view["results"]
    node = view["results"][0]
    expanded = call(cfg, "seywo_expand", object_id=node["id"], max_bytes=16000)
    assert expanded["results"][0]["item"]["evidence"]
    recalled = call(cfg, "seywo_recall", query=node["title"])
    assert any(r["kind"] == "narrative_item" for r in recalled["results"])
    resumed = call(cfg, "seywo_resume")
    assert resumed["results"][0]["chat_id"] == "chat"
    hybrid = call(cfg, "seywo_search", query=node["title"], mode="hybrid")
    assert any(r["kind"] == "narrative_item" for r in hybrid["results"])
    graph = call(cfg, "seywo_search", query=node["title"], mode="graph")
    assert graph["results"]


def test_remember_quote_check_idempotency_recall_supersession(project):
    store, cfg, _, _ = project
    args = memory_args(store)
    first = call(cfg, "seywo_remember", **args)
    assert not first["reused"] and first["model_calls"] == 0
    assert call(cfg, "seywo_remember", **args)["reused"]
    with pytest.raises(Exception, match="memory_quote_mismatch"):
        call(cfg, "seywo_remember", **{**args, "source_quote": "编造原文"})
    with pytest.raises(Exception, match="memory_id_conflict"):
        call(cfg, "seywo_remember", **{**args, "content": "修改后"})
    later = call(
        cfg,
        "seywo_remember",
        **{
            **args,
            "memory_id": "new",
            "content": "先完成索引，再验证",
            "supersedes_id": first["memory_id"],
        },
    )
    recalled = call(cfg, "seywo_recall", query="索引约束")
    assert [r["memory_id"] for r in recalled["results"]] == [later["memory_id"]]
    assert (
        len(
            call(cfg, "seywo_recall", query="索引约束", include_superseded=True)[
                "results"
            ]
        )
        == 2
    )
    expanded = call(
        cfg,
        "seywo_expand",
        object_id=identity("m", later["memory_id"]),
        max_bytes=16000,
    )
    assert expanded["results"][0]["source_current"]
    assert call(cfg, "seywo_status")["memories"] == 1


@pytest.mark.parametrize(
    "change",
    [
        {"paths": ["/made/up/path"]},
        {"scope": "unknown"},
        {"kind": "unknown"},
        {"end": 8001},
        {"title": "x" * 41},
        {"content": "api_key=abcdef0123456789"},
        {"supersedes_id": "not-present"},
    ],
)
def test_invalid_memory_does_not_write(project, change):
    store, cfg, _, _ = project
    with pytest.raises(Exception):
        call(cfg, "seywo_remember", **{**memory_args(store), **change})
    assert call(cfg, "seywo_status")["memories"] == 0


def test_byte_budgets_and_pagination_are_real_utf8(project):
    _, cfg, _, _ = project
    hits = []
    offset = 0
    for _ in range(10):
        result = call(
            cfg, "seywo_search", query="索引", offset=offset, limit=1, max_bytes=1000
        )
        assert len(serialize(result).encode()) <= 1000
        hits += result["results"]
        if not result["retrieval"]["has_more"]:
            break
        offset = result["retrieval"]["next_offset"]
    assert len(hits) == 2 and len({r["id"] for r in hits}) == 2
    for tool, args in [
        ("seywo_packet", {"query": "索引"}),
        ("seywo_recall", {"query": "事项"}),
        ("seywo_read_narrative", {"chat_id": "chat"}),
        ("seywo_resume", {}),
    ]:
        result = call(cfg, tool, **args, max_bytes=512)
        assert len(serialize(result).encode()) <= 512


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"offset": -1},
        {"max_bytes": 10},
        {"chat_ids": ["absent"]},
        {"mode": "semantic"},
        {"query": ""},
    ],
)
def test_parameter_validation(project, params):
    _, cfg, _, _ = project
    with pytest.raises(Exception):
        call(cfg, "seywo_search", **{"query": "索引", **params})


def test_read_tools_do_not_write_or_call_provider(project, monkeypatch):
    store, cfg, _, _ = project

    def denied(*args, **kwargs):
        raise AssertionError("network/model construction forbidden")

    monkeypatch.setattr("urllib.request.urlopen", denied)
    with Store(load_config(cfg), read_only=True) as ro:

        def authorizer(action, *args):
            return (
                sqlite3.SQLITE_OK
                if action
                in {
                    sqlite3.SQLITE_SELECT,
                    sqlite3.SQLITE_READ,
                    sqlite3.SQLITE_FUNCTION,
                    sqlite3.SQLITE_TRANSACTION,
                    sqlite3.SQLITE_RECURSIVE,
                    sqlite3.SQLITE_PRAGMA,
                }
                else sqlite3.SQLITE_DENY
            )

        ro.db.set_authorizer(authorizer)
        with snapshot(ro):
            reader = Retrieval(ro)
            hit = reader.search("索引")["results"][0]
            assert reader.expand(hit["id"])["text"]
            assert reader.recall("事项")
            assert reader.resume()
            assert reader.packet("索引")
            assert reader.read_narrative("chat", max_bytes=16000)
            assert reader.status()["model_calls"] == 0
        assert ro.db.total_changes == 0


def test_other_project_isolation(project, tmp_path):
    store, cfg, _, _ = project
    root = tmp_path / "second"
    initialize(root)
    other_cfg = root / ".seywo/config.json"
    assert not call(other_cfg, "seywo_search", query="索引")["results"]
    hit = call(cfg, "seywo_search", query="索引")["results"][0]
    with pytest.raises(Exception):
        call(other_cfg, "seywo_expand", object_id=hit["id"])


def test_real_stdio_handshake_list_and_call(project):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    store, cfg, _, _ = project

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-B", "-m", "seywo", "--config", str(cfg), "mcp"],
            env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert len((await session.list_tools()).tools) == 9

                async def invoke(name, **args):
                    response = await session.call_tool(name, args)
                    assert not response.isError and len(response.content) == 1
                    assert response.structuredContent is None
                    assert len(response.content[0].text.encode()) <= args.get(
                        "max_bytes", 4096
                    )
                    return json.loads(response.content[0].text)

                found = await invoke("seywo_search", query="索引")
                assert len(found["results"]) == 2
                hit = found["results"][0]
                source = await invoke(
                    "seywo_read_source",
                    **{k: hit[k] for k in ("chat_id", "event_id", "snapshot_id")},
                )
                assert source["text"]
                assert (await invoke("seywo_expand", object_id=hit["id"]))["text"]
                assert (await invoke("seywo_read_narrative", chat_id="chat"))[
                    "version"
                ] == 1
                await invoke("seywo_recall", query="索引")
                await invoke("seywo_resume")
                assert (await invoke("seywo_packet", query="索引"))["results"]
                remembered = await invoke("seywo_remember", **memory_args(store))
                assert remembered["memory_id"] == "fixture-memory"
                assert (await invoke("seywo_status"))["memories"] == 1

    asyncio.run(run())


def test_match_excerpt_near_end_and_unicode_continuation(project):
    store, cfg, setup, _ = project
    text = "前言" * 2000 + " 唯一针点 " + ("😀中" * 1000)
    setup[4]([message(text)])
    store.import_source(setup[3], "chat", 0)
    hit = call(cfg, "seywo_search", query="唯一针点", chat_ids=["chat"])["results"][0]
    assert hit["char_start"] > 3000 and "唯一针点" in hit["preview"]
    packet = call(cfg, "seywo_packet", query="唯一针点")
    assert "唯一针点" in packet["results"][0]["text"]
    result = call(
        cfg,
        "seywo_read_source",
        chat_id="chat",
        event_id=hit["event_id"],
        snapshot_id=hit["snapshot_id"],
        start=4000,
        max_bytes=900,
    )
    assert len(serialize(result).encode()) <= 900
    assert result["text"] == text[result["char_start"] : result["char_end"]]
    assert result["next_start"] == result["char_end"] and result["char_end"] < len(text)


def test_graph_traverses_narrative_dependencies(project):
    store, cfg, _, view = project
    from seywo.narrative import Narrative
    from seywo.store import encode
    import copy

    doc = copy.deepcopy(view["narrative"])
    first, second = doc["items"]
    first["title"] = "独特检索项"
    second["title"] = "依赖邻居"
    second["content"] = "其他内容"
    second["dependencies"] = [first["id"]]
    with store.db:
        Narrative(store)._write(
            "chat",
            2,
            0,
            doc,
            encode(
                [
                    dict(r)
                    for r in store.db.execute(
                        "SELECT id,head_snapshot,ordinal FROM source_segments WHERE chat_id='chat' ORDER BY ordinal"
                    )
                ]
            ),
            origin="fixture",
        )
    graph = call(cfg, "seywo_search", query="独特检索项", mode="graph")
    assert {r["title"] for r in graph["results"]} == {"独特检索项", "依赖邻居"}
    expanded = call(
        cfg,
        "seywo_expand",
        object_id=graph["results"][0]["id"],
        radius=1,
        max_bytes=16000,
    )
    assert len(expanded["results"]) == 2


def test_memory_delete_chat_cascade_and_current_source_status(project):
    store, cfg, setup, _ = project
    result = call(cfg, "seywo_remember", **memory_args(store))
    setup[4]([message("变更后的原文")])
    store.import_source(setup[3], "chat", 0)
    memory = call(
        cfg,
        "seywo_expand",
        object_id=identity("m", result["memory_id"]),
        max_bytes=16000,
    )
    assert not memory["results"][0]["source_current"]
    store.delete_chat("chat")
    assert call(cfg, "seywo_status")["memories"] == 0
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()


def test_startup_does_not_create_missing_database(tmp_path):
    root = tmp_path / "new"
    root.mkdir()
    state = root / ".seywo"
    state.mkdir()
    cfg = state / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "product": "seywo",
                "schema_version": 1,
                "project_id": "x",
                "project_root": str(root),
                "database": "index.sqlite3",
            }
        )
    )
    with pytest.raises(sqlite3.OperationalError):
        create_server(cfg)
    assert not (state / "index.sqlite3").exists()


def test_known_internal_chat_is_not_leaked_in_project_search(project):
    store, cfg, _, _ = project
    internal = Path(store.config["project_root"]) / "internal.jsonl"
    # Mimic a pre-admission legacy import; source metadata later identifies the origin.
    internal.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "internal", "cwd": store.config["project_root"]},
            }
        )
        + "\n"
        + json.dumps(message("internalneedle"))
        + "\n"
    )
    store.import_source(internal, "internal", 0)
    internal.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "internal", "source": {"subagent": {}}},
            }
        )
        + "\n"
    )
    assert not call(cfg, "seywo_search", query="internalneedle")["results"]
    with pytest.raises(Exception, match="nonhuman_subagent_source"):
        call(cfg, "seywo_search", query="internalneedle", chat_ids=["internal"])


def test_project_registration_preserves_other_servers_and_is_idempotent(project):
    _, cfg, _, _ = project
    from seywo.mcp_install import install
    import tomllib

    config = load_config(cfg)
    path = Path(config["project_root"]) / ".codex/config.toml"
    path.parent.mkdir()
    path.write_text('[mcp_servers.existing]\ncommand="unchanged"\n')
    result = install(config)
    assert result["changed"] and path.read_text().startswith("[mcp_servers.existing]")
    data = tomllib.loads(path.read_text())
    server = data['mcp_servers']['seywo']
    assert Path(server['command']).is_absolute() and Path(server['command']).is_file()
    assert Path(server['command']).name in {'seywo-host', 'seywo-host.cmd'}
    assert server['args'] == ['--cli', '--config', config['_path'], 'mcp']
    assert 'env' not in server
    before = path.read_bytes()
    assert not install(config)["changed"] and path.read_bytes() == before
    path.write_text('[mcp_servers.seywo]\ncommand="custom"\n')
    with pytest.raises(ValueError, match="custom_seywo_registration_exists"):
        install(config)
    assert "custom" in path.read_text()


def test_cross_chat_memory_survives_deleted_predecessor(project):
    store, cfg, _, _ = project
    call(cfg, "seywo_remember", **memory_args(store))
    row = first_source(store, "other")
    args = memory_args(store)
    args.update(
        chat_id="other",
        event_id=row["event_id"],
        snapshot_id=row["snapshot_id"],
        start=0,
        end=len(row["safe_text"]),
        source_quote=row["safe_text"],
        memory_id="new-memory",
        supersedes_id="fixture-memory",
    )
    call(cfg, "seywo_remember", **args)
    store.delete_chat("chat")
    memory = call(
        cfg, "seywo_expand", object_id=identity("m", "new-memory"), max_bytes=16000
    )
    assert memory["results"][0]["chat_id"] == "other"
    assert memory["results"][0]["supersedes_id"] is None
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("offset,total", [(0, 1), (9, 11)])
def test_required_budget_is_sufficient_and_exact(offset, total):
    from seywo.retrieval import page

    rows = [{"text": "中" * 200} for _ in range(total)]
    small = page(rows, offset=offset, limit=1, max_bytes=512)
    required = small["budget"]["required_bytes"]
    assert required > 512
    retried = page(rows, offset=offset, limit=1, max_bytes=required)
    assert len(retried["results"]) == 1
    assert len(serialize(retried).encode()) == required
