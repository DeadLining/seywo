"""Bounded, project-local request diagnostics. Never logs Authorization or reasoning."""
import hashlib
import json
import os
import sqlite3
from pathlib import Path
import re
import time
import uuid
from .stages import CURRENT

PIPELINE_REVISION = "2026-10-16-stability-v2"
RUNTIME_REVISION = "2026-10-20-bounded-recovery-v17"


def scrub(value, key=""):
    from .graph_contract import clean
    if isinstance(value, dict):
        return {k: ("[redacted]" if re.search(r"(?i)(authorization|api.?key|access.?token|password|secret)", k) else scrub(v,key)) for k,v in value.items()}
    if isinstance(value,list): return [scrub(v,key) for v in value]
    if isinstance(value,str):
        value = value.replace(key,"[redacted]") if key else value
        return clean(value)
    return value


def job_live(config, job_id):
    """Fail closed only after a definite absence; unavailable DB keeps diagnostics."""
    if not isinstance(job_id, str) or not job_id:
        return None
    try:
        db = sqlite3.connect(Path(config["database"]).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='sg2_jobs'").fetchone():
                return None
            row = db.execute("SELECT 1 FROM sg2_jobs WHERE id=? AND project=?",
                             (job_id, config["project_id"])).fetchone()
            return bool(row)
        finally:
            db.close()
    except (sqlite3.Error, KeyError, OSError):
        return None


def prune_diagnostics(folder, config):
    """Keep unresolved and surviving jobs replayable; apply retention after finish."""
    db = None
    try:
        db = sqlite3.connect(Path(config["database"]).resolve().as_uri() + "?mode=ro", uri=True)
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='sg2_jobs'").fetchone():
            return
        pending = {row[0] for row in db.execute(
            "SELECT id FROM sg2_jobs WHERE project=? AND state NOT IN ('complete','superseded')",
            (config["project_id"],))}
    except (sqlite3.Error, KeyError, OSError):
        # An unavailable state index must not erase the only failure evidence.
        return
    finally:
        if db is not None:
            db.close()
    files = sorted(folder.glob("request-*.json"), key=lambda p:p.stat().st_mtime, reverse=True)
    retained = {True: 0, False: 0}
    for old in files:
        try:
            prior = json.loads(old.read_text())
        except (ValueError, OSError):
            continue
        if not isinstance(prior, dict) or prior.get("job_id") in pending:
            continue
        failed = prior.get("state") not in {"received", "validated"}
        retained[failed] += 1
        if retained[failed] > (64 if failed else 31):
            old.unlink(missing_ok=True)


def read_response(extractor, req, handlers):
    from .resource_policy import admitted
    from .stage_leases import transport_window
    with admitted(extractor, req) as ticket:
        with transport_window(extractor):
            return _read_response(extractor, req, handlers, ticket)


def _read_response(extractor, req, handlers, ticket):
    from urllib import request
    from .auto_update import _atomic
    folder = Path(extractor.config["database"]).parent / "request-audit"
    identity = ticket['id'] if ticket else uuid.uuid4().hex
    job_id = CURRENT.get().get('job_id')
    req.add_header("X-SessionGraph-Request-Id", identity)
    body = json.loads(req.data)
    safe = scrub(body,extractor.key)
    serial = json.dumps(safe,ensure_ascii=False).encode()
    record = dict(id=identity, pipeline_revision=PIPELINE_REVISION, runtime_revision=RUNTIME_REVISION, run_id=os.environ.get("SESSIONGRAPH_RUN_ID"), created_at=time.time(),
                  endpoint=req.full_url, model=extractor.model, request_bytes=len(req.data),
                  max_output_tokens=extractor.max_output, state="started", provider=getattr(extractor,"provider_name","deepseek"), **CURRENT.get(),
                  request=safe if len(serial)<=256*1024 else None,
                  request_omitted=len(serial)>256*1024,
                  sanitized_request_sha256=hashlib.sha256(serial).hexdigest())
    live = job_live(extractor.config, job_id) is not False
    if live:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        prune_diagnostics(folder, extractor.config)
    path = folder / ("request-" + identity + ".json")
    trace = dict(id=identity, path=str(path), model=extractor.model, **CURRENT.get(),
                 max_output_tokens=extractor.max_output, request_bytes=len(req.data))
    if live:
        from .call_logs import record as record_call
        record_call(record, project=extractor.config.get("project_id", ""), secret=extractor.key)
        # Keep a bounded "started" receipt even if the process exits before
        # transport returns. The final record replaces it atomically below.
        _atomic(path, scrub(record, extractor.key))
        extractor.traces.append(trace)
    from .recovery_policy import settings as recovery_settings
    timed = recovery_settings(extractor.config) is not None
    started = time.monotonic() if timed else None
    headers_at = finished_at = None
    if timed:
        record.update(transport_phase="awaiting_headers", timeout_seconds=extractor.timeout,
                      transport_timing_scope="client_open_and_body_only")
    try:
        opener = request.build_opener(*handlers)
        if ticket:
            ticket['transport_started'] = time.monotonic()
            ticket['sent'] = True
        with opener.open(req, timeout=ticket['reserved_seconds'] if ticket else extractor.timeout) as response:
            if timed:
                headers_at = time.monotonic()
                record.update(transport_phase="reading_body", headers_ms=round((headers_at-started)*1000, 2))
            record["http_status"] = getattr(response,"status",200)
            headers = getattr(response,"headers",{})
            record["provider_request_id"] = headers.get("x-request-id") or headers.get("request-id")
            raw = response.read(2*1024*1024+1)
            if ticket:
                ticket['received'] = True
            if timed:
                finished_at = time.monotonic()
                record["transport_phase"] = "response_received"
        if ticket:
            ticket['elapsed_seconds'] = time.monotonic() - ticket['transport_started']
        record["response_bytes"] = len(raw)
        try:
            envelope = json.loads(raw)
        except (ValueError,UnicodeError):
            record["state"] = "invalid_json"
        else:
            if not isinstance(envelope, dict):
                record["state"] = "invalid_envelope"
                return raw
            from .resource_policy import usage
            usage(ticket, envelope.get('usage'))
            # Diagnostics observes an untrusted envelope; it must not become a
            # second protocol decoder that throws before the actual adapter.
            choices = envelope.get("choices")
            choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
            record.update(state="received", usage=envelope.get("usage"),
                          response_id=envelope.get("id"), response_model=envelope.get("model"), status=envelope.get("status"),
                          finish_reason=choice.get("finish_reason"),
                          incomplete_details=envelope.get("incomplete_details"))
            record["envelope_shape"] = {k:type(v).__name__ for k,v in envelope.items()}
            message = choice.get("message")
            text = message.get("content") if isinstance(message, dict) else None
            texts = [text] if isinstance(text, str) else []
            outputs = envelope.get("output")
            if not texts and isinstance(outputs, list):
                for item in outputs:
                    if not isinstance(item, dict) or item.get("type") != "message":continue
                    content = item.get("content")
                    if not isinstance(content, list):continue
                    texts.extend(p["text"] for p in content if isinstance(p, dict)
                                 and p.get("type") == "output_text" and isinstance(p.get("text"), str))
            text = "".join(texts)
            if "answers" in envelope:
                text = json.dumps({k:envelope[k] for k in ("answers","model","usage") if k in envelope},ensure_ascii=False)
            # Final answer only: never retain provider reasoning or analysis.
            record["output_excerpt"] = text[:4096]
            record["final_output"] = text[:256*1024]
            record["final_output_truncated"] = len(text)>256*1024
            record["output_excerpt_truncated"] = len(text)>4096
            trace.update(usage=record["usage"], finish_reason=record["finish_reason"], incomplete_details=record["incomplete_details"])
        return raw
    except Exception as exc:
        if timed:
            finished_at = time.monotonic()
        from .failures import describe, transport
        if ticket:
            from .failures import Failure
            observed = exc if isinstance(exc, Failure) else transport(exc, getattr(extractor, 'provider_name', 'deepseek'))
            ticket['code'] = observed.code
            from urllib.error import URLError
            from http.client import HTTPException
            ticket['transport_failure'] = (isinstance(exc, (URLError, OSError, HTTPException))
                and (observed.retryable or getattr(exc, 'code', None) in {401, 403}))
        from urllib.error import HTTPError
        from .failures import Failure
        record.update(state="response_failed" if isinstance(exc, Failure) else "transport_failed",
                      error_type=type(exc).__name__, http_status=getattr(exc,"code",None) if not isinstance(exc, Failure) else None,
                      failure=describe(exc if isinstance(exc, Failure) else transport(exc,getattr(extractor,"provider_name","deepseek")),CURRENT.get().get("stage","")))
        if isinstance(exc,HTTPError):
            if ticket:
                ticket['http_response'] = True
            try:
                data=exc.read(65537)
                record["error_body_truncated"] = len(data)>65536
                headers=getattr(exc,"headers",{}) or {}
                record["error_content_type"]=headers.get("Content-Type","")
                record["provider_request_id"]=headers.get("x-request-id") or headers.get("request-id")
                try:
                    record["error_body"] = scrub(json.loads(data[:65536]),extractor.key)
                    # Preserve bounded structured error evidence for the adapter;
                    # the HTTPError body has now been consumed by diagnostics.
                    exc.provider_error_body = record["error_body"]
                except (ValueError,UnicodeError):
                    # Proxy/CDN HTTP failures often have HTML/plain-text bodies.
                    # Preserve a bounded, redacted diagnostic instead of discarding it.
                    record["error_body_text"] = scrub(data[:4096].decode("utf-8",errors="replace"),extractor.key)
                    record["error_body_truncated"] = len(data)>4096
            except Exception:
                record["error_body_unavailable"] = True
        raise
    finally:
        if ticket and ticket['sent'] and 'elapsed_seconds' not in ticket:
            ticket['elapsed_seconds'] = time.monotonic() - ticket['transport_started']
        # ProxyHandler rewrites req.host and sets _tunnel_host before HTTPS
        # connection. Persist the selected connection authority on success AND
        # failure; never include headers, credentials, or query strings.
        from urllib.parse import urlsplit
        connection = urlsplit("//" + req.host)
        record["transport"] = dict(
            mode="https_connect_proxy" if getattr(req, "_tunnel_host", None) else "direct",
            connect_host=connection.hostname, connect_port=connection.port,
            target_host=urlsplit(req.full_url).hostname,
        )
        trace["transport"] = record["transport"]
        record["elapsed_ms"] = round((time.time()-record["created_at"])*1000,2)
        if timed:
            finished_at = time.monotonic() if finished_at is None else finished_at
            record["elapsed_ms"] = round((finished_at-started)*1000, 2)
            if headers_at is not None:
                record["body_ms"] = round((finished_at-headers_at)*1000, 2)
            trace.update({k:record[k] for k in ("transport_phase", "headers_ms", "body_ms") if k in record})
        trace["elapsed_ms"] = record["elapsed_ms"]
        if live:
            # Re-check after transport so a deletion that raced this request
            # also removes the initial "started" record created above.
            if job_live(extractor.config, job_id) is not False:
                _atomic(path,scrub(record,extractor.key))
                record_call(record, project=extractor.config.get("project_id", ""), secret=extractor.key)
            else:
                from .call_logs import remove_jobs
                remove_jobs(extractor.config, [job_id])
                path.unlink(missing_ok=True)


def _audit_job_live(extractor, record, job_id):
    """Return False only for a definite absent job; None keeps diagnostics."""
    config = getattr(extractor, "audit_config", None) or getattr(extractor, "config", None)
    if not isinstance(config, dict):
        return None
    selected = job_id if job_id is not None else record.get("job_id")
    return job_live(config, selected)


def validation_result(extractor, failure=None, *, job_id=None, part=None):
    """Attach local validation to the exact transport receipt without exposing keys."""
    from .auto_update import _atomic
    traces = getattr(extractor, "traces", [])
    if not traces:return
    path=Path(traces[-1]["path"])
    try:
        record=json.loads(path.read_text())
    except (OSError, ValueError):return
    if job_id is not None and record.get("job_id") != job_id:return
    if part is not None and record.get("part") != part:return
    if record.get("state") == "transport_failed":return
    # A chat can be deleted between transport completion and this validation
    # write. Do not resurrect its audit file; an unavailable DB stays fail-open.
    if _audit_job_live(extractor, record, job_id) is False:
        path.unlink(missing_ok=True)
        return
    record["state"]="validation_failed" if failure else "validated"
    if failure:record["failure"]=failure
    _atomic(path,scrub(record,getattr(extractor,"key","")))
    from .call_logs import record as record_call
    config = getattr(extractor, "audit_config", None) or getattr(extractor, "config", {})
    record_call(record, project=config.get("project_id", ""), secret=getattr(extractor,"key",""))
