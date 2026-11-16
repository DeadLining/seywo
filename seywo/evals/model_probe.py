"""Manual synthetic A/B evaluation with explicit app settings; never a Hook.

Run from the repository with PYTHONPATH=runtime:seywo/src. The legacy import is
limited to the existing settings credential reader, outside the Seywo package.
No legacy graph, chat, index or database is opened. The reports contain synthetic
requests and replies only. Semantic checks are reviewed by a person, not keyword
scored or inferred from the runtime's structural acceptance.
"""

import argparse
from contextlib import nullcontext
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from seywo.config import initialize, load_config
from seywo.contracts import validate_understanding
from seywo.model_contract import POLICY, hydrate, responses_body
from seywo.narrative import Narrative
from seywo.narrative_contract import validate_output
from seywo.provider import Capacity
from seywo.store import Store
from seywo.understanding import Limits, Options, Understanding


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def wire_body(request):
    return responses_body(request)


class Transport:
    def __init__(self, endpoint, key, *, timeout=100):
        self.endpoint, self.key, self.timeout = endpoint, key, timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.calls = 0

    def complete(self, request):
        body = wire_body(request)
        started = time.monotonic()
        self.calls += 1
        try:
            with self.opener.open(Request(self.endpoint, data=json.dumps(body).encode(), headers={
                "Content-Type": "application/json", "Authorization": "Bearer " + self.key,
            }), timeout=self.timeout) as response:
                data = json.load(response)
        except HTTPError as exc:
            return {"output": None, "finish": "error", "error": f"http_{exc.code}", "seconds": round(time.monotonic() - started, 3)}
        except (OSError, ValueError):
            return {"output": None, "finish": "error", "error": "transport_error", "seconds": round(time.monotonic() - started, 3)}
        text = "".join(c.get("text", "") for o in data.get("output", []) if o.get("type") == "message" for c in o.get("content", []) if c.get("type") == "output_text")
        usage = data.get("usage", {})
        result = {
            "returned_model": data.get("model"), "provider_status": data.get("status"),
            "seconds": round(time.monotonic() - started, 3), "text": text,
            "wire_body_bytes": len(json.dumps(body).encode()),
            "provider_usage": usage, "incomplete_details": data.get("incomplete_details"),
            "output_item_types": [item.get("type") for item in data.get("output", [])],
        }
        if all(type(usage.get(k)) is int for k in ("input_tokens", "output_tokens")):
            result["usage"] = {k: usage[k] for k in ("input_tokens", "output_tokens")}
        try:
            result["output"] = json.loads(text)
            result["finish"] = "complete" if data.get("status") == "completed" else "length"
        except (ValueError, TypeError):
            result.update(output=None, finish="length" if data.get("status") == "incomplete" else "error", error="invalid_json")
        if data.get("model") != request["model"]:
            result.update(finish="error", error="returned_model_mismatch")
        return result


def record(message, root):
    text = message["text"]
    if "middle" in message:
        text += "校验日志：正在核对常规记录。\n" * message["prefix_repeat"] + message["middle"] + "校验日志：继续记录扫描进度。\n" * message["suffix_repeat"]
    if message["role"] == "tool":
        return {"type": "response_item", "payload": {"type": "function_call_output", "call_id": f"synthetic-{root}", "output": text}}
    return {"type": "response_item", "payload": {"type": "message", "role": message["role"], "content": [{"type": "input_text", "text": text}]}}


def evaluate_case(case, transport, *, output_reserve=4000, chunk_chars=1200, project_directory=None):
    result = {"id": case["id"], "semantic_checks": case["checks"], "semantic_review": "pending", "attempts": []}
    with (nullcontext(str(project_directory)) if project_directory else tempfile.TemporaryDirectory(prefix="seywo-model-eval-")) as tmp:
        root = Path(tmp)
        initialize(root)
        config = load_config(root / ".seywo/config.json")
        source = root / "synthetic.jsonl"
        records = [{"type": "session_meta", "payload": {"id": case["id"], "cwd": str(root)}}] + [record(m, i) for i, m in enumerate(case["messages"])]
        source.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
        with Store(config) as store:
            store.import_source(source, case["id"], 0)
            engine, narrative = Understanding(store), Narrative(store)
            # Declared test allocations, not an assertion of provider capacity.
            options = Options("explicit-live-eval", transport.model, Capacity(64000, 32000, 8000), context_chars=200, chunk_chars=chunk_chars, output_reserve=output_reserve, lease_seconds=180)
            limits = Limits(total_input_tokens=400000, total_output_tokens=100000, call_count=30, wall_seconds=3600, retry_limit=2)
            result["options"] = asdict(options)
            result["job_id"] = None
            if project_directory:
                result["synthetic_project_directory"] = str(root)
            job = engine.prepare(case["id"], options, limits)["job_id"]
            result["job_id"] = job
            organizing = None
            for _ in range(30):
                claim = engine.claim(job, backend=options.backend)
                if claim["state"] != "request":
                    status = engine.status(job)
                    if claim["state"] in {"ready", "partial"} and organizing is None:
                        organizing = narrative.prepare(job)
                        continue
                    result["stop_state"] = claim["state"]
                    break
                request = claim["request"]
                response = transport.complete(request)
                validation = None
                if response["finish"] == "complete":
                    try:
                        stored = json.loads(store.db.execute("SELECT request_json FROM understanding_tasks WHERE id=?", (claim["task_id"],)).fetchone()[0])
                        (validate_output if request["phase"] == "organize" else validate_understanding)(hydrate(response["output"], stored), stored)
                    except (ValueError, KeyError, TypeError) as exc:
                        validation = str(exc)[:200]
                status = engine.submit(claim["attempt_id"], backend=options.backend, output=response["output"], usage=response.get("usage"), finish=response["finish"])
                attempt = dict(store.db.execute("SELECT state,error_code FROM understanding_attempts WHERE id=?", (claim["attempt_id"],)).fetchone())
                result["attempts"].append({"phase": request["phase"], "request": request, "response": response, "validation_error": validation, "runtime_error": attempt["error_code"], "attempt_state": attempt["state"]})
                print(json.dumps({"case": case["id"], "phase": request["phase"], "attempt": len(result["attempts"]), "error": status["error_code"], "validation": validation, "seconds": response["seconds"]}, ensure_ascii=False), flush=True)
                if request["phase"] == "organize" and attempt["state"] == "accepted":
                    result["published"] = narrative.publish(organizing["task_id"])
                    result["stop_state"] = "published_unreviewed"
                    break
            status = engine.status(job)
            result["usage"] = status["usage"]
            result["extracted"] = status["result"]
            result["excluded_policy"] = status["excluded_policy"]
            result["integrity_check"] = store.db.execute("PRAGMA integrity_check").fetchone()[0]
            result["foreign_key_errors"] = [list(r) for r in store.db.execute("PRAGMA foreign_key_check")]
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings-state", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--case", action="append")
    parser.add_argument("--corpus", type=Path, default=Path(__file__).with_name("cases-indexed.json"))
    parser.add_argument("--output-reserve", type=int, default=4000, choices=(4000, 8000))
    parser.add_argument("--chunk-chars", type=int, default=1200)
    args = parser.parse_args()
    # Explicitly selected application settings only; no environment credential lookup.
    from sessiongraph.provider_credentials import load_key, validate_endpoint
    from sessiongraph.provider_settings import read_template
    provider = read_template(args.settings_state)["provider"]
    endpoint = validate_endpoint(provider["endpoint"])
    if provider["protocol"] != "responses" or provider["auth"] != "bearer":
        raise ValueError("unsupported_evaluation_provider")
    key = load_key(args.settings_state, endpoint)
    if not key:
        raise ValueError("saved_endpoint_key_missing")
    transport = Transport(endpoint, key)
    transport.model = provider["model"]
    corpus_path = args.corpus
    cases = json.loads(corpus_path.read_text())
    if args.case:
        cases = [case for case in cases if case["id"] in args.case]
        if len(cases) != len(set(args.case)):
            raise ValueError("unknown_case")
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(), "model": transport.model, "model_policy": POLICY,
        "endpoint": endpoint, "credential_source": "explicit application settings, endpoint-bound saved key",
        "environment_credential_used": False, "hook_integrated": False,
        "corpus_sha256": hashlib.sha256(corpus_path.read_bytes()).hexdigest(),
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "runtime_source_sha256": {str(p.relative_to(Path(__file__).resolve().parents[1])): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((Path(__file__).resolve().parents[1] / "src/seywo").glob("*.py"))},
        "provider_capacity_verified": False, "real_chat_imported": False, "cases": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for case in cases:
        report["cases"].append(evaluate_case(case, transport, output_reserve=args.output_reserve, chunk_chars=args.chunk_chars))
        report["model_calls"] = transport.calls
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report": str(args.output), "calls": transport.calls}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
