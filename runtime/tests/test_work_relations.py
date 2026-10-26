"""Semantic edges require explicit source evidence; bad edges cannot lose memories."""
from sessiongraph.memories import run_chat
from sessiongraph.work_map import work_map
from test_memories import FakeExtractor, make_store
from test_mvp import write_session

TEXT = '部署任务依赖回归测试，只有回归测试通过后才能部署。'


def payload(eid):
    def item(title, kind):
        return {'kind': kind, 'title': title, 'content': title, 'status': 'proposed',
                'aliases': [], 'entry_id': eid, 'quote': TEXT}
    return {'memories': [item('部署任务', 'next_step'), item('回归测试', 'next_step')],
            'relations': [{'source': 'new:0', 'target': 'new:1', 'kind': 'DEPENDS_ON',
                           'entry_id': eid, 'quote': TEXT,
                           'source_mention': '部署任务', 'target_mention': '回归测试'}]}


def setup(tmp_path):
    _, store, _ = make_store(tmp_path, [('user', TEXT)])
    eid = store.db.execute('select id from entries').fetchone()[0]
    return store, payload(eid)


def test_extract_stores_semantic_edge_and_readonly_map_projects_it(tmp_path):
    store, output = setup(tmp_path)
    result = run_chat(store, FakeExtractor([output]), 'chat-1')
    assert not result['failed'] and len(result['saved']) == 2
    assert len(result['saved_relations']) == 1
    before = store.db.total_changes
    graph = work_map(store)
    edge = next(e for e in graph['edges'] if e['kind'] == 'DEPENDS_ON')
    assert edge['basis'] == 'extracted_claim' and edge['evidence'][0]['quote'] == TEXT
    assert edge['source'] != edge['target']
    assert graph['stats']['semantic_relations'] == 1 and graph['model_calls'] == 0
    assert store.db.total_changes == before
    recalled = store.recall('部署任务')
    assert recalled['model_calls'] == 0
    assert recalled['relations'][0]['id'] == edge['id']
    store.close()


def test_invalid_edge_is_rejected_but_valid_memories_still_saved(tmp_path):
    store, output = setup(tmp_path)
    output['relations'][0]['quote'] = '原文并没有的关系'
    result = run_chat(store, FakeExtractor([output]), 'chat-1')
    assert not result['failed'] and len(result['saved']) == 2
    assert not result['saved_relations'] and result['rejected_relations']
    assert work_map(store)['stats']['semantic_relations'] == 0
    store.close()


def test_foreign_endpoint_and_same_endpoint_rejected(tmp_path):
    store, output = setup(tmp_path)
    first = output['relations'][0]
    output['relations'] = [dict(first, target='wm_not_offered'),
                           dict(first, target='new:0'), dict(first, kind='CAUSES')]
    result = run_chat(store, FakeExtractor([output]), 'chat-1')
    assert not result['failed'] and len(result['saved']) == 2
    assert len(result['rejected_relations']) == 3 and not result['saved_relations']
    store.close()


def test_new_batch_can_reference_bounded_existing_project_memory(tmp_path):
    root, store, _ = make_store(tmp_path, [('user', '回归测试需要先完成')])
    eid = store.db.execute('select id from entries').fetchone()[0]
    old = store.remember(kind='next_step', title='回归测试', content='回归测试需要先完成',
                         source_entry_id=eid, source_quote='回归测试需要先完成')['id']
    source = write_session(root / 'another.jsonl', root, 'chat-2', [('user', TEXT)])
    store.ingest(source)
    new_eid = store.db.execute('select id from entries where text=?', (TEXT,)).fetchone()[0]
    out = payload(new_eid)
    out['memories'] = out['memories'][:1]
    out['relations'][0]['target'] = old
    ext = FakeExtractor([out])
    result = run_chat(store, ext, 'chat-2')
    assert not result['failed'] and len(result['saved_relations']) == 1
    offered = ext.calls[0][1]['existing_memories']
    assert old in {m['id'] for m in offered} and len(offered) <= 6
    assert work_map(store)['stats']['semantic_relations'] == 1
    # A scoped view must not leak nodes or relationship evidence from another chat.
    assert work_map(store, chat='chat-2')['stats']['semantic_relations'] == 0
    assert store.recall('部署任务', chat_id='chat-2')['relations'] == []
    store.close()


def test_legacy_memory_only_output_and_legacy_readonly_db_still_work(tmp_path):
    store, output = setup(tmp_path)
    del output['relations']
    result = run_chat(store, FakeExtractor([output]), 'chat-1')
    assert not result['failed'] and len(result['saved']) == 2
    store.db.execute('drop table work_memory_relations')
    store.db.commit()
    before = store.db.total_changes
    assert work_map(store)['stats']['semantic_relations'] == 0
    assert store.recall('部署任务')['relations'] == []
    assert store.db.total_changes == before
    store.close()


def test_evidence_changes_hide_edge_and_change_revision(tmp_path):
    store, output = setup(tmp_path)
    run_chat(store, FakeExtractor([output]), 'chat-1')
    before = work_map(store)
    assert before['stats']['semantic_relations'] == 1
    store.db.execute('update entries set text=?', ('已修改，不再匹配',))
    store.db.commit()
    after = work_map(store)
    assert after['stats']['semantic_relations'] == 0
    assert before['revision'] != after['revision']
    assert store.recall('部署任务')['relations'] == []
    store.close()


def test_cooccurrence_without_predicate_is_not_a_semantic_edge(tmp_path):
    store, output = setup(tmp_path)
    output['relations'][0]['source_mention'] = '没有的名称'
    result = run_chat(store, FakeExtractor([output]), 'chat-1')
    assert len(result['saved']) == 2 and result['rejected_relations'][0]['reason'] == 'unanchored_endpoint'
    store.close()


def test_relation_replay_dedup_and_revision_changes_for_edges_only(tmp_path):
    from sessiongraph.work_relations import save
    store, output = setup(tmp_path)
    ext = FakeExtractor([{'memories': output['memories']}])
    run_chat(store, ext, 'chat-1')
    before = work_map(store)['revision']
    ids = {r['title']: r['id'] for r in store.db.execute('select id,title from work_memory')}
    bindings = {'new:0': ids['部署任务'], 'new:1': ids['回归测试']}
    saved, rejected = save(store, output['relations'], bindings)
    assert len(saved) == 1 and not rejected and work_map(store)['revision'] != before
    assert save(store, output['relations'], bindings) == ([], [])
    # Keeping a raw stale edge does not make superseded/cross-project endpoints visible.
    store.db.execute('update work_memory set project_id=? where id=?', ('other', ids['回归测试']))
    store.db.commit()
    assert not store.recall('部署任务')['relations']
    assert work_map(store)['stats']['semantic_relations'] == 0
    store.close()


def test_missing_or_malformed_relations_does_not_change_memory_success(tmp_path):
    store, output = setup(tmp_path)
    output['relations'] = 'bad shape'
    result = run_chat(store, FakeExtractor([output]), 'chat-1')
    assert len(result['saved']) == 2 and not result['failed']
    assert result['rejected_relations'] == [{'reason': 'invalid_relations_array'}]
    store.close()


def test_explicit_negation_is_not_stored_as_positive_dependency(tmp_path):
    text = '部署任务不依赖回归测试，这两项工作可独立进行。'
    _, store, _ = make_store(tmp_path, [('user', text)])
    eid = store.db.execute('select id from entries').fetchone()[0]
    out = payload(eid)
    for m in out['memories']:
        m['quote'] = text
    out['relations'][0]['quote'] = text
    result = run_chat(store, FakeExtractor([out]), 'chat-1')
    assert not result['saved_relations']
    assert len(result['saved']) == 2 and not result['failed']
    store.close()


def test_bare_cooccurrence_rejects_inferred_dependency(tmp_path):
    text = '部署任务和回归测试是两项工作。'
    _, store, _ = make_store(tmp_path, [('user', text)])
    eid = store.db.execute('select id from entries').fetchone()[0]
    out = payload(eid)
    for m in out['memories']:
        m['quote'] = text
    out['relations'][0]['quote'] = text
    result = run_chat(store, FakeExtractor([out]), 'chat-1')
    assert result['rejected_relations'] == [{'index': 0, 'reason': 'missing_explicit_predicate'}]
    assert not result['saved_relations'] and len(result['saved']) == 2
    store.close()


def test_bad_legacy_aliases_do_not_break_normal_extraction(tmp_path):
    root, store, _ = make_store(tmp_path, [('user', '回归测试需要先完成')])
    eid = store.db.execute('select id from entries').fetchone()[0]
    store.remember(kind='next_step', title='回归测试', content='回归测试需要先完成',
                   source_entry_id=eid, source_quote='回归测试需要先完成')
    store.db.execute("update work_memory set aliases_json='{broken'")
    store.db.commit()
    path = write_session(root / 'b.jsonl', root, 'chat-b', [('user', '回归测试仍在进行')])
    store.ingest(path)
    ext = FakeExtractor([{'memories': []}])
    result = run_chat(store, ext, 'chat-b')
    assert not result['failed'] and result['model_calls'] == 1
    store.close()
