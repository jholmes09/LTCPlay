"""BENCH BUILD ONLY: the soak signs in the way an operator does.

Signed out is view only (PR #143, Jeff's field report 2026-10-28): with
users on, every press that is not Stop, Hold, Abort, Acknowledge or a
disarm needs a signed-in role, the rack screen included, and the Stream
Deck acts as whoever is signed in on the show machine. So the soak signs
in through the engine's real route, POST /api/remote/signin, from this
machine (loopback: a sign-in on the show machine itself), exactly as the
rack screen's sign-in card does, and sends the cookie it gets back with
every later press. Nothing in LTC Player is bypassed or switched off for
it: the role matrix, the users file and the sign-in rules are the real
ones.

Who: Andy, one of the names LTC Player seeds at first run with NO
password (users.SEED_USERS), and on the scheduler's default operator list
(ltcplay_operators.json: Andy, Jeff), so the same person signed in is the
Stream Deck's operator. The soak's engine keeps its settings in
the soak's own folder (soak.py, LOCALAPPDATA), so its users file is the
soak's own, never the show account's. The first sign-in claims the name
by choosing its password (users.Users.sign_in, `new_password`), which is
only allowed on the show machine itself, as here. The password is made up
for this run and kept in memory only; the users file holds its hash.

Sessions live in the engine's memory: an engine restarted after a crash,
an idle sign-out or the sign-out after the night's last show ends them.
A press refused because nobody is signed in ("role_refused") signs in
again and is sent once more; nothing was changed by the refused one.
"""
import json
import secrets
import threading
import urllib.error
import urllib.request

# Seeded (users.SEED_USERS) and on the default operator list.
USER = "Andy"
COOKIE = "ltcplay_user"          # users.COOKIE


def _cookie_from(headers):
    """The ltcplay_user token from a response's Set-Cookie, "" when it
    clears it, None when it does not mention it."""
    for v in (headers.get_all("Set-Cookie") or []) if headers else []:
        first = v.split(";", 1)[0].strip()
        k, _, tok = first.partition("=")
        if k.strip() == COOKIE:
            return tok.strip()
    return None


class SignedIn:
    """JSON over HTTP to the engine on this machine, as a signed-in page.

    `http(method, path, body=None, signed_out=False)` -> (status, doc).
    `signed_out=True` sends the press with no cookie at all, as a signed
    out screen does (Stop, Hold, Abort and the disarms must still work)."""

    def __init__(self, base, name=USER, note=print, timeout=5.0,
                 opener=None):
        self.base = base.rstrip("/")
        self.name = name
        self.note = note
        self.timeout = timeout
        self.password = secrets.token_urlsafe(18)
        self.token = ""
        self.sign_ins = 0
        self.refusals = 0             # presses refused for no sign-in
        self.last_error = ""
        self._lock = threading.Lock()
        self._open = opener or urllib.request.urlopen

    # -- the wire ---------------------------------------------------------
    def _send(self, method, path, body, token, timeout=None):
        """(status, doc, headers)."""
        data = None if method == "GET" else json.dumps(
            {} if body is None else body).encode()
        h = {"Content-Type": "application/json"}
        if token:
            h["Cookie"] = f"{COOKIE}={token}"
        req = urllib.request.Request(self.base + path, data=data, headers=h,
                                     method=method)
        try:
            with self._open(req, timeout=timeout or self.timeout) as r:
                return r.status, json.loads(r.read() or b"null"), r.headers
        except urllib.error.HTTPError as e:
            try:
                doc = json.loads(e.read() or b"null")
            except ValueError:
                doc = {"error": f"HTTP {e.code}"}
            return e.code, doc, e.headers
        except Exception as e:
            return 0, {"error": f"{type(e).__name__}: {e}"}, None

    # -- signing in and out -----------------------------------------------
    def sign_in(self):
        """Sign in at the engine's real route. The first time on a fresh
        users file the name has no password yet: this chooses it (the
        same request the sign-in card sends with "new_password"). True
        when signed in."""
        with self._lock:
            st, doc, hd = self._send("POST", "/api/remote/signin", {
                "name": self.name, "password": self.password,
                "new_password": self.password}, "")
            tok = _cookie_from(hd)
            if st == 200 and tok:
                self.token = tok
                self.sign_ins += 1
                self.last_error = ""
                self.note(f"signed in as {self.name} on this machine "
                          f"(sign-in {self.sign_ins})")
                return True
            self.token = ""
            self.last_error = str((doc or {}).get("error") or f"HTTP {st}") \
                if isinstance(doc, dict) else f"HTTP {st}"
            self.note(f"signing in as {self.name} failed: {self.last_error}")
            return False

    def sign_out(self):
        """Sign out at the engine (which signs this machine out: the rack
        screen and the Stream Deck together). True when it answered."""
        with self._lock:
            tok, self.token = self.token, ""
            st, _doc, _hd = self._send("POST", "/api/remote/signout", {}, tok)
        if st == 200:
            self.note(f"signed out ({self.name})")
        return st == 200

    # -- a press ------------------------------------------------------------
    def __call__(self, method, path, body=None, signed_out=False,
                 timeout=None):
        if signed_out:
            st, doc, _ = self._send(method, path, body, "", timeout)
            return st, doc
        if not self.token and method != "GET":
            self.sign_in()
        st, doc, _ = self._send(method, path, body, self.token, timeout)
        if st == 403 and isinstance(doc, dict) and doc.get("role_refused"):
            # The session is gone (an engine restart, an idle sign-out,
            # the sign-out after the last show): sign in again, once, and
            # press again. The refused press changed nothing.
            self.refusals += 1
            self.note(f"{path} refused as signed out ({doc.get('error')}); "
                      f"signing in again")
            if self.sign_in():
                st, doc, _ = self._send(method, path, body, self.token,
                                        timeout)
        return st, doc


def self_test():
    """Yields one line per check passed (soak.py --self-test)."""
    import io

    class Resp(io.BytesIO):
        def __init__(self, status, doc, cookie=None):
            super().__init__(json.dumps(doc).encode())
            self.status = status
            self.headers = _Headers(cookie)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()

    class _Headers:
        def __init__(self, cookie):
            self.c = cookie

        def get_all(self, k, default=None):
            if k == "Set-Cookie" and self.c is not None:
                return [f"{COOKIE}={self.c}; HttpOnly; Path=/"]
            return default

    eng = {"pw": None, "sessions": set(), "calls": []}

    def opener(req, timeout=None):
        path = req.full_url.split("127.0.0.1:1", 1)[1]
        body = json.loads(req.data) if req.data else None
        tok = (req.headers.get("Cookie") or "").partition("=")[2]
        eng["calls"].append((path, tok))
        if path == "/api/remote/signin":
            assert body["name"] == USER and len(body["password"]) >= 6
            if eng["pw"] is None:
                eng["pw"] = body["new_password"]
            elif body["password"] != eng["pw"]:
                raise urllib.error.HTTPError(req.full_url, 401, "no", None,
                                             io.BytesIO(b'{"error":"x"}'))
            t = secrets.token_urlsafe(16)
            eng["sessions"].add(t)
            return Resp(200, {"ok": True}, t)
        if path == "/api/remote/signout":
            eng["sessions"].clear()
            return Resp(200, {"ok": True}, "")
        if path in ("/api/start", "/api/remote/resume") and \
                tok not in eng["sessions"]:
            raise urllib.error.HTTPError(
                req.full_url, 403, "no", None, io.BytesIO(json.dumps(
                    {"error": "Nobody is signed in", "role_refused": True}
                ).encode()))
        return Resp(200, {"ok": True})

    s = SignedIn("http://127.0.0.1:1", note=lambda t: None, opener=opener)
    assert s("POST", "/api/start", {"timeline": "x"}) == (200, {"ok": True})
    assert s.sign_ins == 1 and eng["pw"] == s.password and s.token
    eng["sessions"].clear()                       # an engine restart
    assert s("POST", "/api/remote/resume", {})[0] == 200
    assert s.sign_ins == 2 and s.refusals == 1
    assert s("POST", "/api/remote/hold", {}, signed_out=True)[0] == 200
    assert eng["calls"][-1] == ("/api/remote/hold", ""), \
        "a signed-out press carries no cookie"
    assert s.sign_out() and not s.token and not eng["sessions"]
    yield ("the soak signs in at the real sign-in route (choosing the "
           "seeded name's password the first time), keeps the cookie, "
           "signs in again when a press is refused as signed out (an "
           "engine restart), and can press signed out")


if __name__ == "__main__":
    for line in self_test():
        print("ok:", line)
