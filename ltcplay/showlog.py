"""A show log on disk.

A rehearsal you can debug afterwards beats a rehearsal someone describes to you
over the phone.  Every state change, cue change, jump, socket failure and
exception lands here with a wall clock time and the timecode it happened at,
so "the lights dropped out somewhere in the third song" becomes a line.
"""
import logging
import logging.handlers
import os
import time


class ShowLog:
    def __init__(self, path, echo=False, keep=5, max_bytes=5_000_000):
        self.path = path
        self.echo = echo
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        self._log = logging.getLogger("ltcplay")
        self._log.setLevel(logging.INFO)
        self._log.handlers[:] = []
        h = logging.handlers.RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=keep)
        h.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs)03d  %(message)s", "%Y-%m-%d %H:%M:%S"))
        self._log.addHandler(h)
        self._log.propagate = False
        self.player = None            # set by the caller, for timecode stamps
        self.timeline = None
        self._counts = {}
        self._last_of = {}

    def _stamp(self):
        p, tl = self.player, self.timeline
        if p is None or tl is None or p.tc_seconds is None or p.tc_seconds < 0:
            return "--:--:--:--"
        return tl.format(p.tc_seconds)

    def event(self, kind, msg, throttle_s=0.0):
        """Record one event. `throttle_s` collapses a repeating one."""
        self._counts[kind] = self._counts.get(kind, 0) + 1
        if throttle_s:
            now = time.monotonic()
            last = self._last_of.get(kind)
            if last is not None and now - last < throttle_s:
                return
            self._last_of[kind] = now
        line = f"[{self._stamp()}] {kind:12s} {msg}"
        self._log.info(line)
        if self.echo:
            print(line, flush=True)

    def info(self, msg):
        self._log.info(msg)
        if self.echo:
            print(msg, flush=True)

    def counts(self):
        return dict(self._counts)
