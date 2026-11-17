import { App } from '@modelcontextprotocol/ext-apps/app-with-deps';
import { mountPanel } from './panel.js';
const root = document.querySelector('#app');
const app = new App({ name: 'Seywo', version: '0.4.0' }, {});
let initial = {}, controller;
app.ontoolresult = params => { initial = params.structuredContent || {}; if (controller)
    controller.open(initial); };
app.onhostcontextchanged = ctx => { if (ctx.theme)
    document.documentElement.dataset.theme = ctx.theme; };
const serverTool = async (name,args) => {
    const result = await app.callServerTool({ name, arguments: args });
    if (result.isError) {
        const error = new Error('panel_read_failed');
        if (name === 'seywo_initialize_project')
            error.initializationCause = result.content.find(c => c.type === 'text')?.text;
        throw error;
    }
    if (result.structuredContent)
        return result.structuredContent;
    return JSON.parse(result.content.find(c => c.type === 'text').text);
};
const call=args=>serverTool('seywo_panel_data',args);
const actions={initializeProject:()=>serverTool('seywo_initialize_project',{}),selectProject:project_root=>serverTool('seywo_select_project',{project_root}),openCurrent:()=>serverTool('seywo_open',{}),modelStatus:()=>serverTool('seywo_model_status',{}),modelSave:args=>serverTool('seywo_model_save',args),modelTest:args=>serverTool('seywo_model_test',args)};
try {
    await app.connect();
    const theme = app.getHostContext()?.theme;
    if (theme)
        document.documentElement.dataset.theme = theme;
    const seed = initial;
    controller = await mountPanel(root, call, seed, actions);
    if (initial !== seed)
        await controller.open(initial);
}
catch {
    root.replaceChildren();
    const p = document.createElement('p');
    p.className = 'empty';
    p.textContent = 'Seywo 暂未连接到宿主。请在支持 MCP Apps 的客户端中重新打开。';
    root.append(p);
}
