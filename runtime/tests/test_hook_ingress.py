"""Synthetic-only ingress, real importer and deterministic in-flight interleavings."""
import copy
import io
import json
from pathlib import Path

import pytest

from sessiongraph import continuation as c
from sessiongraph import hook_ingress as ingress
from sessiongraph.core import Store, init_project, load_config
from test_global_hooks import hook_module
from test_mvp import write_session


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv('SESSIONGRAPH_WORKER_LOCK', str(tmp_path / 'runtime' / 'extract.lock'))
    home = tmp_path / 'codex'
    home.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(home))
    (home / 'sessiongraph.json').write_text('{"enabled":true}')
    project = tmp_path / 'project'
    path = init_project(project)
    raw = json.loads(path.read_text())
    raw['hook'].update(enabled=True, durable_ingress=True,
                       allowed_transcript_roots=[str(tmp_path / 'sessions')])
    raw['graph_v2'] = {'enabled': True, 'recovery_policy': {'enabled': True, 'max_calls_per_run': 3}}
    path.write_text(json.dumps(raw))
    Store(load_config(path)).close()
    (tmp_path / 'sessions').mkdir()
    source = write_session(tmp_path / 'sessions' / 'chat.jsonl', project, 'chat',
                           [('user', 'Threshold <= 0.10m, not yet accepted.')])
    return path, load_config(path), source


def notify(setup, event='Stop', **kwargs):
    path, _, source = setup
    return ingress.notify(path, 'chat', event, str(source), launch=False, **kwargs)


def queued(setup):
    path, config, _ = setup
    return c.read(c.root(config) / 'queue.json', {})[c.key(path, 'chat')]


def tail(source, text='new correction; earlier acceptance is withdrawn'):
    with source.open('a') as out:
        out.write(json.dumps({'type': 'response_item', 'payload': {'type': 'message',
            'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}}) + '\n')


def worker(setup, calls, *, during=None, more=False):
    path, config, _ = setup

    def execute(argv, timeout):
        calls.append(argv)
        if 'refresh-chat' in argv:
            from sessiongraph.continuity import refresh_chat
            store = Store(config)
            try:
                return refresh_chat(store, 'chat', argv[argv.index('--transcript-path') + 1])
            finally:
                store.close()
        assert argv[argv.index('--memories-call-budget') + 1] == '3'
        assert '--respect-graph-schedule' in argv
        if during:
            during()
        return {'saved': [], 'failed': [], 'model_calls': 0,
                'graph_v2': {'durable_stages': True, 'source_remaining': more,
                             'runnable_remaining': more, 'input_source_chars': 1}}
    return execute


def test_notification_has_no_source_reads_provider_or_database_writes(configured, monkeypatch):
    path, config, source = configured
    database = Path(config['database'])
    before = database.read_bytes()
    original = Path.open

    def guarded(self, *args, **kwargs):
        assert self != source, 'notification must not open transcript'
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', guarded)
    assert notify(configured)['accepted']
    assert database.read_bytes() == before
    raw = json.dumps(queued(configured))
    assert 'Threshold' not in raw and 'content' not in raw
    assert len(raw.encode()) < 1600


def test_repeat_and_compact_pair_coalesce_without_queue_writes(configured):
    _, config, _ = configured
    notify(configured, 'PreCompact', now=1)
    before = (c.root(config) / 'queue.json').read_bytes()
    for event in ('PostCompact', 'Stop', 'PreCompact'):
        assert notify(configured, event, now=2)['coalesced']
    assert (c.root(config) / 'queue.json').read_bytes() == before
    assert len(ingress.pending(queued(configured))) == 1


def test_ordinary_turn_imports_real_evidence_then_repeat_dispatches_nothing(configured):
    _, config, _ = configured
    notify(configured)
    calls = []
    execute = worker(configured, calls)
    assert c.process_one(c.root(config), execute=execute)
    assert len(calls) == 2 and c.snapshot(config) == []
    checkpoint = queued(configured)
    assert checkpoint['state'] == 'dismissed' and not ingress.pending(checkpoint)
    assert not checkpoint.get('lease')
    notify(configured)
    assert not c.process_one(c.root(config), execute=execute)
    assert len(calls) == 2
    store = Store(config, readonly=True)
    try:
        from sessiongraph.source_text import hydrate
        assert hydrate(store, store.db.execute('SELECT * FROM entries').fetchone())['text'] == 'Threshold <= 0.10m, not yet accepted.'
        assert store.db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    finally:
        store.close()


@pytest.mark.parametrize('more', [False, True])
def test_new_tail_arriving_during_extraction_is_never_acknowledged_by_old_run(configured, more):
    _, config, source = configured
    notify(configured)
    calls = []

    def newer():
        tail(source)
        notify(configured)

    assert c.process_one(c.root(config), execute=worker(configured, calls, during=newer, more=more))
    assert len(ingress.pending(queued(configured))) == 1
    assert queued(configured)['state'] == 'ready'
    assert c.process_one(c.root(config), execute=worker(configured, calls))
    assert len(calls) == 4 and not ingress.pending(queued(configured))
    store = Store(config, readonly=True)
    try:
        assert store.db.execute('SELECT count(*) FROM entries').fetchone()[0] == 2
    finally:
        store.close()


def test_distinct_segments_of_same_chat_cannot_overwrite_one_another(configured, tmp_path):
    path, config, _ = configured
    second = write_session(tmp_path / 'sessions' / 'segment-two.jsonl', path.parent.parent,
                           'chat', [('user', 'seed=43 is a different experiment')])
    notify(configured, now=1)
    ingress.notify(path, 'chat', 'Stop', str(second), launch=False, now=2)
    assert len(ingress.pending(queued(configured))) == 2
    calls = []
    assert c.process_one(c.root(config), execute=worker(configured, calls))
    assert len(ingress.pending(queued(configured))) == 1
    assert c.process_one(c.root(config), execute=worker(configured, calls))
    assert c.snapshot(config) == [] and len(calls) == 4
    assert len(queued(configured)['ingress']['sources']) == 2


def test_old_lease_cannot_remove_replacement_or_user_cleanup(configured):
    path, config, _ = configured
    notify(configured)
    folder = c.root(config)
    queue = c.read(folder / 'queue.json', {})
    identity = c.key(path, 'chat')
    claimed = {**queue[identity], 'lease': 'old', 'state': 'running'}
    queue[identity] = {**claimed, 'lease': 'replacement'}
    c.write(folder / 'queue.json', queue)
    before = (folder / 'queue.json').read_bytes()
    result = dict(run_id='old-result', chat_id='chat', event='AutoContinue',
                  state='succeeded', target_rowid=1, updated_at=99999999999)
    c.record(path, result, claimed=claimed, launch=False)
    assert (folder / 'queue.json').read_bytes() == before
    c.record(path, result, launch=False)  # Legacy writer cannot override a lease.
    assert (folder / 'queue.json').read_bytes() == before
    c.write(folder / 'queue.json', {})
    c.record(path, result, claimed=claimed, launch=False)
    assert c.read(folder / 'queue.json', {}) == {}


def test_import_failure_keeps_pending_input_and_duplicate_does_not_reset_backoff(configured):
    _, config, _ = configured
    notify(configured, now=1)

    def broken(argv, timeout):
        assert 'refresh-chat' in argv
        raise OSError('synthetic failure')

    assert c.process_one(c.root(config), now=2, execute=broken)
    item = queued(configured)
    assert item['state'] == 'retry_wait' and len(ingress.pending(item)) == 1
    notify(configured, now=3)
    assert queued(configured) == item
    assert not c.process_one(c.root(config), now=3, execute=broken)


def test_changed_source_is_persisted_without_bypassing_retry_deadline(configured):
    path, config, source = configured
    notify(configured, now=1)
    folder = c.root(config)
    queue = c.read(folder / 'queue.json', {})
    identity = c.key(path, 'chat')
    queue[identity].update(state='retry_wait', due=160, error_code='provider_http_502',
                           failures=2, target=42)
    c.write(folder / 'queue.json', queue)
    before = copy.deepcopy(queued(configured))
    tail(source)
    notify(configured, now=100)
    item = queued(configured)
    assert item['state'] == 'retry_wait' and item['due'] == 160
    assert item['error_code'] == 'provider_http_502' and item['failures'] == 2
    assert item['target'] == 42 and item['ingress'] != before['ingress']
    assert len(ingress.pending(item)) == 1
    assert not c.process_one(folder, now=159, execute=lambda *_: pytest.fail('early retry'))


def test_inflight_new_input_cannot_override_durable_retry_result(configured):
    path, config, source = configured
    notify(configured, now=1)
    prior = queued(configured)
    tail(source)
    notify(configured, now=2)
    queue = c.read(c.root(config) / 'queue.json', {})
    identity = c.key(path, 'chat')
    queue[identity].update(state='retry_wait', due=160, error_code='provider_http_502')
    ingress.reconcile(queue, identity, queued(configured),
        {'state': 'succeeded', 'run_id': 'done', 'updated_at': 100}, prior, input_imported=True)
    assert queue[identity]['state'] == 'retry_wait' and queue[identity]['due'] == 160
    assert len(ingress.pending(queue[identity])) == 1


def test_incompatible_provider_stays_blocked_until_identity_changes(configured):
    path, config, source = configured
    notify(configured, now=1)
    c.record(path, {'run_id': 'incompatible', 'chat_id': 'chat', 'event': 'AutoContinue',
        'state': 'failed', 'updated_at': 2, 'target_rowid': 42,
        'error_code': 'single_token_budget_incompatible', 'pending_retry_count': 1,
        'graph_v2': {'durable_stages': True, 'source_remaining': True, 'runnable_remaining': True}},
        launch=False, now=2)
    original = queued(configured)
    assert original['state'] == 'blocked' and original['provider_identity'] == c.provider_identity(config)
    tail(source)
    notify(configured, now=3)
    item = queued(configured)
    assert item['state'] == 'blocked' and item['target'] == 42
    assert item['failures'] == original['failures'] and len(ingress.pending(item)) == 1
    assert not c.process_one(c.root(config), now=1000, execute=lambda *_: pytest.fail('incompatible retry'))
    raw = json.loads(path.read_text())
    raw['provider']['model'] = 'new-explicitly-selected-model'
    path.write_text(json.dumps(raw))
    tail(source)
    notify(configured, now=4)
    assert queued(configured)['state'] == 'ready' and queued(configured)['target'] == 42


def test_exception_preserves_notification_that_arrived_after_claim(configured, monkeypatch):
    _, config, source = configured
    notify(configured)

    def fail(*args, **kwargs):
        tail(source)
        notify(configured)
        raise RuntimeError('synthetic runner crash')

    monkeypatch.setattr('sessiongraph.auto_update.run_update', fail)
    assert c.process_one(c.root(config))
    assert queued(configured)['state'] == 'blocked'
    assert ingress.pending(queued(configured)) and not queued(configured).get('lease')


def test_launcher_failure_is_durable_and_same_notification_can_retry_launch(configured, monkeypatch):
    path, config, source = configured
    launches = []

    def fail(folder):
        launches.append(folder)
        raise OSError('cannot fork')

    monkeypatch.setattr(c, 'start', fail)
    with pytest.raises(OSError):
        ingress.notify(path, 'chat', 'Stop', str(source))
    assert queued(configured)['wake_pending'] is True
    monkeypatch.setattr(c, 'start', lambda folder: launches.append(folder))
    for _ in range(10):
        ingress.notify(path, 'chat', 'Stop', str(source))
    assert len(launches) == 2 and queued(configured)['wake_pending'] is False


@pytest.mark.parametrize('setting', [False, 'yes', True])
def test_opt_in_requires_boolean_and_bounded_graph_policy(configured, setting):
    path, config, _ = configured
    config['hook']['durable_ingress'] = setting
    config['graph_v2']['recovery_policy']['enabled'] = False
    if setting is False:
        assert not ingress.enabled(config)
    else:
        with pytest.raises(ValueError):
            ingress.enabled(config)


@pytest.mark.parametrize('value', ['', 'relative.jsonl', '/outside/chat.jsonl', 7])
def test_invalid_or_outside_transcript_is_rejected_before_queue_creation(configured, value):
    path, config, _ = configured
    with pytest.raises(ValueError):
        ingress.notify(path, 'chat', 'Stop', value, launch=False)
    assert not (c.root(config) / 'queue.json').exists()


def test_symlink_escape_and_non_regular_file_are_rejected(configured, tmp_path):
    path, _, source = configured
    outside = tmp_path / 'outside.jsonl'
    outside.write_text('not this project')
    link = source.parent / 'link.jsonl'
    link.symlink_to(outside)
    with pytest.raises(ValueError):
        ingress.notify(path, 'chat', 'Stop', str(link), launch=False)
    directory = source.parent / 'directory.jsonl'
    directory.mkdir()
    with pytest.raises(ValueError):
        ingress.notify(path, 'chat', 'Stop', str(directory), launch=False)


def test_missing_source_is_durable_and_later_appearance_changes_frontier(configured):
    _, _, source = configured
    body = source.read_bytes()
    source.unlink()
    notify(configured)
    before = copy.deepcopy(queued(configured))
    source.write_bytes(body)
    assert not notify(configured)['coalesced']
    assert ingress.pending(queued(configured))[0][1]['revision'] != ingress.pending(before)[0][1]['revision']


@pytest.mark.parametrize('disable', ['global', 'project', 'ingress'])
def test_disable_after_admission_never_dispatches_provider(configured, monkeypatch, disable):
    path, config, _ = configured
    notify(configured)
    if disable == 'global':
        import os
        (Path(os.environ['CODEX_HOME']) / 'sessiongraph.json').write_text('{"enabled":false}')
    else:
        raw = json.loads(path.read_text())
        raw['hook']['enabled' if disable == 'project' else 'durable_ingress'] = False
        path.write_text(json.dumps(raw))
    assert c.process_one(c.root(config), execute=lambda *_: pytest.fail('disabled dispatch'))
    assert queued(configured)['state'] == 'blocked' and ingress.pending(queued(configured))


def test_hook_stop_and_compaction_opt_in_queue_only_no_stdout_or_direct_extraction(configured, monkeypatch):
    path, config, source = configured
    hook = hook_module()
    monkeypatch.setattr(hook, 'GLOBAL_SETTINGS', path.parent / 'absent.json')
    monkeypatch.setattr(hook, '_spawn_extraction', lambda *_: pytest.fail('must enqueue'))
    launches = []
    monkeypatch.setattr(c, 'start', lambda folder: launches.append(folder))
    output = io.StringIO()
    monkeypatch.setattr('sys.stdout', output)
    for event in ('Stop', 'PreCompact', 'PostCompact'):
        monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(dict(
            hook_event_name=event, session_id='chat', cwd=str(path.parent.parent),
            transcript_path=str(source), stop_hook_active=True))))
        assert hook.main() == 0
    assert output.getvalue() == '' and len(launches) == 1
    assert hook._pending_restore(path, 'chat')
    from sessiongraph.global_hooks import EVENTS
    assert 'Stop' in EVENTS  # Registration alone is not proof of host delivery.


def test_hook_default_stop_is_noop_and_reads_do_not_enqueue(configured, monkeypatch):
    path, config, source = configured
    raw = json.loads(path.read_text())
    raw['hook']['durable_ingress'] = False
    path.write_text(json.dumps(raw))
    hook = hook_module()
    monkeypatch.setattr(hook, 'GLOBAL_SETTINGS', path.parent / 'absent.json')
    monkeypatch.setattr(hook, '_spawn_extraction', lambda *_: pytest.fail('no extraction'))
    for event in ('Stop', 'UserPromptSubmit'):
        monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(dict(
            hook_event_name=event, session_id='chat', cwd=str(path.parent.parent), transcript_path=str(source)))))
        assert hook.main() == 0
    assert c.snapshot(config) == [] and not (c.root(config) / 'queue.json').exists()


def test_existing_indexed_boundary_is_still_used_after_notification_is_acknowledged(configured):
    _, config, source = configured
    notify(configured)
    calls = []
    assert c.process_one(c.root(config), execute=worker(configured, calls, more=True))
    assert not ingress.pending(queued(configured))
    source.unlink()
    assert c.process_one(c.root(config), execute=worker(configured, calls))
    assert len(calls) == 3 and 'refresh-chat' not in calls[-1]
    assert c.snapshot(config) == []


def test_failed_import_does_not_consume_input_when_old_indexed_boundary_can_drain(configured):
    _, config, source = configured
    notify(configured)
    calls = []
    assert c.process_one(c.root(config), execute=worker(configured, calls, more=True))
    tail(source)
    notify(configured)

    def broken(argv, timeout):
        assert 'refresh-chat' in argv
        raise OSError('synthetic import failure')

    assert c.process_one(c.root(config), execute=broken)
    job = queued(configured)
    assert job['pending_import'] and job['target'] is not None
    assert len(ingress.pending(job)) == 1
    # Existing policy drains the immutable old boundary, then leaves the import
    # failure visible. That drain cannot acknowledge an unattempted new source.
    assert c.process_one(c.root(config), execute=worker(configured, calls))
    job = queued(configured)
    assert job['state'] == 'blocked' and job['target'] is None
    assert len(ingress.pending(job)) == 1 and job['error_code'] == 'import_failed'
    assert not c.process_one(c.root(config), execute=broken)


@pytest.mark.parametrize('kind', ['chat', 'project', 'prefix'])
def test_notification_cannot_bypass_importer_identity_or_prefix_verification(configured, kind):
    path, config, source = configured
    if kind == 'prefix':
        notify(configured)
        assert c.process_one(c.root(config), execute=worker(configured, []))
        source.write_text(source.read_text().replace('Threshold', 'CHANGED!!'))
    else:
        rows = source.read_text().splitlines()
        header = json.loads(rows[0])
        header['payload']['id' if kind == 'chat' else 'cwd'] = 'wrong-chat' if kind == 'chat' else '/different-project'
        source.write_text('\n'.join([json.dumps(header), *rows[1:]]) + '\n')
    notify(configured)
    calls = []
    assert c.process_one(c.root(config), execute=worker(configured, calls))
    assert len(calls) == 1 and 'refresh-chat' in calls[0]
    job = queued(configured)
    assert job['state'] == 'blocked' and len(ingress.pending(job)) == 1
    store = Store(config, readonly=True)
    try:
        assert store.db.execute('SELECT count(*) FROM entries').fetchone()[0] == (1 if kind == 'prefix' else 0)
    finally:
        store.close()


def test_checkpoint_and_status_reads_never_wake_or_rewrite_queue(configured, monkeypatch):
    _, config, _ = configured
    notify(configured)
    assert c.process_one(c.root(config), execute=worker(configured, []))
    before = (c.root(config) / 'queue.json').read_bytes()
    monkeypatch.setattr(c, 'start', lambda *_: pytest.fail('read launched scheduler'))
    from sessiongraph.auto_update import update_status
    for _ in range(3):
        assert c.snapshot(config) == []
        assert update_status(config)['model_calls'] == 0
    assert (c.root(config) / 'queue.json').read_bytes() == before


def test_completed_old_boundary_cannot_hide_or_clean_unimported_notification(configured):
    notify(configured)
    item = {**queued(configured), 'target': 10, 'state': 'blocked', 'error_code': 'graph_call_budget_pending'}
    # Even if all earlier graph jobs completed, new input invalidates that proof.
    assert c.completed_boundary(None, item) is False


def test_legacy_import_block_releases_execution_lease_even_without_ingress(configured):
    path, config, _ = configured
    folder = c.root(config)
    with c.locked(folder):
        c.write(folder / 'queue.json', {c.key(path, 'chat'): {
            'config': str(path), 'chat': 'chat', 'state': 'ready', 'due': 0,
            'target': 0, 'run_id': 'legacy', 'pending_import': {
                'error_code': 'import_failed', 'run_id': 'import', 'source_transcript': ''}}})
    assert c.process_one(folder, execute=worker(configured, []))
    result = queued(configured)
    assert result['state'] == 'blocked' and 'lease' not in result and 'ingress_source' not in result


def test_explicit_recovery_during_execution_supersedes_old_lease(configured):
    _, config, _ = configured
    notify(configured)

    def retry():
        from sessiongraph.recovery import wake
        store = Store(config)
        try:
            assert wake(store, {'chat'}, launch=False) == ['chat']
        finally:
            store.close()

    assert c.process_one(c.root(config), execute=worker(configured, [], during=retry))
    job = queued(configured)
    assert job['state'] == 'ready' and job['error_code'] == 'blocked_task_recovered'
    assert not job.get('lease') and len(ingress.pending(job)) == 1
    assert c.process_one(c.root(config), execute=worker(configured, []))
    assert c.snapshot(config) == []


def test_concurrent_notifications_coalesce_before_one_launch(configured, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    path, _, source = configured
    launches = []
    monkeypatch.setattr(c, 'start', lambda folder: launches.append(folder))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: ingress.notify(path, 'chat', 'Stop', str(source)), range(32)))
    assert len(launches) == 1 and sum(not item['coalesced'] for item in results) == 1
    assert len(ingress.pending(queued(configured))) == 1
