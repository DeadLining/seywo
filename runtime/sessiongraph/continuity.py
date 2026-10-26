"""Import only a selected task's new segments, without extraction or network."""
import json
import re
from pathlib import Path

from .import_diagnostics import classify_exception, source_issue


def _relocate_known_source(store, missing, roots, chat_id):
    """Rebind an archived source only after verifying its entire indexed prefix.

    Stable segment/entry IDs, scope and evidence are retained. A basename alone
    is never identity; ambiguous matches and changed prefixes fail closed.
    """
    from .ingestion import _prefix, SOURCE_CHANGED
    prior = store.db.execute("SELECT s.* FROM sessions s JOIN session_chats c ON c.segment_id=s.id WHERE s.path=? AND c.chat_id=?", (str(missing), chat_id)).fetchone()
    if prior is None:
        # Hooks/queued receipts can retain the pre-archive path after rebinding.
        # Resolve only one already-bound segment of this chat with that basename;
        # ingest still verifies its indexed prefix before accepting any new bytes.
        bound = [row for row in store.db.execute("SELECT s.* FROM sessions s JOIN session_chats c ON c.segment_id=s.id WHERE c.chat_id=?", (chat_id,)) if Path(row['path']).name == missing.name]
        if len(bound) != 1:
            return missing
        prior = bound[0]
        canonical = Path(prior['path']).resolve()
        if canonical.is_file() and any(canonical.is_relative_to(root) for root in roots):
            return canonical
    matches = set()
    for root in roots:
        for candidate in root.rglob(missing.name):
            resolved = candidate.resolve()
            if resolved.is_file() and any(resolved.is_relative_to(r) for r in roots):
                matches.add(resolved)
    verified = []
    for candidate in sorted(matches):
        try:
            with candidate.open('rb') as stream:
                if candidate.stat().st_size >= prior['offset'] and _prefix(stream, prior['offset']).hexdigest() == prior['prefix_hash']:
                    verified.append(candidate)
        except OSError:
            continue
    if len(verified) != 1:
        if matches:
            raise ValueError(SOURCE_CHANGED)
        return missing
    destination = verified[0]
    with store.db:
        # A concurrent import may have advanced/rebound this source while hashing.
        if store.db.execute("SELECT 1 FROM sessions WHERE path=? AND id!=?", (str(destination), prior['id'])).fetchone():
            raise ValueError(SOURCE_CHANGED)
        changed = store.db.execute("UPDATE sessions SET path=? WHERE id=? AND path=? AND offset=? AND prefix_hash=?", (str(destination), prior['id'], prior['path'], prior['offset'], prior['prefix_hash'])).rowcount
        if changed != 1:
            raise ValueError(SOURCE_CHANGED)
    return destination


def refresh_chat(store, chat_id, transcript_path=None):
    store._ensure_writable()
    if not isinstance(chat_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', chat_id):
        raise ValueError('invalid chat_id')
    roots = [Path(p).expanduser().resolve() for p in store.config['hook']['allowed_transcript_roots']]
    if not roots:
        raise ValueError('configure transcript allowlist before refreshing')
    known_sources = {Path(r[0]) for r in store.db.execute(
        'SELECT s.path FROM sessions s JOIN session_chats c ON c.segment_id=s.id WHERE c.chat_id=?', (chat_id,))}
    # Hook fast path avoids scanning and hashing every historical segment.
    # Manual refresh retains full discovery for explicit reconciliation.
    if transcript_path:
        candidates = {Path(transcript_path).expanduser()}
        discovery = 'hook_path'
    else:
        candidates = set(known_sources)
        for root in roots:
            candidates.update(root.glob(f'**/*{chat_id}*.jsonl'))
        discovery = 'full_scan'
    results, skipped = [], []
    processed = set()
    relocated_names = set()
    for candidate in sorted(candidates, key=lambda p: (p not in known_sources, str(p))):
        known = candidate in known_sources or sum(p.name == candidate.name for p in known_sources) == 1
        if candidate not in known_sources and candidate.name in relocated_names:
            continue  # A known source owns this relocation, including its failure.
        try:
            path = candidate.resolve()
            if not any(path.is_relative_to(root) for root in roots):
                skipped.append(source_issue(candidate, 'outside_allowlist', known=known))
                continue
            if not path.exists() and known:
                relocated_names.add(candidate.name)
                path = _relocate_known_source(store, path, roots, chat_id)
            if path in processed:
                continue
            processed.add(path)
            with path.open('rb') as f:
                header = f.readline(1024 * 1024)
            if not header.endswith(b'\n'):
                code = 'invalid_metadata' if len(header) >= 1024 * 1024 else 'incomplete_metadata'
                skipped.append(source_issue(candidate, code, known=known))
                continue
            first = json.loads(header)
            if (not isinstance(first, dict) or first.get('type') != 'session_meta'
                    or not isinstance(first.get('payload'), dict)
                    or not isinstance(first['payload'].get('id'), str)):
                skipped.append(source_issue(candidate, 'invalid_metadata', known=known))
                continue
            if first['payload']['id'] != chat_id:
                skipped.append(source_issue(candidate, 'chat_identity_mismatch', known=known))
                continue
            # Store validates project cwd, immutable prefix, size and complete JSONL events.
            results.append(dict(path=str(path), **store.ingest(path)))
        except json.JSONDecodeError:
            skipped.append(source_issue(candidate, 'invalid_json', known=known))
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError) as exc:
            skipped.append(classify_exception(candidate, exc, known=known))
    return {'discovery': discovery, 'candidate_count': len(candidates), 'chat_id': chat_id, 'imports': results, 'skipped': skipped,
            'added_entries': sum(r['added'] for r in results), 'model_calls': 0,
            'note': 'Original messages imported only; no key-fact extraction or work-memory write.'}
