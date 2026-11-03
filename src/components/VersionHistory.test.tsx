import { fireEvent, render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'
import { VersionHistory } from './VersionHistory'
import { APP_VERSION, CHANGELOG } from '../changelog'

it('shows the package version and opens the changelog', () => {
  render(<VersionHistory />)
  expect(screen.getByText(APP_VERSION)).toBeInTheDocument()
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()

  fireEvent.click(screen.getByRole('button', { name: '更新日志' }))

  expect(screen.getByRole('dialog', { name: 'SessionGraph 版本更新日志' })).toBeInTheDocument()
  expect(screen.getByText(`当前版本 ${APP_VERSION}`)).toBeInTheDocument()
  expect(screen.getByText(CHANGELOG[0].items[0])).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '关闭更新日志' })).toHaveFocus()
})

it('closes with Escape, the close button and a backdrop click', () => {
  render(<VersionHistory />)
  const trigger = screen.getByRole('button', { name: '更新日志' })

  fireEvent.click(trigger)
  fireEvent.keyDown(document, { key: 'Escape' })
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(trigger).toHaveFocus()

  fireEvent.click(trigger)
  fireEvent.click(screen.getByRole('button', { name: '关闭更新日志' }))
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()

  fireEvent.click(trigger)
  fireEvent.click(document.querySelector('.app-changelog-backdrop')!)
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})
