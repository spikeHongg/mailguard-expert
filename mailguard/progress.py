"""Idle progress deadline that also detects a suspended scheduler on resume."""
import time


class ProgressDeadline:
    def __init__(self,seconds):
        self.seconds=seconds
        self.completed()

    def completed(self):
        # Only a terminal email result resets progress, never bytes or retries.
        self.monotonic=time.monotonic()
        self.wall=time.time()

    def remaining(self):
        elapsed=max(0,time.monotonic()-self.monotonic,time.time()-self.wall)
        return max(0,self.seconds-elapsed)

    def expired(self):return self.remaining()<=0

    def wait_seconds(self):
        # A long OS wait may itself exclude suspension. Recheck wall time promptly.
        return min(1.0,self.remaining())
