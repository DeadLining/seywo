"""Development-only rehearsal against an existing consistent project DB backup.

Reads original project receipts only; all writes target the isolated backup folder.
Never instantiates an extractor, registers a worker, or changes the source project.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sessiongraph.auto_update import _folder, update_status
from sessiongraph.core import load_config
from sessiongraph.maintenance import maintain_receipts
from sessiongraph import run_receipts

PROTECTED = ('raw_events', 'sessions', 'session_chats', 'entries', 'work_memory', 'work_memory_relations', 'sg2_nodes',
             'sg2_relations', 'sg2_evidence', 'sg2_relation_evidence', 'sg_source_locations',
             'sg_input_coverage', 'sg2_jobs', 'sg2_job_parts', 'sg2_attempts',
             'sg2_control', 'sg2_claims', 'sg2_checkpoints', 'sg2_cursors', 'sg2_unresolved')


def manifest(path):
    result = {}
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.execute('BEGIN')
        for table in PROTECTED:
            if not db.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (table,)).fetchone():
                continue
            columns = db.execute(f'PRAGMA table_info("{table}")').fetchall()
            keys = sorted((c[5], c[1]) for c in columns if c[5])
            order = ','.join('"' + name + '"' for _, name in keys) or 'rowid'
            digest = hashlib.sha256()
            count = 0
            for row in db.execute(f'SELECT * FROM "{table}" ORDER BY {order}'):
                raw = json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode()
                digest.update(len(raw).to_bytes(8, 'big'))
                digest.update(raw)
                count += 1
            result[table] = dict(rows=count, sha256=digest.hexdigest())
    return result


def footprint(folder):
    paths = [p for p in folder.rglob('*') if p.is_file() and p.name != 'rehearsal-report.json']
    return dict(apparent_bytes=sum(p.stat().st_size for p in paths),
                allocated_bytes=sum(p.stat().st_blocks * 512 for p in paths), files=len(paths))


def main():
    total_started = time.monotonic()
    total_cpu = time.process_time()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', required=True, type=Path)
    p.add_argument('--snapshot', required=True, type=Path)
    args = p.parse_args()
    original = load_config(args.config)
    snapshot = args.snapshot.resolve()
    if snapshot == Path(original['database']).resolve() or snapshot.parent.is_relative_to(
            Path(original['database']).resolve().parent):
        raise ValueError('rehearsal_must_be_outside_production_state')
    if not snapshot.is_file():
        raise ValueError('consistent_database_backup_required')
    config = dict(original, database=str(snapshot), _config_path=str(snapshot.parent / 'config.json'))
    # Never read the live global continuation queue while rehearsing.
    os.environ.pop('SESSIONGRAPH_WORKER_LOCK', None)
    os.environ['SESSIONGRAPH_RUNTIME_HOME'] = str(snapshot.parent / 'runtime')
    target = _folder(config)
    if target.exists():
        raise ValueError('rehearsal_receipts_already_exist')
    target.mkdir(parents=True)
    hashes = {}
    for source in _folder(original).glob('*.json'):
        copied = target / source.name
        shutil.copy2(source, copied)
        hashes[source.stem] = hashlib.sha256(copied.read_bytes()).hexdigest()
    before = manifest(snapshot)
    status_before = update_status(config)
    baseline_timings = []
    for _ in range(10):
        t = time.monotonic()
        update_status(config)
        baseline_timings.append((time.monotonic() - t) * 1000)
    bytes_before = footprint(snapshot.parent)
    started = time.monotonic()
    cpu = time.process_time()
    batches = []
    cursor = ''
    while True:
        batch = maintain_receipts(config, apply=True, limit=100, after=cursor)
        batches.append(batch)
        if not batch['more']:
            break
        if batch['next_cursor'] == cursor:
            raise ValueError('rehearsal_no_progress')
        cursor = batch['next_cursor']
    elapsed = time.monotonic() - started
    cpu = time.process_time() - cpu
    after = manifest(snapshot)
    verified = 0
    for run_id, expected in hashes.items():
        raw = run_receipts.read(config, run_id).encode('utf-8')
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('rehearsal_receipt_hash_mismatch')
        verified += 1
    status_after = update_status(config)
    if status_after != status_before or before != after:
        raise ValueError('rehearsal_protected_state_changed')
    timings = []
    for _ in range(10):
        t = time.monotonic()
        update_status(config)
        timings.append((time.monotonic() - t) * 1000)
    with sqlite3.connect(snapshot) as db:
        check = db.execute('PRAGMA quick_check').fetchone()[0]
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        if check != 'ok':
            raise ValueError('rehearsal_integrity_failed')
    report = dict(project_id=config['project_id'], snapshot=str(snapshot), model_calls=0,
                  production_writes=0, protected_tables=before, protected_state_unchanged=True,
                  status_unchanged=True, receipts_verified=verified,
                  archived=sum(x['archived'] for x in batches),
                  skipped=sum(x['skipped'] for x in batches),
                  errors=[x for b in batches for x in b['errors']], batches=len(batches),
                  migration_seconds=elapsed, migration_cpu_seconds=cpu,
                  read_ms_before=baseline_timings, read_ms_after=timings,
                  total_seconds=time.monotonic() - total_started,
                  total_cpu_seconds=time.process_time() - total_cpu,
                  before=bytes_before, after=footprint(snapshot.parent),
                  integrity=check)
    path = snapshot.parent / 'rehearsal-report.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k:v for k,v in report.items() if k != 'protected_tables'}, indent=2))
    print('report:', path)


if __name__ == '__main__':
    main()
