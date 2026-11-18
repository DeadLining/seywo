"""Current priorities are an identity-based projection, not a historical backlog."""
from .windows import terms
from .compact_identity import canonical_item

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
    current = {canonical_item(doc, i) for key in FIELDS.values() for i in overview.get(key, [])}
    def rank(pair):
        pos, item = pair
        overlap = len(query & terms(item.get('title', '')))
        location = locations.get(item['id'], {})
        here = (location.get('phase_id') == focus.get('phase_id')
                and location.get('branch_id') == focus.get('branch_id'))
        return (bool(overlap), overlap, here, item['id'] in current, pos)
    # Current work and structural anchors are obligatory identities. Lexical
    # relevance only fills spare slots; it must not evict the active line.
    required = list(overview.get('goal_item_ids', [])[:1])
    phase = next((p for p in doc.get('phases', []) if p['id'] == focus.get('phase_id')), {})
    branch = next((b for b in doc.get('branches', []) if b['id'] == focus.get('branch_id')), {})
    required += phase.get('goal_item_ids', [])[:1] + branch.get('evidence_item_ids', [])[:1]
    for key, limit in LIMITS.items():
        required += overview.get(FIELDS[key], [])[:limit]
    required_ids = {canonical_item(doc, identity) for identity in required}
    reserved = [(n, item) for n, item in enumerate(visible) if item['id'] in required_ids]
    chosen = reserved + sorted(((n, i) for n, i in enumerate(visible) if i['id'] not in required_ids),
                               key=rank, reverse=True)[:12 - len(reserved)]
    # Stable chronological indexes within the selected, relevant working set.
    selected = [i for _, i in sorted(chosen)]
    catalog = [{'index': n, 'id': i['id'], 'title': i.get('title', ''),
                'type': i['type'], 'status': i.get('status', 'open')}
               for n, i in enumerate(selected)]
    by_id = {i['id']: i for i in visible}
    def titles(key, limit):
        ids = [canonical_item(doc, identity) for identity in overview.get(key, [])]
        return list(dict.fromkeys(by_id[i]['title'] for i in ids if i in by_id))[:limit]
    priorities = [canonical_item(doc, identity) for field in ('open_question_ids', 'next_step_ids')
                  for identity in overview.get(field, [])]
    state = {'focus': ' / '.join(v for v in (phase.get('title', ''), branch.get('title', '')) if v),
             'goals': titles('goal_item_ids', 1),
             'open': [by_id[identity]['title'] for identity in dict.fromkeys(priorities)
                      if identity in by_id and identity in {i['id'] for i in selected}
                      and by_id[identity].get('status') == 'open'][:4]}
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
        # The model chooses identities and priority, not a second copy of
        # each item's type. Legacy typed lists are only selection evidence.
        if 'priority_indexes' in selection:
            goals = [] if selection['goal_index'] is None else [selection['goal_index']]
            priorities = selection['priority_indexes']
        else:
            goals = selection['goal_indexes']
            priorities = selection['question_indexes'] + selection['next_step_indexes']
        picked = {key: [] for key in LIMITS}

        def selected(index):
            item = by_id.get(canonical_item(doc, identities.get(index))) if type(index) is int else None
            if item is None:
                raise ValueError('compact_invalid_current_item')
            return item if item.get('status', 'open') == 'open' else None

        for index in goals:
            item = selected(index)
            if item is not None:
                if item['type'] not in TYPES['goal_indexes']:
                    raise ValueError('compact_invalid_current_item')
                picked['goal_indexes'].append(item['id'])
        # Dedup against the goal that will actually be published after locks.
        effective_goals = overview['goal_item_ids'] if 'goal_item_ids' in locked else picked['goal_indexes']
        seen = set(effective_goals)
        for index in priorities:
            item = selected(index)
            if item is None or item['id'] in seen:
                continue
            seen.add(item['id'])
            # Priority is an explicit work role. An unresolved fault may be
            # categorized as an observation; never discard that selected
            # concern merely because its information type is not question.
            key = 'next_step_indexes' if item['type'] in TYPES['next_step_indexes'] else 'question_indexes'
            if len(picked[key]) < LIMITS[key]:
                picked[key].append(item['id'])
    for key, field in FIELDS.items():
        if field not in locked:
            overview[field] = picked[key]
    # Goals describe purpose. Only current questions/actions describe a stop.
    ids = overview['open_question_ids'] + overview['next_step_ids']
    overview['unresolved'] = list(dict.fromkeys(by_id[i]['title'] for i in ids if i in by_id))
