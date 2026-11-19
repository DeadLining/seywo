"""Bounded Codex catalog discovery and fixed, credential-free failure reasons."""
import json
import re
from pathlib import Path
import tomllib

from .plugin_context import catalog_home
from .runtime_platform import find_codex

REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def validate_reasoning_effort(value):
    if not isinstance(value, str) or value not in ("auto", *REASONING_EFFORTS):
        raise ValueError("invalid_reasoning_selection")
    return value


ERROR_MESSAGES = {
    'codex_not_available': '未找到 Codex 应用服务运行时。请更新并重新打开 Codex App。',
    'codex_app_server_incompatible': '当前 Codex 应用服务不支持所需协议。请更新 Codex App 后重试。',
    'codex_app_server_invalid': 'Codex 应用服务返回的协议数据无效。请重试或更新 Codex App。',
    'codex_worker_broken': 'Codex 应用服务已断开。下次重试会重新启动服务。',
    'codex_worker_busy': 'Codex 应用服务正在处理其他请求。请稍后重试。',
    'codex_worker_closed': 'Codex 应用服务已关闭。请重新打开 Seywo。',
    'codex_input_limit': '整理内容超过应用服务输入上限。请缩小本次任务。',
    'codex_interrupted': '本次整理已中断。可以重试继续处理。',
    'codex_cli_incompatible': '当前 Codex 运行时不支持所需接口或隔离选项。请更新 Codex 后重试。',
    'codex_auth_required': 'Codex 账号需要授权。请在模型设置中连接 Codex 账号，再检查授权并重试。',
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
    """Return only a fixed code; never expose runtime/provider messages."""
    value = text.lower()
    if any(s in value for s in ('unexpected argument', 'unknown variant', 'unknown feature',
                                'unrecognized subcommand', 'method not found', 'unknown method')):
        return 'codex_app_server_incompatible'
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


def app_server_command(command, home):
    """Build one app-server command with discovery surfaces disabled."""
    path = Path(home) / 'config.toml'
    try:
        config = tomllib.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        servers = config.get('mcp_servers') or {}
        if any(not re.fullmatch(r'[A-Za-z0-9_-]+', name) for name in servers):
            raise ValueError('unsupported_mcp_name')
    except FileNotFoundError:
        servers = {}
    except (OSError, ValueError, TypeError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError('codex_config_invalid') from exc
    values = [
        'features.hooks=false',
        'features.plugins=false',
        'features.apps=false',
        'features.shell_tool=false',
        'features.memories=false',
        'features.multi_agent=false',
        'features.multi_agent_v2=false',
        'features.view_image=false',
        'features.image_generation=false',
        'features.goals=false',
        'features.skill_search=false',
        'features.skip_host_skill_discovery=true',
        'project_doc_max_bytes=0',
        'web_search="disabled"',
        'memories.use_memories=false',
        'memories.generate_memories=false',
    ]
    values.extend(f'mcp_servers.{name}.enabled=false' for name in servers)
    skills = set()
    for root in (Path(home) / 'skills', Path.home() / '.agents/skills'):
        if root.is_dir():
            skills.update(root.glob('*/SKILL.md'))
            skills.update(root.glob('*/*/SKILL.md'))
    skills.update(Path(home) / 'skills/.system' / name / 'SKILL.md' for name in
                  ('imagegen', 'skill-creator', 'skill-installer', 'plugin-creator', 'openai-docs'))
    skills |= {path.resolve() for path in skills}
    values.append('skills.config=[' + ','.join(
        '{path=' + json.dumps(str(path)) + ',enabled=false}' for path in sorted(skills)) + ']')
    args = [*command, 'app-server', '--stdio']
    for value in values:
        args.extend(['-c', value])
    return args



def normalize_models(data):
    """Expose only the catalog's bounded public fields."""
    if not isinstance(data, list):
        raise ValueError('codex_catalog_invalid')
    models = []
    for row in data:
        if not isinstance(row, dict):
            raise ValueError('codex_catalog_invalid')
        model = row.get('model')
        if not isinstance(model, str) or not model or len(model) > 200:
            raise ValueError('codex_catalog_invalid')
        if row.get('hidden'):
            continue
        label = row.get('displayName')
        efforts = row.get('supportedReasoningEfforts') or []
        efforts = [e.get('reasoningEffort') for e in efforts if isinstance(e, dict)] if isinstance(efforts, list) else []
        efforts = [e for e in efforts if e in REASONING_EFFORTS]
        default = row.get('defaultReasoningEffort')
        models.append({'model': model, 'label': label[:200] if isinstance(label, str) and label else model,
                       'is_default': row.get('isDefault') is True, 'reasoning_efforts': efforts[:20],
                       'default_reasoning_effort': default if default in efforts else None})
    return models


def list_models(*, command=None, home=None, timeout=15, refresh=True):
    """Read the shared worker catalog; no thread or model turn is created."""
    from .codex_app_server import get_worker
    command = find_codex() if command is None else command
    result = {'models': [], 'error': None, 'model_calls': 0}
    if not command:
        result['error'] = 'codex_not_available'
    else:
        try:
            worker = get_worker(command=command, home=Path(home) if home is not None else catalog_home(), timeout=timeout)
            result['models'] = worker.models(timeout=timeout, refresh=refresh)
        except (OSError, ValueError, RuntimeError, TypeError, KeyError) as exc:
            code = str(exc)
            result['error'] = code if code in ERROR_MESSAGES else 'codex_catalog_invalid'
    if result['error']:
        result['message'] = error_message(result['error'])
    return result


def account_status(*, action='status', command=None, home=None, timeout=15):
    """Explicit app-server managed login; Seywo never reads or returns tokens."""
    from .codex_app_server import get_worker
    if action not in {'status', 'login', 'cancel'}:
        raise ValueError('invalid_account_action')
    command = find_codex() if command is None else command
    try:
        if not command:
            raise ValueError('codex_not_available')
        worker = get_worker(command=command, home=Path(home) if home is not None else catalog_home(), timeout=timeout)
        return worker.account(action=action, timeout=timeout)
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        code = str(exc) if str(exc) in ERROR_MESSAGES else 'codex_execution_failed'
        return {'state': 'unavailable', 'error': code, 'message': error_message(code), 'model_calls': 0}
