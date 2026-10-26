"""Reproducible provider-free three-arm benchmark; hand-authored graph is NOT model quality."""

import argparse
import hashlib
import json
import socket
import statistics
import tempfile
import time
from pathlib import Path

from sessiongraph.core import Store, init_project, load_config
from sessiongraph.extraction import run_one


def run(suite_path, out):
    raw = suite_path.read_bytes()
    suite = json.loads(raw)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sessiongraph-benchmark-") as temp:
        root = Path(temp)
        store = Store(load_config(init_project(root)))
        source_ids = {}
        for thread in suite["threads"]:
            path = root / f"{thread['id']}.jsonl"
            rows = [{"type": "session_meta", "payload": {"id": thread["id"], "cwd": str(root)}}]
            for message in thread["messages"]:
                rows.append(
                    {
                        "type": "response_item",
                        "timestamp": "2026-10-03T00:00:00Z",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": message["text"]}],
                        },
                    }
                )
            path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
            store.ingest(path)
            entries = store.db.execute(
                "SELECT * FROM entries WHERE session_id=? ORDER BY line", (thread["id"],)
            ).fetchall()
            source_ids.update({m["key"]: e["id"] for m, e in zip(thread["messages"], entries)})
        store.enqueue()

        def fixture_extractor(batch, policy):
            thread = next(t for t in suite["threads"] if t["id"] == batch[0]["session_id"])
            records = []
            messages = {m["key"]: m for m in thread["messages"]}
            for message in thread["messages"]:
                if "kind" not in message:
                    continue
                records.append(
                    {
                        "id": message["key"],
                        "kind": message["kind"],
                        "content": message["text"],
                        "scope": "合成测试",
                        "aliases": message["aliases"],
                        "evidence": [
                            {"entry_id": source_ids[message["key"]], "quote": message["text"]}
                        ],
                    }
                )
            edges = [
                {
                    "source": a,
                    "kind": relation,
                    "target": b,
                    "evidence": [
                        {"entry_id": source_ids[a], "quote": messages[a]["text"]},
                        {"entry_id": source_ids[b], "quote": messages[b]["text"]},
                    ],
                }
                for a, relation, b in thread["edges"]
            ]
            return {
                "summary": "Synthetic fixture, not model output",
                "records": records,
                "relations": edges,
            }

        while True:
            result = run_one(store, fixture_extractor)
            if result["state"] == "idle":
                break
            assert result["state"] == "done", result

        def blocked(*args, **kwargs):
            raise AssertionError("No network allowed during benchmark")

        previous = socket.socket
        socket.socket = blocked
        try:
            rows = []
            for case in suite["queries"]:
                row = dict(case)
                gold = {source_ids[k] for k in case["gold"]}
                for mode in ("raw", "hybrid", "graph"):
                    response = store.search(case["query"], limit=3, max_chars=1800, mode=mode)
                    assert response["budget"]["returned_text_chars"] <= 1800
                    hits = response["hits"] + response["related"]
                    evidence = []
                    for hit in hits:
                        evidence.extend(
                            [hit["id"]]
                            if hit["type"] == "entry"
                            else [e["entry_id"] for e in hit["evidence"]]
                        )
                    found = set(evidence)
                    row[mode] = {
                        "covered": len(gold & found),
                        "gold_count": len(gold),
                        "complete": bool(gold) and gold <= found,
                        "candidate_count": len(hits),
                        "irrelevant_sources": len(found - gold),
                        "source_ids": sorted(found),
                        "response_chars": len(json.dumps(response, ensure_ascii=False)),
                    }
                rows.append(row)
            timings = {mode: [] for mode in ("raw", "hybrid", "graph")}
            for repeat in range(15):
                modes = ["raw", "hybrid", "graph"]
                modes = modes[repeat % 3 :] + modes[: repeat % 3]
                for case in suite["queries"]:
                    for mode in modes:
                        start = time.perf_counter()
                        store.search(case["query"], limit=3, max_chars=1800, mode=mode)
                        timings[mode].append((time.perf_counter() - start) * 1000)
        finally:
            socket.socket = previous
        summary = {}
        for mode in ("raw", "hybrid", "graph"):
            pos = [r for r in rows if r["gold"]]
            neg = [r for r in rows if not r["gold"]]
            summary[mode] = {
                "complete_positive_queries": sum(r[mode]["complete"] for r in pos),
                "positive_queries": len(pos),
                "covered_gold_sources": sum(r[mode]["covered"] for r in pos),
                "gold_sources": sum(r[mode]["gold_count"] for r in pos),
                "irrelevant_sources_positive": sum(r[mode]["irrelevant_sources"] for r in pos),
                "negative_queries_with_candidates": sum(
                    r[mode]["candidate_count"] > 0 for r in neg
                ),
                "negative_queries": len(neg),
                "median_json_chars": statistics.median(r[mode]["response_chars"] for r in rows),
                "median_search_ms": round(statistics.median(timings[mode]), 3),
            }
        report = {
            "dataset_sha256": hashlib.sha256(raw).hexdigest(),
            "fixture_only": True,
            "new_model_calls": 0,
            "network_blocked": True,
            "limits": {"seed_limit": 3, "max_text_chars": 1800, "graph_related_limit": 8},
            "status": store.status(),
            "rows": rows,
            "summary": summary,
            "limitations": [
                "Synthetic alias and relation tests, not blinded natural-language evaluation",
                "Graph can return more items but uses the same total text budget",
                "Candidate-source coverage is not answer accuracy; unrelated graph nodes count as noise",
            ],
        }
        (out / "synthetic-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        store.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--suite", type=Path, default=Path(__file__).with_name("retrieval_suite.json"))
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    run(args.suite, args.output)
