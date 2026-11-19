"""MCP App resource and app-only read tools for the installed plugin."""
import asyncio
import sqlite3
from pathlib import Path
from typing import Literal

from mcp.types import CallToolResult, TextContent, ToolAnnotations

from .config import initialize, load_config
from .panel import Panel
from .plugin_binding import ProjectRequired
from .plugin_context import current_chat, host_thread_id, catalog_home
from .retrieval import Retrieval, serialize, snapshot
from .settings import provider_status, read_provider, save_provider, use_codex, validate_endpoint
from .transport import Transport
from .codex_transport import CodexTransport, DEFAULT_MODEL, model_selection
from .codex_runtime import (list_models as list_codex_models, error_message,
                            validate_reasoning_effort, account_status as codex_account_status)
from .store import Store

# Bump the resource URI when the self-contained app shell changes materially.
# Codex may retain an MCP App resource by URI for the lifetime of a Chat, so
# reusing panel-v1.html would keep an already-open Chat on the previous DOM
# even after the plugin files have been reinstalled.
RESOURCE_URI = 'ui://seywo/panel-v8.html'


def register_panel(server, resolve_config):
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    @server.resource(RESOURCE_URI, name='Seywo', mime_type='text/html;profile=mcp-app',
                     meta={'ui': {'prefersBorder': False, 'csp': {'connectDomains': [], 'resourceDomains': []}},
                           'openai/widgetDescription': '项目对话主线、当前重点与按需原文回溯'})
    def seywo_panel_resource() -> str:
        return (Path(__file__).resolve().parents[2] / 'ui' / 'panel.html').read_text(encoding='utf-8')

    @server.tool(title='Seywo',
                 annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
                 meta={'ui': {'resourceUri': RESOURCE_URI, 'visibility': ['model', 'app']},
                       'openai/outputTemplate': RESOURCE_URI,
                       'openai/ui': {'entrypoints': [{'type': 'thread'}]},
                       'openai/toolInvocation/invoking': '正在打开项目记忆',
                       'openai/toolInvocation/invoked': 'Seywo 已就绪'})
    async def seywo_open(chat_id: str | None = None) -> CallToolResult:
        """Open the Seywo panel for this project. Pass an imported chat_id only when known.

        Without chat_id, default to the exact host thread only if it is already an admitted Chat.
        First open initializes only the bound project and enables its Hook. Existing settings
        are reused. No chat import or model calls. Detailed data is loaded by the app.
        """
        path = None
        try:
            path = await resolve_config(server.get_context())
            config = load_config(path) if path.exists() else None
            initialized = config is None or not Path(config['_database']).exists()
            if initialized:
                # New configurations enable the bundled Hook atomically. A retry
                # after a database creation failure reuses the existing identity.
                initialize(path.parent.parent, plugin_hook=True)
            store = Store(load_config(path), read_only=True)
        except ProjectRequired:
            info = {'state': 'project_required', 'project_root': None, 'chat_id': None, 'model_calls': 0}
        except (OSError, ValueError, sqlite3.Error) as exc:
            # Return a renderable error, including the filesystem/validation cause,
            # so the panel can retry through the same binding checks. No traceback.
            info = {'state': 'initialization_failed',
                    'project_root': str(path.parent.parent) if path is not None else None,
                    'chat_id': None, 'model_calls': 0, 'retryable': True,
                    'error': str(exc)}
        else:
            with store:
                if chat_id is not None:
                    store.require_human_chat(chat_id)
                identity, default = current_chat(store, server.get_context())
                info = {'project_root': store.config['project_root'], 'chat_id': chat_id or default,
                        'current_chat_id': identity, 'model_calls': 0}
                if initialized:
                    info['initialized'] = True
        return CallToolResult(content=[TextContent(type='text', text='Seywo：查看项目对话与主线。')],
                              structuredContent=info)

    app_only_read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    app_only_write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

    @server.tool(annotations=app_only_read, structured_output=False,
                 meta={'ui': {'visibility': ['app']}})
    async def seywo_model_status() -> str:
        """Read the project-local model configuration without calling a model."""
        path = await resolve_config(server.get_context())
        # The model setting is host-level by default. It must remain readable
        # before Seywo is initialized because initialization controls storage
        # and hooks, not access to the Codex login/provider.
        if not path.exists():
            return serialize(provider_status())
        return serialize(provider_status(load_config(path)))

    @server.tool(annotations=app_only_read, structured_output=False,
                 meta={'ui': {'visibility': ['app']}})
    async def seywo_model_list() -> str:
        """Read this host's Codex model catalog without generating model output."""
        catalog = await asyncio.to_thread(list_codex_models)
        try:
            default_model = model_selection(catalog_home(), DEFAULT_MODEL)['selected_model']
        except (OSError, ValueError, TypeError):
            default_model = None
        return serialize({**catalog, 'default_model': default_model})

    @server.tool(annotations=app_only_write, structured_output=False,
                 meta={'ui': {'visibility': ['app']}})
    async def seywo_codex_account(action: Literal['status', 'login', 'cancel'] = 'status') -> str:
        """App-only account status or explicit browser login, without a model turn.

        Codex app-server owns OAuth and credential storage. Seywo exposes only
        state and a validated browser authorization URL, never account tokens.
        """
        return serialize(await asyncio.to_thread(codex_account_status, action=action))

    @server.tool(annotations=app_only_write, structured_output=False,
                 meta={'ui': {'visibility': ['app']}})
    async def seywo_model_save(endpoint: str = "", model: str = "", api_key: str = "",
                               timeout_seconds: int = 180, max_output_tokens: int = 4000,
                               backend: Literal["codex", "responses"] = "responses",
                               reasoning_effort: str = "auto") -> str:
        """Select Codex or save a custom provider; keys never enter config.json."""
        path = await resolve_config(server.get_context())
        if backend == "codex":
            # Codex is a host-level runtime. Selecting it does not need a
            # project database and must work before Seywo initialization.
            validate_reasoning_effort(reasoning_effort)
            if not path.exists():
                if reasoning_effort != "auto" or model.strip() not in {'', 'codex-default'}:
                    raise ValueError('initialization_required')
                return serialize({"saved": True, "persisted": False, **provider_status()})
            return serialize(use_codex(load_config(path), model=model, reasoning_effort=reasoning_effort))
        if not path.exists():
            raise ValueError('initialization_required')
        result = save_provider(load_config(path), endpoint=endpoint, model=model, api_key=api_key,
                               timeout_seconds=timeout_seconds, max_output_tokens=max_output_tokens)
        return serialize(result)

    @server.tool(annotations=app_only_write, structured_output=False,
                 meta={'ui': {'visibility': ['app']}})
    async def seywo_model_test(endpoint: str = "", model: str = "", api_key: str = "",
                               timeout_seconds: int = 60,
                               backend: Literal["codex", "responses"] | None = None,
                               reasoning_effort: str | None = None) -> str:
        """Make one explicit connectivity call using the entered or saved provider."""
        path = await resolve_config(server.get_context())
        # Codex uses the host login/provider and can be tested before Seywo
        # creates project-local storage. Custom providers still use saved
        # project settings when available.
        config = load_config(path) if path.exists() else {}
        current = provider_status(config)
        selected = backend or ("responses" if endpoint or api_key else current.get("backend"))
        if selected == "codex":
            # Inherit Codex's model when no explicit Seywo override is saved.
            runtime = config.get("model_runtime") or {}
            probe = CodexTransport(model=model or runtime.get("model") or DEFAULT_MODEL,
                                   timeout=max(1, min(timeout_seconds, 300)),
                                   reasoning_effort=reasoning_effort if reasoning_effort is not None
                                       else runtime.get("reasoning_effort", "auto"))
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
                          'seconds': result.get('seconds'), 'model_calls': result.get('model_calls', 1),
                          'model_info': result.get('model_info'),
                          'message': error_message(result.get('error')) if result.get('error') else None})

    def maintenance_chat(store, requested):
        # Current unimported chat may already have a queued/failed Compact.
        # Other selections must be admitted human chats in this exact project.
        identity = host_thread_id(server.get_context())
        selected = requested or identity
        if selected is None:
            raise ValueError('chat_id_required')
        exists = store.db.execute('SELECT 1 FROM chats WHERE id=?', (selected,)).fetchone()
        if not exists and selected != identity:
            raise ValueError('unknown_chat_id')
        if exists:
            store.require_human_chat(selected)
        from .maintenance_status import _path
        _path(store.config, selected)
        return selected

    @server.tool(annotations=app_only_write, structured_output=False,
                 meta={'ui': {'visibility': ['app']}})
    async def seywo_retry_maintenance(chat_id: str | None = None) -> str:
        """Explicit app retry for this project's selected chat. Schedules a worker.

        Reuses durable source boundaries and existing progress; never imports a
        different chat, clears a published mainline or calls a model in this MCP
        request. Repeated clicks while queued/running do not schedule more work.
        """
        from .maintenance_control import retry
        path = await resolve_config(server.get_context())
        config = load_config(path)
        with Store(config, read_only=True) as store:
            selected = maintenance_chat(store, chat_id)
        return serialize(await asyncio.to_thread(retry, config, selected))

    @server.tool(annotations=readonly, structured_output=False,
                 meta={'ui': {'visibility': ['app']}})
    async def seywo_panel_data(
        view: Literal['chats', 'mainline', 'narrative', 'versions', 'changes', 'node', 'source', 'maintenance'],
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
            if view == 'maintenance':
                from .maintenance_status import read
                result = read(store.config, maintenance_chat(store, chat_id), db=store.db)
            elif view == 'chats':
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
