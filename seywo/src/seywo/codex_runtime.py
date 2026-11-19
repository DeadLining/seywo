"""Bounded Codex catalog discovery and fixed, credential-free failure reasons."""
import asyncio
import json
import os
import re
from pathlib import Path
import tempfile

from .plugin_context import catalog_home
from .runtime_platform import ProcessScope

REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def validate_reasoning_effort(value):
    if not isinstance(value, str) or value not in ("auto", *REASONING_EFFORTS):
        raise ValueError("invalid_reasoning_selection")
    return value


ERROR_MESSAGES = {
    'codex_not_available': '未找到可运行的 Codex。请更新并重新打开 Codex App，或安装 Codex CLI。',
    'codex_cli_incompatible': '当前 Codex 运行时不支持所需接口或隔离选项。请更新 Codex 后重试。',
    'codex_auth_required': '本机 Codex 登录已失效或尚未登录。请在本机 Codex 登录后重试。',
    'codex_model_unavailable': '当前登录或提供方无法使用所选模型。请从列表选择其他模型后测试。',
    'codex_reasoning_unsupported': '当前模型未声明支持所选思考强度。请重新读取模型列表，选择支持的档位或自动后重试。',
    'invalid_reasoning_selection': '思考强度无效。请重新选择支持的档位或自动。',
    'codex_rate_limited': '当前模型额度不足或请求过于频繁。请稍后重试或切换模型。',
    'codex_network_error': '无法连接当前 Codex 提供方。请检查本机网络、代理和提供方地址。',
    'codex_config_invalid': '本机 Codex 配置无法读取或无效。请检查 Codex 配置后重试。',
    'codex_timeout': 'Codex 请求超时。请检查连接后重试。',
    'codex_catalog_invalid': 'Codex 返回的模型列表格式不受支持。请更新 Codex 后重试。',
    'codex_catalog_limit': '模型列表超过读取上限，未加载不完整列表。请检查 Codex 提供方配置。',
    'codex_execution_failed': 'Codex 执行失败。请确认本机 Codex 可以正常使用后重试。',
    'invalid_json': '模型未返回所需结构。请重试或选择其他模型。',
    'codex_output_missing_or_large': '模型未返回有效结果或结果超出上限。请重试或选择其他模型。',
    'codex_output_limit': 'Codex 输出超出读取上限。请检查模型配置后重试。',
    'codex_unexpected_tool_use': '模型执行了未允许的工具操作，本次结果已拒绝。请重试。',
}


def error_message(code):
    return ERROR_MESSAGES.get(code, '模型测试失败，请检查配置后重试。')


def classify_error(text):
    """Return only an allowlisted code. Never return raw CLI/provider messages."""
    value = text.lower()
    if any(s in value for s in ('unexpected argument', 'unknown variant', 'unknown feature',
                                'unrecognized subcommand', 'method not found', 'unknown method')):
        return 'codex_cli_incompatible'
    if any(s in value for s in ('unauthorized', '401 unauthorized', 'not logged in', 'please log in',
                                'authentication', 'invalid api key', 'invalid_api_key', 'token expired')):
        return 'codex_auth_required'
    if ('reasoning' in value or 'effort' in value) and any(s in value for s in ('not support', 'unsupported', 'invalid', 'not allowed')):
        return 'codex_reasoning_unsupported'
    if 'model' in value and any(s in value for s in ('not support', 'unsupported', 'not found', 'not available',
                                                   'unavailable', 'does not exist', 'no access')):
        return 'codex_model_unavailable'
    if any(s in value for s in ('rate limit', 'rate_limit', 'quota', '429', 'usage limit')):
        return 'codex_rate_limited'
    if any(s in value for s in ('connection refused', 'connection error', 'dns', 'error sending request',
                                'failed to connect', 'network is unreachable', 'certificate verify')):
        return 'codex_network_error'
    if any(s in value for s in ('error loading config', 'failed to load config', 'toml parse error')):
        return 'codex_config_invalid'
    return 'codex_execution_failed'


async def _catalog(command, home, timeout):
    async def read_catalog():
        with tempfile.TemporaryDirectory(prefix='seywo-catalog-') as directory, ProcessScope() as scope:
            # No thread is created. Disable executable discovery surfaces even
            # during initialization, without relying on CLI feature-name flags.
            args = [*command, 'app-server']
            for key in ('hooks', 'plugins', 'apps', 'shell_tool', 'memories'):
                args += ['-c', f'features.{key}=false']
            process = await scope.start_async(*args, cwd=directory,
                env={**os.environ, 'CODEX_HOME': str(home), 'SEYWO_INTERNAL_MODEL': '1'},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=256 * 1024)
            async def drain_stderr():
                captured = bytearray()
                while chunk := await process.stderr.read(8192):
                    captured.extend(chunk[:max(0, 65536 - len(captured))])
                return bytes(captured)
            stderr_task = asyncio.create_task(drain_stderr())
            total = 0
            request_id = 0

            async def send(method, params=None, *, notification=False):
                nonlocal request_id, total
                request_id += 1
                msg = {'method': method}
                if params is not None: msg['params'] = params
                if not notification: msg['id'] = request_id
                process.stdin.write((json.dumps(msg) + '\n').encode())
                await process.stdin.drain()
                if notification: return None
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        # Wait for stderr to flush before classification.
                        await process.wait()
                        raise ValueError(classify_error((await stderr_task).decode('utf-8', errors='replace')))
                    total += len(line)
                    if total > 2 * 1024 * 1024: raise ValueError('codex_catalog_limit')
                    msg = json.loads(line)
                    if not isinstance(msg, dict): raise ValueError('codex_catalog_invalid')
                    if msg.get('id') != request_id: continue
                    if 'error' in msg:
                        error = msg['error']
                        if isinstance(error, dict) and error.get('code') == -32601:
                            raise ValueError('codex_cli_incompatible')
                        raise ValueError(classify_error(json.dumps(error)))
                    if not isinstance(msg.get('result'), dict): raise ValueError('codex_catalog_invalid')
                    return msg['result']
            try:
                await send('initialize', {'clientInfo': {'name': 'seywo_models', 'version': '0.4.0'}})
                await send('initialized', notification=True)
                models, seen, cursors = [], set(), set()
                cursor = None
                for _ in range(10):
                    page = await send('model/list', {'limit': 100, 'cursor': cursor, 'includeHidden': False})
                    data = page.get('data')
                    if not isinstance(data, list): raise ValueError('codex_catalog_invalid')
                    for row in data:
                        if not isinstance(row, dict): raise ValueError('codex_catalog_invalid')
                        model = row.get('model')
                        if not isinstance(model, str) or not model or len(model) > 200:
                            raise ValueError('codex_catalog_invalid')
                        if row.get('hidden') or model in seen: continue
                        seen.add(model)
                        label = row.get('displayName')
                        efforts = row.get('supportedReasoningEfforts') or []
                        efforts = [e.get('reasoningEffort') for e in efforts if isinstance(e, dict)] if isinstance(efforts, list) else []
                        efforts = [e for e in efforts if isinstance(e, str) and re.fullmatch(r'[a-z_]{1,30}', e)]
                        default_effort = row.get('defaultReasoningEffort')
                        models.append({'model': model, 'label': label[:200] if isinstance(label, str) and label else model,
                                       'is_default': row.get('isDefault') is True,
                                       'reasoning_efforts': efforts[:20],
                                       'default_reasoning_effort': default_effort if default_effort in efforts else None})
                    cursor = page.get('nextCursor')
                    if cursor is None: return models
                    if not isinstance(cursor, str) or not cursor or cursor in cursors: raise ValueError('codex_catalog_invalid')
                    cursors.add(cursor)
                raise ValueError('codex_catalog_limit')
            finally:
                if process.returncode is None:
                    scope.terminate()
                await process.wait()
                if not stderr_task.done(): stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
    return await asyncio.wait_for(read_catalog(), timeout=timeout)


def list_models(*, command=None, home=None, timeout=15):
    """Ask this Codex runtime for its catalog; no model generation or chat import."""
    from .codex_transport import codex_command
    command = codex_command() if command is None else command
    result = {'models': [], 'error': None, 'model_calls': 0}
    if not command:
        result['error'] = 'codex_not_available'
    else:
        try:
            result['models'] = asyncio.run(_catalog(command, Path(home) if home is not None else catalog_home(), timeout))
        except TimeoutError:
            result['error'] = 'codex_timeout'
        except FileNotFoundError:
            result['error'] = 'codex_not_available'
        except (OSError, ValueError, TypeError, KeyError) as exc:
            code = str(exc)
            result['error'] = code if code in ERROR_MESSAGES else 'codex_catalog_invalid'
    if result['error']: result['message'] = error_message(result['error'])
    return result
