"""Evidence-first retrieval: independent recall channels, deterministic rerank, no model."""

from __future__ import annotations

import json
import math
from collections.abc import Container

from .core import digest, tokens
from .source_text import hydrate, available, external, unified

# These characters alone carry little retrieval information; compounds are preserved.
STOP_SINGLE = frozenset("的了是在呢吗和与及或就都也而把被这那我你他她它们")
CAP = 200


def query_terms(query: str) -> list[str]:
    # Prefer informative terms before applying the bound, not the tokenizer's unigram-first order.
    words = [t for t in tokens(query) if t not in STOP_SINGLE]
    return sorted(words, key=lambda t: (is_han_single(t), -len(t), t))[:80]


def is_han_single(term: str) -> bool:
    return len(term) == 1 and "\u3400" <= term <= "\u9fff"


class IndexedTokens:
    """Membership on already-normalized FTS content without rebuilding large token sets."""

    def __init__(self, body: str):
        self.body = " " + body + " "

    def __contains__(self, term: str) -> bool:
        return " " + term + " " in self.body


def lexical(text: str, terms: list[str], present: Container[str] | None = None) -> float:
    if present is None:
        present = set(tokens(text))
    weights = {t: 0.04 if is_han_single(t) and len(terms) > 1 else 1.0 for t in terms}
    matched = sum(w for t, w in weights.items() if t in present)
    # Query-local scoring: adding derived documents cannot change the score of raw text.
    norm = 1 + 0.1 * math.log1p(max(0, len(text) - 100) / 100)
    return matched / max(sum(weights.values()), 1) / norm


def _source_candidates(store, expression, kind, chat_id, segment_id):
    if not available(store):
        return []
    mapped = store.db.execute("SELECT 1 FROM sqlite_master WHERE name='sg_search_keys'").fetchone()
    args = [expression]
    if mapped:
        keys = 'JOIN sg_search_keys k ON k.search_rowid=sg_source_search.rowid'
        if kind == 'entry':
            joins = keys + " JOIN entries e ON e.id=k.id JOIN session_chats c ON c.segment_id=e.session_id"
            where = "k.kind='entry'"
        else:
            joins = keys + ' JOIN records r ON r.id=k.id JOIN jobs j ON j.id=r.job_id JOIN session_chats c ON c.segment_id=j.session_id'
            where = "k.kind='record' AND j.policy_hash=?"
            args.append(store.config['policy_hash'])
        object_id = 'k.id'
    elif kind == 'entry':
        stable = any(r['name'] == 'search_rowid' for r in
                     store.db.execute('PRAGMA table_info(sg_external_entries)'))
        joins = ('JOIN sg_external_entries x ON x.search_rowid=sg_source_search.rowid '
                 'JOIN entries e ON e.id=x.entry_id' if stable else
                 'JOIN entries e ON e.rowid=sg_source_search.rowid '
                 'JOIN sg_external_entries x ON x.entry_id=e.id')
        joins += ' JOIN session_chats c ON c.segment_id=e.session_id'
        where, object_id = '1', 'e.id'
    else:
        return []
    args.extend([chat_id, chat_id, segment_id, segment_id])
    return store.db.execute(f'''SELECT {object_id},NULL,bm25(sg_source_search) rank
        FROM sg_source_search {joins} WHERE sg_source_search MATCH ? AND {where}
        AND (? IS NULL OR c.chat_id=?) AND (? IS NULL OR c.segment_id=?)
        ORDER BY rank,{object_id} LIMIT 201''', args).fetchall()


def _candidates(store, terms, kind, chat_id=None, segment_id=None):
    if not terms:
        return [], False
    scope = (
        """EXISTS(SELECT 1 FROM entries e JOIN session_chats c ON c.segment_id=e.session_id
        WHERE e.id=docs.id AND (? IS NULL OR c.chat_id=?)
        AND (? IS NULL OR c.segment_id=?))"""
        if kind == "entry"
        else """EXISTS(
        SELECT 1 FROM records r JOIN jobs j ON j.id=r.job_id JOIN session_chats c ON c.segment_id=j.session_id
        WHERE r.id=docs.id AND j.policy_hash=? AND (? IS NULL OR c.chat_id=?)
        AND (? IS NULL OR c.segment_id=?))"""
    )
    strong = [t for t in terms if not is_han_single(t)]
    # Weak character recall is an explicit fallback per channel, not an equal OR signal.
    passes = [strong, terms] if strong else [terms]
    for active in passes:
        expression = " OR ".join('"' + t.replace('"', '""') + '"' for t in active)
        args = [expression, kind]
        if kind == "record":
            args.append(store.config["policy_hash"])
        args.extend([chat_id, chat_id, segment_id, segment_id])
        rows = [] if unified(store) else store.db.execute(
            f"SELECT docs.id,docs.body,bm25(docs) rank FROM docs WHERE docs MATCH ? AND docs.type=? AND {scope} "
            "ORDER BY bm25(docs), docs.id LIMIT 201",
            args,
        ).fetchall()
        external_rows = _source_candidates(store, expression, kind, chat_id, segment_id)
        merged = {r[0]: r for r in rows}
        merged.update({r[0]: r for r in external_rows})
        rows = sorted(merged.values(), key=lambda r: (r[2], r[0]))
        if rows:
            return [(r[0], r[1]) for r in rows[:CAP]], len(rows) > CAP
    return [], False


def _entry(store, eid):
    row = store.db.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()
    return hydrate(store, row)


def _snippet(text, terms, cap):
    # Prefer the strongest/longest matching term rather than the first common single character.
    lower = text.lower()
    matched = [t for t in terms if t in lower]
    term = min(matched, key=lambda t: (is_han_single(t), -len(t), lower.find(t))) if matched else ""
    start = max(0, lower.find(term) - cap // 4) if term else 0
    part = text[start : start + cap]
    return part, start, len(part) != len(text)


def _graph(store, hits, remaining, terms, chat_id=None, segment_id=None):
    related = []
    seen_sources = set()
    record_ids = []
    for hit in hits:
        sources = (
            [hit["id"]] if hit["type"] == "entry" else [e["entry_id"] for e in hit["evidence"]]
        )
        seen_sources.update(sources)
        for eid in sources:
            rows = store.db.execute(
                """SELECT DISTINCT r.id FROM evidence ev JOIN records r ON r.id=ev.owner_id
                JOIN jobs j ON j.id=r.job_id WHERE ev.entry_id=? AND j.policy_hash=? ORDER BY r.id LIMIT 8""",
                (eid, store.config["policy_hash"]),
            ).fetchall()
            record_ids.extend(r[0] for r in rows)
    seen_records = set(record_ids)
    for rid in list(dict.fromkeys(record_ids))[:24]:
        edges = store.db.execute(
            "SELECT * FROM edges WHERE source=? OR target=? ORDER BY id LIMIT 12", (rid, rid)
        ).fetchall()
        for edge in edges:
            target = edge["target"] if edge["source"] == rid else edge["source"]
            if target in seen_records or remaining <= 0 or len(related) >= 8:
                continue
            seen_records.add(target)
            row = store.db.execute(
                """SELECT r.*,c.chat_id FROM records r JOIN jobs j ON j.id=r.job_id
                JOIN session_chats c ON c.segment_id=j.session_id
                WHERE r.id=? AND j.policy_hash=?
                AND (? IS NULL OR c.chat_id=?) AND (? IS NULL OR c.segment_id=?)""",
                (target, store.config["policy_hash"], chat_id, chat_id, segment_id, segment_id),
            ).fetchone()
            if not row:
                continue
            refs = store._evidence(target, include_quotes=False)
            ids = {e["entry_id"] for e in refs}
            if not ids - seen_sources:
                continue
            # Return source text, not just a generated relationship claim.
            evidence_entry = _entry(
                store, next(e["entry_id"] for e in refs if e["entry_id"] not in seen_sources)
            )
            part, start, truncated = _snippet(evidence_entry["text"], terms, min(400, remaining))
            related.append(
                {
                    "id": target,
                    "type": "record",
                    "kind": row["kind"],
                    "chat_id": row["chat_id"],
                    "relation": edge["kind"],
                    "direction": "outgoing" if edge["source"] == rid else "incoming",
                    "via_record_id": rid,
                    "edge_id": edge["id"],
                    "snippet": part,
                    "snippet_entry_id": evidence_entry["id"],
                    "snippet_start": start,
                    "truncated": truncated,
                    "evidence": refs,
                    "edge_evidence": store._evidence(edge["id"], include_quotes=False),
                }
            )
            remaining -= len(part)
            seen_sources.update(ids)
    return related, remaining


def search(
    store,
    query,
    limit=8,
    max_chars=6000,
    session_id=None,
    mode="hybrid",
    *,
    chat_id=None,
    segment_id=None,
):
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise ValueError("query must contain 1..1000 characters")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if type(max_chars) is not int or not 256 <= max_chars <= 50000:
        raise ValueError("max_chars must be between 256 and 50000")
    if mode not in {"raw", "hybrid", "graph"}:
        raise ValueError("mode must be raw, hybrid or graph")
    # `session_id` was the original public name but always meant logical chat ID.
    # Keep it as a compatibility alias while making exact rollout filtering explicit.
    if session_id is not None:
        if chat_id is not None:
            raise ValueError("use either session_id or chat_id, not both")
        chat_id = session_id
    for name, value in (("chat_id", chat_id), ("segment_id", segment_id)):
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 256):
            raise ValueError(f"{name} must be a non-empty string of at most 256 characters")
    scope_chat_id = chat_id
    terms = query_terms(query)
    raw_ids, raw_capped = _candidates(store, terms, "entry", scope_chat_id, segment_id)
    derived_ids, derived_capped = (
        _candidates(store, terms, "record", scope_chat_id, segment_id)
        if mode != "raw"
        else ([], False)
    )
    raw_cache = {}
    unavailable = []
    if raw_ids:
        placeholders = ",".join("?" for _ in raw_ids)
        rows = store.db.execute(
            f"SELECT * FROM entries WHERE id IN ({placeholders})", [eid for eid, _ in raw_ids]
        )
        for row in rows:
            try:
                raw_cache[row['id']] = hydrate(store, row)
            except ValueError as exc:
                if not str(exc).startswith('source_'):
                    raise
                raw_cache[row['id']] = None
                unavailable.append(dict(entry_id=row['id'], status=str(exc)))
    token_cache = {eid: IndexedTokens(body) for eid, body in raw_ids if body is not None}
    chat_cache = {}

    def chat_id(sid):
        if sid not in chat_cache:
            chat_cache[sid] = store.chat_id(sid)
        return chat_cache[sid]

    candidates = []

    def entry(eid):
        if eid not in raw_cache:
            try:
                raw_cache[eid] = _entry(store, eid)
            except ValueError as exc:
                if not str(exc).startswith('source_'):
                    raise
                raw_cache[eid] = None
                unavailable.append(dict(entry_id=eid, status=str(exc)))
        return raw_cache[eid]

    def raw_score(row):
        eid = row["id"]
        if eid not in token_cache:
            found = None if unified(store) or external(store, eid) else store.db.execute(
                "SELECT body FROM docs WHERE id=? AND type='entry'", (eid,)
            ).fetchone()
            token_cache[eid] = IndexedTokens(found[0]) if found else set(tokens(row["text"]))
        return lexical(row["text"], terms, present=token_cache[eid])

    def canonical(row):
        return digest(
            chat_id(row["session_id"]) + ":" + row["role"] + ":" + row["kind"] + ":" + row["text"]
        )

    for eid, _body in raw_ids:
        row = entry(eid)
        if row is None:
            continue
        score = raw_score(row)
        if score:
            candidates.append(
                {
                    "row": row,
                    "text": row["text"],
                    "type": "entry",
                    "score": score,
                    "sources": {row["id"]},
                    "evidence": [],
                }
            )
    for rid, _body in derived_ids:
        row = dict(
            store.db.execute(
                "SELECT r.*,j.session_id FROM records r JOIN jobs j ON j.id=r.job_id WHERE r.id=?",
                (rid,),
            ).fetchone()
        )
        refs = store._evidence(rid, include_quotes=False)
        originals = [entry(e["entry_id"]) for e in refs]
        originals = [e for e in originals if e]
        if not originals:
            continue
        best = max(originals, key=raw_score)
        source_score = raw_score(best)
        derived_score = max(
            0.7 * lexical(row["content"], terms),
            0.3 * lexical(row["scope"], terms),
            max(
                (0.35 * lexical(alias, terms) for alias in json.loads(row["aliases"])),
                default=0,
            ),
        )
        score = max(source_score, derived_score)
        if not score:
            continue
        if source_score >= derived_score:
            # A derived record can recall its original, but cannot elevate it above its raw score.
            candidates.append(
                {
                    "row": best,
                    "text": best["text"],
                    "type": "entry",
                    "score": source_score,
                    "sources": {best["id"]},
                    "evidence": [],
                }
            )
        else:
            candidates.append(
                {
                    "row": row,
                    "text": row["content"],
                    "type": "record",
                    "score": derived_score,
                    "sources": {e["id"] for e in originals},
                    "evidence": refs,
                }
            )
    candidates.sort(key=lambda c: (-c["score"], c["type"] != "entry", c["row"]["id"]))
    seen = set()
    hits = []
    remaining = max_chars
    # Graph mode preserves a bounded text reserve; it cannot silently exceed the same budget.
    seed_budget = max_chars if mode != "graph" else max_chars * 3 // 4
    for candidate in candidates:
        fingerprints = {canonical(entry(eid)) for eid in candidate["sources"]}
        if fingerprints & seen:
            continue
        if len(hits) >= limit or seed_budget <= 0:
            break
        seen.update(fingerprints)
        row = candidate["row"]
        part, start, truncated = _snippet(candidate["text"], terms, min(600, seed_budget))
        hit = {
            "id": row["id"],
            "type": candidate["type"],
            "kind": row["kind"],
            "chat_id": chat_id(row["session_id"]),
            "session_id": row["session_id"],
            "snippet": part,
            "snippet_start": start,
            "truncated": truncated,
            "score": round(candidate["score"], 6),
        }
        if candidate["type"] == "entry":
            hit.update(role=row["role"], line=row["line"], timestamp=row["timestamp"])
        else:
            hit["evidence"] = candidate["evidence"]
            hit["scope"] = row["scope"][: min(200, seed_budget - len(part))]
            seed_budget -= len(hit["scope"])
            remaining -= len(hit["scope"])
        hits.append(hit)
        seed_budget -= len(part)
        remaining -= len(part)
    related = []
    if mode == "graph":
        related, remaining = _graph(
            store, hits, remaining, terms, scope_chat_id, segment_id
        )
    status = store.coverage()
    scope = {
        "chat_id": scope_chat_id,
        "segment_id": segment_id,
        "chat_exists": (
            store.db.execute("SELECT 1 FROM session_chats WHERE chat_id=? LIMIT 1", (scope_chat_id,)).fetchone()
            is not None
            if scope_chat_id is not None
            else None
        ),
        "segment_exists": (
            store.db.execute("SELECT 1 FROM session_chats WHERE segment_id=?", (segment_id,)).fetchone()
            is not None
            if segment_id is not None
            else None
        ),
    }
    return {
        "query": query,
        "hits": hits,
        "related": related,
        "model_calls": 0,
        "unavailable_sources": unavailable,
        "scope": scope,
        "coverage": {
            k: status[k]
            for k in (
                "project_id",
                "sessions",
                "chats",
                "source_segments",
                "raw_entries",
                "semantic_entries",
                "normal_semantic_entries",
                "semantic_entries_total",
                "records",
                "edges",
                "policy_version",
            )
        },
        "budget": {"max_text_chars": max_chars, "returned_text_chars": max_chars - remaining},
        "retrieval": {
            "version": "evidence-v2",
            "mode": mode,
            "candidate_limit_reached": raw_capped or derived_capped,
        },
        "note": "Candidates are not answers. Expand source evidence; absence is not proven by no match.",
    }
