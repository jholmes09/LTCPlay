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
  laser gate    conductor.laser_gate_for(the scheduler's state), AND the
                laser arm (laserarm.py, Jeff 2026-10-22): every laser armed,
                none locked out. The arm lives on the Morning and Shutdown
                page and is handed in later (Wiring.attach_laser_arm);
                until it is, the lasers stay dark. The same check is the
                Blanking's arm_check, asked before every unblank packet.
  hold_gate     schedule_service.Service.hold_for_announcement.
  announcer     announce.AnnounceService.play, when --announce is given.

The config file is ltcplay_fire_ice.json beside the schedule rule file. It
is optional: without it the conductor is built with no lasers or video and
the scheduler stays a dry run. Its one switch that changes what the
scheduler does is "scheduler_performs" (see ShowRunner and BENCH.md):
false, the default, is today's dry run exactly.
"""
import atexit
import json
import os
import threading
import time

from . import benchhooks
from . import conductor as C
from . import showlog as showlog_mod

CONFIG_FILE = "ltcplay_fire_ice.json"
KEYS = frozenset(("scheduler_performs", "auto_start", "show_cue",
                  "madmapper", "beyond", "flamesafe_config",
                  "flame_controller", "notes", "show_name", "venue",
                  "beyond_blank", "beyond_black_hour", "beyond_timecode_ip",
                  "soak_only", "preroll_s", "preroll_video_unconfirmed_ok"))

# "auto_start": the ONE setting that decides whether the scheduler, once it
# performs, starts a scheduled show by itself (an open question for Jeff,
# PR #32 Q5: is "Run pressed" enough to start a show, and light the lasers
# through the conductor, with no other confirmation?).
#   "when_run_pressed"  PR #32's reading, the default: a show that comes due
#                       is started on the show audio once Run has been
#                       pressed on the page.
#   "off"               the scheduler never starts a show by itself: a show
#                       that comes due is refused by the runner and reported
#                       as a failed start that sent nothing (no fade, no
#                       MadMapper or BEYOND command, no disarm); an
#                       operator's Start now still starts one.
# Every start, automatic or Start now, is journaled either way.
AUTO_START = ("when_run_pressed", "off")

# How the lasers are kept dark when the file does not say (Jeff,
# 2026-10-07): by OSC, beyond.py's brightness 0 or 100. "timecode" (BEYOND's
# own Art-Net timecode sent to the black zone) and "both" stay selectable
# with "beyond_blank". beyondtc.DEFAULT_MODE says the same; the selftest
# holds them equal.
BEYOND_BLANK_DEFAULT = "osc"


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
                 flame_controller=None, show_name=None, venue=None,
                 beyond_blank=BEYOND_BLANK_DEFAULT, beyond_black_hour=23,
                 beyond_timecode_ip=None, soak_only=False, preroll_s=None,
                 preroll_video_unconfirmed_ok=False):
        # How the lasers are kept dark (beyondtc.py): "osc" (beyond.py's
        # brightness 0/100, the default since Jeff 2026-10-07), "timecode"
        # (BEYOND's own timecode to the black zone) or "both".
        self.beyond_blank = beyond_blank
        # Written by the soak test (packaging/windows/soak.py) only: any
        # engine the soak did not start itself refuses these settings
        # (soak_refusal), so a soak kit file left behind on a show PC can
        # never run a show unattended (Pico, 2026-10-23).
        self.soak_only = soak_only
        self.beyond_black_hour = beyond_black_hour
        self.beyond_timecode_ip = beyond_timecode_ip
        # The show's own name and where it plays, for the screens' title
        # (/api/brand). Here, not in the shared ltcplay_brand.json, so the
        # GPL build keeps its own name; Jeff Holmes Presents stays global.
        self.show_name = show_name
        self.venue = venue
        self.scheduler_performs = scheduler_performs
        self.auto_start = auto_start
        self.show_cue = show_cue
        self.madmapper = madmapper
        self.beyond = beyond
        self.path = path
        self.flamesafe_config = flamesafe_config
        self.flame_controller = flame_controller
        # The pre-roll (preroll.py, Jeff 2026-10-28): the rig is checked
        # this many seconds before a show's time (3 to 15, default 10). 0
        # (no pre-roll) only in a soak kit's file (soak_only).
        from . import preroll
        self.preroll_s = preroll.DEFAULT_S if preroll_s is None \
            else preroll_s
        # True: a MadMapper with no heartbeat track passes the video check
        # as NOT confirmed instead of failing it. Off unless set.
        self.preroll_video_unconfirmed_ok = preroll_video_unconfirmed_ok

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
        soak_only = doc.get("soak_only", False)
        if soak_only is not True and soak_only is not False:
            raise FireIceConfigError(
                f"{where}: 'soak_only' has to be true or false, not "
                f"{soak_only!r}.")
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
        titles = {}
        for k in ("show_name", "venue"):
            v = doc.get(k)
            if v is not None and (not isinstance(v, str) or not v.strip()):
                raise FireIceConfigError(
                    f"{where}: {k!r} is words for the screens' title, or "
                    f"leave it out.")
            titles[k] = v.strip() if v else None
        blank = doc.get("beyond_blank", BEYOND_BLANK_DEFAULT)
        if blank not in ("timecode", "osc", "both"):
            raise FireIceConfigError(
                f"{where}: 'beyond_blank' is how the lasers are kept dark: "
                f"\"osc\" (brightness, the default), \"timecode\" (the "
                f"black zone) or \"both\", not {blank!r}.")
        hour = doc.get("beyond_black_hour", 23)
        if isinstance(hour, bool) or not isinstance(hour, int) or \
                not 0 <= hour <= 23:
            raise FireIceConfigError(
                f"{where}: 'beyond_black_hour' is the hour of BEYOND's black "
                f"zone, a whole number from 0 to 23, not {hour!r}.")
        tip = doc.get("beyond_timecode_ip")
        if tip is not None:
            import ipaddress
            try:
                ipaddress.IPv4Address(str(tip))
            except ValueError:
                raise FireIceConfigError(
                    f"{where}: 'beyond_timecode_ip' is BEYOND's address for "
                    f"its timecode, like 127.0.0.2, not {tip!r}.")
        from . import preroll
        pre = doc.get("preroll_s", preroll.DEFAULT_S)
        if isinstance(pre, bool) or not isinstance(pre, int) or not (
                preroll.MIN_S <= pre <= preroll.MAX_S or
                (pre == 0 and soak_only)):
            raise FireIceConfigError(
                f"{where}: 'preroll_s' is how many seconds before a show the "
                f"rig is checked, a whole number from {preroll.MIN_S} to "
                f"{preroll.MAX_S} (default {preroll.DEFAULT_S}), not "
                f"{pre!r}.")
        vok = doc.get("preroll_video_unconfirmed_ok", False)
        if vok is not True and vok is not False:
            raise FireIceConfigError(
                f"{where}: 'preroll_video_unconfirmed_ok' has to be true or "
                f"false, not {vok!r}.")
        titles.update(beyond_blank=blank, beyond_black_hour=hour,
                      beyond_timecode_ip=tip, soak_only=soak_only,
                      preroll_s=pre, preroll_video_unconfirmed_ok=vok)
        mm = bey = None
        if "madmapper" in doc:
            from . import madmapper as madmapper_mod
            mm = madmapper_mod.MadMapperConfig.parse(doc["madmapper"], where)
        if "beyond" in doc:
            from . import beyond as beyond_mod
            bey = beyond_mod.BeyondConfig.parse(doc["beyond"], where)
        return cls(performs, cue.strip() if cue else None, mm, bey, where,
                   auto_start=auto, flamesafe_config=fs,
                   flame_controller=fc, **titles)

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


def soak_refusal(cfg, env=None, ppid=None):
    """None, or why `ltc serve` refuses these settings: they are the soak
    kit's ("soak_only": true) and the soak did not start this engine
    (benchhooks.soak_started, its own process id as this engine's parent).
    `env` and `ppid` are the selftest's."""
    if not getattr(cfg, "soak_only", False) or \
            benchhooks.soak_started(env, ppid):
        return None
    return (f"{cfg.path or CONFIG_FILE} was written by the soak test "
            f"(\"soak_only\": true), and only the soak's own engine may use "
            f"it. LTC Player did not start, so nothing reaches the rig. Put "
            f"the show's own ltcplay_fire_ice.json and schedule back beside "
            f"each other, then start LTC Player again.")


# ----------------------------------------------------------- ShowOutputs --

NO_FLAME_LINK = (
    "ltcplay has no flame link to flamesafe in this build: it sends no flame "
    "cue frames at all, so the conductor's flame cue commands reach nothing.")
DISARM_SENT = (
    "a disarm was sent to every flame group through the flame link. Sent "
    "is not confirmed: only flamesafe's status frame can say it took it. "
    "The journal says when flamesafe confirms it, or that it has not after "
    "1 s, and the Stream Deck shows whether each group disarmed.")
NO_DISARM = (
    "A screen-initiated Abort cannot disarm the flame groups from this "
    "engine: it has no flame link to flamesafe (ltcplay_fire_ice.json names "
    "no flamesafe_config, or there is no such file beside the schedule). "
    "Flame cues from ltcplay are zero. The link (flamesafe/CONTRACT.md, "
    "disarm_all) does carry a disarm once it is configured; until then "
    "disarm with the Stream Deck's own Abort or its arm keys, or the "
    "hardware e-stop.")


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

    def clock_nolock(self):
        """The running session's show audio clock, or None, taking no lock
        and hooking nothing: what the flame link's sender reads every frame
        (PR #43 review, item 9: it used to take this object's lock)."""
        s = getattr(self.control, "session", None)
        if s is None or not getattr(s, "running", False):
            return None
        clk = getattr(s, "clock", None)
        if clk is None or getattr(clk, "source", None) != "audio_master":
            return None
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
            # A show start's hand-over still waiting for its first frame
            # (pixels_for_show) ends here: this black stands, and what it
            # gives back later is the show, never the look held at the
            # start (a Hold before the first frame, then Resume).
            cancel = getattr(p, "cancel_show_handoff", None)
            waiting = cancel() if cancel is not None else False
            if not self._pix_ours:
                self._pix_prev = None if waiting else p.override
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
        left = False
        with self._lock:
            if not self._pix_ours:
                return C.done("Pixels: the show conductor had not taken "
                              "them, so they were left as they are.")
            self._pix_ours = False
            if p.override != "blackout":
                # Someone pressed a look on the page since: theirs stands.
                left, look = True, p.override
            else:
                p.override = self._pix_prev
        if left:
            # Written after the lock is let go: no lock is ever held across
            # a journal line (PR #43 review, item 9).
            self._note(f"Pixels left on {look or 'auto'}: the operator "
                       f"changed the look while the show conductor had "
                       f"them black.", action="pixels", outcome="left")
            return C.done("Pixels left on the operator's look.")
        if s.log:
            s.log.event("override", f"show conductor set output to "
                                    f"{p.override or 'auto'}")
        return C.done("Pixels back.")

    # -- every show start (Jeff's report, 2026-10-29) ------------------------
    LOOK_NAMES = {"blackout": "Blackout", "preshow": "Preshow"}

    def pixels_for_show(self, what, programming=False):
        """THE one place a show start clears the pixel output: Start now, a
        scheduled start (ShowRunner.start_show) and the Programming
        screen's Play (playhead.ShowAudioPlayhead). Called before the show
        cue is played, whatever ended the show before it. Writes the same
        journal line every time and returns it ("" with nothing running).

        Every start clears:
          the show conductor's black (closing, a stopped or failed show, an
            Abort since Reset, a Hold), and what it had saved under it;
          a stale record of that black with nothing over the output.
        A scheduled start and Start now also clear a look the page put up
        before the start (Blackout, Preshow; PR #43 fix round 2, E: the
        page is locked during the show, so it could not be undone). On the
        Programming screen, which is not locked, the page's own look stays.

        Nothing goes dark for it: the look that is up (the idle look, a
        Preshow, a black) stays until the first show frame and the show
        takes over on that frame (Player.hold_until_show). A Hold, an
        Abort or a look pressed after the start is not touched and holds."""
        s, p = self._player()
        if p is None:
            return ""
        with self._lock:
            ours, under = self._pix_ours, self._pix_prev
            self._pix_ours, self._pix_prev = False, None
            look = p.override
            keep = None
            if programming:
                keep = (under if look == "blackout" else look) if ours \
                    else look
            if keep is not None:
                # A hand-over still waiting from an earlier start never
                # lifts this look on later timecode (review of PR #171).
                cancel = getattr(p, "cancel_show_handoff", None)
                if cancel is not None:
                    cancel()
                if p.override != keep:
                    p.override = keep
                held = None
            else:
                hold = getattr(p, "hold_until_show", None)
                if hold is not None:
                    held = hold()
                else:
                    if look is not None:
                        p.override = None
                    held = None
        name = self.LOOK_NAMES.get(look, look)
        if ours and look == "blackout":
            cleared = "the show conductor's black from between shows"
        elif ours and look is None:
            cleared = ("a stale record of the show conductor's black, with "
                       "the idle look up")
        elif ours:
            cleared = f"the page's {name} look over the show conductor's black"
        elif look is not None and keep is None:
            cleared = f"the page's {name} look"
        else:
            cleared = "nothing was over it"
        if keep is not None:
            rest = (f" The page's {self.LOOK_NAMES.get(keep, keep)} look "
                    f"stays: on the Programming screen it is the operator's; "
                    f"press Stop on the Show file card (1.0, on the show "
                    f"machine), then Run, to give the pixels to the "
                    f"show.")
        else:
            first = {None: "The idle look", "blackout": "The black",
                     "preshow": "The Preshow look"}.get(held, "The look")
            rest = (f" {first} stays up until the first show frame goes "
                    f"out, and the show takes over on that frame.")
        text = (f"{what}: the pixel output is cleared for the show "
                f"({cleared}).{rest} A Hold, an Abort or a Stop after this "
                f"start still holds.")
        self._note(text, action="pixels", outcome="cleared_for_show")
        return text

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
                         f"with Disarm all flame groups on the Rack screen, "
                         f"the Stream Deck's group keys, or the hardware "
                         f"e-stop.")
        z = self.flames_zero()
        tail = "" if z.ok else f" Zeroing the cues also failed: {z.sentence}"
        return C.failed(f"{reason}: {NO_DISARM}{tail}")


# ------------------------------------------------------------ flame cues --

class FlameControllerError(ValueError):
    """The flame controller in xlights_networks.xml cannot be used, in a
    sentence."""


class FlameControllerActive(FlameControllerError):
    """The flame controller is Active (or has no ActiveState) in
    xlights_networks.xml: the pixel output would send its channels."""


def flame_channels(networks_xml, name):
    """(first absolute channel, count) of the controller called `name` in
    xlights_networks.xml, walked in the same order netmap.load() and
    xLights use, so the channels are the ones the FSEQ renders for it.
    Refused unless that controller is Inactive: an active one is in the
    pixel output's map, and the pixel output would send its fire values
    straight to the flame node, around flamesafe. Count is at most 512."""
    found, near = [], []
    for c, first, span in _controllers(networks_xml):
        cname = c.attrib.get("Name", "")
        if cname == name:
            found.append((c, first, span))
        elif cname.strip().lower() == str(name).strip().lower():
            near.append(cname)
    if len(found) > 1:
        raise FlameControllerError(
            f"{len(found)} controllers are called {name!r} in "
            f"{networks_xml}, so which one is the flame controller cannot be "
            f"told. Give it a name of its own in xLights and the same name "
            f"as \"flame_controller\" in ltcplay_fire_ice.json.")
    if not found:
        hint = (f" There is one called {near[0]!r}: the name must match "
                f"exactly, capitals and spaces included." if near else "")
        raise FlameControllerError(
            f"There is no controller called {name!r} (exactly that spelling) "
            f"in {networks_xml}, so which channels are the flames cannot be "
            f"told.{hint} Every flame cue is zero.")
    c, first, span = found[0]
    if c.attrib.get("ActiveState", "Active") == "Active":
        raise FlameControllerActive(
            f"The flame controller {name!r} is Active in "
            f"{networks_xml}, so the pixel output would send its "
            f"fire values straight to the flame node, around "
            f"flamesafe. Set it Inactive in xLights (it keeps its "
            f"channels). Until then every flame cue is zero.")
    if span <= 0:
        raise FlameControllerError(
            f"The flame controller {name!r} in {networks_xml} has "
            f"no channels.")
    return first, min(span, 512)


def _controllers(networks_xml):
    """(element, first absolute channel, span) for every controller in
    xlights_networks.xml, in netmap.load()'s and xLights' order."""
    import xml.etree.ElementTree as ET
    root = ET.parse(networks_xml).getroot()
    chan = 1
    out = []
    for c in root:
        if c.tag != "Controller":
            continue
        nets = [n for n in c if n.tag == "network"]
        span = sum(max(0, int(n.attrib.get("MaxChannels", "0") or 0))
                   for n in nets)
        out.append((c, chan, span))
        chan += span
    return out


def map_total_channels(networks_xml):
    """Every channel xlights_networks.xml lays out, as netmap.load() counts
    them: what a render made for this map has."""
    return sum(span for _c, _f, span in _controllers(networks_xml))


def flame_destinations(networks_xml, name):
    """(address, universe, protocol) of every network row of the flame
    controller `name` (ComPort, else the controller's IP; BaudRate is the
    universe, as netmap.load() reads them)."""
    from . import netmap
    out = set()
    for c, _f, _s in _controllers(networks_xml):
        if c.attrib.get("Name", "") != name:
            continue
        for n in c:
            if n.tag != "network":
                continue
            na = n.attrib
            proto = netmap.UDP_PROTOCOLS.get(
                (na.get("NetworkType") or "").lower())
            dest = na.get("ComPort", "") or c.attrib.get("IP", "")
            try:
                univ = int(na.get("BaudRate", ""))
            except ValueError:
                continue
            if proto and dest:
                out.add((dest, univ, proto))
    return out


def flamesafe_destination(path):
    """(address, universe, "e131") flamesafe sends the flame node, read from
    its own config, or None when it cannot be read."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
        return (str(doc["destination"]["ip"]), int(doc["universe"]), "e131")
    except Exception:
        return None


def _show_networks(show_file):
    """xlights_networks.xml of the show this show file plays, or None."""
    from . import timeline as timeline_mod
    try:
        tl = timeline_mod.Timeline.load(show_file)
    except Exception:
        return None
    return os.path.join(getattr(tl, "show_dir", "") or "",
                        "xlights_networks.xml")


def refuse_active_flame_controller(show_file, name, fs_dest=None):
    """Raise FlameControllerError when the show this file plays cannot be
    run safely with flames, from its xlights_networks.xml:
      - the flame controller `name` is Active (or has no ActiveState,
        which xLights reads as Active): the pixel output would send its
        fire values straight to the flame node, around flamesafe (PR #43
        review, finding 3);
      - no controller has exactly that name, or two do, or it has no
        channels (fix round 2, B: a rename or a change of case in xLights
        used to slip past, with the controller Active);
      - any Active controller sends to the flame controller's own address
        and universe, or to flamesafe's destination (fix round 2, B: a
        second controller at the flame node's address did).
    Returns the (address, universe, protocol) set the pixel output must
    never send to. A map that does not parse is left to the session, which
    refuses it itself."""
    path = _show_networks(show_file)
    if not path or not os.path.exists(path):
        return set()
    try:
        _controllers(path)
    except Exception:
        return set()
    flame_channels(path, name)
    blocked = set(flame_destinations(path, name))
    if fs_dest:
        blocked.add(tuple(fs_dest))
    from . import netmap
    try:
        nm = netmap.load(path)
    except Exception:
        return blocked
    for u in nm.universes:
        if (u.ip, u.universe, u.protocol) in blocked:
            raise FlameControllerActive(
                f"Controller {u.controller!r} is Active in {path} and sends "
                f"to {u.ip}, {u.protocol} universe {u.universe}: the flame "
                f"node's own address and universe, so the pixel output would "
                f"send its values straight to the flame node, around "
                f"flamesafe. Set it Inactive, or give it another address, "
                f"in xLights.")
    return blocked


def refuse_alert_pixels_on_flame_channels(show_file, name, pixels):
    """The blue alert pulse (player.py, alerts.py) paints `pixels`
    ((start0, count, order), remote.check_alert_pixels) over the pixel
    frame. Those channels must not be the flame controller's: a pulse on a
    flame channel would be a fire value sent around flamesafe. Raises
    FlameControllerError naming both ranges when they overlap; quiet when
    there is nothing to check (no pixels, no map, no such controller: the
    other checks say so)."""
    if not pixels:
        return
    path = _show_networks(show_file)
    if not path or not os.path.exists(path):
        return
    try:
        first, span = flame_channels(path, name)
    except Exception:
        return
    start0, count, _order = pixels
    lo, hi = start0 + 1, start0 + 3 * count
    f_lo, f_hi = first, first + span - 1
    if lo <= f_hi and f_lo <= hi:
        raise FlameControllerError(
            f"The alert pulse pixels (channels {lo} to {hi}, alert_pixels "
            f"in ltcplay_remote.json) overlap the flame controller "
            f"{name!r} (channels {f_lo} to {f_hi}). Pick a floor range "
            f"that is not the flame controller's.")


def check_flame_groups(folder, cfg):
    """At `ltc serve` startup: the flame groups in flamesafe's config, the
    one place they are decided (Jeff: a settings change at tech, never a
    code change). The deck's and the pages' labels are read from the same
    file. Raises FireIceConfigError, in a sentence, for an edit that would
    not work on the night: a group with no name or one name twice, one
    channel (safety or fire) in two groups, or a channel the flame
    controller in a show's layout does not have (a head that does not
    exist)."""
    if not cfg.flamesafe_config:
        return
    path = cfg.flamesafe_config
    try:
        with open(path, encoding="utf-8-sig") as fh:
            groups = json.load(fh).get("groups")
    except (OSError, ValueError, AttributeError) as e:
        raise FireIceConfigError(f"{path}: could not be read: {e}")
    if not isinstance(groups, list) or not groups:
        raise FireIceConfigError(f"{path}: it lists no flame groups.")
    # Any number of groups: the Stream Deck keys three (ltcplay_deck.json
    # says which) and the rest are armed from a screen (Jeff, 2026-10-26:
    # a layout that does not fit is said, never refused).
    owner, names = {}, set()
    for i, g in enumerate(groups):
        name = g.get("name") if isinstance(g, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise FireIceConfigError(f"{path}: group {i + 1} has no name.")
        if name in names:
            raise FireIceConfigError(
                f"{path}: two groups are both called {name!r}.")
        names.add(name)
        # A group's safety is one channel, or a list of them (flamesafe
        # multi-safety, 2026-10-26: one arm group, one safety channel per
        # fixture). Every one of them is checked like a fire channel.
        safety = g.get("safety")
        if isinstance(safety, list):
            if not safety:
                raise FireIceConfigError(
                    f"{path}: {name} has an empty list of safety channels.")
            slots = list(safety)
        else:
            slots = [safety]
        slots += list(g.get("fire") or [])
        for s in slots:
            if not isinstance(s, int) or isinstance(s, bool):
                raise FireIceConfigError(
                    f"{path}: {name} lists {s!r}, which is not a channel "
                    f"number.")
            if s in owner and owner[s] != name:
                raise FireIceConfigError(
                    f"{path}: channel {s} is in both {owner[s]} and {name}. "
                    f"A head belongs to one group only.")
            owner[s] = name
    if not cfg.flame_controller or not folder or not os.path.isdir(folder):
        return
    layouts = [os.path.join(folder, "xlights_networks.xml")]
    for n in sorted(os.listdir(folder)):
        if n.lower().endswith(".json"):
            try:
                layouts.append(_show_networks(os.path.join(folder, n)))
            except Exception:
                pass
    seen = set()
    for xml in layouts:
        if not xml or xml in seen or not os.path.isfile(xml):
            continue
        seen.add(xml)
        try:
            _first, count = flame_channels(xml, cfg.flame_controller)
        except FlameControllerError:
            continue     # check_flame_controllers says why, in its words
        for s, name in sorted(owner.items()):
            if not 1 <= s <= count:
                raise FireIceConfigError(
                    f"{path}: {name} lists channel {s}, but the flame "
                    f"controller {cfg.flame_controller!r} has channels 1 to "
                    f"{count} in {xml}. There is no such head.")


def check_flame_controllers(folder, cfg, alert_pixels=None):
    """At `ltc serve` startup: every show file in the folder, checked with
    refuse_active_flame_controller, and with the alert pulse's pixel range
    (`alert_pixels`, remote.check_alert_pixels) against the flame
    controller's channels: the pulse runs before a show too, so the
    overlap is refused at startup, not only at Run (review of PR #59).
    Raises FireIceConfigError naming the first show file that would put
    fire values on the pixel output."""
    if not cfg.flame_controller or not folder or not os.path.isdir(folder):
        return
    for n in sorted(os.listdir(folder)):
        if not n.lower().endswith(".json"):
            continue
        p = os.path.join(folder, n)
        try:
            with open(p, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict) or "cues" not in doc:
            continue
        try:
            refuse_active_flame_controller(
                p, cfg.flame_controller,
                flamesafe_destination(cfg.flamesafe_config)
                if cfg.flamesafe_config else None)
            refuse_alert_pixels_on_flame_channels(p, cfg.flame_controller,
                                                  alert_pixels)
        except FlameControllerError as e:
            raise FireIceConfigError(f"{n}: {e}")


def beyond_gate_address(cfg):
    """The address BEYOND's timecode gate starts with, as build_blanking
    gives it: beyond_timecode_ip, else the BEYOND block's host, else None
    (then taken from the show file at session open)."""
    return (getattr(cfg, "beyond_timecode_ip", None) or
            getattr(getattr(cfg, "beyond", None), "host", None) or None)


def beyond_addresses(cfg, beyond=None):
    """Every address BEYOND may be reached at: beyond_timecode_ip, the
    BEYOND block's host, and the host of the BEYOND link `beyond` (its OSC
    host, which can also be where it hears timecode). A finished show's
    held timecode frame never goes to any of them
    (clock.TimecodeOut.held_dests)."""
    out = set()
    for v in (getattr(cfg, "beyond_timecode_ip", None),
              getattr(getattr(cfg, "beyond", None), "host", None),
              getattr(getattr(beyond, "cfg", None), "host", None)):
        if isinstance(v, str) and v.strip():
            out.add(v.strip())
    return out


def blanks_by_timecode(cfg):
    return getattr(cfg, "beyond_blank", BEYOND_BLANK_DEFAULT) in (
        "timecode", "both")


def lasers_configured(cfg):
    """True when this config names a BEYOND at all (its OSC block or its
    timecode address)."""
    return bool(getattr(cfg, "beyond", None) is not None or
                getattr(cfg, "beyond_timecode_ip", None))


def beyond_timecode_route(show_file, gate_ip, tl=None, timecode_ip=None,
                          lasers=True):
    """(name, address) of the ONE Art-Net timecode destination in this show
    file that BEYOND's timecode gate takes over (review of PR #43, P0-2),
    or None when there is nothing to route (re-review P2-a): a show file
    that sends no Art-Net timecode at all (a pixel-only or test show: no
    timecode reaches BEYOND, so none can light it), or, with no BEYOND
    configured (`lasers` False), one that names no BEYOND destination.
    The gate keeps the lasers dark only by holding back the show's timecode
    for BEYOND and sending the black zone instead, and it can only hold
    back what it is handed: a destination named BEYOND, or at the gate's
    address. Raises FireIceConfigError, in a sentence, for a show file
    whose timecode would reach BEYOND around the gate or not at all:
    broadcast timecode, no Art-Net timecode, no destination the gate takes,
    or more than one."""
    from . import beyondtc
    where = os.path.basename(show_file)
    if tl is None:
        from . import timeline as timeline_mod
        tl = timeline_mod.Timeline.load(show_file)
    art = getattr(getattr(tl, "clock", None), "artnet", None)
    if art is None:
        return None
    if art.broadcast:
        raise FireIceConfigError(
            f"{where}: the show's timecode is broadcast "
            f"({art.broadcast}), so it reaches BEYOND directly and the "
            f"black zone cannot hold it back: the lasers would follow the "
            f"show while they must be dark. Name each receiver under "
            f"'clock.artnet.nodes' instead, BEYOND among them.")
    hits = [(name, ip) for name, ip in art.dests
            if str(name).strip().lower() == beyondtc.LABEL or
            (gate_ip and ip == gate_ip)]
    if len(hits) == 1:
        if timecode_ip and hits[0][1] != timecode_ip:
            # Re-review P2-h: the gate sends the black zone to
            # beyond_timecode_ip, so a BEYOND the show file puts anywhere
            # else would get the show's timecode from nobody and the black
            # zone from an address it does not listen on.
            raise FireIceConfigError(
                f"{where}: the show file sends BEYOND's timecode to "
                f"{hits[0][1]}, but beyond_timecode_ip in "
                f"ltcplay_fire_ice.json is {timecode_ip}. They must be the "
                f"same address: change one of them.")
        return hits[0]
    if not hits and not lasers:
        return None
    if not hits:
        raise FireIceConfigError(
            f"{where}: no Art-Net timecode destination is named BEYOND"
            + (f" or at BEYOND's timecode address {gate_ip}" if gate_ip
               else "") + f" ({art.summary()}), so the black zone has no "
            f"stream to replace and BEYOND's lasers would not be kept "
            f"dark. Name BEYOND under 'clock.artnet.nodes'.")
    raise FireIceConfigError(
        f"{where}: {len(hits)} Art-Net timecode destinations would go to "
        f"BEYOND's timecode gate ({', '.join(f'{n} {i}' for n, i in hits)}),"
        f" so which one is BEYOND cannot be told. Keep exactly one, named "
        f"BEYOND.")


def check_beyond_timecode_routes(folder, cfg):
    """At `ltc serve` startup, with beyond_blank "timecode" or "both": every
    show file in the folder must send BEYOND's timecode through the gate
    (beyond_timecode_route). Raises FireIceConfigError naming the first
    that does not. Returns a sentence for each show file passed over
    because it has nothing to route (re-review P2-a)."""
    skipped = []
    if not blanks_by_timecode(cfg) or not folder or not os.path.isdir(folder):
        return skipped
    gate_ip = beyond_gate_address(cfg)
    from . import timeline as timeline_mod
    for n in sorted(os.listdir(folder)):
        if not n.lower().endswith(".json"):
            continue
        p = os.path.join(folder, n)
        try:
            with open(p, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict) or "cues" not in doc:
            continue
        try:
            tl = timeline_mod.Timeline.load(p)
        except Exception:
            continue      # the session refuses it itself, in its words
        if beyond_timecode_route(
                p, gate_ip, tl=tl,
                timecode_ip=getattr(cfg, "beyond_timecode_ip", None),
                lasers=lasers_configured(cfg)) is None:
            skipped.append(no_route_sentence(n, cfg))
    return skipped


def no_route_sentence(name, cfg):
    return (f"{name}: no BEYOND timecode to keep dark ("
            + ("it sends no Art-Net timecode"
               if lasers_configured(cfg) else
               "no BEYOND is configured and it names none")
            + "), so the black zone is not used for it.")


# -- renders older than the layout (soak 2026-10-21) -------------------------
# FlameCues refuses every flame cue of a render older than the show's
# xlights_networks.xml (review of PR #43, P0-6). That refusal is right and
# stays exactly as it is; what was wrong is that it was said only in the
# journal, so a whole soak ran with flame cues at zero and nothing on a
# screen said so. These say it at show load, on the rack screen and the
# Stream Deck (CUES FAULT), and in the Morning checks.

def renders_older_than_layout(nm_path, paths):
    """The renders among `paths` whose file is older than `nm_path`
    (xlights_networks.xml), in order, each once. Stat only: no file is
    read. [] when the layout cannot be looked at; a render that is not
    there is not this check's business (Run refuses a missing render)."""
    try:
        laid = os.stat(nm_path).st_mtime_ns
    except (OSError, TypeError, ValueError):
        return []
    out, seen = [], set()
    for p in paths:
        if not p or p in seen:
            continue
        seen.add(p)
        try:
            made = os.stat(p).st_mtime_ns
        except (OSError, TypeError, ValueError):
            continue
        if made < laid:
            out.append(p)
    return out


def render_age_sentence(nm_path, stale, show=""):
    """The CUES FAULT for `stale` (renders_older_than_layout), naming the
    files and what to do; "" when there are none."""
    if not stale:
        return ""
    names = ", ".join(os.path.basename(str(p)) for p in stale[:6])
    if len(stale) > 6:
        names += f" and {len(stale) - 6} more"
    of = f" in {show}" if show else ""
    return (f"CUES FAULT{of}: flame cues will be zero. {len(stale)} "
            f"render(s) are older than {nm_path}: {names}. The layout was "
            f"saved after they were rendered, so which channels are the "
            f"flames cannot be trusted and every flame cue in them is "
            f"refused. Render them again in xLights for this layout. If the "
            f"layout change moved no channel (the same flame controller "
            f"and every channel count the same), saving or touching each "
            f"render so it is newer than xlights_networks.xml clears this "
            f"too.")


def show_file_render_fault(show_file):
    """render_age_sentence for one show file (a .json), from the show file
    and file dates only; "" when it cannot be read (Run says why)."""
    from . import timeline as timeline_mod
    try:
        tl = timeline_mod.Timeline.load(show_file)
    except Exception:
        return ""
    nm = os.path.join(getattr(tl, "show_dir", "") or "",
                      "xlights_networks.xml")
    paths = [getattr(c, "path", None) for c in (tl.cues or ())]
    return render_age_sentence(nm, renders_older_than_layout(nm, paths),
                               os.path.basename(show_file))


def show_folder_render_faults(folder):
    """show_file_render_fault for every show file in `folder`: the Morning
    checks' list, one sentence per show file with a stale render."""
    out = []
    try:
        names = sorted(os.listdir(folder)) if folder else []
    except OSError:
        return out
    for n in names:
        if not n.lower().endswith(".json"):
            continue
        p = os.path.join(folder, n)
        try:
            with open(p, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict) or "cues" not in doc:
            continue
        text = show_file_render_fault(p)
        if text:
            out.append(text)
    return out


# How far past a render's last frame the timecode may be before the zero
# flame output there is also called a FAULT, in frames of the render's own
# step time (a render a few ms shorter than its audio is normal). Only the
# fault depends on it: past the end the output is zero either way.
RENDER_END_TOLERANCE_FRAMES = 2

# A show playing with the pixel output on a look (Jeff's report,
# 2026-10-29: the closing black stayed over two whole shows and only the
# journal said so): once it has lasted this long it is a CUES FAULT on the
# rack screen and the Stream Deck, not a quiet zero. Display only. Never a
# Hold's or an Abort's own fade (the music is pausing or stopping then),
# nor the look a start holds until the first show frame.
LOOK_IN_SHOW_FAULT_S = 1.5
# Rack screen words: short as a whole (1.0 shows it as one message, 2.0
# in a sheet behind "Flame cues are off"), no sentence over eight words.
# The Fire & Ice screens have no Black or Preshow button (those are the
# GPL page's). In 1.0 what clears a look in Programming is Stop on the
# "Show file, on this machine" card (it ends Run and sends black), then
# Run (a new player, no look); that card is on the show machine only.
# 2.0 has no Show file card: it sends you to 1.0 ("Open in 1.0").
LOOK_IN_SHOW = ("CUES FAULT: pixels show {look}, not the show. "
                "Programming: Show file card, Stop, then Run. "
                "On 2.0, Open in 1.0 first. "
                "Scheduled show: tell Jeff.")
# The look in plain words.
LOOK_BUTTONS = {"blackout": "black", "preshow": "preshow"}

RENDER_OLDER_THAN_LAYOUT = (
    "the show's render is older than xlights_networks.xml: the layout was "
    "changed after the show was rendered, so which channels are the flames "
    "cannot be trusted. Render the show again for this layout")


def _tail_by_design(clk, label, f, rel):
    """True when timecode `rel` s into cue `label` is past the end of its
    render `f` only because the show clock deliberately runs the cue
    longer than its lights file (clock.AudioMaster.show_lengths: the
    configured show length), and not past that end. Never raises.

    Only 'clock.zones.show_len_s' makes a tail deliberate: it must be set
    and the show must run longer than BOTH its audio and its render. A
    render that is simply shorter than its audio (stale, or cut short) is
    still the FAULT, the only warning there is (review of #157)."""
    try:
        zones = getattr(getattr(clk, "cfg", None), "zones", None)
        if not getattr(zones, "show_len_s", None):
            return False
        got = clk.show_lengths(label)
        if not got:
            return False
        render_s = f.frame_count * f.step_time_ms / 1000.0
        show_s = float(got["show_s"])
        audio_s = float(got["audio_s"])
        return show_s > render_s + 1e-6 and show_s > audio_s + 1e-6 and \
            rel <= show_s + 1.0 / 30.0
    except Exception:                            # noqa: BLE001
        return False


# Background mode's cut ahead (2026-10-29): each cue's first CUT_START_S
# before its render is published (the reading thread), and the playing cue
# CUT_AHEAD_S ahead of its show timecode on a thread of its own that never
# touches the disk (review of #174: a stalled stat on the reading thread
# used to stop the cutting too, and the playing cue ran out of cut frames
# after 3 s).
CUT_START_S = 0.5
CUT_AHEAD_S = 15.0
# An uncompressed render (v1, or v2 stored whole) is cut in pieces of this
# many frames, sliced straight out of the bytes in memory: never one copy
# of the whole render (review of #174: 230 MB renders were copied whole
# at Run, holding every other thread for half a second).
CUT_PIECE_FRAMES = 200
# A frame whose block is not cut yet is zero, and a CUES FAULT only once it
# has stayed so this long (review of #174: every far move blinked a CUES
# FAULT for 25 to 75 ms). The flame link's own seek guard holds every flame
# at zero for 0.5 s after any move anyway.
CUT_FAULT_AFTER_S = 1.0
# A block whose cut failed for a reason that is not the render's own bytes
# (memory, the system) is tried again after these, CUT_RETRIES times in all;
# failing once more it is a fault for good, named (review of #177: a
# missing zstandard or a bug was retried forever and never named).
CUT_RETRY_S = (0.5, 1.0, 2.0)
CUT_RETRIES = 3
# Named and kept as a fault, such a block is still tried again this often,
# so a passing trouble at one Run never faults it for every later show while
# serve runs (round 2 review of #177). A data error is never tried again.
CUT_RETRY_SLOW_S = 30.0
CUT_SHORT_TEXT = "the render's channel data ends before its last frames"


def _cut_data_error(e):
    """True for an error in the render's own bytes (it will never cut):
    a truncated or corrupt block, a bad table. Anything else may pass,
    a zstd error that is out of memory too (round 3 review of #177)."""
    import zlib
    from .fseq import FSEQError
    kinds = [FSEQError, zlib.error, ValueError, IndexError, EOFError]
    try:
        import zstandard
        if isinstance(e, zstandard.ZstdError):
            text = str(e).lower()
            return not ("memory" in text or "allocat" in text)
    except Exception:                            # noqa: BLE001
        pass
    return isinstance(e, tuple(kinds))


def _bare(e):
    """`e` with no traceback or chained errors, to be kept: a kept error
    never pins the frames (and the decompressed block) it was raised in
    (round 3 review of #177)."""
    try:
        e.__traceback__ = None
        e.__context__ = None
        e.__cause__ = None
    except Exception:                            # noqa: BLE001
        pass
    return e


# How often the cutting thread looks at the play position.
CUT_EVERY_S = 0.05
CUES_LOADING = ("the show's flame cues are still being cut from its render "
                "(off the flame link's sender), so they are zero here; "
                "they follow within a second")
CUES_CUTTER_DEAD = ("the thread that cuts the show's flame cues from its "
                    "render has stopped and could not be started again, so "
                    "every flame cue is zero; restart LTC Player")
# The cutting thread has stopped and waits to be started again (its back-off,
# review of #177): no promise of the cues within a second.
CUES_CUTTER_WAITING = ("the thread that cuts the show's flame cues from its "
                       "render has stopped and is started again shortly, so "
                       "the flame cues not cut yet are zero until it runs")
CUES_READING_LAYOUT = ("the flame controller's channels are still being "
                       "read (off the flame link's sender)")
CUES_READING_RENDER = ("the show's render is still being read into memory "
                       "(off the flame link's sender)")
# Faults that only say the cues are on their way: a background thread that
# keeps stopping outranks them on the screens (review of #177).
CUES_WAITING = (CUES_LOADING, CUES_CUTTER_WAITING, CUES_READING_LAYOUT,
                CUES_READING_RENDER)


class _FlameCut:
    """One render's flame channels for one span, cut block by block off
    the sender (FlameCues background mode), through an FSEQ of its own over
    the render's bytes in memory (never the published one, which another
    thread may be reading). The sender only reads `row`: dict lookups and a
    slice, no lock. The cutting threads share `lock`. A block that cannot
    be cut keeps its error, which `row` raises: for good when it is the
    render's own bytes (a data error), else tried again (CUT_RETRY_S, then
    every CUT_RETRY_SLOW_S). A block cut only part of the way (its later
    frames missing) keeps the rows it has and the error from the first
    missing frame on."""

    def __init__(self, path, f, spans, span, data):
        import io
        import threading as _th
        from .fseq import FSEQ, NONE
        self.f = f
        self.span = tuple(span)
        self.count = int(span[1])
        self.spans = spans
        g = FSEQ(path, fileobj=io.BytesIO(data))
        if g.frame_count != f.frame_count or \
                g.channel_count != f.channel_count:
            g.close()
            raise ValueError("the render changed while it was read")
        self.g = g
        self.whole = g.compression == NONE and len(g._blocks) == 1
        if self.whole:
            # Uncompressed: pieces sliced straight from the bytes, never past
            # the one block's own end (review of #177: a table whose block
            # is shorter than the data, or than the frame count, used to be
            # cut from whatever bytes followed it). Piece b is frames
            # b * CUT_PIECE_FRAMES on, worked out, never listed (a garbage
            # frame count must not allocate a list of its pieces).
            self.mv = memoryview(data)
            _first, self.boff, self.blen = g._blocks[0]
            self.firsts = None
            self.pieces = max(1, -(-g.frame_count // CUT_PIECE_FRAMES))
        else:
            self.mv = None
            self.firsts = [b[0] for b in g._blocks]
        self.lock = _th.Lock()
        self.retry = {}          # block index -> (tries, not before, error)
        self.blocks = {}         # block index -> its frames' rows, joined
        self.errors = {}         # block index -> the error cutting it
        self.short = {}          # block index -> (first missing frame, error)
        self.slow = {}           # block index -> when its kept error is retried
        self.rebuild_at = None   # a cut that never fails as a whole

    @classmethod
    def failed(cls, f, span, error):
        """A render whose cut could not be set up: every row raises `error`
        (a CUES FAULT). A data error is for good for this FSEQ; anything
        else (memory, the system) is set up again from CUT_RETRY_SLOW_S on
        (rebuild_at; round 3 review of #177: it was kept for every later
        show)."""
        import threading as _th
        c = cls.__new__(cls)
        c.f, c.span, c.count, c.spans = f, tuple(span), int(span[1]), []
        c.g, c.whole, c.mv = None, False, None
        c.firsts = [0]
        c.lock = _th.Lock()
        c.retry, c.slow = {}, {}
        c.blocks, c.errors, c.short = {}, {0: _bare(error)}, {}
        c.rebuild_at = None if _cut_data_error(error) else \
            c.clock() + CUT_RETRY_SLOW_S
        return c

    def stale(self):
        """True when this failed set-up is due to be made again."""
        return self.rebuild_at is not None and \
            self.clock() >= self.rebuild_at

    def frames(self, seconds):
        if self.g is None:
            return 0
        return int(seconds * 1000.0 // self.g.step_time_ms) + 1

    def _block(self, idx):
        if self.firsts is None:
            return max(0, min(idx // CUT_PIECE_FRAMES, self.pieces - 1))
        import bisect
        return max(0, bisect.bisect_right(self.firsts, idx) - 1)

    def _first(self, b):
        """Block (or piece) b's first frame."""
        return b * CUT_PIECE_FRAMES if self.firsts is None else self.firsts[b]

    def _end(self, b):
        """The frame after block (or piece) b's last."""
        if self.firsts is None:
            return min((b + 1) * CUT_PIECE_FRAMES, self.g.frame_count)
        return self.firsts[b + 1] if b + 1 < len(self.firsts) \
            else self.g.frame_count

    def _due(self, b):
        """True when block b may be cut now: not cut, not a kept error
        (unless its slow retry is due), not waiting for its retry."""
        if b in self.blocks:
            return False
        if b in self.errors:
            t = self.slow.get(b)
            return t is not None and self.clock() >= t
        r = self.retry.get(b)
        return r is None or self.clock() >= r[1]

    clock = staticmethod(time.monotonic)

    def ensure(self, lo, hi):
        """Cut every block holding frames lo to hi (inside the render),
        yielding the interpreter between blocks. Never raises."""
        if self.g is None:
            return
        n = self.g.frame_count
        lo, hi = max(0, lo), min(n - 1, hi)
        if lo > hi:
            return
        import time as _t
        for b in range(self._block(lo), self._block(hi) + 1):
            if not self._due(b):
                continue
            with self.lock:
                # Again under the lock: the other cutting thread may have
                # cut it, or failed it and set its retry, meanwhile.
                if not self._due(b):
                    continue
                r = self.retry.get(b)
                try:
                    rows, short = self._cut_block(b)
                    if short is not None:
                        self.short[b] = short
                    self.blocks[b] = rows
                    self.retry.pop(b, None)
                    self.errors.pop(b, None)
                    self.slow.pop(b, None)
                except Exception as e:
                    n = (r[0] if r else 0) + 1
                    e = _bare(e)
                    if _cut_data_error(e):
                        # The render's own bytes are wrong: for good.
                        self.errors[b] = e
                        self.retry.pop(b, None)
                        self.slow.pop(b, None)
                    elif n > CUT_RETRIES or b in self.errors:
                        # It failed every retry (a missing module, a bug,
                        # memory that does not come back): a fault, its
                        # error named, and still tried again every
                        # CUT_RETRY_SLOW_S (round 2 review of #177).
                        self.errors[b] = e
                        self.retry.pop(b, None)
                        self.slow[b] = self.clock() + CUT_RETRY_SLOW_S
                    else:
                        # Anything else (memory, the system) may pass: the
                        # block stays uncut, a CUES FAULT naming the error
                        # while the show is on it, and is tried again later
                        # (review of #177: one MemoryError faulted it all
                        # night).
                        self.retry[b] = (n, self.clock() + CUT_RETRY_S[
                            min(n, len(CUT_RETRY_S)) - 1], e)
            _t.sleep(0)

    def _cut_block(self, b):
        """(the rows of block b joined, None or (first missing frame, its
        error))."""
        g, cc = self.g, self.g.channel_count
        first, last = self._first(b), self._end(b)
        if self.whole:
            from .fseq import FSEQError
            end = self.boff + self.blen
            if end > len(self.mv):
                # As _load_block says it of the one block, for every frame.
                raise FSEQError(f"{g.path}: block 0 truncated "
                                f"({max(0, len(self.mv) - self.boff)} of "
                                f"{self.blen} bytes)")
            off = self.boff + first * cc
            raw = self.mv[off:max(off, min(end, off + (last - first) * cc))]
        else:
            try:
                raw = memoryview(g._load_block(b))
            finally:
                # Only this block's rows are kept, never its channel data.
                g._cache_idx, g._cache = -1, b""
        rows, short = [], None
        for n in range(first, last):
            at = (n - first) * cc
            if at + cc > len(raw):
                from .fseq import FSEQError
                # One sentence per render, whichever piece it is found in
                # (review of #177: a frame number in it journaled a line
                # for every piece).
                short = (n, FSEQError(CUT_SHORT_TEXT))
                break
            rows.append(FlameCues._cut(raw[at:at + cc], self.spans,
                                       self.span))
        return b"".join(rows), short

    def failing(self, idx):
        """The error frame idx's block failed with while it waits to be
        tried again, else None."""
        r = self.retry.get(self._block(idx))
        return r[2] if r is not None else None

    def row(self, idx):
        """Frame idx's flame channels (count bytes), None while its block
        is not cut yet; raises the error its block (or, in a block cut part
        of the way, that frame) could not be cut with."""
        b = self._block(idx)
        e = self.errors.get(b)
        if e is not None:
            # With no traceback: raised again on every frame, it would
            # grow one for good (final review of #177).
            raise e.with_traceback(None)
        blk = self.blocks.get(b)
        if blk is None:
            return None
        sh = self.short.get(b)
        if sh is not None and idx >= sh[0]:
            raise sh[1].with_traceback(None)
        at = (idx - self._first(b)) * self.count
        return blk[at:at + self.count]


class FlameCues:
    """flamelink's cue provider: the show's flame universe, read from the
    show's OWN render at the show timecode (PR #43 review, finding 1).

    The timecode FlameLink passes is the frame the show audio last sent
    (clock.AudioMaster: 00:00:00:00 at the top of the cue it is playing).
    The values are that frame of THAT cue's FSEQ, read through a file handle
    of this provider's own (never the pixel output's, whose block cache is
    not shared across threads), at the Inactive flame controller's
    channels. Never the pixel output's buffer: Blackout, Preshow and a look
    override leave that buffer holding a frame that is not the show's.

    None (all zeros) unless every one of these holds: Run pressed and the
    session running; the show audio playing a cue of this show file and
    not paused; the pixel output following the show (no Blackout, Preshow
    or look override, and no GO free run); the timecode is the clock's own
    current frame; the show folder has a usable flame controller; the frame
    is inside the cue's render. Never raises."""

    def __init__(self, control, name, journal=None, background=False):
        self.control = control
        self.name = name
        self._journal = journal
        self._folder = None      # (session, map path, its mtime and size)
        self._span = None
        self._total = None       # the map's channel count, for the render
        self._problem = ""
        self._why = ""
        # Since when a playing show has had a look over its pixels (see
        # LOOK_IN_SHOW_FAULT_S), or None.
        self._look_since = None
        self._mono = time.monotonic
        # The refusal now in force that is a FAULT (review of PR #43, P1-3:
        # a render that does not fit the layout, a timecode past its end, a
        # render that cannot be read, a render older than the layout or
        # made for another flame controller start): "" when none. Read by
        # /api/conductor (the Stream Deck) and the rack screen.
        self.fault = ""
        self._layout_mtime = None   # xlights_networks.xml's, at _locate
        self._open = {}     # fseq path -> (FSEQ, spans, its mtime, start)
        self.link = None    # the FlameLink (closing it closes the files)
        # background (ltc serve): every file is read, and every file's date
        # looked at, on a thread of its own, never on the flame link's
        # sender, so a stalled drive can never hold up a flame frame (the
        # show PC's SSD stalled on 2026-09-25). The render is read whole
        # into memory. Without it (the selftest), the same work is done in
        # the call, as before.
        self._bg = bool(background)
        self._layout = None      # (session, span, total, layout mtime)
        self._by_path = {}       # fseq path -> _render's tuple, or an error
        # Background mode (2026-10-29, Jeff's Pico: flame frames 78 to 178
        # ms late): the flame controller's channels, cut out of each render
        # block by block on the reading thread (_FlameCut), so the sender
        # only slices memory. It used to decompress the render's block
        # (megabytes) on the sender thread itself each time the show
        # crossed into the next one. Cut ahead: every cue's first
        # CUT_START_S before its render is published, and the playing cue
        # CUT_AHEAD_S ahead of the show timecode. fseq path -> _FlameCut.
        self._cuts = {}
        self._data = {}          # fseq path -> (its FSEQ, the render's bytes)
        self._stop = threading.Event()
        self._thread = None
        self._cutter = None      # the cutting thread (review of #174)
        self._died = {}          # thread attribute -> what ended it
        self._ended = {}         # thread attribute -> when it ended
        self._start_failed = set()
        self._start_tries = {}   # thread attribute -> (failed starts, last)
        self._restarts = {}      # thread attribute -> (restarts, last, began)
        self._start_lock = threading.Lock()
        self.thread_fault = ""   # a thread that keeps stopping, for screens
        self._uncut = None       # ((session, cue), since) while not cut yet
        self._mono = time.monotonic
        # The CUES FAULT said at show load (soak 2026-10-21): renders of
        # the running show older than its xlights_networks.xml, which
        # __call__ refuses. "" when none. on_render_fault(text) is told
        # each new one (web.serve raises the rack screen's alert with it).
        self.render_fault = ""
        self.on_render_fault = None
        # (session, the renders render_fault names), published by the
        # reading thread BEFORE render_fault. __call__ refuses a cue whose
        # render is named here even while the channels it reads from are
        # still the ones published before the layout changed (the same pass
        # reads xlights_networks.xml and the renders after its age check,
        # which can take seconds on a stalled drive) or none are published
        # yet, so the cues are never live, nor refused without the fault,
        # while the screens say CUES FAULT.
        self._stale = (None, frozenset())

    def _note(self, text, **f):
        if self._journal is not None:
            try:
                self._journal(text, **f)
            except Exception:
                pass

    def _zero(self, why, fault=False, cause=None):
        """All zeros, saying why once each time the reason changes (an
        episode); the cue going out again clears it. A `fault` is
        journaled as one and held in self.fault for the screens until
        the reason changes. `cause`, when given, is what counts as the
        reason for the journal (round 3 review of #177: one line per cause,
        not one for each block's own words)."""
        self.fault = why if fault else ""
        key = why if cause is None else cause
        if key != self._why:
            self._why = key
            if why:
                self._note(f"Flame cues are zero: {why}", action="flames",
                           outcome="cues_refused" if fault else "cues_zero",
                           **({"fault": True} if fault else {}))
        return None

    def _late_zero(self, key, why):
        """Zero while the show plays and its render or layout is still being
        read: silent for CUT_FAULT_AFTER_S (one episode per `key`, the
        session and its cue, until a row is served or the show stops,
        holds, pauses or leaves the show), then the CUES FAULT `why`
        (review of #177: a reader stalled at Run used to give silent zeros
        for as long as it lasted)."""
        now = self._mono()
        u = self._uncut
        if u is None or u[0] != key:
            u = self._uncut = (key, now)
        if now - u[1] < CUT_FAULT_AFTER_S:
            self.fault = ""
            return None
        return self._zero(why, fault=True)

    def _idle(self, why=""):
        """Zero while the show is not playing here (stopped, held, paused,
        a look or a free run): the uncut grace episode ends with it, so a
        Resume on an uncut frame starts a grace of its own (review of
        #177)."""
        self._uncut = None
        return self._zero(why)

    def _locate(self, session):
        """The flame controller's channels, found again for every new
        session and whenever xlights_networks.xml changes (fix round 2, A:
        they were cached per show folder for the life of ltc serve, so a
        layout changed with serve running read fire from the wrong
        channels)."""
        folder = getattr(getattr(session, "tl", None), "show_dir", None)
        path = getattr(session, "nm_path", None) or (
            os.path.join(folder, "xlights_networks.xml") if folder else None)
        try:
            st = os.stat(path) if path else None
            stamp = (st.st_mtime_ns, st.st_size) if st else None
        except OSError:
            stamp = None
        if self._folder is not None and self._folder[0] is session and \
                self._folder[1:] == (path, stamp):
            return self._span
        self._folder = (session, path, stamp)
        self._layout_mtime = stamp[0] if stamp else None
        self._span, self._total = None, None
        try:
            if not path:
                raise FlameControllerError("The running show has no show "
                                           "folder.")
            self._span = flame_channels(path, self.name)
            self._total = map_total_channels(path)
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

    def _render(self, path, start=None):
        """(FSEQ, spans, the render's mtime, the flame controller's first
        channel when this render was first read and checked)."""
        try:
            st = os.stat(path)
            key = (path, st.st_mtime_ns, st.st_size)
        except OSError:
            key = (path, None, None)
        got = self._open.get(key)
        if got is None:
            for old in [k for k in self._open if k[0] == path]:
                try:
                    self._open.pop(old)[0].close()
                except Exception:
                    pass
            import io
            from .fseq import FSEQ
            with open(path, "rb") as fh:
                data = fh.read()
            f = FSEQ(path, fileobj=io.BytesIO(data))
            spans, src = [], 0
            for start0, length in (f.sparse_ranges or
                                   [(0, f.channel_count)]):
                spans.append((start0, src, length))
                src += length
            got = self._open[key] = (f, spans, key[1], start)
            if self._bg:
                self._data[path] = (f, data)
        return got

    @staticmethod
    def _cut(data, spans, span):
        """The flame controller's `span` (first channel, count) out of one
        frame's channel bytes, as count bytes: the same cut __call__ makes,
        a channel no range covers left at zero."""
        start, count = span
        first, end = start - 1, start - 1 + count
        row = bytearray(count)
        for dst, src, length in spans:
            lo, hi = max(dst, first), min(dst + length, end)
            if lo < hi:
                row[lo - first:hi - first] = data[src + lo - dst:
                                                  src + hi - dst]
        if len(row) != count:
            # A range past the frame's bytes (round 2 review of #177):
            # never a row shifted out of place.
            from .fseq import FSEQError
            raise FSEQError(f"the render's sparse ranges run past its "
                            f"{len(data)} channels")
        return bytes(row)

    def _refresh_once(self):
        """Background mode: the channels and every cue's render of the
        running session, read and published for the sender to use."""
        s = getattr(self.control, "session", None)
        if s is None or not getattr(s, "running", False):
            return
        span = self._locate(s)
        self._layout = (s, span, self._total, self._layout_mtime)
        by = {}
        for c in (getattr(getattr(s, "tl", None), "cues", None) or ()):
            path = getattr(c, "path", None)
            if path:
                try:
                    by[path] = self._render(
                        path, span[0] if span else None)
                except Exception as e:
                    by[path] = e
        # Each render's cut (_FlameCut), its first CUT_START_S and the
        # playing cue's next CUT_AHEAD_S cut, published BEFORE the renders
        # themselves: whoever finds a render in _by_path finds the start of
        # its cues ready, as when only the file was read. One _FlameCut per
        # (FSEQ, span), kept from pass to pass and session to session; a
        # block that cannot be cut is kept as its error (tried again unless
        # it is the render's own bytes), and a cut that could not be set up
        # for a passing reason is set up again every CUT_RETRY_SLOW_S.
        cuts = {}
        for path, got in by.items():
            if isinstance(got, Exception) or span is None:
                continue
            cut = self._cuts.get(path)
            if cut is None or cut.f is not got[0] or \
                    cut.span != tuple(span) or cut.stale():
                mine = self._data.get(path)
                if mine is None or mine[0] is not got[0]:
                    continue    # its bytes are not this render's: not cut
                try:
                    cut = _FlameCut(path, got[0], got[1], span, mine[1])
                except Exception as e:
                    cut = _FlameCut.failed(got[0], span, e)
            cuts[path] = cut
        for cut in cuts.values():
            cut.ensure(0, cut.frames(CUT_START_S))
        # And the playing cue at its timecode now, so a render found in
        # _by_path is ready where the show is; from here on the cutting
        # thread keeps ahead of it.
        self._cut_ahead(s, cuts)
        self._cuts = cuts
        self._by_path = by

    def _cut_ahead(self, s=None, cuts=None):
        """The playing cue's blocks from its show timecode to CUT_AHEAD_S
        on, cut now (the cutting thread). Never raises."""
        try:
            s = s or getattr(self.control, "session", None)
            cuts = self._cuts if cuts is None else cuts
            clk = getattr(s, "clock", None)
            cue = getattr(clk, "_cue", None)
            last = getattr(clk, "last_sent", None)
            label = cue.get("label") if isinstance(cue, dict) else None
            if not label or not last:
                return
            hits = [c for c in (getattr(getattr(s, "tl", None), "cues",
                                        None) or ())
                    if getattr(c, "name", None) == label]
            cut = cuts.get(getattr(hits[0], "path", None)) \
                if len(hits) == 1 else None
            if cut is None:
                return
            rel = (last[0] * 3600 + last[1] * 60 + last[2]) + last[3] / 30.0
            idx = int(rel * 1000.0 // cut.g.step_time_ms)
            cut.ensure(idx, idx + cut.frames(CUT_AHEAD_S))
        except Exception:
            pass

    def _refresher(self):
        """The reading thread: file dates, the layout, the renders read
        into memory and their cuts made. The only one that touches the
        disk."""
        while not self._stop.is_set():
            try:
                self._check_renders_once()
            except Exception:
                pass
            try:
                self._refresh_once()
            except Exception:
                pass
            self._stop.wait(0.25)

    def _cutting(self):
        """The cutting thread (review of #174): the playing cue cut
        CUT_AHEAD_S ahead, every CUT_EVERY_S. Memory only, never the disk,
        so a read or a stat stalled on the reading thread cannot stop it."""
        while not self._stop.wait(CUT_EVERY_S):
            self._cut_ahead()

    def _guarded(self, attr, body):
        """A background thread's body. Whatever ends it is kept (its
        sentence), and when, so start() can say so when it starts it again
        and knows how long it ran."""
        try:
            body()
        except BaseException as e:               # noqa: BLE001
            self._died[attr] = f"{type(e).__name__}: {e}"[:200]
        finally:
            try:
                self._ended[attr] = self._mono()
            except BaseException:                # noqa: BLE001
                pass

    THREADS = (("_thread", "_refresher", "ltcplay-flame-cues-read",
                "reads the show's renders and checks their dates against "
                "the layout"),
               ("_cutter", "_cutting", "ltcplay-flame-cues-cut",
                "cuts the show's flame cues from its renders"))
    # A thread that keeps dying is started again later each time (review
    # of #177): at once, then 1 s, 2, 5, 10, 30 and every 60 s after it
    # died; one that ran this long (started to died) before it died starts
    # the count again. Time with no start() call never does.
    RESTART_BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
    RESTART_RESET_S = 60.0
    # After this many restarts in a row it is a fault on the screens too
    # (faults_now), until the thread runs RESTART_RESET_S again.
    RESTART_FAULT_AFTER = 3

    def start(self):
        """Background mode: the reading thread and the cutting thread,
        started now, so the running show's renders are read into memory
        when Run is pressed, long before its first flame frame (soak
        2026-10-21: it used to start on the first live frame, so every read
        landed at the top of a show). Asked again on every flame frame: a
        thread that has ended without close() is started again, backed off
        by RESTART_BACKOFF_S, journaled as a fault (review of #174: a
        dead reader left every flame zero for the rest of the night). Does
        nothing without background mode, or once closed."""
        if not self._bg or self._stop.is_set():
            return self
        # From the sender and from the screens' threads (render_fault_now):
        # one caller at a time, so a thread is never started twice.
        with self._start_lock:
            if self._stop.is_set():
                return self              # close() came in while we waited
            for attr, body, name, does in self.THREADS:
                self._start_one(attr, body, name, does)
        return self

    def _start_one(self, attr, body, name, does):
        t = getattr(self, attr)
        if t is not None and t.is_alive():
            return
        now = self._mono()
        n = 0
        # A start that failed is backed off on its own count (round 2
        # review of #177: a first start that failed was tried again on
        # every call), never counted as a restart (round 3: five failed
        # starts made the first real death wait 60 s and 'keep stopping').
        tried = self._start_tries.get(attr)
        if tried is not None and now - tried[1] < self.RESTART_BACKOFF_S[
                min(tried[0], len(self.RESTART_BACKOFF_S)) - 1]:
            return
        again = tried is not None
        if t is not None:
            n, last, began = self._restarts.get(attr, (0, None, None))
            ended = self._ended.get(attr)
            # How long it actually ran, started to died (review of #177:
            # the time since it was started counted a back-off as running).
            if began is not None and ended is not None and \
                    ended - began >= self.RESTART_RESET_S:
                n = 0
            since = ended if ended is not None and (
                last is None or ended > last) else last
            if since is not None and n and now - since < \
                    self.RESTART_BACKOFF_S[min(n, len(
                        self.RESTART_BACKOFF_S)) - 1]:
                return
            n += 1
        # Taken BEFORE the start (round 2 review of #177): a new thread that
        # dies at once leaves its own reason, never popped by this call.
        why = self._died.pop(attr, "it ended")
        try:
            nt = threading.Thread(target=self._guarded,
                                  args=(attr, getattr(self, body)),
                                  daemon=True, name=name)
            self._ended.pop(attr, None)
            nt.start()
        except Exception as e:                   # noqa: BLE001
            # Said once, then tried again after the back-off.
            self._died.setdefault(attr, why)
            self._start_tries[attr] = ((tried[0] if tried else 0) + 1, now)
            if attr not in self._start_failed:
                self._note(f"Flame cues: the thread {name} could not be "
                           f"started ({type(e).__name__}: {e}).", fault=True,
                           action="flames",
                           outcome="cues_thread_start_failed")
            self._start_failed.add(attr)
            return
        setattr(self, attr, nt)
        self._start_failed.discard(attr)
        self._start_tries.pop(attr, None)
        self._restarts[attr] = (n, now, now)
        if t is None and again:
            self._note(f"Flame cues: the thread {name} is running now, after "
                       f"it could not be started (try {tried[0] + 1}).",
                       fault=True,
                       action="flames", outcome="cues_thread_restarted")
        elif t is not None:
            # Said only once it is running again (review of #177).
            self._note(f"Flame cues: the thread {name} stopped ({why}); it "
                       f"was started again (restart {n}).", fault=True,
                       action="flames", outcome="cues_thread_restarted")
            if n >= self.RESTART_FAULT_AFTER:
                self.thread_fault = (
                    f"the thread that {does} keeps stopping ({n} restarts, "
                    f"the last: {why}); restart LTC Player")

    def _settle_threads(self):
        """A thread that has run RESTART_RESET_S since it was started again
        clears its screen fault."""
        now = self._mono()
        if self.thread_fault and all(
                (getattr(self, a) is not None and getattr(self, a).is_alive()
                 and (self._restarts.get(a, (0, None, now))[2] is None or
                      now - self._restarts.get(a, (0, None, now))[2]
                      >= self.RESTART_RESET_S))
                for a, _b, _n, _d in self.THREADS):
            self.thread_fault = ""

    def _cutter_dead(self):
        """True when a background thread could not be started again: the
        cues cannot follow, and the fault says so instead of promising
        them within a second."""
        return bool(self._start_failed)

    def _cutter_waiting(self):
        """True when the cutting thread has stopped and waits out its
        back-off to be started again."""
        t = self._cutter
        return t is not None and not t.is_alive() and \
            not self._stop.is_set()

    def _check_renders_once(self):
        """The running show's renders against its layout, by file date
        only (renders_older_than_layout). A new CUES FAULT is journaled
        and handed to on_render_fault once; none running, none."""
        s = getattr(self.control, "session", None)
        text, stale = "", []
        if s is not None and getattr(s, "running", False):
            tl = getattr(s, "tl", None)
            folder = getattr(tl, "show_dir", None)
            nm = getattr(s, "nm_path", None) or (
                os.path.join(folder, "xlights_networks.xml") if folder
                else None)
            paths = [getattr(c, "path", None)
                     for c in (getattr(tl, "cues", None) or ())]
            show = os.path.basename(str(getattr(s, "timeline_path", "")
                                        or ""))
            if nm:
                stale = renders_older_than_layout(nm, paths)
                text = render_age_sentence(nm, stale, show)
        # The refusal first, then the screens: whoever sees render_fault
        # set finds the cues already refused.
        self._stale = (s, frozenset(stale))
        if text and text != self.render_fault:
            self._note(text, fault=True, action="flames",
                       outcome="cues_fault_renders")
            tell = self.on_render_fault
            if tell is not None:
                try:
                    tell(text)
                except Exception:
                    pass
        self.render_fault = text
        return text

    def render_fault_now(self):
        """The show-load CUES FAULT for the screens. In background mode it
        is worked out on the reading thread (started here if need be),
        never on the caller's thread or the flame link's sender; without
        it (the selftest), here."""
        if self._bg:
            self.start()
        else:
            self._check_renders_once()
        return self.render_fault

    def faults_now(self):
        """What the rack screen and the Stream Deck show as CUES FAULT:
        the refusal now in force, else a background thread that keeps
        stopping (review of #177), else the show-load one. A thread that
        keeps stopping outranks a fault that only says the cues are on
        their way (CUES_WAITING): they are not."""
        rf = self.render_fault_now()
        self._settle_threads()
        if self.thread_fault and (not self.fault or
                                  self.fault in CUES_WAITING):
            return self.thread_fault
        return self.fault or rf

    def close(self):
        self._stop.set()
        with self._start_lock:
            pass                         # a start() under way has finished
        for t in (self._thread, self._cutter):
            if t is not None and t is not threading.current_thread():
                t.join(2.0)
        for got in self._open.values():
            try:
                got[0].close()
            except Exception:
                pass
        self._open.clear()
        self._data.clear()
        self._cuts = {}

    def __call__(self, tc):
        s = getattr(self.control, "session", None)
        if s is None or not getattr(s, "running", False):
            self._look_since = None
            return self._idle()
        clk = getattr(s, "clock", None)
        cue = getattr(clk, "_cue", None)
        if getattr(clk, "source", None) != "audio_master" or not cue or \
                getattr(clk, "paused", True):
            # No show playing: a look held over the last one never counts
            # toward the next one's CUES FAULT.
            self._look_since = None
            return self._idle()
        p = getattr(s, "player", None)
        if p is None:
            self._look_since = None
            return self._idle()
        look = getattr(p, "override", None)
        if look is None:
            self._look_since = None
        else:
            # Not the show: the uncut grace episode ends here too.
            self._uncut = None
            plain = f"the pixel output is on {look}, not the show"
            if getattr(p, "show_handoff", None) is not None or \
                    getattr(clk, "_halting", False) or \
                    getattr(clk, "_pause_req", False):
                self._look_since = None
                return self._zero(plain)
            now = self._mono()
            if self._look_since is None:
                self._look_since = now
            if now - self._look_since < LOOK_IN_SHOW_FAULT_S:
                return self._zero(plain)
            return self._zero(LOOK_IN_SHOW.format(
                look=LOOK_BUTTONS.get(look, look)), fault=True)
        if getattr(p, "freerun_epoch", None) is not None:
            return self._idle("the show was moved by hand (GO), so the "
                              "pixels are not following the show audio")
        last = getattr(clk, "last_sent", None)
        if not tc or not last or tc != (f"{last[0]:02d}:{last[1]:02d}:"
                                         f"{last[2]:02d}:{last[3]:02d}"):
            return self._zero("")
        if self._bg:
            self.start()
            stale_s, stale = self._stale
            label = cue.get("label") if isinstance(cue, dict) else None
            if stale_s is s and any(
                    getattr(c, "path", None) in stale
                    for c in (getattr(getattr(s, "tl", None), "cues", None)
                              or ()) if getattr(c, "name", None) == label):
                # The reading thread has found this cue's render older than
                # the layout: refused now, whatever the channels in memory say.
                return self._zero(RENDER_OLDER_THAN_LAYOUT, fault=True)
            lay = self._layout
            if lay is None or lay[0] is not s:
                return self._late_zero((id(s), label), CUES_READING_LAYOUT)
            span, total, laid = lay[1], lay[2], lay[3]
        else:
            span = self._locate(s)
            total, laid = self._total, self._layout_mtime
        if span is None:
            self.fault = ""
            return None
        tl = getattr(s, "tl", None)
        label = cue.get("label") if isinstance(cue, dict) else None
        hits = [c for c in (getattr(tl, "cues", None) or ())
                if getattr(c, "name", None) == label]
        if len(hits) != 1:
            return self._zero(f"the show audio is playing {label!r}, which "
                              f"is not one cue of this show file")
        try:
            if self._bg:
                got = self._by_path.get(hits[0].path)
                if got is None:
                    return self._late_zero((id(s), label),
                                           CUES_READING_RENDER)
                if isinstance(got, Exception):
                    # The render file itself could not be opened or read:
                    # said as a render that could not be read, never as a
                    # cut tried again (final review of #177), and never
                    # raised again on every frame.
                    name = type(got).__name__
                    return self._zero(
                        f"the show's render could not be read "
                        f"({name}: {got})", fault=True,
                        cause=("read", label, name))
                f, spans, made, checked_at = got
            else:
                f, spans, made, checked_at = self._render(hits[0].path,
                                                          span[0])
            # A render whose one range starts at channel 1 is a whole
            # render and must match the map exactly; a truly sparse one
            # must at least lie inside it.
            have = max(s + n for s, _src, n in spans)
            covered = sum(n for _s, _src, n in spans)
            if covered != f.channel_count:
                # Its sparse ranges and its channel count disagree (round 2
                # review of #177): every flame channel would be read from
                # the wrong place, in this path and the cut one alike.
                return self._zero(
                    f"the show's render has {f.channel_count} channels but "
                    f"its sparse ranges cover {covered}: it is damaged. "
                    f"Render the show again", fault=True)
            whole = len(spans) == 1 and spans[0][0] == 0
            if total is None or (have != total if whole
                                 else have > total):
                return self._zero(
                    f"the show's render has {have} channels but "
                    f"xlights_networks.xml lays out {total}: they were "
                    f"not made for each other, so which channels are the "
                    f"flames cannot be told. Render the show again for this "
                    f"layout", fault=True)
            if made is not None and laid is not None and made < laid:
                # Review of PR #43, P0-6: a layout changed after the render
                # was made can keep the same channel total and move the
                # flames, so the count alone cannot tell.
                return self._zero(RENDER_OLDER_THAN_LAYOUT, fault=True)
            if checked_at is not None and checked_at != span[0]:
                return self._zero(
                    f"the flame controller starts at channel {span[0]} in "
                    f"xlights_networks.xml now, but at channel {checked_at} "
                    f"when this render was checked: the layout changed "
                    f"under it. Render the show again for this layout",
                    fault=True)
            rel = (last[0] * 3600 + last[1] * 60 + last[2]) + last[3] / 30.0
            idx = int(rel * 1000.0 // f.step_time_ms)
            if not 0 <= idx < f.frame_count:
                # Past the render's last frame the flame output is zero,
                # always: never the last frame held, never extended.
                # Whether that is also a FAULT on the screens (2026-10-24:
                # a render 20 ms shorter than its audio raised one at the
                # end of every show) allows a tiny overrun: up to
                # RENDER_END_TOLERANCE_FRAMES of the render's own step.
                over_ms = rel * 1000.0 - f.frame_count * f.step_time_ms
                if idx >= f.frame_count and over_ms <= \
                        RENDER_END_TOLERANCE_FRAMES * f.step_time_ms:
                    return self._zero(
                        f"the show's render has ended (the timecode is "
                        f"within {RENDER_END_TOLERANCE_FRAMES} frames of "
                        f"its end)")
                if idx >= f.frame_count and _tail_by_design(
                        clk, label, f, rel):
                    # The show runs on past its lights file on purpose
                    # ('clock.zones.show_len_s', the video's length): zero,
                    # said, never a FAULT (safety review of #157, 4).
                    return self._zero(
                        "the show's lights file has ended and the show "
                        "runs on to its configured length with no flame "
                        "cues")
                return self._zero("the timecode is past the end of the "
                                  "show's render", fault=True)
            if self._bg:
                # Memory only on the sender (2026-10-29): the frame's flame
                # channels as the reading thread cut them from this very
                # render, for this very span. A block it could not cut
                # raises its error here (the render could not be read, a
                # fault). Not cut yet while the show plays: zero, and a CUES
                # FAULT once it has stayed so for CUT_FAULT_AFTER_S (a move
                # lands on uncut frames for a moment; the link's seek guard
                # holds them at zero then anyway), never a silent zero for
                # longer; and at once if the cutting thread is gone.
                cut = self._cuts.get(hits[0].path)
                row = None
                if cut is not None and cut.f is f and cut.span == span:
                    row = cut.row(idx)
                if row is None:
                    dead = self._cutter_dead()
                    failing = cut.failing(idx) if cut is not None else None
                    if failing is not None:
                        # Its block failed to cut and waits to be tried
                        # again: a CUES FAULT naming why, at once, never
                        # hidden by the grace (review of #177).
                        return self._zero(
                            f"the show's render could not be cut here "
                            f"({type(failing).__name__}: {failing}); it is "
                            f"tried again", fault=True,
                            cause=("retrying", label,
                                   type(failing).__name__))
                    # One episode per session and cue (review of #177):
                    # playing on across uncut blocks faults after
                    # CUT_FAULT_AFTER_S however short the blocks.
                    key = (id(s), label)
                    now = self._mono()
                    u = self._uncut
                    if u is None or u[0] != key:
                        u = self._uncut = (key, now)
                    if not dead and now - u[1] < CUT_FAULT_AFTER_S:
                        self.fault = ""
                        return None
                    return self._zero(
                        CUES_CUTTER_DEAD if dead else
                        self.thread_fault or (
                            CUES_CUTTER_WAITING if self._cutter_waiting()
                            else CUES_LOADING), fault=True)
                self._uncut = None
                count = span[1]
                out = [0] * 512
                out[0:count] = row
                self._why = ""
                self.fault = ""
                return out
            data = f.frame(idx)
        except Exception as e:
            name = type(e).__name__
            if self._bg and not _cut_data_error(e):
                # A passing trouble (memory, the system, a missing module):
                # said as one, never as a damaged file (round 3 review of
                # #177).
                return self._zero(
                    f"the show's flame cues could not be cut from its "
                    f"render ({name}: {e}); they are tried again every "
                    f"{CUT_RETRY_SLOW_S:g} s", fault=True,
                    cause=("cut", label, name))
            return self._zero(f"the show's render could not be read "
                              f"({name}: {e})", fault=True,
                              cause=("read", label, name))
        start, count = span
        first, end = start - 1, start - 1 + count
        out = [0] * 512
        for dst, src, length in spans:
            lo, hi = max(dst, first), min(dst + length, end)
            if lo < hi:
                out[lo - first:hi - first] = data[src + lo - dst:
                                                  src + hi - dst]
        self._why = ""
        self.fault = ""
        return out


class OffThreadJournal:
    """A journal that never makes its caller wait: each line is queued and
    written in order by a thread of its own. Up to MAX lines may wait; past
    that the newest are dropped and counted, and the count is written as a
    fault once the line is moving again. Never raises."""

    MAX = 500

    def __init__(self, journal):
        import collections
        self._journal = journal
        self._q = collections.deque()
        self._cv = threading.Condition()
        self._busy = False
        self.dropped = 0
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="ltcplay-flame-journal")
        self._thread.start()

    def __call__(self, text, **fields):
        with self._cv:
            if len(self._q) >= self.MAX:
                self.dropped += 1
                return None
            self._q.append((text, fields))
            self._cv.notify_all()
        return None

    def _run(self):
        while True:
            with self._cv:
                while not self._q:
                    self._cv.wait()
                text, fields = self._q.popleft()
                dropped, self.dropped = self.dropped, 0
                self._busy = True
            try:
                if dropped:
                    self._write(f"Flame link: {dropped} journal line(s) "
                                f"were dropped because the journal was not "
                                f"keeping up.",
                                {"fault": True, "action": "flame_link",
                                 "outcome": "journal_dropped"})
                self._write(text, fields)
            finally:
                with self._cv:
                    self._busy = False
                    self._cv.notify_all()

    def _write(self, text, fields):
        try:
            self._journal(text, **fields)
        except Exception:
            pass

    def flush(self, timeout=5.0):
        """Wait until every queued line is written (tests, and closing)."""
        with self._cv:
            return self._cv.wait_for(lambda: not self._q and not self._busy,
                                     timeout)


_log_writer = None     # the one BackgroundShowLog writer thread running


@atexit.register
def _drain_show_log():
    """Every queued show log line is written before the program exits."""
    lst = _log_writer
    if lst is not None:
        try:
            lst.stop()
        except Exception:
            pass


class BackgroundShowLog(showlog_mod.ShowLog):
    """The show log for Fire & Ice: showlog.ShowLog, byte for byte the GPL
    one, with its file handler (and the console echo) moved behind a queue
    onto one writer thread of its own. The threads that log, the show
    audio's timecode thread among them, then never write, flush or print a
    line, nor wait on the logging handler's lock while another thread does
    (PR #43 review, finding 9). A new one stops the last one's writer,
    which drains first, as ShowLog replaces the logger's handlers."""

    def __init__(self, path, echo=False, **kw):
        global _log_writer
        import logging
        import logging.handlers
        import queue
        import sys
        super().__init__(path, echo=False, **kw)
        old, _log_writer = _log_writer, None
        if old is not None:
            try:
                old.stop()
            except Exception:
                pass
        out = list(self._log.handlers)
        if echo:
            e = logging.StreamHandler(sys.stdout)
            e.setFormatter(logging.Formatter("%(message)s"))
            out.append(e)
        q = queue.SimpleQueue()
        self._writer = logging.handlers.QueueListener(q, *out)
        self._writer.start()
        _log_writer = self._writer
        self._log.handlers[:] = [logging.handlers.QueueHandler(q)]
        self.background = True

    def flush(self):
        """Wait until every line logged so far is written."""
        w = self._writer
        if w is not None and w is _log_writer:
            w.stop()
            w.start()


# The interpreter's switch interval while a flame link sends (2026-10-29).
# Python hands its lock (the GIL) to a waiting thread only after the
# running one has held it this long; the default is 5 ms. Every flame frame
# needs the lock two or three times (waking from its sleep, after each
# send), so with a few busy threads (status requests, the pixel output) the
# sender waited 5 ms a time, again and again: in the timing harness four
# busy threads made 99 in 100 frames within 190 ms at 5 ms and within
# 102 ms at 1 ms. Only `ltc serve` with a flame link sets it.
FLAME_SWITCH_INTERVAL_S = 0.001


def keep_flame_frames_on_time(sys_mod=None, gc_mod=None):
    """`ltc serve` with a flame link, once everything is built and before
    it serves (cli._cmd_serve): the switch interval to
    FLAME_SWITCH_INTERVAL_S, then one full garbage collection and
    gc.freeze(), so the objects made before it serves (the modules, the
    engine's own objects) are never walked again by the collector. A full
    collection holds the GIL from start to end: 24 to 97 ms each, every
    20 s or so, in the timing harness's engine, and no flame frame goes out
    meanwhile; after this a pass no longer walks those. Only once, at
    start: an object set aside is never collected, so nothing made later
    (a show loaded by Run) is ever frozen. Nothing about what is sent
    changes. Returns one sentence for the log. Never raises."""
    import gc as _gc
    import sys as _sys
    sys_mod = sys_mod or _sys
    gc_mod = gc_mod or _gc
    out = []
    try:
        was = sys_mod.getswitchinterval()
        sys_mod.setswitchinterval(FLAME_SWITCH_INTERVAL_S)
        out.append(f"the interpreter lock changes hands every "
                   f"{FLAME_SWITCH_INTERVAL_S * 1000:g} ms (was "
                   f"{was * 1000:g} ms)")
    except Exception as e:                       # noqa: BLE001
        out.append(f"the switch interval could not be set "
                   f"({type(e).__name__})")
    try:
        gc_mod.collect()
        gc_mod.freeze()
        out.append(f"{gc_mod.get_freeze_count()} objects made at start are "
                   f"left out of every garbage collection from now on")
    except Exception as e:                       # noqa: BLE001
        out.append(f"the start up objects could not be set aside "
                   f"({type(e).__name__})")
    return ("Flame link timing: " + "; ".join(out) + ", so neither can hold "
            "up a flame frame for long.")


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


def aim_sentence(slots, path):
    """The flame link's start line for the aim slots (2026-10-28)."""
    if slots:
        return (f"Flame link: aim slots "
                f"{', '.join(str(x) for x in sorted(slots))} (a wave "
                f"flamer's angle, never held at zero) read from {path}.")
    return (f"Flame link: no aim slots in {path}: every flame channel, a "
            f"wave flamer's angle too, waits to be seen at zero.")


def build_flame_link(cfg, control, show, journal=None):
    """The FlameLink for this config, started, or None when the config
    names no flamesafe config. Raises FireIceConfigError for a config file
    that cannot be read, so serve refuses to start rather than run without
    the link it was told to have."""
    if not cfg.flamesafe_config:
        return None
    from . import flamelink
    lcfg = flame_link_config(cfg)
    # The flame link's own sender thread writes its journal lines (a
    # timecode that stopped at a show's end, a seek, a refused flame
    # controller) at the very moments the rest of the show is busiest. A
    # journal line is never written on that thread: it is handed to a line
    # of its own, so nothing the night journal or the console does can
    # hold up a flame frame (CONTRACT.md's 50 ms floor).
    journal = OffThreadJournal(journal) if journal is not None else None
    cues = (FlameCues(control, cfg.flame_controller, journal,
                      background=True)
            if cfg.flame_controller else flamelink.zero_cues)
    link = flamelink.FlameLink(
        lcfg, cues=cues,
        show_state=flamelink.audio_master_state(show.clock_nolock),
        journal=journal)
    link.journal_line = journal
    # Aim slots (2026-10-28): which slots this engine leaves out of the
    # zero-first hold, and the file it read them from, said once at start
    # so the journal shows whether the wave flamer's angles are loaded.
    link.flamesafe_path = os.path.abspath(str(cfg.flamesafe_config))
    if journal is not None:
        journal(aim_sentence(lcfg.aim_slots, link.flamesafe_path),
                action="flame_link", outcome="aim")
    link.status_mirrored = _has_status_mirror(cfg.flamesafe_config)
    if journal is not None and not link.status_mirrored:
        journal("Flame link: flamesafe's status frames go to the Stream Deck "
                "program and flamesafe's config has no link.status_mirror_port"
                ", so this program cannot see flamesafe confirm a disarm, or "
                "raise the lock alarm itself; the Stream Deck shows both. "
                "No disarm fault is raised here after an Abort for that "
                "reason. Set status_mirror_port to give this program its "
                "own copy.",
                action="flame_link", outcome="no_status")
    # The seek guard (PR #39) counts the show audio's own timecode frames,
    # which are always 30 a second (clock.MASTER_FPS), whatever the show
    # file's rate.
    link.tc_fps = 30.0
    if isinstance(cues, FlameCues):
        cues.link = link
    return link


def _has_status_mirror(path):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
        return "status_mirror_port" in (doc.get("link") or {})
    except (OSError, ValueError, AttributeError):
        return False


# ------------------------------------------------------------- the runner --

def follow_show_bank(mm, note):
    """MadMapper's show bank set following timecode again, at every show
    start (Start now, a scheduled show) and Programming's Play from, sent
    right after its select and before the show audio starts.

    Why (Jeff's field report, 2026-10-28: after Abort, Reset, Start now
    MadMapper's clock stayed on the aborted frame and its video did not
    follow the new show): every Abort, stopped show and failed start ends
    with `/timelines/<show bank>/conductor/stop` (ConductorDevices.
    video_stop). Bench B2 measured that on a bank chasing Art-Net
    timecode: "Works, even while chasing: ... counter stopped although
    timecode kept arriving", and that `/timelines/<bank>/conductor/play
    ,T` after it had the bank "playing and chasing again". Nothing before
    this sent that play, so the bank stayed stopped. It is MadMapper's own
    documented OSC (madmapper.Link.play); no other message is sent.

    `note(text, fault=, action=, outcome=)`. Never raises."""
    try:
        mm.play(mm.cfg.show_bank, wait=False)
    except Exception as e:                   # noqa: BLE001
        try:
            note(f"Setting the MadMapper show bank following timecode "
                 f"again failed: {e}", fault=True, action="video",
                 outcome="failed")
        except Exception:                    # noqa: BLE001
            pass


def _full_stop(text):
    """`text` as one sentence ending in a full stop, for a refusal reason
    that goes on with what to press."""
    text = str(text or "").strip()
    return text if text.endswith((".", "!", "?")) else text + "."


class ShowRunner:
    """What the scheduler's dry run used to make up, done for real, only
    while Service.dry_run is False (config "scheduler_performs": true).
    schedule.py's "Contract for PR 3":

      START_SHOW     start_show(): the show cue on the running session's
                     clock (and the MadMapper show bank selected first).
                     Only once Run has been pressed: nothing reaches the
                     rig until then (CLAUDE.md), so without a running show
                     the start is refused and reported as SHOW_FAILED
                     with nothing_sent: no fade or dark sequence follows
                     it, and while Run is not pressed the scheduler sends
                     the conductor nothing at all (Service.run_gate).
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
        self._seen_state = None
        self._stop = threading.Event()
        self._thread = None
        # Continue anyway (Service.continue_anyway): (outputs off, who), for
        # the next start only. Never set by the scheduler itself.
        self.continue_off = None
        # Bumped (under _lock) every time a start sets what is left off:
        # poll clears it only if no start came since it read the state.
        self._off_seq = 0

    def _note(self, text, **fields):
        if self._journal is not None:
            try:
                self._journal(text, **fields)
            except Exception:
                pass

    # Why a start was refused, in plain words, each with what to press
    # (Jeff's field report, 2026-10-28: the alert said only "The show did
    # not start" and the reason was in the journal alone). Each is the
    # slot's reason on the rack screen ("FAULT (...)") and the start
    # alert's first sentence, so none ends in a full stop.
    AUTO_START_OFF = ("auto_start is off in ltcplay_fire_ice.json, so the "
                      "scheduler does not start a show by itself; press "
                      "Start now to start it")
    RUN_OFF = ("Run has not been pressed, so nothing may reach the rig; "
               "press Run on the Programming screen, then Start now")
    NOT_AUDIO_MASTER = ("the running show file's clock is not "
                        "\"audio_master\", so this program cannot start it; "
                        "on the Programming screen press Stop, choose the "
                        "show file whose clock is \"audio_master\", then "
                        "press Run")
    LATCHED = ("the show is aborted, and the show conductor stays latched "
               "until someone presses Reset; press Reset, then Start now")
    AUDIO_LOADING = ("the show audio is still loading (it takes a few "
                     "seconds after Run); wait a moment, then press Start "
                     "now, which waits for it to finish loading")
    AUDIO_FAILED = ("the show audio did not load: {why} Replace the file, "
                    "then on the Programming screen press Stop and Run "
                    "again")
    AUDIO_DEVICE = ("the show audio interface is not available: {why} "
                    "Plug it in or pick it again, then press Start now")
    NEVER_CONFIRMED = ("the show audio stopped before its timecode was seen "
                       "moving ({how}); check the show audio interface, "
                       "then press Start now")
    SHOW_START_ERROR = ("{why} Fix that, then press Start now")

    def _refusal(self, s, clk):
        """The sentence a start would be refused with before anything goes
        out, or "": Run not pressed, a show file this program cannot start,
        or show audio that is not ready (still loading, failed to load, its
        interface missing). Reads the clock's own records without its lock:
        a guess here only words the refusal, never decides a start."""
        if s is None:
            return self.RUN_OFF
        if clk is None:
            return self.NOT_AUDIO_MASTER
        try:
            if getattr(clk, "_live", True) and \
                    getattr(clk, "_device_ok", True) is False:
                why = getattr(clk, "_fault", None) or "it has not opened yet."
                return self.AUDIO_DEVICE.format(why=_full_stop(why))
            failed = dict(getattr(clk, "_load_failed", None) or {})
            loaded = set(getattr(clk, "_loaded", None) or ())
            role = self._show_role(s, clk)
        except Exception:                        # noqa: BLE001
            return ""
        # Only the show cue's own audio (review of #144, F3): a slow or
        # broken intermission file never holds the show back, exactly as
        # clock.play judges only the cue it is asked to play.
        roles = [role] if role is not None else []
        for role in roles:
            if role in failed:
                return self.AUDIO_FAILED.format(why=_full_stop(failed[role]))
        if any(role not in loaded for role in roles):
            return self.AUDIO_LOADING
        return ""

    def _show_role(self, s, clk):
        """The show audio role of the cue start_show plays (cfg.show_cue,
        or the show file's first cue), picked the way Session.clock_play
        picks it, or None when it cannot be told (then nothing is judged
        here and clock.play says what is wrong)."""
        cues = list(getattr(getattr(s, "tl", None), "cues", None) or ())
        table = getattr(clk, "cues", None) or {}
        want = self.cfg.show_cue
        if want is None:
            pick = cues[0] if cues else None
        else:
            w = str(want).strip().lower()
            hits = [c for c in cues
                    if w in (str(getattr(c, "name", "")).lower(),
                             os.path.basename(str(getattr(c, "path", "")))
                             .lower(),
                             str(getattr(c, "tc_text", "")).lower())]
            pick = hits[0] if len(hits) == 1 else None
        if pick is None:
            # No show file cues to read (or no match): the table's own
            # entry under the cue's name, if it has one.
            got = table.get(want) if want is not None else None
        else:
            got = table.get(getattr(pick, "name", None))
        if isinstance(got, tuple) and got:
            return got[0]
        return None

    def start_blocked(self):
        """What an operator's Start now would be refused with right now, as
        {"text", "loading"}, or None when nothing is in the way (the
        scheduler's own rules, an Abort latch among them, still apply).
        Service.operator_press refuses Start now with it before anything is
        decided, and the rack screen shows it on the button: a start that
        cannot work is never turned into a FAULT slot and a generic alert
        (Jeff, 2026-10-28). Never raises."""
        try:
            # No lock taken and nothing hooked (show._clock() takes the
            # show's lock): this is read on every status poll.
            s = self.show._session()
            clk = getattr(s, "clock", None) if s is not None else None
            if clk is not None and \
                    getattr(clk, "source", None) != "audio_master":
                clk = None
            why = self._refusal(s, clk)
        except Exception:                        # noqa: BLE001
            return None
        if not why:
            return None
        return {"text": why, "loading": why == self.AUDIO_LOADING}

    def start_show(self, n, who="the scheduler"):
        """Called on the scheduler's ordered line of conductor requests,
        off its lock, after tonight is saved; the conductor is told a show
        started only once it is confirmed. Returns a conductor Result at
        once. `who` is "the scheduler" for a show that came due, or the
        operator who pressed Start now."""
        clk = self.show._clock()
        s = self.show._session()
        # A Continue anyway is for the start it was pressed for, whatever
        # happens to it: never carried to a later one.
        cont, self.continue_off = self.continue_off, None
        auto = who == "the scheduler"
        if auto and self.cfg.auto_start == "off":
            why = self.AUTO_START_OFF
            with self._lock:
                self._cue = {"show": n, "failed": why, "nothing_sent": True}
            return C.failed(f"Show {n} was not started: {why}.")
        why = self._refusal(s, clk) or (
            self.LATCHED if self.conductor.latched else "")
        if why:
            # Refused before anything went out: the failed start this is
            # reported as sends nothing either (no fade, no MadMapper).
            with self._lock:
                self._cue = {"show": n, "failed": why, "nothing_sent": True}
            return C.failed(f"Show {n} was not started: {why}.")
        pix = self._clear_look(s, n)
        # This start's own hand-over (one slot on the player): what poll()
        # may later cancel, never a newer start's.
        hand = getattr(pix, "show_handoff", None)
        if pix is None and self.conductor.latched:
            # An Abort landed just before the hand-over: its black stands
            # and nothing goes out.
            why = self.LATCHED
            with self._lock:
                self._cue = {"show": n, "failed": why, "nothing_sent": True}
            return C.failed(f"Show {n} was not started: {why}.")
        off = self._outputs_off(clk, n, cont)
        if self.mm is not None and "video" not in off:
            try:
                self.mm.select_bank(self.mm.cfg.show_bank, wait=False)
            except Exception as e:
                self._note(f"Selecting the MadMapper show bank failed: {e}",
                           fault=True, action="video", outcome="failed")
            follow_show_bank(self.mm, self._note)
        sent = self._mono()
        try:
            pick = s.clock_play(self.cfg.show_cue)
        except Exception as e:
            # The clock's own sentence, then what to press (Jeff,
            # 2026-10-28): "still loading" says to wait.
            why = self._refusal(s, clk) or self.SHOW_START_ERROR.format(
                why=_full_stop(e))
            self._start_undone()
            with self._lock:
                self._cue = {"show": n, "failed": why}
            return C.failed(f"Show {n} was not started: {why}.")
        # The music is now playing, whatever the conductor's record says:
        # an Abort or Hold before SHOW_CONFIRMED must stop or freeze it (PR
        # #43 review, finding 4).
        told = getattr(self.conductor, "music_started", None)
        if told is not None:
            told()
        if self.conductor.latched:
            # An Abort landed while the cue was starting, so its music step
            # may have run before the cue began: stop the music here too.
            why = "it was aborted while it was starting"
            self.show.music_halt(C.ABORT_FADE_S)
            self._start_undone()
            with self._lock:
                self._cue = {"show": n, "failed": why}
            return C.failed(f"Show {n} was not started: {why}; the music "
                            f"was stopped.")
        with self._lock:
            self._cue = {"show": n, "clock": clk,
                         "played": clk.cues_played, "confirmed": False,
                         "failed": None, "pix": pix, "sent": sent,
                         "hand": hand}
        return C.done(f"Show {n}: {pick.name} started on the show audio.")

    def _degraded_over(self, off_seq):
        """Out of the show: what a Continue anyway left off for it ends
        (the conductor's record, MadMapper's timecode mute). Only ever
        removes a restriction once no show is up: never when a start has
        set what is left off since poll() read the state (`off_seq`), nor
        when the scheduler is in a show again by now (delta review of
        #162). Never raises."""
        with self._lock:
            try:
                m = self.svc.machine
                if self._off_seq != off_seq or (
                        m is not None and m.state in ("SHOW", "PAUSED")):
                    return
                self._clear_off()
            except Exception:                    # noqa: BLE001
                pass

    def _clear_off(self):
        try:
            if self.conductor.outputs_off:
                self.conductor.outputs_off = frozenset()
            out = getattr(self.show._clock(), "out", None)
            if out is not None and getattr(out, "muted", None):
                out.muted = frozenset()
        except Exception:                        # noqa: BLE001
            pass

    def _outputs_off(self, clk, n, got):
        """What this show leaves off: only what a person's Continue anyway
        named (consumed here, one show), else nothing. The conductor keeps
        those dark for the show, and MadMapper is sent no timecode when its
        video failed. Never raises."""
        off, who = got if got else (frozenset(), "")
        with self._lock:
            self._off_seq += 1
        try:
            self.conductor.outputs_off = frozenset(off)
            out = getattr(clk, "out", None)
            if out is not None:
                out.muted = frozenset(
                    str(lbl).strip().lower() for lbl, _ip in
                    getattr(out, "dests", ()) or ()
                    if "madmapper" in str(lbl).lower()) \
                    if "video" in off else frozenset()
        except Exception as e:                   # noqa: BLE001
            self._note(f"Continue anyway: what to leave off could not be "
                       f"set ({type(e).__name__}: {e}).", fault=True,
                       action="pre-roll", outcome="error")
        if off:
            self._note(f"Show {n} started by {who}'s Continue anyway, with "
                       f"{', '.join(sorted(off))} left off for this show.",
                       fault=True, action="pre-roll", outcome="degraded",
                       show=n)
        return frozenset(off)

    def _clear_look(self, s, n):
        """Every look that is not the show is cleared for it, in the one
        place every start does it (FireIceShow.pixels_for_show), whatever
        ended the show before: never left to that show's own end (Jeff's
        report, 2026-10-29: after a show that finished on its own, the
        closing black stayed on all of the next show). Returns the player
        whose hand-over poll() watches for the first show frame, or None
        when an Abort latched first (its black stands; nothing handed
        over). The conductor's pixels record goes UNKNOWN in the same step
        (Conductor.pixels_to_show), so a Hold, an Abort or a Stop after
        it sends its black (review of PR #171)."""
        what = f"Show {n} start"
        to_show = getattr(self.conductor, "pixels_to_show", None)
        if to_show is None:
            self.show.pixels_for_show(what)
        elif to_show(lambda: self.show.pixels_for_show(what)) is None:
            return None
        return getattr(s, "player", None)

    def _start_undone(self):
        """A start that did not happen (the clock refused it, an Abort
        landed while it was starting): the hand-over it armed ends and the
        pixels are black, never the show nor the look held at the start
        (review of PR #171). The conductor's dark sequence for the failed
        start or the Abort sends its black again. Never raises."""
        try:
            self.show.pixels_fade_out(0)
        except Exception:                        # noqa: BLE001
            pass

    def poll(self):
        with self._lock:
            off_seq = self._off_seq
        sch = self.svc.machine
        state = sch.state if sch is not None else None
        # The state the last poll saw: a closing straight out of a show is
        # that show's own end (_close).
        was, self._seen_state = self._seen_state, state
        with self._lock:
            cue = self._cue
        if state == "CLOSING":
            if not self._closing_reported:
                self._closing_reported = True
                self._close(after_show=was in ("SHOW", "PAUSED"))
            return
        self._closing_reported = False
        if state not in ("SHOW", "PAUSED"):
            self._degraded_over(off_seq)
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
                self.svc.report("SHOW_FAILED", cue["failed"], show=n,
                                nothing_sent=cue.get("nothing_sent") is True)
            return
        self._first_frame(cue)
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
            self._handoff_over(cue)
            if not cue["confirmed"]:
                how = self.NEVER_CONFIRMED.format(how=how)
            self.svc.report("SHOW_ENDED" if cue["confirmed"]
                            else "SHOW_FAILED", how, show=n)

    @staticmethod
    def _handoff_over(cue):
        """The show this start was for failed or ended: its hand-over, if
        still waiting for a first frame, never lifts the look on later
        timecode (review B of PR #171: a show that never confirmed and
        ended late, then an Abort refused as "nothing is playing", left it
        armed). Never raises."""
        hand = cue.get("hand")
        cancel = getattr(cue.get("pix"), "cancel_show_handoff", None)
        if cancel is not None and hand is not None:
            try:
                # Only this start's own hand-over: a new start may have
                # armed the player's one slot since poll() read this cue.
                cancel(hand)
            except Exception:                    # noqa: BLE001
                pass

    @staticmethod
    def _mono():
        from . import player as player_mod
        return player_mod._now()

    def _first_frame(self, cue):
        """Once: when the show's first frame went out on the pixels, in
        the journal (Jeff's report, 2026-10-29, the blink at the start)."""
        p = cue.get("pix")
        done = getattr(p, "show_handoff_done", None) if p is not None \
            else None
        if done is None or cue.get("pix_said"):
            return
        cue["pix_said"] = True
        look, at = done
        took = max(0.0, at - cue.get("sent", at))
        was = {None: "the idle look", "blackout": "black",
               "preshow": "the Preshow look"}.get(look, look)
        self._note(f"Show {cue['show']}: the first show frame went out on "
                   f"the pixels {took:.2f} s after the start was sent. The "
                   f"pixels went from {was} to the show on that frame, with "
                   f"no dark frame between.", action="pixels",
                   outcome="first_show_frame", show=cue["show"])

    def _operator_show_ended(self):
        """The show that ended last was an operator's extra show (Start
        now) and it ended on its own. Never raises."""
        try:
            m = self.svc.machine
            ended = [x for x in m.slots if x.ended_at is not None]
            if not ended:
                return None
            last = max(ended, key=lambda x: x.ended_at)
            if last.origin == "operator" and \
                    str(last.reason).startswith("DONE") and \
                    m.last_end == last.ended_at:
                return last.n
        except Exception:                        # noqa: BLE001
            pass
        return None

    def _close(self, after_show=False):
        if self.show._session() is None:
            # Run is not pressed: the closing sends nothing to the rig.
            self._note("Closing: Run has not been pressed, so nothing was "
                       "sent to the rig.", action="closing",
                       outcome="held back")
        else:
            r = self.conductor.intermission("the scheduler", "")
            if not r.ok:
                self._note(f"Closing: {r.sentence}", fault=True,
                           action="closing", outcome="failed")
            extra = self._operator_show_ended() if after_show else None
            if extra is None:
                self.show.pixels_fade_out(1.0)
            else:
                # Jeff's report, 2026-10-29: a show started with Start now
                # ends on the idle look like a scheduled show that has
                # another after it, not on the closing black.
                self._note(f"Closing: the pixels stay on the idle look. "
                           f"Show {extra} was started with Start now and "
                           f"ended on its own, so it ends like a scheduled "
                           f"show, not on black. Flame cues are zero and "
                           f"the lasers are dark.", action="closing",
                           outcome="idle_look", show=extra)
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

    def __init__(self, conductor, show, devices, runner, flame_link=None,
                 arm_slot=None):
        self.conductor = conductor
        self.show = show
        self.devices = devices
        self.runner = runner
        self.flame_link = flame_link
        self._arm_slot = arm_slot if arm_slot is not None else {}

    @property
    def laser_arm(self):
        return self._arm_slot.get("arm")

    def attach_laser_arm(self, arm):
        """The Morning page's laser arm (laserarm.LaserArm), once it is
        built: the laser gate and the Blanking's arm_check ask it, Abort and
        Hold disarm through it, and arming every laser tells the conductor
        (lasers_armed) so a show already up lights them again."""
        self._arm_slot["arm"] = arm
        if arm is not None:
            arm.on_permitted = self.conductor.lasers_armed

    def close(self):
        try:
            if self.runner is not None:
                self.runner.close()
            self.conductor.close()
            gate = getattr(getattr(self.devices, "beyond", None), "gate",
                           None)
            if gate is not None:
                gate.close()
        finally:
            # Last: zero frames until the end. flamesafe disarms every
            # group once it stops hearing it.
            fl = self.flame_link
            if fl is not None and hasattr(fl, "stop"):
                try:
                    fl.zero()
                finally:
                    fl.stop()
                    closer = getattr(getattr(fl, "cues", None), "close",
                                     None)
                    if closer is not None:
                        closer()


def attach(svc, control, cfg, madmapper=None, beyond=None, announce=None,
           journal=None, flame_link=None, threaded=True, alert_pixels=None,
           **conductor_kw):
    """Build the real Conductor and attach it to the scheduler service
    `svc`. `madmapper` is web.serve's (link, watchdog) pair or None,
    `beyond` its Beyond or None. Call before svc.start(), so the first tick
    already reaches the conductor. `threaded` False and `conductor_kw`
    (clock, waiter) are the selftest's: nothing runs on its own then."""
    link = madmapper[0] if madmapper is not None else None
    built_link = None
    # A finished show's held timecode frame never goes to BEYOND, whatever
    # the lasers' blanking mode (clock.TimecodeOut.held_dests; safety
    # review of #157, 1).
    from . import clock as clock_mod
    clock_mod.HOLD_NEVER.update(beyond_addresses(cfg, beyond))
    # The show log is written on a thread of its own, so the show audio's
    # timecode thread never writes, flushes or prints a line itself, nor
    # waits on the logging lock (PR #43 review, finding 9).
    defaults = dict(getattr(control, "defaults", None) or {})
    defaults["log_factory"] = BackgroundShowLog
    control.defaults = defaults
    opens = []        # the checks a show must pass before it opens
    if cfg.flame_controller:
        # The flame controller's channels are never sent by the pixel
        # output in Fire & Ice, whatever xlights_networks.xml says, and a
        # show whose flame controller is Active is refused at Run (PR #43
        # review, finding 3).
        defaults = dict(getattr(control, "defaults", None) or {})
        defaults["exclude_controllers"] = (cfg.flame_controller,)
        control.defaults = defaults

        fs_dest = (flamesafe_destination(cfg.flamesafe_config)
                   if cfg.flamesafe_config else None)

        def flame_check(show_file, _name=cfg.flame_controller):
            """Refuses a show that cannot run safely with flames, and
            returns what the session must leave out of the pixel output:
            the flame controller by name AND every address and universe
            the flame node is reached at (fix round 2, B)."""
            from .session import SessionError
            try:
                blocked = refuse_active_flame_controller(show_file, _name,
                                                         fs_dest)
                # The alert pulse's floor pixels are never the flame
                # controller's channels (alerts.py, 2026-10-12).
                refuse_alert_pixels_on_flame_channels(show_file, _name,
                                                      alert_pixels)
            except FlameControllerError as e:
                raise SessionError(f"This show will not start: {e}")
            return {"exclude_destinations": tuple(sorted(blocked))}
        opens.append(flame_check)
    elif cfg.flamesafe_config and journal is not None:
        journal("Flame cues: no 'flame_controller' is named in "
                "ltcplay_fire_ice.json, so no flame cue is ever sent: every "
                "flame frame is zero.", fault=True, action="flames",
                outcome="no_controller")
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
            # The flame cues' reading thread from now on, not from the
            # first flame frame of a show (soak 2026-10-21).
            if isinstance(getattr(built_link, "cues", None), FlameCues):
                built_link.cues.start()
        else:
            built_link.open()
    else:
        show = FireIceShow(control, journal=journal, flame_link=flame_link)
    blanking = build_blanking(cfg, beyond, journal, threaded)
    if blanking.fault is not None:
        def laser_fault_check(show_file, _b=blanking):
            # Loud, never a refusal: running is the operator's call.
            _b.note_not_set_up(f"show start, "
                               f"{os.path.basename(str(show_file))}")
            return None
        opens.append(laser_fault_check)
    gate = getattr(blanking, "gate", None)
    if gate is not None:
        def laser_check(show_file, _gate=gate):
            """A show whose timecode would reach BEYOND around the black
            zone, or not through it at all, is refused (review of PR #43,
            P0-2); the one BEYOND destination's address is the gate's when
            it has none yet."""
            from .session import SessionError
            try:
                got = beyond_timecode_route(
                    show_file, _gate.ip,
                    timecode_ip=getattr(cfg, "beyond_timecode_ip", None),
                    lasers=lasers_configured(cfg))
            except FireIceConfigError as e:
                raise SessionError(f"This show will not start: {e}")
            except Exception as e:
                # Re-review P1-B: a check that cannot be made is a refusal,
                # never a pass.
                raise SessionError(
                    f"This show will not start: BEYOND's timecode route in "
                    f"{os.path.basename(show_file)} could not be checked "
                    f"({type(e).__name__}: {e}).")
            if got is None:
                if journal is not None:
                    journal(no_route_sentence(os.path.basename(show_file),
                                              cfg),
                            action="lasers", outcome="no_route")
                return None
            _gate.adopt(got[1])
            return None
        opens.append(laser_check)
    if opens:
        def before_open(show_file, _checks=tuple(opens)):
            extra = {}
            for chk in _checks:
                got = chk(show_file)
                if isinstance(got, dict):
                    extra.update(got)
            return extra
        control.before_open = before_open
    devices = C.ConductorDevices(link, blanking, journal=journal)

    def state():
        m = svc.machine
        return m.state if m is not None else None
    # The laser arm (laserarm.py, Jeff 2026-10-22), handed in later by
    # Wiring.attach_laser_arm. Until then, and if it never comes, the
    # lasers stay dark: there is no default that could light them.
    arm_slot = {"arm": None}

    def arm_gate(_slot=arm_slot):
        arm = _slot.get("arm")
        if arm is None:
            return ("laser arming is not set up in this engine (it lives on "
                    "the Morning and Shutdown page)")
        return arm.may_enable()
    blanking.arm_check = arm_gate
    state_gate = C.laser_gate_for(state)

    def laser_gate():
        why = state_gate()
        if why is not None:
            return why
        why = arm_gate()
        if why is not None:
            return f"the lasers are not armed: {why}. They stay dark."
        return None

    def lasers_disarm(reason, who, screen, _slot=arm_slot):
        arm = _slot.get("arm")
        if arm is not None:
            arm.disarm_all(reason, who, screen, defer=True)
    conductor = C.Conductor(
        devices, show, laser_gate,
        hold_gate=svc.hold_for_announcement,
        announcer=announce.play if announce is not None else None,
        journal=journal, threaded=threaded, lasers_disarm=lasers_disarm,
        **conductor_kw)
    svc.conductor = conductor
    # Nothing the scheduler decides by itself reaches this conductor until
    # the operator presses Run (Service._hold_back_unattended).
    svc.run_gate = lambda: show._session() is not None
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
    return Wiring(conductor, show, devices, runner, built_link, arm_slot)


def build_blanking(cfg, beyond, journal=None, threaded=True):
    """BEYOND as the conductor sees it: beyondtc.Blanking, keeping the
    lasers dark by timecode, OSC or both (cfg.beyond_blank). Journals the
    mode. A mode that uses OSC with no BEYOND OSC target still gets a
    Blanking, whose standing fault (beyondtc.NOT_SET_UP) is journaled here
    and at every show start, and whose blank() says False: never a quiet
    pass."""
    from . import beyondtc
    mode = getattr(cfg, "beyond_blank", BEYOND_BLANK_DEFAULT)
    gate = None
    if mode in ("timecode", "both"):
        ip = getattr(cfg, "beyond_timecode_ip", None) or (
            beyond.cfg.host if beyond is not None and
            getattr(beyond, "cfg", None) is not None else None)
        gate = beyondtc.TimecodeGate(ip, hour=cfg.beyond_black_hour,
                                     journal=journal)
        if threaded:
            # Only a real serve registers the gate with the show's timecode
            # sender; the selftest's unthreaded attach() starts it itself.
            gate.start()
    if journal is not None:
        where = gate.ip if gate is not None and gate.ip else \
            "the show file's BEYOND node"
        what = {"timecode": f"by timecode: BEYOND's own Art-Net timecode "
                            f"({where}) "
                            f"jumps to the black zone, hour "
                            f"{cfg.beyond_black_hour}, running, whenever the "
                            f"lasers must be dark",
                "osc": "by OSC: BEYOND's brightness 0 or 100",
                "both": f"by timecode (black zone hour "
                        f"{cfg.beyond_black_hour}) AND by OSC brightness"}[mode]
        journal(f"Lasers are blanked {what} (beyond_blank \"{mode}\").",
                action="lasers", outcome="blank_mode")
    blanking = beyondtc.Blanking(mode, gate=gate, osc=beyond, journal=journal)
    blanking.note_not_set_up("at startup")
    return blanking


class _LinkSlot:
    """Stands in for the flame link only while it is being built, so
    FireIceShow does not journal "no flame link" for a link that is about
    to exist."""

    def __init__(self, holder):
        self.holder = holder
