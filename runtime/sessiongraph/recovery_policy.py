"""Opt-in recovery controls; no I/O, provider construction, or schema migration."""
import hashlib
import json
import os


def settings(config):
    value = config.get('graph_v2', {}).get('recovery_policy', {})
    if not isinstance(value, dict) or type(value.get('enabled', False)) is not bool:
        raise ValueError('graph recovery_policy.enabled must be a boolean')
    if not value.get('enabled', False):
        return None
    calls = value.get('max_calls_per_run', 3)
    if type(calls) is not int or not 1 <= calls <= 3:
        raise ValueError('graph recovery max_calls_per_run must be 1..3')
    return {'max_calls_per_run': calls, 'revision': 'bounded-recovery-v1'}


def retry_scope(config, job, part, stage):
    if settings(config) is None:
        return None
    from .extraction import _provider_options
    from .request_audit import PIPELINE_REVISION
    provider = _provider_options(config)
    protocol = provider.get('protocol', 'chat-completions')
    selected = {
        'protocol': protocol,
        'model': (provider.get('model') or os.environ.get(provider.get('model_env', ''), ''))
                 if protocol == 'responses' else os.environ.get(provider.get('model_env', ''), ''),
        'endpoint': provider.get('endpoint', '') if protocol == 'responses'
                    else os.environ.get(provider.get('base_url_env', ''), ''),
        'timeout': provider.get('timeout_seconds'),
        'output_limit': provider.get('max_output_tokens'),
    }
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    scope = {'version': 1, 'project': config['project_id'], 'job': job, 'part': part,
             'stage': stage, 'semantic_revision': PIPELINE_REVISION,
             'provider_fingerprint': digest(selected)}
    scope['id'] = digest(scope)
    return scope
