"""Independent, versioned source store. No legacy imports or model-derived facts."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from .sqlite_paths import sqlite_uri
from .current_schema import DDL
from .admission import require_human, transcript_meta
from .project_scope import SourceOwnerMismatch, source_belongs_to_project
from .projection import VERSION, assistant_body_hash, compact_body_hash, project, search_tokens

APPLICATION_ID = 0x5345574F
SCHEMA_VERSION = 9
MAX_LINE_BYTES = 16 * 1024 * 1024


def digest(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class UnsupportedStorage(ValueError):
    pass


class Store:
    def __init__(self, config: dict, *, create=False, read_only=False):
        from .locking import lifecycle
        self._lease = None
        if not read_only:
            self._lease = lifecycle(config)
            self._lease.__enter__()
        try:
            self._open(config, create=create, read_only=read_only)
        except BaseException:
            if self._lease is not None:
                self._lease.__exit__(None, None, None)
                self._lease = None
            raise

    def _open(self, config: dict, *, create=False, read_only=False):
        self.config = config
        path = Path(config["_database"])
        if path.is_symlink():
            raise ValueError("Seywo database must not be a symlink")
        if create and not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        mode = "ro" if read_only else "rw"
        self.db = sqlite3.connect(sqlite_uri(path, mode=mode), uri=True, timeout=10)
        self.db.row_factory = sqlite3.Row
        from .storage_schema import register
        register(self.db)
        self.db.execute("PRAGMA foreign_keys=ON")
        if not read_only:
            self.db.execute("PRAGMA secure_delete=ON")
        try:
            app_id = self.db.execute("PRAGMA application_id").fetchone()[0]
            tables = self.db.execute("SELECT name FROM sqlite_master").fetchall()
            if create and not tables and app_id == 0:
                self.db.executescript("BEGIN IMMEDIATE;" + DDL)
                self.db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                self.db.execute("INSERT INTO projects VALUES(?,?)",
                                (config["project_id"], config["project_root"]))
                self.db.commit()
            if self.db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                raise ValueError("not a Seywo database")
            if self.db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise UnsupportedStorage("unsupported Seywo storage schema")
            owner = self.db.execute("SELECT id,root FROM projects").fetchall()
            if len(owner) != 1 or tuple(owner[0]) != (config["project_id"], config["project_root"]):
                raise ValueError("database belongs to a different project")
        except BaseException:
            self.db.rollback()
            self.db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        try:
            self.db.close()
        finally:
            if self._lease is not None:
                self._lease.__exit__(None, None, None)
                self._lease = None
        if getattr(self, '_reclaim_after_close', False):
            from .storage_lifecycle import safe_collect
            safe_collect(self.config)

    @contextmanager
    def _transaction(self):
        if self.db.in_transaction:
            yield
        else:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                yield

    def has_admitted_source(self, chat_id):
        """An identity row alone is not evidence: imports stage it before validation."""
        return self.db.execute(
            'SELECT 1 FROM chats c JOIN source_segments s ON s.chat_id=c.id '
            'WHERE c.id=? AND c.project_id=? AND s.head_snapshot IS NOT NULL LIMIT 1',
            (chat_id, self.config['project_id']),
        ).fetchone() is not None

    def require_human_chat(self, chat_id):
        """Block known internal sources imported before admission was enforced.

        Read bounded provenance only; preserve all original rows and versions.
        Missing old source metadata retains legacy compatibility.
        """
        for row in self.db.execute("SELECT path FROM source_segments WHERE chat_id=?", (chat_id,)):
            require_human(transcript_meta(row["path"]))

    def _require_source_owner(self, meta):
        cwd = meta.get('cwd')
        if not isinstance(cwd, str) or not cwd or not source_belongs_to_project(
            cwd, Path(self.config['_path']).parent.parent,
            has_history=lambda: self.has_admitted_source(meta['id']),
        ):
            raise SourceOwnerMismatch('source cwd differs from the bound project root')

    def capture_source(self, path, chat_id, *, dialogue):
        from .archive import content_capture

        def admit(record):
            if not (isinstance(record, dict) and record.get('type') == 'session_meta'
                    and isinstance(meta := record.get('payload'), dict) and isinstance(meta.get('id'), str)):
                raise ValueError('first record must identify the explicitly selected chat')
            self._require_source_owner(meta)
            require_human(meta)

        folder = Path(self.config['_path']).parent / 'inbox' / 'live'
        return content_capture(path, folder, chat_id, dialogue=dialogue, admit=admit)

    def import_source(self, path: Path, chat_id: str, ordinal: int, *, stream=None) -> dict:
        with self._transaction():
            return self._import_source(path, chat_id, ordinal, stream=stream)

    def _import_source(self, path: Path, chat_id: str, ordinal: int, *, stream=None, identity=None, preserve_prefix=0) -> dict:
        """Import exact source identity, sharing unchanged revisions before indexing.

        Explicit imports retain physical coverage. Compact ingress may discard an
        already-represented leading prefix across segments. Automatic captures
        retain dialogue and original coordinates, not exact native bytes.
        """
        from .archive import is_dialogue, open_source, source_digest
        from .dialogue_capture import MIRROR, coordinates
        from .storage_schema import membership, payload
        if not chat_id or type(ordinal) is not int or ordinal < 0:
            raise ValueError("chat and non-negative segment order are required")
        path = path.expanduser().resolve()
        if path.suffix != ".jsonl":
            raise ValueError("only explicit Codex .jsonl source records are supported")
        expected_hash, expected_size = identity or source_digest(path)
        dialogue = is_dialogue(path)
        with open_source(path) as handle:
            if self.db.execute("SELECT 1 FROM tombstones WHERE chat_id=?", (chat_id,)).fetchone():
                raise ValueError("chat was deleted; implicit reimport is prohibited")
            before = os.fstat(handle.fileno())
            self.db.execute("INSERT OR IGNORE INTO chats VALUES(?,?)", (chat_id, self.config["project_id"]))
            prior = self.db.execute("SELECT * FROM source_segments WHERE path=?", (str(path),)).fetchone()
            if stream is not None:
                mapped = self.db.execute("SELECT s.* FROM source_segments s JOIN compact_streams m ON m.segment_id=s.id WHERE m.chat_id=? AND m.transcript=?", (chat_id, stream)).fetchone()
                if mapped and mapped["ordinal"] == ordinal:
                    prior = mapped
            if prior and (prior["chat_id"] != chat_id or prior["ordinal"] != ordinal):
                raise ValueError("source binding/order cannot change implicitly")
            if prior and prior['head_snapshot'] and self.db.execute(
                'SELECT 1 FROM source_snapshots WHERE id=? AND (projection_version<>? OR dialogue<>?)',
                (prior['head_snapshot'], VERSION, dialogue)).fetchone():
                raise ValueError('source_capture_format_unsupported')
            segment_id = prior["id"] if prior else uuid4().hex
            if not prior:
                self.db.execute("INSERT INTO source_segments VALUES(?,?,?,?,NULL)", (segment_id, chat_id, str(path), ordinal))
            duplicate = self.db.execute(
                "SELECT id,records FROM source_snapshots WHERE segment_id=? AND sha256=? AND projection_version=?",
                (segment_id, expected_hash, VERSION)).fetchone()
            if duplicate:
                return {"chat_id": chat_id, "segment_id": segment_id, "snapshot_id": duplicate[0],
                        "records": duplicate[1], "source_sha256": expected_hash, "reused_snapshot": True,
                        "semantic_done": False, "inserted_fragments": 0}
            previous_rows = {}
            if prior and prior["head_snapshot"]:
                previous_rows = {r["record_no"]: r for r in self.db.execute(
                    "SELECT f.row_id,f.payload_id,f.raw_sha256,f.byte_start,f.byte_end,f.turn_id,f.root_turn_id,f.turn_seq,e.record_no "
                    "FROM snapshot_fragments f JOIN source_events e ON e.id=f.event_id WHERE f.snapshot_id=?",
                    (prior["head_snapshot"],))}
            snapshot_id = uuid4().hex
            self.db.execute("INSERT INTO source_snapshots VALUES(?,?,?,0,0,?,?)", (snapshot_id, segment_id, snapshot_id, VERSION, dialogue))
            previous = self.db.execute(
                "SELECT turn_id,root_turn_id FROM current_sources WHERE chat_id=? AND ordinal<? AND turn_id IS NOT NULL "
                "ORDER BY ordinal DESC,record_no DESC LIMIT 1", (chat_id, ordinal)).fetchone()
            current_turn, current_root = (previous[0], previous[1]) if previous else (None, None)
            turns = {r[0]: r[1] for r in self.db.execute(
                "SELECT turn_id,min(turn_seq) FROM current_sources WHERE chat_id=? AND turn_id IS NOT NULL GROUP BY turn_id", (chat_id,))}
            for r in previous_rows.values():
                if r["turn_id"]:
                    turns[r["turn_id"]] = r["turn_seq"]
            turn_seq = max((n or 0 for n in turns.values()), default=0)
            prefix_hash, offset, count, metadata_seen = hashlib.sha256(), 0, 0, False
            origin_end = 0
            row_ids, inserted, prior_assistant = [], 0, None
            new_payloads = set()
            while True:
                raw = handle.readline(MAX_LINE_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_LINE_BYTES:
                    raise ValueError("source_record_too_large: import rolled back without truncation")
                count += 1
                start, offset = offset, offset + len(raw)
                prefix_hash.update(raw)
                raw_hash = digest(raw)
                try:
                    record = json.loads(raw.decode("utf-8"))
                    if not isinstance(record, dict):
                        raise ValueError("non-object record")
                except (ValueError, UnicodeDecodeError):
                    record = {"type": "unparsed"}
                complete, end = raw.endswith(b"\n"), offset
                if dialogue:
                    start, end, raw_hash, complete = coordinates(record, origin_end)
                    origin_end = end
                if count == 1:
                    meta = record.get("payload", {})
                    if record.get("type") != "session_meta" or not isinstance(meta, dict) or meta.get("id") != chat_id:
                        raise ValueError("first record must identify the explicitly selected chat")
                    require_human(meta)
                    self._require_source_owner(meta)
                    metadata_seen = True
                elif record.get("type") == "session_meta":
                    raise ValueError("multiple session identities in one physical source")
                projection = project(record)
                if dialogue:
                    projection["source_byte_space"] = "original_transcript"
                    if projection["kind"] in {"function_call", "custom_tool_call", "function_call_output",
                                             "custom_tool_call_output", "tool_result", "tool_response"} or projection["role"] == "tool":
                        projection.update(state="excluded_mechanical", reason="tool_payload_not_captured")
                if (record.get("type") == "turn_context" and projection["turn_id"]
                        or projection.get("turn_event") == "task_started" and projection.get("event_turn_id")):
                    current_turn = projection.get("event_turn_id") or projection["turn_id"]
                    current_root = projection["root_turn_id"]
                    if current_turn not in turns:
                        turn_seq += 1
                        turns[current_turn] = turn_seq
                projection.update(turn_id=current_turn, root_turn_id=current_root,
                                  turn_seq=turns.get(current_turn) if current_turn else None)
                compact_hash = record.get(MIRROR) if dialogue else None
                mirror = (compact_hash if record.get("type") == "compacted" and dialogue else compact_body_hash(record))
                compacted = record.get("type") == "compacted" or projection["reason"] == "compact_reference_not_original"
                if prior_assistant and mirror == prior_assistant[1]:
                    position, _ = prior_assistant
                    fragment = row_ids[position]
                    saved = self.db.execute("SELECT * FROM stored_fragments WHERE row_id=?", (fragment,)).fetchone()
                    saved_projection = json.loads(saved["projection_json"])
                    saved_projection.update(state="excluded_mechanical", reason="compact_summary_mirror", mirror_record_no=count)
                    changed = payload(self.db, saved["safe_text"], saved_projection)
                    if saved["snapshot_id"] != snapshot_id:
                        # An inherited fragment is immutable in older snapshots.
                        names = [r[1] for r in self.db.execute("PRAGMA table_info(source_fragments)") if r[1] != "row_id"]
                        values = [snapshot_id if n == "snapshot_id" else changed if n == "payload_id" else "" if n == "safe_text"
                                  else "{}" if n == "projection_json" else saved[n] for n in names]
                        from .storage_v8 import insert_fragment
                        fragment = insert_fragment(self.db, dict(zip(names, values)))
                        row_ids[position] = fragment
                        inserted += 1
                        self.db.execute("INSERT INTO coverage_ranges VALUES(?,?,?)", (fragment, "excluded_mechanical", "compact_summary_mirror"))
                    else:
                        self.db.execute("UPDATE source_fragments SET payload_id=? WHERE row_id=?", (changed, fragment))
                        self.db.execute("UPDATE coverage_ranges SET state='excluded_mechanical',reason='compact_summary_mirror' WHERE fragment_id=?", (fragment,))
                if not complete:
                    projection.update(state="pending", reason="unterminated_record", text="")
                safe = projection["text"]
                old = previous_rows.get(count)
                unchanged = (old and old["raw_sha256"] == raw_hash and old["byte_start"] == start and old["byte_end"] == end
                             and old["turn_id"] == current_turn and old["root_turn_id"] == current_root
                             and old["turn_seq"] == projection["turn_seq"])
                if unchanged and count <= preserve_prefix:
                    pid = old["payload_id"]
                else:
                    pid = payload(self.db, safe, {k: v for k, v in projection.items() if k != "text"})
                if unchanged and old["payload_id"] == pid:
                    fragment = old["row_id"]
                else:
                    event_id = digest(f"{segment_id}:{count}")
                    self.db.execute("INSERT OR IGNORE INTO source_events VALUES(?,?,?)", (event_id, segment_id, count))
                    from .storage_v8 import insert_fragment
                    fragment = insert_fragment(self.db, dict(
                        event_id=event_id, snapshot_id=snapshot_id, byte_start=start, byte_end=end,
                        raw_sha256=raw_hash, kind=projection["kind"], role=projection["role"], channel=projection["channel"],
                        call_id=projection["call_id"], tool=projection["tool"], occurred_at=projection["timestamp"],
                        safe_text="", safe_sha256=digest(safe), turn_id=current_turn, root_turn_id=current_root,
                        turn_seq=projection["turn_seq"], projection_json="{}", payload_id=pid))
                    self.db.execute("INSERT INTO coverage_ranges VALUES(?,?,?)", (fragment, projection["state"], projection["reason"]))
                    inserted += 1
                row_ids.append(fragment)
                if safe and projection["state"] != "excluded_mechanical":
                    new_payloads.add(pid)
                body_hash = (compact_hash or assistant_body_hash(record)) if not compacted else None
                if body_hash is not None and projection["state"] == "projected":
                    prior_assistant = (len(row_ids) - 1, body_hash)
                elif compacted or projection["role"] in {"user", "assistant"}:
                    prior_assistant = None
            after = os.fstat(handle.fileno())
            if (not metadata_seen or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns)
                    or prefix_hash.hexdigest() != expected_hash or offset != expected_size):
                raise ValueError("empty or changing source; retry from a stable source")
            membership(self.db, snapshot_id, row_ids)
            self.db.execute("UPDATE source_snapshots SET sha256=?,byte_count=?,records=? WHERE id=?", (expected_hash, offset, count, snapshot_id))
            self.db.execute("UPDATE source_segments SET head_snapshot=? WHERE id=?", (snapshot_id, segment_id))
            self.db.execute("INSERT INTO source_archives VALUES(?,?)", (snapshot_id, str(path)))
            if stream is not None:
                self.db.execute("INSERT INTO compact_streams VALUES(?,?,?) ON CONFLICT(chat_id,transcript) DO UPDATE SET segment_id=excluded.segment_id", (chat_id, stream, segment_id))
            for pid in new_payloads:
                if self.db.execute("SELECT 1 FROM source_fts WHERE rowid=?", (pid,)).fetchone():
                    continue
                text = self.db.execute("SELECT seywo_source_text(data) FROM source_payloads WHERE id=?", (pid,)).fetchone()[0]
                self.db.execute("INSERT OR IGNORE INTO search_documents VALUES(?)", (pid,))
                self.db.execute("INSERT INTO source_fts(rowid,tokens) VALUES(?,?)", (pid, search_tokens(text)))
        return {"chat_id": chat_id, "segment_id": segment_id, "snapshot_id": snapshot_id,
                "records": count, "source_sha256": expected_hash, "reused_snapshot": False,
                "semantic_done": False, "inserted_fragments": inserted}

    def import_compact(self, path, chat_id, transcript, *, dialogue=False):
        """Replay is inert; only verified append prefixes reuse an ordinal."""
        from .locking import file_lock
        with file_lock(Path(self.config["_path"]).parent / "inbox" / ".lock"):
            path, manifest = self.capture_source(path, chat_id, dialogue=dialogue)
            return self._import_compact_capture(path, manifest, chat_id, transcript)

    def _import_compact_capture(self, path, manifest, chat_id, transcript):
        from .archive import is_dialogue, open_source
        identity = manifest["sha256"], manifest["bytes"]
        with self._transaction():
            if self.db.execute("SELECT 1 FROM tombstones WHERE chat_id=?", (chat_id,)).fetchone():
                raise ValueError("chat was deleted; implicit reimport is prohibited")
            last = self.db.execute("SELECT coalesce(max(ordinal),-1) FROM source_segments WHERE chat_id=?", (chat_id,)).fetchone()[0]
            mapped = self.db.execute(
                "SELECT s.ordinal,ss.*,s.id segment FROM source_segments s JOIN compact_streams m ON s.id=m.segment_id "
                "JOIN source_snapshots ss ON ss.id=s.head_snapshot WHERE m.chat_id=? AND m.transcript=?",
                (chat_id, transcript)).fetchone()
            latest = mapped is not None and mapped['ordinal'] == last
            dialogue = is_dialogue(path)
            if mapped and (mapped['projection_version'] != VERSION or mapped['dialogue'] != dialogue):
                raise ValueError('source_capture_format_unsupported')
            duplicate = self.db.execute(
                "SELECT ss.*,s.id segment FROM source_snapshots ss JOIN source_segments s ON s.id=ss.segment_id "
                "WHERE s.chat_id=? AND ss.sha256=? AND ss.projection_version=? AND ss.dialogue=?",
                (chat_id, identity[0], VERSION, dialogue)).fetchone()
            if duplicate:
                return {"chat_id": chat_id, "segment_id": duplicate["segment"], "snapshot_id": duplicate["id"],
                        "records": duplicate["records"], "source_sha256": identity[0], "reused_snapshot": True,
                        "semantic_done": False, "inserted_fragments": 0}
            ordinal = last if latest and identity[0] == mapped['sha256'] else last + 1
            preserve_prefix = 0
            if latest and identity[1] > mapped["byte_count"]:
                prefix = hashlib.sha256()
                remaining = mapped["byte_count"]
                with open_source(path) as handle:
                    while remaining:
                        part = handle.read(min(remaining, 256 * 1024))
                        if not part:
                            break
                        prefix.update(part)
                        remaining -= len(part)
                if remaining == 0 and prefix.hexdigest() == mapped["sha256"]:
                    ordinal = last
                    if dialogue:
                        preserve_prefix = mapped["records"]
            if latest and ordinal != last and dialogue:
                incomplete = self.db.execute(
                    "SELECT 1 FROM snapshot_fragments f JOIN source_events e ON e.id=f.event_id "
                    "JOIN coverage_ranges c ON c.fragment_id=f.row_id "
                    "WHERE f.snapshot_id=? AND e.record_no=? AND c.reason='unterminated_record'",
                    (mapped["id"], mapped["records"])).fetchone() is not None
                match = self._dialogue_prefix(path, mapped, incomplete)
                if match in {'stale', 'replay'}:
                    return {'chat_id': chat_id, 'segment_id': mapped['segment'], 'snapshot_id': mapped['id'],
                            'records': mapped['records'], 'source_sha256': mapped['sha256'], 'reused_snapshot': True,
                            'semantic_done': False, 'inserted_fragments': 0}
                if incomplete and match == 'append':
                    ordinal = last
            return self._import_source(path, chat_id, ordinal, stream=transcript, identity=identity, preserve_prefix=preserve_prefix)

    def _dialogue_prefix(self, path, mapped, incomplete):
        from .archive import open_source
        from .dialogue_capture import coordinates
        # Compare native coordinates when normalized captures differ in length.
        # An unterminated tail was never eligible for semantic consumption.
        count = mapped["records"] - int(incomplete)
        if not count:
            return None
        prior = self.db.execute(
            "SELECT raw_sha256,byte_start,byte_end FROM snapshot_fragments f "
            "JOIN source_events e ON e.id=f.event_id WHERE f.snapshot_id=? AND e.record_no<=? ORDER BY e.record_no",
            (mapped["id"], count))
        end = matched = 0
        with open_source(path) as handle:
            for old in prior:
                raw = handle.readline(MAX_LINE_BYTES + 1)
                if not raw:
                    return 'stale' if matched else None
                start, end, sha, _ = coordinates(json.loads(raw), end)
                if (sha, start, end) != tuple(old):
                    return None
                matched += 1
            if matched != count:
                return None
            if handle.read(1):
                return "append"
            return None if incomplete else "replay"

    def _reindex(self):
        self.db.execute("INSERT INTO source_fts(source_fts) VALUES('delete-all')")
        self.db.execute("DELETE FROM search_documents")
        rows = self.db.execute("SELECT DISTINCT payload_id,safe_text FROM current_sources WHERE safe_text<>'' AND state<>'excluded_mechanical'").fetchall()
        self.db.executemany("INSERT OR IGNORE INTO search_documents VALUES(?)", ((r[0],) for r in rows))
        self.db.executemany("INSERT INTO source_fts(rowid,tokens) VALUES(?,?)",
                            ((r["payload_id"], search_tokens(r["safe_text"])) for r in rows))

    def reindex(self):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._reindex()
        return {"rebuilt": True, "model_calls": 0}

    def list_events(self, chat_id: str, *, limit=50, after_order=-1, after_record=0):
        if not 1 <= limit <= 200:
            raise ValueError("limit must be in 1..200")
        rows = self.db.execute(
            """SELECT event_id,record_no,ordinal,kind,role,state,reason,snapshot_id
            FROM current_sources WHERE chat_id=? AND
            (ordinal>? OR (ordinal=? AND record_no>?)) ORDER BY ordinal,record_no LIMIT ?""",
            (chat_id, after_order, after_order, after_record, limit)).fetchall()
        return {"events": [dict(r) for r in rows], "source_only": True, "semantic_done": False}

    def read(self, event_id: str, *, snapshot_id=None, start=0, end=None):
        # Pin the event and call/result context to one database read snapshot.
        owns_transaction = not self.db.in_transaction
        if owns_transaction:
            self.db.execute("BEGIN")
        try:
            return self._read(event_id, snapshot_id=snapshot_id, start=start, end=end)
        finally:
            if owns_transaction:
                self.db.rollback()

    def _read(self, event_id: str, *, snapshot_id=None, start=0, end=None):
        if snapshot_id:
            row = self.db.execute(
                """SELECT f.*,e.record_no,a.path,s.chat_id,c.state,c.reason FROM snapshot_fragments f
                JOIN source_events e ON e.id=f.event_id JOIN source_segments s ON s.id=e.segment_id
                LEFT JOIN source_archives a ON a.snapshot_id=f.snapshot_id
                JOIN coverage_ranges c ON c.fragment_id=f.row_id WHERE f.event_id=? AND f.snapshot_id=?""",
                (event_id, snapshot_id)).fetchone()
        else:
            row = self.db.execute("SELECT * FROM current_sources WHERE event_id=?", (event_id,)).fetchone()
        if not row:
            raise ValueError("source event/version not found")
        end = len(row["safe_text"]) if end is None else end
        if not 0 <= start <= end <= len(row["safe_text"]):
            raise ValueError("invalid Unicode character range")
        projection = json.loads(row["projection_json"])
        # Never reread the original file, which may be absent, mutated or contain credentials.
        return {"event_id": event_id, "snapshot_id": row["snapshot_id"], "chat_id": row["chat_id"],
                "kind": row["kind"], "role": row["role"], "channel": row["channel"],
                "call_id": row["call_id"], "tool": row["tool"],
                "turn_id": row["turn_id"], "root_turn_id": row["root_turn_id"],
                "turn_seq": row["turn_seq"],
                "pairing": self.pairing(row["chat_id"], row["call_id"], snapshot_id=snapshot_id),
                "source": {"path": row["path"], "record_no": row["record_no"],
                           "byte_start": row["byte_start"], "byte_end": row["byte_end"],
                           "raw_sha256": row["raw_sha256"],
                           **({"byte_space": projection["source_byte_space"]} if "source_byte_space" in projection else {})},
                "text": row["safe_text"][start:end], "char_start": start, "char_end": end,
                "safe_sha256": row["safe_sha256"], "projection": projection,
                "coverage": {"state": row["state"], "reason": row["reason"]},
                "source_only": True, "semantic_done": False}

    def pairing(self, chat_id: str, call_id: str | None, *, snapshot_id=None) -> dict:
        if not call_id:
            return {"state": "unknown", "calls": [], "results": []}
        if snapshot_id:
            # Historical reads can pair only within the requested physical snapshot.
            rows = self.db.execute(
                "SELECT event_id,kind FROM snapshot_fragments WHERE snapshot_id=? AND call_id=? ORDER BY byte_start",
                (snapshot_id, call_id)).fetchall()
        else:
            rows = self.db.execute(
                "SELECT event_id,kind FROM current_sources WHERE chat_id=? AND call_id=? ORDER BY ordinal,record_no",
                (chat_id, call_id)).fetchall()
        calls = [r["event_id"] for r in rows if r["kind"] in {"function_call", "custom_tool_call"}]
        results = [r["event_id"] for r in rows if r["kind"] in {"function_call_output", "custom_tool_call_output"}]
        state = "ambiguous" if len(calls) > 1 or len(results) > 1 else "paired" if calls and results else "missing"
        return {"state": state, "calls": calls, "results": results,
                "scope": "physical_snapshot" if snapshot_id else "current_chat_sources"}

    def search(self, query: str, *, chat_id=None, limit=20):
        if not 1 <= limit <= 100 or not query.strip() or len(query) > 1000:
            raise ValueError("query must contain 1..1000 characters; limit 1..100")
        tokens = search_tokens(query).split()
        if not tokens:
            return {"matches": [], "source_only": True, "semantic_done": False}
        fts_query = " AND ".join('"' + t + '"' for t in dict.fromkeys(tokens))
        rows = self.db.execute(
            """SELECT c.event_id,c.snapshot_id,c.chat_id,c.record_no,c.ordinal,c.kind,c.role,
            c.state,c.reason,substr(c.safe_text,1,320) AS preview FROM source_fts
            JOIN current_sources c ON c.payload_id=source_fts.rowid WHERE source_fts MATCH ?
            AND c.state<>'excluded_mechanical' AND (? IS NULL OR c.chat_id=?) ORDER BY rank,c.ordinal,c.record_no LIMIT ?""",
            (fts_query, chat_id, chat_id, limit)).fetchall()
        return {"matches": [dict(r) for r in rows], "source_only": True, "semantic_done": False}

    def status(self, chat_id=None):
        rows = self.db.execute("SELECT state,count(*) AS n FROM current_sources WHERE (? IS NULL OR chat_id=?) GROUP BY state",
                               (chat_id, chat_id)).fetchall()
        jobs, attempts = {}, 0
        for row in self.db.execute("SELECT state,count(*) AS n,sum(call_count) AS calls FROM understanding_jobs WHERE (? IS NULL OR chat_id=?) GROUP BY state", (chat_id, chat_id)):
            jobs[row["state"]] = row["n"]
            attempts += row["calls"]
        return {"product": "seywo", "schema_version": SCHEMA_VERSION,
                "project_id": self.config["project_id"], "coverage": {r["state"]: r["n"] for r in rows},
                "semantic_done": False, "model_calls": None if attempts else 0,
                "model_attempts": attempts, "understanding_jobs": jobs, "automatic_maintenance": False}

    def delete_chat(self, chat_id: str):
        self._delete_chats([chat_id])
        self._reclaim_after_close = True
        return {"chat_id": chat_id, "deleted": True, "reimport_blocked": True,
                "original_source_deleted": False}

    def chat_deleted(self, chat_id):
        return self.db.execute("SELECT 1 FROM tombstones WHERE chat_id=?", (chat_id,)).fetchone() is not None

    def _delete_chats(self, chat_ids):
        with self._transaction():
            args = [(chat_id,) for chat_id in chat_ids]
            self.db.executemany("INSERT OR IGNORE INTO tombstones VALUES(?)", args)
            self.db.executemany("DELETE FROM chats WHERE id=?", args)
            from .storage_lifecycle import collect_database
            collect_database(self.db)
            self._reindex()
            self.db.execute("INSERT INTO source_fts(source_fts) VALUES('optimize')")
