"""Citations cannot cross gaps removed by input filtering, even during local repair."""
import copy
import pytest
from sessiongraph.graph_contract import (
    VERSION, MAX_EVIDENCE, anchor_identity_evidence, identity_is_anchored,
    restore_source_quotes, validate,
)


def fixture(parts):
    batch = [{'id': 'e1', 'text': '\n'.join(parts), 'evidence_fragments': parts}]
    node = dict(id='n1', type='Execution', title='训练', content='执行训练',
                status='completed', aliases=[], identity=[],
                evidence=[{'entry_id': 'e1', 'quote': parts[0]}])
    out = dict(schema_version=VERSION, nodes=[node], relations=[], unresolved=[],
               coverage=dict(entry_ids=['e1'], outcome='candidates_found'))
    return batch, out


def test_identity_expansion_uses_separate_original_fragments():
    parts = ['执行训练 R17', '项目 Alpha']
    batch, out = fixture(parts)
    out['nodes'][0]['identity'] = [dict(namespace='run_id', value='R17', scope='项目 Alpha')]
    before = copy.deepcopy(out)
    fixed, count = anchor_identity_evidence(out, batch)
    assert out == before
    assert count == 1
    evidence = fixed['nodes'][0]['evidence']
    assert len(evidence) == 2
    assert all(any(e['quote'] in p for p in parts) for e in evidence)
    validate(fixed, batch, [])


@pytest.mark.parametrize('group', ['nodes', 'relations', 'unresolved'])
def test_validate_rejects_quote_spanning_filtered_gap(group):
    batch, out = fixture(['执行训练 R17', '项目 Alpha'])
    ev = dict(entry_id='e1', quote=batch[0]['text'])
    if group == 'nodes':
        out['nodes'][0]['evidence'] = [ev]
    elif group == 'relations':
        out['nodes'].append({**copy.deepcopy(out['nodes'][0]), 'id': 'n2'})
        out['relations'] = [dict(source='n1', target='n2', kind='DEPENDS_ON',
                                statement_mode='asserted', scope='项目', evidence=[ev])]
    else:
        out['unresolved'] = [dict(mention='待确认', reason='insufficient_context', evidence=[ev])]
    with pytest.raises(ValueError, match='quote_crosses_fragment'):
        validate(out, batch, [], defer_unanchored_identities=True)


def test_format_repair_cannot_join_fragments():
    batch, out = fixture(['**执行训练 R17**', '**项目 Alpha**'])
    out['nodes'][0]['evidence'][0]['quote'] = '执行训练 R17\n项目 Alpha'
    fixed, count = restore_source_quotes(out, batch)
    assert count == 0 and fixed == out
    with pytest.raises(ValueError, match='quote_not_located'):
        validate(fixed, batch, [])


def test_format_repair_still_restores_one_fragment_and_rejects_ambiguity():
    batch, out = fixture(['**执行训练 R17**', '项目 Alpha'])
    out['nodes'][0]['evidence'][0]['quote'] = '执行训练 R17'
    fixed, count = restore_source_quotes(out, batch)
    # This is already a literal substring; no repair is needed.
    assert count == 0
    out['nodes'][0]['evidence'][0]['quote'] = '执行训练 R17 后结束'
    batch, _ = fixture(['**执行训练 R17** 后结束', '项目 Alpha'])
    fixed, count = restore_source_quotes(out, batch)
    assert count == 1
    validate(fixed, batch, [])
    batch[0]['evidence_fragments'].append('**执行训练 R17** 后结束')
    fixed, count = restore_source_quotes(out, batch)
    assert count == 0


def test_identity_never_matches_synthetic_quote_join():
    batch, out = fixture(['R17', 'Alpha'])
    node = out['nodes'][0]
    node['evidence'].append(dict(entry_id='e1', quote='Alpha'))
    node['identity'] = [dict(namespace='run_id', value='R17\nAlpha', scope='R17')]
    assert not identity_is_anchored(node)
    fixed, count = anchor_identity_evidence(out, batch)
    assert fixed == out and count == 0
    with pytest.raises(ValueError, match='unanchored_identity'):
        validate(fixed, batch, [])


@pytest.mark.parametrize('scenario', ['full', 'foreign', 'fabricated', 'redacted', 'missing'])
def test_repair_fails_closed_without_discarding_evidence(scenario):
    batch, out = fixture(['执行训练 R17', '项目 Alpha'])
    node = out['nodes'][0]
    node['identity'] = [dict(namespace='run_id', value='R17', scope='项目 Alpha')]
    if scenario == 'full':
        node['evidence'] *= MAX_EVIDENCE
    elif scenario == 'foreign':
        batch[0]['evidence_fragments'] = ['执行训练 R17']
        batch.append(dict(id='e2', text='项目 Alpha', evidence_fragments=['项目 Alpha']))
    elif scenario == 'fabricated':
        node['evidence'][0]['quote'] = '不存在'
    elif scenario == 'redacted':
        node['identity'][0]['scope'] = '项目 █ Alpha'
        batch[0]['evidence_fragments'][1] = '项目 █ Alpha'
    else:
        batch[0]['evidence_fragments'] = []
    fixed, count = anchor_identity_evidence(out, batch)
    assert fixed == out and count == 0


def test_fragmented_source_pipeline_publishes_and_replays_without_calls(tmp_path, monkeypatch):
    from sessiongraph import input_pipeline
    from sessiongraph.graph_v2 import run_chat
    from test_memories import make_store, FakeExtractor
    from test_graph_v2 import output, Judge
    first, gap, last = '执行训练 R17，产出模型 B', ' OMITTED-NOISE ', '项目 Alpha'
    original = first + gap + last
    _, store, _ = make_store(tmp_path, [('user', original)])
    monkeypatch.setattr(input_pipeline, 'projection',
                        lambda s, row: ([[0, len(first)], [len(first + gap), len(original)]], 'fixture_filter'))
    def extractor(batch, policy):
        candidate = copy.deepcopy(output(batch, quote=first))
        candidate['nodes'][0]['identity'] = [dict(namespace='run_id', value='R17', scope=last)]
        return candidate
    try:
        result = run_chat(store, extractor, 'chat-1', jev=Judge())
        assert not result['failed']
        assert len(result['saved']) == 2 and len(result['saved_relations']) == 1
        quotes = [r[0] for r in store.db.execute('SELECT quote FROM sg2_evidence')]
        assert last in quotes and all(q in original for q in quotes)
        replay = run_chat(store, FakeExtractor([]), 'chat-1', jev=Judge())
        assert replay['model_calls'] == 0
    finally:
        store.close()


@pytest.mark.parametrize('bad_group', ['nodes', 'relations'])
def test_commit_rechecks_every_quote_before_any_write(tmp_path, bad_group):
    from sessiongraph.graph_v2 import _commit, _stats, setup
    from test_memories import make_store, entries
    from test_graph_v2 import output
    _, store, _ = make_store(tmp_path, [('user', '训练 R17 产出模型 B')])
    setup(store)
    batch = entries(store)
    candidate = copy.deepcopy(output(batch))
    candidate[bad_group][0]['evidence'].append(dict(entry_id=batch[0]['id'], quote='伪造的第二条引用'))
    job = dict(id='fixture-job', chat='chat-1', segment=store.db.execute('SELECT session_id FROM entries LIMIT 1').fetchone()[0])
    try:
        with pytest.raises(ValueError, match='source_changed'):
            _commit(store, job, candidate, [], {'n1': 'node1', 'n2': 'node2'}, [], [], [], _stats(), [2, 0], finalize=False)
        assert store.db.execute('SELECT count(*) FROM sg2_nodes').fetchone()[0] == 0
        assert store.db.execute('SELECT count(*) FROM sg2_evidence').fetchone()[0] == 0
    finally:
        store.close()


def test_identity_can_add_distant_scope_without_overlong_quote():
    text = '执行训练 R17' + '。' * 650 + '项目 Alpha'
    batch, out = fixture([text])
    out['nodes'][0]['evidence'][0]['quote'] = '执行训练 R17'
    out['nodes'][0]['identity'] = [dict(namespace='run_id', value='R17', scope='项目 Alpha')]
    fixed, count = anchor_identity_evidence(out, batch)
    assert count == 1 and len(fixed['nodes'][0]['evidence']) == 2
    validate(fixed, batch, [])


def test_identity_repair_is_atomic_when_scope_is_absent():
    batch, out = fixture(['执行训练，稍后运行 R17'])
    out['nodes'][0]['evidence'][0]['quote'] = '执行训练'
    out['nodes'][0]['identity'] = [dict(namespace='run_id', value='R17', scope='不存在的项目')]
    fixed, count = anchor_identity_evidence(out, batch)
    assert count == 0 and fixed == out


def test_explicit_empty_fragments_are_not_treated_as_full_text():
    batch, out = fixture(['原文仍在但没有可引用片段'])
    batch[0]['evidence_fragments'] = []
    with pytest.raises(ValueError, match='quote_crosses_fragment'):
        validate(out, batch, [])


def atomic_wire():
    from sessiongraph.graph_contract import prepare_evidence_spans, policy
    batch, out = fixture(['runA success 192万帧 heldout', 'runB failed'])
    node = out['nodes'][0]
    node.update(type='Claim', context_kind='work', provenance='direct', assertion=dict(
        subject=dict(anchor_id='a1', text='runA'), predicate=dict(anchor_id='a1', text='success'),
        value=dict(anchor_id='a1', text='192万'), unit=[dict(anchor_id='a1', text='帧')],
        conditions=[dict(anchor_id='a1', text='heldout')]))
    node['evidence'] = [dict(anchor_id='a1')]
    out['coverage'].pop('entry_ids')
    _, contract, anchors = prepare_evidence_spans(batch, policy([], effective=True))
    return batch, out, contract, anchors


def test_atomic_wire_decodes_only_exact_cited_literals_without_changing_persistence():
    from sessiongraph.graph_contract import resolve_evidence_spans, prompt_policy, policy, check
    batch, wire, contract, anchors = atomic_wire()
    before = copy.deepcopy(wire)
    check(wire, contract['json_schema'])
    result = resolve_evidence_spans(wire, anchors, entry_ids=['e1'], atomic_refs=True)
    atom = result['nodes'][0]['assertion'][0]
    assert atom == dict(subject='runA', predicate='success', value='192万', unit='帧', conditions=['heldout'])
    validate(result, batch, [])
    assert wire == before
    prompt = prompt_policy(contract)
    assert 'EXACT contiguous substring' in prompt['instructions']
    assert isinstance(prompt['output_contract']['nodes']['items']['assertion']['one_of'][0]['predicate'], dict)
    assert policy([], effective=True)['json_schema']['properties']['nodes']['items']['properties']['assertion']['items']['properties']['predicate']['type'] == 'string'
    wire['nodes'][0]['assertion']['unit'] = []
    assert resolve_evidence_spans(wire, anchors, atomic_refs=True)['nodes'][0]['assertion'][0]['unit'] == 'none'


@pytest.mark.parametrize('bad', ['synonym', 'uncited', 'foreign', 'number', 'redacted', 'string', 'extra', 'unit', 'condition', 'gap'])
def test_atomic_wire_rejects_fabricated_or_unreferenced_fields(bad):
    from sessiongraph.graph_contract import resolve_evidence_spans
    _, wire, _, anchors = atomic_wire()
    atom = wire['nodes'][0]['assertion']
    if bad == 'synonym': atom['predicate']['text'] = 'succeeded'
    elif bad == 'uncited': atom['subject'] = dict(anchor_id='a2', text='runB')
    elif bad == 'foreign': atom['subject']['anchor_id'] = 'a_foreign'
    elif bad == 'number': atom['value']['text'] = '1920000'
    elif bad == 'redacted': atom['subject']['text'] = '█'
    elif bad == 'string': atom['predicate'] = 'success'
    elif bad == 'extra': atom['predicate']['label'] = 'invented'
    elif bad == 'unit': atom['unit'] = 'none'
    elif bad == 'condition': atom['conditions'][0]['text'] = 'training'
    else: atom['subject']['text'] = 'runA\nrunB'
    with pytest.raises(ValueError, match='invalid_atomic_source_ref'):
        resolve_evidence_spans(wire, anchors, atomic_refs=True)


def test_atomic_fields_can_reference_separate_cited_fragments():
    from sessiongraph.graph_contract import resolve_evidence_spans
    batch, wire, _, anchors = atomic_wire()
    wire['nodes'][0]['evidence'].append(dict(anchor_id='a2'))
    wire['nodes'][0]['assertion']['conditions'].append(dict(anchor_id='a2', text='runB'))
    result = resolve_evidence_spans(wire, anchors, entry_ids=['e1'], atomic_refs=True)
    assert result['nodes'][0]['assertion'][0]['conditions'] == ['heldout', 'runB']
    validate(result, batch, [])


def test_atomic_wire_rejects_arrays_and_decodes_null_without_guessing_a_split():
    from sessiongraph.graph_contract import resolve_evidence_spans
    _, wire, _, anchors = atomic_wire()
    wire['nodes'][0]['assertion'] = [wire['nodes'][0]['assertion']] * 2
    with pytest.raises(ValueError, match='expected_one_object_or_null'):
        resolve_evidence_spans(wire, anchors, atomic_refs=True)
    wire['nodes'][0]['assertion'] = None
    assert resolve_evidence_spans(wire, anchors, atomic_refs=True)['nodes'][0]['assertion'] == []
