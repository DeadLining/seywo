"""Schema 4: model policy exclusions and shared budget lineage (no Hook)."""
DDL = """
CREATE TABLE model_exclusions(
 job_id TEXT NOT NULL REFERENCES understanding_jobs(id) ON DELETE CASCADE,
 fragment_id INTEGER NOT NULL REFERENCES source_fragments(row_id) ON DELETE CASCADE,
 char_start INTEGER NOT NULL, char_end INTEGER NOT NULL, byte_count INTEGER NOT NULL,
 reason TEXT NOT NULL, policy TEXT NOT NULL, PRIMARY KEY(job_id,fragment_id));
CREATE TABLE model_budget_owners(
 job_id TEXT PRIMARY KEY REFERENCES understanding_jobs(id) ON DELETE CASCADE,
 owner_id TEXT NOT NULL REFERENCES understanding_jobs(id) ON DELETE CASCADE);
INSERT INTO model_budget_owners SELECT id,id FROM understanding_jobs;
CREATE INDEX model_budget_owner ON model_budget_owners(owner_id);
"""


def migrate(db):
    if db.execute("PRAGMA user_version").fetchone()[0] != 3:
        return
    try:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("PRAGMA user_version").fetchone()[0] == 3:
            for statement in DDL.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute("PRAGMA user_version=4")
        db.commit()
    except BaseException:
        db.rollback()
        raise
