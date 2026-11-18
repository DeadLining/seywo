"""Live Compact structure repair and semantic boundary acknowledgement."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from seywo import compact_model, inbox, maintain, maintenance_control, maintenance_status
from seywo.store import Store
from test_compact_lossless_maintain import setup
from test_compact_maintain import make_source


def change(title, indexes, *, kind='goal', attribution='user'):
    return dict(operation='new', title=title, type=kind, attribution=attribution,
                turn_indexes=indexes, target_index=None, open=True, merge_indexes=[])


def phases(payload):
    start = max((i['index'] for i in payload['active_items']), default=-1) + 1
    indexes = [t['index'] for t in payload['turns']]
    return dict(segments=[dict(title='整理主线', action='continue', turn_indexes=[indexes[0]], anchor_index=None),
                          dict(title='切换发布目标', action='new_phase', turn_indexes=indexes[1:], anchor_index=start + 1)],
                changes=[change('整理当前对话', [indexes[0]]), change('发布项目插件', indexes[1:])],
                current=dict(goal_index=start + 1, priority_indexes=[]))


class PhaseTransport:
    model = 'phase-fixture'

    def __init__(self, bad='old', recover=True):
        self.bad, self.recover = bad, recover
        self.requests = []

    def complete_compact(self, **request):
        self.requests.append(deepcopy(request))
        output = phases(request['payload'])
        if len(self.requests) == 1 or not self.recover:
            segment = output['segments'][1]
            if self.bad == 'old':
                segment['anchor_index'] = 0
            elif self.bad == 'missing':
                del segment['anchor_index']
            elif self.bad == 'type':
                output['changes'][1]['type'] = 'next_step'
            elif self.bad == 'assistant':
                output['changes'][1]['attribution'] = 'assistant'
            elif self.bad == 'outside':
                output['changes'][1]['turn_indexes'] = [0]
        return dict(output=output, error=None, usage=dict(input_tokens=100, output_tokens=50))


def counts(config):
    with Store(config, read_only=True) as store:
        return {name: store.db.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                for name in ['source_segments', 'source_snapshots', 'source_fragments',
                             'narrative_versions', 'compact_turn_commits']}


@pytest.mark.parametrize('bad,reason', [('old', 'anchor_not_in_segment'), ('missing', 'anchor_required'),
                                     ('type', 'anchor_not_user_objective'), ('assistant', 'anchor_not_user_objective'),
                                     ('outside', 'anchor_not_in_segment')])
def test_invalid_phase_is_repaired_with_specific_feedback_before_publication(tmp_path, monkeypatch, bad, reason):
    transport = PhaseTransport(bad)
    config = setup(tmp_path, monkeypatch, transport)
    boundary = inbox.record(config, 'repair', 'PreCompact', str(make_source(tmp_path, 'repair')))
    report = maintain.run_chat(config, 'repair')
    assert report['state'] == 'done', report
    assert report['calls'] == 2
    assert len(transport.requests) == 2
    assert reason in transport.requests[1]['instructions']
    assert 'compact_new_phase_requires_user_objective' in transport.requests[1]['instructions']
    assert transport.requests[0]['payload'] == transport.requests[1]['payload']
    with Store(config, read_only=True) as store:
        row = store.db.execute('SELECT * FROM compact_batches').fetchone()
        assert row['state'] == 'done' and row['model_calls'] == 2
        assert row['input_tokens'] == 200 and row['output_tokens'] == 100
        body = json.loads(store.db.execute('SELECT body_json FROM narrative_versions').fetchone()[0])
        assert len(body['phases']) == 2
        assert [t['kind'] for t in body['transitions']] == ['goal_change']
    assert inbox._read(Path(boundary['_path']))['state'] == 'done'


def test_exhausted_repair_is_failed_retryable_and_does_not_reimport_or_republish(tmp_path, monkeypatch):
    transport = PhaseTransport('missing', recover=False)
    config = setup(tmp_path, monkeypatch, transport)
    boundary = inbox.record(config, 'repair', 'PreCompact', str(make_source(tmp_path, 'repair')))
    failed = maintain.run_chat(config, 'repair')
    assert failed['state'] == 'failed', failed
    assert failed['calls'] == 2
    assert failed['error_code'] == 'compact_new_phase_requires_user_objective'
    status = maintenance_status.read(config, 'repair')
    assert status['state'] == 'failed' and status['retryable']
    assert '阶段' in status['message']
    saved = inbox._read(Path(boundary['_path']))
    assert saved['state'] == 'failed' and saved.get('imported_cursor')
    before = counts(config)
    assert before['narrative_versions'] == before['compact_turn_commits'] == 0
    with Store(config, read_only=True) as store:
        receipt = store.db.execute('SELECT * FROM compact_batches').fetchone()
        assert receipt['published_version'] is None and receipt['state'] == 'failed'
        assert store.db.execute('SELECT count(*) FROM delta_progress').fetchone()[0] == 0
    transport.recover = True
    # Neither a source import nor a new snapshot is necessary for model repair.
    monkeypatch.setattr(Store, '_import_compact_capture', lambda *a: pytest.fail('duplicate import'))
    retried = maintain.run_chat(config, 'repair')
    assert retried['state'] == 'done', retried
    after = counts(config)
    for table in ['source_segments', 'source_snapshots', 'source_fragments']:
        assert after[table] == before[table]
    assert after['narrative_versions'] == 1
    assert maintain.run_chat(config, 'repair')['calls'] == 0
    assert counts(config) == after
    assert inbox._read(Path(boundary['_path']))['state'] == 'done'


def test_old_persisted_objective_cannot_anchor_new_phase(tmp_path, monkeypatch):
    from test_compact_maintain import CompactTransport
    transport = CompactTransport()
    config = setup(tmp_path, monkeypatch, transport)
    path = make_source(tmp_path, 'repair')
    inbox.record(config, 'repair', 'PreCompact', str(path))
    assert maintain.run_chat(config, 'repair')['state'] == 'done'
    from test_s2 import message
    with path.open('a') as stream:
        for index in range(2):
            stream.write(json.dumps(dict(type='turn_context', payload=dict(turn_id=f'new-{index}'))) + '\n')
            stream.write(json.dumps(message('开始发布另一个产品')) + '\n')
            stream.write(json.dumps(message('记录新的发布目标', 'assistant')) + '\n')
    transport = PhaseTransport('old')
    monkeypatch.setattr(maintain, 'Transport', lambda *a, **kw: transport)
    inbox.record(config, 'repair', 'PreCompact', str(path))
    report = maintain.run_chat(config, 'repair')
    assert report['state'] == 'done' and report['calls'] == 2, report
    assert 'anchor_not_new' in transport.requests[1]['instructions']


def test_failed_ledger_overrides_stale_done_receipt_and_retry_schedules(tmp_path, monkeypatch):
    transport = PhaseTransport('missing', recover=False)
    config = setup(tmp_path, monkeypatch, transport)
    boundary = inbox.record(config, 'repair', 'PreCompact', str(make_source(tmp_path, 'repair')))
    maintain.run_chat(config, 'repair')
    # Reproduce old releases acknowledging the source before semantic failure.
    inbox.finish(config, boundary, state='done')
    maintenance_status.record(config, 'repair', 'done')
    with Store(config, read_only=True) as store:
        status = maintenance_status.read(config, 'repair', db=store.db)
    assert status['state'] == 'failed' and status['retryable']
    assert '阶段' in status['message']
    scheduled = []
    monkeypatch.setattr('seywo.hook.spawn', lambda path, chat: scheduled.append(chat) or True)
    assert maintenance_control.retry(config, 'repair')['accepted']
    assert scheduled == ['repair']
    transport.recover = True
    monkeypatch.setattr(Store, '_import_compact_capture', lambda *a: pytest.fail('legacy retry must reuse sources'))
    assert maintain.run_chat(config, 'repair')['state'] == 'done'
    with Store(config, read_only=True) as store:
        assert maintenance_status.read(config, 'repair', db=store.db)['state'] == 'done'
        assert store.db.execute('SELECT count(*) FROM narrative_versions').fetchone()[0] == 1


def test_live_schema_and_prompt_make_new_phase_anchor_obligatory():
    payload = dict(active_items=[dict(index=4)], turns=[dict(index=0)])
    fields = compact_model.schema_for(payload)['properties']['segments']['items']
    assert 'anchor_index' in fields['required']
    assert 'new_phase' in fields['properties']['anchor_index']['description']
    assert '必须' in compact_model.instructions().split('new_phase')[-1] or '当前 segment' in compact_model.instructions()


def test_boundaries_are_not_done_while_model_runs(tmp_path, monkeypatch):
    transport = PhaseTransport('missing')
    config = setup(tmp_path, monkeypatch, transport)
    boundary = inbox.record(config, 'repair', 'PreCompact', str(make_source(tmp_path, 'repair')))
    complete = transport.complete_compact
    def observe(**request):
        saved = inbox._read(Path(boundary['_path']))
        assert saved['state'] == 'claimed'
        assert saved['imported_snapshot_id'] and saved['imported_cursor']
        with Store(config, read_only=True) as store:
            assert store.db.execute('SELECT count(*) FROM narrative_versions').fetchone()[0] == 0
        return complete(**request)
    monkeypatch.setattr(transport, 'complete_compact', observe)
    assert maintain.run_chat(config, 'repair')['state'] == 'done'


def test_commit_then_ack_interruption_retries_without_model_or_duplicate_version(tmp_path, monkeypatch):
    transport = PhaseTransport()
    config = setup(tmp_path, monkeypatch, transport)
    inbox.record(config, 'repair', 'PreCompact', str(make_source(tmp_path, 'repair')))
    settle = inbox.settle_imported
    def interrupt(*a, **kw):
        raise RuntimeError('process interrupted after DB commit')
    monkeypatch.setattr(inbox, 'settle_imported', interrupt)
    with pytest.raises(RuntimeError):
        maintain.run_chat(config, 'repair')
    before = counts(config)
    assert before['narrative_versions'] == 1
    monkeypatch.setattr(inbox, 'settle_imported', settle)
    monkeypatch.setattr(Store, '_import_compact_capture', lambda *a: pytest.fail('duplicate import'))
    report = maintain.run_chat(config, 'repair')
    assert report['state'] == 'done' and report['calls'] == 0
    assert counts(config) == before


def test_open_trailing_turn_is_not_acknowledged_by_prefix_publication(tmp_path, monkeypatch):
    from test_compact_boundaries import configured, native_turn, write_source
    from test_s2 import message
    config, _ = configured(tmp_path, monkeypatch)
    rows = native_turn(tmp_path)
    rows.extend([message('Hook 已确认', 'assistant'),
                 dict(type='event_msg', payload=dict(type='task_complete', turn_id='t1')),
                 dict(type='event_msg', payload=dict(type='task_started', turn_id='t2')),
                 dict(type='turn_context', payload=dict(turn_id='t2')), message('发布另一个目标')])
    boundary = inbox.record(config, 'live-chat', 'PreCompact', str(write_source(tmp_path, rows)))
    assert maintain.run_chat(config, 'live-chat')['calls'] == 1
    assert inbox._read(Path(boundary['_path']))['state'] == 'claimed'
    rows.extend([message('记录发布目标', 'assistant'),
                 dict(type='event_msg', payload=dict(type='task_complete', turn_id='t2'))])
    inbox.record(config, 'live-chat', 'PreCompact', str(write_source(tmp_path, rows)))
    assert maintain.run_chat(config, 'live-chat')['state'] == 'done'
    assert inbox._read(Path(boundary['_path']))['state'] == 'done'


def test_model_confirmed_noop_acknowledges_without_narrative_version(tmp_path, monkeypatch):
    transport = PhaseTransport()
    config = setup(tmp_path, monkeypatch, transport)
    def noop(**request):
        return dict(output=dict(segments=[dict(title='对话回执', action='continue', turn_indexes=[0, 1], anchor_index=None)],
                                changes=[], current=dict(goal_index=None, priority_indexes=[])), error=None)
    monkeypatch.setattr(transport, 'complete_compact', noop)
    boundary = inbox.record(config, 'repair', 'PreCompact', str(make_source(tmp_path, 'repair')))
    report = maintain.run_chat(config, 'repair')
    assert report['state'] == 'done' and report['calls'] == 1
    assert counts(config)['narrative_versions'] == 0
    assert inbox._read(Path(boundary['_path']))['state'] == 'done'


def test_empty_panel_exposes_failed_batch_and_schedules_retry_without_model(tmp_path, monkeypatch):
    from seywo.plugin import create_plugin_server
    from test_maintenance_app import call
    transport = PhaseTransport('missing', recover=False)
    config = setup(tmp_path, monkeypatch, transport)
    inbox.record(config, 'repair', 'PreCompact', str(make_source(tmp_path, 'repair')))
    assert maintain.run_chat(config, 'repair')['state'] == 'failed'
    server = create_plugin_server(tmp_path)
    monkeypatch.setattr(transport, 'complete_compact', lambda **kw: pytest.fail('status must not call model'))
    assert call(server, 'seywo_panel_data', view='mainline', chat_id='repair')['version'] == 0
    status = call(server, 'seywo_panel_data', view='maintenance', chat_id='repair')
    assert status['state'] == 'failed' and status['retryable'] and '阶段' in status['message']
    launches = []
    monkeypatch.setattr('seywo.hook.spawn', lambda path, chat: launches.append(chat) or True)
    assert call(server, 'seywo_retry_maintenance', chat_id='repair')['accepted']
    assert launches == ['repair']


def test_eleven_turn_five_topic_repair_publishes_exactly_one_complete_version(tmp_path, monkeypatch):
    from test_s2 import message
    transport = PhaseTransport()
    config = setup(tmp_path, monkeypatch, transport)
    path = tmp_path / 'topics.jsonl'
    rows = [dict(type='session_meta', payload=dict(id='topics', cwd=str(tmp_path)))]
    for index in range(11):
        rows.extend([dict(type='turn_context', payload=dict(turn_id=f'turn-{index}')),
                     message(f'目标 {index // 2} 的用户要求'), message('记录当前要求', 'assistant')])
    path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))
    attempts = []
    def complete(**request):
        attempts.append(request)
        groups = [[0, 1], [2, 3], [4, 5], [6, 7], [8, 9, 10]]
        output = dict(segments=[dict(title=f'用户目标{i}', action='continue' if i == 0 else 'new_phase',
                                    turn_indexes=group, anchor_index=None if i == 0 else i)
                               for i, group in enumerate(groups)],
                      changes=[change(f'用户目标{i}', group) for i, group in enumerate(groups)],
                      current=dict(goal_index=4, priority_indexes=[]))
        if len(attempts) == 1:
            output['segments'][4]['anchor_index'] = 1
        return dict(output=output, error=None)
    monkeypatch.setattr(transport, 'complete_compact', complete)
    inbox.record(config, 'topics', 'PreCompact', str(path))
    report = maintain.run_chat(config, 'topics')
    assert report['state'] == 'done' and report['calls'] == 2
    with Store(config, read_only=True) as store:
        versions = store.db.execute('SELECT body_json FROM narrative_versions').fetchall()
        assert len(versions) == 1
        body = json.loads(versions[0][0])
        assert len(body['phases']) == len(body['items']) == 5
        assert len(body['transitions']) == 4
        assert store.db.execute('SELECT count(*) FROM compact_turn_commits').fetchone()[0] == 11


def test_imported_failure_retry_budget_does_not_block_a_later_success(tmp_path, monkeypatch):
    transport = PhaseTransport('missing', recover=False)
    config = setup(tmp_path, monkeypatch, transport)
    boundary = inbox.record(config, 'repair', 'PreCompact', str(make_source(tmp_path, 'repair')))
    assert maintain.run_chat(config, 'repair')['state'] == 'failed'
    path = Path(boundary['_path'])
    item = inbox._read(path)
    item['attempts'] = 99
    inbox._write_atomic(path, item)
    transport.recover = True
    assert maintain.run_chat(config, 'repair')['state'] == 'done'
    assert inbox._read(path)['state'] == 'done'


def test_phase_anchor_requires_user_evidence_inside_its_segment():
    from seywo.compact_pipeline import apply_compact
    from seywo.narrative import empty
    from test_compact_pipeline import source, turn
    turns = [turn('first', '建立初始目标', '确认初始目标', source(0, 'user', '建立初始目标')),
             turn('second', '', '助手解释', source(1, 'assistant', '助手解释', turn='second'))]
    payload = dict(active_items=[], turns=[dict(index=0), dict(index=1)])
    output = phases(payload)
    output['changes'][1]['turn_indexes'] = [0, 1]
    output = compact_model.validate(output, [0, 1], set(), require_current=True)
    with pytest.raises(ValueError, match='compact_new_phase_requires_user_objective') as error:
        apply_compact(empty(), output, turns, [], 'assistant-only-phase')
    assert error.value.details['reason'] == 'anchor_user_evidence_missing_in_segment'
