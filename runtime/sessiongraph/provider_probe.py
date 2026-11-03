"""One explicit, bounded Responses probe; never persists settings or chat data."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import socket
import time
from urllib import error, request
from urllib.parse import urlsplit

from .gateway_worker import gateway_credentials
from .provider_credentials import validate_endpoint


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe_provider(provider: dict, *, state: Path, codex_home: Path, api_key=None):
    endpoint = validate_endpoint(provider['endpoint'])
    if api_key is not None and (not isinstance(api_key, str) or not api_key.strip()
            or len(api_key) > 2048 or any(ord(c) < 32 for c in api_key)):
        raise ValueError('invalid_api_key')
    started = time.perf_counter()
    calls, status = 0, None

    def result(ok, message):
        elapsed = round((time.perf_counter() - started) * 1000)
        return dict(ok=ok, model_calls=calls, message=f'{message} · 总耗时 {elapsed} ms',
                    provider_test=dict(ok=ok, elapsed_ms=elapsed, http_status=status,
                                       model=provider['model']))

    def send(key):
        nonlocal calls, status
        payload = dict(model=provider['model'], input='Reply with OK.', stream=False,
                       store=False, max_output_tokens=16)
        req = request.Request(endpoint, data=json.dumps(payload).encode(), headers={
            'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key,
        })
        handlers = [_NoRedirect()]
        if urlsplit(endpoint).hostname in {'localhost', '127.0.0.1', '::1'}:
            handlers.append(request.ProxyHandler({}))
        calls = 1
        with request.build_opener(*handlers).open(req, timeout=20) as response:
            status = response.status
            raw = response.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            return result(False, '测试失败：服务返回的内容过大')
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            return result(False, '测试失败：服务未返回有效的 Responses JSON')
        if not isinstance(data, dict) or data.get('error'):
            return result(False, '测试失败：模型服务返回错误')
        # A bounded reasoning model may consume the tiny budget before emitting text.
        # Its valid incomplete response still proves this model route is reachable.
        details = data.get('incomplete_details')
        limited = isinstance(details, dict) and details.get('reason') == 'max_output_tokens'
        if data.get('status') == 'incomplete' and limited:
            return result(True, '模型已连通（达到测试输出上限）')
        output = data.get('output')
        messages = [item.get('content') for item in output
                    if isinstance(item, dict) and item.get('type') == 'message'] if isinstance(output, list) else []
        text = any(isinstance(part, dict) and part.get('type') == 'output_text'
                   and isinstance(part.get('text'), str) and part['text'].strip()
                   for content in messages if isinstance(content, list) for part in content)
        if data.get('status') != 'completed' or not text:
            return result(False, '测试失败：模型未返回有效的完成结果')
        return result(True, '模型已连通')

    try:
        if api_key is not None:
            return send(api_key.strip())
        with gateway_credentials({'provider': provider}, codex_home / 'config.toml', state=state):
            return send(os.environ[provider['api_key_env']])
    except error.HTTPError as exc:
        status = exc.code
        exc.close()
        message = {
            400: '测试请求被拒绝，请检查模型名称和 Responses 接口支持',
            401: '鉴权失败，请检查 API KEY', 403: '无访问权限，请检查 API KEY 和模型权限',
            404: '端点或模型不存在，请检查 BASE URL 和 MODEL NAME',
            429: '请求受限或额度不足，请稍后重试',
        }.get(status, '模型服务暂时不可用' if status >= 500 else '模型服务拒绝了测试请求')
        return result(False, f'测试失败：{message}（HTTP {status}）')
    except (TimeoutError, socket.timeout):
        return result(False, '测试超时：20 秒内未完成响应')
    except error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            return result(False, '测试超时：20 秒内未完成响应')
        return result(False, '连接失败：请检查 BASE URL、网络和代理')
    except FileNotFoundError:
        return result(False, '测试失败：未找到此端点的有效凭据，请填写 API KEY')
    except (OSError, http.client.HTTPException):
        return result(False, '连接失败：服务断开或网络不可用')
    except ValueError:
        return result(False, '测试失败：未找到此端点的有效凭据，请填写 API KEY')
