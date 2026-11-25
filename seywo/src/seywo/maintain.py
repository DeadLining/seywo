"""Detached compact maintainer: one writer per chat, ordered by compact boundary.

Only compact boundaries reach here. The source was already made durable by the
hook as a filtered dialogue capture. The worker imports it and publishes
validated compact changes, with durable boundaries for retries.
"""

import json
from pathlib import Path
import sys

from .runtime_platform import FileLease

from . import inbox, maintenance_status
from .admission import NonHumanSource
from .config import load_config
from .codex_transport import CodexTransport
from .locking import file_lock
from .hook import spawn as spawn_worker
from .settings import read_provider
from .store import Store, UnsupportedStorage
from .transport import Transport

def _state_path(config, chat_id):
    folder = Path(config["_path"]).parent / "maintain"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{chat_id}.json"


def _read_state(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 0, "cursor": None, "windows": 0, "writer": None}


def run_chat(config, chat_id):
    try:
        report = _run_chat_owned(config, chat_id)
    except UnsupportedStorage:
        maintenance_status.safe_record(config, chat_id, 'blocked', error_code='storage_schema_unsupported')
        return {'state': 'blocked', 'error_code': 'storage_schema_unsupported', 'calls': 0}
    if report.get('state') in {'done', 'idle'}:
        from .storage_lifecycle import safe_collect
        report['storage'] = safe_collect(config, retire_dialogue=True)
    return report


def _run_chat_owned(config, chat_id):
    from .locking import lifecycle
    with lifecycle(config):
        with Store(config, read_only=True) as store:
            if store.chat_deleted(chat_id):
                return {'state': 'excluded', 'calls': 0, 'chat_id': chat_id}
        handle = _lock(config, chat_id)
        if handle is None:
            return {"state": "busy", "calls": 0, "chat_id": chat_id}
        totals, windows = {}, []
        maintenance_status.safe_record(config, chat_id, "running")
        try:
            while True:
                report = _run_chat(config, chat_id)
                for key in ('calls', 'input_chars', 'output_chars', 'input_tokens', 'output_tokens', 'compact_segments'):
                    if key in report:
                        totals[key] = totals.get(key, 0) + report[key]
                windows.extend(report.get('windows', []))
                # The Hook publishes under the same inbox lock. Checking for
                # work and releasing ownership must be one serialized step:
                # arrivals before it are drained here; arrivals after it can
                # acquire the chat lock in their own spawned worker.
                with file_lock(inbox.root(config) / '.lock'):
                    if report.get('state') in {'done', 'idle'} and inbox.pending(config, chat_id):
                        continue
                    completed = {**report, **totals, 'windows': windows}
                    receipt_state = report.get('state')
                    failed = inbox.failed(config, chat_id)
                    if receipt_state in {'done', 'idle'} and failed:
                        receipt_state = 'failed'
                        completed.update(state='failed', error_code=failed[-1].get('error_code') or 'operation_failed')
                    if receipt_state in {'done', 'idle'} and report.get('deferred_turn'):
                        receipt_state = 'partial'
                        completed['error_code'] = 'awaiting_turn_complete'
                    if receipt_state in {'done', 'idle'}:
                        receipt_state = 'done'
                    elif receipt_state == 'excluded':
                        receipt_state = 'idle'
                    elif receipt_state not in {'partial', 'failed', 'blocked'}:
                        receipt_state = 'partial'
                    maintenance_status.safe_record(config, chat_id, receipt_state,
                                                   error_code=completed.get('error_code'), report=completed)
                    handle.close()
                    handle = None
                    return completed
        except Exception:
            maintenance_status.safe_record(config, chat_id, 'failed', error_code='maintainer_exception')
            raise
        finally:
            if handle is not None:
                handle.close()


def _run_chat(config, chat_id):
    """Process every pending compact record for one chat. Returns a small report."""
    try:
        provider = read_provider(config)
    except (OSError, ValueError):
        return {"state": "blocked", "error_code": "provider_not_configured", "calls": 0}
    native = provider.get("backend") == "codex"
    model = provider["model"]
    timeout = max(provider["timeout"], 420)
    if native:
        transport = CodexTransport(model=model, timeout=timeout,
                                   reasoning_effort=provider["reasoning_effort"])
    else:
        key = provider.get("_api_key", "")
        if not key:
            return {"state": "blocked", "error_code": "missing_provider_credential", "calls": 0}
        transport = Transport(provider["endpoint"], key, model, timeout=timeout)
    state_path = _state_path(config, chat_id)
    state = _read_state(state_path)
    report = {"chat_id": chat_id, "windows": [], "calls": 0, "state": "idle"}
    inbox.requeue_failed(config, chat_id)
    inbox.reclaim(config, chat_id)
    from .compact_pipeline import run_compact
    try:
        return run_compact(config, chat_id, state, state_path, transport, report)
    except NonHumanSource:
        return {**report, "state": "excluded", "error_code": "excluded_nonhuman_source"}


def _lock(config, chat_id):
    folder = Path(config["_path"]).parent / "maintain"
    folder.mkdir(parents=True, exist_ok=True)
    handle = FileLease(folder / f"{chat_id}.lock")
    if not handle.acquire(blocking=False):
        handle.close()
        return None
    return handle


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="seywo.maintain")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--chat", required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except (OSError, ValueError):
        maintenance_status.safe_record(args.config, args.chat, 'blocked', error_code='config_invalid')
        return 1
    if (config.get("hook") or {}).get("enabled") is not True:
        return 0
    try:
        report = run_chat(config, args.chat)
        from .chat_deletion import available, catalog_home, reconcile
        home = catalog_home()
        if available(home):
            reconcile(config, home)
    except Exception:  # run_chat records failure while it still owns the chat lock
        report = {"state": "failed", "error_code": "maintainer_exception"}
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get("state") in {"done", "idle", "partial", "busy"} else 1


if __name__ == "__main__":
    sys.exit(main())
