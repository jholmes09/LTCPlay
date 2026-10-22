"""Named users with passwords, sign-in, and who may do what (Jeff,
2026-10-20). USERS.md says it in plain words.

WHAT THIS IS FOR: knowing who pressed what (audit.py writes it down). It is
not what keeps the show safe, and it never stands in the way of anything
that makes the rig safer.

THE SAFETY RULE (not negotiable): Abort, Hold, Stop, every disarm, letting
go of a hold, stopping or clearing a purge, locking a machine out, turning
a laser test off, Acknowledge and signing in or out work from every
surface whether anyone is signed in or not, whatever the role, and are
logged as "unsigned" when nobody is. classify() names them "safety" and
allowed() answers True for "safety" before it looks at anything else.
Nothing in flamesafe is touched by this module: its sole writer, the
Abort latch, the link key and hold-to-arm stay exactly as they were.

THE EARLIER PINs (PR #39, removed 2026-10-07): an operator PIN was needed
on the iPad, and arming from a screen needed a PIN session. Jeff ruled "no
PINs, no remote arming PIN": on a show night the sign-in stood between the
crew and the show. So here:
  - nobody has to sign in for the show to run. Until the first user is
    made, nothing changes at all; after that, the "Signed out" column of
    the role matrix says what a screen with nobody signed in may do, and
    by default that is every show control and arming, as before;
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
                      "idle_signout_min": 0}
Sessions live in memory: an engine restart signs everyone out, which
changes nothing on the rig.
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
COOKIE = "ltcplay_user"
ROLES = ("admin", "operator", "viewer")
SIGNED_OUT = "signed_out"
COLUMNS = ROLES + (SIGNED_OUT,)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .'_-]{0,39}$")
RESERVED = ("unsigned", "system", "signed out", "nobody")
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
    ("arm", "Arm flames: hold to arm, the Stream Deck's arm keys, a purge"),
    ("unlock", "Unlock a machine, turn a laser test on"),
    ("checklist", "Tick the Morning and Shutdown checklists"),
    ("show_files", "Show files and setup: the show folder, the schedule, "
                   "reload, the machine layout"),
    ("settings", "Settings: the show network, the timecode input, the "
                 "audit upload, idle sign out"),
    ("users", "Users: add, remove, reset passwords, set roles, clear "
              "sign-in lockouts"),
)
PERM_KEYS = tuple(p for p, _ in PERMS)
SAFETY_ROWS = ("Abort, Hold, Stop, Disarm (one group or every group), "
               "letting go of a hold, stopping or clearing a purge, "
               "locking a machine, a laser test off, Acknowledge, signing "
               "in and out: always, for everyone, signed in or not")

# The proposed defaults (Jeff decides; USERS.md). Signed out keeps the
# 2026-10-07 rule: no sign-in is needed to run or arm on a show night.
DEFAULT_MATRIX = {
    "admin": {p: True for p in PERM_KEYS},
    "operator": {"show": True, "arm": True, "unlock": True,
                 "checklist": True, "show_files": True, "settings": False,
                 "users": False},
    "viewer": {p: False for p in PERM_KEYS},
    SIGNED_OUT: {"show": True, "arm": True, "unlock": True,
                 "checklist": True, "show_files": False, "settings": False,
                 "users": False},
}
# The people LTC Player starts with (Jeff, 2026-10-20). Written once, at
# first run, with NO password: each chooses their own at their first
# sign-in. No password is ever shipped in the repo.
SEED_USERS = (("Jeff", "admin"), ("Andy", "admin"), ("Dallen", "operator"),
              ("Dave", "operator"), ("Jared", "operator"))
# An admin can always manage users, so nobody can lock every admin out.
FIXED = {("admin", "users"): True}


# ---------------------------------------------------- which press is which --

# The remote page's POST routes (remote.py) and the old page's (web.py).
# "safety": never restricted. None: harmless, never restricted.
REMOTE_ROUTES = {
    "abort": "safety", "hold": "safety", "disarm-all": "safety",
    "group-disarm": "safety", "arm-release": "safety", "alert": "safety",
    "purge-stop": "safety", "purge-clear": "safety",
    "signin": "safety", "signout": "safety",
    "start-now": "show", "resume": "show", "reset": "show",
    "play-from": "show", "jump": "show", "pause": "show",
    "continue": "show", "mark-a": "show", "mark-b": "show",
    "loop": "show", "follow": "show",
    "arm-hold": "arm",
    "network": "settings", "audit-settings": "settings",
    "audit-upload-now": "settings", "idle-signout": "settings",
    "users": "users", "role-matrix": "users", "clear-lockouts": "users",
    "schedule-file": "show_files", "show-folder": "show_files",
    "layout": "show_files",
    "checklist-tick": "checklist", "checklist-note": "checklist",
    "morning-open": None, "checklist-verify": None,
    "checklist-tally-retry": None, "open-folder": None, "screen": None,
    "audit-verify": None, "my-password": None,
    # These depend on the body: see classify().
    "purge-select": "arm", "purge-fire": "arm", "laser-test": "unlock",
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
        if name == "laser-test" and body.get("on") is not True:
            return "safety"
        if name == "lock" and body.get("on") is not False:
            return "safety" if body.get("kind") != "rewrite" else "settings"
        return REMOTE_ROUTES.get(name)
    if route == "/api/override" and body.get("look") == "blackout":
        return "safety"
    if route.startswith("/api/schedule"):
        # The deck's journal lines and the operator choice: the deck's own
        # rules already apply to them.
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
    in)? Safety and harmless presses always may; a role this does not know
    may do nothing restricted."""
    if perm == "safety" or perm is None:
        return True
    col = role if role in ROLES else (SIGNED_OUT if role is None else None)
    if col is None:
        return False
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
                         "dashes or apostrophes, and not unsigned, system or "
                         "nobody.")
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
        self._clock = clock
        self._wall = wall
        self._hash_params = hash_params
        self._audit = audit
        # Re-entrant: a session read drops idle sessions under the same
        # lock, and that reads the settings.
        self._lock = threading.RLock()
        self._check_lock = threading.Lock()   # one password check at a time
        self._sessions = {}       # token -> session dict
        self._fails = {}          # key -> [count, locked_until]
        self._doc = None
        self._mtime = None
        self.error = ""

    # -- the file -------------------------------------------------------
    def _load(self):
        """The file as a dict, read again when it changed on disk."""
        try:
            m = os.path.getmtime(self.path)
        except OSError:
            self._doc, self._mtime, self.error = {"users": {}}, None, ""
            return self._doc
        if self._doc is not None and m == self._mtime:
            return self._doc
        try:
            with open(self.path, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
            if not isinstance(doc, dict) or not isinstance(
                    doc.get("users", {}), dict):
                raise ValueError("not a users object")
            doc.setdefault("users", {})
            self.error = ""
        except (OSError, ValueError) as e:
            # A broken file: nobody can sign in, and the matrix is the
            # defaults. Nothing about the show is held back by it.
            doc = {"users": {}, "broken": True}
            self.error = f"{USERS_FILE} could not be read: {e}"
        self._doc, self._mtime = doc, m
        return doc

    def _save(self, doc):
        os.makedirs(self.folder, exist_ok=True)
        clean = {k: v for k, v in doc.items() if k != "broken"}
        _write_private(self.path, clean)
        self._doc = None

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
        """True once at least one user exists: only then do roles apply."""
        with self._lock:
            return bool(self._load().get("users"))

    def matrix(self):
        with self._lock:
            return normalise_matrix(self._load().get("matrix"))

    def idle_minutes(self):
        with self._lock:
            v = self._load().get("idle_signout_min", 0)
        return v if isinstance(v, int) and not isinstance(v, bool) and \
            0 <= v <= 24 * 60 else 0

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

    def session(self, token):
        """The live session for a cookie token, or None. A session whose
        user was removed is gone."""
        if not token:
            return None
        with self._lock:
            now = self._clock()
            self._drop_idle(now)
            s = self._sessions.get(token)
            if s is None:
                return None
            u = self._load().get("users", {}).get(s["name"])
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
            best = None
            users = self._load().get("users", {})
            for s in self._sessions.values():
                if s["local"] and isinstance(users.get(s["name"]), dict) and \
                        (best is None or s["at"] > best["at"]):
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
            self._sessions[token] = {"name": real, "local": bool(local),
                                     "source": str(source), "at": now,
                                     "seen": now}
        self._note("sign-in", real, source=source)
        return True, f"Signed in as {real}.", token, False

    def sign_out(self, token, source=""):
        with self._lock:
            s = self._sessions.pop(token, None) if token else None
        self._note("sign-out", s["name"] if s else "unsigned",
                   source=source)
        return s is not None

    # -- changes (the Users screen) -----------------------------------------
    def _change(self, fn):
        with self._lock:
            doc = json.loads(json.dumps(self._load()))
            if doc.get("broken"):
                raise ValueError(f"{self.error} Fix or remove the file "
                                 f"first.")
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
        if os.path.exists(self.path):
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
                        raise ValueError("An admin can always manage users.")
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
                "error": self.error}
