import io
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError

import pytest

from sessiongraph.native_settings import handle
from sessiongraph.provider_credentials import save_key


@pytest.fixture
def app(tmp_path, monkeypatch):
    state, home = tmp_path / 'state', tmp_path / 'codex'
    state.mkdir(); home.mkdir()
    monkeypatch.delenv('PROBE_TEST_KEY', raising=False)
    return dict(state=state, codex_home=home, source=tmp_path / 'source')


def probe(app, endpoint='https://provider.test/v1/responses', **extra):
    return handle(dict(action='test_provider', endpoint=endpoint, model='draft-model',
                       api_key_env='PROBE_TEST_KEY', **extra), **app)


def test_actual_http_probe_reuses_saved_key_without_writing(app, monkeypatch):
    captured = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            captured.append((self.path, self.headers['Authorization'], json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            body = json.dumps(dict(status='completed', output=[dict(type='message', content=[dict(type='output_text', text='OK')])])).encode()
            self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        def log_message(self, *a): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    endpoint = f'http://127.0.0.1:{server.server_port}/v1/responses'
    save_key(app['state'], endpoint, 'fixture-saved-key')
    before = {str(p):p.read_bytes() for base in [app['state'],app['codex_home']] for p in base.rglob('*') if p.is_file() and 'call-logs' not in p.parts}
    monkeypatch.setenv('http_proxy','http://127.0.0.1:1')
    try: result = probe(app, endpoint)
    finally: server.shutdown(); server.server_close(); thread.join()
    assert result['ok'] and result['model_calls'] == 1
    assert result['provider_test']['elapsed_ms'] >= 0 and result['provider_test']['http_status'] == 200
    assert result['provider_test']['model'] == 'draft-model'
    assert captured == [('/v1/responses','Bearer fixture-saved-key',dict(model='draft-model',input='Reply with OK.',stream=False,store=False,max_output_tokens=16))]
    after = {str(p):p.read_bytes() for base in [app['state'],app['codex_home']] for p in base.rglob('*') if p.is_file() and 'call-logs' not in p.parts}
    assert before == after
    assert 'fixture-saved-key' not in json.dumps(result)


@pytest.mark.parametrize('failure,expected',[
    (HTTPError('https://provider.test',401,'secret',{},io.BytesIO(b'private-key')), '鉴权失败'),
    (HTTPError('https://provider.test',404,'secret',{},io.BytesIO()), '端点或模型不存在'),
    (HTTPError('https://provider.test',302,'secret',{'Location':'https://other.test'},io.BytesIO()), 'HTTP 302'),
    (URLError(socket.timeout()), '测试超时'),
    (URLError('private-key'), '连接失败'),
])
def test_errors_are_chinese_and_do_not_expose_provider_body(app, monkeypatch, failure, expected):
    class Opener:
        def open(self, *a, **kw): raise failure
    monkeypatch.setattr('sessiongraph.provider_probe.request.build_opener',lambda *a: Opener())
    result = probe(app, api_key='fixture-input-key')
    assert not result['ok'] and result['model_calls']==1 and expected in result['message']
    assert 'private-key' not in json.dumps(result) and 'fixture-input-key' not in json.dumps(result)


@pytest.mark.parametrize('body,ok',[
    (b'<html>OK</html>',False),
    (b'{}',False),
    (b'[]',False),
    (b'{"status":"completed","output":[]}',False),
    (b'{"status":"incomplete","incomplete_details":{"reason":"max_output_tokens"}}',True),
    (b'{"status":"failed","error":{"message":"private-key"}}',False),
])
def test_validates_responses_envelope(app, monkeypatch, body, ok):
    class Response(io.BytesIO): status=200
    class Opener:
        def open(self, req, timeout):
            assert timeout == 20
            assert req.headers['Authorization'] == 'Bearer fixture-input-key'
            return Response(body)
    monkeypatch.setattr('sessiongraph.provider_probe.request.build_opener',lambda *a: Opener())
    result=probe(app, api_key='fixture-input-key')
    assert result['ok'] is ok and result['model_calls']==1
    assert 'private-key' not in json.dumps(result)


def test_missing_credential_never_calls_model(app, monkeypatch):
    monkeypatch.setattr('sessiongraph.provider_probe.request.build_opener',lambda *a: pytest.fail('network opened'))
    result = probe(app)
    assert not result['ok'] and result['model_calls']==0 and '凭据' in result['message']


def test_probe_creates_one_summary_with_usage_and_no_content(app,monkeypatch):
    from sessiongraph.call_logs import read
    class Response(io.BytesIO):
        status=200;headers={'x-request-id':'provider-fixture'}
    class Opener:
        def open(self,*a,**k):return Response(b'{"model":"actual-model","status":"completed","usage":{"input_tokens":3,"output_tokens":1},"output":[{"type":"message","content":[{"type":"output_text","text":"private-output"}]}]}')
    monkeypatch.setattr('sessiongraph.provider_probe.request.build_opener',lambda *a:Opener())
    assert probe(app,api_key='fixture-input-key')['ok']
    rows=read(state=app['state'])['items']
    assert len(rows)==1 and rows[0]['state']=='probe_ok' and rows[0]['retry_count']==0
    assert rows[0]['response_model']=='actual-model' and rows[0]['output_tokens']==1
    assert 'fixture-input-key' not in json.dumps(rows) and 'private-output' not in json.dumps(rows)
