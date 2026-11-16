"""Project initialization; no host registration or legacy configuration discovery."""

import fcntl
import json
import os
import tempfile
from pathlib import Path
from uuid import uuid4

DEFAULT_IGNORE = "*\n!.gitignore\n"


def state_dir(root: Path) -> Path:
    state = root.resolve() / ".seywo"
    if state.is_symlink():
        raise ValueError(".seywo must not be a symlink")
    return state


def ensure_ignore(state: Path) -> None:
    path = state / ".gitignore"
    if path.is_symlink():
        raise ValueError(".seywo/.gitignore must not be a symlink")
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = existing.splitlines()
    # A new catch-all must precede custom exceptions, or it silently disables them.
    content = ("*\n" if "*" not in lines else "") + existing
    if "!.gitignore" not in lines:
        content += ("\n" if content and not content.endswith("\n") else "") + "!.gitignore\n"
    if path.exists() and content == existing:
        return
    _atomic_write(path, content)


def _atomic_write(path: Path, content: str) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".seywo-init-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_config(path: Path) -> dict:
    path = path.expanduser().absolute()
    if path.name != "config.json" or path.parent.name != ".seywo":
        raise ValueError("Seywo requires an explicit .seywo/config.json")
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Seywo configuration must not be a symlink")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (data.get("product") != "seywo" or data.get("schema_version") != 1
            or not isinstance(data.get("project_id"), str) or not data["project_id"]
            or data.get("database") != "index.sqlite3"
            or data.get("project_root") != str(path.parent.parent.resolve())):
        raise ValueError("invalid or foreign Seywo configuration")
    database = path.parent / data["database"]
    if database.is_symlink():
        raise ValueError("Seywo database must not be a symlink")
    return {**data, "_path": str(path), "_database": str(database)}


def enable_plugin_hook(config_path: Path) -> dict:
    """Enable the bundled lifecycle pipeline after explicit plugin setup."""
    path = config_path.expanduser().absolute()
    from .locking import file_lock
    with file_lock(path.parent / ".init.lock"):
        current = load_config(path)
        data = {k: v for k, v in current.items() if not k.startswith("_")}
        hook = data.get("hook") if isinstance(data.get("hook"), dict) else {}
        data["hook"] = {**hook, "enabled": True,
                        "pipeline": hook.get("pipeline") or "seywo-compact-v1"}
        # Existing explicitly configured providers retain their backend. New
        # plugin projects use the compact engine and the host's Codex model.
        if "model_runtime" not in data:
            data["model_runtime"] = {"backend": "codex"}
            data["hook"]["pipeline"] = "seywo-compact-v1"
        _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        return data["hook"]


def initialize(root: Path) -> dict:
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    state = state_dir(root)
    state.mkdir(mode=0o700, exist_ok=True)
    ensure_ignore(state)  # Must precede every configuration/database write.
    lock = state / ".init.lock"
    if lock.is_symlink():
        raise ValueError("initialization lock must not be a symlink")
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        return _initialize_locked(root, state)


def _initialize_locked(root: Path, state: Path) -> dict:
    path = state / "config.json"
    if not path.exists():
        data = {"product": "seywo", "schema_version": 1, "project_id": uuid4().hex,
                "project_root": str(root), "database": "index.sqlite3",
                "automatic_maintenance": False, "storage_v2": True, "hook": {"enabled": False}}
        # The project lock serializes creators; atomic publication prevents partial JSON.
        _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    config = load_config(path)
    from .store import Store
    with Store(config, create=True):
        pass
    return {"config": str(path), "project_id": config["project_id"],
            "database": config["_database"], "automatic_maintenance": False}
