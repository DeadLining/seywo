"""Long messages are covered, source-backed and resumable, not dropped."""
import json

import pytest

from sessiongraph.extraction import ProviderError
from sessiongraph.memories import CHUNK_CAP, _state_path, run_chat, validate_memories
from test_memories import FakeExtractor, make_store


def sent_rows(extractor):
    return [row for batch, _ in extractor.calls for row in batch]


def assert_coverage(text, rows):
    covered = bytearray(len(text))
    for row in rows:
        start, end = row['source_start'], row['source_end']
        assert row['text'] == text[start:end]
        assert len(row['text']) <= CHUNK_CAP
        covered[start:end] = b'\1' * (end - start)
    assert all(covered)


def test_long_entry_full_coverage_original_id_and_exact_evidence(tmp_path):
    text = '首部：run-314 的目标是复现。\n' + '背景说明。\n' * 2000 + '尾部：F1=0.913，报告在 /tmp/run-314.json。'
    _, store, _ = make_store(tmp_path, [('user', text)])
    eid = store.db.execute('select id from entries').fetchone()[0]
    extractor = FakeExtractor([])
    result = run_chat(store, extractor, 'chat-1')
    rows = sent_rows(extractor)
    assert rows, 'oversized entry must reach extractor'
    assert_coverage(text, rows)
    assert {r['id'] for r in rows} == {eid}
    assert result['dropped_oversize'] == 0
    assert result['fragment_batches'] == len(rows)
    last = rows[-1]
    item = {'kind': 'finding', 'title': '训练结果', 'content': 'F1=0.913',
            'status': 'active', 'aliases': [], 'entry_id': eid,
            'quote': '尾部：F1=0.913，报告在 /tmp/run-314.json。'}
    validate_memories({'memories': [item]}, [last])
    with pytest.raises(ValueError, match='exact source substring'):
        validate_memories({'memories': [dict(item, quote='首部：run-314 的目标是复现。')]}, [last])
    assert run_chat(store, FakeExtractor([]), 'chat-1')['model_calls'] == 0
    store.close()


def test_failed_fragment_resumes_without_skipping_tail_or_repeating_success(tmp_path, monkeypatch):
    monkeypatch.setattr("sessiongraph.memory_retry.time.sleep", lambda _: None)
    text = '开始\n' + '训练材料xyz\n' * 2800 + '关键结论在末尾。'
    _, store, _ = make_store(tmp_path, [('user', text), ('user', '后续短消息')])
    calls = []
    def fail_second(batch, policy):
        calls.append(batch)
        if 2 <= len(calls) <= 4:
            raise ProviderError('sanitized', 'provider_timeout')
        return {'memories': []}
    first = run_chat(store, fail_second, 'chat-1', lookback_chars=100000)
    assert len(calls) == 4 and first['failure_codes'] == ['provider_timeout']
    assert all(r['text'] != '后续短消息' for b in calls for r in b)
    checkpoint = json.loads(_state_path(store, 'chat-1').read_text())
    assert checkpoint['segments']['chat-1'] < calls[0][0]['line']
    retry = FakeExtractor([])
    second = run_chat(store, retry, 'chat-1')
    assert not second['failed']
    assert retry.calls[0][0] == calls[1]
    fragments = calls[0] + [r for r in sent_rows(retry) if 'source_start' in r]
    assert_coverage(text, fragments)
    assert sent_rows(retry)[-1]['text'] == '后续短消息'
    store.close()


def test_first_run_keeps_newest_whole_long_message_even_over_tail_budget(tmp_path):
    text = '有效材料\n' * 7000 + '最后的重要决定'
    _, store, _ = make_store(tmp_path, [('user', '旧消息'), ('user', text)])
    extractor = FakeExtractor([])
    run_chat(store, extractor, 'chat-1', lookback_chars=24000)
    rows = sent_rows(extractor)
    assert rows and all(r['text'] != '旧消息' for r in rows)
    assert_coverage(text, rows)
    store.close()


def test_normal_batch_is_unchanged_and_lower_budget_is_respected(tmp_path):
    _, store, _ = make_store(tmp_path, [('user', '正常消息'), ('assistant', '确认结论')])
    extractor = FakeExtractor([])
    run_chat(store, extractor, 'chat-1')
    assert len(extractor.calls) == 1
    assert [r['text'] for r in sent_rows(extractor)] == ['正常消息', '确认结论']
    assert all('source_start' not in r for r in sent_rows(extractor))
    store.close()


def test_legacy_repair_preview_is_noop_and_execution_is_idempotent(tmp_path):
    text = '历史长消息材料\n' * 1500 + '历史结论'
    _, store, _ = make_store(tmp_path, [('user', text), ('user', '普通消息')])
    last = store.db.execute('select max(line) from entries').fetchone()[0]
    path = _state_path(store, 'chat-1')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'segments': {'chat-1': last}}))
    before = path.read_bytes()
    preview_extractor = FakeExtractor([])
    preview = run_chat(store, preview_extractor, 'chat-1', repair_oversize=True, dry_run=True)
    assert preview['candidate_entries'] == 1
    assert preview_extractor.calls == [] and path.read_bytes() == before
    extractor = FakeExtractor([])
    result = run_chat(store, extractor, 'chat-1', repair_oversize=True)
    assert not result['failed']
    assert_coverage(text, sent_rows(extractor))
    assert json.loads(path.read_text())['segments']['chat-1'] == last
    again = run_chat(store, FakeExtractor([]), 'chat-1', repair_oversize=True)
    assert again['model_calls'] == 0
    store.close()


def test_fragment_boundary_overlap_preserves_short_quotes(tmp_path):
    text = '甲' * (CHUNK_CAP - 80) + '边界上的关键结论' * 15 + '乙' * 8000
    _, store, _ = make_store(tmp_path, [('user', text)])
    extractor = FakeExtractor([])
    run_chat(store, extractor, 'chat-1')
    quote = text[CHUNK_CAP-120:CHUNK_CAP+120]
    assert any(quote in r['text'] for r in sent_rows(extractor))
    store.close()


def test_repair_cli_preview_needs_no_provider_or_writes(tmp_path, monkeypatch, capsys):
    from sessiongraph.cli import main
    root, store, _ = make_store(tmp_path, [('user', '历史材料' * 3000)])
    path = _state_path(store, 'chat-1')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'segments': {'chat-1': 100}}))
    before = path.read_bytes()
    store.close()
    def forbidden(*args):
        raise AssertionError('preview must not construct a provider')
    monkeypatch.setattr('sessiongraph.extraction.create_extractor', forbidden)
    assert main(['--config', str(root / '.sessiongraph/config.json'), 'memories',
                 '--chat-id', 'chat-1', '--repair-oversize', '--dry-run']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['candidate_entries'] == 1 and path.read_bytes() == before


def test_small_budget_and_json_inside_long_message_do_not_drop_data(tmp_path):
    text = '前言\n' + '{这是消息内部内容}\n' * 900
    _, store, _ = make_store(tmp_path, [('user', text)])
    extractor = FakeExtractor([])
    run_chat(store, extractor, 'chat-1', max_chars=1000)
    assert_coverage(text, sent_rows(extractor))
    assert all(sum(len(r['text']) for r in b) <= 1000 for b, _ in extractor.calls)
    store.close()


def test_repair_failure_resumes_and_does_not_move_normal_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr("sessiongraph.memory_retry.time.sleep", lambda _: None)
    _, store, _ = make_store(tmp_path, [('user', '历史材料\n' * 4000)])
    path = _state_path(store, 'chat-1')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'segments': {'chat-1': 100}}))
    calls = []
    def fail_second(batch, policy):
        calls.append(batch)
        if 2 <= len(calls) <= 4:
            raise ProviderError('sanitized', 'provider_timeout')
        return {'memories': []}
    result = run_chat(store, fail_second, 'chat-1', repair_oversize=True)
    assert result['failed']
    assert json.loads(path.read_text())['segments']['chat-1'] == 100
    retry = FakeExtractor([])
    run_chat(store, retry, 'chat-1', repair_oversize=True)
    assert retry.calls[0][0] == calls[1]
    assert json.loads(path.read_text())['segments']['chat-1'] == 100
    store.close()


def test_repair_respects_timestamp_and_fresh_fragments_not_candidates(tmp_path):
    _, store, _ = make_store(tmp_path, [('user', '材料\n' * 4000)])
    run_chat(store, FakeExtractor([]), 'chat-1')
    assert run_chat(store, None, 'chat-1', repair_oversize=True, dry_run=True)['candidate_entries'] == 0
    path = _state_path(store, 'chat-1')
    path.write_text(json.dumps({'segments': {'chat-1': 100}}))
    result = run_chat(store, None, 'chat-1', repair_oversize=True, dry_run=True,
                      since_time='2026-10-01T00:00:00Z')
    assert result['candidate_entries'] == 0
    store.close()


def test_saved_fragment_survives_crash_and_is_source_backed(tmp_path):
    from sessiongraph.work_map import work_map
    text = '重要发现：训练最终通过。\n' + '背景\n' * 4000
    _, store, _ = make_store(tmp_path, [('user', text)])
    calls = []
    def crash(batch, policy):
        calls.append(batch)
        if len(calls) == 2:
            raise KeyboardInterrupt()
        return {'memories': [{'kind': 'finding', 'title': '训练已通过',
            'content': '训练最终通过', 'status': 'completed', 'aliases': [],
            'entry_id': batch[0]['id'], 'quote': '重要发现：训练最终通过。'}]}
    with pytest.raises(KeyboardInterrupt):
        run_chat(store, crash, 'chat-1')
    row = store.db.execute('select source_entry_id, source_quote from work_memory').fetchone()
    assert row['source_entry_id'] == calls[0][0]['id']
    assert row['source_quote'] in text
    assert any(n['type'] == 'memory' for n in work_map(store)['nodes'])
    retry = FakeExtractor([])
    run_chat(store, retry, 'chat-1')
    assert retry.calls[0][0] == calls[1]
    store.close()


def test_short_message_first_run_window_keeps_old_budget_behavior():
    from sessiongraph.memories import _bounded_tail
    rows = [{'id': str(i), 'line': i, 'text': 'short', 'kind': 'message', 'role': 'user'}
            for i in range(3)]
    assert _bounded_tail(rows, 7) == rows[-1:]


def test_corrupt_checkpoint_fails_closed_instead_of_resending_history(tmp_path):
    _, store, _ = make_store(tmp_path, [('user', '背景材料' * 3000)])
    path = _state_path(store, 'chat-1')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{broken')
    extractor = FakeExtractor([])
    with pytest.raises(ValueError, match='checkpoint'):
        run_chat(store, extractor, 'chat-1')
    assert extractor.calls == [] and path.read_text() == '{broken'
    store.close()
