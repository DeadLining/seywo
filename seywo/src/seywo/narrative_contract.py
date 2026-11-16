"""B organizes supplied items; executable checks prove structure, never meaning."""

from .contracts import exact_keys, lists, string
from .store import digest
from .titles import validate_title

RULE_VERSION = "seywo-narrative-3"
SCHEMA = {
    "fields": ["input_fingerprint", "base_version", "override_revision", "bindings", "phases", "branches", "placements", "transitions", "overview", "resolutions"],
    "binding_actions": ["keep", "revise", "propose"],
    "focus_fields": ["phase_id", "branch_id"],
    "types": "bindings/phases/branches/placements/transitions are arrays; overview/focus are objects. *_ids and unresolved are arrays of strings; all other fields are strings except version integers and nullable phase/branch references. No extra fields. New items must bind to supplied new_id with action revise; keep/propose are only for existing item IDs; keep preserves an unchanged identity; propose retains a material revision for explicit review.",
    "binding": ["candidate_id", "item_id", "action", "reason"],
    "phase": ["id", "title", "goal_item_ids"],
    "branch": ["id", "phase_id", "title", "goal", "resolve_when", "state", "outcome_item_ids", "evidence_item_ids"],
    "placement": ["item_id", "phase_id", "branch_id"],
    "transition": ["id", "kind", "from_phase_id", "from_branch_id", "to_phase_id", "to_branch_id", "result_item_ids", "unresolved", "reason", "evidence_item_ids"],
    "overview": ["goal_item_ids", "focus", "open_question_ids", "next_step_ids", "unresolved"],
    "new_identity": "Use new:<local-id> for new phases/branches/transitions; runtime assigns stable IDs. Reuse existing IDs for revisions.",
    "branch_states": ["active", "waiting", "resolved", "abandoned"],
    "transition_kinds": ["enter", "return", "goal_change", "reopen", "focus"],
    "constraints": "Every old and incoming item has exactly one placement. Branch belongs to its phase. enter MUST target a branch; return MUST leave a branch for the mainline. Initial/mainline focus uses focus or goal_change, not enter. Resolved branches require outcome and evidence IDs; abandoned require evidence. All transitions require evidence IDs. Reuse every previous structural ID; never rewrite old transitions. Preserve unresolved strings and manual locks exactly.",
}


def ids(value, allowed):
    lists(value)
    if any(not isinstance(i, str) or i not in allowed for i in value) or len(value) != len(set(value)):
        raise ValueError("unknown_or_duplicate_reference")


def index(rows):
    lists(rows)
    result = {}
    for row in rows:
        string(row["id"], maximum=100)
        if row["id"] in result:
            raise ValueError("duplicate_narrative_identity")
        result[row["id"]] = row
    return result


def acyclic(items):
    remaining = {i: set(v["dependencies"]) for i, v in items.items()}
    if any(not d <= set(items) for d in remaining.values()):
        raise ValueError("unknown_dependency")
    while remaining:
        roots = {i for i, deps in remaining.items() if not deps}
        if not roots:
            raise ValueError("narrative_dependency_cycle")
        remaining = {i: deps - roots for i, deps in remaining.items() if i not in roots}


def validate_layout(doc):
    """Validate a full materialized snapshot, including hidden and deferred items."""
    items, phases, branches = index(doc["items"]), index(doc["phases"]), index(doc["branches"])
    structural = [r["id"] for k in ("phases", "branches", "transitions") for r in doc[k]]
    if len(structural) != len(set(structural)) or set(structural) & set(items):
        raise ValueError("cross_kind_identity_collision")
    acyclic(items)
    for item in items.values():
        if "title" in item:  # Legacy snapshots stay readable and immutable.
            validate_title(item["title"])
    for p in phases.values():
        exact_keys(p, SCHEMA["phase"])
        string(p["title"])
        ids(p["goal_item_ids"], items)
    for b in branches.values():
        exact_keys(b, SCHEMA["branch"])
        for field in ("title", "goal", "resolve_when"):
            string(b[field])
        if b["phase_id"] not in phases or b["state"] not in SCHEMA["branch_states"]:
            raise ValueError("invalid_branch")
        for field in ("outcome_item_ids", "evidence_item_ids"):
            ids(b[field], items)
        if b["state"] in {"resolved", "abandoned"} and not b["evidence_item_ids"]:
            raise ValueError("branch_state_requires_evidence")
        if b["state"] == "resolved" and not b["outcome_item_ids"]:
            raise ValueError("resolution_requires_outcome")
    def location(phase, branch):
        if phase is not None and phase not in phases:
            raise ValueError("unknown_phase")
        if branch is not None and (branch not in branches or branches[branch]["phase_id"] != phase):
            raise ValueError("branch_location_mismatch")
    placed = set()
    for p in doc["placements"]:
        exact_keys(p, SCHEMA["placement"])
        if p["item_id"] not in items or p["item_id"] in placed:
            raise ValueError("invalid_item_placement")
        placed.add(p["item_id"])
        location(p["phase_id"], p["branch_id"])
    if placed != set(items):
        raise ValueError("unaccounted_narrative_items")
    transitions = index(doc["transitions"])
    for t in transitions.values():
        exact_keys(t, SCHEMA["transition"])
        if t["kind"] not in SCHEMA["transition_kinds"]:
            raise ValueError("invalid_transition_kind")
        location(t["from_phase_id"], t["from_branch_id"])
        location(t["to_phase_id"], t["to_branch_id"])
        if t["kind"] == "enter" and t["to_branch_id"] is None:
            raise ValueError("enter_requires_branch")
        if t["kind"] == "return" and (t["from_branch_id"] is None or t["to_branch_id"] is not None):
            raise ValueError("return_requires_mainline")
        string(t["reason"])
        ids(t["result_item_ids"], items)
        ids(t["evidence_item_ids"], items)
        if not t["evidence_item_ids"]:
            raise ValueError("transition_requires_evidence")
        lists(t["unresolved"])
        for u in t["unresolved"]:
            string(u)
    overview = doc["overview"]
    exact_keys(overview, SCHEMA["overview"])
    exact_keys(overview["focus"], ["phase_id", "branch_id"])
    location(**{"phase": overview["focus"]["phase_id"], "branch": overview["focus"]["branch_id"]})
    for k in ("goal_item_ids", "open_question_ids", "next_step_ids"):
        ids(overview[k], items)
    lists(overview["unresolved"])
    for u in overview["unresolved"]:
        string(u)
    if items and not phases:
        raise ValueError("mainline_phase_required")
    return doc


def validate_output(output, request):
    exact_keys(output, SCHEMA["fields"])
    for field in ("input_fingerprint", "base_version", "override_revision"):
        if output[field] != request[field] or type(output[field]) is not type(request[field]):
            raise ValueError("stale_narrative_candidate")
    previous = request["previous"]
    old_items = {i["id"]: i for i in previous["items"]}
    incoming = {i["candidate_id"]: i for i in request["incoming"]}
    mapping, seen_targets = {}, {}
    lists(output["bindings"])
    for binding in output["bindings"]:
        exact_keys(binding, SCHEMA["binding"])
        string(binding["reason"])
        cid, target = binding["candidate_id"], binding["item_id"]
        if cid not in incoming or cid in mapping or (target in seen_targets and (binding["action"] not in {"keep", "propose"} or seen_targets[target] not in {"keep", "propose"})):
            raise ValueError("invalid_identity_binding")
        if target not in old_items and target != incoming[cid]["new_id"]:
            raise ValueError("identity_not_runtime_allocated")
        if binding["action"] not in {"keep", "revise", "propose"} or (target not in old_items and binding["action"] != "revise"):
            raise ValueError("invalid_binding_action")
        if binding["action"] == "revise" and any(lock["entity"] == "item" and lock["id"] == target for lock in request["locks"]):
            raise ValueError("manual_item_revision_conflict")
        mapping[cid] = target
        seen_targets[target] = binding["action"]
    if set(mapping) != set(incoming):
        raise ValueError("unaccounted_candidates")
    aliases=request.get("applied_dependency_ids",{})
    if set(aliases) & set(incoming) or any(target not in old_items for target in aliases.values()):
        raise ValueError("invalid_applied_dependency_map")
    def dependency_target(cid):
        if cid in mapping:
            return mapping[cid]
        if cid in aliases:
            return aliases[cid]
        raise ValueError("unavailable_batch_dependency")
    items = dict(old_items)
    pending = list(previous.get("pending_revisions", []))
    for cid, target in mapping.items():
        candidate = incoming[cid]["item"]
        binding = next(b for b in output["bindings"] if b["candidate_id"] == cid)
        if binding["action"] == "keep":
            if request.get("organization_policy") == "seywo-stop-8":
                # Same work identity is decided by stable axes, not copied wording.
                # An unlocked mismatch gets its own new identity in the hydrator;
                # a manual lock still has to fail closed.
                target_item = old_items[target]
                if (any(candidate.get(field) != target_item.get(field)
                        for field in ("type", "title", "attribution"))
                        or [dependency_target(d) for d in candidate["dependencies"]]
                        != target_item["dependencies"]) and any(
                            lock["entity"] == "item" and lock["id"] == target
                            for lock in request["locks"]):
                    raise ValueError("manual_item_revision_conflict")
            continue
        if binding["action"] == "propose":
            comparable = ("type", "title", "content", "attribution", "scope", "evidence", "dependencies")
            mapped_candidate = {**candidate, "dependencies": [dependency_target(d) for d in candidate["dependencies"]]}
            if all(mapped_candidate.get(k) == old_items[target].get(k) for k in comparable):
                continue
            pending.append({"id": digest(request["input_fingerprint"] + ":" + cid)[:32], "item_id": target,
                            "candidate": {**candidate, "dependencies": [dependency_target(d) for d in candidate["dependencies"]]},
                            "reason": binding["reason"], "state": "open"})
            continue
        items[target] = {**candidate, "id": target,
                         "dependencies": [dependency_target(d) for d in candidate["dependencies"]],
                         "hidden": False, "origin": "model", "user_confirmed": False}
    doc = {k: output[k] for k in ("phases", "branches", "placements", "transitions", "overview")}
    doc["items"] = list(items.values())
    doc["pending_revisions"] = pending
    doc["identity_changes"] = previous.get("identity_changes", [])
    doc["applied_candidates"] = {**previous.get("applied_candidates", {}), **{incoming[cid]["new_id"]: target for cid, target in mapping.items()}}
    validate_layout(doc)
    from .phase_integrity import validate_phase_changes
    validate_phase_changes(previous, doc)
    for kind in ("phases", "branches", "transitions"):
        old = {v["id"]: v for v in previous[kind]}
        new = {v["id"]: v for v in doc[kind]}
        if not set(old) <= set(new):
            raise ValueError("historical_identity_dropped")
        for identity in set(new) - set(old):
            if not identity.startswith("new:"):
                raise ValueError("new_identity_requires_local_prefix")
        if kind == "transitions" and any(new[i] != v for i, v in old.items()):
            raise ValueError("transition_history_rewritten")
    concerns = set(previous["overview"]["unresolved"] + request["unresolved"])
    resolved = set()
    lists(output["resolutions"])
    for resolution in output["resolutions"]:
        exact_keys(resolution, ["concern", "evidence_item_ids", "reason"])
        string(resolution["reason"], maximum=240)
        if resolution["concern"] not in concerns or resolution["concern"] in resolved:
            raise ValueError("unknown_concern_resolution")
        ids(resolution["evidence_item_ids"], items)
        if not resolution["evidence_item_ids"]:
            raise ValueError("resolution_requires_evidence")
        resolved.add(resolution["concern"])
    doc["resolution_log"] = previous.get("resolution_log", []) + output["resolutions"]
    if not concerns - resolved <= set(doc["overview"]["unresolved"]):
        raise ValueError("unresolved_question_dropped")
    for lock in request["locks"]:
        entity, identity, field, value = (lock[k] for k in ("entity", "id", "field", "value"))
        collection = {"item": "items", "phase": "phases", "branch": "branches", "placement": "placements"}
        row = doc["overview"] if entity == "overview" else next((v for v in doc[collection[entity]] if v.get("id", v.get("item_id")) == identity), {})
        if row.get(field) != value:
            raise ValueError("manual_override_conflict")
    return doc


def materialize(doc, task_id):
    """Assign new structural IDs from task identity, never from labels or wording."""
    mapping = {v["id"]: digest(task_id + ":" + kind + ":" + v["id"])[:32]
               for kind in ("phases", "branches", "transitions") for v in doc[kind] if v["id"].startswith("new:")}
    def rewrite(value, key=None):
        if isinstance(value, dict):
            return {k: rewrite(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [rewrite(v, key) for v in value]
        if key in {"id", "phase_id", "branch_id", "from_phase_id", "to_phase_id", "from_branch_id", "to_branch_id"} and isinstance(value, str):
            return mapping.get(value, value)
        return value
    return rewrite(doc)
