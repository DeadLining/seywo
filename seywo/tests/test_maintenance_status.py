"""Failure visibility is local, bounded, source-free and independent of extraction."""
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from seywo.config import initialize, load_config
from seywo import maintenance_status as status


@pytest.fixture
def config(tmp_path):
    initialized = initialize(tmp_path / 'project', plugin_hook=True)
    return load_config(Path(initialized['config']))


def test_status_read_is_zero_write_and_zero_model(config):
    base = Path(config['_path']).parent
    before = set(base.rglob('*'))
    result = status.read(config, 'chat')
    assert result['state'] == 'idle' and result['model_calls'] == 0
    assert set(base.rglob('*')) == before


def test_failed_run_preserves_success_and_never_persists_source_or_raw_error(config):
    status.record(config, 'chat', 'done')
    first = status.read(config, 'chat')
    status.record(config, 'chat', 'running')
    status.record(config, 'chat', 'failed', error_code='private payload sk-secret /secret/path')
    result = status.read(config, 'chat')
    assert result['state'] == 'failed' and result['retryable'] and result['stale_view']
    assert result['last_success_at'] == first['last_success_at']
    raw = (Path(config['_path']).parent / 'maintenance/chat.json').read_text()
    assert 'sk-secret' not in raw and '/secret/path' not in raw
    assert 'sk-secret' not in json.dumps(result)
    assert len(raw.encode()) < 2048


def test_dead_worker_is_interrupted_and_retryable(config):
    status.record(config, 'chat', 'running')
    assert status.read(config, 'chat')['state'] == 'interrupted'
    assert status.read(config, 'chat')['retryable']


def test_live_lock_keeps_running_state(config):
    from seywo.runtime_platform import FileLease
    path = Path(config['_path']).parent / 'maintain/chat.lock'
    path.parent.mkdir(exist_ok=True)
    with FileLease(path) as handle:
        assert handle.acquire(blocking=False)
        status.record(config, 'chat', 'running')
        assert status.read(config, 'chat')['state'] == 'running'


def test_queued_launch_that_never_starts_is_not_running_forever(config, monkeypatch):
    status.record(config, 'chat', 'queued')
    assert status.read(config, 'chat')['state'] == 'queued'
    monkeypatch.setattr(status.time, 'time', lambda: 10**12)
    result = status.read(config, 'chat')
    assert result['state'] == 'interrupted' and result['retryable']


def test_notice_once_per_failure_and_recovery_resets_notice(config):
    status.record(config, 'chat', 'failed', error_code='source_missing')
    first = status.take_notice(config, 'chat')
    assert first and 'Seywo' in first
    assert status.take_notice(config, 'chat') is None
    status.record(config, 'chat', 'failed', error_code='source_missing')
    assert status.take_notice(config, 'chat') is None
    status.record(config, 'chat', 'running')
    status.record(config, 'chat', 'failed', error_code='source_missing')
    assert status.take_notice(config, 'chat')
    status.record(config, 'chat', 'done')
    assert status.take_notice(config, 'chat') is None


@pytest.mark.parametrize('code, phrase', [
    ('provider_not_configured', '模型'), ('missing_provider_credential', '凭据'),
    ('source_missing', '原始对话'), ('worker_start_failed', '启动'),
    ('context_ceiling', '上下文'), ('call_budget', '预算'),
])
def test_failure_copy_has_actionable_safe_reason(config, code, phrase):
    status.record(config, 'chat', 'failed', error_code=code)
    assert phrase in status.read(config, 'chat')['message']


@pytest.mark.parametrize('chat', ['../outside', '/tmp/outside', 'a/b', '', 'x'*129])
def test_status_rejects_untrusted_path_identity(config, chat):
    with pytest.raises(ValueError):
        status.record(config, chat, 'failed')
    with pytest.raises(ValueError):
        status.read(config, chat)


def test_corrupt_receipt_is_an_explicit_interruption(config):
    status.record(config, 'chat', 'done')
    (Path(config['_path']).parent / 'maintenance/chat.json').write_text('{broken')
    assert status.read(config, 'chat')['state'] == 'interrupted'


def test_worker_records_blocked_and_exception(config, monkeypatch):
    from seywo import maintain
    monkeypatch.setattr(maintain, '_run_chat', lambda *_: {'state': 'blocked', 'error_code': 'provider_not_configured'})
    assert maintain.run_chat(config, 'chat')['state'] == 'blocked'
    assert status.read(config, 'chat')['state'] == 'blocked'
    def crash(*_):
        raise RuntimeError('private content')
    monkeypatch.setattr(maintain, '_run_chat', crash)
    with pytest.raises(RuntimeError):
        maintain.run_chat(config, 'chat')
    assert status.read(config, 'chat')['state'] == 'failed'


def test_spawn_failure_is_visible(config, monkeypatch):
    from seywo import hook
    monkeypatch.setattr(hook.subprocess, 'Popen', Mock(side_effect=OSError('private path')))
    assert hook.spawn(Path(config['_path']), 'chat') is False
    result = status.read(config, 'chat')
    assert result['state'] == 'failed' and '启动' in result['message']


def test_disabled_status_is_not_retryable(config):
    disabled = {**config, 'hook': {**config['hook'], 'enabled': False}}
    assert status.read(disabled, 'chat')['state'] == 'disabled'
    assert not status.read(disabled, 'chat')['retryable']


def test_explicit_retry_is_single_flight_and_keeps_existing_progress(config, monkeypatch):
    from seywo import hook, maintenance_control
    state_path = Path(config['_path']).parent / 'maintain/chat.json'
    state_path.parent.mkdir(exist_ok=True)
    state_path.write_text('{"cursor":[4,10],"version":7}')
    before = state_path.read_bytes()
    calls = []
    monkeypatch.setattr(hook, 'spawn', lambda *a: calls.append(a) or True)
    status.record(config, 'chat', 'partial', error_code='call_budget')
    first = maintenance_control.retry(config, 'chat')
    second = maintenance_control.retry(config, 'chat')
    assert first['accepted'] and not second['accepted']
    assert first['maintenance']['state'] == 'queued' and len(calls) == 1
    assert first['model_calls'] == 0
    assert state_path.read_bytes() == before


def test_retry_spawn_error_leaves_failure_visible(config, monkeypatch):
    from seywo import hook, maintenance_control
    monkeypatch.setattr(hook.subprocess, 'Popen', Mock(side_effect=OSError('not launched')))
    status.record(config, 'chat', 'failed')
    result = maintenance_control.retry(config, 'chat')
    assert not result['accepted'] and result['maintenance']['state'] == 'failed'


def test_hook_failure_notice_is_native_bounded_and_emitted_once(config, monkeypatch):
    import io
    from seywo import hook
    status.record(config, 'chat', 'failed', error_code='source_missing')
    payload = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'chat',
               'cwd': config['project_root']}
    monkeypatch.setattr(hook, 'resume_context', lambda *a: 'existing context')
    outputs = []
    for _ in range(2):
        out = io.StringIO()
        monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(payload)))
        monkeypatch.setattr('sys.stdout', out)
        assert hook.main() == 0
        outputs.append(json.loads(out.getvalue()))
    assert outputs[0]['systemMessage'].startswith('Seywo：')
    assert len(outputs[0]['systemMessage']) < 300
    assert 'systemMessage' not in outputs[1]
    assert outputs[1]['hookSpecificOutput']['additionalContext'] == 'existing context'


def test_queue_write_failure_reports_without_breaking_compact(config, monkeypatch):
    import io
    from seywo import hook
    monkeypatch.setattr(hook.inbox, 'record', Mock(side_effect=OSError('sk-private-key')))
    out = io.StringIO()
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'hook_event_name': 'PreCompact',
        'session_id': 'chat', 'cwd': config['project_root']})))
    monkeypatch.setattr('sys.stdout', out)
    assert hook.main() == 0
    message = json.loads(out.getvalue())['systemMessage']
    assert '磁盘' in message and 'sk-private-key' not in message
    assert status.read(config, 'chat')['state'] == 'failed'


def test_bad_config_reports_without_replacing_config(config, monkeypatch):
    import io
    from seywo import hook
    path = Path(config['_path'])
    path.write_text('{bad')
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'hook_event_name': 'PreCompact',
        'session_id': 'chat', 'cwd': config['project_root']})))
    out = io.StringIO()
    monkeypatch.setattr('sys.stdout', out)
    assert hook.main() == 0
    assert '配置' in json.loads(out.getvalue())['systemMessage']
    assert path.read_text() == '{bad'


def test_failed_import_cannot_look_successful(config, monkeypatch):
    from seywo import inbox, maintain
    item = inbox.record(config, 'chat', 'PreCompact', '/missing/transcript')
    inbox.finish(config, item, state='failed', error='source_missing')
    monkeypatch.setattr(maintain, '_run_chat', lambda *_: {'state': 'done', 'calls': 0})
    report = maintain.run_chat(config, 'chat')
    assert report['state'] == 'failed'
    assert status.read(config, 'chat')['state'] == 'failed'


def test_open_native_turn_is_waiting_not_a_false_success_or_failure_notice(config, monkeypatch):
    from seywo import maintain
    monkeypatch.setattr(maintain, '_run_chat', lambda *_: {'state': 'done', 'calls': 0, 'deferred_turn': True})
    maintain.run_chat(config, 'chat')
    result = status.read(config, 'chat')
    assert result['state'] == 'partial' and result['retryable'] and result['waiting_for_turn']
    assert '本轮对话' in result['message']
    assert status.take_notice(config, 'chat') is None


def test_upgrade_reads_existing_failed_queue_without_writes(config):
    from seywo import inbox
    item = inbox.record(config, 'chat', 'PreCompact', '/missing/transcript')
    inbox.finish(config, item, state='failed', error='source_missing')
    before = set(Path(config['_path']).parent.rglob('*'))
    result = status.read(config, 'chat')
    assert result['state'] == 'failed' and '原始对话' in result['message']
    assert set(Path(config['_path']).parent.rglob('*')) == before
    assert status.take_notice(config, 'chat')
    assert status.take_notice(config, 'chat') is None
    assert '原始对话' in status.read(config, 'chat')['message']


def test_source_size_failure_recovers_specific_reason_from_inbox(config):
    from seywo import inbox

    item = inbox.record(config, 'chat', 'PreCompact', '/missing/transcript')
    inbox.finish(config, item, state='failed', error='source_archive_capacity')
    status.record(config, 'chat', 'failed', error_code='operation_failed')
    result = status.read(config, 'chat')
    assert result['state'] == 'failed' and result['retryable']
    assert '超出归档容量' in result['message']


def test_outer_worker_exception_does_not_overwrite_successor(config, monkeypatch, capsys):
    from seywo import maintain
    def successor_started(*_):
        # The old run has already released its lock and a successor owns work.
        status.record(config, 'chat', 'queued')
        raise RuntimeError('old worker exception unwinds to CLI')
    monkeypatch.setattr(maintain, 'run_chat', successor_started)
    assert maintain.main(['--config', config['_path'], '--chat', 'chat']) == 1
    assert status.read(config, 'chat')['state'] == 'queued'


def test_completion_exposes_published_version_for_parallel_panel_reads(config):
    status.record(config, 'chat', 'done', report={'version': 7, 'calls': 1})
    assert status.read(config, 'chat')['published_version'] == 7
    status.record(config, 'chat', 'failed')
    assert status.read(config, 'chat')['published_version'] == 7


@pytest.mark.parametrize('code, phrase', [('codex_execution_failed','模型连接'),
    ('transport_error','模型连接'), ('http_401','凭据'), ('http_429','限流'),
    ('source_archive_capacity','归档容量'), ('source_record_too_large: import rolled back without truncation','单条'),
    ('compact_invalid_evidence','校验')])
def test_real_backend_error_codes_have_safe_actionable_copy(config, code, phrase):
    status.record(config, 'chat', 'failed', error_code=code)
    assert phrase in status.read(config, 'chat')['message']
