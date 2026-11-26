"""Compact-level semantic contract.

The model sees a bounded batch of user/assistant turns and returns only the
conversation segments and durable changes. Source text, identities, evidence,
and narrative structure remain runtime-owned.
"""

from copy import deepcopy
import json

from .contracts import ITEM_TYPES
from .titles import validate_title
from .phase_integrity import OBJECTIVE_TYPES

POLICY = "seywo-compact-segment-6"
MAX_REPAIRS = 1
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
                "action": {"type": "string", "enum": ["continue", "new_phase", "branch", "mainline"]},
                "anchor_index": {"type": ["integer", "null"], "minimum": 0,
                    "description": "new_phase 必须指向当前 segment 中 operation=new、attribution=user 且 type 为用户目标类型的新事项索引；不得引用历史目标或其他片段的事项。"},
            },
            "required": ["title", "turn_indexes", "action", "anchor_index"],
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
        "current": {"type": "object", "additionalProperties": False,
            "properties": {
                "goal_index": {"type": ["integer", "null"], "minimum": 0},
                "priority_indexes": {"type": "array", "maxItems": 6,
                    "items": {"type": "integer", "minimum": 0}},
            },
            "required": ["goal_index", "priority_indexes"]},
    },
    "required": ["segments", "changes", "current"],
}

def schema_for(payload):
    """Constrain generation to this request's provenance and identity domains.

    Runtime still validates chronological order and materialization dependencies;
    the model need not invent unrestricted integer references and repair them.
    """
    schema = deepcopy(SCHEMA)
    turns = sorted({turn['index'] for turn in payload.get('turns', [])})
    active = sorted({item['index'] for item in payload.get('active_items', [])})
    offered = active + list(range(max(active, default=-1) + 1,
                                  max(active, default=-1) + 1 + MAX_CHANGES))
    segments = schema['properties']['segments']['items']['properties']
    changes = schema['properties']['changes']['items']['properties']
    for fields in (segments, changes):
        if turns:
            fields['turn_indexes']['items']['enum'] = turns
    segments['anchor_index']['enum'] = offered + [None]
    changes['target_index']['enum'] = offered + [None]
    if active:
        changes['merge_indexes']['items']['enum'] = active
    else:
        changes['merge_indexes']['maxItems'] = 0
    current = schema['properties']['current']['properties']
    current['goal_index']['enum'] = offered + [None]
    current['priority_indexes']['items']['enum'] = offered
    return schema


INSTRUCTIONS = """整理 Compact 内的用户-AI对话：分片段，提取工作变化，不摘抄。
part=[序号,总数] 是同轮长文本分片，按已有身份更新，不另建阶段或重复事项。segments 按轮次连续有序，覆盖全部 turn，不交错、不倒序。同轮多个真实结构动作可共享边界轮次，数组顺序即动作顺序；不额外记录初始主线。程序回放推导焦点，不输出 focus_segment。进入支线、返回主线、再次进入必须分别保留，即使无事项变化。计划切换只记待办，不作已发生的 branch/mainline。
保留用户目标、问题、约束、决定及助手持久方案、结果、未决问题。只有不改变工作的寒暄、回执不生成 change；简短回复也要结合上下文判断，确认方案属于决定，完成或重开属于状态变更。省略事项名称时依据当前焦点与目录唯一绑定已有事项，不把状态变更当寒暄；归属不明时不得把多个事项一并关闭。
同一事项的追问、修复、复测和进展只交一个最终净状态，不新建重复事项。按独立生命周期划分事项：主目标与能独立解决的阻塞、验收项分别保留身份，未决阻塞不能藏入目标标题；范围、优先次序等修饰凝练入原标题，不另建同义事项。current 的关注引用具体未决事项，不以主目标替代。首次提到但已完成的事项用 new、open=false；不要为同批进展反复生成 new/update/resolve。
工具记录已排除，不猜测工具事实；助手声称的结果标为 assistant，不代表已验证。
segment.action：continue 继续当前工作；new_phase 用户主目标改变；branch 暂时离开主目标开展独立调查（含任何重入或回到支线）；mainline 仅切回主线，不代表支线解决。普通子任务或故障进展不自动构成 branch，用户继续主线不证明此前进入过支线。结构动作独立于事项变化，没有 changes 也要保留切换。
每个 segment 给 anchor_index，引用 active_items 或本批新事项。branch 指明对应支线事项；new_phase 必须给 anchor_index，引用当前 segment 内 operation=new、attribution=user、type=goal/question/decision/correction/action 的事项（turn_indexes 交集非空），不得引用旧目标或其他片段。无新用户目标不得切阶段；只有证据明确继续旧目标才用 continue。重入历史支线即使没有 changes 也给原事项 anchor。continue/mainline 可为 null。初轮同时提出主线和支线时分别保留主线目标、支线事项，用 branch 的 anchor 区分归属，不把主线目标吞入支线。
change.title 为6-15字单行短标题，最多40字，不解释或列文件；attribution 为 user/assistant/inference。
operation=new 创建事项，target_index=null；update/resolve 引用明确的已有事项，resolve 必须 open=false；merge 将 merge_indexes 列出的历史同类事项归并到 target_index。历史更新必须给 target，不猜测无目标操作的归属。
新事项的 index 从 active_items 最大 index+1 开始（无历史从0），按 new 出现顺序递增；target_index、anchor_index、current 共用该索引，禁止用 changes 数组位置代替。兼容同批引用时只能引用此前已创建的事项，明确给定的合法 target 不得改写。
current.goal_index 选择当前主目标的事项索引或 null，主目标可为 goal/question/decision/action；priority_indexes 按优先级列最多6个事项索引。只选择身份和次序，分类、去重、已解决过滤由程序完成；不再相关的旧待办留在历史，子功能完成不代表复合目标完成。
只输出 JSON，不要 Markdown、解释、原文或额外字段。"""



class CompactValidationError(ValueError):
    """Machine-readable feedback without transcript or provider exception text."""

    def __init__(self, code, **details):
        super().__init__(code)
        self.details = details


def repair_instructions(error, output):
    feedback = {"error_code": str(error), **getattr(error, "details", {})}
    return (INSTRUCTIONS + "\n上次输出未通过校验，尚未发布或应用。请依据相同输入修正完整 JSON。"
            "保留有证据的阶段切换，不得为通过校验机械改成 continue。"
            "\nvalidation_error=" + json.dumps(feedback, ensure_ascii=False, separators=(',', ':'))
            + "\nrejected_output=" + json.dumps(output, ensure_ascii=False, separators=(',', ':')))


def validate_phase_anchors(output, active_indexes):
    """A phase must cite a new user objective from its own segment."""
    start = max(active_indexes, default=-1) + 1
    new = [change for change in output["changes"] if change["operation"] == "new"]
    for index, segment in enumerate(output["segments"]):
        if segment["action"] != "new_phase":
            continue
        anchor = segment.get("anchor_index")
        reason = None
        if anchor is None:
            reason = "anchor_required"
        elif type(anchor) is not int or not start <= anchor < start + len(new):
            reason = "anchor_not_new"
        else:
            change = new[anchor - start]
            if change["attribution"] != "user" or change["type"] not in OBJECTIVE_TYPES:
                reason = "anchor_not_user_objective"
            elif not set(change["turn_indexes"]).intersection(segment["turn_indexes"]):
                reason = "anchor_not_in_segment"
        if reason:
            raise CompactValidationError("compact_new_phase_requires_user_objective",
                                         segment_index=index, anchor_index=anchor, reason=reason,
                                         new_index_start=start,
                                         objective_types=sorted(OBJECTIVE_TYPES))


def _clip(text, limit):
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + "…"


def _bound_title(value):
    value = value.strip() if isinstance(value, str) else value
    if isinstance(value, str) and len(value) > MAX_TITLE:
        value = value[:MAX_TITLE - 1].rstrip() + "…"
    validate_title(value)
    return value


def request_payload(turns, state, active_items):
    safe_turns = []
    for turn in turns:
        user, assistant = turn.get("user") or "", turn.get("assistant") or ""
        if len(user) > MAX_USER_CHARS or len(assistant) > MAX_ASSISTANT_CHARS:
            raise ValueError("compact_input_requires_scheduling")
        row = {"index": turn["index"], "user": user, "assistant": assistant}
        if "part" in turn:
            row["part"] = list(turn["part"])
        safe_turns.append(row)
    if (len(safe_turns) > MAX_TURNS
            or sum(len(t["user"]) + len(t["assistant"]) for t in safe_turns) > MAX_INPUT_CHARS):
        raise ValueError("compact_input_requires_scheduling")
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


def validate(output, turn_indexes, active_indexes):
    output = deepcopy(output)
    if (not isinstance(output, dict) or not {"segments", "changes"} <= set(output)
            or set(output) - {"segments", "changes", "current"}):
        raise ValueError("compact_invalid_fields")
    if "current" not in output:
        raise ValueError("compact_current_required")
    allowed = set(turn_indexes)
    segments = output["segments"]
    if not isinstance(segments, list) or not segments or len(segments) > MAX_SEGMENTS:
        raise ValueError("compact_invalid_segments")
    seen = []
    for segment_index, segment in enumerate(segments):
        if isinstance(segment, dict) and segment.get('action') == 'new_phase' and 'anchor_index' not in segment:
            raise CompactValidationError('compact_new_phase_requires_user_objective',
                                         segment_index=segment_index, reason='anchor_required')
        if not isinstance(segment, dict) or set(segment) != {"title", "turn_indexes", "action", "anchor_index"}:
            raise ValueError("compact_invalid_segment_fields")
        segment["title"] = _bound_title(segment["title"])
        # Map the wire action to the narrative transition kind.
        if segment["action"] == "mainline":
            segment["action"] = "return"
        elif segment["action"] not in ("continue", "new_phase", "branch"):
            raise ValueError("compact_invalid_segment_action")
        indexes = segment["turn_indexes"]
        if not isinstance(indexes, list) or not indexes or len(indexes) != len(set(indexes)):
            raise ValueError("compact_invalid_segment_turns")
        if any(type(i) is not int or i not in allowed for i in indexes):
            raise ValueError("compact_unknown_turn")
        seen.extend(indexes)
    # A source turn is an evidence unit, not a structural time slot. Several
    # real actions can occur within one turn; adjacent segments may share
    # that boundary, while reverse/interleaved evidence still fails closed.
    if set(seen) != allowed:
        raise ValueError("compact_turn_coverage")
    if seen != sorted(seen):
        raise ValueError("compact_turn_order")
    changes = output["changes"]
    if not isinstance(changes, list) or len(changes) > MAX_CHANGES:
        raise ValueError("compact_invalid_changes")
    virtual_start = max(active_indexes, default=-1) + 1
    materialized = 0
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("compact_invalid_change_fields")
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
        if change["operation"] == "resolve" and change["open"]:
            raise ValueError("compact_resolve_must_close")
        if (change["operation"] in {"update", "resolve"} and target is None
                and not active_indexes and materialized == 0):
            # An empty history can accept one first net state, regardless of
            # the provider's insertion spelling. Normalize before allocating
            # identities so target, anchor, and current share the same count.
            change["operation"] = "new"
        if change["operation"] != "new":
            if type(target) is not int or (
                target not in active_indexes
                and not virtual_start <= target < virtual_start + materialized
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
        if change["operation"] == "new":
            materialized += 1
    offered = set(active_indexes) | set(range(virtual_start, virtual_start + materialized))
    for segment in segments:
        anchor = segment["anchor_index"]
        if anchor is not None and (type(anchor) is not int or anchor not in offered):
            raise ValueError("compact_invalid_anchor_index")
    validate_phase_anchors(output, active_indexes)
    current = output['current']
    if not isinstance(current, dict) or set(current) != {'goal_index', 'priority_indexes'}:
        raise ValueError('compact_invalid_current_fields')
    goal, refs = current['goal_index'], current['priority_indexes']
    if (goal is not None and (type(goal) is not int or goal not in offered)
            or not isinstance(refs, list) or len(refs) > 6
            or any(type(i) is not int or i not in offered for i in refs)):
        raise ValueError('compact_invalid_current_indexes')
    return output
