import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { SessionGraphSettings } from './SessionGraphSettings'
import type { SessionGraphSettingsSnapshot } from '../sessionGraph'
const state:SessionGraphSettingsSnapshot={ok:true,model_calls:0,changed_count:0,model:'old',runtime_installed:true,hook_registered:false,hook_events:0,hook_enabled:false,mcp_registered:false,mcp_guidance_installed:false,message:''}
const options=[{value:'old',label:'原模型'},{value:'new',label:'新模型'}]

describe('Standalone provider fields',()=>{
 const provider={...state,endpoint:'https://example.test/openai/v1/responses',api_key_env:'EXISTING_KEY'}
 it('shows only BASE URL, API KEY and MODEL NAME, preserving existing credentials',async()=>{
  const manage=vi.fn().mockResolvedValue(provider)
  const {container}=render(<SessionGraphSettings project="p1" models={[]} manage={manage}/>);
  await waitFor(()=>expect(screen.getByRole('button',{name:'保存模型配置'})).not.toBeDisabled())
  expect([...container.querySelectorAll('.sg-provider-fields label')].map(e=>e.textContent)).toEqual(['BASE URL','API KEY','MODEL NAME'])
  expect(screen.getByLabelText('BASE URL')).toHaveValue('https://example.test/openai/v1')
  expect(screen.getByLabelText('API KEY')).toHaveAttribute('type','password')
  expect(screen.getByLabelText('API KEY')).toHaveValue('')
  expect(screen.getByLabelText('MODEL NAME')).toHaveValue('old')
  expect(screen.queryByLabelText('密钥环境变量')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:'保存模型配置'}))
  await waitFor(()=>expect(manage).toHaveBeenLastCalledWith({action:'save_provider',model:'old',endpoint:provider.endpoint,api_key_env:'EXISTING_KEY'}))
 })
 it.each(['https://new.test/v1','https://new.test/v1/','https://new.test/v1/responses'])(
  'saves a Responses endpoint from %s and clears the entered key',async(base)=>{
   const manage=vi.fn().mockResolvedValue(provider)
   render(<SessionGraphSettings project="p1" models={[]} manage={manage}/>);
   await waitFor(()=>expect(screen.getByRole('button',{name:'保存模型配置'})).not.toBeDisabled())
   fireEvent.change(screen.getByLabelText('BASE URL'),{target:{value:base}})
   fireEvent.change(screen.getByLabelText('API KEY'),{target:{value:'fixture-key'}})
   fireEvent.change(screen.getByLabelText('MODEL NAME'),{target:{value:'new-model'}})
   fireEvent.click(screen.getByRole('button',{name:'保存模型配置'}))
   await waitFor(()=>expect(manage).toHaveBeenLastCalledWith({action:'save_provider',model:'new-model',endpoint:'https://new.test/v1/responses',api_key_env:'EXISTING_KEY',api_key:'fixture-key'}))
   await waitFor(()=>expect(screen.getByLabelText('API KEY')).toHaveValue(''))
  })
 it('does not normalize an unchanged endpoint bound to an existing key',async()=>{
  const endpoint='https://EXAMPLE.test:443/openai/v1/responses'
  const manage=vi.fn().mockResolvedValue({...provider,endpoint})
  render(<SessionGraphSettings project="p1" models={[]} manage={manage}/>);
  await waitFor(()=>expect(screen.getByRole('button',{name:'保存模型配置'})).not.toBeDisabled())
  fireEvent.click(screen.getByRole('button',{name:'保存模型配置'}))
  await waitFor(()=>expect(manage).toHaveBeenLastCalledWith(expect.objectContaining({endpoint})))
 })
 it('rejects malformed BASE URL without submitting or losing the form',async()=>{
  const manage=vi.fn().mockResolvedValue(provider)
  render(<SessionGraphSettings project="p1" models={[]} manage={manage}/>);
  await waitFor(()=>expect(screen.getByRole('button',{name:'保存模型配置'})).not.toBeDisabled())
  fireEvent.change(screen.getByLabelText('BASE URL'),{target:{value:'invalid'}})
  fireEvent.click(screen.getByRole('button',{name:'保存模型配置'}))
  expect(await screen.findByRole('alert')).toHaveTextContent('请输入有效的 BASE URL')
  expect(manage).toHaveBeenCalledTimes(1)
  expect(screen.getByLabelText('BASE URL')).toHaveValue('invalid')
 })
})

describe('SessionGraph real settings',()=>{
 it('reads only on opening and keeps model saving separate from installation',async()=>{
  const manage=vi.fn().mockResolvedValue(state)
  render(<SessionGraphSettings project="p1" models={options} manage={manage}/>);await screen.findByLabelText('记忆模型')
  await waitFor(()=>expect(screen.getByRole('button',{name:'保存模型'})).not.toBeDisabled())
  expect(manage).toHaveBeenCalledTimes(1);expect(manage).toHaveBeenCalledWith({action:'status',project:'p1'})
  fireEvent.change(screen.getByLabelText('记忆模型'),{target:{value:'new'}})
  expect(manage).toHaveBeenCalledTimes(1)
  manage.mockResolvedValue({...state,model:'new',message:'模型已保存'})
  fireEvent.click(screen.getByRole('button',{name:'保存模型'}));await screen.findByText('模型已保存')
  expect(manage).toHaveBeenLastCalledWith({action:'save_model',model:'new'})
 })
 it('installs hooks and MCP independently without claiming trusted',async()=>{
  const manage=vi.fn().mockResolvedValue(state)
  render(<SessionGraphSettings project="p1" models={options} manage={manage}/>);
  const hook=await screen.findByRole('button',{name:'自动安装 Hook'});await waitFor(()=>expect(hook).not.toBeDisabled())
  manage.mockResolvedValue({...state,hook_registered:true,hook_enabled:true,hook_events:5,hook_event_count:5})
  fireEvent.click(hook);await screen.findByText('已注册 · 5/5 事件')
  expect(manage).toHaveBeenLastCalledWith({action:'install_hooks',project:'p1'})
  expect(screen.getByText(/信任/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:'安装 MCP'}))
  await waitFor(()=>expect(manage).toHaveBeenLastCalledWith({action:'install_mcp',project:'p1'}))
 })
 it('shows current missing model but disallows saving it unless configured',async()=>{
  const manage=vi.fn().mockResolvedValue({...state,model:'removed'})
  render(<SessionGraphSettings project="p1" models={options} manage={manage}/>);
  await screen.findByText('removed（不在当前可用模型中）')
  expect(screen.getByRole('button',{name:'保存模型'})).toBeDisabled()
 })
 it('shows errors without claiming successful registration and allows recheck',async()=>{
  const manage=vi.fn().mockResolvedValueOnce({ok:false,model_calls:0,message:'配置冲突'}).mockResolvedValue(state)
  render(<SessionGraphSettings project="p1" models={options} manage={manage}/>);
  await screen.findByRole('alert');expect(screen.queryByText('已注册')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:'重新检查'}))
  await waitFor(()=>expect(screen.queryByRole('alert')).not.toBeInTheDocument())
 })
 it('allows global provider editing without a selected project, without polling',async()=>{
  const manage=vi.fn().mockResolvedValue({...state,model:''})
  render(<SessionGraphSettings project="" models={[]} manage={manage}/>);
  await screen.findByText(/请选择已初始化项目/)
  await act(async()=>{await new Promise(resolve=>setTimeout(resolve,20))})
  expect(manage).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('button',{name:'自动安装 Hook'})).toBeDisabled()
  expect(screen.getByLabelText('BASE URL')).not.toBeDisabled()
  fireEvent.change(screen.getByLabelText('BASE URL'),{target:{value:'https://app.example/v1'}})
  fireEvent.change(screen.getByLabelText('MODEL NAME'),{target:{value:'shared'}})
  fireEvent.click(screen.getByRole('button',{name:'保存模型配置'}))
  await waitFor(()=>expect(manage).toHaveBeenLastCalledWith({action:'save_provider',model:'shared',endpoint:'https://app.example/v1/responses',api_key_env:'GRAPH_LLM_API_KEY'}))
 })
 it('blocks duplicate actions and ignores a late response after unmount',async()=>{
  let resolve!:(v:SessionGraphSettingsSnapshot)=>void
  const manage=vi.fn().mockResolvedValueOnce(state).mockImplementation(()=>new Promise(r=>{resolve=r}))
  const view=render(<SessionGraphSettings project="p1" models={options} manage={manage}/>);
  const button=await screen.findByRole('button',{name:'安装 MCP'});await waitFor(()=>expect(button).not.toBeDisabled())
  fireEvent.click(button);fireEvent.click(button);expect(manage).toHaveBeenCalledTimes(2)
  view.unmount();await act(async()=>resolve({...state,mcp_registered:true}))
 })
})

it('distinguishes registered server from missing AGENTS guidance, then reports both installed',async()=>{
 const manage=vi.fn().mockResolvedValue({...state,mcp_registered:true})
 render(<SessionGraphSettings project="p1" models={options} manage={manage}/>);
 await screen.findByText('服务已注册 · 使用指引未安装')
 manage.mockResolvedValue({...state,mcp_registered:true,mcp_guidance_installed:true})
 fireEvent.click(screen.getByRole('button',{name:'安装 MCP'}))
 await screen.findByText('已安装 · 服务与使用指引就绪')
 expect(screen.getByText(/AGENTS.md/)).toBeInTheDocument()
})

it('requires confirmation and supports independent global uninstall without project',async()=>{
 const manage=vi.fn().mockResolvedValue({...state,hook_registered:true,hook_events:4,mcp_registered:true,mcp_guidance_installed:true})
 render(<SessionGraphSettings project="" models={[]} manage={manage}/>);
 const hook=await screen.findByRole('button',{name:'卸载 Hook'});await waitFor(()=>expect(hook).not.toBeDisabled())
 fireEvent.click(hook);expect(manage).toHaveBeenCalledTimes(1)
 fireEvent.click(screen.getByRole('button',{name:'取消'}));expect(manage).toHaveBeenCalledTimes(1)
 fireEvent.click(screen.getByRole('button',{name:'卸载 MCP'}))
 fireEvent.click(screen.getByRole('button',{name:'确认卸载'}))
 await waitFor(()=>expect(manage).toHaveBeenLastCalledWith({action:'uninstall_mcp'}))
 fireEvent.click(screen.getByRole('button',{name:'卸载 Hook'}))
 fireEvent.click(screen.getByRole('button',{name:'确认卸载'}))
 await waitFor(()=>expect(manage).toHaveBeenLastCalledWith({action:'uninstall_hooks'}))
})
it('keeps a saved raw model usable without silently changing its routing',async()=>{
 const manage=vi.fn().mockResolvedValue({...state,model:'raw'})
 const models=[{value:'alias',label:'Friendly · Provider',upstreamModel:'raw'}]
 render(<SessionGraphSettings project="p1" models={models} manage={manage}/>);
 const select=await screen.findByLabelText('记忆模型')
 await waitFor(()=>expect(screen.getByRole('button',{name:'保存模型'})).not.toBeDisabled())
 expect(select).toHaveValue('raw')
 expect(screen.getByRole('option',{name:'Friendly · Provider'})).toHaveValue('alias')
 expect(select.querySelector('option[value="raw"]')).toHaveTextContent('当前配置')
 fireEvent.click(screen.getByRole('button',{name:'保存模型'}))
 await waitFor(()=>expect(manage).toHaveBeenLastCalledWith({action:'save_model',model:'raw'}))
 fireEvent.change(select,{target:{value:'alias'}})
 fireEvent.click(screen.getByRole('button',{name:'保存模型'}))
 await waitFor(()=>expect(manage).toHaveBeenLastCalledWith({action:'save_model',model:'alias'}))
})


it('shares the selected model with judgment and needs no Jev credentials', async () => {
 const manage=vi.fn().mockResolvedValue(state)
 render(<SessionGraphSettings project="p1" models={options} manage={manage}/>);
 await screen.findByLabelText('记忆模型')
 expect(screen.queryByLabelText('Jev API Key')).not.toBeInTheDocument()
 expect(screen.getByLabelText('记忆模型')).toHaveValue('old')
 await waitFor(()=>expect(screen.getByRole('button',{name:'启用 v2'})).not.toBeDisabled())
 manage.mockResolvedValue({...state,graph_v2_enabled:true})
 fireEvent.click(screen.getByRole('button',{name:'启用 v2'}))
 await screen.findByRole('button',{name:'停用 v2'})
 expect(manage).toHaveBeenLastCalledWith({action:'enable_graph',project:'p1'})
 fireEvent.click(screen.getByRole('button',{name:'停用 v2'}))
 await waitFor(()=>expect(manage).toHaveBeenLastCalledWith({action:'disable_graph',project:'p1'}))
})

it('tests draft configuration without saving or clearing edited credentials, and prevents duplicate requests',async()=>{
 const provider={...state,endpoint:'https://saved.test/v1/responses',api_key_env:'EXISTING_KEY'}
 let finish!:(value:SessionGraphSettingsSnapshot)=>void
 const manage=vi.fn().mockResolvedValueOnce(provider).mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve}))
 render(<SessionGraphSettings project="p1" models={[]} manage={manage}/>);
 await waitFor(()=>expect(screen.getByRole('button',{name:'测试模型'})).not.toBeDisabled())
 fireEvent.change(screen.getByLabelText('BASE URL'),{target:{value:'https://draft.test/v1'}})
 fireEvent.change(screen.getByLabelText('MODEL NAME'),{target:{value:'draft-model'}})
 fireEvent.change(screen.getByLabelText('API KEY'),{target:{value:'fixture-draft-key'}})
 fireEvent.click(screen.getByRole('button',{name:'测试模型'}))
 expect(manage).toHaveBeenLastCalledWith({action:'test_provider',endpoint:'https://draft.test/v1/responses',model:'draft-model',api_key_env:'EXISTING_KEY',api_key:'fixture-draft-key'})
 expect(screen.getByRole('button',{name:'测试中…'})).toBeDisabled()
 expect(screen.getByRole('button',{name:'保存模型配置'})).toBeDisabled()
 await act(async()=>finish({...provider,message:'模型已连通 · 总耗时 123 ms',model_calls:1}))
 expect(await screen.findByText('模型已连通 · 总耗时 123 ms')).toBeInTheDocument()
 expect(screen.getByLabelText('MODEL NAME')).toHaveValue('draft-model')
 expect(screen.getByLabelText('BASE URL')).toHaveValue('https://draft.test/v1')
 expect(screen.getByLabelText('API KEY')).toHaveValue('fixture-draft-key')
 fireEvent.change(screen.getByLabelText('MODEL NAME'),{target:{value:'other-model'}})
 expect(screen.queryByText('模型已连通 · 总耗时 123 ms')).not.toBeInTheDocument()
})

it('tests with stored credentials when the key is blank, and shows failure with elapsed time',async()=>{
 const provider={...state,endpoint:'https://EXAMPLE.test:443/v1/responses',api_key_env:'EXISTING_KEY'}
 const manage=vi.fn().mockResolvedValueOnce(provider).mockResolvedValueOnce({...provider,ok:false,message:'测试失败：鉴权失败（HTTP 401） · 总耗时 87 ms',model_calls:1})
 render(<SessionGraphSettings project="" models={[]} manage={manage}/>);
 await waitFor(()=>expect(screen.getByRole('button',{name:'测试模型'})).not.toBeDisabled())
 fireEvent.click(screen.getByRole('button',{name:'测试模型'}))
 expect(await screen.findByRole('alert')).toHaveTextContent('鉴权失败（HTTP 401） · 总耗时 87 ms')
 expect(manage).toHaveBeenLastCalledWith({action:'test_provider',endpoint:provider.endpoint,model:'old',api_key_env:'EXISTING_KEY'})
 expect(screen.getByLabelText('BASE URL')).toHaveValue('https://example.test/v1')
})
