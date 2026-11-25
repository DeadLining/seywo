"""Streaming content-defined boundaries; no native dependency or model work.

Candidate boundaries are punctuation/newline occurrences. A rolling local-window
fingerprint selects anchors independently of file offsets, resynchronizing after
insertions in nested histories. Minimum/maximum sizes bound metadata and work.
"""
import re
import zlib

MIN_BYTES = 32 * 1024
MAX_BYTES = 256 * 1024
CANDIDATE = re.compile(rb'\n|\\n|[{},]')


def _cut(data):
    for match in CANDIDATE.finditer(data, MIN_BYTES, min(len(data), MAX_BYTES)):
        end = match.end()
        if zlib.adler32(data[end - 64:end]) & 511 == 0:
            return end
    return min(len(data), MAX_BYTES)


def chunks(handle):
    parts = iter(lambda: handle.read(MAX_BYTES), b'') if hasattr(handle, 'read') else iter(handle)
    buffer = b''
    for part in parts:
        for start in range(0, len(part), MAX_BYTES):
            buffer += part[start:start + MAX_BYTES]
            while len(buffer) >= MAX_BYTES:
                end = _cut(buffer)
                yield buffer[:end]
                buffer = buffer[end:]
    while buffer:
        end = _cut(buffer)
        yield buffer[:end]
        buffer = buffer[end:]
