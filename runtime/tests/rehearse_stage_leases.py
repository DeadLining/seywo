"""Synthetic short-write-window rehearsal; never loads project or provider data."""
import argparse
import fcntl
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sessiongraph import graph_v2 as graph, maintenance
from sessiongraph.core import Store, init_project, load_config
from sessiongraph.extraction import create_extractor
from sessiongraph.model_judge import ModelJudge
from rehearse_resource_policy import footprint
from test_graph_v2 import output
from test_memories import entries
from test_mvp import write_session


def rehearse():
    with tempfile.TemporaryDirectory(prefix='sg-stage-fixture-') as temporary:
        root = Path(temporary) / 'project'
        config = load_config(init_project(root))
        config['provider'].update(protocol='responses', endpoint='http://127.0.0.1:4317/v1/responses',
                                  model='synthetic', auth='none')
        config['graph_v2'] = dict(enabled=True, recovery_policy={'enabled': True}, stage_policy={'enabled': True},
            resource_policy=dict(enabled=True, max_requests=12, max_input_bytes=1048576,
                max_output_tokens=65536, max_transport_seconds=180,
                circuit_failure_threshold=2, circuit_cooldown_seconds=120))
        store = Store(config)
        try:
            source = write_session(root / 'synthetic.jsonl', root, 'chat-1', [('user', 'synthetic training A produced model B')])
            store.ingest(source)
            graph.setup(store)
            target = store.db.execute('SELECT max(rowid) FROM entries').fetchone()[0]
            store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            before = footprint(config['database'])
            selected = output(entries(store))
            for item in selected['nodes'] + selected['relations']:
                item['evidence'] = [{'anchor_id': 'a1'}]
            selected['coverage'].pop('entry_ids')
            observations = []
            class Response:
                status, headers = 200, {}
                def __init__(self, result): self.result = result
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self, limit):
                    return json.dumps({'status': 'completed', 'usage': {'input_tokens': 10, 'output_tokens': 4},
                        'output': [{'type': 'message', 'role': 'assistant', 'content': [
                            {'type': 'output_text', 'text': json.dumps(self.result)}]}]}).encode()
            class Opener:
                def open(self, request, timeout):
                    with (Path(config['database']).parent / 'graph-v2.lock').open('a') as handle:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(handle, fcntl.LOCK_UN)
                    db = sqlite3.connect(config['database'], timeout=.1)
                    try:
                        db.execute('BEGIN IMMEDIATE')
                        running = db.execute('SELECT state FROM sg2_stage_lease').fetchone()[0]
                        db.rollback()
                    finally:
                        db.close()
                    busy = False
                    try:
                        with maintenance.project_lock(config): pass
                    except ValueError as error:
                        assert str(error) == 'maintenance_busy'
                        busy = True
                    assert running == 'running' and busy
                    if not observations:
                        with source.open('a') as stream:
                            stream.write(json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                                'content': [{'type': 'input_text', 'text': 'later unrelated input'}]}}) + '\n')
                        other = Store(config)
                        try: assert other.ingest(source)['added'] == 1
                        finally: other.close()
                    body = json.loads(request.data)
                    payload = json.loads(body['input'][0]['content'][0]['text'])
                    result = selected
                    if 'questions' in payload:
                        result = {}
                        for key, question in payload['questions'].items():
                            criteria = question['criteria']
                            chosen = next(c for c in ('supports', 'asserted', 'different', 'insufficient_evidence') if c in criteria)
                            result[key] = {c: float(c == chosen) for c in criteria}
                    observations.append(dict(graph_writer_available=True, sqlite_writer_available=True,
                                             maintenance_excluded=busy, stage=store.db.execute('SELECT stage FROM sg2_stage_lease').fetchone()[0]))
                    return Response(result)
            def forbidden(*args, **kwargs):
                raise AssertionError('no real network or subprocess in rehearsal')
            cpu, wall = time.process_time(), time.perf_counter()
            with patch('urllib.request.build_opener', lambda *args: Opener()), \
                    patch('socket.create_connection', forbidden), patch('subprocess.Popen', forbidden):
                result = graph.run_chat(store, create_extractor(config), 'chat-1',
                                        jev=ModelJudge(config), through_rowid=target)
                count = len(observations)
                replay = graph.run_chat(store, create_extractor(config), 'chat-1',
                                        jev=ModelJudge(config), through_rowid=target)
                assert replay['model_calls'] == 0 and len(observations) == count
            wall, cpu = time.perf_counter() - wall, time.process_time() - cpu
            assert len(result['saved']) == 2 and len(result['saved_relations']) == 1
            assert not result['failure_codes'] and result['model_calls'] == count == 2
            assert store.db.execute('SELECT count(*) FROM entries').fetchone()[0] == 2
            row = dict(store.db.execute('SELECT * FROM sg2_stage_lease').fetchone())
            assert row['state'] == 'released'
            active = footprint(config['database'])
            store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            after = footprint(config['database'])
            audit = list((Path(config['database']).parent / 'request-audit').glob('*.json'))
            return dict(scope='synthetic graph, real adapters/locks/importer/DB; provider output is a controlled fixture',
                mocked_dispatches=count, real_network_calls=0, production_accesses=0,
                saved_nodes=len(result['saved']), saved_relations=len(result['saved_relations']),
                input_entries_after_concurrent_append=2, frozen_frontier_replay_calls=replay['model_calls'],
                transport_windows=observations, final_lease_state=row['state'], lease_rows=1,
                wall_seconds=round(wall, 6), process_cpu_seconds=round(cpu, 6),
                database_before=before, database_active_after=active, database_checkpointed_after=after,
                checkpointed_database_growth={k: after[k] - before[k] for k in before},
                audit_file_bytes=sum(p.stat().st_size for p in audit),
                audit_allocated_bytes=sum(p.stat().st_blocks * 512 for p in audit),
                quick_check=store.db.execute('PRAGMA quick_check').fetchone()[0],
                limits=['Not installed-host delivery, production migration, canary or rollback',
                    'Not a real-model quality/latency/cost or electricity benchmark',
                    'One semantic stage at a time; same-chat run/coordinator and maintenance-exclusion leases remain',
                    'No fencing of legacy writers or time-based takeover of live owners',
                    'Conservative source/graph mutation invalidation may spend another bounded request'])
        finally:
            store.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = rehearse()
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
