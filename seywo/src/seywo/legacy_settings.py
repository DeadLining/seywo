"""Compatibility for explicitly configured standalone installations only.

The plugin selects model_runtime=codex (or responses) and never enters this path.
"""
import hashlib
import json
import os
from pathlib import Path
import stat


def state_dir():
    override = os.environ.get('SEYWO_APP_STATE') or os.environ.get('SESSIONGRAPH_RUNTIME_HOME')
    if override:
        return Path(override).expanduser()
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA')
        if not base:
            raise OSError('LOCALAPPDATA is required')
        return Path(base) / 'SessionGraphData'
    return Path.home() / 'Library/Application Support/local-ai-gateway/sessiongraph'


def provider(config):
    local = (config or {}).get('provider') or {}
    template = state_dir() / 'template.json'
    if template.is_file() and not template.is_symlink() and template.stat().st_size <= 262144:
        shared = json.loads(template.read_text(encoding='utf-8')).get('provider')
        if isinstance(shared, dict):
            local = shared
    if not local:
        raise ValueError('provider_not_configured')
    endpoint, model = local.get('endpoint'), local.get('model')
    if not isinstance(endpoint, str) or not isinstance(model, str) or not model:
        raise ValueError('provider_not_configured')
    if not endpoint.endswith('/responses'):
        raise ValueError('provider_requires_exact_responses_endpoint')
    return {'endpoint': endpoint, 'model': model, 'timeout': int(local.get('timeout_seconds') or 180),
            'max_output_tokens': int(local.get('max_output_tokens') or 4000)}


def load_key(endpoint):
    folder = state_dir() / 'credentials'
    name = 'provider-' + hashlib.sha256(endpoint.encode()).hexdigest() + ('.dpapi' if os.name == 'nt' else '.json')
    if os.name == 'nt':
        try:
            from sessiongraph.windows_secret import unprotect
        except ImportError:
            return ''
        target = folder / name
        if not target.exists():
            return ''
        data = json.loads(unprotect(target.read_bytes()))
    else:
        try:
            fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return ''
        try:
            meta = os.fstat(fd)
            if meta.st_uid != os.getuid() or stat.S_IMODE(meta.st_mode) != 0o700:
                raise ValueError('unsafe_credentials')
            try:
                keyfd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                return ''
            with os.fdopen(keyfd, 'r') as stream:
                data = json.load(stream)
        finally:
            os.close(fd)
    if data.get('endpoint') != endpoint:
        raise ValueError('credential_endpoint_mismatch')
    return data.get('api_key') or ''
