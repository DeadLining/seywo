"""Real Git worktree regressions for project identity and Compact continuity."""
import asyncio
import io
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from seywo import hook, inbox, maintain
from seywo.config import initialize, load_config
from seywo.plugin import create_plugin_server
from seywo.store import Store
from test_compact_maintain import CompactTransport, make_source
from test_plugin_context import CHAT, context
from test_s2 import message


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], capture_output=True, check=True)


@pytest.fixture
def checkouts(tmp_path):
    main = tmp_path / 'main checkout'
    main.mkdir()
    git(main, 'init')
    git(main, '-c', 'user.name=Seywo Test', '-c', 'user.email=test@example.invalid',
        '-c', 'commit.gpgSign=false', 'commit', '--allow-empty', '-m', 'Fixture')
    linked = tmp_path / 'linked checkout'
    git(main, 'worktree', 'add', '--detach', str(linked))
    return main, linked


def precompact(monkeypatch, cwd, source, chat=CHAT):
    payload = {'hook_event_name': 'PreCompact', 'session_id': chat,
               'cwd': str(cwd), 'transcript_path': str(source)}
    started = []
    with monkeypatch.context() as patch:
        patch.setattr('sys.stdin', io.StringIO(json.dumps(payload)))
        patch.setattr(hook, 'spawn', lambda config, identity: started.append((config, identity)) or True)
        assert hook.main() == 0
    return started


def test_moved_chat_keeps_panel_and_next_compact_on_the_published_mainline(checkouts, tmp_path, monkeypatch):
    main, linked = checkouts
    config_path = Path(initialize(main, plugin_hook=True)['config'])
    config = load_config(config_path)
    transport = CompactTransport()
    monkeypatch.setattr(maintain, 'Transport', lambda *args, **kwargs: transport)
    monkeypatch.setattr(maintain, 'read_provider', lambda config=None: {
        'endpoint': 'http://127.0.0.1:1/responses', 'model': transport.model,
        'timeout': 5, 'max_output_tokens': 1200, '_api_key': 'fixture',
    })
    source = make_source(main, CHAT)
    assert precompact(monkeypatch, main, source) == [(config_path, CHAT)]
    assert maintain.run_chat(config, CHAT)['version'] == 1

    home = tmp_path / 'codex'
    home.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(home))
    with sqlite3.connect(home / 'state_5.sqlite') as catalog:
        catalog.execute('CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT)')
        catalog.execute('INSERT INTO threads VALUES (?,?)', (CHAT, str(main)))
    loaded_server = create_plugin_server()
    monkeypatch.setattr(loaded_server, 'get_context', lambda: context(thread_id=CHAT))
    assert asyncio.run(loaded_server.call_tool('seywo_open', {})).structuredContent['chat_id'] == CHAT
    with sqlite3.connect(home / 'state_5.sqlite') as catalog:
        catalog.execute('UPDATE threads SET cwd=? WHERE id=?', (str(linked), CHAT))

    # Recreate the stale, empty local index that an old panel could initialize.
    stale_config = Path(initialize(linked, plugin_hook=True)['config'])
    stale_bytes = stale_config.read_bytes()
    stale_db = stale_config.with_name('index.sqlite3')
    stale_index = stale_db.read_bytes()
    with source.open('a', encoding='utf-8') as handle:
        for row in ({'type': 'turn_context', 'payload': {'turn_id': 'turn-3', 'cwd': str(linked)}},
                    message('Continue the investigation'), message('Checked the new evidence', 'assistant')):
            handle.write(json.dumps(row) + '\n')

    # The Hook must be independent of both a fresh panel and the host's catalog.
    assert precompact(monkeypatch, linked, source) == [(config_path, CHAT)]
    assert maintain.run_chat(config, CHAT)['version'] == 2
    assert precompact(monkeypatch, linked, source) == [(config_path, CHAT)]
    assert maintain.run_chat(config, CHAT)['calls'] == 0
    assert [len(payload['turns']) for payload in transport.payloads] == [2, 1]
    for server in (loaded_server, create_plugin_server()):
        monkeypatch.setattr(server, 'get_context', lambda: context(thread_id=CHAT))
        opened = asyncio.run(server.call_tool('seywo_open', {})).structuredContent
        assert opened['project_root'] == str(main)
        assert opened['chat_id'] == CHAT
        page = asyncio.run(server.call_tool('seywo_panel_data', {'view': 'maintenance'}))
        assert json.loads(page[0].text)['published_version'] == 2
    assert stale_config.read_bytes() == stale_bytes
    assert stale_db.read_bytes() == stale_index
    assert not inbox.pending(load_config(stale_config))
    with Store(config, read_only=True) as store:
        assert store.db.execute('SELECT count(*) FROM chats').fetchone()[0] == 1
        assert store.db.execute('SELECT version FROM narrative_heads WHERE chat_id=?', (CHAT,)).fetchone()[0] == 2


def test_chat_born_in_linked_worktree_is_admitted_only_to_its_project(checkouts, tmp_path, monkeypatch):
    main, linked = checkouts
    config_path = Path(initialize(main, plugin_hook=True)['config'])
    source = make_source(linked, CHAT)
    assert precompact(monkeypatch, linked, source) == [(config_path, CHAT)]
    assert not (linked / '.seywo').exists()
    config = load_config(config_path)
    snapshot = Path(inbox.pending(config, CHAT)[0]['snapshot'])
    with Store(config) as store:
        assert store.import_source(snapshot, CHAT, 0)['chat_id'] == CHAT
    foreign = load_config(Path(initialize(tmp_path / 'unrelated')['config']))
    with Store(foreign) as store:
        with pytest.raises(ValueError, match='source cwd differs'):
            store.import_source(snapshot, CHAT, 0)
        assert store.db.execute('SELECT count(*) FROM chats').fetchone()[0] == 0


def test_worktree_respects_owner_opt_out_and_never_initializes_from_hook(checkouts, monkeypatch):
    main, linked = checkouts
    source = make_source(linked, CHAT)
    assert precompact(monkeypatch, linked, source) == []
    assert not (main / '.seywo').exists() and not (linked / '.seywo').exists()
    config = load_config(Path(initialize(main)['config']))
    stale = load_config(Path(initialize(linked, plugin_hook=True)['config']))
    assert precompact(monkeypatch, linked, source) == []
    assert not inbox.pending(config) and not inbox.pending(stale)


def test_nested_repo_and_copied_gitfile_do_not_inherit_other_project(checkouts, tmp_path):
    main, linked = checkouts
    initialize(main, plugin_hook=True)
    nested = linked / 'independent'
    nested.mkdir()
    git(nested, 'init')
    detached = tmp_path / 'copied'
    detached.mkdir()
    (detached / '.git').write_bytes((linked / '.git').read_bytes())
    separate = linked / 'separate metadata'
    separate.mkdir()
    git(separate, 'init', '--separate-git-dir', str(tmp_path / 'separate.git'))
    for root in (nested, detached, separate):
        assert hook.find_config(str(root)) is None
        opened = asyncio.run(create_plugin_server(root).call_tool('seywo_open', {})).structuredContent
        assert opened['project_root'] == str(root)


def test_first_open_from_linked_subdirectory_initializes_only_the_owner(checkouts):
    main, linked = checkouts
    child = linked / 'new branch directory'
    child.mkdir()
    opened = asyncio.run(create_plugin_server(child).call_tool('seywo_open', {})).structuredContent
    assert opened['project_root'] == str(main)
    assert opened['initialized'] is True
    assert opened['model_calls'] == 0
    assert hook.find_config(str(child)) == main / '.seywo/config.json'
    assert not (child / '.seywo').exists() and not (linked / '.seywo').exists()


def test_explicit_nested_scope_still_owns_its_own_data(checkouts):
    main, linked = checkouts
    initialize(main, plugin_hook=True)
    nested = linked / 'independent scope'
    config_path = Path(initialize(nested, plugin_hook=True)['config'])
    opened = asyncio.run(create_plugin_server(nested).call_tool('seywo_open', {})).structuredContent
    assert opened['project_root'] == str(nested)
    assert hook.find_config(str(nested)) == config_path
    with Store(load_config(main / '.seywo/config.json')) as store:
        with pytest.raises(ValueError, match='source cwd differs'):
            store.import_source(make_source(nested, CHAT), CHAT, 0)
