import { listen } from '@tauri-apps/api/event'
import { SessionGraphTaskCleanup } from './SessionGraphTaskCleanup'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { readPreferences, readSessionGraph, savePreferences, type Catalog, type GraphNode, type MapView, type MemoryDetail, type SessionGraphRead, type UpdateStatus, type Run, type GraphJobDiagnostic, type WorkMap } from '../sessionGraph'
import './SessionGraphPanel.css'
import { SessionGraphCanvas } from './SessionGraphCanvas'
import { SessionGraphSettings } from './SessionGraphSettings'
import { SessionGraphMaintenance } from './SessionGraphMaintenance'
import { SessionGraphDialog } from './SessionGraphDialog'
import { ProjectChats } from './ProjectChats'
import { Search, ChevronLeft, ChevronRight } from 'lucide-react'
import type { SessionGraphManage, SessionGraphModelOption } from '../sessionGraph'
import { revisionValidityLabel } from '../sessionGraph'
import { deferredCodes, errorDescription, errorLabel, reasonLabel, stageLabel, traceReasonLabel, warningLabel } from '../diagnostics'

const statistic=(value:unknown)=>typeof value==='number'&&Number.isFinite(value)?value.toLocaleString():'—'
const errorText=(e:unknown)=>e instanceof Error?e.message:String(e)
const labels:Record<string,string>={Activity:'工作事项',Execution:'执行实例',Artifact:'产物',Source:'资料',Claim:'结论',Decision:'决策',goal:'目标',decision:'决策',finding:'发现',artifact:'产物',progress:'进度',next_step:'下一步',constraint:'约束',active:'有效',completed:'完成',in_progress:'进行中',proposed:'提议',blocked:'受阻',succeeded:'成功',failed:'失败',stalled:'无终态',queued:'排队',importing:'导入',extracting:'抽取',retry_wait:'等待重试',pending:'待续处理',prepared:'已导入，待压缩后抽取',coalesced:'已合并重复事件'}
function GraphJobDiagnostics({job}:{job:GraphJobDiagnostic}) {
  // Old runtimes may put normal deferrals in blocked_jobs.
  const deferred=job.state==='retry_wait'||((!job.state||job.state==='pending')&&deferredCodes.has(job.error_code))
  return <p className={`sg-diagnostic-notice ${deferred?'sg-muted':'sg-error'}`}>
    {job.chat&&<small title={job.chat}>chat {job.chat.slice(-6)} · </small>}{job.stage&&<small>{stageLabel(job.stage)} · </small>}
    {job.error_code==='resource_circuit_open'?<>{errorDescription(job.error_code)}</>:job.state==='retry_wait'?<>请求失败，已安排重试 · {errorLabel(job.error_code)}</>:deferred?<>等待续处理：{errorDescription(job.error_code)}</>:<>抽取受阻：{errorLabel(job.error_code)}{job.reason&&job.reason!==job.error_code&&<> · {reasonLabel(job.reason)}</>}</>}
  </p>
}
const eventLabels:Record<string,string>={AutoContinue:'自动续跑',PreCompact:'压缩前导入',PostCompact:'压缩后抽取',SessionStart:'会话恢复',Stop:'会话结束',manual:'手动触发'}
function RunDiagnostics({run:r}:{run:Run}) {
  const codes=Array.from(new Set([r.error_code,...(r.failure_codes||[])].filter((code):code is string=>!!code)))
  const history=Array.from(new Set([...(r.historical_failure_codes||[]),...(r.batches_recovered?codes:[])])).filter(code=>!deferredCodes.has(code))
  const circuitWait=codes.length>0&&codes.every(code=>code==='resource_circuit_open')
  const roundSuccess=r.attempt_outcome==='succeeded'&&['succeeded','pending'].includes(r.state)&&!codes.length
  const statusLabel=r.batches_recovered?'该轮批次已恢复':circuitWait?'等待连接恢复':roundSuccess&&r.state==='pending'?'本轮执行成功，仍有待处理内容':label(r.state)
  const tone=circuitWait?'neutral':r.batches_recovered||roundSuccess?'success':r.state==='failed'||codes.some(code=>!(r.state==='pending'&&deferredCodes.has(code)))?'error':['succeeded','prepared','coalesced'].includes(r.state)?'success':'neutral'
  return <li className="sg-run-card">
    <details className="sg-run-disclosure">
      <summary className="sg-run-row">
        <time title={r.updated_at?new Date(r.updated_at*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'}):'时间未知'}>{r.updated_at?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(r.updated_at*1000)):'—'}</time>
        <span className="sg-run-operation" title={eventLabels[r.event||'']||'触发方式未记录'}>{eventLabels[r.event||'']||'其他触发方式'} <small title={r.chat_id}>· {r.chat_id.slice(-6)}</small></span>
        <span className={`sg-status-dot sg-dot-${tone}`} role="img" aria-label={statusLabel} title={statusLabel}/>
        <span className="sg-run-counts" title="本次保存：节点 / 关系">{r.saved_count??'—'}/{r.saved_relation_count??'—'}</span>
        <span className="sg-run-calls" title={`${r.statistics_incomplete?'已记录调用（统计不完整）':'模型调用'}${typeof r.model_call_limit==='number'?`，本次上限 ${r.model_call_limit}`:''}`}>{r.statistics_incomplete?'≥':''}{r.reported_model_calls??'—'}</span>
      </summary>
      <div className="sg-run-content">
        {r.batches_recovered&&<p className="sg-muted">该轮批次已恢复；以下保留历史记录，不代表全部积压已完成。</p>}
        <dl className="sg-run-facts">
          <div><dt>保存产出</dt><dd>{typeof r.saved_count==='number'?`本次已保存 ${r.saved_count} 个节点 / ${r.saved_relation_count??0} 条关系`:'此记录未提供产出统计'}</dd></div>
          <div><dt>输入覆盖</dt><dd>{r.graph_v2?.source_remaining===undefined?'此记录未提供覆盖状态':r.graph_v2.source_remaining?'仍有输入或批次待处理':'本次输入已处理'}</dd></div>
          {typeof r.reported_model_calls==='number'&&<div><dt>模型调用</dt><dd><span>{r.statistics_incomplete?'已记录调用':'累计调用'} {r.reported_model_calls} 次</span><small>{typeof r.model_call_limit==='number'?<>本次调用额度 {r.reported_model_calls}/{r.model_call_limit} 次（含重试）</>:r.scheduling_policy==='continuous'?<>持续处理，无调用次数上限</>:<>历史记录未保存调用上限</>}</small>{r.graph_v2?.deepseek_calls!==undefined&&<small>抽取 {r.graph_v2.deepseek_calls-(r.graph_v2.judge_calls||0)} / 判断 {r.graph_v2.judge_calls||0}{Boolean(r.graph_v2.jev_calls)&&<> / 历史 Jev {r.graph_v2.jev_calls}</>}</small>}</dd></div>}
          {r.graph_v2?.pending_jobs!==undefined&&<div><dt>本轮结束时积压</dt><dd>待处理 {r.graph_v2.ready_jobs??Math.max(0,r.graph_v2.pending_jobs-(r.graph_v2.retry_wait_jobs??0)-(r.graph_v2.blocked_jobs??0))} · 等待重试 {r.graph_v2.retry_wait_jobs??0} · 受阻 {r.graph_v2.blocked_jobs??0}</dd></div>}
        </dl>
        {r.statistics_incomplete&&<p className="sg-diagnostic-notice sg-muted">调用统计不完整，以上仅为已记录次数，不能作为最终总量。</p>}
        {!r.batches_recovered&&codes.map(code=><p key={code} className={`sg-diagnostic-notice ${circuitWait||(r.state==='pending'&&deferredCodes.has(code))?'sg-muted':'sg-error'}`}>
          {errorLabel(code)}{!deferredCodes.has(code)&&r.graph_v2?.failure_reason&&<> · {reasonLabel(r.graph_v2.failure_reason)}</>}
        </p>)}
        {r.warning_code&&<p className="sg-diagnostic-notice sg-muted">{warningLabel(r.warning_code)}</p>}
        <details className="sg-technical"><summary>技术详情 · 输入统计与任务标识</summary>
          {r.graph_v2?.input_source_chars!==undefined&&<p>
            原文 {statistic(r.graph_v2.input_source_chars)} 字符 · 规则排除 {statistic(r.graph_v2.input_excluded_chars)} 字符 · 组装后输入 {statistic(r.graph_v2.input_selected_chars)} 字符。
            输入含上下文与片段分隔，不含提示词及重试，不能用原文减去排除量直接计算；完整原文仍可追溯。
          </p>}
          {r.input_stats&&<p>本轮扫描原文 {statistic(r.input_stats.source_chars)} 字符 · 规则排除 {statistic(r.input_stats.excluded_chars)} 字符 · 保留 {statistic(r.input_stats.selected_chars)} 字符（不含提示词与重试）。</p>}
          {r.request_traces?.map(trace=><div key={trace.id} className="sg-diagnostic-notice"><p><code>{trace.id}</code> · {trace.model}</p><p>输入 {trace.usage?.input_tokens??'—'} / 输出 {trace.usage?.output_tokens??'—'} token · 输出上限 {trace.max_output_tokens}{trace.usage?.output_tokens_details?.reasoning_tokens!==undefined&&<> · 思考 {trace.usage.output_tokens_details.reasoning_tokens}</>}</p><p>{trace.transport&&<>连接：{trace.transport.mode==='https_connect_proxy'?'代理':'直连'} {trace.transport.connect_host}{trace.transport.connect_port?`:${trace.transport.connect_port}`:''} · </>}终止原因：{traceReasonLabel(trace.incomplete_details?.reason||trace.finish_reason)} · 请求日志：<code>{trace.path}</code></p></div>)}
          <dl className="sg-run-facts"><div><dt>任务 ID</dt><dd><code>{r.run_id}</code></dd></div><div><dt>Chat ID</dt><dd><code>{r.chat_id||'未记录'}</code></dd></div><div><dt>触发事件</dt><dd><code>{r.event||'未记录'}</code></dd></div></dl>
          {r.coalesced_run_id&&<p>本次未调用模型，关联任务：<code>{r.coalesced_run_id}</code></p>}
          {!!history.length&&<p className="sg-muted">{r.batches_recovered?'历史错误（相关批次已恢复）：':'历史重试原因：'}{history.map(errorLabel).join('、')}</p>}
          {!!r.backlog_failures?.length&&<p className="sg-muted">积压批次最近错误（非本轮请求失败）：{Array.from(new Set(r.backlog_failures.map(item=>errorLabel(item.error_code)))).join('、')}</p>}
        </details>
      </div>
    </details>
  </li>
}
const label=(s?:string)=>s?(labels[s]||s):'未知'

export function SessionGraphPanel({read=readSessionGraph,models=[],manage}:{read?:SessionGraphRead;models?:SessionGraphModelOption[];manage?:SessionGraphManage}){
  const [dialog,setDialog]=useState<'settings'|'maintenance'|null>(null)
  const [preferences]=useState(readPreferences),[catalog,setCatalog]=useState<Catalog|null>(null),[project,setProject]=useState(preferences.project),[refresh,setRefresh]=useState(0)
  const [map,setMap]=useState<WorkMap|null>(null),[status,setStatus]=useState<UpdateStatus|null>(null),[error,setError]=useState(''),[statusError,setStatusError]=useState(''),[loading,setLoading]=useState(false)
  const [query,setQuery]=useState(''),[chat,setChat]=useState(''),[mode,setMode]=useState<'list'|'graph'>('graph'),[view,setView]=useState<MapView>('knowledge'),[selected,setSelected]=useState<GraphNode|null>(null),[detail,setDetail]=useState<MemoryDetail|null>(null),[detailError,setDetailError]=useState(''),[detailLoading,setDetailLoading]=useState(false)
  const detailEpoch=useRef(0),mapEpoch=useRef(0)
  const [locate,setLocate]=useState<{id:string;sequence:number}|null>(null),[navigationNote,setNavigationNote]=useState('')
  const locateSequence=useRef(0),listRef=useRef<HTMLUListElement>(null),detailRef=useRef<HTMLElement>(null)
  const listButtons=useRef(new Map<string,HTMLButtonElement>())
  useLayoutEffect(()=>{
    if(mode!=='list'||!locate)return
    const list=listRef.current,button=listButtons.current.get(locate.id)
    if(!list||!button)return
    // Scroll only the pane; scrollIntoView can also move the outer document.
    const box=list.getBoundingClientRect(),item=button.getBoundingClientRect()
    const top=Math.max(0,list.scrollTop+item.top-box.top-(box.height-item.height)/2)
    if(list.scrollTo)list.scrollTo({top,behavior:'auto'});else list.scrollTop=top
    button.focus({preventScroll:true})
  },[locate,mode])
  useLayoutEffect(()=>{
    const detail=detailRef.current,info=detail?.parentElement
    if(selected&&detail&&info){
      const top=info.scrollTop+detail.getBoundingClientRect().top-info.getBoundingClientRect().top
      if(info.scrollTo)info.scrollTo({top:Math.max(0,top-12),behavior:'auto'})
    }
  },[selected?.id])
  useEffect(()=>{
    if (!(window as unknown as {__TAURI_INTERNALS__?:unknown}).__TAURI_INTERNALS__) return
    let disposed=false, unlisten:(()=>void)|undefined
    void listen('sessiongraph-tiers-updated',()=>setRefresh(n=>n+1))
      .then(stop=>{if(disposed)stop();else unlisten=stop}).catch(()=>{})
    return ()=>{disposed=true;unlisten?.()}
  },[])
  useEffect(()=>savePreferences({version:1,project}),[project])
  useEffect(()=>{
    let current=true;setLoading(true);setError('')
    read<Catalog>('projects').then(c=>{
      if(!current)return
      if(!Array.isArray(c.projects)||typeof c.default_project!=='string')throw new Error('项目目录格式无效')
      setCatalog(c);setProject(p=>c.projects.some(item=>item.id===p)?p:c.default_project)
    }).catch(e=>{if(current){setError(errorText(e));setCatalog(null);setMap(null);setStatus(null)}}).finally(()=>{if(current)setLoading(false)})
    return ()=>{current=false}
  },[read,refresh])
  useEffect(()=>{
    let current=true;mapEpoch.current++;detailEpoch.current++;setLocate(null);setNavigationNote('');setSelected(null);setDetail(null);setDetailError('');setDetailLoading(false);setMap(null);setStatus(null);setStatusError('');setChat('');setQuery('')
    if(!catalog||catalog.projects.find(p=>p.id===project)?.state!=='ready')return
    setLoading(true);setError('')
    // The two reads fail independently. Neither schedules extraction or retries.
    const graph=read<WorkMap>('work_map',{project,view}).then(data=>{if(current){if(!Array.isArray(data.nodes)||!Array.isArray(data.edges))throw new Error('图谱格式无效');setMap(data)}}).catch(e=>{if(current)setError(errorText(e))})
    const tasks=read<UpdateStatus>('status',{project}).then(data=>{if(current)setStatus(data)}).catch(()=>{if(current)setStatusError('后台任务状态读取失败；图谱仍可查看。')})
    Promise.all([graph,tasks]).finally(()=>{if(current)setLoading(false)})
    return ()=>{current=false;mapEpoch.current++}
  },[read,catalog,project,view])
  useEffect(()=>()=>{detailEpoch.current++},[])
  const filtered=map
  async function searchMap(nextQuery=query,nextChat=chat,offset=0){
    const epoch=++mapEpoch.current
    setLoading(true);setError('');setSelected(null);setDetail(null);detailEpoch.current++
    try {
      const data=await read<WorkMap>('work_map',{project,query:nextQuery,chat:nextChat,offset,view})
      if(epoch===mapEpoch.current){
        if(!Array.isArray(data.nodes)||!Array.isArray(data.edges))throw new Error('图谱格式无效')
        setMap(data)
      }
    } catch(e){if(epoch===mapEpoch.current)setError(errorText(e))}
    finally {if(epoch===mapEpoch.current)setLoading(false)}
  }
  function select(node:GraphNode){
    setLocate(null);setSelected(node);setDetail(null);setDetailError('');const epoch=++detailEpoch.current
    if(node.type!=='memory'){setDetailLoading(false);return}
    setDetailLoading(true)
    read<MemoryDetail>('memory',{project,id:node.id}).then(d=>{if(epoch===detailEpoch.current)setDetail(d)}).catch(e=>{if(epoch===detailEpoch.current)setDetailError(errorText(e))}).finally(()=>{if(epoch===detailEpoch.current)setDetailLoading(false)})
  }
  async function moreEvidence(){
    const offset=detail?.graph_v2?.evidence_page?.next_offset
    if(!selected||offset==null||detailLoading)return
    const epoch=detailEpoch.current
    setDetailLoading(true);setDetailError('')
    try {
      const next=await read<MemoryDetail>('memory',{project,id:selected.id,offset})
      if(epoch===detailEpoch.current)setDetail(old=>old?.graph_v2&&next.graph_v2?{...old,graph_v2:{...next.graph_v2,assertions:[...old.graph_v2.assertions,...next.graph_v2.assertions]}}:old)
    } catch(e){if(epoch===detailEpoch.current)setDetailError(errorText(e))}
    finally {if(epoch===detailEpoch.current)setDetailLoading(false)}
  }
  function navigateRelation(node:GraphNode){
    const hidden=!filtered?.nodes.some(n=>n.id===node.id)
    if(hidden){setQuery('');setChat('')}
    setNavigationNote(hidden?'已清除搜索与 chat 筛选，定位关联事项。':'')
    if(node.type!=='memory')setMode('graph')
    select(node)
    setLocate({id:node.id,sequence:++locateSequence.current})
  }
  const currentProject=catalog?.projects.find(p=>p.id===project),memories=filtered?.nodes.filter(n=>n.type==='memory')||[]
  return <section className="sg-panel" aria-label="项目记忆">
    <header className="sg-header"><div><h2>项目记忆</h2><p>SessionGraph · 跨 chat 的关键事项与原文证据</p></div><div className="sg-actions"><label>项目 <select aria-label="项目" disabled={!catalog} value={project} onChange={e=>setProject(e.target.value)}>{!catalog&&<option value="">等待连接</option>}{catalog?.projects.map(p=><option key={p.id} value={p.id}>{p.name}{p.state==='ready'?'':' · 未就绪'}</option>)}</select></label><button type="button" disabled={loading} onClick={()=>setRefresh(r=>r+1)}>刷新</button><button type="button" disabled={currentProject?.state!=='ready'} aria-haspopup="dialog" aria-expanded={dialog==='maintenance'} onClick={event=>{event.currentTarget.focus({preventScroll:true});setDialog('maintenance')}}>存储维护</button><button type="button" aria-haspopup="dialog" aria-expanded={dialog==='settings'} onClick={event=>{event.currentTarget.focus({preventScroll:true});setDialog('settings')}}>设置</button></div></header>
    <ProjectChats project={currentProject?.state==='ready'?project:''} read={read}/>
    <div className="sg-body"><div className="sg-main-layout">
      <section className="sg-canvas" aria-label="图谱与事项列表">{!map?<p className="sg-empty">{loading?'正在载入图谱…':'暂无图谱数据。'}</p>:!memories.length?<p className="sg-empty">{query||chat?'没有匹配的事项。':'暂无关键事项；不会因打开面板而触发抽取。'}</p>:mode==='graph'&&filtered?<SessionGraphCanvas map={filtered} focus={selected?.id||''} locate={locate} onSelect={select}/>:<ul className="sg-list" ref={listRef}>{memories.map(n=><li key={n.id}><button ref={element=>{if(element)listButtons.current.set(n.id,element);else listButtons.current.delete(n.id)}} aria-pressed={selected?.id===n.id} onClick={()=>select(n)}><strong>{n.label}</strong><span>{label(n.object_type || n.kind)} · {label(n.status)}</span><p>{n.content?.slice(0,180)}</p></button></li>)}</ul>}</section>
      <section className="sg-info" aria-label="项目状态与详情">

        <div className="sg-status"><span>{map?'本地索引':loading?'读取中…':error?'图谱读取失败':'索引未就绪'}</span><span>检索不调用模型</span>{status&&<span>后台 {status.active_count} 项进行中</span>}</div>
        {currentProject&&<p className="sg-root">{currentProject.root}</p>}
        {error&&<p role="alert" className="sg-error">{error}。读取失败不代表图谱为空或数据已删除。请确认 SessionGraph 运行时已安装、项目索引可读后刷新；不会自动启动抽取。</p>}
        {selected?.validity&&<p role="status" className={selected.validity.state==='current'?'sg-muted':'sg-error'}>{selected.label} · {revisionValidityLabel(selected.validity)}</p>}
        {currentProject?.state==='not_initialized'&&<p className="sg-empty">项目尚未初始化。此面板不会自动创建索引或导入聊天。</p>}
        {currentProject?.state==='unavailable'&&<p role="alert" className="sg-error">项目索引不可用，请检查该项目配置与数据库。</p>}
        {map&&<><div className="sg-metrics"><span><b>{map.stats.total_memories}</b> 当前层事项</span><span><b>{map.stats.semantic_relations}</b> 语义关系</span><span><b>{map.stats.reference_relations}</b> 引用关系</span></div>{map.tiering?.available&&<p className="sg-muted">知识层 {map.tiering.canonical.toLocaleString()} · 过程记录 {map.tiering.episode.toLocaleString()} · 仅回执 {map.tiering.record_only.toLocaleString()}。切换层次不会删除或隐藏原文证据。{map.tiering.complete===false&&<span> 历史分类进度：{map.tiering.covered?.toLocaleString()??'—'} / {map.tiering.total?.toLocaleString()??'—'}；后台空闲时分批补齐，可刷新查看进度。</span>}{map.tiering.complete!==false&&map.tiering.current===false&&<span> 历史分类规则待更新（{map.tiering.revision||'未知'}），后台空闲时自动更新。</span>}</p>}
          <form className="sg-toolbar" onSubmit={e=>{e.preventDefault();void searchMap()}}>
            <div className="sg-search-field"><Search size={15} aria-hidden="true"/><input aria-label="搜索事项" placeholder="搜索全部历史 · 标题、编号、结果、路径" value={query} maxLength={1000} onChange={e=>setQuery(e.target.value)}/><button type="submit" aria-label="搜索全部历史" title="搜索全部历史" disabled={loading}><Search size={15}/></button></div>
            <div className="sg-filter-row"><select aria-label="筛选 chat" value={chat} onChange={e=>{setChat(e.target.value);void searchMap(query,e.target.value)}}><option value="">全部聊天</option>{map.chats.map(c=><option key={c.chat_id} value={c.chat_id}>{c.chat_id} · {c.count}</option>)}</select><div className="sg-layer-switch" aria-label="图谱层次"><button type="button" aria-pressed={view==='knowledge'} onClick={()=>setView('knowledge')}>知识层</button><button type="button" aria-pressed={view==='episode'} onClick={()=>setView('episode')}>过程记录</button><button type="button" aria-pressed={view==='all'} onClick={()=>setView('all')}>全部</button></div><div className="sg-view-switch"><button type="button" aria-pressed={mode==='graph'} onClick={()=>setMode('graph')}>图谱</button><button type="button" aria-pressed={mode==='list'} onClick={()=>setMode('list')}>列表</button></div></div>
          </form>
          {navigationNote&&<p role="status" className="sg-muted">{navigationNote}</p>}
          {map.page?<div className="sg-pagination"><p className="sg-muted" title="搜索覆盖所选范围的全部历史，分页只控制展示数量">{map.page.chat?'所选聊天':'全部历史'} · {map.page.matched.toLocaleString()} 条<span>{map.stats.memories?map.page.offset+1:0}–{map.page.offset+map.stats.memories}</span></p><button aria-label="上一页" title="上一页" disabled={loading||map.page.offset===0} onClick={()=>void searchMap(map.page!.query,map.page!.chat,Math.max(0,map.page!.offset-map.page!.limit))}><ChevronLeft size={15}/></button><button aria-label="下一页" title="下一页" disabled={loading||map.page.next_offset===null} onClick={()=>void searchMap(map.page!.query,map.page!.chat,map.page!.next_offset!)}><ChevronRight size={15}/></button></div>:map.truncated&&<p className="sg-muted">仅载入最近 {map.stats.memories} / {map.stats.total_memories} 条事项；请更新运行时以启用全项目搜索。</p>}

          <aside ref={detailRef} className="sg-detail" aria-label="事项详情">{selected?<><h3>{selected.label}</h3><p className="sg-muted">{selected.type==='memory'?`${label(selected.object_type || selected.kind)} · ${label(selected.status)} · 历史声明，不代表当前已验证`:'精确引用，不表示文件当前存在'}</p>{selected.chat_id&&<p className="sg-root">chat: {selected.chat_id}</p>}<p className="sg-content">{selected.content||selected.path}</p>{selected.evidence_status==='missing_or_mismatched'&&<p role="alert" className="sg-error">原文缺失或引用不匹配，不应据此确认事实。</p>}{detailLoading&&<p>正在读取来源…</p>}{detailError&&<p role="alert">{detailError}</p>}{detail&&<>{detail.graph_v2 && <details><summary>对象证据与历史 · {detail.graph_v2.assertions.length}</summary>{detail.graph_v2.assertions.map((a,i)=><div key={`${a.entry_id}-${i}`}><p>{a.assertion.content}</p><blockquote>{a.quote}</blockquote><code>{a.chat_id}{a.source_time?` · ${a.source_time}`:''}{a.original_node_id?` · ${a.original_node_id}`:''}</code></div>)}{detail.graph_v2.evidence_page?.has_more&&<button disabled={detailLoading} onClick={()=>void moreEvidence()}>加载更多对象证据</button>}{(detail.graph_v2.member_ids?.length||0)>1&&<p className="sg-muted">统一身份包含 {detail.graph_v2.member_ids?.length} 个原始节点；全部旧 ID 保留。</p>}</details>}<h4>引用证据 · {detail.memory.source?.role||'未知角色'}</h4><blockquote>{detail.memory.source?.quote||'没有引用'}</blockquote>{detail.memory.paths?.length? <><h4>文件位置（未检查存在性）</h4>{detail.memory.paths.map(p=><code className="sg-path" key={p}>{p}</code>)}</>:null}<details><summary>查看原文</summary>{detail.entries.map(e=><pre key={e.id}>{e.text}</pre>)}</details>{detail.superseded_by.length>0&&<p className="sg-error">已被后续事项替代：{detail.superseded_by.map(m=>m.title).join('；')}</p>}</>}{map.edges.some(e=>e.source===selected.id||e.target===selected.id)&&<><h4>已存关系</h4><ul className="sg-relations">{map.edges.filter(e=>e.source===selected.id||e.target===selected.id).slice(0,30).map((e,i)=>{const other=map.nodes.find(n=>n.id===(e.source===selected.id?e.target:e.source));return <li key={i}><span className="sg-relation-kind">{e.source===selected.id?'→':'←'} {e.kind}{e.statement_mode && e.statement_mode !== 'asserted' ? ` · ${e.statement_mode}` : ''}</span>{other&&<button title="在左侧定位此事项" onClick={()=>navigateRelation(other)}>{other.label}</button>}</li>})}</ul></>}</>:<p className="sg-empty">选择事项，查看详情与原文来源。</p>}</aside></>}
        {statusError&&<p role="status" className="sg-error">{statusError}</p>}
        {map?.graph_v2?.initialized && <details className="sg-tasks sg-diagnostic-section">
          <summary className="sg-section-summary"><span><strong>对象图谱 v2 · {map.graph_v2.nodes} 个对象 / {map.graph_v2.relations} 条关系</strong></span><span className="sg-disclosure-chevron" aria-hidden="true">›</span></summary>
          <div className="sg-section-content">
            {map.graph_v2.progress?<p className="sg-muted">处理中 {map.graph_v2.progress.running||0} · 待处理 {map.graph_v2.progress.ready||0} · 等待重试 {map.graph_v2.progress.retry_wait||0} · 受阻 {map.graph_v2.progress.blocked||0}</p>:<p className="sg-compact-overview">未完成抽取批次：{Object.entries(map.graph_v2.jobs||{}).filter(([state])=>state!=='complete'&&state!=='superseded'&&state!=='dismissed').reduce((sum,[,count])=>sum+count,0)}</p>}
            <details className="sg-technical"><summary>查看诊断与候选原因</summary>
            {[...(map.graph_v2.deferred_jobs||[]),...(map.graph_v2.blocked_jobs||[])].map(job=><GraphJobDiagnostics key={job.id} job={job}/>)}
            <h4>候选与未采用原因</h4>
            <p className="sg-muted">这些是尚未确认或未进入正式图谱的候选，不是排队任务，也不代表已验证事实。</p>
            {Object.keys(map.graph_v2.unresolved||{}).length?<ul className="sg-reason-list">{Object.entries(map.graph_v2.unresolved||{}).sort((a,b)=>b[1]-a[1]).map(([reason,count])=><li key={reason}><span>{reasonLabel(reason)}</span><b>{count}</b></li>)}</ul>:<p className="sg-muted">暂无候选原因记录。</p>}
            {!!Object.keys(map.graph_v2.unresolved||{}).length&&<details className="sg-technical"><summary>技术详情 · 原始原因码</summary><dl className="sg-code-list">{Object.entries(map.graph_v2.unresolved||{}).map(([reason,count])=><div key={reason}><dt><code>{reason}</code></dt><dd>{count}</dd></div>)}</dl></details>}
            </details>
          </div>
        </details>}
        <details className="sg-tasks sg-diagnostic-section">
          <summary className="sg-section-summary"><span><strong>后台任务与诊断{status?` · 最近 ${status.runs.length} 次`:''}</strong></span><span className="sg-disclosure-chevron" aria-hidden="true">›</span></summary>
          <div className="sg-section-content">
            {currentProject?.state==='ready'&&<SessionGraphTaskCleanup key={project} project={project} manage={manage} onChanged={()=>setRefresh(r=>r+1)}/>}
            {status?.continuations?.map(item=><p key={item.chat} className={item.state==='blocked'?'sg-error':'sg-muted'} title={item.state==='blocked'?errorDescription(item.error_code):undefined}><small title={item.chat}>chat {item.chat.slice(-6)} · </small>{item.state==='blocked'?`自动续跑受阻 · ${errorLabel(item.error_code)}`:item.state==='running'?'自动续跑中':item.error_code==='resource_circuit_open'?`连接冷却中 · ${new Date(item.due*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false})} 自动继续`:item.state==='daily_limit'?'旧版限制已移除，等待自动续跑':item.state==='ready'||(item.state==='cooldown'&&deferredCodes.has(item.error_code||''))?'等待执行 · 将按队列顺序继续':`请求失败，等待重试 · ${new Date(item.due*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false})} 自动继续`}</p>)}
            {status?.unreadable_runs?<p className="sg-error">{status.unreadable_runs} 份任务记录无法读取</p>:null}
            {status?.runs.length?<><div className="sg-run-row sg-run-columns" aria-hidden="true"><span>时间</span><span>操作</span><span>状态</span><span>节点/关系</span><span>调用</span></div><ol className="sg-run-list">{status.runs.map(r=><RunDiagnostics key={r.run_id} run={r}/>)}</ol></>:<p className="sg-muted">暂无可显示的任务记录。</p>}
            <details className="sg-technical"><summary>如何理解这些记录</summary><p className="sg-muted">任务日志是历史状态，恢复后仍保留原失败记录；已创建批次完成不代表全部原文已抽取，请同时查看本次输入覆盖状态。日志不证明 MCP 或 Hook 当前在线；此处只读取，不重试任务。</p></details>
          </div>
        </details>
      </section>
    </div></div>
    {dialog&&<SessionGraphDialog title={dialog==='settings'?'设置':'存储维护'} onClose={()=>setDialog(null)}>
      {dialog==='settings'
        ?<SessionGraphSettings key={project} project={currentProject?.state==='ready'?project:''} models={models} manage={manage}/>
        :currentProject?.state==='ready'&&<SessionGraphMaintenance key={project} project={project} manage={manage} onChanged={()=>setRefresh(r=>r+1)}/>}
    </SessionGraphDialog>}
  </section>
}
