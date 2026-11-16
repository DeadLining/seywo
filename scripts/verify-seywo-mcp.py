#!/usr/bin/env python3
"""Read-only protocol acceptance against an explicitly selected project's registration."""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import tomllib

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def verify(config, query):
    settings = json.loads(config.read_text())
    root = Path(settings["project_root"])
    if config.resolve() != root / ".seywo/config.json":
        raise ValueError("config must belong to the selected project")
    registration = root / ".codex/config.toml"
    server = tomllib.loads(registration.read_text())["mcp_servers"]["seywo"]
    params = StdioServerParameters(
        command=server["command"], args=server["args"], env=server.get("env")
    )
    expected = {
        "seywo_" + name
        for name in (
            "search",
            "read_source",
            "read_narrative",
            "expand",
            "packet",
            "recall",
            "resume",
            "status",
            "remember",
        )
    }
    results = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            assert {t.name for t in tools} == expected
            assert all(
                t.annotations.readOnlyHint == (t.name != "seywo_remember")
                for t in tools
            )

            async def call(name, args=None, *, expect_error=False):
                args = args or {}
                started = time.perf_counter()
                response = await session.call_tool(name, args)
                assert bool(response.isError) == expect_error, name
                assert len(response.content) == 1 and response.structuredContent is None
                body = response.content[0].text
                size = len(body.encode())
                assert size <= args.get("max_bytes", 4096)
                record = {
                    "tool": name,
                    "seconds": round(time.perf_counter() - started, 4),
                    "utf8_bytes": size,
                    "expected_error": expect_error,
                    "passed": True,
                }
                if expect_error:
                    results.append(record)
                    return None
                value = json.loads(body)
                assert value["model_calls"] == 0
                assert not value.get("error") and not value.get("budget"), name
                record.update(results=len(value.get("results", [])), model_calls=0)
                results.append(record)
                return value

            found = await call("seywo_search", {"query": query})
            assert found["results"], "use a query present in the selected project"
            hit = found["results"][0]
            source = await call(
                "seywo_read_source",
                {
                    **{k: hit[k] for k in ("chat_id", "event_id", "snapshot_id")},
                    "start": hit["char_start"],
                },
            )
            assert source["text"]
            expanded = await call(
                "seywo_expand", {"object_id": hit["id"], "offset": hit["char_start"]}
            )
            assert expanded["text"] == source["text"]
            await call("seywo_packet", {"query": query})
            await call("seywo_recall", {"query": query})
            resumed = await call("seywo_resume")
            chat = next(
                r["chat_id"] for r in resumed["results"] if r["kind"] == "chat_resume"
            )
            await call("seywo_read_narrative", {"chat_id": chat})
            status = await call("seywo_status")
            await call("seywo_search", {"query": query, "chat_ids": [hit["chat_id"]]})
            await call("seywo_search", {"query": query, "mode": "graph"})
            await call(
                "seywo_search",
                {"query": query, "chat_ids": ["seywo-verification-missing-chat"]},
                expect_error=True,
            )
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "project_id": settings["project_id"],
        "project_root": str(root),
        "registration": str(registration),
        "transport": "real MCP stdio using installed project registration",
        "listed_tools": sorted(expected),
        "checks": results,
        "coverage": {
            k: status[k] for k in ("chats", "source_segments", "coverage", "memories")
        },
        "real_project_writes": False,
        "model_calls": 0,
        "remember_boundary": "All nine tools, including remember, exercised through real stdio in isolated fixture test_real_stdio_handshake_list_and_call",
        "codex_boundary": "CLI recognizes project registration; current chat native tool refresh is not established by stdio acceptance",
        "source_sha256": {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((root / "seywo/src/seywo").glob("*.py"))
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--query", default="MCP")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(verify(args.config.resolve(), args.query))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "passed": True,
                "checks": len(report["checks"]),
                "output": str(args.output),
                "model_calls": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
