import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from sessiongraph.core import init_project, load_config
from sessiongraph.extraction import create_extractor


@pytest.fixture
def cfg(tmp_path):
    config = load_config(init_project(tmp_path))
    config["provider"].update(
        protocol="responses",
        endpoint="http://127.0.0.1:4317/openai/v1/responses",
        model="deepseek-v4.1-flash",
        auth="none",
    )
    return config


def test_responses_exact_url_payload_and_output(cfg):
    seen = []
    mode = ["completed"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            seen.append(
                (
                    self.path,
                    dict(self.headers),
                    json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                )
            )
            payload = {
                "status": mode[0],
                "output": [
                    {"type": "reasoning", "summary": []},
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {"summary": "ok", "records": [], "relations": []}
                                ),
                            }
                        ],
                    },
                ],
            }
            if mode[0] == "refusal":
                payload["status"] = "completed"
                payload["output"][1]["content"] = [{"type": "refusal", "refusal": "no"}]
            if mode[0] == "missing":
                payload.pop("status")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cfg["provider"]["endpoint"] = f"http://127.0.0.1:{server.server_port}/openai/v1/responses"
    try:
        extractor = create_extractor(cfg)
        assert extractor([], cfg["policy"])["summary"] == "ok"
        path, headers, body = seen[0]
        assert path == "/openai/v1/responses"
        assert "Authorization" not in headers
        assert body["model"] == "deepseek-v4.1-flash"
        assert body["stream"] is False and body["store"] is False
        assert body["text"]["format"]["type"] == "json_object"
        assert "max_output_tokens" in body and "messages" not in body
        assert extractor.provenance["kind"] == "responses"
        for status in ["incomplete", "failed", "refusal", "missing"]:
            mode[0] = status
            with pytest.raises(ValueError):
                extractor([], cfg["policy"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_provider_auth_and_url_validation(cfg, monkeypatch):
    monkeypatch.delenv("GRAPH_LLM_API_KEY", raising=False)
    cfg["provider"]["auth"] = "bearer"
    with pytest.raises(ValueError, match="key"):
        create_extractor(cfg)
    monkeypatch.setenv("GRAPH_LLM_API_KEY", "fixture-key")
    assert create_extractor(cfg).key == "fixture-key"
    cfg["provider"]["endpoint"] = "http://example.com/v1/responses"
    with pytest.raises(ValueError):
        create_extractor(cfg)
    cfg["provider"]["endpoint"] = "https://example.com/v1/chat/completions"
    with pytest.raises(ValueError):
        create_extractor(cfg)
    cfg["provider"]["protocol"] = "unknown"
    with pytest.raises(ValueError):
        create_extractor(cfg)


def test_gateway_worker_credentials_are_scoped(tmp_path, cfg, monkeypatch):
    from sessiongraph.gateway_worker import gateway_credentials

    monkeypatch.delenv("GRAPH_LLM_API_KEY", raising=False)
    path = tmp_path / "codex.toml"
    path.write_text(
        '[model_providers.local]\nbase_url="http://127.0.0.1:4317/openai/v1"\nexperimental_bearer_token="fixture-local-token"\n'
    )
    with gateway_credentials(cfg, path):
        import os

        assert os.environ["GRAPH_LLM_API_KEY"] == "fixture-local-token"
    assert "GRAPH_LLM_API_KEY" not in __import__("os").environ
    path.write_text(
        '[model_providers.other]\nbase_url="https://unrelated.example/v1"\nexperimental_bearer_token="unrelated"\n'
    )
    with pytest.raises(ValueError, match="matching"):
        with gateway_credentials(cfg, path):
            pass


def test_gateway_worker_requires_explicit_consent(tmp_path, monkeypatch):
    from sessiongraph.gateway_worker import main

    monkeypatch.setattr("sys.argv", ["gateway-worker", "--config", str(tmp_path / "not-existing")])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


@pytest.mark.parametrize("strict", [False, True])
def test_graph_prompt_is_compact_but_provider_schema_is_complete(cfg, monkeypatch, strict):
    from sessiongraph import extraction
    from sessiongraph.graph_contract import policy, SCHEMA
    sent = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit):
            return json.dumps({"status":"completed", "output":[{"type":"message", "role":"assistant", "content":[{"type":"output_text", "text":json.dumps({"schema_version":"sgv2-1","nodes":[],"relations":[],"unresolved":[],"coverage":{"outcome":"no_durable_item"}})}]}]}).encode()
    class Opener:
        def open(self, req, **kwargs):
            sent.append(json.loads(req.data))
            return Response()
    monkeypatch.setattr(extraction.request, "build_opener", lambda *args: Opener())
    contract = policy([])
    contract["strict_json_schema"] = strict
    create_extractor(cfg)([], contract)
    prompt = json.loads(sent[0]["instructions"].split("\n", 1)[1])
    assert "json_schema" not in prompt and "output_contract" in prompt
    if strict:
        evidence = sent[0]["text"]["format"]["schema"]["properties"]["nodes"]["items"]["properties"]["evidence"]["items"]
        assert set(evidence["properties"]) == {"anchor_id"}
        assert evidence["additionalProperties"] is False
    else: assert sent[0]["text"]["format"] == {"type":"json_object"}
    assert contract["json_schema"] == policy([])["json_schema"]


def test_graph_output_budget_is_bounded_and_does_not_change_legacy(cfg):
    before = cfg["provider"]["max_output_tokens"]
    assert create_extractor(cfg).max_output == before
    cfg["graph_v2"] = {"enabled":True}
    assert create_extractor(cfg).max_output == 12000
    assert cfg["provider"]["max_output_tokens"] == before
    cfg["graph_v2"]["max_output_tokens"] = 8000
    assert create_extractor(cfg).max_output == 8000
    cfg["graph_v2"]["max_output_tokens"] = 999999
    with pytest.raises(ValueError): create_extractor(cfg)


@pytest.mark.parametrize("reported_complete", [False, True])
def test_output_limit_is_not_a_retryable_json_error(cfg, monkeypatch, reported_complete):
    from sessiongraph import extraction
    from sessiongraph.memory_retry import retryable
    envelope = {"status":"completed" if reported_complete else "incomplete", "usage":{"output_tokens":cfg["provider"]["max_output_tokens"]},
        "output":[{"type":"message", "role":"assistant", "content":[{"type":"output_text", "text":'{"nodes":['}]}]}
    if not reported_complete: envelope["incomplete_details"] = {"reason":"max_output_tokens"}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return json.dumps(envelope).encode()
    class Opener:
        def open(self, *args, **kwargs): return Response()
    monkeypatch.setattr(extraction.request, "build_opener", lambda *args: Opener())
    ex = create_extractor(cfg)
    with pytest.raises(extraction.ProviderError) as error: ex([], cfg["policy"])
    assert error.value.code == "provider_output_limit" and not retryable(error.value)
    assert ex.last_usage == envelope["usage"]


@pytest.mark.parametrize("protocol", ["responses", "chat-completions"])
@pytest.mark.parametrize("anchor", ["a1", "a_foreign", None])
@pytest.mark.parametrize('effective', [False, True])
def test_graph_wire_anchors_round_trip_and_reject_invalid(cfg, monkeypatch, protocol, anchor, effective):
    from sessiongraph import extraction
    from sessiongraph.graph_contract import policy
    cfg["provider"]["protocol"] = protocol
    for key, value in (("base_url_env", "http://127.0.0.1:4317/openai/v1"), ("api_key_env", "test-only"), ("model_env", "fixture")):
        monkeypatch.setenv(cfg["provider"][key], value)
    sent = []
    selected = {"nodes": [{"evidence": [{"anchor_id": anchor}]}], "relations": [], "unresolved": [], "coverage": {"outcome": "candidates_found"}}
    if effective:
        selected['nodes'][0]['assertion'] = dict(
            subject=dict(anchor_id=anchor, text='R17'), predicate=dict(anchor_id=anchor, text='completed'),
            value=dict(anchor_id=anchor, text='completed'), unit=[], conditions=[])
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit):
            text = json.dumps(selected)
            if protocol == "responses":
                return json.dumps({"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}]}).encode()
            return json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": text}}]}).encode()
    class Opener:
        def open(self, req, **kwargs):
            sent.append(json.loads(req.data))
            return Response()
    monkeypatch.setattr(extraction.request, "build_opener", lambda *args: Opener())
    source = "Run R17 **completed**; report `/tmp/r17/report.json`."
    extractor = create_extractor(cfg)
    if anchor == "a1":
        result = extractor([{"id": "e1", "text": source, "role": "tool"}], policy([], effective=effective))
        assert result["nodes"][0]["evidence"] == [{"entry_id": "e1", "quote": source}]
        if effective:
            assert result['nodes'][0]['assertion'][0] == dict(subject='R17', predicate='completed', value='completed', unit='none', conditions=[])
    else:
        with pytest.raises(extraction.ProviderError) as exc:
            extractor([{"id": "e1", "text": source, "role": "tool"}], policy([], effective=effective))
        assert exc.value.code == "provider_evidence_reference"
    if protocol == "responses":
        wire = json.loads(sent[0]["input"][0]["content"][0]["text"])
    else:
        wire = json.loads(sent[0]["messages"][-1]["content"])
    entry = wire["untrusted_transcript"][0]
    assert "text" not in entry
    assert entry["source_spans"] == [{"anchor_id": "a1", "text": source}]
