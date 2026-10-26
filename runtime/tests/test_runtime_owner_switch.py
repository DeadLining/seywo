"""Migration fixtures never touch real registration, queues, credentials or graphs."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
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


def test_old_loaded_package_can_import_forwarded_module_after_replacement(tmp_path):
    gateway = ROOT.parent / "local-ai-gateway"
    package = tmp_path / "old/sessiongraph"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    state = tmp_path / "state"
    state.mkdir()
    (state / "runtime.json").write_text(json.dumps({"source": str(ROOT / "runtime")}))
    (package / "provider_credentials.py").write_bytes((gateway / "vendor/sessiongraph-compat/forward_module.py").read_bytes())
    code = "import sessiongraph; from sessiongraph import provider_credentials as p; print(p.__file__); assert p.validate_endpoint('https://example.test/v1/responses')"
    result = subprocess.run([sys.executable,"-B","-c",code],cwd=tmp_path,env={**os.environ,"PYTHONPATH":str(package.parent),"SESSIONGRAPH_RUNTIME_HOME":str(state)},capture_output=True,text=True,check=True)
    assert str(ROOT / "runtime/sessiongraph/provider_credentials.py") in result.stdout


def test_gateway_has_only_forwarders_and_no_retired_commands():
    gateway = ROOT.parent / "local-ai-gateway"
    resources = json.loads((gateway / "src-tauri/tauri.conf.json").read_text())["bundle"]["resources"]
    assert resources and all("sessiongraph-compat/" in path for path in resources)
    code = (gateway / "src-tauri/src/server.rs").read_text()
    assert "crate::session_graph::" not in code
    assert "crate::work_summary::" not in code
    assert "crate::work_materials::" not in code
    assert not (gateway / "vendor/sessiongraph").exists()
