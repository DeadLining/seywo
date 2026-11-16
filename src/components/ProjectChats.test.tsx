import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ProjectChats } from './ProjectChats'

describe('Seywo chat narrative', () => {
  it('loads chats only after opening and then reads the selected narrative', async () => {
    const read = vi.fn(async (op: string) => op === 'seywo_project_chats'
      ? {chats: [{id:'chat-1',title:'聊天一',updated_at:1}, {id:'chat-2',title:'聊天二',updated_at:2}]}
      : {available:false})
    render(<ProjectChats project="project-id" read={read as never}/>)
    expect(read).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('项目聊天'))
    await screen.findAllByText('聊天一')
    await waitFor(() => expect(read).toHaveBeenCalledWith('seywo_narrative', {project:'project-id',id:'chat-1'}))
    expect(screen.getByText('这个 chat 还没有 Seywo 主线')).toBeInTheDocument()
  })
  it('renders a compact mainline with branches and short titles', async () => {
    const read = vi.fn(async (op: string) => op === 'seywo_project_chats'
      ? {chats: [{id:'chat-1',title:'方案讨论',updated_at:1}]}
      : {available:true,version:4,state:'published_unreviewed',narrative:{items:[{id:'i1',title:'确定目标',content:'先明确目标',evidence:[{event_id:'event-12345678',snapshot_id:'snap-1',start:3,end:8}]},{id:'i2',title:'比较方案',evidence:[]}],phases:[{id:'p1',title:'目标确认'}],branches:[{id:'b1',phase_id:'p1',title:'备选路径',goal:'验证可行性',state:'active'}],placements:[{item_id:'i1',phase_id:'p1',branch_id:null},{item_id:'i2',phase_id:'p1',branch_id:'b1'}],overview:{focus:{phase_id:'p1',branch_id:'b1'},goal_item_ids:['i1'],next_step_ids:['i2']}}})
    render(<ProjectChats project="project-id" read={read as never} standalone/>)
    expect((await screen.findAllByText('确定目标')).length).toBeGreaterThan(0)
    expect((screen.getAllByText('备选路径')).length).toBeGreaterThan(0)
    expect(screen.getByText('当前')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', {name:/确定目标/}))
    expect(screen.getByText('先明确目标')).toBeInTheDocument()
    expect(screen.getByText(/来源 12345678/)).toBeInTheDocument()
  })
})

it('progressively reveals nodes, keeps branch membership and opens a separate inspector without rereading', async () => {
  const items = Array.from({length:9}, (_, index) => ({id:`n${index}`, title:`节点 ${index}`, content:`正文 ${index}`, evidence:[]}))
  const read = vi.fn(async (op: string) => op === 'seywo_project_chats'
    ? {chats:[{id:'one',title:'设计讨论',updated_at:1}]}
    : {available:true,version:1,state:'needs_review',source_changed:true,gaps:[{reason:'pending',n:1200}],narrative:{
      items:[...items, {id:'branch',title:'支线节点'}, {id:'hidden',title:'隐藏节点',hidden:true}],
      phases:[{id:'p1',title:'设计阶段'}, {id:'p2',title:'后续阶段'}],
      branches:[{id:'b1',phase_id:'p1',title:'方案验证',state:'waiting'}],
      placements:[...items.map(item => ({item_id:item.id,phase_id:'p1'})), {item_id:'branch',phase_id:'p1',branch_id:'b1'}, {item_id:'hidden',phase_id:'p1'}],
      overview:{focus:{phase_id:'p1'}},
    }})
  render(<ProjectChats project="project" read={read as never} standalone/> )
  await screen.findByRole('button', {name:'节点 0'})
  expect(screen.queryByRole('button', {name:'节点 8'})).not.toBeInTheDocument()
  expect(screen.queryByText('隐藏节点')).not.toBeInTheDocument()
  expect(screen.getByRole('button', {name:'支线节点'}).closest('.seywo-branch')).toHaveTextContent('方案验证')
  expect(screen.getByRole('button', {name:/后续阶段/})).toHaveAttribute('aria-expanded','false')
  fireEvent.click(screen.getByRole('button', {name:'展开其余 3 个节点'}))
  fireEvent.click(screen.getByRole('button', {name:'节点 8'}))
  expect(screen.getByRole('complementary', {name:'节点详情'})).toHaveTextContent('正文 8')
  fireEvent.click(screen.getByRole('button', {name:'关闭节点详情'}))
  expect(screen.queryByRole('complementary', {name:'节点详情'})).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', {name:'收起节点'}))
  expect(screen.queryByRole('button', {name:'节点 8'})).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', {name:/设计阶段/}))
  expect(screen.queryByRole('button', {name:'节点 0'})).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', {name:/设计阶段/}))
  expect(screen.getByRole('button', {name:'节点 0'})).toBeInTheDocument()
  expect(screen.getByText('1,200 条来源记录未纳入整理，包含无法解析或内容不完整的记录。').closest('details')).not.toHaveAttribute('open')
  expect(read).toHaveBeenCalledTimes(2)
})


it('rereads the selected narrative on window focus so compact and repairs become visible', async () => {
  let version = 0
  const read = vi.fn(async (op: string) => op === 'seywo_project_chats'
    ? {chats:[{id:'one',title:'修正阶段',updated_at:1}]}
    : {available:true,version:++version,narrative:{items:[],phases:[{id:'phase',title:version === 1 ? '旧阶段' : '已修正阶段'}],branches:[],placements:[],overview:{}}})
  const {unmount} = render(<ProjectChats project="project" read={read as never} standalone/>)
  await screen.findByRole('button', {name:/旧阶段/})
  fireEvent.focus(window)
  await screen.findByRole('button', {name:/已修正阶段/})
  expect(read.mock.calls.filter(([op]) => op === 'seywo_project_chats')).toHaveLength(1)
  expect(read.mock.calls.filter(([op]) => op === 'seywo_narrative')).toHaveLength(2)
  unmount()
  fireEvent.focus(window)
  expect(read).toHaveBeenCalledTimes(3)
})


it('shows pending updates without asking to review unchanged nodes', async () => {
  const read = vi.fn(async (op: string) => op === 'seywo_project_chats'
    ? {chats:[{id:'one',title:'设计讨论'}]}
    : {available:true,version:2,state:'update_pending',source_changed:true,update_pending:true,review_count:0,pending_revision_count:0,narrative:{
      items:[{id:'n',title:'已整理节点',review_state:'unreviewed'}],phases:[{id:'p',title:'当前阶段'}],branches:[],placements:[{item_id:'n',phase_id:'p'}],overview:{},
    }})
  render(<ProjectChats project="project" read={read as never} standalone/>)
  expect(await screen.findByText('待更新')).toBeInTheDocument()
  expect(screen.getByText('对话有新增或修改，等待下次整理。')).toBeInTheDocument()
  expect(screen.queryByText('需核对')).not.toBeInTheDocument()
  expect(screen.queryByText(/建议复核/)).not.toBeInTheDocument()
})

it('marks only affected nodes and separates source changes from revision proposals', async () => {
  const read = vi.fn(async (op: string) => op === 'seywo_project_chats'
    ? {chats:[{id:'one',title:'设计讨论'}]}
    : {available:true,version:2,state:'needs_review',source_changed:true,update_pending:true,review_count:1,pending_revision_count:2,narrative:{
      items:[{id:'n',title:'依据变化节点',review_state:'needs_review',review_reason:'dependency_changed'}, {id:'s',title:'稳定节点',review_state:'unreviewed'}],
      phases:[{id:'p',title:'当前阶段'}],branches:[],placements:[{item_id:'n',phase_id:'p'},{item_id:'s',phase_id:'p'}],overview:{},
    }})
  render(<ProjectChats project="project" read={read as never} standalone/>)
  const changed = await screen.findByRole('button', {name:/依据变化节点/})
  expect(changed).toHaveTextContent('需核对')
  expect(screen.getByRole('button', {name:'稳定节点'})).not.toHaveTextContent('需核对')
  expect(screen.getByText('1 个节点的依据或依赖发生变化，需要核对。')).toBeInTheDocument()
  expect(screen.getByText('2 项修订建议待确认。')).toBeInTheDocument()
  fireEvent.click(changed)
  expect(screen.getByRole('complementary', {name:'节点详情'})).toHaveTextContent('依赖的节点有变化，请核对本节点。')
})

it('keeps automatic organization neutral when only mechanical sources changed', async () => {
  const read = vi.fn(async (op: string) => op === 'seywo_project_chats'
    ? {chats:[{id:'one',title:'设计讨论'}]}
    : {available:true,state:'published_unreviewed',source_changed:true,update_pending:false,narrative:{items:[],phases:[],branches:[],placements:[],overview:{}}})
  render(<ProjectChats project="project" read={read as never} standalone/>)
  const label = await screen.findByText('自动整理')
  expect(label.querySelector('.seywo-health-dot')).not.toHaveClass('needs-attention')
  expect(screen.queryByText(/建议复核/)).not.toBeInTheDocument()
})
