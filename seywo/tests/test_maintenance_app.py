"""App-only retry, exact chat binding and preservation of published history."""
import asyncio
import json
from pathlib import Path

import pytest

from seywo import maintenance_status as status
from seywo.config import initialize, load_config
from seywo.plugin import create_plugin_server
from seywo.store import Store


def call(server, name, **args):
    return json.loads(asyncio.run(server.call_tool(name, args))[0].text)


@pytest.fixture
def project(tmp_path):
    result = initialize(tmp_path, plugin_hook=True)
    config = load_config(Path(result['config']))
    source = tmp_path / 'source.jsonl'
    source.write_text(json.dumps({'type':'session_meta', 'payload':{'id':'chat','cwd':str(tmp_path)}})+'\n'+
        json.dumps({'type':'response_item','payload':{'type':'message','role':'user',
        'content':[{'type':'input_text','text':'test'}]}})+'\n')
    with Store(config) as store:
        store.import_source(source, 'chat', 0)
    return config, create_plugin_server(tmp_path)


def test_maintenance_is_readonly_and_retry_is_app_only(project, monkeypatch):
    config, server = project
    def forbidden(*a, **k):
        pytest.fail('must not call model')
    monkeypatch.setattr('seywo.codex_transport.CodexTransport.__init__', forbidden)
    monkeypatch.setattr('seywo.transport.Transport.__init__', forbidden)
    tools = {t.name:t for t in asyncio.run(server.list_tools())}
    assert tools['seywo_retry_maintenance'].meta['ui']['visibility'] == ['app']
    assert not tools['seywo_retry_maintenance'].annotations.readOnlyHint
    status.record(config, 'chat', 'partial', error_code='call_budget')
    result = call(server, 'seywo_panel_data', view='maintenance', chat_id='chat')
    assert result['state'] == 'partial' and result['model_calls'] == 0
    launched = []
    monkeypatch.setattr('seywo.hook.spawn', lambda *a: launched.append(a) or True)
    result = call(server, 'seywo_retry_maintenance', chat_id='chat')
    assert result['accepted'] and result['maintenance']['state'] == 'queued'
    assert result['model_calls'] == 0 and len(launched) == 1
    assert not call(server, 'seywo_retry_maintenance', chat_id='chat')['accepted']


def test_foreign_or_path_chat_rejected(project):
    _, server = project
    for chat in ['foreign', '../outside']:
        with pytest.raises(Exception):
            call(server, 'seywo_panel_data', view='maintenance', chat_id=chat)
        with pytest.raises(Exception):
            call(server, 'seywo_retry_maintenance', chat_id=chat)


def test_unimported_exact_host_chat_can_read_and_retry_local_receipt(project, monkeypatch):
    config, server = project
    monkeypatch.setattr('seywo.plugin_ui.host_thread_id', lambda *_:'current-host')
    status.record(config, 'current-host', 'failed', error_code='worker_start_failed')
    result = call(server, 'seywo_panel_data', view='maintenance')
    assert result['chat_id'] == 'current-host' and result['state'] == 'failed'
    monkeypatch.setattr('seywo.hook.spawn', lambda *a: True)
    assert call(server, 'seywo_retry_maintenance')['accepted']


def test_failure_status_does_not_change_mainline(project):
    config, server = project
    before = call(server, 'seywo_panel_data', view='mainline', chat_id='chat')
    status.record(config, 'chat', 'failed', error_code='maintainer_exception')
    call(server, 'seywo_panel_data', view='maintenance', chat_id='chat')
    after = call(server, 'seywo_panel_data', view='mainline', chat_id='chat')
    assert after == before


def test_codex_public_status_includes_failure_without_running_model(project):
    config, server = project
    status.record(config, 'chat', 'failed', error_code='source_missing')
    result = call(server, 'seywo_status', chat_ids=['chat'])
    maintenance = result['results'][0]['maintenance']
    assert maintenance['state'] == 'failed' and maintenance['retryable']
    assert '原始对话' in maintenance['message'] and result['model_calls'] == 0


def test_upgrade_retry_uses_existing_compact_failure_ledger(project, monkeypatch):
    from seywo.compact_schema import setup
    config, server = project
    with Store(config) as store:
        setup(store.db)
        store.db.execute("INSERT INTO compact_batches (id,chat_id,cursor_before,cursor_after,base_version,state,turn_count,segment_count,model_calls,input_chars,output_chars,input_tokens,output_tokens,segments_json,changes_json,error_code,created_at,updated_at,model_runs_json) VALUES ('batch','chat','null','[0,2]',0,'failed',1,0,1,1,0,1,0,'[]','[]','context_ceiling',1,2,'[]')")
        store.db.commit()
    result = call(server, 'seywo_panel_data', view='maintenance', chat_id='chat')
    assert result['state'] == 'failed' and result['retryable']
    monkeypatch.setattr('seywo.hook.spawn', lambda *a: True)
    assert call(server, 'seywo_retry_maintenance', chat_id='chat')['accepted']
