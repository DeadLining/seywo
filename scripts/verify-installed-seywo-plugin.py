#!/usr/bin/env python3
"""Read-only acceptance of the installed Seywo plugin through a real Codex host.

Uses an ephemeral host thread and an optional report. --chat also checks the
installed STDIO server directly with exact native-style request metadata. Never imports chats,
initializes projects, calls a model, or edits the narrative/provider/Hook settings.
"""
import argparse
import json
import os
from pathlib import Path
from seywo_protocol import PipeReader, wait_server
import subprocess
import tempfile
import time


def verify_installed_payload(repository, installed):
    """Verify deployed bytes, not just the manifest's version label.

    Reusing a version leaves Codex free to reuse its existing cached payload.
    This check fails before starting a host or reading project data.
    """
    source = Path(repository) / 'seywo'
    files = [source / name for name in (
        '.codex-plugin/plugin.json', 'pyproject.toml', 'mcp.json', 'hooks.json', 'ui/panel.html')]
    for folder in ('src/seywo', 'bin', 'skills'):
        files.extend(path for path in (source / folder).rglob('*')
                     if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc')
    for path in files:
        relative = path.relative_to(source)
        target = Path(installed) / relative
        assert (not target.is_symlink() and target.is_file() and target.read_bytes() == path.read_bytes()), (
            f'installed_payload_mismatch: {relative}; update the installed plugin and reconnect existing chats')
    return len(files)


def verify(project, host_chat=None, host_binary="codex"):
    def cli(*arguments):
        result = subprocess.run([host_binary, *arguments], cwd=project, capture_output=True,
                                text=True, timeout=30, check=True)
        return json.loads(result.stdout)

    plugins = cli('plugin', 'list', '--json')['installed']
    plugin = next(item for item in plugins if item['pluginId'] == 'seywo@seywo-local')
    expected_version = json.loads((Path(__file__).resolve().parents[1] / 'seywo/.codex-plugin/plugin.json').read_text())['version']
    assert plugin['enabled'] and plugin['version'] == expected_version
    servers = cli('mcp', 'list', '--json')
    registered = [item for item in servers if item['name'] == 'seywo']
    assert len(registered) == 1
    transport = registered[0]['transport']
    installed = Path(transport['cwd'])
    expected = json.loads((Path(__file__).resolve().parents[1] / 'seywo/mcp.json').read_text())['mcpServers']['seywo']
    assert transport['command'] == expected['command']
    assert transport['args'] == expected['args']
    assert Path(transport['cwd']) == installed
    payload_files = verify_installed_payload(Path(__file__).resolve().parents[1], installed)
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen([host_binary, 'app-server', '--stdio'], cwd=project,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
        reader = PipeReader(process.stdout)
        buffered = b''
        next_id = 0

        def request(method, params):
            nonlocal buffered, next_id
            next_id += 1
            identity = next_id
            process.stdin.write((json.dumps({'id': identity, 'method': method,
                                            'params': params}) + '\n').encode())
            process.stdin.flush()
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if b'\n' not in buffered:
                    chunk = reader.read(0.5)
                    if chunk is None:
                        continue
                    if not chunk:
                        raise RuntimeError('Codex host closed the connection')
                    buffered += chunk
                while b'\n' in buffered:
                    line, buffered = buffered.split(b'\n', 1)
                    response = json.loads(line)
                    if response.get('id') == identity:
                        if 'error' in response:
                            raise RuntimeError(str(response['error']))
                        return response['result']
            raise TimeoutError(method)

        try:
            host = request('initialize', {'clientInfo': {'name': 'seywo-installed-acceptance',
                                                       'version': '1'},
                                          'capabilities': {'experimentalApi': True}})
            process.stdin.write(b'{"method":"initialized"}\n')
            process.stdin.flush()
            started = request('thread/start', {'cwd': str(project), 'ephemeral': True,
                                               'sandbox': 'workspace-write', 'approvalPolicy': 'never'})
            thread = started['thread']['id']
            hooks = request('hooks/list', {'cwds': [str(project)]})
            plugin_hooks = [hook for entry in hooks['data'] for hook in entry['hooks']
                            if hook.get('pluginId') == 'seywo@seywo-local']
            declared = json.loads((installed / 'hooks.json').read_text())['hooks']
            actual = sorted(hook['eventName'] for hook in plugin_hooks)
            expected_events = sorted(key[0].lower() + key[1:] for key in declared)
            assert actual == expected_events, {'expected': expected_events, 'actual': actual}
            server = wait_server(request, thread)
            # Older desktop backends omit runtimeStatus. Actual calls below
            # establish connectivity rather than inferring it from inventory.
            if 'runtimeStatus' in server:
                assert server['runtimeStatus'] == 'connected'
            assert server.get('tools'), {'fields': sorted(server)}
            tools = server['tools']
            assert len(tools) == 19
            metadata = tools['seywo_open']['_meta']
            assert metadata['openai/ui']['entrypoints'] == [{'type': 'thread'}]
            assert tools['seywo_panel_data']['_meta']['ui']['visibility'] == ['app']
            assert tools['seywo_retry_maintenance']['_meta']['ui']['visibility'] == ['app']

            def raw_tool(name, **arguments):
                result = request('mcpServer/tool/call', {'threadId': thread, 'server': 'seywo',
                                                       'tool': name, 'arguments': arguments})
                assert not result.get('isError'), result
                return result

            def tool(name, **arguments):
                return json.loads(raw_tool(name, **arguments)['content'][0]['text'])

            if host_chat:
                import asyncio
                from mcp import ClientSession, StdioServerParameters
                from mcp.client.stdio import stdio_client

                async def automatic_check():
                    params = StdioServerParameters(command=transport['command'], args=transport['args'],
                                                   cwd=transport['cwd'], env=transport['env'])
                    async with stdio_client(params) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            result = await session.call_tool('seywo_open', {},
                                meta={'thread_id': host_chat, 'threadId': host_chat})
                            assert not result.isError
                            assert result.structuredContent['project_root'] == str(project)
                            assert result.structuredContent['chat_id'] == host_chat
                asyncio.run(automatic_check())
            selected = tool('seywo_select_project', project_root=str(project))
            assert selected['initialized'], 'Existing project must already be initialized'
            status = tool('seywo_status')
            model = tool('seywo_model_status')
            assert model.get('scope') == 'user', 'Installed plugin is not using user-level model settings'
            opened = raw_tool('seywo_open')
            resource = request('mcpServer/resource/read', {
                'threadId': thread, 'server': 'seywo', 'uri': metadata['ui']['resourceUri']})
            html = resource['contents'][0]
            assert html['mimeType'] == 'text/html;profile=mcp-app'
            chats = tool('seywo_panel_data', view='chats')
            narrative = None
            maintenance = None
            if chats['results']:
                chat = chats['results'][0]
                narrative = tool('seywo_panel_data', view='narrative', chat_id=chat['chat_id'])
                mainline = tool('seywo_panel_data', view='mainline', chat_id=chat['chat_id'])
                assert mainline['model_calls'] == 0 and 'results' not in mainline
                maintenance = tool('seywo_panel_data', view='maintenance', chat_id=chat['chat_id'])
                assert maintenance['model_calls'] == 0 and 'retryable' in maintenance
            assert not (installed / '.seywo').exists()
            return {'passed': True, 'project': str(project), 'plugin_version': plugin['version'],
                    'installed_path': str(installed), 'host': host, 'tools': len(tools),
                    'installed_payload_files_verified': payload_files,
                    'model_backend': model.get('backend'),
                    'hooks': [{'event': hook['eventName'], 'trust': hook['trustStatus']} for hook in plugin_hooks],
                    'entrypoint_preserved': True, 'app_only_visibility': True,
                    'ui_bytes': len(html['text'].encode()),
                    'open_response_bytes': len(json.dumps(opened).encode()),
                    'automatic_binding_verified_via_installed_stdio': bool(host_chat), 'status_read': bool(status), 'chat_page_count': len(chats['results']),
                    'narrative_read': narrative is not None, 'maintenance_read': maintenance is not None,
                    'model_calls': 0, 'desktop_menu_verified': False,
                    'desktop_panel_verified': False,
                    'desktop_note': 'This protocol verifier does not operate the desktop UI',
                    'scope': 'Installed plugin and fresh Codex app-server; native desktop rendering not verified',
                    'host_thread': 'ephemeral'}
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path.cwd())
    parser.add_argument('--report', type=Path)
    parser.add_argument('--host', default='codex', help='Codex executable; use the running desktop backend for desktop acceptance')
    parser.add_argument('--chat', help='Exact existing host thread; verify installed STDIO automatic binding without resuming it')
    args = parser.parse_args()
    report = verify(args.project.resolve(strict=True), args.chat, args.host)
    output = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.report:
        args.report.write_text(output)
    print(output, end='')


if __name__ == '__main__':
    main()
