"""Plugin binding uses host workspace roots, not the plugin process directory."""
from pathlib import Path
from .runtime_platform import local_file_path

from .config import enable_plugin_hook, initialize, load_config, state_dir
from .mcp_server import create_server
from .plugin_binding import ProjectRequired, ReconnectRequired
from .plugin_context import host_workspace
from .project_scope import ProjectBinding, project_directory, project_label
from .retrieval import serialize


def _workspace_binding(root, installation, *, explicit):
    root = project_directory(root, strict=True)
    if not root.is_dir():
        raise ValueError('Seywo requires a project directory')
    owner = (ProjectBinding.from_workspace(root) if explicit
             else ProjectBinding.from_observed_cwd(root))
    if installation.contains_directory(root) or (str(owner.root) != str(root)
                                                and installation.contains_directory(owner.root)):
        raise ValueError('select a workspace outside the plugin installation')
    state_dir(owner.root)  # Reject a symlinked state directory even before initialization.
    return owner


def workspace_from_roots(roots):
    if len(roots) != 1:
        raise ValueError('Seywo requires exactly one selected workspace root')
    return local_file_path(roots[0].uri)


def create_plugin_server(project_root: Path | None = None):
    installation = ProjectBinding.at(Path(__file__).resolve().parents[2])
    selected = None
    selected_id = None
    explicit_selection = project_root is not None

    def bind(workspace, *, explicit=False):
        nonlocal selected, selected_id
        candidate = _workspace_binding(workspace, installation, explicit=explicit)
        if selected is not None and candidate.identity != selected.identity:
            raise ReconnectRequired('Seywo is already bound to another project; reconnect before switching'
                                    if explicit else 'host workspace changed; reconnect Seywo')
        path = candidate.config_path
        project_id = load_config(path)['project_id'] if path.exists() or path.is_symlink() else None
        if selected_id is not None and project_id != selected_id:
            raise ReconnectRequired('project identity changed; reconnect Seywo')
        selected, selected_id = candidate, project_id
        return path

    def initialize_bound(root, *, plugin_hook=False):
        nonlocal selected_id
        result = initialize(root, plugin_hook=plugin_hook)
        selected_id = result['project_id']
        return result

    if project_root is not None:
        bind(project_root, explicit=True)

    async def resolve(context):
        workspace = host_workspace(context)
        if workspace is not None:
            return bind(workspace)
        # Only a user selection is standing authority. Catalog and roots are
        # request evidence; the previous binding is solely a guard against switching.
        if explicit_selection:
            return bind(selected.root, explicit=True)
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
        return bind(workspace_from_roots(result.roots))

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
        nonlocal explicit_selection
        root = Path(project_root).expanduser()
        if not root.is_absolute():
            raise ValueError('project_root must be an absolute path')
        path = bind(root, explicit=True)
        explicit_selection = True
        return serialize({'project_root': str(selected.root), 'project_name': project_label(selected.root),
                          'initialized': path.exists(),
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
        result = initialize_bound(path.parent.parent)
        result["hook"] = enable_plugin_hook(path)
        return serialize(result)

    from .plugin_ui import register_panel
    register_panel(server, resolve, initialize_bound)
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
