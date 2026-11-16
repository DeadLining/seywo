import importlib.util
import io
import json
from pathlib import Path

import pytest
from sessiongraph.core import init_project


@pytest.mark.parametrize(
    ('event', 'extracts', 'cleans'),
    [
        ('SessionStart', False, False),
        ('UserPromptSubmit', False, False),
        ('PreCompact', True, False),
        ('PostCompact', True, False),
        ('Stop', False, False),
        ('SessionEnd', False, True),
    ],
)
def test_hook_only_schedules_background_work_without_generating(
        event, extracts, cleans, tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('manual_hook', Path(__file__).resolve().parents[1] / 'hooks/sg_codex_hook.py')
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    config = init_project(tmp_path / 'project')
    raw = json.loads(config.read_text())
    raw['hook']['durable_ingress'] = False
    raw['hook']['cleanup_missing_chats'] = True
    config.write_text(json.dumps(raw))
    transcript = tmp_path / 'transcript.jsonl'
    transcript.write_text('')
    monkeypatch.setattr(hook, 'GLOBAL_SETTINGS', tmp_path / 'missing')
    monkeypatch.setattr(hook, '_find_config', lambda cwd: config)
    monkeypatch.setattr(hook, '_extract_context', lambda *args: None)
    monkeypatch.setattr('sessiongraph.tier_backfill.start', lambda *args: None)
    monkeypatch.setattr('sessiongraph.deletion_observer.start', lambda *args: None)
    extractions, cleanups = [], []
    monkeypatch.setattr(hook, '_spawn_extraction', lambda *args: extractions.append(args))
    monkeypatch.setattr(hook, '_spawn_cleanup', lambda *args: cleanups.append(args))
    output = io.StringIO()
    monkeypatch.setattr('sys.stdout', output)
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({
        'hook_event_name': event, 'cwd': str(config.parent.parent), 'session_id': 'chat',
        'transcript_path': str(transcript)})))
    assert hook.main() == 0
    assert bool(extractions) is extracts
    assert bool(cleanups) is cleans
    assert output.getvalue() == ''
