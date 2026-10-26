"""Opt-in write-side notifications in the existing continuation queue.

Stat fingerprints coalesce notifications, never verify source integrity. The
importer still verifies identity, project binding and the historical prefix.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import stat
import time
import uuid

EVENTS = {'Stop', 'PreCompact', 'PostCompact'}


def enabled(config):
    value = config.get('hook', {}).get('durable_ingress', False)
    if type(value) is not bool:
        raise ValueError('hook.durable_ingress must be a boolean')
    if not value:
        return False
    from .recovery_policy import settings
    if config.get('graph_v2', {}).get('enabled') is not True or settings(config) is None:
        raise ValueError('durable ingress requires graph_v2 and bounded recovery')
    return config.get('hook', {}).get('enabled') is not False


def pending(job):
    sources = (job.get('ingress') or {}).get('sources', {})
    return sorted(((key, item) for key, item in sources.items() if item['pending']),
                  key=lambda pair: (pair[1]['observed_at'], pair[0]))


def notify(config_path, chat, event, transcript, *, launch=True, now=None):
    """Persist before launch, one latest notification per source, no source reads.

    Separate segments are retained separately; coalescing one path cannot discard
    another path. Checkpoints grow with distinct segments, not turns or polls.
    """
    from . import continuation as c
    from .core import load_config
    config_path = Path(config_path).resolve()
    config = load_config(config_path)
    if not enabled(config) or (config_path.parent / 'disabled').exists():
        return {'accepted': False, 'reason': 'disabled', 'model_calls': 0}
    if event not in EVENTS or not isinstance(chat, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', chat):
        raise ValueError('invalid ingress identity or event')
    if not isinstance(transcript, str) or not transcript or len(transcript) > 4096 or not Path(transcript).is_absolute():
        raise ValueError('durable ingress requires an absolute transcript path')
    path = Path(transcript).resolve()
    roots = config.get('hook', {}).get('allowed_transcript_roots', [])
    if path.suffix != '.jsonl' or not any(path.is_relative_to(Path(root).expanduser().resolve()) for root in roots):
        raise ValueError('ingress transcript is outside the allowlist')
    source = hashlib.sha256(str(path).encode()).hexdigest()
    folder = c.root(config)
    stamp = time.time() if now is None else now
    with c.locked(folder):
        # Observe under the notification lock so a delayed old stat cannot
        # overwrite a newer observation. No transcript bytes are read here.
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('ingress transcript is not a regular file')
            frontier = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        except FileNotFoundError:
            frontier = ['missing']  # Import classifies the failure durably.
        fingerprint = hashlib.sha256(json.dumps(frontier).encode()).hexdigest()
        queue = c.read(folder / 'queue.json', {})
        identity = c.key(config_path, chat)
        prior = queue.get(identity, {})
        ingress = prior.get('ingress') or {'version': 1, 'sources': {}}
        item = ingress['sources'].get(source)
        duplicate = item is not None and item['fingerprint'] == fingerprint
        if not duplicate:
            ingress['sources'][source] = {
                'path': str(path), 'fingerprint': fingerprint, 'revision': uuid.uuid4().hex,
                'pending': True, 'event': event, 'observed_at': stamp,
            }
            # New input can revive a blocked chat, not reset per-job retry controls.
            queue[identity] = {**prior, 'config': str(config_path), 'chat': chat,
                'target': prior.get('target'), 'run_id': prior.get('run_id', ''),
                'state': 'ready' if prior.get('state') != 'running' else 'running',
                'due': stamp, 'updated_at': stamp, 'ingress': ingress, 'error_code': None,
                'wake_pending': True}
            c.write(folder / 'queue.json', queue)
        # Retrying an accepted notification also retries a failed launcher, but
        # never revives blocked/acknowledged work or resets its failure budget.
        if launch and queue[identity].get('wake_pending') and queue[identity]['state'] not in {'blocked', 'dismissed'}:
            c.start(folder)
            queue[identity]['wake_pending'] = False
            c.write(folder / 'queue.json', queue)
    return {'accepted': True, 'coalesced': duplicate, 'model_calls': 0}


def reconcile(queue, identity, prior, run, claimed, *, input_imported):
    """Merge a result without acknowledging notifications received during work.

    Called only under queue.lock, after the ordinary graph scheduling reducer.
    A dismissed checkpoint is understood by old schedulers and invisible to reads.
    """
    ingress = prior.get('ingress')
    if not ingress:
        return
    if claimed and claimed.get('ingress_source') and input_imported:
        source = claimed['ingress_source']
        selected = claimed['ingress']['sources'][source]
        current = ingress['sources'].get(source)
        if current and current['revision'] == selected['revision']:
            current.update(pending=False, acknowledged_run_id=run['run_id'])
    result = queue.get(identity)
    if result is None:
        result = {**prior, 'target': None, 'run_id': run['run_id'],
                  'state': 'dismissed', 'dismissal_reason': 'ingress_checkpoint',
                  'dismissed_at': 0, 'due': 0, 'updated_at': run.get('updated_at', 0),
                  'failures': 0, 'no_progress': 0, 'error_code': None}
    if (pending({'ingress': ingress}) and not prior.get('pending_import')
            and run['state'] in {'succeeded', 'cancelled'}):
        result.update(state='ready' if run['state'] == 'succeeded' else 'blocked',
                      due=0, error_code=None if run['state'] == 'succeeded' else 'ingress_cancelled')
    result['ingress'] = ingress
    result.pop('lease', None)
    result.pop('ingress_source', None)
    queue[identity] = result
