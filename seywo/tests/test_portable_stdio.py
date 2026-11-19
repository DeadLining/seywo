"""Exercise the actual marketplace command and offline Hook on each native OS."""
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


def test_declared_uv_entry_serves_relocated_project_and_offline_hook(tmp_path):
    assert shutil.which('uv'), 'The portable plugin runtime requires uv on PATH'
    installed = tmp_path / '中文 plugin with spaces'
    shutil.copytree(ROOT, installed, ignore=shutil.ignore_patterns('__pycache__', '.venv', 'node_modules'))
    project = tmp_path / '中文 project % #'
    project.mkdir()
    home = tmp_path / 'isolated-codex-home'
    home.mkdir()
    spec = json.loads((installed / 'mcp.json').read_text())['mcpServers']['seywo']
    env = {**os.environ, 'CODEX_HOME': str(home), 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}
    env.pop('SEYWO_INTERNAL_MODEL', None)
    params = StdioServerParameters(command=spec['command'],
        args=[*spec['args'], '--project', str(project)], cwd=installed, env=env)

    async def check():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as session:
                await session.initialize()
                assert len((await session.list_tools()).tools) == 18
                assert not (project / '.seywo').exists()
                setup = await session.call_tool('seywo_initialize_project', {})
                assert not setup.isError
                source = project / 'source.jsonl'
                source.write_text(json.dumps({'type': 'session_meta', 'payload': {
                    'id': 'portable-chat', 'cwd': str(project)}}) + '\n' + json.dumps({
                    'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                    'content': [{'type': 'input_text', 'text': '跨平台检索 portableproof'}]}}) + '\n', encoding='utf-8')
                result = await asyncio.to_thread(subprocess.run,
                    [sys.executable, '-B', str(installed / 'bin/seywo-launch.py'), '--cli',
                     '--config', str(project / '.seywo/config.json'), 'import', str(source),
                     '--chat', 'portable-chat', '--order', '0'],
                    cwd=project, env=env, capture_output=True, encoding='utf-8', timeout=30)
                assert result.returncode == 0, result.stderr
                search = await session.call_tool('seywo_search', {'query': 'portableproof'})
                assert not search.isError
                result = json.loads(search.content[0].text)
                assert len(result['results']) == 1
                assert result['results'][0]['chat_id'] == 'portable-chat'
                assert '跨平台检索' in json.dumps(result, ensure_ascii=False)
                status = await session.call_tool('seywo_status', {})
                assert json.loads(status.content[0].text)['model_calls'] == 0

    asyncio.run(check())
    # The hook uses exactly the same cached environment, with networking disabled.
    args = spec['args'][:]
    args.insert(1, '--offline')
    args[args.index('./bin/seywo-launch.py')] = str(installed / 'bin/seywo-launch.py')
    args[args.index('--mcp')] = '--hook'
    before = {p.relative_to(project): p.read_bytes() for p in (project / '.seywo').rglob('*') if p.is_file()}
    result = subprocess.run([spec['command'], *args], input=json.dumps({
        'hook_event_name': 'SessionStart', 'session_id': 'portable-chat', 'cwd': str(project)}),
        env=env, cwd=project, capture_output=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'Traceback' not in result.stderr
    assert not (installed / '.seywo').exists()
    after = {p.relative_to(project): p.read_bytes() for p in (project / '.seywo').rglob('*') if p.is_file()}
    assert all(after.get(path) == content for path, content in before.items())
    assert set(after) - set(before) <= {Path(".seywo/maintenance/.lock")}
