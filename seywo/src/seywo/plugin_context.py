"""Codex request identity -> exact, read-only workspace catalog lookup.

The desktop attaches thread_id/threadId to MCP calls. It does not include the
workspace in MCP Apps hostContext. Never infer identity from recency, process
cwd, another project's Seywo state, or an inherited CODEX_THREAD_ID.
"""
from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3
from uuid import UUID


def host_thread_id(context):
    try:
        meta = context.request_context.meta
    except (AttributeError, ValueError):
        return None
    values = [getattr(meta, key, None) for key in ('thread_id', 'threadId')]
    values = [value for value in values if value is not None]
    if not values or any(value != values[0] for value in values):
        return None
    value = values[0]
    if not isinstance(value, str):
        return None
    try:
        return value if str(UUID(value)) == value else None
    except ValueError:
        return None


def catalog_home():
    """Locate this host's catalog even when Codex filters CODEX_HOME from MCP.

    PLUGIN_DATA is supplied by Codex and retains the configured installation
    root. Only the verified agent-plugins layout is recognized; no search.
    """
    if value := os.environ.get('CODEX_HOME'):
        return Path(value)
    data = Path(os.environ.get('PLUGIN_DATA', ''))
    if (data.is_absolute() and len(data.parents) >= 4
            and [parent.name for parent in list(data.parents)[:3]] == ['agent-plugins', 'data', 'plugins']):
        return data.parents[3]
    # Codex's compatible plugin loader resolves cwd but does not inject the
    # Agent Plugins environment. The entrypoint supplies its own exact root.
    root = Path(os.environ.get('PLUGIN_ROOT', ''))
    if (root.is_absolute() and len(root.parents) >= 5
            and root.parents[2].name == 'cache' and root.parents[3].name == 'plugins'):
        return root.parents[4]
    return Path.home() / '.codex'


def host_workspace(context, *, home=None):
    thread = host_thread_id(context)
    if thread is None:
        return None
    directory = Path(home) if home is not None else catalog_home()
    # Schema version, not modification time. Never fall back to an older catalog
    # when the active schema exists but lacks this exact thread.
    catalogs = [(int(match[1]), path) for path in directory.glob('state_*.sqlite')
                if (match := re.fullmatch(r'state_(\d+)\.sqlite', path.name))]
    if not catalogs:
        return None
    path = max(catalogs, key=lambda item: item[0])[1]
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
            db.execute('PRAGMA query_only=ON')
            row = db.execute('SELECT cwd FROM threads WHERE id=?', (thread,)).fetchone()
    except (sqlite3.Error, OSError):
        return None
    if row is None or not isinstance(row[0], str):
        return None
    root = Path(row[0])
    return root if root.is_absolute() and root.is_dir() else None


def current_chat(store, context):
    """Physical host identity is a Chat only after exact local admission checks."""
    identity = host_thread_id(context)
    if identity is None or not store.db.execute('SELECT 1 FROM chats WHERE id=?', (identity,)).fetchone():
        return identity, None
    from .admission import NonHumanSource
    try:
        store.require_human_chat(identity)
    except NonHumanSource:
        return identity, None
    return identity, identity
