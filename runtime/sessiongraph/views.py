"""Loopback-only read-only explorer. No extraction or provider calls."""

from __future__ import annotations

from .source_text import hydrate


# Upper bound on work-memory nodes drawn at once. The full item is always
# reachable by search or by raising this; the graph is a view, not an export.
MEMORY_NODE_LIMIT = 300


def graph_data(store, query="", chat="", include_superseded=False):
    chats = [
        dict(r)
        for r in store.db.execute(
            "SELECT c.chat_id,count(*) AS count FROM entries e JOIN session_chats c "
            "ON c.segment_id=e.session_id GROUP BY c.chat_id ORDER BY c.chat_id"
        )
    ]
    nodes, edges = {}, []

    def entry(eid):
        if eid in nodes:
            return
        r = store.db.execute(
            "SELECT e.*,c.chat_id FROM entries e JOIN session_chats c "
            "ON c.segment_id=e.session_id WHERE e.id=?",
            (eid,),
        ).fetchone()
        if not r:
            return
        cid = "chat:" + r["chat_id"]
        r = hydrate(store, r)
        nodes[cid] = dict(id=cid, type="chat", label=r["chat_id"], chat_id=r["chat_id"])
        nodes[eid] = dict(
            id=eid,
            type="entry",
            kind=r["role"],
            label=r["text"][:160],
            chat_id=r["chat_id"],
            line=r["line"],
        )
        edges.append(dict(source=cid, target=eid, kind="CONTAINS"))

    def memory(mid):
        """A work_memory item: the project-level handoff layer.

        Distinct from `record` below, which is the older per-batch semantic
        layer tied to a policy hash. Work memory has no policy: it is explicit
        and source-backed, and it is what a new chat actually resumes from.
        """
        from .graph_compaction import canonical
        mid=canonical(store,mid)
        if mid in nodes:
            return
        r = store.db.execute("SELECT * FROM work_memory WHERE id=?", (mid,)).fetchone()
        if not r:
            return
        if r["status"] in {"superseded", "cancelled"} and not include_superseded:
            return
        nodes[mid] = dict(
            id=mid,
            type="memory",
            kind=r["kind"],
            status=r["status"],
            label=r["title"],
            content=r["content"],
            scope=r["scope"],
            project_id=r["project_id"],
            chat_id=r["chat_id"],
            updated_at=r["updated_at"],
        )
        if r["source_entry_id"]:
            entry(r["source_entry_id"])
            if r["source_entry_id"] in nodes:
                edges.append(dict(source=mid, target=r["source_entry_id"], kind="EVIDENCED_BY"))
        if r["supersedes_id"] and r["supersedes_id"] in nodes:
            edges.append(dict(source=mid, target=r["supersedes_id"], kind="SUPERSEDES"))

    def record(rid):
        if rid in nodes:
            return
        r = store.db.execute(
            "SELECT r.* FROM records r JOIN jobs j ON j.id=r.job_id "
            "WHERE r.id=? AND j.policy_hash=?",
            (rid, store.config["policy_hash"]),
        ).fetchone()
        if not r:
            return
        nodes[rid] = dict(id=rid, type="record", kind=r["kind"], label=r["content"][:240])
        for ev in store._evidence(rid)[:6]:
            entry(ev["entry_id"])
            edges.append(dict(source=rid, target=ev["entry_id"], kind="EVIDENCED_BY"))

    from .graph_compaction import expression
    canonical_memory=expression(store,"work_memory.id")
    truncated = False
    if query:
        rows = store.db.execute(
            f"""SELECT DISTINCT {canonical_memory} id FROM work_memory
               WHERE status NOT IN ('superseded','cancelled')
               AND (title LIKE ? OR content LIKE ?)
               ORDER BY updated_at DESC LIMIT 12""",
            (f"%{query}%", f"%{query}%"),
        ).fetchall()
        for r in rows:
            memory(r["id"])
        result = store.search(query, limit=12, max_chars=6000, chat_id=chat or None, mode="hybrid")
        for hit in result["hits"]:
            (entry if hit["type"] == "entry" else record)(hit["id"])
    else:
        live_total = store.db.execute(
            f"""SELECT count(DISTINCT {canonical_memory}) FROM work_memory
               WHERE status NOT IN ('superseded','cancelled')
               AND (?="" OR chat_id=?)""",
            (chat, chat),
        ).fetchone()[0]
        truncated = live_total > MEMORY_NODE_LIMIT
        rows = store.db.execute(
            f"""SELECT DISTINCT {canonical_memory} id FROM work_memory
               WHERE status NOT IN ('superseded','cancelled')
               AND (?="" OR chat_id=?)
               ORDER BY updated_at DESC, id DESC LIMIT ?""",
            (chat, chat, MEMORY_NODE_LIMIT),
        ).fetchall()
        for r in rows:
            memory(r["id"])
        rows = store.db.execute(
            "SELECT r.id FROM records r JOIN jobs j ON j.id=r.job_id "
            "JOIN session_chats c ON c.segment_id=j.session_id "
            'WHERE j.policy_hash=? AND (?="" OR c.chat_id=?) '
            "ORDER BY r.id LIMIT 60",
            (store.config["policy_hash"], chat, chat),
        )
        for r in rows.fetchall():
            record(r["id"])
        if not nodes:
            rows = store.db.execute(
                "SELECT e.id FROM entries e JOIN session_chats c "
                'ON c.segment_id=e.session_id WHERE e.kind="message" '
                'AND (?="" OR c.chat_id=?) ORDER BY e.timestamp DESC,e.id '
                "LIMIT 12",
                (chat, chat),
            ).fetchall()
            for r in rows:
                entry(r["id"])
    # Recover records for raw evidence seeds, then expand at most 100 semantic edges.
    for eid in list(nodes):
        if nodes[eid]["type"] == "entry":
            for r in store.db.execute(
                "SELECT owner_id FROM evidence WHERE entry_id=? LIMIT 4", (eid,)
            ).fetchall():
                record(r["owner_id"])
    seeds = [n["id"] for n in nodes.values() if n["type"] == "record"]
    if seeds:
        marks = ",".join("?" for _ in seeds)
        rows = store.db.execute(
            f"SELECT e.* FROM edges e JOIN jobs j ON j.id=e.job_id WHERE j.policy_hash=? "
            f"AND (e.source IN ({marks}) OR e.target IN ({marks})) ORDER BY e.id LIMIT 100",
            [store.config["policy_hash"], *seeds, *seeds],
        ).fetchall()
        for r in rows:
            record(r["source"])
            record(r["target"])
            if r["source"] in nodes and r["target"] in nodes:
                edges.append(
                    dict(
                        id=r["id"],
                        source=r["source"],
                        target=r["target"],
                        kind=r["kind"],
                        evidence=store._evidence(r["id"]),
                    )
                )
    from .graph_compaction import visible
    memory_stats = dict(
        store.db.execute(f"SELECT status,count(*) FROM work_memory WHERE {visible(store,'work_memory.id')} GROUP BY status").fetchall()
    )
    return dict(
        nodes=list(nodes.values()),
        edges=edges,
        chats=chats,
        memory_stats=memory_stats,
        truncated=truncated,
        status=store.status(False),
        model_calls=0,
        semantic_edges=store.db.execute(
            "SELECT count(*) FROM edges e JOIN jobs j ON j.id=e.job_id WHERE j.policy_hash=?",
            (store.config["policy_hash"],),
        ).fetchone()[0],
        notice=(
            f"局部视图：最多 {MEMORY_NODE_LIMIT} 条工作记忆 / 60 个起始记录 / 12 个搜索命中、"
            "100 条语义关系；非完整图谱。工作记忆为项目级，关联可跨聊天。"
        ),
    )


def memory_detail(store, mid: str, *, offset=0):
    """Full work-memory item plus the original evidence it was drawn from.

    Read-only and model-free: the quote is re-located in the source entry so the
    caller can see exactly which span was cited.
    """
    from .memory import _record

    if not mid or len(mid) > 256:
        raise ValueError("invalid memory id")
    from .graph_compaction import canonical
    requested=mid
    mid=canonical(store,mid)
    row = store.db.execute("SELECT * FROM work_memory WHERE id=?", (mid,)).fetchone()
    if not row:
        raise ValueError("unknown memory id")
    item = _record(row, store)
    entries = []
    if item["source"]:
        eid = item["source"]["entry_id"]
        e = store.db.execute(
            "SELECT e.*,c.chat_id FROM entries e JOIN session_chats c "
            "ON c.segment_id=e.session_id WHERE e.id=?",
            (eid,),
        ).fetchone()
        if e:
            entries.append(hydrate(store, e))
    superseded_by = [
        dict(r)
        for r in store.db.execute(
            "SELECT id,title,status,updated_at FROM work_memory WHERE supersedes_id=? "
            "AND status NOT IN ('cancelled') ORDER BY updated_at DESC",
            (mid,),
        ).fetchall()
    ]
    from .graph_v2 import node_details
    return dict(memory=item, entries=entries, superseded_by=superseded_by, graph_v2=node_details(store, requested, offset=offset), model_calls=0)
