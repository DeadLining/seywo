from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest

from seywo import inbox
from seywo.config import initialize, load_config
from seywo.store import Store


DELETED = '11111111-1111-1111-1111-111111111111'
KEPT = '22222222-2222-2222-2222-222222222222'
QUEUED = '33333333-3333-3333-3333-333333333333'
PREFIX = 'codex-writing-block-deleted-thread-v1:'


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project'
    config = load_config(Path(initialize(root, plugin_hook=True)['config']))
    host = tmp_path / 'codex'
    host.mkdir()
    with closing(sqlite3.connect(host / 'state_5.sqlite')) as db, db:
        db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, archived INTEGER)')
        db.execute('INSERT INTO threads VALUES (?,1)', (KEPT,))
    with closing(sqlite3.connect(host / '.codex-global-state.sqlite')) as db, db:
        db.execute('CREATE TABLE settings (scope TEXT, key TEXT, value TEXT)')
        db.executemany('INSERT INTO settings VALUES (?,?,?)', [
            ('electron-owner-atoms:test', PREFIX + chat, 'true') for chat in (DELETED, KEPT, QUEUED)
        ])
    for chat in (DELETED, KEPT, QUEUED):
        source = host / f'{chat}.jsonl'
        source.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': chat, 'cwd': str(root)}}) + '\n')
        item = inbox.record(config, chat, 'PreCompact', str(source))
        if chat != QUEUED:
            with Store(config) as store:
                store.import_compact(item['snapshot'], chat, str(source), dialogue=True)
            inbox.finish(config, inbox.claim(config, item), state='done')
    return config, host


def test_sync_removes_only_confirmed_project_chats_and_all_their_pending_work(project):
    from seywo.chat_deletion import reconcile

    config, host = project
    state = Path(config['_path']).parent
    for folder in ('maintain', 'maintenance'):
        (state / folder).mkdir(exist_ok=True)
        (state / folder / f'{DELETED}.json').write_text('{}')
    result = reconcile(config, host)
    assert result['deleted'] == 2 and result['model_calls'] == 0
    assert result['storage']['removed_blocks'] == 2
    assert reconcile(config, host)['deleted'] == 0
    with Store(config, read_only=True) as store:
        assert [r[0] for r in store.db.execute('SELECT id FROM chats')] == [KEPT]
        assert {r[0] for r in store.db.execute('SELECT chat_id FROM tombstones')} == {DELETED, QUEUED}
    assert not inbox.pending(config, QUEUED)
    assert not list((state / 'inbox' / 'live').glob(f'{DELETED}*.jsonl'))
    assert not list((state / 'inbox' / 'live').glob(f'{QUEUED}*.jsonl'))
    assert list((state / 'inbox' / 'live').glob(f'{KEPT}*.jsonl'))
    assert not (state / 'maintain' / f'{DELETED}.json').exists()
    assert not (state / 'maintenance' / f'{DELETED}.json').exists()
    assert (host / f'{DELETED}.jsonl').is_file()
    late = inbox.record(config, DELETED, 'PreCompact', str(host / f'{DELETED}.jsonl'))
    assert late['state'] == 'excluded'
    assert not list((state / 'inbox' / 'live').glob(f'{DELETED}*.jsonl'))


def test_unavailable_backend_does_not_authorize_delete(project):
    from seywo.chat_deletion import reconcile

    config, host = project
    (host / 'state_5.sqlite').unlink()
    assert reconcile(config, host)['state'] == 'unavailable'
    with Store(config, read_only=True) as store:
        assert store.db.execute('SELECT count(*) FROM chats').fetchone()[0] == 2
        assert store.db.execute('SELECT count(*) FROM tombstones').fetchone()[0] == 0


def test_absent_host_row_without_marker_is_not_a_deletion(project):
    from seywo.chat_deletion import reconcile

    config, host = project
    with closing(sqlite3.connect(host / '.codex-global-state.sqlite')) as db, db:
        db.execute('DELETE FROM settings')
    assert reconcile(config, host)['deleted'] == 0
    assert inbox.pending(config, QUEUED)
    with Store(config, read_only=True) as store:
        assert not store.chat_deleted(DELETED)
        assert store.db.execute('SELECT 1 FROM chats WHERE id=?', (DELETED,)).fetchone()


def test_reconciliation_defers_while_chat_writer_owns_lifecycle(project):
    from seywo.chat_deletion import reconcile
    from seywo.locking import lifecycle

    config, host = project
    with lifecycle(config):
        assert reconcile(config, host)['state'] == 'deferred'
        assert inbox.pending(config, QUEUED)
    assert reconcile(config, host)['deleted'] == 2


def test_reconciliation_never_opens_an_old_index_for_writing(project):
    from seywo.chat_deletion import reconcile
    from seywo.store import SCHEMA_VERSION

    config, host = project
    older = SCHEMA_VERSION - 1
    with closing(sqlite3.connect(config['_database'])) as db, db:
        db.execute(f'PRAGMA user_version={older}')
    result = reconcile(config, host)
    assert result['state'] == 'deferred' and result['deleted'] == 0
    with closing(sqlite3.connect(config['_database'])) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == older
        assert not db.execute('SELECT 1 FROM tombstones WHERE chat_id=?', (DELETED,)).fetchone()


def test_legacy_markers_require_a_true_value_and_do_not_tombstone_foreign_chats(project):
    from seywo.chat_deletion import reconcile

    config, host = project
    (host / '.codex-global-state.sqlite').unlink()
    foreign = '44444444-4444-4444-4444-444444444444'
    (host / '.codex-global-state.json').write_text(json.dumps({'electron-persisted-atom-state': {
        PREFIX + DELETED: True, PREFIX + QUEUED: 1, PREFIX + foreign: True,
    }}))
    assert reconcile(config, host)['deleted'] == 1
    assert inbox.pending(config, QUEUED)
    with Store(config, read_only=True) as store:
        assert {r[0] for r in store.db.execute('SELECT chat_id FROM tombstones')} == {DELETED}


def test_reclaim_retries_without_repeating_logical_deletion(project, monkeypatch):
    from seywo import storage_lifecycle
    from seywo.chat_deletion import reconcile

    config, host = project
    guard = Path(config['_path']).parent / 'deletion-reclaim.pending'
    with monkeypatch.context() as patch:
        patch.setattr(storage_lifecycle, 'safe_collect', lambda *a, **kw: {'state': 'failed'})
        assert reconcile(config, host)['state'] == 'failed'
        assert guard.exists()
    with closing(sqlite3.connect(host / '.codex-global-state.sqlite')) as db, db:
        db.execute('DELETE FROM settings')
    result = reconcile(config, host)
    assert result['deleted'] == 0 and result['storage']['removed_blocks'] == 2
    assert not guard.exists() and not inbox.pending(config, QUEUED)
