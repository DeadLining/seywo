"""Metrics may report source IDs, never promote them to answer correctness."""
import importlib.util
from pathlib import Path

PATH=Path(__file__).resolve().parents[1]/'benchmarks/run_frozen_packet_preflight.py'
spec=importlib.util.spec_from_file_location('packet_preflight',PATH)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_source_coverage_and_negative_candidates_are_separate():
    rows=[]
    for budget in [2048,4096,8192]:
        for mode in ['raw','graph']:
            rows.extend([
                dict(id='P',group='history',mode=mode,budget=budget,seconds=.01,bytes=400,
                     packet={'entries':[{'entry_id':'s1','truncated':True,'text_coverage':.02}]}),
                dict(id='N',group='negative',mode=mode,budget=budget,seconds=.02,bytes=300,
                     packet={'entries':[{'entry_id':'noise','truncated':False,'text_coverage':1}]}),
            ])
    cases=[{'id':'P','gold_evidence':[{'id':'s1'},{'id':'s2'}]},
           {'id':'N','gold_evidence':[]}]
    result=module.measure(rows,cases)
    assert len(result)==6
    for r in result:
        assert r['found_gold']==1 and r['gold_sources']==2
        assert r['positive_all_gold_found']==0
        assert r['negative_nonempty']==1 and r['negative_trials']==1
        assert r['returned_entries']==2 and r['truncated_entries']==1
        assert r['mean_source_text_coverage']==.51
        assert r['p95_ms']==20
        assert not any('accuracy' in k for k in r)
