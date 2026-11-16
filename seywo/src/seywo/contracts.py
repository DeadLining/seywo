"""Internal materialized output/evidence validation, independent of provider.

model_contract.py owns the indexed model wire; these full objects stay local.
"""

import math

from .titles import validate_title

ITEM_TYPES = ("goal", "question", "option", "decision", "action", "observation", "correction", "next_step")
RELATIONS = ("supports", "contradicts", "qualifies", "quotes")
JUDGMENTS = {"identity": ("same", "different", "insufficient"),
             "support": ("supports", "contradicts", "insufficient"),
             "approval": ("proposes", "confirms", "conditional", "unclear")}
RULE_VERSION = "understanding-v3"

# Internal storage/transaction schema, never the indexed model output schema.
OUTPUT_SCHEMA = {
    "version": RULE_VERSION,
    "required": ["input_fingerprint", "base_version", "override_revision", "source_ranges",
                 "candidate_items", "read_requests", "unresolved", "no_change", "dispositions", "resolutions"],
    "source_range_fields": ["event_id", "snapshot_id", "start", "end"],
    "types": "candidate_items/read_requests/unresolved/dispositions/source_ranges are arrays; unresolved contains strings; base_version/override_revision/start/end are integers; no_change is boolean; id/type/content/attribution/scope/quote/relation/reason are strings; dependencies/output_ids contain string IDs. No extra fields in any object.",
    "item_types": ITEM_TYPES,
    "item_fields": ["id", "type", "title", "content", "attribution", "scope", "evidence", "dependencies", "turn_id"],
    "evidence_fields": ["event_id", "snapshot_id", "start", "end", "quote", "relation"],
    "relations": RELATIONS,
    "attribution": ["user_statement", "assistant_statement", "tool_observation", "inference"],
    "read_request_fields": ["event_id", "snapshot_id", "start", "end", "reason"],
    "disposition_fields": ["input_id", "action", "output_ids", "reason"],
    "disposition_actions": ["keep", "merge", "drop"],
}


def exact_keys(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError("invalid_output_fields")


def string(value, *, maximum=16000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("invalid_output_string")


def integer(value):
    if type(value) is not int or value < 0:
        raise ValueError("invalid_output_integer")


def lists(value, maximum=10000):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError("invalid_output_list")


def reference_key(ref):
    return ref["event_id"], ref["snapshot_id"], ref["start"], ref["end"]


def validate_reference(ref, allowed, *, quote=False):
    exact_keys(ref, ["event_id", "snapshot_id", "start", "end"] + (["quote", "relation"] if quote else []))
    integer(ref["start"])
    integer(ref["end"])
    if ref["start"] >= ref["end"]:
        raise ValueError("empty_evidence_range")
    for source in allowed:
        if (ref["event_id"] == source["event_id"] and ref["snapshot_id"] == source["snapshot_id"]
                and source["start"] <= ref["start"] < ref["end"] <= source["end"]):
            if quote:
                if (ref["relation"] not in RELATIONS or ref["quote"] !=
                        source["text"][ref["start"] - source["start"]:ref["end"] - source["start"]]):
                    raise ValueError("evidence_quote_mismatch")
            return source
    raise ValueError("evidence_not_in_model_input")


def validate_understanding(output, request):
    exact_keys(output, OUTPUT_SCHEMA["required"] + (["non_work_user_sources"] if "non_work_user_sources" in output else []))
    lists(output.get("non_work_user_sources", []))
    for reference in output.get("non_work_user_sources", []):
        source = validate_reference(reference, request["sources"], quote=True)
        if source["role"] != "user" or source["purpose"] != "primary" or not source.get("turn_id"):
            raise ValueError("invalid_non_work_user_source")
    string(output["input_fingerprint"], maximum=100)
    integer(output["base_version"])
    integer(output["override_revision"])
    if (output["input_fingerprint"] != request["input_fingerprint"]
            or output["base_version"] != request["base_version"]
            or output["override_revision"] != request["override_revision"]):
        raise ValueError("stale_output_identity")
    if type(output["no_change"]) is not bool:
        raise ValueError("invalid_no_change")
    expected = [{k: s[k] for k in ("event_id", "snapshot_id", "start", "end")} for s in request["sources"]]
    lists(output["source_ranges"])
    for reference in output["source_ranges"]:
        validate_reference(reference, request["sources"])
    if output["source_ranges"] != expected:
        raise ValueError("incomplete_source_accounting")
    for key in ("candidate_items", "read_requests", "unresolved", "dispositions"):
        lists(output[key])
    for value in output["unresolved"]:
        string(value)
    if output["no_change"] != (not output["candidate_items"]):
        raise ValueError("no_change_conflicts_with_items")
    items = output["candidate_items"]
    ids = []
    for item in items:
        exact_keys(item, OUTPUT_SCHEMA["item_fields"])
        string(item["id"], maximum=100)
        if item["id"] in ids or item["type"] not in ITEM_TYPES:
            raise ValueError("invalid_candidate_identity")
        ids.append(item["id"])
        validate_title(item["title"])
        string(item["content"])
        string(item["scope"])
        if item["turn_id"] is not None and (not isinstance(item["turn_id"], str) or not item["turn_id"]):
            raise ValueError("invalid_turn_identity")
        if item["attribution"] not in OUTPUT_SCHEMA["attribution"]:
            raise ValueError("invalid_attribution")
        lists(item["evidence"])
        if not item["evidence"]:
            raise ValueError("candidate_requires_evidence")
        role = {"user_statement": "user", "assistant_statement": "assistant", "tool_observation": "tool"}.get(item["attribution"])
        origin_found = role is None
        for evidence in item["evidence"]:
            source = validate_reference(evidence, request["sources"], quote=True)
            if source["role"] == role and evidence["relation"] in {"supports", "quotes"}:
                origin_found = True
        if not origin_found:
            raise ValueError("attribution_source_mismatch")
        lists(item["dependencies"])
        for dep in item["dependencies"]:
            string(dep, maximum=100)
    for item in items:
        if any(dep not in ids or dep == item["id"] for dep in item["dependencies"]):
            raise ValueError("invalid_candidate_dependency")
    graph = {item["id"]: item["dependencies"] for item in items}
    remaining = {node: set(deps) for node, deps in graph.items()}
    while remaining:
        roots = {node for node, deps in remaining.items() if not deps}
        if not roots:
            raise ValueError("cyclic_candidate_dependencies")
        remaining = {node: deps - roots for node, deps in remaining.items() if node not in roots}
    for read in output["read_requests"]:
        exact_keys(read, OUTPUT_SCHEMA["read_request_fields"])
        string(read["reason"])
        for k in ("event_id", "snapshot_id"):
            string(read[k])
        integer(read["start"])
        integer(read["end"])
        if read["start"] >= read["end"]:
            raise ValueError("invalid_read_request")
    lists(output["resolutions"])
    resolved = set()
    for resolution in output["resolutions"]:
        exact_keys(resolution, ["concern", "evidence", "reason"])
        string(resolution["reason"], maximum=240)
        if resolution["concern"] not in request.get("unresolved", []) or resolution["concern"] in resolved:
            raise ValueError("unknown_concern_resolution")
        resolved.add(resolution["concern"])
        lists(resolution["evidence"])
        if not resolution["evidence"]:
            raise ValueError("resolution_requires_evidence")
        for evidence in resolution["evidence"]:
            validate_reference(evidence, request["sources"], quote=True)
    retained = set(output["unresolved"])
    dispositions = {d["input_id"]: d for d in output["dispositions"]
                    if isinstance(d, dict) and "input_id" in d}
    output_titles = {i["id"]: i["title"] for i in items}
    for concern in set(request.get("unresolved", [])) - resolved:
        if concern in retained:
            continue
        old_matches = [i for i in request.get("items", []) if i["title"] == concern]
        targets = []
        for item in old_matches:
            disp = dispositions.get(item["id"], {})
            if disp.get("action") == "merge" and len(disp.get("output_ids", [])) == 1:
                targets.extend(disp["output_ids"])
        if (not old_matches or len(targets) != len(old_matches) or len(set(targets)) != 1
                or output_titles.get(targets[0]) not in retained):
            raise ValueError("unresolved_material_silently_dropped")
    if request["phase"] == "extract":
        if output["dispositions"]:
            raise ValueError("unexpected_dispositions")
    else:
        inputs = {i["id"] for i in request["items"]}
        seen = set()
        for disp in output["dispositions"]:
            exact_keys(disp, OUTPUT_SCHEMA["disposition_fields"])
            if disp["input_id"] not in inputs or disp["input_id"] in seen:
                raise ValueError("invalid_disposition_input")
            seen.add(disp["input_id"])
            if disp["action"] not in {"keep", "merge", "drop"}:
                raise ValueError("invalid_disposition_action")
            string(disp["reason"])
            lists(disp["output_ids"])
            if any(i not in ids for i in disp["output_ids"]):
                raise ValueError("invalid_disposition_output")
            if (disp["action"] == "drop") != (not disp["output_ids"]):
                raise ValueError("invalid_disposition_mapping")
        if seen != inputs:
            raise ValueError("merge_dropped_input_accounting")
    return output


def validate_judgment(output, request):
    exact_keys(output, ["input_fingerprint", "choice", "scores", "score_kind", "reason"])
    if output["input_fingerprint"] != request["input_fingerprint"] or output["choice"] not in request["options"]:
        raise ValueError("invalid_judgment_identity")
    string(output["reason"])
    string(output["score_kind"], maximum=100)
    scores = output["scores"]
    if not isinstance(scores, dict) or set(scores) != set(request["options"]):
        raise ValueError("invalid_judgment_scores")
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in scores.values()):
        raise ValueError("invalid_judgment_score")
    if output["score_kind"] == "probabilities":
        if any(not 0 <= v <= 1 for v in scores.values()) or abs(sum(scores.values()) - 1) > 0.02:
            raise ValueError("invalid_judgment_probabilities")
    return output
