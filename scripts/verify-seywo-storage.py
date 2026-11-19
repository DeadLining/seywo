#!/usr/bin/env python3
"""Lossless storage acceptance on an isolated copy; no model or Hook execution."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'seywo/src'))
from seywo.config import load_config
from seywo.locking import lifecycle
from seywo.projection import VERSION, search_tokens
from seywo.storage_admin import _db_digest, _integrity, migrate
from seywo.storage_lifecycle import collect
from seywo.store import Store


def size(state):
    files = [p for p in state.rglob('*') if p.is_file() and not p.is_symlink()]
    backup = [p for p in files if p.name.endswith('.sqlite3.gz') and 'storage-migration' in p.parts]
    return {'active_bytes': sum(p.stat().st_size for p in files if p not in backup),
            'rollback_bytes': sum(p.stat().st_size for p in backup),
            'database_bytes': (state / 'index.sqlite3').stat().st_size,
            'archive_bytes': sum(p.stat().st_size for p in (state / 'inbox/objects').glob('*.z'))}


def search_checks(config):
    results = {}
    with Store(config, read_only=True) as store:
        for query in ('compact', 'hook', 'Seywo', '模型', '主线', 'Retry', 'call_id', 'new_phase'):
            terms = ' AND '.join('"' + t + '"' for t in dict.fromkeys(search_tokens(query).split()))
            rows = store.db.execute('''SELECT DISTINCT c.event_id,c.snapshot_id FROM source_fts
                JOIN current_sources c ON c.payload_id=source_fts.rowid WHERE source_fts MATCH ?
                AND c.state<>'excluded_mechanical' ORDER BY c.event_id,c.snapshot_id''', (terms,)).fetchall()
            text = json.dumps([list(r) for r in rows], separators=(',', ':'))
            results[query] = {'matches': len(rows), 'sha256': hashlib.sha256(text.encode()).hexdigest()}
    return results


def copy_project(config, target):
    incoming = Path(config['_path']).parent
    state = target / '.seywo'
    target.mkdir(parents=True, mode=0o700)
    state.mkdir(mode=0o700)
    with lifecycle(config, exclusive=True):
        for item in incoming.iterdir():
            if item.name.startswith('index.sqlite3') or item.name.endswith('.lock'):
                continue
            if item.is_symlink():
                raise ValueError('symlink in source state')
            destination = state / item.name
            if item.is_dir():
                shutil.copytree(item, destination)
            else:
                shutil.copy2(item, destination)
        with closing(sqlite3.connect(f"file:{config['_database']}?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(state / 'index.sqlite3')) as db:
                source.backup(db)
    data = json.loads((state / 'config.json').read_text())
    data['project_root'] = str(target.resolve())
    (state / 'config.json').write_text(json.dumps(data, ensure_ascii=False, indent=2))
    with closing(sqlite3.connect(state / 'index.sqlite3')) as db, db:
        db.execute('UPDATE projects SET root=?', (str(target.resolve()),))
        for table in ('source_segments', 'source_archives'):
            db.execute(f'UPDATE {table} SET path=replace(path,?,?)', (str(incoming), str(state)))
    for path in (state / 'inbox/pending').glob('*.json'):
        item = json.loads(path.read_text())
        if item.get('snapshot'):
            item['snapshot'] = item['snapshot'].replace(str(incoming), str(state))
        path.write_text(json.dumps(item, ensure_ascii=False))
    return load_config(state / 'config.json')


def verify(config, output):
    state = Path(config['_path']).parent
    before = size(state)
    print(json.dumps({'stage': 'baseline', 'size': before}), flush=True)
    logical = _db_digest(config)
    searches = search_checks(config)
    began = time.monotonic()
    result = migrate(config)
    print(json.dumps({'stage': 'migrated', 'seconds': round(time.monotonic()-began, 2)}), flush=True)
    collection = collect(config, force=True)
    after = size(state)
    final = _db_digest(config)
    assert all(logical[k] == final[k] for k in ('source', 'narrative', 'tasks')), 'logical identities changed'
    assert search_checks(config) == searches, 'search membership changed'
    _integrity(config)  # Also validate exact archive reads after reclamation.
    with Store(config) as store:
        tables = ('source_fragments', 'source_payloads', 'source_snapshots', 'narrative_revisions', 'compact_batches')
        count = lambda: {t: store.db.execute('SELECT count(*) FROM ' + t).fetchone()[0] for t in tables}
        baseline = count()
        for row in store.db.execute('''SELECT s.chat_id,a.path FROM source_segments s JOIN source_snapshots ss ON ss.id=s.head_snapshot
                JOIN source_archives a ON a.snapshot_id=ss.id WHERE ss.projection_version=?''', (VERSION,)).fetchall():
            store.import_compact(row['path'], row['chat_id'], 'acceptance-replay')
        assert count() == baseline, 'identical source replay grew storage'
    report = {'ok': True, 'model_calls': 0, 'before': before, 'after': after,
              'active_reduction_percent': round((1-after['active_bytes']/before['active_bytes'])*100, 2),
              'logical_before': logical, 'logical_after': final, 'searches': searches,
              'migration': result, 'collection': collection,
              'replay_counts_unchanged': True, 'seconds': round(time.monotonic()-began, 2)}
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'stage': 'complete', 'report': str(output), 'before': before, 'after': after,
                      'reduction_percent': report['active_reduction_percent']}, ensure_ascii=False), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', help='Migrate the explicitly selected project instead of a copy.')
    args = parser.parse_args()
    args.output.mkdir(parents=True, mode=0o700)
    cfg = load_config(args.project.resolve() / '.seywo/config.json')
    if not args.apply:
        cfg = copy_project(cfg, args.output.resolve() / 'project')
    with lifecycle(cfg, exclusive=True):
        verify(cfg, args.output.resolve() / 'report.json')
