import hashlib
import json
import random
from pathlib import Path

import pytest

from seywo.config import initialize, load_config
from seywo.store import SCHEMA_VERSION, Store


def project(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    initialize(root)
    return root, load_config(root / '.seywo/config.json')


def source(root, path, rows):
    meta = {'type': 'session_meta', 'payload': {'id': 'chat', 'cwd': str(root)}}
    path.write_bytes(b''.join((json.dumps(r, ensure_ascii=False) + '\n').encode() for r in [meta, *rows]))
    return path


def message(text):
    return {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
            'content': [{'type': 'input_text', 'text': text}]}}


def test_shared_descriptors_keep_distinct_occurrences_and_exact_reads(tmp_path):
    root, cfg = project(tmp_path)
    raw = source(root, root / 'raw.jsonl', [message('同一个目标 some_symbol')])
    with Store(cfg) as s:
        assert s.db.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        snapshots = [s.import_source(raw if i == 0 else source(root, root / f'{i}.jsonl',
                     [message('同一个目标 some_symbol')]), 'chat', i) for i in range(8)]
        assert s.db.execute('SELECT count(*) FROM fragment_descriptors').fetchone()[0] <= 2
        assert s.db.execute('SELECT count(*) FROM source_fragments').fetchone()[0] == 16
        assert len(s.search('some_symbol')['matches']) == 8
        assert len(s.search('some symbol')['matches']) == 8
        assert len(s.search('同一个目标')['matches']) == 8
        row = s.db.execute("SELECT event_id FROM source_fragments WHERE role='user' LIMIT 1").fetchone()
        assert s.read(row[0], snapshot_id=snapshots[0]['snapshot_id'])['text'] == '同一个目标 some_symbol'
        assert not s.db.execute('PRAGMA foreign_key_check').fetchall()


def test_execution_documents_are_exact_shared_and_bounded(tmp_path):
    from seywo.execution_documents import put, read
    root, cfg = project(tmp_path)
    texts = []
    with Store(cfg) as s, s.db:
        for i in range(75):
            text = json.dumps({'previous': {'items': [{'title': '目标' * 30, 'i': n} for n in range(200)]},
                               'progress': i}, ensure_ascii=False, sort_keys=True)
            ref = put(s.db, text, family='chat:organize:request')
            texts.append((ref, text))
        assert put(s.db, texts[-1][1], family='other') == texts[-1][0]
        assert all(read(s.db, ref) == text for ref, text in texts)
        assert s.db.execute('SELECT max(depth) FROM execution_documents').fetchone()[0] < 32
        packed = s.db.execute('SELECT sum(length(body)) FROM execution_documents').fetchone()[0]
        assert packed < sum(len(t.encode()) for _, t in texts) / 20
        noncanonical = '{ "a" : 1 }\n'
        assert read(s.db, put(s.db, noncanonical, family='raw')) == noncanonical


def test_task_sql_contract_survives_shared_storage_updates_and_cascade(tmp_path):
    root, cfg = project(tmp_path)
    with Store(cfg) as s, s.db:
        s.db.execute('INSERT INTO chats VALUES(?,?)', ('chat', cfg['project_id']))
        s.db.execute("INSERT INTO understanding_jobs(id,chat_id,target_hash,source_heads,options_json,limits_json,state,created_at) VALUES('job','chat','hash','{}','{}','{}','running',0)")
        request = json.dumps({'previous': ['state'] * 20000}, sort_keys=True)
        s.db.execute("INSERT INTO understanding_tasks VALUES(?,?,?,?,?,?,?,?,?,?)",
                     ('task', 'job', 0, 'organize', 'pending', request, 'fingerprint', None, '[]', 0))
        assert s.db.execute('SELECT request_json FROM understanding_tasks').fetchone()[0] == request
        s.db.execute("UPDATE understanding_tasks SET state='done', result_json=? WHERE id='task'", ('{"ok":true}',))
        row = s.db.execute('SELECT * FROM understanding_tasks').fetchone()
        assert row['request_json'] == request and row['result_json'] == '{"ok":true}'
        assert s.db.execute('SELECT length(request_json) FROM task_records').fetchone()[0] == 64
        s.db.execute("DELETE FROM chats WHERE id='chat'")
        assert s.db.execute('SELECT count(*) FROM understanding_tasks').fetchone()[0] == 0
        assert not s.db.execute('PRAGMA foreign_key_check').fetchall()


def test_shifted_raw_histories_share_blocks_and_remain_byte_exact(tmp_path):
    from seywo.archive import capture, open_source, reblock
    live = tmp_path / 'inbox/live'
    live.mkdir(parents=True)
    rng = random.Random(1129)
    body = b''.join((json.dumps({'item': i, 'history': rng.randbytes(260).hex()}) + '\n').encode() for i in range(7000))
    first, second = tmp_path / 'one', tmp_path / 'two'
    first.write_bytes(body)
    second.write_bytes(b'inserted context changes offsets\n' + body)
    a, b = live / 'a.jsonl', live / 'b.jsonl'
    capture(first, a)
    before = {p.name for p in (live.parent / 'objects').glob('*.z')}
    capture(second, b)
    after = {p.name for p in (live.parent / 'objects').glob('*.z')}
    assert len(after - before) < len(before) / 4
    assert open_source(a).read() == body
    assert open_source(b).read() == second.read_bytes()
    old = a.read_bytes()
    reblock(a)
    assert a.read_bytes() == old
    assert open_source(a).read() == body


def test_collection_keeps_retry_sources_and_collects_terminal_unbound_captures(tmp_path):
    from seywo import inbox
    from seywo.storage_lifecycle import collect
    root, cfg = project(tmp_path)
    native = source(root, root / 'native.jsonl', [message('已经入库')])
    with Store(cfg) as s:
        imported = s.import_compact(native, 'chat', str(native))
    failed_source = source(root, root / 'failed.jsonl', [message('等待重试')])
    item = inbox.record(cfg, 'chat', 'PreCompact', str(failed_source))
    item = inbox.claim(cfg, item)
    inbox.finish(cfg, item, state='failed', error='operation_failed')
    terminal = source(root, root / 'terminal.jsonl', [message('无变化的临时捕获')])
    done = inbox.record(cfg, 'chat', 'PreCompact', str(terminal))
    done = inbox.claim(cfg, done)
    inbox.finish(cfg, done, state='done')
    result = collect(cfg, force=True)
    assert result['state'] == 'done'
    assert Path(item['snapshot']).exists()
    assert not Path(done['snapshot']).exists()
    with Store(cfg, read_only=True) as s:
        assert s.db.execute('SELECT count(*) FROM source_snapshots').fetchone()[0] == 1
        assert s.db.execute('SELECT id FROM source_snapshots').fetchone()[0] == imported['snapshot_id']
    assert collect(cfg, force=True)['removed_manifests'] == 0


def test_collection_is_nonblocking_while_other_writer_owns_lifecycle(tmp_path):
    from seywo.storage_lifecycle import collect
    from seywo.locking import lifecycle
    root, cfg = project(tmp_path)
    with lifecycle(cfg):
        assert collect(cfg)['state'] == 'deferred'


def test_committed_maintenance_also_reclaims_noop_captures(tmp_path, monkeypatch):
    from seywo import maintain, inbox
    root, cfg = project(tmp_path)
    raw = source(root, root / 'raw.jsonl', [message('无需再次提取')])
    item = inbox.record(cfg, 'chat', 'PreCompact', str(raw))
    claim = inbox.claim(cfg, item)
    inbox.finish(cfg, claim, state='done')
    monkeypatch.setattr(maintain, '_run_chat_owned', lambda *args: {'state': 'done', 'calls': 0, 'version': 0})
    report = maintain.run_chat(cfg, 'chat')
    assert report['state'] == 'done' and report['calls'] == 0
    assert report['storage']['removed_manifests'] == 1
    assert not Path(item['snapshot']).exists()


def test_collection_failure_is_visible_without_reversing_published_state(tmp_path, monkeypatch):
    from seywo import maintain, maintenance_status, storage_lifecycle
    root, cfg = project(tmp_path)
    monkeypatch.setattr(maintain, '_run_chat_owned', lambda *args: {'state': 'done', 'calls': 0, 'version': 7})
    def fail(*args, **kwargs):
        raise OSError('private detail must not be shown')
    monkeypatch.setattr(storage_lifecycle, 'collect', fail)
    report = maintain.run_chat(cfg, 'chat')
    assert report['state'] == 'done' and report['version'] == 7
    assert report['storage']['error_code'] == 'storage_maintenance_failed'
    assert maintenance_status.read(cfg, 'chat')['storage']['state'] == 'failed'
    assert 'private detail' not in json.dumps(report)


def test_ambiguous_pending_record_prevents_capture_deletion(tmp_path):
    from seywo import inbox
    from seywo.storage_lifecycle import collect
    root, cfg = project(tmp_path)
    raw = source(root, root / 'raw.jsonl', [message('保护来源')])
    path = Path(inbox.snapshot(cfg, raw, 'chat'))
    pending = path.parent.parent / 'pending'
    pending.mkdir(exist_ok=True)
    (pending / 'corrupt.json').write_text('{')
    with pytest.raises(ValueError):
        collect(cfg, force=True)
    assert path.exists()


def test_identifier_search_keeps_original_phrase_semantics(tmp_path):
    root, cfg = project(tmp_path)
    raw = source(root, root / 'raw.jsonl', [message('call_id'), message('call id'), message('call unrelated id')])
    with Store(cfg) as store:
        store.import_compact(raw, 'chat', str(raw))
        assert {m['preview'] for m in store.search('call_id')['matches']} == {'call_id', 'call id'}
        assert {m['preview'] for m in store.search('call id')['matches']} == {'call_id', 'call id', 'call unrelated id'}


def test_collection_preserves_execution_delta_ancestors_and_removes_orphan_chains(tmp_path):
    from seywo.execution_documents import put, read
    from seywo.storage_lifecycle import collect_database
    root, cfg = project(tmp_path)
    with Store(cfg) as store, store.db:
        db = store.db
        db.execute('INSERT INTO chats VALUES(?,?)', ('chat', cfg['project_id']))
        db.execute("INSERT INTO understanding_jobs(id,chat_id,target_hash,source_heads,options_json,limits_json,state,created_at) VALUES('job','chat','hash','{}','{}','{}','running',0)")
        first = json.dumps({'items': ['persistent history'] * 1000, 'progress': 1}, sort_keys=True)
        second = first.replace('1}', '2}')
        a = put(db, first, family='kept')
        b = put(db, second, family='kept')
        assert db.execute('SELECT base_hash FROM execution_documents WHERE hash=?', (b,)).fetchone()[0] == a
        db.execute("INSERT INTO understanding_tasks VALUES('task','job',0,'organize','pending',?,'fingerprint',NULL,'[]',0)", (second,))
        for i in range(3):
            put(db, json.dumps({'unrelated': ['other'] * 1000, 'progress': i}, sort_keys=True), family='orphan')
        assert collect_database(db)['removed_documents'] == 3
        assert read(db, b) == second
        assert db.execute('SELECT count(*) FROM execution_documents').fetchone()[0] == 2
        assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_collection_refuses_symlinked_pending_directory(tmp_path):
    from seywo.storage_lifecycle import collect
    root, cfg = project(tmp_path)
    external = tmp_path / 'external'
    external.mkdir()
    inbox = root / '.seywo/inbox'
    inbox.mkdir()
    (inbox / 'pending').symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        collect(cfg, force=True)


def test_content_defined_manifest_keeps_legacy_maximum_source_capacity(tmp_path):
    from seywo import archive
    path = tmp_path / 'manifest'
    record = {'seywo_archive': 1, 'bytes': 4096 * archive.BLOCK_BYTES, 'sha256': 'a' * 64,
              'chunks': ['b' * 64] * 32768}
    path.write_text(json.dumps(record, separators=(',', ':')))
    with path.open('rb') as handle:
        assert archive._manifest(handle) == record


def test_initial_database_is_published_only_when_schema_is_complete(tmp_path, monkeypatch):
    from seywo import store as store_module
    original = store_module.Store._open
    observed = []
    root = tmp_path / 'atomic-project'
    def inspect(self, config, **kwargs):
        if kwargs.get('create'):
            observed.append(Path(config['_database']))
            assert not (root / '.seywo/index.sqlite3').exists()
        return original(self, config, **kwargs)
    monkeypatch.setattr(store_module.Store, '_open', inspect)
    initialize(root)
    assert observed and observed[0].name.startswith('.index-')
    assert not observed[0].exists()
    with Store(load_config(root / '.seywo/config.json'), read_only=True) as store:
        assert store.db.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION


@pytest.mark.parametrize('damage', [{'state': 'failed', 'chat_id': ['chat'], 'snapshot': 'file'}, {'state': 'failed', 'chat_id': 'chat', 'snapshot': ['file']}])
def test_malformed_retry_reference_fails_safely_without_reversing_success(tmp_path, monkeypatch, damage):
    from seywo import maintain, storage_lifecycle
    root, cfg = project(tmp_path)
    pending = root / '.seywo/inbox/pending'
    pending.mkdir(parents=True)
    (pending / 'bad.json').write_text(json.dumps(damage))
    monkeypatch.setattr(maintain, '_run_chat_owned', lambda *args: {'state': 'done', 'version': 7})
    report = maintain.run_chat(cfg, 'chat')
    assert report['state'] == 'done' and report['version'] == 7
    assert report['storage']['state'] == 'failed'
    assert storage_lifecycle.status(cfg)['state'] == 'failed'


def test_corrupt_storage_receipt_is_a_safe_warning(tmp_path):
    from seywo.storage_lifecycle import status
    root, cfg = project(tmp_path)
    receipt = root / '.seywo/maintenance/storage.json'
    receipt.parent.mkdir(parents=True)
    receipt.write_text('[]')
    assert status(cfg) == {'state': 'failed', 'error_code': 'storage_maintenance_failed'}


@pytest.mark.parametrize('alias_in_reference', [False, True])
def test_collection_preserves_live_references_across_ancestor_path_aliases(tmp_path, alias_in_reference):
    from seywo import archive, inbox
    from seywo.storage_lifecycle import collect
    root, cfg = project(tmp_path)
    raw = source(root, root / 'native.jsonl', [message('来源必须保留')])
    with Store(cfg) as store:
        imported = store.import_compact(raw, 'chat', str(raw))
        paths = [Path(row[0]) for row in store.db.execute('SELECT path FROM source_archives')]
    failed_raw = source(root, root / 'failed.jsonl', [message('失败来源必须保留')])
    failed = inbox.record(cfg, 'chat', 'PreCompact', str(failed_raw))
    inbox.finish(cfg, inbox.claim(cfg, failed), state='failed', error='operation_failed')
    alias = tmp_path / 'alias'
    alias.symlink_to(tmp_path, target_is_directory=True)
    if alias_in_reference:
        with Store(cfg) as store, store.db:
            for table in ('source_segments', 'source_archives'):
                store.db.execute(f'UPDATE {table} SET path=replace(path,?,?)', (str(root), str(alias / 'project')))
        pending = Path(failed['_path'])
        item = json.loads(pending.read_text())
        item['snapshot'] = item['snapshot'].replace(str(root), str(alias / 'project'))
        pending.write_text(json.dumps(item))
    alias_cfg = load_config(alias / 'project/.seywo/config.json')
    assert collect(alias_cfg, force=True)['removed_manifests'] == 0
    assert all(path.exists() for path in paths)
    assert Path(failed['snapshot']).exists()
    for path in paths:
        assert archive.source_digest(path)
    with Store(alias_cfg, read_only=True) as store:
        event = store.db.execute("SELECT event_id FROM source_fragments WHERE role='user' LIMIT 1").fetchone()[0]
        assert store.read(event, snapshot_id=imported['snapshot_id'])['text'] == '来源必须保留'
