"""Phase creation is a grounded goal change, not a model array append.

Only new structure is checked: immutable old snapshots stay readable. This gate
never decides semantic equivalence from labels and never merges phases.
"""

import unicodedata

PHASE_POLICY = "seywo-phase-1"
OBJECTIVE_TYPES = {"goal", "question", "decision", "correction", "action"}


def label_key(title):
    return "".join(unicodedata.normalize("NFKC", title).casefold().split())


def validate_phase_changes(previous, doc):
    old_ids = {p["id"] for p in previous["phases"]}
    old_items = {i["id"] for i in previous["items"]}
    items = {i["id"]: i for i in doc["items"]}
    locations = {p["item_id"]: p for p in doc["placements"]}
    old_transitions = {t["id"] for t in previous["transitions"]}
    for index, phase in enumerate(doc["phases"]):
        if phase["id"] in old_ids:
            continue
        inside = {p["item_id"] for p in doc["placements"]
                  if p["phase_id"] == phase["id"] and not items[p["item_id"]].get("hidden")}
        if not inside:
            raise ValueError("new_phase_requires_work")
        # Initializing a reading line may start with an assistant explanation.
        if index == 0 and not old_ids:
            continue
        if index and label_key(phase["title"]) == label_key(doc["phases"][index - 1]["title"]):
            raise ValueError("new_phase_repeats_previous_title")
        goals = {identity for identity in phase["goal_item_ids"] if identity in inside
                 and identity not in old_items
                 and locations[identity].get("branch_id") is None
                 and items[identity].get("attribution") == "user_statement"
                 and items[identity]["type"] in OBJECTIVE_TYPES
                 and items[identity].get("evidence")}
        if not goals:
            raise ValueError("new_phase_requires_user_objective")
        # A fresh goal must be linked by an explicit change to the earlier main
        # objective, not by focus/enter or an unrelated assistant progress item.
        earlier = {p["id"] for p in doc["phases"][:index]}
        changes = [t for t in doc["transitions"] if t["id"] not in old_transitions
                   and t["kind"] == "goal_change" and t["to_phase_id"] == phase["id"]
                   and t["from_phase_id"] in earlier and t["to_branch_id"] is None]
        if not any(goals & set(t["evidence_item_ids"]) for t in changes):
            raise ValueError("new_phase_requires_goal_change")
