"""Select a committed prefix of native dialogue, separate from source ingestion.

The physical cursor is advanced only past closed turns. Lifecycle metadata never
enters semantic text. An old cursor inside a turn is repaired by rehydrating its
user/final sources, without decoding tool payloads or replaying other turns.
"""
import json

from .delta_pipeline import _fresh_rows
from .model_contract import model_eligible


def _metadata(store, rows):
    by_id = {row['row_id']: row for row in rows}
    wanted = [row['row_id'] for row in rows if row['kind'] == 'event_msg']
    for offset in range(0, len(wanted), 512):
        batch = wanted[offset:offset + 512]
        marks = ','.join('?' for _ in batch)
        for row in store.db.execute(
                'SELECT f.row_id,seywo_source_projection(p.data,f.turn_id,f.root_turn_id,f.turn_seq) projection '
                'FROM source_fragments f JOIN source_payloads p ON p.id=f.payload_id '
                f'WHERE f.row_id IN ({marks})', batch):
            projection = json.loads(row['projection'])
            by_id[row['row_id']]['turn_event'] = projection.get('turn_event')
            by_id[row['row_id']]['event_turn_id'] = projection.get('event_turn_id')


def closed_rows(store, chat_id, cursor):
    fresh = _fresh_rows(store, chat_id, cursor)
    if not fresh:
        return [], False
    _metadata(store, fresh)
    candidates = {row['turn_id'] for row in fresh if row['turn_id'] and model_eligible(row)
                  and row.get('channel') in (None, 'final')}
    candidates.update(row['turn_id'] for row in fresh if row['turn_id'] and row['reason'] == 'unterminated_record')
    if cursor:
        terminals = {row.get('event_turn_id') or row['turn_id'] for row in fresh
                     if row.get('turn_event') in {'task_complete', 'turn_aborted'}}
        for turn_id in terminals - candidates - {None}:
            committed = store.db.execute(
                'SELECT 1 FROM compact_turn_commits WHERE chat_id=? AND turn_id=?', (chat_id, turn_id)).fetchone()
            # Legacy cursors have no turn receipt. A previously complete answer
            # proves the terminal is only trailing metadata; otherwise restore
            # the interrupted request before advancing the semantic cursor.
            answer = store.db.execute(
                "SELECT 1 FROM current_sources WHERE chat_id=? AND turn_id=? AND role='assistant' "
                "AND (channel IS NULL OR channel='final') AND state='projected' "
                "AND (ordinal<? OR (ordinal=? AND record_no<=?)) LIMIT 1",
                (chat_id, turn_id, cursor[0], cursor[0], cursor[1])).fetchone()
            if not committed and not answer:
                candidates.add(turn_id)
    prior = []
    if cursor and candidates:
        # Only turns with a new visible body can need old context. A trailing
        # task_complete alone must never run the same semantic work twice.
        identities = sorted(candidates)
        for offset in range(0, len(identities), 256):
            batch = identities[offset:offset + 256]
            marks = ','.join('?' for _ in batch)
            prior.extend(dict(r) for r in store.db.execute(
                'SELECT row_id,event_id,snapshot_id,kind,role,channel,state,reason,turn_id,turn_seq,'
                'ordinal,record_no,segment_id,raw_sha256,occurred_at FROM current_sources '
                f'WHERE chat_id=? AND turn_id IN ({marks}) '
                'AND (ordinal<? OR (ordinal=? AND record_no<=?)) ORDER BY ordinal,record_no',
                (chat_id, *batch, cursor[0], cursor[0], cursor[1])))
        eligible = [r for r in prior if model_eligible(r) and r.get('channel') in (None, 'final')]
        by_id = {r['row_id']: r for r in eligible}
        for offset in range(0, len(eligible), 512):
            batch = [r['row_id'] for r in eligible[offset:offset + 512]]
            marks = ','.join('?' for _ in batch)
            for row in store.db.execute(
                    'SELECT f.row_id,seywo_source_text(p.data) text FROM source_fragments f '
                    f'JOIN source_payloads p ON p.id=f.payload_id WHERE f.row_id IN ({marks})', batch):
                by_id[row['row_id']]['safe_text'] = row['text']
        for row in prior:
            row.setdefault('safe_text', '')
    rows = sorted(prior + fresh, key=lambda r: (r['ordinal'], r['record_no']))
    _metadata(store, rows)
    groups, order = {}, []
    for row in rows:
        key = row['turn_id'] or f"record:{row['ordinal']}:{row['record_no']}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)
    blocked = None
    for position, key in enumerate(order):
        group = groups[key]
        if any(r['reason'] == 'unterminated_record' for r in group):
            blocked = min((r['ordinal'], r['record_no']) for r in group)
            break
        visible = [r for r in group if model_eligible(r) and r.get('channel') in (None, 'final')]
        if not visible:
            continue
        events = {r.get('turn_event') for r in group
                  if r.get('event_turn_id') in (None, key)}
        final = any(r['role'] == 'assistant' and r.get('channel') == 'final' for r in visible)
        legacy_answer = ('task_started' not in events
                         and any(r['role'] == 'assistant' for r in visible))
        closed = final or legacy_answer or bool(events & {'task_complete', 'turn_aborted'})
        # A subsequent native turn also seals an interrupted prior request.
        if not closed and position == len(order) - 1:
            blocked = min((r['ordinal'], r['record_no']) for r in group)
            break
    if blocked is None:
        return rows, False
    return [r for r in rows if (r['ordinal'], r['record_no']) < blocked], True


def commit_turns(db, chat_id, rows):
    """Progress metadata only; no dialogue/output duplication or Compact diary."""
    ends = {}
    for row in rows:
        if row['turn_id']:
            ends[row['turn_id']] = (row['ordinal'], row['record_no'])
    db.executemany('INSERT INTO compact_turn_commits VALUES(?,?,?,?) '
        'ON CONFLICT(chat_id,turn_id) DO UPDATE SET through_ordinal=excluded.through_ordinal,'
        'through_record=excluded.through_record',
        [(chat_id, turn, *end) for turn, end in ends.items()])
