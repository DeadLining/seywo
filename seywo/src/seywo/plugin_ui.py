"""MCP App resource and app-only read tools for the installed plugin."""
import asyncio
from pathlib import Path
from typing import Literal

from mcp.types import CallToolResult, TextContent, ToolAnnotations

from .config import load_config
from .panel import Panel
from .plugin_binding import ProjectRequired
from .plugin_context import current_chat
from .retrieval import Retrieval, serialize, snapshot
from .settings import provider_status, read_provider, save_provider, use_codex, validate_endpoint
from .transport import Transport
from .codex_transport import CodexTransport
from .store import Store

RESOURCE_URI = 'ui://seywo/panel-v1.html'


def register_panel(server, resolve_config):
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    @server.resource(RESOURCE_URI, name='Seywo', mime_type='text/html;profile=mcp-app',
                     meta={'ui': {'prefersBorder': False, 'csp': {'connectDomains': [], 'resourceDomains': []}},
                           'openai/widgetDescription': '项目对话主线、当前重点与按需原文回溯'})
    def seywo_panel_resource() -> str:
        return (Path(__file__).resolve().parents[2] / 'ui' / 'panel.html').read_text(encoding='utf-8')

    @server.tool(title='Seywo', annotations=readonly,
                 meta={'ui': {'resourceUri': RESOURCE_URI, 'visibility': ['model', 'app']},
                       'openai/outputTemplate': RESOURCE_URI,
                       'openai/ui': {'entrypoints': [{'type': 'thread'}]},
                       'openai/toolInvocation/invoking': '正在打开 Seywo',
                       'openai/toolInvocation/invoked': 'Seywo 已就绪'})
    async def seywo_open(chat_id: str | None = None) -> CallToolResult:
        """Open the Seywo panel for this project. Pass an imported chat_id only when known.

        Without chat_id, default to the exact host thread only if it is already an admitted Chat.
        Read-only; no import, setup or model calls. Detailed data is loaded by the app.
        """
        try:
            path = await resolve_config(server.get_context())
        except ProjectRequired:
            info = {'state': 'project_required', 'project_root': None, 'chat_id': None, 'model_calls': 0}
        else:
            if not path.exists():
                info = {'state': 'initialization_required', 'project_root': str(path.parent.parent),
                        'chat_id': None, 'model_calls': 0}
            else:
                with Store(load_config(path), read_only=True) as store:
                    if chat_id is not None:
                        store.require_human_chat(chat_id)
                    identity, default = current_chat(store, server.get_context())
                    info = {'project_root': store.config['project_root'], 'chat_id': chat_id or default,
                            'current_chat_id': identity, 'model_calls': 0}
        return CallToolResult(content=[TextContent(type='text', text='Seywo：查看项目对话与主线。')],
                              structuredContent=info)

    app_only_read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    app_only_write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

    @server.tool(annotations=app_only_read, structured_output=False,
                 meta={'ui': {'visibility': ['app']}})
    async def seywo_model_status() -> str:
        """Read the project-local model configuration without calling a model."""
        path = await resolve_config(server.get_context())
        if not path.exists():
            return serialize({'configured': False, 'endpoint': '', 'model': '',
                              'credential_configured': False, 'model_calls': 0})
        return serialize(provider_status(load_config(path)))

    @server.tool(annotations=app_only_write, structured_output=False,
                 meta={'ui': {'visibility': ['app']}})
    async def seywo_model_save(endpoint: str = "", model: str = "", api_key: str = "",
                               timeout_seconds: int = 180, max_output_tokens: int = 4000,
                               backend: Literal["codex", "responses"] = "responses") -> str:
        """Save the provider for this project; the key is never returned or persisted in config.json."""
        path = await resolve_config(server.get_context())
        if not path.exists():
            raise ValueError('initialization_required')
        if backend == "codex":
            return serialize(use_codex(load_config(path), model=model))
        result = save_provider(load_config(path), endpoint=endpoint, model=model, api_key=api_key,
                               timeout_seconds=timeout_seconds, max_output_tokens=max_output_tokens)
        return serialize(result)

    @server.tool(annotations=app_only_write, structured_output=False,
                 meta={'ui': {'visibility': ['app']}})
    async def seywo_model_test(endpoint: str = "", model: str = "", api_key: str = "",
                               timeout_seconds: int = 60,
                               backend: Literal["codex", "responses"] | None = None) -> str:
        """Make one explicit connectivity call using the entered or saved provider."""
        path = await resolve_config(server.get_context())
        if not path.exists():
            raise ValueError('initialization_required')
        config = load_config(path)
        current = provider_status(config)
        selected = backend or ("responses" if endpoint or api_key else current.get("backend"))
        if selected == "codex":
            # Inherit Codex's model when no explicit Seywo override is saved.
            runtime = config.get("model_runtime") or {}
            probe = CodexTransport(model=model or runtime.get("model") or "codex-default",
                                   timeout=max(1, min(timeout_seconds, 300)))
            result = await asyncio.to_thread(probe.probe)
        else:
            saved_config = {**config, "model_runtime": {"backend": "responses"}}
            saved_status = provider_status(saved_config)
            saved = read_provider(saved_config) if saved_status["configured"] and saved_status["credential_configured"] else {}
            endpoint = validate_endpoint(endpoint or saved.get("endpoint") or saved_status.get("endpoint"))
            model = model or saved.get("model")
            key = api_key or (saved.get("_api_key") if endpoint == saved.get("endpoint") else None)
            if not endpoint or not model or not key:
                raise ValueError("provider_not_configured")
            probe = Transport(endpoint, key, model, timeout=max(1, min(timeout_seconds, 300)))
            result = await asyncio.to_thread(probe._complete_json, model=model,
                instructions='Return a JSON object with ok=true.', payload={"probe": "seywo"}, max_output_tokens=256)
        return serialize({'ok': result.get('output') == {'ok': True} and result.get('finish', 'complete') == 'complete' and result.get('error') is None,
                          'error': result.get('error'), 'usage': result.get('usage'),
                          'seconds': result.get('seconds'), 'model_calls': 1})

    @server.tool(annotations=readonly, structured_output=False,
                 meta={'ui': {'visibility': ['app']}})
    async def seywo_panel_data(
        view: Literal['chats', 'mainline', 'narrative', 'versions', 'changes', 'node', 'source'],
        chat_id: str | None = None, version: int | None = None,
        phase_id: str | None = None, branch_id: str | None = None,
        object_id: str | None = None, event_id: str | None = None,
        snapshot_id: str | None = None, start: int = 0, end: int | None = None,
        offset: int = 0, limit: int = 20,
    ) -> str:
        """App-only, read-only paged data. No model calls or automatic initialization."""
        path = await resolve_config(server.get_context())
        with Store(load_config(path), read_only=True) as store, snapshot(store):
            panel = Panel(store)
            if view == 'chats':
                result = panel.chats(offset=offset, limit=limit)
                identity, default = current_chat(store, server.get_context())
                result.update(current_chat_id=identity, default_chat_id=default)
            elif view == 'mainline':
                if chat_id is None:
                    raise ValueError('chat_id_required')
                result = panel.mainline(chat_id)
            elif view == 'node':
                if object_id is None:
                    raise ValueError('object_id_required')
                result = Retrieval(store).expand(object_id, limit=1, max_bytes=32000)
            elif view == 'source':
                if not all((chat_id, event_id, snapshot_id)):
                    raise ValueError('source_identity_required')
                result = Retrieval(store).read_source(chat_id, event_id, snapshot_id, start=start, end=end, max_bytes=16000)
            else:
                if chat_id is None:
                    raise ValueError('chat_id_required')
                args = {'chat_id': chat_id, 'offset': offset, 'limit': limit}
                if view != 'versions':
                    args['version'] = version
                if view == 'narrative':
                    args.update(phase_id=phase_id, branch_id=branch_id)
                result = panel.read(view, **args)
            return serialize(result)
