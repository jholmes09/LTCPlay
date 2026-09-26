"""MadMapper: the OSC transport, the heartbeat watchdog, and ltcplay's own
half of Hold, Resume, Abort and End night (the music fade, and, for Abort
and End night, the video fade to black too).

Imported ONLY when a show file has a "madmapper" block. The GPL show at
Dollywood never has one, so on the Mac none of this is loaded -- the same
inertness clock.py, schedule_service.py and announce.py each already rely
on, proven the same way: see test_the_gpl_path_never_loads_madmapper.

Everything below follows the Pico bench report, 2026-09-25
(bench/bench_report_2026-09-25.md on branch bench/2026-09-25), sections B1
to B4, which override the handoff wherever the two differ. The facts that
shape this module:

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

  `/master/master_audio_level` is a float 0 to 1, linear, and jumps with no
  ramp of its own (B2.2): the smoothness is entirely the sender's job, 31
  steps over 1 s in the bench. `/master/master_video_level` did nothing
  (B2.2); `/surfaces/<name>/opacity` is the real fade to black, one message
  per surface per step.

  Hold (B4): fade the music out over `fade_s`, THEN freeze the clock --
  never the other way around. MadMapper's own audio runs on 0.3 to 0.4 s
  after the last new frame and would otherwise be heard fading into
  silence, or worse, repeating on resume. Freezing the clock is NOT this
  module's job (see Link.hold's docstring); ltcplay's own clock.py
  (ArtNetMaster.pause/resume, PR #10) owns that, and nothing wires the two
  together yet -- an open question, not a gap papered over. Resume is the
  mirror image: the clock first, then the fade back up, hiding MadMapper's
  own 5-frame audio repeat (B4.3).

  Abort (Jeff, 2026-09-24): flame cues and lasers are somebody else's job,
  done first and instantly; music, video and pixels fade to black TOGETHER
  over `fade_s`; then the conductor stops. No intermission comes back.

  The heartbeat (B3) is an OSC Float track MadMapper renders once a frame,
  about 60/s, whose value times the show length is MadMapper's own reported
  position. It sends nothing while the timecode is frozen, after the show
  ends, or on a bank with no track of its own (the intermission, in Jeff's
  model): silence there is NORMAL and must never alarm. It answers no
  ack either -- see Watchdog's class docstring for exactly when "no pulse
  for `timeout_s`" is, and is not, a fault.
"""
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

# schedule.py's own state and event names, copied here as plain strings
# rather than imported. This module is wired to the scheduler only through
# web.py's serve(), exactly the way announce.py is wired (see its module
# docstring): neither ever imports schedule.py or schedule_service.py, so a
# show file with no "madmapper" block never even imports socket code for
# this. If schedule.py ever renames one of these, the mismatch is silent
# here (a transition simply goes unrecognised) -- the same trade
# announce.py already makes with BLOCKED_STATES.
_SHOW = "SHOW"
_PAUSED = "PAUSED"
_CLOSING = "CLOSING"
_ABORT_LIKE = frozenset(("ABORT", "SHOW_FAILED"))


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
                      "drift_ms"))

    def __init__(self, bind=DEFAULT_HEARTBEAT_BIND, port=DEFAULT_HEARTBEAT_PORT,
                 address=DEFAULT_HEARTBEAT_ADDRESS, show_len_s=None,
                 timeout_s=DEFAULT_TIMEOUT_S, drift_ms=DEFAULT_DRIFT_MS):
        self.bind = bind
        self.port = port
        self.address = address
        self.show_len_s = show_len_s
        self.timeout_s = timeout_s
        self.drift_ms = drift_ms

    @classmethod
    def parse(cls, doc, where):
        what = "'madmapper.heartbeat'"
        _obj(doc, where, what)
        _no_typos(doc, cls.KEYS, where, what)
        bind = _str(doc, "bind", where, what, DEFAULT_HEARTBEAT_BIND,
                   required=True)
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
        return cls(bind, port, address, show_len_s, timeout_s, drift_ms)


class MadMapperConfig:
    """The "madmapper" block of a show file, validated.

    `show_bank` and `intermission_bank` are MadMapper's own timeline bank
    names (bench B2.3: kept as "Bank-1" and "Bank-2" in the bench because
    `by_name` did not work, but the name itself is free text on MadMapper's
    side). `intermission_bank` is optional: a show with no intermission
    timeline simply never gets one selected back after a show ends."""

    KEYS = frozenset(("host", "port", "show_bank", "intermission_bank",
                      "surfaces", "fade_s", "ramp_steps", "heartbeat",
                      "notes"))

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT,
                 show_bank="Bank-1", intermission_bank=None, surfaces=(),
                 fade_s=DEFAULT_FADE_S, ramp_steps=DEFAULT_RAMP_STEPS,
                 heartbeat=None):
        self.host = host
        self.port = port
        self.show_bank = show_bank
        self.intermission_bank = intermission_bank
        self.surfaces = tuple(surfaces)
        self.fade_s = fade_s
        self.ramp_steps = ramp_steps
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
                f"black; `master_video_level` does nothing). Abort and "
                f"Closing both need it.")
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
        hb = doc.get("heartbeat")
        hb = HeartbeatConfig() if hb is None else HeartbeatConfig.parse(
            hb, where)
        return cls(host, port, show_bank, intermission_bank, surfaces,
                   fade_s, steps, hb)

    def summary(self):
        return (f"{self.host}:{self.port}, show bank {self.show_bank}"
                + (f", intermission {self.intermission_bank}"
                  if self.intermission_bank else "")
                + f", {len(self.surfaces)} surface(s)")


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
    master audio level and per-surface opacity ramps, and the sequencing
    that belongs to Hold, Resume, Abort and Closing (handoff section 5,
    Jeff's decisions of 2026-09-23/24; bench B2/B4).

    Every send and every ramp runs on this object's own worker thread,
    never on the caller's. The scheduler's hook-dispatch thread is already
    off Service.lock by the time it calls in here (schedule_service.py's
    _locked() runs each pending hook on a thread of its own -- see its
    docstring), but a 1 second ramp still has no business blocking whatever
    called hold()/resume()/abort() a moment longer than it has to, and two
    fades issued back to back (a fast Hold immediately followed by a
    Resume) have to run in the order they were issued rather than race each
    other on the same socket -- a single worker, fed through a queue, is
    what makes that true regardless of which thread calls in."""

    def __init__(self, cfg, socket_factory=None, clock=time.perf_counter,
                sleep=time.sleep, journal=None, watchdog=None, beyond=None):
        self.cfg = cfg
        self._clock = clock
        self._sleep = sleep
        self.journal = journal
        self.watchdog = watchdog
        # The laser link (beyond.py), or None -- the laser blank waited on
        # a BEYOND bench result (open gate, handoff section 14) and is now
        # unblocked (bench B8, 2026-09-26). Optional so this class works
        # standalone with no lasers at all, exactly like `watchdog`.
        self.beyond = beyond
        self._osc = _OscSocket(cfg.host, cfg.port,
                               socket_factory=socket_factory, clock=clock,
                               on_fail=self._on_send_fail)
        self._q = _queue.Queue()
        self._closed = False
        self._worker = threading.Thread(target=self._run, daemon=True,
                                        name="ltcplay-madmapper")
        self._worker.start()

    # -- plumbing ---------------------------------------------------------
    def _on_send_fail(self, msg):
        self._note(f"A MadMapper command failed: {msg}.", action="command",
                  outcome="failed")

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
                          outcome="error")
            finally:
                if done is not None:
                    done.set()

    def _submit(self, fn, wait=True):
        if self._closed:
            return
        done = threading.Event() if wait else None
        self._q.put((fn, done))
        if wait:
            done.wait()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._q.put(None)
        if self._worker.is_alive():
            self._worker.join(timeout=2.0)
        self._osc.close()

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
        self._submit(
            lambda: self._send(self._bank_addr(name, "conductor/stop")),
            wait=wait)

    def play_bank(self, name, wait=True):
        self._submit(
            lambda: self._send(self._bank_addr(name, "conductor/play"),
                              True), wait=wait)

    def play_from_beginning(self, name, wait=True):
        self._submit(lambda: self._send(
            self._bank_addr(name, "conductor/play_from_beginning")),
            wait=wait)

    # -- ramps --------------------------------------------------------------
    def _ramp(self, addresses, start, end, seconds, steps):
        """Send `addresses` the same ramped value together, every step,
        paced by absolute deadlines from a single start time (the same
        shape clock.py's Ticker uses, and for the same reason: a late wake
        must never let sleep error accumulate across 31 steps). Returns
        the values sent, for tests that check the ramp itself with no real
        clock at all."""
        values = ramp_values(start, end, steps)
        interval = seconds / (steps - 1) if steps > 1 else 0.0
        t0 = self._clock()
        for i, v in enumerate(values):
            for addr in addresses:
                self._send(addr, float(v))
            if i < len(values) - 1:
                due = t0 + (i + 1) * interval
                now = self._clock()
                if now < due:
                    self._sleep(due - now)
        return values

    def _surface_addrs(self):
        return [f"/surfaces/{s}/opacity" for s in self.cfg.surfaces]

    def fade_audio(self, start, end, seconds=None, steps=None, wait=True):
        seconds = self.cfg.fade_s if seconds is None else seconds
        steps = self.cfg.ramp_steps if steps is None else steps
        self._submit(lambda: self._ramp([AUDIO_ADDR], start, end, seconds,
                                        steps), wait=wait)

    def fade_video(self, start, end, seconds=None, steps=None, wait=True):
        seconds = self.cfg.fade_s if seconds is None else seconds
        steps = self.cfg.ramp_steps if steps is None else steps
        self._submit(lambda: self._ramp(self._surface_addrs(), start, end,
                                        seconds, steps), wait=wait)

    def fade_out(self, seconds=None, steps=None, wait=True):
        """Audio AND every configured surface, together, from 1 to 0 over
        the same ramp (Abort and Closing, section 5): one loop, one step
        count, so the two can never drift apart from each other the way
        two separately-timed ramps could."""
        seconds = self.cfg.fade_s if seconds is None else seconds
        steps = self.cfg.ramp_steps if steps is None else steps
        addrs = [AUDIO_ADDR] + self._surface_addrs()
        self._submit(lambda: self._ramp(addrs, 1.0, 0.0, seconds, steps),
                    wait=wait)

    # -- the show-level actions --------------------------------------------
    def show_started(self, show):
        """Section 4: select the show bank. Stopping the intermission bank
        first is Jeff's own model (bench B2.3): stop the intermission,
        select the show bank, then start timecode at zero -- the last part
        is ltcplay's own clock, not this module's job. Unblanking BEYOND
        here too covers a fresh show starting right after an Abort, which
        leaves the rig dark on purpose (see abort()) until the next show
        starts -- this is that moment."""
        if self.cfg.intermission_bank:
            self.stop_bank(self.cfg.intermission_bank)
        self.select_bank(self.cfg.show_bank)
        if self.beyond is not None:
            self.beyond.unblank(show)
        self._note(f"MadMapper selected for show {show}: bank "
                  f"{self.cfg.show_bank}.", action="show started", show=show)

    def show_ended(self, show):
        """A show finished on its own -- never after an Abort or a failed
        start; see abort(). Stop the show bank so it sits at zero (bench
        B2.3's own recommendation, to hide the 0.2 s of stale audio the
        bench measured at the next switch) and bring the intermission
        back."""
        self.stop_bank(self.cfg.show_bank)
        if self.cfg.intermission_bank:
            self.select_bank(self.cfg.intermission_bank)
            self.play_from_beginning(self.cfg.intermission_bank)
        self._note(f"Show {show} ended. MadMapper's show bank was stopped"
                  + (" and the intermission is back."
                     if self.cfg.intermission_bank else "."),
                  action="show ended", show=show)

    def hold(self, show, clock=None):
        """Jeff/Andy, 2026-09-26, building on bench B4 and B8.2: blank the
        lasers AT ONCE -- the same moment the flame cues go to zero,
        elsewhere, never after the music fade -- then fade the music out
        over `fade_s`, THEN -- and only then -- freeze the clock, so
        MadMapper's own audio run-on (0.3 to 0.4 s after the last new
        frame) plays into silence instead of being heard. `clock` is
        anything with a pause() method; it is None until whoever wires a
        running Session's clock to the scheduler passes one in here -- not
        part of this change; see the PR body's open question."""
        if self.beyond is not None:
            self.beyond.blank(show)
        self.fade_audio(1.0, 0.0, wait=True)
        if clock is not None:
            clock.pause()
        self._note(f"Show {show} on hold. MadMapper's music faded out over "
                  f"{self.cfg.fade_s:g} s.", action="hold", show=show)

    def resume(self, show, clock=None):
        """The mirror of hold(): the clock first, so the frozen frame is
        moving again before anything is audible, then unblank the lasers,
        then the fade back up -- hiding MadMapper's own 5-frame audio
        repeat on resume (bench B4.3). See beyond.py's own module
        docstring for why unblanking right after the clock restarts,
        rather than before it or after waiting out BEYOND's own second of
        run-on, is the safer of the two orders: with "Keep running" off
        (bench B8.2), BEYOND is already dark on its own the instant the
        clock stops, so nothing is exposed early by unblanking the moment
        it starts again -- unblanking BEFORE the clock restarts is what
        would risk showing a static beam."""
        if clock is not None:
            clock.resume()
        if self.beyond is not None:
            self.beyond.unblank(show)
        self.fade_audio(0.0, 1.0, wait=True)
        self._note(f"Show {show} resumed. MadMapper's music faded back up "
                  f"over {self.cfg.fade_s:g} s.", action="resume", show=show)

    def abort(self, show):
        """Jeff, 2026-09-24 (lasers added 2026-09-26): flame cues and
        lasers both go at once, first thing -- flame cues are somebody
        else's job, but the laser blank is this module's, and it happens
        here before anything else, never waiting on the fade; then music,
        video and pixels fade to black together over `fade_s`, then the
        conductor stops. No intermission comes back: the rig stays dark
        (see on_transition)."""
        if self.beyond is not None:
            self.beyond.blank(show)
        self.fade_out(wait=True)
        self.stop_bank(self.cfg.show_bank)
        self._note(f"Show {show} aborted. MadMapper faded to black over "
                  f"{self.cfg.fade_s:g} s and stopped. Nothing was "
                  f"disarmed.", action="abort", show=show)

    def closing(self):
        """End night, or the schedule's own CLOSING once the last show is
        done: blank the lasers, fade to black, then stop -- the same shape
        as abort(), with no show number and no assumption about which bank
        was live."""
        if self.beyond is not None:
            self.beyond.blank()
        self.fade_out(wait=True)
        for name in (self.cfg.show_bank, self.cfg.intermission_bank):
            if name:
                self.stop_bank(name)
        self._note(f"Closing for the night. MadMapper faded to black over "
                  f"{self.cfg.fade_s:g} s and stopped.", action="closing")

    # -- the scheduler bridge -----------------------------------------------
    def on_transition(self, before_state, after_state, event_kind, show,
                      clock=None):
        """Called by schedule_service.py's Service.on_transition hook (see
        that module and web.py's serve(), which is the only place this is
        wired) after every real state change the scheduler makes, on a
        thread of its own, never Service.lock. `clock`, when given, is
        anything with pause()/resume() -- see hold()/resume() above; no
        current wiring passes one, so today this always freezes/resumes
        nothing and only the MadMapper-side action happens.

        A failed start (SHOW_FAILED, within the confirm window) is treated
        exactly like an operator ABORT (section 5: "The same applies after
        a failed start or a restart during a show."): both leave the rig
        dark, so both go through abort(), never show_ended()."""
        if after_state == before_state:
            return
        wd = self.watchdog
        if event_kind in _ABORT_LIKE:
            if wd is not None:
                wd.disarm()
            self.abort(show)
            return
        if after_state == _PAUSED:
            if wd is not None:
                wd.disarm()
            self.hold(show, clock=clock)
            return
        if before_state == _PAUSED and after_state == _SHOW:
            if wd is not None:
                wd.arm(show)
            self.resume(show, clock=clock)
            return
        if after_state == _SHOW:
            if wd is not None:
                wd.arm(show)
            self.show_started(show)
            return
        if after_state == _CLOSING:
            if wd is not None:
                wd.disarm()
            self.closing()
            return
        # STANDBY, HOLD, IDLE, OFF: no heartbeat is ever expected here.
        if wd is not None:
            wd.disarm()
        if before_state == _SHOW and event_kind == "SHOW_ENDED":
            self.show_ended(show)


# ------------------------------------------------------------- watchdog ----
class Watchdog:
    """Listens for MadMapper's heartbeat OSC track (bench B3) and answers
    the health panel's "MadMapper link" dot (handoff section 8): last
    heartbeat age in milliseconds, state, and drift.

    Armed only while ltcplay is actually running the clock for a show bank
    that has a heartbeat track configured -- arm()/disarm(), called by
    Link.on_transition. Silence is completely normal while disarmed: on
    Hold, between shows, or on the intermission bank, which has no track
    of its own in Jeff's model (bench B3.3). While ARMED, no packet for
    `timeout_s` (default 3.0, handoff section 4) is the fault section 4
    describes: logged once, at actor "madmapper", in a plain sentence, and
    again the moment a packet returns.

    A second, independent check: every packet's value, times `show_len_s`,
    is MadMapper's own reported position in seconds (bench B3.2). Compare
    it against ltcplay's own timecode position -- fed in by note_position(),
    since this module never reads the clock itself -- and flag more than
    `drift_ms` (default 100 ms) of daylight as a second health sentence,
    separate from "no heartbeat": the bench found MadMapper can run a
    frame or two off without ever going silent (bench B1's tracking
    numbers), and the two are different problems on the health panel."""

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
        self._show = None
        self._armed_at = None
        self._last_packet_at = None
        self._last_value = None
        self._alarmed = False
        self.packets_in = 0
        self._ltcplay_pos = None       # (seconds, at) from note_position()
        self.last_drift_ms = None
        self.drift_flagged = False

    def _default_socket(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((self.cfg.bind, self.cfg.port))
        s.settimeout(0.25)
        return s

    # -- running ------------------------------------------------------------
    def start(self):
        if self._listen_thread is not None:
            return
        self._stop.clear()
        self._sock = self._factory()
        self._listen_thread = threading.Thread(
            target=self._listen, daemon=True, name="ltcplay-madmapper-hb")
        self._listen_thread.start()
        self._poll_thread = threading.Thread(
            target=self._poll, daemon=True, name="ltcplay-madmapper-hb-poll")
        self._poll_thread.start()

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

        Ignored outright while disarmed (bench B9's 4 hour soak): between
        shows, MadMapper re-sends its Float track's last value, exactly
        one packet, the instant `/timelines/Bank-1/select` arrives, about
        2 s before the show actually starts. That packet must never flip
        `armed`'s own bookkeeping, be read as a position, or be compared
        for drift -- it says nothing about a running show because none is
        running yet. See test_madmapper_watchdog_ignores_a_lone_packet_
        while_disarmed."""
        now = self._clock() if at is None else at
        recovered = False
        drift_note = None
        with self._lock:
            if not self.armed:
                return
            self.packets_in += 1
            self._last_packet_at = now
            self._last_value = value
            if self._alarmed:
                self._alarmed = False
                recovered = True
            pos = self._ltcplay_pos
            show_len_s = self.cfg.show_len_s
        if recovered:
            self._note(f"MadMapper answered again, {self.packets_in} "
                      f"packet(s) in.", action="watchdog",
                      outcome="recovered", show=self._show)
        if show_len_s and pos is not None:
            ltc_s, ltc_at = pos
            mm_s = value * show_len_s
            ltc_now = ltc_s + (now - ltc_at)
            drift_ms = (mm_s - ltc_now) * 1000.0
            with self._lock:
                self.last_drift_ms = drift_ms
                was_flagged = self.drift_flagged
                flagged = abs(drift_ms) > self.cfg.drift_ms
                self.drift_flagged = flagged
            if flagged and not was_flagged:
                drift_note = (
                    f"MadMapper is {abs(drift_ms):.0f} ms "
                    f"{'ahead of' if drift_ms > 0 else 'behind'} ltcplay's "
                    f"own timecode.", "drift")
            elif was_flagged and not flagged:
                drift_note = ("MadMapper's position is back in step with "
                             "ltcplay's own timecode.", "drift clear")
        if drift_note:
            self._note(drift_note[0], action="watchdog",
                      outcome=drift_note[1], show=self._show)

    def note_position(self, seconds, at=None):
        """ltcplay's own timecode position, for the drift check. Nothing
        in this module calls this itself -- see the class docstring; it
        is fed in by whoever also owns the real clock, exactly like the
        `clock` argument to Link.hold()/resume()."""
        with self._lock:
            self._ltcplay_pos = (seconds, self._clock() if at is None
                                else at)

    def arm(self, show=None):
        with self._lock:
            self.armed = True
            self._show = show
            now = self._clock()
            self._armed_at = now
            self._last_packet_at = now       # a fresh grace window, not an
                                             # instant alarm
            self._alarmed = False

    def disarm(self):
        with self._lock:
            self.armed = False
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
                  action="watchdog", outcome="fault", show=show)

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
                    "packets_in": self.packets_in}


# --------------------------------------------------------------- build ----
def build(cfg, journal=None, clock=time.perf_counter, sleep=time.sleep,
         link_socket_factory=None, watchdog_socket_factory=None):
    """A (Link, Watchdog) pair wired to each other and to one journal
    callback, from a validated MadMapperConfig. `journal(text, **extra)` is
    called for every command and every watchdog change, and is the only
    place the actor "madmapper" is attached -- see web.py's serve(), which
    binds it to Service._journal_line("madmapper", ...)."""
    watchdog = Watchdog(cfg.heartbeat, clock=clock, sleep=sleep,
                        socket_factory=watchdog_socket_factory,
                        journal=journal)
    link = Link(cfg, socket_factory=link_socket_factory, clock=clock,
               sleep=sleep, journal=journal, watchdog=watchdog)
    return link, watchdog
