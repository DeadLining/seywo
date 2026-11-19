"""Protocol-level tests for Seywo's Codex app-server worker."""
import json
from pathlib import Path
import sys

from seywo.codex_app_server import CodexAppServerWorker


def fake_app_server(tmp_path, *, mode='ok'):
    script = tmp_path / 'fake-app-server.py'
    script.write_text(r'''
import json, os, sys, time
mode = os.environ.get('FAKE_APP_SERVER_MODE', 'ok')
thread_id = 'thread-fixture'
turn_id = 'turn-fixture'
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    rid = request.get('id')
    if method == 'initialize':
        print(json.dumps({'id': rid, 'result': {'userAgent': 'fake', 'codexHome': os.environ.get('CODEX_HOME', '')}}), flush=True)
    elif method == 'account/read':
        print(json.dumps({'id': rid, 'result': {'requiresOpenaiAuth': False}}), flush=True)
    elif method == 'thread/start':
        if mode == 'startup_error':
            print(json.dumps({'id': rid, 'error': {'code': -32000, 'message': 'Unauthorized secret-token'}}), flush=True)
            continue
        print(json.dumps({'id': rid, 'result': {'thread': {'id': thread_id}}}), flush=True)
        print(json.dumps({'method': 'thread/started', 'params': {'thread': {'id': thread_id}}}), flush=True)
    elif method == 'turn/start':
        if mode == 'hang':
            print(json.dumps({'id': rid, 'result': {'turn': {'id': turn_id, 'status': 'inProgress'}}}), flush=True)
            time.sleep(30)
            continue
        print(json.dumps({'id': rid, 'result': {'turn': {'id': turn_id, 'status': 'inProgress'}}}), flush=True)
        if mode == 'tool':
            item = {'id': 'tool-1', 'type': 'commandExecution', 'command': 'cat secret'}
            print(json.dumps({'method': 'item/completed', 'params': {'threadId': thread_id, 'turnId': turn_id, 'item': item}}), flush=True)
            continue
        text = '{"ok":true}'
        print(json.dumps({'method': 'item/agentMessage/delta', 'params': {'threadId': thread_id, 'turnId': turn_id, 'itemId': 'agent-1', 'delta': text}}), flush=True)
        print(json.dumps({'method': 'item/completed', 'params': {'threadId': thread_id, 'turnId': turn_id, 'item': {'id': 'agent-1', 'type': 'agentMessage', 'text': text}}}), flush=True)
        print(json.dumps({'method': 'turn/completed', 'params': {'threadId': thread_id, 'turn': {'id': turn_id, 'status': 'completed', 'items': []}}}), flush=True)
    elif method == 'thread/unsubscribe':
        print(json.dumps({'id': rid, 'result': {}}), flush=True)
    elif method == 'turn/interrupt':
        print(json.dumps({'id': rid, 'result': {}}), flush=True)
''', encoding='utf-8')
    env = {'FAKE_APP_SERVER_MODE': mode}
    return [sys.executable, str(script)], env


def worker(tmp_path, mode='ok', timeout=2):
    command, env = fake_app_server(tmp_path, mode=mode)
    # The worker inherits the test process environment; set the fixture mode in
    # the script command itself so tests do not mutate global environment.
    wrapper = tmp_path / f'wrapper-{mode}.py'
    wrapper.write_text(
        'import os,sys\n'
        f'os.environ["FAKE_APP_SERVER_MODE"]={mode!r}\n'
        f'exec(compile(open({command[1]!r}, encoding="utf-8").read(), {command[1]!r}, "exec"))\n',
        encoding='utf-8')
    return CodexAppServerWorker(command=[sys.executable, str(wrapper)], home=tmp_path, timeout=timeout)


def test_worker_uses_ephemeral_thread_and_structured_turn(tmp_path):
    w = worker(tmp_path)
    try:
        result = w.probe(model='fixture-model', provider='fixture-provider', cwd=tmp_path, reasoning_effort='low')
        assert result['finish'] == 'complete', result
        assert result['output'] == {'ok': True}
        assert result['model_calls'] == 1
    finally:
        w.close()


def test_worker_rejects_tool_use_and_can_restart(tmp_path):
    w = worker(tmp_path, mode='tool')
    try:
        result = w.probe(model='fixture-model', provider='fixture-provider', cwd=tmp_path)
        assert result['error'] == 'codex_unexpected_tool_use'
        w.close()
        w = worker(tmp_path, mode='ok')
        result = w.probe(model='fixture-model', provider='fixture-provider', cwd=tmp_path)
        assert result['finish'] == 'complete', result
    finally:
        w.close()


def test_worker_timeout_discards_process_and_next_worker_is_clean(tmp_path):
    w = worker(tmp_path, mode='hang', timeout=.1)
    try:
        result = w.probe(model='fixture-model', provider='fixture-provider', cwd=tmp_path, timeout=.1)
        assert result['error'] == 'codex_timeout'
        w.close()
        w = worker(tmp_path, mode='ok')
        result = w.probe(model='fixture-model', provider='fixture-provider', cwd=tmp_path)
        assert result['finish'] == 'complete', result
    finally:
        w.close()


def test_worker_does_not_echo_provider_error(tmp_path):
    w = worker(tmp_path, mode='startup_error')
    try:
        result = w.probe(model='fixture-model', provider='fixture-provider', cwd=tmp_path)
        assert result['error'] == 'codex_auth_required', result
        assert 'secret-token' not in json.dumps(result)
    finally:
        w.close()
