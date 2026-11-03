"""Media is not textual evidence, including legacy replay and malformed envelopes."""
from sessiongraph.source_text import hydrate
import json
import pytest
from test_input_framework import fixture, Empty, msg
from test_graph_v2 import Judge
from sessiongraph import input_pipeline as p, graph_v2 as g


def mixed():
    return json.dumps([{'type':'input_text','text':'run_id=run_042 accuracy=0.913 artifact=/tmp/metrics.json'},
        {'type':'input_image','image_url':'data:image/jpeg;base64,'+'QkFC'*12000},
        {'type':'audio','source':{'type':'base64','media_type':'audio/wav','data':'UklG'*12000},'path':'/tmp/audio.wav'},
        {'type':'input_text','text':'论文 DOI=10.1234/example；结论有效'}],ensure_ascii=False)


def test_mixed_media_preserves_metrics_paths_and_source_offsets(tmp_path):
    raw=mixed();s,_=fixture(tmp_path,[{'type':'function_call_output','call_id':'c','output':raw}])
    ex=Empty();r=g.run_chat(s,ex,'input-chat',jev=Judge())
    sent='\n'.join(e['text'] for b in ex.calls for e in b)
    assert 'QkFC'*10 not in sent and 'UklG'*10 not in sent
    assert all(v in sent for v in ['run_042','0.913','/tmp/metrics.json','/tmp/audio.wav','10.1234/example'])
    assert r['model_calls']==1 and not r['failed']
    assert hydrate(s, s.db.execute('select * from entries').fetchone())['text']==raw
    for b in ex.calls:
        for e in b:assert all(fragment in raw for fragment in e['evidence_fragments'])
    assert g.run_chat(s,Empty(),'input-chat',jev=Judge())['model_calls']==0


def test_legacy_mid_base64_descriptor_rechecks_whole_source(tmp_path):
    raw=mixed();s,_=fixture(tmp_path,[{'type':'function_call_output','call_id':'c','output':raw}])
    row=hydrate(s, s.db.execute('select * from entries').fetchone());at=raw.index('QkFC')+1000
    restored=g._restore_batch(s,[p.row_descriptor(row,[[at,at+24000]])])
    assert not restored or not restored[0]['text']


@pytest.mark.parametrize('raw',[
    'prefix data:image/png;base64,'+'QUJD'*4000+' suffix metric=0.9',
    '{"type":"input_image","image_url":"data:image/png;base64,'+'QUJD'*4000,
    json.dumps({'type':'image','source':{'type':'base64','data':'QUJD'*4000},'path':'/tmp/img.png'}),
])
def test_media_removed_in_plain_malformed_and_typed_envelopes(tmp_path,raw):
    s,_=fixture(tmp_path,[msg(raw,'user')]);row=hydrate(s, s.db.execute('select * from entries').fetchone())
    spans,_=p.projection(s,row);selected='\n'.join(raw[a:b] for a,b in spans)
    assert 'QUJD'*10 not in selected
    if 'metric=0.9' in raw:assert 'metric=0.9' in selected
    if '/tmp/img.png' in raw:assert '/tmp/img.png' in selected


def test_ordinary_long_text_and_untyped_data_are_preserved(tmp_path):
    raw=json.dumps({'data':'测量值 '*5000,'accuracy':0.913,'hash':'a'*64})
    s,_=fixture(tmp_path,[msg(raw,'user')]);row=hydrate(s, s.db.execute('select * from entries').fetchone())
    spans,_=p.projection(s,row)
    assert ''.join(raw[a:b] for a,b in spans)==raw


def test_pure_image_needs_zero_model_calls(tmp_path):
    raw=json.dumps([{'type':'input_image','image_url':'data:image/png;base64,'+'QUJD'*9000}])
    s,_=fixture(tmp_path,[{'type':'function_call_output','call_id':'c','output':raw}])
    ex=Empty();r=g.run_chat(s,ex,'input-chat',jev=Judge())
    assert r['model_calls']==0 and not ex.calls and not r['graph_v2']['source_remaining']


def test_final_provider_boundary_blocks_binary_before_network():
    from sessiongraph.content_boundary import assert_text_only
    from sessiongraph.failures import Failure
    with pytest.raises(Failure,match='model_input_contains_binary'):
        assert_text_only({'entries':[{'text':mixed()}]})
    assert_text_only({'text':'accuracy=0.913','image':{'type':'image','path':'/tmp/image.png'}})


def test_legacy_pending_pure_binary_is_completed_without_provider(tmp_path,monkeypatch):
    raw='data:image/png;base64,'+'QUJD'*9000
    s,_=fixture(tmp_path,[{'type':'function_call_output','call_id':'c','output':raw}])
    # Construct the same durable legacy window using pre-fix selection/rendering.
    projection=p.projection;render=p.render
    monkeypatch.setattr(p,'projection',lambda store,row:([[0,len(row['text'])]],'legacy'))
    monkeypatch.setattr(p,'render',lambda row,d:dict(row,**{k:v for k,v in d.items() if k not in row}))
    first=g.run_chat(s,Empty(),'input-chat',jev=Judge(),call_budget=0)
    assert s.db.execute("select count(*) from sg2_jobs where state='pending'").fetchone()[0]==1
    monkeypatch.setattr(p,'projection',projection);monkeypatch.setattr(p,'render',render)
    # Remove failure control from the deliberately synthetic blocked window.
    s.db.execute('delete from sg2_control');s.db.commit()
    ex=Empty();r=g.run_chat(s,ex,'input-chat',jev=Judge(),pending_only=True)
    assert not ex.calls and r['model_calls']==0 and not r['failed']
    assert s.db.execute("select state from sg2_jobs").fetchone()[0]=='complete'


def test_data_uri_documentation_without_payload_is_not_binary():
    from sessiongraph.content_boundary import assert_text_only
    assert_text_only({"text": "工具示例 `data:image/jpeg;base64,...`，不包含图片数据"})


def test_judgment_context_projects_full_source_before_slicing(tmp_path):
    from sessiongraph.content_boundary import assert_text_only
    quote = "run_042 produces /tmp/result.json"
    raw = "data:image/png;base64," + "QUJD" * 4000 + "\n" + quote
    s, _ = fixture(tmp_path, [msg(raw, "user")])
    row = s.db.execute("select * from entries").fetchone()
    contexts = g._evidence_context(s, [{"evidence": [{"entry_id": row["id"], "quote": quote}]}])
    assert len(contexts) == 1 and quote in contexts[0]["context"]
    assert "QUJD" not in contexts[0]["context"]
    assert_text_only(contexts)
    assert hydrate(s, s.db.execute('select * from entries').fetchone())['text'] == raw


def test_targeted_pending_recovery_does_not_touch_other_jobs(tmp_path):
    s, _ = fixture(tmp_path, [msg("实验 run_042 输出 /tmp/metrics.json", "user")])
    g.run_chat(s, Empty(), "input-chat", jev=Judge(), call_budget=0)
    first = dict(s.db.execute("select * from sg2_jobs").fetchone())
    clone = dict(first, id="target-recovery-job")
    s.db.execute("insert into sg2_jobs values(" + ",".join("?" for _ in clone) + ")", tuple(clone.values()))
    s.db.commit()
    ex = Empty()
    result = g.run_chat(s, ex, "input-chat", jev=Judge(), pending_only=True, job_ids=[clone["id"]])
    assert not result["failed"] and len(ex.calls) == 1
    assert dict(s.db.execute("select * from sg2_jobs where id=?", (first["id"],)).fetchone()) == first
    assert s.db.execute("select state from sg2_jobs where id=?", (clone["id"],)).fetchone()[0] == "complete"
    assert s.db.execute("select count(*) from sg2_jobs").fetchone()[0] == 2
    with pytest.raises(ValueError, match="pending-only"):
        g.run_chat(s, ex, "input-chat", job_ids=[clone["id"]])
    with pytest.raises(ValueError, match="selected project and chat"):
        g.run_chat(s, ex, "input-chat", pending_only=True, job_ids=["foreign-job"])


def test_quoted_credential_fields_are_redacted_before_input(tmp_path):
    secret = 'fixture-value-123456'
    text = "配置 {'api_key': '" + secret + "'}；run_042 accuracy=0.913"
    s, _ = fixture(tmp_path, [msg(text, 'user')])
    ex = Empty()
    result = g.run_chat(s, ex, 'input-chat', jev=Judge())
    sent = '\n'.join(e['text'] for b in ex.calls for e in b)
    assert secret not in sent
    assert 'run_042 accuracy=0.913' in sent
    assert not result['failed']
    assert hydrate(s, s.db.execute('select * from entries').fetchone())['text'] == text


def test_generated_display_secret_is_redacted_without_losing_valid_nodes(tmp_path):
    from test_graph_v2 import output
    secret = 'fixture-value-123456'
    s, _ = fixture(tmp_path, [msg('run_042 produced artifact /tmp/result.json', 'user')])
    class Extract:
        def __call__(self, batch, policy):
            o = output(batch)
            o['nodes'][0]['content'] = 'run_042 配置 api_key=' + secret + '，结果保存在 /tmp/result.json'
            return o
    result = g.run_chat(s, Extract(), 'input-chat', jev=Judge())
    assert not result['failed'] and len(result['saved']) == 2
    content = '\n'.join(r[0] for r in s.db.execute('select content from work_memory'))
    assert secret not in content and '/tmp/result.json' in content
    assert result['graph_v2']['display_fields_redacted'] == 1


def test_redaction_never_rewrites_identity_or_source_evidence():
    from sessiongraph.graph_contract import redact_display_fields
    unsafe = 'api_key=fixture-value-123456'
    payload = {'nodes': [{'title': 'test', 'content': unsafe + ' failed', 'aliases': [],
                         'identity': [{'value': unsafe}], 'evidence': [{'quote': unsafe}]}]}
    cleaned, count = redact_display_fields(payload)
    assert count == 1 and unsafe not in cleaned['nodes'][0]['content']
    assert cleaned['nodes'][0]['identity'] == payload['nodes'][0]['identity']
    assert cleaned['nodes'][0]['evidence'] == payload['nodes'][0]['evidence']
    assert payload['nodes'][0]['content'] == unsafe + ' failed'


def test_failed_job_diagnostics_survive_retention_until_completion(tmp_path):
    import os
    from sessiongraph.request_audit import prune_diagnostics
    s, _ = fixture(tmp_path, [msg("run_042 produced a result", "user")])
    g.run_chat(s, Empty(), "input-chat", jev=Judge(), call_budget=0)
    job = s.db.execute("select id from sg2_jobs").fetchone()[0]
    folder = tmp_path / 'audit'; folder.mkdir()
    original = folder / 'request-original.json'
    original.write_text(json.dumps({'job_id': job, 'state': 'validation_failed'}))
    os.utime(original, (1, 1))
    for i in range(70):
        f = folder / f'request-complete-{i}.json'
        f.write_text(json.dumps({'job_id': 'finished', 'state': 'validation_failed'}))
        os.utime(f, (100+i, 100+i))
    prune_diagnostics(folder, s.config)
    assert original.exists() and len(list(folder.glob('*.json'))) == 65
    s.db.execute("update sg2_jobs set state='complete' where id=?", (job,));s.db.commit()
    prune_diagnostics(folder, s.config)
    assert not original.exists() and len(list(folder.glob('*.json'))) == 64


def test_malformed_judgment_retries_cached_extraction_without_deepseek(tmp_path):
    from test_graph_v2 import output
    s, _ = fixture(tmp_path, [msg('run_042 produced /tmp/result.json', 'user')])
    class Extract:
        calls = 0
        def __call__(self, batch, policy):
            self.calls += 1
            return output(batch)
    class BadOnce(Judge):
        def evaluate(self, state, questions):
            if not self.calls:
                self.calls += 1
                return {'answers': {}}
            return super().evaluate(state, questions)
    ex, judge = Extract(), BadOnce()
    first = g.run_chat(s, ex, 'input-chat', jev=judge)
    assert first['failed'][0]['error_code'] == 'jev_malformed_response'
    assert first['failed'][0]['retryable'] is True
    assert s.db.execute('select state from sg2_control').fetchone()[0] == 'retry_wait'
    second = g.run_chat(s, ex, 'input-chat', jev=judge, pending_only=True)
    assert not second['failed'] and ex.calls == 1
    assert second['graph_v2']['deepseek_calls'] == 0
    assert s.db.execute('select state from sg2_jobs').fetchone()[0] == 'complete'
