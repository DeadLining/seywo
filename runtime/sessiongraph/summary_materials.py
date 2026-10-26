"""Bounded source-backed material projection. Read-only, no provider or extraction."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .graph_contract import SECRET
from .read_diagnostics import phase

PAGE_SIZE = 200


def validate_query(query):
    if not isinstance(query, dict) or set(query) - {"start", "end", "offset", "revision", "check_only"}:
        raise ValueError("invalid material query")
    dates = []
    for key in ("start", "end"):
        value = query.get(key)
        if not isinstance(value, str) or len(value) > 40:
            raise ValueError("invalid material date")
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("material dates require timezone")
        dates.append(dt.astimezone(timezone.utc))
    if not 0 < (dates[1] - dates[0]).total_seconds() <= 32 * 86400:
        raise ValueError("invalid material range")
    offset = query.get("offset", 0)
    if type(offset) is not int or not 0 <= offset <= 10000:
        raise ValueError("invalid material offset")
    if query.get("revision") is not None and (not isinstance(query["revision"], str) or len(query["revision"]) != 64):
        raise ValueError("invalid material revision")
    if type(query.get("check_only", False)) is not bool:
        raise ValueError("invalid material check")
    if offset and not query.get("revision"):
        raise ValueError("pagination requires revision")
    return dates[0].isoformat(), dates[1].isoformat(), offset


def revision(store):
    # Includes raw import/deletion as well as graph changes; graph revision alone is insufficient.
    stamps = []
    for name in (str(store.config["database"]), str(store.config["database"]) + "-wal"):
        try:
            st = Path(name).stat()
            stamps.append([st.st_ino, st.st_size, st.st_mtime_ns])
        except FileNotFoundError:
            stamps.append(None)
    return hashlib.sha256(json.dumps(stamps).encode()).hexdigest()


def safe_text(value):
    return SECRET.sub("[redacted]", value if isinstance(value, str) else "")


def assertion_rows(store, ids):
    """Hydrate only selected evidence, loading each source message once per batch."""
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    claims = {r["id"]: dict(r) for r in store.db.execute(f"""SELECT x.*,n.type
        FROM sg2_evidence x JOIN sg2_nodes n ON n.id=x.node WHERE x.id IN ({marks})""", ids)}
    source_ids = sorted({r["entry_id"] for r in claims.values()})
    source_marks = ",".join("?" for _ in source_ids)
    sources = {r["id"]: r for r in store.db.execute(
        f"SELECT id,timestamp,role,text FROM entries WHERE id IN ({source_marks})", source_ids)} if source_ids else {}
    rows = []
    for object_id in ids:
        claim = claims.get(object_id)
        if claim is not None and claim["entry_id"] in sources:
            source = sources[claim["entry_id"]]
            rows.append({**claim, **{key: source[key] for key in ("timestamp", "role", "text")}})
    return rows


def candidates(store, query):
    start, end, offset = validate_query(query)
    rev = revision(store)
    if query.get("revision") and query["revision"] != rev:
        raise ValueError("material_revision_changed")
    base = {"schema_version": 1, "project_id": store.config["project_id"], "revision": rev, "model_calls": 0}
    if query.get("check_only"):
        return base
    tables = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"sg2_nodes", "sg2_evidence", "sg2_relations", "sg2_relation_evidence"} <= tables:
        return {**base, "assertions": [], "relations": [], "next_offset": None,
                "coverage": {"state": "not_initialized", "read_state": "unavailable", "reason": "结构化材料尚未建立"}}
    # No read-side migration. Writer-created covering indexes keep metadata reads
    # off the wide source/evidence tables; legacy databases remain readable.
    window = "julianday(e.timestamp)>=julianday(?) AND julianday(e.timestamp)<julianday(?)"
    # Materialize only in-range IDs/timestamps, then pin them as the join driver.
    # An ordinary JOIN lets SQLite scan every historical evidence ID and randomly
    # fetch its source before filtering, even for an empty day. CROSS JOIN retains
    # a range-first plan, with indexed evidence lookups on upgraded databases.
    # Keep the existing timestamp/ID ordering and hydrate text only after LIMIT.
    with phase("summary.rows"):
        page_ids = [r[0] for r in store.db.execute(f"""WITH recent AS MATERIALIZED (
                SELECT e.id,e.timestamp FROM entries e WHERE {window})
            SELECT x.id FROM recent e
            CROSS JOIN sg2_evidence x ON x.entry_id=e.id
            CROSS JOIN sg2_nodes n ON n.id=x.node
            WHERE n.project=? ORDER BY e.timestamp,x.id LIMIT ? OFFSET ?""",
            (start, end, store.config["project_id"], PAGE_SIZE + 1, offset))]
    with phase("summary.payload"):
        rows = assertion_rows(store, page_ids[:PAGE_SIZE])
    assertions, invalid = [], 0
    from .revision_ledger import projected_many
    projections = projected_many(store, [r['node'] for r in rows])
    def revision_ids(evidence_ids):
        if not projections or not evidence_ids:
            return {}
        marks = ','.join('?' for _ in evidence_ids)
        return dict(store.db.execute(f'SELECT evidence,revision FROM sg_revision_sources WHERE evidence IN ({marks})', evidence_ids))
    assertion_revisions = revision_ids([r['id'] for r in rows])
    source_hashes = {}
    def add_assertion(r, in_range=True):
        nonlocal invalid
        try:
            claim = json.loads(r["assertion_json"])
            if not isinstance(claim, dict) or not r["quote"] or r["quote"] not in r["text"]:
                raise ValueError("invalid evidence")
            if any(not isinstance(claim.get(k), str) for k in ("title", "content", "status")):
                raise ValueError("missing assertion")
            if len(claim["title"]) > 1000 or len(claim["content"]) > 10000 or len(r["quote"]) > 4000:
                raise ValueError("oversized assertion")
            if r["entry_id"] not in source_hashes:
                source_hashes[r["entry_id"]] = hashlib.sha256(r["text"].encode()).hexdigest()
            assertions.append({"id": r["id"], "node_id": r["node"], "type": r["type"],
                "title": safe_text(claim["title"]), "content": safe_text(claim["content"]),
                "status": claim["status"], "context_kind": claim.get("context_kind", "uncertain"),
                "entry_id": r["entry_id"], "quote": safe_text(r["quote"]), "role": r["role"],
                "recorded_at": r["timestamp"], "source_hash": source_hashes[r["entry_id"]],
                "time_basis": "recorded_only", "in_range": in_range})
            validity = projections.get(r['node'])
            if validity is not None:
                # The historical payload stays intact. Current state is context,
                # not a rewrite of what was reported during this date range.
                assertions[-1]['current_validity'] = validity
                historical = assertion_revisions.get(r['id'])
                assertions[-1]['assertion_revision'] = historical
                assertions[-1]['assertion_state'] = ('needs_review' if validity['state'] != 'current'
                    else 'current' if historical == validity['revision'] else 'not_current')
        except (ValueError, TypeError):
            invalid += 1
    for r in rows[:PAGE_SIZE]:
        add_assertion(r)
    # Only asserted, accepted relations whose own quoted evidence predates the range end.
    # Membership can predate this period. No inferred/proposed edges silently merge work.
    ids = sorted({a["node_id"] for a in assertions})
    relations = []
    relation_overflow = False
    if ids:
        marks = ",".join("?" for _ in ids)
        with phase("summary.relations"):
            relation_ids = [r[0] for r in store.db.execute(f"""SELECT x.id
              FROM sg2_relations r JOIN sg2_relation_evidence x ON x.relation=r.id
              JOIN entries e ON e.id=x.entry_id WHERE r.project=? AND r.state='supported' AND r.mode='asserted'
              AND (r.source IN ({marks}) OR r.target IN ({marks}))
              AND julianday(e.timestamp)<julianday(?) ORDER BY r.id,x.id LIMIT 1001""",
              (store.config["project_id"], *ids, *ids, end))]
        relation_overflow = len(relation_ids) > 1000
        rels = []
        if relation_ids:
            with phase("summary.relation_payload"):
                selected = relation_ids[:1000]
                relation_marks = ",".join("?" for _ in selected)
                raw = {r["evidence_id"]: dict(r) for r in store.db.execute(f"""SELECT
                    x.id evidence_id,r.id,r.source,r.target,r.kind,x.entry_id,x.quote
                    FROM sg2_relation_evidence x JOIN sg2_relations r ON r.id=x.relation
                    WHERE x.id IN ({relation_marks})""", selected)}
                entry_ids = sorted({r["entry_id"] for r in raw.values()})
                entry_marks = ",".join("?" for _ in entry_ids)
                sources = {r["id"]: r for r in store.db.execute(
                    f"SELECT id,timestamp,text FROM entries WHERE id IN ({entry_marks})", entry_ids)} if entry_ids else {}
                rels = [{**raw[key], "timestamp": sources[raw[key]["entry_id"]]["timestamp"],
                         "text": sources[raw[key]["entry_id"]]["text"]} for key in selected
                        if key in raw and raw[key]["entry_id"] in sources]
        for r in rels[:1000]:
            if r["quote"] and r["quote"] in r["text"]:
                relations.append({k: safe_text(r[k]) for k in ("id", "source", "target", "kind", "entry_id", "quote", "timestamp")})
    # One bounded batch for historical parent/context nodes. These are never counted
    # as new work. No current work_memory fields or per-node IPC round trips.
    related = sorted({r[k] for r in relations for k in ("source", "target")} - set(ids))
    if related:
        marks = ",".join("?" for _ in related)
        with phase("summary.context"):
            context_ids = [r[0] for r in store.db.execute(f"""SELECT id FROM (
                SELECT x.id,
                       row_number() OVER(PARTITION BY x.node ORDER BY julianday(e.timestamp) DESC,x.id DESC) rn
                FROM sg2_evidence x JOIN entries e ON e.id=x.entry_id JOIN sg2_nodes n ON n.id=x.node
                WHERE x.node IN ({marks}) AND n.project=? AND julianday(e.timestamp)<julianday(?)
                ) WHERE rn=1 ORDER BY id LIMIT 201""", (*related, store.config["project_id"], start))]
        if len(context_ids) > 200:
            relation_overflow = True
        with phase("summary.context_payload"):
            context_rows = assertion_rows(store, context_ids[:200])
        projections.update(projected_many(store, [r['node'] for r in context_rows]))
        assertion_revisions.update(revision_ids([r['id'] for r in context_rows]))
        for r in context_rows:
            add_assertion(r, False)
    # Older databases lack an entry_id index on sg2_evidence. A correlated EXISTS
    # rescans all evidence for every source message. The independent IN subquery
    # builds one membership set, retains duplicate-safe counts, and needs no migration.
    with phase("summary.coverage"):
        total, covered = store.db.execute(f"""SELECT count(*),coalesce(sum(
            e.id IN (SELECT entry_id FROM sg2_evidence)),0) FROM entries e
            WHERE {window} AND e.role IN ('user','assistant')""", (start, end)).fetchone()
    if revision(store) != rev:
        raise ValueError("material_revision_changed")
    return {**base, "assertions": assertions, "relations": relations,
            "next_offset": offset + PAGE_SIZE if len(page_ids) > PAGE_SIZE else None,
            "coverage": {"state": "partial" if invalid or relation_overflow or total > covered else "complete",
                         "read_state": "partial" if invalid or relation_overflow else "complete",
                         "source_entries": total, "covered_entries": covered, "invalid_evidence": invalid,
                         "relations_truncated": relation_overflow,
                         "reason": "按原文记录时间筛选；实际发生时间需核验。覆盖数表示存在图谱证据，不代表所有语义已提取。"}}
