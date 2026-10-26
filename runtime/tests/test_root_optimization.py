import json
from sessiongraph.auto_update import run_update
from sessiongraph.core import Store, init_project, load_config
from sessiongraph.graph_v2 import run_chat, read_relations, candidates
from test_memories import make_store, entries, FakeExtractor
from test_graph_v2 import output, Judge
from test_memory import seed_store


def test_recovery_rechecks_backlog_and_accumulates_round_stats(tmp_path, monkeypatch):
    monkeypatch.setattr('sessiongraph.auto_update.time.sleep', lambda _: None)
    config = init_project(tmp_path)
    Store(load_config(config)).close()
    calls = []
    def execute(argv, timeout):
        if 'refresh-chat' in argv:
            return {'added_entries': 3, 'skipped': []}
        calls.append(argv)
        fail = len(calls) == 1
        return {'saved': [] if fail else ['n'+str(len(calls))],
                'failed': [{'error_code':'jev_tls_handshake','retryable':True}] if fail else [],
                'pending_retry_count':int(fail),'model_calls':1,
                'graph_v2':{'deepseek_calls':int(fail),'jev_calls':int(not fail),
                            'budget_exhausted':False,'nodes_saved':int(not fail),
                            'usage':[{'provider':'fixture','usage':{'input_tokens':10}}],
                            'reasons':{'identity_pending':1}}}
    r = run_update(config, 'chat', execute=execute)
    assert len(calls) == 3  # first fail, durable retry, full backlog check
    assert '--memories-pending-only' in calls[1]
    assert '--memories-pending-only' not in calls[2]
    assert r['state'] == 'succeeded'
    assert r['graph_v2']['deepseek_calls'] == 1 and r['graph_v2']['jev_calls'] == 2
    assert len(r['graph_v2']['usage']) == 3
    assert r['historical_failure_codes'] == ['jev_tls_handshake']


def test_resume_uses_source_time_not_backfill_time(tmp_path):
    _, s, entry = seed_store(tmp_path)
    second = s.db.execute('select id from entries where id!=? order by line desc limit 1',(entry,)).fetchone()[0]
    old = s.remember(kind='progress',title='old backfill',content='old',source_entry_id=entry,source_quote='论文调研')
    new = s.remember(kind='progress',title='recent work',content='new',source_entry_id=second,source_quote='原文证据')
    with s.db:
        s.db.execute('update entries set timestamp=? where id=?',('2026-10-01T00:00:00Z',entry))
        s.db.execute('update entries set timestamp=? where id=?',('2026-10-14T00:00:00Z',second))
        s.db.execute('update work_memory set updated_at=? where id=?',('2099-01-01T00:00:00Z',old['id']))
    assert s.resume(limit=1)['memories'][0]['id'] == new['id']


def test_exact_identifier_outranks_token_permutation(tmp_path):
    _,s,entry=seed_store(tmp_path)
    target=s.remember(kind='artifact',title='Report',content='sessiongraph-capacity-fix-20261014/REPORT.md',source_entry_id=entry,source_quote='论文调研')
    wrong=s.remember(kind='artifact',title='Unrelated',content='20261014 fix capacity sessiongraph',source_entry_id=entry,source_quote='论文调研')
    with s.db:s.db.execute('update work_memory set updated_at=? where id=?',('2099-01-01T00:00:00Z',wrong['id']))
    assert s.recall('sessiongraph-capacity-fix-20261014',limit=1)['memories'][0]['id']==target['id']


def test_example_nodes_preserved_for_audit_but_not_work_graph(tmp_path):
    _,s,_=make_store(tmp_path,[('assistant','构造的测试输入：训练 A 得到模型 B')])
    d=output(entries(s))
    for n in d['nodes']:n['context_kind']='example'
    r=run_chat(s,FakeExtractor([d]),'chat-1',jev=Judge())
    assert not r['failed']
    assert not r['saved'] and not read_relations(s)
    assert s.db.execute("select count(*) from sg2_unresolved where reason='example_only'").fetchone()[0]==2
    assert not s.resume()['memories']


def test_generic_chinese_overlap_not_identity_gate(tmp_path):
    _,s,_=make_store(tmp_path,[('assistant','训练 A 得到模型 B；训练 Z 得到模型 C')])
    d=output(entries(s))
    run_chat(s,FakeExtractor([d]),'chat-1',jev=Judge())
    # Compare a genuinely different referent with only generic Chinese words shared.
    from sessiongraph.graph_v2 import _identities,_stats
    incoming=output(entries(s),title='训练 Z',artifact='模型 C')
    class Uncertain(Judge):
        def evaluate(self,state,questions):
            assert False, 'generic word overlap must not force an ambiguous identity judgment'
    bindings,pending=_identities(s,incoming,candidates(s,'训练 A 模型 B'),Uncertain(),_stats())
    assert not pending and len(bindings)>=2


def test_high_water_excludes_later_import_without_advancing_over_it(tmp_path):
    _,s,_=make_store(tmp_path,[('assistant','训练 A 得到模型 B'),('assistant','后来训练 Z 得到模型 C')])
    first=entries(s)[0]
    bound=s.db.execute('select rowid from entries where id=?',(first['id'],)).fetchone()[0]
    ex=FakeExtractor([output([first])])
    r=run_chat(s,ex,'chat-1',jev=Judge(),through_rowid=bound)
    assert not r['failed'] and not r['graph_v2']['source_remaining']
    assert len(ex.calls)==1
    r2=run_chat(s,FakeExtractor([output([entries(s)[1]],title='训练 Z',artifact='模型 C')]),'chat-1',jev=Judge())
    assert r2['model_calls']>0


def test_legacy_hook_call_limit_does_not_interrupt_progress(tmp_path):
    config=init_project(tmp_path)
    cfg=json.loads(config.read_text());cfg['hook']={'max_model_calls':2};config.write_text(json.dumps(cfg))
    Store(load_config(config)).close()
    calls=[]
    def execute(argv,timeout):
        if 'refresh-chat' in argv:return {'added_entries':3,'skipped':[]}
        calls.append(argv)
        assert '--memories-call-budget' not in argv
        return {'saved':[],'failed':[],'model_calls':2,'graph_v2':{
            'source_remaining':len(calls)<3,'input_source_chars':100,'deepseek_calls':1,'jev_calls':1}}
    r=run_update(config,'chat',execute=execute)
    assert r['state']=='succeeded' and r['error_code'] is None
    assert r['reported_model_calls']==6


def test_context_classification_does_not_discard_actual_test_results(tmp_path):
    _,s,_=make_store(tmp_path,[('assistant','真实测试运行 A 已完成，报告位于 /tmp/test-results.json；测试使用合成数据。')])
    d=output(entries(s),title='真实测试运行 A',artifact='报告 /tmp/test-results.json')
    for n in d['nodes']:n['context_kind']='work'
    r=run_chat(s,FakeExtractor([d]),'chat-1',jev=Judge())
    assert len(r['saved'])==2 and len(r['saved_relations'])==1


def test_uncertain_context_is_preserved_pending(tmp_path):
    _,s,_=make_store(tmp_path,[('assistant','训练 A 可能得到模型 B')])
    d=output(entries(s))
    for n in d['nodes']:n['context_kind']='uncertain'
    r=run_chat(s,FakeExtractor([d]),'chat-1',jev=Judge())
    assert not r['failed'] and not r['saved']
    assert s.db.execute("select count(*) from sg2_unresolved where reason='context_uncertain'").fetchone()[0]==2


def test_name_candidates_retain_aliases_and_exact_titles():
    from sessiongraph.graph_v2 import _name_candidate
    assert _name_candidate({'title':'renamed','aliases':['训练 A']},{'title':'训练 A','aliases':[]})
    assert _name_candidate({'title':'同名对象','aliases':[]},{'title':'同名对象','aliases':[]})
    assert not _name_candidate({'title':'训练 A','aliases':[]},{'title':'训练 Z','aliases':[]})


def test_work_execution_contract_includes_research_and_reports():
    from sessiongraph.graph_contract import policy
    prompt=policy([])['instructions']
    assert 'literature review' in prompt and 'HAS_RESULT/PRODUCES' in prompt
    assert 'attribution must be in the original text' in prompt


def test_hard_call_budget_stops_before_judge_and_reuses_extraction(tmp_path):
    _,s,_=make_store(tmp_path,[('assistant','训练 A 得到模型 B')])
    extractor=FakeExtractor([output(entries(s))]);judge=Judge()
    first=run_chat(s,extractor,'chat-1',jev=judge,call_budget=1)
    assert first['model_calls']==1 and judge.calls==0 and len(first['saved'])==2
    assert first['failure_codes']==['graph_call_budget_pending']
    assert s.db.execute('select count(*) from sg2_cursors').fetchone()[0]==0
    resumed=run_chat(s,FakeExtractor([]),'chat-1',jev=judge,call_budget=1)
    assert resumed['model_calls']==1 and resumed['graph_v2']['deepseek_calls']==0
    assert not resumed['saved'] and len(resumed['saved_relations'])==1


def test_budget_exhaustion_does_not_schedule_network_retry(tmp_path,monkeypatch):
    monkeypatch.setattr('sessiongraph.auto_update.time.sleep',lambda _:(_ for _ in ()).throw(AssertionError('budget is not a network retry')))
    config=init_project(tmp_path);Store(load_config(config)).close()
    def execute(argv,timeout):
        if 'refresh-chat' in argv:return {'added_entries':1,'skipped':[]}
        return {'saved':[],'failed':[{'error_code':'graph_call_budget_pending','retryable':True}],
                'pending_retry_count':1,'model_calls':1,'graph_v2':{'source_remaining':True,'deepseek_calls':1}}
    r=run_update(config,'chat',execute=execute)
    assert r['state']=='pending' and r['error_code']=='graph_call_budget_pending'


def test_runner_preserves_frozen_frontier_without_call_quota(tmp_path):
    config=init_project(tmp_path);cfg=json.loads(config.read_text());cfg['graph_v2']={'enabled':True};cfg['hook']={'max_model_calls':3};config.write_text(json.dumps(cfg));Store(load_config(config)).close()
    calls=[]
    def execute(argv,timeout):
        if 'refresh-chat' in argv:return {'added_entries':0,'skipped':[]}
        calls.append(argv)
        assert argv[argv.index('--memories-through-rowid')+1]=='0'
        assert argv[argv.index('--memories-step-budget')+1]=='1'
        assert '--memories-call-budget' not in argv
        return {'saved':[],'failed':[],'model_calls':1,'graph_v2':{'source_remaining':len(calls)==1,'target_rowid':0,'deepseek_calls':1}}
    r=run_update(config,'chat',execute=execute)
    assert len(calls)==2 and r['reported_model_calls']==2 and r['graph_v2']['target_rowid']==0
