"""Reconcile verified desktop deletions without scanning transcripts or using a model."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import time

from . import inbox
from .config import _atomic_write, load_config
from .locking import file_lock, try_exclusive
from .plugin_context import catalog_home
from .sqlite_paths import sqlite_uri
from .store import Store, UnsupportedStorage

PREFIX = 'codex-writing-block-deleted-thread-v1:'
CHAT = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')
MAX_LEGACY_BYTES = 8 * 1024 * 1024


def available(home):
    return ((home / 'state_5.sqlite').is_file()
            and ((home / '.codex-global-state.sqlite').is_file()
                 or (home / '.codex-global-state.json').is_file()))


def _read_database(path):
    return sqlite3.connect(sqlite_uri(path.absolute(), mode='ro'), uri=True, timeout=0.5)


def _markers(home):
    desktop = home / '.codex-global-state.sqlite'
    if desktop.exists():
        with closing(_read_database(desktop)) as db:
            keys = [k for (k,) in db.execute(
                "SELECT DISTINCT key FROM settings WHERE scope GLOB 'electron-owner-atoms:*' "
                "AND key GLOB ? AND value='true'", (PREFIX + '*',))]
    else:
        with (home / '.codex-global-state.json').open('rb') as stream:
            body = stream.read(MAX_LEGACY_BYTES + 1)
        if len(body) > MAX_LEGACY_BYTES:
            raise ValueError('legacy deletion ledger exceeds limit')
        atoms = json.loads(body)['electron-persisted-atom-state']
        if not isinstance(atoms, dict):
            raise ValueError('invalid deletion ledger')
        keys = [k for k, value in atoms.items() if k.startswith(PREFIX) and value is True]
    return {k[len(PREFIX):] for k in keys if CHAT.fullmatch(k[len(PREFIX):])}


def reconcile(config, home):
    """Missing, malformed or busy host stores never authorize deletion."""
    result = {'state': 'done', 'deleted': 0, 'model_calls': 0}
    state = Path(config['_path']).parent
    reclaim = state / 'deletion-reclaim.pending'
    if (state / 'disabled').exists() or (config.get('hook') or {}).get('enabled') is not True:
        return {**result, 'state': 'disabled'}
    try:
        markers = _markers(home)
        if not markers and not reclaim.is_file():
            return result
        with try_exclusive(state / 'storage-migration.lock') as acquired:
            if not acquired:
                return {**result, 'state': 'deferred'}
            with file_lock(inbox.root(config) / '.lock'), Store(config) as store:
                # Only indexed or queued chats in this project are eligible.
                # Never add tombstones for unrelated entries in the host ledger.
                known = set()
                for chat in markers:
                    if store.db.execute('SELECT 1 FROM chats WHERE id=?', (chat,)).fetchone():
                        known.add(chat)
                queued = set()
                for path in (inbox.root(config) / 'pending').glob('*.json'):
                    item = inbox._read(path)
                    if item and item.get('chat_id') in markers:
                        queued.add(item['chat_id'])
                candidates = sorted(markers & (known | queued))
                if candidates:
                    with closing(_read_database(home / 'state_5.sqlite')) as db:
                        db.execute('BEGIN')
                        # An archived chat still has a row and is preserved.
                        deleted = [chat for chat in candidates if not db.execute(
                            'SELECT 1 FROM threads WHERE id=?', (chat,)).fetchone()]
                    if deleted:
                        _atomic_write(reclaim, 'pending\n')
                        store._delete_chats(deleted)
                        result['deleted'] = len(deleted)
            if reclaim.is_file():
                from .storage_lifecycle import safe_collect
                result['storage'] = safe_collect(config, reclaim_blocks=True)
                if result['storage']['state'] == 'done':
                    reclaim.unlink(missing_ok=True)
                else:
                    result['state'] = result['storage']['state']
        return result
    except UnsupportedStorage:
        return {**result, 'state': 'deferred'}
    except (OSError, ValueError, KeyError, sqlite3.Error):
        return {**result, 'state': 'unavailable'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--settle', action='store_true', help='Allow desktop state to settle after SessionEnd')
    args = parser.parse_args()
    config, home = load_config(args.config), catalog_home()
    result = reconcile(config, home)
    if args.settle and result['state'] != 'disabled':
        # SessionEnd can precede the shell's deletion marker or backend commit.
        # Recheck twice; durable markers are reconciled on later hooks as well.
        for delay in (0.5, 2):
            time.sleep(delay)
            checked = reconcile(config, home)
            result = {**checked, 'deleted': result['deleted'] + checked['deleted']}
    print(json.dumps(result))


if __name__ == '__main__':
    main()
