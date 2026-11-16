import { useEffect, useState } from 'react'
import { invoke } from '@tauri-apps/api/core'
import type { SessionGraphRead } from '../sessionGraph'

type Chat = { id: string; title: string; updated_at: number | null; flow_state?: 'partial'|'complete'; flow_filtered?: boolean }
type Node = { id: string; label: string; kind: string; quote: string; source_time: string; entry_id: string; manual: number; parent_entry_id?: string; branch_quote?: string }
type Task = { id: string; title: string; nodes: Node[] }

export function SessionGraphProjectChats({ project, read, standalone = false }: { project: string; read: SessionGraphRead; standalone?: boolean }) {
  const [chats, setChats] = useState<Chat[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [tasks, setTasks] = useState<Task[]>([])
  const [running, setRunning] = useState('')
  const [message, setMessage] = useState('')
  const [revision, setRevision] = useState(0)
  const [open, setOpen] = useState(standalone)
  const [selected, setSelected] = useState('')
  const [editing, setEditing] = useState('')
  const [draft, setDraft] = useState('')
  const [deleting, setDeleting] = useState('')
  const [taskAction, setTaskAction] = useState<'rename'|'merge'|''>('')
  const [target, setTarget] = useState('')
  const [originals, setOriginals] = useState<Record<string, {text:string; next:number|null}>>({})
  useEffect(() => {
    let active = true
    setChats([])
    setTasks([])
    setOriginals({})
    setError('')
    if (!project || !open) return
    setLoading(true)
    read<{chats: Chat[]}>('project_chats', {project}).then(result => {
      if (!Array.isArray(result.chats)) throw new Error('聊天列表格式无效')
      if (active) setChats(result.chats)
    }).catch(() => { if (active) setError('无法读取该项目的聊天记录，请检查 Codex 聊天索引和项目配置。') })
      .finally(() => { if (active) setLoading(false) })
    read<{tasks: Task[]}>('flow_tasks', {project}).then(result => {
      if (active && Array.isArray(result.tasks)) setTasks(result.tasks)
    }).catch(() => { if (active) setError('无法读取任务结果。') })
    return () => { active = false }
  }, [project, read, revision, open])
  async function generate(chat: Chat) {
    setRunning(chat.id)
    setMessage('正在分析聊天，请稍候…')
    try {
      const result = await invoke<{ok: boolean; message?: string; saved_nodes?: number; complete?: boolean}>('generate_task_flow', {project, chat: chat.id})
      if (!result.ok) throw new Error(result.message || '生成失败')
      setMessage(result.complete === false ? `本次新增 ${result.saved_nodes ?? 0} 个节点，仍有内容未处理，请点击“继续生成”。` : `筛选处理完成，本次新增 ${result.saved_nodes ?? 0} 个节点。`)
      setRevision(value => value + 1)
    } catch (e) { setMessage(`${e instanceof Error ? e.message : String(e)}。已完成的部分会保留，可继续重试。`); setRevision(value => value + 1) }
    finally { setRunning('') }
  }
  async function editNode(action: 'add'|'edit'|'delete'|'rename'|'merge'|'split', node = '') {
    if (!selected || running) return
    setRunning('edit')
    try {
      const result = await invoke<{ok:boolean;message?:string}>('edit_task_node', {project, task:selected, action, node:node || null, label:draft || null, target:target || null})
      if (!result.ok) throw new Error(result.message || '保存失败')
      setEditing(''); setDeleting(''); setTaskAction(''); setDraft(''); setTarget(''); setMessage('已保存修改。')
      if (action==='merge') setSelected(target)
      setRevision(value => value + 1)
    } catch (e) { setMessage(e instanceof Error ? e.message : String(e)) }
    finally { setRunning('') }
  }
  async function showOriginal(node: Node, offset = 0) {
    try {
      const result = await read<{entries:{text:string;next_offset:number|null}[]}>('evidence', {project, id:node.entry_id, offset})
      const entry = result.entries[0]
      if (!entry) throw new Error('原文不可用')
      setOriginals(old => ({...old, [node.id]: {text:(offset ? old[node.id]?.text || '' : '') + entry.text, next:entry.next_offset}}))
    } catch {setMessage('原文读取失败，请检查聊天记录是否仍在本机。')}
  }
  if (!project) return null
  const kindLabel: Record<string, string> = {goal:'目标',problem:'问题',option:'方案',decision:'决定',action:'执行',result:'结果',next_step:'下一步'}
  const task = tasks.find(item => item.id === selected)
  const content = <div className="flow-columns">
    <section className="flow-chats"><h2>聊天记录</h2>
    <p>选择需要整理的聊天，点击生成。系统会筛选对话；部分助手回复和工具内容不会发送给模型。</p>
    {loading && <p>正在读取聊天列表…</p>}
    {error && <p role="alert">{error}</p>}
    {!loading && !error && chats.length === 0 && <p>没有找到可读取的项目聊天。</p>}
    {message && <p role="status">{message}</p>}
    <ul>{chats.map(chat => <li key={chat.id}><div><strong>{chat.title}</strong>{chat.updated_at != null && <small>{new Date(chat.updated_at * 1000).toLocaleString('zh-CN')}</small>}{chat.flow_state && <small>{chat.flow_state==='partial'?'上次未处理完':'上次筛选处理完成'}</small>}{chat.flow_filtered && <small>部分助手回复未发送给模型</small>}</div> <button type="button" disabled={!!running} onClick={() => void generate(chat)}>{running===chat.id?'生成中…':chat.flow_state==='partial'?'继续生成':'生成 / 更新'}</button></li>)}</ul>
    </section>
    <section className="flow-tasks"><h2>任务</h2>{!tasks.length && <p>选择一条聊天生成任务。</p>}
    {tasks.map(item => <button className="flow-task" aria-pressed={task?.id===item.id} key={item.id} onClick={() => setSelected(item.id)}>{item.title}<small>{item.nodes.length} 个节点</small></button>)}</section>
    <section className="flow-detail"><h2>{task?.title || '任务流程'}</h2>{!task ? <p>选择一个任务查看流程和原文。</p> : <>
      <button onClick={() => {setTaskAction('rename');setDraft(task.title)}}>修改任务名称</button>{' '}
      {tasks.length>1 && <button onClick={() => {setTaskAction('merge');setTarget('')}}>合并任务</button>}
      {taskAction==='rename' && <form onSubmit={e => {e.preventDefault();void editNode('rename')}}><label>任务名称 <input value={draft} maxLength={120} required onChange={e => setDraft(e.target.value)}/></label> <button disabled={!!running}>保存</button><button type="button" onClick={() => setTaskAction('')}>取消</button></form>}
      {taskAction==='merge' && <form onSubmit={e => {e.preventDefault();void editNode('merge')}}><label>合并到 <select required value={target} onChange={e => setTarget(e.target.value)}><option value="">选择任务</option>{tasks.filter(t=>t.id!==task.id).map(t=><option key={t.id} value={t.id}>{t.title}</option>)}</select></label> <button disabled={!!running}>确认合并</button><button type="button" onClick={() => setTaskAction('')}>取消</button></form>}
      <hr/>
      <button onClick={() => {setEditing('new');setDraft('')}}>新增节点</button>
      {editing === 'new' && <form onSubmit={e => {e.preventDefault();void editNode('add')}}><label>节点内容 <input value={draft} maxLength={200} required onChange={e => setDraft(e.target.value)}/></label> <button disabled={!!running}>保存</button><button type="button" onClick={() => setEditing('')}>取消</button></form>}
      <ol className="flow-timeline">{task.nodes.map(node => <li key={node.id} data-branch={!!node.parent_entry_id}><span>{node.manual ? '手动记录' : kindLabel[node.kind] || '节点'}</span>
        {node.parent_entry_id && task.nodes.find(parent => parent.entry_id === node.parent_entry_id) && <div className="flow-branch">分支：{task.nodes.find(parent => parent.entry_id === node.parent_entry_id)?.label} <details><summary>查看分支依据</summary><blockquote>{node.branch_quote}</blockquote></details></div>}
        {editing===node.id ? <form onSubmit={e => {e.preventDefault();void editNode('edit',node.id)}}><label>节点内容 <input value={draft} maxLength={200} required onChange={e => setDraft(e.target.value)}/></label> <button disabled={!!running}>保存</button><button type="button" onClick={() => setEditing('')}>取消</button></form> : <><strong>{node.label}</strong><button onClick={() => {setEditing(node.id);setDraft(node.label)}}>修改</button> <button onClick={() => setDeleting(node.id)}>删除</button></>}
        <button onClick={() => {setEditing('split:'+node.id);setDraft('')}}>拆为新任务</button>
        {editing==='split:'+node.id && <form onSubmit={e => {e.preventDefault();void editNode('split',node.id)}}><label>新任务名称 <input value={draft} maxLength={120} required onChange={e => setDraft(e.target.value)}/></label> <button disabled={!!running}>确认拆分</button><button type="button" onClick={() => setEditing('')}>取消</button></form>}
        {deleting===node.id && <p>确定删除这个节点？ <button disabled={!!running} onClick={() => void editNode('delete',node.id)}>确认删除</button> <button onClick={() => setDeleting('')}>取消</button></p>}
        {!node.manual && <details onToggle={event => {if (event.currentTarget.open && !originals[node.id]) void showOriginal(node)}}><summary>查看原文</summary><blockquote>{originals[node.id]?.text || node.quote}</blockquote>{originals[node.id]?.next != null && <button onClick={() => void showOriginal(node, originals[node.id].next!)}>继续阅读</button>}</details>}</li>)}</ol></>}</section>
  </div>
  if (standalone) return content
  return <details className="sg-project-chats" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>项目聊天</summary>
    {content}
  </details>
}
