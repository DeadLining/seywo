import { render, screen, waitFor } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { TaskWorkspace } from './TaskWorkspace'
import { readSessionGraph } from '../sessionGraph'

vi.mock('../sessionGraph', async importOriginal => {
  const actual = await importOriginal<typeof import('../sessionGraph')>()
  return {...actual, readSessionGraph: vi.fn()}
})

it('loads both the SessionGraph and Seywo project catalogs', async () => {
  const project = {id:'a'.repeat(20), name:'测试项目', root:'/fixture', state:'ready'}
  vi.mocked(readSessionGraph).mockImplementation(async operation => {
    if (operation === 'projects' || operation === 'seywo_projects') return {projects:[project],default_project:project.id} as never
    if (operation === 'project_chats' || operation === 'flow_tasks') return {chats:[],tasks:[]} as never
    if (operation === 'seywo_project_chats') return {chats:[]} as never
    throw new Error(`unexpected read: ${operation}`)
  })
  render(<TaskWorkspace/>)
  await waitFor(() => expect(screen.getAllByText('测试项目').length).toBeGreaterThanOrEqual(1))
  await waitFor(() => {
    const calls = vi.mocked(readSessionGraph).mock.calls.map(([op]) => op)
    expect(calls).toContain('projects')
    expect(calls).toContain('seywo_projects')
  })
  expect(screen.getByRole('tab', {name:'SessionGraph'})).toHaveAttribute('aria-selected', 'true')
  expect(screen.getByRole('button', {name:'调用日志'})).toBeInTheDocument()
  expect(screen.getByRole('button', {name:'设置'})).toBeInTheDocument()
})
