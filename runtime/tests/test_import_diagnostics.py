"""Import classification must not hide loss or unnecessarily block valid sources."""
import json

import pytest

from sessiongraph.auto_update import run_update, update_status
from sessiongraph.continuity import refresh_chat
from sessiongraph.core import Store, digest, init_project, load_config
from test_mvp import write_session


def setup(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    logs = tmp_path / 'logs'
    logs.mkdir()
    path = init_project(root)
    config = load_config(path)
    config['hook']['allowed_transcript_roots'] = [str(logs)]
    return path, root, logs, Store(config)


def test_unrelated_candidates_are_nonblocking_and_typed(tmp_path):
    _, root, logs, store = setup(tmp_path)
    write_session(logs / 'good-chat-A.jsonl', root, 'chat-A', [('user', '目标')])
    write_session(logs / 'foreign-chat-A.jsonl', tmp_path / 'other', 'chat-A', [('user', '不要读')])
    write_session(logs / 'different-chat-A.jsonl', root, 'chat-B', [('user', '不要读')])
    result = refresh_chat(store, 'chat-A')
    assert result['added_entries'] == 1
    assert {s['code'] for s in result['skipped']} == {'outside_project', 'chat_identity_mismatch'}
    assert all(s['severity'] == 'warning' and not s['blocking'] for s in result['skipped'])
    assert store.db.execute('select count(*) from entries').fetchone()[0] == 1
    store.close()


@pytest.mark.parametrize('damage,code', [('missing', 'source_missing'), ('rewrite', 'source_changed'),
                                         ('identity', 'chat_identity_mismatch')])
def test_known_source_loss_blocks_and_keeps_index(tmp_path, damage, code):
    _, root, logs, store = setup(tmp_path)
    source = write_session(logs / 'good-chat-A.jsonl', root, 'chat-A', [('user', '旧内容')])
    refresh_chat(store, 'chat-A')
    if damage == 'missing':
        source.unlink()
    elif damage == 'identity':
        write_session(source, root, 'chat-B', [('user', '新内容')])
    else:
        write_session(source, root, 'chat-A', [('user', '新内容')])
    result = refresh_chat(store, 'chat-A')
    failure = result['skipped'][0]
    assert failure['code'] == code and failure['blocking']
    assert failure['known_source'] and failure['severity'] == 'error'
    assert store.db.execute('select text_hash from sg_source_locations').fetchone()[0] == digest('旧内容')
    assert store.db.execute('select count(*) from sg_external_entries').fetchone()[0] == 1
    store.close()


def test_malformed_source_is_classified_without_raw_error(tmp_path):
    _, root, logs, store = setup(tmp_path)
    source = write_session(logs / 'bad-chat-A.jsonl', root, 'chat-A', [('user', '正常')])
    with source.open('a') as out:
        out.write('secret-transcript-or-token\n')
    result = refresh_chat(store, 'chat-A')
    failure = result['skipped'][0]
    assert failure['code'] == 'invalid_event' and failure['line'] == 3
    assert failure['blocking'] and 'secret-transcript' not in json.dumps(result)
    assert store.db.execute('select count(*) from entries').fetchone()[0] == 0
    store.close()


def run_with_result(tmp_path, imported):
    config = init_project(tmp_path)
    Store(load_config(config)).close()
    calls = []
    def execute(argv, timeout):
        calls.append(argv)
        if 'refresh-chat' in argv:
            return imported
        return {'saved': [], 'failed': [], 'model_calls': 0}
    result = run_update(config, 'chat-A', 'PostCompact', execute=execute)
    assert update_status(load_config(config))['latest'] == result
    return result, calls


def issue(code='outside_project', blocking=False):
    return {'path': '/logs/rollout.jsonl', 'code': code, 'blocking': blocking,
            'severity': 'error' if blocking else 'warning', 'known_source': False,
            'reason': 'ignored', 'retryable': False}


def test_safe_exclusions_do_not_block_extraction_and_diagnostics_survive(tmp_path):
    result, calls = run_with_result(tmp_path, {'imports': [{'added': 1}], 'added_entries': 1,
                                             'skipped': [issue()]})
    assert len(calls) == 2 and result['state'] == 'succeeded'
    assert result['import_diagnostics']['counts_by_code'] == {'outside_project': 1}
    assert result['warning_code'] == 'import_sources_excluded'
    assert result['import_diagnostics']['sources'][0]['path'] == '/logs/rollout.jsonl'


def test_real_failure_blocks_even_with_valid_imports(tmp_path):
    result, calls = run_with_result(tmp_path, {'imports': [{'added': 1}], 'added_entries': 1,
                                             'skipped': [issue('source_changed', True)]})
    assert len(calls) == 1 and result['state'] == 'failed'
    assert result['error_code'] == 'import_source_changed'
    assert result['import_diagnostics']['blocking_count'] == 1


@pytest.mark.parametrize('skipped', [[], [issue()]])
def test_no_valid_source_is_not_success(tmp_path, skipped):
    result, calls = run_with_result(tmp_path, {'imports': [], 'added_entries': 0, 'skipped': skipped})
    assert len(calls) == 1
    assert result['error_code'] == ('import_project_binding_conflict' if skipped else 'import_sources_not_found')


def test_diagnostics_are_bounded_counts_are_complete(tmp_path):
    result, _ = run_with_result(tmp_path, {'imports': [{'added': 0}], 'added_entries': 0,
                                         'skipped': [issue() for _ in range(35)]})
    diagnostics = result['import_diagnostics']
    assert len(diagnostics['sources']) == 20 and diagnostics['omitted_sources'] == 15
    assert diagnostics['counts_by_code']['outside_project'] == 35


def test_unknown_legacy_reason_stays_blocking_and_private(tmp_path):
    result, calls = run_with_result(tmp_path, {'imports': [{'added': 0}], 'added_entries': 0,
                                             'skipped': [{'reason': 'secret-value'}]})
    assert len(calls) == 1 and result['state'] == 'failed'
    assert 'secret-value' not in json.dumps(result)


def test_incomplete_tail_is_visible_but_complete_entries_can_extract(tmp_path):
    result, calls = run_with_result(tmp_path, {'imports': [{'added': 1, 'incomplete_tail': True}],
                                             'added_entries': 1, 'skipped': []})
    assert len(calls) == 2 and result['state'] == 'succeeded'
    assert result['import_diagnostics']['incomplete_tail_count'] == 1
    assert result['warning_code'] == 'import_incomplete_tail'


def test_unfinished_initial_metadata_blocks_before_extraction(tmp_path):
    _, root, logs, store = setup(tmp_path)
    source = logs / 'new-chat-A.jsonl'
    source.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'chat-A', 'cwd': str(root)}}))
    result = refresh_chat(store, 'chat-A')
    assert not result['imports']
    assert result['skipped'][0]['code'] == 'incomplete_metadata'
    assert result['skipped'][0]['blocking'] and result['skipped'][0]['retryable']
    store.close()


@pytest.mark.parametrize('exception,code', [
    (PermissionError('private detail'), 'permission_denied'),
    (OSError('private detail'), 'io_error'),
    (ValueError('source exceeds 128 MiB import limit'), 'source_too_large'),
    (ValueError('invalid session metadata'), 'invalid_metadata'),
    (ValueError('unexpected private detail'), 'unknown_import_error'),
])
def test_exception_classifier_never_echoes_raw_error(exception, code):
    from sessiongraph.import_diagnostics import classify_exception
    item = classify_exception('/logs/example.jsonl', exception)
    assert item['code'] == code and item['blocking']
    assert 'private detail' not in json.dumps(item)


@pytest.mark.parametrize('raw', ['not-json\n', '[]\n', '{"type":"session_meta","payload":null}\n'])
def test_malformed_metadata_has_safe_diagnostic(tmp_path, raw):
    _, _, logs, store = setup(tmp_path)
    (logs / 'bad-chat-A.jsonl').write_text(raw)
    result = refresh_chat(store, 'chat-A')
    assert result['skipped'][0]['code'] in {'invalid_json', 'invalid_metadata'}
    assert result['skipped'][0]['blocking']
    store.close()


def test_complete_prefix_of_incomplete_tail_remains_incremental(tmp_path):
    _, root, logs, store = setup(tmp_path)
    source = write_session(logs / 'good-chat-A.jsonl', root, 'chat-A', [('user', '已完整')])
    with source.open('a') as out:
        out.write('{"type":')
    result = refresh_chat(store, 'chat-A')
    assert result['added_entries'] == 1 and result['imports'][0]['incomplete_tail']
    assert not result['skipped']
    with source.open('a') as out:
        out.write('"event_msg", "payload": {}}\n')
    result = refresh_chat(store, 'chat-A')
    assert result['added_entries'] == 0 and not result['imports'][0]['incomplete_tail']
    store.close()
