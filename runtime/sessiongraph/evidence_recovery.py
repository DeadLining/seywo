"""Explicit, once-only recovery. Reuse exact audited responses before spending calls."""
import hashlib
import json
from pathlib import Path
import time
import uuid

from . import recovery
from .storage import exists, load_payload
from .job_lifecycle import cascade_schema

REVISION = 'exact-evidence-recovery-v1'
GRANT_SCHEMA = '''CREATE TABLE IF NOT EXISTS sg_resource_grants(
 project TEXT NOT NULL,job TEXT NOT NULL,reason TEXT NOT NULL,created REAL NOT NULL,
 limits_json TEXT NOT NULL,PRIMARY KEY(project,job,reason));''' + cascade_schema('sg_resource_grants')


def audited_output(store, row, audits):
    from . import graph_v2 as g
    from .graph_contract import prepare_evidence_spans, decode_evidence_candidates
    from .condensation import enabled
    from .revision_ledger import effective
    desc = json.loads(row['input_json'])
    batch = g._restore_batch(store, desc['entries'])
    offered = load_payload(store, row['candidates_json'])
    prepared, _, anchors = prepare_evidence_spans(batch,
        g.policy(offered, effective=effective(store.config), condense=enabled(store)))
    failures = []
    for audit, sha in sorted(audits, key=lambda item:item[0].get('created_at', 0), reverse=True):
        try:
            if (audit.get('http_status') != 200 or audit.get('status') != 'completed'
                    or audit.get('final_output_truncated') or audit.get('request_omitted')):
                continue
            sent = audit['request']['input']
            if len(sent) != 1 or len(sent[0]['content']) != 1:
                continue
            wire = json.loads(sent[0]['content'][0]['text'])
            if wire != {'untrusted_transcript': prepared}:
                continue
            attempt = store.db.execute('SELECT 1 FROM sg2_attempts WHERE job=? AND id=? AND part=? AND stage=\'extract\'',
                (row['id'], audit.get('attempt_id'), audit.get('part'))).fetchone()
            if not attempt:
                continue
            original = json.loads(audit['final_output'])
            output = decode_evidence_candidates(original, anchors,
                entry_ids=[e['id'] for e in batch], atomic_refs=effective(store.config))
            output, _ = g.redact_display_fields(output)
            output = g.normalize_references(output, offered)
            output, _ = g.restore_source_quotes(output, batch)
            output, _ = g.anchor_identity_evidence(output, batch)
            if enabled(store) and any('retention' not in n for n in output.get('nodes', [])):
                raise ValueError('graph_schema:retention_required')
            g.validate(output, batch, offered, defer_unanchored_identities=True)
            isolated = sum(n.get('context_kind') == 'uncertain' and old.get('context_kind') != 'uncertain'
                for n, old in zip(output['nodes'], original['nodes']))
            return output, {'request_id':audit['id'], 'sha256':sha, 'isolated_candidates':isolated}, None
        except (KeyError, TypeError, ValueError) as exc:
            failures.append(str(exc)[:160])
    return None, None, failures[0] if failures else 'no_matching_complete_audit'


def repair(store, *, apply=False, retry_invalid=False, grant_budget=False, launch=True):
    from .resource_policy import settings
    project = store.config['project_id']
    rows = store.db.execute('''SELECT j.*,c.error_json FROM sg2_jobs j JOIN sg2_control c ON c.job=j.id
        WHERE j.project=? AND c.state='blocked' AND j.state NOT IN ('complete','superseded','dismissed')
        AND NOT EXISTS(SELECT 1 FROM sg2_attempts a WHERE a.job=j.id AND a.state='running')
        ORDER BY j.created_at,j.id''', (project,)).fetchall()
    selected = {r['id'] for r in rows}
    audits = {job:[] for job in selected}
    folder = Path(store.config['database']).parent / 'request-audit'
    for path in folder.glob('request-*.json'):
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            continue
        try:
            raw = path.read_bytes()
            data = json.loads(raw)
            job = data.get('job_id')
            if (job in selected and data.get('stage') == 'extract'
                    and path.name == f'request-{data.get("id")}.json'):
                audits[job].append((data, hashlib.sha256(raw).hexdigest()))
        except (OSError, ValueError):
            continue
    result = dict(model_calls=0, applied=apply, recovered=0, reused=0, retried=0,
                  budget_grants=0, candidates=[])
    if apply:
        store._ensure_writable()
        store.db.executescript(recovery.SCHEMA)
        store.db.executescript(GRANT_SCHEMA)
    for row in rows:
        error = json.loads(row['error_json'] or '{}')
        code = error.get('code')
        if code not in {'provider_evidence_reference', 'resource_budget_exhausted',
                        'revision_baseline_unavailable'}:
            continue
        key = f'{REVISION}:{row["id"]}:{code}'
        used = exists(store.db, 'sg2_recoveries') and store.db.execute(
            'SELECT 1 FROM sg2_recoveries WHERE automatic_key=?', (key,)).fetchone()
        output, proof, problem = None, None, None
        refreshed = None
        action = None
        if not used and code == 'provider_evidence_reference':
            try:
                from . import graph_v2 as g
                desc = json.loads(row['input_json'])
                fresh = g._without_deleted_context(store, desc) if not row['output_json'] else desc
                if fresh != desc:
                    batch = g._restore_batch(store, fresh['entries'])
                    refreshed = (fresh, g._live_offered(store, load_payload(store,row['candidates_json'])))
                    action = 'refresh_deleted_context'
                else:
                    output, proof, problem = audited_output(store, row, audits[row['id']])
                if output is not None:
                    action = 'reuse_validated_response'
                elif retry_invalid and not action:
                    action = 'retry_under_fixed_contract'
            except (ValueError, KeyError, OSError) as exc:
                # Source integrity failures must never be retried away.
                problem = str(exc)[:160]
        if not used and code == 'resource_budget_exhausted' and grant_budget:
            policy = settings(store.config)
            if policy:
                action = 'explicit_bounded_budget_grant'
        if (not used and code == 'revision_baseline_unavailable'
                and retry_invalid and row['output_json']):
            try:
                from . import graph_v2 as g
                desc = json.loads(row['input_json'])
                g._restore_batch(store, desc['entries'])
                action = 'retry_under_fixed_baseline'
            except (ValueError, KeyError, OSError) as exc:
                problem = str(exc)[:160]
        result['candidates'].append(dict(job_id=row['id'], chat=row['chat'], code=code,
            action=action, problem='already_recovered' if used else problem, proof=proof))
        if not apply or not action:
            continue
        with store.db:
            n = store.db.execute("UPDATE sg2_control SET state='ready',due=0,failures=0 WHERE job=? AND state='blocked' AND error_json IS ?",
                (row['id'], row['error_json'])).rowcount
            if not n:
                continue
            if output is not None:
                store.db.execute("UPDATE sg2_jobs SET output_json=?,state='judging',updated_at=? WHERE id=?",
                    (json.dumps(output, ensure_ascii=False), time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), row['id']))
                result['reused'] += 1
            elif refreshed:
                store.db.execute('DELETE FROM sg2_checkpoints WHERE job=?', (row['id'],))
                store.db.execute('UPDATE sg2_jobs SET input_json=?,candidates_json=? WHERE id=?',
                    (json.dumps(refreshed[0],ensure_ascii=False),json.dumps(refreshed[1],ensure_ascii=False),row['id']))
                result['retried'] += 1
            elif action == 'explicit_bounded_budget_grant':
                limits = {k:policy[k] for k in ('max_requests','max_input_bytes','max_output_tokens','max_transport_seconds')}
                store.db.execute('INSERT INTO sg_resource_grants VALUES(?,?,?,?,?)',
                    (project,row['id'],REVISION,time.time(),json.dumps(limits)))
                result['budget_grants'] += 1
            else:
                result['retried'] += 1
            before = dict(row)
            before['revalidation_proof'] = proof
            store.db.execute('INSERT INTO sg2_recoveries(id,job,reason,created,before_json,proof_id,automatic_key) VALUES(?,?,?,?,?,?,?)',
                (uuid.uuid4().hex,row['id'],action,time.time(),json.dumps(before,ensure_ascii=False),
                 proof['request_id'] if proof else None,key))
            result['recovered'] += 1
    if apply:
        # Existing recovery outbox provides crash-safe, project-scoped wakeups.
        wake = recovery.recover(store, apply=True, launch=launch)
        result['scheduled_chats'] = wake.get('scheduled_chats', [])
    return result
