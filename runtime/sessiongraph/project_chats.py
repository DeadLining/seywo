"""Read Codex chat metadata for one project without importing chat contents."""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from .project_binding import inside_project, normalized_path


_ATTACHMENT_TITLE = re.compile(r"^#{1,6}\s+Files mentioned by the user\s*:", re.I)
_REQUEST_HEADING = re.compile(r"(?:^|\s)#{1,6}\s+My request\s*:", re.I)
_AMBIENT_PREFIX = re.compile(r"^<in-app-browser-context\b[^>]*>.*?</in-app-browser-context>\s*", re.S)


def display_chat_title(title: str | None) -> str:
    """Project metadata for display only; never rewrite the Codex index or evidence."""
    text = (title or '').strip()
    text, ambient_count = _AMBIENT_PREFIX.subn('', text)
    text = text.strip()
    if ambient_count:
        text = re.sub(r'^#{1,6}\s+My request\s*:', '', text, count=1, flags=re.I).strip()
    if _ATTACHMENT_TITLE.match(text):
        request = _REQUEST_HEADING.search(text)
        if request is None:
            return '附件对话'
        text = text[request.end():].strip()
        if not text:
            return '附件对话'
    text = ' '.join(text.split())
    return text or '未命名聊天'


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
        results.append({'id': chat_id, 'title': display_chat_title(title), 'updated_at': updated})
    return results
