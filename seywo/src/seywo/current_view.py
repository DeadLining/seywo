"""Current priorities are an identity-based projection, not a historical backlog."""
from .windows import terms

LIMITS = {'goal_indexes': 1, 'question_indexes': 3, 'next_step_indexes': 3}
FIELDS = {'goal_indexes': 'goal_item_ids', 'question_indexes': 'open_question_ids',
          'next_step_indexes': 'next_step_ids'}
TYPES = {'goal_indexes': {'goal', 'question', 'decision', 'action', 'next_step'},
         'question_indexes': {'question'}, 'next_step_indexes': {'action', 'next_step'}}


def catalog_for(doc, turns=()):
    overview = doc.get('overview', {})
    focus = overview.get('focus', {})
    visible = [i for i in doc.get('items', []) if not i.get('hidden')]
    locations = {p['item_id']: p for p in doc.get('placements', [])}
    query = terms(' '.join(t.get('user', '') + ' ' + t.get('assistant', '') for t in turns))
    current = {i for key in FIELDS.values() for i in overview.get(key, [])}
    def rank(pair):
        pos, item = pair
        overlap = len(query & terms(item.get('title', '')))
        location = locations.get(item['id'], {})
        here = (location.get('phase_id') == focus.get('phase_id')
                and location.get('branch_id') == focus.get('branch_id'))
        return (bool(overlap), overlap, here, item['id'] in current, pos)
    goal_ids = overview.get('goal_item_ids', [])[:1]
    reserved = [(n, i) for n, i in enumerate(visible) if i['id'] in goal_ids]
    chosen = reserved + sorted(((n, i) for n, i in enumerate(visible) if i['id'] not in goal_ids),
                               key=rank, reverse=True)[:12 - len(reserved)]
    # Stable chronological indexes within the selected, relevant working set.
    selected = [i for _, i in sorted(chosen)]
    catalog = [{'index': n, 'id': i['id'], 'title': i.get('title', ''),
                'type': i['type'], 'status': i.get('status', 'open')}
               for n, i in enumerate(selected)]
    by_id = {i['id']: i for i in visible}
    def titles(key, limit):
        return [by_id[i]['title'] for i in overview.get(key, []) if i in by_id][:limit]
    phase = next((p['title'] for p in doc.get('phases', []) if p['id'] == focus.get('phase_id')), '')
    branch = next((b['title'] for b in doc.get('branches', []) if b['id'] == focus.get('branch_id')), '')
    state = {'focus': ' / '.join(v for v in (phase, branch) if v),
             'goals': titles('goal_item_ids', 1),
             'open': [i['title'] for i in selected if i['id'] in current
                      and i.get('status') == 'open' and i['type'] in {'action', 'next_step', 'question'}][-4:]}
    return state, catalog, {i['index']: i['id'] for i in catalog}


def project_current(doc, selection, identities, *, locks=()):
    by_id = {i['id']: i for i in doc['items'] if not i.get('hidden')}
    overview = doc['overview']
    locked = {lock['field'] for lock in locks if lock.get('entity') == 'overview'}
    if selection is None:
        # Compatibility for stored v1 outputs: derive a bounded current-phase
        # view. Historical open status alone never means "current priority".
        focus = overview['focus']
        locations = {p['item_id']: p for p in doc['placements']}
        active = [i for i in by_id.values() if i.get('status') == 'open'
                  and locations.get(i['id'], {}).get('phase_id') == focus.get('phase_id')
                  and locations.get(i['id'], {}).get('branch_id') == focus.get('branch_id')]
        picked = {key: [i['id'] for i in reversed(active) if i['type'] in TYPES[key]][:limit]
                  for key, limit in LIMITS.items()}
    else:
        picked = {}
        for key in LIMITS:
            picked[key] = []
            for index in selection[key]:
                identity = identities.get(index)
                item = by_id.get(identity)
                if not item or item.get('status') != 'open' or item['type'] not in TYPES[key]:
                    raise ValueError('compact_invalid_current_item')
                picked[key].append(identity)
    for key, field in FIELDS.items():
        if field not in locked:
            overview[field] = picked[key]
    # Goals describe purpose. Only current questions/actions describe a stop.
    ids = overview['open_question_ids'] + overview['next_step_ids']
    overview['unresolved'] = list(dict.fromkeys(by_id[i]['title'] for i in ids if i in by_id))
