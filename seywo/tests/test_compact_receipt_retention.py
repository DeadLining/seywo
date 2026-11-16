"""Compact is a processing trigger, not a second semantic history store."""
import sqlite3

from seywo.compact_pipeline import _record
from seywo.compact_schema import setup


def receipt(identity, chat='chat', state='done'):
    return (identity, chat, 'null', '[1,2]', 0, 1, state, 2, 1, 1,
            100, 50, 20, 10, '[{"title":"duplicate segment"}]',
            '[{"title":"duplicate change"}]', None, 1, 1)


def database():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE chats(id TEXT PRIMARY KEY)')
    db.executemany('INSERT INTO chats VALUES(?)', [('chat',), ('other',)])
    db.commit()
    setup(db)
    assert not db.in_transaction
    return db


def test_receipts_do_not_duplicate_semantics_and_retention_is_per_chat():
    db = database()
    for i in range(100):
        _record(db, receipt(str(i)))
    _record(db, receipt('other', 'other'))
    _record(db, receipt('failed', state='failed'))
    assert db.execute('SELECT count(*) FROM compact_batches WHERE chat_id="chat"').fetchone()[0] == 2
    assert db.execute('SELECT DISTINCT segments_json,changes_json FROM compact_batches').fetchall() == [('[]', '[]')]
    _record(db, receipt('success'))
    assert db.execute('SELECT id FROM compact_batches WHERE chat_id="chat"').fetchall() == [('success',)]
    assert db.execute('SELECT count(*) FROM compact_batches WHERE chat_id="other"').fetchone()[0] == 1


def test_existing_receipts_are_trimmed_transactionally_without_losing_retry_state():
    db = database()
    for i in range(4):
        db.execute('INSERT INTO compact_batches VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (*receipt(str(i)), '[]'))
    db.commit()
    db.execute('BEGIN IMMEDIATE')
    setup(db)
    assert db.in_transaction
    assert db.execute('SELECT count(*) FROM compact_batches').fetchone()[0] == 1
    assert db.execute('SELECT segments_json,changes_json FROM compact_batches').fetchone() == ('[]', '[]')
    db.rollback()
    assert db.execute('SELECT count(*) FROM compact_batches').fetchone()[0] == 4
