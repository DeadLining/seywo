"""Read-only plugin UI data and real version-difference contracts."""
import asyncio
import copy
import json

import pytest

from test_s2 import setup as setup
from test_s3 import publish, amend
from seywo.panel import Panel, compare_documents
from seywo.plugin import create_plugin_server
from seywo.retrieval import snapshot


def test_published_panel_is_paged_and_readonly(setup):
    store, _, _, service, _, _, view = publish(setup)
    before = store.db.total_changes
    panel = Panel(store)
    with snapshot(store):
        chats = panel.chats()
        assert chats['results'][0]['chat_id'] == 'chat'
        assert chats['results'][0]['version'] == 1
        page = panel.narrative('chat', limit=1)
        assert page['version'] == 1
        assert len(page['results']) == 1
        assert page['results'][0]['id'] == view['narrative']['items'][-1]['id']
        assert page['order'] == 'newest_created_first'
        assert page['phases'][0]['title'] == view['narrative']['phases'][0]['title']
        assert page['model_calls'] == 0
    assert store.db.total_changes == before
    with pytest.raises(ValueError):
        panel.narrative('missing')
    with pytest.raises(ValueError):
        panel.narrative('chat', version=999)


def test_compare_uses_persisted_versions_and_does_not_invent_merges(setup):
    store, _, _, service, _, _, view = publish(setup)
    item = view['narrative']['items'][0]
    updated = amend(service, view, 'item', item['id'], {'title': '已确认新的标题'})
    changes = Panel(store).changes('chat', version=updated['version'])
    assert changes['before_version'] == 1
    assert changes['after_version'] == 2
    assert changes['counts']['updated'] == 1
    assert changes['counts']['added'] == 0
    assert changes['counts']['merged'] == 0
    assert changes['results'][0]['before']['title'] == item['title']
    assert changes['results'][0]['after']['title'] == '已确认新的标题'


def test_diff_separates_added_completed_hidden_and_explicit_merge():
    base = {'items': [{'id': 'a', 'title': '等待', 'status': 'open', 'hidden': False},
                      {'id': 'b', 'title': '重复项', 'hidden': False},
                      {'id': 'c', 'title': '暂存', 'hidden': False}], 'placements': [],
            'phases': [], 'branches': [], 'identity_changes': [], 'overview': {}}
    after = copy.deepcopy(base)
    after['items'][0].update(title='已完成', status='resolved')
    after['items'][1]['hidden'] = True
    after['items'][2]['hidden'] = True
    after['items'].append({'id': 'd', 'title': '新节点', 'hidden': False})
    after['identity_changes'] = [{'kind': 'item_merge', 'from_ids': ['b'], 'to_ids': ['a']}]
    diff = compare_documents(base, after)
    assert diff['counts'] == {'added': 1, 'updated': 1, 'completed': 1, 'merged': 1, 'hidden': 1, 'removed': 0}
    assert diff['before_count'] == 3
    assert diff['after_count'] == 2
    assert next(row for row in diff['results'] if row['item_id'] == 'b')['operation'] == 'merged'
    assert next(row for row in diff['results'] if row['item_id'] == 'c')['operation'] == 'hidden'
    assert compare_documents(after, after)['counts']['merged'] == 0


def test_plugin_advertises_self_contained_app_and_tiny_render_result(setup):
    store, *_ = setup
    server = create_plugin_server(__import__('pathlib').Path(store.config['project_root']))
    async def check():
        tools = {t.name: t for t in await server.list_tools()}
        meta = tools['seywo_open'] .meta
        assert meta['ui']['resourceUri'].startswith('ui://')
        assert meta['openai/ui']['entrypoints'] == [{'type': 'thread'}]
        assert tools['seywo_open'].title == 'Seywo'
        assert tools['seywo_panel_data'].meta['ui']['visibility'] == ['app']
        resources = await server.read_resource(meta['ui']['resourceUri'])
        assert resources[0].mime_type == 'text/html;profile=mcp-app'
        assert 'Seywo' in resources[0].content
        assert '<script src="http' not in resources[0].content
        assert resources[0].meta['ui']['csp']['connectDomains'] == []
        result = await server.call_tool('seywo_open', {})
        assert len(result.content[0].text.encode()) < 1000
        assert result.structuredContent['model_calls'] == 0
        data = await server.call_tool('seywo_panel_data', {'view': 'chats'})
        assert json.loads(data[0].text)['model_calls'] == 0
    asyncio.run(check())


def test_panel_defaults_to_focused_branch_and_can_return_to_mainline(setup):
    def branch(out):
        out['branches'] = [{'phase_index': 0, 'title': '连接排查', 'goal': '确认连接',
                            'resolve_when': '实测成功', 'state': 'active',
                            'outcome_item_indexes': [], 'evidence_item_indexes': []}]
        out['placements'][0]['branch_index'] = 0
        out['overview']['focus']['branch_index'] = 0
    store, *_, view = publish(setup, branch)
    panel = Panel(store)
    focused = panel.narrative('chat')
    branch_id = view['narrative']['branches'][0]['id']
    phase_id = view['narrative']['phases'][0]['id']
    assert focused['selected_branch'] == branch_id
    assert len(focused['results']) == 1
    assert focused['results'][0]['id'] == view['narrative']['items'][0]['id']
    main = panel.narrative('chat', phase_id=phase_id)
    assert main['selected_branch'] is None
    assert len(main['results']) == 1


def test_open_uninitialized_project_returns_setup_state_without_writes(tmp_path):
    server = create_plugin_server(tmp_path)
    async def check():
        result = await server.call_tool('seywo_open', {})
        assert result.structuredContent['state'] == 'initialization_required'
        assert result.structuredContent['project_root'] == str(tmp_path)
    asyncio.run(check())
    assert not (tmp_path / '.seywo').exists()


def test_open_unbound_project_is_renderable_without_backend_error(tmp_path):
    from mcp.server.fastmcp import FastMCP
    from seywo.plugin_binding import ProjectRequired
    from seywo.plugin_ui import register_panel
    async def missing(_):
        raise ProjectRequired('unbound')
    server = FastMCP('test')
    register_panel(server, missing)
    async def check():
        result = await server.call_tool('seywo_open', {})
        assert result.structuredContent['state'] == 'project_required'
        assert result.structuredContent['model_calls'] == 0
    asyncio.run(check())
    assert not (tmp_path / '.seywo').exists()


def test_mainline_projection_prioritizes_overview_without_stage_records(setup):
    store, _, _, _, _, _, view = publish(setup)
    panel = Panel(store)
    before = store.db.total_changes
    data = panel.read('mainline', chat_id='chat')
    doc = view['narrative']
    assert data['version'] == view['version']
    assert 'results' not in data
    assert 'node_count' not in data
    assert [p['id'] for p in data['phases']] == [p['id'] for p in doc['phases']]
    assert all(set(p) <= {'id', 'title', 'state'} for p in data['phases'])
    assert [i['id'] for i in data['goals']] == doc['overview']['goal_item_ids']
    assert [i['id'] for i in data['open_questions']] == doc['overview']['open_question_ids']
    assert [i['id'] for i in data['next_steps']] == doc['overview']['next_step_ids']
    assert data['model_calls'] == 0
    assert store.db.total_changes == before
