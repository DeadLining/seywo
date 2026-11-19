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
