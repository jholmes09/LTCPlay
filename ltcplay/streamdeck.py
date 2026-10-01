"""The real Stream Deck wiring: the keys from tools/streamdeck_design_demo.py
(approved look, bench demo only -- "connected to nothing else: no show, no
flames, no lasers") driving REAL flamesafe arm/disarm, and real Start Now /
Hold / Abort through ltcplay/conductor.py where one is connected.

WHAT IS REAL AND WHAT IS NOT, read this before wiring it into a show
====================================================================

REAL, unconditionally, the moment this runs against the real hardware:
  - The three bottom-row keys arm and disarm real flamesafe groups, over a
    new keyed loopback link (CONTRACT.md, flamesafe/arminput.SocketArmInput)
    that this module is the ltcplay-side half of. flamesafe's own consent,
    dwell, chatter and edge-quiet rules are untouched; this module only
    says what is WANTED, exactly like the test driver.
  - Pressing ABORT (a 0.5 s hold, same as the demo) sends every group's
    wanted state to false on that SAME link, at once, before anything else:
    this is the "an Abort from the Stream Deck disarms (the safety program
    owns the deck)" path conductor.py's ShowOutputs.flames_disarm_all
    docstring describes. It happens whether or not a Conductor is wired in
    at all.
  - Disarm (pressing an armed or held group key) is INSTANT: this module
    never adds a hold-down or confirm step to a disarm press (Jeff,
    2026-10-01). Arming keeps whatever confirm/wait shape flamesafe's own
    composer already gives it (dwell, dirty-edge, chatter); this module
    does not add or remove any of that, it only reports it.
  - The operator gate: START NOW, HOLD/RESUME and arming a group are
    refused, loudly, while no operator is selected (see OPERATOR GATE
    below). ABORT and disarming a group are NEVER gated on this: a press
    that only reduces risk must never wait on a picker.

NOT REAL unless this module is given a `conductor` (ltcplay.conductor.
Conductor, or anything shaped like it) by whoever starts it:
  - START NOW, HOLD and RESUME do nothing but journal a refusal. There is
    today no production Conductor built anywhere in ltcplay (conductor.py's
    own docstring: "nothing constructs a Conductor outside the selftest"),
    and no scheduler route that starts a show on demand
    (schedule_service.py: "there is deliberately no route that starts,
    stops, holds or arms anything"). Wiring a real Conductor into a running
    ltcplay (real ShowOutputs for music/pixels/flames, real DeviceOutputs
    for lasers/video once PR #17 lands) is separate, later work; this
    module is ready for it (see Controller.conductor) but does not invent
    one itself -- that is a bigger, separate change than Stream Deck
    wiring, and this PR does one thing.
  - Even WITH a conductor wired in, Abort's cascade to lasers, video,
    pixels and music depends on what DeviceOutputs/ShowOutputs it was built
    with (NotWiredDevices today, since PR #17 is not merged) -- the flame
    disarm above is real regardless, but "lasers fade to black" is only as
    real as the Conductor it is asked of.

THE OPERATOR GATE (Jeff, 2026-10-01)
=====================================
Jeff wants a named operator picked before the deck's actions unlock, for
accountability, with NO password or PIN (Andy's own physical key is the
real-world accountability control; a software password is redundant).
There is no existing "who is operating the rig right now" picker anywhere
in ltcplay (checked: nothing in web/index.html, nothing in web.py, nothing
in schedule_service.py before this change). The simplest correct thing,
per Jeff, is built here: schedule_service.py now keeps a
`current_operator` (ltcplay_current_operator.json, validated against the
existing operator list, ltcplay_operators.json) and serves it at
GET/POST /api/schedule/operator. This module READS that over the SAME
local web server ltcplay already runs for the Rack screen and Phone
(loopback traffic skips the token, see web.py's _authorised); it never
writes it. Picking the operator is the Rack screen's job (or any other
client of that route); the deck has no spare key to spend on a picker
and the approved layout is not to be changed.

FAILURE MODES, assumptions made here (flag these in the safety review):

  1. Deck unplugged mid-show, while armed. ArmSocket keeps sending whatever
     this module last told it to; but the Stream Deck's own read/write
     failing (DeckDisconnected) stops the main loop from calling it again
     at all -- no more arm frames leave this machine. flamesafe's EXISTING
     arm_stale_ms rule (500 ms default, unchanged by this PR) then disarms
     every group with no new code: silence is silence, whatever caused it.
  2. Deck reconnected while armed. On every (re)open, this module resets
     its own `wanted` vector to all-false and ArmSocket's seq to 0 before
     sending anything: it never remembers what was pressed before the
     gap. This also could not re-arm anything even if it tried: flamesafe's
     consent rule (rule 6) needs the counter seen advancing while already
     fresh, and a counter that restarts fails that by construction. The
     operator re-arms by pressing each group key again, same as the lamp
     already says ("cycle the arm").
  3. A key press while flamesafe itself is in fault, or its own status
     link has gone quiet for more than a second (CONTRACT.md: "ltcplay
     shows red for the safety program"). Arm/disarm presses are still SENT
     (sending costs nothing and a disarm must never be swallowed); whether
     a group actually (dis)arms is, as always, flamesafe's call, and its
     own status will read it back (e.g. "held: safety program fault").
     This module adds no special-case logic for a fault: there already is
     one, in rules.py, and duplicating it here would be a second place to
     get it right or wrong.
  4. Two people reaching for the deck "at once". The hardware reports one
     key-state snapshot per read; this module's single reader thread
     processes it in a fixed key order, so two simultaneous presses are
     both delivered, in that order, same as two presses a moment apart by
     the same person. There is no separate lock-out for a second presser:
     the operator picker names who to hold accountable in the journal, not
     who is allowed to press, matching how conductor.py's own generation
     counter and composer.assert_arm already resolve "two requests at
     once" for every other input this show has.
  5. More than 3 flamesafe groups configured. The deck has exactly 3
     bottom-row keys. This module refuses to start (loud error, not a
     silent partial mapping) if given more than 3 group names: deciding
     which real groups share a key, or which groups the deck simply cannot
     reach, is a decision for Jeff and Andy, not a default this module
     should guess at. flamesafe.example.json ships with 6 groups today;
     a real deployment of this deck needs that trimmed to 3, or the deck
     extended, before showtime.
"""
from __future__ import annotations

import io
import json
import os
import socket
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------------------
# Visual constants and geometry, copied from tools/streamdeck_design_demo.py
# (the approved look) and left as close to it as code reuse allows. This
# file does not import that one: it is a frozen bench tool, kept standalone
# on purpose (see its own docstring), and this is the production build of
# the same artwork driven by real data instead of a local fake.
# --------------------------------------------------------------------------
VID, PID = 0x0FD9, 0x0063
K = 80
COLS, ROWS = 3, 2
W, H = COLS * K, ROWS * K

BLACK = (7, 6, 5)
GOLD = (212, 168, 74)
CHAMPAGNE = (246, 227, 174)
BULB_OFF = (52, 41, 20)
OUTLINE_DIM = (92, 72, 32)
RED = (230, 30, 24)
DIM_TEXT = (72, 66, 58)
GREEN = (40, 190, 90)
AMBER = (245, 160, 30)

ABORT_HOLD_S = 0.5          # Jeff: unchanged from the demo. Disarm itself
                            # (a bottom-row key) has NO hold; see module doc.
ARM_SEND_HZ = 20.0          # >= the arm link's 10 Hz floor (CONTRACT.md)
STATUS_STALE_S = 1.0        # CONTRACT.md: "ltcplay shows red for the safety
                            # program" past this
GROUP_KEY_LIMIT = 3         # the deck's bottom row. See module docstring 5.

TOP_START, TOP_HOLD, TOP_ABORT = 0, 1, 2
GROUP_KEYS = (3, 4, 5)


def _first_font(paths):
    for p in paths:
        if os.path.exists(p):
            return p
    return None


def _import_hid():
    try:
        import hid
        return hid
    except Exception as e:
        raise SystemExit(
            "The hidapi package is not installed, so the Stream Deck "
            "cannot be opened.\n"
            f"  {e}\n"
            "Run the installer again, or: pip install hidapi")


def _import_pil():
    try:
        from PIL import Image, ImageDraw, ImageFont
        return Image, ImageDraw, ImageFont
    except Exception as e:
        raise SystemExit(
            "The Pillow package is not installed, so the Stream Deck's key "
            "artwork cannot be drawn.\n"
            f"  {e}\n"
            "Run the installer again, or: pip install pillow")


class DeckDisconnected(Exception):
    """The Stream Deck stopped answering (unplugged, USB fault, driver
    error). The caller must stop sending arm frames at once; see module
    docstring, failure mode 1."""


# --------------------------------------------------------------------------
# Pure protocol: the arm frame this module sends, and the status frame it
# reads. Hand-written from flamesafe/CONTRACT.md, exactly as ltcplay's own
# flame-frame encoder would be: this module never imports flamesafe (the
# wall test, selftest.py's test_the_wall_between_ltcplay_and_flamesafe,
# fails the build if it does).
# --------------------------------------------------------------------------
CONTRACT_VERSION = 2


def encode_arm_frame(seq, wanted, names, key):
    return json.dumps({
        "v": CONTRACT_VERSION, "k": key, "t": "arm", "seq": int(seq),
        "wanted": [bool(w) for w in wanted],
        "names": [str(n) for n in names],
    }, separators=(",", ":")).encode("utf-8")


def decode_status_frame(data, key):
    """The fields this module actually uses off a status frame, or None if
    it is not a status frame with our key. Never raises: a malformed or
    foreign datagram is simply not a status frame, and the deck keeps
    showing whatever it last knew (then goes stale after STATUS_STALE_S,
    same as CONTRACT.md's own rule for ltcplay)."""
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or obj.get("v") != CONTRACT_VERSION:
        return None
    if obj.get("k") != key or obj.get("t") != "status":
        return None
    groups = obj.get("groups")
    if not isinstance(groups, list):
        return None
    return obj


# --------------------------------------------------------------------------
# ArmSocket: the one socket this module sends arm frames from, for its
# whole lifetime (CONTRACT.md's own lesson from the flame-frame link,
# applied here too). Resets wanted and seq to a clean slate on every open,
# never on its own initiative otherwise.
# --------------------------------------------------------------------------
class ArmSocket:

    def __init__(self, ip, port, key, n):
        self.ip = ip
        self.port = port
        self.key = key
        self.n = n
        self._sock = None
        self.wanted = [False] * n
        self.seq = 0

    def open(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.wanted = [False] * self.n
        self.seq = 0

    def set_group(self, i, on):
        self.wanted[i] = bool(on)

    def set_all(self, on):
        self.wanted = [bool(on)] * self.n

    def send(self, names):
        """Send the current `wanted` vector with an advancing seq. Called
        at ARM_SEND_HZ or faster, whether or not anything changed (the arm
        link's own 10 Hz-or-faster rule, CONTRACT.md)."""
        if self._sock is None:
            return
        self.seq += 1
        try:
            self._sock.sendto(encode_arm_frame(self.seq, self.wanted, names,
                                               self.key), (self.ip, self.port))
        except OSError:
            pass  # a dropped UDP send; the next one, 50 ms away, tries again

    def close(self):
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


class StatusSocket:
    """Reads flamesafe's status frames for display only. See CONTRACT.md:
    "There is no path from a status frame to flame output" -- this module
    holds to that just as strictly: nothing read here is ever sent back as
    an arm or fire command, only drawn on a key."""

    def __init__(self, ip, port, key):
        self.ip = ip
        self.port = port
        self.key = key
        self._sock = None
        self.last = None
        self.last_at = None

    def open(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((self.ip, self.port))
        sock.setblocking(False)
        self._sock = sock

    def poll(self, clock=time.monotonic):
        sock = self._sock
        if sock is None:
            return
        for _ in range(200):
            try:
                data, _addr = sock.recvfrom(65535)
            except BlockingIOError:
                return
            except OSError:
                return
            obj = decode_status_frame(data, self.key)
            if obj is not None:
                self.last = obj
                self.last_at = clock()

    def stale(self, clock=time.monotonic):
        return self.last_at is None or (clock() - self.last_at) > STATUS_STALE_S

    def close(self):
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


# --------------------------------------------------------------------------
# Pure logic: visual state from real data. No socket, no hid, fully
# unit-testable (see selftest.py).
# --------------------------------------------------------------------------
def group_look(group_status):
    """(line1, line2_or_None, bg, text, flashing) for one bottom-row key
    from the status frame's own per-group dict (CONTRACT.md section on the
    status frame), or from None (no status ever received / stale): drawn
    as "NO LINK", matching CONTRACT.md's "ltcplay shows red for the safety
    program" rule -- the deck never claims a group is armed, disarmed or
    anything else when it cannot actually see flamesafe's answer."""
    if group_status is None:
        return "NO", "LINK", (26, 24, 21), DIM_TEXT, True
    armed = group_status.get("armed")
    if armed == "armed":
        return "ARMED", None, GREEN, (6, 30, 12), False
    if armed == "disarmed":
        return "OFF", None, (44, 36, 24), CHAMPAGNE, False
    # held: dwell_s counts down (re-arm dwell, chatter); otherwise the
    # reason is shown, flashing exactly when CONTRACT.md's own `amber`
    # field says cycling the arm is the fix.
    flashing = group_status.get("amber") == "flashing"
    dwell = group_status.get("dwell_s") or 0
    if dwell > 0:
        return str(int(dwell)), None, AMBER, (40, 20, 0), flashing
    reason = group_status.get("reason") or "held"
    short = _SHORT_REASON.get(reason, "HELD")
    line1, line2 = short.split(" ", 1) if " " in short else (short, None)
    return line1, line2, AMBER, (40, 20, 0), flashing


# flamesafe's own reason sentences (CONTRACT.md), to a label that fits a
# 56x30 px key face. Anything not in this table still shows (dim "HELD"),
# it is just less specific; nothing here hides a real reason, it only
# abbreviates the ones already catalogued in CONTRACT.md.
_SHORT_REASON = {
    "cycle the arm": "CYCLE ARM",
    "dirty edge": "DIRTY EDGE",
    "re-arm dwell": "WAIT",
    "chatter": "CHATTER",
    "arm input stale": "NO SIGNAL",
    "arm input has never asserted": "NEVER ARMED",
    "safety program fault": "FAULT",
    "Show program stopped answering: disarmed. Cycle the arm to re-arm "
    "once it is back.": "SHOW LOST",
    "Show program has not answered yet: disarmed. Cycle the arm once it "
    "is running.": "SHOW LOST",
}


def abort_is_live(show_running):
    """Jeff, 2026-10-01: ABORT dims (not interactive) with nothing to
    abort, between shows or idle; live only while a show is playing or
    held. `show_running` is True/False/None (None: unknown -- treated as
    "not live", the safe default: a key that might do nothing is better
    than one that looks live and does nothing)."""
    return show_running is True


def operator_gate(current_operator, action):
    """None if `action` may proceed, else a refusal sentence. ABORT and a
    DISARM press (action="disarm") are never gated: see module docstring.
    Everything else (arm, start, hold, resume) needs a chosen operator."""
    if action in ("abort", "disarm"):
        return None
    if current_operator:
        return None
    return (f"the Stream Deck's {action} key was pressed but no operator is "
            f"chosen. Pick an operator on the Rack screen first. Nothing "
            f"was done.")


class AbortHold:
    """The 0.5 s hold-to-fire timer for ABORT, exactly as the demo: held
    down continuously for ABORT_HOLD_S fires once; let go early and it
    resets to nothing. Pure (a clock function is passed in), so the exact
    demo behaviour is unit-tested without hardware."""

    def __init__(self, hold_s=ABORT_HOLD_S):
        self.hold_s = hold_s
        self._down_at = None

    def press(self, now):
        self._down_at = now

    def release(self):
        self._down_at = None

    def fraction(self, now):
        """0..1 while held, 0 once released. 1.0 means "fires now"."""
        if self._down_at is None:
            return 0.0
        return min(1.0, (now - self._down_at) / self.hold_s)

    def fired(self, now):
        """True exactly once, the instant the hold reaches 1.0 (consumes
        the press: calling it again before the next press returns False,
        matching the demo's own abort_down_at = None on fire)."""
        if self._down_at is not None and (now - self._down_at) >= self.hold_s:
            self._down_at = None
            return True
        return False


def edges(prev, cur):
    """Which key indices went from up to down between two reads, in a
    fixed, deterministic order (see module docstring, failure mode 4)."""
    return [k for k in range(len(cur)) if cur[k] and not prev[k]]


def releases(prev, cur):
    return [k for k in range(len(cur)) if prev[k] and not cur[k]]


# --------------------------------------------------------------------------
# A plain local HTTP read of ltcplay's own web server: loopback traffic
# skips the token (web.py's _authorised), so this is an ordinary GET/POST,
# the same kind the Rack screen's own page already makes. Never imports
# web.py or schedule_service.py: this module may run as its own process,
# entirely separate from `ltc serve`.
# --------------------------------------------------------------------------
CACHE_S = 0.25   # the draw loop asks every frame (20 Hz); this spares
                 # ltcplay's web server a request on every one of them for
                 # data that only changes when a person does something


class LocalSchedule:
    """Reads the operator and the scheduler's state over HTTP from a
    running `ltc serve --schedule` on this machine. Returns ("", None) for
    anything it cannot reach: a server not running, not reachable yet, or
    without a schedule configured is the same, from here, as "nothing to
    read" -- never an exception out of the main loop."""

    def __init__(self, base_url, clock=time.monotonic):
        self.base_url = base_url.rstrip("/")
        self._clock = clock
        self._cache = {}   # path -> (fetched_at, value)

    def _get(self, path):
        at, value = self._cache.get(path, (None, None))
        now = self._clock()
        if at is not None and (now - at) < CACHE_S:
            return value
        try:
            with urllib.request.urlopen(self.base_url + path, timeout=1.0) as r:
                value = json.loads(r.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError):
            value = None
        self._cache[path] = (now, value)
        return value

    def current_operator(self):
        doc = self._get("/api/schedule/operator")
        if not isinstance(doc, dict):
            return ""
        return str(doc.get("current_operator") or "")

    def show_running(self):
        """True while the scheduler's own state says a show is playing or
        held (schedule.py's SHOW/PAUSED -- the same two names conductor.py
        reuses for LASER_STATES), False otherwise, None if unreachable."""
        doc = self._get("/api/schedule/state")
        if not isinstance(doc, dict) or not doc.get("ok"):
            return None
        return doc.get("state") in ("SHOW", "PAUSED")


# --------------------------------------------------------------------------
# Drawing: geometry and helpers ported from tools/streamdeck_design_demo.py
# (the approved look), kept as close to it as a second file reasonably can.
# Duplicated rather than imported on purpose: the demo stays a frozen,
# standalone bench tool (its own docstring: "connected to nothing else"),
# and this file must import neither it nor flamesafe.
# --------------------------------------------------------------------------
def _spaced_width(d, text, f, sp):
    return sum(d.textlength(c, font=f) for c in text) + sp * max(0, len(text) - 1)


class Fonts:
    """Lazily built, once Pillow is known to be importable."""

    def __init__(self):
        Image, ImageDraw, ImageFont = _import_pil()
        self.ImageDraw = ImageDraw
        serif = _first_font([
            "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
            r"C:\Windows\Fonts\georgiab.ttf",
        ]) or _first_font(["/System/Library/Fonts/Helvetica.ttc"])
        self._serif_path = serif
        self._cache = {}
        self.ImageFont = ImageFont
        self.Image = Image

    def _sans_bold(self, size):
        for path in ("/System/Library/Fonts/Avenir Next Condensed.ttc",
                     "/System/Library/Fonts/HelveticaNeue.ttc"):
            if not os.path.exists(path):
                continue
            for idx in range(12):
                try:
                    f = self.ImageFont.truetype(path, size, index=idx)
                except Exception:
                    break
                name = " ".join(f.getname()).lower()
                if "bold" in name and "italic" not in name and "ultra" not in name:
                    return f
        for path in (r"C:\Windows\Fonts\bahnschrift.ttf",
                     r"C:\Windows\Fonts\arialnb.ttf",
                     r"C:\Windows\Fonts\arialbd.ttf"):
            if os.path.exists(path):
                f = self.ImageFont.truetype(path, size)
                if path.endswith("bahnschrift.ttf"):
                    try:
                        f.set_variation_by_name("Bold SemiCondensed")
                    except Exception:
                        pass
                return f
        return self.ImageFont.truetype(self._serif_path, size)

    def get(self, kind, size):
        if (kind, size) not in self._cache:
            self._cache[(kind, size)] = (
                self.ImageFont.truetype(self._serif_path, size)
                if kind == "serif" else self._sans_bold(size))
        return self._cache[(kind, size)]

    def text_block(self, d, box, lines, kind, fill, max_size, sp_ratio=0.06):
        x0, y0, x1, y1 = box
        size = max_size
        while size > 8:
            f = self.get(kind, size)
            sp = size * sp_ratio
            cap = f.getbbox("H")
            cap_h = cap[3] - cap[1]
            pitch = cap_h * 1.4
            total_h = cap_h + pitch * (len(lines) - 1)
            if all(_spaced_width(d, ln, f, sp) <= (x1 - x0) - 2 for ln in lines) \
                    and total_h <= (y1 - y0) - 4:
                break
            size -= 1
        f = self.get(kind, size)
        sp = size * sp_ratio
        cap = f.getbbox("H")
        cap_h = cap[3] - cap[1]
        pitch = cap_h * 1.4
        total_h = cap_h + pitch * (len(lines) - 1)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        for i, ln in enumerate(lines):
            w = _spaced_width(d, ln, f, sp)
            lead = f.getbbox(ln[0])[0]
            trail = d.textlength(ln[-1], font=f) - f.getbbox(ln[-1])[2]
            ink_w = w - lead - trail
            x = cx - ink_w / 2 - lead
            cap_top = cy - total_h / 2 + pitch * i
            y = cap_top - cap[1]
            for c in ln:
                d.text((x, y), c, font=f, fill=fill)
                x += d.textlength(c, font=f) + sp

    def show_key(self, d, box, lines, text, bg=None, kind="serif", max_size=24):
        if bg:
            d.rounded_rectangle(box, radius=5, fill=bg)
        self.text_block(d, (box[0] + 2, box[1] + 2, box[2] - 2, box[3] - 2),
                        lines, kind, text, max_size)


def key_origin(k):
    return (k % COLS) * K, (k // COLS) * K


def face_box(k, margin=8):
    ox, oy = key_origin(k)
    return (ox + margin, oy + margin, ox + K - margin, oy + K - margin)


LINE_INSET, LINE_W = 4, 3


def _outer_path(inset):
    x0, y0, x1, y1 = inset, inset, W - inset, H - inset
    pts = [(x, y0) for x in range(x0, x1)]
    pts += [(x1, y) for y in range(y0, y1)]
    pts += [(x, y1) for x in range(x1, x0, -1)]
    pts += [(x0, y) for y in range(y1, y0, -1)]
    return pts


def _in_gap(x, y, inset):
    return ((x % K) < inset or (x % K) > K - inset) and x not in (inset, W - inset) \
        or ((y % K) < inset or (y % K) > K - inset) and y not in (inset, H - inset)


PATH_B = [p for p in _outer_path(LINE_INSET) if not _in_gap(p[0], p[1], LINE_INSET)]
RUN_FRACTION = 0.16


def draw_outline_chase(d, chase, abort_frac):
    """The "look b" outline chase from the demo: a solid border round every
    key, and a bright run travelling round the outside, or a full red fill
    while Abort is held or latched. Purely decorative; no safety meaning."""
    for k in range(6):
        ox, oy = key_origin(k)
        d.rounded_rectangle((ox + LINE_INSET, oy + LINE_INSET,
                             ox + K - LINE_INSET, oy + K - LINE_INSET),
                            radius=7, outline=OUTLINE_DIM, width=LINE_W)
    n = len(PATH_B)
    if abort_frac > 0:
        seg = PATH_B[:round(abort_frac * n)]
        colour = RED
    else:
        run = round(n * RUN_FRACTION)
        start = (chase * 4) % n
        seg = [PATH_B[(start - j) % n] for j in range(run)]
        colour = GOLD
    for (x, y) in seg:
        d.rectangle((x - 1, y - 1, x + 1, y + 1), fill=colour)


def arm_key_image(fonts, d, box, name, look, blink_on):
    """One bottom-row key: the real group name on top, the real status
    below it (group_look's output), flashing when told to."""
    line1, line2, bg, text, flashing = look
    x0, y0, x1, y1 = box
    bar = y0 + 17
    fonts.text_block(d, (x0, y0 - 1, x1, bar), [_fit_name(name)], "sans",
                     CHAMPAGNE, 14)
    body = (x0, bar + 1, x1, y1)
    if flashing and not blink_on:
        d.rounded_rectangle(body, radius=4, fill=(26, 24, 21))
        fonts.text_block(d, body, [line1] + ([line2] if line2 else []),
                         "sans", DIM_TEXT, 18)
        return
    d.rounded_rectangle(body, radius=4, fill=bg)
    lines = [line1] + ([line2] if line2 else [])
    fonts.text_block(d, body, lines, "sans", text, 20 if line2 else 24)


def _fit_name(name):
    """A real flamesafe group name, shortened to fit a ~56px-wide label.
    Never silently truncates to nothing: a name this short still reads,
    just not in full -- the full name is always in the journal and the
    status frame, never only on the key."""
    name = name.upper()
    return name if len(name) <= 10 else name[:9] + "\u2026"


def to_native(Image, img):
    img = img.rotate(90).transpose(Image.FLIP_TOP_BOTTOM)
    with io.BytesIO() as buf:
        img.save(buf, "BMP")
        return buf.getvalue()


class Deck:
    """The physical Stream Deck Mini. Any read or write failure is raised
    as DeckDisconnected: the caller (Controller) must stop sending arm
    frames the instant this happens (module docstring, failure mode 1), so
    this class never swallows an I/O error to "keep going"."""

    def __init__(self):
        hid = _import_hid()
        self.h = hid.device()
        try:
            self.h.open(VID, PID)
        except Exception as e:
            raise DeckDisconnected(f"could not open the Stream Deck: {e}") from e
        self.h.set_nonblocking(1)
        self.h.send_feature_report([0x0B, 0x63] + [0] * 15)
        self.h.send_feature_report([0x05, 0x55, 0xAA, 0xD1, 0x01, 80] + [0] * 11)
        self.last = {}

    def set_key(self, Image, key, img):
        data = to_native(Image, img)
        if self.last.get(key) == data:
            return
        self.last[key] = data
        step = 1024 - 16
        page = sent = 0
        try:
            while sent < len(data):
                chunk = data[sent:sent + step]
                last = 1 if sent + len(chunk) >= len(data) else 0
                pkt = (bytes([0x02, 0x01, page, 0, last, key + 1] + [0] * 10)
                      + chunk)
                self.h.write(pkt + bytes(1024 - len(pkt)))
                sent += len(chunk)
                page += 1
        except Exception as e:
            raise DeckDisconnected(f"could not write a key image: {e}") from e

    def keys_down(self):
        """The latest 6-key snapshot since the last call, or the same one
        if nothing changed. Never None once open: a USB read failure is a
        disconnect, raised, not a silent "nothing changed"."""
        latest = None
        try:
            while True:
                r = self.h.read(7)
                if not r:
                    return latest
                latest = [bool(v) for v in r[1:7]]
        except Exception as e:
            raise DeckDisconnected(f"could not read the keys: {e}") from e

    def close(self):
        try:
            Image, _ImageDraw, _ImageFont = _import_pil()
            black = Image.new("RGB", (K, K), (0, 0, 0))
            self.last.clear()
            for k in range(6):
                self.set_key(Image, k, black)
        except Exception:
            pass
        try:
            self.h.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# The controller: wires a Deck, an ArmSocket, a StatusSocket and (optionally)
# a conductor and a LocalSchedule together. Every collaborator is injected,
# so the loop itself (run_once) is unit-testable with fakes; only main()
# constructs the real hardware and real sockets.
# --------------------------------------------------------------------------
class Controller:
    """One Stream Deck. `group_names` must be 1 to GROUP_KEY_LIMIT names,
    in flamesafe's own config order -- see module docstring, failure mode
    5, for why this refuses more than that instead of guessing a mapping.

    `conductor` is optional and duck-typed: .hold(who, screen),
    .resume(who, screen), .abort(who, screen), .snapshot() (for `latched`).
    None (the default) means no show control is connected; see module
    docstring for exactly what still works without one.

    `operator_provider` () -> current operator name or "". Defaults to
    always "" (operator gate never opens) if not given, which is the safe
    default: a deck that cannot ask who is operating must never ACT as if
    someone had been chosen.

    `show_running_provider` () -> True/False/None, for the ABORT key's
    dimness. None (unreachable) dims the key: see abort_is_live."""

    def __init__(self, arm_socket, status_socket, group_names, deck_factory=Deck,
                operator_provider=lambda: "", show_running_provider=lambda: None,
                conductor=None, journal=None, clock=time.monotonic,
                sleep=time.sleep):
        if not (1 <= len(group_names) <= GROUP_KEY_LIMIT):
            raise ValueError(
                f"the Stream Deck has {GROUP_KEY_LIMIT} arm keys; it was "
                f"given {len(group_names)} flamesafe group names "
                f"({', '.join(group_names) or 'none'}). Trim flamesafe's "
                f"config to {GROUP_KEY_LIMIT} groups for deck control, or "
                f"decide how more than {GROUP_KEY_LIMIT} should share a "
                f"key before wiring this up -- this module will not guess.")
        self.arm = arm_socket
        self.status = status_socket
        self.names = list(group_names)
        self.deck_factory = deck_factory
        self.operator_provider = operator_provider
        self.show_running_provider = show_running_provider
        self.conductor = conductor
        self._journal = journal or (lambda text, **kw: None)
        self._clock = clock
        self._sleep = sleep
        self._abort_hold = AbortHold()
        self._latched = False      # fallback when no conductor is wired
        self._prev_keys = [False] * 6

    def _log(self, text, **kw):
        try:
            self._journal(text, **kw)
        except Exception:
            pass

    def _latched_now(self):
        if self.conductor is not None:
            try:
                return bool(self.conductor.snapshot().get("latched"))
            except Exception as e:
                self._log(f"could not read the show conductor's state "
                         f"({type(e).__name__}: {e}); treating the rig as "
                         f"not latched.", fault=True)
                return False
        return self._latched

    def _do_abort(self):
        """ALWAYS real: every group's wanted goes false on the arm link at
        once, whether or not a conductor is connected (module docstring)."""
        self.arm.set_all(False)
        self.arm.send(self.names)
        who = self.operator_provider() or ""
        if self.conductor is not None:
            r = self.conductor.abort(who=who, screen="Stream Deck")
            self._log(f"Stream Deck Abort: {r.sentence}", fault=not r.ok,
                      action="abort", who=who, screen="Stream Deck")
        else:
            self._latched = True
            self._log("Stream Deck Abort: every flame group's wanted state "
                      "was sent false at once. No show conductor is "
                      "connected in this build, so lasers, video, pixels "
                      "and music were not asked to do anything.",
                      fault=True, action="abort", who=who,
                      screen="Stream Deck")

    def _do_reset(self):
        who = self.operator_provider() or ""
        if self.conductor is not None:
            r = self.conductor.reset(who=who, screen="Stream Deck")
            self._log(f"Stream Deck Reset: {r.sentence}", fault=not r.ok,
                      action="reset", who=who, screen="Stream Deck")
        else:
            self._latched = False
            self._log("Stream Deck Reset.", action="reset", who=who,
                      screen="Stream Deck")

    def _do_hold_or_resume(self, held_look):
        who = self.operator_provider() or ""
        refusal = operator_gate(who, "hold")
        if refusal:
            self._log(f"Stream Deck: {refusal}", action="hold")
            return
        if self.conductor is None:
            self._log("Stream Deck Hold/Resume pressed: no show conductor "
                      "is connected in this build. Nothing was done.",
                      fault=True, action="hold", who=who,
                      screen="Stream Deck")
            return
        if held_look:
            r = self.conductor.resume(who=who, screen="Stream Deck")
            action = "resume"
        else:
            r = self.conductor.hold(who=who, screen="Stream Deck")
            action = "hold"
        self._log(f"Stream Deck {action.title()}: {r.sentence}",
                  fault=not r.ok, action=action, who=who, screen="Stream Deck")

    def _do_start_now(self):
        who = self.operator_provider() or ""
        refusal = operator_gate(who, "start")
        if refusal:
            self._log(f"Stream Deck: {refusal}", action="start")
            return
        self._log("Stream Deck Start Now pressed: there is no real 'start "
                  "the show now' entry point wired up yet in this build "
                  "(the scheduler decides when a show starts; the "
                  "conductor only reconciles the rig once a cue has "
                  "already begun). Nothing was started.", fault=True,
                  action="start", who=who, screen="Stream Deck")

    def _do_group_press(self, i):
        who = self.operator_provider() or ""
        turning_on = not self.arm.wanted[i]
        action = "arm" if turning_on else "disarm"
        if turning_on:
            refusal = operator_gate(who, "arm")
            if refusal:
                self._log(f"Stream Deck: {refusal}", action="arm")
                return
        # Disarm is instant, always, gate or no gate (Jeff, 2026-10-01):
        # just flip `wanted` and let the next send() carry it. Arming is
        # exactly as instant to SEND; flamesafe's own consent/dwell/edge
        # rules, unchanged by this module, decide what happens next.
        self.arm.set_group(i, turning_on)
        self.arm.send(self.names)
        self._log(f"Stream Deck: {self.names[i]} {action} pressed by "
                  f"{who or 'an operator the deck could not name'}.",
                  action=action, who=who, screen="Stream Deck")

    def run_once(self, down):
        """One pass given the deck's current 6-key snapshot. Pure apart
        from the collaborators it was built with; `down` is the only new
        input, so this is unit-testable with a fake deck snapshot."""
        now = self._clock()
        latched = self._latched_now()
        if latched:
            # Aborted: only a plain press of the Abort/Reset key (index 2)
            # does anything (the demo's own rule, kept exactly).
            for k in edges(self._prev_keys, down):
                if k == TOP_ABORT:
                    self._do_reset()
            self._abort_hold.release()
            self._prev_keys = list(down)
            return
        for k in releases(self._prev_keys, down):
            if k == TOP_ABORT:
                self._abort_hold.release()
        for k in edges(self._prev_keys, down):
            if k == TOP_START:
                self._do_start_now()
            elif k == TOP_HOLD:
                self._do_hold_or_resume(self._held_hint())
            elif k == TOP_ABORT:
                if abort_is_live(self.show_running_provider()):
                    self._abort_hold.press(now)
            elif k in GROUP_KEYS:
                self._do_group_press(k - GROUP_KEYS[0])
        if down[TOP_ABORT] and self._abort_hold.fired(now):
            self._do_abort()
        self._prev_keys = list(down)

    def status_for(self, name):
        """The real per-group status dict for `name`, or None if no status
        frame has ever arrived or the last one is stale (module docstring;
        CONTRACT.md's own "ltcplay shows red" rule, applied here too)."""
        if self.status.stale(self._clock) or self.status.last is None:
            return None
        for g in self.status.last.get("groups", []):
            if g.get("name") == name:
                return g
        return None

    def draw(self, fonts, blink_on, chase):
        """One frame, as a PIL Image the size of the whole deck. Kept
        separate from run_once so the loop can draw at its own rate (20 Hz)
        independently of how often new key presses arrive."""
        Image, ImageDraw, _ImageFont = fonts.Image, fonts.ImageDraw, None
        canvas = Image.new("RGB", (W, H), BLACK)
        d = ImageDraw.Draw(canvas)
        now = self._clock()
        latched = self._latched_now()
        abort_frac = 1.0 if latched else self._abort_hold.fraction(now)
        draw_outline_chase(d, chase, abort_frac)
        running = self.show_running_provider()
        live = abort_is_live(running)
        b0 = face_box(TOP_START)
        if latched:
            fonts.show_key(d, b0, ["START", "NOW"], DIM_TEXT)
        else:
            op = self.operator_provider()
            if operator_gate(op, "start"):
                fonts.show_key(d, b0, ["PICK", "OPERATOR"], DIM_TEXT, kind="sans",
                              max_size=16)
            else:
                fonts.show_key(d, b0, ["START", "NOW"], CHAMPAGNE)
        b1 = face_box(TOP_HOLD)
        if latched:
            fonts.show_key(d, b1, ["HOLD"], DIM_TEXT, max_size=28)
        elif self._held_hint() and blink_on:
            fonts.show_key(d, b1, ["RESUME"], BLACK, bg=GOLD)
        elif self._held_hint():
            fonts.show_key(d, b1, ["RESUME"], GOLD)
        else:
            fonts.show_key(d, b1, ["HOLD"], CHAMPAGNE, max_size=28)
        b2 = face_box(TOP_ABORT)
        if latched:
            fonts.show_key(d, b2, ["RESET"], BLACK if blink_on else RED,
                           bg=RED if blink_on else None)
        else:
            fonts.show_key(d, b2, ["ABORT"], RED if live else DIM_TEXT)
        for i, name in enumerate(self.names):
            box = face_box(GROUP_KEYS[i])
            look = ("OFF", None, (26, 24, 21), DIM_TEXT, False) if latched \
                else group_look(self.status_for(name))
            arm_key_image(fonts, d, box, name, look, blink_on)
        return canvas

    def _held_hint(self):
        """Whether the show looks HELD right now, for the HOLD key's own
        toggle (Hold vs Resume). Best-effort from the conductor snapshot
        when one is wired; otherwise the show-running provider cannot tell
        PLAYING from HELD, so this defaults to "Hold" (the safer guess:
        asking to hold an already-held show is a no-op, per conductor.py)."""
        if self.conductor is not None:
            try:
                return self.conductor.snapshot().get("look") in ("HELD", "DARK")
            except Exception:
                return False
        return False


# --------------------------------------------------------------------------
# The real hardware loop. Everything above this line is unit-testable
# without a Stream Deck plugged in; everything below it is the glue that
# only makes sense against the real device, and is exercised on the bench,
# not in selftest.py.
# --------------------------------------------------------------------------
def run_forever(controller, deck_factory=Deck, journal=None, sleep=time.sleep,
                clock=time.monotonic):
    """Opens the real Stream Deck and runs until interrupted (Ctrl-C) or
    told to stop. Reconnects on DeckDisconnected: every reconnect goes
    through controller.arm.open() again first, which resets `wanted` to
    all-false and the seq counter to 0 before anything is sent (module
    docstring, failure mode 2) -- the operator sees every group read OFF
    and re-arms by pressing it, same as after any other interruption."""
    journal = journal or (lambda text, **kw: None)
    fonts = Fonts()
    chase = 0
    period = 1.0 / ARM_SEND_HZ
    while True:
        try:
            deck = deck_factory()
        except DeckDisconnected as e:
            journal(f"Stream Deck: {e}. Retrying in 2 s.", fault=True,
                   action="deck")
            sleep(2.0)
            continue
        controller.arm.close()
        controller.arm.open()
        journal("Stream Deck connected. Every group starts OFF until "
               "pressed; nothing on this machine remembers what was armed "
               "before.", action="deck")
        try:
            while True:
                t0 = clock()
                down = deck.keys_down()
                if down is not None:
                    controller.run_once(down)
                controller.status.poll(clock)
                controller.arm.send(controller.names)
                chase += 1
                blink_on = int(t0 * 2) % 2 == 0
                img = controller.draw(fonts, blink_on, chase)
                for k in range(6):
                    ox, oy = key_origin(k)
                    deck.set_key(fonts.Image, k,
                                img.crop((ox, oy, ox + K, oy + K)))
                elapsed = clock() - t0
                if elapsed < period:
                    sleep(period - elapsed)
        except DeckDisconnected as e:
            journal(f"Stream Deck: {e}. No more arm frames will be sent "
                   f"until it reconnects; flamesafe disarms every group "
                   f"within its own arm_stale_ms.", fault=True,
                   action="deck")
            controller.arm.close()
            try:
                deck.close()
            except Exception:
                pass
            sleep(1.0)


def load_flamesafe_link(path):
    """The handful of flamesafe config fields this module needs, read as
    plain JSON (never flamesafe.config: this module must not import
    flamesafe). Returns (arm_ip, arm_port, status_ip, status_port, key,
    group_names). Raises ValueError with a plain sentence on anything
    wrong, including more than GROUP_KEY_LIMIT groups or no arm link
    configured at all."""
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    link = doc.get("link") or {}
    if "arm_port" not in link:
        raise ValueError(f"{path} has no link.arm_port: this flamesafe "
                         f"config has not been set up for a real Stream "
                         f"Deck yet (see flamesafe/CONTRACT.md, the arm "
                         f"link).")
    names = [g.get("name") for g in (doc.get("groups") or [])]
    if not all(isinstance(n, str) for n in names):
        raise ValueError(f"{path}: every group needs a name.")
    if not (1 <= len(names) <= GROUP_KEY_LIMIT):
        raise ValueError(
            f"{path} configures {len(names)} flamesafe group(s) "
            f"({', '.join(names) or 'none'}); the Stream Deck has "
            f"{GROUP_KEY_LIMIT} arm keys. Trim the config to "
            f"{GROUP_KEY_LIMIT} groups for deck control, or decide how "
            f"more should share a key before wiring this up.")
    return (link.get("arm_ip", link.get("listen_ip", "127.0.0.1")),
            int(link["arm_port"]), link.get("status_ip", "127.0.0.1"),
            int(link["status_port"]), link["key"], names)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        prog="ltc deck", description="Run the real Stream Deck: arms and "
        "disarms real flamesafe groups, and reaches Start Now, Hold and "
        "Abort through a show conductor where one is connected.")
    ap.add_argument("--flamesafe-config", required=True,
                    help="the flamesafe config this deck talks to "
                    "(its link.arm_port, link.key and group names)")
    ap.add_argument("--ltcplay-url", default="http://127.0.0.1:8080",
                    help="ltcplay's own local web server (for the chosen "
                    "operator and the show's state); default "
                    "http://127.0.0.1:8080")
    args = ap.parse_args(argv)
    try:
        arm_ip, arm_port, status_ip, status_port, key, names = \
            load_flamesafe_link(args.flamesafe_config)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"error: {e}")
        return 2
    sched = LocalSchedule(args.ltcplay_url)
    arm = ArmSocket(arm_ip, arm_port, key, len(names))
    status = StatusSocket(status_ip, status_port, key)
    status.open()

    def journal(text, **kw):
        print(("FAULT " if kw.get("fault") else "") + text)

    controller = Controller(arm, status, names,
                            operator_provider=sched.current_operator,
                            show_running_provider=sched.show_running,
                            conductor=None, journal=journal)
    print(f"Stream Deck: arming {', '.join(names)} over {arm_ip}:{arm_port}, "
         f"reading flamesafe's status on {status_ip}:{status_port}. No "
         f"show conductor is connected in this build: Start Now, Hold and "
         f"Resume will journal a refusal. Ctrl-C to stop.")
    try:
        run_forever(controller, journal=journal)
    except KeyboardInterrupt:
        pass
    finally:
        arm.close()
        status.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
