"""The show-network remote: the few show controls an iPad (or the rack
screen) may press, over web.py's server. No sign-in (Jeff, 2026-10-07).

What this is, and what it is not
================================

The engine and the show live in the `ltc serve` process. The page is only a
window onto them: closing it, an iPad that sleeps or loses the Wi-Fi, a
connection that drops, none of those is an event this module acts on. There
is no timer here that does anything to the show when a page goes quiet, and
the selftest proves a dropped page changes nothing.

Who may press what
------------------
- No sign-in, no PIN, no operator names (Jeff, 2026-10-07). What keeps the
  page to the show is the network: the engine listens only on the one
  listed show-network address and on 127.0.0.1 (never 0.0.0.0), and
  refuses anything that looks proxied, cross-site or misaddressed.
- The journal names the surface a press came from, never a person: "Rack
  screen" for the machine itself, "iPad at <its address>" for a device on
  the show network (screen "iPad"). Taken from the request's own address,
  never from the request body.
- The Stream Deck keeps Jeff's 2026-10-01 rule and never comes through here.

The controls
------------
Start now, Hold, Resume and Abort are the scheduler's own operator events
(schedule_service.Service.operator_press: the same schedule.step every
press goes through, journaled with who and which screen, the Abort latch
saved before the conductor is asked). Reset is Service.reset_conductor.
"Disarm every flame group" is the show's own flames_disarm_all (the flame
link's disarm_all, the same call the conductor's Abort makes), journaled
with the surface and which screen.

ARMING (Jeff, 2026-10-03, its own PR): the page never arms anything. A
hold to arm (arm-hold, repeated every 100 ms while the
finger is down) is read by the Stream Deck process (deck-input) as a remote
press of that group's key. The deck owns flamesafe's arm link and runs its
own rules (the hold, the refractory window, the latched refusal) and
flamesafe runs all of its own (consent, dwell, the post-Abort window, the
round-4 veto, the second-copy guard) exactly as for a finger on the deck.
It needs a listed address (above), a page status and a
flamesafe status no older than ARM_FRESH_S, heartbeats the engine actually
received for SCREEN_HOLD_S, and screen_arming on in ltcplay_remote.json
(default on). A gap in the heartbeats, an Abort, a disarm or a sign out
lets the hold go; an interrupted hold never carries on.


Fresh state
-----------
Start now, Resume and Reset act on what the page
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
import hmac
import ipaddress
import json
import math
import os
import re
import socket
import threading
import time

from . import appdata
from . import settings as settings_mod
from . import version as version_mod

FIXED_PORT = 7878
DISK_FILE = "disk-low.txt"          # packaging/windows/housekeeping.DISK_FILE
DISK_READ_S = 30.0
SETTINGS_FILE = "ltcplay_remote.json"
# How old the page's status may be for a press that acts on what it shows.
FRESH_S = 2.0
# How old flamesafe's last status may be before the lamps read "stale"
# (CONTRACT.md: no status frame for 1 s is red for the safety program).
FLAME_STALE_S = 1.0

# The routes this module answers, and nothing else. No "arm" among them.
CONTROL_ROUTES = ("start-now", "hold", "resume", "abort", "reset",
                  "disarm-all")
# Programming-session transport (Jeff, 2026-10-03): play from a timecode,
# jump, pause and continue, an A/B loop, back to following timecode. Only
# in a programming session (a show started from the page as Rehearse, or in
# Rehearsal mode, or a rehearsal conductor), and never while a scheduled
# show is live; see _scrub_refusal.
TRANSPORT_ROUTES = ("play-from", "jump", "pause", "continue", "mark-a",
                    "mark-b", "loop", "follow")
FRESH_ROUTES = frozenset(("start-now", "resume", "reset")
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
# The Morning and Shutdown page (Jeff, 2026-10-17; morning.py, MORNING.md).
# None of these touches the arm link: the purge selection is published on
# deck-input for the Stream Deck process to act on, the fire heartbeat sets
# the flame link's purge frame, the lockout writes the file flamesafe reads.
MORNING_GET_ROUTES = ("morning", "checklist-pdf", "checklist-export")
MORNING_POST_ROUTES = ("morning-open", "purge-select", "purge-fire",
                       "purge-stop", "purge-clear", "laser-test", "lock",
                       "layout", "checklist-tick", "checklist-note",
                       "checklist-verify", "checklist-tally-retry")
GET_ROUTES = ("whoami", "status", "network", "deck-input") + \
    MORNING_GET_ROUTES
# "alert" (alerts.py, 2026-10-12): raise and clear only from this machine
# (the Stream Deck process, over loopback); Acknowledge from any page.
POST_ROUTES = CONTROL_ROUTES + TRANSPORT_ROUTES + ARM_ROUTES + (
    "network", "alert") + MORNING_POST_ROUTES
LOCAL_ONLY = frozenset(("network",))

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

# First run and setup (Jeff, 2026-10-17: a fresh install was "just a
# shell", with no screen to make a schedule or choose the show folder).
# This machine only, never the network: they read and write files beside
# the settings. None of them touches the rig: the show folder change asks
# the Windows supervisor to restart the engine, which it refuses during a
# show; the schedule is refused while a show is live.
SETUP_GET_ROUTES = ("setup", "schedule-file")
SETUP_POST_ROUTES = ("schedule-file", "open-folder", "show-folder", "screen")
PROGRAMS_FILE = "programs.json"      # packaging/windows/supervisor.py writes
FIRST_RUN_FILE = "first-run.json"    # what the supervisor wrote at first run
SHOWPC_FILE = "showpc.json"          # the supervisor's own settings
SCREEN_CLOSE_FILE = "close-rack-screen"   # the supervisor closes its Edge
RESTART_ENGINE_FILE = "restart-engine"    # the supervisor restarts ltcplay
PROGRAMS_READ_S = 3.0
FLAMESAFE_ENV = "LTCPLAY_FLAMESAFE_CONFIG"     # cli.py and ltcwin.py agree


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
    # purge_arming (Jeff, 2026-10-17, the Morning and Shutdown page) is ON
    # unless the file says false: a click on that page arms a machine for
    # a purge, from the Rack screen itself; from a device on the show
    # network it also needs screen_arming (MORNING.md).
    out = {"show_network_address": None, "flamesafe_config": None,
           "screen_arming": False, "purge_arming": True, "booth_alert": None,
           "alert_pixels": None, "path": path}
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
    if "purge_arming" in doc:
        v = doc["purge_arming"]
        if v is not True and v is not False:
            raise ValueError(f"{path}: purge_arming must be true or false.")
        out["purge_arming"] = v
    if doc.get("booth_alert") is not None:
        # Checked by boothaudio.parse_config, which owns the shape; a bad
        # block is refused here, at load, never at show time.
        from .boothaudio import parse_config
        out["booth_alert"] = parse_config(doc["booth_alert"],
                                          f"{path} booth_alert")
    if doc.get("alert_pixels") is not None:
        out["alert_pixels"] = check_alert_pixels(doc["alert_pixels"], path)
    return out


def check_alert_pixels(doc, path="ltcplay_remote.json"):
    """The floor pixels the blue alert pulse paints (player.py):
    {"start": 1, "count": 300, "order": "RGB"}, start the 1-based
    absolute channel of the first pixel. Returns (start0, count, order)
    with start0 counted from 0, or raises ValueError."""
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: alert_pixels has to be an object like "
                         f'{{"start": 1, "count": 300, "order": "RGB"}}.')
    start, count = doc.get("start"), doc.get("count")
    order = str(doc.get("order", "RGB") or "RGB").upper()
    for name, v in (("start", start), ("count", count)):
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            raise ValueError(f"{path}: alert_pixels {name} has to be a "
                             f"whole number from 1 up.")
    if sorted(order) != ["B", "G", "R"]:
        raise ValueError(f"{path}: alert_pixels order has to be the three "
                         f"letters R, G and B in the pixels' order, like "
                         f"RGB or GRB.")
    return (start - 1, count, order)


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


def _open_in_file_manager(path):
    """The folder in Explorer (Windows) or the Finder (Mac)."""
    import subprocess
    import sys
    if os.name == "nt":
        os.startfile(path)                       # noqa: S606
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open",
                          path], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True)


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
                           "dwell_s": g.get("dwell_s", 0),
                           "locked_fire": list(g.get("locked_fire") or [])})
        if not groups:
            groups = [{"name": n, "armed": "unknown", "wanted": False,
                       "reason": "", "dwell_s": 0} for n in self.names]
        if stale:
            # Never show a lamp from an old frame as if it were now.
            for g in groups:
                g["armed"] = "unknown"
        lk = last.get("lockout") if isinstance(last.get("lockout"), dict) \
            else None
        return {"connected": True, "stale": stale,
                "age_ms": None if age is None else int(age * 1000),
                "fault": str(last.get("fault", "")) if not stale else "",
                # flamesafe's own top-level `confirmed` (CONTRACT.md: false
                # until Andy confirms the config's numbers). The rack
                # screen's Info popup says it; the Stream Deck's keys no
                # longer carry a mark for it (2026-10-19). None while no
                # fresh frame says either way.
                "confirmed": (bool(last.get("confirmed", True))
                              if self.last is not None and not stale
                              else None),
                "groups": groups,
                # What flamesafe says it is holding at zero (the lockout
                # file as it read it), for the page to compare with its own.
                "lockout": ({"slots": [int(s) for s in lk.get("slots") or []
                                       if isinstance(s, int)],
                             "state": str(lk.get("state", ""))}
                            if lk is not None and not stale else None)}


# ------------------------------------------------------- the remote --

class Ctx:
    """Where a request came from: the machine itself, or a device on the
    show network. That is all a press is known by (no sign-in)."""

    def __init__(self, local, ip):
        self.local = local
        self.ip = ip

    @property
    def screen(self):
        """The screen-list name the scheduler checks."""
        return "Rack screen" if self.local else "iPad"

    @property
    def device(self):
        """What the journal calls it: the surface, never a person."""
        return "Rack screen" if self.local else f"iPad at {self.ip}"


def countdown_view(sched, show, cue_now):
    """What the page's countdown shows (Jeff, 2026-10-07). During a show,
    running or held, the time left in THAT show: its length minus where
    the engine's own show clock is in it (session.snapshot()'s "now", so a
    Hold, which freezes the clock, freezes this too). A show whose length
    is not known shows no countdown at all, never a guess. Between shows,
    the countdown to the next one, as before. None means show nothing.
    Display only: nothing here starts or stops anything."""
    sched = sched or {}
    if sched.get("attached"):
        live = sched.get("state") in ("SHOW", "PAUSED")
        held = sched.get("state") == "PAUSED"
    else:
        live = bool((show or {}).get("running")) and cue_now is not None
        held = False
    if live:
        now = cue_now if isinstance(cue_now, dict) else {}
        dur, el = now.get("duration"), now.get("elapsed")
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool)
                   and math.isfinite(x) for x in (dur, el)) or dur <= 0:
            return None
        return {"kind": "left", "label": "Left in this show",
                "s": round(max(0.0, dur - max(0.0, el)), 1),
                "running": not held}
    nx = sched.get("next") if sched.get("attached") else None
    if isinstance(nx, dict) and isinstance(nx.get("in_s"), (int, float)) \
            and not isinstance(nx.get("in_s"), bool):
        return {"kind": "next", "label": "Next show", "s": nx["in_s"],
                "running": True}
    return None


class Remote:
    """The routes under /api/remote/. `control` is web.Control, `schedule`
    the scheduler service (or None), `flame_status` a FlameStatus (or
    None), `flame_disarm` a callable(reason) returning something with .ok
    and .sentence (the show's flames_disarm_all), or None to take it from
    the scheduler's conductor when one is attached."""

    def __init__(self, control, schedule=None, folder=None,
                 flame_status=None, flame_disarm=None,
                 clock=time.monotonic, wall=time.time, log=print):
        self.control = control
        self.schedule = schedule
        self.folder = folder or (schedule.state_dir if schedule is not None
                                 else settings_folder())
        self.flame_status = flame_status
        self._flame_disarm = flame_disarm
        self.flame_cues = None       # fire_ice.FlameCues, set by web.serve
        self.morning = None          # morning.Morning, set by web.serve
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
        self._deck_seen = None       # clock() of the deck's last read
        self._version = None         # version_line(), once asked
        self.audio_watch = None      # audiowatch.AudioInterfaceWatch, F&I
        # The Windows supervisor's word on the disk (housekeeping.py writes
        # disk-low.txt in the settings folder while it is low or critical,
        # and removes it when it is fine again). Read at most every 30 s.
        self.disk_file = os.path.join(settings_folder(), DISK_FILE)
        self._disk = (None, 0.0)
        # The alert service (alerts.py, 2026-10-12): the rack screen's
        # banners, the booth note and the blue pulse. web.serve gives it
        # the booth player and the pulse's pixel range from the settings;
        # the scheduler raises its failed-start alert through alert_sink.
        from .alerts import Alerts
        self.alerts = Alerts(journal=self._alert_journal, clock=clock,
                             wall=wall, show_running=self._show_live,
                             on_change=self._apply_pulse)
        self.alert_pixels = None     # (start0, count, order) or None
        if schedule is not None and hasattr(schedule, "alert_sink"):
            schedule.alert_sink = self.alerts.raise_

    def _alert_journal(self, text, fault=False, action="alert",
                       outcome="", who="", screen="", **_kw):
        self._journal(who or "system", screen or "engine", action,
                      outcome or ("fault" if fault else "done"), text,
                      fault=fault)

    def _show_live(self):
        """True while the scheduler says a show is live (SHOW or PAUSED):
        the pulse's during-show rule (alerts.py)."""
        svc = self.schedule
        if svc is None:
            return False
        try:
            return svc.state_view().get("state") in ("SHOW", "PAUSED")
        except Exception:                # noqa: BLE001
            return False

    def _apply_pulse(self):
        """The blue floor pulse follows the open alerts: a flag the live
        player reads on its own tick (player.alert_pulse), set here on
        every alert change and again on every status read, so a Run
        pressed after the alert was raised still shows it."""
        s = getattr(self.control, "session", None)
        p = getattr(s, "player", None)
        if p is None:
            return
        try:
            p.alert_pixels = self.alert_pixels
            p.alert_pulse = self.alerts.pulse_wanted()
        except Exception:                # noqa: BLE001
            pass

    # -- who ---------------------------------------------------------------
    def context(self, local, ip):
        return Ctx(local, ip)

    @staticmethod
    def _actor(ctx, body=None):
        """(who, screen, surface) for a press. From the network: the
        surface it came from, by the request's own address, whatever the
        body says. On the machine itself the Stream Deck process names its
        chosen operator (or none) and "Stream Deck" (EngineConductor), and
        that is kept, checked against the lists as before; the page sends
        neither, and is the Rack screen."""
        body = body if isinstance(body, dict) else {}
        if ctx.local:
            who = str(body.get("who") or "").strip()
            screen = str(body.get("screen") or "").strip()
            if who or screen:
                return who, screen or "Rack screen", False
        return ctx.device, ctx.screen, True

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
    def get(self, route, ctx, query=""):
        name = route[len("/api/remote/"):]
        if name in SETUP_GET_ROUTES:
            if not ctx.local:
                return 403, {"error": "Only on the show machine itself."}
            return 200, (self.setup_view() if name == "setup"
                         else self.schedule_file_view())
        if name == "whoami":
            return 200, self.whoami(ctx)
        if name == "deck-input":
            # The Stream Deck process, on this machine. Never the network:
            # it carries the holds the deck acts on.
            if not ctx.local:
                return 403, {"error": "Only on the show machine itself."}
            return 200, self.deck_input()
        if name == "status":
            return 200, self.status(ctx)
        if name == "network":
            if not ctx.local:
                return 403, {"error": "Only on the show machine itself."}
            return 200, self.network_view()
        if name in MORNING_GET_ROUTES:
            return self.morning_get(name, query, ctx)
        return 404, {"error": "no such thing here"}

    def post(self, route, body, ctx):
        """(code, body, extra headers)."""
        name = route[len("/api/remote/"):]
        body = body if isinstance(body, dict) else {}
        if name in SETUP_POST_ROUTES:
            if not ctx.local:
                return 403, {"error": "The show folder, the schedule file "
                                      "and this window are set on the show "
                                      "machine itself, not from here."}, {}
            return (*self.setup_post(name, body),) + ({},)
        if name in LOCAL_ONLY and not ctx.local:
            return 403, {"error": "The show network is set on the show "
                                  "machine itself, not from here."}, {}
        if name == "network":
            return (*self.set_network(body),) + ({},)
        if name in MORNING_POST_ROUTES:
            return (*self.morning_post(name, body, ctx),) + ({},)
        if name in CONTROL_ROUTES or name in TRANSPORT_ROUTES:
            return (*self.press(name, body, ctx),) + ({},)
        if name == "arm-hold":
            return (*self.arm_hold(body, ctx),) + ({},)
        if name == "arm-release":
            return (*self.arm_release(body, ctx),) + ({},)
        if name == "group-disarm":
            return (*self.group_disarm(body, ctx),) + ({},)
        if name == "alert":
            return (*self.alert(body, ctx),) + ({},)
        return 404, {"error": "no such thing here"}, {}

    # -- alerts (alerts.py) ------------------------------------------------
    def alert(self, body, ctx):
        """POST /api/remote/alert. From this machine only: {"raise": kind,
        "text": sentence, "options": [...]} and {"clear": kind}, the
        Stream Deck process's and the scheduler's. From any page: {"id":
        n, "action": "acknowledge"}; an Acknowledge with no id clears
        every open alert (a deck key). Acknowledge changes nothing on the
        rig; the options that choose are refused with a sentence."""
        who, screen, _surface = self._actor(ctx, body)
        if "raise" in body or "clear" in body:
            if not ctx.local:
                return 403, {"error": "Alerts are raised on the show "
                                      "machine itself, not from here."}
            if "raise" in body:
                try:
                    a = self.alerts.raise_(body.get("raise"),
                                           body.get("text"),
                                           body.get("options") or
                                           ("Acknowledge",))
                except ValueError as e:
                    return 400, {"error": str(e)}
                return 200, {"text": f"Alert {a.kind} is up: {a.text}",
                             "alert": a.view()}
            ok, text = self.alerts.clear(kind=str(body.get("clear") or ""),
                                         action="Confirm", who=who,
                                         screen=screen, address=ctx.ip)
            return (200 if ok else 409), ({"text": text} if ok
                                          else {"error": text})
        action = str(body.get("action") or "").strip()
        aid = body.get("id")
        if aid is not None and (not isinstance(aid, int) or
                                isinstance(aid, bool)):
            return 400, {"error": "id has to be the alert's number."}
        ok, text = self.alerts.clear(id=aid, action=action or "Acknowledge",
                                     who=who, screen=screen, address=ctx.ip)
        if not ok:
            # A cleared alert journals itself (alerts.py). A press that
            # clears nothing (the alert already gone, a wrong id, an
            # option that chooses) used to leave no line at all, so an ACK
            # pressed on the deck could never be found in the journal
            # afterwards (the Pico, 2026-10-18). Who, when and what, every
            # time.
            label = ("Acknowledge" if (action or "").lower() == "acknowledge"
                     else action or "Acknowledge")
            self._journal(who, screen, "alert", "refused",
                          f"{label} pressed by "
                          f"{who or 'someone'} on the {screen or 'unknown screen'}"
                          f"{' for alert ' + str(aid) if aid is not None else ''}"
                          f" cleared nothing: {text}")
            return 409, {"error": text}
        return 200, {"text": text}

    def whoami(self, ctx):
        return {"local": ctx.local, "device": ctx.device,
                "screen": ctx.screen, "fresh_s": FRESH_S}

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
        # The press's place in line, taken before anything else (an Abort's
        # disarm and laser blank, a lock): the scheduler compares a Reset's
        # with the latest Abort's, so work done first never reorders them.
        stamp = getattr(self.schedule, "stamp_press", None)
        pressed = stamp() if stamp is not None else None
        who, screen, surface = self._actor(ctx, body)
        if name in FRESH_ROUTES:
            why = self._stale(body)
            if why:
                self._journal(who, screen, name, "refused",
                              f"{who or 'Someone'}'s {name} on the {screen} "
                              f"was refused. {why}")
                return 409, {"error": why, "stale": True}
        flames = None
        if name == "abort":
            # The flames first, every time (review of PR #43, P0-1): every
            # flame group disarmed through the flame link before any
            # scheduler step, any lock or any journal line, whatever the
            # scheduler is doing and whether or not a show is live. The
            # scheduler takes an Abort only in SHOW or PAUSED, and the
            # conductor only while something plays; a group armed before a
            # show, between shows or after one must still come off. Nothing
            # here waits on a disk (P0-5). Before the confirm check too
            # (safety audit of bench-build, P2 b): a disarm only removes
            # risk, so an unconfirmed Abort still takes the flames off; the
            # rest of it is refused below.
            flames = self._disarm_now(who, screen, "Abort")
        if name in CONFIRM_ROUTES and body.get("confirmed") is not True:
            if flames is not None and flames[0] is not None:
                why = ("Confirm it on the page first. Every flame group was "
                       "sent a disarm; nothing else was changed.")
            else:
                why = "Confirm it on the page first. Nothing was changed."
            self._journal(who, screen, name, "refused",
                          f"{who or 'Someone'}'s {name} on the {screen} was "
                          f"refused. {why}")
            return 400, {"error": why}
        if name == "abort":
            # And the lasers, the same way (re-review P1-A): blanked here,
            # before the scheduler's step and its save, not after them.
            lasers = self._lasers_dark_now()
        if name in ("abort", "disarm-all"):
            # No screen hold survives an Abort or a disarm, whoever pressed
            # it and whether or not it goes on to be accepted.
            self.cancel_holds(f"{who or 'someone'} pressed {name} on the "
                              f"{screen}")
            # Nor a purge or a laser test (the Morning and Shutdown page):
            # Abort wins over everything, before the scheduler is asked.
            m = self.morning
            if m is not None:
                try:
                    m.on_abort(who, screen)
                except Exception:
                    pass
        if name == "disarm-all":
            return self.disarm_all(who, screen)
        if name == "abort":
            return self.abort(who, screen, flames, lasers, pressed)
        if name in TRANSPORT_ROUTES:
            return self.transport(name, body, who, screen)
        svc = self.schedule
        if svc is None:
            return 409, {"error": "There is no scheduler running in this "
                                  "engine, so there is no show to press "
                                  "this on. Nothing was changed."}
        try:
            # surface: the press names the surface, not a person on the
            # operator list (Jeff, 2026-10-07). Every other rule the
            # scheduler has for these presses still applies.
            if name == "reset":
                r = svc.reset_conductor(who, screen, pressed=pressed,
                                        surface=surface)
            else:
                r = svc.operator_press(name, who, screen,
                                       confirmed=body.get("confirmed") is True,
                                       surface=surface)
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
        # The hold belongs to the surface it came from: one device, one
        # hold, and another device cannot carry it on.
        who, device = ctx.device, ctx.screen
        token = ctx.device
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
            # A live hold is nobody else's to take over or restart: not
            # another device's, and with no sign-in not another tab's on
            # the same device either (Jeff, 2026-10-07). Only one hold at a
            # time, and a new one starts only once it has gone.
            if h is not None and now - h["beat"] <= BEAT_STALE_S:
                theirs = h["who"]
                h = "busy"
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
        try:
            i, gname = self._group_index(body)
        except ValueError:
            return 200, {"ok": True}
        h = self._drop_hold(i, ctx.device)
        if h is not None:
            held = h["beat"] - h["start"]
            self._journal(h["who"], h["device"], "arm hold", "let go",
                          f"{h['who']} let go of {gname} on the "
                          f"{h['device']} after {held:.1f} s.")
        return 200, {"ok": True}

    def group_disarm(self, body, ctx):
        """Disarm one group: handed to the Stream Deck process, which owns
        the arm link, as an instant tap of that group's key. Never waits
        on a fresh page."""
        try:
            i, gname = self._group_index(body)
        except ValueError as e:
            return 400, {"error": str(e)}
        who, device, _surface = self._actor(ctx, body)
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
        self._deck_seen = now
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
        self._apply_pulse()
        # The open alerts too: the deck relights a key as ACK while one is
        # open, and any key press acknowledges (alerts.py).
        out = {"enabled": enabled, "holds": holds if enabled else [],
               "disarms": disarms, "alerts": self.alerts.view()}
        # The Morning and Shutdown page (morning.py): whether it is open
        # (the deck re-maps itself while it is), the groups the purge
        # selection needs armed, and the locks. The deck arms a purge
        # group only while the engine says purge arming is on.
        m = self.morning
        if m is not None:
            try:
                mv = m.deck_view()
            except Exception as e:                   # noqa: BLE001
                mv = {"open": False, "purge": {"wanted_groups": [],
                                               "selected": 0, "firing": False},
                      "locks": {}, "error": f"{type(e).__name__}: {e}"}
            mv["purge_enabled"] = self.purge_enabled(local=True)
            out["morning"] = mv
        return out

    # -- the Morning and Shutdown page (morning.py) --------------------------
    def purge_enabled(self, local):
        """May a purge be armed from here? purge_arming (default on) for
        the machine itself; a device on the show network also needs
        screen_arming, the switch every other arming from the network has
        (MORNING.md, T9). A broken settings file: off."""
        try:
            rs = load_settings(self.folder)
        except (ValueError, OSError):
            return False
        if rs["purge_arming"] is not True:
            return False
        return bool(local) or rs["screen_arming"] is True

    def may_act_from(self, local):
        """May a device UNLOCK a machine or turn a laser test ON from here?
        The show machine itself always; a device on the show network only
        with screen_arming (review of PR #68, P2-4 and P2-5: the two things
        on this page that raise risk from the network are gated like
        arming). Locking stays open to every device: it only lowers risk."""
        if local:
            return True
        try:
            rs = load_settings(self.folder)
        except (ValueError, OSError):
            return False
        return rs["screen_arming"] is True

    def _morning_journal(self, text, action="morning", outcome="done",
                         who="", screen="", fault=False, **_kw):
        self._journal(who or "system", screen or "engine", action, outcome,
                      text, fault=fault)

    def show_live_refusal(self):
        """Why nothing may fire from the Morning page right now: a show
        live (SHOW or PAUSED), an Abort standing, or the show clock playing
        a cue. "" when a purge may go (between shows, nothing playing)."""
        svc = self.schedule
        m = getattr(svc, "machine", None)
        state = getattr(m, "state", None)
        if state in ("SHOW", "PAUSED"):
            return (f"a scheduled show is live ({state}); nothing fires from "
                    f"this page until it has ended")
        cond = getattr(svc, "conductor", None)
        if getattr(m, "abort_latched", False) or \
                getattr(cond, "latched", False):
            return "the show is aborted; press Reset first"
        s = getattr(self.control, "session", None)
        clk = getattr(s, "clock", None) if s is not None else None
        if clk is not None and getattr(clk, "playing", False):
            return "the show clock is playing a cue; nothing fires from this page meanwhile"
        return ""

    def morning_get(self, name, query, ctx):
        m = self.morning
        if m is None:
            return 404, {"error": "The Morning and Shutdown page is not "
                                  "set up in this engine (it needs the Fire "
                                  "& Ice scheduler)."}
        if name == "morning":
            v = m.full_view(ctx.device)
            v["purge_enabled"] = self.purge_enabled(ctx.local)
            v["served_at"] = int(self.wall() * 1000)
            return 200, v
        import urllib.parse
        q = urllib.parse.parse_qs(query or "")
        if name == "checklist-pdf":
            date = (q.get("date") or [m.checklist.show_date()])[0]
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
                return 400, {"error": "date has to be YYYY-MM-DD."}
            data = m.checklist.pdf_for_date(date)
            return (200, data, "application/pdf",
                    {"Content-Disposition":
                     f'attachment; filename="checklist_{date}.pdf"'})
        if name == "checklist-export":
            frm = (q.get("from") or [None])[0]
            to = (q.get("to") or [None])[0]
            for d in (frm, to):
                if d is not None and not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                    return 400, {"error": "from and to have to be "
                                          "YYYY-MM-DD."}
            fname, data = m.checklist.export_zip(frm, to)
            return (200, data, "application/zip",
                    {"Content-Disposition": f'attachment; filename="{fname}"'})
        return 404, {"error": "no such thing here"}

    def morning_post(self, name, body, ctx):
        m = self.morning
        if m is None:
            return 404, {"error": "The Morning and Shutdown page is not "
                                  "set up in this engine."}
        who, screen, _surface = self._actor(ctx, body)
        device = ctx.device
        try:
            seen = float(body.get("seen"))
            seen_ok = abs(self.wall() - seen / 1000.0) <= 1.0
        except (TypeError, ValueError):
            seen_ok = False
        if name == "morning-open" and not self.purge_enabled(ctx.local):
            # A device that may not purge from here reads the page but does
            # not hold it "open": the deck re-maps itself and the purge
            # selection lives only for devices that may arm one (review of
            # PR #68, P2-8).
            return 200, {"ok": True, "open": False, "view_only": True}
        if name in ("purge-select", "purge-fire") and \
                body.get("on") is not False:
            if not self.purge_enabled(ctx.local):
                return 403, {"error": "Arming a purge from here is switched "
                                      "off (purge_arming, and from the show "
                                      "network screen_arming, in "
                                      "ltcplay_remote.json).",
                             "let_go": True}
        if ((name == "lock" and body.get("on") is False) or
                (name == "laser-test" and body.get("on") is True)) and \
                not self.may_act_from(ctx.local):
            return 403, {"error": ("Unlocking" if name == "lock" else
                                   "Turning a laser test on") +
                         " from a device on the show network needs "
                         "screen_arming true in ltcplay_remote.json. Do it on "
                         "the show machine itself."}
        if name == "purge-select" and body.get("on") is not False:
            try:
                deck_alive = self.clock() - (self._deck_seen or -1e9) <= \
                    self.DECK_QUIET_S
            except Exception:
                deck_alive = False
            if not deck_alive:
                return 409, {"error": "The Stream Deck program has not read "
                                      "this engine in the last 2 s, and it "
                                      "is what arms a group for the purge. "
                                      "Nothing was armed."}
        return m.post(name, body, device, who, screen, seen_ok, ctx.local)

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

    def _lasers_dark_now(self):
        """(ok or None, sentence): the conductor's laser blank, on this
        thread, at once. None when no conductor is attached."""
        cond = getattr(self.schedule, "conductor", None)
        fn = getattr(cond, "lasers_dark_now", None)
        if fn is None:
            return None, ""
        try:
            r = fn("Abort pressed on a screen")
            ok = bool(getattr(r, "ok", False))
        except Exception as e:
            return False, (f"The laser blank did NOT go out at the press "
                           f"({type(e).__name__}: {e}).")
        return ok, ("Lasers blanked at the press." if ok else
                    "The laser blank did NOT go out at the press.")

    def abort(self, who, screen, flames, lasers=(None, ""), pressed=None):
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
                r = svc.operator_press("abort", who, screen, confirmed=True,
                                       pressed=pressed)
                s_ok = bool(r.get("ok"))
                s_text = str(r.get("text") or "")
            except ValueError as e:
                s_ok, s_text = False, str(e)
            if not s_ok:
                s_text = f"No show was stopped: {s_text}"
        text = f"{f_text} {lasers[1]} {s_text}".strip().replace("  ", " ")
        # A disarm that went out is the Abort's safety half done, whatever
        # the scheduler said; one that failed is a fault even if the show
        # stopped. With no flame link at all, the scheduler decides.
        ok = f_ok is True or (f_ok is None and s_ok)
        out = {"ok": ok, "text": text, "disarmed": f_ok, "stopped": s_ok,
               "lasers_blanked": lasers[0]}
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
    DECK_QUIET_S = 2.0

    def laser_fault(self):
        """beyondtc.NOT_SET_UP while the conductor's laser blanking has no
        OSC target, else "". Display only; never raises."""
        try:
            cond = getattr(self.schedule, "conductor", None)
            b = getattr(getattr(cond, "devices", None), "beyond", None)
            return str(getattr(b, "fault", "") or "")
        except Exception:
            return ""

    def disk_view(self):
        """{"state": "low" | "critical", "text"} while the supervisor says
        the disk is low (its disk-low.txt), else None. Never raises."""
        now = self.clock()
        cached, at = self._disk
        if at and now - at < DISK_READ_S:
            return cached
        view = None
        try:
            with open(self.disk_file, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            state = (lines[0].strip().lower() if lines else "")
            text = " ".join(l.strip() for l in lines[1:] if l.strip())
            if state in ("low", "critical") and text:
                view = {"state": state, "text": text}
        except OSError:
            view = None
        except Exception:                        # noqa: BLE001
            view = None
        self._disk = (view, now)
        return view

    # -- first run and setup (this machine only) -----------------------------
    def _settings_json(self, name, cache_key, every_s=PROGRAMS_READ_S):
        """A JSON object file in the settings folder, read at most every
        `every_s` seconds; {} when it is not there or not an object. Never
        raises."""
        now = self.clock()
        cache = getattr(self, "_json_cache", None)
        if cache is None:
            cache = self._json_cache = {}
        doc, at = cache.get(cache_key, ({}, 0.0))
        if at and now - at < every_s:
            return doc
        try:
            with open(os.path.join(settings_folder(), name),
                      encoding="utf-8-sig") as fh:
                doc = json.load(fh)
            if not isinstance(doc, dict):
                doc = {}
        except Exception:                        # noqa: BLE001
            doc = {}
        cache[cache_key] = (doc, now)
        return doc

    def programs_view(self):
        """The Windows supervisor's word on its three programs
        (programs.json: {"flamesafe": {"running": bool, "why": ""}, ...},
        written on every change), or {} off the show PC. Never raises."""
        return self._settings_json(PROGRAMS_FILE, "programs")

    def deck_view(self):
        """The Stream Deck program, as this engine sees it, in a sentence
        a person can act on: whether it has been heard (its deck-input
        reads), and if not, why not, from the supervisor's word when there
        is one (Jeff, 2026-10-17: "the Stream Deck never connected")."""
        seen = self._deck_seen
        if seen is not None:
            age = self.clock() - seen
            if age <= self.DECK_QUIET_S:
                return {"heard": True, "age_s": round(age, 1), "text": ""}
            return {"heard": False, "age_s": round(age, 1),
                    "text": (f"The Stream Deck program was last heard "
                             f"{age:.0f} s ago; LTC Player starts it again "
                             f"within seconds if it stopped. Its log is "
                             f"deck.log in the settings folder.")}
        pr = (self.programs_view().get("deck") or {})
        why = str(pr.get("why") or "").strip()
        if why:
            text = (f"The Stream Deck program is not running: {why} So "
                    f"nothing has looked for the Stream Deck hardware yet.")
        elif pr.get("running"):
            text = ("The Stream Deck program is running but has not reached "
                    "this engine yet. If this stays, its log (deck.log in "
                    "the settings folder) says what it found.")
        else:
            text = ("The Stream Deck program has never reached this engine. "
                    "On the show PC, LTC Player starts it only when "
                    "flamesafe's config file exists (showpc.json, "
                    "flamesafe_config) and show_mode is fire_ice; its "
                    "supervisor.log says which. The Stream Deck hardware "
                    "itself is looked for by that program, not by this one.")
        return {"heard": False, "age_s": None, "text": text}

    def _show_folder(self):
        return str(getattr(self.control, "folder", "") or "")

    def _show_files(self):
        """The show files the Programming screen's Run picker would offer."""
        try:
            tls = self.control.timelines()
        except Exception:                        # noqa: BLE001
            return 0
        return len([t for t in tls if not t.get("error")])

    def _schedule_path(self):
        """The scheduler's own file, or where one would go (the same name
        schedule_service.RULE_FILE has, spelled here so a GPL engine, which
        never loads the scheduler, does not load it for this)."""
        svc = self.schedule
        p = getattr(svc, "path", None)
        if p:
            return str(p)
        return os.path.join(settings_folder(), "ltcplay_schedule.json")

    def _show_live(self):
        return self._show_live_state() in self.LIVE_STATES

    def _show_live_state(self):
        try:
            return self.schedule.state_view().get("state")
        except Exception:                        # noqa: BLE001
            return None

    def setup_view(self):
        """What a fresh install has and lacks, for the getting-started card
        and the Programming screen: the show folder and its show files,
        the schedule file and whether the scheduler loaded it, flamesafe
        and the Stream Deck as this engine sees them, and which files the
        supervisor wrote as examples at first run. Display only."""
        folder = self._show_folder()
        files = self._show_files()
        svc = self.schedule
        sched_path = self._schedule_path()
        sched_ok = svc is not None and getattr(svc, "rule", None) is not None
        first = self._settings_json(FIRST_RUN_FILE, "first_run", 30.0)
        written = first.get("written") or {}
        try:
            fl = self.flame_status.view() if self.flame_status else None
        except Exception:                        # noqa: BLE001
            fl = None
        fs_connected = bool(fl and fl.get("connected") and not fl.get("stale"))
        return {"show_folder": folder,
                "show_folder_exists": bool(folder) and os.path.isdir(folder),
                "show_files": files,
                "schedule_path": sched_path,
                "schedule_exists": os.path.isfile(sched_path),
                "schedule_ok": bool(sched_ok),
                "schedule_error": str(getattr(svc, "error", "") or ""),
                "schedule_attached": svc is not None,
                "example_schedule": bool(written.get("schedule")),
                "example_flamesafe": bool(written.get("flamesafe")),
                "flamesafe_config": os.environ.get(FLAMESAFE_ENV, ""),
                "flamesafe_connected": fs_connected,
                "deck_heard": self.deck_view()["heard"],
                "settings_folder": settings_folder(),
                "kiosk_closable": bool(appdata.WINDOWS),
                "ready": files > 0 and bool(sched_ok)}

    def schedule_file_view(self):
        """GET schedule-file: the file as it is (or the example's times to
        start from), its path, and the scheduler's word on it."""
        path = self._schedule_path()
        exists = os.path.isfile(path)
        doc, err = None, ""
        if exists:
            try:
                with open(path, encoding="utf-8-sig") as fh:
                    doc = json.load(fh)
                if not isinstance(doc, dict):
                    doc, err = None, "the file is not a schedule object"
            except (OSError, ValueError) as e:
                doc, err = None, str(e)
        if doc is None:
            doc = self.example_schedule()
        svc_err = str(getattr(self.schedule, "error", "") or "")
        return {"path": path, "exists": exists, "doc": doc,
                "error": err or svc_err}

    @staticmethod
    def example_schedule():
        """ltcplay_schedule.example.json's times, the starting point for a
        new file; a built-in copy if the example is not shipped."""
        here = os.path.dirname(os.path.abspath(__file__))
        try:
            with open(os.path.join(here, "ltcplay_schedule.example.json"),
                      encoding="utf-8-sig") as fh:
                doc = json.load(fh)
            if isinstance(doc, dict):
                return doc
        except (OSError, ValueError):
            pass
        return {"timezone": "America/Denver",
                "season": {"first_date": "2026-11-14",
                           "last_date": "2027-01-02"},
                "weekly": {"fri": {"first_start": "17:30",
                                   "interval_min": 20, "last_end": "22:00"},
                           "sat": {"first_start": "17:00",
                                   "interval_min": 20, "last_end": "22:00"}},
                "exceptions": {}, "show_len_s": 440, "guard_s": 120,
                "late_grace_s": 15}

    def save_schedule_file(self, body):
        """POST schedule-file {"doc": {...}}: checked by the scheduler's
        own parser first (every refusal is its sentence), written whole,
        then loaded by the running scheduler. Refused while a show is live:
        the night's list is not rebuilt under a running show."""
        doc = body.get("doc")
        if not isinstance(doc, dict):
            return 400, {"error": "Send the schedule as an object."}
        if self.schedule is None:
            # A GPL engine never loads the scheduler, not even to check a
            # file for it.
            return 409, {"error": "There is no scheduler in this engine, so "
                                  "there is no schedule to edit here."}
        if self._show_live():
            return 409, {"error": "A show is live. Save the schedule between "
                                  "shows. Nothing was changed."}
        try:
            from . import schedule as sch
            sch.parse_rule(doc)
        except Exception as e:                   # RuleError and kin
            problems = getattr(e, "problems", None)
            why = "; ".join(str(p) for p in problems) if problems else str(e)
            return 400, {"error": f"Not saved: {why}"}
        path = self._schedule_path()
        try:
            _write_json(path, doc)
        except OSError as e:
            return 500, {"error": f"Could not write {path}: {e}"}
        svc = self.schedule
        loaded = ""
        if svc is not None and hasattr(svc, "reload"):
            try:
                svc.reload()
                err = str(getattr(svc, "error", "") or "")
                loaded = (f" The scheduler could not load it: {err}" if err
                          else " The scheduler has loaded it.")
            except Exception as e:               # noqa: BLE001
                loaded = f" The scheduler could not reload it: {e}"
        self._journal("", "Rack screen", "schedule file", "saved",
                      f"The schedule file {path} was saved from the "
                      f"Programming screen.{loaded}")
        return 200, {"ok": True, "text": f"Saved to {path}.{loaded}",
                     "doc": doc, "path": path}

    def open_folder(self, body):
        """POST open-folder {"which": "show" | "settings"}: the folder
        opened in the file manager of this machine."""
        which = str(body.get("which") or "")
        if which == "show":
            path = self._show_folder()
        elif which == "settings":
            path = settings_folder()
        else:
            return 400, {"error": "which has to be show or settings."}
        if not path or not os.path.isdir(path):
            return 404, {"error": f"{path or 'that folder'} does not exist "
                                  f"on this machine yet."}
        opener = getattr(self, "_opener", None) or _open_in_file_manager
        try:
            opener(path)
        except Exception as e:                   # noqa: BLE001
            return 500, {"error": f"Could not open {path}: {e}"}
        return 200, {"ok": True, "text": f"Opened {path}."}

    def set_show_folder(self, body):
        """POST show-folder {"path": ...}: the show PC's showpc.json
        show_folder, then a restart request the supervisor acts on (and
        refuses during a show). Off the show PC the folder is the engine's
        --folder, so this says how instead of pretending."""
        path = str(body.get("path") or "").strip().strip('"')
        if not path or not os.path.isabs(path):
            return 400, {"error": "Type the folder's full path, like "
                                  "C:\\Users\\show\\Documents\\LTC Shows."}
        if not os.path.isdir(path):
            return 400, {"error": f"{path} is not a folder on this machine. "
                                  f"Make it first, then try again."}
        if self._show_live():
            return 409, {"error": "A show is live. Change the show folder "
                                  "between shows. Nothing was changed."}
        showpc = os.path.join(settings_folder(), SHOWPC_FILE)
        if not os.path.isfile(showpc):
            return 409, {"error": f"On this machine the show folder is given "
                                  f"when ltcplay is started (its --folder), "
                                  f"not here. The engine is using "
                                  f"{self._show_folder()}."}
        try:
            with open(showpc, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
            if not isinstance(doc, dict):
                raise ValueError("not a settings object")
            doc["show_folder"] = path
            _write_json(showpc, doc)
            with open(os.path.join(settings_folder(), RESTART_ENGINE_FILE),
                      "w", encoding="utf-8") as fh:
                fh.write(f"show folder {time.time():.3f}\n")
        except (OSError, ValueError) as e:
            return 500, {"error": f"Could not save the show folder: {e}"}
        self._journal("", "Rack screen", "show folder", "saved",
                      f"The show folder was set to {path} from the "
                      f"Programming screen; LTC Player restarts the engine "
                      f"to use it.")
        return 200, {"ok": True, "text": f"Saved. LTC Player restarts the "
                     f"show engine now to use {path} (it refuses while a "
                     f"show is running). This page reconnects by itself."}

    def screen(self, body):
        """POST screen {"action": "close"}: a request the Windows
        supervisor acts on, closing the full screen window it opened. The
        engine, the show and the rig are not touched."""
        if str(body.get("action") or "") != "close":
            return 400, {"error": "action has to be close."}
        try:
            with open(os.path.join(settings_folder(), SCREEN_CLOSE_FILE),
                      "w", encoding="utf-8") as fh:
                fh.write(f"close {time.time():.3f}\n")
        except OSError as e:
            return 500, {"error": f"Could not ask: {e}. Press Alt+F4."}
        return 200, {"ok": True, "text": "Asked LTC Player to close this "
                     "window. The show engine keeps running. If the window "
                     "stays, press Alt+F4."}

    def setup_post(self, name, body):
        if name == "schedule-file":
            return self.save_schedule_file(body)
        if name == "open-folder":
            return self.open_folder(body)
        if name == "show-folder":
            return self.set_show_folder(body)
        return self.screen(body)

    def audio_interface_view(self):
        """audiowatch's view, or None without the watch. Never raises."""
        w = getattr(self, "audio_watch", None)
        if w is None:
            return None
        try:
            return w.view()
        except Exception:                        # noqa: BLE001
            return None

    def subsystems(self, cs):
        """The rack screen's compact strip: one word per subsystem, and
        "ok", "warn", "bad" or "off". Display only, read from what the
        engine already knows; never raises."""
        out = []

        def add(name, state, word):
            out.append({"name": name, "state": state, "word": word})
        seen = self._deck_seen
        if seen is None:
            add("Deck", "off", "not heard")
        else:
            age = self.clock() - seen
            add("Deck", "ok" if age <= self.DECK_QUIET_S else "bad",
                "ok" if age <= self.DECK_QUIET_S else f"quiet {age:.0f} s")
        try:
            fl = self.flame_status.view() if self.flame_status else None
        except Exception:
            fl = None
        if not fl or not fl.get("connected"):
            add("Flamesafe", "off", "not connected")
        elif fl.get("stale"):
            add("Flamesafe", "bad", "no status")
        elif fl.get("fault"):
            add("Flamesafe", "bad", "fault")
        else:
            add("Flamesafe", "ok", "linked")
        audio = ((cs or {}).get("clock") or {}).get("audio")
        watch = self.audio_interface_view()
        if watch and watch.get("missing"):
            # Before Run: the interface the show file names is not on this
            # PC at all (audiowatch.py). The banner says which.
            add("Audio", "bad", "not attached")
        elif not isinstance(audio, dict):
            add("Audio", "off", "none")
        elif audio.get("fault") or not audio.get("connected"):
            add("Audio", "bad", "fault" if audio.get("fault") else "lost")
        else:
            add("Audio", "ok", "ok")
        disk = self.disk_view()
        if disk:
            add("Disk", "bad" if disk["state"] == "critical" else "warn",
                disk["state"])
        applied = {}
        cond = getattr(self.schedule, "conductor", None)
        try:
            applied = (cond.snapshot() or {}).get("applied") or {}
        except Exception:
            applied = {}
        laser_fault = self.laser_fault()
        for name, key in (("Video", "video"), ("Lasers", "lasers")):
            v = applied.get(key)
            if key == "lasers" and laser_fault:
                # Never "ok" or "lit" while nothing can blank them.
                add(name, "bad", "blanking not set up")
            elif cond is None or v is None:
                add(name, "off", "none")
            elif str(v) == "unknown":
                add(name, "warn", "not known")
            else:
                add(name, "ok", str(v).lower())
        if not (cs or {}).get("running"):
            add("Pixels", "off", "stopped")
        elif (cs.get("send_errors") or 0) and \
                (cs.get("since_ok") or 0) > 2.0:
            add("Pixels", "bad", "not sending")
        else:
            add("Pixels", "ok", "sending")
        return out

    def version_line(self):
        """Which LTC Player this is, for the rack screen's header: the
        release it was stamped with and its build id, or that it has been
        edited since (version.status()). Worked out once: the build id
        hashes every file the program ships, which is cheap once and not on
        every poll. Never raises."""
        if self._version is None:
            try:
                self._version = version_mod.status()
            except Exception as e:                  # noqa: BLE001
                self._version = f"version unknown ({type(e).__name__})"
        return self._version

    def status(self, ctx):
        out = {"served_at": int(self.wall() * 1000), "fresh_s": FRESH_S,
               "me": self.whoami(ctx), "version": self.version_line()}
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
                    "slots": tn.get("slots", [])}
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
            cue_now = cs.get("now")
            out["subsystems"] = self.subsystems(cs)
        except Exception as e:
            out["show"] = {"running": False,
                           "error": f"{type(e).__name__}: {e}"}
            cue_now = None
            out["subsystems"] = self.subsystems({})
        out["countdown"] = countdown_view(out["schedule"], out["show"],
                                          cue_now)
        out["audio_interface"] = self.audio_interface_view()
        out["disk"] = self.disk_view()
        out["deck"] = self.deck_view()
        out["programs"] = self.programs_view()
        if ctx.local:
            # The getting-started card: the rack screen's, not the iPad's.
            out["setup"] = self.setup_view()
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
        # Review of PR #43, P1-3: the flame cues refused for a reason that
        # is a fault (the render and the layout do not fit, and the like).
        out["flames"]["cues_fault"] = str(
            getattr(self.flame_cues, "fault", "") or "")
        out["laser_fault"] = self.laser_fault()
        out["disarm_connected"] = self._disarm_fn() is not None
        now = self.clock()
        tok = ctx.device
        with self._arm_lock:
            holds = [{"group": i, "who": h["who"], "device": h["device"],
                      "held_s": round(h["beat"] - h["start"], 2),
                      "mine": h["token"] == tok}
                     for i, h in self._holds.items()
                     if now - h["beat"] <= BEAT_STALE_S]
        out["arming"] = {"enabled": self.arming_enabled(),
                         "needs_s": SCREEN_HOLD_S, "fresh_s": ARM_FRESH_S,
                         "holds": holds}
        self._apply_pulse()
        out["alerts"] = self.alerts.view()
        booth = getattr(self.alerts.booth, "device_name", "") or ""
        out["booth"] = {"configured": bool(booth), "device": booth}
        # The lock banner and the purge, on every screen (morning.py).
        m = self.morning
        if m is not None:
            try:
                out["morning"] = m.status_view()
            except Exception as e:                   # noqa: BLE001
                out["morning"] = {"error": f"{type(e).__name__}: {e}"}
            out["morning"]["purge_enabled"] = self.purge_enabled(ctx.local)
        return out
