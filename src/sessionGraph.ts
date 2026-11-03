import { invoke } from '@tauri-apps/api/core'

export type Project = { id: string; name: string; root: string; state: string }
export type RevisionValidity = { state: string; revision: string | null; alternatives?: string[] }
export function revisionValidityLabel(validity?: RevisionValidity): string {
  return validity ? ({current:'当前版本',disputed:'存在争议',needs_review:'待复核',legacy_unassessed:'历史版本未评估',derived_only:'仅有转述来源'} as Record<string,string>)[validity.state] || '待复核' : ''
}
export type GraphNode = { id: string; type: string; kind: string; object_type?: string; label: string; content?: string; status?: string; chat_id?: string; chat_ids?: string[]; evidence_status?: string; path?: string; validity?: RevisionValidity }
export type GraphEdge = { source: string; target: string; kind: string; basis?: string; statement_mode?: string; state?: string }
export type GraphJobDiagnostic = {chat?:string;stage?:string;due?:number;id:string;state?:string;error_code:string;reason:string}
export type GraphDiagnostics = {progress?:Record<string,number>;enabled:boolean;initialized:boolean;nodes?:number;relations?:number;jobs?:Record<string,number>;blocked_jobs?:GraphJobDiagnostic[];deferred_jobs?:GraphJobDiagnostic[];unresolved?:Record<string,number>}
export type TieringSummary = {available:boolean;revision:string;current?:boolean;canonical:number;episode:number;record_only:number;covered?:number;total?:number;complete?:boolean;revisions?:Record<string,number>}
export type MapView = 'all'|'knowledge'|'episode'
export type WorkMap = { page?:{offset:number;limit:number;matched:number;next_offset:number|null;query:string;chat:string;view?:MapView}; graph_v2?:GraphDiagnostics; view?:MapView; tiering?:TieringSummary; revision: string; nodes: GraphNode[]; edges: GraphEdge[]; chats: {chat_id:string;count:number}[]; truncated?: boolean; stats: {memories:number;total_memories:number;references:number;semantic_relations:number;reference_relations:number}; notice: string }
export type RequestTrace = {transport?:{mode:string;connect_host:string;connect_port:number|null;target_host:string};id:string;path:string;model:string;max_output_tokens:number|null;request_bytes:number;usage?:{input_tokens?:number;output_tokens?:number;output_tokens_details?:{reasoning_tokens?:number}};finish_reason?:string|null;incomplete_details?:{reason?:string}|null}
export type Run = {attempt_outcome?:string;backlog_failures?:{job_id:string;error_code:string;retryable:boolean}[];batches_recovered?:boolean;request_traces?:RequestTrace[];input_stats?:{source_chars?:number|null;selected_chars?:number|null;excluded_chars?:number|null}|null;run_id:string;chat_id:string;state:string;event?:string;updated_at?:number;error_code?:string;warning_code?:string;reported_model_calls?:number;model_call_limit?:number|null;scheduling_policy?:string;statistics_incomplete?:boolean;saved_count?:number;saved_relation_count?:number;failure_codes?:string[];coalesced_run_id?:string;historical_failure_codes?:string[];graph_v2?:{pending_jobs?:number;ready_jobs?:number;retry_wait_jobs?:number;blocked_jobs?:number;input_source_chars?:number;input_selected_chars?:number;input_excluded_chars?:number;failure_reason?:string;source_remaining?:boolean;deepseek_calls?:number;judge_calls?:number;jev_calls?:number}}
export type UpdateStatus = {continuations?:{chat:string;state:string;due:number;run_id:string;error_code?:string}[];runs:Run[];active_count:number;unreadable_runs?:number}
export type MemoryDetail = { graph_v2?:{type:string;requested_id?:string;canonical_id?:string;member_ids?:string[];evidence_page?:{offset:number;limit:number;has_more:boolean;next_offset:number|null};assertions:{entry_id:string;quote:string;chat_id:string;original_node_id?:string;source_time?:string;role?:string;assertion:{content:string;status:string}}[]}; memory: {content:string;paths?:string[];source?:{entry_id:string;quote:string;role?:string}}; entries: {id:string;text:string;role?:string;chat_id?:string}[];superseded_by:{id:string;title:string;status:string}[] }
export type Catalog = {projects:Project[];default_project:string}
export type ReadOperation = 'projects' | 'work_map' | 'status' | 'memory' | 'evidence' | 'call_logs'
export type SessionGraphRead = <T>(operation:ReadOperation, args?:{project?:string;id?:string;offset?:number;query?:string;chat?:string;view?:MapView;logModel?:string;logStatus?:string})=>Promise<T>
export const readSessionGraph:SessionGraphRead = async (operation,args={}) => {
  if (import.meta.env.DEV && !('__TAURI_INTERNALS__' in window)) {
    const {logModel,logStatus,...other}=args
    const response=await fetch('/__sessiongraph/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operation,...other,...(logModel!==undefined?{log_model:logModel}:{}),...(logStatus!==undefined?{log_status:logStatus}:{})})})
    const result=await response.json()
    if (!response.ok || result.error || result.model_calls!==0) throw new Error('本地只读开发连接不可用')
    return result
  }
  return invoke('read_session_graph',{operation,...args})
}
export type Preferences = {version:1;project:string}
const key='gateway.sessiongraph.v1'
export function readPreferences():Preferences {
  try { const p=JSON.parse(localStorage.getItem(key)||'null'); if(p?.version===1&&typeof p.project==='string')return {version:1,project:p.project} } catch { /* Corrupt preferences do not change project data. */ }
  return {version:1,project:''}
}
export function savePreferences(p:Preferences){ try{localStorage.setItem(key,JSON.stringify(p))}catch{/* Storage unavailable: connection still works for this window. */} }
export function graphWindow(nodes:GraphNode[],edges:GraphEdge[],focus='',limit=24){
  const byId=new Map(nodes.map(n=>[n.id,n])); const ids=new Set<string>()
  if(byId.has(focus))ids.add(focus)
  else {
    // Prefer stored semantic links in the overview, then exact-reference links.
    for(const e of [...edges].sort((a,b)=>Number(['MENTIONS','REFERENCES_PATH'].includes(a.kind))-Number(['MENTIONS','REFERENCES_PATH'].includes(b.kind)))) {
      if(ids.size>=Math.floor(limit/2))break
      if(byId.has(e.source))ids.add(e.source)
      if(ids.size<limit&&byId.has(e.target))ids.add(e.target)
    }
  }
  // Two-hop traversal shows relationships rather than manufacturing a chat tree.
  for(let hop=0;hop<2;hop++){
    const layer=new Set(ids)
    for(const e of edges){if(ids.size>=limit)break;if(layer.has(e.source)&&byId.has(e.target))ids.add(e.target);if(ids.size<limit&&layer.has(e.target)&&byId.has(e.source))ids.add(e.source)}
  }
  for(const n of nodes){if(ids.size>=limit)break;ids.add(n.id)}
  return {nodes:[...ids].map(id=>byId.get(id)!),edges:edges.filter(e=>ids.has(e.source)&&ids.has(e.target)),omitted:nodes.length-ids.size}
}
export function graphLayout(nodes:GraphNode[],edges:GraphEdge[]){
  const points=new Map(nodes.map((n,i)=>{const a=i*2.3999632297,r=90+55*Math.sqrt(i);return [n.id,{x:450+Math.cos(a)*r,y:300+Math.sin(a)*r}]}))
  for(let step=0;step<150;step++){
    const force=new Map(nodes.map(n=>[n.id,{x:0,y:0}]))
    for(let i=0;i<nodes.length;i++)for(let j=i+1;j<nodes.length;j++){
      const a=points.get(nodes[i].id)!,b=points.get(nodes[j].id)!,dx=a.x-b.x,dy=a.y-b.y,d=Math.max(1,Math.hypot(dx,dy)),f=18000/(d*d)
      force.get(nodes[i].id)!.x+=dx/d*f;force.get(nodes[i].id)!.y+=dy/d*f;force.get(nodes[j].id)!.x-=dx/d*f;force.get(nodes[j].id)!.y-=dy/d*f
    }
    for(const e of edges){const a=points.get(e.source),b=points.get(e.target);if(!a||!b)continue;const dx=b.x-a.x,dy=b.y-a.y,d=Math.max(1,Math.hypot(dx,dy)),f=(d-220)*.02;force.get(e.source)!.x+=dx/d*f;force.get(e.source)!.y+=dy/d*f;force.get(e.target)!.x-=dx/d*f;force.get(e.target)!.y-=dy/d*f}
    for(const n of nodes){const p=points.get(n.id)!,f=force.get(n.id)!;p.x+=Math.max(-8,Math.min(8,f.x+(450-p.x)*.008));p.y+=Math.max(-8,Math.min(8,f.y+(300-p.y)*.008))}
  }
  for(let step=0;step<100;step++)for(let i=0;i<nodes.length;i++)for(let j=i+1;j<nodes.length;j++){
    const a=points.get(nodes[i].id)!,b=points.get(nodes[j].id)!,dx=b.x-a.x,dy=b.y-a.y,ox=165-Math.abs(dx),oy=72-Math.abs(dy)
    if(ox>0&&oy>0){if(ox<oy){const v=(ox+1)/2*(dx>=0?1:-1);a.x-=v;b.x+=v}else{const v=(oy+1)/2*(dy>=0?1:-1);a.y-=v;b.y+=v}}
  }
  return points
}

export type SessionGraphModelOption={value:string;label:string;upstreamModel?:string}
export type SessionGraphSettingsAction='status'|'save_model'|'save_provider'|'test_provider'|'reveal_provider_key'|'install_hooks'|'install_mcp'|'uninstall_hooks'|'uninstall_mcp'|'enable_graph'|'disable_graph'|'save_jev'|'storage_preview'|'storage_compact'|'clean_notices'|'clear_tasks'
export type SessionGraphSettingsRequest={action:SessionGraphSettingsAction;project?:string;token?:string;run_id?:string;model?:string;api_key?:string;endpoint?:string;api_key_env?:string}
export type MaintenanceResult={before?:{database_bytes:number;wal_bytes:number;logical_bytes:number;reclaimable_bytes:number};after?:{database_bytes:number;wal_bytes:number;logical_bytes:number;reclaimable_bytes:number};reclaimed_bytes?:number;vacuum_performed?:boolean;vacuum_skipped_reason?:string;integrity_scope?:string[];counts?:Record<string,number>;dismissed_tasks?:number;dismissed_batches?:number;archived_runs?:number;unreadable_runs?:number;removed?:number;remaining?:number;warning?:string;model_calls:number}
export type SessionGraphSettingsSnapshot={api_key_configured?:boolean;api_key?:string;provider_test?:{ok:boolean;elapsed_ms:number;http_status:number|null;model:string};endpoint?:string;api_key_env?:string;hook_event_count?:number;maintenance?:MaintenanceResult;jev_configured?:boolean;judgment_backend?:'selected_model';graph_v2_enabled?:boolean;ok:boolean;model_calls:number;changed_count:number;model:string;runtime_installed:boolean;hook_registered:boolean;hook_events:number;hook_enabled:boolean;mcp_registered:boolean;mcp_guidance_installed:boolean;message:string}
export type SessionGraphManage=(request:SessionGraphSettingsRequest)=>Promise<SessionGraphSettingsSnapshot>
export const manageSessionGraph:SessionGraphManage=request=>invoke('manage_session_graph',{request})
export function sessionGraphModelOptions(models:{name?:string;model:string;provider:string;status?:string;protocol?:string}[]):SessionGraphModelOption[]{
  const options=new Map<string,SessionGraphModelOption>()
  for(const model of models){
    if(model.status==='停用'||model.protocol==='moa'||!model.model.trim())continue
    const value=model.name?.trim()||model.model.trim()
    if(!options.has(value))options.set(value,{
      value,label:`${model.name?.trim()||model.model} · ${model.provider||'未命名供应商'}`,
      upstreamModel:model.model.trim(),
    })
  }
  return [...options.values()]
}

export type ModelCall = {id:string;project:string;model:string;response_model:string;endpoint:string;chat_id:string;run_id:string;job_id:string;attempt_id:string;stage:string;state:string;created_at:number;elapsed_ms:number|null;http_status:number|null;input_tokens:number|null;output_tokens:number|null;provider_request_id:string;part:number|null;retry_count:number|null;error_code:string}
export type ModelCallPage = {items:ModelCall[];models:string[];offset:number;has_more:boolean;retention_days:number;max_bytes:number;model_calls:0}
