"""One native user turn can contain several real structure actions."""
import pytest

from seywo.compact_model import validate
from seywo.compact_pipeline import apply_compact
from seywo.narrative import empty
from test_compact_pipeline import source, turn


def semantic():
    return {'segments': [
        {'title': '账单主线', 'action': 'mainline', 'anchor_index': 0, 'turn_indexes': [0]},
        {'title': '进入重试调查', 'action': 'branch', 'anchor_index': 1, 'turn_indexes': [0]},
        {'title': '回到账单', 'action': 'mainline', 'anchor_index': 0, 'turn_indexes': [0]}],
        'changes': [
            {'operation': 'new', 'title': '账单审阅', 'type': 'goal', 'attribution': 'user',
             'turn_indexes': [0], 'target_index': None, 'open': True, 'merge_indexes': []},
            {'operation': 'new', 'title': '重试调查', 'type': 'question', 'attribution': 'user',
             'turn_indexes': [0], 'target_index': None, 'open': True, 'merge_indexes': []}],
        'current': {'goal_index': 0, 'priority_indexes': [1]}}


def test_same_native_turn_can_supply_multiple_ordered_structural_events():
    raw = semantic()
    checked = validate(raw, [0], set())
    text = '主线账单审阅；先进入重试调查，排查后仍未知，现在返回账单主线。'
    turns = [turn('native', text, '', source(0, 'user', text))]
    doc, added, _ = apply_compact(empty(), checked, turns, [], 'same-native')
    assert added == 2
    assert len(doc['branches']) == 1
    assert [t['kind'] for t in doc['transitions']] == ['enter', 'return']
    assert doc['overview']['focus']['branch_id'] is None
    assert doc['branches'][0]['state'] == 'waiting'
    assert doc['placements'][0]['branch_id'] is None
    assert doc['placements'][1]['branch_id'] == doc['branches'][0]['id']


def test_adjacent_segments_can_share_a_boundary_turn_but_not_reverse_time():
    raw = semantic()
    raw['segments'] = [
        {'title': '主线', 'action': 'continue', 'anchor_index': None, 'turn_indexes': [0, 1]},
        {'title': '调查', 'action': 'branch', 'anchor_index': 1, 'turn_indexes': [1, 2]}]
    assert validate(raw, [0, 1, 2], set())['segments'][1]['turn_indexes'] == [1, 2]
    raw['segments'][1]['turn_indexes'] = [0, 1, 2]
    with pytest.raises(ValueError, match='compact_turn_order'):
        validate(raw, [0, 1, 2], set())


def test_reusing_a_turn_does_not_allow_missing_another_turn():
    with pytest.raises(ValueError, match='compact_turn_coverage'):
        validate(semantic(), [0, 1], set())
