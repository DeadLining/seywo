"""Small model boundary for Seywo's turn-delta pipeline.

The model sees one native turn plus a tiny state digest. It returns titles and
indexes only. Source ranges, identities, phases, branches and publication stay
owned by the runtime.
"""

import json
from copy import deepcopy

from .contracts import ITEM_TYPES
from .titles import validate_title

POLICY = "seywo-delta-2"
MAX_ITEMS = 8
MAX_TITLE = 40
MAX_SOURCE_CHARS = 2400
MAX_SOURCE_REFS = 32

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "items": {"type": "array", "maxItems": MAX_ITEMS, "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "title": {"type": "string", "maxLength": MAX_TITLE},
                "type": {"type": "string", "enum": list(ITEM_TYPES)},
                "attribution": {"type": "string", "enum": ["user", "assistant", "inference"]},
                "source_indexes": {"type": "array", "maxItems": MAX_SOURCE_REFS, "items": {"type": "integer", "minimum": 0}},
            },
            "required": ["title", "type", "attribution", "source_indexes"],
        }},
        "line": {"type": "object", "additionalProperties": False, "properties": {
            "action": {"type": "string", "enum": ["continue", "new_phase", "branch", "return"]},
            "title": {"type": "string", "maxLength": 32},
        }, "required": ["action", "title"]},
        "focus_item": {"type": ["integer", "null"], "minimum": 0},
        "open_item_indexes": {"type": "array", "maxItems": 4, "items": {"type": "integer", "minimum": 0}},
        "resolve_titles": {"type": "array", "maxItems": 4, "items": {"type": "string", "maxLength": MAX_TITLE}},
        "no_work_source_indexes": {"type": "array", "maxItems": 8, "items": {"type": "integer", "minimum": 0}},
    },
    "required": ["items", "line", "focus_item", "open_item_indexes", "resolve_titles", "no_work_source_indexes"],
}

_ITEM_PROMPT = """你只提取这一轮相对于已有主线的语义变化。
只记录不可替代的工作节点，最多6个；同一件事的多个进展合并成1个节点。
节点标题是高度凝练的短标题，最多40字，不写解释、不写文件列表、不写测试清单。
用户的问题、要求、决定必须保留；助手的方案、结果、下一步按原意标为 assistant。
工具调用和工具返回已经被过滤，不能猜测未提供的工具事实。
line.action=continue 表示仍在当前主线；new_phase 表示人类目标明显改变；branch 表示进入隔离支线；return 表示从支线回到主线。
new_phase 时 focus_item 必须指向本轮用户提出的新目标节点；该节点须引用用户来源。续问、修复、复测、助手进展用 continue，不开新阶段。相邻阶段标题必须清楚区分目标。
纯社交、礼貌确认且没有工作语义的用户来源放入 no_work_source_indexes；工作问题、要求、纠正和决定不能放进去。
只输出符合给定 JSON 形状的对象，不要 Markdown，不要复述输入，不要输出引用原文。"""


def _source_text(source):
    text = str(source.get("text", ""))
    if len(text) <= MAX_SOURCE_CHARS:
        return text
    # Keep the beginning and end of a long assistant response. The evidence map
    # remains the full source range; this only limits model input.
    left = MAX_SOURCE_CHARS // 2
    right = MAX_SOURCE_CHARS - left
    return text[:left] + "\n…[中间内容省略]…\n" + text[-right:]


def request_payload(sources, previous):
    safe_sources = []
    for index, source in enumerate(sources):
        safe_sources.append({
            "index": index,
            "role": source["role"],
            "turn_id": source.get("turn_id"),
            "text": _source_text(source),
        })
    context = {
        "phase": previous.get("phase"),
        "goals": previous.get("goals", [])[:2],
        "focus": previous.get("focus"),
        "open": previous.get("open", [])[:4],
        "recent": previous.get("recent", [])[-8:],
    }
    return {"policy": POLICY, "sources": safe_sources, "state": context, "shape": SCHEMA}


def instructions():
    return _ITEM_PROMPT + "\n字段定义：" + json.dumps(SCHEMA, ensure_ascii=False, separators=(",", ":"))


def _bound_title(value, limit):
    value = value.strip() if isinstance(value, str) else value
    if isinstance(value, str) and len(value) > limit:
        return value[:limit - 1].rstrip() + "…"
    return value


def validate(output, source_count):
    output = deepcopy(output)
    if not isinstance(output, dict) or set(output) != {"items", "line", "focus_item", "open_item_indexes", "resolve_titles", "no_work_source_indexes"}:
        raise ValueError("delta_invalid_fields")
    items = output["items"]
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise ValueError("delta_invalid_items")
    for item in items:
        if not isinstance(item, dict) or set(item) != {"title", "type", "attribution", "source_indexes"}:
            raise ValueError("delta_invalid_item_fields")
        item["title"] = _bound_title(item["title"], MAX_TITLE)
        validate_title(item["title"])
        if item["type"] not in ITEM_TYPES:
            raise ValueError("delta_invalid_item")
        if item["attribution"] not in {"user", "assistant", "inference"}:
            raise ValueError("delta_invalid_attribution")
        indexes = item["source_indexes"]
        if not isinstance(indexes, list) or not indexes or len(indexes) > MAX_SOURCE_REFS:
            raise ValueError("delta_invalid_source_indexes")
        if any(type(i) is not int or i < 0 or i >= source_count for i in indexes):
            raise ValueError("delta_unknown_source_index")
        if len(indexes) != len(set(indexes)):
            raise ValueError("delta_duplicate_source_index")
    line = output["line"]
    if not isinstance(line, dict) or set(line) != {"action", "title"} or line["action"] not in {"continue", "new_phase", "branch", "return"}:
        raise ValueError("delta_invalid_line")
    line["title"] = _bound_title(line["title"], 32)
    if not isinstance(line["title"], str) or not line["title"]:
        raise ValueError("delta_invalid_line_title")
    if output["focus_item"] is not None and (type(output["focus_item"]) is not int or output["focus_item"] < 0 or output["focus_item"] >= len(items)):
        raise ValueError("delta_invalid_focus")
    for key in ("open_item_indexes",):
        values = output[key]
        if not isinstance(values, list) or any(type(i) is not int or i < 0 or i >= len(items) for i in values) or len(values) != len(set(values)):
            raise ValueError("delta_invalid_item_index")
    if not isinstance(output["resolve_titles"], list) or any(not isinstance(v, str) for v in output["resolve_titles"]):
        raise ValueError("delta_invalid_resolve_titles")
    output["resolve_titles"] = [_bound_title(v, MAX_TITLE) for v in output["resolve_titles"]]
    no_work = output["no_work_source_indexes"]
    if not isinstance(no_work, list) or any(type(i) is not int or i < 0 or i >= source_count for i in no_work) or len(no_work) != len(set(no_work)):
        raise ValueError("delta_invalid_no_work_indexes")
    return output
