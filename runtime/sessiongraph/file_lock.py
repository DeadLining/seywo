"""Process file locks with the same flock-style calls on Unix and Windows."""

from __future__ import annotations

import os

if os.name != "nt":
    from fcntl import LOCK_EX, LOCK_NB, LOCK_SH, LOCK_UN, flock
else:
    import ctypes
    import msvcrt
    import threading
    import weakref
    from ctypes import wintypes

    LOCK_SH = 1
    LOCK_EX = 2
    LOCK_NB = 4
    LOCK_UN = 8

    _EXCLUSIVE = 0x00000002
    _FAIL_IMMEDIATELY = 0x00000001
    _LOCK_VIOLATION = 33

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _lock_file = _kernel32.LockFileEx
    _lock_file.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped),
    ]
    _lock_file.restype = wintypes.BOOL
    _unlock_file = _kernel32.UnlockFileEx
    _unlock_file.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, ctypes.POINTER(_Overlapped),
    ]
    _unlock_file.restype = wintypes.BOOL
    _held = weakref.WeakKeyDictionary()
    _guard = threading.Lock()

    def flock(stream, operation: int) -> None:
        """Lock byte zero; closing the stream also releases the OS lock."""
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
        if operation & LOCK_UN:
            with _guard:
                overlap = _held.pop(stream, None)
            if overlap is None:
                return
            if not _unlock_file(handle, 0, 1, 0, ctypes.byref(overlap)):
                raise ctypes.WinError(ctypes.get_last_error())
            return
        if not operation & (LOCK_SH | LOCK_EX):
            raise ValueError("expected a shared or exclusive file lock")
        with _guard:
            if stream in _held:
                raise ValueError("file lock already held")
        overlap = _Overlapped()
        flags = (_EXCLUSIVE if operation & LOCK_EX else 0)
        if operation & LOCK_NB:
            flags |= _FAIL_IMMEDIATELY
        if not _lock_file(handle, flags, 0, 1, 0, ctypes.byref(overlap)):
            code = ctypes.get_last_error()
            if code == _LOCK_VIOLATION:
                raise BlockingIOError(code, "file lock is held")
            raise ctypes.WinError(code)
        with _guard:
            _held[stream] = overlap
