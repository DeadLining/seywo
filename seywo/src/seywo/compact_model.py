"""Compact-level semantic contract.

The model sees a bounded batch of user/assistant turns and returns only the
conversation segments and durable changes. Source text, identities, evidence,
and narrative structure remain runtime-owned.
"""

from copy import deepcopy

from .contracts import ITEM_TYPES
from .titles import validate_title

POLICY = "seywo-compact-segment-2"
MAX_TURNS = 24
MAX_SEGMENTS = 8
MAX_CHANGES = 8
MAX_TITLE = 40
MAX_USER_CHARS = 1200
MAX_ASSISTANT_CHARS = 2200
MAX_INPUT_CHARS = 18000
MAX_OUTPUT_TOKENS = 768

ACTIONS = ("continue", "new_phase", "branch", "return")
OPERATIONS = ("new", "update", "resolve", "merge")
ATTRIBUTIONS = ("user", "assistant", "inference")

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "segments": {"type": "array", "maxItems": MAX_SEGMENTS, "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "title": {"type": "string", "maxLength": MAX_TITLE},
                "turn_indexes": {"type": "array", "items": {"type": "integer", "minimum": 0}},
                "action": {"type": "string", "enum": list(ACTIONS)},
            },
            "required": ["title", "turn_indexes", "action"],
        }},
        "changes": {"type": "array", "maxItems": MAX_CHANGES, "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "operation": {"type": "string", "enum": list(OPERATIONS)},
                "title": {"type": "string", "maxLength": MAX_TITLE},
                "type": {"type": "string", "enum": list(ITEM_TYPES)},
                "attribution": {"type": "string", "enum": list(ATTRIBUTIONS)},
                "turn_indexes": {"type": "array", "items": {"type": "integer", "minimum": 0}},
                "target_index": {"type": ["integer", "null"], "minimum": 0},
                "open": {"type": "boolean"},
                "merge_indexes": {"type": "array", "maxItems": 4, "items": {"type": "integer", "minimum": 0}},
            },
            "required": ["operation", "title", "type", "attribution", "turn_indexes", "target_index", "open", "merge_indexes"],
        }},
        "focus_segment": {"type": ["integer", "null"], "minimum": 0},
        "current": {"type": "object", "additionalProperties": False,
            "properties": {key: {"type": "array", "maxItems": limit,
                "items": {"type": "integer", "minimum": 0}}
                for key, limit in {"goal_indexes": 1, "question_indexes": 3, "next_step_indexes": 3}.items()},
            "required": ["goal_indexes", "question_indexes", "next_step_indexes"]},
    },
    "required": ["segments", "changes", "focus_segment", "current"],
}

INSTRUCTIONS = """你整理的是一个 Compact 范围内的多轮用户-AI对话，不是逐条摘抄。
先把连续讨论同一工作的 turn 合并成少量对话片段，再提取真正改变工作的事项。
每个 turn 必须且只能属于一个 segment；没有工作变化的 turn 仍放入 segment，但不要生成 change。
用户的目标、问题、约束、决定必须保留；助手只保留有持久价值的方案、结果或未解决问题。
“继续、好的、收到、可以、已处理”等没有新语义的内容不要生成 change。
同一工作事项的追问、修复、复测和助手进展合并为一个 change；已有事项优先使用 target_index 更新或解决，不要新建重复事项。
工具调用和工具返回已经从输入中删除；不要猜测工具事实。助手声称的结果只能标为 assistant，不代表已验证。
segment.action=continue 表示当前主线（包括 Compact 的第一个片段），new_phase 表示用户目标明显改变，branch 表示进入隔离支线，return 只表示焦点回到主线；支线是否解决由其事项状态决定。
change.title 是单行短标题，优先 6-15 个汉字，最多 40 字；不写解释、原文、文件清单或测试清单。
operation=new 表示没有对应的现有事项且 target_index 必须为 null；update 表示历史事项有新的重要进展；resolve 表示历史事项被当前证据明确解决；merge 表示两个或多个相同事项归并到 target_index，merge_indexes 列出要吸收的历史事项。一个 Compact 内同一事项只保留最终净状态；如果事项在本批次先创建再变化，target_index 引用本批次按 new 出现顺序排列的虚拟事项，从 active_items 最大 index+1 开始（无历史时从0开始）。
current 用索引选择本次对话结束时的当前重点，替换旧清单：goal_indexes最多1项，question_indexes最多3项，next_step_indexes最多3项。只选尚未解决且类型匹配的事项；不再相关的旧待办留在历史，不要带入current，不要把已完成事项当下一步。索引引用active_items或本批新建事项；next_step只选action/next_step，question只选question。不要把某个子功能完成误判为整个复合目标完成。
只输出 JSON，不要 Markdown、解释、引用原文或额外字段。"""


def _clip(text, limit):
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    # Keep the bounded prompt small while retaining high-signal constraints
    # that often occur in the middle of a long user turn.
    parts = [part.strip() for part in __import__("re").split(r"(?<=[。！？!?；;])\s*", text) if part.strip()]
    signal = __import__("re").compile(r"(?:必须|要求|约束|不允许|禁止|只能|不要|保持|目标|决定|不能|不得|must|only|do not|keep|require|constraint)", __import__("re").I)
    chosen = []
    for part in parts:
        if signal.search(part) and part not in chosen:
            chosen.append(part)
    for part in (parts[:1] + parts[-1:]):
        if part and part not in chosen:
            chosen.append(part)
    result = ""
    for part in chosen:
        candidate = part if not result else result + " " + part
        if len(candidate) <= limit:
            result = candidate
        else:
            remaining = limit - len(result) - (1 if result else 0)
            if remaining > 12:
                result = (result + " " if result else "") + part[:remaining - 1].rstrip() + "…"
            break
    if len(result) < min(limit, 24):
        left = max(1, limit // 2)
        result = text[:left] + "…" + text[-(limit - left):]
    return result[:limit]


def _bound_title(value):
    value = value.strip() if isinstance(value, str) else value
    if isinstance(value, str) and len(value) > MAX_TITLE:
        value = value[:MAX_TITLE - 1].rstrip() + "…"
    validate_title(value)
    return value


def request_payload(turns, state, active_items):
    safe_turns = []
    for turn in turns:
        safe_turns.append({
            "index": turn["index"],
            "user": _clip(turn.get("user"), MAX_USER_CHARS),
            "assistant": _clip(turn.get("assistant"), MAX_ASSISTANT_CHARS),
        })
    payload = {
        "policy": POLICY,
        "turns": safe_turns,
        "state": {
            "focus": _clip(state.get("focus"), 80),
            "goals": [_clip(v, 80) for v in state.get("goals", [])[:2]],
            "open": [_clip(v, 80) for v in state.get("open", [])[:4]],
        },
        "active_items": [
            {"index": row["index"], "title": _clip(row["title"], MAX_TITLE), "type": row["type"], "status": row.get("status", "open")}
            for row in active_items[:12]
        ],
    }
    return payload


def instructions():
    return INSTRUCTIONS


def validate(output, turn_indexes, active_indexes, *, require_current=False):
    output = deepcopy(output)
    if not isinstance(output, dict) or set(output) not in ({"segments", "changes", "focus_segment"}, {"segments", "changes", "focus_segment", "current"}):
        raise ValueError("compact_invalid_fields")
    if require_current and "current" not in output:
        raise ValueError("compact_current_required")
    allowed = set(turn_indexes)
    segments = output["segments"]
    if not isinstance(segments, list) or not segments or len(segments) > MAX_SEGMENTS:
        raise ValueError("compact_invalid_segments")
    seen = []
    for segment in segments:
        if not isinstance(segment, dict) or set(segment) != {"title", "turn_indexes", "action"}:
            raise ValueError("compact_invalid_segment_fields")
        segment["title"] = _bound_title(segment["title"])
        if segment["action"] not in ACTIONS:
            raise ValueError("compact_invalid_segment_action")
        indexes = segment["turn_indexes"]
        if not isinstance(indexes, list) or not indexes or len(indexes) != len(set(indexes)):
            raise ValueError("compact_invalid_segment_turns")
        if any(type(i) is not int or i not in allowed for i in indexes):
            raise ValueError("compact_unknown_turn")
        seen.extend(indexes)
    if sorted(seen) != sorted(turn_indexes):
        raise ValueError("compact_turn_coverage")
    changes = output["changes"]
    if not isinstance(changes, list) or len(changes) > MAX_CHANGES:
        raise ValueError("compact_invalid_changes")
    new_positions = []
    for position, change in enumerate(changes):
        if not isinstance(change, dict):
            raise ValueError("compact_invalid_change_fields")
        # Accept the v1 fixture shape at the boundary, then canonicalize it.
        if set(change) == {"operation", "title", "type", "attribution", "turn_indexes", "target_index", "open"}:
            change["merge_indexes"] = []
        if set(change) != {
            "operation", "title", "type", "attribution", "turn_indexes", "target_index", "open", "merge_indexes"
        }:
            raise ValueError("compact_invalid_change_fields")
        if change["operation"] not in OPERATIONS or change["type"] not in ITEM_TYPES:
            raise ValueError("compact_invalid_change")
        if change["attribution"] not in ATTRIBUTIONS:
            raise ValueError("compact_invalid_attribution")
        if type(change["open"]) is not bool:
            raise ValueError("compact_invalid_open")
        change["title"] = _bound_title(change["title"])
        indexes = change["turn_indexes"]
        if not isinstance(indexes, list) or not indexes or len(indexes) != len(set(indexes)):
            raise ValueError("compact_invalid_change_turns")
        if any(type(i) is not int or i not in allowed for i in indexes):
            raise ValueError("compact_unknown_change_turn")
        target = change["target_index"]
        # Some providers express “update the item just created” as
        # new+target. Canonicalize that equivalent form before validating the
        # target; it prevents duplicate nodes without accepting an unbound
        # historical update.
        if change["operation"] == "new" and target is not None:
            change["operation"] = "update"
        if change["operation"] != "new":
            virtual_start = max(active_indexes, default=-1) + 1
            virtual_count = len(new_positions)
            # Providers may refer to an earlier new change by its change
            # position instead of its compact-local virtual ordinal. Resolve
            # that unambiguous spelling to the canonical virtual index.
            if type(target) is int and target not in active_indexes and not virtual_start <= target < virtual_start + virtual_count:
                position_to_virtual = {position: virtual_start + ordinal for ordinal, position in enumerate(new_positions)}
                if target in position_to_virtual:
                    target = position_to_virtual[target]
                    change["target_index"] = target
            # A first Compact may contain an update or resolve before any
            # durable item exists. There is no historical identity to target,
            # so materialize the first net state; later updates still require
            # an explicit target.
            first_batch_unbound = (
                change["operation"] in {"update", "resolve"}
                and target is None
                and not active_indexes
                and not new_positions
            )
            if not first_batch_unbound and (
                type(target) is not int or (target not in active_indexes and not virtual_start <= target < virtual_start + virtual_count)
            ):
                raise ValueError("compact_target_required")
        merges = change["merge_indexes"]
        if not isinstance(merges, list) or len(merges) != len(set(merges)) or any(type(i) is not int for i in merges):
            raise ValueError("compact_invalid_merge_indexes")
        if change["operation"] == "merge":
            if not merges or type(target) is not int or target in merges:
                raise ValueError("compact_merge_target_required")
            if any(i not in active_indexes for i in merges):
                raise ValueError("compact_merge_source_required")
        elif merges:
            raise ValueError("compact_merge_indexes_without_merge")
        if change["operation"] == "resolve" and change["open"]:
            raise ValueError("compact_resolve_must_close")
        if change["operation"] == "new":
            new_positions.append(position)
    focus = output["focus_segment"]
    if focus is not None and (type(focus) is not int or not 0 <= focus < len(segments)):
        raise ValueError("compact_invalid_focus")
    if "current" in output:
        from .current_view import LIMITS
        current = output["current"]
        if not isinstance(current, dict) or set(current) != set(LIMITS):
            raise ValueError("compact_invalid_current_fields")
        virtual_start = max(active_indexes, default=-1) + 1
        # An unbound first update/resolve also materializes a first item.
        materialized = sum(c["operation"] == "new" or c["target_index"] is None for c in changes)
        offered = set(active_indexes) | set(range(virtual_start, virtual_start + materialized))
        for key, limit in LIMITS.items():
            refs = current[key]
            if (not isinstance(refs, list) or len(refs) > limit
                    or any(type(i) is not int or i not in offered for i in refs)
                    or len(refs) != len(set(refs))):
                raise ValueError("compact_invalid_current_indexes")
    return output
