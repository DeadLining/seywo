"""Plugin binding uses host workspace roots, not the plugin process directory."""
from pathlib import Path
from .runtime_platform import local_file_path

from .config import enable_plugin_hook, initialize, load_config, project_directory, state_dir
from .mcp_server import create_server
from .plugin_binding import ProjectRequired
from .plugin_context import host_workspace
from .retrieval import serialize


def _config_path(root):
    root = project_directory(root, strict=True)
    if not root.is_dir():
        raise ValueError('Seywo requires a project directory')
    path = state_dir(root) / 'config.json'
    plugin_root = Path(__file__).resolve().parents[2]
    if root == plugin_root or root.is_relative_to(plugin_root):
        raise ValueError('select a workspace outside the plugin installation')
    if path.exists() or path.is_symlink():
        load_config(path)
    return path


def workspace_from_roots(roots):
    if len(roots) != 1:
        raise ValueError('Seywo requires exactly one selected workspace root')
    return local_file_path(roots[0].uri)


def create_plugin_server(project_root: Path | None = None):
    selected = _config_path(project_root) if project_root is not None else None
    host_selected = False

    async def resolve(context):
        nonlocal selected, host_selected
        workspace = host_workspace(context)
        if workspace is not None:
            candidate = _config_path(workspace)
            if selected is not None and candidate != selected:
                raise ProjectRequired('host workspace changed; reconnect Seywo')
            host_selected = host_selected or selected is None
            selected = candidate
            return selected
        # A cached catalog lookup cannot bind a different/unknown request.
        # Explicit selection and a host workspace root remain valid bindings.
        if selected is not None and not host_selected:
            return selected
        from mcp.types import ErrorData
        from mcp.shared.exceptions import McpError
        try:
            result = await context.session.list_roots()
        except McpError as exc:
            if isinstance(exc.error, ErrorData) and exc.error.code in (-32601, -32600):
                raise ProjectRequired('workspace is not bound') from exc
            raise
        if len(result.roots) != 1:
            raise ProjectRequired('workspace is not bound; use seywo_select_project with the current project absolute path')
        candidate = _config_path(workspace_from_roots(result.roots))
        if selected is not None and candidate != selected:
            raise ProjectRequired('host workspace changed; reconnect Seywo')
        selected = candidate
        return selected

    server = create_server(config_resolver=resolve)
    from mcp.types import ToolAnnotations

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
        structured_output=False,
    )
    def seywo_select_project(project_root: str) -> str:
        """Bind this plugin process to the current user-selected project absolute path.

        Use when the host does not supply one unambiguous MCP workspace root.
        No discovery, import, initialization or model calls. Binding cannot change
        to another project during this process; reconnect to select another workspace.
        """
        nonlocal selected, host_selected
        root = Path(project_root).expanduser()
        if not root.is_absolute():
            raise ValueError('project_root must be an absolute path')
        candidate = _config_path(root)
        if selected is not None and candidate != selected:
            raise ValueError('Seywo is already bound to another project; reconnect before switching')
        selected = candidate
        host_selected = False
        return serialize({'project_root': str(selected.parent.parent), 'initialized': selected.exists(),
                          'model_calls': 0})

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
        structured_output=False,
    )
    async def seywo_initialize_project() -> str:
        """Explicitly enable Seywo in the host's selected workspace.

        Creates or reuses .seywo, preserves existing settings, and enables the bundled lifecycle Hook.
        No chat import or model call. Use only when enabling Seywo is authorized.
        """
        path = await resolve(server.get_context())
        result = initialize(path.parent.parent)
        result["hook"] = enable_plugin_hook(path)
        return serialize(result)

    from .plugin_ui import register_panel
    register_panel(server, resolve)
    return server


def main(argv=None):
    import argparse
    import sys

    parser = argparse.ArgumentParser(description='Seywo Codex plugin MCP')
    parser.add_argument('--project', type=Path, help='Explicit project for CLI diagnostics; otherwise use host MCP roots')
    args = parser.parse_args(argv)
    try:
        create_plugin_server(args.project).run(transport='stdio')
    except (OSError, ValueError) as exc:
        print(f'Seywo could not open the selected project: {exc}', file=sys.stderr)
        return 1
    finally:
        from .codex_app_server import close_workers
        close_workers()
    return 0
