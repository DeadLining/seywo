"""Version 2 storage migration; all extraction artifacts remain unconfirmed candidates."""

DDL = """
CREATE TABLE understanding_jobs(
 id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
 target_hash TEXT NOT NULL UNIQUE, source_heads TEXT NOT NULL, options_json TEXT NOT NULL,
 limits_json TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL,
 input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
 call_count INTEGER NOT NULL DEFAULT 0, wall_seconds REAL NOT NULL DEFAULT 0,
 final_task TEXT, error_code TEXT);
CREATE TABLE understanding_tasks(
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES understanding_jobs(id) ON DELETE CASCADE,
 ordinal INTEGER NOT NULL, phase TEXT NOT NULL, state TEXT NOT NULL,
 request_json TEXT NOT NULL, request_hash TEXT NOT NULL, result_json TEXT,
 parents_json TEXT NOT NULL, depth INTEGER NOT NULL DEFAULT 0,
 UNIQUE(job_id,ordinal));
CREATE TABLE understanding_attempts(
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES understanding_tasks(id) ON DELETE CASCADE,
 job_id TEXT NOT NULL REFERENCES understanding_jobs(id) ON DELETE CASCADE,
 lease_until REAL NOT NULL, state TEXT NOT NULL, backend TEXT NOT NULL,
 input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
 wall_seconds REAL NOT NULL, usage_kind TEXT NOT NULL, response_hash TEXT,
 error_code TEXT, started_at REAL NOT NULL);
CREATE TABLE semantic_ranges(
 task_id TEXT NOT NULL REFERENCES understanding_tasks(id) ON DELETE CASCADE,
 fragment_id INTEGER NOT NULL REFERENCES source_fragments(row_id) ON DELETE CASCADE,
 char_start INTEGER NOT NULL, char_end INTEGER NOT NULL, state TEXT NOT NULL,
 PRIMARY KEY(task_id,fragment_id,char_start,char_end));
CREATE INDEX understanding_tasks_job ON understanding_tasks(job_id,ordinal);
CREATE INDEX understanding_attempts_job ON understanding_attempts(job_id,started_at);
"""


def migrate(db):
    if db.execute("PRAGMA user_version").fetchone()[0] != 1:
        return
    try:
        db.execute("BEGIN IMMEDIATE")
        # Another opener may have completed migration while we waited for the lock.
        if db.execute("PRAGMA user_version").fetchone()[0] == 1:
            for statement in DDL.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute("PRAGMA user_version=2")
        db.commit()
    except BaseException:
        db.rollback()
        raise
