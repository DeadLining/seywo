"""Freeze dialogue before Codex rewrites a rollout, without archiving payloads."""

import hashlib
import json
import re

from .projection import BINARY, assistant_body_hash, compact_body_hash

MAX_RECORD_BYTES = 16 * 1024 * 1024
ORIGIN = "_seywo_dialogue_v1"
MIRROR = "_seywo_summary_hash"
_CHUNK = 64 * 1024
_STRING_END = re.compile(rb'["\\]')
_SPACE = re.compile(rb'[ \t\r\n]*')

# Structural allowlist for oversized JSONL records. Normal records go through
# json.loads. The large-record reader scans discarded values without loading them.
_BLOCK = {"type": True, "text": True}
_PAYLOAD = dict.fromkeys(("type", "id", "cwd", "turn_id", "root_turn_id", "role", "channel", "call_id", "name", "message"), True)
_SOURCE = {"subagent": None}
_PAYLOAD.update(content=[_BLOCK], source=_SOURCE)
_RECORD = {"type": True, "timestamp": True, "payload": _PAYLOAD}


class _OversizedRecord:
    def __init__(self, handle, first):
        self.handle = handle
        self.buffer, self.pos = first, 0
        self.hash, self.size = hashlib.sha256(first), len(first)
        self.retained = MAX_RECORD_BYTES

    def _reserve(self, size):
        self.retained -= size
        if self.retained < 0:
            raise ValueError("source_record_too_large: retained dialogue record")

    def _peek(self):
        if self.pos == len(self.buffer):
            more = b"" if self.buffer.endswith(b"\n") else self.handle.readline(_CHUNK)
            if not more:
                return b""
            self.hash.update(more)
            self.size += len(more)
            self.buffer, self.pos = more, 0
        return self.buffer[self.pos:self.pos + 1]

    def _get(self):
        token = self._peek()
        self.pos += bool(token)
        return token

    def _space(self):
        while self._peek():
            self.pos = _SPACE.match(self.buffer, self.pos).end()
            if self.pos < len(self.buffer):
                break

    def _string(self, keep):
        pieces = []
        self.pos += 1  # opening quote
        while self._peek():
            match = _STRING_END.search(self.buffer, self.pos)
            end = match.end() if match else len(self.buffer)
            marker = match.group() if match else None
            if keep:
                self._reserve(end - self.pos)
                pieces.append(self.buffer[self.pos:end])
            self.pos = end
            if marker == b'"':
                return json.loads(b'"' + b"".join(pieces)) if keep else None
            if marker == b"\\":
                escaped = self._get()
                if keep:
                    self._reserve(len(escaped))
                    pieces.append(escaped)
        raise ValueError("invalid source record JSON")

    def value(self, schema, depth=0):
        if depth > 100:
            raise ValueError("invalid source record JSON: excessive nesting")
        self._space()
        token = self._peek()
        if token == b'"':
            return self._string(schema is True or schema is _SOURCE)
        if token in (b"{", b"["):
            obj = token == b"{"
            self.pos += 1
            end = b"}" if obj else b"]"
            result = {} if isinstance(schema, dict) else [] if isinstance(schema, list) else None
            self._space()
            if self._peek() == end:
                self.pos += 1
                return result
            while True:
                if obj:
                    if self._peek() != b'"':
                        raise ValueError("invalid source record JSON")
                    key = self._string(isinstance(result, dict))
                    self._space()
                    if self._get() != b":":
                        raise ValueError("invalid source record JSON")
                    item = self.value(schema.get(key) if isinstance(result, dict) else None, depth + 1)
                    if isinstance(result, dict) and key in schema:
                        result[key] = item
                else:
                    item = self.value(schema[0] if isinstance(result, list) else None, depth + 1)
                    if isinstance(result, list):
                        self._reserve(4)
                        result.append(item)
                self._space()
                separator = self._get()
                if separator == end:
                    return result
                if separator != b",":
                    raise ValueError("invalid source record JSON")
                self._space()
        literal = bytearray()
        while (token := self._peek()) and token not in b",]} \t\r\n":
            if len(literal) >= 128:
                raise ValueError("invalid source record JSON")
            literal += self._get()
        value = json.loads(literal)
        return value if schema is not None else None

    def record(self):
        try:
            value = self.value(_RECORD)
            self._space()
            if self._peek():
                raise ValueError("invalid source record JSON: trailing content")
        except ValueError as exc:
            if str(exc).startswith("source_record_too_large:"):
                raise
            # Match the small-record path: keep the source coordinate for a
            # malformed/unfinished line, not a partial semantic body.
            self.pos = len(self.buffer)
            while self._peek():
                self.pos = len(self.buffer)
            value = None
        return value, self.size, self.hash.hexdigest(), self.buffer.endswith(b"\n")


def _strings(value, keys):
    return {k: value[k] for k in keys if isinstance(value.get(k), str)}


def _retain(record):
    if not isinstance(record, dict):
        return {"type": "unparsed"}
    row = _strings(record, ("type", "timestamp"))
    p = record.get("payload")
    if not isinstance(p, dict):
        return row or {"type": "unparsed"}
    event, kind = row.get("type"), p.get("type")
    payload = {}
    if event == "session_meta":
        payload = _strings(p, ("id", "cwd", "source"))
        if isinstance(p.get("source"), dict) and "subagent" in p["source"]:
            payload["source"] = {"subagent": None}
    elif event == "turn_context":
        payload = _strings(p, ("turn_id", "root_turn_id"))
    elif event == "event_msg":
        payload = _strings(p, ("type", "turn_id"))
    elif event == "response_item":
        payload = _strings(p, ("type", "role", "channel", "call_id", "name"))
        if kind == "message" and p.get("role") in ("user", "assistant") and isinstance(p.get("content"), list):
            # Keep placeholders so evidence paths for mixed text/media retain
            # their original content indices. No media URL or binary is copied.
            content = []
            for block in p["content"]:
                if isinstance(block, dict) and block.get("type") in ("input_text", "output_text"):
                    kept = {"type": block["type"]}
                    if isinstance(block.get("text"), str):
                        kept["text"] = BINARY.sub("[REDACTED]", block["text"])
                    content.append(kept)
                else:
                    content.append(None)
            payload["content"] = content
            if content != p["content"]:
                original = {"type": event, "payload": {**payload, "content": p["content"]}}
                if mirror := assistant_body_hash(original):
                    row[MIRROR] = mirror
    elif event == "compacted":
        if mirror := compact_body_hash(record):
            row[MIRROR] = mirror
    if payload or event == "session_meta":
        row["payload"] = payload
    return row or {"type": "unparsed"}


def records(handle):
    """Yield one small JSON row per source line, with original byte provenance."""
    offset = 0
    while raw := handle.readline(_CHUNK):
        if len(raw) == _CHUNK and not raw.endswith(b"\n"):
            record, size, sha, complete = _OversizedRecord(handle, raw).record()
        else:
            size, sha, complete = len(raw), hashlib.sha256(raw).hexdigest(), raw.endswith(b"\n")
            try:
                record = json.loads(raw.decode("utf-8"))
            except (ValueError, RecursionError):
                record = None
        row = _retain(record) if complete or offset == 0 else {"type": "unparsed"}
        row[ORIGIN] = [offset, offset + size, sha, complete]
        encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "backslashreplace") + b"\n"
        if len(encoded) > MAX_RECORD_BYTES:
            raise ValueError("source_record_too_large: retained dialogue record")
        yield encoded
        offset += size


def coordinates(record, start):
    """Validate projection coordinates before using them as source evidence."""
    origin = record.get(ORIGIN)
    if (not isinstance(origin, list) or len(origin) != 4
            or type(origin[0]) is not int or origin[0] != start
            or type(origin[1]) is not int or not start < origin[1] < 2**63
            or not isinstance(origin[2], str) or not re.fullmatch(r"[0-9a-f]{64}", origin[2])
            or type(origin[3]) is not bool):
        raise ValueError("invalid dialogue capture coordinates")
    return origin
