"""End-to-end source import, slicing and all-or-nothing publication."""
import json
from pathlib import Path

from seywo import inbox, maintain
from seywo.config import initialize, load_config
from seywo.store import Store
from test_compact_maintain import SequentialCompactTransport
from test_s2 import message


def setup(root, monkeypatch, transport):
    initialize(root)
    path = root / '.seywo/config.json'
    data = json.loads(path.read_text())
    data['hook'] = {'enabled': True, 'pipeline': 'seywo-compact-v1', 'model': transport.model}
    path.write_text(json.dumps(data))
    config = load_config(path)
    monkeypatch.setattr(maintain, 'Transport', lambda *a, **kw: transport)
    monkeypatch.setattr(maintain, 'read_provider', lambda *a, **kw: {
        'endpoint': 'http://127.0.0.1:1/responses', 'model': transport.model,
        'timeout': 5, 'max_output_tokens': 768, '_api_key': 'fixture'})
    return config


def capture(root, chat):
    user = '规格条目。' * 2400 + '月结口径改为自然月。' + '补充条目。' * 1900
    assistant = '实现进展。' * 1300 + '兼容性检查尚未结束。'
    records = [
        {'type': 'session_meta', 'payload': {'id': chat, 'cwd': str(root)}},
        {'type': 'turn_context', 'payload': {'turn_id': 'native', 'root_turn_id': 'native'}},
        message(user), message(assistant, 'assistant')]
    path = root / 'source.jsonl'
    path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records))
    return path, user, assistant


def test_long_native_turn_is_complete_condensed_and_correctly_cited(tmp_path, monkeypatch):
    transport = SequentialCompactTransport()
    config = setup(tmp_path, monkeypatch, transport)
    path, user, assistant = capture(tmp_path, 'long')
    inbox.record(config, 'long', 'PreCompact', str(path))
    report = maintain.run_chat(config, 'long')
    assert report['state'] == 'done', report
    assert transport.calls >= 2
    for role, text in [('user', user), ('assistant', assistant)]:
        assert ''.join(t[role] for p in transport.payloads for t in p['turns']) == text
    with Store(config, read_only=True) as store:
        body = json.loads(store.db.execute(
            'SELECT body_json FROM narrative_versions WHERE chat_id=? ORDER BY version DESC LIMIT 1', ('long',)).fetchone()[0])
        assert len(body['items']) == 1
        refs = body['items'][0]['evidence']
        assert sum(ref['end'] - ref['start'] for ref in refs) == len(user)
        assert store.db.execute('SELECT windows FROM delta_progress WHERE chat_id=?', ('long',)).fetchone()[0] == 1
        receipt = store.db.execute('SELECT turn_count,model_calls FROM compact_batches WHERE chat_id=?', ('long',)).fetchone()
        assert tuple(receipt) == (1, transport.calls)
    # Repeated maintenance sees the committed native turn, not new fragments.
    again = maintain.run_chat(config, 'long')
    assert again['calls'] == 0
    assert transport.calls == report['calls']


class FailSecond(SequentialCompactTransport):
    def complete_compact(self, **kwargs):
        if self.calls == 1:
            self.calls += 1
            return {'output': None, 'error': 'fixture_second_batch_failure', 'usage': {}}
        return super().complete_compact(**kwargs)


def test_later_slice_failure_publishes_nothing_then_retry_publishes_once(tmp_path, monkeypatch):
    broken = FailSecond()
    config = setup(tmp_path, monkeypatch, broken)
    path, user, assistant = capture(tmp_path, 'retry')
    boundary = inbox.record(config, 'retry', 'PreCompact', str(path))
    failed = maintain.run_chat(config, 'retry')
    assert failed['state'] == 'failed', failed
    assert failed['error_code'] == 'fixture_second_batch_failure'
    # The source was durably imported before semantic extraction failed.  It is
    # therefore complete in the source inbox and retry starts from the
    # semantic cursor without importing the transcript again.
    saved = inbox._read(Path(boundary['_path']))
    assert saved['state'] == 'claimed' and saved['imported_snapshot_id']
    with Store(config, read_only=True) as store:
        for table in ['narrative_versions', 'delta_progress']:
            assert store.db.execute(f'SELECT count(*) FROM {table} WHERE chat_id=?', ('retry',)).fetchone()[0] == 0
    healthy = SequentialCompactTransport()
    monkeypatch.setattr(maintain, 'Transport', lambda *a, **kw: healthy)
    retried = maintain.run_chat(config, 'retry')
    assert retried['state'] == 'done', retried
    assert ''.join(t['user'] for p in healthy.payloads for t in p['turns']) == user
    assert ''.join(t['assistant'] for p in healthy.payloads for t in p['turns']) == assistant
    with Store(config, read_only=True) as store:
        assert store.db.execute('SELECT count(*) FROM narrative_versions WHERE chat_id=?', ('retry',)).fetchone()[0] == 1
        assert store.db.execute('SELECT windows FROM delta_progress WHERE chat_id=?', ('retry',)).fetchone()[0] == 1
