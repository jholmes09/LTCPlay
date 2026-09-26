"""BEYOND: the laser blank/unblank device layer.

Imported ONLY when a show file has a "beyond" block. The GPL show at
Dollywood never has one, so on the Mac none of this is loaded -- the same
inertness madmapper.py, clock.py and schedule_service.py each already rely
on: see test_the_gpl_path_never_loads_beyond.

Everything below follows the Pico bench report, 2026-09-25
(bench/bench_report_2026-09-25.md on branch bench/2026-09-25), section B8,
which override the handoff wherever the two differ. BEYOND is Pangolin's
laser show program; Andy Win's laser rig chases the same Art-Net timecode
ltcplay sends MadMapper (clock.py's ArtNetConfig already names more than one
node -- nothing anywhere in this codebase assumes a single timecode
destination; see the PR body's setup section for the two-node address split
B8.0 found: MadMapper on 127.0.0.1 or its Interface address, BEYOND on
127.0.0.2, because MadMapper binds specific addresses and BEYOND binds
0.0.0.0).

This module is DEVICE LAYER ONLY, like madmapper.py: blank() and unblank()
are the only two operations, there is no sequencing logic here, and this
module has no idea when a show starts, holds or aborts. An earlier version
of this PR wired blank/unblank into a scheduler-state-derived sequence
(hold()/resume()/abort()/closing(), reached through Link.on_transition);
that entire mechanism was removed after an opus review found real races
from deriving actions from state pairs on the scheduler's own unordered
hook threads (see madmapper.py's module docstring). The follow-up
"conductor" module is what will decide WHEN to call blank()/unblank(), on
one serialized executor, with the real ordering guarantees Hold, Resume,
Abort and Closing each need.

The facts that shape this module:

  No feedback, ever (like MadMapper: bench B2.4, and BEYOND's own OSC
  Monitor in the bench only ever showed messages ltcplay SENT, never a
  reply). So nothing here can confirm a command landed on its own -- see
  blank()/unblank()'s own retry-and-report design below, and health(),
  which says only that a command was sent and what happened trying,
  never a liveness claim (Jeff/Andy, 2026-09-26: "Don't show any liveness
  for BEYOND on the health panel beyond command sent").

  The blank (B8.3): `/beyond/master/livecontrol/brightness` ,f 0. BEYOND's
  preview went black at once, no ramp needed (bench: "Preview black at 0,
  back at 100"). Unblank is the same address with ,f 100. The timeline and
  the timecode input both keep running throughout: this is a real blank,
  not a stop.

  blank()/unblank() send the brightness packet 3 times, about 20 ms apart,
  and check whether at least one actually got out (an audit of the first
  version of this PR, round 2, found a failed send was still journaled as
  "blanked" -- a false "the lasers are down" report is worse than no
  report, since it is trusted). A failed socket open is retried within the
  same call rather than silently dropped by the socket's own reopen
  backoff, which exists for ordinary traffic, not a deliberate short retry
  burst a few tens of milliseconds long.

  ONLY the brightness address, and ONLY the values 0.0 and 100.0, are ever
  allowed off this module at all (S5, an audit finding: a deny list alone
  let `/beyond/general/blackout` (wrong case), a trailing slash, a
  wildcard, an OSC bundle-looking string and a dozen other near-misses
  straight through, and nothing below _send() enforced anything at all).
  See _allowed(), enforced at BOTH _send() and the socket's own send() --
  two layers, so a future change that bypasses one still meets the other.
  The original exact-address deny list for BlackOut/MasterPause is kept as
  a third, explicit layer on top of that, named for what it is (the two
  addresses that must never be sent, not just "not brightness"):

    /beyond/general/BlackOut restarts BEYOND's own application core (bench
    B8.3) and switches its TC-IN toolbar toggle off; the only way back is a
    manual "Show it now" press, and a second BlackOut does not undo it.

    /beyond/general/MasterPause freezes the beams on whatever they were
    doing when it arrived (bench: "beams frozen") -- a static beam, the
    exact hazard a blank exists to prevent, not achieve.

  "Keep running even though timecode stops" MUST be OFF in BEYOND's own
  Settings > Configuration > Timecode In (bench B8.2): the default, ON,
  keeps the lasers moving straight through a frozen or lost timecode feed.
  This is a manual, one-time BEYOND setting, not something OSC can read
  back or this module can enforce in code -- see the PR body's BEYOND
  setup section, and the same section's note that the TC-IN toolbar
  toggle has to be checked by eye before every show, since it switches
  itself off after a BlackOut or a Configuration OK and BEYOND exposes no
  OSC way to read it.

  Safe defaults (an audit finding, S6): build() blanks once immediately,
  before returning, so a fresh link never starts in an unknown state; and
  close() blanks before it closes the socket, so tearing a link down never
  leaves the lasers live by omission.
"""
import math
import socket
import struct
import time

# Deliberately NOT `from . import madmapper`: this module has its own tiny
# copy of the OSC wire format and the self-healing socket, the same way
# clock.py's TimecodeOut and output.py's Sender each have their own socket
# rather than sharing one. It also keeps this module's own inertness proof
# honest: a show file with a "beyond" block but no "madmapper" block must
# not load madmapper.py just to blank the lasers, and
# test_the_gpl_path_never_loads_madmapper enumerates every module except
# madmapper.py itself, which would otherwise import it right back in
# through here.

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8100                    # bench B8: 8000 clashes with MadMapper
BRIGHTNESS_ADDR = "/beyond/master/livecontrol/brightness"
BLANK_VALUE = 0.0
UNBLANK_VALUE = 100.0
ALLOWED_VALUES = (BLANK_VALUE, UNBLANK_VALUE)

# OSC pattern-matching special characters (the OSC 1.0 spec's own address
# pattern syntax) plus the bundle marker: none of these can ever appear in
# a single, exact, allowed address, but _allowed() checks for them
# explicitly anyway -- see its own docstring for why.
_SPECIAL_CHARS = frozenset("*?[]{}#")

# NEVER sent by this module, under any path. Named for what they are, on
# top of (not instead of) the allow-list in _allowed(): see the module
# docstring's "ONLY the brightness address" paragraph.
FORBIDDEN_ADDRESSES = frozenset(("/beyond/general/BlackOut",
                                 "/beyond/general/MasterPause"))

RETRY_COUNT = 3
RETRY_INTERVAL_S = 0.02          # about 20 ms apart, per the audit


class BeyondConfigError(ValueError):
    """A beyond block that cannot run, with the sentence that says why."""


def _obj(v, where, what):
    if not isinstance(v, dict):
        raise BeyondConfigError(f"{where}: {what} must be an object like "
                                f"{{...}}")
    return v


def _no_typos(doc, keys, where, what):
    unknown = sorted(k for k in doc if k not in keys)
    if unknown:
        raise BeyondConfigError(
            f"{where}: {what} has no setting "
            f"{', '.join(repr(k) for k in unknown)}; it takes: "
            f"{', '.join(sorted(keys))}.")


def _str(doc, key, where, what, default=None, required=False):
    v = doc.get(key, default)
    if v is None and not required:
        return None
    if not isinstance(v, str) or not v.strip():
        raise BeyondConfigError(f"{where}: {what}.{key} has to be a name, "
                                f"not {v!r}.")
    return v.strip()


def _port(doc, key, where, what, default):
    v = doc.get(key, default)
    if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 65535:
        raise BeyondConfigError(f"{where}: {what}.{key} is a port number, "
                                f"1 to 65535, not {v!r}.")
    return v


class BeyondConfig:
    """The "beyond" block of a show file, validated. There is no
    "address" setting on purpose: the brightness address is the only one
    this module ever sends, and it is not configurable -- see _allowed()."""

    KEYS = frozenset(("host", "port", "notes"))

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT):
        self.host = host
        self.port = port

    @classmethod
    def parse(cls, doc, where="timeline"):
        what = "'beyond'"
        _obj(doc, where, what)
        _no_typos(doc, cls.KEYS, where, what)
        host = _str(doc, "host", where, what, DEFAULT_HOST, required=True)
        port = _port(doc, "port", where, what, DEFAULT_PORT)
        if port == 8000:
            raise BeyondConfigError(
                f"{where}: {what}.port is 8000, MadMapper's own OSC input "
                f"port on the same PC (bench B8). BEYOND needs its own "
                f"port; the bench used 8100. In BEYOND itself: Settings > "
                f"OSC > OSC Settings, Enable receiving OSC messages on, "
                f"Incoming port 8100 (it defaults to off, and to 8000 once "
                f"turned on).")
        return cls(host, port)

    def summary(self):
        return f"{self.host}:{self.port}"


def _allowed(address, value):
    """S5's allow-list: the ONLY thing this module may ever send is the
    brightness address, with the value exactly 0.0 or 100.0. Checked at
    BOTH _send() and _Socket.send() (see each) -- an audit (R6) found a
    deny list alone let a wrong-case address, a trailing slash, a doubled
    slash, an OSC wildcard/range/alternation pattern and a direct call
    below _send() all straight through; none of those can pass an
    EXACT-match allow-list, which is what this is, checked as such (not
    merely inferred from the special-character reject, which is kept as
    an explicit, separately testable condition even though the exact-match
    check below already excludes every string that contains one)."""
    if not isinstance(address, str):
        return False
    if address.startswith("#"):
        return False
    if any(c in _SPECIAL_CHARS for c in address):
        return False
    if address != BRIGHTNESS_ADDR:
        return False
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    if math.isnan(value):
        return False
    return any(value == v for v in ALLOWED_VALUES)


def _pad(b):
    """Null-terminate then pad to a 4-byte boundary: at least one null,
    always. The identical rule madmapper.py's own _pad follows -- both are
    the OSC 1.0 spec, not a shared implementation."""
    b = b + b"\x00"
    while len(b) % 4:
        b += b"\x00"
    return b


def _encode_float(address, value):
    """One OSC message carrying exactly one float argument -- all this
    module ever needs to send (brightness, 0.0 or 100.0). The same wire
    format madmapper.py's own encode() produces (see that module's own
    test against the bench's captured bytes); this module's copy is
    proved against the bench's own B8.3 numbers by
    test_beyond_osc_bytes_and_config_refusals."""
    if not isinstance(address, str) or not address.startswith("/"):
        raise ValueError(f"an OSC address has to start with /, not "
                         f"{address!r}")
    return (_pad(address.encode("utf-8")) + _pad(b",f")
           + struct.pack(">f", float(value)))


class _Socket:
    """One UDP socket to BEYOND. Never raises OSError into the caller --
    the same self-healing rule clock.py's TimecodeOut, output.py's Sender
    and madmapper.py's own socket all follow, scaled down for one
    destination and no acks (bench B8: BEYOND answers nothing at all, so a
    "failure" here only ever means the OS refused to hand the packet to
    the network). DOES raise BeyondConfigError for anything that fails
    _allowed() or is one of the two forbidden addresses -- see the module
    docstring's "ONLY the brightness address" paragraph: this is the
    lowest level anything reaches the network from, so this is where the
    guard has to hold even if every layer above it did not."""

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

    def _ensure(self, now, force=False):
        if self._sock is not None:
            return self._sock
        if not force and self._last_open is not None and \
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

    def send(self, address, value, force=False):
        if address in FORBIDDEN_ADDRESSES or not _allowed(address, value):
            raise BeyondConfigError(
                f"beyond.py's socket layer refuses to send {address!r} "
                f"with value {value!r}: only the brightness address, "
                f"with 0.0 or 100.0, is ever allowed off this module (S5).")
        now = self._clock()
        sock = self._ensure(now, force=force)
        if sock is None:
            return False
        pkt = _encode_float(address, value)
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


def _for_show(show):
    return f" for show {show}" if show else ""


class Beyond:
    """BEYOND's OSC transport: blank and unblank ONLY (bench B8.3). No
    ramp -- brightness jumps at once in the bench. Pure device layer: no
    sequencing, no idea when a show starts, holds or aborts (see the
    module docstring).

    Each call sends the brightness packet 3 times, about 20 ms apart
    (RETRY_COUNT/RETRY_INTERVAL_S), forcing the socket open on every
    attempt rather than trusting a single try. Returns True the instant at
    least one of the 3 got out, False if all 3 failed -- and only ever
    journals the calm "blanked"/"unblanked" sentence when it actually
    succeeded; a failure gets its own sentence, flagged as a fault, never
    silently reported as done."""

    def __init__(self, cfg, socket_factory=None, clock=time.perf_counter,
                sleep=time.sleep, journal=None):
        self.cfg = cfg
        self.journal = journal
        self._sleep = sleep
        self._osc = _Socket(cfg.host, cfg.port, socket_factory=socket_factory,
                            clock=clock, on_fail=self._on_send_fail)
        self.last_command = None       # "blank" or "unblank"
        self.last_result = None        # "ok" or "failed"

    def _on_send_fail(self, msg):
        self._note(f"A BEYOND command failed: {msg}.", action="command",
                  outcome="failed", fault=True)

    def _note(self, text, **extra):
        if self.journal:
            try:
                self.journal(text, **extra)
            except Exception:
                pass

    def _send(self, address, value=0.0, force=False):
        """The one place any OSC message reaches BEYOND above the socket
        layer. Refuses the two forbidden addresses AND anything that
        fails the allow-list outright (both checked again, independently,
        at _Socket.send() itself -- see its own docstring)."""
        if address in FORBIDDEN_ADDRESSES or not _allowed(address, value):
            raise BeyondConfigError(
                f"beyond.py refuses to send {address!r} with value "
                f"{value!r}: only the brightness address, with 0.0 or "
                f"100.0, is ever allowed (S5), and BlackOut/MasterPause "
                f"are refused by name as well (BlackOut restarts BEYOND's "
                f"own core and needs a manual recovery; MasterPause "
                f"freezes the beams, a static-beam hazard).")
        return self._osc.send(address, value, force=force)

    def _send_retried(self, value):
        ok = False
        for i in range(RETRY_COUNT):
            if self._send(BRIGHTNESS_ADDR, value, force=True):
                ok = True
            if i < RETRY_COUNT - 1:
                self._sleep(RETRY_INTERVAL_S)
        return ok

    def blank(self, show=None):
        """A real blank command: brightness to 0. The timeline and the
        timecode input both keep running (bench B8.3) -- this never stops
        or pauses anything on BEYOND's side, only dims its output dark.
        Returns True if at least one of the 3 packets got out."""
        ok = self._send_retried(BLANK_VALUE)
        self.last_command = "blank"
        self.last_result = "ok" if ok else "failed"
        if ok:
            self._note(f"BEYOND blanked{_for_show(show)}.", action="blank",
                      show=show)
        else:
            self._note(
                f"BEYOND failed to blank{_for_show(show)}: no packet got "
                f"out after {RETRY_COUNT} tries. The lasers may still be "
                f"showing whatever they were.", action="blank",
                outcome="failed", show=show, fault=True)
        return ok

    def unblank(self, show=None):
        """Returns True if at least one of the 3 packets got out."""
        ok = self._send_retried(UNBLANK_VALUE)
        self.last_command = "unblank"
        self.last_result = "ok" if ok else "failed"
        if ok:
            self._note(f"BEYOND unblanked{_for_show(show)}.",
                      action="unblank", show=show)
        else:
            self._note(
                f"BEYOND failed to unblank{_for_show(show)}: no packet "
                f"got out after {RETRY_COUNT} tries. The lasers stay "
                f"dark.", action="unblank", outcome="failed", show=show,
                fault=True)
        return ok

    def health(self):
        """No liveness claim -- BEYOND sends no feedback at all (bench
        B8), so this says only that a command was sent, what happened
        trying, and how many packets actually went out."""
        return {"last_command": self.last_command,
                "last_result": self.last_result,
                "packets_sent": self._osc.packets_sent,
                "last_sent_at": self._osc.last_ok_at}

    def close(self):
        """A safe default (S6): blank before closing, so tearing this
        link down never leaves the lasers live by omission."""
        try:
            self.blank()
        except Exception:
            pass
        self._osc.close()


def build(cfg, journal=None, clock=time.perf_counter, sleep=time.sleep,
         socket_factory=None):
    """A Beyond, blanked once immediately (S6's other safe default: a
    fresh link never starts in an unknown state)."""
    link = Beyond(cfg, socket_factory=socket_factory, clock=clock,
                 sleep=sleep, journal=journal)
    link.blank()
    return link
