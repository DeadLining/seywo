"""Strict UTF-8 response budgets, loss-visible previews and recoverable paging."""
import asyncio
import json
import socket
from pathlib import Path

import pytest

from sessiongraph.core import Store, init_project, load_config
from sessiongraph.mcp_server import create_server
from test_mvp import write_session


def wire(result):
    return json.dumps(result, ensure_ascii=False, separators=(',', ':'))


@pytest.fixture
def memories(tmp_path):
    s = Store(load_config(init_project(tmp_path)))
    text = 'budgetX 结果：' + '中😀\\"\n' * 500 + '/tmp/budgetX.json'
    s.ingest(write_session(tmp_path / 'a.jsonl', tmp_path, 'chat', [('user', text)]))
    eid = s.search('budgetX', mode='raw')['hits'][0]['id']
    ids = []
    for i in range(8):
        ids.append(s.remember(kind='artifact', title=f'budgetX 报告{i}', content=text,
                   paths=['/tmp/budgetX.json'], source_entry_id=eid, source_quote=text)['id'])
    yield s, ids
    s.close()


def test_default_preview_is_small_and_has_source(memories):
    s, _ = memories
    result = s.recall('budgetX')
    assert result['view'] == 'brief'
    assert 0 < len(result['memories']) <= 3
    assert len(wire(result).encode()) <= 4096
    item = result['memories'][0]
    assert 'content' not in item and 'quote' not in item['source']
    assert len(item['summary']) <= 160 and item['summary_truncated']
    assert item['paths'] == ['/tmp/budgetX.json']
    assert s.expand(item['source']['entry_id'])['entries']


@pytest.mark.parametrize('budget', [1024, 1536, 4096, 8192])
def test_budget_covers_json_unicode_escaping_and_metadata(memories, budget):
    result = memories[0].recall('budgetX', limit=50, max_bytes=budget)
    size = len(wire(result).encode())
    assert size <= budget
    assert result['budget']['returned_bytes'] == size
    assert result['budget']['max_bytes'] == budget


def test_paging_does_not_skip_or_repeat_items(memories):
    s, expected = memories
    offset, seen = 0, []
    while True:
        result = s.recall('budgetX', limit=3, max_bytes=4096, offset=offset)
        seen.extend(m['id'] for m in result['memories'])
        nxt = result['retrieval']['next_offset']
        if nxt is None:
            break
        assert nxt > offset
        offset = nxt
    assert set(seen) == set(expected) and len(seen) == len(expected)


def test_full_view_requires_opt_in_and_honors_budget(memories):
    s, _ = memories
    small = s.recall('budgetX', view='full', limit=1, max_bytes=1024)
    assert small['memories'] == []
    assert small['retrieval']['next_offset'] == 0  # Not silently skipped.
    assert small['budget']['required_bytes'] > 1024
    full = s.recall('budgetX', view='full', limit=1,
                    max_bytes=small['budget']['required_bytes'])
    assert len(full['memories']) == 1
    assert full['memories'][0]['content'] == full['memories'][0]['source']['quote']
    assert full['budget']['returned_bytes'] <= full['budget']['max_bytes']


def test_brief_omits_long_paths_without_fabricating_shortened_paths(memories):
    s, ids = memories
    paths = ['/tmp/' + '长' * 200 + '.json', '/a.json', '/b.json', '/c.json']
    with s.db:
        s.db.execute('update work_memory set paths_json=? where id=?',
                     (json.dumps(paths), ids[-1]))
    result = s.recall('budgetX')
    item = next(m for m in result['memories'] if m['id'] == ids[-1])
    assert item['paths'] == ['/a.json', '/b.json']
    assert item['paths_omitted'] == 2


def test_no_results_and_long_query_still_fit_minimum(memories):
    result = memories[0].recall('陌' * 1000, max_bytes=1024, view='full')
    assert not result['memories']
    assert len(wire(result).encode()) <= 1024
    assert result['retrieval']['state'] == 'no_match'


@pytest.mark.parametrize('extra', [{'max_bytes': 1023}, {'max_bytes': True},
                                   {'offset': -1}, {'offset': True}, {'view': 'unknown'}])
def test_invalid_output_options(memories, extra):
    with pytest.raises(ValueError):
        memories[0].recall('budgetX', **extra)


def test_mcp_returns_one_compact_text_payload(memories):
    async def exercise():
        server = create_server(Path(memories[0].config['roots'][0]) / '.sessiongraph' / 'config.json')
        response = await server.call_tool('session_recall', {'query': 'budgetX', 'max_bytes': 1024})
        assert isinstance(response, list) and len(response) == 1
        text = response[0].text
        result = json.loads(text)
        assert len(text.encode()) <= 1024
        assert result['budget']['returned_bytes'] == len(text.encode())
        tools = {t.name: t for t in await server.list_tools()}
        assert tools['session_recall'].outputSchema is None
    # Use the actual config path rather than a fabricated server project.
    asyncio.run(exercise())


def test_no_network_or_database_writes(memories, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('network forbidden')
    monkeypatch.setattr(socket, 'socket', forbidden)
    s, _ = memories
    before = s.db.total_changes
    assert s.recall('budgetX')['model_calls'] == 0
    assert s.db.total_changes == before


def test_long_scope_does_not_break_minimum_budget(memories):
    result = memories[0].recall('budgetX', chat_id='中' * 256, max_bytes=1024)
    assert result['retrieval']['scope']['chat_filter_applied'] is True
    assert 'chat_id' not in result['retrieval']['scope']
    assert len(wire(result).encode()) <= 1024


def test_cli_output_obeys_same_budget(memories, capsys):
    from sessiongraph.cli import main
    config = Path(memories[0].config['roots'][0]) / '.sessiongraph' / 'config.json'
    assert main(['--config', str(config), 'recall', 'budgetX', '--max-bytes', '1024']) == 0
    text = capsys.readouterr().out.rstrip('\n')
    assert len(text.encode()) == json.loads(text)['budget']['returned_bytes'] <= 1024


def test_relations_are_light_and_full_evidence_is_recoverable(tmp_path):
    from test_work_relations import setup
    from test_memories import FakeExtractor
    from sessiongraph.memories import run_chat
    store, output = setup(tmp_path)
    try:
        run_chat(store, FakeExtractor([output]), 'chat-1')
        brief = store.recall('部署任务')
        assert brief['relations']
        edge = brief['relations'][0]
        assert 'evidence' not in edge and edge['source_entry_id']
        assert store.expand(edge['source_entry_id'])['entries']
        full = store.recall('部署任务', view='full', max_bytes=65536)
        assert full['relations'][0]['evidence'][0]['quote']
    finally:
        store.close()


def test_exhausted_page_and_wildcard_are_explicit(memories):
    s, _ = memories
    assert s.recall('*', offset=8)['retrieval']['next_offset'] is None
    assert s.recall('*', offset=8)['memories'] == []
    assert s.recall('*')['memories']
