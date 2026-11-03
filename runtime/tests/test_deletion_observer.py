"""Permanent deletion without SessionEnd, using real SQLite and macOS vnode events."""
import json
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import time

import pytest

from sessiongraph import deletion_observer as observer
from sessiongraph.core import Store
from test_chat_cleanup import env, publish
from test_global_hooks import hook_module

CHAT = '01a11ac0-be78-7f63-8b59-0ff0665252b0'
OTHER = '01a0db3b-7ce9-7603-8fad-739eed0eaeae'


@pytest.fixture
def desktop(env, tmp_path, monkeypatch):
    cfg, store, source, other, archive = env
    with store.db:
        store.db.execute('UPDATE session_chats SET chat_id=? WHERE chat_id=?', (CHAT, 'chat-a'))
    codex = tmp_path / 'codex'
    codex.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(codex))
    state = codex / '.codex-global-state.json'
    state.write_text(json.dumps({'electron-persisted-atom-state': {}}))
    db = sqlite3.connect(codex / 'state_5.sqlite')
    db.execute('CREATE TABLE threads(id TEXT PRIMARY KEY)')
    db.commit()
    db.close()
    return cfg, store, source, other, state


def mark(state, ids, *, atomic=False):
    payload = json.dumps({'electron-persisted-atom-state': {
        observer.PREFIX + chat: True for chat in ids}})
    target = state.with_suffix('.tmp') if atomic else state
    target.write_text(payload)
    if atomic:
        target.replace(state)


def test_markers_are_strict_and_bounded(desktop):
    *_, state = desktop
    state.write_text(json.dumps({'electron-persisted-atom-state': {
        observer.PREFIX + CHAT: True, observer.PREFIX + OTHER: 1,
        observer.PREFIX + 'not-a-chat': True}}))
    assert observer.deleted_ids(state) == {CHAT}
    state.write_text(' ' * (observer.MAX_STATE_BYTES + 1))
    with pytest.raises(ValueError, match='limit'):
        observer.deleted_ids(state)


def test_failure_receipt_keeps_safe_reason_and_redacts_arbitrary_text():
    safe = observer.failure_result(CHAT, ValueError('cleanup_shared_node_without_evidence'))
    assert safe['reason'] == 'cleanup_shared_node_without_evidence'
    assert safe['model_calls'] == 0
    unsafe = observer.failure_result(CHAT, ValueError(
        'Authorization: Bearer sk-private transcript text'))
    encoded = json.dumps(unsafe)
    assert unsafe['reason'] == 'graph_validation'
    assert 'sk-private' not in encoded and 'transcript text' not in encoded


def test_marker_is_not_erasure_authority(desktop):
    cfg, store, source, _, state = desktop
    publish(store, CHAT, 'node-a')
    assert observer.check(cfg, CHAT, set())['reason'] == 'no_delete_marker'
    assert observer.check(cfg, CHAT, {CHAT})['reason'] == 'source_present'
    source.unlink()
    db = sqlite3.connect(state.parent / 'state_5.sqlite')
    db.execute('INSERT INTO threads VALUES(?)', (CHAT,))
    db.commit()
    db.close()
    assert observer.check(cfg, CHAT, {CHAT})['reason'] == 'codex_chat_present'
    assert store.db.execute('SELECT 1 FROM work_memory WHERE id=?', ('node-a',)).fetchone()


def test_targeted_cleanup_keeps_shared_evidence_and_other_chat(desktop):
    cfg, store, source, other, _ = desktop
    publish(store, CHAT, 'shared')
    publish(store, 'chat-b', 'shared')
    publish(store, CHAT, 'only-a', 'deleted fact')
    source.unlink()
    result = observer.check(cfg, CHAT, {CHAT})
    assert result['cleaned'] and result['model_calls'] == 0
    assert store.db.execute('SELECT chat_id FROM work_memory WHERE id=?', ('shared',)).fetchone()[0] == 'chat-b'
    assert not store.db.execute('SELECT 1 FROM work_memory WHERE id=?', ('only-a',)).fetchone()
    assert other.exists() and not store.db.execute('PRAGMA foreign_key_check').fetchall()
    assert observer.check(cfg, CHAT, {CHAT})['reason'] == 'unindexed_source'


def test_unavailable_backend_or_root_never_authorizes_delete(desktop):
    cfg, store, source, _, state = desktop
    publish(store, CHAT, 'node-a')
    source.parent.rename(source.parent.with_name('offline'))
    assert observer.check(cfg, CHAT, {CHAT})['reason'] == 'source_unavailable'
    (state.parent / 'state_5.sqlite').unlink()
    with pytest.raises(sqlite3.OperationalError):
        observer.check(cfg, CHAT, {CHAT})
    assert store.db.execute('SELECT 1 FROM work_memory WHERE id=?', ('node-a',)).fetchone()


def test_project_disable_and_foreign_chat_are_noops(desktop):
    cfg, store, source, _, _ = desktop
    publish(store, CHAT, 'node-a')
    source.unlink()
    assert observer.check(cfg, OTHER, {OTHER})['reason'] == 'unindexed_source'
    (cfg.parent / 'disabled').touch()
    assert observer.check(cfg, CHAT, {CHAT})['reason'] == 'disabled'
    assert store.db.execute('SELECT 1 FROM work_memory WHERE id=?', ('node-a',)).fetchone()


def test_global_uninstall_stops_cleanup_even_with_local_opt_in(desktop):
    cfg, store, source, _, state = desktop
    publish(store, CHAT, 'node-a')
    source.unlink()
    (state.parent / 'sessiongraph.json').write_text(json.dumps({'enabled': False}))
    assert observer.check(cfg, CHAT, {CHAT})['reason'] == 'disabled'
    assert store.db.execute('SELECT 1 FROM work_memory WHERE id=?', ('node-a',)).fetchone()


def test_global_uninstall_stops_explicit_batch_cleanup(desktop):
    cfg, store, source, other, state = desktop
    with store.db:
        store.db.execute('UPDATE session_chats SET chat_id=? WHERE chat_id=?', (OTHER, 'chat-b'))
    publish(store, CHAT, 'node-a')
    publish(store, OTHER, 'node-b')
    source.unlink(); other.unlink()
    (state.parent / 'sessiongraph.json').write_text(json.dumps({'enabled': False}))
    result = observer.confirmed_many(cfg, [CHAT, OTHER], {CHAT, OTHER})
    assert result['reason'] == 'disabled' and not result['cleaned']
    assert store.db.execute('SELECT 1 FROM work_memory WHERE id=?', ('node-a',)).fetchone()
    assert store.db.execute('SELECT 1 FROM work_memory WHERE id=?', ('node-b',)).fetchone()


def test_observer_admission_failure_does_not_suppress_restore(desktop, monkeypatch):
    import io
    cfg, store, _, _, _ = desktop
    hook = hook_module()
    monkeypatch.setattr(hook, 'GLOBAL_SETTINGS', cfg.parent / 'absent-settings.json')
    monkeypatch.setattr(observer, 'start', lambda *_: (_ for _ in ()).throw(OSError('fixture')))
    monkeypatch.setattr(hook, '_pending_restore', lambda *_: 'token')
    monkeypatch.setattr(hook, '_extract_context', lambda *_: 'preserved context')
    monkeypatch.setattr(hook, '_ack_restore', lambda *_: True)
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({'hook_event_name': 'UserPromptSubmit',
        'session_id': CHAT, 'cwd': store.config['roots'][0]})))
    output = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', output)
    assert hook.main() == 0
    assert json.loads(output.getvalue())['hookSpecificOutput']['additionalContext'] == 'preserved context'


@pytest.mark.skipif(not hasattr(select, 'kqueue'), reason='macOS vnode integration')
@pytest.mark.parametrize('atomic', [False, True])
def test_delete_event_cleans_without_hook_and_preserves_startup_history(desktop, atomic, monkeypatch):
    cfg, store, source, other, state = desktop
    publish(store, CHAT, 'node-a')
    # Existing historical tombstones must not cause a startup cleanup sweep.
    mark(state, [OTHER])
    source.unlink()
    runtime = Path(observer.__file__).resolve().parents[1]
    proc = subprocess.Popen([sys.executable, '-B', '-m', 'sessiongraph.deletion_observer',
        '--config', str(cfg)], cwd=runtime, env={**os.environ, 'PYTHONPATH': str(runtime)},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ready = observer.folder(cfg) / 'deletion-observer.json'
        deadline = time.monotonic() + 8
        while not ready.exists() and time.monotonic() < deadline:
            assert proc.poll() is None
            time.sleep(.02)
        assert ready.exists()
        with monkeypatch.context() as context:
            context.setattr(observer.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('singleton already running'))
            observer.start(cfg)
        assert store.db.execute('SELECT 1 FROM work_memory WHERE id=?', ('node-a',)).fetchone()
        mark(state, [OTHER, CHAT], atomic=atomic)
        receipt = observer.folder(cfg) / 'last-delete-check.json'
        while not receipt.exists() and time.monotonic() < deadline:
            assert proc.poll() is None
            time.sleep(.02)
        assert json.loads(receipt.read_text())['cleaned']
        assert not store.db.execute('SELECT 1 FROM work_memory WHERE id=?', ('node-a',)).fetchone()
        assert other.exists()
        assert not store.db.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        proc.terminate()
        proc.communicate(timeout=5)


@pytest.mark.skipif(not hasattr(select, 'kqueue'), reason='macOS vnode integration')
def test_explicit_batch_delete_cleans_shared_and_own_nodes_in_one_transaction(desktop):
    cfg, store, source, other, state = desktop
    with store.db:
        store.db.execute('UPDATE session_chats SET chat_id=? WHERE chat_id=?', (OTHER, 'chat-b'))
    publish(store, CHAT, 'shared', 'deleted A statement')
    publish(store, OTHER, 'shared', 'deleted B statement')
    publish(store, OTHER, 'only-b', 'deleted B fact')
    source.unlink(); other.unlink()
    mark(state, [CHAT, OTHER])
    ids = observer.deleted_ids(state)
    result = observer.confirmed_many(cfg, [CHAT, OTHER], ids)
    assert result['cleaned'] and result['model_calls'] == 0
    assert not store.db.execute("SELECT 1 FROM work_memory WHERE id IN ('shared','only-b')").fetchall()
    assert store.db.execute('SELECT count(*) FROM entries').fetchone()[0] == 0
    assert not store.db.execute('PRAGMA foreign_key_check').fetchall()
    assert store.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
