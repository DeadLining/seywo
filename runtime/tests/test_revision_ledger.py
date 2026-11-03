"""Real publication/SQLite/CLI, explicit fixture reviews; no semantic model proof."""
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from sessiongraph import graph_v2 as graph, revision_ledger as ledger
from sessiongraph.core import Store
from sessiongraph.failures import Failure
from test_memories import make_store, entries, FakeExtractor
from test_graph_v2 import Judge


def proposal(entry, content, *, kind='Claim', status='completed'):
    return dict(id='n1', type=kind, title='metric', content=content, status=status,
                aliases=[], identity=[], context_kind='work',
                evidence=[dict(entry_id=entry['id'], quote=entry['text'])])


def publish(store, entry, content, *, node='subject', kind='Claim', status='completed'):
    item = proposal(entry, content, kind=kind, status=status)
    jid = 'job-' + entry['id'] + '-' + node
    with store.db:
        store.db.execute('INSERT OR IGNORE INTO sg2_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (jid, store.config['project_id'], 'chat-1', 'chat-1', 'judging', '{}', None, '[]', '{}', None, '', ''))
    graph._commit(store, dict(id=jid, chat='chat-1'), dict(nodes=[item], relations=[], unresolved=[]),
                  [], {'n1':node}, [], [], [], graph._stats(), (99,0), finalize=False)
    if ledger.enabled(store.config):
        row = store.db.execute('SELECT r.* FROM sg_revision_items r JOIN sg2_evidence e ON e.id=r.payload_evidence WHERE r.node=? AND json_extract(e.assertion_json,\'$.content\')=? AND json_extract(e.assertion_json,\'$.status\')=?',
                               (node,content,status)).fetchone()
        return row['id']


@pytest.fixture
def env(tmp_path):
    texts = [('assistant','metric old report: held-out pass'),
             ('assistant','metric correction: held-out not measured'),
             ('user','metric directive: use latest approved constraint'),
             ('assistant','metric old report: held-out pass'),
             ('user','metric withdrawal of this statement')]
    root, store, path = make_store(tmp_path, texts)
    with path.open('a') as stream:
        stream.write(json.dumps(dict(timestamp='2026-09-30T00:00:00Z',type='response_item',
            payload=dict(type='function_call_output',call_id='test-call',output='metric observed failure: test actually failed'))) + '\n')
    store.ingest(path)
    store.config['graph_v2'] = {'enabled':True, 'revision_policy':{'enabled':True}}
    graph.setup(store)
    yield root, store, path, entries(store)
    store.close()


def review(store, source, target, effect='corrects', **overrides):
    evidence = store.db.execute('SELECT evidence FROM sg_revision_sources WHERE revision=? ORDER BY evidence LIMIT 1', (source,)).fetchone()[0]
    base = dict(version=ledger.VERSION, project_id=store.config['project_id'], source=source, target=target,
        effect=effect, facet='held-out evaluation', conditions={'split':'unseen'}, evidence=evidence,
        authority='source_report', effective_from='2026-10-24T00:00:00Z')
    return {**base, **overrides}


def apply(store, manifest):
    from sessiongraph.maintenance import project_lock
    with project_lock(store.config), store.db:
        store.db.execute('BEGIN IMMEDIATE')
        return ledger.assess(store, manifest, apply=True)


def item(store, revision, **kwargs):
    node = ledger._item(store,revision)['node']
    return next(r for r in ledger.inspect(store,node,**kwargs)['revisions'] if r['id']==revision)


def test_graph_publication_retains_versions_without_new_graph_nodes_or_payload_copy(env):
    _, s, _, e = env
    a = publish(s,e[0],'held-out pass')
    b = publish(s,e[1],'held-out not measured')
    assert a != b
    assert s.db.execute('SELECT count(*) FROM sg2_nodes').fetchone()[0] == 1
    assert s.db.execute('SELECT count(*) FROM sg_revision_items').fetchone()[0] == 2
    assert s.db.execute('SELECT count(*) FROM sg_retained_sources').fetchone()[0] == 2
    columns = {r[1] for r in s.db.execute('PRAGMA table_info(sg_revision_items)')}
    assert not columns & {'content','quote','payload','text'}
    assert item(s,a)['content']=='held-out pass'
    assert item(s,b)['content']=='held-out not measured'
    assert item(s,a)['validity']==item(s,b)['validity']=='unassessed'


def test_repetition_adds_unique_evidence_not_another_statement_and_is_idempotent(env):
    _, s, _, e = env
    a=publish(s,e[0],'held-out pass')
    assert publish(s,e[3],'held-out pass')==a
    assert publish(s,e[3],'held-out pass')==a
    assert s.db.execute('SELECT count(*) FROM sg_revision_items').fetchone()[0]==1
    assert s.db.execute('SELECT count(*) FROM sg_revision_sources').fetchone()[0]==2
    assert s.db.execute('SELECT count(*) FROM sg2_nodes').fetchone()[0]==1


def test_scoped_correction_retains_history_and_later_summary_cannot_revive_it(env):
    _, s, _, e = env
    a=publish(s,e[0],'held-out pass');b=publish(s,e[1],'held-out not measured')
    result=apply(s,review(s,b,a))
    assert result['applied'] and result['model_calls']==0
    assert item(s,a)['scopes']==[dict(facet='held-out evaluation',conditions={'split':'unseen'},state='corrected')]
    assert item(s,a)['execution_status']=='completed'
    publish(s,e[3],'held-out pass')
    assert item(s,a)['scopes'][0]['state']=='corrected'
    assert item(s,b)['validity']=='unassessed'  # Newer is not automatically true.
    assert s.db.execute('SELECT count(*) FROM sg_revision_items').fetchone()[0]==2


@pytest.mark.parametrize('effect,state',[('supports','supported'),('disputes','disputed'),('corrects','corrected'),('replaces','superseded'),('withdraws','withdrawn')])
def test_effects_are_deterministic_and_not_execution_status(env,effect,state):
    _,s,_,e=env
    a=publish(s,e[0],'initial report');b=publish(s,e[1],'later report')
    apply(s,review(s,b,a,effect))
    result=item(s,a)
    assert result['scopes'][0]['state']==state
    assert result['execution_status']=='completed'


def test_support_after_withdrawal_does_not_resurrect_old_assertion(env):
    _,s,_,e=env
    a=publish(s,e[0],'initial report');b=publish(s,e[1],'later report');c=publish(s,e[3],'independent repeat')
    apply(s,review(s,b,a,'withdraws'))
    apply(s,review(s,c,a,'supports'))
    assert item(s,a)['scopes'][0]['state']=='withdrawn'


def test_different_conditions_remain_separate_and_no_global_revocation(env):
    _,s,_,e=env
    a=publish(s,e[0],'offline pass, closed loop unknown');b=publish(s,e[1],'closed loop failed')
    apply(s,review(s,b,a,conditions={'mode':'closed_loop'}))
    apply(s,review(s,b,a,'supports',conditions={'mode':'offline'}))
    result=item(s,a)
    assert {r['conditions']['mode']:r['state'] for r in result['scopes']}=={'closed_loop':'corrected','offline':'supported'}
    assert result['validity']=='unassessed'


def test_competing_replacements_are_disputed_not_newest_wins(env):
    _,s,_,e=env
    a=publish(s,e[0],'initial');b=publish(s,e[1],'replacement one');c=publish(s,e[3],'replacement two')
    apply(s,review(s,b,a));apply(s,review(s,c,a))
    assert item(s,a)['scopes'][0]['state']=='disputed'


def test_correction_of_correction_is_append_only_chain(env):
    _,s,_,e=env
    a=publish(s,e[0],'initial');b=publish(s,e[1],'first correction');c=publish(s,e[3],'second correction')
    apply(s,review(s,b,a));apply(s,review(s,c,b))
    assert item(s,a)['scopes'][0]['state']==item(s,b)['scopes'][0]['state']=='corrected'
    assert item(s,c)['scopes']==[]
    assert s.db.execute('SELECT count(*) FROM sg_revision_reviews').fetchone()[0]==2


def test_cycle_and_self_replacement_rejected_even_with_different_conditions(env):
    _,s,_,e=env
    a=publish(s,e[0],'initial');b=publish(s,e[1],'first correction');c=publish(s,e[3],'second correction')
    apply(s,review(s,b,a));apply(s,review(s,c,b))
    with pytest.raises(Failure,match='replacement_cycle'):apply(s,review(s,a,c,conditions={'scope':'other'}))
    with pytest.raises(Failure,match='self_edge'):apply(s,review(s,a,a))


def test_assistant_cannot_replace_user_decision_or_launder_role_as_direct_user(env):
    _,s,_,e=env
    a=publish(s,e[2],'use official baseline',kind='Decision')
    b=publish(s,e[1],'switch to my suggestion',kind='Decision')
    with pytest.raises(Failure,match='user_authority_required'):apply(s,review(s,b,a,'replaces'))
    with pytest.raises(Failure,match='authority_invalid'):apply(s,{**review(s,b,a,'replaces'),'authority':'direct_user'})
    assert not item(s,a)['scopes']


def test_explicit_review_of_user_directive_is_scoped_not_automatic_approval(env):
    _,s,_,e=env
    a=publish(s,e[2],'old decision',kind='Decision');b=publish(s,e[4],'new decision',kind='Decision')
    result=apply(s,{**review(s,b,a,'replaces'),'authority':'direct_user'})
    assert result['review_origin']=='explicit_local_review'
    assert 'not proven' in result['note']
    assert item(s,a)['scopes'][0]['state']=='superseded'


def test_assistant_report_cannot_erase_tool_observation(env):
    _,s,_,e=env
    a=publish(s,e[5],'test failed');b=publish(s,e[1],'test passed')
    with pytest.raises(Failure,match='authority_insufficient'):apply(s,review(s,b,a))
    apply(s,review(s,b,a,'disputes'))
    assert item(s,a)['scopes'][0]['state']=='disputed'


@pytest.mark.parametrize('column,value',[('text','source changed'),('role','user'),('timestamp','new time'),('call_id','new call')])
def test_changed_source_or_attribution_requires_review_and_never_reactivates_old_version(env,column,value):
    _,s,_,e=env
    a=publish(s,e[0],'initial');b=publish(s,e[1],'correction')
    apply(s,review(s,b,a))
    with s.db:s.db.execute(f'UPDATE entries SET {column}=? WHERE id=?',(value,e[1]['id']))
    assert item(s,a)['scopes'][0]['state']=='needs_review'
    assert item(s,b)['source_state']=='needs_review'
    with pytest.raises(Failure,match='review_evidence_invalid'):apply(s,review(s,b,a))


def test_independent_source_retention_survives_external_transcript_deletion(env):
    _,s,path,e=env
    a=publish(s,e[0],'initial');path.unlink()
    from sessiongraph.source_retention import read
    assert read(s,e[0]['id'])['status']=='retained_verified'
    assert item(s,a)['source_state']=='valid'


@pytest.mark.parametrize('table',['sg_revision_items','sg_revision_sources','sg_revision_reviews','sg2_evidence'])
@pytest.mark.parametrize('operation',['UPDATE','DELETE'])
def test_revision_history_and_referenced_payloads_are_sql_immutable(env,table,operation):
    _,s,_,e=env
    a=publish(s,e[0],'initial');b=publish(s,e[1],'correction');apply(s,review(s,b,a))
    column={'sg_revision_items':'created','sg_revision_sources':'text_hash','sg_revision_reviews':'created','sg2_evidence':'quote'}[table]
    sql=f'UPDATE {table} SET {column}=\'changed\'' if operation=='UPDATE' else f'DELETE FROM {table}'
    with pytest.raises(sqlite3.IntegrityError,match='immutable'),s.db:s.db.execute(sql)


def test_future_effective_time_is_not_ingestion_time(env):
    _,s,_,e=env
    a=publish(s,e[0],'initial');b=publish(s,e[1],'correction')
    apply(s,{**review(s,b,a),'effective_from':'2026-10-25T08:00:00+08:00'})
    assert item(s,a,at='2026-10-24T23:59:59Z')['scopes']==[]
    assert item(s,a,at='2026-10-25T00:00:00Z')['scopes'][0]['state']=='corrected'


def test_review_idempotence_preview_and_project_subject_guards(env):
    _,s,_,e=env
    a=publish(s,e[0],'initial');b=publish(s,e[1],'correction');other=publish(s,e[3],'foreign subject',node='another')
    manifest=review(s,b,a)
    before=s.db.total_changes
    assert ledger.assess(s,manifest)['applied'] is False
    assert s.db.total_changes==before
    assert apply(s,manifest)['review_id']==apply(s,manifest)['review_id']
    assert s.db.execute('SELECT count(*) FROM sg_revision_reviews').fetchone()[0]==1
    with pytest.raises(Failure,match='subject_mismatch'):ledger.assess(s,{**manifest,'target':other})
    with pytest.raises(Failure,match='project_or_version'):ledger.assess(s,{**manifest,'project_id':'another'})


@pytest.mark.parametrize('change',[{'effect':[]},{'authority':'assistant'},{'facet':''},{'conditions':{'mode':float('nan')}},{'conditions':[]},{'effective_from':'2026-10-24'}])
def test_review_schema_rejects_ambiguous_values(env,change):
    _,s,_,e=env
    a=publish(s,e[0],'initial');b=publish(s,e[1],'correction')
    with pytest.raises(Failure):ledger.assess(s,{**review(s,b,a),**change})


def test_old_policy_default_remains_schema_free_and_payload_editable(tmp_path):
    _,s,_=make_store(tmp_path,[('assistant','metric first report')])
    graph.setup(s);publish(s,entries(s)[0],'first')
    assert not s.db.execute("SELECT 1 FROM sqlite_master WHERE name='sg_revision_items'").fetchone()
    assert ledger.inspect(s,'subject')['available'] is False
    with s.db:s.db.execute("UPDATE sg2_evidence SET quote='metric'")
    s.close()


def test_enablement_adopts_exact_previous_payload_without_historical_model_replay(tmp_path):
    _,s,_=make_store(tmp_path,[('assistant','metric first report'),('assistant','metric correction')])
    graph.setup(s);e=entries(s);publish(s,e[0],'first')
    s.config['graph_v2']={'enabled':True,'revision_policy':{'enabled':True}}
    graph.setup(s);publish(s,e[1],'second')
    assert {r['content'] for r in ledger.inspect(s,'subject')['revisions']}=={'first','second'}
    s.close()


def test_missing_baseline_provenance_fails_publication_without_fabricating_history(tmp_path):
    _,s,_=make_store(tmp_path,[('assistant','metric first report'),('assistant','metric correction')])
    graph.setup(s);e=entries(s);publish(s,e[0],'first')
    with s.db:s.db.execute('DELETE FROM sg2_evidence')
    s.config['graph_v2']={'enabled':True,'revision_policy':{'enabled':True}}
    graph.setup(s)
    with pytest.raises(Failure,match='baseline_unavailable'):publish(s,e[1],'second')
    assert s.db.execute('SELECT content FROM work_memory').fetchone()[0]=='first'
    assert s.db.execute('SELECT count(*) FROM sg_revision_items').fetchone()[0]==0
    s.close()


def test_reanchored_primary_reuses_exact_valid_ledger_baseline(env):
    _, s, _, e = env
    first = publish(s, e[0], 'first')
    with s.db:
        s.db.execute('UPDATE work_memory SET source_entry_id=?,source_quote=? WHERE id=?',
            (e[1]['id'], e[1]['text'], 'subject'))
    second = publish(s, e[1], 'second')
    assert first != second
    assert s.db.execute('SELECT count(*) FROM sg_revision_items').fetchone()[0] == 2
    assert item(s, first)['content'] == 'first'


@pytest.mark.parametrize('defect', ['changed_payload', 'changed_attribution', 'missing_sources'])
def test_reanchored_baseline_still_requires_exact_payload_and_valid_source(env, defect):
    _, s, _, e = env
    publish(s, e[0], 'first')
    with s.db:
        s.db.execute('UPDATE work_memory SET source_entry_id=?,source_quote=? WHERE id=?',
            (e[1]['id'], e[1]['text'], 'subject'))
        if defect == 'changed_payload':
            s.db.execute("UPDATE work_memory SET content='unsupported edit'")
        elif defect == 'changed_attribution':
            s.db.execute("UPDATE entries SET role='user' WHERE id=?", (e[0]['id'],))
        else:
            s.db.execute('DROP TRIGGER sg_revision_sources_delete')
            s.db.execute('DELETE FROM sg_revision_sources')
    with pytest.raises(Failure, match='baseline_unavailable'):
        publish(s, e[1], 'second')
    assert s.db.execute('SELECT count(*) FROM sg_revision_items').fetchone()[0] == 1


def test_read_and_review_preview_are_read_only_and_do_not_launch_or_call_models(env,monkeypatch):
    _,s,_,e=env
    a=publish(s,e[0],'initial');b=publish(s,e[1],'correction')
    config=s.config
    def forbidden(*args,**kwargs):raise AssertionError('unexpected write/model/worker')
    monkeypatch.setattr(ledger,'setup',forbidden);monkeypatch.setattr(subprocess,'Popen',forbidden)
    readonly=Store(config,readonly=True)
    try:
        assert ledger.inspect(readonly,'subject')['model_calls']==0
        assert ledger.assess(readonly,review(s,b,a))['model_calls']==0
        assert readonly.db.total_changes==0
    finally:readonly.close()


def test_pagination_has_no_newest_wins_or_silent_overflow(env):
    _,s,_,e=env
    for i in range(3):publish(s,e[i],f'statement {i}')
    first=ledger.inspect(s,'subject',limit=2)
    assert first['has_more'] and first['next_after']
    second=ledger.inspect(s,'subject',limit=2,after=first['next_after'])
    assert len(second['revisions'])==1 and not second['has_more']
    assert not {r['id'] for r in first['revisions']} & {r['id'] for r in second['revisions']}


def test_cli_preview_and_apply_are_explicit_and_never_update_normal_memory(env,capsys,tmp_path):
    from sessiongraph.cli import main
    _,s,_,e=env
    a=publish(s,e[0],'initial');b=publish(s,e[1],'correction')
    config_path=Path(s.config['_config_path'])
    config=json.loads(config_path.read_text());config['graph_v2']=s.config['graph_v2'];config_path.write_text(json.dumps(config))
    manifest=tmp_path/'review.json';manifest.write_text(json.dumps(review(s,b,a)))
    before=[tuple(row) for row in s.db.execute('SELECT * FROM work_memory')]
    for extra in ([],['--apply']):
        assert main(['--config',str(config_path),'revision-assess',str(manifest),*extra])==0
        result=json.loads(capsys.readouterr().out)
        assert result['applied']==bool(extra) and result['model_calls']==0
    assert main(['--config',str(config_path),'revision-read','subject'])==0
    assert json.loads(capsys.readouterr().out)['shadow'] is True
    assert [tuple(row) for row in s.db.execute('SELECT * FROM work_memory')]==before


def test_real_graph_run_records_claim_snapshots_and_replay_costs_no_calls(tmp_path):
    _,s,_=make_store(tmp_path,[('assistant','metric first report')])
    s.config['graph_v2']={'enabled':True,'revision_policy':{'enabled':True}}
    n=proposal(entries(s)[0],'first report')
    out=dict(schema_version=graph.VERSION,nodes=[n],relations=[],unresolved=[],coverage=dict(entry_ids=[entries(s)[0]['id']],outcome='candidates_found'))
    result=graph.run_chat(s,FakeExtractor([out]),'chat-1',jev=Judge())
    assert not result['failed'] and len(result['saved'])==1
    assert len(ledger.inspect(s,result['saved'][0])['revisions'])==1
    replay=graph.run_chat(s,FakeExtractor([]),'chat-1',jev=Judge())
    assert replay['model_calls']==0
    s.close()


@pytest.mark.parametrize('table',['sg_revision_items','sg_revision_sources','sg_revision_reviews','sg2_evidence'])
def test_sql_replace_cannot_bypass_immutable_history_with_default_sqlite_pragmas(env,table):
    _,s,_,e=env
    a=publish(s,e[0],'initial');b=publish(s,e[1],'correction');apply(s,review(s,b,a))
    assert s.db.execute('PRAGMA recursive_triggers').fetchone()[0]==0
    before=[tuple(r) for r in s.db.execute(f'SELECT * FROM {table}')]
    columns=[r[1] for r in s.db.execute(f'PRAGMA table_info({table})')]
    values=list(before[0])
    field={'sg_revision_items':'created','sg_revision_sources':'text_hash','sg_revision_reviews':'created','sg2_evidence':'quote'}[table]
    values[columns.index(field)]='changed'
    with s.db:s.db.execute(f'INSERT OR REPLACE INTO {table} VALUES('+','.join('?' for _ in values)+')',values)
    assert [tuple(r) for r in s.db.execute(f'SELECT * FROM {table}')]==before


def test_review_apply_requires_caller_owned_transaction_and_rolls_back_atomically(env):
    _,s,_,e=env
    a=publish(s,e[0],'initial');b=publish(s,e[1],'correction')
    manifest=review(s,b,a)
    with pytest.raises(Failure,match='transaction_required'):ledger.assess(s,manifest,apply=True)
    with pytest.raises(RuntimeError),s.db:
        s.db.execute('BEGIN IMMEDIATE');ledger.assess(s,manifest,apply=True)
        raise RuntimeError('abort publication')
    assert s.db.execute('SELECT count(*) FROM sg_revision_reviews').fetchone()[0]==0
    assert ledger.READ.get() is None and not s.db.in_transaction


def test_snapshot_failure_rolls_back_graph_publication_and_does_not_forge_missing_source(env,monkeypatch):
    _,s,_,e=env
    from sessiongraph import source_retention
    def broken(*args,**kwargs):raise Failure('source_retention_source_missing')
    monkeypatch.setattr(source_retention,'capture',broken)
    with pytest.raises(Failure,match='source_missing'):publish(s,e[0],'initial')
    for table in ('work_memory','sg2_nodes','sg2_evidence','sg_revision_items','sg_revision_sources'):
        assert s.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]==0


def test_cli_review_apply_cannot_cross_active_worker_maintenance_lease(env,capsys,tmp_path):
    from sessiongraph.cli import main
    from sessiongraph.stage_leases import locked,root
    _,s,_,e=env
    a=publish(s,e[0],'initial');b=publish(s,e[1],'correction')
    config_path=Path(s.config['_config_path'])
    cfg=json.loads(config_path.read_text());cfg['graph_v2']=s.config['graph_v2'];config_path.write_text(json.dumps(cfg))
    path=tmp_path/'review.json';path.write_text(json.dumps(review(s,b,a)))
    with locked(root(s.config)/'stage-activity.lock',shared=True):
        assert main(['--config',str(config_path),'revision-assess',str(path),'--apply'])==2
    assert 'maintenance_busy' in capsys.readouterr().err
    assert s.db.execute('SELECT count(*) FROM sg_revision_reviews').fetchone()[0]==0


def test_review_mutation_is_a_dependency_on_first_opted_in_stage_setup(env):
    from sessiongraph import stage_leases
    from test_resource_policy import enable
    _,s,_,e=env
    enable(s.config);s.config['graph_v2']['stage_policy']={'enabled':True}
    graph.setup(s)
    a=publish(s,e[0],'initial');b=publish(s,e[1],'correction')
    before=stage_leases.revision(s)
    apply(s,review(s,b,a))
    assert int(stage_leases.revision(s))>int(before)


def test_partial_review_history_is_never_reported_as_a_complete_effect(env):
    _,s,_,e=env
    a=publish(s,e[0],'initial');b=publish(s,e[1],'correction')
    for i in range(33):apply(s,review(s,b,a,facet=f'review lane {i}'))
    result=item(s,a)
    assert result['effects_complete'] is False and len(result['effects'])==32
    assert all(lane['state']=='needs_review' for lane in result['scopes'])


@pytest.mark.parametrize('policy',[True,{'enabled':'yes'},{'enabled':True,'force':True}])
def test_revision_policy_is_strict_and_explicit(env,policy):
    _,s,_,_=env
    s.config['graph_v2']['revision_policy']=policy
    with pytest.raises(Failure,match='policy_invalid'):ledger.enabled(s.config)


def test_disabling_policy_preserves_history_readability_and_immutability(env):
    _,s,_,e=env
    a=publish(s,e[0],'initial');b=publish(s,e[1],'correction');apply(s,review(s,b,a))
    s.config['graph_v2'].pop('revision_policy')
    assert item(s,a)['scopes'][0]['state']=='corrected'
    with pytest.raises(Failure,match='policy_disabled'):ledger.assess(s,review(s,b,a))
    with pytest.raises(sqlite3.IntegrityError,match='immutable'),s.db:s.db.execute('DELETE FROM sg_revision_items')


def test_proposed_decision_cannot_revoke_existing_decision_even_when_role_is_user(env):
    _,s,_,e=env
    a=publish(s,e[2],'existing constraint',kind='Decision')
    b=publish(s,e[4],'suggested replacement',kind='Decision',status='proposed')
    with pytest.raises(Failure,match='proposed_cannot_revoke'):
        apply(s,{**review(s,b,a,'replaces'),'authority':'direct_user'})


def test_original_attribution_remains_visible_when_current_metadata_is_invalid(env):
    _,s,_,e=env
    a=publish(s,e[0],'initial')
    with s.db:s.db.execute("UPDATE entries SET role='user',timestamp='changed' WHERE id=?",(e[0]['id'],))
    result=item(s,a)
    assert result['source_state']=='needs_review'
    assert result['source_refs'][0]['role']=='assistant'
    assert result['source_refs'][0]['entry_id']==e[0]['id']
    assert result['source_refs'][0]['source_time']=='2026-09-30T00:00:00Z'


def test_inspection_budget_exhaustion_is_explicit_and_never_writes(env,monkeypatch):
    _,s,_,e=env
    publish(s,e[0],'initial')
    original=ledger.source
    def limited(*args,**kwargs):
        ledger.READ.get()['remaining']=0
        return original(*args,**kwargs)
    monkeypatch.setattr(ledger,'source',limited)
    before=s.db.total_changes
    with pytest.raises(Failure,match='read_byte_budget'):ledger.inspect(s,'subject')
    assert s.db.total_changes==before and ledger.READ.get() is None


def test_rehearsal_preserves_history_without_extra_semantic_nodes():
    from rehearse_revision_ledger import rehearse
    result=rehearse()
    assert result['counts']['sg2_nodes']==1
    assert result['counts']['sg_revision_items']==2 and result['counts']['sg_revision_reviews']==1
    assert result['old_scope_state']=='corrected' and result['new_revision_validity']=='unassessed'
    assert result['model_calls']==result['real_network_calls']==result['production_accesses']==0
    assert result['quick_check']=='ok'


def test_conditions_in_title_cannot_be_collapsed_when_content_and_source_are_identical(env):
    _,s,_,e=env
    publish(s,e[0],'reward 1.0')
    changed=proposal(e[0],'reward 1.0');changed['title']='closed loop metric'
    graph._commit(s,dict(id='job-'+e[0]['id']+'-subject',chat='chat-1'),dict(nodes=[changed],relations=[],unresolved=[]),
        [],{'n1':'subject'},[],[],[],graph._stats(),(99,0),finalize=False)
    result=ledger.inspect(s,'subject')
    assert len(result['revisions'])==2
    assert {r['title'] for r in result['revisions']}=={'metric','closed loop metric'}


def test_review_metadata_rejects_credentials_instead_of_persisting_them(env):
    _,s,_,e=env
    a=publish(s,e[0],'initial');b=publish(s,e[1],'correction')
    with pytest.raises(Failure,match='contains_secret'):
        apply(s,review(s,b,a,conditions={'auth':'Bearer synthetic-secret-value'}))
    assert s.db.execute('SELECT count(*) FROM sg_revision_reviews').fetchone()[0]==0
