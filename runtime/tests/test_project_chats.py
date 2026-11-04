import sqlite3

from sessiongraph.project_chats import list_project_chats


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
    import pytest
    home = tmp_path / 'codex'
    home.mkdir()
    with sqlite3.connect(home / 'state_5.sqlite') as db:
        db.execute('CREATE TABLE threads(id TEXT,title TEXT,updated_at INTEGER,rollout_path TEXT,cwd TEXT)')
    with pytest.raises(ValueError, match='缺少会话类型'):
        list_project_chats({'roots': [str(tmp_path)], 'hook': {'allowed_transcript_roots': [str(home)]}}, home)
