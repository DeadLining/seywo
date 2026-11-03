"""Exact boundary addressing; no extra model calls or persistent schema changes."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from sessiongraph.graph_contract import (
    atomic_anchored, check, policy, prepare_evidence_spans, resolve_evidence_spans, validate,
)


def wire_node(anchor, subject):
    return dict(id='n1', type='Claim', title='run result', content='run passed',
                status='completed', aliases=[], identity=[], context_kind='work',
                retention='knowledge', provenance='direct', evidence=[dict(anchor_id=anchor)],
                assertion=dict(subject=dict(anchor_id=anchor, text=subject),
                               predicate=dict(anchor_id=anchor, text='passed'),
                               value=dict(anchor_id=anchor, text='PASS'), unit=[], conditions=[]))


def candidate(node):
    return dict(schema_version='sgv2-1', nodes=[node], relations=[], unresolved=[],
                coverage=dict(outcome='candidates_found'))


def boundary_case():
    # No whitespace before the subject: the hard split is exactly at char 480.
    text = 'x' * 474 + 'LIF+MLP passed PASS'
    batch = [dict(id='e1', text=text)]
    prepared, contract, anchors = prepare_evidence_spans(batch, policy([], effective=True, condense=True))
    wire = candidate(wire_node('a2', 'LIF+MLP'))
    return batch, prepared, contract, anchors, wire


def test_cross_boundary_literal_reanchors_without_mutating_text_or_wire():
    batch, prepared, contract, anchors, wire = boundary_case()
    before = copy.deepcopy((prepared, wire, dict(anchors)))
    check(wire, contract['json_schema'])
    decoded = resolve_evidence_spans(wire, anchors, entry_ids=['e1'], atomic_refs=True)
    node = decoded['nodes'][0]
    assert node['assertion'][0]['subject'] == 'LIF+MLP'
    assert node['evidence'] == [dict(entry_id='e1', quote='LIF+MLP passed PASS')]
    assert atomic_anchored(node)
    validate(decoded, batch, [])
    assert (prepared, wire, dict(anchors)) == before
    # No offsets or full source regions are transmitted to the model or persisted.
    assert all(set(s) == {'anchor_id', 'text'} for s in prepared[0]['source_spans'])
    assert all(set(e) == {'entry_id', 'quote'} for e in node['evidence'])


def test_extension_of_left_anchor_keeps_every_original_citation():
    batch, _, _, anchors, wire = boundary_case()
    node = wire['nodes'][0]
    node['evidence'].insert(0, dict(anchor_id='a1'))
    node['assertion']['subject']['anchor_id'] = 'a1'
    decoded = resolve_evidence_spans(wire, anchors, atomic_refs=True)
    refs = decoded['nodes'][0]['evidence']
    assert len(refs) == 2 and refs[1] == anchors['a2']
    assert refs[0]['quote'].startswith(anchors['a1']['quote'])
    assert refs[0]['quote'] == batch[0]['text'][:481]
    assert atomic_anchored(decoded['nodes'][0])


@pytest.mark.parametrize('field', ['subject', 'predicate', 'value', 'unit', 'conditions'])
def test_every_atomic_field_uses_the_same_boundary_guard(field):
    _, _, _, anchors, wire = boundary_case()
    atom = wire['nodes'][0]['assertion']
    atom['subject'] = dict(anchor_id='a2', text='P')
    ref = dict(anchor_id='a2', text='LIF+MLP')
    atom[field] = [ref] if field in {'unit', 'conditions'} else ref
    decoded = resolve_evidence_spans(wire, anchors, atomic_refs=True)
    assert 'LIF+MLP' in decoded['nodes'][0]['evidence'][0]['quote']


@pytest.mark.parametrize('defect', ['synonym', 'whitespace', 'case', 'uncited', 'legacy_metadata'])
def test_boundary_repair_still_rejects_invented_or_unaddressed_literals(defect):
    _, _, _, anchors, wire = boundary_case()
    node = wire['nodes'][0]
    if defect == 'synonym':
        node['assertion']['subject']['text'] = 'LIF and MLP'
    elif defect == 'whitespace':
        node['assertion']['subject']['text'] = 'LIF+ MLP'
    elif defect == 'case':
        node['assertion']['subject']['text'] = 'lif+MLP'
    elif defect == 'uncited':
        node['evidence'] = [dict(anchor_id='a1')]
    else:
        anchors = dict(anchors)
    with pytest.raises(ValueError, match='invalid_atomic_source_ref'):
        resolve_evidence_spans(wire, anchors, atomic_refs=True)


@pytest.mark.parametrize('barrier', ['fragment', 'redaction', 'message'])
def test_no_join_across_filtered_regions_redactions_or_messages(barrier):
    left, right = 'x' * 474 + 'LIF+ML', 'P passed PASS'
    if barrier == 'fragment':
        batch = [dict(id='e1', text=left + '\n' + right, evidence_fragments=[left, right])]
    elif barrier == 'redaction':
        batch = [dict(id='e1', text=left + '\u2588' + right)]
    else:
        batch = [dict(id='e1', text=left), dict(id='e2', text=right)]
    _, _, anchors = prepare_evidence_spans(batch, policy([], effective=True))
    wire = candidate(wire_node('a2', 'LIF+MLP'))
    with pytest.raises(ValueError, match='invalid_atomic_source_ref'):
        resolve_evidence_spans(wire, anchors, atomic_refs=True)


def test_complete_but_uncited_neighbor_does_not_grant_new_evidence():
    batch = [dict(id='e1', text='x' * 480 + 'LIF+MLP passed PASS')]
    _, _, anchors = prepare_evidence_spans(batch, policy([], effective=True))
    node = wire_node('a2', 'LIF+MLP')
    node['assertion']['subject'] = dict(anchor_id='a1', text='LIF+MLP')
    node['evidence'] = [dict(anchor_id='a1')]
    with pytest.raises(ValueError, match='invalid_atomic_source_ref'):
        resolve_evidence_spans(candidate(node), anchors, atomic_refs=True)


def test_ambiguous_boundary_occurrences_fail_closed():
    # The phrase overlaps both edges of the middle 480-character anchor.
    text = 'x' * 479 + 'AB' + 'x' * 478 + 'AB passed PASS'
    _, _, anchors = prepare_evidence_spans([dict(id='e1', text=text)], policy([], effective=True))
    node = wire_node('a2', 'AB')
    node['evidence'].append(dict(anchor_id='a3'))
    node['assertion']['predicate']['anchor_id'] = 'a3'
    node['assertion']['value']['anchor_id'] = 'a3'
    with pytest.raises(ValueError, match='invalid_atomic_source_ref'):
        resolve_evidence_spans(candidate(node), anchors, atomic_refs=True)


def test_boundary_quote_length_limit_is_not_relaxed():
    # Use the maximum value size to extend a 480-char quote beyond 600 chars.
    node = wire_node('a1', 'x')
    node['assertion']['predicate']['text'] = 'x'
    node['assertion']['value'] = dict(anchor_id='a1', text='A' * 200 + ' passed PASS')
    text = 'x' * 450 + 'A' * 200 + ' passed PASS'
    _, _, anchors = prepare_evidence_spans([dict(id='e1', text=text)], policy([], effective=True))
    with pytest.raises(ValueError, match='invalid_atomic_source_ref'):
        resolve_evidence_spans(candidate(node), anchors, atomic_refs=True)


def test_multiple_field_extensions_share_the_existing_quote_budget():
    text = 'x' * 400 + 'L' * 100 + 'x' * 380 + 'R' * 100
    _, _, anchors = prepare_evidence_spans([dict(id='e1', text=text)], policy([], effective=True))
    node = wire_node('a2', 'L' * 100)
    node['assertion']['predicate'] = dict(anchor_id='a2', text='R' * 100)
    node['assertion']['value'] = dict(anchor_id='a2', text='x')
    # Individually the expansions are 560 and 500 chars; their union is 580.
    decoded = resolve_evidence_spans(candidate(node), anchors, atomic_refs=True)
    assert decoded['nodes'][0]['evidence'][0]['quote'] == text[400:980]
    # A larger right extension makes the combined evidence exceed 600.
    text += 'R' * 40
    _, _, anchors = prepare_evidence_spans([dict(id='e1', text=text)], policy([], effective=True))
    node['assertion']['predicate']['text'] = 'x' + 'R' * 140
    with pytest.raises(ValueError, match='invalid_atomic_source_ref'):
        resolve_evidence_spans(candidate(node), anchors, atomic_refs=True)


@pytest.mark.parametrize('case_index', [0, 1])
def test_verified_fruit_fly_failed_response_without_a_model_or_store(case_index):
    path = Path(__file__).parent / 'fixtures/fruit_fly_atomic_boundary.json'
    case = json.loads(path.read_text())[case_index]
    batch, wire = case['batch'], case['output']
    assert hashlib.sha256(batch[3]['text'].encode()).hexdigest() == case['source_text_sha256']
    _, _, anchors = prepare_evidence_spans(batch, policy([], effective=True))
    # Old request maps have no continuity proof and must keep failing closed.
    with pytest.raises(ValueError, match='invalid_atomic_source_ref'):
        resolve_evidence_spans(wire, dict(anchors), atomic_refs=True)
    decoded = resolve_evidence_spans(wire, anchors, entry_ids=[e['id'] for e in batch], atomic_refs=True)
    validate(decoded, batch, [])
    node = decoded['nodes'][0]
    assert atomic_anchored(node)
    assert node['assertion'][0]['subject'] == wire['nodes'][0]['assertion']['subject']['text']
    assert all(e['quote'] in batch[3]['text'] for e in node['evidence'])
