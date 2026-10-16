"""Client-side model request overlap measurement, without credentials or payloads."""
from threading import Lock
from contextlib import contextmanager
import time


class InFlightMeter:
    """Records client-side request overlap without recording payloads or credentials."""
    def __init__(self):
        self.lock=Lock();self.active=0;self.peak=0;self.calls=[]

    @contextmanager
    def track(self):
        start=time.perf_counter();outcome='response'
        with self.lock:
            self.active+=1;self.peak=max(self.peak,self.active)
        try:yield
        except BaseException as exc:
            outcome=type(exc).__name__;raise
        finally:
            with self.lock:
                self.active-=1
                self.calls.append({'seconds':round(time.perf_counter()-start,4),'outcome':outcome})
