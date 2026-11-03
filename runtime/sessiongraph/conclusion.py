"""Deterministic, conservative conclusion identity for Claim/Decision nodes.

Claim/Decision nodes are paraphrased findings, so most cannot carry a run id,
DOI or artifact version.  This module derives a *weak* key from the node's own
cited text only:

  scope = the single dated experiment token that occurs literally in a quote
          (two different experiment tokens make the scope ambiguous and yield no
          key); a title match is preferred but not required because historical
          findings often cite the run only in their evidence;
  value = one quoted sentence that supports this content, i.e. every number in
          it also appears in the content and its tokens are largely present in
          the content;
  shape = the kind of claim (decision, negative/failed, positive result) so a
          "plan adopted" and a "plan rejected" sentence can never share a key.

The merge still requires the model judge to decide full semantic equivalence;
this key only shortlists a candidate.  A single quoted sentence is never treated
as proof that two differently-worded nodes are the same fact.

Both halves are literal citations, so the key passes identity anchoring, and the
same experiment's verbatim conclusion merges across chats.  The guard is
deliberately conservative: a sentence that adds a number the content does not
state, or that shares too little wording with it, is rejected.  A different
scope or a different quoted conclusion therefore yields a different key; nothing
is merged on title similarity.  No model call, no filesystem access.
"""
from __future__ import annotations

import json
import re
import unicodedata

SCOPE = re.compile(r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]*?20\d{6}[A-Za-z0-9_.-]*)(?![\w.-])")
SENTENCE = re.compile(r"(?<=[。！？!?；;\n])")
NUMBER = re.compile(r"[+-]?\d+(?:[.,]\d+)*(?:[eE][+-]?\d+)?")
NUMERIC_TERM = re.compile(r"[A-Za-z_][A-Za-z0-9_./:+-]*[\d.+-]|[\d.+-][A-Za-z_][A-Za-z0-9_./:+-]*")
STRUCTURAL = re.compile(r"(?i)^\s*(?:[|#>*\-]|\{|\}|\[|\"|https?://|/|\w+\s*[:=]\s*$)")
MIN_SPAN = 24
MAX_SPAN = 300
MIN_OVERLAP = 0.35
MAX_SCOPE = 240
NEGATIVE = re.compile(r"(?i)(?:未|没有|不再|尚未|未能|失败|拒绝|不通过|不合格|无\s*(?:合格|接受|候选)|"
                      r"\bnot\b|\bno\b|\bfailed?\b|\brejected?\b|\bwithout\b|\bmissing\b)")
DECISION = re.compile(r"(?i)(?:下一步|下一轮|后续|计划|方案|方向|改为|决定|采用|优先|"
                      r"\bnext\b|\bplan\b|\bwill\b|\bdecide\b|\bshould\b)")


def _collapse(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


def _tokens(text):
    return set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", _collapse(text).casefold()))


def _numbers(text):
    normalized = unicodedata.normalize("NFKC", text or "")
    values = set(NUMBER.findall(normalized))
    # Also preserve alphanumeric quantities such as 100Hz, 0.04rad, v2, e-6.
    values.update(m.group(0).casefold() for m in NUMERIC_TERM.finditer(normalized))
    return values


def _quotes(node):
    return [e["quote"] for e in node.get("evidence", [])
            if isinstance(e, dict) and isinstance(e.get("quote"), str) and e["quote"]]


def _scope(node, quotes):
    """The one literal dated experiment named by the citations.

    A title match is preferred, but requiring it dropped nearly every historical
    finding because the title is a paraphrase. Ambiguity is the real danger, not
    an absent title echo: if the quotes name two different dated experiments we
    cannot tell which conclusion belongs to which run, so no key is produced.
    """
    title = _collapse(node.get("title") or "").casefold()
    found = {}
    for quote in quotes:
        for match in SCOPE.finditer(quote):
            token = match.group(1).strip(".-_")
            if not 8 <= len(token) <= MAX_SCOPE or not re.search(r"[A-Za-z_]", token):
                continue
            # A decimal quantity such as 0.000206030179 is not an experiment.
            if re.fullmatch(r"[\d.]+", token):
                continue
            found[_collapse(token).casefold()] = token
    if len(found) != 1:
        return None
    token = next(iter(found.values()))
    return token


def _span(node, quotes):
    """One quoted sentence that states this content without adding facts."""
    content_tokens = _tokens(node.get("content"))
    content_numbers = _numbers(node.get("content"))
    if not content_tokens:
        return None
    best = None
    for quote in quotes:
        for sentence in SENTENCE.split(quote):
            raw = sentence.strip(" -*#|`<>")
            collapsed = _collapse(raw)
            if not MIN_SPAN <= len(collapsed) <= MAX_SPAN or STRUCTURAL.search(raw):
                continue
            # Numbers must match exactly. A quote that states fewer numbers
            # than the content does not support the rest of the content; a quote
            # with extra numbers states facts the content omits. Either way the
            # two texts are not the same conclusion and must not share a key.
            if _numbers(raw) != content_numbers:
                continue
            tokens = _tokens(raw)
            if not tokens:
                continue
            overlap = len(tokens & content_tokens) / len(tokens)
            if overlap < MIN_OVERLAP:
                continue
            if best is None or (overlap, len(collapsed)) > best[:2]:
                best = (overlap, len(collapsed), raw.strip())
    # Return the exact cited characters: identity anchoring is a literal match,
    # so whitespace must be preserved rather than normalised.
    return best[2] if best else None


def _shape(text):
    body = _collapse(text).casefold()
    return ("negative:" if NEGATIVE.search(body) else "positive:") + (
        "decision" if DECISION.search(body) else "result")


def derive(node):
    """Return one identity entry, or None. Never mutates the node."""
    if not isinstance(node, dict) or node.get("type") not in {"Claim", "Decision"}:
        return None
    from .graph_contract import atomic_key
    if atomic_key(node) is not None:
        # A complete assertion is already keyed and revision-adjudicated; a
        # value-level conclusion key here would merge different subjects.
        return None
    if any(isinstance(i, dict) and i.get("namespace") == "claim_conclusion"
           for i in node.get("identity") or []):
        return None
    quotes = _quotes(node)
    if not quotes:
        return None
    scope = _scope(node, quotes)
    value = _span(node, quotes)
    if not scope or not value:
        return None
    # Same scope and same quoted sentence are still not enough when one node
    # records a rejected/absent result and the other records an accepted plan.
    keyed = f"{_shape(node.get('content'))}|{value}"
    if len(keyed) > 240:  # identity.value schema bound; never truncate evidence
        return None
    return {"namespace": "claim_conclusion", "value": keyed, "scope": scope}


def enrich(output):
    """Derive identities deterministically; never trust a model-authored one.

    `claim_conclusion` is application-owned. Provider output carrying that
    namespace is discarded first, so a model cannot mint a merge key.
    """
    added = stripped = 0
    for node in output.get("nodes", []):
        current = node.get("identity")
        if isinstance(current, list):
            kept = [i for i in current
                    if not (isinstance(i, dict) and i.get("namespace") == "claim_conclusion")]
            stripped += len(current) - len(kept)
            node["identity"] = kept
        identity = derive(node)
        if identity is None:
            continue
        current = node.setdefault("identity", [])
        if len(current) >= 4:  # schema bound; never drop an extractor identity
            continue
        current.append(identity)
        added += 1
    return output, added, stripped


def key_values(identity):
    """Normalise for the fingerprint: whitespace is not a semantic difference."""
    return _collapse(identity.get("value") or "").casefold(), _collapse(identity.get("scope") or "").casefold()


def grounded_value(identity):
    """The exact quoted part of a derived value, without the shape prefix."""
    value = identity.get("value") or ""
    prefix, sep, rest = value.partition("|")
    return rest if sep and prefix in {"positive:result", "positive:decision",
                                      "negative:result", "negative:decision"} else value


def backfill(store, *, apply=False, limit=None, after=''):
    """Preview or add derived identities to existing Claim/Decision nodes.

    Only `sg2_nodes.identity_json` and the corresponding `sg2_keys` index rows are
    written. Titles, content, status, evidence, relations and tier rows are not
    modified. Nodes with any existing strong identity are left untouched unless
    they already carry a derived conclusion key. The write is additive and
    idempotent; no model calls, filesystem access or deletion.
    """
    if type(limit) not in {int, type(None)} or (limit is not None and not 1 <= limit <= 100000):
        raise ValueError('limit must be between 1 and 100000')
    if not isinstance(after, str) or len(after) > 256:
        raise ValueError('invalid cursor')
    from .graph_v2 import _identity_keys, dumps
    project = store.config['project_id']
    sql = """SELECT n.id,n.type,n.identity_json,m.title,m.content
             FROM sg2_nodes n JOIN work_memory m ON m.id=n.id
             WHERE n.project=? AND n.type IN ('Claim','Decision')"""
    args = [project]
    if after:
        sql += ' AND n.id>?'
        args.append(after)
    sql += ' ORDER BY n.id'
    if limit is not None:
        sql += ' LIMIT ?'
        args.append(limit)
    scanned = derived = added = skipped_existing = 0
    last = ''
    samples = []
    with store.db if apply else _null_context():
        for row in list(store.db.execute(sql, args)):
            scanned += 1
            last = row['id']
            existing = json.loads(row['identity_json'] or '[]')
            if any(isinstance(i, dict) and i.get('namespace') == 'claim_conclusion' for i in existing):
                skipped_existing += 1
                continue
            # A strong model/extractor identity already governs this node; adding
            # a weak conclusion key could create a second competing lane.
            if existing:
                skipped_existing += 1
                continue
            quotes = [r[0] for r in store.db.execute(
                'SELECT quote FROM sg2_evidence WHERE node=? ORDER BY id LIMIT 8', (row['id'],))]
            node = {'type': row['type'], 'title': row['title'], 'content': row['content'],
                    'identity': existing, 'evidence': [{'quote': q} for q in quotes]}
            identity = derive(node)
            if identity is None:
                continue
            derived += 1
            updated = [*existing, identity]
            if apply:
                store.db.execute('UPDATE sg2_nodes SET identity_json=? WHERE id=?',
                                 (dumps(updated), row['id']))
                for key in _identity_keys({**node, 'identity': updated}):
                    store.db.execute('INSERT OR IGNORE INTO sg2_keys VALUES(?,?,?)',
                                     (project, key, row['id']))
            added += 1
            if len(samples) < 5:
                samples.append({'node': row['id'], 'type': row['type'],
                                'scope': identity['scope'],
                                'value': grounded_value(identity)[:120]})
    return {'version': 'claim-conclusion-backfill-v1', 'applied': bool(apply),
            'scanned': scanned, 'derived': derived, 'written': added,
            'skipped_existing_identity': skipped_existing, 'next_after': last,
            'samples': samples, 'model_calls': 0}


class _null_context:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False
