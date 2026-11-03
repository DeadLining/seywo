"""Versioned evidence validity, invalidated transactionally by SQL-only triggers.

Only IDs and booleans are cached. Original bytes remain in their existing store.
Old writers invalidate the cache without knowing this module. Readers never write.
"""
from collections import defaultdict


def exists(db,name):
    return bool(db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(name,)).fetchone())


def setup(store):
    db=store.db
    if not exists(db,'sg2_relation_evidence'): return
    db.executescript('''
    CREATE INDEX IF NOT EXISTS sg2_relation_evidence_lookup ON sg2_relation_evidence(relation,job,id);
    CREATE INDEX IF NOT EXISTS sg2_relation_evidence_source ON sg2_relation_evidence(entry_id);
    CREATE TABLE IF NOT EXISTS sg_read_evidence(id TEXT PRIMARY KEY,valid INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS sg_read_evidence_dirty(id TEXT PRIMARY KEY) WITHOUT ROWID;
    CREATE TRIGGER IF NOT EXISTS sg_read_ev_insert AFTER INSERT ON sg2_relation_evidence BEGIN
      INSERT OR IGNORE INTO sg_read_evidence_dirty VALUES(new.id); END;
    CREATE TRIGGER IF NOT EXISTS sg_read_ev_update AFTER UPDATE ON sg2_relation_evidence BEGIN
      INSERT OR IGNORE INTO sg_read_evidence_dirty VALUES(new.id); END;
    CREATE TRIGGER IF NOT EXISTS sg_read_ev_delete AFTER DELETE ON sg2_relation_evidence BEGIN
      DELETE FROM sg_read_evidence WHERE id=old.id;
      DELETE FROM sg_read_evidence_dirty WHERE id=old.id; END;
    CREATE TRIGGER IF NOT EXISTS sg_read_ev_source_insert AFTER INSERT ON entries BEGIN
      INSERT OR IGNORE INTO sg_read_evidence_dirty SELECT id FROM sg2_relation_evidence WHERE entry_id=new.id; END;
    CREATE TRIGGER IF NOT EXISTS sg_read_ev_source_update AFTER UPDATE OF text ON entries WHEN old.text IS NOT new.text BEGIN
      INSERT OR IGNORE INTO sg_read_evidence_dirty SELECT id FROM sg2_relation_evidence WHERE entry_id=new.id; END;
    CREATE TRIGGER IF NOT EXISTS sg_read_ev_source_delete AFTER DELETE ON entries BEGIN
      INSERT OR IGNORE INTO sg_read_evidence_dirty SELECT id FROM sg2_relation_evidence WHERE entry_id=old.id; END;
    ''')
    version=db.execute("SELECT value FROM metadata WHERE key='relation_proof_version'").fetchone()
    if not version or version[0]!='literal-proof-v1':
        with db:
            db.execute('INSERT OR IGNORE INTO sg_read_evidence_dirty SELECT id FROM sg2_relation_evidence')
            db.execute("INSERT OR REPLACE INTO metadata VALUES('relation_proof_version','literal-proof-v1')")


def source_checks(db,rows):
    ids=list({r['entry_id'] for r in rows}); originals={}
    for start in range(0,len(ids),400):
        chunk=ids[start:start+400]
        text = 'sg_source_text(id,text)' if exists(db, 'sg_external_entries') else 'text'
        originals.update(db.execute('SELECT id,'+text+' FROM entries WHERE id IN ('+','.join('?' for _ in chunk)+')',chunk))
    checked={}; result={}
    for row in rows:
        key=(row['entry_id'],row['quote'])
        if key not in checked:
            checked[key]=bool(row['quote'] and row['quote'] in (originals.get(row['entry_id']) or ''))
        result[row['id']]=checked[key]
    return result


def flush(store):
    db=store.db
    if store.readonly: raise ValueError('store is read-only')
    if not exists(db,'sg_read_evidence_dirty') or not db.execute('SELECT 1 FROM sg_read_evidence_dirty LIMIT 1').fetchone(): return
    from .read_projection import publication
    with publication(db):
        rows=[dict(r) for r in db.execute('SELECT e.id,e.entry_id,e.quote FROM sg2_relation_evidence e JOIN sg_read_evidence_dirty d ON d.id=e.id')]
        db.executemany('INSERT OR REPLACE INTO sg_read_evidence VALUES(?,?)',((key,int(value)) for key,value in source_checks(db,rows).items()))
        db.execute('DELETE FROM sg_read_evidence_dirty')


def verified(store, relations):
    db=store.db; evidence=[]
    cached=exists(db,'sg_read_evidence') and exists(db,'sg_read_evidence_dirty')
    extra=',p.valid cached_valid,d.id dirty' if cached else ',NULL cached_valid,NULL dirty'
    join=' LEFT JOIN sg_read_evidence p ON p.id=x.id LEFT JOIN sg_read_evidence_dirty d ON d.id=x.id' if cached else ''
    for start in range(0,len(relations),400):
        ids=relations[start:start+400]
        evidence.extend(dict(r) for r in db.execute(
            'SELECT x.id,x.relation,x.entry_id,x.quote,x.job'+extra+' FROM sg2_relation_evidence x'+join+' WHERE x.relation IN ('
            +','.join('?' for _ in ids)+') ORDER BY x.id',ids))
    pending=[r for r in evidence if r['cached_valid'] is None or r['dirty'] is not None]
    checks=source_checks(db,pending)
    invalid={(r['relation'],r['job']) for r in evidence if not checks.get(r['id'],r['cached_valid'])}
    result=defaultdict(list)
    for row in evidence:
        if (row['relation'],row['job']) not in invalid and len(result[row['relation']])<8:
            result[row['relation']].append({'entry_id':row['entry_id'],'quote':row['quote']})
    return dict(result)
