"""Read Codex chat metadata for one project without importing chat contents."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .project_binding import inside_project, normalized_path


def list_project_chats(config: dict, codex_home: Path | None = None) -> list[dict]:
    home = codex_home or Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    index = home / 'state_5.sqlite'
    if not index.is_file():
        raise ValueError('Codex 聊天索引不存在')
    roots = [normalized_path(p) for p in config.get('hook', {}).get('allowed_transcript_roots', [])]
    if not roots:
        raise ValueError('尚未配置允许读取的聊天目录')
    with sqlite3.connect(index.resolve().as_uri() + '?mode=ro', uri=True) as conn:
        columns = {row[1] for row in conn.execute('PRAGMA table_info(threads)')}
        if 'thread_source' not in columns:
            raise ValueError('Codex 聊天索引缺少会话类型，无法区分内部记录')
        rows = conn.execute("SELECT id, title, updated_at, rollout_path, cwd FROM threads WHERE thread_source=? ORDER BY updated_at DESC", ('user',)).fetchall()
    results = []
    for chat_id, title, updated, transcript, cwd in rows:
        try:
            if not isinstance(chat_id, str) or not isinstance(transcript, str) or not inside_project(config, cwd):
                continue
            path = normalized_path(transcript)
            if not path.is_file() or not any(path.is_relative_to(root) for root in roots):
                continue
        except (OSError, TypeError, ValueError):
            continue
        results.append({'id': chat_id, 'title': title or '未命名聊天', 'updated_at': updated})
    return results
