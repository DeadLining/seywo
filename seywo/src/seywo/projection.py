"""Deterministic Codex projection. Unsupported material remains an explicit gap."""

import hashlib
import json
import re

# Snapshot adapter revision and per-record body format are independent. A new
# lifecycle adapter must not duplicate every unchanged historical text payload.
VERSION = "codex-safe-v5"
BODY_VERSION = "codex-safe-v4"
# Conservative deterministic patterns, not a guarantee of detecting every secret.
SECRET = re.compile(
    r"(?i)(?:\b(?:sk|ghp|github_pat)-?[A-Za-z0-9_-]{16,}\b"
    r"|\bBearer\s+[A-Za-z0-9._~+/-]{8,}=*"
    r"|\b(?:api[_-]?key|access[_-]?token|password|secret)\b[\"']?\s*[:=]\s*[\"']?[^\s\"',;}]+)"
)
BINARY = re.compile(r"data:[a-zA-Z0-9.+/-]+;base64,[A-Za-z0-9+/=\r\n]+")
PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
)
HOST_PREFIXES = (
    "# AGENTS.md instructions",
    "<environment_context>",
    "<permissions instructions>",
    "<app-context>",
    "<turn_aborted>",
)


def sanitize(text: str) -> tuple[str, list[dict]]:
    parts, spans, cursor, safe_cursor = [], [], 0, 0
    matches = sorted(
        [*SECRET.finditer(text), *BINARY.finditer(text), *PRIVATE_KEY.finditer(text)],
        key=lambda m: (m.start(), -m.end()),
    )
    for match in matches:
        if match.start() < cursor:
            continue
        prefix = text[cursor : match.start()]
        parts.append(prefix)
        safe_cursor += len(prefix)
        spans.append(
            {
                "source_start": match.start(),
                "source_end": match.end(),
                "safe_start": safe_cursor,
                "safe_end": safe_cursor + 10,
                "reason": "binary_payload"
                if BINARY.fullmatch(match.group())
                else "credential_pattern",
            }
        )
        parts.append("[REDACTED]")
        safe_cursor += 10
        cursor = match.end()
    parts.append(text[cursor:])
    return "".join(parts), spans


def project(record: dict) -> dict:
    result = {
        "kind": record.get("type", "unknown"),
        "role": "unknown",
        "channel": None,
        "call_id": None,
        "tool": None,
        "text": "",
        "state": "pending",
        "reason": "unsupported_event",
        "gaps": [],
        "redactions": [],
        "text_fields": [],
        "projection_version": BODY_VERSION,
        "timestamp": record.get("timestamp"),
        "turn_id": None,
        "root_turn_id": None,
        "turn_seq": None,
    }
    event_type = record.get("type")
    payload = record.get("payload", {})
    if not isinstance(payload, dict):
        return result
    if event_type == "turn_context":
        turn_id = payload.get("turn_id")
        root_turn_id = payload.get("root_turn_id")
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        safe, redactions = sanitize(text)
        return {
            **result,
            "state": "excluded_mechanical",
            "reason": event_type,
            "turn_id": turn_id if isinstance(turn_id, str) and turn_id else None,
            "root_turn_id": root_turn_id
            if isinstance(root_turn_id, str) and root_turn_id
            else None,
            "text": safe,
            "redactions": redactions,
            "text_fields": [
                {
                    "json_path": "payload",
                    "source_start": 0,
                    "source_end": len(text),
                }
            ],
        }
    if event_type == "event_msg" and payload.get("type") in {"task_started", "task_complete", "turn_aborted"}:
        # Lifecycle coordinates are rules input only, never model text.
        return {**result, "state": "excluded_mechanical", "reason": event_type,
                "turn_event": payload["type"], "event_turn_id": payload.get("turn_id"),
                "projection_version": VERSION}
    if event_type in {"session_meta", "event_msg"}:
        # response_item is the canonical visible-body stream for this adapter.
        return {**result, "state": "excluded_mechanical", "reason": event_type}
    if event_type == "compacted":
        return {**result, "reason": "compact_reference_not_original"}
    if event_type != "response_item":
        return result
    kind = payload.get("type", "unknown")
    result.update(
        kind=kind,
        role=payload.get("role", "tool"),
        channel=payload.get("channel"),
        call_id=payload.get("call_id"),
        tool=payload.get("name"),
    )
    if kind == "reasoning" or result["role"] in {"system", "developer"}:
        return {
            **result,
            "state": "excluded_mechanical",
            "reason": "instruction_or_reasoning",
        }
    text = None
    if kind == "message" and result["role"] in {"user", "assistant"}:
        if result["role"] == "assistant" and result["channel"] not in {
            None,
            "final",
            "commentary",
        }:
            return {
                **result,
                "state": "excluded_mechanical",
                "reason": "internal_channel",
            }
        content = payload.get("content", [])
        if not isinstance(content, list):
            return {**result, "reason": "invalid_message_content"}
        pieces = []
        position = 0
        for index, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") in {
                "input_text",
                "output_text",
            }:
                if isinstance(block.get("text"), str):
                    if pieces:
                        position += 1  # Normalized newline between text blocks.
                    result["text_fields"].append(
                        {
                            "json_path": f"payload.content[{index}].text",
                            "source_start": position,
                            "source_end": position + len(block["text"]),
                        }
                    )
                    position += len(block["text"])
                    pieces.append(block["text"])
                else:
                    result["gaps"].append("invalid_text_block")
            else:
                result["gaps"].append("media_or_unknown_content")
        text = "\n".join(pieces)
        if result["role"] == "user" and text.lstrip().startswith(HOST_PREFIXES):
            # Fail closed; mixed host/user records require a validated host adapter.
            return {**result, "reason": "host_envelope_needs_adapter"}
    elif kind in {"function_call", "custom_tool_call"}:
        text = payload.get("arguments" if kind == "function_call" else "input")
    elif kind == "message" and result["role"] == "tool":
        text = payload.get("content")
    elif kind in {
        "function_call_output",
        "custom_tool_call_output",
        "tool_result",
        "tool_response",
    }:
        text = payload.get("output")
    if text is None:
        return result
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, sort_keys=True)
    if not result["text_fields"]:
        result["text_fields"] = [
            {
                "json_path": "payload."
                + (
                    "content"
                    if kind == "message" and result["role"] == "tool"
                    else "output"
                    if kind.endswith("_output")
                    or kind in {"tool_result", "tool_response"}
                    else "arguments"
                    if kind == "function_call"
                    else "input"
                ),
                "source_start": 0,
                "source_end": len(text),
            }
        ]
    safe, redactions = sanitize(text)
    if any(span["reason"] == "binary_payload" for span in redactions):
        result["gaps"].append("binary_payload_unavailable")
    return {
        **result,
        "text": safe,
        "redactions": redactions,
        "state": "projected" if not result["gaps"] else "pending",
        "reason": "visible_text" if not result["gaps"] else "partial_content",
    }


def search_tokens(text: str) -> str:
    """Versioned CJK unigram/bigram and identifier search, unrelated to LLM tokens."""
    tokens = re.findall(r"[a-z0-9_]+", text.lower())
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        tokens.extend(run)
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return " ".join(tokens)


COMPACT_HANDOFF_PREFIX = (
    "Another language model started to solve this problem and produced a summary of its thinking process. "
    "You also have access to the state of the tools that were used by that language model. "
    "Use this to build on the work that has already been done and avoid duplicating work. "
    "Here is the summary produced by the other language model, use the information in this summary to assist with your own analysis:\n"
)


def compact_body_hash(record):
    """Only a known host envelope, not text headings or keyword heuristics."""
    payload = record.get("payload")
    if record.get("type") != "compacted" or not isinstance(payload, dict):
        return None
    message = payload.get("message")
    if not isinstance(message, str) or not message.startswith(COMPACT_HANDOFF_PREFIX):
        return None
    return hashlib.sha256(message[len(COMPACT_HANDOFF_PREFIX) :].encode()).hexdigest()


def assistant_body_hash(record):
    payload = record.get("payload")
    if record.get("type") != "response_item" or not isinstance(payload, dict):
        return None
    if (
        payload.get("type") != "message"
        or payload.get("role") != "assistant"
        or payload.get("channel") not in {None, "final", "commentary"}
    ):
        return None
    content = payload.get("content")
    if (
        not isinstance(content, list)
        or not content
        or any(
            not isinstance(b, dict)
            or b.get("type") not in {"input_text", "output_text"}
            or not isinstance(b.get("text"), str)
            for b in content
        )
    ):
        return None
    return hashlib.sha256("\n".join(b["text"] for b in content).encode()).hexdigest()
