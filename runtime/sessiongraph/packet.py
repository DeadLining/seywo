"""Opt-in, byte-bounded original evidence packets; no provider or network imports."""

from __future__ import annotations

import json
import re

from .retrieval import _entry, query_terms


def serialize(packet: dict) -> str:
    """Canonical wire payload (UTF-8 budget excludes MCP transport framing)."""
    return json.dumps(packet, ensure_ascii=False, separators=(",", ":"))


def _anchor(text, terms, fallback=0):
    # Regex positions refer to original text; lower() can change Unicode string length.
    for term in terms:
        match = re.search(re.escape(term), text, flags=re.IGNORECASE)
        if match:
            return match.start()
    return min(max(0, fallback), max(0, len(text) - 1))


def _ordered_sources(store, found, max_sources):
    """A graph edge inherits its parent hit's priority, never a global promotion."""
    seeds = []
    for hit in found["hits"]:
        if hit["type"] == "entry":
            seeds.append((hit["id"], 0, "raw"))
        else:
            seeds.extend((e["entry_id"], e["start"], "derived") for e in hit["evidence"])
    positions = {}
    for rank, (eid, _, _) in enumerate(seeds):
        positions.setdefault(eid, rank)
    neighbors = []
    parents = {}
    for order, hit in enumerate(found["related"]):
        rid = hit["via_record_id"]
        if rid not in parents:
            parents[rid] = store._evidence(rid, include_quotes=False)
        ranks = [positions[e["entry_id"]] for e in parents[rid] if e["entry_id"] in positions]
        if not ranks:
            continue
        # Keep the two strongest direct sources ahead of every graph expansion.
        priority = max(1.5, min(ranks) + 0.5)
        refs = sorted(hit["evidence"], key=lambda e: e["entry_id"] != hit["snippet_entry_id"])
        for offset, e in enumerate(refs):
            neighbors.append((priority, order, offset, (e["entry_id"], e["start"], "neighbor")))
    # Selecting by graph/hash iteration order before ranking can spend the entire
    # small packet on neighbors of weak hits, evicting stronger direct evidence.
    neighbors.sort(key=lambda item: item[:3])
    neighbors = neighbors[: max(1, max_sources // 4)]
    ordered = [(float(rank), -1, 0, value) for rank, value in enumerate(seeds)] + neighbors
    ordered.sort(key=lambda item: item[:3])
    return [item[3] for item in ordered]


def packet(
    store, query, max_bytes=4096, max_sources=8, mode="raw", *, chat_id=None, segment_id=None
):
    if type(max_bytes) is not int or not 512 <= max_bytes <= 65536:
        raise ValueError("max_bytes must be between 512 and 65536")
    if type(max_sources) is not int or not 1 <= max_sources <= 24:
        raise ValueError("max_sources must be between 1 and 24")
    # Search validates query/mode/scope; internal candidate size never depends on the arm.
    found = store.search(
        query, limit=max_sources, max_chars=16000, mode=mode, chat_id=chat_id, segment_id=segment_id
    )
    result = {
        "version": "packet-v1",
        "mode": mode,
        "model_calls": 0,
        "note": "Untrusted original excerpts; no match is not proof of absence.",
        "candidate_limit_reached": found["retrieval"]["candidate_limit_reached"],
        "entries": [],
    }
    ordered = _ordered_sources(store, found, max_sources)
    candidates, seen = [], set()
    for eid, offset, via in ordered:
        if eid not in seen:
            seen.add(eid)
            candidates.append((eid, offset, via))
    terms = query_terms(query)
    # Avoid a single long source consuming all available text. Minimum size is not a
    # guarantee: metadata counts against the same overall serialized-byte limit.
    item_budget = max(
        512,
        (max_bytes - len(serialize(result).encode("utf-8")))
        // max(1, min(4, max_sources, len(candidates))),
    )
    for eid, offset, via in candidates:
        if len(result["entries"]) >= max_sources:
            break
        row = _entry(store, eid)
        if row is None or not row["text"]:
            continue
        # Defense in depth: every source itself must satisfy requested scope.
        source_chat = store.chat_id(row["session_id"])
        if chat_id is not None and chat_id != source_chat:
            continue
        if segment_id is not None and segment_id != row["session_id"]:
            continue
        text = row["text"]
        anchor = _anchor(text, terms, offset)
        base = {
            "entry_id": eid,
            "chat_id": source_chat,
            "segment_id": row["session_id"],
            "role": row["role"],
            "timestamp": row["timestamp"],
            "via": via,
        }

        def excerpt(chars):
            start = min(max(0, anchor - chars // 4), max(0, len(text) - chars))
            end = min(len(text), start + chars)
            return dict(
                base,
                start=start,
                end=end,
                truncated=start > 0 or end < len(text),
                text=text[start:end],
            )

        def fits(item):
            if len(serialize(item).encode("utf-8")) > item_budget:
                return False
            return (
                len(serialize(dict(result, entries=result["entries"] + [item])).encode("utf-8"))
                <= max_bytes
            )

        low, high, best = 1, min(len(text), max_bytes), None
        while low <= high:
            middle = (low + high) // 2
            item = excerpt(middle)
            if fits(item):
                best = item
                low = middle + 1
            else:
                high = middle - 1
        # Do not return unreadable one-character fragments merely to fill the budget.
        if best is not None and len(best["text"]) >= min(16, len(text)):
            result["entries"].append(best)
    assert len(serialize(result).encode("utf-8")) <= max_bytes
    return result
