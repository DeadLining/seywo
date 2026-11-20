/** Project panel controller. Transport is supplied by the MCP Apps entrypoint. */
export async function mountPanel(root, call, initial = {}, actions = {}) {
    const doc = root.ownerDocument;
    const state = { chat: initial.chat_id || '', chats: [], version: null, phase: null, branch: null, offset: 0, epoch: 0, chatEpoch: 0, bootstrapEpoch: 0, data: null, project: '', chatPage: null, recordsOpen: false };
    const labels = { open: '进行中', resolved: '已记录结果', active: '进行中', waiting: '等待中', abandoned: '已放弃', added: '新增', updated: '更新', merged: '合并', hidden: '隐藏', removed: '移除' };
    const el = (tag, text, cls) => { const n = doc.createElement(tag); if (text !== undefined)
        n.textContent = text; if (cls)
        n.className = cls; return n; };
    const button = (text, fn, cls = '') => { const b = el('button', text, cls); b.type = 'button'; b.onclick = fn; return b; };
    root.innerHTML = '<header class="header"><div class="brand"><span class="mark" aria-label="Seywo 轨迹标记"><svg viewBox="0 0 48 48" aria-hidden="true"><circle class="brand-halo" cx="34.5" cy="13.5" r="5.5"/><path class="brand-glyph" d="M36.1 13.8c-2.6-2.4-6.5-3.7-10.8-3.7-6.8 0-11.5 3.1-11.5 7.6 0 4.8 4.7 6.6 10.7 7.9 5.8 1.3 8.5 2.4 8.5 5.1 0 2.8-3.1 4.7-7.8 4.7-4.3 0-8.2-1.5-10.9-4.1l-3.1 3.8c3.4 3.2 8.3 5 14.1 5 8.1 0 13.4-3.9 13.4-9.8 0-5.1-3.9-7.5-10.9-9-5.8-1.3-8.3-2.4-8.3-4.5 0-1.8 2.5-3.1 6.3-3.1 3.7 0 7 1.1 9.7 3.1z"/><circle class="brand-node" cx="36.8" cy="34.2" r="3.2"/></svg></span><div><h1>Seywo</h1><p data-project>项目记忆</p></div></div><div class="header-actions"><button data-refresh aria-label="刷新记录" title="刷新记录"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 7a7 7 0 0 1 11.5-1L20 9M4 15l2.4 3a7 7 0 0 0 11.5-1"/></svg></button><details data-chat-switcher class="chat-switcher" name="seywo-toolbar"><summary title="查看其他对话" aria-label="查看其他对话"><span class="toolbar-full">查看其他对话</span><span class="toolbar-short" aria-hidden="true">对话</span></summary><div class="toolbar-popover"><div class="selector"><label for="chat">对话</label><select id="chat" data-chat><option value="">选择一个 Chat</option></select></div><button data-more-chats class="more" hidden>更多对话</button></div></details><details data-settings name="seywo-toolbar"><summary title="模型设置" aria-label="模型设置"><span class="toolbar-full">模型设置</span><span class="toolbar-short" aria-hidden="true">模型</span></summary><div data-settings-body class="toolbar-popover"></div></details></div></header><div data-notice role="status"></div><section class="workspace"><section data-maintenance class="maintenance" role="status" aria-live="polite" hidden></section><main data-content></main></section><aside data-drawer hidden aria-label="节点来源"><button data-close aria-label="关闭来源">×</button><div data-detail></div></aside><footer>本地记录 · 按需读取原文</footer>';
    const content = root.querySelector('[data-content]'), notice = root.querySelector('[data-notice]');
    const chatSwitcher = root.querySelector('[data-chat-switcher]');
    const drawer = root.querySelector('[data-drawer]'), detail = root.querySelector('[data-detail]');
    const settings = root.querySelector('[data-settings]'), settingsBody = root.querySelector('[data-settings-body]');
    let settingsLoading = false;
    const clearNotice = () => notice.replaceChildren();
    function error(retry) { notice.replaceChildren(el('p', '暂时无法读取这部分记录。你可以重试。')); const b = button('重试', retry); b.dataset.retry = ''; notice.append(b); }
    // Maintenance has its own lifecycle: a failed update must never replace a published view.
    const maintenanceBox = root.querySelector('[data-maintenance]');
    const maintenanceLabels = {idle: '暂时无需整理', queued: '等待整理', running: '正在整理', done: '整理完成', partial: '整理部分完成', failed: '整理失败', blocked: '整理暂受阻', interrupted: '整理已中断', disabled: '自动整理已停用'};
    let maintenanceTarget = '', maintenanceStatus = null, maintenanceGeneration = 0, maintenanceRead = 0;
    let maintenanceTimer, maintenanceUntil = 0, maintenanceSubmitting = false, maintenanceProblem = '';
    let maintenanceCompletionReloaded = false;
    const maintenanceActive = () => ['queued', 'running'].includes(maintenanceStatus?.state) || Boolean(maintenanceStatus?.waiting_for_turn);
    const storageFailed = () => maintenanceStatus?.storage?.state === 'failed';
    const maintenanceVisible = () => storageFailed() || Boolean(maintenanceProblem) || ['queued', 'running', 'failed', 'partial', 'blocked', 'interrupted'].includes(maintenanceStatus?.state);
    function clearMaintenanceTimer() {
        clearTimeout(maintenanceTimer); maintenanceTimer = undefined;
    }
    function resetMaintenance() {
        clearMaintenanceTimer(); maintenanceGeneration++; maintenanceRead++;
        maintenanceTarget = ''; maintenanceStatus = null; maintenanceSubmitting = false;
        maintenanceProblem = ''; maintenanceUntil = 0; maintenanceCompletionReloaded = false;
        maintenanceBox.hidden = true; maintenanceBox.replaceChildren();
        maintenanceBox.removeAttribute('aria-busy');
    }
    function renderEmptyMainline() {
        const status = maintenanceTarget === state.chat ? maintenanceStatus : null;
        if (['failed', 'partial', 'blocked', 'interrupted'].includes(status?.state)) {
            empty('主线整理失败', '这段对话还没有发布主线。失败原因和重试入口见上方整理状态，原始对话仍可搜索读取。');
        } else {
            empty('这段对话还没有发布主线', '原始对话可通过 Seywo 搜索读取；主线将在整理后显示。');
        }
    }
    function renderMaintenance() {
        if (!maintenanceVisible()) {
            maintenanceBox.hidden = true;
            maintenanceBox.replaceChildren();
            maintenanceBox.removeAttribute('data-state');
            maintenanceBox.removeAttribute('aria-busy');
            return;
        }
        if (state.data && !state.data.version) renderEmptyMainline();
        maintenanceBox.hidden = false; maintenanceBox.replaceChildren();
        maintenanceBox.dataset.state = maintenanceStatus?.state || 'unavailable';
        maintenanceBox.setAttribute('aria-busy', String(maintenanceSubmitting));
        const statusState = maintenanceStatus?.state || 'unavailable';
        maintenanceBox.append(el('span', maintenanceStatus?.waiting_for_turn ? '…' : ['failed', 'partial', 'blocked', 'interrupted'].includes(statusState) ? '!' : '…', 'maintenance-mark'));
        const copy = el('div', undefined, 'maintenance-copy');
        if (!state.chat) copy.append(el('small', '当前对话', 'muted'));
        const onlyStorage = storageFailed() && !['queued', 'running', 'failed', 'partial', 'blocked', 'interrupted'].includes(statusState);
        copy.append(el('span', onlyStorage ? '存储回收暂未完成' : maintenanceStatus?.title || maintenanceLabels[maintenanceStatus?.state] || '整理状态暂不可用'));
        if (storageFailed()) copy.append(el('small', '已发布主线正常可用；存储回收会在下次整理完成后重试。'));
        if (maintenanceStatus?.message) copy.append(el('small', maintenanceStatus.message));
        if (maintenanceStatus?.stale_view || ['failed', 'partial', 'blocked', 'interrupted'].includes(maintenanceStatus?.state))
            copy.append(el('small', '整理成功后会更新主线。'));
        if (maintenanceProblem) copy.append(el('small', maintenanceProblem));
        maintenanceBox.append(copy);
        if (maintenanceStatus?.retryable && actions.maintenanceRetry) {
            const retry = button(maintenanceSubmitting ? '正在提交…' : '重试整理', retryMaintenance, 'more');
            retry.dataset.maintenanceRetry = ''; retry.disabled = maintenanceSubmitting;
            maintenanceBox.append(retry);
        }
        if (maintenanceProblem || (maintenanceActive() && Date.now() >= maintenanceUntil)) {
            const refresh = button('刷新状态', () => {
                maintenanceUntil = Date.now() + 300000;
                return readMaintenance();
            }, 'more');
            refresh.dataset.maintenanceRefresh = ''; refresh.disabled = maintenanceSubmitting;
            maintenanceBox.append(refresh);
        }
    }
    function scheduleMaintenance() {
        clearMaintenanceTimer();
        if (!maintenanceTarget || doc.hidden || maintenanceSubmitting || (!maintenanceActive() && maintenanceStatus)) return;
        if (Date.now() >= maintenanceUntil) { renderMaintenance(); return; }
        const generation = maintenanceGeneration;
        maintenanceTimer = setTimeout(() => {
            maintenanceTimer = undefined;
            if (generation !== maintenanceGeneration || doc.hidden) return;
            if (Date.now() >= maintenanceUntil) { renderMaintenance(); return; }
            void readMaintenance();
        }, 4000);
        // Node's tests should not wait on a background UI poll.
        maintenanceTimer.unref?.();
    }
    async function acceptMaintenance(data, generation) {
        if (!data || data.chat_id !== maintenanceTarget || !Object.hasOwn(maintenanceLabels, data.state))
            throw Error('maintenance_unavailable');
        const completed = maintenanceActive() && data.state === 'done';
        maintenanceStatus = data; maintenanceProblem = '';
        renderMaintenance(); scheduleMaintenance();
        if (completed && generation === maintenanceGeneration) {
            maintenanceCompletionReloaded = true;
            if (state.chat) await loadView(true);
            else await bootstrap(); // The exact host Chat may now have been imported.
        }
    }
    async function readMaintenance() {
        if (!maintenanceTarget || maintenanceSubmitting || doc.hidden) return;
        clearMaintenanceTimer();
        const generation = maintenanceGeneration, request = ++maintenanceRead, chat = maintenanceTarget;
        try {
            const data = await call({view: 'maintenance', chat_id: chat});
            if (generation !== maintenanceGeneration || request !== maintenanceRead) return;
            await acceptMaintenance(data, generation);
        } catch {
            if (generation !== maintenanceGeneration || request !== maintenanceRead) return;
            maintenanceProblem = '暂时无法读取整理状态。可以刷新状态重试。';
            renderMaintenance();
        }
    }
    async function retryMaintenance() {
        if (!maintenanceTarget || maintenanceSubmitting || !actions.maintenanceRetry) return;
        clearMaintenanceTimer(); maintenanceRead++;
        const generation = maintenanceGeneration, chat = maintenanceTarget;
        maintenanceSubmitting = true; maintenanceProblem = ''; renderMaintenance();
        try {
            const result = await actions.maintenanceRetry(chat);
            if (generation !== maintenanceGeneration) return;
            maintenanceSubmitting = false; maintenanceUntil = Date.now() + 300000;
            await acceptMaintenance(result?.maintenance, generation);
        } catch {
            if (generation !== maintenanceGeneration) return;
            maintenanceSubmitting = false;
            maintenanceProblem = '未能提交重试，请稍后再试。'; renderMaintenance();
        }
    }
    async function startMaintenance(chat) {
        resetMaintenance(); maintenanceTarget = chat;
        maintenanceUntil = Date.now() + 300000;
        await readMaintenance();
    }
    doc.addEventListener('visibilitychange', () => {
        clearMaintenanceTimer(); maintenanceRead++;
        if (!doc.hidden) scheduleMaintenance();
    });
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
        const nativeInfo = el('p', '通过 Codex 应用服务整理对话，沿用本机提供方配置。模型和思考强度可单独设置。', 'muted');
        const nativeFields = el('div', undefined, 'model-settings-fields');
        const nativeModel = el('select'); nativeModel.dataset.codexModel = '';
        const inherit = el('option', '跟随 Codex 默认模型'); inherit.value = 'codex-default'; nativeModel.append(inherit);
        const savedModel = data.backend === 'codex' && data.model ? data.model : 'codex-default';
        if (savedModel !== 'codex-default') {
            const saved = el('option', savedModel + '（已保存）'); saved.value = savedModel; nativeModel.append(saved);
        }
        nativeModel.value = savedModel;
        const nativeLabel = el('label', '模型'); nativeLabel.append(nativeModel);
        const nativeReasoning = el('select'); nativeReasoning.dataset.codexReasoning = '';
        const reasoningLabel = el('label', '思考强度'); reasoningLabel.append(nativeReasoning);
        const reasoningInfo = el('p', '', 'muted'); reasoningInfo.dataset.reasoningStatus = '';
        const effortLabels = {auto:'自动（优先低消耗）', none:'关闭 · none', minimal:'最低 · minimal',
            low:'低 · low', medium:'中 · medium', high:'高 · high', xhigh:'更高 · xhigh', max:'最高 · max', ultra:'极高 · ultra'};
        const savedEffort = data.backend === 'codex' ? data.reasoning_effort || 'auto' : 'auto';
        let modelRows = [], catalogLoading = false, catalogLoaded = false;
        let defaultModel = savedModel === 'codex-default' ? data.effective_model : null;
        function updateEfforts(resetUnsupported = false) {
            let selected = nativeReasoning.value || savedEffort;
            const targetModel = nativeModel.value === 'codex-default' ? defaultModel : nativeModel.value;
            const target = targetModel ? modelRows.find(row => row.model === targetModel)
                : modelRows.find(row => row.is_default);
            const supported = Object.keys(effortLabels).filter(value => value !== 'auto' && target?.reasoning_efforts?.includes(value));
            let reset = false;
            if (resetUnsupported && selected !== 'auto' && !supported.includes(selected)) {
                selected = 'auto'; reset = true;
            }
            nativeReasoning.replaceChildren();
            for (const value of ['auto', ...supported]) {
                const option = el('option', effortLabels[value]); option.value = value; nativeReasoning.append(option);
            }
            if (selected !== 'auto' && !supported.includes(selected)) {
                const option = el('option', `${effortLabels[selected] || selected}（待确认支持）`);
                option.value = selected; nativeReasoning.append(option);
            }
            nativeReasoning.value = selected;
            reasoningInfo.textContent = reset ? '新模型未声明支持原档位，已切换为自动（优先低消耗）。'
                : selected === 'auto' ? '自动优先使用当前模型支持的低强度档位。'
                : supported.includes(selected) ? '测试和后台整理均使用所选思考强度。'
                : '当前列表未确认支持已保存的档位，请重新读取模型或选择自动。';
        }
        nativeModel.onchange = () => updateEfforts(true);
        nativeReasoning.onchange = () => updateEfforts();
        updateEfforts();
        const catalogStatus = el('p', '', 'muted'); catalogStatus.dataset.modelListStatus = '';
        const refreshModels = button('重新读取模型', () => void loadModels());
        refreshModels.dataset.modelListRefresh = '';
        refreshModels.hidden = !actions.modelList;
        nativeFields.append(nativeLabel, reasoningLabel, reasoningInfo, catalogStatus, refreshModels);
        async function loadModels() {
            if (!actions.modelList || catalogLoading) return;
            catalogLoading = true; refreshModels.disabled = true;
            catalogStatus.textContent = '正在读取本机 Codex 模型列表…';
            try {
                const result = await actions.modelList();
                if (result.error) { catalogStatus.textContent = result.message || '模型列表暂不可用，可继续测试默认模型。'; return; }
                if (Object.hasOwn(result, 'default_model')) defaultModel = result.default_model;
                modelRows = Array.isArray(result.models) ? result.models.filter(row => row && typeof row.model === 'string') : [];
                const selected = nativeModel.value;
                nativeModel.replaceChildren(inherit);
                const seen = new Set(['codex-default']);
                for (const row of result.models || []) {
                    if (!row || typeof row.model !== 'string' || !row.model || seen.has(row.model)) continue;
                    seen.add(row.model);
                    const option = el('option', row.label && row.label !== row.model ? `${row.label} · ${row.model}` : row.model);
                    option.value = row.model; nativeModel.append(option);
                }
                if (!seen.has(selected)) {
                    const saved = el('option', selected + '（未在当前列表中）'); saved.value = selected; nativeModel.append(saved);
                }
                nativeModel.value = selected;
                catalogLoaded = true; updateEfforts();
                catalogStatus.textContent = seen.size > 1 ? '列表来自本机 Codex，实际可用性以测试结果为准。' : 'Codex 未提供可选模型，仍可使用默认配置。';
            } catch { catalogStatus.textContent = '读取模型列表失败，请重试；仍可测试默认配置。'; }
            finally { catalogLoading = false; refreshModels.disabled = false; }
        }
        const showFields = () => {
            const native = source.value === 'codex';
            fields.hidden = native; nativeInfo.hidden = nativeFields.hidden = !native;
            if (native && !catalogLoaded) void loadModels();
            endpoint.required = model.required = !native;
        };
        source.onchange = () => { showFields(); login.hidden = accountRefresh.hidden = true; }; showFields();
        const actionRow = el('div', undefined, 'model-settings-actions');
        const test = button('测试模型', undefined); test.dataset.modelTest = '';
        const save = button('保存配置', undefined); save.dataset.modelSave = ''; save.type = 'submit';
        const login = button('连接 Codex 账号', undefined); login.dataset.codexLogin = ''; login.hidden = true;
        const accountRefresh = button('检查授权', undefined); accountRefresh.dataset.codexAccountRefresh = ''; accountRefresh.hidden = true;
        actionRow.append(test, save, login, accountRefresh); const scope = el('p', '适用于本机所有项目和对话的 Seywo 整理任务。', 'muted');
        scope.dataset.modelScope = '';
        form.append(scope, sourceLabel, nativeInfo, nativeFields, fields, actionRow);
        const status = el('p', data.backend === 'codex'
            ? (data.configured ? '正在使用 Codex。' : '请更新并重新打开 Codex App。')
            : (data.configured ? (data.credential_configured ? '已配置，可供 Compact 使用。' : '还没有 API KEY。') : '尚未配置模型。'), 'muted');
        status.dataset.modelStatus = ''; settingsBody.replaceChildren(status, form);
        const accountAction = async action => {
            login.disabled = accountRefresh.disabled = true;
            try {
                const result = await actions.codexAccount?.(action);
                if (result?.state === 'login_pending') {
                    await actions.openLink?.(result.auth_url);
                    accountRefresh.hidden = false;
                    status.textContent = '请在浏览器中完成 Codex 授权，然后点击“检查授权”。';
                } else if (result?.state === 'ready') {
                    login.hidden = accountRefresh.hidden = true;
                    catalogLoaded = false;
                    await loadModels();
                    status.textContent = 'Codex 账号已连接，可以测试模型。';
                } else status.textContent = result?.message || '尚未完成授权，请在浏览器中完成后重新检查。';
            } catch { status.textContent = '无法连接账号授权服务，请稍后重试。'; }
            finally { login.disabled = accountRefresh.disabled = false; }
        };
        login.onclick = () => accountAction('login');
        accountRefresh.onclick = () => accountAction('status');
        const run = async kind => {
            test.disabled = save.disabled = source.disabled = nativeModel.disabled = nativeReasoning.disabled = true;
            try {
                const native = source.value === 'codex';
                const args = { backend: source.value, endpoint: native ? '' : endpoint.value.trim(), model: native ? nativeModel.value : model.value.trim(), api_key: native ? '' : key.value, ...(native ? {reasoning_effort: nativeReasoning.value} : {}) };
                status.textContent = kind === 'test' ? '正在测试模型…' : '正在保存…';
                const result = kind === 'test' ? await actions.modelTest?.(args) : await actions.modelSave?.(args);
                if (!result) throw Error('unavailable');
                if (kind === 'test') {
                    login.hidden = accountRefresh.hidden = !(native && result.error === 'codex_auth_required' && actions.codexAccount);
                    const selected = result.model_info?.selected_model;
                    const effort = result.model_info?.reasoning_effort;
                    status.textContent = result.ok ? `连接成功${selected ? ' · ' + selected : ''}${effort ? ' · 思考强度：' + (effortLabels[effort] || effort) : ''}，本次仅调用 1 次模型。` : (result.message || '连接失败，请检查模型配置或 Codex 登录状态。');
                }
                else { status.textContent = '已保存，本机所有项目后续的 Seywo 整理将使用该配置。'; key.value = ''; key.placeholder = '已保存，留空沿用'; }
            } catch { status.textContent = '模型设置暂时不可用，请稍后重试。'; }
            finally { test.disabled = save.disabled = source.disabled = nativeModel.disabled = nativeReasoning.disabled = false; }
        };
        test.onclick = () => void run('test');
        form.onsubmit = event => { event.preventDefault(); void run('save'); };
    }
    async function loadSettings() {
        if (!actions.modelStatus || settingsLoading) return;
        settingsLoading = true;
        settingsBody.replaceChildren(el('p', '正在读取模型设置…', 'muted'));
        try { renderSettingsStatus(await actions.modelStatus()); }
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
    function icon(name) {
        const icons = {
            goal: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r="1.4"/></svg>',
            questions: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m8 5 8 0 3 3v10a1 1 0 0 1-1 1H8a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1Z"/><path d="M5 8V5a1 1 0 0 1 1-1h8M10 10h6M10 14h6M10 18h4"/></svg>',
            next: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 19V5l14 7-14 7Z"/><path d="M19 12h-5"/></svg>'
        };
        const span = el('span', undefined, 'section-icon');
        span.innerHTML = icons[name] || icons.goal;
        return span;
    }
    function renderGroup(title, items, cls = '', kind = 'goal') {
        const list = Array.isArray(items) ? items : [];
        const section = el('section', undefined, 'focus ' + cls);
        section.dataset.group = kind;
        const heading = el('div', undefined, 'group-heading');
        const label = el('div', undefined, 'group-label');
        label.append(icon(kind), el('h2', title));
        heading.append(label);
        if (kind === 'goal') heading.append(el('span', 'Focus', 'group-meta'));
        if (kind === 'questions') heading.append(el('span', `${list.length} 项`, 'group-meta'));
        if (kind === 'next') heading.append(el('span', 'Next', 'group-meta'));
        section.append(heading);
        if (!list.length) {
            const emptyText = kind === 'goal' ? '当前阶段暂无未完成目标'
                : kind === 'questions' ? '暂无待解决事项' : '暂无下一步';
            section.append(el('p', emptyText, 'focus-empty'));
            return section;
        }
        for (const item of list.slice(0, 3)) section.append(nodeButton(item, 'focus-node'));
        if (kind === 'goal') {
            const source = button('查看目标来源 ↗', () => showNode(list[0].object_id), 'source-link');
            source.dataset.source = '';
            section.append(source);
        }
        if (list.length > 3) {
            const more = el('details', undefined, 'priority-more');
            more.append(el('summary', `其余 ${list.length - 3} 项`));
            for (const item of list.slice(3)) more.append(nodeButton(item, 'focus-node'));
            section.append(more);
        }
        return section;
    }
    function renderMainline(data) {
        content.replaceChildren();
        const mainlineColumn = el('section', undefined, 'mainline-column');
        const heading = el('div', undefined, 'section-head mainline-head');
        const title = el('div', undefined, 'mainline-title');
        title.append(el('span', 'CONVERSATION SPINE', 'eyebrow'), el('h2', '对话主线'));
        const phaseCount = data.phases?.length || 0;
        const branchCount = data.branches?.length || 0;
        heading.append(title, el('span', `共 ${phaseCount} 个阶段 · ${branchCount} 条支线`, 'muted'));
        mainlineColumn.append(heading);
        const line = el('div', undefined, 'mainline');
        for (const phase of data.phases) {
            const row = el('div', undefined, 'stage-row');
            const b = button('', () => openRecords(phase.id), 'phase');
            b.append(el('span', phase.title));
            b.dataset.phase = phase.id;
            b.setAttribute('aria-expanded', 'false');
            if (phase.id === data.focus?.phase_id) {
                b.classList.add('current');
                b.append(el('small', '当前阶段', 'focus-tag'));
            }
            row.append(b);
            const branches = data.branches.filter(x => x.phase_id === phase.id);
            if (branches.length) {
                const disclosure = el('details', undefined, 'branch-list');
                if (phase.id === data.focus?.phase_id) disclosure.open = true;
                disclosure.append(el('summary', `${disclosure.open ? '收起' : '展开'} ${branches.length} 条支线`));
                for (const branch of branches) {
                    const resolved = ['resolved', 'done', 'completed'].includes(branch.status || branch.state);
                    const bb = button('', () => openRecords(phase.id, branch.id), 'branch' + (resolved ? ' resolved' : ''));
                    bb.dataset.branch = branch.id;
                    if (resolved) bb.append(el('span', '✓', 'branch-check'));
                    bb.append(el('span', branch.title));
                    disclosure.append(bb);
                }
                disclosure.ontoggle = () => { disclosure.querySelector('summary').textContent = `${disclosure.open ? '收起' : '展开'} ${branches.length} 条支线`; };
                row.append(disclosure);
            }
            line.append(row);
        }
        if (!data.phases.length) line.append(el('p', '主线正在整理中', 'muted'));
        mainlineColumn.append(line);
        content.append(mainlineColumn);
        const sideColumn = el('aside', undefined, 'side-column');
        const goals = renderGroup('当前目标', data.goals, 'goals focus-primary', 'goal');
        if (goals) sideColumn.append(goals);
        const questions = renderGroup('待解决', data.open_questions, 'open-questions', 'questions');
        if (questions) sideColumn.append(questions);
        const nextSteps = renderGroup('下一步', data.next_steps, 'next-steps', 'next');
        if (nextSteps) sideColumn.append(nextSteps);
        content.append(sideColumn);
        const records = el('section', undefined, 'records');
        records.dataset.records = '';
        records.hidden = true;
        // Keep stage details with the conversation spine. They are an on-demand
        // drill-down, not a third full-width section of the dashboard.
        mainlineColumn.append(records);
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
    async function loadView(preserve = false) {
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
                renderEmptyMainline();
                return;
            }
            renderMainline(data);
        } catch {
            if (token === state.epoch) {
                if (!preserve || !state.data?.version) empty('主线暂时不可用');
                error(() => loadView(preserve));
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
        chatSwitcher.open = !state.chat;
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
        resetMaintenance();
        state.chat = chat;
        state.version = null; state.phase = null; state.branch = null; state.offset = 0;
        state.recordsOpen = false; state.data = null;
        chatSwitcher.open = !chat;
        state.drawerEpoch++; drawer.hidden = true;
        state.chatEpoch++; state.epoch++;
        content.removeAttribute('aria-busy'); clearNotice(); populateChats();
        if (!chat) {
            empty('选择一个 Chat', '选择对话，查看主线与当前重点。');
            await startMaintenance(state.currentChat || ''); return;
        }
        empty('正在读取主线…');
        const chatToken = state.chatEpoch;
        await Promise.all([startMaintenance(chat), loadView()]);
        if (chatToken !== state.chatEpoch || maintenanceStatus?.state !== 'done' || maintenanceCompletionReloaded) return;
        const publishedVersion = maintenanceStatus.published_version;
        // The first status read can observe completion after the parallel view
        // read captured an older version. Legacy receipts need one bounded reload.
        if (!Number.isInteger(publishedVersion) || (state.data?.version ?? 0) < publishedVersion) {
            maintenanceCompletionReloaded = true;
            await loadView(true);
        }
    }
    root.querySelector('[data-chat]').onchange = e => selectChat(e.target.value);
    root.querySelector('[data-more-chats]').onclick = loadMoreChats;
    async function bootstrap() {
        resetMaintenance();
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
            state.currentChat = data.current_chat_id || state.currentChat || '';
            if (!state.chat) {
                const currentIsImported = state.currentChat && data.results.some(chat => chat.chat_id === state.currentChat);
                state.chat = data.default_chat_id || (currentIsImported ? state.currentChat : '');
            }
            state.project = data.project_root;
            state.chats = data.results;
            state.chatPage = data.retrieval;
            populateChats();
            root.querySelector('[data-project]').textContent = state.project.split('/').filter(Boolean).at(-1) || '项目记忆';
            if (!state.chat && data.current_chat_id) {
                empty('当前对话尚未收录', '整理当前对话后会在这里显示。也可以从上方查看项目中的其他对话。');
                await startMaintenance(data.current_chat_id);
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
        resetMaintenance();
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
            resetMaintenance();submit.disabled=true;clearNotice();
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
        resetMaintenance();state.currentChat=info.current_chat_id||'';
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
