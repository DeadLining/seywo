"""Deterministic, disposable read indexes. Never store another copy of source text.

SQL-only dirty triggers also work for old writers. Publication is one transaction;
readers use that snapshot or a linear, read-only fallback for unprojected writes.
"""
from collections import deque
from contextlib import contextmanager
import json
import posixpath
import re
import zlib

VERSION = 'literal-projection-v1'
BITS = 4096
PATH = re.compile(r'(?<![\w:/])(?:/|\./|[\w.-]+/)[\w./@+~-]+\.[a-zA-Z0-9]{1,12}(?![\w/])')


def items(raw):
    try:
        value = json.loads(raw or '[]')
        return [x for x in value if isinstance(x,str)] if isinstance(value,list) else []
    except (ValueError,TypeError):
        return []


def folded(text):
    # Candidate generation only. Exact Python re.I boundaries are rechecked below.
    return text.casefold().replace('\u0131','i').replace('i\u0307','i')


class Matcher:
    """Aho-Corasick candidate scan, followed by the original literal predicate.

    No alias-by-every-character nested loop; overlapping and Unicode candidates
    are preserved. Candidate normalization is not evidence of an actual match.
    """
    def __init__(self, terms):
        self.next = [{}]; self.fail = [0]; self.outputs = [set()]
        self.patterns = {}
        for term in sorted(set(terms)):
            state = 0
            for ch in folded(term):
                nxt = self.next[state].get(ch)
                if nxt is None:
                    nxt = len(self.next); self.next[state][ch] = nxt
                    self.next.append({}); self.fail.append(0); self.outputs.append(set())
                state = nxt
            self.outputs[state].add(term)
            self.patterns[term] = re.compile(r'(?<![A-Za-z0-9_])'+re.escape(term)+r'(?![A-Za-z0-9_])',re.I)
        queue = deque(self.next[0].values())
        while queue:
            state = queue.popleft()
            for ch, nxt in self.next[state].items():
                queue.append(nxt); fail = self.fail[state]
                while fail and ch not in self.next[fail]: fail = self.fail[fail]
                self.fail[nxt] = self.next[fail].get(ch,0)
                self.outputs[nxt].update(self.outputs[self.fail[nxt]])

    def matches(self,text):
        if not self.patterns: return set()
        state = 0; candidates = set()
        for ch in folded(text):
            while state and ch not in self.next[state]: state = self.fail[state]
            state = self.next[state].get(ch,0)
            candidates.update(self.outputs[state])
        return {term for term in candidates if self.patterns[term].search(text)}


def bloom(text):
    text=folded(text); bits=0
    for gram in {text[i:i+3] for i in range(max(0,len(text)-2))}:
        h=zlib.crc32(gram.encode())
        for shift in (0,10,20): bits |= 1 << ((h >> shift) % BITS)
    return bits.to_bytes(BITS//8,'little')


def possible(bits,term):
    text=folded(term); value=int.from_bytes(bits,'little')
    for i in range(len(text)-2):
        h=zlib.crc32(text[i:i+3].encode())
        if any(not value & (1 << ((h >> shift) % BITS)) for shift in (0,10,20)): return False
    return True


def available(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='sg_read_nodes'").fetchone())


def setup(store):
    db=store.db
    db.executescript('''
    CREATE TABLE IF NOT EXISTS sg_read_nodes(id TEXT PRIMARY KEY,valid INTEGER NOT NULL,
      aliases_json TEXT NOT NULL,paths_json TEXT NOT NULL,bloom BLOB NOT NULL);
    CREATE TABLE IF NOT EXISTS sg_read_aliases(node TEXT NOT NULL,term TEXT NOT NULL,PRIMARY KEY(node,term)) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS sg_read_alias_terms ON sg_read_aliases(term,node);
    CREATE TABLE IF NOT EXISTS sg_read_mentions(node TEXT NOT NULL,term TEXT NOT NULL,PRIMARY KEY(node,term)) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS sg_read_mention_terms ON sg_read_mentions(term,node);
    CREATE TABLE IF NOT EXISTS sg_read_dirty(id TEXT PRIMARY KEY) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS work_memory_source_entry ON work_memory(source_entry_id);
    CREATE INDEX IF NOT EXISTS work_memory_page ON work_memory(project_id,updated_at DESC,id DESC);
    CREATE TRIGGER IF NOT EXISTS sg_read_insert AFTER INSERT ON work_memory BEGIN
      INSERT OR IGNORE INTO sg_read_dirty VALUES(new.id); END;
    CREATE TRIGGER IF NOT EXISTS sg_read_update AFTER UPDATE OF title,content,aliases_json,paths_json,source_entry_id,source_quote ON work_memory BEGIN
      INSERT OR IGNORE INTO sg_read_dirty VALUES(new.id); END;
    CREATE TRIGGER IF NOT EXISTS sg_read_delete AFTER DELETE ON work_memory BEGIN
      INSERT OR IGNORE INTO sg_read_dirty VALUES(old.id); END;
    CREATE TRIGGER IF NOT EXISTS sg_read_source_insert AFTER INSERT ON entries BEGIN
      INSERT OR IGNORE INTO sg_read_dirty SELECT id FROM work_memory WHERE source_entry_id=new.id; END;
    CREATE TRIGGER IF NOT EXISTS sg_read_source_update AFTER UPDATE OF text ON entries WHEN old.text IS NOT new.text BEGIN
      INSERT OR IGNORE INTO sg_read_dirty SELECT id FROM work_memory WHERE source_entry_id=new.id; END;
    CREATE TRIGGER IF NOT EXISTS sg_read_source_delete AFTER DELETE ON entries BEGIN
      INSERT OR IGNORE INTO sg_read_dirty SELECT id FROM work_memory WHERE source_entry_id=old.id; END;
    ''')
    version=db.execute("SELECT value FROM metadata WHERE key='read_projection_version'").fetchone()
    if not version or version[0]!=VERSION:
        with db:
            db.execute('INSERT OR IGNORE INTO sg_read_dirty SELECT id FROM work_memory')
            db.execute("INSERT OR REPLACE INTO metadata VALUES('read_projection_version',?)",(VERSION,))
    from .relation_evidence import setup as setup_proofs
    setup_proofs(store)
    flush(store)


def rows_by_id(db,ids):
    ids=list(ids)
    for start in range(0,len(ids),400):
        chunk=ids[start:start+400]
        yield from (dict(r) for r in db.execute('SELECT * FROM work_memory WHERE id IN ('+','.join('?' for _ in chunk)+')',chunk))


def derive(store,rows):
    ids={r['source_entry_id'] for r in rows if r['source_entry_id']}
    originals={}
    ids=list(ids)
    for start in range(0,len(ids),400):
        chunk=ids[start:start+400]
        originals.update(store.db.execute('SELECT id,text FROM entries WHERE id IN ('+','.join('?' for _ in chunk)+')',chunk))
    result={}; root=store.config['roots'][0]
    for row in rows:
        quote=row['source_quote'] or ''; text=row['title']+' '+row['content']
        valid=bool(quote and quote in originals.get(row['source_entry_id'],''))
        aliases=set(); paths=[]
        if valid:
            for value in items(row['aliases_json']):
                term=value.strip().casefold()
                if (3<=len(term)<=48 and '/' not in term and '.' not in term
                    and not re.fullmatch(r'[\d\W_]+',term) and not re.fullmatch(r'[a-f\d-]{20,}',term)):
                    pattern=re.compile(r'(?<![A-Za-z0-9_])'+re.escape(term)+r'(?![A-Za-z0-9_])',re.I)
                    if pattern.search(quote) and pattern.search(text): aliases.add(term)
            raw=set(items(row['paths_json']))
            raw.update(m.group(0) for m in PATH.finditer(quote) if m.group(0) in row['title']+'\n'+row['content'])
            seen=set()
            for path in sorted(raw):
                if not path or path not in quote or '\x00' in path or '://' in path: continue
                canonical=posixpath.normpath(path if path.startswith('/') else posixpath.join(root,path))
                if canonical not in seen: paths.append([canonical,path]); seen.add(canonical)
        result[row['id']]={'valid':valid,'aliases':sorted(aliases),'paths':paths,'mentions':set()}
    return result


@contextmanager
def publication(db):
    if db.in_transaction:
        # The caller owns both source facts and derived publication atomically.
        yield
    else:
        with db:
            db.execute('BEGIN IMMEDIATE')
            yield


def flush(store):
    """Writable entry points only. Do not commit a caller's unfinished transaction."""
    db=store.db
    if store.readonly: raise ValueError('store is read-only')
    from .relation_evidence import flush as flush_proofs
    flush_proofs(store)
    if not available(db) or not db.execute('SELECT 1 FROM sg_read_dirty LIMIT 1').fetchone(): return
    with publication(db):
        dirty={r[0] for r in db.execute('SELECT id FROM sg_read_dirty')}
        old_terms={r[0] for r in db.execute('SELECT DISTINCT term FROM sg_read_aliases')}
        rows=list(rows_by_id(db,dirty)); derived=derive(store,rows)
        db.executemany('DELETE FROM sg_read_aliases WHERE node=?',((i,) for i in dirty))
        db.executemany('DELETE FROM sg_read_mentions WHERE node=?',((i,) for i in dirty))
        db.executemany('DELETE FROM sg_read_nodes WHERE id=?',((i,) for i in dirty))
        for row in rows:
            d=derived[row['id']]
            db.execute('INSERT INTO sg_read_nodes VALUES(?,?,?,?,?)',(row['id'],int(d['valid']),json.dumps(d['aliases']),json.dumps(d['paths']),bloom(row['source_quote'] or '') if d['valid'] else bytes(BITS//8)))
            db.executemany('INSERT INTO sg_read_aliases VALUES(?,?)',((row['id'],t) for t in d['aliases']))
        terms={r[0] for r in db.execute('SELECT DISTINCT term FROM sg_read_aliases')}
        db.executemany('DELETE FROM sg_read_mentions WHERE term=?',((t,) for t in old_terms-terms))
        matcher=Matcher(terms)
        for row in rows:
            if derived[row['id']]['valid']:
                hits=matcher.matches(row['source_quote']) & matcher.matches(row['title']+'\n'+row['content'])
                db.executemany('INSERT OR IGNORE INTO sg_read_mentions VALUES(?,?)',((row['id'],t) for t in hits))
        added=terms-old_terms
        if added:
            matcher=Matcher(added)
            # Compact trigram Bloom signatures filter old rows before loading any text.
            candidates=[r['id'] for r in db.execute('SELECT id,bloom FROM sg_read_nodes WHERE valid=1')
                        if r['id'] not in dirty and any(possible(r['bloom'],t) for t in added)]
            for row in rows_by_id(db,candidates):
                hits=matcher.matches(row['source_quote']) & matcher.matches(row['title']+'\n'+row['content'])
                db.executemany('INSERT OR IGNORE INTO sg_read_mentions VALUES(?,?)',((row['id'],t) for t in hits))
        db.execute('DELETE FROM sg_read_dirty')


def rebuild(store):
    store._ensure_writable()
    with store.db:
        store.db.execute('INSERT OR IGNORE INTO sg_read_dirty SELECT id FROM work_memory')
        store.db.execute('INSERT OR IGNORE INTO sg_read_dirty SELECT id FROM sg_read_nodes')
    flush(store)


def page(store,rows):
    db=store.db; ids=[r['id'] for r in rows]
    if not ids: return {}
    if available(db) and not db.execute('SELECT 1 FROM sg_read_dirty LIMIT 1').fetchone():
        result={}
        for start in range(0,len(ids),400):
            chunk=ids[start:start+400]; marks=','.join('?' for _ in chunk)
            for r in db.execute('SELECT * FROM sg_read_nodes WHERE id IN ('+marks+')',chunk):
                result[r['id']]={'valid':bool(r['valid']),'aliases':json.loads(r['aliases_json']),'paths':json.loads(r['paths_json']),'mentions':set()}
            for r in db.execute('SELECT node,term FROM sg_read_mentions WHERE node IN ('+marks+')',chunk):
                if r[0] in result: result[r[0]]['mentions'].add(r[1])
        if len(result)==len(ids): return result
    # Old writers or an unmigrated DB remain readable without schema writes.
    result=derive(store,rows); matcher=Matcher(t for d in result.values() for t in d['aliases'])
    for row in rows:
        d=result[row['id']]
        if d['valid']: d['mentions']=matcher.matches(row['source_quote']) & matcher.matches(row['title']+'\n'+row['content'])
    return result
