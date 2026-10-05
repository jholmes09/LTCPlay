"""BEYOND's laser blanking by timecode (Jeff, 2026-10-04): Fire & Ice only.

BEYOND Essentials has no OSC input, so the lasers are kept dark by the
timecode BEYOND follows. BEYOND gets its own Art-Net timecode stream
(unicast, apart from MadMapper's). Whenever the lasers must be dark, that
stream jumps to a reserved BLACK ZONE: hour 23 by default, 23:00:00:00 and
running, where Andy keeps no laser cues. When the lasers may light again
it returns to the true show timecode on the next frame the show sends
(after a Hold, that is the exact held frame). MadMapper's stream is never
touched.

The zone's timecode keeps RUNNING, 30 frames a second, never frozen, so
BEYOND never sits on a frozen cue; BEYOND's "keep running when timecode
stops" must be OFF (the checklist says so).

How it is wired: `TimecodeGate` registers itself in clock.DIVERT for the
destination labelled "BEYOND" in the show file's clock.artnet.nodes, so the
show's own timecode sender hands it every packet meant for BEYOND instead
of sending it. While the lasers may light the gate sends that packet on at
once; while they must be dark it drops it and its own thread sends the
black zone. `Blanking` gives the conductor's device layer the blank() and
unblank() it already calls (conductor.ConductorDevices), so every place the
conductor blanks the lasers (Hold, Abort and latched, a failed start, the
intermission, closing, a show's end, any gate that says no) blanks them
here too, by timecode, by OSC (beyond.py's brightness 0/100), or both,
per "beyond_blank" in ltcplay_fire_ice.json.

Fail safe: a black packet that cannot be sent is a fault, journaled, and
blank() reports it failed, so the conductor records the lasers as UNKNOWN
and blanks again; dark is never assumed.
"""
import socket
import threading
import time

MODES = ("timecode", "osc", "both")
DEFAULT_MODE = "timecode"
DEFAULT_BLACK_HOUR = 23
FPS = 30                         # clock.MASTER_FPS
ARTNET_PORT = 6454
LABEL = "beyond"                 # clock.artnet.nodes name, any case


def _clock():
    # Inside a function, never at module scope: importing the program must
    # never load the clock (the GPL wall, selftest).
    from . import clock
    return clock


def black_tc(hour, n):
    """The black zone's timecode `n` frames after it began: hour `hour`,
    running; it wraps inside that hour, never leaving it."""
    n = n % (3600 * FPS)
    s, f = divmod(n, FPS)
    m, s = divmod(s, 60)
    return hour, m, s, f


def tc_of(pkt):
    """(h, m, s, f) of an ArtTimeCode packet, or None."""
    if len(pkt) < 19 or pkt[:8] != b"Art-Net\0":
        return None
    return pkt[17], pkt[16], pkt[15], pkt[14]


class TimecodeGate:
    """BEYOND's own timecode stream: the show's, or the black zone."""

    def __init__(self, ip, port=ARTNET_PORT,
                 hour=DEFAULT_BLACK_HOUR, journal=None, socket_factory=None,
                 clock=time.monotonic, stream_id=0):
        self.ip = ip
        self.port = port
        self.hour = hour
        self.stream_id = stream_id
        self._journal = journal
        self._factory = socket_factory or (
            lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        self._clock = clock
        self._sock = None
        self._lock = threading.Lock()
        self.lit = False             # dark from the start
        self._zone_start = clock()
        self._stop = threading.Event()
        self._thread = None
        self.black_frames = 0
        self.show_frames = 0
        self.send_errors = 0
        self.last_error = ""
        self.last_sent = None
        self._failing = False
        # Bumped by every dark(), under the lock: a light() that read an
        # older value refuses (review of PR #43, P0-3).
        self._blank_epoch = 0

    # -- sending ------------------------------------------------------------
    def _send(self, pkt, dest=None):
        try:
            if self._sock is None:
                self._sock = self._factory()
            self._sock.sendto(pkt, dest or (self.ip, self.port))
        except OSError as e:
            self.send_errors += 1
            self.last_error = str(e)
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
            if not self._failing:
                self._failing = True
                self._note(f"BEYOND's timecode stream to {self.ip} could not "
                           f"be sent ({e}): the lasers' state is UNKNOWN, "
                           f"not dark.", fault=True, outcome="send_failed")
            return False
        if self._failing:
            self._failing = False
            self._note(f"BEYOND's timecode stream to {self.ip} is being sent "
                       f"again.", outcome="send_ok")
        self.last_sent = tc_of(pkt)
        return True

    def _black_now(self):
        n = int((self._clock() - self._zone_start) * FPS)
        h, m, s, f = black_tc(self.hour, n)
        c = _clock()
        return c.arttimecode(h, m, s, f, c.MASTER_TYPE, self.stream_id)

    def send_black(self):
        with self._lock:
            if self.ip is not None:
                ok = self._send(self._black_now())
                if ok:
                    self.black_frames += 1
                return ok
            # No address yet: not one black frame can be sent, so the
            # lasers are NOT known to be dark (review of PR #43, P0-2: this
            # used to answer True, and the conductor recorded the lasers
            # black). A fault, every time, never assumed dark.
            self.send_errors += 1
            self.last_error = "no address for BEYOND's timecode"
        self._note("BEYOND's timecode stream has no address yet, so no black "
                   "frame could be sent: the lasers' state is UNKNOWN, not "
                   "dark. Name BEYOND under 'clock.artnet.nodes' in the show "
                   "file, or set 'beyond_timecode_ip' in "
                   "ltcplay_fire_ice.json.", fault=True, outcome="no_address")
        return False

    def divert(self, pkt, ip, port):
        """clock.DIVERT's hook: the show's packet for BEYOND. Sent on while
        the lasers may light; dropped while they must be dark (the black
        zone is sent instead)."""
        with self._lock:
            if self.ip is None:
                self.ip = ip
            if not self.lit:
                return True
            ok = self._send(pkt, (self.ip, port))
            if ok:
                self.show_frames += 1
            return ok

    # -- dark and lit -------------------------------------------------------
    def dark(self):
        """Into the black zone, sending its first frame at once. False when
        that frame could not be sent."""
        with self._lock:
            self._blank_epoch += 1
            if self.lit:
                self.lit = False
                self._zone_start = self._clock()
        return self.send_black()

    def epoch(self):
        """How many times dark() has run: read before a restore decides,
        and handed to light()."""
        with self._lock:
            return self._blank_epoch

    def light(self, epoch=None):
        """Back to the show's timecode, unless dark() has run since `epoch`
        was read (review of PR #43, P0-3): the check and the change are one
        step under the gate's lock, so an Abort's blank landing between a
        restore's last check and this call always wins. False when
        refused."""
        with self._lock:
            if epoch is not None and epoch != self._blank_epoch:
                return False
            self.lit = True
        return True

    # -- the black zone's own frames ---------------------------------------
    def run(self):
        period = 1.0 / FPS
        nxt = time.perf_counter()
        while not self._stop.is_set():
            if not self.lit and self.ip is not None:
                self.send_black()
            nxt += period
            wait = nxt - time.perf_counter()
            if wait < -period:
                nxt = time.perf_counter()
                wait = 0
            if wait > 0:
                time.sleep(wait)

    def adopt(self, ip):
        """The address the show file names for BEYOND's timecode (the one
        destination this gate diverts, checked at session open), taken when
        the gate has none yet, so the black zone reaches BEYOND from the
        first blank and not only after the show's first packet."""
        with self._lock:
            if self.ip is not None or not ip:
                return self.ip
            self.ip = ip
        if self._thread is not None:
            _clock().DIVERT[ip] = self.divert
        return ip

    def start(self):
        _clock().DIVERT[LABEL] = self.divert
        if self.ip:
            # A node at BEYOND's address under any other name is BEYOND too.
            _clock().DIVERT[self.ip] = self.divert
        self._thread = threading.Thread(target=self.run, daemon=True,
                                        name="ltcplay-beyond-timecode")
        self._thread.start()
        return self

    def close(self):
        for k in [k for k, v in _clock().DIVERT.items()
                  if v == self.divert]:
            del _clock().DIVERT[k]
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2)
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def stats(self):
        return {"mode_lit": self.lit, "black_frames": self.black_frames,
                "show_frames": self.show_frames,
                "send_errors": self.send_errors, "ip": self.ip,
                "hour": self.hour, "last_sent": self.last_sent}

    def _note(self, text, **kw):
        if self._journal is not None:
            try:
                self._journal(text, action="lasers", **kw)
            except Exception:
                pass


class Blanking:
    """What conductor.ConductorDevices calls as its BEYOND: blank() and
    unblank(), by timecode, by OSC, or both (`mode`). `osc` is beyond.py's
    Beyond or None; `gate` a TimecodeGate or None."""

    def __init__(self, mode, gate=None, osc=None, journal=None):
        self.mode = mode
        self.gate = gate
        self.osc = osc
        self._journal = journal
        self.last_result = None
        self.cfg = getattr(osc, "cfg", None)

    def _uses(self, kind):
        return self.mode in (kind, "both")

    def blank(self, show=None):
        ok = True
        parts = []
        if self._uses("timecode"):
            sent = self.gate is not None and self.gate.dark()
            ok = ok and sent
            parts.append(f"BEYOND's timecode to the black zone (hour "
                         f"{self.gate.hour if self.gate else '?'})"
                         if sent else "the black zone could NOT be sent")
        if self._uses("osc"):
            sent = self.osc is not None and self.osc.blank(show=show) is True
            ok = ok and sent
            parts.append("OSC brightness 0" if sent else
                         "the OSC blank could NOT be sent")
        self._note(f"Lasers blanked ({self.mode}): {', '.join(parts)}.",
                   fault=not ok, outcome="blanked" if ok else "blank_failed")
        return ok

    def unblank(self, show=None, in_show=False, still_wanted=None):
        if in_show is not True:
            return False
        # Read before anything is decided: any blank from here on refuses
        # the timecode half below, under the gate's own lock.
        epoch = self.gate.epoch() if self.gate is not None else None
        ok = True
        parts = []
        if self._uses("osc"):
            kw = {"in_show": True}
            if still_wanted is not None:
                kw["still_wanted"] = still_wanted
            sent = self.osc is not None and \
                self.osc.unblank(show=show, **kw) is True
            self.last_result = getattr(self.osc, "last_result", None)
            ok = ok and sent
            parts.append("OSC brightness 100" if sent else
                         "the OSC restore did not go out")
        if self._uses("timecode") and ok:
            if still_wanted is not None and not still_wanted():
                self.last_result = "cut"
                return False
            ok = self.gate is not None and self.gate.light(epoch)
            if self.gate is not None and not ok:
                # A blank landed after the restore's last check: it wins.
                self.last_result = "cut"
                return False
            parts.append("BEYOND's timecode back to the show's")
        self._note(f"Lasers restored ({self.mode}): {', '.join(parts)}.",
                   fault=not ok, outcome="restored" if ok else
                   "restore_failed")
        return ok

    def health(self):
        out = {"blank_mode": self.mode}
        if self.gate is not None:
            out["timecode"] = self.gate.stats()
        if self.osc is not None:
            try:
                out["osc"] = self.osc.health()
            except Exception:
                pass
        return out

    def _note(self, text, **kw):
        if self._journal is not None:
            try:
                self._journal(text, action="lasers", **kw)
            except Exception:
                pass
