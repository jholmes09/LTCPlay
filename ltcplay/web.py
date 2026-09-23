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
import os
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
LOOPBACK = ("127.0.0.1", "::1", "localhost")


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
        with open(path) as fh:
            doc = json.load(fh)
    except Exception:
        return False
    return isinstance(doc, dict) and isinstance(doc.get("cues"), list)


class Control:
    """Holds at most one running show, and the answers the page needs.

    One show at a time, guarded by a lock, because two players sending to the
    same universes would fight frame by frame and the result on the rig would
    look like a hardware fault."""

    def __init__(self, folder, defaults=None, sd=None):
        self.folder = folder
        self.defaults = defaults or {}
        self.session = None
        self.lock = threading.Lock()
        self._starting = False
        self._start_gen = 0
        self.last_error = ""
        self._sd = sd

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
        for p in sorted(glob.glob(os.path.join(self.folder, "*.json"))):
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
        with open(path) as fh:
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
            with open(p, errors="replace") as fh:
                return fh.read().splitlines()[-int(n):]
        except OSError:
            return []


class Handler(BaseHTTPRequestHandler):
    server_version = "ltcplay"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):
        pass                      # the show log is the log; this is noise

    def _authorised(self):
        token = self.server.token
        if not token:
            return True
        host = self.client_address[0]
        if host in LOOPBACK:
            return True
        q = urllib.parse.urlparse(self.path).query
        given = urllib.parse.parse_qs(q).get("t", [None])[0]
        if given is None:
            given = self.headers.get("X-ltcplay-token")
        return secrets.compare_digest(str(given or ""), token)

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        route = urllib.parse.urlparse(self.path).path
        if not self._authorised():
            return self._send(403, {"error": "This machine is serving on the "
                                             "network, so a token is needed. "
                                             "It is printed where the server "
                                             "started."})
        c = self.server.control
        try:
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
                    kind = ("image/png" if name.endswith(".png") else
                            "image/svg+xml" if name.endswith(".svg") else
                            "application/octet-stream")
                    with open(f, "rb") as fh:
                        return self._send(200, fh.read(), kind)
                return self._send(404, {"error": "no such thing here"})
            if route == "/api/brand":
                return self._send(200, brand_mod.load())
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
        except USER_ERRORS as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        return self._send(404, {"error": "no such thing here"})

    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path
        if not self._authorised():
            return self._send(403, {"error": "token required"})
        c = self.server.control
        body = self._body()
        try:
            if route == "/api/find":
                return self._send(200, c.find(float(body.get("seconds", 3.0))))
            if route == "/api/input":
                return self._send(200, {"saved": c.set_input(
                    body.get("device"), body.get("channel"), body.get("rate"))})
            if route == "/api/check":
                return self._send(200, c.check(body.get("timeline", "")))
            if route == "/api/start":
                s = c.start(body.get("timeline", ""),
                            no_output=bool(body.get("no_output")),
                            on_lost=body.get("on_lost"),
                            allow_missing=bool(body.get("allow_missing")),
                            auto_reload=body.get("auto_reload"))
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
                return self._send(200, {"stopped": c.stop()})
        except USER_ERRORS as e:
            c.last_error = str(e)
            return self._send(400, {"error": str(e)})
        except Exception as e:
            c.last_error = f"{type(e).__name__}: {e}"
            return self._send(500, {"error": c.last_error})
        return self._send(404, {"error": "no such thing here"})


def serve(folder, port=7878, bind="127.0.0.1", defaults=None, sd=None,
          token=None, on_ready=None):
    control = Control(folder, defaults=defaults, sd=sd)
    on_network = bind not in LOOPBACK
    if on_network and token is None:
        # Anyone who can reach this port can black out the rig. On a venue
        # network that is not a theoretical concern, so serving off loopback
        # gets a token whether or not anybody asked for one.
        token = secrets.token_urlsafe(9)
    httpd = ThreadingHTTPServer((bind, port), Handler)
    httpd.control = control
    httpd.token = token if on_network else None
    httpd.daemon_threads = True
    if on_ready:
        on_ready(httpd, control, token)
    return httpd
