"""One app-wide provider identity; project budgets and graph data stay local."""
from __future__ import annotations

import json
from pathlib import Path

from .provider_credentials import runtime_state

PROVIDER_FIELDS = {
    "base_url_env", "api_key_env", "model_env", "timeout_seconds",
    "max_output_tokens", "protocol", "endpoint", "model", "auth",
}
IDENTITY_FIELDS = PROVIDER_FIELDS - {"timeout_seconds", "max_output_tokens"}


def read_template(state: Path) -> dict:
    path = state / "template.json"
    if path.is_symlink():
        raise ValueError("unsafe_provider_settings")
    if not path.exists():
        return {}
    if path.stat().st_size > 256 * 1024:
        raise ValueError("configuration_too_large")
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("provider"), dict):
        raise ValueError("invalid_provider_settings")
    return data


def effective_provider(local: dict, state: Path) -> dict:
    shared = read_template(state).get("provider")
    if shared is None:
        return dict(local)
    # Identity is replaced as a unit: stale local endpoint/model env cannot leak in.
    provider = {key: value for key, value in shared.items() if key in PROVIDER_FIELDS}
    for key in ("timeout_seconds", "max_output_tokens"):
        if key in local:
            provider[key] = local[key]
    return provider


def apply_provider(config: dict) -> None:
    local = config.get("provider", {})
    provider = effective_provider(local, runtime_state())
    identity_changed = any(local.get(key) != provider.get(key) for key in IDENTITY_FIELDS)
    config["provider"] = provider
    if identity_changed:
        # Pins attest to an old route. Do not reuse its cached classifications.
        options = config.get("graph_v2", {}).get("single_token_judge", {})
        options.pop("expected_model", None)
        options.pop("route_revision", None)
