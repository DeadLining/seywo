"""Manual sequential live A/B probe with protected manual edit and late evidence."""

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from model_probe import Transport, evaluate_case, record
from sessiongraph.provider_credentials import load_key
from sessiongraph.provider_settings import read_template
from seywo.config import load_config
from seywo.narrative import Narrative
from seywo.model_contract import POLICY, hydrate
from seywo.narrative_contract import validate_output
from seywo.contracts import validate_understanding
from seywo.provider import Capacity
from seywo.store import Store
from seywo.understanding import Limits, Options, Understanding

parser = argparse.ArgumentParser()
parser.add_argument("--settings-state", required=True, type=Path)
parser.add_argument("--output", required=True, type=Path)
parser.add_argument("--output-reserve", type=int, default=4000, choices=(4000, 8000))
args = parser.parse_args()
state = args.settings_state
p = read_template(state)["provider"]
transport = Transport(p["endpoint"], load_key(state, p["endpoint"]))
transport.model = p["model"]
root = Path(tempfile.mkdtemp(prefix="seywo-live-reader-")) / "project"
case = json.loads(Path(__file__).with_name("cases-indexed.json").read_text())[0]
r = {
    "initial": evaluate_case(
        case, transport, output_reserve=args.output_reserve, project_directory=root
    ),
    "stages": [],
    "hook_integrated": False, "model_policy": POLICY,
    "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    "runtime_source_sha256": {str(f.relative_to(Path(__file__).resolve().parents[1])): hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted((Path(__file__).resolve().parents[1] / "src/seywo").glob("*.py"))},
    "model": transport.model,
    "project_directory": str(root),
}
args.output.write_text(json.dumps(r, ensure_ascii=False, indent=2))
if "published" not in r["initial"]:
    raise SystemExit("initial_not_published")
config = load_config(root / ".seywo/config.json")
with Store(config) as store:
    engine = Understanding(store)
    narrative = Narrative(store)
    chat = case["id"]
    view = narrative.read(chat)
    selected = view["narrative"]["items"][0]
    locked = narrative.edit(
        chat,
        base_version=view["version"],
        override_revision=view["override_revision"],
        actor="explicit-evaluation-operator",
        patch={
            "entity": "item",
            "id": selected["id"],
            "fields": {
                "content": "人工修正：本轮仅允许生成迁移预览，生产迁移必须等待用户另行明确确认。"
            },
        },
    )
    r["manual_edit"] = {
        "before": view,
        "after": locked,
        "locked_item_id": selected["id"],
    }

    def drive(job, stage):
        attempts = []
        task = None
        published = None
        for _ in range(20):
            claim = engine.claim(job, backend="explicit-live-eval")
            if claim["state"] != "request":
                if claim["state"] in {"ready", "partial"} and task is None:
                    task = narrative.prepare(job)
                    continue
                break
            req = claim["request"]
            response = transport.complete(req)
            validation = None
            if response["finish"] == "complete":
                try:
                    private = json.loads(store.db.execute("SELECT request_json FROM understanding_tasks WHERE id=?", (claim["task_id"],)).fetchone()[0])
                    (validate_output if req["phase"] == "organize" else validate_understanding)(hydrate(response["output"], private), private)
                except (ValueError, KeyError, TypeError) as exc:
                    validation = str(exc)
            status = engine.submit(
                claim["attempt_id"],
                backend="explicit-live-eval",
                output=response["output"],
                usage=response.get("usage"),
                finish=response["finish"],
            )
            attempts.append(
                {
                    "request": req,
                    "response": response,
                    "runtime_error": status["error_code"], "validation_error": validation,
                }
            )
            print(
                json.dumps(
                    {
                        "stage": stage,
                        "phase": req["phase"],
                        "error": status["error_code"],
                        "seconds": response["seconds"],
                    }
                ),
                flush=True,
            )
            if req["phase"] == "organize" and status["error_code"] is None:
                published = narrative.publish(task["task_id"])
                break
        result = {
            "stage": stage,
            "attempts": attempts,
            "status": engine.status(job),
            "published": published,
            "current": narrative.read(chat),
        }
        r["stages"].append(result)
        r["model_calls"] = transport.calls
        args.output.write_text(json.dumps(r, ensure_ascii=False, indent=2))

    drive(r["initial"]["job_id"], "model_after_manual_edit")
    path = root / "synthetic.jsonl"
    with path.open("a") as stream:
        stream.write(
            json.dumps(
                record(
                    {
                        "role": "assistant",
                        "text": "追加验收结果：订单表校验失败，发布条件未满足。测试没有全部通过，生产迁移必须保持阻止。",
                    },
                    99,
                ),
                ensure_ascii=False,
            )
            + "\n"
        )
    with path.open("a") as stream:
        stream.write(json.dumps(record({"role": "tool", "text": "EXCLUDED_SEQUENCE_TOOL_BODY_7849"}, 100)) + "\n")
    store.import_source(path, chat, 0)
    job = engine.prepare(
        chat,
        Options(
            "explicit-live-eval",
            transport.model,
            Capacity(64000, 32000, 8000),
            context_chars=200,
            chunk_chars=1200,
            lease_seconds=180,
            output_reserve=args.output_reserve,
        ),
        Limits(
            total_input_tokens=400000,
            total_output_tokens=100000,
            call_count=30,
            wall_seconds=3600,
            retry_limit=2,
        ),
    )["job_id"]
    drive(job, "late_counterevidence")
    r["integrity_check"] = store.db.execute("PRAGMA integrity_check").fetchone()[0]
    r["foreign_key_errors"] = [
        list(x) for x in store.db.execute("PRAGMA foreign_key_check")
    ]
    r["model_calls"] = transport.calls
    args.output.write_text(json.dumps(r, ensure_ascii=False, indent=2))
print(json.dumps({"reader_project": str(root), "calls": transport.calls}), flush=True)
