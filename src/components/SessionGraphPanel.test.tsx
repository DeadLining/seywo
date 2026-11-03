import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SessionGraphPanel } from './SessionGraphPanel'
import type { SessionGraphRead, SessionGraphManage } from '../sessionGraph'

const projects = { projects: [{ id: 'p1', name: 'jev', root: '/project/jev', state: 'ready' }, { id: 'p2', name: 'Other', root: '/project/other', state: 'ready' }, { id: 'p3', name: 'Empty', root: '/project/empty', state: 'not_initialized' }], default_project: 'p1', model_calls: 0 }
const graph = { revision: 'rev1', nodes: [{ id: 'm1', type: 'memory', label: '实验结果', content: 'F1 = 0.913', kind: 'finding', status: 'active', chat_id: 'chat1', evidence_status: 'quote_located' }, { id: 'm2', type: 'memory', label: '训练记录', content: 'SG-LONG-314', kind: 'artifact', status: 'completed', chat_id: 'chat1' }], edges: [{ source: 'm1', target: 'm2', kind: 'SUPPORTS', basis: 'semantic_claim' }], chats: [{chat_id:'chat1',count:2}], stats: { memories: 2, total_memories: 2, references: 0, semantic_relations: 1, reference_relations: 0 }, notice: '语义关系仍需核实', model_calls: 0 }
function client() {
  return vi.fn(async (op: string) => {
    if (op === 'projects') return projects
    if (op === 'work_map') return graph
    if (op === 'status') return { runs: [], active_count: 0, model_calls: 0 }
    if (op === 'memory') return { memory: { content: 'F1 = 0.913', paths: ['/tmp/report.json'], source: {entry_id: 'e1', quote: '原文结果 F1 = 0.913', role: 'assistant'} }, entries: [{id:'e1',text:'原文结果 F1 = 0.913',role:'assistant',chat_id:'chat1'}], superseded_by: [], model_calls: 0 }
    return {}
  }) as unknown as ReturnType<typeof vi.fn> & SessionGraphRead
}
beforeEach(() => localStorage.clear())
afterEach(() => vi.restoreAllMocks())
describe('SessionGraphPanel', () => {
  it('has no standalone graph compaction controls or background settings calls', async () => {
    const read=client(), manage=vi.fn()
    render(<SessionGraphPanel read={read} manage={manage}/>)
    await screen.findByText('实验结果')
    expect(screen.queryByRole('button',{name:'图谱压缩'})).not.toBeInTheDocument()
    expect(screen.queryByRole('button',{name:/判别历史重复/})).not.toBeInTheDocument()
    expect(screen.getByRole('button',{name:'存储维护'})).toBeInTheDocument()
    expect(manage).not.toHaveBeenCalled()
  })
  it('shows revision disputes separately from execution status without extra reads', async () => {
    const read = client(); const original = read.getMockImplementation()! as (...args: unknown[])=>Promise<unknown>
    read.mockImplementation((op:string,...args:unknown[])=>op==='work_map'?Promise.resolve({...graph,nodes:graph.nodes.map(n=>({...n,validity:{state:'disputed',revision:null}}))}):original(op,...args))
    render(<SessionGraphPanel read={read} />)
    fireEvent.click(await screen.findByRole('button', {name:'实验结果'}))
    expect(await screen.findByText('实验结果 · 存在争议')).toBeInTheDocument()
    expect(read.mock.calls.filter(([op])=>op==='memory')).toHaveLength(1)
  })
  it('does not confuse read failure with a missing or empty graph', async () => {
    const read=client(); const original=read.getMockImplementation()! as (...args: unknown[])=>Promise<unknown>
    read.mockImplementation((op: string,...args:unknown[])=>op==='work_map'?Promise.reject(new Error('SessionGraph 本地读取超时；已停止读取进程')):original(op,...args))
    render(<SessionGraphPanel read={read} />)
    await screen.findByText('图谱读取失败')
    expect(screen.getByText(/读取失败不代表图谱为空或数据已删除/)).toBeInTheDocument()
    expect(screen.queryByText('索引未就绪')).not.toBeInTheDocument()
    expect(screen.getByText('后台 0 项进行中')).toBeInTheDocument()
  })

  it('distinguishes immediate queueing from failure retry backoff', async () => {
    const read=client(); const original=read.getMockImplementation()! as (...args: unknown[])=>Promise<unknown>
    read.mockImplementation((op: string,...args:unknown[])=>op==='status'?Promise.resolve({runs:[],active_count:0,continuations:[
      {chat:'ready',state:'ready',due:0},
      {chat:'retry',state:'retry_wait',due:1792034258,error_code:'extraction_failed'},
    ]}):original(op,...args))
    render(<SessionGraphPanel read={read} />)
    await screen.findByText('等待执行 · 将按队列顺序继续')
    expect(screen.getByText(/请求失败，等待重试/)).toBeInTheDocument()
    expect(screen.queryByText(/冷却中/)).not.toBeInTheDocument()
  })

  it('shows legacy circuit-only runs and current cooldowns without failure styling', async () => {
    const read=client(); const original=read.getMockImplementation()! as (...args: unknown[])=>Promise<unknown>
    read.mockImplementation((op: string,...args:unknown[])=>op==='status'?Promise.resolve({runs:[{
      run_id:'circuit-only',chat_id:'chat1',state:'failed',error_code:'resource_circuit_open',model_calls:0,
    }],active_count:0,continuations:[{chat:'chat1',state:'retry_wait',due:1792034258,error_code:'resource_circuit_open'}]}):original(op,...args))
    render(<SessionGraphPanel read={read} />)
    const status=await screen.findByLabelText('等待连接恢复')
    expect(status).toHaveClass('sg-dot-neutral')
    expect(screen.getByText(/^连接冷却中 ·/)).toBeInTheDocument()
    expect(screen.queryByText(/请求失败，等待重试/)).not.toBeInTheDocument()
    expect(status.closest('li')?.querySelector('.sg-error')).toBeNull()
  })

  it('explains incompatible one-token routes as blocked instead of waiting', async () => {
    const read=client(); const original=read.getMockImplementation()! as (...args: unknown[])=>Promise<unknown>
    read.mockImplementation((op: string,...args:unknown[])=>op==='status'?Promise.resolve({runs:[],active_count:0,
      continuations:[{chat:'chat1',state:'blocked',due:0,error_code:'single_token_budget_incompatible'}]}):original(op,...args))
    render(<SessionGraphPanel read={read} />)
    expect(await screen.findByText(/当前模型不支持单标签分类/)).toBeInTheDocument()
    expect(screen.queryByText(/请求失败，等待重试|自动续跑中/)).not.toBeInTheDocument()
  })

  it('renders blocked continuation errors in Chinese without raw codes', async () => {
    const read=client(); const original=read.getMockImplementation()! as (...args: unknown[])=>Promise<unknown>
    read.mockImplementation((op: string,...args:unknown[])=>op==='status'?Promise.resolve({runs:[],active_count:0,continuations:[
      {chat:'chat-38630c',state:'blocked',due:0,error_code:'worker_failed'},
      {chat:'chat-d68bbc',state:'blocked',due:0,error_code:'graph_validation'},
    ]}):original(op,...args))
    render(<SessionGraphPanel read={read} />)
    expect(await screen.findByText(/后台抽取进程意外退出/)).toBeInTheDocument()
    expect(screen.getByText(/图谱抽取结果未通过校验/)).toBeInTheDocument()
    expect(screen.queryByText(/worker_failed|graph_validation/)).not.toBeInTheDocument()
  })

  it('explains legacy daily-limit records without telling the user to wait until tomorrow', async () => {
    const read=client(); const original=read.getMockImplementation()! as (...args: unknown[])=>Promise<unknown>
    read.mockImplementation((op: string,...args:unknown[])=>op==='status'?Promise.resolve({runs:[{
      run_id:'legacy-daily',chat_id:'current-chat',state:'pending',error_code:'daily_call_budget_pending',
    }],active_count:0,continuations:[{chat:'current-chat',state:'daily_limit',due:9999999999}]}):original(op,...args))
    render(<SessionGraphPanel read={read} />)
    await screen.findByText('旧版限制已移除，等待自动续跑')
    expect(screen.getByText(/旧版每日调用限制/)).toBeInTheDocument()
    expect(screen.queryByText(/今日额度已用完|等待次日/)).not.toBeInTheDocument()
  })

  it.each([
    {source_chars:0,excluded_chars:0,selected_chars:0},
    {source_chars:1200,excluded_chars:null},
    {source_chars:'invalid',excluded_chars:[],selected_chars:{}},
    null,
  ])('accepts incomplete diagnostic statistics: %j', async (input_stats) => {
    const read=client(); const original=read.getMockImplementation()! as (...args: unknown[])=>Promise<unknown>
    read.mockImplementation((op: string,...args:unknown[])=>op==='status'?Promise.resolve({runs:[{
      run_id:'partial',chat_id:'current-chat',state:'pending',event:'AutoContinue',input_stats,
      graph_v2:{input_source_chars:null},
    }],active_count:0}):original(op,...args))
    render(<SessionGraphPanel read={read} />)
    await screen.findByText('实验结果')
    if(input_stats?.source_chars===0) expect(screen.getByText(/本轮扫描原文/)).toHaveTextContent('本轮扫描原文 0 字符 · 规则排除 0 字符 · 保留 0 字符')
    expect(screen.getByText('自动续跑')).toBeInTheDocument()
  })

  it('keeps the graph visible when graph-v2 runs have empty input_stats', async () => {
    const read=client(); const original=read.getMockImplementation()! as (...args: unknown[])=>Promise<unknown>
    read.mockImplementation((op: string,...args:unknown[])=>op==='status'?Promise.resolve({runs:[{
      run_id:'ec326a3e683545b0be60d6038b1d9e81',chat_id:'current-chat',state:'pending',event:'AutoContinue',
      input_stats:{},updated_at:1792033310.120251,
    }],active_count:0}):original(op,...args))
    render(<SessionGraphPanel read={read} />)
    await screen.findByText('实验结果')
    expect(screen.getByText(/本轮扫描原文/)).toHaveTextContent('本轮扫描原文 — 字符 · 规则排除 — 字符 · 保留 — 字符')
  })

  it('loads the default project and shows honest read-only status', async () => {
    const read = client(); render(<SessionGraphPanel read={read} />)
    await screen.findByText('实验结果')
    expect(screen.getByLabelText('项目')).toHaveValue('p1')
    expect(screen.queryByText(/MCP.*Hook.*未探测/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button',{name:'接入设置'})).not.toBeInTheDocument()
    expect(screen.queryByLabelText('启用面板连接')).not.toBeInTheDocument()
    expect(read).toHaveBeenCalledWith('work_map', {project:'p1',view:'knowledge'})
    expect(read.mock.calls.every(([op]) => ['projects','work_map','status'].includes(op))).toBe(true)
  })
  it('shows evidence only after selecting an item, without a provider call', async () => {
    const read = client(); render(<SessionGraphPanel read={read} />)
    fireEvent.click(await screen.findByText('实验结果'))
    await screen.findByText('原文结果 F1 = 0.913', {selector:'blockquote'})
    expect(screen.getByText('/tmp/report.json')).toBeInTheDocument()
    expect(read).toHaveBeenCalledWith('memory', {project:'p1',id:'m1'})
    expect(screen.getByText(/历史声明/)).toBeInTheDocument()
  })
  it('switches projects and clears old selection and evidence', async () => {
    const read = client(); render(<SessionGraphPanel read={read} />)
    fireEvent.click(await screen.findByText('实验结果')); await screen.findByText('/tmp/report.json')
    fireEvent.change(screen.getByLabelText('项目'), {target:{value:'p2'}})
    await waitFor(() => expect(read).toHaveBeenCalledWith('work_map',{project:'p2',view:'knowledge'}))
    expect(screen.queryByText('/tmp/report.json')).not.toBeInTheDocument()
  })
  it('does not read an uninitialized project or create an index', async () => {
    const read = client(); render(<SessionGraphPanel read={read} />)
    await screen.findByText('实验结果'); read.mockClear()
    fireEvent.change(screen.getByLabelText('项目'),{target:{value:'p3'}})
    await screen.findByText(/项目尚未初始化/)
    expect(read).not.toHaveBeenCalled()
  })
  it('renders connection failure and allows retry', async () => {
    const read = client(); read.mockRejectedValueOnce(new Error('服务未连接'))
    render(<SessionGraphPanel read={read} />)
    await screen.findByRole('alert')
    fireEvent.click(screen.getByRole('button',{name:'刷新'}))
    await screen.findByText('实验结果')
  })
  it('searches the whole project on submit and renders stored graph edges', async () => {
    const read=client(), original=read.getMockImplementation()! as (...a:unknown[])=>Promise<unknown>
    read.mockImplementation(async(op:string,args?:{query?:string})=>op==='work_map'&&args?.query?{...graph,nodes:[graph.nodes[0]],edges:[]}:original(op,args))
    render(<SessionGraphPanel read={read}/>);await screen.findByText('实验结果');read.mockClear()
    fireEvent.change(screen.getByLabelText('搜索事项'),{target:{value:'0.913'}})
    expect(read).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button',{name:'搜索全部历史'}))
    await waitFor(()=>expect(screen.queryByText('训练记录')).not.toBeInTheDocument())
    expect(read).toHaveBeenCalledWith('work_map',{project:'p1',query:'0.913',chat:'',offset:0,view:'knowledge'})
    fireEvent.change(screen.getByLabelText('搜索事项'),{target:{value:''}})
    fireEvent.click(screen.getByRole('button',{name:'搜索全部历史'}))
    await screen.findByText('SUPPORTS')
    expect(screen.getByRole('img',{name:'事项关系图'})).toBeInTheDocument()
  })
  it('ignores a late response from the previous project', async () => {
    const read=client(); let resolveOld!: (value: unknown) => void
    read.mockImplementation(async (op: string, args: {project?:string}) => {
      if(op==='projects') return projects
      if(op==='status') return {runs:[],active_count:0}
      if(args.project==='p1') return new Promise(resolve => {resolveOld=resolve})
      return {...graph,nodes:[],edges:[]}
    })
    render(<SessionGraphPanel read={read} />)
    await waitFor(()=>expect(resolveOld).toBeDefined())
    fireEvent.change(screen.getByLabelText('项目'),{target:{value:'p2'}})
    await screen.findByText(/暂无关键事项/)
    await act(async()=>resolveOld(graph))
    expect(screen.queryByText('实验结果')).not.toBeInTheDocument()
  })
  it('ignores a retired disabled flag and preserves the selected project', async () => {
    localStorage.setItem('gateway.sessiongraph.v1',JSON.stringify({version:1,enabled:false,project:'p2'}))
    const read=client(); render(<SessionGraphPanel read={read} />)
    await screen.findByText('实验结果')
    expect(screen.getByLabelText('项目')).toHaveValue('p2')
    expect(read).toHaveBeenCalledWith('work_map',{project:'p2',view:'knowledge'})
    expect(JSON.parse(localStorage.getItem('gateway.sessiongraph.v1')!)).toEqual({version:1,project:'p2'})
    const calls=read.mock.calls.length
    await act(async()=>{await new Promise(resolve=>setTimeout(resolve,20))})
    expect(read).toHaveBeenCalledTimes(calls)
    fireEvent.click(screen.getByRole('button',{name:'刷新'}))
    await waitFor(()=>expect(read.mock.calls.length).toBeGreaterThan(calls))
  })
  it('handles a partial task-status failure without hiding the graph', async () => {
    const read=client(); const original=read.getMockImplementation()! as (...args: unknown[])=>Promise<unknown>
    read.mockImplementation((op: string,...args:unknown[])=>op==='status'?Promise.reject(new Error('状态读取失败')):original(op,...args))
    render(<SessionGraphPanel read={read} />)
    await screen.findByText('实验结果'); await screen.findByText(/后台任务状态读取失败/)
  })
})

it('shows task failures, truncation, source mismatches, references and supersession', async()=>{
  const read=client(); const original=read.getMockImplementation()! as (...a:unknown[])=>Promise<unknown>
  read.mockImplementation(async(op:string,...args:unknown[])=>{
    if(op==='work_map')return {...graph,truncated:true,nodes:[{...graph.nodes[0],evidence_status:'missing_or_mismatched'},{id:'ref1',type:'reference',kind:'path',label:'/tmp/report.json',path:'/tmp/report.json'}],edges:[{source:'m1',target:'ref1',kind:'REFERENCES_PATH'}]}
    if(op==='status')return {active_count:1,unreadable_runs:2,runs:[{run_id:'run1',state:'failed',event:'PreCompact',chat_id:'chat1',updated_at:12345,error_code:'timeout',warning_code:'historical_warning'},{run_id:'run2',state:'custom',chat_id:'chat1'}]}
    if(op==='memory')return {memory:{content:'old',source:{quote:'old quote'}},entries:[],superseded_by:[{id:'new',title:'New result',status:'active'}]}
    return original(op,...args)
  })
  render(<SessionGraphPanel read={read}/>);fireEvent.click(await screen.findByText('实验结果'))
  await screen.findByText(/已被后续事项替代/)
  expect(screen.getByText(/原文缺失或引用不匹配/)).toBeInTheDocument()
  expect(screen.getByText(/仅载入最近/)).toBeInTheDocument()
  fireEvent.click(screen.getByText('后台任务与诊断 · 最近 2 次'))
  expect(screen.getByText(/请求超时/)).toBeInTheDocument()
  fireEvent.click(screen.getAllByRole('button',{name:'/tmp/report.json'})[0])
  expect(screen.getByText(/不表示文件当前存在/)).toBeInTheDocument()
})
it('offers empty, unavailable, and no-match states',async()=>{
  const read=client();read.mockResolvedValueOnce({...projects,projects:[...projects.projects,{id:'bad',name:'Broken',state:'unavailable',root:'/bad'}]})
  render(<SessionGraphPanel read={read}/>);await screen.findByText('实验结果')
  read.mockResolvedValueOnce({...graph,nodes:[],edges:[]})
  fireEvent.change(screen.getByLabelText('搜索事项'),{target:{value:'not found'}})
  fireEvent.click(screen.getByRole('button',{name:'搜索全部历史'}))
  await screen.findByText('没有匹配的事项。')
  fireEvent.change(screen.getByLabelText('项目'),{target:{value:'bad'}})
  await screen.findByText(/项目索引不可用/)
})
it('loads the list and handles unavailable original evidence',async()=>{
  const read=client();const original=read.getMockImplementation()! as (...a:unknown[])=>Promise<unknown>
  read.mockImplementation((op:string,...args:unknown[])=>op==='memory'?Promise.reject('来源读取失败'):original(op,...args))
  render(<SessionGraphPanel read={read}/>);await screen.findByText('实验结果')
  fireEvent.click(screen.getByRole('button',{name:'列表'}))
  fireEvent.change(screen.getByLabelText('筛选 chat'),{target:{value:'chat1'}})
  fireEvent.click(screen.getByText('实验结果'))
  await screen.findByText('来源读取失败')
})
it('ignores stale evidence after selecting another node and supports keyboard selection',async()=>{
  const read=client();const original=read.getMockImplementation()! as (...a:unknown[])=>Promise<unknown>;let old!: (v:unknown)=>void
  read.mockImplementation((op:string,args:{id?:string})=>op==='memory'&&args.id==='m1'?new Promise(r=>{old=r}):original(op,args))
  render(<SessionGraphPanel read={read}/>);await screen.findByText('实验结果')
  fireEvent.keyDown(screen.getByRole('button',{name:'实验结果'}),{key:'Enter'})
  await waitFor(()=>expect(old).toBeDefined())
  fireEvent.keyDown(screen.getAllByRole('button',{name:'训练记录'})[0],{key:' '})
  await screen.findByText('/tmp/report.json')
  await act(async()=>old({memory:{source:{quote:'STALE'}},entries:[],superseded_by:[]}))
  expect(screen.queryByText('STALE')).not.toBeInTheDocument()
})
it('survives invalid service envelopes and supports manual refresh',async()=>{
  const read=client();read.mockResolvedValueOnce({projects:null})
  render(<SessionGraphPanel read={read}/>);await screen.findByRole('alert')
  fireEvent.click(screen.getByRole('button',{name:'刷新'}))
  await screen.findByText('实验结果')
})
it('rejects malformed graph payload without hanging',async()=>{
  const read=client();const original=read.getMockImplementation()! as (...a:unknown[])=>Promise<unknown>
  read.mockImplementation((op:string,...args:unknown[])=>op==='work_map'?Promise.resolve({nodes:null,edges:[]}):original(op,...args))
  render(<SessionGraphPanel read={read}/>);await screen.findByText(/图谱格式无效/)
})
it('bounds a large graph and labels omitted nodes',async()=>{
  const read=client();const original=read.getMockImplementation()! as (...a:unknown[])=>Promise<unknown>
  read.mockImplementation((op:string,...args:unknown[])=>op==='work_map'?Promise.resolve({...graph,nodes:Array.from({length:60},(_,i)=>({...graph.nodes[0],id:`m${i}`,label:`Very long node label number ${i}`})),edges:[]}):original(op,...args))
  render(<SessionGraphPanel read={read}/>);await screen.findByText(/当前显示 24 \/ 60/)
})


it('locates a relation target inside the left list without scrolling the page', async()=>{
  render(<SessionGraphPanel read={client()}/>); await screen.findByText('实验结果')
  fireEvent.click(screen.getByRole('button',{name:'列表'}))
  const left=screen.getByRole('region',{name:'图谱与事项列表'}), list=within(left).getByRole('list')
  const target=within(left).getByRole('button',{name:/训练记录/}), scroll=vi.fn()
  list.scrollTo=scroll
  vi.spyOn(list,'getBoundingClientRect').mockReturnValue({top:100,height:400} as DOMRect)
  vi.spyOn(target,'getBoundingClientRect').mockReturnValue({top:900,height:100} as DOMRect)
  fireEvent.click(within(left).getByRole('button',{name:/实验结果/}))
  fireEvent.click(within(screen.getByRole('complementary',{name:'事项详情'})).getByRole('button',{name:'训练记录'}))
  expect(scroll).toHaveBeenCalledWith({top:650,behavior:'auto'})
  expect(target).toHaveAttribute('aria-pressed','true')
  expect(target).toHaveFocus()
  await screen.findByText('/tmp/report.json')
})
it('switches between the compact knowledge layer and process records without deleting evidence',async()=>{
  const read=client(),original=read.getMockImplementation()! as (...a:unknown[])=>Promise<unknown>
  read.mockImplementation((op:string,...args:unknown[])=>op==='work_map'?Promise.resolve({...graph,tiering:{available:true,revision:'tiering-v3',canonical:3,episode:8,record_only:1}}):original(op,...args))
  render(<SessionGraphPanel read={read}/>);await screen.findByText('实验结果')
  expect(screen.getByText(/知识层 3 · 过程记录 8 · 仅回执 1/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:'过程记录'}))
  await waitFor(()=>expect(read).toHaveBeenCalledWith('work_map',{project:'p1',view:'episode'}))
  fireEvent.click(screen.getByRole('button',{name:'全部'}))
  await waitFor(()=>expect(read).toHaveBeenCalledWith('work_map',{project:'p1',view:'all'}))
  expect(screen.queryByText(/已删除/)).not.toBeInTheDocument()
})
it('warns when a fully covered projection still uses old tier rules',async()=>{
  const read=client(),original=read.getMockImplementation()! as (...a:unknown[])=>Promise<unknown>
  read.mockImplementation((op:string,...args:unknown[])=>op==='work_map'?Promise.resolve({...graph,tiering:{available:true,revision:'tiering-v1',current:false,canonical:3,episode:8,record_only:1,covered:12,total:12,complete:true,revisions:{'tiering-v1':12}}}):original(op,...args))
  render(<SessionGraphPanel read={read}/>);await screen.findByText('实验结果')
  expect(screen.getByText(/历史分类规则待更新/)).toBeInTheDocument()
})
it('requests chat filtering from the backend and paginates matching history',async()=>{
  const read=client(),original=read.getMockImplementation()! as (...a:unknown[])=>Promise<unknown>
  read.mockImplementation(async(op:string,args?:{offset?:number;chat?:string})=>op==='work_map'?{...graph,page:{offset:args?.offset||0,limit:1,matched:2,next_offset:args?.offset?null:1,query:'',chat:args?.chat||''}}:original(op,args))
  render(<SessionGraphPanel read={read}/>);await screen.findByText('实验结果')
  fireEvent.change(screen.getByLabelText('筛选 chat'),{target:{value:'chat1'}})
  await waitFor(()=>expect(read).toHaveBeenCalledWith('work_map',{project:'p1',query:'',chat:'chat1',offset:0,view:'knowledge'}))
  await waitFor(()=>expect(screen.getByRole('button',{name:'下一页'})).not.toBeDisabled())
  fireEvent.click(screen.getByRole('button',{name:'下一页'}))
  await waitFor(()=>expect(read).toHaveBeenCalledWith('work_map',{project:'p1',query:'',chat:'chat1',offset:1,view:'knowledge'}))
  await waitFor(()=>expect(screen.getByRole('button',{name:'下一页'})).toBeDisabled())
})
it('opens graph mode for a reference relation absent from the memory list', async()=>{
  const read=client(), original=read.getMockImplementation()! as (...args:unknown[])=>Promise<unknown>
  read.mockImplementation(async(op:string,...args:unknown[])=>op==='work_map'?{...graph,nodes:[...graph.nodes,{id:'r1',type:'reference',kind:'concept',label:'hooks'}],edges:[...graph.edges,{source:'m1',target:'r1',kind:'MENTIONS'}]}:original(op,...args))
  render(<SessionGraphPanel read={read}/>); await screen.findByText('实验结果')
  fireEvent.click(screen.getByRole('button',{name:'列表'}))
  fireEvent.click(screen.getByRole('button',{name:/实验结果/}))
  fireEvent.click(within(screen.getByRole('complementary',{name:'事项详情'})).getByRole('button',{name:'hooks'}))
  expect(screen.getByRole('button',{name:'图谱'})).toHaveAttribute('aria-pressed','true')
  expect(screen.getByRole('button',{name:'hooks'})).toHaveAttribute('aria-pressed','true')
})

it('keeps the graph flush in a full-height left column and moves controls to the right info column', async()=>{
  render(<SessionGraphPanel read={client()}/>); await screen.findByText('实验结果')
  const body=document.querySelector<HTMLElement>('.sg-body')!, layout=document.querySelector<HTMLElement>('.sg-main-layout')!
  const canvas=document.querySelector<HTMLElement>('.sg-canvas')!, info=document.querySelector<HTMLElement>('.sg-info')!
  expect(body).toContainElement(layout)
  expect(layout).toContainElement(canvas)
  expect(layout).toContainElement(info)
  expect(info).toContainElement(screen.getByText('/project/jev'))
  expect(info).toContainElement(document.querySelector<HTMLElement>('.sg-metrics'))
  expect(info).toContainElement(screen.getByLabelText('搜索事项'))
  expect(canvas).not.toContainElement(screen.getByLabelText('搜索事项'))
})

it('opens settings in a modal on demand and restores focus without triggering registration',async()=>{
 const manage=vi.fn().mockResolvedValue({ok:true,model_calls:0,model:'model',hook_registered:true,hook_events:4,hook_enabled:true,mcp_registered:true,message:''})
 render(<SessionGraphPanel read={client()} manage={manage} models={[{value:'model',label:'模型'}]}/>);await screen.findByText('实验结果')
 expect(manage).not.toHaveBeenCalled()
 const trigger=screen.getByRole('button',{name:'设置'})
 fireEvent.click(trigger)
 const dialog=screen.getByRole('dialog',{name:'设置'})
 expect(dialog).toHaveAttribute('open')
 expect(dialog).toContainElement(await screen.findByRole('region',{name:'项目记忆设置'}))
 expect(document.querySelector('.sg-info')).not.toContainElement(dialog)
 expect(screen.getByRole('button',{name:'关闭设置'})).toHaveFocus()
 await waitFor(()=>expect(manage).toHaveBeenCalledWith({action:'status',project:'p1'}))
 expect(manage).toHaveBeenCalledTimes(1)
 fireEvent.click(within(dialog).getByRole('button',{name:'关闭设置'}))
 expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
 expect(trigger).toHaveFocus()
 expect(trigger).toHaveAttribute('aria-expanded','false')
 fireEvent.click(trigger)
 const reopened=screen.getByRole('dialog',{name:'设置'})
 fireEvent(reopened,new Event('cancel',{cancelable:true}))
 expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
 expect(trigger).toHaveFocus()
})

it('runs scoped maintenance inside the modal and keeps clicks in its content open',async()=>{
 const read=client()
 const before={database_bytes:1048576,wal_bytes:0,reclaimable_bytes:0,logical_bytes:1048576}
 const manage=vi.fn(async({action})=>({ok:true,model_calls:0,maintenance:action==='storage_preview'?{before}:{before,after:before,removed:2}})) as unknown as ReturnType<typeof vi.fn>&SessionGraphManage
 render(<SessionGraphPanel read={read} manage={manage}/>);await screen.findByText('实验结果')
 const trigger=screen.getByRole('button',{name:'存储维护'})
 fireEvent.click(trigger)
 const dialog=screen.getByRole('dialog',{name:'存储维护'})
 expect(document.querySelector('.sg-info')).not.toContainElement(dialog)
 await within(dialog).findByText('1.0 MB')
 expect(manage).toHaveBeenCalledExactlyOnceWith({action:'storage_preview',project:'p1'})
 fireEvent.click(within(dialog).getByText('无损压缩'))
 expect(dialog).toBeInTheDocument()
 const readsBefore=read.mock.calls.filter(([op])=>op==='projects').length
 fireEvent.click(within(dialog).getByRole('button',{name:'清理已恢复告警'}))
 expect(await within(dialog).findByRole('status')).toHaveTextContent('已清理 2 条')
 expect(manage).toHaveBeenLastCalledWith({action:'clean_notices',project:'p1'})
 await waitFor(()=>expect(read.mock.calls.filter(([op])=>op==='projects').length).toBeGreaterThan(readsBefore))
 fireEvent.click(dialog)
 expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
 expect(trigger).toHaveFocus()
})


it('distinguishes blocked extraction, uncertain candidates, and historical failures', async () => {
  let blocked=true
  const read=vi.fn(async(op:string)=>{
    if(op==='projects')return {...projects}
    if(op==='work_map')return {...graph,graph_v2:{enabled:true,initialized:true,nodes:5,relations:0,jobs:blocked?{complete:4,pending:1}:{complete:5},blocked_jobs:blocked?[{id:'j1',error_code:'graph_validation',reason:'graph_schema:output:fields:missing=relations:extra_count=0'}]:[],unresolved:{identity_pending:8,unanchored_identity:1}}}
    if(op==='status')return {runs:[{run_id:'r1',chat_id:'chat1',state:'failed',event:'PostCompact',error_code:'graph_validation',graph_v2:{failure_reason:'graph_schema:invalid_node_ids'}}],active_count:0}
    return {}
  }) as unknown as SessionGraphRead
  render(<SessionGraphPanel read={read}/>);await screen.findByText('实验结果')
  expect(screen.getByText(/未完成抽取批次：1/)).toBeInTheDocument()
  expect(screen.getByText(/抽取受阻：图谱抽取结果未通过校验/)).toHaveTextContent('缺少 relations')
  expect(screen.getByText(/关系端点尚未确认/).closest('li')).toHaveTextContent('8')
  expect(screen.getByText(/标识或所属范围没有原文依据/)).toBeInTheDocument()
  expect(screen.getByText(/节点标识无效或重复/)).toBeInTheDocument()
  blocked=false;fireEvent.click(screen.getByRole('button',{name:'刷新'}))
  await screen.findByText(/未完成抽取批次：0/)
  expect(screen.queryByText(/抽取受阻：/)).not.toBeInTheDocument()
  expect(screen.getByRole('img',{name:'失败'})).toBeInTheDocument()
  expect(screen.getByText(/恢复后仍保留原失败记录/)).toBeInTheDocument()
})

it('shows cumulative calls and deferred source coverage instead of false success',async()=>{
  const read=client();const original=read.getMockImplementation()! as (...a:unknown[])=>Promise<unknown>
  read.mockImplementation((op:string,...args:unknown[])=>op==='status'?Promise.resolve({runs:[
    {run_id:'pending',chat_id:'chat1',state:'pending',event:'PostCompact',reported_model_calls:17,error_code:'graph_call_budget_pending',historical_failure_codes:['jev_tls_handshake'],graph_v2:{source_remaining:true,deepseek_calls:7,jev_calls:10}},
    {run_id:'done',chat_id:'chat1',state:'succeeded',reported_model_calls:0,graph_v2:{source_remaining:false,deepseek_calls:0,jev_calls:0}}
  ],active_count:0}):original(op,...args))
  render(<SessionGraphPanel read={read}/>);
  await screen.findByRole('img',{name:'待续处理'})
  expect(screen.getByText(/累计调用 17 次/).closest('dd')).toHaveTextContent('抽取 7 / 判断 0 / 历史 Jev 10')
  expect(screen.getByText('仍有输入或批次待处理')).toBeInTheDocument()
  expect(screen.getByText('本次输入已处理')).toBeInTheDocument()
  expect(screen.getByText(/旧版本的分轮记录/)).not.toHaveClass('sg-error')
  expect(screen.getAllByText(/历史记录未保存调用上限/)).toHaveLength(2)
  expect(screen.getByText(/历史重试原因：Jev 安全连接握手失败/)).toBeInTheDocument()
  expect(read.mock.calls.every(([op])=>['projects','work_map','status'].includes(op))).toBe(true)
})


it('shows input filtering and import-only/coalesced compact events',async()=>{
  const read=client();const base=read.getMockImplementation()! as (op:string,args:unknown)=>Promise<unknown>
  read.mockImplementation(async(op:string,args:unknown)=>op==='status'?{runs:[
    {run_id:'pre',chat_id:'chat1',event:'PreCompact',state:'prepared',reported_model_calls:0},
    {run_id:'duplicate',chat_id:'chat1',event:'PostCompact',state:'coalesced',coalesced_run_id:'original-run',reported_model_calls:0},
    {run_id:'original-run',chat_id:'chat1',event:'PostCompact',state:'succeeded',graph_v2:{input_source_chars:24000,input_selected_chars:2000,input_excluded_chars:22000}}
  ],model_calls:0}:base(op,args))
  render(<SessionGraphPanel read={read}/>);await screen.findByText('实验结果')
  expect(screen.getByRole('img',{name:'已导入，待压缩后抽取'})).toBeInTheDocument()
  expect(screen.getByRole('img',{name:'已合并重复事件'})).toBeInTheDocument()
  expect(screen.getByText(/原文 24,000 字符/)).toHaveTextContent('规则排除 22,000 字符')
  expect(screen.getByText(/本次未调用模型/)).toHaveTextContent('original-run')
})


it('distinguishes budget deferral, real errors and incomplete historical statistics',async()=>{
  const read=client();const base=read.getMockImplementation()! as (...args:unknown[])=>Promise<unknown>
  read.mockImplementation((op:string,...args:unknown[])=>op==='status'?Promise.resolve({runs:[
    {run_id:'budget',chat_id:'chat1',state:'pending',reported_model_calls:2,model_call_limit:2,saved_count:2,saved_relation_count:0,error_code:'graph_call_budget_pending',historical_failure_codes:['graph_call_budget_pending'],graph_v2:{source_remaining:true,input_source_chars:24445,input_selected_chars:24000,input_excluded_chars:448}},
    {run_id:'old',chat_id:'chat1',state:'pending',reported_model_calls:42,statistics_incomplete:true,error_code:'runtime_upgrade_pending'},
    {run_id:'bad',chat_id:'chat1',state:'failed',error_code:'graph_validation',failure_codes:['graph_call_budget_pending','graph_validation'],graph_v2:{failure_reason:'invalid_node_ids'}}
  ],active_count:0}):base(op,...args))
  render(<SessionGraphPanel read={read}/>);await screen.findByText(/本次调用额度 2\/2 次/)
  expect(screen.getByText('本次已保存 2 个节点 / 0 条关系')).toBeInTheDocument()
  expect(screen.getAllByText(/旧版本的分轮记录/)[0]).not.toHaveClass('sg-error')
  expect(screen.getAllByText(/旧版本的分轮记录/)[1]).toHaveClass('sg-error')
  expect(screen.getByText(/图谱抽取结果未通过校验 · 节点标识无效或重复/)).toHaveClass('sg-error')
  expect(screen.getByText(/等待新版运行时继续/)).not.toHaveClass('sg-error')
  expect(screen.getByText(/调用统计不完整/)).toBeInTheDocument()
  expect(screen.getByText(/已记录调用 42 次/)).toBeInTheDocument()
  expect(screen.getByText(/组装后输入 24,000/)).toHaveTextContent('不能用原文减去排除量直接计算')
  expect(screen.queryByText(/历史重试原因：旧版本的分轮记录/)).not.toBeInTheDocument()
  expect(read.mock.calls.every(([op])=>['projects','work_map','status'].includes(op))).toBe(true)
})


it.each(['blocked_jobs','deferred_jobs'])('renders object-graph budget pauses without failure styling from %s',async(bucket)=>{
  const read=client();const base=read.getMockImplementation()! as (...args:unknown[])=>Promise<unknown>
  read.mockImplementation((op:string,...args:unknown[])=>op==='work_map'?Promise.resolve({...graph,graph_v2:{
    enabled:true,initialized:true,nodes:342,relations:34,jobs:{complete:5,pending:2},
    blocked_jobs:[{id:'bad',state:'pending',error_code:'graph_validation',reason:'invalid_endpoint'},
      ...(bucket==='blocked_jobs'?[{id:'pause',error_code:'graph_call_budget_pending',reason:'graph_call_budget_pending'}]:[])],
    deferred_jobs:bucket==='deferred_jobs'?[{id:'pause',state:'pending',error_code:'graph_call_budget_pending',reason:'graph_call_budget_pending'}]:[],
    unresolved:{ambiguous_reference:9}
  }}):base(op,...args))
  render(<SessionGraphPanel read={read}/>);await screen.findByText(/对象图谱 v2 · 342/)
  expect(screen.getByText(/等待续处理：/)).not.toHaveClass('sg-error')
  expect(screen.getByText(/等待续处理：/)).toHaveTextContent('剩余内容由后台继续处理')
  expect(screen.queryByText(/抽取受阻：旧版本的分轮记录/)).not.toBeInTheDocument()
  expect(screen.getByText(/抽取受阻：图谱抽取结果未通过校验/)).toHaveClass('sg-error')
  expect(screen.getByText(/未完成抽取批次：2/)).toBeInTheDocument()
  expect(screen.getByText(/指代对象不明确/).closest('li')).toHaveTextContent('9')
  expect(read.mock.calls.every(([op])=>['projects','work_map','status'].includes(op))).toBe(true)
})


it('lets readers reveal runs and technical fields without additional requests',async()=>{
  const read=client();const base=read.getMockImplementation()! as (...args:unknown[])=>Promise<unknown>
  read.mockImplementation((op:string,...args:unknown[])=>op==='status'?Promise.resolve({runs:[
    {run_id:'latest-run',chat_id:'chat1',event:'PostCompact',state:'pending',reported_model_calls:2,model_call_limit:8,saved_count:4,saved_relation_count:1,error_code:'graph_call_budget_pending',graph_v2:{source_remaining:true}},
    {run_id:'older-run',chat_id:'chat1',event:'PreCompact',state:'prepared',reported_model_calls:0}
  ],active_count:0}):base(op,...args))
  render(<SessionGraphPanel read={read}/>);await screen.findByText('实验结果')
  const calls=read.mock.calls.length
  const runFacts=screen.getByText('本次已保存 4 个节点 / 1 条关系')
  expect(runFacts).not.toBeVisible()
  fireEvent.click(screen.getByText('后台任务与诊断 · 最近 2 次'))
  expect(runFacts).not.toBeVisible()
  fireEvent.click(screen.getByText('压缩后抽取'))
  expect(runFacts).toBeVisible()
  expect(screen.getByText('latest-run')).not.toBeVisible()
  expect(screen.getByText('older-run')).not.toBeVisible()
  fireEvent.click(screen.getAllByText('技术详情 · 输入统计与任务标识')[0])
  expect(screen.getByText('latest-run')).toBeVisible()
  fireEvent.click(screen.getByText('压缩前导入'))
  expect(screen.getByText('此记录未提供产出统计')).toBeVisible()
  expect(screen.getByText('older-run')).not.toBeVisible()
  expect(read).toHaveBeenCalledTimes(calls)
})


it('shows automatic continuation time without launching work from reads',async()=>{
  const read=client();const base=read.getMockImplementation()! as (...args:unknown[])=>Promise<unknown>
  read.mockImplementation((op:string,...args:unknown[])=>op==='status'?Promise.resolve({runs:[],active_count:0,continuations:[{chat:'chat1',state:'cooldown',due:1791975600,run_id:'r1'}]}):base(op,...args))
  render(<SessionGraphPanel read={read}/>);await screen.findByText(/请求失败，等待重试 · .* 自动继续/)
  expect(read.mock.calls.every(([op])=>['projects','work_map','status'].includes(op))).toBe(true)
})

it('labels each continuation with its chat and distinguishes recovered historical runs', async () => {
  const read=client();const original=read.getMockImplementation()! as (...args:unknown[])=>Promise<unknown>
  read.mockImplementation(async (op,...args)=>op==='status'?({runs:[{
    run_id:'old',chat_id:'chat-AAAAAA',state:'failed',event:'PostCompact',error_code:'jev_transport',batches_recovered:true,
    saved_count:2,saved_relation_count:0,reported_model_calls:2,updated_at:1792044124,
  }],active_count:1,continuations:[
    {chat:'chat-AAAAAA',state:'running',due:0,run_id:'new'},
    {chat:'chat-BBBBBB',state:'blocked',due:0,run_id:'other',error_code:'graph_validation'},
  ]}):original(op,...args))
  render(<SessionGraphPanel read={read}/>);
  await screen.findByText('实验结果');
  expect(screen.getByText('chat AAAAAA ·').parentElement).toHaveTextContent('自动续跑中');
  expect(screen.getByText('chat BBBBBB ·').parentElement).toHaveTextContent('自动续跑受阻');
  expect(screen.getByLabelText('该轮批次已恢复')).toHaveClass('sg-dot-success');
  expect(screen.getByText(/该轮批次已恢复；以下保留历史记录/)).toBeInTheDocument();
})


it('shows continuous execution without inventing a quota and explains project exclusions',async()=>{
  const read=client();const original=read.getMockImplementation()! as (...a:unknown[])=>Promise<unknown>
  read.mockImplementation((op:string,...args:unknown[])=>op==='status'?Promise.resolve({runs:[
    {run_id:'continuous',chat_id:'chat1',state:'extracting',reported_model_calls:27,model_call_limit:null,scheduling_policy:'continuous',warning_code:'import_cross_project_records_excluded',graph_v2:{source_remaining:true}}
  ],active_count:1}):original(op,...args))
  render(<SessionGraphPanel read={read}/>);
  await screen.findByText('持续处理，无调用次数上限')
  expect(screen.getByText('已过滤其他项目的记录。')).toBeInTheDocument()
  expect(screen.queryByText(/历史记录未保存调用上限/)).not.toBeInTheDocument()
  expect(screen.queryByText('import_cross_project_records_excluded')).not.toBeInTheDocument()
})

it('keeps recovered errors in history and shows pending work separately',async()=>{
  const read=client();const base=read.getMockImplementation()! as (...a:unknown[])=>Promise<unknown>
  read.mockImplementation((op:string,...args:unknown[])=>op==='status'?Promise.resolve({runs:[
    {run_id:'old',chat_id:'chat1',state:'failed',batches_recovered:true,error_code:'jev_http_451'},
    {run_id:'new',chat_id:'chat1',state:'pending',attempt_outcome:'succeeded',failure_codes:[],graph_v2:{source_remaining:true,pending_jobs:5,ready_jobs:2,retry_wait_jobs:2,blocked_jobs:1}}
  ],active_count:0}):base(op,...args))
  render(<SessionGraphPanel read={read}/>);
  expect(await screen.findByText(/历史错误（相关批次已恢复）：Jev 服务拒绝了请求/)).not.toHaveClass('sg-error')
  expect(screen.getByText('待处理 2 · 等待重试 2 · 受阻 1')).toBeInTheDocument()
  expect(screen.getByLabelText('本轮执行成功，仍有待处理内容')).toHaveClass('sg-dot-success')
  expect(document.querySelectorAll('.sg-run-content .sg-error')).toHaveLength(0)
})

it('keeps a current failure red even when other batches succeeded',async()=>{
  const read=client();const base=read.getMockImplementation()! as (...a:unknown[])=>Promise<unknown>
  read.mockImplementation((op:string,...args:unknown[])=>op==='status'?Promise.resolve({runs:[
    {run_id:'failed',chat_id:'chat1',state:'failed',attempt_outcome:'failed',error_code:'jev_http_451',saved_count:11,saved_relation_count:2}
  ],active_count:0}):base(op,...args))
  render(<SessionGraphPanel read={read}/>);
  expect((await screen.findByText(/Jev 服务拒绝了请求/)).closest('p')).toHaveClass('sg-error')
  expect(document.querySelector('.sg-run-row .sg-dot-error')).toBeTruthy()
})


it('keeps an ongoing run neutral after an earlier successful batch',async()=>{
  const read=client();const base=read.getMockImplementation()! as (...a:unknown[])=>Promise<unknown>
  read.mockImplementation((op:string,...args:unknown[])=>op==='status'?Promise.resolve({runs:[
    {run_id:'active',chat_id:'chat1',state:'extracting',attempt_outcome:'succeeded',failure_codes:[],reported_model_calls:3}
  ],active_count:1}):base(op,...args))
  render(<SessionGraphPanel read={read}/>);
  await screen.findByText('实验结果');
  expect(document.querySelector('.sg-run-row .sg-dot-neutral')).toBeTruthy()
  expect(document.querySelector('.sg-run-row .sg-dot-success')).toBeNull()
})

it('shows partial historical classification as normal background progress',async()=>{
  const read=client(),original=read.getMockImplementation()! as (...a:unknown[])=>Promise<unknown>
  read.mockImplementation((op:string,...args:unknown[])=>op==='work_map'?Promise.resolve({...graph,tiering:{available:true,revision:'tiering-v3',current:false,canonical:44,episode:19,record_only:0,covered:63,total:205,complete:false}}):original(op,...args))
  const manage=vi.fn()
  render(<SessionGraphPanel read={read} manage={manage}/>);await screen.findByText('实验结果')
  const progress=screen.getByText(/历史分类进度/)
  expect(progress).toHaveTextContent('63 / 205')
  expect(progress).toHaveTextContent('后台空闲时分批补齐')
  expect(progress).not.toHaveClass('sg-error')
  expect(manage).not.toHaveBeenCalled()
})
