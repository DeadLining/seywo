"""Regression for successful split parts inheriting another part's failures."""
import json
from sessiongraph import graph_v2 as g
from sessiongraph.extraction import ProviderError
from test_memories import make_store
from test_graph_v2 import Judge


class Extractor:
    def __init__(self):
        self.calls = 0
        self.mode = 'limit'

    def __call__(self, batch, policy):
        self.calls += 1
        if self.mode == 'limit':
            raise ProviderError('output too long', 'provider_output_limit')
        if self.mode == 'timeout':
            raise ProviderError('timed out', 'provider_timeout')
        return dict(schema_version=g.VERSION, nodes=[], relations=[], unresolved=[],
                    coverage=dict(entry_ids=[e['id'] for e in batch], outcome='no_durable_item'))


def step(store, extractor, **kwargs):
    return g.run_chat(store, extractor, 'chat-1', jev=Judge(), step_budget=1, **kwargs)


def setup_split(tmp_path):
    _, store, _ = make_store(tmp_path, [('assistant', 'abcdefghij ' * 400)])
    ex = Extractor()
    step(store, ex)
    desc = json.loads(store.db.execute('SELECT input_json FROM sg2_jobs').fetchone()[0])
    assert desc['remaining'] and desc['part_depth'] == 1
    ex.mode = 'timeout'
    step(store, ex)
    step(store, ex)
    assert store.db.execute('SELECT failures FROM sg2_control').fetchone()[0] == 2
    return store, ex


def test_completed_part_resets_retry_state_atomically(tmp_path):
    store, ex = setup_split(tmp_path)
    ex.mode = 'success'
    step(store, ex)
    assert store.db.execute('SELECT count(*) FROM sg2_job_parts').fetchone()[0] == 1
    assert store.db.execute('SELECT state FROM sg2_jobs').fetchone()[0] == 'pending'
    assert store.db.execute('SELECT count(*) FROM sg2_control').fetchone()[0] == 0
    assert store.db.execute("SELECT count(*) FROM sg2_attempts WHERE state='failed'").fetchone()[0] == 3
    assert store.db.execute('SELECT count(*) FROM sg2_claims').fetchone()[0] > 0
    assert store.db.execute('SELECT count(*) FROM sg2_cursors').fetchone()[0] == 0


def test_next_part_gets_its_own_bounded_retries_and_can_finish(tmp_path):
    store, ex = setup_split(tmp_path)
    ex.mode = 'success'
    step(store, ex)
    ex.mode = 'timeout'
    step(store, ex)
    step(store, ex)
    row = store.db.execute('SELECT state, failures FROM sg2_control').fetchone()
    assert tuple(row) == ('retry_wait', 2)
    store.db.execute('UPDATE sg2_control SET due=0')
    store.db.commit()
    ex.mode = 'success'
    step(store, ex, respect_schedule=True)
    assert store.db.execute('SELECT state FROM sg2_jobs').fetchone()[0] == 'complete'
    assert store.db.execute('SELECT count(*) FROM sg2_job_parts').fetchone()[0] == 2
    assert store.db.execute('SELECT count(*) FROM sg2_control').fetchone()[0] == 0


def test_same_part_still_blocks_after_four_failures(tmp_path):
    store, ex = setup_split(tmp_path)
    step(store, ex)
    step(store, ex)
    assert tuple(store.db.execute('SELECT state, failures FROM sg2_control').fetchone()) == ('blocked', 4)
    before = ex.calls
    step(store, ex, respect_schedule=True)
    assert ex.calls == before
