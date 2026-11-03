"""Real SQLite lifecycle regression: no providers, source writes outside fixtures or GPUs."""
import json
from pathlib import Path
import sqlite3

import pytest

from sessiongraph import chat_cleanup, evidence_recovery, job_lifecycle, recovery, resource_policy
from sessiongraph.core import Store
from test_chat_cleanup import env, publish
from test_resource_policy import env as resource_env, reserve, finish


def ledgers(store):
    store.db.executescript(resource_policy.SCHEMA + recovery.SCHEMA + evidence_recovery.GRANT_SCHEMA)


def seed(store, job, *, project=None):
    project = project or store.config['project_id']
    with store.db:
        store.db.execute('INSERT INTO sg_resource_scopes VALUES(?,?,?,7,100,20,15)', (project, job, '{}'))
        store.db.execute("INSERT INTO sg_resource_requests VALUES(?,?,?,?,?,'received',1,2,100,20,15,15,NULL,NULL)",
                         (project + job, project, job, 'attempt', 'backend'))
        store.db.execute('INSERT INTO sg_resource_grants VALUES(?,?,?,?,?)', (project, job, 'once', 1, '{"max_requests":2}'))
        store.db.execute('INSERT INTO sg2_recoveries(id,job,reason,created,before_json,automatic_key) VALUES(?,?,?,?,?,?)',
                         (project + job, job, 'once', 1, '{"output_json":"private snapshot"}', project + job))


def snapshot(store, job):
    return {table: [dict(r) for r in store.db.execute(f'SELECT * FROM {table} WHERE job=? ORDER BY 1', (job,))]
            for table in job_lifecycle.TABLES}


def drop_guards(store):
    with store.db:
        for table in job_lifecycle.TABLES:
            store.db.execute(f'DROP TRIGGER IF EXISTS sg_job_delete_{table}')


def test_chat_delete_cascades_all_ledgers_and_keeps_shared_node_and_other_budget(env):
    cfg, store, a, _, _ = env
    publish(store, 'chat-a', 'shared')
    surviving_entry = publish(store, 'chat-b', 'shared')
    ledgers(store)
    seed(store, 'job-chat-a-shared')
    seed(store, 'job-chat-b-shared')
    before = snapshot(store, 'job-chat-b-shared')
    a.unlink()
    assert chat_cleanup.cleanup(cfg, 'chat-a', str(a))['cleaned']
    assert all(not rows for rows in snapshot(store, 'job-chat-a-shared').values())
    assert snapshot(store, 'job-chat-b-shared') == before
    assert store.db.execute("SELECT source_entry_id FROM work_memory WHERE id='shared'").fetchone()[0] == surviving_entry
    assert not store.db.execute('PRAGMA foreign_key_check').fetchall()
    assert store.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_cleanup_failure_rolls_back_graph_parent_and_every_ledger(env):
    cfg, store, a, _, _ = env
    publish(store, 'chat-a', 'a')
    ledgers(store)
    seed(store, 'job-chat-a-a')
    before = snapshot(store, 'job-chat-a-a')
    store.db.execute("CREATE TRIGGER fail_ledger_delete BEFORE DELETE ON sg_resource_requests BEGIN SELECT RAISE(ABORT,'test_failure'); END")
    store.db.commit()
    a.unlink()
    with pytest.raises(sqlite3.IntegrityError, match='test_failure'):
        chat_cleanup.cleanup(cfg, 'chat-a', str(a))
    assert snapshot(store, 'job-chat-a-a') == before
    assert store.db.execute("SELECT 1 FROM sg2_jobs WHERE id='job-chat-a-a'").fetchone()
    assert store.db.execute("SELECT 1 FROM work_memory WHERE id='a'").fetchone()
    assert chat_cleanup.marker(store.db, 'chat-a') is None


def test_upgrade_protects_already_open_old_connection_without_foreign_keys(env):
    _, store, _, _, _ = env
    publish(store, 'chat-a', 'a')
    ledgers(store)
    seed(store, 'job-chat-a-a')
    drop_guards(store)
    old = sqlite3.connect(store.config['database'])
    try:
        assert old.execute('PRAGMA foreign_keys').fetchone()[0] == 0
        upgraded = Store(store.config)
        upgraded.close()
        with old:
            old.execute("DELETE FROM sg2_jobs WHERE id='job-chat-a-a'")
        assert all(not rows for rows in snapshot(store, 'job-chat-a-a').values())
    finally:
        old.close()


@pytest.mark.parametrize('state', ['pending', 'judging', 'complete', 'dismissed', 'superseded'])
def test_repair_only_orphans_preserves_all_live_job_states_and_circuits(env, state):
    _, store, _, _, _ = env
    publish(store, 'chat-a', 'a')
    ledgers(store)
    seed(store, 'job-chat-a-a')
    seed(store, 'historical-missing')
    seed(store, 'foreign-missing', project='other-project')
    with store.db:
        store.db.execute('UPDATE sg2_jobs SET state=?', (state,))
        store.db.execute('INSERT INTO sg_resource_circuits VALUES(?,?,?,3,100,1,NULL,NULL)',
                         (store.config['project_id'], 'backend', '{}'))
    before = snapshot(store, 'job-chat-a-a')
    foreign = snapshot(store, 'foreign-missing')
    changes = store.db.total_changes
    preview = job_lifecycle.prune_orphans(store)
    assert preview['applied'] is False and store.db.total_changes == changes
    assert preview['orphan_rows'] == dict(sg2_recoveries=2, sg_resource_scopes=1,
                                         sg_resource_requests=1, sg_resource_grants=1)
    repaired = job_lifecycle.prune_orphans(store, apply=True)
    assert repaired['orphan_rows'] == preview['orphan_rows']
    assert all(not rows for rows in snapshot(store, 'historical-missing').values())
    assert snapshot(store, 'job-chat-a-a') == before
    # Recovery rows have no project column; the project-validated DB is their owner.
    for table in job_lifecycle.TABLES[1:]:
        assert snapshot(store, 'foreign-missing')[table] == foreign[table]
    assert store.db.execute('SELECT failures FROM sg_resource_circuits').fetchone()[0] == 3
    assert not any(job_lifecycle.prune_orphans(store, apply=True)['orphan_rows'].values())


def test_repair_rejects_mismatched_project_and_readonly_apply(env):
    _, store, _, _, _ = env
    readonly = Store(store.config, readonly=True)
    try:
        with pytest.raises((ValueError, PermissionError)):
            job_lifecycle.prune_orphans(readonly, apply=True)
    finally:
        readonly.close()
    store.config['project_id'] = 'wrong-project'
    with pytest.raises(ValueError, match='project_id'):
        job_lifecycle.prune_orphans(store, apply=True)


def test_missing_parent_table_is_not_permission_to_prune_or_create_optional_tables(env):
    _, store, _, _, _ = env
    before = [tuple(r) for r in store.db.execute('SELECT name,sql FROM sqlite_master ORDER BY name')]
    job_lifecycle.setup(store.db)
    assert before == [tuple(r) for r in store.db.execute('SELECT name,sql FROM sqlite_master ORDER BY name')]
    store.db.execute('CREATE TABLE sg2_recoveries(job TEXT,before_json TEXT)')
    store.db.execute("INSERT INTO sg2_recoveries VALUES('orphan','keep')")
    store.db.commit()
    store.db.execute('PRAGMA foreign_keys=OFF')
    store.db.execute('DROP TABLE sg2_jobs')
    store.db.commit()
    assert job_lifecycle.prune_orphans(store, apply=True)['orphan_rows'] == {}
    assert store.db.execute('SELECT before_json FROM sg2_recoveries').fetchone()[0] == 'keep'


def test_prune_failure_is_atomic(env):
    _, store, _, _, _ = env
    ledgers(store)
    seed(store, 'missing')
    before = snapshot(store, 'missing')
    store.db.execute("CREATE TRIGGER fail_prune BEFORE DELETE ON sg_resource_requests BEGIN SELECT RAISE(ABORT,'test_failure'); END")
    store.db.commit()
    with pytest.raises(sqlite3.IntegrityError, match='test_failure'):
        job_lifecycle.prune_orphans(store, apply=True)
    assert snapshot(store, 'missing') == before


def test_late_settlement_cannot_recreate_deleted_budget(resource_env):
    store, client = resource_env
    ticket = reserve(store, client)
    with store.db:
        store.db.execute("DELETE FROM sg2_jobs WHERE id='job'")
    finish(store, ticket)
    for table in ('sg_resource_scopes', 'sg_resource_requests'):
        assert not store.db.execute(f'SELECT 1 FROM {table}').fetchone()
    assert store.db.execute('SELECT failures FROM sg_resource_circuits').fetchone()[0] == 0


def test_readonly_open_does_not_install_guards_or_prune(env):
    _, store, _, _, _ = env
    ledgers(store)
    seed(store, 'historical-missing')
    drop_guards(store)
    readonly = Store(store.config, readonly=True)
    try:
        assert job_lifecycle.prune_orphans(readonly)['orphan_rows']['sg2_recoveries'] == 1
        assert not readonly.db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name LIKE 'sg_job_delete_%'").fetchone()
        assert readonly.db.total_changes == 0
    finally:
        readonly.close()


def test_cli_preview_and_apply_use_same_owner(env, capsys):
    from sessiongraph.cli import main
    cfg, store, _, _, _ = env
    ledgers(store)
    seed(store, 'missing')
    args = ['--config', str(cfg), 'cleanup-orphan-job-ledgers']
    assert main(args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview['applied'] is False and preview['orphan_rows']['sg2_recoveries'] == 1
    assert main([*args, '--apply']) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied['applied'] and applied['model_calls'] == 0
    assert all(not rows for rows in snapshot(store, 'missing').values())


def test_packaging_includes_job_lifecycle():
    config = json.loads((Path(__file__).resolve().parents[2] / 'src-tauri/tauri.conf.json').read_text())
    assert config['bundle']['resources']['../runtime/sessiongraph/job_lifecycle.py'] == 'sessiongraph/sessiongraph/job_lifecycle.py'


def test_source_trigger_upgrade_is_atomic_and_unchanged_setup_avoids_ddl(env):
    from sessiongraph import source_text
    _, store, _, _, _ = env
    with store.db:
        store.db.execute('DROP TRIGGER sg_source_search_delete')
        store.db.execute('CREATE TRIGGER sg_source_search_delete BEFORE DELETE ON entries BEGIN SELECT 1; END')
    observer = sqlite3.connect(store.config['database'])
    seen = []
    during_replacement = []
    def trace(sql):
        seen.append(sql)
        if sql.startswith('DROP TRIGGER'):
            during_replacement.append((store.db.in_transaction,
                observer.execute("SELECT sql FROM sqlite_master WHERE name='sg_source_search_delete'").fetchone()[0]))
    store.db.set_trace_callback(trace)
    try:
        source_text.setup(store)
        assert any(sql.startswith('DROP TRIGGER') for sql in seen)
        assert len(during_replacement) == 1
        assert during_replacement[0][0] and 'SELECT 1' in during_replacement[0][1]
        assert 'sg_search_keys' in observer.execute("SELECT sql FROM sqlite_master WHERE name='sg_source_search_delete'").fetchone()[0]
        seen.clear()
        source_text.setup(store)
        assert not any(sql.startswith(('DROP TRIGGER', 'CREATE TRIGGER sg_source_search_delete')) for sql in seen)
    finally:
        store.db.set_trace_callback(None)
        observer.close()
