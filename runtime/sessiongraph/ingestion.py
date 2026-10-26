"""Streaming JSONL import. Checkpoints and their records commit together.

There is no file-size ceiling. Memory is bounded by one record (64 MiB),
not by the source or its indexed prefix. Oversized records are explicit errors:
no truncation, skipping, or advancing the checkpoint past unread evidence.
"""
import hashlib
import json
import os
from pathlib import Path

READ_CHUNK_BYTES = 1024 * 1024
MAX_RECORD_BYTES = 64 * 1024 * 1024
BATCH_BYTES = 4 * 1024 * 1024
BATCH_RECORDS = 256
SOURCE_CHANGED = 'source changed or was truncated; rebuild into a fresh index'


def _prefix(stream, offset):
    stream.seek(0)
    hasher = hashlib.sha256()
    remaining = offset
    while remaining:
        chunk = stream.read(min(READ_CHUNK_BYTES, remaining))
        if not chunk:
            raise ValueError(SOURCE_CHANGED)
        hasher.update(chunk)
        remaining -= len(chunk)
    return hasher


def _record(stream, end, line):
    # Snapshot the file length so an active writer cannot make this run endless.
    remaining = end - stream.tell()
    if remaining <= 0:
        return b'', False
    raw = stream.readline(min(MAX_RECORD_BYTES + 1, remaining))
    if not raw:
        raise ValueError(SOURCE_CHANGED)
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError(f'source record exceeds import memory limit at line {line}')
    if not raw.endswith(b'\n'):
        return b'', bool(raw)  # leave incomplete tail for a later refresh
    return raw, False


def ingest(store, path):
    from .core import digest, normalize, tokens
    from .project_binding import inside_project, verify_moved_source

    store._ensure_writable()
    from . import source_locator
    source_locator.setup(store)
    path = Path(path).expanduser().resolve()
    added = batches = 0
    expected = None
    with path.open('rb') as stream:
        end = os.fstat(stream.fileno()).st_size
        header, tail = _record(stream, end, 1)
        if not header:
            return {'added': 0, 'incomplete_tail': tail}
        try:
            meta = json.loads(header)
            if not isinstance(meta, dict):
                raise ValueError('invalid session metadata: expected object')
            if meta.get('type') != 'session_meta':
                raise ValueError('first event must be session_meta')
            chat = meta['payload']['id']
            if not isinstance(chat, str) or not chat or len(chat) > 256:
                raise ValueError('invalid session id')
            cwd = Path(meta['payload']['cwd']).resolve()
            header_in_project = inside_project(store.config, str(cwd))
            if not header_in_project:
                prior_source = store.db.execute('SELECT id FROM sessions WHERE path=?', (str(path),)).fetchone()
                bound = False
                if prior_source and store.chat_id(prior_source['id']) == chat:
                    binding = store.db.execute('SELECT value FROM metadata WHERE key=?',
                                               ('ingest_scope:' + prior_source['id'],)).fetchone()
                    bound = bool(binding and json.loads(binding[0]).get('admitted'))
                verify_moved_source(store.config, chat, path, previously_bound=bound)
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError('invalid session metadata') from exc

        while True:
            batch_added = byte_count = count = 0
            with store.db:
                store.db.execute('BEGIN IMMEDIATE')
                prior = store.db.execute('SELECT * FROM sessions WHERE path=?', (str(path),)).fetchone()
                sid = chat
                if prior:
                    sid = prior['id']
                    if store.chat_id(sid) != chat:
                        raise ValueError('source path registered with a different chat id')
                elif store.db.execute('SELECT 1 FROM sessions WHERE id=?', (sid,)).fetchone():
                    sid = chat + '::' + digest(str(path))[:24]
                offset, line = (prior['offset'], prior['lines']) if prior else (0, 0)
                scope_key = 'ingest_scope:' + sid
                saved_scope = store.db.execute('SELECT value FROM metadata WHERE key=?', (scope_key,)).fetchone()
                scope = json.loads(saved_scope[0]) if saved_scope else dict(
                    active=header_in_project, admitted=header_in_project, excluded_records=0,
                    origin_cwd=str(cwd), first_project_line=1 if header_in_project else None)
                # Another importer may have advanced this source between batches.
                # Rebase on its committed checkpoint rather than inserting duplicates.
                state = (sid, offset, line, prior['prefix_hash'] if prior else digest(b''))
                if state != expected:
                    if offset > end:
                        # A concurrent importer may have seen later appends. Never
                        # regress it; validate its prefix against the current file.
                        end = os.fstat(stream.fileno()).st_size
                    hasher = _prefix(stream, offset)
                    if prior and hasher.hexdigest() != prior['prefix_hash']:
                        raise ValueError(SOURCE_CHANGED)
                else:
                    stream.seek(offset)
                if not prior:
                    store.db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?)',
                                     (sid, str(path), str(cwd), 0, 0, digest(b'')))
                    store.db.execute('INSERT INTO session_chats VALUES (?,?)', (sid, chat))
                while count < BATCH_RECORDS and byte_count < BATCH_BYTES:
                    raw, tail = _record(stream, end, line + 1)
                    if not raw:
                        break
                    line += 1
                    # Keep legacy entry IDs: the newline is excluded from the hash.
                    record = raw[:-1].removesuffix(b'\r')
                    try:
                        event = json.loads(record)
                        if not isinstance(event, dict):
                            raise ValueError('event must be an object')
                        if event.get('type') == 'turn_context':
                            context = event.get('payload')
                            if not isinstance(context, dict) or not isinstance(context.get('cwd'), str):
                                raise ValueError('turn context missing cwd')
                            scope['active'] = inside_project(store.config, context['cwd'])
                            if scope['active']:
                                scope['admitted'] = True
                                if scope['first_project_line'] is None:
                                    scope['first_project_line'] = line
                        if not scope['active']:
                            scope['excluded_records'] += 1
                        # Original JSON remains in the Codex transcript. Entries below
                        # retain searchable text and verified byte locators, not an event copy.
                        if scope['active'] and event.get('type') == 'compacted':
                            store.db.execute('INSERT OR IGNORE INTO sg_compact_events VALUES(?,?,?)', (sid,line,digest(record)))
                        if scope['active'] and event.get('type') == 'response_item':
                            payload = event.get('payload')
                            if not isinstance(payload, dict):
                                raise ValueError('response_item payload must be an object')
                            normalized = normalize(payload)
                            if normalized and normalized[2]:
                                kind, role, text, call_id = normalized
                                if call_id is not None and not isinstance(call_id, str):
                                    raise ValueError('invalid call_id')
                                eid = 'e_' + digest(f'{sid}:{line}:{digest(record)}')[:32]
                                store.db.execute('INSERT INTO entries VALUES (?,?,?,?,?,?,?,?)',
                                                 (eid, sid, line, str(event.get('timestamp', '')),
                                                  kind, role, text, call_id))
                                source_locator.record(store, eid, payload, record, text, offset, len(raw))
                                store.db.execute('INSERT INTO docs VALUES (?,?,?)',
                                                 (eid, 'entry', ' '.join(tokens(text))))
                                batch_added += 1
                    except (ValueError, TypeError, UnicodeError) as exc:
                        raise ValueError(f'invalid event at line {line}: {type(exc).__name__}') from exc
                    hasher.update(raw)
                    offset += len(raw)
                    byte_count += len(raw)
                    count += 1
                if os.fstat(stream.fileno()).st_size < end:
                    raise ValueError(SOURCE_CHANGED)
                if count or not saved_scope:
                    store.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                                     (scope_key, json.dumps(scope, ensure_ascii=False)))
                if count or not prior:
                    store.db.execute('UPDATE sessions SET offset=?,lines=?,prefix_hash=? WHERE id=?',
                                     (offset, line, hasher.hexdigest(), sid))
            added += batch_added
            batches += bool(count)
            expected = (sid, offset, line, hasher.hexdigest())
            if tail or offset >= end:
                return dict(session_id=sid, chat_id=chat, added=added, incomplete_tail=tail,
                            committed_batches=batches, offset=offset,
                            scope_pending=not scope['admitted'],
                            excluded_records=scope['excluded_records'],
                            first_project_line=scope['first_project_line'])
