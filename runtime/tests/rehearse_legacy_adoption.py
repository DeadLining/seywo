"""Bounded lazy-adoption rehearsal on an isolated consistent project snapshot.

No historical extraction, semantic guesses, node deletion or production writes.
"""
import argparse
from contextlib import closing
import copy
import hashlib
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sessiongraph import graph_v2 as graph, revision_ledger as ledger
from sessiongraph.core import Store, load_config
from sessiongraph.failures import Failure
from sessiongraph.maintenance import counts


def graph_manifest(db):
    result = {}
    for table in ('work_memory', 'sg2_nodes', 'sg2_evidence', 'sg2_relations', 'sg2_relation_evidence'):
        digest = hashlib.sha256()
        count = 0
        for row in db.execute(f'SELECT * FROM {table} ORDER BY id'):
            raw = json.dumps(list(row), ensure_ascii=False, separators=(',', ':')).encode()
            digest.update(len(raw).to_bytes(8, 'big'))
            digest.update(raw)
            count += 1
        result[table] = dict(rows=count, sha256=digest.hexdigest())
    return result


def rehearse(config_path, snapshot, limit=64):
    config = load_config(config_path)
    snapshot = snapshot.resolve()
    production = Path(config['database']).resolve()
    if not snapshot.is_file() or snapshot == production or snapshot.is_relative_to(production.parent):
        raise ValueError('isolated_consistent_snapshot_required')
    if not 1 <= limit <= 128:
        raise ValueError('bounded_sample_required')
    config = copy.deepcopy(config)
    config.update(database=str(snapshot), _config_path=str(snapshot.parent / 'config.json'))
    config['graph_v2']['revision_policy'] = dict(enabled=True, mode='effective')
    started, cpu = time.monotonic(), time.process_time()
    with closing(Store(config, readonly=True)) as store:
        before, preserved = graph_manifest(store.db), counts(store)
        nodes = [r[0] for r in store.db.execute('''SELECT id FROM sg2_nodes
            WHERE project=? AND type IN ('Claim','Decision') ORDER BY id LIMIT ?''', (config['project_id'], limit))]
        legacy_count = store.db.execute("SELECT count(*) FROM sg2_nodes WHERE type IN ('Claim','Decision')").fetchone()[0]
        changes = store.db.total_changes
        statuses = ledger.projected_many(store, nodes)
        assert all(x['state'] == 'legacy_unassessed' for x in statuses.values()) and len(statuses) == len(nodes)
        assert store.db.total_changes == changes
    failures, adopted = [], []
    def forbidden(*args, **kwargs):
        raise AssertionError('no network, model or subprocess in migration rehearsal')
    with patch('socket.create_connection', forbidden), patch('subprocess.Popen', forbidden):
        with closing(Store(config)) as store:
            graph.setup(store)
            schema = snapshot.stat().st_size
            for node in nodes:
                try:
                    with store.db:
                        store.db.execute('BEGIN IMMEDIATE')
                        ledger.baseline(store, node)
                        ledger.refresh(store, node)
                    adopted.append(node)
                except Failure as exc:
                    failures.append(dict(node=node, code=exc.code))
            assert counts(store) == preserved
            assert graph_manifest(store.db) == before
            assert all(ledger.projected(store, node)['state'] == 'legacy_unassessed' for node in nodes)
            before_replay = store.db.total_changes
            for _ in range(100):
                ledger.projected_many(store, nodes)
            assert store.db.total_changes == before_replay
            # Roll back the opt-in without deleting the newly captured versions.
            store.config['graph_v2'].pop('revision_policy')
            assert ledger.projected_many(store, nodes) == {}
            assert graph_manifest(store.db) == before
            retained_versions = store.db.execute('SELECT count(*) FROM sg_revision_items').fetchone()[0]
            store.config['graph_v2']['revision_policy'] = dict(enabled=True, mode='effective')
            assert len(ledger.projected_many(store, nodes)) == len(nodes)
            assert store.db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
            store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            after_bytes = snapshot.stat().st_size
    return dict(project_id=config['project_id'], snapshot=str(snapshot), production_writes=0, model_calls=0,
        migration='additive schema plus lazy exact-payload adoption; no historical atomization',
        legacy_claims_and_decisions=legacy_count, sampled=len(nodes), adopted=len(adopted), failures=failures,
        graph_manifest=before, protected_counts=preserved, original_ids_and_payloads_unchanged=True,
        legacy_truth_not_fabricated=True, read_replays=100, replay_sqlite_changes=0,
        rollback='disable opt-in after baseline-only writes; originals and newly captured ledger rows preserved',
        retained_versions=retained_versions, new_effective_writes_rollback_proven=False,
        quick_check='ok', schema_bytes=schema, after_bytes=after_bytes,
        elapsed_seconds=round(time.monotonic()-started, 3), cpu_seconds=round(time.process_time()-cpu, 3),
        limitations=['Bounded adoption sample, not full-history atomization or semantic classifier acceptance',
                     'Disabling the opt-in is NOT an approved downgrade after effective correction writes'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('report_already_exists')
    result = rehearse(args.config, args.snapshot)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k:v for k,v in result.items() if k != 'graph_manifest'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
