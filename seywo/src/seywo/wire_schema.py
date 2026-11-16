"""Executable provider JSON Schema for the small indexed delta protocol.

The local hydrator remains authoritative for evidence, role and identity checks.
Optional fields stay optional (strict=False); providers may enforce or merely use
this schema as guidance. No fallback silently broadens the response contract.
"""
from .contracts import ITEM_TYPES, RELATIONS


def obj(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required),
            'additionalProperties': False}


def array(items):
    return {'type': 'array', 'items': items}


INT = {'type': 'integer', 'minimum': 0}
NULL_INT = {'type': ['integer', 'null'], 'minimum': 0}
TEXT = {'type': 'string', 'minLength': 1}
INTS = array(INT)


def choice(values):
    return {'type': 'string', 'enum': list(values)}


def response_schema(wire):
    if wire['phase'] in {'extract', 'merge'}:
        item = obj({'type': choice(ITEM_TYPES),
                    'title': {**TEXT, 'maxLength': 40},
                    'source_indexes': {**INTS, 'minItems': 1},
                    'detail': {**TEXT, 'maxLength': 240},
                    'scope': {**TEXT, 'maxLength': 240},
                    'inference': {'type': 'boolean'},
                    'dependencies': INTS,
                    'relations': array(obj({'source_index': INT, 'relation': choice(RELATIONS)},
                                           ['source_index', 'relation']))},
                   ['type', 'title', 'source_indexes'])
        edit = {'anyOf': [
            obj({'op': {'const': 'merge'}, 'item_indexes': {**INTS, 'minItems': 2},
                 'into_index': INT, 'reason': TEXT}, ['op', 'item_indexes', 'into_index', 'reason']),
            obj({'op': {'const': 'drop'}, 'item_index': INT, 'source_indexes': {**INTS, 'minItems': 1},
                 'reason': TEXT}, ['op', 'item_index', 'source_indexes', 'reason']),
            obj({'op': {'const': 'relate'}, 'item_index': INT, 'source_index': INT,
                 'relation': choice(RELATIONS), 'reason': TEXT},
                ['op', 'item_index', 'source_index', 'relation'])]}
        fields = {'items': array(item), 'edits': array(edit), 'unresolved': INTS,
                  'no_work_source_indexes': INTS,
                  'reads': array(obj({'source_index': INT, 'direction': choice(['before', 'after']),
                                      'reason': TEXT}, ['source_index', 'direction', 'reason'])),
                  'resolve': array(obj({'unresolved_index': INT, 'source_indexes': {**INTS, 'minItems': 1},
                                       'reason': TEXT}, ['unresolved_index', 'source_indexes', 'reason']))}
    elif wire['phase'] == 'organize':
        phase = {'title': TEXT, 'goal_item_indexes': INTS}
        branch = {'phase_index': INT, 'title': TEXT, 'goal': TEXT, 'resolve_when': TEXT,
                  'state': choice(['active', 'waiting', 'resolved', 'abandoned']),
                  'outcome_item_indexes': INTS, 'evidence_item_indexes': INTS}
        fields = {
            'bindings': array(obj({'item_index': INT, 'target_index': {'type': 'integer', 'minimum': -1},
                                   'action': choice(['keep', 'revise', 'propose']), 'reason': TEXT},
                                  ['item_index', 'target_index', 'action', 'reason'])),
            'phases': array(obj(phase, phase)),
            'phase_updates': array(obj({'index': INT, **phase}, ['index'])),
            'branches': array(obj(branch, branch)),
            'branch_updates': array(obj({'index': INT, **branch}, ['index'])),
            'placements': array(obj({'item_index': INT, 'phase_index': NULL_INT,
                                      'branch_index': NULL_INT}, ['item_index', 'phase_index'])),
            'transitions': array(obj({'kind': choice(['enter', 'return', 'goal_change', 'reopen', 'focus']),
                                      'from_phase_index': NULL_INT, 'from_branch_index': NULL_INT,
                                      'to_phase_index': NULL_INT, 'to_branch_index': NULL_INT,
                                      'result_item_indexes': INTS, 'unresolved': INTS, 'reason': TEXT,
                                      'evidence_source_indexes': {**INTS, 'minItems': 1}},
                                     ['kind', 'from_phase_index', 'from_branch_index', 'to_phase_index',
                                      'to_branch_index', 'reason', 'evidence_source_indexes'])),
            'overview': obj({'goal_item_indexes': INTS,
                             'focus': obj({'phase_index': NULL_INT, 'branch_index': NULL_INT},
                                          ['phase_index', 'branch_index']),
                             'open_question_indexes': INTS, 'next_step_indexes': INTS,
                             'unresolved_add': INTS,
                             'unresolved_resolve': array(obj({'unresolved_index': INT, 'item_indexes': INTS,
                                                               'reason': TEXT},
                                                              ['unresolved_index', 'item_indexes', 'reason']))})}
    else:
        fields = {'choice': choice(wire['options']),
                  'scores': obj({option: {'type': 'number'} for option in wire['options']}, wire['options']),
                  'score_kind': choice(['probabilities', 'scores']), 'reason': TEXT}
    if wire['phase'] == 'organize':
        actions = [item['index'] for item in wire['items']
                   if item['type'] in {'action', 'goal', 'next_step'}]
        fields['overview']['properties']['next_step_indexes'] = (
            array({**INT, 'enum': actions}) if actions else {'type': 'array', 'maxItems': 0})
        issues = [item['index'] for item in wire['items']
                  if item['type'] in {'question', 'observation', 'correction'}
                  and ('catalog_scope' not in wire or item['title'] in wire['unresolved'])]
        issue_refs = array({**INT, 'enum': issues}) if issues else {'type': 'array', 'maxItems': 0}
        fields['overview']['properties']['unresolved_add'] = issue_refs
        fields['transitions']['items']['properties']['unresolved'] = issue_refs
    advertised = wire['output_schema']
    allowed = set(advertised.get('required', [])) | set(advertised.get('optional', []))
    if wire['phase'] == 'judgment':
        allowed = set(fields)
    return obj({key: value for key, value in fields.items() if key in allowed}, advertised.get('required', []))
