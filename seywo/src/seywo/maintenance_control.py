"""Explicit local retry scheduling. The app action never waits for a model."""
from pathlib import Path

from . import inbox, maintenance_status
from .locking import file_lock, lifecycle
from .project_scope import SourceOwnerMismatch
from .store import Store


def retry(config, chat_id):
    from .hook import spawn
    # Serialize button clicks with Hook queue publication and worker completion.
    with lifecycle(config), file_lock(inbox.root(config) / '.lock'):
        with Store(config, read_only=True) as store:
            current = maintenance_status.read(config, chat_id, db=store.db)
        if not current['retryable']:
            return {'accepted': False, 'maintenance': current, 'model_calls': 0}
        if current['retry_capture']:
            try:
                inbox.resume_turn(config, chat_id)
            except (OSError, ValueError) as exc:
                code = exc.code if isinstance(exc, SourceOwnerMismatch) else (
                    'source_missing' if isinstance(exc, OSError) else maintenance_status.safe_code(str(exc)))
                maintenance_status.safe_record(config, chat_id, 'failed', error_code=code, retry_capture=True)
                return {'accepted': False, 'maintenance': maintenance_status.read(config, chat_id), 'model_calls': 0}
        for item in inbox.failed(config, chat_id):
            inbox.retry(config, item)
        saved = maintenance_status.safe_record(config, chat_id, 'queued')
        if not saved:
            return {'accepted': False, 'maintenance': {**current, 'state': 'failed',
                    'title': '无法开始重试', 'message': maintenance_status.REASONS['status_unavailable']},
                    'model_calls': 0}
        accepted = spawn(Path(config['_path']), chat_id)
        return {'accepted': bool(accepted), 'maintenance': maintenance_status.read(config, chat_id),
                'model_calls': 0}
