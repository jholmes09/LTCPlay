"""Fire & Ice: the one place `ltc serve` builds a real show conductor.

Imported ONLY when `ltc serve` is given a schedule (`--schedule`), which is
how this codebase already tells Fire & Ice from GPL: the GPL launchers never
pass it, so on the Mac this module, conductor.py, devices.py, madmapper.py
and beyond.py are never loaded (test_ltc_serve_gpl_builds_no_conductor).

What it builds, from conductor.py's own integration notes:

  ShowOutputs   FireIceShow, below: ltcplay's own side.
    music       the running session's clock.AudioMaster: pause(fade_ms)
                for Hold, resume(fade_ms) for Resume, halt(fade_ms) for
                Abort, each with the conductor's own fade (rehearsal: 0).
    frozen      from the clock's real on_pause/on_resume signal, chained
                after the session's own, never from the request
                (AudioMaster.paused is already True during the fade).
    pixels      the existing pixel output path: the player's override, the
                same "blackout" the page's Blackout button sets. It has no
                fade, so "fade to black" is black at once, journaled as such.
    flames      ltcplay has no flame link to flamesafe in this build: it
                sends no flame cue frames at all (flamesafe/CONTRACT.md
                describes ltcplay's side; nothing implements it yet). So
                zero and release reach nothing, and say so, and
                flames_disarm_all FAILS LOUDLY: the contract has no disarm
                message, and this module does not invent one. A
                `flame_link` with zero()/release() can be handed in later.
  DeviceOutputs conductor.ConductorDevices on web.serve's own MadMapper and
                BEYOND links, built from this config's "madmapper" and
                "beyond" blocks; either may be absent, and the conductor then
                says "not connected" at start.
  laser gate    conductor.laser_gate_for(the scheduler's state).
  hold_gate     schedule_service.Service.hold_for_announcement.
  announcer     announce.AnnounceService.play, when --announce is given.

The config file is ltcplay_fire_ice.json beside the schedule rule file. It
is optional: without it the conductor is built with no lasers or video and
the scheduler stays a dry run. Its one switch that changes what the
scheduler does is "scheduler_performs" (see ShowRunner and BENCH.md):
false, the default, is today's dry run exactly.
"""
import json
import os
import threading

from . import conductor as C

CONFIG_FILE = "ltcplay_fire_ice.json"
KEYS = frozenset(("scheduler_performs", "show_cue", "madmapper", "beyond",
                  "notes"))


class FireIceConfigError(ValueError):
    """The Fire & Ice config is wrong, in a sentence."""


def config_path_for(schedule_path):
    """ltcplay_fire_ice.json in the same folder as the schedule rule file."""
    return os.path.join(os.path.dirname(os.path.abspath(schedule_path)),
                        CONFIG_FILE)


class FireIceConfig:
    """The Fire & Ice settings. Defaults are today's behavior: a dry-run
    scheduler, no MadMapper, no BEYOND."""

    def __init__(self, scheduler_performs=False, show_cue=None,
                 madmapper=None, beyond=None, path=None):
        self.scheduler_performs = scheduler_performs
        self.show_cue = show_cue
        self.madmapper = madmapper
        self.beyond = beyond
        self.path = path

    @classmethod
    def parse(cls, doc, where=CONFIG_FILE):
        if not isinstance(doc, dict):
            raise FireIceConfigError(f"{where}: it has to be one JSON "
                                     f"object.")
        unknown = sorted(k for k in doc if k not in KEYS)
        if unknown:
            raise FireIceConfigError(
                f"{where}: {', '.join(repr(k) for k in unknown)} is not a "
                f"setting this file has. It takes: "
                f"{', '.join(sorted(KEYS))}.")
        performs = doc.get("scheduler_performs", False)
        if performs is not True and performs is not False:
            # Only the JSON words true and false. "yes", 1 or "true" are
            # refused: a switch that changes what the scheduler does on show
            # night is never guessed from something that looks like one.
            raise FireIceConfigError(
                f"{where}: 'scheduler_performs' has to be true or false, "
                f"not {performs!r}.")
        cue = doc.get("show_cue")
        if cue is not None and (not isinstance(cue, str) or not cue.strip()):
            raise FireIceConfigError(
                f"{where}: 'show_cue' is the name of the show's cue in the "
                f"show file, or leave it out for the first cue.")
        mm = bey = None
        if "madmapper" in doc:
            from . import madmapper as madmapper_mod
            mm = madmapper_mod.MadMapperConfig.parse(doc["madmapper"], where)
        if "beyond" in doc:
            from . import beyond as beyond_mod
            bey = beyond_mod.BeyondConfig.parse(doc["beyond"], where)
        return cls(performs, cue.strip() if cue else None, mm, bey, where)

    @classmethod
    def load(cls, path):
        """The file, or the defaults when there is none. A file that is
        there but wrong raises: serve refuses to start rather than run
        Fire & Ice on settings it could not read."""
        if not os.path.exists(path):
            return cls()
        try:
            with open(path, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            raise FireIceConfigError(f"{path}: could not be read: {e}")
        return cls.parse(doc, path)

    def summary(self):
        return ("scheduler performs" if self.scheduler_performs
                else "scheduler dry run") + \
            (f", MadMapper {self.madmapper.summary()}" if self.madmapper
             else ", no MadMapper") + \
            (", BEYOND " + self.beyond.summary() if self.beyond
             else ", no BEYOND")


# ----------------------------------------------------------- ShowOutputs --

NO_FLAME_LINK = (
    "ltcplay has no flame link to flamesafe in this build: it sends no flame "
    "cue frames at all, so the conductor's flame cue commands reach nothing.")
NO_DISARM = (
    "A screen-initiated Abort cannot disarm the flame groups: flamesafe's "
    "link contract (flamesafe/CONTRACT.md, version 2) has no disarm message "
    "yet. Flame cues from ltcplay are zero. The Stream Deck's own Abort "
    "disarms through its own link; until the contract has a disarm message, "
    "disarm with the Stream Deck or the arm keys.")


class FireIceShow(C.ShowOutputs):
    """conductor.ShowOutputs for Fire & Ice. `control` is web.Control: the
    running session, its clock and its player are looked up on every call,
    so a Stop and a new Run are followed without rebuilding anything.

    Same rules as every output: returns a conductor Result, never raises,
    returns at once (AudioMaster's pause, resume and halt only send a
    message to the audio process)."""

    def __init__(self, control, journal=None, flame_link=None):
        self.control = control
        self._journal = journal
        self.flame_link = flame_link
        self.flames = C.ZERO        # what the conductor last asked for
        self._lock = threading.Lock()
        self._hooked = None         # the clock whose callbacks are chained
        self._frozen = None         # True/False once hooked, from the clock
        self._pix_prev = None       # the override before ours, if ours
        self._pix_ours = False
        if flame_link is None:
            self._note(NO_FLAME_LINK, fault=True, action="flames",
                       outcome="not_configured")

    def _note(self, text, **fields):
        if self._journal is not None:
            try:
                self._journal(text, **fields)
            except Exception:
                pass

    # -- the session's clock ------------------------------------------------
    def _session(self):
        s = getattr(self.control, "session", None)
        if s is None or not getattr(s, "running", False):
            return None
        return s

    def _clock(self):
        s = self._session()
        clk = getattr(s, "clock", None) if s is not None else None
        if clk is None or getattr(clk, "source", None) != "audio_master":
            return None
        self._hook(clk)
        return clk

    def _hook(self, clk):
        """Chain onto the clock's own on_pause/on_resume, after whatever the
        session set (the player's hard park), once per clock. The moment
        those fire is the moment the timecode froze or moved again."""
        with self._lock:
            if self._hooked is clk:
                return
            self._hooked = clk
            self._frozen = bool(getattr(clk, "_paused", False))
            before_p, before_r = clk.on_pause, clk.on_resume

            def on_pause():
                try:
                    if before_p is not None:
                        before_p()
                finally:
                    if self._hooked is clk:
                        self._frozen = True

            def on_resume():
                try:
                    if before_r is not None:
                        before_r()
                finally:
                    if self._hooked is clk:
                        self._frozen = False
            clk.on_pause, clk.on_resume = on_pause, on_resume

    def _no_clock(self, what):
        return C.failed(f"{what} was not sent: no show is running on this "
                        f"machine's show audio (Run not pressed, or the "
                        f"show file's clock is not \"audio_master\").")

    def playing(self):
        clk = self._clock()
        return bool(clk is not None and clk.playing)

    def _music(self, what, call):
        clk = self._clock()
        if clk is None:
            return self._no_clock(what)
        try:
            call(clk)
        except Exception as e:
            return C.failed(f"{what} failed: {e}")
        return C.done(f"{what}: sent to the show audio.")

    def music_hold(self, fade_s):
        return self._music(f"Music fade out over {fade_s:g} s and freeze",
                           lambda c: c.pause(fade_ms=fade_s * 1000.0))

    def music_resume(self, fade_s):
        return self._music(f"Music back in over {fade_s:g} s",
                           lambda c: c.resume(fade_ms=fade_s * 1000.0))

    def music_halt(self, fade_s):
        return self._music(f"Music fade out over {fade_s:g} s and stop",
                           lambda c: c.halt(fade_ms=fade_s * 1000.0))

    def music_frozen(self):
        """True once the clock has frozen, False once it is moving, None
        when there is no clock to ask (the conductor treats that as not
        confirmed, and says so)."""
        if self._clock() is None:
            return None
        return self._frozen

    # -- pixels: the player's override ---------------------------------------
    def _player(self):
        s = self._session()
        return (s, getattr(s, "player", None)) if s is not None \
            else (None, None)

    def pixels_fade_out(self, seconds):
        s, p = self._player()
        if p is None:
            return C.failed("Pixels to black was not sent: nothing is "
                            "running. Press Run first.")
        with self._lock:
            if not self._pix_ours:
                self._pix_prev = p.override
                self._pix_ours = True
            p.override = "blackout"
        if s.log:
            s.log.event("override", "show conductor set output to blackout")
        if seconds > 0:
            self._note(f"The pixels went black at once, not over "
                       f"{seconds:g} s: the pixel output has no fade.",
                       action="pixels", outcome="black_not_faded")
        return C.done("Pixels black.")

    def pixels_restore(self, seconds):
        s, p = self._player()
        if p is None:
            return C.failed("Pixels back was not sent: nothing is running.")
        with self._lock:
            if not self._pix_ours:
                return C.done("Pixels: the show conductor had not taken "
                              "them, so they were left as they are.")
            self._pix_ours = False
            if p.override != "blackout":
                # Someone pressed a look on the page since: theirs stands.
                self._note(f"Pixels left on {p.override or 'auto'}: the "
                           f"operator changed the look while the show "
                           f"conductor had them black.", action="pixels",
                           outcome="left")
                return C.done("Pixels left on the operator's look.")
            p.override = self._pix_prev
        if s.log:
            s.log.event("override", f"show conductor set output to "
                                    f"{p.override or 'auto'}")
        return C.done("Pixels back.")

    # -- flames ---------------------------------------------------------------
    def flames_zero(self):
        self.flames = C.ZERO
        if self.flame_link is None:
            return C.done("Flame cues zero: ltcplay sends none in this "
                          "build.")
        try:
            ok = self.flame_link.zero()
        except Exception as e:
            return C.failed(f"Flame cues to zero failed: {e}")
        return C.done("Flame cues zero.") if ok is True else \
            C.failed("Flame cues to zero did not go out.")

    def flames_release(self):
        self.flames = C.LIVE
        if self.flame_link is None:
            self._note("Flame cues released by the show conductor, but " +
                       NO_FLAME_LINK,
                       action="flames", outcome="nothing_sent")
            return C.done("Flame cues released; none are sent in this "
                          "build.")
        try:
            ok = self.flame_link.release()
        except Exception as e:
            return C.failed(f"Flame cues release failed: {e}")
        return C.done("Flame cues released.") if ok is True else \
            C.failed("Flame cues release did not go out.")

    def flames_disarm_all(self, reason):
        """Never a quiet success: the cues go to zero, and the answer is a
        failure carrying NO_DISARM, which the conductor writes down as a
        fault, every time."""
        z = self.flames_zero()
        tail = "" if z.ok else f" Zeroing the cues also failed: {z.sentence}"
        return C.failed(f"{reason}: {NO_DISARM}{tail}")


# ------------------------------------------------------------- the runner --

class ShowRunner:
    """What the scheduler's dry run used to make up, done for real, only
    while Service.dry_run is False (config "scheduler_performs": true).
    schedule.py's "Contract for PR 3":

      START_SHOW     start_show(): the show cue on the running session's
                     clock (and the MadMapper show bank selected first).
                     Only once Run has been pressed: nothing reaches the
                     rig until then (CLAUDE.md), so without a running show
                     the start is refused and reported as SHOW_FAILED.
      SHOW_CONFIRMED the timecode is seen moving after the start.
      SHOW_ENDED     the cue it started has ended on its own (the audio's
                     end), while the scheduler is still in SHOW.
      SHOW_FAILED    the start was refused, or the cue ended before it was
                     ever confirmed.
      CLOSING_DONE   at closing: flame cues zero and lasers blanked by the
                     conductor, pixels black, then reported done. The
                     MadMapper stop is not performed and the journal says so.

    poll() does all the reporting; a thread calls it every POLL_S. Reports
    are made outside the conductor and with no lock of this module held."""

    POLL_S = 0.1

    def __init__(self, svc, control, show, conductor, cfg, journal=None,
                 madmapper=None):
        self.svc = svc
        self.control = control
        self.show = show
        self.conductor = conductor
        self.cfg = cfg
        self.mm = madmapper
        self._journal = journal
        self._lock = threading.Lock()
        self._cue = None    # {"show", "clock", "played", "confirmed",
                            #  "failed"}
        self._closing_reported = False
        self._stop = threading.Event()
        self._thread = None

    def _note(self, text, **fields):
        if self._journal is not None:
            try:
                self._journal(text, **fields)
            except Exception:
                pass

    def start_show(self, n):
        """Called by the scheduler inside its own step, before the conductor
        is told a show started. Returns a conductor Result at once."""
        clk = self.show._clock()
        s = self.show._session()
        if s is None or clk is None or self.conductor.latched:
            why = ("Run has not been pressed, so nothing may reach the rig"
                   if s is None else
                   "the running show file's clock is not \"audio_master\""
                   if clk is None else
                   "the show is aborted, and the show conductor stays "
                   "latched until someone presses Reset")
            with self._lock:
                self._cue = {"show": n, "failed": why}
            return C.failed(f"Show {n} was not started: {why}.")
        if self.mm is not None:
            try:
                self.mm.select_bank(self.mm.cfg.show_bank, wait=False)
            except Exception as e:
                self._note(f"Selecting the MadMapper show bank failed: {e}",
                           fault=True, action="video", outcome="failed")
        try:
            pick = s.clock_play(self.cfg.show_cue)
        except Exception as e:
            with self._lock:
                self._cue = {"show": n, "failed": str(e)}
            return C.failed(f"Show {n} was not started: {e}")
        with self._lock:
            self._cue = {"show": n, "clock": clk,
                         "played": clk.cues_played, "confirmed": False,
                         "failed": None}
        return C.done(f"Show {n}: {pick.name} started on the show audio.")

    def poll(self):
        sch = self.svc.machine
        state = sch.state if sch is not None else None
        with self._lock:
            cue = self._cue
        if state == "CLOSING":
            if not self._closing_reported:
                self._closing_reported = True
                self._close()
            return
        self._closing_reported = False
        if cue is None:
            return
        n = cue["show"]
        if state not in ("SHOW", "PAUSED"):
            # Aborted, or the scheduler moved on: nothing to report on.
            with self._lock:
                if self._cue is cue:
                    self._cue = None
            return
        if cue.get("failed"):
            if state == "SHOW":
                with self._lock:
                    if self._cue is cue:
                        self._cue = None
                self.svc.report("SHOW_FAILED", cue["failed"], show=n)
            return
        clk = cue["clock"]
        mine = clk.playing and clk.cues_played == cue["played"]
        if not cue["confirmed"] and mine and \
                getattr(clk, "_last_frame", None) not in (None, 0):
            cue["confirmed"] = True
            self.svc.report("SHOW_CONFIRMED",
                            "the show timecode is moving", show=n)
            return
        if not mine and state == "SHOW":
            with self._lock:
                if self._cue is cue:
                    self._cue = None
            how = getattr(clk, "last_ended", "") or "the cue stopped"
            self.svc.report("SHOW_ENDED" if cue["confirmed"]
                            else "SHOW_FAILED", how, show=n)

    def _close(self):
        r = self.conductor.intermission("the scheduler", "")
        if not r.ok:
            self._note(f"Closing: {r.sentence}", fault=True,
                       action="closing", outcome="failed")
        if self.show._session() is not None:
            self.show.pixels_fade_out(1.0)
        self._note("Closing: the MadMapper stop is not performed in this "
                   "build.", action="closing", outcome="not performed")
        self.svc.report("CLOSING_DONE",
                        "flame cues zero, lasers blanked, pixels black")

    def _run(self):
        while not self._stop.wait(self.POLL_S):
            try:
                self.poll()
            except Exception as e:
                self._note(f"The show runner hit an error and carried on: "
                           f"{type(e).__name__}: {e}. That is a bug in "
                           f"ltcplay.", fault=True, action="runner",
                           outcome="error")

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="ltcplay-show-runner")
            self._thread.start()
        return self

    def close(self):
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(2.0)


# ---------------------------------------------------------------- wiring --

class Wiring:
    """What attach() built, for web.serve to keep and close."""

    def __init__(self, conductor, show, devices, runner):
        self.conductor = conductor
        self.show = show
        self.devices = devices
        self.runner = runner

    def close(self):
        if self.runner is not None:
            self.runner.close()
        self.conductor.close()


def attach(svc, control, cfg, madmapper=None, beyond=None, announce=None,
           journal=None, flame_link=None, threaded=True, **conductor_kw):
    """Build the real Conductor and attach it to the scheduler service
    `svc`. `madmapper` is web.serve's (link, watchdog) pair or None,
    `beyond` its Beyond or None. Call before svc.start(), so the first tick
    already reaches the conductor. `threaded` False and `conductor_kw`
    (clock, waiter) are the selftest's: nothing runs on its own then."""
    link = madmapper[0] if madmapper is not None else None
    show = FireIceShow(control, journal=journal, flame_link=flame_link)
    devices = C.ConductorDevices(link, beyond, journal=journal)

    def state():
        m = svc.machine
        return m.state if m is not None else None
    conductor = C.Conductor(
        devices, show, C.laser_gate_for(state),
        hold_gate=svc.hold_for_announcement,
        announcer=announce.play if announce is not None else None,
        journal=journal, threaded=threaded, **conductor_kw)
    svc.conductor = conductor
    runner = None
    if cfg.scheduler_performs:
        runner = ShowRunner(svc, control, show, conductor, cfg,
                            journal=journal, madmapper=link)
        svc.performer = runner
        svc.dry_run = False
        if threaded:
            runner.start()
    if journal is not None:
        journal(f"Fire & Ice show conductor built: {cfg.summary()}.",
                action="fire_ice", outcome="built")
    return Wiring(conductor, show, devices, runner)
