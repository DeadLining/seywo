"""Schema 5: native Codex turn identity retained beside the visible projection.

Repeated identity survives compact boundaries and ordered physical segments; it
never contributes model text by itself.
"""

DDL = """
ALTER TABLE source_fragments ADD COLUMN turn_id TEXT;
ALTER TABLE source_fragments ADD COLUMN root_turn_id TEXT;
ALTER TABLE source_fragments ADD COLUMN turn_seq INTEGER;
CREATE INDEX fragments_turn ON source_fragments(turn_seq);
"""


def migrate(db):
    if db.execute("PRAGMA user_version").fetchone()[0] != 4:
        return
    try:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("PRAGMA user_version").fetchone()[0] == 4:
            columns = {row[1] for row in db.execute("PRAGMA table_info(source_fragments)")}
            for name, kind in (("turn_id", "TEXT"), ("root_turn_id", "TEXT"), ("turn_seq", "INTEGER")):
                if name not in columns:
                    db.execute(f"ALTER TABLE source_fragments ADD COLUMN {name} {kind}")
            db.execute("CREATE INDEX IF NOT EXISTS fragments_turn ON source_fragments(turn_seq)")
            db.execute("PRAGMA user_version=5")
        db.commit()
    except BaseException:
        db.rollback()
        raise
