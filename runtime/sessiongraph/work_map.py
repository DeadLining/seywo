"""Read-only work-item projection: exact references and stored semantic claims.

Never merge memories by title, promote source messages to work nodes, or infer
causality from co-occurrence. Shared references only aid navigation. Semantic
relations are separately validated and labeled as extracted claims. Stored
claims remain unverified even when their source quote can be located.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from collections import Counter, defaultdict

from .auto_update import revision
from .work_relations import read as read_relations
from .read_diagnostics import phase

LIVE = ("proposed", "active", "in_progress", "blocked", "completed")
PATH = re.compile(r"(?<![\w:/])(?:/|\./|[\w.-]+/)[\w./@+~-]+\.[a-zA-Z0-9]{1,12}(?![\w/])")


def _list(raw):
    try:
        value = json.loads(raw or "[]")
        return [s for s in value if isinstance(s, str)] if isinstance(value, list) else []
    except (ValueError, TypeError):
        return []


def _has(text, value, patterns):
    # Avoid matching 'API' inside 'capital' or 'compact' inside 'auto_compaction'.
    # Page-local compiled patterns avoid re's small global LRU thrashing when
    # hundreds of aliases are tested against every row. Never cache source text.
    if value not in patterns:
        patterns[value] = re.compile(
            r"(?<![A-Za-z0-9_])" + re.escape(value) + r"(?![A-Za-z0-9_])", re.I
        )
    return bool(patterns[value].search(text))


def _id(kind, value):
    return "ref_" + hashlib.sha256((kind + ":" + value).encode()).hexdigest()[:24]


def work_map(store, chat="", limit=1000, query="", offset=0):
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    if type(offset) is not int or not 0 <= offset <= 50_000_000:
        raise ValueError("invalid offset")
    if not isinstance(query, str) or len(query) > 1000 or not isinstance(chat, str) or len(chat) > 256:
        raise ValueError("invalid search")
    from .storage import search_filter, exists
    where = "m.status IN (?,?,?,?,?) AND m.project_id=?"
    args = [*LIVE, store.config["project_id"]]
    total = store.db.execute(f"SELECT count(*) FROM work_memory m WHERE {where}", args).fetchone()[0]
    if chat:
        where += " AND (m.chat_id=?"
        args.append(chat)
        if exists(store.db, "sg2_evidence"):
            where += " OR EXISTS(SELECT 1 FROM sg2_evidence x WHERE x.node=m.id AND x.chat=?)"
            args.append(chat)
        where += ")"
    if query.strip():
        if not exists(store.db, "memory_search"):
            raise ValueError("memory search index needs writable runtime initialization")
        predicate, search_args = search_filter(store.db,query,'m')
        where += " AND " + predicate
        args.extend(search_args)
    matched = store.db.execute(f"SELECT count(*) FROM work_memory m WHERE {where}", args).fetchone()[0]
    with phase("map.rows"):
        rows = [
            dict(r)
            for r in store.db.execute(
                f"SELECT m.* FROM work_memory m WHERE {where} "
                "ORDER BY m.updated_at DESC,m.id DESC LIMIT ? OFFSET ?",
                [*args, limit, offset],
            )
        ]
    chats = [
        dict(r)
        for r in store.db.execute(
            "SELECT chat_id,count(*) AS count FROM work_memory WHERE project_id=? "
            "AND status IN (?,?,?,?,?) GROUP BY chat_id ORDER BY chat_id",
            [store.config["project_id"], *LIVE],
        )
    ]
    root = store.config["roots"][0]
    nodes, edges, references = {}, [], defaultdict(list)
    from .read_projection import page
    with phase("map.references"):
        projected = page(store, rows)
    candidates = {t for value in projected.values() for t in value["aliases"]}
    valid = {}
    from .revision_ledger import projected_many
    revisions = projected_many(store, [r['id'] for r in rows])
    for r in rows:
        quote = r["source_quote"] or ""
        ok = projected[r["id"]]["valid"]
        valid[r["id"]] = ok
        nodes[r["id"]] = dict(
            id=r["id"],
            type="memory",
            kind=r["kind"],
            status=r["status"],
            label=r["title"],
            content=r["content"],
            chat_id=r["chat_id"],
            updated_at=r["updated_at"],
            evidence_status="quote_located" if ok else "missing_or_mismatched",
        )
        validity = revisions.get(r['id'])
        if validity is not None:
            nodes[r['id']]['validity'] = validity
    for r in rows:
        if not valid[r["id"]]:
            continue
        projected_row = projected[r["id"]]
        for canonical, path in projected_row["paths"]:
            references[("path", canonical)].append((r, path))
        for term in sorted(projected_row["mentions"] & candidates):
            references[("term", term)].append((r, term))
    for (kind, identity), mentions in sorted(references.items()):
        if kind == "term" and len(mentions) < 2:
            continue
        rid = _id(kind, identity)
        label = (
            posixpath.relpath(identity, root)
            if kind == "path" and identity.startswith(root + "/")
            else identity
        )
        nodes[rid] = dict(
            id=rid,
            type="reference",
            kind=kind,
            label=label,
            path=identity if kind == "path" else None,
            reference_count=len(mentions),
        )
        for row, literal in mentions:
            quote = row["source_quote"]
            start = max(0, quote.casefold().find(literal.casefold()) - 120)
            edges.append(
                dict(
                    source=row["id"],
                    target=rid,
                    kind="REFERENCES_PATH" if kind == "path" else "MENTIONS",
                    basis="exact_reference",
                    evidence=[
                        dict(
                            entry_id=row["source_entry_id"],
                            memory_id=row["id"],
                            quote=quote[start : start + 600],
                            literal=literal,
                        )
                    ],
                )
            )
    reference_edges = len(edges)
    with phase("map.relations"):
        semantic = read_relations(store, [r["id"] for r in rows], both=True, chat=chat, limit=1000)
    edges.extend(semantic)
    degrees = Counter()
    for edge in edges:
        degrees.update((edge["source"], edge["target"]))
    for node in nodes.values():
        node["degree"] = degrees[node["id"]]
    from .graph_v2 import diagnostics, exists as has_v2
    if has_v2(store) and rows:
        ids = [r["id"] for r in rows]
        marks = ",".join("?" for _ in ids)
        for row in store.db.execute(f"SELECT id,type FROM sg2_nodes WHERE project=? AND id IN ({marks})", (store.config["project_id"], *ids)):
            if row["id"] in nodes:
                nodes[row["id"]]["object_type"] = row["type"]
        for row in store.db.execute(f"SELECT DISTINCT node,chat FROM sg2_evidence WHERE node IN ({marks})", ids):
            if row["node"] in nodes:
                nodes[row["node"]].setdefault("chat_ids", []).append(row["chat"])
        chat_counts = Counter()
        for node in nodes.values():
            if node["type"] == "memory":
                chat_counts.update(node.get("chat_ids") or [node["chat_id"]])
        # Keep project-wide chat choices across result pages.
        chats = [dict(r) for r in store.db.execute("SELECT chat_id,count(DISTINCT id) count FROM (SELECT id,chat_id FROM work_memory WHERE project_id=? UNION SELECT m.id,x.chat FROM work_memory m JOIN sg2_evidence x ON x.node=m.id WHERE m.project_id=?) WHERE chat_id IS NOT NULL GROUP BY chat_id ORDER BY chat_id", (store.config["project_id"],store.config["project_id"]))]
    with phase("map.diagnostics"):
        graph_diagnostics = diagnostics(store)
    return dict(
        graph_v2=graph_diagnostics,
        revision=revision(store.db, store.config["project_id"]),
        nodes=list(nodes.values()),
        edges=edges,
        chats=chats,
        model_calls=0,
        truncated=offset + len(rows) < matched,
        page=dict(offset=offset, limit=limit, matched=matched, next_offset=offset+len(rows) if offset+len(rows)<matched else None, query=query, chat=chat),
        stats=dict(
            memories=len(rows),
            total_memories=total,
            references=len(nodes) - len(rows),
            relations=len(edges),
            reference_relations=reference_edges,
            semantic_relations=len(semantic),
            unlinked_memories=sum(not degrees[r["id"]] for r in rows),
            invalid_sources=sum(not value for value in valid.values()),
        ),
        notice="虚线为字面引用；实线箭头为有原文依据的语义关系声明，仍需核实，不代表已验证事实。"
        "不自动推断因果、合并同名事项或变更决策状态。",
    )
