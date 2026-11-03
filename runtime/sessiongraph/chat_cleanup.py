"""One-shot SessionEnd source cleanup. Archive and delete share this opt-in policy.

The hook returns before Codex moves/removes the transcript. A detached worker
checks once after a short delay; ordinary exits and inaccessible roots preserve
the graph. No models, transcript scans, polling service, or source-file writes.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from .core import Store, load_config
from . import storage

PREFIX = 'chat_cleanup:'


def enabled(config):
    hook = config.get('hook', {})
    if hook.get('enabled') is False:
        return False
    if 'cleanup_missing_chats' in hook:
        value = hook['cleanup_missing_chats']
    else:
        path = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'sessiongraph.json'
        try:
            settings = json.loads(path.read_text())
        except FileNotFoundError:
            settings = {}
        if not isinstance(settings, dict):
            raise ValueError('invalid global SessionGraph settings')
        value = settings.get('cleanup_missing_chats', False) if settings.get('enabled') is True else False
    if type(value) is not bool:
        raise ValueError('cleanup_missing_chats must be a boolean')
    return value


def marker(db, chat):
    row = db.execute('SELECT value FROM metadata WHERE key=?', (PREFIX + chat,)).fetchone()
    return json.loads(row[0]) if row else None


def excluded(store, chat):
    prior = marker(store.db, chat)
    if prior is None:
        return False
    for path in prior['paths']:
        try:
            if stat.S_ISREG(Path(path).lstat().st_mode):
                return False
        except FileNotFoundError:
            continue
        except OSError:
            return True
    return True


def check_import(store, chat, path):
    """An explicit restoration at an original path may recreate cleaned content."""
    prior = marker(store.db, chat)
    if prior is None:
        if enabled(store.config):
            registered = [r[0] for r in store.db.execute('''SELECT s.path FROM sessions s
                JOIN session_chats c ON c.segment_id=s.id WHERE c.chat_id=?''', (chat,))]
            if str(path) not in registered:
                for old in registered:
                    try:
                        Path(old).lstat()
                    except FileNotFoundError:
                        raise ValueError('chat source cleanup pending') from None
        return
    if str(path) not in prior['paths']:
        raise ValueError('chat source was cleaned; restore an original transcript path first')
    store.db.execute('DELETE FROM metadata WHERE key=?', (PREFIX + chat,))


def _sources(store, chat, transcript):
    rows = store.db.execute('''SELECT s.id,s.path FROM sessions s
        JOIN session_chats c ON c.segment_id=s.id WHERE c.chat_id=?''', (chat,)).fetchall()
    if not rows or transcript not in {r['path'] for r in rows}:
        return [], 'unindexed_source'
    return rows, None


def _all_missing(config, rows):
    roots = [Path(p).expanduser().resolve() for p in config.get('hook', {}).get('allowed_transcript_roots', [])]
    for row in rows:
        path = Path(row['path'])
        matches = [root for root in roots if path.is_relative_to(root)]
        if not matches or path.suffix != '.jsonl':
            return 'outside_allowlist'
        root = max(matches, key=lambda p: len(p.parts))
        try:
            # ENOENT for a whole unavailable store is not deletion evidence.
            with os.scandir(root):
                pass
            path.lstat()  # A dangling symlink or non-regular source is not absence.
            return 'source_present'
        except FileNotFoundError:
            try:
                with os.scandir(root):
                    pass
            except OSError:
                return 'source_unavailable'
        except OSError:
            return 'source_unavailable'
    return None


@contextmanager
def _ledger_erase(db):
    """Authorized erasure only, inside the same exclusive write transaction.

    Immutable history is not editable by ordinary writers. Transactional DDL
    suspends only its guards during erasure/reanchoring and restores the exact
    guards before commit. Other connections never see an unguarded schema, and
    rollback restores both history and guards if any operation fails.
    """
    names = [f'{table}_{action}' for table in (
        'sg_revision_items', 'sg_revision_sources', 'sg_revision_reviews',
        'sg_revision_review_controls') for action in ('update', 'delete')]
    guards = db.execute('SELECT name,sql FROM sqlite_master WHERE type=\'trigger\' AND name IN ('
                        + ','.join('?' for _ in names) + ')', names).fetchall()
    for row in guards:
        db.execute(f'DROP TRIGGER "{row["name"]}"')
    try:
        yield
    finally:
        for row in guards:
            db.execute(row['sql'])




def _extraction_states(config, chats):
    """Return chat-owned memory-extraction checkpoints and their lock files."""
    chats = set(chats)
    folder = Path(config['database']).parent / 'memory-extraction'
    found = []
    if not folder.is_dir():
        return found
    for chat in sorted(chats):
        for suffix in ('.json', '.lock'):
            path = folder / (chat + suffix)
            if path.is_file() and not path.is_symlink():
                found.append(path)
    return found


def _receipt_folders(config):
    from .auto_update import _folder
    root = _folder(config)
    return (root, root.parent / 'cleared-runs')


def _remove_hot_receipts(config, chats):
    """Delete chat-owned hot receipts, checkpoints and restore flags."""
    chats = set(chats)
    removed, errors = 0, 0
    from .restoration import discard
    restore_removed, restore_errors = discard(config, chats)
    removed += restore_removed
    errors += restore_errors
    for path in _extraction_states(config, chats):
        try:
            path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            errors += 1
    for folder in _receipt_folders(config):
        for path in folder.glob('*.json'):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                run = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(run, dict) and run.get('chat_id') in chats:
                    path.unlink(missing_ok=True)
                    removed += 1
            except FileNotFoundError:
                continue
            except (OSError, ValueError, UnicodeError):
                errors += 1
    return dict(receipt_files=removed, receipt_file_errors=errors)


def _remove_request_audits(config, job_ids):
    """Delete only diagnostics whose JSON names an explicitly deleted job."""
    job_ids = set(job_ids)
    removed, errors = 0, 0
    folder = Path(config['database']).parent / 'request-audit'
    if not folder.is_dir():
        return dict(request_audit_files=0, request_audit_file_errors=0)
    for path in folder.glob('request-*.json'):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            record = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(record, dict) and record.get('job_id') in job_ids:
                path.unlink(missing_ok=True)
                removed += 1
        except FileNotFoundError:
            continue
        except (OSError, ValueError, UnicodeError):
            errors += 1
    return dict(request_audit_files=removed, request_audit_file_errors=errors)


def _purge(store, chats, sources, *, reanchor=True):
    """Purge one or more already-authorized chats in one graph transaction.

    ``sources`` maps each chat ID to its revalidated session rows. Purging all
    chats before a single reanchor prevents evidence from a second deleted chat
    being treated as a surviving support for a shared node.
    """
    db = store.db
    chats = list(chats)
    counts = {}

    def delete(table, condition, args=()):
        if storage.exists(db, table):
            counts[table] = db.execute(f'DELETE FROM {table} WHERE {condition}', args).rowcount

    def selection(name, query, args=()):
        db.execute(f'CREATE TEMP TABLE {name}(id TEXT PRIMARY KEY) WITHOUT ROWID')
        db.execute(f'INSERT OR IGNORE INTO {name} {query}', args)

    selection('cleanup_chats', 'SELECT chat_id FROM session_chats WHERE chat_id IN ('
              + ','.join('?' for _ in chats) + ')', chats)
    selection('cleanup_segments', 'SELECT segment_id FROM session_chats WHERE chat_id IN (SELECT id FROM cleanup_chats)')
    selection('cleanup_entries', 'SELECT id FROM entries WHERE session_id IN (SELECT id FROM cleanup_segments)')
    has_graph_jobs = storage.exists(db, 'sg2_jobs')
    if has_graph_jobs:
        selection('cleanup_graph_jobs', '''SELECT id FROM sg2_jobs
            WHERE chat IN (SELECT id FROM cleanup_chats)
            OR segment IN (SELECT id FROM cleanup_segments)''')
    payload_candidates = set()
    if storage.exists(db, 'sg_payload_archive'):
        def collect(table, column, condition, args=()):
            if storage.exists(db, table):
                for row in db.execute(f'SELECT {column} FROM {table} WHERE {condition}', args):
                    payload_candidates.update(storage.payload_hashes(row[0]))
        jobs = '''SELECT id FROM sg2_jobs WHERE chat IN (SELECT id FROM cleanup_chats)
            OR segment IN (SELECT id FROM cleanup_segments)'''
        if has_graph_jobs:
            collect('sg2_jobs', 'output_json', '''chat IN (SELECT id FROM cleanup_chats)
                OR segment IN (SELECT id FROM cleanup_segments)''')
            collect('sg2_jobs', 'candidates_json', '''chat IN (SELECT id FROM cleanup_chats)
                OR segment IN (SELECT id FROM cleanup_segments)''')
            collect('sg2_job_parts', 'output_json', f'job IN ({jobs})')
            collect('sg2_checkpoints', 'result_json', f'job IN ({jobs})')
            collect('sg2_unresolved', 'payload_json', f'''chat IN (SELECT id FROM cleanup_chats)
                OR job IN ({jobs})''')
            collect('sg2_recoveries', 'before_json', f'job IN ({jobs})')
            collect('sg2_task_dismissals', 'before_json', f'job IN ({jobs})')
            collect('sg2_judge_migrations', 'before_json', f'job IN ({jobs})')
        collect('sg_run_receipts', 'payload', '''project=? AND chat IN (SELECT id FROM cleanup_chats)''',
                (store.config['project_id'],))
        collect('sg_retained_sources', 'payload', 'entry_id IN (SELECT id FROM cleanup_entries)')
    selection('cleanup_memories', '''SELECT id FROM work_memory WHERE
        source_entry_id IN (SELECT id FROM cleanup_entries)
        OR chat_id IN (SELECT id FROM cleanup_chats)''')
    if storage.exists(db, 'sg2_nodes'):
        selection('cleanup_affected', '''SELECT node FROM sg2_evidence WHERE entry_id IN (SELECT id FROM cleanup_entries)
            UNION SELECT id FROM cleanup_memories''')
        db.execute('''DELETE FROM cleanup_memories WHERE EXISTS(SELECT 1 FROM sg2_evidence e
            WHERE e.node=cleanup_memories.id AND e.entry_id NOT IN (SELECT id FROM cleanup_entries))''')
        # A node may be owned by another chat while every piece of its evidence
        # came from the deleted chats. It cannot survive reanchoring, so remove it
        # with the other unsupported nodes instead of failing the whole cleanup.
        counts['unsupported_nodes_removed'] = db.execute('''
            INSERT OR IGNORE INTO cleanup_memories
            SELECT n.id FROM sg2_nodes n JOIN cleanup_affected a ON a.id=n.id
            WHERE NOT EXISTS(SELECT 1 FROM sg2_evidence e WHERE e.node=n.id
                AND e.entry_id NOT IN (SELECT id FROM cleanup_entries))''').rowcount
        if storage.exists(db, 'sg_payload_archive'):
            # Rows about to be removed may carry the only reference to a blob.
            # Include every canonical text column, not only the obvious payload.
            for table, columns in (
                ('sg2_evidence', ('assertion_json', 'quote')),
                ('sg2_relation_evidence', ('quote',)),
            ):
                if storage.exists(db, table):
                    for row in db.execute(f'''SELECT {','.join(columns)} FROM {table}
                        WHERE entry_id IN (SELECT id FROM cleanup_entries)''').fetchall():
                        for value in row:
                            payload_candidates.update(storage.payload_hashes(value))
            for row in db.execute('''SELECT content,source_quote FROM work_memory
                WHERE source_entry_id IN (SELECT id FROM cleanup_entries)''').fetchall():
                for value in row:
                    payload_candidates.update(storage.payload_hashes(value))
            for row in db.execute('''SELECT source_quote,source_mention,target_mention FROM work_memory_relations
                WHERE source_entry_id IN (SELECT id FROM cleanup_entries)''').fetchall():
                for value in row:
                    payload_candidates.update(storage.payload_hashes(value))
        from .graph_compaction import available
        if available(store):
            db.execute('''DELETE FROM sg_graph_redirects WHERE alias IN (SELECT id FROM cleanup_affected)
                OR canonical IN (SELECT id FROM cleanup_affected)
                OR canonical IN (SELECT canonical FROM sg_graph_redirects WHERE alias IN (SELECT id FROM cleanup_affected))''')
        selection('cleanup_evidence', 'SELECT id FROM sg2_evidence WHERE entry_id IN (SELECT id FROM cleanup_entries)')
        selection('cleanup_relations', '''SELECT relation FROM sg2_relation_evidence WHERE entry_id IN (SELECT id FROM cleanup_entries)
            UNION SELECT id FROM sg2_relations WHERE source IN (SELECT id FROM cleanup_memories)
            OR target IN (SELECT id FROM cleanup_memories)''')
        if storage.exists(db, 'sg_revision_items'):
            selection('cleanup_revisions', '''SELECT r.id FROM sg_revision_items r WHERE
                r.node IN (SELECT id FROM cleanup_memories) OR
                (r.payload_evidence IN (SELECT id FROM cleanup_evidence) AND NOT EXISTS(
                    SELECT 1 FROM sg_revision_sources s WHERE s.revision=r.id
                    AND s.evidence NOT IN (SELECT id FROM cleanup_evidence)))''')
            selection('cleanup_reviews', '''SELECT id FROM sg_revision_reviews WHERE
                source IN (SELECT id FROM cleanup_revisions) OR target IN (SELECT id FROM cleanup_revisions)
                OR evidence IN (SELECT id FROM cleanup_evidence)''')
            db.execute('''INSERT OR IGNORE INTO cleanup_affected SELECT r.node FROM sg_revision_items r
                JOIN sg_revision_reviews v ON v.target=r.id WHERE v.id IN (SELECT id FROM cleanup_reviews)''')
            with _ledger_erase(db):
                delete('sg_revision_review_controls', '''review IN (SELECT id FROM cleanup_reviews)
                    OR replacement_review IN (SELECT id FROM cleanup_reviews)
                    OR source IN (SELECT id FROM cleanup_revisions) OR evidence IN (SELECT id FROM cleanup_evidence)''')
                delete('sg_revision_reviews', 'id IN (SELECT id FROM cleanup_reviews)')
                delete('sg_revision_sources', 'revision IN (SELECT id FROM cleanup_revisions) OR evidence IN (SELECT id FROM cleanup_evidence)')
                db.execute('''UPDATE sg_revision_items SET payload_evidence=(SELECT s.evidence FROM sg_revision_sources s
                    WHERE s.revision=sg_revision_items.id ORDER BY s.evidence LIMIT 1)
                    WHERE payload_evidence IN (SELECT id FROM cleanup_evidence) AND id NOT IN (SELECT id FROM cleanup_revisions)''')
                delete('sg_revision_items', 'id IN (SELECT id FROM cleanup_revisions)')
            delete('sg_revision_projection', 'node IN (SELECT id FROM cleanup_affected)')
        delete('sg2_evidence', 'id IN (SELECT id FROM cleanup_evidence)')
        delete('sg2_relation_evidence', '''entry_id IN (SELECT id FROM cleanup_entries) OR relation IN (
            SELECT id FROM sg2_relations WHERE source IN (SELECT id FROM cleanup_memories) OR target IN (SELECT id FROM cleanup_memories))''')
        # A relation without a surviving endpoint cannot remain even if one of
        # its own evidence rows belongs to another chat. Dangling endpoint rows
        # would otherwise fail deferred foreign-key validation at commit.
        delete('sg2_relations', '''id IN (SELECT id FROM cleanup_relations) AND (
            source IN (SELECT id FROM cleanup_memories)
            OR target IN (SELECT id FROM cleanup_memories)
            OR NOT EXISTS(SELECT 1 FROM sg2_relation_evidence e
                WHERE e.relation=sg2_relations.id))''')
        for table in ('sg2_terms', 'sg2_keys'):
            delete(table, 'node IN (SELECT id FROM cleanup_affected)')
        for table in ('sg2_attempts', 'sg2_checkpoints', 'sg2_control', 'sg2_claims',
                      'sg2_job_parts', 'sg2_task_dismissals', 'sg2_judge_migrations'):
            delete(table, 'job IN (SELECT id FROM cleanup_graph_jobs)')
        delete('sg2_claims', 'entry_id IN (SELECT id FROM cleanup_entries)')
        delete('sg2_pending_terms', '''item IN (SELECT id FROM sg2_unresolved
            WHERE chat IN (SELECT id FROM cleanup_chats) OR job IN (SELECT id FROM cleanup_graph_jobs))''')
        delete('sg2_unresolved', '''chat IN (SELECT id FROM cleanup_chats)
            OR job IN (SELECT id FROM cleanup_graph_jobs)''')
        delete('sg2_stage_lease', 'job IN (SELECT id FROM cleanup_graph_jobs)')
        delete('sg_legacy_migrations', '''node IN (SELECT id FROM cleanup_memories)
            OR job IN (SELECT id FROM cleanup_graph_jobs) OR evidence IN (SELECT id FROM cleanup_evidence)''')
        delete('sg2_jobs', 'id IN (SELECT id FROM cleanup_graph_jobs)')
        delete('sg2_cursors', 'segment IN (SELECT id FROM cleanup_segments)')
        if reanchor:
            _reanchor(store)
        delete('sg2_nodes', 'id IN (SELECT id FROM cleanup_memories)')

    # Receipts belong to the deleted chats, not to their graph nodes.
    delete('sg_run_receipts', 'project=? AND chat IN (SELECT id FROM cleanup_chats)',
           (store.config['project_id'],))
    delete('work_memory_relations', '''source IN (SELECT id FROM cleanup_memories) OR target IN (SELECT id FROM cleanup_memories)
        OR source_entry_id IN (SELECT id FROM cleanup_entries)''')
    db.execute('UPDATE work_memory SET supersedes_id=NULL WHERE supersedes_id IN (SELECT id FROM cleanup_memories)')
    delete('work_memory', 'id IN (SELECT id FROM cleanup_memories)')
    selection('cleanup_legacy_jobs', 'SELECT id FROM jobs WHERE session_id IN (SELECT id FROM cleanup_segments)')
    selection('cleanup_records', '''SELECT id FROM records WHERE job_id IN (SELECT id FROM cleanup_legacy_jobs)
        UNION SELECT r.id FROM records r JOIN evidence e ON e.owner_id=r.id
        WHERE e.entry_id IN (SELECT id FROM cleanup_entries) AND NOT EXISTS(
            SELECT 1 FROM evidence v WHERE v.owner_id=r.id AND v.entry_id NOT IN (SELECT id FROM cleanup_entries))''')
    selection('cleanup_edges', '''SELECT id FROM edges WHERE job_id IN (SELECT id FROM cleanup_legacy_jobs)
        OR source IN (SELECT id FROM cleanup_records) OR target IN (SELECT id FROM cleanup_records)''')
    delete('evidence', '''entry_id IN (SELECT id FROM cleanup_entries) OR owner_id IN (SELECT id FROM cleanup_records)
        OR owner_id IN (SELECT id FROM cleanup_edges)''')
    delete('docs', '''id IN (SELECT id FROM cleanup_entries) OR id IN (SELECT id FROM cleanup_records)
        OR id IN (SELECT id FROM cleanup_edges)''')
    delete('edges', 'id IN (SELECT id FROM cleanup_edges)')
    delete('records', 'id IN (SELECT id FROM cleanup_records)')
    delete('reviewed_batches', 'job_id IN (SELECT id FROM cleanup_legacy_jobs)')
    delete('jobs', 'id IN (SELECT id FROM cleanup_legacy_jobs)')
    for table in ('sg_source_locations', 'sg_retained_sources', 'sg_input_coverage'):
        delete(table, 'entry_id IN (SELECT id FROM cleanup_entries)')
    for table in ('sg_compact_events', 'sg_work_units', 'sg_input_schedule'):
        delete(table, 'segment IN (SELECT id FROM cleanup_segments)')
    delete('raw_events', 'session_id IN (SELECT id FROM cleanup_segments)')
    delete('sg_raw_archive', 'session_id IN (SELECT id FROM cleanup_segments)')
    delete('entries', 'id IN (SELECT id FROM cleanup_entries)')
    delete('metadata', "key IN (SELECT 'ingest_scope:' || id FROM cleanup_segments)")
    delete('session_chats', 'segment_id IN (SELECT id FROM cleanup_segments)')
    delete('sessions', 'id IN (SELECT id FROM cleanup_segments)')
    # Old, already-running importers do not know the Python marker guard. Keep
    # their whole import transaction from resurrecting an archived transcript.
    db.execute('''CREATE TRIGGER IF NOT EXISTS sg_cleanup_reimport
        BEFORE INSERT ON session_chats WHEN EXISTS(
            SELECT 1 FROM metadata WHERE key='chat_cleanup:' || new.chat_id)
        AND NOT EXISTS(SELECT 1 FROM metadata m,json_each(m.value,'$.paths') p
            JOIN sessions s ON s.id=new.segment_id AND s.path=p.value
            WHERE m.key='chat_cleanup:' || new.chat_id)
        BEGIN SELECT RAISE(ABORT,'chat_source_cleaned'); END''')
    for chat in chats:
        db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', (PREFIX + chat, json.dumps(
            {'paths': [r['path'] for r in sources[chat]], 'cleaned_at': time.time()},
            separators=(',', ':'))))
    # Existing graph truths may retain an envelope inside a quote or assertion.
    # They are read-only survivors, not archive owners, but must pin the blob.
    if payload_candidates and storage.exists(db, 'sg_payload_archive'):
        # Only blocks referenced by this deletion are candidates. Any surviving
        # canonical row removes a hash from the result, preserving shared content.
        survivors = storage.payloads_still_referenced(db, payload_candidates)
        counts['sg_payload_archive'] = storage.delete_payloads(
            db, sorted(payload_candidates - survivors))
    jobs = [r[0] for r in db.execute('SELECT id FROM temp.cleanup_graph_jobs')] if has_graph_jobs else []
    return counts, jobs


def cleanup_many(config_path, chats):
    """Atomically clean several already-confirmed deletions.

    Each source is revalidated under the same locks. Purging all chats before a
    single reanchor prevents a node shared by two deleted chats from being
    mistaken for a survivor merely because the second chat was not purged yet.
    """
    config_path = Path(config_path).resolve()
    config = load_config(config_path)
    chats = list(dict.fromkeys(chats))
    result = {'cleaned': False, 'chat_ids': chats, 'model_calls': 0}
    if not chats:
        return {**result, 'reason': 'no_chats'}
    if not enabled(config) or (config_path.parent / 'disabled').exists():
        return {**result, 'reason': 'disabled'}
    if any(not isinstance(chat, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', chat)
           for chat in chats):
        return {**result, 'reason': 'invalid_source'}
    if not Path(config['database']).is_file():
        return {**result, 'reason': 'unindexed_source'}
    checked = {}
    reader = Store(config, readonly=True)
    try:
        for chat in chats:
            transcript = reader.db.execute('''SELECT s.path FROM sessions s
                JOIN session_chats c ON c.segment_id=s.id WHERE c.chat_id=?
                ORDER BY s.id LIMIT 1''', (chat,)).fetchone()
            if transcript is None:
                return {**result, 'reason': 'unindexed_source'}
            rows, reason = _sources(reader, chat, transcript[0])
            reason = reason or _all_missing(config, rows)
            if reason:
                return {**result, 'reason': reason}
            checked[chat] = transcript[0]
    finally:
        reader.close()
    from .stage_leases import locked
    folder = Path(config['database']).parent
    with locked(folder / 'stage-activity.lock'), locked(folder / 'hook-state/update.lock'), \
            locked(folder / 'graph-v2.lock'):
        store = Store(config)
        try:
            with store.db:
                store.db.execute('BEGIN IMMEDIATE')
                store.db.execute('PRAGMA defer_foreign_keys=ON')
                verified = {}
                for chat in chats:
                    rows, reason = _sources(store, chat, checked[chat])
                    reason = reason or _all_missing(config, rows)
                    if reason:
                        return {**result, 'reason': reason}
                    verified[chat] = rows
                counts, jobs = _purge(store, chats, verified)
            result.update(cleaned=True, reason='source_missing', deleted=counts)
            removed = {'receipt_files': 0, 'receipt_file_errors': 0,
                       'request_audit_files': 0, 'request_audit_file_errors': 0}
            for chat in chats:
                for key, value in _remove_hot_receipts(config, [chat]).items():
                    removed[key] += value
            for key, value in _remove_request_audits(config, jobs).items():
                removed[key] += value
            result.update(removed)
            warnings = {}
            for chat in chats:
                try:
                    _dismiss(config, chat)
                except (OSError, ValueError) as exc:
                    warnings[chat] = type(exc).__name__
            if warnings:
                result['queue_warnings'] = warnings
            return result
        finally:
            store.close()


def _reanchor(store):
    """Rebuild shared display/index data exclusively from surviving evidence."""
    from . import graph_v2 as graph, revision_ledger as ledger
    from .source_retention import capture_publication
    db = store.db
    for item in db.execute('''SELECT n.id FROM sg2_nodes n JOIN cleanup_affected a ON a.id=n.id
        WHERE n.id NOT IN (SELECT id FROM cleanup_memories)''').fetchall():
        nid = item['id']
        evidence = db.execute('SELECT * FROM sg2_evidence WHERE node=? ORDER BY id', (nid,)).fetchall()
        if not evidence:
            raise ValueError('cleanup_shared_node_without_evidence')
        first = evidence[0]
        capture_publication(store, [first['entry_id']])
        payload = json.loads(first['assertion_json'])
        aliases, identities = set(), {}
        declared_paths, cited_quotes = [], []
        for ev in evidence:
            value = json.loads(ev['assertion_json'])
            aliases.update(value['aliases'])
            aliases.add(value['title'])
            identities.update({graph.dumps(i): i for i in value['identity']})
            declared_paths.extend(value.get('paths') or [])
            cited_quotes.extend(
                item.get('quote') for item in value.get('evidence', [])
                if isinstance(item, dict) and isinstance(item.get('quote'), str) and item.get('quote')
            )
            cited_quotes.append(ev['quote'])
            graph._index(store, nid, value)
            db.executemany('INSERT OR IGNORE INTO sg2_keys VALUES(?,?,?)',
                           [(store.config['project_id'], key, nid) for key in graph._identity_keys(value)])
        # A node may survive in several chats. Rebuild paths from every
        # surviving evidence row, not only the representative payload: paths
        # unique to the second chat must remain navigable after the first is
        # deleted. Literal checking stays inside evidence_paths.for_node.
        from .evidence_paths import for_node as evidence_paths
        path_payload = {
            **payload,
            'paths': list(dict.fromkeys(declared_paths)),
            'evidence': [{'quote': quote} for quote in dict.fromkeys(cited_quotes)],
        }
        db.execute('''UPDATE work_memory SET title=?,content=?,status=?,source_entry_id=?,source_quote=?,chat_id=?,
            aliases_json=?,paths_json=? WHERE id=?''', (payload['title'], payload['content'], payload['status'],
            first['entry_id'], first['quote'], first['chat'], graph.dumps(sorted(aliases)),
            graph.dumps(evidence_paths(path_payload)), nid))
        db.execute('UPDATE sg2_nodes SET identity_json=? WHERE id=?', (graph.dumps(list(identities.values())), nid))
        if storage.exists(db, 'sg_revision_projection'):
            ledger.refresh(store, nid)


def cleanup(config_path, chat, transcript):
    """Check registered sources only; unknown/error/present means no deletion."""
    config_path = Path(config_path).resolve()
    config = load_config(config_path)
    result = {'cleaned': False, 'chat_id': chat, 'model_calls': 0}
    if not enabled(config) or (config_path.parent / 'disabled').exists():
        return {**result, 'reason': 'disabled'}
    if (not isinstance(chat, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', chat)
            or not isinstance(transcript, str) or not Path(transcript).is_absolute()):
        return {**result, 'reason': 'invalid_source'}
    if not Path(config['database']).is_file():
        return {**result, 'reason': 'unindexed_source'}
    # Ordinary exits need only this read-only point lookup/stat, not writer locks.
    reader = Store(config, readonly=True)
    try:
        rows, reason = _sources(reader, chat, transcript)
        reason = reason or _all_missing(config, rows)
        if reason:
            return {**result, 'reason': reason}
    finally:
        reader.close()
    from .stage_leases import locked
    folder = Path(config['database']).parent
    # Match coordinator ordering; sleeping kernel locks do not consume CPU.
    with locked(folder / 'stage-activity.lock'), locked(folder / 'hook-state/update.lock'), \
            locked(folder / 'graph-v2.lock'):
        store = Store(config)
        try:
            with store.db:
                store.db.execute('BEGIN IMMEDIATE')
                store.db.execute('PRAGMA defer_foreign_keys=ON')
                rows, reason = _sources(store, chat, transcript)
                reason = reason or _all_missing(config, rows)
                if reason:
                    return {**result, 'reason': reason}
                counts, job_ids = _purge(store, [chat], {chat: rows})
                # SQLite checks deferred constraints at commit; full-store scans
                # belong in tests/maintenance, not every lifecycle notification.
            result.update(cleaned=True, reason='source_missing', deleted=counts)
            # Files are removed only after a committed fence. A racing writer can
            # no longer expose them because readers honor the chat_cleanup marker.
            result.update(_remove_hot_receipts(config, [chat]))
            result.update(_remove_request_audits(config, job_ids))
            try:
                _dismiss(config, chat)
            except (OSError, ValueError) as exc:
                # The committed marker still fences a late import/model worker.
                result['queue_warning'] = type(exc).__name__
            return result
        finally:
            store.close()


def _dismiss(config, chat):
    from . import continuation as c
    folder = c.root(config)
    with c.locked(folder):
        path = folder / 'queue.json'
        queue = c.read(path, {})
        key = c.key(config['_config_path'], chat)
        if key in queue:
            queue[key] = {'config': config['_config_path'], 'chat': chat, 'target': None,
                'run_id': '', 'state': 'dismissed', 'dismissal_reason': 'source_cleanup',
                'dismissed_at': time.time(), 'updated_at': time.time(), 'due': 0}
            c.write(path, queue)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--chat', required=True)
    parser.add_argument('--transcript', required=True)
    args = parser.parse_args()
    time.sleep(2)
    try:
        result = cleanup(args.config, args.chat, args.transcript)
    except (OSError, ValueError, sqlite3.Error) as exc:
        from .failures import describe, safe_detail
        info = describe(exc)
        result = {'cleaned': False, 'reason': safe_detail(info.get('detail')) or info['code'],
                  'error': info, 'model_calls': 0}
    print(json.dumps(result))


def _deleted_chats(db):
    prefix = PREFIX
    return {row[0][len(prefix):] for row in db.execute(
        'SELECT key FROM metadata WHERE substr(key,1,?)=?', (len(prefix), prefix))}


def _orphan_judge_migrations(db, chats):
    """Return migration rows for deleted chats whose parent job is gone.

    The ledger has no foreign key because it is a one-time backend history. A
    missing parent alone is not deletion evidence for an arbitrary chat, so the
    segment must still name one of this project's committed cleanup markers.
    """
    chats = set(chats)
    if (not chats or not storage.exists(db, 'sg2_judge_migrations')
            or not storage.exists(db, 'sg2_jobs')):
        return []
    orphans = []
    for row in db.execute('''SELECT job,before_json FROM sg2_judge_migrations m
            WHERE NOT EXISTS(SELECT 1 FROM sg2_jobs j WHERE j.id=m.job)'''):
        try:
            before = json.loads(row[1])
        except (TypeError, ValueError):
            continue
        segment = before.get('segment') if isinstance(before, dict) else None
        if (isinstance(segment, str)
                and any(segment == chat or segment.startswith(chat + '::')
                        for chat in chats)):
            orphans.append(row[0])
    return orphans


def _hot_receipt_count(config, chats):
    from .restoration import count as restore_count
    count = len(_extraction_states(config, chats)) + restore_count(config, chats)
    errors = 0
    for folder in _receipt_folders(config):
        for path in folder.glob('*.json'):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                run = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(run, dict) and run.get('chat_id') in chats:
                    count += 1
            except FileNotFoundError:
                continue
            except (OSError, ValueError, UnicodeError):
                errors += 1
    return count, errors


def _orphan_audit_files(config, known_jobs, apply=False):
    """Project-local diagnostics with no parent job; malformed files stay."""
    folder = Path(config['database']).parent / 'request-audit'
    found = removed = errors = 0
    if not folder.is_dir():
        return found, removed, errors
    for path in folder.glob('request-*.json'):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            record = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(record, dict):
                continue
            job_id = record.get('job_id')
            project = record.get('project_id')
            if (not isinstance(job_id, str) or not job_id or job_id in known_jobs
                    or project not in (None, config['project_id'])):
                continue
            # A same-named job in another project is not proof of an orphan.
            found += 1
            if apply:
                path.unlink(missing_ok=True)
                removed += 1
        except FileNotFoundError:
            continue
        except (OSError, ValueError, UnicodeError):
            errors += 1
    return found, removed, errors


def cleanup_orphan_content(store, *, apply=False):
    """Explicit project-wide lifecycle repair; never scans transcript text.

    Deletes only (1) receipts whose chat has a committed cleanup marker,
    (2) their hot receipt duplicates, (3) diagnostics whose parent job is
    definitely absent, (4) judge-migration history owned by a deleted chat
    whose parent job is gone, and (5) content-addressed blobs with no canonical
    reference after those deletions. Shared and live rows are retained.
    """
    config = store.config
    project = config['project_id']
    if apply:
        store._ensure_writable()
        from .stage_leases import locked
        folder = Path(config['database']).parent
        lock_paths = (folder / 'stage-activity.lock', folder / 'hook-state/update.lock',
                      folder / 'graph-v2.lock')
        try:
            with locked(lock_paths[0], nonblocking=True), locked(lock_paths[1], nonblocking=True), \
                    locked(lock_paths[2], nonblocking=True):
                return _cleanup_orphan_content_locked(store, apply=True)
        except BlockingIOError:
            raise ValueError('maintenance_busy') from None
    return _cleanup_orphan_content_locked(store, apply=False)


def _cleanup_orphan_content_locked(store, *, apply):
    db = store.db
    config = store.config
    project = config['project_id']
    owner = db.execute("SELECT value FROM metadata WHERE key='project_id'").fetchone()
    if not owner or owner[0] != project:
        raise ValueError('database project_id does not match configuration')
    chats = _deleted_chats(db)
    receipts_table = storage.exists(db, 'sg_run_receipts')
    receipts = db.execute('SELECT count(*) FROM sg_run_receipts WHERE project=? AND chat IN ('
                          + ','.join('?' for _ in chats) + ')', (project, *chats)).fetchone()[0] \
        if receipts_table and chats else 0
    hot, hot_errors = _hot_receipt_count(config, chats)
    migration_rows = _orphan_judge_migrations(db, chats)
    known_jobs = {row[0] for row in db.execute('SELECT id FROM sg2_jobs')} if storage.exists(db, 'sg2_jobs') else set()
    audits, _, audit_errors = _orphan_audit_files(config, known_jobs, apply=False)
    candidates = storage.unreferenced_payloads(
        db, exclude_receipt_chats=tuple(chats) if receipts_table else ())
    payload_bytes = sum(packed for packed, _ in candidates.values())
    payload_original = sum(original for _, original in candidates.values())
    result = dict(applied=apply, model_calls=0, deleted_chats=len(chats),
                  stale_receipts=receipts, stale_hot_receipts=hot,
                  stale_judge_migrations=len(migration_rows),
                  orphan_request_audits=audits, orphan_payloads=len(candidates),
                  orphan_payload_packed_bytes=payload_bytes,
                  orphan_payload_original_bytes=payload_original,
                  hot_receipt_errors=hot_errors, request_audit_errors=audit_errors)
    if not apply:
        return result
    receipts = db.execute('SELECT count(*) FROM sg_run_receipts WHERE project=? AND chat IN ('
                          + ','.join('?' for _ in chats) + ')', (project, *chats)).fetchone()[0] \
        if receipts_table and chats else 0
    result['stale_receipts'] = receipts
    with db:
        db.execute('BEGIN IMMEDIATE')
        migration_rows = _orphan_judge_migrations(db, chats)
        result['stale_judge_migrations'] = len(migration_rows)
        migration_jobs = sorted(set(migration_rows))
        if migration_jobs:
            result['judge_migrations_deleted'] = db.execute(
                'DELETE FROM sg2_judge_migrations WHERE job IN ('
                + ','.join('?' for _ in migration_jobs) + ')', migration_jobs).rowcount
        if receipts_table and chats:
            db.execute('DELETE FROM sg_run_receipts WHERE project=? AND chat IN ('
                       + ','.join('?' for _ in chats) + ')', (project, *chats))
        # Re-scan references after receipt and migration removal in the same transaction.
        candidates = storage.unreferenced_payloads(db)
        result['orphan_payloads'] = len(candidates)
        result['orphan_payload_packed_bytes'] = sum(packed for packed, _ in candidates.values())
        result['orphan_payload_original_bytes'] = sum(original for _, original in candidates.values())
        result['payload_rows_deleted'] = storage.delete_payloads(db, candidates)
    hot_report = _remove_hot_receipts(config, chats)
    known_jobs = {row[0] for row in db.execute('SELECT id FROM sg2_jobs')} if storage.exists(db, 'sg2_jobs') else set()
    _, audit_removed, audit_errors = _orphan_audit_files(config, known_jobs, apply=True)
    result.update(hot_report)
    result.update(request_audit_removed=audit_removed, request_audit_errors=audit_errors)
    return result


if __name__ == '__main__':
    main()
