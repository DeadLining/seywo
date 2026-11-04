import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { SessionGraphCallLogs } from './SessionGraphCallLogs'
import { SessionGraphPanel } from './SessionGraphPanel'
import type { SessionGraphRead, ModelCallPage, ModelCall } from '../sessionGraph'

const projects=[{id:'a'.repeat(20),name:'果蝇',root:'/fixture/project',state:'ready'}]
const item={id:'request-one',project:projects[0].id,model:'fixture-model',response_model:'returned-model',endpoint:'https://provider.test/v1/responses',chat_id:'chat-fixture',run_id:'run-fixture',stage:'extract',state:'validation_failed',created_at:1793664000,elapsed_ms:321,http_status:200,input_tokens:null,output_tokens:8,error_code:'graph_validation'} as ModelCall
const page:ModelCallPage={items:[item],models:['fixture-model'],offset:0,has_more:true,retention_days:7,max_bytes:20*1024*1024,model_calls:0}

describe('model call log dialog content',()=>{
 it('reads only on demand, displays validation failure despite HTTP 200 and keeps missing usage unknown',async()=>{
  const read=vi.fn().mockResolvedValue(page)
  render(<SessionGraphCallLogs project={projects[0].id} projects={projects} read={read as SessionGraphRead}/>)
  await screen.findByText('内容校验失败')
  expect(screen.getByText(/Token 输入 未知 \/ 输出 8/)).toBeInTheDocument()
  expect(screen.getByText('200')).toBeInTheDocument()
  expect(read).toHaveBeenCalledExactlyOnceWith('call_logs',{project:projects[0].id,logModel:'',logStatus:'all',offset:0})
 })
 it('filters project, model and result and resets pagination',async()=>{
  const read=vi.fn().mockResolvedValue(page)
  render(<SessionGraphCallLogs project={projects[0].id} projects={projects} read={read as SessionGraphRead}/>)
  await screen.findByText('内容校验失败')
  fireEvent.click(screen.getByRole('button',{name:'下一页'}))
  await waitFor(()=>expect(read).toHaveBeenLastCalledWith('call_logs',expect.objectContaining({offset:50})))
  fireEvent.change(screen.getByLabelText('模型'),{target:{value:'fixture-model'}})
  await waitFor(()=>expect(read).toHaveBeenLastCalledWith('call_logs',expect.objectContaining({logModel:'fixture-model',offset:0})))
  fireEvent.change(screen.getByLabelText('结果'),{target:{value:'failed'}})
  await waitFor(()=>expect(read).toHaveBeenLastCalledWith('call_logs',expect.objectContaining({logStatus:'failed'})))
  fireEvent.change(screen.getByLabelText('项目'),{target:{value:''}})
  await waitFor(()=>expect(read).toHaveBeenLastCalledWith('call_logs',{logModel:'',logStatus:'failed',offset:0}))
 })
 it('does not show stale rows after changing scope or unmounting',async()=>{
  let resolve!:(p:ModelCallPage)=>void
  const pending=new Promise<ModelCallPage>(r=>{resolve=r})
  const read=vi.fn().mockReturnValueOnce(pending).mockResolvedValue({...page,items:[],has_more:false})
  const view=render(<SessionGraphCallLogs project={projects[0].id} projects={projects} read={read as SessionGraphRead}/>)
  fireEvent.change(screen.getByLabelText('项目'),{target:{value:''}})
  await screen.findByText(/当前范围暂无调用记录/)
  await act(async()=>resolve(page))
  expect(screen.queryByText('内容校验失败')).not.toBeInTheDocument()
  view.unmount()
 })
 it('reports read failures without claiming that logs are empty',async()=>{
  const read=vi.fn().mockRejectedValue(new Error('日志读取失败'))
  render(<SessionGraphCallLogs project={projects[0].id} projects={projects} read={read as SessionGraphRead}/>)
  expect(await screen.findByRole('alert')).toHaveTextContent('日志读取失败')
  expect(screen.queryByText(/当前范围暂无调用记录/)).not.toBeInTheDocument()
 })
 it('opens from the graph header as a modal and restores focus on close',async()=>{
  const read=vi.fn(async op=>op==='projects'?{projects,default_project:projects[0].id}:op==='call_logs'?page:op==='status'?{runs:[],active_count:0}:{nodes:[],edges:[],chats:[],stats:{}}) as SessionGraphRead
  render(<SessionGraphPanel read={read}/>)
  await screen.findByText('/fixture/project')
  expect(vi.mocked(read).mock.calls.some(([op])=>op==='call_logs')).toBe(false)
  const trigger=screen.getByRole('button',{name:'调用日志'})
  fireEvent.click(trigger)
  const dialog=screen.getByRole('dialog',{name:'调用日志'})
  expect(dialog).toContainElement(await screen.findByText('内容校验失败'))
  fireEvent.click(screen.getByRole('button',{name:'关闭调用日志'}))
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(trigger).toHaveFocus()
 })
})
