"""Existing independent retrieval fixture, copied without changing its facts or queries."""

import json
from sessiongraph.core import Store, init_project, load_config
from sessiongraph.extraction import run_one


def build(root, suite):
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

    store.close()
    return root / ".sessiongraph/config.json", source_ids
