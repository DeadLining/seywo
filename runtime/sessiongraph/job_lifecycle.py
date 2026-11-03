"""Job-owned ledgers share the parent's deletion transaction, including old writers."""

TABLES = ('sg2_recoveries', 'sg_resource_scopes', 'sg_resource_requests', 'sg_resource_grants')


def _cascade_statements(table):
    if table not in TABLES:
        raise ValueError('unknown job ledger')
    scope = '' if table == 'sg2_recoveries' else ' AND project=old.project'
    trigger = f'''CREATE TRIGGER IF NOT EXISTS sg_job_delete_{table} AFTER DELETE ON sg2_jobs BEGIN
 DELETE FROM {table} WHERE job=old.id{scope};
END;'''
    # Resource tables already have project/job prefix indexes.
    index = ('CREATE INDEX IF NOT EXISTS sg2_recoveries_job ON sg2_recoveries(job);',) if table == 'sg2_recoveries' else ()
    return (*index, trigger)


def cascade_schema(table):
    return '\n'.join(_cascade_statements(table))


def setup(db):
    """Install guards for existing optional tables; never scan or prune live data."""
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'sg2_jobs' not in tables:
        return
    for table in TABLES:
        if table in tables:
            # execute(), not executescript(): guard installation must not commit a caller.
            for statement in _cascade_statements(table):
                db.execute(statement)


def prune_orphans(store, *, apply=False):
    """Explicit per-project repair. An absent parent table is not deletion evidence."""
    db = store.db
    project = store.config['project_id']
    owner = db.execute("SELECT value FROM metadata WHERE key='project_id'").fetchone()
    if not owner or owner[0] != project:
        raise ValueError('database project_id does not match configuration')
    if apply:
        store._ensure_writable()
    with db:
        db.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {}
        if 'sg2_jobs' in tables:
            for table in TABLES:
                if table not in tables:
                    continue
                scope, args = ('', ()) if table == 'sg2_recoveries' else ('project=? AND ', (project,))
                where = scope + f'NOT EXISTS(SELECT 1 FROM sg2_jobs j WHERE j.id={table}.job)'
                counts[table] = db.execute(f'SELECT count(*) FROM {table} WHERE {where}', args).fetchone()[0]
                if apply and counts[table]:
                    db.execute(f'DELETE FROM {table} WHERE {where}', args)
        return dict(applied=apply, model_calls=0, orphan_rows=counts)
