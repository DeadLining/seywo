"""Immutable narrative snapshots and append-only manual amendments, schema 3."""

DDL = """
CREATE TABLE narrative_heads(
 chat_id TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
 version INTEGER NOT NULL, override_revision INTEGER NOT NULL);
CREATE TABLE narrative_versions(
 chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
 version INTEGER NOT NULL, override_revision INTEGER NOT NULL,
 body_json TEXT NOT NULL, source_heads TEXT NOT NULL,
 task_id TEXT UNIQUE, origin TEXT NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY(chat_id,version));
CREATE TABLE narrative_overrides(
 chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL, actor TEXT NOT NULL, patch_json TEXT NOT NULL,
 created_at REAL NOT NULL, PRIMARY KEY(chat_id,revision));
"""


def migrate(db):
    if db.execute("PRAGMA user_version").fetchone()[0] != 2:
        return
    try:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("PRAGMA user_version").fetchone()[0] == 2:
            for statement in DDL.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute("PRAGMA user_version=3")
        db.commit()
    except BaseException:
        db.rollback()
        raise
