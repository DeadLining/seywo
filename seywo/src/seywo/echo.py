"""Drop request preamble the model echoed back before delta hydration.

Models re-send `items`, `locks`, `gaps` and friends even when told not to. Echoed
keys are only accepted when they match the wire request they came from; a
diverging echo is still an error, so a real edit attempt cannot hide here.

Transitions and phases are deltas, but they legitimately reuse index field names;
they are normalized here as well (id-less form) so a fully echoed object is
equivalent to the delta the contract expects.
"""

# Echoed verbatim from the request: no delta meaning.
DROP = {"phase", "protocol", "backend", "model", "max_output_tokens", "rules",
        "output_schema", "primary_source_indexes", "sources", "gaps", "locks", "unresolved"}


def _same(left, right):
    return json_equal(left, right)


def json_equal(left, right):
    """Structural equality that ignores insignificant whitespace.

    A model echoing the long `rules` prose re-wraps lines; the words are
    unchanged but the bytes are not. Compare string leaves by their collapsed
    whitespace, and everything else structurally, so a real content change is
    still rejected.
    """
    return _shape(left) == _shape(right)


def _shape(value):
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, list):
        return [_shape(item) for item in value]
    if isinstance(value, dict):
        return {key: _shape(item) for key, item in value.items()}
    return value


def strip_echo(output, wire):
    """Return a copy without preamble echoes; raise when an echoed body differs."""
    import copy
    if output is None:
        return None
    result = copy.deepcopy(output)
    for key in DROP:
        if key in result:
            if key in wire and not _same(result[key], wire[key]):
                raise ValueError("echoed_request_field_differs")
            result.pop(key)
    if "items" in result:
        if not _same(result["items"], wire.get("items")):
            raise ValueError("invalid_indexed_fields")
        result.pop("items")
    # An echoed transition list is the request's recent history, not new rows.
    if "recent_transitions" in result:
        expected = wire.get("recent_transitions")
        if expected is not None and _same(result["recent_transitions"], expected):
            result.pop("recent_transitions")
    return result


def strip_extraction_preamble(output, wire):
    """Ignore an exact transport/catalog preamble, never an extraction edit.

    items/unresolved are actual A output and must stay. Unknown or changed
    fields still reach strict validation and are rejected.
    """
    import copy
    if not isinstance(output, dict):
        return output
    result = copy.deepcopy(output)
    for key in DROP - {"unresolved"}:
        if key in result and key in wire:
            if not _same(result[key], wire[key]):
                raise ValueError("echoed_request_field_differs")
            result.pop(key)
    return result
