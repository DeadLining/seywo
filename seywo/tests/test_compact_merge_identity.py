"""All post-merge references must address the surviving work identity."""
import pytest

from seywo.compact_model import validate
from seywo.compact_pipeline import apply_compact
from seywo.current_view import project_current
from seywo.narrative import empty
from test_compact_pipeline import source, turn


def item(identity):
    return {'id': identity, 'title': identity, 'content': identity, 'type': 'question',
            'status': 'open', 'hidden': False, 'attribution': 'user_statement', 'scope': 'chat',
            'evidence': [], 'dependencies': [], 'origin': 'compact_model', 'user_confirmed': False}


def history():
    doc = empty()
    doc['items'] = [item('survivor'), item('absorbed')]
    doc['phases'] = [{'id': 'phase', 'title': '工作', 'goal_item_ids': []}]
    doc['overview']['focus'] = {'phase_id': 'phase', 'branch_id': None}
    doc['placements'] = [{'item_id': i['id'], 'phase_id': 'phase', 'branch_id': None} for i in doc['items']]
    return doc


def batch(*, close=False, branch=False):
    changes = [{'operation': 'merge', 'title': '合并调查', 'type': 'question', 'attribution': 'user',
                'turn_indexes': [0], 'target_index': 0, 'open': True, 'merge_indexes': [1]}]
    if close:
        changes.append({'operation': 'resolve', 'title': '调查已解决', 'type': 'question', 'attribution': 'user',
                        'turn_indexes': [0], 'target_index': 1, 'open': False, 'merge_indexes': []})
    return {'segments': [{'title': '调查', 'action': 'branch' if branch else 'continue',
                          'anchor_index': 1 if branch else None, 'turn_indexes': [0]}],
            'changes': changes, 'current': {'goal_index': None, 'priority_indexes': [1]}}


def apply(output):
    doc = history()
    refs = [{'index': n, **i} for n, i in enumerate(doc['items'])]
    return apply_compact(doc, validate(output, [0], {0, 1}),
                         [turn('native', '合并两个调查，再根据进展处理', '', source(1, 'user', '合并两个调查，再根据进展处理'))],
                         refs, 'merged')[0]


def test_current_reference_to_merged_item_selects_the_survivor():
    doc = apply(batch())
    assert doc['overview']['open_question_ids'] == ['survivor']
    assert doc['items'][1]['hidden']


def test_later_update_via_a_merged_identity_updates_the_survivor_once():
    doc = apply(batch(close=True))
    assert doc['items'][0]['title'] == '调查已解决'
    assert doc['items'][0]['status'] == 'resolved'
    assert doc['overview']['open_question_ids'] == []


def test_structural_anchor_to_merged_item_uses_surviving_identity():
    doc = apply(batch(branch=True))
    assert doc['branches'][0]['evidence_item_ids'] == ['survivor']


def test_manually_hidden_item_without_merge_provenance_is_still_rejected():
    doc = history()
    doc['items'][1]['hidden'] = True
    with pytest.raises(ValueError, match='compact_invalid_current_item'):
        project_current(doc, {'goal_index': None, 'priority_indexes': [1]}, {0: 'survivor', 1: 'absorbed'})
