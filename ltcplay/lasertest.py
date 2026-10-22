"""Laser calibration tests (Jeff, 2026-10-17, the Morning and Shutdown page).

One toggle per laser: ON keeps the alignment grid up at low power until it
is switched off. HOW BEYOND is told is a PLACEHOLDER until Jeff knows
(direct OSC, or a timeline cue): each laser in ltcplay_morning.json carries
a `mechanism` ("none" until decided, or "osc"), and lists of OSC messages
for `on`, `off` and `lock`. With "none" the button refuses in words and
nothing is sent. This module never decides WHEN a test may run (the engine
does: not while a show is live or an Abort stands); it only sends what the
config says, and refuses a few things outright:

  - an ON for a laser that is locked out (lockout is enforced here, the
    one per-laser output path ltcplay has, as well as at the engine);
  - any address that is not a single exact OSC address starting with "/",
    or that carries an OSC pattern character (* ? [ ] { } #);
  - BEYOND's /beyond/general/BlackOut and /beyond/general/MasterPause, by
    name, for the reasons beyond.py gives (BlackOut restarts BEYOND's core
    and needs a manual recovery; MasterPause freezes the beams).

Like beyond.py this module has its own small OSC encoder and socket, and
sends every message three times about 20 ms apart. BEYOND answers nothing,
so "sent" is all it can ever report. It never imports beyond.py: a laser
test with no BEYOND block configured must still load.

The laser arm (Jeff's rulings 2 and 7, 2026-10-22; laserarm.py): a test
ON is a cue for that one laser, so it needs that laser ARMED (`armed`, a
callable(laser_id) -> bool from the engine; anything but a real True is
"not armed"). A disarm turns the laser's test off. Each laser may also
carry `arm` and `disarm` message lists: PLACEHOLDERS for a per-laser
(per-projector or per-zone) BEYOND command, which no bench or document in
this repo has verified. They are empty by default, and an empty list, or
mechanism "none", sends nothing. When Andy fills them in, `arm` goes out
only for an armed laser that is not locked out, and `disarm` on every
disarm of that laser. They never loosen the global rule laserarm.py
keeps: BEYOND's own EnableLaserOutput only with every laser armed.
"""
import math
import socket
import struct
import threading
import time

# The same two addresses beyond.py refuses by name (the selftest holds them
# equal), written here so this module stands alone.
FORBIDDEN_ADDRESSES = frozenset(("/beyond/general/BlackOut",
                                 "/beyond/general/MasterPause"))
# BEYOND's GLOBAL controls, which only beyond.py and the laser arm may
# send, behind their own guards (review of PR #87, P3-6): a laser's own
# config lists (on, off, lock, arm, disarm) never reach them, or a test or
# a placeholder could light every laser around the arm. The selftest
# holds them equal to beyond.py's ALLOWED_ADDRESSES.
GLOBAL_ADDRESSES = frozenset(("/beyond/master/livecontrol/brightness",
                              "/beyond/general/EnableLaserOutput",
                              "/beyond/general/DisableLaserOutput"))
_SPECIAL_CHARS = frozenset("*?[]{}#")
MECHANISMS = ("none", "osc")
RETRY_COUNT = 3
RETRY_INTERVAL_S = 0.02
ADDRESS_MAX = 200
STRING_MAX = 200


class LaserConfigError(ValueError):
    """A laser block that cannot run, in a sentence."""


def allowed_address(address):
    """True for one exact OSC address this module may send to."""
    if not isinstance(address, str) or not address.startswith("/"):
        return False
    if len(address) > ADDRESS_MAX or len(address) < 2:
        return False
    if any(c in _SPECIAL_CHARS for c in address) or " " in address:
        return False
    if address in FORBIDDEN_ADDRESSES or address in GLOBAL_ADDRESSES:
        return False
    return all(0x21 <= ord(c) <= 0x7e for c in address)


def _pad(b):
    b = b + b"\x00"
    while len(b) % 4:
        b += b"\x00"
    return b


def encode_osc(address, value):
    """One OSC message with one argument: a whole number (,i), a decimal
    (,f) or a short string (,s). Raises ValueError for anything else."""
    if not allowed_address(address):
        raise ValueError(f"{address!r} is not an address this program may "
                         f"send to")
    if isinstance(value, bool):
        raise ValueError("a true/false value is not an OSC argument here")
    if isinstance(value, int):
        if not -2 ** 31 <= value < 2 ** 31:
            raise ValueError("the whole number is out of range")
        return _pad(address.encode("ascii")) + _pad(b",i") + \
            struct.pack(">i", value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("the decimal is not finite")
        return _pad(address.encode("ascii")) + _pad(b",f") + \
            struct.pack(">f", value)
    if isinstance(value, str):
        if len(value) > STRING_MAX or not value.isprintable():
            raise ValueError("the text argument is too long or not plain")
        return _pad(address.encode("ascii")) + _pad(b",s") + \
            _pad(value.encode("utf-8"))
    raise ValueError(f"{value!r} is not a whole number, a decimal or text")


def check_messages(msgs, where):
    """A list of [address, value] pairs, validated by encoding each one.
    Returns a list of (address, value) tuples. Raises LaserConfigError."""
    if msgs is None:
        return []
    if not isinstance(msgs, list):
        raise LaserConfigError(f"{where} has to be a list of [address, "
                               f"value] pairs.")
    out = []
    for i, m in enumerate(msgs):
        if not isinstance(m, (list, tuple)) or len(m) != 2:
            raise LaserConfigError(f"{where} item {i + 1} is not an "
                                   f"[address, value] pair.")
        addr, val = m
        try:
            encode_osc(addr, val)
        except ValueError as e:
            raise LaserConfigError(f"{where} item {i + 1}: {e}.") from None
        out.append((addr, val))
    return out


class Laser:
    __slots__ = ("id", "name", "mechanism", "host", "port", "on", "off",
                 "lock", "arm", "disarm", "note")

    def __init__(self, id, name, mechanism="none", host=None, port=None,
                 on=(), off=(), lock=(), note="", arm=(), disarm=()):
        self.id = id
        self.name = name
        self.mechanism = mechanism
        self.host = host
        self.port = port
        self.on = list(on)
        self.off = list(off)
        self.lock = list(lock)
        # PLACEHOLDERS (ruling 7): a per-laser BEYOND arm and disarm, not
        # verified anywhere. Empty: nothing is sent.
        self.arm = list(arm)
        self.disarm = list(disarm)
        self.note = note

    def view(self):
        return {"id": self.id, "name": self.name,
                "mechanism": self.mechanism,
                "configured": self.mechanism != "none" and bool(self.on),
                "per_laser_arm": self.mechanism != "none" and
                bool(self.arm or self.disarm),
                "target": (f"{self.host}:{self.port}" if self.host and
                           self.port else None),
                "note": self.note}


LASER_KEYS = frozenset(("id", "name", "mechanism", "host", "port", "on",
                        "off", "lock", "arm", "disarm", "note"))


def parse_lasers(doc, where, default_host=None, default_port=None):
    """The "lasers" list of ltcplay_morning.json. Raises LaserConfigError."""
    if not isinstance(doc, list):
        raise LaserConfigError(f"{where}: lasers has to be a list.")
    out, ids = [], set()
    for i, d in enumerate(doc):
        w = f"{where}: lasers[{i + 1}]"
        if not isinstance(d, dict):
            raise LaserConfigError(f"{w} is not an object.")
        unknown = sorted(k for k in d if k not in LASER_KEYS)
        if unknown:
            raise LaserConfigError(f"{w} has no setting "
                                   f"{', '.join(repr(k) for k in unknown)}; "
                                   f"it takes {', '.join(sorted(LASER_KEYS))}.")
        lid = d.get("id")
        if not isinstance(lid, str) or not lid.strip() or lid in ids:
            raise LaserConfigError(f"{w} needs an id of its own.")
        ids.add(lid)
        name = d.get("name", lid)
        if not isinstance(name, str) or not name.strip():
            raise LaserConfigError(f"{w}: name has to be words.")
        mech = d.get("mechanism", "none")
        if mech not in MECHANISMS:
            raise LaserConfigError(f"{w}: mechanism has to be one of "
                                   f"{', '.join(repr(m) for m in MECHANISMS)}"
                                   f", not {mech!r}.")
        host = d.get("host", default_host)
        port = d.get("port", default_port)
        if host is not None:
            import ipaddress
            try:
                ipaddress.IPv4Address(str(host))
            except ValueError:
                raise LaserConfigError(f"{w}: host has to be an IPv4 address "
                                       f"like 127.0.0.2.") from None
        if port is not None and (isinstance(port, bool) or
                                 not isinstance(port, int) or
                                 not 1 <= port <= 65535):
            raise LaserConfigError(f"{w}: port has to be 1 to 65535.")
        on = check_messages(d.get("on"), f"{w}: on")
        off = check_messages(d.get("off"), f"{w}: off")
        lock = check_messages(d.get("lock"), f"{w}: lock")
        arm = check_messages(d.get("arm"), f"{w}: arm")
        disarm = check_messages(d.get("disarm"), f"{w}: disarm")
        if mech == "osc" and not on:
            raise LaserConfigError(f"{w}: mechanism \"osc\" needs at least "
                                   f"one message in on.")
        if mech == "osc" and not (host and port):
            raise LaserConfigError(f"{w}: mechanism \"osc\" needs BEYOND's "
                                   f"host and port (or a beyond block in "
                                   f"ltcplay_fire_ice.json).")
        note = d.get("note", "")
        if not isinstance(note, str):
            raise LaserConfigError(f"{w}: note has to be text.")
        out.append(Laser(lid.strip(), name.strip(), mech, str(host) if host
                         else None, port, on, off, lock, note, arm=arm,
                         disarm=disarm))
    return out


class _Socket:
    """One UDP socket; never raises OSError into the caller."""

    def __init__(self, factory=None):
        self._factory = factory or (
            lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        self._sock = None
        self.packets_sent = 0
        self.send_errors = 0
        self.last_error = ""

    def send(self, host, port, address, value):
        if not allowed_address(address):
            raise LaserConfigError(f"lasertest refuses to send {address!r}: "
                                   f"not an allowed OSC address.")
        pkt = encode_osc(address, value)
        try:
            if self._sock is None:
                self._sock = self._factory()
            self._sock.sendto(pkt, (host, port))
        except OSError as e:
            self.send_errors += 1
            self.last_error = str(e)
            self.close()
            return False
        self.packets_sent += 1
        return True

    def close(self):
        s, self._sock = self._sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


class LaserTests:
    """The four (or however many) laser test toggles. `locked` is a
    callable(laser_id) -> bool, the engine's lockout; `armed` a
    callable(laser_id) -> bool, the laser arm (laserarm.py): a test goes ON
    only for a laser that is armed, and with no `armed` given nothing is
    armed; `journal` takes (text, **fields)."""

    def __init__(self, lasers, locked=lambda lid: False, journal=None,
                 socket_factory=None, clock=time.monotonic,
                 sleep=time.sleep, armed=None):
        self.lasers = {l.id: l for l in lasers}
        self.order = [l.id for l in lasers]
        self._locked = locked
        self._armed = armed
        self._journal = journal
        self._clock = clock
        self._sleep = sleep
        self._osc = _Socket(socket_factory)
        self._lock = threading.Lock()
        self.on = {}            # laser id -> {"since", "who"}
        self.last_result = {}   # laser id -> "sent" | "failed" | "refused"

    def _note(self, text, **f):
        if self._journal is None:
            return
        try:
            self._journal(text, **f)
        except Exception:
            pass

    def _send_list(self, laser, msgs):
        if not msgs:
            return True
        ok = True
        for addr, val in msgs:
            got = False
            for i in range(RETRY_COUNT):
                if self._osc.send(laser.host, laser.port, addr, val):
                    got = True
                if i < RETRY_COUNT - 1:
                    self._sleep(RETRY_INTERVAL_S)
            ok = ok and got
        return ok

    def is_armed(self, laser_id):
        """True only for a real True from the laser arm. No arm wired, an
        error or anything else: not armed (fail toward dark)."""
        fn = self._armed
        if fn is None:
            return False
        try:
            return fn(laser_id) is True
        except Exception:
            return False

    def set(self, laser_id, on, who="", device=""):
        """Turn one laser's test on or off. (ok, sentence). An ON is
        refused for a locked laser and for a laser whose mechanism is not
        decided; an OFF always tries (it only darkens)."""
        laser = self.lasers.get(laser_id)
        if laser is None:
            return False, f"There is no laser called {laser_id!r}."
        label = who or device or "someone"
        if on:
            if self._locked(laser.id):
                self.last_result[laser.id] = "refused"
                self._note(f"{label} asked for the {laser.name} test, "
                           f"refused: that laser is locked out.",
                           action="laser-test", outcome="refused")
                return False, (f"{laser.name} is locked out, so its test is "
                               f"refused. Unlock it first.")
            if not self.is_armed(laser.id):
                self.last_result[laser.id] = "refused"
                self._note(f"{label} asked for the {laser.name} test, "
                           f"refused: that laser is not armed.",
                           action="laser-test", outcome="refused")
                return False, (f"{laser.name} is not armed, so its test is "
                               f"refused. Arm it first (Lasers, on this "
                               f"page). Nothing was sent.")
            if laser.mechanism == "none" or not laser.on:
                self.last_result[laser.id] = "refused"
                return False, (f"{laser.name}: how the test is sent to BEYOND "
                               f"is not set yet (a placeholder in "
                               f"ltcplay_morning.json, lasers). Nothing was "
                               f"sent.")
            # Recorded ON before the send, so a disarm landing during it
            # sees the test and turns it off; and asked again after it, so
            # a disarm that came first is never outlived by the grid
            # (review of PR #87, P3-5).
            with self._lock:
                self.on[laser.id] = {"since": self._clock(), "who": label}
            ok = self._send_list(laser, laser.on)
            if not self.is_armed(laser.id):
                with self._lock:
                    self.on.pop(laser.id, None)
                if laser.mechanism != "none":
                    self._send_list(laser, laser.off)
                self.last_result[laser.id] = "refused"
                self._note(f"The {laser.name} test was turned straight off "
                           f"again: the laser was disarmed while it went "
                           f"on.", action="laser-test", outcome="refused")
                return False, (f"{laser.name} was disarmed while its test "
                               f"went on, so it was turned off again.")
            self.last_result[laser.id] = "sent" if ok else "failed"
            self._note(f"{label} turned the {laser.name} calibration test "
                       f"ON{'' if ok else ' (the OSC send FAILED)'}.",
                       action="laser-test", outcome="on" if ok else "failed",
                       fault=not ok)
            return ok, (f"{laser.name} test on: alignment grid at low power "
                        f"until switched off." if ok else
                        f"{laser.name} test: the OSC message did NOT get out "
                        f"({self._osc.last_error}).")
        with self._lock:
            was = self.on.pop(laser.id, None)
        ok = self._send_list(laser, laser.off) if laser.mechanism != "none" \
            else True
        self.last_result[laser.id] = "sent" if ok else "failed"
        if was is not None or not ok:
            self._note(f"{label} turned the {laser.name} calibration test "
                       f"OFF{'' if ok else ' (the OSC send FAILED)'}.",
                       action="laser-test", outcome="off" if ok else "failed",
                       fault=not ok)
        return ok, (f"{laser.name} test off." if ok else
                    f"{laser.name} test off was sent but did NOT get out "
                    f"({self._osc.last_error}).")

    def all_off(self, why):
        """Every test that is on, off: a show starting, an Abort, the engine
        stopping. Returns the ids turned off."""
        with self._lock:
            ids = list(self.on)
        for lid in ids:
            self.set(lid, False, who=f"the engine ({why})")
        if ids:
            self._note(f"Laser tests turned off: {why}.",
                       action="laser-test", outcome="all_off")
        return ids

    def send_lock(self, laser_id, why):
        """The laser's `lock` messages (a placeholder until BEYOND's way to
        shut one laser is known), sent when it is locked and at every show
        start while it is. Also turns its test off."""
        laser = self.lasers.get(laser_id)
        if laser is None:
            return False
        self.set(laser_id, False, who=f"the engine ({why})")
        if not laser.lock or laser.mechanism == "none":
            return True
        ok = self._send_list(laser, laser.lock)
        self._note(f"{laser.name}: its lock messages were sent to BEYOND "
                   f"({why}){'' if ok else ' but did NOT get out'}.",
                   action="laser-test", outcome="lock" if ok else "failed",
                   fault=not ok)
        return ok

    def send_placeholder(self, laser_id, which, why):
        """The laser's `arm` or `disarm` PLACEHOLDER messages (ruling 7):
        nothing at all while the list is empty (the shipped default) or the
        mechanism is "none". An `arm` goes only to a laser that is armed
        and not locked out. Returns True when nothing had to go or it got
        out, False when it did not or was refused."""
        laser = self.lasers.get(laser_id)
        if laser is None or which not in ("arm", "disarm"):
            return False
        msgs = laser.arm if which == "arm" else laser.disarm
        if not msgs or laser.mechanism == "none":
            return True
        if which == "arm":
            try:
                locked = bool(self._locked(laser.id))
            except Exception:
                locked = True
            if locked or not self.is_armed(laser.id):
                return False
        ok = self._send_list(laser, msgs)
        self._note(f"{laser.name}: its {which} messages (a placeholder in "
                   f"ltcplay_morning.json) were sent to BEYOND ({why})"
                   f"{'' if ok else ' but did NOT get out'}.",
                   action="laser-arm", outcome=which if ok else "failed",
                   fault=not ok)
        return ok

    def view(self):
        out = []
        for lid in self.order:
            l = self.lasers[lid]
            v = l.view()
            v["on"] = lid in self.on
            v["locked"] = bool(self._locked(lid))
            v["last_result"] = self.last_result.get(lid)
            out.append(v)
        return out

    def close(self):
        self.all_off("the engine is stopping")
        self._osc.close()
