"""Two-process MCP continuity smoke; synthetic, not real-chat accuracy."""
import argparse
import asyncio
import hashlib
import json
import math
import statistics
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sessiongraph.core import Store, init_project, load_config

ROOT = Path(__file__).resolve().parents[1]


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')


def fixture(out):
    root = out/'fixture'; root.mkdir()
    cfg = init_project(root)
    records = []
    store = Store(load_config(cfg))
    for i, domain in enumerate(['实验', '调研', '工作', '论文']):
        chat = f'continuity-{i}'
        artifact = root/f'artifact-{i}.md'; artifact.write_text(f'{domain}测试产物，仅用于契约测试。\n')
        texts = [
            ('goal', 'active', f'{domain}目标：核对资料后输出说明。'),
            ('constraint', 'active', f'{domain}约束：不得把未确认意见当成结论。'),
            ('decision', 'active', f'{domain}决定：先采用方案甲。'),
            ('progress', 'completed', f'{domain}进度：资料已整理，核对尚未完成。'),
            ('finding', 'active', f'{domain}结论：现有资料不足以确认生产效果。'),
            ('artifact', 'active', f'{domain}产物位置：{artifact}'),
            ('next_step', 'active', f'{domain}下一步：逐条核对原始依据。'),
            ('decision', 'active', f'{domain}修订决定：改用方案乙，替代方案甲。'),
        ]
        events = [{'type':'session_meta','payload':{'id':chat,'cwd':str(root)}}]
        for kind, status, text in texts:
            events.append({'type':'response_item','timestamp':'2026-10-08T00:00:00Z',
                           'payload':{'type':'message','role':'user','content':[{'type':'input_text','text':text}]}})
        transcript = root/f'{chat}.jsonl'
        transcript.write_text(''.join(json.dumps(e,ensure_ascii=False)+'\n' for e in events))
        imported = store.ingest(transcript)
        sources = store.db.execute('SELECT id,text FROM entries WHERE session_id=? ORDER BY line', (imported['session_id'],)).fetchall()
        for n, ((kind,status,text),source) in enumerate(zip(texts,sources)):
            records.append(dict(chat_id=chat,kind=kind,status=status,title=f'{domain}-{kind}',content=text,
                                source_entry_id=source['id'],source_quote=source['text'],scope=chat,
                                paths=[str(artifact)] if kind=='artifact' else [],
                                memory_id=f'{chat}-{n}', **({'supersedes_id':f'{chat}-2'} if n==7 else {})))
    store.close()
    return cfg, records


async def run(out):
    out.mkdir(exist_ok=False,parents=True)
    cfg, records = fixture(out)
    dump(out/'inputs.json', records)
    shim = '''import socket,sys,json,atexit
from pathlib import Path
attempts=[]
def deny(*a,**kw):
 attempts.append('network');raise RuntimeError('no network permitted')
socket.socket.connect=deny
socket.create_connection=deny
atexit.register(lambda:Path(sys.argv[2]).write_text(json.dumps({'network_attempts':attempts,'provider_loaded':any(x in sys.modules for x in ['sessiongraph.extraction','sessiongraph.gateway_worker'])})))
from sessiongraph.mcp_server import create_server
create_server(Path(sys.argv[1])).run(transport='stdio')
'''
    calls = []
    async def call(client, tool, args, error=False):
        before=time.perf_counter(); result=await client.call_tool(tool,args)
        elapsed=time.perf_counter()-before
        assert bool(result.isError)==error, str(result.content)
        text=''.join(x.text for x in result.content if x.type=='text')
        body=text if error else json.loads(text)
        calls.append(dict(tool=tool,seconds=elapsed,error=bool(result.isError),args=args,result=body))
        return body
    for phase in ['write','read']:
        params=StdioServerParameters(command=sys.executable,args=['-c',shim,str(cfg),str(out/f'{phase}-network.json')],cwd=str(ROOT))
        with (out/f'{phase}-stderr.log').open('w') as err:
            async with stdio_client(params,errlog=err) as (r,w):
                async with ClientSession(r,w) as client:
                    await client.initialize()
                    if phase=='write':
                        for record in records:
                            await call(client,'session_remember',record)
                        # Retry old record must not revive it; no mutation from bad provenance.
                        assert (await call(client,'session_remember',records[2]))['status']=='superseded'
                        await call(client,'session_remember',dict(records[0],source_quote='NOT IN SOURCE'),error=True)
                    else:
                        for i in range(4):
                            chat=f'continuity-{i}'
                            for rep in range(5):
                                resumed=await call(client,'session_resume',dict(chat_id=chat,include_completed=True))
                                selected=resumed['memories']
                                assert {m['id'] for m in selected}=={f'{chat}-{n}' for n in [0,1,3,4,5,6,7]}
                                for m in selected:
                                    expected=next(x for x in records if x['memory_id']==m['id'])
                                    assert m['content']==expected['content']
                                    assert m['source']['quote']==expected['source_quote']
                                    assert m['source']['end']-m['source']['start']==len(expected['source_quote'])
                                    assert m['paths']==expected['paths']
                                    assert all(Path(p).is_file() for p in m['paths'])
                                recalled=await call(client,'session_recall',dict(chat_id=chat,query='产物位置',limit=1))
                                assert recalled['memories'][0]['kind']=='artifact'
                            absent=await call(client,'session_recall',dict(chat_id=chat,query='ZXQ_NONEXISTENT_999'))
                            assert not absent['memories']
    audits=[json.loads((out/f'{p}-network.json').read_text()) for p in ['write','read']]
    assert all(not a['network_attempts'] and not a['provider_loaded'] for a in audits)
    summaries={}
    for tool in ['session_resume','session_recall']:
        times=sorted(c['seconds']*1000 for c in calls if c['tool']==tool)
        summaries[tool]={'calls':len(times),'p50_ms':statistics.median(times),'p95_ms':times[math.ceil(len(times)*.95)-1]}
    dump(out/'calls.json',calls)
    dump(out/'report.json',dict(kind='synthetic_two_process_contract_not_answer_quality',
         distinct_scenarios=4,write_calls=34,read_calls=44,current_items_per_scenario=7,
         restored_items=28,source_text_identity_checks=True,real_fixture_files_exist=True,
         superseded_hidden=True,retry_does_not_reactivate=True,unsupported_quote_rejected=True,
         negative_queries_empty=4,latency=summaries,network_audits=audits,
         control_without_memory=None,real_chat_resume_accuracy=None,
         source_hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'sessiongraph').glob('*.py')}))
    print(json.dumps(summaries,ensure_ascii=False))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    asyncio.run(run(p.parse_args().output.resolve()))
