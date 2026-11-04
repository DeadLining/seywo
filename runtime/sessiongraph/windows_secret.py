"""Protect local Windows secrets for the current user with DPAPI."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

if os.name != "nt":
    raise RuntimeError("Windows secret protection is Windows-only")


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_protect = _crypt32.CryptProtectData
_protect.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                     ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                     ctypes.POINTER(_Blob)]
_protect.restype = wintypes.BOOL
_unprotect = _crypt32.CryptUnprotectData
_unprotect.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                       ctypes.POINTER(_Blob)]
_unprotect.restype = wintypes.BOOL
_free = _kernel32.LocalFree
_free.argtypes = [ctypes.c_void_p]
_free.restype = ctypes.c_void_p
_NO_UI = 0x1


def _input_blob(data: bytes):
    buffer = ctypes.create_string_buffer(data)
    blob = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    return buffer, blob


def protect(data: bytes) -> bytes:
    buffer, source = _input_blob(data)
    result = _Blob()
    if not _protect(ctypes.byref(source), "SessionGraph", None, None, None,
                    _NO_UI, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        _free(ctypes.cast(result.data, ctypes.c_void_p))


def unprotect(data: bytes) -> bytes:
    buffer, source = _input_blob(data)
    result = _Blob()
    if not _unprotect(ctypes.byref(source), None, None, None, None,
                      _NO_UI, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        _free(ctypes.cast(result.data, ctypes.c_void_p))
