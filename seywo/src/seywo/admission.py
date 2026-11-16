"""Source admission for human chat narratives, independent of physical identity.

Older transcripts may omit source metadata. Keep that compatibility, but explicit
subagent provenance must never become human work merely because it has an id.
"""
import json
from pathlib import Path


class NonHumanSource(ValueError):
    """An internal agent transcript is not a human chat."""


def require_human(meta):
    source = meta.get('source') if isinstance(meta, dict) else None
    if ((isinstance(source, dict) and 'subagent' in source)
            or source == 'subagent'):
        raise NonHumanSource('nonhuman_subagent_source')


def transcript_meta(path):
    """Bounded metadata read. Unavailable metadata keeps the hook fallback path."""
    try:
        from .archive import open_source
        with open_source(Path(path)) as handle:
            first = handle.readline(1024 * 1024)
        record = json.loads(first)
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(record, dict) or record.get('type') != 'session_meta':
        return None
    meta = record.get('payload')
    return meta if isinstance(meta, dict) else None
