from types import SimpleNamespace
import sqlite3
import pytest
from sessiongraph.core import SCHEMA
from sessiongraph.graph_v2 import SCHEMA as GRAPH_SCHEMA
from sessiongraph.relation_evidence import verified


def fixture():
    db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row;db.executescript(SCHEMA+GRAPH_SCHEMA)
    db.execute("INSERT INTO entries VALUES('e','s',1,'now','message','user','alpha beta',NULL)")
    db.executemany('INSERT INTO sg2_relation_evidence VALUES(?,?,?,?,?)',[
        ('a','r','e','alpha','j1'),('b','r','e','missing','j1'),('c','r','e','beta','j2')])
    db.commit()
    return SimpleNamespace(db=db,readonly=False)


def test_cached_evidence_keeps_invalid_job_out_and_revalidates_original():
    from sessiongraph.relation_evidence import setup,flush
    store=fixture();setup(store);flush(store)
    assert verified(store,['r']) == {'r':[{'entry_id':'e','quote':'beta'}]}
    store.db.execute("UPDATE entries SET text='alpha' WHERE id='e'");store.db.commit()
    assert verified(store,['r']) == {}
    flush(store)
    assert verified(store,['r']) == {}
    store.db.execute("UPDATE entries SET text='alpha beta missing' WHERE id='e'");store.db.commit()
    flush(store)
    assert len(verified(store,['r'])['r'])==3
    store.db.close()


def test_clean_evidence_read_never_loads_original_text():
    from sessiongraph.relation_evidence import setup,flush
    store=fixture();setup(store);flush(store)
    queries=[];store.db.set_trace_callback(queries.append)
    verified(store,['r'])
    assert not any('SELECT id,text FROM entries' in q for q in queries)
    store.db.close()


def test_relation_projection_rollback_does_not_publish_half_state():
    from sessiongraph.relation_evidence import setup,flush
    store=fixture();setup(store);flush(store)
    with pytest.raises(RuntimeError):
        with store.db:
            store.db.execute("DELETE FROM sg2_relation_evidence WHERE id='b'")
            raise RuntimeError('crash')
    assert verified(store,['r']) == {'r':[{'entry_id':'e','quote':'beta'}]}
    store.db.close()
