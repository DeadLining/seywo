"""Bounded import diagnostics: no source text or arbitrary exception messages."""
from collections import Counter
import re

REASONS = {
    'outside_allowlist': 'source is outside the transcript allowlist',
    'outside_project': 'session cwd is outside configured project roots',
    'chat_identity_mismatch': 'source belongs to a different chat',
    'source_missing': 'source file is missing',
    'permission_denied': 'source file cannot be read',
    'source_changed': 'indexed source prefix changed or was truncated',
    'record_too_large': 'one JSONL record exceeds the memory limit; checkpoint retained before it',
    'source_too_large': 'source exceeds the import size limit',
    'incomplete_metadata': 'initial metadata record is not complete; retry after the writer finishes',
    'invalid_metadata': 'source session metadata is invalid',
    'invalid_event': 'source contains an invalid complete event',
    'invalid_json': 'source metadata is not valid JSON',
    'io_error': 'source could not be read',
    'unknown_import_error': 'unclassified import failure; extraction blocked',
}
EXCLUSIONS = {'outside_allowlist', 'outside_project', 'chat_identity_mismatch'}
RETRYABLE = {'source_missing', 'permission_denied', 'io_error', 'invalid_json', 'incomplete_metadata'}


def source_issue(path, code, *, known=False, line=None):
    code = code if code in REASONS else 'unknown_import_error'
    blocking = known or code not in EXCLUSIONS
    result = dict(path=str(path)[:1024], path_truncated=len(str(path)) > 1024,
                  code=code, reason=REASONS[code], known_source=known,
                  severity='error' if blocking else 'warning', blocking=blocking,
                  retryable=code in RETRYABLE)
    if line is not None:
        result['line'] = line
    return result


def classify_exception(path, exc, *, known=False):
    # Exact messages originate in Store.ingest; never echo arbitrary exception text.
    if isinstance(exc, FileNotFoundError):
        code = 'source_missing'
    elif isinstance(exc, PermissionError):
        code = 'permission_denied'
    elif isinstance(exc, OSError):
        code = 'io_error'
    else:
        text = str(exc)
        oversized = re.fullmatch(r'source record exceeds import memory limit at line (\d+)', text)
        if oversized:
            return source_issue(path, 'record_too_large', known=known, line=int(oversized[1]))
        match = re.fullmatch(r'invalid event at line (\d+): \w+', text)
        if match:
            return source_issue(path, 'invalid_event', known=known, line=int(match[1]))
        code = {
            'session cwd is outside configured project roots': 'outside_project',
            'source path registered with a different chat id': 'chat_identity_mismatch',
            'source changed or was truncated; rebuild into a fresh index': 'source_changed',
            'source exceeds 128 MiB import limit': 'source_too_large',
            'invalid session metadata': 'invalid_metadata',
            'invalid session metadata: expected object': 'invalid_metadata',
            'first event must be session_meta': 'invalid_metadata',
            'invalid session id': 'invalid_metadata',
        }.get(text, 'unknown_import_error')
    return source_issue(path, code, known=known)


def summarize_import(imported):
    """Normalize old/new worker results; only known safe exclusions are nonfatal."""
    if not isinstance(imported, dict) or not isinstance(imported.get('skipped', []), list):
        raise ValueError('invalid_import_result')
    imports = imported.get('imports', [])
    if not isinstance(imports, list) or any(not isinstance(item, dict) for item in imports):
        raise ValueError('invalid_import_result')
    issues = []
    for raw in imported.get('skipped', []):
        raw = raw if isinstance(raw, dict) else {}
        code = raw.get('code')
        # Untyped legacy skip records cannot safely be reinterpreted as exclusions.
        code = code if isinstance(code, str) and code in REASONS else 'unknown_import_error'
        issue = source_issue(raw.get('path', ''), code, known=bool(raw.get('known_source')))
        if raw.get('blocking') is True:
            issue.update(blocking=True, severity='error')
        if type(raw.get('line')) is int and 0 < raw['line'] < 100_000_000:
            issue['line'] = raw['line']
        issues.append(issue)
    blocking = [item for item in issues if item['blocking']]
    error = None
    if blocking:
        code = blocking[0]['code']
        error = 'import_sources_skipped' if code == 'unknown_import_error' else 'import_' + code
    elif 'imports' in imported and not imports:
        error = ('import_project_binding_conflict' if issues and all(
            item['code'] == 'outside_project' for item in issues) else
            'import_no_valid_sources' if issues else 'import_sources_not_found')
    elif imports and all(item.get('scope_pending') for item in imports):
        error = 'import_project_boundary_unconfirmed'
    tails = sum(bool(item.get('incomplete_tail')) for item in imports)
    diagnostics = dict(imported_source_count=sum("indexed_boundary" not in item for item in imports), skipped_source_count=len(issues),
                       retryable=bool(blocking) and all(item['retryable'] for item in blocking),
                       blocking_count=len(blocking), excluded_count=len(issues) - len(blocking),
                       incomplete_tail_count=tails,
                       counts_by_code=dict(sorted(Counter(item['code'] for item in issues).items())),
                       sources=issues[:20], omitted_sources=max(0, len(issues) - 20))
    warnings = []
    if any(item.get('excluded_records', 0) for item in imports):
        warnings.append('import_cross_project_records_excluded')
    if len(issues) > len(blocking):
        warnings.append('import_sources_excluded')
    if tails:
        warnings.append('import_incomplete_tail')
    return diagnostics, error, warnings
