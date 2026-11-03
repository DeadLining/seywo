import { useEffect, useRef, useState } from 'react'
import { manageSessionGraph, type SessionGraphManage } from '../sessionGraph'

export function SessionGraphTaskCleanup({project,manage=manageSessionGraph,onChanged}:{project:string;manage?:SessionGraphManage;onChanged:()=>void}) {
  const [busy,setBusy]=useState(false),[notice,setNotice]=useState(''),[error,setError]=useState('')
  const mounted=useRef(true)
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false}},[])
  async function clear() {
    if(busy)return
    setBusy(true);setNotice('');setError('')
    try {
      const result=await manage({action:'clear_tasks',project})
      if(!mounted.current)return
      if(!result.ok)throw new Error(result.message)
      const counts=result.maintenance
      if(!counts)throw new Error('清理结果缺失，请刷新后检查。')
      setNotice(`已清理 ${counts.dismissed_tasks||0} 个受阻续跑任务、${counts.dismissed_batches||0} 个受阻图谱批次，归档 ${counts.archived_runs||0} 条失败或中断记录。${counts.unreadable_runs?`另有 ${counts.unreadable_runs} 条记录未能处理，已保留。`:''}`)
      onChanged()
    } catch(e){if(mounted.current)setError(String(e))}
    finally{if(mounted.current)setBusy(false)}
  }
  return <div className="sg-task-cleanup" aria-busy={busy}>
    <div className="sg-task-cleanup-action"><button type="button" disabled={busy} onClick={()=>void clear()}>{busy?'清理中…':'清理失效任务'}</button><small>仅当前项目 · 保留记忆与原文</small></div>
    <p className="sg-muted">归档受阻续跑任务、受阻图谱批次及失败记录，不标记为成功；保留原文、图谱、已保存进度和运行中或等待执行的任务，新内容照常处理。</p>
    {notice&&<p role="status">{notice}</p>}{error&&<p role="alert" className="sg-error">{error}</p>}
  </div>
}
