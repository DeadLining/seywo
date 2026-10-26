import { invoke } from '@tauri-apps/api/core'
import { describe, expect, it, vi } from 'vitest'
import { graphLayout, graphWindow, readSessionGraph, readPreferences, savePreferences, sessionGraphModelOptions, manageSessionGraph } from './sessionGraph'

describe('SessionGraph deterministic graph view',()=>{
  const nodes=Array.from({length:100},(_,i)=>({id:`n${i}`,type:'memory',kind:'finding',label:`Node ${i}`}))
  const edges=nodes.slice(1).map(n=>({source:'n0',target:n.id,kind:'SUPPORTS'}))
  it('bounds graph nodes and never manufactures edges',()=>{
    const view=graphWindow(nodes,edges,'n0')
    expect(view.nodes.length).toBeLessThanOrEqual(24)
    expect(view.omitted).toBe(76)
    expect(view.edges.every(e=>edges.includes(e))).toBe(true)
    expect(view.edges.every(e=>view.nodes.some(n=>n.id===e.source)&&view.nodes.some(n=>n.id===e.target))).toBe(true)
  })
  it('preserves isolated nodes without adding relationships',()=>{
    const view=graphWindow(nodes,[],'n99'); expect(view.nodes.some(n=>n.id==='n99')).toBe(true); expect(view.edges).toEqual([])
  })
  it('falls back safely for corrupt and unknown-version preferences',()=>{
    localStorage.setItem('gateway.sessiongraph.v1','bad'); expect(readPreferences()).toEqual({version:1,project:''})
    localStorage.setItem('gateway.sessiongraph.v1',JSON.stringify({version:2,enabled:false})); expect(readPreferences()).toEqual({version:1,project:''})
    localStorage.setItem('gateway.sessiongraph.v1',JSON.stringify({version:1,enabled:false,project:'p1'})); expect(readPreferences()).toEqual({version:1,project:'p1'})
    savePreferences({version:1,project:'p2'}); expect(readPreferences()).toEqual({version:1,project:'p2'})
  })
})

it('lays out empty, dangling and connected graphs deterministically',()=>{
  expect(graphLayout([],[]).size).toBe(0)
  const nodes=[{id:'a',type:'memory',kind:'finding',label:'A'},{id:'b',type:'reference',kind:'path',label:'B'}]
  const edges=[{source:'a',target:'b',kind:'MENTIONS'},{source:'a',target:'missing',kind:'MENTIONS'}]
  expect(graphLayout(nodes,edges)).toEqual(graphLayout(nodes,edges))
  expect(graphWindow(nodes,edges).edges).toHaveLength(1)
  expect(graphWindow(nodes,[], 'missing').nodes).toHaveLength(2)
})
it('keeps preference failures non-fatal',()=>{
  const get=vi.spyOn(Storage.prototype,'getItem').mockImplementation(()=>{throw new Error('denied')})
  expect(readPreferences()).toEqual({version:1,project:''});get.mockRestore()
  const set=vi.spyOn(Storage.prototype,'setItem').mockImplementation(()=>{throw new Error('denied')})
  expect(()=>savePreferences({version:1,project:''})).not.toThrow();set.mockRestore()
})
it('uses the typed read-only Tauri command',async()=>{
  vi.stubGlobal('__TAURI_INTERNALS__',{})
  try {
    await readSessionGraph('projects')
    expect(invoke).toHaveBeenCalledWith('read_session_graph',{operation:'projects'})
  } finally { vi.unstubAllGlobals() }
})

it('uses the opt-in development read bridge only outside Tauri and rejects model calls',async()=>{
  const fetch=vi.fn().mockResolvedValue({ok:true,json:async()=>({projects:[],model_calls:0})})
  vi.stubGlobal('fetch',fetch)
  try {
    await expect(readSessionGraph('projects')).resolves.toEqual({projects:[],model_calls:0})
    expect(fetch).toHaveBeenCalledWith('/__sessiongraph/read',expect.objectContaining({method:'POST',body:'{"operation":"projects"}'}))
    fetch.mockResolvedValue({ok:true,json:async()=>({model_calls:1})})
    await expect(readSessionGraph('projects')).rejects.toThrow('本地只读开发连接不可用')
  } finally { vi.unstubAllGlobals() }
})

vi.mock('@tauri-apps/api/core',()=>({invoke:vi.fn().mockResolvedValue({})}))

it('uses the attribution model label and request name without a second raw-ID list',()=>{
 expect(sessionGraphModelOptions([
  {model:'raw',name:'Primary',provider:'A',protocol:'openai'},
  {model:'raw',name:'Backup',provider:'B',protocol:'openai'},
  {model:'raw',name:'Primary',provider:'C',protocol:'openai'},
  {model:'plain',provider:''},
  {model:'off',provider:'D',status:'停用'},
  {model:'moa',provider:'E',protocol:'moa'},
 ])).toEqual([
  {value:'Primary',label:'Primary · A',upstreamModel:'raw'},
  {value:'Backup',label:'Backup · B',upstreamModel:'raw'},
  {value:'plain',label:'plain · 未命名供应商',upstreamModel:'plain'},
 ])
})
it('keeps explicit settings writes separate from the read-only bridge',async()=>{
 await manageSessionGraph({action:'install_mcp',project:'p1'})
 expect(invoke).toHaveBeenCalledWith('manage_session_graph',{request:{action:'install_mcp',project:'p1'}})
})
