import { useEffect, useRef, useState } from 'react'
import { manageSessionGraph, type SessionGraphManage, type SessionGraphSettingsSnapshot, type SessionGraphSettingsAction, type SessionGraphModelOption } from '../sessionGraph'

type UninstallAction = 'uninstall_hooks' | 'uninstall_mcp'

function baseUrlFromEndpoint(endpoint: string) {
  try {
    const url = new URL(endpoint)
    url.pathname = url.pathname.replace(/\/responses\/?$/, '')
    return url.href.replace(/\/$/, '')
  } catch { return endpoint }
}

function endpointFromBaseUrl(baseUrl: string) {
  let url: URL
  try { url = new URL(baseUrl.trim()) } catch { throw new Error('请输入有效的 BASE URL') }
  url.pathname = url.pathname.replace(/\/+$/, '')
  if (!url.pathname.endsWith('/responses')) url.pathname += '/responses'
  return url.href
}

export function SessionGraphSettings({ project, models, manage = manageSessionGraph }: {
  project: string; models: SessionGraphModelOption[]; manage?: SessionGraphManage
}) {
  const [snapshot, setSnapshot] = useState<SessionGraphSettingsSnapshot | null>(null)
  const [model, setModel] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [keyEdited, setKeyEdited] = useState(false)
  const [showKey, setShowKey] = useState(false)
  const [revealedKey, setRevealedKey] = useState<string|null>(null)
  const [busy, setBusy] = useState(true)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ok:boolean;message:string} | null>(null)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [confirm, setConfirm] = useState<UninstallAction | null>(null)
  const active = useRef(true), pending = useRef(false)

  async function run(action: SessionGraphSettingsAction) {
    if (pending.current) return
    pending.current = true
    setBusy(true); setTesting(action === 'test_provider'); setTestResult(null); setError(''); setMessage(''); setConfirm(null)
    const removing = action === 'uninstall_hooks' || action === 'uninstall_mcp'
    const providerRequest = action === 'save_provider' || action === 'test_provider'
    const globalProvider = action === 'save_model' || providerRequest
    try {
      // Preserve the exact stored endpoint so existing endpoint-bound keys still match.
      const endpoint = providerRequest
        ? (snapshot?.endpoint && baseUrl.trim() === baseUrlFromEndpoint(snapshot.endpoint)
          ? snapshot.endpoint : endpointFromBaseUrl(baseUrl))
        : undefined
      const result = await manage({ action, ...(project && !removing && (!globalProvider || action === 'test_provider') ? { project } : {}), ...(globalProvider ? { model } : {}), ...(providerRequest ? {endpoint,api_key_env:snapshot?.api_key_env || 'GRAPH_LLM_API_KEY',...(apiKey?{api_key:apiKey}:{})} : {}) })
      if (!active.current) return
      if (!result.ok) throw new Error(result.message || '设置操作失败')
      if (action === 'test_provider') { setTestResult({ok:true,message:result.message}); return }
      if (action === 'reveal_provider_key') {
        if (result.endpoint !== snapshot?.endpoint) throw new Error('模型配置已变更，请重新检查。')
        if (!result.api_key) throw new Error('没有可显示的已保存 API KEY。')
        setRevealedKey(result.api_key); setShowKey(true); return
      }
      setSnapshot(result)
      if (!removing) { setModel(result.model); setBaseUrl(baseUrlFromEndpoint(result.endpoint || '')) }
      setApiKey(''); setKeyEdited(false); setShowKey(false); setRevealedKey(null)
      setMessage(result.message)
    } catch (e) {
      if (active.current) {
        const message = e instanceof Error ? e.message : String(e)
        if (action === 'test_provider') setTestResult({ok:false,message})
        else setError(message)
      }
    } finally {
      pending.current = false
      if (active.current) { setBusy(false); setTesting(false) }
    }
  }
  useEffect(() => { active.current = true; void run('status'); return () => { active.current = false } }, [])
  const savedKeyAvailable = Boolean(snapshot?.api_key_configured && baseUrl.trim() === baseUrlFromEndpoint(snapshot.endpoint || ''))
  const displayedKey = keyEdited ? apiKey : savedKeyAvailable ? revealedKey ?? '' : ''
  const exact = models.find(m => m.value === model)
  // Keep an existing raw-ID route unchanged until the user explicitly chooses an alias.
  const legacy = !exact && models.find(m => m.upstreamModel === model)
  const available = Boolean(exact || legacy)

  return <section className="sg-settings-card" aria-label="项目记忆设置" aria-busy={busy}>
    <div className="sg-settings-heading"><h3>设置</h3><button type="button" disabled={busy} onClick={() => void run('status')}>重新检查</button></div>
    {models.length > 0 && <label htmlFor="sg-extraction-model">记忆模型 <small>全局</small></label>}
    {models.length > 0 ? <><select id="sg-extraction-model" aria-label="记忆模型" value={model} disabled={busy} onChange={e => setModel(e.target.value)}>
      {!model && <option value="">选择模型</option>}
      {legacy && <option value={model} hidden>{legacy.label}（当前配置）</option>}
      {model && !available && <option value={model}>{model}（不在当前可用模型中）</option>}
      {models.map(m => <option key={m.value} value={m.value}>{m.label}</option>)}
    </select>
    <button type="button" disabled={busy || !available || !snapshot} onClick={() => void run('save_model')}>保存模型</button>
    </> : <div className="sg-provider-fields">
      <label htmlFor="sg-base-url">BASE URL</label><input id="sg-base-url" value={baseUrl} type="url" disabled={busy} onChange={e=>{setBaseUrl(e.target.value);setShowKey(false);setRevealedKey(null);setTestResult(null)}} />
      <label htmlFor="sg-api-key">API KEY</label><div className="sg-api-key-field">
        <input id="sg-api-key" type={showKey?'text':'password'} autoComplete="off" spellCheck={false} placeholder={savedKeyAvailable&&!keyEdited?'••••••••':'请输入 API KEY（留空沿用现有凭据）'} value={displayedKey} disabled={busy}
          onChange={e=>{setApiKey(e.target.value);setKeyEdited(true);setRevealedKey(null);setTestResult(null)}} />
        <button type="button" aria-label={showKey?'隐藏 API KEY':'显示 API KEY'} aria-pressed={showKey} disabled={busy||!(apiKey||savedKeyAvailable&&!keyEdited)}
          onClick={()=>{if(showKey){setShowKey(false);setRevealedKey(null)}else if(keyEdited){setShowKey(true)}else{void run('reveal_provider_key')}}}>{showKey?'隐藏':'显示'}</button>
      </div>
      <label htmlFor="sg-model-id">MODEL NAME</label><input id="sg-model-id" value={model} disabled={busy} onChange={e=>{setModel(e.target.value);setTestResult(null)}} />
      <div className="sg-provider-actions">
        <button type="button" disabled={busy || !model.trim() || !baseUrl.trim() || !snapshot} onClick={()=>void run('test_provider')}>{testing ? '测试中…' : '测试模型'}</button>
        <button type="button" disabled={busy || !model.trim() || !baseUrl.trim() || !snapshot} onClick={()=>void run('save_provider')}>保存模型配置</button>
      </div>
      <small className="sg-provider-hint">测试当前填写的配置，不保存；API KEY 留空沿用现有凭据。耗时包含网络与模型响应。</small>
      {testing && <p className="sg-provider-result" role="status">正在测试模型连通性与延迟…</p>}
      {testResult && <p className={`sg-provider-result ${testResult.ok ? '' : 'sg-error'}`} role={testResult.ok ? 'status' : 'alert'}>{testResult.message}</p>}
    </div>}
    {!project && <p>请选择已初始化项目，再安装 Hook 或 MCP。</p>}
    <div className="sg-install-row">
      <strong>对象图谱 v2</strong>
      <span>{snapshot?.graph_v2_enabled ? '已启用：新内容自动抽取、判断与去重' : '未启用：当前使用旧抽取路径'}</span>
      <div className="sg-settings-actions">
        <button type="button" disabled={busy || !project || !snapshot} onClick={()=>void run(snapshot?.graph_v2_enabled ? 'disable_graph' : 'enable_graph')}>{snapshot?.graph_v2_enabled ? '停用 v2' : '启用 v2'}</button>
      </div>
      <p>所选模型抽取对象与关系，并判断身份和证据；无需另配 Jev。证据不足时保留待确认，读取和检索不调用模型。</p>
    </div>
    <div className="sg-install-row">
      <strong>Compact / 恢复 Hook <small>Codex 全局</small></strong>
      <span>{snapshot ? (snapshot.hook_registered ? `已注册 · ${snapshot.hook_event_count??5}/${snapshot.hook_event_count??5} 事件` : `未完整注册 · ${snapshot.hook_events}/${snapshot.hook_event_count??5} 事件`) : '尚未读取注册配置'}</span>
      {snapshot?.hook_registered && !snapshot.hook_enabled && <span>自动处理已停用</span>}
      <div className="sg-settings-actions">
        <button type="button" disabled={busy || !project || !snapshot} onClick={() => void run('install_hooks')}>自动安装 Hook</button>
        <button type="button" disabled={busy || !snapshot} onClick={() => setConfirm('uninstall_hooks')}>卸载 Hook</button>
      </div>
      <p>安装后需 Codex 信任；卸载不删记忆。</p>
    </div>
    <div className="sg-install-row">
      <strong>SessionGraph MCP <small>Codex 全局</small></strong>
      <span>{snapshot ? (snapshot.mcp_registered ? (snapshot.mcp_guidance_installed ? '已安装 · 服务与使用指引就绪' : '服务已注册 · 使用指引未安装') : '服务未注册或存在自定义配置') : '尚未读取注册配置'}</span>
      <div className="sg-settings-actions">
        <button type="button" disabled={busy || !project || !snapshot} onClick={() => void run('install_mcp')}>安装 MCP</button>
        <button type="button" disabled={busy || !snapshot} onClick={() => setConfirm('uninstall_mcp')}>卸载 MCP</button>
      </div>
      <p>同步服务注册与 AGENTS.md 指引，保留其他内容。</p>
    </div>
    {confirm && <div className="sg-uninstall-confirm" role="group" aria-label="卸载确认">
      <p>{confirm === 'uninstall_hooks' ? '卸载全局 Hook 并停用自动处理？不终止进行中的任务。' : '移除全局 MCP 注册和使用指引？已有连接需关闭或重连。'}保留全部记忆数据。</p>
      <div className="sg-settings-actions"><button type="button" disabled={busy} onClick={() => void run(confirm)}>确认卸载</button><button type="button" disabled={busy} onClick={() => setConfirm(null)}>取消</button></div>
    </div>}
    {busy && <p role="status">正在处理设置…</p>}{error && <p role="alert" className="sg-error">{error}</p>}{message && <p role="status">{message}</p>}
  </section>
}
