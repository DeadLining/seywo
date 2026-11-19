#!/usr/bin/env python3
"""Read the selected Codex host's loaded Seywo hooks; never grants trust."""
import argparse
import json
import os
from pathlib import Path
from seywo_protocol import PipeReader
import subprocess
import tempfile
import time


def verify(binary, project):
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen([binary, 'app-server', '--stdio'], cwd=project,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
        reader = PipeReader(process.stdout)
        buffer = b''
        def request(identity, method, params):
            nonlocal buffer
            process.stdin.write((json.dumps({'id': identity, 'method': method, 'params': params}) + '\n').encode())
            process.stdin.flush()
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    row = json.loads(line)
                    if row.get('id') == identity:
                        if 'error' in row:
                            raise RuntimeError(str(row['error']))
                        return row['result']
                else:
                    chunk = reader.read(0.5)
                    if chunk is None:
                        continue
                    if not chunk:
                        raise RuntimeError('host closed')
                    buffer += chunk
            raise TimeoutError(method)
        try:
            request(1, 'initialize', {'clientInfo': {'name': 'seywo-hook-acceptance', 'version': '1'},
                                     'capabilities': {'experimentalApi': True}})
            process.stdin.write(b'{"method":"initialized"}\n')
            process.stdin.flush()
            result = request(2, 'hooks/list', {'cwds': [str(project)]})
            hooks, errors = [], []
            for entry in result.get('data', []):
                errors.extend(entry.get('errors', []))
                for hook in entry.get('hooks', []):
                    if hook.get('pluginId') == 'seywo@seywo-local':
                        hooks.append({k: hook.get(k) for k in ('eventName', 'enabled', 'trustStatus', 'currentHash', 'sourcePath')})
            return {'host': subprocess.check_output([binary, '--version'], text=True).strip(),
                    'hooks': hooks, 'errors': errors}
        finally:
            process.terminate()
            process.wait(timeout=5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--project', type=Path, default=Path.cwd())
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    result = verify(args.host, args.project.resolve())
    text = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.report:
        args.report.write_text(text)
    print(text)
