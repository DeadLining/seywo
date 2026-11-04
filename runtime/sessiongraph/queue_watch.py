"""Wait for a continuation queue write or its next retry deadline."""

from __future__ import annotations

import os
from pathlib import Path

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _first = _kernel32.FindFirstChangeNotificationW
    _first.argtypes = [wintypes.LPCWSTR, wintypes.BOOL, wintypes.DWORD]
    _first.restype = wintypes.HANDLE
    _next = _kernel32.FindNextChangeNotification
    _next.argtypes = [wintypes.HANDLE]
    _next.restype = wintypes.BOOL
    _close = _kernel32.FindCloseChangeNotification
    _close.argtypes = [wintypes.HANDLE]
    _close.restype = wintypes.BOOL
    _wait = _kernel32.WaitForSingleObject
    _wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _wait.restype = wintypes.DWORD
    _INVALID = ctypes.c_void_p(-1).value

    class QueueWatcher:
        def __init__(self, folder: Path):
            self.handle = _first(str(folder), False, 0x00000001 | 0x00000010)
            if self.handle == _INVALID:
                raise ctypes.WinError(ctypes.get_last_error())

        def wait(self, seconds: float) -> None:
            result = _wait(self.handle, min(max(0, int(seconds * 1000)), 0xFFFFFFFE))
            if result == 0:
                if not _next(self.handle):
                    raise ctypes.WinError(ctypes.get_last_error())
            elif result != 0x102:
                raise ctypes.WinError(ctypes.get_last_error())

        def close(self) -> None:
            if not _close(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())

else:
    import select

    class QueueWatcher:
        def __init__(self, folder: Path):
            self.fd = os.open(folder, os.O_RDONLY)
            self.watcher = select.kqueue()
            self.watcher.control([
                select.kevent(self.fd, filter=select.KQ_FILTER_VNODE,
                              flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                              fflags=select.KQ_NOTE_WRITE)
            ], 0, 0)

        def wait(self, seconds: float) -> None:
            self.watcher.control(None, 1, seconds)

        def close(self) -> None:
            self.watcher.close()
            os.close(self.fd)
