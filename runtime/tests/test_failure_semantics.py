"""Failure semantics: excluded bulk output is not a failed extraction."""
import json

from sessiongraph.auto_update import run_update, update_status
from sessiongraph.core import Store, init_project, load_config
from sessiongraph.extraction import ProviderError
from sessiongraph.failures import Failure, describe
from sessiongraph.memories import CHUNK_CAP, run_chat, _iter_batches
from test_mvp import write_session


def _row(line, text, *, kind='message', role='user'):
    return {'id': f'e{line}', 'line': line, 'text': text, 'kind': kind, 'role': role,
            'timestamp': '2026-10-09T00:00:00Z'}


def test_large_excluded_tool_output_is_not_reported_as_oversize():
    rows = [_row(1, 'large tool output ' + 'x' * (CHUNK_CAP + 200), kind='tool_result', role='tool'),
            _row(2, 'durable user decision')]
    batches = list(_iter_batches(rows, 24000))
    assert batches[0][0] == [rows[1]]
    assert batches[0][1] == []


def test_large_extractable_entry_is_fragmented_not_dropped():
    rows = [_row(1, 'x' * (CHUNK_CAP + 1)), _row(2, 'keep this')]
    batches = list(_iter_batches(rows, 24000))
    assert len(batches) == 3
    assert all(not dropped for _, dropped, _, _ in batches)
    assert batches[0][0][0]['source_start'] == 0
    assert batches[1][0][0]['source_end'] == CHUNK_CAP + 1
    assert batches[-1][0] == [rows[1]]


def test_provider_timeout_is_classified_without_leaking_details(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    config = init_project(root)
    store = Store(load_config(config))
    source = write_session(root / 'chat.jsonl', root, 'chat-1', [('user', '需要保存的决定')])
    store.ingest(source)
    def timeout(batch, policy):
        raise ProviderError('provider request timed out after 180 seconds', 'provider_timeout')
    result = run_chat(store, timeout, 'chat-1')
    assert result['failed'][0]['error_code'] == 'provider_timeout'
    assert result['failure_codes'] == ['provider_timeout']
    assert '180 seconds' not in json.dumps(result)
    store.close()


def test_internal_lifecycle_detail_is_kept_but_arbitrary_text_is_not():
    assert describe(ValueError('cleanup_shared_node_without_evidence'))['detail'] == \
        'cleanup_shared_node_without_evidence'
    assert describe(ValueError('graph_source_changed'))['detail'] == 'graph_source_changed'
    secret = Failure('graph_io', detail='Authorization: Bearer sk-private')
    assert describe(secret)['detail'] == 'graph_io'
    unsafe = describe(ValueError('source text says password=hunter2'))
    assert unsafe['detail'] == 'graph_validation'
    assert 'hunter2' not in json.dumps(unsafe)


def test_oversize_warning_does_not_make_update_failed(tmp_path):
    config = init_project(tmp_path)
    Store(load_config(config)).close()
    def execute(argv, timeout):
        if 'refresh-chat' in argv:
            return {'added_entries': 1, 'skipped': []}
        return {'saved': ['one'], 'failed': [], 'failure_codes': [],
                'model_calls': 1, 'dropped_oversize': 2}
    result = run_update(config, 'chat', 'PostCompact', execute=execute)
    assert result['state'] == 'succeeded'
    assert result['warning_code'] == 'oversize_entries_skipped'
    assert result['warning_count'] == 2
    assert result.get('error_code') is None


def test_provider_failure_is_visible_as_specific_error(tmp_path):
    config = init_project(tmp_path)
    Store(load_config(config)).close()
    def execute(argv, timeout):
        if 'refresh-chat' in argv:
            return {'added_entries': 1, 'skipped': []}
        return {'saved': [], 'failed': [{'error_code': 'provider_timeout'}],
                'failure_codes': ['provider_timeout'], 'model_calls': 1}
    result = run_update(config, 'chat', 'PostCompact', execute=execute)
    assert result['state'] == 'failed'
    assert result['error_code'] == 'provider_timeout'
    assert result['failure_codes'] == ['provider_timeout']


def test_legacy_oversize_failure_is_normalized_without_rewriting_audit(tmp_path):
    config = init_project(tmp_path)
    Store(load_config(config)).close()
    folder = tmp_path / '.sessiongraph/hook-state/runs'
    folder.mkdir(parents=True)
    raw = {
        'run_id': 'legacy', 'chat_id': 'chat', 'event': 'PostCompact',
        'created_at': 1, 'updated_at': 2, 'state': 'failed',
        'saved_count': 8, 'failed_batches': 0, 'dropped_oversize': 3,
        'error_code': 'extraction_partial_failure',
    }
    path = folder / 'legacy.json'
    path.write_text(json.dumps(raw))
    status = update_status(load_config(config))
    assert status['latest']['state'] == 'succeeded'
    assert status['latest']['warning_code'] == 'oversize_entries_skipped'
    assert json.loads(path.read_text())['state'] == 'failed'
