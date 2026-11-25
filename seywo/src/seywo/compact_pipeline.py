"""Compact-level Seywo pipeline.

One Compact becomes one bounded semantic batch. Rules remove mechanical source
records and build user/assistant turns; the model groups those turns into a few
conversation segments and emits only short changes with turn indexes. Runtime
owns identity, evidence, structure, publication, and cursor advancement.
"""

from copy import deepcopy
import json
import re
from pathlib import Path
import time

from . import compact_schema, inbox
from .admission import NonHumanSource
from .compact_model import (
    MAX_OUTPUT_TOKENS, MAX_REPAIRS, POLICY, CompactValidationError,
    request_payload, instructions, validate, repair_instructions,
)
from .compact_sources import _source
from .compact_turns import closed_rows, commit_turns
from .compact_input import batch_turns as _batch_turns
from .compact_identity import canonical_item as _canonical_item
from .hook import chat_identity
from .model_contract import model_eligible
from .narrative import Narrative, empty
from .narrative_contract import validate_layout
from .phase_integrity import OBJECTIVE_TYPES, validate_phase_changes
from .store import digest, encode
from .understanding import Understanding

PIPELINE = "seywo-compact-v1"
UNRESOLVED_TYPES = {"question", "goal", "action", "next_step", "correction"}
_COMPLETED_ASSISTANT = re.compile(r"(?:已(?:经)?(?:实现|完成|修复|验证|解决|关闭|通过|恢复|正常)|确认(?:完成|通过)|问题解决|无遗留)", re.I)
_PENDING_ASSISTANT = re.compile(r"(?:待(?:验证|确认|处理)|尚待|仍(?:需|待)|未(?:完成|解决|验证)|需要继续|还要)", re.I)


def _write_state(path, state):
    temporary = Path(path).with_name("." + Path(path).name + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _source_key(source):
    return source["event_id"], source["snapshot_id"], source["start"], source["end"]


def _turn_packets(rows):
    grouped = []
    by_turn = {}
    order = []
    for row in rows:
        source = _source(row)
        key = source["turn_id"] or f"record:{source['ordinal']}:{source['record_no']}"
        if key not in by_turn:
            by_turn[key] = []
            order.append(key)
        by_turn[key].append(source)
    for key in order:
        sources = by_turn[key]
        # Compact has a stricter boundary than legacy pipelines: only the
        # final user message and final assistant answer enter semantic extraction.
        eligible = [source for source in sources
                    if model_eligible(source) and source.get("channel") in {None, "final"}]
        if not eligible:
            continue
        user = "\n".join(source["text"] for source in eligible if source["role"] == "user")
        assistant = "\n".join(source["text"] for source in eligible if source["role"] == "assistant")
        if not user and not assistant:
            continue
        # Human text is never declared semantically empty by a lexical rule.
        # A short receipt may approve a decision or report work completion.
        grouped.append({"turn_id": key, "user": user, "assistant": assistant, "sources": eligible})
    return grouped


def _state_and_catalog(doc, turns=()):
    from .current_view import catalog_for
    return catalog_for(doc, turns)


def _payload_turns(turns):
    result = []
    for index, turn in enumerate(turns):
        result.append({**turn, "index": index})
    return result


def _evidence(source):
    return {"event_id": source["event_id"], "snapshot_id": source["snapshot_id"],
            "start": source["start"], "end": source["end"], "relation": "supports"}


def _turn_sources(turns, indexes):
    result = []
    for index in indexes:
        if type(index) is int and 0 <= index < len(turns):
            result.extend(turns[index]["sources"])
    seen = set()
    return [source for source in result if not (_source_key(source) in seen or seen.add(_source_key(source)))]


def _ids_for_change(doc, change, turns, catalog):
    sources = _turn_sources(turns, change["turn_indexes"])
    role = {"user": "user", "assistant": "assistant", "inference": None}[change["attribution"]]
    if role:
        sources = [source for source in sources if source["role"] == role]
    if not sources:
        raise ValueError("compact_attribution_source_missing")
    return sources


def _new_id(task_id, ordinal):
    return "compact:" + digest(f"{task_id}:{ordinal}")[:32]


def _phase_id(task_id, ordinal):
    return "compact-phase:" + digest(f"{task_id}:phase:{ordinal}")[:32]


def _branch_id(task_id, ordinal):
    return "compact-branch:" + digest(f"{task_id}:branch:{ordinal}")[:32]


def _transition_id(task_id, ordinal):
    return "compact-transition:" + digest(f"{task_id}:transition:{ordinal}")[:32]


def _locked_fields(locks, entity, identity):
    return {lock["field"] for lock in (locks or [])
            if lock.get("entity") == entity and lock.get("id") == identity}


def _merge_evidence(target, source):
    target.setdefault("evidence", [])
    seen = {_source_key(ref) for ref in target["evidence"]}
    for ref in source.get("evidence", []):
        if _source_key(ref) not in seen:
            target["evidence"].append(ref)
            seen.add(_source_key(ref))


def _apply_change(doc, change, turns, target_id, task_id, ordinal, *, locks=()):
    sources = _ids_for_change(doc, change, turns, [])
    evidence = [_evidence(source) for source in sources]
    operation = change["operation"]
    items = {item["id"]: item for item in doc["items"]}
    if operation != "new" and target_id not in items:
        # Validation permits an unbound update/resolve only for the first
        # materialized item in an empty state. Treat it as a first net-state
        # insertion while keeping the operation in the receipt for auditability.
        if operation not in {"update", "resolve"} or target_id is not None:
            raise ValueError("compact_target_not_found")
        attr = {"user": "user_statement", "assistant": "assistant_statement", "inference": "inference"}[change["attribution"]]
        node = {"id": _new_id(task_id, ordinal), "type": change["type"], "title": change["title"],
                "content": change["title"], "attribution": attr, "scope": "chat", "evidence": evidence,
                "dependencies": [], "turn_id": turns[change["turn_indexes"][0]]["turn_id"], "hidden": False,
                "origin": "compact_model", "user_confirmed": False,
                "status": "open" if change["open"] else "resolved"}
        doc["items"].append(node)
        return node, None, []
    if operation == "new":
        attr = {"user": "user_statement", "assistant": "assistant_statement", "inference": "inference"}[change["attribution"]]
        node = {"id": _new_id(task_id, ordinal), "type": change["type"], "title": change["title"],
                "content": change["title"], "attribution": attr, "scope": "chat", "evidence": evidence,
                "dependencies": [], "turn_id": turns[change["turn_indexes"][0]]["turn_id"], "hidden": False,
                "origin": "compact_model", "user_confirmed": False, "status": "open" if change["open"] else "resolved"}
        doc["items"].append(node)
        return node, None, []
    target = items[target_id]
    prior_title = target.get("title", "")
    _merge_evidence(target, {"evidence": evidence})
    absorbed = []
    if operation == "merge":
        for source_id in change["merge_ids"]:
            if source_id == target_id or source_id not in items:
                raise ValueError("compact_merge_source_not_found")
            source = items[source_id]
            if source.get("type") != target.get("type") or source.get("attribution") != target.get("attribution"):
                raise ValueError("compact_merge_incompatible")
            if "hidden" in _locked_fields(locks, "item", source_id) and not source.get("hidden"):
                raise ValueError("compact_locked_merge_source")
            _merge_evidence(target, source)
            source["hidden"] = True
            absorbed.append(source_id)
        doc.setdefault("identity_changes", []).append({"kind": "item_merge", "from_ids": absorbed, "to_ids": [target_id],
                                                        "actor": "compact_model", "created_at": time.time()})
    if "title" not in _locked_fields(locks, "item", target_id) and operation in {"update", "resolve", "merge"}:
        if change["title"] != prior_title:
            target["title"] = change["title"]
            if "content" not in _locked_fields(locks, "item", target_id):
                target["content"] = change["title"]
    target["status"] = "open" if change["open"] else "resolved"
    return None, target, absorbed


def _ensure_phase(doc, task_id, title):
    if doc["phases"]:
        return next((p for p in doc["phases"] if p["id"] == doc["overview"]["focus"]["phase_id"]), doc["phases"][-1])
    phase = {"id": _phase_id(task_id, 0), "title": title or "当前工作", "goal_item_ids": []}
    doc["phases"].append(phase)
    doc["overview"]["focus"] = {"phase_id": phase["id"], "branch_id": None}
    return phase


def _segment_anchor(doc, segment, related, identities):
    """Bind focus to an item identity, never to a title or a new-item count."""
    index = segment.get("anchor_index")
    if index is not None:
        identity = identities.get(index) if type(index) is int else None
        anchor = next((i for i in doc["items"] if i["id"] == identity and not i.get("hidden")), None)
        if anchor is None:
            raise ValueError("compact_unknown_anchor")
        return anchor
    if segment["action"] not in {"branch", "new_phase"}:
        return None
    # Old saved outputs have no anchor field. Accept only an unambiguous
    # identity-based binding, rather than guessing from semantic title matches.
    if segment["action"] == "new_phase":
        candidates = [i for i in related if i["type"] in OBJECTIVE_TYPES
                      and i["attribution"] == "user_statement"]
    else:
        candidates = [i for i in related if i["type"] != "goal"] or related
        questions = [i for i in candidates if i["type"] == "question"]
        if len(questions) == 1:
            candidates = questions
    if len(candidates) != 1:
        raise ValueError("compact_structure_anchor_required")
    return candidates[0]


def _branch_state(doc, branch, *, active=None, locks=()):
    """Derive completion from canonical work state and activity from focus."""
    items = {i["id"]: i for i in doc["items"]}
    evidence = [items.get(_canonical_item(doc, identity)) for identity in branch["evidence_item_ids"]]
    evidence = [i for i in evidence if i]
    if not evidence or (branch["state"] == "abandoned" and active is None):
        return
    locked = _locked_fields(locks, "branch", branch["id"])
    if all(i.get("status") == "resolved" for i in evidence):
        if "state" not in locked:
            branch["state"] = "resolved"
        if "outcome_item_ids" not in locked:
            branch["outcome_item_ids"] = list(dict.fromkeys(i["id"] for i in evidence))
    else:
        if active is None:
            focus = doc["overview"]["focus"]
            active = focus["phase_id"] == branch["phase_id"] and focus.get("branch_id") == branch["id"]
        if "state" not in locked:
            branch["state"] = "active" if active else "waiting"
        # Reopening work invalidates the current completion, while immutable
        # older snapshots retain its past outcome. A human-pinned resolved
        # state must retain its required outcome for layout integrity.
        if "outcome_item_ids" not in locked and branch["state"] != "resolved":
            branch["outcome_item_ids"] = []


def _apply_segment_structure(doc, segment, related, new_ids, task_id, *, anchor=None, locks=()):
    """Apply every structural action, even when its item delta is empty."""
    action = "return" if segment["action"] == "mainline" else segment["action"]
    if not doc["phases"] and not related and anchor is None:
        if action in {"branch", "new_phase"}:
            raise ValueError("compact_structure_anchor_required")
        return
    initial = not doc["phases"]
    main_goal = next((i for i in related if i["type"] == "goal" and i is not anchor), None)
    current = _ensure_phase(doc, task_id, main_goal["title"] if initial and main_goal else segment["title"])
    focus = deepcopy(doc["overview"]["focus"])
    transition = None
    if initial and action == "new_phase":
        action = "continue"
    if action == "new_phase":
        if (anchor is None or anchor["id"] not in new_ids
                or anchor["id"] not in {item["id"] for item in related}
                or anchor["attribution"] != "user_statement" or anchor["type"] not in OBJECTIVE_TYPES):
            raise ValueError("compact_new_phase_requires_user_objective")
        old_phase = current
        current = {"id": _phase_id(task_id, len(doc["phases"])), "title": segment["title"],
                   "goal_item_ids": [anchor["id"]]}
        doc["phases"].append(current)
        transition = {"kind": "goal_change", "from_phase_id": old_phase["id"],
                      "from_branch_id": focus.get("branch_id"), "to_phase_id": current["id"],
                      "to_branch_id": None, "reason": "compact entered a new human objective",
                      "evidence_item_ids": [anchor["id"]]}
        focus = {"phase_id": current["id"], "branch_id": None}
    elif action == "branch":
        if anchor is None:
            raise ValueError("compact_structure_anchor_required")
        matches = [b for b in doc["branches"] if b["phase_id"] == current["id"]
                   and anchor["id"] in {_canonical_item(doc, i) for i in b["evidence_item_ids"]}]
        if len(matches) > 1:
            raise ValueError("compact_ambiguous_branch_anchor")
        branch = matches[0] if matches else None
        if branch is None:
            branch = {"id": _branch_id(task_id, len(doc["branches"])), "phase_id": current["id"],
                      "title": segment["title"], "goal": anchor["title"],
                      "resolve_when": "问题得到明确结果", "state": "active", "outcome_item_ids": [],
                      "evidence_item_ids": [anchor["id"]]}
            doc["branches"].append(branch)
        # A resolved final item can still have an earlier branch-enter event;
        # historical movement and the item's net completion are independent.
        old_branch = next((b for b in doc["branches"] if b["id"] == focus.get("branch_id")), None)
        if old_branch and old_branch is not branch:
            _branch_state(doc, old_branch, active=False, locks=locks)
        _branch_state(doc, branch, active=True, locks=locks)
        if focus.get("branch_id") != branch["id"]:
            transition = {"kind": "enter", "from_phase_id": current["id"],
                          "from_branch_id": focus.get("branch_id"), "to_phase_id": current["id"],
                          "to_branch_id": branch["id"], "reason": "compact entered an identified side line",
                          "evidence_item_ids": [anchor["id"]]}
        focus = {"phase_id": current["id"], "branch_id": branch["id"]}
    elif action == "return":
        branch = next((b for b in doc["branches"] if b["id"] == focus.get("branch_id")), None)
        if branch:
            _branch_state(doc, branch, active=False, locks=locks)
            transition = {"kind": "return", "from_phase_id": current["id"],
                          "from_branch_id": branch["id"], "to_phase_id": current["id"],
                          "to_branch_id": None, "reason": "compact returned to the mainline",
                          "evidence_item_ids": list(branch["evidence_item_ids"])}
        focus = {"phase_id": current["id"], "branch_id": None}
    doc["overview"]["focus"] = focus
    if transition:
        transition.update(id=_transition_id(task_id, len(doc["transitions"])),
                          result_item_ids=list(dict.fromkeys(i["id"] for i in related)), unresolved=[])
        doc["transitions"].append(transition)


def _place_items(doc, added, structures, *, locks=()):
    """Each item has one owner; broad evidence never dictates branch identity."""
    locations = {p["item_id"]: p for p in doc["placements"]}
    for item in added:
        relevant = [s for s in structures if item["id"] in s["related_ids"]]
        anchored = [s for s in relevant if s["anchor_id"] == item["id"]
                    and s["action"] in {"branch", "new_phase"}]
        chosen = next(iter(anchored or relevant), None)
        if chosen is None or chosen["focus"]["phase_id"] is None:
            raise ValueError("compact_item_placement_required")
        branch = chosen["focus"].get("branch_id")
        if item["type"] == "goal" and not anchored:
            branch = None
        location = {"item_id": item["id"], "phase_id": chosen["focus"]["phase_id"], "branch_id": branch}
        doc["placements"].append(location)
        locations[item["id"]] = location
    # A formerly mainline question can explicitly become a side line. Existing
    # phase ownership and human placement locks remain authoritative.
    for structure in structures:
        if structure["action"] != "branch" or not structure["anchor_id"]:
            continue
        location = locations.get(structure["anchor_id"])
        if location is None:
            continue
        focus = structure["focus"]
        if location["phase_id"] != focus["phase_id"]:
            raise ValueError("compact_branch_anchor_phase_mismatch")
        locked = _locked_fields(locks, "placement", structure["anchor_id"])
        if location["branch_id"] != focus["branch_id"] and "branch_id" in locked:
            raise ValueError("compact_locked_branch_placement")
        location["branch_id"] = focus["branch_id"]
    by_id = {i["id"]: i for i in doc["items"]}
    added_ids = {i["id"] for i in added}
    for phase in doc["phases"]:
        if "goal_item_ids" in _locked_fields(locks, "phase", phase["id"]):
            continue
        # Keep historical goal identities except items explicitly reassigned
        # to a branch in this phase. Only mainline items extend the main goals.
        phase["goal_item_ids"] = [i for i in phase["goal_item_ids"]
                                  if not (i in locations and locations[i]["branch_id"] is not None)]
        for identity, place in locations.items():
            if identity not in added_ids or place["phase_id"] != phase["id"] or place["branch_id"] is not None:
                continue
            if by_id[identity]["type"] in {"goal", "question", "decision"} and identity not in phase["goal_item_ids"]:
                phase["goal_item_ids"].append(identity)


def _normalize_changes(output, turns):
    """Apply deterministic completion semantics before materialization.

    The model remains responsible for deciding what matters and how items
    relate. Runtime only closes an assistant-attributed item when its source
    explicitly reports completion and contains no pending qualifier, avoiding
    stale unresolved nodes from phrases such as “已实现” or “已验证”.
    """
    # v2 explicitly owns semantic state. A completion verb for one subtask
    # must never override the model's open state for a composite historical goal.
    if "current" in output:
        return output
    changes = []
    for change in output["changes"]:
        if change["attribution"] == "assistant" and change["open"]:
            text = " ".join(turns[index].get("assistant", "") for index in change["turn_indexes"])
            if _COMPLETED_ASSISTANT.search(text) and not _PENDING_ASSISTANT.search(text):
                change = {**change, "open": False}
        changes.append(change)
    return {**output, "changes": changes}


def apply_compact(previous, output, turns, catalog, task_id, *, locks=()):
    output = _normalize_changes(output, turns)
    doc = deepcopy(previous) if previous else empty()
    doc.setdefault("identity_changes", [])
    doc.setdefault("pending_revisions", [])
    item_ids = {row["index"]: row["id"] for row in catalog}
    virtual_ids = {}
    effects = []
    added_total, touched_total = [], []
    virtual_count = max(item_ids, default=-1) + 1
    # Apply each model change exactly once. A target may refer to an earlier
    # new change in this same batch, which lets the model describe final state
    # without forcing a second model call or losing the historical item.
    for ordinal, change in enumerate(output["changes"]):
        target_index = change["target_index"]
        if target_index is None:
            target_id = None
        else:
            target_id = item_ids.get(target_index) or virtual_ids.get(target_index)
            if target_id is None:
                raise ValueError("compact_target_not_found")
            target_id = _canonical_item(doc, target_id)
        if change["operation"] == "merge":
            merge_ids = []
            for index in change["merge_indexes"]:
                identity = item_ids.get(index) or virtual_ids.get(index)
                if identity is None:
                    raise ValueError("compact_merge_source_not_found")
                merge_ids.append(_canonical_item(doc, identity))
            change = {**change, "merge_ids": merge_ids}
        node, existing, absorbed = _apply_change(doc, change, turns, target_id, task_id, len(doc["items"]) + ordinal, locks=locks)
        if node:
            virtual_ids[virtual_count] = node["id"]
            virtual_count += 1
            added_total.append(node)
        if existing:
            touched_total.append(existing)
        effects.append({"change": change, "node": node, "existing": existing, "absorbed": absorbed})

    # Content changes are applied once; every structural action independently
    # binds to all relevant item identities, including unchanged history.
    identities = {index: _canonical_item(doc, identity)
                  for index, identity in {**item_ids, **virtual_ids}.items()}
    new_ids = {i["id"] for i in added_total}
    for branch in doc["branches"]:
        _branch_state(doc, branch, locks=locks)
    structures = []
    for segment_index, segment in enumerate(output["segments"]):
        segment_turns = set(segment["turn_indexes"])
        related = {}
        for effect in effects:
            if segment_turns.intersection(effect["change"]["turn_indexes"]):
                item = effect["node"] or effect["existing"]
                if item:
                    identity = _canonical_item(doc, item["id"])
                    related[identity] = next(i for i in doc["items"] if i["id"] == identity)
        related = list(related.values())
        anchor = _segment_anchor(doc, segment, related, identities)
        if segment["action"] == "new_phase" and anchor:
            user_sources = {_source_key(source) for source in _turn_sources(turns, segment["turn_indexes"])
                            if source["role"] == "user"}
            if not any(_source_key(ref) in user_sources for ref in anchor.get("evidence", [])):
                raise CompactValidationError("compact_new_phase_requires_user_objective",
                                             segment_index=segment_index, anchor_index=segment.get("anchor_index"),
                                             reason="anchor_user_evidence_missing_in_segment")
        if segment["action"] != "new_phase" and anchor and anchor["id"] not in {i["id"] for i in related}:
            related.append(anchor)
        _apply_segment_structure(doc, segment, related, new_ids, task_id + f":{segment_index}",
                                 anchor=anchor, locks=locks)
        focus = deepcopy(doc["overview"]["focus"])
        structures.append({"action": segment["action"], "anchor_id": anchor["id"] if anchor else None,
                           "related_ids": {i["id"] for i in related}, "focus": focus})
    _place_items(doc, added_total, structures, locks=locks)

    for lock in locks:
        if lock.get("entity") == "overview" and lock.get("field") == "focus":
            doc["overview"]["focus"] = deepcopy(lock["value"])
    # Chronological actions determine focus; the old redundant focus_segment
    # cannot contradict movement. Human overrides still take precedence.
    # Reconcile activity after overrides, including a phase change away.
    focus = doc["overview"]["focus"]
    for branch in doc["branches"]:
        active = branch["phase_id"] == focus["phase_id"] and branch["id"] == focus.get("branch_id")
        if branch["state"] in {"active", "waiting"} or active:
            _branch_state(doc, branch, active=active, locks=locks)
    from .current_view import project_current
    project_current(doc, output.get("current"), {**item_ids, **virtual_ids}, locks=locks)
    validate_layout(doc)
    validate_phase_changes(previous or empty(), doc)
    return doc, len(added_total), len(touched_total)

def _receipt_id(chat_id, before, after):
    return "compact-batch:" + digest(encode({"chat": chat_id, "before": before, "after": after, "policy": POLICY}))[:32]


def _record(db, values, model_runs=()):
    values = list(values)
    values[14:16] = ['[]', '[]']
    values.append(encode(list(model_runs)))
    db.execute("""INSERT INTO compact_batches
        (id,chat_id,cursor_before,cursor_after,base_version,published_version,state,turn_count,segment_count,model_calls,
         input_chars,output_chars,input_tokens,output_tokens,segments_json,changes_json,error_code,created_at,updated_at,model_runs_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET cursor_after=excluded.cursor_after,published_version=excluded.published_version,
        state=excluded.state,segment_count=excluded.segment_count,model_calls=excluded.model_calls,input_chars=excluded.input_chars,
        output_chars=excluded.output_chars,input_tokens=excluded.input_tokens,output_tokens=excluded.output_tokens,
        segments_json=excluded.segments_json,changes_json=excluded.changes_json,error_code=excluded.error_code,updated_at=excluded.updated_at,model_runs_json=excluded.model_runs_json""", values)

    # Cursor/narrative publication is atomic with this receipt. Older successes
    # cannot be replayed because delta_progress is the authoritative watermark.
    db.execute("DELETE FROM compact_batches WHERE chat_id=? AND state=? AND id<>?",
               (values[1], values[6], values[0]))
    if values[6] == 'done':
        db.execute("DELETE FROM compact_batches WHERE chat_id=? AND state='failed'", (values[1],))


def run_compact(config, chat_id, state, state_path, transport, report):
    from .locking import file_lock, lifecycle
    with lifecycle(config), file_lock(Path(state_path).with_suffix(".compact.lock")):
        return _run_compact(config, chat_id, state, state_path, transport, report)


def _run_compact(config, chat_id, state, state_path, transport, report):
    with __import__("seywo.store", fromlist=["Store"]).Store(config) as store:
        store.require_human_chat(chat_id)
        compact_schema.setup(store.db)
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
        rows, deferred = closed_rows(store, chat_id, state.get("cursor"))
        turns = _turn_packets(rows)
        if not turns:
            if rows and not deferred:
                state["cursor"] = [rows[-1]["ordinal"], rows[-1]["record_no"]]
                with store.db:
                    commit_turns(store.db, chat_id, rows)
                    store.db.execute("INSERT INTO delta_progress VALUES(?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET cursor_json=excluded.cursor_json,version=excluded.version,windows=excluded.windows",
                                     (chat_id, encode(state["cursor"]), state.get("version", 0), state.get("windows", 0) + 1))
            inbox.settle_imported(config, chat_id, state.get("cursor"))
            _write_state(state_path, state)
            report.update(state="done", calls=0, pipeline=PIPELINE, version=state.get("version"), compact_segments=0, deferred_turn=deferred)
            return report
        service = Narrative(store)
        current = service._body(chat_id, service._head(chat_id)[0])
        locks = service._locks(chat_id)
        before = state.get("cursor")
        after = [rows[-1]["ordinal"], rows[-1]["record_no"]]
        batch_id = _receipt_id(chat_id, before, after)
        if store.db.execute("SELECT 1 FROM compact_batches WHERE id=? AND state='done'", (batch_id,)).fetchone():
            state["cursor"] = after
            inbox.settle_imported(config, chat_id, after)
            _write_state(state_path, state)
            report.update(state="done", calls=0, pipeline=PIPELINE, version=state.get("version", 0))
            return report
        chunks = _batch_turns(turns)
        segment_count = 0
        calls = 0
        input_chars = output_chars = input_tokens = output_tokens = 0
        added_count = touched_count = 0
        model_runs = []
        working = current
        base_head = service._head(chat_id)
        try:
            for chunk_index, chunk in enumerate(chunks):
                # Rebuild the bounded state/catalog after every successful chunk.
                # Later batches can update a node created by an earlier batch and
                # inherit its phase/focus without receiving the whole narrative.
                state_ctx, catalog, item_ids = _state_and_catalog(working, chunk)
                payload_turns = _payload_turns(chunk)
                payload = request_payload(payload_turns, state_ctx, catalog)
                request_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                prompt = instructions()
                for attempt in range(MAX_REPAIRS + 1):
                    result = transport.complete_compact(model=transport.model, instructions=prompt, payload=payload, max_output_tokens=MAX_OUTPUT_TOKENS)
                    submitted = result.get("model_calls", 1)
                    calls += submitted
                    input_chars += len(request_text) + (len(prompt) - len(instructions()))
                    usage = result.get("usage") or {}
                    info = result.get("model_info") or {"backend": "responses", "selected_model": transport.model,
                                                        "selection": "explicit"}
                    identity = {key: info[key] for key in ("backend", "requested_model", "selected_model", "provider", "selection", "runtime", "actual_model", "actual_provider", "reasoning_effort") if key in info}
                    run = next((r for r in model_runs if all(r.get(k) == v for k, v in identity.items())), None)
                    if run is None:
                        run = {**identity, "calls": 0, "input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}
                        model_runs.append(run)
                    run["calls"] += submitted
                    for key in ("input_tokens", "output_tokens"):
                        if type(usage.get(key)) is int:
                            run[key] += usage[key]
                    run["cached_input_tokens"] += result.get("cached_input_tokens", 0)
                    input_tokens += usage.get("input_tokens", 0) if isinstance(usage.get("input_tokens"), int) else 0
                    output_tokens += usage.get("output_tokens", 0) if isinstance(usage.get("output_tokens"), int) else 0
                    if result.get("error") or result.get("output") is None:
                        raise ValueError(result.get("error") or "compact_empty")
                    output_chars += len(json.dumps(result["output"], ensure_ascii=False, separators=(",", ":")))
                    try:
                        output = validate(result["output"], list(range(len(payload_turns))), set(item_ids), require_current=True)
                        candidate, added, touched = apply_compact(
                            working, output, chunk, catalog, f"{batch_id}:{chunk_index}", locks=locks)
                    except ValueError as exc:
                        if attempt >= MAX_REPAIRS or not str(exc).startswith(("compact_", "new_phase_")):
                            raise
                        prompt = repair_instructions(exc, result["output"])
                        continue
                    working = candidate
                    segment_count += len(output["segments"])
                    break
                added_count += added
                touched_count += touched
            current = working
            added, touched = added_count, touched_count
            head, override = service._head(chat_id)
            if (head, override) != base_head:
                raise ValueError("narrative_base_changed")
            with store.db:
                store.db.execute("BEGIN IMMEDIATE")
                version = head + 1 if current != service._body(chat_id, head) else head
                if version != head:
                    service._write(chat_id, version, override, current, encode(Understanding(store)._heads(chat_id)), task_id=batch_id, origin=PIPELINE)
                commit_turns(store.db, chat_id, rows)
                cursor = after
                state.update(cursor=cursor, version=version, windows=state.get("windows", 0) + 1)
                store.db.execute("INSERT INTO delta_progress VALUES(?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET cursor_json=excluded.cursor_json,version=excluded.version,windows=excluded.windows",
                                 (chat_id, encode(cursor), version, state["windows"]))
                _record(store.db, (batch_id, chat_id, encode(before), encode(after), base_head[0], version, "done", len(turns), segment_count, calls,
                                    input_chars, output_chars, input_tokens, output_tokens, "[]", "[]", None, time.time(), time.time()), model_runs)
            inbox.settle_imported(config, chat_id, after)
            _write_state(state_path, state)
            report.update(state="done", calls=calls, pipeline=PIPELINE, version=version,
                          compact_items=len(current.get("items", [])), compact_segments=segment_count, deferred_turn=deferred,
                          input_chars=input_chars, output_chars=output_chars, input_tokens=input_tokens, output_tokens=output_tokens)
            return report
        except ValueError as exc:
            with store.db:
                _record(store.db, (batch_id, chat_id, encode(before), encode(after), base_head[0], None, "failed", len(turns), segment_count, calls,
                                    input_chars, output_chars, input_tokens, output_tokens, "[]", "[]", str(exc)[:120], time.time(), time.time()), model_runs)
            # Source ingestion already committed before semantic extraction.
            # Keep the semantic failure in compact_batches and release the
            # source boundary so retry can resume from the semantic cursor.
            inbox.settle_imported(config, chat_id, after)
            report.update(state="failed", error_code=str(exc)[:120], calls=calls, pipeline=PIPELINE,
                          input_chars=input_chars, output_chars=output_chars, input_tokens=input_tokens, output_tokens=output_tokens)
            _write_state(state_path, state)
            return report


def _import_claimed(store, config, chat_id, claimed):
    from .locking import file_lock
    with file_lock(inbox.root(config) / ".lock"):
        return _import_owned(store, config, chat_id, claimed)


def _import_owned(store, config, chat_id, claimed):
    imported = claimed.get("imported_snapshot_id")
    if imported and store.db.execute(
            "SELECT 1 FROM source_snapshots ss JOIN source_segments s ON s.id=ss.segment_id "
            "WHERE ss.id=? AND s.chat_id=?", (imported, chat_id)).fetchone():
        return True
    source = claimed.get("snapshot") or claimed["transcript"]
    path = Path(source)
    try:
        owner = chat_identity(str(path), chat_id)
    except NonHumanSource:
        inbox.finish(config, claimed, state="done", error="excluded_nonhuman_source")
        return False
    if owner != chat_id:
        try:
            inbox.record(config, owner, claimed["event"], str(path))
        except (OSError, ValueError):
            inbox.finish(config, claimed, state="failed", error="chat_identity_unresolved")
            return False
        inbox.finish(config, claimed, state="done", error="rerouted_chat")
        from .maintain import spawn_worker
        spawn_worker(Path(config["_path"]), owner)
        return False
    if not path.is_file():
        inbox.finish(config, claimed, state="failed", error="source_missing")
        return False
    try:
        path, manifest = inbox.capture_dialogue(config, path, chat_id)
    except (OSError, ValueError) as exc:
        inbox.finish(config, claimed, state="failed",
                     error=str(exc)[:120] or "source_capture_failed")
        return False
    if str(path) != claimed.get("snapshot"):
        claimed = inbox.relocate(config, claimed, path)
    try:
        imported = store._import_compact_capture(path, manifest, chat_id, claimed["transcript"])
    except (OSError, ValueError) as exc:
        inbox.finish(config, claimed, state="failed",
                     error=str(exc)[:120] or "source_import_failed")
        return False
    end = store.db.execute(
        "SELECT ordinal,record_no FROM current_sources WHERE chat_id=? ORDER BY ordinal DESC,record_no DESC LIMIT 1",
        (chat_id,)).fetchone()
    inbox.imported(config, claimed, snapshot_id=imported["snapshot_id"], cursor=list(end) if end else None)
    return True
