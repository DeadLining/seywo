import hashlib
import json
from pathlib import Path
import socket
import time

import pytest

from sessiongraph import maintenance, run_receipts as receipts
from sessiongraph.auto_update import _atomic, _folder, run_update, update_status
from sessiongraph.core import Store, init_project, load_config


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.delenv('SESSIONGRAPH_WORKER_LOCK', raising=False)
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex'))
    monkeypatch.setattr(socket, 'socket', lambda *a, **k: pytest.fail('network call'))
    path = init_project(tmp_path)
    config = load_config(path)
    Store(config).close()
    return path, config


def put(config, identity='done', state='succeeded', **changes):
    now = time.time()
    run = dict(run_id=identity, chat_id='chat', state=state,
               created_at=now, updated_at=now, padding='evidence\n' * 4000)
    run.update(changes)
    path = _folder(config) / (identity + '.json')
    _atomic(path, run)
    return path, path.read_bytes()


def test_explicit_lossless_archive_preserves_status_and_old_id(case):
    _, config = case
    path, raw = put(config)
    before = update_status(config)
    preview = maintenance.maintain_receipts(config)
    assert preview['eligible'] == 1 and preview['archive_enabled']
    assert path.read_bytes() == raw
    report = maintenance.maintain_receipts(config, apply=True)
    assert report['archived'] == 1 and report['model_calls'] == 0
    assert not path.exists() and receipts.read(config, 'done').encode() == raw
    assert update_status(config) == before
    with receipts.connection(config) as db:
        row = db.execute('SELECT * FROM sg_run_receipts').fetchone()
        assert row['source_hash'] == hashlib.sha256(raw).hexdigest()
        assert row['payload'].startswith('{"_sessiongraph_payload_v1":')
        assert db.execute('SELECT count(*) FROM sg_payload_archive').fetchone()[0] == 1
    assert maintenance.maintain_receipts(config, apply=True)['archived'] == 0


@pytest.mark.parametrize('state', ['queued', 'importing', 'extracting', 'retry_wait', 'pending'])
def test_active_and_pending_receipts_stay_hot(case, state):
    _, config = case
    path, raw = put(config, state=state)
    report = maintenance.maintain_receipts(config, apply=True)
    assert report['skipped'] == 1 and report['archived'] == 0
    assert path.read_bytes() == raw


def test_crash_after_commit_keeps_verified_copies_and_is_idempotent(case, monkeypatch):
    _, config = case
    path, raw = put(config)
    unlink = Path.unlink
    def fail_unlink(self, *a, **k):
        if self == path:
            raise OSError('injected crash boundary')
        return unlink(self, *a, **k)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', fail_unlink)
        report = maintenance.maintain_receipts(config, apply=True)
    assert len(report['errors']) == 1 and path.read_bytes() == raw
    assert len(update_status(config)['runs']) == 1
    assert maintenance.maintain_receipts(config, apply=True)['archived'] == 1
    assert receipts.read(config, 'done').encode() == raw


@pytest.mark.parametrize('damage', ['blob', 'hash', 'metadata'])
def test_corrupt_archive_fails_explicitly(case, damage):
    _, config = case
    put(config)
    maintenance.maintain_receipts(config, apply=True)
    with receipts.connection(config, readonly=False) as db, db:
        if damage == 'blob':
            db.execute("UPDATE sg_payload_archive SET data=x'00'")
        elif damage == 'hash':
            db.execute("UPDATE sg_run_receipts SET source_hash='wrong'")
        else:
            db.execute("UPDATE sg_run_receipts SET state='cancelled'")
    with pytest.raises(ValueError):
        receipts.read(config, 'done')
    result = update_status(config)
    assert result['unreadable_runs'] == 1 and result['latest'] is None


def test_same_id_different_content_keeps_hot_original(case):
    _, config = case
    put(config)
    maintenance.maintain_receipts(config, apply=True)
    path, raw = put(config, padding='different result')
    report = maintenance.maintain_receipts(config, apply=True)
    assert len(report['errors']) == 1 and path.read_bytes() == raw


def test_budget_cursor_and_malformed_receipt_are_visible(case):
    _, config = case
    paths = [put(config, identity=x)[0] for x in ['a', 'b', 'c']]
    first = maintenance.maintain_receipts(config, apply=True, limit=1)
    assert first['next_cursor'] == 'a.json' and first['more']
    small = maintenance.maintain_receipts(config, apply=True, max_bytes=1, after=first['next_cursor'])
    assert small['budget_exhausted'] and small['archived'] == 0 and paths[1].exists()
    second = maintenance.maintain_receipts(config, apply=True, after=first['next_cursor'])
    assert second['archived'] == 2 and not second['more']
    bad = _folder(config) / 'bad.json'
    bad.write_text('invalid json')
    report = maintenance.maintain_receipts(config, apply=True)
    assert report['errors'][0]['file'] == 'bad.json' and bad.exists()


def test_cleared_receipts_archive_losslessly_without_resurrecting_status(case):
    _, config = case
    path, raw = put(config, state='failed')
    cleared = path.parent.parent / 'cleared-runs'
    cleared.mkdir()
    path.rename(cleared / path.name)
    report = maintenance.maintain_receipts(config, apply=True, cleared=True)
    assert report['archived'] == 1 and not report['errors']
    assert receipts.read(config, 'done').encode() == raw
    assert not update_status(config)['runs']
    with receipts.connection(config) as db:
        assert db.execute('SELECT hidden FROM sg_run_receipts').fetchone()[0] == 1


def test_automatic_archive_is_default_without_project_opt_in(case):
    path, config = case
    def execute(argv, timeout):
        return {'added_entries': 0, 'skipped': []} if 'refresh-chat' in argv else {
            'saved': [], 'failed': [], 'model_calls': 0}
    first = run_update(path, 'chat', execute=execute)
    assert not (_folder(config) / (first['run_id'] + '.json')).exists()
    maintenance.maintain_receipts(config, apply=True)
    second = run_update(path, 'chat', execute=execute)
    assert not (_folder(config) / (second['run_id'] + '.json')).exists()
    assert json.loads(receipts.read(config, second['run_id'])) == second
    assert update_status(config)['latest']['run_id'] == second['run_id']


def test_dismissed_cold_failure_is_not_resurrected_but_remains_expandable(case):
    path, config = case
    _, raw = put(config, state='failed')
    maintenance.maintain_receipts(config, apply=True)
    report = maintenance.run(path, 'clear_tasks')
    assert report['archived_runs'] == 1
    assert update_status(config)['latest'] is None
    assert receipts.read(config, 'done').encode() == raw
    assert maintenance.run(path, 'clear_tasks')['archived_runs'] == 0


def test_cold_queries_are_bounded_read_only_and_project_scoped(case):
    _, config = case
    for i in range(120):
        put(config, str(i))
    maintenance.maintain_receipts(config, apply=True, limit=1000)
    with receipts.connection(config) as db:
        before = db.total_changes
        rows = receipts.recent(db, config['project_id'])
        assert len(rows) == 100 and not receipts.recent(db, 'other-project')
        assert db.total_changes == before
        plan = str([tuple(x) for x in db.execute('EXPLAIN QUERY PLAN SELECT * FROM sg_run_receipts '
            'WHERE project=? AND hidden=0 ORDER BY modified DESC,run_id LIMIT 100', (config['project_id'],))])
        assert 'sg_run_receipts_recent' in plan and 'TEMP B-TREE' not in plan
    assert len(update_status(config)['runs']) == 8


def test_maintenance_respects_active_writer_lock(case):
    import fcntl
    _, config = case
    path, raw = put(config)
    with (_folder(config).parent / 'update.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match='maintenance_busy'):
            maintenance.maintain_receipts(config, apply=True)
        assert maintenance.maintain_receipts(config)['eligible'] == 1
    assert path.read_bytes() == raw


def test_archive_transaction_failure_preserves_original_and_rolls_back_blob(case, monkeypatch):
    from sessiongraph import storage
    _, config = case
    path, raw = put(config)
    archive = storage.archive_text
    def fail_after_insert(*args, **kwargs):
        archive(*args, **kwargs)
        raise OSError('injected commit failure')
    monkeypatch.setattr(storage, 'archive_text', fail_after_insert)
    report = maintenance.maintain_receipts(config, apply=True)
    assert len(report['errors']) == 1 and path.read_bytes() == raw
    with receipts.connection(config) as db:
        assert db.execute('SELECT count(*) FROM sg_run_receipts').fetchone()[0] == 0
        if receipts.storage.exists(db, 'sg_payload_archive'):
            assert db.execute('SELECT count(*) FROM sg_payload_archive').fetchone()[0] == 0


def test_read_during_unlink_falls_back_to_committed_archive(case, monkeypatch):
    _, config = case
    path, raw = put(config)
    maintenance.maintain_receipts(config, apply=True)
    path.write_bytes(raw)
    open_file = Path.open
    def disappearing(self, *a, **k):
        if self == path and self.exists():
            self.unlink()
        return open_file(self, *a, **k)
    monkeypatch.setattr(Path, 'open', disappearing)
    assert receipts.read(config, 'done').encode() == raw


def test_oversize_receipt_cannot_block_following_files(case):
    _, config = case
    big = _folder(config) / 'a.json'
    big.parent.mkdir(parents=True, exist_ok=True)
    with big.open('wb') as stream:
        stream.truncate(65 * 1024 * 1024)
    put(config, 'b')
    report = maintenance.maintain_receipts(config, apply=True)
    assert report['archived'] == 1 and report['errors'][0]['file'] == 'a.json'
    assert big.stat().st_size == 65 * 1024 * 1024


def test_receipt_symlink_never_imports_external_content(case, tmp_path):
    _, config = case
    external = tmp_path / 'external.json'
    external.write_text('not a project receipt')
    path = _folder(config) / 'outside.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(external)
    report = maintenance.maintain_receipts(config, apply=True)
    assert report['errors'][0]['file'] == 'outside.json' and path.is_symlink()
    with pytest.raises(ValueError, match='symlink'):
        receipts.read(config, 'outside')


def test_malformed_legacy_optional_fields_do_not_escape_status_validation(case):
    _, config = case
    put(config, state='failed', error_code='extraction_partial_failure',
        failed_batches=0, dropped_oversize='invalid')
    result = update_status(config)
    assert result['unreadable_runs'] == 1 and result['latest'] is None
