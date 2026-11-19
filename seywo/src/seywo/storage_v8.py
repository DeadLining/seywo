"""Schema 8: stable occurrence identities, shared descriptors and compact FTS."""
from functools import lru_cache
import hashlib
import json
import zlib

from . import execution_documents
from .projection import search_tokens

DETAILS = ('raw_sha256', 'kind', 'role', 'channel', 'tool', 'occurred_at',
           'safe_text', 'safe_sha256', 'projection_json', 'turn_id', 'root_turn_id')


def descriptor(db, values):
    body = json.dumps(values, ensure_ascii=False, separators=(',', ':')).encode()
    identity = hashlib.sha256(body).hexdigest()
    row = db.execute('SELECT id FROM fragment_descriptors WHERE hash=?', (identity,)).fetchone()
    if row:
        return row[0]
    return db.execute('INSERT INTO fragment_descriptors(hash,data) VALUES(?,?)',
                      (identity, zlib.compress(body, 3))).lastrowid


def register(db):
    @lru_cache(maxsize=256)
    def unpack(data):
        return json.loads(zlib.decompress(data))
    db.create_function('seywo_fragment_field', 2, lambda data, i: unpack(data)[i])
    db.create_function('seywo_descriptor_put', len(DETAILS), lambda *v: descriptor(db, v))
    db.create_function('seywo_search_tokens', 1, search_tokens)
    execution_documents.register(db)


def insert_fragment(db, values):
    keys = [k for k in values if k not in DETAILS]
    columns = keys + ['descriptor_id']
    params = [values[k] for k in keys] + [descriptor(db, [values.get(k) for k in DETAILS])]
    return db.execute('INSERT INTO fragment_records(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in params) + ')', params).lastrowid


def _fragments(db):
    views = {r[0]: r[1] for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='view' AND name IN ('stored_fragments','snapshot_fragments','current_sources')")}
    columns = [r[1] for r in db.execute('PRAGMA table_info(source_fragments)')]
    for name in ('current_sources', 'snapshot_fragments', 'stored_fragments'):
        db.execute('DROP VIEW ' + name)
    db.execute('ALTER TABLE source_fragments RENAME TO fragment_records')
    db.execute('CREATE TABLE fragment_descriptors(id INTEGER PRIMARY KEY,hash TEXT NOT NULL UNIQUE,data BLOB NOT NULL)')
    db.execute('ALTER TABLE fragment_records ADD COLUMN descriptor_id INTEGER REFERENCES fragment_descriptors(id)')
    for row in db.execute('SELECT row_id,' + ','.join(DETAILS) + ' FROM fragment_records ORDER BY row_id'):
        db.execute('UPDATE fragment_records SET descriptor_id=? WHERE row_id=?', (descriptor(db, list(row)[1:]), row[0]))
    for name in DETAILS:
        db.execute('ALTER TABLE fragment_records DROP COLUMN ' + name)
    db.execute('CREATE INDEX fragments_descriptor ON fragment_records(descriptor_id)')
    for operation in ('INSERT', 'UPDATE'):
        db.execute(f"CREATE TRIGGER fragment_descriptor_required_{operation.lower()} BEFORE {operation} ON fragment_records WHEN NEW.descriptor_id IS NULL BEGIN SELECT RAISE(ABORT,'source descriptor required'); END")
    fields = [f'seywo_fragment_field(d.data,{DETAILS.index(k)}) AS {k}' if k in DETAILS else 'f.' + k for k in columns]
    db.execute('CREATE VIEW source_fragments AS SELECT ' + ','.join(fields) + ' FROM fragment_records f JOIN fragment_descriptors d ON d.id=f.descriptor_id')
    physical = [r[1] for r in db.execute('PRAGMA table_info(fragment_records)') if r[1] != 'descriptor_id']
    desc = 'seywo_descriptor_put(' + ','.join('NEW.' + k for k in DETAILS) + ')'
    db.execute('CREATE TRIGGER fragment_update INSTEAD OF UPDATE ON source_fragments BEGIN UPDATE fragment_records SET ' +
               ','.join(k + '=NEW.' + k for k in physical) + ',descriptor_id=' + desc + ' WHERE row_id=OLD.row_id; END')
    db.execute('CREATE TRIGGER fragment_delete INSTEAD OF DELETE ON source_fragments BEGIN DELETE FROM fragment_records WHERE row_id=OLD.row_id; END')
    for name in ('stored_fragments', 'snapshot_fragments', 'current_sources'):
        db.execute(views[name])


def _tasks(db):
    execution_documents.setup(db)
    writer = execution_documents.Writer(db)
    db.execute('ALTER TABLE understanding_tasks RENAME TO task_records')
    columns = [r[1] for r in db.execute('PRAGMA table_info(task_records)')]
    for row in db.execute('SELECT id,job_id,phase,request_json,result_json FROM task_records ORDER BY rowid'):
        chat = db.execute('SELECT chat_id FROM understanding_jobs WHERE id=?', (row[1],)).fetchone()[0]
        refs = [writer.put(row[i], family=f'{chat}:{row[2]}:{kind}')
                for i, kind in ((3, 'request'), (4, 'result'))]
        db.execute('UPDATE task_records SET request_json=?,result_json=? WHERE id=?', (*refs, row[0]))
    fields = ['seywo_execution_read(t.' + k + ') AS ' + k if k in {'request_json', 'result_json'} else 't.' + k for k in columns]
    db.execute('CREATE VIEW understanding_tasks AS SELECT ' + ','.join(fields) + ' FROM task_records t')
    def value(k):
        if k in {'request_json', 'result_json'}:
            return f"seywo_execution_put(NEW.{k},NEW.job_id,NEW.phase,'{'request' if k == 'request_json' else 'result'}')"
        return 'NEW.' + k
    db.execute('CREATE TRIGGER task_insert INSTEAD OF INSERT ON understanding_tasks BEGIN INSERT INTO task_records(' + ','.join(columns) + ') VALUES(' + ','.join(value(k) for k in columns) + '); END')
    db.execute('CREATE TRIGGER task_update INSTEAD OF UPDATE ON understanding_tasks BEGIN UPDATE task_records SET ' + ','.join(k + '=' + value(k) for k in columns) + ' WHERE id=OLD.id; END')
    db.execute('CREATE TRIGGER task_delete INSTEAD OF DELETE ON understanding_tasks BEGIN DELETE FROM task_records WHERE id=OLD.id; END')


def _search(db):
    ids = [r[0] for r in db.execute('SELECT rowid FROM source_fts')]
    db.execute('DROP TABLE source_fts')
    db.execute('CREATE TABLE search_documents(id INTEGER PRIMARY KEY REFERENCES source_payloads(id) ON DELETE CASCADE)')
    db.executemany('INSERT INTO search_documents SELECT id FROM source_payloads WHERE id=?', ((i,) for i in ids))
    db.execute('''CREATE VIEW search_content AS SELECT p.id AS rowid,
        seywo_search_tokens(seywo_source_text(p.data)) AS tokens FROM source_payloads p JOIN search_documents d ON d.id=p.id''')
    db.execute('''CREATE VIRTUAL TABLE source_fts USING fts5(tokens,content='search_content',content_rowid='rowid',
        detail=full,columnsize=0,tokenize='unicode61')''')
    db.execute("INSERT INTO source_fts(source_fts) VALUES('rebuild')")


def migrate(db):
    if db.execute('PRAGMA user_version').fetchone()[0] != 7:
        return
    try:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('PRAGMA user_version').fetchone()[0] == 7:
            _fragments(db)
            _tasks(db)
            _search(db)
            db.execute('PRAGMA user_version=8')
        db.commit()
    except BaseException:
        db.rollback()
        raise
