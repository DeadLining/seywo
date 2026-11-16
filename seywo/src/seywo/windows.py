"""Deterministic windows over immutable source order and retained B history.

Windows limit new primary input, never delete source/history. Omitted historical
items remain in the private snapshot and cannot be edited through sparse indexes.
"""

import re


def order(value):
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(type(v) is not int or v < 0 for v in value)
    ):
        raise ValueError("invalid_source_window_order")
    return tuple(value)


def value(row, key, default=None):
    """sqlite Row has no .get; mapping-shaped test rows do."""
    try:
        return row[key] if key in row.keys() else default
    except AttributeError:
        return row.get(key, default)


def boundary(position, char=None):
    return (*order(position), float("inf") if char is None else char)


def in_window(row, after, through, after_char=None):
    position = (row["ordinal"], row["record_no"])
    return (
        after is None
        or position > order(after)
        or (position == order(after) and after_char is not None)
    ) and position <= order(through)


def terms(text):
    words = set(re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", text.casefold()))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        words.update(run[i : i + 2] for i in range(len(run) - 1))
    return words


def visible_history(request, related_limit=3):
    """Include new work, current stop/phase goals, and up to three related items.

    Selection is routing only, not semantic identity or permission. No hidden item
    can be revised. Its full body, placements, locks and provenance stay private.
    """
    previous = request["previous"]
    old = previous["items"]
    count = len(old)
    positions = {item["id"]: i for i, item in enumerate(old)}
    selected = set(range(count, count + len(request["incoming"])))
    stop = previous["overview"]
    pinned = stop["goal_item_ids"] + stop["open_question_ids"] + stop["next_step_ids"]
    selected.update(positions[i] for i in pinned)
    incoming_terms = terms("\n".join(i["item"]["title"] for i in request["incoming"]))
    ranked = sorted(
        (
            (len(incoming_terms & terms(item["title"])), n)
            for n, item in enumerate(old)
            if n not in selected
        ),
        reverse=True,
    )
    selected.update(n for score, n in ranked[:related_limit] if score)
    # If the source is repeated, its original identity must be offered even when
    # a human renamed its title. Provenance matching is exact, not a semantic merge.
    anchors = {
        (e["event_id"], e["snapshot_id"], e["start"], e["end"])
        for row in request["incoming"]
        for e in row["item"]["evidence"]
        if e["relation"] in {"supports", "quotes"}
    }
    for n, item in enumerate(old):
        if any(
            (e["event_id"], e["snapshot_id"], e["start"], e["end"]) in anchors
            for e in item["evidence"]
            if e["relation"] in {"supports", "quotes"}
        ):
            selected.add(n)
    selected.update(latest_phase_goals(request))
    aliases = request.get("applied_dependency_ids", {})
    selected.update(
        positions[aliases[d]]
        for row in request["incoming"]
        for d in row["item"]["dependencies"]
        if d in aliases
    )
    pending = list(selected & set(range(count)))
    while pending:
        for dep in old[pending.pop()]["dependencies"]:
            n = positions[dep]
            if n not in selected:
                selected.add(n)
                pending.append(n)
    return sorted(selected)


def check_visible_edits(output, request):
    """Validate each item namespace before full private hydration, including guesses."""
    if "visible_item_indexes" not in request:
        return
    offered = set(request["visible_item_indexes"])
    singles = {"item_index", "target_index"}
    multiples = {
        "goal_item_indexes",
        "outcome_item_indexes",
        "evidence_item_indexes",
        "result_item_indexes",
        "open_question_indexes",
        "next_step_indexes",
        "unresolved_add",
        "item_indexes",
    }

    def walk(value, path=()):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "target_index" and child in (-1, None):
                    # -1 means "no existing counterpart": the binding targets its
                    # own incoming item, so no visibility check applies.
                    continue
                if key in singles:
                    if type(child) is not int or child not in offered:
                        raise ValueError("hidden_item_edit_requires_readback")
                elif key in multiples or (
                    key == "unresolved" and "transitions" in path
                ):
                    if not isinstance(child, list) or any(
                        type(i) is not int or i not in offered for i in child
                    ):
                        raise ValueError("hidden_item_edit_requires_readback")
                else:
                    walk(child, (*path, key))
        elif isinstance(value, list):
            for child in value:
                walk(child, path)

    walk(output)


def scope_catalog(catalog, selected):
    """Bound model visibility without mutating full stored history or renumbering."""
    selected = set(selected)
    hidden = []
    total = len(catalog["items"])
    catalog["items"] = [row for row in catalog["items"] if row["index"] in selected]
    catalog["placements"] = [
        p for p in catalog["placements"] if p["item_index"] in selected
    ]
    fields = {
        "goal_item_indexes",
        "outcome_item_indexes",
        "evidence_item_indexes",
        "result_item_indexes",
        "open_question_indexes",
        "next_step_indexes",
    }
    for kind in ("phases", "branches", "recent_transitions"):
        for n, row in enumerate(catalog[kind]):
            for field in sorted(fields & row.keys()):
                before = row[field]
                row[field] = [i for i in before if i in selected]
                if len(before) != len(row[field]):
                    hidden.append(
                        {
                            "entity": kind,
                            "index": n,
                            "field": field,
                            "count": len(before) - len(row[field]),
                        }
                    )
    catalog["locks"] = [
        lock
        for lock in catalog["locks"]
        if lock["entity"] not in {"item", "placement"} or lock["index"] in selected
    ]
    # Structural locks can contain hidden references. Do not expose unusable
    # sparse references; private validation continues to enforce every lock.
    for lock in catalog["locks"]:
        for field in sorted(fields & lock["fields"].keys()):
            lock["fields"][field] = [i for i in lock["fields"][field] if i in selected]
    # Old transitions cannot be edited. Two recent transitions are enough for
    # this bounded incremental prompt; the complete history stays private.
    archived_transitions = max(0, len(catalog["recent_transitions"]) - 2)
    catalog["recent_transitions"] = catalog["recent_transitions"][-2:]
    hidden = [r for r in hidden if r["entity"] != "recent_transitions"]
    catalog["catalog_scope"] = {
        "visible_item_indexes": sorted(selected),
        "archived_item_count": total - len(selected),
        "hidden_references": hidden,
        "archived_recent_transition_count": archived_transitions,
    }
    return catalog


def row_bounds(row, options):
    position = (row["ordinal"], row["record_no"])
    start = (
        options.after_char
        if options.after_order is not None
        and position == order(options.after_order)
        and options.after_char is not None
        else 0
    )
    end = (
        options.through_char
        if position == order(options.through_order) and options.through_char is not None
        else len(row["safe_text"])
    )
    return start, end


BRIDGE_CHARS = 800


def _span(row, start, end):
    return [[row["ordinal"], row["record_no"], start, end]] if end > start else []


def _merge_spans(spans):
    """Merge overlapping bridge pieces on one source row; keep source order."""
    merged = {}
    for row, start, end in spans:
        if end <= start:
            continue
        key = (row["ordinal"], row["record_no"])
        merged.setdefault(key, (row, []))[1].append((start, end))
    result = []
    for row, pieces in merged.values():
        pieces.sort()
        current = list(pieces[0])
        for start, end in pieces[1:]:
            if start <= current[1]:
                current[1] = max(current[1], end)
            else:
                result.append((row, current[0], current[1]))
                current = [start, end]
        result.append((row, current[0], current[1]))
    return result


def _turn_bridge(group):
    """Bounded semantic anchor: turn's user opening plus assistant ending."""
    from .model_contract import model_eligible

    if not group or value(group[0], "turn_id") is None:
        return []
    users = [r for r in group if model_eligible(r) and r["role"] == "user" and r["safe_text"]]
    assistants = [r for r in group if model_eligible(r) and r["role"] == "assistant" and r["safe_text"]]
    spans = []
    if users:
        row = users[-1]
        spans.append((row, 0, min(len(row["safe_text"]), BRIDGE_CHARS)))
    if assistants:
        row = assistants[-1]
        spans.append((row, max(0, len(row["safe_text"]) - BRIDGE_CHARS), len(row["safe_text"])))
    return _merge_spans(spans)


def plan_windows(rows, max_chars=1400, *, stop_records=()):
    """Exact contiguous coverage, packing whole native turns when possible.

    A native turn boundary is preferred over a character boundary. Turns
    accumulate until the next turn would overflow, then the window closes at the
    prior turn end. An oversized turn is split at a paragraph boundary; the next
    window receives only a bounded bridge (the turn's user opening and the tail
    of the cut point), never tool output. Sources without native turn identity
    keep the character fallback. No characters are skipped.
    """
    from .model_contract import model_eligible

    if type(max_chars) is not int or max_chars <= 0:
        raise ValueError("invalid_window_size")
    windows = []
    after = None
    after_char = None
    used = 0
    pending_bridge = []

    def close(position, char=None, bridge_spans=()):
        nonlocal after, after_char, used, pending_bridge
        windows.append(
            {
                "after_order": after,
                "after_char": after_char,
                "through_order": position,
                "through_char": char,
                "bridge": pending_bridge,
            }
        )
        pending_bridge = []
        for row, start, end in _merge_spans(bridge_spans):
            pending_bridge.extend(_span(row, start, end))
        after, after_char, used = position, char, 0

    # Mechanical rows keep their exact covered position but contribute no model
    # text; the group's last row is still a valid physical window endpoint.
    groups = []
    for row in rows:
        turn = value(row, "turn_id")
        # Legacy sources have no native identity: retain one row per unit so the
        # character fallback stays row-local instead of becoming one fake turn.
        if turn is None or not groups or value(groups[-1][0], "turn_id") != turn:
            groups.append([row])
        else:
            groups[-1].append(row)

    def size(group):
        return sum(len(r["safe_text"]) for r in group if model_eligible(r))

    previous_end = None
    for group_index, group in enumerate(groups):
        if used and used + size(group) > max_chars:
            close(previous_end, bridge_spans=_turn_bridge(groups[group_index - 1]))
        user = next(
            (r for r in group if model_eligible(r) and r["role"] == "user" and r["safe_text"]), None
        )
        for row_index, row in enumerate(group):
            position = (row["ordinal"], row["record_no"])
            previous_end = position
            if not model_eligible(row):
                if position in stop_records:
                    close(position)
                continue
            start = 0
            text = row["safe_text"]
            while start < len(text):
                end = min(len(text), start + max_chars - used)
                if end < len(text):
                    cut = text.rfind("\n", start + (end - start) * 3 // 4, end)
                    if cut >= start:
                        end = cut + 1
                used += end - start
                if end < len(text):
                    anchors = []
                    if value(row, "turn_id") is not None:
                        if user is not None:
                            anchors.append((user, 0, min(len(user["safe_text"]), BRIDGE_CHARS)))
                        anchors.append((row, max(0, end - BRIDGE_CHARS), end))
                    close(position, end, bridge_spans=anchors)
                start = end
            if used >= max_chars or position in stop_records:
                close(position, bridge_spans=_turn_bridge(group[:row_index + 1]))
    if groups and (
        after != previous_end or after_char is not None
    ):
        close(previous_end)
    if not windows:
        windows.append({"after_order": None, "after_char": None,
                        "through_order": None, "through_char": None, "bridge": []})
    for window in windows:
        window.setdefault("bridge", [])
    return windows


def incoming_groups(incoming, applied=()):
    """Topological units; every native turn stays in exactly one unit.

    Dependency order can require another turn first, so turns are ordered as
    nodes and emitted whole. Legacy items without a turn identity remain single
    item units.
    """
    pending = {row["candidate_id"]: row for row in incoming}
    units, owner = [], {}
    for cid, row in pending.items():
        turn = row.get("turn_id")
        if turn is None:
            units.append([cid])
        elif turn not in owner:
            owner[turn] = len(units)
            units.append([cid])
        else:
            units[owner[turn]].append(cid)
    unit_of = {cid: n for n, unit in enumerate(units) for cid in unit}
    dependencies = {
        n: {
            d
            for cid in unit
            for d in pending[cid]["item"]["dependencies"]
            if d in unit_of and unit_of[d] != n
        }
        for n, unit in enumerate(units)
    }
    known, groups = set(applied), []
    remaining = set(range(len(units)))
    while remaining:
        ready = next((n for n in sorted(remaining) if dependencies[n] <= known), None)
        if ready is None:
            raise ValueError("unavailable_batch_dependency")
        batch = [pending[cid] for cid in units[ready]]
        groups.append(batch)
        known.update(row["candidate_id"] for row in batch)
        remaining.remove(ready)
    return groups


def batch_sources(incoming, sources):
    """B sees exact cited intervals for its batch, never future unbound changes.

    A has processed the whole source window. B's source subset is explicit and
    lossless for every evidence anchor; pending items retain their own intervals.
    """
    evidence = [e for row in incoming for e in row["item"]["evidence"]]
    selected = [source for source in sources if source["purpose"] != "primary"]
    bridged = [source for source in sources if source.get("turn_bridge")]
    for source in sources:
        intervals = sorted(
            (max(e["start"], source["start"]), min(e["end"], source["end"]))
            for e in evidence
            if e["event_id"] == source["event_id"]
            and e["snapshot_id"] == source["snapshot_id"]
            and e["start"] < source["end"]
            and source["start"] < e["end"]
        )
        merged = []
        for start, end in intervals:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        for start, end in merged:
            selected.append(
                {
                    **source,
                    "start": start,
                    "end": end,
                    "text": source["text"][
                        start - source["start"] : end - source["start"]
                    ],
                }
            )
    # Context/readback units and the bounded previous-turn bridge stay visible
    # even when no incoming item cites them directly.
    for source in bridged:
        if source not in selected:
            selected.append(source)
    return selected


def goal_orders(request, db):
    rows = []
    kinds = {}
    items = request["previous"]["items"] + [r["item"] for r in request["incoming"]]
    for n, item in enumerate(items):
        if (
            item["type"] not in {"goal", "action", "decision", "question"}
            or item["attribution"] != "user_statement"
        ):
            continue
        orders = []
        for e in item["evidence"]:
            if e["relation"] not in {"supports", "quotes"}:
                continue
            source = db.execute(
                "SELECT s.ordinal,e.record_no FROM source_events e JOIN source_segments s ON s.id=e.segment_id WHERE e.id=?",
                (e["event_id"],),
            ).fetchone()
            if source:
                orders.append([*source, e["start"], e["end"]])
        if orders:
            rows.append({"item_index": n, "order": max(orders)})
            kinds[n] = item["type"]
    # Actions can express a goal from the same user request. A standalone action
    # may instead be a return/suspend instruction, so it cannot force a new goal.
    # Model-declared goals/decisions anchor this consistency check; no title regex.
    requests = {tuple(r["order"][:2]) for r in rows if kinds[r["item_index"]] == "goal"}
    return [
        r
        for r in rows
        if kinds[r["item_index"]] != "action" or tuple(r["order"][:2]) in requests
    ]


def latest_phase_goals(request):
    previous = request["previous"]
    positions = {p["item_id"]: p for p in previous["placements"]}
    by_phase = {}
    focused_phase = previous["overview"]["focus"]["phase_id"]
    for row in request.get("user_goal_order", []):
        n = row["item_index"]
        if n >= len(previous["items"]):
            continue
        item = previous["items"][n]
        place = positions.get(item["id"])
        if (
            not place
            or place["branch_id"] is not None
            or place["phase_id"] != focused_phase
            or item.get("hidden")
        ):
            continue
        by_phase.setdefault(place["phase_id"], []).append(row)
    return sorted(
        r["item_index"]
        for group in by_phase.values()
        for r in group
        if r["order"] == max(v["order"] for v in group)
    )


def factor_copied_history(request, db):
    """Offer one exact readback instead of repeating a copied body on many nodes.

    Only accepted safe dialogue can be read. No summary, shortening or semantic
    merge occurs. Keep a factoring step only when the complete wire is smaller.
    """
    from .model_contract import measured_payload, model_eligible
    from .provider import measure_request
    from .understanding import Understanding

    items = request["previous"]["items"]
    groups = {}
    for n in request["visible_item_indexes"]:
        if n >= len(items):
            continue
        item = items[n]
        support = [
            e for e in item["evidence"] if e["relation"] in {"supports", "quotes"}
        ]
        if (
            item["content"]
            and support
            and item["content"] == "\n".join(e["quote"] for e in support)
        ):
            groups.setdefault(item["content"], []).append(item)
    for copies in groups.values():
        sources = list(request["sources"])
        for item in copies:
            for e in item["evidence"]:
                if e["relation"] not in {"supports", "quotes"}:
                    continue
                row = db.execute(
                    "SELECT * FROM current_sources WHERE event_id=? AND snapshot_id=?",
                    (e["event_id"], e["snapshot_id"]),
                ).fetchone()
                if (
                    row is None
                    or not model_eligible(row)
                    or row["safe_text"][e["start"] : e["end"]] != e["quote"]
                ):
                    continue
                cursor = e["start"]
                covered = sorted(
                    (s["start"], s["end"])
                    for s in sources
                    if s["event_id"] == e["event_id"]
                    and s["snapshot_id"] == e["snapshot_id"]
                )
                gaps = []
                for start, end in covered:
                    if end <= cursor or start >= e["end"]:
                        continue
                    if start > cursor:
                        gaps.append((cursor, min(start, e["end"])))
                    cursor = max(cursor, min(end, e["end"]))
                if cursor < e["end"]:
                    gaps.append((cursor, e["end"]))
                sources.extend(
                    Understanding._source(row, start, end, "evidence_readback")
                    for start, end in gaps
                )
        candidate = {**request, "sources": sources}
        if (
            measure_request(measured_payload(candidate))["tokens"]
            < measure_request(measured_payload(request))["tokens"]
        ):
            request["sources"] = sources


def collapse_gaps(gaps):
    """One gap row per reason, with summed counts.

    Extract keeps per-record gaps because each row is anchored to an event, but
    a long chat then carries hundreds of `unsupported_event` rows into every
    organize request. Organize can only act on the reasons, so send counts.
    """
    counts = {}
    for gap in gaps:
        reason = gap.get("reason") or "source_unavailable"
        counts[reason] = counts.get(reason, 0) + (gap.get("count") or 1)
    return [{"reason": reason, "count": counts[reason]} for reason in sorted(counts)]
