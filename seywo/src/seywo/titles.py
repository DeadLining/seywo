"""Short human-facing titles; evidence and detail remain independent."""

TITLE_MAX = 40


def validate_title(value):
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or len(value) > TITLE_MAX or any(c in value for c in "\r\n\t")
            or any(ord(c) < 32 or c in "\u2028\u2029" for c in value)):
        raise ValueError("invalid_item_title")
    return value
