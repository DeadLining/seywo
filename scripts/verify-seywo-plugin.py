#!/usr/bin/env python3
"""Real plugin install + host MCP acceptance, with isolated config and project."""
import argparse
from datetime import date
import json
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import tempfile
import time

REPOSITORY = Path(__file__).resolve().parent.parent


def verify(repository=REPOSITORY):
    with tempfile.TemporaryDirectory(prefix='seywo-plugin-acceptance-') as temporary:
        base = Path(temporary).resolve()
        codex_state = base / '.codex'
        workspace = base / 'project with spaces'
        codex_state.mkdir()
        workspace.mkdir()
        env = {**os.environ, 'CODEX_HOME': str(codex_state)}

        def cli(*args, cwd=workspace):
            result = subprocess.run(['codex', *args], env=env, cwd=cwd, capture_output=True,
                                    text=True, timeout=30)
            if result.returncode:
                raise RuntimeError(f'Codex {args[:2]} failed: {result.stderr[:500]}')
            return result.stdout

        cli('plugin', 'marketplace', 'add', str(repository), '--json')
        cli('plugin', 'add', 'seywo@seywo-local')
        servers = json.loads(cli('mcp', 'list', '--json'))
        server = next(item for item in servers if item['name'] == 'seywo')
        transport = server['transport']
        assert transport['command'] == 'sh' and transport['args'] == ['./bin/seywo-runtime', '--mcp']
        installed = Path(transport['cwd'])
        assert installed != repository / 'seywo'
        assert Path(transport['cwd']) == installed
        assert not (installed / '.seywo').exists()
        with (base / 'host.stderr').open('wb') as error_log:
            process = subprocess.Popen(['codex', 'app-server', '--stdio', '--enable', 'hooks'], env=env, cwd=workspace,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=error_log)
            buffered = b''
            next_id = 0
            notifications = []

            def request(method, params):
                nonlocal next_id, buffered
                next_id += 1
                identity = next_id
                process.stdin.write((json.dumps({'id': identity, 'method': method,
                                                'params': params}) + '\n').encode())
                process.stdin.flush()
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if b'\n' not in buffered:
                        if not select.select([process.stdout], [], [], 0.5)[0]:
                            continue
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            raise RuntimeError('Codex host closed the connection')
                        buffered += chunk
                    while b'\n' in buffered:
                        line, buffered = buffered.split(b'\n', 1)
                        response = json.loads(line)
                        if 'method' in response:
                            notifications.append(response)
                        if response.get('id') == identity:
                            if 'error' in response:
                                raise RuntimeError(str(response['error']))
                            return response['result']
                raise TimeoutError(method)

            try:
                host = request('initialize', {'clientInfo': {'name': 'seywo-plugin-acceptance',
                                                           'version': '1'},
                                              'capabilities': {'experimentalApi': True}})
                process.stdin.write(b'{"method":"initialized"}\n')
                process.stdin.flush()
                # Review and trust only this verifier's installed package in
                # its disposable CODEX_HOME; never modify the user's trust.
                discovered = request('hooks/list', {'cwds': [str(workspace)]})
                reviewed = [hook for entry in discovered['data'] for hook in entry['hooks']
                            if hook.get('pluginId') == 'seywo@seywo-local']
                assert len(reviewed) == 4
                for hook in reviewed:
                    request('config/value/write', {
                        'keyPath': 'hooks.state.' + json.dumps(hook['key']) + '.trusted_hash',
                        'value': hook['currentHash'], 'mergeStrategy': 'replace'})
                started = request('thread/start', {'cwd': str(workspace), 'ephemeral': True,
                                                   'sandbox': 'workspace-write', 'approvalPolicy': 'never'})
                thread = started['thread']['id']
                # No model turn is sent, so this new fixture Chat has no persisted
                # host catalog row yet. Seed metadata only in this temporary HOME;
                # native request identity and installed runtime remain real.
                catalog = max(codex_state.glob('state_*.sqlite'),
                              key=lambda path: int(path.stem.split('_')[-1]))
                with sqlite3.connect(catalog) as catalog_db:
                    catalog_db.execute('''INSERT OR IGNORE INTO threads
                        (id,rollout_path,created_at,updated_at,source,model_provider,cwd,title,sandbox_policy,approval_mode)
                        VALUES(?,?,?,?,?,?,?,?,?,?)''',
                        (thread, str(workspace / 'host-catalog-fixture.jsonl'), int(time.time()), int(time.time()),
                         'cli', 'fixture', str(workspace), 'Acceptance fixture',
                         '{"type":"workspace-write"}', 'never'))
                inventory = request('mcpServerStatus/list', {'threadId': thread})
                running = next(item for item in inventory['data'] if item['name'] == 'seywo')
                assert running['runtimeStatus'] == 'connected', running['runtimeStatus']
                assert len(running['tools']) == 16
                hook_inventory = request('hooks/list', {'cwds': [str(workspace)]})
                plugin_hooks = [hook for entry in hook_inventory['data'] for hook in entry['hooks']
                                if hook.get('pluginId') == 'seywo@seywo-local']
                assert len(plugin_hooks) == 4, hook_inventory
                assert {hook['eventName'] for hook in plugin_hooks} == {'preCompact', 'postCompact', 'sessionStart', 'userPromptSubmit'}
                assert all(hook['trustStatus'] == 'trusted' for hook in plugin_hooks), plugin_hooks
                assert all(not entry['errors'] for entry in hook_inventory['data']), hook_inventory
                assert running['tools']['seywo_model_status']['_meta']['ui']['visibility'] == ['app']
                assert running['tools']['seywo_model_save']['_meta']['ui']['visibility'] == ['app']
                assert running['tools']['seywo_model_test']['_meta']['ui']['visibility'] == ['app']
                assert not (workspace / '.seywo').exists(), 'startup wrote project data'

                def raw_tool(name, **arguments):
                    result = request('mcpServer/tool/call', {'threadId': thread, 'server': 'seywo',
                                                           'tool': name, 'arguments': arguments,
                                                           '_meta': {'thread_id': thread, 'threadId': thread}})
                    assert not result.get('isError'), result
                    return result

                def tool(name, **arguments):
                    result = raw_tool(name, **arguments)
                    return json.loads(result['content'][0]['text'])

                unbound_open = raw_tool('seywo_open')
                assert unbound_open['structuredContent']['state'] == 'initialization_required'
                assert unbound_open['structuredContent']['project_root'] == str(workspace)
                selected = tool('seywo_select_project', project_root=str(workspace))
                assert not selected['initialized']
                assert not (workspace / '.seywo').exists(), 'selection initialized data'
                uninitialized_open = raw_tool('seywo_open')
                assert uninitialized_open['structuredContent']['state'] == 'initialization_required'
                first = tool('seywo_initialize_project')
                second = tool('seywo_initialize_project')
                assert first['project_id'] == second['project_id']
                model_status = tool('seywo_model_status')
                assert model_status['backend'] == 'codex' and model_status['model_calls'] == 0
                model_saved = tool('seywo_model_save', endpoint='http://127.0.0.1:4317/openai/v1',
                                   model='fixture-model', api_key='fixture-key')
                assert model_saved['saved'] and model_saved['credential_configured']
                saved_config = (workspace / '.seywo/config.json').read_text()
                assert 'fixture-key' not in saved_config
                assert tool('seywo_model_status')['model'] == 'fixture-model'
                fixture = workspace / 'source.jsonl'
                events = [
                    {'type': 'session_meta', 'payload': {'id': 'plugin-acceptance', 'cwd': str(workspace)}},
                    {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                        'content': [{'type': 'input_text', 'text': 'seywopluginneedle'}]}},
                ]
                fixture.write_text(''.join(json.dumps(item) + '\n' for item in events))
                # Import a fixture only; never scan the user's Codex history.
                # bin/seywo is a shell script, so invoke it through sh for portability.
                result = subprocess.run(['sh', str(installed / 'bin/seywo'),
                                         '--config', first['config'], 'import', str(fixture),
                                         '--chat', 'plugin-acceptance', '--order', '0'],
                                        cwd=workspace, capture_output=True, text=True, timeout=20)
                assert result.returncode == 0, result.stderr
                host_fixture = workspace / 'host-source.jsonl'
                host_events = json.loads(json.dumps(events))
                host_events[0]['payload']['id'] = thread
                host_events[1]['payload']['content'][0]['text'] = 'host current chat fixture'
                host_fixture.write_text(''.join(json.dumps(item) + '\n' for item in host_events))
                imported = subprocess.run(['sh', str(installed / 'bin/seywo'), '--config', first['config'],
                                           'import', str(host_fixture), '--chat', thread, '--order', '0'],
                                          cwd=workspace, capture_output=True, text=True, timeout=20)
                assert imported.returncode == 0, imported.stderr
                automatic = raw_tool('seywo_open')['structuredContent']
                assert automatic['chat_id'] == thread
                assert tool('seywo_panel_data', view='chats')['default_chat_id'] == thread
                search = tool('seywo_search', query='seywopluginneedle')
                assert len(search['results']) == 1
                assert search['results'][0]['chat_id'] == 'plugin-acceptance'
                evidence = tool('seywo_expand', object_id=search['results'][0]['id'])
                assert 'seywopluginneedle' in evidence['text']
                # UI resource and metadata must survive the actual Codex host transport.
                open_meta = running['tools']['seywo_open']['_meta']
                assert open_meta['openai/ui']['entrypoints'] == [{'type': 'thread'}]
                assert running['tools']['seywo_open']['title'] == 'Seywo'
                assert open_meta['ui']['resourceUri'] == 'ui://seywo/panel-v1.html'
                assert running['tools']['seywo_panel_data']['_meta']['ui']['visibility'] == ['app']
                opened = raw_tool('seywo_open', chat_id='plugin-acceptance')
                assert opened['structuredContent']['chat_id'] == 'plugin-acceptance'
                assert len(json.dumps(opened).encode()) < 1000
                resource = request('mcpServer/resource/read', {
                    'threadId': thread, 'server': 'seywo', 'uri': open_meta['ui']['resourceUri']})
                html = resource['contents'][0]
                assert html['mimeType'] == 'text/html;profile=mcp-app'
                assert '<script type="module">' in html['text']
                assert '<script src="http' not in html['text']
                assert html['_meta']['ui']['csp']['connectDomains'] == []
                assert not (installed / 'ui/node_modules').exists()
                chats = tool('seywo_panel_data', view='chats')
                assert any(item['chat_id'] == 'plugin-acceptance' and item['version'] == 0 for item in chats['results'])
                assert not tool('seywo_panel_data', view='versions', chat_id='plugin-acceptance')['results']
                unpublished = tool('seywo_panel_data', view='mainline', chat_id='plugin-acceptance')
                assert unpublished['state'] == 'unpublished' and unpublished['model_calls'] == 0
                # Deterministically publish two real stored versions using the installed runtime.
                # This is a UI fixture, never model extraction or modification of a live project.
                seed_code = r"""
import json,sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'src'))
from seywo.config import load_config
from seywo.store import Store, encode
from seywo.narrative import Narrative, empty
from seywo.compact_pipeline import _turn_packets, apply_compact
from seywo.understanding import Understanding
with Store(load_config(Path(sys.argv[2]))) as store:
    turns = _turn_packets(store.db.execute('SELECT * FROM current_sources ORDER BY record_no').fetchall())
    output = {'segments':[{'title':'插件开发','turn_indexes':[0],'action':'continue'}],
              'changes':[{'operation':'new','title':'验证插件主线','type':'goal',
                          'attribution':'user','turn_indexes':[0],'target_index':None,'open':True}],
              'focus_segment':0}
    doc,_,_ = apply_compact(empty(), output, turns, [], 'plugin-ui-fixture')
    service = Narrative(store)
    with store.db:
        service._write('plugin-acceptance',1,0,doc,encode(Understanding(store)._heads('plugin-acceptance')),origin='fixture')
    item = doc['items'][0]
    service.edit('plugin-acceptance',base_version=1,override_revision=0,actor='fixture',
                 patch={'entity':'item','id':item['id'],'fields':{'title':'已验证插件主线'}})
"""
                seeded = subprocess.run(['python3', '-c', seed_code, str(installed), first['config']],
                                        cwd=workspace, capture_output=True, text=True, timeout=20)
                assert seeded.returncode == 0, seeded.stderr
                narrative = tool('seywo_panel_data', view='narrative', chat_id='plugin-acceptance')
                mainline = tool('seywo_panel_data', view='mainline', chat_id='plugin-acceptance')
                assert mainline['goals'] and mainline['model_calls'] == 0
                assert 'results' not in mainline and 'node_count' not in mainline
                assert narrative['version'] == 2
                assert narrative['results'][0]['title'] == '已验证插件主线'
                node = tool('seywo_panel_data', view='node', object_id=narrative['results'][0]['object_id'])
                ref = node['results'][0]['item']['evidence'][0]
                source = tool('seywo_panel_data', view='source', chat_id='plugin-acceptance',
                              event_id=ref['event_id'], snapshot_id=ref['snapshot_id'],
                              start=ref['start'], end=ref['end'])
                assert 'seywopluginneedle' in source['text']
                changes = tool('seywo_panel_data', view='changes', chat_id='plugin-acceptance', version=2)
                assert changes['counts']['updated'] == 1
                assert changes['counts']['added'] == changes['counts']['merged'] == 0
                assert (changes['before_count'], changes['after_count']) == (1, 1)
                opened = raw_tool('seywo_open', chat_id='plugin-acceptance')
                assert opened['structuredContent']['chat_id'] == 'plugin-acceptance'
                # A host turn with no account in this disposable HOME fails
                # authentication, but still exercises the native lifecycle.
                request('turn/start', {'threadId': thread, 'input': [
                    {'type': 'text', 'text': 'Seywo lifecycle fixture only.', 'text_elements': []}]})
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    done = [notice['params']['run'] for notice in notifications
                            if notice.get('method') == 'hook/completed']
                    if {'sessionStart', 'userPromptSubmit'} <= {
                            run['eventName'] for run in done if run['status'] == 'completed'}:
                        break
                    if b'\n' in buffered:
                        line, buffered = buffered.split(b'\n', 1)
                        notifications.append(json.loads(line))
                    elif select.select([process.stdout], [], [], 0.2)[0]:
                        buffered += os.read(process.stdout.fileno(), 65536)
                completed_hooks = [notice['params']['run'] for notice in notifications
                                   if notice.get('method') == 'hook/completed']
                assert {'sessionStart', 'userPromptSubmit'} <= {
                    run['eventName'] for run in completed_hooks if run['status'] == 'completed'}, notifications
                status = tool('seywo_status')
                assert status['model_calls'] == 0
                assert not (installed / '.seywo').exists()
                assert (workspace / '.seywo/.gitignore').read_text() == '*\n!.gitignore\n'
                return {'verified_on': date.today().isoformat(), 'status': 'passed',
                        'host': host['userAgent'], 'marketplace': 'seywo-local',
                        'plugin': 'seywo', 'plugin_hooks': plugin_hooks, 'completed_hooks': completed_hooks, 'runtime_status': running['runtimeStatus'],
                        'tools': sorted(running['tools']), 'tool_count': len(running['tools']),
                        'app_resource_bytes': len(html['text'].encode()),
                        'open_response_bytes': len(json.dumps(opened).encode()),
                        'fixture_diff_counts': changes['counts'],
                        'checks': ['actual_cli_install', 'resolved_cached_entrypoint',
                                   'host_mcp_connected', 'host_plugin_hooks_inventory', 'no_write_startup', 'no_write_selection',
                                   'idempotent_explicit_setup', 'project_local_model_status_and_save', 'default_gitignore',
                                   'installed_package_import', 'host_search_and_expand',
                                   'installation_directory_untouched', 'host_app_metadata', 'host_thread_entrypoint',
                                   'temporary_host_catalog_fixture', 'host_automatic_project_binding', 'host_automatic_chat_default', 'uninitialized_native_open',
                                   'host_app_only_visibility', 'host_app_resource_read',
                                   'self_contained_installed_ui', 'tiny_render_result',
                                   'unpublished_chat_empty_state', 'host_panel_mainline_without_records', 'host_panel_narrative',
                                   'host_panel_pinned_source', 'host_panel_persisted_version_diff'],
                        'model_calls_for_test_tools': 0,
                        'not_verified': ['desktop_menu_ui', 'desktop_embedded_rendering',
                                         'plugin_compact_hooks', 'codex_model_extraction',
                                         'clean_machine_dependency_installation']}
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--repository', type=Path, default=REPOSITORY)
    args = parser.parse_args()
    report = verify(args.repository.resolve())
    text = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.report:
        args.report.write_text(text)
    print(text)
