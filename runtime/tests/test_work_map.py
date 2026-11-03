"""The work map is a grounded projection, not a log diagram or inferred causality."""

import json

from sessiongraph.core import Store, init_project, load_config
from sessiongraph.memory import remember
from sessiongraph.work_map import work_map
from test_mvp import write_session


def setup_map(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = Store(load_config(init_project(root)))

    def add(chat, text, title, **kwargs):
        path = write_session(
            root / f"{chat}-{store.db.total_changes}.jsonl", root, chat, [("user", text)]
        )
        store.ingest(path)
        eid = store.db.execute("SELECT e.id FROM entries e JOIN sessions s ON s.id=e.session_id "
                               "WHERE s.path=?", (str(path),)).fetchone()[0]
        return remember(
            store,
            kind="finding",
            title=title,
            content=text,
            source_entry_id=eid,
            source_quote=text,
            **kwargs,
        )["id"]

    return store, root, add


def test_shared_exact_references_not_chat_or_tool_nodes(tmp_path):
    store, root, add = setup_map(tmp_path)
    a = add("chat-a", "Compact 测试报告在 reports/a.md", "Compact 测试", aliases=["Compact"])
    b = add("chat-b", "Compact 复验也参考 reports/a.md", "复验", aliases=["Compact"])
    before = store.db.total_changes
    d = work_map(store)
    assert {n["type"] for n in d["nodes"]} == {"memory", "reference"}
    assert not any(n["type"] in {"entry", "chat"} for n in d["nodes"])
    report = next(n for n in d["nodes"] if n.get("path") == str(root / "reports/a.md"))
    edges = [e for e in d["edges"] if e["target"] == report["id"]]
    assert {e["source"] for e in edges} == {a, b}
    assert all(e["kind"] == "REFERENCES_PATH" and e["evidence"][0]["quote"] for e in edges)
    assert len([n for n in d["nodes"] if n.get("kind") == "term"]) == 1
    assert d["model_calls"] == 0 and store.db.total_changes == before
    store.close()


def test_no_invented_links_and_isolated_items_remain_discoverable(tmp_path):
    store, root, add = setup_map(tmp_path)
    a = add("chat-a", "仅记录尚未完成的独立工作", "独立事项", aliases=["NOT-IN-SOURCE"])
    d = work_map(store)
    assert [n["id"] for n in d["nodes"]] == [a]
    assert d["edges"] == []
    assert d["stats"]["unlinked_memories"] == 1
    store.close()


def test_same_basename_different_paths_not_merged_and_chat_filter(tmp_path):
    store, root, add = setup_map(tmp_path)
    a = add("chat-a", "报告位于 first/REPORT.md", "报告一")
    b = add("chat-b", "报告位于 second/REPORT.md", "报告二")
    d = work_map(store)
    assert len({n["path"] for n in d["nodes"] if n.get("path")}) == 2
    only_a = work_map(store, chat="chat-a")
    assert a in {n["id"] for n in only_a["nodes"]}
    assert b not in {n["id"] for n in only_a["nodes"]}
    assert only_a["stats"]["memories"] == 1
    store.close()


def test_invalid_quote_and_other_project_never_create_links(tmp_path):
    store, root, add = setup_map(tmp_path)
    a = add("chat-a", "报告 reports/a.md", "报告")
    b = add("chat-b", "报告 reports/b.md", "别的项目")
    store.db.execute("UPDATE work_memory SET source_quote=? WHERE id=?", ("凭空 reports/a.md", a))
    store.db.execute("UPDATE work_memory SET project_id=? WHERE id=?", ("other-project", b))
    store.db.commit()
    d = work_map(store)
    assert d["edges"] == []
    assert {n["id"] for n in d["nodes"]} == {a}
    assert d["nodes"][0]["evidence_status"] == "missing_or_mismatched"
    store.close()


def test_path_never_inferred_from_generated_content_alone(tmp_path):
    store, root, add = setup_map(tmp_path)
    a = add("chat-a", "讨论报告，但未指定位置", "报告")
    store.db.execute(
        "UPDATE work_memory SET content=?,paths_json=? WHERE id=?",
        ("产物 reports/invented.md", json.dumps(["reports/invented.md"]), a),
    )
    store.db.commit()
    assert work_map(store)["edges"] == []
    store.close()


def test_truncation_is_explicit(tmp_path):
    store, root, add = setup_map(tmp_path)
    add("chat-a", "一个工作", "甲")
    add("chat-b", "另一个工作", "乙")
    d = work_map(store, limit=1)
    assert d["truncated"] and d["stats"]["total_memories"] == 2
    assert d["stats"]["memories"] == 1
    store.close()


def test_absolute_and_relative_paths_share_identity(tmp_path):
    store, root, add = setup_map(tmp_path)
    add("chat-a", "引用 reports/a.md", "相对路径")
    add("chat-b", f"引用 {root}/reports/a.md", "绝对路径")
    paths = [n for n in work_map(store)["nodes"] if n.get("kind") == "path"]
    assert len(paths) == 1 and paths[0]["degree"] == 2
    store.close()


def test_aliases_require_literal_boundaries_and_source_support(tmp_path):
    store, root, add = setup_map(tmp_path)
    add("chat-a", "API 讨论", "API", aliases=["API", "missing", "REPORT.md"])
    add("chat-b", "capital 没有提及独立词条", "capital")
    assert work_map(store)["edges"] == []
    add("chat-c", "api 再次讨论", "复验")
    refs = [n for n in work_map(store)["nodes"] if n.get("kind") == "term"]
    assert [n["label"] for n in refs] == ["api"]
    store.close()


def test_large_alias_page_compiles_each_pattern_once(tmp_path, monkeypatch):
    """More aliases than re's cache must not trigger page-size squared compiles."""
    import re
    store, root, add = setup_map(tmp_path)
    terms = [f'c-{i:03d}' for i in range(540)]
    mid = add('chat', ' '.join(terms), 'fixture')
    template = dict(store.db.execute('SELECT * FROM work_memory WHERE id=?', (mid,)).fetchone())
    store.db.execute('DELETE FROM work_memory WHERE id=?', (mid,))
    for i, term in enumerate(terms):
        row = {**template, 'id': f'wm_scale_{i}', 'title': term, 'content': term,
               'source_quote': term, 'aliases_json': json.dumps([term])}
        store.db.execute(f"INSERT INTO work_memory ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", list(row.values()))
    store.db.commit()
    re.purge()
    compiles = 0
    original = re._compiler.compile
    def tracked(*args, **kwargs):
        nonlocal compiles
        compiles += 1
        return original(*args, **kwargs)
    monkeypatch.setattr(re._compiler, 'compile', tracked)
    try:
        result = work_map(store)
        assert len(result['nodes']) == len(terms)  # independent terms create no shared links
        assert result['edges'] == []
        assert result['model_calls'] == 0
        assert compiles < 4 * len(terms), f'{compiles} regex compilations for {len(terms)} aliases'
    finally:
        store.close()
