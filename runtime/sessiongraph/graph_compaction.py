"""Read compatibility for previously canonicalized graph identities.

Historical redirects/receipts remain valid. There is no candidate scanner, model
assessment, merge writer or undo command; new input uses ordinary identity recall.
"""
import json

from .storage import exists

SCHEMA = '''
CREATE TABLE IF NOT EXISTS sg_graph_redirects(
 alias TEXT PRIMARY KEY REFERENCES sg2_nodes(id) ON DELETE CASCADE,
 canonical TEXT NOT NULL REFERENCES sg2_nodes(id) ON DELETE CASCADE,
 project TEXT NOT NULL, run TEXT NOT NULL, CHECK(alias!=canonical));
CREATE INDEX IF NOT EXISTS sg_graph_canonical ON sg_graph_redirects(canonical,alias);
CREATE TABLE IF NOT EXISTS sg_graph_compactions(
 id TEXT PRIMARY KEY, project TEXT NOT NULL, receipt TEXT NOT NULL,
 applied_revision TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL);
'''


def serial(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def setup(store):
    store.db.executescript(SCHEMA)
    from .storage import revision_triggers
    revision_triggers(store.db)


def available(store):
    return exists(store.db, 'sg_graph_redirects')


def expression(store, node):
    """SQL expression; node is an internal identifier, never user text."""
    return f'coalesce((SELECT canonical FROM sg_graph_redirects WHERE alias={node}),{node})' if available(store) else node


def visible(store, node):
    return f'NOT EXISTS(SELECT 1 FROM sg_graph_redirects WHERE alias={node})' if available(store) else '1'


def canonical(store, node):
    if not available(store):
        return node
    row = store.db.execute('SELECT canonical FROM sg_graph_redirects WHERE alias=? AND project=?',
                           (node, store.config['project_id'])).fetchone()
    return row[0] if row else node


def members(store, node):
    node = canonical(store, node)
    if not available(store):
        return [node]
    return [node, *[r[0] for r in store.db.execute(
        'SELECT alias FROM sg_graph_redirects WHERE canonical=? AND project=? ORDER BY alias',
        (node, store.config['project_id']))]]


def member_sql(store, node):
    node = canonical(store, node)
    return ("SELECT ? UNION SELECT alias FROM sg_graph_redirects WHERE canonical=?", (node, node)) if available(store) else ('SELECT ?', (node,))


def semantic(payload):
    """Full atomic value, conditions and provenance, never just atomic_key's lane."""
    atom = payload.get('assertion')
    if atom:
        atom = json.loads(serial(atom))
        for a in atom:
            if isinstance(a.get('conditions'), list):
                a['conditions'] = sorted(a['conditions'], key=serial)
        body = atom
    else:
        body = payload.get('content')
    return serial([payload.get('type'), body, payload.get('status'),
                   payload.get('provenance', 'direct'), payload.get('context_kind', 'work')])


def revision(store):
    row = store.db.execute("SELECT value FROM metadata WHERE key='graph_revision_v2'").fetchone()
    return str(row[0]) if row else '0'
