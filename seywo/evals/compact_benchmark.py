"""Reproducible Compact benchmark using project-saved provider settings.

Synthetic fixtures only. Never imports the user's chat history or modifies their
project database. The recorded tokens are provider usage including prompt/schema.
"""
import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seywo import inbox, maintain  # noqa: E402
from seywo.config import initialize, load_config  # noqa: E402
from seywo.settings import read_provider, save_provider  # noqa: E402
from seywo.store import Store  # noqa: E402
from seywo.transport import Transport  # noqa: E402
from seywo.codex_transport import CodexTransport  # noqa: E402

TOOL_MARKER = "BENCH_TOOL_BODY_EXCLUDED_9b47e"
CASES = [
    {"id": "repair-six-turns", "checks": {"max_items": 4, "phases": 1, "mainline": True}, "windows": [[
        ("修复 CSV 导出时中文乱码的问题。", "我会检查 CSV 编码并修复导出。"),
        ("现有下载接口保持兼容。", "导出将加 UTF-8 BOM，下载接口保持兼容。"),
        ("继续定位这个乱码问题。", "已定位 Excel 默认编码识别问题。"),
        ("用刚才的方案修复。", "已加入 UTF-8 BOM，并保留原下载参数。"),
        ("再核验导出结果。", "中文 CSV 已验证，下载行为一致。"),
        ("这项完成，记录最终结果。", "CSV 乱码已修复，原下载接口继续兼容。"),
    ]]},
    {"id": "goal-change", "checks": {"phases": 2, "mainline": True}, "windows": [[
        ("当前目标是修复 CSV 导出乱码。", "按 UTF-8 BOM 方案处理。"),
        ("CSV 乱码已确认修复，这个目标结束。", "导出修复已结束。"),
        ("接下来目标改为设计团队权限管理，这是新的独立工作。", "新目标是按成员角色管理团队权限。"),
        ("权限管理采用管理员和只读成员两种角色。", "权限设计确定为管理员与只读成员。"),
    ]]},
    {"id": "branch-return", "checks": {"phases": 1, "branches_min": 1, "mainline": True}, "windows": [[
        ("主线目标是交付 CSV 导出，先保留这条主线。", "主线是完成 CSV 导出交付。"),
        ("暂时进入支线排查登录失败，排查后回到 CSV 导出。", "进入登录排查支线。"),
        ("登录失败原因已经找到，是凭据过期，支线到此结束。", "更换凭据后登录恢复。"),
        ("回到 CSV 导出主线，继续完成交付。", "回到 CSV 主线，整理导出交付。"),
    ]]},
    {"id": "resolve-across-compacts", "checks": {"phases": 1, "mainline": True, "unresolved_empty": True}, "windows": [
        [("当前唯一未解决事项是 CSV 导出内存超限。", "导出内存超限仍未解决，接下来验证流式写出。"),
         ("围绕内存超限继续验证，不要新增其他任务。", "流式写出已经实现，内存峰值尚待验证。")],
        [("流式导出的内存峰值已经验证低于限制，这个问题解决了。", "内存超限问题解决，流式导出验证通过。"),
         ("关闭刚才的内存超限事项。", "已关闭同一内存事项，无遗留问题。")],
    ]},
    {"id": "acknowledgements", "checks": {"max_items": 0}, "windows": [[
        ("好的", "收到"), ("继续", "好的"), ("谢谢", "收到"),
    ]]},
    {"id": "constraints-and-tools", "checks": {"phases": 1, "mainline": True}, "tools": True, "windows": [[
        ("整理当前对话，必须保留原始消息的来源定位。", "我会保留定位索引。"),
        ("工具返回不进入模型；只使用用户问题和助手最终回答。", "在规则层移除工具正文。"),
        ("不允许修改已有正常功能，还要求低 token 消耗和短标题。", "只返回短标题与索引，已有检索和来源回读保留。"),
    ]]},
    {"id": "long-25-turns", "checks": {"phases": 1, "mainline": True, "max_items": 4}, "windows": [[
        (f"继续同一个 CSV 乱码修复，第 {i + 1} 次核验，不改变目标。",
         f"第 {i + 1} 次核验：UTF-8 BOM 的导出结果正常，继续沿用同一方案。") for i in range(25)
    ]]},
]


# These cases check the semantic state and movement, not merely branch count.
REPEATED = [
    ("这轮主线是账单审阅。先进入重试调查这条支线。", "重试原因还没查明，调查被阻塞。"),
    ("现在回到账单主线继续核对，重试调查挂起，问题没有解决。", "回到账单审阅，重试仍未解决。"),
    ("现在再进重试支线复核。原因仍未知，不能关闭问题。", "重新进入同一调查，原因待查。"),
    ("现在回到账单主线继续审阅，重试支线仍未解决，继续挂起。", "回到账单主线，调查保持挂起。"),
]
CASES += [
    {"id": "repeated-return", "checks": {"phases": 1, "max_items": 3, "movement": ["enter", "return", "enter", "return"], "branch_state": "waiting", "mainline": True, "one_branch": True, "billing_placement": True}, "windows": [REPEATED]},
    {"id": "repeated-stop-in-branch", "checks": {"phases": 1, "max_items": 3, "movement": ["enter", "return", "enter"], "branch_state": "active", "branch_focus": True, "one_branch": True, "billing_placement": True}, "windows": [REPEATED[:3]]},
    {"id": "future-return-is-not-return", "checks": {"phases": 1, "max_items": 4, "movement": ["enter"], "branch_state": "active", "branch_focus": True, "one_branch": True, "billing_placement": True}, "windows": [[REPEATED[0], ("仍在重试支线排查。明天查完后才回到账单主线，现在不要切回去。", "继续调查，回主线只是后续计划。")]]},
    {"id": "resolve-only-webhook", "checks": {"phases": 1, "one_branch": True, "mainline": True, "billing_placement": True, "isolated_resolution": True}, "windows": [[
        ("主线是账单审阅。先进入 Webhook 调查支线。", "Webhook 原因未知，调查待完成。"),
        ("Webhook 已核对并验证通过，关闭该调查。回到账单主线，税额尚未核对，账单审阅仍未完成。", "Webhook 调查已完成；税额待核对，继续账单审阅。"),
    ]]},
    {"id": "reenter-across-compacts", "checks": {"phases": 1, "max_items": 3, "movement": ["enter", "return", "enter"], "branch_state": "active", "branch_focus": True, "one_branch": True, "billing_placement": True, "stable_branch": True}, "windows": [REPEATED[:2], [REPEATED[2]]]},
    {"id": "english-repeated-return", "checks": {"phases": 1, "max_items": 3, "movement": ["enter", "return", "enter", "return"], "branch_state": "waiting", "mainline": True, "one_branch": True}, "windows": [[
        ("Main objective: review invoices. First enter a side investigation into failed retries.", "Retry cause is still unknown; investigation is blocked."),
        ("Return to invoice review now. Leave the retry investigation unresolved and parked.", "Back on invoice review; retries remain unresolved."),
        ("Re-enter the same retry investigation. The cause is still unknown, do not close it.", "Investigating retries again; unresolved."),
        ("Return to invoice review again. Keep retries unresolved and parked.", "Reviewing invoices; retry investigation remains parked."),
    ]]},
]


LONG_MIXED = []
for n in range(50):
    if n == 0:
        pair = ("主线是账单审阅。税额、金额与条目都尚未全部核对完。", "开始账单审阅，主目标保持未完成。")
    elif n == 5:
        pair = ("现在进入 Webhook 重试调查支线。原因未知。", "进入重试调查，仍待查。")
    elif n == 12 or n == 40:
        pair = ("现在回到账单审阅主线，Webhook 重试问题仍未解决，挂起调查。", "回到账单审阅，Webhook 调查挂起。")
    elif n == 30:
        pair = ("再次进入同一个 Webhook 重试调查支线。原因仍未知，不能关闭。", "重入原调查，问题未解决。")
    elif 5 < n < 12 or 30 < n < 40:
        pair = (f"第{n}轮，继续当前 Webhook 重试调查，没有新结论。", "仍在同一个调查支线，保持未解决。")
    else:
        pair = (f"第{n}轮，继续账单审阅，不改变主目标，尚未完成。", "继续核对账单，仍未全部核对完成。")
    LONG_MIXED.append(pair)
CASES.append({"id": "long-50-mixed", "checks": {"phases": 1, "max_items": 4,
    "movement": ["enter", "return", "enter", "return"], "branch_state": "waiting",
    "mainline": True, "one_branch": True, "billing_placement": True}, "windows": [LONG_MIXED]})


# Independent review: ordinary child faults must not invent side-line navigation.
CASES.append({'id': 'peer-similar-upload-faults',
 'checks': {'phases': 1,
            'mainline': True,
            'branches_exact': 0,
            'movement': [],
            'similar_faults': True,
            'max_items': 4},
 'windows': [[['主线目标是完成媒体上传稳定性验收，目前尚未完成。有两个独立故障必须分别跟踪：原图上传超时、缩略图上传超时，两个都未解决。',
               '已分别记录原图上传超时与缩略图上传超时，继续媒体上传稳定性验收。'],
              ['原图上传超时已定位为代理超时，调高限制后复测通过，只关闭原图上传超时这一项。缩略图上传超时原因未知，仍未解决。', '原图上传超时已解决；缩略图上传超时继续待查，不合并这两个故障。'],
              ['继续主线验收：缩略图上传超时仍是阻塞项，尚不能宣布稳定性验收完成。', '继续媒体上传稳定性验收；保留缩略图上传超时为未解决事项，原图故障保持已关闭。']]]})

def encode(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def message(text, role):
    return {"type": "response_item", "payload": {"type": "message", "role": role,
            "channel": "final" if role == "assistant" else None,
            "content": [{"type": "input_text", "text": text}]}}


def run_case(case, pipeline, provider, repetition):
    traces = []

    class Metered(Transport):
        def _complete_json(self, **kwargs):
            tick = time.monotonic()
            result = super()._complete_json(**kwargs)
            traces.append({"request": {k: v for k, v in kwargs.items()}, "result": result,
                           "elapsed_seconds": round(time.monotonic() - tick, 4)})
            return result

    class MeteredCodex(CodexTransport):
        def _execute(self, instructions, payload, schema, *, model=None):
            tick = time.monotonic()
            result = super()._execute(instructions, payload, schema, model=model)
            traces.append({"request": {"model": model, "instructions": instructions, "payload": payload, "schema": schema},
                           "result": result, "elapsed_seconds": round(time.monotonic() - tick, 4)})
            return result

    root = Path(tempfile.mkdtemp(prefix="seywo-paired-metrics-")) / "project"
    initialize(root)
    config_path = root / ".seywo/config.json"
    config_data = json.loads(config_path.read_text())
    config_data["hook"] = {"enabled": True, "pipeline": pipeline, "model": provider["model"]}
    config_path.write_text(encode(config_data))
    config = load_config(config_path)
    if provider.get("backend") != "codex":
        save_provider(config, endpoint=provider["endpoint"], model=provider["model"],
                      api_key=provider.get("_api_key", ""), timeout_seconds=provider["timeout"],
                      max_output_tokens=provider["max_output_tokens"])
        config = load_config(config_path)
    elif provider["model"] != "codex-default":
        config_data["model_runtime"] = {"backend": "codex", "model": provider["model"]}
        config_path.write_text(encode(config_data))
        config = load_config(config_path)
    chat = "benchmark-chat"
    source = root / "source.jsonl"
    records = [{"type": "session_meta", "payload": {"id": chat, "cwd": str(root)}}]
    windows = []
    start = time.monotonic()
    previous_transport, previous_codex = maintain.Transport, maintain.CodexTransport
    maintain.Transport, maintain.CodexTransport = Metered, MeteredCodex
    documents = []
    try:
        for wi, turns in enumerate(case["windows"]):
            for ti, (user, assistant) in enumerate(turns):
                identity = f"turn-{wi}-{ti}"
                records.append({"type": "turn_context", "payload": {"turn_id": identity, "root_turn_id": identity}})
                records += [message(user, "user"), message(assistant, "assistant")]
                if case.get("tools"):
                    records.append({"type": "response_item", "payload": {"type": "function_call",
                        "name": "benchmark_tool", "call_id": f"call-{wi}-{ti}",
                        "arguments": encode({"body": TOOL_MARKER + "工具参数" * 1000})}})
                    records.append({"type": "response_item", "payload": {"type": "function_call_output",
                        "call_id": f"call-{wi}-{ti}", "output": TOOL_MARKER + "工具返回" * 1000}})
            source.write_text("".join(encode(r) + "\n" for r in records), encoding="utf-8")
            inbox.record(config, chat, "PreCompact", str(source))
            report = maintain.run_chat(config, chat)
            windows.append(report)
            with Store(config, read_only=True) as snapshot:
                latest = snapshot.db.execute("SELECT body_json FROM narrative_versions WHERE chat_id=? ORDER BY version DESC LIMIT 1", (chat,)).fetchone()
                documents.append(json.loads(latest[0]) if latest else None)
            if report.get("state") != "done":
                break
        with Store(config, read_only=True) as store:
            row = store.db.execute("SELECT body_json FROM narrative_versions WHERE chat_id=? ORDER BY version DESC LIMIT 1", (chat,)).fetchone()
            body = json.loads(row[0]) if row else {"items": [], "phases": [], "branches": [], "overview": {}}
            evidence = [ref for item in body["items"] for ref in item.get("evidence", [])]
            valid_refs = sum(bool(store.db.execute("SELECT 1 FROM source_fragments WHERE event_id=? AND snapshot_id=?", (ref["event_id"], ref["snapshot_id"])).fetchone()) for ref in evidence)
            stored_tools = store.db.execute("SELECT count(*) FROM current_sources WHERE kind IN ('function_call','function_call_output')").fetchone()[0]
    except Exception as exc:
        body = {"items": [], "phases": [], "branches": [], "overview": {}}
        evidence, valid_refs, stored_tools = [], 0, 0
        windows.append({"state": "exception", "error_code": f"{type(exc).__name__}:{str(exc)[:160]}"})
    finally:
        maintain.Transport, maintain.CodexTransport = previous_transport, previous_codex

    usage_known = bool(traces) and all(t["result"].get("usage") is not None for t in traces)
    tool_clean = all(TOOL_MARKER not in encode(t["request"]) for t in traces)
    result = {"case": case["id"], "pipeline": pipeline, "repetition": repetition,
              "turns": sum(map(len, case["windows"])), "compact_count": len(case["windows"]),
              "calls": len(traces), "input_tokens": sum((t["result"].get("usage") or {}).get("input_tokens", 0) for t in traces) if usage_known else None,
              "output_tokens": sum((t["result"].get("usage") or {}).get("output_tokens", 0) for t in traces) if usage_known else None,
              "elapsed_seconds": round(time.monotonic() - start, 4),
              "items": len(body["items"]), "phases": len(body["phases"]), "branches": len(body["branches"]),
              "titles": [i.get("title", "") for i in body["items"]], "overview": body.get("overview"),
              "evidence_refs": len(evidence), "valid_evidence_refs": valid_refs,
              "tool_body_excluded": tool_clean, "stored_tool_records": stored_tools,
              "state": windows[-1].get("state"), "error_code": windows[-1].get("error_code"),
              "windows": windows, "document": body, "documents": documents, "traces": traces}
    checks = {"pipeline_completed": result["state"] == "done", "tool_filter": tool_clean,
              "evidence_identity": valid_refs == len(evidence),
              "short_titles": all(0 < len(title) <= 40 for title in result["titles"])}
    expected = case["checks"]
    if "max_items" in expected:
        checks["condensation"] = result["items"] <= expected["max_items"]
    if "phases" in expected:
        checks["phase_count"] = result["phases"] == expected["phases"]
    if "branches_min" in expected:
        checks["branch_preserved"] = result["branches"] >= expected["branches_min"]
    if expected.get("mainline"):
        checks["focus_mainline"] = (body.get("overview", {}).get("focus") or {}).get("branch_id") is None
    if expected.get("unresolved_empty"):
        checks["resolved_no_stale_unresolved"] = not body.get("overview", {}).get("unresolved", [])
    if "branches_exact" in expected:
        checks["exact_branch_count"] = len(body["branches"]) == expected["branches_exact"]
    if expected.get("similar_faults"):
        originals = [i for i in body["items"] if "原图" in i["title"] and not i.get("hidden")]
        thumbnails = [i for i in body["items"] if "缩略图" in i["title"] and not i.get("hidden")]
        goals = [i for i in body["items"] if i["type"] == "goal" and not i.get("hidden")]
        checks["distinct_fault_statuses"] = len(originals) == 1 and originals[0]["status"] == "resolved" and len(thumbnails) == 1 and thumbnails[0]["status"] == "open" and originals[0]["id"] != thumbnails[0]["id"]
        checks["acceptance_goal_still_open"] = len(goals) == 1 and goals[0]["status"] == "open"
        current_ids = body.get("overview", {}).get("open_question_ids", []) + body.get("overview", {}).get("next_step_ids", [])
        checks["only_open_fault_is_current"] = bool(thumbnails) and thumbnails[0]["id"] in current_ids and all(i["id"] not in current_ids for i in originals)
    if "movement" in expected:
        checks["exact_movement"] = [t["kind"] for t in body.get("transitions", [])] == expected["movement"]
    if expected.get("one_branch"):
        checks["one_stable_branch"] = len(body["branches"]) == 1
    if "branch_state" in expected:
        checks["branch_state"] = len(body["branches"]) == 1 and body["branches"][0]["state"] == expected["branch_state"]
    if expected.get("branch_focus"):
        checks["focus_branch"] = len(body["branches"]) == 1 and (body.get("overview", {}).get("focus") or {}).get("branch_id") == body["branches"][0]["id"]
    if expected.get("billing_placement"):
        goals = [i for i in body["items"] if i["type"] == "goal" and "账单" in i["title"] and not i.get("hidden")]
        checks["main_goal_preserved"] = len(goals) == 1 and goals[0].get("status") == "open"
        places = {p["item_id"]: p for p in body.get("placements", [])}
        checks["main_goal_placement"] = bool(goals) and places.get(goals[0]["id"], {}).get("branch_id", "missing") is None
    if expected.get("stable_branch"):
        checks["cross_compact_branch_identity"] = len(documents) == 2 and all(d and len(d["branches"]) == 1 for d in documents) and documents[0]["branches"][0]["id"] == documents[1]["branches"][0]["id"]
    if expected.get("isolated_resolution"):
        webhooks = [i for i in body["items"] if "webhook" in i["title"].lower() and not i.get("hidden")]
        taxes = [i for i in body["items"] if "税" in i["title"] and not i.get("hidden")]
        checks["only_webhook_closed"] = bool(webhooks) and all(i.get("status") == "resolved" for i in webhooks) and bool(taxes) and all(i.get("status") == "open" for i in taxes)
        checks["resolved_branch"] = len(body["branches"]) == 1 and body["branches"][0]["state"] == "resolved"
        checks["tax_current_webhook_not_current"] = any("税" in s for s in body.get("overview", {}).get("unresolved", [])) and not any("webhook" in s.lower() for s in body.get("overview", {}).get("unresolved", []))
    checks["call_accounting"] = len(traces) == sum(window.get("calls", 0) for window in windows)
    checks["usage_measured"] = usage_known or not any(window.get("calls", 0) for window in windows)
    result["checks"] = checks
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--config", type=Path, default=Path.cwd() / ".seywo/config.json")
    parser.add_argument("--pipeline", choices=("seywo-compact-v1", "seywo-delta-v1"), action="append")
    parser.add_argument("--case", action="append", dest="case_ids")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    provider = read_provider(load_config(args.config))
    pipelines = args.pipeline or ["seywo-compact-v1"]
    cases = [case for case in CASES if not args.case_ids or case["id"] in args.case_ids]
    rows = []
    for repetition in range(args.repetitions):
        for case in cases:
            for pipeline in pipelines:
                row = run_case(case, pipeline, provider, repetition)
                name = f"{case['id']}-{pipeline}-{repetition}.json"
                (args.output / name).write_text(json.dumps(row, ensure_ascii=False, indent=2))
                compact = {k: row[k] for k in ("case", "pipeline", "repetition", "state", "calls", "input_tokens", "output_tokens", "items", "phases", "branches", "checks", "error_code")}
                rows.append(compact)
                print(encode(compact), flush=True)
                (args.output / "progress.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    summaries = {}
    for pipeline in pipelines:
        group = [r for r in rows if r["pipeline"] == pipeline]
        summaries[pipeline] = {"runs": len(group), "completed": sum(r["state"] == "done" for r in group),
            "all_fixture_checks_passed": sum(all(r["checks"].values()) for r in group),
            **{key: sum(r[key] or 0 for r in group) for key in ("calls", "input_tokens", "output_tokens", "items")}}
    report = {"date": date.today().isoformat(), "model": provider["model"], "backend": provider.get("backend", "responses"), "endpoint": provider.get("endpoint", ""),
              "fixtures_sha256": hashlib.sha256(encode(cases).encode()).hexdigest(),
              "fixtures": cases, "repetitions": args.repetitions, "summaries": summaries, "runs": rows,
              "limits": ["Synthetic fixtures, not natural-history semantic acceptance", "Provider token usage; no hardware energy measurements", "Title/evidence and explicit structural checks are not a semantic recall oracle"]}
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(encode(summaries), flush=True)


if __name__ == "__main__":
    main()
