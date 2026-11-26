"""Adversarial, model-free probes of Compact correctness; writes evidence only."""
import argparse
from datetime import date
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seywo import compact_schema  # noqa: E402
from seywo.compact_model import request_payload, validate  # noqa: E402
from seywo.compact_pipeline import apply_compact, _turn_packets, _batch_turns, _payload_turns  # noqa: E402
from seywo.narrative import Narrative, empty  # noqa: E402
from seywo.config import initialize, load_config  # noqa: E402
from seywo.store import Store, encode  # noqa: E402
from seywo.compact_sources import _fresh_rows  # noqa: E402


def source(i, text, *, role='user', channel='final'):
    return dict(event_id=f'e{i}', snapshot_id='s1', kind='message', role=role,
                channel=channel, state='projected', reason='projected',
                turn_id=f't{i}', turn_seq=i, ordinal=0, record_no=i,
                text=text, safe_text=text, start=0, end=len(text))


def turns():
    return [dict(turn_id=f't{i}', user=f'用户第{i}轮', assistant='', sources=[source(i, f'用户第{i}轮')]) for i in range(2)]


def change(title='原始事项', operation='new', indexes=None, target=None, is_open=True):
    return dict(operation=operation, title=title, type='goal', attribution='user',
                turn_indexes=indexes or [0], target_index=target, open=is_open, merge_indexes=[])


def output(changes, segments=None):
    return dict(segments=segments or [dict(title='当前主线', turn_indexes=[0], action='continue', anchor_index=None)],
                changes=changes, current=dict(goal_index=None, priority_indexes=[]))


def run():
    checks = []

    def check(identity, passed, expected, actual):
        checks.append(dict(id=identity, passed=bool(passed), expected=expected, actual=actual))

    out = output([change(indexes=[0, 1])], [
        dict(title='第一个片段', turn_indexes=[0], action='continue', anchor_index=None),
        dict(title='第二个片段', turn_indexes=[1], action='continue', anchor_index=None)])
    try:
        validated = validate(out, [0, 1], set())
        doc, _, _ = apply_compact(empty(), validated, turns(), [], 'cross-segment')
        check('single_change_applied_once', len(doc['items']) == 1,
              'one change becomes one item (or contract rejects cross-segment change)',
              dict(changes=1, published_items=len(doc['items']), titles=[i['title'] for i in doc['items']]))
    except ValueError as exc:
        check('single_change_applied_once', True, 'one item or explicit contract rejection', str(exc))

    first, _, _ = apply_compact(empty(), output([change()]), turns(), [], 'first')
    identity = first['items'][0]['id']
    check('overview_contains_current_goal', identity in first['overview']['goal_item_ids'],
          'initial user goal appears in overview goals', first['overview']['goal_item_ids'])
    catalog = [dict(index=0, id=identity, title='原始事项', type='goal')]
    updated, _, _ = apply_compact(first, output([change('更名后的事项', 'update', target=0)]), turns(), catalog, 'update')
    resolved, _, _ = apply_compact(updated, output([change('更名后的事项', 'resolve', target=0, is_open=False)]), turns(), catalog, 'resolve')
    check('renamed_resolved_item_leaves_no_stale_unresolved', not resolved['overview']['unresolved'],
          'renamed then resolved goal leaves no stale title', resolved['overview']['unresolved'])

    bad = output([change(is_open='false')])
    try:
        validate(bad, [0], set())
        check('boolean_status_is_enforced', False, 'reject non-boolean open', 'accepted open="false"')
    except ValueError as exc:
        check('boolean_status_is_enforced', True, 'reject non-boolean open', str(exc))

    bad = output([change('原始事项', 'resolve', target=0, is_open=True)])
    try:
        validate(bad, [0], {0})
        check('resolve_must_close_item', False, 'resolve cannot leave open=true', 'accepted resolve/open=true')
    except ValueError as exc:
        check('resolve_must_close_item', True, 'resolve cannot leave open=true', str(exc))

    marker = '月结口径改为自然月'
    long_user = '开头说明。' * 250 + marker + '结尾说明。' * 250
    native = dict(turn_id='long', user=long_user, assistant='', sources=[source(20, long_user)])
    payloads = [request_payload(_payload_turns(batch), {}, []) for batch in _batch_turns([native])]
    offered = ''.join(turn['user'] for payload in payloads for turn in payload['turns'])
    check('middle_constraint_reaches_model', offered == long_user,
          'every character reaches model input exactly once, including neutral middle requirements',
          dict(original_chars=len(long_user), model_user_chars=len(offered), marker_present=marker in offered))

    packets = _turn_packets([source(0, '修复导出'), source(1, '正在逐个检查文件和执行测试', role='assistant', channel='commentary')])
    check('only_final_assistant_answer_in_input', not any(p['assistant'] for p in packets),
          'commentary progress absent from final-answer-only packet', [p['assistant'] for p in packets])

    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE chats(id TEXT PRIMARY KEY)')
    db.execute('CREATE TABLE atomicity_probe(value INTEGER)')
    db.commit()
    db.execute('BEGIN IMMEDIATE')
    db.execute('INSERT INTO atomicity_probe VALUES(1)')
    was_active = db.in_transaction
    compact_schema.setup(db)
    after_setup = db.in_transaction
    db.rollback()
    count = db.execute('SELECT count(*) FROM atomicity_probe').fetchone()[0]
    check('receipt_setup_preserves_publication_transaction', was_active and after_setup and count == 0,
          'receipt schema setup never commits a live publication transaction',
          dict(transaction_before=was_active, transaction_after=after_setup, rows_after_rollback=count))
    db.close()

    root = Path(tempfile.mkdtemp(prefix='seywo-manual-lock-probe-')) / 'project'
    initialize(root)
    config = load_config(root / '.seywo/config.json')
    transcript = root / 'source.jsonl'
    records = [
        dict(type='session_meta', payload=dict(id='manual-probe', cwd=str(root))),
        dict(type='turn_context', payload=dict(turn_id='t-manual', root_turn_id='t-manual')),
        dict(type='response_item', payload=dict(type='message', role='user', content=[dict(type='input_text', text='修复导出')]))
    ]
    transcript.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records))
    with Store(config) as store:
        store.import_source(transcript, 'manual-probe', 0)
        packets = _turn_packets(_fresh_rows(store, 'manual-probe', None))
        initial, _, _ = apply_compact(empty(), output([change('原始标题')]), packets, [], 'manual-initial')
        service = Narrative(store)
        with store.db:
            service._write('manual-probe', 1, 0, initial, encode([]), origin='invariant_probe')
        target_id = initial['items'][0]['id']
        service.edit('manual-probe', base_version=1, override_revision=0, actor='human',
                     patch=dict(entity='item', id=target_id, fields=dict(title='人工锁定标题')))
        manually_edited = service._body('manual-probe', 2)
        locks = service._locks('manual-probe')
        catalog = [dict(index=0, id=target_id, title='人工锁定标题', type='goal')]
        try:
            overwritten, _, _ = apply_compact(manually_edited, output([change('模型改写标题', 'update', target=0)]), packets, catalog, 'manual-update', locks=locks)
            actual_title = overwritten['items'][0]['title']
            check('manual_title_lock_is_preserved', actual_title == '人工锁定标题',
                  'a persisted human title lock survives model update (or update is rejected)',
                  dict(persisted_locks=len(locks), after_model_update=actual_title))
        except ValueError as exc:
            check('manual_title_lock_is_preserved', True, 'model update rejected on persisted human lock', str(exc))
    return checks


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    results = run()
    report = dict(date=date.today().isoformat(), probe_count=len(results), passed=sum(r['passed'] for r in results), probes=results)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
