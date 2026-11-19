"""Native locking/process contracts shared by macOS, Windows and Linux CI."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from seywo.runtime_platform import FileLease, ProcessScope, process_options, sync_directory


def child(code, *args):
    return subprocess.run([sys.executable, '-c', code, *map(str, args)],
                          capture_output=True, text=True, timeout=15,
                          env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})


def test_exclusive_lease_is_nonblocking_and_released_on_close(tmp_path):
    path = tmp_path / '中文 lock'
    first = FileLease(path)
    assert first.acquire(blocking=False)
    second = FileLease(path)
    assert not second.acquire(blocking=False)
    first.close()
    assert second.acquire(blocking=False)
    second.close()
    assert path.read_bytes() == b''  # Windows locks beyond EOF; no lock-file growth.


def test_shared_leases_and_cross_process_exclusion(tmp_path):
    path = tmp_path / 'lease'
    code = '''from seywo.runtime_platform import FileLease
import sys
lease=FileLease(sys.argv[1])
print(lease.acquire(exclusive=sys.argv[2]=='exclusive',blocking=False))
lease.close()
'''
    lease = FileLease(path)
    assert lease.acquire(exclusive=False)
    assert child(code, path, 'shared').stdout.strip() == 'True'
    assert child(code, path, 'exclusive').stdout.strip() == 'False'
    lease.close()
    assert child(code, path, 'exclusive').stdout.strip() == 'True'


def test_read_only_lease_never_creates_or_modifies_file(tmp_path):
    path = tmp_path / 'missing'
    with pytest.raises(FileNotFoundError):
        FileLease(path, create=False)
    path.write_bytes(b'unchanged')
    lease = FileLease(path, create=False)
    assert lease.acquire()
    lease.close()
    assert path.read_bytes() == b'unchanged'


def test_crashed_owner_releases_native_lock(tmp_path):
    path = tmp_path / 'crash'
    result = child('from seywo.runtime_platform import FileLease\nimport os,sys\nl=FileLease(sys.argv[1]);l.acquire();os._exit(0)', path)
    assert result.returncode == 0
    lease = FileLease(path)
    assert lease.acquire(blocking=False)
    lease.close()


def test_platform_options_do_not_mix_posix_and_windows_flags():
    assert process_options(platform='posix') == {'start_new_session': True}
    windows = process_options(platform='nt')
    assert 'start_new_session' not in windows
    assert windows['creationflags'] & 0x08000000  # CREATE_NO_WINDOW
    detached = process_options(platform='nt', detached=True)
    assert detached['creationflags'] & 0x00000008  # DETACHED_PROCESS
    assert not detached['creationflags'] & 0x08000000


def test_process_scope_kills_descendants_and_reaps_parent(tmp_path):
    ready = tmp_path / 'ready'
    survivor = tmp_path / 'survivor'
    worker = ('import time;from pathlib import Path;'
              f'time.sleep(1.5);Path({str(survivor)!r}).write_text("orphan")')
    parent = ('import subprocess,sys,time;from pathlib import Path;'
              f'subprocess.Popen([sys.executable,"-c",{worker!r}]);'
              f'Path({str(ready)!r}).write_text("ready");time.sleep(30)')
    with ProcessScope() as scope:
        proc = scope.start([sys.executable, '-c', parent], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists()
        scope.terminate()
        assert proc.wait(timeout=5) != 0
    time.sleep(1.6)
    assert not survivor.exists()


def test_sync_directory_accepts_native_directory(tmp_path):
    sync_directory(tmp_path)


def test_credentials_round_trip_and_endpoint_binding():
    from seywo.runtime_platform import protect_credential, reveal_credential
    blob = protect_credential('private-key', 'https://fixture.test/v1/responses')
    assert reveal_credential(blob, 'https://fixture.test/v1/responses') == 'private-key'
    if os.name == 'nt':
        assert 'private-key' not in json.dumps(blob)
        with pytest.raises(OSError):
            reveal_credential(blob, 'https://other.test/v1/responses')
        with pytest.raises(ValueError, match='unsafe_credentials'):
            reveal_credential({'api_key': 'plaintext'}, 'https://fixture.test/v1/responses')


def test_native_adapter_has_no_unconditional_unix_dependency():
    # Import every shipped module without loading Unix primitives in business code.
    import ast
    root = Path(__file__).resolve().parents[1] / 'src/seywo'
    forbidden = []
    for path in root.glob('*.py'):
        if path.name in {'runtime_platform.py', 'win32_native.py'}:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.Import):
                forbidden.extend((path.name, a.name) for a in node.names if a.name in {'fcntl', 'msvcrt'})
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {'killpg', 'getuid', 'fork', 'setsid'}:
                    forbidden.append((path.name, node.func.attr))
    assert not forbidden


def test_windows_npm_shim_resolves_native_codex_without_shell(tmp_path):
    from seywo.runtime_platform import native_codex_candidate
    shim = tmp_path / 'codex.cmd'
    shim.write_text('@echo off\n')
    assert native_codex_candidate(shim, platform='nt') is None
    binary = tmp_path / 'node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/codex/codex.exe'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'fixture')
    assert native_codex_candidate(shim, platform='nt') == [str(binary)]


def test_local_workspace_uri_preserves_native_path(tmp_path):
    from seywo.runtime_platform import local_file_path
    project = tmp_path / '中文 空格 % #'
    assert local_file_path(project.as_uri()) == project
    with pytest.raises(ValueError):
        local_file_path('file://foreign-host/project')


def test_atomic_publication_preserves_utf8_and_releases_handles(tmp_path):
    from seywo.runtime_platform import replace_file
    target, incoming = tmp_path / '目标.json', tmp_path / '新数据.json'
    target.write_bytes(b'old')
    incoming.write_bytes('中文'.encode('utf-8'))
    replace_file(incoming, target)
    assert target.read_bytes() == '中文'.encode('utf-8') and not incoming.exists()
    target.unlink()
