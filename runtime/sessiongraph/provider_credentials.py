"""Explicit endpoint-bound local credentials. Never used by read operations."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from urllib.parse import urlsplit


def validate_endpoint(endpoint):
    if not isinstance(endpoint, str) or len(endpoint) > 2048 or any(ord(c) < 33 for c in endpoint):
        raise ValueError("invalid_endpoint")
    url = urlsplit(endpoint)
    if (not url.hostname or url.username or url.password or url.query or url.fragment
            or not url.path.endswith("/responses")
            or not (url.scheme == "https" or (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}))):
        raise ValueError("invalid_endpoint")
    return endpoint


def runtime_state():
    return Path(os.environ.get("SESSIONGRAPH_RUNTIME_HOME", str(Path.home() / "Library/Application Support/local-ai-gateway/sessiongraph")))


def _name(endpoint):
    return "provider-" + hashlib.sha256(endpoint.encode()).hexdigest() + ".json"


def save_key(state, endpoint, key):
    validate_endpoint(endpoint)
    if not isinstance(key, str) or not key.strip() or len(key) > 2048 or any(ord(c) < 32 for c in key):
        raise ValueError("invalid_api_key")
    directory = state / "credentials"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".provider-" + next(tempfile._get_candidate_names())
    try:
        meta = os.fstat(fd)
        if meta.st_uid != os.getuid() or stat.S_IMODE(meta.st_mode) != 0o700:
            raise ValueError("unsafe_credentials")
        target = _name(endpoint)
        try:
            existing = os.stat(target, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.getuid():
                raise ValueError("unsafe_credentials")
        except FileNotFoundError:
            pass
        out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(out, "w") as stream:
            json.dump({"endpoint": endpoint, "api_key": key.strip()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(temporary, target, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass
        os.close(fd)


def load_key(state, endpoint):
    try:
        fd = os.open(state / "credentials", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        meta = os.fstat(fd)
        if meta.st_uid != os.getuid() or stat.S_IMODE(meta.st_mode) != 0o700:
            raise ValueError("unsafe_credentials")
        try:
            keyfd = os.open(_name(endpoint), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            return None
        with os.fdopen(keyfd, "r") as stream:
            meta = os.fstat(stream.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or stat.S_IMODE(meta.st_mode) != 0o600 or meta.st_size > 8192:
                raise ValueError("unsafe_credentials")
            data = json.load(stream)
            if data.get("endpoint") != endpoint:
                raise ValueError("credential_endpoint_mismatch")
            return data["api_key"]
    finally:
        os.close(fd)
