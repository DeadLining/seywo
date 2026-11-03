"""Global write defaults, using isolated synthetic projects, never real chat data."""
import json
from pathlib import Path

import pytest

from sessiongraph import run_receipts, source_text
from sessiongraph.auto_update import _folder, run_update
from sessiongraph.core import Store
from test_input_framework import fixture, legacy_fixture, msg


@pytest.mark.parametrize('project', ['first', 'second'])
def test_every_new_project_uses_source_index_and_receipt_defaults(tmp_path, project):
    s, p = fixture(tmp_path / project, [msg('original alpha')])
    assert source_text.enabled(s) and source_text.unified(s)
    assert run_receipts.enabled(s.db)
    assert s.db.execute('SELECT text FROM entries').fetchone()[0] == ''
    assert s.db.execute('SELECT count(*) FROM docs').fetchone()[0] == 0
    assert s.db.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 0
    eid = s.search('alpha', mode='raw')['hits'][0]['id']
    assert s.expand(eid, radius=0)['entries'][0]['text'] == 'original alpha'
    with s.db:
        source_text.write_doc(s, 'reviewed', 'record', 'reviewed beta')
        source_text.write_doc(s, 'memory', 'memory', 'duplicate beta')
    assert s.db.execute('SELECT count(*) FROM docs').fetchone()[0] == 0
    assert s.db.execute("SELECT count(*) FROM sg_search_keys WHERE kind='record'").fetchone()[0] == 1
    s.close()


def test_legacy_next_write_switches_without_migrating_or_hiding_history(tmp_path, monkeypatch):
    s, p = legacy_fixture(tmp_path, [msg('historical alpha')])
    config = s.config
    s.close()
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', lambda *a, **k: pytest.fail('upgrade opened a source file'))
        s = Store(config)
    assert source_text.enabled(s) and not source_text.unified(s)
    assert s.db.execute('SELECT text FROM entries').fetchone()[0] == 'historical alpha'
    assert s.db.execute('SELECT count(*) FROM docs').fetchone()[0] == 1
    with p.open('a') as f:
        f.write(json.dumps(dict(type='response_item', timestamp='new', payload=msg('future beta'))) + '\n')
    assert s.ingest(p)['added'] == 1
    assert s.db.execute('SELECT count(*) FROM docs').fetchone()[0] == 1
    assert s.db.execute('SELECT text FROM entries ORDER BY line DESC').fetchone()[0] == ''
    assert s.search('alpha', mode='raw')['hits']
    assert s.search('beta', mode='raw')['hits']
    # Historical inline evidence remains usable even if its original later disappears.
    p.unlink()
    assert s.search('alpha', mode='raw')['hits']
    assert not s.search('beta', mode='raw')['hits']
    assert s.db.execute('PRAGMA foreign_key_check').fetchall() == []
    s.close()


def test_readonly_legacy_store_does_not_activate_defaults(tmp_path):
    s, _ = legacy_fixture(tmp_path, [msg('historical alpha')])
    before = [tuple(r) for r in s.db.execute('SELECT * FROM metadata ORDER BY key')]
    config = s.config
    s.close()
    with_store = Store(config, readonly=True)
    assert not source_text.enabled(with_store) and not run_receipts.enabled(with_store.db)
    assert [tuple(r) for r in with_store.db.execute('SELECT * FROM metadata ORDER BY key')] == before
    assert with_store.db.total_changes == 0
    with_store.close()


def test_activation_is_atomic_and_idempotent(tmp_path, monkeypatch):
    s, _ = legacy_fixture(tmp_path, [msg('historical alpha')])
    original = run_receipts.setup
    def fail(store):
        original(store)
        raise ValueError('injected_activation_failure')
    with monkeypatch.context() as patch:
        patch.setattr(run_receipts, 'setup', fail)
        with pytest.raises(ValueError, match='injected_activation_failure'):
            source_text.activate_defaults(s)
    assert not source_text.enabled(s) and not run_receipts.enabled(s.db)
    assert not s.db.execute("SELECT 1 FROM metadata WHERE key='storage_defaults_v1'").fetchone()
    source_text.activate_defaults(s)
    count = s.db.total_changes
    source_text.activate_defaults(s)
    assert s.db.total_changes == count
    assert s.db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] == '4'
    s.close()


def test_terminal_receipt_archives_without_manual_maintenance(tmp_path):
    s, _ = fixture(tmp_path, [msg('original alpha')])
    def execute(argv, timeout):
        return {'added_entries': 0, 'skipped': []} if 'refresh-chat' in argv else {
            'saved': [], 'failed': [], 'model_calls': 0}
    run = run_update(s.config['_config_path'], 'input-chat', execute=execute)
    assert run['state'] == 'succeeded'
    assert not (_folder(s.config) / (run['run_id'] + '.json')).exists()
    assert json.loads(run_receipts.read(s.config, run['run_id'])) == run
    s.close()
