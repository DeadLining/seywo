"""Storage, Codex rollout adapter and deterministic retrieval. No network imports."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def init_project(root: Path) -> Path:
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    state = root / ".sessiongraph"
    state.mkdir(mode=0o700, exist_ok=True)
    config = state / "config.json"
    if config.exists():
        raise ValueError("already initialized; existing configuration was not modified")
    policy = json.loads(Path(__file__).with_name("policy.json").read_text())
    (state / "policy.json").write_text(json.dumps(policy, ensure_ascii=False, indent=2) + "\n")
    config.write_text(
        json.dumps(
            {
                "project_id": digest(str(root))[:20],
                "roots": [str(root)],
                "database": "index.sqlite3",
                "policy": "policy.json",
                "provider": {
                    "base_url_env": "GRAPH_LLM_BASE_URL",
                    "api_key_env": "GRAPH_LLM_API_KEY",
                    "model_env": "GRAPH_LLM_MODEL",
                    "timeout_seconds": 90,
                    "max_output_tokens": 4000,
                },
                "extraction": {
                    "max_batch_chars": 24000,
                    "context_entries": 2,
                    "max_input_chars": 32000,
                    "lease_seconds": 600,
                },
                "hook": {"allowed_transcript_roots": []},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    config.chmod(0o600)
    (state / "policy.json").chmod(0o600)
    (state / ".gitignore").write_text("*\n!.gitignore\n")
    return config


def load_config(path: Path) -> dict:
    path = path.expanduser().resolve()
    cfg = json.loads(path.read_text())
    cfg["_config_path"] = str(path)
    if not isinstance(cfg.get("project_id"), str) or not cfg["project_id"]:
        raise ValueError("project_id is required")
    if not isinstance(cfg.get("roots"), list) or not cfg["roots"]:
        raise ValueError("at least one project root is required")
    cfg["roots"] = [str(Path(r).expanduser().resolve()) for r in cfg["roots"]]
    cfg["database"] = str((path.parent / cfg["database"]).resolve())
    cfg["policy"] = json.loads((path.parent / cfg["policy"]).read_text())
    cfg["policy_hash"] = digest(json.dumps(cfg["policy"], sort_keys=True))
    opts = cfg["extraction"]
    for key in ("max_batch_chars", "max_input_chars", "lease_seconds"):
        if type(opts[key]) is not int or opts[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    if type(opts["context_entries"]) is not int or not 0 <= opts["context_entries"] <= 20:
        raise ValueError("context_entries must be between 0 and 20")
    if opts["max_input_chars"] < opts["max_batch_chars"]:
        raise ValueError("max_input_chars must be >= max_batch_chars")
    from .pipeline_settings import apply_defaults
    apply_defaults(cfg)
    from .provider_settings import apply_provider
    apply_provider(cfg)
    return cfg


def tokens(text: str) -> list[str]:
    """Latin identifiers + CJK unigrams/bigrams; no model or downloaded tokenizer."""
    out = []
    for part in re.findall(r"[\u3400-\u9fff]+|[a-zA-Z0-9_]+", text.lower()):
        if "\u3400" <= part[0] <= "\u9fff":
            out.extend(part)
            out.extend(part[i : i + 2] for i in range(len(part) - 1))
        else:
            out.append(part)
    return list(dict.fromkeys(out))


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
 id TEXT PRIMARY KEY,path TEXT UNIQUE NOT NULL,cwd TEXT NOT NULL,
 offset INTEGER NOT NULL,lines INTEGER NOT NULL,prefix_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS session_chats(
 segment_id TEXT PRIMARY KEY REFERENCES sessions(id),chat_id TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS session_chats_chat ON session_chats(chat_id);
CREATE TABLE IF NOT EXISTS raw_events(
 session_id TEXT NOT NULL REFERENCES sessions(id),line INTEGER NOT NULL,
 event_json TEXT NOT NULL,PRIMARY KEY(session_id,line));
CREATE TABLE IF NOT EXISTS entries(
 id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id),line INTEGER NOT NULL,
 timestamp TEXT NOT NULL,kind TEXT NOT NULL,role TEXT NOT NULL,text TEXT NOT NULL,
 call_id TEXT,UNIQUE(session_id,line));
CREATE INDEX IF NOT EXISTS entries_session ON entries(session_id,line);
CREATE INDEX IF NOT EXISTS entries_call ON entries(session_id,call_id);
-- Cover date filtering without reading large source text; retain all timestamp formats.
CREATE INDEX IF NOT EXISTS entries_materials_window ON entries(timestamp,id,role);
CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(id UNINDEXED,type UNINDEXED,body);
CREATE TABLE IF NOT EXISTS jobs(
 id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id),
 start_line INTEGER NOT NULL,end_line INTEGER NOT NULL,policy_hash TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
 lease_until REAL,lease_token TEXT,error TEXT,summary TEXT,provenance TEXT,
 UNIQUE(session_id,start_line,end_line,policy_hash));
CREATE TABLE IF NOT EXISTS reviewed_batches(
 job_id TEXT PRIMARY KEY REFERENCES jobs(id),manifest_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS jobs_coverage ON jobs(session_id,policy_hash,state,start_line,end_line);
CREATE TABLE IF NOT EXISTS records(
 id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES jobs(id),kind TEXT NOT NULL,
 content TEXT NOT NULL,scope TEXT NOT NULL,aliases TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence(
 owner_id TEXT NOT NULL,entry_id TEXT NOT NULL REFERENCES entries(id),quote TEXT NOT NULL,
 start INTEGER NOT NULL,end INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS evidence_owner ON evidence(owner_id);
CREATE TABLE IF NOT EXISTS edges(
 id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES jobs(id),
 source TEXT NOT NULL REFERENCES records(id),target TEXT NOT NULL REFERENCES records(id),
 kind TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS edges_source ON edges(source);
CREATE INDEX IF NOT EXISTS edges_target ON edges(target);
CREATE TABLE IF NOT EXISTS work_memory(
 id TEXT PRIMARY KEY,project_id TEXT,chat_id TEXT,kind TEXT NOT NULL,title TEXT NOT NULL,
 content TEXT NOT NULL,scope TEXT NOT NULL,status TEXT NOT NULL,
 paths_json TEXT NOT NULL,aliases_json TEXT NOT NULL,source_entry_id TEXT,
 source_quote TEXT NOT NULL,supersedes_id TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS work_memory_status ON work_memory(status,updated_at);
CREATE INDEX IF NOT EXISTS work_memory_chat ON work_memory(chat_id,status);
CREATE INDEX IF NOT EXISTS work_memory_project ON work_memory(project_id,status);
CREATE TABLE IF NOT EXISTS work_memory_relations(
 id TEXT PRIMARY KEY,project_id TEXT NOT NULL,
 source TEXT NOT NULL REFERENCES work_memory(id),target TEXT NOT NULL REFERENCES work_memory(id),
 kind TEXT NOT NULL,source_entry_id TEXT NOT NULL REFERENCES entries(id),source_quote TEXT NOT NULL,
 source_mention TEXT NOT NULL,target_mention TEXT NOT NULL,created_at TEXT NOT NULL,
 CHECK(source != target));
CREATE INDEX IF NOT EXISTS work_relations_project ON work_memory_relations(project_id);
CREATE INDEX IF NOT EXISTS work_relations_source ON work_memory_relations(source);
CREATE INDEX IF NOT EXISTS work_relations_target ON work_memory_relations(target);
"""


def normalize(payload: dict):
    kind = payload.get("type")
    if kind == "message" and payload.get("role") in {"user", "assistant"}:
        content = payload.get("content", [])
        if not isinstance(content, list):
            raise ValueError("message content must be an array")
        text = "\n".join(
            x["text"]
            for x in content
            if isinstance(x, dict)
            and x.get("type") in {"input_text", "output_text"}
            and isinstance(x.get("text"), str)
        )
        return "message", payload["role"], text, None
    if kind in {"function_call", "custom_tool_call"}:
        value = payload.get("arguments", payload.get("input", ""))
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return (
            "tool_call",
            "assistant",
            f"{payload.get('name', '')}\n{text}",
            payload.get("call_id"),
        )
    if kind in {"function_call_output", "custom_tool_call_output"}:
        value = payload.get("output", "")
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return "tool_result", "tool", text, payload.get("call_id")
    return None


class Store:
    def __init__(self, config: dict, readonly: bool = False):
        self.config = config
        self.readonly = readonly
        path = Path(config["database"]).expanduser().resolve()
        if readonly:
            if not path.is_file():
                raise ValueError(f"database does not exist: {path}")
            # MCP/UI callers must not create schema, switch journal mode, or backfill metadata.
            # SQLite's URI read-only mode also prevents accidental WAL/metadata writes.
            self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.5)
        else:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Create private database before SQLite can create accompanying files.
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        from .source_text import register as register_sources
        register_sources(self)
        self.db.execute("PRAGMA foreign_keys=ON")
        if readonly:
            self.db.execute("PRAGMA query_only=ON")
        if not readonly:
            self.db.execute("PRAGMA journal_mode=WAL")
            # Add columns before SCHEMA runs: SCHEMA declares an index on
            # work_memory.project_id, which fails on a pre-existing database
            # whose table predates that column.
            self._add_missing_columns()
            self.db.executescript(SCHEMA)
        try:
            for key, value in (("schema_version", "1"), ("project_id", config["project_id"])):
                prior = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
                if key == 'schema_version' and prior and prior[0] in {'2', '3', '4'}:
                    continue  # Source-backed storage; old runtimes must fail closed.
                if prior and prior[0] != value:
                    raise ValueError(f"database {key} does not match configuration")
            if not readonly:
                with self.db:
                    self.db.executemany(
                        "INSERT OR IGNORE INTO metadata VALUES (?,?)",
                        [("schema_version", "1"), ("project_id", config["project_id"])],
                    )
                    self.db.execute("INSERT OR IGNORE INTO session_chats SELECT id,id FROM sessions")
                    # Backfill scope for rows written before the column existed.
                    self.db.execute(
                        "UPDATE work_memory SET project_id=? WHERE project_id IS NULL",
                        (config["project_id"],),
                    )
            if not readonly:
                from .storage import setup as storage_setup
                storage_setup(self)
                from .read_projection import setup as projection_setup
                projection_setup(self)
                from .source_locator import setup as locator_setup
                locator_setup(self)
                from .source_text import setup as source_setup
                source_setup(self)
                from .source_text import activate_defaults
                activate_defaults(self)
                from .job_lifecycle import setup as job_lifecycle_setup
                with self.db:
                    job_lifecycle_setup(self.db)
        except Exception:
            self.db.close()
            raise

    def _add_missing_columns(self) -> None:
        """Bring an existing index up to the current column set.

        Work memory is project-scoped: the database is per project, and the
        metadata check guarantees that. project_id makes the scope explicit and
        queryable instead of implied by which file happened to be opened.
        """
        row = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_memory'"
        ).fetchone()
        if not row:
            return  # fresh database; SCHEMA creates the current shape
        columns = {r["name"] for r in self.db.execute("PRAGMA table_info(work_memory)")}
        if "project_id" not in columns:
            self.db.execute("ALTER TABLE work_memory ADD COLUMN project_id TEXT")

    def _ensure_writable(self):
        if self.readonly:
            raise ValueError("store is read-only")

    def chat_id(self, segment_id: str) -> str:
        return self.db.execute(
            "SELECT chat_id FROM session_chats WHERE segment_id=?", (segment_id,)
        ).fetchone()[0]

    def close(self):
        try:
            if not self.readonly and not self.db.in_transaction:
                from .read_projection import flush
                flush(self)
        finally:
            self.db.close()

    def ingest(self, path: Path) -> dict:
        from .ingestion import ingest

        return ingest(self, path)

    def enqueue(self, session_id: str | None = None) -> int:
        self._ensure_writable()
        created = 0
        cap = self.config["extraction"]["max_batch_chars"]
        ph = self.config["policy_hash"]
        with self.db:
            # Serialize watermark selection and range insertion across hook processes.
            self.db.execute("BEGIN IMMEDIATE")
            sessions = self.db.execute(
                "SELECT id FROM sessions WHERE (? IS NULL OR id IN (SELECT segment_id FROM session_chats WHERE chat_id=? OR segment_id=?))",
                (session_id, session_id, session_id),
            ).fetchall()
            for session in sessions:
                sid = session[0]
                watermark = self.db.execute(
                    "SELECT COALESCE(MAX(end_line),0) FROM jobs WHERE session_id=? AND policy_hash=? AND id NOT IN (SELECT job_id FROM reviewed_batches)",
                    (sid, ph),
                ).fetchone()[0]
                entries = self.db.execute(
                    "SELECT line,coalesce((SELECT chars FROM sg_entry_sizes WHERE entry_id=entries.id),length(text)) AS size FROM entries WHERE session_id=? AND line>? ORDER BY line",
                    (sid, watermark),
                ).fetchall()
                batch, size = [], 0
                batches = []
                for entry in entries:
                    if batch and size + entry["size"] > cap:
                        batches.append(batch)
                        batch, size = [], 0
                    batch.append(entry["line"])
                    size += entry["size"]
                if batch:
                    batches.append(batch)
                for batch in batches:
                    jid = "j_" + digest(f"{sid}:{batch[0]}:{batch[-1]}:{ph}")[:32]
                    self.db.execute(
                        "INSERT INTO jobs(id,session_id,start_line,end_line,policy_hash) VALUES (?,?,?,?,?)",
                        (jid, sid, batch[0], batch[-1], ph),
                    )
                    created += 1
        return created

    def retry_failed(self) -> int:
        self._ensure_writable()
        with self.db:
            return self.db.execute(
                "UPDATE jobs SET state='pending',error=NULL WHERE state='failed' AND policy_hash=?",
                (self.config["policy_hash"],),
            ).rowcount

    def coverage(self) -> dict:
        """Return only counters needed on the hot search path."""
        from .graph_v2 import enabled, exists
        if enabled(self) and exists(self):
            from .status_v2 import coverage
            result = coverage(self)
            chats = self.db.execute("SELECT count(DISTINCT chat_id) FROM session_chats").fetchone()[0]
            return {**result, "project_id": self.config["project_id"],
                    "policy_version": self.config["policy"]["version"], "policy_hash": self.config["policy_hash"],
                    "sessions": chats, "chats": chats,
                    "source_segments": self.db.execute("SELECT count(*) FROM sessions").fetchone()[0],
                    "normal_semantic_entries": result["semantic_entries"],
                    "semantic_entries_total": result["semantic_entries"]}
        db = self.db
        ph = self.config["policy_hash"]
        raw_entries = db.execute("SELECT count(*) FROM entries").fetchone()[0]
        completed_entries = db.execute(
            """SELECT count(DISTINCT e.id) FROM entries e JOIN jobs j
            ON j.session_id=e.session_id AND e.line BETWEEN j.start_line AND j.end_line
            WHERE j.state='done' AND j.policy_hash=?""",
            (ph,),
        ).fetchone()[0]
        normal_processed = db.execute(
            """SELECT count(*) FROM entries e WHERE EXISTS
            (SELECT 1 FROM jobs j WHERE j.session_id=e.session_id AND j.state='done'
             AND j.policy_hash=? AND j.id NOT IN (SELECT job_id FROM reviewed_batches)
             AND e.line BETWEEN j.start_line AND j.end_line)""",
            (ph,),
        ).fetchone()[0]
        chat_count = db.execute("SELECT count(DISTINCT chat_id) FROM session_chats").fetchone()[0]
        return {
            "project_id": self.config["project_id"],
            "policy_version": self.config["policy"]["version"],
            "policy_hash": ph,
            "sessions": chat_count,
            "chats": chat_count,
            "source_segments": db.execute("SELECT count(*) FROM sessions").fetchone()[0],
            "raw_entries": raw_entries,
            "semantic_entries": completed_entries,
            "normal_semantic_entries": normal_processed,
            "semantic_entries_total": completed_entries,
            "records": db.execute(
                "SELECT count(*) FROM records r JOIN jobs j ON j.id=r.job_id WHERE j.policy_hash=?",
                (ph,),
            ).fetchone()[0],
            "edges": db.execute(
                "SELECT count(*) FROM edges e JOIN jobs j ON j.id=e.job_id WHERE j.policy_hash=?",
                (ph,),
            ).fetchone()[0],
        }

    def status(self, detailed: bool = True) -> dict:
        from .graph_v2 import enabled, exists
        if enabled(self) and exists(self):
            from .status_v2 import status
            return status(self, detailed)
        from .memory import memory_status

        db = self.db
        ph = self.config["policy_hash"]
        known_states = ("pending", "running", "done", "failed")
        counts = {state: 0 for state in known_states}
        counts.update(
            dict(
                db.execute(
                    "SELECT state,count(*) FROM jobs WHERE policy_hash=? GROUP BY state", (ph,)
                )
            )
        )
        normal_processed = db.execute(
            """SELECT count(*) FROM entries e WHERE EXISTS
            (SELECT 1 FROM jobs j WHERE j.session_id=e.session_id AND j.state='done'
             AND j.policy_hash=? AND j.id NOT IN (SELECT job_id FROM reviewed_batches)
             AND e.line BETWEEN j.start_line AND j.end_line)""",
            (ph,),
        ).fetchone()[0]
        completed_entries = db.execute(
            """SELECT count(DISTINCT e.id) FROM entries e JOIN jobs j
            ON j.session_id=e.session_id AND e.line BETWEEN j.start_line AND j.end_line
            WHERE j.state='done' AND j.policy_hash=?""",
            (ph,),
        ).fetchone()[0]
        raw_entries = db.execute("SELECT count(*) FROM entries").fetchone()[0]
        reviewed_excerpts = sum(
            len(json.loads(r[0]))
            for r in db.execute(
                """SELECT b.manifest_json FROM reviewed_batches b JOIN jobs j ON j.id=b.job_id
                WHERE j.policy_hash=?""",
                (ph,),
            )
        )
        records = db.execute(
            """SELECT count(*) FROM records r JOIN jobs j ON j.id=r.job_id
            WHERE j.policy_hash=?""",
            (ph,),
        ).fetchone()[0]
        edges = db.execute(
            """SELECT count(*) FROM edges e JOIN jobs j ON j.id=e.job_id
            WHERE j.policy_hash=?""",
            (ph,),
        ).fetchone()[0]
        return {
            "project_id": self.config["project_id"],
            "policy_version": self.config["policy"]["version"],
            "policy_hash": ph,
            "sessions": db.execute("SELECT count(DISTINCT chat_id) FROM session_chats").fetchone()[
                0
            ],
            "chats": db.execute("SELECT count(DISTINCT chat_id) FROM session_chats").fetchone()[0],
            "source_segments": db.execute("SELECT count(*) FROM sessions").fetchone()[0],
            "raw_entries": raw_entries,
            "semantic_entries": completed_entries,
            "normal_semantic_entries": normal_processed,
            "semantic_entries_total": completed_entries,
            "reviewed_source_entries": max(0, completed_entries - normal_processed),
            "reviewed_excerpts": reviewed_excerpts,
            "records": records,
            "edges": edges,
            "work_memory": memory_status(self),
            "jobs": counts,
            "coverage": {
                "policy_hash": ph,
                "raw_entries": raw_entries,
                "completed_entries": completed_entries,
                "pending_entries": max(0, raw_entries - completed_entries),
                "ratio": round(completed_entries / raw_entries, 6) if raw_entries else 0.0,
            },
            "sources": [
                dict(r)
                for r in db.execute(
                    """SELECT s.id,c.chat_id,s.lines,s.offset FROM sessions s
                    JOIN session_chats c ON c.segment_id=s.id ORDER BY s.id LIMIT 100"""
                )
            ]
            if detailed
            else [],
            "source_listing_limit": 100,
            "failed_jobs": [
                dict(r)
                for r in db.execute(
                    "SELECT id,error FROM jobs WHERE state='failed' AND policy_hash=? LIMIT 20",
                    (ph,),
                )
            ]
            if detailed
            else [],
        }

    def remember(self, **kwargs) -> dict:
        from .memory import remember

        return remember(self, **kwargs)

    def recall(self, query: str, limit: int = 3, include_superseded: bool = False,
               chat_id: str | None = None, *, view: str = "brief",
               max_bytes: int = 4096, offset: int = 0) -> dict:
        from .memory import recall

        return recall(self, query, limit, include_superseded, chat_id,
                      view=view, max_bytes=max_bytes, offset=offset)

    def resume(self, limit: int = 20, include_completed: bool = True,
               include_superseded: bool = False, chat_id: str | None = None) -> dict:
        from .memory import resume

        return resume(self, limit, include_completed, include_superseded, chat_id)

    def search(
        self,
        query: str,
        limit: int = 8,
        max_chars: int = 6000,
        session_id: str | None = None,
        mode: str = "hybrid",
        *,
        chat_id: str | None = None,
        segment_id: str | None = None,
    ) -> dict:
        from .retrieval import search

        return search(
            self,
            query,
            limit,
            max_chars,
            session_id,
            mode,
            chat_id=chat_id,
            segment_id=segment_id,
        )

    def packet(
        self, query: str, max_bytes: int = 4096, max_sources: int = 8,
        mode: str = "raw", *, chat_id: str | None = None, segment_id: str | None = None,
    ) -> dict:
        from .packet import packet

        return packet(self, query, max_bytes, max_sources, mode,
                      chat_id=chat_id, segment_id=segment_id)

    def _evidence(self, owner: str, include_quotes: bool = True):
        fields = "entry_id,start,end,quote" if include_quotes else "entry_id,start,end"
        return [
            dict(r)
            for r in self.db.execute(
                f"SELECT {fields} FROM evidence WHERE owner_id=? ORDER BY entry_id,start", (owner,)
            )
        ]

    def expand(
        self, object_id: str, radius: int = 2, max_chars: int = 10000, offset: int = 0
    ) -> dict:
        if type(radius) is not int or not 0 <= radius <= 10:
            raise ValueError("radius must be between 0 and 10")
        if type(max_chars) is not int or not 256 <= max_chars <= 50000:
            raise ValueError("max_chars must be between 256 and 50000")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be nonnegative")
        result = {"id": object_id, "entries": [], "related": [], "evidence": [], "model_calls": 0}
        row = self.db.execute("SELECT * FROM entries WHERE id=?", (object_id,)).fetchone()
        ids = []
        if row:
            ids.append(row["id"])
            neighbors = self.db.execute(
                "SELECT id FROM entries WHERE session_id=? AND line<? ORDER BY line DESC LIMIT ?",
                (row["session_id"], row["line"], radius),
            ).fetchall()
            neighbors += self.db.execute(
                "SELECT id FROM entries WHERE session_id=? AND line>? ORDER BY line LIMIT ?",
                (row["session_id"], row["line"], radius),
            ).fetchall()
            ids.extend(n[0] for n in neighbors)
            if row["call_id"]:
                ids.extend(
                    r[0]
                    for r in self.db.execute(
                        "SELECT id FROM entries WHERE session_id=? AND call_id=? ORDER BY line LIMIT 20",
                        (row["session_id"], row["call_id"]),
                    )
                )
        else:
            rec = self.db.execute(
                "SELECT r.* FROM records r JOIN jobs j ON r.job_id=j.id WHERE r.id=? AND j.policy_hash=?",
                (object_id, self.config["policy_hash"]),
            ).fetchone()
            if not rec:
                raise ValueError("unknown entry or current-policy record id")
            result["record"] = dict(rec)
            result["evidence"] = self._evidence(object_id)
            ids.extend(e["entry_id"] for e in result["evidence"])
            edges = self.db.execute(
                """SELECT e.* FROM edges e JOIN jobs j ON j.id=e.job_id
                WHERE j.policy_hash=? AND (e.source=? OR e.target=?) ORDER BY e.id LIMIT 12""",
                (self.config["policy_hash"], object_id, object_id),
            ).fetchall()
            result["edges"] = []
            for edge in edges:
                other = edge["target"] if edge["source"] == object_id else edge["source"]
                linked_row = self.db.execute(
                    """SELECT r.* FROM records r JOIN jobs j ON j.id=r.job_id
                    WHERE r.id=? AND j.policy_hash=?""",
                    (other, self.config["policy_hash"]),
                ).fetchone()
                if not linked_row:
                    continue
                linked = dict(linked_row)
                linked["evidence"] = self._evidence(other)
                result["related"].append(linked)
                edge_item = dict(edge)
                edge_item["evidence"] = self._evidence(edge["id"])
                result["edges"].append(edge_item)
                ids.extend(e["entry_id"] for e in linked["evidence"] + edge_item["evidence"])
        # Reserve at least half the text budget for original evidence. Bound graph
        # prose as well as raw messages; structural IDs/positions are not text budget.
        graph_budget = max_chars // 2
        graph_used = 0
        result["truncated_fields"] = []

        def clip_graph(value, path=""):
            nonlocal graph_used
            if isinstance(value, list):
                for i, child in enumerate(value):
                    clip_graph(child, f"{path}[{i}]")
            elif isinstance(value, dict):
                for key, child in value.items():
                    if key in {"content", "scope", "aliases", "quote", "summary"} and isinstance(
                        child, str
                    ):
                        cap = max(0, graph_budget - graph_used)
                        value[key] = child[:cap]
                        graph_used += len(value[key])
                        if len(child) > cap:
                            result["truncated_fields"].append(f"{path}.{key}")
                    elif isinstance(child, (dict, list)):
                        clip_graph(child, f"{path}.{key}")

        for field in ("record", "evidence", "related", "edges"):
            if field in result:
                clip_graph(result[field], field)
        remaining = max_chars - graph_used
        result["omitted_entry_ids"] = []
        for eid in dict.fromkeys(ids):
            if remaining <= 0:
                result["omitted_entry_ids"].append(eid)
                continue
            from .source_text import hydrate
            item = hydrate(self, self.db.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone())
            item["chat_id"] = self.chat_id(item["session_id"])
            from .source_locator import describe
            item["source_location"] = describe(self, eid, verify=eid == object_id)
            original = item["text"]
            begin = offset if eid == object_id else 0
            item["text"] = original[begin : begin + remaining]
            end = begin + len(item["text"])
            item.update(
                offset=begin,
                total_chars=len(original),
                truncated=begin > 0 or end < len(original),
                next_offset=end if end < len(original) else None,
            )
            remaining -= len(item["text"])
            result["entries"].append(item)
        result["coverage"] = self.status(detailed=False)
        result["budget"] = {
            "max_text_chars": max_chars,
            "returned_text_chars": graph_used + sum(len(e["text"]) for e in result["entries"]),
        }
        return result
