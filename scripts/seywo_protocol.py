"""Portable bounded pipe reads for read-only Codex acceptance scripts.

Windows select() accepts sockets, not subprocess pipes. One daemon reader feeds
bounded chunks to the caller; deadlines remain in the synchronous verifier.
"""
import os
from queue import Empty, Queue
from threading import Thread


class PipeReader:
    def __init__(self, stream):
        self.chunks = Queue(maxsize=64)
        self.eof = False
        def read():
            try:
                while data := os.read(stream.fileno(), 65536):
                    self.chunks.put(data)
            except (OSError, ValueError):
                pass
            finally:
                self.chunks.put(b'')
        Thread(target=read, daemon=True).start()

    def read(self, timeout):
        if self.eof:
            return b''
        try:
            data = self.chunks.get(timeout=timeout)
        except Empty:
            return None
        if data == b'':
            self.eof = True
        return data


def wait_server(request, thread_id, name='seywo', timeout=60):
    """Wait for asynchronous MCP startup, also accepting older tool-only hosts."""
    import time
    deadline = time.monotonic() + timeout
    while True:
        inventory = request('mcpServerStatus/list', {'threadId': thread_id})
        server = next((item for item in inventory['data'] if item['name'] == name), None)
        if server is not None and server.get('runtimeStatus') != 'starting':
            return server
        if time.monotonic() >= deadline:
            raise TimeoutError('MCP server startup did not complete')
        time.sleep(.2)
