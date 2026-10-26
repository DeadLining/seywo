import { act, fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { SessionGraphMaintenance } from './SessionGraphMaintenance'
import type { SessionGraphManage } from '../sessionGraph'
const base={ok:true,model_calls:0,changed_count:0,model:'',runtime_installed:true,hook_registered:true,hook_events:4,hook_enabled:true,mcp_registered:true,mcp_guidance_installed:true,message:''}
const before={database_bytes:100*1024*1024,wal_bytes:0,logical_bytes:100*1024*1024,reclaimable_bytes:10*1024*1024}
describe('lossless storage maintenance',()=>{
  it('previews only, then performs an explicit scoped compaction and reports reclaimed space',async()=>{
    const manage=vi.fn(async({action})=>({...base,maintenance:action==='storage_preview'?{before,model_calls:0}:{before,after:{...before,database_bytes:80*1024*1024},reclaimed_bytes:20*1024*1024,model_calls:0}})) as unknown as ReturnType<typeof vi.fn>&SessionGraphManage
    const changed=vi.fn()
    render(<SessionGraphMaintenance project="selected" manage={manage} onChanged={changed}/>)
    await screen.findByText('100.0 MB')
    expect(manage).toHaveBeenCalledTimes(1)
    expect(manage).toHaveBeenCalledWith({action:'storage_preview',project:'selected'})
    fireEvent.click(screen.getByRole('button',{name:'开始压缩'}))
    await screen.findByText(/已回收 20.0 MB/)
    expect(manage).toHaveBeenLastCalledWith({action:'storage_compact',project:'selected'})
    expect(changed).toHaveBeenCalledTimes(1)
  })
  it('shows active-batch failures without claiming successful compaction',async()=>{
    const manage=vi.fn(async({action})=>{if(action==='storage_compact')return {...base,ok:false,message:'项目正在处理批次'};return {...base,maintenance:{before,model_calls:0}}}) as SessionGraphManage
    const changed=vi.fn()
    render(<SessionGraphMaintenance project="p" manage={manage} onChanged={changed}/>)
    await screen.findByText('100.0 MB');fireEvent.click(screen.getByRole('button',{name:'开始压缩'}))
    expect(await screen.findByRole('alert')).toHaveTextContent('项目正在处理批次')
    expect(changed).not.toHaveBeenCalled()
  })
  it('does not refresh another project after switching during maintenance',async()=>{
    let resolve!:(value:typeof base)=>void
    const pending=new Promise<typeof base>(r=>{resolve=r})
    const manage=vi.fn(async({action})=>action==='storage_preview'?{...base,maintenance:{before,model_calls:0}}:pending) as SessionGraphManage
    const changed=vi.fn()
    const view=render(<SessionGraphMaintenance project="old" manage={manage} onChanged={changed}/>)
    await screen.findByText('100.0 MB');fireEvent.click(screen.getByRole('button',{name:'开始压缩'}));view.unmount()
    await act(async()=>resolve(base));expect(changed).not.toHaveBeenCalled()
  })
})
