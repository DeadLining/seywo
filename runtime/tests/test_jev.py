import json
from urllib.error import HTTPError
import pytest
from sessiongraph import jev

QUESTIONS = {
    "q": {
        "type": "choice",
        "instructions": "fixture",
        "criteria": {"same": "same", "different": "different"},
    }
}


class Response:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, size):
        return self.data[:size]


def client(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-key")
    return jev.JevClient({})


def test_contract_and_no_provider_access_until_evaluate(monkeypatch):
    calls = []

    def open_request(req, timeout):
        calls.append(json.loads(req.data))
        assert req.get_header("Authorization") == "Bearer synthetic-key"
        return Response(
            json.dumps(
                {
                    "answers": {
                        "q": {"choice": "same", "probabilities": {"same": 0.99, "different": 0.01}}
                    },
                    "model": "fixture",
                    "usage": {"total_tokens": 4},
                }
            ).encode()
        )

    monkeypatch.setattr(
        jev.request,
        "build_opener",
        lambda *args: type("Opener", (), {"open": staticmethod(open_request)})(),
    )
    c = client(monkeypatch)
    assert calls == []
    result = c.evaluate({"fixture": True}, QUESTIONS)
    assert calls == [{"model": "jev-latest", "state": {"fixture": True}, "questions": QUESTIONS}]
    assert result["model"] == "fixture" and c.calls == 1


@pytest.mark.parametrize(
    "probabilities,choice",
    [
        ({"same": 0.2, "different": 0.8}, "same"),
        ({"same": 0.9, "different": 0.9}, "same"),
        ({"same": True, "different": 0}, "same"),
        ({"same": float("nan"), "different": 0}, "same"),
    ],
)
def test_rejects_inconsistent_probability_contract(monkeypatch, probabilities, choice):
    raw = json.dumps(
        {"answers": {"q": {"choice": choice, "probabilities": probabilities}}}
    ).encode()
    monkeypatch.setattr(
        jev.request,
        "build_opener",
        lambda *args: type("Opener", (), {"open": staticmethod(lambda *a, **k: Response(raw))})(),
    )
    with pytest.raises(jev.JevUnavailable, match="jev_malformed_response"):
        client(monkeypatch).evaluate({}, QUESTIONS)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://api.typesafe.ai/v1/systemone",
        "https://other.example/v1/systemone",
        "https://api.typesafe.ai:444/v1/systemone",
        "https://api.typesafe.ai/v1/systemone?key=x",
    ],
)
def test_endpoint_must_be_official(endpoint):
    with pytest.raises(ValueError, match="jev_endpoint_not_allowed"):
        jev.JevClient({"endpoint": endpoint})


def test_missing_key_is_nonretryable_and_makes_no_network_call(monkeypatch, tmp_path):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("SESSIONGRAPH_RUNTIME_HOME", str(tmp_path))
    monkeypatch.setattr(jev.request, "build_opener", lambda *a: pytest.fail("network request"))
    with pytest.raises(jev.JevUnavailable) as err:
        jev.JevClient({}).evaluate({}, QUESTIONS)
    assert err.value.code == "jev_not_configured" and not err.value.retryable


@pytest.mark.parametrize("status,retry", [(429, True), (503, True), (401, False)])
def test_http_failures_are_classified_without_response_body(monkeypatch, status, retry):
    def fail(*a, **k):
        raise HTTPError("https://api.typesafe.ai/v1/systemone", status, "secret body", {}, None)

    monkeypatch.setattr(
        jev.request, "build_opener", lambda *a: type("Opener", (), {"open": staticmethod(fail)})()
    )
    with pytest.raises(jev.JevUnavailable) as err:
        client(monkeypatch).evaluate({}, QUESTIONS)
    assert err.value.code == f"jev_http_{status}" and err.value.retryable == retry
    assert "secret" not in str(err.value)


def test_public_ca_bundle_is_loaded_without_disabling_verification():
    import ssl

    context = jev.verified_tls_context()
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.get_ca_certs()


def test_certificate_failure_is_classified_and_not_retried(monkeypatch):
    import ssl
    from urllib.error import URLError

    def fail(*args, **kwargs):
        raise URLError(ssl.SSLCertVerificationError("fixture"))

    monkeypatch.setattr(
        jev.request,
        "build_opener",
        lambda *args: type("Opener", (), {"open": staticmethod(fail)})(),
    )
    with pytest.raises(jev.JevUnavailable) as err:
        client(monkeypatch).evaluate({}, QUESTIONS)
    assert err.value.code == "jev_tls_certificate" and not err.value.retryable


def test_saved_key_direct_usage_rotation_and_no_subprocess(monkeypatch, tmp_path):
    import subprocess
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("SESSIONGRAPH_RUNTIME_HOME", str(tmp_path))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("subprocess"))
    path = jev.credential_path()
    path.parent.mkdir(mode=0o700)
    path.write_text(json.dumps({"api_key": "synthetic-one"}))
    path.chmod(0o600)
    seen = []
    def response(req, timeout):
        seen.append(req.get_header("Authorization"))
        return Response(b'{"answers":{"q":{"choice":"same","probabilities":{"same":1,"different":0}}}}')
    monkeypatch.setattr(jev.request, "build_opener", lambda *a: type("Opener", (), {"open": staticmethod(response)})())
    c = jev.JevClient({})
    c.evaluate({}, QUESTIONS)
    path.write_text(json.dumps({"api_key": "synthetic-two"}))
    c.evaluate({}, QUESTIONS)
    assert seen == ["Bearer synthetic-one", "Bearer synthetic-two"]
    assert c.calls == 2 and jev.saved_key_configured(tmp_path)


@pytest.mark.parametrize("fault", ["public", "symlink", "directory", "bad_json", "bad_key"])
def test_unsafe_saved_key_fails_closed(monkeypatch, tmp_path, fault):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("SESSIONGRAPH_RUNTIME_HOME", str(tmp_path))
    path = jev.credential_path()
    path.parent.mkdir(mode=0o700)
    path.write_text('{"api_key":"synthetic"}')
    path.chmod(0o600)
    if fault == "public": path.chmod(0o644)
    elif fault == "symlink":
        dest = tmp_path / "other"
        path.rename(dest)
        path.symlink_to(dest)
    elif fault == "directory":
        path.unlink()
        path.mkdir()
    elif fault == "bad_json": path.write_text("secret invalid json")
    elif fault == "bad_key": path.write_text('{"api_key":""}')
    monkeypatch.setattr(jev.request, "build_opener", lambda *a: pytest.fail("network"))
    with pytest.raises(jev.JevUnavailable) as err:
        jev.JevClient({}).evaluate({}, QUESTIONS)
    assert not err.value.retryable and "secret" not in str(err.value)
    assert not jev.saved_key_configured(tmp_path)


@pytest.mark.parametrize('override,expected', [(None,'http://127.0.0.1:7897'),('http://127.0.0.1:9001','http://127.0.0.1:9001')])
def test_npm_proxy_variables_do_not_hide_system_https_proxy(monkeypatch, override, expected):
    import os
    for key in list(os.environ):
        if key.lower().endswith('_proxy'):monkeypatch.delenv(key)
    monkeypatch.setenv('npm_config_https_proxy','http://127.0.0.1:7897')
    monkeypatch.setenv('npm_config_proxy','http://127.0.0.1:7897')
    monkeypatch.setattr(jev.request,'getproxies_macosx_sysconf',lambda:{'https':'http://127.0.0.1:7897'},raising=False)
    if override:monkeypatch.setenv('HTTPS_PROXY',override)
    handler=jev.https_proxy_handler()
    assert handler.proxies=={'https':expected}


def test_all_proxy_supported_without_importing_npm_protocols(monkeypatch):
    monkeypatch.setattr(jev.request,'getproxies_environment',lambda:{'all':'http://127.0.0.1:7897','npm_config':'ignored'})
    assert jev.https_proxy_handler().proxies=={'https':'http://127.0.0.1:7897'}


@pytest.mark.parametrize('bypass',[False,True])
@pytest.mark.parametrize('status',[200,451])
def test_real_opener_routes_and_audits_jev_in_npm_environment(tmp_path, monkeypatch, bypass, status):
    import os,io
    from email.message import Message
    from urllib.response import addinfourl
    for key in list(os.environ):
        if key.lower().endswith('_proxy'):monkeypatch.delenv(key)
    monkeypatch.setenv('npm_config_https_proxy','http://127.0.0.1:7897')
    monkeypatch.setenv('npm_config_proxy','http://127.0.0.1:7897')
    if bypass:monkeypatch.setenv('NO_PROXY','api.typesafe.ai')
    monkeypatch.setattr(jev.request,'getproxies_macosx_sysconf',lambda:{'https':'http://127.0.0.1:7897'},raising=False)
    seen=[]
    def https_open(handler,req):
        seen.append((req.host,req._tunnel_host))
        if status==451:
            raise HTTPError(req.full_url,451,'Unavailable',Message(),io.BytesIO(b'{"status":451}'))
        body={'answers':{'q':{'choice':'same','probabilities':{'same':1.0,'different':0.0}}}}
        response=addinfourl(io.BytesIO(json.dumps(body).encode()),Message(),req.full_url,200)
        response.msg='OK'
        return response
    # Exercise the real urllib opener/ProxyHandler, stub only the network I/O.
    monkeypatch.setattr(jev.request.HTTPSHandler,'https_open',https_open)
    c=client(monkeypatch);c.audit_config={'database':str(tmp_path/'index.sqlite3')}
    if status==451:
        with pytest.raises(jev.JevUnavailable,match='jev_http_451'):c.evaluate({},QUESTIONS)
    else:c.evaluate({},QUESTIONS)
    assert seen==([('api.typesafe.ai',None)] if bypass else [('127.0.0.1:7897','api.typesafe.ai')])
    audit=json.loads(__import__('pathlib').Path(c.traces[-1]['path']).read_text())
    assert audit['http_status']==status
    assert audit['transport']['connect_host']==('api.typesafe.ai' if bypass else '127.0.0.1')
    assert audit['transport']['mode']==('direct' if bypass else 'https_connect_proxy')
    assert 'synthetic-key' not in json.dumps(audit)


@pytest.mark.parametrize('value,reason', [
    ([], 'envelope_type'),
    ({'answers': {}}, 'answer_keys'),
    ({'answers': {'q': {'choice': 'same', 'probabilities': {'same': .2, 'different': .8}}}}, 'choice_not_maximum'),
    ({'answers': {'q': {'choice': 'same', 'probabilities': {'same': .9, 'different': .9}}}}, 'probability_sum'),
])
def test_malformed_answers_are_diagnosed_and_retryable(value, reason):
    with pytest.raises(jev.JevUnavailable) as caught:
        jev.validate_answers(value, QUESTIONS)
    assert caught.value.retryable
    assert caught.value.detail.endswith(reason)


def test_invalid_json_diagnosis_without_response_leak(monkeypatch):
    raw = b'not-json provider-private-response'
    monkeypatch.setattr(jev.request, 'build_opener', lambda *a: type('Opener', (), {
        'open': staticmethod(lambda *a, **kw: Response(raw))})())
    with pytest.raises(jev.JevUnavailable) as caught:
        client(monkeypatch).evaluate({}, QUESTIONS)
    assert caught.value.retryable and caught.value.detail == 'response:invalid_json'
    assert 'provider-private-response' not in caught.value.detail
