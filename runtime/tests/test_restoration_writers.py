"""All restoration writers participate in acknowledgement's compare/remove lock."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import json
from pathlib import Path
from threading import Event

import pytest

from sessiongraph import restoration
from sessiongraph.auto_update import run_update
from sessiongraph.core import Store, init_project, load_config
from test_global_hooks import hook_module


@pytest.mark.parametrize('counts', [dict(saved=['node'], saved_relations=[]),
                                    dict(saved=[], saved_relations=['relation'])])
def test_background_rearm_uses_same_revision_contract_as_hook(tmp_path, counts):
    path = init_project(tmp_path)
    config = load_config(path)
    Store(config).close()
    hook = hook_module()
    hook._mark_needs_restore(path, 'chat')
    old = hook._pending_restore(path, 'chat')
    def execute(argv, timeout):
        return {'added_entries': 0, 'skipped': []} if 'refresh-chat' in argv else {**counts, 'failed': [], 'model_calls': 0}
    result = run_update(path, 'chat', 'PostCompact', execute=execute)
    assert result['state'] == 'succeeded'
    newer = hook._pending_restore(path, 'chat')
    assert newer != old and json.loads(newer)['revision'] != json.loads(old)['revision']
    assert not hook._ack_restore(path, 'chat', old)
    assert hook._pending_restore(path, 'chat') == newer


def test_background_writer_waits_for_compare_and_unlink_to_finish(tmp_path, monkeypatch):
    path = init_project(tmp_path)
    config = load_config(path)
    Store(config).close()
    restoration.mark(config, 'chat')
    old = restoration.pending(config, 'chat')
    flag = restoration.flag_path(config, 'chat')
    inside_unlink, allow_unlink, rearming = Event(), Event(), Event()
    unlink, mark = Path.unlink, restoration.mark

    def paused_unlink(self, *args, **kwargs):
        if self == flag:
            inside_unlink.set()
            assert allow_unlink.wait(5)
        return unlink(self, *args, **kwargs)

    def observed_mark(*args, **kwargs):
        rearming.set()
        return mark(*args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', paused_unlink)
    monkeypatch.setattr(restoration, 'mark', observed_mark)
    def execute(argv, timeout):
        return {'added_entries': 0, 'skipped': []} if 'refresh-chat' in argv else {'saved': ['new'], 'failed': [], 'model_calls': 0}

    with ThreadPoolExecutor(max_workers=2) as pool:
        ack = pool.submit(restoration.acknowledge, config, 'chat', old)
        try:
            assert inside_unlink.wait(5)
            writer = pool.submit(run_update, path, 'chat', 'PostCompact', execute=execute)
            assert rearming.wait(5)
            with pytest.raises(TimeoutError):
                writer.result(timeout=.05)
        finally:
            allow_unlink.set()
        assert ack.result(timeout=5)
        assert writer.result(timeout=5)['state'] == 'succeeded'
    newer = restoration.pending(config, 'chat')
    assert newer and newer != old  # The background opportunity was not unlinked.


def test_atomic_write_failure_keeps_old_marker_and_releases_lock(tmp_path, monkeypatch):
    path = init_project(tmp_path)
    config = load_config(path)
    restoration.mark(config, 'chat')
    old = restoration.pending(config, 'chat')
    def fail(*args):
        raise OSError('disk failure')
    monkeypatch.setattr('sessiongraph.global_hooks.atomic_write', fail)
    with pytest.raises(OSError):
        restoration.mark(config, 'chat')
    assert restoration.pending(config, 'chat') == old
    assert restoration.acknowledge(config, 'chat', old)


def test_old_timestamp_writer_format_remains_readable(tmp_path):
    import time
    config = load_config(init_project(tmp_path))
    flag = restoration.flag_path(config, 'chat')
    flag.parent.mkdir(parents=True)
    flag.write_text(str(time.time()))
    token = restoration.pending(config, 'chat')
    assert token and restoration.acknowledge(config, 'chat', token)
