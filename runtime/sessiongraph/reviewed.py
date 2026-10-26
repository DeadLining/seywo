"""Publish validated, explicitly reviewed excerpts without advancing range watermarks."""

from __future__ import annotations

import json

from .core import digest, tokens
from .extraction import validate_output


def publish_reviewed(store, batch, output, provenance, replace_job_ids=()):
    if not batch or len(batch) > 200 or len({e["id"] for e in batch}) != len(batch):
        raise ValueError("require 1..200 unique reviewed source entries")
    source, manifest = {}, []
    for e in batch:
        row = store.db.execute("SELECT * FROM entries WHERE id=?", (e["id"],)).fetchone()
        if not row or not e["text"].strip() or e["text"] not in row["text"]:
            raise ValueError("reviewed text must be an exact original excerpt")
        if e["role"] != row["role"] or e["kind"] != row["kind"] or row["kind"] != "message":
            raise ValueError("reviewed source attribution mismatch")
        source[e["id"]] = dict(row)
        start = row["text"].index(e["text"])
        manifest.append(
            dict(
                entry_id=e["id"],
                start=start,
                end=start + len(e["text"]),
                source_hash=digest(row["text"]),
            )
        )
    # All segments must belong to the same logical chat; retrieval scopes jobs by chat.
    sessions = {e["session_id"] for e in source.values()}
    if len({store.chat_id(sid) for sid in sessions}) != 1:
        raise ValueError("reviewed publication requires one logical chat")
    for old_id in replace_job_ids:
        old = store.db.execute(
            "SELECT j.session_id FROM jobs j JOIN reviewed_batches b ON b.job_id=j.id WHERE j.id=?",
            (old_id,),
        ).fetchone()
        if old is not None and store.chat_id(old[0]) not in {
            store.chat_id(sid) for sid in sessions
        }:
            raise ValueError("cannot replace another chat's graph")
        if old is None and store.db.execute("SELECT 1 FROM jobs WHERE id=?", (old_id,)).fetchone():
            raise ValueError("only reviewed batches may be replaced")
    validate_output(output, batch, store.config["policy"])
    encoded = json.dumps(manifest, sort_keys=True)
    jid = "review_" + digest(store.config["policy_hash"] + encoded)[:32]
    output_hash = digest(json.dumps(output, sort_keys=True))
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        old = store.db.execute("SELECT provenance FROM jobs WHERE id=?", (jid,)).fetchone()
        if old:
            if json.loads(old[0]).get("output_hash") != output_hash:
                raise ValueError("reviewed batch already exists with different output")
            return {"job_id": jid, "state": "already_published"}
        lines = [e["line"] for e in source.values()]
        meta = dict(
            provenance,
            kind="reviewed-excerpts",
            output_hash=output_hash,
            review="agent-reviewed; not user approval of historical proposals",
        )
        store.db.execute(
            "INSERT INTO jobs(id,session_id,start_line,end_line,policy_hash,state,summary,provenance) VALUES (?,?,?,?,?,?,?,?)",
            (
                jid,
                sorted(sessions)[0],
                min(lines),
                max(lines),
                store.config["policy_hash"],
                "done",
                output["summary"],
                json.dumps(meta),
            ),
        )
        store.db.execute("INSERT INTO reviewed_batches VALUES (?,?)", (jid, encoded))
        mapping = {r["id"]: "r_" + digest(jid + ":" + r["id"])[:32] for r in output["records"]}

        def evidence(owner, refs):
            for ref in refs:
                # Keep absolute original-message offsets, not excerpt-local offsets.
                excerpt = next(e for e in batch if e["id"] == ref["entry_id"])
                start = source[ref["entry_id"]]["text"].index(excerpt["text"]) + excerpt[
                    "text"
                ].index(ref["quote"])
                store.db.execute(
                    "INSERT INTO evidence VALUES (?,?,?,?,?)",
                    (owner, ref["entry_id"], ref["quote"], start, start + len(ref["quote"])),
                )

        for r in output["records"]:
            rid = mapping[r["id"]]
            store.db.execute(
                "INSERT INTO records VALUES (?,?,?,?,?,?)",
                (
                    rid,
                    jid,
                    r["kind"],
                    r["content"],
                    r["scope"],
                    json.dumps(r["aliases"], ensure_ascii=False),
                ),
            )
            store.db.execute(
                "INSERT INTO docs VALUES (?,?,?)",
                (
                    rid,
                    "record",
                    " ".join(tokens(" ".join([r["content"], r["scope"], *r["aliases"]]))),
                ),
            )
            evidence(rid, r["evidence"])
        for i, edge in enumerate(output["relations"]):
            eid = "g_" + digest(jid + ":" + str(i))[:32]
            store.db.execute(
                "INSERT INTO edges VALUES (?,?,?,?,?)",
                (eid, jid, mapping[edge["source"]], mapping[edge["target"]], edge["kind"]),
            )
            evidence(eid, edge["evidence"])
        for old_id in replace_job_ids:
            if old_id == jid:
                raise ValueError("cannot replace the new batch itself")
            old_records = [
                r[0] for r in store.db.execute("SELECT id FROM records WHERE job_id=?", (old_id,))
            ]
            old_edges = [
                r[0] for r in store.db.execute("SELECT id FROM edges WHERE job_id=?", (old_id,))
            ]
            store.db.executemany(
                "DELETE FROM evidence WHERE owner_id=?", [(i,) for i in old_records + old_edges]
            )
            store.db.executemany(
                "DELETE FROM docs WHERE id=? AND type='record'", [(i,) for i in old_records]
            )
            for table in ("edges", "records", "reviewed_batches"):
                store.db.execute(f"DELETE FROM {table} WHERE job_id=?", (old_id,))
            store.db.execute("DELETE FROM jobs WHERE id=?", (old_id,))
    return dict(
        job_id=jid,
        state="published",
        records=len(output["records"]),
        relations=len(output["relations"]),
    )
