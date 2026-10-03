"""A show log on disk.

A rehearsal you can debug afterwards beats a rehearsal someone describes to you
over the phone.  Every state change, cue change, jump, socket failure and
exception lands here with a wall clock time and the timecode it happened at,
so "the lights dropped out somewhere in the third song" becomes a line.
"""
import atexit
import logging
import logging.handlers
import os
import queue
import sys
import time

# The one background writer, when a ShowLog asks for one; a new ShowLog
# stops the old one (it drains first), as it replaces the old handlers.
_listener = None


@atexit.register
def _drain():
    """Every queued line is written before the program exits."""
    lst = _listener
    if lst is not None:
        try:
            lst.stop()
        except Exception:
            pass


class ShowLog:
    """`background` (Fire & Ice only, PR #43 review, finding 9): every line
    is handed to a writer thread of its own, file and console both, so the
    threads that log (the show audio's timecode thread among them) never
    write, flush or print, nor wait on the logging handler's lock while
    another thread does. The GPL path never passes it: its log is written
    exactly as it always was."""

    def __init__(self, path, echo=False, keep=5, max_bytes=5_000_000,
                 background=False):
        global _listener
        self.path = path
        self.echo = echo
        self.background = bool(background)
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        self._log = logging.getLogger("ltcplay")
        self._log.setLevel(logging.INFO)
        self._log.handlers[:] = []
        old, _listener = _listener, None
        if old is not None:
            try:
                old.stop()
            except Exception:
                pass
        # UTF-8 whatever the OS default is: a cue name Windows' own code
        # page cannot spell would otherwise drop the line from the log.
        h = logging.handlers.RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=keep, encoding="utf-8")
        h.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs)03d  %(message)s", "%Y-%m-%d %H:%M:%S"))
        self._listener = None
        if self.background:
            out = [h]
            if echo:
                e = logging.StreamHandler(sys.stdout)
                e.setFormatter(logging.Formatter("%(message)s"))
                out.append(e)
            q = queue.SimpleQueue()
            self._listener = logging.handlers.QueueListener(q, *out)
            self._listener.start()
            _listener = self._listener
            self._log.addHandler(logging.handlers.QueueHandler(q))
        else:
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
        if self.echo and not self.background:
            print(line, flush=True)

    def info(self, msg):
        self._log.info(msg)
        if self.echo and not self.background:
            print(msg, flush=True)

    def flush(self):
        """Wait until every line logged so far is written (background
        mode; a no-op otherwise)."""
        lst = self._listener
        if lst is not None and lst is _listener:
            lst.stop()
            lst.start()

    def counts(self):
        return dict(self._counts)
