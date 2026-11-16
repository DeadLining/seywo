import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from '../../node_modules/jsdom/lib/api.js';
import { mountPanel } from './panel.js';
const tick = () => new Promise(r => setTimeout(r, 15));
const narrative = { chat_id: 'chat-a', version: 7, head_version: 7, selected_phase: 'p', node_count: 2, focus: { phase_id: 'p' },
    phases: [{ id: 'p', title: '开发插件', node_count: 2 }], branches: [{ id: 'b', phase_id: 'p', title: '检查界面', state: 'active' }],
    goals: [{ id: 'g', object_id: 'obj-g', title: '完成插件安装', evidence_count: 1 }], open_questions: [], next_steps: [], results: [{ id: 'n', object_id: 'obj-n', title: '完成插件安装', status: 'resolved', evidence_count: 1 }],
    retrieval: { has_more: false } };
function fixture() {
    const dom = new JSDOM('<main id="root"></main>', { url: 'https://seywo.test' });
    const calls = [];
    const call = async (args) => {
        calls.push(args);
        if (args.view === 'chats')
            return { project_root: '/work/产品', results: [{ chat_id: 'chat-a', title: '插件开发', version: 7 }], retrieval: { has_more: false } };
        if (args.view === 'versions')
            return { results: [{ version: 7, origin: 'compact', created_at: 1794500000 }], retrieval: { has_more: false } };
        if (args.view === 'mainline') return { ...narrative, results: undefined };
        if (args.view === 'narrative')
            return { ...narrative, results: args.branch_id ? [] : narrative.results };
        if (args.view === 'changes')
            return { before_version: 6, after_version: 7, before_count: 1, after_count: 2, counts: { added: 1, updated: 0, completed: 0, merged: 0, hidden: 0, removed: 0 }, results: [{ operation: 'added', item_id: 'n', before: null, after: narrative.results[0] }], retrieval: { has_more: false } };
        if (args.view === 'node')
            return { results: [{ item: { title: '完成插件安装', content: '<img src=x onerror=alert(1)>', evidence: [{ event_id: 'e', snapshot_id: 's', start: 0, end: 10 }] } }] };
        if (args.view === 'source')
            return { text: '原始对话证据', source_current: true, char_start: 0, char_end: 8, next_start: null };
    };
    return { dom, calls, root: dom.window.document.querySelector('#root'), call };
}
test('requires explicit Chat selection, then shows focused narrative and evidence safely', async () => {
    const f = fixture();
    await mountPanel(f.root, f.call);
    await tick();
    assert.match(f.root.textContent, /选择一个 Chat/);
    assert.equal(f.calls.filter(c => c.view === 'narrative').length, 0);
    const select = f.root.querySelector('[data-chat]');
    select.value = 'chat-a';
    select.dispatchEvent(new f.dom.window.Event('change'));
    await tick();
    assert.match(f.root.textContent, /完成插件安装/);
    f.root.querySelector('[data-node]').click();
    await tick();
    assert.equal(f.root.querySelectorAll('img').length, 0);
    assert.match(f.root.textContent, /原始对话证据/);
});
test('opens the current mainline without version or stage history requests', async () => {
    const f = fixture();
    await mountPanel(f.root, f.call, { chat_id: 'chat-a' });
    assert.deepEqual(f.calls.map(c => c.view), ['chats', 'mainline']);
    assert.match(f.root.textContent, /当前目标/);
    assert.match(f.root.textContent, /对话主线/);
    assert.doesNotMatch(f.root.textContent, /Compact|阶段记录|查看版本|个节点/);
    assert.equal(f.root.querySelector('[aria-label="历史版本"]'), null);
    f.root.querySelector('[data-phase]').click();
    await tick();
    assert.equal(f.calls.at(-1).view, 'narrative');
    assert.equal(f.calls.at(-1).version, 7);
    assert.match(f.root.textContent, /阶段明细/);
    f.root.querySelector('[data-branch]').click();
    await tick();
    assert.equal(f.calls.at(-1).branch_id, 'b');
    assert.equal(f.calls.at(-1).offset, 0);
});
test('shows recoverable user-facing errors without leaking backend traces', async () => {
    const f = fixture();
    await mountPanel(f.root, async () => { throw new Error('Traceback /secret/path API_KEY'); });
    assert.match(f.root.textContent, /暂时无法读取/);
    assert.doesNotMatch(f.root.textContent, /secret|API_KEY|Traceback/);
    assert.ok(f.root.querySelector('[data-retry]'));
});
test('loads additional Chats without loading a version directory', async () => {
    const f = fixture();
    const call = async args => args.view === 'chats'
        ? { project_root: '/work/产品', results: [{ chat_id: args.offset ? 'chat-b' : 'chat-a', title: '插件开发' }], retrieval: { has_more: !args.offset, next_offset: args.offset ? null : 20 } }
        : f.call(args);
    await mountPanel(f.root, call, { chat_id: 'chat-a' });
    f.root.querySelector('[data-more-chats]').click(); await tick();
    assert.equal(f.root.querySelectorAll('[data-chat] option').length, 3);
    assert.equal(f.root.querySelector('[data-more-chats]').hidden, true);
    assert.equal(f.calls.some(c => c.view === 'versions'), false);
});
test('switching Chats discards late views and selecting an empty Chat clears busy state', async () => {
    const f = fixture();
    let finish;
    let delayed = false;
    const base = f.call;
    const call = async (args) => {
        if (args.view === 'narrative' && delayed)
            return new Promise(resolve => { finish = resolve; });
        return base(args);
    };
    const ctrl = await mountPanel(f.root, call, { chat_id: 'chat-a' });
    delayed = true;
    f.root.querySelector('[data-phase]').click();
    await tick();
    await ctrl.selectChat('');
    finish({ ...narrative, results: [{ id: 'late', title: '过期结果' }] });
    await tick();
    assert.doesNotMatch(f.root.textContent, /过期结果/);
    assert.equal(f.root.querySelector('[data-content]').hasAttribute('aria-busy'), false);
});
test('unpublished Chat has a clear empty state and source errors stay human-readable', async () => {
    const f = fixture();
    const base = f.call;
    const ctrl = await mountPanel(f.root, async (args) => args.view === 'mainline' ? { state: 'unpublished', version: 0 } : base(args), { chat_id: 'chat-a' });
    assert.match(f.root.textContent, /还没有发布主线/);
    assert.equal(ctrl.state.version, 0);
    const g = fixture();
    await mountPanel(g.root, async (args) => args.view === 'source' ? { error: 'private/path' } : g.call(args), { chat_id: 'chat-a' });
    g.root.querySelector('[data-node]').click();
    await tick();
    assert.match(g.root.textContent, /这处原文暂时无法读取/);
    assert.doesNotMatch(g.root.textContent, /private\/path|undefined/);
});
test('loads only expanded evidence and supports bounded continuation', async () => {
    const f = fixture();
    const base = f.call;
    const reads = [];
    await mountPanel(f.root, async (args) => {
        if (args.view === 'node')
            return { results: [{ item: { title: '来源分页', evidence: [{ event_id: 'e1', snapshot_id: 's', start: 0, end: 5000 }, { event_id: 'e2', snapshot_id: 's', start: 0, end: 10 }] } }] };
        if (args.view === 'source') {
            reads.push(args);
            return { text: args.start ? '后续原文' : '开头原文', source_current: true, char_start: args.start, char_end: args.end, next_start: args.end < 5000 ? args.end : null };
        }
        return base(args);
    }, { chat_id: 'chat-a' });
    f.root.querySelector('[data-node]').click();
    await tick();
    assert.equal(reads.length, 1);
    assert.equal(reads[0].end, 4000);
    [...f.root.querySelectorAll('button')].find(b => b.textContent === '继续读取').click();
    await tick();
    assert.equal(reads[1].start, 4000);
    assert.equal(reads[1].end, 5000);
    assert.match(f.root.textContent, /后续原文/);
    const second = f.root.querySelectorAll('.source-section')[1];
    second.open = true;
    await tick();
    assert.equal(reads[2].event_id, 'e2');
});
test('native entrypoint can bind an existing project without automatic initialization', async () => {
    const f=fixture();let selected;
    await mountPanel(f.root,f.call,{state:'project_required'}, {selectProject:async path=>{selected=path;return {initialized:true,project_root:path};}});
    assert.equal(f.calls.length,0);
    assert.match(f.root.textContent,/选择项目/);
    f.root.querySelector('[data-project-path]').value='/work/产品';
    f.root.querySelector('[data-bind-project]').click();await tick();
    assert.equal(selected,'/work/产品');
    assert.match(f.root.textContent,/选择一个 Chat/);
    assert.equal(f.calls.filter(c=>c.view==='narrative').length,0);
    const g=fixture();
    await mountPanel(g.root,g.call,{state:'initialization_required',project_root:'/work/new'});
    assert.match(g.root.textContent,/尚未启用 Seywo/);
    assert.equal(g.calls.length,0);
});
test('refresh cancels an old Chat page without leaving pagination disabled', async () => {
    const f=fixture();let finish;
    const call=async args=>{
        if(args.view==='chats'&&args.offset)return new Promise(resolve=>{finish=resolve;});
        if(args.view==='chats')return {project_root:'/work/产品',results:[{chat_id:'chat-a',title:'插件开发'}],retrieval:{has_more:true,next_offset:20}};
        return f.call(args);
    };
    const controller=await mountPanel(f.root,call);
    f.root.querySelector('[data-more-chats]').click();await tick();
    assert.equal(f.root.querySelector('[data-more-chats]').disabled,true);
    await controller.refresh();finish({results:[],retrieval:{has_more:false}});await tick();
    assert.equal(f.root.querySelector('[data-more-chats]').disabled,false);
    assert.equal(f.root.querySelector('[data-more-chats]').hidden,false);
});
test('defaults to the host Chat even when it is outside the first picker page', async () => {
    const f=fixture();
    const call=async args=>args.view==='chats' ? {...await f.call(args), current_chat_id:'chat-current', default_chat_id:'chat-current'} : f.call(args);
    const c=await mountPanel(f.root,call);
    assert.equal(c.state.chat,'chat-current');
    assert.equal(f.root.querySelector('[data-chat]').value,'chat-current');
    assert.ok(f.calls.some(c=>c.view==='mainline'&&c.chat_id==='chat-current'));
});
test('unimported host Chat shows its own empty state and never selects another Chat', async()=>{
    const f=fixture();
    await mountPanel(f.root,async args=>({...await f.call(args),current_chat_id:'not-imported',default_chat_id:null}));
    assert.match(f.root.textContent,/当前对话尚未收录/);
    assert.equal(f.calls.filter(c=>c.view==='narrative').length,0);
});
test('refresh uses a consistent small SVG instead of a platform text glyph',async()=>{
    const f=fixture();await mountPanel(f.root,f.call);
    const refresh=f.root.querySelector('[data-refresh]');
    assert.ok(refresh.querySelector('svg'));
    assert.equal(refresh.title,'刷新记录');
    assert.doesNotMatch(refresh.textContent,/↻/);
});

test('keeps questions and next steps visible while stage details stay unloaded', async () => {
    const f = fixture();
    await mountPanel(f.root, async args => args.view === 'mainline'
      ? { ...narrative, open_questions: [{ id: 'q', object_id: 'obj-q', title: '模型语义核验', status: 'open' }], next_steps: [{ id: 's', object_id: 'obj-s', title: '完成桌面验收' }] }
      : f.call(args), { chat_id: 'chat-a' });
    assert.match(f.root.textContent, /待解决/);
    assert.match(f.root.textContent, /模型语义核验/);
    assert.match(f.root.textContent, /下一步/);
    assert.match(f.root.textContent, /完成桌面验收/);
    assert.equal(f.calls.some(c => c.view === 'narrative'), false);
    assert.equal(f.root.querySelector('[data-records]').hidden, true);
});

test('mainline precedes secondary lists and long priority lists fold after three titles', async () => {
    const f = fixture();
    const steps = Array.from({length: 9}, (_, i) => ({id: `s${i}`, object_id: `obj-${i}`, title: `后续事项${i}`}));
    await mountPanel(f.root, async args => args.view === 'mainline' ? {...narrative, next_steps: steps} : f.call(args), {chat_id: 'chat-a'});
    const sections = [...f.root.querySelector('[data-content]').children];
    assert.ok(sections.findIndex(n => n.classList.contains('mainline')) < sections.findIndex(n => n.classList.contains('next-steps')));
    const next = f.root.querySelector('.next-steps');
    assert.equal(next.querySelectorAll(':scope > [data-node]').length, 3);
    const more = next.querySelector('details');
    assert.equal(more.open, false);
    assert.equal(more.querySelectorAll('[data-node]').length, 6);
    assert.match(more.querySelector('summary').textContent, /其余 6 项/);
});

test('shows project-local model settings without exposing the saved key', async () => {
    const f = fixture();
    const actions = {
        modelStatus: async () => ({configured: true, endpoint: 'http://127.0.0.1:4317/openai/v1/responses', model: 'deepseek-v4-flash-0731', credential_configured: true}),
        modelSave: async args => ({saved: true, endpoint: args.endpoint, model: args.model, credential_configured: true}),
        modelTest: async args => { f.testArgs = args; return {ok: true, model_calls: 1}; },
    };
    await mountPanel(f.root, f.call, {chat_id: 'chat-a'}, actions);
    f.root.querySelector('[data-settings-toggle]').click();
    await tick();
    assert.match(f.root.textContent, /模型设置/);
    assert.equal(f.root.querySelector('[data-model-name]').value, 'deepseek-v4-flash-0731');
    assert.equal(f.root.querySelector('[data-model-key]').value, '');
    const testButton = f.root.querySelector('[data-model-test]'); await testButton.onclick();
    await tick();
    assert.ok(f.testArgs);
    assert.match(f.root.textContent, /连接成功/);
});

test('defaults to Codex model with no endpoint or key fields', async () => {
    const f = fixture(); let saved;
    await mountPanel(f.root, f.call, {}, {
        modelStatus: async () => ({backend: 'codex', configured: true, model: 'codex-default', credential_required: false}),
        modelSave: async args => {saved=args;return {saved:true};},
        modelTest: async () => ({ok:true}),
    });
    f.root.querySelector('[data-settings-toggle]').click(); await tick();
    assert.match(f.root.textContent, /使用 Codex 当前配置/);
    assert.equal(f.root.querySelector('[data-model-endpoint]').closest('[data-api-fields]').hidden, true);
    f.root.querySelector('[data-model-save]').click(); await tick();
    assert.equal(saved.backend, 'codex');
    assert.equal(saved.api_key, '');
});
