import json
import os
import pytest
from sessiongraph.core import Store, init_project, load_config
from sessiongraph.continuity import refresh_chat
from test_mvp import write_session


def test_refresh_discovers_only_selected_chat_and_project(tmp_path):
    root = tmp_path/'project'; root.mkdir()
    logs = tmp_path/'logs'; logs.mkdir()
    cfg = load_config(init_project(root))
    cfg['hook']['allowed_transcript_roots'] = [str(logs)]
    store = Store(cfg)
    good = write_session(logs/'rollout-chat-A.jsonl', root, 'chat-A', [('user', '需要保留工作记忆')])
    write_session(logs/'rollout-chat-B.jsonl', root, 'chat-B', [('user', '不可导入')])
    write_session(logs/'wrong-chat-A.jsonl', root, 'chat-C', [('user', '名称相似但不属于A')])
    first = refresh_chat(store, 'chat-A')
    assert first['added_entries'] == 1
    assert first['model_calls'] == 0
    assert store.db.execute('SELECT count(*) FROM entries').fetchone()[0] == 1
    assert refresh_chat(store, 'chat-A')['added_entries'] == 0
    write_session(logs/'continued-chat-A.jsonl', root, 'chat-A', [('assistant', '已形成报告')])
    assert refresh_chat(store, 'chat-A')['added_entries'] == 1
    assert store.status()['source_segments'] == 2
    assert len(first['imports']) == 1
    assert first['skipped']  # identity mismatch is visible
    assert not store.db.execute('SELECT 1 FROM jobs').fetchone()  # no extraction jobs
    store.close()


def test_refresh_requires_allowlist_and_safe_chat_id(tmp_path):
    store = Store(load_config(init_project(tmp_path)))
    with pytest.raises(ValueError, match='chat_id'):
        refresh_chat(store, '../../*')
    with pytest.raises(ValueError, match='allowlist'):
        refresh_chat(store, 'chat-A')
    store.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows extended paths')
def test_refresh_accepts_extended_windows_path_within_allowlist(tmp_path):
    root = tmp_path / 'project'; root.mkdir()
    logs = tmp_path / 'logs'; logs.mkdir()
    source = write_session(logs / 'rollout-chat-A.jsonl', root, 'chat-A', [('user', 'local')])
    outside = write_session(tmp_path / 'outside.jsonl', root, 'chat-A', [('user', 'outside')])
    cfg = load_config(init_project(root))
    cfg['hook']['allowed_transcript_roots'] = [str(logs)]
    store = Store(cfg)
    try:
        result = refresh_chat(store, 'chat-A', '\\\\?\\' + str(source))
        assert result['added_entries'] == 1
        assert not result['skipped']
        rejected = refresh_chat(store, 'chat-A', '\\\\?\\' + str(outside))
        assert rejected['added_entries'] == 0
        assert rejected['skipped'][0]['code'] == 'outside_allowlist'
    finally:
        store.close()


def test_refresh_rejects_other_project_and_symlink_escape(tmp_path):
    root = tmp_path/'root'; root.mkdir()
    logs = tmp_path/'logs'; logs.mkdir()
    outside = write_session(tmp_path/'outside.jsonl', root, 'chat-A', [('user','outside')])
    (logs/'linked-chat-A.jsonl').symlink_to(outside)
    write_session(logs/'wrong-chat-A.jsonl', tmp_path/'other', 'chat-A', [('user','other project')])
    cfg = load_config(init_project(root)); cfg['hook']['allowed_transcript_roots']=[str(logs)]
    store = Store(cfg)
    result = refresh_chat(store, 'chat-A')
    assert result['added_entries'] == 0
    assert len(result['skipped']) == 2
    store.close()
