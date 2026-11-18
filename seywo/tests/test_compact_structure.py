"""Structural focus is independent from condensed item deltas."""
from copy import deepcopy

import pytest

from seywo.compact_pipeline import apply_compact
from seywo.narrative import empty
from test_compact_pipeline import source, turn


def packet(texts):
    return [turn(f't{i}', text, '', source(i, 'user', text, turn=f't{i}'))
            for i, text in enumerate(texts)]


def change(title, indexes, *, kind='question', operation='new', target=None, open=True):
    return dict(operation=operation, title=title, type=kind, attribution='user',
                turn_indexes=indexes, target_index=target, open=open, merge_indexes=[])


def repeated(*, stop=5, broad=False, second_update=False):
    turns = packet(['主线是账单审阅。', '进入重试调查支线，原因未知。',
                    '回到账单主线，调查挂起。', '再次进入同一重试调查。',
                    '回到账单主线，调查仍未解决。'][:stop])
    actions = ['continue', 'branch', 'return', 'branch', 'return'][:stop]
    segments = [dict(title='账单审阅' if action in {'continue', 'return'} else '重试调查',
                     action=action, turn_indexes=[i]) for i, action in enumerate(actions)]
    changes = [change('完成账单审阅', [0], kind='goal'),
               change('查明重试原因', list(range(stop)) if broad else [i for i in (1, 3) if i < stop])]
    if second_update:
        changes[1]['turn_indexes'] = [1]
        changes.append(change('查明重试原因', [3], operation='update', target=1))
    return turns, dict(segments=segments, changes=changes, focus_segment=stop-1,
                       current=dict(goal_indexes=[0], question_indexes=[1], next_step_indexes=[]))


def test_condensed_item_preserves_every_explicit_branch_transition():
    turns, output = repeated()
    doc, added, touched = apply_compact(empty(), output, turns, [], 'repeat')
    assert (added, touched) == (2, 0)
    assert len(doc['branches']) == 1
    assert [t['kind'] for t in doc['transitions']] == ['enter', 'return', 'enter', 'return']
    assert doc['overview']['focus']['branch_id'] is None
    assert doc['branches'][0]['state'] == 'waiting'


def test_second_branch_is_current_even_without_any_new_item_change():
    turns, output = repeated(stop=4)
    doc, _, _ = apply_compact(empty(), output, turns, [], 'stop-in-branch')
    assert doc['overview']['focus']['branch_id'] == doc['branches'][0]['id']
    assert doc['branches'][0]['state'] == 'active'


def test_cross_segment_evidence_does_not_swallow_branch_placement():
    turns, output = repeated(broad=True)
    doc, _, _ = apply_compact(empty(), output, turns, [], 'broad-evidence')
    assert len(doc['branches']) == 1
    locations = {p['item_id']: p for p in doc['placements']}
    assert locations[doc['items'][0]['id']]['branch_id'] is None
    assert locations[doc['items'][1]['id']]['branch_id'] == doc['branches'][0]['id']
    assert doc['phases'][0]['goal_item_ids'] == [doc['items'][0]['id']]


def test_repeat_update_reuses_same_branch_identity():
    turns, output = repeated(second_update=True)
    doc, _, _ = apply_compact(empty(), output, turns, [], 'repeat-update')
    assert len(doc['branches']) == 1
    assert {t['to_branch_id'] for t in doc['transitions'] if t['kind'] == 'enter'} == {doc['branches'][0]['id']}


def seed():
    turns, output = repeated(stop=3)
    doc, _, _ = apply_compact(empty(), output, turns, [], 'seed')
    return doc, [dict(index=i, **item) for i, item in enumerate(doc['items'])]


def test_explicit_existing_anchor_can_reenter_without_changes_across_compacts():
    doc, catalog = seed()
    before = deepcopy(doc)
    output = dict(segments=[dict(title='再次复核重试', action='branch', turn_indexes=[0], anchor_index=1)],
                  changes=[], focus_segment=0,
                  current=dict(goal_indexes=[0], question_indexes=[1], next_step_indexes=[]))
    result, added, touched = apply_compact(doc, output, packet(['再次进入同一重试调查。']), catalog, 'next-compact')
    assert (added, touched) == (0, 0)
    assert len(result['branches']) == 1
    assert result['overview']['focus']['branch_id'] == doc['branches'][0]['id']
    assert [t['kind'] for t in result['transitions']] == ['enter', 'return', 'enter']
    assert doc == before


def test_initial_turn_can_establish_mainline_and_enter_separate_branch():
    turns = packet(['主线是账单审阅。先进入重试调查支线。'])
    output = dict(segments=[dict(title='重试调查', action='branch', turn_indexes=[0], anchor_index=1)],
                  changes=[change('账单审阅', [0], kind='goal'), change('重试原因待查', [0])],
                  focus_segment=0, current=dict(goal_indexes=[0], question_indexes=[1], next_step_indexes=[]))
    doc, _, _ = apply_compact(empty(), output, turns, [], 'same-turn')
    assert doc['phases'][0]['title'] == '账单审阅'
    assert doc['phases'][0]['goal_item_ids'] == [doc['items'][0]['id']]
    assert doc['branches'][0]['evidence_item_ids'] == [doc['items'][1]['id']]
    locations = {p['item_id']: p for p in doc['placements']}
    assert locations[doc['items'][0]['id']]['branch_id'] is None
    assert locations[doc['items'][1]['id']]['branch_id'] == doc['branches'][0]['id']


def test_unknown_anchor_cannot_silently_drop_branch():
    doc, catalog = seed()
    output = dict(segments=[dict(title='错误支线', action='branch', turn_indexes=[0], anchor_index=99)],
                  changes=[], focus_segment=0,
                  current=dict(goal_indexes=[0], question_indexes=[1], next_step_indexes=[]))
    with pytest.raises(ValueError, match='compact_.*anchor'):
        apply_compact(doc, output, packet(['进入调查。']), catalog, 'unknown-anchor')


def test_closing_branch_problem_preserves_other_open_question():
    doc, catalog = seed()
    output = dict(segments=[dict(title='返回账单', action='return', turn_indexes=[0], anchor_index=None)],
                  changes=[change('重试调查已解决', [0], operation='resolve', target=1, open=False),
                           change('核对税额', [0])], focus_segment=0,
                  current=dict(goal_indexes=[0], question_indexes=[2], next_step_indexes=[]))
    result, _, _ = apply_compact(doc, output, packet(['重试调查已解决。回到账单主线，税额待核对。']), catalog, 'close')
    assert result['branches'][0]['state'] == 'resolved'
    assert result['branches'][0]['outcome_item_ids'] == [doc['items'][1]['id']]
    assert result['overview']['unresolved'] == ['核对税额']
    assert result['items'][0]['status'] == 'open'


@pytest.mark.parametrize('stop,focus,expected', [(5, 3, 'waiting'), (4, 0, 'active')])
def test_chronological_focus_ignores_conflicting_legacy_focus_selection(stop, focus, expected):
    turns, output = repeated(stop=stop)
    output['focus_segment'] = focus
    doc, _, _ = apply_compact(empty(), output, turns, [], 'selected-focus')
    assert doc['branches'][0]['state'] == expected
    assert (doc['overview']['focus']['branch_id'] is not None) == (expected == 'active')


def test_human_mainline_focus_lock_reconciles_branch_state():
    doc, catalog = seed()
    main_focus = deepcopy(doc['overview']['focus'])
    output = dict(segments=[dict(title='重试调查', action='branch', turn_indexes=[0], anchor_index=1)],
                  changes=[], focus_segment=0,
                  current=dict(goal_indexes=[0], question_indexes=[1], next_step_indexes=[]))
    result, _, _ = apply_compact(doc, output, packet(['再次进入调查。']), catalog, 'locked-focus',
        locks=[dict(entity='overview', id='overview', field='focus', value=main_focus)])
    assert result['overview']['focus'] == main_focus
    assert result['branches'][0]['state'] == 'waiting'


def test_new_phase_parks_prior_unresolved_branch():
    turns, output = repeated(stop=2)
    doc, _, _ = apply_compact(empty(), output, turns, [], 'active-branch')
    catalog = [dict(index=i, **item) for i, item in enumerate(doc['items'])]
    output = dict(segments=[dict(title='团队权限设计', action='new_phase', turn_indexes=[0], anchor_index=2)],
                  changes=[change('设计团队权限', [0], kind='goal')], focus_segment=0,
                  current=dict(goal_indexes=[2], question_indexes=[], next_step_indexes=[]))
    result, _, _ = apply_compact(doc, output, packet(['新目标改为团队权限设计。']), catalog, 'new-phase')
    assert result['overview']['focus']['branch_id'] is None
    assert result['branches'][0]['state'] == 'waiting'
    assert result['branches'][0]['phase_id'] != result['overview']['focus']['phase_id']


def test_phase_goal_identity_lock_preserved_when_question_enters_branch():
    doc, catalog = seed()
    # phase_merge/item_split can pin a human-maintained goal identity list.
    phase = doc['phases'][0]
    phase['goal_item_ids'].append(doc['items'][1]['id'])
    locked = list(phase['goal_item_ids'])
    output = dict(segments=[dict(title='重试调查', action='branch', turn_indexes=[0], anchor_index=1)],
                  changes=[change('核对税额', [0])], focus_segment=0,
                  current=dict(goal_indexes=[0], question_indexes=[1, 2], next_step_indexes=[]))
    result, _, _ = apply_compact(doc, output, packet(['重进调查，税额也待核对。']), catalog, 'phase-lock',
        locks=[dict(entity='phase', id=phase['id'], field='goal_item_ids', value=locked)])
    assert result['phases'][0]['goal_item_ids'] == locked


def test_manual_branch_state_and_outcome_locks_remain_authoritative():
    doc, catalog = seed()
    branch = doc['branches'][0]
    output = dict(segments=[dict(title='回到账单', action='return', turn_indexes=[0], anchor_index=None)],
                  changes=[change('重试调查解决', [0], operation='resolve', target=1, open=False)],
                  focus_segment=0, current=dict(goal_indexes=[0], question_indexes=[], next_step_indexes=[]))
    locks = [dict(entity='branch', id=branch['id'], field=f, value=deepcopy(branch[f]))
             for f in ('state', 'outcome_item_ids')]
    result, _, _ = apply_compact(doc, output, packet(['重试已解决。']), catalog, 'branch-locks', locks=locks)
    assert result['branches'][0]['state'] == branch['state']
    assert result['branches'][0]['outcome_item_ids'] == branch['outcome_item_ids']


def test_equal_titles_do_not_merge_distinct_branch_identities():
    turns = packet(['账单主线。', '调查邮件重试。', '回到主线。', '调查付款重试。'])
    output = dict(segments=[dict(title='账单', action='continue', turn_indexes=[0], anchor_index=None),
                           dict(title='重试调查', action='branch', turn_indexes=[1], anchor_index=1),
                           dict(title='返回账单', action='return', turn_indexes=[2], anchor_index=None),
                           dict(title='重试调查', action='branch', turn_indexes=[3], anchor_index=2)],
                  changes=[change('账单审阅', [0], kind='goal'),
                           change('重试待查', [1]), change('重试待查', [3])], focus_segment=3,
                  current=dict(goal_indexes=[0], question_indexes=[1, 2], next_step_indexes=[]))
    doc, _, _ = apply_compact(empty(), output, turns, [], 'distinct-objects')
    assert len(doc['branches']) == 2
    assert doc['branches'][0]['evidence_item_ids'] != doc['branches'][1]['evidence_item_ids']
    assert [b['state'] for b in doc['branches']] == ['waiting', 'active']


def evolve(doc, changes, *, action='continue', anchor=None, locks=(), task='branch-evolution'):
    catalog = [dict(index=i, **item) for i, item in enumerate(doc['items'])]
    output = dict(segments=[dict(title='重试调查', action=action, turn_indexes=[0], anchor_index=anchor)],
                  changes=changes, current=dict(goal_indexes=[], question_indexes=[], next_step_indexes=[]))
    return apply_compact(doc, output, packet(['重试调查进度已更新。']), catalog, task, locks=locks)[0]


@pytest.mark.parametrize('focused,expected', [(False, 'waiting'), (True, 'active')])
def test_reopening_resolved_item_refreshes_branch_without_navigation(focused, expected):
    doc, _ = seed()
    if focused:
        doc = evolve(doc, [], action='branch', anchor=1)
    closed = evolve(doc, [change('重试调查解决', [0], operation='resolve', target=1, open=False)])
    assert closed['branches'][0]['state'] == 'resolved'
    assert closed['branches'][0]['outcome_item_ids']
    result = evolve(closed, [change('重试问题再次出现', [0], operation='update', target=1)])
    assert result['branches'][0]['state'] == expected
    assert result['branches'][0]['outcome_item_ids'] == []
    assert result['overview']['focus'] == closed['overview']['focus']


def test_merge_into_open_canonical_item_reopens_parked_branch():
    doc, _ = seed()
    closed = evolve(doc, [change('重试调查解决', [0], operation='resolve', target=1, open=False)])
    merge = change('同一重试问题仍未解决', [0], operation='merge', target=2)
    merge['merge_indexes'] = [1]
    result = evolve(closed, [change('重试原因另一条记录', [0]), merge])
    assert result['branches'][0]['state'] == 'waiting'
    assert result['branches'][0]['outcome_item_ids'] == []
    assert result['items'][1]['hidden']
    assert result['items'][2]['status'] == 'open'


def test_merge_cannot_hide_a_human_pinned_visible_item():
    doc, _ = seed()
    before = deepcopy(doc)
    merge = change('同一重试调查', [0], operation='merge', target=2)
    merge['merge_indexes'] = [1]
    with pytest.raises(ValueError, match='compact_locked_merge_source'):
        evolve(doc, [change('同一重试另条记录', [0]), merge],
               locks=[dict(entity='item', id=doc['items'][1]['id'], field='hidden', value=False)])
    assert doc == before


@pytest.mark.parametrize('field', ['state', 'outcome_item_ids'])
def test_reopen_preserves_manually_locked_branch_completion_fields(field):
    doc, _ = seed()
    closed = evolve(doc, [change('重试调查解决', [0], operation='resolve', target=1, open=False)])
    branch = closed['branches'][0]
    result = evolve(closed, [change('重试问题再次出现', [0], operation='update', target=1)],
        locks=[dict(entity='branch', id=branch['id'], field=field, value=deepcopy(branch[field]))])
    assert result['branches'][0][field] == branch[field]
    if field == 'state':
        assert result['branches'][0]['outcome_item_ids']  # resolved layout still requires its outcome


@pytest.mark.parametrize('opened', [True, False])
def test_abandoned_branch_is_not_reactivated_by_an_item_update(opened):
    doc, _ = seed()
    doc['branches'][0]['state'] = 'abandoned'
    result = evolve(doc, [change('重试调查进度', [0], operation='update', target=1, open=opened)])
    assert result['branches'][0]['state'] == 'abandoned'
    assert result['overview']['focus']['branch_id'] is None


def test_live_mainline_action_has_same_internal_history_as_legacy_return():
    turns, output = repeated()
    for segment in output['segments']:
        if segment['action'] == 'return':
            segment['action'] = 'mainline'
    doc, _, _ = apply_compact(empty(), output, turns, [], 'explicit-mainline')
    assert [t['kind'] for t in doc['transitions']] == ['enter', 'return', 'enter', 'return']
    assert doc['overview']['focus']['branch_id'] is None
    assert doc['branches'][0]['state'] == 'waiting'
