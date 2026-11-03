"""Migration fixtures never touch real registration, queues, credentials or graphs."""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def test_source_switch_is_idempotent_and_preserves_shared_state(tmp_path):
    state = tmp_path / "shared state"
    state.mkdir()
    originals = {"projects.json": '{"projects":[]}', "continuations/queue.json": '{"fixture":"pending"}', "extract.lock": "", "credentials/fixture.json": "private fixture"}
    for name, body in originals.items():
        path = state / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    args = [sys.executable, "-B", str(ROOT / "scripts/switch-runtime.py"), "--state", str(state), "--source", str(ROOT / "runtime"), "--python", sys.executable]
    first = subprocess.run(args, check=True, text=True, capture_output=True)
    assert len(json.loads(first.stdout)["changed"]) == 2
    second = subprocess.run(args, check=True, text=True, capture_output=True)
    assert json.loads(second.stdout)["changed"] == []
    for name, body in originals.items():
        assert (state / name).read_text() == body
    result = subprocess.run([str(state / "bin/sessiongraph"), "runtime-status"], check=True, text=True, capture_output=True)
    data = json.loads(result.stdout)
    assert data["source"] == str(ROOT / "runtime")
    assert data["model_calls"] == 0 and data["http_daemon"] is False
