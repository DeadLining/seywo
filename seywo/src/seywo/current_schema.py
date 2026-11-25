"""Complete on-disk schema for new Seywo indexes."""

DDL = """
CREATE TABLE projects(id TEXT PRIMARY KEY, root TEXT NOT NULL);

CREATE TABLE chats(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id));

CREATE TABLE tombstones(chat_id TEXT PRIMARY KEY);

CREATE TABLE source_segments(
 id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
 path TEXT NOT NULL UNIQUE, ordinal INTEGER NOT NULL, head_snapshot TEXT,
 UNIQUE(chat_id,ordinal));

CREATE TABLE source_snapshots(
 id TEXT PRIMARY KEY, segment_id TEXT NOT NULL REFERENCES source_segments(id) ON DELETE CASCADE,
 sha256 TEXT NOT NULL, byte_count INTEGER NOT NULL, records INTEGER NOT NULL,
 projection_version TEXT NOT NULL, UNIQUE(segment_id,sha256,projection_version));

CREATE TABLE source_events(
 id TEXT PRIMARY KEY, segment_id TEXT NOT NULL REFERENCES source_segments(id) ON DELETE CASCADE,
 record_no INTEGER NOT NULL, UNIQUE(segment_id,record_no));

CREATE TABLE "fragment_records"(
 row_id INTEGER PRIMARY KEY, event_id TEXT NOT NULL REFERENCES source_events(id) ON DELETE CASCADE,
 snapshot_id TEXT NOT NULL REFERENCES source_snapshots(id) ON DELETE CASCADE,
 byte_start INTEGER NOT NULL, byte_end INTEGER NOT NULL, call_id TEXT, turn_seq INTEGER, payload_id INTEGER REFERENCES source_payloads(id), descriptor_id INTEGER REFERENCES fragment_descriptors(id), UNIQUE(event_id,snapshot_id));

CREATE TABLE coverage_ranges(
 fragment_id INTEGER PRIMARY KEY REFERENCES "fragment_records"(row_id) ON DELETE CASCADE,
 state TEXT NOT NULL CHECK(state IN ('projected','excluded_mechanical','pending','unavailable')),
 reason TEXT NOT NULL);

CREATE INDEX fragments_call ON "fragment_records"(call_id);

CREATE INDEX fragments_snapshot ON "fragment_records"(snapshot_id,byte_start);

CREATE INDEX segments_chat ON source_segments(chat_id,ordinal);

CREATE TABLE understanding_jobs(
 id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
 target_hash TEXT NOT NULL UNIQUE, source_heads TEXT NOT NULL, options_json TEXT NOT NULL,
 limits_json TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL,
 input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
 call_count INTEGER NOT NULL DEFAULT 0, wall_seconds REAL NOT NULL DEFAULT 0,
 final_task TEXT, error_code TEXT);

CREATE TABLE "task_records"(
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES understanding_jobs(id) ON DELETE CASCADE,
 ordinal INTEGER NOT NULL, phase TEXT NOT NULL, state TEXT NOT NULL,
 request_json TEXT NOT NULL, request_hash TEXT NOT NULL, result_json TEXT,
 parents_json TEXT NOT NULL, depth INTEGER NOT NULL DEFAULT 0,
 UNIQUE(job_id,ordinal));

CREATE TABLE understanding_attempts(
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES "task_records"(id) ON DELETE CASCADE,
 job_id TEXT NOT NULL REFERENCES understanding_jobs(id) ON DELETE CASCADE,
 lease_until REAL NOT NULL, state TEXT NOT NULL, backend TEXT NOT NULL,
 input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
 wall_seconds REAL NOT NULL, usage_kind TEXT NOT NULL, response_hash TEXT,
 error_code TEXT, started_at REAL NOT NULL);

CREATE TABLE semantic_ranges(
 task_id TEXT NOT NULL REFERENCES "task_records"(id) ON DELETE CASCADE,
 fragment_id INTEGER NOT NULL REFERENCES "fragment_records"(row_id) ON DELETE CASCADE,
 char_start INTEGER NOT NULL, char_end INTEGER NOT NULL, state TEXT NOT NULL,
 PRIMARY KEY(task_id,fragment_id,char_start,char_end));

CREATE INDEX understanding_tasks_job ON "task_records"(job_id,ordinal);

CREATE INDEX understanding_attempts_job ON understanding_attempts(job_id,started_at);

CREATE TABLE narrative_heads(
 chat_id TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
 version INTEGER NOT NULL, override_revision INTEGER NOT NULL);

CREATE TABLE narrative_overrides(
 chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL, actor TEXT NOT NULL, patch_json TEXT NOT NULL,
 created_at REAL NOT NULL, PRIMARY KEY(chat_id,revision));

CREATE TABLE model_exclusions(
 job_id TEXT NOT NULL REFERENCES understanding_jobs(id) ON DELETE CASCADE,
 fragment_id INTEGER NOT NULL REFERENCES "fragment_records"(row_id) ON DELETE CASCADE,
 char_start INTEGER NOT NULL, char_end INTEGER NOT NULL, byte_count INTEGER NOT NULL,
 reason TEXT NOT NULL, policy TEXT NOT NULL, PRIMARY KEY(job_id,fragment_id));

CREATE TABLE model_budget_owners(
 job_id TEXT PRIMARY KEY REFERENCES understanding_jobs(id) ON DELETE CASCADE,
 owner_id TEXT NOT NULL REFERENCES understanding_jobs(id) ON DELETE CASCADE);

CREATE INDEX model_budget_owner ON model_budget_owners(owner_id);

CREATE INDEX fragments_turn ON "fragment_records"(turn_seq);

CREATE TABLE source_payloads(id INTEGER PRIMARY KEY,hash TEXT NOT NULL UNIQUE,data BLOB NOT NULL);

CREATE TABLE compact_streams(chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,transcript TEXT NOT NULL,segment_id TEXT NOT NULL REFERENCES source_segments(id) ON DELETE CASCADE,PRIMARY KEY(chat_id,transcript));

CREATE TABLE source_archives(snapshot_id TEXT PRIMARY KEY REFERENCES source_snapshots(id) ON DELETE CASCADE,path TEXT NOT NULL);

CREATE TABLE snapshot_members(
            snapshot_id TEXT NOT NULL REFERENCES source_snapshots(id) ON DELETE CASCADE,
            first_row INTEGER NOT NULL,last_row INTEGER NOT NULL,
            CHECK(first_row<=last_row),PRIMARY KEY(snapshot_id,first_row));

CREATE INDEX fragments_payload ON "fragment_records"(payload_id);

CREATE TABLE narrative_revisions(
        chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
        version INTEGER NOT NULL, override_revision INTEGER NOT NULL,
        base_version INTEGER, body BLOB NOT NULL, body_hash TEXT NOT NULL,
        depth INTEGER NOT NULL, source_heads TEXT NOT NULL, task_id TEXT UNIQUE,
        origin TEXT NOT NULL, created_at REAL NOT NULL, logical_bytes INTEGER NOT NULL,
        PRIMARY KEY(chat_id,version));

CREATE VIEW narrative_versions AS SELECT chat_id,version,override_revision,
        seywo_document(chat_id,version) AS body_json,source_heads,task_id,origin,created_at
        FROM narrative_revisions;

CREATE TABLE delta_progress(
                chat_id TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
                cursor_json TEXT NOT NULL, version INTEGER NOT NULL, windows INTEGER NOT NULL);

CREATE TABLE fragment_descriptors(id INTEGER PRIMARY KEY,hash TEXT NOT NULL UNIQUE,data BLOB NOT NULL);

CREATE INDEX fragments_descriptor ON fragment_records(descriptor_id);

CREATE TRIGGER fragment_descriptor_required_insert BEFORE INSERT ON fragment_records WHEN NEW.descriptor_id IS NULL BEGIN SELECT RAISE(ABORT,'source descriptor required'); END;

CREATE TRIGGER fragment_descriptor_required_update BEFORE UPDATE ON fragment_records WHEN NEW.descriptor_id IS NULL BEGIN SELECT RAISE(ABORT,'source descriptor required'); END;

CREATE VIEW source_fragments AS SELECT f.row_id,f.event_id,f.snapshot_id,f.byte_start,f.byte_end,seywo_fragment_field(d.data,0) AS raw_sha256,seywo_fragment_field(d.data,1) AS kind,seywo_fragment_field(d.data,2) AS role,seywo_fragment_field(d.data,3) AS channel,f.call_id,seywo_fragment_field(d.data,4) AS tool,seywo_fragment_field(d.data,5) AS occurred_at,seywo_fragment_field(d.data,6) AS safe_text,seywo_fragment_field(d.data,7) AS safe_sha256,seywo_fragment_field(d.data,8) AS projection_json,seywo_fragment_field(d.data,9) AS turn_id,seywo_fragment_field(d.data,10) AS root_turn_id,f.turn_seq,f.payload_id FROM fragment_records f JOIN fragment_descriptors d ON d.id=f.descriptor_id;

CREATE TRIGGER fragment_update INSTEAD OF UPDATE ON source_fragments BEGIN UPDATE fragment_records SET row_id=NEW.row_id,event_id=NEW.event_id,snapshot_id=NEW.snapshot_id,byte_start=NEW.byte_start,byte_end=NEW.byte_end,call_id=NEW.call_id,turn_seq=NEW.turn_seq,payload_id=NEW.payload_id,descriptor_id=seywo_descriptor_put(NEW.raw_sha256,NEW.kind,NEW.role,NEW.channel,NEW.tool,NEW.occurred_at,NEW.safe_text,NEW.safe_sha256,NEW.projection_json,NEW.turn_id,NEW.root_turn_id) WHERE row_id=OLD.row_id; END;

CREATE TRIGGER fragment_delete INSTEAD OF DELETE ON source_fragments BEGIN DELETE FROM fragment_records WHERE row_id=OLD.row_id; END;

CREATE VIEW stored_fragments AS SELECT f.row_id,f.event_id,f.snapshot_id,f.byte_start,f.byte_end,f.raw_sha256,f.kind,f.role,f.channel,f.call_id,f.tool,f.occurred_at,seywo_source_text(p.data) AS safe_text,f.safe_sha256,seywo_source_projection(p.data,f.turn_id,f.root_turn_id,f.turn_seq) AS projection_json,f.turn_id,f.root_turn_id,f.turn_seq,f.payload_id FROM source_fragments f JOIN source_payloads p ON p.id=f.payload_id;

CREATE VIEW snapshot_fragments AS SELECT f.row_id,f.event_id,m.snapshot_id AS snapshot_id,f.byte_start,f.byte_end,f.raw_sha256,f.kind,f.role,f.channel,f.call_id,f.tool,f.occurred_at,f.safe_text,f.safe_sha256,f.projection_json,f.turn_id,f.root_turn_id,f.turn_seq,f.payload_id FROM snapshot_members m JOIN stored_fragments f ON f.row_id BETWEEN m.first_row AND m.last_row;

CREATE VIEW current_sources AS
                SELECT f.*,e.record_no,s.chat_id,s.ordinal,a.path,s.id AS segment_id,c.state,c.reason
                FROM snapshot_fragments f JOIN source_events e ON e.id=f.event_id
                JOIN source_segments s ON s.id=e.segment_id AND s.head_snapshot=f.snapshot_id
                LEFT JOIN source_archives a ON a.snapshot_id=f.snapshot_id
                JOIN coverage_ranges c ON c.fragment_id=f.row_id;

CREATE TABLE execution_documents(
        hash TEXT PRIMARY KEY, base_hash TEXT REFERENCES execution_documents(hash),
        body BLOB NOT NULL, depth INTEGER NOT NULL, family TEXT NOT NULL,
        logical_bytes INTEGER NOT NULL, encoding TEXT NOT NULL);

CREATE INDEX execution_family ON execution_documents(family);

CREATE INDEX execution_base ON execution_documents(base_hash);

CREATE VIEW understanding_tasks AS SELECT t.id,t.job_id,t.ordinal,t.phase,t.state,seywo_execution_read(t.request_json) AS request_json,t.request_hash,seywo_execution_read(t.result_json) AS result_json,t.parents_json,t.depth FROM task_records t;

CREATE TRIGGER task_insert INSTEAD OF INSERT ON understanding_tasks BEGIN INSERT INTO task_records(id,job_id,ordinal,phase,state,request_json,request_hash,result_json,parents_json,depth) VALUES(NEW.id,NEW.job_id,NEW.ordinal,NEW.phase,NEW.state,seywo_execution_put(NEW.request_json,NEW.job_id,NEW.phase,'request'),NEW.request_hash,seywo_execution_put(NEW.result_json,NEW.job_id,NEW.phase,'result'),NEW.parents_json,NEW.depth); END;

CREATE TRIGGER task_update INSTEAD OF UPDATE ON understanding_tasks BEGIN UPDATE task_records SET id=NEW.id,job_id=NEW.job_id,ordinal=NEW.ordinal,phase=NEW.phase,state=NEW.state,request_json=seywo_execution_put(NEW.request_json,NEW.job_id,NEW.phase,'request'),request_hash=NEW.request_hash,result_json=seywo_execution_put(NEW.result_json,NEW.job_id,NEW.phase,'result'),parents_json=NEW.parents_json,depth=NEW.depth WHERE id=OLD.id; END;

CREATE TRIGGER task_delete INSTEAD OF DELETE ON understanding_tasks BEGIN DELETE FROM task_records WHERE id=OLD.id; END;

CREATE TABLE search_documents(id INTEGER PRIMARY KEY REFERENCES source_payloads(id) ON DELETE CASCADE);

CREATE VIEW search_content AS SELECT p.id AS rowid,
        seywo_search_tokens(seywo_source_text(p.data)) AS tokens FROM source_payloads p JOIN search_documents d ON d.id=p.id;

CREATE VIRTUAL TABLE source_fts USING fts5(tokens,content='search_content',content_rowid='rowid',
        detail=full,columnsize=0,tokenize='unicode61');
"""
