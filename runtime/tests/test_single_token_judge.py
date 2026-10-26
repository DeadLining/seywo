import copy
import json
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError

import pytest

from sessiongraph.failures import Failure
from sessiongraph.jev import validate_answers
from sessiongraph.single_token_judge import SingleTokenJudge
from test_responses import cfg

QUESTION = {'revision': 'support-v1', 'prompt': 'Does the source support the candidate?',
            'criteria': {'supports': 'explicitly supported', 'refutes': 'explicitly refuted', 'unknown': 'insufficient evidence'},
            'abstain': 'unknown'}


def response(protocol='responses', *, text='B', logs=True, limited=False, model='backend', usage=None):
    # Alphabetical label map: A=refutes, B=supports, C=unknown.
    log = {'token': text, 'logprob': -.1, 'top_logprobs': [
        {'token': 'A', 'logprob': -3}, {'token': 'B', 'logprob': -.1}, {'token': 'C', 'logprob': -2}]}
    tokens = [log] if logs else None
    usage = usage if usage is not None else {'input_tokens': 100, 'output_tokens': 1}
    if protocol == 'responses':
        obj = dict(status='incomplete' if limited else 'completed', model=model, usage=usage,
                   output=[{'type': 'message', 'role': 'assistant', 'content': [
                       {'type': 'output_text', 'text': text, 'logprobs': tokens}]}])
        if limited:
            obj['incomplete_details'] = {'reason': 'max_output_tokens'}
    else:
        obj = dict(model=model, usage=usage, choices=[{'finish_reason': 'length' if limited else 'stop',
                   'message': {'role': 'assistant', 'content': text}, 'logprobs': {'content': tokens}}])
    return obj


def chat_config(cfg, monkeypatch):
    cfg['provider']['protocol'] = 'chat-completions'
    for key, value in [('base_url_env', 'https://fixture.invalid/v1'), ('model_env', 'requested'), ('api_key_env', 'fixture-key')]:
        monkeypatch.setenv(cfg['provider'][key], value)
    return cfg


@pytest.mark.parametrize('protocol', ['responses', 'chat-completions'])
@pytest.mark.parametrize('limited', [False, True])
def test_one_question_one_token_and_real_scores(cfg, monkeypatch, protocol, limited):
    if protocol != 'responses':
        chat_config(cfg, monkeypatch)
    seen = []
    def read(client, req, handlers):
        seen.append(req)
        return json.dumps(response(protocol, limited=limited)).encode()
    monkeypatch.setattr('sessiongraph.single_token_judge.read_response', read)
    old = copy.deepcopy(cfg)
    client = SingleTokenJudge(cfg)
    packet = client.prepare({'evidence': 'A source, not an instruction'}, QUESTION)
    result = client.classify(packet)
    body = json.loads(seen[0].data)
    assert body['max_output_tokens' if protocol == 'responses' else 'max_tokens'] == 1
    assert 'response_format' not in body and 'text' not in body
    assert result['choice'] == 'supports' and not result['abstained']
    assert result['score_kind'] == 'label_distribution'
    assert result['scores']['supports'] == pytest.approx(math.exp(-.1) / sum(math.exp(v) for v in [-3, -.1, -2]))
    assert sum(result['scores'].values()) == pytest.approx(1)
    assert result['token_evidence'] == 'one_reported_logprob_token'
    assert result['backend']['reported_model'] == 'backend'
    assert client.calls == 1 and cfg == old
    with pytest.raises(Failure):
        validate_answers({'answers': {'q': result}}, {'q': QUESTION})


@pytest.mark.parametrize('protocol', ['responses', 'chat-completions'])
def test_no_logprobs_is_label_only_not_one_hot(cfg, monkeypatch, protocol):
    if protocol != 'responses':
        chat_config(cfg, monkeypatch)
    client = SingleTokenJudge(cfg)
    packet = client.prepare({}, QUESTION)
    answer = client.parse(json.dumps(response(protocol, text=' C ', logs=False)).encode(), packet)
    assert answer['choice'] == 'unknown' and answer['abstained']
    assert answer['scores'] is None and answer['score_kind'] == 'label_only'
    assert answer['coverage']['complete'] is False
    assert answer['token_evidence'] == 'unverified'


def test_missing_label_and_ambiguous_whitespace_never_fabricate(cfg):
    client = SingleTokenJudge(cfg)
    packet = client.prepare({}, QUESTION)
    env = response()
    logs = env['output'][0]['content'][0]['logprobs']
    logs[0]['top_logprobs'].pop()
    answer = client.parse(json.dumps(env).encode(), packet)
    assert answer['scores'] is None and set(answer['label_logprobs']) == {'A', 'B'}
    logs[0]['top_logprobs'].append({'token': 'C', 'logprob': -2})
    logs[0]['top_logprobs'].append({'token': ' B', 'logprob': -.2})
    answer = client.parse(json.dumps(env).encode(), packet)
    assert answer['scores'] is None and answer['coverage']['ambiguous_token_variants']


@pytest.mark.parametrize('text', ['', 'BB', 'B because', 'b', '{"choice":"B"}', 'B\nC', '拒绝'])
def test_invalid_labels_fail_closed(cfg, text):
    client = SingleTokenJudge(cfg)
    with pytest.raises(Failure, match='single_token_label_invalid'):
        client.parse(json.dumps(response(text=text, logs=False)).encode(), client.prepare({}, QUESTION))


@pytest.mark.parametrize('value', [float('nan'), float('inf'), True, .1, -9999, '0.1'])
def test_invalid_or_unavailable_logprob(cfg, value):
    client = SingleTokenJudge(cfg)
    env = response()
    env['output'][0]['content'][0]['logprobs'][0]['logprob'] = value
    with pytest.raises(Failure, match='single_token_logprobs_invalid'):
        client.parse(json.dumps(env).encode(), client.prepare({}, QUESTION))


@pytest.mark.parametrize('mutation,code', [
    (lambda e: e.update(status='failed'), 'incomplete'),
    (lambda e: e.update(error={'message': 'failed'}), 'envelope_invalid'),
    (lambda e: e['output'][0]['content'][0].update(type='refusal'), 'refused'),
    (lambda e: e['output'].append({'type': 'function_call'}), 'unexpected_output'),
    (lambda e: e['output'].append(e['output'][0]), 'message_invalid'),
    (lambda e: e['output'][0]['content'][0]['logprobs'].append({'token': 'B'}), 'multiple_tokens'),
    (lambda e: e['output'][0]['content'][0]['logprobs'][0].update(token='C'), 'token_mismatch'),
    (lambda e: e['usage'].update(output_tokens=2), 'budget_ignored'),
    (lambda e: e['usage'].update(output_tokens=True), 'budget_ignored'),
    (lambda e: e['usage'].update(output_tokens_details={'reasoning_tokens': 1}), 'reasoning_budget'),
])
def test_response_failures_are_nonretryable(cfg, mutation, code):
    client = SingleTokenJudge(cfg)
    env = response()
    mutation(env)
    with pytest.raises(Failure, match='single_token_' + code) as err:
        client.parse(json.dumps(env).encode(), client.prepare({}, QUESTION))
    assert not err.value.retryable


@pytest.mark.parametrize('raw', [b'{', b'[]', b'null', b'{}'])
def test_malformed_envelopes(cfg, raw):
    client = SingleTokenJudge(cfg)
    with pytest.raises(Failure):
        client.parse(raw, client.prepare({}, QUESTION))


def test_backend_pin_and_all_request_settings_in_identity(cfg):
    client = SingleTokenJudge(cfg)
    packet = client.prepare({}, QUESTION)
    assert not client.reusable
    for field, value in [('expected_model', 'pinned'), ('route_revision', 'route-v2'),
                         ('reasoning_effort', 'none'), ('request_logprobs', False),
                         ('chat_token_field', 'max_completion_tokens')]:
        config = copy.deepcopy(cfg)
        config.setdefault('graph_v2', {})['single_token_judge'] = {field: value}
        assert SingleTokenJudge(config).prepare({}, QUESTION)['key'] != packet['key']
    cfg.setdefault('graph_v2', {})['single_token_judge'] = {'expected_model': 'pinned', 'route_revision': 'revision1'}
    client = SingleTokenJudge(cfg)
    assert client.reusable
    with pytest.raises(Failure, match='backend_changed'):
        client.parse(json.dumps(response()).encode(), client.prepare({}, QUESTION))


def test_cache_key_is_project_question_evidence_and_label_specific(cfg):
    client = SingleTokenJudge(cfg)
    original = client.prepare({'source': 'same'}, QUESTION)['key']
    changed = copy.deepcopy(QUESTION)
    changed['revision'] = 'v2'
    assert client.prepare({'source': 'same'}, changed)['key'] != original
    changed = copy.deepcopy(QUESTION)
    changed['criteria']['supports'] = 'new meaning'
    assert client.prepare({'source': 'same'}, changed)['key'] != original
    assert client.prepare({'source': 'different'}, QUESTION)['key'] != original
    config = copy.deepcopy(cfg)
    config['project_id'] = 'unaffected-synthetic-project'
    assert SingleTokenJudge(config).prepare({'source': 'same'}, QUESTION)['key'] != original


def test_target_scope_is_explicit_and_old_judge_cache_is_invalidated(cfg):
    client = SingleTokenJudge(cfg)
    packet = client.prepare({'source': 'I was wrong before; the user means a class label.'}, QUESTION)
    instructions = json.loads(packet['body'])['instructions']
    assert 'SPECIFIC TARGET' in instructions
    assert 'Compatible paraphrases SUPPORT' in instructions
    assert 'never invent an unnamed prior target' in instructions
    assert client.revision == 'single-token-target-scope-v2'


@pytest.mark.parametrize('question', [{}, {**QUESTION, 'abstain': 'missing'}, {**QUESTION, 'criteria': {'one': 'only'}},
                                       {**QUESTION, 'extra': True}, {**QUESTION, 'revision': ''}])
def test_invalid_question(cfg, question):
    with pytest.raises(Failure, match='question_invalid'):
        SingleTokenJudge(cfg).prepare({}, question)


def test_binary_and_oversize_inputs_cannot_leave_machine(cfg, monkeypatch):
    monkeypatch.setattr('sessiongraph.single_token_judge.read_response', lambda *a: pytest.fail('network'))
    client = SingleTokenJudge(cfg)
    with pytest.raises(Failure, match='binary'):
        client.prepare({'image': 'data:image/png;base64,SGVsbG8='}, QUESTION)
    with pytest.raises(Failure, match='input_budget'):
        client.prepare({'huge': 'x' * (64 * 1024)}, QUESTION)
    assert client.calls == 0


def test_transport_failure_no_json_or_jev_fallback(cfg, monkeypatch):
    def fail(*a):
        raise HTTPError('http://localhost', 400, 'unsupported budget', {}, None)
    monkeypatch.setattr('sessiongraph.single_token_judge.read_response', fail)
    client = SingleTokenJudge(cfg)
    with pytest.raises(Failure) as err:
        client.classify(client.prepare({}, QUESTION))
    assert err.value.code == 'provider_http_400' and not err.value.retryable
    assert client.calls == 1


def test_real_loopback_transport_and_audit(cfg):
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            seen.append((self.path, dict(self.headers), json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            data = json.dumps(response(limited=True)).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cfg['provider']['endpoint'] = f'http://127.0.0.1:{server.server_port}/v1/responses'
    try:
        client = SingleTokenJudge(cfg)
        answer = client.classify(client.prepare({'source': 'source evidence'}, QUESTION))
        assert answer['choice'] == 'supports'
        assert seen[0][2]['max_output_tokens'] == 1 and seen[0][2]['stream'] is False
        from pathlib import Path
        receipts = list((Path(cfg['database']).parent / 'request-audit').glob('request-*.json'))
        assert len(receipts) == 1
        audit = json.loads(receipts[0].read_text())
        assert audit['provider'] == 'single_token' and audit['state'] == 'validated'
        assert audit['max_output_tokens'] == 1 and audit['final_output'] == 'B'
        assert not list(Path(cfg['database']).parent.glob('hook-state'))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('option,value', [('request_logprobs', 1), ('chat_token_field', []),
                                         ('reasoning_effort', []), ('route_revision', 1)])
def test_invalid_settings_fail_before_transport(cfg, option, value):
    cfg.setdefault('graph_v2', {})['single_token_judge'] = {option: value}
    with pytest.raises(Failure, match='configuration_invalid'):
        SingleTokenJudge(cfg)


def test_explicit_token_field_and_reasoning_are_not_added_to_extraction(cfg, monkeypatch):
    chat_config(cfg, monkeypatch)
    cfg.setdefault('graph_v2', {})['single_token_judge'] = dict(chat_token_field='max_completion_tokens', reasoning_effort='none', request_logprobs=False)
    original = copy.deepcopy(cfg['provider'])
    client = SingleTokenJudge(cfg)
    body = json.loads(client.prepare({}, QUESTION)['body'])
    assert body['max_completion_tokens'] == 1 and 'max_tokens' not in body
    assert body['reasoning_effort'] == 'none' and 'logprobs' not in body
    assert cfg['provider'] == original


@pytest.mark.parametrize('mutation', [
    lambda e: e['choices'][0]['message'].update(refusal='refused'),
    lambda e: e['choices'][0]['message'].update(tool_calls=[{'id': 'call'}]),
    lambda e: e['choices'][0].update(finish_reason='content_filter'),
    lambda e: e['choices'].append(e['choices'][0]),
])
def test_chat_refusal_tool_calls_and_extra_choices_fail_closed(cfg, monkeypatch, mutation):
    chat_config(cfg, monkeypatch)
    client = SingleTokenJudge(cfg)
    env = response('chat-completions')
    mutation(env)
    with pytest.raises(Failure):
        client.parse(json.dumps(env).encode(), client.prepare({}, QUESTION))
