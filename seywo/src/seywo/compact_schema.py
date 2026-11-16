"""Minimal Compact processing state; semantic history lives in the narrative.

Keep at most the latest success and latest failure per Chat. Payload arrays are
empty: retry/deduplication needs coordinates, not duplicate model output.
"""

from contextlib import nullcontext

DDL = """
CREATE TABLE IF NOT EXISTS compact_batches(
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    cursor_before TEXT NOT NULL,
    cursor_after TEXT NOT NULL,
    base_version INTEGER NOT NULL,
    published_version INTEGER,
    state TEXT NOT NULL CHECK(state IN ('running','done','failed')),
    turn_count INTEGER NOT NULL,
    segment_count INTEGER NOT NULL DEFAULT 0,
    model_calls INTEGER NOT NULL DEFAULT 0,
    input_chars INTEGER NOT NULL DEFAULT 0,
    output_chars INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER,
    output_tokens INTEGER,
    segments_json TEXT NOT NULL,
    changes_json TEXT NOT NULL,
    error_code TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS compact_turn_commits(
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL,
    through_ordinal INTEGER NOT NULL,
    through_record INTEGER NOT NULL,
    PRIMARY KEY(chat_id,turn_id)
);
CREATE INDEX IF NOT EXISTS compact_batches_chat ON compact_batches(chat_id, created_at DESC);
"""


def setup(db):
    # executescript() commits an active SQLite transaction. Compact publication
    # uses one transaction for narrative, cursor and receipt, so create the
    # receipt schema statement by statement and preserve the caller transaction.
    with nullcontext() if db.in_transaction else db:
        for statement in DDL.split(";"):
            statement = statement.strip()
            if statement:
                db.execute(statement)

        columns = {row[1] for row in db.execute("PRAGMA table_info(compact_batches)")}
        if "model_runs_json" not in columns:
            db.execute("ALTER TABLE compact_batches ADD COLUMN model_runs_json TEXT NOT NULL DEFAULT '[]'")

        # Stable rowid breaks equal timestamps deterministically during legacy cleanup.
        db.execute("""DELETE FROM compact_batches WHERE rowid IN (
            SELECT rowid FROM (
                SELECT rowid, row_number() OVER (
                    PARTITION BY chat_id,state ORDER BY updated_at DESC,rowid DESC) n
                FROM compact_batches WHERE state IN ('done','failed')) WHERE n>1)""")
        db.execute("UPDATE compact_batches SET segments_json='[]',changes_json='[]' "
                   "WHERE segments_json<>'[]' OR changes_json<>'[]'")
