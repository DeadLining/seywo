"""Bounded new-input mainline rehearsal; synthetic judgments, no provider traffic."""
import argparse
import json
from pathlib import Path
import tempfile
import time

from sessiongraph import graph_v2 as graph
from sessiongraph.core import Store
from sessiongraph.source_retention import read
from test_memories import make_store, entries
from test_graph_v2 import ContextExtractor
from test_effective_revisions import RevisionJudge, document, append


def sizes(database):
    paths = [Path(str(database) + suffix) for suffix in ('', '-wal', '-shm')]
    return {p.name: {'apparent': p.stat().st_size, 'allocated': p.stat().st_blocks * 512}
            for p in paths if p.exists()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='sg-effective-mainline-') as folder:
        _, store, path = make_store(Path(folder), [('assistant', 'runA success heldout seed42: pass; initial primary report.')])
        store.config['graph_v2'] = {'enabled':True, 'revision_policy': {'enabled':True, 'mode':'effective'}}
        database = store.config['database']
        store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        baseline = sizes(database)
        judge = RevisionJudge()
        sequence = [('pass', 'direct', None),
                    ('not measured', 'direct', 'runA success heldout seed42: explicit correction, not measured.'),
                    ('failed', 'direct', 'runA success heldout seed42: correction of correction, verified failed.'),
                    ('pass', 'derived', 'Summary of prior conversation: runA success heldout seed42: pass.')]
        wall, cpu = time.perf_counter(), time.process_time()
        for value, provenance, text in sequence:
            if text:
                append(store, path, text)
            result = graph.run_chat(store, ContextExtractor([document(entries(store)[-1], value, provenance=provenance)]),
                                    'chat-1', jev=judge)
            assert not result['failed'], result
        before_replay = sizes(database)
        changes = store.db.total_changes
        for _ in range(100):
            result = graph.run_chat(store, ContextExtractor([]), 'chat-1', jev=judge)
            assert result['model_calls'] == 0 and not result['saved'] and not result['failed']
        elapsed = dict(wall_seconds=time.perf_counter()-wall, cpu_seconds=time.process_time()-cpu)
        replay_changes = store.db.total_changes - changes
        after_replay = sizes(database)
        before_delete = store.recall('*', view='full', max_bytes=16000)['memories']
        assert len(before_delete) == 1 and before_delete[0]['content'] == 'failed'
        assert before_delete[0]['validity']['state'] == 'current' and judge.calls == 2
        path.unlink()
        assert all(read(store, e['id'])['status'] == 'retained_verified' for e in entries(store))
        counts = {table: store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in (
            'sg2_nodes', 'sg_revision_items', 'sg_revision_sources', 'sg_revision_reviews', 'sg_retained_sources',
            'sg_revision_projection')}
        active = sizes(database)
        store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        checkpointed = sizes(database)
        assert store.db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        reader = Store(store.config, readonly=True)
        try:
            assert reader.resume()['memories'][0]['content'] == 'failed'
            assert reader.recall('*')['memories'][0]['validity']['state'] == 'current'
        finally:
            reader.close()
        result = dict(scope='synthetic new-input mainline, not provider quality/production rollout',
            replay_count=100, counts=counts, semantic_fixture_calls=judge.calls,
            repeated_frontier_model_calls=0, network_calls=0, source_files_deleted_and_verified=True,
            replay_sqlite_changes=replay_changes, elapsed=elapsed,
            storage=dict(baseline=baseline, before_replay=before_replay, after_replay=after_replay,
                         active=active, checkpointed=checkpointed),
            current_content=before_delete[0]['content'], current_validity=before_delete[0]['validity'],
            quick_check='ok')
        store.close()
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
