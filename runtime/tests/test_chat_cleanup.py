"""Real SQLite publication/cleanup and Hook routing; no provider calls."""
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from sessiongraph import chat_cleanup as cleanup, graph_v2 as graph
from sessiongraph.core import Store, init_project, load_config
from test_global_hooks import hook_module
from test_mvp import write_session


@pytest.fixture
def env(tmp_path):
    project = tmp_path / 'project'
    path = init_project(project)
    sources = tmp_path / 'sessions'
    sources.mkdir()
    archive = tmp_path / 'archived_sessions'
    archive.mkdir()
    raw = json.loads(path.read_text())
    raw['hook'].update(enabled=True, cleanup_missing_chats=True,
                       allowed_transcript_roots=[str(sources), str(archive)])
    raw['graph_v2'] = {'enabled': True, 'revision_policy': {'enabled': True, 'mode': 'effective'},
                       'recovery_policy': {'enabled': False},
                       'resource_policy': {'enabled': False},
                       'stage_policy': {'enabled': False}}
    path.write_text(json.dumps(raw))
    a = write_session(sources / 'chat-a.jsonl', project, 'chat-a', [('user', 'source A work evidence')])
    b = write_session(sources / 'chat-b.jsonl', project, 'chat-b', [('user', 'source B work evidence')])
    store = Store(load_config(path))
    store.ingest(a)
    store.ingest(b)
    graph.setup(store)
    yield path, store, a, b, archive
    store.close()


def publish(store, chat, node, content='shared fact', kind='Claim'):
    entry = store.db.execute('''SELECT e.* FROM entries e JOIN session_chats c ON c.segment_id=e.session_id
        WHERE c.chat_id=? ORDER BY e.line LIMIT 1''', (chat,)).fetchone()
    from sessiongraph.source_text import hydrate
    entry = hydrate(store, entry)
    jid = f'job-{chat}-{node}'
    with store.db:
        store.db.execute('INSERT OR IGNORE INTO sg2_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (jid, store.config['project_id'], chat, entry['session_id'], 'judging', '{}', None, '[]', '{}', None, '', ''))
    item = dict(id='local', type=kind, title=content, content=content, status='completed',
                aliases=[], identity=[], context_kind='work', evidence=[dict(entry_id=entry['id'], quote=entry['text'])])
    graph._commit(store, dict(id=jid, chat=chat), dict(nodes=[item], relations=[], unresolved=[]),
                  [], {'local': node}, [], [], [], graph._stats(), (99, 0), finalize=False)
    return entry['id']


def table_rows(store, table):
    return [tuple(r) for r in store.db.execute(f'SELECT * FROM {table} ORDER BY 1')]


MIGRATION_SCHEMA = '''CREATE TABLE IF NOT EXISTS sg2_judge_migrations(
    job TEXT NOT NULL, revision TEXT NOT NULL, created REAL NOT NULL,
    before_json TEXT NOT NULL, PRIMARY KEY(job,revision))'''


@pytest.mark.parametrize('local,global_value,expected', [
    (None, True, True), (None, False, False), (False, True, False),
    (True, False, True),
])
def test_global_default_and_project_override(tmp_path, monkeypatch, local, global_value, expected):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    (tmp_path / 'sessiongraph.json').write_text(json.dumps({
        'enabled': True, 'cleanup_missing_chats': global_value}))
    config = {'hook': {} if local is None else {'cleanup_missing_chats': local}}
    assert cleanup.enabled(config) is expected
    config['hook']['enabled'] = False
    assert cleanup.enabled(config) is False


def test_absent_disabled_and_invalid_global_policy_preserve(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    assert cleanup.enabled({}) is False
    path = tmp_path / 'sessiongraph.json'
    path.write_text(json.dumps({'enabled': False, 'cleanup_missing_chats': True}))
    assert cleanup.enabled({}) is False
    for invalid in ('true', 1, None):
        path.write_text(json.dumps({'enabled': True, 'cleanup_missing_chats': invalid}))
        with pytest.raises(ValueError, match='must be a boolean'):
            cleanup.enabled({})


def test_global_default_cleans_existing_project_without_rewriting_it(env, tmp_path, monkeypatch):
    cfg, s, a, _, _ = env
    raw = json.loads(cfg.read_text())
    del raw['hook']['cleanup_missing_chats']
    cfg.write_text(json.dumps(raw))
    s.config = load_config(cfg)
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    (tmp_path / 'sessiongraph.json').write_text(json.dumps({
        'enabled': True, 'cleanup_missing_chats': True}))
    before = cfg.read_bytes()
    publish(s, 'chat-a', 'a')
    a.unlink()
    assert cleanup.cleanup(cfg, 'chat-a', str(a))['cleaned']
    assert cfg.read_bytes() == before
    assert not s.db.execute('SELECT 1 FROM work_memory WHERE id=\'a\'').fetchone()
    assert not s.db.execute('PRAGMA foreign_key_check').fetchall()


def test_normal_exit_preserves_graph_without_writer_path(env, monkeypatch):
    cfg, s, a, _, _ = env
    publish(s, 'chat-a', 'a')
    before = table_rows(s, 'work_memory')
    monkeypatch.setattr(cleanup, '_purge', lambda *_: pytest.fail('present source must not enter purge'))
    result = cleanup.cleanup(cfg, 'chat-a', str(a))
    assert result['reason'] == 'source_present' and not result['cleaned']
    assert table_rows(s, 'work_memory') == before


@pytest.mark.parametrize('action', ['delete', 'archive'])
def test_missing_source_cascades_and_preserves_other_chat(env, action):
    cfg, s, a, b, archive = env
    publish(s, 'chat-a', 'only-a', 'private A')
    publish(s, 'chat-b', 'only-b', 'private B')
    before_b = s.db.execute('SELECT * FROM work_memory WHERE id=\'only-b\'').fetchone()
    a.rename(archive / a.name) if action == 'archive' else a.unlink()
    result = cleanup.cleanup(cfg, 'chat-a', str(a))
    assert result['cleaned'] and result['model_calls'] == 0
    assert s.db.execute('SELECT count(*) FROM work_memory WHERE id=\'only-a\'').fetchone()[0] == 0
    assert s.db.execute('SELECT count(*) FROM sessions').fetchone()[0] == 1
    assert s.db.execute('SELECT count(*) FROM sg_retained_sources').fetchone()[0] == 1
    assert tuple(s.db.execute('SELECT * FROM work_memory WHERE id=\'only-b\'').fetchone()) == tuple(before_b)
    assert b.exists()
    assert s.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not s.db.execute('PRAGMA foreign_key_check').fetchall()
    assert cleanup.cleanup(cfg, 'chat-a', str(a))['reason'] == 'unindexed_source'


def test_shared_node_revision_and_relation_retain_surviving_support(env):
    cfg, s, a, _, _ = env
    ea = publish(s, 'chat-a', 'shared')
    eb = publish(s, 'chat-b', 'shared')
    publish(s, 'chat-b', 'target', 'target fact', kind='Artifact')
    with s.db:
        s.db.execute('INSERT INTO sg2_relations VALUES(?,?,?,?,?,?,?,?,?,?)',
            ('relation', s.config['project_id'], 'shared', 'target', 'supports', '', 'direct', 'accepted', '{}', ''))
        s.db.executemany('INSERT INTO sg2_relation_evidence VALUES(?,?,?,?,?)', [
            ('ev-a', 'relation', ea, 'source A work evidence', 'job-a'),
            ('ev-b', 'relation', eb, 'source B work evidence', 'job-b')])
    a.unlink()
    assert cleanup.cleanup(cfg, 'chat-a', str(a))['cleaned']
    memory = s.db.execute('SELECT * FROM work_memory WHERE id=\'shared\'').fetchone()
    assert memory['source_entry_id'] == eb and memory['chat_id'] == 'chat-b'
    assert s.db.execute('SELECT count(*) FROM sg2_nodes').fetchone()[0] == 2
    assert s.db.execute('SELECT count(*) FROM sg2_relations').fetchone()[0] == 1
    assert [r[0] for r in s.db.execute('SELECT id FROM sg2_relation_evidence')] == ['ev-b']
    assert s.db.execute('SELECT count(*) FROM sg_revision_items').fetchone()[0] == 1
    assert s.db.execute('SELECT count(*) FROM sg_revision_sources').fetchone()[0] == 1
    payload = s.db.execute('SELECT payload_evidence FROM sg_revision_items').fetchone()[0]
    assert s.db.execute('SELECT entry_id FROM sg2_evidence WHERE id=?', (payload,)).fetchone()[0] == eb
    with pytest.raises(sqlite3.IntegrityError, match='revision_immutable'):
        s.db.execute('DELETE FROM sg_revision_items')
    s.db.rollback()
    assert not s.db.execute('PRAGMA foreign_key_check').fetchall()


@pytest.fixture
def two_deleted(env):
    cfg, s, a, b, _ = env
    # A and B cite the same node. Purging A first must not retain B's source as
    # a survivor when B is part of the same confirmed deletion batch.
    publish(s, 'chat-a', 'shared', 'deleted A statement')
    publish(s, 'chat-b', 'shared', 'deleted B statement')
    publish(s, 'chat-b', 'only-b', 'deleted B fact')
    a.unlink(); b.unlink()
    return cfg, s, a, b


def test_batch_cleanup_reanchors_only_after_all_deleted_evidence(two_deleted):
    cfg, s, a, b = two_deleted
    result = cleanup.cleanup_many(cfg, ['chat-a', 'chat-b'])
    assert result['cleaned'] and result['model_calls'] == 0
    assert not s.db.execute("SELECT 1 FROM work_memory WHERE id IN ('shared','only-b')").fetchall()
    assert s.db.execute('SELECT count(*) FROM entries').fetchone()[0] == 0
    assert s.db.execute('SELECT count(*) FROM sg2_nodes').fetchone()[0] == 0
    assert s.db.execute('SELECT count(*) FROM sg2_evidence').fetchone()[0] == 0
    assert s.db.execute('SELECT count(*) FROM sg2_relations').fetchone()[0] == 0
    assert not s.db.execute('PRAGMA foreign_key_check').fetchall()
    assert s.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_batch_cleanup_rejects_one_present_source_without_partial_delete(two_deleted):
    cfg, s, a, b = two_deleted
    b.write_text('still present\n')
    before = table_rows(s, 'entries')
    result = cleanup.cleanup_many(cfg, ['chat-a', 'chat-b'])
    assert not result['cleaned'] and result['reason'] == 'source_present'
    assert table_rows(s, 'entries') == before
    assert s.db.execute("SELECT 1 FROM work_memory WHERE id='shared'").fetchone()


def test_batch_cleanup_keeps_node_surviving_in_third_chat(env):
    cfg, s, a, b, _ = env
    publish(s, 'chat-a', 'shared', 'deleted A statement')
    publish(s, 'chat-b', 'shared', 'deleted B statement')
    publish(s, 'chat-b', 'target', 'target fact', kind='Artifact')
    # Third chat cites the same node through an independent evidence row.
    kept_source = write_session(a.parent / 'chat-c.jsonl', Path(s.config['roots'][0]), 'chat-c',
                                [('user', 'surviving C statement')])
    s.ingest(kept_source)
    publish(s, 'chat-c', 'shared', 'surviving C statement')
    a.unlink(); b.unlink()
    result = cleanup.cleanup_many(cfg, ['chat-a', 'chat-b'])
    assert result['cleaned']
    memory = s.db.execute("SELECT * FROM work_memory WHERE id='shared'").fetchone()
    assert memory['chat_id'] == 'chat-c' and memory['content'] == 'surviving C statement'
    assert not s.db.execute("SELECT 1 FROM work_memory WHERE id='target'").fetchone()
    assert s.db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_node_owned_by_other_chat_but_supported_only_by_deleted_chat_is_removed(env):
    cfg, s, a, _, _ = env
    evidence_a = s.db.execute('''SELECT e.id FROM entries e JOIN session_chats c ON c.segment_id=e.session_id
        WHERE c.chat_id='chat-a' ORDER BY e.line LIMIT 1''').fetchone()[0]
    evidence_b_source = s.db.execute('''SELECT e.id FROM entries e JOIN session_chats c ON c.segment_id=e.session_id
        WHERE c.chat_id='chat-b' ORDER BY e.line LIMIT 1''').fetchone()[0]
    # Activity avoids a revision payload, so the fixture can model evidence
    # convergence without mutating an immutable assertion snapshot.
    publish(s, 'chat-b', 'orphan', 'orphan fact', kind='Activity')
    publish(s, 'chat-b', 'target', 'target fact', kind='Artifact')
    evidence_b = s.db.execute("SELECT id FROM sg2_evidence WHERE node='orphan'").fetchone()[0]
    assertion = json.loads(s.db.execute(
        "SELECT assertion_json FROM sg2_evidence WHERE id=?", (evidence_b,)).fetchone()[0])
    assertion['evidence'] = [dict(entry_id=evidence_a, quote='source A work evidence')]
    with s.db:
        # Simulate a compaction/merge representative owned by B whose only
        # concrete support came from A. Ownership alone must not retain it.
        s.db.execute('''UPDATE sg2_evidence SET entry_id=?,quote=?,assertion_json=? WHERE id=?''',
            (evidence_a, 'source A work evidence', json.dumps(assertion, ensure_ascii=False), evidence_b))
        s.db.execute('INSERT INTO sg2_relations VALUES(?,?,?,?,?,?,?,?,?,?)',
            ('relation-orphan', s.config['project_id'], 'orphan', 'target', 'DEPENDS_ON', '',
             'direct', 'accepted', '{}', ''))
        s.db.execute('INSERT INTO sg2_relation_evidence VALUES(?,?,?,?,?)',
            ('rel-ev-b', 'relation-orphan', evidence_b_source, 'source B work evidence', 'job-chat-b-orphan'))
    a.unlink()
    result = cleanup.cleanup(cfg, 'chat-a', str(a))
    assert result['cleaned'] and result['deleted']['unsupported_nodes_removed'] == 1
    assert not s.db.execute('SELECT 1 FROM sg2_nodes WHERE id=\'orphan\'').fetchone()
    assert not s.db.execute('SELECT 1 FROM work_memory WHERE id=\'orphan\'').fetchone()
    assert not s.db.execute('SELECT 1 FROM sg2_relations WHERE id=\'relation-orphan\'').fetchone()
    assert not s.db.execute('SELECT 1 FROM sg2_relation_evidence WHERE relation=\'relation-orphan\'').fetchone()
    assert s.db.execute('SELECT 1 FROM sg2_nodes WHERE id=\'target\'').fetchone()
    assert not s.db.execute('PRAGMA foreign_key_check').fetchall()


def test_reanchor_keeps_paths_from_every_surviving_chat(env):
    cfg, s, a, _, _ = env
    root = Path(s.config['roots'][0])
    deleted = write_session(a.parent / 'chat-deleted.jsonl', root, 'chat-deleted',
                            [('user', '产物 /gpu3/kongsz/microduck-fly/deleted.json 已写入')])
    kept_one = write_session(a.parent / 'chat-kept-one.jsonl', root, 'chat-kept-one',
                             [('user', '产物 /gpu3/kongsz/microduck-fly/kept-one.json 已写入')])
    kept_two = write_session(a.parent / 'chat-kept-two.jsonl', root, 'chat-kept-two',
                             [('user', '产物 /gpu3/kongsz/microduck-fly/kept-two.json 已写入')])
    for path in (deleted, kept_one, kept_two):
        s.ingest(path)
    publish(s, 'chat-deleted', 'shared')
    publish(s, 'chat-kept-one', 'shared')
    publish(s, 'chat-kept-two', 'shared')
    before = json.loads(s.db.execute(
        "SELECT paths_json FROM work_memory WHERE id='shared'").fetchone()[0])
    assert any('deleted.json' in path for path in before)
    assert any('kept-one.json' in path for path in before)
    assert any('kept-two.json' in path for path in before)

    deleted.unlink()
    assert cleanup.cleanup(cfg, 'chat-deleted', str(deleted))['cleaned']
    after = json.loads(s.db.execute(
        "SELECT paths_json FROM work_memory WHERE id='shared'").fetchone()[0])
    # Both surviving chats still support the shared node. The representative
    # payload of one row must not erase the other row's literal path.
    assert any('kept-one.json' in path for path in after)
    assert any('kept-two.json' in path for path in after)
    assert not any('deleted.json' in path for path in after)


def test_different_surviving_assertion_does_not_retain_removed_content(env):
    cfg, s, a, _, _ = env
    publish(s, 'chat-a', 'shared', 'deleted A statement')
    eb = publish(s, 'chat-b', 'shared', 'surviving B statement')
    a.unlink()
    assert cleanup.cleanup(cfg, 'chat-a', str(a))['cleaned']
    row = s.db.execute('SELECT * FROM work_memory WHERE id=\'shared\'').fetchone()
    assert row['content'] == 'surviving B statement' and row['source_entry_id'] == eb
    assert 'deleted A' not in row['aliases_json']
    assert s.db.execute('SELECT count(*) FROM sg_revision_items').fetchone()[0] == 1


def test_reanchor_captures_legacy_survivor_under_retention_policy(env):
    from sessiongraph import source_retention as retention
    cfg, s, a, _, _ = env
    publish(s, 'chat-a', 'shared', 'deleted A statement')
    eb = publish(s, 'chat-b', 'shared', 'surviving B statement')
    with s.db:
        retention.setup(s)
        s.db.execute('DELETE FROM sg_retained_sources WHERE entry_id=?', (eb,))
        s.db.execute('INSERT INTO metadata VALUES(?,?)', (retention.FLAG, retention.POLICY))
    assert not s.db.execute('SELECT 1 FROM sg_retained_sources WHERE entry_id=?', (eb,)).fetchone()
    a.unlink()
    assert cleanup.cleanup(cfg, 'chat-a', str(a))['cleaned']
    row = s.db.execute("SELECT * FROM work_memory WHERE id='shared'").fetchone()
    assert row['source_entry_id'] == eb and row['chat_id'] == 'chat-b'
    assert row['content'] == 'surviving B statement'
    assert retention.read(s, eb)['text'] == 'source B work evidence'
    assert not s.db.execute('PRAGMA foreign_key_check').fetchall()


def test_unavailable_root_and_permission_error_preserve(env, monkeypatch):
    cfg, s, a, _, _ = env
    publish(s, 'chat-a', 'a')
    a.unlink()
    real_scandir = cleanup.os.scandir
    def unavailable(path):
        if Path(path) == a.parent:
            raise PermissionError('fixture')
        return real_scandir(path)
    monkeypatch.setattr(cleanup.os, 'scandir', unavailable)
    assert cleanup.cleanup(cfg, 'chat-a', str(a))['reason'] == 'source_unavailable'
    assert s.db.execute('SELECT count(*) FROM work_memory').fetchone()[0] == 1


def test_whole_root_missing_is_not_chat_deletion(env):
    cfg, s, a, _, _ = env
    a.parent.rename(a.parent.with_name('offline'))
    assert cleanup.cleanup(cfg, 'chat-a', str(a))['reason'] == 'source_unavailable'
    assert s.db.execute('SELECT count(*) FROM sessions').fetchone()[0] == 2


def test_multi_segment_chat_retained_while_one_source_exists(env):
    cfg, s, a, _, _ = env
    second = write_session(a.parent / 'segment-two.jsonl', Path(s.config['roots'][0]), 'chat-a', [('user', 'second source')])
    s.ingest(second)
    a.unlink()
    assert cleanup.cleanup(cfg, 'chat-a', str(a))['reason'] == 'source_present'
    second.unlink()
    assert cleanup.cleanup(cfg, 'chat-a', str(a))['cleaned']
    assert s.db.execute('SELECT count(*) FROM sessions').fetchone()[0] == 1


def test_unknown_path_chat_disabled_and_symlink_never_clean(env):
    cfg, s, a, b, _ = env
    assert cleanup.cleanup(cfg, 'chat-a', str(b))['reason'] == 'unindexed_source'
    assert cleanup.cleanup(cfg, 'unknown', str(a))['reason'] == 'unindexed_source'
    a.unlink()
    a.symlink_to(a.parent / 'missing-target')
    assert cleanup.cleanup(cfg, 'chat-a', str(a))['reason'] == 'source_present'
    raw = json.loads(cfg.read_text())
    raw['hook']['cleanup_missing_chats'] = False
    cfg.write_text(json.dumps(raw))
    assert cleanup.cleanup(cfg, 'chat-a', str(a))['reason'] == 'disabled'


def test_failure_rolls_back_data_and_exact_immutable_guards(env, monkeypatch):
    cfg, s, a, _, _ = env
    publish(s, 'chat-a', 'a')
    guards = s.db.execute('SELECT name,sql FROM sqlite_master WHERE type=\'trigger\' ORDER BY name').fetchall()
    before = table_rows(s, 'sg_revision_items')
    a.unlink()
    def broken(*_):
        raise ValueError('fixture failure')
    monkeypatch.setattr(cleanup, '_reanchor', broken)
    with pytest.raises(ValueError, match='fixture failure'):
        cleanup.cleanup(cfg, 'chat-a', str(a))
    assert table_rows(s, 'sg_revision_items') == before
    assert s.db.execute('SELECT name,sql FROM sqlite_master WHERE type=\'trigger\' ORDER BY name').fetchall() == guards
    assert not cleanup.excluded(s, 'chat-a')


def test_archived_source_not_reimported_but_original_restoration_works(env):
    cfg, s, a, _, archive = env
    target = archive / a.name
    a.rename(target)
    assert cleanup.cleanup(cfg, 'chat-a', str(a))['cleaned']
    with pytest.raises(ValueError, match='chat source was cleaned'):
        s.ingest(target)
    assert cleanup.excluded(s, 'chat-a')
    target.rename(a)
    assert not cleanup.excluded(s, 'chat-a')
    assert s.ingest(a)['added'] == 1
    assert cleanup.marker(s.db, 'chat-a') is None


def test_archive_relocation_cannot_race_cleanup(env):
    from sessiongraph.continuity import refresh_chat
    cfg, s, a, _, archive = env
    a.rename(archive / a.name)
    result = refresh_chat(s, 'chat-a', str(a))
    assert result['added_entries'] == 0 and result['skipped']
    with pytest.raises(ValueError, match='chat source cleanup pending'):
        s.ingest(archive / a.name)
    assert s.db.execute('SELECT path FROM sessions WHERE id=\'chat-a\'').fetchone()[0] == str(a)
    assert cleanup.cleanup(cfg, 'chat-a', str(a))['cleaned']


def test_old_importer_cannot_resurrect_cleaned_archive(env, monkeypatch):
    cfg, s, a, _, archive = env
    target = archive / a.name
    a.rename(target)
    assert cleanup.cleanup(cfg, 'chat-a', str(a))['cleaned']
    monkeypatch.setattr(cleanup, 'check_import', lambda *_: None)
    with pytest.raises(sqlite3.IntegrityError, match='chat_source_cleaned'):
        s.ingest(target)
    assert not s.db.execute('SELECT 1 FROM session_chats WHERE chat_id=\'chat-a\'').fetchone()
    target.rename(a)
    assert s.ingest(a)['added'] == 1


def test_direct_extractor_does_not_read_stale_checkpoint_or_call_model(env):
    from sessiongraph.memories import run_chat
    cfg, s, a, _, _ = env
    a.unlink()
    assert cleanup.cleanup(cfg, 'chat-a', str(a))['cleaned']
    def forbidden(*_):
        pytest.fail('a cleaned chat cannot call a model')
    with pytest.raises(ValueError, match='chat source was cleaned'):
        run_chat(s, forbidden, 'chat-a')


def test_deleting_correction_cleans_review_and_controls_without_deleting_old_support(env):
    from sessiongraph import revision_ledger as ledger
    cfg, s, _, b, _ = env
    publish(s, 'chat-a', 'shared', 'old assertion')
    eb = publish(s, 'chat-b', 'shared', 'new assertion')
    revs = {json.loads(row['assertion_json'])['content']: row['id'] for row in s.db.execute('''
        SELECT r.id,e.assertion_json FROM sg_revision_items r JOIN sg2_evidence e ON e.id=r.payload_evidence''')}
    ev = s.db.execute('SELECT id FROM sg2_evidence WHERE entry_id=?', (eb,)).fetchone()[0]
    manifest = dict(version=ledger.VERSION, project_id=s.config['project_id'], source=revs['new assertion'],
        target=revs['old assertion'], effect='corrects', facet='fixture', conditions={}, evidence=ev,
        authority='direct_user', effective_from='2026-10-24T00:00:00Z')
    with s.db:
        s.db.execute('BEGIN IMMEDIATE')
        ledger.assess(s, manifest, apply=True)
        review = s.db.execute('SELECT id FROM sg_revision_reviews').fetchone()[0]
        s.db.execute('INSERT INTO sg_revision_review_controls VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('control', s.config['project_id'], review, 'root', 'retract', revs['new assertion'], ev,
             'direct_user', '2026-10-25T00:00:00Z', None, '2026-10-25T00:00:00Z'))
    b.unlink()
    assert cleanup.cleanup(cfg, 'chat-b', str(b))['cleaned']
    assert s.db.execute('SELECT count(*) FROM sg_revision_reviews').fetchone()[0] == 0
    assert s.db.execute('SELECT count(*) FROM sg_revision_review_controls').fetchone()[0] == 0
    assert s.db.execute('SELECT count(*) FROM sg_revision_items').fetchone()[0] == 1
    assert s.db.execute('SELECT content FROM work_memory WHERE id=\'shared\'').fetchone()[0] == 'old assertion'
    assert not s.db.execute('PRAGMA foreign_key_check').fetchall()


def test_pending_work_dismissed_and_no_model_work_after_cleanup(env, monkeypatch):
    from sessiongraph import continuation as c
    from sessiongraph.auto_update import run_update
    cfg, s, a, _, _ = env
    folder = c.root(s.config)
    folder.mkdir(parents=True)
    identity = c.key(cfg, 'chat-a')
    c.write(folder / 'queue.json', {identity: {'config': str(cfg), 'chat': 'chat-a', 'state': 'ready', 'ingress': {'sources': {}}}})
    a.unlink()
    assert cleanup.cleanup(cfg, 'chat-a', str(a))['cleaned']
    assert c.read(folder / 'queue.json', {})[identity]['state'] == 'dismissed'
    def no_execute(*_):
        pytest.fail('cleaned chat must not import or call a model')
    result = run_update(cfg, 'chat-a', 'manual', execute=no_execute)
    assert result['state'] == 'cancelled' and result['reported_model_calls'] == 0


@pytest.mark.parametrize('reason', ['other', 'deleted', 'archived'])
def test_session_end_hook_only_dispatches_opt_in_check(env, monkeypatch, reason):
    cfg, s, a, _, _ = env
    hook = hook_module()
    monkeypatch.setattr(hook, 'GLOBAL_SETTINGS', cfg.parent / 'no-global-settings.json')
    spawned = []
    monkeypatch.setattr(hook.subprocess, 'Popen', lambda *args, **kwargs: spawned.append((args, kwargs)))
    monkeypatch.setattr(hook, 'ensure_project', lambda *_: pytest.fail('must not enroll'))
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({'hook_event_name': 'SessionEnd',
        'session_id': 'chat-a', 'cwd': s.config['roots'][0], 'transcript_path': str(a), 'reason': reason})))
    out = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', out)
    assert hook.main() == 0 and out.getvalue() == ''
    assert len(spawned) == 1 and 'sessiongraph.chat_cleanup' in spawned[0][0][0]
    assert spawned[0][1]['start_new_session'] is True
    assert a.exists()


def test_detached_checker_handles_file_removal_after_hook_returns(env, monkeypatch):
    cfg, s, a, _, _ = env
    publish(s, 'chat-a', 'a')
    hook = hook_module()
    monkeypatch.setattr(hook, 'GLOBAL_SETTINGS', cfg.parent / 'no-global-settings.json')
    monkeypatch.setenv('CODEX_HOME', str(cfg.parent / 'isolated-codex'))
    raw = json.loads(cfg.read_text())
    del raw['hook']['cleanup_missing_chats']
    cfg.write_text(json.dumps(raw))
    home = Path(cleanup.os.environ['CODEX_HOME'])
    home.mkdir()
    (home / 'sessiongraph.json').write_text(json.dumps({
        'enabled': True, 'cleanup_missing_chats': True}))
    real_popen, children = subprocess.Popen, []
    def tracked(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(hook.subprocess, 'Popen', tracked)
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({'hook_event_name': 'SessionEnd',
        'session_id': 'chat-a', 'cwd': s.config['roots'][0], 'transcript_path': str(a), 'reason': 'other'})))
    output = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', output)
    started = time.monotonic()
    assert hook.main() == 0 and output.getvalue() == ''
    assert time.monotonic() - started < 1.5
    assert len(children) == 1
    proc = children[0]
    try:
        assert a.exists() and proc.poll() is None
        a.unlink()
        assert proc.wait(timeout=10) == 0
        assert cleanup.excluded(s, 'chat-a')
        assert not s.db.execute('SELECT 1 FROM work_memory WHERE id=\'a\'').fetchone()
        assert time.monotonic() - started >= 2
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_chat_deletion_cascades_task_dismissal_receipts(env):
    from sessiongraph.maintenance import dismiss_graph_batches
    cfg, s, a, b, _ = env
    publish(s, 'chat-a', 'only-a')
    publish(s, 'chat-b', 'only-b')
    with s.db:
        s.db.executemany("INSERT INTO sg2_control VALUES(?,'blocked',0,1,'{}')",
                         [('job-chat-a-only-a',), ('job-chat-b-only-b',)])
    assert dismiss_graph_batches(s.config, time.time()) == 2
    a.unlink()
    assert cleanup.cleanup(cfg, 'chat-a', str(a))['cleaned']
    assert [r[0] for r in s.db.execute('SELECT job FROM sg2_task_dismissals')] == ['job-chat-b-only-b']
    assert b.exists() and not s.db.execute('PRAGMA foreign_key_check').fetchall()


def _archive(store, value):
    from sessiongraph import storage
    with store.db:
        return storage.archive_text(store, value)


def _receipt(config, run_id, chat, *, folder='runs', state='succeeded'):
    from sessiongraph.auto_update import _atomic, _folder
    path = (_folder(config) if folder == 'runs' else _folder(config).parent / 'cleared-runs') / (run_id + '.json')
    now = time.time()
    _atomic(path, dict(run_id=run_id, chat_id=chat, state=state,
                       created_at=now, updated_at=now, padding='receipt\n' * 300))
    return path


def test_chat_delete_removes_exclusive_local_state(env):
    from sessiongraph import restoration
    cfg, s, a, b, _ = env
    folder = Path(s.config['database']).parent / 'memory-extraction'
    folder.mkdir(exist_ok=True)
    for chat in ('chat-a', 'chat-b'):
        (folder / (chat + '.json')).write_text(json.dumps({'segments': {}, 'updated_at': 'x', 'version': 2}))
        (folder / (chat + '.lock')).touch()
        restoration.mark(s.config, chat)
    a.unlink()
    result = cleanup.cleanup(cfg, 'chat-a', str(a))
    assert result['cleaned'] and result['receipt_files'] == 3
    assert not (folder / 'chat-a.json').exists() and not (folder / 'chat-a.lock').exists()
    assert not restoration.flag_path(s.config, 'chat-a').exists()
    assert (folder / 'chat-b.json').exists() and (folder / 'chat-b.lock').exists()
    assert restoration.flag_path(s.config, 'chat-b').exists()


def test_chat_delete_removes_exclusive_receipt_and_payload_but_keeps_shared(env):
    from sessiongraph import run_receipts, storage
    cfg, s, a, _, _ = env
    publish(s, 'chat-a', 'a')
    publish(s, 'chat-b', 'b')
    private = _archive(s, json.dumps({'private': 'A-only ' * 400}))
    shared = _archive(s, json.dumps({'shared': 'both ' * 400}))
    with s.db:
        s.db.execute('UPDATE sg2_jobs SET candidates_json=? WHERE id=?', (private, 'job-chat-a-a'))
        s.db.execute('UPDATE sg2_jobs SET output_json=? WHERE id=?', (shared, 'job-chat-a-a'))
        s.db.execute('UPDATE sg2_jobs SET output_json=? WHERE id=?', (shared, 'job-chat-b-b'))
    cold_a, cold_b = _receipt(s.config, 'run-a', 'chat-a'), _receipt(s.config, 'run-b', 'chat-b')
    with run_receipts.connection(s.config, readonly=False) as receipts_db:
        run_receipts.archive_file(s.config, cold_a, receipts_db)
        run_receipts.archive_file(s.config, cold_b, receipts_db)
    hot_a = _receipt(s.config, 'hot-a', 'chat-a')
    hot_b = _receipt(s.config, 'hot-b', 'chat-b')
    audit = Path(s.config['database']).parent / 'request-audit'
    audit.mkdir()
    (audit / 'request-a.json').write_text(json.dumps({'id': 'a', 'job_id': 'job-chat-a-a'}))
    (audit / 'request-b.json').write_text(json.dumps({'id': 'b', 'job_id': 'job-chat-b-b'}))
    hashes_before = set(storage.archive_payloads(s.db))
    assert len(hashes_before) >= 4
    a.unlink()
    result = cleanup.cleanup(cfg, 'chat-a', str(a))
    assert result['cleaned'] and result['deleted']['sg_run_receipts'] == 1
    assert not s.db.execute("SELECT 1 FROM sg_run_receipts WHERE run_id='run-a'").fetchone()
    assert s.db.execute("SELECT 1 FROM sg_run_receipts WHERE run_id='run-b'").fetchone()
    assert not hot_a.exists() and hot_b.exists() and not (audit / 'request-a.json').exists()
    assert (audit / 'request-b.json').exists()
    remaining = set(storage.archive_payloads(s.db))
    assert __import__('sessiongraph.storage', fromlist=['payload_hash']).payload_hash(private) not in remaining
    assert json.loads(shared)['_sessiongraph_payload_v1'] in remaining
    assert run_receipts.read(s.config, 'run-b').startswith('{')


def test_deleted_chat_receipt_is_fenced_even_if_worker_rewrites_hot_file(env):
    from sessiongraph import run_receipts
    from sessiongraph.auto_update import update_status
    cfg, s, a, _, _ = env
    a.unlink()
    assert cleanup.cleanup(cfg, 'chat-a', str(a))['cleaned']
    late = _receipt(s.config, 'late-a', 'chat-a')
    with pytest.raises(ValueError, match='chat_deleted'):
        run_receipts.read(s.config, 'late-a')
    assert all(run['chat_id'] != 'chat-a' for run in update_status(s.config)['runs'])
    assert late.exists()
    # The exact original transcript path restores normal readable history.
    write_session(a, Path(s.config['roots'][0]), 'chat-a', [('user', 'restored')])
    s.ingest(a)
    assert cleanup.marker(s.db, 'chat-a') is None
    assert json.loads(run_receipts.read(s.config, 'late-a'))['chat_id'] == 'chat-a'


def test_payload_reference_parser_is_exact_and_recursive():
    from sessiongraph import storage
    exact = '{"_sessiongraph_payload_v1":"' + 'a' * 64 + '"}'
    assert storage.payload_hash(exact) == 'a' * 64
    assert storage.payload_hash('prefix ' + exact) is None
    assert storage.payload_hash('{"_sessiongraph_payload_v1":"' + 'g' * 64 + '"}') is None
    nested = json.dumps({'before_json': json.dumps({'output_json': exact})})
    assert storage.payload_hashes(nested) == {'a' * 64}


def test_orphan_content_preview_is_read_only_and_apply_is_idempotent(env):
    from sessiongraph import run_receipts, storage
    cfg, s, a, _, _ = env
    a.unlink()
    assert cleanup.cleanup(cfg, 'chat-a', str(a))['cleaned']
    # Reproduce historical residue after an older runtime: cold receipt and a
    # payload that no surviving canonical row references.
    cold = _receipt(s.config, 'old-a', 'chat-a')
    with run_receipts.connection(s.config, readonly=False) as db:
        # Bypass the new guard deliberately to model a pre-fix database row.
        payload = storage.archive_text(type('D', (), {'db': db})(), 'old residue ' * 300)
        db.execute('''INSERT INTO sg_run_receipts VALUES(?,?,?,?,?,?,?,?,?,0)''',
                   (s.config['project_id'], 'old-a', 'chat-a', 'succeeded', 1, 1, 1, 'x', payload))
        db.commit()
    before = [tuple(r) for r in s.db.execute('SELECT * FROM sg_run_receipts ORDER BY run_id')]
    preview = cleanup.cleanup_orphan_content(s)
    assert preview['applied'] is False and preview['model_calls'] == 0
    assert preview['stale_receipts'] == 1 and preview['stale_hot_receipts'] == 1
    assert preview['orphan_payloads'] >= 1
    assert [tuple(r) for r in s.db.execute('SELECT * FROM sg_run_receipts ORDER BY run_id')] == before
    applied = cleanup.cleanup_orphan_content(s, apply=True)
    assert applied['applied'] is True and applied['stale_receipts'] == 1
    assert not s.db.execute("SELECT 1 FROM sg_run_receipts WHERE run_id='old-a'").fetchone()
    assert not cold.exists()
    again = cleanup.cleanup_orphan_content(s, apply=True)
    assert again['payload_rows_deleted'] == 0
    assert again['stale_hot_receipts'] == 0 and again['receipt_files'] == 0


def test_payload_reference_in_nested_json_string_is_never_deleted(env):
    from sessiongraph import storage
    cfg, s, a, _, _ = env
    publish(s, 'chat-a', 'a')
    publish(s, 'chat-b', 'b')
    nested = json.dumps({'outer': json.dumps({'inner': _archive(s, 'shared nested ' * 400)})})
    with s.db:
        s.db.execute('UPDATE sg2_jobs SET candidates_json=? WHERE id=?', (nested, 'job-chat-b-b'))
    identity = storage.payload_hashes(nested).pop()
    a.unlink()
    assert cleanup.cleanup(cfg, 'chat-a', str(a))['cleaned']
    assert identity in storage.archive_payloads(s.db)
    assert identity in storage.referenced_payloads(s.db)


def test_cleanup_orphan_content_reports_maintenance_busy(env):
    import fcntl
    cfg, s, a, _, _ = env
    folder = Path(s.config['database']).parent
    with (folder / 'stage-activity.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match='maintenance_busy'):
            cleanup.cleanup_orphan_content(s, apply=True)


def test_live_job_has_started_audit_during_transport(env, monkeypatch):
    from urllib.request import Request
    from sessiongraph import request_audit, stages
    cfg, s, a, _, _ = env
    publish(s, 'chat-a', 'a')
    seen = []
    class Response:
        status = 200
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return b'{"status":"completed","output":[],"usage":{"output_tokens":1}}'
    class Opener:
        def open(self, request, timeout):
            records = list((Path(s.config['database']).parent / 'request-audit').glob('request-*.json'))
            assert len(records) == 1
            seen.append(json.loads(records[0].read_text()))
            return Response()
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: Opener())
    client = type('C', (), {'config': s.config, 'key': 'secret', 'model': 'fixture',
        'max_output': 10, 'timeout': 30, 'traces': [], 'calls': 0, 'provider_name': 'fixture'})()
    request = Request('http://127.0.0.1/v1/responses',
        data=json.dumps({'model': 'fixture', 'max_output_tokens': 10}).encode())
    token = stages.CURRENT.set({'job_id': 'job-chat-a-a', 'attempt_id': 'live',
        'part': 'part', 'stage': 'extract'})
    try:
        request_audit._read_response(client, request, [], None)
    finally:
        stages.CURRENT.reset(token)
    assert seen[0]['state'] == 'started' and seen[0]['job_id'] == 'job-chat-a-a'
    final = json.loads(Path(client.traces[0]['path']).read_text())
    assert final['state'] == 'received'


def test_deleted_job_late_transport_reuses_full_path_without_audit(env, monkeypatch):
    from urllib.request import Request
    from sessiongraph import request_audit, stages
    cfg, s, a, _, _ = env
    publish(s, 'chat-a', 'a')
    with s.db:
        s.db.execute("DELETE FROM sg2_jobs WHERE id='job-chat-a-a'")
    seen = []
    class Response:
        status = 200
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return b'{"status":"completed","output":[],"usage":{"output_tokens":1}}'
    class Opener:
        def open(self, request, timeout):
            seen.append(request.full_url)
            return Response()
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: Opener())
    client = type('C', (), {'config': s.config, 'key': 'secret', 'model': 'fixture',
        'max_output': 10, 'timeout': 30, 'traces': [], 'calls': 0, 'provider_name': 'fixture'})()
    request = Request('http://127.0.0.1/v1/responses',
        data=json.dumps({'model': 'fixture', 'max_output_tokens': 10}).encode())
    token = stages.CURRENT.set({'job_id': 'job-chat-a-a', 'attempt_id': 'late',
        'part': 'part', 'stage': 'extract'})
    try:
        raw = request_audit.read_response(client, request, [])
    finally:
        stages.CURRENT.reset(token)
    assert b'completed' in raw and len(seen) == 1 and client.traces == []
    assert not list((Path(s.config['database']).parent / 'request-audit').glob('request-*.json'))


def test_chat_delete_removes_only_its_judge_migration_history(env):
    cfg, s, a, _, _ = env
    publish(s, 'chat-a', 'a')
    publish(s, 'chat-b', 'b')
    segment_a = s.db.execute("SELECT segment FROM sg2_jobs WHERE id='job-chat-a-a'").fetchone()[0]
    segment_b = s.db.execute("SELECT segment FROM sg2_jobs WHERE id='job-chat-b-b'").fetchone()[0]
    with s.db:
        s.db.execute(MIGRATION_SCHEMA)
        s.db.executemany('INSERT INTO sg2_judge_migrations VALUES(?,?,?,?)', [
            ('job-chat-a-a', 'rev-a', 1, json.dumps({'segment': segment_a, 'state': 'blocked'})),
            ('job-chat-b-b', 'rev-b', 1, json.dumps({'segment': segment_b, 'state': 'blocked'})),
            ('ghost-other-chat', 'rev-c', 1, json.dumps({'segment': 'other-chat::segment', 'state': 'blocked'})),
        ])
    a.unlink()
    assert cleanup.cleanup(cfg, 'chat-a', str(a))['cleaned']
    jobs = {r[0] for r in s.db.execute('SELECT job FROM sg2_judge_migrations')}
    assert jobs == {'job-chat-b-b', 'ghost-other-chat'}
    assert not s.db.execute('PRAGMA foreign_key_check').fetchall()


def test_orphan_repair_deletes_only_migrations_of_deleted_chats(env):
    cfg, s, a, _, _ = env
    publish(s, 'chat-a', 'a')
    publish(s, 'chat-b', 'b')
    segment_a = s.db.execute("SELECT segment FROM sg2_jobs WHERE id='job-chat-a-a'").fetchone()[0]
    segment_b = s.db.execute("SELECT segment FROM sg2_jobs WHERE id='job-chat-b-b'").fetchone()[0]
    a.unlink()
    assert cleanup.cleanup(cfg, 'chat-a', str(a))['cleaned']
    with s.db:
        # Model a row written by an older runtime after the chat transaction.
        s.db.execute(MIGRATION_SCHEMA)
        s.db.executemany('INSERT INTO sg2_judge_migrations VALUES(?,?,?,?)', [
            ('legacy-deleted', 'rev-old', 1, json.dumps({'segment': segment_a, 'state': 'blocked'})),
            ('legacy-live', 'rev-old', 1, json.dumps({'segment': segment_b, 'state': 'blocked'})),
        ])
    preview = cleanup.cleanup_orphan_content(s)
    assert preview['stale_judge_migrations'] == 1 and preview['model_calls'] == 0
    assert s.db.execute('SELECT count(*) FROM sg2_judge_migrations').fetchone()[0] == 2
    applied = cleanup.cleanup_orphan_content(s, apply=True)
    assert applied['judge_migrations_deleted'] == 1
    jobs = {r[0] for r in s.db.execute('SELECT job FROM sg2_judge_migrations')}
    # A missing parent whose segment still names a surviving chat is preserved.
    assert jobs == {'legacy-live'}


def test_validation_result_does_not_resurrect_deleted_job_audit(env):
    from sessiongraph import request_audit
    cfg, s, a, _, _ = env
    publish(s, 'chat-a', 'a')
    folder = Path(s.config['database']).parent / 'request-audit'
    folder.mkdir(exist_ok=True)
    path = folder / 'request-race.json'
    path.write_text(json.dumps({'id': 'race', 'job_id': 'job-chat-a-a', 'state': 'received'}))
    client = type('C', (), {'config': s.config, 'key': 'secret', 'traces': [{'path': str(path)}]})()
    with s.db:
        s.db.execute("DELETE FROM sg2_jobs WHERE id='job-chat-a-a'")
    request_audit.validation_result(client, job_id='job-chat-a-a')
    assert not path.exists()


def test_validation_result_still_writes_for_live_job(env):
    from sessiongraph import request_audit
    cfg, s, a, _, _ = env
    publish(s, 'chat-a', 'a')
    folder = Path(s.config['database']).parent / 'request-audit'
    folder.mkdir(exist_ok=True)
    path = folder / 'request-live.json'
    path.write_text(json.dumps({'id': 'live', 'job_id': 'job-chat-a-a', 'state': 'received'}))
    client = type('C', (), {'config': s.config, 'key': 'secret', 'traces': [{'path': str(path)}]})()
    request_audit.validation_result(client, job_id='job-chat-a-a')
    assert json.loads(path.read_text())['state'] == 'validated'


def test_validation_result_fails_open_when_database_unavailable(env, tmp_path):
    from sessiongraph import request_audit
    _, s, _, _, _ = env
    path = tmp_path / 'request-offline.json'
    path.write_text(json.dumps({'id': 'offline', 'job_id': 'job-chat-a-a', 'state': 'received'}))
    config = {'database': str(tmp_path / 'missing.sqlite3'), 'project_id': 'fixture'}
    client = type('C', (), {'config': config, 'key': 'secret', 'traces': [{'path': str(path)}]})()
    request_audit.validation_result(client, job_id='job-chat-a-a')
    assert json.loads(path.read_text())['state'] == 'validated'


def test_orphan_content_cli_project_scope_and_help():
    from sessiongraph import cli
    parser = cli.parser()
    assert parser.parse_args(['cleanup-orphan-content']).apply is False
    assert parser.parse_args(['cleanup-orphan-content', '--apply']).apply is True
