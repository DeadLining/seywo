import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from sessiongraph import ingestion
from sessiongraph.core import Store, init_project, load_config
from sessiongraph.continuity import refresh_chat
from sessiongraph.import_diagnostics import classify_exception, summarize_import


@pytest.fixture
def setup(tmp_path):
    cfg = load_config(init_project(tmp_path))
    cfg['hook']['allowed_transcript_roots'] = [str(tmp_path)]
    store = Store(cfg)
    path = tmp_path / 'chat-stream.jsonl'
    path.write_bytes((json.dumps({'type': 'session_meta', 'payload': {
        'id': 'chat-stream', 'cwd': str(tmp_path)}}) + '\n').encode())
    yield store, path
    store.close()


def message(text):
    return (json.dumps({'type': 'response_item', 'payload': {'type': 'message',
        'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}}) + '\n').encode()


def checkpoint(store):
    return dict(store.db.execute('SELECT * FROM sessions').fetchone())


def test_real_source_over_128_mib_and_multimegabyte_record(setup):
    store, path = setup
    # 130 MiB of valid records, not a mocked file-size check. Include an 8 MiB
    # single event and a normalized entry to prove raw fidelity and retrieval.
    filler = json.dumps({'type': 'event_msg', 'payload': 'x' * (512 * 1024)}).encode() + b'\n'
    with path.open('ab') as f:
        for _ in range(244):
            f.write(filler)
        f.write(json.dumps({'type': 'event_msg', 'payload': 'z' * (8 * 1024 * 1024)}).encode() + b'\n')
        f.write(message('preservedneedle scientific result'))
    assert path.stat().st_size > 128 * 1024 * 1024
    result = refresh_chat(store, 'chat-stream')
    assert result['skipped'] == []
    assert summarize_import(result)[1] is None
    assert result['added_entries'] == 1
    assert store.db.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 0
    assert checkpoint(store)['lines'] == 247
    from sessiongraph.source_locator import read_original
    eid = store.db.execute('SELECT id FROM entries').fetchone()[0]
    assert read_original(store,eid)['status'] == 'verified'
    assert store.search('preservedneedle')['hits']
    assert checkpoint(store)['offset'] == path.stat().st_size
    assert checkpoint(store)['prefix_hash'] == hashlib.file_digest(path.open('rb'), 'sha256').hexdigest()
    assert store.ingest(path)['added'] == 0
    with path.open('ab') as f:
        f.write(message('incrementalneedle'))
    assert store.ingest(path)['added'] == 1
    assert store.search('incrementalneedle')['hits']


def test_interrupted_transaction_resumes_without_lost_or_duplicate_records(setup, monkeypatch):
    store, path = setup
    monkeypatch.setattr(ingestion, 'BATCH_RECORDS', 3)
    with path.open('ab') as f:
        for i in range(12):
            f.write(message(f'result {i}'))
    store.db.execute("CREATE TRIGGER fail_ingest BEFORE INSERT ON entries WHEN NEW.line=8 BEGIN SELECT RAISE(ABORT, 'interrupted'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.ingest(path)
    before = checkpoint(store)
    assert before['lines'] == 6
    assert store.db.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 0
    assert store.db.execute('SELECT count(*) FROM sg_source_locations').fetchone()[0] == 5
    store.db.execute('DROP TRIGGER fail_ingest')
    assert store.ingest(path)['added'] == 7
    assert store.db.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 0
    assert store.db.execute('SELECT count(*) FROM sg_source_locations').fetchone()[0] == 12
    assert store.db.execute('SELECT count(*) FROM entries').fetchone()[0] == 12
    assert store.ingest(path)['added'] == 0


def test_partial_tail_keeps_exact_checkpoint_and_crlf_ids(setup):
    store, path = setup
    raw = message('多字节 retainedneedle').replace(b'\n', b'\r\n')
    with path.open('ab') as f:
        f.write(raw)
        f.write(message('laterneedle')[:-2])
    result = store.ingest(path)
    assert result['incomplete_tail'] is True
    row = checkpoint(store)
    assert row['lines'] == 2
    record = raw[:-2]
    eid = 'e_' + hashlib.sha256(f"chat-stream:2:{hashlib.sha256(record).hexdigest()}".encode()).hexdigest()[:32]
    assert store.db.execute('SELECT id FROM entries').fetchone()[0] == eid
    with path.open('ab') as f:
        f.write(b'}\n')
    assert store.ingest(path)['added'] == 1
    assert store.search('laterneedle')['hits']


def test_record_safety_limit_does_not_skip_or_advance_checkpoint(setup, monkeypatch):
    store, path = setup
    store.ingest(path)
    before = checkpoint(store)
    monkeypatch.setattr(ingestion, 'MAX_RECORD_BYTES', 1024)
    with path.open('ab') as f:
        f.write(message('x' * 2048))
        f.write(message('afterlimit'))
    with pytest.raises(ValueError) as caught:
        store.ingest(path)
    assert classify_exception(path, caught.value)['code'] == 'record_too_large'
    assert classify_exception(path, caught.value)['line'] == 2
    assert checkpoint(store) == before
    monkeypatch.setattr(ingestion, 'MAX_RECORD_BYTES', 4096)
    assert store.ingest(path)['added'] == 2


def test_concurrent_importers_rebase_between_batches(setup, monkeypatch):
    store, path = setup
    monkeypatch.setattr(ingestion, 'BATCH_RECORDS', 3)
    with path.open('ab') as f:
        for i in range(50):
            f.write(message(f'concurrent {i}'))
    def run(_):
        other = Store(store.config)
        try:
            return other.ingest(path)['added']
        finally:
            other.close()
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(run, range(4))) == 50
    assert checkpoint(store)['lines'] == 51


def test_previously_indexed_prefix_mutation_is_detected_even_far_from_header(setup):
    store, path = setup
    with path.open('ab') as f:
        f.write(message('x' * (3 * 1024 * 1024)))
    store.ingest(path)
    before = checkpoint(store)
    with path.open('r+b') as f:
        f.seek(2 * 1024 * 1024)
        f.write(b'y')
    with pytest.raises(ValueError, match='source changed'):
        store.ingest(path)
    assert checkpoint(store) == before


def test_snapshot_does_not_chase_writer_appends(setup, monkeypatch):
    store, path = setup
    original = ingestion._record
    appended = False
    def read(stream, end, line):
        nonlocal appended
        result = original(stream, end, line)
        if not appended:
            with path.open('ab') as f:
                f.write(message('appendedlater'))
            appended = True
        return result
    monkeypatch.setattr(ingestion, '_record', read)
    assert store.ingest(path)['added'] == 0
    assert store.ingest(path)['added'] == 1


def test_truncation_during_import_does_not_spin_or_commit(setup, monkeypatch):
    store, path = setup
    header_size = path.stat().st_size
    with path.open('ab') as f:
        f.write(message('will be truncated'))
    original = ingestion._record
    truncated = False
    def read(stream, end, line):
        nonlocal truncated
        result = original(stream, end, line)
        if not truncated:
            with path.open('r+b') as f:
                f.truncate(header_size)
            truncated = True
        return result
    monkeypatch.setattr(ingestion, '_record', read)
    with pytest.raises(ValueError, match='source changed'):
        store.ingest(path)
    assert store.db.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 0
