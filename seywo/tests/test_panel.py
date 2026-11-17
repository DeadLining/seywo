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


def test_open_uninitialized_project_initializes_and_enables_hook(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('panel initialization must not call models')
    monkeypatch.setattr('seywo.codex_transport.CodexTransport.__init__', forbidden)
    monkeypatch.setattr('seywo.transport.Transport.__init__', forbidden)
    server = create_plugin_server(tmp_path)
    async def check():
        result = await server.call_tool('seywo_open', {})
        assert result.structuredContent.get('state') != 'initialization_required'
        assert result.structuredContent['initialized'] is True
        assert result.structuredContent['model_calls'] == 0
        assert result.structuredContent['project_root'] == str(tmp_path)
    asyncio.run(check())
    from seywo.config import load_config
    from seywo.store import Store
    config = load_config(tmp_path / '.seywo/config.json')
    assert config['hook'] == {'enabled': True, 'pipeline': 'seywo-compact-v1'}
    assert config['model_runtime'] == {'backend': 'codex'}
    assert (tmp_path / '.seywo/.gitignore').read_text() == '*\n!.gitignore\n'
    with Store(config, read_only=True) as store:
        assert store.db.execute('SELECT count(*) FROM chats').fetchone()[0] == 0


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


def test_open_existing_project_never_reinitializes_or_rewrites(tmp_path, monkeypatch):
    from seywo.config import initialize
    initialize(tmp_path)
    config = tmp_path / '.seywo/config.json'
    data = json.loads(config.read_text())
    data.update(model_runtime={'backend': 'responses', 'model': 'custom-model'},
                hook={'enabled': False, 'pipeline': 'manual-pipeline'}, manual={'keep': True})
    config.write_text(json.dumps(data))
    before = {p.name: p.read_bytes() for p in config.parent.iterdir() if p.is_file()}
    def forbidden(*args, **kwargs):
        pytest.fail('existing project must not initialize or enable hooks')
    monkeypatch.setattr('seywo.config.initialize', forbidden)
    monkeypatch.setattr('seywo.plugin_ui.initialize', forbidden)
    monkeypatch.setattr('seywo.config.enable_plugin_hook', forbidden)
    server = create_plugin_server(tmp_path)
    async def check():
        for _ in range(2):
            result = await server.call_tool('seywo_open', {})
            assert result.structuredContent['project_root'] == str(tmp_path)
            assert not result.structuredContent.get('initialized')
    asyncio.run(check())
    for name, content in before.items():
        assert (config.parent / name).read_bytes() == content


def test_open_initialization_failure_is_retryable_and_preserves_identity(tmp_path, monkeypatch):
    from seywo.store import Store
    original = Store.__init__
    def fail(*args, **kwargs):
        raise PermissionError('index database is read-only')
    server = create_plugin_server(tmp_path)
    monkeypatch.setattr(Store, '__init__', fail)
    async def check():
        result = await server.call_tool('seywo_open', {})
        info = result.structuredContent
        assert info['state'] == 'initialization_failed'
        assert info['retryable'] is True
        assert 'index database is read-only' in info['error']
        config = tmp_path / '.seywo/config.json'
        before = config.read_bytes()
        monkeypatch.setattr(Store, '__init__', original)
        result = await server.call_tool('seywo_open', {})
        assert result.structuredContent['initialized'] is True
        assert config.read_bytes() == before
        assert (config.parent / 'index.sqlite3').is_file()
    asyncio.run(check())


def test_open_preserves_preexisting_state_files_and_parallel_identity(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    state = tmp_path / '.seywo'
    state.mkdir()
    (state / '.gitignore').write_text('!shared-notes.md\n')
    (state / 'shared-notes.md').write_text('manual notes')
    def open_project(_):
        async def check():
            result = await create_plugin_server(tmp_path).call_tool('seywo_open', {})
            assert result.structuredContent.get('state') != 'initialization_failed'
        asyncio.run(check())
        return json.loads((state / 'config.json').read_text())['project_id']
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(open_project, range(4)))
    assert len(set(ids)) == 1
    assert (state / '.gitignore').read_text() == '!shared-notes.md\n'
    assert (state / 'shared-notes.md').read_text() == 'manual notes'


def test_first_open_preserves_orphan_database_instead_of_claiming_it(tmp_path):
    state = tmp_path / '.seywo'
    state.mkdir()
    database = state / 'index.sqlite3'
    database.write_bytes(b'')
    async def check():
        result = await create_plugin_server(tmp_path).call_tool('seywo_open', {})
        assert result.structuredContent['state'] == 'initialization_failed'
        assert 'config' in result.structuredContent['error']
    asyncio.run(check())
    assert database.read_bytes() == b''
    assert not (state / 'config.json').exists()


def test_open_explicit_chat_retains_admission_validation_error(tmp_path, monkeypatch):
    from mcp.server.fastmcp.exceptions import ToolError
    from seywo.config import initialize
    initialize(tmp_path)
    from seywo.admission import NonHumanSource
    def refuse(*args):
        raise NonHumanSource('nonhuman_subagent_source')
    monkeypatch.setattr('seywo.store.Store.require_human_chat', refuse)
    server = create_plugin_server(tmp_path)
    async def check():
        with pytest.raises(ToolError, match='nonhuman_subagent_source'):
            await server.call_tool('seywo_open', {'chat_id': 'internal-chat'})
    asyncio.run(check())
