import { useEffect, useMemo, useState } from 'react'
import { ArrowUpRight, ChevronDown, ChevronRight, CircleDot, FileText, GitBranch, Layers3, MessageSquare, X } from 'lucide-react'
import type { SessionGraphRead } from '../sessionGraph'
import './ProjectChats.css'

type Chat = { id: string; title: string; updated_at: number | null }
type Evidence = { event_id: string; snapshot_id: string; start?: number; end?: number }
type Item = { id: string; title?: string; content?: string; scope?: string; status?: string; review_state?: string; review_reason?: string; hidden?: boolean; evidence?: Evidence[] }
type Phase = { id: string; title: string; goal_item_ids?: string[] }
type Branch = { id: string; phase_id: string; title: string; goal?: string; resolve_when?: string; state: string; outcome_item_ids?: string[]; evidence_item_ids?: string[] }
type Placement = { item_id: string; phase_id: string; branch_id?: string | null }
type Narrative = { available: boolean; version?: number; head_version?: number; state?: string; source_changed?: boolean; update_pending?: boolean; review_count?: number; pending_revision_count?: number; narrative?: { items: Item[]; phases: Phase[]; branches: Branch[]; placements: Placement[]; transitions?: { id: string; kind: string; reason?: string; from_phase_id?: string; to_phase_id?: string; from_branch_id?: string; to_branch_id?: string }[]; overview: { focus?: { phase_id?: string; branch_id?: string | null }; goal_item_ids?: string[]; open_question_ids?: string[]; next_step_ids?: string[]; unresolved?: string[] } }; gaps?: { reason: string; n: number }[] }

const stateLabels: Record<string, string> = { published_unreviewed: '自动整理', partial: '部分整理', update_pending: '待更新', needs_review: '需核对' }
const branchLabels: Record<string, string> = { active: '进行中', waiting: '等待', resolved: '已解决', abandoned: '已放弃' }

function NodeGroup({ items, selected, onSelect, previewLimit = 6 }: { items: Item[]; selected: string; onSelect: (id: string) => void; previewLimit?: number }) {
  const [expanded, setExpanded] = useState(false)
  const shown = expanded ? items : items.slice(0, previewLimit)
  return <>
    <div className="seywo-node-grid">{shown.map(item => <button type="button" key={item.id}
      className={`seywo-node ${selected === item.id ? 'is-selected' : ''}`} aria-pressed={selected === item.id}
      onClick={() => onSelect(item.id)} title={item.title || '未命名节点'}>
      <span className="seywo-node-dot" aria-hidden="true"/>
      <span className="seywo-node-title">{item.title || '未命名节点'}</span>
      {item.review_state === 'needs_review' && <small className="seywo-node-review">需核对</small>}
      <ChevronRight size={12} aria-hidden="true"/>
    </button>)}</div>
    {items.length > previewLimit && <button type="button" className="seywo-more" aria-expanded={expanded}
      onClick={() => setExpanded(!expanded)}>{expanded ? '收起节点' : `展开其余 ${items.length - previewLimit} 个节点`}<ChevronDown size={12} aria-hidden="true"/></button>}
  </>
}

function NarrativeView({ value }: { value: Narrative }) {
  const [selected, setSelected] = useState('')
  const [phaseOpen, setPhaseOpen] = useState<Record<string, boolean>>({})
  const doc = value.narrative
  const items = useMemo(() => new Map((doc?.items || []).filter(item => !item.hidden).map(item => [item.id, item])), [doc])
  const placements = useMemo(() => {
    const groups = new Map<string, Item[]>()
    for (const p of doc?.placements || []) {
      const item = items.get(p.item_id)
      if (!item) continue
      const key = JSON.stringify([p.phase_id, p.branch_id || null])
      if (!groups.has(key)) groups.set(key, [])
      groups.get(key)!.push(item)
    }
    return groups
  }, [doc, items])
  if (!value.available || !doc) return <div className="seywo-empty"><Layers3 size={28} strokeWidth={1.3} aria-hidden="true"/><strong>这个 chat 还没有 Seywo 主线</strong><p>完成一次 compact 后，这里会呈现对话的工作脉络。</p></div>
  const byPhase = (phase: string, branch?: string) => placements.get(JSON.stringify([phase, branch || null])) || []
  const focus = doc.overview?.focus
  const focusTitle = focus?.branch_id ? doc.branches.find(b => b.id === focus.branch_id)?.title : doc.phases.find(p => p.id === focus?.phase_id)?.title
  const titles = (ids: string[] = []) => ids.map(id => items.get(id)?.title).filter(Boolean).join(' · ')
  const detail = items.get(selected)
  const gaps = value.gaps?.reduce((n, row) => n + row.n, 0) || 0
  const select = (id: string) => setSelected(previous => previous === id ? '' : id)
  return <div className="seywo-reader">
    <div className="seywo-reader-meta"><span><Layers3 size={13} aria-hidden="true"/>{doc.phases.length} 个阶段<span className="seywo-meta-dot">·</span>{doc.branches.length} 条支线<span className="seywo-meta-dot">·</span>{items.size} 个节点</span>
      <details className="seywo-health"><summary><span className={`seywo-health-dot ${value.state === 'needs_review' || value.state === 'update_pending' || value.state === 'partial' ? 'needs-attention' : ''}`}/>{stateLabels[value.state || ''] || '已发布'}<ChevronDown size={11} aria-hidden="true"/></summary><div><strong>整理状态 · v{value.version ?? '—'}</strong>
        {value.update_pending && <p>对话有新增或修改，等待下次整理。</p>}
        {!!value.review_count && <p>{value.review_count} 个节点的依据或依赖发生变化，需要核对。</p>}
        {!!value.pending_revision_count && <p>{value.pending_revision_count} 项修订建议待确认。</p>}
        {gaps > 0 && <p>{gaps.toLocaleString('zh-CN')} 条来源记录未纳入整理，包含无法解析或内容不完整的记录。</p>}
        <p>当前主线可继续阅读。</p></div></details>
    </div>
    <section className="seywo-focus-strip" aria-label="当前工作"><CircleDot size={18} aria-hidden="true"/><div><span>当前焦点</span><strong>{focusTitle || '尚未标记'}</strong></div></section>
    <dl className="seywo-context"><div><dt>目标</dt><dd>{titles(doc.overview?.goal_item_ids) || '尚未标记'}</dd></div><div><dt><ArrowUpRight size={12} aria-hidden="true"/>下一步</dt><dd>{titles(doc.overview?.next_step_ids) || '暂无'}</dd></div></dl>
    <div className={`seywo-map-layout ${detail ? 'has-inspector' : ''}`}>
      <div className="seywo-map"><div className="seywo-line-label"><h3>工作脉络</h3><span>主线与支线</span></div>
        <div className="seywo-mainline">{doc.phases.map((phase, index) => {
          const main = byPhase(phase.id)
          const branches = doc.branches.filter(b => b.phase_id === phase.id).sort((a, b) => Number(b.id === focus?.branch_id) - Number(a.id === focus?.branch_id))
          const expanded = phaseOpen[phase.id] ?? (focus?.phase_id ? focus.phase_id === phase.id : index === 0)
          return <section className={`seywo-phase ${focus?.phase_id === phase.id ? 'is-current' : ''} ${expanded ? 'is-expanded' : ''}`} key={phase.id}>
            <div className="seywo-phase-marker" aria-hidden="true">{String(index + 1).padStart(2, '0')}</div>
            <div className="seywo-phase-body"><button type="button" className="seywo-phase-toggle" aria-expanded={expanded} onClick={() => setPhaseOpen(previous => ({...previous, [phase.id]: !expanded}))}>
              <h4>{phase.title}</h4>{focus?.phase_id === phase.id && !focus.branch_id && <span className="seywo-focus">当前</span>}<small>{main.length} 个节点{branches.length > 0 && ` · ${branches.length} 条支线`}</small><ChevronDown size={14} className={expanded ? 'is-open' : ''} aria-hidden="true"/>
            </button>
            {expanded && <div className={`seywo-phase-lanes ${branches.length ? 'has-branches' : ''}`}><div className="seywo-trunk"><span className="seywo-lane-label">主线</span><NodeGroup items={main} selected={selected} onSelect={select}/>{!main.length && <p className="seywo-lane-empty">该阶段的节点在支线中</p>}</div>
              {branches.length > 0 && <div className="seywo-branches">{branches.map(branch => <section className={`seywo-branch branch-${branch.state}`} key={branch.id}>
                <header><GitBranch size={14} aria-hidden="true"/><strong>{branch.title}</strong>{focus?.branch_id === branch.id && <span className="seywo-focus">当前</span>}<span className="seywo-branch-state">{branchLabels[branch.state] || '待确认'}</span></header>
                {branch.goal && <p>{branch.goal}</p>}{branch.resolve_when && <p className="seywo-branch-resolve">结束条件：{branch.resolve_when}</p>}
                <NodeGroup items={byPhase(phase.id, branch.id)} selected={selected} onSelect={select} previewLimit={3}/>
              </section>)}</div>}
            </div>}</div>
          </section>
        })}</div>
        {doc.transitions?.length ? <details className="seywo-transitions"><summary>主线转折 · {doc.transitions.length}</summary>{doc.transitions.map(t => <p key={t.id}>{t.reason || '焦点变化'}</p>)}</details> : null}
      </div>
      {detail && <aside className="seywo-inspector" aria-label="节点详情"><header><span><FileText size={13} aria-hidden="true"/>节点详情</span><button type="button" aria-label="关闭节点详情" onClick={() => setSelected('')}><X size={15}/></button></header><h3>{detail.title || '未命名节点'}</h3>{detail.review_state === 'needs_review' && <p className="seywo-node-review">{detail.review_reason === 'dependency_changed' ? '依赖的节点有变化，请核对本节点。' : detail.review_reason === 'item_split' ? '节点已拆分，请核对依赖关系。' : '引用的原文已变化或不可用，请核对本节点。'}</p>}{detail.content && <p className="seywo-node-content">{detail.content}</p>}<div className="seywo-evidence"><h4>来源 · {detail.evidence?.length || 0}</h4>{detail.evidence?.length ? detail.evidence.map(e => <div key={`${e.event_id}:${e.snapshot_id}:${e.start}:${e.end}`}><span>来源 {e.event_id.slice(-8)}</span><small>定位 {e.start ?? 0}–{e.end ?? '末尾'}</small></div>) : <p>暂无来源引用</p>}</div></aside>}
    </div>
  </div>
}

export function ProjectChats({ project, read, standalone = false }: { project: string; read: SessionGraphRead; standalone?: boolean }) {
  const [chats, setChats] = useState<Chat[]>([])
  const [selected, setSelected] = useState('')
  const [narrative, setNarrative] = useState<Narrative | null>(null)
  const [narrativeError, setNarrativeError] = useState(false)
  const [narrativeRetry, setNarrativeRetry] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [open, setOpen] = useState(standalone)
  useEffect(() => { let active = true; setChats([]); setSelected(''); setNarrative(null); setError(''); if (!project || !open) return; setLoading(true); read<{chats: Chat[]}>('seywo_project_chats', { project }).then(result => { if (!Array.isArray(result.chats)) throw new Error('invalid'); if (active) { setChats(result.chats); setSelected(result.chats[0]?.id || '') } }).catch(() => active && setError('无法读取该项目的聊天记录。')).finally(() => active && setLoading(false)); return () => { active = false } }, [project, read, open])
  useEffect(() => { let active = true; if (!project || !selected) return; setNarrative(null); setNarrativeError(false); read<Narrative>('seywo_narrative', { project, id: selected }).then(value => active && setNarrative(value)).catch(() => { if (active) setNarrativeError(true) }); return () => { active = false } }, [project, selected, read, narrativeRetry])
  useEffect(() => {
    if (!project || !selected) return
    const refresh = () => setNarrativeRetry(value => value + 1)
    window.addEventListener('focus', refresh)
    return () => window.removeEventListener('focus', refresh)
  }, [project, selected])
  const currentTitle = chats.find(chat => chat.id === selected)?.title || '对话'
  const content = <div className="seywo-columns">
    <section className="seywo-chats" aria-label="项目对话"><div className="seywo-section-heading"><h2><MessageSquare size={15} aria-hidden="true"/>对话</h2><span>{chats.length}</span></div>
      {loading && <p className="seywo-loading">正在读取…</p>}{error && <p role="alert">{error}</p>}
      {!loading && !error && chats.length === 0 && <p className="seywo-loading">暂无对话</p>}
      <div className="seywo-chat-list">{chats.map(chat => <button type="button" key={chat.id} className={selected === chat.id ? 'is-selected' : ''} aria-pressed={selected === chat.id} onClick={() => setSelected(chat.id)} title={chat.title || '未命名 chat'}><strong>{chat.title || '未命名 chat'}</strong>{chat.updated_at && <small>{new Date(chat.updated_at * 1000).toLocaleString('zh-CN', {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'})}</small>}</button>)}</div>
    </section>
    <section className="seywo-detail">{selected ? <><header className="seywo-detail-heading"><span className="seywo-kicker">对话脉络</span><h2 title={currentTitle}>{currentTitle}</h2></header>
      {narrativeError ? <div className="seywo-empty" role="status"><strong>暂时无法读取这条 chat 的主线</strong><button type="button" onClick={() => setNarrativeRetry(value => value + 1)}>重试</button></div> : narrative ? <NarrativeView key={selected} value={narrative} /> : <p className="seywo-loading">正在读取主线…</p>}
    </> : <div className="seywo-empty"><MessageSquare size={28} strokeWidth={1.3} aria-hidden="true"/><strong>选择一个 chat</strong><p>从左侧选择对话，查看工作脉络。</p></div>}</section>
  </div>
  if (standalone) return content
  return <details className="sg-project-chats" open={open} onToggle={event => setOpen(event.currentTarget.open)}><summary>项目聊天</summary>{content}</details>
}
