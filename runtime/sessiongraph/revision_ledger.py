"""Opt-in immutable assertion snapshots and explicitly reviewed shadow effects.

Existing graph evidence owns statement payloads. This ledger stores references,
source fingerprints and review deltas, never infers authority from recency.
"""
from .source_text import hydrate
import json
import math
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps

from .core import digest
from .failures import Failure
from .storage import exists

VERSION = 'assertion-shadow-v1'
KINDS = {'Claim', 'Decision'}
EFFECTS = {'supports', 'disputes', 'corrects', 'replaces', 'withdraws'}
READ = ContextVar('revision_read_budget', default=None)
SCHEMA = '''
CREATE TABLE IF NOT EXISTS sg_revision_items(
 id TEXT PRIMARY KEY,project TEXT NOT NULL,node TEXT NOT NULL REFERENCES sg2_nodes(id),
 kind TEXT NOT NULL,payload_evidence TEXT NOT NULL REFERENCES sg2_evidence(id),created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS sg_revision_node ON sg_revision_items(project,node,id);
CREATE TABLE IF NOT EXISTS sg_revision_sources(
 revision TEXT NOT NULL REFERENCES sg_revision_items(id),evidence TEXT NOT NULL REFERENCES sg2_evidence(id),
 text_hash TEXT NOT NULL,metadata_hash TEXT NOT NULL,role TEXT NOT NULL,kind TEXT NOT NULL,
 source_time TEXT NOT NULL,PRIMARY KEY(revision,evidence)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS sg_revision_evidence ON sg_revision_sources(evidence);
CREATE TABLE IF NOT EXISTS sg_revision_reviews(
 id TEXT PRIMARY KEY,project TEXT NOT NULL,source TEXT NOT NULL REFERENCES sg_revision_items(id),
 target TEXT NOT NULL REFERENCES sg_revision_items(id),effect TEXT NOT NULL,facet TEXT NOT NULL,
 conditions_json TEXT NOT NULL,evidence TEXT NOT NULL REFERENCES sg2_evidence(id),
 authority TEXT NOT NULL,effective_from TEXT NOT NULL,created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS sg_revision_review_node ON sg_revision_reviews(project,target,id);
CREATE INDEX IF NOT EXISTS sg_revision_review_source ON sg_revision_reviews(project,source,target);
CREATE TABLE IF NOT EXISTS sg_revision_review_controls(
 id TEXT PRIMARY KEY,project TEXT NOT NULL,review TEXT NOT NULL REFERENCES sg_revision_reviews(id),
 previous TEXT NOT NULL,action TEXT NOT NULL,source TEXT NOT NULL REFERENCES sg_revision_items(id),
 evidence TEXT NOT NULL REFERENCES sg2_evidence(id),authority TEXT NOT NULL,effective_from TEXT NOT NULL,
 replacement_review TEXT REFERENCES sg_revision_reviews(id),created TEXT NOT NULL,
 UNIQUE(project,review,previous));
CREATE INDEX IF NOT EXISTS sg_revision_control_review ON sg_revision_review_controls(project,review,id);
'''


def fail(code):
    raise Failure('revision_' + code, stage='revision')


def serial(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def enabled(config):
    graph = config.get('graph_v2', {})
    value = graph.get('revision_policy', {})
    if (not isinstance(value, dict) or set(value) - {'enabled', 'mode'}
            or not isinstance(value.get('mode', 'shadow'), str)
            or value.get('mode', 'shadow') not in {'shadow', 'effective'}
            or type(value.get('enabled', False)) is not bool):
        fail('policy_invalid')
    if value.get('enabled', False) and graph.get('enabled') is not True:
        fail('requires_graph')
    return value.get('enabled', False)


def effective(config):
    return enabled(config) and config['graph_v2']['revision_policy'].get('mode') == 'effective'


def setup(store):
    store._ensure_writable()
    store.db.executescript(SCHEMA)
    # The referenced payload, not another copied statement, is immutable too.
    for table in ('sg_revision_items', 'sg_revision_sources', 'sg_revision_reviews', 'sg_revision_review_controls'):
        for action in ('UPDATE', 'DELETE'):
            store.db.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()}
                BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'revision_immutable'); END''')
        predicate = ('revision=new.revision AND evidence=new.evidence' if table=='sg_revision_sources'
                     else 'id=new.id OR (project=new.project AND review=new.review AND previous=new.previous)'
                     if table=='sg_revision_review_controls' else 'id=new.id')
        # REPLACE skips DELETE triggers unless recursive_triggers is enabled.
        store.db.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_reinsert
            BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table} WHERE {predicate})
            BEGIN SELECT RAISE(IGNORE); END''')
    for action in ('UPDATE', 'DELETE'):
        store.db.execute(f'''CREATE TRIGGER IF NOT EXISTS sg_revision_payload_{action.lower()}
            BEFORE {action} ON sg2_evidence WHEN EXISTS(
              SELECT 1 FROM sg_revision_sources WHERE evidence=old.id)
            BEGIN SELECT RAISE(ABORT,'revision_payload_immutable'); END''')
    store.db.execute('''CREATE TRIGGER IF NOT EXISTS sg_revision_payload_reinsert
        BEFORE INSERT ON sg2_evidence WHEN EXISTS(
          SELECT 1 FROM sg_revision_sources WHERE evidence=new.id)
        BEGIN SELECT RAISE(IGNORE); END''')
    from .source_retention import setup as retention_setup
    retention_setup(store)
    if effective(store.config):
        store.db.execute('''CREATE TABLE IF NOT EXISTS sg_revision_projection(
            node TEXT PRIMARY KEY REFERENCES sg2_nodes(id),project TEXT NOT NULL,state TEXT NOT NULL,
            revision TEXT,alternatives_json TEXT NOT NULL,next_change TEXT)''')
        # Invalidating a small derived row is cheaper than revalidating every
        # historical source at recall time. Only writers rebuild the projection.
        for action in ('UPDATE', 'DELETE'):
            fields = ' OF text,role,kind,timestamp,call_id,session_id,line' if action == 'UPDATE' else ''
            from .graph_compaction import expression
            source_node=expression(store,'x.node')
            trigger='sg_revision_projection_'+action.lower()
            prior=store.db.execute('SELECT sql FROM sqlite_master WHERE name=?',(trigger,)).fetchone()
            if prior and 'sg_graph_redirects' not in prior[0]:
                store.db.execute('DROP TRIGGER '+trigger)
            store.db.execute(f'''CREATE TRIGGER IF NOT EXISTS sg_revision_projection_{action.lower()}
                AFTER {action}{fields} ON entries BEGIN
                UPDATE sg_revision_projection SET state='needs_review' WHERE node IN (
                    SELECT {source_node} FROM sg2_evidence x WHERE x.entry_id=old.id); END''')
    from .storage import revision_triggers
    revision_triggers(store.db)
    store.db.commit()


def stamp():
    return datetime.now(timezone.utc).isoformat()


def signature(node):
    from .graph_contract import atomic_key
    if atomic_key(node) is not None:
        return ['atomic-v1', atomic_key(node), node['assertion'][0]['value'], node['status']]
    # Execution status is retained, not confused with assertion validity.
    return [node['type'], node['title'], node['content'], node['status']]


def metadata(row):
    return digest(serial([row[k] for k in ('session_id', 'line', 'kind', 'role', 'timestamp', 'call_id')]))


def bounded_read(function):
    @wraps(function)
    def wrapper(store, *args, **kwargs):
        if READ.get() is not None and READ.get()['store'] is store:
            return function(store, *args, **kwargs)
        token = READ.set({'store':store, 'remaining':16*1024*1024, 'entries':{}, 'controls':{}, 'control_rows':4096})
        own = not store.db.in_transaction
        try:
            if own and kwargs.get('apply'):
                fail('transaction_required')
            if own:
                store.db.execute('BEGIN')
            return function(store, *args, **kwargs)
        finally:
            if own:
                store.db.rollback()  # End our read snapshot; never commit caller writes.
            READ.reset(token)
    return wrapper


def source(store, evidence_id):
    size = store.db.execute('SELECT sg_source_bytes(e.id,e.text) FROM sg2_evidence v JOIN entries e ON e.id=v.entry_id WHERE v.id=?', (evidence_id,)).fetchone()
    if not size or size[0] > 8*1024*1024:
        fail('source_read_limit' if size else 'source_changed')
    meter = READ.get()
    if meter is not None:
        if evidence_id in meter['entries']:
            return meter['entries'][evidence_id]
        if size[0] > meter['remaining']:
            fail('read_byte_budget')
        meter['remaining'] -= size[0]
    row = store.db.execute('''SELECT v.id evidence_id,v.node,v.entry_id,v.quote,v.assertion_json,
        e.*,n.project,n.type FROM sg2_evidence v JOIN entries e ON e.id=v.entry_id
        JOIN sg2_nodes n ON n.id=v.node WHERE v.id=? AND n.project=?''',
        (evidence_id, store.config['project_id'])).fetchone()
    try:
        row = hydrate(store, row)
    except (ValueError, OSError):
        fail('source_changed')
    if not row or not row['quote'] or row['quote'] not in row['text']:
        fail('source_changed')
    if row['type'] != json.loads(row['assertion_json'])['type']:
        fail('source_changed')
    if meter is not None:
        meter['entries'][evidence_id] = row
    return row


def capture(store, node_id, proposal, evidence_ids):
    """Caller owns publication transaction. Only explicit Claim/Decision opt-in."""
    if not enabled(store.config) or proposal['type'] not in KINDS:
        return None
    if not store.db.in_transaction:
        fail('transaction_required')
    if not evidence_ids or len(evidence_ids) > 8:
        fail('evidence_limit')
    identity = 'ar_' + digest(serial([VERSION, store.config['project_id'], node_id, signature(proposal)]))[:40]
    rows = [source(store, eid) for eid in sorted(set(evidence_ids))]
    for row in rows:
        payload = json.loads(row['assertion_json'])
        if (row['node'] != node_id or row['type'] != proposal['type']
                or signature(payload) != signature(proposal)):
            fail('payload_mismatch')
    # Critical records use the existing hash-deduplicated retention owner.
    from .source_retention import capture as retain, _Budget, BATCH_BYTES
    meter = _Budget(BATCH_BYTES)
    for entry in sorted({row['entry_id'] for row in rows}):
        retain(store, entry, _meter=meter)
    store.db.execute('INSERT OR IGNORE INTO sg_revision_items VALUES(?,?,?,?,?,?)',
        (identity, store.config['project_id'], node_id, proposal['type'], rows[0]['evidence_id'], stamp()))
    for row in rows:
        values = (identity, row['evidence_id'], digest(row['text']), metadata(row), row['role'], row['kind'], row['timestamp'])
        old = store.db.execute('SELECT * FROM sg_revision_sources WHERE revision=? AND evidence=?', values[:2]).fetchone()
        if old and tuple(old) != values:
            fail('source_changed')
        store.db.execute('INSERT OR IGNORE INTO sg_revision_sources VALUES(?,?,?,?,?,?,?)', values)
    return identity


def valid_sources(store, revision, *, evidence=None):
    """Point reads only. Changed attribution invalidates even identical quotes."""
    valid = []
    rows = list(store.db.execute('SELECT * FROM sg_revision_sources WHERE revision=?'
        + (' AND evidence=?' if evidence else '') + ' ORDER BY evidence LIMIT 65',
        (revision, evidence) if evidence else (revision,)))
    if len(rows) > 64:
        fail('source_read_limit')
    for item in rows:
        try:
            row = source(store, item['evidence'])
            if digest(row['text']) == item['text_hash'] and metadata(row) == item['metadata_hash']:
                valid.append(row)
        except Failure as error:
            if error.code != 'revision_source_changed':
                raise
    return valid


def baseline(store, node_id):
    """Adopt only an exact existing evidence payload, never invent history."""
    row = store.db.execute('SELECT m.*,n.type FROM work_memory m JOIN sg2_nodes n ON n.id=m.id WHERE m.id=? AND n.project=?',
                           (node_id, store.config['project_id'])).fetchone()
    if not row or row['type'] not in KINDS:
        return
    evidence = store.db.execute('SELECT id,assertion_json FROM sg2_evidence WHERE node=? AND entry_id=? AND quote=? LIMIT 65',
                               (node_id, row['source_entry_id'], row['source_quote'])).fetchall()
    for item in evidence[:64]:
        proposal = json.loads(item['assertion_json'])
        if [proposal[k] for k in ('type', 'title', 'content', 'status')] == [row['type'], row['title'], row['content'], row['status']]:
            capture(store, node_id, proposal, [item['id']])
            return
    # A shared node's primary citation can be reanchored independently of its
    # immutable revision payload. Reuse only that exact, still-valid baseline.
    recorded = store.db.execute('''SELECT r.id,e.assertion_json FROM sg_revision_items r
        JOIN sg2_evidence e ON e.id=r.payload_evidence
        WHERE r.project=? AND r.node=? AND e.node=? ORDER BY r.id LIMIT 65''',
        (store.config['project_id'], node_id, node_id)).fetchall()
    if len(recorded) > 64:
        fail('baseline_unavailable')
    for item in recorded:
        proposal = json.loads(item['assertion_json'])
        if ([proposal[k] for k in ('type', 'title', 'content', 'status')]
                == [row['type'], row['title'], row['content'], row['status']]
                and valid_sources(store, item['id'])):
            return
    fail('baseline_unavailable')


def _item(store, identity):
    row = store.db.execute('SELECT * FROM sg_revision_items WHERE id=? AND project=?',
                           (identity, store.config['project_id'])).fetchone()
    if row is None:
        fail('unknown_item')
    return row


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.utcoffset() is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, AttributeError):
        fail('effective_time_invalid')


@bounded_read
def assess(store, manifest, *, apply=False):
    """Explicit local semantic review, NOT automatic model/user authorization.

    Preview is read-only. Apply must run under maintenance.project_lock. Facet,
    same-condition and direct-authority judgments are review inputs, not facts
    proved by a quote or role; this is why normal readers never consume them yet.
    """
    if not enabled(store.config):
        fail('policy_disabled')
    if apply:
        store._ensure_writable()
        if not store.db.in_transaction:
            fail('transaction_required')
    fields = {'version', 'project_id', 'source', 'target', 'effect', 'facet', 'conditions',
              'evidence', 'authority', 'effective_from'}
    if not isinstance(manifest, dict) or set(manifest) != fields:
        fail('review_schema')
    if any(not isinstance(manifest[k], str) for k in fields - {'conditions'}):
        fail('review_schema')
    if manifest['version'] != VERSION or manifest['project_id'] != store.config['project_id']:
        fail('review_project_or_version')
    if not exists(store.db, 'sg_revision_items'):
        fail('ledger_unavailable')
    effect = manifest['effect']
    if effect not in EFFECTS or manifest['authority'] not in {'source_report', 'direct_user'}:
        fail('review_schema')
    if (not isinstance(manifest['facet'], str) or not manifest['facet'].strip()
            or len(manifest['facet']) > 160 or manifest['facet'] != manifest['facet'].strip()):
        fail('review_schema')
    conditions = manifest['conditions']
    if (not isinstance(conditions, dict) or len(conditions) > 8
            or any(not isinstance(k, str) or not k.strip() or len(k) > 80
                   or type(v) not in (str, int, float, bool, type(None)) and not (
                       k == 'qualifiers' and isinstance(v, list) and len(v) <= 8
                       and all(isinstance(x, str) and 0 < len(x) <= 240 for x in v))
                   or isinstance(v, str) and len(v) > 300
                   or type(v) is float and not math.isfinite(v) for k, v in conditions.items())):
        fail('review_schema')
    from .graph_contract import SECRET
    if SECRET.search(serial(manifest)):
        fail('review_contains_secret')
    effective = _time(manifest['effective_from'])
    a, b = _item(store, manifest['source']), _item(store, manifest['target'])
    if a['id'] == b['id']:
        fail('self_edge')
    from .graph_compaction import canonical, member_sql, members
    if canonical(store,a['node']) != canonical(store,b['node']) or a['kind'] != b['kind']:
        fail('subject_mismatch')
    current = valid_sources(store, a['id'], evidence=manifest['evidence'])
    cited = next((row for row in current if row['evidence_id'] == manifest['evidence']), None)
    if cited is None or not valid_sources(store, b['id']):
        fail('review_evidence_invalid')
    proposal = json.loads(cited['assertion_json'])
    if proposal.get('context_kind', 'work') != 'work':
        fail('review_context_invalid')
    destructive = effect in {'corrects', 'replaces', 'withdraws'}
    if manifest['authority'] == 'direct_user' and (cited['role'] != 'user' or cited['kind'] != 'message'):
        fail('authority_invalid')
    if destructive:
        if len(members(store,b['node']))>1:
            membership,member_args=member_sql(store,b['node'])
            old_roles={r[0] for r in store.db.execute(f'SELECT DISTINCT s.role FROM sg_revision_sources s JOIN sg_revision_items i ON i.id=s.revision WHERE i.node IN ({membership})',member_args)}
        else:
            old_roles = {r[0] for r in store.db.execute('SELECT DISTINCT role FROM sg_revision_sources WHERE revision=?', (b['id'],))}
        if proposal['status'] == 'proposed':
            fail('proposed_cannot_revoke')
        if a['kind'] == 'Decision' and manifest['authority'] != 'direct_user':
            fail('user_authority_required')
        # Assistant reports can correct their own reports, not erase user/tool
        # evidence. All other factual adjudication remains an explicit review.
        if cited['role'] == 'assistant' and old_roles - {'assistant'}:
            fail('authority_insufficient')
        if cited['role'] not in {'assistant', 'user', 'tool'}:
            fail('authority_insufficient')
    payload = {**manifest, 'effective_from': effective}
    identity = 'ra_' + digest(serial(payload))[:40]
    # Append-only DAG across all destructive reviews, even future/invalid ones;
    # re-importing an old assertion cannot silently revive a withdrawn version.
    if destructive:
        cycle = store.db.execute('''WITH RECURSIVE reach(id) AS (
          SELECT target FROM sg_revision_reviews WHERE project=? AND source=?
            AND effect IN ('corrects','replaces','withdraws')
          UNION SELECT r.target FROM sg_revision_reviews r JOIN reach x ON r.source=x.id
            WHERE r.project=? AND r.effect IN ('corrects','replaces','withdraws'))
          SELECT 1 FROM reach WHERE id=? LIMIT 1''',
          (store.config['project_id'], b['id'], store.config['project_id'], a['id'])).fetchone()
        if cycle:
            fail('replacement_cycle')
    result = dict(review_id=identity, applied=False, model_calls=0, review_origin='explicit_local_review',
                  note='Facet/conditions/authority are reviewed judgments, not proven by provenance or user approval.')
    if apply:
        store.db.execute('INSERT OR IGNORE INTO sg_revision_reviews VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (identity, store.config['project_id'], a['id'], b['id'], effect, manifest['facet'],
             serial(conditions), cited['evidence_id'], manifest['authority'], effective, stamp()))
        result['applied'] = True
        refresh(store, a['node'])
    return result


def refresh(store, node_id):
    """Write-side effective materialization; no copied statement/source bodies."""
    if not effective(store.config):
        return
    if not store.db.in_transaction:
        fail('transaction_required')
    from .graph_contract import atomic_key
    from .graph_compaction import canonical, member_sql, semantic, members
    node_id=canonical(store,node_id)
    membership, member_args=member_sql(store,node_id)
    projection = inspect(store, node_id, limit=100)
    revisions = projection['revisions']
    if not revisions:
        return
    payloads = {r['id']: json.loads(r['assertion_json']) for r in store.db.execute(f'''
        SELECT r.id,e.assertion_json FROM sg_revision_items r JOIN sg2_evidence e ON e.id=r.payload_evidence
        WHERE r.project=? AND r.node IN ({membership}) ORDER BY r.id LIMIT 100''', (store.config['project_id'], *member_args))}
    keys = {serial(atomic_key(p)) for p in payloads.values()}
    complete_lane = len(keys) == 1 and 'null' not in keys
    removed, disputed, invalid = set(), set(), projection.get('has_more', False)
    for rev in revisions:
        invalid |= rev['source_state'] != 'valid' or not rev['effects_complete']
        atom = payloads[rev['id']].get('assertion', [])
        for effect in rev['effects']:
            if effect['state'] == 'retracted':
                continue
            invalid |= effect['state'] != 'reviewed'
            same_lane = (complete_lane and effect['facet'] == atom[0]['predicate']
                         and effect['conditions'] == {'qualifiers': sorted(atom[0]['conditions'])})
            if effect['state'] == 'reviewed' and same_lane:
                if effect['effect'] in {'corrects', 'replaces', 'withdraws'}:
                    removed.add(rev['id'])
                elif effect['effect'] == 'disputes':
                    disputed.update((rev['id'], effect['source']))
    # Equivalent snapshots keep all immutable sources, but are one semantic head.
    # A reviewed full-lane correction covers exact duplicates, not another value.
    if len(members(store,node_id))>1:
        removed_values={semantic(payloads[r]) for r in removed}
        disputed_values={semantic(payloads[r]) for r in disputed if r in payloads}
        # Equal source/target values must not erase both equivalence classes.
        invalid |= any(effect['state']=='reviewed' and effect['effect'] in {'corrects','replaces','withdraws'}
            and effect['source'] in payloads and semantic(payloads[effect['source']]) in removed_values
            for rev in revisions for effect in rev['effects'])
        removed.update(r for r,p in payloads.items() if semantic(p) in removed_values)
        disputed.update(r for r,p in payloads.items() if semantic(p) in disputed_values)
    heads = [r for r in revisions if r['id'] not in removed
             and payloads[r['id']].get('provenance', 'direct') == 'direct']
    # No timestamp winner, including competing replacements and incomplete scope.
    one_head = len(heads)==1 or (len(members(store,node_id))>1 and len({semantic(payloads[r['id']]) for r in heads})==1)
    chosen = heads[0] if complete_lane and one_head and heads and not invalid and not disputed.intersection(r['id'] for r in heads) else None
    state = ('needs_review' if invalid else 'disputed' if disputed.intersection(r['id'] for r in heads)
             else 'legacy_unassessed' if all(atomic_key(p) is None for p in payloads.values())
             else 'current' if chosen else 'derived_only' if not heads else 'needs_review')
    now = stamp()
    future = store.db.execute(f'''SELECT min(effective_from) FROM (
        SELECT r.effective_from FROM sg_revision_reviews r JOIN sg_revision_items i ON i.id=r.target
          WHERE i.node IN ({membership}) AND r.effective_from>?
        UNION ALL SELECT c.effective_from FROM sg_revision_review_controls c
          JOIN sg_revision_items i ON i.id=c.source WHERE i.node IN ({membership}) AND c.effective_from>?)''',
        (*member_args, now, *member_args, now)).fetchone()[0]
    store.db.execute('''INSERT INTO sg_revision_projection VALUES(?,?,?,?,?,?)
        ON CONFLICT(node) DO UPDATE SET state=excluded.state,revision=excluded.revision,
        alternatives_json=excluded.alternatives_json,next_change=excluded.next_change''',
        (node_id, store.config['project_id'], state, chosen['id'] if chosen else None,
         serial([r['id'] for r in heads]), future))
    if chosen:
        payload = payloads[chosen['id']]
        evidence = store.db.execute('''SELECT e.entry_id,e.quote,e.chat FROM sg_revision_sources s
            JOIN sg2_evidence e ON e.id=s.evidence WHERE s.revision=? ORDER BY s.evidence LIMIT 1''',
            (chosen['id'],)).fetchone()
        store.db.execute('''UPDATE work_memory SET title=?,content=?,status=?,source_entry_id=?,source_quote=?,chat_id=?
            WHERE id=?''', (payload['title'], payload['content'], payload['status'], evidence['entry_id'],
            evidence['quote'], evidence['chat'], node_id))
        from .graph_v2 import _index
        _index(store, node_id, payload)


def projected(store, node_id):
    """Constant-size normal read, no ledger walk, migration or network."""
    return projected_many(store, [node_id]).get(node_id)


def projected_many(store, node_ids):
    """One bounded metadata query for an already selected page of objects."""
    if not effective(store.config) or not exists(store.db, 'sg2_nodes'):
        return {}
    materialized = exists(store.db, 'sg_revision_projection')
    from .graph_compaction import canonical
    bindings={node:canonical(store,node) for node in node_ids}
    ids = sorted(set(bindings.values()))
    result, now = {}, stamp()
    for start in range(0, len(ids), 200):
        chunk = ids[start:start+200]
        marks = ','.join('?' for _ in chunk)
        # Lazy adoption: old IDs stay readable without a graph-wide backfill.
        # An unversioned compound claim must not look like confirmed current truth.
        query = (f'''SELECT n.id AS node,p.state,p.revision,p.alternatives_json,p.next_change
            FROM sg2_nodes n LEFT JOIN sg_revision_projection p ON p.node=n.id AND p.project=n.project
            WHERE n.project=? AND n.type IN ('Claim','Decision') AND n.id IN ({marks})'''
            if materialized else f'''SELECT id AS node,NULL AS state,NULL AS revision,
                NULL AS alternatives_json,NULL AS next_change FROM sg2_nodes
                WHERE project=? AND type IN ('Claim','Decision') AND id IN ({marks})''')
        for row in store.db.execute(query, (store.config['project_id'], *chunk)):
            expired = row['next_change'] is not None and row['next_change'] <= now
            result[row['node']] = dict(state='needs_review' if expired else row['state'] or 'legacy_unassessed',
                                      revision=row['revision'], alternatives=json.loads(row['alternatives_json'] or '[]'))
    return {node:result[cid] for node,cid in bindings.items() if cid in result}


def adjudicate(store, output, bindings, client, stats):
    """One semantic question only for changed atomic values; replay uses judge cache."""
    if not effective(store.config):
        return []
    from .graph_contract import atomic_key, atomic_anchored
    from .graph_v2 import judge, _question, _accept, _evidence_context
    from .single_token_judge import SingleTokenJudge
    manifests = []
    for proposal in output['nodes']:
        node = bindings.get(proposal['id'])
        if (not node or not atomic_anchored(proposal) or proposal.get('provenance') != 'direct'):
            continue
        identity = 'ar_' + digest(serial([VERSION, store.config['project_id'], node, signature(proposal)]))[:40]
        incoming = _item(store, identity)
        heads = projected(store, node)['alternatives']
        if identity not in heads or len(heads) > 16:
            continue
        representative = json.loads(store.db.execute('SELECT assertion_json FROM sg2_evidence WHERE id=?',
                                                      (incoming['payload_evidence'],)).fetchone()[0])
        # Bounded point lookup in the same canonical node, never all project history.
        marks = ','.join('?' for _ in heads)
        others = list(store.db.execute(f'''SELECT r.id,e.assertion_json FROM sg_revision_items r
            JOIN sg2_evidence e ON e.id=r.payload_evidence WHERE r.node=? AND r.project=? AND r.id!=?
            AND r.id IN ({marks}) ORDER BY r.id LIMIT 16''', (node, store.config['project_id'], identity, *heads)))
        source_rows = valid_sources(store, incoming['id'], evidence=incoming['payload_evidence'])
        cited = next(iter(source_rows), None)
        if cited is None:
            continue
        for old in others:
            previous = json.loads(old['assertion_json'])
            if (old['id'] not in heads or atomic_key(previous) != atomic_key(proposal)
                    or previous.get('provenance', 'direct') != 'direct'):
                continue
            reviewed = store.db.execute('''SELECT 1 FROM sg_revision_reviews WHERE project=?
                AND ((source=? AND target=?) OR (source=? AND target=?)) LIMIT 1''',
                (store.config['project_id'], identity, old['id'], old['id'], identity)).fetchone()
            if reviewed:
                continue
            # Equal values are supporting evidence, not a revision decision.
            if previous['assertion'][0]['value'] == proposal['assertion'][0]['value']:
                continue
            # An additional paraphrase/source for the same value reuses the
            # immutable semantic question, rather than buying another verdict.
            state = dict(incoming=representative, previous=previous,
                         comparison_scope=dict(target_revision=old['id'],
                             target=previous['assertion'][0], incoming=representative['assertion'][0]),
                         original_context=_evidence_context(store, [representative, previous]))
            question = _question(
                'Classify the incoming assertion relative to the previous one in EXACTLY the same atomic scope. '
                'Use original speaker evidence. Later timestamps and repeated summaries do not prove replacement. '
                'The ONLY target is comparison_scope.target, not another statement mentioned in the source. '
                'Compare its value to comparison_scope.incoming before interpreting correction language. '
                'A compatible paraphrase supports the target even if the source corrects another statement. '
                'corrects/replaces/withdraws require an explicit correction, replacement or withdrawal, not just a different value. '
                'A proposal is not a decision; only a direct user directive can replace a Decision.',
                dict(corrects='Explicitly corrects the prior assertion in this scope.',
                     replaces='Explicitly adopts a new constraint instead of the previous one.',
                     withdraws='Explicitly withdraws the previous assertion without establishing its opposite.',
                     disputes='Incompatible observations in the same scope, without authority to resolve them.',
                     supports='Additional compatible support.',
                     insufficient_evidence='Different context, summary, ambiguous authority or insufficient evidence.'))
            answer = judge(store, client, 'revision', state, {'r0': question}, stats)['answers']['r0']
            effect = answer['choice']
            if effect not in EFFECTS or not _accept(answer, effect, .98, labels=isinstance(client, SingleTokenJudge)):
                continue
            atom = proposal['assertion'][0]
            manifest = dict(version=VERSION, project_id=store.config['project_id'], source=identity,
                target=old['id'], effect=effect, facet=atom['predicate'],
                conditions={'qualifiers': sorted(atom['conditions'])}, evidence=cited['evidence_id'],
                authority='direct_user' if cited['role']=='user' and cited['kind']=='message' else 'source_report',
                effective_from=_time(cited['timestamp']))
            # Admission failure leaves both versions visible as unresolved, never
            # turns a whole otherwise valid batch into a failed extraction.
            try:
                assess(store, manifest)
            except Failure:
                continue
            manifests.append(manifest)
    with store.db:
        if not store.db.in_transaction:
            store.db.execute('BEGIN IMMEDIATE')
        for manifest in manifests:
            assess(store, manifest, apply=True)
    return [m['source'] for m in manifests]


@bounded_read
def inspect(store, node_id, *, at=None, limit=50, after=''):
    """Bounded shadow projection; never writes, migrates, calls models or wakes."""
    from .review_control import current
    if type(limit) is not int or not 1 <= limit <= 100 or not isinstance(after, str):
        fail('read_limit')
    if not exists(store.db, 'sg_revision_items'):
        return dict(available=False, revisions=[], model_calls=0)
    now = _time(at) if at is not None else stamp()
    from .graph_compaction import member_sql
    membership, member_args=member_sql(store,node_id)
    rows = list(store.db.execute(f'SELECT * FROM sg_revision_items WHERE project=? AND node IN ({membership}) AND id>? ORDER BY id LIMIT ?',
                                (store.config['project_id'], *member_args, after, limit + 1)))
    output = []
    for row in rows[:limit]:
        evidence = store.db.execute('SELECT assertion_json FROM sg2_evidence WHERE id=?', (row['payload_evidence'],)).fetchone()
        proposal = json.loads(evidence[0]) if evidence else {}
        sources = valid_sources(store, row['id'])
        effects = []
        # A review's scope is a lane, never an excuse to revoke the whole item.
        # Multiple different scopes stay separate; no current-winner selection.
        reviews = list(store.db.execute('SELECT * FROM sg_revision_reviews WHERE project=? AND target=? AND effective_from<=? ORDER BY id LIMIT 33',
                                       (store.config['project_id'], row['id'], now)))
        for review in reviews[:32]:
            control = current(store, review, now)
            valid = (bool(valid_sources(store, review['source'], evidence=review['evidence']))
                     if control['state']=='active' else False)
            state = ('needs_review' if control['state']=='needs_review' else
                     'retracted' if control['state']=='retracted' else
                     'reviewed' if valid and sources else 'needs_review')
            effects.append(dict(id=review['id'], source=review['source'], effect=review['effect'],
                facet=review['facet'], conditions=json.loads(review['conditions_json']),
                evidence=review['evidence'], authority=review['authority'], effective_from=review['effective_from'],
                state=state, control=control))
        lanes = {}
        for effect in effects:
            if effect['state']=='retracted':
                continue
            key = serial([effect['facet'], effect['conditions']])
            lanes.setdefault(key, []).append(effect)
        scopes = []
        for values in lanes.values():
            terminal = [v for v in values if v['effect'] in {'corrects', 'replaces', 'withdraws'} and v['state']=='reviewed']
            terminal_kinds = {(v['source'], v['effect']) for v in terminal}
            state = ('needs_review' if len(reviews)>32 or any(v['state']=='needs_review' for v in values)
                else 'disputed' if len(terminal_kinds)>1 or any(v['effect']=='disputes' for v in values)
                else {'corrects':'corrected', 'replaces':'superseded', 'withdraws':'withdrawn'}[terminal[0]['effect']] if terminal
                else 'supported')
            scopes.append(dict(facet=values[0]['facet'], conditions=values[0]['conditions'], state=state))
        output.append(dict(id=row['id'], node=row['node'], kind=row['kind'], title=proposal.get('title'), content=proposal.get('content'),
            execution_status=proposal.get('status'), source_state='valid' if sources else 'needs_review',
            evidence_ids=[s['evidence_id'] for s in sources], effects=effects, scopes=scopes, effects_complete=len(reviews)<=32,
            source_refs=[dict(s) for s in store.db.execute('''SELECT s.evidence,e.entry_id,s.role,s.kind,s.source_time,s.text_hash,s.metadata_hash
                FROM sg_revision_sources s JOIN sg2_evidence e ON e.id=s.evidence
                WHERE s.revision=? ORDER BY s.evidence LIMIT 64''', (row['id'],))],
            validity='needs_review' if not sources or len(reviews)>32 or any(e['state']=='needs_review' for e in effects) else 'disputed' if any(e['effect']=='disputes' and e['state']=='reviewed' for e in effects) else 'unassessed'))
    return dict(available=True, shadow=True, revisions=output, has_more=len(rows)>limit,
                next_after=rows[limit-1]['id'] if len(rows)>limit else None, model_calls=0,
                note='No newest-wins rule or automatic default-reader changes; effects apply only in their reviewed facet/conditions.')
