"""Queue ownership must survive corrupt records and concurrent Hook delivery."""
import json

from seywo import inbox, maintain
from seywo.store import Store
from test_compact_boundaries import configured
from test_s2 import message


def transcript(root, path, chat='live-chat', *, agent=False, label='第一段'):
    meta = {'id': chat, 'cwd': str(root)}
    if agent:
        meta['source'] = {'subagent': 'fixture'}
    rows = [{'type': 'session_meta', 'payload': meta},
            {'type': 'turn_context', 'payload': {'turn_id': label}},
            message(f'整理{label}需求'), message(f'已记录{label}需求', 'assistant')]
    path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))
    return path


def test_excluded_legacy_source_cannot_block_later_human_boundary(tmp_path, monkeypatch):
    config, transport = configured(tmp_path, monkeypatch)
    bad = transcript(tmp_path, tmp_path / 'agent.jsonl', agent=True)
    good = transcript(tmp_path, tmp_path / 'human.jsonl')
    inbox.record(config, 'live-chat', 'PreCompact', str(bad))
    inbox.record(config, 'live-chat', 'PostCompact', str(good))
    result = maintain.run_chat(config, 'live-chat')
    assert result['state'] == 'done', result
    assert transport.calls == 1
    assert len(inbox.pending(config, 'live-chat')) == 0
    records = [json.loads(p.read_text()) for p in (inbox.root(config) / 'pending').glob('*.json')]
    assert any(r['state'] == 'done' and r['error_code'] == 'excluded_nonhuman_source' for r in records)
    assert maintain.run_chat(config, 'live-chat')['calls'] == 0


def test_rerouting_a_boundary_schedules_the_actual_owner(tmp_path, monkeypatch):
    config, transport = configured(tmp_path, monkeypatch)
    path = transcript(tmp_path, tmp_path / 'foreign.jsonl', chat='actual-chat')
    inbox.record(config, 'live-chat', 'PreCompact', str(path))
    spawned = []
    monkeypatch.setattr(maintain, 'spawn_worker', lambda *args: spawned.append(args))
    report = maintain.run_chat(config, 'live-chat')
    assert report['state'] == 'done', report
    assert transport.calls == 0
    assert [args[1] for args in spawned] == ['actual-chat']
    assert len(inbox.pending(config, 'actual-chat')) == 1


def test_busy_sibling_does_not_lose_boundary_arriving_during_model_call(tmp_path, monkeypatch):
    config, transport = configured(tmp_path, monkeypatch)
    path = transcript(tmp_path, tmp_path / 'first.jsonl')
    inbox.record(config, 'live-chat', 'PreCompact', str(path))
    original = transport.complete_compact
    siblings = []
    def complete(**kwargs):
        if not siblings:
            later = transcript(tmp_path, tmp_path / 'second.jsonl', label='第二段')
            inbox.record(config, 'live-chat', 'PostCompact', str(later))
            siblings.append(maintain.run_chat(config, 'live-chat'))
        return original(**kwargs)
    monkeypatch.setattr(transport, 'complete_compact', complete)
    result = maintain.run_chat(config, 'live-chat')
    assert siblings[0]['state'] == 'busy'
    assert result['state'] == 'done', result
    assert result['calls'] == 2
    assert transport.calls == 2
    assert not inbox.pending(config, 'live-chat')
    assert '第二段' in transport.payloads[-1]['turns'][0]['user']
    with Store(config, read_only=True) as store:
        assert store.db.execute('SELECT count(*) FROM compact_turn_commits WHERE chat_id=?', ('live-chat',)).fetchone()[0] == 2


def test_boundary_in_final_exit_check_is_processed_before_unlock(tmp_path, monkeypatch):
    config, transport = configured(tmp_path, monkeypatch)
    path = transcript(tmp_path, tmp_path / 'first.jsonl')
    inbox.record(config, 'live-chat', 'PreCompact', str(path))
    original = maintain._run_chat
    runs = []
    def at_exit(*args):
        result = original(*args)
        runs.append(result)
        if len(runs) == 1:
            later = transcript(tmp_path, tmp_path / 'last.jsonl', label='临界时序')
            inbox.record(config, 'live-chat', 'PostCompact', str(later))
        return result
    monkeypatch.setattr(maintain, '_run_chat', at_exit)
    result = maintain.run_chat(config, 'live-chat')
    assert result['state'] == 'done', result
    assert len(runs) == 2
    assert result['calls'] == 2
    assert not inbox.pending(config, 'live-chat')
