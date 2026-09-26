"""The show clock as a plugin: who decides where the show is.

Nothing in this module is imported unless the show file has a "clock" block.
A show file without one (GPL 2026 at Dollywood) runs exactly the path it has
always run: LTC in on an audio input, decoded, chased. See Session.open.

Three sources, one position stream. The position stream is
Player.feed_timecode(seconds, captured_at): every source ends up there, so the
chase engine does not know or care which one is in charge.

  ArtNetMaster     this machine IS the clock. Each cue starts at 00:00:00:00,
                   one Art-Net ArtTimeCode packet goes out per frame, 30 a
                   second, and the same position feeds the pixels. No
                   timecode input is opened.
  LtcAudioSlave    today's LTC decode path, untouched. The adapter only taps
                   the decoded frames, reads the hour field as a zone (01
                   show, 02 intermission, anything else idle, numbers from
                   the show file) and, when asked, forwards the show zone to
                   Art-Net for BEYOND. Fallback 3 of the Fire & Ice handoff.
  LtcAudioMaster   fallback 1, LTC audio generated on a named output device.
                   Not built. Naming it in a show file is refused at load
                   with a sentence, never discovered at showtime.

Which one runs is the "source" setting, and show audio is a separate
"show_audio" switch, so proving or disproving MadMapper's Art-Net lock on day
one changes a show file, not this code.

ArtTimeCode byte layout, from the Art-Net 4 specification (Artistic Licence,
"Art-Net 4 Protocol Release V1.4", document revision 1.4dp, 23/10/2025,
ArtTimeCode packet definition pp. 54-55, OpTimeCode 0x9700 in the opcode
table p. 21, UDP port 0x1936 in "Port" p. 10):

  0..7   ID        "Art-Net" then 0x00
  8..9   OpCode    0x9700, transmitted low byte first: 0x00 0x97
  10     ProtVerHi 0
  11     ProtVerLo 14
  12     Filler1   0
  13     StreamId  0x00 is the master stream
  14     Frames    0..29 depending on type
  15     Seconds   0..59
  16     Minutes   0..59
  17     Hours     0..23
  18     Type      0 Film 24, 1 EBU 25, 2 DF 29.97, 3 SMPTE 30

19 bytes. The spec says the source port is also 0x1936. This sends from an
ephemeral port, the same as the pixel sender always has, because MadMapper
listens on 6454 on the same machine and two programs cannot both own it.
BENCH DAY: confirm BEYOND accepts timecode from a port other than 6454.
--bind applies to this socket exactly as it does to the pixel sender.
"""
import ipaddress
import math
import socket
import threading
import time

from .output import ARTNET_PORT
from .tc import frames_to_tc, tc_to_frames

# tctest.py drives this module standalone, on purpose, without ever loading
# player.py or session.py -- proven fresh in selftest.py. So this does not
# `from .player import _now`; it uses time.perf_counter() directly, which
# is exactly what player._now() calls too (see player.py's module
# docstring). Same clock, by both defaulting to the same builtin, not by
# sharing an import.

OP_TIMECODE = 0x9700
PROTOCOL_VERSION = 14
ARTTIMECODE_LEN = 19
TYPE_FILM, TYPE_EBU, TYPE_DF, TYPE_SMPTE = 0, 1, 2, 3

# ltcplay as master always sends 30 fps non drop. Nothing in this rig comes
# from film or broadcast, and drop frame buys only arithmetic bugs. Handoff
# section 4, decided 2026-09-23. Not a setting on purpose.
MASTER_FPS = 30
MASTER_TYPE = TYPE_SMPTE

SOURCES = ("artnet_master", "ltc_audio_master", "ltc_audio_slave")
SHOW_AUDIO = ("madmapper", "ltcplay")
ROLES = ("show", "intermission")
IDLE = "idle"


class ClockConfigError(ValueError):
    """A clock block that cannot run, with the sentence that says why.

    A ValueError, so the session reports it the way it reports every other
    show file mistake: as a line a person can act on."""


# ---------------------------------------------------------------- wire ----
def type_for(count, drop=False):
    """ArtTimeCode type for a frame count and drop flag."""
    if drop:
        if count != 30:
            raise ValueError("drop frame is only defined for a 30 count")
        return TYPE_DF
    try:
        return {24: TYPE_FILM, 25: TYPE_EBU, 30: TYPE_SMPTE}[count]
    except KeyError:
        raise ValueError(f"Art-Net timecode has no type for {count} frames "
                         f"a second")


_COUNT_FOR_TYPE = {TYPE_FILM: 24, TYPE_EBU: 25, TYPE_DF: 30, TYPE_SMPTE: 30}


def arttimecode(h, m, s, f, tc_type=MASTER_TYPE, stream_id=0):
    """One ArtTimeCode packet, 19 bytes. See the layout at the top."""
    if tc_type not in _COUNT_FOR_TYPE:
        raise ValueError(f"ArtTimeCode type {tc_type} does not exist")
    if not (0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59
            and 0 <= f < _COUNT_FOR_TYPE[tc_type]):
        raise ValueError(f"{h:02d}:{m:02d}:{s:02d}:{f:02d} is not a timecode "
                         f"of type {tc_type}")
    if not 0 <= stream_id <= 255:
        raise ValueError("stream id is one byte")
    b = bytearray(ARTTIMECODE_LEN)
    b[0:8] = b"Art-Net\x00"
    b[8] = OP_TIMECODE & 0xFF          # low byte first
    b[9] = (OP_TIMECODE >> 8) & 0xFF
    b[10] = (PROTOCOL_VERSION >> 8) & 0xFF
    b[11] = PROTOCOL_VERSION & 0xFF
    b[12] = 0                          # Filler1
    b[13] = stream_id
    b[14] = f
    b[15] = s
    b[16] = m
    b[17] = h
    b[18] = tc_type
    return bytes(b)


def _looks_broadcast(ip):
    # The pixel sender's rule, so both outputs agree on what a broadcast is.
    from .output import Sender
    return Sender.looks_broadcast(ip)


class TimecodeOut:
    """The Art-Net timecode socket. Never raises into the clock thread.

    Its own socket, apart from the pixel sender, for the reason the scene
    trigger has its own: one should never be able to take the other down.
    Broadcast is switched on only when the show file asks for broadcast,
    the same rule the pixel sender follows. Three sends in a row that reach
    nobody close the socket, and the next send opens a fresh one, no faster
    than once a second: the same self-healing the pixel sender does."""

    FAILURES_BEFORE_REOPEN = 3
    REOPEN_BACKOFF_S = 1.0
    # One line per receiver per this many seconds while it keeps failing, so
    # one dead node out of several is named in the log without burying it.
    DEST_LOG_EVERY_S = 5.0

    def __init__(self, dests, broadcast=False, port=ARTNET_PORT, log=None,
                 socket_factory=None, clock=time.monotonic, bind_ip=None):
        self.dests = list(dests)            # [(label, ip)]
        self.broadcast = bool(broadcast)
        self.port = port
        self.bind_ip = bind_ip
        self.log = log
        # Per receiver: [failures since it last took a packet, last error,
        # when it was last logged]. A receiver is in here only while failing.
        self.failing = {}
        self._factory = socket_factory or self._default_socket
        self._clock = clock
        self._sock = None
        self._fails = 0
        self._last_open = None
        self.packets_sent = 0
        self.send_errors = 0
        self.reopens = 0
        self.last_error = ""
        self.last_error_at = None
        self.last_ok_at = None

    def _default_socket(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            if self.broadcast:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            if self.bind_ip:
                # The same rule as the pixel sender's --bind: pick the
                # interface, keep an ephemeral port.
                s.bind((self.bind_ip, 0))
        except OSError:
            s.close()
            raise
        return s

    def _ensure(self, now):
        if self._sock is not None:
            return self._sock
        if self._last_open is not None and \
                now - self._last_open < self.REOPEN_BACKOFF_S:
            return None
        self._last_open = now
        try:
            self._sock = self._factory()
        except OSError as e:
            self._fail(now, f"open: {e}")
            return None
        if self.packets_sent or self.send_errors:
            self.reopens += 1
        return self._sock

    def _fail(self, now, msg):
        self.send_errors += 1
        self.last_error = msg
        self.last_error_at = now

    def _log(self, msg):
        if self.log:
            try:
                self.log.event("timecode", msg)
            except Exception:
                pass

    def _dest_failed(self, now, label, ip, e):
        msg = f"timecode to {label} ({ip}): {e}"
        self._fail(now, msg)
        f = self.failing.get(ip)
        if f is None:
            f = self.failing[ip] = [0, "", None]
        f[0] += 1
        f[1] = msg
        if f[2] is None or now - f[2] >= self.DEST_LOG_EVERY_S:
            f[2] = now
            self._log(f"{label} ({ip}) is not taking Art-Net timecode, "
                      f"{f[0]} packet(s) refused so far: {e}")

    def _dest_ok(self, label, ip):
        f = self.failing.pop(ip, None)
        if f is not None:
            self._log(f"{label} ({ip}) is taking Art-Net timecode again "
                      f"after {f[0]} refused packet(s)")

    @property
    def failing_labels(self):
        return [f"{label} ({ip})" for label, ip in self.dests
                if ip in self.failing]

    @property
    def seconds_since_error(self):
        if self.last_error_at is None:
            return None
        return self._clock() - self.last_error_at

    def send(self, pkt):
        now = self._clock()
        sock = self._ensure(now)
        if sock is None:
            return False
        ok = False
        for label, ip in self.dests:
            try:
                sock.sendto(pkt, (ip, self.port))
                self.packets_sent += 1
                ok = True
                if self.failing:
                    self._dest_ok(label, ip)
            except OSError as e:
                self._dest_failed(now, label, ip, e)
        if ok:
            self._fails = 0
            self.last_ok_at = now
            return True
        self._fails += 1
        if self._fails >= self.FAILURES_BEFORE_REOPEN:
            self._fails = 0
            self.close()
            self._log(f"{self.last_error}; rebuilding the timecode socket")
        return False

    def close(self):
        s, self._sock = self._sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

    @property
    def seconds_since_ok(self):
        if self.last_ok_at is None:
            return None
        return self._clock() - self.last_ok_at


# -------------------------------------------------------------- pacing ----
def frame_at(elapsed, fps):
    """Which frame is current `elapsed` seconds after frame 0 began.

    The epsilon keeps a clock reading of exactly t0 + n/fps on frame n rather
    than n - 1, which float division would otherwise do about half the time."""
    if elapsed <= 0:
        return 0
    return int(math.floor(elapsed * fps + 1e-9))


class Ticker:
    """Calls tick(n, now) once per frame, paced by absolute deadlines.

    Frame n is due at t0 + n/fps, computed fresh from t0 every time, so a
    late wake never moves any later deadline and sleep error cannot
    accumulate. The frame sent is always the one that is current when the
    thread wakes, never the one that was due:

      late by less than a frame   the due frame is still current and goes
                                  out late. Nothing is lost.
      late past a frame boundary  the missed frames are skipped and counted,
                                  and the current one goes out. Never a burst.

    Why skip rather than catch up: a timecode receiver treats each packet as
    "the time is now X" and freewheels between packets. MadMapper and BEYOND
    both chase the value, they do not count packets. A burst of overdue
    frames tells them the time a few frames ago, several times, in the same
    millisecond, which is a backward jump followed by a stutter. A skipped
    frame reads as the one frame step it really is.

    `clock` is time.perf_counter, not time.monotonic. Both are monotonic and
    neither is the wall clock, but under Python 3.12 on Windows monotonic
    ticks every 15.6 ms, which is half a frame, and perf_counter is the
    high resolution counter on every platform. player.py's chase engine
    keeps its own time on the same call, player._now(), for the same
    reason -- see its module docstring."""

    MAX_SLEEP_S = 0.05      # so stop() is noticed within a twentieth second

    def __init__(self, fps, tick, clock=time.perf_counter, sleep=time.sleep,
                 name="ltcplay-timecode", log=None):
        self.fps = float(fps)
        self.tick = tick
        self._clock = clock
        self._sleep = sleep
        self.name = name
        self.log = log
        self._stop = threading.Event()
        self._thread = None
        self.t0 = None
        self.ticks = 0
        self.skipped = 0
        self.errors = 0
        self.last_error = ""

    def run(self, t0, stop=None, n0=0):
        """The loop itself. Runs on the caller's thread; start() runs it on
        its own. Returns when tick() returns False or stop() is called.

        The stop flag is this run's own, so a thread that outlives a join
        still sees the stop meant for it, not the next run's fresh flag.

        `n0` is the frame number this run already considers itself to be
        at, before the first tick. Play() begins a cue at 0, the default.
        Resume() begins one already in progress: it moves `t0` back by the
        frozen frame's own length so the very first frame computed from it
        lands on frame_frozen + 1, and n0 is what tells that apart from a
        clock that is simply, legitimately late. Without it the first
        iteration below sees `n_next` still at 0 and `n` already at
        frame_frozen + 1, and counts the whole paused span as skipped --
        a real bug, found on the Fire & Ice bench 2026-09-25: 600-ish
        skipped frames appearing out of nowhere on every Hold/Resume, none
        of them real, because the receiver saw nothing skipped at all."""
        self.t0 = t0
        stop = self._stop if stop is None else stop
        fps = self.fps
        n_next = n0
        clock, sleep = self._clock, self._sleep
        while not stop.is_set():
            due = t0 + n_next / fps
            now = clock()
            if now < due:
                sleep(min(due - now, self.MAX_SLEEP_S))
                continue
            # Never below the frame that was due. At a large clock reading,
            # t0 + n/fps and back again can round to a hair under n, which
            # would send frame n - 1 a second time.
            n = max(frame_at(now - t0, fps), n_next)
            if n > n_next:
                self.skipped += n - n_next
            self.ticks += 1
            try:
                more = self.tick(n, now)
            except Exception as e:
                # Nothing raised in a tick may end the clock. A dead clock
                # thread is a show that stops with nothing on screen saying so.
                more = True
                self.errors += 1
                self.last_error = f"{type(e).__name__}: {e}"
                if self.log:
                    try:
                        self.log.event("clock-error", f"tick failed: "
                                                      f"{self.last_error}",
                                       throttle_s=5.0)
                    except Exception:
                        pass
            if more is False:
                return
            n_next = n + 1

    def start(self, t0=None, n0=0):
        self.stop()
        t0 = self._clock() if t0 is None else t0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self.run,
                                        args=(t0, self._stop, n0),
                                        name=self.name, daemon=True)
        self._thread.start()
        return t0

    def stop(self):
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread() \
                and t.is_alive():
            t.join(timeout=1.0)
        self._thread = None

    @property
    def running(self):
        t = self._thread
        return bool(t and t.is_alive())


# --------------------------------------------------------------- zones ----
def route(h, m, s, f, zones):
    """Which zone a frame of MadMapper's timecode is in, and where in it.

    `zones` maps an hour to a role, from the show file: {1: "show",
    2: "intermission"} for Fire & Ice. Returns (role, (0, m, s, f)), the
    position rebased to hour zero so BEYOND sees the same numbers whichever
    machine is master, or (IDLE, None) for any hour the table does not name.
    Pure: no clock, no state, no I/O."""
    role = zones.get(h)
    if role is None:
        return IDLE, None
    return role, (0, m, s, f)


class ZoneReader:
    """Decoded LTC frames in, the current zone and position out.

    Three rules on top of route(), the first two borrowed from the chase
    engine:

      A change has to be said twice. LTC has no checksum; one flipped bit in
      the hours reads as a valid frame in another zone. A frame that
      disagrees with where the reader is, in zone or by more than a few
      frames in position, moves it only when the next frame agrees with it.
      That holds after a gap too, and for the very first frame: a feed that
      comes back on a corrupt frame must not be believed on that one frame.

      The position is kept as an epoch, the moment its zone's frame zero
      began, and small differences are slewed rather than taken. Frame
      stamps arrive with the jitter of the audio callback itself -- real
      buffer-delivery jitter, not a clock resolution problem, so slewing
      still earns its keep even now that every stamp on the way here is
      read from player._now() -- and taking each one raw makes the
      forwarded frames step 0, 1 or 2 instead of 1.

      Timecode loss during the show zone free runs to the end of the show.
      Past the show length nothing is ever invented: a frame there goes out
      only while the feed is fresh (its last frame under FRESH_FRAMES old)
      AND that last frame was itself past the end, so MadMapper really is
      still sending. A feed that stops on the last frame, or hands over to
      the intermission, gets no made-up 07:20:00. Any other zone stops
      after `hold_s` with no frames, and the receivers hold and then time
      out on their own."""

    JUMP_FRAMES = 5         # about the chase engine's 0.15 s jump threshold
    CONFIRM_FRAMES = 2
    FRESH_FRAMES = 2
    SLEW = 0.1

    def __init__(self, zones, count=30, drop=False, fps=30.0,
                 forward=("show",), show_len_s=None, hold_s=1.0):
        self.zones = dict(zones)
        self.count = count
        self.drop = drop
        self.fps = float(fps)
        self.forward = tuple(forward)
        self.show_len_frames = (None if show_len_s is None
                                else int(math.ceil(show_len_s * self.fps
                                                   - 1e-9)))
        self.hold_s = hold_s
        self._lock = threading.Lock()
        # (role, epoch or None, time of the last agreeing frame, its
        # position as received)
        self._last = None
        self._pending = None        # (role, epoch or None, at)
        self.frames_in = 0
        self.rejects = 0
        self.zone_changes = 0
        self.freerunning = False
        self.show_over = False

    def _pos(self, rel):
        if rel is None:
            return None
        _, m, s, f = rel
        return tc_to_frames(0, m, s, f, self.count, self.drop)

    def _agree(self, a_role, a_epoch, b_role, b_epoch, slack):
        if a_role != b_role:
            return False
        if a_epoch is None or b_epoch is None:
            return a_epoch is None and b_epoch is None
        return abs(a_epoch - b_epoch) * self.fps <= slack

    def frame(self, h, m, s, f, at):
        """One decoded frame, captured at `at` on the reader's clock."""
        role, rel = route(h, m, s, f, self.zones)
        pos = self._pos(rel)
        epoch = None if pos is None else at - pos / self.fps
        with self._lock:
            self.frames_in += 1
            last = self._last
            if last is not None and self._agree(role, epoch, last[0], last[1],
                                                self.JUMP_FRAMES):
                self._pending = None
                if epoch is not None:
                    epoch = last[1] + (epoch - last[1]) * self.SLEW
                self._last = (role, epoch, at, pos)
                self.show_over = False
                return role
            p = self._pending
            if p is not None and self._agree(role, epoch, p[0], p[1],
                                             self.CONFIRM_FRAMES):
                if last is None or last[0] != role:
                    self.zone_changes += 1
                self._pending = None
                self._last = (role, epoch, at, pos)
                self.show_over = False
                return role
            if p is not None:
                self.rejects += 1
            self._pending = (role, epoch, at)
            return last[0] if last is not None else None

    def position(self, now):
        """(role, frames into the zone as a float) to send now, or None."""
        with self._lock:
            last = self._last
        if last is None:
            self.freerunning = False
            return None
        role, epoch, t, got = last
        if role not in self.forward or epoch is None:
            self.freerunning = False
            return None
        age = now - t
        cur = (now - epoch) * self.fps
        if role == "show":
            self.freerunning = age > self.hold_s
            end = self.show_len_frames
            if end is not None and cur >= end - 1e-9:
                live = (age * self.fps <= self.FRESH_FRAMES
                        and got is not None and got >= end)
                if not live:
                    self.show_over = True
                    self.freerunning = False
                    return None
        elif age > self.hold_s:
            return None
        return role, cur

    def tc(self, frames):
        h, m, s, f = frames_to_tc(frames, self.count, self.drop)
        return (h % 24, m, s, f)

    def at(self, now):
        """(role, (h, m, s, f)) to send now, or None to send nothing."""
        got = self.position(now)
        if got is None:
            return None
        role, cur = got
        return role, self.tc(int(math.floor(cur + 1e-9)))

    @property
    def zone(self):
        last = self._last
        return last[0] if last else None


# -------------------------------------------------------------- config ----
def _obj(v, where, what):
    if not isinstance(v, dict):
        raise ClockConfigError(f"{where}: {what} must be an object like "
                               f"{{...}}")
    return v


def _no_typos(doc, keys, where, what):
    # The same rule as the rest of the show file: a setting spelled wrong is
    # worse than one missing, because the file loads and the thing asked for
    # silently does not happen.
    unknown = sorted(k for k in doc if k not in keys)
    if unknown:
        raise ClockConfigError(
            f"{where}: {what} has no setting "
            f"{', '.join(repr(k) for k in unknown)}; it takes: "
            f"{', '.join(sorted(keys))}.")


def _ipv4(v):
    try:
        return str(ipaddress.IPv4Address(str(v).strip()))
    except (ipaddress.AddressValueError, ValueError):
        return None


class ArtNetConfig:
    """Where the Art-Net timecode goes: named nodes, or one broadcast.

    Unicast to named nodes is the default and the rule the sACN traffic
    follows. Broadcast is there as a setting because Art-Net's own guidance
    is that a single timecode source broadcasts, but it has to be asked for
    by address; it is never switched on by accident."""

    KEYS = frozenset(("nodes", "broadcast", "stream_id"))

    def __init__(self, nodes=None, broadcast=None, stream_id=0):
        self.nodes = dict(nodes or {})
        self.broadcast = broadcast
        self.stream_id = stream_id

    @property
    def dests(self):
        if self.broadcast:
            return [("broadcast", self.broadcast)]
        return list(self.nodes.items())

    def summary(self):
        if self.broadcast:
            return f"broadcast to {self.broadcast}"
        return ", ".join(f"{k} {v}" for k, v in self.nodes.items())

    @classmethod
    def parse(cls, doc, where):
        what = "'clock.artnet'"
        _obj(doc, where, what)
        _no_typos(doc, cls.KEYS, where, what)
        nodes = doc.get("nodes", {})
        if not isinstance(nodes, dict):
            raise ClockConfigError(
                f"{where}: 'clock.artnet.nodes' names each receiver and its "
                f"address, like {{\"MadMapper\": \"127.0.0.1\", "
                f"\"BEYOND\": \"10.0.0.40\"}}")
        clean, seen = {}, {}
        for name, ip in nodes.items():
            if not isinstance(name, str) or not name.strip():
                raise ClockConfigError(f"{where}: every node in "
                                       f"'clock.artnet.nodes' needs a name")
            addr = _ipv4(ip) if isinstance(ip, str) else None
            if addr is None:
                raise ClockConfigError(
                    f"{where}: 'clock.artnet.nodes' gives {name!r} the "
                    f"address {ip!r}, which is not an IPv4 address")
            if _looks_broadcast(addr):
                raise ClockConfigError(
                    f"{where}: {name!r} is {addr}, which is a broadcast "
                    f"address. Put it in 'clock.artnet.broadcast' instead, "
                    f"so broadcast is something the show file says out loud.")
            if addr in seen:
                raise ClockConfigError(
                    f"{where}: {seen[addr]!r} and {name!r} are both {addr}, "
                    f"so that receiver would get every frame twice")
            seen[addr] = name
            clean[name.strip()] = addr
        bc = doc.get("broadcast")
        if bc is not None:
            addr = _ipv4(bc) if isinstance(bc, str) else None
            if addr is None or not _looks_broadcast(addr):
                raise ClockConfigError(
                    f"{where}: 'clock.artnet.broadcast' is {bc!r}. It is the "
                    f"broadcast address of the show network, like "
                    f"10.0.0.255, or leave it out and list the nodes.")
            if clean:
                raise ClockConfigError(
                    f"{where}: 'clock.artnet' has both 'broadcast' and "
                    f"'nodes'. Broadcast already reaches every node, so each "
                    f"named one would get every frame twice. Pick one.")
            bc = addr
        if not clean and not bc:
            raise ClockConfigError(
                f"{where}: 'clock.artnet' sends to nobody. List the "
                f"receivers under 'nodes', or give a 'broadcast' address.")
        sid = doc.get("stream_id", 0)
        if not isinstance(sid, int) or isinstance(sid, bool) \
                or not 0 <= sid <= 255:
            raise ClockConfigError(f"{where}: 'clock.artnet.stream_id' is 0 "
                                   f"to 255, and 0 is the master stream")
        return cls(clean, bc, sid)


class ZoneConfig:
    """The hour zone table for fallback 3, when MadMapper owns the clock.

    Only read by the LTC slave. It may sit in a show file that uses another
    source, so switching source is one edit, not two."""

    KEYS = frozenset(("show", "intermission", "forward", "show_len_s",
                      "hold_ms"))

    def __init__(self, show=1, intermission=2, forward=("show",),
                 show_len_s=None, hold_ms=1000):
        self.show = show
        self.intermission = intermission
        self.forward = tuple(forward)
        self.show_len_s = show_len_s
        self.hold_ms = hold_ms

    @property
    def table(self):
        return {self.show: "show", self.intermission: "intermission"}

    @classmethod
    def parse(cls, doc, where):
        what = "'clock.zones'"
        _obj(doc, where, what)
        _no_typos(doc, cls.KEYS, where, what)
        hours = {}
        for role, default in (("show", 1), ("intermission", 2)):
            v = doc.get(role, default)
            if not isinstance(v, int) or isinstance(v, bool) \
                    or not 0 <= v <= 23:
                raise ClockConfigError(f"{where}: 'clock.zones.{role}' is "
                                       f"the timecode hour for the {role}, "
                                       f"0 to 23")
            hours[role] = v
        if hours["show"] == hours["intermission"]:
            raise ClockConfigError(
                f"{where}: 'clock.zones' puts the show and the intermission "
                f"both in hour {hours['show']}, so nothing could tell them "
                f"apart")
        fwd = doc.get("forward", ["show"])
        if isinstance(fwd, str):
            fwd = [fwd]
        if not isinstance(fwd, (list, tuple)) or \
                any(x not in ROLES for x in fwd):
            raise ClockConfigError(
                f"{where}: 'clock.zones.forward' lists which zones go out as "
                f"Art-Net timecode: \"show\", \"intermission\", or both")
        sl = doc.get("show_len_s")
        if sl is not None and (not isinstance(sl, (int, float))
                               or isinstance(sl, bool) or sl <= 0):
            raise ClockConfigError(f"{where}: 'clock.zones.show_len_s' is "
                                   f"the show length in seconds")
        hold = doc.get("hold_ms", 1000)
        if not isinstance(hold, (int, float)) or isinstance(hold, bool) \
                or not 100 <= hold <= 10000:
            raise ClockConfigError(
                f"{where}: 'clock.zones.hold_ms' is how long a zone other "
                f"than the show keeps going with no timecode, 100 to 10000 "
                f"milliseconds")
        return cls(hours["show"], hours["intermission"], tuple(fwd), sl,
                   hold)


class ClockConfig:
    """The "clock" block of a show file, validated."""

    KEYS = frozenset(("source", "show_audio", "artnet", "zones", "notes"))

    def __init__(self, source, show_audio="madmapper", artnet=None,
                 zones=None):
        self.source = source
        self.show_audio = show_audio
        self.artnet = artnet
        self.zones = zones or ZoneConfig()

    @classmethod
    def parse(cls, doc, where="timeline"):
        # A "clock": null is refused like any other malformed block. It was
        # refused before this block existed, as an unknown key, and a show
        # file that says "clock" and means nothing is a mistake to name.
        what = "'clock'"
        _obj(doc, where, what)
        _no_typos(doc, cls.KEYS, where, what)
        src = doc.get("source")
        if src not in SOURCES:
            raise ClockConfigError(
                f"{where}: 'clock.source' is {src!r}; it must be one of "
                f"{', '.join(SOURCES)}.")
        if src == "ltc_audio_master":
            raise ClockConfigError(LtcAudioMaster.REFUSAL.format(where=where))
        audio = doc.get("show_audio", "madmapper")
        if audio not in SHOW_AUDIO:
            raise ClockConfigError(
                f"{where}: 'clock.show_audio' is {audio!r}; it must be "
                f"\"madmapper\" or \"ltcplay\".")
        if audio == "ltcplay":
            raise ClockConfigError(
                f"{where}: 'clock.show_audio' is \"ltcplay\", which is "
                f"fallback 2: show audio played by this program instead of "
                f"MadMapper. This build does not have a show audio player "
                f"yet, so it cannot run. Set it to \"madmapper\".")
        art = doc.get("artnet")
        art = None if art is None else ArtNetConfig.parse(art, where)
        if src == "artnet_master" and art is None:
            raise ClockConfigError(
                f"{where}: 'clock.source' is \"artnet_master\" but there is "
                f"no 'clock.artnet' block, so the timecode would go nowhere. "
                f"Name the receivers under 'clock.artnet.nodes'.")
        zones = doc.get("zones")
        zones = ZoneConfig() if zones is None else ZoneConfig.parse(zones,
                                                                    where)
        return cls(src, audio, art, zones)

    def summary(self):
        s = self.source
        if self.artnet is not None:
            s += f", Art-Net timecode to {self.artnet.summary()}"
        return s


# -------------------------------------------------------------- clocks ----
class Clock:
    """What every clock source looks like to the session.

    sink       Player.feed_timecode, the one position stream.
    on_stop    masters only: called when the clock stops a cue (end, halt,
               Stop). The session hands the pixels back to the idle look,
               never through on_lost.
    master     True when this machine makes the position, so no timecode
               input is opened at all.
    start()    called when the operator presses Run. Sends nothing by
               itself: a master waits for play(), a slave for timecode.
    stop()     called on Stop, before the blackout.
    ltc_frame  each decoded LTC frame, from the audio thread. Masters ignore.
    play()     masters only: run a cue from 00:00:00:00.
    halt()     masters only: stop the clock now.
    pause()    masters only: freeze on the current frame, still sending it.
    resume()   masters only: carry on from exactly the frozen frame."""

    source = ""
    master = False
    ticker = None
    out = None

    def start(self):
        pass

    def stop(self):
        pass

    def ltc_frame(self, h, m, s, f, captured_at):
        pass

    def play(self, position_s=0.0, length_s=None, label=""):
        raise ClockConfigError("This show follows incoming timecode, so this "
                               "machine cannot start the clock.")

    def halt(self):
        pass

    def pause(self):
        raise ClockConfigError("This show follows incoming timecode, so "
                               "this machine cannot pause the clock.")

    def resume(self):
        raise ClockConfigError("This show follows incoming timecode, so "
                               "this machine cannot resume the clock.")

    def snapshot(self):
        return {"source": self.source, "master": self.master}


def _out_snapshot(out, ticker):
    d = {"tick_errors": ticker.errors if ticker else 0,
         "tick_error": ticker.last_error if ticker else ""}
    if out is None:
        d["artnet"] = None
        return d
    d.update({"artnet": [f"{k} {v}" for k, v in out.dests],
              "packets": out.packets_sent, "send_errors": out.send_errors,
              "since_ok": out.seconds_since_ok,
              "since_error": getattr(out, "seconds_since_error", None),
              "failing": list(getattr(out, "failing_labels", [])),
              "last_error": out.last_error})
    return d


class ArtNetMaster(Clock):
    """This machine is the clock. Art-Net timecode out, pixels follow it.

    The clock owns the pixel position outright. When a cue ends or is
    halted, on_stop hands the pixels straight back to the idle look; the
    chase engine never sees that as lost timecode, so on_lost (a policy for
    a feed that dies) never runs the rest of the show file on its own.

    play() and halt() are locked: the scheduler and the web server's threads
    will both call them."""

    source = "artnet_master"
    master = True

    def __init__(self, cfg, sink=None, out=None, clock=time.perf_counter,
                 sleep=time.sleep, mono=time.perf_counter, log=None,
                 on_stop=None, on_pause=None, on_resume=None):
        self.cfg = cfg
        self.sink = sink
        self.on_stop = on_stop
        # Told apart from on_stop: a Hold is not the clock stopping, it is
        # the clock telling the chase engine, in so many words, "this is a
        # real pause, not a hiccup" -- see _set_paused() below for why that
        # needs saying at all.
        self.on_pause = on_pause
        self.on_resume = on_resume
        self.out = out
        self._clock = clock
        self._mono = mono
        self.log = log
        self.stream_id = cfg.artnet.stream_id if cfg.artnet else 0
        self.ticker = Ticker(MASTER_FPS, self._tick, clock=clock,
                             sleep=sleep, log=log)
        self._lock = threading.RLock()
        self._live = False
        self._cue = None             # (position_s, length_frames, label)
        self._mono_t0 = None
        self.cues_played = 0
        self.last_sent = None
        self.last_ended = ""
        # Hold: frozen on one frame, the ticker still ticking so MadMapper
        # and BEYOND keep getting packets instead of timing out. Set by
        # pause(), cleared by resume(), play() and halt().
        self._paused = False
        self._frozen = None          # (h, m, s, f) while paused
        self._frozen_n = None        # the cue frame number pause() froze on
        self._frozen_pos = None      # the pixel position fed to the sink
                                     # every tick while paused
        self._frame_n = None         # the last real frame number sent
        # A test seam, nothing more: called with "pause" the instant
        # pause() has finished writing everything the paused branch of
        # _tick() reads, before it can be observed as paused. A no-op in
        # every real run. Tests use it to hold pause() right there and
        # let a real tick land in that exact window, proving -- not
        # assuming -- that the ordering above is what makes it safe.
        self._sync_point = lambda tag: None

    def start(self):
        with self._lock:
            self._live = True

    def stop(self):
        with self._lock:
            self._live = False
            self.halt()
            if self.out is not None:
                self.out.close()

    def play(self, position_s, length_s, label=""):
        """Run a cue: timecode from 00:00:00:00, pixels from `position_s`.

        `position_s` is where the cue sits in the show file, so one show
        file serves this master and the fallback 3 slave alike. A cue with
        no length is refused: its timecode would never stop."""
        if length_s is None or not float(length_s) > 0:
            raise ClockConfigError(
                f"{label or 'This cue'} has no length, so its timecode "
                f"would run forever. It only has one when its sequence "
                f"opened.")
        with self._lock:
            if not self._live:
                raise ClockConfigError("Nothing is running. Press Run first.")
            self.ticker.stop()
            frames = int(math.ceil(float(length_s) * MASTER_FPS - 1e-9))
            self._cue = (float(position_s), frames, label)
            self._set_paused(False)
            self._frozen = None
            self._frozen_n = None
            self._frozen_pos = None
            self._frame_n = None
            self.cues_played += 1
            self._event(f"timecode from 00:00:00:00 for {label or 'a cue'}")
            # Frame n began at t0 + n/30 on the pacing clock. `mono` and
            # `clock` default to the same call, time.perf_counter -- the
            # same one player._now() makes -- so this translation is an
            # identity to within the cost of one extra call, but it stays
            # written as a translation: a caller can still hand this a
            # different `mono`, and reading either clock once here and
            # counting from it, rather than reading it every frame, is what
            # keeps the pixels off its steps -- 15.6ms on time.monotonic
            # under Python 3.12 on Windows, if that is ever what `mono` is.
            t0 = self._clock()
            self._mono_t0 = self._mono() - (self._clock() - t0)
            return self.ticker.start(t0)

    def halt(self):
        with self._lock:
            was = self._cue
            self.ticker.stop()
            self._cue = None
            self._set_paused(False)
            self._frozen = None
            self._frozen_n = None
            self._frozen_pos = None
            self._frame_n = None
            if was is not None:
                self.last_ended = f"{was[2] or 'cue'} stopped"
                self._event(f"timecode stopped for {was[2] or 'a cue'}")
                self._stopped()

    def pause(self):
        """Freeze the clock on its current frame.

        It keeps sending that one frame every tick, so MadMapper and BEYOND
        hold instead of timing out on a dead stream, and the pixels are fed
        the same repeated position: the chase engine reads a repeated
        position as PARKED, not lost, and holds too. Master only, and only
        while a cue is actually playing. Locked with play() and halt(): the
        scheduler and the web server's threads both call these."""
        with self._lock:
            if self._cue is None:
                raise ClockConfigError("Nothing is playing to pause.")
            if self._paused:
                raise ClockConfigError("The clock is already paused.")
            position_s, _frames, label = self._cue
            n = self._frame_n if self._frame_n is not None else 0
            h, m, s, f = frames_to_tc(n, MASTER_FPS)
            # Order matters. _tick() runs on the ticker thread and never
            # takes this lock (see resume()'s note on why not), so it can
            # read these fields the instant any one of them changes.
            # Everything the paused branch reads has to be in place BEFORE
            # _paused flips to True, or a tick landing in the gap sees
            # paused=True with the frozen frame still unset (or, on a
            # second pause, still the previous one) and throws trying to
            # unpack it -- silently dropping that one tick, the one thing
            # this feature promises never happens. CPython's GIL makes a
            # single attribute write atomic and preserves the order one
            # thread issues its writes in, so setting _paused last, after
            # everything it implies is already true, is enough on its own.
            self._frozen = (h, m, s, f)
            self._frozen_n = n
            self._frozen_pos = position_s + n / MASTER_FPS
            self.last_sent = (h, m, s, f)
            self._set_paused(True)
            self._sync_point("pause")
            self._event(f"paused at {h:02d}:{m:02d}:{s:02d}:{f:02d} for "
                        f"{label or 'the cue'}")

    def resume(self):
        """Carry on from exactly the frame pause() froze.

        Restarts the ticker with its zero point moved back by the frozen
        frame, so the very next frame sent is the frozen one plus one: no
        jump, no repeated burst, no skipped frame. The cue runs that much
        longer than it would have run with no pause, because the frames
        spent paused were never counted against its length."""
        with self._lock:
            if not self._paused:
                raise ClockConfigError("The clock is not paused, so there "
                                       "is nothing to resume.")
            if self._cue is None:
                raise ClockConfigError("Nothing is playing to resume.")
            # Stop the ticker BEFORE touching anything _tick() reads: the
            # same shape play() and halt() already use, and for the same
            # reason. _tick() never takes this lock, so the ticker thread
            # is still alive and still calling it every frame right up
            # until stop() joins it -- that is the whole mechanism, the
            # same thread has to keep ticking through the pause so the
            # frozen frame keeps going out. Its own frame count kept
            # climbing at 30 a second the entire time regardless, since
            # pause() never stops it either. Clear _paused/_frozen first
            # and a tick that lands before the join catches up would take
            # the UNPAUSED branch with that huge, long-stale frame number,
            # see it well past the cue's length, end the cue and fire
            # on_stop -- silently, with resume() itself returning as if
            # nothing had gone wrong. Stopping first means any tick still
            # in flight runs against the state that was true when it was
            # scheduled: still paused, still frozen, harmless.
            self.ticker.stop()
            label = self._cue[2]
            n_frozen = self._frozen_n
            self._set_paused(False)
            self._frozen = None
            self._frozen_n = None
            self._frozen_pos = None
            t0 = self._clock() - (n_frozen + 1) / MASTER_FPS
            self._mono_t0 = self._mono() - (self._clock() - t0)
            self._event(f"resumed for {label or 'the cue'}")
            # n0 tells the ticker it is already at frame_frozen + 1, not
            # starting fresh at 0: see Ticker.run()'s docstring. Without it
            # the backdated t0 above reads as the ticker having fallen
            # frame_frozen + 1 frames behind on its very first iteration,
            # and counts the whole hold as skipped -- see the bug this
            # fixes, named in Ticker.run()'s docstring.
            return self.ticker.start(t0, n0=n_frozen + 1)

    @property
    def paused(self):
        return self._paused

    def _set_paused(self, active):
        """Flip _paused and tell the player, the one place both happen, so
        the two can never drift apart. pause(), resume(), a fresh play()
        starting over an old pause, and halt() while paused all go through
        here.

        Why the player needs telling at all: Player.feed_timecode() already
        reads a repeated position as PARKED, immediately -- but the output
        thread only trusts that reading once the same value has held for
        `park_s` (its debounce against a real LTC deck's decode noise,
        never a machine-generated clock's concern). For up to that long
        after every Hold, the pixels kept computing their position from the
        old, running clock instead of the frozen one, drifted forward, and
        then snapped back the moment the debounce caught up -- one pixel
        frame sent out of order on nearly every Hold, on the Fire & Ice
        bench 2026-09-25. This tells the player, without ambiguity and
        without waiting, that this is a real pause; the debounce itself is
        untouched, so a real LTC deck (GPL/Dollywood) is unaffected."""
        if self._paused == active:
            return
        self._paused = active
        cb = self.on_pause if active else self.on_resume
        if cb is not None:
            try:
                cb()
            except Exception as e:
                self._event(f"telling the pixels the clock "
                            f"{'paused' if active else 'resumed'} failed: "
                            f"{e}")

    def _stopped(self):
        if self.on_stop is not None:
            try:
                self.on_stop()
            except Exception as e:
                self._event(f"handing the pixels back failed: {e}")

    def _event(self, msg):
        if self.log:
            try:
                self.log.event("clock", msg)
            except Exception:
                pass

    def _tick(self, n, now):
        # Deliberately no lock here. This runs on the ticker's own thread,
        # every frame, and pause()/play()/halt()/resume() all call
        # ticker.stop() at some point while holding self._lock; stop()
        # joins this very thread, so if _tick() ever waited on the same
        # lock, a stop() call could sit blocked on a tick that is itself
        # blocked waiting for the lock stop() is holding. Safety against
        # pause()/resume() comes from their own field ordering instead
        # (see the comments in each), not from serializing this.
        cue = self._cue
        if cue is None:
            return False
        if self._paused:
            # Ignore the ticker's own frame count entirely: the cue is
            # frozen, so whatever frame is "due" by the wall clock is not
            # the one to send. Send the frozen one again, forever, until
            # resume() restarts the ticker with a corrected zero point.
            h, m, s, f = self._frozen
            if self.out is not None:
                self.out.send(arttimecode(h, m, s, f, MASTER_TYPE,
                                          self.stream_id))
            self.last_sent = (h, m, s, f)
            if self.sink is not None:
                self.sink(self._frozen_pos, self._mono(), False,
                          f"{h:02d}:{m:02d}:{s:02d}:{f:02d}")
            return True
        position_s, frames, label = cue
        if n >= frames:
            # The cue has run its length. Nothing is playing, so nothing is
            # sent: receivers hold and then time out on their own, and the
            # pixels go back to the idle look now.
            if self._cue is cue:
                self._cue = None
                self.last_ended = f"{label or 'cue'} finished"
                self._event(f"timecode ended with {label or 'the cue'}")
                self._stopped()
            return False
        h, m, s, f = frames_to_tc(n, MASTER_FPS)
        if self.out is not None:
            self.out.send(arttimecode(h, m, s, f, MASTER_TYPE,
                                      self.stream_id))
        self.last_sent = (h, m, s, f)
        self._frame_n = n
        if self.sink is not None:
            self.sink(position_s + n / MASTER_FPS,
                      self._mono_t0 + n / MASTER_FPS, False,
                      f"{h:02d}:{m:02d}:{s:02d}:{f:02d}")
        return True

    @property
    def playing(self):
        return self._cue is not None

    def snapshot(self):
        d = super().snapshot()
        lt = self.last_sent
        cue = self._cue
        d.update({"playing": cue[2] if cue else None,
                  "paused": self._paused,
                  "sending": (f"{lt[0]:02d}:{lt[1]:02d}:{lt[2]:02d}:"
                              f"{lt[3]:02d}" if lt and cue else None),
                  "skipped": self.ticker.skipped,
                  "last_ended": self.last_ended})
        d.update(_out_snapshot(self.out, self.ticker))
        return d


class LtcAudioSlave(Clock):
    """Today's LTC path, with a tap. Fallback 3 of the Fire & Ice handoff.

    The chase engine keeps reading the decoded frames exactly as it always
    has; this only hears them too. It reads the hour as a zone and, when
    the show file names Art-Net receivers, forwards the zones it is asked to
    as Art-Net timecode, rebased to hour zero, at the timeline's own rate.

    Forwarded frames step by exactly one per tick. Each tick carries on
    from the last frame sent, and only resyncs to the reader when the frame
    it would send strays more than FLYWHEEL_FRAMES from the middle of the
    frame the reader says is current: a real jump, a zone change or a
    genuine drift, never stamp jitter. The sent frame is then never more
    than half a frame ahead of the reader, nor one and a half behind."""

    source = "ltc_audio_slave"
    master = False
    FLYWHEEL_FRAMES = 1.0

    def __init__(self, cfg, count=30, drop=False, fps=30.0, show_len_s=None,
                 out=None, clock=time.perf_counter, sleep=time.sleep,
                 mono=time.perf_counter, log=None):
        self.cfg = cfg
        self.out = out
        self._clock = clock
        self._mono = mono
        self.log = log
        z = cfg.zones
        self.reader = ZoneReader(z.table, count=count, drop=drop, fps=fps,
                                 forward=z.forward, show_len_s=show_len_s,
                                 hold_s=z.hold_ms / 1000.0)
        self.fps = float(fps)
        self.tc_type = type_for(count, drop)
        self.stream_id = cfg.artnet.stream_id if cfg.artnet else 0
        self.ticker = (Ticker(fps, self._tick, clock=clock, sleep=sleep,
                              log=log)
                       if cfg.artnet is not None else None)
        self.last_sent = None
        self._fly = None             # (role, frame sent, tick number)
        self._last_zone = None

    def start(self):
        if self.ticker is not None:
            self.ticker.start()

    def stop(self):
        if self.ticker is not None:
            self.ticker.stop()
        if self.out is not None:
            self.out.close()

    def ltc_frame(self, h, m, s, f, captured_at):
        # captured_at is on `mono`, the audio thread's clock -- the same
        # time.perf_counter() player._now() calls, by default, which is
        # what `clock` also defaults to, so this is a same-clock identity
        # unless a caller hands in two different ones. Translate once,
        # here, rather than assume: see the same note on
        # ArtNetMaster.play().
        at = self._clock() - (self._mono() - captured_at)
        zone = self.reader.frame(h, m, s, f, at)
        if zone != self._last_zone:
            if self._last_zone is not None and self.log:
                try:
                    self.log.event("clock", f"timecode zone {self._last_zone}"
                                            f" -> {zone} at hour {h:02d}")
                except Exception:
                    pass
            self._last_zone = zone

    def _tick(self, n, now):
        t0 = self.ticker.t0 if self.ticker is not None else None
        due = now if t0 is None else t0 + n / self.fps
        got = self.reader.position(due)
        if got is None:
            self._fly = None
            self.last_sent = None
            return True
        role, cur = got
        frame = None
        fly = self._fly
        if fly is not None and fly[0] == role:
            cand = fly[1] + (n - fly[2])
            if abs(cand - (cur - 0.5)) <= self.FLYWHEEL_FRAMES:
                frame = cand
        if frame is None:
            frame = int(math.floor(cur + 1e-9))
        if frame < 0:
            self._fly = None
            self.last_sent = None
            return True
        self._fly = (role, frame, n)
        h, m, s, f = self.reader.tc(frame)
        if self.out is not None:
            self.out.send(arttimecode(h, m, s, f, self.tc_type,
                                      self.stream_id))
        self.last_sent = (h, m, s, f)
        return True

    def snapshot(self):
        d = super().snapshot()
        r = self.reader
        lt = self.last_sent
        d.update({"zone": r.zone, "freerunning": r.freerunning,
                  "show_over": r.show_over, "zone_rejects": r.rejects,
                  "sending": (f"{lt[0]:02d}:{lt[1]:02d}:{lt[2]:02d}:"
                              f"{lt[3]:02d}" if lt else None)})
        d.update(_out_snapshot(self.out, self.ticker))
        return d


class LtcAudioMaster(Clock):
    """Fallback 1: LTC audio generated on a named output device. NOT BUILT.

    ltc.synthesize() makes LTC for the tests, but a real output needs an
    output device chosen by name, a channel shared with the announcements,
    and proof on the DSP that MadMapper reads it. That is its own change.
    Until it lands, naming this source is refused when the show file loads,
    never at showtime."""

    source = "ltc_audio_master"
    master = True
    REFUSAL = ("{where}: 'clock.source' is \"ltc_audio_master\", which is "
               "fallback 1: LTC audio generated on a named output device. "
               "This build does not have it yet, so it cannot run. Use "
               "\"artnet_master\", or \"ltc_audio_slave\" if MadMapper is "
               "the clock.")

    def __init__(self, *a, **kw):
        raise ClockConfigError(self.REFUSAL.format(where="clock"))


def build(cfg, timeline, sink, log=None, no_output=False, out=None,
          bind_ip=None, on_stop=None, on_pause=None, on_resume=None):
    """The clock a show file asks for, wired to the position stream."""
    if out is None and not no_output and cfg.artnet is not None:
        out = TimecodeOut(cfg.artnet.dests,
                          broadcast=bool(cfg.artnet.broadcast), log=log,
                          bind_ip=bind_ip)
    if cfg.source == "artnet_master":
        return ArtNetMaster(cfg, sink=sink, out=out, log=log, on_stop=on_stop,
                            on_pause=on_pause, on_resume=on_resume)
    if cfg.source == "ltc_audio_slave":
        show_len = cfg.zones.show_len_s
        if show_len is None and "show" in cfg.zones.forward \
                and cfg.artnet is not None:
            show_len = _show_length(timeline, cfg.zones.show)
        return LtcAudioSlave(cfg, count=timeline.count, drop=timeline.drop,
                             fps=timeline.fps, show_len_s=show_len, out=out,
                             log=log)
    if cfg.source == "ltc_audio_master":
        return LtcAudioMaster()
    raise ClockConfigError(f"no clock called {cfg.source!r}")


def _show_length(timeline, hour):
    """How long the show zone runs: to the end of its last cue.

    Free running to the end of the show needs to know where the end is.
    Read from the renders rather than typed in a second place."""
    start = hour * 3600.0
    end = None
    for c in timeline.cues:
        if c.tc_seconds is None or not start <= c.tc_seconds < start + 3600:
            continue
        if c.end_seconds is None:
            continue
        end = max(end or 0.0, c.end_seconds)
    if end is None:
        raise ClockConfigError(
            f"The show zone is hour {hour:02d}, but no cue in that hour "
            f"opened, so there is no way to know where the show ends and a "
            f"free run would never stop. Set 'clock.zones.show_len_s', or "
            f"put the show at {hour:02d}:00:00:00.")
    return end - start
