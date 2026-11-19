"""Local filesystem paths -> SQLite URIs, independent of Windows namespace syntax."""
from pathlib import Path, PurePath, PureWindowsPath
import re
from urllib.parse import quote


def sqlite_uri(path: PurePath, *, mode: str) -> str:
    """Open an existing database without putting Windows prefixes in the authority.

    pathlib treats ``\\\\?\\`` as a UNC authority when producing a file URI.
    SQLite instead needs a drive path, or a UNC path with an empty authority.
    Escape filename punctuation before adding our sole, allowlisted URI option.
    Filesystem checks and symlink policy remain the caller's responsibility.
    """
    if mode not in {'ro', 'rw'}:
        raise ValueError('SQLite URI requires mode ro or rw')
    if not isinstance(path, PurePath):
        path = Path(path)
    if not path.is_absolute():
        raise ValueError('SQLite database path must be absolute')
    if isinstance(path, PureWindowsPath):
        raw = str(path)
        if raw[:8].lower() == '\\\\?\\unc\\':
            raw = '\\\\' + raw[8:]
        elif raw.startswith('\\\\?\\'):
            raw = raw[4:]
            if not re.match(r'^[a-zA-Z]:[\\/]', raw):
                raise ValueError('Unsupported Windows database namespace')
        if raw.startswith('\\\\.\\'):
            raise ValueError('Windows device paths are not database paths')
        native = PureWindowsPath(raw)
        if not native.is_absolute():
            raise ValueError('SQLite database path must be absolute')
        if re.fullmatch('[a-zA-Z]:', native.drive):
            uri = 'file:///' + quote(native.as_posix(), safe='/:')
        elif native.drive.startswith('\\\\'):
            # SQLite rejects non-local URI authorities by default. Keep the UNC
            # server/share in the path so no special SQLite build flag is needed.
            uri = 'file://' + quote(native.as_posix(), safe='/:')
        else:
            raise ValueError('Unsupported Windows database namespace')
    else:
        uri = path.as_uri()
    return uri + '?mode=' + mode
