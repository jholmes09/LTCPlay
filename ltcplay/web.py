"""A local web page for running the show, and the small server behind it.

The important decision here is that the browser is a VIEW, not the program.
The engine runs in this process; the page polls it. Close the tab, put the
laptop lid down on the page, lose wifi on the iPad you were watching from, and
the show keeps running, because none of those things are where the show lives.
A web UI that owns the engine would add a whole new way to lose a show, which
is the opposite of the point.

No dependencies beyond the standard library, so nothing new can fail to install
the night before.
"""
import json
import math
import os
import re
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import appdata
from . import audio as audio_mod
from . import brand as brand_mod
from . import settings as settings_mod
from . import timeline as timeline_mod
from .ltc import LTCDecoder
from . import benchhooks
from .session import Session, SessionError

# A device that is not attached, a channel that does not exist, a show file
# with a bad line in it: all of these are the caller asking for something that
# is not there, which is a 400. A 500 should mean the program broke, so that
# the page can tell "you picked the wrong thing" from "something is wrong with
# ltcplay" without reading the text.
USER_ERRORS = (SessionError, audio_mod.DeviceError, ValueError,
               FileNotFoundError)

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "web", "index.html")
REMOTE_PAGE = os.path.join(HERE, "web", "remote.html")
# Interface 2.0 (Jeff, 2026-10-28), beside 1.0, never instead of it.
# /remote serves 1.0 unless this screen chose 2.0 with its own cookie (set
# by /ui?v=2, cleared back by /ui?v=1), or the show machine's default says
# 2 (remote.ui_default, 1 until Jeff sets it). /remote1 and /remote2 are
# always that page. /remote-logic.js is 1.0's own pure rules (remote.html's
# remote-logic block, cut out at each request), so both pages run one set.
REMOTE2_PAGE = os.path.join(HERE, "web", "remote2.html")
UI_COOKIE = "ltcplay_ui"
UI_ROUTES = ("/remote1", "/remote2", "/remote-logic.js", "/ui")
LOGIC_OPEN = '<script id="remote-logic">'
LOGIC_TAG = '<script src="/remote-logic.js"></script>'


def remote_logic():
    """1.0's pure rules (remote.html's remote-logic block), or None."""
    with open(REMOTE_PAGE, encoding="utf-8") as fh:
        page = fh.read()
    a = page.find(LOGIC_OPEN)
    b = page.find("</script>", a) if a >= 0 else -1
    if a < 0 or b < 0:
        return None
    return page[a + len(LOGIC_OPEN):b]


def remote2_page():
    """remote2.html with 1.0's rules written into it (safety review of
    #166, B1): the page arrives whole in one answer, never waiting on a
    second request for its rules. Without the block, the page as it is
    (its own tag then asks /remote-logic.js, and it says when that fails;
    its Abort needs neither)."""
    with open(REMOTE2_PAGE, encoding="utf-8") as fh:
        page = fh.read()
    logic = remote_logic()
    if logic is not None and LOGIC_TAG in page:
        page = page.replace(LOGIC_TAG, LOGIC_OPEN + logic + "</script>", 1)
    return page.encode("utf-8")
# The pages' icons (tab, iPad home screen, installed web app), made from the
# LTC Player app icon by tools/make_web_icons.py. A fixed list of files at
# fixed addresses: nothing here reads a path the request names.
ICONS_DIR = os.path.join(HERE, "web", "icons")
ICONS = {
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    "/apple-touch-icon-precomposed.png": ("apple-touch-icon.png",
                                          "image/png"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/manifest.webmanifest": ("manifest.webmanifest",
                              "application/manifest+json"),
}
# They change only with a new build, so a browser may keep them a month.
ICON_CACHE = "public, max-age=2592000"
LOOPBACK = ("127.0.0.1", "::1", "localhost")
WILDCARD = ("0.0.0.0", "::", "")

# The show's own transport and output routes. While a scheduled show is live
# (the scheduler in SHOW or PAUSED) every one of them is refused, on every
# door, the machine's own page included: Hold, Resume and Abort are the only
# presses a live show takes (PR #43 review, finding 2: a GO during a live
# show moved the pixels and fired a flame cue it jumped into).
LIVE_SHOW_REFUSED = ("/api/start", "/api/go", "/api/skip", "/api/release",
                     "/api/stop", "/api/override", "/api/reload",
                     "/api/showdir", "/api/reinput", "/api/input",
                     "/api/trigger", "/api/autoreload", "/api/find")
LIVE_SHOW_STATES = ("SHOW", "PAUSED")
# After an Abort, until Reset (PR #43 fix round 2, E): the same routes are
# refused, except Stop and a Blackout, which only make the rig darker. The
# page used to bring the show's pixels back up while the Abort stood.
LATCHED_ALLOWED = ("/api/stop",)
LATCHED_REFUSAL = ("The show was aborted. Nothing on this page may bring "
                   "the rig back up until someone presses Reset; Stop and "
                   "Blackout still work.")
# A halt that ended a live show (the Stream Deck's Stop) waits at most this
# long for the conductor's calls, and again for its fade, before playback
# stops (review of #130, 5). The deck's post waits STOP_POST_TIMEOUT_S.
HALT_FADE_WAIT_S = 3.0
LIVE_SHOW_REFUSAL = ("A scheduled show is live. Only Hold, Resume and Abort "
                     "work during a show; this waits until the show has "
                     "ended or been aborted.")


HOST_REFUSAL = "This page has to be opened by the show machine's address."


def wrong_host_page(bind, port, local):
    """The short page a browser gets when the page was opened by another
    name for this PC (the Host check in _refusal). It names only this
    server's own address, never anything from the request."""
    import html as _html
    if local or bind in LOOPBACK:
        addr = "127.0.0.1"
    else:
        addr = f"[{bind}]" if ":" in str(bind) else str(bind)
    url = _html.escape(f"http://{addr}:{int(port)}/")
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,"
            "initial-scale=1\"><title>LTC Player</title><style>body{"
            "font:20px/1.4 system-ui,sans-serif;margin:40px 16px;color:#111;"
            "background:#fff}a{font-weight:bold}</style></head><body>"
            "<h1>Open this page by its address</h1>"
            f"<p>Open this page at <a href=\"{url}\">{url}</a> instead.</p>"
            "<p>LTC Player answers only to the show PC's own address, not "
            "to another name for it.</p></body></html>")


def normalize_bind(bind):
    """The address bind() will really use, written plainly: "127.0.0.1"
    for any loopback spelling, "0.0.0.0" (refused by serve) for any
    every-interface spelling, else the address itself. A name that does
    not resolve is left as it is, for bind() to refuse."""
    import ipaddress
    import socket as _socket
    text = str(bind if bind is not None else "").strip()
    if text in ("", "::", "0.0.0.0"):
        return "0.0.0.0" if text != "::" else "::"
    try:
        # inet_aton reads every IPv4 spelling the operating system does
        # ("0", "0.0", "000.000.000.000"); getaddrinfo the rest.
        addrs = {ipaddress.ip_address(_socket.inet_ntoa(
            _socket.inet_aton(text)))}
    except (OSError, ValueError):
        try:
            infos = _socket.getaddrinfo(text.strip("[]"), None,
                                        type=_socket.SOCK_STREAM)
        except (OSError, UnicodeError):
            return text
        addrs = {ipaddress.ip_address(i[4][0].split("%")[0])
                 for i in infos}
    if any(a.is_unspecified for a in addrs):
        return "0.0.0.0"
    if addrs and all(a.is_loopback for a in addrs):
        return "127.0.0.1"
    if len(addrs) == 1:
        return str(next(iter(addrs)))
    return text


def network_may_reach(route):
    """Fix round 1 of #39, F1: what a request from the network may reach
    at all. The remote page and its own routes, the logo, the pages'
    icons, and nothing else: the operator page's API (Stop, Start, GO, the
    input, the show folder) and the scheduler's routes (choosing the
    operator, the Stream Deck's journal lines) are the machine's own, and
    a network session reaching them bypassed every rule /api/remote/*
    keeps (who pressed, no scrubbing during a show). Loopback is unchanged."""
    return (route in ("/", "/index.html", "/remote", "/api/brand")
            or route in UI_ROUTES
            or route.startswith("/brand/") or route in ICONS
            or route == "/api/remote" or route.startswith("/api/remote/"))


# Bumped whenever the page needs something this module did not have. The
# page carries the same number; a mismatch means one of the two is stale.
API = 7


# Files this program writes into the show folder. None of them is a show.
OURS = frozenset((settings_mod.FILENAME, settings_mod.PREFS_FILE,
                  "ltcplay_verified.json"))


def _looks_like_a_show(path):
    """A JSON file with a 'cues' list is a show; anything else is not ours
    to offer as one."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    except Exception:
        return False
    return isinstance(doc, dict) and isinstance(doc.get("cues"), list)


class Control:
    """Holds at most one running show, and the answers the page needs.

    One show at a time, guarded by a lock, because two players sending to the
    same universes would fight frame by frame and the result on the rig would
    look like a hardware fault."""

    # How often state() is allowed to actually rebuild, in seconds. The page
    # polls every 250ms (ltcplay/web/index.html's `setInterval(poll, 250)`),
    # so this keeps every viewer's freshness the same as it always was, one
    # poll cycle old at worst; a second or fifth viewer just reads the same
    # answer instead of paying to build another one. See state() below.
    STATE_CACHE_S = 0.2

    def __init__(self, folder, defaults=None, sd=None):
        self.folder = folder
        self.defaults = defaults or {}
        self.session = None
        self.last_timeline = None
        self.lock = threading.Lock()
        self._starting = False
        self._start_gen = 0
        self.last_error = ""
        self._sd = sd
        # -- state() cache: see the method itself for why this exists.
        self._state_cache = None            # (payload, built_at) or None
        self._state_building = threading.Lock()

    # -- audio ------------------------------------------------------------
    def sd(self):
        if self._sd is None:
            from .session import _import_sounddevice
            self._sd = _import_sounddevice()
        return self._sd

    def devices(self):
        try:
            ins = audio_mod.list_inputs(self.sd())
        except SessionError as e:
            return {"error": str(e), "inputs": [], "candidates": []}
        return {"error": None,
                "inputs": ins,
                "candidates": [d["index"] for d in audio_mod.candidates(ins)],
                "saved": settings_mod.load()}

    def find(self, seconds=3.0):
        try:
            sd = self.sd()
        except SessionError as e:
            return {"error": str(e), "devices": []}
        out = []
        for res in audio_mod.scan(sd, LTCDecoder, seconds=seconds):
            out.append({
                "name": res["device"]["name"],
                "index": res["device"]["index"],
                "kind": res["device"].get("kind", "hardware"),
                "error": res["error"],
                "channels": res.get("channels", []),
            })
        return {"error": None, "devices": out}

    def set_input(self, device, channel, rate=None):
        """Save the input, and point a running show at it too.

        Stopping the engine to fix a cable blacks the rig out in front of an
        audience over something that has nothing to do with what is on the
        trees. Asked for by Jeff, 2026-09-14."""
        if not device:
            settings_mod.clear()
            out = settings_mod.load()
        else:
            sd = self.sd()
            dev = audio_mod.resolve_device(sd, device)
            ch = int(channel or 1)
            if ch > dev["channels"]:
                raise SessionError(f"{dev['name']} has {dev['channels']} "
                                   f"input(s), so there is no input {ch}.")
            settings_mod.save(dev["name"], ch, rate)
            out = settings_mod.load()
        s = self.session
        if s is not None and s.running:
            # A failure here must not lose the saved setting above: the next
            # start has to use it even if this show cannot.
            try:
                opened = s.retarget_input(device or None, channel, rate)
                out = dict(out, live=True, opened=bool(opened),
                           input=s.input_summary)
            except SessionError as e:
                out = dict(out, live=False, why=str(e))
        return out

    # -- shows ------------------------------------------------------------
    def timelines(self):
        import glob
        out = []
        # Escaped: a show folder named "Fire [2026]" is a name, not a
        # pattern (review of #137).
        pattern = os.path.join(glob.escape(self.folder), "*.json")
        for p in sorted(glob.glob(pattern)):
            base = os.path.basename(p)
            # ltcplay's own files are not shows. They used to be listed,
            # they sort before the real ones, and the picker defaulted to
            # `ltcplay_prefs.json` -- so the first thing an operator pressed
            # gave a schema error. Anything that does not parse as a show with
            # cues is left out of the list entirely rather than offered.
            if base.startswith("_superseded") or base in OURS:
                continue
            if "conflicted copy" in base:
                # Dropbox leaves these beside the real file, with the same
                # show name inside. Offering both is how the wrong one runs.
                out.append({"file": base, "name": base, "cues": None,
                            "error": "A Dropbox conflicted copy. Delete it or "
                                     "move it out of this folder."})
                continue
            entry = {"file": base, "name": base, "cues": None, "error": None}
            try:
                tl = timeline_mod.Timeline.load(p)
                entry["name"] = tl.name or base
                entry["cues"] = len(tl.cues)
                entry["rate"] = tl.rate_label
                entry["note"] = tl.show_dir_note
            except Exception as e:
                entry["error"] = str(e)
            if entry["error"] and not _looks_like_a_show(p):
                continue          # one of ours, or not a show file at all
            out.append(entry)
        return out

    def check(self, timeline):
        path = os.path.join(self.folder, os.path.basename(timeline))
        # The caller's defaults come first and the validation-only settings win,
        # so a default that happens to name one of them cannot collide.
        kw = dict(self.defaults)
        kw.update(no_output=True, no_log=True, sd=self._sd)
        s = Session(path, **kw)
        try:
            s.open()
        except SessionError as e:
            return {"ok": False, "error": str(e), "cues": [], "problems": [],
                    "notes": []}
        cues = []
        prev_end = None
        for c in s.tl.cues:
            if c.fseq is None:
                cues.append({"tc": c.tc_text, "name": c.name, "missing": True})
                continue
            end = c.tc_seconds + c.duration
            over = (prev_end is not None and
                    c.tc_seconds < prev_end - 2 * (c.fseq.step_time_ms / 1000.0))
            prev_end = end
            cues.append({"tc": c.tc_text, "name": c.name, "missing": False,
                         "duration": c.duration,
                         "ends": s.tl.format(end),
                         "channels": c.fseq.channel_count,
                         "step": c.fseq.step_time_ms,
                         "overlap": over})
        out = {"ok": not s.problems, "error": None, "cues": cues,
               "problems": s.problems, "notes": s.notes,
               "networks": s.nm_path, "summary": s.nm.summary(),
               "input": s.input_summary}
        s.stop()
        return out

    def override(self, what):
        """Force the rig to a look, or hand it back to the timecode.

        This is the preshow button. It has to work before any timecode has
        ever arrived, which is why it sets a flag on the player rather than
        going through the cue machinery."""
        if what not in (None, "", "auto", "preshow", "blackout"):
            raise SessionError(f"{what!r} is not one of: preshow, blackout, "
                               f"auto")
        s = self.session
        if s is None or not s.running or s.player is None:
            raise SessionError("Nothing is running, so there is nothing to "
                               "put a look on. Start the show first.")
        if what == "preshow" and s.player.idle_cue is None:
            raise SessionError("This show file has no preshow sequence, so "
                               "there is no look to hold. Set \"idle\" in the "
                               "show file to a .fseq in the show folder.")
        # A show start's hand-over still waiting for its first frame
        # (fire_ice pixels_for_show) ends: a look set now stands, even the
        # same black the start held (review B of PR #171).
        cancel = getattr(s.player, "cancel_show_handoff", None)
        if cancel is not None:
            cancel()
        s.player.override = None if what in (None, "", "auto") else what
        if s.log:
            s.log.event("override", f"operator set output to "
                                    f"{s.player.override or 'auto'}")
        return s.player.override

    def set_auto_reload(self, on):
        """Turn automatic pick-up of re-renders on or off, right now.

        Live, not at the next start: the supervisor reads this flag every
        half second. A setting you cannot change while the thing is running
        forces the interface to talk about "this run", which is a distinction
        the operator never asked for and cannot see.
        """
        on = bool(on)
        settings_mod.save_pref("auto_reload", on)
        s = self.session
        if s is not None and s.running and s.player is not None:
            s.player.auto_reload = on
            if s.log:
                s.log.event("reload-mode",
                            "re-renders load automatically" if on else
                            "re-renders wait for the Reload button")
        return on

    def go(self, at=None):
        """GO: run the show from a point, on this machine's clock."""
        s = self.session
        if s is None or not s.running or s.player is None:
            raise SessionError("Nothing is running, so there is nothing to "
                               "run from. Start the show first.")
        tl = s.tl
        if at in (None, "", "here"):
            tc = s.player.tc_seconds
            if tc is None or tc < 0:
                raise SessionError(
                    "There is no clock to carry on from: no timecode has "
                    "been read yet. Give a timecode to start at, or the name "
                    "of a cue.")
        else:
            at = str(at).strip()
            match = [c for c in tl.cues
                     if c.name.lower() == at.lower() or c.tc_text == at]
            if match:
                tc = match[0].tc_seconds
            else:
                try:
                    tc = tl.parse(at)
                except ValueError as e:
                    raise SessionError(str(e))
        s.player.go(tc)
        return {"freerun": True, "at": tl.format(tc)}

    def show_folder(self, timeline, folder=None):
        """Read, or change, the folder a show file plays its sequences from.

        Without this the only way to point a show at a new render folder was
        to hand-edit JSON: the page lists show FILES beside the launcher, but
        where those files read their sequences from lives inside them. Asked
        for by Jeff, 2026-09-13.
        """
        from .cli import check_show_folder, write_json
        path = os.path.join(self.folder, os.path.basename(timeline or ""))
        if not os.path.exists(path):
            raise SessionError(f"No such show file: {timeline}")
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
        if folder is None:
            # Resolve it the way the LOADER does. A bundle deliberately stores
            # a relative "show", and reporting that raw string made the page
            # offer the same folder twice: once as "show" and once as the
            # absolute path the option scan found. It also meant the check ran
            # against the working directory rather than the show file. Jeff
            # saw the double entry in the bundle, 2026-09-14.
            stored = doc.get("show_dir") or ""
            here = self._resolved_show_dir(stored, path)
            ok, why, counts = check_show_folder(here) if here else (
                False, "This show file does not name a folder.",
                {"fseq": 0, "networks": False})
            return {"folder": here, "stored": stored, "ok": ok, "why": why,
                    "sequences": counts["fseq"], "map": counts["networks"],
                    "options": self._folder_options(here)}
        if self._starting or (self.session is not None
                              and self.session.running):
            raise SessionError("A show is running. Stop it before changing "
                               "which folder it plays from.")
        ok, why, counts = check_show_folder(folder)
        if not ok:
            raise SessionError(why)
        chosen = os.path.abspath(os.path.expanduser(folder))
        # A folder that lives inside the launcher folder is stored RELATIVE,
        # which is what makes a bundle carryable: writing an absolute path
        # here would pin the bundle to this Mac the first time somebody used
        # the picker on it.
        inside = os.path.join(os.path.abspath(self.folder), "")
        doc["show_dir"] = (os.path.relpath(chosen, os.path.abspath(self.folder))
                           if chosen.startswith(inside) else chosen)
        write_json(path, doc)          # never leave a half-written show file
        return {"folder": chosen, "stored": doc["show_dir"], "ok": True,
                "why": "", "sequences": counts["fseq"],
                "map": counts["networks"], "changed": True,
                "options": self._folder_options(chosen)}

    def _resolved_show_dir(self, stored, show_path):
        """Where a show file's sequences actually are, absolute.

        Uses the loader's own resolver so the page and the engine cannot
        disagree about which folder a show plays from."""
        if not stored:
            return ""
        try:
            folder, _note = timeline_mod.resolve_show_dir(stored, show_path)
            return folder
        except Exception:
            # Not there. Report the path the loader WOULD look at, so the
            # message names something the operator can go and find.
            if os.path.isabs(stored):
                return stored
            return os.path.normpath(
                os.path.join(os.path.dirname(os.path.abspath(show_path)),
                             stored))

    def _folder_options(self, current):
        """Folders on this Mac that a show could actually play from.

        Cheap on purpose: the ones beside the current show folder, and the
        ones beside the launcher. Walking the disk for .fseq files would take
        minutes and find last season."""
        from .cli import check_show_folder
        seen, out = set(), []
        roots = []
        if current:
            roots.append(os.path.dirname(os.path.abspath(current)))
        roots.append(self.folder)
        for r in roots:
            try:
                names = sorted(os.listdir(r))
            except OSError:
                continue
            for n in names:
                d = os.path.join(r, n)
                if not os.path.isdir(d) or d in seen:
                    continue
                seen.add(d)
                ok, _why, counts = check_show_folder(d)
                if ok:
                    out.append({"folder": d, "sequences": counts["fseq"]})
        if current and os.path.isdir(current) and \
                not any(o["folder"] == os.path.abspath(current) for o in out):
            ok, _w, counts = check_show_folder(current)
            if ok:
                out.insert(0, {"folder": os.path.abspath(current),
                               "sequences": counts["fseq"]})
        return out[:40]

    def reset_input(self):
        """Rebuild the timecode input without interrupting the rig."""
        se = self.session
        if se is None or not se.running:
            raise SessionError("Nothing is running.")
        return se.reset_input()

    def set_trigger(self, on):
        """Hand the six Advateks over to their own recorded scenes, or take
        them back. The alternate playback mode; sequences play directly with
        this left off."""
        se = self.session
        if se is None or not se.running:
            raise SessionError("Nothing is running.")
        armed, msg = se.arm_trigger(bool(on))
        if not armed and bool(on):
            raise SessionError(msg or "Could not arm the scene triggers.")
        return {"trigger_armed": armed, "message": msg}

    def skip(self, seconds=None, cue=None):
        """Move a free run: by seconds, or to a cue."""
        s = self.session
        if s is None or not s.running or s.player is None:
            raise SessionError("Nothing is running.")
        try:
            if cue is not None:
                target = s.player.go_to_cue(int(cue))
                at = target.tc_seconds
                where = f"{target.name} at {target.tc_text}"
            else:
                at = s.player.nudge(float(seconds or 0))
                where = s.tl.format(at)
        except ValueError as e:
            raise SessionError(str(e))
        return {"freerun": True, "at": s.tl.format(at), "where": where}

    def release(self):
        s = self.session
        if s is None or not s.running or s.player is None:
            raise SessionError("Nothing is running.")
        was_free = s.player.freerun_epoch is not None
        released = s.player.release()
        # Say what actually happened to the rig, and do not claim the show is
        # following timecode when no timecode is arriving.
        feed = s.player.feed_state if was_free else s.player.state
        return {"freerun": False, "released": released,
                "feed": feed,
                "following": feed == "LOCKED"}

    def reload(self, only=None):
        """Swap in re-rendered sequences without stopping the chase."""
        from .player import ReloadError
        s = self.session
        if s is None or not s.running or s.player is None:
            raise SessionError("Nothing is running, so there is nothing to "
                               "reload. Start the show first.")
        names = None
        if only:
            names = {os.path.basename(str(n)) for n in only}
        try:
            return s.player.reload(names)
        except ReloadError as e:
            raise SessionError(
                "Nothing was reloaded and the show is still playing what it "
                "was. The usual cause is xLights still writing the render; "
                "wait for it to finish and press it again.\n  "
                + "\n  ".join(e.errors))

    def start(self, timeline, no_output=False, on_lost=None,
              allow_missing=False, auto_reload=None):
        # The lock is held only long enough to claim the slot. Opening a show
        # reads 22 files and opens an audio device, and a CoreAudio device
        # that wedges can block that forever: holding the lock across it froze
        # every other route, including Stop, and the page could not even say
        # what was wrong. Found by an adversarial audit, 2026-09-13.
        with self.lock:
            if self._starting:
                raise SessionError("A show is already being started. Give it "
                                   "a moment; if it never finishes, the audio "
                                   "device is not answering and this window "
                                   "has to be closed.")
            if self.session is not None and self.session.running:
                raise SessionError("A show is already running. Stop it first; "
                                   "two players sending to the same universes "
                                   "fight frame by frame and the rig looks "
                                   "broken.")
            self._starting = True
            self._start_gen += 1
            mine = self._start_gen
        try:
            return self._start(timeline, no_output, on_lost, allow_missing,
                               auto_reload)
        finally:
            with self.lock:
                # Only clear the flag if it is still OUR start. A start that
                # wedged inside the audio open must not unpick a start the
                # operator began after stopping it.
                if self._start_gen == mine:
                    self._starting = False

    def _start(self, timeline, no_output, on_lost, allow_missing, auto_reload):
        # Bench test hooks set outside the soak (safety audit of
        # bench-build, P1): refused before anything is opened.
        why = benchhooks.run_refusal()
        if why:
            print(f"ltcplay: {why}", flush=True)
            raise SessionError(why)
        path = os.path.join(self.folder, os.path.basename(timeline))
        if not os.path.exists(path):
            raise SessionError(f"No such show file: {timeline}")
        kw = dict(self.defaults)
        if auto_reload is None:
            auto_reload = settings_mod.load_prefs()["auto_reload"]
        kw.update(no_output=no_output, sd=self._sd,
                  allow_missing=bool(allow_missing),
                  auto_reload=bool(auto_reload))
        if on_lost:
            kw["on_lost"] = on_lost
        check = getattr(self, "before_open", None)
        if check is not None:
            # Fire & Ice (fire_ice.py): a show that must not run, refused
            # before anything is opened.
            extra = check(path)
            if isinstance(extra, dict):
                kw.update(extra)
        s = Session(path, **kw)
        s.from_web = True
        s.open()
        # Store it BEFORE starting. Session.start brings the output
        # thread up and THEN opens the audio device, and an audio open
        # that wedges (rather than failing) left the rig driven by an
        # engine the page could not see and Stop could not find. Round 2
        # of the audit reproduced it: the page said idle while 1,500
        # packets a second went to the rig. Found 2026-09-13.
        with self.lock:
            self.session = s
        try:
            s.start()
        except Exception:
            with self.lock:
                if self.session is s:
                    self.session = None
            raise
        self.last_error = ""
        # The show file last Run (2026-10-23): the Art-Net receiver check
        # judges this one when nothing is loaded (remote._active_show_files).
        self.last_timeline = os.path.basename(path)
        if s.wav:
            threading.Thread(target=s.pump_wav, daemon=True,
                             name="ltcplay-wav").start()
        return s

    def stop(self):
        # Never hold the lock across Session.stop: closing an audio device
        # that is wedged would take every other route down with it, and Stop
        # is the one that has to work when things are wrong.
        with self.lock:
            s, self.session = self.session, None
            # Stop means stop. A start that wedged inside the audio open never
            # returns, so the `finally` that clears this flag never runs, and
            # every later Start was refused until the window was closed --
            # after the operator had already blacked the rig out and wanted it
            # back. Round 4 of the audit, 2026-09-13.
            # Stop means stop. A start wedged inside the audio open
            # never returns, so its own cleanup never ran, and every
            # later Start was refused until the window was closed --
            # after the operator had blacked the rig out and wanted it
            # back. Round 4 of the audit, 2026-09-13.
            self._starting = False
            self._start_gen += 1
        if s is not None:
            s.stop()
        return s is not None

    def state(self):
        """What the page's poll asks for, served from a cache at most
        STATE_CACHE_S old.

        Building this calls Session.snapshot(), which calls into
        version_mod twice for the build id -- a full SHA-256 of every file
        this program ships, read off disk, on every single call. That is
        cheap once. It is not cheap on every poll from every device that
        happens to have the page open: profiled 2026-09-26 against
        bench/bench_report_2026-09-25.md's B11 (a real show PC bench, one
        viewer costs nothing measurable, five to ten cost the pixel loop
        real frames), a single /api/state call spent the great majority of
        its own time in that hash, and it runs on the SAME interpreter as
        the pixel-sending thread -- so a second viewer, or a stray tablet
        left open on a shelf, competes with the show for the interpreter
        lock, purely to answer a question nobody needed answered again yet.

        So: build it at most once every STATE_CACHE_S, and hand every asker
        in that window the same answer. The page already polls every 250ms
        and redraws from whatever it is given; it cannot tell an answer
        that is a few hundred milliseconds old from one built the instant
        it asked, and it does not need to -- the rig runs off the engine's
        own clock, never off what the page has drawn.

        Concurrent misses do not pile up: if a rebuild is already under way
        when this is called, the caller gets whatever is cached rather than
        starting a second build of the same answer (or, on the very first
        call ever, waits for the one in flight instead of racing it)."""
        now = time.monotonic()
        cached = self._state_cache
        if cached is not None and now - cached[1] < self.STATE_CACHE_S:
            return cached[0]
        if not self._state_building.acquire(blocking=False):
            cached = self._state_cache
            if cached is not None:
                # Someone else is building this instant. Their answer is
                # about to be at least as fresh as one started now, so read
                # it instead of doing the same work twice at once.
                return cached[0]
            self._state_building.acquire()  # first call ever: wait it out
        try:
            cached = self._state_cache
            now = time.monotonic()
            if cached is not None and now - cached[1] < self.STATE_CACHE_S:
                return cached[0]             # built while this waited for the lock
            fresh = self._build_state()
            self._state_cache = (fresh, time.monotonic())
            return fresh
        finally:
            self._state_building.release()

    def _build_state(self):
        """The actual answer, built fresh. Never called more than once per
        STATE_CACHE_S across every thread; see state() above."""
        s = self.session
        if self._starting and (s is None or not s.running):
            # Mid-start. The engine may already be sending, so the page has
            # to show Stop rather than an idle panel with no way out.
            return {"running": False, "starting": True,
                    "last_error": self.last_error, "api": API,
                    "saved_input": settings_mod.load(),
                    "auto_reload": settings_mod.load_prefs()["auto_reload"]}
        if s is None or not s.running:
            return {"running": False, "last_error": self.last_error,
                    "api": API, "saved_input": settings_mod.load(),
                    "auto_reload": settings_mod.load_prefs()["auto_reload"]}
        try:
            snap = s.snapshot()
        except Exception as e:
            return {"running": True, "error": f"{type(e).__name__}: {e}",
                    "saved_input": settings_mod.load()}
        snap["saved_input"] = settings_mod.load()
        # The page is read off disk on every load; this module is read into
        # memory once, when the server starts. Ship a new button and the
        # browser has it while the server does not, and the button 404s with
        # no clue why. So the page checks this number against its own and
        # says "restart the server" instead.
        snap["api"] = API
        return snap

    def log_tail(self, n=120):
        s = self.session
        p = (s.log.path if s and s.log else
             appdata.log_path() if appdata.WINDOWS else
             os.path.join(self.folder, "ltcplay.log"))
        if not os.path.exists(p):
            return []
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                return fh.read().splitlines()[-int(n):]
        except OSError:
            return []


class Handler(BaseHTTPRequestHandler):
    server_version = "ltcplay"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):
        pass                      # the show log is the log; this is noise

    def _local(self):
        """The request came from this machine itself. The one thing a proxy
        or tunnel on the show machine would fake, which is why _refusal()
        turns away anything that looks forwarded, loopback included."""
        return self.client_address[0] in LOOPBACK

    def _ctx(self):
        """Where this request came from (ltcplay/remote.py Ctx): the
        machine itself or a device on the show network, and who is signed
        in on that page. Being the machine itself is not a sign-in
        (2026-10-28): with users on, _role_refused asks the role matrix for
        the rack screen too. network_may_reach() is what keeps a device on
        the network to the remote page's own routes."""
        ctx = getattr(self, "_ctx_cache", None)
        if ctx is None:
            from . import remote as remote_mod
            ctx = remote_mod.Ctx(self._local(), self.client_address[0])
            # Who is signed in on this page, if anyone (users.py). Nobody
            # has to be; a cookie that names no live session is nobody.
            users = getattr(getattr(self.server, "remote", None), "users",
                            None)
            win = str(self.headers.get("X-LTC-Window") or "")
            ctx.window = win if re.match(r"^[A-Za-z0-9-]{1,24}$", win) \
                else ""
            if users is not None:
                try:
                    tok = remote_mod.cookie_token(self.headers.get("Cookie"))
                    ctx.token = tok
                    ctx.session = users.session(tok) if tok else None
                except Exception:                # noqa: BLE001
                    ctx.session = None
            self._ctx_cache = ctx
        return ctx

    def _halt_asked(self):
        """The Stream Deck's Stop (Jeff, 2026-10-26): a complete halt of
        everything, taken during a scheduled show too. Only a POST from
        this machine with "halt": true (the deck process, over loopback);
        the Rack screen's own Stop button does not ask for it and is still
        refused during a show."""
        try:
            body = self._body()
        except Exception:                        # noqa: BLE001
            return False
        return self._local() and isinstance(body, dict) and \
            body.get("halt") is True

    def _authed_who(self, body):
        """With users on (users.py): the signed-in name this press is from
        (the page's own session, or for the Stream Deck process whoever is
        signed in on this machine), or "unsigned". None without users, and
        then the old rules name the press. Never a name from the body
        (Jeff's field report, 2026-10-28)."""
        rm = getattr(self.server, "remote", None)
        fn = getattr(rm, "users_active", None)
        try:
            if fn is None or not fn():
                return None
            return rm.signed_in_name(self._ctx(), body)
        except Exception:                        # noqa: BLE001
            from . import users as users_mod
            return users_mod.UNSIGNED

    # The Stream Deck process's own journal lines name the deck's own
    # presses ("Stream Deck", and the purge it arms for the "Morning page")
    # as whoever is signed in on this machine; a line it writes about a
    # screen's hold or disarm carries the name the engine gave that hold.
    DECK_OWN_SCREENS = ("Stream Deck", "Morning page")

    def _authed_body(self, route, body):
        """The body with "who" set to the signed-in user (_authed_who) when
        users are on (2026-10-28): the Stream Deck's journal lines for its
        own keys, an incident, an announcement. The operator route is never
        passed here: its "who" is the name being chosen, not who chose it.
        A deck line with no name (a system line) stays one."""
        if route == "/api/schedule/deck-event":
            said = str(body.get("who") or "").strip()
            if not said:
                return body
            if str(body.get("screen") or "Stream Deck") in \
                    self.DECK_OWN_SCREENS:
                who = self._authed_who(dict(body, screen="Stream Deck"))
            else:
                # A line about a screen's hold or disarm: written as the
                # deck, never a name from the body (review of PR #143, 6).
                who = self._deck_screen_who(said)
        else:
            who = self._authed_who(body)
        if who is None:
            return body
        body = dict(body, who=who)
        self._body_cache = body
        return body

    # Who a Stream Deck journal line about a screen is written as, with
    # users on: the deck process, never a person's name from its body
    # (re-review of PR #143, 5). The screen's own line, from the engine,
    # already names who held or pressed.
    DECK_SCREEN_WHO = "deck"

    def _deck_screen_who(self, said):
        """DECK_SCREEN_WHO when users are on (or cannot be asked); None
        without users, and the old rules name the line."""
        rm = getattr(self.server, "remote", None)
        fn = getattr(rm, "users_active", None)
        try:
            if fn is None or not fn():
                return None
        except Exception:                        # noqa: BLE001
            pass
        return self.DECK_SCREEN_WHO

    def _who_where(self, body):
        """(who, screen) for a Stop's journal and audit lines."""
        body = body if isinstance(body, dict) else {}
        try:
            ctx = self._ctx()
            who = self._authed_who(body)
            if who is None:
                who = str(body.get("who") or getattr(ctx, "user", "") or "")
            screen = str(body.get("screen") or getattr(ctx, "screen", "")
                         or "Rack screen")
        except Exception:                        # noqa: BLE001
            who, screen = "", "Rack screen"
        return who, screen

    def _halt(self, body):
        """The halt before playback stops (Remote.halt): {"flames",
        "lasers", "show", "failed"}, the steps that are in this engine. A
        live show that was ended fades to dark first: every conductor call
        the scheduler decided is made, and the conductor's fade is let run
        (HALT_FADE_WAIT_S at most), so the music fades and nothing of the
        show_stopped sequence is cut short by the stop (review of #130,
        5). Never raises."""
        rm = getattr(self.server, "remote", None)
        fn = getattr(rm, "halt", None)
        if fn is None:
            return {}
        who, screen = self._who_where(body)
        try:
            h = fn(who, screen)
        except Exception as e:                   # noqa: BLE001
            return {"failed": ["halt"],
                    "error": f"{type(e).__name__}: {e}"}
        out = {k: h[k] for k in ("flames", "lasers", "lasers_blank", "show")
               if h.get(k) is not None}
        out["failed"] = list(h.get("failed") or [])
        if h.get("show_ended"):
            svc = getattr(self.server, "schedule", None)
            try:
                svc.flush_conductor(HALT_FADE_WAIT_S)
            except Exception:                    # noqa: BLE001
                pass
            cond = getattr(svc, "conductor", None)
            if cond is not None and getattr(cond, "threaded", False):
                try:
                    cond.wait_idle(HALT_FADE_WAIT_S)
                except Exception:                # noqa: BLE001
                    pass
        return out

    def _halt_stop(self, body, c):
        """The Stream Deck's Stop (/api/stop with "halt"): Remote.halt,
        then playback stops. Answers the request."""
        out = self._halt(body)
        try:
            out["stopped"] = c.stop()
        except Exception as e:       # noqa: BLE001
            # The halt ran; only the playback stop failed. The
            # answer still carries every step (third review of
            # #130, 4), for the deck to say.
            out["stopped"] = False
            out["failed"] = list(out.get("failed") or []) + [
                "playback stop"]
            said = f"{type(e).__name__}: {e}"
            out["playback_error"] = said
            # The halt's own error, if it had one, is kept
            # beside this one, never overwritten (review of
            # #130, low items, 2).
            out["error"] = (f"{out['error']}; and the playback "
                            f"stop: {said}" if out.get("error")
                            else said)
            c.last_error = said
            return self._send(500, out)
        return self._send(200, out)

    def _stop_flames(self, body):
        """Stop's flame half (review of #126, P2-1): live flame programming
        cut, then the flame link's disarm_all
        (the show's flames_disarm_all, never the conductor's Abort, so
        nothing is latched). {"disarmed": True/False, "text": ...}, or None
        with no flame link in this engine (the GPL path). Never raises."""
        rm = getattr(self.server, "remote", None)
        if rm is None:
            return None
        body = body if isinstance(body, dict) else {}
        try:
            who, screen = self._who_where(body)
        except Exception:                        # noqa: BLE001
            who, screen = "", "Rack screen"
        reason = f"{who or 'Someone'} pressed Stop on the {screen}"
        for name in ("live_flame_cut",):
            fn = getattr(rm, name, None)
            if fn is not None:
                try:
                    fn(reason, who, screen)
                except Exception:                # noqa: BLE001
                    pass
        # The hazers OFF (hazers.py, Jeff 2026-10-29: Stop is a complete
        # halt); the flame link's disarm_all below does it at the link too.
        fn = getattr(rm, "hazers_cut", None)
        if fn is not None:
            try:
                fn(reason, who, screen)
            except Exception:                    # noqa: BLE001
                pass
        # The fuel pump OFF (fuelpump.py, Jeff 2026-10-30: "it should be
        # affected by the stop button"); the disarm_all below does it at
        # the link too.
        fn = getattr(rm, "pump_cut", None)
        if fn is not None:
            try:
                fn(reason, who, screen)
            except Exception:                    # noqa: BLE001
                pass
        # The wave flamer cleaning (cleaning.py): Stop ends it, from the
        # rack screen, an iPad or the Stream Deck.
        cl = getattr(rm, "cleaning", None)
        if cl is not None:
            try:
                cl.stop(reason, who=who, screen=screen)
            except Exception:                    # noqa: BLE001
                pass
        fn = getattr(rm, "_disarm_now", None)
        if fn is None:
            return None
        try:
            ok, said = fn(who, screen, "Stop")
        except Exception as e:                   # noqa: BLE001
            ok, said = False, f"{type(e).__name__}: {e}"
        if ok is None:
            return None
        return {"disarmed": bool(ok), "text": said}

    # -- users and the audit log (users.py, audit.py) ---------------------
    def _acting_user(self, body):
        """{"name", "role"} of whoever this press counts as, or None for
        nobody signed in: the page's own sign-in, or for the Stream Deck
        process (loopback, screen "Stream Deck") whoever is signed in on
        this machine."""
        ctx = self._ctx()
        if ctx.session:
            return {"name": ctx.session.get("name"),
                    "role": ctx.session.get("role")}
        if ctx.local and isinstance(body, dict) and \
                str(body.get("screen") or "") == "Stream Deck":
            users = getattr(getattr(self.server, "remote", None), "users",
                            None)
            if users is not None:
                return users.machine_user()
        return None

    def _role_refused(self, route):
        """Send a 403 and return True when the role matrix does not let
        this press go ahead. NEVER for a safety press (users.classify:
        Abort, Hold, Stop, every disarm, a release, a lock, Acknowledge,
        signing in or out), and never before any user exists. Being on
        this machine is not a sign-in: the rack screen is asked like any
        other (Jeff's field report, 2026-10-28). A fault in here never
        stops a safety press (those are known before anything can fail),
        and refuses everything else: an arm is never let through because
        the check could not be made."""
        try:
            users = getattr(getattr(self.server, "remote", None), "users",
                            None)
            if users is None:
                return False
            from . import users as users_mod
            body = self._body()
            perm = users_mod.classify(route, body)
            if perm == "safety" or perm is None:
                return False
        except Exception:                        # noqa: BLE001
            return False
        try:
            who = self._acting_user(body)
            fault = None
            role = who.get("role") if who else None
            if users.may(role, perm):
                return False
        except Exception as e:                   # noqa: BLE001
            # The check could not be made: refused (fail closed), and said
            # so below.
            who, fault = None, e
        ended = False
        try:
            # A purge press while a timed purge runs is a second press: it
            # ends it, whoever is signed in (re-review of #172, B2), and
            # is refused for everything else as before.
            rm = getattr(self.server, "remote", None)
            m = getattr(rm, "morning", None)
            if route in ("/api/remote/purge-fire",
                         "/api/remote/purge-timed") and m is not None and \
                    m.purge.timed_running():
                ended = rm.timed_purge_cut(
                    f"a second purge press on the {self._ctx().screen} "
                    f"({(who or {}).get('name') or 'signed out'})",
                    (who or {}).get("name") or "", self._ctx().screen)
        except Exception:                        # noqa: BLE001
            pass
        try:
            if fault is not None:
                raise fault
            name = (who or {}).get("name")
            why = (f"{name} ({role}) may not do this: "
                   if name else "Nobody is signed in on this screen, and "
                   "signed out is view only: ") + \
                f"{users_mod.perm_label(perm)}. Sign in as someone who may " \
                f"(the user chip at the top). Abort, Hold, Stop and every " \
                f"disarm always work. Nothing was changed."
        except Exception as e:                   # noqa: BLE001
            why = (f"The sign-in check could not be made ({type(e).__name__})"
                   f", so this was refused: {users_mod.perm_label(perm)}. "
                   f"Abort, Hold, Stop and every disarm always work. Nothing "
                   f"was changed.")
        if ended:
            why = ("The timed purge was ended (a second purge press stops "
                   "it, signed in or not); nothing else was done. " + why)
        self._send(403, {"error": why, "role_refused": True,
                         "ended": bool(ended)})
        return True

    # Presses repeated many times a second while a finger is down (or a
    # page is open): only the first, and any that is not taken, are logged.
    AUDIT_BEATS = ("/api/remote/morning-open", "/api/schedule/deck-event",
                   "/api/remote/signin", "/api/remote/signout")
    AUDIT_TARGET_KEYS = ("group", "machine", "machines", "laser", "id",
                         "kind", "on", "at", "seconds", "which", "action",
                         "op", "name", "role", "checklist", "step", "state",
                         "look", "timeline", "only", "cue", "minutes",
                         "enabled", "time", "keep_days", "confirmed",
                         "raise", "clear")

    def _audit_post(self, route):
        """One audit line for this POST (audit.py): who, which surface,
        what, to what, and what came of it. Never raises, never waits;
        never a password, a token or a whole body."""
        try:
            remote = getattr(self.server, "remote", None)
            log = getattr(remote, "audit", None)
            if log is None:
                return
            body = getattr(self, "_body_cache", None)
            body = body if isinstance(body, dict) else {}
            code, out = getattr(self, "_said", None) or (0, {})
            ok = 200 <= code < 300
            short = route.rsplit("/", 1)[-1]
            beat = (route in self.AUDIT_BEATS or
                    (short == "arm-hold" and
                     body.get("hold_id") is not None) or
                    (short == "purge-fire" and
                     body.get("fire_id") is not None and
                     body.get("on") is True) or   # a release is logged
                    # The Fireball page: a beat that changes nothing.
                    (short == "fireball-beat" and
                     body.get("run") is not None and
                     body.get("change") is not True) or
                    # The wave flamer cleaning: a beat while held.
                    (short == "cleaning-beat" and
                     body.get("run") is not None and
                     body.get("hold") is True and
                     body.get("change") is not True))
            if beat and (ok or route in ("/api/remote/signin",
                                         "/api/remote/signout",
                                         "/api/remote/morning-open",
                                         "/api/schedule/deck-event")):
                return
            from . import audit as audit_mod
            parts = []
            for k in self.AUDIT_TARGET_KEYS:
                v = body.get(k)
                if v is None or audit_mod.secret_name(k):
                    continue
                if isinstance(v, list):
                    v = ",".join(str(x) for x in v[:12])
                elif isinstance(v, dict):
                    continue
                parts.append(f"{k}={v}")
            ctx = self._ctx()
            deck = ctx.local and str(body.get("screen") or "") == \
                "Stream Deck"
            who = self._acting_user(body)
            reason = ""
            if not ok and isinstance(out, dict):
                reason = str(out.get("error") or out.get("text") or "")
            name = route[len("/api/remote/"):] if \
                route.startswith("/api/remote/") else route[len("/api/"):] \
                if route.startswith("/api/") else route
            log.event(user=(who or {}).get("name") or "unsigned",
                      surface="deck" if deck else "web", action=name,
                      target=" ".join(parts),
                      result=("accepted" if ok else
                              "error" if code >= 500 or code == 0 else
                              "refused"),
                      reason=reason,
                      source=("Stream Deck" if deck else ctx.source))
            users = getattr(remote, "users", None)
            if users is not None and ctx.token and ctx.session:
                users.touch(ctx.token)
        except Exception:                        # noqa: BLE001
            pass

    def _refusal(self, post=False):
        """A sentence when this request must be turned away before any
        route sees it: forwarded by a proxy or tunnel, addressed to another
        host name, or posted from another site's page."""
        from . import remote as remote_mod
        h = remote_mod.looks_proxied(self.headers)
        if h:
            return (f"This request came through a proxy or tunnel ({h}). "
                    f"The show engine refuses those: remove the remote "
                    f"access software or port forward from the show "
                    f"machine.")
        bind = getattr(self.server, "bind_address", "127.0.0.1")
        port = self.server.server_address[1]
        if not remote_mod.host_ok(self.headers.get("Host"), self._local(),
                                  bind, port):
            return HOST_REFUSAL
        if post:
            # Fix round 1 of #39, F2: a sandboxed iframe or a data: page on
            # this machine sends Origin "null", and a text/plain or bodiless
            # POST is a "simple" request a browser sends cross-site with no
            # preflight. So: Origin null is cross-site; any Origin must be
            # this host; a browser's Sec-Fetch-Site must say same-origin
            # (or none: typed by the user); and the body must be declared
            # JSON, which no cross-site page can send without a preflight
            # this server never answers.
            origin = self.headers.get("Origin")
            if origin is not None:
                o = urllib.parse.urlparse(origin)
                if origin.strip().lower() == "null" or \
                        o.netloc.lower() != str(self.headers.get("Host") or
                                                "").lower():
                    return "A press from another site's page was refused."
            sfs = self.headers.get("Sec-Fetch-Site")
            if sfs is not None and sfs.strip().lower() not in ("same-origin",
                                                               "none"):
                return "A press from another site's page was refused."
            ctype = str(self.headers.get("Content-Type") or "")
            if ctype.split(";")[0].strip().lower() != "application/json":
                return ("A press has to be sent as JSON by the ltcplay page. "
                        "Nothing was done.")
        return None

    def _send(self, code, body, ctype="application/json", headers=None):
        # What was answered, for the audit line (_audit_post).
        self._said = (code, body if isinstance(body, dict) else {})
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        headers = dict(headers or {})
        self.send_header("Cache-Control",
                         headers.pop("Cache-Control", "no-store"))
        self.send_header("X-Frame-Options", "DENY")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _body(self):
        """The request's JSON body, read once: the role check reads it
        before the route does, and the audit line after."""
        if getattr(self, "_body_read", False):
            return self._body_cache
        self._body_cache = self._read_body()
        self._body_read = True
        return self._body_cache

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if not n:
            return {}
        try:
            raw = self.rfile.read(n)
        except OSError:
            raw = b""
        if len(raw) < n:
            # The connection went before the body arrived: nothing is done
            # on half a request (the remote routes answer None with 400).
            return None if "/api/remote/" in self.path else {}
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    def _schedule(self):
        """The scheduler's routes, when a schedule file is configured. When
        it is not, every schedule route is a plain 404, exactly as if this
        build had no scheduler at all."""
        sched = getattr(self.server, "schedule", None)
        return sched if sched is not None else _NoSchedule()

    def _announce(self):
        """Same rule as _schedule: without --announce every announcement
        route is a plain 404, and nothing about announcements is imported."""
        ann = getattr(self.server, "announce", None)
        return ann if ann is not None else _NoAnnounce()

    def _madmapper(self):
        """Same rule again: without a madmapper config every route here is
        a plain 404, and madmapper.py is never imported."""
        mm = getattr(self.server, "madmapper", None)
        return _MadMapperRoutes(*mm) if mm is not None else _NoMadMapper()

    def _beyond(self):
        """Same rule again: without a beyond config every route here is a
        plain 404, and beyond.py is never imported. Read-only -- there is
        no POST here: this build has no sequencing logic anywhere (see
        madmapper.py's module docstring), so there is nothing for a route
        here to trigger."""
        b = getattr(self.server, "beyond", None)
        return _BeyondRoutes(b) if b is not None else _NoBeyond()

    # -- routes -----------------------------------------------------------
    def _conductor(self):
        """Read-only: what the Fire & Ice show conductor last did. A plain
        404 without one, like every optional route here. There is no route
        that presses Hold, Resume or Abort."""
        c = getattr(self.server, "conductor", None)
        if c is None:
            return 404, {"error": "no such thing here"}
        snap = c.snapshot()
        # The scheduler's own Abort latch, saved in tonight's file, is the
        # one that survives an engine restart (the conductor starts
        # unlatched): latched while either is (review of PR #43, P1-5), so
        # the Stream Deck shows the Abort latch after a restart too. A
        # plain read, never the scheduler's lock: this route is polled.
        m = getattr(getattr(self.server, "schedule", None), "machine", None)
        sched_latched = bool(getattr(m, "abort_latched", False))
        snap["scheduler_abort_latched"] = sched_latched
        snap["latched"] = bool(snap.get("latched")) or sched_latched
        out = {"conductor": snap}
        fi = getattr(self.server, "fire_ice", None)
        fl = getattr(fi, "flame_link", None)
        if fl is not None and hasattr(fl, "snapshot"):
            try:
                out["flame_link"] = fl.snapshot()
            except Exception:
                pass
        cues = getattr(fl, "cues", None)
        if hasattr(cues, "fault"):
            # Review of PR #43, P1-3: a show whose flame cues are refused
            # (a render that does not fit the layout, and the like) is a
            # fault the Stream Deck shows, not a quiet zero. Soak
            # 2026-10-21: and from show load, a render older than the
            # layout, before any flame frame is refused for it.
            now = getattr(cues, "faults_now", None)
            out["flame_cues"] = {"fault": str(
                (now() if now is not None else cues.fault) or "")}
        lasers = getattr(getattr(fi, "devices", None), "beyond", None)
        if lasers is not None and hasattr(lasers, "health"):
            # How the lasers are kept dark, and BEYOND's timecode stream's
            # own counts (beyondtc.py), for the soak and the screens.
            try:
                out["lasers"] = lasers.health()
            except Exception:
                pass
        out["audio"] = self._show_audio(fi)
        return 200, out

    @staticmethod
    def _show_audio(fi):
        """Read-only: whether the show audio is lost mid-show, for the
        Stream Deck's Start key (AUDIO LOST) and the screens. Off the
        running session's show audio clock (clock.AudioMaster, duck-typed
        as flamelink.audio_master_state reads it): "lost" is a cue playing
        while the clock has stopped following the audio and runs on this
        computer's time instead (its freerun after a loss), until the
        audio comes back or the cue ends. Nothing playing: never lost.
        "fault" is the clock's standing audio fault sentence, or None."""
        get = getattr(fi, "clock_nolock", None)
        clk = get() if callable(get) else None
        if clk is None:
            return {"playing": False, "lost": False, "fault": None}
        playing = bool(getattr(clk, "playing", False))
        mode = getattr(clk, "_mode", None) or "idle"
        fault = getattr(clk, "_fault", None)
        return {"playing": playing,
                "lost": playing and mode == "freerun",
                "fault": str(fault) if fault else None}

    def do_GET(self):
        self._ctx_cache = None          # one connection carries many requests
        route = urllib.parse.urlparse(self.path).path
        why = self._refusal()
        if why == HOST_REFUSAL and not route.startswith("/api/"):
            # A person typed another name for this PC (its computer name,
            # a .local name): a short page with the address that works.
            # The check itself is not loosened; nothing else is served.
            return self._send(403, wrong_host_page(
                getattr(self.server, "bind_address", "127.0.0.1"),
                self.server.server_address[1], self._local()),
                "text/html; charset=utf-8",
                headers={"Content-Security-Policy":
                         "default-src 'none'; style-src 'unsafe-inline'",
                         "X-Content-Type-Options": "nosniff"})
        if why:
            return self._send(403, {"error": why})
        if not self._local() and not network_may_reach(route):
            return self._send(403, {"error": "Not from the network. Only "
                                             "the remote page's own routes "
                                             "answer here."})
        c = self.server.control
        try:
            if route in ("/", "/index.html") and \
                    getattr(self.server, "fire_ice_config", None) is not None:
                # Fire & Ice (Jeff, 2026-10-04): the rack screen is the one
                # operator screen. The old operator page stays for the GPL
                # path only; nothing in this mode serves or links to it.
                return self._send(302, b"", "text/plain",
                                  headers={"Location": "/remote"})
            if route == "/ui":
                return self._ui_switch()
            if route == "/remote-logic.js":
                return self._remote_logic()
            if route in ("/remote", "/remote1", "/remote2") or (
                    route in ("/", "/index.html") and not self._local()):
                # The show network gets the remote page: the sign-in form
                # until it has a session, then the touch controls. 1.0
                # unless this screen chose 2.0 (or the default is 2.0);
                # /remote1 and /remote2 are always that page.
                page = {"/remote1": REMOTE_PAGE,
                        "/remote2": REMOTE2_PAGE}.get(route) or (
                    REMOTE2_PAGE if self._ui_choice() == 2 else REMOTE_PAGE)
                if page == REMOTE2_PAGE:
                    return self._send(200, remote2_page(),
                                      "text/html; charset=utf-8")
                with open(page, "rb") as fh:
                    return self._send(200, fh.read(),
                                      "text/html; charset=utf-8")
            if route == "/api/remote" or route.startswith("/api/remote/"):
                remote = getattr(self.server, "remote", None)
                if remote is None:
                    return self._send(404, {"error": "no such thing here"})
                # The query (a checklist export's dates) rides along; the
                # route itself is still the bare path.
                query = urllib.parse.urlparse(self.path).query
                return self._send(*remote.get(route, self._ctx(),
                                              query=query))
            if route in ("/", "/index.html"):
                with open(PAGE, "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            if route.startswith("/brand/"):
                # The logo, served straight off disk. Confined to the brand
                # folder: a path that escapes it is a way to read the Mac.
                name = os.path.basename(route[len("/brand/"):])
                f = os.path.join(os.path.dirname(PAGE), "brand", name)
                if (name and not name.startswith(".")
                        and os.path.isfile(f)
                        and os.path.abspath(f).startswith(
                            os.path.join(os.path.dirname(PAGE), "brand"))):
                    # The bundled show fonts live here too, so the pages
                    # never need the internet at the venue.
                    kind = ("image/png" if name.endswith(".png") else
                            "image/svg+xml" if name.endswith(".svg") else
                            "font/woff2" if name.endswith(".woff2") else
                            "application/octet-stream")
                    with open(f, "rb") as fh:
                        return self._send(200, fh.read(), kind)
                return self._send(404, {"error": "no such thing here"})
            if route in ICONS:
                name, kind = ICONS[route]
                try:
                    with open(os.path.join(ICONS_DIR, name), "rb") as fh:
                        data = fh.read()
                except OSError:
                    return self._send(404, {"error": "no such thing here"})
                return self._send(200, data, kind,
                                  headers={"Cache-Control": ICON_CACHE})
            if route == "/api/brand":
                return self._send(200, brand_doc(self.server))
            if route == "/api/state":
                return self._send(200, c.state())
            if route == "/api/devices":
                return self._send(200, c.devices())
            if route == "/api/timelines":
                return self._send(200, {"folder": c.folder,
                                        "timelines": c.timelines()})
            if route == "/api/log":
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                return self._send(200, {"lines": c.log_tail(q.get("n", [120])[0])})
            if route == "/api/schedule" or route.startswith("/api/schedule/"):
                return self._send(*self._schedule().get(route))
            if route == "/api/announce" or route.startswith("/api/announce/"):
                return self._send(*self._announce().get(route))
            if route == "/api/madmapper" or route.startswith("/api/madmapper/"):
                return self._send(*self._madmapper().get(route))
            if route == "/api/beyond" or route.startswith("/api/beyond/"):
                return self._send(*self._beyond().get(route))
            if route == "/api/conductor":
                return self._send(*self._conductor())
        except USER_ERRORS as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        return self._send(404, {"error": "no such thing here"})

    def _ui_choice(self):
        """1 or 2: which page /remote serves this screen. Its own
        ltcplay_ui cookie ("1" or "2") first, else the show machine's
        default (remote.ui_default, 1 until Jeff sets 2). Anything odd, or
        anything that cannot be read, is 1: the page every screen had."""
        for part in str(self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == UI_COOKIE and v in ("1", "2"):
                return int(v)
        rm = getattr(self.server, "remote", None)
        try:
            return 2 if rm is not None and rm.ui_default() == 2 else 1
        except Exception:                        # noqa: BLE001
            return 1

    def _ui_switch(self):
        """GET /ui?v=2 or /ui?v=1: this screen's own choice of interface, a
        cookie in this browser and nothing else, then back to /remote.
        Nothing on the show machine changes. A link followed from another
        site's page is refused, as a press from one is."""
        sfs = self.headers.get("Sec-Fetch-Site")
        if sfs is not None and sfs.strip().lower() not in ("same-origin",
                                                           "none"):
            return self._send(403, {"error": "A switch from another site's "
                                             "page was refused."})
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        v = (q.get("v") or [""])[0]
        if v not in ("1", "2"):
            return self._send(400, {"error": "v has to be 1 or 2."})
        return self._send(302, b"", "text/plain", headers={
            "Location": "/remote",
            "Set-Cookie": f"{UI_COOKIE}={v}; SameSite=Strict; Path=/; "
                          f"Max-Age=31536000"})

    def _remote_logic(self):
        """GET /remote-logic.js: 1.0's pure rules, cut out of remote.html
        (its remote-logic block) at each request, so 2.0 runs the very
        functions 1.0 runs and selftest.py proves."""
        logic = remote_logic()
        if logic is None:
            return self._send(500, {"error": "remote.html has no "
                                             "remote-logic block."})
        return self._send(200, logic,
                          "application/javascript; charset=utf-8")

    def _abort_latched(self):
        """True while an Abort stands (the scheduler's latch, or the show
        conductor's), until Reset."""
        svc = getattr(self.server, "schedule", None)
        if svc is None:
            return False
        m = getattr(svc, "machine", None)
        cond = getattr(svc, "conductor", None)
        return bool(getattr(m, "abort_latched", False) or
                    getattr(cond, "latched", False))

    def _scheduled_show_live(self):
        svc = getattr(self.server, "schedule", None)
        m = getattr(svc, "machine", None) if svc is not None else None
        return m is not None and getattr(m, "state", None) in \
            LIVE_SHOW_STATES

    def do_POST(self):
        # One connection carries many requests: nothing of the last one's
        # body or answer is kept. Every POST gets one audit line once it
        # has been answered (audit.py), whatever the answer was.
        self._body_read, self._body_cache, self._said = False, None, None
        route = urllib.parse.urlparse(self.path).path
        try:
            return self._do_post(route)
        finally:
            self._audit_post(route)

    def _do_post(self, route):
        self._ctx_cache = None          # one connection carries many requests
        why = self._refusal(post=True)
        if why:
            return self._send(403, {"error": why})
        # The role matrix (users.py): never in front of a safety press.
        if self._role_refused(route):
            return None
        if not self._local() and not network_may_reach(route):
            return self._send(403, {"error": "Not from the network. Only "
                                             "the remote page's own routes "
                                             "answer here."})
        if route == "/api/remote" or route.startswith("/api/remote/"):
            remote = getattr(self.server, "remote", None)
            if remote is None:
                return self._send(404, {"error": "no such thing here"})
            # Read the whole body before anything is done: a page that
            # drops mid-request has pressed nothing.
            body = self._body()
            if body is None:
                return self._send(400, {"error": "The request did not "
                                                 "arrive whole. Nothing was "
                                                 "done."})
            self.server.control._state_cache = None
            try:
                code, out, headers = remote.post(route, body, self._ctx())
            except Exception as e:
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})
            return self._send(code, out, headers=headers)
        if route in LIVE_SHOW_REFUSED and self._scheduled_show_live() and \
                not (route == "/api/stop" and self._halt_asked()):
            return self._send(409, {"error": LIVE_SHOW_REFUSAL})
        c = self.server.control
        # An operator action (Start, Stop, GO, autoreload, override, ...) must
        # never be hidden behind a stale cached /api/state answer: the whole
        # point of Control.state()'s cache is to spare a PASSIVE poll, never
        # to make the button that just changed something lie about it. This
        # just clears the slot; the next state() call rebuilds, lazily, once.
        c._state_cache = None
        body = self._body()
        if route in LIVE_SHOW_REFUSED and route not in LATCHED_ALLOWED and \
                not (route == "/api/override" and
                     (body or {}).get("look") == "blackout") and \
                self._abort_latched():
            return self._send(409, {"error": LATCHED_REFUSAL})
        if isinstance(body, dict) and "who" in body and \
                route != "/api/schedule/operator":
            body = self._authed_body(route, body)
        if route == "/api/schedule" or route.startswith("/api/schedule/"):
            # Kept apart from the show's routes: a refused edit to tonight's
            # list is not the show's last error and must not appear as one.
            try:
                return self._send(*self._schedule().post(route, body))
            except USER_ERRORS as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        if route == "/api/announce" or route.startswith("/api/announce/"):
            # Kept apart from the show's routes for the same reason: a
            # refused announcement press is not the show's last error.
            try:
                return self._send(*self._announce().post(route, body))
            except USER_ERRORS as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        try:
            if route == "/api/find":
                return self._send(200, c.find(float(body.get("seconds", 3.0))))
            if route == "/api/input":
                return self._send(200, {"saved": c.set_input(
                    body.get("device"), body.get("channel"), body.get("rate"))})
            if route == "/api/check":
                return self._send(200, c.check(body.get("timeline", "")))
            if route == "/api/start":
                # Run ends a timed purge at once (morning.py), refused or
                # not: it only takes risk away.
                _rm0 = getattr(self.server, "remote", None)
                if _rm0 is not None and hasattr(_rm0, "timed_purge_cut"):
                    _rm0.timed_purge_cut("Run was pressed on the " +
                                         str(self._ctx().screen),
                                         self._ctx().user or "unsigned",
                                         self._ctx().screen)
                # Never while a Stop's halt is under way (re-review of
                # #144, N2): a Run from another screen would bring the rig
                # back in the middle of the Stop.
                _svc = getattr(self.server, "schedule", None)
                if getattr(_svc, "halting", lambda: False)() is True:
                    return self._send(409, {"error": (
                        "Run was refused: " + _svc.HALTING + ". Nothing was "
                        "started.")})
                # Never over Programming playback (playhead.py): the same
                # sentence as Start now's.
                _rm = getattr(self.server, "remote", None)
                _why = getattr(_rm, "programming_playback_refusal",
                               lambda: "")()
                if _why:
                    return self._send(409, {"error": _why})
                # Run ends live flame programming first (flameprog.py):
                # the show's own flame cues and xLights are never both a
                # source of fire values.
                _rm = getattr(self.server, "remote", None)
                if _rm is not None and hasattr(_rm, "live_flame_end"):
                    _rm.live_flame_end("Run was pressed",
                                       self._ctx().user or "unsigned",
                                       self._ctx().screen)
                s = c.start(body.get("timeline", ""),
                            no_output=bool(body.get("no_output")),
                            on_lost=body.get("on_lost"),
                            allow_missing=bool(body.get("allow_missing")),
                            auto_reload=body.get("auto_reload"))
                # The show Run is the show in use, kept across a restart
                # (remote.note_run; 2026-10-24).
                if _rm is not None and hasattr(_rm, "note_run"):
                    _rm.note_run(getattr(s, "timeline_path", None))
                return self._send(200, {"started": True, "banner": s.banner,
                                        "notes": s.notes,
                                        "problems": s.problems})
            if route == "/api/autoreload":
                return self._send(200, {"auto_reload": c.set_auto_reload(
                    body.get("on"))})
            if route == "/api/go":
                return self._send(200, c.go(body.get("at")))
            if route == "/api/showdir":
                return self._send(200, c.show_folder(
                    body.get("timeline"), body.get("folder")))
            if route == "/api/reinput":
                return self._send(200, c.reset_input())
            if route == "/api/trigger":
                return self._send(200, c.set_trigger(body.get("on")))
            if route == "/api/skip":
                return self._send(200, c.skip(body.get("seconds"),
                                              body.get("cue")))
            if route == "/api/release":
                return self._send(200, c.release())
            if route == "/api/reload":
                return self._send(200, c.reload(body.get("only")))
            if route == "/api/override":
                return self._send(200, {"override": c.override(
                    body.get("look"))})
            if route == "/api/stop":
                # Stop takes the flames down first (Jeff, 2026-10-26;
                # review of #126, P2-1), the Rack screen's and the Stream
                # Deck's alike: live flame programming ends, and the flame
                # link's disarm_all goes out
                # (the cues zeroed, a zero frame, the keyed disarm and its
                # repeats: the flame half of an Abort), WITHOUT the Abort
                # latch, which is the Abort's alone. Then playback stops.
                # The Stream Deck's Stop is a complete halt (Jeff,
                # 2026-10-26, "halt"): Remote.halt takes away everything
                # an Abort does, in the Abort's order, with no latch, and
                # ends a live scheduled show (STOP_SHOW). Its fade runs to
                # the end before playback stops.
                if self._halt_asked():
                    # A Stop's halt under way: Start now and Run are
                    # refused until it is over (re-review of #144, N2).
                    svc = getattr(self.server, "schedule", None)
                    begin = getattr(svc, "halt_begin", None)
                    if begin is not None:
                        begin()
                    try:
                        return self._halt_stop(body, c)
                    finally:
                        end = getattr(svc, "halt_end", None)
                        if end is not None:
                            end()
                flames = self._stop_flames(body)
                out = {"stopped": c.stop()}
                if flames is not None:
                    out["flames"] = flames
                return self._send(200, out)
        except USER_ERRORS as e:
            c.last_error = str(e)
            return self._send(400, {"error": str(e)})
        except Exception as e:
            c.last_error = f"{type(e).__name__}: {e}"
            return self._send(500, {"error": c.last_error})
        return self._send(404, {"error": "no such thing here"})


class _NoSchedule:
    """Stands in for the scheduler when none is configured."""

    def get(self, route):
        return 404, {"error": "no such thing here"}

    def post(self, route, body):
        return 404, {"error": "no such thing here"}


class _NoAnnounce:
    """Stands in for the announcements service when none is configured."""

    def get(self, route):
        return 404, {"error": "no such thing here"}

    def post(self, route, body):
        return 404, {"error": "no such thing here"}


class _NoMadMapper:
    """Stands in for the MadMapper link when none is configured."""

    def get(self, route):
        return 404, {"error": "no such thing here"}


class _MadMapperRoutes:
    """The one read-only route the MadMapper link answers: the health dict
    for the 'MadMapper link' dot (handoff section 8), from the link's
    outbound side and the watchdog's heartbeat side together. Read-only:
    this build's madmapper.py is device layer only (select_bank, fade_*,
    set_*, ...), with no sequencing logic here to trigger over a route."""

    def __init__(self, link, watchdog):
        self.link = link
        self.watchdog = watchdog

    def get(self, route):
        if route == "/api/madmapper" or route == "/api/madmapper/state":
            return 200, {"watchdog": self.watchdog.health(),
                        "config": self.link.cfg.summary()}
        return 404, {"error": "no such thing here"}


class _NoBeyond:
    """Stands in for the BEYOND link when none is configured."""

    def get(self, route):
        return 404, {"error": "no such thing here"}


class _BeyondRoutes:
    """The one read-only route the BEYOND link answers: "command sent"
    only, never a liveness claim -- see beyond.py's own health()."""

    def __init__(self, link):
        self.link = link

    def get(self, route):
        if route == "/api/beyond" or route == "/api/beyond/state":
            return 200, {"beyond": self.link.health(),
                        "config": self.link.cfg.summary()}
        return 404, {"error": "no such thing here"}


def _device_journal(httpd_schedule):
    """A real journal for beyond.py's and madmapper.py's fault lines (both
    links, and the MadMapper watchdog), so a failed blank/unblank or video
    goes SOMEWHERE rather than nowhere (beyond.build() with no `journal` at
    all is exactly what silently drops them -- see beyond.py's own
    docstring: "a false 'the lasers are down' report is worse than no
    report", but no report at all is not the goal either).

    Always prints, the same way this function already prints what it
    cannot silently skip (see the schedule/show-length check, above). When
    a schedule is configured, its own night journal is also reachable, so
    every line is forwarded there too, through the exact `_journal_line()`
    call the schedule's own show-length warning already uses (actor=
    "system") -- not a new journal, the one already running. Without a
    schedule there is no night journal in this process to reach at all,
    the same as every other device link web.py builds; printing is what is
    left."""
    def journal(text, **extra):
        print(text)
        if httpd_schedule is not None:
            httpd_schedule._journal_line("system", text, **extra)
    return journal


_beyond_journal = _device_journal      # its earlier name


def brand_doc(server):
    """ltcplay_brand.json, with the show's own name and venue from the
    Fire & Ice config when this server runs one (its "show_name" and
    "venue"): the shared brand file never names one show."""
    b = brand_mod.load()
    cfg = getattr(server, "fire_ice_config", None)
    if cfg is not None:
        if getattr(cfg, "show_name", None):
            b["show"] = cfg.show_name
        if getattr(cfg, "venue", None):
            b["venue"] = cfg.venue
    return b


def _attach_accounts(httpd, users_seed=None, transport=None):
    """Users, sign-in, the engine's audit log, what the engine sees by
    itself (auditwatch.py) and the nightly upload, on httpd.remote.
    Returns True when on. Never raises."""
    r = httpd.remote
    try:
        from . import audit as audit_mod
        from . import auditsend
        from . import auditwatch
        from . import users as users_mod
        log = audit_mod.AuditLog(audit_mod.default_folder(r.folder),
                                 "engine").start()
        r.audit = log

        def _note(action, user, result="accepted", reason="", target="",
                  source=""):
            log.event(user=user, surface="web", action=action,
                      target=target, result=result, reason=reason,
                      source=source)
        r.users = users_mod.Users(r.folder, audit=_note)
        # Sign everyone out after the night's last show (Jeff, 2026-10-26).
        r.users.last_show = r.last_show_over
        r.users.start()
        r.users.seed(users_mod.SEED_USERS if users_seed is None
                     else users_seed)
        r.uploader = auditsend.Uploader(
            log.folder, r.folder, transport=transport,
            note=lambda **kw: log.event(user=audit_mod.SYSTEM,
                                        surface="engine", **kw)).start()
        r.audit_watch = auditwatch.EngineWatch(r, log).start()
        log.event(user=audit_mod.SYSTEM, surface="engine",
                  action="engine-start", target=r.folder)
        before = httpd.server_close

        def _close():
            try:
                r.audit_watch.stop()
                r.users.stop()
                r.uploader.stop()
                log.event(user=audit_mod.SYSTEM, surface="engine",
                          action="engine-stop")
                log.flush(1.0)
                log.stop(1.0)
            except Exception:                    # noqa: BLE001
                pass
            before()
        httpd.server_close = _close
        return True
    except Exception as e:                       # noqa: BLE001
        print(f"Users and the audit log are off: {type(e).__name__}: {e}")
        return False


def _startup_lasers_dark(wiring):
    """The engine starting: the lasers blanked and BEYOND's laser output
    switched off, through the conductor and its own Blanking. Never
    raises; a failure is the Blanking's and beyond.py's own fault line."""
    try:
        wiring.conductor.lasers_dark_now("the engine started")
    except Exception:                            # noqa: BLE001
        pass
    try:
        bl = getattr(getattr(wiring, "devices", None), "beyond", None)
        if bl is not None and hasattr(bl, "output_disable"):
            bl.output_disable()
    except Exception:                            # noqa: BLE001
        pass



def _preroll_hosts(httpd, fire_ice):
    """(label, address) of every device on the network the show uses, for
    the pre-roll's Network check: MadMapper and BEYOND when not on this
    PC (thispc.on_this_pc), the timecode receivers, the flame node flamesafe sends to, and the
    pixel controllers. Loopback and broadcast are left out (the program
    checks cover this PC). Never raises."""
    import ipaddress
    from . import fire_ice as fire_ice_mod
    out = []

    def add(label, ip):
        try:
            a = ipaddress.IPv4Address(str(ip))
        except ValueError:
            return
        if a.is_loopback or str(ip).endswith(".255") or a.is_multicast:
            return
        out.append((label, str(ip)))
    try:
        # MadMapper and BEYOND on this PC, a loopback adapter's address
        # included (thispc.py), are the program checks', not the network's.
        from . import thispc
        mm = getattr(fire_ice, "madmapper", None)
        if mm is not None and not thispc.on_this_pc(mm.host):
            add("MadMapper", mm.host)
        bey = getattr(fire_ice, "beyond", None)
        if bey is not None and not thispc.on_this_pc(bey.host):
            add("BEYOND", bey.host)
        gate = getattr(getattr(getattr(httpd, "fire_ice", None), "devices",
                               None), "beyond", None)
        gate = getattr(gate, "gate", None)
        if gate is not None and gate.ip:
            add("BEYOND timecode", gate.ip)
        if getattr(fire_ice, "flamesafe_config", None):
            dest = fire_ice_mod.flamesafe_destination(
                fire_ice.flamesafe_config)
            if dest is not None:
                add("flame node", dest[0])
        s = getattr(httpd.control, "session", None)
        clk = getattr(s, "clock", None)
        for label, ip in getattr(getattr(clk, "out", None), "dests",
                                 None) or []:
            add(f"timecode to {label}", ip)
        rig = getattr(s, "rig", None)
        # Pixel controllers: every one that answered a ping since Run
        # (rigwatch's baseline; many Art-Net nodes never answer ICMP, and
        # one that never did would hold every show). Before the first
        # sweep, every one.
        base = getattr(rig, "baseline", None)
        for ip in getattr(rig, "ips", None) or []:
            if base is None or ip in base:
                add("pixel controller", ip)
    except Exception:                                # noqa: BLE001
        pass
    return out


def _page_monitor(folder=None):
    """showpc.json's page_monitor (the supervisor's settings, in this PC's
    settings folder): the rack screen's display when its window cannot be
    seen. 1 when it cannot be read."""
    try:
        from . import appdata
        with open(os.path.join(folder or appdata.folder(), "showpc.json"),
                  encoding="utf-8-sig") as fh:
            v = json.load(fh).get("page_monitor", 1)
        return v if isinstance(v, int) and not isinstance(v, bool) and \
            v >= 1 else 1
    except Exception:                                # noqa: BLE001
        return 1


def _attach_mm_output(httpd, fire_ice, mm_cfg, windows=None):
    """MadMapper's video output, on or OFF (showwindows.py; Jeff's field
    report, 2026-10-29): read from this PC's window list on a thread of its
    own, for the rack screen's lamp and the rig check. Only with MadMapper
    on this Windows PC and madmapper_output_check on; otherwise the reason
    it is not checked. Never raises."""
    remote = httpd.remote
    try:
        import sys
        from . import madmapperwatch as mmw
        from . import showwindows
        if windows is None:
            windows = sys.platform == "win32"
        host = getattr(mm_cfg, "host", None)
        why = ""
        if not getattr(fire_ice, "madmapper_output_check", True):
            why = ("not checked: madmapper_output_check is false in "
                   "ltcplay_fire_ice.json")
        elif not mmw.on_this_pc(host):
            why = (f"not checked: MadMapper is on another machine ({host}), "
                   f"so its output window cannot be seen from here")
        elif not windows:
            why = "not checked: this is not the Windows show PC"
        remote.mm_output_why = why
        if why:
            remote.mm_output = None
            return None
        look = showwindows.OutputLook(
            monitor=getattr(fire_ice, "madmapper_output_monitor", 0),
            rack=_page_monitor()).start()
        remote.mm_output = look
        before = httpd.server_close

        def _close():
            try:
                look.stop()
            finally:
                before()
        httpd.server_close = _close
        return look
    except Exception as e:                           # noqa: BLE001
        remote.mm_output = None
        remote.mm_output_why = (f"not known: the output check could not "
                                f"start ({type(e).__name__}: {e})")
        return None


def _output_seen_on(alerts, r, look=None):
    """A passed rig check whose MadMapper output item passed (seen on, not
    just not used) clears the "output" alert (showwindows.OutputWatch's)
    raised before the check began (review B of #176: a stale one held a
    healthy show). Never one raised after, and never while the engine's
    own look (`look`, showwindows.OutputLook) reads it off again now: the
    supervisor's watch still thinks its alert is up for that off spell and
    would not raise it again (review B round 2). Returns whether it
    cleared."""
    # "used": the check looked and saw it on. "note" is no guide: a pass
    # that says what it saw carries one too (review 1 of #176).
    seen = [i for i in r.get("items") or ()
            if i.get("name") == "MadMapper output" and i.get("ok")
            and i.get("used")]
    began = r.get("wall_at")
    if not seen or began is None or look is None:
        return False
    try:
        if look.now()[0] != "on":
            return False
    except Exception:                                # noqa: BLE001
        return False
    for a in alerts.open():
        if a.kind == "output" and a.since < began:
            ok, _t = alerts.clear(id=a.id, action="Confirm",
                                  who="LTC Player (the rig check saw "
                                      "MadMapper's output on)",
                                  screen="engine")
            return ok
    return False


def _attach_preroll(httpd, fire_ice, fstatus):
    """The pre-roll (preroll.py, Jeff 2026-10-28): the rig checked before
    every show the scheduler starts and before Start now, on the schedule
    service (Service.preroll) and the rack screen (Remote.preroll). Only
    for a scheduler that performs, and preroll_s above 0. Never raises."""
    try:
        from . import preroll as P
        from . import rigwatch
        svc = httpd.schedule
        wiring = httpd.fire_ice
        if svc is None or wiring is None or \
                not getattr(fire_ice, "scheduler_performs", False) or \
                not getattr(fire_ice, "preroll_s", 0):
            return None
        remote = httpd.remote
        control = httpd.control

        def clk():
            s = getattr(control, "session", None)
            c = getattr(s, "clock", None)
            return c if getattr(c, "source", None) == "audio_master" else None

        def refusal():
            got = wiring.runner.start_blocked() if wiring.runner else None
            return (got or {}).get("text", "")

        def booth_resolve(name):
            from . import announce as announce_mod
            from . import boothaudio
            sd = remote.alerts.booth._sd or boothaudio._import_sounddevice()
            if sd is None:
                raise ValueError("sounddevice is not installed here")
            announce_mod.resolve_output_device(sd, name)
        mm = httpd.madmapper if isinstance(httpd.madmapper, tuple) else None
        mm_cfg = getattr(fire_ice, "madmapper", None)
        hb = getattr(getattr(mm_cfg, "heartbeat", None), "show_len_s", None)

        def mm_running():
            w = getattr(remote, "madmapper_watch", None)
            return w.check_once() if w is not None else None

        def bey_running():
            w = getattr(remote, "beyond_watch", None)
            return w.check_once() if w is not None else None

        def bey_why():
            from . import beyondwatch
            return beyondwatch.unknown_why(
                getattr(getattr(fire_ice, "beyond", None), "host", None))
        blanking = getattr(wiring.devices, "beyond", None) \
            if getattr(fire_ice, "beyond", None) is not None else None
        checks = [
            ("Audio", True, lambda p: P.check_audio(p, clk(), refusal())),
            # Advisory (Jeff, 2026-10-30): nothing plays on the PA by
            # itself, so a missing output or recording is a warning line
            # for the operator's own buttons, never a failed check.
            ("Announcements", False,
             lambda p: P.check_announcements(p, httpd.announce)),
            ("Booth speaker", True,
             lambda p: P.check_booth(p, remote.alerts.booth, booth_resolve)),
            ("Timecode", True, lambda p: P.check_timecode(
                p, getattr(clk(), "out", None),
                getattr(blanking, "gate", None))),
            ("Video", True, lambda p: P.check_video(
                p, mm[0] if mm else None,
                mm[1] if mm and hb is not None else None, clk(),
                wiring.conductor, mm_running,
                fire_ice.preroll_video_unconfirmed_ok)),
            ("MadMapper output", True, lambda p: P.check_madmapper_output(
                p, getattr(getattr(remote, "mm_output", None), "now", None),
                getattr(remote, "mm_output_why", "") or (
                    "no MadMapper in this show"
                    if getattr(fire_ice, "madmapper", None) is None else
                    "not known: MadMapper's output check did not start"))),
            ("Flame link", True, lambda p: P.check_flame_link(
                p, fstatus, wiring.flame_link, remote.deck_chip
                if fstatus is not None else None)),
            ("Lasers", True, lambda p: P.check_lasers(
                p, blanking, wiring.laser_arm, wiring.conductor,
                bey_running if getattr(remote, "beyond_watch", None)
                else None, bey_why)),
            ("Network", True, lambda p: P.check_network(
                p, _preroll_hosts(httpd, fire_ice),
                lambda ip: rigwatch.ping(ip, 1.0))),
        ]
        journal = _device_journal(svc)

        def note(text, fault=False, outcome=""):
            journal(text, action="pre-roll", outcome=outcome, fault=fault)

        def skip():
            return "Programming" if remote.programming()[0] else ""
        pr = P.PreRoll(
            checks, window_s=fire_ice.preroll_s, journal=note,
            on_pass=lambda r: _output_seen_on(
                remote.alerts, r, getattr(remote, "mm_output", None)),
            skip=skip, **P.alert_hooks(remote.alerts))
        svc.preroll = pr
        remote.preroll = pr
        return pr
    except Exception as e:                           # noqa: BLE001
        # Fail closed (review of #162): said in the journal and as an
        # alert, which holds every show at its time while it is open.
        text = (f"The rig check could not be set up "
                f"({type(e).__name__}: {e}), so no show can be checked. "
                f"Shows are held at their time while this is open; restart "
                f"LTC Player, and check the journal.")
        print(text)
        try:
            _device_journal(httpd.schedule)(text, action="pre-roll",
                                            outcome="error", fault=True)
            httpd.remote.alerts.raise_("preroll", text)
        except Exception:                            # noqa: BLE001
            pass
        # A stand-in whose one check always fails (delta review of #162):
        # an acknowledged alert must not let shows start unchecked.
        try:
            from . import preroll as P
            al = getattr(httpd.remote, "alerts", None)
            pr = P.standin(
                f"the rig check could not be set up ({type(e).__name__}: "
                f"{e})",
                alert=getattr(al, "raise_", None),
                open_alert=(lambda: any(a.kind == P.KIND
                                        for a in al.open()))
                if al is not None else None)
            if httpd.schedule is not None:
                httpd.schedule.preroll = pr
            httpd.remote.preroll = pr
        except Exception:                            # noqa: BLE001
            pass
        return None


def _attach_live_flame(httpd, fire_ice, flamesafe_config):
    """Live flame programming (flameprog.py, LIVE_FLAME_PROGRAMMING.md):
    built for the Fire & Ice engine with a flame link and a flame
    controller. Always OFF at start; nothing listens until it is turned on
    on the rack screen. A failure to build it leaves it unavailable and is
    said once; the show is never held back."""
    from . import flameprog
    from . import fire_ice as fire_ice_mod
    remote = httpd.remote
    try:
        def _map():
            s = flameprog.load_settings(remote.live_flame_settings_path())
            return flameprog.find_map(
                getattr(remote.control, "folder", None),
                fire_ice.flame_controller,
                show_networks=fire_ice_mod._show_networks,
                explicit=s.networks_xml, dest=s.listen_ip)

        def _journal(who, screen, outcome, text, fault=False):
            remote._journal(who or "system", screen or "engine",
                            "live flame", outcome, text, fault=fault)

        def _disarm(reason):
            fn = remote._disarm_fn()
            if fn is None:
                link = getattr(httpd.fire_ice, "flame_link", None)
                return bool(link is not None and link.disarm_all(reason))
            r = fn(reason)
            return bool(getattr(r, "ok", False))
        remote.live_flame = flameprog.LiveFlame(
            link_get=lambda: getattr(httpd.fire_ice, "flame_link", None),
            blockers=remote.live_flame_blockers, map_get=_map,
            armed_groups=remote.live_flame_armed_groups,
            fs_dest=lambda: flameprog.flamesafe_destination(
                flamesafe_config or fire_ice.flamesafe_config),
            settings_get=lambda: flameprog.load_settings(
                remote.live_flame_settings_path()),
            disarm=_disarm, journal=_journal,
            audit=getattr(remote, "audit", None),
            device_ips=lambda: flameprog.configured_ips(
                (getattr(fire_ice, "path", None),
                 flamesafe_config or fire_ice.flamesafe_config)))
    except Exception as e:                       # noqa: BLE001
        print(f"Live flame programming could not be set up: "
              f"{type(e).__name__}: {e}")
        return
    _close_before = httpd.server_close

    def _close_live_flame():
        try:
            remote.live_flame_end("the engine is closing")
        finally:
            _close_before()
    httpd.server_close = _close_live_flame

def _attach_hazers(httpd):
    """The hazers (hazers.py, Jeff 2026-10-29): built for the Fire & Ice
    engine with a flame link whose flamesafe config lists any. Every hazer
    DISARMED at start, its levels read from the settings folder. A failure
    leaves them unavailable (every hazer DISARMED, its slots zero) and is
    said once; the show is never held back."""
    from . import hazers as hazers_mod
    remote = httpd.remote
    link = getattr(httpd.fire_ice, "flame_link", None)
    if link is None or not link.hazer_names():
        return
    try:
        def _journal(who, screen, outcome, text, fault=False):
            remote._journal(who or "system", screen or "engine", "hazers",
                            outcome, text, fault=fault)
        fs = remote.flame_status
        remote.hazers = hazers_mod.Hazers(
            link_get=lambda: getattr(httpd.fire_ice, "flame_link", None),
            flames_get=lambda: fs.view() if fs is not None else None,
            abort_standing=remote._abort_standing,
            show_state=remote.hazer_show_state,
            blocker=remote.hazer_blocker, folder=remote.folder,
            journal=_journal, audit=getattr(remote, "audit", None))
    except Exception as e:                       # noqa: BLE001
        print(f"The hazers could not be set up: {type(e).__name__}: {e}")
        return
    say = getattr(link, "journal_line", None)
    if say is not None:
        say("Flame link: hazers " + "; ".join(
            f"{n} haze {sl[0]}, fan {sl[1]}" for n, sl in link.cfg.hazers)
            + f" read from {getattr(link, 'flamesafe_path', '')}. Every "
              f"hazer is DISARMED until armed; it runs at its levels only "
              f"while a show plays.", action="flame_link", outcome="hazers")


def _attach_pump(httpd):
    """The fireball's fuel pump (fuelpump.py, Jeff 2026-10-30): built for
    the Fire & Ice engine with a flame link whose flamesafe config lists
    one. OFF at start. A failure leaves it unavailable (OFF, its slot
    zero) and is said once; the show is never held back."""
    from . import fuelpump as fuelpump_mod
    remote = httpd.remote
    link = getattr(httpd.fire_ice, "flame_link", None)
    if link is None or link.pump_name() is None:
        return
    try:
        def _journal(who, screen, outcome, text, fault=False):
            remote._journal(who or "system", screen or "engine",
                            "fuel-pump", outcome, text, fault=fault)
        fs = remote.flame_status
        remote.pump = fuelpump_mod.FuelPump(
            link_get=lambda: getattr(httpd.fire_ice, "flame_link", None),
            flames_get=lambda: fs.view() if fs is not None else None,
            abort_standing=remote._abort_standing,
            blocker=remote.pump_blocker,
            journal=_journal, audit=getattr(remote, "audit", None),
            night_state=remote.pump_night_state,
            signed_out=remote.pump_signed_out)
    except Exception as e:                       # noqa: BLE001
        print(f"The fuel pump could not be set up: {type(e).__name__}: {e}")
        return
    say = getattr(link, "journal_line", None)
    if say is not None:
        n, slot, on = link.cfg.pump
        say(f"Flame link: fuel pump {n} on channel {slot} at {on}, read from "
            f"{getattr(link, 'flamesafe_path', '')}. It is OFF until turned "
            f"on; Stop and Abort turn it off.", action="flame_link",
            outcome="fuel pump")


def _attach_fuel(httpd):
    """The Fuel page (fuel.py, FUEL.md): the Morning page's machines, and
    the flame link watched when there is one. It only observes: a failure
    to build it leaves the page unavailable and is said once."""
    from . import fuel as fuel_mod
    remote = httpd.remote
    try:
        def _journal(text, fault=False):
            remote._journal("system", "engine", "fuel",
                            "fault" if fault else "done", text, fault=fault)
        remote.fuel = fuel_mod.build(
            remote, httpd.morning,
            getattr(httpd.fire_ice, "flame_link", None), journal=_journal)
    except Exception as e:                       # noqa: BLE001
        print(f"The Fuel page could not be set up: "
              f"{type(e).__name__}: {e}")
        return
    _close_before = httpd.server_close

    def _close_fuel():
        # Last: the flame link, the conductor and everything closed before
        # this was built go first, and its save never waits more than
        # fuel.CLOSE_TIMEOUT_S, so its disk can never hold up a safety step.
        try:
            _close_before()
        finally:
            remote.fuel.close()
    httpd.server_close = _close_fuel


def _attach_fireball(httpd):
    """The Fireball page (fireball.py, OPERATOR.md): the fireball group
    from the Startup page's reading of flamesafe's config, its purge rules
    and lockout, and the flame link's purge frame. Closed FIRST, so every
    channel is off before anything else stops."""
    from . import fireball as fireball_mod
    remote = httpd.remote
    try:
        def _journal(text, outcome="done", who="", screen="", fault=False):
            remote._journal(who or "system", screen or "engine", "fireball",
                            outcome, text, fault=fault)
        remote.fireball = fireball_mod.build(
            remote, httpd.morning, httpd.fire_ice.flame_link,
            journal=_journal)
    except Exception as e:                       # noqa: BLE001
        print(f"The Fireball page could not be set up: "
              f"{type(e).__name__}: {e}")
        return
    _close_before = httpd.server_close

    def _close_fireball():
        try:
            remote.fireball.close()
        finally:
            _close_before()
    httpd.server_close = _close_fireball


def _attach_fireball_timeline(httpd):
    """The fireball timeline on the flame cues (fireball_cues.py, Jeff
    2026-10-30): for a show set to "timeline", slots 55 to 59 come from
    the settings folder's ltcplay_fireball_timeline.json. Built before any
    show can run. If it cannot be built, every show's fireball slots are
    zero (fail closed: a show set to the timeline never falls back to its
    FSEQ), and that is said."""
    from . import fire_ice as fire_ice_mod
    from . import fireball_cues as fbc_mod
    remote = httpd.remote
    link = getattr(httpd.fire_ice, "flame_link", None)
    cues = getattr(link, "cues", None)
    if not isinstance(cues, fire_ice_mod.FlameCues):
        return
    say = getattr(link, "journal_line", None)
    try:
        def fireball_slots():
            # flamesafe's fireball group's fire slots, as the Fireball page
            # reads them (the Startup page's reading of flamesafe's
            # config), or None when there is none to compare.
            from . import fireball as fireball_mod
            g, _why = fireball_mod.find_group(
                getattr(getattr(httpd, "morning", None), "groups", None)
                or [])
            return g["fire"] if g else None
        fb = fbc_mod.FireballCues(
            remote.fireball_timeline_store(),
            charge_get=lambda: getattr(remote, "fireball_charge", None),
            journal=say, group_get=fireball_slots)
        fb.refresh()
    except Exception as e:                       # noqa: BLE001
        fb = fbc_mod.FireballCues(None, journal=say)
        fb._plan = (None, f"the fireball timeline could not be set up "
                          f"({type(e).__name__}: {e}); no show fires its "
                          f"fireball", {})
        print(f"The fireball timeline could not be set up: "
              f"{type(e).__name__}: {e}")
    cues.fireball = fb
    remote.fireball_cues = fb


def _attach_fireball_charge(httpd):
    """The fireball's charge state (fireball_charge.py, Jeff 2026-10-30):
    watches every flame frame after it went out (chained onto the flame
    link's observer, beside the fuel tracker's) and flamesafe's status.
    It never sends and never changes a value. A failure leaves it
    unavailable and is said once."""
    from . import fireball_charge as charge_mod
    remote = httpd.remote
    link = getattr(httpd.fire_ice, "flame_link", None)
    if link is None:
        return
    try:
        def _journal(who, screen, outcome, text, fault=False):
            remote._journal(who or "system", screen or "engine",
                            "fireball charge", outcome, text, fault=fault)
        fs = remote.flame_status
        tr = charge_mod.ChargeTracker(
            remote.folder, flames_get=lambda: fs.view() if fs is not None
            else None, journal=_journal, audit=getattr(remote, "audit", None))
    except Exception as e:                       # noqa: BLE001
        print(f"The fireball charge state could not be set up: "
              f"{type(e).__name__}: {e}")
        return
    before = link.observer

    def both(tc, values, ok, _before=before, _tr=tr):
        if _before is not None:
            try:
                _before(tc, values, ok)
            except Exception:                    # noqa: BLE001
                pass
        _tr.observe(tc, values, ok)
    link.observer = both
    remote.fireball_charge = tr
    fb = getattr(remote, "fireball", None)
    if fb is not None:
        fb.on_controls = lambda on, who, screen: tr.note_manual(on, who,
                                                                screen)
    _close_before = httpd.server_close

    def _close_charge():
        # The flame link (and everything closed before it) first, the
        # charge record last: it sees every frame that goes out (review
        # C-5: closed first, it missed the last frames).
        try:
            _close_before()
        finally:
            tr.close()
    httpd.server_close = _close_charge


def _attach_cleaning(httpd):
    """Wave flamer cleaning (cleaning.py, the Shutdown page): flamesafe's
    wave flamer group, the engine's hold to arm and disarm, the Startup
    page's lockout and purge frame, and the flame link. Closed FIRST, so
    its ignition is off and its disarm sent before anything else stops."""
    from . import cleaning as cleaning_mod
    remote = httpd.remote
    try:
        def _journal(text, outcome="done", who="", screen="", fault=False):
            remote._journal(who or "system", screen or "engine", "cleaning",
                            outcome, text, fault=fault)
        remote.cleaning = cleaning_mod.build(
            remote, httpd.morning, httpd.fire_ice.flame_link,
            journal=_journal, fuel=getattr(remote, "fuel", None))
    except Exception as e:                       # noqa: BLE001
        print(f"The wave flamer cleaning could not be set up: "
              f"{type(e).__name__}: {e}")
        return
    _close_before = httpd.server_close

    def _close_cleaning():
        try:
            remote.cleaning.close()
        finally:
            _close_before()
    httpd.server_close = _close_cleaning


def serve(folder, port=7878, bind="127.0.0.1", defaults=None, sd=None,
          token=None, on_ready=None, schedule=None, announce=None,
          madmapper=None, beyond=None, fire_ice=None,
          remote_folder=None, flamesafe_config=None, flame_disarm=None,
          accounts=False, config_watch=None, network_fallback=False,
          network_note=""):
    """`schedule` is the path of a schedule rule file, or a ready-made
    scheduler service. Without it the scheduler is not even imported: the
    GPL show runs exactly the program it ran before the scheduler existed.

    `announce` is the same shape for the announcements config: a path, or a
    ready-made AnnounceService. Without it announcements are not imported
    either. When BOTH are configured, the two are wired together in four
    directions, here and nowhere else: the announcement service reads the
    scheduler's state through its one-method provider (may I play), the
    scheduler pushes to the announcement service's on_show_started hook the
    instant it starts a show (stop, a show just started), the announcement
    service asks the scheduler to Hold, through hold_requester, before it
    ever plays (Jeff, 2026-09-26: every announcement triggers a Hold), and
    it asks again, read-only, through hold_still_claimed, immediately
    before the stream opens, so an operator's own Resume during the file
    read always wins rather than being silently undone (review round 2,
    2026-09-26: audit15_resume_race.py). Neither module imports the other;
    this function is the only place that knows both.

    This is also the only place that knows both the schedule rules and the
    show's own media (Jeff, 2026-09-26: show length follows the music,
    everywhere the code has access to it): if a schedule is configured
    and exactly one show file in `folder` names a clock block with a
    derivable show length, the schedule's own show_len_s is cross-checked
    against it before anything is served, and refused, naming both
    numbers, if it is shorter. More than one candidate, with no way to
    tell which one is the real one, refuses too, naming them, rather than
    guessing the first one alphabetically (review round 2, 2026-09-26).
    The check is never silently skipped: unable to identify or derive a
    show length is printed and journalled as a warning, not passed over
    in silence. schedule.py stays pure and schedule_service.py is not
    touched for this: see clock.derive_show_length_in_folder.

    `madmapper` is the same shape again, for madmapper.py: a validated
    MadMapperConfig, or a ready-made (Link, Watchdog) pair from
    madmapper.build(). Without it madmapper.py is not even imported, the
    same inertness announce.py and schedule_service.py each already rely
    on. `beyond` is the same shape for beyond.py's laser link: a
    BeyondConfig, or a ready-made Beyond from beyond.build().

    madmapper.py and beyond.py stay device layer only here: this function
    only constructs the links, exposes their read-only health routes, and
    -- when it built them itself, from a config -- closes them on
    server_close() (which blanks BEYOND, a safe default; see beyond.py). A
    ready-made pair or link passed in is the caller's own to close. Their
    Hold/Resume/Abort hooks (devices.py's on_hold/on_resume/on_abort,
    composing madmapper.py's and beyond.py's own primitives) are NOT wired
    to the scheduler by this function -- that sequencing belongs to the
    show conductor, built separately; see devices.py's module docstring.

    `fire_ice` is a fire_ice.FireIceConfig, given by `ltc serve` only when
    a schedule is (see fire_ice.py). It is the one place the real show
    conductor is built: its "madmapper" and "beyond" blocks become the two
    links above (unless the caller passed its own), and the conductor is
    attached to the scheduler BEFORE the scheduler's first tick. Without
    it, nothing below changes in any way, and fire_ice.py and conductor.py
    are never imported."""
    if fire_ice is not None and schedule is None:
        raise ValueError("The Fire & Ice show conductor needs the scheduler: "
                         "serve it with a schedule.")
    control = Control(folder, defaults=defaults, sd=sd)
    httpd_schedule = None
    if schedule is not None:
        if isinstance(schedule, str):
            from . import schedule_service
            schedule = schedule_service.Service(schedule)
        if schedule.rule is not None:
            from . import clock as clock_mod
            show_path, derived, warning = \
                clock_mod.derive_show_length_in_folder(folder)
            if show_path is not None:
                configured = schedule.rule.show_len_s
                if configured < derived:
                    # Plainly what to change (safety review of #157, 3):
                    # a show now runs to the longest of its audio, lights
                    # file and clock.zones.show_len_s.
                    need = int(math.ceil(derived - 1e-9))
                    raise ValueError(
                        f"ltcplay did not start: the schedule's show_len_s "
                        f"is {configured:g} s, but the show in "
                        f"{os.path.basename(show_path)} runs {derived:g} s "
                        f"(the longest of its audio, its lights file and "
                        f"'clock.zones.show_len_s'), so shows would "
                        f"overlap. In the schedule file "
                        f"{os.path.basename(getattr(schedule, 'path', '') or 'ltcplay_schedule.json')}"
                        f" set \"show_len_s\" to {need} (or more), then "
                        f"start ltcplay again.")
            elif warning:
                # Never silently skip the check (review round 2,
                # 2026-09-26): a schedule that could not be checked
                # against the show's media is not the same as one that
                # was checked and found fine, and the operator has to be
                # told which.
                msg = (f"The schedule's show_len_s could not be checked "
                      f"against the show's own media: {warning}.")
                print(msg)
                schedule._journal_line("system", msg,
                                       action="show length check",
                                       outcome="warning")
        httpd_schedule = schedule
    # Fix round 1 of #39, F4: "0", "0.0" and "000.000.000.000" are all
    # 0.0.0.0 to the operating system. Resolve the address the way bind()
    # will, and refuse it if ANY form of it means every interface.
    bind = normalize_bind(bind)
    on_network = bind not in LOOPBACK
    if on_network and bind in WILDCARD:
        # Every interface means the venue's network and anything else this
        # machine is on, not just the show Wi-Fi. One address, or none.
        raise ValueError(
            f"{bind or 'every interface'} would serve the engine on every "
            f"network this machine is on. Set its show Wi-Fi address on the "
            f"page (Show network), then start Web ltcplay on the show "
            f"network.")
    # The old shared link token is gone, and so is the PIN sign-in (Jeff,
    # 2026-10-07): the network is kept to the remote page's own routes by
    # network_may_reach(). `token` is accepted and ignored so an old caller
    # does not break.
    # network_fallback (the Windows supervisor's --network-if-saved): a
    # saved show network address this PC does not have right now (another
    # network, the cable out) must not stop the show engine. It then
    # listens on this PC only, and the Show network card says why.
    # network_note: why a saved address was not even tried (cli.py
    # _network_if_saved), said on the Show network card.
    network_why = str(network_note or "")
    try:
        httpd = ThreadingHTTPServer((bind, port), Handler)
    except OSError as e:
        if not (network_fallback and on_network):
            raise
        network_why = (f"The saved show network address {bind} could not "
                       f"be used ({e}), so the engine listens on this PC "
                       f"only. Check that this PC is on the show network "
                       f"with that address, then restart the engine.")
        print(network_why, flush=True)
        bind, on_network = "127.0.0.1", False
        httpd = ThreadingHTTPServer((bind, port), Handler)
    httpd.control = control
    httpd.token = None
    httpd.bind_address = bind
    httpd.daemon_threads = True
    httpd.schedule = None
    if httpd_schedule is not None:
        # With Fire & Ice the scheduler is started further down, once the
        # conductor is attached, so its first tick already reaches it.
        httpd.schedule = (httpd_schedule if fire_ice is not None
                          else httpd_schedule.start())
    from . import remote as remote_mod
    fstatus = None
    if flamesafe_config:
        try:
            fstatus = remote_mod.FlameStatus.from_config(
                flamesafe_config).start()
        except (OSError, ValueError, KeyError) as e:
            print(f"The flame lamps on the remote page are off: {e}")
    httpd.remote = remote_mod.Remote(control, httpd.schedule,
                                     folder=remote_folder,
                                     flame_status=fstatus,
                                     flame_disarm=flame_disarm)
    if accounts:
        # The show engine (`ltc serve`): its disarm ids start above the
        # last engine's, whatever the PC clock did (remote.py).
        httpd.remote.reserve_disarm_ids()
        # Named users and the audit log (users.py, audit.py, auditsend.py;
        # Jeff, 2026-10-20), for `ltc serve` (the show engine). A failure
        # here leaves them off and says so; the show is never held back.
        _attach_accounts(httpd)
    # The booth speaker and the blue pulse's pixels (alerts.py,
    # boothaudio.py), from ltcplay_remote.json. Off until configured; a
    # bad block is said once here and the booth stays off.
    try:
        rs = remote_mod.load_settings(remote_folder)
        if rs.get("booth_alert"):
            from .boothaudio import BoothPlayer, note_folders
            # booth_<kind>.wav in Settings and logs\Booth notes (a
            # person's own copy), then beside the program (the installer's).
            booth = BoothPlayer(rs["booth_alert"], sd=sd,
                                folders=note_folders(
                                    remote_folder or
                                    remote_mod.settings_folder()))
            problems = booth.check_files()
            for kind, why in sorted(problems.items()):
                print(f"Booth note for {kind}: {why}")
            nv = booth.notes()
            if nv["missing"]:
                print(f"Booth notes: {nv['found']} of {nv['expected']} "
                      f"found; missing {', '.join(nv['missing'])}.")
            httpd.remote.alerts.booth = booth
        httpd.remote.alert_pixels = rs.get("alert_pixels")
    except ValueError as e:
        print(f"The booth speaker is off: {e}")
    # The rack screen's Weather block (weather.py, WEATHER.md): its own
    # thread, display only, never anything the show waits on. On by
    # default for Fire & Ice (Thanksgiving Point); any other engine only
    # with a "weather" block in ltcplay_remote.json. A failure here leaves
    # the block saying so; the engine carries on.
    try:
        from . import weather as weather_mod
        _wx = weather_mod.start_for(
            remote_folder or remote_mod.settings_folder(),
            default_on=fire_ice is not None)
        httpd.remote.weather = _wx
        if _wx is not None:
            _close_before_wx = httpd.server_close

            def _close_wx():
                try:
                    _wx.stop()
                finally:
                    _close_before_wx()
            httpd.server_close = _close_wx
    except Exception as e:                           # noqa: BLE001
        print(f"The weather block could not start: {type(e).__name__}: {e}")
    # The alert workers (alerthold.py) stop with the server.
    _close_before_alerts = httpd.server_close

    def _close_alerts():
        try:
            httpd.remote.close_alerts()
        finally:
            _close_before_alerts()
    httpd.server_close = _close_alerts
    httpd.audio_watch = None
    if fire_ice is not None and not os.environ.get("LTCPLAY_BENCH_FAKE_AUDIO"):
        # Fire & Ice: is the show's audio interface attached, before anyone
        # presses Run (audiowatch.py)? It lists the outputs from a process
        # of its own, never while a session runs. Not under the soak's fake
        # audio device, which no other process could see.
        from . import audiowatch

        def _busy():
            st = control.state() or {}
            return bool(st.get("running") or st.get("starting"))
        httpd.audio_watch = audiowatch.AudioInterfaceWatch(
            control.folder, alerts=httpd.remote.alerts, log=print,
            busy=_busy).start()
        httpd.remote.audio_watch = httpd.audio_watch
    httpd.loopback = None
    if on_network:
        # The machine itself keeps its own door on loopback, trusted as
        # before, beside the one show-network address.
        lb = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        lb.control, lb.token, lb.bind_address = control, None, "127.0.0.1"
        lb.daemon_threads = True
        lb.schedule, lb.remote = httpd.schedule, httpd.remote
        httpd.loopback = lb
        threading.Thread(target=lb.serve_forever,
                         kwargs={"poll_interval": 0.2}, daemon=True,
                         name="ltcplay-web-loopback").start()
    _say_listening(httpd, on_network, network_why)
    httpd.announce = None
    if announce is not None:
        if isinstance(announce, str):
            from . import announce as announce_mod
            announce = announce_mod.AnnounceService(announce)
        httpd.announce = announce
    if httpd.announce is not None and \
            getattr(httpd.announce, "show_route", False) is None:
        # Announcements play through the show audio while a show audio
        # process runs (Jeff's field report, 2026-10-28, item 9): the
        # running session's show clock, when it plays audio of its own.
        # showaudio is imported only once such a clock exists (it has
        # already loaded it), so the GPL path never loads it.
        def _show_route(control=control):
            s = control.session
            if s is None or not getattr(s, "running", False):
                return None
            clk = getattr(s, "clock", None)
            if clk is None or getattr(clk, "source", None) != "audio_master":
                return None
            from . import showaudio
            return showaudio.ShowRoute.of_clock(clk)
        httpd.announce.show_route = _show_route
    if httpd.announce is not None and httpd.schedule is not None:
        sched = httpd.schedule
        httpd.announce.state_provider = (
            lambda: sched.machine.state if sched.machine is not None
            else None)
        # The other half of the same bridge, in the other direction: the
        # scheduler pushes the instant it starts a show, rather than the
        # announcement service finding out on its own next status() poll.
        # See announce.py's module docstring and on_show_started.
        sched.on_show_started = httpd.announce.on_show_started
        # The third direction: every announcement Holds the scheduler
        # first, through the same path an operator's own Hold uses. See
        # Service.hold_for_announcement and announce.py's play().
        # Wrapped by the remote (Remote.announce_hold): when it really
        # holds, the operator Hold's safety cuts go with it.
        httpd.announce.hold_requester = httpd.remote.announce_hold(sched)
        # The read-only partner to the above, for the second check right
        # before the stream opens: never re-Holds, so an operator's own
        # Resume during the file read always wins (review round 2,
        # 2026-09-26: audit15_resume_race.py). See Service.hold_still_claimed
        # and announce.py's _check_still_held.
        httpd.announce.hold_still_claimed = sched.hold_still_claimed
    httpd.fire_ice_config = fire_ice
    if fire_ice is not None:
        if madmapper is None:
            madmapper = fire_ice.madmapper
        if beyond is None:
            beyond = fire_ice.beyond
    httpd.madmapper = None
    _built_madmapper = False
    if madmapper is not None:
        from . import madmapper as madmapper_mod
        if isinstance(madmapper, madmapper_mod.MadMapperConfig):
            # A real journal for the Link and watchdog too (independent
            # review of PR #29, finding A): a failed MadMapper send must
            # land somewhere a person reads, never nowhere.
            madmapper = madmapper_mod.build(
                madmapper, journal=_device_journal(httpd_schedule))
            madmapper[1].start()      # the watchdog's own listener thread
            _built_madmapper = True
        httpd.madmapper = madmapper
    httpd.beyond = None
    _built_beyond = False
    if beyond is not None:
        from . import beyond as beyond_mod
        if isinstance(beyond, beyond_mod.BeyondConfig):
            beyond = beyond_mod.build(beyond,
                                      journal=_device_journal(httpd_schedule))
            _built_beyond = True
        httpd.beyond = beyond
    if _built_madmapper or _built_beyond:
        # Close only what this call built from a config: a ready-made pair
        # or link the caller passed in is the caller's own to close, on
        # whatever schedule the caller (a test, a future conductor) wants.
        _orig_server_close = httpd.server_close

        def _server_close():
            _orig_server_close()
            if _built_madmapper:
                link, watchdog = httpd.madmapper
                link.close()
                watchdog.stop()
            if _built_beyond:
                httpd.beyond.close()
        httpd.server_close = _server_close
    if fire_ice is not None:
        from . import fire_ice as fire_ice_mod
        wiring = fire_ice_mod.attach(
            httpd_schedule, control, fire_ice, madmapper=httpd.madmapper,
            beyond=httpd.beyond, announce=httpd.announce,
            journal=_beyond_journal(httpd_schedule),
            alert_pixels=httpd.remote.alert_pixels)
        httpd.conductor = wiring.conductor
        httpd.fire_ice = wiring
        # Every laser starts disarmed (Jeff, 2026-10-22): the blank and
        # BEYOND's DisableLaserOutput go out here, whatever happens to the
        # Morning page's laser arm below (review of PR #87, round 2, P3-5:
        # a page that fails to build must not leave the output enabled).
        _startup_lasers_dark(wiring)
        # The rack screen shows a flame cue fault too (P1-3), and a render
        # older than the layout is an alert from show load on (soak
        # 2026-10-21): it used to be a journal line only.
        httpd.remote.flame_cues = getattr(wiring.flame_link, "cues", None)
        # Aim slots (2026-10-28): this engine's, and the file they came
        # from, for the rack screen's aim line and the mismatch alert.
        # Flame frame timing (2026-10-29): the rack screen's Systems line
        # and the display only "timing" alert read the link's own counts.
        httpd.remote.flame_link_get = (
            lambda: getattr(httpd.fire_ice, "flame_link", None))
        if wiring.flame_link is not None:
            httpd.remote.flame_aim = (
                frozenset(getattr(wiring.flame_link.cfg, "aim_slots", ())),
                getattr(wiring.flame_link, "flamesafe_path", ""))
        if hasattr(httpd.remote.flame_cues, "on_render_fault"):
            httpd.remote.flame_cues.on_render_fault = \
                httpd.remote.render_fault_alert
        # The rig check before each show (preroll.py), before the
        # scheduler's first tick.
        _attach_preroll(httpd, fire_ice, fstatus)
        httpd_schedule.start()
        _close_before_fire_ice = httpd.server_close

        def _close_fire_ice():
            # The conductor and the runner first, while the links they
            # send through are still open.
            try:
                wiring.close()
            finally:
                _close_before_fire_ice()
        httpd.server_close = _close_fire_ice
    if httpd.loopback is not None:
        httpd.loopback.announce = httpd.announce
    if getattr(httpd, "remote", None) is not None:
        # The rack screen's and the Stream Deck's announcement presses go
        # through the remote routes (remote.announce_post).
        httpd.remote.announce = httpd.announce
    httpd.morning = None
    if fire_ice is not None:
        # The Morning and Shutdown page (Jeff, 2026-10-17; morning.py,
        # MORNING.md): the purge, the lockout, the laser tests and the
        # checklists. Fire & Ice only: it needs the flame link and
        # flamesafe's status. Its settings live beside the schedule's.
        from . import morning as morning_mod
        _bey = getattr(fire_ice, "beyond", None)
        _fs = fstatus

        def _flames_view(_fs=_fs):
            return _fs.view() if _fs is not None else None

        def _group_names(_fs=_fs):
            return list(_fs.names) if _fs is not None else []
        def _laser_output():
            # The conductor's BEYOND (beyondtc.Blanking): the laser arm's
            # one way to BEYOND's laser output switch.
            return getattr(getattr(httpd.fire_ice, "devices", None),
                           "beyond", None)

        def _laser_blank(what):
            return httpd.conductor.lasers_dark_now(what)

        def _night_state():
            m = getattr(httpd_schedule, "machine", None)
            return m.state if m is not None else None
        try:
            httpd.morning = morning_mod.Morning(
                httpd.remote.folder, flamesafe_config=flamesafe_config,
                link_get=lambda: getattr(httpd.fire_ice, "flame_link", None),
                flames_get=_flames_view,
                live_check=httpd.remote.morning_live_refusal,
                group_names=_group_names if fstatus is not None else None,
                journal=httpd.remote._morning_journal,
                night=getattr(httpd_schedule, "_night", None),
                laser_host=getattr(_bey, "host", None),
                laser_port=getattr(_bey, "port", None),
                logs_dir=os.path.join(httpd.remote.folder, "logs"),
                laser_output=_laser_output,
                laser_blockers=httpd.remote.laser_arm_blockers,
                laser_blank=_laser_blank, night_state=_night_state,
                audit=getattr(httpd.remote, "audit", None))
            httpd.remote.morning = httpd.morning
            # The timed purge's engine checks (a Hold, Run, the flame link,
            # the starter's sign-in): without them no timed purge starts.
            httpd.morning.timed_blockers = httpd.remote.timed_purge_blockers
            # The laser arm (Jeff, 2026-10-22): the conductor's laser gate,
            # its Abort and Hold disarm and the Blanking's unblank all ask
            # it from here on. Without it the lasers stay dark.
            httpd.fire_ice.attach_laser_arm(httpd.morning.laser_arm)
            if httpd.morning.config_error:
                print(f"Morning and Shutdown: {httpd.morning.config_error}")
            _close_before_morning = httpd.server_close

            def _close_morning():
                try:
                    httpd.morning.close()
                finally:
                    _close_before_morning()
            httpd.server_close = _close_morning
        except Exception as e:                       # noqa: BLE001
            # The page is a convenience; a failure to build it never stops
            # the show engine. Said once, here.
            print(f"The Morning and Shutdown page could not be built: "
                  f"{type(e).__name__}: {e}")
    if fire_ice is not None and \
            getattr(httpd.fire_ice, "flame_link", None) is not None:
        _attach_fireball_timeline(httpd)
        _attach_hazers(httpd)
        _attach_pump(httpd)
    if httpd.morning is not None:
        _attach_fuel(httpd)
        if fire_ice is not None and \
                getattr(httpd.fire_ice, "flame_link", None) is not None:
            _attach_fireball(httpd)
            _attach_cleaning(httpd)
    if fire_ice is not None and \
            getattr(httpd.fire_ice, "flame_link", None) is not None:
        # After the fuel tracker (its observer is chained, never replaced)
        # and the Fireball page (who pressed is told here).
        _attach_fireball_charge(httpd)
    if fire_ice is not None and \
            getattr(httpd.fire_ice, "flame_link", None) is not None and \
            getattr(fire_ice, "flame_controller", None):
        _attach_live_flame(httpd, fire_ice, flamesafe_config)
    _bey_cfg = getattr(fire_ice, "beyond", None) if fire_ice else None
    if _bey_cfg is not None:
        # Open BEYOND on the Startup page (windowfront.py) refuses when
        # BEYOND is set up on another machine.
        httpd.remote.beyond_host = getattr(_bey_cfg, "host", None)
        # Is BEYOND still running (2026-10-23)? Its own thread, display
        # only: the Lasers lamp and an alert when it is gone.
        from . import beyondwatch as beyondwatch_mod
        try:
            _bw = beyondwatch_mod.BeyondWatch(
                beyondwatch_mod.probe_for(getattr(_bey_cfg, "host", None)),
                on_change=httpd.remote.beyond_link_changed).start()
            httpd.remote.beyond_watch = _bw
            _close_before_bw = httpd.server_close

            def _close_bw():
                try:
                    _bw.stop()
                finally:
                    _close_before_bw()
            httpd.server_close = _close_bw
        except Exception as e:                       # noqa: BLE001
            print(f"The BEYOND watch could not start: "
                  f"{type(e).__name__}: {e}")
    _mm_cfg = getattr(fire_ice, "madmapper", None) if fire_ice else None
    if _mm_cfg is not None:
        # Is MadMapper running (Jeff, 2026-10-26)? The BEYOND watch's
        # twin, its own thread, display only: the Video lamp says so
        # before any show plays, and an alert when it is gone. Its
        # heartbeat, when the show file has a heartbeat block, is read
        # from the watchdog web.serve already started; nothing new listens
        # or sends.
        from . import madmapperwatch as mmwatch_mod
        try:
            _mw = mmwatch_mod.watch(
                getattr(_mm_cfg, "host", None),
                on_change=httpd.remote.madmapper_link_changed).start()
            httpd.remote.madmapper_watch = _mw
            if isinstance(httpd.madmapper, tuple):
                httpd.remote.madmapper_heartbeat = httpd.madmapper[1]
            httpd.remote.madmapper_heartbeat_cfg = getattr(
                _mm_cfg, "heartbeat", None)
            _close_before_mw = httpd.server_close

            def _close_mw():
                try:
                    _mw.stop()
                finally:
                    _close_before_mw()
            httpd.server_close = _close_mw
        except Exception as e:                       # noqa: BLE001
            print(f"The MadMapper watch could not start: "
                  f"{type(e).__name__}: {e}")
        # MadMapper's output (showwindows.py): wired whether or not the
        # watch above started (review A of #176).
        _attach_mm_output(httpd, fire_ice, _mm_cfg)
    if fire_ice is not None and fstatus is not None and \
            getattr(httpd.fire_ice, "flame_link", None) is not None:
        # flamesafe's status_mirror_port (PR #39): the remote page's copy of
        # every status frame also reaches the flame link, so ltc serve sees
        # flamesafe confirm a disarm and raises the lock alarm itself. Only
        # frames keyed with flamesafe's key (remote.decode_status) count.
        _link = httpd.fire_ice.flame_link
        _note_before = fstatus.note

        def _note_and_link(data, _before=_note_before):
            _before(data)
            obj = remote_mod.decode_status(data, fstatus.key)
            if obj is not None:
                _link.note_status(obj)
        fstatus.note = _note_and_link
        _beyond_journal(httpd_schedule)(
            "Flame link: flamesafe's status frames reach this program "
            "through link.status_mirror_port, so a disarm's confirmation and "
            "the lock alarm are seen here.", action="flame_link",
            outcome="status_mirror")
    if httpd.loopback is not None:
        # The loopback door answers the same optional routes.
        for _name in ("conductor", "fire_ice", "madmapper", "beyond",
                      "morning"):
            if hasattr(httpd, _name):
                setattr(httpd.loopback, _name, getattr(httpd, _name))
    if config_watch is not None:
        _attach_config_watch(httpd, config_watch)
    if on_ready:
        on_ready(httpd, control, token)
    return httpd


def _say_listening(httpd, on_network, why=""):
    """Where the engine's page really listens: kept for the Show network
    card (remote.Remote.listen), printed for the log and one journal line
    at start. Never raises."""
    try:
        port = httpd.server_address[1]
        addrs = ["127.0.0.1"] + ([httpd.bind_address] if on_network else [])
        httpd.remote.listen = {"addresses": addrs, "port": port, "why": why}
        line = ("The engine's page listens on " +
                " and ".join(f"{a}:{port}" for a in addrs) +
                ("." if on_network else
                 ", this PC only: no show network address is in use."))
        print(line, flush=True)
        if getattr(httpd.remote, "schedule", None) is not None:
            # The show journal (without a scheduler there is none, and
            # Remote._journal would only print the line a second time).
            httpd.remote._journal("system", "engine", "web page",
                                  "listening",
                                  f"{line} {why}" if why else line)
    except Exception as e:                       # noqa: BLE001
        print(f"Where the page listens could not be noted: "
              f"{type(e).__name__}: {e}")


def _attach_config_watch(httpd, watch):
    """Settings changed on disk since this engine read them (configwatch.py,
    Jeff 2026-10-28): its own thread, display only, on the rack screen and
    the status. The announcements file alone is read again live, once no
    announcement plays and no show is running or starting. Never raises."""
    try:
        remote = httpd.remote
        remote.config_watch = watch
        watch._journal = remote.config_journal
        ann = getattr(httpd, "announce", None)
        if ann is not None and hasattr(ann, "reload_if_idle") and \
                getattr(ann, "config_path", None):
            watch.set_reload(ann.config_path, lambda: ann.reload_if_idle(
                remote.show_busy_for_reload()))
            # The picker's own save (announce.save_device) tells the watch
            # the engine's copy is the file on disk.
            ann.config_watch = watch
        watch.check(remote.programs_now())
        watch.start(remote.programs_now)
        _close_before_cw = httpd.server_close

        def _close_cw():
            try:
                watch.stop()
            finally:
                _close_before_cw()
        httpd.server_close = _close_cw
    except Exception as e:                       # noqa: BLE001
        print(f"The settings file watch could not start: "
              f"{type(e).__name__}: {e}")
