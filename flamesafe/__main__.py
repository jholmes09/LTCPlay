"""python -m flamesafe <config.json>

Starts the safety program.  If the config carries link.arm_port (build step
7b), the real arm input listens there for the Stream Deck (ltcplay's own
process, never this one) and every group stays disarmed until it connects
and is cycled.  Without link.arm_port, flamesafe runs exactly as it always
has: NullArmInput, every group disarmed, the flame universe all zeros at
priority 200.  A config that fails any check stops it here with one sentence.

Stopping.  Ctrl-C, SIGTERM (and Ctrl-Break on Windows) set the stop event,
and the service then sends zeros and the stream-terminated flag.  On Windows
nothing else reaches this handler (End task is a hard kill and sends no
zeros), so build step 7b must give the operator an in-band stop that sets
the same event.  See service.py's module docstring for what a hard kill
leaves on the wire.
"""

from __future__ import annotations

import os
import signal
import sys
import threading

from . import config as config_mod
from .arminput import NullArmInput, SocketArmInput
from .journal import Journal
from .service import Service


def profile_line(g):
    """The start line for a group with a safety_profile."""
    p = g.profile
    window = ("" if p["enable_lo"] is None else
              f", enable window {p['enable_lo']} to {p['enable_hi']}")
    guard = ("it has no fire guard (no fire_at in its safety_profile)"
             if p["fire_at"] is None else
             f"fire guard: below {p['fire_at']}, every single-bit flip too")
    return (f"{g.name}: arms at {g.arm_value} from its safety_profile"
            f"{window}; {guard}")


def aim_line(groups, path):
    """The start line for the aim slots (2026-10-28): every group's aim
    slots, or that there are none, and the file they were read from, so a
    journal shows in one line whether the wave flamer's angles are loaded
    and from which flamesafe.json. Display only."""
    where = os.path.abspath(str(path))
    parts = [f"{g.name} {', '.join(str(a) for a in sorted(g.aim))}"
             for g in groups if g.aim]
    if not parts:
        return (f"aim slots: none in {where}. Every fire slot counts as a "
                f"flame, a wave flamer's angle too.")
    return (f"aim slots: {'; '.join(parts)} (the angle, sent only while "
            f"the group is armed), read from {where}")


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
    # safety_profile (2026-10-26): one plain line per group that arms at a
    # value of its own, and whether that value has a fire guard.
    for g in cfg.groups:
        if g.profile is not None:
            journal.event("config", profile_line(g))
    journal.event("config", aim_line(cfg.groups, argv[0]))
    if cfg.link_arm_port is not None:
        arm_input = SocketArmInput(cfg.link_arm_ip, cfg.link_arm_port,
                                   cfg.link_key, cfg.n, log=journal,
                                   stale_ms=cfg.arm_stale_ms)
        journal.event("config", f"arm input: the Stream Deck asserts arm "
                                f"over {cfg.link_arm_ip}:{cfg.link_arm_port} "
                                f"(build step 7b); until it connects and is "
                                f"cycled, every group stays disarmed")
    else:
        arm_input = NullArmInput()
        journal.event("config", "no arm input configured (link.arm_port is "
                                "not set): every group stays disarmed and "
                                "the flame universe is all zeros")
    svc = Service(cfg, arm_input, log=journal)
    if cfg.lockout_file:
        journal.event("config", f"lockout: fire slots named in "
                                f"{cfg.lockout_file} (written by ltcplay's "
                                f"engine) are held at zero on every packet, "
                                f"and a group with every slot locked cannot "
                                f"arm")
    else:
        journal.event("config", "no lockout_file is set: nothing can be "
                                "locked out from the Morning and Shutdown "
                                "page")
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
