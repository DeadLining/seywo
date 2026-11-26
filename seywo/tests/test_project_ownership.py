from pathlib import Path, PureWindowsPath
import io
import json

import pytest

from seywo import hook
from seywo.config import initialize, load_config
from seywo.project_scope import ProjectBinding, source_belongs_to_project


def test_stored_owner_is_not_a_filesystem_locator(tmp_path, monkeypatch):
    config = Path(initialize(tmp_path / 'actual')['config'])
    foreign = tmp_path / 'unrelated'
    data = json.loads(config.read_text())
    data['project_root'] = str(foreign)
    config.write_text(json.dumps(data))
    original = ProjectBinding.at

    def guarded(path):
        assert path != foreign, 'untrusted owner reached filesystem lookup'
        return original(path)

    monkeypatch.setattr(ProjectBinding, 'at', staticmethod(guarded))
    with pytest.raises(ValueError, match='foreign'):
        load_config(config)


@pytest.mark.parametrize(('regular', 'extended'), [
    (r'D:\projects\test', r'\\?\D:\projects\test'),
    (r'\\server\share\test', r'\\?\UNC\server\share\test'),
])
def test_only_matching_native_and_extended_owner_spellings_are_eligible(regular, extended):
    from seywo.project_scope import stored_owner_candidate
    from seywo.windows_paths import extended_windows_path

    for root in (regular, extended):
        path = PureWindowsPath(root)
        assert str(extended_windows_path(path)) == extended
        for spelling in (regular, extended):
            assert stored_owner_candidate(path, spelling)
        for other in (r'\\elsewhere\share\test', r'\\server\other\test', r'E:\projects\test',
                      r'\\.\D:\projects\test', r'\\?\GLOBALROOT\Device\test',
                      regular + r'\..\test', regular + 'X', None):
            assert not stored_owner_candidate(path, other)


def test_leaf_alias_is_valid_read_evidence_not_a_writable_binding(tmp_path):
    real = tmp_path / 'actual'
    config = Path(initialize(real, plugin_hook=True)['config'])
    alias = tmp_path / 'alias'
    alias.symlink_to(real, target_is_directory=True)
    assert source_belongs_to_project(str(alias), str(real))
    assert hook.find_config(str(alias)) == config
    with pytest.raises(ValueError, match='symlink'):
        initialize(alias)
    foreign = tmp_path / 'other'
    foreign.mkdir()
    assert not source_belongs_to_project(str(alias), str(foreign))


def test_unc_namespace_only_is_case_insensitive():
    from seywo.project_scope import stored_owner_candidate
    root = PureWindowsPath(r'\\host\Share\Data')
    for marker in ('UNC', 'unc', 'UnC'):
        prefix = '\\\\?\\' + marker
        assert stored_owner_candidate(root, prefix + r'\host\Share\Data')
        assert not stored_owner_candidate(root, prefix + r'\host\share\Data')
        assert not stored_owner_candidate(root, prefix + r'\other\Share\Data')


def test_case_spelling_reopens_only_same_physical_project(tmp_path, monkeypatch):
    import asyncio
    from seywo.plugin import create_plugin_server

    actual = tmp_path / 'Project'
    alias = tmp_path / 'project'
    path = Path(initialize(actual)['config'])
    if not alias.exists():  # A case-sensitive volume can host two distinct owners.
        alias.mkdir()
    same = actual.samefile(alias)
    server = create_plugin_server(actual)
    monkeypatch.setattr('seywo.plugin.host_workspace', lambda _: alias)
    info = asyncio.run(server.call_tool('seywo_open', {})).structuredContent
    if same:
        assert info.get('state') is None, info
        assert load_config(alias / '.seywo/config.json')['project_id'] == load_config(path)['project_id']
    else:
        assert info['state'] == 'reconnect_required'
        assert not (alias / '.seywo').exists()


@pytest.mark.parametrize('batch', [False, True])
def test_unidentifiable_root_leaves_no_partial_state(tmp_path, monkeypatch, batch):
    from seywo import batch_init

    def unavailable(root):
        raise ValueError('unidentifiable directory')

    monkeypatch.setattr(ProjectBinding, 'at', staticmethod(unavailable))
    if batch:
        monkeypatch.setattr('builtins.input', lambda _: 'yes')
        assert batch_init.run([tmp_path]) == 1
    else:
        with pytest.raises(ValueError, match='unidentifiable'):
            initialize(tmp_path)
    assert not (tmp_path / '.seywo').exists()


@pytest.mark.parametrize('enabled', (True, False, None))
def test_missing_cwd_without_git_proof_never_enqueues(tmp_path, monkeypatch, capsys, enabled):
    root = tmp_path / 'workspace'
    root.mkdir()
    if enabled is not None:
        initialize(root, plugin_hook=enabled)
    payload = {'session_id': 'test', 'hook_event_name': 'PreCompact', 'cwd': str(root / 'gone')}
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(hook, 'spawn', lambda *a: pytest.fail('unknown owner'))
    assert hook.main() == 0
    output = capsys.readouterr().out
    if enabled:
        assert '无法确认' in json.loads(output)['systemMessage']
    else:
        assert output == ''
    assert not (root / '.seywo/inbox').exists()


@pytest.mark.parametrize('event', ['PreCompact', 'PostCompact'])
def test_hook_rejects_foreign_source_before_capture(tmp_path, monkeypatch, capsys, event):
    from test_hook import project
    _, source, payload = project(tmp_path, event=event)
    foreign = tmp_path / 'other'
    initialize(foreign, plugin_hook=True)
    payload['cwd'] = str(foreign)
    spawned = []
    monkeypatch.setattr(hook, 'spawn', lambda *args: spawned.append(args))
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(payload)))
    assert hook.main() == 0
    assert not list((foreign / '.seywo/inbox').rglob('*.z'))
    assert not list((foreign / '.seywo/inbox').rglob('*.jsonl'))
    assert not list((foreign / '.seywo/inbox/pending').glob('*.json'))
    assert not spawned and '归属' in json.loads(capsys.readouterr().out)['systemMessage']
    assert source.is_file()


@pytest.mark.parametrize('dialogue', [True, False])
def test_direct_compact_rejects_foreign_source_before_capture(tmp_path, dialogue):
    from seywo.store import Store
    from test_hook import project
    _, source, _ = project(tmp_path)
    other = tmp_path / 'other'
    config = load_config(Path(initialize(other)['config']))
    with Store(config) as store, pytest.raises(ValueError, match='source cwd differs'):
        store.import_compact(source, 'chat-1', str(source), dialogue=dialogue)
    assert not list((other / '.seywo/inbox').rglob('*.z'))
    assert not list((other / '.seywo/inbox').rglob('*.jsonl'))


def test_unicode_spelling_requires_same_physical_owner(tmp_path):
    actual = tmp_path / 'Caf\u00e9'
    other = tmp_path / 'Cafe\u0301'
    config = Path(initialize(actual)['config'])
    other.mkdir(exist_ok=True)
    binding = ProjectBinding.at(other)
    assert binding.matches_stored_owner(str(actual)) == actual.samefile(other)
    if actual.samefile(other):
        assert load_config(binding.config_path)['project_id'] == load_config(config)['project_id']


def test_retry_rechecks_source_before_writing_blocks(tmp_path, monkeypatch):
    from seywo import archive, compact_pipeline, inbox
    from seywo.store import Store
    from test_hook import project

    _, source, _ = project(tmp_path)
    config = load_config(Path(initialize(tmp_path / 'other')['config']))
    missing = tmp_path / 'delayed.jsonl'
    item = inbox.record(config, 'chat-1', 'PreCompact', str(missing))
    assert item['snapshot'] is None
    missing.write_bytes(source.read_bytes())
    with Store(config) as store, monkeypatch.context() as guard:
        guard.setattr(archive, '_atomic', lambda *_: pytest.fail('foreign archive write attempted'))
        assert not compact_pipeline._import_claimed(store, config, 'chat-1', inbox.claim(config, item))
        assert not store.has_admitted_source('chat-1')
        failed, = inbox.failed(config, 'chat-1')
        assert failed['error_code'] == 'source_project_unverified'
    # Correct the source and retry the original boundary; no second record is needed.
    rows = missing.read_text().splitlines()
    header = json.loads(rows[0])
    header['payload']['cwd'] = config['project_root']
    missing.write_text('\n'.join([json.dumps(header), *rows[1:]]) + '\n')
    inbox.retry(config, failed)
    with Store(config) as store:
        queued, = inbox.pending(config, 'chat-1')
        assert queued['id'] == item['id']
        assert compact_pipeline._import_claimed(store, config, 'chat-1', inbox.claim(config, queued))
        assert store.has_admitted_source('chat-1')


def test_stop_cannot_replace_owned_boundary_with_foreign_content(tmp_path, monkeypatch, capsys):
    from seywo import archive, inbox
    from test_hook import project

    config, source, payload = project(tmp_path)
    initial = inbox.record(config, 'chat-1', 'PreCompact', str(source))
    other = tmp_path / 'other'
    other.mkdir()
    header = {'type': 'session_meta', 'payload': {'id': 'chat-1', 'cwd': str(other)}}
    source.write_text(json.dumps(header) + '\n')
    payload['hook_event_name'] = 'Stop'
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(archive, '_atomic', lambda *_: pytest.fail('foreign archive write attempted'))
    monkeypatch.setattr(hook, 'spawn', lambda *_: pytest.fail('unexpected worker'))
    assert hook.main() == 0
    assert '归属' in json.loads(capsys.readouterr().out)['systemMessage']
    remaining, = inbox.pending(config, 'chat-1')
    assert remaining['id'] == initial['id'] and remaining['snapshot'] == initial['snapshot']


@pytest.mark.parametrize('cwd', [None, '', 'relative', 3])
def test_missing_or_invalid_cwd_never_authorizes_capture(tmp_path, cwd, monkeypatch):
    from seywo import archive, inbox
    from seywo.project_scope import SourceOwnerMismatch

    config = load_config(Path(initialize(tmp_path)['config']))
    source = tmp_path / 'source.jsonl'
    meta = {'id': 'chat'} if cwd is None else {'id': 'chat', 'cwd': cwd}
    source.write_text(json.dumps({'type': 'session_meta', 'payload': meta}) + '\n')
    monkeypatch.setattr(archive, '_atomic', lambda *_: pytest.fail('unverified archive write attempted'))
    with pytest.raises(SourceOwnerMismatch):
        inbox.record(config, 'chat', 'PreCompact', str(source))
    assert not inbox.pending(config, 'chat')


@pytest.mark.parametrize('route', ['inbox', 'raw', 'managed'])
def test_second_header_cannot_publish_or_leave_objects(tmp_path, route):
    from seywo import archive, inbox
    from seywo.project_scope import SourceOwnerMismatch
    from seywo.store import Store
    from test_hook import message, project

    config, source, _ = project(tmp_path)
    item = inbox.record(config, 'chat-1', 'PreCompact', str(source))
    folder = inbox.root(config)
    with source.open('a') as f:
        for _ in range(12):
            print(json.dumps(message('A' * 40000)), file=f)
        print(json.dumps({'type': 'session_meta', 'payload': {'cwd': str(tmp_path), 'id': 'other'}}), file=f)
        print(json.dumps(message('foreign body')), file=f)
    if route == 'managed':
        target = folder / 'live' / 'pre-existing.jsonl'
        archive.capture(source, target)
        source = target
    def files():
        return {p for p in folder.rglob('*') if p.suffix in {'.json', '.jsonl', '.z'}}
    before = files()
    with Store(config) as store, pytest.raises(SourceOwnerMismatch):
        if route == 'inbox':
            inbox.record(config, 'chat-1', 'Stop', str(source))
        else:
            store.import_compact(source, 'chat-1', str(source))
    assert files() == before
    assert [r['id'] for r in inbox.pending(config, 'chat-1')] == [item['id']]
