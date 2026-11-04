import json
import sqlite3
import time
from pathlib import Path

import pytest

from sessiongraph import flow_generation as flow
from sessiongraph.core import Store, init_project, load_config
from test_mvp import write_session


def make_store(tmp_path, monkeypatch, messages):
    root = tmp_path / 'project'
    root.mkdir()
    home = tmp_path / 'codex'
    logs = home / 'sessions'
    logs.mkdir(parents=True)
    source = write_session(logs / 'rollout-chat-A.jsonl', root, 'chat-A', messages)
    with sqlite3.connect(home / 'state_5.sqlite') as db:
        db.execute('CREATE TABLE threads(id TEXT,rollout_path TEXT,cwd TEXT)')
        db.execute('INSERT INTO threads VALUES(?,?,?)', ('chat-A', str(source), str(root)))
    monkeypatch.setenv('CODEX_HOME', str(home))
    config = load_config(init_project(root))
    config['hook']['allowed_transcript_roots'] = [str(logs)]
    return Store(config), home, source


def test_confirmation_receives_context_but_not_as_new_evidence(tmp_path, monkeypatch):
    store, home, source = make_store(tmp_path, monkeypatch, [
        ('assistant', '建议选择时间线作为展示方式。'), ('user', '确认'),
        ('assistant', '好的'), ('assistant', '这一段是普通闲聊。'), ('assistant', '测试完成，发现错误并已修复。'),
    ])
    with source.open('a', encoding='utf8') as out:
        out.write(json.dumps({'type':'response_item','timestamp':'2026-10-01T00:00:00Z',
            'payload':{'type':'function_call','name':'exec_command','arguments':'very-long-tool-data','call_id':'call1'}})+'\n')
    calls = []
    def model(config, batch, codex_home):
        calls.append(batch)
        item = batch[-1]
        if item['text'] == '确认':
            assert any('时间线' in prev['text'] and prev['context_only'] for prev in batch[:-1])
            return {'tasks':[{'title':'展示方式','nodes':[{'kind':'decision','label':'采用时间线',
                'entry_id':item['id'],'quote':'确认'}]}]}
        return {'tasks':[]}
    try:
        result = flow.generate(store, 'chat-A', home, model)
        assert result['complete'] is True
        assert any('已修复' in batch[-1]['text'] for batch in calls)
        assert not any('very-long-tool-data' in item['text'] or item['text'] in ('好的','这一段是普通闲聊。') for batch in calls for item in batch)
        assert store.db.execute('SELECT filtered_assistant FROM flow_run_state').fetchone()[0] == 1
        assert store.db.execute('SELECT quote FROM flow_nodes').fetchone()[0] == '确认'
    finally:
        store.close()


def test_output_limit_splits_only_failed_fragment_and_keeps_order():
    calls = []
    def model(config, batch, home):
        text = batch[-1]['text']; calls.append(text)
        if len(text) > 700:
            raise flow.OutputLimit('模型输出达到长度上限，结果未完成')
        return {'tasks':[]}
    entry = {'id':'e1','role':'user','text':'甲' * 1100,'source_rowid':1}
    budget = {'calls':0,'deadline':time.monotonic()+30}
    assert flow._extract_fragment({},entry,[],Path('.'),model,budget) == []
    assert len(calls) == 3
    assert calls[0] == entry['text']
    assert all(len(part) < len(entry['text']) for part in calls[1:])


def test_unrecoverable_single_fragment_has_bounded_calls():
    def model(*args): raise flow.OutputLimit('模型输出达到长度上限，结果未完成')
    budget = {'calls':0,'deadline':time.monotonic()+30}
    with pytest.raises(ValueError, match='拆分后仍超过'):
        flow._extract_fragment({}, {'id':'e','role':'user','text':'甲'*500}, [], Path('.'), model, budget)
    assert budget['calls'] == 1


def test_context_only_message_cannot_be_used_as_node_evidence():
    from sessiongraph.task_flow import validate_extraction
    entries = [{'id':'context','role':'assistant','text':'采用方案一','context_only':True},
               {'id':'current','role':'user','text':'确认'}]
    payload = {'tasks':[{'title':'任务','nodes':[{'kind':'option','label':'方案一',
                'entry_id':'context','quote':'采用方案一'}]}]}
    with pytest.raises(ValueError, match='context_only_evidence'):
        validate_extraction(payload, entries)


def test_a_fully_split_request_can_finish_within_one_run_budget():
    calls = []
    def model(config, batch, home):
        text = batch[-1]['text']; calls.append(text)
        if len(text) > 1400: raise flow.OutputLimit('模型输出达到长度上限，结果未完成')
        return {'tasks':[]}
    budget = {'calls':0,'deadline':time.monotonic()+30}
    assert flow._extract_fragment({}, {'id':'e','role':'user','text':'甲'*4500}, [], Path('.'), model, budget) == []
    assert 1 < budget['calls'] <= flow.MAX_CALLS


def test_partial_run_resumes_without_reprocessing_saved_entries(tmp_path, monkeypatch):
    store, home, _ = make_store(tmp_path, monkeypatch, [('user','第一个需求'),('user','第二个需求')])
    monkeypatch.setattr(flow, 'MAX_CALLS', 1)
    calls = []
    def model(config, batch, codex_home):
        item = batch[-1]; calls.append(item['text'])
        return {'tasks':[{'title':'任务','nodes':[{'kind':'goal','label':item['text'],
                   'entry_id':item['id'],'quote':item['text']}]}]}
    try:
        assert flow.generate(store, 'chat-A', home, model)['complete'] is False
        assert store.db.execute('SELECT state FROM flow_run_state').fetchone()[0] == 'partial'
        assert flow.generate(store, 'chat-A', home, model)['complete'] is True
        assert calls == ['第一个需求','第二个需求']
        assert store.db.execute('SELECT count(*) FROM flow_nodes').fetchone()[0] == 2
    finally:
        store.close()


def test_read_chat_list_exposes_partial_and_complete_status(tmp_path, monkeypatch):
    from sessiongraph.core import digest
    from sessiongraph.native_bridge import read
    store, home, source = make_store(tmp_path, monkeypatch, [('user','需求一'),('user','需求二')])
    with sqlite3.connect(home / 'state_5.sqlite') as db:
        db.execute('ALTER TABLE threads ADD COLUMN title TEXT')
        db.execute('ALTER TABLE threads ADD COLUMN updated_at INTEGER')
        db.execute('ALTER TABLE threads ADD COLUMN thread_source TEXT')
        db.execute("UPDATE threads SET title='测试会话',updated_at=1,thread_source='user'")
    registry = tmp_path / 'projects.json'
    registry.write_text(json.dumps({'projects':[{'root':store.config['roots'][0]}]}), encoding='utf8')
    config_path = Path(store.config['_config_path'])
    raw = json.loads(config_path.read_text(encoding='utf8'))
    raw['hook']['allowed_transcript_roots'] = [str(home / 'sessions')]
    config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding='utf8')
    project_id = digest(str(Path(store.config['roots'][0]).resolve()))[:20]
    monkeypatch.setattr(flow, 'MAX_CALLS', 1)
    try:
        result = flow.generate(store, 'chat-A', home, lambda *_: {'tasks':[]})
        assert not result['complete']
        assert read(registry, {'operation':'project_chats','project':project_id})['chats'][0]['flow_state'] == 'partial'
        assert flow.generate(store, 'chat-A', home, lambda *_: {'tasks':[]})['complete']
        assert read(registry, {'operation':'project_chats','project':project_id})['chats'][0]['flow_state'] == 'complete'
    finally:
        store.close()


def test_long_single_message_resumes_at_fragment_checkpoint(tmp_path, monkeypatch):
    text = ''.join(str(i % 10) for i in range(10000))
    store, home, _ = make_store(tmp_path, monkeypatch, [('user',text)])
    monkeypatch.setattr(flow, 'MAX_CALLS', 1)
    received = []
    def model(config, batch, codex_home):
        received.append(batch[-1]['text'])
        return {'tasks':[]}
    try:
        for _ in range(4):
            result = flow.generate(store, 'chat-A', home, model)
            if result['complete']: break
        assert result['complete'] is True
        assert all(len(part) <= flow.FRAGMENT_CHARS for part in received)
        assert sum(map(len,received)) >= len(text)
        assert store.db.execute('SELECT count(*) FROM flow_fragment_progress').fetchone()[0] == 0
    finally:
        store.close()
