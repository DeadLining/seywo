"""OS boundaries for leases, durable publication and owned process trees.

Business code never imports fcntl, Win32 or sends Unix signals directly.
"""
import asyncio
import base64
import os
from pathlib import Path
import signal
import shutil
import stat
import subprocess
import time


class FileLease:
    """Kernel-owned lock: crash-safe, shared/exclusive, optionally nonblocking.

    create=False opens an existing file read-only. Probes never create lock files.
    All backends lock the same inode/byte range and never write lock contents.
    """
    def __init__(self, path, *, create=True):
        flags = os.O_RDWR | os.O_CREAT if create else os.O_RDONLY
        self.fd = os.open(Path(path), flags | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        self.locked = False

    def acquire(self, *, exclusive=True, blocking=True):
        if self.locked:
            raise RuntimeError('lease_already_acquired')
        if os.name == 'nt':
            from .win32_native import lock_file
            acquired = lock_file(self.fd, exclusive=exclusive, blocking=blocking)
        else:
            import fcntl
            flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            try:
                fcntl.flock(self.fd, flags | (0 if blocking else fcntl.LOCK_NB))
                acquired = True
            except BlockingIOError:
                acquired = False
        self.locked = acquired
        return acquired

    def close(self):
        if self.fd is not None:
            try:
                if self.locked:
                    if os.name == 'nt':
                        from .win32_native import unlock_file
                        unlock_file(self.fd)
                    else:
                        import fcntl
                        fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None
                self.locked = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def sync_directory(path):
    """Persist rename metadata on POSIX; Windows has no directory fsync API.

    Callers fsync and close file data before atomic replacement on every OS.
    Do not emulate directory flushing by opening it with POSIX flags on Windows.
    """
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def process_options(*, platform=None, detached=False):
    if (platform or os.name) == 'nt':
        # Detached workers outlive the short Hook. Model children have no console.
        return {'creationflags': (0x00000008 if detached else 0x08000000)
                                 | 0x00000200}  # CREATE_NEW_PROCESS_GROUP
    return {'start_new_session': True}


class ProcessScope:
    """Own a model/catalog process tree until the complete operation exits.

    On Windows start suspended, assign to a kill-on-close Job, then resume.
    This closes the race in which a child could spawn before Job assignment.
    Detached maintenance workers deliberately use process_options directly.
    """
    def __init__(self):
        self.process = None
        self.job = None
        self._terminated = False

    def __enter__(self):
        return self

    def _options(self):
        options = process_options()
        if os.name == 'nt':
            from .win32_native import Job
            self.job = Job()
            options['creationflags'] |= 0x00000004  # CREATE_SUSPENDED
        return options

    def _adopt(self, process):
        self.process = process
        if self.job:
            try:
                self.job.assign_and_resume(process.pid)
            except BaseException:
                process.kill()
                self.job.close()
                self.job = None
                raise
        return process

    def start(self, args, **kwargs):
        try:
            return self._adopt(subprocess.Popen(args, **kwargs, **self._options()))
        except BaseException:
            self.terminate()
            if self.process:
                self.process.wait()
            raise

    async def start_async(self, *args, **kwargs):
        try:
            return self._adopt(await asyncio.create_subprocess_exec(*args, **kwargs, **self._options()))
        except BaseException:
            self.terminate()
            if self.process:
                await self.process.wait()
            raise

    def terminate(self):
        if self._terminated:
            return
        self._terminated = True
        if self.job:
            self.job.close()
            self.job = None
        elif self.process and os.name != "nt":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def __exit__(self, *exc):
        self.terminate()
        # Async owners await wait() in their own finally block.
        if isinstance(self.process, subprocess.Popen):
            self.process.wait()


def private_owner(info, mode):
    """POSIX ownership/mode guard; Windows secrets are current-user DPAPI blobs."""
    return os.name == 'nt' or (stat.S_IMODE(info.st_mode) == mode and info.st_uid == os.getuid())


def protect_credential(api_key, endpoint):
    if os.name == 'nt':
        from .win32_native import protect_secret
        return {'api_key_dpapi': base64.b64encode(protect_secret(api_key.encode('utf-8'), endpoint.encode('utf-8'))).decode('ascii')}
    return {'api_key': api_key}


def reveal_credential(data, endpoint):
    if os.name == 'nt':
        from .win32_native import reveal_secret
        value = data.get('api_key_dpapi')
        if not isinstance(value, str):
            raise ValueError('unsafe_credentials')
        try:
            raw = base64.b64decode(value, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError('unsafe_credentials') from exc
        return reveal_secret(raw, endpoint.encode('utf-8')).decode('utf-8')
    return data.get('api_key') or ''


def local_file_path(value):
    from urllib.parse import urlsplit
    from urllib.request import url2pathname
    uri = urlsplit(str(value))
    if uri.scheme != 'file' or uri.netloc not in ('', 'localhost') or uri.query or uri.fragment:
        raise ValueError('Seywo requires a local file workspace root')
    return Path(url2pathname(uri.path))


def native_codex_candidate(candidate, *, platform=None):
    """Resolve Windows npm shims to their packaged executable, without cmd.exe."""
    candidate = Path(candidate)
    platform = platform or os.name
    if not candidate.is_file():
        return None
    if platform == 'nt':
        if candidate.suffix.lower() == '.exe':
            return [str(candidate)]
        if candidate.suffix.lower() in {'.cmd', '.bat', '.ps1'}:
            base = candidate.parent
            for arch in ('x64', 'arm64'):
                triple = ('x86_64' if arch == 'x64' else 'aarch64') + '-pc-windows-msvc'
                suffix = Path('vendor') / triple / 'codex/codex.exe'
                for package in (base / 'node_modules/@openai' / ('codex-win32-' + arch),
                                base / 'node_modules/@openai/codex/node_modules/@openai' / ('codex-win32-' + arch),
                                base / 'node_modules/@openai/codex'):
                    binary = package / suffix
                    if binary.is_file():
                        return [str(binary)]
        return None
    return [str(candidate)] if os.access(candidate, os.X_OK) else None


def find_codex():
    host = os.environ.get('CODEX_APP_RUNTIME_CLI_PATH', '')
    if host and Path(host).is_absolute():
        if command := native_codex_candidate(host):
            return command
    # Prefer a native Windows executable before any npm command shim.
    names = ('codex.exe', 'codex') if os.name == 'nt' else ('codex',)
    for name in names:
        if found := shutil.which(name):
            if command := native_codex_candidate(found):
                return command
    if os.name == 'posix':
        for base in (Path('/Applications'), Path.home() / 'Applications'):
            for relative in ('Codex.app/Contents/Resources/codex',
                             'ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'):
                if command := native_codex_candidate(base / relative):
                    return command
    return None


def replace_file(source, target):
    """Atomic replacement, with Windows write-through and bounded sharing retry."""
    if os.name != 'nt':
        os.replace(source, target)
        return
    from .win32_native import replace_file as native_replace
    for attempt in range(4):
        try:
            native_replace(source, target)
            return
        except OSError as exc:
            if exc.winerror not in (32, 33) or attempt == 3:
                raise
            time.sleep(.01 * (2 ** attempt))
