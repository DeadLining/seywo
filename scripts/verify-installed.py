"""Explicit zero-model installed smoke, scoped only to the fruit-fly project."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT = Path("/Users/shengzhoukong/Documents/果蝇")
PID = "a217c2bc1da5c4913760"
STATE = Path.home() / "Library/Application Support/local-ai-gateway/sessiongraph"
LAUNCHER = STATE / "bin/sessiongraph"
APP = Path("/Applications/SessionGraph.app/Contents/Resources/sessiongraph")


def read(operation, **kwargs):
    result = subprocess.run([str(LAUNCHER), "native-read"], input=json.dumps({"operation": operation, "project": PID, **kwargs}), capture_output=True, text=True, check=True, timeout=15)
    data = json.loads(result.stdout)
    assert data["model_calls"] == 0
    return data


async def main():
    runtime = json.loads(subprocess.check_output([str(LAUNCHER), "runtime-status"], text=True))
    assert runtime["model_calls"] == 0
    assert "/runtimes/" in runtime["source"] and "Gateway.app" not in runtime["source"]
    graph = read("work_map")
    status = read("status")
    # Read settings only for this project. Never change selection/model/hooks.
    result = subprocess.run([runtime["python"], "-B", str(APP / "entrypoint.py"), "native-settings"], input=json.dumps({"action":"status","project":PID}), capture_output=True, text=True, check=True, timeout=15)
    settings = json.loads(result.stdout)
    assert settings["ok"] and settings["model_calls"] == 0
    assert settings["model"] == "MiniMax-M3"
    assert settings["mcp_registered"] and settings["hook_registered"]
    parameters = StdioServerParameters(command=str(LAUNCHER), args=["--config", str(PROJECT / ".sessiongraph/config.json"), "mcp"], cwd=str(PROJECT))
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert {"session_recall","session_expand","session_search","session_resume"} <= names
            recalled = await session.call_tool("session_recall", {"query":"SessionGraph", "limit":1, "max_bytes":3000})
            assert not recalled.isError
            data = json.loads(next(c.text for c in recalled.content if c.type == "text"))
            assert data["model_calls"] == 0
            assert data["retrieval"]["scope"]["project_id"] == PID
            entry = data["memories"][0]["source"]["entry_id"]
            expanded = await session.call_tool("session_expand", {"object_id":entry, "max_chars":3000, "radius":0})
            assert not expanded.isError
            evidence = json.loads(next(c.text for c in expanded.content if c.type == "text"))
            assert evidence.get("model_calls",0) == 0
    output = {"project_id":PID,"model_calls":0,"runtime_source":runtime["source"],"mcp_tools":len(names),"recall_and_expand":True,"work_map_nodes":len(graph.get("nodes",[])),"work_map_edges":len(graph.get("edges",[])),"settings_model":settings["model"],"hook_events":settings["hook_events"],"mcp_registered":settings["mcp_registered"],"native_status_ok":status.get("model_calls")==0}
    print(json.dumps(output,ensure_ascii=False,indent=2))


if __name__ == "__main__":
    asyncio.run(main())
