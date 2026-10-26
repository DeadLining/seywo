"""Compare current raw/hybrid retrieval to a saved Store implementation on one local index."""

import argparse
import importlib.util
import json
import socket
import statistics
import time
from pathlib import Path

from sessiongraph.core import Store, load_config


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--baseline", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    spec = importlib.util.spec_from_file_location("sessiongraph._saved_baseline", args.baseline)
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    config = load_config(args.config)
    stores = {"before": baseline.Store(config), "after": Store(config)}

    def no_network(*args, **kwargs):
        raise AssertionError("network prohibited")

    socket.socket = no_network
    timings = {name: [] for name in stores}
    try:
        for repeat in range(20):
            order = ["before", "after"] if repeat % 2 == 0 else ["after", "before"]
            for query in ["检索 大模型", "沙盒"]:
                for name in order:
                    start = time.perf_counter()
                    stores[name].search(query, limit=8, max_chars=6000)
                    timings[name].append((time.perf_counter() - start) * 1000)
        report = {
            name: {"median_ms": round(statistics.median(values), 3), "calls": len(values)}
            for name, values in timings.items()
        }
        report["method"] = (
            "40 interleaved warm in-process calls per variant, same existing project index; no MCP/cold start"
        )
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        for store in stores.values():
            store.close()


if __name__ == "__main__":
    main()
