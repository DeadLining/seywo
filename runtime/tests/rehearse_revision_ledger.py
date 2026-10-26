"""Synthetic publication -> immutable revisions -> scoped shadow correction."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sessiongraph import graph_v2 as graph, revision_ledger as ledger
from sessiongraph.source_retention import read as retained
from test_memories import make_store, entries
from test_revision_ledger import publish, review, apply
from rehearse_resource_policy import footprint


def rehearse():
    with tempfile.TemporaryDirectory(prefix='sg-revision-fixture-') as temporary:
        _,store,path=make_store(Path(temporary),[
            ('assistant','metric initial report: held-out pass'),
            ('assistant','metric correction: held-out was not measured'),
            ('assistant','metric stale summary: held-out pass')])
        try:
            store.config['graph_v2']={'enabled':True}
            graph.setup(store)
            store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            before=footprint(store.config['database'])
            store.config['graph_v2']['revision_policy']={'enabled':True}
            graph.setup(store)
            store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            schema=footprint(store.config['database'])
            data=entries(store)
            def forbidden(*args,**kwargs):raise AssertionError('no network, model or process in rehearsal')
            cpu,wall=time.process_time(),time.perf_counter()
            with patch('socket.create_connection',forbidden),patch('subprocess.Popen',forbidden):
                old=publish(store,data[0],'held-out pass')
                new=publish(store,data[1],'held-out not measured')
                manifest=review(store,new,old)
                apply(store,manifest)
                for _ in range(100):
                    assert publish(store,data[0],'held-out pass')==old
                    apply(store,manifest)
                assert publish(store,data[2],'held-out pass')==old
                result=ledger.inspect(store,'subject')
                historical=next(row for row in result['revisions'] if row['id']==old)
                assert historical['scopes'][0]['state']=='corrected'
                assert {row['validity'] for row in result['revisions']}=={'unassessed'}
                path.unlink()
                assert all(retained(store,e['id'])['status']=='retained_verified' for e in data)
            wall,cpu=time.perf_counter()-wall,time.process_time()-cpu
            active=footprint(store.config['database'])
            counts={name:store.db.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                for name in ('sg2_nodes','sg2_evidence','sg_revision_items','sg_revision_sources','sg_revision_reviews','sg_retained_sources')}
            assert counts==dict(sg2_nodes=1,sg2_evidence=3,sg_revision_items=2,sg_revision_sources=3,sg_revision_reviews=1,sg_retained_sources=3)
            store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            after=footprint(store.config['database'])
            return dict(scope='synthetic canonical subject; real publication/retention/SQLite/reducer; identity and semantic review explicitly supplied fixtures',
                model_calls=0,real_network_calls=0,production_accesses=0,repeated_publications_and_reviews=100,
                counts=counts,old_scope_state=historical['scopes'][0]['state'],new_revision_validity='unassessed',
                external_transcript_deleted=True,all_three_retained_records_verified=True,
                wall_seconds=round(wall,6),process_cpu_seconds=round(cpu,6),database_before=before,database_after_schema=schema,
                database_active_after=active,database_checkpointed_after=after,
                checkpointed_database_growth={key:after[key]-before[key] for key in before},
                quick_check=store.db.execute('PRAGMA quick_check').fetchone()[0],
                limitations=['Normal MCP/native/summary/restoration readers remain unchanged; this is a shadow ledger',
                    'Snapshots preserve existing scoped Claim/Decision content, not a new fully atomic predicate/value/unit schema',
                    'Facet/conditions/direct-authority judgments are explicit local review inputs, not model-validated facts or user approval',
                    'Same-node comparison only; canonical identity, cross-node correction, semantic review supersession and dependency propagation remain gated',
                    'Effective-time view of currently known reviews, not a knowledge-as-of reconstruction',
                    'No production migration, rollout, model quality/cost/energy or whole-project disk acceptance'])
        finally:store.close()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=rehearse()
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))


if __name__=='__main__':main()
