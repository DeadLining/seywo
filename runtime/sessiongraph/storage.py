"""Local indexes and explicit, verified storage migration. No provider calls."""
import hashlib
import json
from pathlib import Path
from .core import tokens, normalize, digest

VERSION = "storage_v2"


def exists(db, table):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone())


def setup(store):
    db = store.db
    if not exists(db, "memory_search"):
        with db:
            db.execute("CREATE VIRTUAL TABLE memory_search USING fts5(body)")
            db.executemany("INSERT INTO memory_search(rowid,body) VALUES (?,?)", (
                (r[0], search_text(*r[1:])) for r in db.execute(
                    "SELECT rowid,title,content,aliases_json,paths_json FROM work_memory").fetchall()))
    # Built-in SQL only: old MCP processes remain able to write after upgrading.
    # Readers include dirty rows until the next writable entry point flushes them.
    db.execute("CREATE TABLE IF NOT EXISTS memory_search_dirty(id TEXT PRIMARY KEY) WITHOUT ROWID")
    previous=db.execute("SELECT sql FROM sqlite_master WHERE name='memory_search_insert'").fetchone()
    if previous and 'sg_search_text' in previous[0]:
        for name in ('insert','update','delete'):db.execute('DROP TRIGGER IF EXISTS memory_search_'+name)
    db.executescript("""
    CREATE TRIGGER IF NOT EXISTS memory_search_insert AFTER INSERT ON work_memory BEGIN
      INSERT OR IGNORE INTO memory_search_dirty VALUES(new.id); END;
    CREATE TRIGGER IF NOT EXISTS memory_search_delete AFTER DELETE ON work_memory BEGIN
      DELETE FROM memory_search WHERE rowid=old.rowid;
      DELETE FROM memory_search_dirty WHERE id=old.id; END;
    CREATE TRIGGER IF NOT EXISTS memory_search_update AFTER UPDATE OF title,content,aliases_json,paths_json ON work_memory
      WHEN new.title IS NOT old.title OR new.content IS NOT old.content OR new.aliases_json IS NOT old.aliases_json OR new.paths_json IS NOT old.paths_json BEGIN
      INSERT OR IGNORE INTO memory_search_dirty VALUES(new.id); END;
    """)
    flush_search(db)
    revision_triggers(db)


def flush_search(db):
    if not db.execute("SELECT 1 FROM memory_search_dirty LIMIT 1").fetchone(): return
    with db:
        db.execute('BEGIN IMMEDIATE')
        rows=db.execute("SELECT m.rowid,m.id,m.title,m.content,m.aliases_json,m.paths_json FROM work_memory m JOIN memory_search_dirty d ON d.id=m.id").fetchall()
        for row in rows:
            db.execute('DELETE FROM memory_search WHERE rowid=?',(row[0],))
            db.execute('INSERT INTO memory_search(rowid,body) VALUES(?,?)',(row[0],search_text(*row[2:])))
        db.execute('DELETE FROM memory_search_dirty')


def search_filter(db, query, alias='work_memory'):
    expr=expression(query)
    if not expr:return '0', []
    condition=f"{alias}.rowid IN (SELECT rowid FROM memory_search WHERE memory_search MATCH ?)"
    args=[expr]
    if exists(db,'memory_search_dirty'):
        from .retrieval import query_terms, is_han_single
        terms=query_terms(query);terms=[t for t in terms if not is_han_single(t)] or terms
        fields=f"lower({alias}.title || char(10) || {alias}.content || char(10) || {alias}.aliases_json || char(10) || {alias}.paths_json)"
        match=' OR '.join(f'instr({fields},?)>0' for _ in terms)
        condition+=f" OR ({alias}.id IN (SELECT id FROM memory_search_dirty) AND ({match}))"
        args.extend(terms)
    return '('+condition+')',args


def search_text(title, content, aliases, paths):
    def items(raw):
        try:
            value=json.loads(raw)
            return [x for x in value if isinstance(x,str)] if isinstance(value,list) else []
        except (ValueError,TypeError):return []
    return " ".join(tokens("\n".join([title, content, *items(aliases), *items(paths)])))


def expression(query):
    from .retrieval import query_terms, is_han_single
    terms = query_terms(query)
    strong = [t for t in terms if not is_han_single(t)]
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in (strong or terms))


def revision_triggers(db):
    # A revision is a change token, not a cryptographic proof of the whole graph.
    db.execute("INSERT OR IGNORE INTO metadata VALUES ('graph_revision_v2','0')")
    tables = ('work_memory', 'entries', 'work_memory_relations', 'sg2_nodes',
              'sg2_relations', 'sg2_unresolved', 'sg2_evidence', 'sg2_relation_evidence')
    for table in tables:
        if not exists(db, table): continue
        for action in ('INSERT','UPDATE','DELETE'):
            name = 'sg_revision_' + table + '_' + action.lower()
            db.execute(f"CREATE TRIGGER IF NOT EXISTS {name} AFTER {action} ON {table} BEGIN "
                       "UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='graph_revision_v2'; END")
    db.commit()


def migrate_sources(store):
    """Remove raw duplicates only after exact verification against the source prefix.

    Missing/changed sources retain their raw snapshots. Each session commits
    atomically; interruption never removes data before locator backfill.
    Existing entries, jobs, evidence, coverage and cursors are not modified.
    """
    from . import source_locator
    from .ingestion import MAX_RECORD_BYTES
    source_locator.setup(store)
    report = dict(verified_sessions=0, removed_events=0, removed_payload_bytes=0,
                  locators_backfilled=0, retained_sessions=[], model_calls=0)
    sessions = store.db.execute("SELECT s.* FROM sessions s WHERE EXISTS "
        "(SELECT 1 FROM raw_events r WHERE r.session_id=s.id)").fetchall()
    for session in sessions:
        sid = session['id']
        # Freeze the indexed frontier; an importer may append after this read.
        entries = {r['line']:r for r in store.db.execute("SELECT * FROM entries WHERE session_id=? AND line<=?", (sid,session['lines']))}
        originals = {r[0]:digest(r[1]) for r in store.db.execute(
            "SELECT line,event_json FROM raw_events WHERE session_id=? AND line<=?", (sid,session['lines']))}
        locations=[]; offset=0; hasher=hashlib.sha256(); found=set(); compact=[]
        try:
            with Path(session['path']).open('rb') as stream:
                for line in range(1,session['lines']+1):
                    raw=stream.readline(MAX_RECORD_BYTES+1)
                    if not raw.endswith(b'\n') or len(raw)>MAX_RECORD_BYTES:
                        raise ValueError('source_incomplete_or_oversize')
                    record=raw[:-1].removesuffix(b'\r'); h=digest(record)
                    if line in originals and h != originals[line]:
                        raise ValueError('source_changed')
                    if line in entries:
                        event=json.loads(record); payload=event.get('payload',{})
                        normalized=normalize(payload); entry=entries[line]
                        if not normalized or normalized[2]!=entry['text']:
                            raise ValueError('entry_text_changed')
                        # Older IDs and hashes are preserved, not regenerated.
                        locations.append((entry['id'],payload.get('channel',''),payload.get('name',''),offset,len(raw),h,digest(entry['text'])))
                        found.add(line)
                    if line in originals:
                        event=json.loads(record)
                        if event.get('type')=='compacted':compact.append((sid,line,h))
                    hasher.update(raw);offset+=len(raw)
            if offset!=session['offset'] or hasher.hexdigest()!=session['prefix_hash'] or found!=set(entries):
                raise ValueError('source_prefix_changed')
        except (OSError, ValueError, TypeError, KeyError) as exc:
            report['retained_sessions'].append({'segment':sid,'reason':type(exc).__name__ if isinstance(exc,OSError) else str(exc)[:80]})
            continue
        with store.db:
            store.db.execute('BEGIN IMMEDIATE')
            # Do not accept a concurrently replaced/reimported session.
            current=store.db.execute('SELECT path,offset,lines FROM sessions WHERE id=?',(sid,)).fetchone()
            if current['path']!=session['path'] or current['offset']<offset or current['lines']<session['lines']:
                raise ValueError('migration_frontier_changed')
            store.db.executemany('INSERT OR REPLACE INTO sg_source_locations VALUES(?,?,?,?,?,?,?)',locations)
            store.db.executemany('INSERT OR IGNORE INTO sg_compact_events VALUES(?,?,?)',compact)
            n,size=store.db.execute('SELECT count(*),coalesce(sum(length(cast(event_json AS blob))),0) FROM raw_events WHERE session_id=? AND line<=?',(sid,session['lines'])).fetchone()
            store.db.execute('DELETE FROM raw_events WHERE session_id=? AND line<=?',(sid,session['lines']))
        report['verified_sessions']+=1;report['removed_events']+=n;report['removed_payload_bytes']+=size
        report['locators_backfilled']+=len(locations)
    return report


def compact_term_tables(store):
    """Same terms and keys, stored once rather than table plus duplicate PK index."""
    report={}
    for table,field,parent in (('sg2_terms','node',' REFERENCES sg2_nodes(id)'),('sg2_pending_terms','item','')):
        sql=store.db.execute("SELECT sql FROM sqlite_master WHERE name=?",(table,)).fetchone()
        if not sql or 'WITHOUT ROWID' in sql[0].upper():continue
        with store.db:
            store.db.execute('BEGIN IMMEDIATE')
            store.db.execute(f'CREATE TABLE {table}_compact(project TEXT NOT NULL,term TEXT NOT NULL,{field} TEXT NOT NULL{parent},PRIMARY KEY(project,term,{field})) WITHOUT ROWID')
            store.db.execute(f'INSERT INTO {table}_compact SELECT * FROM {table}')
            count=store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
            if store.db.execute(f'SELECT count(*) FROM {table}_compact').fetchone()[0]!=count:raise ValueError('term_migration_count')
            store.db.execute(f'DROP TABLE {table}')
            store.db.execute(f'ALTER TABLE {table}_compact RENAME TO {table}')
            if field=='node':store.db.execute('CREATE INDEX sg2_terms_node ON sg2_terms(node)')
        report[table]=count
    return report


def prune_completed_checkpoints(store, job_id=None):
    """Discard only replay caches whose job and every attempt are durably done.

    Caller owns the transaction. Never deletes jobs, split receipts, attempts,
    source coverage, unresolved candidates or the output used by later resolution.
    A terminal job with an active control/reservation/attempt is inconsistent and
    remains untouched for diagnosis. New completions use a primary-key lookup.
    """
    db = store.db
    tables = ('sg2_jobs', 'sg2_checkpoints', 'sg2_claims', 'sg2_control', 'sg2_attempts')
    if not all(exists(db, table) for table in tables):
        return {"checkpoints": 0, "payload_bytes": 0}
    where = """job IN (SELECT j.id FROM sg2_jobs j
        WHERE j.project=? AND j.state='complete'
        AND NOT EXISTS (SELECT 1 FROM sg2_claims c WHERE c.job=j.id)
        AND NOT EXISTS (SELECT 1 FROM sg2_control c WHERE c.job=j.id)
        AND NOT EXISTS (SELECT 1 FROM sg2_attempts a WHERE a.job=j.id AND a.state='running')"""
    args = [store.config['project_id']]
    if job_id is not None:
        where += ' AND j.id=?'
        args.append(job_id)
    where += ')'
    count, size = db.execute(
        'SELECT count(*),coalesce(sum(length(cast(result_json AS blob))),0) FROM sg2_checkpoints WHERE '+where,
        args).fetchone()
    if count:
        db.execute('DELETE FROM sg2_checkpoints WHERE '+where, args)
    return {"checkpoints": count, "payload_bytes": size}


def ranked_memories(store, query, terms, patterns, statuses, chat_filter, chat_args,
                    limit, offset):
    """Rank all matches in SQLite; never cap the candidate set or alter scores.

    Clean rows use token membership in the existing FTS body. Old MCP writes
    are included through the dirty queue and scored with the original matcher.
    Python decodes full records/evidence only for the requested result page.
    """
    from .memory import _match_details
    from .retrieval import is_han_single
    db = store.db
    db.create_function('sg_casefold', 1, str.casefold, deterministic=True)
    def dirty_score(title, content, aliases, paths):
        if not terms:
            return 0.0
        return _match_details(dict(title=title, content=content,
            aliases_json=aliases, paths_json=paths), terms, patterns)['score']
    db.create_function('sg_dirty_score', 4, dirty_score, deterministic=True)
    weights = {t: 0.04 if is_han_single(t) and len(terms) > 1 else 1.0 for t in terms}
    total = max(sum(weights.values()), 1)
    score = ' + '.join('CASE WHEN instr(c.body,?)>0 THEN ? ELSE 0 END' for _ in terms) or '0'
    score_args = [v for t in terms for v in (' '+t+' ', weights[t])]
    strong = [t for t in terms if not is_han_single(t)]
    strong_sql = ' OR '.join('instr(c.body,?)>0' for _ in strong) or '1'
    strong_args = [' '+t+' ' for t in strong]
    expr = expression(query)
    if query.strip() == '*':
        candidates = "SELECT rowid,NULL body,1 dirty FROM work_memory"
        candidate_args = []
    elif not expr:
        return [], 0
    else:
        # FTS rowids lead the join. Do not scan the project index first, which
        # made even a no-hit query proportional to the number of stored nodes.
        candidates = """SELECT rowid,' '||body||' ' body,0 dirty FROM memory_search
            WHERE memory_search MATCH ? AND rowid NOT IN
              (SELECT m.rowid FROM memory_search_dirty d JOIN work_memory m ON m.id=d.id)
            UNION ALL SELECT m.rowid,NULL body,1 dirty FROM memory_search_dirty d
              JOIN work_memory m ON m.id=d.id"""
        candidate_args = [expr]
    eligibility = '1' if query.strip() == '*' else 'lexical>0'
    sql = f"""WITH candidates AS MATERIALIZED ({candidates}),
      matched AS MATERIALIZED (
        SELECT m.rowid rid,m.id,m.updated_at,
          CASE WHEN c.dirty THEN sg_dirty_score(m.title,m.content,m.aliases_json,m.paths_json)
            WHEN ({strong_sql}) THEN round(({score}) / ?,6) ELSE 0 END lexical,
          CASE WHEN instr(sg_casefold(m.title||char(10)||m.content||char(10)||m.paths_json||char(10)||m.aliases_json),?)>0
            THEN 2 ELSE 0 END exact
        FROM candidates c CROSS JOIN work_memory m ON m.rowid=c.rowid
        WHERE m.status IN ({','.join('?' for _ in statuses)})
          AND (m.project_id IS NULL OR m.project_id=?) AND {chat_filter}),
      ranked AS (SELECT *,lexical+exact score FROM matched WHERE {eligibility})
      SELECT rid,count(*) OVER() total FROM ranked
      ORDER BY score DESC,updated_at DESC,id DESC LIMIT ? OFFSET ?"""
    args = [*candidate_args,*strong_args,*score_args,total,query.strip().casefold(),
            *statuses,store.config['project_id'],*chat_args,limit,offset]
    rows = db.execute(sql,args).fetchall()
    if not rows:
        # Preserve total/offset semantics even for a page beyond the last match.
        count_sql = sql[:sql.index('      SELECT rid,count(*)')] + ' SELECT count(*) FROM ranked'
        return [], db.execute(count_sql,args[:-2]).fetchone()[0]
    ids = [r[0] for r in rows]
    fetched = {r['rowid']:r for r in db.execute(
        f"SELECT rowid,* FROM work_memory WHERE rowid IN ({','.join('?' for _ in ids)})",ids)}
    return [fetched[i] for i in ids], rows[0][1]


ARCHIVE_MARKER = '_sessiongraph_payload_v1'


def payload_text(store, value, *, max_bytes=None):
    """Read raw or losslessly archived JSON; corrupt evidence fails explicitly."""
    if value is None:
        return None
    # Raw JSON payloads never have this reserved top-level envelope.
    if not value.startswith('{"'+ARCHIVE_MARKER+'":'):
        if max_bytes is not None and len(value.encode('utf-8')) > max_bytes:
            raise ValueError('graph_archive_size_limit')
        return value
    reference = json.loads(value)
    if set(reference) != {ARCHIVE_MARKER} or not isinstance(reference[ARCHIVE_MARKER], str):
        raise ValueError('graph_archive_invalid_reference')
    row = store.db.execute('SELECT size,data FROM sg_payload_archive WHERE hash=?',
                           (reference[ARCHIVE_MARKER],)).fetchone()
    if row is None:
        raise ValueError('graph_archive_missing_payload')
    if max_bytes is not None and not 0 <= row['size'] <= max_bytes:
        raise ValueError('graph_archive_size_limit')
    import zlib
    try:
        if max_bytes is None:
            original = zlib.decompress(row['data'])
        else:
            decoder = zlib.decompressobj()
            original = decoder.decompress(row['data'], max_bytes + 1)
            if len(original) > max_bytes or not decoder.eof or decoder.unused_data:
                raise ValueError('graph_archive_size_or_stream_invalid')
    except zlib.error as exc:
        raise ValueError('graph_archive_corrupt_payload') from exc
    if len(original) != row['size'] or hashlib.sha256(original).hexdigest() != reference[ARCHIVE_MARKER]:
        raise ValueError('graph_archive_hash_mismatch')
    return original.decode('utf-8')


def load_payload(store, value):
    return json.loads(payload_text(store, value))


def archive_text(store, value, report=None):
    """Return raw text or a verified lossless reference; caller owns the transaction."""
    import zlib
    data = value.encode('utf-8')
    if len(data) < 1024:
        return value
    packed = zlib.compress(data, 6)
    identity = hashlib.sha256(data).hexdigest()
    reference = json.dumps({ARCHIVE_MARKER: identity}, separators=(',', ':'))
    if len(packed) + len(reference) + 96 >= len(data):
        return value
    store.db.execute('''CREATE TABLE IF NOT EXISTS sg_payload_archive(
        hash TEXT PRIMARY KEY,size INTEGER NOT NULL,data BLOB NOT NULL) WITHOUT ROWID''')
    if zlib.decompress(packed) != data:
        raise ValueError('graph_archive_roundtrip')
    added = store.db.execute('INSERT OR IGNORE INTO sg_payload_archive VALUES(?,?,?)',
                             (identity, len(data), packed)).rowcount
    if payload_text(store, reference) != value:
        raise ValueError('graph_archive_collision')
    if report is not None:
        report['payloads'] += 1
        report['raw_bytes'] += len(data)
        report['blob_bytes_added'] += len(packed) if added else 0
        report['reference_bytes'] += len(reference)
        report['deduplicated'] += not added
    return reference


def archive_completed_payloads(store, job_id=None):
    """Compress completed job payloads in the same database/transaction.

    Content-addressed blobs deduplicate byte-identical JSON. Source facts, input
    descriptors, unfinished jobs, controls and audit receipts are never removed.
    Readers load only a referenced payload, without any model or network call.
    """
    db = store.db
    report = dict(payloads=0,raw_bytes=0,blob_bytes_added=0,reference_bytes=0,deduplicated=0)
    if not all(exists(db, table) for table in ('sg2_jobs','sg2_job_parts','sg2_attempts','sg2_control','sg2_claims')):
        return report
    db.execute('''CREATE TABLE IF NOT EXISTS sg_payload_archive(
        hash TEXT PRIMARY KEY,size INTEGER NOT NULL,data BLOB NOT NULL) WITHOUT ROWID''')
    args = [store.config['project_id']]
    condition = ''
    if job_id is not None:
        condition = ' AND j.id=?';args.append(job_id)
    eligible = """SELECT j.id FROM sg2_jobs j WHERE j.project=? AND j.state='complete'
        AND NOT EXISTS(SELECT 1 FROM sg2_attempts a WHERE a.job=j.id AND a.state='running')
        AND NOT EXISTS(SELECT 1 FROM sg2_control c WHERE c.job=j.id)
        AND NOT EXISTS(SELECT 1 FROM sg2_claims c WHERE c.job=j.id)""" + condition
    report = dict(payloads=0,raw_bytes=0,blob_bytes_added=0,reference_bytes=0,deduplicated=0)
    for table, fields, parent in [('sg2_jobs',('output_json','candidates_json'),'id'),
                                  ('sg2_job_parts',('output_json',),'job')]:
        for row in db.execute(f'SELECT id,{",".join(fields)} FROM {table} WHERE {parent} IN ({eligible})',args).fetchall():
            for field in fields:
                value = row[field]
                if value is None or value.startswith('{"'+ARCHIVE_MARKER+'":'):
                    continue
                reference = archive_text(store, value, report)
                if reference != value:
                    db.execute(f'UPDATE {table} SET {field}=? WHERE id=?',(reference,row['id']))
    return report
