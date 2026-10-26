"""Timeout diagnostics report observed client phases, never guessed upstream causes."""
import json
from types import SimpleNamespace
from urllib.request import Request

import pytest

from sessiongraph import request_audit
from sessiongraph.core import init_project, load_config


@pytest.mark.parametrize('failure,phase', [('headers', 'awaiting_headers'), ('body', 'reading_body'), (None, 'response_received')])
def test_transport_phase_and_monotonic_durations(tmp_path, monkeypatch, failure, phase):
    config = load_config(init_project(tmp_path))
    config['graph_v2'] = {'recovery_policy': {'enabled': True}}
    client = SimpleNamespace(config=config, key='', model='fixture', max_output=1000,
                             timeout=180, traces=[], provider_name='selected_model')
    class Response:
        status = 200
        headers = {'x-request-id': 'fixture-request'}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit):
            if failure == 'body':
                raise TimeoutError('fixture')
            return b'{"status":"completed","output":[]}'
    class Opener:
        def open(self, req, timeout):
            if failure == 'headers':
                raise TimeoutError('fixture')
            return Response()
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: Opener())
    ticks = iter([10., 11., 12., 13., 14.])
    monkeypatch.setattr(request_audit.time, 'monotonic', lambda: next(ticks))
    req = Request('http://127.0.0.1:4317/v1/responses', data=b'{}')
    if failure:
        with pytest.raises(TimeoutError):
            request_audit.read_response(client, req, [])
    else:
        assert json.loads(request_audit.read_response(client, req, []))['status'] == 'completed'
    record = json.loads(__import__('pathlib').Path(client.traces[-1]['path']).read_text())
    assert record['transport_phase'] == phase
    assert record['timeout_seconds'] == 180
    assert record['elapsed_ms'] >= 0
    assert ('headers_ms' in record) == (failure != 'headers')
    assert ('body_ms' in record) == (failure != 'headers')
    assert record['transport_timing_scope'] == 'client_open_and_body_only'
    assert client.traces[-1]['transport_phase'] == phase
