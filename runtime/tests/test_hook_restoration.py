"""Restore opportunities survive read/output failures and concurrent compaction."""
import io
import json
import time
from types import SimpleNamespace

import pytest

from sessiongraph.core import init_project
from test_global_hooks import hook_module


def setup(tmp_path, monkeypatch):
    config = init_project(tmp_path)
    hook = hook_module()
    monkeypatch.setattr(hook, 'GLOBAL_SETTINGS', tmp_path / 'absent-global-settings.json')
    monkeypatch.setattr(hook, '_spawn_extraction', lambda *a: pytest.fail('read must not spawn extraction'))
    output = io.StringIO()
    monkeypatch.setattr('sys.stdout', output)
    return hook, config, output


def invoke(hook, config, monkeypatch, event='UserPromptSubmit'):
    payload = dict(hook_event_name=event, session_id='chat', cwd=str(config.parent.parent))
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(payload)))
    assert hook.main() == 0


def test_failed_read_keeps_marker_until_successfully_flushed_output(tmp_path, monkeypatch):
    hook, config, output = setup(tmp_path, monkeypatch)
    hook._mark_needs_restore(config, 'chat')
    token = hook._pending_restore(config, 'chat')
    monkeypatch.setattr(hook, '_extract_context', lambda c: None)
    invoke(hook, config, monkeypatch)
    assert output.getvalue() == '' and hook._pending_restore(config, 'chat') == token
    monkeypatch.setattr(hook, '_extract_context', lambda c: 'verified context')
    invoke(hook, config, monkeypatch)
    assert hook._pending_restore(config, 'chat') is None
    invoke(hook, config, monkeypatch)
    assert len(output.getvalue().splitlines()) == 1
    assert json.loads(output.getvalue())['hookSpecificOutput']['additionalContext'] == 'verified context'


def test_session_start_read_failure_can_retry_at_next_prompt(tmp_path, monkeypatch):
    hook, config, output = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(hook, '_extract_context', lambda c: None)
    invoke(hook, config, monkeypatch, event='SessionStart')
    assert hook._pending_restore(config, 'chat')
    monkeypatch.setattr(hook, '_extract_context', lambda c: 'retry context')
    invoke(hook, config, monkeypatch)
    assert hook._pending_restore(config, 'chat') is None and 'retry context' in output.getvalue()


def test_stdout_flush_failure_keeps_restore_opportunity(tmp_path, monkeypatch):
    hook, config, _ = setup(tmp_path, monkeypatch)
    hook._mark_needs_restore(config, 'chat')
    token = hook._pending_restore(config, 'chat')
    monkeypatch.setattr(hook, '_extract_context', lambda c: 'context')

    class BrokenPipe(io.StringIO):
        def flush(self):
            raise BrokenPipeError('closed output')

    monkeypatch.setattr('sys.stdout', BrokenPipe())
    invoke(hook, config, monkeypatch)
    assert hook._pending_restore(config, 'chat') == token


def test_new_compaction_revision_is_not_consumed_by_old_delivery(tmp_path, monkeypatch):
    hook, config, _ = setup(tmp_path, monkeypatch)
    hook._mark_needs_restore(config, 'chat')
    old = hook._pending_restore(config, 'chat')
    monkeypatch.setattr(hook, '_extract_context', lambda c: 'old context')

    def newer_compaction(context):
        hook._mark_needs_restore(config, 'chat')
        return True

    monkeypatch.setattr(hook, '_emit', newer_compaction)
    invoke(hook, config, monkeypatch)
    new = hook._pending_restore(config, 'chat')
    assert new and new != old
    assert not hook._ack_restore(config, 'chat', old)
    assert hook._pending_restore(config, 'chat') == new


def test_empty_success_does_not_repeat_database_reads_on_every_prompt(tmp_path, monkeypatch):
    hook, config, output = setup(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(hook, '_extract_context', lambda c: calls.append(c) or '')
    invoke(hook, config, monkeypatch, event='SessionStart')
    invoke(hook, config, monkeypatch)
    assert calls == [config] and output.getvalue() == ''
    assert hook._pending_restore(config, 'chat') is None


@pytest.mark.parametrize('raw', ['{', '[]', '{}', '{"memories":null}', '{"memories":"bad"}'])
def test_invalid_resume_payload_is_failure_not_successful_empty(tmp_path, monkeypatch, raw):
    hook, config, _ = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(hook.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout=raw))
    assert hook._extract_context(config) is None


def test_subprocess_failure_keeps_distinct_empty_contract(tmp_path, monkeypatch):
    hook, config, _ = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(hook.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1, stdout=''))
    assert hook._extract_context(config) is None
    monkeypatch.setattr(hook.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout='{"memories":[]}'))
    assert hook._extract_context(config) == ''


def test_legacy_timestamp_flags_are_acknowledged_only_after_delivery(tmp_path, monkeypatch):
    hook, config, _ = setup(tmp_path, monkeypatch)
    flag = hook._flag_path(config, 'chat')
    flag.parent.mkdir(parents=True)
    flag.write_text(str(time.time()))
    token = hook._pending_restore(config, 'chat')
    assert token and flag.exists()
    assert hook._ack_restore(config, 'chat', token) and not flag.exists()


@pytest.mark.parametrize('value', ['NaN', 'true', '"not-a-timestamp"', '{"created_at":"bad"}'])
def test_invalid_flag_is_not_deleted_as_if_delivered(tmp_path, monkeypatch, value):
    hook, config, _ = setup(tmp_path, monkeypatch)
    flag = hook._flag_path(config, 'chat')
    flag.parent.mkdir(parents=True)
    flag.write_text(value)
    assert hook._pending_restore(config, 'chat') is None and flag.read_text() == value


def test_context_budget_keeps_whole_items_and_source_ids(tmp_path, monkeypatch):
    hook, config, _ = setup(tmp_path, monkeypatch)
    memories = [dict(kind='finding', title='long conclusion', status='active',
                     content='x' * 5900 + ' NOT ACCEPTED', source={'entry_id': 'e_long'}),
                dict(kind='decision', title='criterion', status='active',
                     content='drift <= 0.10m; success is not established', source={'entry_id': 'e_short'})]
    monkeypatch.setattr(hook.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=json.dumps(dict(memories=memories))))
    context = hook._extract_context(config)
    assert len(context) <= hook.MAX_CONTEXT_CHARS
    assert 'long conclusion' not in context and 'xxx' not in context
    assert 'drift <= 0.10m; success is not established' in context
    assert 'source.entry_id: e_short' in context and 'Omitted 1 selected handoff items' in context
