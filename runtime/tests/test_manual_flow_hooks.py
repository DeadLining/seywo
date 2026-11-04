import importlib.util
import io
import json
from pathlib import Path

import pytest
from sessiongraph.core import init_project


@pytest.mark.parametrize('event', ['SessionStart', 'UserPromptSubmit', 'PreCompact', 'PostCompact', 'Stop', 'SessionEnd'])
def test_hook_does_not_generate_or_schedule(event, tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('manual_hook', Path(__file__).resolve().parents[1] / 'hooks/sg_codex_hook.py')
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    config = init_project(tmp_path / 'project')
    monkeypatch.setattr(hook, 'GLOBAL_SETTINGS', tmp_path / 'missing')
    monkeypatch.setattr(hook, '_find_config', lambda cwd: config)
    monkeypatch.setattr(hook, '_spawn_extraction', lambda *args: pytest.fail('unexpected extraction'))
    monkeypatch.setattr(hook, '_spawn_cleanup', lambda *args: pytest.fail('unexpected cleanup'))
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'hook_event_name': event, 'cwd': str(config.parent.parent), 'session_id': 'chat'})))
    assert hook.main() == 0
