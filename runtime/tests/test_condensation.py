"""Write-time retention and identity recall survive maintenance retirement."""
import copy
import json
import pytest
from sessiongraph import condensation as c, graph_v2 as g
from sessiongraph.graph_contract import policy,prepare_evidence_spans
from sessiongraph.single_token_judge import SingleTokenJudge
from test_memories import make_store,entries


class Labels(SingleTokenJudge):
    def __init__(self,choice='same'):
        self.choice=choice;self.calls=0;self.timeout=10;self.traces=[]
        self.endpoint='fixture';self.model='fixture';self.revision='fixture'
    @property
    def cache_identity(self):return ['fixture',self.choice]
    def evaluate(self,state,questions):
        self.calls+=1
        return dict(answers={k:dict(choice=self.choice,score_kind='label_only',abstained=self.choice=='insufficient_evidence') for k in questions},model='fixture',usage=dict(output_tokens=1))


@pytest.fixture
def env(tmp_path):
    root,s,path=make_store(tmp_path,[('assistant','本轮结果已核验，误差为1。运行一与运行二不同。'),('assistant','已核验本轮结果，误差为1。')])
    s.config['graph_v2']=dict(enabled=True,condensation=dict(enabled=True))
    g.setup(s)
    yield s,entries(s)
    s.close()


def node(entry,content='本轮结果已核验，误差为1。',title='核验结果',**extra):
    return dict(dict(id='n1',type='Claim',title=title,content=content,status='completed',aliases=[],identity=[],evidence=[dict(entry_id=entry['id'],quote=entry['text'])]),**extra)


def publish(s,dbid,entry,**extra):
    n=node(entry,**extra);job='job-'+dbid
    with s.db:s.db.execute('INSERT INTO sg2_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(job,s.config['project_id'],'chat-1','chat-1','judging','{}',None,'[]','{}',None,'',''))
    g._commit(s,dict(id=job,chat='chat-1'),dict(nodes=[n],relations=[],unresolved=[]),[],dict(n1=dbid),[],[],[],g._stats(),(90,0),finalize=False)
    return n


def test_reworded_fact_without_title_match_or_offered_batch_is_recalled_and_reused(env):
    s,e=env;publish(s,'old',e[0]);incoming=node(e[0],content='已核验本轮结果，误差为1。',title='另一种表达')
    assert not g._name_candidate(incoming,c.payload(s,'old'))
    labels=Labels();stats=g._stats();bindings,pending=g._identities(s,dict(nodes=[incoming]),[],labels,stats)
    assert bindings['n1']=='old' and not pending
    assert stats['identity_recalled']==1 and stats['nodes_merged']==1 and labels.calls==1
    assert s.db.execute('SELECT count(*) FROM sg2_nodes').fetchone()[0]==1


def test_cross_message_per_object_term_recall_not_only_source_or_batch_title(env):
    s,e=env;publish(s,'old',e[0]);incoming=node(e[1],content='已核验本轮结果，误差为1。',title='另一种表达')
    found,incomplete=c.recall(s,incoming)
    assert [p['id'] for p in found]==['old'] and not incomplete


def test_different_values_cannot_merge_even_if_classifier_says_same(env):
    s,e=env;publish(s,'old',e[0]);incoming=node(e[1],content='已核验本轮结果，误差为2。',title='另一种表达')
    found,_=c.recall(s,incoming);assert not found
    bindings,pending=g._identities(s,dict(nodes=[incoming]),[],Labels(),g._stats())
    assert bindings['n1']!='old' and not pending


def test_incomplete_recall_is_not_proof_of_novelty(env):
    s,e=env
    for i in range(6):publish(s,f'old{i}',e[0])
    incoming=node(e[0],content='已核验本轮结果，误差为1。',title='另一种表达')
    bindings,pending=g._identities(s,dict(nodes=[incoming]),[],Labels('different'),g._stats())
    assert 'n1' not in bindings and pending[0][1]=='identity_uncertain'
    # If several existing duplicates ARE equivalent, reuse one rather than mint
    # another or leave a permanently ambiguous new copy.
    bindings,pending=g._identities(s,dict(nodes=[incoming]),[],Labels('same'),g._stats())
    assert bindings['n1'].startswith('old') and not pending


def test_routine_retention_stays_in_receipt_and_unique_facts_survive(env):
    _,e=env;routine=node(e[0],retention='record_only');knowledge=node(e[1],retention='knowledge');knowledge['id']='n2'
    original=dict(nodes=[routine,knowledge],relations=[],unresolved=[],coverage=dict(entry_ids=[x['id'] for x in e],outcome='candidates_found'))
    before=copy.deepcopy(original);stats=g._stats();result,blocked=c.project_output(original,stats)
    assert original==before and result['nodes']==[knowledge] and blocked=={'n1'}
    assert stats['routine_nodes_skipped']==1


def test_new_wire_requires_retention_only_for_enabled_project(env):
    _,e=env
    for flag in [False,True]:
        contract=policy([],effective=False,condense=flag)
        _,wire,_=prepare_evidence_spans(e,contract)
        n=wire['json_schema']['properties']['nodes']['items']
        assert ('retention' in n['required']) is flag
        assert ('retention' in n['properties']) is flag


def test_commit_cannot_bypass_retention_projection_or_fresh_wire_guard(env):
    s,e=env
    publish(s,'old',e[0])
    job=dict(id='job-old',chat='chat-1')
    for n,descriptor,error in [(node(e[0],retention='record_only'),{},'record_only_publication'),(node(e[0]),dict(condensation_revision='condensation-v1'),'retention_required')]:
        with s.db:s.db.execute('UPDATE sg2_jobs SET input_json=? WHERE id=?',(json.dumps(descriptor),job['id']))
        with pytest.raises(ValueError,match=error):
            g._commit(s,job,dict(nodes=[n],relations=[],unresolved=[]),[],dict(n1='other'),[],[],[],g._stats(),(90,0),finalize=False)
    assert s.db.execute('SELECT count(*) FROM sg2_nodes').fetchone()[0]==1


def test_extra_proposition_cannot_be_erased_by_same_label(env):
    s,e=env
    publish(s,'short',e[0])
    publish(s,'long',e[0],content='本轮结果已核验，误差为1。随后运行首步末步误差分解以量化参数到动作的非线性与约束项抵消的预测收益。')
    incoming=c.payload(s,'long')
    assert all(candidate['id']!='short' for candidate in c.recall(s,incoming)[0])
    assert not c.coverage_compatible(c.payload(s,'short'),c.payload(s,'long'))


@pytest.mark.parametrize('left,right',[('GPU2 空闲后将运行误差分解。','GPU2 已空闲，准备运行误差分解。'),('误差为1。条件：损失改善很小。','误差为1。损失改善很小。')])
def test_conditional_and_actual_state_are_not_merge_candidates(env,left,right):
    s,e=env
    publish(s,'a',e[0],content=left);publish(s,'b',e[0],content=right)
    assert c.protected(c.payload(s,'a'))!=c.protected(c.payload(s,'b'))
    found,_=c.recall(s,c.payload(s,'b'))
    assert all(candidate['id']!='a' for candidate in found)


@pytest.mark.parametrize('all_routine',[False,True])
def test_fresh_job_retention_end_to_end_keeps_full_receipt_and_only_promotes_knowledge(env,all_routine):
    from test_memories import FakeExtractor
    from sessiongraph.graph_contract import VERSION
    from sessiongraph.storage import load_payload
    s,e=env
    routine=node(e[0],retention='record_only')
    finding=node(e[1],retention='record_only' if all_routine else 'knowledge');finding['id']='n2'
    original=dict(schema_version=VERSION,nodes=[routine,finding],relations=[],unresolved=[],coverage=dict(entry_ids=[x['id'] for x in e],outcome='candidates_found'))
    extractor=FakeExtractor([original])
    result=g.run_chat(s,extractor,'chat-1',jev=Labels())
    assert not result['failed']
    assert len(result['saved'])==(0 if all_routine else 1)
    assert result['graph_v2']['routine_nodes_skipped']==(2 if all_routine else 1)
    receipt=load_payload(s,s.db.execute('SELECT output_json FROM sg2_jobs').fetchone()[0])
    assert len(receipt['nodes'])==2 and receipt['nodes'][0]['retention']=='record_only'
    assert s.db.execute('SELECT count(*) FROM entries').fetchone()[0]==2
