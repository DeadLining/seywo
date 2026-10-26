"""Zero-provider retrieval applicability check; gold is opened only after retrieval."""
import argparse
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import random
import statistics
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sessiongraph.core import load_config  # noqa: E402


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def measure(rows, cases):
    gold = {c['id']: {e['id'] for e in c['gold_evidence']} for c in cases}
    for row in rows:
        expected = gold[row['id']]
        got = {e['entry_id'] for e in row['packet']['entries']}
        row.update(gold_sources=len(expected), found_gold=len(got & expected),
                   irrelevant_sources=len(got - expected), all_gold_found=bool(expected) and expected <= got)
    summaries=[]
    for budget in [2048,4096,8192]:
        for mode in ['raw','graph']:
            rs=[r for r in rows if r['budget']==budget and r['mode']==mode]
            ps=[r for r in rs if r['group']!='negative']
            ns=[r for r in rs if r['group']=='negative']
            times=sorted(r['seconds']*1000 for r in rs)
            entries=[e for r in rs for e in r['packet']['entries']]
            summaries.append(dict(budget=budget,mode=mode,calls=len(rs),
                p50_ms=statistics.median(times),p95_ms=times[math.ceil(len(times)*.95)-1],
                mean_bytes=statistics.mean(r['bytes'] for r in rs),
                positive_trials=len(ps),positive_all_gold_found=sum(r['all_gold_found'] for r in ps),
                found_gold=sum(r['found_gold'] for r in ps),gold_sources=sum(r['gold_sources'] for r in ps),
                negative_trials=len(ns),negative_nonempty=sum(bool(r['packet']['entries']) for r in ns),
                truncated_entries=sum(e['truncated'] for e in entries),returned_entries=len(entries),
                mean_source_text_coverage=statistics.mean(e['text_coverage'] for e in entries) if entries else None))
    return summaries


async def run(base, out):
    if out.exists():
        raise RuntimeError('Refusing to overwrite existing measurements')
    out.mkdir(parents=True)
    os.chmod(out,0o700)
    cfg=load_config(base/'config.json');db=Path(cfg['database']);before=sha(db)
    assert before==json.loads((base/'graph-freeze.json').read_text())['db_sha256']
    questions=json.loads((base/'questions.json').read_text())
    originals={e['id']:e for e in json.loads((base/'corpus.json').read_text())}
    assert len(questions)==30 and len(originals)==245
    jobs=[(q,m,b,r) for q in questions for m in ['raw','graph'] for b in [2048,4096,8192] for r in range(3)]
    random.Random(417).shuffle(jobs)
    sources={str(p.relative_to(ROOT)):sha(p) for p in (ROOT/'sessiongraph').glob('*.py')}
    freeze=dict(base=str(base),database_sha256=before,question_sha256=sha(base/'questions.json'),
                corpus_sha256=sha(base/'corpus.json'),policy_sha256=sha(base/'policy.json'),
                config_sha256=sha(base/'config.json'),source_sha256=sources,
                runner_sha256=sha(Path(__file__)),calls=len(jobs),provider_calls=0,
                query_policy='verbatim question; no gold access until all retrieval calls finish',
                limits=dict(max_sources=8,budgets=[2048,4096,8192],repetitions=3))
    dump(out/'freeze.json',freeze)
    shim='''import socket,sys,json,atexit
from pathlib import Path
attempts=[]
def deny(*args,**kwargs):
 attempts.append('network_attempt');raise RuntimeError('network forbidden')
socket.socket.connect=deny
socket.create_connection=deny
atexit.register(lambda:Path(sys.argv[2]).write_text(json.dumps({'attempts':attempts,'provider_module_loaded':any(x in sys.modules for x in ['sessiongraph.extraction','sessiongraph.gateway_worker'])})))
from sessiongraph.mcp_server import create_server
create_server(Path(sys.argv[1])).run(transport='stdio')
'''
    params=StdioServerParameters(command=sys.executable,args=['-c',shim,str(base/'config.json'),str(out/'network.json')],cwd=str(ROOT),env=dict(os.environ))
    rows=[];start=time.perf_counter()
    with (out/'mcp-stderr.log').open('w') as err:
        async with stdio_client(params,errlog=err) as (reader,writer):
            async with ClientSession(reader,writer) as client:
                await client.initialize();startup=time.perf_counter()-start
                await client.call_tool('session_packet',{'query':'warmup'})
                for q,mode,budget,rep in jobs:
                    begin=time.perf_counter()
                    res=await client.call_tool('session_packet',dict(query=q['question'],mode=mode,max_bytes=budget,max_sources=8))
                    seconds=time.perf_counter()-begin
                    assert not res.isError and res.structuredContent is None
                    wire=''.join(c.text for c in res.content if c.type=='text')
                    packet=json.loads(wire);assert len(wire.encode())<=budget and packet['model_calls']==0
                    # Added coverage is audit metadata, not part of returned/provider packet.
                    checks=[]
                    for e in packet['entries']:
                        source=originals[e['entry_id']]
                        assert e['text']==source['text'][e['start']:e['end']]
                        assert e['role']==source['role'] and e['timestamp']==source['timestamp']
                        assert e['chat_id']==source['chat_id'] and e['segment_id']==source['session_id']
                        checks.append(dict(entry_id=e['entry_id'],text_coverage=len(e['text'])/len(source['text'])))
                    rows.append(dict(id=q['id'],group=q['group'],query=q['question'],mode=mode,budget=budget,rep=rep,seconds=seconds,bytes=len(wire.encode()),packet=packet,coverage_checks=checks))
    network=json.loads((out/'network.json').read_text())
    assert not network['attempts'] and not network['provider_module_loaded']
    assert sha(db)==before and sha(base/'questions.json')==freeze['question_sha256']
    assert all(sha(ROOT/name)==digest for name,digest in sources.items())
    # Retrieval is finished. Only here are scoring sources opened.
    scored=json.loads(json.dumps(rows))
    for r in scored:
        coverage={c['entry_id']:c['text_coverage'] for c in r['coverage_checks']}
        for e in r['packet']['entries']:e['text_coverage']=coverage[e['entry_id']]
    summaries=measure(scored,json.loads((base/'cases.json').read_text()))
    dump(out/'packets.json',rows)
    dump(out/'scored-packets.json',scored)
    dump(out/'report.json',dict(kind='retrieval_only_not_answer_accuracy',calls=len(rows),startup_seconds=startup,
        summaries=summaries,network=network,database_unchanged=True,provider_calls=0,
        limitations=['Question-verbatim prefetch differs from prior model-composed queries.',
                     'Source-ID recall is not passage entailment or answer accuracy.',
                     'Same byte/source budgets for both modes; original full raw baseline preserved.',
                     'No end-to-end answers, actual token usage or extraction accuracy measured.']))
    print(json.dumps(summaries,ensure_ascii=False,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();asyncio.run(run(args.base.resolve(),args.output.resolve()))
