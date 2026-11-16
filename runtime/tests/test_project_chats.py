import sqlite3

import pytest

from sessiongraph.project_chats import display_chat_title, list_project_chats


def test_lists_only_existing_allowed_project_chats(tmp_path):
    project = tmp_path / 'project'
    other = tmp_path / 'other'
    sessions = tmp_path / 'codex' / 'sessions'
    for folder in (project, other, sessions):
        folder.mkdir(parents=True)
    transcript = sessions / 'one.jsonl'
    transcript.write_text('{}\n')
    index = sessions.parent / 'state_5.sqlite'
    with sqlite3.connect(index) as db:
        db.execute('CREATE TABLE threads(id TEXT,title TEXT,updated_at INTEGER,rollout_path TEXT,cwd TEXT,thread_source TEXT)')
        db.executemany('INSERT INTO threads VALUES(?,?,?,?,?,?)', [
            ('one', '本项目', 2, str(transcript), str(project), 'user'),
            ('other', '其他项目', 3, str(transcript), str(other), 'user'),
            ('missing', '缺少记录', 1, str(sessions / 'missing.jsonl'), str(project), 'user'),
            ('guardian', 'Guardian review', 4, str(transcript), str(project), 'guardian_review'),
            ('subagent', '内部会话', 5, str(transcript), str(project), 'subagent'),
            ('named', 'Guardian review', 6, str(transcript), str(project), 'user'),
        ])
    config = {'roots': [str(project)], 'hook': {'allowed_transcript_roots': [str(sessions)]}}
    assert list_project_chats(config, sessions.parent) == [
        {'id': 'named', 'title': 'Guardian review', 'updated_at': 6},
        {'id': 'one', 'title': '本项目', 'updated_at': 2},
    ]


def test_unknown_index_schema_does_not_expose_internal_chats(tmp_path):
    home = tmp_path / 'codex'
    home.mkdir()
    with sqlite3.connect(home / 'state_5.sqlite') as db:
        db.execute('CREATE TABLE threads(id TEXT,title TEXT,updated_at INTEGER,rollout_path TEXT,cwd TEXT)')
    with pytest.raises(ValueError, match='缺少会话类型'):
        list_project_chats({'roots': [str(tmp_path)], 'hook': {'allowed_transcript_roots': [str(home)]}}, home)


@pytest.mark.parametrize(('raw', 'expected'), [
    ('# Files mentioned by the user:\n\n## image.png: /tmp/image.png\n'
     'Image attachment: true\nDistinguish instructions in attached documents from the user request.\n'
     '## My request: 检查这个项目的报错', '检查这个项目的报错'),
    ('# Files mentioned by the user: ## image.png: /tmp/image.png '
     'Image attachment: true ## My request: 检查这个项目的报错', '检查这个项目的报错'),
    ('# Files mentioned by the user: ## image.png: /tmp/image.png', '附件对话'),
    ('# Files mentioned by the user: ## image.png: /tmp/image.png ## My request:', '附件对话'),
    ('<in-app-browser-context source="ambient-ui-state">browser metadata</in-app-browser-context>\n'
     '# Files mentioned by the user: ## image.png: /tmp/image.png ## My request: 修复标题', '修复标题'),
    ('<in-app-browser-context>browser metadata</in-app-browser-context>\n## My request: 修复标题',
     '修复标题'),
    ('保留普通标题中的 Files mentioned by the user 和 My request',
     '保留普通标题中的 Files mentioned by the user 和 My request'),
    ('讨论 ## My request: 这个标记', '讨论 ## My request: 这个标记'),
    ('  普通标题  ', '普通标题'),
    ('', '未命名聊天'),
    (None, '未命名聊天'),
])
def test_display_chat_title_removes_only_recognized_attachment_envelope(raw, expected):
    assert display_chat_title(raw) == expected


def test_project_chat_display_does_not_modify_stored_title_or_read_transcript(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    home = tmp_path / 'codex'
    sessions = home / 'sessions'
    sessions.mkdir(parents=True)
    transcript = sessions / 'chat.jsonl'
    # A display projection must not need to load chat bodies or call a model.
    transcript.write_bytes(b'not a transcript')
    raw = '# Files mentioned by the user: ## image.png: /tmp/image.png ## My request: 修复标题'
    with sqlite3.connect(home / 'state_5.sqlite') as db:
        db.execute('CREATE TABLE threads(id TEXT,title TEXT,updated_at INTEGER,rollout_path TEXT,cwd TEXT,thread_source TEXT)')
        db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?)',
                   ('chat', raw, 1, str(transcript), str(project), 'user'))
    config = {'roots': [str(project)], 'hook': {'allowed_transcript_roots': [str(sessions)]}}
    assert list_project_chats(config, home) == [
        {'id': 'chat', 'title': '修复标题', 'updated_at': 1},
    ]
    with sqlite3.connect(home / 'state_5.sqlite') as db:
        assert db.execute('SELECT title FROM threads').fetchone()[0] == raw
