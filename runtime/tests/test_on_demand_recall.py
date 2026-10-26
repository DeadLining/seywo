"""Agent-directed recall: bounded lexical evidence, explicit fallback, no provider."""
import asyncio
import socket

import pytest

from sessiongraph.core import Store, init_project, load_config
from sessiongraph.mcp_server import create_server
from test_mvp import write_session


@pytest.fixture
def saved(tmp_path):
    cfg = init_project(tmp_path)
    store = Store(load_config(cfg))
    text = 'step30 F1=0.913，报告 /tmp/step30/report.json；论文检索采用原文证据。'
    store.ingest(write_session(tmp_path / 'a.jsonl', tmp_path, 'chat-a', [('user', text)]))
    eid = store.search('step30', mode='raw')['hits'][0]['id']
    item = store.remember(kind='artifact', title='step30 评估报告', content=text,
                          paths=['/tmp/step30/report.json'], aliases=['三十步实验'],
                          source_entry_id=eid, source_quote=text)
    yield store, cfg, item
    store.close()


def test_recall_returns_explainable_match_and_expand_action(saved, monkeypatch):
    store, _, item = saved
    def forbidden(*args, **kwargs):
        raise AssertionError('retrieval must not use network')
    monkeypatch.setattr(socket, 'socket', forbidden)
    before = store.db.total_changes
    result = store.recall('step30', limit=1, view='full', max_bytes=65536)
    assert result['model_calls'] == 0
    assert result['memories'][0]['match']['terms'] == ['step30']
    assert result['retrieval']['state'] == 'matches'
    assert result['retrieval']['scope']['project_id'] == store.config['project_id']
    action = result['retrieval']['next_actions'][0]
    assert action['tool'] == 'session_expand'
    assert action['arguments']['object_id'] == item['source']['entry_id']
    assert store.expand(**action['arguments'])['entries']
    assert result['retrieval']['elapsed_ms'] >= 0
    assert store.db.total_changes == before


def test_no_memory_hit_offers_raw_search_not_extraction(saved):
    store, _, _ = saved
    result = store.recall('unindexed-experiment', chat_id='chat-a', view='full')
    assert result['memories'] == []
    action = result['retrieval']['next_actions'][0]
    assert action == {'tool': 'session_search', 'arguments': {
        'query': 'unindexed-experiment', 'mode': 'raw', 'chat_id': 'chat-a',
        'limit': 5, 'max_chars': 6000}}
    assert result['retrieval']['state'] == 'no_match'
    assert 'absence' in result['note']


def test_raw_only_history_is_recoverable_from_suggested_fallback(saved, tmp_path):
    store, _, _ = saved
    store.ingest(write_session(tmp_path / 'b.jsonl', tmp_path, 'chat-b', [
        ('user', 'paperZX 在 /tmp/paperZX.md，尚未提取成关键事项。')]))
    result = store.recall('paperZX', view='full')
    action = result['retrieval']['next_actions'][0]
    raw = store.search(**action['arguments'])
    assert raw['hits'] and raw['model_calls'] == 0
    assert store.expand(raw['hits'][0]['id'])['entries']


def test_identifiers_are_not_substring_matches(saved):
    store, _, _ = saved
    assert store.recall('step3')['memories'] == []
    assert store.recall('STEP30')['memories']


def test_scope_path_is_not_a_relevance_signal(saved):
    store, _, _ = saved
    assert store.recall('pytest')['memories'] == []


def test_alias_chinese_and_path_still_match(saved):
    store, _, item = saved
    for query in ('三十步实验', '/tmp/step30/report.json', '论文检索'):
        assert store.recall(query)['memories'][0]['id'] == item['id']


def test_chat_filter_and_supersession_remain_intact(saved):
    store, _, item = saved
    assert store.recall('step30', chat_id='other')['memories'] == []
    store.remember(kind='artifact', title='step30 新结果', content='step30 新结果',
                   supersedes_id=item['id'], source_entry_id=item['source']['entry_id'],
                   source_quote='step30')
    assert len(store.recall('step30')['memories']) == 1
    assert len(store.recall('step30', include_superseded=True)['memories']) == 2


def test_foreign_project_rows_are_not_returned(saved):
    store, _, item = saved
    with store.db:
        store.db.execute('UPDATE work_memory SET project_id=? WHERE id=?', ('foreign', item['id']))
    assert store.recall('step30')['memories'] == []


def test_only_selected_memories_load_sources_and_report_truncation(saved):
    store, _, item = saved
    for i in range(20):
        store.remember(kind='finding', title=f'step30 item {i}', content='step30',
                       source_entry_id=item['source']['entry_id'], source_quote='step30')
    queries = []
    store.db.set_trace_callback(queries.append)
    result = store.recall('step30', limit=2)
    store.db.set_trace_callback(None)
    assert result['retrieval']['matched_count'] == 21
    assert result['retrieval']['returned_count'] == 2
    assert result['retrieval']['truncated'] is True
    assert len([q for q in queries if 'SELECT e.*, c.chat_id FROM entries' in q]) == 2


@pytest.mark.parametrize('kwargs', [{'query': 'x' * 1001}, {'query': 'x', 'chat_id': ''},
                                   {'query': 'x', 'chat_id': 3}])
def test_invalid_query_scope_is_rejected(saved, kwargs):
    with pytest.raises(ValueError):
        saved[0].recall(**kwargs)


def test_legacy_database_offers_readonly_fallback(saved):
    store, _, _ = saved
    with store.db:
        store.db.execute('DROP TABLE work_memory')
    reader = Store(store.config, readonly=True)
    try:
        result = reader.recall('step30', view='full')
        assert result['available'] is False
        assert result['relations'] == []
        assert result['retrieval']['state'] == 'memory_unavailable'
        assert result['retrieval']['next_actions'][0]['tool'] == 'session_search'
        assert reader.db.total_changes == 0
    finally:
        reader.close()


def test_mcp_contract_explains_when_to_use_and_when_to_skip(saved):
    async def exercise():
        server = create_server(saved[1])
        tools = {t.name: t for t in await server.list_tools()}
        assert len(tools) == 7
        for name, tool in tools.items():
            if name != 'session_remember':
                assert tool.annotations.readOnlyHint is True
                assert tool.annotations.openWorldHint is False
        desc = tools['session_recall'].description
        assert 'current context' in desc and 'session_search' in desc
        assert 'keywords' in desc and 'session_expand' in desc
        assert 'not on every turn' in server.instructions
        response = await server.call_tool('session_recall', {'query': 'step30', 'limit': 1})
        assert 'returned_bytes' in response[0].text
    asyncio.run(exercise())
