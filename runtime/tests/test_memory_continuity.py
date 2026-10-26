import pytest
from sessiongraph.core import Store, init_project, load_config
from test_mvp import write_session


@pytest.fixture
def memory_store(tmp_path):
    cfg = init_project(tmp_path)
    store = Store(load_config(cfg))
    source = write_session(tmp_path/'chat.jsonl', tmp_path, 'chat', [
        ('user', '采用 delta 方法处理 sigma；下一步核对数据。'),
        ('user', 'delta 只是背景术语。'),
        ('user', '改为 theta 方法处理 sigma，替代 delta 方案。'),
    ])
    store.ingest(source)
    entries = [dict(r) for r in store.db.execute('SELECT * FROM entries ORDER BY line')]
    yield store, entries
    store.close()


def save(store, entry, **extra):
    return store.remember(kind='decision', title='方法', content=entry['text'],
                          source_entry_id=entry['id'], source_quote=entry['text'], **extra)


def test_recall_best_match_first(memory_store):
    store, rows = memory_store
    strong = save(store, rows[0])
    save(store, rows[1])
    assert store.recall('delta sigma', limit=1)['memories'][0]['id'] == strong['id']


def test_memory_ids_cannot_overwrite_old_evidence(memory_store):
    store, rows = memory_store
    original = save(store, rows[0], memory_id='chosen')
    with pytest.raises(ValueError, match='immutable'):
        save(store, rows[2], memory_id='chosen')
    restored = store.resume()['memories'][0]
    assert restored['content'] == original['content']
    assert restored['source']['quote'] == rows[0]['text']


def test_identical_retry_does_not_reactivate_superseded(memory_store):
    store, rows = memory_store
    old = save(store, rows[0])
    new = save(store, rows[2], supersedes_id=old['id'])
    retry = save(store, rows[0])
    assert retry['status'] == 'superseded'
    assert [m['id'] for m in store.resume()['memories']] == [new['id']]


def test_no_competing_successor_or_self_replacement(memory_store):
    store, rows = memory_store
    old = save(store, rows[0])
    with pytest.raises(ValueError, match='itself'):
        save(store, rows[0], memory_id=old['id'], supersedes_id=old['id'])
    save(store, rows[2], supersedes_id=old['id'])
    with pytest.raises(ValueError, match='already superseded'):
        save(store, rows[1], supersedes_id=old['id'])


def test_completed_progress_is_restored_by_default(memory_store):
    store, rows = memory_store
    item = store.remember(kind='progress', title='已完成工作', content=rows[0]['text'],
                          status='completed', source_entry_id=rows[0]['id'], source_quote=rows[0]['text'])
    assert item['id'] in {m['id'] for m in store.resume()['memories']}
    assert not store.resume(include_completed=False)['memories']


def test_memory_requires_evidence_and_path_must_occur_in_quote(memory_store):
    store, rows = memory_store
    with pytest.raises(ValueError, match='source_entry_id'):
        store.remember(kind='finding',title='x',content='unsupported')
    with pytest.raises(ValueError, match='path'):
        save(store, rows[0], paths=['/invented/report.md'])


def test_provenance_is_quote_check_not_truth_certificate(memory_store):
    store, rows = memory_store
    item = save(store, rows[0])
    assert item['source']['start'] == 0
    assert item['source']['end'] == len(rows[0]['text'])
    assert item['verification'] == 'quote_match_only'
    assert 'not' in store.resume()['note'].lower()


def test_readonly_memory_never_writes(memory_store):
    store, rows = memory_store
    save(store, rows[0])
    reader = Store(store.config, readonly=True)
    try:
        assert reader.recall('delta')['memories']
        assert reader.resume()['memories']
        with pytest.raises(ValueError, match='read-only'):
            save(reader, rows[1])
        assert reader.db.total_changes == 0
    finally:
        reader.close()


def test_legacy_readonly_index_still_supports_history(memory_store):
    store, rows = memory_store
    with store.db:
        store.db.execute('DROP TABLE work_memory')
    reader = Store(store.config, readonly=True)
    try:
        assert reader.status()['work_memory']['available'] is False
        assert reader.search('delta', mode='raw')['hits']
        assert reader.resume()['available'] is False
        assert reader.recall('delta')['available'] is False
        assert reader.db.total_changes == 0
    finally:
        reader.close()
