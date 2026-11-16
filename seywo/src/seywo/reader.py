"""Loopback reading and manual correction UI; GETs use only published versions."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import secrets
from urllib.parse import parse_qs, urlparse

from .config import load_config
from .narrative import Narrative
from .store import Store
from .titles import LEGACY_TITLE, TITLE_MAX

HTML = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Seywo · 看清你的工作</title><style>
:root{font-family:system-ui,-apple-system,sans-serif;color:#253343;background:#f4f6fa}body{max-width:1050px;margin:32px auto;padding:0 20px}h1{margin-bottom:4px}button,select,input,textarea{font:inherit;padding:7px;margin:4px;border:1px solid #bbc7d5;border-radius:6px}button{cursor:pointer;background:white}button:disabled{opacity:.5;cursor:default}article,section{background:white;padding:18px;margin:14px 0;border:1px solid #dae0e8;border-radius:12px}.branch{margin-left:28px;border-left:4px solid #b1b0e7}.node-title{font-size:16px;margin:0 0 6px}.item-detail{white-space:pre-wrap}.item{padding:12px 0;border-bottom:1px solid #e9edf1}.muted{color:#627184;font-size:13px}.warning{color:#875000;background:#fff1ca;padding:12px;border-radius:8px}blockquote{white-space:pre-wrap;margin:9px 0;padding:10px;background:#f4f6fa;border-left:3px solid #8b9bad}dialog{max-width:680px;width:85%;border:1px solid #bbc7d5;border-radius:12px}textarea{display:block;width:95%;min-height:80px}label{display:block;margin:12px 0}#error{color:#a22;white-space:pre-wrap}summary{cursor:pointer;padding:8px 0}.phase{background:white;border:1px solid #dae0e8;border-radius:12px;margin:12px 0;padding:10px 18px}.phase>summary{font-weight:600}.phase>section{border:0;margin:0;padding:10px 0}.archive{margin:12px 0}.counts{font-size:13px;font-weight:400;color:#627184}.top{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.pill{font-size:12px;background:#eef1f7;border-radius:4px;padding:2px 5px}</style>
<h1>Seywo</h1><p class="muted">看清你的工作 · 主线、支线与当前停点</p><div class="top"><button id="refresh">查看当前版本</button><label>历史版本 <input id="version" type="number" min="1" style="width:75px"></label><button id="history">查看</button></div><div id="error" role="alert"></div><main id="main"></main><dialog id="editor"><form id="editform"><h2 id="editTitle"></h2><div id="fields"></div><p class="muted">修正会生成新版本并保留历史；修改文字不代表批准执行或确认结论。</p><button type="submit">保存修正</button><button type="button" id="cancel">取消</button></form></dialog>
<script>
const token=TOKEN, chat=CHAT;let view, pending;
const nodeTitle=i=>i.title||LEGACY_LABEL;
const el=(tag,text,cls)=>{let n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
const gapLabels={compact_reference_not_original:'宿主压缩记录，原文另行定位',host_envelope_needs_adapter:'宿主封装尚待适配',unsupported_event:'尚未适配的事件记录',media_not_supported:'媒体正文尚未理解'};
const labels={active:'进行中',waiting:'等待',resolved:'已解决（待核对）',abandoned:'已放弃',needs_review:'需核对',unreviewed:'自动整理',manual:'人工补充',user_statement:'用户陈述',assistant_statement:'助手陈述',tool_observation:'工具观察',inference:'推断',enter:'进入支线',return:'返回主线',goal_change:'目标转向',reopen:'重新打开',focus:'焦点变化'};
function button(parent,text,fn){let b=el('button',text);b.type='button';b.onclick=fn;b.disabled=!view.is_current;parent.append(b);}
function itemNames(ids){return ids.map(id=>(()=>{let i=view.narrative.items.find(i=>i.id===id);return i?.hidden?'[已隐藏事项]':i?nodeTitle(i):'未找到事项';})()).join('；')||'尚未明确';}
function inputField(name,label,value,kind='text',options=[]){let l=el('label',label),n=el(['select','multiselect'].includes(kind)?'select':kind==='textarea'?'textarea':'input');n.name=name;
if(['select','multiselect'].includes(kind)){for(const [v,t]of options){let o=el('option',t);o.value=v;n.append(o);}}else if(kind==='text')n.type='text';if(kind==='multiselect'){n.multiple=true;for(let o of n.options)o.selected=(value||[]).includes(o.value);}else n.value=value??'';l.append(n);document.querySelector('#fields').append(l);return n;}
function titleField(name,label,value){let n=inputField(name,label,value);n.required=true;n.maxLength=TITLE_LIMIT;return n;}
function openEdit(title,entity,id,fields,collect){pending={entity,id,collect};document.querySelector('#editTitle').textContent=title;document.querySelector('#fields').replaceChildren();fields();document.querySelector('#editor').showModal();}
function locationFields(value){let p=inputField('phase_id','主线阶段',value.phase_id,'select',[['','待定区'],...view.narrative.phases.map(p=>[p.id,p.title])]);let b=inputField('branch_id','支线',value.branch_id,'select',[]);
function branches(){b.replaceChildren();for(const[v,t]of [['','主线'],...view.narrative.branches.filter(x=>x.phase_id===p.value).map(x=>[x.id,x.title])]){let o=el('option',t);o.value=v;b.append(o);}b.value=value.branch_id||'';if(b.selectedIndex<0)b.value='';}p.onchange=branches;branches();}
function locationValue(f){return{phase_id:f.get('phase_id')||null,branch_id:f.get('branch_id')||null};}
function renderItem(parent,i){let n=el('div',undefined,'item');n.dataset.itemId=i.id;n.append(el('h4',nodeTitle(i),'node-title'),el('p',(i.amended_by?'人工修正 · 原来源归属：':'')+(labels[i.attribution]||i.attribution)+' · '+(labels[i.review_state]||i.review_state)+(i.hidden?' · 已隐藏':''),'muted'));
let detail=el('details');detail.append(el('summary','查看详情与依据（'+i.evidence.length+'）'),el('p',i.content,'item-detail'),el('p','范围：'+i.scope,'muted'));if(!i.evidence.length)detail.append(el('p','人工补充，操作者：'+i.actor+'；无原文证据。','muted'));
for(let e of i.evidence){detail.append(el('div',e.relation+' · '+e.event_id+' · 字符 '+e.start+'–'+e.end,'muted'),el('blockquote',e.quote));let more=el('button','展开相邻原文');more.type='button';more.onclick=async()=>{try{let q=new URLSearchParams({version:view.version,event_id:e.event_id,snapshot_id:e.snapshot_id,start:Math.max(0,e.start-300),end:Math.min(Math.max(0,e.start-300)+8000,e.end+300)});let r=await fetch('/api/source?'+q);if(!r.ok)throw Error('原文暂不可用');let source=await r.json();detail.append(el('p','来源角色：'+source.role+' · 字符 '+source.char_start+'–'+source.char_end,'muted'),el('blockquote',source.text));more.disabled=true;}catch(error){document.querySelector('#error').textContent=error.message;}};detail.append(more);}
if(i.dependencies.length)detail.append(el('p','依赖：'+itemNames(i.dependencies)));
button(detail,'修正标题与详情',()=>openEdit('修正事项','item',i.id,()=>{titleField('title','短标题',i.title||'');inputField('content','详情',i.content,'textarea');inputField('scope','适用范围',i.scope,'textarea');},f=>({title:f.get('title'),content:f.get('content'),scope:f.get('scope')})));
button(detail,'拆成两项',()=>openEdit('拆分事项','item_split',i.id,()=>{titleField('first_title','第一项标题','');inputField('first','第一项详情',i.content,'textarea');titleField('second_title','第二项标题','');inputField('second','第二项详情','','textarea');inputField('scope','适用范围',i.scope,'textarea');},f=>({pieces:[{title:f.get('first_title'),content:f.get('first'),scope:f.get('scope')},{title:f.get('second_title'),content:f.get('second'),scope:f.get('scope')}]})));button(detail,'移动',()=>{let p=view.narrative.placements.find(p=>p.item_id===i.id);openEdit('调整事项位置','placement',i.id,()=>locationFields(p),locationValue);});
button(detail,i.hidden?'恢复显示':'隐藏',()=>save({entity:'item',id:i.id,fields:{hidden:!i.hidden}}));n.append(detail);parent.append(n);}
function render(data){view=data;let doc=data.narrative,m=document.querySelector('#main');m.replaceChildren();document.querySelector('#version').value=data.version;
m.append(el('p','版本 '+data.version+' · 人工修订 '+data.override_revision+' · '+(data.is_current?'当前版本':'历史版本，仅供阅读'),'muted'));
if(data.update_pending)m.append(el('p','对话有新增或修改，等待下次整理；当前主线可继续阅读。','warning'));
if(data.review_count)m.append(el('p',data.review_count+' 个节点的依据或依赖发生变化，需要核对。','warning'));
if(data.pending_revision_count)m.append(el('p',data.pending_revision_count+' 项修订建议待确认。','warning'));
if(doc.pending_organization_count)m.append(el('p','当前窗口还有 '+doc.pending_organization_count+' 项待整理，此版本尚未覆盖全部候选。','warning'));
let top=el('section');top.append(el('h2','当前停点'),el('p','目标：'+itemNames(doc.overview.goal_item_ids)));let focus=doc.overview.focus,p=doc.phases.find(p=>p.id===focus.phase_id),b=doc.branches.find(b=>b.id===focus.branch_id);top.append(el('p','当前位置：'+(p?.title||'待定')+(b?' / '+b.title:'')),el('p','未决问题：'+itemNames(doc.overview.open_question_ids)),el('p','下一步：'+itemNames(doc.overview.next_step_ids)));
button(top,'调整目标与下一步',()=>openEdit('调整当前目标、未决问题与下一步','overview','overview',()=>{for(let[k,t]of [['goal_item_ids','当前目标'],['open_question_ids','未决问题'],['next_step_ids','下一步']])inputField(k,t,doc.overview[k],'multiselect',doc.items.filter(i=>!i.hidden).map(i=>[i.id,nodeTitle(i)]));},f=>({goal_item_ids:f.getAll('goal_item_ids'),open_question_ids:f.getAll('open_question_ids'),next_step_ids:f.getAll('next_step_ids')})));button(top,'调整当前位置',()=>openEdit('调整当前位置','overview','overview',()=>locationFields(focus),f=>({focus:locationValue(f)})));
for(let review of (doc.pending_revisions||[]).filter(r=>r.state==='open')){let conflict=el('article');conflict.append(el('h3','待审修订：'+nodeTitle(review.candidate)),el('p',review.reason,'muted'));let proposed=el('details');proposed.append(el('summary','查看候选详情与依据'),el('p',review.candidate.content,'item-detail'));for(let e of review.candidate.evidence)proposed.append(el('blockquote',e.quote));conflict.append(proposed);button(conflict,'保留当前内容',()=>save({entity:'review',id:review.id,fields:{decision:'keep_current'}}));button(conflict,'采用候选修订',()=>save({entity:'review',id:review.id,fields:{decision:'accept_candidate'}}));top.append(conflict);}if(doc.overview.unresolved.length){let retained=el('details',undefined,'archive');retained.append(el('summary','保留的待核查记录（'+doc.overview.unresolved.length+'）'),el('p','这些记录仍未凭依据关闭，可能来自先前阶段；当前问题单独显示在上方。','muted'));for(let u of doc.overview.unresolved)retained.append(el('p',u));top.append(retained);}
const sourceGaps=[...data.gaps.map(g=>({reason:g.reason,count:g.n,scope:'当前材料'})),...Object.entries((data.published_gaps||[]).reduce((counts,g)=>{counts[g.reason]=(counts[g.reason]||0)+(g.count??1);return counts;},{})).map(([reason,count])=>({reason,count,scope:'整理此版本时'}))];if(sourceGaps.length){let gaps=el('details',undefined,'archive');gaps.append(el('summary','材料覆盖说明（'+sourceGaps.length+' 类记录）'),el('p','部分宿主记录或材料尚未理解，不能据此认定整个对话已核实。','muted'));for(let g of sourceGaps)gaps.append(el('p',g.scope+'：'+(gapLabels[g.reason]||g.reason)+'（'+g.count+'）'));top.append(gaps);}m.append(top);
function contents(parent,phase,branch){let items=doc.placements.filter(x=>x.phase_id===phase&&x.branch_id===branch).map(x=>doc.items.find(i=>i.id===x.item_id)).filter(i=>!i.hidden);if(phase===focus.phase_id){let current=new Set([...doc.overview.goal_item_ids,...doc.overview.open_question_ids,...doc.overview.next_step_ids]);for(let i of items.filter(i=>current.has(i.id)))renderItem(parent,i);let others=items.filter(i=>!current.has(i.id));if(others.length){let archive=el('details',undefined,'archive');archive.append(el('summary','该位置的其他事项（'+others.length+'）'));for(let i of others)renderItem(archive,i);parent.append(archive);}}else for(let i of items)renderItem(parent,i);}
let line=el('h2','工作主线');m.append(line);for(let phase of doc.phases){let group=el('details',undefined,'phase');group.dataset.phaseId=phase.id;group.open=phase.id===focus.phase_id;let summary=el('summary',phase.title),count=doc.placements.filter(x=>x.phase_id===phase.id&&!doc.items.find(i=>i.id===x.item_id).hidden).length;summary.append(el('span',(group.open?' · 当前阶段':'')+' · '+count+' 项','counts'));group.append(summary);let s=el('section');s.append(el('h3',phase.title));button(s,'改阶段名称',()=>openEdit('修正阶段名称','phase',phase.id,()=>inputField('title','名称',phase.title),f=>({title:f.get('title')})));if(doc.phases.length>1)button(s,'合并阶段',()=>openEdit('将另一个阶段合并到此处','phase_merge',phase.id,()=>{inputField('source_phase_id','并入的阶段','','select',doc.phases.filter(p=>p.id!==phase.id).map(p=>[p.id,p.title]));inputField('title','合并后名称',phase.title);},f=>({source_phase_id:f.get('source_phase_id'),title:f.get('title')})));contents(s,phase.id,null);
for(let branch of doc.branches.filter(b=>b.phase_id===phase.id)){let bs=el('article',undefined,'branch');bs.append(el('h3',branch.title+' · '+labels[branch.state]),el('p','局部目标：'+branch.goal),el('p','解决条件：'+branch.resolve_when));
button(bs,'修正支线',()=>openEdit('修正支线','branch',branch.id,()=>{inputField('title','名称',branch.title);inputField('goal','局部目标',branch.goal,'textarea');inputField('resolve_when','解决条件',branch.resolve_when,'textarea');inputField('state','状态',branch.state,'select',Object.entries(labels).filter(([k])=>['active','waiting','resolved','abandoned'].includes(k)));inputField('outcome_item_ids','结果依据',branch.outcome_item_ids,'multiselect',doc.items.map(i=>[i.id,nodeTitle(i)]));inputField('evidence_item_ids','状态变化依据',branch.evidence_item_ids,'multiselect',doc.items.map(i=>[i.id,nodeTitle(i)]));},f=>({title:f.get('title'),goal:f.get('goal'),resolve_when:f.get('resolve_when'),state:f.get('state'),outcome_item_ids:f.getAll('outcome_item_ids'),evidence_item_ids:f.getAll('evidence_item_ids')})));
contents(bs,phase.id,branch.id);s.append(bs);}group.append(s);m.append(group);}
let deferred=el('section');deferred.append(el('h2','待定区'));contents(deferred,null,null);m.append(deferred);
let visits=el('details',undefined,'archive');visits.append(el('summary','转向与支线往返（'+doc.transitions.length+'）'));for(let t of doc.transitions)visits.append(el('p',(labels[t.kind]||t.kind)+'：'+t.reason),el('p','带回结果：'+itemNames(t.result_item_ids)+'；未决：'+t.unresolved.join('；'),'muted'));m.append(visits);
let hidden=el('details');hidden.append(el('summary','已隐藏事项'));for(let i of doc.items.filter(i=>i.hidden))renderItem(hidden,i);m.append(hidden);
button(m,'补充人工事项',()=>openEdit('补充人工事项','manual_item','new',()=>{titleField('title','短标题','');inputField('content','详情','','textarea');inputField('scope','适用范围','','textarea');inputField('type','类型','observation','select',[['observation','观察'],['question','问题'],['next_step','下一步'],['goal','目标']]);locationFields(focus);},f=>({title:f.get('title'),content:f.get('content'),scope:f.get('scope'),type:f.get('type'),...locationValue(f)})));
m.append(el('p','自动整理与出处匹配均不代表人工确认，也不代表工作已完成。','muted'));}
async function load(version){try{let r=await fetch('/api/read'+(version?'?version='+encodeURIComponent(version):''));if(!r.ok)throw Error('读取失败：请确认该版本已发布。');render(await r.json());document.querySelector('#error').textContent='';}catch(e){document.querySelector('#error').textContent=e.message;}}
async function save(patch){try{let r=await fetch('/api/edit',{method:'POST',headers:{'Content-Type':'application/json','X-Seywo-Token':token},body:JSON.stringify({base_version:view.version,override_revision:view.override_revision,actor:'local-reader',patch})});if(!r.ok)throw Error(r.status===409?'版本已变化，请查看当前版本后重新修正。':'修正未保存：请核对字段及解决/放弃支线所需的依据。');render(await r.json());document.querySelector('#error').textContent='';document.querySelector('#editor').close();}catch(e){document.querySelector('#error').textContent=e.message;}}
document.querySelector('#refresh').onclick=()=>load();document.querySelector('#history').onclick=()=>load(document.querySelector('#version').value);document.querySelector('#cancel').onclick=()=>document.querySelector('#editor').close();document.querySelector('#editform').onsubmit=e=>{e.preventDefault();save({entity:pending.entity,id:pending.id,fields:pending.collect(new FormData(e.target))});};load();
</script></html>'''


def make_server(config_path, chat_id, *, port=0):
    """Explicit loopback-only writer; random per-process token guards every mutation."""
    return make_server_from_config(load_config(config_path),chat_id,port=port)


def make_server_from_config(config, chat_id, *, port=0):
    """Explicit API configuration, including an isolated DB with original binding.

    Store validates its owner. No configuration discovery or source import occurs.
    """
    config = dict(config)
    with Store(config, read_only=True) as store:
        Narrative(store).read(chat_id)
    token = secrets.token_urlsafe(32)
    def literal(value):
        return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")
    html = HTML.replace("TOKEN", literal(token)).replace("CHAT", literal(chat_id)).replace("LEGACY_LABEL", literal(LEGACY_TITLE)).replace("TITLE_LIMIT", str(TITLE_MAX)).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, data, content_type="application/json; charset=utf-8"):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def valid_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def do_GET(self):
            if not self.valid_host():
                return self.reply(403, {"error": "invalid_host"})
            parsed = urlparse(self.path)
            if parsed.path == "/":
                return self.reply(200, html, "text/html; charset=utf-8")
            if parsed.path == "/api/source":
                try:
                    query = parse_qs(parsed.query)
                    args = {k: query[k][0] for k in ("event_id", "snapshot_id")}
                    args.update({k: int(query[k][0]) for k in ("version", "start", "end")})
                    with Store(config, read_only=True) as store:
                        result = Narrative(store).source(chat_id, **args)
                    return self.reply(200, result)
                except (ValueError, TypeError, KeyError):
                    return self.reply(400, {"error": "source_unavailable"})
            if parsed.path != "/api/read":
                return self.reply(404, {"error": "not_found"})
            try:
                query = parse_qs(parsed.query)
                version = int(query["version"][0]) if "version" in query else None
                with Store(config, read_only=True) as store:
                    result = Narrative(store).read(chat_id, version=version)
                return self.reply(200, result)
            except (ValueError, TypeError, KeyError):
                return self.reply(400, {"error": "version_unavailable"})

        def do_POST(self):
            if (not self.valid_host() or self.path != "/api/edit"
                    or not secrets.compare_digest(self.headers.get("X-Seywo-Token", ""), token)
                    or self.headers.get("Content-Type") != "application/json"):
                return self.reply(403, {"error": "request_not_authorized"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 128 * 1024:
                    raise ValueError("invalid_body_length")
                body = json.loads(self.rfile.read(length))
                if set(body) != {"base_version", "override_revision", "actor", "patch"}:
                    raise ValueError("invalid_edit_fields")
                with Store(config) as store:
                    result = Narrative(store).edit(chat_id, **body)
                return self.reply(200, result)
            except (ValueError, TypeError, KeyError):
                return self.reply(409, {"error": "edit_rejected_or_version_conflict"})
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
