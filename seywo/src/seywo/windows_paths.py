"""Pure conversion between drive/share spellings; never opens a filesystem path."""
from pathlib import PureWindowsPath
import re


def native_windows_path(path: PureWindowsPath) -> PureWindowsPath:
    raw = str(path)
    if raw[:8].lower() == '\\\\?\\unc\\':
        raw = '\\\\' + raw[8:]
    elif raw.startswith('\\\\?\\'):
        raw = raw[4:]
        if not re.match(r'^[A-Za-z]:\\', raw):
            raise ValueError('unsupported Windows path namespace')
    native = PureWindowsPath(raw)
    if (not native.is_absolute() or native.drive.startswith(('\\\\.\\', '\\\\?\\'))
            or not (re.fullmatch('[A-Za-z]:', native.drive) or native.drive.startswith('\\\\'))):
        raise ValueError('expected an absolute Windows drive or share path')
    return native


def extended_windows_path(path: PureWindowsPath) -> PureWindowsPath:
    native = native_windows_path(path)
    raw = str(native)
    return PureWindowsPath('\\\\?\\UNC\\' + raw[2:] if native.drive.startswith('\\\\') else '\\\\?\\' + raw)
