"""Independent fixture benchmark over real stdio, with network disabled in server."""

import argparse
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import random
import statistics
import sys
import time

from mcp import ClientSession, StdioServerParameters  # noqa: E402 - local script path bootstrap
from mcp.client.stdio import stdio_client  # noqa: E402 - local script path bootstrap

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packet_fixture import build  # noqa: E402 - local script path bootstrap
from sessiongraph.core import load_config  # noqa: E402 - local script path bootstrap

ROOT = Path(__file__).resolve().parents[1]


def dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def run(out):
    out.mkdir(parents=True, exist_ok=True)
    suite_path = ROOT / "benchmarks/retrieval_suite.json"
    suite = json.loads(suite_path.read_text())
    fixture = out / "fixture"
    if fixture.exists():
        raise RuntimeError("Refusing to overwrite development fixture/results")
    fixture.mkdir()
    config, sources = build(fixture, suite)
    cfg = load_config(config)
    db = Path(cfg["database"])
    before = sha(db)
    lookup = {sources[m["key"]]: m["text"] for t in suite["threads"] for m in t["messages"]}
    shim = """import socket,sys,json,atexit
from pathlib import Path
attempts=[]
def deny(*args,**kwargs):
 attempts.append('network_attempt');raise RuntimeError('network forbidden')
socket.socket.connect=deny
socket.create_connection=deny
atexit.register(lambda:Path(sys.argv[2]).write_text(json.dumps({'attempts':attempts,'provider_module_loaded':'sessiongraph.extraction' in sys.modules})))
from sessiongraph.mcp_server import create_server  # noqa: E402 - local script path bootstrap
create_server(Path(sys.argv[1])).run(transport='stdio')
"""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", shim, str(config), str(out / "network.json")],
        env=dict(os.environ),
        cwd=str(ROOT),
    )
    jobs = [
        (q, mode, budget, rep)
        for q in suite["queries"]
        for mode in ["raw", "graph"]
        for budget in [2048, 4096, 8192]
        for rep in range(5)
    ]
    random.Random(510).shuffle(jobs)
    rows = []
    begin = time.perf_counter()
    with (out / "stdio.log").open("w") as log:
        async with stdio_client(params, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                startup = time.perf_counter() - begin
                await client.call_tool("session_packet", {"query": "warmup"})
                for q, mode, budget, rep in jobs:
                    started = time.perf_counter()
                    res = await client.call_tool(
                        "session_packet", dict(query=q["query"], mode=mode, max_bytes=budget)
                    )
                    elapsed = time.perf_counter() - started
                    assert not res.isError, res
                    assert res.structuredContent is None, (
                        "Do not duplicate bounded packet in structuredContent"
                    )
                    text = "".join(c.text for c in res.content if c.type == "text")
                    packet = json.loads(text)
                    assert len(text.encode("utf-8")) <= budget
                    for e in packet["entries"]:
                        assert e["text"] == lookup[e["entry_id"]][e["start"] : e["end"]]
                    got = {e["entry_id"] for e in packet["entries"]}
                    gold = {sources[k] for k in q["gold"]}
                    rows.append(
                        dict(
                            id=q["id"],
                            kind=q["kind"],
                            mode=mode,
                            max_bytes=budget,
                            rep=rep,
                            seconds=elapsed,
                            bytes=len(text.encode("utf-8")),
                            gold_sources=len(gold),
                            found_gold=len(got & gold),
                            returned_sources=len(got),
                            packet=packet,
                        )
                    )
    network = json.loads((out / "network.json").read_text())
    assert not network["attempts"] and not network["provider_module_loaded"]
    assert sha(db) == before
    aggregates = []
    for budget in [2048, 4096, 8192]:
        for mode in ["raw", "graph"]:
            xs = [r for r in rows if r["mode"] == mode and r["max_bytes"] == budget]
            times = sorted(r["seconds"] * 1000 for r in xs)
            positives = [r for r in xs if r["gold_sources"]]
            negatives = [r for r in xs if not r["gold_sources"]]
            aggregates.append(
                dict(
                    mode=mode,
                    max_bytes=budget,
                    n=len(xs),
                    p50_ms=statistics.median(times),
                    p95_ms=times[math.ceil(0.95 * len(times)) - 1],
                    mean_bytes=statistics.mean(r["bytes"] for r in xs),
                    max_actual_bytes=max(r["bytes"] for r in xs),
                    source_recall=sum(r["found_gold"] for r in positives)
                    / sum(r["gold_sources"] for r in positives),
                    all_gold_rate=sum(r["found_gold"] == r["gold_sources"] for r in positives)
                    / len(positives),
                    negative_nonempty_rate=sum(r["returned_sources"] > 0 for r in negatives)
                    / len(negatives),
                )
            )
    dump(
        out / "results.json",
        dict(
            suite_sha256=sha(suite_path),
            startup_seconds=startup,
            db_unchanged=True,
            network=network,
            aggregates=aggregates,
            calls=rows,
        ),
    )
    dump(out / "sources.json", sources)
    print(json.dumps(aggregates, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    asyncio.run(run(parser.parse_args().out.resolve()))
