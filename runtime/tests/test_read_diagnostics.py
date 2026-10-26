import json
from pathlib import Path
import pytest
from sessiongraph import native_bridge
from test_work_map import setup_map


def test_read_emits_safe_phase_timing_and_zero_models(tmp_path,capsys):
    store,root,add=setup_map(tmp_path)
    add('a','private evidence must not be logged','title')
    pid=store.config['project_id'];store.close()
    registry=tmp_path/'projects.json';registry.write_text(json.dumps({'projects':[{'root':str(root)}]}))
    value=native_bridge.read(registry,{'operation':'work_map','project':pid})
    assert value['read_diagnostics']['model_calls']==0
    assert 'map.references' in value['read_diagnostics']['phases_ms']
    assert 'private evidence' not in capsys.readouterr().err


def test_budget_reports_phase_without_sensitive_sql(tmp_path):
    from sessiongraph.read_diagnostics import ReadTrace,ReadTimeout
    with ReadTrace(deadline_seconds=-1) as trace:
        with pytest.raises(ReadTimeout) as caught:
            with trace.phase('map.relations'):
                pass
        assert caught.value.phase=='map.relations'


def test_graph_counts_and_recent_page_use_small_indexes(tmp_path):
    from sessiongraph import graph_v2
    store, _, add = setup_map(tmp_path)
    add('a', 'source backed result', 'result')
    graph_v2.setup(store)
    queries = [
        ('SELECT count(*) FROM sg2_nodes WHERE project=?', 'sg2_nodes_project'),
        ("SELECT reason,count(*) FROM sg2_unresolved WHERE project=? AND revision='' GROUP BY reason", 'sg2_unresolved_counts'),
        ('SELECT * FROM work_memory WHERE project_id=? ORDER BY updated_at DESC,id DESC LIMIT 1000', 'work_memory_page'),
    ]
    for sql, index in queries:
        plan = ' '.join(str(row[3]) for row in store.db.execute('EXPLAIN QUERY PLAN '+sql, (store.config['project_id'],)))
        assert index in plan and 'TEMP B-TREE' not in plan, plan
    store.close()


def test_canvas_response_is_preview_and_full_detail_remains_available(tmp_path):
    store,root,add=setup_map(tmp_path)
    text='evidence and result '*40
    mid=add('a',text,'result')
    pid=store.config['project_id'];store.close()
    registry=tmp_path/'projects.json';registry.write_text(json.dumps({'projects':[{'root':str(root)}]}))
    value=native_bridge.read(registry,{'operation':'work_map','project':pid})
    row=next(n for n in value['nodes'] if n['id']==mid)
    assert len(row['content'])==180 and row['content_truncated'] is True
    full=native_bridge.read(registry,{'operation':'memory','project':pid,'id':mid})
    assert full['memory']['content']==text.strip()
