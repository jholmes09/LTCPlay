"""Send TEST Art-Net timecode by hand. Nothing else.

Built for the Fire & Ice day-one proof: does MadMapper lock to ltcplay's
Art-Net timecode, and does Andy's BEYOND lock to the same stream. Handoff
section 5, Jeff, 2026-09-24: "A timecode test button... yes. A way to send
test Art-Net timecode by hand for the day-one MadMapper and BEYOND proof,
outside the schedule."

This talks only Art-Net timecode, and reuses the exact code the show uses to
send it: clock.TimecodeOut for the socket and clock.Ticker for the pacing.
Nothing here opens a session, reads cues, sends pixels, opens an sACN
socket, or runs the scheduler. `ltcplay run` is a whole show; this is one
stream of test numbers, sent on purpose, to named machines.

Like every other file outside clock.py, nothing here imports it at module
scope: only `tctest` on the command line reaches it, at the top of
parse_start() and run(), the same rule session.py and timeline.py follow.
A show run through `ltcplay run` never touches this module at all.

Destinations are always named. There is no "send to everyone": a show file
names BEYOND because BEYOND follows this stream and plays its laser cues
from it, so test timecode with no destination, or a silent broadcast, is
exactly how a laser cue fires with nobody expecting it. Matching the
destination name against a list of known laser software is not safe -- a
node can be called "Lasers", "Andy", or anything else an operator chose.
So every run prints a warning naming every destination before the first
packet goes out, no matter what they are called. A destination whose name
mentions BEYOND or laser gets an extra, stronger line on top of that.

Refuses if ltcplay already holds the output lock (onlyone.py): two timecode
sources on the rig fight each other frame by frame, the same reason a
second `ltcplay run` refuses.
"""
import ipaddress
import sys
import time

from . import onlyone
from . import tc as tc_mod
from .appdata import WINDOWS
from .output import ARTNET_PORT

DEFAULT_START = "00:00:00:00"
DEFAULT_SECONDS = 60.0

GENERAL_WARNING = ("Anything that follows timecode will play its cues, "
                   "lasers included. Make sure their operators are ready.")

LASER_WARNING = ("This looks like a laser system. Confirm the laser "
                 "operator is ready before you continue.")

NO_DEST = ("tctest needs to know where to send test timecode. Give it one "
          "of: --show FILE --to NAME (one or more names from that show "
          "file's clock.artnet.nodes), --node NAME=IP given directly, or "
          "--broadcast ADDR. It never guesses a destination: anything "
          "that follows test timecode plays its cues, lasers included.")


class TcTestError(ValueError):
    """A tctest run that cannot start, with the sentence that says why."""


def parse_start(text):
    """'HH:MM:SS:FF' at 30 fps non drop -> (h, m, s, f).

    Goes through tc.py's own parser rather than a second regex, so a
    malformed --start is refused in the same words as a malformed cue
    timecode anywhere else in the program."""
    from .clock import MASTER_FPS
    try:
        seconds = tc_mod.parse_tc(text, MASTER_FPS, drop=False)
    except ValueError as e:
        raise TcTestError(str(e))
    return tc_mod.frames_to_tc(int(round(seconds * MASTER_FPS)), MASTER_FPS,
                               False)


def _ipv4(v):
    try:
        return str(ipaddress.IPv4Address(str(v).strip()))
    except (ipaddress.AddressValueError, ValueError):
        return None


def load_show_nodes(show_path):
    """The clock.artnet.nodes of a show file, name -> ip.

    Only that one block is read. tctest never loads cues, the controller
    map, or anything else a session would; --show names a file to read
    destinations from, not a show to run."""
    import json
    from .clock import ArtNetConfig

    try:
        with open(show_path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    except OSError as e:
        raise TcTestError(f"{show_path}: {e.strerror or e}")
    except ValueError as e:
        raise TcTestError(f"{show_path} is not valid JSON: {e}")
    clk = doc.get("clock")
    if not isinstance(clk, dict):
        raise TcTestError(f"{show_path} has no 'clock' block, so it names "
                          f"no Art-Net timecode nodes.")
    art = clk.get("artnet")
    if not isinstance(art, dict):
        raise TcTestError(f"{show_path}: 'clock' has no 'artnet' block, so "
                          f"it names no Art-Net timecode nodes.")
    cfg = ArtNetConfig.parse(art, show_path)
    if not cfg.nodes:
        bc = f" Use --broadcast {cfg.broadcast} instead of --to." \
            if cfg.broadcast else ""
        raise TcTestError(f"{show_path}: 'clock.artnet' names no nodes."
                          f"{bc}")
    return cfg.nodes


def resolve_destinations(show=None, to=(), node=(), broadcast=None):
    """Work out exactly who gets test timecode, and refuse everything else.

    Returns (dests, is_broadcast), dests a list of (name, ip). Never
    returns an empty list: every path either names somebody or raises
    TcTestError."""
    to = list(to or ())
    node = list(node or ())
    modes = []
    if show or to:
        modes.append("show")
    if node:
        modes.append("node")
    if broadcast:
        modes.append("broadcast")
    if len(modes) > 1:
        raise TcTestError(
            "Pick one way to say where test timecode goes: --show/--to, "
            "--node, or --broadcast, not more than one at a time.")

    if "show" in modes:
        if not show:
            raise TcTestError(
                "--to needs --show as well: name the show file its node "
                "names come from.")
        available = load_show_nodes(show)
        names = to
        if not names:
            raise TcTestError(
                "--show needs --to as well: name at least one node from "
                "its clock.artnet.nodes. tctest never defaults to sending "
                "to every node in the show file.")
        dests = []
        for name in names:
            if name not in available:
                raise TcTestError(
                    f"{name!r} is not a node in {show}'s "
                    f"clock.artnet.nodes. It has: "
                    f"{', '.join(sorted(available)) or 'none'}.")
            dests.append((name, available[name]))
        return dests, False

    if "node" in modes:
        dests, seen = [], set()
        for spec in node:
            if "=" not in spec:
                raise TcTestError(f"--node wants NAME=IP, not {spec!r}.")
            name, ip = (x.strip() for x in spec.split("=", 1))
            if not name:
                raise TcTestError(f"--node {spec!r} has no name before "
                                  f"the '='.")
            if name in seen:
                raise TcTestError(f"{name!r} is given more than once with "
                                  f"--node.")
            addr = _ipv4(ip)
            if addr is None:
                raise TcTestError(f"--node {name}={ip!r}: {ip!r} is not "
                                  f"an IPv4 address.")
            seen.add(name)
            dests.append((name, addr))
        return dests, False

    if "broadcast" in modes:
        addr = _ipv4(broadcast)
        if addr is None:
            raise TcTestError(f"--broadcast {broadcast!r} is not an IPv4 "
                              f"address.")
        return [("broadcast", addr)], True

    raise TcTestError(NO_DEST)


def laser_like_names(dests, is_broadcast):
    """Destination names that mention BEYOND or laser, case blind.

    Not a safety gate: run() warns about every destination regardless of
    its name. This only decides which ones get an extra, stronger line,
    so it is never the thing standing between silence and a laser cue --
    a node can be called "Lasers", "Andy", or anything else an operator
    chose, and matching a fixed word against a free-form name would miss
    it. Broadcast is always in the list: it is not possible to say from
    here whether a laser system is listening on the network, and a silent
    laser cue is the failure this exists to prevent."""
    if is_broadcast:
        return ["broadcast"]
    return [name for name, _ in dests
           if "beyond" in name.lower() or "laser" in name.lower()]


def _install_signals():
    """Return a predicate that goes true on ctrl-c or a kill.

    The same rule cli.py's run and serve follow: a show tool that leaves
    something running when it exits is worse than one that stops cleanly."""
    import signal
    flag = {"stop": False}

    def handler(signum, frame):
        flag["stop"] = True

    for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, AttributeError):
            pass
    return lambda: flag["stop"]


def run(dests, is_broadcast, start=DEFAULT_START, seconds=DEFAULT_SECONDS,
       bind_ip=None, port=ARTNET_PORT, lock_path=None,
       note="test timecode (tctest)", out_stream=sys.stdout,
       err_stream=sys.stderr, socket_factory=None, clock=time.perf_counter,
       sleep=time.sleep, stop_check=None):
    """Send Art-Net timecode to `dests` and nothing else.

    Returns an exit code the CLI hands straight to sys.exit. `stop_check`
    lets a test end a run without signals; the CLI leaves it None and gets
    ctrl-c and ctrl-break for free."""
    from .clock import MASTER_FPS, MASTER_TYPE, Ticker, TimecodeOut, \
        arttimecode
    h0, m0, s0, f0 = parse_start(start)
    dest_label = ", ".join(f"{n} ({ip})" for n, ip in dests)

    lock = onlyone.OutputLock(where=lock_path, note=note)
    try:
        lock.acquire()
    except onlyone.AlreadyRunning as e:
        here = "computer" if WINDOWS else "Mac"
        who = f" It says: {e.holder}." if e.holder else ""
        print(f"Another ltcplay on this {here} is already sending to the "
              f"rig.{who} Test timecode would fight it frame by frame, the "
              f"same as two players would. Stop that one first, or wait "
              f"for it to finish.", file=err_stream)
        return 2

    # Once the lock is held, everything below must release it on the way
    # out -- a clean end, ctrl-c, ctrl-break, or any exception, including
    # one raised while still setting up (a bad socket, a bad bind_ip). A
    # stale lock here is the exact failure onlyone.py exists to prevent:
    # it would stop the real show from starting. out_sock starts as None
    # so the finally below never calls close() on a socket that was never
    # built.
    out_sock = None
    try:
        # Before the first packet, every run says where test timecode is
        # headed. Matching destination names against a fixed word list is
        # not a safe gate -- see laser_like_names() -- so this line names
        # every destination, not only ones that look like laser software.
        print(f"Test timecode is about to go to: {dest_label}. "
              f"{GENERAL_WARNING}", file=err_stream)
        laser_names = laser_like_names(dests, is_broadcast)
        if laser_names:
            print(f"{', '.join(laser_names)}: {LASER_WARNING}",
                  file=err_stream)

        if stop_check is None:
            stop_check = _install_signals()

        start_frame = tc_mod.tc_to_frames(h0, m0, s0, f0, MASTER_FPS, False)
        total_frames = (int(round(seconds * MASTER_FPS)) if seconds > 0
                        else None)
        stopped = {"reason": None}
        out_sock = TimecodeOut(dests, broadcast=is_broadcast, port=port,
                               bind_ip=bind_ip,
                               socket_factory=socket_factory)

        def tick(n, now):
            if total_frames is not None and n >= total_frames:
                stopped["reason"] = \
                    f"sent {seconds:g} seconds of test timecode"
                return False
            if stop_check():
                stopped["reason"] = "stopped by the operator"
                return False
            h, m, s, f = tc_mod.frames_to_tc(start_frame + n, MASTER_FPS,
                                             False)
            h %= 24
            out_sock.send(arttimecode(h, m, s, f, MASTER_TYPE))
            if n % MASTER_FPS == 0:
                print(f"{h:02d}:{m:02d}:{s:02d}:{f:02d}  ->  {dest_label}   "
                      f"sent {out_sock.packets_sent}  failed "
                      f"{out_sock.send_errors}", file=out_stream)
            return True

        ticker = Ticker(MASTER_FPS, tick, clock=clock, sleep=sleep,
                        name="ltcplay-tctest")
        ticker.run(clock())
    finally:
        if out_sock is not None:
            out_sock.close()
        lock.release()

    print(stopped["reason"] or "stopped", file=out_stream)
    return 0
