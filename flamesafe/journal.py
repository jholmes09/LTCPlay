"""flamesafe journal: one plain line per event, to stdout and to a file.

Never raises, and NEVER BLOCKS THE TICK LOOP.  Writing goes through a
bounded queue to a helper thread: on Windows a console in QuickEdit mode
(a click in the window selects text) blocks every writer to stdout until
the selection is released, and a tick loop that writes its journal inline
would freeze with an armed group on the wire.  The composer's event() call
only puts a line on the queue; if the queue is full the line is dropped
and counted, which is the safe direction.
"""

from __future__ import annotations

import datetime
import queue
import sys
import threading
import time
from pathlib import Path

QUEUE_MAX = 1000


class Journal:

    def __init__(self, log_dir=None, stream=None):
        self.stream = stream if stream is not None else sys.stdout
        self.path = None
        if log_dir:
            try:
                d = Path(log_dir)
                d.mkdir(parents=True, exist_ok=True)
                self.path = d / "flamesafe.log"
            except OSError:
                self.path = None
        self.lines = []
        self.dropped = 0
        self._q = queue.Queue(maxsize=QUEUE_MAX)
        self._thread = threading.Thread(target=self._writer, daemon=True,
                                        name="flamesafe-journal")
        self._thread.start()

    def event(self, kind, msg):
        """Queue one line.  Returns at once whatever the console is doing."""
        try:
            stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            line = f"{stamp}  {kind}: {msg}"
            self.lines.append(line)
            if len(self.lines) > 500:
                del self.lines[:-500]
            try:
                self._q.put_nowait(line)
            except queue.Full:
                self.dropped += 1
        except Exception:                               # noqa: BLE001
            pass

    def _writer(self):
        while True:
            try:
                line = self._q.get()
            except Exception:                           # noqa: BLE001
                continue
            try:
                print(line, file=self.stream, flush=True)
            except Exception:                           # noqa: BLE001
                pass
            if self.path is not None:
                try:
                    with open(self.path, "a", encoding="utf-8") as fh:
                        fh.write(line + "\n")
                except Exception:                       # noqa: BLE001
                    pass

    def flush(self, timeout=2.0):
        """For tests and a clean stop: wait until the queue has drained or
        the timeout passes.  Never raises."""
        try:
            end = time.perf_counter() + timeout
            while not self._q.empty() and time.perf_counter() < end:
                time.sleep(0.01)
        except Exception:                               # noqa: BLE001
            pass
