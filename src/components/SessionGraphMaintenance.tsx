import { useEffect, useRef, useState } from 'react'
import { manageSessionGraph, type MaintenanceResult, type SessionGraphManage } from '../sessionGraph'

const size=(bytes:number)=>`${(bytes/1024/1024).toFixed(1)} MB`
export function SessionGraphMaintenance({project,manage=manageSessionGraph,onChanged}:{project:string;manage?:SessionGraphManage;onChanged:()=>void}) {
  const [result,setResult]=useState<MaintenanceResult|null>(null),[busy,setBusy]=useState(''),[error,setError]=useState(''),[notice,setNotice]=useState('')
  const mounted=useRef(true)
  useEffect(()=>{
    mounted.current=true
    let current=true
    setBusy('preview')
    manage({action:'storage_preview',project}).then(r=>{if(!r.ok)throw new Error(r.message);if(current)setResult(r.maintenance||null)}).catch(e=>{if(current)setError(String(e))}).finally(()=>{if(current)setBusy('')})
    return ()=>{current=false;mounted.current=false}
  },[project,manage])
  async function run(action:'storage_compact'|'clean_notices') {
    setBusy(action);setError('');setNotice('')
    try {
      const r=await manage({action,project})
      if(!mounted.current)return
      if(!r.ok)throw new Error(r.message)
      const m=r.maintenance
      if(!m)throw new Error('维护结果缺失，请刷新后检查。')
      if(action==='storage_compact'){
        setResult(m)
        setNotice(`已回收 ${size(m.reclaimed_bytes||0)}，节点、关系和原文证据保留。${m.warning||''}`)
      } else setNotice(`已清理 ${m.removed||0} 条已恢复告警；保留历史记录。`)
      onChanged()
    } catch(e){if(mounted.current)setError(String(e))}
    finally {if(mounted.current)setBusy('')}
  }
  const current=result?.after||result?.before
  return <section className="sg-maintenance" aria-label="存储维护" aria-busy={!!busy}>
    <div className="sg-maintenance-title"><strong>无损压缩</strong><span>本机处理 · 不调用模型</span></div>
    <p>压缩已完成任务的载荷、去重相同载荷并回收空闲页。保留全部知识节点、关系和来源。</p>
    {current&&<div className="sg-storage-stats"><span>索引占用 <b>{size(current.database_bytes+current.wal_bytes)}</b></span><span title="仅统计当前空闲页，载荷压缩可能进一步释放空间">空闲页 <b>{size(current.reclaimable_bytes)}</b></span></div>}
    <div className="sg-maintenance-actions"><button disabled={!!busy} onClick={()=>void run('storage_compact')}>{busy==='storage_compact'?'压缩中…':'开始压缩'}</button><button disabled={!!busy} onClick={()=>void run('clean_notices')}>{busy==='clean_notices'?'核对中…':'清理已恢复告警'}</button></div>
    {notice&&<p role="status">{notice}</p>}{error&&<p role="alert" className="sg-error">{error}</p>}
  </section>
}
