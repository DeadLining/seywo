/** Project panel controller. Transport is supplied by the MCP Apps entrypoint. */
export async function mountPanel(root, call, initial = {}, actions = {}) {
    const doc = root.ownerDocument;
    const state = { chat: initial.chat_id || '', chats: [], version: null, phase: null, branch: null, offset: 0, epoch: 0, chatEpoch: 0, bootstrapEpoch: 0, data: null, project: '', chatPage: null, recordsOpen: false };
    const labels = { open: '进行中', resolved: '已记录结果', active: '进行中', waiting: '等待中', abandoned: '已放弃', added: '新增', updated: '更新', merged: '合并', hidden: '隐藏', removed: '移除' };
    const el = (tag, text, cls) => { const n = doc.createElement(tag); if (text !== undefined)
        n.textContent = text; if (cls)
        n.className = cls; return n; };
    const button = (text, fn, cls = '') => { const b = el('button', text, cls); b.type = 'button'; b.onclick = fn; return b; };
    root.innerHTML = '<header class="header"><div class="brand"><span class="mark">S</span><div><h1>Seywo</h1><p data-project>项目记忆</p></div></div><div class="header-actions"><button data-refresh aria-label="刷新记录" title="刷新记录"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 7a7 7 0 0 1 11.5-1L20 9M4 15l2.4 3a7 7 0 0 0 11.5-1"/></svg></button></div></header><div class="selector"><label for="chat">对话</label><select id="chat" data-chat><option value="">选择一个 Chat</option></select></div><button data-more-chats class="more" hidden>更多对话</button><div data-notice role="status"></div><details data-settings><summary>模型设置</summary><div data-settings-body></div></details><main data-content></main><aside data-drawer hidden aria-label="节点来源"><button data-close aria-label="关闭来源">×</button><div data-detail></div></aside><footer>本地记录 · 按需读取原文</footer>';
    const content = root.querySelector('[data-content]'), notice = root.querySelector('[data-notice]');
    const drawer = root.querySelector('[data-drawer]'), detail = root.querySelector('[data-detail]');
    const settings = root.querySelector('[data-settings]'), settingsBody = root.querySelector('[data-settings-body]');
    let settingsLoaded = false, settingsLoading = false;
    const clearNotice = () => notice.replaceChildren();
    function error(retry) { notice.replaceChildren(el('p', '暂时无法读取这部分记录。你可以重试。')); const b = button('重试', retry); b.dataset.retry = ''; notice.append(b); }
    function renderSettingsStatus(data) {
        const source = el('select'); source.dataset.modelBackend = '';
        for (const [value, label] of [['codex', '使用 Codex 当前配置'], ['responses', '自定义模型 API']]) {
            const option = el('option', label); option.value = value; source.append(option);
        }
        source.value = data.backend || 'responses';
        const endpoint = el('input'); endpoint.type = 'url'; endpoint.placeholder = 'http://127.0.0.1:4317/openai/v1'; endpoint.value = data.endpoint || ''; endpoint.dataset.modelEndpoint = '';
        const model = el('input'); model.type = 'text'; model.placeholder = '模型名称'; model.value = data.backend === 'codex' ? '' : data.model || ''; model.dataset.modelName = '';
        const key = el('input'); key.type = 'password'; key.autocomplete = 'off'; key.placeholder = data.credential_configured ? '已保存，留空沿用' : 'API KEY'; key.dataset.modelKey = '';
        const form = el('form', undefined, 'model-settings-form');
        const sourceLabel = el('label', '模型来源'); sourceLabel.append(source);
        const fields = el('div', undefined, 'model-settings-fields'); fields.dataset.apiFields = '';
        for (const [label, input] of [['端点', endpoint], ['模型', model], ['API KEY', key]]) { const l = el('label', label); l.append(input); fields.append(l); }
        const nativeInfo = el('p', '沿用 Codex 的默认模型、提供方和登录信息，无需 API KEY。', 'muted');
        const showFields = () => {
            const native = source.value === 'codex';
            fields.hidden = native; nativeInfo.hidden = !native;
            endpoint.required = model.required = !native;
        };
        source.onchange = showFields; showFields();
        const actionRow = el('div', undefined, 'model-settings-actions');
        const test = button('测试模型', undefined); test.dataset.modelTest = '';
        const save = button('保存配置', undefined); save.dataset.modelSave = ''; save.type = 'submit';
        actionRow.append(test, save); form.append(sourceLabel, nativeInfo, fields, actionRow);
        const status = el('p', data.backend === 'codex'
            ? (data.configured ? '正在使用 Codex。' : '请先安装并登录 Codex 命令行。')
            : (data.configured ? (data.credential_configured ? '已配置，可供 Compact 使用。' : '还没有 API KEY。') : '尚未配置模型。'), 'muted');
        status.dataset.modelStatus = ''; settingsBody.replaceChildren(status, form);
        const run = async kind => {
            test.disabled = save.disabled = source.disabled = true;
            try {
                const native = source.value === 'codex';
                const args = { backend: source.value, endpoint: native ? '' : endpoint.value.trim(), model: native ? '' : model.value.trim(), api_key: native ? '' : key.value };
                status.textContent = kind === 'test' ? '正在测试模型…' : '正在保存…';
                const result = kind === 'test' ? await actions.modelTest?.(args) : await actions.modelSave?.(args);
                if (!result) throw Error('unavailable');
                if (kind === 'test') status.textContent = result.ok ? '连接成功，本次仅调用 1 次模型。' : '连接失败，请检查模型配置或 Codex 登录状态。';
                else { status.textContent = result.persisted === false ? '已使用 Codex 当前配置，无需写入项目。' : '已保存，后续 Compact 将使用该配置。'; key.value = ''; key.placeholder = '已保存，留空沿用'; }
            } catch { status.textContent = '模型设置暂时不可用，请稍后重试。'; }
            finally { test.disabled = save.disabled = source.disabled = false; }
        };
        test.onclick = () => void run('test');
        form.onsubmit = event => { event.preventDefault(); void run('save'); };
    }
    async function loadSettings() {
        if (!actions.modelStatus || settingsLoaded || settingsLoading) return;
        settingsLoading = true;
        settingsBody.replaceChildren(el('p', '正在读取模型设置…', 'muted'));
        try { renderSettingsStatus(await actions.modelStatus()); settingsLoaded = true; }
        catch { settingsBody.replaceChildren(el('p', '模型设置暂时不可用。', 'muted')); }
        finally { settingsLoading = false; }
    }
    function empty(text, subtitle = '') { content.replaceChildren(el('div', text, 'empty')); if (subtitle)
        content.append(el('p', subtitle, 'muted center')); }
    function nodeButton(item, cls = 'node') {
        const b = button('', () => showNode(item.object_id), cls);
        b.dataset.node = item.object_id;
        b.append(el('span', item.title || '未命名节点', 'node-title'));
        const meta = el('span', undefined, 'node-meta');
        meta.append(el('span', labels[item.status] || '记录', 'status ' + (item.status === 'resolved' ? 'done' : '')), el('span', `${item.evidence_count ?? 0} 处来源 ↗`));
        b.append(meta);
        return b;
    }
    function renderGroup(title, items, cls = '') {
        if (!items?.length) return;
        const section = el('section', undefined, 'focus ' + cls);
        section.append(el('p', title, 'eyebrow'));
        for (const item of items.slice(0, 3)) section.append(nodeButton(item, 'focus-node'));
        if (items.length > 3) {
            const more = el('details', undefined, 'priority-more');
            more.append(el('summary', `其余 ${items.length - 3} 项`));
            for (const item of items.slice(3)) more.append(nodeButton(item, 'focus-node'));
            section.append(more);
        }
        content.append(section);
    }
    function renderMainline(data) {
        content.replaceChildren();
        renderGroup('当前目标', data.goals, 'goals');
        const heading = el('div', undefined, 'section-head');
        heading.append(el('h2', '对话主线'), el('span', '点击阶段，按需回溯', 'muted'));
        content.append(heading);
        const line = el('div', undefined, 'mainline');
        for (const phase of data.phases) {
            const row = el('div', undefined, 'stage-row');
            const b = button('', () => openRecords(phase.id), 'phase');
            b.append(el('span', phase.title));
            b.dataset.phase = phase.id;
            b.setAttribute('aria-expanded', 'false');
            if (phase.id === data.focus?.phase_id) {
                b.classList.add('current');
                b.append(el('small', '当前', 'focus-tag'));
            }
            row.append(b);
            const branches = data.branches.filter(x => x.phase_id === phase.id);
            if (branches.length) {
                const disclosure = el('details', undefined, 'branch-list');
                disclosure.append(el('summary', `${branches.length} 条支线`));
                for (const branch of branches) {
                    const bb = button(branch.title, () => openRecords(phase.id, branch.id), 'branch');
                    bb.dataset.branch = branch.id;
                    disclosure.append(bb);
                }
                row.append(disclosure);
            }
            line.append(row);
        }
        if (!data.phases.length) line.append(el('p', '主线正在整理中', 'muted'));
        content.append(line);
        renderGroup('待解决', data.open_questions);
        renderGroup('下一步', data.next_steps, 'next-steps');
        const records = el('section', undefined, 'records');
        records.dataset.records = '';
        records.hidden = true;
        content.append(records);
    }
    async function openRecords(phase, branch = null) {
        state.phase = phase; state.branch = branch; state.offset = 0;
        state.recordsOpen = true;
        await loadRecords();
    }
    async function loadRecords() {
        const box = content.querySelector('[data-records]');
        if (!box || !state.chat || !state.recordsOpen) return;
        const token = ++state.epoch;
        box.hidden = false;
        box.setAttribute('aria-busy', 'true');
        box.replaceChildren(el('p', '正在读取阶段明细…', 'muted'));
        for (const b of content.querySelectorAll('[data-phase]')) {
            const selected = b.dataset.phase === state.phase;
            b.classList.toggle('selected', selected);
            b.setAttribute('aria-expanded', String(selected));
        }
        try {
            const data = await call({ view: 'narrative', chat_id: state.chat, version: state.version,
                phase_id: state.phase, branch_id: state.branch, offset: state.offset, limit: 20 });
            if (token !== state.epoch) return;
            if (data.error) throw Error('records_unavailable');
            box.replaceChildren();
            const heading = el('div', undefined, 'section-head');
            heading.append(el('h2', state.branch
                ? state.data.branches.find(b => b.id === state.branch)?.title || '支线明细' : '阶段明细'));
            heading.append(button('收起', () => {
                state.recordsOpen = false; state.epoch++; box.hidden = true;
                for (const b of content.querySelectorAll('[data-phase]')) {
                    b.classList.remove('selected'); b.setAttribute('aria-expanded', 'false');
                }
            }, 'more'));
            box.append(heading);
            for (const item of data.results) box.append(nodeButton(item));
            if (!data.results.length) box.append(el('p', '此处还没有节点', 'muted'));
            if (state.offset || data.retrieval?.has_more) {
                const row = el('div', undefined, 'pagination');
                const prev = button('上一页', () => { state.offset = Math.max(0, state.offset - 20); loadRecords(); });
                prev.disabled = state.offset === 0;
                const next = button('下一页', () => { state.offset = data.retrieval.next_offset; loadRecords(); });
                next.disabled = !data.retrieval?.has_more;
                row.append(prev, el('span', `第 ${Math.floor(state.offset / 20) + 1} 页`, 'muted'), next);
                box.append(row);
            }
        } catch {
            if (token === state.epoch) {
                box.replaceChildren(el('p', '暂时无法读取阶段明细。', 'muted'));
                box.append(button('重试', loadRecords, 'more'));
            }
        } finally {
            if (token === state.epoch) box.removeAttribute('aria-busy');
        }
    }
    async function showNode(objectId) {
        const token = ++state.drawerEpoch, chat = state.chat;
        drawer.hidden = false;
        detail.replaceChildren(el('p', '正在定位来源…', 'muted'));
        try {
            const data = await call({ view: 'node', object_id: objectId });
            if (token !== state.drawerEpoch)
                return;
            const node = data.results?.[0]?.item;
            if (!node)
                throw Error('not_found');
            detail.replaceChildren(el('p', '节点与来源', 'eyebrow'), el('h2', node.title || '记录'));
            if (node.content && node.content !== node.title)
                detail.append(el('p', node.content, 'node-content'));
            const refs = node.evidence || [];
            if (!refs.length)
                detail.append(el('p', '此节点暂未关联可读取来源。', 'muted'));
            for (const [index, ref] of refs.slice(0, 12).entries()) {
                const section = el('details', undefined, 'source-section');
                section.open = index === 0;
                section.append(el('summary', `来源 ${index + 1}`));
                const box = el('blockquote');
                section.append(box);
                detail.append(section);
                let loaded = false, loading = false, next = ref.start;
                async function readPart() {
                    if (loading || token !== state.drawerEpoch)
                        return;
                    loading = true;
                    box.replaceChildren(el('p', '正在读取原文…', 'muted'));
                    try {
                        const source = await call({ view: 'source', chat_id: chat, event_id: ref.event_id, snapshot_id: ref.snapshot_id, start: next, end: Math.min(ref.end, next + 4000) });
                        if (token !== state.drawerEpoch)
                            return;
                        if (source.error || typeof source.text !== 'string')
                            throw Error('source_unavailable');
                        loaded = true;
                        box.replaceChildren(el('span', source.text));
                        box.append(el('small', `${source.source_current ? '已定位原始记录' : '历史快照 · 当前来源可能已变化'} · ${source.char_start}–${source.char_end}`, 'muted'));
                        if (source.next_start != null && source.next_start < ref.end && source.next_start > next) {
                            next = source.next_start;
                            box.append(button('继续读取', readPart, 'more'));
                        }
                    }
                    catch {
                        if (token === state.drawerEpoch) {
                            box.replaceChildren(el('p', '这处原文暂时无法读取。'));
                            box.append(button('重试', readPart, 'more'));
                        }
                    }
                    finally {
                        loading = false;
                    }
                }
                section.ontoggle = () => { if (section.open && !loaded)
                    readPart(); };
                if (section.open)
                    readPart();
            }
            if (refs.length > 12)
                detail.append(el('p', `另有 ${refs.length - 12} 处来源，可通过 Seywo 来源工具继续定位。`, 'muted'));
        }
        catch {
            if (token === state.drawerEpoch)
                detail.replaceChildren(el('p', '暂时无法读取这个节点的来源。'));
        }
    }
    state.drawerEpoch = 0;
    root.querySelector('[data-close]').onclick = () => { drawer.hidden = true; state.drawerEpoch++; };
    async function loadView() {
        if (!state.chat) return;
        const token = ++state.epoch;
        clearNotice();
        content.setAttribute('aria-busy', 'true');
        try {
            const data = await call({ view: 'mainline', chat_id: state.chat });
            if (token !== state.epoch) return;
            if (data.error) throw Error('mainline_unavailable');
            state.version = data.version;
            state.data = data;
            if (!data.version) {
                empty('这段对话还没有发布主线', '原始对话可通过 Seywo 搜索读取；主线将在整理后显示。');
                return;
            }
            renderMainline(data);
        } catch {
            if (token === state.epoch) {
                empty('主线暂时不可用'); error(loadView);
            }
        } finally {
            if (token === state.epoch) content.removeAttribute('aria-busy');
        }
    }
    function populateChats() {
        const select = root.querySelector('[data-chat]');
        select.replaceChildren(el('option', '选择一个 Chat'));
        select.firstChild.value = '';
        for (const c of state.chats) {
            const option = el('option', `${c.title} · ${c.chat_id.slice(0, 8)}`);
            option.value = c.chat_id;
            select.append(option);
        }
        // An explicit imported Chat may be outside the currently loaded picker page.
        if (state.chat && !state.chats.some(c => c.chat_id === state.chat)) {
            const option = el('option', `指定对话 · ${state.chat.slice(0, 8)}`);
            option.value = state.chat;
            select.append(option);
        }
        select.value = state.chat;
        root.querySelector('[data-more-chats]').hidden = !state.chatPage?.has_more;
    }
    async function loadMoreChats() {
        const token = state.bootstrapEpoch, b = root.querySelector('[data-more-chats]');
        b.disabled = true;
        clearNotice();
        try {
            const data = await call({ view: 'chats', offset: state.chatPage.next_offset, limit: 20 });
            if (token !== state.bootstrapEpoch)
                return;
            if (data.error)
                throw Error('chats_unavailable');
            const ids = new Set(state.chats.map(c => c.chat_id));
            state.chats.push(...data.results.filter(c => !ids.has(c.chat_id)));
            state.chatPage = data.retrieval;
            populateChats();
        }
        catch {
            if (token === state.bootstrapEpoch)
                error(loadMoreChats);
        }
        finally {
            if (token === state.bootstrapEpoch)
                b.disabled = false;
        }
    }
    async function selectChat(chat) {
        state.chat = chat;
        state.version = null; state.phase = null; state.branch = null; state.offset = 0;
        state.recordsOpen = false; state.data = null;
        state.drawerEpoch++; drawer.hidden = true;
        state.chatEpoch++; state.epoch++;
        content.removeAttribute('aria-busy'); clearNotice(); populateChats();
        if (!chat) { empty('选择一个 Chat', '选择对话，查看主线与当前重点。'); return; }
        empty('正在读取主线…');
        await loadView();
    }
    root.querySelector('[data-chat]').onchange = e => selectChat(e.target.value);
    root.querySelector('[data-more-chats]').onclick = loadMoreChats;
    async function bootstrap() {
        root.querySelector('[data-more-chats]').disabled = false;
        const token = ++state.bootstrapEpoch;
        state.epoch++;
        state.chatEpoch++;
        state.drawerEpoch++;
        drawer.hidden = true;
        clearNotice();
        try {
            const data = await call({ view: 'chats', offset: 0, limit: 20 });
            if (token !== state.bootstrapEpoch)
                return;
            if (data.error)
                throw Error('chats_unavailable');
            if (!state.chat && data.default_chat_id) state.chat = data.default_chat_id;
            state.project = data.project_root;
            state.chats = data.results;
            state.chatPage = data.retrieval;
            populateChats();
            root.querySelector('[data-project]').textContent = state.project.split('/').filter(Boolean).at(-1) || '项目记忆';
            if (!state.chat && data.current_chat_id) {
                empty('当前对话尚未收录', '整理当前对话后会在这里显示。也可以从上方查看项目中的其他对话。');
                return;
            }
            await selectChat(state.chat);
        }
        catch {
            if (token === state.bootstrapEpoch)
                error(bootstrap);
        }
    }
    function initializationError(info) {
        root.querySelector('[data-project]').textContent=info.project_root?.split('/').filter(Boolean).at(-1)||'项目记忆';
        empty('项目记忆暂时无法启用', info.error || '无法初始化当前项目，请重试。');
        const retry=button('重试',()=>reopen());retry.dataset.retry='';content.append(retry);
    }
    async function reopen(initialize=false) {
        const token=++state.bootstrapEpoch;
        clearNotice();empty('正在初始化项目记忆');
        try {
            if(initialize)await actions.initializeProject();
            if(token!==state.bootstrapEpoch)return;
            const info=await actions.openCurrent();
            if(token!==state.bootstrapEpoch)return;
            await open(info);
        } catch (error) {
            if(token===state.bootstrapEpoch)initializationError({error:error.initializationCause || '无法连接到项目初始化服务，请重试。'});
        }
    }
    function projectPicker(){
        empty('暂时无法定位当前对话','重新读取当前对话的项目信息后，会自动显示主线。');
        if(actions.openCurrent)content.append(button('重新读取当前对话',async()=>{
            clearNotice();try{await open(await actions.openCurrent());}catch{error(projectPicker);}
        }));
        const fallback=el('details',undefined,'project-fallback');
        fallback.append(el('summary','手动选择项目'));content.append(fallback);
        const form=el('form',undefined,'project-picker'),label=el('label','项目路径');
        const input=el('input');input.type='text';input.required=true;input.placeholder='/absolute/project/path';input.dataset.projectPath='';input.id='seywo-project-path';label.htmlFor=input.id;
        const submit=button('打开项目');submit.type='submit';submit.dataset.bindProject='';
        form.append(label,input,submit);fallback.append(form);
        form.onsubmit=async event=>{
            event.preventDefault();if(!input.value.trim())return;
            submit.disabled=true;clearNotice();
            const token=++state.bootstrapEpoch;
            try{
                const result=await actions.selectProject(input.value.trim());
                if(token!==state.bootstrapEpoch)return;
                if(actions.openCurrent)await reopen();else if(result.initialized)await bootstrap();else await reopen(true);
            }catch{if(token===state.bootstrapEpoch)error(()=>{submit.disabled=false;input.focus();});}
            finally{submit.disabled=false;}
        };
    }
    async function open(info={}){
        state.epoch++;state.chatEpoch++;state.bootstrapEpoch++;state.drawerEpoch++;
        state.chat=info.chat_id||'';state.version=null;drawer.hidden=true;
        content.removeAttribute('aria-busy');clearNotice();
        if(info.state==='project_required'){projectPicker();return;}
        if(info.state==='initialization_failed'){initializationError(info);return;}
        if(info.state==='initialization_required'){await reopen(true);return;}
        const token=state.bootstrapEpoch+1;
        await bootstrap();
        if(info.initialized && token===state.bootstrapEpoch){notice.replaceChildren(el('p','项目记忆已启用'));}
    }
    root.querySelector('[data-refresh]').onclick = actions.openCurrent ? ()=>reopen() : bootstrap;
    settings.ontoggle = () => { if (settings.open) void loadSettings(); };
    if (!actions.modelStatus) settings.hidden = true;
    await open(initial);
    return { refresh: bootstrap, selectChat, open, state };
}
