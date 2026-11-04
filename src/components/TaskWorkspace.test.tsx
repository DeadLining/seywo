import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { TaskWorkspace } from './TaskWorkspace'
import { readSessionGraph } from '../sessionGraph'

vi.mock('../sessionGraph', async importOriginal => {
  const actual = await importOriginal<typeof import('../sessionGraph')>()
  return {...actual, readSessionGraph: vi.fn()}
})

it('opens the model call log from the task workspace', async () => {
  const project = {id:'a'.repeat(20), name:'测试项目', root:'/fixture', state:'ready'}
  vi.mocked(readSessionGraph).mockImplementation(async operation => {
    if (operation === 'projects') return {projects:[project],default_project:project.id} as never
    if (operation === 'project_chats') return {chats:[]} as never
    if (operation === 'flow_tasks') return {tasks:[]} as never
    if (operation === 'call_logs') return {items:[],models:[],has_more:false} as never
    throw new Error('unexpected read')
  })
  render(<TaskWorkspace/>)
  await screen.findByText('测试项目')
  expect(vi.mocked(readSessionGraph).mock.calls.some(([op])=>op==='call_logs')).toBe(false)
  fireEvent.click(screen.getByRole('button',{name:'调用日志'}))
  await waitFor(() => expect(vi.mocked(readSessionGraph).mock.calls.some(([op])=>op==='call_logs')).toBe(true))
  expect(screen.getByRole('dialog',{name:'调用日志'})).toBeInTheDocument()
})
