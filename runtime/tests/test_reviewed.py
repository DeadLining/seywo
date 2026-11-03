"""Selective publication must not mark skipped history processed."""

from sessiongraph.source_text import hydrate
import pytest

from sessiongraph.core import Store, init_project, load_config
from sessiongraph.reviewed import publish_reviewed
from test_mvp import write_session


def test_reviewed_excerpt_idempotence_coverage_and_watermark(tmp_path):
    s = Store(load_config(init_project(tmp_path)))
    s.ingest(
        write_session(
            tmp_path / "chat.jsonl",
            tmp_path,
            "chat",
            [("user", "前文不允许外发。"), ("user", "前缀。查询不能调用模型。后缀。")],
        )
    )
    e = hydrate(s, s.db.execute("SELECT * FROM entries ORDER BY line DESC LIMIT 1").fetchone())
    excerpt = dict(e, text="查询不能调用模型。")
    output = {
        "summary": "人工筛选片段",
        "records": [
            {
                "id": "c",
                "kind": "Constraint",
                "content": "查询不能调用模型。",
                "scope": "本项目",
                "aliases": [],
                "evidence": [{"entry_id": e["id"], "quote": "查询不能调用模型。"}],
            }
        ],
        "relations": [],
    }
    args = (s, [excerpt], output, {"model": "test"})
    first = publish_reviewed(*args)
    assert publish_reviewed(*args)["job_id"] == first["job_id"]
    assert s.status()["records"] == 1
    assert s.status()["semantic_entries"] == 1
    assert s.status()["normal_semantic_entries"] == 0
    assert s.status()["reviewed_excerpts"] == 1
    assert s.db.execute("SELECT start FROM evidence").fetchone()[0] == e["text"].index(
        excerpt["text"]
    )
    assert s.enqueue() == 1
    job = s.db.execute("SELECT * FROM jobs WHERE state='pending'").fetchone()
    assert job["start_line"] == s.db.execute("SELECT min(line) FROM entries").fetchone()[0]
    with pytest.raises(ValueError):
        publish_reviewed(s, [dict(excerpt, text="伪造文字")], output, {})
    s.close()


def test_reviewed_spans_segments_not_chats(tmp_path):
    s = Store(load_config(init_project(tmp_path)))
    for i in (1, 2):
        s.ingest(
            write_session(
                tmp_path / f"part{i}.jsonl", tmp_path, "same-chat", [("user", f"约束{i}")]
            )
        )
    batch = [hydrate(s, r) for r in s.db.execute("SELECT * FROM entries")]
    output = {
        "summary": "同一聊天跨段",
        "records": [
            {
                "id": str(i),
                "kind": "Constraint",
                "content": e["text"],
                "scope": "测试",
                "aliases": [],
                "evidence": [{"entry_id": e["id"], "quote": e["text"]}],
            }
            for i, e in enumerate(batch)
        ],
        "relations": [],
    }
    assert publish_reviewed(s, batch, output, {})["records"] == 2
    with s.db:
        s.db.execute(
            "UPDATE session_chats SET chat_id=? WHERE segment_id=?",
            ("other", batch[1]["session_id"]),
        )
    with pytest.raises(ValueError, match="one logical chat"):
        publish_reviewed(s, batch, output, {})
    s.close()


def test_snapshot_replaces_only_prior_reviewed_graph(tmp_path):
    s = Store(load_config(init_project(tmp_path)))
    s.ingest(
        write_session(
            tmp_path / "s.jsonl", tmp_path, "chat", [("user", "限制一"), ("user", "限制二")]
        )
    )
    batch = [hydrate(s, r) for r in s.db.execute("SELECT * FROM entries ORDER BY line")]

    def output(entries):
        return {
            "summary": "test",
            "records": [
                dict(
                    id=str(i),
                    kind="Constraint",
                    content=e["text"],
                    scope="test",
                    aliases=[],
                    evidence=[dict(entry_id=e["id"], quote=e["text"])],
                )
                for i, e in enumerate(entries)
            ],
            "relations": [],
        }

    old = publish_reviewed(s, batch[:1], output(batch[:1]), {})
    new = publish_reviewed(s, batch, output(batch), {}, replace_job_ids=[old["job_id"]])
    assert new["records"] == 2
    assert s.status()["records"] == 2
    assert s.db.execute("SELECT count(*) FROM evidence").fetchone()[0] == 2
    assert s.db.execute("SELECT count(*) FROM sg_search_keys WHERE kind='record'").fetchone()[0] == 2
    assert s.status()["reviewed_excerpts"] == 2
    assert (
        publish_reviewed(s, batch, output(batch), {}, replace_job_ids=[old["job_id"]])["state"]
        == "already_published"
    )
    s.close()
