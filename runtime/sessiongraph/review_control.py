"""Immutable explicit controls for mistaken shadow reviews, never user approval.

The predecessor is a compare-and-swap token, not a timestamp winner. Source
payloads remain owned by the assertion ledger and existing retention archive.
"""
import json

from . import revision_ledger as ledger
from .core import digest
from .storage import exists

VERSION = 'assertion-review-control-v1'
TABLE = 'sg_revision_review_controls'
CHAIN_LIMIT = 64


def _review(store, identity):
    row = store.db.execute('SELECT * FROM sg_revision_reviews WHERE project=? AND id=?',
                           (store.config['project_id'], identity)).fetchone()
    if row is None:
        ledger.fail('unknown_review')
    return row


def _chain(store, review):
    meter = ledger.READ.get()
    if meter is not None and review['id'] in meter['controls']:
        return meter['controls'][review['id']]
    rows = list(store.db.execute('SELECT * FROM sg_revision_review_controls WHERE project=? AND review=? ORDER BY id LIMIT ?',
                                (store.config['project_id'], review['id'], CHAIN_LIMIT + 1)))
    if len(rows) > CHAIN_LIMIT:
        ledger.fail('control_history_limit')
    if meter is not None:
        if len(rows) > meter['control_rows']:
            ledger.fail('control_read_limit')
        meter['control_rows'] -= len(rows)
    following = {r['previous']:r for r in rows}
    chain, previous, effective = [], review['id'], review['effective_from']
    while previous in following:
        row = following.pop(previous)
        if row['effective_from'] < effective or row['action'] not in {'retract','restore','amend'}:
            ledger.fail('control_chain_invalid')
        chain.append(row)
        previous, effective = row['id'], row['effective_from']
    if following or len(chain) != len(rows):
        ledger.fail('control_chain_invalid')
    if meter is not None:
        meter['controls'][review['id']] = chain
    return chain


def current(store, review, at):
    """A valid explicit later control may correct a mistaken prior control.

    Invalid current evidence is needs_review, never implicit reactivation of an
    earlier effect. The whole chain remains inspectable; only its selected head
    decides this review's participation. Retraction does not prove an assertion.
    """
    available = exists(store.db, TABLE)
    base = dict(available=available, state='not_effective' if at<review['effective_from'] else 'active',
                head=review['id'], recorded_head=review['id'], replacement_review=None)
    if not available:
        return base
    chain = _chain(store, review)
    base['recorded_head'] = chain[-1]['id'] if chain else review['id']
    eligible = [r for r in chain if r['effective_from'] <= at]
    if not eligible:
        return base
    head = eligible[-1]
    valid = bool(ledger.valid_sources(store, head['source'], evidence=head['evidence']))
    return dict(available=True, head=head['id'], recorded_head=base['recorded_head'], replacement_review=head['replacement_review'],
                state='needs_review' if not valid else 'active' if head['action']=='restore' else 'retracted')


def _evidence(store, manifest, review):
    a = ledger._item(store, manifest['source'])
    b = ledger._item(store, review['target'])
    if a['node'] != b['node'] or a['kind'] != b['kind']:
        ledger.fail('subject_mismatch')
    cited = next(iter(ledger.valid_sources(store, a['id'], evidence=manifest['evidence'])), None)
    if cited is None:
        ledger.fail('review_evidence_invalid')
    payload = json.loads(cited['assertion_json'])
    if payload.get('context_kind', 'work') != 'work':
        ledger.fail('review_context_invalid')
    if payload['status'] == 'proposed':
        ledger.fail('proposed_cannot_revoke')
    authority = manifest['authority']
    if authority == 'direct_user' and (cited['role'] != 'user' or cited['kind'] != 'message'):
        ledger.fail('authority_invalid')
    if (a['kind']=='Decision' or review['authority']=='direct_user') and authority != 'direct_user':
        ledger.fail('user_authority_required')
    roles = {r[0] for r in store.db.execute('''SELECT DISTINCT s.role FROM sg_revision_sources s
        WHERE s.revision=? OR (s.revision=? AND s.evidence=?)''',
        (review['target'], review['source'], review['evidence']))}
    if cited['role'] not in {'assistant','user','tool'} or cited['role']=='assistant' and roles-{'assistant'}:
        ledger.fail('authority_insufficient')
    return cited


@ledger.bounded_read
def control(store, manifest, *, apply=False):
    """Preview/apply one explicit local review-control event.

    Write callers own maintenance.project_lock and an outer transaction. The
    savepoint also protects atomic amendment when a caller catches an exception.
    """
    if not ledger.enabled(store.config):
        ledger.fail('policy_disabled')
    if apply:
        store._ensure_writable()
        if not store.db.in_transaction:
            ledger.fail('transaction_required')
    fields = {'version','project_id','review','previous','action','source','evidence',
              'authority','effective_from','replacement'}
    if (not isinstance(manifest, dict) or set(manifest) != fields
            or any(not isinstance(manifest[k], str) or not manifest[k] or len(manifest[k])>200
                   for k in fields-{'replacement'})):
        ledger.fail('control_schema')
    if manifest['version'] != VERSION or manifest['project_id'] != store.config['project_id']:
        ledger.fail('review_project_or_version')
    if (manifest['action'] not in {'retract','restore','amend'}
            or manifest['authority'] not in {'direct_user','source_report'}
            or (manifest['action']=='amend') != isinstance(manifest['replacement'], dict)
            or manifest['action']!='amend' and manifest['replacement'] is not None):
        ledger.fail('control_schema')
    from .graph_contract import SECRET
    if SECRET.search(ledger.serial(manifest)):
        ledger.fail('review_contains_secret')
    if not exists(store.db, TABLE):
        ledger.fail('control_ledger_unavailable')
    review = _review(store, manifest['review'])
    effective = ledger._time(manifest['effective_from'])
    cited = _evidence(store, manifest, review)
    replacement = manifest['replacement']
    replacement_id = None
    if replacement is not None:
        if (replacement.get('target') != review['target']
                or replacement.get('source') != manifest['source']
                or replacement.get('evidence') != manifest['evidence']
                or replacement.get('authority') != manifest['authority']
                or ledger._time(replacement.get('effective_from')) != effective):
            ledger.fail('control_replacement_mismatch')
        replacement_id = ledger.assess(store, replacement)['review_id']
        if replacement_id == review['id']:
            ledger.fail('control_replacement_unchanged')
    identity = 'rc_' + digest(ledger.serial([VERSION, store.config['project_id'], review['id'],
        manifest['previous'], manifest['action'], manifest['source'], manifest['evidence'],
        manifest['authority'], effective, replacement_id]))[:40]
    result = dict(control_id=identity, review_id=review['id'], replacement_review=replacement_id,
                  applied=False, model_calls=0, review_origin='explicit_local_review',
                  note='No truth or user approval inferred; restoring this review does not disable separate replacement reviews.')
    recorded = store.db.execute('SELECT 1 FROM sg_revision_review_controls WHERE id=? AND project=?',
                               (identity, store.config['project_id'])).fetchone()
    if recorded:
        result.update(applied=apply, replay=True)
        return result
    chain = _chain(store, review)
    if len(chain) >= CHAIN_LIMIT:
        ledger.fail('control_history_limit')
    head = chain[-1] if chain else review
    if manifest['previous'] != head['id']:
        ledger.fail('control_stale_head')
    if chain:
        if head['authority']=='direct_user' and manifest['authority']!='direct_user':
            ledger.fail('user_authority_required')
        prior = store.db.execute('SELECT role FROM sg_revision_sources WHERE revision=? AND evidence=?',
                                 (head['source'], head['evidence'])).fetchone()
        if cited['role']=='assistant' and (not prior or prior['role']!='assistant'):
            ledger.fail('authority_insufficient')
    if effective < head['effective_from']:
        ledger.fail('control_time_order')
    if replacement_id and store.db.execute('SELECT 1 FROM sg_revision_reviews WHERE id=?', (replacement_id,)).fetchone():
        ledger.fail('control_replacement_exists')
    if apply:
        store.db.execute('SAVEPOINT sg_review_control')
        try:
            if replacement is not None:
                ledger.assess(store, replacement, apply=True)
            inserted = store.db.execute('INSERT INTO sg_revision_review_controls VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (identity, store.config['project_id'], review['id'], manifest['previous'], manifest['action'],
                 manifest['source'], manifest['evidence'], manifest['authority'], effective, replacement_id, ledger.stamp()))
            if inserted.rowcount != 1:
                ledger.fail('control_stale_head')
        except BaseException:
            store.db.execute('ROLLBACK TO sg_review_control')
            raise
        finally:
            store.db.execute('RELEASE sg_review_control')
        ledger.READ.get()['controls'].pop(review['id'], None)
        ledger.refresh(store, ledger._item(store, review['target'])['node'])
        result['applied'] = True
    return result


@ledger.bounded_read
def read(store, review_id, *, at=None, limit=20, after=''):
    """Bounded, immutable detail pagination; no migration/model/write/wakeup."""
    if type(limit) is not int or not 1 <= limit <= 100 or not isinstance(after, str):
        ledger.fail('read_limit')
    if not exists(store.db, 'sg_revision_reviews'):
        ledger.fail('ledger_unavailable')
    review = _review(store, review_id)
    now = ledger._time(at) if at is not None else ledger.stamp()
    state = current(store, review, now)
    rows = list(store.db.execute('SELECT * FROM sg_revision_review_controls WHERE project=? AND review=? AND id>? ORDER BY id LIMIT ?',
        (store.config['project_id'], review_id, after, limit+1))) if state['available'] else []
    actions = []
    for row in rows[:limit]:
        valid = bool(ledger.valid_sources(store, row['source'], evidence=row['evidence']))
        reference = store.db.execute('''SELECT e.entry_id,s.role,s.kind,s.source_time,s.text_hash,s.metadata_hash
            FROM sg_revision_sources s JOIN sg2_evidence e ON e.id=s.evidence
            WHERE s.revision=? AND s.evidence=?''', (row['source'],row['evidence'])).fetchone()
        actions.append({**dict(row), 'source_state':'valid' if valid else 'needs_review',
                        'source_ref':dict(reference) if reference else None})
    return dict(review=dict(review), control=state, actions=actions, has_more=len(rows)>limit,
                next_after=rows[limit-1]['id'] if len(rows)>limit else None, model_calls=0, shadow=True,
                note='Action details include future events; control is an effective-time view of currently known records, not knowledge-as-of history.')
