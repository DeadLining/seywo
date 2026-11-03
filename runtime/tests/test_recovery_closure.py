import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from sessiongraph import continuation as c, evidence_recovery as repair, graph_v2 as g
from sessiongraph.graph_contract import policy, prepare_evidence_spans, decode_evidence_candidates, validate
from test_continuation import configured, receipt
from test_memories import make_store
from test_status_recovery import blocked


def test_replayed_input_and_calls_are_not_durable_progress(configured):
    path, config = configured
    for stamp in range(100, 103):
        c.record(path, receipt(event='AutoContinue', updated_at=stamp,
            reported_model_calls=3, graph_v2={'durable_stages':True,
                'progress_tracked':True,'durable_progress':0,
                'input_source_chars':24000,'nodes_saved':9,'relations_saved':4,
                'source_remaining':True,'runnable_remaining':True}), launch=False, now=stamp)
    item = c.snapshot(config)[0]
    assert item['state'] == 'blocked' and item['error_code'] == 'continuation_no_progress'


def test_persisted_judgment_is_progress_before_publication(configured):
    path, config = configured
    for stamp in range(100, 106):
        c.record(path, receipt(event='AutoContinue', updated_at=stamp,
            graph_v2={'durable_stages':True,'progress_tracked':True,
                'durable_progress':1,'source_remaining':True,
                'runnable_remaining':True}), launch=False, now=stamp)
    assert c.snapshot(config)[0]['state'] == 'ready'
    assert c.snapshot(config)[0]['no_progress'] == 0


@pytest.mark.parametrize('reason', ['graph_evidence:invalid_atomic_source_ref', 'graph_evidence:unknown_source_anchor'])
def test_reference_repair_exhaustion_splits_instead_of_repeating_large_request(reason):
    assert g._should_split('provider_evidence_reference', reason, {'repair_attempts':1})
    assert not g._should_split('provider_evidence_reference', reason, {})
    assert not g._should_split('provider_evidence_reference', 'graph_source_changed', {'repair_attempts':1})


def recovery_fixture(tmp_path, monkeypatch):
    _, store, _ = make_store(tmp_path, [('user', 'source')])
    store.config.setdefault('graph_v2', {}).update(enabled=True,
        revision_policy={'enabled':True, 'mode':'effective'})
    blocked(store, code='provider_evidence_reference')
    store.db.execute("UPDATE sg2_jobs SET input_json=?,output_json=NULL,candidates_json='[]'",
        (json.dumps({'entries':[],'end':[1,0]}),))
    case = json.loads((Path(__file__).parent/'fixtures/fruit_fly_atomic_boundary.json').read_text())[0]
    monkeypatch.setattr(g, '_restore_batch', lambda *args:copy.deepcopy(case['batch']))
    prepared, _, _ = prepare_evidence_spans(case['batch'], policy([], effective=True))
    audit = dict(id='a'*32,job_id='blocked',stage='extract',attempt_id='attempt',part='part',
        status='completed',http_status=200,created_at=1,final_output=json.dumps(case['output']),
        request={'input':[{'content':[{'text':json.dumps({'untrusted_transcript':prepared})}]}]})
    folder = Path(store.config['database']).parent/'request-audit'
    folder.mkdir()
    path = folder/f'request-{audit["id"]}.json'
    path.write_text(json.dumps(audit))
    store.db.execute("INSERT INTO sg2_attempts VALUES('attempt','blocked','part','extract','failed',1,2,NULL,NULL,NULL)")
    store.db.commit()
    monkeypatch.setattr(repair.recovery, 'wake', lambda *a, **kw:[])
    return store, path, audit


def test_exact_audit_recovery_is_model_free_once_only_and_keeps_history(tmp_path, monkeypatch):
    store, path, audit = recovery_fixture(tmp_path, monkeypatch)
    before = path.read_bytes()
    changes = store.db.total_changes
    preview = repair.repair(store)
    assert preview['candidates'][0]['action'] == 'reuse_validated_response'
    assert store.db.total_changes == changes
    result = repair.repair(store, apply=True, launch=False)
    assert result['reused'] == 1 and result['model_calls'] == 0
    row = store.db.execute('SELECT * FROM sg2_jobs').fetchone()
    assert row['state'] == 'judging'
    assert json.loads(row['output_json'])['nodes'][0]['assertion'][0]['subject'] == 'LIF 与双层 MLP'
    assert row['error'] == 'provider_evidence_reference' and path.read_bytes() == before
    store.db.execute("UPDATE sg2_control SET state='blocked'");store.db.commit()
    assert repair.repair(store, apply=True, retry_invalid=True, launch=False)['recovered'] == 0


@pytest.mark.parametrize('defect', ['changed_input', 'truncated', 'wrong_attempt', 'invalid_output', 'wrong_filename'])
def test_audit_recovery_fails_closed(tmp_path, monkeypatch, defect):
    store, path, audit = recovery_fixture(tmp_path, monkeypatch)
    if defect == 'changed_input':
        audit['request']['input'][0]['content'][0]['text'] = '{}'
    elif defect == 'truncated':
        audit['final_output_truncated'] = True
    elif defect == 'wrong_attempt':
        audit['attempt_id'] = 'unrelated'
    elif defect == 'invalid_output':
        audit['final_output'] = '{}'
    else:
        audit['id'] = 'b'*32
    path.write_text(json.dumps(audit))
    assert repair.repair(store, apply=True, launch=False)['recovered'] == 0
    assert store.db.execute('SELECT output_json FROM sg2_jobs').fetchone()[0] is None


def test_changed_source_is_not_retried_away(tmp_path, monkeypatch):
    store, _, _ = recovery_fixture(tmp_path, monkeypatch)
    def changed(*args):
        raise ValueError('graph_source_changed')
    monkeypatch.setattr(g, '_restore_batch', changed)
    assert repair.repair(store, apply=True, retry_invalid=True, launch=False)['recovered'] == 0


@pytest.mark.parametrize('changed', [False, True])
def test_diagnosed_baseline_retry_keeps_output_and_source_gate(tmp_path, monkeypatch, changed):
    store, _, _ = recovery_fixture(tmp_path, monkeypatch)
    store.db.execute("UPDATE sg2_control SET error_json=?",
        (json.dumps({'code':'revision_baseline_unavailable'}),))
    store.db.execute("UPDATE sg2_jobs SET output_json='{}'")
    store.db.commit()
    if changed:
        def missing(*args):
            raise ValueError('graph_source_changed')
        monkeypatch.setattr(g, '_restore_batch', missing)
    result = repair.repair(store, apply=True, retry_invalid=True, launch=False)
    assert result['recovered'] == (0 if changed else 1)
    assert store.db.execute('SELECT output_json FROM sg2_jobs').fetchone()[0] == '{}'
    if not changed:
        store.db.execute("UPDATE sg2_control SET state='blocked'")
        store.db.commit()
        assert repair.repair(store, apply=True, retry_invalid=True, launch=False)['recovered'] == 0


def test_deleted_context_is_optional_but_reserved_source_is_not(tmp_path):
    _, store, _ = make_store(tmp_path, [('user', 'source')])
    missing = {'id':'deleted','context_only':True}
    source = {'id':'also-missing'}
    desc = {'entries':[source, missing], 'coverage':[['also-missing','hash',0,6]]}
    fixed = g._without_deleted_context(store, desc)
    assert fixed['entries'] == [source] and fixed['coverage'] == desc['coverage']
    assert len(desc['entries']) == 2
    protected = {**desc,'coverage':[['deleted','hash',0,6]]}
    assert g._without_deleted_context(store, protected) == protected


def test_invalid_atomic_candidate_never_publishes_or_blocks_a_valid_neighbor(tmp_path):
    from test_atomic_boundary import boundary_case
    from test_graph_v2 import Judge
    batch, _, _, anchors, wire = boundary_case()
    invalid = copy.deepcopy(wire['nodes'][0])
    invalid.update(id='n2', title='unsupported derived result')
    invalid['assertion']['value']['text'] = 'invented conclusion'
    wire['nodes'].append(invalid)
    before = copy.deepcopy(wire)
    decoded = decode_evidence_candidates(wire, anchors, entry_ids=['e1'], atomic_refs=True)
    validate(decoded, batch, [])
    good, bad = decoded['nodes']
    assert good['assertion'] and good['context_kind'] == 'work'
    assert bad['assertion'] == [] and bad['context_kind'] == 'uncertain'
    assert wire == before
    _, store, _ = make_store(tmp_path, [('user', batch[0]['text'])])
    store.config.setdefault('graph_v2', {}).update(enabled=True,
        revision_policy={'enabled':True,'mode':'effective'})
    g.setup(store)
    # No identity or relation model can turn the isolated candidate into a binding.
    bindings, pending = g._identities(store, {'nodes':[bad]}, [], Judge(), g._stats())
    assert 'n2' not in bindings and pending == [(bad, 'context_uncertain')]
    invalid['evidence'] = [{'anchor_id':'missing'}]
    wire['nodes'] = [invalid]
    with pytest.raises(ValueError, match='unknown_source_anchor'):
        decode_evidence_candidates(wire, anchors, entry_ids=['e1'], atomic_refs=True)


def test_desktop_bootstrap_uses_active_runtime_and_preserves_app_identity(tmp_path):
    root = Path(__file__).resolve().parents[2]
    bundle = tmp_path/'SessionGraph.app/Contents/Resources/sessiongraph'
    bundle.mkdir(parents=True)
    shutil.copy2(root/'runtime/entrypoint.py', bundle/'entrypoint.py')
    state = tmp_path/'state'
    active = state/'runtimes/fixed'
    active.mkdir(parents=True)
    (active/'entrypoint.py').write_text('import json,os\nprint(json.dumps(dict(active=True,app=os.environ.get("SESSIONGRAPH_APP_SOURCE"))))\n')
    manifest = state/'runtime.json'
    manifest.write_text(json.dumps({'source':str(active), 'python':sys.executable}))
    env = {**os.environ, 'SESSIONGRAPH_RUNTIME_HOME':str(state)}
    result = subprocess.run([sys.executable, '-B', str(bundle/'entrypoint.py'), 'native-settings'],
        env=env, text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == dict(active=True, app=str(bundle))
    manifest.write_text(json.dumps({'source':str(state/'missing')}))
    result = subprocess.run([sys.executable, '-B', str(bundle/'entrypoint.py'), 'native-settings'],
        env=env, text=True, capture_output=True)
    assert result.returncode != 0 and 'refusing stale bundle fallback' in result.stderr
