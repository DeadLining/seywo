"""Importer -> actual graph stages -> normal readers, with deterministic semantics."""
import json
from datetime import datetime, timezone

import pytest

from sessiongraph import graph_v2 as graph, revision_ledger as ledger
from sessiongraph.core import Store
from sessiongraph.single_token_judge import SingleTokenJudge
from test_memories import make_store, entries
from test_graph_v2 import ContextExtractor, Judge
from test_revision_ledger import review, apply


class RevisionJudge(Judge):
    def __init__(self, effect='corrects', **kwargs):
        super().__init__(**kwargs)
        self.effect = effect

    def evaluate(self, state, questions):
        if 'r0' not in questions:
            return super().evaluate(state, questions)
        self.calls += 1
        return {'answers': {'r0': {'choice': self.effect,
            'probabilities': {k: float(k == self.effect) for k in questions['r0']['criteria']}}}}


def document(entry, value, *, seed='seed42', provenance='direct', kind='Claim', title='evaluation'):
    atom = dict(subject='runA', predicate='success', value=value, unit='none', conditions=[seed, 'heldout'])
    return dict(schema_version=graph.VERSION, nodes=[dict(id='n1', type=kind, title=title,
        content=value, status='completed', aliases=[], identity=[], context_kind='work',
        assertion=[atom], provenance=provenance,
        evidence=[dict(entry_id=entry['id'], quote=entry['text'])])],
        relations=[], unresolved=[], coverage=dict(entry_ids=[entry['id']], outcome='candidates_found'))


@pytest.fixture
def env(tmp_path):
    _, s, path = make_store(tmp_path, [('assistant', 'runA success heldout seed42: pass; initial primary report.')])
    s.config['graph_v2'] = {'enabled': True, 'revision_policy': {'enabled': True, 'mode': 'effective'}}
    yield s, path
    s.close()


def run(s, value, judge=None, **kwargs):
    result = graph.run_chat(s, ContextExtractor([document(entries(s)[-1], value, **kwargs)]),
                            'chat-1', jev=judge or RevisionJudge())
    assert not result['failed'], result
    return result


def append(s, path, text, *, role='assistant'):
    with path.open('a') as stream:
        stream.write(json.dumps(dict(timestamp=datetime.now(timezone.utc).isoformat(), type='response_item',
            payload=dict(type='message', role=role, content=[dict(type='output_text', text=text)]))) + '\n')
    s.ingest(path)


def current(s):
    return s.recall('*', view='full', max_bytes=16000)['memories']


def test_actual_stages_correction_repeat_and_readonly_resume(env):
    s, path = env
    judge = RevisionJudge()
    run(s, 'pass', judge)
    append(s, path, 'runA success heldout seed42: correction, heldout was not measured.')
    run(s, 'not measured', judge)
    result = current(s)
    assert len(result) == 1 and result[0]['content'] == 'not measured'
    assert result[0]['validity']['state'] == 'current'
    assert judge.calls == 1
    append(s, path, 'Summary of earlier conversation: runA success heldout seed42: pass.')
    run(s, 'pass', judge, provenance='derived')
    assert current(s)[0]['content'] == 'not measured' and judge.calls == 1
    assert s.db.execute('SELECT count(*) FROM sg2_nodes').fetchone()[0] == 1
    assert s.db.execute('SELECT count(*) FROM sg_revision_items').fetchone()[0] == 2
    assert graph.run_chat(s, ContextExtractor([]), 'chat-1', jev=judge)['model_calls'] == 0
    readonly = Store(s.config, readonly=True)
    try:
        assert readonly.resume()['memories'][0]['content'] == 'not measured'
        assert readonly.recall('*')['memories'][0]['validity']['state'] == 'current'
    finally:
        readonly.close()


def test_distinct_conditions_do_not_merge_or_call_revision_judge(env):
    s, path = env
    judge = RevisionJudge()
    run(s, 'pass', judge)
    append(s, path, 'runA success heldout seed43: failed in this independent run.')
    run(s, 'failed', judge, seed='seed43')
    assert len(current(s)) == 2 and judge.calls == 0


def test_legacy_adoption_is_lazy_readonly_and_never_invents_atomic_identity(env):
    from test_revision_ledger import publish
    s, _ = env
    s.config['graph_v2'].pop('revision_policy')
    graph.setup(s)
    publish(s, entries(s)[0], 'legacy compound report')
    s.config['graph_v2']['revision_policy'] = {'enabled': True, 'mode': 'effective'}
    before = s.db.total_changes
    assert ledger.projected(s, 'subject') == dict(state='legacy_unassessed', revision=None, alternatives=[])
    assert s.db.total_changes == before
    graph.setup(s)
    with s.db:
        s.db.execute('BEGIN IMMEDIATE')
        ledger.baseline(s, 'subject')
        ledger.refresh(s, 'subject')
    assert ledger.projected(s, 'subject')['state'] == 'legacy_unassessed'
    assert s.db.execute('SELECT content FROM work_memory WHERE id="subject"').fetchone()[0] == 'legacy compound report'
    old = json.loads(s.db.execute('SELECT assertion_json FROM sg2_evidence WHERE node="subject"').fetchone()[0])
    atomic = document(entries(s)[0], 'pass')['nodes'][0]
    assert graph._conflict(old, atomic) and graph._conflict(atomic, old)
    assert not graph._conflict(old, old)


def test_ambiguous_changes_visible_and_not_implicitly_current(env):
    s, path = env
    run(s, 'pass')
    append(s, path, 'runA success heldout seed42: failed; observations disagree without a correction.')
    run(s, 'failed', RevisionJudge('disputes'))
    assert current(s)[0]['validity']['state'] == 'disputed'
    assert s.resume()['memories'][0]['validity']['state'] == 'disputed'


def test_correction_of_correction_no_timestamp_resurrection(env):
    s, path = env
    judge = RevisionJudge()
    run(s, 'pass', judge)
    append(s, path, 'runA success heldout seed42: correction, not measured.')
    run(s, 'not measured', judge)
    append(s, path, 'runA success heldout seed42: final correction, verified failed.')
    run(s, 'failed', judge)
    assert current(s)[0]['content'] == 'failed'
    assert current(s)[0]['validity']['state'] == 'current'
    assert s.db.execute('SELECT count(*) FROM sg_revision_items').fetchone()[0] == 3
    assert judge.calls == 2  # Compare active heads, not every historical pair.


def test_unanchored_scope_and_derived_new_entity_do_not_publish(env):
    s, _ = env
    run(s, 'pass', seed='seed99')
    assert current(s) == []


def test_derived_new_entity_preserves_pending_evidence_without_creating_fact(env):
    s, _ = env
    run(s, 'pass', provenance='derived')
    assert current(s) == []
    assert s.db.execute("SELECT count(*) FROM sg2_unresolved WHERE reason='derived_only'").fetchone()[0] == 1


def test_same_value_paraphrase_reuses_abstention_no_new_fact_or_model_call(env):
    s, path = env
    judge = RevisionJudge('insufficient_evidence')
    run(s, 'pass', judge)
    append(s, path, 'runA success heldout seed42: possibly not measured, no clear resolution.')
    run(s, 'not measured', judge)
    assert current(s)[0]['validity']['state'] == 'needs_review'
    assert judge.calls == 1
    append(s, path, 'Repeating the same primary report: runA success heldout seed42: not measured.')
    run(s, 'not measured', judge, title='paraphrased evaluation')
    assert judge.calls == 1
    assert s.db.execute('SELECT count(*) FROM sg_revision_items').fetchone()[0] == 2


def test_revision_judgment_names_the_exact_target_and_incoming_atomic_scope(env):
    s, path = env
    run(s, 'pass')
    append(s, path, 'runA success heldout seed42: correction, not measured.')

    class ScopedJudge(RevisionJudge):
        def evaluate(self, state, questions):
            if 'r0' in questions:
                scope = state['comparison_scope']
                assert scope['target_revision'].startswith('ar_')
                assert scope['target'] == state['previous']['assertion'][0]
                assert scope['incoming'] == state['incoming']['assertion'][0]
                assert scope['target']['value'] == 'pass'
                assert scope['incoming']['value'] == 'not measured'
                assert 'ONLY target is comparison_scope.target' in questions['r0']['instructions']
            return super().evaluate(state, questions)

    run(s, 'not measured', ScopedJudge())
    assert current(s)[0]['content'] == 'not measured'


def test_assistant_label_cannot_revoke_direct_user_decision(tmp_path):
    _, s, path = make_store(tmp_path, [('user', 'runA success heldout seed42: pass; use this constraint.')])
    s.config['graph_v2'] = {'enabled':True, 'revision_policy': {'enabled':True, 'mode':'effective'}}
    try:
        run(s, 'pass', kind='Decision')
        append(s, path, 'runA success heldout seed42: replace the user constraint with my proposal.')
        run(s, 'failed', RevisionJudge('replaces'), kind='Decision')
        assert current(s)[0]['validity']['state'] == 'needs_review'
        assert s.db.execute('SELECT count(*) FROM sg_revision_reviews').fetchone()[0] == 0
    finally:
        s.close()


def test_review_retract_restore_refreshes_real_readers(env):
    from test_review_control import manifest, commit
    s, path = env
    run(s, 'pass')
    append(s, path, 'runA success heldout seed42: correction, not measured.')
    run(s, 'not measured')
    r = s.db.execute('SELECT * FROM sg_revision_reviews').fetchone()
    first = commit(s, manifest(s, r['id'], r['source'], effective_from=ledger.stamp()))
    assert current(s)[0]['validity']['state'] == 'needs_review'
    commit(s, manifest(s, r['id'], r['source'], action='restore', previous=first['control_id'],
                       effective_from=ledger.stamp()))
    assert current(s)[0]['content'] == 'not measured'
    assert current(s)[0]['validity']['state'] == 'current'


def test_primary_source_change_invalidates_without_ledger_walk_on_read(env, monkeypatch):
    s, _ = env
    run(s, 'pass')
    with s.db:
        s.db.execute("UPDATE entries SET role='user' WHERE id=?", (entries(s)[0]['id'],))
    monkeypatch.setattr(ledger, 'inspect', lambda *a, **k: pytest.fail('read walked ledger'))
    assert current(s)[0]['validity']['state'] == 'needs_review'


def test_normal_reads_materials_and_native_map_preserve_original_and_current_context(env):
    from sessiongraph.summary_materials import candidates
    from sessiongraph.work_map import work_map
    s, path = env
    run(s, 'pass')
    append(s, path, 'runA success heldout seed42: correction, not measured.')
    run(s, 'not measured')
    out = candidates(s, dict(start='2026-09-25T00:00:00Z', end='2026-10-25T00:00:00Z'))
    old = next(x for x in out['assertions'] if x['content'] == 'pass')
    assert old['assertion_state'] == 'not_current'
    assert old['current_validity']['state'] == 'current'
    assert old['current_validity']['revision'] == current(s)[0]['validity']['revision']
    mapped = work_map(s)
    node = next(n for n in mapped['nodes'] if n['id'] == current(s)[0]['id'])
    assert node['content'] == 'not measured' and node['validity']['state'] == 'current'


def test_default_configuration_keeps_legacy_shadow_behavior(env):
    s, path = env
    s.config['graph_v2']['revision_policy'].pop('mode')
    run(s, 'pass')
    append(s, path, 'runA success heldout seed42: a later report not measured.')
    run(s, 'not measured')
    assert not s.db.execute("SELECT 1 FROM sqlite_master WHERE name='sg_revision_projection'").fetchone()
    assert all('validity' not in m for m in current(s))


def test_label_only_backend_uses_real_single_token_transport_and_no_fake_probability(env, monkeypatch):
    s, path = env
    s.config['provider'].update(protocol='responses', endpoint='https://fixture.invalid/v1/responses', model='fixture', auth='none')
    s.config['graph_v2'].update(judge_backend='single_token', single_token_judge={'request_logprobs': False})
    seen = []
    def response(client, request, handlers):
        body = json.loads(request.data)
        seen.append(body)
        assert body['max_output_tokens'] == 1
        return json.dumps(dict(status='completed', model='fixture', usage={'input_tokens':100, 'output_tokens':1},
            output=[dict(type='message', role='assistant', content=[dict(type='output_text', text='A')])])).encode()
    monkeypatch.setattr('sessiongraph.single_token_judge.read_response', response)
    run(s, 'pass')
    append(s, path, 'runA success heldout seed42: correction, not measured.')
    result = graph.run_chat(s, ContextExtractor([document(entries(s)[-1], 'not measured')]), 'chat-1')
    assert not result['failed'], result
    assert len(seen) == 1 and current(s)[0]['content'] == 'not measured'
    cached = [json.loads(r[0]) for r in s.db.execute("SELECT answer_json FROM sg2_judgments WHERE stage='revision'")]
    assert cached[0]['answers']['r0']['score_kind'] == 'label_only'
    assert 'probabilities' not in cached[0]['answers']['r0']


def test_label_marker_cannot_bypass_probability_backend_threshold():
    answer = dict(choice='same', probabilities={'same':.6, 'different':.3, 'insufficient_evidence':.1},
                  score_kind='label_only', abstained=False)
    assert not graph._accept(answer, 'same', .98)


def test_judge_rollback_keeps_effective_state_and_all_new_writes(env, monkeypatch):
    s, path = env
    s.config['provider'].update(protocol='responses', endpoint='https://fixture.invalid/v1/responses', model='fixture', auth='none')
    s.config['graph_v2'].update(judge_backend='single_token', single_token_judge={'request_logprobs':False})
    calls = []
    def response(client, request, handlers):
        calls.append(json.loads(request.data))
        return json.dumps(dict(status='completed', model='fixture', usage={'output_tokens':1},
            output=[dict(type='message', role='assistant', content=[dict(type='output_text', text='A')])])).encode()
    monkeypatch.setattr('sessiongraph.single_token_judge.read_response', response)
    run(s, 'pass')
    append(s, path, 'runA success heldout seed42: correction, not measured.')
    result = graph.run_chat(s, ContextExtractor([document(entries(s)[-1], 'not measured')]), 'chat-1')
    assert not result['failed'] and len(calls) == 1
    # Supported rollback changes the judge, not the revision-aware reader/writer.
    s.config['graph_v2'].pop('judge_backend')
    s.config['graph_v2'].pop('single_token_judge')
    assert ledger.effective(s.config) and current(s)[0]['content'] == 'not measured'
    append(s, path, 'runA success heldout seed42: correction of correction, verified failed.')
    run(s, 'failed', RevisionJudge())
    assert current(s)[0]['content'] == 'failed' and current(s)[0]['validity']['state'] == 'current'
    assert s.db.execute('SELECT count(*) FROM sg_revision_items').fetchone()[0] == 3
    assert s.db.execute('SELECT count(*) FROM sg_revision_reviews').fetchone()[0] == 2
    assert s.db.execute('SELECT count(*) FROM sg_revision_sources').fetchone()[0] == 3
    assert len(calls) == 1


def test_future_review_cannot_silently_turn_stale_projection_into_truth(env, monkeypatch):
    s, path = env
    run(s, 'pass')
    append(s, path, 'runA success heldout seed42: correction planned for tomorrow, not measured.')
    run(s, 'not measured', RevisionJudge('insufficient_evidence'))
    versions = {json.loads(r['assertion_json'])['content']:r['id'] for r in s.db.execute('''
        SELECT r.id,e.assertion_json FROM sg_revision_items r JOIN sg2_evidence e ON e.id=r.payload_evidence''')}
    apply(s, review(s, versions['not measured'], versions['pass'], facet='success',
                    conditions={'qualifiers':['heldout','seed42']}, effective_from='2099-01-01T00:00:00Z'))
    assert current(s)[0]['validity']['state'] == 'needs_review'
    monkeypatch.setattr(ledger, 'stamp', lambda: '2099-01-02T00:00:00+00:00')
    assert current(s)[0]['validity']['state'] == 'needs_review'


def test_policy_only_extends_wire_contract_for_effective_mode():
    from sessiongraph.graph_contract import policy
    legacy = policy([])['json_schema']['properties']['nodes']['items']
    assert 'assertion' not in legacy['properties'] and 'provenance' not in legacy['properties']
    effective = policy([], effective=True)['json_schema']['properties']['nodes']['items']
    assert {'assertion','provenance'} <= set(effective['required'])
