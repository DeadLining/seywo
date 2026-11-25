"""Responses transport for the detached maintainer.

The local gateway can answer either a plain Responses object or an SSE stream
even when `stream` is false. Both shapes are accepted; the stream path is used
explicitly because a reasoning model can stay silent far longer than a JSON
read timeout while it thinks.
"""

import json
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .model_contract import responses_body


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Transport:
    def __init__(self, endpoint, key, model, *, timeout=180):
        self.endpoint = endpoint
        self.key = key
        self.model = model
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def complete(self, request):
        # The frozen job's model is authoritative: settings may have moved to a
        # different alias since the job was opened, and the body still carries
        # the original one. Compare the reply against what was actually sent,
        # not against whatever the worker was constructed with.
        expected_model = request.get("model") or self.model
        body = responses_body(request)
        body["stream"] = True
        started = time.monotonic()
        try:
            with self.opener.open(Request(self.endpoint, data=json.dumps(body).encode(), headers={
                "Content-Type": "application/json", "Accept": "text/event-stream",
                "Authorization": "Bearer " + self.key,
            }), timeout=self.timeout) as response:
                payload = self._read(response)
        except HTTPError as exc:
            return {"output": None, "finish": "error", "error": f"http_{exc.code}", "usage": None,
                    "seconds": round(time.monotonic() - started, 3)}
        except (OSError, ValueError):
            return {"output": None, "finish": "error", "error": "transport_error", "usage": None,
                    "seconds": round(time.monotonic() - started, 3)}
        result = {"seconds": round(time.monotonic() - started, 3), "finish": "error",
                  "error": None, "usage": None, "output": None}
        usage = payload.get("usage") or {}
        if all(type(usage.get(key)) is int for key in ("input_tokens", "output_tokens")):
            result["usage"] = {key: usage[key] for key in ("input_tokens", "output_tokens")}
        if payload.get("model") != expected_model:
            result["error"] = "returned_model_mismatch"
            return result
        text = payload.get("_text", "")
        try:
            result["output"] = json.loads(text)
            result["finish"] = "complete" if payload.get("status") == "completed" else "length"
        except (ValueError, TypeError):
            result["finish"] = "length" if payload.get("status") == "incomplete" else "error"
            result["error"] = "invalid_json"
        if payload.get("status") not in {"completed", "incomplete"}:
            result["finish"] = "error"
            result["error"] = "provider_status_" + str(payload.get("status"))
        return result

    def _complete_json(self, *, model, instructions, payload, max_output_tokens, schema=None):
        body = {
            "model": model or self.model,
            "stream": True,
            "store": False,
            "instructions": instructions,
            "input": [{"role": "user", "content": [{"type": "input_text",
                "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}]}],
            "text": {"format": {"type": "json_object"}},
            "max_output_tokens": max_output_tokens,
        }
        if schema is not None:
            body["text"]["format"] = {"type": "json_schema", "name": "seywo_compact", "strict": True, "schema": schema}
        expected_model = body["model"]
        started = time.monotonic()
        try:
            with self.opener.open(Request(self.endpoint, data=json.dumps(body).encode(), headers={
                "Content-Type": "application/json", "Accept": "text/event-stream",
                "Authorization": "Bearer " + self.key,
            }), timeout=self.timeout) as response:
                payload_result = self._read(response)
        except HTTPError as exc:
            return {"output": None, "finish": "error", "error": f"http_{exc.code}", "usage": None,
                    "seconds": round(time.monotonic() - started, 3)}
        except (OSError, ValueError):
            return {"output": None, "finish": "error", "error": "transport_error", "usage": None,
                    "seconds": round(time.monotonic() - started, 3)}
        usage = payload_result.get("usage") or {}
        result = {"output": None, "finish": "error", "error": None,
                  "usage": {k: usage[k] for k in ("input_tokens", "output_tokens")}
                  if all(type(usage.get(k)) is int for k in ("input_tokens", "output_tokens")) else None,
                  "seconds": round(time.monotonic() - started, 3)}
        if payload_result.get("model") != expected_model:
            result["error"] = "returned_model_mismatch"
            return result
        text = payload_result.get("_text", "")
        try:
            result["output"] = json.loads(text)
            result["finish"] = "complete" if payload_result.get("status") == "completed" else "length"
        except (ValueError, TypeError):
            result["error"] = "invalid_json"
        if payload_result.get("status") not in {"completed", "incomplete"}:
            result["finish"] = "error"
            result["error"] = "provider_status_" + str(payload_result.get("status"))
        return result

    def complete_compact(self, *, model, instructions, payload, max_output_tokens=1200):
        from .compact_model import schema_for
        return self._complete_json(model=model, instructions=instructions, payload=payload,
                                   max_output_tokens=max_output_tokens, schema=schema_for(payload))

    @staticmethod
    def _read(stream):
        """Collect SSE deltas, or fall back to a single JSON document."""
        parts, final, first = [], None, True
        while True:
            raw = stream.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if first and line.startswith("{"):
                return json.loads(line)
            first = False
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "response.output_text.delta":
                parts.append(event.get("delta", ""))
            elif kind in {"response.completed", "response.incomplete"}:
                final = event.get("response") or {}
        if final is None:
            raise ValueError("stream_without_terminal_event")
        body = dict(final)
        body["_text"] = "".join(parts) or Transport._message_text(final)
        return body

    @staticmethod
    def _message_text(payload):
        return "".join(
            content.get("text", "")
            for item in payload.get("output", []) if item.get("type") == "message"
            for content in item.get("content", []) if content.get("type") == "output_text"
        )
