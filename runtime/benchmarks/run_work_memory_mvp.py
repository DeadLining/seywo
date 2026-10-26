"""Quantify the source-backed work-memory MVP after a simulated context reset."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import statistics
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from sessiongraph.core import Store, init_project, load_config

ROOT = Path(__file__).resolve().parents[1]
DOMAINS = ["experiment", "research", "work", "paper"]


def write_transcript(path: Path, chat_id: str, domain: str) -> None:
    text = {
        "experiment": "实验目标是比较两种方案，结果数据在 /workspace/experiment/results.json。",
        "research": "调研目标是比较候选工具，结论报告在 /workspace/research/report.md。",
        "work": "工作目标是完成迁移，变更记录在 /workspace/work/migration.md。",
        "paper": "论文目标是整理方法与局限，笔记在 /workspace/paper/notes.md。",
    }[domain]
    rows = [
        {"type": "session_meta", "payload": {"id": chat_id, "cwd": str(path.parent)}},
        {"timestamp": "2026-10-05T00:00:00Z", "type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]
        }},
    ]
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def build_fixture(out: Path):
    fixture = out / "fixture"
    if fixture.exists():
        shutil.rmtree(fixture)
    fixture.mkdir(parents=True)
    cfg_path = init_project(fixture)
    store = Store(load_config(cfg_path))
    scenarios = []
    for i in range(20):
        domain = DOMAINS[i % len(DOMAINS)]
        chat_id = f"memory-{i:02d}"
        transcript = fixture / f"{chat_id}.jsonl"
        write_transcript(transcript, chat_id, domain)
        ingested = store.ingest(transcript)
        entry = store.db.execute("SELECT id FROM entries WHERE session_id=?", (ingested["session_id"],)).fetchone()[0]
        path = f"/workspace/{domain}/item-{i:02d}.md"
        items = [
            ("goal", "当前目标", f"完成{domain}工作并形成可交付结果", "目标"),
            ("constraint", "当前约束", "必须保留原始依据，不能把建议当成决定", "约束"),
            ("progress", "已完成工作", f"已经完成{domain}资料收集和第一轮核对", "完成"),
            ("decision", "当前决定", "采用来源可追溯的方案，暂不引入未验证的复杂依赖", "决定"),
            ("finding", "关键结论", f"第一轮结果支持继续推进{domain}，但仍需验证边界条件", "结论"),
            ("artifact", "产物位置", path, "位置"),
            ("next_step", "下一步", f"复核{domain}的边界条件并更新交付物", "下一步"),
        ]
        ids = []
        for kind, title, content, marker in items:
            saved = store.remember(
                kind=kind, title=title, content=content, scope=f"{domain}-{i}",
                paths=[path] if kind == "artifact" else [], aliases=[domain, marker],
                source_entry_id=entry, source_quote=text_for_quote(domain), chat_id=chat_id,
            )
            ids.append(saved["id"])
        old = store.remember(
            kind="decision", title="旧决定", content="曾考虑引入复杂依赖", status="superseded",
            scope=f"{domain}-{i}", aliases=[domain], source_entry_id=entry,
            source_quote=text_for_quote(domain), chat_id=chat_id,
        )
        scenarios.append({"id": f"S{i:02d}", "chat_id": chat_id, "domain": domain,
                          "memory_ids": ids, "path": path, "old_id": old["id"]})
    store.close()
    return cfg_path, scenarios


def text_for_quote(domain: str) -> str:
    return {
        "experiment": "实验目标是比较两种方案",
        "research": "调研目标是比较候选工具",
        "work": "工作目标是完成迁移",
        "paper": "论文目标是整理方法与局限",
    }[domain]


def parse_result(result):
    if result.isError:
        raise RuntimeError(str(result.content))
    text = "".join(x.text for x in result.content if getattr(x, "type", None) == "text")
    return json.loads(text)


async def run_stdio(cfg: Path, scenarios):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "sessiongraph", "--config", str(cfg), "mcp"],
        cwd=str(ROOT), env=dict(os.environ),
    )
    rows = []
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            required = {"session_remember", "session_recall", "session_resume"}
            assert required <= names
            for scenario in scenarios:
                start = time.perf_counter()
                result = parse_result(await client.call_tool("session_resume", {
                    "chat_id": scenario["chat_id"], "limit": 20,
                }))
                elapsed = time.perf_counter() - start
                memories = result["memories"]
                by_id = {m["id"]: m for m in memories}
                required_ok = all(mid in by_id for mid in scenario["memory_ids"])
                stale_hidden = scenario["old_id"] not in by_id
                provenance_ok = all(
                    m.get("source", {}).get("entry_id") and m["source"].get("quote")
                    for m in memories
                )
                path_ok = any(scenario["path"] in m.get("paths", []) for m in memories)
                recall_start = time.perf_counter()
                recalled = parse_result(await client.call_tool("session_recall", {
                    "query": scenario["domain"], "chat_id": scenario["chat_id"], "limit": 20,
                }))
                recall_elapsed = time.perf_counter() - recall_start
                rows.append({"scenario": scenario["id"], "domain": scenario["domain"],
                             "resume_seconds": elapsed, "recall_seconds": recall_elapsed,
                             "items": len(memories), "required_items": required_ok,
                             "stale_hidden": stale_hidden, "provenance_ok": provenance_ok,
                             "path_ok": path_ok, "model_calls": result["model_calls"] + recalled["model_calls"]})
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    cfg, scenarios = build_fixture(out)
    (out / "scenarios.json").write_text(json.dumps(scenarios, ensure_ascii=False, indent=2) + "\n")
    # Control is the same post-reset task without loading the work-memory rows:
    # it has no durable state to evaluate, therefore 0/20 recoverable scenarios.
    store = Store(load_config(cfg), readonly=True)
    store.close()
    rows = asyncio.run(run_stdio(cfg, scenarios))
    resume_times = [r["resume_seconds"] for r in rows]
    recall_times = [r["recall_seconds"] for r in rows]
    def p95(values):
        return sorted(values)[max(0, int(len(values) * .95) - 1)]
    report = {
        "kind": "work_memory_mvp_context_reset",
        "scenarios": len(rows), "domains": DOMAINS,
        "control_without_memory": {"scenarios_passed": 0, "scenarios_total": len(rows)},
        "mvp": {
            "scenarios_passed": sum(r["required_items"] and r["stale_hidden"] and r["provenance_ok"] and r["path_ok"] for r in rows),
            "required_item_recall": sum(r["required_items"] for r in rows) / len(rows),
            "current_state_accuracy": sum(r["stale_hidden"] for r in rows) / len(rows),
            "provenance_accuracy": sum(r["provenance_ok"] for r in rows) / len(rows),
            "artifact_path_accuracy": sum(r["path_ok"] for r in rows) / len(rows),
            "model_calls": sum(r["model_calls"] for r in rows),
            "resume_p50_ms": statistics.median(resume_times) * 1000,
            "resume_p95_ms": p95(resume_times) * 1000,
            "recall_p50_ms": statistics.median(recall_times) * 1000,
            "recall_p95_ms": p95(recall_times) * 1000,
        },
        "rows": rows,
        "limitations": [
            "This measures deterministic memory storage/retrieval and provenance, not answer-model prose quality.",
            "Writes are explicit session_remember calls; automatic LLM extraction at compaction is not included in this MVP.",
            "The fixture is provider-free and uses four synthetic domain scenarios repeated five times.",
        ],
    }
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit("Retired: unsupported fixture claims and unmeasured baseline. "
                     "Use run_continuity_smoke.py for explicitly synthetic contract checks.")
