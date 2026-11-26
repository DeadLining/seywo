"""Current focus is a selected view; historical open states are not a todo list."""

import pytest

from seywo.compact_model import instructions, request_payload, validate, SCHEMA
from seywo.compact_pipeline import apply_compact, _state_and_catalog
from seywo.narrative import empty
from test_compact_pipeline import source, turn, output


def history():
    doc = empty()
    doc['phases'] = [{'id': 'old-phase', 'title': '旧目标', 'goal_item_ids': []},
                     {'id': 'now-phase', 'title': '插件验收', 'goal_item_ids': ['goal']}]
    doc['overview'].update(focus={'phase_id': 'now-phase', 'branch_id': None},
                          goal_item_ids=['goal'], next_step_ids=['old', 'next'],
                          unresolved=['幽灵待办', '旧 Hook 未接入'])
    for identity, title, kind, phase in [('old', '旧 Hook 未接入', 'action', 'old-phase'),
            ('goal', '插件验收', 'goal', 'now-phase'), ('next', '验收检索结果', 'next_step', 'now-phase')]:
        doc['items'].append({'id': identity, 'title': title, 'content': title, 'type': kind,
            'status': 'open', 'hidden': False, 'attribution': 'user_statement',
            'evidence': [], 'dependencies': [], 'scope': 'chat'})
        doc['placements'].append({'item_id': identity, 'phase_id': phase, 'branch_id': None})
    return doc


def test_current_view_replaces_old_strings_without_destroying_history():
    doc = history()
    semantic = output(operation='resolve', title='Hook 已就绪', item_type='action', target_index=0)
    semantic['current'] = {"goal_index": 1, "priority_indexes": [2]}
    catalog = [{'index': n, **v} for n, v in enumerate(doc['items'])]
    turns = [turn('t', 'Hook 已就绪，接下来核验检索', '', source(1, 'user', 'Hook 已就绪，接下来核验检索'))]
    result, _, _ = apply_compact(doc, semantic, turns, catalog, 'current')
    assert result['overview']['goal_item_ids'] == ['goal']
    assert result['overview']['next_step_ids'] == ['next']
    assert result['overview']['unresolved'] == ['验收检索结果']
    assert len(result['items']) == 3
    assert doc['items'][0]['status'] == 'open'  # old revision untouched


def test_explicit_current_can_leave_old_open_items_as_history():
    doc = history()
    semantic = {'segments': [{'title': '插件验收', 'turn_indexes': [0], 'action': 'continue', "anchor_index": None}],
                'changes': [],
                'current': {"goal_index": 1, "priority_indexes": [2]}}
    turns = [turn('t', '重点是插件验收', '', source(1, 'user', '重点是插件验收'))]
    result, _, _ = apply_compact(doc, semantic, turns,
        [{'index': n, **v} for n, v in enumerate(doc['items'])], 'view-only')
    assert result['items'][0]['status'] == 'open'
    assert 'old' not in result['overview']['next_step_ids']
    assert result != doc


def test_catalog_does_not_starve_recent_relevant_history_behind_old_todos():
    doc = history()
    for n in range(30):
        doc['items'].insert(0, {**doc['items'][0], 'id': f'old-{n}', 'title': f'早期问题 {n}'})
        doc['overview']['next_step_ids'].insert(0, f'old-{n}')
    state, catalog, _ = _state_and_catalog(doc, [{'user': '验收检索结果', 'assistant': ''}])
    assert len(catalog) <= 12
    assert 'next' in {v['id'] for v in catalog}
    assert '幽灵待办' not in state['open']


@pytest.mark.parametrize('current', [
    {"goal_index": 99, "priority_indexes": []},
    {'goal_indexes': [0, 0], 'question_indexes': [], 'next_step_indexes': []},
    {"goal_index": None, "priority_indexes": list(range(7))},
])
def test_invalid_current_indexes_fail_at_contract_boundary(current):
    semantic = output()
    semantic['current'] = current
    with pytest.raises(ValueError, match='compact_.*current'):
        validate(semantic, [0], {0, 1, 2, 3})


def test_schema_is_sent_once_through_structured_output_not_duplicated_in_prompt():
    assert 'current' in SCHEMA['required']
    assert 'additionalProperties' not in instructions()
    assert len(instructions()) < 1800
    payload = request_payload([], {}, [{'index': 0, 'title': '待验证', 'type': 'action', 'status': 'open'}])
    assert payload['active_items'][0]['status'] == 'open'


def test_current_view_respects_manual_overview_selection():
    doc = history()
    semantic = {'segments': [{'title': '插件验收', 'turn_indexes': [0], 'action': 'continue', "anchor_index": None}],
                'changes': [],
                'current': {"goal_index": 1, "priority_indexes": [2]}}
    result, _, _ = apply_compact(doc, semantic,
        [turn('t', '验收检索', '', source(1, 'user', '验收检索'))],
        [{'index': n, **v} for n, v in enumerate(doc['items'])], 'locked',
        locks=[{'entity': 'overview', 'id': 'overview', 'field': 'next_step_ids', 'value': ['old', 'next']}])
    assert result['overview']['next_step_ids'] == ['old', 'next']


def test_new_virtual_indexes_do_not_collide_with_historical_targets():
    doc = history()
    semantic = {
        'segments': [{'title': '当前工作', 'turn_indexes': [0], 'action': 'continue', "anchor_index": None}],
        'changes': [
            {'operation': 'new', 'title': '新事项', 'type': 'action', 'attribution': 'user',
             'turn_indexes': [0], 'target_index': None, 'open': True, 'merge_indexes': []},
            {'operation': 'update', 'title': '新事项补充', 'type': 'action', 'attribution': 'user',
             'turn_indexes': [0], 'target_index': 3, 'open': True, 'merge_indexes': []}],

        'current': {"goal_index": 1, "priority_indexes": [3]}}
    validated = validate(semantic, [0], {0, 1, 2})
    result, added, touched = apply_compact(doc, validated,
        [turn('t', '新增事项并补充要求', '', source(1, 'user', '新增事项并补充要求'))],
        [{'index': n, **v} for n, v in enumerate(doc['items'])], 'new-virtual')
    assert (added, touched) == (1, 1)
    assert result['items'][0] == doc['items'][0]
    assert result['items'][-1]['title'] == '新事项补充'
    assert result['overview']['next_step_ids'] == [result['items'][-1]['id']]


def test_completion_of_one_subtask_cannot_override_model_open_composite_status():
    doc = history()
    semantic = output(operation='update', title='Hook已完成，依赖安装待验证',
                      item_type='action', attribution='assistant', target_index=0, open=True)
    semantic['current'] = {"goal_index": 1, "priority_indexes": [0]}
    text = '已实现 Hook 接入，依赖自动安装还没验证'
    result, _, _ = apply_compact(doc, semantic,
        [turn('t', '', text, source(1, 'assistant', text))],
        [{'index': n, **v} for n, v in enumerate(doc['items'])], 'composite')
    assert result['items'][0]['status'] == 'open'


def test_relevance_selection_reserves_the_current_goal():
    doc = history()
    for n in range(20):
        doc['items'].append({**doc['items'][0], 'id': f'related-{n}', 'title': '验收检索结果回归'})
    _, catalog, _ = _state_and_catalog(doc, [{'user': '验收检索结果回归', 'assistant': ''}])
    assert 'goal' in {i['id'] for i in catalog}
    assert len(catalog) <= 12


def test_chronological_actions_select_current_line_and_manual_focus_has_precedence():
    semantic = {
        'segments': [{'title': '主线', 'turn_indexes': [0], 'action': 'continue', "anchor_index": None},
                     {'title': '排查支线', 'turn_indexes': [1], 'action': 'branch', "anchor_index": None}],
        'changes': [{'operation': 'new', 'title': title, 'type': 'goal', 'attribution': 'user',
                     'turn_indexes': [n], 'target_index': None, 'open': True, 'merge_indexes': []}
                    for n, title in enumerate(['主线目标', '支线目标'])],

        'current': {"goal_index": 0, "priority_indexes": []}}
    turns = [turn('t1', '主线目标', '', source(1, 'user', '主线目标')),
             turn('t2', '排查支线', '', source(2, 'user', '排查支线', turn='t2'))]
    doc, _, _ = apply_compact(empty(), semantic, turns, [], 'focus')
    assert len(doc['branches']) == 1
    assert doc['overview']['focus']['branch_id'] == doc['branches'][0]['id']
    prior = history()
    focused = {'segments': [{'title': '当前工作', 'turn_indexes': [0], 'action': 'continue', "anchor_index": None}],
        'changes': [],
        'current': {"goal_index": 1, "priority_indexes": [2]}}
    locked_focus = {'phase_id': 'old-phase', 'branch_id': None}
    revised, _, _ = apply_compact(prior, focused, turns[:1],
        [{'index': n, **v} for n, v in enumerate(prior['items'])], 'locked-focus',
        locks=[{'entity': 'overview', 'id': 'overview', 'field': 'focus', 'value': locked_focus}])
    assert revised['overview']['focus'] == locked_focus


def test_new_model_boundary_cannot_silently_fall_back_to_legacy_current_view():
    semantic = output()
    del semantic['current']
    with pytest.raises(ValueError, match='compact_current_required'):
        validate(semantic, [0], set())


def test_title_update_preserves_independently_locked_manual_content():
    doc = history()
    doc['items'][0]['content'] = '人工补充的准确说明'
    semantic = output(operation='update', title='Hook 已确认', item_type='action', target_index=0, open=True)
    semantic['current'] = {"goal_index": 1, "priority_indexes": [0]}
    result, _, _ = apply_compact(doc, semantic,
        [turn('t', 'Hook 已确认', '', source(1, 'user', 'Hook 已确认'))],
        [{'index': n, **v} for n, v in enumerate(doc['items'])], 'content-locked',
        locks=[{'entity': 'item', 'id': 'old', 'field': 'content', 'value': '人工补充的准确说明'}])
    assert result['items'][0]['title'] == 'Hook 已确认'
    assert result['items'][0]['content'] == '人工补充的准确说明'


def select(doc, selection, *, locks=()):
    semantic = {'segments': [{'title': '当前工作', 'turn_indexes': [0], 'action': 'continue', "anchor_index": None}],
                'changes': [], 'current': selection}
    return apply_compact(doc, semantic,
        [turn('t', '继续当前重点。', '', source(1, 'user', '继续当前重点。'))],
        [{'index': n, **v} for n, v in enumerate(doc['items'])], 'priority-projection', locks=locks)[0]


def question_history(count=1):
    doc = history()
    for n in range(count):
        identity = f'question-{n}'
        doc['items'].append({**doc['items'][1], 'id': identity, 'title': f'待查问题{n}', 'type': 'question'})
        doc['placements'].append({'item_id': identity, 'phase_id': 'now-phase', 'branch_id': None})
    return doc


def test_priority_indexes_are_classified_by_materialized_type_once():
    doc = question_history()
    result = select(doc, {'goal_index': 1, 'priority_indexes': [3, 2, 3]})
    assert result['overview']['goal_item_ids'] == ['goal']
    assert result['overview']['open_question_ids'] == ['question-0']
    assert result['overview']['next_step_ids'] == ['next']
    assert result['overview']['unresolved'] == ['待查问题0', '验收检索结果']


def test_duplicate_priority_is_projected_once():
    doc = question_history()
    result = select(doc, {"goal_index": 1, "priority_indexes": [3, 3]})
    assert result['overview']['open_question_ids'] == ['question-0']
    assert result['overview']['next_step_ids'] == []


def test_completed_items_cannot_remain_current_even_when_selected():
    doc = question_history()
    doc['items'][0]['status'] = 'resolved'
    doc['items'][1]['status'] = 'resolved'
    result = select(doc, {'goal_index': 1, 'priority_indexes': [0, 2, 3]})
    assert result['overview']['goal_item_ids'] == []
    assert result['overview']['open_question_ids'] == ['question-0']
    assert result['overview']['next_step_ids'] == ['next']


def test_priority_order_bounds_each_category_without_losing_history():
    doc = question_history(count=6)
    result = select(doc, {'goal_index': 1, 'priority_indexes': [8, 7, 6, 5, 4, 3]})
    assert result['overview']['open_question_ids'] == ['question-5', 'question-4', 'question-3']
    assert len(result['items']) == len(doc['items'])
    assert all(i['status'] == 'open' for i in result['items'])


def test_current_goal_is_a_semantic_choice_and_can_be_a_question():
    doc = question_history()
    result = select(doc, {'goal_index': 3, 'priority_indexes': [2]})
    assert result['overview']['goal_item_ids'] == ['question-0']
    assert result['overview']['open_question_ids'] == []
    assert result['overview']['next_step_ids'] == ['next']


def test_unknown_priority_identity_is_still_rejected():
    with pytest.raises(ValueError, match='compact_invalid_current_item'):
        select(history(), {'goal_index': 1, 'priority_indexes': [99]})


@pytest.mark.parametrize('item_type', ['observation', 'correction', 'decision', 'option', 'goal'])
def test_selected_open_concern_is_not_lost_due_to_information_category(item_type):
    doc = question_history()
    doc['items'][3]['type'] = item_type
    result = select(doc, {'goal_index': 1, 'priority_indexes': [1, 3]})
    assert result['overview']['goal_item_ids'] == ['goal']
    assert result['overview']['open_question_ids'] == ['question-0']
    assert result['overview']['unresolved'] == ['待查问题0']
    assert result['items'][3]['type'] == item_type


@pytest.mark.parametrize('model_goal', [None, 3])
def test_priority_dedup_uses_effective_human_locked_goal(model_goal):
    doc = question_history()
    result = select(doc, {'goal_index': model_goal, 'priority_indexes': [1, 3]},
        locks=[{'entity': 'overview', 'id': 'overview', 'field': 'goal_item_ids', 'value': ['goal']}])
    assert result['overview']['goal_item_ids'] == ['goal']
    assert result['overview']['open_question_ids'] == ['question-0']
    assert result['overview']['unresolved'] == ['待查问题0']
