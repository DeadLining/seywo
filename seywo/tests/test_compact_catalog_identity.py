"""A bounded history catalog must retain the identities of active work."""
from seywo.current_view import catalog_for
from test_compact_current import history


def crowded():
    doc = history()
    doc['overview']['open_question_ids'] = ['concern']
    doc['items'].append({**doc['items'][0], 'id': 'concern', 'title': '磁盘增长原因', 'type': 'observation'})
    for n in range(30):
        doc['items'].append({**doc['items'][0], 'id': f'noise-{n}', 'title': '账单核验输出结果'})
    return doc


def test_relevance_cannot_evict_open_current_priorities():
    doc = crowded()
    state, catalog, _ = catalog_for(doc, [{'user': '账单核验输出结果', 'assistant': ''}])
    identities = {item['id'] for item in catalog}
    assert {'goal', 'old', 'next', 'concern'} <= identities
    assert len(catalog) <= 12
    assert '磁盘增长原因' in state['open']


def test_focus_identity_survives_even_when_not_selected_as_current_priority():
    doc = crowded()
    doc['items'].append({**doc['items'][0], 'id': 'branch-anchor', 'title': '缓冲区调查', 'type': 'question'})
    doc['branches'] = [{'id': 'branch', 'phase_id': 'now-phase', 'evidence_item_ids': ['branch-anchor'],
                        'title': '调查缓冲区'}]
    doc['overview']['focus']['branch_id'] = 'branch'
    _, catalog, _ = catalog_for(doc, [{'user': '账单核验输出结果', 'assistant': ''}])
    assert 'branch-anchor' in {item['id'] for item in catalog}
    assert len(catalog) <= 12


def test_focus_anchor_is_canonical_after_repeated_merges():
    doc = crowded()
    doc['items'].append({**doc['items'][0], 'id': 'survivor', 'title': '最终调查身份'})
    doc['branches'] = [{'id': 'branch', 'phase_id': 'now-phase', 'evidence_item_ids': ['original'],
                        'title': '调查缓冲区'}]
    doc['overview']['focus']['branch_id'] = 'branch'
    doc['identity_changes'] = [
        {'kind': 'item_merge', 'from_ids': ['original'], 'to_ids': ['middle']},
        {'kind': 'item_merge', 'from_ids': ['middle'], 'to_ids': ['survivor']}]
    _, catalog, _ = catalog_for(doc, [{'user': '账单核验输出结果', 'assistant': ''}])
    assert 'survivor' in {item['id'] for item in catalog}
    assert not {'original', 'middle'} & {item['id'] for item in catalog}


def test_hidden_current_identity_is_not_reintroduced():
    doc = crowded()
    doc['items'][1]['hidden'] = True
    _, catalog, _ = catalog_for(doc, [{'user': '账单核验输出结果', 'assistant': ''}])
    assert 'goal' not in {item['id'] for item in catalog}
