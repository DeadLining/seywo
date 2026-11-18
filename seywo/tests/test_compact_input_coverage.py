"""Budgeting must not turn partial model input into full-turn publication."""
from collections import defaultdict

import pytest

from seywo.compact_model import MAX_ASSISTANT_CHARS, MAX_INPUT_CHARS, MAX_TURNS, MAX_USER_CHARS, request_payload
from seywo.compact_pipeline import _batch_turns, _payload_turns, apply_compact
from seywo.narrative import empty
from test_compact_pipeline import source, turn, output


def scheduled(turns):
    return [part for batch in _batch_turns(turns) for part in batch]


@pytest.mark.parametrize('role,limit', [('user', MAX_USER_CHARS), ('assistant', MAX_ASSISTANT_CHARS)])
def test_every_character_in_oversized_native_turn_reaches_the_model_once(role, limit):
    # The marker deliberately has no modal keywords; relevance must not be guessed.
    text = '首段。' * limit + '月结口径改为自然月。' + '尾段。' * limit
    native = turn('native', text if role == 'user' else '', text if role == 'assistant' else '',
                  source(1, role, text, turn='native'))
    batches = _batch_turns([native])
    payloads = [request_payload(_payload_turns(batch), {}, []) for batch in batches]
    offered = [row for payload in payloads for row in payload['turns']]
    assert ''.join(row[role] for row in offered) == text
    assert all(len(row[role]) <= limit for row in offered)
    assert [row['part'] for row in offered] == [[n + 1, len(offered)] for n in range(len(offered))]
    assert {part['turn_id'] for batch in batches for part in batch} == {'native'}


def test_slices_preserve_source_offsets_and_role_chronology():
    user_a, user_b = ' a\n' * 650, '指定结算日为每月第3个工作日。' * 140
    answer = '结果\n  a = b\n' * 550
    native = turn('native', user_a + '\n' + user_b, answer,
                  source(1, 'user', user_a), source(2, 'user', user_b), source(3, 'assistant', answer))
    originals = {ref['event_id']: ref['text'] for ref in native['sources']}
    coverage = defaultdict(list)
    parts = scheduled([native])
    for part in parts:
        for ref in part['sources']:
            assert ref['text'] == originals[ref['event_id']][ref['start']:ref['end']]
            coverage[ref['event_id']].append((ref['start'], ref['end']))
        if part['assistant']:
            assert all(not later['user'] for later in parts[parts.index(part) + 1:])
    for identity, text in originals.items():
        ranges = coverage[identity]
        assert ranges[0][0] == 0 and ranges[-1][1] == len(text)
        assert all(left[1] == right[0] for left, right in zip(ranges, ranges[1:]))
    assert ''.join(part['user'] for part in parts) == native['user']
    assert ''.join(part['assistant'] for part in parts) == answer


def test_ordinary_native_turn_is_unchanged_including_whitespace():
    native = turn('native', '第一行\n\n  第二行\t末尾 ', '答复\n  代码\n',
                  source(1, 'user', '第一行\n\n  第二行\t末尾 '), source(2, 'assistant', '答复\n  代码\n'))
    assert scheduled([native]) == [native]
    payload = request_payload(_payload_turns([native]), {}, [])
    assert payload['turns'] == [{'index': 0, 'user': native['user'], 'assistant': native['assistant']}]


def test_budget_applies_to_actual_slices_and_preserves_native_order():
    turns = [turn(str(n), '原文' * (MAX_USER_CHARS + n), '回答' * (MAX_ASSISTANT_CHARS + n),
                  source(n * 2, 'user', '原文' * (MAX_USER_CHARS + n)),
                  source(n * 2 + 1, 'assistant', '回答' * (MAX_ASSISTANT_CHARS + n))) for n in range(9)]
    batches = _batch_turns(turns)
    assert all(len(batch) <= MAX_TURNS for batch in batches)
    assert all(sum(len(t['user']) + len(t['assistant']) for t in batch) <= MAX_INPUT_CHARS for batch in batches)
    parts = [part for batch in batches for part in batch]
    assert list(dict.fromkeys(part['turn_id'] for part in parts)) == [str(n) for n in range(9)]
    for native in turns:
        matching = [p for p in parts if p['turn_id'] == native['turn_id']]
        for role in ('user', 'assistant'):
            assert ''.join(p[role] for p in matching) == native[role]


@pytest.mark.parametrize('role,limit', [('user', MAX_USER_CHARS), ('assistant', MAX_ASSISTANT_CHARS)])
def test_unscheduled_oversize_input_is_rejected_instead_of_silently_clipped(role, limit):
    with pytest.raises(ValueError, match='compact_input_requires_scheduling'):
        request_payload([{'index': 0, role: '原' * (limit + 1)}], {}, [])


def test_publication_evidence_only_covers_the_slice_seen_by_model():
    text = '记录需求。' * 500
    native = turn('native', text, '', source(1, 'user', text))
    first = scheduled([native])[0]
    doc, _, _ = apply_compact(empty(), output(title='记录需求', item_type='goal', open=True), [first], [], 'slice')
    evidence = doc['items'][0]['evidence']
    assert len(evidence) == 1
    assert evidence[0]['start'] == 0
    assert evidence[0]['end'] == len(first['user']) < len(text)


def test_native_turn_that_fits_one_batch_is_not_split_across_calls():
    prefix = [turn(str(n), '前' * 1000, '答' * 2000,
                   source(n * 2, 'user', '前' * 1000), source(n * 2 + 1, 'assistant', '答' * 2000))
              for n in range(5)]
    native = turn('long', '说明。' * 500, '答复。' * 1500,
                  source(20, 'user', '说明。' * 500), source(21, 'assistant', '答复。' * 1500))
    batches = _batch_turns(prefix + [native])
    assert len([batch for batch in batches if any(p['turn_id'] == 'long' for p in batch)]) == 1


def test_slices_prefer_sentence_boundaries_without_changing_text():
    user = '说明。' * 399 + '这是完整的中间约束。' + '后续。' * 400
    native = turn('native', user, '', source(1, 'user', user))
    parts = scheduled([native])
    assert parts[0]['user'].endswith('。')
    assert ''.join(p['user'] for p in parts) == user


@pytest.mark.parametrize('text', ['已完成', '已处理'])
def test_completion_updates_are_not_discarded_as_polite_acknowledgements(text):
    from seywo.compact_pipeline import _turn_packets
    packets = _turn_packets([source(0, 'user', text), source(1, 'assistant', '收到')])
    assert len(packets) == 1
    assert packets[0]['user'] == text


@pytest.mark.parametrize('user,assistant', [('可以', '收到'), ('好的', '好的'), ('继续', '好的')])
def test_rules_cannot_decide_whether_short_dialogue_changes_a_prior_decision(user, assistant):
    from seywo.compact_pipeline import _turn_packets
    packets = _turn_packets([source(0, 'user', user), source(1, 'assistant', assistant)])
    assert len(packets) == 1


def test_semantic_contract_distinguishes_receipts_from_status_and_decision_changes():
    from seywo.compact_model import instructions
    assert '状态变更' in instructions()
    assert '上下文' in instructions()
