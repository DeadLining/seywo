"""Terminal cache lifecycle and split-safe pending resolution; no providers."""
import copy
import json
import pytest
from sessiongraph import graph_v2 as g, storage
from sessiongraph.graph_contract import TYPES, RELATIONS, policy, relation_type_allowed
from test_memories import make_store, entries, FakeExtractor
from test_graph_v2 import output, Judge


def completed(tmp_path, support='supports'):
    _, s, _ = make_store(tmp_path, [('assistant', '训练 A 得到模型 B')])
    o = output(entries(s))
    result = g.run_chat(s, FakeExtractor([o]), 'chat-1', jev=Judge(support=support))
    assert not result['failed']
    return s, o


def test_complete_reclaims_cache_but_preserves_audit_and_does_not_replay(tmp_path):
    s, _ = completed(tmp_path)
    assert not s.db.execute('SELECT * FROM sg2_checkpoints').fetchall()
    assert s.db.execute('SELECT count(*) FROM sg2_attempts').fetchone()[0] >= 4
    assert s.db.execute('SELECT output_json FROM sg2_jobs').fetchone()[0]
    assert s.db.execute('SELECT count(*) FROM sg_input_coverage').fetchone()[0]
    replay = g.run_chat(s, FakeExtractor([]), 'chat-1', jev=Judge())
    assert replay['model_calls'] == 0


@pytest.mark.parametrize('blocker', ['pending','superseded','running','claim','control','other_project',None])
def test_gc_only_finalized_project_jobs_and_rollback(tmp_path, blocker):
    s, _ = completed(tmp_path)
    job = s.db.execute('SELECT id FROM sg2_jobs').fetchone()[0]
    with s.db:
        s.db.execute("INSERT INTO sg2_checkpoints VALUES(?, 'part', 'identity', ?)", (job, json.dumps({'audit':'中文'})))
        if blocker in ('pending','superseded'):
            s.db.execute('UPDATE sg2_jobs SET state=?', (blocker,))
        elif blocker == 'running':
            s.db.execute("UPDATE sg2_attempts SET state='running'")
        elif blocker == 'claim':
            s.db.execute('INSERT INTO sg2_claims VALUES(?,?,?,?,?)', (job,'e','hash',0,1))
        elif blocker == 'control':
            s.db.execute("INSERT INTO sg2_control VALUES(?, 'blocked', 0, 1, '{}')", (job,))
        elif blocker == 'other_project':
            s.db.execute("UPDATE sg2_jobs SET project='another'")
    before = [tuple(r) for r in s.db.execute('SELECT * FROM sg2_jobs')]
    s.db.execute('BEGIN IMMEDIATE')
    result = storage.prune_completed_checkpoints(s)
    assert result['checkpoints'] == (0 if blocker else 1)
    s.db.rollback()
    assert s.db.execute('SELECT count(*) FROM sg2_checkpoints').fetchone()[0] == 1
    with s.db:
        storage.prune_completed_checkpoints(s)
    assert before == [tuple(r) for r in s.db.execute('SELECT * FROM sg2_jobs')]
    assert storage.prune_completed_checkpoints(s)['checkpoints'] == 0


def resolve(s, o):
    with s.db:
        g._resolve_pending(s, {'id':'resolver'}, o, [], {'n1':'a','n2':'b'},
                           [(o['relations'][0], 'asserted', {})])


def test_pending_edge_survives_cleared_output_and_reclaims_only_search_terms(tmp_path):
    s, o = completed(tmp_path, 'insufficient_evidence')
    row = s.db.execute('SELECT * FROM sg2_unresolved').fetchone()
    assert json.loads(row['payload_json'])['_endpoint_keys']
    with s.db:
        s.db.execute('UPDATE sg2_jobs SET output_json=NULL')
    resolve(s, o)
    assert s.db.execute('SELECT revision FROM sg2_unresolved').fetchone()[0] == 'resolver'
    assert s.db.execute('SELECT count(*) FROM sg2_pending_terms').fetchone()[0] == 0
    assert s.db.execute('SELECT payload_json FROM sg2_unresolved').fetchone()[0] == row['payload_json']


def test_pending_node_does_not_require_job_output(tmp_path):
    s, o = completed(tmp_path, 'insufficient_evidence')
    with s.db:
        s.db.execute('UPDATE sg2_jobs SET output_json=NULL')
        s.db.execute('UPDATE sg2_unresolved SET payload_json=?', (json.dumps(o['nodes'][0]),))
    resolve(s, o)
    assert s.db.execute('SELECT revision FROM sg2_unresolved').fetchone()[0] == 'resolver'


@pytest.mark.parametrize('legacy', [False,True])
def test_reused_local_ids_must_not_resolve_different_endpoints(tmp_path, legacy):
    s, o = completed(tmp_path, 'insufficient_evidence')
    if legacy:
        with s.db:
            s.db.execute('UPDATE sg2_unresolved SET payload_json=?',(json.dumps(o['relations'][0]),))
    changed = copy.deepcopy(o)
    changed['nodes'][0]['title']='different execution'
    resolve(s, changed)
    assert s.db.execute('SELECT revision FROM sg2_unresolved').fetchone()[0] == ''


@pytest.mark.parametrize('with_part', [False,True])
def test_legacy_split_resolution_uses_matching_part_not_latest_output(tmp_path, with_part):
    s, o = completed(tmp_path, 'insufficient_evidence')
    job = s.db.execute('SELECT id FROM sg2_jobs').fetchone()[0]
    with s.db:
        s.db.execute('UPDATE sg2_unresolved SET payload_json=?',(json.dumps(o['relations'][0]),))
        s.db.execute('UPDATE sg2_jobs SET output_json=NULL')
        if with_part:
            s.db.execute('INSERT INTO sg2_job_parts VALUES(?,?,?,?,?,?)',('part',job,'{}',json.dumps(o),'{}','now'))
    resolve(s, o)
    assert s.db.execute('SELECT revision FROM sg2_unresolved').fetchone()[0] == ('resolver' if with_part else '')


def test_type_contract_all_pairs_preserves_existing_matrix():
    rules=policy([])['relation_endpoint_types']
    expected={
        'PART_OF': {(a,'Activity') for a in ('Activity','Execution')},
        'USES': {(a,b) for a in ('Activity','Execution') for b in ('Artifact','Source')},
        'PRODUCES': {(a,'Artifact') for a in ('Activity','Execution')},
        'HAS_RESULT': {('Execution','Claim')},
        'SUPPORTS': {(a,b) for a in ('Claim','Source','Artifact') for b in ('Claim','Decision')},
        'CONTRADICTS': {('Claim','Claim')},
        'SUPERSEDES': {(a,a) for a in TYPES},
        'DEPENDS_ON': {(a,b) for a in TYPES for b in TYPES},
    }
    for kind in RELATIONS:
        rule=rules[kind]
        for a in TYPES:
            for b in TYPES:
                allowed=(a,b) in expected[kind]
                assert relation_type_allowed(kind,a,b) == allowed
                assert (a in rule['source_types'] and b in rule['target_types']
                        and (not rule['same_type'] or a==b)) == allowed
    assert not relation_type_allowed('unknown','Activity','Activity')


def test_legacy_ambiguous_slot_reuse_never_closes_a_candidate(tmp_path):
    s, o = completed(tmp_path, 'insufficient_evidence')
    job=s.db.execute('SELECT id FROM sg2_jobs').fetchone()[0]
    changed=copy.deepcopy(o);changed['nodes'][0]['title']='different execution'
    with s.db:
        s.db.execute('UPDATE sg2_unresolved SET payload_json=?',(json.dumps(o['relations'][0]),))
        s.db.execute('INSERT INTO sg2_job_parts VALUES(?,?,?,?,?,?)',('ambiguous',job,'{}',json.dumps(changed),'{}','now'))
    resolve(s,o)
    assert s.db.execute('SELECT revision FROM sg2_unresolved').fetchone()[0]==''


def test_maintenance_refuses_an_active_project_batch(tmp_path, monkeypatch, capsys):
    import fcntl
    from pathlib import Path
    from sessiongraph import cli
    s,_=completed(tmp_path)
    config=Path(s.config['database']).parent/'config.json'
    def forbidden(*args):
        pytest.fail('migration must not run while graph writer holds lock')
    monkeypatch.setattr(storage,'migrate_sources',forbidden)
    with (config.parent/'graph-v2.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        assert cli.main(['--config',str(config),'storage-maintain','--apply'])==2
    assert 'project batch active' in capsys.readouterr().err
