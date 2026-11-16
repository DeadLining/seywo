import { useEffect, useState } from 'react'
import { readSessionGraph, type Catalog } from '../sessionGraph'
import { ProjectChats as SeywoProjectChats } from './ProjectChats'
import { SessionGraphProjectChats } from './SessionGraphProjectChats'
import { SessionGraphDialog } from './SessionGraphDialog'
import { SessionGraphSettings } from './SessionGraphSettings'
import { SessionGraphCallLogs } from './SessionGraphCallLogs'
import { VersionHistory } from './VersionHistory'
import './SessionGraphPanel.css'
import './TaskWorkspace.css'

type Mode = 'sessiongraph' | 'seywo'

function SessionGraphWorkspace() {
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [project, setProject] = useState('')
  const [settings, setSettings] = useState(false)
  const [logs, setLogs] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    readSessionGraph<Catalog>('projects').then(data => {
      if (active) { setCatalog(data); setProject(data.default_project) }
    }).catch(() => { if (active) setError('无法读取项目，请检查 SessionGraph 的安装配置。') })
    return () => { active = false }
  }, [])

  const current = catalog?.projects.find(item => item.id === project)
  return <section className="task-workspace-mode" aria-label="SessionGraph">
    <header className="task-header">
      <div><h1>任务流程</h1><p>把重要的讨论和决定整理成清晰的过程。</p></div>
      <div className="task-actions"><label>项目 <select aria-label="SessionGraph 项目" value={project} onChange={event => setProject(event.target.value)}>
        {!catalog && <option value="">正在读取</option>}
        {catalog?.projects.map(item => <option key={item.id} value={item.id}>{item.name}{item.state === 'ready' ? '' : ' · 未就绪'}</option>)}
      </select></label>
        <button type="button" onClick={() => setLogs(true)}>调用日志</button>
        <button type="button" onClick={() => setSettings(true)}>设置</button>
      </div>
    </header>
    {error && <p role="alert">{error}</p>}
    {current && current.state !== 'ready' && <p>该项目尚未准备好，请检查项目配置。</p>}
    {current?.state === 'ready' && <SessionGraphProjectChats key={project} project={project} read={readSessionGraph} standalone />}
    {settings && <SessionGraphDialog title="设置" onClose={() => setSettings(false)}><SessionGraphSettings project={current?.state === 'ready' ? project : ''} models={[]} /></SessionGraphDialog>}
    {logs && <SessionGraphDialog title="调用日志" onClose={() => setLogs(false)}><SessionGraphCallLogs project={project} projects={catalog?.projects || []} /></SessionGraphDialog>}
  </section>
}

function SeywoWorkspace() {
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [project, setProject] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    readSessionGraph<Catalog>('seywo_projects').then(data => {
      if (active) { setCatalog(data); setProject(data.default_project) }
    }).catch(() => { if (active) setError('暂时无法读取 Seywo 项目，请稍后重试。') })
    return () => { active = false }
  }, [])

  const current = catalog?.projects.find(item => item.id === project)
  return <section className="task-workspace-mode" aria-label="Seywo">
    <header className="app-header task-header">
      <div className="task-brand"><img src="/icon.png" alt="" /><h1>Seywo</h1></div>
      <div className="task-actions"><label>项目 <select aria-label="Seywo 项目" value={project} onChange={event => setProject(event.target.value)}>
        {!catalog && <option value="">正在读取</option>}
        {catalog?.projects.map(item => <option key={item.id} value={item.id}>{item.name}{item.state === 'ready' ? '' : ' · 未就绪'}</option>)}
      </select></label></div>
    </header>
    {error && <div className="task-notice" role="status">{error}</div>}
    {current && current.state !== 'ready' && <p>该项目尚未准备好，请检查 Seywo 项目配置。</p>}
    {current?.state === 'ready' && <SeywoProjectChats key={project} project={project} read={readSessionGraph} standalone />}
  </section>
}

export function TaskWorkspace() {
  const [mode, setMode] = useState<Mode>('sessiongraph')
  return <section className="task-workspace">
    <header className="workspace-mode-header">
      <div className="task-brand"><img src="/icon.png" alt="" /><h1>SessionGraph + Seywo</h1></div>
      <nav className="workspace-mode-tabs" aria-label="产品模式" role="tablist">
        <button type="button" role="tab" aria-selected={mode === 'sessiongraph'} className={mode === 'sessiongraph' ? 'is-active' : ''} onClick={() => setMode('sessiongraph')}>SessionGraph</button>
        <button type="button" role="tab" aria-selected={mode === 'seywo'} className={mode === 'seywo' ? 'is-active' : ''} onClick={() => setMode('seywo')}>Seywo</button>
      </nav>
      <VersionHistory />
    </header>
    <div hidden={mode !== 'sessiongraph'}><SessionGraphWorkspace /></div>
    <div hidden={mode !== 'seywo'}><SeywoWorkspace /></div>
  </section>
}
