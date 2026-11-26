"""Provider guidance must agree with the enforced indexed delta contract."""
import json

import pytest
from jsonschema import Draft202012Validator, ValidationError

from seywo.model_contract import responses_body
from seywo.narrative import Narrative
from seywo.wire_schema import response_schema
from test_s2 import setup as setup, message


def test_provider_schema_rejects_envelope_and_wrong_nested_fields(setup):
    _, engine, options, *_ = setup
    job = engine.prepare('chat', options)['job_id']
    request = engine.claim(job, backend=options.backend)['request']
    body = responses_body(request)
    inline = json.loads(body['input'])['output_schema']
    assert inline == response_schema(request)
    validator = Draft202012Validator(inline)
    minimal = {'items': [{'type': 'question', 'title': '核对剩余事项', 'source_indexes': [0]}]}
    validator.validate(minimal)
    for invalid in [dict(minimal, phase='extract'), dict(minimal, overview={}),
                    {'items': [{'type': 'question', 'title': '核对剩余事项', 'source_indexes': [0.5]}]},
                    {'items': [{'type': 'question', 'title': '核对剩余事项', 'source_indexes': [0], 'evidence': []}]}]:
        with pytest.raises(ValidationError):
            validator.validate(invalid)
    assert body['text']['format'] == {'type': 'json_object'}  # provider schema enforcement is not assumed


def test_explanatory_question_publishes_without_fabricated_execution_goal(setup):
    store, engine, options, path, write = setup
    write([message('当前卡在哪个流程？')])
    store.import_source(path, 'chat', 0)
    job = engine.prepare('chat', options)['job_id']
    a = engine.claim(job, backend=options.backend)
    result = engine.submit(a['attempt_id'], backend=options.backend, output={
        'items': [{'type': 'question', 'title': '定位当前流程卡点', 'source_indexes': [0]}]})
    assert result['error_code'] is None
    service = Narrative(store)
    task = service.prepare(job)
    b = engine.claim(job, backend=options.backend)
    delta = {'phases': [{'title': '澄清当前卡点', 'goal_item_indexes': [0]}],
             'placements': [{'item_index': 0, 'phase_index': 0}],
             'overview': {'focus': {'phase_index': 0, 'branch_index': None},
                          'goal_item_indexes': [0], 'next_step_indexes': []}}
    Draft202012Validator(response_schema(b['request'])).validate(delta)
    with pytest.raises(ValidationError):
        Draft202012Validator(response_schema(b['request'])).validate(dict(delta, items=[]))
    assert engine.submit(b['attempt_id'], backend=options.backend, output=delta)['error_code'] is None
    doc = service.publish(task['task_id'])['narrative']
    assert [item['type'] for item in doc['items']] == ['question']
    assert doc['overview']['next_step_ids'] == []
    assert not doc['branches']


def test_upgrade_from_index4_preserves_spend_and_invalidates_old_lease(setup):
    from seywo.store import digest, encode
    store, engine, options, *_ = setup
    old = engine.prepare('chat', options)['job_id']
    lease = engine.claim(old, backend=options.backend)
    request = json.loads(store.db.execute('SELECT request_json FROM understanding_tasks WHERE id=?',
                                          (lease['task_id'],)).fetchone()[0])
    request['indexed']['policy'] = 'seywo-index-4'
    target = digest(encode({'chat': 'chat', 'heads': engine._heads('chat'), 'base': options.base_version,
                           'override': options.override_revision, 'model': options.model,
                           'model_policy': 'seywo-index-4'}))
    with store.db:
        store.db.execute('UPDATE understanding_tasks SET request_json=? WHERE id=?',
                         (encode(request), lease['task_id']))
        store.db.execute('UPDATE understanding_jobs SET target_hash=? WHERE id=?', (target, old))
    replacement = engine.prepare('chat', options)['job_id']
    assert replacement != old
    assert engine.status(replacement)['usage']['call_count'] == 0
    assert engine.status(old)['usage']['call_count'] == 1
    assert engine.claim(replacement, backend=options.backend)['request']['protocol'] == 'seywo-index-5'
    assert store.db.execute('SELECT count(*) FROM narrative_versions').fetchone()[0] == 0


@pytest.mark.parametrize('question_after', [True, False])
def test_later_explanation_can_focus_same_phase_but_old_question_cannot_hide_new_action(setup, question_after):
    store, engine, options, path, write = setup
    first, second = ('修复导出', '为什么采用这个校验方式？') if question_after else ('为什么采用这个校验方式？', '修复导出')
    write([message(first), message(second)])
    store.import_source(path, 'chat', 0)
    job = engine.prepare('chat', options)['job_id']
    while True:
        a = engine.claim(job, backend=options.backend)
        if a['state'] != 'request':
            break
        items = [{'type': 'question' if '为什么' in s['text'] else 'goal',
                  'title': '澄清导出校验原理' if '为什么' in s['text'] else '修复导出功能',
                  'source_indexes': [s['index']]} for s in a['request']['sources'] if s['purpose'] == 'primary']
        engine.submit(a['attempt_id'], backend=options.backend, output={'items': items})
    service = Narrative(store)
    task = service.prepare(job)
    b = engine.claim(job, backend=options.backend)
    # Use the source ordering used for actual bounded-history organization.
    from seywo.model_contract import freeze, hydrate
    from seywo.windows import goal_orders
    private = json.loads(store.db.execute('SELECT request_json FROM understanding_tasks WHERE id=?',
                                          (b['task_id'],)).fetchone()[0])
    private['user_goal_order'] = goal_orders(private, store.db)
    private['semantic_stop_policy'] = 'seywo-stop-intent-2'
    private = freeze({k: v for k, v in private.items() if k not in {'indexed', 'input_fingerprint'}})
    question = next(i['index'] for i in b['request']['items'] if i['type'] == 'question')
    delta = {'phases': [{'title': '完善导出', 'goal_item_indexes': [question]}],
             'placements': [{'item_index': i['index'], 'phase_index': 0} for i in b['request']['items']],
             'overview': {'focus': {'phase_index': 0, 'branch_index': None},
                          'goal_item_indexes': [question], 'next_step_indexes': []}}
    if question_after:
        assert hydrate(delta, private)['overview']['goal_item_ids']
    else:
        with pytest.raises(ValueError, match='goal_requires_work_item'):
            hydrate(delta, private)
    assert task['task_id']


def test_previous_index4_narrative_is_rejected_without_rewriting_it(setup):
    from test_s3 import publish
    from test_s2 import drain
    store, engine, _, service, _, _, view = publish(setup)
    old = view['narrative']
    old['model_policy'] = 'seywo-index-4'
    with store.db:
        import hashlib
        import zlib
        text = json.dumps(old, ensure_ascii=False, sort_keys=True)
        store.db.execute("UPDATE narrative_revisions SET base_version=NULL,depth=0,body=?,body_hash=? WHERE chat_id=? AND version=?",
                         (zlib.compress(text.encode()), hashlib.sha256(text.encode()).hexdigest(), 'chat', view['version']))
        from seywo.document_store import register
        register(store.db)
    setup[4]([message('补充解释当前校验'), message('核对继续', 'assistant')])
    store.import_source(setup[3], 'chat', 0)
    job = engine.prepare('chat', setup[2])['job_id']
    drain(engine, job)
    with pytest.raises(ValueError, match='previous_model_policy_incompatible'):
        service.prepare(job)
    assert store.db.execute('SELECT max(version) FROM narrative_versions').fetchone()[0] == view['version']
