"""Compact commits dialogue units, never a prefix of a live native turn."""
import json
from pathlib import Path

from seywo import inbox, maintain
from seywo.store import Store
from seywo.config import initialize, load_config
from test_compact_maintain import CompactTransport
from test_s2 import message


def configured(tmp_path, monkeypatch):
    initialize(tmp_path)
    path = tmp_path / '.seywo/config.json'
    data = json.loads(path.read_text())
    data['hook'] = {'enabled': True, 'pipeline': 'seywo-compact-v1'}
    path.write_text(json.dumps(data))
    config = load_config(path)
    transport = CompactTransport()
    monkeypatch.setattr(maintain, 'Transport', lambda *a, **kw: transport)
    monkeypatch.setattr(maintain, 'read_provider', lambda config=None: {
        'endpoint': 'http://127.0.0.1:1/responses', 'model': transport.model,
        'timeout': 5, 'max_output_tokens': 1200, '_api_key': 'fixture'})
    return config, transport


def write_source(tmp_path, rows):
    source = tmp_path / 'live.jsonl'
    source.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))
    return source


def native_turn(tmp_path):
    return [
        {'type': 'session_meta', 'payload': {'id': 'live-chat', 'cwd': str(tmp_path)}},
        {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 't1'}},
        {'type': 'turn_context', 'payload': {'turn_id': 't1'}},
        message('检查 Hook 是否就绪'),
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
            'channel': 'commentary', 'content': [{'type': 'output_text', 'text': '正在检查'}]}},
    ]


def run(config, source):
    inbox.record(config, 'live-chat', 'PreCompact', str(source))
    return maintain.run_chat(config, 'live-chat')


def test_midturn_compact_defers_and_later_submits_complete_question_answer(tmp_path, monkeypatch):
    config, transport = configured(tmp_path, monkeypatch)
    rows = native_turn(tmp_path)
    source = write_source(tmp_path, rows)
    first = run(config, source)
    assert first['calls'] == 0
    state = json.loads((Path(config['_path']).parent / 'maintain/live-chat.json').read_text())
    assert state.get('cursor') is None
    rows.extend([message('四个 Hook 已就绪，可以验收', 'assistant'),
                 {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 't1'}}])
    second = run(config, write_source(tmp_path, rows))
    assert second['calls'] == 1
    assert transport.payloads[0]['turns'] == [
        {'index': 0, 'user': '检查 Hook 是否就绪', 'assistant': '四个 Hook 已就绪，可以验收'}]
    assert run(config, source)['calls'] == 0


def test_legacy_cursor_inside_turn_rehydrates_original_question(tmp_path, monkeypatch):
    from seywo.store import Store
    config, transport = configured(tmp_path, monkeypatch)
    rows = native_turn(tmp_path)
    source = write_source(tmp_path, rows)
    run(config, source)
    with Store(config) as store:
        last = store.db.execute('SELECT ordinal,max(record_no) FROM current_sources').fetchone()
        store.db.execute('INSERT OR REPLACE INTO delta_progress VALUES(?,?,?,?)',
                         ('live-chat', json.dumps(list(last)), 0, 1))
        store.db.commit()
    rows.extend([message('已完成核验', 'assistant'),
                 {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 't1'}}])
    assert run(config, write_source(tmp_path, rows))['calls'] == 1
    assert transport.payloads[0]['turns'][0]['user'] == '检查 Hook 是否就绪'


def test_new_turn_does_not_implicitly_complete_an_aborted_turn(tmp_path, monkeypatch):
    config, transport = configured(tmp_path, monkeypatch)
    rows = native_turn(tmp_path)
    rows.append({'type': 'event_msg', 'payload': {'type': 'turn_aborted', 'turn_id': 't1'}})
    rows.extend([{'type': 'turn_context', 'payload': {'turn_id': 't2'}},
                 message('改为检查检索'), message('检索已验证', 'assistant')])
    report = run(config, write_source(tmp_path, rows))
    assert report['calls'] == 1
    assert transport.payloads[0]['turns'][0]['user'] == '检查 Hook 是否就绪'
    assert transport.payloads[0]['turns'][0]['assistant'] == ''
    assert len(transport.payloads[0]['turns']) == 2


def test_closed_prefix_commits_while_the_last_turn_stays_pending(tmp_path, monkeypatch):
    config, fake = configured(tmp_path, monkeypatch)
    rows = native_turn(tmp_path)
    rows.extend([message('Hook已验证', 'assistant'),
        {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 't1'}},
        {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 't2'}},
        {'type': 'turn_context', 'payload': {'turn_id': 't2'}}, message('再检查检索')])
    report = run(config, write_source(tmp_path, rows))
    assert report['calls'] == 1
    from seywo import maintenance_status
    assert maintenance_status.read(config, 'live-chat')['waiting_for_turn']
    assert len(fake.payloads[0]['turns']) == 1
    assert fake.payloads[0]['turns'][0]['assistant'] == 'Hook已验证'
    assert run(config, tmp_path / 'live.jsonl')['calls'] == 0
    rows.extend([message('检索已经验证', 'assistant'),
        {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 't2'}}])
    assert run(config, write_source(tmp_path, rows))['calls'] == 1
    assert len(fake.payloads[-1]['turns']) == 1
    assert fake.payloads[-1]['turns'][0]['user'] == '再检查检索'


def test_terminal_event_without_new_body_does_not_replay_a_committed_turn(tmp_path, monkeypatch):
    config, fake = configured(tmp_path, monkeypatch)
    rows = native_turn(tmp_path)
    answer = message('Hook已验证', 'assistant')
    answer['payload']['channel'] = 'final'
    rows.append(answer)
    assert run(config, write_source(tmp_path, rows))['calls'] == 1
    rows.append({'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 't1'}})
    assert run(config, write_source(tmp_path, rows))['calls'] == 0
    assert fake.calls == 1


def test_unterminated_final_record_never_seals_the_turn(tmp_path, monkeypatch):
    config, fake = configured(tmp_path, monkeypatch)
    rows = native_turn(tmp_path)
    answer = message('完整的最终结论', 'assistant')
    answer['payload']['channel'] = 'final'
    source = write_source(tmp_path, rows)
    with source.open('a') as handle:
        handle.write(json.dumps(answer, ensure_ascii=False))
    assert run(config, source)['calls'] == 0
    with source.open('a') as handle:
        handle.write('\n')
    assert run(config, source)['calls'] == 1
    assert fake.payloads[0]['turns'][0]['assistant'] == '完整的最终结论'


def test_old_midturn_cursor_rehydrates_question_on_abort_without_new_visible_body(tmp_path, monkeypatch):
    from seywo.store import Store
    config, fake = configured(tmp_path, monkeypatch)
    rows = native_turn(tmp_path)
    run(config, write_source(tmp_path, rows))
    with Store(config) as store:
        last = store.db.execute('SELECT ordinal,max(record_no) FROM current_sources').fetchone()
        store.db.execute('INSERT OR REPLACE INTO delta_progress VALUES(?,?,?,?)',
                         ('live-chat', json.dumps(list(last)), 0, 1))
        store.db.commit()
    rows.append({'type': 'event_msg', 'payload': {'type': 'turn_aborted', 'turn_id': 't1'}})
    report = run(config, write_source(tmp_path, rows))
    assert report['calls'] == 1
    assert fake.payloads[0]['turns'][0]['user'] == '检查 Hook 是否就绪'
    assert run(config, tmp_path / 'live.jsonl')['calls'] == 0


def test_partial_json_after_legacy_cursor_does_not_advance_over_unfinished_record(tmp_path, monkeypatch):
    from seywo.store import Store
    config, fake = configured(tmp_path, monkeypatch)
    rows = native_turn(tmp_path)
    source = write_source(tmp_path, rows)
    run(config, source)
    with Store(config) as store:
        last = store.db.execute('SELECT ordinal,max(record_no) FROM current_sources').fetchone()
        store.db.execute('INSERT OR REPLACE INTO delta_progress VALUES(?,?,?,?)',
                         ('live-chat', json.dumps(list(last)), 0, 1))
        store.db.commit()
    answer = message('续写完成的最终结论', 'assistant')
    answer['payload']['channel'] = 'final'
    wire = json.dumps(answer, ensure_ascii=False)
    with source.open('a') as handle:
        handle.write(wire[:25])
    assert run(config, source)['calls'] == 0
    with source.open('a') as handle:
        handle.write(wire[25:] + '\n')
    assert run(config, source)['calls'] == 1
    assert fake.payloads[0]['turns'][0]['assistant'] == '续写完成的最终结论'


def test_terminal_only_after_committed_abort_does_not_repeat_user_request(tmp_path, monkeypatch):
    config, fake = configured(tmp_path, monkeypatch)
    rows = native_turn(tmp_path)
    rows.append({'type': 'event_msg', 'payload': {'type': 'turn_aborted', 'turn_id': 't1'}})
    assert run(config, write_source(tmp_path, rows))['calls'] == 1
    rows.append({'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 't1'}})
    assert run(config, write_source(tmp_path, rows))['calls'] == 0
    assert fake.calls == 1


def test_lifecycle_projection_upgrade_reuses_unchanged_body_payloads(tmp_path, monkeypatch):
    from seywo import store as storage
    from seywo.store import Store
    config, _ = configured(tmp_path, monkeypatch)
    rows = native_turn(tmp_path)
    rows.append(message('保留既有来源正文', 'assistant'))
    source = write_source(tmp_path, rows)
    current_project = storage.project
    def old_project(record):
        p = current_project(record)
        p.pop('turn_event', None)
        p.pop('event_turn_id', None)
        p['projection_version'] = 'codex-safe-v4'
        return p
    with monkeypatch.context() as old:
        old.setattr(storage, 'VERSION', 'codex-safe-v4')
        old.setattr(storage, 'project', old_project)
        with Store(config) as store:
            store.import_source(source, 'live-chat', 0)
            body_payloads = {r['event_id']: r['payload_id'] for r in store.db.execute(
                "SELECT event_id,payload_id FROM current_sources WHERE kind='message'")}
    with Store(config) as store:
        store.import_source(source, 'live-chat', 0)
        after = {r['event_id']: r['payload_id'] for r in store.db.execute(
            "SELECT event_id,payload_id FROM current_sources WHERE kind='message'")}
        assert after == body_payloads


def test_stop_resumes_open_compact_turn_once_without_reimporting_old_sources(tmp_path, monkeypatch):
    import io
    from seywo import hook, maintenance_status
    config, transport = configured(tmp_path, monkeypatch)
    rows = native_turn(tmp_path)
    source = write_source(tmp_path, rows)
    inbox.record(config, 'live-chat', 'PreCompact', str(source))
    maintain.run_chat(config, 'live-chat')
    assert transport.calls == 0
    rows.extend([message('Hook 已就绪', 'assistant'),
                 {'type':'event_msg','payload':{'type':'task_complete','turn_id':'t1'}}])
    write_source(tmp_path, rows)
    spawned = []
    monkeypatch.setattr(hook, 'spawn', lambda *args: spawned.append(args) or True)
    payload = dict(hook_event_name='Stop', session_id='live-chat', cwd=str(tmp_path), transcript_path=str(source))
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(payload)))
    assert hook.main() == 0
    assert len(spawned) == 1
    result = maintain.run_chat(config, 'live-chat')
    assert result['state'] == 'done' and transport.calls == 1
    assert maintenance_status.read(config, 'live-chat')['state'] == 'done'
    records = [json.loads(p.read_text()) for p in (inbox.root(config)/'pending').glob('*.json')]
    assert all(r['state'] == 'done' for r in records)
    with Store(config, read_only=True) as store:
        versions = store.db.execute('SELECT count(*) FROM compact_batches').fetchone()[0]
        sources = store.db.execute('SELECT count(*) FROM source_snapshots').fetchone()[0]
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(payload)))
    assert hook.main() == 0
    assert len(spawned) == 1
    maintain.run_chat(config, 'live-chat')
    assert transport.calls == 1
    with Store(config, read_only=True) as store:
        assert store.db.execute('SELECT count(*) FROM compact_batches').fetchone()[0] == versions
        assert store.db.execute('SELECT count(*) FROM source_snapshots').fetchone()[0] == sources


def test_retry_waiting_turn_captures_completion_instead_of_replaying_frozen_snapshot(tmp_path, monkeypatch):
    from seywo import maintenance_control, hook, maintenance_status
    config, transport = configured(tmp_path, monkeypatch)
    rows = native_turn(tmp_path)
    source = write_source(tmp_path, rows)
    inbox.record(config, 'live-chat', 'PreCompact', str(source))
    maintain.run_chat(config, 'live-chat')
    rows.extend([message('完成检查', 'assistant'),
                 {'type':'event_msg','payload':{'type':'task_complete','turn_id':'t1'}}])
    write_source(tmp_path, rows)
    monkeypatch.setattr(hook, 'spawn', lambda *args: True)
    assert maintenance_control.retry(config, 'live-chat')['accepted']
    assert maintain.run_chat(config, 'live-chat')['state'] == 'done'
    assert transport.calls == 1
    assert maintenance_status.read(config, 'live-chat')['state'] == 'done'
