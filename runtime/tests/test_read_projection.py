"""Unified write/read contracts: deterministic output, invalidation, no model calls."""
import importlib.util
import json
from pathlib import Path

from test_work_map import setup_map
from sessiongraph.work_map import work_map
from sessiongraph.core import Store


def legacy(store, **kwargs):
    spec = importlib.util.spec_from_file_location('sessiongraph._map_oracle', Path(__file__).parent/'fixtures/work_map_before_projection.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.work_map(store, **kwargs)


def assert_legacy_equivalent(store):
    """Keep the old projection contract intact while allowing additive view metadata."""
    actual = work_map(store)
    expected = legacy(store)
    assert set(actual) == set(expected) | {"view", "tiering"}
    assert actual["view"] == "all" and actual["page"]["view"] == "all"
    # The tier summary is additive: old read clients still receive the original
    # five fields unchanged, while newer clients can detect coverage/version
    # completeness. An unavailable projection must never claim coverage.
    assert {key: actual["tiering"][key] for key in
            ("available", "revision", "canonical", "episode", "record_only")} == {
                "available": False,
                "revision": "",
                "canonical": 0,
                "episode": 0,
                "record_only": 0,
            }
    assert actual["tiering"]["complete"] is False
    assert actual["tiering"]["current"] is False
    actual_legacy = {key: value for key, value in actual.items()
                     if key not in {"view", "tiering"}}
    actual_legacy["page"] = {key: value for key, value in actual["page"].items()
                             if key != "view"}
    assert actual_legacy == expected


def test_projection_matches_legacy_and_read_does_not_write(tmp_path):
    from sessiongraph.read_projection import flush
    store, root, add = setup_map(tmp_path)
    add('a', 'API 实验 reports/a.md 和 API-v2', 'API 实验', aliases=['API', 'API-v2'])
    add('b', 'API 实验 reports/a.md', 'API 复验', aliases=['实验'])
    add('c', 'capital unrelated API-v2', '其他', aliases=['unrelated'])
    flush(store)
    expected = legacy(store)
    store.close()
    ro = Store(store.config, readonly=True)
    changes = ro.db.total_changes
    try:
        assert_legacy_equivalent(ro)
        assert ro.db.total_changes == changes
    finally:
        ro.close()


def test_dirty_old_writer_and_changed_evidence_never_return_stale_projection(tmp_path):
    from sessiongraph.read_projection import flush
    store, root, add = setup_map(tmp_path)
    first = add('a', 'shared unique', 'shared', aliases=['shared'])
    add('b', 'shared unique', 'shared', aliases=['shared'])
    flush(store)
    eid = store.db.execute('SELECT source_entry_id FROM work_memory WHERE id=?', (first,)).fetchone()[0]
    store.db.execute("UPDATE entries SET text='changed' WHERE id=?", (eid,))
    store.db.commit()
    # Existing processes may commit without calling new Python code.
    assert_legacy_equivalent(store)
    assert work_map(store)['stats']['invalid_sources'] > 0
    flush(store)
    assert_legacy_equivalent(store)
    store.close()


def test_new_alias_finds_old_mentions_and_removal_matches_full_rebuild(tmp_path):
    from sessiongraph.read_projection import flush, rebuild
    store, root, add = setup_map(tmp_path)
    add('a', 'OldTerm 在历史中存在', 'OldTerm 历史')
    flush(store)
    new = add('b', 'OldTerm 新工作', 'OldTerm 新工作', aliases=['OldTerm'])
    flush(store)
    assert_legacy_equivalent(store)
    assert work_map(store)['stats']['reference_relations'] == 2
    store.db.execute("UPDATE work_memory SET aliases_json='[]' WHERE id=?", (new,))
    store.db.commit(); flush(store)
    before = work_map(store)
    rebuild(store)
    assert work_map(store) == before
    assert_legacy_equivalent(store)
    store.close()


def test_matcher_preserves_overlap_unicode_and_ascii_boundaries():
    from sessiongraph.read_projection import Matcher
    from sessiongraph.work_map import _has
    terms = ['api', 'api-v2', '实验', '验结', 'σxx', 'ixx', 'ßxx', 'sxx', 'kxx']
    matcher = Matcher(terms)
    for text in ['capital API-v2 实验结果', 'İxx ıxx Σxx ςxx ẞxx ſxx Kxx', 'api_api api/v2', '实验实验']:
        assert matcher.matches(text) == {t for t in terms if _has(text,t,{})}


def test_repeated_clean_flush_is_noop(tmp_path):
    from sessiongraph.read_projection import flush
    store, root, add = setup_map(tmp_path)
    add('a', 'result saved at reports/a.md', 'result', aliases=['result'])
    flush(store)
    before = store.db.total_changes
    flush(store)
    assert store.db.total_changes == before
    store.close()


def test_fact_and_projection_rollback_together(tmp_path):
    import pytest
    from sessiongraph.read_projection import flush
    store,root,add=setup_map(tmp_path)
    mid=add('a','original result','original',aliases=['original'])
    before=work_map(store)
    with pytest.raises(RuntimeError):
        with store.db:
            store.db.execute("UPDATE work_memory SET aliases_json='[]' WHERE id=?",(mid,))
            flush(store)
            assert store.db.in_transaction
            raise RuntimeError('crash before publication')
    assert work_map(store)==before
    store.close()


def test_unicode_candidates_and_bloom_never_drop_literal_matches():
    from sessiongraph.read_projection import Matcher,bloom,possible
    from sessiongraph.work_map import _has
    import random
    rng=random.Random(17)
    alphabet='aAbBİıſsSKkKσΣςßẞ实验证据_ -/'
    terms=[''.join(rng.choices(alphabet,k=3)) for _ in range(70)]
    matcher=Matcher(terms)
    for _ in range(70):
        text=''.join(rng.choices(alphabet,k=160))
        expected={term for term in terms if _has(text,term,{})}
        assert matcher.matches(text)==expected
        assert all(possible(bloom(text),term) for term in expected)

def test_evidence_mutation_invalidates_projection_even_without_python_writer(tmp_path):
    from sessiongraph.read_projection import flush
    from sessiongraph import graph_v2
    from test_memories import make_store, entries
    from test_graph_v2 import Judge, ContextExtractor, output
    _, store, _ = make_store(tmp_path, [('assistant', '产物 /gpu3/kongsz/microduck-fly/protocol.json 通过校验')])
    batch = entries(store)
    graph_v2.run_chat(store, ContextExtractor([output(batch, quote=batch[0]['text'])]), 'chat-1', jev=Judge())
    node = store.db.execute('select node from sg2_evidence').fetchone()[0]
    store.db.execute('update work_memory set source_quote=? where id=?', ('无路径', node))
    store.db.execute('update sg2_evidence set quote=? where node=?', ('无路径', node))
    store.db.commit()
    assert store.db.execute('select count(*) from sg_read_dirty').fetchone()[0] == 1
    flush(store)
    paths = json.loads(store.db.execute('select paths_json from sg_read_nodes where id=?', (node,)).fetchone()[0])
    assert paths == []
    store.close()


def test_evidence_only_mutation_invalidates_projection(tmp_path):
    from sessiongraph.read_projection import flush
    from sessiongraph import graph_v2
    from test_memories import make_store, entries
    from test_graph_v2 import Judge, ContextExtractor, output
    _, store, _ = make_store(tmp_path, [('assistant', '产物 /gpu3/kongsz/microduck-fly/protocol.json 通过校验')])
    batch = entries(store)
    graph_v2.run_chat(store, ContextExtractor([output(batch, quote=batch[0]['text'])]), 'chat-1', jev=Judge())
    node = store.db.execute('select node from sg2_evidence').fetchone()[0]
    # Make the representative quote independent of the path-bearing evidence so
    # this test cannot pass through the work_memory validity gate.
    store.db.execute('update work_memory set source_quote=? where id=?', ('无路径', node))
    store.db.commit()
    flush(store)
    assert store.db.execute('select count(*) from sg_read_dirty').fetchone()[0] == 0
    # This writer touches neither work_memory nor entries. Only the evidence
    # trigger can invalidate the cached projection before the next read.
    store.db.execute('update sg2_evidence set quote=? where node=?', ('无路径', node))
    store.db.commit()
    assert store.db.execute('select count(*) from sg_read_dirty').fetchone()[0] == 1
    flush(store)
    paths = json.loads(store.db.execute('select paths_json from sg_read_nodes where id=?', (node,)).fetchone()[0])
    assert paths == []
    store.close()
