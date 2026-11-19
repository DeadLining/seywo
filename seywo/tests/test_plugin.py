"""Plugin bootstrap behavior; setup must never run merely on connection."""
import asyncio
import json
from pathlib import Path

import pytest

from seywo.config import initialize
from seywo.plugin import create_plugin_server


def call(server, name, **kwargs):
    async def run():
        result = await server.call_tool(name, kwargs)
        return json.loads(result[0].text)
    return asyncio.run(run())


def test_startup_and_listing_do_not_create_project_data(tmp_path):
    server = create_plugin_server(tmp_path)
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert 'seywo_initialize_project' in names
    assert 'seywo_search' in names
    assert len(names) == 19
    assert not (tmp_path / '.seywo').exists()


def test_explicit_setup_is_idempotent_and_local(tmp_path):
    root = tmp_path / 'selected project'
    root.mkdir()
    other = tmp_path / 'other'
    other.mkdir()
    server = create_plugin_server(root)
    first = call(server, 'seywo_initialize_project')
    second = call(server, 'seywo_initialize_project')
    assert first['project_id'] == second['project_id']
    assert Path(first['config']) == root / '.seywo/config.json'
    assert (root / '.seywo/.gitignore').read_text() == '*\n!.gitignore\n'
    assert not (other / '.seywo').exists()
    assert call(server, 'seywo_status')['model_calls'] == 0


def test_existing_project_status_works_without_mutating_config(tmp_path):
    initialize(tmp_path)
    config = tmp_path / '.seywo/config.json'
    before = config.read_bytes()
    result = call(create_plugin_server(tmp_path), 'seywo_status')
    assert result['model_calls'] == 0
    assert config.read_bytes() == before


def test_invalid_config_is_rejected_without_replacing_it(tmp_path):
    state = tmp_path / '.seywo'
    state.mkdir()
    config = state / 'config.json'
    config.write_text('{broken')
    with pytest.raises(ValueError):
        create_plugin_server(tmp_path)
    assert config.read_text() == '{broken'


def test_symlink_state_is_rejected(tmp_path):
    root = tmp_path / 'selected'
    other = tmp_path / 'other'
    root.mkdir()
    other.mkdir()
    (root / '.seywo').symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError):
        create_plugin_server(root)
    assert list(other.iterdir()) == []


def test_installed_copy_stdio_preserves_workspace_and_serves_history(tmp_path):
    """A relocated package must work without the repository on sys.path."""
    import shutil
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from seywo.config import load_config
    from seywo.store import Store

    installed = tmp_path / 'plugin cache with spaces'
    shutil.copytree(Path(__file__).resolve().parents[1], installed,
                    ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache', '.ruff_cache', '.venv'))
    workspace = tmp_path / 'selected workspace'
    workspace.mkdir()
    params = StdioServerParameters(command=sys.executable,
                                   args=[str(installed / 'bin/seywo-plugin'), '--project', str(workspace)], cwd=installed)

    async def check():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert len(tools.tools) == 19
                assert not (workspace / '.seywo').exists()
                setup = await session.call_tool('seywo_initialize_project', {})
                assert not setup.isError
                source = workspace / 'fixture.jsonl'
                source.write_text(json.dumps({'type': 'session_meta', 'payload': {
                    'id': 'plugin-chat', 'cwd': str(workspace)}}) + '\n' + json.dumps({
                    'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                    'content': [{'type': 'input_text', 'text': 'pluginsearchneedle'}]}}) + '\n')
                with Store(load_config(workspace / '.seywo/config.json')) as store:
                    store.import_source(source, 'plugin-chat', 0)
                result = await session.call_tool('seywo_search', {'query': 'pluginsearchneedle'})
                assert not result.isError
                data = json.loads(result.content[0].text)
                assert len(data['results']) == 1
                assert data['results'][0]['chat_id'] == 'plugin-chat'
                status = await session.call_tool('seywo_status', {})
                assert json.loads(status.content[0].text)['model_calls'] == 0
        assert not (installed / '.seywo').exists()

    asyncio.run(check())


def test_explicit_workspace_selection_without_host_roots(tmp_path):
    server = create_plugin_server()
    selected = call(server, 'seywo_select_project', project_root=str(tmp_path))
    assert selected['project_root'] == str(tmp_path.resolve())
    assert selected['initialized'] is False
    assert not (tmp_path / '.seywo').exists()
    call(server, 'seywo_initialize_project')
    assert call(server, 'seywo_status')['model_calls'] == 0


def test_selection_cannot_rebind_a_running_process(tmp_path):
    other = tmp_path / 'other'
    other.mkdir()
    server = create_plugin_server(tmp_path)
    async def check():
        from mcp.server.fastmcp.exceptions import ToolError
        with pytest.raises(ToolError, match='already bound'):
            await server.call_tool('seywo_select_project', {'project_root': str(other)})
    asyncio.run(check())
    assert not (other / '.seywo').exists()


def test_workspace_uri_validation(tmp_path):
    from types import SimpleNamespace
    from seywo.plugin import workspace_from_roots
    assert workspace_from_roots([SimpleNamespace(uri=tmp_path.as_uri())]) == tmp_path
    for roots in ([], [SimpleNamespace(uri='file:///a'), SimpleNamespace(uri='file:///b')],
                  [SimpleNamespace(uri='https://example.com/project')],
                  [SimpleNamespace(uri='file://foreign-host/project')]):
        with pytest.raises(ValueError):
            workspace_from_roots(roots)


def test_plugin_installation_cannot_be_selected_as_project():
    plugin_root = Path(__file__).resolve().parents[1]
    with pytest.raises(ValueError, match='outside the plugin installation'):
        create_plugin_server(plugin_root)


def test_plugin_setup_enables_its_bundled_compact_hook_on_existing_project(tmp_path):
    initialize(tmp_path)
    config = tmp_path / '.seywo/config.json'
    data = json.loads(config.read_text())
    data['hook'] = {'enabled': False, 'pipeline': 'seywo-delta-v1', 'model': 'existing-model'}
    config.write_text(json.dumps(data))
    result = call(create_plugin_server(tmp_path), 'seywo_initialize_project')
    assert result['hook'] == {'enabled': True, 'pipeline': 'seywo-compact-v1', 'model': 'existing-model'}
    saved = json.loads(config.read_text())
    assert saved['hook']['enabled'] is True
    assert saved['hook']['pipeline'] == 'seywo-compact-v1'
    assert saved['model_runtime'] == {'backend': 'codex'}


@pytest.mark.parametrize('kind', ['root', 'state', 'config', 'dangling_config', 'database'])
def test_open_rejects_symlinks_without_writing_targets(tmp_path, kind):
    root = tmp_path / 'project'
    outside = tmp_path / 'outside'
    root.mkdir()
    outside.mkdir()
    if kind == 'root':
        root.rmdir()
        root.symlink_to(outside, target_is_directory=True)
    elif kind == 'state':
        (root / '.seywo').symlink_to(outside, target_is_directory=True)
    else:
        state = root / '.seywo'
        state.mkdir()
        target = outside / 'target'
        if kind != 'dangling_config':
            target.write_text('preserve me')
        (state / ('index.sqlite3' if kind == 'database' else 'config.json')).symlink_to(target)
    before = {p: p.read_bytes() for p in outside.iterdir()}
    try:
        server = create_plugin_server(root)
    except ValueError as exc:
        assert 'symlink' in str(exc)
    else:
        async def check():
            result = await server.call_tool('seywo_open', {})
            assert result.structuredContent['state'] == 'initialization_failed'
            assert 'symlink' in result.structuredContent['error']
        asyncio.run(check())
    assert {p: p.read_bytes() for p in outside.iterdir()} == before
    if kind != 'root':
        assert not (root / '.seywo/config.json').is_file() or kind == 'config'


def test_initialize_rejects_project_and_dangling_config_symlinks(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        initialize(link)
    assert list(outside.iterdir()) == []
    state = tmp_path / '.seywo'
    state.mkdir()
    (state / 'config.json').symlink_to(outside / 'missing.json')
    with pytest.raises(ValueError, match='symlink'):
        initialize(tmp_path)
    assert (state / 'config.json').is_symlink()
    assert list(outside.iterdir()) == []
