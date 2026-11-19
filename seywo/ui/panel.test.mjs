import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
const require = createRequire(new URL('../../tools/seywo-ui/package.json', import.meta.url));
const { JSDOM } = require('jsdom');
import { mountPanel } from './panel.js';
const shellSource = readFileSync(fileURLToPath(new URL('./shell.html', import.meta.url)), 'utf8');
const bundledSource = readFileSync(fileURLToPath(new URL('./panel.html', import.meta.url)), 'utf8');
const tick = () => new Promise(r => setTimeout(r, 15));
const narrative = { chat_id: 'chat-a', version: 7, head_version: 7, selected_phase: 'p', node_count: 2, focus: { phase_id: 'p' },
    phases: [{ id: 'p', title: '开发插件', node_count: 2 }], branches: [{ id: 'b', phase_id: 'p', title: '检查界面', state: 'active' }],
    goals: [{ id: 'g', object_id: 'obj-g', title: '完成插件安装', evidence_count: 1 }], open_questions: [], next_steps: [], results: [{ id: 'n', object_id: 'obj-n', title: '完成插件安装', status: 'resolved', evidence_count: 1 }],
    retrieval: { has_more: false } };
function fixture() {
    const dom = new JSDOM('<main id="root"></main>', { url: 'https://seywo.test', pretendToBeVisual: true });
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
    assert.deepEqual(f.calls.map(c => c.view), ['chats', 'maintenance', 'mainline']);
    assert.match(f.root.textContent, /当前目标/);
    assert.match(f.root.textContent, /对话主线/);
    assert.doesNotMatch(f.root.textContent, /Compact|阶段记录|查看版本|个节点/);
    assert.equal(f.root.querySelector('[aria-label="历史版本"]'), null);
    f.root.querySelector('[data-phase]').click();
    await tick();
    assert.equal(f.calls.at(-1).view, 'narrative');
    assert.equal(f.calls.at(-1).version, 7);
    assert.match(f.root.textContent, /阶段明细/);
    assert.equal(f.root.querySelector('[data-records]').closest('.mainline-column') !== null, true);
    for (const source of [shellSource, bundledSource])
        assert.match(source, /\.records\{[^}]*max-height:320px/);
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

test('keeps the conversation spine as the primary column and folds long priority lists', async () => {
    const f = fixture();
    const steps = Array.from({length: 9}, (_, i) => ({id: `s${i}`, object_id: `obj-${i}`, title: `后续事项${i}`}));
    await mountPanel(f.root, async args => args.view === 'mainline' ? {...narrative, next_steps: steps} : f.call(args), {chat_id: 'chat-a'});
    const content = f.root.querySelector('[data-content]');
    assert.ok(content.querySelector('.mainline-column .mainline'));
    assert.ok(content.querySelector('.side-column .next-steps'));
    assert.ok(content.querySelector('.mainline-column').compareDocumentPosition(content.querySelector('.side-column')) & f.dom.window.Node.DOCUMENT_POSITION_FOLLOWING);
    const next = f.root.querySelector('.next-steps');
    assert.equal(next.querySelectorAll(':scope > [data-node]').length, 3);
    const more = next.querySelector('details');
    assert.equal(more.open, false);
    assert.equal(more.querySelectorAll('[data-node]').length, 6);
    assert.match(more.querySelector('summary').textContent, /其余 6 项/);
});

test('uses the current Chat by default and keeps the alternate Chat picker secondary', async () => {
    const f = fixture();
    await mountPanel(f.root, async args => args.view === 'chats'
      ? { ...await f.call(args), current_chat_id: 'chat-a', default_chat_id: 'chat-a' }
      : f.call(args));
    const switcher = f.root.querySelector('[data-chat-switcher]');
    assert.equal(switcher.open, false);
    assert.equal(f.root.querySelector('[data-chat]').value, 'chat-a');
    assert.match(switcher.textContent, /查看其他对话/);
});

test('places the conversation spine first and keeps goal and unresolved work in the side column', async () => {
    const f = fixture();
    await mountPanel(f.root, async args => args.view === 'mainline'
      ? { ...narrative, open_questions: [{ id: 'q', object_id: 'obj-q', title: '模型语义核验', status: 'open' }] }
      : f.call(args), { chat_id: 'chat-a' });
    const content = f.root.querySelector('[data-content]');
    assert.ok(content.querySelector('.goals.focus-primary'));
    assert.ok(content.querySelector('.mainline'));
    assert.ok(content.querySelector('.open-questions'));
    assert.equal(content.querySelector('.mainline-column .mainline') !== null, true);
    assert.equal(content.querySelector('.side-column .goals') !== null, true);
    assert.equal(content.querySelector('.side-column .open-questions') !== null, true);
    assert.match(content.querySelector('.goals .group-meta').textContent, /Focus/);
    assert.match(content.querySelector('.open-questions .group-meta').textContent, /1 项/);
});

test('moves the priority column above the conversation spine on narrow screens', () => {
    for (const source of [shellSource, bundledSource]) {
        const narrow = source.slice(source.indexOf('@media(max-width:760px)'));
        assert.notEqual(narrow, source);
        assert.match(narrow, /\.side-column\{[^}]*order:1/);
        assert.match(narrow, /\.mainline-column\{[^}]*order:2/);
    }
});

test('keeps the maintenance notice on one line on narrow screens', () => {
    for (const source of [shellSource, bundledSource]) {
        const narrow = source.slice(source.indexOf('@media(max-width:760px)'));
        assert.match(narrow, /\.workspace>\.maintenance \.maintenance-copy\{[^}]*white-space:nowrap/);
        assert.match(narrow, /\.workspace>\.maintenance \.maintenance-copy small\{display:inline/);
    }
});

test('keeps the three priority modules visually distinct on narrow screens', () => {
    for (const source of [shellSource, bundledSource]) {
        const narrow = source.slice(source.indexOf('@media(max-width:760px)'));
        assert.match(narrow, /\.side-column \.open-questions\{[^}]*border-top:1px solid var\(--line\)/);
        assert.match(narrow, /\.side-column \.open-questions \.group-label\{[^}]*color:#b06b32/);
        assert.match(narrow, /\.side-column \.next-steps\{[^}]*border-top:1px solid var\(--line\)/);
        assert.match(source, /\.next-steps \.focus-node\{[^}]*background:var\(--accent\)/);
        assert.match(narrow, /\.side-column \.next-steps \.focus-node\{[^}]*min-height:49px/);
    }
});

test('keeps empty goal and question modules visible instead of removing them', async () => {
    const f = fixture();
    await mountPanel(f.root, async args => args.view === 'mainline'
        ? { ...narrative, goals: [], open_questions: [], next_steps: [{ id: 's', object_id: 'obj-s', title: '重开面板确认新版布局' }] }
        : f.call(args), { chat_id: 'chat-a' });
    assert.ok(f.root.querySelector('.goals'));
    assert.ok(f.root.querySelector('.open-questions'));
    assert.match(f.root.querySelector('.goals').textContent, /当前阶段暂无未完成目标/);
    assert.match(f.root.querySelector('.open-questions').textContent, /暂无待解决事项/);
    assert.match(f.root.querySelector('.next-steps').textContent, /重开面板确认新版布局/);
});

test('renders the compact reference hierarchy and current phase affordances', async () => {
    const f = fixture();
    await mountPanel(f.root, f.call, { chat_id: 'chat-a' });
    assert.ok(f.root.querySelector('.workspace'));
    assert.ok(f.root.querySelector('.workspace [data-maintenance]'));
    assert.ok(f.root.querySelector('.workspace [data-content]'));
    assert.equal(f.root.querySelector('.mainline .branch-list').open, true);
    assert.match(f.root.querySelector('.branch-list summary').textContent, /收起 1 条支线/);
    assert.match(f.root.querySelector('.mainline .focus-tag').textContent, /当前阶段/);
    assert.match(f.root.querySelector('.mainline-head').textContent, /共 1 个阶段 · 1 条支线/);
    assert.ok(f.root.querySelector('.goals .section-icon svg'));
});

test('hides completed maintenance while retaining interrupted status', async () => {
    const f = fixture();
    await mountPanel(f.root, async args => args.view === 'maintenance'
      ? maintenance('done', args.chat_id) : f.call(args), { chat_id: 'chat-a' });
    assert.equal(f.root.querySelector('[data-maintenance]').hidden, true);
    const g = fixture();
    await mountPanel(g.root, async args => args.view === 'maintenance'
      ? maintenance('interrupted', args.chat_id, { message: '需要重试' }) : g.call(args), { chat_id: 'chat-a' });
    assert.equal(g.root.querySelector('[data-maintenance]').hidden, false);
    assert.match(g.root.querySelector('[data-maintenance]').textContent, /整理已中断/);
});

test('uses one model settings entry instead of a duplicate header control', async () => {
    const f = fixture();
    await mountPanel(f.root, f.call, {}, { modelStatus: async () => ({backend: 'codex', configured: true}) });
    assert.equal(f.root.querySelector('[data-settings-toggle]'), null);
    assert.equal(f.root.querySelectorAll('[data-settings]').length, 1);
    assert.equal(f.root.querySelectorAll('[data-settings] summary').length, 1);
    const toolbar = f.root.querySelector('.header .header-actions');
    assert.ok(toolbar.querySelector('[data-chat-switcher]'));
    assert.ok(toolbar.querySelector('[data-settings]'));
    assert.equal(f.root.querySelector(':scope > [data-settings]'), null);
    assert.equal(f.root.querySelector(':scope > [data-chat-switcher]'), null);
    assert.equal(toolbar.querySelector('[data-settings]').getAttribute('name'),
        toolbar.querySelector('[data-chat-switcher]').getAttribute('name'));
});

test('shows project-local model settings without exposing the saved key', async () => {
    const f = fixture();
    const actions = {
        modelStatus: async () => ({configured: true, endpoint: 'http://127.0.0.1:4317/openai/v1/responses', model: 'deepseek-v4-flash-0731', credential_configured: true}),
        modelSave: async args => ({saved: true, endpoint: args.endpoint, model: args.model, credential_configured: true}),
        modelTest: async args => { f.testArgs = args; return {ok: true, model_calls: 1}; },
    };
    await mountPanel(f.root, f.call, {chat_id: 'chat-a'}, actions);
    f.root.querySelector('[data-settings] summary').click();
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
    f.root.querySelector('[data-settings] summary').click(); await tick();
    assert.match(f.root.textContent, /使用 Codex 当前配置/);
    assert.equal(f.root.querySelector('[data-model-endpoint]').closest('[data-api-fields]').hidden, true);
    f.root.querySelector('[data-model-save]').click(); await tick();
    assert.equal(saved.backend, 'codex');
    assert.equal(saved.api_key, '');
});


test('first panel open initializes automatically then reloads the current Chat', async () => {
    const f=fixture(); const calls=[];
    const controller=await mountPanel(f.root,f.call,{state:'initialization_required',project_root:'/work/new'}, {
        initializeProject:async()=>{calls.push('initialize'); assert.match(f.root.textContent,/正在初始化项目记忆/); return {};},
        openCurrent:async()=>{calls.push('open'); return {chat_id:'chat-a',initialized:true};}
    });
    assert.deepEqual(calls,['initialize','open']);
    assert.equal(controller.state.chat,'chat-a');
    assert.match(f.root.textContent,/对话主线/);
    assert.doesNotMatch(f.root.textContent,/请在对话中要求启用/);
});

test('initialization error shows its cause and retry reopens and loads the current Chat', async () => {
    const f=fixture(); let opens=0;
    await mountPanel(f.root,f.call,{state:'initialization_failed',error:'无法写入索引数据库：权限不足',retryable:true}, {
        openCurrent:async()=>{opens++;return {chat_id:'chat-a',initialized:true};}
    });
    assert.match(f.root.textContent,/权限不足/);
    assert.equal(f.calls.length,0);
    f.root.querySelector('[data-retry]').click();await tick();
    assert.equal(opens,1);
    assert.match(f.root.textContent,/对话主线/);
});

test('selected uninitialized project opens automatically; unbound panel never initializes', async () => {
    const f=fixture(); let opens=0; let initialized=0;
    await mountPanel(f.root,f.call,{state:'project_required'}, {
        selectProject:async path=>({initialized:false,project_root:path}),
        initializeProject:async()=>{initialized++;},
        openCurrent:async()=>{opens++;return {chat_id:'chat-a',initialized:true};}
    });
    assert.equal(opens,0); assert.equal(initialized,0); assert.equal(f.calls.length,0);
    f.root.querySelector('[data-project-path]').value='/work/new';
    f.root.querySelector('[data-bind-project]').click(); await tick();
    assert.equal(opens,1); assert.match(f.root.textContent,/对话主线/);
});

test('an initialized panel loads directly without calling initialization', async () => {
    const f=fixture();let initializations=0;
    await mountPanel(f.root,f.call,{chat_id:'chat-a'}, {
        initializeProject:async()=>{initializations++;}
    });
    assert.equal(initializations,0);
    assert.match(f.root.textContent,/对话主线/);
});

test('initialization transport failures retain the real cause safely and allow retry', async () => {
    const f=fixture();let opens=0;
    const call=async args=>args.view==='chats' ? {project_root:'/work/new',results:[],current_chat_id:'new-chat',retrieval:{has_more:false}} : f.call(args);
    await mountPanel(f.root,call,{state:'initialization_required'}, {
        initializeProject:async()=>{const error=new Error('panel_read_failed');error.initializationCause='权限不足 <img src=x onerror=alert(1)>';throw error;},
        openCurrent:async()=>{opens++;return {current_chat_id:'new-chat',initialized:true};}
    });
    assert.match(f.root.textContent,/权限不足/);
    assert.equal(f.root.querySelector('img'),null);
    assert.equal(f.calls.length,0);
    f.root.querySelector('[data-retry]').click();await tick();
    assert.equal(opens,1);
    assert.match(f.root.textContent,/项目记忆已启用/);
    assert.match(f.root.textContent,/当前对话尚未收录/);
});

test('a delayed initialization cannot replace a newer project picker', async () => {
    const f=fixture();let finish;let delay=false;let opens=0;
    const controller=await mountPanel(f.root,f.call,{chat_id:'chat-a'}, {
        initializeProject:async()=>{if(delay)await new Promise(resolve=>{finish=resolve;});},
        openCurrent:async()=>{opens++;return {chat_id:'chat-a',initialized:true};}
    });
    delay=true;
    const old=controller.open({state:'initialization_required'});
    await controller.open({state:'project_required'});
    finish();await old;
    assert.equal(opens,0);
    assert.ok(f.root.querySelector('[data-project-path]'));
    assert.doesNotMatch(f.root.textContent,/项目记忆已启用/);
});


const flush = () => new Promise(resolve => setImmediate(resolve));
const maintenance = (state, chat_id = 'chat-a', extra = {}) => ({
    chat_id, state, title: '', message: '', retryable: ['failed', 'partial', 'interrupted', 'blocked'].includes(state),
    updated_at: null, last_success_at: null, stale_view: false, model_calls: 0, ...extra,
});

test('failed maintenance preserves mainline and retries once while submitting', async () => {
    const f = fixture(); let finish; const retries = [];
    await mountPanel(f.root, args => args.view === 'maintenance'
        ? maintenance('failed', args.chat_id, {stale_view: true, message: '本次整理未完成'}) : f.call(args), {chat_id: 'chat-a'}, {
        maintenanceRetry: chat => { retries.push(chat); return new Promise(resolve => {finish = resolve;}); },
    });
    const status = f.root.querySelector('[data-maintenance]');
    assert.equal(status.getAttribute('role'), 'status');
    assert.match(status.textContent, /整理失败/);
    assert.match(status.textContent, /整理成功后会更新主线。/);
    const line = f.root.querySelector('.mainline');
    const retry = status.querySelector('[data-maintenance-retry]');
    retry.click(); retry.click();
    assert.equal(status.querySelector('[data-maintenance-retry]').disabled, true);
    assert.deepEqual(retries, ['chat-a']);
    finish({accepted: true, maintenance: maintenance('queued')}); await flush();
    assert.match(status.textContent, /等待整理/);
    assert.equal(f.root.querySelector('.mainline'), line);
    assert.match(f.root.querySelector('[data-content]').textContent, /完成插件安装/);
});

test('polls active maintenance then reloads mainline once on done without changing Chat', async t => {
    t.mock.timers.enable({apis: ['setTimeout', 'Date']});
    const f = fixture(); const reads = []; let phase = 'running'; let mainlines = 0;
    const ctrl = await mountPanel(f.root, args => {
        if (args.view === 'maintenance') {reads.push(args); return maintenance(phase, args.chat_id);}
        if (args.view === 'mainline') mainlines++;
        return f.call(args);
    }, {chat_id: 'chat-a'});
    assert.equal(reads.length, 1); assert.equal(mainlines, 1);
    phase = 'done'; t.mock.timers.tick(4000); await flush();
    assert.equal(reads.length, 2); assert.equal(mainlines, 2);
    assert.equal(ctrl.state.chat, 'chat-a');
    assert.equal(f.root.querySelector('[data-chat]').value, 'chat-a');
    t.mock.timers.tick(300000); await flush();
    assert.equal(reads.length, 2); assert.equal(mainlines, 2);
});

test('late maintenance reads and retry results cannot label a different selected Chat', async () => {
    const f = fixture(); let oldRead; let oldRetry; let delay = false;
    const ctrl = await mountPanel(f.root, args => {
        if (args.view === 'maintenance') {
            if (args.chat_id === 'chat-a' && delay) return new Promise(resolve => {oldRead = resolve;});
            return maintenance('failed', args.chat_id, {title: `整理 ${args.chat_id}`});
        }
        return f.call(args);
    }, {chat_id: 'chat-a'}, {
        maintenanceRetry: () => new Promise(resolve => {oldRetry = resolve;}),
    });
    f.root.querySelector('[data-maintenance-retry]').click();
    await ctrl.selectChat('chat-b');
    oldRetry({accepted: true, maintenance: maintenance('running', 'chat-a', {title: '旧重试'})}); await flush();
    assert.match(f.root.querySelector('[data-maintenance]').textContent, /chat-b/);
    assert.doesNotMatch(f.root.querySelector('[data-maintenance]').textContent, /旧重试/);
    delay = true; const pending = ctrl.selectChat('chat-a'); await flush();
    await ctrl.selectChat('chat-b');
    oldRead(maintenance('done', 'chat-a', {title: '过期状态'})); await pending;
    assert.match(f.root.querySelector('[data-maintenance]').textContent, /chat-b/);
    assert.doesNotMatch(f.root.textContent, /过期状态/);
});

test('loads and retries maintenance for unimported exact host Chat from bootstrap', async () => {
    const f = fixture(); const reads = []; const retries = [];
    const ctrl = await mountPanel(f.root, async args => {
        if (args.view === 'chats') return {...await f.call(args), current_chat_id: 'host-unimported'};
        if (args.view === 'maintenance') {reads.push(args); return maintenance('partial', args.chat_id);}
        return f.call(args);
    }, {}, {maintenanceRetry: async chat => {retries.push(chat); return {accepted: false, maintenance: maintenance('blocked', chat)};}});
    assert.equal(ctrl.state.chat, '');
    assert.match(f.root.textContent, /当前对话尚未收录/);
    assert.match(f.root.querySelector('[data-maintenance]').textContent, /部分完成/);
    assert.deepEqual(reads, [{view: 'maintenance', chat_id: 'host-unimported'}]);
    assert.equal(f.calls.some(c => c.view === 'mainline'), false);
    f.root.querySelector('[data-maintenance-retry]').click(); await flush();
    assert.deepEqual(retries, ['host-unimported']);
    await ctrl.selectChat('chat-a');
    assert.equal(reads.at(-1).chat_id, 'chat-a');
});

test('unsupported maintenance reads and failed retry stay safe and preserve content', async () => {
    const f = fixture(); let reads = 0; let supported = false;
    await mountPanel(f.root, args => {
        if (args.view === 'maintenance') {
            reads++;
            if (!supported) throw Error('Traceback /private API_KEY');
            return maintenance('failed');
        }
        return f.call(args);
    }, {chat_id: 'chat-a'}, {maintenanceRetry: async () => {throw Error('Traceback /secret API_KEY');}});
    const line = f.root.querySelector('.mainline');
    assert.match(f.root.querySelector('[data-maintenance]').textContent, /暂时无法读取整理状态/);
    supported = true;
    f.root.querySelector('[data-maintenance-refresh]').click(); await flush();
    assert.equal(reads, 2);
    f.root.querySelector('[data-maintenance-retry]').click(); await flush();
    assert.match(f.root.querySelector('[data-maintenance]').textContent, /未能提交重试/);
    assert.ok(f.root.querySelector('[data-maintenance-retry]'));
    assert.doesNotMatch(f.root.textContent, /Traceback|private|secret|API_KEY/);
    assert.equal(f.root.querySelector('.mainline'), line);
});

test('polling pauses while hidden, ignores hidden responses, and expires after five minutes', async t => {
    t.mock.timers.enable({apis: ['setTimeout', 'Date']});
    const f = fixture(); let hidden = false; let reads = 0; let finish; let delay = false;
    Object.defineProperty(f.dom.window.document, 'hidden', {get: () => hidden});
    const ctrl = await mountPanel(f.root, args => {
        if (args.view !== 'maintenance') return f.call(args);
        reads++;
        if (delay) return new Promise(resolve => {finish = resolve;});
        return maintenance('running');
    }, {chat_id: 'chat-a'});
    delay = true; t.mock.timers.tick(4000); await flush(); assert.equal(reads, 2);
    hidden = true; f.dom.window.document.dispatchEvent(new f.dom.window.Event('visibilitychange'));
    finish(maintenance('failed', 'chat-a', {title: '隐藏响应'})); await flush();
    assert.doesNotMatch(f.root.textContent, /隐藏响应/);
    t.mock.timers.tick(20000); await flush(); assert.equal(reads, 2);
    delay = false; hidden = false; f.dom.window.document.dispatchEvent(new f.dom.window.Event('visibilitychange'));
    t.mock.timers.tick(4000); await flush(); assert.equal(reads, 3);
    t.mock.timers.tick(300000); await flush();
    assert.equal(reads, 3);
    await ctrl.refresh(); assert.equal(reads, 4);
    t.mock.timers.tick(4000); await flush(); assert.equal(reads, 5);
});


test('reopening and rebinding discard pending maintenance and cancel old polling', async t => {
    t.mock.timers.enable({apis: ['setTimeout', 'Date']});
    const f = fixture(); let finish; let delay = false; let reads = 0;
    const ctrl = await mountPanel(f.root, args => {
        if (args.view === 'maintenance') {
            reads++;
            if (delay) return new Promise(resolve => {finish = resolve;});
            return maintenance('running', args.chat_id);
        }
        return f.call(args);
    }, {chat_id: 'chat-a'}, {selectProject: async () => ({initialized: true})});
    delay = true; t.mock.timers.tick(4000); await flush();
    await ctrl.open({state: 'project_required'});
    finish(maintenance('failed', 'chat-a', {title: '旧项目失败'})); await flush();
    assert.equal(f.root.querySelector('[data-maintenance]').hidden, true);
    assert.doesNotMatch(f.root.textContent, /旧项目失败/);
    t.mock.timers.tick(4000); await flush(); assert.equal(reads, 2);
    delay = false;
    f.root.querySelector('[data-project-path]').value = '/work/new';
    f.root.querySelector('[data-bind-project]').click(); await flush();
    assert.equal(f.root.querySelector('[data-maintenance]').hidden, true);
    assert.equal(reads, 2);
    await ctrl.open({chat_id: 'chat-b'});
    assert.equal(reads, 3);
    assert.equal(ctrl.state.chat, 'chat-b');
});

test('active to partial retains published mainline and stops polling', async t => {
    t.mock.timers.enable({apis: ['setTimeout', 'Date']});
    const f = fixture(); let status = 'running'; let reads = 0; let mainlines = 0;
    await mountPanel(f.root, args => {
        if (args.view === 'maintenance') {reads++; return maintenance(status);}
        if (args.view === 'mainline') mainlines++;
        return f.call(args);
    }, {chat_id: 'chat-a'});
    const line = f.root.querySelector('.mainline');
    status = 'partial'; t.mock.timers.tick(4000); await flush();
    assert.equal(f.root.querySelector('.mainline'), line);
    assert.match(f.root.querySelector('[data-maintenance]').textContent, /部分完成.*整理成功后会更新主线。/s);
    t.mock.timers.tick(20000); await flush();
    assert.equal(reads, 2); assert.equal(mainlines, 1);
});

test('unimported Chat completion loads its new mainline without repeated refreshes', async t => {
    t.mock.timers.enable({apis: ['setTimeout', 'Date']});
    const f = fixture(); let imported = false; let chatReads = 0; let statusReads = 0;
    const ctrl = await mountPanel(f.root, async args => {
        if (args.view === 'chats') {
            chatReads++;
            return {...await f.call(args), current_chat_id: 'host-new', default_chat_id: imported ? 'host-new' : null};
        }
        if (args.view === 'maintenance') {statusReads++; return maintenance(imported ? 'done' : 'running', args.chat_id);}
        return f.call(args);
    });
    imported = true; t.mock.timers.tick(4000); await flush();
    assert.equal(ctrl.state.chat, 'host-new');
    assert.match(f.root.querySelector('[data-content]').textContent, /对话主线/);
    assert.equal(chatReads, 2);
    t.mock.timers.tick(20000); await flush();
    assert.equal(chatReads, 2); assert.equal(statusReads, 3);
});

test('mismatched maintenance Chat IDs show safe refresh notice rather than wrong status', async () => {
    const f = fixture();
    await mountPanel(f.root, args => args.view === 'maintenance'
        ? maintenance('failed', 'chat-other', {title: '其他对话状态'}) : f.call(args), {chat_id: 'chat-a'});
    assert.doesNotMatch(f.root.textContent, /其他对话状态/);
    assert.match(f.root.querySelector('[data-maintenance]').textContent, /暂时无法读取整理状态/);
    assert.ok(f.root.querySelector('[data-maintenance-refresh]'));
    assert.match(f.root.querySelector('[data-content]').textContent, /对话主线/);
});


test('completion refresh failure keeps the published mainline available', async t => {
    t.mock.timers.enable({apis: ['setTimeout', 'Date']});
    const f = fixture(); let done = false;
    await mountPanel(f.root, args => {
        if (args.view === 'maintenance') return maintenance(done ? 'done' : 'running');
        if (args.view === 'mainline' && done) throw Error('private backend details');
        return f.call(args);
    }, {chat_id: 'chat-a'});
    const line = f.root.querySelector('.mainline');
    done = true; t.mock.timers.tick(4000); await flush();
    assert.equal(f.root.querySelector('.mainline'), line);
    assert.equal(f.root.querySelector('[data-content]').hasAttribute('aria-busy'), false);
    assert.doesNotMatch(f.root.textContent, /private backend details/);
    assert.match(f.root.querySelector('[data-notice]').textContent, /暂时无法读取/);
});


for (const version of [0, 7]) {
    test(`maintenance copy stays accurate before and after parallel mainline version ${version}`, async () => {
        const f = fixture(); let finishMainline;
        const pending = mountPanel(f.root, args => {
            if (args.view === 'maintenance') return maintenance('failed', args.chat_id, {stale_view: true});
            if (args.view === 'mainline') return new Promise(resolve => {finishMainline = resolve;});
            return f.call(args);
        }, {chat_id: 'chat-a'});
        await flush();
        const status = f.root.querySelector('[data-maintenance]');
        assert.match(status.textContent, /整理成功后会更新主线。/);
        assert.doesNotMatch(status.textContent, /已有主线|上次成功/);
        finishMainline({...narrative, version}); await pending;
        assert.match(status.textContent, /整理成功后会更新主线。/);
        assert.doesNotMatch(status.textContent, /已有主线|上次成功/);
        if (version) assert.ok(f.root.querySelector('.mainline'));
        else assert.match(f.root.querySelector('[data-content]').textContent, /还没有发布主线/);
    });
}

test('late maintenance copy leaves a parallel mainline reading error visible', async () => {
    const f = fixture(); let finishStatus;
    const pending = mountPanel(f.root, args => {
        if (args.view === 'maintenance') return new Promise(resolve => {finishStatus = resolve;});
        if (args.view === 'mainline') throw Error('private error');
        return f.call(args);
    }, {chat_id: 'chat-a'});
    await flush();
    const notice = f.root.querySelector('[data-notice]');
    assert.match(notice.textContent, /暂时无法读取/);
    finishStatus(maintenance('failed', 'chat-a', {stale_view: true})); await pending;
    assert.match(f.root.querySelector('[data-maintenance]').textContent, /整理成功后会更新主线。/);
    assert.match(notice.textContent, /暂时无法读取/);
    assert.ok(notice.querySelector('[data-retry]'));
    assert.doesNotMatch(f.root.textContent, /已有主线|private error/);
});


for (const publishedVersion of [8, null, undefined, 7, 6]) {
    test(`initial done reconciles published version ${publishedVersion} after parallel reads once`, async t => {
        t.mock.timers.enable({apis: ['setTimeout', 'Date']});
        const f = fixture(); let finishStatus; let mainlines = 0;
        const pending = mountPanel(f.root, args => {
            if (args.view === 'maintenance') return new Promise(resolve => {finishStatus = resolve;});
            if (args.view === 'mainline') {
                mainlines++;
                return {...narrative, version: mainlines === 1 ? 7 : 8};
            }
            return f.call(args);
        }, {chat_id: 'chat-a'});
        await flush();
        // Rendering remains independent of the delayed maintenance response.
        assert.match(f.root.querySelector('[data-content]').textContent, /对话主线/);
        assert.equal(mainlines, 1);
        finishStatus(maintenance('done', 'chat-a', {published_version: publishedVersion}));
        const ctrl = await pending;
        const needsReload = publishedVersion == null || publishedVersion > 7;
        assert.equal(mainlines, needsReload ? 2 : 1);
        assert.equal(ctrl.state.version, needsReload ? 8 : 7);
        assert.equal(ctrl.state.chat, 'chat-a');
        t.mock.timers.tick(300000); await flush();
        assert.equal(mainlines, needsReload ? 2 : 1);
    });
}

test('initial done waits for a delayed mainline before comparing published versions', async () => {
    const f = fixture(); let finishMainline; let reads = 0;
    const pending = mountPanel(f.root, args => {
        if (args.view === 'maintenance') return maintenance('done', args.chat_id, {published_version: 8});
        if (args.view === 'mainline') {
            reads++;
            if (reads === 1) return new Promise(resolve => {finishMainline = resolve;});
            return {...narrative, version: 8};
        }
        return f.call(args);
    }, {chat_id: 'chat-a'});
    await flush(); assert.equal(reads, 1);
    finishMainline({...narrative, version: 7});
    const ctrl = await pending;
    assert.equal(reads, 2); assert.equal(ctrl.state.version, 8);
});

test('initial done reconciliation preserves the existing mainline if its reload fails', async () => {
    const f = fixture(); let finishStatus; let reads = 0;
    const pending = mountPanel(f.root, args => {
        if (args.view === 'maintenance') return new Promise(resolve => {finishStatus = resolve;});
        if (args.view === 'mainline' && ++reads > 1) throw Error('private backend failure');
        return f.call(args);
    }, {chat_id: 'chat-a'});
    await flush();
    const line = f.root.querySelector('.mainline');
    finishStatus(maintenance('done', 'chat-a', {published_version: 8})); await pending;
    assert.equal(reads, 2);
    assert.equal(f.root.querySelector('.mainline'), line);
    assert.match(f.root.querySelector('[data-notice]').textContent, /暂时无法读取/);
    assert.doesNotMatch(f.root.textContent, /private backend failure/);
});

test('active to done during initial loading does not cause a duplicate completion reload', async t => {
    t.mock.timers.enable({apis: ['setTimeout', 'Date']});
    const f = fixture(); let finishMainline; let reads = 0; let done = false;
    const pending = mountPanel(f.root, args => {
        if (args.view === 'maintenance') return maintenance(done ? 'done' : 'running', args.chat_id);
        if (args.view === 'mainline') {
            reads++;
            if (reads === 1) return new Promise(resolve => {finishMainline = resolve;});
            return {...narrative, version: 8};
        }
        return f.call(args);
    }, {chat_id: 'chat-a'});
    await flush();
    done = true; t.mock.timers.tick(4000); await flush();
    assert.equal(reads, 2);
    finishMainline({...narrative, version: 7});
    const ctrl = await pending;
    assert.equal(reads, 2); assert.equal(ctrl.state.version, 8);
});

test('chat switch during initial reads cannot reconcile the old done status into the new Chat', async () => {
    const f = fixture(); let finishOld; let delay = false; const reads = [];
    const ctrl = await mountPanel(f.root, args => {
        if (args.view === 'maintenance') {
            if (delay && args.chat_id === 'chat-a') return new Promise(resolve => {finishOld = resolve;});
            return maintenance('failed', args.chat_id);
        }
        if (args.view === 'mainline') reads.push(args.chat_id);
        return f.call(args);
    }, {chat_id: 'chat-a'});
    delay = true;
    const oldSelection = ctrl.selectChat('chat-a'); await flush();
    await ctrl.selectChat('chat-b');
    finishOld(maintenance('done', 'chat-a', {published_version: 8})); await oldSelection;
    assert.equal(ctrl.state.chat, 'chat-b');
    assert.deepEqual(reads, ['chat-a', 'chat-a', 'chat-b']);
    assert.equal(f.root.querySelector('[data-maintenance]').dataset.state, 'failed');
});

test('empty mainline shows phase failure and an actionable retry regardless of read order', async () => {
    for (const statusFirst of [true, false]) {
        const f = fixture(); let finishStatus, finishMainline; const retries = [];
        const mounting = mountPanel(f.root, args => {
            if (args.view === 'maintenance') return new Promise(resolve => {finishStatus = resolve;});
            if (args.view === 'mainline') return new Promise(resolve => {finishMainline = resolve;});
            return f.call(args);
        }, {chat_id: 'chat-a'}, {maintenanceRetry: async chat => {
            retries.push(chat); return {accepted: true, maintenance: maintenance('queued', chat)};
        }});
        await flush();
        const failed = maintenance('failed', 'chat-a', {message: '新阶段缺少本段用户目标的有效定位，模型修正后仍未通过校验。可以重试整理。'});
        if (statusFirst) { finishStatus(failed); await flush(); finishMainline({chat_id: 'chat-a', version: 0}); }
        else { finishMainline({chat_id: 'chat-a', version: 0}); await flush(); finishStatus(failed); }
        await mounting;
        assert.match(f.root.querySelector('[data-content]').textContent, /主线整理失败/);
        assert.match(f.root.querySelector('[data-maintenance]').textContent, /本段用户目标/);
        f.root.querySelector('[data-maintenance-retry]').click(); await flush();
        assert.deepEqual(retries, ['chat-a']);
        assert.match(f.root.querySelector('[data-maintenance]').textContent, /等待整理/);
        assert.doesNotMatch(f.root.querySelector('[data-content]').textContent, /主线整理失败/);
    }
});

test('waiting for a compact turn remains retryable and polls through publication', async t => {
    t.mock.timers.enable({apis: ['setTimeout', 'Date']});
    const f = fixture(); let completed = false; let mainlines = 0;
    await mountPanel(f.root, args => {
        if (args.view === 'maintenance') return maintenance(completed ? 'done' : 'partial', args.chat_id,
            {waiting_for_turn: !completed, retryable: !completed, message: completed ? '' : '等待本轮对话结束后继续整理。'});
        if (args.view === 'mainline') mainlines++;
        return f.call(args);
    }, {chat_id: 'chat-a'}, {maintenanceRetry: async () => ({accepted:true,maintenance:maintenance('queued')})});
    assert.ok(f.root.querySelector('[data-maintenance-retry]'));
    assert.notEqual(f.root.querySelector('.maintenance-mark').textContent, '✓');
    completed = true;
    t.mock.timers.tick(4000); await flush();
    assert.equal(f.root.querySelector('[data-maintenance]').hidden, true);
    assert.equal(mainlines, 2);
});

test('lists host models on demand and sends selected Codex model to test and save', async () => {
    const f = fixture(); let reads = 0, tested, saved;
    await mountPanel(f.root, f.call, {}, {
        modelStatus: async () => ({ backend: 'codex', configured: true, model: 'codex-default' }),
        modelList: async () => { reads++; return { models: [{ model: 'available-model', label: 'Available' }], model_calls: 0 }; },
        modelTest: async args => { tested = args; return { ok: true }; },
        modelSave: async args => { saved = args; return { saved: true }; },
    });
    assert.equal(reads, 0);
    f.root.querySelector('[data-settings] summary').click(); await tick(); await tick();
    assert.equal(reads, 1);
    const selector = f.root.querySelector('[data-codex-model]');
    assert.ok(selector);
    assert.equal(selector.value, 'codex-default');
    assert.ok([...selector.options].some(o => o.value === 'available-model'));
    selector.value = 'available-model';
    await f.root.querySelector('[data-model-test]').onclick();
    f.root.querySelector('[data-model-save]').click(); await tick();
    assert.equal(tested.model, 'available-model');
    assert.equal(saved.model, 'available-model');
});

test('shows safe actionable Codex failure message and preserves unavailable saved model', async () => {
    const f = fixture();
    await mountPanel(f.root, f.call, {}, {
        modelStatus: async () => ({ backend: 'codex', configured: true, model: 'saved-model' }),
        modelList: async () => ({ models: [], error: 'codex_timeout', message: '读取模型列表超时，请重试。' }),
        modelTest: async () => ({ ok: false, error: 'codex_auth_required', message: '请先在本机 Codex 登录后重试。' }),
    });
    f.root.querySelector('[data-settings] summary').click(); await tick(); await tick();
    assert.equal(f.root.querySelector('[data-codex-model]').value, 'saved-model');
    assert.match(f.root.textContent, /读取模型列表超时/);
    await f.root.querySelector('[data-model-test]').onclick();
    assert.match(f.root.querySelector('[data-model-status]').textContent, /本机 Codex 登录/);
});

test('reasoning picker follows model capabilities and sends the same effort to test and save', async () => {
    const f = fixture(); let tested, saved;
    await mountPanel(f.root, f.call, {}, {
        modelStatus: async () => ({backend:'codex', configured:true, model:'codex-default', effective_model:'configured', reasoning_effort:'high'}),
        modelList: async () => ({models:[
            {model:'catalog-default', is_default:true, reasoning_efforts:['none']},
            {model:'configured', reasoning_efforts:['low','high']},
            {model:'second', reasoning_efforts:['minimal','medium']}
        ]}),
        modelTest: async args => {tested=args;return {ok:true, model_info:{selected_model:'configured', reasoning_effort:args.reasoning_effort}};},
        modelSave: async args => {saved=args;return {saved:true};},
    });
    f.root.querySelector('[data-settings] summary').click(); await tick(); await tick();
    const effort=f.root.querySelector('[data-codex-reasoning]');
    assert.ok(effort);
    assert.equal(effort.value, 'high');
    assert.deepEqual([...effort.options].map(o=>o.value), ['auto','low','high']);
    effort.value='low';
    await f.root.querySelector('[data-model-test]').onclick();
    assert.match(f.root.querySelector('[data-model-status]').textContent, /low|低/);
    f.root.querySelector('[data-model-save]').click(); await tick();
    assert.equal(tested.reasoning_effort, 'low');
    assert.equal(saved.reasoning_effort, 'low');
    const model=f.root.querySelector('[data-codex-model]');
    model.value='second';model.dispatchEvent(new f.dom.window.Event('change'));
    assert.deepEqual([...effort.options].map(o=>o.value), ['auto','minimal','medium']);
    assert.equal(effort.value, 'auto');
});

test('failed model catalog preserves saved effort and retries without generation', async () => {
    const f=fixture();let reads=0, tests=0, saved;
    await mountPanel(f.root, f.call, {}, {
        modelStatus:async()=>({backend:'codex',configured:true,model:'chosen',reasoning_effort:'high'}),
        modelList:async()=>{reads++;return reads===1?{error:'codex_network_error'}:{models:[{model:'chosen',reasoning_efforts:['low','high']}]};},
        modelTest:async()=>{tests++;return {ok:true};},
        modelSave:async args=>{saved=args;return {saved:true};},
    });
    f.root.querySelector('[data-settings] summary').click();await tick();await tick();
    const effort=f.root.querySelector('[data-codex-reasoning]');
    assert.equal(effort.value,'high');
    assert.match(f.root.querySelector('[data-reasoning-status]').textContent,/未确认/);
    f.root.querySelector('[data-model-list-refresh]').click();await tick();await tick();
    assert.equal(effort.value,'high');
    assert.deepEqual([...effort.options].map(o=>o.value),['auto','low','high']);
    f.root.querySelector('[data-model-save]').click();await tick();
    assert.equal(saved.reasoning_effort,'high');
    assert.equal(tests,0);
});

test('switching an explicit model back to default uses the configured model rather than the catalog default', async () => {
    const f=fixture();
    await mountPanel(f.root, f.call, {}, {
        modelStatus:async()=>({backend:'codex',configured:true,model:'explicit',effective_model:'explicit',reasoning_effort:'auto'}),
        modelList:async()=>({default_model:'configured',models:[
            {model:'catalog-default',is_default:true,reasoning_efforts:['none']},
            {model:'configured',reasoning_efforts:['low','high']},
            {model:'explicit',reasoning_efforts:['medium']}
        ]}),
    });
    f.root.querySelector('[data-settings] summary').click();await tick();await tick();
    const model=f.root.querySelector('[data-codex-model]'),effort=f.root.querySelector('[data-codex-reasoning]');
    assert.deepEqual([...effort.options].map(o=>o.value),['auto','medium']);
    model.value='codex-default';model.dispatchEvent(new f.dom.window.Event('change'));
    assert.deepEqual([...effort.options].map(o=>o.value),['auto','low','high']);
});
