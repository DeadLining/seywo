"""The frozen job's model must be the identity checked against the reply.

A compact job freezes its model alias. The settings page may point at another
alias by the time the run happens, and the worker is constructed from settings.
Comparing the reply against the worker's model rejected perfectly good answers.
"""

import json

import pytest

from seywo import transport as transport_module
from seywo.transport import Transport


class _Stream:
    def __init__(self, lines):
        self._lines = list(lines)

    def readline(self):
        return self._lines.pop(0) if self._lines else b""


class _Response(_Stream):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _sse(payload):
    return [
        b"data: " + json.dumps({"type": "response.output_text.delta", "delta": '{"items": []}'}).encode() + b"\n",
        b"data: " + json.dumps({"type": "response.completed", "response": payload}).encode() + b"\n",
    ]


@pytest.fixture
def wire(monkeypatch):
    monkeypatch.setattr(transport_module, "responses_body", lambda request: dict(request))


def _complete(monkeypatch, returned_model):
    payload = {"model": returned_model, "status": "completed", "output": [],
               "usage": {"input_tokens": 10, "output_tokens": 5}}
    worker = Transport("http://127.0.0.1:1/responses", "key", "current-settings-alias")
    monkeypatch.setattr(worker, "opener", type("Opener", (), {
        "open": staticmethod(lambda *a, **k: _Response(_sse(payload)))})(), raising=False)
    return worker.complete({"model": "frozen-job-alias", "phase": "extract"})


def test_reply_is_matched_against_the_frozen_job_model(monkeypatch, wire):
    result = _complete(monkeypatch, "frozen-job-alias")
    assert result["error"] is None
    assert result["finish"] == "complete"


def test_a_reply_from_a_different_model_is_still_rejected(monkeypatch, wire):
    result = _complete(monkeypatch, "some-other-alias")
    assert result["error"] == "returned_model_mismatch"
