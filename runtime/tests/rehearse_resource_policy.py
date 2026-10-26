"""Synthetic cumulative-admission measurement, no production or provider access."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sessiongraph import graph_v2, request_audit, resource_policy, stages
from sessiongraph.core import Store, init_project, load_config


def footprint(path):
    files = [Path(str(path) + suffix) for suffix in ('', '-wal', '-shm')]
    return {key: sum(getattr(p.stat(), attr) * scale for p in files if p.exists())
            for key, attr, scale in [('apparent', 'st_size', 1), ('allocated', 'st_blocks', 512)]}


def rehearse():
    with tempfile.TemporaryDirectory(prefix='sg-resource-fixture-') as temporary:
        config = load_config(init_project(Path(temporary) / 'project'))
        config['graph_v2'] = dict(enabled=True, recovery_policy={'enabled': True},
            resource_policy=dict(enabled=True, max_requests=12, max_input_bytes=1048576,
                max_output_tokens=120, max_transport_seconds=180,
                circuit_failure_threshold=2, circuit_cooldown_seconds=120))
        store = Store(config)
        try:
            graph_v2.setup(store)
            with store.db:
                store.db.execute('INSERT INTO sg2_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    ('synthetic-job', config['project_id'], 'synthetic-chat', 'segment', 'pending',
                     '{}', None, '[]', '{}', None, 'now', 'now'))
            store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            before = footprint(config['database'])
            client = SimpleNamespace(config=config, key='', model='synthetic', timeout=5,
                max_output=10, traces=[], provider_name='synthetic')
            request = Request('http://127.0.0.1:4317/v1/responses',
                              data=b'{"model":"synthetic","max_output_tokens":10}')
            dispatched = []
            class Response:
                status, headers = 200, {}
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self, limit): return b'{"usage":{"input_tokens":3,"output_tokens":1}}'
            class Opener:
                def open(self, request, timeout):
                    dispatched.append(timeout)
                    return Response()
            def forbidden(*args, **kwargs):
                raise AssertionError('rehearsal must not connect or launch processes')
            cpu, wall = time.process_time(), time.perf_counter()
            with patch('urllib.request.build_opener', lambda *args: Opener()), \
                    patch('socket.create_connection', forbidden), patch('subprocess.Popen', forbidden):
                for number in range(12):
                    stages.execute(store, 'synthetic-job', str(number), 'extract',
                        lambda: bool(request_audit.read_response(client, request, [])), {}, cache=False)
                elapsed, cpu = time.perf_counter() - wall, time.process_time() - cpu
                def denials():
                    for _ in range(100):
                        try:
                            resource_policy.reserve(client, request, resource_policy.settings(config))
                        except resource_policy.AdmissionDenied as exc:
                            assert exc.code == 'resource_budget_exhausted'
                        else:
                            raise AssertionError('cumulative budget reset')
                stages.execute(store, 'synthetic-job', 'denied', 'relations', denials, {}, cache=False)
            scope = dict(store.db.execute('SELECT * FROM sg_resource_scopes').fetchone())
            assert scope['requests'] == len(dispatched) == 12
            assert scope['input_bytes'] == 12 * len(request.data)
            assert scope['output_tokens'] == 12
            reserved_output = store.db.execute('SELECT sum(output_tokens) FROM sg_resource_requests').fetchone()[0]
            assert store.db.execute('SELECT count(*) FROM sg_resource_requests').fetchone()[0] == 12
            objects = [r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE tbl_name LIKE 'sg_resource_%'")]
            marks = ','.join('?' for _ in objects)
            has_dbstat = store.db.execute("SELECT sqlite_compileoption_used('ENABLE_DBSTAT_VTAB')").fetchone()[0]
            pages = dict(store.db.execute(f'SELECT name,sum(pgsize) FROM dbstat WHERE name IN ({marks}) GROUP BY name', objects)) if has_dbstat else None
            active = footprint(config['database'])
            store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            after = footprint(config['database'])
            integrity = store.db.execute('PRAGMA quick_check').fetchone()[0]
            audit_files = list((Path(config['database']).parent / 'request-audit').glob('*.json'))
            audit_bytes = sum(p.stat().st_size for p in audit_files)
            audit_allocated = sum(p.stat().st_blocks * 512 for p in audit_files)
            return dict(scope='synthetic stage + audited transport + SQLite only',
                wall_seconds=round(elapsed, 6), process_cpu_seconds=round(cpu, 6),
                admitted_requests=12, mocked_dispatches=len(dispatched), blocked_preflights=100,
                request_bytes_charged=scope['input_bytes'], output_tokens_reserved=reserved_output,
                output_tokens_charged=scope['output_tokens'],
                observed_output_tokens=12, transport_seconds_measured=scope['seconds'],
                database_before=before, database_active_after=active, database_checkpointed_after=after,
                checkpointed_database_growth={k: after[k] - before[k] for k in before},
                resource_table_index_bytes=pages, audit_file_bytes=audit_bytes,
                audit_allocated_bytes=audit_allocated, quick_check=integrity,
                resource_table_attribution='dbstat' if has_dbstat else 'unavailable_in_this_sqlite_build',
                production_accesses=0, real_network_calls=0,
                limits=['No real provider latency, model quality or total token cost measured',
                    'No whole-project disk cap or ledger GC', 'No input tokenizer, hard wall deadline, CPU cap or electricity measurement',
                    'Timing covers 12 successful synthetic stages, not the following 100 denial checks'])
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
