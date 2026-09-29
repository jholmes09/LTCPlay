"""MadMapper: the OSC transport (device layer only) and the heartbeat
watchdog.

Imported ONLY when a show file has a "madmapper" block. The GPL show at
Dollywood never has one, so on the Mac none of this is loaded -- the same
inertness clock.py, schedule_service.py and announce.py each already rely
on, proven the same way: see test_the_gpl_path_never_loads_madmapper.

This module is the DEVICE LAYER: clean primitives (select_bank, stop_bank,
play, play_from_beginning, fade_audio, fade_surfaces, fade_all, set_audio,
set_surfaces, restore_levels) and the Watchdog (arm/disarm are explicit
calls, never inferred). It contains NO sequencing logic and NO scheduler
wiring: no on_transition, no hold()/resume()/abort()/closing(), no
show_started()/show_ended(). Those all lived here in an earlier version of
this PR and were removed after an opus review (round 2) found two real
races: deriving "what to do" from a PAIR of scheduler states on the
scheduler's own unordered per-transition hook threads (schedule_service.py's
_locked() spawns a fresh thread per pending hook, with no ordering
guarantee between them) means a Hold immediately followed by a quick Resume
can run in either order, or interleave, leaving the real clock frozen while
the lasers show unblanked (R1), or a late show-start unblank landing after
a Hold's blank (R2). The fix is architectural, not a patch: a future
"conductor" module will run the scheduler's own ORDERED effects list
(START_SHOW, FREEZE_SHOW, BLANK_LASERS, FADE_MUSIC_OUT, ...) through ONE
serialized executor with a generation guard, and will be the only thing
that ever calls the primitives below. See the PR body for the follow-up.

devices.py, alongside this module, composes these primitives (and
beyond.py's blank()/unblank()) into on_hold()/on_resume()/on_abort(): three
plain synchronous functions with the handoff's own ordering already built
in, ready for that future conductor to call directly. They are NOT
scheduler hooks either -- see devices.py's own module docstring for why
that distinction matters here.

Everything below follows the Pico bench report, 2026-09-25
(bench/bench_report_2026-09-25.md on branch bench/2026-09-25), sections B1
to B4 and B9, which override the handoff wherever the two differ. The facts
that shape this module:

  No replies, ever (B2.4). MadMapper answers nothing: not an ack, not an
  error for a bad address. So nothing here can confirm a command landed --
  only the Watchdog's heartbeat says the video is actually moving.

  Bank select is `/timelines/Bank-N/select` with the bare bool tag T (no
  data bytes for T/F -- not part of OSC 1.0 proper, but what MadMapper's own
  OSC implementation and the bench's sender both do, and what the captured
  bytes in test_madmapper_osc_bytes_match_the_bench_capture prove byte for
  byte). `/timelines/active_bank` and `by_name` did nothing (B2.2).

  `/timelines/Bank-N/conductor/stop` (no arguments) works even while
  chasing; `play_from_beginning` and `pause` are ignored while chasing
  (B2.2) -- a chasing bank is driven by Art-Net timecode, never by these.
  On a NON-chasing bank (the intermission, in Jeff's model), stop is a
  PAUSE, NOT a rewind (bench B14, on the Pico against the real MadMapper):
  the playhead stays exactly where it was, and a later `play` resumes
  from there, not from zero. The intermission must always be started
  with `play_from_beginning`, never a bare `play`, or it will resume
  mid-loop from wherever it last stopped rather than from its own start.

  `/master/master_audio_level` is a float 0 to 1, linear, and jumps with no
  ramp of its own (B2.2): the smoothness is entirely the sender's job, 31
  steps over 1 s in the bench. `/master/master_video_level` did nothing
  (B2.2); `/surfaces/<name>/opacity` is the real fade to black, one message
  per surface per step.

  MadMapper keeps whatever level a fade last set (bench, inferred from B4:
  nothing ever resets it). After Abort or Closing leaves both at 0, the
  next show needs both explicitly set back to 1 before anyone can hear or
  see it -- restore_levels() is that primitive; a future conductor calls it
  at the right point in its own sequence.

  The heartbeat (B3) is an OSC Float track MadMapper renders once a frame,
  about 60/s, whose value times the show length is MadMapper's own reported
  position. It sends nothing while the timecode is frozen, after the show
  ends, or on a bank with no track of its own (the intermission, in Jeff's
  model): silence there is NORMAL and must never alarm. It answers no
  ack either -- see Watchdog's class docstring for exactly when "no pulse
  for `timeout_s`" is, and is not, a fault.

  Between shows, MadMapper sends exactly one heartbeat packet, value 1.0
  (bench B9's 4 hour soak): it re-sends the Float track's last value the
  instant `/timelines/Bank-1/select` arrives, about 2 s before the show
  starts. A caller that arms the watchdog before or at that select (which
  any real wiring will do, since the bank needs selecting before the show
  can start) must never have that lone, stale packet read as a live
  position -- see Watchdog's "awaiting its first real packet" gate.
"""
import ipaddress
import math
import queue as _queue
import socket
import struct
import threading
import time

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_HEARTBEAT_BIND = "127.0.0.1"
DEFAULT_HEARTBEAT_PORT = 9001
DEFAULT_HEARTBEAT_ADDRESS = "/float-1"
DEFAULT_FADE_S = 1.0
DEFAULT_RAMP_STEPS = 31          # bench B2, B4: 31 steps over 1 s was smooth
DEFAULT_TIMEOUT_S = 3.0          # handoff section 4: no pulse for 3 s = fault
DEFAULT_DRIFT_MS = 100.0         # handoff section 4: configurable, default 100
AUDIO_ADDR = "/master/master_audio_level"
# Bench B9: MadMapper's re-sent value at bank select is 1.0 (the end of
# whatever it last showed), not anywhere near a real show's start. A real
# start reads near 0. This is deliberately a small absolute number of
# seconds, not a fraction, so it means the same thing whatever the show's
# length: 2 s into a 444 s show and 2 s into a 60 s one are both "just
# started", and 2 s is comfortably inside the reader's own JUMP_FRAMES-style
# tolerance for start-up jitter without being anywhere near what a stale
# end-of-show value (444.42 s or 60 s) reads as.
START_WINDOW_S = 2.0

# The worker queue's own patience: a ramp or a send that never returns
# (a wedged socket call, a bug in a future ramp) must not make every public
# method here block forever. Generous on purpose -- the longest real ramp
# is a couple of seconds -- so this only ever fires on a genuine hang.
SUBMIT_TIMEOUT_S = 10.0


class MadMapperConfigError(ValueError):
    """A madmapper block that cannot run, with the sentence that says why.

    A ValueError, so the session reports it the way it reports every other
    show file mistake: as a line a person can act on."""


# ---------------------------------------------------------------- wire ----
def _pad(b):
    """Null-terminate then pad to a 4-byte boundary: at least one null,
    always. A length already a multiple of 4 still gets a full 4 bytes of
    padding, never zero -- see test_madmapper_osc_bytes_match_the_bench_
    capture, whose 24-character address is exactly this case."""
    b = b + b"\x00"
    while len(b) % 4:
        b += b"\x00"
    return b


def encode(address, args=()):
    """One OSC 1.0 message: address, then a type-tag string, then the
    arguments -- the same shape the bench's own throwaway sender built
    (bench_report_2026-09-25.md, B2, `scratch/osc_send.py`), and what the
    captured bytes for `/timelines/Bank-2/select ,T` match byte for byte.

    A bool is the bare tag T or F with NO data bytes: not part of OSC 1.0
    proper, but what MadMapper's OSC implementation and the bench's own
    sender both use for /select and /conductor/play."""
    if not isinstance(address, str) or not address.startswith("/"):
        raise ValueError(f"an OSC address has to start with /, not "
                         f"{address!r}")
    tags = ","
    data = b""
    for a in args:
        if isinstance(a, bool):
            tags += "T" if a else "F"
        elif isinstance(a, float):
            tags += "f"
            data += struct.pack(">f", a)
        elif isinstance(a, int):
            tags += "i"
            data += struct.pack(">i", a)
        elif isinstance(a, str):
            tags += "s"
            data += _pad(a.encode("utf-8"))
        else:
            raise TypeError(f"an OSC argument cannot be a "
                            f"{type(a).__name__}")
    return _pad(address.encode("utf-8")) + _pad(tags.encode("ascii")) + data


def _read_osc_string(pkt, at):
    """(text, offset of the next field) reading one null-padded OSC string
    starting at `at`. Raises ValueError if there is no terminator."""
    end = pkt.find(b"\x00", at)
    if end == -1:
        raise ValueError("no null terminator")
    s = pkt[at:end].decode("utf-8", "replace")
    length = end - at
    nxt = at + ((length + 4) // 4) * 4
    return s, nxt


def decode_float(pkt):
    """(address, value) from one OSC message carrying exactly one float,
    the shape of MadMapper's heartbeat track (bench B3: `/float-1 ,f
    <value>`, sent from MadMapper's own OSC input port, 127.0.0.1:8000 in
    the bench). Returns None for anything else -- a short packet, a bad or
    absent type tag, more or fewer arguments -- rather than raising: a
    watchdog that crashes on one stray packet is worse than one that
    ignores it."""
    try:
        addr, at = _read_osc_string(pkt, 0)
        tags, at = _read_osc_string(pkt, at)
        if tags != ",f" or len(pkt) < at + 4:
            return None
        value, = struct.unpack(">f", pkt[at:at + 4])
        return addr, value
    except ValueError:
        return None


def ramp_values(start, end, steps=DEFAULT_RAMP_STEPS):
    """The list of values a ramp sends, first exactly `start` and last
    exactly `end` (bench B2/B4: 31 steps over 1 s). Pure, so the step count
    and the values themselves are testable with no socket, no thread and no
    clock at all."""
    if steps < 2:
        return [float(end)]
    step = (end - start) / (steps - 1)
    return [float(start + step * i) for i in range(steps)]


CURVE_LINEAR = "linear"
CURVE_PERCEPTUAL = "perceptual"
VIDEO_CURVES = (CURVE_LINEAR, CURVE_PERCEPTUAL)


def _shape_perceptual(v_lin, start, end):
    """One linearly-interpolated value, reshaped so the OUTPUT looks
    linear to the eye rather than to a light meter (a Pico bench finding,
    B14: a plain linear opacity fade "holds, then drops in the last
    0.5 s", because human brightness perception is not linear -- most of
    a linear ramp's steps land in the range the eye reads as "still
    bright", and the actual-looking fade is compressed into the last
    fraction of the fall).

    Fading DOWN (start above end): the fraction of brightness still
    remaining, linearly, is squared -- an ease-in curve that drops fast
    at first and lingers longest in the dark range, where the eye is most
    sensitive to a change. Fading UP: the mirror image (ease-out), so a
    fade up and a fade down of the same two values look like reverses of
    each other, not different shapes. Audio never uses this (bench B14:
    audio measured even as a plain linear ramp); only fade_surfaces()
    ever passes a curve other than CURVE_LINEAR."""
    if start == end:
        return v_lin
    if start > end:
        frac = (v_lin - end) / (start - end)
        return end + (frac ** 2) * (start - end)
    frac = (v_lin - start) / (end - start)
    return start + (1 - (1 - frac) ** 2) * (end - start)


def shape_values(values, start, end, curve):
    """Reshape an already-linear ramp_values() list in place (a new list,
    `values` itself is untouched) for the given curve name. CURVE_LINEAR
    is a no-op, returned as is -- see _shape_perceptual for the other
    one. Pure, like ramp_values() itself, and kept as its own step so a
    test can check exact reshaped numbers with no socket, thread or clock
    at all."""
    if curve == CURVE_LINEAR:
        return list(values)
    if curve != CURVE_PERCEPTUAL:
        raise ValueError(f"no such curve: {curve!r}; it has to be one of "
                         f"{', '.join(VIDEO_CURVES)}")
    return [_shape_perceptual(v, start, end) for v in values]


def _is_loopback(host):
    """True for 127.0.0.0/8, ::1, or the literal name "localhost". Used to
    refuse binding the heartbeat listener to every interface by accident
    (handoff section 4: "Bind loopback by default")."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# -------------------------------------------------------------- config ----
def _no_typos(doc, keys, where, what):
    unknown = sorted(k for k in doc if k not in keys)
    if unknown:
        raise MadMapperConfigError(
            f"{where}: {what} has no setting "
            f"{', '.join(repr(k) for k in unknown)}; it takes: "
            f"{', '.join(sorted(keys))}.")


def _obj(v, where, what):
    if not isinstance(v, dict):
        raise MadMapperConfigError(f"{where}: {what} must be an object like "
                                   f"{{...}}")
    return v


def _str(doc, key, where, what, default=None, required=False):
    v = doc.get(key, default)
    if v is None and not required:
        return None
    if not isinstance(v, str) or not v.strip():
        raise MadMapperConfigError(f"{where}: {what}.{key} has to be a "
                                   f"name, not {v!r}.")
    return v.strip()


def _port(doc, key, where, what, default):
    v = doc.get(key, default)
    if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 65535:
        raise MadMapperConfigError(f"{where}: {what}.{key} is a port number, "
                                   f"1 to 65535, not {v!r}.")
    return v


def _positive(doc, key, where, what, default, required=False):
    v = doc.get(key, default)
    if v is None and not required:
        return None
    if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
        raise MadMapperConfigError(f"{where}: {what}.{key} has to be a "
                                   f"number greater than zero, not {v!r}.")
    return float(v)


class HeartbeatConfig:
    """Where MadMapper's heartbeat OSC track lands, and how the watchdog
    judges it (handoff section 4; bench B3)."""

    KEYS = frozenset(("bind", "port", "address", "show_len_s", "timeout_s",
                      "drift_ms", "allow_non_loopback_bind"))

    def __init__(self, bind=DEFAULT_HEARTBEAT_BIND, port=DEFAULT_HEARTBEAT_PORT,
                 address=DEFAULT_HEARTBEAT_ADDRESS, show_len_s=None,
                 timeout_s=DEFAULT_TIMEOUT_S, drift_ms=DEFAULT_DRIFT_MS,
                 allow_non_loopback_bind=False):
        self.bind = bind
        self.port = port
        self.address = address
        self.show_len_s = show_len_s
        self.timeout_s = timeout_s
        self.drift_ms = drift_ms
        self.allow_non_loopback_bind = allow_non_loopback_bind

    @classmethod
    def parse(cls, doc, where):
        what = "'madmapper.heartbeat'"
        _obj(doc, where, what)
        _no_typos(doc, cls.KEYS, where, what)
        bind = _str(doc, "bind", where, what, DEFAULT_HEARTBEAT_BIND,
                   required=True)
        allow_non_loopback = bool(doc.get("allow_non_loopback_bind", False))
        if not _is_loopback(bind) and not allow_non_loopback:
            raise MadMapperConfigError(
                f"{where}: {what}.bind is {bind!r}, which is not loopback. "
                f"The heartbeat listener binds loopback only by default "
                f"(handoff section 4): a UDP port open to the whole "
                f"network is not something to do by accident. Set "
                f"{what}.allow_non_loopback_bind to true if this machine "
                f"genuinely needs to listen on {bind!r}.")
        port = _port(doc, "port", where, what, DEFAULT_HEARTBEAT_PORT)
        address = _str(doc, "address", where, what, DEFAULT_HEARTBEAT_ADDRESS,
                      required=True)
        if not address.startswith("/"):
            raise MadMapperConfigError(
                f"{where}: {what}.address has to start with /, like "
                f"\"{DEFAULT_HEARTBEAT_ADDRESS}\", not {address!r}.")
        # required=False (the default) here on purpose, even though this
        # value really is required: _positive(required=True) would raise
        # its own generic "has to be a number greater than zero, not
        # None" the instant it saw a missing key, which would make the
        # much more specific sentence below (why a show length is needed
        # at all) permanently unreachable dead code. Let it return None
        # for a missing key and explain the real reason ourselves; an
        # explicit bad value (0, negative, a string) still gets
        # _positive's own generic refusal, which is adequate there.
        show_len_s = _positive(doc, "show_len_s", where, what, None)
        if show_len_s is None:
            raise MadMapperConfigError(
                f"{where}: {what}.show_len_s is required. The heartbeat's "
                f"value is a fraction of the show (bench B3.2: value times "
                f"the show length is MadMapper's own position), so there "
                f"is no way to read it as seconds without knowing the show "
                f"length.")
        timeout_s = _positive(doc, "timeout_s", where, what, DEFAULT_TIMEOUT_S)
        drift_ms = _positive(doc, "drift_ms", where, what, DEFAULT_DRIFT_MS)
        return cls(bind, port, address, show_len_s, timeout_s, drift_ms,
                   allow_non_loopback)


class MadMapperConfig:
    """The "madmapper" block of a show file, validated.

    `show_bank` and `intermission_bank` are MadMapper's own timeline bank
    names (bench B2.3: kept as "Bank-1" and "Bank-2" in the bench because
    `by_name` did not work, but the name itself is free text on MadMapper's
    side). `intermission_bank` is optional."""

    KEYS = frozenset(("host", "port", "show_bank", "intermission_bank",
                      "surfaces", "fade_s", "ramp_steps", "video_curve",
                      "heartbeat", "notes"))

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT,
                 show_bank="Bank-1", intermission_bank=None, surfaces=(),
                 fade_s=DEFAULT_FADE_S, ramp_steps=DEFAULT_RAMP_STEPS,
                 heartbeat=None, video_curve=CURVE_PERCEPTUAL):
        self.host = host
        self.port = port
        self.show_bank = show_bank
        self.intermission_bank = intermission_bank
        self.surfaces = tuple(surfaces)
        self.fade_s = fade_s
        self.ramp_steps = ramp_steps
        self.video_curve = video_curve
        self.heartbeat = heartbeat or HeartbeatConfig()

    @classmethod
    def parse(cls, doc, where="timeline"):
        what = "'madmapper'"
        _obj(doc, where, what)
        _no_typos(doc, cls.KEYS, where, what)
        host = _str(doc, "host", where, what, DEFAULT_HOST, required=True)
        port = _port(doc, "port", where, what, DEFAULT_PORT)
        show_bank = _str(doc, "show_bank", where, what, "Bank-1",
                        required=True)
        intermission_bank = _str(doc, "intermission_bank", where, what, None)
        if intermission_bank == show_bank:
            raise MadMapperConfigError(
                f"{where}: {what}.show_bank and {what}.intermission_bank "
                f"are both {show_bank!r}. Each chasing bank needs its own "
                f"MadMapper Interface (bench B2.3), so the show and the "
                f"intermission cannot share one name.")
        surfaces = doc.get("surfaces", [])
        if not isinstance(surfaces, list) or not surfaces or \
                not all(isinstance(s, str) and s.strip() for s in surfaces):
            raise MadMapperConfigError(
                f"{where}: {what}.surfaces has to be a list of at least "
                f"one surface name, like [\"Quad-1\", \"Quad-2\"] (bench "
                f"B2: `/surfaces/<name>/opacity` is the real fade to "
                f"black; `master_video_level` does nothing).")
        surfaces = [s.strip() for s in surfaces]
        if len(set(surfaces)) != len(surfaces):
            raise MadMapperConfigError(
                f"{where}: {what}.surfaces lists the same name twice.")
        fade_s = _positive(doc, "fade_s", where, what, DEFAULT_FADE_S)
        steps = doc.get("ramp_steps", DEFAULT_RAMP_STEPS)
        if not isinstance(steps, int) or isinstance(steps, bool) \
                or steps < 2:
            raise MadMapperConfigError(
                f"{where}: {what}.ramp_steps has to be a whole number, 2 "
                f"or more, not {steps!r}.")
        video_curve = doc.get("video_curve", CURVE_PERCEPTUAL)
        if video_curve not in VIDEO_CURVES:
            raise MadMapperConfigError(
                f"{where}: {what}.video_curve is {video_curve!r}; it has "
                f"to be one of {', '.join(VIDEO_CURVES)} (bench B14: a "
                f"plain linear opacity fade looks like it holds, then "
                f"drops late, so 'perceptual' is the default).")
        hb = doc.get("heartbeat")
        hb = HeartbeatConfig() if hb is None else HeartbeatConfig.parse(
            hb, where)
        return cls(host, port, show_bank, intermission_bank, surfaces,
                   fade_s, steps, hb, video_curve)

    def summary(self):
        return (f"{self.host}:{self.port}, show bank {self.show_bank}"
                + (f", intermission {self.intermission_bank}"
                  if self.intermission_bank else "")
                + f", {len(self.surfaces)} surface(s), "
                + f"{self.video_curve} video curve")


# ---------------------------------------------------------------- link ----
class _OscSocket:
    """One UDP socket to MadMapper. Never raises into the caller: the same
    self-healing rule clock.py's TimecodeOut and output.py's Sender both
    follow, scaled down for one destination and no acks to key failure
    detection off of (bench B2.4: MadMapper answers nothing at all, so a
    "failure" here only ever means the OS refused to hand the packet to
    the network, never that MadMapper ignored it)."""

    FAILURES_BEFORE_REOPEN = 3
    REOPEN_BACKOFF_S = 1.0

    def __init__(self, host, port, socket_factory=None,
                clock=time.perf_counter, on_fail=None):
        self.host = host
        self.port = port
        self._factory = socket_factory or self._default_socket
        self._clock = clock
        self.on_fail = on_fail
        self._sock = None
        self._fails = 0
        self._last_open = None
        self.packets_sent = 0
        self.send_errors = 0
        self.last_error = ""
        self.last_error_at = None
        self.last_ok_at = None

    @staticmethod
    def _default_socket():
        return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

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
        return self._sock

    def _fail(self, now, msg):
        self.send_errors += 1
        self.last_error = msg
        self.last_error_at = now
        if self.on_fail:
            try:
                self.on_fail(msg)
            except Exception:
                pass

    def send(self, address, args=()):
        now = self._clock()
        sock = self._ensure(now)
        if sock is None:
            return False
        pkt = encode(address, args)
        try:
            sock.sendto(pkt, (self.host, self.port))
        except OSError as e:
            self._fail(now, f"{address}: {e}")
            self._fails += 1
            if self._fails >= self.FAILURES_BEFORE_REOPEN:
                self._fails = 0
                self.close()
            return False
        self.packets_sent += 1
        self._fails = 0
        self.last_ok_at = now
        return True

    def close(self):
        s, self._sock = self._sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


class Link:
    """MadMapper's OSC transport: bank select, conductor play/stop, the
    master audio level and per-surface opacity ramps, and nothing else.
    Pure device layer -- no sequencing, no scheduler wiring. See the
    module docstring for why.

    Every send and every ramp runs on this object's own worker thread,
    never on the caller's, so a 1 s ramp never blocks whatever called
    fade_audio()/fade_surfaces() a moment longer than it has to, and two
    calls issued back to back run in the order they were issued rather
    than race each other on the same socket."""

    def __init__(self, cfg, socket_factory=None, clock=time.perf_counter,
                sleep=time.sleep, journal=None):
        self.cfg = cfg
        self._clock = clock
        self._sleep = sleep
        self.journal = journal
        self._osc = _OscSocket(cfg.host, cfg.port,
                               socket_factory=socket_factory, clock=clock,
                               on_fail=self._on_send_fail)
        self._q = _queue.Queue()
        self._closed = False
        self._gen_lock = threading.Lock()
        self._gen = 0
        # An instance attribute, not just the module constant, so a test
        # can shorten it and force a real timeout in well under a second
        # rather than waiting out the real SUBMIT_TIMEOUT_S.
        self._submit_timeout_s = SUBMIT_TIMEOUT_S
        self._worker = threading.Thread(target=self._run, daemon=True,
                                        name="ltcplay-madmapper")
        self._worker.start()

    # -- plumbing ---------------------------------------------------------
    def _on_send_fail(self, msg):
        self._note(f"A MadMapper command failed: {msg}.", action="command",
                  outcome="failed", fault=True)

    def _note(self, text, **extra):
        if self.journal:
            try:
                self.journal(text, **extra)
            except Exception:
                pass

    def _run(self):
        while True:
            job = self._q.get()
            if job is None:
                return
            fn, done = job
            try:
                fn()
            except Exception as e:
                self._note(f"A MadMapper command raised "
                          f"{type(e).__name__}: {e}.", action="command",
                          outcome="error", fault=True)
            finally:
                if done is not None:
                    done.set()

    def _submit(self, fn, wait=True):
        if self._closed:
            return
        done = threading.Event() if wait else None
        self._q.put((fn, done))
        if wait:
            got = done.wait(self._submit_timeout_s)
            if not got:
                self._note(
                    f"A MadMapper command did not finish within "
                    f"{self._submit_timeout_s:g} s. It may still be "
                    f"running.", action="command", outcome="timeout",
                    fault=True)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._q.put(None)
        if self._worker.is_alive():
            self._worker.join(timeout=2.0)
        self._osc.close()

    # -- ramp cancellation --------------------------------------------------
    def _bump_gen(self):
        with self._gen_lock:
            self._gen += 1
            return self._gen

    def _gen_current(self):
        with self._gen_lock:
            return self._gen

    def cancel(self):
        """Stop whatever ramp is in flight at once (S3). Also called
        implicitly by fade_audio()/fade_surfaces() themselves: starting a
        new ramp always cancels whatever old one was still running,
        rather than queuing behind it."""
        self._bump_gen()

    # -- raw transport, always via the worker ------------------------------
    def _send(self, address, *args):
        self._osc.send(address, args)

    @staticmethod
    def _bank_addr(name, tail):
        return f"/timelines/{name}/{tail}"

    def select_bank(self, name, wait=True):
        self._submit(lambda: self._send(self._bank_addr(name, "select"),
                                        True), wait=wait)

    def stop_bank(self, name, wait=True):
        """On a non-chasing bank (the intermission), this is a PAUSE, not
        a rewind (bench B14): the playhead stays exactly where it stopped.
        A later play() resumes from there, not from zero -- to actually
        restart a bank at its own beginning, call play_from_beginning(),
        never play() after stop_bank()."""
        self._submit(
            lambda: self._send(self._bank_addr(name, "conductor/stop")),
            wait=wait)

    def play(self, name, wait=True):
        """Resumes wherever the bank's playhead currently sits -- see
        stop_bank()'s own docstring. Never use this to start the
        intermission; use play_from_beginning()."""
        self._submit(
            lambda: self._send(self._bank_addr(name, "conductor/play"),
                              True), wait=wait)

    def play_from_beginning(self, name, wait=True):
        """The only primitive that actually starts a bank at zero. The
        intermission must always be started this way (bench B14: its own
        stop_bank() never rewinds it)."""
        self._submit(lambda: self._send(
            self._bank_addr(name, "conductor/play_from_beginning")),
            wait=wait)

    # -- instant (non-ramped) levels ----------------------------------------
    def set_audio(self, value, wait=True):
        self._submit(lambda: self._send(AUDIO_ADDR, float(value)), wait=wait)

    def set_surfaces(self, value, wait=True):
        addrs = self._surface_addrs()
        self._submit(lambda: [self._send(a, float(value)) for a in addrs],
                    wait=wait)

    def restore_levels(self, wait=True):
        """MadMapper keeps whatever level a fade last left it at (nothing
        in the bench ever showed it resetting on its own). After Abort or
        Closing, both master_audio_level and every surface's opacity sit
        at 0 until something explicitly sets them back -- this is that
        something. A pure device-layer primitive: it does not know or care
        why it is being called, only that "back to normal" means audio and
        every surface at 1.0. See the module docstring: WHEN to call this
        is the conductor's job, not this module's."""
        self.set_audio(1.0, wait=wait)
        self.set_surfaces(1.0, wait=wait)

    # -- ramps --------------------------------------------------------------
    def _ramp(self, addresses, start, end, seconds, steps, gen,
             curve=CURVE_LINEAR):
        """Send `addresses` the same ramped value together, every step,
        paced by absolute deadlines from a single start time (the same
        shape clock.py's Ticker uses, and for the same reason: a late wake
        must never let sleep error accumulate across many steps).
        Cancellable: checks `gen` against the Link's own current
        generation before every step, including the first, and stops at
        once if a newer ramp (or an explicit cancel()) has superseded it.
        `curve` reshapes the otherwise-linear values (see shape_values());
        fade_audio() never passes anything but CURVE_LINEAR."""
        values = shape_values(ramp_values(start, end, steps), start, end,
                             curve)
        interval = seconds / (steps - 1) if steps > 1 else 0.0
        t0 = self._clock()
        sent = []
        for i, v in enumerate(values):
            if self._gen_current() != gen:
                break
            for addr in addresses:
                self._send(addr, float(v))
            sent.append(v)
            if i < len(values) - 1:
                due = t0 + (i + 1) * interval
                now = self._clock()
                if now < due:
                    self._sleep(due - now)
        return sent

    def _surface_addrs(self):
        return [f"/surfaces/{s}/opacity" for s in self.cfg.surfaces]

    def fade_audio(self, start, end, seconds=None, steps=None, wait=True):
        """Always linear (bench B14: audio measured even that way); see
        fade_surfaces() for the video curve."""
        seconds = self.cfg.fade_s if seconds is None else seconds
        steps = self.cfg.ramp_steps if steps is None else steps
        gen = self._bump_gen()
        self._submit(lambda: self._ramp([AUDIO_ADDR], start, end, seconds,
                                        steps, gen, curve=CURVE_LINEAR),
                    wait=wait)

    def fade_surfaces(self, start, end, seconds=None, steps=None, wait=True,
                      curve=None):
        """`curve` defaults to the show file's own `madmapper.video_curve`
        (CURVE_PERCEPTUAL unless configured otherwise -- see
        shape_values()); pass CURVE_LINEAR explicitly to bypass it for one
        call."""
        curve = self.cfg.video_curve if curve is None else curve
        seconds = self.cfg.fade_s if seconds is None else seconds
        steps = self.cfg.ramp_steps if steps is None else steps
        gen = self._bump_gen()
        self._submit(lambda: self._ramp(self._surface_addrs(), start, end,
                                        seconds, steps, gen, curve=curve),
                    wait=wait)

    def fade_all(self, start, end, seconds=None, steps=None, wait=True,
                surface_curve=None):
        """Fade the master audio level (always linear, like fade_audio())
        and every surface's opacity (the configured video curve, like
        fade_surfaces()) TOGETHER, in the same real time.

        This exists because fade_audio() and fade_surfaces() each submit
        their own job to this Link's single worker thread: calling them
        back to back sends the audio ramp to completion BEFORE the surface
        ramp even starts, not "together". The handoff's Abort wording
        (section 4a, Jeff 2026-09-27) is explicit that music, video and
        pixels fade to black "together over 1 s" -- this is that single,
        combined ramp, still one worker job, still cancellable the same
        way (a newer ramp, or cancel(), stops it at the next step)."""
        surface_curve = (self.cfg.video_curve if surface_curve is None
                         else surface_curve)
        seconds = self.cfg.fade_s if seconds is None else seconds
        steps = self.cfg.ramp_steps if steps is None else steps
        gen = self._bump_gen()

        def _run():
            audio_values = ramp_values(start, end, steps)
            surface_values = shape_values(ramp_values(start, end, steps),
                                          start, end, surface_curve)
            addrs = self._surface_addrs()
            interval = seconds / (steps - 1) if steps > 1 else 0.0
            t0 = self._clock()
            for i in range(len(audio_values)):
                if self._gen_current() != gen:
                    break
                self._send(AUDIO_ADDR, float(audio_values[i]))
                for addr in addrs:
                    self._send(addr, float(surface_values[i]))
                if i < len(audio_values) - 1:
                    due = t0 + (i + 1) * interval
                    now = self._clock()
                    if now < due:
                        self._sleep(due - now)
        self._submit(_run, wait=wait)


# ------------------------------------------------------------- watchdog ----
class Watchdog:
    """Listens for MadMapper's heartbeat OSC track (bench B3) and answers
    the health panel's "MadMapper link" dot (handoff section 8): last
    heartbeat age in milliseconds, state, and drift.

    Armed and disarmed explicitly, by whatever calls arm()/disarm() --
    this module never infers either from a scheduler state; that decision
    belongs to the future conductor. Silence is completely normal while
    disarmed: on Hold, between shows, or on the intermission bank, which
    has no track of its own in Jeff's model (bench B3.3). While ARMED, no
    packet for `timeout_s` (default 3.0, handoff section 4) is the fault
    section 4 describes: logged once, at actor "madmapper", in a plain
    sentence, and again the moment a packet returns.

    A second, independent check: every packet's value, times `show_len_s`,
    is MadMapper's own reported position in seconds (bench B3.2). Compare
    it against ltcplay's own timecode position -- fed in by note_position(),
    since this module never reads the clock itself -- and flag more than
    `drift_ms` (default 100 ms) of daylight as a second health sentence.
    A NaN value (a corrupt or malformed float) counts as bad and is also
    flagged as a drift fault rather than silently accepted as "0 drift".

    Bench B9's 4 hour soak found MadMapper sends exactly one heartbeat
    packet BETWEEN shows: it re-sends the Float track's last value, value
    1.0, the instant `/timelines/Bank-1/select` arrives, about 2 s before
    the real show starts. Any real wiring arms the watchdog at or before
    that select (the bank has to be selected before the show can start),
    so arm() alone is not enough to keep that lone, stale packet from being
    read as a live position. arm() therefore also opens an "awaiting its
    first real packet" window: every packet is watched but not counted,
    not read as a position, and never compared for drift, until one
    arrives within START_WINDOW_S seconds of the show's own start (bench:
    a real start reads near 0). Only then does normal tracking begin. A
    show that never actually starts still alarms on schedule, because
    arm() itself sets a fresh timeout grace window regardless.

    A second, narrower version of the same problem, found on the Pico
    against the real MadMapper and BEYOND (bench B14): the very first
    heartbeat after a genuine Hold/Resume can itself carry a stale
    position for one packet -- MadMapper reported 5016 ms of "drift", then
    "back in step" 9 ms later, once its own next packet caught up. So
    DRIFT_SETTLE_PACKETS worth of packets right after arm()'s own
    awaiting-start window clears, AND right after any recovery from a
    silence alarm, are still counted and still clear the alarm (the video
    really is back), but are never judged for drift -- exactly the same
    logic as "ignore until near 0 after arm" (S1), extended to "ignore a
    couple more once things are moving again" for the same reason: a
    single stale reading right at a discontinuity is not a real drift
    fault, and reporting one as though it were teaches an operator to
    ignore the alarm."""

    DRIFT_SETTLE_PACKETS = 1

    def __init__(self, cfg=None, clock=time.perf_counter, sleep=time.sleep,
                socket_factory=None, journal=None, poll_s=0.1):
        cfg = cfg or HeartbeatConfig()
        self.cfg = cfg
        self._clock = clock
        self._sleep = sleep
        self._factory = socket_factory or self._default_socket
        self.journal = journal
        self.poll_s = poll_s
        self._sock = None
        self._stop = threading.Event()
        self._listen_thread = None
        self._poll_thread = None
        self._lock = threading.Lock()
        self.armed = False
        self._awaiting_start = False
        self._settle_count = 0
        self._show = None
        self._armed_at = None
        self._last_packet_at = None
        self._last_value = None
        self._alarmed = False
        self.packets_in = 0
        self._ltcplay_pos = None       # (seconds, at) from note_position()
        self.last_drift_ms = None
        self.drift_flagged = False
        self.bind_error = None

    def _default_socket(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((self.cfg.bind, self.cfg.port))
        s.settimeout(0.25)
        return s

    # -- running ------------------------------------------------------------
    def start(self):
        if self._listen_thread is not None:
            return True
        self._stop.clear()
        try:
            self._sock = self._factory()
        except OSError as e:
            # Its own sentence, naming the heartbeat port specifically:
            # a bind failure here must never read as the web server's own
            # port being unavailable, which is a different problem with a
            # different fix.
            self.bind_error = (
                f"The MadMapper heartbeat could not listen on "
                f"{self.cfg.bind}:{self.cfg.port}: {e}. No heartbeat "
                f"means the video-frozen alarm cannot work until this is "
                f"fixed.")
            self._note(self.bind_error, action="watchdog", outcome="failed",
                      fault=True)
            return False
        self.bind_error = None
        self._listen_thread = threading.Thread(
            target=self._listen, daemon=True, name="ltcplay-madmapper-hb")
        self._listen_thread.start()
        self._poll_thread = threading.Thread(
            target=self._poll, daemon=True, name="ltcplay-madmapper-hb-poll")
        self._poll_thread.start()
        return True

    def stop(self):
        self._stop.set()
        for t in (self._listen_thread, self._poll_thread):
            if t is not None and t.is_alive():
                t.join(timeout=1.0)
        self._listen_thread = None
        self._poll_thread = None
        s, self._sock = self._sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

    def _listen(self):
        while not self._stop.is_set():
            try:
                data, _addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            got = decode_float(data)
            if got is None or got[0] != self.cfg.address:
                continue
            self.on_packet(got[1])

    def _poll(self):
        while not self._stop.wait(self.poll_s):
            self._check()

    # -- what a real socket, or a test, feeds in ---------------------------
    def on_packet(self, value, at=None):
        """One heartbeat packet: `value` 0 to 1, MadMapper's own fraction
        of the show. Public (not just reached through the socket) so a
        test can feed packets on an injected clock with no real UDP at
        all.

        Ignored outright while disarmed, and -- see the class docstring --
        also ignored while "awaiting start" even though armed, until a
        packet reads near the show's own beginning. Neither case counts
        the packet, sets the last-packet time, or is ever compared for
        drift; a NaN value is treated as a drift fault instead of silently
        read as 0 drift. The first DRIFT_SETTLE_PACKETS packet(s) once
        awaiting-start clears, and again right after any recovery from a
        silence alarm, DO count and DO clear the alarm, but are never
        judged for drift either (bench B14: the very first packet back
        can itself be stale)."""
        now = self._clock() if at is None else at
        recovered = False
        drift_note = None
        skip_drift = False
        with self._lock:
            if not self.armed:
                return
            show_len_s = self.cfg.show_len_s
            if self._awaiting_start:
                near_start = (show_len_s is not None
                             and isinstance(value, (int, float))
                             and not math.isnan(value)
                             and value * show_len_s <= START_WINDOW_S)
                if not near_start:
                    return
                self._awaiting_start = False
                self._settle_count = self.DRIFT_SETTLE_PACKETS
            self.packets_in += 1
            self._last_packet_at = now
            self._last_value = value
            if self._alarmed:
                self._alarmed = False
                recovered = True
                self._settle_count = self.DRIFT_SETTLE_PACKETS
            if self._settle_count > 0:
                self._settle_count -= 1
                skip_drift = True
            pos = self._ltcplay_pos
        if recovered:
            self._note(f"MadMapper answered again, {self.packets_in} "
                      f"packet(s) in.", action="watchdog",
                      outcome="recovered", show=self._show)
        if skip_drift:
            return
        if show_len_s and pos is not None:
            ltc_s, ltc_at = pos
            bad = not isinstance(value, (int, float)) or math.isnan(value)
            mm_s = None if bad else value * show_len_s
            ltc_now = ltc_s + (now - ltc_at)
            drift_ms = None if bad else (mm_s - ltc_now) * 1000.0
            with self._lock:
                self.last_drift_ms = drift_ms
                was_flagged = self.drift_flagged
                flagged = bad or abs(drift_ms) > self.cfg.drift_ms
                self.drift_flagged = flagged
            if flagged and not was_flagged:
                drift_note = (
                    ("MadMapper's heartbeat value is not a usable number "
                     "(NaN)." if bad else
                     f"MadMapper is {abs(drift_ms):.0f} ms "
                     f"{'ahead of' if drift_ms > 0 else 'behind'} "
                     f"ltcplay's own timecode."), "drift")
            elif was_flagged and not flagged:
                drift_note = ("MadMapper's position is back in step with "
                             "ltcplay's own timecode.", "drift clear")
        if drift_note:
            self._note(drift_note[0], action="watchdog",
                      outcome=drift_note[1], show=self._show,
                      fault=(drift_note[1] == "drift"))

    def note_position(self, seconds, at=None):
        """ltcplay's own timecode position, for the drift check. Nothing
        in this module calls this itself -- see the class docstring; it
        is fed in by whoever also owns the real clock."""
        with self._lock:
            self._ltcplay_pos = (seconds, self._clock() if at is None
                                else at)

    def arm(self, show=None):
        with self._lock:
            self.armed = True
            self._awaiting_start = True
            self._settle_count = 0
            self._show = show
            now = self._clock()
            self._armed_at = now
            self._last_packet_at = now       # a fresh grace window, not an
                                             # instant alarm
            self._alarmed = False

    def disarm(self):
        with self._lock:
            self.armed = False
            self._awaiting_start = False
            self._settle_count = 0
            self._alarmed = False
            self._last_packet_at = None
            self._armed_at = None

    def _check(self):
        now = self._clock()
        show = None
        since = None
        with self._lock:
            if not self.armed or self._last_packet_at is None \
                    or self._alarmed:
                return
            if now - self._last_packet_at <= self.cfg.timeout_s:
                return
            self._alarmed = True
            show = self._show
            if self._armed_at is not None:
                since = now - self._armed_at
        where = f"show {show}" if show else "the show"
        when = (f"{since:.0f} s into {where}" if since is not None
               else where)
        self._note(f"MadMapper stopped answering {when}. Video may be "
                  f"frozen. The rest of the show carries on.",
                  action="watchdog", outcome="fault", show=show, fault=True)

    def _note(self, text, **extra):
        if self.journal:
            try:
                self.journal(text, **extra)
            except Exception:
                pass

    def health(self):
        with self._lock:
            now = self._clock()
            age_ms = (None if self._last_packet_at is None
                     else (now - self._last_packet_at) * 1000.0)
            state = ("fault" if self._alarmed else
                    "ok" if self.armed else "quiet")
            return {"armed": self.armed, "state": state, "age_ms": age_ms,
                    "drift_ms": self.last_drift_ms,
                    "drift_flagged": self.drift_flagged,
                    "packets_in": self.packets_in,
                    "bind_error": self.bind_error}


# --------------------------------------------------------------- build ----
def build(cfg, journal=None, clock=time.perf_counter, sleep=time.sleep,
         link_socket_factory=None, watchdog_socket_factory=None):
    """A (Link, Watchdog) pair from a validated MadMapperConfig, wired to
    one journal callback -- the only place the actor "madmapper" is ever
    attached, by whoever calls build() (see web.py's serve())."""
    watchdog = Watchdog(cfg.heartbeat, clock=clock, sleep=sleep,
                        socket_factory=watchdog_socket_factory,
                        journal=journal)
    link = Link(cfg, socket_factory=link_socket_factory, clock=clock,
               sleep=sleep, journal=journal)
    return link, watchdog
