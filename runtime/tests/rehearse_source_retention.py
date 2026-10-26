"""Finite, provider-free retention rehearsal on an existing isolated DB backup."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sessiongraph.core import Store, load_config
from sessiongraph.maintenance import maintain_sources
from sessiongraph import source_retention as retention
from rehearse_run_receipts import manifest


def db_bytes(path):
    paths = [path.with_name(path.name + suffix) for suffix in ('', '-wal', '-shm')]
    return {p.name: dict(apparent=p.stat().st_size, allocated=p.stat().st_blocks * 512)
            for p in paths if p.is_file()}


def metadata_hash(path):
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        return hashlib.sha256(json.dumps(db.execute('SELECT * FROM metadata ORDER BY key').fetchall(),
                                          ensure_ascii=False).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--verify-only', action='store_true', help='recheck existing snapshots without any database writes')
    args = parser.parse_args()
    original = load_config(args.config)
    target = args.snapshot.resolve()
    production = Path(original['database']).resolve()
    if target == production or target.is_relative_to(production.parent) or not target.is_file():
        raise ValueError('isolated_consistent_backup_required')
    config = dict(original, database=str(target), _config_path=str(target.parent / 'config.json'))
    if args.verify_only:
        store = Store(config, readonly=True)
        counts = Counter()
        try:
            with patch.object(Path, 'open', side_effect=FileNotFoundError('source unavailable')):
                for row in store.db.execute('SELECT entry_id FROM sg_retained_sources ORDER BY entry_id'):
                    counts[retention.read(store, row[0])['status']] += 1
            report = dict(project_id=config['project_id'], independently_verified=dict(counts),
                          external_reads_disabled=True, model_calls=0, production_writes=0,
                          snapshot_writes=store.db.total_changes)
        finally:
            store.close()
        (target.parent / 'source-retention-recheck.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2))
        return
    report_path = target.parent / 'source-retention-report.json'
    if report_path.exists():
        raise ValueError('rehearsal_report_already_exists')
    started, cpu = time.monotonic(), time.process_time()
    before, meta_before = manifest(target), metadata_hash(target)
    bytes_before = db_bytes(target)
    batches, cursor = [], ''
    for _ in range(2000):
        result = maintain_sources(config, apply=True, limit=100, after=cursor)
        batches.append(result)
        if not result['more']:
            break
        if result['next_cursor'] == cursor:
            raise ValueError('rehearsal_budget_stalled')
        cursor = result['next_cursor']
    else:
        raise ValueError('rehearsal_batch_limit')
    after, meta_after = manifest(target), metadata_hash(target)
    if before != after or meta_before != meta_after:
        raise ValueError('protected_source_graph_or_recovery_state_changed')
    counts = Counter()
    store = Store(config, readonly=True)
    try:
        ids = [r[0] for r in store.db.execute('SELECT entry_id FROM sg_retained_sources ORDER BY entry_id')]
        # No external record access is permitted during the independent-read gate.
        with patch.object(Path, 'open', side_effect=FileNotFoundError('source unavailable')):
            for entry_id in ids:
                saved = retention.read(store, entry_id)
                counts[saved['status']] += 1
        if store.db.total_changes:
            raise ValueError('retention_reader_wrote_state')
        enabled = retention.enabled(store)
        references = list(store.db.execute('SELECT payload FROM sg_retained_sources'))
        archived = sum(r[0].startswith('{"_sessiongraph_payload_v1":') for r in references)
    finally:
        store.close()
    with sqlite3.connect(target) as db:
        check = db.execute('PRAGMA quick_check').fetchone()[0]
        violations = db.execute('PRAGMA foreign_key_check(sg_retained_sources)').fetchall()
        if check != 'ok' or violations:
            raise ValueError('rehearsal_integrity_failed')
        checkpoint = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
    bytes_after = db_bytes(target)
    report = dict(project_id=config['project_id'], snapshot=str(target), production_writes=0,
                  model_calls=0, publication_enabled=enabled, policy=retention.POLICY,
                  protected_tables=before, protected_state_unchanged=True, metadata_unchanged=True,
                  external_reads_disabled=True, independently_verified=dict(counts),
                  archived_payloads=archived, inline_payloads=len(references)-archived,
                  batches=len(batches), examined=sum(b['examined'] for b in batches),
                  added=sum(b['added'] for b in batches), existing=sum(b['existing'] for b in batches),
                  gaps=[gap for b in batches for gap in b['gaps']],
                  payload_bytes_reserved=sum(b['payload_bytes'] for b in batches),
                  before=bytes_before, after=bytes_after,
                  allocated_growth=sum(x['allocated'] for x in bytes_after.values()) - sum(x['allocated'] for x in bytes_before.values()),
                  integrity=check, retained_source_foreign_keys='ok', checkpoint=list(checkpoint),
                  elapsed_seconds=time.monotonic()-started, cpu_seconds=time.process_time()-cpu)
    # Evidence artifacts contain only identifiers, hashes, counts and measurements.
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k not in {'protected_tables', 'gaps'}}, indent=2))
    print('gap counts:', dict(Counter(g['code'] for g in report['gaps'])))
    print('report:', report_path)


if __name__ == '__main__':
    main()
