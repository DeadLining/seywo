"""One error contract for transport, stages, retries and diagnostics."""
from __future__ import annotations
import errno
import http.client
import ssl
import sqlite3
from urllib import error


class Failure(ValueError):
    def __init__(self, code, *, retryable=False, stage='', provider='', detail='', exception_type=''):
        super().__init__(code)
        self.code, self.retryable = code, retryable
        self.stage, self.provider = stage, provider
        self.detail, self.exception_type = detail, exception_type


def transport(exc, provider):
    """Call only around provider I/O, never around credential or database access."""
    prefix = 'jev' if provider == 'jev' else 'provider'
    cause = exc.reason if isinstance(exc, error.URLError) else exc
    code, retry = prefix + '_transport', True
    if isinstance(exc, error.HTTPError):
        code = f'{prefix}_http_{exc.code}'
        retry = exc.code in {408, 425, 429, 500, 502, 503, 504}
    elif isinstance(cause, ssl.SSLCertVerificationError):
        code, retry = prefix + '_tls_certificate', False
    elif isinstance(cause, ssl.SSLError):
        code = prefix + '_tls_handshake'
    elif isinstance(cause, http.client.IncompleteRead):
        code = prefix + '_response_truncated'
    elif isinstance(cause, TimeoutError):
        code = prefix + '_transport' if prefix == 'jev' else 'provider_timeout'
    elif isinstance(cause, OSError) and not isinstance(cause, ConnectionError):
        retry = cause.errno in {None, errno.ECONNRESET, errno.ECONNABORTED,
            errno.EPIPE, errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ETIMEDOUT}
    # Never stringify IncompleteRead.partial or an HTTP response body here.
    detail = str(cause)[:500] if not isinstance(cause, http.client.IncompleteRead) else 'response ended before Content-Length'
    return Failure(code, retryable=retry, provider=provider, detail=detail,
                   exception_type=type(cause).__name__)


def describe(exc, stage=''):
    from .request_audit import scrub
    from .memory_retry import retryable
    code = getattr(exc, 'code', None)
    if not code:
        code = ('graph_validation' if isinstance(exc, ValueError) else
                'graph_database' if isinstance(exc, sqlite3.Error) else
                'graph_io' if isinstance(exc, OSError) else 'graph_internal')
    detail=getattr(exc,'detail','')
    if not detail:
        if isinstance(exc,OSError):
            detail=f"errno={exc.errno}; {exc.strerror or type(exc).__name__}"
        elif isinstance(exc,ValueError) and str(exc).startswith(('graph_schema:','graph_evidence:','graph_source_changed')):
            detail=str(exc)[:500]
        else:
            detail=code  # arbitrary exception strings can contain transcript text or credentials
    result = dict(code=code, stage=getattr(exc, 'stage', '') or stage,
        provider=getattr(exc, 'provider', ''),
        retryable=bool(getattr(exc, 'retryable', False) or retryable(exc)),
        exception_type=getattr(exc, 'exception_type', '') or type(exc).__name__,
        detail=detail)
    if code in {'resource_circuit_open', 'stage_snapshot_changed', 'stage_lease_busy'}:
        result['retry_at'] = exc.retry_at
    return scrub(result)
