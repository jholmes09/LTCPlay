"""python -m flamesafe <config.json>

Starts the safety program with NO arm input: every group stays disarmed and
the flame universe is all zeros at priority 200 until build step 7b adds the
Stream Deck.  A config that fails any check stops it here with one sentence.

Stopping.  Ctrl-C, SIGTERM (and Ctrl-Break on Windows) set the stop event,
and the service then sends zeros and the stream-terminated flag.  On Windows
nothing else reaches this handler (End task is a hard kill and sends no
zeros), so build step 7b must give the operator an in-band stop that sets
the same event.  See service.py's module docstring for what a hard kill
leaves on the wire.
"""

from __future__ import annotations

import signal
import sys
import threading

from . import config as config_mod
from .arminput import NullArmInput
from .journal import Journal
from .service import Service


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if len(argv) != 1:
        print("Usage: python -m flamesafe <config.json>")
        return 2
    try:
        cfg = config_mod.load(argv[0])
    except config_mod.ConfigError as e:
        print(f"flamesafe will not start: {e}")
        return 2
    journal = Journal(cfg.log_dir)
    if not cfg.confirmed:
        journal.event("config", "UNCONFIRMED numbers: the universe, the "
                                "group map and the arm value in this config "
                                "have not been confirmed by Andy. "
                                + (cfg.note or ""))
    journal.event("config", "no arm input in this build: every group stays "
                            "disarmed and the flame universe is all zeros")
    svc = Service(cfg, NullArmInput(), log=journal)
    stop = threading.Event()

    def _stop(*_a):
        stop.set()

    for sig in (getattr(signal, "SIGINT", None),
                getattr(signal, "SIGTERM", None),
                getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            try:
                signal.signal(sig, _stop)
            except (ValueError, OSError):
                pass
    try:
        svc.open()
    except OSError as e:
        print(f"flamesafe will not start: it cannot open its sockets: {e}")
        return 2
    try:
        svc.run_forever(stop)
    finally:
        svc.close()
        # Let the journal's writer thread drain what the stop wrote (it is
        # a daemon thread and would otherwise die with the queue unwritten).
        journal.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
