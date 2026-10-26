"""Synthetic real publisher/retention/SQLite/control rehearsal, no model calls."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sessiongraph import graph_v2 as graph, revision_ledger as ledger, review_control as controls
from sessiongraph.failures import Failure
from sessiongraph.source_retention import read as retained
from test_memories import make_store, entries
from test_revision_ledger import publish, review, apply, item
from test_review_control import manifest, commit
from rehearse_resource_policy import footprint


def rehearse():
    with tempfile.TemporaryDirectory(prefix='sg-review-control-fixture-') as temporary:
        _,store,path=make_store(Path(temporary),[
            ('assistant','metric initial report: held-out pass'),
            ('assistant','metric correction: held-out was not measured'),
            ('assistant','metric audit: correction needs a narrower scope, not proof of truth'),
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
            def forbidden(*args,**kwargs):raise AssertionError('no network/model/process in rehearsal')
            cpu,wall=time.process_time(),time.perf_counter()
            with patch('socket.create_connection',forbidden),patch('subprocess.Popen',forbidden):
                old=publish(store,data[0],'held-out pass')
                new=publish(store,data[1],'held-out not measured')
                audit=publish(store,data[2],'held-out correction scope disputed')
                original=review(store,new,old)
                rid=apply(store,original)['review_id']
                store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                before_controls=footprint(store.config['database'])
                retract=manifest(store,rid,audit)
                first=commit(store,retract)
                assert item(store,old)['scopes']==[] and item(store,old)['validity']=='unassessed'
                restore=manifest(store,rid,audit,action='restore',previous=first['control_id'])
                second=commit(store,restore)
                assert item(store,old)['scopes'][0]['state']=='corrected'
                amend=manifest(store,rid,audit,action='amend',previous=second['control_id'],
                    replacement=review(store,audit,old,'disputes',conditions={'split':'unseen','task':'standing'},
                                       effective_from='2026-10-24T01:00:00Z'))
                third=commit(store,amend)
                store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                after_controls=footprint(store.config['database'])
                stale_rejected=False
                try:commit(store,manifest(store,rid,audit,action='restore',effective_from='2026-10-24T03:00:00Z'))
                except Failure as error:
                    assert error.code=='revision_control_stale_head'
                    stale_rejected=True
                for _ in range(100):
                    assert publish(store,data[0],'held-out pass')==old
                    apply(store,original)
                    commit(store,retract);commit(store,restore);commit(store,amend)
                assert publish(store,data[3],'held-out pass')==old
                output=item(store,old)
                assert output['scopes']==[dict(facet='held-out evaluation',conditions={'split':'unseen','task':'standing'},state='disputed')]
                assert controls.read(store,rid)['control']['head']==third['control_id']
                first_page=controls.read(store,rid,limit=2)
                last_page=controls.read(store,rid,limit=2,after=first_page['next_after'])
                assert len(first_page['actions'])+len(last_page['actions'])==3 and not last_page['has_more']
                path.unlink()
                assert all(retained(store,row['id'])['status']=='retained_verified' for row in data)
            wall,cpu=time.perf_counter()-wall,time.process_time()-cpu
            active=footprint(store.config['database'])
            counts={name:store.db.execute(f'SELECT count(*) FROM {name}').fetchone()[0] for name in
                ('sg2_nodes','sg2_evidence','sg_revision_items','sg_revision_sources','sg_revision_reviews','sg_revision_review_controls','sg_retained_sources')}
            assert counts==dict(sg2_nodes=1,sg2_evidence=4,sg_revision_items=3,sg_revision_sources=4,
                                sg_revision_reviews=2,sg_revision_review_controls=3,sg_retained_sources=4)
            store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            after=footprint(store.config['database'])
            return dict(scope='synthetic canonical subject and explicit review semantics; real publication/retention/SQLite/control/reducer',
                model_calls=0,real_network_calls=0,production_accesses=0,repeated_replay_cycles=100,
                counts=counts,after_retract_validity='unassessed',after_restore_scope='corrected',
                final_scopes=output['scopes'],final_validity=output['validity'],stale_head_rejected=stale_rejected,
                old_replay_did_not_reactivate=True,control_history_pages=2,all_four_retained_records_verified=True,
                external_transcript_deleted=True,wall_seconds=round(wall,6),process_cpu_seconds=round(cpu,6),
                database_before=before,database_after_schema=schema,database_before_controls=before_controls,
                database_after_controls=after_controls,database_active_after=active,database_checkpointed_after=after,
                checkpointed_database_growth={key:after[key]-before[key] for key in before},
                quick_check=store.db.execute('PRAGMA quick_check').fetchone()[0],
                limitations=['Normal readers and production unchanged; not classifier quality or user approval acceptance',
                    'Control changes are explicit, same-node, predecessor-guarded and nondecreasing in effective time',
                    '64 controls/review and 4096 scanned control rows/read; limits fail explicitly, no GC or automatic rollover',
                    'All destructive review edges remain in the conservative cycle guard even when retracted',
                    'Restoring an amended original does not implicitly retract its independent replacement',
                    'No knowledge-as-of history, production migration, app delivery or energy/whole-project saving proof'])
        finally:store.close()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=rehearse()
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))


if __name__=='__main__':main()
