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
        assert work_map(ro) == expected
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
    assert work_map(store) == legacy(store)
    assert work_map(store)['stats']['invalid_sources'] > 0
    flush(store)
    assert work_map(store) == legacy(store)
    store.close()


def test_new_alias_finds_old_mentions_and_removal_matches_full_rebuild(tmp_path):
    from sessiongraph.read_projection import flush, rebuild
    store, root, add = setup_map(tmp_path)
    add('a', 'OldTerm 在历史中存在', 'OldTerm 历史')
    flush(store)
    new = add('b', 'OldTerm 新工作', 'OldTerm 新工作', aliases=['OldTerm'])
    flush(store)
    assert work_map(store) == legacy(store)
    assert work_map(store)['stats']['reference_relations'] == 2
    store.db.execute("UPDATE work_memory SET aliases_json='[]' WHERE id=?", (new,))
    store.db.commit(); flush(store)
    before = work_map(store)
    rebuild(store)
    assert work_map(store) == before == legacy(store)
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
