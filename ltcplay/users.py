"""Named users with passwords, sign-in, and who may do what (Jeff,
2026-10-20). USERS.md says it in plain words.

WHAT THIS IS FOR: knowing who pressed what (audit.py writes it down). It is
not what keeps the show safe, and it never stands in the way of anything
that makes the rig safer.

THE SAFETY RULE (not negotiable): Stop, Hold, every disarm, letting
go of a hold, stopping or clearing a purge, locking a machine out, turning
a laser test off, disarming a laser (one, a group or every laser),
Acknowledge and signing in or out work from every
surface whether anyone is signed in or not, whatever the role, and are
logged as "unsigned" when nobody is. classify() names them "safety" and
allowed() answers True for "safety" before it looks at anything else.
Nothing in flamesafe is touched by this module: its sole writer, the
link key and hold-to-arm stay exactly as they were.

SIGNED OUT IS VIEW ONLY (Jeff's field report, 2026-10-28, Tier A): once
any user exists, every press that is not safety needs a signed-in user
whose role may, on every surface: the rack screen on this machine, an
iPad, the old page and the Stream Deck (which acts as whoever is signed in
on this machine's rack screen). Being on the machine itself (loopback) is
NOT a sign-in. Signed out, a screen sees status, the schedule and the
alerts, and may only do the safety presses above: Stop, Hold,
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
has ended (nothing still to come, nothing live or held, nothing going on
at the rig), and `signout_after_last_show_min`
minutes have passed (default 10; 0 is off), every session signed in
before the show ended is signed out, once for that night. Someone who
signs in after the show ended is left signed in. Without a show on the
schedule that night it never happens. The engine says when the last show
ended (`last_show`, remote.Remote.last_show_over); this module only counts
the minutes. It is checked on every session read and on its own beat
(start(), every LAST_SHOW_BEAT_S), so it happens on time with no page
open.
"""
import contextlib
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
# The SuperUser's own files beside the users file (2026-11-13, review of
# #251): the engine's key that fingerprints every SuperUser record and the
# set of SuperUsers (Users._trust: one this build did not write is an admin
# until a SuperUser confirms it), the cross-process lock every change takes
# (the engine and the local command line), and the local command line's
# signal to the running engine after a SuperUser's password reset. "key"
# in the name keeps it out of the diagnostics bundle (diagnostics.py).
USERS_KEY = "ltcplay_users_key.json"
USERS_LOCK = "ltcplay_users.lock"
USERS_SIGNAL = "ltcplay_users_signal.json"
# How long the lock is waited for, and how old a signal may be.
LOCK_WAIT_S = 2.0
AUTH_FAIL_PAUSE_S = 1.0     # the local command line, after a wrong password
SIGNAL_MAX_AGE_S = 600.0
# A SuperUser's session ends after this long with no press, whatever the
# admin's idle and last show settings say (M3, review of #251).
SUPERUSER_IDLE_S = 15 * 60
# Written once, when the first SuperUser is made: from then on the "first
# SuperUser" path of the local command line is refused forever (verifi-
# cation review of #251, B1a).
SU_MARK = "ltcplay_superuser.made"
# The engine's last-written set of SuperUsers, a second copy beside the
# audit log (B1c): kept in memory too while the engine runs.
SU_SECOND = os.path.join("logs", "audit", "ltcplay_superusers.json")
# THE SUPERUSER (Jeff, 2026-11-13): one level above admin. A SuperUser may
# do everything an admin may (its matrix column is fixed all on), and is
# the only one who may see or change the nightly upload (SUPERUSER_ROWS)
# or create, change, reset or remove a SuperUser. Being one takes the
# local command line on the show PC (main(), below): never the web page.
# What a SuperUser may do beyond an admin is used only through
# superuser_here(): on the show PC's own rack screen, signed in there.
SUPERUSER = "superuser"
ROLES = (SUPERUSER, "admin", "operator", "viewer")
# The roles with every admin power (the last of them is never removed).
ADMIN_ROLES = (SUPERUSER, "admin")
SIGNED_OUT = "signed_out"
COLUMNS = ROLES + (SIGNED_OUT,)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .'_-]{0,39}$")
# Never an account's name. With the engine's own names (announce.py's
# ENGINE_NAMES: what the scheduler and the alert hold act as), so no real
# person's press can read as the engine's, and refused (Jeff, 2026-10-30).
RESERVED = ("unsigned", "system", "signed out", "nobody", "deck", "engine",
            "scheduler", "ltc player", "ltcplay")
# Pre-show audio's name in the journal (announce.PRESHOW_WHO): no person's
# account may carry it, so its automatic play never reads as a press.
RESERVED += ("pre-show audio",)
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
    ("show", "Start now, Start at a cue, Resume, Show control on/off, "
             "rehearsal scrubbing"),
    ("arm", "Arm flames and lasers: hold to arm, the Stream Deck's arm "
            "keys, a purge (momentary or timed), the Fireball page's "
            "channels, the wave flamer "
            "cleaning, arming a laser, arming a hazer, Arm all hazers, "
            "the hazer test, the hazer levels, "
            "turning the fuel pump on, Fireball practice (open and Step, "
            "with the Show row too)"),
    ("unlock", "Unlock a machine, turn a laser test on"),
    ("checklist", "Tick the Morning and Shutdown checklists"),
    ("show_files", "Show files and setup: the show folder, the schedule, "
                   "reload, the machine layout"),
    ("settings", "Settings: the show network, arming from a screen, live "
                 "flame programming, moving Programming playback from an "
                 "iPad while flames follow it, the timecode input, the "
                 "announcement output, idle sign out, "
                 "sign out after the last show, the play-from cues, the "
                 "fireball timeline"),
    ("users", "Users: add, remove, reset passwords, set roles, clear "
              "sign-in lockouts"),
)
PERM_KEYS = tuple(p for p, _ in PERMS)
# The SuperUser's own rows: never in PERMS, never in a stored matrix, never
# in whoami's "may", never in the Users screen's matrix for anyone but a
# SuperUser here. allowed() answers them from the role alone, so no matrix
# edit can ever give them to anyone else.
UPLOAD = "upload"
SUPERUSER_ROWS = (
    (UPLOAD, "The nightly upload: on or off, server, token, time, days "
             "kept, Send now and its state"),
)
SUPERUSER_PERMS = tuple(p for p, _ in SUPERUSER_ROWS)
# The upload's routes (remote.py). is_upload_route() also takes any route
# whose name says "upload", so a route added for the upload later (the
# night journal's, PR #246) is the SuperUser's without a change here.
UPLOAD_ROUTES = ("audit-settings", "audit-upload-now")
# Who may have set a SuperUser's temporary password that it then signs in
# with (Users.sign_in): a SuperUser, or the local command line.
LOCAL_COMMAND = "local command line"
SUPERUSER_RESETS = (SUPERUSER, LOCAL_COMMAND)
# Audit lines the local command line leaves in the users file for the
# engine to write (Users.flush_pending_audit): at most this many.
PENDING_AUDIT_MAX = 50
# What a refused upload route answers: the same words as a route that is
# not there at all, so it says nothing about the setting.
UPLOAD_REFUSED = "no such thing here"
# What an admin hears when a change needs a SuperUser.
SUPERUSER_ONLY = "Only a SuperUser can do that. Nothing was changed."
SAFETY_ROWS = ("Stop, Hold, Disarm (one group or every group, "
               "flames or lasers), "
               "letting go of a hold, stopping or clearing a purge, "
               "locking a machine, a laser test off, live flame "
               "programming off, "
               "a hazer disarm, Hazers off, the hazer test off, "
               "the fuel pump off, "
               "Fireball practice's Done, Acknowledge, signing "
               "in and out: always, for everyone, signed in or not")

# The defaults (Jeff decides; USERS.md). Signed out is view only (Jeff's
# field report, 2026-10-28): it may do nothing restricted, and FIXED keeps
# it that way.
DEFAULT_MATRIX = {
    SUPERUSER: {p: True for p in PERM_KEYS},
    "admin": {p: True for p in PERM_KEYS},
    "operator": {"show": True, "arm": True, "unlock": True,
                 "checklist": True, "show_files": True, "settings": False,
                 "users": False},
    "viewer": {p: False for p in PERM_KEYS},
    SIGNED_OUT: {p: False for p in PERM_KEYS},
}
# The people LTC Player starts with (Jeff, 2026-10-20). Written once, at
# first run, with NO password: each chooses their own at their first
# sign-in. No password is ever shipped in the repo. Never a SuperUser
# (seed() refuses one): a name with no password yet is claimed by whoever
# signs in as it first at the rack screen, so a SuperUser is only ever
# made by the local command line, by someone with the PC's files.
SEED_USERS = (("Jeff", "admin"), ("Andy", "admin"), ("Dallen", "operator"),
              ("Dave", "operator"), ("Jared", "operator"))
# An admin can always manage users, so nobody can lock every admin out.
# And signed out may do nothing restricted (2026-10-28): view only.
FIXED = {("admin", "users"): True}
FIXED.update({(SIGNED_OUT, p): False for p in PERM_KEYS})
# A SuperUser may always do everything an admin may.
FIXED.update({(SUPERUSER, p): True for p in PERM_KEYS})
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
    "hold": "safety", "disarm-all": "safety", "stop": "safety",
    "group-disarm": "safety", "arm-release": "safety", "alert": "safety",
    "purge-stop": "safety", "purge-clear": "safety",
    "signin": "safety", "signout": "safety",
    # Operator announcements (remote.ANNOUNCE_ROUTES): a Play is a show
    # press; Stop ends only the announcement's audio, so it only takes
    # something away and is never restricted.
    "announce-stop": "safety", "announce-play": "show",
    "start-now": "show", "resume": "show",
    # Start at a cue (play_from.py part 2, Jeff 2026-10-31): Start now from
    # a saved cue; operators may, as for Start now.
    "start-at-cue": "show",
    # Run off (2026-10-30): Stop no longer turns Run off, so the
    # Programming Run card's Run off does (the Stop, then playback
    # stops). Run itself is a show press (/api/start), and so is this.
    "run-off": "show",
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
    # Pre-show audio's On/Off and file (remote.preshow_audio_post).
    "preshow-audio": "settings",
    "live-flame": "settings",
    # The nightly upload: the SuperUser's alone (UPLOAD, is_upload_route).
    "audit-settings": UPLOAD,
    "audit-upload-now": UPLOAD, "idle-signout": "settings",
    "last-show-signout": "settings",
    "users": "users", "role-matrix": "users", "clear-lockouts": "users",
    "schedule-file": "show_files", "show-folder": "show_files",
    # Reload engine (2026-10-28): a restart of the engine between shows,
    # a settings press (the Settings row, admin by default).
    "reload-engine": "settings",
    "layout": "show_files",
    # The machine rows the Fuel and Purge steps share (morning.Arrangement):
    # the same row as the map's Arrange.
    "arrangement": "show_files",
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
    # Haze fluid (fuelhaze.py): the same, a refill of a hazer's tank and
    # its undo (the route itself needs an operator or admin, an admin).
    "haze-refill": None, "haze-undo": None,
    # The Fireball page (fireball.py): All off only takes risk away.
    "fireball-off": "safety",
    # Wave flamer cleaning (cleaning.py): Stop only takes risk away.
    "cleaning-stop": "safety",
    # The hazers (hazers.py): Hazers off only takes something away; a
    # hazer ON is an arm press, OFF safety (classify).
    "hazers-off": "safety", "hazer": "arm", "hazer-level": "arm",
    # Arm all hazers (2026-11-11): one arm press per hazer, the same row.
    "hazers-arm-all": "arm",
    # The hazer test (hazertest.py, 2026-11-11): ON and its beat are arm
    # presses (the row that arms a hazer); OFF safety (classify).
    "hazer-test": "arm",
    # Play-from cues (play_from.py): saving the cue list is setup, admin
    # only by default. It sends nothing and starts nothing.
    "play-from-cues-save": "settings",
    # The fuel pump (fuelpump.py, 2026-10-30): ON is an arm press, OFF
    # safety (classify).
    "fuel-pump": "arm",
    # The fireball timeline (fireball_timeline.py, Jeff 2026-10-30): a
    # save is setup, admin only by default.
    "fireball-timeline-save": "settings",
    # The charge record (fireball_charge.py): an admin confirms NOT KNOWN
    # tanks are empty.
    "fireball-charge-empty": "settings",
    # Shows per refill (fuel.py): like fuel-refill, no matrix row; the
    # route checks who (a refill's operator or admin, N an admin).
    "fuel-shows": None,
    # Fireball practice (fireball_practice.py): open, Step and the beat
    # that keeps it open are arm presses (the engine also asks the Show
    # row, at every press, on any screen); Done only takes risk away
    # (classify).
    "fireball-practice": "arm",
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


def is_upload_route(name):
    """True for a route of the nightly upload: UPLOAD_ROUTES, and any route
    whose name says "upload". Only superuser_here() may use one."""
    n = str(name or "").strip().lower()
    return n in UPLOAD_ROUTES or "upload" in n


def superuser_here(session, local, tailscale=False):
    """THE one test for a SuperUser's own powers (the nightly upload, and
    making or changing a SuperUser): a SuperUser signed in on this show
    PC's own rack screen (the session was made here, and this request is
    from here), never from an iPad, never from Tailscale (remote.py's
    Tailscale door, PR #244), never with nobody signed in. Anything else,
    or anything not known, is False."""
    try:
        return local is True and not tailscale and \
            isinstance(session, dict) and \
            session.get("role") == SUPERUSER and \
            session.get("local") is True and bool(session.get("name"))
    except Exception:                            # noqa: BLE001
        return False


class RankRefused(ValueError):
    """A change only a SuperUser may make, asked by someone else. Its words
    are SUPERUSER_ONLY; `what` is for the audit log."""

    def __init__(self, what, words=SUPERUSER_ONLY):
        super().__init__(words)
        self.what = what


def _need_superuser(by, what):
    if by != SUPERUSER:
        raise RankRefused(what)


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
        if is_upload_route(name):
            return UPLOAD
        if name == "purge-fire":
            # The engine starts or carries on a press only for
            # purge_fire_wanted(); everything else it turns into a stop.
            return "arm" if purge_fire_wanted(raw) else "safety"
        if name == "purge-select" and \
                body.get("on") is False:
            return "safety"
        if name == "fireball-practice" and body.get("action") not in (
                "open", "step", "beat"):
            # Done, or anything the engine does not open or step with,
            # only ends practice.
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
        if name == "fuel-pump" and body.get("on") is not True:
            # Anything but the JSON word true turns the pump off at the
            # engine (remote.pump_post): it only takes something away.
            return "safety"
        if name == "hazer-test" and body.get("on") is not True and \
                not isinstance(body.get("beat"), str):
            # Anything but ON (the JSON word true) or a beat turns the
            # hazer test off at the engine (remote.hazer_test_post).
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
        if name == "arrangement" and body.get("beat") is True:
            # The Fuel step's Arrange beat saves nothing; it only holds
            # this screen's refills back (morning.Morning.rows_beat).
            return None
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
    if perm in SUPERUSER_PERMS:
        # Never from a matrix: the role alone.
        return role == SUPERUSER
    if role is None or role not in ROLES:
        return False
    col = role
    row = (matrix or DEFAULT_MATRIX).get(col) or {}
    return row.get(perm) is True


def perm_label(perm):
    return dict(PERMS + SUPERUSER_ROWS).get(perm, perm)


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


@contextlib.contextmanager
def _file_lock(path, timeout=None):
    """An exclusive lock on `path` across processes (the engine and the
    local command line), released by the system if the holder dies.
    Raises ValueError with a sentence when it cannot be had in time."""
    fh = open(path, "a+b")
    got = False
    try:
        deadline = time.monotonic() + (LOCK_WAIT_S if timeout is None
                                       else timeout)
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ValueError("Another program is changing the users "
                                     "file just now. Nothing was changed; "
                                     "try again in a moment.")
                time.sleep(0.05)
        yield
    finally:
        if got:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


def _pw_digest(u):
    """A short digest of a user record's password hash as written."""
    pw = u.get("pw") if isinstance(u, dict) and \
        isinstance(u.get("pw"), dict) else {}
    return hashlib.sha256(json.dumps(
        [pw.get("alg"), pw.get("salt"), pw.get("hash")]).encode()
    ).hexdigest()[:32]


def _audit_history_says_superuser(folder):
    """True when any audit line here ever recorded a SuperUser being made:
    read only by the local command line, never on a hot path. It never
    refuses anything (the files decide, ever_superuser); a first
    SuperUser made again after the SuperUser files were removed by hand
    (SUPERUSER.md, the emergency procedure) is written down with an
    alert."""
    try:
        from . import audit
        d = audit.default_folder(folder)
        for n in audit.audit_files(d):
            recs, _p = audit.read_lines(os.path.join(d, n))
            if any(r.get("action") in ("superuser-set",
                                       "superuser-confirmed")
                   for r in recs):
                return True
    except Exception:                            # noqa: BLE001
        return True                  # not known: as if one had existed
    return False


def _cli_audit_ids(folder):
    """The ids of the local command line's own audit lines (stream
    "cli"). Never raises."""
    try:
        from . import audit
        d = audit.default_folder(folder)
        out = set()
        for n in audit.audit_files(d):
            if audit.parse_name(n)[1] != "cli":
                continue
            recs, _p = audit.read_lines(os.path.join(d, n))
            out |= {str(r.get("id")) for r in recs if r.get("id")}
        return out
    except Exception:                            # noqa: BLE001
        return set()


def _cli_audit(folder, action, user, target, reason, ident):
    """The local command line's own audit line, written and fsynced to the
    audit log (stream "cli") before the command returns (B1d)."""
    from . import audit
    log = audit.AuditLog(audit.default_folder(folder), "cli",
                         threaded=False, fsync=True)
    log.event(user=user, surface="cli", action=action, target=target,
              reason=reason, source=LOCAL_COMMAND, id=ident)
    if log.fault:
        raise OSError(f"the audit log could not be written: {log.fault}")


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
        # The password typed again (confirm): its own small counter, so a
        # borrowed session's wrong guesses never lock the person out of
        # signing in.
        self._confirm_fails = {}
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
        # show is over and nothing is live or held. Set by the
        # engine (remote.Remote.last_show_over). None: never.
        self.last_show = None
        self._last_show_done = None   # the night already signed out
        self._beat_stop = threading.Event()
        self._beat = None
        self.key_path = os.path.join(folder, USERS_KEY)
        self.lock_path = os.path.join(folder, USERS_LOCK)
        self.signal_path = os.path.join(folder, USERS_SIGNAL)
        # What the last read found wrong with the SuperUsers (_trust):
        # sentences, written to the audit log and raised as an alert by
        # the engine (reconcile, flush_pending_audit). `on_alert(text)` is
        # the engine's alert (remote.py); None: no alert.
        self._tamper = []
        self.on_alert = None
        # What _trust has already said since the file was last found
        # consistent: a SuperUser demoted outside the engine stays that
        # way until confirmed, and is said (and alerted) once, not on
        # every beat that reads the file again.
        self._said = set()
        # The SuperUsers this program last wrote (B1c), a digest of each
        # one's password hash as written, and lockdown (B1b).
        self._known = None
        self._known_pw = {}
        self._lockdown = False

    # -- the SuperUser's fingerprints (review of #251, M1, M2) -----------
    def _key(self, create=False):
        """The engine's key (bytes), or None. Made once, private, when a
        SuperUser is first written."""
        try:
            with open(self.key_path, encoding="utf-8") as fh:
                k = bytes.fromhex(json.load(fh)["key"])
            if len(k) >= 32:
                return k
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if not create:
            return None
        k = secrets.token_bytes(32)
        os.makedirs(self.folder, exist_ok=True)
        _write_private(self.key_path, {"key": k.hex(), "note": (
            "LTC Player's SuperUser fingerprints. Removing or changing this "
            "file makes every SuperUser an admin until a SuperUser "
            "confirms them again.")})
        return k

    @staticmethod
    def _mac(key, what):
        return hmac.new(key, json.dumps(what, sort_keys=True).encode(
            "utf-8"), hashlib.sha256).hexdigest()

    def _fp(self, key, name, u):
        """A SuperUser record's fingerprint: its name, role and password
        hash (and whether that is temporary, and who set it), as this
        build wrote them. An older build's reset, a hand edit or a copy
        from elsewhere does not match."""
        pw = u.get("pw") if isinstance(u.get("pw"), dict) else {}
        return self._mac(key, ["su-fp-1", str(name).lower(), u.get("role"),
                               str(pw.get("alg", "")), str(pw.get("salt",
                                                              "")),
                               str(pw.get("hash", "")),
                               bool(u.get("must_change")),
                               str(u.get("reset_by", ""))])

    # -- the engine's last-written set of SuperUsers (B1c) --------------
    def _second_path(self):
        return os.path.join(self.folder, SU_SECOND)

    def _read_second(self):
        try:
            with open(self._second_path(), encoding="utf-8") as fh:
                d = json.load(fh)
            names = d.get("names")
            if isinstance(names, list):
                pw = d.get("pw") if isinstance(d.get("pw"), dict) else {}
                return {"names": {str(x).lower() for x in names},
                        "pw": {str(k).lower(): str(v)
                               for k, v in pw.items()},
                        "lockdown": d.get("lockdown") is True}
        except (OSError, ValueError, AttributeError):
            pass
        return None

    def _known_set(self):
        """(names, lockdown): the SuperUsers this program last wrote, from
        memory, else from the second copy; (None, False) when it never
        wrote one."""
        if self._known is None:
            got = self._read_second()
            if got is not None:
                self._known = set(got["names"])
                self._known_pw = dict(got["pw"])
                self._lockdown = self._lockdown or got["lockdown"]
        return self._known, self._lockdown

    def _write_second(self):
        try:
            p = self._second_path()
            os.makedirs(os.path.dirname(p), exist_ok=True)
            _write_private(p, {"names": sorted(self._known or ()),
                               "pw": {k: v for k, v in
                                      self._known_pw.items()
                                      if k in (self._known or ())},
                               "lockdown": bool(self._lockdown),
                               "note": "The SuperUsers LTC Player last "
                                       "wrote (users.py)."})
        except OSError:
            pass

    def ever_superuser(self, doc=None):
        """True while any of the SuperUser's own files is here (B1a): the
        mark (SU_MARK), the key (USERS_KEY) or the second copy (SU_SECOND),
        or this engine wrote a SuperUser since it started. While any one
        is here the first SuperUser path is refused. What the users file
        itself says (su_set, su_fp) never keeps it shut: removing the
        three files by hand, with LTC Player stopped, is Jeff's own way
        back (SUPERUSER.md, the emergency procedure). `doc` is unused and
        kept for the callers."""
        return (os.path.exists(os.path.join(self.folder, SU_MARK)) or
                os.path.exists(self.key_path) or
                os.path.exists(self._second_path()) or
                self._known is not None)

    def lockdown(self):
        """True while no SuperUser is trusted at all (B1b): a key, set
        record or fingerprint that had existed went missing. Ends when a
        SuperUser this program wrote confirms with their password on the
        rack screen (confirm_superuser), or by the emergency procedure in
        SUPERUSER.md (the SuperUser files removed by hand)."""
        with self._lock:
            self._load()
            return bool(self._lockdown)

    def _trust(self, doc):
        """On every read of the file: a SuperUser whose fingerprint is not
        this engine's, or who is not in the set this engine last wrote
        (memory, else the second copy), reads as an admin until a
        SuperUser confirms them. If the key, the set record or a
        fingerprint that had existed is missing, NO SuperUser is trusted
        (lockdown) until one this program wrote confirms with their
        password. Never promotes anyone. Anything found goes into
        self._tamper (the audit log and an alert). Changes `doc`."""
        users = doc.get("users") or {}
        key = self._key()
        known, _lock = self._known_set()
        ever = self.ever_superuser(doc)
        found = []
        missing = []
        if ever and key is None:
            missing.append("the SuperUser key")
        if ever and not isinstance(doc.get("su_set"), dict):
            missing.append("the record of who is a SuperUser")
        supers = [n for n, u in users.items()
                  if isinstance(u, dict) and u.get("role") == SUPERUSER]
        if ever and any("su_fp" not in users[n] for n in supers
                        if known is None or n.lower() in known):
            missing.append("a SuperUser's fingerprint")
        if missing and not self._lockdown:
            self._lockdown = True
            self._write_second()
            found.append(f"Missing: {', '.join(missing)}. No SuperUser is "
                         f"trusted until one confirms with their password "
                         f"on the rack screen.")
        if known is not None and not self._lockdown:
            known = self._accept_local_command(doc, known)
        trusted = []
        for n in supers:
            u = users[n]
            fp = u.get("su_fp")
            ok = (not self._lockdown and key is not None and
                  isinstance(fp, str) and
                  hmac.compare_digest(fp, self._fp(key, n, u)) and
                  (known is None or n.lower() in known))
            if ok:
                trusted.append(n.lower())
                continue
            u["role"] = "admin"
            u["su_untrusted"] = True
            if not self._lockdown:
                found.append(f"{n} is a SuperUser in the users file, but "
                             f"not one this program wrote (a hand edit, an "
                             f"older version's change, or a missing key). "
                             f"{n} is an admin until a SuperUser confirms "
                             f"them.")
        st = doc.get("su_set")
        if isinstance(st, dict) and not self._lockdown:
            names = st.get("names")
            ok = key is not None and isinstance(names, list) and \
                isinstance(st.get("sig"), str) and hmac.compare_digest(
                    st["sig"], self._mac(key, ["su-set-1", sorted(
                        str(x) for x in names)]))
            if not ok:
                found.append("The record of who is a SuperUser was changed "
                             "outside this program.")
            else:
                gone = sorted(set(str(x) for x in names) - set(trusted))
                if known is not None:
                    gone = sorted(set(gone) | (known - set(trusted)))
                    # A name added to the record is said here; one
                    # taken out is said once below ("No longer").
                    if set(str(x) for x in names) - known:
                        found.append("The record of who is a SuperUser "
                                     "differs from the one this program "
                                     "wrote last.")
                if gone:
                    found.append(f"No longer a SuperUser, changed outside "
                                 f"this program: {', '.join(gone)}.")
        if not found:
            self._said = set()
        new = [f for f in found if f not in self._said]
        self._said |= set(new)
        if new:
            self._tamper.extend(new)
        return doc

    def _accept_local_command(self, doc, known):
        """A SuperUser the local command line made while this engine runs
        is not in the engine's own set yet. It is taken only when all
        three agree: the command's second copy names them, the users file
        carries the command's pending audit line for them, and the
        command's own line with the same id is in the audit log (B1d).
        Anything else stays an admin (B1c)."""
        users = doc.get("users") or {}
        new = {n.lower() for n, u in users.items()
               if isinstance(u, dict) and u.get("role") == SUPERUSER} - known
        if not new:
            return known
        second = self._read_second() or {"names": set(), "pw": {}}
        q = doc.get("pending_audit") if isinstance(
            doc.get("pending_audit"), list) else []
        ids = None
        for n in sorted(new):
            want = [x for x in q if isinstance(x, dict) and
                    str(x.get("target", "")).lower() == n and
                    x.get("action") == "superuser-set" and
                    x.get("id") and x.get("source") == LOCAL_COMMAND]
            if not want or n not in second["names"]:
                continue
            if ids is None:
                ids = _cli_audit_ids(self.folder)
            if any(x["id"] in ids for x in want):
                known = set(known) | {n}
                self._known = known
                if n in second["pw"]:
                    self._known_pw[n] = second["pw"][n]
        return known

    def _sign(self, doc):
        """Before every save: each SuperUser's fingerprint and the set of
        SuperUsers, as this build writes them; the same set kept in
        memory and in the second copy. In lockdown nobody is signed and
        the remembered set stays as it was (those who may confirm)."""
        users = doc.get("users") or {}
        supers = [n for n, u in users.items()
                  if isinstance(u, dict) and u.get("role") == SUPERUSER]
        for u in users.values():
            if isinstance(u, dict):
                if u.get("role") != SUPERUSER:
                    u.pop("su_fp", None)
        for u in users.values():
            if isinstance(u, dict):
                u.pop("su_untrusted", None)
        if not supers and self._known is None and \
                not os.path.exists(self.key_path):
            # No SuperUser and none of the SuperUser's files: nothing is
            # made here, and a stale set record (the files removed by
            # hand, SUPERUSER.md) goes, so the first SuperUser path stays
            # open for the emergency procedure.
            doc.pop("su_set", None)
            return
        key = self._key(create=True)
        for n in supers:
            users[n]["su_fp"] = self._fp(key, n, users[n])
        names = sorted(n.lower() for n in supers)
        doc["su_set"] = {"names": names,
                         "sig": self._mac(key, ["su-set-1", names])}
        if not self._lockdown and not getattr(self, "_keep_known", False):
            # Only a SuperUser's own change (set_role, remove) takes a name
            # out of the engine's set; one demoted by a change found
            # outside stays able to confirm.
            gone = getattr(self, "_dropped", set())
            keep = (self._known or set()) - gone
            self._known = set(names) | keep
            self._known_pw = dict(
                {k: v for k, v in self._known_pw.items() if k in keep},
                **{n.lower(): _pw_digest(users[n]) for n in supers})
            self._dropped = set()
            self._write_second()
        if supers:
            try:
                with open(os.path.join(self.folder, SU_MARK), "w",
                          encoding="utf-8") as fh:
                    fh.write("A SuperUser was made on this machine. While "
                             "this file is here a SuperUser is made only "
                             "with a SuperUser's password (SUPERUSER.md).\n")
            except OSError:
                pass

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
            doc = self._trust(doc)
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
        self._sign(clean)
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
                            "unconfirmed": bool(u.get("su_untrusted")),
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
        if perm in SUPERUSER_PERMS:
            # Fail closed: with no users there is no SuperUser.
            return self.active() and allowed(None, role, perm)
        if not self.active():
            return True
        return allowed(self.matrix(), role, perm)

    # -- sessions -----------------------------------------------------------
    def _drop_idle(self, now):
        self._drop_superuser_idle(now)
        mins = self.idle_minutes()
        if not mins:
            return
        gone = [(t, s) for t, s in self._sessions.items()
                if now - s["seen"] > mins * 60]
        for t, s in gone:
            del self._sessions[t]
            self._note("sign-out", s["name"], reason=f"idle for {mins} min",
                       source=s["source"])

    def _drop_superuser_idle(self, now):
        """A SuperUser's session ends SUPERUSER_IDLE_S after its last press,
        whatever the admin's idle and last show settings are (review of
        #251, M3): an admin cannot keep a SuperUser's rack screen session
        open for themselves."""
        users = self._load().get("users", {})
        gone = [(t, s) for t, s in self._sessions.items()
                if (users.get(s["name"]) or {}).get("role") == SUPERUSER
                and now - s["seen"] > SUPERUSER_IDLE_S]
        for t, s in gone:
            del self._sessions[t]
            self._note("sign-out", s["name"],
                       reason=f"SuperUser idle for "
                              f"{SUPERUSER_IDLE_S // 60} min",
                       source=s["source"])

    def confirm(self, name, password, source):
        """True when `password` is `name`'s own: a SuperUser's again before
        an upload change (M3). A wrong one is audited and counts on its own
        small counter (self._confirm_fails), never toward the sign-in
        lockout: a session someone borrowed cannot lock its owner out of
        signing in. Too many wrong ones pause only the password again."""
        now = self._clock()
        keys = [("name", str(name).lower()), ("from", str(source))]
        with self._lock:
            if self._locked(keys, now, self._confirm_fails):
                self._note("password-confirm", name, "refused",
                           "too many wrong passwords typed again; wait",
                           source=source)
                return False
            rec = (self._load().get("users", {}).get(name) or {}).get("pw")
        with self._check_lock:
            ok = rec is not None and check_password(str(password or ""), rec)
        with self._lock:
            if ok:
                for k in keys:
                    self._confirm_fails.pop(k, None)
            else:
                self._failed(keys, now, self._confirm_fails)
        if not ok:
            self._note("password-confirm", name, "refused",
                       "the password typed again did not match",
                       source=source)
        return ok

    def may_confirm(self, name):
        """True when `name` is a SuperUser this program wrote who reads as
        an admin now (lockdown, or their record changed outside it): they
        may confirm themselves with their password on the rack screen."""
        with self._lock:
            doc = self._load()
            known, _l = self._known_set()
            u = (doc.get("users") or {}).get(name)
            # Only with the password this program last wrote for them: a
            # password changed outside it (an older build's reset someone
            # else knows) confirms nothing; the emergency procedure in
            # SUPERUSER.md does.
            return bool(known) and str(name).lower() in known and \
                isinstance(u, dict) and u.get("role") != SUPERUSER and \
                self._known_pw.get(str(name).lower()) == _pw_digest(u)

    def confirm_superuser(self, name, password, source):
        """A SuperUser this program wrote, reading as an admin (lockdown or
        a record changed outside it), confirms themselves: their own
        password typed on the rack screen (remote.py checks the screen).
        Ends lockdown. Raises ValueError; audited either way."""
        if not self.may_confirm(name):
            raise ValueError("There is nothing for you to confirm. Nothing "
                             "was changed.")
        if not self.confirm(name, password, source):
            raise ValueError("That is not your password. Nothing was "
                             "changed.")

        def fn(doc):
            users = doc["users"]
            real = self._real(users, name)
            self._lockdown = False
            users[real]["role"] = SUPERUSER
            users[real].pop("su_untrusted", None)
            users[real]["changed"] = self._stamp()
            return real
        with self._lock:
            keep, keep_pw = self._known, dict(self._known_pw)
            real = self._change(fn)
            # The others this program wrote stay confirmable.
            self._known = set(keep or ()) | {real.lower()}
            self._known_pw = dict(keep_pw, **self._known_pw)
            self._write_second()
        self._note("superuser-confirmed", real, reason="confirmed with "
                   "their own password on the rack screen", target=real,
                   source=source)
        return f"{real} is a SuperUser again."

    def consume_signal(self):
        """The local command line's word to this engine after a SuperUser's
        password reset (USERS_SIGNAL, M4): end that person's sessions and
        clear their lockout. Taken once and removed; one that is not
        signed with this engine's key, names nobody, or is older than
        SIGNAL_MAX_AGE_S is ignored, and said in the audit log. Never
        raises."""
        try:
            with open(self.signal_path, encoding="utf-8") as fh:
                raw = fh.read()
        except OSError:
            return None
        try:
            os.remove(self.signal_path)
        except OSError:
            pass
        why, name = "", ""
        try:
            doc = json.loads(raw)
            name = str(doc.get("name") or "")
            at = float(doc.get("at"))
            key = self._key()
            if key is None or not isinstance(doc.get("sig"), str) or \
                    not hmac.compare_digest(doc["sig"], self._mac(
                        key, ["su-signal-1", name, doc.get("at"),
                              doc.get("nonce")])):
                why = "not signed by this program"
            elif abs(self._wall() - at) > SIGNAL_MAX_AGE_S:
                why = "too old"
            elif str(doc.get("nonce")) in self._seen_nonces():
                why = "already used (a replay)"
            elif self.role_of(name) != SUPERUSER:
                why = "names no SuperUser"
        except (ValueError, TypeError, AttributeError):
            why = "not readable"
        if why:
            self._note("superuser-signal", "unknown", "refused",
                       f"a signal file was ignored: {why}",
                       target=name[:40], source="outside the engine")
            return False
        self._remember_nonce(str(doc.get("nonce")))
        self._end_sessions(name)
        with self._lock:
            self._fails.pop(("name", name.lower()), None)
            self._confirm_fails.pop(("name", name.lower()), None)
        self._note("superuser-signal", "unknown", reason=(
            "a SuperUser's password was reset on this PC: their sessions "
            "ended and their lockout cleared"), target=name,
                   source=LOCAL_COMMAND)
        return True

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
            self.flush_pending_audit()
            self.consume_signal()
            while not self._beat_stop.wait(every_s):
                self.after_last_show_tick()
                self.flush_pending_audit()
                self.consume_signal()
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

    def session_check(self, token):
        """For a watcher that must never wait on this lock (the fuel pump's
        timer: a password hash runs under it): ("ok", session), ("gone",
        None), ("broken", None) while the users file cannot be used, ("off",
        None) while there are no users at all, or ("busy", None) when the
        lock is held right now. Never raises."""
        if not self._lock.acquire(blocking=False):
            return "busy", None
        try:
            if self._load().get("broken"):
                return "broken", None
            if not self.active():
                return "off", None
            s = self.session(token)
            return ("ok", s) if s is not None else ("gone", None)
        except Exception:
            return "broken", None
        finally:
            self._lock.release()

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

    def _locked(self, keys, now, table=None):
        table = self._fails if table is None else table
        worst = 0.0
        for k in keys:
            f = table.get(k)
            if f and f[1] > now:
                worst = max(worst, f[1] - now)
        return worst

    def _failed(self, keys, now, table=None):
        table = self._fails if table is None else table
        for k in keys:
            f = table.setdefault(k, [0, 0.0])
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
            if u is not None and u.get("role") == SUPERUSER and (
                    first or (must and u.get("reset_by") not in
                              SUPERUSER_RESETS)):
                # A SuperUser's password is never claimed: not with none
                # at all, and not with a temporary one that a SuperUser did
                # not set (an older build's admin reset, a hand edit). The
                # local command line sets one (main()).
                first, rec, must = False, None, False
                u = None
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
                self._set_pw(real, pw, temporary=False, own=True)
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
            os.makedirs(self.folder, exist_ok=True)
            if getattr(self, "_xheld", False):
                # The local command line holds the lock already (main()).
                return self._change_locked(fn)
            # One change at a time across processes too (the engine and
            # the local command line), each from a fresh read.
            with _file_lock(self.lock_path):
                return self._change_locked(fn)

    def _change_locked(self, fn):
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
            # A save that puts right a change found outside this program
            # never rewrites the engine's own set (B1c): those it made stay
            # the ones who may confirm.
            self._keep_known = bool(self._tamper)
            self._dropped = set()
            said = fn(doc)
            # What _trust found goes into the file for the engine's audit
            # log and alert (flush_pending_audit), whichever process saw
            # it first: the save below makes the file consistent again.
            for t in self._tamper:
                self._pend(doc, "superuser-set-changed", "unknown",
                           "users file", t, alert=True,
                           source="outside the engine")
            self._tamper = []
            hook = getattr(self, "_before_save", None)
            if hook is not None:
                # The local command line's own audit line, written and
                # fsynced before the users file changes (B1d), so the
                # engine never reads the change without it.
                hook(said)
            try:
                self._save(doc)
            finally:
                self._keep_known = False
            return said

    def _stamp(self):
        return time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(self._wall()))

    def add(self, name, role, password, by=None):
        """`by`: the rank the change is made with (remote.Remote.rank): a
        SuperUser is added only with SUPERUSER."""
        name = check_name(name)
        if role not in ROLES:
            raise ValueError(f"A role is {', '.join(ROLES)}.")
        if role == SUPERUSER:
            _need_superuser(by, f"add {name} as {SUPERUSER}")
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
        """Everyone with an admin's powers (a SuperUser too)."""
        return [n for n, u in users.items() if n != without and
                isinstance(u, dict) and u.get("role") in ADMIN_ROLES]

    def _supers(self, users, without=None):
        return [n for n, u in users.items() if n != without and
                isinstance(u, dict) and u.get("role") == SUPERUSER]

    def _guard_super(self, users, real, by, what):
        """A change to a SuperUser is a SuperUser's alone."""
        u = users.get(real)
        if isinstance(u, dict) and u.get("role") == SUPERUSER:
            _need_superuser(by, f"{what} {real} ({SUPERUSER})")

    def _real(self, users, name):
        real = {n.lower(): n for n in users}.get(str(name or "").lower())
        if real is None:
            raise ValueError(f"{name} is not a user.")
        return real

    def remove(self, name, by=None):
        def fn(doc):
            users = doc["users"]
            real = self._real(users, name)
            self._guard_super(users, real, by, "remove")
            self._dropped = {real.lower()}
            if users[real].get("role") == SUPERUSER and \
                    not self._supers(users, without=real):
                raise ValueError("That is the last SuperUser. Nothing was "
                                 "changed.")
            if users[real].get("role") in ADMIN_ROLES and \
                    not self._admins(users, without=real):
                raise ValueError("That is the last admin. Make another admin "
                                 "first.")
            del users[real]
            return f"{real} removed."
        said = self._change(fn)
        self._end_sessions(name)
        return said

    def set_role(self, name, role, by=None):
        if role not in ROLES:
            raise ValueError(f"A role is {', '.join(ROLES)}.")

        def fn(doc):
            users = doc["users"]
            real = self._real(users, name)
            self._guard_super(users, real, by, "change the role of")
            if role == SUPERUSER:
                _need_superuser(by, f"make {real} {SUPERUSER}")
            if users[real].get("role") == SUPERUSER and role != SUPERUSER:
                self._dropped = {real.lower()}
            if users[real].get("role") == SUPERUSER and role != SUPERUSER \
                    and not self._supers(users, without=real):
                raise ValueError("That is the last SuperUser. Nothing was "
                                 "changed.")
            if users[real].get("role") in ADMIN_ROLES and \
                    role not in ADMIN_ROLES and \
                    not self._admins(users, without=real):
                raise ValueError("That is the last admin. Make another admin "
                                 "first.")
            users[real]["role"] = role
            users[real]["changed"] = self._stamp()
            return f"{real} is now {role}."
        return self._change(fn)

    def _set_pw(self, name, pw, temporary, by=None, own=False):
        """`own`: the person's own password (their first sign-in, or their
        own change). Anyone else's: a SuperUser's only with `by`
        SUPERUSER."""
        def fn(doc):
            users = doc["users"]
            real = self._real(users, name)
            if not own:
                self._guard_super(users, real, by, "reset the password of")
                if pw is None and users[real].get("role") == SUPERUSER:
                    # A name with no password is claimed by whoever signs
                    # in as it first: never a SuperUser's.
                    raise ValueError("A SuperUser's password is reset with "
                                     "a temporary one, never cleared. "
                                     "Nothing was changed.")
            if temporary:
                users[real]["reset_by"] = str(by or "")
            else:
                users[real].pop("reset_by", None)
            users[real]["pw"] = (hash_password(pw, params=self._hash_params)
                                 if pw is not None else None)
            users[real]["must_change"] = bool(temporary)
            users[real]["changed"] = self._stamp()
            return real
        return self._change(fn)

    def set_password(self, name, password, by=None):
        """An admin's reset: a temporary password, changed by its user at
        their next sign-in."""
        pw = check_new_password(password)
        real = self._set_pw(name, pw, temporary=True, by=by)
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
        self._set_pw(name, pw, temporary=False, own=True)
        return "Your password was changed."

    def clear_password(self, name, by=None):
        """An admin's reset with no temporary password: the user chooses
        one at their next sign-in."""
        real = self._set_pw(name, None, temporary=False, by=by)
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
                if r not in ROLES or r == SUPERUSER:
                    # Never a SuperUser with no password (SEED_USERS).
                    raise ValueError(f"{r} is not a role a first run makes")
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
                    # The upload row is not a row of the matrix: the same
                    # words as any unknown row, so they tell nothing
                    # (remote.py audits the attempt).
                    if p not in PERM_KEYS or not isinstance(v, bool):
                        raise ValueError(f"{p} is not a row of the matrix.")
                    if c == SUPERUSER and v is not True:
                        raise RankRefused(
                            f"role matrix {c}/{p} off",
                            "A SuperUser can always do what an admin can.")
                    if (c, p) in FIXED and v != FIXED[(c, p)]:
                        raise ValueError(
                            "An admin can always manage users."
                            if c == "admin" else
                            "Signed out is view only: nobody signed in "
                            "may do anything but Stop, Hold, "
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
            self._confirm_fails.clear()
        return f"Sign-in lockouts cleared ({n})."

    # -- the SuperUser, from the local command line only (main()) ----------
    def _pend(self, doc, action, who, target, reason="", alert=False,
              source=LOCAL_COMMAND):
        """An audit line for the engine to write (flush_pending_audit): the
        command line has no audit writer of its own, and a second writer
        on the engine's day file would break its chain."""
        q = doc.get("pending_audit")
        q = [x for x in q if isinstance(x, dict)] if isinstance(q, list) \
            else []
        ident = getattr(self, "_audit_id", None)
        q.append({"action": action, "user": str(who or UNSIGNED)[:80],
                  "id": ident,
                  "target": str(target)[:120], "reason": str(reason)[:200],
                  "source": str(source)[:80], "at": self._stamp(),
                  "alert": bool(alert)})
        doc["pending_audit"] = q[-PENDING_AUDIT_MAX:]

    NOT_A_SUPERUSER = ("That is not a SuperUser's name and password. "
                       "Nothing was changed.")

    EVER_REFUSED = ("A SuperUser was made on this PC before, so a new one "
                    "needs a SuperUser's password. Nothing was changed.")

    def _authorize(self, users, real, by_name, by_password, doc=None):
        """The local command line's own check (review of #251, M1): once a
        SuperUser exists, a CURRENT SuperUser's name and password; before
        any, the person being made one types their own password. Never a
        password on the command line itself (main() asks for it)."""
        supers = self._supers(users)
        if not supers and self.ever_superuser():
            # The "first SuperUser" path is shut while any of the
            # SuperUser's files is here (B1a): removing the key, the set
            # record or a fingerprint through anything but a hand edit of
            # the settings folder never reopens it.
            time.sleep(AUTH_FAIL_PAUSE_S)
            raise ValueError(self.EVER_REFUSED)
        if supers:
            by = {n.lower(): n for n in supers}.get(
                " ".join(str(by_name or "").split()).lower())
        else:
            by = real if " ".join(str(by_name or "").split()).lower() == \
                real.lower() else None
        rec = (users.get(by) or {}).get("pw") if by else None
        ok = check_password(str(by_password or ""), rec) if rec else False
        if not ok or (supers and (users.get(by) or {}).get("must_change")):
            time.sleep(AUTH_FAIL_PAUSE_S)      # slow a guesser down
            raise ValueError(self.NOT_A_SUPERUSER if supers else
                             f"That is not {real}'s own password. Nothing "
                             f"was changed.")
        return by

    def promote_local(self, name, who, by_name=None, by_password=None):
        """Make an existing user a SuperUser. ONLY the local command line
        calls this (main(): run on the show PC by an account that can
        write the settings folder); no route reaches it. Once a SuperUser
        exists it needs a current SuperUser's name and password; the very
        first one needs the person's own password. Refused for a name
        whose password is not their own yet (none, or a temporary one
        someone else knows): that name could be claimed. Returns a
        sentence; raises ValueError."""
        def fn(doc):
            users = doc["users"]
            real = self._real(users, name)
            u = users[real] if isinstance(users[real], dict) else {}
            if u.get("pw") is None or u.get("must_change"):
                raise ValueError(f"{real} has not chosen their own password "
                                 f"yet. Sign in as {real} on the rack screen "
                                 f"first, then run this again. Nothing was "
                                 f"changed.")
            if u.get("role") == SUPERUSER:
                return f"{real} is already a SuperUser. Nothing was changed."
            first = not self._supers(users)
            by = self._authorize(users, real, by_name, by_password, doc)
            again = first and _audit_history_says_superuser(self.folder)
            before = u.get("role")
            u["role"] = SUPERUSER
            u["changed"] = self._stamp()
            users[real] = u
            self._pend(doc, "superuser-set", who, real,
                       f"was {before}; confirmed with {by}'s password" +
                       ("; the first SuperUser path again, after the "
                        "SuperUser files were removed by hand (the "
                        "emergency procedure)" if again else ""),
                       alert=again)
            return (f"{real} is now a SuperUser. LTC Player picks this up "
                    f"by itself; no restart is needed.")
        return self._change(fn)

    def superuser_password_local(self, name, password, who, by_name=None,
                                 by_password=None):
        """A temporary password for a SuperUser (the local command line
        only, like promote_local), confirmed with a current SuperUser's
        name and password. They choose their own at their next sign-in on
        the rack screen. The running engine is told (USERS_SIGNAL): it ends
        their sessions and clears their lockout."""
        pw = check_new_password(password)
        said = []

        def fn(doc):
            users = doc["users"]
            real = self._real(users, name)
            u = users[real] if isinstance(users[real], dict) else {}
            if u.get("role") != SUPERUSER:
                raise ValueError(f"{real} is not a SuperUser. Nothing was "
                                 f"changed.")
            said.append(self._authorize(users, real, by_name, by_password,
                                        doc))
            u["pw"] = hash_password(pw, params=self._hash_params)
            u["must_change"] = True
            u["reset_by"] = LOCAL_COMMAND
            u["changed"] = self._stamp()
            users[real] = u
            self._pend(doc, "superuser-password-reset", who, real,
                       f"confirmed with {said[0]}'s password")
            return real
        real = self._change(fn)
        self._signal(real)
        self._end_sessions(real)
        with self._lock:
            self._fails.pop(("name", real.lower()), None)
        return (f"{real} has a temporary password. {real} chooses their own "
                f"at the next sign-in on the rack screen.")

    def _seen_nonces(self):
        with self._lock:
            got = self._load().get("su_nonces")
        return set(got) if isinstance(got, dict) else set()

    def _remember_nonce(self, nonce):
        """A signal's nonce, kept SIGNAL_MAX_AGE_S in the users file, so the
        same signal file put back is refused (a replay)."""
        now = self._wall()

        def fn(doc):
            got = doc.get("su_nonces")
            got = {k: v for k, v in (got.items() if isinstance(got, dict)
                                     else ())
                   if isinstance(v, (int, float)) and
                   now - v <= SIGNAL_MAX_AGE_S}
            got[nonce] = now
            doc["su_nonces"] = got
        try:
            self._change(fn)
        except (ValueError, OSError):
            pass

    def _signal(self, name):
        """USERS_SIGNAL for the running engine (consume_signal): signed with
        the engine's key, written whole (atomic)."""
        key = self._key(create=True)
        at = self._wall()
        nonce = secrets.token_hex(8)
        _write_private(self.signal_path, {
            "name": name, "at": at, "nonce": nonce,
            "sig": self._mac(key, ["su-signal-1", name, at, nonce])})

    def flush_pending_audit(self):
        """Write the local command line's audit lines (pending_audit in the
        users file) through this engine's audit log, once each, and take
        them out of the file. Never raises."""
        try:
            with self._lock:
                q = self._load().get("pending_audit")
                if not q and not self._tamper:
                    return 0
                taken = []

                def fn(doc):
                    # What this engine's own read found (_trust), then
                    # what another process left in the file. The save
                    # makes the file consistent again (_sign).
                    for t in self._tamper:
                        taken.append({"action": "superuser-set-changed",
                                      "user": "unknown",
                                      "target": "users file", "reason": t,
                                      "source": "outside the engine",
                                      "at": self._stamp(), "alert": True})
                    self._tamper = []
                    got = doc.pop("pending_audit", None)
                    taken.extend(x for x in (got or [])
                                 if isinstance(x, dict))
                self._change(fn)
            alerts = []
            ids = None
            for x in taken:
                if x.get("id") and x.get("source") == LOCAL_COMMAND:
                    # The command writes its own line to the audit log
                    # before it returns (B1d): it has to be there.
                    if ids is None:
                        ids = _cli_audit_ids(self.folder)
                    if x["id"] not in ids:
                        why = (f"The local command's own audit line for "
                               f"{x.get('action')} ({x.get('target')}) is "
                               f"not in the audit log.")
                        self._note("superuser-audit-missing", "unknown",
                                   "error", why,
                                   target=str(x.get("target") or ""),
                                   source="outside the engine")
                        alerts.append(why)
                self._note(str(x.get("action") or "superuser-set")[:40],
                           str(x.get("user") or UNSIGNED),
                           "error" if x.get("alert") else "accepted",
                           reason=(str(x.get("reason") or "") +
                                   f" (at {x.get('at', '')})").strip(),
                           target=str(x.get("target") or ""),
                           source=str(x.get("source") or LOCAL_COMMAND))
                if x.get("alert"):
                    alerts.append(str(x.get("reason") or ""))
            if alerts and self.on_alert is not None:
                try:
                    self.on_alert(alerts)
                except Exception:                # noqa: BLE001
                    pass
            return len(taken)
        except Exception:                        # noqa: BLE001
            return 0

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


# ---------------------------------------------- the local command line --

USAGE = ("usage: ltcplay superuser NAME  (or python -m ltcplay.users "
         "superuser NAME)\n"
         "       ltcplay superuser-password NAME\n"
         "  [--folder SETTINGS_FOLDER]\n"
         "Passwords are asked for, hidden; never put one on this line.")
# The local command line tries this many times, a second apart, while the
# engine (or an antivirus) holds the users file.
LOCAL_TRIES = 5
_BUSY = ("Another program is changing", "cannot be opened just now")
COMMANDS = {"superuser": "superuser-set",
            "superuser-password": "superuser-password-reset"}


def main(argv=None, folder=None, who=None, read_password=None,
         read_line=None, out=print, pause=time.sleep):
    """ltcplay superuser NAME: make an existing user a SuperUser.
    ltcplay superuser-password NAME: a temporary password for a SuperUser.
    Run on the show PC itself (the double-click launchers run it); nothing
    on the web page can do either. The first SuperUser needs their own
    password, and only while none of the SuperUser's files is here
    (ever_superuser); after that a CURRENT SuperUser's password. A
    forgotten password with no other SuperUser: SUPERUSER.md, the
    emergency procedure. Every password is asked for, hidden. Each
    writes its own audit line (fsynced) before it returns, and leaves one
    for the engine, which checks they match. Exit 0 when done; never a
    traceback."""
    import getpass
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--folder" in argv:
        i = argv.index("--folder")
        if i + 1 >= len(argv):
            out(USAGE)
            return 2
        folder = argv[i + 1]
        del argv[i:i + 2]
    if len(argv) != 2 or argv[0] not in COMMANDS or not argv[1].strip():
        out(USAGE)
        return 2
    ask = read_password or getpass.getpass
    line = read_line or input
    cmd = argv[0]
    try:
        if folder is None:
            from . import remote
            folder = remote.settings_folder()
        if who is None:
            try:
                who = (f"Windows account {getpass.getuser()}"
                       if os.name == "nt" else
                       f"account {getpass.getuser()}")
            except Exception:                    # noqa: BLE001
                who = LOCAL_COMMAND
        us = Users(folder)
        if not os.path.exists(us.path):
            out(f"No users file in {folder}. Start LTC Player once first.")
            return 1
        name = " ".join(argv[1].split())
        doc = us._load()
        if doc.get("broken"):
            out(f"{us.error}. Nothing was changed.")
            return 1
        has = bool(us._supers(doc.get("users") or {}))
        by_name = by_pw = new = None
        if has:
            by_name = line("A SuperUser's name (yours, if you are one): ")
            by_pw = ask("That SuperUser's password: ")
        elif us.ever_superuser(doc):
            out(Users.EVER_REFUSED)
            return 1
        else:
            by_name = name
            by_pw = ask(f"{name}'s own password: ")
        if cmd == "superuser-password":
            new = ask("New temporary password (6 or more characters): ")
            if new != ask("The same again: "):
                out("The two did not match. Nothing was changed.")
                return 1
        ident = secrets.token_hex(8)
        us._audit_id = ident
        for n in range(LOCAL_TRIES):
            try:
                def before(said, cmd=cmd, name=name):
                    if isinstance(said, str) and "Nothing was changed" in \
                            said:
                        return
                    _cli_audit(folder, COMMANDS[cmd], who, name,
                               "by the local command line", ident)
                with _file_lock(us.lock_path):
                    us._xheld = True
                    us._before_save = before
                    try:
                        if cmd == "superuser":
                            said = us.promote_local(name, who, by_name, by_pw)
                        else:
                            said = us.superuser_password_local(
                                name, new, who, by_name, by_pw)
                    finally:
                        us._xheld = False
                        us._before_save = None
                out(said)
                return 0
            except ValueError as e:
                if n + 1 < LOCAL_TRIES and any(b in str(e) for b in _BUSY):
                    pause(1.0)
                    continue
                out(str(e))
                return 1
            except OSError as e:
                if n + 1 < LOCAL_TRIES:
                    pause(1.0)
                    continue
                out(f"The users file or the audit log could not be saved "
                    f"({e.strerror or e}). Close anything that has them "
                    f"open and try again.")
                return 1
    except (KeyboardInterrupt, EOFError):
        out("Stopped. Nothing was changed.")
        return 1
    except Exception as e:                       # noqa: BLE001
        out(f"Nothing was changed: {type(e).__name__}: {e}")
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
