// Development-only MCP Apps host. The shipped plugin never starts this server.
import { AppBridge, PostMessageTransport } from '@modelcontextprotocol/ext-apps/app-bridge';
const frame = document.querySelector('iframe');
const status = document.querySelector('[data-status]');
const bridge = new AppBridge(null, { name: 'Seywo UI verification host', version: '1' }, { serverTools: {}, logging: {} }, { hostContext: { theme: 'light', displayMode: 'inline', availableDisplayModes: ['inline'] } });
bridge.oncalltool = async ({ name, arguments: args }) => {
    if (name !== 'seywo_panel_data')
        throw new Error('read_only_tools_only');
    const response = await fetch('/call', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(args) });
    if (!response.ok)
        throw new Error('read_failed');
    return response.json();
};
bridge.oninitialized = async () => {
    status.textContent = 'MCP Apps 已连接 · 本地只读预览';
    await bridge.sendToolInput({ arguments: {} });
    await bridge.sendToolResult({ content: [], structuredContent: { chat_id: null, model_calls: 0 } });
};
await bridge.connect(new PostMessageTransport(frame.contentWindow, frame.contentWindow));
frame.src = '/panel';
document.querySelector('[data-width]').onchange = e => { frame.style.width = e.target.value; };
document.querySelector('[data-theme]').onchange = e => bridge.setHostContext({ theme: e.target.value, displayMode: 'inline', availableDisplayModes: ['inline'] });
