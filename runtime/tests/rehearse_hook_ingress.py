"""Bounded synthetic ingress measurement; never opens production project data."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sessiongraph import auto_update, continuation as queue, hook_ingress
from sessiongraph.core import Store, init_project, load_config


def rehearse(repetitions=1000):
    if type(repetitions) is not int or not 1 <= repetitions <= 1000:
        raise ValueError('repetitions must be 1..1000')
    with tempfile.TemporaryDirectory(prefix='sg-ingress-fixture-') as temporary:
        base = Path(temporary).resolve()
        config_path = init_project(base / 'project')
        raw = json.loads(config_path.read_text())
        raw['hook'].update(enabled=True, durable_ingress=True,
                           allowed_transcript_roots=[str(base / 'sources')])
        raw['graph_v2'] = {'enabled': True, 'recovery_policy': {'enabled': True}}
        config_path.write_text(json.dumps(raw))
        config = load_config(config_path)
        Store(config).close()
        source = base / 'sources' / 'synthetic-chat.jsonl'
        source.parent.mkdir()
        source.write_text(json.dumps({'type': 'session_meta', 'payload': {
            'id': 'synthetic-chat', 'cwd': str(base / 'project')}}) + '\n')
        database = Path(config['database'])
        db_before = hashlib.sha256(database.read_bytes()).hexdigest()
        writes, launches = [], []
        atomic = auto_update._atomic
        path_open = Path.open

        def write(path, value):
            writes.append(path.name)
            atomic(path, value)

        def open_guard(path, *args, **kwargs):
            if path == source or path == database:
                raise AssertionError('ingress must not read source or DB bodies')
            return path_open(path, *args, **kwargs)

        def forbidden(*args, **kwargs):
            raise AssertionError('rehearsal must not use network or spawn a process')

        with patch.dict('os.environ', {'SESSIONGRAPH_WORKER_LOCK': str(base / 'runtime' / 'worker.lock')}), \
                patch.object(auto_update, '_atomic', write), \
                patch.object(queue, 'start', lambda path: launches.append(path)), \
                patch.object(Path, 'open', open_guard), \
                patch('socket.create_connection', forbidden), \
                patch('subprocess.Popen', forbidden):
            first = hook_ingress.notify(config_path, 'synthetic-chat', 'PreCompact', str(source))
            queue_path = queue.root(config) / 'queue.json'
            queue_before = queue_path.read_bytes()
            writes_before, launches_before = len(writes), len(launches)
            cpu, wall = time.process_time(), time.perf_counter()
            for _ in range(repetitions):
                result = hook_ingress.notify(config_path, 'synthetic-chat', 'PostCompact', str(source))
                assert result['coalesced'] and result['model_calls'] == 0
                assert len(queue.snapshot(config)) == 1
            wall, cpu = time.perf_counter() - wall, time.process_time() - cpu
            assert first['accepted'] and not first['coalesced']
            assert queue_path.read_bytes() == queue_before
            assert len(writes) == writes_before and len(launches) == launches_before
            queue_bytes = queue_path.stat().st_size
            queue_allocated = queue_path.stat().st_blocks * 512
        assert hashlib.sha256(database.read_bytes()).hexdigest() == db_before
        return {'scope': 'synthetic ingress notifications and queue reads only',
            'repetitions': repetitions, 'wall_seconds': round(wall, 6),
            'process_cpu_seconds': round(cpu, 6), 'first_notification_queue_writes': writes_before,
            'first_notification_launches': launches_before,
            'duplicate_queue_writes': len(writes) - writes_before,
            'duplicate_launches': len(launches) - launches_before,
            'queue_bytes': queue_bytes, 'queue_allocated_bytes': queue_allocated,
            'source_body_reads': 0, 'database_body_reads_during_notifications': 0,
            'database_unchanged': True, 'network_calls': 0, 'production_accesses': 0,
            'limits': ['Not an installed-host event-delivery test',
                'Not a model, full-pipeline, token-cost, idle-wakeup or energy benchmark',
                'One pending source; no source-checkpoint garbage collection']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repetitions', type=int, default=1000)
    args = parser.parse_args()
    result = rehearse(args.repetitions)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
