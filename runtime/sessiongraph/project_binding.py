"""Read-only local thread identity checks. Never infer parent/side-chat identity."""
import os
import sqlite3
from pathlib import Path


def inside_project(config, cwd):
    if not isinstance(cwd, str) or not cwd:
        return False
    path = Path(cwd).expanduser().resolve()
    return any(path.is_relative_to(Path(root).resolve()) for root in config['roots'])


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
            path = Path(row[0]).expanduser().resolve()
            allowlist = config.get('hook', {}).get('allowed_transcript_roots', [])
            if any(path.is_relative_to(Path(root).expanduser().resolve()) for root in allowlist):
                return path
    except (OSError, sqlite3.Error, TypeError, ValueError):
        pass  # Never bypass project checks when local identity cannot be read.
    return None


def verify_moved_source(config, chat, path, *, previously_bound=False):
    current = current_transcript(config, chat)
    allowed = any(path.is_relative_to(Path(root).expanduser().resolve())
                  for root in config.get('hook', {}).get('allowed_transcript_roots', []))
    if not allowed or current is None or (current != path and not previously_bound):
        raise ValueError('session cwd is outside configured project roots')
