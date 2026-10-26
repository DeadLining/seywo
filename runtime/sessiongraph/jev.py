"""Bounded TypeSafe judgments; only explicit write-side callers may instantiate this."""

from __future__ import annotations
import json
import math
import os
import stat
import ssl
import http.client
from types import SimpleNamespace
from .failures import Failure, transport
from .resource_policy import AdmissionDenied
from pathlib import Path
from urllib import request, parse, error
from .extraction import NoRedirect


class JevUnavailable(Failure):
    def __init__(self, code="jev_unavailable", retryable=False):
        super().__init__(code, retryable=retryable, provider="jev")


def malformed(reason, index=None):
    failure = JevUnavailable("jev_malformed_response", retryable=True)
    prefix = "response" if index is None else f"response.answers[{index}]"
    failure.detail = f"{prefix}:{reason}"
    return failure


def validate_answers(result, questions):
    """One strict probability contract for network and custom judge adapters."""
    if not isinstance(result, dict):
        raise malformed("envelope_type")
    answers = result.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise malformed("answer_keys")
    for i, (key, question) in enumerate(questions.items()):
        answer = answers[key]
        if not isinstance(answer, dict):
            raise malformed("answer_type", i)
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, dict) or set(probabilities) != set(question["criteria"]):
            raise malformed("probability_keys", i)
        if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
               for v in probabilities.values()):
            raise malformed("probability_value", i)
        if abs(sum(probabilities.values()) - 1) > 0.02 + 1e-9:
            raise malformed("probability_sum", i)
        choice = answer.get("choice")
        if not isinstance(choice, str) or choice not in probabilities:
            raise malformed("choice_key", i)
        if probabilities[choice] < max(probabilities.values()):
            raise malformed("choice_not_maximum", i)
    return answers


def credential_path(home=None):
    base = Path(home) if home is not None else Path(os.environ.get(
        "SESSIONGRAPH_RUNTIME_HOME",
        str(Path.home() / "Library/Application Support/local-ai-gateway/sessiongraph"),
    ))
    return base / "credentials/jev.json"


def load_saved_key(home=None):
    path = credential_path(home)
    try:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            meta = os.fstat(directory)
            if meta.st_uid != os.getuid() or stat.S_IMODE(meta.st_mode) != 0o700:
                raise JevUnavailable("jev_credentials_permissions")
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(fd, "rb") as stream:
            meta = os.fstat(stream.fileno())
            if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                    or stat.S_IMODE(meta.st_mode) != 0o600 or meta.st_size > 16384):
                raise JevUnavailable("jev_credentials_permissions")
            value = json.loads(stream.read(16385))
        key = value.get("api_key") if isinstance(value, dict) else None
        if (not isinstance(key, str) or not key.strip() or len(key) > 2048
                or any(ord(c) < 32 or ord(c) == 127 for c in key)):
            raise JevUnavailable("jev_credentials_invalid")
        return key.strip()
    except FileNotFoundError:
        return ""
    except (OSError, ValueError) as exc:
        if isinstance(exc, JevUnavailable):
            raise
        raise JevUnavailable("jev_credentials_invalid") from None


def saved_key_configured(home):
    try:
        return bool(load_saved_key(home))
    except JevUnavailable:
        return False


def verified_tls_context():
    """Keep system/custom trust and add public CAs for Python.org macOS runtimes."""
    context = ssl.create_default_context()
    try:
        import certifi
    except ImportError:
        if not context.get_ca_certs():
            raise JevUnavailable("jev_tls_trust_unavailable") from None
    else:
        try:
            context.load_verify_locations(cafile=certifi.where())
        except (OSError, ssl.SSLError):
            raise JevUnavailable("jev_tls_trust_unavailable") from None
    return context


def https_proxy_handler():
    """Resolve the HTTPS route without npm variables suppressing system proxies.

    urllib on macOS chooses environment OR system proxies. It treats every
    *_proxy name as a protocol, including npm_config_https_proxy, so that
    unrelated dictionary can silently suppress the real system HTTPS proxy.
    Keep standard HTTPS/ALL overrides and otherwise read the system settings.
    ProxyHandler continues to honor the normal host bypass rules.
    """
    environment = request.getproxies_environment()
    proxy = environment.get("https") or environment.get("all")
    if not proxy:
        system_reader = getattr(request, "getproxies_macosx_sysconf", None)
        if system_reader is None:
            system_reader = getattr(request, "getproxies_registry", None)
        system = system_reader() if system_reader else {}
        proxy = system.get("https")
    return request.ProxyHandler({"https": proxy} if proxy else {})


class JevClient:
    def __init__(self, config):
        self.model = config.get("model", "jev-latest")
        self.endpoint = config.get("endpoint", "https://api.typesafe.ai/v1/systemone")
        u = parse.urlsplit(self.endpoint)
        if (
            u.scheme != "https"
            or u.hostname != "api.typesafe.ai"
            or u.port not in (None, 443)
            or u.path != "/v1/systemone"
            or u.username
            or u.password
            or u.query
            or u.fragment
        ):
            raise ValueError("jev_endpoint_not_allowed")
        self.key = os.environ.get(config.get("api_key_env", "TYPESAFE_API_KEY"), "")
        self.timeout = min(90, max(1, config.get("timeout_seconds", 45)))
        self.last_usage = None
        self.calls = 0
        self.audit_config = None
        self.traces = []

    def evaluate(self, state, questions):
        from .content_boundary import assert_text_only
        assert_text_only(state)
        assert_text_only(questions)
        if not questions or len(questions) > 32:
            raise ValueError("jev_question_limit")
        body = json.dumps(
            {"model": self.model, "state": state, "questions": questions}, ensure_ascii=False
        ).encode()
        if len(body) > 64000:
            raise ValueError("jev_input_budget")
        try:
            key = self.key or load_saved_key()
            if not key:
                raise JevUnavailable("jev_not_configured")
            req = request.Request(
                self.endpoint, data=body,
                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            )
            tls = request.HTTPSHandler(context=verified_tls_context())
            proxy = https_proxy_handler()
            self.calls += 1
            if self.audit_config:
                from .request_audit import read_response
                adapter=SimpleNamespace(config=self.audit_config,key=key,model=self.model,
                    max_output=None,timeout=self.timeout,traces=self.traces,provider_name="jev")
                raw=read_response(adapter,req,[NoRedirect,tls,proxy])
            else:
                with request.build_opener(NoRedirect, tls, proxy).open(req, timeout=self.timeout) as response:
                    raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise JevUnavailable("jev_response_too_large")
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeError):
                raise malformed("invalid_json") from None
            answers = validate_answers(result, questions)
            self.last_usage = result.get("usage")
            return {
                "answers": answers,
                "model": result.get("model", self.model),
                "usage": self.last_usage,
            }
        except JevUnavailable:
            raise
        except AdmissionDenied:
            self.calls = max(0, self.calls - 1)
            raise
        except (error.URLError, OSError, http.client.HTTPException) as exc:
            info=transport(exc,"jev")
            failure=JevUnavailable(info.code,info.retryable)
            failure.detail, failure.exception_type=info.detail,info.exception_type
            raise failure from None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise malformed("invalid_envelope") from None
