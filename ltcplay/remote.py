"""The show-network remote: operator sign-in with a PIN, and the few show
controls an iPad (or the rack screen) may press, over web.py's server.

What this is, and what it is not
================================

The engine and the show live in the `ltc serve` process. The page is only a
window onto them: closing it, an iPad that sleeps or loses the Wi-Fi, a
session that runs out, none of those is an event this module acts on. There
is no timer here that does anything to the show when a page goes quiet, and
the selftest proves a dropped page changes nothing.

Who may press what
------------------
- On the show machine itself (a loopback request that does not look
  proxied), nothing changes from before: no sign-in, as today. The page
  names the operator and screen it presses from, checked against the lists.
- From anywhere else, every route except the sign-in page needs a session:
  an operator from the existing operator list, signed in with their own PIN
  from a device named on the screen list ("iPad" by default). The session
  is a cookie on that device only. Who pressed and which device are taken
  from the session, never from the request body.
- The Stream Deck keeps Jeff's 2026-10-01 rule: no PIN. It never comes
  through here.

The controls
------------
Start now, Hold, Resume and Abort are the scheduler's own operator events
(schedule_service.Service.operator_press: the same schedule.step every
press goes through, journaled with who and which screen, the Abort latch
saved before the conductor is asked). Reset is Service.reset_conductor.
"Disarm every flame group" is the show's own flames_disarm_all (the flame
link's disarm_all, the same call the conductor's Abort makes), journaled
with who and which screen. Picking the operator is Service.set_operator.

ARMING (Jeff, 2026-10-03, its own PR): the page never arms anything. A
signed-in operator's hold to arm (arm-hold, repeated every 100 ms while the
finger is down) is read by the Stream Deck process (deck-input) as a remote
press of that group's key. The deck owns flamesafe's arm link and runs its
own rules (the hold, the refractory window, the latched refusal) and
flamesafe runs all of its own (consent, dwell, the post-Abort window, the
round-4 veto, the second-copy guard) exactly as for a finger on the deck.
It needs a PIN session even on the show machine, a page status and a
flamesafe status no older than ARM_FRESH_S, heartbeats the engine actually
received for SCREEN_HOLD_S, and screen_arming on in ltcplay_remote.json
(default on). A gap in the heartbeats, an Abort, a disarm or a sign out
lets the hold go; an interrupted hold never carries on.

Fresh state
-----------
Start now, Resume, Reset and choosing the operator act on what the page
shows, so they are refused when the page's last status is more than
FRESH_S old (the page sends the `served_at` of the status it is showing).
Hold, Abort and disarm are never refused for that: they only take risk
away, and a press that reduces risk must never wait on a stale page.

The network
-----------
The engine listens on ONE show-network address (ltcplay_remote.json,
`show_network_address`, set from the page on the show machine) on the fixed
port FIXED_PORT, plus loopback for the machine itself. Anything that looks
proxied (a Forwarded, X-Forwarded-For, Via or similar header) is refused,
from anywhere, loopback included: a proxy or tunnel on the show machine
would make the whole world look like the machine itself. NEVER put remote
access software, a tunnel or a port forward on the show machine.
"""
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import threading
import time
from http import cookies as http_cookies

from . import appdata
from . import settings as settings_mod

# The scheduler's operator and screen lists (schedule_service.OPERATORS_FILE,
# SCREENS_FILE, DEFAULT_SCREENS and schedule.DEFAULT_OPERATORS), repeated
# here so the GPL path can read them without importing the scheduler. The
# selftest checks they match.
OPERATORS_FILE = "ltcplay_operators.json"
SCREENS_FILE = "ltcplay_screens.json"
DEFAULT_OPERATORS = ("Andy", "Jeff")
DEFAULT_SCREENS = ("Rack screen", "Stream Deck", "Phone", "iPad")


def read_names(path, key, default):
    """The names in {key: [names]} at `path`, by schedule_service's rules
    (that one key only, at least one name, each a non-empty string, no
    name twice whatever its case, each stripped), or `default` when the
    file is missing or breaks a rule. Never writes and never raises."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return tuple(default)
    if not isinstance(doc, dict) or set(doc) != {key}:
        return tuple(default)
    names = doc[key]
    if not isinstance(names, list) or not names:
        return tuple(default)
    out, seen = [], set()
    for n in names:
        if not isinstance(n, str) or not n.strip():
            return tuple(default)
        if n.strip().lower() in seen:
            return tuple(default)
        seen.add(n.strip().lower())
        out.append(n.strip())
    return tuple(out)

FIXED_PORT = 7878
SETTINGS_FILE = "ltcplay_remote.json"
PIN_FILE = "ltcplay_remote_pins.json"
COOKIE = "ltcplay_session"

# A PIN is 4 to 8 digits. Stored as PBKDF2-SHA256 with a salt of its own;
# the number of rounds makes one guess cost about a tenth of a second.
PIN_MIN, PIN_MAX = 4, 8
PBKDF2_ITERATIONS = 200_000

# Wrong PINs: the first FREE_TRIES cost nothing extra; after that each one
# locks that device's address AND that operator's name out for
# LOCK_BASE_S * 2**(n - FREE_TRIES - 1), capped at LOCK_MAX_S. A right PIN
# clears both counts.
FREE_TRIES = 3
LOCK_BASE_S = 5.0
LOCK_MAX_S = 300.0

# A session ends after this long without a request, and in any case after
# SESSION_MAX_S. Ending it changes nothing on the show; the operator signs
# in again to press anything.
SESSION_IDLE_S = 12 * 3600
SESSION_MAX_S = 18 * 3600

# How old the page's status may be for a press that acts on what it shows.
FRESH_S = 2.0
# How old flamesafe's last status may be before the lamps read "stale"
# (CONTRACT.md: no status frame for 1 s is red for the safety program).
FLAME_STALE_S = 1.0

# The routes this module answers, and nothing else. No "arm" among them.
CONTROL_ROUTES = ("start-now", "hold", "resume", "abort", "reset",
                  "disarm-all", "operator")
# Programming-session transport (Jeff, 2026-10-03): play from a timecode,
# jump, pause and continue, an A/B loop, back to following timecode. Only
# in a programming session (a show started from the page as Rehearse, or in
# Rehearsal mode, or a rehearsal conductor), and never while a scheduled
# show is live; see _scrub_refusal.
TRANSPORT_ROUTES = ("play-from", "jump", "pause", "continue", "mark-a",
                    "mark-b", "loop", "follow")
FRESH_ROUTES = frozenset(("start-now", "resume", "reset", "operator")
                         + TRANSPORT_ROUTES)
JUMP_MAX_S = 600.0
CONFIRM_ROUTES = frozenset(("start-now", "abort"))
# Arming from a screen (Jeff, 2026-10-03; its own PR and safety review).
# The page never arms anything itself: a hold here is a remote press of the
# group's key on the Stream Deck process, which owns flamesafe's arm link
# and runs every one of its own and flamesafe's rules on it. See
# flamesafe/CONTRACT.md, "Arming from a screen".
ARM_ROUTES = ("arm-hold", "arm-release", "group-disarm")
ARM_FRESH_S = 1.0      # the page's status, and flamesafe's, no older
BEAT_STALE_S = 0.25    # a hold with no heartbeat for this long is let go
SCREEN_HOLD_S = 1.0    # beat-evidenced hold before the deck may fire
GET_ROUTES = ("whoami", "status", "network", "deck-input")
POST_ROUTES = CONTROL_ROUTES + TRANSPORT_ROUTES + ARM_ROUTES + (
    "login", "logout", "pin", "network")
LOCAL_ONLY = frozenset(("pin", "network"))

# Any of these on a request means something forwarded it. A browser on the
# show network never sends them; a reverse proxy, tunnel or CDN does.
PROXY_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host",
                 "x-forwarded-proto", "x-forwarded-port",
                 "x-forwarded-server", "x-real-ip", "via",
                 "x-original-forwarded-for", "cf-connecting-ip",
                 "true-client-ip", "x-client-ip", "x-cluster-client-ip",
                 "fastly-client-ip", "client-ip", "x-proxyuser-ip",
                 "x-original-url", "x-rewrite-url", "cf-ray",
                 "x-amzn-trace-id", "ngrok-trace-id")

LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1", "[::1]")


def settings_folder():
    """The same place as this machine's other settings."""
    return appdata.folder() if appdata.WINDOWS else settings_mod.folder()


def _write_json(path, doc, private=False):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    if private and os.name != "nt":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# --------------------------------------------------------- the network --

def check_show_address(text):
    """The show-network address as written, or ValueError with a sentence.
    One real address of this machine: never every interface, never
    loopback, never multicast."""
    text = str(text or "").strip()
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        raise ValueError(f"{text!r} is not an address. Write this machine's "
                         f"address on the show Wi-Fi, like 10.20.0.5.")
    if ip.is_unspecified:
        raise ValueError(f"{text} means every network this machine is on, "
                         f"the venue's and the internet's included. Use its "
                         f"address on the show Wi-Fi only.")
    if ip.is_loopback:
        raise ValueError(f"{text} is the machine itself; an iPad cannot "
                         f"reach it. Use the address on the show Wi-Fi.")
    if ip.is_multicast or ip.is_link_local and ip.version == 6:
        raise ValueError(f"{text} cannot be served on. Use the address on "
                         f"the show Wi-Fi.")
    return str(ip)


def load_settings(folder=None):
    """{"show_network_address": str or None, "flamesafe_config": str or
    None}. A missing file is all None; a broken one raises ValueError."""
    path = os.path.join(folder or settings_folder(), SETTINGS_FILE)
    # screen_arming is OFF unless the file says true (review of PR #43,
    # P0-4): arming from a screen came in from PR #41, whose own safety
    # review decides whether it is switched on.
    out = {"show_network_address": None, "flamesafe_config": None,
           "screen_arming": False, "path": path}
    if not os.path.exists(path):
        return out
    try:
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as e:
        raise ValueError(f"{path} could not be read: {e}")
    if not isinstance(doc, dict):
        raise ValueError(f"{path} is not a settings object.")
    if doc.get("show_network_address"):
        out["show_network_address"] = check_show_address(
            doc["show_network_address"])
    if doc.get("flamesafe_config"):
        out["flamesafe_config"] = str(doc["flamesafe_config"])
    if "screen_arming" in doc:
        # One switch for screen and browser arming, off by default. Only
        # the JSON word true turns it on; anything else that is not false
        # is refused rather than guessed at.
        v = doc["screen_arming"]
        if v is not True and v is not False:
            raise ValueError(f"{path}: screen_arming must be true or false.")
        out["screen_arming"] = v
    return out


def save_settings(folder, **changes):
    path = os.path.join(folder or settings_folder(), SETTINGS_FILE)
    doc = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                doc = json.load(fh) or {}
        except (OSError, ValueError):
            doc = {}
    doc.update(changes)
    _write_json(path, doc)
    return load_settings(folder)


def address_candidates():
    """This machine's IPv4 addresses, for the page to offer. Best effort:
    the stdlib has no interface list, so this asks the name service and
    the routing table, and the operator can also type one."""
    seen = []
    try:
        for fam, _t, _p, _c, sa in socket.getaddrinfo(
                socket.gethostname(), None, socket.AF_INET):
            a = sa[0]
            if a not in seen and not a.startswith("127."):
                seen.append(a)
    except OSError:
        pass
    try:
        k = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            k.connect(("192.0.2.1", 9))        # TEST-NET-1; nothing is sent
            a = k.getsockname()[0]
            if a not in seen and not a.startswith("127."):
                seen.append(a)
        finally:
            k.close()
    except OSError:
        pass
    return seen


def looks_proxied(headers):
    """The first forwarding header on the request, or ""."""
    for h in PROXY_HEADERS:
        if headers.get(h) is not None:
            return h
    return ""


def host_ok(host_header, client_is_loopback, bind, port):
    """True when the Host header names this server the way the client
    reached it. Stops a web page elsewhere from steering a browser at the
    engine under another name (DNS rebinding)."""
    if not host_header:
        return client_is_loopback          # curl and the launcher's checks
    h = host_header.strip().lower()
    if h.startswith("["):
        name = h[:h.find("]") + 1] if "]" in h else h
        rest = h[len(name):]
    else:
        name, _, rest = h.partition(":")
        rest = ":" + rest if rest else ""
    if rest and rest != f":{port}":
        return False
    if client_is_loopback:
        return name in LOOPBACK_NAMES
    want = bind.lower()
    return name == want or name == f"[{want}]"


# --------------------------------------------------------------- PINs --

class PinStore:
    """Operator PINs, hashed, in the settings folder. Never the PIN."""

    def __init__(self, folder, iterations=PBKDF2_ITERATIONS):
        self.path = os.path.join(folder, PIN_FILE)
        self.iterations = iterations
        self._lock = threading.Lock()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return {}
        pins = doc.get("pins") if isinstance(doc, dict) else None
        return pins if isinstance(pins, dict) else {}

    def names(self):
        return sorted(self._load())

    def has(self, name):
        return name.lower() in {n.lower() for n in self._load()}

    @staticmethod
    def check_pin(pin):
        pin = str(pin or "")
        if not (pin.isdigit() and PIN_MIN <= len(pin) <= PIN_MAX
                and pin.isascii()):
            raise ValueError(f"A PIN is {PIN_MIN} to {PIN_MAX} digits.")
        return pin

    def _hash(self, pin, salt, iterations):
        return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt,
                                   iterations).hex()

    def set(self, name, pin):
        pin = self.check_pin(pin)
        salt = secrets.token_bytes(16)
        with self._lock:
            pins = self._load()
            pins = {k: v for k, v in pins.items()
                    if k.lower() != name.lower()}
            pins[name] = {"salt": salt.hex(), "iterations": self.iterations,
                          "hash": self._hash(pin, salt, self.iterations)}
            _write_json(self.path, {"pins": pins}, private=True)

    def clear(self, name):
        with self._lock:
            pins = {k: v for k, v in self._load().items()
                    if k.lower() != name.lower()}
            _write_json(self.path, {"pins": pins}, private=True)

    def verify(self, name, pin):
        rec = None
        for k, v in self._load().items():
            if k.lower() == str(name or "").lower():
                rec = v
        pin = str(pin or "")
        if not isinstance(rec, dict):
            # Spend the same time as a real check, so a missing PIN does
            # not read differently from a wrong one.
            self._hash(pin, b"\0" * 16, self.iterations)
            return False
        try:
            salt = bytes.fromhex(rec["salt"])
            want = str(rec["hash"])
            n = int(rec.get("iterations") or self.iterations)
        except (KeyError, ValueError, TypeError):
            return False
        return hmac.compare_digest(self._hash(pin, salt, n), want)


class Throttle:
    """Counts wrong PINs per key (a device address, an operator name)."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._fails = {}
        self._until = {}
        self._lock = threading.Lock()
        self._key_locks = {}

    def serial(self, keys):
        """Fix round 1 of #39, F3: one PIN check at a time per device
        address and per name. Without it, 24 overlapping guesses all read
        "not locked out" before any of them recorded a failure, and 20 were
        checked. Held across the whole check-verify-record, so the lock-out
        is seen by the very next guess. The locks are taken in a fixed
        order, so two keys can never deadlock."""
        import contextlib
        with self._lock:
            locks = [self._key_locks.setdefault(k, threading.Lock())
                     for k in sorted(keys)]

        @contextlib.contextmanager
        def held():
            for lk in locks:
                lk.acquire()
            try:
                yield
            finally:
                for lk in reversed(locks):
                    lk.release()
        return held()

    def wait_s(self, keys):
        now = self.clock()
        with self._lock:
            return max([self._until.get(k, 0.0) - now for k in keys] + [0.0])

    def fail(self, keys):
        now = self.clock()
        with self._lock:
            for k in keys:
                n = self._fails.get(k, 0) + 1
                self._fails[k] = n
                if n > FREE_TRIES:
                    lock = min(LOCK_MAX_S,
                               LOCK_BASE_S * 2 ** (n - FREE_TRIES - 1))
                    self._until[k] = now + lock

    def succeed(self, keys):
        with self._lock:
            for k in keys:
                self._fails.pop(k, None)
                self._until.pop(k, None)


class Sessions:
    """Signed-in devices, in memory. A restart signs everyone out, which
    changes nothing on the show."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._by_token = {}
        self._lock = threading.Lock()

    def create(self, who, device, ip):
        tok = secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock:
            self._by_token[tok] = {"who": who, "device": device, "ip": ip,
                                   "created": now, "seen": now}
        return tok

    def get(self, tok):
        if not tok:
            return None
        now = self.clock()
        with self._lock:
            s = self._by_token.get(tok)
            if s is None:
                return None
            if now - s["seen"] > SESSION_IDLE_S or \
                    now - s["created"] > SESSION_MAX_S:
                del self._by_token[tok]
                return None
            s["seen"] = now
            return dict(s, token=tok)

    def drop(self, tok):
        with self._lock:
            return self._by_token.pop(tok, None) is not None

    def drop_who(self, who):
        with self._lock:
            for t in [t for t, s in self._by_token.items()
                      if s["who"].lower() == who.lower()]:
                del self._by_token[t]


# ------------------------------------------------ flamesafe's status --

def load_flamesafe_status_link(path):
    """(ip, mirror_port, key, group names) from a flamesafe config, read as
    plain JSON (ltcplay never imports flamesafe). ValueError when it has no
    status_mirror_port: the deck holds status_port."""
    with open(path, encoding="utf-8-sig") as fh:
        doc = json.load(fh)
    link = doc.get("link") or {}
    if "status_mirror_port" not in link:
        raise ValueError(f"{path} has no link.status_mirror_port, so "
                         f"flamesafe sends this engine no copy of its "
                         f"status. Add one (a free loopback port, like "
                         f"5574) to show the flame lamps on the page.")
    names = [str(g.get("name")) for g in (doc.get("groups") or [])]
    return (link.get("status_ip", "127.0.0.1"),
            int(link["status_mirror_port"]), str(link["key"]), names)


def decode_status(data, key):
    """A flamesafe status frame, or None unless it is keyed with ours."""
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or obj.get("t") != "status":
        return None
    if not hmac.compare_digest(str(obj.get("k", "")), key):
        return None
    return obj


class FlameStatus:
    """flamesafe's status frames, read for display only. CONTRACT.md:
    there is no path from a status frame to flame output, and nothing read
    here is ever sent anywhere."""

    def __init__(self, ip, port, key, names=(), clock=time.monotonic):
        self.ip, self.port, self.key = ip, port, key
        self.names = list(names)
        self.clock = clock
        self.last = None
        self.last_at = None
        self._sock = None
        self._stop = threading.Event()

    @classmethod
    def from_config(cls, path, clock=time.monotonic):
        ip, port, key, names = load_flamesafe_status_link(path)
        return cls(ip, port, key, names, clock=clock)

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind((self.ip, self.port))
        s.settimeout(0.2)
        self._sock = s
        threading.Thread(target=self._loop, daemon=True,
                         name="ltcplay-flame-status").start()
        return self

    def _loop(self):
        while not self._stop.is_set():
            try:
                data, _a = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                time.sleep(0.2)
                continue
            self.note(data)

    def note(self, data):
        obj = decode_status(data, self.key)
        if obj is not None:
            self.last, self.last_at = obj, self.clock()

    def stop(self):
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def view(self):
        age = (None if self.last_at is None
               else self.clock() - self.last_at)
        stale = age is None or age > FLAME_STALE_S
        groups = []
        last = self.last or {}
        for g in last.get("groups") or []:
            groups.append({"name": str(g.get("name", "")),
                           "armed": str(g.get("armed", "")),
                           "wanted": bool(g.get("wanted")),
                           "reason": str(g.get("reason", "")),
                           "dwell_s": g.get("dwell_s", 0)})
        if not groups:
            groups = [{"name": n, "armed": "unknown", "wanted": False,
                       "reason": "", "dwell_s": 0} for n in self.names]
        if stale:
            # Never show a lamp from an old frame as if it were now.
            for g in groups:
                g["armed"] = "unknown"
        return {"connected": True, "stale": stale,
                "age_ms": None if age is None else int(age * 1000),
                "fault": str(last.get("fault", "")) if not stale else "",
                "groups": groups}


# ------------------------------------------------------- the remote --

class Ctx:
    """Who is asking: the machine itself, or a signed-in device."""

    def __init__(self, local, ip, session=None):
        self.local = local
        self.ip = ip
        self.session = session

    @property
    def allowed(self):
        return self.local or self.session is not None


def cookie_token(header):
    if not header:
        return ""
    try:
        c = http_cookies.SimpleCookie()
        c.load(header)
    except http_cookies.CookieError:
        return ""
    m = c.get(COOKIE)
    return m.value if m is not None else ""


class Remote:
    """The routes under /api/remote/. `control` is web.Control, `schedule`
    the scheduler service (or None), `flame_status` a FlameStatus (or
    None), `flame_disarm` a callable(reason) returning something with .ok
    and .sentence (the show's flames_disarm_all), or None to take it from
    the scheduler's conductor when one is attached."""

    def __init__(self, control, schedule=None, folder=None,
                 flame_status=None, flame_disarm=None,
                 clock=time.monotonic, wall=time.time,
                 iterations=PBKDF2_ITERATIONS, log=print):
        self.control = control
        self.schedule = schedule
        self.folder = folder or (schedule.state_dir if schedule is not None
                                 else settings_folder())
        self.pins = PinStore(self.folder, iterations=iterations)
        self.sessions = Sessions(clock)
        self.throttle = Throttle(clock)
        self.flame_status = flame_status
        self._flame_disarm = flame_disarm
        self.clock = clock
        self.wall = wall
        self._log = log
        self.marks = {"a": None, "b": None}     # loop marks, show seconds
        # Screen arm holds: group index -> {token, who, device, start,
        # beat, id}; per-group disarms waiting for the deck to read them.
        import collections
        self._arm_lock = threading.Lock()
        self._holds = {}
        self._hold_ids = 0
        self._disarms = collections.deque(maxlen=64)
        self._disarm_ids = 0

    # -- who ---------------------------------------------------------------
    # With no scheduler (the GPL path) the lists are read here, read-only,
    # by the same rules as schedule_service.load_operators/load_screens, so
    # the GPL path never imports the scheduler (PR #43 review, finding 7).
    # Nothing is written: a missing file means the defaults. The selftest
    # holds both readers and both default lists to the same answers.
    def operators(self):
        if self.schedule is not None:
            return list(self.schedule.operators)
        return list(read_names(os.path.join(self.folder, OPERATORS_FILE),
                               "operators", DEFAULT_OPERATORS))

    def screens(self):
        if self.schedule is not None:
            return list(self.schedule.screens)
        return list(read_names(os.path.join(self.folder, SCREENS_FILE),
                               "screens", DEFAULT_SCREENS))

    def context(self, local, ip, cookie_header):
        # The machine itself is let in with or without a session, as
        # before; a session there still names who is pressing, and arming
        # needs one wherever it comes from.
        return Ctx(local, ip, self.sessions.get(cookie_token(cookie_header)))

    def _actor(self, ctx, body):
        """(who, screen) for a press: the session's on the network, the
        page's own choice on the machine itself."""
        if ctx.session is not None:
            return ctx.session["who"], ctx.session["device"]
        who = str(body.get("who") or "").strip()
        if not who and self.schedule is not None:
            who = self.schedule.current_operator or ""
        screen = str(body.get("screen") or "Rack screen").strip()
        return who, screen

    def _journal(self, who, screen, action, outcome, text, fault=False):
        if self.schedule is not None:
            try:
                self.schedule.journal_press(who, screen, action, outcome,
                                            text, fault=fault)
                return
            except Exception:
                pass
        s = getattr(self.control, "session", None)
        log = getattr(s, "log", None)
        try:
            if log is not None:
                log.event("remote", text)
            else:
                self._log(text)
        except Exception:
            pass

    # -- the routes --------------------------------------------------------
    def get(self, route, ctx):
        name = route[len("/api/remote/"):]
        if name == "whoami":
            return 200, self.whoami(ctx)
        if name == "deck-input":
            # The Stream Deck process, on this machine. Never the network:
            # it carries the holds the deck acts on.
            if not ctx.local:
                return 403, {"error": "Only on the show machine itself."}
            return 200, self.deck_input()
        if not ctx.allowed:
            return 401, {"error": "Sign in with your PIN first."}
        if name == "status":
            return 200, self.status(ctx)
        if name == "network":
            if not ctx.local:
                return 403, {"error": "Only on the show machine itself."}
            return 200, self.network_view()
        return 404, {"error": "no such thing here"}

    def post(self, route, body, ctx):
        """(code, body, extra headers)."""
        name = route[len("/api/remote/"):]
        body = body if isinstance(body, dict) else {}
        if name == "login":
            return self.login(body, ctx)
        if name == "logout":
            return self.logout(ctx)
        if not ctx.allowed:
            return 401, {"error": "Sign in with your PIN first."}, {}
        if name in LOCAL_ONLY and not ctx.local:
            return 403, {"error": "PINs and the show network are set on the "
                                  "show machine itself, not from here."}, {}
        if name == "pin":
            return (*self.set_pin(body),) + ({},)
        if name == "network":
            return (*self.set_network(body),) + ({},)
        if name in CONTROL_ROUTES or name in TRANSPORT_ROUTES:
            return (*self.press(name, body, ctx),) + ({},)
        if name == "arm-hold":
            return (*self.arm_hold(body, ctx),) + ({},)
        if name == "arm-release":
            return (*self.arm_release(body, ctx),) + ({},)
        if name == "group-disarm":
            return (*self.group_disarm(body, ctx),) + ({},)
        return 404, {"error": "no such thing here"}, {}

    # -- sign in -----------------------------------------------------------
    def whoami(self, ctx):
        s = ctx.session
        out = {"local": ctx.local, "signed_in": s is not None,
               "who": s["who"] if s else None,
               "device": s["device"] if s else None,
               "operators": self.operators(),
               "screens": [n for n in self.screens()
                           if n.lower() != "stream deck"],
               "fresh_s": FRESH_S}
        if ctx.local:
            out["pins_set"] = self.pins.names()
        return out

    def login(self, body, ctx):
        who = str(body.get("who") or "").strip()
        device = str(body.get("device") or "").strip()
        pin = str(body.get("pin") or "")
        names = {n.lower(): n for n in self.operators()}
        screens = {n.lower(): n for n in self.screens()
                   if n.lower() != "stream deck"}
        if who.lower() not in names:
            return 400, {"error": "Pick your name from the list."}, {}
        if device.lower() not in screens:
            return 400, {"error": "Pick this device's name from the list."}, {}
        who, device = names[who.lower()], screens[device.lower()]
        keys = (("ip", ctx.ip), ("who", who.lower()))
        with self.throttle.serial(keys):
            refused = self._check_pin(who, device, pin, keys, ctx)
            if refused is not None:
                return refused
            self.throttle.succeed(keys)
        tok = self.sessions.create(who, device, ctx.ip)
        self._journal(who, device, "sign in", "done",
                      f"{who} signed in on the {device} ({ctx.ip}).")
        cookie = (f"{COOKIE}={tok}; Path=/; HttpOnly; SameSite=Strict; "
                  f"Max-Age={SESSION_MAX_S}")
        return 200, {"ok": True, "who": who, "device": device}, \
            {"Set-Cookie": cookie}

    def _check_pin(self, who, device, pin, keys, ctx):
        """None when the PIN is right; else the refusal to send. Called
        only with throttle.serial(keys) held."""
        wait = self.throttle.wait_s(keys)
        if wait > 0:
            self._journal(who, device, "sign in", "refused",
                          f"A sign in as {who} from {ctx.ip} was refused "
                          f"without checking the PIN: too many wrong PINs, "
                          f"{wait:.0f} s left to wait.")
            return 429, {"error": f"Too many wrong PINs. Wait {wait:.0f} s "
                                  f"and try again.",
                         "wait_s": round(wait, 1)}, {}
        if not self.pins.has(who):
            self.pins.verify(who, pin)        # same time as a real check
            self.throttle.fail(keys)
            self._journal(who, device, "sign in", "refused",
                          f"{who} tried to sign in from {ctx.ip} but has no "
                          f"PIN set. Set one on the show machine.")
            # The same answer as a wrong PIN: the network is never told
            # which operators have a PIN (fix round 1, A10b). The journal
            # on the show machine says the truth.
            return 403, {"error": "That PIN is not right."}, {}
        if not self.pins.verify(who, pin):
            self.throttle.fail(keys)
            self._journal(who, device, "sign in", "refused",
                          f"A wrong PIN for {who} from {ctx.ip} "
                          f"({device}).")
            return 403, {"error": "That PIN is not right."}, {}
        return None

    def logout(self, ctx):
        s = ctx.session
        if s is not None:
            self.sessions.drop(s["token"])
            with self._arm_lock:
                for i in [i for i, h in self._holds.items()
                          if h["token"] == s["token"]]:
                    del self._holds[i]
            self._journal(s["who"], s["device"], "sign out", "done",
                          f"{s['who']} signed out on the {s['device']}.")
        return 200, {"ok": True}, {
            "Set-Cookie": f"{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; "
                          f"Max-Age=0"}

    def set_pin(self, body):
        who = str(body.get("who") or "").strip()
        names = {n.lower(): n for n in self.operators()}
        if who.lower() not in names:
            return 400, {"error": "Pick a name from the operator list."}
        who = names[who.lower()]
        pin = str(body.get("pin") or "")
        if not pin:
            self.pins.clear(who)
            self.sessions.drop_who(who)
            self._journal(who, "Rack screen", "PIN", "done",
                          f"{who}'s PIN was removed on the show machine. "
                          f"{who} can no longer sign in from the network.")
            return 200, {"ok": True, "pins_set": self.pins.names()}
        try:
            self.pins.set(who, pin)
        except ValueError as e:
            return 400, {"error": str(e)}
        # A new PIN signs out every device signed in with the old one.
        self.sessions.drop_who(who)
        self._journal(who, "Rack screen", "PIN", "done",
                      f"{who}'s PIN was set on the show machine.")
        return 200, {"ok": True, "pins_set": self.pins.names()}

    def network_view(self):
        try:
            saved = load_settings(self.folder)
            err = None
        except ValueError as e:
            saved, err = {}, str(e)
        return {"saved": saved.get("show_network_address"), "error": err,
                "port": FIXED_PORT, "candidates": address_candidates()}

    def set_network(self, body):
        try:
            addr = check_show_address(body.get("address"))
            save_settings(self.folder, show_network_address=addr)
        except (ValueError, OSError) as e:
            return 400, {"error": str(e)}
        return 200, dict(self.network_view(), ok=True,
                         note="Saved. Quit the Web ltcplay window and start "
                              "it again on the show network to use it.")

    # -- presses -----------------------------------------------------------
    def _stale(self, body):
        """A sentence when the page's status is too old for this press."""
        try:
            seen = float(body.get("seen"))
        except (TypeError, ValueError):
            return ("The page has not shown a status yet. Wait for it to "
                    "connect, then press again. Nothing was changed.")
        age = self.wall() - seen / 1000.0
        if age > FRESH_S or age < -FRESH_S:
            return (f"The page was showing a status {age:.1f} s old, so "
                    f"this press was refused. Wait for it to reconnect, "
                    f"then press again. Nothing was changed.")
        return None

    def press(self, name, body, ctx):
        who, screen = self._actor(ctx, body)
        if name in FRESH_ROUTES:
            why = self._stale(body)
            if why:
                self._journal(who, screen, name, "refused",
                              f"{who or 'Someone'}'s {name} on the {screen} "
                              f"was refused. {why}")
                return 409, {"error": why, "stale": True}
        if name in CONFIRM_ROUTES and body.get("confirmed") is not True:
            why = "Confirm it on the page first. Nothing was changed."
            self._journal(who, screen, name, "refused",
                          f"{who or 'Someone'}'s {name} on the {screen} was "
                          f"refused. {why}")
            return 400, {"error": why}
        flames = None
        if name == "abort":
            # The flames first, every time (review of PR #43, P0-1): every
            # flame group disarmed through the flame link before any
            # scheduler step, any lock or any journal line, whatever the
            # scheduler is doing and whether or not a show is live. The
            # scheduler takes an Abort only in SHOW or PAUSED, and the
            # conductor only while something plays; a group armed before a
            # show, between shows or after one must still come off. Nothing
            # here waits on a disk (P0-5).
            flames = self._disarm_now(who, screen, "Abort")
        if name in ("abort", "disarm-all"):
            # No screen hold survives an Abort or a disarm, whoever pressed
            # it and whether or not it goes on to be accepted.
            self.cancel_holds(f"{who or 'someone'} pressed {name} on the "
                              f"{screen}")
        if name == "disarm-all":
            return self.disarm_all(who, screen)
        if name == "abort":
            return self.abort(who, screen, flames)
        if name in TRANSPORT_ROUTES:
            return self.transport(name, body, who, screen)
        svc = self.schedule
        if svc is None:
            return 409, {"error": "There is no scheduler running in this "
                                  "engine, so there is no show to press "
                                  "this on. Nothing was changed."}
        try:
            if name == "operator":
                if ctx.session is not None:
                    want = str(body.get("pick") or "").strip()
                    if want.lower() != who.lower():
                        return 403, {
                            "error": f"Signed in as {who}, this device can "
                                     f"only pick {who}. To pick someone "
                                     f"else, sign in as them with their "
                                     f"own PIN."}
                    pick = who
                else:
                    pick = str(body.get("pick") or "").strip()
                return 200, svc.set_operator({"who": pick, "screen": screen})
            if name == "reset":
                r = svc.reset_conductor(who, screen)
            else:
                r = svc.operator_press(name, who, screen,
                                       confirmed=body.get("confirmed") is True)
        except ValueError as e:
            return 400, {"error": str(e)}
        return (200 if r.get("ok") else 409), r

    # -- programming-session transport ----------------------------------
    LIVE_STATES = ("SHOW", "PAUSED")

    def programming(self):
        """(True, "") in a programming session, else (False, why)."""
        svc = self.schedule
        if svc is not None:
            m = getattr(svc, "machine", None)
            if m is not None and m.state in self.LIVE_STATES:
                return False, ("A scheduled show is live. Only Hold, Resume "
                               "and Abort work during a show; scrubbing is "
                               "for programming sessions.")
        s = getattr(self.control, "session", None)
        p = getattr(s, "player", None)
        if s is None or not s.running or p is None:
            return False, ("Nothing is playing. Start the show on the show "
                           "machine with Rehearse, or in Rehearsal mode, to "
                           "scrub it.")
        cond = getattr(svc, "conductor", None)
        rehearsal = (getattr(s, "no_output", False)
                     or getattr(p, "on_lost", "") == "hold"
                     or getattr(cond, "mode", None) == "rehearsal")
        if not rehearsal:
            return False, ("This show was started in Show mode. Scrubbing "
                           "only works in a programming session: start it "
                           "with Rehearse, or in Rehearsal mode.")
        return True, ""

    def transport(self, name, body, who, screen):
        ok, why = self.programming()
        label = who or "Someone"
        if not ok:
            self._journal(who, screen, name, "refused",
                          f"{label}'s {name} on the {screen} was refused. "
                          f"{why}")
            return 409, {"error": why}
        c = self.control
        p = c.session.player
        tl = c.session.tl

        def here():
            if p.freerun_paused_at is not None:
                return p.freerun_paused_at
            tc = p.tc_seconds
            if tc is None or tc < 0:
                raise ValueError("There is no show time to start from yet. "
                                 "Play from a timecode first.")
            return tc

        def ensure_freerun():
            if p.freerun_epoch is None:
                c.go(tl.format(here()))

        try:
            if name == "play-from":
                at = str(body.get("at") or "").strip()
                if not at:
                    raise ValueError("Type a timecode to play from, like "
                                     "00:01:30:00, or a cue name.")
                r = c.go(at)
                said = f"played from {r['at']}"
            elif name == "jump":
                try:
                    secs = float(body.get("seconds"))
                except (TypeError, ValueError):
                    raise ValueError("Jump by a number of seconds.")
                if not (-JUMP_MAX_S <= secs <= JUMP_MAX_S) or secs == 0:
                    raise ValueError(f"Jump by up to {JUMP_MAX_S:g} s either "
                                     f"way.")
                ensure_freerun()
                at = p.nudge(secs)
                said = f"jumped {secs:+g} s to {tl.format(at)}"
            elif name == "pause":
                ensure_freerun()
                at = p.freerun_pause(True)
                said = f"paused at {tl.format(at)}"
            elif name == "continue":
                ensure_freerun()
                at = p.freerun_pause(False)
                said = f"continued from {tl.format(at)}"
            elif name in ("mark-a", "mark-b"):
                at = here()
                self.marks[name[-1]] = at
                said = f"set mark {name[-1].upper()} at {tl.format(at)}"
            elif name == "loop":
                if body.get("on") is True:
                    a, b = self.marks["a"], self.marks["b"]
                    if a is None or b is None:
                        raise ValueError("Set mark A and mark B first.")
                    p.set_loop(a, b)
                    if p.freerun_epoch is None:
                        c.go(tl.format(a))
                    said = (f"turned the loop on, {tl.format(a)} to "
                            f"{tl.format(b)}")
                else:
                    p.set_loop(None, None)
                    said = "turned the loop off"
            else:                                   # follow
                c.release()
                said = "handed the show back to the timecode"
        except ValueError as e:
            self._journal(who, screen, name, "refused",
                          f"{label}'s {name} on the {screen} was refused. "
                          f"{e}")
            return 400, {"error": str(e)}
        except Exception as e:                      # SessionError and kin
            self._journal(who, screen, name, "refused",
                          f"{label}'s {name} on the {screen} was refused. "
                          f"{e}")
            return 400, {"error": str(e)}
        text = f"{label} {said} on the {screen} (programming session)."
        self._journal(who, screen, name, "done", text)
        return 200, {"ok": True, "text": text}

    def _transport_view(self):
        s = getattr(self.control, "session", None)
        p = getattr(s, "player", None)
        ok, why = self.programming()
        out = {"programming": ok, "why": why, "freerun": False,
               "paused": False, "loop": None, "marks": {}}
        if p is None or s is None:
            return out
        tl = s.tl
        out["freerun"] = p.freerun_epoch is not None
        out["paused"] = p.freerun_paused_at is not None
        out["loop"] = ([tl.format(p.loop[0]), tl.format(p.loop[1])]
                       if p.loop else None)
        out["marks"] = {k: (tl.format(v) if v is not None else None)
                        for k, v in self.marks.items()}
        return out

    # -- arming from a screen (its own PR) ---------------------------------
    def arming_enabled(self):
        try:
            return load_settings(self.folder)["screen_arming"] is True
        except (ValueError, OSError):
            return False              # a broken settings file: off

    def _group_index(self, body):
        """(index, name) of the flame group the page named, from the names
        flamesafe itself reports, or ValueError."""
        fs = self.flame_status
        names = []
        if fs is not None:
            names = [g["name"] for g in fs.view()["groups"]]
        g = body.get("group")
        if isinstance(g, int) and not isinstance(g, bool) and \
                0 <= g < len(names):
            return g, names[g]
        for i, n in enumerate(names):
            if isinstance(g, str) and n.lower() == g.strip().lower():
                return i, n
        raise ValueError("That is not a flame group flamesafe reports.")

    def _drop_hold(self, i, token=None):
        with self._arm_lock:
            h = self._holds.get(i)
            if h is not None and (token is None or h["token"] == token):
                del self._holds[i]
                return h
        return None

    def cancel_holds(self, why):
        """Every screen arm hold let go, at once (an Abort, a Hold, a
        disarm). The deck sees them gone on its next read."""
        with self._arm_lock:
            gone, self._holds = self._holds, {}
        for h in gone.values():
            self._journal(h["who"], h["device"], "arm hold", "cancelled",
                          f"{h['who']}'s hold to arm group {h['group']} on "
                          f"the {h['device']} was let go: {why}.")
        return len(gone)

    def arm_hold(self, body, ctx):
        """The page's hold to arm one group: the first call starts it, and
        the page repeats it every 100 ms while the finger stays down. Each
        call checks everything again; any check that fails lets go."""
        if not self.arming_enabled():
            # First, before anything else is looked at (review of PR #43,
            # P0-4): with screen arming switched off nothing about a hold
            # is accepted, whoever asks.
            return 403, {"error": "Arming from a screen is switched off in "
                                  "ltcplay_remote.json. Arm from the Stream "
                                  "Deck.", "let_go": True}
        s = ctx.session
        if s is None:
            return 401, {"error": "Arming needs your own PIN sign in, even "
                                  "on the show machine."}
        who, device, token = s["who"], s["device"], s["token"]
        try:
            i, gname = self._group_index(body)
        except ValueError as e:
            return 400, {"error": str(e)}

        def refuse(code, why):
            h = self._drop_hold(i, token)
            if h is not None or body.get("hold_id") is None:
                self._journal(who, device, "arm hold", "refused",
                              f"{who}'s hold to arm {gname} on the "
                              f"{device} was refused. {why}")
            return code, {"error": why, "let_go": True}
        try:
            seen = float(body.get("seen"))
        except (TypeError, ValueError):
            seen = None
        if seen is None or abs(self.wall() - seen / 1000.0) > ARM_FRESH_S:
            return refuse(409, "The page's status is more than 1 s old. "
                               "Wait for it to be live, then hold again.")
        fl = self.flame_status.view() if self.flame_status else None
        if not fl or fl["stale"] or fl["age_ms"] is None or \
                fl["age_ms"] > ARM_FRESH_S * 1000:
            return refuse(409, "flamesafe has not reported in the last "
                               "second, so its real armed state is not "
                               "known. Nothing can arm from here until it "
                               "does.")
        g = fl["groups"][i]
        if g["armed"] == "armed" or g["wanted"]:
            return refuse(409, f"{gname} is already armed or asked for.")
        now = self.clock()
        if body.get("hold_id") is not None:
            # A heartbeat for a hold already under way. If that hold is not
            # the live one any more (a gap longer than BEAT_STALE_S, an
            # Abort, a disarm, a restart), it is over: a hold that was
            # interrupted never carries on, the finger has to come up and
            # go down again.
            with self._arm_lock:
                h = self._holds.get(i)
                live = (h is not None and h["token"] == token and
                        h["id"] == body.get("hold_id") and
                        now - h["beat"] <= BEAT_STALE_S)
                if live:
                    h["beat"] = now
                    return 200, {"ok": True, "hold_id": h["id"],
                                 "held_s": round(now - h["start"], 3),
                                 "needs_s": SCREEN_HOLD_S}
            return refuse(409, "The hold was interrupted. Lift your finger "
                               "and hold again.")
        with self._arm_lock:
            h = self._holds.get(i)
            if h is not None and h["token"] != token and \
                    now - h["beat"] <= BEAT_STALE_S:
                theirs = f"{h['who']} on the {h['device']}"
                h = "busy"
            elif h is not None and h["token"] == token and \
                    now - h["beat"] <= BEAT_STALE_S and \
                    body.get("hold_id") == h["id"]:
                h["beat"] = now                 # still holding
                return 200, {"ok": True, "hold_id": h["id"],
                             "held_s": round(now - h["start"], 3),
                             "needs_s": SCREEN_HOLD_S}
            else:
                self._hold_ids += 1
                h = {"token": token, "who": who, "device": device,
                     "group": gname, "start": now, "beat": now,
                     "id": self._hold_ids}
                self._holds[i] = h
        if h == "busy":
            why = (f"{theirs} is already holding {gname}. Only one hold at "
                   f"a time.")
            self._journal(who, device, "arm hold", "refused",
                          f"{who}'s hold to arm {gname} on the {device} was "
                          f"refused. {why}")
            return 409, {"error": why, "let_go": True}
        self._journal(who, device, "arm hold", "started",
                      f"{who} started holding to arm {gname} on the "
                      f"{device}. The Stream Deck arms it only if the hold "
                      f"lasts {SCREEN_HOLD_S:g} s and every rule allows it.")
        return 200, {"ok": True, "hold_id": h["id"], "held_s": 0.0,
                     "needs_s": SCREEN_HOLD_S}

    def arm_release(self, body, ctx):
        s = ctx.session
        if s is None:
            return 200, {"ok": True}
        try:
            i, gname = self._group_index(body)
        except ValueError:
            return 200, {"ok": True}
        h = self._drop_hold(i, s["token"])
        if h is not None:
            held = h["beat"] - h["start"]
            self._journal(s["who"], s["device"], "arm hold", "let go",
                          f"{s['who']} let go of {gname} on the "
                          f"{s['device']} after {held:.1f} s.")
        return 200, {"ok": True}

    def group_disarm(self, body, ctx):
        """Disarm one group: handed to the Stream Deck process, which owns
        the arm link, as an instant tap of that group's key. Never waits
        on a fresh page."""
        try:
            i, gname = self._group_index(body)
        except ValueError as e:
            return 400, {"error": str(e)}
        who, device = self._actor(ctx, body)
        if not self.arming_enabled():
            # The Stream Deck reads a screen's per-group disarm only while
            # screen arming is on (review of PR #43, P0-4): said, never
            # "sent" to nobody.
            why = ("Disarming one group from a screen goes through the "
                   "Stream Deck's screen link, which is off with screen "
                   "arming. Use Disarm every flame group, or the group's key "
                   "on the Stream Deck. Nothing was changed.")
            self._journal(who, device, "disarm", "refused",
                          f"{who or 'Someone'}'s Disarm {gname} on the "
                          f"{device} was refused. {why}")
            return 409, {"error": why}
        self._drop_hold(i)
        with self._arm_lock:
            self._disarm_ids += 1
            self._disarms.append({"id": self._disarm_ids, "group": i,
                                  "name": gname, "who": who,
                                  "device": device})
        self._journal(who, device, "disarm", "sent",
                      f"{who or 'Someone'} pressed Disarm {gname} on the "
                      f"{device}. Sent to the Stream Deck, which owns the "
                      f"arm link; flamesafe's lamp shows when it is off.")
        return 200, {"ok": True, "text": f"Disarm {gname} sent."}

    def deck_input(self):
        """What the Stream Deck process reads, 20 times a second: the
        fresh holds (with the held time the engine has heartbeats for) and
        the per-group disarms."""
        now = self.clock()
        enabled = self.arming_enabled()
        holds = []
        with self._arm_lock:
            for i, h in list(self._holds.items()):
                age = now - h["beat"]
                if age > 10 * BEAT_STALE_S:
                    del self._holds[i]          # long gone; tidy up
                    continue
                holds.append({"group": i, "id": h["id"],
                              "held_s": round(h["beat"] - h["start"], 3),
                              "fresh": age <= BEAT_STALE_S,
                              "who": h["who"], "device": h["device"]})
            disarms = list(self._disarms)
        return {"enabled": enabled, "holds": holds if enabled else [],
                "disarms": disarms}

    def _disarm_fn(self):
        if self._flame_disarm is not None:
            return self._flame_disarm
        cond = getattr(self.schedule, "conductor", None)
        show = getattr(cond, "show", None)
        fn = getattr(show, "flames_disarm_all", None)
        return fn

    def _disarm_now(self, who, screen, what):
        """(ok, sentence) of the flame link's disarm_all, called at once on
        this thread: no lock taken, nothing journaled, no disk touched.
        ok is None when no flame link is connected in this engine."""
        fn = self._disarm_fn()
        if fn is None:
            return None, ("No flame link is connected in this engine, so no "
                          "disarm could be sent from here. Disarm from the "
                          "Stream Deck.")
        label = who or "An unnamed operator"
        try:
            r = fn(f"{label} pressed {what} on the {screen}")
            ok = bool(getattr(r, "ok", False))
            said = str(getattr(r, "sentence", "") or "").strip()
        except Exception as e:
            ok, said = False, f"{type(e).__name__}: {e}"
        if ok:
            return True, (f"Every flame group: {said}" if said else
                          "Every flame group: a disarm was sent.")
        return False, (f"The disarm of every flame group did NOT go out "
                       f"({said}). Disarm from the Stream Deck.")

    def abort(self, who, screen, flames):
        """The screen's and the deck's Abort. `flames` is what the disarm
        sent before anything else did (_disarm_now). Then the scheduler's
        Abort, which stops a live show through the conductor. The answer
        says both, truthfully: a disarm sent with no show to stop is not
        "aborted", and a stopped show whose disarm failed is a fault."""
        f_ok, f_text = flames
        label = who or "An unnamed operator"
        self._journal(who, screen, "abort disarm",
                      "fault" if f_ok is False else
                      ("done" if f_ok else "not connected"),
                      f"{label} pressed Abort on the {screen}. {f_text}",
                      fault=f_ok is False)
        svc = self.schedule
        if svc is None:
            s_ok, s_text = False, ("There is no scheduler running in this "
                                   "engine, so no show was stopped.")
        else:
            try:
                r = svc.operator_press("abort", who, screen, confirmed=True)
                s_ok = bool(r.get("ok"))
                s_text = str(r.get("text") or "")
            except ValueError as e:
                s_ok, s_text = False, str(e)
            if not s_ok:
                s_text = f"No show was stopped: {s_text}"
        text = f"{f_text} {s_text}".strip()
        # A disarm that went out is the Abort's safety half done, whatever
        # the scheduler said; one that failed is a fault even if the show
        # stopped. With no flame link at all, the scheduler decides.
        ok = f_ok is True or (f_ok is None and s_ok)
        out = {"ok": ok, "text": text, "disarmed": f_ok, "stopped": s_ok}
        if not ok:
            out["error"] = text
        return (200 if ok else 409), out

    def disarm_all(self, who, screen):
        """Every flame group disarmed by the flame link's disarm_all: the
        same call the conductor's Abort makes. Never waits on a show, an
        operator or a fresh page."""
        fn = self._disarm_fn()
        label = who or "An unnamed operator"
        if fn is None:
            text = (f"{label} pressed Disarm every flame group on the "
                    f"{screen}, but no flame link is connected in this "
                    f"engine, so nothing could be sent. Disarm from the "
                    f"Stream Deck.")
            self._journal(who, screen, "disarm all", "fault", text,
                          fault=True)
            return 409, {"ok": False, "error": text}
        try:
            r = fn(f"{label} pressed Disarm every flame group on the "
                   f"{screen}")
            ok = bool(getattr(r, "ok", False))
            said = str(getattr(r, "sentence", "") or "")
        except Exception as e:
            ok, said = False, f"{type(e).__name__}: {e}"
        text = (f"{label} pressed Disarm every flame group on the {screen}. "
                f"{said}").strip()
        self._journal(who, screen, "disarm all", "done" if ok else "fault",
                      text, fault=not ok)
        return (200 if ok else 409), {"ok": ok, "text": text}

    # -- status ------------------------------------------------------------
    def status(self, ctx):
        out = {"served_at": int(self.wall() * 1000), "fresh_s": FRESH_S,
               "me": self.whoami(ctx)}
        svc = self.schedule
        if svc is None:
            out["schedule"] = {"attached": False}
        else:
            try:
                st = svc.state_view(journal=8)
                tn = svc.tonight_view()
                cv = st.get("conductor") or {}
                state = st.get("state")
                out["schedule"] = {
                    "attached": True, "ok": st.get("ok"),
                    "error": st.get("error"), "state": state,
                    "running": st.get("running"),
                    "held": state in ("HOLD", "PAUSED"),
                    "aborted": bool(cv.get("aborted")),
                    "conductor": cv.get("attached", False),
                    "trouble": cv.get("trouble"),
                    "next": st.get("next"), "delayed": st.get("delayed"),
                    "dry_run": st.get("dry_run"),
                    "slots": tn.get("slots", []),
                    "current_operator": svc.current_operator,
                    "operators": list(svc.operators)}
            except Exception as e:
                out["schedule"] = {"attached": True, "ok": False,
                                   "error": f"{type(e).__name__}: {e}"}
        try:
            cs = self.control.state()
            out["show"] = {"running": bool(cs.get("running")),
                           "show": cs.get("show"),
                           "timeline": cs.get("timeline"),
                           "timecode": cs.get("playing") or cs.get("ltc_in"),
                           "state": cs.get("state")}
        except Exception as e:
            out["show"] = {"running": False,
                           "error": f"{type(e).__name__}: {e}"}
        try:
            out["transport"] = self._transport_view()
        except Exception as e:
            out["transport"] = {"programming": False,
                                "why": f"{type(e).__name__}: {e}"}
        if self.flame_status is None:
            out["flames"] = {"connected": False, "stale": True, "groups": [],
                             "why": "flamesafe's status is not connected to "
                                    "this engine, so the flame lamps cannot "
                                    "be shown here. Look at the Stream "
                                    "Deck."}
        else:
            out["flames"] = self.flame_status.view()
        out["disarm_connected"] = self._disarm_fn() is not None
        now = self.clock()
        tok = ctx.session["token"] if ctx.session else None
        with self._arm_lock:
            holds = [{"group": i, "who": h["who"], "device": h["device"],
                      "held_s": round(h["beat"] - h["start"], 2),
                      "mine": h["token"] == tok}
                     for i, h in self._holds.items()
                     if now - h["beat"] <= BEAT_STALE_S]
        out["arming"] = {"enabled": self.arming_enabled(),
                         "signed_in": ctx.session is not None,
                         "needs_s": SCREEN_HOLD_S, "fresh_s": ARM_FRESH_S,
                         "holds": holds}
        return out
