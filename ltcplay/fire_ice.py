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
    flames      ltcplay's flame link (flamelink.FlameLink, PR #34), built
                here when this config names "flamesafe_config": its key,
                port, universe and frame_stale_ms come from flamesafe's own
                config file (FlameLinkConfig.from_flamesafe_config), so the
                two programs cannot disagree. zero, release and the
                Abort's disarm_all go through it. The cue values are the
                show's own flame universe, read from the frame the pixel
                output is rendering (FlameCues), and only when this config
                names the flame controller in xlights_networks.xml AND that
                controller is Inactive there, so the pixel output never
                sends fire values to the flame node itself, around
                flamesafe. Anything else: all zeros. Without
                "flamesafe_config" there is no flame link: zero and release
                reach nothing, and a screen Abort's disarm FAILS LOUDLY.
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
KEYS = frozenset(("scheduler_performs", "auto_start", "show_cue",
                  "madmapper", "beyond", "flamesafe_config",
                  "flame_controller", "notes"))

# "auto_start": the ONE setting that decides whether the scheduler, once it
# performs, starts a scheduled show by itself (an open question for Jeff,
# PR #32 Q5: is "Run pressed" enough to start a show, and light the lasers
# through the conductor, with no other confirmation?).
#   "when_run_pressed"  PR #32's reading, the default: a show that comes due
#                       is started on the show audio once Run has been
#                       pressed on the page.
#   "off"               the scheduler never starts a show by itself: a show
#                       that comes due is refused by the runner and reported
#                       as a failed start (which disarms every flame group);
#                       an operator's Start now still starts one.
# Every start, automatic or Start now, is journaled either way.
AUTO_START = ("when_run_pressed", "off")


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
                 madmapper=None, beyond=None, path=None,
                 auto_start="when_run_pressed", flamesafe_config=None,
                 flame_controller=None):
        self.scheduler_performs = scheduler_performs
        self.auto_start = auto_start
        self.show_cue = show_cue
        self.madmapper = madmapper
        self.beyond = beyond
        self.path = path
        self.flamesafe_config = flamesafe_config
        self.flame_controller = flame_controller

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
        auto = doc.get("auto_start", "when_run_pressed")
        if auto not in AUTO_START:
            raise FireIceConfigError(
                f"{where}: 'auto_start' has to be one of "
                f"{', '.join(repr(a) for a in AUTO_START)}, not {auto!r}.")
        fs = doc.get("flamesafe_config")
        if fs is not None:
            if not isinstance(fs, str) or not fs.strip():
                raise FireIceConfigError(
                    f"{where}: 'flamesafe_config' is the path of flamesafe's "
                    f"own config file, or leave it out for no flame link.")
            fs = fs.strip()
            if not os.path.isabs(fs):
                base = os.path.dirname(os.path.abspath(where)) \
                    if where != CONFIG_FILE else os.getcwd()
                fs = os.path.join(base, fs)
        fc = doc.get("flame_controller")
        if fc is not None:
            if not isinstance(fc, str) or not fc.strip():
                raise FireIceConfigError(
                    f"{where}: 'flame_controller' is the exact name of the "
                    f"flame controller in xlights_networks.xml.")
            if fs is None:
                raise FireIceConfigError(
                    f"{where}: 'flame_controller' needs 'flamesafe_config': "
                    f"flame cues only ever go to flamesafe.")
            fc = fc.strip()
        mm = bey = None
        if "madmapper" in doc:
            from . import madmapper as madmapper_mod
            mm = madmapper_mod.MadMapperConfig.parse(doc["madmapper"], where)
        if "beyond" in doc:
            from . import beyond as beyond_mod
            bey = beyond_mod.BeyondConfig.parse(doc["beyond"], where)
        return cls(performs, cue.strip() if cue else None, mm, bey, where,
                   auto_start=auto, flamesafe_config=fs,
                   flame_controller=fc)

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
        return ("scheduler performs" + (
                    ", starts shows by itself once Run is pressed"
                    if self.auto_start == "when_run_pressed" else
                    ", never starts a show by itself (auto_start off)")
                if self.scheduler_performs else "scheduler dry run") + \
            (f", flame link from {self.flamesafe_config}"
             + (f", flame cues from controller {self.flame_controller!r}"
                if self.flame_controller else ", flame cues all zero")
             if self.flamesafe_config else ", no flame link") + \
            (f", MadMapper {self.madmapper.summary()}" if self.madmapper
             else ", no MadMapper") + \
            (", BEYOND " + self.beyond.summary() if self.beyond
             else ", no BEYOND")


# ----------------------------------------------------------- ShowOutputs --

NO_FLAME_LINK = (
    "ltcplay has no flame link to flamesafe in this build: it sends no flame "
    "cue frames at all, so the conductor's flame cue commands reach nothing.")
DISARM_SENT = (
    "a disarm was sent to every flame group through the flame link. Sent "
    "is not confirmed: flamesafe's status frames go to the Stream Deck "
    "program, and the Stream Deck shows whether each group disarmed.")
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
        """With a flame link: its disarm_all (cues to zero, a zero frame,
        then the disarm, repeated past frame_stale_ms), done when it went
        out. Without one, never a quiet success: the cues go to zero, and
        the answer is a failure carrying NO_DISARM, which the conductor
        writes down as a fault, every time."""
        if self.flame_link is not None:
            self.flames = C.ZERO
            try:
                ok = self.flame_link.disarm_all(reason)
            except Exception as e:
                return C.failed(f"{reason}: the disarm failed: {e}")
            return C.done(f"{reason}: {DISARM_SENT}") if ok is True else \
                C.failed(f"{reason}: the disarm could NOT be sent to "
                         f"flamesafe (it is retried every frame). Disarm "
                         f"with the Stream Deck's Abort or its group keys.")
        z = self.flames_zero()
        tail = "" if z.ok else f" Zeroing the cues also failed: {z.sentence}"
        return C.failed(f"{reason}: {NO_DISARM}{tail}")


# ------------------------------------------------------------ flame cues --

class FlameControllerError(ValueError):
    """The flame controller in xlights_networks.xml cannot be used, in a
    sentence."""


def flame_channels(networks_xml, name):
    """(first absolute channel, count) of the controller called `name` in
    xlights_networks.xml, walked in the same order netmap.load() and
    xLights use, so the channels are the ones the FSEQ renders for it.
    Refused unless that controller is Inactive: an active one is in the
    pixel output's map, and the pixel output would send its fire values
    straight to the flame node, around flamesafe. Count is at most 512."""
    import xml.etree.ElementTree as ET
    root = ET.parse(networks_xml).getroot()
    chan = 1
    for c in root:
        if c.tag != "Controller":
            continue
        a = c.attrib
        nets = [n for n in c if n.tag == "network"]
        span = sum(max(0, int(n.attrib.get("MaxChannels", "0") or 0))
                   for n in nets)
        if a.get("Name", "") == name:
            if a.get("ActiveState", "Active") == "Active":
                raise FlameControllerError(
                    f"The flame controller {name!r} is Active in "
                    f"{networks_xml}, so the pixel output would send its "
                    f"fire values straight to the flame node, around "
                    f"flamesafe. Set it Inactive in xLights (it keeps its "
                    f"channels). Until then every flame cue is zero.")
            if span <= 0:
                raise FlameControllerError(
                    f"The flame controller {name!r} in {networks_xml} has "
                    f"no channels.")
            return chan, min(span, 512)
        chan += span
    raise FlameControllerError(f"There is no controller called {name!r} in "
                               f"{networks_xml}. Every flame cue is zero.")


class FlameCues:
    """flamelink's cue provider: the show's flame universe, read from the
    frame the pixel output is rendering right now (the player's buffer
    holds the whole show's channels, the Inactive flame controller's
    included; nothing sends those). The timecode FlameLink passes is the
    frame the show audio last sent, which is what the player renders.
    None (all zeros) whenever there is no running player, or the show
    file's folder has no usable flame controller. Never raises."""

    def __init__(self, control, name, journal=None):
        self.control = control
        self.name = name
        self._journal = journal
        self._folder = None
        self._span = None
        self._problem = ""

    def _note(self, text, **f):
        if self._journal is not None:
            try:
                self._journal(text, **f)
            except Exception:
                pass

    def _locate(self, session):
        folder = getattr(getattr(session, "tl", None), "show_dir", None)
        if folder == self._folder:
            return self._span
        self._folder, self._span = folder, None
        try:
            if not folder:
                raise FlameControllerError("The running show has no show "
                                           "folder.")
            self._span = flame_channels(
                os.path.join(folder, "xlights_networks.xml"), self.name)
            self._problem = ""
            self._note(f"Flame cues: from controller {self.name!r}, "
                       f"channels {self._span[0]} to "
                       f"{self._span[0] + self._span[1] - 1} of the show.",
                       action="flames", outcome="cues_found")
        except Exception as e:
            text = str(e)
            if text != self._problem:
                self._problem = text
                self._note(f"Flame cues are zero: {text}", fault=True,
                           action="flames", outcome="cues_refused")
        return self._span

    def __call__(self, tc):
        s = getattr(self.control, "session", None)
        if s is None or not getattr(s, "running", False):
            return None
        p = getattr(s, "player", None)
        buf = getattr(p, "_buf", None)
        if buf is None:
            return None
        span = self._locate(s)
        if span is None:
            return None
        start, count = span
        vals = list(bytes(buf[start - 1:start - 1 + count]))
        return vals + [0] * (512 - len(vals))


def flame_link_config(cfg):
    """The FlameLinkConfig read from flamesafe's own config, or None when
    the Fire & Ice config names none. `ltc serve` calls this before it
    binds anything, so a flamesafe config it cannot read stops it in one
    sentence. Raises FireIceConfigError."""
    if not cfg.flamesafe_config:
        return None
    from . import flamelink
    try:
        return flamelink.FlameLinkConfig.from_flamesafe_config(
            cfg.flamesafe_config)
    except flamelink.FlameLinkConfigError as e:
        raise FireIceConfigError(str(e))


def build_flame_link(cfg, control, show, journal=None):
    """The FlameLink for this config, started, or None when the config
    names no flamesafe config. Raises FireIceConfigError for a config file
    that cannot be read, so serve refuses to start rather than run without
    the link it was told to have."""
    if not cfg.flamesafe_config:
        return None
    from . import flamelink
    lcfg = flame_link_config(cfg)
    cues = (FlameCues(control, cfg.flame_controller, journal)
            if cfg.flame_controller else flamelink.zero_cues)
    link = flamelink.FlameLink(
        lcfg, cues=cues, show_state=flamelink.audio_master_state(show._clock),
        journal=journal)
    if journal is not None:
        journal("Flame link: flamesafe's status frames go to the Stream Deck "
                "program, so this program cannot see flamesafe confirm a "
                "disarm, or raise the lock alarm itself; the Stream Deck "
                "shows both.", action="flame_link", outcome="no_status")
    return link


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

    def start_show(self, n, who="the scheduler"):
        """Called on the scheduler's ordered line of conductor requests,
        off its lock, after tonight is saved; the conductor is told a show
        started only once it is confirmed. Returns a conductor Result at
        once. `who` is "the scheduler" for a show that came due, or the
        operator who pressed Start now."""
        clk = self.show._clock()
        s = self.show._session()
        auto = who == "the scheduler"
        if auto and self.cfg.auto_start == "off":
            why = ("auto_start is off in ltcplay_fire_ice.json, so the "
                   "scheduler does not start a show by itself; press Start "
                   "now to start it")
            with self._lock:
                self._cue = {"show": n, "failed": why}
            return C.failed(f"Show {n} was not started: {why}.")
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

    def __init__(self, conductor, show, devices, runner, flame_link=None):
        self.conductor = conductor
        self.show = show
        self.devices = devices
        self.runner = runner
        self.flame_link = flame_link

    def close(self):
        try:
            if self.runner is not None:
                self.runner.close()
            self.conductor.close()
        finally:
            # Last: zero frames until the end. flamesafe disarms every
            # group once it stops hearing it.
            fl = self.flame_link
            if fl is not None and hasattr(fl, "stop"):
                try:
                    fl.zero()
                finally:
                    fl.stop()


def attach(svc, control, cfg, madmapper=None, beyond=None, announce=None,
           journal=None, flame_link=None, threaded=True, **conductor_kw):
    """Build the real Conductor and attach it to the scheduler service
    `svc`. `madmapper` is web.serve's (link, watchdog) pair or None,
    `beyond` its Beyond or None. Call before svc.start(), so the first tick
    already reaches the conductor. `threaded` False and `conductor_kw`
    (clock, waiter) are the selftest's: nothing runs on its own then."""
    link = madmapper[0] if madmapper is not None else None
    built_link = None
    if flame_link is None and cfg.flamesafe_config:
        # Built before the show outputs so they have it from the start; the
        # clock it reads is looked up through the show on every frame.
        holder = {}
        show = FireIceShow(control, journal=journal,
                           flame_link=_LinkSlot(holder))
        built_link = build_flame_link(cfg, control, show, journal)
        holder["link"] = built_link
        show.flame_link = built_link
        if threaded:
            built_link.start()
        else:
            built_link.open()
    else:
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
    return Wiring(conductor, show, devices, runner, built_link)


class _LinkSlot:
    """Stands in for the flame link only while it is being built, so
    FireIceShow does not journal "no flame link" for a link that is about
    to exist."""

    def __init__(self, holder):
        self.holder = holder
