"""Exact host-thread binding; no latest-chat guesses or history import."""
import sqlite3
from types import SimpleNamespace

from seywo.plugin_context import host_thread_id, host_workspace

CHAT = '01a1a5f6-da58-73a2-871e-dbf6a9d210b4'


def context(**meta):
    return SimpleNamespace(request_context=SimpleNamespace(meta=SimpleNamespace(**meta)))


def test_host_thread_identity_is_request_scoped():
    assert host_thread_id(context(thread_id=CHAT, threadId=CHAT)) == CHAT
    assert host_thread_id(context(thread_id=CHAT, threadId='other')) is None
    assert host_thread_id(context(thread_id='../../other')) is None
    assert host_thread_id(None) is None


def test_exact_host_lookup_reads_only_the_given_thread(tmp_path):
    home = tmp_path / 'codex'
    home.mkdir()
    project = tmp_path / 'project'
    project.mkdir()
    path = home / 'state_5.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT)')
        db.execute('INSERT INTO threads VALUES (?,?)', (CHAT, str(project)))
        db.execute('INSERT INTO threads VALUES (?,?)', ('unrelated', '/secret/other'))
    before = path.read_bytes()
    assert host_workspace(context(thread_id=CHAT), home=home) == project
    assert host_workspace(context(thread_id='00000000-0000-0000-0000-000000000001'), home=home) is None
    assert path.read_bytes() == before
    assert not (project / '.seywo').exists()


def test_missing_catalog_is_not_created(tmp_path):
    assert host_workspace(context(thread_id=CHAT), home=tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_catalog_relative_path_is_rejected(tmp_path):
    with sqlite3.connect(tmp_path / 'state_5.sqlite') as db:
        db.execute('CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT)')
        db.execute('INSERT INTO threads VALUES (?,?)', (CHAT, 'relative'))
    assert host_workspace(context(thread_id=CHAT), home=tmp_path) is None


def test_real_stdio_request_automatically_binds_project_and_chat(tmp_path):
    import asyncio
    import json
    import os
    from pathlib import Path
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from seywo.config import initialize, load_config
    from seywo.store import Store

    home = tmp_path / 'codex'
    home.mkdir()
    project = tmp_path / 'project'
    project.mkdir()
    initialize(project)
    source = project / 'fixture.jsonl'
    source.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': CHAT, 'cwd': str(project)}})
                      + '\n' + json.dumps({'type': 'response_item', 'payload': {'type': 'message',
                        'role': 'user', 'content': [{'type': 'input_text', 'text': 'context fixture'}]}}) + '\n')
    with Store(load_config(project / '.seywo/config.json')) as store:
        store.import_source(source, CHAT, 0)
    other = tmp_path / 'other'
    other.mkdir()
    other_chat = '00000000-0000-0000-0000-000000000002'
    with sqlite3.connect(home / 'state_5.sqlite') as db:
        db.execute('CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT)')
        db.executemany('INSERT INTO threads VALUES (?,?)', [(CHAT, str(project)), (other_chat, str(other))])
    plugin = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(command=sys.executable, args=[str(plugin / 'bin/seywo-plugin')],
                                   cwd=plugin, env={**os.environ, 'CODEX_HOME': str(home)})

    async def check():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool('seywo_open', {}, meta={'thread_id': CHAT, 'threadId': CHAT})
                assert not result.isError
                assert result.structuredContent['project_root'] == str(project)
                assert result.structuredContent['chat_id'] == CHAT
                page = await session.call_tool('seywo_panel_data', {'view': 'chats'}, meta={'thread_id': CHAT})
                assert json.loads(page.content[0].text)['default_chat_id'] == CHAT
                # A process cannot silently leak its selected project into another Chat.
                result = await session.call_tool('seywo_open', {}, meta={'thread_id': other_chat})
                assert result.structuredContent['state'] == 'project_required'
                assert result.structuredContent['project_root'] is None
    asyncio.run(check())
    assert not (other / '.seywo').exists()


def test_active_catalog_does_not_fall_back_to_stale_identity(tmp_path):
    for version in (4, 5):
        with sqlite3.connect(tmp_path / f'state_{version}.sqlite') as db:
            db.execute('CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT)')
            if version == 4:
                db.execute('INSERT INTO threads VALUES (?,?)', (CHAT, str(tmp_path)))
    assert host_workspace(context(thread_id=CHAT), home=tmp_path) is None


def test_host_catalog_location_uses_plugin_data_when_codex_home_is_filtered(tmp_path, monkeypatch):
    from seywo.plugin_context import catalog_home
    monkeypatch.delenv('CODEX_HOME', raising=False)
    monkeypatch.setenv('PLUGIN_DATA', str(tmp_path / 'plugins/data/agent-plugins/test-plugin'))
    assert catalog_home() == tmp_path


def test_explicit_codex_home_takes_priority_and_foreign_plugin_layout_is_ignored(tmp_path, monkeypatch):
    from pathlib import Path
    from seywo.plugin_context import catalog_home
    monkeypatch.setenv('PLUGIN_DATA', '/arbitrary/foreign/plugin')
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    assert catalog_home() == tmp_path
    monkeypatch.delenv('CODEX_HOME')
    assert catalog_home() == Path.home() / '.codex'


def test_compatible_cached_plugin_locates_exact_codex_home(tmp_path, monkeypatch):
    from seywo.plugin_context import catalog_home
    monkeypatch.delenv('CODEX_HOME', raising=False)
    monkeypatch.delenv('PLUGIN_DATA', raising=False)
    root = tmp_path / '.codex/plugins/cache/seywo-local/seywo/0.4.0'
    root.mkdir(parents=True)
    monkeypatch.setenv('PLUGIN_ROOT', str(root))
    assert catalog_home() == tmp_path / '.codex'
