"""Schema 7: a delta publication and its consumed cursor commit together."""


def migrate(db):
    if db.execute("PRAGMA user_version").fetchone()[0] != 6:
        return
    try:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("PRAGMA user_version").fetchone()[0] == 6:
            db.execute("""CREATE TABLE delta_progress(
                chat_id TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
                cursor_json TEXT NOT NULL, version INTEGER NOT NULL, windows INTEGER NOT NULL)""")
            db.execute("DROP VIEW current_sources")
            db.execute("""CREATE VIEW current_sources AS
                SELECT f.*,e.record_no,s.chat_id,s.ordinal,a.path,s.id AS segment_id,c.state,c.reason
                FROM snapshot_fragments f JOIN source_events e ON e.id=f.event_id
                JOIN source_segments s ON s.id=e.segment_id AND s.head_snapshot=f.snapshot_id
                LEFT JOIN source_archives a ON a.snapshot_id=f.snapshot_id
                JOIN coverage_ranges c ON c.fragment_id=f.row_id""")
            db.execute("PRAGMA user_version=7")
        db.commit()
    except BaseException:
        db.rollback()
        raise
