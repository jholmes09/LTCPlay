"""flamesafe journal: one plain line per event, to stdout and to a file.

Never raises.  A logging failure must not be able to stop the composer.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path


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

    def event(self, kind, msg):
        try:
            stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            line = f"{stamp}  {kind}: {msg}"
            self.lines.append(line)
            if len(self.lines) > 500:
                del self.lines[:-500]
            try:
                print(line, file=self.stream, flush=True)
            except Exception:                           # noqa: BLE001
                pass
            if self.path is not None:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception:                               # noqa: BLE001
            pass
