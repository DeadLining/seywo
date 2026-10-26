from sessiongraph.storage import load_payload
"""Capacity recovery must preserve ranges, receipts, and atomic cursor semantics."""
import json
import pytest
from sessiongraph.core import Store
from sessiongraph.extraction import ProviderError
from sessiongraph.graph_contract import VERSION, SCHEMA, check
from sessiongraph.graph_v2 import run_chat, diagnostics
from test_memories import make_store
from test_graph_v2 import Judge, output


class Extractor:
    def __init__(self, limit=700, failure='tokens'):
        self.limit, self.failure, self.calls = limit, failure, []
    def __call__(self, batch, policy):
        self.calls.append([(e['id'],e['source_start'],e['source_end']) for e in batch])
        if sum(e['source_end']-e['source_start'] for e in batch) > self.limit:
            if self.failure == 'tokens':
                raise ProviderError('provider output token budget exhausted','provider_output_limit')
            out = output(batch, quote=batch[0]['text'][:100])
            if self.failure == 'nodes':
                out['nodes'] = [dict(out['nodes'][0], id=f'n{i+1}') for i in range(14)]
            else:
                out['nodes'][0]['evidence'] *= 9
            return out
        return {'schema_version':VERSION,'nodes':[],'relations':[],'unresolved':[],
                'coverage':{'entry_ids':[e['id'] for e in batch],'outcome':'no_durable_item'}}


def assert_ranges(store, job):
    desc = json.loads(job['input_json'])
    expected = desc['original_input']['entries']
    actual = []
    for row in store.db.execute('select input_json,output_json from sg2_job_parts where job=?',(job['id'],)):
        part = json.loads(row[0]); out = load_payload(store,row[1])
        assert out['coverage']['entry_ids'] == [e['id'] for e in part['entries']]
        actual.extend(part['entries'])
    for e in expected:
        ranges = sorted((a['source_start'],a['source_end']) for a in actual if a['id']==e['id'])
        assert ranges[0][0] == e['source_start'] and ranges[-1][1] == e['source_end']
        assert all(a[1]==b[0] for a,b in zip(ranges,ranges[1:]))
        assert all(a['source_hash']==e['source_hash'] for a in actual if a['id']==e['id'])
    assert {a['id'] for a in actual} == {e['id'] for e in expected}


@pytest.mark.parametrize('failure',['tokens','nodes','evidence'])
def test_recursive_capacity_recovery_preserves_every_range(tmp_path,failure):
    _,s,_=make_store(tmp_path,[('user','甲'*1500),('assistant','乙'*900)])
    s.config['graph_v2']={'max_batches':16}
    ex=Extractor(failure=failure)
    r=run_chat(s,ex,'chat-1',jev=Judge())
    assert not r['failed'] and r['graph_v2']['adaptive_splits']==3
    assert r['graph_v2']['deepseek_calls']==7
    job=s.db.execute('select * from sg2_jobs').fetchone()
    assert job['state']=='complete'
    assert_ranges(s,job)
    assert s.db.execute('select line,start from sg2_cursors').fetchone() is not None
    assert run_chat(s,Extractor(),'chat-1',pending_only=True,jev=Judge())['model_calls']==0
    s.close()


def test_crash_resume_never_replays_committed_part_or_advances_early(tmp_path):
    _,s,_=make_store(tmp_path,[('user','甲'*2400)])
    s.config['graph_v2']={'max_batches':2}
    ex=Extractor(limit=1300)
    r=run_chat(s,ex,'chat-1',jev=Judge())
    assert not r['failed'] and r['graph_v2']['budget_exhausted']
    assert s.db.execute('select count(*) from sg2_job_parts').fetchone()[0]==1
    assert s.db.execute('select count(*) from sg2_cursors').fetchone()[0]==0
    cfg=s.config;s.close();s=Store(cfg)
    resumed=Extractor(limit=1300)
    r=run_chat(s,resumed,'chat-1',pending_only=True,jev=Judge())
    assert not r['failed'] and len(resumed.calls)==1
    assert resumed.calls[0][0][1]==1200
    job=s.db.execute('select * from sg2_jobs').fetchone()
    assert job['state']=='complete';assert_ranges(s,job)
    s.close()


def test_legacy_output_limit_splits_before_repeating_same_request(tmp_path):
    _,s,_=make_store(tmp_path,[('user','甲'*2400)])
    s.config['graph_v2']={'max_batches':1}
    def fail(batch,policy): raise ValueError('legacy failure')
    run_chat(s,fail,'chat-1',jev=Judge())
    with s.db:
        s.db.execute("update sg2_jobs set error='provider_output_limit',stats_json=?",(json.dumps({'failure_reason':'provider output token budget exhausted'}),))
    s.config['graph_v2']['max_batches']=8
    ex=Extractor(limit=1300)
    r=run_chat(s,ex,'chat-1',pending_only=True,jev=Judge())
    assert not r['failed'] and len(ex.calls)==2
    assert all(sum(b-a for _,a,b in c)==1200 for c in ex.calls)
    s.close()


def test_terminal_and_non_capacity_errors_remain_visible_without_skipping(tmp_path):
    _,s,_=make_store(tmp_path,[('user','甲'*1200)])
    r=run_chat(s,Extractor(limit=0),'chat-1',jev=Judge())
    assert r['failure_codes']==['provider_output_limit']
    assert r['graph_v2']['deepseek_calls']==2
    assert s.db.execute('select count(*) from sg2_cursors').fetchone()[0]==0
    assert len(diagnostics(s)['blocked_jobs'])==1
    s.close()


def test_bad_evidence_is_not_capacity_and_does_not_trigger_extra_calls(tmp_path):
    _,s,_=make_store(tmp_path,[('user','甲'*2400)])
    def fake(batch,policy): return output(batch,quote='fabricated unsupported evidence')
    r=run_chat(s,fake,'chat-1',jev=Judge())
    assert r['failed'] and r['graph_v2']['deepseek_calls']==1
    assert r['graph_v2']['adaptive_splits']==0
    s.close()


def test_array_diagnostics_distinguish_shape_and_bounds():
    spec=SCHEMA['properties']['nodes']
    with pytest.raises(ValueError,match='expected_array'): check({},spec,'output.nodes')
    with pytest.raises(ValueError,match='min=0:max=12:actual=14'): check([{}]*14,spec,'output.nodes')


def test_conflicting_existing_candidate_does_not_abort_valid_sibling(tmp_path):
    _,s,_=make_store(tmp_path,[('user','训练 A 产出模型 B。'*200)])
    s.config['graph_v2']={'max_batches':1}
    def first(batch,policy):return output(batch,quote=batch[0]['text'][:50])
    run_chat(s,first,'chat-1',max_chars=1000,jev=Judge())
    original=s.db.execute("select id from sg2_nodes where type='Execution'").fetchone()[0]
    before=dict(s.db.execute('select * from work_memory where id=?',(original,)).fetchone())
    def conflict(batch,policy):
        out=output(batch,artifact='新的报告 C',quote=batch[0]['text'][:50])
        out['nodes'][0].update(id=original,type='Claim')
        out['relations'][0]['source']=original
        return out
    result=run_chat(s,conflict,'chat-1',max_chars=1000,jev=Judge())
    assert not result['failed'] and result['saved']
    assert not result['saved_relations']
    assert diagnostics(s)['unresolved']['existing_identity_conflict']==1
    assert dict(s.db.execute('select * from work_memory where id=?',(original,)).fetchone())==before
    pending=s.db.execute("select payload_json from sg2_unresolved where reason='existing_identity_conflict'").fetchone()[0]
    assert json.loads(pending)['type']=='Claim'
    s.close()


def test_error_guided_schema_repair_is_bounded_and_durable(tmp_path):
    _,s,_=make_store(tmp_path,[('user','训练 A 产出模型 B。')])
    s.config['graph_v2']={'max_batches':1}
    seen=[]
    def wrong(batch,policy):
        seen.append(policy)
        out=output(batch);out['nodes'][0]['type']='InventedType'
        return out
    first=run_chat(s,wrong,'chat-1',jev=Judge())
    assert not first['failed'] and first['graph_v2']['budget_exhausted']
    assert first['graph_v2']['schema_repairs']==1
    cfg=s.config;s.close();s=Store(cfg)
    second=run_chat(s,wrong,'chat-1',pending_only=True,jev=Judge())
    assert second['failed'] and second['graph_v2']['deepseek_calls']==1
    assert second['graph_v2']['schema_repairs']==0
    assert seen[1]['validation_feedback']['error']=='graph_schema:output.nodes[0].type:enum'
    assert s.db.execute('select count(*) from sg2_cursors').fetchone()[0]==0
    s.close()


def test_schema_repair_still_requires_valid_evidence(tmp_path):
    _,s,_=make_store(tmp_path,[('user','训练 A 产出模型 B。')])
    calls=[]
    def fix(batch,policy):
        calls.append(policy)
        out=output(batch)
        if len(calls)==1:out['nodes'][0]['type']='InventedType'
        return out
    result=run_chat(s,fix,'chat-1',jev=Judge())
    assert not result['failed'] and len(calls)==2 and result['saved_relations']
    assert 'validation_feedback' in calls[1]
    s.close()
