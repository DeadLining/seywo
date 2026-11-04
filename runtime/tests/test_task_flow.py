import pytest
import json

import sqlite3
from sessiongraph.task_flow import validate_extraction, save_extraction, edit_node, edit_task


def test_nodes_require_actual_quotes_and_follow_source_order():
    entries = [{'id': 'a', 'text': '目标是生成流程图', 'timestamp': '1', 'role': 'user'},
               {'id': 'b', 'text': '确认采用时间线', 'timestamp': '2', 'role': 'user'}]
    payload = {'tasks': [{'title': '流程图', 'nodes': [
        {'kind': 'decision', 'label': '采用时间线', 'entry_id': 'b', 'quote': '确认采用时间线'},
        {'kind': 'goal', 'label': '生成流程图', 'entry_id': 'a', 'quote': '目标是生成流程图'},
    ]}]}
    result = validate_extraction(payload, entries)
    assert [n['entry_id'] for n in result[0]['nodes']] == ['a', 'b']
    payload['tasks'][0]['nodes'][0]['quote'] = '确认采用思维导图'
    with pytest.raises(ValueError, match='invalid_node_evidence'):
        validate_extraction(payload, entries)


def test_branch_requires_earlier_parent_and_source_quote():
    entries = [{'id':'a','role':'user','text':'讨论两种方案'}, {'id':'b','role':'user','text':'方案一先试用'}]
    data = {'tasks':[{'title':'任务','nodes':[
        {'kind':'problem','label':'讨论方案','entry_id':'a','quote':'讨论两种方案'},
        {'kind':'option','label':'先试用','entry_id':'b','quote':'方案一先试用','parent_entry_id':'a','branch_quote':'方案一先试用'}]}]}
    assert validate_extraction(data, entries)[0]['nodes'][1]['parent_entry_id'] == 'a'
    data['tasks'][0]['nodes'][1]['branch_quote'] = '不存在的依据'
    with pytest.raises(ValueError, match='invalid_branch_evidence'):
        validate_extraction(data, entries)


def test_save_is_idempotent_and_keeps_source():
    db = sqlite3.connect(':memory:')
    nodes = [{'title': '任务', 'nodes': [{'kind': 'decision', 'label': '使用时间线',
              'entry_id': 'e1', 'quote': '确认时间线', 'timestamp': '2026-01-01'}]}]
    assert save_extraction(db, 'project', 'chat', nodes) == 1
    assert save_extraction(db, 'project', 'chat', nodes) == 0
    assert db.execute('SELECT entry_id,quote FROM flow_nodes').fetchall() == [('e1', '确认时间线')]


def test_user_edit_and_deletion_survive_repeat_generation():
    db = sqlite3.connect(':memory:')
    tasks = [{'title':'任务', 'nodes':[{'kind':'goal','label':'初始','entry_id':'a', 'quote':'原文', 'timestamp':'1'}]}]
    save_extraction(db, 'p', 'chat', tasks)
    task, node = db.execute('SELECT task_id,id FROM flow_nodes').fetchone()
    edit_node(db, 'p', 'edit', task, node, '手动修改')
    save_extraction(db, 'p', 'chat', tasks)
    assert db.execute('SELECT label FROM flow_nodes').fetchone()[0] == '手动修改'
    edit_node(db, 'p', 'delete', task, node)
    save_extraction(db, 'p', 'chat', tasks)
    assert db.execute('SELECT hidden FROM flow_nodes').fetchone()[0] == 1
    edit_node(db, 'p', 'add', task, label='补充节点')
    assert db.execute('SELECT label FROM flow_nodes WHERE manual=1').fetchone()[0] == '补充节点'


def test_merge_and_split_survive_repeat_generation():
    db = sqlite3.connect(':memory:')
    a = [{'title':'甲', 'nodes':[{'kind':'goal','label':'一个','entry_id':'a', 'quote':'原文甲', 'timestamp':'1'}]}]
    b = [{'title':'乙', 'nodes':[{'kind':'goal','label':'两个','entry_id':'b', 'quote':'原文乙', 'timestamp':'2'}]}]
    save_extraction(db, 'p', 'chat', a)
    save_extraction(db, 'p', 'chat', b)
    first = db.execute("SELECT id FROM flow_tasks WHERE title='甲'").fetchone()[0]
    second = db.execute("SELECT id FROM flow_tasks WHERE title='乙'").fetchone()[0]
    edit_task(db, 'p', 'merge', first, target=second)
    save_extraction(db, 'p', 'chat', a)
    assert db.execute('SELECT count(*) FROM flow_nodes WHERE task_id=?', (second,)).fetchone()[0] == 2
    node = db.execute("SELECT id FROM flow_nodes WHERE entry_id='a'").fetchone()[0]
    edit_task(db, 'p', 'split', second, title='拆分', node=node)
    save_extraction(db, 'p', 'chat', a)
    assert db.execute('SELECT count(*) FROM flow_nodes').fetchone()[0] == 2
    assert db.execute("SELECT title FROM flow_tasks WHERE id=(SELECT task_id FROM flow_nodes WHERE id=?)", (node,)).fetchone()[0] == '拆分'


def test_generation_saves_successful_batch_before_later_failure(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from sessiongraph import flow_generation
    source = tmp_path / 'chat.jsonl'
    source.write_text('{}\n')
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('''CREATE TABLE entries(id TEXT,timestamp TEXT,role TEXT,text TEXT,session_id TEXT,line INTEGER,kind TEXT);
        CREATE TABLE session_chats(segment_id TEXT,chat_id TEXT);
        INSERT INTO session_chats VALUES('segment','chat');''')
    db.executemany('INSERT INTO entries VALUES(?,?,?,?,?,?,?)', [
        ('a', '1', 'user', 'a', 'segment', 1, 'message'),
        ('b', '2', 'user', 'b', 'segment', 2, 'message')])
    db.commit()
    store = SimpleNamespace(db=db, config={'project_id': 'project'})
    monkeypatch.setattr(flow_generation, 'current_transcript', lambda *args: source)
    monkeypatch.setattr(flow_generation, 'refresh_chat', lambda *args: {})
    monkeypatch.setattr(flow_generation, 'summarize_import', lambda *args: ({}, None, []))
    monkeypatch.setattr(flow_generation, '_context', lambda *args: [])
    monkeypatch.setattr('sessiongraph.input_pipeline.projection', lambda store, row: ([(0, len(row['text']))], 'message'))
    def answer(config, batch, home):
        entry = batch[0]
        return {'tasks': [{'title': '任务', 'nodes': [{'kind': 'goal', 'label': '目标',
            'entry_id': entry['id'], 'quote': 'a' if entry['id'] == 'a' else 'wrong'}]}]}
    with pytest.raises(ValueError, match='invalid_node_evidence'):
        flow_generation.generate(store, 'chat', tmp_path, answer)
    assert db.execute('SELECT count(*) FROM flow_nodes').fetchone()[0] == 1
    assert db.execute('SELECT last_rowid FROM flow_progress').fetchone()[0] == 1
    assert db.execute('SELECT state FROM flow_run_state').fetchone()[0] == 'partial'


def test_generate_imports_only_selected_chat_and_saves_evidence(tmp_path, monkeypatch):
    from sessiongraph.core import Store, init_project, load_config
    from sessiongraph.flow_generation import generate
    from test_mvp import write_session
    root = tmp_path / 'project'
    root.mkdir()
    home = tmp_path / 'codex'
    logs = home / 'sessions'
    logs.mkdir(parents=True)
    source = write_session(logs / 'rollout-chat-A.jsonl', root, 'chat-A', [('user', '确认采用时间线')])
    write_session(logs / 'rollout-chat-B.jsonl', root, 'chat-B', [('user', '不应被读取')])
    with sqlite3.connect(home / 'state_5.sqlite') as db:
        db.execute('CREATE TABLE threads(id TEXT,rollout_path TEXT,cwd TEXT)')
        db.execute('INSERT INTO threads VALUES(?,?,?)', ('chat-A', str(source), str(root)))
    monkeypatch.setenv('CODEX_HOME', str(home))
    cfg = load_config(init_project(root))
    cfg['hook']['allowed_transcript_roots'] = [str(logs)]
    store = Store(cfg)
    from sessiongraph.continuity import refresh_chat
    diagnostic = refresh_chat(store, 'chat-A', str(source))
    assert diagnostic['added_entries'] == 1, json.dumps(diagnostic, ensure_ascii=True)
    def answer(config, batch, codex_home):
        assert len(batch) == 1
        return {'tasks': [{'title': '流程图', 'nodes': [{'kind': 'decision', 'label': '采用时间线',
                  'entry_id': batch[0]['id'], 'quote': '确认采用时间线'}]}]}
    try:
        assert generate(store, 'chat-A', home, answer)['saved_nodes'] == 1
        assert generate(store, 'chat-A', home, answer)['saved_nodes'] == 0
        assert store.db.execute('SELECT count(*) FROM entries').fetchone()[0] == 1
        assert store.db.execute('SELECT quote FROM flow_nodes').fetchone()[0] == '确认采用时间线'
    finally:
        store.close()


def test_responses_request_and_evidence_validation(monkeypatch, tmp_path):
    from contextlib import contextmanager
    from sessiongraph.flow_generation import request_tasks
    from unittest.mock import patch
    @contextmanager
    def credentials(config, path):
        monkeypatch.setenv('TEST_FLOW_KEY', 'synthetic')
        yield
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size):
            output = {'tasks':[{'title':'任务','nodes':[{'kind':'goal','label':'目标','entry_id':'e1','quote':'目标'}]}]}
            return json.dumps({'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(output, ensure_ascii=False)}]}]}, ensure_ascii=False).encode()
    class Opener:
        def open(self, req, timeout):
            body = json.loads(req.data)
            assert body['store'] is False
            assert json.loads(body['input'])['untrusted_transcript'][0]['id'] == 'e1'
            return Response()
    config = {'provider':{'endpoint':'http://127.0.0.1:4444/v1/responses','model':'fixture','api_key_env':'TEST_FLOW_KEY'}}
    entries = [{'id':'e1','role':'user','text':'目标','timestamp':'1'}]
    with patch('sessiongraph.flow_generation.gateway_credentials', credentials), patch('sessiongraph.flow_generation.request.build_opener', return_value=Opener()):
        assert validate_extraction(request_tasks(config, entries, tmp_path), entries)[0]['nodes'][0]['quote'] == '目标'


@pytest.mark.parametrize(('response', 'message'), [
    ({'status':'incomplete','incomplete_details':{'reason':'max_output_tokens'}}, '模型输出达到长度上限'),
    ({'status':'incomplete','incomplete_details':{'reason':'content_filter'}}, '模型服务的内容过滤'),
    ({'status':'incomplete','incomplete_details':{'reason':'sensitive-provider-text'}}, '服务未提供可识别的原因'),
    ({'status':'failed'}, '模型服务报告本次请求失败'),
    ({'status':'completed','error':{'message':'sensitive-provider-text'}}, '模型服务返回错误'),
    ({'status':'cancelled'}, '模型服务取消了本次请求'),
    ({'status':'in_progress'}, '模型服务返回了尚未完成的状态'),
    ({'status':'sensitive-provider-text'}, '模型服务返回了未知或缺失的状态'),
    ([], '模型响应格式无效'),
])
def test_response_status_diagnostics_never_echo_provider_text(response, message):
    from sessiongraph.flow_generation import check_response_status
    with pytest.raises(ValueError, match=message) as error:
        check_response_status(response)
    assert 'sensitive-provider-text' not in str(error.value)


def test_completed_response_status_is_accepted():
    from sessiongraph.flow_generation import check_response_status
    assert check_response_status({'status':'completed','error':None}) is None


def test_complete_generation_path_with_simulated_provider(tmp_path, monkeypatch):
    from sessiongraph.core import Store, init_project, load_config
    from sessiongraph.flow_generation import generate
    from test_mvp import write_session
    root = tmp_path / 'project'; root.mkdir()
    home = tmp_path / 'codex'; logs = home / 'sessions'; logs.mkdir(parents=True)
    source = write_session(logs / 'rollout-chat-C.jsonl', root, 'chat-C', [('user', '确定使用时间线')])
    with sqlite3.connect(home / 'state_5.sqlite') as db:
        db.execute('CREATE TABLE threads(id TEXT,rollout_path TEXT,cwd TEXT)')
        db.execute('INSERT INTO threads VALUES(?,?,?)', ('chat-C', str(source), str(root)))
    monkeypatch.setenv('CODEX_HOME', str(home))
    monkeypatch.setenv('TEST_FLOW_KEY', 'synthetic')
    class Response:
        def __init__(self, payload): self.payload = payload
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size): return self.payload
    class Opener:
        def open(self, req, timeout):
            entries = json.loads(json.loads(req.data)['input'])['untrusted_transcript']
            result = {'tasks':[{'title':'流程图','nodes':[{'kind':'decision','label':'采用时间线',
                'entry_id':entries[0]['id'],'quote':'确定使用时间线'}]}]}
            return Response(json.dumps({'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(result,ensure_ascii=False)}]}]},ensure_ascii=False).encode())
    monkeypatch.setattr('sessiongraph.flow_generation.request.build_opener', lambda *args: Opener())
    cfg = load_config(init_project(root))
    cfg['hook']['allowed_transcript_roots'] = [str(logs)]
    cfg['provider'].update(protocol='responses',endpoint='http://127.0.0.1:4444/v1/responses',model='fixture',api_key_env='TEST_FLOW_KEY')
    store = Store(cfg)
    try:
        assert generate(store, 'chat-C', home)['saved_nodes'] == 1
        assert generate(store, 'chat-C', home)['batches'] == 0
        assert store.db.execute('SELECT quote FROM flow_nodes').fetchone()[0] == '确定使用时间线'
        from sessiongraph.call_logs import read as read_logs
        logs = read_logs(project=cfg['project_id'])
        assert len(logs['items']) == 1 and logs['items'][0]['state'] == 'validated'
        assert '确定使用时间线' not in json.dumps(logs, ensure_ascii=False)
        assert 'synthetic' not in json.dumps(logs, ensure_ascii=False)
    finally:
        store.close()
