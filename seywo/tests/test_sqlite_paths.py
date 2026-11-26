"""SQLite locators preserve path identity and never weaken open modes."""
import os
from pathlib import Path, PureWindowsPath
import sqlite3

import pytest

from seywo.sqlite_paths import sqlite_uri
from seywo.windows_paths import extended_windows_path


@pytest.mark.parametrize(('raw', 'expected'), [
    (r'D:\seywo\.seywo\index.sqlite3', 'file:///D:/seywo/.seywo/index.sqlite3'),
    (r'\\?\D:\seywo\.seywo\index.sqlite3', 'file:///D:/seywo/.seywo/index.sqlite3'),
    (r'\\server\share\index.sqlite3', 'file:////server/share/index.sqlite3'),
    (r'\\?\UNC\server\share\index.sqlite3', 'file:////server/share/index.sqlite3'),
    (r'\\?\D:\项目 空格\a%#?.sqlite',
     'file:///D:/%E9%A1%B9%E7%9B%AE%20%E7%A9%BA%E6%A0%BC/a%25%23%3F.sqlite'),
])
def test_windows_paths_have_no_uri_authority(raw, expected):
    assert sqlite_uri(PureWindowsPath(raw), mode='ro') == expected + '?mode=ro'


@pytest.mark.parametrize('raw', [r'D:relative.sqlite', r'\\.\PhysicalDrive0',
                                r'\\?\GLOBALROOT\Device\HarddiskVolume1\db.sqlite'])
def test_ambiguous_or_device_paths_are_rejected(raw):
    with pytest.raises(ValueError):
        sqlite_uri(PureWindowsPath(raw), mode='rw')


def test_open_existing_special_character_database_read_only(tmp_path):
    # URI punctuation is valid in POSIX filenames and must never become options.
    path = tmp_path / ('项目 空格 % # ?.sqlite' if os.name != 'nt' else '项目 空格 % #.sqlite')
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE proof(value)')
        db.execute('INSERT INTO proof VALUES (42)')
    with sqlite3.connect(sqlite_uri(path, mode='ro'), uri=True) as db:
        assert db.execute('SELECT value FROM proof').fetchone() == (42,)
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            db.execute('DELETE FROM proof')


@pytest.mark.parametrize('mode', ['ro', 'rw'])
def test_missing_database_is_never_created(tmp_path, mode):
    path = tmp_path / 'missing.sqlite'
    with pytest.raises(sqlite3.OperationalError):
        sqlite3.connect(sqlite_uri(path, mode=mode), uri=True)
    assert not path.exists()


def test_only_read_existing_modes_allowed(tmp_path):
    with pytest.raises(ValueError):
        sqlite_uri(tmp_path / 'db.sqlite', mode='rwc')
    with pytest.raises(ValueError):
        sqlite_uri(Path('relative.sqlite'), mode='ro')


@pytest.mark.skipif(os.name != 'nt', reason='requires native Windows SQLite')
@pytest.mark.parametrize('extended', [False, True])
def test_native_windows_sqlite_reads_same_database(tmp_path, extended):
    path = tmp_path / '项目 空格 % #.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE proof(value)')
        db.execute('INSERT INTO proof VALUES (42)')
    locator = extended_windows_path(path) if extended else path
    with sqlite3.connect(sqlite_uri(locator, mode='ro'), uri=True) as db:
        assert db.execute('SELECT value FROM proof').fetchone() == (42,)


def test_project_database_preserves_special_path_identity(tmp_path):
    from seywo.config import initialize, load_config
    from seywo.store import Store

    project = tmp_path / '项目 %23 # 空格'
    project.mkdir()
    first = initialize(project)
    config = load_config(project / '.seywo' / 'config.json')
    with Store(config, read_only=True) as store:
        assert store.db.execute('SELECT id, root FROM projects').fetchone()['id'] == first['project_id']
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            store.db.execute('DELETE FROM projects')
    with Store(config) as store:
        assert store.db.execute('SELECT id FROM projects').fetchone()[0] == first['project_id']
    assert not (project / '.seywo' / 'index.sqlite3?mode=ro').exists()
