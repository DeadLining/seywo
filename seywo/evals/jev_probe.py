"""Explicit live bounded-judge probe; controlled extraction, real Jev decisions.

Uses only the selected application's saved Jev credential, never an environment
key. Synthetic sources live in a temporary independent Seywo database. No Hook.
"""
import argparse
import hashlib
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time

from seywo.backends import JevBackend
from seywo.config import initialize, load_config
from seywo.provider import Capacity
from seywo.model_contract import POLICY, jev_envelope
from seywo.store import Store
from seywo.understanding import Options, Understanding

CASES = [
    ("conditional", "approval", "现在只生成迁移预览。只有测试通过，而且我核对预览并明确确认后，才允许执行生产迁移。", "生产迁移是否已经批准？confirms=无条件明确批准当前执行；conditional=仍须满足条件或再次确认；proposes=只是提议；unclear=无法判断。", "conditional"),
    ("contradiction", "support", "助手转述：进程退出码0；订单表校验失败，验收未通过。", "这份助手转述对‘全量迁移验收已经通过’的关系是什么？supports=支持；contradicts=反驳；insufficient=证据不足。", "contradicts"),
    ("different_runs", "identity", "检查一：本地开发库 alpha-7 单元测试12项通过。检查二：预发布库 beta-9 集成测试鉴权失败。", "两次检查是否同一工作执行？same=同一目标版本和执行；different=明确不同目标版本或执行；insufficient=无法判定。", "different"),
    ("abstention", "approval", "助手：生产迁移方案已经写好；目前没有任何用户授权消息。", "用户是否明确批准现在执行生产迁移？confirms=用户无条件明确授权；conditional=用户提出条件；proposes=用户提出建议；unclear=无用户批准证据。", "unclear"),
]


def fixture_reply(request):
    """Deterministic source-backed fixture isolates judgment from extraction quality."""
    return {"items": [{"type": "observation", "title": "核对判别依据", "source_indexes": [0]}]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings-state", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--resolved-model", help="Explicitly pin an observed concrete provider version for an alias")
    args = parser.parse_args()
    from sessiongraph.jev import JevClient, load_saved_key
    from sessiongraph.provider_settings import read_template
    provider = read_template(args.settings_state)["provider"]
    client = JevClient({"model": args.resolved_model or provider.get("jev_model", "jev-latest"), "timeout_seconds": 15})
    client.key = load_saved_key(args.settings_state)
    if not client.key:
        raise ValueError("saved_jev_key_missing")
    observed = []
    def evaluate(state, questions):
        response = client.evaluate(state, questions)
        observed.append(response)
        return response
    adapter = JevBackend(client.model, evaluate)
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "model": client.model,
              "settings_model": provider.get("jev_model", "jev-latest"), "explicit_model_pin": args.resolved_model, "credential_source": "explicit application settings saved Jev credential", "environment_credential_used": False,
              "hook_integrated": False, "model_policy": POLICY,
              "runtime_source_sha256": {str(p.relative_to(Path(__file__).resolve().parents[1])): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((Path(__file__).resolve().parents[1] / "src/seywo").glob("*.py"))},
              "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "extraction": "controlled fixture; only judgment calls are real", "cases": []}
    for identity, kind, text, question, expected in CASES:
        with tempfile.TemporaryDirectory(prefix="seywo-jev-eval-") as tmp:
            root = Path(tmp)
            initialize(root)
            path = root / "synthetic.jsonl"
            role = {"conditional": "user", "contradiction": "assistant", "different_runs": "assistant", "abstention": "assistant"}[identity]
            payload = {"type": "function_call_output", "call_id": identity, "output": text} if role == "tool" else {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}
            path.write_text(json.dumps({"type": "session_meta", "payload": {"id": identity, "cwd": str(root)}}) + "\n" + json.dumps({"type": "response_item", "payload": payload}, ensure_ascii=False) + "\n")
            with Store(load_config(root / ".seywo/config.json")) as store:
                store.import_source(path, identity, 0)
                engine = Understanding(store)
                options = Options("controlled-source-fixture", "fixture", Capacity(64000, 32000, 8000), context_chars=0)
                job = engine.prepare(identity, options)["job_id"]
                claim = engine.claim(job, backend=options.backend)
                status = engine.submit(claim["attempt_id"], backend=options.backend, output=fixture_reply(claim["request"]))
                if status["state"] != "ready":
                    raise ValueError("fixture_not_ready")
                engine.queue_judgment(job, kind=kind, question=question,
                    item_ids=[status["result"]["candidate_items"][0]["id"]], backend=adapter.identity, model=adapter.model)
                claim = engine.claim(job, backend=adapter.identity)
                result = {"id": identity, "expected": expected, "request": claim["request"], "wire_envelope": jev_envelope(claim["request"])}
                start = time.monotonic()
                try:
                    response = adapter.complete(claim["request"])
                    result["response"] = response
                    status = engine.submit(claim["attempt_id"], backend=adapter.identity, **response)
                    result["runtime_error"] = status["error_code"]
                    result["expected_choice_observed"] = response["output"]["choice"] == expected
                except Exception as exc:
                    result["error"] = {"type": type(exc).__name__, "code": getattr(exc, "code", str(exc)[:150])}
                    status = engine.submit(claim["attempt_id"], backend=adapter.identity, output=None, finish="error")
                result["provider_responses"] = observed[:]
                observed.clear()
                result.update(seconds=round(time.monotonic() - start, 3), usage=status["usage"],
                    user_confirmed=status["user_confirmed"], task_completed=status["task_completed"],
                    integrity_check=store.db.execute("PRAGMA integrity_check").fetchone()[0])
                report["cases"].append(result)
                report["model_calls"] = client.calls
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                print(json.dumps({k: result[k] for k in ("id", "seconds", "error", "expected_choice_observed") if k in result}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
