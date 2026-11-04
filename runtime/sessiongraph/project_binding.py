"""Read-only local thread identity checks. Never infer parent/side-chat identity."""
import os
import sqlite3
from pathlib import Path


def normalized_path(value):
    raw = os.fspath(value)
    if os.name == 'nt' and raw.startswith('\\\\?\\'):
        suffix = raw[4:]
        if suffix.upper().startswith('UNC\\'):
            raw = '\\\\' + suffix[4:]
        elif len(suffix) >= 3 and suffix[0].isalpha() and suffix[1] == ':' and suffix[2] in '\\/':
            raw = suffix
        else:
            raise ValueError('unsupported Windows path')
    return Path(raw).expanduser().resolve()


def inside_project(config, cwd):
    if not isinstance(cwd, str) or not cwd:
        return False
    path = normalized_path(cwd)
    return any(path.is_relative_to(normalized_path(root)) for root in config['roots'])


def current_transcript(config, chat):
    # A path/identity hint, not permission to import old cross-project history.
    home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser()
    db = home / 'state_5.sqlite'
    if not db.is_file():
        return None
    try:
        conn = sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.05)
        try:
            row = conn.execute('SELECT rollout_path,cwd FROM threads WHERE id=?', (chat,)).fetchone()
        finally:
            conn.close()
        if row and inside_project(config, row[1]):
            path = normalized_path(row[0])
            allowlist = config.get('hook', {}).get('allowed_transcript_roots', [])
            if any(path.is_relative_to(normalized_path(root)) for root in allowlist):
                return path
    except (OSError, sqlite3.Error, TypeError, ValueError):
        pass  # Never bypass project checks when local identity cannot be read.
    return None


def verify_moved_source(config, chat, path, *, previously_bound=False):
    current = current_transcript(config, chat)
    path = normalized_path(path)
    allowed = any(path.is_relative_to(normalized_path(root))
                  for root in config.get('hook', {}).get('allowed_transcript_roots', []))
    if not allowed or current is None or (current != path and not previously_bound):
        raise ValueError('session cwd is outside configured project roots')
