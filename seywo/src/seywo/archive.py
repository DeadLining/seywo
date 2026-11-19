"""Exact raw sources: immutable compressed blocks and atomic small manifests.

Only managed captures are manifests. Native files remain untouched. The manifest
filename is a source locator; byte offsets always refer to the logical raw file.
"""

from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import zlib
from uuid import uuid4

from .runtime_platform import replace_file, sync_directory

from .locking import file_lock

BLOCK_BYTES = 256 * 1024
MAX_MANIFEST = 1024 * 1024
HASH = re.compile(r"[a-f0-9]{64}")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _objects(path):
    # Never resolve an arbitrary path supplied inside a manifest.
    folder = path.parent.parent / "objects"
    if path.parent.name != "live" or path.parent.parent.name != "inbox":
        raise ValueError("archive outside managed inbox")
    if folder.is_symlink():
        raise ValueError("archive objects must not be a symlink")
    return folder


def _atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("archive must not be a symlink")
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".archive-", delete=False) as f:
            name = Path(f.name)
            os.chmod(name, 0o600)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        replace_file(name, path)
        sync_directory(path.parent)
    finally:
        if name is not None:
            name.unlink(missing_ok=True)


def finalize(temporary, target):
    """Rename under publication ownership, then persist the final locator."""
    replace_file(temporary, target)
    sync_directory(Path(target).parent)


def _decode(blob):
    decoder = zlib.decompressobj()
    data = decoder.decompress(blob, BLOCK_BYTES + 1)
    if len(data) > BLOCK_BYTES or not decoder.eof or decoder.unused_data:
        raise ValueError("invalid archive block")
    return data


def _block(folder, identity):
    if not isinstance(identity, str) or not HASH.fullmatch(identity):
        raise ValueError("invalid archive block identity")
    path = folder / (identity + ".z")
    if path.is_symlink():
        raise ValueError("archive block must not be a symlink")
    if path.stat().st_size > BLOCK_BYTES + 1024:
        raise ValueError("oversized archive block")
    data = _decode(path.read_bytes())
    if _sha(data) != identity:
        raise ValueError("archive block hash mismatch")
    return data


class _Chunks(io.RawIOBase):
    def __init__(self, handle, path, manifest):
        self.handle, self.folder = handle, _objects(path)
        self.manifest, self.index, self.buffer = manifest, 0, b""
        self.digest, self.size, self.verified = hashlib.sha256(), 0, False

    def readable(self):
        return True

    def fileno(self):
        return self.handle.fileno()

    def readinto(self, target):
        if not self.buffer:
            chunks = self.manifest["chunks"]
            if self.index == len(chunks):
                if not self.verified:
                    if self.size != self.manifest["bytes"] or self.digest.hexdigest() != self.manifest["sha256"]:
                        raise ValueError("archive source digest mismatch")
                    self.verified = True
                return 0
            data = _block(self.folder, chunks[self.index])
            self.index += 1
            self.digest.update(data)
            self.size += len(data)
            self.buffer = data
        n = min(len(target), len(self.buffer))
        target[:n], self.buffer = self.buffer[:n], self.buffer[n:]
        return n

    def close(self):
        self.handle.close()
        super().close()


def _manifest(handle):
    """Recognize archive metadata regardless of JSON whitespace/key order."""
    first = handle.readline(MAX_MANIFEST + 1)
    handle.seek(0)
    try:
        record = json.loads(first)
    except (ValueError, UnicodeError):
        record = None
    if isinstance(record, dict) and "seywo_archive" not in record:
        return None
    if record is None and not re.search(rb'"seywo_archive"\s*:', first):
        # Pretty-printed metadata may put the key on a later line.
        if first.strip() != b"{" or os.fstat(handle.fileno()).st_size > MAX_MANIFEST:
            return None
    if os.fstat(handle.fileno()).st_size > MAX_MANIFEST:
        raise ValueError("oversized archive manifest")
    manifest = record if record is not None else json.load(handle)
    if (not isinstance(manifest, dict)
            or set(manifest) != {"seywo_archive", "bytes", "sha256", "chunks"}
            or type(manifest["seywo_archive"]) is not int or manifest["seywo_archive"] != 1
            or type(manifest["bytes"]) is not int or manifest["bytes"] < 0
            or not isinstance(manifest["sha256"], str) or not HASH.fullmatch(manifest["sha256"])
            or not isinstance(manifest["chunks"], list) or len(manifest["chunks"]) > 4096
            or any(not isinstance(c, str) or not HASH.fullmatch(c) for c in manifest["chunks"])):
        raise ValueError("invalid archive manifest")
    return manifest


def open_source(path):
    """Open a native JSONL or managed manifest as a bounded binary stream."""
    path = Path(path)
    handle = path.open("rb")
    try:
        manifest = _manifest(handle)
        if manifest is None:
            return handle
        return io.BufferedReader(_Chunks(handle, path, manifest))
    except BaseException:
        handle.close()
        raise


def archived(path):
    with Path(path).open("rb") as f:
        return _manifest(f) is not None


@contextmanager
def publication(target):
    """Own blocks and all manifest renames until publication is durable."""
    target = Path(target)
    _objects(target)
    with file_lock(target.parent.parent / ".archive.lock"):
        yield


def capture(source, target, *, limit=None):
    with publication(target):
        return _capture(source, target, limit=limit)


def _capture(source, target, *, limit=None):
    """Stream once; publish only after every referenced raw block is durable."""
    source, target = Path(source), Path(target)
    folder = _objects(target)
    if target.is_symlink():
        raise ValueError("archive must not be a symlink")
    if target.exists():
        with target.open("rb") as handle:
            previous = _manifest(handle)
        if (previous is None
                or source_digest(target) != (previous["sha256"], previous["bytes"])
                or source_digest(source) != (previous["sha256"], previous["bytes"])):
            raise ValueError("immutable archive cannot be overwritten")
        return previous
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    size, digest, chunks = 0, hashlib.sha256(), []
    with open_source(source) as incoming:
        before = os.fstat(incoming.fileno())
        while True:
            data = incoming.read(BLOCK_BYTES)
            if not data:
                break
            size += len(data)
            if limit is not None and size > limit:
                raise ValueError("snapshot_too_large")
            digest.update(data)
            identity = _sha(data)
            block = folder / (identity + ".z")
            if block.exists():
                # Verify reusable data rather than trusting a filename alone.
                if _block(folder, identity) != data:
                    raise ValueError("archive block collision")
            else:
                _atomic(block, zlib.compress(data, level=3))
            chunks.append(identity)
            if len(chunks) > 4096:
                raise ValueError("oversized archive manifest")
        after = os.fstat(incoming.fileno())
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("changing archive source")
    manifest = {"seywo_archive": 1, "bytes": size, "sha256": digest.hexdigest(), "chunks": chunks}
    body = json.dumps(manifest, separators=(",", ":")).encode()
    if len(body) > MAX_MANIFEST:
        raise ValueError("oversized archive manifest")
    _atomic(target, body)
    return manifest


def content_capture(source, folder, prefix):
    """Canonical immutable locator, independent of legacy capture filenames."""
    source, folder = Path(source).resolve(), Path(folder).resolve()
    temporary = folder / f".{prefix}-{uuid4().hex}.jsonl"
    with publication(temporary):
        try:
            with source.open("rb") as handle:
                existing = _manifest(handle)
            if existing is not None and source.parent == folder:
                if source_digest(source) != (existing["sha256"], existing["bytes"]):
                    raise ValueError("archive identity mismatch")
                manifest = existing
            else:
                manifest = _capture(source, temporary)
            target = folder / f"{prefix}-{manifest['sha256']}.jsonl"
            if target.exists():
                if target != source and source_digest(target) != (manifest["sha256"], manifest["bytes"]):
                    raise ValueError("immutable capture locator collision")
            elif temporary.exists():
                finalize(temporary, target)
            else:
                _atomic(target, json.dumps(manifest, separators=(",", ":")).encode())
            return target, manifest
        finally:
            temporary.unlink(missing_ok=True)


def source_digest(path):
    digest, size = hashlib.sha256(), 0
    with open_source(path) as f:
        while data := f.read(BLOCK_BYTES):
            digest.update(data)
            size += len(data)
    return digest.hexdigest(), size

def inspect_archives(inbox):
    """Validate the complete reference set before any destructive operation."""
    inbox = Path(inbox)
    if inbox.name != "inbox" or inbox.is_symlink():
        raise ValueError("invalid archive inbox")
    referenced = set()
    for path in sorted((inbox / "live").glob("*.jsonl")):
        if path.is_symlink():
            raise ValueError("archive must not be a symlink")
        with path.open("rb") as handle:
            manifest = _manifest(handle)
            if manifest is None:
                # Legacy native captures must positively identify a JSONL record.
                # Unknown/corrupt metadata cannot prove which blocks are unused.
                record = json.loads(handle.readline(16 * 1024 * 1024 + 1))
                if not isinstance(record, dict) or not isinstance(record.get("type"), str):
                    raise ValueError("unrecognized live source")
                continue
        source_digest(path)  # validates missing blocks, full size and full digest
        referenced.update(manifest["chunks"])
    objects = inbox / "objects"
    if objects.is_symlink():
        raise ValueError("archive objects must not be a symlink")
    blocks = list(objects.glob("*.z"))
    if any(p.is_symlink() or not HASH.fullmatch(p.stem) for p in blocks):
        raise ValueError("invalid archive object")
    return referenced, blocks


def garbage_collect(inbox):
    """Fail closed and serialize reference validation/deletion with publishers."""
    inbox = Path(inbox)
    with file_lock(inbox / ".archive.lock"):
        referenced, blocks = inspect_archives(inbox)
        unused = [p for p in blocks if p.stem not in referenced]
        for block in unused:
            block.unlink()
        return {"referenced_blocks": len(referenced), "removed_blocks": len(unused)}
