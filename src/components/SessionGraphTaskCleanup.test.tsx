import { act, fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { SessionGraphTaskCleanup } from './SessionGraphTaskCleanup'
import type { SessionGraphManage, SessionGraphSettingsSnapshot } from '../sessionGraph'

const response={ok:true,maintenance:{dismissed_tasks:1,archived_runs:3,model_calls:0}} as SessionGraphSettingsSnapshot

describe('SessionGraphTaskCleanup',()=>{
  it('cleans only the selected project on explicit click and refreshes',async()=>{
    const manage=vi.fn().mockResolvedValue(response),changed=vi.fn()
    render(<SessionGraphTaskCleanup project="jev" manage={manage} onChanged={changed}/>)
    expect(manage).not.toHaveBeenCalled()
    expect(screen.getByText(/保留记忆与原文/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button',{name:'清理失效任务'}))
    await screen.findByText(/已清理 1 个受阻任务，归档 3 条失败或中断记录/)
    expect(manage).toHaveBeenCalledExactlyOnceWith({action:'clear_tasks',project:'jev'})
    expect(changed).toHaveBeenCalledOnce()
  })
  it('reports failure and does not claim success',async()=>{
    const manage=vi.fn().mockRejectedValue(new Error('项目正在处理批次')),changed=vi.fn()
    render(<SessionGraphTaskCleanup project="jev" manage={manage} onChanged={changed}/>)
    fireEvent.click(screen.getByRole('button',{name:'清理失效任务'}))
    expect(await screen.findByRole('alert')).toHaveTextContent('项目正在处理批次')
    expect(changed).not.toHaveBeenCalled()
  })
  it('does not refresh a different project after unmount',async()=>{
    let resolve!:(value:SessionGraphSettingsSnapshot)=>void
    const manage:SessionGraphManage=()=>new Promise(r=>{resolve=r}),changed=vi.fn()
    const view=render(<SessionGraphTaskCleanup project="jev" manage={manage} onChanged={changed}/>)
    fireEvent.click(screen.getByRole('button',{name:'清理失效任务'}))
    expect(screen.getByRole('button',{name:'清理中…'})).toBeDisabled()
    view.unmount();await act(async()=>resolve(response))
    expect(changed).not.toHaveBeenCalled()
  })
})
