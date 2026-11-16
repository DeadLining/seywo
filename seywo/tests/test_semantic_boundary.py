"""Regression guards for observed semantic errors at the program/model boundary."""
import json

import pytest

from test_s2 import setup as setup, message
from test_s3 import layout, publish, amend
from seywo.model_contract import item_wire
from seywo.narrative import Narrative
from seywo.store import digest, encode


@pytest.mark.parametrize('unresolved', [['账单问题未解决'], [True], [0.0], [20]])
def test_unanchored_concern_cannot_advance_coverage(setup, unresolved):
    store, engine, options, path, write = setup
    write([message('现在回到账单主线，重试调查挂起，问题没有解决。')])
    store.import_source(path, 'chat', 0)
    job = engine.prepare('chat', options)['job_id']
    claim = engine.claim(job, backend=options.backend)
    status = engine.submit(claim['attempt_id'], backend=options.backend, output={
        'items': [{'type': 'observation', 'title': '重试调查尚未解决', 'source_indexes': [0]}],
        'unresolved': unresolved}, usage={'input_tokens': 75, 'output_tokens': 36})
    assert status['error_code'] == 'invalid_model_output'
    assert status['usage']['input_tokens'] == 75
    assert store.db.execute("SELECT count(*) FROM semantic_ranges WHERE state='semantic_done'").fetchone()[0] == 0


def test_item_backed_failure_survives_organizer_without_retyping(setup):
    store, engine, options, path, write = setup
    text = '现在回到账单主线，重试调查挂起，问题没有解决。'
    write([message(text)])
    store.import_source(path, 'chat', 0)
    job = engine.prepare('chat', options)['job_id']
    claim = engine.claim(job, backend=options.backend)
    status = engine.submit(claim['attempt_id'], backend=options.backend, output={
        'items': [{'type': 'observation', 'title': '重试调查尚未解决', 'source_indexes': [0]}], 'unresolved': [0]})
    assert status['result']['unresolved'] == ['重试调查尚未解决']
    service = Narrative(store)
    task = service.prepare(job)
    b = engine.claim(job, backend=options.backend)
    # No free text concern from B; the A reference survives an otherwise valid layout.
    engine.submit(b['attempt_id'], backend=options.backend, output=layout(b['request']))
    view = service.publish(task['task_id'])
    item = view['narrative']['items'][0]
    assert item['evidence'][0]['quote'] == text
    assert view['narrative']['overview']['unresolved'] == [item['title']]
    assert '账单问题未解决' not in view['narrative']['overview']['unresolved']


@pytest.mark.parametrize('kind,allowed', [('observation', False), ('correction', False), ('decision', False),
                                         ('goal', True), ('option', False), ('question', False),
                                         ('action', True), ('next_step', True)])
def test_result_cannot_be_published_as_next_action(setup, kind, allowed):
    store, engine, options, path, write = setup
    write([message('复核检查结果，并核对后续步骤。')])
    store.import_source(path, 'chat', 0)
    job = engine.prepare('chat', options)['job_id']
    claim = engine.claim(job, backend=options.backend)
    engine.submit(claim['attempt_id'], backend=options.backend, output={
        'items': [{'type': kind, 'title': '核对检查结果', 'source_indexes': [0]}]})
    service = Narrative(store)
    task = service.prepare(job)
    claim = engine.claim(job, backend=options.backend)
    out = layout(claim['request'])
    out['overview']['next_step_indexes'] = [0]
    status = engine.submit(claim['attempt_id'], backend=options.backend, output=out)
    assert (status['error_code'] is None) == allowed
    if allowed:
        assert service.publish(task['task_id'])['narrative']['overview']['next_step_ids']
    else:
        with pytest.raises(ValueError, match='narrative_not_published'):
            service.read('chat')


def test_wire_compression_is_exact_and_preserves_unavailable_or_interpreted_text(setup):
    store, engine, job, service, task, claim, view = publish(setup)
    item = view['narrative']['items'][0]
    e = item['evidence'][0]
    unit = {**e, 'text': e['quote']}
    assert item_wire(item, 0, [unit], {})['text'] == ''
    assert item_wire(item, 0, [], {})['text'] == item['content']
    custom = {**item, 'content': '人工明确限定：等待复核，保留原文以供核对。'}
    assert item_wire(custom, 0, [unit], {})['text'] == custom['content']
    assert service.read('chat')['narrative']['items'][0]['content'] == item['content']


def test_keep_is_not_a_proposal_and_material_proposal_stays_reviewable(setup):
    store, engine, job, service, task, claim, first = publish(setup)
    item = first['narrative']['items'][0]
    manual = amend(service, first, 'item', item['id'], {'content': '人工限定当前范围'})
    write = setup[4]
    write([message('新增核对范围'), message('结果仍需核验', 'assistant')])
    store.import_source(setup[3], 'chat', 0)
    job = engine.prepare('chat', setup[2])['job_id']
    from test_s2 import drain
    drain(engine, job)
    service.prepare(job)
    claim = engine.claim(job, backend='fixture-A')
    out = layout(claim['request'])
    out['bindings'][1]['action'] = 'propose'
    assert engine.submit(claim['attempt_id'], backend='fixture-A', output=out)['error_code'] is None
    target = store.db.execute("SELECT id FROM understanding_tasks WHERE job_id=? AND phase='organize'", (job,)).fetchone()[0]
    view = service.publish(target)
    assert view['narrative']['items'][0]['content'] == manual['narrative']['items'][0]['content']
    assert len(view['narrative']['pending_revisions']) == 1
    assert view['narrative']['pending_revisions'][0]['item_id'] != item['id']


def test_index2_policy_upgrade_cannot_reset_spend_or_use_old_lease(setup):
    store, engine, options, *_ = setup
    job = engine.prepare('chat', options)['job_id']
    claim = engine.claim(job, backend=options.backend)
    heads = engine._heads('chat')
    prior_target = digest(encode({'chat': 'chat', 'heads': heads, 'base': 0, 'override': 0,
                                  'model': options.model, 'model_policy': 'seywo-index-2'}))
    store.db.execute('UPDATE understanding_jobs SET target_hash=? WHERE id=?', (prior_target, job))
    store.db.commit()
    before = engine.status(job)['usage']
    current = engine.prepare('chat', options)['job_id']
    assert current != job and engine.status(job)['state'] == 'retired_policy'
    assert engine.status(current)['usage'] == before
    state = store.db.execute('SELECT state,error_code FROM understanding_attempts WHERE id=?', (claim['attempt_id'],)).fetchone()
    assert tuple(state) == ('superseded', 'model_policy_retired')
    engine.submit(claim['attempt_id'], backend=options.backend, output={'items': []})
    state = store.db.execute('SELECT state,error_code FROM understanding_attempts WHERE id=?', (claim['attempt_id'],)).fetchone()
    assert tuple(state) == ('late', 'late_response')
    assert engine.status(current)['usage'] == before
    private = json.loads(store.db.execute('SELECT request_json FROM understanding_tasks WHERE id=?', (claim['task_id'],)).fetchone()[0])
    assert private['indexed']['policy'] == 'seywo-index-5'


def test_explicit_identity_merge_updates_concern_label_without_false_resolution(setup):
    store, engine, options, path, write = setup
    write([message('先前校验报告失败，订单表仍不一致。', 'assistant'),
           message('同一订单校验失败仍存在。', 'assistant')])
    store.import_source(path, 'chat', 0)
    job = engine.prepare('chat', options)['job_id']
    for title in ['订单校验未通过', '订单校验失败待排查']:
        claim = engine.claim(job, backend=options.backend)
        status = engine.submit(claim['attempt_id'], backend=options.backend, output={
            'items': [{'type': 'observation', 'title': title,
                       'source_indexes': claim['request']['primary_source_indexes']}], 'unresolved': [0]})
        assert status['error_code'] is None
    claim = engine.claim(job, backend=options.backend)
    status = engine.submit(claim['attempt_id'], backend=options.backend, output={
        'edits': [{'op': 'merge', 'item_indexes': [0, 1], 'into_index': 0,
                   'reason': '同一订单校验的重复失败说法'}]})
    assert status['error_code'] is None
    assert status['result']['unresolved'] == ['订单校验未通过']
    assert status['result']['resolutions'] == []
    assert len(status['result']['candidate_items']) == 1


def test_organizer_rejects_free_prose_concern_and_same_evidence_can_be_indexed(setup):
    store, engine, job, service, task, claim, view = publish(setup)
    service.prepare(job)
    claim = engine.claim(job, backend='fixture-A')
    from seywo.model_contract import hydrate
    private = json.loads(store.db.execute('SELECT request_json FROM understanding_tasks WHERE id=?', (claim['task_id'],)).fetchone()[0])
    with pytest.raises(ValueError):
        hydrate({'overview': {'unresolved_add': ['凭空生成另一个任务的失败']}}, private)
    out = hydrate({'overview': {'unresolved_add': [0]}}, private)
    assert out['overview']['unresolved'] == [view['narrative']['items'][0]['title']]


def test_keep_alias_cannot_smuggle_observation_into_next_steps(setup):
    store, engine, job, service, task, claim, first = publish(setup)
    setup[4]([message('新的核对行动'), message('补充结果', 'assistant')])
    store.import_source(setup[3], 'chat', 0)
    job = engine.prepare('chat', setup[2])['job_id']
    from test_s2 import drain
    drain(engine, job)
    service.prepare(job)
    claim = engine.claim(job, backend='fixture-A')
    incoming = [i for i in claim['request']['items'] if i['origin'] == 'incoming'][0]['index']
    from seywo.model_contract import hydrate
    private = json.loads(store.db.execute('SELECT request_json FROM understanding_tasks WHERE id=?', (claim['task_id'],)).fetchone()[0])
    out = {'bindings': [{'item_index': incoming, 'target_index': 0, 'action': 'keep', 'reason': 'same fixture'}],
           'overview': {'next_step_indexes': [incoming]}}
    with pytest.raises(ValueError, match='next_step_requires_action'):
        hydrate(out, private)


@pytest.mark.parametrize('phase_index,accepted', [(None, True), (0, True), (1, False)])
@pytest.mark.parametrize('branch_goal', ['查明重试原因', 0])
def test_branch_parent_is_completed_but_contradiction_rejected(setup, phase_index, accepted, branch_goal):
    accepted = accepted and isinstance(branch_goal, str)
    store, engine, options, path, write = setup
    write([message('主线核对账单，现在进入重试调查支线。')])
    store.import_source(path, 'chat', 0)
    job = engine.prepare('chat', options)['job_id']
    claim = engine.claim(job, backend=options.backend)
    engine.submit(claim['attempt_id'], backend=options.backend, output={
        'items': [{'type': 'action', 'title': '进入重试调查', 'source_indexes': [0]}]})
    service = Narrative(store)
    task = service.prepare(job)
    claim = engine.claim(job, backend=options.backend)
    out = {
        'phases': [{'title': '账单主线', 'goal_item_indexes': [0]},
                   {'title': '无关阶段', 'goal_item_indexes': []}],
        'branches': [{'phase_index': 0, 'title': '重试调查', 'goal': branch_goal,
                      'resolve_when': '原因已明确', 'state': 'active',
                      'evidence_item_indexes': [0]}],
        'placements': [{'item_index': 0, 'phase_index': phase_index, 'branch_index': 0}],
        'transitions': [{'kind': 'enter', 'from_phase_index': 0, 'from_branch_index': None,
                         'to_phase_index': phase_index, 'to_branch_index': 0,
                         'reason': '明确进入调查', 'evidence_source_indexes': [0]}],
        'overview': {'focus': {'phase_index': phase_index, 'branch_index': 0}}}
    status = engine.submit(claim['attempt_id'], backend=options.backend, output=out)
    assert (status['error_code'] is None) == accepted
    if accepted:
        doc = service.publish(task['task_id'])['narrative']
        parent = doc['branches'][0]['phase_id']
        assert doc['placements'][0]['phase_id'] == parent
        assert doc['overview']['focus']['phase_id'] == parent
        assert doc['transitions'][0]['to_phase_id'] == parent
    else:
        with pytest.raises(ValueError, match='narrative_not_published'):
            service.read('chat')


@pytest.mark.parametrize('gap', [False, True])
def test_source_copy_spanning_wire_units_is_only_removed_with_complete_coverage(setup, gap):
    store, engine, job, service, task, claim, view = publish(setup)
    item = view['narrative']['items'][0]
    e = item['evidence'][0]
    mid = e['start'] + len(e['quote']) // 2
    sources = [
        {**e, 'end': mid, 'text': e['quote'][:mid-e['start']]},
        {**e, 'start': mid + int(gap), 'text': e['quote'][mid-e['start']+int(gap):]}]
    wire = item_wire(item, 0, sources, {})
    assert wire['text'] == (item['content'] if gap else '')
    assert wire['evidence'][0]['source_indexes'] == [0, 1]
    assert service.read('chat')['narrative']['items'][0]['content'] == item['content']


@pytest.mark.parametrize('different_snapshot', [False, True])
def test_redundant_readback_prefix_only_deduplicates_identical_provenance(different_snapshot):
    from seywo.model_contract import source_map
    source = {'event_id': 'event', 'snapshot_id': 'snapshot', 'role': 'user',
              'kind': 'message', 'purpose': 'evidence_readback', 'start': 0,
              'end': 9, 'text': '这轮只核对迁移预览'}
    prefix = {**source, 'end': 4, 'text': source['text'][:4]}
    if different_snapshot:
        prefix['snapshot_id'] = 'other-snapshot'
    mapped = source_map({'sources': [source, prefix]})
    assert len(mapped) == (2 if different_snapshot else 1)
    assert mapped[0]['text'] == source['text']


def test_content_equal_to_one_support_quote_is_not_sent_again(setup):
    store, engine, job, service, task, claim, view = publish(setup)
    item = view['narrative']['items'][0]
    e = item['evidence'][0]
    other = {**e, 'event_id': 'different-user-confirmation', 'start': 0, 'end': 6, 'quote': '补充核对条件'}
    candidate = {**item, 'evidence': item['evidence'] + [other]}
    sources = [{**e, 'text': e['quote']}, {**other, 'text': other['quote']}]
    wire = item_wire(candidate, 0, sources, {})
    assert wire['text'] == ''
    assert wire['evidence'][0]['source_indexes'] == [0, 1]
    assert candidate['content'] == item['content']


def test_retry_feedback_is_metered_without_changing_frozen_maps(setup):
    from seywo.model_contract import responses_body
    from seywo.provider import measure_request
    store, engine, options, *_ = setup
    job = engine.prepare('chat', options)['job_id']
    claim = engine.claim(job, backend=options.backend)
    before = store.db.execute('SELECT request_json FROM understanding_tasks WHERE id=?', (claim['task_id'],)).fetchone()[0]
    failed = engine.submit(claim['attempt_id'], backend=options.backend,
                           output={'items': [], 'locks': []}, usage={'input_tokens': 50, 'output_tokens': 20})
    assert failed['error_code'] == 'invalid_model_output'
    again = engine.claim(job, backend=options.backend)
    assert again['task_id'] == claim['task_id']
    assert again['request']['retry_feedback'] == 'invalid_indexed_fields'
    assert 'Previous output was rejected' in responses_body(again['request'])['instructions']
    assert again['reserved_input'] == measure_request(responses_body(again['request']))['tokens']
    assert store.db.execute('SELECT request_json FROM understanding_tasks WHERE id=?', (claim['task_id'],)).fetchone()[0] == before
    assert engine.status(job)['usage']['input_tokens'] == 50 + again['reserved_input']
    engine.submit(again['attempt_id'], backend=options.backend, output={'items': []})
    assert engine.status(job)['usage']['call_count'] == 2


def test_retry_feedback_cannot_copy_arbitrary_exception_or_bypass_budget(setup):
    from dataclasses import replace
    from seywo.understanding import Limits
    store, engine, options, *_ = setup
    options = replace(options, context_chars=0)
    job = engine.prepare('chat', options)['job_id']
    claim = engine.claim(job, backend=options.backend)
    engine.submit(claim['attempt_id'], backend=options.backend, output={'items': [], 'secret-value': 'do not echo'},
                  usage={'input_tokens': 50, 'output_tokens': 20})
    # The complete retry envelope, including the fixed hint, must still fit.
    frozen = json.loads(store.db.execute('SELECT request_json FROM understanding_tasks WHERE id=?', (claim['task_id'],)).fetchone()[0])
    from seywo.model_contract import measured_payload
    from seywo.provider import measure_request
    needed = measure_request(measured_payload(frozen, 'invalid_indexed_fields'))['tokens']
    from dataclasses import asdict
    limits = Limits(total_input_tokens=50 + needed - 1)
    store.db.execute('UPDATE understanding_jobs SET limits_json=? WHERE id=?', (json.dumps(asdict(limits)), job))
    store.db.commit()
    assert engine.claim(job, backend=options.backend)['state'] == 'waiting_budget'
    assert engine.status(job)['usage']['call_count'] == 1
    from seywo.model_contract import retry_request
    wire = retry_request(frozen, 'invalid_indexed_fields')
    assert 'secret-value' not in json.dumps(wire)
    with pytest.raises(ValueError, match='invalid_retry_feedback'):
        retry_request(frozen, 'secret-value')


def test_initial_enter_stays_rejected_and_feedback_allows_explicit_focus_correction(setup):
    from test_s3 import organize
    store, engine, job, service, task, claim = organize(setup)
    output = layout(claim['request'])
    output['transitions'] = [{'kind': 'enter', 'from_phase_index': None, 'from_branch_index': None,
                              'to_phase_index': 0, 'to_branch_index': None,
                              'evidence_source_indexes': [0], 'reason': '开始核对范围'}]
    failed = engine.submit(claim['attempt_id'], backend='fixture-A', output=output)
    assert failed['error_code'] == 'invalid_model_output'
    with pytest.raises(ValueError, match='narrative_not_published'):
        service.read('chat')
    retry = engine.claim(job, backend='fixture-A')
    assert retry['request']['retry_feedback'] == 'enter_requires_branch'
    output['transitions'][0]['kind'] = 'focus'
    assert engine.submit(retry['attempt_id'], backend='fixture-A', output=output)['error_code'] is None
    view = service.publish(task['task_id'])
    assert view['narrative']['transitions'][0]['kind'] == 'focus'
    assert view['narrative']['branches'] == []


@pytest.mark.parametrize('selected,allowed', [([0], False), ([1], True), ([0, 1], True), ([], False)])
def test_current_goal_cannot_only_be_precondition_when_work_action_exists(setup, selected, allowed):
    store, engine, options, path, write = setup
    write([message('现在只生成预览；生产迁移必须等待确认。')])
    store.import_source(path, 'chat', 0)
    job = engine.prepare('chat', options)['job_id']
    a = engine.claim(job, backend='fixture-A')
    engine.submit(a['attempt_id'], backend='fixture-A', output={'items': [
        {'type': 'decision', 'title': '生产迁移需确认', 'source_indexes': [0]},
        {'type': 'action', 'title': '仅生成迁移预览', 'source_indexes': [0]}]})
    service = Narrative(store)
    task = service.prepare(job)
    b = engine.claim(job, backend='fixture-A')
    output = layout(b['request'])
    output['overview']['goal_item_indexes'] = selected
    state = engine.submit(b['attempt_id'], backend='fixture-A', output=output)
    assert (state['error_code'] is None) == allowed
    if not allowed:
        retry = engine.claim(job, backend='fixture-A')
        assert retry['request']['retry_feedback'] == ('goal_requires_work_item' if selected else 'reading_line_incomplete')
        output['overview']['goal_item_indexes'] = [1]
        assert engine.submit(retry['attempt_id'], backend='fixture-A', output=output)['error_code'] is None
    assert service.publish(task['task_id'])['narrative']['overview']['goal_item_ids'] == (
        [service.read('chat')['narrative']['items'][i]['id'] for i in (selected if allowed else [1])])


@pytest.mark.parametrize('indexes,valid', [([0], True), ([True], False), ([0.0], False), ([99], False), ([], False)])
def test_transition_sources_never_use_item_catalog_order(setup, indexes, valid):
    from test_s3 import organize
    from seywo.model_contract import freeze, model_request, hydrate
    store, engine, job, service, task, claim = organize(setup)
    private = json.loads(store.db.execute('SELECT request_json FROM understanding_tasks WHERE id=?', (claim['task_id'],)).fetchone()[0])
    first_source = private['sources'][0]['event_id']
    private['incoming'].sort(key=lambda row: any(e['event_id'] == first_source for e in row['item']['evidence']))
    private = freeze({k: v for k, v in private.items() if k not in {'indexed', 'input_fingerprint'}})
    wire = model_request(private)
    assert 0 not in wire['items'][0]['evidence'][0]['source_indexes']
    output = layout(wire)
    output['transitions'] = [{'kind': 'focus', 'from_phase_index': None, 'from_branch_index': None,
                              'to_phase_index': 0, 'to_branch_index': None, 'reason': '初始工作',
                              'evidence_source_indexes': indexes}]
    if not valid:
        with pytest.raises(ValueError):
            hydrate(output, private)
        return
    doc = hydrate(output, private)
    expected = [row['new_id'] for row in private['incoming'] if any(e['event_id'] == first_source for e in row['item']['evidence'])]
    assert doc['transitions'][0]['evidence_item_ids'] == expected
    assert private['incoming'][0]['new_id'] not in expected


@pytest.mark.parametrize('selected,allowed', [([0, 1], False), ([1], True)])
def test_current_goals_exclude_paused_earlier_phase(setup, selected, allowed):
    from test_s3 import organize_two_objectives, goal_change
    store, engine, job, service, task, claim = organize_two_objectives(setup)
    output = layout(claim['request'])
    output['transitions'] = [goal_change()]
    output['phases'].append({'title': '核对权限报告', 'goal_item_indexes': [1]})
    output['placements'][1]['phase_index'] = 1
    output['overview'] = {'focus': {'phase_index': 1, 'branch_index': None}, 'goal_item_indexes': selected}
    state = engine.submit(claim['attempt_id'], backend='fixture-A', output=output)
    assert (state['error_code'] is None) == allowed
    if not allowed:
        retry = engine.claim(job, backend='fixture-A')
        assert retry['request']['retry_feedback'] == 'goal_outside_current_phase'


@pytest.mark.parametrize('earlier_type', ['goal', 'action', 'next_step'])
def test_current_design_decision_not_rejected_for_historical_work_or_future_step(setup, earlier_type):
    store, engine, options, path, write = setup
    write([message('先核对旧方案；现在决定从零设计。下一步定义表结构。')])
    store.import_source(path, 'chat', 0)
    job = engine.prepare('chat', options)['job_id']
    a = engine.claim(job, backend='fixture-A')
    engine.submit(a['attempt_id'], backend='fixture-A', output={'items': [
        {'type': earlier_type, 'title': '核对旧方案', 'source_indexes': [0]},
        {'type': 'decision', 'title': '从零设计系统', 'source_indexes': [0]},
        {'type': 'next_step', 'title': '定义新表结构', 'source_indexes': [0]}]})
    service = Narrative(store)
    task = service.prepare(job)
    b = engine.claim(job, backend='fixture-A')
    output = layout(b['request'])
    output['phases'] = [{'title': '核对旧方案', 'goal_item_indexes': [0]},
                        {'title': '从零设计系统', 'goal_item_indexes': [1]}]
    from test_s3 import goal_change
    output['transitions'] = [goal_change(0)]
    output['placements'] = [{'item_index': i, 'phase_index': 0 if i == 0 else 1} for i in range(3)]
    output['overview'] = {'focus': {'phase_index': 1}, 'goal_item_indexes': [1], 'next_step_indexes': [2]}
    assert engine.submit(b['attempt_id'], backend='fixture-A', output=output)['error_code'] is None
    assert service.publish(task['task_id'])['version'] == 1


def test_initial_phase_title_without_goal_does_not_publish_as_understood(setup):
    from test_s3 import organize
    store, engine, job, service, task, claim = organize(setup)
    output = {'phases': [{'title': '核对范围', 'goal_item_indexes': []}],
              'overview': {'focus': {'phase_index': 0}}}
    assert engine.submit(claim['attempt_id'], backend='fixture-A', output=output)['error_code'] == 'invalid_model_output'
    retry = engine.claim(job, backend='fixture-A')
    assert retry['request']['retry_feedback'] == 'reading_line_incomplete'
    assert engine.submit(retry['attempt_id'], backend='fixture-A', output=layout(retry['request']))['error_code'] is None
    assert service.publish(task['task_id'])['narrative']['overview']['goal_item_ids']


@pytest.mark.parametrize('kind', ['goal', 'action', 'next_step', 'decision', 'option'])
def test_unresolved_cannot_be_an_entry_action_or_other_non_issue(setup, kind):
    _, engine, options, *_ = setup
    job = engine.prepare('chat', options)['job_id']
    claim = engine.claim(job, backend='fixture-A')
    out = {'items': [{'type': kind, 'title': '进入重试调查支线', 'source_indexes': [0]}], 'unresolved': [0]}
    assert engine.submit(claim['attempt_id'], backend='fixture-A', output=out)['error_code'] == 'invalid_model_output'
    retry = engine.claim(job, backend='fixture-A')
    assert retry['request']['retry_feedback'] == 'unresolved_requires_issue'
    out['unresolved'] = []
    assert engine.submit(retry['attempt_id'], backend='fixture-A', output=out)['error_code'] is None


def test_retry_keeps_all_fixed_corrections_for_same_task_and_meters_them(setup):
    from seywo.model_contract import responses_body
    from seywo.provider import measure_request
    from seywo.understanding import Limits
    store, engine, options, *_ = setup
    job = engine.prepare('chat', options, Limits(retry_limit=3))['job_id']
    first = engine.claim(job, backend=options.backend)
    frozen_body = store.db.execute('SELECT request_json FROM understanding_tasks WHERE id=?',
                                   (first['task_id'],)).fetchone()[0]
    engine.submit(first['attempt_id'], backend=options.backend,
                  output={'items': [], 'locks': []})
    second = engine.claim(job, backend=options.backend)
    bad = {'items': [{'type': 'observation', 'title': '核对来源内容', 'source_indexes': [0],
                      'relations': [{'source_index': 0, 'relation': 'unsupported'}]}]}
    engine.submit(second['attempt_id'], backend=options.backend, output=bad)
    third = engine.claim(job, backend=options.backend)
    assert first['task_id'] == second['task_id'] == third['task_id']
    assert third['request']['retry_feedback'] == 'invalid_relation'
    assert third['request']['retry_constraints'] == ['invalid_indexed_fields']
    assert third['reserved_input'] == measure_request(responses_body(third['request']))['tokens']
    assert store.db.execute('SELECT request_json FROM understanding_tasks WHERE id=?',
                            (first['task_id'],)).fetchone()[0] == frozen_body
    assert engine.submit(third['attempt_id'], backend=options.backend,
                         output={'items': []})['error_code'] is None


@pytest.mark.parametrize('inference_only', [False, True])
def test_native_user_question_cannot_disappear_from_extraction(setup, inference_only):
    from seywo.understanding import Limits
    store, engine, options, path, write = setup
    write([{'type': 'turn_context', 'payload': {'turn_id': 'turn-new'}},
           message('为什么不直接按原生对话轮次划分？')])
    store.import_source(path, 'chat', 0)
    job = engine.prepare('chat', options, Limits(retry_limit=3))['job_id']
    first = engine.claim(job, backend=options.backend)
    items = [{'type': 'option', 'title': '原生轮次方案', 'source_indexes': [0], 'inference': True}] if inference_only else []
    rejected = engine.submit(first['attempt_id'], backend=options.backend, output={'items': items})
    assert rejected['error_code'] == 'invalid_model_output'
    assert store.db.execute("SELECT count(*) FROM semantic_ranges WHERE state='semantic_done'").fetchone()[0] == 0
    retry = engine.claim(job, backend=options.backend)
    assert retry['request']['retry_feedback'] == 'native_user_message_not_accounted'
    accepted = engine.submit(retry['attempt_id'], backend=options.backend, output={
        'items': [{'type': 'question', 'title': '为何不按原生轮次划分', 'source_indexes': [0]}]})
    assert accepted['error_code'] is None
    assert accepted['result']['candidate_items'][0]['attribution'] == 'user_statement'


def test_non_work_native_user_message_needs_explicit_stored_disposition(setup):
    store, engine, options, path, write = setup
    write([{'type': 'turn_context', 'payload': {'turn_id': 'social-turn'}}, message('晚安，谢谢。')])
    store.import_source(path, 'chat', 0)
    job = engine.prepare('chat', options)['job_id']
    claim = engine.claim(job, backend=options.backend)
    accepted = engine.submit(claim['attempt_id'], backend=options.backend,
                             output={'items': [], 'no_work_source_indexes': [0]})
    assert accepted['error_code'] is None
    assert accepted['result']['candidate_items'] == []
    assert accepted['result']['non_work_user_sources'][0]['quote'] == '晚安，谢谢。'
    stored = json.loads(store.db.execute('SELECT result_json FROM understanding_tasks WHERE id=?',
                                         (claim['task_id'],)).fetchone()[0])
    assert stored['non_work_user_sources'] == accepted['result']['non_work_user_sources']
