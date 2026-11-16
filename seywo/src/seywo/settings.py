"""Project-local provider settings for the standalone and plugin runtimes.

The plugin must be relocatable: provider metadata and credentials live under the
selected project's ignored ``.seywo`` directory. No SessionGraph application
state or environment credential is required.
"""

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from .locking import file_lock
from urllib.parse import urlsplit


DEFAULT_ENDPOINT_HOSTS = {"localhost", "127.0.0.1", "::1"}
DEFAULT_TIMEOUT = 180
DEFAULT_MAX_OUTPUT_TOKENS = 4000


def _project_state(config: dict) -> Path:
    path = config.get("_path")
    if not isinstance(path, str) or not path:
        raise ValueError("project_config_required")
    state = Path(path).expanduser().absolute().parent
    if state.name != ".seywo":
        raise ValueError("project_config_required")
    if state.is_symlink():
        raise ValueError("Seywo configuration must not be a symlink")
    return state


def _credential_dir(config: dict) -> Path:
    state = _project_state(config)
    folder = state / "credentials"
    if folder.is_symlink():
        raise ValueError("Seywo credentials must not be a symlink")
    return folder


def _credential_name(endpoint: str) -> str:
    return "provider-" + hashlib.sha256(endpoint.encode()).hexdigest() + ".json"


def validate_endpoint(endpoint: str) -> str:
    if not isinstance(endpoint, str):
        raise ValueError("provider_requires_exact_responses_endpoint")
    endpoint = endpoint.strip().rstrip("/")
    if not endpoint.endswith("/responses"):
        endpoint += "/responses"
    parsed = urlsplit(endpoint)
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("provider_requires_exact_responses_endpoint")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in DEFAULT_ENDPOINT_HOSTS):
        raise ValueError("provider_requires_exact_responses_endpoint")
    if not parsed.netloc:
        raise ValueError("provider_requires_exact_responses_endpoint")
    return endpoint


def _read_credential(config: dict, endpoint: str) -> str:
    folder = _credential_dir(config)
    if not folder.is_dir() or folder.is_symlink():
        return ""
    mode = stat.S_IMODE(folder.stat().st_mode)
    if mode != 0o700 or folder.stat().st_uid != os.getuid():
        raise ValueError("unsafe_credentials")
    path = folder / _credential_name(endpoint)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return ""
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid() or info.st_size > 65536):
            raise ValueError("unsafe_credentials")
        data = json.loads(handle.read(65537))
    if not isinstance(data, dict) or data.get("endpoint") != endpoint:
        raise ValueError("credential_endpoint_mismatch")
    key = data.get("api_key") or ""
    if not isinstance(key, str):
        raise ValueError("unsafe_credentials")
    return key


def _write_credential(config: dict, endpoint: str, api_key: str) -> None:
    folder = _credential_dir(config)
    folder.mkdir(mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    path = folder / _credential_name(endpoint)
    if path.is_symlink():
        raise ValueError("Seywo credential must not be a symlink")
    payload = json.dumps({"endpoint": endpoint, "api_key": api_key}, ensure_ascii=False) + "\n"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder,
                                         prefix=".provider-", delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_config(config: dict, data: dict) -> None:
    path = Path(config["_path"])
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".seywo-settings-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_provider(config=None) -> dict:
    """Read the project-local provider and attach its key for the worker only."""
    runtime = (config or {}).get("model_runtime") or {}
    if runtime.get("backend") == "codex":
        from .codex_transport import DEFAULT_MODEL
        return {"backend": "codex", "model": runtime.get("model") or DEFAULT_MODEL,
                "timeout": 420, "max_output_tokens": 768}
    if not runtime:
        from .legacy_settings import provider
        return provider(config)
    if runtime.get("backend") != "responses":
        raise ValueError("unsupported_model_backend")
    local = (config or {}).get("provider") or {}
    if not isinstance(local, dict):
        raise ValueError("provider_not_configured")
    endpoint = validate_endpoint(local.get("endpoint"))
    model = local.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("provider_not_configured")
    result = {
        "endpoint": endpoint,
        "model": model.strip(),
        "timeout": int(local.get("timeout_seconds") or DEFAULT_TIMEOUT),
        "max_output_tokens": int(local.get("max_output_tokens") or DEFAULT_MAX_OUTPUT_TOKENS),
    }
    if config is not None:
        result["_api_key"] = _read_credential(config, endpoint)
    return result


def load_key(endpoint: str) -> str:
    """Standalone compatibility only; plugin paths use explicit project keys."""
    from .legacy_settings import load_key as legacy_key
    return legacy_key(endpoint)


def provider_status(config: dict) -> dict:
    runtime = config.get("model_runtime") or {}
    if runtime.get("backend") == "codex":
        from .codex_transport import codex_command, DEFAULT_MODEL
        return {"configured": bool(codex_command()), "backend": "codex", "endpoint": "",
                "model": runtime.get("model") or DEFAULT_MODEL, "credential_required": False,
                "credential_configured": False, "model_calls": 0}
    local = config.get("provider") or {}
    if not isinstance(local, dict) or not local.get("endpoint") or not local.get("model"):
        return {"configured": False, "backend": "responses", "endpoint": "", "model": "", "credential_configured": False,
                "model_calls": 0}
    try:
        endpoint = validate_endpoint(local["endpoint"])
        key = _read_credential(config, endpoint)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        endpoint = str(local.get("endpoint") or "")
        key = ""
    return {"configured": bool(endpoint and local.get("model")), "backend": "responses", "endpoint": endpoint,
            "model": str(local.get("model") or ""), "credential_configured": bool(key),
            "model_calls": 0}


def save_provider(config: dict, *, endpoint: str, model: str, api_key: str = "",
                  timeout_seconds: int = DEFAULT_TIMEOUT,
                  max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS) -> dict:
    endpoint = validate_endpoint(endpoint)
    if not isinstance(model, str) or not model.strip():
        raise ValueError("provider_not_configured")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
        raise ValueError("invalid_provider_timeout")
    if type(max_output_tokens) is not int or not 256 <= max_output_tokens <= 1_000_000:
        raise ValueError("invalid_provider_output_limit")
    from .config import load_config
    with file_lock(_project_state(config) / ".init.lock"):
        fresh = load_config(Path(config["_path"]))
        current = provider_status(fresh)
        if not api_key:
            if current.get("endpoint") == endpoint:
                api_key = _read_credential(fresh, endpoint)
            if not api_key:
                raise ValueError("provider_credential_required")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("provider_credential_required")
        data = {k: v for k, v in fresh.items() if not k.startswith("_")}
        data["model_runtime"] = {"backend": "responses"}
        data["provider"] = {"protocol": "responses", "endpoint": endpoint, "model": model.strip(),
                            "timeout_seconds": timeout_seconds, "max_output_tokens": max_output_tokens}
        _write_credential(fresh, endpoint, api_key.strip())
        _write_config(fresh, data)
    return {"saved": True, "backend": "responses", "endpoint": endpoint, "model": model.strip(),
            "credential_configured": True, "model_calls": 0}


def use_codex(config: dict, *, model: str = "") -> dict:
    from .config import load_config
    with file_lock(_project_state(config) / ".init.lock"):
        fresh = load_config(Path(config["_path"]))
        data = {k: v for k, v in fresh.items() if not k.startswith("_")}
        data["model_runtime"] = {"backend": "codex", **({"model": model.strip()} if model.strip() else {})}
        data["hook"] = {**(data.get("hook") or {}), "pipeline": "seywo-compact-v1"}
        _write_config(fresh, data)
    return {"saved": True, **provider_status({**fresh, "model_runtime": data["model_runtime"]})}
