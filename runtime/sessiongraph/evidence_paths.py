"""Deterministic, source-grounded path projection for graph nodes.

The extractor may name paths, but a path is only useful if it points at an
object the cited evidence actually mentions.  No path is invented, guessed or
resolved on disk: every stored value is a literal substring of the node's own
evidence.  This keeps `paths_json` usable for retrieval without adding a model
call or a second source of truth.
"""
from __future__ import annotations

import json
import re

MAX_PATHS = 16
_EXTS = (
    "py pyi md json jsonl yaml yml toml txt log sqlite sqlite3 db sh bash zsh ts tsx js jsx "
    "rs css html csv npz npy pt pth ckpt bin safetensors png jpg jpeg gif svg pdf tex cfg ini "
    "lock parquet h5 hdf5 zip tar gz tgz bz2 xz"
)
_EXT = re.compile(rf"\.(?:{_EXTS.replace(' ', '|')})$", re.I)
_SEGMENT = r"[A-Za-z0-9_.@+~-]+"
_CANDIDATE = re.compile(rf"(?<![\w/\\.-])((?:/|~/|\./|\.\./)?{_SEGMENT}(?:/{_SEGMENT})*)(?![\w/-])")
# A bare "/automation_id" or a jargon pair like "reward/advantage" is not a
# path. Only real filesystem roots, explicit relative paths, known file types
# or dated experiment directories qualify.
_ROOTS = re.compile(r"^(?:/Users/|/home/|/gpu\d*/|/mnt/|/data/|/workspace/|/opt/|/tmp/|/var/|/srv/|/root/|/private/)")
_RELATIVE = re.compile(r"^(?:~/|\./|\.\./)")
_TRAIL = ".,;:)]}>）】』」”’"
_DATE = re.compile(r"(?:^|[_/.-])20\d{6}(?:$|[_/.-])")


def plausible(path):
    """A path-shaped literal: real root, explicit relative, known type, or dated run."""
    if not 4 <= len(path) <= 240:
        return False
    if path.startswith(("http://", "https://", "-")):
        return False
    if not re.search(r"[A-Za-z_]", path):
        return False
    base = path.rsplit("/", 1)[-1]
    # `.json` (often the tail of an escaped `\n` line) is not a file name.
    if len(base) < 2 or (base.startswith(".") and base.count(".") == 1):
        return False
    explicit = bool(_ROOTS.match(path) or _RELATIVE.match(path))
    # A bare `protocol.json` has no locatable directory; storing it would
    # resolve to the project root and produce a false path. Require directory
    # context for relative files, while known absolute roots may be directories.
    if explicit:
        return True
    return "/" in path and bool(_EXT.search(path) or any(_DATE.search(part) for part in path.split("/")))


def from_text(text):
    out = []
    for match in _CANDIDATE.finditer(text or ""):
        path = match.group(1).rstrip(_TRAIL)
        if plausible(path) and path not in out:
            out.append(path)
    return out


def for_node(node):
    """Merge declared and derived paths, keeping only literal citations."""
    quotes = [e.get("quote") or "" for e in node.get("evidence", []) if isinstance(e, dict)]
    grounded = "\n".join(quotes)
    declared = [p for p in (node.get("paths") or []) if isinstance(p, str) and p.strip()]
    found = [p.strip() for p in declared if p.strip() in grounded]
    for quote in quotes:
        for path in from_text(quote):
            if path in quote and path not in found:
                found.append(path)
    return found[:MAX_PATHS]


def backfill(store, *, apply=False, limit=None, after=''):
    """Preview or fill `paths_json` from already-cited evidence. No model calls.

    Idempotent and additive: existing paths are preserved, only literal
    citations from this node's own evidence rows are appended, and the result
    stays bounded by MAX_PATHS. Nothing outside `work_memory.paths_json` changes.
    """
    if type(limit) not in {int, type(None)} or (limit is not None and not 1 <= limit <= 100000):
        raise ValueError('limit must be between 1 and 100000')
    if not isinstance(after, str) or len(after) > 256:
        raise ValueError('invalid cursor')
    project = store.config['project_id']
    sql = '''SELECT m.id,m.paths_json FROM work_memory m WHERE m.project_id=?'''
    args = [project]
    if after:
        sql += ' AND m.id>?'
        args.append(after)
    sql += ' ORDER BY m.id'
    if limit is not None:
        sql += ' LIMIT ?'
        args.append(limit)
    scanned = filled = added_total = 0
    last = ''
    samples = []
    with store.db if apply else _null():
        for row in list(store.db.execute(sql, args)):
            scanned += 1
            last = row['id']
            quotes = [r[0] for r in store.db.execute(
                'SELECT quote FROM sg2_evidence WHERE node=? ORDER BY id LIMIT 8', (row['id'],))]
            current = _load(row['paths_json'])
            found = list(current)
            for quote in quotes:
                for path in from_text(quote):
                    if path not in found:
                        found.append(path)
            found = found[:MAX_PATHS]
            if found == current:
                continue
            filled += 1
            added_total += len(found) - len(current)
            if len(samples) < 5:
                samples.append({'node': row['id'], 'paths': found[:4]})
            if apply:
                store.db.execute('UPDATE work_memory SET paths_json=? WHERE id=?',
                                 (json.dumps(found, ensure_ascii=False), row['id']))
    return {'version': 'paths-backfill-v1', 'applied': bool(apply), 'scanned': scanned,
            'filled': filled, 'paths_added': added_total, 'next_after': last,
            'samples': samples, 'model_calls': 0}


class _null:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def _load(raw):
    try:
        value = json.loads(raw or '[]')
    except (TypeError, ValueError):
        return []
    return [p for p in value if isinstance(p, str)] if isinstance(value, list) else []
