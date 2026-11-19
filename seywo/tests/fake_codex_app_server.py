"""Executable app-server fixture; no network, credentials or Codex installation."""
import json
import os
from pathlib import Path
import sys
import time

mode = os.environ.get('SEYWO_TEST_MODE', 'ok')
log = Path(os.environ['SEYWO_TEST_LOG'])
thread_count = 0


def emit(value):
    print(json.dumps(value), flush=True)


def note(method, params):
    emit({'method': method, 'params': params})


for raw in sys.stdin:
    msg = json.loads(raw)
    with log.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'pid': os.getpid(), 'args': sys.argv[1:], 'request': msg}) + '\n')
    method, rid, params = msg['method'], msg.get('id'), msg.get('params') or {}
    result = {}
    if mode == 'crash':
        print('Unauthorized secret-token', file=sys.stderr, flush=True)
        sys.exit(1)
    if mode == 'initialize_hang':
        time.sleep(30)
    if method == 'initialize':
        result = {'userAgent': 'fixture'}
    elif method == 'model/list':
        result = {'data': [{'model': 'fixture', 'displayName': 'Fixture', 'isDefault': True,
            'supportedReasoningEfforts': [{'reasoningEffort': 'low'}, {'reasoningEffort': 'high'}],
            'defaultReasoningEffort': 'low'}], 'nextCursor': None}
    elif method == 'account/read':
        result = {'account': None if mode == 'no_auth' else {'type': 'chatgpt', 'email': 'private@example.test'},
                  'requiresOpenaiAuth': mode == 'no_auth'}
    elif method == 'account/login/start':
        result = {'type': 'chatgpt', 'loginId': 'login-1', 'authUrl': 'https://auth.openai.com/authorize?fixture=1'}
    elif method == 'account/login/cancel':
        result = {'status': 'canceled'}
    elif method == 'thread/start':
        thread_count += 1
        tid = f'thread-{thread_count}'
        if mode == 'auth':
            emit({'id': rid, 'error': {'code': -32000, 'message': 'Unauthorized secret-token'}})
            continue
        result = {'thread': {'id': tid, 'ephemeral': True, 'path': None},
                  'model': params.get('model') or 'fixture', 'modelProvider': params['modelProvider']}
    elif method == 'turn/start':
        tid = params['threadId']
        turn_id = 'turn-' + tid
        if mode == 'hang':
            emit({'id': rid, 'result': {'turn': {'id': turn_id, 'status': 'inProgress'}}})
            continue
        schema = params.get('outputSchema') or {}
        output = {'ok': True} if 'ok' in schema.get('properties', {}) else {
            'segments': [], 'changes': [], 'current': {'goal_index': None, 'priority_indexes': []}}
        text = 'invalid' if mode == 'invalid_json' else json.dumps(output)
        if mode == 'wrong_schema':
            text = '{"ok":"yes"}'
        if mode == 'oversize':
            text = json.dumps({'ok': 'x' * 70000})
        item = {'type': 'agentMessage', 'id': 'answer', 'text': text, 'phase': 'final_answer'}
        if mode == 'tool':
            item = {'type': 'commandExecution', 'id': 'tool', 'command': 'forbidden'}
        if mode == 'terminal_tool':
            items = [item, {'type': 'mcpToolCall', 'id': 'tool'}]
        else:
            items = [item]
        if mode != 'early':
            emit({'id': rid, 'result': {'turn': {'id': turn_id, 'status': 'inProgress', 'items': []}}})
        if mode == 'server_request':
            emit({'id': 'approval-1', 'method': 'item/commandExecution/requestApproval',
                  'params': {'threadId': tid, 'turnId': turn_id}})
            continue
        if mode == 'malformed_event':
            note('item/completed', ['invalid params'])
            continue
        note('item/started', {'threadId': tid, 'turnId': turn_id, 'item': item})
        note('item/completed', {'threadId': tid, 'turnId': turn_id, 'item': item})
        note('thread/tokenUsage/updated', {'threadId': tid, 'turnId': turn_id,
             'tokenUsage': {'last': {'inputTokens': 120, 'cachedInputTokens': 40, 'outputTokens': 30}}})
        if mode == 'retry_error':
            note('error', {'threadId': tid, 'turnId': turn_id, 'willRetry': True,
                          'error': {'message': 'connection refused'}})
        status = 'interrupted' if mode == 'interrupted' else 'completed'
        note('turn/completed', {'threadId': tid,
             'turn': {'id': turn_id, 'status': status, 'items': items}})
        if mode == 'early':
            emit({'id': rid, 'result': {'turn': {'id': turn_id, 'status': 'inProgress', 'items': []}}})
        continue
    elif method == 'turn/interrupt':
        note('turn/completed', {'threadId': params['threadId'],
             'turn': {'id': params['turnId'], 'status': 'interrupted', 'items': []}})
    elif method == 'thread/unsubscribe':
        result = {'status': 'unsubscribed'}
    elif method == 'initialized':
        if mode == 'idle_flood':
            for _ in range(100):
                note('fixture/flood', {'data': 'x' * 65536})
        continue
    else:
        emit({'id': rid, 'error': {'code': -32601, 'message': 'method not found'}})
        continue
    emit({'id': rid, 'result': result})
