"""Named users with passwords, sign-in, and who may do what (Jeff,
2026-10-20). USERS.md says it in plain words.

WHAT THIS IS FOR: knowing who pressed what (audit.py writes it down). It is
not what keeps the show safe, and it never stands in the way of anything
that makes the rig safer.

THE SAFETY RULE (not negotiable): Abort, Hold, Stop, every disarm, letting
go of a hold, stopping or clearing a purge, locking a machine out, turning
a laser test off, disarming a laser (one, a group or every laser),
Acknowledge and signing in or out work from every
surface whether anyone is signed in or not, whatever the role, and are
logged as "unsigned" when nobody is. classify() names them "safety" and
allowed() answers True for "safety" before it looks at anything else.
Nothing in flamesafe is touched by this module: its sole writer, the
Abort latch, the link key and hold-to-arm stay exactly as they were.

SIGNED OUT IS VIEW ONLY (Jeff's field report, 2026-10-28, Tier A): once
any user exists, every press that is not safety needs a signed-in user
whose role may, on every surface: the rack screen on this machine, an
iPad, the old page and the Stream Deck (which acts as whoever is signed in
on this machine's rack screen). Being on the machine itself (loopback) is
NOT a sign-in. Signed out, a screen sees status, the schedule and the
alerts, and may only do the safety presses above: Stop, Hold, Abort,
Acknowledge and every disarm. allowed() refuses every restricted row for
nobody signed in, whatever the stored matrix says, and the "Signed out"
column is fixed off (FIXED) so nobody can tick it back on.

Who a press is from (the journal and the audit log): the signed-in user
only, never a name the page or a process puts in its body, and
"unsigned" when nobody is (UNSIGNED).

THE EARLIER PINs (PR #39, removed 2026-10-07; the 2026-10-07 "no sign-in
to run or arm" default was replaced by the rule above on 2026-10-28):
  - until the first user is made, nothing changes at all;
  - roles restrict only things that are not safety: settings, users,
    show files, unlocking, the show controls and arming;
  - the stdlib PBKDF2 store of the old PinStore is kept in spirit: a salt
    per password, a slow hash (scrypt now, PBKDF2 where scrypt is missing),
    hmac.compare_digest, and the same time spent on an unknown name;
  - the old lockout grew without end (doubling up to an hour); this one is
    short: five free tries, then 30 s, doubling to at most 5 minutes, and
    an admin can clear it.

Files, in the settings folder (never in the repo):
  ltcplay_users.json  {"users": {name: {"role", "pw": {alg, params, salt,
                      hash}, "created", "changed"}}, "matrix": {...},
                      "idle_signout_min": 0,
                      "signout_after_last_show_min": 10}
Sessions live in memory: an engine restart signs everyone out, which
changes nothing on the rig.

SIGN OUT AFTER THE LAST SHOW (Jeff, 2026-10-26): when tonight's last show
has ended (nothing still to come, nothing live or held, no Abort
standing, nothing going on at the rig), and `signout_after_last_show_min`
minutes have passed (default 10; 0 is off), every session signed in
before the show ended is signed out, once for that night. Someone who
signs in after the show ended is left signed in. Without a show on the
schedule that night it never happens. The engine says when the last show
ended (`last_show`, remote.Remote.last_show_over); this module only counts
the minutes. It is checked on every session read and on its own beat
(start(), every LAST_SHOW_BEAT_S), so it happens on time with no page
open.
"""
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import time

USERS_FILE = "ltcplay_users.json"
# Written beside it once users exist on this machine: a users file that is
# then missing was deleted or lost, never "not set up yet", and locks the
# controls instead of opening them (review of PR #143, 2).
USERS_MARK = "ltcplay_users.made"
COOKIE = "ltcplay_user"
ROLES = ("admin", "operator", "viewer")
SIGNED_OUT = "signed_out"
COLUMNS = ROLES + (SIGNED_OUT,)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .'_-]{0,39}$")
# Never an account's name. With the engine's own names (announce.py's
# ENGINE_NAMES: what the scheduler and the alert hold act as), so no real
# person's press can read as the engine's, and refused (Jeff, 2026-10-30).
RESERVED = ("unsigned", "system", "signed out", "nobody", "deck", "engine",
            "scheduler", "ltc player", "ltcplay")
# Who a press is from when nobody is signed in (the journal, the audit).
UNSIGNED = "unsigned"
PW_MIN, PW_MAX = 6, 128
USERS_MAX = 50

# The slow hash. scrypt (16 MiB, about 50 ms) where the Python has it, the
# old PinStore's PBKDF2-SHA256 where it does not.
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
PBKDF2_ITERATIONS = 600_000

# Wrong passwords: FREE_TRIES cost nothing; after that each one locks the
# name and the device for LOCK_BASE_S * 2**(extra - 1), at most LOCK_MAX_S.
# A right password clears the name's count. Never permanent.
FREE_TRIES = 5
LOCK_BASE_S = 30.0
LOCK_MAX_S = 300.0

# The rows of the role matrix that can be restricted. Everything else a
# screen does is either safety (never restricted) or harmless (reading,
# opening a folder, checking a file).
PERMS = (
    ("show", "Start now, Resume, Reset, Run a show file, rehearsal "
             "scrubbing"),
    ("arm", "Arm flames and lasers: hold to arm, the Stream Deck's arm "
            "keys, a purge (momentary or timed), the Fireball page's "
            "channels, the wave flamer "
            "cleaning, arming a laser, arming a hazer, the hazer levels"),
    ("unlock", "Unlock a machine, turn a laser test on"),
    ("checklist", "Tick the Morning and Shutdown checklists"),
    ("show_files", "Show files and setup: the show folder, the schedule, "
                   "reload, the machine layout"),
    ("settings", "Settings: the show network, arming from a screen, live "
                 "flame programming, moving Programming playback from an "
                 "iPad while flames follow it, the timecode input, the "
                 "announcement output, the audit upload, idle sign out, "
                 "sign out after the last show"),
    ("users", "Users: add, remove, reset passwords, set roles, clear "
              "sign-in lockouts"),
)
PERM_KEYS = tuple(p for p, _ in PERMS)
SAFETY_ROWS = ("Abort, Hold, Stop, Disarm (one group or every group, "
               "flames or lasers), "
               "letting go of a hold, stopping or clearing a purge, "
               "locking a machine, a laser test off, live flame "
               "programming off, "
               "a hazer disarm, Hazers off, Acknowledge, signing "
               "in and out: always, for everyone, signed in or not")

# The defaults (Jeff decides; USERS.md). Signed out is view only (Jeff's
# field report, 2026-10-28): it may do nothing restricted, and FIXED keeps
# it that way.
DEFAULT_MATRIX = {
    "admin": {p: True for p in PERM_KEYS},
    "operator": {"show": True, "arm": True, "unlock": True,
                 "checklist": True, "show_files": True, "settings": False,
                 "users": False},
    "viewer": {p: False for p in PERM_KEYS},
    SIGNED_OUT: {p: False for p in PERM_KEYS},
}
# The people LTC Player starts with (Jeff, 2026-10-20). Written once, at
# first run, with NO password: each chooses their own at their first
# sign-in. No password is ever shipped in the repo.
SEED_USERS = (("Jeff", "admin"), ("Andy", "admin"), ("Dallen", "operator"),
              ("Dave", "operator"), ("Jared", "operator"))
# An admin can always manage users, so nobody can lock every admin out.
# And signed out may do nothing restricted (2026-10-28): view only.
FIXED = {("admin", "users"): True}
FIXED.update({(SIGNED_OUT, p): False for p in PERM_KEYS})
# Sign everyone out this many minutes after the night's last show ended
# (Jeff, 2026-10-26): on by default; 0 is off.
LAST_SHOW_SIGNOUT_DEFAULT_MIN = 10
LAST_SHOW_BEAT_S = 5.0     # the sign-out's own beat (Users.start)
# A users file that is there but cannot be opened is tried again on every
# read; while it keeps failing for less than this the last good read
# stands, then it counts as broken (locked) until a read works.
READ_GRACE_S = 10.0


# ---------------------------------------------------- which press is which --

# The remote page's POST routes (remote.py) and the old page's (web.py).
# "safety": never restricted. None: harmless, never restricted.
REMOTE_ROUTES = {
    "abort": "safety", "hold": "safety", "disarm-all": "safety",
    "stop": "safety",
    "group-disarm": "safety", "arm-release": "safety", "alert": "safety",
    "purge-stop": "safety", "purge-clear": "safety",
    "signin": "safety", "signout": "safety",
    # Operator announcements (remote.ANNOUNCE_ROUTES): a Play is a show
    # press; Stop ends only the announcement's audio, so it only takes
    # something away and is never restricted.
    "announce-stop": "safety", "announce-play": "show",
    "start-now": "show", "resume": "show", "reset": "show",
    "play-from": "show", "jump": "show", "pause": "show",
    "continue": "show", "mark-a": "show", "mark-b": "show",
    "loop": "show", "follow": "show",
    "arm-hold": "arm",
    "network": "settings", "screen-arming": "settings",
    # Check the rig (preroll.py): an admin's, between shows.
    "preroll-check": "settings",
    # Continue anyway after a failed rig check: a run press (Show row).
    "preroll-continue": "show",
    # The announcement output picker (remote.announce_output).
    "announce-output": "settings", "announce-test-tone": "settings",
    # The default interface (interface 2.0, 2026-10-28): which page a
    # screen that has not chosen gets. Setup, admin by default.
    "ui-default": "settings",
    "live-flame": "settings",
    "audit-settings": "settings",
    "audit-upload-now": "settings", "idle-signout": "settings",
    "last-show-signout": "settings",
    "users": "users", "role-matrix": "users", "clear-lockouts": "users",
    "schedule-file": "show_files", "show-folder": "show_files",
    # Reload engine (2026-10-28): a restart of the engine between shows,
    # a settings press (the Settings row, admin by default).
    "reload-engine": "settings",
    "layout": "show_files",
    "checklist-tick": "checklist", "checklist-note": "checklist",
    "morning-open": None, "checklist-verify": None,
    "checklist-tally-retry": None, "open-folder": "show_files",
    "screen": None,
    # Choosing the show in use and opening a folder in the file manager
    # (the settings folder holds the users file) are setup (2026-10-28).
    "show-select": "show_files", "show-file-new": "show_files",
    "audit-verify": None, "my-password": None,
    # Open BEYOND (windowfront.py) only moves a window on the show PC.
    "open-beyond": None,
    # The Fuel page (fuel.py): not a show control, so no matrix row; the
    # route itself needs a signed-in operator or admin (an undo, an admin).
    "fuel-refill": None, "fuel-undo": None,
    # The Fireball page (fireball.py): All off only takes risk away.
    "fireball-off": "safety",
    # Wave flamer cleaning (cleaning.py): Stop only takes risk away.
    "cleaning-stop": "safety",
    # The hazers (hazers.py): Hazers off only takes something away; a
    # hazer ON is an arm press, OFF safety (classify).
    "hazers-off": "safety", "hazer": "arm", "hazer-level": "arm",
    # These depend on the body: see classify().
    "fireball-beat": "arm", "cleaning-beat": "arm",
    "purge-select": "arm", "purge-fire": "arm", "laser-test": "unlock",
    # The timed purge's start hold: the purge's own row (Jeff,
    # 2026-10-29); a release only lets go (classify).
    "purge-timed": "arm",
    "laser-arm": "arm",
    "lock": "unlock",
}
WEB_ROUTES = {
    "/api/stop": "safety",
    "/api/start": "show", "/api/go": "show", "/api/skip": "show",
    "/api/release": "show", "/api/override": "show",
    "/api/reload": "show_files", "/api/showdir": "show_files",
    "/api/autoreload": "show_files", "/api/trigger": "show_files",
    "/api/input": "settings", "/api/reinput": "settings",
    "/api/find": None, "/api/check": None,
}


def purge_fire_wanted(body):
    """True only for a purge-fire body that asks to START or CARRY ON a fire
    press: "on" is the JSON word true and nothing else. Every other body
    ("on" false, missing, null, 0, 1, "true", "false", a body that is not
    an object) is a release, which can only stop a press: a request that
    means release must never be able to start a fire (found on PR #82: a
    body with "on" false fired 240 on slot 411 with purge arming off). The
    engine (remote.py, morning.py) and classify() below both decide by
    this one test, so a press is only ever "safety" when the engine will
    only stop with it."""
    return isinstance(body, dict) and body.get("on") is True


def fireball_on_wanted(body):
    """True only for a fireball-beat body that asks for any Fireball
    channel ON: a "controls" object with at least one value that is the
    JSON word true. Every other body only turns things off, which the
    engine (fireball.py, controls_on) never refuses."""
    c = body.get("controls") if isinstance(body, dict) else None
    return isinstance(c, dict) and any(v is True for v in c.values())


def classify(route, body=None):
    """"safety", a key of PERMS, or None, for one POST. A press that only
    takes risk away is "safety" whatever its route: a purge selection
    turned off, a purge fire release, a laser test off, a lock put on, the
    blackout look. Classified by what the engine will DO with the body,
    never by whether a field is present."""
    raw = body
    body = body if isinstance(body, dict) else {}
    route = str(route or "")
    if route.startswith("/api/remote/"):
        name = route[len("/api/remote/"):]
        if name == "purge-fire":
            # The engine starts or carries on a press only for
            # purge_fire_wanted(); everything else it turns into a stop.
            return "arm" if purge_fire_wanted(raw) else "safety"
        if name == "purge-select" and \
                body.get("on") is False:
            return "safety"
        if name == "fireball-beat":
            # A beat with every channel off only turns things off.
            return "arm" if fireball_on_wanted(raw) else "safety"
        if name == "purge-timed":
            # Only "hold" the JSON word true can start a timed purge
            # (morning.Purge.timed_beat); every other body is a release.
            return "safety" if body.get("hold") is not True else "arm"
        if name == "cleaning-beat":
            # Only "hold" the JSON word true runs it (cleaning.py); every
            # other body is a release, which only stops it.
            return "arm" if body.get("hold") is True else "safety"
        if name == "laser-test" and body.get("on") is not True:
            return "safety"
        if name == "hazer" and body.get("on") is not True:
            # A hazer disarm (anything but the JSON word true disarms at
            # the engine, remote.hazer_post) only takes something away.
            return "safety"
        if name == "screen-arming" and body.get("on") is False:
            # Switching arming from a screen OFF only takes risk away.
            return "safety"
        if name == "live-flame" and body.get("on") is not True:
            # Live flame programming OFF (anything but the JSON word true
            # is off at the engine, remote.live_flame_post) only takes
            # risk away: anyone, any surface, signed out.
            return "safety"
        if name == "laser-arm" and body.get("on") is not True:
            # morning.py arms only for "on" the JSON word true; every other
            # body disarms (Jeff, 2026-10-22).
            return "safety"
        if name == "lock" and body.get("on") is not False:
            return "safety" if body.get("kind") != "rewrite" else "settings"
        return REMOTE_ROUTES.get(name)
    if route == "/api/override" and body.get("look") == "blackout":
        return "safety"
    if route.startswith("/api/schedule"):
        # Editing tonight's list is setup; choosing the deck's operator
        # and saving an incident are show presses; clearing the operator
        # only takes something away. The deck's journal lines write a line
        # and nothing else (2026-10-28).
        if route == "/api/schedule/tonight":
            return "show_files"
        if route == "/api/schedule/operator":
            return "show" if str(body.get("who") or "").strip() else "safety"
        if route == "/api/schedule/incident":
            return "show"
        return None
    if route.startswith("/api/announce"):
        return "show"
    return WEB_ROUTES.get(route)


def normalise_matrix(doc):
    """The matrix as stored, merged over the defaults, with the fixed cells
    put back. Unknown roles or rows are dropped, never guessed at."""
    out = {c: dict(DEFAULT_MATRIX[c]) for c in COLUMNS}
    if isinstance(doc, dict):
        for c, row in doc.items():
            if c in out and isinstance(row, dict):
                for p, v in row.items():
                    if p in PERM_KEYS and isinstance(v, bool):
                        out[c][p] = v
    for (c, p), v in FIXED.items():
        out[c][p] = v
    return out


def allowed(matrix, role, perm):
    """May a press of kind `perm` go ahead for `role` (None: nobody signed
    in)? Safety and harmless presses always may; nobody signed in and a
    role this does not know may do nothing restricted (2026-10-28: signed
    out is view only, whatever a stored matrix says)."""
    if perm == "safety" or perm is None:
        return True
    if role is None or role not in ROLES:
        return False
    col = role
    row = (matrix or DEFAULT_MATRIX).get(col) or {}
    return row.get(perm) is True


def perm_label(perm):
    return dict(PERMS).get(perm, perm)


# ------------------------------------------------------------- passwords --

def _have_scrypt():
    return hasattr(hashlib, "scrypt")


def hash_password(password, salt=None, alg=None, params=None):
    """{"alg", params..., "salt", "hash"} for a password. Never the
    password itself."""
    salt = salt or secrets.token_bytes(16)
    alg = alg or ("scrypt" if _have_scrypt() else "pbkdf2_sha256")
    pw = str(password).encode("utf-8")
    if alg == "scrypt":
        p = dict(SCRYPT, **(params or {}))
        h = hashlib.scrypt(pw, salt=salt, n=p["n"], r=p["r"], p=p["p"],
                           maxmem=256 * 1024 * 1024, dklen=32)
        return {"alg": "scrypt", "n": p["n"], "r": p["r"], "p": p["p"],
                "salt": salt.hex(), "hash": h.hex()}
    it = int((params or {}).get("iterations", PBKDF2_ITERATIONS))
    h = hashlib.pbkdf2_hmac("sha256", pw, salt, it)
    return {"alg": "pbkdf2_sha256", "iterations": it, "salt": salt.hex(),
            "hash": h.hex()}


def check_password(password, rec):
    """True only for the right password. A broken record is never True."""
    try:
        salt = bytes.fromhex(rec["salt"])
        alg = rec["alg"]
        params = ({"n": int(rec["n"]), "r": int(rec["r"]), "p": int(rec["p"])}
                  if alg == "scrypt" else
                  {"iterations": int(rec["iterations"])})
        if alg not in ("scrypt", "pbkdf2_sha256"):
            return False
        got = hash_password(password, salt=salt, alg=alg, params=params)
        return hmac.compare_digest(got["hash"], str(rec["hash"]))
    except Exception:                        # noqa: BLE001
        return False


def check_name(name):
    name = " ".join(str(name or "").split())
    if not NAME_RE.match(name) or name.lower() in RESERVED:
        raise ValueError("A name is 1 to 40 letters, digits, spaces, dots, "
                         "dashes or apostrophes, and not unsigned, system, "
                         "signed out, deck, nobody, engine, scheduler, LTC "
                         "Player or ltcplay.")
    return name


def check_new_password(pw):
    pw = str(pw or "")
    if not PW_MIN <= len(pw) <= PW_MAX:
        raise ValueError(f"A password is {PW_MIN} to {PW_MAX} characters.")
    return pw


def _write_private(path, doc):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# ----------------------------------------------------------- the service --

class Users:
    """The user list, the role matrix, sign-in and sessions. Every method
    is safe to call from the web server's threads. `hash_params` is for
    the selftest (a cheaper scrypt); `audit(action, user, result, reason,
    target, source)` is told about every sign-in, sign-out and change."""

    def __init__(self, folder, clock=time.monotonic, wall=time.time,
                 hash_params=None, audit=None):
        self.folder = folder
        self.path = os.path.join(folder, USERS_FILE)
        self.mark = os.path.join(folder, USERS_MARK)
        self._clock = clock
        self._wall = wall
        self._hash_params = hash_params
        self._audit = audit
        # Re-entrant: a session read drops idle sessions under the same
        # lock, and that reads the settings.
        self._lock = threading.RLock()
        self._check_lock = threading.Lock()   # one password check at a time
        self._sessions = {}       # token -> session dict
        # Each sign-in's place in line: which is the latest is never left
        # to the clock, whose tick is about 16 ms on Windows, so two
        # sign-ins in one tick are still told apart.
        self._signin_seq = 0
        self._fails = {}          # key -> [count, locked_until]
        self._doc = None
        self._mtime = None
        self._good = None         # the last users file read whole and good
        self._fail_since = None   # when the opens started failing
        # Why the file cannot be used: "open" (there, but could not be
        # opened), "invalid" (read whole, not a users file) or "missing"
        # (gone after users were set up); "" when it can.
        self.error_kind = ""
        self.error = ""
        # () -> None, or {"night": the schedule's night, "ended": wall
        # clock seconds the last show of it ended}, only while that last
        # show is over and nothing is live, held or aborted. Set by the
        # engine (remote.Remote.last_show_over). None: never.
        self.last_show = None
        self._last_show_done = None   # the night already signed out
        self._beat_stop = threading.Event()
        self._beat = None

    # -- the file -------------------------------------------------------
    def _load(self):
        """The file as a dict, read again when it changed on disk."""
        try:
            m = os.path.getmtime(self.path)
        except OSError:
            if os.path.exists(self.mark):
                # Users were set up here and the file is gone: locked, the
                # same as a broken file, never back to "nobody set up".
                self._doc, self._mtime = {"users": {}, "broken": True}, None
                self.error = (f"{USERS_FILE} is missing, but users were set "
                              f"up on this machine")
                self.error_kind = "missing"
                return self._doc
            self._doc, self._mtime, self.error = {"users": {}}, None, ""
            self.error_kind = ""
            return self._doc
        if self._doc is not None and m == self._mtime:
            self._fail_since = None
            if not self._doc.get("broken"):
                self.error, self.error_kind = "", ""
            return self._doc
        try:
            # Bytes only here: a file that is not text (not UTF-8) is read
            # whole and found invalid below, never a failed open (fourth
            # review of PR #143).
            with open(self.path, "rb") as fh:
                raw = fh.read()
        except OSError as e:
            # The file is there but could not be opened just now (an
            # antivirus or indexer holding it): never remembered, so the
            # next read tries again (re-review of PR #143, 2). Meanwhile
            # the last good read stands; with none yet, or once it has
            # failed for READ_GRACE_S, nobody may do anything restricted,
            # and nobody signed in is signed out by it.
            now = self._clock()
            if self._fail_since is None:
                self._fail_since = now
            if self._good is not None and \
                    now - self._fail_since < READ_GRACE_S:
                return self._good
            self.error = f"{USERS_FILE} could not be opened: {e}"
            self.error_kind = "open"
            return {"users": {}, "broken": True}
        self._fail_since = None
        try:
            doc = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(doc, dict) or not isinstance(
                    doc.get("users", {}), dict):
                raise ValueError("not a users object")
            doc.setdefault("users", {})
            self.error, self.error_kind = "", ""
            self._good = doc
            if doc["users"] and not os.path.exists(self.mark):
                self._write_mark()
        except ValueError as e:
            # Read whole and not a users file: broken until it changes.
            # Nobody can sign in, and every restricted press is refused.
            doc = {"users": {}, "broken": True}
            self.error = f"{USERS_FILE} could not be read: {e}"
            self.error_kind = "invalid"
        self._doc, self._mtime = doc, m
        return doc

    def _save(self, doc):
        os.makedirs(self.folder, exist_ok=True)
        clean = {k: v for k, v in doc.items() if k != "broken"}
        _write_private(self.path, clean)
        if clean.get("users"):
            self._write_mark()
        # What was just written is the last good read: a failed open right
        # after never brings back the copy from before this change (third
        # review of PR #143, 2).
        self._good = json.loads(json.dumps(clean))
        self._doc = None

    def _write_mark(self):
        """USERS_MARK, once: users exist on this machine. Never raises."""
        try:
            with open(self.mark, "w", encoding="utf-8") as fh:
                fh.write("Users were set up on this machine. While this file "
                         "is here a missing users file locks every control "
                         "but the stops and disarms.\n")
        except OSError:
            pass

    def _note(self, action, user, result="accepted", reason="", target="",
              source=""):
        if self._audit is None:
            return
        try:
            self._audit(action=action, user=user, result=result,
                        reason=reason, target=target, source=source)
        except Exception:                    # noqa: BLE001
            pass

    # -- reading ----------------------------------------------------------
    def active(self):
        """True once at least one user exists: only then do roles apply.
        Also True while the users file is broken, or missing after users
        were set up here: then nobody can sign in, so every restricted
        press is refused (fail closed; review of PR #143, 2). Only a
        machine never set up (no file, no USERS_MARK) is not active."""
        with self._lock:
            doc = self._load()
            return bool(doc.get("users")) or bool(doc.get("broken"))

    def broken(self):
        """The sentence saying why the users file cannot be used, or ""."""
        with self._lock:
            doc = self._load()
            return self.error if doc.get("broken") else ""

    def matrix(self):
        with self._lock:
            return normalise_matrix(self._load().get("matrix"))

    def idle_minutes(self):
        with self._lock:
            v = self._load().get("idle_signout_min", 0)
        return v if isinstance(v, int) and not isinstance(v, bool) and \
            0 <= v <= 24 * 60 else 0

    def last_show_minutes(self):
        """Minutes after the night's last show ended that everyone is
        signed out; 0 is off. Not in the file: the default (10)."""
        with self._lock:
            v = self._load().get("signout_after_last_show_min",
                                 LAST_SHOW_SIGNOUT_DEFAULT_MIN)
        return v if isinstance(v, int) and not isinstance(v, bool) and \
            0 <= v <= 24 * 60 else LAST_SHOW_SIGNOUT_DEFAULT_MIN

    def role_of(self, name):
        with self._lock:
            u = self._load().get("users", {}).get(name)
        return u.get("role") if isinstance(u, dict) and \
            u.get("role") in ROLES else None

    def names(self):
        with self._lock:
            return sorted(self._load().get("users", {}), key=str.lower)

    def listing(self):
        """For the Users screen: names, roles, whether locked out. Never a
        hash or a salt."""
        with self._lock:
            users = self._load().get("users", {})
            now = self._clock()
            out = []
            for n in sorted(users, key=str.lower):
                u = users[n] if isinstance(users[n], dict) else {}
                f = self._fails.get(("name", n.lower()))
                out.append({"name": n, "role": u.get("role", ""),
                            "password_set": u.get("pw") is not None,
                            "temporary": bool(u.get("must_change")),
                            "created": u.get("created", ""),
                            "changed": u.get("changed", ""),
                            "locked_s": (round(f[1] - now) if f and
                                         f[1] > now else 0)})
            return out

    def may(self, role, perm):
        """allowed() with the live matrix; before any user exists every
        press may (the feature is not switched on yet)."""
        if perm == "safety" or perm is None:
            return True
        if not self.active():
            return True
        return allowed(self.matrix(), role, perm)

    # -- sessions -----------------------------------------------------------
    def _drop_idle(self, now):
        mins = self.idle_minutes()
        if not mins:
            return
        gone = [(t, s) for t, s in self._sessions.items()
                if now - s["seen"] > mins * 60]
        for t, s in gone:
            del self._sessions[t]
            self._note("sign-out", s["name"], reason=f"idle for {mins} min",
                       source=s["source"])

    def _drop_after_last_show(self):
        """Every session signed in before the night's last show ended plus
        the grace, signed out once for that night. Asks the engine
        (`last_show`) and does nothing at all while it says no, or when
        the setting is 0."""
        mins = self.last_show_minutes()
        fn = self.last_show
        if not mins or fn is None:
            return
        try:
            info = fn()
            if not info:
                return
            night = str(info["night"])
            due = float(info["ended"]) + mins * 60
        except Exception:                        # noqa: BLE001
            return
        if night == self._last_show_done or self._wall() < due:
            return
        self._last_show_done = night
        ended = due - mins * 60
        # Only sessions signed in before the show ended: a sign-in after
        # it is never signed out by this (review of PR #125, P2-5).
        gone = [(t, s) for t, s in self._sessions.items()
                if s.get("wall_at", 0.0) < ended]
        for t, s in gone:
            del self._sessions[t]
            self._note("sign-out", s["name"],
                       reason=f"after the last show of the night ({mins} "
                              f"min after it ended)",
                       source=s["source"])

    def after_last_show_tick(self):
        """The same check, from this module's own beat (start()). Never
        raises."""
        try:
            with self._lock:
                self._drop_after_last_show()
        except Exception:                        # noqa: BLE001
            pass

    def start(self, every_s=LAST_SHOW_BEAT_S):
        """The sign-out after the last show on a beat of its own, so it
        happens on time with no page open (review of PR #125, P2-6).
        web.serve starts it and stops it with the engine."""
        if self._beat is not None:
            return self
        self._beat_stop.clear()

        def loop():
            while not self._beat_stop.wait(every_s):
                self.after_last_show_tick()
        self._beat = threading.Thread(target=loop, daemon=True,
                                      name="ltcplay-users-beat")
        self._beat.start()
        return self

    def stop(self):
        self._beat_stop.set()
        t, self._beat = self._beat, None
        if t is not None:
            t.join(timeout=2.0)

    def session(self, token):
        """The live session for a cookie token, or None. A session whose
        user was removed is gone."""
        if not token:
            return None
        with self._lock:
            now = self._clock()
            self._drop_idle(now)
            self._drop_after_last_show()
            s = self._sessions.get(token)
            if s is None:
                return None
            doc = self._load()
            if doc.get("broken"):
                # Locked while the file cannot be used, but nobody is
                # signed out by it: a good read brings them back.
                return None
            u = doc.get("users", {}).get(s["name"])
            if not isinstance(u, dict):
                del self._sessions[token]
                return None
            s = dict(s, role=u.get("role"))
            return s

    def touch(self, token):
        """A command from this session: it is not idle."""
        with self._lock:
            s = self._sessions.get(token)
            if s is not None:
                s["seen"] = self._clock()

    def machine_user(self):
        """{"name", "role"} of the user signed in last on this machine
        itself (the rack screen), or None. The Stream Deck acts as them."""
        with self._lock:
            now = self._clock()
            self._drop_idle(now)
            self._drop_after_last_show()
            best = None
            users = self._load().get("users", {})
            for s in self._sessions.values():
                if s["local"] and isinstance(users.get(s["name"]), dict) and \
                        (best is None or s.get("seq", 0) > best.get("seq", 0)):
                    best = s
            if best is None:
                return None
            return {"name": best["name"],
                    "role": users[best["name"]].get("role")}

    def _locked(self, keys, now):
        worst = 0.0
        for k in keys:
            f = self._fails.get(k)
            if f and f[1] > now:
                worst = max(worst, f[1] - now)
        return worst

    def _failed(self, keys, now):
        for k in keys:
            f = self._fails.setdefault(k, [0, 0.0])
            f[0] += 1
            extra = f[0] - FREE_TRIES
            if extra > 0:
                f[1] = now + min(LOCK_MAX_S, LOCK_BASE_S * 2 ** (extra - 1))

    def sign_in(self, name, password, local, source, new_password=None):
        """(ok, sentence, token or None, needs_new). Never says whether the
        name exists; never logs a password.

        A user with no password yet (seeded at first run, or cleared by an
        admin) signs in by choosing one: `new_password`. A user whose
        password is temporary (set by an admin) signs in with it AND a new
        one of their own. Until then needs_new is True and nobody is signed
        in."""
        name = " ".join(str(name or "").split())
        now = self._clock()
        keys = [("name", name.lower()), ("from", str(source))]
        with self._lock:
            wait = self._locked(keys, now)
        if wait:
            why = (f"Too many wrong passwords. Try again in "
                   f"{math.ceil(wait)} s, or ask an admin to clear it.")
            self._note("sign-in", name or "unsigned", "refused", why,
                       source=source)
            return False, why, None, False
        with self._check_lock:
            with self._lock:
                users = self._load().get("users", {})
                real = {n.lower(): n for n in users}.get(name.lower())
                u = users.get(real) if real else None
                u = u if isinstance(u, dict) else None
                rec = u.get("pw") if u else None
                must = bool(u and u.get("must_change"))
            first = u is not None and rec is None
            if first:
                ok = True                  # nothing to check yet
            elif rec is None:
                # The same time as a real check, so a wrong name is not
                # quicker than a wrong password.
                check_password(str(password), hash_password(
                    "x", salt=b"\0" * 16, params=self._hash_params))
                ok = False
            else:
                ok = check_password(str(password), rec)
        if not ok:
            with self._lock:
                self._failed(keys, now)
            why = "That name and password do not match."
            self._note("sign-in", name or "unsigned", "refused", why,
                       source=source)
            return False, why, None, False
        if first and not local:
            # A name with no password yet is claimed on the show machine
            # itself, never from the network (review of PR #143, 2).
            why = (f"{real} has no password yet. Choose it on the show "
                   f"machine's rack screen first.")
            self._note("sign-in", real, "refused", why, source=source)
            return False, why, None, False
        if first or must:
            if not new_password:
                why = (f"Choose your own password, {real}, to finish "
                       f"signing in." if first else
                       f"That was a temporary password, {real}. Choose your "
                       f"own to finish signing in.")
                return False, why, None, True
            try:
                pw = check_new_password(new_password)
            except ValueError as e:
                return False, str(e), None, True
            if not first and str(password) == pw:
                return False, ("The new password has to be different from "
                               "the temporary one."), None, True
            try:
                self._set_pw(real, pw, temporary=False)
            except (ValueError, OSError) as e:
                return False, f"The password could not be saved: {e}", \
                    None, True
            self._note("password-set", real, target=real, source=source,
                       reason="first sign-in" if first else
                       "replaced a temporary password")
        with self._lock:
            for k in keys:
                self._fails.pop(k, None)
            token = secrets.token_urlsafe(24)
            self._signin_seq += 1
            self._sessions[token] = {"name": real, "local": bool(local),
                                     "seq": self._signin_seq,
                                     "source": str(source), "at": now,
                                     "seen": now, "wall_at": self._wall()}
        self._note("sign-in", real, source=source)
        return True, f"Signed in as {real}.", token, False

    def sign_out(self, token, source="", local=False):
        """End this page's session. On this machine itself (`local`, or a
        session made here) every session made on this machine ends too, so
        the rack screen and the Stream Deck are signed out together and a
        sign-out there always leaves the machine view only, even when the
        page lost its cookie (Jeff's field report, 2026-10-28: a sign-out
        that changed nothing). True when anything ended."""
        with self._lock:
            s = self._sessions.pop(token, None) if token else None
            gone = []
            if local or (s is not None and s["local"]):
                gone = [t for t, x in self._sessions.items() if x["local"]]
                gone = [(t, self._sessions.pop(t)) for t in gone]
        self._note("sign-out", s["name"] if s else UNSIGNED,
                   source=source)
        for _t, x in gone:
            self._note("sign-out", x["name"], source=source,
                       reason="signed out on this machine")
        return s is not None or bool(gone)

    # -- changes (the Users screen) -----------------------------------------
    def _change(self, fn):
        with self._lock:
            loaded = self._load()
            if self._fail_since is not None:
                # Only ever changed from a fresh read: a change made from
                # the last good copy while the file cannot be opened could
                # undo a change made since.
                raise ValueError(f"{USERS_FILE} cannot be opened just now "
                                 f"(another program may be holding it). "
                                 f"Nothing was changed; try again in a "
                                 f"moment.")
            doc = json.loads(json.dumps(loaded))
            if doc.get("broken"):
                raise ValueError(f"{self.error}. Nothing was changed. To "
                                 f"start over, close LTC Player, remove "
                                 f"{USERS_FILE} and {USERS_MARK} from "
                                 f"{self.folder} and start it again.")
            doc.setdefault("users", {})
            said = fn(doc)
            self._save(doc)
            return said

    def _stamp(self):
        return time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(self._wall()))

    def add(self, name, role, password):
        name = check_name(name)
        if role not in ROLES:
            raise ValueError(f"A role is {', '.join(ROLES)}.")
        pw = check_new_password(password)

        def fn(doc):
            users = doc["users"]
            if name.lower() in {n.lower() for n in users}:
                raise ValueError(f"{name} is already a user.")
            if len(users) >= USERS_MAX:
                raise ValueError(f"There can be at most {USERS_MAX} users.")
            if not users and role != "admin":
                raise ValueError("The first user has to be an admin.")
            users[name] = {"role": role, "created": self._stamp(),
                           "changed": self._stamp(), "must_change": True,
                           "pw": hash_password(pw, params=self._hash_params)}
            return (f"{name} added as {role}, with a temporary password "
                    f"they change when they first sign in.")
        return self._change(fn)

    def _admins(self, users, without=None):
        return [n for n, u in users.items() if n != without and
                isinstance(u, dict) and u.get("role") == "admin"]

    def _real(self, users, name):
        real = {n.lower(): n for n in users}.get(str(name or "").lower())
        if real is None:
            raise ValueError(f"{name} is not a user.")
        return real

    def remove(self, name):
        def fn(doc):
            users = doc["users"]
            real = self._real(users, name)
            if users[real].get("role") == "admin" and \
                    not self._admins(users, without=real):
                raise ValueError("That is the last admin. Make another admin "
                                 "first.")
            del users[real]
            return f"{real} removed."
        said = self._change(fn)
        self._end_sessions(name)
        return said

    def set_role(self, name, role):
        if role not in ROLES:
            raise ValueError(f"A role is {', '.join(ROLES)}.")

        def fn(doc):
            users = doc["users"]
            real = self._real(users, name)
            if users[real].get("role") == "admin" and role != "admin" and \
                    not self._admins(users, without=real):
                raise ValueError("That is the last admin. Make another admin "
                                 "first.")
            users[real]["role"] = role
            users[real]["changed"] = self._stamp()
            return f"{real} is now {role}."
        return self._change(fn)

    def _set_pw(self, name, pw, temporary):
        def fn(doc):
            users = doc["users"]
            real = self._real(users, name)
            users[real]["pw"] = (hash_password(pw, params=self._hash_params)
                                 if pw is not None else None)
            users[real]["must_change"] = bool(temporary)
            users[real]["changed"] = self._stamp()
            return real
        return self._change(fn)

    def set_password(self, name, password):
        """An admin's reset: a temporary password, changed by its user at
        their next sign-in."""
        pw = check_new_password(password)
        real = self._set_pw(name, pw, temporary=True)
        said = (f"The password for {real} was reset. It is temporary: "
                f"{real} chooses their own at the next sign-in.")
        self._end_sessions(real)
        with self._lock:
            self._fails.pop(("name", str(name).lower()), None)
        return said

    def change_own_password(self, name, old, new):
        with self._lock:
            rec = (self._load().get("users", {}).get(name) or {}).get("pw")
        if rec is None or not check_password(str(old), rec):
            raise ValueError("The current password is not right. Nothing "
                             "was changed.")
        pw = check_new_password(new)
        self._set_pw(name, pw, temporary=False)
        return "Your password was changed."

    def clear_password(self, name):
        """An admin's reset with no temporary password: the user chooses
        one at their next sign-in."""
        real = self._set_pw(name, None, temporary=False)
        self._end_sessions(real)
        return f"{real} chooses a new password at the next sign-in."

    def _end_sessions(self, name):
        with self._lock:
            for t in [t for t, s in self._sessions.items()
                      if s["name"].lower() == str(name).lower()]:
                del self._sessions[t]

    def seed(self, people):
        """First run only (no users file yet): the named people, each with
        no password; each chooses their own at their first sign-in. No
        password is ever shipped. [(name, role)]. True if it wrote."""
        if os.path.exists(self.path) or os.path.exists(self.mark):
            # Set up before: a lost file is never seeded again with names
            # that have no password, which anyone could then claim.
            return False

        def fn(doc):
            if doc["users"]:
                return False
            for n, r in people:
                n = check_name(n)
                if r not in ROLES:
                    raise ValueError(f"{r} is not a role")
                doc["users"][n] = {"role": r, "created": self._stamp(),
                                   "changed": self._stamp(), "pw": None,
                                   "must_change": False}
            if not self._admins(doc["users"]):
                raise ValueError("the first users need an admin")
            return True
        done = self._change(fn)
        if done:
            self._note("users-seeded", "system",
                       target=", ".join(f"{n} ({r})" for n, r in people))
        return done

    def set_matrix(self, changes):
        """{column: {perm: bool}}: only cells that may change do."""
        if not isinstance(changes, dict):
            raise ValueError("Send the matrix as an object.")

        def fn(doc):
            m = normalise_matrix(doc.get("matrix"))
            for c, row in changes.items():
                if c not in COLUMNS or not isinstance(row, dict):
                    raise ValueError(f"{c} is not a column of the matrix.")
                for p, v in row.items():
                    if p not in PERM_KEYS or not isinstance(v, bool):
                        raise ValueError(f"{p} is not a row of the matrix.")
                    if (c, p) in FIXED and v != FIXED[(c, p)]:
                        raise ValueError(
                            "An admin can always manage users."
                            if c == "admin" else
                            "Signed out is view only: nobody signed in "
                            "may do anything but Stop, Hold, Abort, "
                            "Acknowledge and disarm.")
                    m[c][p] = v
            doc["matrix"] = m
            return "The role matrix was saved."
        return self._change(fn)

    def set_idle(self, minutes):
        if isinstance(minutes, bool) or not isinstance(minutes, int) or \
                not 0 <= minutes <= 24 * 60:
            raise ValueError("Idle sign out is 0 (off) to 1440 minutes.")

        def fn(doc):
            doc["idle_signout_min"] = minutes
            return ("Idle sign out is off." if not minutes else
                    f"A screen idle for {minutes} min is signed out.")
        return self._change(fn)

    def set_last_show(self, minutes):
        if isinstance(minutes, bool) or not isinstance(minutes, int) or \
                not 0 <= minutes <= 24 * 60:
            raise ValueError("Sign out after the last show is 0 (off) to "
                             "1440 minutes.")

        def fn(doc):
            doc["signout_after_last_show_min"] = minutes
            return ("Sign out after the last show is off." if not minutes
                    else f"Everyone is signed out {minutes} min after the "
                         f"last show of the night ends.")
        return self._change(fn)

    def clear_lockouts(self):
        with self._lock:
            n = len(self._fails)
            self._fails.clear()
        return f"Sign-in lockouts cleared ({n})."

    def view(self):
        """The Users screen's read: users, matrix, settings. No secrets."""
        return {"active": self.active(), "users": self.listing(),
                "roles": list(ROLES), "columns": list(COLUMNS),
                "perms": [{"key": k, "label": l} for k, l in PERMS],
                "safety": SAFETY_ROWS, "matrix": self.matrix(),
                "fixed": [list(k) for k in FIXED],
                "idle_signout_min": self.idle_minutes(),
                "signout_after_last_show_min": self.last_show_minutes(),
                "error": self.error}
