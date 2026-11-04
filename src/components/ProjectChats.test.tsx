import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { invoke } from '@tauri-apps/api/core'
import { ProjectChats } from './ProjectChats'

vi.mock('@tauri-apps/api/core', () => ({invoke: vi.fn()}))

describe('project chats', () => {
  it('loads metadata only when opened and generates only the clicked chat', async () => {
    const read = vi.fn(async (op: string) => op === 'project_chats'
      ? {chats: [{id:'chat-1',title:'聊天一',updated_at:1}, {id:'chat-2',title:'聊天二',updated_at:2}]}
      : {tasks: []})
    vi.mocked(invoke).mockResolvedValue({ok:true,saved_nodes:2})
    render(<ProjectChats project="project-id" read={read as never}/>)
    expect(read).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('项目聊天'))
    await screen.findByText('聊天一')
    fireEvent.click(screen.getAllByRole('button', {name:'生成 / 更新'})[0])
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('generate_task_flow', {project:'project-id',chat:'chat-1'}))
    expect(invoke).toHaveBeenCalledTimes(1)
    expect(await screen.findByText(/本次新增 2 个节点/)).toBeInTheDocument()
  })
  it('shows a task and edits its node without launching generation', async () => {
    const read = vi.fn(async (op: string) => op === 'project_chats' ? {chats: []} : {tasks: [
      {id:'t1',title:'工作任务',nodes:[{id:'n1',kind:'decision',label:'采用方案',quote:'我同意',entry_id:'e1',source_time:'1',manual:0}]}
    ]})
    vi.mocked(invoke).mockReset().mockResolvedValue({ok:true})
    render(<ProjectChats project="project-id" read={read as never} standalone/>)
    fireEvent.click(await screen.findByRole('button', {name:/工作任务/}))
    fireEvent.click(screen.getByRole('button', {name:'修改'}))
    fireEvent.change(screen.getByLabelText('节点内容'), {target:{value:'修改后的决定'}})
    fireEvent.click(screen.getByRole('button', {name:'保存'}))
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('edit_task_node', expect.objectContaining({action:'edit',node:'n1',label:'修改后的决定'})))
  })
  it('shows that a filtered chat is unfinished and can be continued', async () => {
    const read = vi.fn(async (op: string) => op === 'project_chats'
      ? {chats:[{id:'chat-1',title:'长会话',updated_at:1,flow_state:'partial',flow_filtered:true}]}
      : {tasks:[]})
    vi.mocked(invoke).mockReset().mockResolvedValue({ok:true,saved_nodes:1,complete:false})
    render(<ProjectChats project="project-id" read={read as never} standalone/>)
    expect(await screen.findByText('上次未处理完')).toBeInTheDocument()
    expect(screen.getByText('部分助手回复未发送给模型')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', {name:'继续生成'}))
    expect(await screen.findByText(/仍有内容未处理/)).toBeInTheDocument()
  })
})
