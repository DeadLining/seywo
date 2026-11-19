"""Windows kernel primitives, loaded only on Windows (no external dependency)."""
import ctypes as c
from ctypes import wintypes as w
import msvcrt

kernel = c.WinDLL('kernel32', use_last_error=True)
ULONG_PTR = c.c_size_t
SIZE_T = c.c_size_t


class OVERLAPPED(c.Structure):
    _fields_ = [('Internal', ULONG_PTR), ('InternalHigh', ULONG_PTR),
                ('Offset', w.DWORD), ('OffsetHigh', w.DWORD), ('hEvent', w.HANDLE)]


class BASIC_LIMIT(c.Structure):
    _fields_ = [('PerProcessUserTimeLimit', c.c_longlong), ('PerJobUserTimeLimit', c.c_longlong),
                ('LimitFlags', w.DWORD), ('MinimumWorkingSetSize', SIZE_T),
                ('MaximumWorkingSetSize', SIZE_T), ('ActiveProcessLimit', w.DWORD),
                ('Affinity', ULONG_PTR), ('PriorityClass', w.DWORD), ('SchedulingClass', w.DWORD)]


class IO_COUNTERS(c.Structure):
    _fields_ = [(name, c.c_ulonglong) for name in ('ReadOperationCount', 'WriteOperationCount',
        'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]


class EXTENDED_LIMIT(c.Structure):
    _fields_ = [('BasicLimitInformation', BASIC_LIMIT), ('IoInfo', IO_COUNTERS),
                ('ProcessMemoryLimit', SIZE_T), ('JobMemoryLimit', SIZE_T),
                ('PeakProcessMemoryUsed', SIZE_T), ('PeakJobMemoryUsed', SIZE_T)]


class THREADENTRY32(c.Structure):
    _fields_ = [('dwSize', w.DWORD), ('cntUsage', w.DWORD), ('th32ThreadID', w.DWORD),
                ('th32OwnerProcessID', w.DWORD), ('tpBasePri', w.LONG),
                ('tpDeltaPri', w.LONG), ('dwFlags', w.DWORD)]


def api(name, result, args):
    fn = getattr(kernel, name)
    fn.restype, fn.argtypes = result, args
    return fn


_lock = api('LockFileEx', w.BOOL, [w.HANDLE, w.DWORD, w.DWORD, w.DWORD, w.DWORD, c.POINTER(OVERLAPPED)])
_unlock = api('UnlockFileEx', w.BOOL, [w.HANDLE, w.DWORD, w.DWORD, w.DWORD, c.POINTER(OVERLAPPED)])
_close = api('CloseHandle', w.BOOL, [w.HANDLE])
_create_job = api('CreateJobObjectW', w.HANDLE, [c.c_void_p, w.LPCWSTR])
_set_job = api('SetInformationJobObject', w.BOOL, [w.HANDLE, c.c_int, c.c_void_p, w.DWORD])
_assign = api('AssignProcessToJobObject', w.BOOL, [w.HANDLE, w.HANDLE])
_open_process = api('OpenProcess', w.HANDLE, [w.DWORD, w.BOOL, w.DWORD])
_snapshot = api('CreateToolhelp32Snapshot', w.HANDLE, [w.DWORD, w.DWORD])
_first = api('Thread32First', w.BOOL, [w.HANDLE, c.POINTER(THREADENTRY32)])
_next = api('Thread32Next', w.BOOL, [w.HANDLE, c.POINTER(THREADENTRY32)])
_open_thread = api('OpenThread', w.HANDLE, [w.DWORD, w.BOOL, w.DWORD])
_resume = api('ResumeThread', w.DWORD, [w.HANDLE])


def checked(value):
    if not value:
        raise c.WinError(c.get_last_error())
    return value


def lock_file(fd, *, exclusive, blocking):
    flags = (2 if exclusive else 0) | (0 if blocking else 1)
    overlap = OVERLAPPED()
    if not _lock(msvcrt.get_osfhandle(fd), flags, 0, 1, 0, c.byref(overlap)):
        error = c.get_last_error()
        if not blocking and error == 33:  # ERROR_LOCK_VIOLATION
            return False
        raise c.WinError(error)
    return True


def unlock_file(fd):
    checked(_unlock(msvcrt.get_osfhandle(fd), 0, 1, 0, c.byref(OVERLAPPED())))


class Job:
    def __init__(self):
        self.handle = checked(_create_job(None, None))
        limits = EXTENDED_LIMIT()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        try:
            checked(_set_job(self.handle, 9, c.byref(limits), c.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    def assign_and_resume(self, pid):
        # Suspended process has no descendants yet. Fail closed before resuming.
        process = checked(_open_process(0x0100 | 0x0001, False, pid))
        try:
            checked(_assign(self.handle, process))
        finally:
            _close(process)
        snapshot = _snapshot(4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == w.HANDLE(-1).value:
            raise c.WinError(c.get_last_error())
        try:
            entry = THREADENTRY32()
            entry.dwSize = c.sizeof(entry)
            more = _first(snapshot, c.byref(entry))
            while more:
                if entry.th32OwnerProcessID == pid:
                    thread = checked(_open_thread(2, False, entry.th32ThreadID))
                    try:
                        if _resume(thread) == 0xFFFFFFFF:
                            raise c.WinError(c.get_last_error())
                    finally:
                        _close(thread)
                    return
                more = _next(snapshot, c.byref(entry))
            raise OSError('suspended_process_thread_missing')
        finally:
            _close(snapshot)

    def close(self):
        if self.handle:
            checked(_close(self.handle))
            self.handle = None


class DATA_BLOB(c.Structure):
    _fields_ = [('cbData', w.DWORD), ('pbData', c.POINTER(c.c_ubyte))]


crypt = c.WinDLL('crypt32', use_last_error=True)
_protect = crypt.CryptProtectData
_protect.restype = w.BOOL
_protect.argtypes = [c.POINTER(DATA_BLOB), w.LPCWSTR, c.POINTER(DATA_BLOB),
                    c.c_void_p, c.c_void_p, w.DWORD, c.POINTER(DATA_BLOB)]
_reveal = crypt.CryptUnprotectData
_reveal.restype = w.BOOL
_reveal.argtypes = [c.POINTER(DATA_BLOB), c.c_void_p, c.POINTER(DATA_BLOB),
                   c.c_void_p, c.c_void_p, w.DWORD, c.POINTER(DATA_BLOB)]
_free = api('LocalFree', w.HANDLE, [w.HANDLE])


def _secret(data, entropy, *, encrypt):
    incoming = DATA_BLOB(len(data), c.cast(c.create_string_buffer(data), c.POINTER(c.c_ubyte)))
    extra = DATA_BLOB(len(entropy), c.cast(c.create_string_buffer(entropy), c.POINTER(c.c_ubyte)))
    outgoing = DATA_BLOB()
    function = _protect if encrypt else _reveal
    checked(function(c.byref(incoming), None, c.byref(extra), None, None, 1, c.byref(outgoing)))
    try:
        return c.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        # Wipe decrypted copies allocated by Windows before releasing them.
        c.memset(outgoing.pbData, 0, outgoing.cbData)
        _free(c.cast(outgoing.pbData, w.HANDLE))


def protect_secret(data, entropy):
    return _secret(data, entropy, encrypt=True)


def reveal_secret(data, entropy):
    return _secret(data, entropy, encrypt=False)


_move = api('MoveFileExW', w.BOOL, [w.LPCWSTR, w.LPCWSTR, w.DWORD])


def replace_file(source, target):
    checked(_move(str(source), str(target), 0x00000001 | 0x00000008))
