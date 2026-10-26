"""Read-only V2 counters. Completed input and semantic input are different units."""
from . import input_pipeline
from .storage import exists


def coverage(store):
    db, project = store.db, store.config['project_id']
    raw = db.execute('SELECT count(*) FROM entries').fetchone()[0]
    counts = dict(completed_entries=0, semantic_entries=0, excluded_entries=0,
                  partial_entries=0, covered_entries=0)
    if exists(db, 'sg_input_coverage') and exists(db, 'sg_source_locations'):
        # Only committed receipts of this project/current input revision/hash count.
        # Merge overlaps before testing full coverage; gaps and partial entries are
        # never promoted to completed by merely seeing a receipt or a high cursor.
        indexed = exists(db, 'sg_entry_sizes')
        size = 'z.chars' if indexed else 'length(e.text)'
        sizes_join = 'JOIN sg_entry_sizes z ON z.entry_id=t.entry_id' if indexed else 'JOIN entries e ON e.id=t.entry_id'
        rows = db.execute(f'''
          WITH spans AS (
            SELECT c.entry_id,c.start,c.end,c.disposition,
              max(c.end) OVER (PARTITION BY c.entry_id ORDER BY c.start,c.end
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) prior_end
            FROM sg_input_coverage c JOIN sg2_jobs j ON j.id=c.job
            JOIN sg_source_locations l ON l.entry_id=c.entry_id AND l.text_hash=c.source_hash
            WHERE c.revision=? AND j.project=? AND j.state='complete'
          ), totals AS (
            SELECT entry_id,min(start) first_start,max(end) last_end,
              sum(max(0,end-max(start,coalesce(prior_end,start)))) covered,
              max(disposition!='excluded') semantic FROM spans GROUP BY entry_id
          ) SELECT {size} size,t.* FROM totals t {sizes_join}
        ''', (input_pipeline.VERSION, project))
        for r in rows:
            counts['covered_entries'] += 1
            full = r['first_start'] == 0 and r['last_end'] == r['size'] and r['covered'] == r['size']
            counts['completed_entries' if full else 'partial_entries'] += 1
            if full:
                counts['semantic_entries' if r['semantic'] else 'excluded_entries'] += 1
    nodes = db.execute('SELECT count(*) FROM sg2_nodes WHERE project=?', (project,)).fetchone()[0]
    edges = db.execute('SELECT count(*) FROM sg2_relations WHERE project=?', (project,)).fetchone()[0]
    return dict(engine='graph_v2', input_revision=input_pipeline.VERSION, raw_entries=raw,
                records=nodes, edges=edges, **counts,
                pending_entries=max(0, raw-counts['completed_entries']),
                ratio=round(counts['completed_entries']/raw, 6) if raw else 0.0)


def status(store, detailed=True):
    from .graph_v2 import diagnostics
    from .memory import memory_status
    c = coverage(store)
    g = diagnostics(store)
    chats = store.db.execute('SELECT count(DISTINCT chat_id) FROM session_chats').fetchone()[0]
    jobs = dict(g['jobs'])
    # Keep exact V2 states as well as the scheduler's separate progress buckets.
    result = dict(project_id=store.config['project_id'], engine='graph_v2',
        policy_version=store.config['policy']['version'], policy_hash=store.config['policy_hash'],
        sessions=chats, chats=chats,
        source_segments=store.db.execute('SELECT count(*) FROM sessions').fetchone()[0],
        raw_entries=c['raw_entries'], semantic_entries=c['semantic_entries'],
        normal_semantic_entries=c['semantic_entries'], semantic_entries_total=c['semantic_entries'],
        reviewed_source_entries=0, reviewed_excerpts=0,
        records=c['records'], edges=c['edges'], jobs=jobs, progress=g['progress'],
        coverage=c, work_memory=memory_status(store), model_calls=0,
        graph_v2=g, source_listing_limit=100,
        sources=[dict(r) for r in store.db.execute('''SELECT s.id,c.chat_id,s.lines,s.offset
          FROM sessions s JOIN session_chats c ON c.segment_id=s.id ORDER BY s.id LIMIT 100''')] if detailed else [],
        failed_jobs=g['blocked_jobs'] if detailed else [], failed_job_listing_limit=5)
    return result
