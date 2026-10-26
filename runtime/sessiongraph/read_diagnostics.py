"""Bounded phase telemetry. No prompts, SQL, source text, paths or credentials."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import sys
import time

CURRENT=ContextVar('sessiongraph_read_trace',default=None)


class ReadTimeout(TimeoutError):
    def __init__(self,phase):
        self.phase=phase
        super().__init__('local_read_timeout')


class ReadTrace:
    def __init__(self,deadline_seconds=8):
        self.start=time.monotonic();self.deadline=self.start+deadline_seconds
        self.current='request';self.phases={};self.token=None

    def __enter__(self):
        self.token=CURRENT.set(self);return self

    def __exit__(self,*args):
        CURRENT.reset(self.token)

    def check(self):
        if time.monotonic()>self.deadline: raise ReadTimeout(self.current)

    @contextmanager
    def phase(self,name):
        self.current=name
        print(json.dumps({'sg_read_phase':name}),file=sys.stderr,flush=True)
        self.check();start=time.monotonic()
        try: yield
        finally: self.phases[name]=round((time.monotonic()-start)*1000,3)
        self.check()

    def summary(self):
        return {'phases_ms':self.phases,'elapsed_ms':round((time.monotonic()-self.start)*1000,3),'model_calls':0}


@contextmanager
def phase(name):
    trace=CURRENT.get()
    if trace:
        with trace.phase(name): yield
    else: yield
