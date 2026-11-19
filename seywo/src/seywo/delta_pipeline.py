"""Seywo delta-v1: one native turn in, one compact semantic delta out.

The model extracts only semantic changes. The runtime owns all structure and
source binding, so compact publication does not require a second organizer call.
"""

from copy import deepcopy
import json
import re
from pathlib import Path
import time

from . import inbox
from .delta_model import POLICY, instructions, request_payload, validate
from .hook import chat_identity
from .model_contract import model_eligible
from .narrative import Narrative, empty
from .narrative_contract import validate_layout
from .phase_integrity import OBJECTIVE_TYPES, validate_phase_changes
from .store import digest, encode
from .understanding import Understanding


def _write_state(path, state):
    temporary = Path(path).with_name("." + Path(path).name + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)

PIPELINE = "seywo-delta-v1"
MAX_TURN_CHARS = 16000
MAX_RECENT_TITLES = 8
UNRESOLVED_TYPES = {"question", "goal", "action", "next_step", "correction"}


def _source(row):
    return {
        "event_id": row["event_id"], "snapshot_id": row["snapshot_id"],
        "kind": row["kind"], "role": row["role"], "channel": row["channel"],
        "state": row["state"], "reason": row["reason"], "turn_id": row["turn_id"],
        "turn_seq": row["turn_seq"], "text": row["safe_text"],
        "start": 0, "end": len(row["safe_text"]), "ordinal": row["ordinal"],
        "record_no": row["record_no"],
    }


def _fresh_rows(store, chat_id, cursor):
    # Inspect identities first. Historical and tool bodies never need decoding
    # merely to determine whether a compact has new model-eligible input.
    fields = ("row_id,event_id,snapshot_id,kind,role,channel,state,reason,turn_id,turn_seq,"
              "ordinal,record_no,segment_id,raw_sha256,occurred_at")
    after = " AND (ordinal>? OR (ordinal=? AND record_no>?))" if cursor is not None else ""
    args = (chat_id, cursor[0], cursor[0], cursor[1]) if cursor is not None else (chat_id,)
    rows = [dict(r) for r in store.db.execute(
        f"SELECT {fields} FROM current_sources WHERE chat_id=?{after} ORDER BY ordinal,record_no", args)]
    if not rows:
        return []
    from collections import Counter

    def occurrence(row):
        native = row["turn_id"] or row["occurred_at"]
        return (native, row["raw_sha256"]) if native else None

    seen = Counter()
    if cursor is not None:
        previous = store.db.execute(
            "SELECT turn_id,occurred_at,raw_sha256 FROM current_sources WHERE chat_id=? "
            "AND (ordinal<? OR (ordinal=? AND record_no<=?))", args)
        seen.update(key for row in previous if (key := occurrence(row)) is not None)
    fresh, segment, leading, available = [], None, True, Counter()
    for row in rows:
        if row["segment_id"] != segment:
            segment, leading, available = row["segment_id"], True, seen.copy()
        key = occurrence(row)
        if cursor is not None and leading and key is not None and available[key]:
            available[key] -= 1
            continue
        if row["state"] != "excluded_mechanical":
            leading = False
        if key is not None:
            seen[key] += 1
        row["safe_text"] = ""
        fresh.append(row)
    eligible = [r for r in fresh if model_eligible(r)]
    for offset in range(0, len(eligible), 512):
        batch = {r["row_id"]: r for r in eligible[offset:offset + 512]}
        placeholders = ",".join("?" for _ in batch)
        for row in store.db.execute(
                "SELECT f.row_id,seywo_source_text(p.data) text FROM source_fragments f "
                "JOIN source_payloads p ON p.id=f.payload_id "
                f"WHERE f.row_id IN ({placeholders})", tuple(batch)):
            batch[row["row_id"]]["safe_text"] = row["text"]
    return fresh


def _turn_groups(rows):
    groups = []
    by_turn = {}
    for row in rows:
        source = _source(row)
        key = source["turn_id"] or f"record:{source['ordinal']}:{source['record_no']}"
        by_turn.setdefault(key, []).append(source)
    for key, sources in by_turn.items():
        eligible = [s for s in sources if model_eligible(s)]
        if eligible:
            groups.append((key, eligible, sources[-1]))
        else:
            groups.append((key, [], sources[-1]))
    return groups


def _doc_context(doc):
    phases = doc.get("phases", [])
    focus = doc.get("overview", {}).get("focus", {})
    phase = next((p["title"] for p in phases if p["id"] == focus.get("phase_id")), None)
    recent = [i["title"] for i in doc.get("items", [])[-MAX_RECENT_TITLES:]]
    goals = set(doc.get("overview", {}).get("goal_item_ids", []))
    return {"phase": phase, "goals": [i["title"] for i in doc.get("items", []) if i["id"] in goals][:2], "focus": {"phase": phase, "branch": focus.get("branch_id")},
            "open": list(doc.get("overview", {}).get("unresolved", []))[:4], "recent": recent}


def _source_key(source):
    return source["event_id"], source["snapshot_id"], source["start"], source["end"]


def _existing_source_keys(doc):
    return {_source_key(e) for item in doc.get("items", []) for e in item.get("evidence", [])}


def _new_id(task_id, ordinal):
    return "delta:" + digest(f"{task_id}:{ordinal}")[:32]


def _phase_id(task_id, ordinal):
    return "delta-phase:" + digest(f"{task_id}:phase:{ordinal}")[:32]


def _branch_id(task_id, ordinal):
    return "delta-branch:" + digest(f"{task_id}:branch:{ordinal}")[:32]


def _transition_id(task_id, ordinal):
    return "delta-transition:" + digest(f"{task_id}:transition:{ordinal}")[:32]


def _evidence(source):
    # Quote text is intentionally absent. The source endpoint resolves ranges
    # when a reader asks for evidence, and the model never copies source text.
    return {"event_id": source["event_id"], "snapshot_id": source["snapshot_id"],
            "start": source["start"], "end": source["end"], "relation": "supports"}


def apply_delta(previous, output, sources, task_id):
    doc = deepcopy(previous) if previous else empty()
    doc.setdefault("identity_changes", [])
    doc.setdefault("pending_revisions", [])
    doc.setdefault("resolution_log", [])
    previous_keys = _existing_source_keys(doc)
    existing_keys = set(previous_keys)
    added = []
    added_by_model_index = []
    for ordinal, item in enumerate(output["items"]):
        refs = [sources[i] for i in item["source_indexes"]]
        if all(_source_key(_evidence(s)) in previous_keys for s in refs):
            added_by_model_index.append(None)
            continue
        if item["attribution"] == "user" and not any(s["role"] == "user" for s in refs):
            raise ValueError("delta_user_requires_user_source")
        identity = _new_id(task_id, ordinal)
        attr = {"user": "user_statement", "assistant": "assistant_statement", "inference": "inference"}[item["attribution"]]
        node = {"id": identity, "type": item["type"], "title": item["title"],
                "content": item["title"], "attribution": attr, "scope": "chat",
                "evidence": [_evidence(s) for s in refs], "dependencies": [],
                "turn_id": refs[0].get("turn_id"), "hidden": False, "origin": "model",
                "user_confirmed": False}
        doc["items"].append(node)
        for ref in node["evidence"]:
            existing_keys.add(_source_key(ref))
        added.append(node)
        added_by_model_index.append(node)

    # Coverage is a runtime invariant: a model omission cannot erase a primary
    # human request. The fallback is deliberately mechanical and short; normal
    # titles still come from the model.
    user_sources = [source for source in sources if source["role"] == "user"]
    for source_ordinal, source in enumerate(user_sources):
        ref = _evidence(source)
        if _source_key(ref) in existing_keys or sources.index(source) in output["no_work_source_indexes"]:
            continue
        text = re.sub(r"\s+", " ", source.get("text", "")).strip().lstrip("-• ")
        title = text[:39].rstrip() + "…" if len(text) > 40 else text
        if not title:
            title = "用户请求"
        item_type = "question" if any(token in text for token in ("？", "?", "为什么", "如何", "能否", "是否", "哪些", "怎么")) else "goal"
        node = {"id": _new_id(task_id, len(output["items"]) + source_ordinal), "type": item_type, "title": title,
                "content": title, "attribution": "user_statement", "scope": "chat",
                "evidence": [ref], "dependencies": [], "turn_id": source.get("turn_id"),
                "hidden": False, "origin": "runtime_coverage", "user_confirmed": False}
        doc["items"].append(node)
        existing_keys.add(_source_key(ref))
        added.append(node)

    phases = doc["phases"]
    branches = doc["branches"]
    overview = doc["overview"]
    focus = overview["focus"]
    action = output["line"]["action"]
    title = output["line"]["title"].strip() or "当前工作"
    current_phase = next((p for p in phases if p["id"] == focus.get("phase_id")), None)
    transition = None
    if not added:
        # A replay/no-work turn must not create structure or change focus.
        return doc, 0
    if not phases:
        current_phase = {"id": _phase_id(task_id, 0), "title": title,
                         "goal_item_ids": []}
        phases.append(current_phase)
        focus = {"phase_id": current_phase["id"], "branch_id": None}
    elif action == "new_phase":
        anchor_index = output["focus_item"]
        anchor = added_by_model_index[anchor_index] if anchor_index is not None else None
        if (not anchor or anchor["attribution"] != "user_statement"
                or anchor["type"] not in OBJECTIVE_TYPES):
            raise ValueError("new_phase_requires_user_objective")
        old_phase = current_phase
        old_branch_id = focus.get("branch_id")
        current_phase = {"id": _phase_id(task_id, len(phases)), "title": title,
                         "goal_item_ids": [anchor["id"]]}
        phases.append(current_phase)
        focus = {"phase_id": current_phase["id"], "branch_id": None}
        if added and old_phase:
            transition = {"kind": "goal_change", "from_phase_id": old_phase["id"], "from_branch_id": old_branch_id,
                          "to_phase_id": current_phase["id"], "to_branch_id": None,
                          "result_item_ids": [i["id"] for i in added], "unresolved": [],
                          "reason": "native turn entered a new human objective",
                          "evidence_item_ids": [i["id"] for i in added]}
    elif action == "branch" and current_phase:
        branch = {"id": _branch_id(task_id, len(branches)), "phase_id": current_phase["id"],
                  "title": title, "goal": added[-1]["title"] if added else title,
                  "resolve_when": "问题得到明确结果", "state": "active",
                  "outcome_item_ids": [], "evidence_item_ids": [i["id"] for i in added]}
        branches.append(branch)
        focus = {"phase_id": current_phase["id"], "branch_id": branch["id"]}
        if added:
            transition = {"kind": "enter", "from_phase_id": current_phase["id"], "from_branch_id": None,
                          "to_phase_id": current_phase["id"], "to_branch_id": branch["id"],
                          "result_item_ids": [i["id"] for i in added], "unresolved": [],
                          "reason": "native turn opened a bounded side line",
                          "evidence_item_ids": [i["id"] for i in added]}
    elif action == "return":
        old_branch = next((b for b in branches if b["id"] == focus.get("branch_id")), None)
        if old_branch and added:
            transition = {"kind": "return", "from_phase_id": old_branch["phase_id"], "from_branch_id": old_branch["id"],
                          "to_phase_id": old_branch["phase_id"], "to_branch_id": None,
                          "result_item_ids": [i["id"] for i in added], "unresolved": [],
                          "reason": "native turn returned to the mainline",
                          "evidence_item_ids": [i["id"] for i in added]}
        focus = {"phase_id": focus.get("phase_id"), "branch_id": None}

    current_phase = next(p for p in phases if p["id"] == focus["phase_id"])
    for node in added:
        placement = {"item_id": node["id"], "phase_id": focus["phase_id"], "branch_id": focus.get("branch_id")}
        doc["placements"].append(placement)
        if node["type"] in {"goal", "question", "decision"} and node["id"] not in current_phase["goal_item_ids"]:
            current_phase["goal_item_ids"].append(node["id"])
    if transition:
        transition["id"] = _transition_id(task_id, len(doc["transitions"]))
        doc["transitions"].append(transition)

    old_open = [v for v in overview.get("unresolved", []) if v not in output["resolve_titles"]]
    new_open = [added_by_model_index[i]["title"] for i in output["open_item_indexes"]
                if i < len(added_by_model_index) and added_by_model_index[i] is not None
                and added_by_model_index[i]["type"] in UNRESOLVED_TYPES]
    overview["unresolved"] = list(dict.fromkeys(old_open + new_open))
    overview["focus"] = focus
    user_focus = [i for i in added if i["attribution"] == "user_statement"
                  and i["type"] in {"question", "goal", "decision", "correction", "action"}]
    focus_candidates = user_focus[-1:] if user_focus else [added_by_model_index[i] for i in ([output["focus_item"]] if output["focus_item"] is not None else range(len(added_by_model_index)))
                       if i < len(added_by_model_index) and added_by_model_index[i] is not None]
    if focus_candidates:
        overview["goal_item_ids"] = [focus_candidates[-1]["id"]]
    else:
        overview["goal_item_ids"] = overview.get("goal_item_ids", [])
    overview["open_question_ids"] = [i["id"] for i in added if i["type"] == "question" and i["title"] in overview["unresolved"]]
    overview["next_step_ids"] = [i["id"] for i in added if i["type"] in {"action", "next_step", "goal"}]
    doc["model_policy"] = POLICY
    doc["published_gaps"] = []
    doc["pending_organization_count"] = 0
    validate_layout(doc)
    validate_phase_changes(previous or empty(), doc)
    return doc, len(added)


def _import_claimed(store, config, chat_id, claimed):
    from .locking import file_lock
    with file_lock(inbox.root(config) / ".lock"):
        return _import_owned(store, config, chat_id, claimed)


def _import_owned(store, config, chat_id, claimed):
    source = claimed.get("snapshot") or claimed["transcript"]
    path = Path(source)
    owner = chat_identity(str(path), chat_id)
    if owner != chat_id:
        inbox.record(config, owner, claimed["event"], str(path))
        inbox.finish(config, claimed, state="done", error="rerouted_chat")
        return False
    if not path.is_file():
        inbox.finish(config, claimed, state="failed", error="source_missing")
        return False
    from .archive import content_capture
    try:
        path, manifest = content_capture(path, inbox.root(config) / "live", chat_id,
                                         limit=inbox.SNAPSHOT_LIMIT)
    except (OSError, ValueError) as exc:
        inbox.finish(config, claimed, state="failed",
                     error=str(exc)[:120] or "source_capture_failed")
        return False
    if str(path) != claimed.get("snapshot"):
        claimed = inbox.relocate(config, claimed, path)
    try:
        store._import_compact_capture(path, manifest, chat_id, claimed["transcript"])
    except (OSError, ValueError) as exc:
        inbox.finish(config, claimed, state="failed",
                     error=str(exc)[:120] or "source_import_failed")
        return False
    inbox.finish(config, claimed, state="done", error=None)
    return True


def _save_progress(store, chat_id, state):
    store.db.execute(
        "INSERT INTO delta_progress VALUES(?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET "
        "cursor_json=excluded.cursor_json,version=excluded.version,windows=excluded.windows",
        (chat_id, encode(state.get("cursor")), state.get("version", 0), state.get("windows", 0)))


def run_delta(config, chat_id, state, state_path, transport, report):
    from .locking import file_lock, lifecycle
    with lifecycle(config), file_lock(Path(state_path).with_suffix(".delta.lock")):
        return _run_delta(config, chat_id, state, state_path, transport, report)


def _run_delta(config, chat_id, state, state_path, transport, report):
    with __import__("seywo.store", fromlist=["Store"]).Store(config) as store:
        store.require_human_chat(chat_id)
        quiet = 0
        while True:
            pending = inbox.pending(config, chat_id)
            if not pending:
                quiet += 1
                if quiet >= 2:
                    break
                time.sleep(0.1)
                continue
            quiet = 0
            for item in pending:
                claimed = inbox.claim(config, item)
                if claimed:
                    _import_claimed(store, config, chat_id, claimed)
        progress = store.db.execute("SELECT * FROM delta_progress WHERE chat_id=?", (chat_id,)).fetchone()
        if progress:
            state.update(cursor=json.loads(progress["cursor_json"]), version=progress["version"], windows=progress["windows"])
        rows = _fresh_rows(store, chat_id, state.get("cursor"))
        groups = _turn_groups(rows)
        if not groups:
            _write_state(state_path, state)
            report.update(state="done", calls=0, pipeline=PIPELINE, version=state.get("version"))
            return report
        service = Narrative(store)
        current = service._body(chat_id, service._head(chat_id)[0])
        calls = 0
        for turn_key, sources, tail in groups:
            next_cursor = [tail["ordinal"], tail["record_no"]]
            if not sources:
                state["cursor"] = next_cursor
                with store.db:
                    _save_progress(store, chat_id, state)
                _write_state(state_path, state)
                continue
            base_head = service._head(chat_id)
            current = service._body(chat_id, base_head[0])
            payload = request_payload(sources, _doc_context(current))
            result = transport.complete_delta(model=transport.model, instructions=instructions(), payload=payload)
            calls += 1
            report.setdefault("usage", []).append(result.get("usage"))
            if result.get("error") or result.get("output") is None:
                report.update(state="partial", error_code=result.get("error") or "delta_empty", calls=calls, pipeline=PIPELINE)
                _write_state(state_path, state)
                return report
            try:
                output = validate(result["output"], len(sources))
                task_id = "delta-run:" + digest(f"{chat_id}:{turn_key}:{state['cursor']}:{POLICY}")[:32]
                current, added = apply_delta(current, output, sources, task_id)
            except ValueError as exc:
                report.update(state="partial", error_code=str(exc)[:100], calls=calls, pipeline=PIPELINE)
                _write_state(state_path, state)
                return report
            with store.db:
                store.db.execute("BEGIN IMMEDIATE")
                head, override = service._head(chat_id)
                if (head, override) != base_head:
                    report.update(state="partial", error_code="narrative_base_changed", calls=calls, pipeline=PIPELINE)
                    return report
                if current and added:
                    service._write(chat_id, head + 1, override, current,
                                   encode(Understanding(store)._heads(chat_id)),
                                   task_id=task_id, origin="model_delta_v1")
                    state["version"] = head + 1
                state["cursor"] = next_cursor
                state["windows"] = state.get("windows", 0) + 1
                _save_progress(store, chat_id, state)
            _write_state(state_path, state)
        report.update(state="done", calls=calls, pipeline=PIPELINE,
                      version=state.get("version"), compact_items=len(current.get("items", [])))
        return report


def run_delta_with_lock(config, chat_id, state_path, state, transport, report):
    return run_delta(config, chat_id, state, state_path, transport, report)
