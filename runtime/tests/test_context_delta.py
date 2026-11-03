"""Current input owns writes; old context remains available without being replayed."""
import copy
import json
import pytest
from sessiongraph import graph_v2 as graph
from test_graph_v2 import Judge, ContextExtractor, output
from test_memories import make_store, entries, FakeExtractor
from test_mvp import write_session


def projection(candidate, batch, offered=()):
    stats = graph._stats()
    result, blocked = graph._delta_output(candidate, batch, offered, stats)
    return result, blocked, stats


def test_background_projection_is_readonly_and_needs_no_model():
    batch = [dict(id='old', text='old fact', context_only=True), dict(id='new', text='continue')]
    original = output(batch)
    original['unresolved'] = [dict(mention='old', reason='insufficient_context', evidence=original['nodes'][0]['evidence'])]
    before = copy.deepcopy(original)
    result, blocked, stats = projection(original, batch)
    assert original == before
    assert result['nodes'] == result['relations'] == result['unresolved'] == []
    assert result['coverage']['outcome'] == 'no_durable_item'
    assert blocked == {'n1', 'n2'}
    assert stats['context_nodes_skipped'] == 2 and stats['context_relations_skipped'] == 1
    assert stats['context_unresolved_skipped'] == 1
    assert stats['deepseek_calls'] == stats['jev_calls'] == 0


def test_current_fact_with_background_scope_and_conditions_is_not_dropped():
    batch = [dict(id='old', text='run and scope', context_only=True), dict(id='new', text='new result')]
    candidate = output(batch)
    candidate['nodes'][0]['evidence'].append(dict(entry_id='new', quote='new result'))
    result, blocked, stats = projection(candidate, batch)
    assert result['nodes'] == [candidate['nodes'][0]]
    assert result['nodes'][0]['evidence'] == candidate['nodes'][0]['evidence']
    assert stats['context_nodes_skipped'] == 1 and blocked == {'n2'}


def test_current_relation_can_reference_existing_background_without_rewriting_it():
    batch = [dict(id='old', text='run output', context_only=True), dict(id='new', text='confirmed output')]
    candidate = output(batch)
    offered = [dict(n, id='stored'+n['id']) for n in candidate['nodes']]
    candidate['relations'][0]['evidence'].append(dict(entry_id='new', quote='confirmed output'))
    result, blocked, _ = projection(candidate, batch, offered)
    assert result['nodes'] == [] and not blocked
    assert result['relations'][0]['source'] == 'storedn1'
    assert result['relations'][0]['target'] == 'storedn2'


@pytest.mark.parametrize('change', ['value', 'conditions', 'status', 'provenance'])
def test_atomic_lane_alone_does_not_bind_old_background(change):
    batch = [dict(id='old', text='run output', context_only=True), dict(id='new', text='new input')]
    candidate = output(batch)
    n = candidate['nodes'][0]
    n.update(type='Claim', assertion=[dict(subject='run', predicate='score', value='1', unit='none', conditions=[])], provenance='direct')
    offered = copy.deepcopy(n); offered['id'] = 'stored'
    if change in {'value', 'conditions'}:
        offered['assertion'][0][change] = '2' if change == 'value' else ['validation']
    else:
        offered[change] = 'active' if change == 'status' else 'derived'
    result, blocked, _ = projection(candidate, batch, [offered])
    assert 'n1' in blocked and not result['nodes']


def test_background_same_run_can_be_endpoint_but_distinct_seed_cannot():
    batch = [dict(id='old', text='project runA runB', context_only=True), dict(id='new', text='verified')]
    candidate = output(batch)
    candidate['nodes'][0]['identity'] = [dict(namespace='run_id', scope='project', value='runA')]
    offered = copy.deepcopy(candidate['nodes'][0]);offered.update(id='stored', content='another description')
    candidate['relations'][0]['evidence'].append(dict(entry_id='new', quote='verified'))
    result, blocked, _ = projection(candidate, batch, [offered])
    assert result['relations'][0]['source'] == 'stored' and 'n1' not in blocked
    offered['identity'][0]['value'] = 'runB'
    _, blocked, _ = projection(candidate, batch, [offered])
    assert 'n1' in blocked


def test_no_context_preserves_behavior():
    batch = [dict(id='new', text='run output')]
    candidate = output(batch)
    result, blocked, stats = projection(candidate, batch)
    assert result is candidate and not blocked and not stats['context_nodes_skipped']


def test_repeated_background_does_not_grow_nodes_pending_or_model_judgments(tmp_path):
    root, store, _ = make_store(tmp_path, [('assistant', '训练 A 得到模型 B')])
    original = entries(store)
    first = graph.run_chat(store, FakeExtractor([output(original)]), 'chat-1', jev=Judge())
    assert len(first['saved']) == 2
    before = {t: store.db.execute('SELECT count(*) FROM '+t).fetchone()[0]
              for t in ('sg2_nodes', 'sg2_evidence', 'sg2_unresolved', 'sg2_relations')}
    for i in range(3):
        chat = f'chat-{i+2}'
        store.ingest(write_session(root/f'next-{i}.jsonl', root, chat, [('assistant', '训练 A 模型 B 继续检查')]))
        judge = Judge(identity='same')
        result = graph.run_chat(store, ContextExtractor([output(original)]), chat, jev=judge)
        assert not result['failed'] and not result['saved']
        assert result['graph_v2']['context_nodes_skipped'] == 2, repr(result)
        assert judge.calls == 0
        job = store.db.execute('SELECT output_json,state FROM sg2_jobs WHERE chat=?', (chat,)).fetchone()
        assert len(json.loads(job[0])['nodes']) == 2 and job[1] == 'complete'
    after = {t: store.db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in before}
    assert before == after
    # All current input was consumed; no retry or extraction call on no-op resume.
    result = graph.run_chat(store, FakeExtractor([]), 'chat-4', jev=Judge())
    assert result['model_calls'] == 0
    store.close()


def test_publication_guard_blocks_old_checkpoint_bypass(tmp_path):
    _, store, _ = make_store(tmp_path, [('assistant', '训练 A 得到模型 B')])
    graph.setup(store)
    batch = entries(store); candidate = output(batch)
    desc = dict(entries=[dict(id=batch[0]['id'], context_only=True)])
    store.db.execute('INSERT INTO sg2_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                     ('old-job',store.config['project_id'],'chat-1','chat-1','judging',json.dumps(desc),None,'[]','{}',None,'',''))
    store.db.commit()
    with pytest.raises(ValueError, match='context_only_publication'):
        graph._commit(store, dict(id='old-job', chat='chat-1'), candidate, [],
                      dict(n1='new1', n2='new2'), [], [], [], graph._stats(), (99,0))
    assert store.db.execute('SELECT count(*) FROM sg2_nodes').fetchone()[0] == 0
    store.close()
