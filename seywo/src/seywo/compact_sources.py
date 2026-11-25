"""Select fresh dialogue occurrences and load only their eligible text."""

from .model_contract import model_eligible


def _source(row):
    return {
        "event_id": row["event_id"], "snapshot_id": row["snapshot_id"],
        "kind": row["kind"], "role": row["role"], "channel": row["channel"],
        "state": row["state"], "reason": row["reason"], "turn_id": row["turn_id"],
        "turn_seq": row["turn_seq"], "text": row["safe_text"],
        "start": 0, "end": len(row["safe_text"]), "ordinal": row["ordinal"],
        "record_no": row["record_no"],
    }


def _fresh_rows(store, chat_id, cursor):
    # Inspect identities first. Historical and tool bodies never need decoding
    # merely to determine whether a compact has new model-eligible input.
    fields = ("row_id,event_id,snapshot_id,kind,role,channel,state,reason,turn_id,turn_seq,"
              "ordinal,record_no,segment_id,raw_sha256,occurred_at")
    after = " AND (ordinal>? OR (ordinal=? AND record_no>?))" if cursor is not None else ""
    args = (chat_id, cursor[0], cursor[0], cursor[1]) if cursor is not None else (chat_id,)
    rows = [dict(r) for r in store.db.execute(
        f"SELECT {fields} FROM current_sources WHERE chat_id=?{after} ORDER BY ordinal,record_no", args)]
    if not rows:
        return []
    from collections import Counter

    def occurrence(row):
        native = row["turn_id"] or row["occurred_at"]
        return (native, row["raw_sha256"]) if native else None

    seen = Counter()
    if cursor is not None:
        previous = store.db.execute(
            "SELECT turn_id,occurred_at,raw_sha256 FROM current_sources WHERE chat_id=? "
            "AND (ordinal<? OR (ordinal=? AND record_no<=?))", args)
        seen.update(key for row in previous if (key := occurrence(row)) is not None)
    fresh, segment, leading, available = [], None, True, Counter()
    for row in rows:
        if row["segment_id"] != segment:
            segment, leading, available = row["segment_id"], True, seen.copy()
        key = occurrence(row)
        if cursor is not None and leading and key is not None and available[key]:
            available[key] -= 1
            continue
        if row["state"] != "excluded_mechanical":
            leading = False
        if key is not None:
            seen[key] += 1
        row["safe_text"] = ""
        fresh.append(row)
    eligible = [r for r in fresh if model_eligible(r)]
    for offset in range(0, len(eligible), 512):
        batch = {r["row_id"]: r for r in eligible[offset:offset + 512]}
        placeholders = ",".join("?" for _ in batch)
        for row in store.db.execute(
                "SELECT f.row_id,seywo_source_text(p.data) text FROM source_fragments f "
                "JOIN source_payloads p ON p.id=f.payload_id "
                f"WHERE f.row_id IN ({placeholders})", tuple(batch)):
            batch[row["row_id"]]["safe_text"] = row["text"]
    return fresh
