"""Local workspace ownership shared by ingestion, Hooks and the plugin."""

import os
from pathlib import Path
import re


CONFIG = Path('.seywo/config.json')
_GIT_FILE_LIMIT = 65536


def _git_path(file: Path, prefix: bytes = b'') -> Path | None:
    if file.is_symlink() or not file.is_file():
        return None
    with file.open('rb') as handle:
        raw = handle.read(_GIT_FILE_LIMIT + 1)
    if len(raw) > _GIT_FILE_LIMIT or not raw.startswith(prefix):
        return None
    raw = raw[len(prefix):].rstrip(b'\r\n')
    if not raw or any(char in raw for char in (b'\0', b'\n', b'\r')):
        return None
    return (file.parent / os.fsdecode(raw)).resolve(strict=True)


def _linked_owner(marker: Path) -> Path | None:
    """Recognize a registered linked checkout of a repository with a local .git directory."""
    try:
        admin = _git_path(marker, b'gitdir: ')
        if admin is None or not admin.is_dir() or admin.parent.name != 'worktrees':
            return None
        common = admin.parent.parent
        # A gitfile alone can also be a submodule, separate git dir, or stale copy.
        # Require the common directory and the registration's exact backlink.
        if (common.name != '.git' or common.parent == marker.parent
                or not (common / 'HEAD').is_file() or not (common / 'objects').is_dir()
                or _git_path(admin / 'commondir') != common
                or _git_path(admin / 'gitdir') != marker):
            return None
        return common.parent
    except (OSError, ValueError, RuntimeError):
        return None


def resolve_project_root(workspace: Path, *, discover_config: bool = False) -> Path:
    """Resolve a verified linked worktree to its primary checkout.

    Ordinary workspaces keep their exact binding. Only Hook discovery inherits
    an enclosing configuration, and never crosses the nearest Git boundary.
    A separately initialized scope below a linked checkout remains independent;
    a configuration at the linked checkout root is not the repository owner.
    """
    workspace = workspace.expanduser().resolve()
    configured = None
    for candidate in (workspace, *workspace.parents):
        marker = candidate / '.git'
        if marker.exists() or marker.is_symlink():
            if owner := _linked_owner(marker):
                return configured or owner
            if configured is None and (candidate / CONFIG).is_file():
                configured = candidate
            break
        if configured is None and (candidate / CONFIG).is_file():
            configured = candidate
    return configured if discover_config and configured is not None else workspace


def _without_windows_path_prefix(path: str) -> str:
    if path[:8].lower() == '\\\\?\\unc\\':
        return '\\\\' + path[8:]
    if re.match(r'^\\\\\?\\[A-Za-z]:\\', path):
        return path[4:]
    return path


def project_paths_match(path: Path, project_root: str) -> bool:
    """Compare resolved project paths while preserving the stored owner spelling."""
    if not isinstance(project_root, str):
        return False
    if os.name == 'nt':
        return _without_windows_path_prefix(str(path)) == _without_windows_path_prefix(project_root)
    return str(path) == project_root


def source_belongs_to_project(cwd: str, project_root: str) -> bool:
    workspace = Path(cwd).expanduser().resolve()
    return (project_paths_match(workspace, project_root)
            or project_paths_match(resolve_project_root(workspace), project_root))
