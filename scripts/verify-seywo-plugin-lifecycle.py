#!/usr/bin/env python3
"""Native host Hook lifecycle with an isolated deterministic HTTP model fixture.

No user credentials/config/history are used. This verifies trigger/plumbing,
not semantic model quality; real Codex extraction is a separate acceptance.
"""
import argparse
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import tempfile
import threading
import time

REPO = Path(__file__).resolve().parents[1]


def verify():
    requests = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            schema = (body.get('text') or {}).get('format', {}).get('schema') or {}
            extracting = 'segments' in schema.get('properties', {})
            requests.append({'path': self.path, 'extraction': extracting})
            text = 'Fixture answer: package the kernel inside the plugin.'
            if extracting:
                messages = [item for item in body.get('input', []) if item.get('role') == 'user']
                prompt = '\n'.join(part.get('text', '') for item in messages
                                   for part in item.get('content', []) if isinstance(part, dict))
                payload = json.loads(prompt.splitlines()[-1])
                indexes = [turn['index'] for turn in payload['turns']]
                text = json.dumps({'segments': [{'title': '插件独立运行', 'turn_indexes': indexes,
                                  'action': 'continue'}], 'changes': [], 'focus_segment': 0, 'current': {'goal_indexes': [], 'question_indexes': [], 'next_step_indexes': []}}, ensure_ascii=False)
            message = {'id': 'msg_fixture', 'type': 'message', 'role': 'assistant',
                       'status': 'completed', 'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}
            events = [
                {'type': 'response.created', 'response': {'id': 'resp_fixture', 'status': 'in_progress'}},
                {'type': 'response.output_item.added', 'output_index': 0,
                 'item': {**message, 'status': 'in_progress', 'content': []}},
                {'type': 'response.output_text.delta', 'item_id': message['id'], 'output_index': 0,
                 'content_index': 0, 'delta': text},
                {'type': 'response.output_item.done', 'output_index': 0, 'item': message},
                {'type': 'response.completed', 'response': {'id': 'resp_fixture', 'status': 'completed',
                 'output': [message], 'usage': {'input_tokens': 10, 'output_tokens': 10, 'total_tokens': 20}}},
            ]
            wire = ''.join('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n' for event in events).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)

    api = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=api.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='seywo-native-lifecycle-') as temporary:
            base = Path(temporary).resolve()
            home, project = base / '.codex', base / 'project with spaces'
            home.mkdir()
            project.mkdir()
            (home / 'config.toml').write_text(f'''model = "fixture"
model_provider = "fixture"
[model_providers.fixture]
name = "Isolated fixture"
base_url = "http://127.0.0.1:{api.server_port}/v1"
wire_api = "responses"
requires_openai_auth = false
request_max_retries = 0
stream_max_retries = 0
''')
            env = {**os.environ, 'CODEX_HOME': str(home)}
            for args in [('plugin', 'marketplace', 'add', str(REPO)), ('plugin', 'add', 'seywo@seywo-local')]:
                subprocess.run(['codex', *args], env=env, cwd=project, check=True, capture_output=True, timeout=30)
            version = json.loads((REPO / 'seywo/.codex-plugin/plugin.json').read_text())['version']
            installed = home / 'plugins/cache/seywo-local/seywo' / version
            setup = "from pathlib import Path; from seywo.config import initialize,enable_plugin_hook; initialize(Path('.')); enable_plugin_hook(Path('.seywo/config.json'))"
            subprocess.run(['python3', '-c', setup], env={**env, 'PYTHONPATH': str(installed / 'src')},
                           cwd=project, check=True, capture_output=True, timeout=20)
            notices, buffer, identity = [], b'', 0
            with (base / 'host.stderr').open('wb') as errors:
                process = subprocess.Popen(['codex', 'app-server', '--stdio', '--enable', 'hooks'],
                    cwd=project, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)

                def read(timeout):
                    nonlocal buffer
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        if b'\n' in buffer:
                            line, buffer = buffer.split(b'\n', 1)
                            row = json.loads(line)
                            if 'method' in row:
                                notices.append(row)
                            return row
                        if select.select([process.stdout], [], [], 0.2)[0]:
                            chunk = os.read(process.stdout.fileno(), 65536)
                            if not chunk:
                                raise RuntimeError('Host exited')
                            buffer += chunk
                    raise TimeoutError('host notification')

                def request(method, params):
                    nonlocal identity
                    identity += 1
                    current = identity
                    process.stdin.write((json.dumps({'id': current, 'method': method, 'params': params}) + '\n').encode())
                    process.stdin.flush()
                    while True:
                        row = read(30)
                        if row.get('id') == current:
                            assert 'error' not in row, row
                            return row['result']

                def until(method):
                    deadline = time.monotonic() + 40
                    while not any(row.get('method') == method for row in notices):
                        read(max(0.1, deadline - time.monotonic()))

                try:
                    request('initialize', {'clientInfo': {'name': 'seywo-lifecycle', 'version': '1'},
                                          'capabilities': {'experimentalApi': True}})
                    process.stdin.write(b'{"method":"initialized"}\n')
                    process.stdin.flush()
                    hooks = request('hooks/list', {'cwds': [str(project)]})['data'][0]['hooks']
                    assert len(hooks) == 4
                    # This isolated verifier reviews its own installed source.
                    for hook in hooks:
                        request('config/value/write', {'keyPath': 'hooks.state.' + json.dumps(hook['key']) + '.trusted_hash',
                                'value': hook['currentHash'], 'mergeStrategy': 'replace'})
                    started = request('thread/start', {'cwd': str(project), 'ephemeral': False,
                                                       'sandbox': 'workspace-write', 'approvalPolicy': 'never'})
                    thread = started['thread']['id']
                    request('turn/start', {'threadId': thread, 'input': [{'type': 'text',
                        'text': '把内核放到插件中，不再依赖独立应用。', 'text_elements': []}]})
                    until('turn/completed')
                    assert not any(row.get('method') == 'error' for row in notices), notices
                    notices.clear()
                    request('thread/compact/start', {'threadId': thread})
                    until('turn/completed')
                    deadline = time.monotonic() + 40
                    found = []
                    while time.monotonic() < deadline:
                        db_path = project / '.seywo/index.sqlite3'
                        with sqlite3.connect(db_path) as db:
                            db.row_factory = sqlite3.Row
                            found = [dict(row) for row in db.execute('SELECT state,turn_count,segment_count,model_calls,error_code FROM compact_batches')]
                        if found and any(row['state'] == 'done' for row in found):
                            break
                        time.sleep(0.2)
                    assert found and all(row['state'] == 'done' for row in found), found
                    runs = [row['params']['run'] for row in notices if row.get('method') == 'hook/completed']
                    assert {'preCompact', 'postCompact'} <= {row['eventName'] for row in runs if row['status'] == 'completed'}, runs
                    assert sum(row['model_calls'] for row in found) == 1, found
                    return {'status': 'passed', 'verified_on': date.today().isoformat(), 'host': subprocess.check_output(['codex', '--version'], text=True).strip(),
                            'provider': 'deterministic loopback fixture; no semantic quality claim',
                            'compact_hooks': [{'event': row['eventName'], 'status': row['status']} for row in runs],
                            'receipts': found, 'provider_requests': requests, 'user_config_modified': False}
                except Exception:
                    print(json.dumps({'requests': requests, 'notices': notices[-12:]}, ensure_ascii=False)[-12000:])
                    raise
                finally:
                    process.terminate()
                    process.wait(timeout=5)
    finally:
        api.shutdown()
        api.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    result = verify()
    text = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.report:
        args.report.write_text(text)
    print(text)
