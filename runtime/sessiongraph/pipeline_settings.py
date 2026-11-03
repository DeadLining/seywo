"""App-wide write-pipeline defaults; explicit project values remain authoritative."""
from copy import deepcopy

GRAPH_FIELDS = ('enabled', 'max_output_tokens', 'max_batches', 'identity_threshold',
                'relation_threshold', 'input_pipeline', 'judge_backend')
OPTIONS = {
    'jev': ('model', 'api_key_env', 'timeout_seconds'),
    'condensation': ('enabled',),
    'recovery_policy': ('enabled', 'max_calls_per_run'),
    'revision_policy': ('enabled', 'mode'),
    'stage_policy': ('enabled',),
    'resource_policy': ('enabled', 'max_requests', 'max_input_bytes', 'max_output_tokens',
                        'max_transport_seconds', 'circuit_failure_threshold',
                        'circuit_cooldown_seconds'),
    'single_token_judge': ('request_logprobs', 'reasoning_effort'),
}


def graph_template(config):
    graph = config.get('graph_v2')
    if not isinstance(graph, dict) or type(graph.get('enabled')) is not bool:
        return None
    result = {key: deepcopy(graph[key]) for key in GRAPH_FIELDS if key in graph}
    for group, fields in OPTIONS.items():
        if group in graph:
            value = graph[group]
            if not isinstance(value, dict):
                raise ValueError(f'graph_v2.{group} must be an object')
            result[group] = {key: deepcopy(value[key]) for key in fields if key in value}
    # Keep the established template shape; provider pins never cross projects.
    result.setdefault('jev', {})
    return result


def merge(defaults, local):
    result = deepcopy(defaults)
    for key, value in local.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def apply_defaults(config):
    from .provider_credentials import runtime_state
    from .provider_settings import read_template
    template = read_template(runtime_state())
    graph = graph_template(template)
    if graph is not None:
        local = config.get('graph_v2', {})
        if not isinstance(local, dict):
            raise ValueError('graph_v2 must be an object')
        effective = merge(graph, local)
        for group in OPTIONS:
            if group in effective and not isinstance(effective[group], dict):
                raise ValueError(f'graph_v2.{group} must be an object')
        # Do not inherit dependent policies across an explicit opt-out. Keep
        # explicit local settings for the existing policy validators to check.
        dependencies = {
            'recovery_policy': effective.get('enabled') is True,
            'revision_policy': effective.get('enabled') is True,
            'condensation': effective.get('enabled') is True,
            'resource_policy': (effective.get('enabled') is True
                                and effective.get('recovery_policy', {}).get('enabled') is True),
        }
        for group, allowed in dependencies.items():
            if not allowed and 'enabled' not in local.get(group, {}):
                effective.setdefault(group, {})['enabled'] = False
        if (effective.get('resource_policy', {}).get('enabled') is not True
                and 'enabled' not in local.get('stage_policy', {})):
            effective.setdefault('stage_policy', {})['enabled'] = False
        config['graph_v2'] = effective
    # Old project/template knobs cannot revive the retired maintenance path.
    config.get('graph_v2', {}).pop('auto_compaction', None)
    hook = template.get('hook', {})
    if not isinstance(hook, dict):
        raise ValueError('global hook must be an object')
    if 'durable_ingress' in hook:
        if type(hook['durable_ingress']) is not bool:
            raise ValueError('global durable_ingress must be a boolean')
        effective = config.get('graph_v2', {})
        # An explicit graph/recovery opt-out must not break ordinary Hooks.
        if (effective.get('enabled') is True
                and effective.get('recovery_policy', {}).get('enabled') is True):
            config.setdefault('hook', {}).setdefault('durable_ingress', hook['durable_ingress'])
