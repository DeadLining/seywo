import { useEffect, useState } from 'react'
import { readSessionGraph, type ModelCallPage, type Project, type SessionGraphRead } from '../sessionGraph'
import { errorLabel } from '../diagnostics'

const statusLabel:Record<string,string>={started:'请求中／未记录结束',received:'已响应，待校验',validated:'校验通过',transport_failed:'连接失败',response_failed:'响应失败',invalid_json:'响应格式错误',invalid_envelope:'响应结构错误',validation_failed:'内容校验失败',probe_ok:'测试连通',probe_failed:'测试失败'}
const stageLabel:Record<string,string>={extract:'抽取',identity:'身份判断',relations:'关系判断',judge:'判断',probe:'连通性测试',publish_relations:'关系构建'}
const number=(n:number|null)=>n===null||n===undefined?'未知':n.toLocaleString()
export function SessionGraphCallLogs({project,projects,read=readSessionGraph}:{project:string;projects:Project[];read?:SessionGraphRead}){
  const [scope,setScope]=useState(project),[model,setModel]=useState(''),[status,setStatus]=useState('all')
  const [offset,setOffset]=useState(0),[revision,setRevision]=useState(0),[page,setPage]=useState<ModelCallPage|null>(null),[error,setError]=useState(''),[loading,setLoading]=useState(true)
  useEffect(()=>{
    let current=true
    setLoading(true);setError('');setPage(null)
    read<ModelCallPage>('call_logs',{...(scope?{project:scope}:{}),logModel:model,logStatus:status,offset})
      .then(result=>{if(current)setPage(result)})
      .catch(e=>{if(current)setError(e instanceof Error?e.message:'调用日志读取失败，请稍后刷新。')})
      .finally(()=>{if(current)setLoading(false)})
    return ()=>{current=false}
  },[read,scope,model,status,offset,revision])
  return <section className="sg-call-logs" aria-label="模型调用日志" aria-busy={loading}>
    <p className="sg-muted">记录最近 7 天的调用摘要，日志库最多 20 MiB。不会保存密钥、完整提示词或模型输出，也不生成图谱节点。升级前的详细审计未复制到此处。</p>
    <div className="sg-call-filters">
      <label>项目<select value={scope} onChange={e=>{setScope(e.target.value);setModel('');setOffset(0)}}><option value="">全部项目（含全局测试）</option>{projects.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
      <label>模型<select value={model} onChange={e=>{setModel(e.target.value);setOffset(0)}}><option value="">全部模型</option>{Array.from(new Set([...(page?.models||[]),...(model?[model]:[])])).map(m=><option key={m} value={m}>{m}</option>)}</select></label>
      <label>结果<select value={status} onChange={e=>{setStatus(e.target.value);setOffset(0)}}><option value="all">全部结果</option><option value="failed">失败</option><option value="succeeded">成功</option><option value="pending">请求中／待校验</option></select></label>
      <button type="button" disabled={loading} onClick={()=>setRevision(n=>n+1)}>刷新日志</button>
    </div>
    {loading&&<p role="status">正在读取调用日志…</p>}
    {error&&<p role="alert" className="sg-error">{error}</p>}
    {page&&!page.items.length&&<p className="sg-muted">当前范围暂无调用记录；后续抽取、判断和模型测试会自动记录。</p>}
    <ol className="sg-call-list">{page?.items.map(call=><li key={call.id}>
      <div className="sg-call-title"><strong>{call.model||'模型未知'}</strong><span className={['transport_failed','response_failed','invalid_json','invalid_envelope','validation_failed','probe_failed'].includes(call.state)?'sg-error':''}>{statusLabel[call.state]||'状态未知'}</span></div>
      <p><time>{new Date(call.created_at*1000).toLocaleString('zh-CN',{hour12:false})}</time> · {stageLabel[call.stage]||(call.stage?'模型判断':'模型请求')} · {call.elapsed_ms===null?'耗时未知':`${number(call.elapsed_ms)} ms`} · Token 输入 {number(call.input_tokens)} / 输出 {number(call.output_tokens)}</p>
      <details><summary>调用详情</summary><dl>
        <dt>项目</dt><dd>{projects.find(p=>p.id===call.project)?.name||call.project||'全局测试'}</dd>
        <dt>请求编号</dt><dd>{call.id}</dd><dt>服务地址</dt><dd>{call.endpoint||'未知'}</dd>
        <dt>HTTP 状态</dt><dd>{number(call.http_status)}</dd>
        {call.response_model&&<><dt>服务返回模型</dt><dd>{call.response_model}</dd></>}
        {call.chat_id&&<><dt>chat</dt><dd>{call.chat_id}</dd></>}
        {call.run_id&&<><dt>任务编号</dt><dd>{call.run_id}</dd></>}
        {call.attempt_id&&<><dt>执行编号</dt><dd>{call.attempt_id}</dd></>}
        {call.provider_request_id&&<><dt>服务请求编号</dt><dd>{call.provider_request_id}</dd></>}
        <dt>阶段重试次数</dt><dd>{number(call.retry_count)}</dd>
        {call.error_code&&<><dt>失败原因</dt><dd>{call.error_code==='probe_failed'?'模型连通性测试失败':errorLabel(call.error_code)}</dd></>}
      </dl></details>
    </li>)}</ol>
    <div className="sg-call-pagination"><button type="button" disabled={loading||offset===0} onClick={()=>setOffset(n=>Math.max(0,n-50))}>上一页</button><span>第 {Math.floor(offset/50)+1} 页</span><button type="button" disabled={loading||!page?.has_more} onClick={()=>setOffset(n=>n+50)}>下一页</button></div>
  </section>
}
