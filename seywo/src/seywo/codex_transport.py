"""Seywo's Codex model adapter.

Semantic generation goes through the Codex app-server protocol. The adapter
keeps the existing compact/provider contract while the app-server worker owns
processes, JSON-RPC framing, isolation and bounded recovery.
"""
from __future__ import annotations

from pathlib import Path
import time
import tomllib

from .codex_app_server import get_worker, AppServerError
from .codex_runtime import list_models, validate_reasoning_effort
from .plugin_context import catalog_home
from .runtime_platform import find_codex

DEFAULT_MODEL = 'codex-default'


def codex_command():
    return find_codex()


def model_selection(home, requested):
    """Freeze the configured target; never label it an upstream identity."""
    path = Path(home) / 'config.toml'
    config = tomllib.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    profile = (config.get('profiles') or {}).get(config.get('profile'), {})
    explicit = requested and requested != DEFAULT_MODEL
    selected = requested if explicit else profile.get('model', config.get('model'))
    provider = profile.get('model_provider', config.get('model_provider', 'openai'))
    if selected is not None and (not isinstance(selected, str) or not selected or len(selected) > 200):
        raise ValueError('invalid model selection')
    if not isinstance(provider, str) or not provider or len(provider) > 100:
        raise ValueError('invalid provider selection')
    return {'backend': 'codex', 'requested_model': requested or DEFAULT_MODEL,
            'selected_model': selected, 'provider': provider,
            'selection': 'explicit' if explicit else 'config' if selected else 'codex_builtin'}


class CodexTransport:
    """Stable provider interface backed by a reusable isolated app-server worker."""

    def __init__(self, *, model=DEFAULT_MODEL, timeout=420, command=None, home=None, reasoning_effort='auto'):
        self.model = model
        self.reasoning_effort = validate_reasoning_effort(reasoning_effort)
        self.timeout = max(0.1, float(timeout))
        self.command = command if command is not None else codex_command()
        self.home = Path(home) if home is not None else catalog_home()
        self._worker = None

    def _failed(self, started, code, *, model_info=None, model_calls=0):
        return {'output': None, 'finish': 'error', 'error': code, 'usage': None,
                'model_calls': model_calls, 'model_info': model_info,
                'seconds': round(time.monotonic() - started, 3)}

    def _execute(self, instructions, payload, schema, *, model=None):
        started = time.monotonic()
        if not self.command:
            return self._failed(started, 'codex_not_available')
        try:
            info = model_selection(self.home, model or self.model)
        except (OSError, ValueError, TypeError, UnicodeError, tomllib.TOMLDecodeError):
            return self._failed(started, 'codex_config_invalid')

        catalog = list_models(command=self.command, home=self.home, timeout=min(self.timeout, 15), refresh=False)
        if catalog.get('error'):
            return self._failed(started, catalog['error'], model_info=info)
        target = next((row for row in catalog['models'] if
                       row['model'] == info['selected_model'] or
                       (info['selected_model'] is None and row.get('is_default'))), {})
        efforts = target.get('reasoning_efforts') or []
        if self.reasoning_effort != 'auto':
            if self.reasoning_effort not in efforts:
                return self._failed(started, 'codex_reasoning_unsupported', model_info=info)
            effort = self.reasoning_effort
        else:
            effort = next((value for value in ('low', 'minimal', 'none') if value in efforts),
                          target.get('default_reasoning_effort'))
        if effort:
            info['reasoning_effort'] = effort

        try:
            self._worker = get_worker(command=self.command, home=self.home, timeout=self.timeout)
        except AppServerError as exc:
            return self._failed(started, exc.code, model_info=info)
        budget = self.timeout - (time.monotonic() - started)
        if budget <= 0:
            return self._failed(started, 'codex_timeout', model_info=info)
        result = self._worker.complete(
            model=info['selected_model'], provider=info['provider'],
            instructions=instructions, payload=payload, output_schema=schema,
            reasoning_effort=effort, timeout=budget, model_info=info)
        result['seconds'] = round(time.monotonic() - started, 3)
        return result

    def complete_compact(self, *, model, instructions, payload, max_output_tokens=768):
        from .compact_model import schema_for
        return self._execute(instructions, payload, schema_for(payload), model=model)

    def probe(self):
        return self._execute(
            'Return exactly a JSON object with the boolean field ok set to true.',
            {'probe': 'seywo'},
            {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
             'required': ['ok'], 'additionalProperties': False})

    def close(self):
        if self._worker is not None:
            self._worker.close()
            self._worker = None
