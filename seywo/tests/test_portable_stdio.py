"""Exercise the actual marketplace command and offline Hook on each native OS."""
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


def test_host_entry_serves_relocated_project_and_offline_hook(tmp_path):
    import importlib.util
    import platform
    import mcp
    runtime = tmp_path / 'fixture host runtime'
    (runtime / 'dependencies').mkdir(parents=True)
    (runtime / 'dependencies/python').symlink_to(sys.base_prefix, target_is_directory=True)
    (runtime / 'runtime.json').write_text(json.dumps({
        'bundleFormatVersion': 2, 'bundleVersion': 'test-fixture-not-desktop', 'targetPlatform': sys.platform,
        'targetArch': platform.machine(), 'pythonVersion': platform.python_version()}))
    installed = tmp_path / '中文 plugin with spaces'
    shutil.copytree(ROOT, installed, ignore=shutil.ignore_patterns('__pycache__', '.venv', 'node_modules'))
    project = tmp_path / '中文 project % #'
    project.mkdir()
    home = tmp_path / 'isolated-codex-home'
    home.mkdir()
    spec = json.loads((installed / 'mcp.json').read_text())['mcpServers']['seywo']
    env = {**os.environ, 'CODEX_HOME': str(home), 'SEYWO_CODEX_RUNTIME': str(runtime),
           'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}
    # Source suite seeds private SDK deps from its test environment; installed-host
    # acceptance separately verifies first provisioning from an empty cache.
    spec_module = importlib.util.spec_from_file_location('host_fixture', installed / 'bin/seywo-host.py')
    bootstrap = importlib.util.module_from_spec(spec_module)
    spec_module.loader.exec_module(bootstrap)
    key = bootstrap.dependency_key(bootstrap.LOCK.read_bytes())
    private = home / 'seywo/runtime' / key
    import importlib.metadata
    import re
    private.mkdir(parents=True)
    from packaging.requirements import Requirement
    names = re.findall(r'^([a-zA-Z0-9_-]+)==', bootstrap.LOCK.read_text(), re.M)
    copied = set()
    while names:
        name = names.pop()
        if name.lower() in copied:
            continue
        copied.add(name.lower())
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            continue  # platform-marked distributions, such as pywin32 on POSIX
        for dependency in distribution.requires or []:
            requirement = Requirement(dependency)
            if not requirement.marker or requirement.marker.evaluate({'extra': ''}):
                names.append(requirement.name)
        for relative in distribution.files or []:
            if '..' in relative.parts or relative.suffix == '.pyc':
                continue
            source = distribution.locate_file(relative)
            if source.is_file():
                destination = private / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
    checked = subprocess.run([sys.executable, '-IBS', '-c', 'import sys,site; site.addsitedir(sys.argv[1]); from mcp.server.fastmcp import FastMCP', str(private)], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
    (private / 'ready.json').write_text(json.dumps({'key': key}))
    env['PATH'] = os.path.join(os.environ['SystemRoot'], 'System32') if os.name == 'nt' else '/usr/bin:/bin'
    command = str(installed / 'bin' / ('seywo-host.cmd' if os.name == 'nt' else 'seywo-host'))
    env.pop('SEYWO_INTERNAL_MODEL', None)
    params = StdioServerParameters(command=command,
        args=[*spec['args'], '--project', str(project)], cwd=installed, env=env)

    async def check():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as session:
                await session.initialize()
                assert len((await session.list_tools()).tools) == 19
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
    # Use exactly the installed project's registration, not the development
    # interpreter or the marketplace MCP (which has a different tool set).
    subprocess.run([command, '--cli', 'mcp-install'], cwd=project, env=env,
                   capture_output=True, check=True, timeout=30)
    bound = tomllib.loads((project / '.codex/config.toml').read_text())['mcp_servers']['seywo']

    async def check_project_registration():
        params = StdioServerParameters(command=bound['command'], args=bound['args'], cwd=project, env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=30)) as session:
                await session.initialize()
                result = await session.call_tool('seywo_search', {'query': 'portableproof'})
                assert not result.isError
                assert json.loads(result.content[0].text)['results'][0]['chat_id'] == 'portable-chat'

    asyncio.run(check_project_registration())
    # Hook never prepares dependencies, even before first MCP initialization.
    args = ['--hook']
    before = {p.relative_to(project): p.read_bytes() for p in (project / '.seywo').rglob('*') if p.is_file()}
    result = subprocess.run([command, *args], input=json.dumps({
        'hook_event_name': 'SessionStart', 'session_id': 'portable-chat', 'cwd': str(project)}),
        env=env, cwd=project, capture_output=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'Traceback' not in result.stderr
    assert not (installed / '.seywo').exists()
    after = {p.relative_to(project): p.read_bytes() for p in (project / '.seywo').rglob('*') if p.is_file()}
    assert all(after.get(path) == content for path, content in before.items())
    assert set(after) - set(before) <= {Path(".seywo/maintenance/.lock")}
