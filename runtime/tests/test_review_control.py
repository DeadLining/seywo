"""Explicit review-control fixtures, not classifier or user-approval acceptance."""
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from sessiongraph import graph_v2 as graph, revision_ledger as ledger, review_control as controls
from sessiongraph.core import Store
from sessiongraph.failures import Failure
from test_revision_ledger import env, publish, review, apply, item


def manifest(store, review_id, source, *, action='retract', previous=None, **overrides):
    evidence = store.db.execute('SELECT evidence FROM sg_revision_sources WHERE revision=? ORDER BY evidence LIMIT 1', (source,)).fetchone()[0]
    return {**dict(version=controls.VERSION, project_id=store.config['project_id'],
        review=review_id, previous=previous or review_id, action=action, source=source,
        evidence=evidence, authority='source_report', effective_from='2026-10-24T01:00:00Z',
        replacement=None), **overrides}


def commit(store, value):
    from sessiongraph.maintenance import project_lock
    with project_lock(store.config), store.db:
        store.db.execute('BEGIN IMMEDIATE')
        return controls.control(store, value, apply=True)


def fixture(store, data, *, kind='Claim'):
    a=publish(store,data[0],'initial',kind=kind)
    b=publish(store,data[1],'correction',kind=kind)
    rid=apply(store,review(store,b,a))['review_id']
    return a,b,rid


def test_retract_restore_and_old_replay_never_prove_original_true(env):
    _,s,_,e=env
    a,b,rid=fixture(s,e)
    request=manifest(s,rid,b)
    first=commit(s,request)
    result=item(s,a)
    assert result['scopes']==[] and result['validity']=='unassessed'
    assert result['effects'][0]['state']=='retracted'
    restored=commit(s,manifest(s,rid,b,action='restore',previous=first['control_id'],effective_from='2026-10-24T02:00:00Z'))
    assert item(s,a)['scopes'][0]['state']=='corrected'
    assert commit(s,request)['control_id']==first['control_id']
    assert controls.read(s,rid)['control']['head']==restored['control_id']
    assert item(s,a)['scopes'][0]['state']=='corrected'
    assert s.db.execute('SELECT count(*) FROM sg_revision_review_controls').fetchone()[0]==2
    assert s.db.execute('SELECT count(*) FROM sg_revision_reviews').fetchone()[0]==1


def test_stale_expected_head_cannot_fork_even_at_newer_effective_time(env):
    _,s,_,e=env
    _,b,rid=fixture(s,e)
    commit(s,manifest(s,rid,b))
    with pytest.raises(Failure,match='control_stale_head'):
        commit(s,manifest(s,rid,b,action='restore',effective_from='2026-10-24T03:00:00Z'))
    assert s.db.execute('SELECT count(*) FROM sg_revision_review_controls').fetchone()[0]==1


def test_atomic_amendment_changes_scope_without_overwriting_old_review(env):
    _,s,_,e=env
    a,b,rid=fixture(s,e)
    replacement=review(s,b,a,'disputes',conditions={'split':'fixed'},effective_from='2026-10-24T01:00:00Z')
    value=manifest(s,rid,b,action='amend',replacement=replacement)
    result=commit(s,value)
    assert result['replacement_review']!=rid
    assert item(s,a)['scopes']==[dict(facet='held-out evaluation',conditions={'split':'fixed'},state='disputed')]
    assert {r['state'] for r in item(s,a)['effects']}=={'retracted','reviewed'}
    assert commit(s,value)['control_id']==result['control_id']
    assert s.db.execute('SELECT count(*) FROM sg_revision_reviews').fetchone()[0]==2
    assert s.db.execute('SELECT count(*) FROM sg_revision_review_controls').fetchone()[0]==1


def test_caught_failure_rolls_back_both_parts_of_amendment_inside_caller_transaction(env):
    _,s,_,e=env
    a,b,rid=fixture(s,e)
    replacement=review(s,b,a,'disputes',effective_from='2026-10-24T01:00:00Z')
    with s.db:
        s.db.execute("CREATE TRIGGER fail_control BEFORE INSERT ON sg_revision_review_controls BEGIN SELECT RAISE(ABORT,'synthetic fail'); END")
    with s.db:
        s.db.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.IntegrityError,match='synthetic fail'):
            controls.control(s,manifest(s,rid,b,action='amend',replacement=replacement),apply=True)
        s.db.execute("INSERT OR REPLACE INTO metadata VALUES('unrelated_fixture','preserved')")
    assert s.db.execute('SELECT count(*) FROM sg_revision_reviews').fetchone()[0]==1
    assert s.db.execute('SELECT count(*) FROM sg_revision_review_controls').fetchone()[0]==0
    assert s.db.execute("SELECT value FROM metadata WHERE key='unrelated_fixture'").fetchone()[0]=='preserved'


@pytest.mark.parametrize('change',[{'role':'user'},{'text':'rewritten source'},{'timestamp':'changed'},{'call_id':'changed'}])
def test_invalid_control_evidence_requires_review_never_falls_back_to_original(env,change):
    _,s,_,e=env
    a,b,rid=fixture(s,e)
    c=publish(s,e[3],'review audit: first correction was mistaken')
    commit(s,manifest(s,rid,c))
    key,value=next(iter(change.items()))
    with s.db:s.db.execute(f'UPDATE entries SET {key}=? WHERE id=?',(value,e[3]['id']))
    result=item(s,a)
    assert result['effects'][0]['state']=='needs_review'
    assert result['scopes'][0]['state']=='needs_review'
    history=controls.read(s,rid)
    assert history['control']['state']=='needs_review'
    assert history['actions'][0]['source_ref']['entry_id']==e[3]['id']
    assert history['actions'][0]['source_ref']['role']=='assistant'
    assert history['actions'][0]['source_ref']['source_time']=='2026-09-30T00:00:00Z'


def test_new_explicit_control_can_correct_an_invalid_prior_control_with_fresh_evidence(env):
    _,s,_,e=env
    a,b,rid=fixture(s,e)
    c=publish(s,e[3],'review audit')
    first=commit(s,manifest(s,rid,c))
    with s.db:s.db.execute("UPDATE entries SET text='changed' WHERE id=?",(e[3]['id'],))
    second=commit(s,manifest(s,rid,b,previous=first['control_id'],effective_from='2026-10-24T02:00:00Z'))
    assert controls.read(s,rid)['control']['head']==second['control_id']
    assert item(s,a)['scopes']==[] and item(s,a)['validity']=='unassessed'


def test_control_can_retract_review_with_invalid_original_source_without_claiming_truth(env):
    _,s,_,e=env
    a,b,rid=fixture(s,e)
    c=publish(s,e[3],'review audit')
    with s.db:s.db.execute("UPDATE entries SET text='changed' WHERE id=?",(e[1]['id'],))
    commit(s,manifest(s,rid,c))
    assert item(s,a)['effects'][0]['state']=='retracted'
    assert item(s,a)['scopes']==[] and item(s,a)['validity']=='unassessed'


def test_effective_time_uses_explicit_chain_and_same_time_actions_not_hash_order(env):
    _,s,_,e=env
    a,b,rid=fixture(s,e)
    first=commit(s,manifest(s,rid,b))
    second=commit(s,manifest(s,rid,b,action='restore',previous=first['control_id']))
    assert item(s,a,at='2026-10-24T00:59:59Z')['scopes'][0]['state']=='corrected'
    assert controls.read(s,rid,at='2026-10-24T01:00:00Z')['control']['head']==second['control_id']
    assert item(s,a,at='2026-10-24T01:00:00Z')['scopes'][0]['state']=='corrected'


def test_control_cannot_reorder_scheduled_future_head(env):
    _,s,_,e=env
    _,b,rid=fixture(s,e)
    first=commit(s,manifest(s,rid,b,effective_from='2026-10-26T00:00:00Z'))
    assert controls.read(s,rid,at='2026-10-24T01:00:00Z')['control']['head']==rid
    assert controls.read(s,rid,at='2026-10-24T01:00:00Z')['control']['recorded_head']==first['control_id']
    with pytest.raises(Failure,match='control_time_order'):
        commit(s,manifest(s,rid,b,action='restore',previous=first['control_id']))


@pytest.mark.parametrize('operation',['UPDATE','DELETE','REPLACE'])
def test_control_history_is_sql_immutable_even_with_recursive_triggers_off(env,operation):
    _,s,_,e=env
    _,b,rid=fixture(s,e)
    commit(s,manifest(s,rid,b))
    before=[tuple(r) for r in s.db.execute('SELECT * FROM sg_revision_review_controls')]
    assert s.db.execute('PRAGMA recursive_triggers').fetchone()[0]==0
    if operation=='REPLACE':
        columns=[r[1] for r in s.db.execute('PRAGMA table_info(sg_revision_review_controls)')]
        values=list(before[0]);values[columns.index('action')]='restore'
        with s.db:s.db.execute('INSERT OR REPLACE INTO sg_revision_review_controls VALUES('+','.join('?' for _ in values)+')',values)
    else:
        query="UPDATE sg_revision_review_controls SET action='restore'" if operation=='UPDATE' else 'DELETE FROM sg_revision_review_controls'
        with pytest.raises(sqlite3.IntegrityError,match='immutable'),s.db:s.db.execute(query)
    assert [tuple(r) for r in s.db.execute('SELECT * FROM sg_revision_review_controls')]==before


def test_decision_review_controls_require_explicit_direct_user_source(env):
    _,s,_,e=env
    a=publish(s,e[2],'old constraint',kind='Decision');b=publish(s,e[4],'new constraint',kind='Decision')
    rid=apply(s,review(s,b,a,'replaces',authority='direct_user'))['review_id']
    assistant=publish(s,e[1],'undo user choice',kind='Decision')
    with pytest.raises(Failure,match='user_authority_required'):commit(s,manifest(s,rid,assistant))
    with pytest.raises(Failure,match='authority_invalid'):commit(s,manifest(s,rid,assistant,authority='direct_user'))
    commit(s,manifest(s,rid,b,authority='direct_user'))
    assert item(s,a)['scopes']==[]


def test_assistant_cannot_retract_tool_dispute_or_direct_user_review(env):
    _,s,_,e=env
    a=publish(s,e[0],'pass');tool=publish(s,e[5],'failed');user=publish(s,e[2],'user observation')
    for origin,authority in [(tool,'source_report'),(user,'direct_user')]:
        rid=apply(s,review(s,origin,a,'disputes',authority=authority))['review_id']
        with pytest.raises(Failure,match='authority_insufficient|user_authority_required'):
            commit(s,manifest(s,rid,a))
    assert item(s,a)['scopes'][0]['state']=='disputed'


@pytest.mark.parametrize('status,context',[('proposed','work'),('completed','reference')])
def test_proposed_or_reference_sources_cannot_control_reviews(env,status,context):
    _,s,_,e=env
    _,b,rid=fixture(s,e)
    if context=='work':
        c=publish(s,e[3],'audit proposal',status=status)
        with pytest.raises(Failure,match='proposed_cannot_revoke'):commit(s,manifest(s,rid,c))
    else:
        # Evidence payload guards also apply to metadata; construct before capture.
        from test_revision_ledger import proposal
        candidate=proposal(e[3],'reference audit');candidate['context_kind']=context
        graph._commit(s,dict(id='ref-job',chat='chat-1'),dict(nodes=[candidate],relations=[],unresolved=[]),
            [],{'n1':'subject'},[],[],[],graph._stats(),(99,0),finalize=False)
        c=s.db.execute("SELECT id FROM sg_revision_items WHERE id NOT IN (SELECT source FROM sg_revision_reviews UNION SELECT target FROM sg_revision_reviews)").fetchone()[0]
        with pytest.raises(Failure,match='review_context_invalid'):commit(s,manifest(s,rid,c))


@pytest.mark.parametrize('change',[{'version':'bad'},{'project_id':'other'},{'action':'delete'},
    {'previous':None},{'effective_from':'2026-10-24'},{'replacement':{}},
    {'evidence':[]},{'authority':'admin'},{'action':'amend'}, {'extra':'unrecognized'}])
def test_strict_control_schema_rejects_ambiguous_or_cross_project_inputs(env,change):
    _,s,_,e=env
    _,b,rid=fixture(s,e)
    with pytest.raises(Failure):controls.control(s,{**manifest(s,rid,b),**change})
    assert s.db.execute('SELECT count(*) FROM sg_revision_review_controls').fetchone()[0]==0


def test_control_metadata_never_persists_credentials(env):
    _,s,_,e=env
    _,b,rid=fixture(s,e)
    with pytest.raises(Failure,match='contains_secret'):
        commit(s,manifest(s,rid,b,previous='Bearer synthetic-secret-value'))


def test_source_subject_evidence_and_unknown_review_are_not_guessable(env):
    _,s,_,e=env
    _,b,rid=fixture(s,e)
    other=publish(s,e[3],'unrelated',node='another')
    with pytest.raises(Failure,match='subject_mismatch'):controls.control(s,manifest(s,rid,other))
    with pytest.raises(Failure,match='review_evidence_invalid'):controls.control(s,manifest(s,rid,b,evidence='invented'))
    with pytest.raises(Failure,match='unknown_review'):controls.control(s,manifest(s,'invented',b))


@pytest.mark.parametrize('change',[{'target':'invented'}, {'source':'invented'}, {'authority':'direct_user'},
    {'effective_from':'2026-10-25T00:00:00Z'}])
def test_replacement_cannot_escape_atomic_scope_authority_or_time(env,change):
    _,s,_,e=env
    a,b,rid=fixture(s,e)
    replacement={**review(s,b,a,'disputes',effective_from='2026-10-24T01:00:00Z'),**change}
    with pytest.raises(Failure):commit(s,manifest(s,rid,b,action='amend',replacement=replacement))
    assert s.db.execute('SELECT count(*) FROM sg_revision_reviews').fetchone()[0]==1


def test_preview_readonly_budget_and_no_models_workers_or_schema_changes(env,monkeypatch):
    _,s,_,e=env
    _,b,rid=fixture(s,e)
    def forbidden(*args,**kwargs):raise AssertionError('unexpected schema/model/process')
    monkeypatch.setattr(ledger,'setup',forbidden);monkeypatch.setattr(subprocess,'Popen',forbidden)
    readonly=Store(s.config,readonly=True)
    try:
        assert controls.control(readonly,manifest(s,rid,b))['applied'] is False
        assert controls.read(readonly,rid)['model_calls']==0
        assert readonly.db.total_changes==0
    finally:readonly.close()
    with pytest.raises(Failure,match='transaction_required'):controls.control(s,manifest(s,rid,b),apply=True)
    assert ledger.READ.get() is None


def test_legacy_ledger_inspects_without_migrating_and_control_fails_explicitly(env):
    _,s,_,e=env
    _,b,rid=fixture(s,e)
    with s.db:s.db.execute('DROP TABLE sg_revision_review_controls')
    before=s.db.total_changes
    assert controls.read(s,rid)['control']['state']=='active'
    assert not controls.read(s,rid)['control']['available']
    assert ledger.inspect(s,'subject')['revisions']
    with pytest.raises(Failure,match='control_ledger_unavailable'):controls.control(s,manifest(s,rid,b))
    assert s.db.total_changes==before


def test_per_review_chain_limit_and_detail_pagination_are_explicit(env):
    _,s,_,e=env
    _,b,rid=fixture(s,e)
    previous=rid
    for i in range(64):
        previous=commit(s,manifest(s,rid,b,previous=previous,action='retract' if i%2==0 else 'restore'))['control_id']
    with pytest.raises(Failure,match='control_history_limit'):
        commit(s,manifest(s,rid,b,previous=previous))
    first=controls.read(s,rid,limit=13)
    ids={r['id'] for r in first['actions']}
    while first['has_more']:
        first=controls.read(s,rid,limit=13,after=first['next_after'])
        batch={r['id'] for r in first['actions']};assert not ids & batch;ids |= batch
    assert len(ids)==64 and first['control']['head']==previous


def test_cli_control_preview_apply_and_read_and_maintenance_busy(env,tmp_path,capsys):
    from sessiongraph.cli import main
    from sessiongraph.stage_leases import locked,root
    _,s,_,e=env
    _,b,rid=fixture(s,e)
    config_path=Path(s.config['_config_path'])
    cfg=json.loads(config_path.read_text());cfg['graph_v2']=s.config['graph_v2'];config_path.write_text(json.dumps(cfg))
    path=tmp_path/'control.json';path.write_text(json.dumps(manifest(s,rid,b)))
    for args in ([],['--apply']):
        assert main(['--config',str(config_path),'revision-control',str(path),*args])==0
        assert json.loads(capsys.readouterr().out)['applied']==bool(args)
    assert main(['--config',str(config_path),'revision-review-read',rid,'--limit','1'])==0
    assert json.loads(capsys.readouterr().out)['control']['state']=='retracted'
    with locked(root(s.config)/'stage-activity.lock',shared=True):
        assert main(['--config',str(config_path),'revision-control',str(path),'--apply'])==2
    assert 'maintenance_busy' in capsys.readouterr().err


def test_new_control_invalidates_first_opted_in_stage_and_disable_preserves_history(env):
    from sessiongraph import stage_leases
    from test_resource_policy import enable
    _,s,_,e=env
    enable(s.config);s.config['graph_v2']['stage_policy']={'enabled':True};graph.setup(s)
    _,b,rid=fixture(s,e)
    before=stage_leases.revision(s)
    commit(s,manifest(s,rid,b))
    assert int(stage_leases.revision(s))>int(before)
    s.config['graph_v2'].pop('revision_policy')
    assert controls.read(s,rid)['control']['state']=='retracted'
    with pytest.raises(Failure,match='policy_disabled'):controls.control(s,manifest(s,rid,b))


def test_amend_preview_has_no_writes_and_reuses_one_source_byte_budget(env,monkeypatch):
    _,s,_,e=env
    a,b,rid=fixture(s,e)
    replacement=review(s,b,a,'disputes',effective_from='2026-10-24T01:00:00Z')
    value=manifest(s,rid,b,action='amend',replacement=replacement)
    before=s.db.total_changes
    assert not controls.control(s,value)['applied']
    assert s.db.total_changes==before
    original=ledger.assess
    def no_remaining_bytes(*args,**kwargs):
        ledger.READ.get()['remaining']=0
        return original(*args,**kwargs)
    monkeypatch.setattr(ledger,'assess',no_remaining_bytes)
    with pytest.raises(Failure,match='read_byte_budget'):controls.control(s,value)
    assert ledger.READ.get() is None and s.db.total_changes==before


def test_amendment_is_only_visible_as_one_committed_snapshot_to_other_reader(env,monkeypatch):
    _,s,_,e=env
    a,b,rid=fixture(s,e)
    original=ledger.assess
    reader=Store(s.config,readonly=True)
    def observe_inside_transaction(*args,**kwargs):
        result=original(*args,**kwargs)
        if kwargs.get('apply'):
            during=item(reader,a)
            assert len(during['effects'])==1 and during['scopes'][0]['state']=='corrected'
            assert controls.read(reader,rid)['control']['head']==rid
        return result
    monkeypatch.setattr(ledger,'assess',observe_inside_transaction)
    try:
        replacement=review(s,b,a,'disputes',effective_from='2026-10-24T01:00:00Z')
        commit(s,manifest(s,rid,b,action='amend',replacement=replacement))
        assert item(reader,a)['scopes'][0]['state']=='disputed'
        assert len(item(reader,a)['effects'])==2
    finally:reader.close()


def test_concurrent_expected_head_is_serialized_by_real_sqlite_write_transactions(env):
    from concurrent.futures import ThreadPoolExecutor
    _,s,_,e=env
    _,b,rid=fixture(s,e)
    values=[manifest(s,rid,b,effective_from=f'2026-10-24T0{i}:00:00Z') for i in (1,2)]
    def writer(value):
        store=Store(s.config)
        try:
            with store.db:
                store.db.execute('BEGIN IMMEDIATE')
                return controls.control(store,value,apply=True)['applied']
        except Failure as error:return error.code
        finally:store.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(writer,values))
    assert sorted(results,key=str)==sorted([True,'revision_control_stale_head'],key=str)
    assert s.db.execute('SELECT count(*) FROM sg_revision_review_controls').fetchone()[0]==1


def test_equivalent_utc_time_replays_identical_control_instead_of_new_action(env):
    _,s,_,e=env
    _,b,rid=fixture(s,e)
    value=manifest(s,rid,b)
    first=commit(s,value)
    assert commit(s,{**value,'effective_from':'2026-10-24T09:00:00+08:00'})['control_id']==first['control_id']
    assert s.db.execute('SELECT count(*) FROM sg_revision_review_controls').fetchone()[0]==1


def test_retracting_dispute_does_not_remove_independent_support_or_declare_truth(env):
    _,s,_,e=env
    a=publish(s,e[0],'initial');b=publish(s,e[1],'counterclaim');c=publish(s,e[3],'independent support')
    rid=apply(s,review(s,b,a,'disputes'))['review_id']
    apply(s,review(s,c,a,'supports'))
    commit(s,manifest(s,rid,b))
    assert item(s,a)['scopes'][0]['state']=='supported'
    assert item(s,a)['validity']=='unassessed'


def test_restoring_original_after_amendment_never_silently_disables_replacement(env):
    _,s,_,e=env
    a,b,rid=fixture(s,e)
    replacement=review(s,b,a,'disputes',effective_from='2026-10-24T01:00:00Z')
    first=commit(s,manifest(s,rid,b,action='amend',replacement=replacement))
    commit(s,manifest(s,rid,b,action='restore',previous=first['control_id']))
    assert item(s,a)['scopes'][0]['state']=='disputed'
    assert {r['state'] for r in item(s,a)['effects']}=={'reviewed'}
    assert controls.read(s,first['replacement_review'])['control']['state']=='active'


def test_restoring_review_with_invalid_original_evidence_is_still_needs_review(env):
    _,s,_,e=env
    a,b,rid=fixture(s,e)
    c=publish(s,e[3],'audit')
    first=commit(s,manifest(s,rid,c))
    with s.db:s.db.execute("UPDATE entries SET text='changed' WHERE id=?",(e[1]['id'],))
    commit(s,manifest(s,rid,c,action='restore',previous=first['control_id']))
    assert item(s,a)['effects'][0]['state']=='needs_review'
    assert item(s,a)['scopes'][0]['state']=='needs_review'


def test_retracted_originals_are_not_reread_to_decide_effect_state(env,monkeypatch):
    _,s,_,e=env
    a,b,rid=fixture(s,e)
    c=publish(s,e[3],'audit')
    commit(s,manifest(s,rid,c))
    original=ledger.valid_sources
    def refuse_old_review(*args,**kwargs):
        if args[1]==b:raise AssertionError('retracted review must not reread its source')
        return original(*args,**kwargs)
    monkeypatch.setattr(ledger,'valid_sources',refuse_old_review)
    # Limit inspection to this revision so unrelated version-source reads remain out of scope.
    after=s.db.execute('SELECT id FROM sg_revision_items WHERE id<? ORDER BY id DESC LIMIT 1',(a,)).fetchone()
    result=ledger.inspect(s,'subject',limit=1,after=after[0] if after else '')['revisions'][0]
    assert result['id']==a and result['effects'][0]['state']=='retracted'


def test_control_read_budget_is_shared_by_entire_inspection_and_exhaustion_never_writes(env,monkeypatch):
    _,s,_,e=env
    a,b,rid=fixture(s,e)
    commit(s,manifest(s,rid,b))
    original=controls._chain
    def no_rows(*args,**kwargs):
        ledger.READ.get()['control_rows']=0
        return original(*args,**kwargs)
    monkeypatch.setattr(controls,'_chain',no_rows)
    before=s.db.total_changes
    with pytest.raises(Failure,match='control_read_limit'):ledger.inspect(s,'subject')
    assert s.db.total_changes==before and ledger.READ.get() is None


def test_broken_control_predecessor_is_not_ignored_or_treated_as_active(env):
    _,s,_,e=env
    _,b,rid=fixture(s,e)
    first=commit(s,manifest(s,rid,b))
    values=dict(s.db.execute('SELECT * FROM sg_revision_review_controls').fetchone())
    values.update(id='rc-broken-fixture',previous='missing',action='restore')
    with s.db:s.db.execute('INSERT INTO sg_revision_review_controls VALUES('+','.join('?' for _ in values)+')',tuple(values.values()))
    with pytest.raises(Failure,match='control_chain_invalid'):controls.read(s,rid)


def test_database_unique_constraint_prevents_same_predecessor_forks(env):
    _,s,_,e=env
    _,b,rid=fixture(s,e)
    commit(s,manifest(s,rid,b))
    values=dict(s.db.execute('SELECT * FROM sg_revision_review_controls').fetchone())
    values.update(id='rc-duplicate-fixture',action='restore')
    before=[tuple(r) for r in s.db.execute('SELECT * FROM sg_revision_review_controls')]
    # The alternate unique key must also be protected against REPLACE deleting
    # history when recursive_triggers is off. Duplicate forks are no-ops in SQL.
    with s.db:
        s.db.execute('INSERT INTO sg_revision_review_controls VALUES('+','.join('?' for _ in values)+')',tuple(values.values()))
        s.db.execute('INSERT OR REPLACE INTO sg_revision_review_controls VALUES('+','.join('?' for _ in values)+')',tuple(values.values()))
    assert [tuple(r) for r in s.db.execute('SELECT * FROM sg_revision_review_controls')]==before


def test_controls_reference_existing_retained_evidence_without_new_graph_nodes_or_body_copies(env):
    from sessiongraph.source_retention import read as retained
    _,s,path,e=env
    _,b,rid=fixture(s,e)
    before=s.db.execute('SELECT count(*) FROM sg2_nodes').fetchone()[0]
    commit(s,manifest(s,rid,b))
    columns={r[1] for r in s.db.execute('PRAGMA table_info(sg_revision_review_controls)')}
    assert not columns & {'text','quote','content','conditions_json','replacement_manifest'}
    assert s.db.execute('SELECT count(*) FROM sg2_nodes').fetchone()[0]==before
    path.unlink()
    assert retained(s,e[1]['id'])['status']=='retained_verified'
    assert controls.read(s,rid)['actions'][0]['source_state']=='valid'


@pytest.mark.parametrize('limit',[0,101,True])
def test_control_detail_limits_are_strict(env,limit):
    _,s,_,e=env
    _,_,rid=fixture(s,e)
    with pytest.raises(Failure,match='read_limit'):controls.read(s,rid,limit=limit)


def test_cli_control_manifest_size_is_bounded_before_writable_store_open(env,tmp_path,capsys):
    from sessiongraph.cli import main
    _,s,_,_=env
    path=tmp_path/'oversize.json';path.write_text(' '*16385)
    assert main(['--config',s.config['_config_path'],'revision-control',str(path),'--apply'])==2
    assert 'revision_manifest_too_large' in capsys.readouterr().err


@pytest.mark.parametrize('entry,authority',[(2,'direct_user'),(2,'source_report'),(5,'source_report')])
def test_assistant_cannot_override_user_or_tool_control_of_its_own_review(env,entry,authority):
    _,s,_,e=env
    a,b,rid=fixture(s,e)
    c=publish(s,e[entry],'audit: retract this review')
    first=commit(s,manifest(s,rid,c,authority=authority))
    with pytest.raises(Failure,match='user_authority_required|authority_insufficient'):
        commit(s,manifest(s,rid,b,action='restore',previous=first['control_id']))
    assert item(s,a)['scopes']==[]


def test_direct_user_can_explicitly_restore_after_user_retraction(env):
    _,s,_,e=env
    a,b,rid=fixture(s,e)
    c=publish(s,e[2],'audit retraction');d=publish(s,e[4],'audit restoration')
    first=commit(s,manifest(s,rid,c,authority='direct_user'))
    commit(s,manifest(s,rid,d,authority='direct_user',action='restore',previous=first['control_id']))
    assert item(s,a)['scopes'][0]['state']=='corrected'


def test_incomplete_effects_mark_validity_needs_review_even_if_visible_effects_retracted(env):
    _,s,_,e=env
    a=publish(s,e[0],'initial');b=publish(s,e[1],'correction')
    for i in range(33):
        rid=apply(s,review(s,b,a,facet=f'lane {i}'))['review_id']
        commit(s,manifest(s,rid,b))
    result=item(s,a)
    assert not result['effects_complete'] and result['scopes']==[]
    assert result['validity']=='needs_review'


def test_write_side_schema_upgrade_is_additive_and_keeps_previous_versions_and_reviews(env):
    _,s,_,e=env
    _,_,rid=fixture(s,e)
    before={t:[tuple(r) for r in s.db.execute(f'SELECT * FROM {t}')] for t in
        ('sg_revision_items','sg_revision_sources','sg_revision_reviews','sg2_nodes','work_memory')}
    with s.db:s.db.execute('DROP TABLE sg_revision_review_controls')
    graph.setup(s)
    assert controls.read(s,rid)['control']['available']
    assert before=={t:[tuple(r) for r in s.db.execute(f'SELECT * FROM {t}')] for t in before}


def test_ignored_control_insert_cannot_leave_an_orphaned_replacement_review(env):
    _,s,_,e=env
    a,b,rid=fixture(s,e)
    with s.db:
        s.db.execute("CREATE TRIGGER ignore_control BEFORE INSERT ON sg_revision_review_controls BEGIN SELECT RAISE(IGNORE); END")
    replacement=review(s,b,a,'disputes',effective_from='2026-10-24T01:00:00Z')
    with pytest.raises(Failure,match='control_stale_head'):
        commit(s,manifest(s,rid,b,action='amend',replacement=replacement))
    assert s.db.execute('SELECT count(*) FROM sg_revision_reviews').fetchone()[0]==1
    assert s.db.execute('SELECT count(*) FROM sg_revision_review_controls').fetchone()[0]==0


def test_nested_control_then_read_observes_new_head_without_stale_cache(env):
    _,s,_,e=env
    _,b,rid=fixture(s,e)
    @ledger.bounded_read
    def command(store):
        controls.control(store,manifest(store,rid,b),apply=True)
        return controls.read(store,rid)
    with s.db:
        s.db.execute('BEGIN IMMEDIATE')
        assert command(s)['control']['state']=='retracted'


def test_rehearsal_replay_controls_preserve_small_graph_and_evidence():
    from rehearse_review_control import rehearse
    result=rehearse()
    assert result['counts']['sg2_nodes']==1 and result['counts']['sg_revision_review_controls']==3
    assert result['old_replay_did_not_reactivate'] and result['stale_head_rejected']
    assert result['model_calls']==result['real_network_calls']==result['production_accesses']==0
    assert result['all_four_retained_records_verified'] and result['quick_check']=='ok'


def test_review_detail_before_original_effective_time_never_claims_active(env):
    _,s,_,e=env
    _,_,rid=fixture(s,e)
    assert controls.read(s,rid,at='2026-10-23T23:59:59Z')['control']['state']=='not_effective'
