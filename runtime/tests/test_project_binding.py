import json
import sqlite3
from pathlib import Path

import pytest

from sessiongraph.core import Store, init_project, load_config
from sessiongraph.continuity import refresh_chat
from sessiongraph.import_diagnostics import summarize_import
from sessiongraph.auto_update import run_update


def event(kind, payload):
    return json.dumps({'type': kind, 'payload': payload}) + '\n'


def message(text):
    return event('response_item', {'type': 'message', 'role': 'user',
        'content': [{'type': 'input_text', 'text': text}]})


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / 'project'; root.mkdir()
    logs = tmp_path / 'logs'; logs.mkdir()
    home = tmp_path / 'codex'; home.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(home))
    cfg_path = init_project(root)
    cfg = json.loads(cfg_path.read_text())
    cfg['hook']['allowed_transcript_roots'] = [str(logs)]
    cfg_path.write_text(json.dumps(cfg))
    cfg = load_config(cfg_path)
    store = Store(cfg)
    db = sqlite3.connect(home / 'state_5.sqlite')
    db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT)')
    source = logs / 'renamed-source.jsonl'
    def register(cwd=None, chat='chat'):
        db.execute('INSERT OR REPLACE INTO threads VALUES (?,?,?)',
                   (chat, str(source), str(cwd or root)))
        db.commit()
    yield store, source, root, register, cfg_path
    store.close(); db.close()


def test_hook_path_works_without_filename_id_or_directory_scan(project, monkeypatch):
    store, source, root, _, _ = project
    source.write_text(event('session_meta', {'id': 'chat', 'cwd': str(root)})+message('retained'))
    def forbidden(*a, **kw):
        raise AssertionError('Hook fast path must never glob history')
    monkeypatch.setattr(Path, 'glob', forbidden)
    result = refresh_chat(store, 'chat', str(source))
    assert result['added_entries'] == 1
    assert result['discovery'] == 'hook_path'
    assert result['candidate_count'] == 1
    assert result['model_calls'] == 0


def test_moved_chat_indexes_only_explicit_project_turns(project):
    store, source, root, register, _ = project
    other = root.parent / 'other'
    register()
    source.write_text(event('session_meta', {'id': 'chat', 'cwd': str(other)})
        + message('foreignsecret')
        + event('turn_context', {'cwd': str(root)}) + message('localneedle')
        + event('turn_context', {'cwd': str(other)}) + message('outsideagain')
        + event('turn_context', {'cwd': str(root)}) + message('localagain'))
    result = refresh_chat(store, 'chat', str(source))
    assert result['added_entries'] == 2
    imported = result['imports'][0]
    assert imported['first_project_line'] == 3
    assert imported['excluded_records'] == 4
    assert summarize_import(result)[1] is None
    text = '\n'.join(r[0] for r in store.db.execute('SELECT event_json FROM raw_events'))
    assert 'foreignsecret' not in text and 'outsideagain' not in text
    assert store.search('localneedle')['hits']
    assert not store.search('foreignsecret')['hits']
    assert refresh_chat(store, 'chat', str(source))['added_entries'] == 0


def test_scope_persists_at_cursor_across_imports(project):
    store, source, root, register, _ = project
    other = root.parent / 'other'; register()
    source.write_text(event('session_meta', {'id': 'chat', 'cwd': str(other)})
        + event('turn_context', {'cwd': str(root)})+message('here')
        + event('turn_context', {'cwd': str(other)}))
    assert refresh_chat(store, 'chat', str(source))['added_entries'] == 1
    with source.open('a') as f:
        f.write(message('stilloutside')+event('turn_context', {'cwd': str(root)})+message('backhere'))
    other_store = Store(store.config)
    try:
        assert refresh_chat(other_store, 'chat', str(source))['added_entries'] == 1
        assert not other_store.search('stilloutside')['hits']
        assert other_store.search('backhere')['hits']
    finally:
        other_store.close()


def test_current_binding_alone_does_not_authorize_old_history(project):
    store, source, root, register, _ = project
    register()
    source.write_text(event('session_meta', {'id': 'chat', 'cwd': str(root.parent/'other')})
                      + message('foreignonly'))
    result = refresh_chat(store, 'chat', str(source))
    assert result['added_entries'] == 0
    assert summarize_import(result)[1] == 'import_project_boundary_unconfirmed'
    assert store.db.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 0
    with source.open('a') as f:
        f.write(event('turn_context', {'cwd': str(root)})+message('nowconfirmed'))
    result = refresh_chat(store, 'chat', str(source))
    assert summarize_import(result)[1] is None
    assert result['added_entries'] == 1


@pytest.mark.parametrize('bad', ['wrong_chat','outside_allowlist','unbound'])
def test_invalid_identity_path_or_binding_still_rejected(project, bad):
    store, source, root, register, _ = project
    register()
    cwd=root; chat='chat'
    if bad=='wrong_chat':chat='another'
    if bad=='outside_allowlist':source=root/'external.jsonl'
    if bad=='unbound':
        cwd=root.parent/'other'; register(cwd)
    source.write_text(event('session_meta', {'id': chat, 'cwd': str(cwd)})+message('neverimport'))
    result=refresh_chat(store,'chat',str(source))
    assert result['added_entries']==0 and result['skipped']
    assert store.db.execute('SELECT count(*) FROM entries').fetchone()[0]==0


def test_hook_passes_transcript_to_import_and_extraction(project):
    _, source, _, _, cfg = project
    calls=[]
    def execute(argv, timeout):
        calls.append(argv)
        if 'refresh-chat' in argv:
            return {'imports':[{'added':0}], 'skipped':[], 'added_entries':0}
        return {'saved':[], 'failed':[], 'model_calls':0}
    result=run_update(cfg,'chat','PostCompact',str(source),execute)
    assert result['state']=='succeeded'
    assert calls[0][calls[0].index('--transcript-path')+1]==str(source)
    assert calls[1][calls[1].index('--memories-transcript-path')+1]==str(source)


def test_prior_binding_survives_new_segment_but_not_project_move(project, monkeypatch):
    store, source, root, register, _ = project
    register()
    source.write_text(event('session_meta', {'id': 'chat', 'cwd': str(root.parent/'other')})
                      + event('turn_context', {'cwd': str(root)}) + message('local'))
    assert refresh_chat(store, 'chat', str(source))['added_entries'] == 1
    from sessiongraph import project_binding
    monkeypatch.setattr(project_binding, 'current_transcript', lambda *_: source.with_name('new-segment.jsonl'))
    assert refresh_chat(store, 'chat', str(source))['skipped'] == []
    monkeypatch.setattr(project_binding, 'current_transcript', lambda *_: None)
    result = refresh_chat(store, 'chat', str(source))
    assert result['skipped'][0]['code'] == 'outside_project'


def test_scope_checkpoint_rolls_back_with_failed_batch(project, monkeypatch):
    store, source, root, register, _ = project
    from sessiongraph import ingestion
    monkeypatch.setattr(ingestion, 'BATCH_RECORDS', 2)
    register()
    source.write_text(event('session_meta', {'id': 'chat', 'cwd': str(root.parent/'other')})
                      + message('outside')
                      + event('turn_context', {'cwd': str(root)}) + '{invalid}\n')
    with pytest.raises(ValueError):
        store.ingest(source)
    state=json.loads(store.db.execute("SELECT value FROM metadata WHERE key='ingest_scope:chat'").fetchone()[0])
    assert state['admitted'] is False and state['excluded_records'] == 2
    assert store.db.execute('SELECT lines FROM sessions').fetchone()[0] == 2


def test_unchanged_refresh_does_not_write_checkpoints(project):
    store, source, root, _, _ = project
    source.write_text(event('session_meta', {'id': 'chat', 'cwd': str(root)})+message('stable'))
    refresh_chat(store, 'chat', str(source))
    before=store.db.total_changes
    result=refresh_chat(store, 'chat', str(source))
    assert result['added_entries']==0
    assert store.db.total_changes==before
