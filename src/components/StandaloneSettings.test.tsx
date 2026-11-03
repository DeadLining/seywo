import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { SessionGraphSettings } from './SessionGraphSettings'
it('saves a direct endpoint without requiring Gateway model catalog and clears the key',async()=>{
  const snapshot={ok:true,model_calls:0,changed_count:0,model:'MiniMax-M3',endpoint:'https://provider.test/v1/responses',runtime_installed:true,hook_registered:true,hook_events:5,hook_event_count:5,hook_enabled:true,mcp_registered:true,mcp_guidance_installed:true,message:''}
  const manage=vi.fn().mockResolvedValue(snapshot)
  render(<SessionGraphSettings project={'a'.repeat(20)} models={[]} manage={manage}/>)
  await waitFor(()=>expect(screen.getByLabelText('MODEL NAME')).toHaveValue('MiniMax-M3'))
  fireEvent.change(screen.getByLabelText('BASE URL'),{target:{value:'https://direct.test/v1'}})
  fireEvent.change(screen.getByLabelText('API KEY'),{target:{value:'fixture-private-key'}})
  fireEvent.click(screen.getByRole('button',{name:'保存模型配置'}))
  await waitFor(()=>expect(manage).toHaveBeenLastCalledWith({action:'save_provider',model:'MiniMax-M3',endpoint:'https://direct.test/v1/responses',api_key_env:'GRAPH_LLM_API_KEY',api_key:'fixture-private-key'}))
  await waitFor(()=>expect(screen.getByLabelText('API KEY')).toHaveValue(''))
  expect(document.body.textContent).not.toContain('fixture-private-key')
})
