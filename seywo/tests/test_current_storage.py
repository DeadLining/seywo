from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from seywo.config import initialize, load_config
from seywo.store import SCHEMA_VERSION, Store


@pytest.mark.parametrize('version', [1, 7, 9])
@pytest.mark.parametrize('read_only', [True, False])
def test_open_refuses_noncurrent_indexes_without_writes(tmp_path, version, read_only):
    config = load_config(Path(initialize(tmp_path)['config']))
    database = Path(config['_database'])
    with closing(sqlite3.connect(database)) as db, db:
        db.execute(f'PRAGMA user_version={version}')
    before = database.read_bytes()
    with pytest.raises(ValueError, match='unsupported Seywo storage schema'):
        with Store(config, read_only=read_only):
            pass
    assert database.read_bytes() == before
    assert not (database.parent / 'maintenance/storage-migration').exists()


def test_fresh_index_is_created_directly_at_the_current_schema(tmp_path):
    config = load_config(Path(initialize(tmp_path)['config']))
    with Store(config, read_only=True) as store:
        assert store.db.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        assert store.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not store.db.execute('PRAGMA foreign_key_check').fetchall()
