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
  AudioMaster      handoff section 4a (Jeff, 2026-09-27): this machine plays
                   the show's multi-track audio itself, in its own process
                   (showaudio.py), and the timecode is read off the audio
                   device's playback position. Art-Net timecode out and the
                   pixels follow it exactly as they follow ArtNetMaster.
                   showaudio.py is imported only for this source.

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

SOURCES = ("artnet_master", "ltc_audio_master", "ltc_audio_slave",
           "audio_master")
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
                 zones=None, audio=None):
        self.source = source
        self.show_audio = show_audio
        self.artnet = artnet
        self.zones = zones or ZoneConfig()
        # showaudio.AudioConfig, only for "audio_master".
        self.audio = audio

    @classmethod
    def parse(cls, doc, where="timeline"):
        # A "clock": null is refused like any other malformed block. It was
        # refused before this block existed, as an unknown key, and a show
        # file that says "clock" and means nothing is a mistake to name.
        what = "'clock'"
        _obj(doc, where, what)
        # "audio" is a setting only audio_master has. Any other source reads
        # it as the typo it would be, in exactly the words it always did.
        keys = cls.KEYS | {"audio"} if doc.get("source") == "audio_master" \
            else cls.KEYS
        _no_typos(doc, keys, where, what)
        src = doc.get("source")
        if src not in SOURCES:
            raise ClockConfigError(
                f"{where}: 'clock.source' is {src!r}; it must be one of "
                f"{', '.join(SOURCES)}.")
        if src == "ltc_audio_master":
            raise ClockConfigError(LtcAudioMaster.REFUSAL.format(where=where))
        if src == "audio_master":
            return cls._parse_audio_master(doc, where)
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

    @classmethod
    def _parse_audio_master(cls, doc, where):
        """The audio_master block. Its own path, so the other sources parse
        exactly as they did before it existed."""
        audio = doc.get("show_audio", "ltcplay")
        if audio != "ltcplay":
            raise ClockConfigError(
                f"{where}: 'clock.source' is \"audio_master\", so this "
                f"program plays the show audio itself. 'clock.show_audio' "
                f"must be \"ltcplay\" or left out, not {audio!r}; MadMapper "
                f"plays the video only.")
        art = doc.get("artnet")
        if art is None:
            raise ClockConfigError(
                f"{where}: 'clock.source' is \"audio_master\" but there is "
                f"no 'clock.artnet' block, so the timecode would reach "
                f"nobody. Name MadMapper and BEYOND under "
                f"'clock.artnet.nodes'.")
        art = ArtNetConfig.parse(art, where)
        if art.broadcast:
            raise ClockConfigError(
                f"{where}: with \"audio_master\" the timecode goes to each "
                f"program by name, never by broadcast: on the bench "
                f"broadcast reached BEYOND but not MadMapper. List them "
                f"under 'clock.artnet.nodes' instead.")
        block = doc.get("audio")
        if block is None:
            raise ClockConfigError(
                f"{where}: 'clock.source' is \"audio_master\" but there is "
                f"no 'clock.audio' block naming the audio interface and the "
                f"stems to play.")
        from . import showaudio
        try:
            acfg = showaudio.AudioConfig.parse(block, where)
        except showaudio.AudioConfigError as e:
            raise ClockConfigError(str(e))
        zones = doc.get("zones")
        zones = ZoneConfig() if zones is None else ZoneConfig.parse(zones,
                                                                    where)
        return cls("audio_master", "ltcplay", art, zones, audio=acfg)

    def summary(self):
        s = self.source
        if self.audio is not None:
            s += f", {self.audio.summary()}"
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


class AudioMaster(Clock):
    """This machine plays the show audio, and the audio is the clock.

    Handoff section 4a (Jeff, 2026-09-27). The show's stems play in their
    own process (showaudio.py); this class never touches a sample. It reads
    where the audio device is, from the audio process's shared memory: the
    cue frame at the start of the last buffer, and when that buffer is heard
    (the callback's perf_counter plus the device's own output latency). From
    that it keeps one number, `_epoch`, the perf_counter time at which the
    cue's second zero is heard, so the position between callbacks is simply
    now - epoch: interpolated on perf_counter, smooth at 30 fps whatever
    size the device's buffers are. Each callback nudges the epoch a tenth of
    the way to what it says (callback timing jitter, and the device's own
    crystal drifting against this computer's), so the timecode follows the
    audio without stepping on its jitter.

    Out: one ArtTimeCode packet per frame, through the same TimecodeOut and
    arttimecode() as ArtNetMaster, to the named nodes (unicast only), and
    the same position to the pixels through the same sink call. The frame
    sent is the one the audio is in, sent the moment the audio reaches it;
    a frame is never sent twice in a row while playing and never goes
    backwards.

    Hold (pause): the audio fades out over hold_fade_ms (250 ms) and stops
    on an exact sample, which the audio process announces the moment the
    fade starts. The timecode follows the audio down the fade and freezes on
    the frame holding the last sample played, repeating it 30 times a second
    as ArtNetMaster does, with on_pause() at that moment. No run-on: the
    audio process plays silence from that sample on. Resume plays from that
    exact sample with a fade in, and the timecode carries on from the frozen
    frame once the audio is heard again (on_resume()).

    Abort (halt): the level fades to zero over abort_fade_ms (1 s), the
    timecode following the audio as it fades, then the cue stops: the
    audio, the timecode, and on_stop() to hand the pixels back.

    The end of the audio ends the cue: when the position passes the audio's
    length the cue stops through on_stop(), the same way ArtNetMaster's cue
    ends, so the same SHOW_ENDED path works.

    The audio interface lost mid-show (Jeff, 2026-09-27): the show carries
    on. The epoch stops being corrected and the position runs on
    perf_counter from exactly where it was, so the timecode does not jump;
    health goes red with a sentence and a journal line. The audio process
    keeps trying to reopen the interface (1 s, then backing off to 3 s).
    When it is back the audio restarts at the show's current position, fades
    in over return_fade_ms (1 s), and the clock goes back to following it:
    the small offset left at the handover is slewed out, never more than 5%
    faster or slower than real time, never a jump. The audio process dying
    counts as the interface being lost; AudioEngine starts a new one.

    Locking: every command and every step of the timecode thread take one
    lock. Unlike ArtNetMaster this has one long-lived thread, started by
    start() and joined only by stop() outside the lock, so no command ever
    waits on the thread while holding the lock the thread needs."""

    source = "audio_master"
    master = True
    ticker = None

    # No callback for this long while playing: lost. A little longer than
    # the audio process's own limit (showaudio.AudioProcess.STALL_S), so the
    # audio process, which can see why, is the one that says so; this is
    # the backstop for one that cannot. A shorter hiccup is not a loss: the
    # clock keeps following, and catches up with the audio when it moves.
    STALL_S = 0.6
    START_S = 1.0          # play or resume sent, nothing heard: lost
    FOLLOW_SLEW = 0.1      # share of each callback's correction taken
    RETURN_RATE = 0.05     # handover slew: at most 5% off real time
    RETURN_DONE_S = 0.001
    RESEEK_S = 0.25        # a handover further off than this is re-tried,
                           # not slewed (0.25 s is 5 s of slewing)
    RETRY_S = (1.0, 2.0, 3.0)
    MAX_SLEEP_S = 0.05
    LEAD_S = 0.005         # a command's trip to the audio process
    STOP_FADE_MS = 50      # Stop (not Abort): just enough not to click
    OUTLIER_S = 0.05       # a reading this far off the clock is ignored,
    OUTLIER_RUN = 6        # unless this many in a row (a fifth of a
                           # second) agree with it: then the clock goes
                           # straight to it
    RECENT_S = 10.0

    def __init__(self, cfg, sink=None, out=None, engine=None, cues=None,
                 clock=time.perf_counter, sleep=time.sleep,
                 mono=time.perf_counter, log=None, journal=None,
                 on_stop=None, on_pause=None, on_resume=None, threaded=True):
        from . import showaudio
        self._sa = showaudio
        self.cfg = cfg
        self.audio = cfg.audio
        self.rate = showaudio.RATE
        self.sink = sink
        self.out = out
        self.engine = engine
        self.cues = dict(cues or {})     # timeline cue name -> (role, frames)
        self._clock = clock
        self._sleep = sleep
        self._mono = mono
        self.log = log
        # Anything with the journal's fault(actor, sentence, action=...) and
        # record(...): journal.Logbook. None writes the show log only.
        self.journal = journal
        self.on_stop = on_stop
        self.on_pause = on_pause
        self.on_resume = on_resume
        self.threaded = threaded
        self.stream_id = cfg.artnet.stream_id if cfg.artnet else 0
        self._lock = threading.RLock()
        self._thread = None
        self._halt_evt = threading.Event()
        # Set by every command so the timecode thread acts on it now, not
        # at the end of whatever it was sleeping for.
        self._wake = threading.Event()
        self.kicked = False
        self._live = False
        self._started = False
        # The device, as the audio process last reported it.
        self._device_ok = False
        self._device_desc = ""
        self._latency = 0.0          # what the driver says
        self._cb_latency = None      # what the stream's callbacks show
        self._outliers = 0           # callbacks ignored in a row
        self._resync = False         # the audio really moved: follow it in
        self.outliers = 0
        self._loss_open = False      # a loss the journal has not closed
        self._loss_fault = None
        self._stall_loss = False     # lost because callbacks went quiet
        self.render_errors = 0
        self._render_err_at = None
        self._shared = False
        self._loaded = set()
        self._load_failed = {}
        self._fault = None
        self._fault_at = None
        # The cue.
        self._cue = None
        self._token = 0
        self._mode = None       # wait | follow | freerun | return
        self._epoch = None
        self._target = None
        self._slewed_at = None
        self._wait_since = None
        self._seen_seq = None
        self._last_cb = None
        self._child = (None, None)   # (token, state) of the last callback
        self._stop_frame = None
        self._pause_req = False
        self._paused = False
        self._resume_fade_ms = None   # resume()'s own fade, see _resume
        self._resuming = False
        self._resume_after = False
        self._halting = False
        self._halt_end = None
        self._level_down = False
        self._frozen_frame = None
        self._frozen_sec = None
        self._frozen_pos = None
        self._last_frame = None
        self._last_send_at = None
        self._lost_at = None
        self._retry_n = 0
        self._next_return = 0.0
        # What the page shows.
        self.last_sent = None
        self.last_ended = ""
        self.cues_played = 0
        self.skipped = 0
        self.losses = 0
        self.returns = 0
        self.clipped = 0
        self.underflows = 0
        self._clip_at = None
        self._underflow_at = None
        self.errors = 0
        self.last_error = ""

    # -- the session's side ----------------------------------------------
    def start(self):
        """Run pressed: start the audio process and open the interface.
        A device that is there but cannot run this show (too few outputs,
        not at 48 kHz, only reachable through Windows' shared mixer) refuses
        Run with its sentence. One that is simply not attached does not:
        health goes red and the audio process keeps trying."""
        with self._lock:
            self._live = True
        if not self._started:
            try:
                first = self.engine.start()
            except Exception as e:
                raise ClockConfigError(f"The show audio could not start: "
                                       f"{type(e).__name__}: {e}.")
            if first is not None and first[0] == "refused":
                try:
                    self.engine.close()
                except Exception:
                    pass
                raise ClockConfigError(first[1])
            self._started = True
        if self.threaded and (self._thread is None
                              or not self._thread.is_alive()):
            self._halt_evt = threading.Event()
            self._thread = threading.Thread(
                target=self._run, args=(self._halt_evt,),
                name="ltcplay-audio-timecode", daemon=True)
            self._thread.start()

    def stop(self):
        with self._lock:
            self._live = False
            if self._cue is not None:
                self._end("stopped", self._clock(),
                          fade=self._sa.fade_frames(self.STOP_FADE_MS))
        self._halt_evt.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(1.0)
        self._thread = None
        if self._started:
            try:
                self.engine.close()
            except Exception:
                pass
            self._started = False
        if self.out is not None:
            self.out.close()

    def play(self, position_s, length_s=None, label=""):
        """Run a cue: its audio from the top, the timecode from 00:00:00:00
        as the first sample is heard, the pixels from `position_s`. The
        cue's length is its audio's, whatever `length_s` says."""
        got = self.cues.get(label)
        if got is None:
            raise ClockConfigError(
                f"{label or 'This cue'} has no show audio in "
                f"'clock.audio.cues'. With \"audio_master\" the audio is the "
                f"clock, so a cue without audio cannot play.")
        role, frames = got
        with self._lock:
            now = self._clock()
            if not self._live:
                raise ClockConfigError("Nothing is running. Press Run first.")
            if not self._device_ok:
                # Jeff, 2026-09-27: a show never starts without its sound.
                # Losing the interface once a show is running is different:
                # that carries on and recovers (see _lost).
                why = self._fault or "It has not opened yet."
                msg = (f"The show will not start: the show audio interface "
                       f"{self.audio.device} is not available. "
                       f"{_strip_stop(why)}")
                self._event(msg)
                raise ClockConfigError(msg)
            if role in self._load_failed:
                raise ClockConfigError(
                    f"{_strip_stop(self._load_failed[role])} Replace the "
                    f"file, then press Stop and Run again.")
            if role not in self._loaded:
                raise ClockConfigError(
                    f"The {role} audio is still loading. Try again in a few "
                    f"seconds.")
            self._reset_cue()
            self._token += 1
            tc_frames = int(math.ceil(frames / float(self.rate) * MASTER_FPS
                                      - 1e-9))
            self._cue = {"label": label, "role": role, "frames": frames,
                         "position_s": float(position_s),
                         "tc_frames": tc_frames}
            self.cues_played += 1
            self._set_paused(False)
            self._restore_level()
            self._send(("play", role, 0, 0, self._token))
            self._mode = "wait"
            self._wait_since = now
            self._event(f"timecode from 00:00:00:00 for "
                        f"{label or 'a cue'}, following its audio")
            self._kick()
            return now

    def halt(self, fade_ms=None):
        """Abort: fade the level to zero over abort_fade_ms, then stop.
        `fade_ms` overrides abort_fade_ms for this one call (the show
        conductor's own fade length); None, every caller before it, is
        abort_fade_ms exactly as before."""
        with self._lock:
            if self._cue is None or self._halting:
                return
            now = self._clock()
            ms = self.audio.abort_fade_ms if fade_ms is None else fade_ms
            fade = self._sa.fade_frames(ms)
            if self._paused or self._mode in ("freerun", "wait") \
                    or fade <= 0:
                self._end("stopped", now)
                return
            self._send(("level", 0.0, fade))
            self._level_down = True
            self._halting = True
            # Until the fade has been heard, not just sent.
            self._halt_end = now + fade / float(self.rate) + \
                self._heard_latency() + 0.05
            self._pause_req = False
            self._resume_after = False
            self._kick()
            self._event(f"Abort: the show audio fades out over "
                        f"{ms / 1000.0:g} s, then the "
                        f"timecode stops")

    def pause(self, fade_ms=None):
        """Hold: fade the audio out and freeze on the frame it stops on.
        `fade_ms` overrides hold_fade_ms for this one call: the show
        conductor's rehearsal Hold passes 0, an instant freeze. None, every
        caller before the conductor, is hold_fade_ms exactly as before."""
        with self._lock:
            if self._cue is None:
                raise ClockConfigError("Nothing is playing to pause.")
            if self._paused or self._pause_req:
                raise ClockConfigError("The clock is already paused.")
            if self._halting:
                raise ClockConfigError("The show is stopping, so it cannot "
                                       "be paused.")
            now = self._clock()
            ms = self.audio.hold_fade_ms if fade_ms is None else fade_ms
            fade = self._sa.fade_frames(ms)
            if self._mode in ("follow", "return") and fade > 0:
                self._send(("pause", fade, self._token))
                self._stop_frame = None
                self._pause_req = True
                self._event(f"Hold: the show audio fades out over "
                            f"{ms:g} ms and the "
                            f"timecode freezes where it stops")
                return
            if self._mode in ("follow", "return", "wait"):
                self._send(("pause", 0, self._token))
            self._freeze(now)
            self._kick()

    def resume(self, fade_ms=None):
        """Carry on from exactly where the audio stopped. `fade_ms`
        overrides hold_fade_ms for this resume's fade in (the show
        conductor's rehearsal Resume passes 0); None is hold_fade_ms
        exactly as before."""
        with self._lock:
            if self._cue is None:
                raise ClockConfigError("Nothing is playing to resume.")
            # Set on every call, None included, so a value one Resume
            # asked for is never left behind for a later one.
            self._resume_fade_ms = fade_ms
            if self._pause_req and not self._paused:
                # Resume pressed inside the Hold's own fade: the Hold
                # finishes first, then this runs, so the audio and the
                # timecode stop and start on the same sample.
                self._resume_after = True
                return
            if not self._paused:
                raise ClockConfigError(
                    "The clock is not paused, so there is nothing to resume.")
            if self._resuming:
                raise ClockConfigError("The clock is already resuming.")
            self._resume(self._clock())
            self._kick()

    @property
    def paused(self):
        return self._paused or self._pause_req

    @property
    def playing(self):
        return self._cue is not None

    # -- internals, all called with the lock held --------------------------
    def _kick(self):
        self.kicked = True
        self._wake.set()

    def _send(self, msg):
        try:
            return self.engine.send(msg)
        except Exception:
            return False

    def _restore_level(self):
        if self._level_down:
            self._send(("level", 1.0, 0))
            self._level_down = False

    def _heard_latency(self):
        """How long after a callback its sound is heard: what the stream's
        callbacks show, or what the driver says before there are any."""
        return self._latency if self._cb_latency is None else \
            self._cb_latency

    def _reset_cue(self):
        self._mode = None
        self._epoch = None
        self._target = None
        self._stop_frame = None
        self._pause_req = False
        self._resuming = False
        self._resume_after = False
        self._halting = False
        self._halt_end = None
        self._frozen_frame = self._frozen_sec = self._frozen_pos = None
        self._last_frame = None
        self._last_send_at = None

    def _set_paused(self, active):
        """Flip _paused and tell the pixels, the one place both happen:
        the same contract as PR 20's ArtNetMaster._set_paused."""
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

    def _freeze(self, now):
        cue = self._cue
        if self._stop_frame is not None:
            sec = self._stop_frame / float(self.rate)
            last = max(self._stop_frame - 1, 0)
            frame = int(math.floor(last * MASTER_FPS / float(self.rate)
                                   + 1e-9))
        else:
            sec = max(self._pos(now) or 0.0, 0.0)
            frame = frame_at(sec, MASTER_FPS)
        if self._last_frame is not None:
            frame = max(frame, self._last_frame)
        self._frozen_frame = frame
        self._frozen_sec = sec
        self._frozen_pos = cue["position_s"] + frame / MASTER_FPS
        self._pause_req = False
        self._set_paused(True)
        h, m, s, f = frames_to_tc(frame, MASTER_FPS)
        self._event(f"paused at {h:02d}:{m:02d}:{s:02d}:{f:02d} for "
                    f"{cue['label'] or 'the cue'}")
        if self._resume_after:
            self._resume_after = False
            self._resume(now)

    def _resume(self, now):
        cue = self._cue
        start = int(round(self._frozen_sec * self.rate))
        ms = self._resume_fade_ms
        fade = self._sa.fade_frames(self.audio.hold_fade_ms if ms is None
                                    else ms)
        if start >= cue["frames"]:
            # Held on the very end of the audio: there is nothing left to
            # resume, so the cue ends here, the normal way.
            self._end("finished", now)
            return
        if self._mode != "freerun" and self._device_ok \
                and cue["role"] in self._loaded:
            if self._child == (self._token, self._sa.PAUSED) and \
                    self._stop_frame == start:
                # The audio process is holding this cue on that sample:
                # it carries on from there.
                self._send(("resume", fade, self._token))
            else:
                # It lost the cue meanwhile (a new process, a reopened
                # interface): the same sample, from the top.
                self._token += 1
                self._send(("play", cue["role"], start, fade, self._token))
            self._mode = "wait"
            self._wait_since = now
            self._resuming = True
            self._stop_frame = None
            self._event(f"resumed for {cue['label'] or 'the cue'}: the "
                        f"audio fades back in from where it stopped")
            return
        self._unfreeze(now)
        self._event(f"resumed for {cue['label'] or 'the cue'} on this "
                    f"computer's own clock")

    def _unfreeze(self, now):
        self._epoch = now - self._frozen_sec
        self._stop_frame = None
        self._resuming = False
        self._mode = "freerun"
        self._set_paused(False)

    def _pos(self, now):
        """Seconds into the cue's audio, or None before it is heard."""
        if self._paused:
            return self._frozen_sec
        if self._epoch is None or self._mode == "wait":
            return None
        p = now - self._epoch
        if self._stop_frame is not None:
            p = min(p, self._stop_frame / float(self.rate))
        return p

    def _end(self, why, now, fade=0):
        cue = self._cue
        self._cue = None
        self._reset_cue()
        self._send(("stop", fade, None))
        self._restore_level()
        self._set_paused(False)
        label = cue["label"] if cue else ""
        self.last_ended = f"{label or 'cue'} {why}"
        self._event(f"timecode {'ended with' if why == 'finished' else 'stopped for'}"
                    f" {label or 'the cue'}")
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

    def _journal(self, sentence, fault, action="show audio"):
        if self.log:
            try:
                self.log.event("show-audio", sentence)
            except Exception:
                pass
        j = self.journal
        if j is None:
            return
        try:
            if fault:
                j.fault("system", sentence, action=action)
            else:
                j.record(actor="system", action=action, outcome="recovered",
                         reason=sentence, text=sentence)
        except Exception:
            pass

    def _tc_text(self, frame):
        h, m, s, f = frames_to_tc(frame, MASTER_FPS)
        return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"

    def _set_fault(self, sentence, now, journal=True):
        if sentence == self._fault:
            return
        self._fault = sentence
        self._fault_at = now
        if journal:
            self._journal(sentence, True)

    # -- what the audio process says ---------------------------------------
    def _on_event(self, ev, now):
        kind = ev[0]
        if kind == "opened":
            self._device_ok = True
            self._device_desc = ev[1]
            self._latency = float(ev[2] or 0.0)
            self._cb_latency = None
            self._shared = bool(ev[3])
            self._event(f"show audio on {ev[1]}")
            if (self._cue is None or self._mode != "freerun") and \
                    self._fault not in self._load_failed.values():
                if self._fault is not None:
                    self._journal(f"The show audio interface is working: "
                                  f"{ev[1]}.", False)
                self._fault = None
        elif kind in ("lost", "refused", "unavailable", "crashed"):
            self._device_ok = False
            self._stall_loss = False
            if kind == "crashed":
                self._loaded.clear()
            if self._cue is not None and self._mode in ("wait", "follow",
                                                        "return"):
                self._lost(now, ev[1])
            elif self._cue is not None and self._mode == "freerun":
                self._fault = self._fault or ev[1]
            else:
                self._set_fault(ev[1], now)
        elif kind == "loaded":
            self._loaded.add(ev[1])
            self._load_failed.pop(ev[1], None)
            self._event(f"the {ev[1]} audio is loaded, "
                        f"{self._sa.fmt_len(ev[2])}")
        elif kind == "load_failed":
            self._loaded.discard(ev[1])
            self._load_failed[ev[1]] = ev[2]
            self._set_fault(ev[2], now)

    def _on_reading(self, r, now):
        if r.seq == self._seen_seq:
            return
        self._seen_seq = r.seq
        self._last_cb = r.perf
        self._cb_latency = r.latency
        self._child = (r.token, r.state)
        if r.underflows > self.underflows:
            self.underflows = r.underflows
            self._underflow_at = now
        if r.clipped > self.clipped:
            self.clipped = r.clipped
            self._clip_at = now
        if r.errors > self.render_errors:
            self.render_errors = r.errors
            self._render_err_at = now
        if self._cue is None or r.token != self._token:
            return                       # another cue's, or an older play's
        if r.stop_frame >= 0 and self._pause_req:
            self._stop_frame = r.stop_frame
        if not r.playing:
            if self._mode == "follow" and not self._paused \
                    and not self._pause_req and not self._halting:
                self._stopped_playing(r, now)
            return
        e = r.perf + r.latency - r.frame / float(self.rate)
        if self._mode == "freerun" and self._stall_loss and \
                not self._paused:
            # The same stream playing the same cue again after going quiet:
            # follow it again rather than stop it, and let the timecode
            # wait for the audio to catch up.
            self._mode = "follow"
            self._stall_loss = False
            self._recovered(now)
            self._resync = True
        if self._mode == "wait":
            self._epoch = e
            self._mode = "follow"
            self._outliers = 0
            self._resync = False
            if self._resuming:
                self._resuming = False
                self._set_paused(False)
            self._recovered(now)
        elif self._mode == "follow":
            if self._outlier(e, self._epoch):
                return
            if self._resync:
                # The audio really moved (an underrun, a stream catching its
                # breath): go straight to it. The timecode never runs
                # backwards (_emit holds the last frame until the audio
                # reaches it), so this is the clock waiting for the sound,
                # exactly as long as the sound stopped.
                self._epoch = e
                self._resync = False
                return
            self._epoch += (e - self._epoch) * self.FOLLOW_SLEW
        elif self._mode == "return":
            if self._target is None and abs(e - self._epoch) > self.RESEEK_S:
                self._lost(now, f"{self.audio.device} came back "
                                f"{abs(e - self._epoch):.2f} s away from the "
                                f"show.")
                return
            if self._target is not None and self._outlier(e, self._target):
                return
            self._target = e if self._target is None else \
                self._target + (e - self._target) * self.FOLLOW_SLEW

    def _outlier(self, e, ref):
        """True for one callback far off the clock: a driver's latency
        report spiking, not the audio moving, so it is ignored. When
        OUTLIER_RUN in a row agree, the audio really has moved (an underrun,
        a stream catching its breath) and the clock follows it in until it
        is close again."""
        if abs(e - ref) <= self.OUTLIER_S:
            self._outliers = 0
            self._resync = False
            return False
        if self._resync:
            return False
        self._outliers += 1
        self.outliers += 1
        if self._outliers >= self.OUTLIER_RUN:
            self._resync = True
        return True

    def _stopped_playing(self, r, now):
        """The audio process says it is not playing this cue although the
        clock is following it. At the end of the audio that is the end of
        the cue, which _emit ends; anywhere else the sound has stopped,
        and that is a loss, never something to carry on quietly past."""
        cue = self._cue
        if r.state == self._sa.ENDED and \
                r.frame >= cue["frames"] - self.rate // MASTER_FPS:
            return
        self._lost(now, f"The show audio stopped playing at "
                        f"{self._sa.fmt_len(r.frame)} of "
                        f"{self._sa.fmt_len(cue['frames'])} with nothing "
                        f"asking it to.")

    def _recovered(self, now):
        """The clock follows the audio again after a loss: clear the red and
        close the loss in the journal. Every way back comes through here."""
        if not self._loss_open:
            return
        self._loss_open = False
        self.returns += 1
        self._retry_n = 0
        gone = now - (self._lost_at or now)
        pos = self._pos(now)
        at = self._tc_text(frame_at(pos, MASTER_FPS)) if pos is not None \
            and pos >= 0 else "the frozen frame"
        if self._fault == self._loss_fault:
            self._fault = None
        self._loss_fault = None
        self._journal(f"The show audio is back after {gone:.1f} s and the "
                      f"show clock follows it again, from {at}.", False)

    def _check(self, now):
        cue = self._cue
        if cue is None:
            return
        name = self.audio.device
        if self._mode == "wait" and now - self._wait_since > self.START_S:
            self._lost(now, f"No sound came from {name} within "
                            f"{self.START_S:g} s of starting.")
        elif self._mode == "return" and self._target is None and \
                now - self._wait_since > self.START_S:
            self._lost(now, f"{name} came back but did not play.")
        elif (self._mode == "follow" or (self._mode == "return"
                                          and self._target is not None)) \
                and self._last_cb is not None and \
                now - self._last_cb > self.STALL_S:
            self._lost(now, f"{name} stopped playing: nothing from it for "
                            f"{now - self._last_cb:.1f} s.", stall=True)

    def _lost(self, now, why, stall=False):
        """The audio stopped mid-cue. Carry on on perf_counter from exactly
        where the clock was: the epoch simply stops being corrected."""
        cue = self._cue
        retry = self._mode == "return"
        if self._mode == "wait":
            if self._resuming:
                self._unfreeze(now)
            elif self._epoch is None:
                self._epoch = now
        self._mode = "freerun"
        self._target = None
        # A stall seen only from here may be the stream catching its breath:
        # leave its sound alone. The audio process stops it itself if it
        # really has died, and says so.
        self._stall_loss = stall
        if not stall:
            self._send(("stop", 0, None))
        if self._pause_req:
            self._stop_frame = None
            self._freeze(now)
        if self._halting:
            self._end("stopped", now)
            return
        if retry:
            # A handover that did not take: back on this computer's clock,
            # the same loss still standing, and try again a little later.
            self._retry_n += 1
            self._next_return = now + self.RETRY_S[
                min(self._retry_n, len(self.RETRY_S) - 1)]
            self._event(f"the show audio did not come back: {why}")
            return
        if self._loss_open:
            return
        self.losses += 1
        self._lost_at = now
        self._retry_n = 0
        self._next_return = now + self.RETRY_S[0]
        pos = self._pos(now) or 0.0
        at = self._tc_text(frame_at(pos, MASTER_FPS))
        self._loss_open = True
        self._loss_fault = (
            f"The show audio dropped out at {at} in "
            f"{cue['label'] or 'the cue'}. {_strip_stop(why)} The rest of "
            f"the show carries on on this computer's own clock, and "
            f"ltcplay keeps trying to reopen the interface.")
        self._set_fault(self._loss_fault, now)

    def _maybe_return(self, now):
        cue = self._cue
        if cue is None or self._mode != "freerun" or self._paused \
                or self._pause_req or self._halting:
            return
        if not self._device_ok or cue["role"] not in self._loaded:
            return
        if now < self._next_return:
            return
        fade = self._sa.fade_frames(self.audio.return_fade_ms)
        start = now - self._epoch + self.LEAD_S + self._heard_latency()
        first = int(round(start * self.rate))
        if first + fade >= cue["frames"]:
            return               # too near the end to be worth it
        self._token += 1
        self._restore_level()
        self._send(("play", cue["role"], first, fade, self._token))
        self._stall_loss = False
        self._resync = False
        self._outliers = 0
        self._mode = "return"
        self._target = None
        self._wait_since = now
        self._slewed_at = now
        self._event(f"the show audio interface is back; its audio restarts "
                    f"at {self._tc_text(frame_at(start, MASTER_FPS))} and "
                    f"fades in")

    def _slew(self, now):
        if self._mode != "return" or self._target is None:
            self._slewed_at = now
            return
        dt = max(0.0, now - (self._slewed_at or now))
        self._slewed_at = now
        d = self._target - self._epoch
        lim = self.RETURN_RATE * dt
        self._epoch += max(-lim, min(lim, d))
        if abs(self._target - self._epoch) <= self.RETURN_DONE_S:
            self._mode = "follow"
            self._recovered(now)

    # -- the timecode ------------------------------------------------------
    def step(self, now):
        """One pass: take in what the audio process said, send the frame
        that is due, and return when the next one is. The timecode thread
        calls this; the tests call it on a clock of their own."""
        with self._lock:
            for ev in self.engine.events():
                self._on_event(ev, now)
            r = self.engine.read()
            if r is not None:
                self._on_reading(r, now)
            self._check(now)
            self._maybe_return(now)
            self._slew(now)
            return self._emit(now)

    def _emit(self, now):
        cue = self._cue
        if cue is None:
            return now + self.MAX_SLEEP_S
        period = 1.0 / MASTER_FPS
        if self._paused:
            last = self._last_send_at
            if last is None or now - last >= period - 1e-9:
                self._send_frame(self._frozen_frame, now, frozen=True)
                if last is not None and now - last < 2 * period:
                    self._last_send_at = last + period
                last = self._last_send_at
            return last + period
        if self._halting and now >= self._halt_end:
            self._end("stopped", now)
            return now + self.MAX_SLEEP_S
        pos = self._pos(now)
        if pos is None:
            return now + 0.002
        if pos < 0:
            return min(self._epoch, now + self.MAX_SLEEP_S)
        if self._pause_req and self._stop_frame is not None and \
                pos * self.rate >= self._stop_frame - 1e-6:
            self._freeze(now)
            return now
        frame = frame_at(pos, MASTER_FPS)
        if frame >= cue["tc_frames"]:
            self._end("finished", now)
            return now + self.MAX_SLEEP_S
        last = self._last_frame
        if last is None:
            # Every cue's timecode starts at 00:00:00:00 (handoff section
            # 4), however late this thread got to it on a busy machine:
            # frame 0 first, stamped with the moment it began, then straight
            # on to the frame the audio is in.
            frame = 0
        if last is None or frame > last:
            if last is not None and frame > last + 1:
                self.skipped += frame - last - 1
            self._send_frame(frame, now)
            last = frame
        return min(self._epoch + (last + 1) / MASTER_FPS,
                   now + self.MAX_SLEEP_S)

    def _send_frame(self, frame, now, frozen=False):
        cue = self._cue
        h, m, s, f = frames_to_tc(frame, MASTER_FPS)
        if self.out is not None:
            self.out.send(arttimecode(h, m, s, f, MASTER_TYPE,
                                      self.stream_id))
        self.last_sent = (h, m, s, f)
        self._last_frame = frame
        self._last_send_at = now
        if self.sink is None:
            return
        text = f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"
        if frozen:
            self.sink(self._frozen_pos, self._mono(), False, text)
        else:
            began = self._epoch + frame / MASTER_FPS
            self.sink(cue["position_s"] + frame / MASTER_FPS,
                      self._mono() - (self._clock() - began), False, text)

    def _run(self, halt):
        while not halt.is_set():
            try:
                due = self.step(self._clock())
            except Exception as e:
                # Nothing raised here may end the clock: see Ticker.run.
                self.errors += 1
                self.last_error = f"{type(e).__name__}: {e}"
                if self.log:
                    try:
                        self.log.event(
                            "clock-error", f"step failed: {self.last_error}",
                            throttle_s=5.0)
                    except Exception:
                        pass
                due = self._clock() + 0.01
            wait = due - self._clock()
            if wait > 0:
                self._wake.wait(min(wait, self.MAX_SLEEP_S))
            self._wake.clear()
            self.kicked = False

    # -- the page ----------------------------------------------------------
    def health_warnings(self):
        """What is wrong with the show audio right now, as sentences.
        display.clock_warnings adds these to the page's red list."""
        out = []
        now = self._clock()
        if self._fault:
            out.append(self._fault)
        if self._shared:
            out.append("The show audio is going through Windows' shared "
                       "audio engine because 'allow_shared_mode' is on. "
                       "That is for a bench test only: on the bench it "
                       "broke up 3 to 6 times a show.")
        if self._clip_at is not None and now - self._clip_at < self.RECENT_S:
            out.append(f"The show audio mix is clipping ({self.clipped} "
                       f"samples so far). Turn a stem's gain_db down.")
        if self._underflow_at is not None and \
                now - self._underflow_at < self.RECENT_S:
            out.append(f"The audio interface ran short of audio "
                       f"{self.underflows} time(s) so far; each is a click "
                       f"in the sound.")
        if self._render_err_at is not None and \
                now - self._render_err_at < self.RECENT_S:
            out.append(f"The show audio process hit {self.render_errors} "
                       f"error(s) making the sound and played silence "
                       f"instead. The show log has the details.")
        if self.errors:
            out.append(f"The show clock hit {self.errors} error(s) and "
                       f"kept going. Last: {self.last_error}")
        return out

    def snapshot(self):
        d = super().snapshot()
        lt = self.last_sent
        cue = self._cue
        pos = None
        try:
            pos = self._pos(self._clock()) if cue else None
        except Exception:
            pass
        d.update({"playing": cue["label"] if cue else None,
                  "paused": self.paused,
                  "sending": (f"{lt[0]:02d}:{lt[1]:02d}:{lt[2]:02d}:"
                              f"{lt[3]:02d}" if lt and cue else None),
                  "skipped": self.skipped,
                  "last_ended": self.last_ended,
                  "audio": {"device": self.audio.device,
                            "connected": self._device_ok,
                            "via": self._device_desc,
                            "mixer_free": (not self._shared
                                           if self._device_desc else None),
                            "mode": self._mode or "idle",
                            "following": self._mode == "follow",
                            "stopping": self._halting,
                            "position": pos,
                            "fault": self._fault,
                            "losses": self.losses,
                            "returns": self.returns,
                            "clipped": self.clipped,
                            "underflows": self.underflows,
                            "render_errors": self.render_errors,
                            "outliers": self.outliers,
                            "respawns": getattr(self.engine, "respawns", 0),
                            "loaded": sorted(self._loaded),
                            # A cue is ready only once every stem of it is
                            # wholly in memory and checked.
                            "ready": {
                                role: ("failed" if role in self._load_failed
                                       else "ready" if role in self._loaded
                                       else "loading")
                                for role, _f in self.cues.values()}}})
        d.update(_out_snapshot(self.out, self))
        return d


def _strip_stop(why):
    why = str(why).strip()
    return why if why.endswith(".") else why + "."


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
          bind_ip=None, on_stop=None, on_pause=None, on_resume=None,
          engine=None):
    """The clock a show file asks for, wired to the position stream.

    engine is read by audio_master only."""
    if out is None and not no_output and cfg.artnet is not None:
        out = TimecodeOut(cfg.artnet.dests,
                          broadcast=bool(cfg.artnet.broadcast), log=log,
                          bind_ip=bind_ip)
    if cfg.source == "audio_master":
        return _build_audio_master(cfg, timeline, sink, out, log, on_stop,
                                   on_pause, on_resume, engine)
    if cfg.source == "artnet_master":
        return ArtNetMaster(cfg, sink=sink, out=out, log=log, on_stop=on_stop,
                            on_pause=on_pause, on_resume=on_resume)
    if cfg.source == "ltc_audio_slave":
        show_len = cfg.zones.show_len_s
        derived = None
        if "show" in cfg.zones.forward and cfg.artnet is not None:
            derived = _show_length(timeline, cfg.zones.show)
        if show_len is None:
            # Show length follows the show's own media (Jeff, 2026-09-26):
            # read from the renders, never typed in a second place, so a
            # config that names no length cannot drift from the show.
            show_len = derived
        elif derived is not None and show_len < derived:
            # A configured length shorter than the show's own media would
            # cut it off mid-cue (the handoff's own example: 440 configured
            # against a 444.42 s music track). Refuse rather than free run
            # to a made-up end that is short by the difference.
            raise ClockConfigError(
                f"'clock.zones.show_len_s' is {show_len:g} s, shorter than "
                f"the show's own media, which runs {derived:g} s. Set "
                f"'clock.zones.show_len_s' to at least {derived:g}, or "
                f"leave it out so the length is read from the show.")
        return LtcAudioSlave(cfg, count=timeline.count, drop=timeline.drop,
                             fps=timeline.fps, show_len_s=show_len, out=out,
                             log=log)
    if cfg.source == "ltc_audio_master":
        return LtcAudioMaster()
    raise ClockConfigError(f"no clock called {cfg.source!r}")


def _build_audio_master(cfg, timeline, sink, out, log, on_stop, on_pause,
                        on_resume, engine):
    """Check every stem against the show folder, then wire the clock to an
    audio process that is not started until Run."""
    from . import showaudio
    try:
        checked = showaudio.check_show(cfg.audio, timeline)
    except showaudio.AudioConfigError as e:
        raise ClockConfigError(str(e))
    if engine is None:
        engine = showaudio.AudioEngine(
            showaudio.engine_spec(cfg.audio, checked), log=log)
    cues = {c["label"]: (role, c["frames"]) for role, c in checked.items()}
    return AudioMaster(cfg, sink=sink, out=out, engine=engine, cues=cues,
                       log=log, on_stop=on_stop, on_pause=on_pause,
                       on_resume=on_resume)


def _show_length(timeline, hour):
    """How long the show zone runs: to the end of its last cue.

    Free running to the end of the show needs to know where the end is.
    Read from the renders rather than typed in a second place. This is also
    what "show length follows the music track" (Jeff, 2026-09-26) means in
    practice: the show's cues cover its own music, so the last cue's own end
    is the music's own length, without opening the audio file a second time
    to ask again."""
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


def derive_show_length_in_folder(folder, sd=None):
    """(path, length_s, warning) about the show the folder's clock config
    actually names:

    - (path, length_s, None): exactly one show file in the folder names a
      clock block with a derivable show length. Use it.
    - (None, None, sentence): otherwise -- more than one candidate, with
      no way to tell which one is the real one (refuse rather than guess
      "the first *.json", review round 2, 2026-09-26), or none at all, or
      one that named a clock block but would not open or would not
      derive. `sentence` says which, and names every candidate file
      involved, so the caller can warn instead of silently skipping the
      check (review round 2: "never silently skip").

    Used at startup, before anything is served, to cross-check the
    SCHEDULER's own show_len_s against the show's own media the same way
    `build` already checks `clock.zones.show_len_s` (Jeff, 2026-09-26:
    show length follows the music, wherever the code has access to it).
    See web.serve(), the only place that has both the schedule rules and a
    show folder at once; schedule.py stays pure and never reads a file of
    its own, and this function does not touch it or schedule_service.py.

    A plain Timeline.load() is not enough: a cue's own end_seconds needs
    its duration, which only a real open reads off the FSEQ header, the
    same as clock.build() itself is only ever called from inside one (see
    session.py). So each candidate is opened the same way the page's own
    Check button does (Session, no_output, no_log): safe to call for every
    file in the folder, and it never touches real audio or network output.

    Never raises: a file that is not JSON, is not a show, has no clock
    block, will not open (a missing FSEQ, a bad setting) or cannot derive
    a length is recorded as a problem, not thrown."""
    import glob
    import json as _json
    import os as _os
    from .session import Session
    candidates = []       # [(path, length_s)]
    problems = []          # [sentence], one per candidate that could not
                           # be used
    for path in sorted(glob.glob(_os.path.join(folder, "*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = _json.load(fh)
        except (OSError, ValueError):
            continue
        # A cheap pre-filter before the expensive part (opening every FSEQ
        # a candidate names): only a real show names cues, and only one
        # with a clock block is a candidate for this check at all.
        if not (isinstance(doc, dict) and isinstance(doc.get("cues"), list)
                and "clock" in doc):
            continue
        name = _os.path.basename(path)
        try:
            s = Session(path, no_output=True, no_log=True, sd=sd)
            s.open()
        except Exception as e:
            problems.append(f"{name} could not be opened: {_clean_e(e)}")
            continue
        if s.tl is None or s.tl.clock is None:
            continue
        try:
            length_s = _show_length(s.tl, s.tl.clock.zones.show)
        except ClockConfigError as e:
            problems.append(f"{name}'s show length could not be read: "
                            f"{_clean_e(e)}")
            continue
        candidates.append((path, length_s))
    if len(candidates) == 1:
        return candidates[0][0], candidates[0][1], None
    if len(candidates) > 1:
        names = ", ".join(_os.path.basename(p) for p, _ in candidates)
        return None, None, (
            f"more than one show file in the folder names a clock block "
            f"({names}), so it is not clear which one to check the "
            f"schedule's show_len_s against")
    if problems:
        return None, None, "; ".join(problems)
    return None, None, ("no show file in the folder names a clock block, "
                        "so the schedule's show_len_s could not be checked "
                        "against the show's own media")


def _clean_e(e):
    # Unicode escapes, not literal characters: this file's own source is
    # scanned for a bare em or en dash (test_clock_settings_fail_loudly),
    # and this is the one place clock.py has to name them in order to
    # strip them from someone else's exception text.
    return str(e).replace("\u2014", "-").replace("\u2013", "-")
