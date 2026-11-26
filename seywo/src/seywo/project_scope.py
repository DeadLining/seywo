"""Local workspace ownership shared by ingestion, Hooks and the plugin."""

from dataclasses import dataclass
import os
from pathlib import Path, PurePath, PureWindowsPath
from stat import S_ISDIR
from unicodedata import normalize

from .windows_paths import extended_windows_path, native_windows_path


CONFIG = Path('.seywo/config.json')
_GIT_FILE_LIMIT = 65536


def project_directory(root: Path, *, strict: bool = False) -> Path:
    root = root.expanduser().absolute()
    if root.is_symlink():
        raise ValueError('Seywo project directory must not be a symlink')
    return root.resolve(strict=strict)


@dataclass(frozen=True, eq=False)
class ProjectBinding:
    """Existing physical owner, pinned independently of the spelling used for I/O."""

    root: Path
    identity: tuple[int, int]

    @classmethod
    def at(cls, root: Path):
        root = project_directory(root, strict=True)
        st = root.stat()
        if not S_ISDIR(st.st_mode) or not st.st_ino:
            raise ValueError('Seywo requires an identifiable project directory')
        return cls(root, (st.st_dev, st.st_ino))

    @classmethod
    def from_workspace(cls, workspace: Path):
        workspace = project_directory(workspace, strict=True)
        return cls.at(_resolve_owner(workspace))

    @classmethod
    def from_observed_cwd(cls, workspace: Path, *, confirm_history=None):
        """Read evidence, not a new workspace selection. Never create the observed path."""
        if not workspace.is_absolute():
            raise ValueError('expected an absolute observed working directory')
        try:
            resolved = workspace.resolve(strict=True)
        except FileNotFoundError:
            if '..' in workspace.parts:
                raise UnverifiedWorkingDirectory(None)
            # Only ENOENT may discard a suffix. Never walk past dangling links,
            # access errors, an existing file, or an intervening Git boundary.
            for ancestor in (workspace, *workspace.parents):
                try:
                    ancestor.lstat()
                except FileNotFoundError:
                    continue
                resolved = ancestor.resolve(strict=True)
                break
            else:
                raise
            candidate = cls.at(_resolve_owner(resolved, require_worktree=True))
            # Registration identifies where to look, not which project owned a
            # vanished descendant. It may have been an independent workspace.
            if confirm_history is None or not confirm_history(candidate):
                raise UnverifiedWorkingDirectory(candidate.config_path)
            return candidate
        return cls.at(_resolve_owner(resolved, discover_config=True))

    @property
    def config_path(self):
        return self.root / CONFIG

    def owns(self, path: Path) -> bool:
        try:
            return self.identity == ProjectBinding.at(path).identity
        except (OSError, ValueError, RuntimeError):
            return False

    def contains_directory(self, resolved: Path) -> bool:
        """Check existing ancestors; propagate I/O errors instead of bypassing exclusion."""
        for parent in (resolved, *resolved.parents):
            st = parent.stat()
            if (st.st_dev, st.st_ino) == self.identity:
                return True
        return False

    def matches_stored_owner(self, value) -> bool:
        # The stored string is not a locator. Only stat a namespace alternative
        # generated from our bound path (verbatim and DOS semantics can differ).
        if stored_owner_candidate(self.root, value):
            return value == str(self.root) or self.owns(Path(value))
        return _same_posix_spelling(self.root, value)


def _same_posix_spelling(root: Path, value: object) -> bool:
    """On case-preserving volumes, verify each alternate name at its trusted parent."""
    if os.name != 'posix' or not isinstance(value, str):
        return False
    stored = Path(value)
    if (str(stored) != value or stored.anchor != root.anchor or len(stored.parts) != len(root.parts)
            or any(normalize('NFD', a.casefold()) != normalize('NFD', b.casefold())
                   for a, b in zip(root.parts, stored.parts))):
        return False
    # Never traverse the configured path. lstat also rejects a same-named symlink
    # on a case-sensitive volume, even when it points back to the real owner.
    parent = Path(root.anchor)
    for actual, other in zip(root.parts[1:], stored.parts[1:]):
        child = parent / actual
        if actual != other:
            try:
                if not os.path.samestat(child.lstat(), (parent / other).lstat()):
                    return False
            except FileNotFoundError:
                return False
        parent = child
    return True


def stored_owner_candidate(root: PurePath, value: object) -> bool:
    if not isinstance(value, str):
        return False
    if isinstance(root, PureWindowsPath):
        try:
            if value[:8].lower() == '\\\\?\\unc\\':
                value = '\\\\?\\UNC\\' + value[8:]
            return value in (str(root), str(native_windows_path(root)), str(extended_windows_path(root)))
        except ValueError:
            return False
    return value == str(root)


def project_label(root: Path) -> str:
    # Drive, share and filesystem roots have no leaf directory name.
    return root.name or '项目记忆'


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
        backlink = _git_path(admin / 'gitdir')
        commondir = _git_path(admin / 'commondir')
        if (common.name != '.git' or common.parent.samefile(marker.parent)
                or not (common / 'HEAD').is_file() or not (common / 'objects').is_dir()
                or commondir is None or not commondir.samefile(common)
                or backlink is None or not backlink.parent.samefile(marker.parent)
                or not backlink.samefile(marker)):
            return None
        return common.parent
    except (OSError, ValueError, RuntimeError):
        return None


class UnverifiedWorkingDirectory(ValueError):
    """A vanished cwd lacks registration or corroborating chat history."""

    def __init__(self, nearby_config):
        super().__init__('cannot verify the owner of the missing working directory')
        self.nearby_config = nearby_config  # Notice opt-in only; never an enqueue target.


class SourceOwnerMismatch(ValueError):
    """A readable source is not authorized for the destination project."""

    code = 'source_project_unverified'


def _resolve_owner(workspace: Path, *, discover_config=False, require_worktree=False) -> Path:
    """Resolve a verified linked worktree to its primary checkout.

    Ordinary workspaces keep their exact binding. Observed dialogue directories
    inherit an enclosing configuration, never crossing the nearest Git boundary.
    A separately initialized scope below a linked checkout remains independent;
    a configuration at the linked checkout root is not the repository owner.
    """
    if not S_ISDIR(workspace.stat().st_mode):
        raise ValueError('expected a working directory')
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
    if require_worktree:
        raise UnverifiedWorkingDirectory(configured / CONFIG if configured else None)
    return configured if discover_config and configured is not None else workspace


def source_belongs_to_project(cwd: str, project_root: str | Path, *, has_history=None) -> bool:
    try:
        owner = ProjectBinding.at(Path(project_root))
        def confirm(candidate):
            return candidate.identity == owner.identity and has_history is not None and has_history()

        observed = ProjectBinding.from_observed_cwd(Path(cwd), confirm_history=confirm)
        return observed.identity == owner.identity
    except (OSError, ValueError, RuntimeError):
        return False
