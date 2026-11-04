import { useEffect, useState } from 'react'
import { readSessionGraph, type Catalog } from '../sessionGraph'
import { ProjectChats } from './ProjectChats'
import { SessionGraphDialog } from './SessionGraphDialog'
import { SessionGraphSettings } from './SessionGraphSettings'
import { SessionGraphCallLogs } from './SessionGraphCallLogs'
import './SessionGraphPanel.css'
import './TaskWorkspace.css'

export function TaskWorkspace() {
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [project, setProject] = useState('')
  const [settings, setSettings] = useState(false)
  const [logs, setLogs] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    readSessionGraph<Catalog>('projects').then(data => {
      if (active) {setCatalog(data); setProject(data.default_project)}
    }).catch(() => {if (active) setError('无法读取项目，请检查 SessionGraph 的安装配置。')})
    return () => {active = false}
  }, [])
  const current = catalog?.projects.find(item => item.id === project)
  return <section className="task-workspace">
    <header className="task-header"><div><h1>任务流程</h1><p>把重要的讨论和决定整理成清晰的过程。</p></div>
      <div><label>项目 <select aria-label="项目" value={project} onChange={e => setProject(e.target.value)}>
        {!catalog && <option value="">正在读取</option>}{catalog?.projects.map(item => <option key={item.id} value={item.id}>{item.name}{item.state === 'ready' ? '' : ' · 未就绪'}</option>)}
      </select></label> <button onClick={() => setLogs(true)}>调用日志</button> <button onClick={() => setSettings(true)}>设置</button></div></header>
    {error && <p role="alert">{error}</p>}
    {current && current.state !== 'ready' && <p>该项目尚未准备好，请检查项目配置。</p>}
    {current?.state === 'ready' && <ProjectChats key={project} project={project} read={readSessionGraph} standalone/>}
    {settings && <SessionGraphDialog title="设置" onClose={() => setSettings(false)}><SessionGraphSettings project={current?.state === 'ready' ? project : ''} models={[]}/></SessionGraphDialog>}
    {logs && <SessionGraphDialog title="调用日志" onClose={() => setLogs(false)}><SessionGraphCallLogs project={project} projects={catalog?.projects || []}/></SessionGraphDialog>}
  </section>
}
