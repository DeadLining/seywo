"""Bounded project retrieval over safe sources, immutable narratives and memory."""

import base64
import json
from contextlib import contextmanager
from itertools import islice, chain

from . import work_memory
from .projection import search_tokens


def serialize(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid_" + name)
    return value


def identity(kind, *parts):
    return (
        "sw:"
        + kind
        + ":"
        + base64.urlsafe_b64encode(serialize(parts).encode()).decode().rstrip("=")
    )


def parse_identity(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("invalid_object_id")
    try:
        prefix, kind, raw = value.split(":", 2)
        parts = json.loads(
            base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
        )
    except (ValueError, UnicodeError):
        raise ValueError("invalid_object_id") from None
    if (
        prefix != "sw"
        or kind not in {"s", "n", "m"}
        or not isinstance(parts, list)
        or len(parts) != {"s": 3, "n": 3, "m": 1}[kind]
    ):
        raise ValueError("invalid_object_id")
    return kind, parts


@contextmanager
def snapshot(store):
    nested = store.db.in_transaction
    if not nested:
        store.db.execute("BEGIN")
    try:
        yield
    finally:
        if not nested:
            store.db.rollback()


def page(rows, *, offset=0, limit=8, max_bytes=4096, **metadata):
    integer(offset, "offset", 0, 10**9)
    integer(limit, "limit", 1, 100)
    integer(max_bytes, "max_bytes", 512, 64000)
    result = {
        **metadata,
        "results": [],
        "retrieval": {"offset": offset, "next_offset": None, "has_more": False},
        "model_calls": 0,
    }
    candidates = list(islice(iter(rows), offset, offset + limit + 1))

    def with_rows(selected):
        count = len(selected)
        more = count < len(candidates)
        return {
            **result,
            "results": selected,
            "retrieval": {
                "offset": offset,
                "next_offset": offset + count if more else None,
                "has_more": more,
            },
        }

    result = with_rows([])
    for row in candidates[:limit]:
        trial = with_rows([*result["results"], row])
        if len(serialize(trial).encode()) > max_bytes:
            break
        result = trial
    if candidates and not result["results"]:
        required = len(serialize(with_rows(candidates[:1])).encode())
        result["budget"] = {"max_bytes": max_bytes, "required_bytes": required}
    if len(serialize(result).encode()) > max_bytes:
        return {
            "results": [],
            "budget": {
                "max_bytes": max_bytes,
                "required_bytes": len(serialize(result).encode()),
            },
            "model_calls": 0,
        }
    return result


class Retrieval:
    def __init__(self, store):
        self.store, self.db = store, store.db

    def scope(self, chat_ids=None):
        from .admission import NonHumanSource

        if chat_ids is not None and (
            not isinstance(chat_ids, list) or len(chat_ids) > 100
        ):
            raise ValueError("invalid_chat_ids")
        requested = chat_ids or [
            r[0] for r in self.db.execute("SELECT id FROM chats ORDER BY id")
        ]
        chats = []
        for chat in requested:
            if not isinstance(chat, str) or not chat or len(chat) > 256:
                raise ValueError("invalid_chat_id")
            if not self.db.execute(
                "SELECT 1 FROM chats WHERE id=?", (chat,)
            ).fetchone():
                raise ValueError("unknown_chat_id")
            try:
                self.store.require_human_chat(chat)
            except NonHumanSource:
                if chat_ids:
                    raise
                continue
            chats.append(chat)
        return sorted(set(chats))

    @staticmethod
    def query(query):
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise ValueError("invalid_query")
        return list(dict.fromkeys(search_tokens(query).split()))

    def _visible(self, chat_id, event_id, snapshot_id):
        for value in (chat_id, event_id, snapshot_id):
            if not isinstance(value, str) or not value or len(value) > 256:
                raise ValueError("invalid_source_identity")
        self.store.require_human_chat(chat_id)
        row = self.db.execute(
            "SELECT f.*,c.state,c.reason,s.chat_id,s.head_snapshot,e.record_no,s.ordinal "
            "FROM snapshot_fragments f JOIN coverage_ranges c ON c.fragment_id=f.row_id "
            "JOIN source_events e ON e.id=f.event_id JOIN source_segments s ON s.id=e.segment_id "
            "WHERE f.event_id=? AND f.snapshot_id=? AND s.chat_id=?",
            (event_id, snapshot_id, chat_id),
        ).fetchone()
        if (
            not row
            or row["state"] in {"excluded_mechanical", "unavailable"}
            or row["role"] in {"system", "developer"}
            or row["kind"] == "reasoning"
        ):
            raise ValueError("source_not_available")
        return row

    def _source_brief(self, row):
        return {
            "id": identity("s", row["chat_id"], row["event_id"], row["snapshot_id"]),
            **{
                k: row[k]
                for k in (
                    "chat_id",
                    "event_id",
                    "snapshot_id",
                    "record_no",
                    "role",
                    "kind",
                )
            },
            "preview": row["preview"],
            "char_start": row["char_start"],
            "source_state": row["state"],
        }

    def _raw(self, tokens, chats, include_tools):
        if not tokens:
            return []
        scope = (
            " AND c.chat_id IN (" + ",".join("?" for _ in chats) + ")"
            if chats
            else " AND 0"
        )
        dialogue = (
            ""
            if include_tools
            else " AND c.kind='message' AND c.role IN ('user','assistant') AND (c.channel IS NULL OR c.channel IN ('final','commentary'))"
        )
        # Keep exact hit coordinates; decode only matching payloads and a short preview.
        rows = self.db.execute(
            "SELECT c.event_id,c.snapshot_id,c.chat_id,c.record_no,c.role,c.kind,c.state,"
            "max(0,instr(lower(c.safe_text),?)-61) char_start,"
            "substr(c.safe_text,max(1,instr(lower(c.safe_text),?)-60),240) preview FROM source_fts JOIN current_sources c ON c.payload_id=source_fts.rowid "
            "WHERE source_fts MATCH ? AND c.state NOT IN ('excluded_mechanical','unavailable') "
            "AND c.role NOT IN ('system','developer') AND c.kind<>'reasoning'"
            + scope
            + dialogue
            + " ORDER BY rank,c.ordinal,c.record_no,c.event_id",
            [
                max(tokens, key=len).lower(),
                max(tokens, key=len).lower(),
                " AND ".join('"' + t + '"' for t in tokens),
                *chats,
            ],
        )
        return (self._source_brief(r) for r in rows)

    def _documents(self, chats):
        scope = (
            " AND h.chat_id IN (" + ",".join("?" for _ in chats) + ")"
            if chats
            else " AND 0"
        )
        rows = self.db.execute(
            "SELECT v.chat_id,v.version,v.created_at,v.body_json FROM narrative_heads h "
            "JOIN narrative_versions v ON v.chat_id=h.chat_id AND v.version=h.version WHERE 1=1"
            + scope
            + " ORDER BY v.created_at DESC,v.chat_id",
            chats,
        )
        for row in rows:
            self.store.require_human_chat(row["chat_id"])
            yield row, json.loads(row["body_json"])

    def _node(self, row, item):
        return {
            "id": identity("n", row["chat_id"], row["version"], item["id"]),
            "kind": "narrative_item",
            "chat_id": row["chat_id"],
            "version": row["version"],
            "item_id": item["id"],
            "title": item.get("title", ""),
            "type": item["type"],
            "preview": item["content"][:180],
            "evidence": [
                {k: e[k] for k in ("event_id", "snapshot_id", "start", "end")}
                for e in item["evidence"][:2]
            ],
            "evidence_count": len(item["evidence"]),
        }

    def _derived(self, tokens, chats, include_superseded=False):
        def matches(value):
            indexed = set(search_tokens(value).split())
            return all(t in indexed for t in tokens)

        rows = []
        for memory in (
            work_memory.list_rows(self.db, chats, include_superseded) if chats else []
        ):
            if matches(
                memory["title"] + " " + memory["content"] + " " + memory["aliases_json"]
            ):
                rows.append(
                    {
                        "id": identity("m", memory["id"]),
                        "kind": "memory",
                        "memory_id": memory["id"],
                        "chat_id": memory["chat_id"],
                        "title": memory["title"],
                        "type": memory["kind"],
                        "status": memory["status"],
                        "preview": memory["content"][:180],
                        "evidence": [
                            {
                                "event_id": memory["event_id"],
                                "snapshot_id": memory["snapshot_id"],
                                "start": memory["start"],
                                "end": memory["end"],
                            }
                        ],
                    }
                )
        for row, doc in self._documents(chats):
            rows.extend(
                self._node(row, i)
                for i in doc["items"]
                if not i["hidden"] and matches(i.get("title", "") + " " + i["content"])
            )
        return rows

    def search(
        self,
        query,
        chat_ids=None,
        mode="raw",
        include_tools=False,
        offset=0,
        limit=8,
        max_bytes=4096,
    ):
        if mode not in {"raw", "hybrid", "graph"} or type(include_tools) is not bool:
            raise ValueError("invalid_search_mode")
        tokens, chats = self.query(query), self.scope(chat_ids)
        rows = self._raw(tokens, chats, include_tools)
        if mode != "raw":
            derived = self._derived(tokens, chats)
            neighbors = []
            if mode == "graph":
                seen = {r["id"] for r in derived}
                for match in derived:
                    if match["kind"] != "narrative_item":
                        continue
                    doc = self._body(match["chat_id"], match["version"])
                    item = next(i for i in doc["items"] if i["id"] == match["item_id"])
                    adjacent = set(item["dependencies"]) | {
                        i["id"] for i in doc["items"] if item["id"] in i["dependencies"]
                    }
                    for i in doc["items"]:
                        n = self._node(match, i)
                        if (
                            i["id"] in adjacent
                            and not i["hidden"]
                            and n["id"] not in seen
                        ):
                            neighbors.append(
                                {**n, "match_reason": "dependency_neighbor"}
                            )
                            seen.add(n["id"])
            rows = chain(rows, derived, neighbors)
        return page(
            rows,
            offset=offset,
            limit=limit,
            max_bytes=max_bytes,
            scope={"project_id": self.store.config["project_id"], "chat_ids": chats},
            mode=mode,
            source_only=mode == "raw",
        )

    def read_source(
        self, chat_id, event_id, snapshot_id, start=0, end=None, max_bytes=4096
    ):
        integer(start, "start", 0, 10**9)
        integer(max_bytes, "max_bytes", 512, 64000)
        row = self._visible(chat_id, event_id, snapshot_id)
        text = row["safe_text"]
        if start > len(text):
            raise ValueError("invalid_source_range")
        end = min(start + 4000, len(text)) if end is None else end
        integer(end, "end", start, min(start + 8000, len(text)))
        result = {
            k: row[k]
            for k in (
                "chat_id",
                "event_id",
                "snapshot_id",
                "role",
                "kind",
                "record_no",
                "ordinal",
                "occurred_at",
            )
        }
        current = self.db.execute(
            "SELECT safe_sha256,role,kind FROM snapshot_fragments WHERE event_id=? AND snapshot_id=?",
            (event_id, row["head_snapshot"]),
        ).fetchone()
        result.update(
            char_start=start,
            char_end=end,
            text=text[start:end],
            next_start=end if end < len(text) else None,
            source_current=bool(
                current
                and current["safe_sha256"] == row["safe_sha256"]
                and current["role"] == row["role"]
                and current["kind"] == row["kind"]
            ),
            model_calls=0,
            source_only=True,
        )
        # Shorten only the requested excerpt and expose the exact continuation.
        while len(serialize(result).encode()) > max_bytes and result["text"]:
            excess = len(serialize(result).encode()) - max_bytes
            result["text"] = result["text"][
                : max(0, len(result["text"]) - max(1, excess // 4))
            ]
            result["char_end"] = start + len(result["text"])
            result["next_start"] = result["char_end"]
        if len(serialize(result).encode()) > max_bytes or (
            end > start and not result["text"]
        ):
            return {"error": "response_budget_too_small", "model_calls": 0}
        return result

    def _body(self, chat, version=None):
        self.store.require_human_chat(chat)
        if version is not None:
            integer(version, "version", 1, 10**9)
        row = self.db.execute(
            "SELECT v.version,v.body_json FROM narrative_versions v JOIN narrative_heads h ON h.chat_id=v.chat_id "
            "WHERE v.chat_id=? AND v.version=coalesce(?,h.version)",
            (chat, version),
        ).fetchone()
        if not row:
            raise ValueError("narrative_not_published")
        return json.loads(row["body_json"])

    def read_narrative(self, chat_id, version=None, offset=0, limit=12, max_bytes=4096):
        from .narrative import Narrative

        view = Narrative(self.store).read(chat_id, version=version)
        doc = view["narrative"]
        locations = {p["item_id"]: p for p in doc["placements"]}
        rows = [
            {**self._node(view, i), "placement": locations.get(i["id"])}
            for i in doc["items"]
            if not i["hidden"]
        ]
        return page(
            rows,
            offset=offset,
            limit=limit,
            max_bytes=max_bytes,
            chat_id=chat_id,
            version=view["version"],
            state=view["state"],
            update_pending=view["update_pending"],
            review_count=view["review_count"],
            phases=[{"id": p["id"], "title": p["title"]} for p in doc["phases"]],
            branches=[
                {k: b[k] for k in ("id", "phase_id", "title", "state")}
                for b in doc["branches"]
            ],
            overview=doc["overview"],
        )

    def recall(
        self,
        query,
        chat_ids=None,
        include_superseded=False,
        offset=0,
        limit=3,
        max_bytes=4096,
    ):
        tokens, chats = self.query(query), self.scope(chat_ids)
        return page(
            self._derived(tokens, chats, include_superseded),
            offset=offset,
            limit=limit,
            max_bytes=max_bytes,
            source_only=False,
            scope={"chat_ids": chats},
        )

    def resume(self, chat_ids=None, offset=0, limit=5, max_bytes=4096):
        chats = self.scope(chat_ids)
        rows = []
        for row, doc in self._documents(chats):
            by_id = {i["id"]: i for i in doc["items"] if not i["hidden"]}
            overview = doc["overview"]

            def entries(key):
                return [
                    {
                        "id": identity("n", row["chat_id"], row["version"], i),
                        "title": by_id[i].get("title", ""),
                    }
                    for i in overview.get(key, [])
                    if i in by_id
                ]

            rows.append(
                {
                    "kind": "chat_resume",
                    "chat_id": row["chat_id"],
                    "version": row["version"],
                    "focus": overview["focus"],
                    "goals": entries("goal_item_ids"),
                    "open_questions": entries("open_question_ids"),
                    "next_steps": entries("next_step_ids"),
                }
            )
        for m in work_memory.list_rows(self.db, chats) if chats else []:
            rows.append(
                {
                    "kind": "memory",
                    "id": identity("m", m["id"]),
                    "chat_id": m["chat_id"],
                    "title": m["title"],
                    "status": m["status"],
                }
            )
        return page(
            rows,
            offset=offset,
            limit=limit,
            max_bytes=max_bytes,
            scope={"chat_ids": chats},
        )

    def expand(self, object_id, radius=0, offset=0, limit=8, max_bytes=4096):
        integer(radius, "radius", 0, 3)
        kind, parts = parse_identity(object_id)
        if kind == "s":
            chat, event, snap = parts
            row = self._visible(chat, event, snap)
            if not radius:
                return self.read_source(
                    chat, event, snap, start=offset, max_bytes=max_bytes
                )
            neighbors = self.db.execute(
                "SELECT f.event_id,e.record_no FROM snapshot_fragments f JOIN source_events e ON e.id=f.event_id "
                "JOIN coverage_ranges c ON c.fragment_id=f.row_id WHERE f.snapshot_id=? AND e.record_no BETWEEN ? AND ? "
                "AND c.state NOT IN ('excluded_mechanical','unavailable') AND (f.event_id=? OR (f.role IN ('user','assistant') AND f.kind='message')) ORDER BY e.record_no",
                (snap, row["record_no"] - radius, row["record_no"] + radius, event),
            ).fetchall()
            rows = [
                self.read_source(
                    chat, n["event_id"], snap, max_bytes=min(max_bytes, 2000)
                )
                for n in neighbors
            ]
        elif kind == "n":
            chat, version, item_id = parts
            doc = self._body(chat, version)
            item = next((i for i in doc["items"] if i["id"] == item_id), None)
            if not item:
                raise ValueError("item_not_found")
            selected = {item_id}
            for _ in range(radius):
                selected |= {
                    d
                    for i in doc["items"]
                    if i["id"] in selected
                    for d in i["dependencies"]
                }
                selected |= {
                    i["id"] for i in doc["items"] if set(i["dependencies"]) & selected
                }
            rows = [
                {
                    "id": identity("n", chat, version, i["id"]),
                    "chat_id": chat,
                    "version": version,
                    "item": i,
                    "placement": next(
                        (p for p in doc["placements"] if p["item_id"] == i["id"]), None
                    ),
                }
                for i in [
                    item,
                    *[
                        i
                        for i in doc["items"]
                        if i["id"] != item_id
                        and i["id"] in selected
                        and not i["hidden"]
                    ],
                ]
            ]
        else:
            if not work_memory.exists(self.db):
                raise ValueError("memory_not_found")
            row = self.db.execute(
                "SELECT * FROM work_memories WHERE id=?", parts
            ).fetchone()
            if not row:
                raise ValueError("memory_not_found")
            self.store.require_human_chat(row["chat_id"])
            memory = work_memory.decode(row)
            source = self.read_source(
                row["chat_id"],
                row["event_id"],
                row["snapshot_id"],
                start=row["start"],
                end=row["end"],
                max_bytes=32000,
            )
            memory["source_current"] = source.get("source_current", False)
            rows = [memory]
        return page(rows, offset=offset, limit=limit, max_bytes=max_bytes)

    def packet(self, query, chat_ids=None, max_bytes=4096, max_sources=4):
        integer(max_sources, "max_sources", 1, 20)
        chats, tokens = self.scope(chat_ids), self.query(query)
        hits = list(islice(self._raw(tokens, chats, False), max_sources + 1))
        excerpts = [
            self.read_source(
                h["chat_id"],
                h["event_id"],
                h["snapshot_id"],
                start=h["char_start"],
                max_bytes=2000,
            )
            for h in hits[:max_sources]
        ]
        return page(
            excerpts,
            limit=max_sources,
            max_bytes=max_bytes,
            more_sources=len(hits) > max_sources,
            scope={"chat_ids": chats},
            source_only=True,
        )

    def status(self, chat_ids=None, offset=0, limit=20, max_bytes=4096):
        chats = self.scope(chat_ids)
        where = ",".join("?" for _ in chats) or "NULL"
        coverage = dict(
            self.db.execute(
                "SELECT state,count(*) FROM current_sources WHERE chat_id IN ("
                + where
                + ") GROUP BY state",
                chats,
            )
        )
        jobs = self.db.execute(
            "SELECT state,count(*) n,sum(call_count) calls FROM understanding_jobs WHERE chat_id IN ("
            + where
            + ") GROUP BY state",
            chats,
        ).fetchall()
        memories = work_memory.list_rows(self.db, chats) if chats else []
        return page(
            [{"chat_id": chat} for chat in chats],
            offset=offset,
            limit=limit,
            max_bytes=max_bytes,
            project_id=self.store.config["project_id"],
            chats=len(chats),
            source_segments=self.db.execute(
                "SELECT count(*) FROM source_segments WHERE chat_id IN (" + where + ")",
                chats,
            ).fetchone()[0],
            coverage=coverage,
            understanding_jobs={r["state"]: r["n"] for r in jobs},
            historical_model_attempts=sum(r["calls"] or 0 for r in jobs),
            memories=len(memories),
            search_covers="imported_current_safe_sources",
            no_match_proves_absence=False,
        )
