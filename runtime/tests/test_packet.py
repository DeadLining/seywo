from sessiongraph.source_text import hydrate
import json
import socket

import pytest

from sessiongraph.core import Store, init_project, load_config
from test_mvp import write_session
from test_retrieval import indexed as indexed  # pytest fixture re-export


def encoded(packet):
    return json.dumps(packet, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@pytest.mark.parametrize("mode", ["raw", "hybrid", "graph"])
@pytest.mark.parametrize("budget", [512, 1024, 2048, 4096])
def test_packet_budget_exact_sources_and_no_network(indexed, monkeypatch, mode, budget):
    store, entries = indexed

    def blocked(*a, **k):
        raise AssertionError("retrieval network prohibited")

    monkeypatch.setattr(socket, "socket", blocked)
    packet = store.packet("compact hook", mode=mode, max_bytes=budget)
    assert len(encoded(packet)) <= budget
    assert packet["model_calls"] == 0
    assert packet["mode"] == mode
    assert len({e["entry_id"] for e in packet["entries"]}) == len(packet["entries"])
    for excerpt in packet["entries"]:
        row = next(e for e in entries if e["id"] == excerpt["entry_id"])
        assert excerpt["text"] == row["text"][excerpt["start"] : excerpt["end"]]
        assert excerpt["role"] == row["role"]
        assert excerpt["timestamp"] == row["timestamp"]
        assert excerpt["segment_id"] == row["session_id"]
        assert excerpt["truncated"] == (excerpt["start"] > 0 or excerpt["end"] < len(row["text"]))
    if budget >= 2048:
        assert any(e["entry_id"] == entries[1]["id"] for e in packet["entries"])
        if mode == "graph":
            assert any(e["entry_id"] == entries[2]["id"] for e in packet["entries"])


def test_alias_returns_original_not_derived(indexed):
    store, entries = indexed
    packet = store.packet("管理负担", mode="graph")
    assert any(e["text"] == entries[2]["text"] for e in packet["entries"])
    assert all("管理负担" not in e["text"] for e in packet["entries"])


def test_long_unicode_escaped_source(tmp_path):
    store = Store(load_config(init_project(tmp_path)))
    text = ('İ中文🙂"\\\n' * 400) + "marker exact original" + ("尾巴" * 900)
    store.ingest(
        write_session(tmp_path / "unicode.jsonl", tmp_path, "unicode", [("assistant", text)])
    )
    row = hydrate(store, store.db.execute("SELECT * FROM entries").fetchone())
    for budget in [512, 1024, 2048, 4000]:
        packet = store.packet("marker", max_bytes=budget)
        assert len(encoded(packet)) <= budget
        assert packet["entries"]
        e = packet["entries"][0]
        assert e["text"] == row["text"][e["start"] : e["end"]]
        assert "marker" in e["text"]
        assert e["truncated"]
    store.close()


@pytest.mark.parametrize(
    "kw",
    [
        dict(max_bytes=511),
        dict(max_bytes=True),
        dict(max_bytes=65537),
        dict(max_sources=0),
        dict(max_sources=True),
        dict(mode="bad"),
        dict(chat_id=""),
        dict(segment_id=""),
    ],
)
def test_invalid(indexed, kw):
    with pytest.raises(ValueError):
        indexed[0].packet("compact", **kw)


def test_empty_scoped_default_and_readonly(indexed):
    store, _ = indexed
    assert store.packet("ZXQ_NONEXISTENT_999")["entries"] == []
    assert store.packet("compact", chat_id="other")["entries"] == []
    assert store.packet("compact")["mode"] == "raw"
    readonly = Store(store.config, readonly=True)
    before = readonly.db.total_changes
    assert readonly.packet("compact")["entries"]
    assert readonly.db.total_changes == before
    readonly.close()


def ranked_fixture(parent_map, parent_order):
    class EvidenceStore:
        def _evidence(self, rid, include_quotes=False):
            return [{'entry_id':eid} for eid in parent_map[rid]]
    found={'hits':[{'type':'entry','id':f's{i}'} for i in range(5)],
           'related':[{'via_record_id':rid,'snippet_entry_id':f'n{rid}',
                       'evidence':[{'entry_id':f'n{rid}','start':0}]}
                      for rid in parent_order]}
    return EvidenceStore(),found


def test_packet_low_rank_parent_does_not_promote_neighbor_ahead_of_direct_hits():
    from sessiongraph.packet import _ordered_sources
    store,found=ranked_fixture({'low':['s4']},['low'])
    ids=[eid for eid,_,_ in _ordered_sources(store,found,8)]
    assert ids.index('nlow')>ids.index('s4')
    assert ids[:4]==['s0','s1','s2','s3']


def test_packet_neighbor_cap_is_applied_after_parent_relevance_order():
    from sessiongraph.packet import _ordered_sources
    store,found=ranked_fixture({'low':['s4'],'high':['s0']},['low','high'])
    ids=[eid for eid,_,_ in _ordered_sources(store,found,4)]
    assert 'nhigh' in ids and 'nlow' not in ids
    assert ids[:2]==['s0','s1']
    assert ids.index('nhigh')<ids.index('s2')


def test_packet_untraceable_neighbor_is_not_promoted():
    from sessiongraph.packet import _ordered_sources
    store,found=ranked_fixture({'orphan':['unreturned']},['orphan'])
    assert [e[0] for e in _ordered_sources(store,found,8)]==[f's{i}' for i in range(5)]


def test_packet_raw_candidate_order_is_unchanged():
    from sessiongraph.packet import _ordered_sources
    store,found=ranked_fixture({},[])
    assert _ordered_sources(store,found,8)==[(f's{i}',0,'raw') for i in range(5)]
