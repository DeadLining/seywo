"""Internal review transcripts cannot enter a human narrative at any boundary."""
import json
from pathlib import Path

import pytest

from seywo import hook, inbox, maintain
from seywo.admission import NonHumanSource, require_human
from seywo.config import initialize, load_config
from seywo.store import Store
from test_hook import run


def source(tmp_path, origin):
    initialize(tmp_path / 'project')
    config_path = tmp_path / 'project/.seywo/config.json'
    data = json.loads(config_path.read_text())
    data['hook'] = {'enabled': True}
    config_path.write_text(json.dumps(data))
    config = load_config(config_path)
    path = tmp_path / 'agent.jsonl'
    path.write_text(json.dumps({'type': 'session_meta', 'payload': {
        'id': 'physical-child', 'session_id': 'human-parent',
        'cwd': config['project_root'], 'source': origin}}) + '\n')
    return config, path


@pytest.mark.parametrize('origin', [{'subagent': {'other': 'guardian'}},
                                    {'subagent': 'review'}, {'subagent': None}, 'subagent'])
def test_hook_ignores_internal_sources_before_snapshot(tmp_path, monkeypatch, origin):
    config, path = source(tmp_path, origin)
    assert run(monkeypatch, {'hook_event_name': 'PreCompact', 'session_id': 'human-parent',
                            'cwd': config['project_root'], 'transcript_path': str(path)}) == []
    assert not (Path(config['_path']).parent / 'inbox').exists()


def test_direct_import_rejects_agent_atomically(tmp_path):
    config, path = source(tmp_path, {'subagent': {'other': 'guardian'}})
    with Store(config) as store:
        with pytest.raises(NonHumanSource, match='nonhuman_subagent_source'):
            store.import_source(path, 'physical-child', 0)
        for table in ['chats', 'source_segments', 'source_snapshots', 'source_fragments']:
            assert store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0


@pytest.mark.parametrize('metadata', [[], 7, None, {'type': 'session_meta', 'payload': []}])
def test_malformed_metadata_uses_identity_fallback(tmp_path, metadata):
    path = tmp_path / 'bad.jsonl'
    path.write_text(json.dumps(metadata) + '\n')
    assert hook.chat_identity(str(path), 'payload-chat') == 'payload-chat'


def test_explicit_human_child_keeps_physical_identity(tmp_path):
    _, path = source(tmp_path, 'vscode')
    assert hook.chat_identity(str(path), 'human-parent') == 'physical-child'
    require_human({})  # old metadata without provenance remains compatible


def test_previously_queued_agent_is_terminally_excluded(tmp_path, monkeypatch):
    config, path = source(tmp_path, {'subagent': {'other': 'guardian'}})
    # Simulate an inbox record written by the older hook.
    inbox.record(config, 'human-parent', 'PreCompact', str(path))
    monkeypatch.setattr(maintain, 'read_provider', lambda config: {
        'endpoint': 'http://127.0.0.1:1/responses', 'model': 'fake',
        'timeout': 5, 'max_output_tokens': 4000})
    monkeypatch.setattr(maintain, 'load_key', lambda endpoint: 'test-key')
    def forbidden(*args, **kwargs):
        raise AssertionError('agent source must not cause a request or reroute')
    monkeypatch.setattr(maintain.Transport, 'complete', forbidden)
    monkeypatch.setattr(maintain, 'spawn_worker', forbidden)
    maintain.run_chat(config, 'human-parent')
    assert inbox.pending(config) == []
    records = list((Path(config['_path']).parent / 'inbox/pending').glob('*.json'))
    assert len(records) == 1
    assert json.loads(records[0].read_text())['error_code'] == 'excluded_nonhuman_source'
    with Store(config) as store:
        assert store.db.execute('SELECT count(*) FROM source_segments').fetchone()[0] == 0


def test_existing_agent_rows_are_preserved_but_not_published_or_resumed(tmp_path, monkeypatch):
    from seywo.narrative import Narrative
    import seywo.store as store_module
    config, path = source(tmp_path, {'subagent': {'other': 'guardian'}})
    # A database produced by the prior version, without its admission guard.
    with monkeypatch.context() as old:
        old.setattr(store_module, 'require_human', lambda meta: None)
        with Store(config) as store:
            store.import_source(path, 'physical-child', 0)
    with Store(config) as store:
        with pytest.raises(NonHumanSource):
            Narrative(store).read('physical-child')
        assert store.db.execute('SELECT count(*) FROM source_segments').fetchone()[0] == 1
    monkeypatch.setattr(maintain, 'read_provider', lambda config: {
        'endpoint': 'http://127.0.0.1:1/responses', 'model': 'fake',
        'timeout': 5, 'max_output_tokens': 4000})
    monkeypatch.setattr(maintain, 'load_key', lambda endpoint: 'test-key')
    report = maintain.run_chat(config, 'physical-child')
    assert report['state'] == 'excluded' and report['calls'] == 0
