"""BEYOND: the laser blank/unblank half of Hold, Resume, Abort and Closing.

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

The facts that shape this module:

  No feedback, ever (like MadMapper: bench B2.4, and BEYOND's own OSC
  Monitor in the bench only ever showed messages ltcplay SENT, never a
  reply). So, exactly like madmapper.py's Link, nothing here can confirm a
  command landed, and the health dict this module exposes says only that a
  command was sent -- never "armed" or "ok", which would claim a liveness
  check this module cannot make (Jeff/Andy, 2026-09-26: "Don't show any
  liveness for BEYOND on the health panel beyond command sent").

  The blank (B8.3): `/beyond/master/livecontrol/brightness` ,f 0. BEYOND's
  preview went black at once, no ramp needed (bench: "Preview black at 0,
  back at 100" -- unlike MadMapper's master_audio_level and surface
  opacity, which both need this module's own ramp because MadMapper does
  not smooth them; BEYOND's brightness needs none). Unblank is the same
  address with ,f 100. The timeline and the timecode input both keep
  running throughout: this is a real blank, not a stop.

  Two addresses are PERMANENTLY FORBIDDEN and this module never sends
  either, under any path -- see FORBIDDEN_ADDRESSES, the _send() guard that
  refuses them outright, and test_beyond_never_sends_blackout_or_masterpause:

    /beyond/general/BlackOut restarts BEYOND's own application core (bench
    B8.3) and switches its TC-IN toolbar toggle off; the only way back is a
    manual "Show it now" press, and a second BlackOut does not undo it. A
    scheduler hook that sent this on every Hold would need a person at the
    keyboard to recover the very first time it fired.

    /beyond/general/MasterPause freezes the beams on whatever they were
    doing when it arrived (bench: "beams frozen") -- a static beam, the
    exact hazard a blank exists to prevent, not achieve.

  "Keep running even though timecode stops" MUST be OFF in BEYOND's own
  Settings > Configuration > Timecode In (bench B8.2): the default, ON,
  keeps the lasers moving straight through a frozen or lost timecode feed,
  which is unsafe for Hold on its own terms even before this module's
  blank ever reaches it. This is a manual, one-time BEYOND setting, not
  something OSC can read back or this module can enforce in code -- see
  the PR body's BEYOND setup section, and the same section's note that the
  TC-IN toolbar toggle has to be checked by eye before every show, since it
  switches itself off after a BlackOut or a Configuration OK and BEYOND
  exposes no OSC way to read it.

  Hold and Resume ordering (Jeff/Andy, 2026-09-26, building on bench B8.2):
  blank the lasers AT ONCE -- the same moment the flame cues go to zero,
  never after the music fade -- then fade the music, then freeze the
  clock. On Resume: restart the clock, THEN unblank, then fade the music
  back up. With "Keep running" off, BEYOND itself goes dark about 1 s
  after ltcplay's clock freezes and picks the timeline back up the moment
  timecode moves again (bench B8.2's own "OFF" case: it relocks and
  follows on resume, the same quick relock B1 measured for MadMapper), so
  unblanking right after the clock restarts -- rather than before, or
  waiting for BEYOND's own second of run-on to finish -- is the earliest
  moment a real blank is actually redundant, and it costs nothing to send
  it that early: BEYOND is still dark from its own 1 s timeout at that
  instant regardless, so brightness back to 100 cannot expose a static or
  stale beam by arriving "too soon". Unblanking BEFORE the clock restarts,
  by contrast, would show a laser sitting on whatever the last live frame
  was for however long the operator's Resume-to-clock-restart gap runs --
  exactly the static-beam risk a blank exists to avoid, on the one edge
  that this module's own hold()/resume() sequencing controls. See
  madmapper.Link.hold()/resume() for where this is wired in.
"""
import socket
import struct
import time

# Deliberately NOT `from . import madmapper`: this module has its own tiny
# copy of the OSC wire format and the self-healing socket, the same way
# clock.py's TimecodeOut and output.py's Sender each have their own socket
# rather than sharing one ("one should never be able to take the other
# down" -- clock.py's own module docstring). It also keeps this module's
# own inertness proof honest: a show file with a "beyond" block but no
# "madmapper" block must not load madmapper.py just to blank the lasers,
# and test_the_gpl_path_never_loads_madmapper enumerates every module
# except madmapper.py itself, which would otherwise import it right back
# in through here.

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8100                    # bench B8: 8000 clashes with MadMapper
BRIGHTNESS_ADDR = "/beyond/master/livecontrol/brightness"
BLANK_VALUE = 0.0
UNBLANK_VALUE = 100.0

# NEVER sent by this module, under any path. See the module docstring and
# _send()'s guard.
FORBIDDEN_ADDRESSES = frozenset(("/beyond/general/BlackOut",
                                 "/beyond/general/MasterPause"))


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
    """The "beyond" block of a show file, validated."""

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
    """One UDP socket to BEYOND. Never raises into the caller -- the same
    self-healing rule clock.py's TimecodeOut, output.py's Sender and
    madmapper.py's own socket all follow, scaled down for one destination
    and no acks (bench B8: BEYOND answers nothing at all, so a "failure"
    here only ever means the OS refused to hand the packet to the
    network)."""

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

    def send(self, address, value):
        now = self._clock()
        sock = self._ensure(now)
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


class Beyond:
    """BEYOND's OSC transport: blank and unblank ONLY (bench B8.3). No
    ramp -- brightness jumps at once in the bench, unlike MadMapper's
    audio and video, which both need madmapper.Link's own ramp because
    MadMapper does not smooth them itself.

    Sends are fire-and-forget UDP, on this module's own socket: BEYOND
    answers nothing, ever, so a "failure" here only ever means the OS
    refused to hand the packet to the network."""

    def __init__(self, cfg, socket_factory=None, clock=time.perf_counter,
                journal=None):
        self.cfg = cfg
        self.journal = journal
        self._osc = _Socket(cfg.host, cfg.port, socket_factory=socket_factory,
                            clock=clock, on_fail=self._on_send_fail)
        self.last_command = None       # "blank" or "unblank", for health()

    def _on_send_fail(self, msg):
        self._note(f"A BEYOND command failed: {msg}.", action="command",
                  outcome="failed")

    def _note(self, text, **extra):
        if self.journal:
            try:
                self.journal(text, **extra)
            except Exception:
                pass

    def _send(self, address, value=0.0):
        """The one place any OSC message reaches BEYOND. Refuses the two
        forbidden addresses outright, rather than merely never calling
        them from blank()/unblank(): see test_beyond_never_sends_
        blackout_or_masterpause and its matching mutations, which prove
        this guard is what actually stops them, not just that today's two
        public methods happen not to try."""
        if address in FORBIDDEN_ADDRESSES:
            raise BeyondConfigError(
                f"beyond.py refuses to send {address!r}: see the module "
                f"docstring for why (BlackOut restarts BEYOND's own core "
                f"and needs a manual recovery; MasterPause freezes the "
                f"beams, a static-beam hazard).")
        return self._osc.send(address, value)

    def blank(self, show=None):
        """A real blank command: brightness to 0. The timeline and the
        timecode input both keep running (bench B8.3) -- this never stops
        or pauses anything on BEYOND's side, only dims its output dark."""
        self._send(BRIGHTNESS_ADDR, BLANK_VALUE)
        self.last_command = "blank"
        self._note(f"BEYOND blanked{_for_show(show)}.", action="blank",
                  show=show)

    def unblank(self, show=None):
        self._send(BRIGHTNESS_ADDR, UNBLANK_VALUE)
        self.last_command = "unblank"
        self._note(f"BEYOND unblanked{_for_show(show)}.", action="unblank",
                  show=show)

    def health(self):
        """No liveness claim -- BEYOND sends no feedback at all (bench
        B8), so this says only that a command was sent, never "armed" or
        "ok" (Jeff/Andy, 2026-09-26)."""
        return {"last_command": self.last_command,
                "packets_sent": self._osc.packets_sent,
                "last_sent_at": self._osc.last_ok_at}

    def close(self):
        self._osc.close()


def _for_show(show):
    return f" for show {show}" if show else ""


def build(cfg, journal=None, clock=time.perf_counter, socket_factory=None):
    return Beyond(cfg, socket_factory=socket_factory, clock=clock,
                 journal=journal)
