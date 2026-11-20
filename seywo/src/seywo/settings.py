"""User-level model settings shared by every Seywo project and process.

Project configuration owns records and hooks only. Model selection and provider
credentials live below the active Codex home, outside repositories and versioned
plugin installations. Readers never cache settings or initialize project state.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import urlsplit

from .codex_runtime import validate_reasoning_effort
from .locking import file_lock
from .plugin_context import catalog_home
from .runtime_platform import (private_owner, protect_credential, replace_file,
                               reveal_credential, sync_directory)

DEFAULT_ENDPOINT_HOSTS = {"localhost", "127.0.0.1", "::1"}
DEFAULT_TIMEOUT = 180
DEFAULT_MAX_OUTPUT_TOKENS = 4000


def settings_home() -> Path:
    """Stable across projects and plugin upgrades, including custom CODEX_HOME."""
    return catalog_home() / 'seywo'


def _state(*, create=False) -> Path:
    state = settings_home().expanduser().absolute()
    if state.is_symlink():
        raise ValueError('Seywo settings directory must not be a symlink')
    if create:
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(state, 0o700)
    if state.exists() and (not state.is_dir() or not private_owner(state.stat(), 0o700)):
        raise ValueError('unsafe_settings')
    return state


def _credential_dir(state: Path) -> Path:
    folder = state / 'credentials'
    if folder.is_symlink():
        raise ValueError('Seywo credentials must not be a symlink')
    return folder


def validate_endpoint(endpoint: str) -> str:
    if not isinstance(endpoint, str):
        raise ValueError('provider_requires_exact_responses_endpoint')
    endpoint = endpoint.strip().rstrip('/')
    if not endpoint.endswith('/responses'):
        endpoint += '/responses'
    parsed = urlsplit(endpoint)
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError('provider_requires_exact_responses_endpoint')
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in DEFAULT_ENDPOINT_HOSTS):
        raise ValueError('provider_requires_exact_responses_endpoint')
    if not parsed.netloc:
        raise ValueError('provider_requires_exact_responses_endpoint')
    return endpoint


def _private_json(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'r', encoding='utf-8') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or not private_owner(info, 0o600) or info.st_size > 65536:
            raise ValueError('unsafe_settings')
        data = json.loads(handle.read(65537))
    if not isinstance(data, dict):
        raise ValueError('invalid_model_settings')
    return data


def _read_settings(state: Path) -> dict:
    path = state / 'settings.json'
    if path.is_symlink():
        raise ValueError('Seywo settings must not be a symlink')
    try:
        data = _private_json(path)
    except FileNotFoundError:
        return {'schema_version': 1, 'model_runtime': {'backend': 'codex'}}
    runtime = data.get('model_runtime')
    if (data.get('schema_version') != 1 or not isinstance(runtime, dict)
            or runtime.get('backend') not in {'codex', 'responses'}):
        raise ValueError('invalid_model_settings')
    return data


def _read_credential(state: Path, local: dict) -> str:
    name = local.get('credential')
    if not isinstance(name, str) or not re.fullmatch(r'provider-[a-f0-9]{64}\.json', name):
        raise ValueError('unsafe_credentials')
    folder = _credential_dir(state)
    if not folder.is_dir() or not private_owner(folder.stat(), 0o700):
        raise ValueError('unsafe_credentials')
    data = _private_json(folder / name)
    endpoint = local['endpoint']
    if data.get('endpoint') != endpoint:
        raise ValueError('credential_endpoint_mismatch')
    key = reveal_credential(data, endpoint)
    if not isinstance(key, str):
        raise ValueError('unsafe_credentials')
    return key


def _write_json(path: Path, data: dict) -> None:
    if path.is_symlink():
        raise ValueError('Seywo settings must not be a symlink')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.seywo-settings-', delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            handle.write(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        replace_file(temporary, path)
        sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _provider(state: Path, data: dict, *, backend=None) -> dict:
    runtime = data['model_runtime']
    selected = backend or runtime['backend']
    if selected == 'codex':
        from .codex_transport import DEFAULT_MODEL
        return {'backend': 'codex', 'model': runtime.get('model') or DEFAULT_MODEL,
                'timeout': 420, 'max_output_tokens': 768,
                'reasoning_effort': validate_reasoning_effort(runtime.get('reasoning_effort', 'auto'))}
    if selected != 'responses':
        raise ValueError('unsupported_model_backend')
    local = data.get('provider')
    if not isinstance(local, dict):
        raise ValueError('provider_not_configured')
    endpoint = validate_endpoint(local.get('endpoint'))
    model = local.get('model')
    if not isinstance(model, str) or not model.strip():
        raise ValueError('provider_not_configured')
    return {'backend': 'responses', 'endpoint': endpoint, 'model': model.strip(),
            'timeout': int(local.get('timeout_seconds') or DEFAULT_TIMEOUT),
            'max_output_tokens': int(local.get('max_output_tokens') or DEFAULT_MAX_OUTPUT_TOKENS),
            '_api_key': _read_credential(state, local)}


def read_provider(config=None, *, backend=None) -> dict:
    """Take one coherent snapshot for a job/probe; never consult a project."""
    state = _state()
    if not state.exists():
        return _provider(state, _read_settings(state), backend=backend)
    with file_lock(state / 'settings.lock', exclusive=False):
        return _provider(state, _read_settings(state), backend=backend)


def _status(provider: dict) -> dict:
    common = {'scope': 'user', 'model_calls': 0, 'backend': provider['backend'], 'model': provider['model']}
    if provider['backend'] == 'codex':
        from .codex_transport import codex_command, model_selection
        try:
            effective_model = model_selection(catalog_home(), provider['model'])['selected_model']
        except (OSError, ValueError, TypeError):
            effective_model = None
        return {**common, 'configured': bool(codex_command()), 'endpoint': '',
                'effective_model': effective_model, 'reasoning_effort': provider['reasoning_effort'],
                'credential_required': False, 'credential_configured': False}
    return {**common, 'configured': True, 'endpoint': provider['endpoint'],
            'credential_required': True, 'credential_configured': bool(provider['_api_key'])}


def provider_status(config=None) -> dict:
    """Read user-level settings without creating storage or exposing secrets."""
    return _status(read_provider())


def save_provider(config=None, *, endpoint: str, model: str, api_key: str = '',
                  timeout_seconds: int = DEFAULT_TIMEOUT,
                  max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS) -> dict:
    endpoint = validate_endpoint(endpoint)
    if not isinstance(model, str) or not model.strip():
        raise ValueError('provider_not_configured')
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
        raise ValueError('invalid_provider_timeout')
    if type(max_output_tokens) is not int or not 256 <= max_output_tokens <= 1_000_000:
        raise ValueError('invalid_provider_output_limit')
    if not isinstance(api_key, str):
        raise ValueError('provider_credential_required')
    state = _state(create=True)
    with file_lock(state / 'settings.lock'):
        data = _read_settings(state)
        local = data.get('provider') or {}
        if not api_key:
            api_key = _read_credential(state, local) if local.get('endpoint') == endpoint else ''
        if not api_key.strip():
            raise ValueError('provider_credential_required')
        folder = _credential_dir(state)
        folder.mkdir(mode=0o700, exist_ok=True)
        os.chmod(folder, 0o700)
        # Immutable credential generation + atomic settings pointer publication:
        # a failed publication leaves the previously selected key intact.
        name = 'provider-' + hashlib.sha256((endpoint + '\0' + api_key.strip()).encode()).hexdigest() + '.json'
        _write_json(folder / name, {'endpoint': endpoint, **protect_credential(api_key.strip(), endpoint)})
        data['model_runtime'] = {'backend': 'responses'}
        data['provider'] = {'protocol': 'responses', 'endpoint': endpoint, 'model': model.strip(),
                            'timeout_seconds': timeout_seconds, 'max_output_tokens': max_output_tokens,
                            'credential': name}
        _write_json(state / 'settings.json', data)
        for old in folder.glob('provider-*.json'):
            if old.name != name and re.fullmatch(r'provider-[a-f0-9]{64}\.json', old.name):
                old.unlink()
    return {'saved': True, 'persisted': True, 'scope': 'user', 'backend': 'responses',
            'endpoint': endpoint, 'model': model.strip(), 'credential_configured': True, 'model_calls': 0}


def use_codex(config=None, *, model: str = '', reasoning_effort: str = 'auto') -> dict:
    reasoning_effort = validate_reasoning_effort(reasoning_effort)
    if not isinstance(model, str) or len(model.strip()) > 200:
        raise ValueError('invalid_model_selection')
    model = model.strip()
    if model == 'codex-default':
        model = ''
    state = _state(create=True)
    with file_lock(state / 'settings.lock'):
        data = _read_settings(state)
        data['model_runtime'] = {'backend': 'codex', **({'model': model} if model else {}),
                                 **({'reasoning_effort': reasoning_effort} if reasoning_effort != 'auto' else {})}
        _write_json(state / 'settings.json', data)
        status = _status(_provider(state, data))
    return {'saved': True, 'persisted': True, **status}
