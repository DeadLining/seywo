"""Bounded per-chat maintenance receipts shared by Hook, worker and panel.

No transcript, model response or raw exception is stored here. Reads never write
or call a model. Kernel writer locks distinguish an interrupted worker from a
long model call without heartbeat writes or an extra background service.
"""
import json
import math
from pathlib import Path
import re
import time
from uuid import uuid4

from .runtime_platform import FileLease

from .config import _atomic_write
from .locking import file_lock
from .codex_runtime import ERROR_MESSAGES

CHAT = re.compile(r'[A-Za-z0-9_-]{1,128}')
START_GRACE_SECONDS = 60
STATES = {'idle', 'queued', 'running', 'done', 'partial', 'failed', 'blocked', 'interrupted', 'disabled'}
FAILURES = {'partial', 'failed', 'blocked', 'interrupted'}
REASONS = {
    **{code: ERROR_MESSAGES[code] for code in (
        'codex_cli_incompatible', 'codex_auth_required', 'codex_model_unavailable',
        'codex_reasoning_unsupported', 'codex_rate_limited', 'codex_network_error', 'codex_config_invalid',
        'codex_app_server_incompatible', 'codex_app_server_invalid', 'codex_worker_broken',
        'codex_worker_busy', 'codex_worker_closed', 'codex_interrupted', 'codex_input_limit')},
    'compact_new_phase_requires_user_objective': '新阶段缺少本段用户目标的有效定位，模型修正后仍未通过校验。可以重试整理。',
    'provider_not_configured': '模型尚未配置，请检查模型设置。',
    'missing_provider_credential': '模型凭据不可用，请检查模型设置。',
    'source_missing': '原始对话暂时无法读取，请确认对话文件仍然可用。',
    'source_capture_format_unsupported': '对话快照格式不受支持，未修改原数据。请从原始日志建立新索引。',
    'source_project_unverified': '无法确认对话日志归属当前项目，请核对工作目录后重试。',
    'source_archive_capacity': '待保留的对话记录超出归档容量，未能导入。请保留原对话，后续工作可在新对话中继续整理。',
    'source_record_too_large': '原始对话中有单条记录超出导入上限，未能导入。请保留原对话，后续工作可在新对话中继续整理。',
    'chat_identity_unresolved': '暂时无法确认这段对话的身份，请重新打开当前对话。',
    'worker_start_failed': '后台整理未能启动，可以重试。',
    'maintainer_exception': '后台整理遇到异常，可以重试。',
    'config_invalid': '项目记忆配置无法读取，请检查项目配置后重试。',
    'queue_write_failed': '未能保存本次整理任务，请检查磁盘空间和写入权限。',
    'storage_schema_unsupported': '项目记忆索引格式不受支持，原有数据未修改。重新初始化前请先移出旧索引。',
    'context_ceiling': '本次整理达到上下文限制，尚未完成。',
    'call_budget': '本次整理达到调用预算，剩余内容可继续整理。',
    'retry_ceiling': '多次重试仍未完成，请检查模型连接后重试。',
    'rate_limited': '模型服务暂时限流，请稍后重试。',
    'model_connection': '模型连接暂时不可用，请检查 Codex 登录或模型服务。',
    'model_output': '模型返回的整理结果未通过校验，可以重试。',
    'worker_interrupted': '后台整理已中断，可以重试。',
    'status_unavailable': '无法读取整理状态，请检查磁盘空间和写入权限。',
    'awaiting_turn_complete': '原始对话已保存，等待本轮对话结束后继续整理。',
    'pending_job': '仍有内容尚未整理完成，可以继续整理。',
    'operation_failed': '本次整理未能完成，可以重试。',
}
TITLES = {'idle': '尚未开始整理', 'queued': '等待整理', 'running': '正在整理',
          'done': '主线已更新', 'partial': '整理尚未完成', 'failed': '整理失败',
          'blocked': '整理暂时无法继续', 'interrupted': '整理已中断', 'disabled': '自动整理已关闭'}


def _config_path(config):
    path = Path(config['_path']) if isinstance(config, dict) else Path(config)
    if path.name != 'config.json' or path.parent.name != '.seywo' or not path.is_absolute():
        raise ValueError('invalid_status_config')
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('invalid_status_config')
    return path


def _path(config, chat_id):
    if not isinstance(chat_id, str) or not CHAT.fullmatch(chat_id):
        raise ValueError('invalid_chat_id')
    folder = _config_path(config).parent / 'maintenance'
    path = folder / f'{chat_id}.json'
    if folder.is_symlink() or path.is_symlink():
        raise ValueError('invalid_status_path')
    return path


def safe_code(code):
    if code in REASONS:
        return code
    # Only fixed categories cross this boundary, never provider/exception text.
    if isinstance(code, str):
        prefix = code.split(':', 1)[0]
        if prefix == 'source_record_too_large' or code.startswith('source record exceeds 16 MiB;'):
            return 'source_record_too_large'
        if prefix in {'http_401', 'http_403'}:
            return 'missing_provider_credential'
        if prefix == 'http_429':
            return 'rate_limited'
        if (re.fullmatch(r'http_[1-5][0-9]{2}', prefix)
                or prefix in {'transport_error', 'codex_not_available', 'codex_execution_failed'}):
            return 'model_connection'
        if prefix in {'compact_empty', 'codex_output_limit', 'codex_output_missing_or_large',
                      'codex_unexpected_tool_use', 'returned_model_mismatch'} or prefix.startswith('compact_invalid_'):
            return 'model_output'
        if prefix in {'timeout', 'network_error', 'connection_error', 'codex_timeout', 'http_error', 'codex_error'}:
            return 'model_connection'
        if prefix in {'invalid_output', 'invalid_json', 'schema_error', 'contract_error', 'model_invalid_output'}:
            return 'model_output'
    return 'operation_failed'


def _load(path):
    try:
        with path.open('rb') as stream:
            raw = stream.read(8193)
        data = json.loads(raw) if len(raw) <= 8192 else None
        if not isinstance(data, dict) or data.get('state') not in STATES:
            raise ValueError('invalid_receipt')
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeError):
        return {'state': 'interrupted', 'error_code': 'status_unavailable', 'revision': 'unreadable'}


def _durable_status(config, chat_id, db=None, *, include_inbox=True):
    """Recover status from durable work if the worker did not write its receipt."""
    from . import inbox
    if not isinstance(config, dict):
        config = {"_path": str(config)}
    selected = {}
    last_success = None
    if db is not None and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='compact_batches'").fetchone():
        last_success = db.execute("SELECT max(updated_at) FROM compact_batches WHERE chat_id=? AND state='done'", (chat_id,)).fetchone()[0]
        row = db.execute("SELECT state,error_code,updated_at,published_version FROM compact_batches WHERE chat_id=? AND state IN ('failed','done') ORDER BY updated_at DESC,rowid DESC LIMIT 1", (chat_id,)).fetchone()
        if row:
            selected = {'state': 'failed' if row[0] == 'failed' else 'done',
                        'error_code': safe_code(row[1]) if row[1] else None,
                        'updated_at': row[2], 'published_version': row[3]}
    candidates = inbox.failed(config, chat_id) + inbox._open_for(config, chat_id) if include_inbox else []
    candidates = [item for item in candidates if not item.get('imported_snapshot_id') or not selected]
    if candidates:
        item = max(candidates, key=lambda item: item.get('updated_at', 0))
        if item.get('updated_at', 0) >= (selected.get('updated_at') or 0):
            selected = {'state': 'failed' if item['state'] == 'failed' else 'queued',
                        'error_code': safe_code(item['error_code']) if item.get('error_code') else None,
                        'updated_at': item.get('updated_at')}
    if selected:
        selected.update(last_success_at=last_success, revision='ledger:' + str(selected.get('updated_at')))
    return selected


def _alive(config, chat_id):
    path = _config_path(config).parent / 'maintain' / f'{chat_id}.lock'
    try:
        if path.is_symlink():
            return False
        with FileLease(path, create=False) as lease:
            return not lease.acquire(blocking=False)
    except OSError:
        return False


def _stamp(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def record(config, chat_id, state, *, error_code=None, report=None, retry_capture=False):
    """Replace one small receipt atomically; never append event/transcript logs."""
    if state not in STATES:
        raise ValueError('invalid_maintenance_state')
    path = _path(config, chat_id)
    with file_lock(path.parent / '.lock'):
        old = _load(path)
        code = safe_code(error_code) if error_code else None
        revision = old.get('revision') if old.get('state') == state and old.get('error_code') == code else uuid4().hex
        data = {'state': state, 'error_code': code, 'revision': revision,
                'updated_at': time.time(), 'last_success_at': _stamp(old.get('last_success_at')),
                'notified': old.get('notified'), 'published_version': old.get('published_version')}
        if state == 'done':
            data['last_success_at'] = data['updated_at']
        if retry_capture:
            data['retry_capture'] = True
        if report:
            if type(report.get('version')) is int and report['version'] >= 0:
                data['published_version'] = report['version']
            data['calls'] = max(0, int(report.get('calls') or 0))
        _atomic_write(path, json.dumps(data, ensure_ascii=False, sort_keys=True) + '\n')


def safe_record(config, chat_id, state, **kwargs):
    """Observability must never abort an otherwise valid Compact transaction."""
    try:
        record(config, chat_id, state, **kwargs)
        return True
    except (OSError, ValueError):
        return False


def read(config, chat_id, *, db=None):
    path = _path(config, chat_id)
    data = _load(path)
    generic_failure = data.get('state') == 'failed' and data.get('error_code') in {None, 'operation_failed', 'model_output'}
    if not data or generic_failure or db is not None:
        durable = _durable_status(config, chat_id, db, include_inbox=not data or generic_failure)
        if durable and (not data or (generic_failure and durable['state'] == 'failed') or
                        (_stamp(durable.get('updated_at')) or 0) > (_stamp(data.get('updated_at')) or 0)):
            data = durable
    state = data.get('state', 'idle')
    code = safe_code(data['error_code']) if data.get('error_code') else None
    if (_config_path(config).parent / 'disabled').exists() or (
            isinstance(config, dict) and (config.get('hook') or {}).get('enabled') is not True):
        state = 'disabled'
    elif state in {'queued', 'running'}:
        alive = _alive(config, chat_id)
        if alive:
            state = 'running'
        elif state == 'running' or time.time() - (_stamp(data.get('updated_at')) or 0) > START_GRACE_SECONDS:
            state, code = 'interrupted', 'worker_interrupted'
    message = REASONS.get(code, '') if state in FAILURES else ''
    if state in FAILURES and not message:
        message = REASONS['pending_job' if state == 'partial' else 'operation_failed']
    if state == 'running':
        message = '完成后会更新主线，你可以继续对话。'
    elif state == 'queued':
        message = '任务已保存，正在等待后台整理。'
    from .storage_lifecycle import status as storage_status
    waiting = state == 'partial' and code == 'awaiting_turn_complete'
    return {'storage': storage_status(config if isinstance(config, dict) else {'_path': str(config)}),
            'chat_id': chat_id, 'state': state, 'title': '等待本轮回答结束' if waiting else TITLES[state], 'message': message,
            'retryable': state in FAILURES and code != 'storage_schema_unsupported', 'waiting_for_turn': waiting, 'updated_at': _stamp(data.get('updated_at')),
            'retry_capture': waiting or (state == 'failed' and data.get('retry_capture') is True),
            'last_success_at': _stamp(data.get('last_success_at')),
            'published_version': data.get('published_version') if type(data.get('published_version')) is int else None,
            'stale_view': state in FAILURES | {'queued', 'running'}, 'model_calls': 0}


def take_notice(config, chat_id):
    """At most one native Hook message for a failure, not one per user turn."""
    path = _path(config, chat_id)
    with file_lock(path.parent / '.lock'):
        # Read under the same receipt lock as acknowledgement: a worker that
        # just recovered must not produce a stale failure notification.
        result = read(config, chat_id)
        if result['state'] not in FAILURES or result.get('waiting_for_turn') or not result['retryable']:
            return None
        data = _load(path) or _durable_status(config, chat_id)
        key = f"{result['state']}:{data.get('revision', 'unreadable')}"
        if data.get('notified') == key:
            return None
        data.update(notified=key)
        # Preserve an unreadable receipt as a safe explicit failure record.
        if not data.get('state'):
            data['state'] = result['state']
        _atomic_write(path, json.dumps(data, ensure_ascii=False, sort_keys=True) + '\n')
    return f"Seywo：{result['title']}。{result['message']} 已发布的主线保留，可在 Seywo 面板查看并重试。"
