"""Opt-in cumulative graph-job admission and project/backend transport circuits.

Only the shared provider transport writes this ledger. No source bodies, model
retrieval, periodic wakeups or automatic quota resets. Reservations precede I/O.
"""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .failures import Failure

REVISION = 'job-resource-v1'
LIMITS = {
    'max_requests': (1, 512), 'max_input_bytes': (1, 64 * 1024 * 1024),
    'max_output_tokens': (1, 1024 * 1024), 'max_transport_seconds': (1, 3600),
    'circuit_failure_threshold': (1, 16), 'circuit_cooldown_seconds': (1, 3600),
}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS sg_resource_scopes(
 project TEXT NOT NULL,job TEXT NOT NULL,policy_json TEXT NOT NULL,
 requests INTEGER NOT NULL,input_bytes INTEGER NOT NULL,output_tokens INTEGER NOT NULL,
 seconds REAL NOT NULL,PRIMARY KEY(project,job));
CREATE TABLE IF NOT EXISTS sg_resource_requests(
 id TEXT PRIMARY KEY,project TEXT NOT NULL,job TEXT NOT NULL,attempt TEXT NOT NULL,
 backend TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,finished REAL,
 input_bytes INTEGER NOT NULL,output_tokens INTEGER NOT NULL,reserved_seconds REAL NOT NULL,
 elapsed_seconds REAL,code TEXT,usage_json TEXT);
CREATE INDEX IF NOT EXISTS sg_resource_requests_job ON sg_resource_requests(project,job);
CREATE TABLE IF NOT EXISTS sg_resource_circuits(
 project TEXT NOT NULL,backend TEXT NOT NULL,policy_json TEXT NOT NULL,failures INTEGER NOT NULL,
 open_until REAL NOT NULL,generation INTEGER NOT NULL,probe TEXT,probe_pid INTEGER,
 PRIMARY KEY(project,backend));
'''


class AdmissionDenied(Failure):
    network_sent = False

    def __init__(self, code, *, retry_at=None):
        super().__init__(code, retryable=retry_at is not None)
        self.retry_at = retry_at


def settings(config):
    graph = config.get('graph_v2', {})
    if not isinstance(graph, dict):
        raise AdmissionDenied('resource_policy_invalid')
    value = graph.get('resource_policy', {})
    if not isinstance(value, dict) or type(value.get('enabled', False)) is not bool:
        raise AdmissionDenied('resource_policy_invalid')
    if not value.get('enabled', False):
        return None
    from .recovery_policy import settings as recovery_settings
    if config.get('graph_v2', {}).get('enabled') is not True or recovery_settings(config) is None:
        raise AdmissionDenied('resource_policy_requires_bounded_graph')
    if set(value) != {'enabled', *LIMITS}:
        raise AdmissionDenied('resource_policy_invalid')
    for field, (low, high) in LIMITS.items():
        if type(value[field]) is not int or not low <= value[field] <= high:
            raise AdmissionDenied('resource_policy_invalid')
    return {'revision': REVISION, **{key: value[key] for key in LIMITS}}


def _connect(config):
    # An absent database is not permission to create an empty replacement.
    db = sqlite3.connect(Path(config['database']).resolve().as_uri() + '?mode=rw',
                         uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    return db


def _backend(client, req):
    from urllib.parse import urlsplit
    endpoint = urlsplit(req.full_url)
    if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise AdmissionDenied('resource_endpoint_invalid')
    value = [getattr(client, 'provider_name', 'extractor'), endpoint.scheme,
             endpoint.hostname, endpoint.port, endpoint.path, client.model]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def _alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def reserve(client, req, policy, *, now=None):
    from .stages import CURRENT
    scope = CURRENT.get()
    if not all(isinstance(scope.get(k), str) and scope[k] for k in ('job_id', 'attempt_id', 'part', 'stage')):
        raise AdmissionDenied('resource_scope_missing')
    if scope['stage'] not in {'extract', 'identity', 'relations'}:
        raise AdmissionDenied('resource_scope_invalid')
    try:
        body = json.loads(req.data)
        caps = [body[k] for k in ('max_output_tokens', 'max_completion_tokens', 'max_tokens') if k in body]
        if len(caps) != 1 or type(caps[0]) is not int or caps[0] < 1:
            raise AdmissionDenied('resource_output_limit_unknown')
    except (TypeError, ValueError, UnicodeError) as exc:
        if isinstance(exc, AdmissionDenied):
            raise
        raise AdmissionDenied('resource_request_invalid') from None
    if (type(client.timeout) not in (int, float) or not math.isfinite(client.timeout)
            or not 1 <= client.timeout <= 300):
        raise AdmissionDenied('resource_timeout_invalid')
    project, job = client.config['project_id'], scope['job_id']
    backend = _backend(client, req)
    stamp = time.time() if now is None else now
    identity = uuid.uuid4().hex
    policy_json = json.dumps(policy, sort_keys=True, separators=(',', ':'))
    circuit_policy = json.dumps({key: policy[key] for key in ('circuit_failure_threshold', 'circuit_cooldown_seconds')}, sort_keys=True)
    db = _connect(client.config)
    try:
        db.executescript(SCHEMA)
        with db:
            db.execute('BEGIN IMMEDIATE')
            # Require a committed live attempt, not just caller-provided IDs.
            owned = db.execute('''SELECT 1 FROM sg2_jobs j JOIN sg2_attempts a ON a.job=j.id
                WHERE j.project=? AND j.id=? AND a.id=? AND a.part=? AND a.stage=?
                AND a.state='running' AND j.state NOT IN ('complete','superseded')''',
                (project, job, scope['attempt_id'], scope['part'], scope['stage'])).fetchone()
            if not owned:
                raise AdmissionDenied('resource_scope_invalid')
            spent = db.execute('SELECT * FROM sg_resource_scopes WHERE project=? AND job=?', (project, job)).fetchone()
            if spent and spent['policy_json'] != policy_json:
                raise AdmissionDenied('resource_policy_changed')
            used = dict(spent) if spent else dict(requests=0, input_bytes=0, output_tokens=0, seconds=0)
            remaining = policy['max_transport_seconds'] - used['seconds']
            if (used['requests'] >= policy['max_requests']
                    or used['input_bytes'] + len(req.data) > policy['max_input_bytes']
                    or used['output_tokens'] + caps[0] > policy['max_output_tokens'] or remaining < 1):
                raise AdmissionDenied('resource_budget_exhausted')
            circuit = db.execute('SELECT * FROM sg_resource_circuits WHERE project=? AND backend=?', (project, backend)).fetchone()
            if circuit and circuit['policy_json'] != circuit_policy:
                raise AdmissionDenied('resource_circuit_policy_changed')
            generation = circuit['generation'] if circuit else 0
            probe = bool(circuit and circuit['failures'] >= policy['circuit_failure_threshold'])
            if probe and (circuit['open_until'] > stamp or (circuit['probe'] and _alive(circuit['probe_pid']))):
                raise AdmissionDenied('resource_circuit_open',
                    retry_at=circuit['open_until'] if circuit['open_until'] > stamp
                    else stamp + policy['circuit_cooldown_seconds'])
            seconds = min(client.timeout, remaining)
            db.execute('INSERT OR IGNORE INTO sg_resource_scopes VALUES(?,?,?,0,0,0,0)', (project, job, policy_json))
            db.execute('UPDATE sg_resource_scopes SET requests=requests+1,input_bytes=input_bytes+?,output_tokens=output_tokens+?,seconds=seconds+? WHERE project=? AND job=?',
                       (len(req.data), caps[0], seconds, project, job))
            db.execute("INSERT INTO sg_resource_requests VALUES(?,?,?,?,?,'reserved',?,NULL,?,?,?,NULL,NULL,NULL)",
                       (identity, project, job, scope['attempt_id'], backend, stamp, len(req.data), caps[0], seconds))
            db.execute('INSERT OR IGNORE INTO sg_resource_circuits VALUES(?,?,?,0,0,0,NULL,NULL)', (project, backend, circuit_policy))
            if probe:
                # Every new probe fences all earlier requests, including an
                # abandoned probe whose process died before settlement.
                generation += 1
                db.execute('UPDATE sg_resource_circuits SET probe=?,probe_pid=?,generation=? WHERE project=? AND backend=?',
                           (identity, os.getpid(), generation, project, backend))
    finally:
        db.close()
    return dict(id=identity, project=project, job=job, backend=backend,
                generation=generation, probe=probe, reserved_seconds=seconds,
                output_tokens=caps[0], sent=False, policy=policy)


def settle(config, ticket, elapsed, *, now=None):
    """Idempotent settlement. Unsettled/crashed reservations are never refunded."""
    stamp = time.time() if now is None else now
    if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
        elapsed = ticket['reserved_seconds']
    policy = ticket['policy']
    code = ticket.get('code')
    db = _connect(config)
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute('''UPDATE sg_resource_requests SET state=?,finished=?,elapsed_seconds=?,code=?,usage_json=?
                WHERE id=? AND project=? AND job=? AND state='reserved' ''',
                ('received' if ticket.get('received') and not code else 'failed' if ticket['sent'] else 'local_failure',
                 stamp, elapsed, code, json.dumps(ticket.get('usage')), ticket['id'], ticket['project'], ticket['job'])).rowcount
            if not changed:
                return
            observed_output = (ticket.get('usage') or {}).get('output_tokens')
            known_output = type(observed_output) is int and observed_output >= 0
            charged_output = ticket['output_tokens']
            if known_output:
                # Refund unused reservation only after a successful response.
                charged_output = (observed_output if ticket.get('received') and not code
                                  else max(charged_output, observed_output))
            db.execute('UPDATE sg_resource_scopes SET seconds=max(0,seconds-?+?),output_tokens=output_tokens+? WHERE project=? AND job=?',
                       (ticket['reserved_seconds'], elapsed, charged_output - ticket['output_tokens'], ticket['project'], ticket['job']))
            circuit = db.execute('SELECT * FROM sg_resource_circuits WHERE project=? AND backend=?',
                                 (ticket['project'], ticket['backend'])).fetchone()
            if not circuit or circuit['generation'] != ticket['generation']:
                return  # Late old responses cannot close a newly opened/recovered circuit.
            unhealthy = ticket.get('transport_failure') or code == 'resource_provider_budget_violation'
            if unhealthy or ticket['probe']:
                failed = unhealthy or not ticket.get('received')
                failures = max(circuit['failures'] + 1, policy['circuit_failure_threshold']) if ticket['probe'] and failed else circuit['failures'] + 1 if failed else 0
                opened = failures >= policy['circuit_failure_threshold']
                db.execute('UPDATE sg_resource_circuits SET failures=?,open_until=?,generation=generation+?,probe=NULL,probe_pid=NULL WHERE project=? AND backend=?',
                    (failures, stamp + policy['circuit_cooldown_seconds'] if opened else 0,
                     int(opened or ticket['probe']), ticket['project'], ticket['backend']))
            elif ticket.get('received') and not circuit['probe']:
                db.execute('UPDATE sg_resource_circuits SET failures=0 WHERE project=? AND backend=?',
                           (ticket['project'], ticket['backend']))
    finally:
        db.close()


def usage(ticket, value):
    if ticket is None or not isinstance(value, dict):
        return
    observed = {}
    for canonical, aliases in (('input_tokens', ('input_tokens', 'prompt_tokens')),
                               ('output_tokens', ('output_tokens', 'completion_tokens'))):
        for alias in aliases:
            if type(value.get(alias)) is int and value[alias] >= 0:
                observed[canonical] = value[alias]
                break
    ticket['usage'] = observed or None
    if observed.get('output_tokens', 0) > ticket['output_tokens']:
        ticket['code'] = 'resource_provider_budget_violation'
        raise Failure(ticket['code'])


@contextmanager
def admitted(client, req):
    try:
        policy = settings(client.config)
        ticket = reserve(client, req, policy) if policy else None
    except (ValueError, OSError, sqlite3.Error) as exc:
        # Judge adapters count just before this entry point. An admission denial
        # is not a provider call. Extractor stats are handled by their graph stage.
        if hasattr(client, 'calls'):
            client.calls = max(0, client.calls - 1)
        if isinstance(exc, AdmissionDenied):
            raise
        raise AdmissionDenied('resource_state_unavailable' if isinstance(exc, (OSError, sqlite3.Error)) else 'resource_policy_invalid') from None
    try:
        yield ticket
    finally:
        if ticket:
            # A failed settlement leaves its full pre-I/O reservation charged.
            settle(client.config, ticket, ticket.get('elapsed_seconds',
                ticket['reserved_seconds'] if ticket['sent'] else 0))
