"""Independent evidence retention; synthetic fixtures never call a provider."""
from sessiongraph.source_text import hydrate
import json
import sqlite3
import zlib

import pytest

from sessiongraph import maintenance, source_locator, source_retention as retention, storage
from sessiongraph.core import Store, digest
from sessiongraph.failures import Failure
from test_input_framework import fixture, legacy_fixture, msg


def remember(store, eid=None, title='result'):
    row = store.db.execute('SELECT * FROM entries WHERE id=?', (eid,)).fetchone() if eid else store.db.execute('SELECT * FROM entries ORDER BY line LIMIT 1').fetchone()
    row = hydrate(store, row)
    return store.remember(kind='finding', title=title, content='Retained evidence',
                          source_entry_id=row['id'], source_quote=row['text'][:100])


def enable(store):
    with store.db:
        retention.setup(store)
        store.db.execute('INSERT INTO metadata VALUES(?,?)', (retention.FLAG, retention.POLICY))


def capture(store, eid=None):
    eid = eid or store.db.execute('SELECT id FROM entries ORDER BY line LIMIT 1').fetchone()[0]
    with store.db:
        retention.setup(store)
        result = retention.capture(store, eid)
    return eid, result


def test_not_opted_in_does_not_create_schema_or_change_old_behavior(tmp_path):
    s, p = legacy_fixture(tmp_path, [msg('source')])
    p.unlink()
    remember(s)
    assert not storage.exists(s.db, 'sg_retained_sources')
    assert not retention.enabled(s)


def test_exact_record_is_independently_retained_and_idempotent(tmp_path):
    s, p = fixture(tmp_path, [msg('Unicode result \u679c\u8747 \U0001f642 0.10m; not passed')])
    original = p.read_text().splitlines()[1]
    eid, first = capture(s)
    p.unlink()
    second = retention.capture(s, eid)
    result = retention.read(s, eid)
    assert first['added'] and not second['added']
    assert result['record'] == original
    assert result['record_hash'] == result['snapshot_hash'] == digest(original)
    assert result['status'] == 'retained_verified'
    assert retention.read(s, 'unknown') is None
    assert s.db.execute('SELECT count(*) FROM sg_retained_sources').fetchone()[0] == 1


def test_preview_is_readonly_and_does_not_enable_publication(tmp_path):
    s, _ = fixture(tmp_path, [msg('source')])
    remember(s)
    ro = Store(s.config, readonly=True)
    before = ro.db.total_changes
    result = retention.maintain(ro)
    assert result['eligible'] == 1 and result['added'] == 0 and not result['publication_enabled']
    assert ro.db.total_changes == before and not storage.exists(ro.db, 'sg_retained_sources')
    ro.close()
    applied = maintenance.maintain_sources(s.config, apply=True)
    assert applied['added'] == 1 and not applied['publication_enabled']
    assert retention.maintain(s, apply=True)['existing'] == 1


def test_old_id_expand_and_read_original_work_when_external_source_disappears(tmp_path):
    s, p = fixture(tmp_path, [msg('evidence line')])
    mid = remember(s)['id']
    eid, _ = capture(s)
    p.unlink()
    ro = Store(s.config, readonly=True)
    assert source_locator.read_original(ro, eid)['text'] == 'evidence line'
    assert source_locator.describe(ro, eid, verify=True)['status'] == 'retained_verified'
    assert source_locator.describe(ro, eid)['status'] == 'retained_unverified'
    assert ro.expand(eid)['entries'][0]['source_location']['status'] == 'retained_verified'
    source = ro.recall('result')['memories'][0]['source']['entry_id']
    assert ro.expand(source)['entries'][0]['source_location']['storage'] == 'retained_snapshot'
    assert ro.db.total_changes == 0
    ro.close()


@pytest.mark.parametrize('change', ['missing', 'changed', 'locator', 'indexed_text'])
def test_unverifiable_sources_report_gap_without_fabrication(tmp_path, change):
    s, p = fixture(tmp_path, [msg('original source')])
    remember(s)
    if change == 'missing':
        p.unlink()
    elif change == 'changed':
        p.write_text(p.read_text().replace('original source', 'modified source'))
    elif change == 'locator':
        with s.db:
            s.db.execute('DELETE FROM sg_source_locations')
    else:
        with s.db:
            s.db.execute("UPDATE entries SET text='modified source'")
    result = retention.maintain(s, apply=True)
    assert result['added'] == 0 and len(result['gaps']) == 1
    assert result['gaps'][0]['code'].startswith('source_retention_')
    assert s.db.execute('SELECT count(*) FROM sg_retained_sources').fetchone()[0] == 0


def test_legacy_raw_record_is_retained_without_external_file_or_scanning(tmp_path):
    s, p = legacy_fixture(tmp_path, [msg('legacy source')])
    row = s.db.execute('SELECT * FROM entries').fetchone()
    raw = p.read_text().splitlines()[1]
    with s.db:
        s.db.execute('INSERT INTO raw_events VALUES(?,?,?)', (row['session_id'], row['line'], raw))
        s.db.execute('DELETE FROM sg_source_locations')
    p.unlink()
    eid, _ = capture(s)
    assert retention.read(s, eid)['record'] == raw
    assert s.db.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 1


def test_redacted_snapshot_does_not_claim_original_byte_equality(tmp_path):
    payload = msg('password=super-secret-value, result=0.913 is not final')
    payload['authorization'] = 'credential-short'
    s, p = fixture(tmp_path, [payload])
    original = p.read_text().splitlines()[1]
    eid, _ = capture(s)
    result = retention.read(s, eid)
    assert result['status'] == 'retained_sanitized'
    assert result['record_hash'] == digest(original) != result['snapshot_hash']
    assert result['redactions'] == ['credentials']
    assert 'super-secret-value' not in result['record'] and 'credential-short' not in result['record']
    assert 'result=0.913 is not final' in result['text']
    assert result['indexed_text_hash'] != result['snapshot_text_hash']


def test_inline_media_not_copied_but_metadata_and_conditions_survive(tmp_path):
    payload = msg('frame 0, model run_42; camera unavailable')
    payload['content'].append({'type': 'input_image', 'image_url': 'data:image/png;base64,AAAA',
                               'filename': 'frame.png', 'caption': 'first frame',
                               'source': {'encoding': 'base64', 'data': 'AAAA'}})
    s, _ = fixture(tmp_path, [payload])
    eid, _ = capture(s)
    result = retention.read(s, eid)
    assert result['redactions'] == ['inline_media']
    assert 'AAAA' not in result['record'] and 'data:image' not in result['record']
    assert all(x in result['record'] for x in ('frame.png', 'first frame', 'camera unavailable'))


def test_credentials_inside_serialized_tool_arguments_are_removed(tmp_path):
    s, _ = fixture(tmp_path, [{'type': 'function_call', 'name': 'request', 'call_id': 'c',
                             'arguments': json.dumps({'password': 'short', 'run_id': 'run_42'})}])
    eid, _ = capture(s)
    record = retention.read(s, eid)['record']
    assert 'short' not in record and 'run_42' in record


def test_publication_and_retention_share_one_transaction(tmp_path):
    s, p = legacy_fixture(tmp_path, [msg('first source'), msg('second source')])
    enable(s)
    row1, row2 = list(s.db.execute('SELECT id FROM entries ORDER BY line'))
    remember(s, row1[0])
    p.unlink()
    with pytest.raises(Failure, match='source_retention_source_missing') as exc:
        remember(s, row2[0], title='second')
    assert not exc.value.retryable
    assert s.db.execute('SELECT count(*) FROM work_memory').fetchone()[0] == 1
    assert s.db.execute('SELECT count(*) FROM sg_retained_sources').fetchone()[0] == 1
    assert not s.db.in_transaction


def test_failed_memory_write_rolls_back_new_snapshot(tmp_path):
    s, _ = fixture(tmp_path, [msg('source')])
    enable(s)
    with pytest.raises(ValueError, match='unknown supersedes_id'):
        row = hydrate(s, s.db.execute('SELECT * FROM entries').fetchone())
        s.remember(kind='finding', title='result', content='result', source_entry_id=row['id'],
                   source_quote=row['text'], supersedes_id='missing')
    assert not s.db.execute('SELECT * FROM sg_retained_sources').fetchall()
    assert not s.db.execute('SELECT * FROM work_memory').fetchall()


def test_graph_publication_retains_all_citations_and_relation_proofs(tmp_path):
    from sessiongraph.graph_v2 import run_chat
    from test_graph_v2 import output, Judge
    from test_memories import FakeExtractor
    s, p = fixture(tmp_path, [msg('Training A produces model B')])
    enable(s)
    value = output([hydrate(s, r) for r in s.db.execute('SELECT * FROM entries')])
    result = run_chat(s, FakeExtractor([value]), 'input-chat', jev=Judge())
    assert not result['failed'], result
    assert result['saved'] and result['saved_relations']
    ids = retention.candidates(s, '', 100)
    p.unlink()
    assert all(retention.read(s, eid)['status'] == 'retained_verified' for eid in ids)


@pytest.mark.parametrize('field,value,code', [
    ('snapshot_hash', 'wrong', 'snapshot_hash_mismatch'),
    ('project', 'other-project', 'metadata_mismatch'),
    ('policy', 'future-policy', 'metadata_mismatch'),
    ('snapshot_bytes', -1, 'snapshot_size'),
    ('record_hash', 'wrong', 'metadata_mismatch'),
    ('redactions_json', '{}', 'metadata_mismatch'),
])
def test_corrupt_snapshot_fails_closed_without_external_fallback(tmp_path, field, value, code):
    s, _ = fixture(tmp_path, [msg('source')])
    eid, _ = capture(s)
    with s.db:
        s.db.execute(f'UPDATE sg_retained_sources SET {field}=?', (value,))
    with pytest.raises(Failure, match='source_retention_' + code):
        source_locator.read_original(s, eid)


def test_compression_dedup_and_bounded_corrupt_blob_read(tmp_path):
    s, _ = fixture(tmp_path, [msg('x' * 6000)])
    eid, _ = capture(s)
    reference = s.db.execute('SELECT payload FROM sg_retained_sources').fetchone()[0]
    assert storage.ARCHIVE_MARKER in reference
    identity = json.loads(reference)[storage.ARCHIVE_MARKER]
    with s.db:
        s.db.execute('UPDATE sg_payload_archive SET size=100,data=? WHERE hash=?',
                     (zlib.compress(b'x' * 2000), identity))
    with pytest.raises(ValueError, match='graph_archive_size_or_stream_invalid'):
        storage.payload_text(s, reference, max_bytes=1000)
    with pytest.raises(Failure, match='source_retention_snapshot_corrupt'):
        retention.read(s, eid)


def test_budget_cursor_does_not_skip_blocked_entry_and_pagination_is_idempotent(tmp_path):
    s, _ = fixture(tmp_path, [msg('first'), msg('second')])
    for eid, in s.db.execute('SELECT id FROM entries').fetchall():
        remember(s, eid, title=eid)
    blocked = retention.maintain(s, apply=True, max_bytes=1)
    assert blocked['budget_exhausted'] and blocked['next_cursor'] == '' and blocked['more']
    one = retention.maintain(s, apply=True, limit=1)
    two = retention.maintain(s, apply=True, after=one['next_cursor'], limit=1)
    assert one['more'] and one['added'] == two['added'] == 1 and not two['more']
    assert retention.maintain(s, apply=True)['existing'] == 2


def test_enable_requires_explicit_apply_and_is_project_local(tmp_path):
    s, _ = fixture(tmp_path / 'first', [msg('source')])
    other, _ = fixture(tmp_path / 'other', [msg('other source')])
    with pytest.raises(ValueError, match='retention_enable_requires_apply'):
        retention.maintain(s, enable=True)
    result = retention.maintain(s, apply=True, enable=True)
    assert result['publication_enabled'] and retention.enabled(s)
    assert not retention.enabled(other)


def test_maintenance_lock_contention_preserves_originals(tmp_path):
    import fcntl
    s, _ = fixture(tmp_path, [msg('source')])
    lock_path = tmp_path / '.sessiongraph' / 'hook-state' / 'update.lock'
    lock_path.parent.mkdir(parents=True)
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match='maintenance_busy'):
            maintenance.maintain_sources(s.config, apply=True)
    assert not storage.exists(s.db, 'sg_retained_sources')


def test_cli_preview_apply_and_expand_are_model_free(tmp_path, capsys):
    from sessiongraph.cli import main
    s, p = fixture(tmp_path, [msg('CLI evidence')])
    remember(s)
    argv = ['--config', s.config['_config_path']]
    assert main(argv + ['source-retain']) == 0
    assert json.loads(capsys.readouterr().out)['eligible'] == 1
    assert not storage.exists(s.db, 'sg_retained_sources')
    assert main(argv + ['source-retain', '--apply']) == 0
    assert json.loads(capsys.readouterr().out)['added'] == 1
    eid = s.db.execute('SELECT id FROM entries').fetchone()[0]
    p.unlink()
    assert main(argv + ['source-read', eid]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'retained_verified'


def test_old_sql_writer_is_fenced_only_after_explicit_enable(tmp_path):
    s, _ = fixture(tmp_path, [msg('first source'), msg('second source')])
    first, second = list(s.db.execute('SELECT id FROM entries ORDER BY line'))
    mid = remember(s, first[0])['id']
    retention.maintain(s, apply=True, enable=True)
    legacy = sqlite3.connect(s.config['database'])
    with pytest.raises(sqlite3.IntegrityError, match='source_retention_snapshot_required'):
        with legacy:
            legacy.execute('UPDATE work_memory SET source_entry_id=? WHERE id=?', (second[0], mid))
    # A policy rollback preserves all historical snapshots and restores old writes.
    disabled = retention.maintain(s, apply=True, disable=True)
    assert not disabled['publication_enabled'] and disabled['examined'] == 0
    with legacy:
        legacy.execute('UPDATE work_memory SET source_entry_id=? WHERE id=?', (second[0], mid))
    assert retention.read(s, first[0]) is not None
    legacy.close()


def test_legacy_relation_writer_retains_proof_in_same_transaction(tmp_path):
    from sessiongraph.memories import run_chat
    from test_memories import FakeExtractor
    from test_work_relations import setup
    s, value = setup(tmp_path)
    enable(s)
    result = run_chat(s, FakeExtractor([value]), 'chat-1')
    assert not result['failed'] and result['saved_relations']
    eid = value['relations'][0]['entry_id']
    assert retention.read(s, eid)['status'] == 'retained_verified'


def test_failed_source_reads_are_charged_to_batch_budget(tmp_path):
    s, p = fixture(tmp_path, [msg('first evidence'), msg('other evidence')])
    for eid, in s.db.execute('SELECT id FROM entries').fetchall():
        remember(s, eid, title=eid)
    size = s.db.execute('SELECT max(byte_length) FROM sg_source_locations').fetchone()[0]
    p.write_text(p.read_text().replace('evidence', 'changed!'))
    result = retention.maintain(s, apply=True, max_bytes=size)
    assert len(result['gaps']) == 1 and result['budget_exhausted']
    assert 0 < result['payload_bytes'] <= size


def test_oversized_record_is_explicit_gap_not_endless_budget_retry(tmp_path, monkeypatch):
    s, _ = fixture(tmp_path, [msg('x' * 6000)])
    remember(s)
    monkeypatch.setattr(retention, 'MAX_BYTES', 1000)
    result = retention.maintain(s, apply=True)
    assert result['gaps'][0]['code'] == 'source_retention_record_too_large'
    assert not result['more'] and result['next_cursor']


def test_setup_reuses_existing_covering_source_index(tmp_path):
    s, _ = fixture(tmp_path, [msg('source')])
    retention.maintain(s, apply=True)
    indexes = {row['name'] for row in s.db.execute('PRAGMA index_list(sg2_evidence)')}
    assert 'sg2_evidence_source' in indexes
    assert 'sg_retention_sg2_evidence_source' not in indexes


def test_unknown_policy_is_not_silently_downgraded(tmp_path):
    s, _ = fixture(tmp_path, [msg('source')])
    enable(s)
    with s.db:
        s.db.execute('UPDATE metadata SET value=? WHERE key=?', ('future-policy', retention.FLAG))
    with pytest.raises(Failure, match='source_retention_policy_unavailable'):
        remember(s)
    assert s.db.execute('SELECT count(*) FROM work_memory').fetchone()[0] == 0
