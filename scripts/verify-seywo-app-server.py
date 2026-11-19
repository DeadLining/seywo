#!/usr/bin/env python3
"""Check real app-server isolation with a local fake Responses provider.

No model or account is used. Run explicitly on a host with Codex installed;
ordinary protocol tests use a fake app-server executable instead.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'seywo/src'))
from seywo.codex_app_server import close_workers
from seywo.codex_transport import CodexTransport


def verify(*, command=None):
    captured = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            size = int(self.headers.get('content-length', 0))
            if size > 4 * 1024 * 1024:
                self.send_error(413)
                return
            captured.append(json.loads(self.rfile.read(size)))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            answer = {
                'id': 'message-1', 'type': 'message', 'role': 'assistant',
                'status': 'completed', 'phase': 'final_answer',
                'content': [{'type': 'output_text', 'text': '{"ok":true}'}],
            }
            events = [
                {'type': 'response.created', 'response': {'id': 'r1', 'status': 'in_progress'}},
                {'type': 'response.output_item.added', 'output_index': 0,
                 'item': {'id': 'message-1', 'type': 'message', 'role': 'assistant', 'content': []}},
                {'type': 'response.output_text.delta', 'item_id': 'message-1',
                 'output_index': 0, 'content_index': 0, 'delta': '{"ok":true}'},
                {'type': 'response.output_item.done', 'output_index': 0, 'item': answer},
                {'type': 'response.completed', 'response': {
                    'id': 'r1', 'status': 'completed', 'output': [answer],
                    'usage': {'input_tokens': 100, 'output_tokens': 5, 'total_tokens': 105}}},
            ]
            try:
                for event in events:
                    self.wfile.write(('data: ' + json.dumps(event) + '\n\n').encode())
                    self.wfile.flush()
            except OSError:
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='seywo-wire-') as folder:
            root = Path(folder)
            (root / 'config.toml').write_text(
                'model="gpt-5.4"\nmodel_provider="fixture"\n'
                '[model_providers.fixture]\nname="fixture"\n'
                f'base_url="http://127.0.0.1:{server.server_port}/v1"\n'
                'wire_api="responses"\nrequires_openai_auth=false\n'
                'experimental_bearer_token="fixture-key"\n', encoding='utf-8')
            try:
                transport = CodexTransport(home=root, timeout=20, command=command)
                results = [transport.probe(), transport.probe()]
                assert all(r['finish'] == 'complete' and r['output'] == {'ok': True}
                           for r in results), results
                assert len(captured) == 2
                for request in captured:
                    names = {tool.get('name') for tool in request.get('tools', [])}
                    # Older hosts also include apply_patch even with shell tools
                    # disabled. Definitions vary; invoking any is rejected by
                    # the worker and is covered by the protocol tests.
                    assert names <= {'request_user_input', 'apply_patch'}, names
                    content = json.dumps({'input': request.get('input', []),
                                          'instructions': request.get('instructions')}, ensure_ascii=False)
                    assert '<skills_instructions>' not in content and 'Available skills' not in content
                    assert 'You are the Seywo conversation compressor' in content
                    assert request['text']['format']['schema']['properties']['ok']['type'] == 'boolean'
                assert not list((root / 'sessions').rglob('*.jsonl'))
                return {
                    'ok': True, 'provider': 'local fixture (no real model)',
                    'requests': len(captured),
                    'tools': [t.get('name') for t in captured[0].get('tools', [])],
                    'input_chars': [len(json.dumps(r.get('input', []), ensure_ascii=False)) for r in captured],
                    'persistent_transcripts': 0,
                }
            finally:
                close_workers()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex', help='Explicit native Codex executable to verify')
    args = parser.parse_args()
    print(json.dumps(verify(command=[args.codex] if args.codex else None), ensure_ascii=False))
