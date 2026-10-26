"""Finite route probe using one whole project source on an isolated DB backup.

This is a capability smoke case, NOT held-out semantic or cost acceptance.
No graph publication, provider fallback, hook registration or background retry.
"""
import argparse
from contextlib import closing
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sessiongraph.classifier_shadow import run
from sessiongraph.core import Store, load_config
from sessiongraph.gateway_worker import gateway_credentials
from rehearse_run_receipts import manifest as protected_manifest
from rehearse_source_retention import db_bytes, metadata_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--entry-id', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--without-logprobs', action='store_true', help='explicit independent label-only probe, never an automatic fallback')
    parser.add_argument('--allow-network', action='store_true')
    parser.add_argument('--verify-only', action='store_true', help='replay existing probe using current code with transport disabled')
    args = parser.parse_args()
    config = load_config(args.config)
    target, production = args.snapshot.resolve(), Path(config['database']).resolve()
    if target == production or target.is_relative_to(production.parent) or not target.is_file():
        raise ValueError('isolated_consistent_backup_required')
    report_path = target.parent / (args.run_id + ('-recheck.json' if args.verify_only else '-report.json'))
    if report_path.exists():
        raise ValueError('rehearsal_report_already_exists')
    config = copy.deepcopy(config)
    config.update(database=str(target), _config_path=str(target.parent / 'config.json'))
    config.setdefault('graph_v2', {})['single_token_judge'] = {'request_logprobs': not args.without_logprobs}
    with closing(Store(config, readonly=True)) as store:
        row = store.db.execute('SELECT id,text,role FROM entries WHERE id=? AND length(text)<=4000', (args.entry_id,)).fetchone()
        if not row or row['role'] != 'user' or 'max-token=1' not in row['text']:
            raise ValueError('whole_project_user_classification_source_required')
        plan = dict(project_id=config['project_id'], run_id=args.run_id,
                    budget=dict(max_requests=1, max_input_bytes=64 * 1024, max_seconds=45),
                    questions=[dict(id='capability-smoke', state={'candidate': 'The user means a one-label model call to simulate classification.'},
                        question=dict(revision='classifier-intent-smoke-v1', prompt='Which interpretation of max-token=1 does this user explicitly request?',
                                      criteria={'classification': 'A normal generative model returning a class label to simulate classification or Jev.',
                                                'json_truncation': 'Truncating a multi-token JSON Judge answer to one token.',
                                                'unclear': 'Neither interpretation is explicitly established.'}, abstain='unclear'),
                        evidence=[dict(entry_id=row['id'], source_hash=hashlib.sha256(row['text'].encode()).hexdigest(), start=0, end=len(row['text']))])])
        preview = run(store, plan)
    if args.verify_only:
        with closing(Store(config)) as store:
            before_changes = store.db.total_changes
            with patch('sessiongraph.single_token_judge.read_response', side_effect=AssertionError('transport forbidden during recheck')):
                replay = run(store, plan, allow_network=True, call_budget=1)
            report = dict(replay=replay, transport_disabled=True, production_writes=0,
                          snapshot_row_changes=store.db.total_changes-before_changes)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if not args.allow_network:
        print(json.dumps(preview, indent=2))
        return
    started, cpu = time.monotonic(), time.process_time()
    before, meta_before, bytes_before = protected_manifest(target), metadata_hash(target), db_bytes(target)
    with gateway_credentials(config, Path.home() / '.codex/config.toml'):
        store = Store(config)
        try:
            result = run(store, plan, allow_network=True, call_budget=1)
            replay = run(store, plan, allow_network=True, call_budget=1)
            if replay['model_calls'] != 0:
                raise ValueError('probe_replay_called_model')
        finally:
            store.close()
    after, meta_after = protected_manifest(target), metadata_hash(target)
    if before != after or meta_before != meta_after:
        raise ValueError('protected_state_changed')
    with sqlite3.connect(target) as db:
        check = db.execute('PRAGMA quick_check').fetchone()[0]
        if check != 'ok':
            raise ValueError('integrity_failed')
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    report = dict(project_id=config['project_id'], source_entry_id=args.entry_id, snapshot=str(target),
                  production_writes=0, publication_enabled=False, held_out_acceptance=False,
                  protected_tables=before, protected_state_unchanged=True, metadata_unchanged=True,
                  integrity=check, result=result, replay=replay, before=bytes_before, after=db_bytes(target),
                  elapsed_seconds=time.monotonic()-started, cpu_seconds=time.process_time()-cpu)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    (target.parent / (args.run_id + '-manifest.json')).write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'protected_tables'}, ensure_ascii=False, indent=2))
    print('report:', report_path)


if __name__ == '__main__':
    main()
