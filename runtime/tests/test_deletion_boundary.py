"""Deletion is not inferred from lifecycle hooks or untrusted notifications.

These are safety boundaries, not acceptance tests for an implemented purge.
"""
import io
import json

import pytest

from sessiongraph import hook_ingress
from sessiongraph.core import init_project
from sessiongraph.global_hooks import merge_hooks
from test_global_hooks import hook_module


@pytest.mark.parametrize('payload', [
    {'hook_event_name': 'SessionEnd', 'reason': 'other'},
    {'hook_event_name': 'SessionEnd', 'reason': 'deleted'},
    {'hook_event_name': 'SessionEnd', 'reason': 'archived'},
    {'hook_event_name': 'ThreadDeleted'},
    {'hook_event_name': 'thread/deleted'},
    {'method': 'thread/deleted', 'params': {'threadId': 'chat'}},
    {'method': 'thread/archived', 'params': {'threadId': 'chat'}},
])
def test_hook_does_not_promote_ambiguous_or_forged_events(tmp_path, monkeypatch, payload):
    hook = hook_module()

    def forbidden(*args, **kwargs):
        pytest.fail('untrusted deletion input must not reach project operations')

    for name in ('ensure_project', '_find_config', '_spawn_extraction', '_extract_context'):
        monkeypatch.setattr(hook, name, forbidden)
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({
        **payload, 'cwd': str(tmp_path), 'session_id': 'chat',
        'transcript_path': str(tmp_path / 'missing.jsonl'),
    })))
    output = io.StringIO()
    monkeypatch.setattr('sys.stdout', output)
    assert hook.main() == 0
    assert output.getvalue() == ''
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('event', ['SessionEnd', 'ThreadDeleted', 'thread/deleted', 'thread/archived'])
def test_ingress_rejects_deletion_before_opening_queue(tmp_path, monkeypatch, event):
    config = init_project(tmp_path)
    monkeypatch.setattr(hook_ingress, 'enabled', lambda config: True)
    from sessiongraph import continuation
    monkeypatch.setattr(continuation, 'root', lambda *_: pytest.fail('must not open queue'))
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with pytest.raises(ValueError, match='invalid ingress identity or event'):
        hook_ingress.notify(config, 'chat', event, str(tmp_path / 'missing.jsonl'), launch=False)
    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert after == before


def test_registration_does_not_treat_session_end_as_delete():
    unrelated = {'hooks': {'SessionEnd': [{'hooks': [
        {'type': 'command', 'command': '/existing/session-end-handler'},
    ]}]}}
    merged = merge_hooks(unrelated, '/sessiongraph/hook.py')
    assert merged['hooks']['SessionEnd'] == unrelated['hooks']['SessionEnd']
    assert 'ThreadDeleted' not in merged['hooks']
    assert 'thread/deleted' not in merged['hooks']
