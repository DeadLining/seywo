"""Dependency-aware reclamation after publication, never on the model hot path."""
import json
from pathlib import Path
import sqlite3
import time

from . import archive
from .config import _atomic_write
from .locking import file_lock, try_exclusive

MAX_ORPHAN_BYTES = 8 * 1024 * 1024
MAX_ORPHAN_BLOCKS = 32
MIN_VACUUM_BYTES = 32 * 1024 * 1024


def collect_database(db):
    """Reclaim only data unreachable from durable identities, including delta bases."""
    from .projection import search_tokens
    counts = {}
    orphans = db.execute('SELECT id,data FROM source_payloads p WHERE NOT EXISTS(SELECT 1 FROM fragment_records f WHERE f.payload_id=p.id)').fetchall()
    for pid, data in orphans:
        if db.execute('SELECT 1 FROM search_documents WHERE id=?', (pid,)).fetchone():
            text = db.execute('SELECT seywo_source_text(?)', (data,)).fetchone()[0]
            db.execute("INSERT INTO source_fts(source_fts,rowid,tokens) VALUES('delete',?,?)", (pid, search_tokens(text)))
            db.execute('DELETE FROM search_documents WHERE id=?', (pid,))
        db.execute('DELETE FROM source_payloads WHERE id=?', (pid,))
    counts['removed_payloads'] = len(orphans)
    before = db.total_changes
    db.execute('DELETE FROM fragment_descriptors WHERE NOT EXISTS(SELECT 1 FROM fragment_records f WHERE f.descriptor_id=fragment_descriptors.id)')
    counts['removed_descriptors'] = db.total_changes - before
    before = db.total_changes
    db.execute('''WITH RECURSIVE needed(hash) AS (
        SELECT request_json FROM task_records UNION SELECT result_json FROM task_records WHERE result_json IS NOT NULL
        UNION SELECT d.base_hash FROM execution_documents d JOIN needed n ON n.hash=d.hash WHERE d.base_hash IS NOT NULL)
        DELETE FROM execution_documents WHERE hash NOT IN (SELECT hash FROM needed)''')
    counts['removed_documents'] = db.total_changes - before
    return counts


def _receipt(config, result):
    path = Path(config['_path']).parent / 'maintenance/storage.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps({**result, 'updated_at': time.time()}, sort_keys=True) + '\n')


def _validate_database_archives(db):
    """Validate every durable archive locator before any reclaim operation."""
    rows = db.execute(
        'SELECT a.path,ss.sha256,ss.byte_count '
        'FROM source_archives a JOIN source_snapshots ss ON ss.id=a.snapshot_id'
    ).fetchall()
    for row in rows:
        path = Path(row['path'])
        if path.is_symlink() or not path.is_file():
            raise ValueError('registered source archive missing')
        if archive.source_digest(path) != (row['sha256'], row['byte_count']):
            raise ValueError('registered source archive corrupt')


def status(config):
    try:
        path = Path(config['_path']).parent / 'maintenance/storage.json'
        with path.open('rb') as f:
            data = json.loads(f.read(8193))
        if not isinstance(data, dict) or not isinstance(data.get('state'), str) or data['state'] not in {'done', 'failed', 'deferred'}:
            raise ValueError('invalid storage receipt')
        return {k: data[k] for k in ('state', 'updated_at', 'error_code') if k in data}
    except FileNotFoundError:
        return {'state': 'idle'}
    except (OSError, ValueError, TypeError):
        return {'state': 'failed', 'error_code': 'storage_maintenance_failed'}


def collect(config, *, force=False, reclaim_blocks=False, retire_dialogue=False):
    """Exclusive, nonblocking by default; no deletion can race a writer or Hook."""
    state = Path(config['_path']).parent
    for path in (state, state / 'inbox', *(state / 'inbox' / name for name in ('pending', 'live', 'objects')), state / 'maintenance'):
        if path.is_symlink():
            raise ValueError('symlinked storage directory')
    with try_exclusive(state / 'storage-migration.lock', blocking=force) as acquired:
        if not acquired:
            return {'state': 'deferred', 'reason': 'active_writer'}
        from .store import Store
        inbox = state / 'inbox'
        result = {'state': 'done', 'removed_manifests': 0, 'removed_blocks': 0, 'model_calls': 0}
        with file_lock(inbox / '.lock'), file_lock(inbox / '.archive.lock'):
            with Store(config) as store:
                # A missing/corrupt database-owned archive means the reference
                # graph is no longer trustworthy.  Stop before deleting any
                # manifest, block, database row, or vacuumed page.
                _validate_database_archives(store.db)
                bound = set()
                deleted = {r[0] for r in store.db.execute('SELECT chat_id FROM tombstones')}
                # Parse/validate every pending record before deleting anything.
                pending = []
                for path in (inbox / 'pending').glob('*.json'):
                    if path.is_symlink():
                        raise ValueError('symlinked pending source record')
                    item = json.loads(path.read_text())
                    if (not isinstance(item, dict)
                            or not isinstance(item.get('state'), str)
                            or item['state'] not in {'pending', 'claimed', 'failed', 'done'}
                            or not isinstance(item.get('chat_id'), str) or not item['chat_id']
                            or (item.get('snapshot') is not None and not isinstance(item['snapshot'], str))):
                        raise ValueError('ambiguous pending source reference')
                    pending.append((path, item))
                    if item.get('chat_id') not in deleted and item.get('state') != 'done' and item.get('snapshot'):
                        bound.add(str(Path(item['snapshot']).resolve()))
                # Validate all manifests before removing any reference owner.
                manifests = []
                for path in (inbox / 'live').glob('*.jsonl'):
                    if path.is_symlink():
                        raise ValueError('symlinked source manifest')
                    with path.open('rb') as f:
                        manifest = archive._manifest(f)
                    if manifest is None:
                        # Raw inputs are not managed archive manifests.
                        bound.add(str(path.resolve()))
                    manifests.append((path, manifest))
                with store.db:
                    result.update(collect_database(store.db))
                    if retire_dialogue:
                        for path, manifest in manifests:
                            if (manifest is not None and manifest.get('format') == archive.DIALOGUE_FORMAT
                                    and str(path.resolve()) not in bound):
                                store.db.execute('DELETE FROM source_archives WHERE path=?', (str(path.resolve()),))
                bound.update(str(Path(r[0]).resolve()) for r in store.db.execute('SELECT path FROM source_archives'))
                # A retired dialogue locator remains a segment identity, not a
                # file dependency. Legacy native segment files stay protected.
                dialogue_paths = {str(path.resolve()) for path, manifest in manifests
                                  if manifest is not None and manifest.get('format') == archive.DIALOGUE_FORMAT}
                bound.update(str(Path(r[0]).resolve()) for r in store.db.execute('SELECT path FROM source_segments')
                             if str(Path(r[0]).resolve()) not in dialogue_paths)
                for path, item in pending:
                    if item.get('chat_id') in deleted:
                        path.unlink()
                for folder in (state / 'maintain', state / 'maintenance'):
                    if folder.is_symlink():
                        raise ValueError('symlinked chat cache directory')
                    for path in folder.glob('*.json'):
                        if path.stem in deleted:
                            path.unlink()
                for path, manifest in manifests:
                    if manifest is not None and str(path.resolve()) not in bound:
                        path.unlink()
                        result['removed_manifests'] += 1
                refs = {chunk for path, manifest in manifests if path.exists() and manifest is not None for chunk in manifest['chunks']}
                blocks = list((inbox / 'objects').glob('*.z'))
                orphan = [p for p in blocks if p.stem not in refs]
                result['pending_block_bytes'] = sum(p.stat().st_size for p in orphan)
                if orphan and (force or reclaim_blocks or retire_dialogue or len(orphan) >= MAX_ORPHAN_BLOCKS or result['pending_block_bytes'] >= MAX_ORPHAN_BYTES):
                    result.update(archive.garbage_collect(inbox))
                    result['pending_block_bytes'] = 0
                free, total, page = [store.db.execute('PRAGMA ' + k).fetchone()[0] for k in ('freelist_count', 'page_count', 'page_size')]
                result['free_database_bytes'] = free * page
                if (force and free) or (free * page >= MIN_VACUUM_BYTES and free >= total // 5):
                    store.db.execute('VACUUM')
                    result['free_database_bytes'] = 0
        _receipt(config, result)
        return result


def safe_collect(config, *, reclaim_blocks=False, retire_dialogue=False):
    try:
        return collect(config, reclaim_blocks=reclaim_blocks, retire_dialogue=retire_dialogue)
    except (OSError, ValueError, sqlite3.Error):
        result = {'state': 'failed', 'error_code': 'storage_maintenance_failed', 'model_calls': 0}
        try:
            _receipt(config, result)
        except OSError:
            pass
        return result
