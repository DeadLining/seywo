"""Validated host Python, private locked dependencies, one startup contract.

Only this bootstrap prepares SDK dependencies. Hooks/workers need stdlib only.
The host interpreter/site-packages are never modified. Run with -IBS to exclude
user/system site packages and Python environment injection.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import site
import subprocess
import sys
import sysconfig
import tempfile
import time

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN / 'src'))
from seywo.runtime_platform import FileLease  # stdlib-only OS boundary

LOCK = PLUGIN / 'runtime/requirements.lock'
CACHE_PATTERN = re.compile(r'[a-f0-9]{32}')


class RuntimeFailure(RuntimeError):
    pass


def arch(value):
    return {'aarch64': 'arm64', 'AMD64': 'x64', 'amd64': 'x64', 'x86_64': 'x64'}.get(value, value)


def validate_runtime(root):
    root = Path(root)
    try:
        with (root / 'runtime.json').open('rb') as handle:
            data = json.loads(handle.read(16385))
        if (not isinstance(data, dict) or data.get('bundleFormatVersion') != 2
                or not all(isinstance(data.get(key), str) and data[key]
                           for key in ('bundleVersion', 'targetPlatform', 'targetArch', 'pythonVersion'))):
            raise ValueError('invalid manifest')
    except (OSError, ValueError):
        raise RuntimeFailure('runtime_metadata_missing') from None
    if data.get('targetPlatform') != sys.platform or arch(data.get('targetArch')) != arch(platform.machine()):
        raise RuntimeFailure('runtime_platform_mismatch')
    expected = {(root / 'dependencies/python').resolve()}
    if os.name == 'nt':
        expected.update({(root / 'dependencies/python/python').resolve(),
                         (root / 'dependencies/python/bin').resolve()})
    if Path(sys.base_prefix).resolve() not in expected:
        raise RuntimeFailure('runtime_interpreter_mismatch')
    if data.get('pythonVersion') != platform.python_version() or sys.version_info < (3, 11):
        raise RuntimeFailure('runtime_python_incompatible')
    try:
        import sqlite3
    except ImportError:
        raise RuntimeFailure('runtime_sqlite_unavailable') from None
    try:
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE VIRTUAL TABLE check_fts USING fts5(text)')
    except sqlite3.Error:
        raise RuntimeFailure('runtime_sqlite_unavailable') from None
    return {'python': platform.python_version(), 'bundle': data['bundleVersion'],
            'platform': sys.platform, 'arch': arch(platform.machine())}


def dependency_key(lock):
    abi = f'{sys.implementation.cache_tag}:{sysconfig.get_config_var("SOABI")}:{sys.platform}:{platform.machine()}'
    return hashlib.sha256(lock + abi.encode()).hexdigest()[:32]


def private_dir(path):
    if path.is_symlink():
        raise RuntimeFailure('dependencies_unsafe_path')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if not path.is_dir() or (hasattr(os, 'getuid') and info.st_uid != os.getuid()):
        raise RuntimeFailure('dependencies_unsafe_path')
    if os.name != 'nt' and info.st_mode & 0o022:
        raise RuntimeFailure('dependencies_unsafe_path')
    return path


def cache_root():
    # MCP may filter CODEX_HOME. Use the same verified host catalog resolver as
    # settings and project binding so private SDK state never forks by process.
    from seywo.plugin_context import catalog_home
    home = catalog_home(plugin_root=PLUGIN).expanduser().absolute()
    private_dir(home)
    return private_dir(private_dir(home / 'seywo') / 'runtime')


def validate_dependencies(path):
    """Execute a bounded import probe with only stdlib + private dependency path."""
    code = ('import importlib.metadata,site,sys; site.addsitedir(sys.argv[1]); '
            'from mcp.server.fastmcp import FastMCP; '
            'assert importlib.metadata.version("mcp")=="1.28.1"')
    try:
        result = subprocess.run([sys.executable, '-IBS', '-c', code, str(path)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=20)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def install_dependencies(target, lock, *, timeout=180):
    # Use the host's bundled pip wheel directly; ensurepip never installs into it.
    try:
        import ensurepip
    except ImportError:
        raise RuntimeFailure('runtime_installer_unavailable') from None
    wheels = list((Path(ensurepip.__file__).parent / '_bundled').glob('pip-*.whl'))
    if len(wheels) != 1:
        raise RuntimeFailure('runtime_installer_unavailable')
    code = 'import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module("pip",run_name="__main__")'
    command = [sys.executable, '-IBS', '-c', code, str(wheels[0]), '--isolated',
               'install', '--disable-pip-version-check', '--no-input', '--require-hashes',
               '--only-binary=:all:', '--no-compile', '--no-cache-dir', '--retries', '1',
               '--timeout', '15', '--index-url', 'https://pypi.org/simple',
               '--target', str(target), '-r', str(lock)]
    print('Seywo: preparing pinned MCP dependencies in its private cache.', file=sys.stderr)
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeFailure('dependencies_prepare_timeout') from None
    except OSError:
        raise RuntimeFailure('dependencies_prepare_failed') from None
    if result.returncode or not validate_dependencies(target):
        raise RuntimeFailure('dependencies_prepare_failed')


def acquire(lease, *, exclusive=True, deadline=None):
    if deadline is None:
        deadline = time.monotonic() + 210
    while not lease.acquire(exclusive=exclusive, blocking=False):
        if time.monotonic() >= deadline:
            raise RuntimeFailure('dependencies_busy')
        time.sleep(.1)


def ready(path, key):
    if path.is_symlink() or (path / 'ready.json').is_symlink():
        raise RuntimeFailure('dependencies_unsafe_path')
    try:
        return json.loads((path / 'ready.json').read_text()) == {'key': key} and validate_dependencies(path)
    except (OSError, ValueError):
        return False


def collect_unused(root, current):
    # Caller owns global preparation lock. MCP keeps a shared lease while alive;
    # never delete dependencies still used by another project/chat/process.
    deferred = False
    try:
        for candidate in root.iterdir():
            try:
                if candidate.name == current or candidate.is_symlink() or not candidate.is_dir():
                    continue
                if candidate.name.startswith('.staging-'):
                    shutil.rmtree(candidate)
                elif CACHE_PATTERN.fullmatch(candidate.name):
                    with FileLease(root / (candidate.name + '.lock')) as lease:
                        if lease.acquire(blocking=False):
                            shutil.rmtree(candidate)
            except OSError:
                deferred = True
    except OSError:  # Enumeration itself can also fail after acquiring a ready cache.
        deferred = True
    if deferred:
        print('Seywo [dependencies_cleanup_deferred]: old private caches will be retried.',
              file=sys.stderr)


def prepare_dependencies():
    deadline = time.monotonic() + 210
    key = dependency_key(LOCK.read_bytes())
    root = cache_root()
    final = root / key
    shared = FileLease(root / (key + '.lock'))
    try:
        with FileLease(root / '.prepare.lock') as preparation:
            acquire(preparation, deadline=deadline)
            if not ready(final, key):
                acquire(shared, deadline=deadline)
                if final.exists():
                    shutil.rmtree(final)
                staging = Path(tempfile.mkdtemp(prefix='.staging-', dir=root))
                try:
                    remaining = deadline - time.monotonic() - 20
                    if remaining <= 0:
                        raise RuntimeFailure('dependencies_busy')
                    install_dependencies(staging, LOCK, timeout=min(180, remaining))
                    (staging / 'ready.json').write_text(json.dumps({'key': key}), encoding='utf-8')
                    staging.rename(final)
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
                shared.close()
                shared = FileLease(root / (key + '.lock'))
            acquire(shared, exclusive=False, deadline=deadline)
            collect_unused(root, key)
        return final, shared
    except BaseException:
        shared.close()
        raise


@contextmanager
def sdk_environment():
    path, lease = prepare_dependencies()
    try:
        site.addsitedir(str(path))
        yield
    finally:
        lease.close()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 3 or argv[0] != '--runtime-root':
        raise RuntimeFailure('launcher_mode_required')
    _, root, mode, *tail = argv
    if mode not in {'--mcp', '--hook', '--cli', '--doctor'}:
        raise RuntimeFailure('launcher_mode_required')
    if mode == '--hook' and os.environ.get('SEYWO_INTERNAL_MODEL') == '1':
        return 0
    details = validate_runtime(root)
    if mode == '--doctor':
        print(json.dumps(details))
        return 0
    from seywo.entrypoint import dispatch
    return dispatch(mode, tail, plugin_root=PLUGIN, sdk=sdk_environment)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except RuntimeFailure as error:
        print(f'Seywo [{error}]: startup unavailable; check the Codex workspace runtime or retry dependency preparation.', file=sys.stderr)
        raise SystemExit(1)
    except (OSError, ValueError):
        print('Seywo [runtime_unavailable]: cannot read runtime or dependency state.', file=sys.stderr)
        raise SystemExit(1)
