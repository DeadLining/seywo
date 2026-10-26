import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { RenderErrorBoundary } from './RenderErrorBoundary'

afterEach(() => vi.restoreAllMocks())
it('isolates a broken panel, shows its error and recovers on explicit retry', () => {
  const log = vi.spyOn(console, 'error').mockImplementation(() => {})
  let broken = true
  function Panel() {
    if (broken) throw new Error('Missing task statistic')
    return <p>图谱已恢复</p>
  }
  render(<><nav>其他功能导航</nav><RenderErrorBoundary name="项目记忆"><Panel /></RenderErrorBoundary></>)
  expect(screen.getByRole('alert')).toHaveTextContent('Missing task statistic')
  expect(screen.getByText('其他功能导航')).toBeInTheDocument()
  expect(log).toHaveBeenCalled()
  broken = false
  fireEvent.click(screen.getByRole('button', { name: '重试显示' }))
  expect(screen.getByText('图谱已恢复')).toBeInTheDocument()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})
