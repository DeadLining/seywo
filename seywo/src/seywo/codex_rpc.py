"""Bounded newline JSON-RPC transport for an owned app-server process."""
from collections import deque
import json
import os
import queue
import subprocess
import tempfile
import threading
import time

from .codex_runtime import app_server_command, classify_error
from .runtime_platform import ProcessScope

MAX_LINE_BYTES = 2 * 1024 * 1024
MAX_SESSION_BYTES = 4 * 1024 * 1024
MAX_MESSAGES = 256


class AppServerError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise AppServerError('codex_timeout')
    return value


def protocol_error(error):
    if isinstance(error, dict) and error.get('code') in {-32600, -32601, -32602}:
        return 'codex_app_server_incompatible'
    return classify_error(json.dumps(error, ensure_ascii=False))


class RpcConnection:
    """One pipe pair; worker serialization owns request/notification ordering."""
    def __init__(self, command, home, *, max_line_bytes=MAX_LINE_BYTES):
        self.scope = ProcessScope()
        self.workspace = tempfile.TemporaryDirectory(prefix='seywo-worker-')
        self.events = queue.Queue(maxsize=MAX_MESSAGES)
        self._queue_lock = threading.Lock()
        self._queued_bytes = 0
        self.deferred = deque()
        self.failure = None
        self.diagnostic = bytearray()
        self.byte_count = 0
        self.request_id = 0
        self.closed = False
        try:
            args = app_server_command(command, home)
            self.process = self.scope.start(args, cwd=self.workspace.name,
                env={**os.environ, 'CODEX_HOME': str(home), 'SEYWO_INTERNAL_MODEL': '1'},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except BaseException:
            self.workspace.cleanup()
            raise
        self.readers = [
            threading.Thread(target=self._read, args=(max_line_bytes,), daemon=True,
                             name='seywo-app-server-read'),
            threading.Thread(target=self._read_errors, daemon=True,
                             name='seywo-app-server-errors'),
        ]
        for reader in self.readers:
            reader.start()

    @property
    def alive(self):
        return not self.closed and self.process.poll() is None and self.failure is None

    def reset_budget(self):
        self.byte_count = 0
        self.deferred.clear()

    def _put(self, message):
        # Bound bytes at the producer too: an idle consumer must not let the
        # queue retain MAX_MESSAGES * MAX_LINE_BYTES worth of notifications.
        overflow = False
        with self._queue_lock:
            if self.failure:
                return
            if self._queued_bytes + message[0] > MAX_SESSION_BYTES:
                overflow = True
            else:
                try:
                    self.events.put_nowait(message)
                    self._queued_bytes += message[0]
                except queue.Full:
                    overflow = True
            if overflow:
                self.failure = 'codex_output_limit'
        if overflow:
            self.scope.terminate()

    def _read(self, limit):
        try:
            while not self.closed and self.failure is None:
                raw = self.process.stdout.readline(limit + 1)
                if not raw:
                    break
                if len(raw) > limit:
                    self.failure = 'codex_output_limit'
                    self.scope.terminate()
                    break
                try:
                    msg = json.loads(raw)
                    if not isinstance(msg, dict):
                        raise ValueError()
                except (ValueError, UnicodeError):
                    self.failure = 'codex_app_server_invalid'
                    self.scope.terminate()
                    break
                self._put((len(raw), msg))
        except (OSError, ValueError):
            pass
        finally:
            self._put((0, None))

    def _read_errors(self):
        try:
            while chunk := self.process.stderr.read(4096):
                self.diagnostic.extend(chunk[:max(0, 8192 - len(self.diagnostic))])
        except (OSError, ValueError):
            pass

    def _broken(self):
        # Provider stderr is never confused with source text or model output.
        self.readers[1].join(timeout=.1)
        code = classify_error(self.diagnostic.decode('utf-8', errors='replace'))
        return AppServerError(code if code != 'codex_execution_failed' else 'codex_worker_broken')

    def send(self, message, deadline):
        if not self.alive:
            raise AppServerError(self.failure) if self.failure else self._broken()
        data = (json.dumps(message, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
        if len(data) > MAX_SESSION_BYTES:
            raise AppServerError('codex_input_limit')
        completed = threading.Event()
        error = []
        process = self.process
        def write():
            try:
                process.stdin.write(data)
                process.stdin.flush()
            except (OSError, ValueError) as exc:
                error.append(exc)
            finally:
                completed.set()
        writer = threading.Thread(target=write, name='seywo-app-server-write', daemon=True)
        writer.start()
        if not completed.wait(remaining(deadline)):
            self.scope.terminate()
            writer.join(timeout=.2)
            raise AppServerError('codex_timeout')
        if error:
            raise self._broken()

    def _read_message(self, deadline):
        if self.failure:
            raise AppServerError(self.failure)
        try:
            size, message = self.events.get(timeout=remaining(deadline))
        except queue.Empty as exc:
            raise AppServerError('codex_timeout') from exc
        with self._queue_lock:
            self._queued_bytes -= size
        self.byte_count += size
        if self.byte_count > MAX_SESSION_BYTES:
            raise AppServerError('codex_output_limit')
        if message is None:
            raise AppServerError(self.failure) if self.failure else self._broken()
        # An extraction task may never execute approvals or dynamic tools.
        if 'id' in message and 'method' in message:
            raise AppServerError('codex_unexpected_tool_use')
        return message

    def request(self, method, params, deadline):
        self.request_id += 1
        request_id = self.request_id
        self.send({'id': request_id, 'method': method, 'params': params}, deadline)
        while True:
            message = self._read_message(deadline)
            if message.get('id') != request_id:
                if 'method' in message:
                    if len(self.deferred) >= MAX_MESSAGES:
                        raise AppServerError('codex_output_limit')
                    self.deferred.append(message)
                continue
            if 'error' in message:
                raise AppServerError(protocol_error(message['error']))
            if not isinstance(message.get('result'), dict):
                raise AppServerError('codex_app_server_invalid')
            return message['result']

    def next_event(self, deadline):
        return self.deferred.popleft() if self.deferred else self._read_message(deadline)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.scope.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)
        for reader in self.readers:
            reader.join(timeout=.2)
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            pipe.close()
        self.workspace.cleanup()
