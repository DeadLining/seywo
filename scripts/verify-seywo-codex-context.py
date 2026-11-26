#!/usr/bin/env python3
"""Capture native requests against a loopback fixture, without an upstream model."""
# ruff: noqa: E402
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'seywo/src'))
from seywo.codex_transport import CodexTransport
from seywo.compact_model import instructions, request_payload


def main(report):
    requests = []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            messages = [part.get('text', '') for item in body.get('input', [])
                        for part in item.get('content', [])]
            requests.append({'skill_catalog_present': any('<skills_instructions>' in text for text in messages),
                'input_chars': sum(len(text) for text in messages),
                'tools': [tool.get('name', tool.get('type')) for tool in body.get('tools', [])]})
            output = {'segments': [], 'changes': [],
                      'current': {"goal_index": None, "priority_indexes": []}}
            message = {'id': 'msg', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                       'content': [{'type': 'output_text', 'text': json.dumps(output), 'annotations': []}]}
            result = {'id': 'response', 'status': 'completed', 'model': body['model'], 'output': [message],
                      'usage': {'input_tokens': 10, 'output_tokens': 10, 'total_tokens': 20}}
            events = [{'type': 'response.created', 'response': {**result, 'status': 'in_progress', 'output': []}},
                {'type': 'response.output_item.added', 'output_index': 0, 'item': {**message, 'content': []}},
                {'type': 'response.output_text.delta', 'item_id': 'msg', 'output_index': 0,
                 'content_index': 0, 'delta': json.dumps(output)},
                {'type': 'response.output_item.done', 'output_index': 0, 'item': message},
                {'type': 'response.completed', 'response': result}]
            wire = ''.join('event: '+event['type']+'\ndata: '+json.dumps(event)+'\n\n' for event in events).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)
    api = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=api.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='seywo-context-fixture-') as directory:
            home = Path(directory)
            (home / 'config.toml').write_text('model="gpt-6.1-sol"\nmodel_provider="fixture"\n'
                '[model_providers.fixture]\nname="fixture"\nwire_api="responses"\nrequires_openai_auth=false\n'
                f'base_url="http://127.0.0.1:{api.server_port}/v1"\n')
            baseline = REPO / 'dist/seywo-plugin-0.3.0/seywo/src/seywo/codex_transport.py'
            spec = importlib.util.spec_from_file_location('seywo._baseline_context_transport', baseline)
            old = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(old)
            for cls in (old.CodexTransport, CodexTransport):
                result = cls(home=home, timeout=40).complete_compact(model='codex-default',
                    instructions=instructions(), payload=request_payload([], {}, []))
                assert not result.get('error'), result
            assert len(requests) == 2
            assert not requests[1]['skill_catalog_present'], requests
            assert 'view_image' not in requests[1]['tools'], requests
            report.write_text(json.dumps({'verified': True, 'upstream_model_calls': 0,
                'usage_is_synthetic': True, 'user_settings_modified': False,
                'before': requests[0], 'after': requests[1]}, ensure_ascii=False, indent=2) + '\n')
            print(report.read_text())
    finally:
        api.shutdown()
        api.server_close()


if __name__ == '__main__':
    main(Path(sys.argv[1]))
