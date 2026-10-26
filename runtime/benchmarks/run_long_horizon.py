from __future__ import annotations
import json,statistics,time
from pathlib import Path
from sessiongraph.core import Store,load_config
root=Path('/Users/shengzhoukong/Documents/jev'); cases=json.loads((root/'sessiongraph/benchmarks/long_horizon_cases.json').read_text())
s=Store(load_config(root/'.sessiongraph/config.json'))
rows=[]
for case in cases['cases']:
 row={'id':case['id'],'kind':case['kind'],'question':case['question'],'gold':case['gold_entry_ids'],'modes':{}}
 gold=set(case['gold_entry_ids'])
 for mode in ('raw','hybrid','graph'):
  t=[]; r=None
  for _ in range(10):
   started=time.perf_counter();r=s.search(case['question'],limit=8,max_chars=6000,mode=mode);t.append((time.perf_counter()-started)*1000)
  hits=r['hits']; ranks=[]
  for i,h in enumerate(hits,1):
   src={h['id']} if h['type']=='entry' else {x['entry_id'] for x in h.get('evidence',[])}
   if src & gold:ranks.append(i)
  row['modes'][mode]={'source_rank':min(ranks) if ranks else None,'hit_at3':bool(ranks and min(ranks)<=3),'hits':len(hits),'related':len(r.get('related',[])),'median_ms':round(statistics.median(t),3),'json_chars':len(json.dumps(r,ensure_ascii=False)),'model_calls':r.get('model_calls',0)}
 rows.append(row)
summary={}
for mode in ('raw','hybrid','graph'):
 pos=[r for r in rows if r['gold']];neg=[r for r in rows if not r['gold']]
 summary[mode]={'positive':len(pos),'source_hit_at3':sum(r['modes'][mode]['hit_at3'] for r in pos),'negative_candidates':sum(r['modes'][mode]['hits']>0 for r in neg),'median_ms':statistics.median(r['modes'][mode]['median_ms'] for r in rows),'median_json_chars':statistics.median(r['modes'][mode]['json_chars'] for r in rows),'model_calls':sum(r['modes'][mode]['model_calls'] for r in rows)}
out=root/'sessiongraph/outputs/long-horizon-2026-10-03';out.mkdir(exist_ok=True)
(out/'report.json').write_text(json.dumps({'scope':'cross-chat / distant-history retrieval only; not final answer quality','summary':summary,'cases':rows},ensure_ascii=False,indent=2))
print(json.dumps(summary,ensure_ascii=False,indent=2))
s.close()
