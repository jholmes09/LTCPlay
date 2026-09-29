"""The show conductor: the one place Hold, Resume, Abort and an announcement
are carried out on the rig, fire, lasers, video, pixels and music together.

Imported ONLY by code that runs the Fire & Ice show. The GPL show at
Dollywood never reaches it (test_the_gpl_path_never_loads_the_conductor).
In this build nothing constructs a Conductor outside the selftest: the
scheduler still runs dry (schedule_service.DRY_RUN), and the real laser and
video calls (PR #17, madmapper.py and beyond.py) are not merged yet. See
DeviceOutputs for exactly what that integration has to provide.

What it does, from the handoff and Jeff's decisions
===================================================

Abort (sections 4a and 5, Jeff 2026-09-26 and 2026-09-27): the software
E-stop. Every flame cue channel goes to zero and every flame group is asked
to disarm INSTANTLY, on the pressing thread, before this call returns. The
lasers (a BEYOND brightness ramp, not an instant blank), video, pixels and
music then fade to black TOGETHER over 1 s, and the video stops. It LATCHES:
nothing else is accepted until one Reset press. Only while something plays.

Hold, production (section 5, Jeff 2026-09-27): flames to zero and the lasers
blanked by a real command (a frozen laser cue is a static beam), then the
music fades over 0.25 s and the clock freezes on the frame where the fade
ends. That fade then freeze is clock.py's own (AudioMaster.pause); this
module asks for it and waits for the clock to say it froze. Video and pixels
hold on the frozen frame, as the handoff says ("pixels hold, video holds").
Rehearsal (Jeff 2026-09-27): the same Hold with no fade, a mode flag, not a
second code path.

Resume: the music fades back in from the frozen frame; once the clock says
the timecode is MOVING again, the lasers come back (if the show is not in
intermission) and the flame cues are released. Never before: a flame cue
released while the clock is still frozen would hold that frame's fire.

An announcement during a show (Jeff 2026-09-27): Hold the production way but
with lasers, video and pixels faded to black, wait about 0.5 s in the dark,
then play. schedule_service.hold_for_announcement is the "may I hold" gate
(called here, not re-implemented); announce.play plays the file.

No lasers during intermission (Jeff 2026-09-26): every path that would light
the lasers goes through _restore_lasers(), which asks `laser_gate` first and
on anything but a clear yes, including an error or an unknown state, BLANKS
them (unless already known dark). And intermission() blanks them the moment
the scheduler leaves the show, so it does not rest on how the show ended.

Design: one executor, one generation, one record of what was sent
==================================================================

The failure this module exists to rule out is two sequences racing: a Hold
fade still running when Abort starts its own, or a Resume whose last step
("lasers back on") lands after an Abort has already blanked them. Three
pieces, each small enough to prove:

1. ONE executor. Every request (Hold, Resume, Abort, announce, show start)
   is turned into one "effect" and handed to a single executor thread. Two
   effects never run at once, so two fades can never race on this side.

2. ONE generation counter, `_gen`, bumped by every accepted request, under
   `_lock`. An effect captures the generation it was started for and checks
   it is still current before EVERY step, inside the same locked section as
   the step itself (_step). Waits (a fade, the 0.5 s in the dark, waiting
   for the clock to freeze or move) are on a condition that every request
   notifies, and they re-check the generation on waking (_pause). So a
   superseded effect never sends another command: not after its wait, not
   between two steps, not half way through one. It raises _Superseded,
   writes one line saying how far it got, and the executor starts the newer
   effect at once. This is the pattern clock.py uses for its cue `_token`
   (a stale reading from an older play is ignored), announce.py for its
   `_claim_gen`, and PR #17's madmapper.Link for its ramp `_gen`; here it
   guards whole sequences rather than single messages.

   Why check inside the lock, not just before the call: "check, then act"
   with the lock released in between leaves a window in which Abort can
   bump the generation and cut the flames, and then the stale step
   releases them again. Holding `_lock` across check and call closes it:
   either the step lands first and Abort's cut comes after it, or Abort
   lands first and the step sees a stale generation and does nothing.
   The price is that device calls must return promptly (DeviceOutputs
   says so, and a slow one is written down as a fault).

3. ONE record of what was actually sent, `_applied`, updated in the same
   locked step as the call. Effects do not replay a fixed script; they
   drive each output from what was actually applied toward the look the
   latest request wants (reconcile). So a Resume that supersedes a Hold
   half way through undoes exactly the steps the Hold got to, no more and
   no fewer, and an Abort after a Hold does not blank lasers that are
   already dark. A call that fails, raises, or returns something that is
   not a Result leaves that output UNKNOWN, which never counts as done, so
   the next effect that wants it dark sends the command again.

Abort's flame cut does not wait for the executor at all: abort() bumps the
generation and zeroes and disarms the flames on the calling thread, inside
`_lock`. The worst it can wait for is one device call already in progress.
A second Abort while latched does nothing: no new generation, no second
fade.
"""
import threading
import time
from collections import namedtuple

# Seconds, from the handoff. The selftest pins every one of these.
HOLD_FADE_S = 0.25        # production Hold: music fade, then freeze
ABORT_FADE_S = 1.0        # Abort: lasers, video, pixels, music together
ANNOUNCE_FADE_S = 0.25    # an announcement's fade to black. The handoff
                          # gives no number; the Hold's own fade is used.
ANNOUNCE_DARK_S = 0.5     # "waits about 0.5 s in the dark"
CONFIRM_S = 0.5           # beyond the fade, how long the clock has to say
                          # it froze (Hold) or is moving again (Resume)
POLL_S = 0.02             # how often the clock is asked while waiting
SLOW_CALL_S = 0.1         # a device call slower than this is a fault

PRODUCTION = "production"
REHEARSAL = "rehearsal"
MODES = (PRODUCTION, REHEARSAL)

# Looks: what the rig should show once an effect finishes.
PLAYING = "PLAYING"       # everything live
HELD = "HELD"             # production Hold: flames zero, lasers blanked,
                          # music frozen, video and pixels frozen in view
DARK = "DARK"             # announcement: HELD, and video and pixels black
ABORTED = "ABORTED"       # flames zero and disarmed, all faded, stopped
BETWEEN = "BETWEEN"       # out of the show (intermission, preshow,
                          # closing): flame cues zero, lasers blanked;
                          # music, video and pixels are the scheduler's
HOLDING_LOOKS = (HELD, DARK)

# What each output was last told. UNKNOWN is never "done".
UNKNOWN = "unknown"
LIT, BLACK, STOPPED = "lit", "black", "stopped"
LIVE, ZERO = "live", "zero"
MUSIC_PLAYING, MUSIC_HELD, MUSIC_STOPPED = "playing", "held", "stopped"

# Scheduler states in which the lasers may be lit (schedule.py's names),
# plus the rehearsal page's own. Anything else, None included, is dark.
LASER_STATES = frozenset(("SHOW", "PAUSED", "REHEARSAL"))


Result = namedtuple("Result", "ok sentence")
Result.__doc__ = """What every output call returns. `ok` False means the
command may not have landed; `sentence` says what happened, in words an
operator can read, and is written to the journal."""


def done(sentence=""):
    return Result(True, sentence)


def failed(sentence):
    return Result(False, sentence)


class DeviceOutputs:
    """The lasers (BEYOND) and the video (MadMapper): what PR #17's device
    layer must provide for the conductor. This class is the interface; the
    real one subclasses it (or duck-types it) in the integration step.

    Every method MUST:
      - return a Result, never raise. A device error (a socket that will not
        open, a send that fails) comes back as failed("sentence"). The
        conductor survives a raise or a non-Result anyway, and treats either
        as a failure with a fault line, but that is a bug in the device layer.
      - return promptly, well under SLOW_CALL_S (0.1 s). A fade is STARTED
        and runs on the device layer's own thread; the method never sleeps
        through it. (madmapper.Link: pass wait=False. beyond.Beyond's 3
        retries 20 ms apart are fine.) The conductor holds its lock across
        each call, so a slow call delays an Abort; it is journaled as a fault.
      - let the latest command to an output supersede any fade still running
        on it (madmapper.Link's ramp generation already does this).
      - be safe to repeat: the conductor re-sends after a failure.

    Nothing here decides WHEN; the conductor does. Nothing here may light the
    lasers except lasers_restore(), and the conductor only calls that after
    its laser gate says yes."""

    wired = True

    def lasers_blank(self):
        """Lasers dark at once (BEYOND brightness 0; never BlackOut, never
        MasterPause). Hold, and an instant rehearsal announcement."""
        raise NotImplementedError

    def lasers_fade_out(self, seconds):
        """Ramp BEYOND's brightness to 0 over `seconds` (Abort: 1 s, Jeff
        2026-09-27; an announcement: 0.25 s). NOTE for #17: beyond.Beyond
        today sends only 0 or 100 and refuses anything between, so this
        needs a ramp (or, until Andy approves one, a blank, reported in the
        Result's sentence so the journal says it was not a fade)."""
        raise NotImplementedError

    def lasers_restore(self):
        """BEYOND brightness back to the show level (100)."""
        raise NotImplementedError

    def video_fade_out(self, seconds):
        """Every MadMapper surface's opacity to 0 over `seconds` (0 means at
        once). madmapper.Link.fade_surfaces(1, 0, seconds, wait=False)."""
        raise NotImplementedError

    def video_restore(self, seconds):
        """Opacity back to 1 over `seconds` (0 means at once)."""
        raise NotImplementedError

    def video_stop(self):
        """Stop the show bank's conductor (after Abort's fade)."""
        raise NotImplementedError


class NotWiredDevices(DeviceOutputs):
    """The stand-in until PR #17 is integrated: sends nothing, and says so.
    `wired` is False, so a Conductor built with it writes a fault line at
    start and shows it on its snapshot: this must never be mistaken for a
    rig that blanks its lasers."""

    wired = False

    def _nothing(self, what):
        return done(f"{what} was not sent: the lasers and video are not "
                    f"connected in this build.")

    def lasers_blank(self):
        return self._nothing("Laser blank")

    def lasers_fade_out(self, seconds):
        return self._nothing("Laser fade")

    def lasers_restore(self):
        return self._nothing("Laser restore")

    def video_fade_out(self, seconds):
        return self._nothing("Video fade")

    def video_restore(self, seconds):
        return self._nothing("Video restore")

    def video_stop(self):
        return self._nothing("Video stop")


class ShowOutputs:
    """ltcplay's own side: the show clock (clock.AudioMaster), the pixels
    and the flame cue frames to the safety program. The same rules as
    DeviceOutputs: return a Result, never raise, return promptly.

    Integration notes, all in this repo:
      - music_hold(fade_s) is AudioMaster.pause(): the music fades over
        fade_s and the timecode freezes on the frame where the fade ends.
        fade_s 0 (rehearsal) needs AudioMaster.pause() to take a per call
        fade; today it always uses clock.audio.hold_fade_ms.
      - music_frozen() is True only once the clock HAS frozen (the moment
        AudioMaster calls on_pause), not from the request:
        AudioMaster.paused is already True during the fade, so use its
        _paused, or on_pause/on_resume. It turns False again once the audio
        is heard moving after music_resume().
      - flames_disarm_all() needs a message to the safety program that the
        link contract (flamesafe/CONTRACT.md, version 2) does not have yet.
        Until it does, an Abort from the Stream Deck disarms (the safety
        program owns the deck) but one from the screen only zeroes the cues."""

    def playing(self):
        """True while a show cue is loaded, running or held."""
        raise NotImplementedError

    def music_hold(self, fade_s):
        raise NotImplementedError

    def music_resume(self, fade_s):
        raise NotImplementedError

    def music_halt(self, fade_s):
        """Abort: the music fades over fade_s, then the cue stops."""
        raise NotImplementedError

    def music_frozen(self):
        raise NotImplementedError

    def pixels_fade_out(self, seconds):
        raise NotImplementedError

    def pixels_restore(self, seconds):
        raise NotImplementedError

    def flames_zero(self):
        """Every flame cue channel ltcplay sends to the safety program is
        zero from the next frame on, until flames_release()."""
        raise NotImplementedError

    def flames_release(self):
        raise NotImplementedError

    def flames_disarm_all(self, reason):
        raise NotImplementedError


def laser_gate_for(state_fn):
    """A laser gate from a function returning the show's state name (the
    scheduler's, or "REHEARSAL"). Lit only in LASER_STATES; anything else,
    None, or an error keeps the lasers dark, with the reason."""
    def gate():
        try:
            state = state_fn()
        except Exception as e:
            return (f"the show state could not be read ({type(e).__name__}: "
                    f"{e}), so the lasers stay dark.")
        if state in LASER_STATES:
            return None
        if state in ("STANDBY", "HOLD"):
            return (f"the show is in intermission ({state}), and there are "
                    f"no lasers during intermission, so they stay dark.")
        return f"the show state is {state or 'unknown'}, so the lasers " \
               f"stay dark."
    return gate


class _Superseded(Exception):
    """A newer request took over. Never escapes the executor."""


class Conductor:
    """One show's Hold, Resume, Abort, Reset and announcements.

    Every request returns a Result at once and never raises. The work runs
    on the executor (a thread, or run_pending() when `threaded` is False,
    which is how the selftest drives it with a fake clock).

    devices     DeviceOutputs (PR #17's, or NotWiredDevices)
    show        ShowOutputs
    laser_gate  () -> None to allow the lasers, or a sentence why not.
                Required: there is no default that could light them.
    hold_gate   schedule_service.Service.hold_for_announcement:
                (who, screen, detail) -> (refusal or None, epoch).
    announcer   announce.AnnounceService.play: (ann_id, who, screen), raises
                ValueError with a sentence when refused.
    journal     (text, **fields) -> anything. Its failures never stop a show.
    clock       seconds, monotonic (perf_counter, as clock.py uses).
    waiter      (timeout) -> None; replaces the condition wait. Test only.
    """

    def __init__(self, devices, show, laser_gate, hold_gate=None,
                 announcer=None, journal=None, clock=time.perf_counter,
                 waiter=None, threaded=True):
        if laser_gate is None or not callable(laser_gate):
            raise ValueError("The conductor needs a laser gate: without one "
                             "nothing could keep the lasers dark during "
                             "intermission.")
        self.devices = devices
        self.show = show
        self.laser_gate = laser_gate
        self.hold_gate = hold_gate
        self.announcer = announcer
        self._journal = journal
        self._clock = clock
        self._waiter = waiter
        self.threaded = threaded
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._gen = 0
        self._done_gen = 0
        self._want = None          # the latest accepted request
        self._look = None          # the look it asked for
        self._latched = False
        self._latch_who = ""
        self._mode = PRODUCTION
        self._announcing = None    # an announcement not yet started
        self._applied = {"flames": UNKNOWN, "lasers": UNKNOWN,
                         "video": UNKNOWN, "pixels": UNKNOWN,
                         "music": UNKNOWN, "disarmed": False}
        self.faults = 0
        self.journal_errors = 0
        self.lines = []            # the last few lines, for the page
        self._closed = False
        self._thread = None
        self.wired = bool(getattr(devices, "wired", True))
        if not self.wired:
            self._note("The lasers and video are not connected in this "
                       "build: Hold, Resume and Abort do not reach BEYOND or "
                       "MadMapper.", fault=True, action="devices")
        if threaded:
            self._thread = threading.Thread(target=self._executor,
                                            name="ltcplay-conductor",
                                            daemon=True)
            self._thread.start()

    # -- requests: each returns a Result at once ----------------------------
    def hold(self, who="", screen=""):
        with self._lock:
            refused = self._refuse_if_latched("Hold")
            if refused:
                return refused
            if self._look in HOLDING_LOOKS:
                return done("Already on hold. Nothing was changed.")
            if not self._playing():
                return done("Nothing is playing, so the rig was left as it "
                            "is.")
            fade = self._fade(HOLD_FADE_S)
            self._accept("Hold", HELD, who, screen, fade_s=fade)
            return done(f"Hold: flames to zero and lasers blanked, then the "
                        f"music fades over {fade:g} s and the show freezes.")

    def resume(self, who="", screen=""):
        with self._lock:
            refused = self._refuse_if_latched("Resume")
            if refused:
                return refused
            if self._look not in HOLDING_LOOKS:
                return self._refused("Resume", "the show is not on hold, so "
                                     "there is nothing to resume.")
            if not self._playing():
                return self._refused("Resume", "no show is loaded to "
                                     "resume.")
            fade = self._fade(HOLD_FADE_S)
            self._accept("Resume", PLAYING, who, screen, fade_s=fade,
                         resume=True)
            return done("Resume: the music fades back in; lasers and flame "
                        "cues come back once the timecode is moving.")

    def show_starting(self, who="", screen=""):
        """A show cue has just started: bring back whatever a previous Abort
        or announcement left dark (lasers only through the gate)."""
        with self._lock:
            refused = self._refuse_if_latched("Show start")
            if refused:
                return refused
            if not self._playing():
                return self._refused("Show start", "no show cue is playing.")
            # A fact, not a command: the caller has just started the cue, so
            # a later Hold pauses it even after an Abort marked it stopped.
            self._applied["music"] = MUSIC_PLAYING
            self._accept("Show start", PLAYING, who, screen, fade_s=0.0,
                         resume=False)
            return done("The rig comes up for the show.")

    def intermission(self, who="", screen=""):
        """The show has been left (intermission, preshow, closing): flame
        cues to zero and the lasers blanked with a real command, so "no
        lasers during intermission" does not depend on how the last show
        ended. For the scheduler to call on leaving SHOW or PAUSED. While
        aborted it changes nothing: the rig is already dark, and a new
        request must not cut the Abort's fade short."""
        with self._lock:
            if self._latched:
                return done("The show is aborted, so the rig is already "
                            "dark.")
            self._accept("Intermission", BETWEEN, who, screen, fade_s=0.0)
            return done("Out of the show: flame cues zero, lasers dark.")

    def abort(self, who="", screen=""):
        with self._lock:
            if self._latched:
                # Idempotent: no new generation, no second fade.
                return done("Already aborted. Press Reset to carry on.")
            if not self._playing():
                return self._refused("Abort", "nothing is playing, so there "
                                     "is nothing to abort.")
            self._latched = True
            self._latch_who = who
            # The flames do not wait for the executor, or even for the
            # journal line: cut them here, on the pressing thread, inside
            # the lock, so no stale step can land between the cut and the
            # new generation that makes every older step stale.
            self._flames_cut()
            self._accept("Abort", ABORTED, who, screen, fade_s=ABORT_FADE_S)
            return done(f"Abort: flames zeroed and disarm sent; lasers, "
                        f"video, pixels and music fade to black over "
                        f"{ABORT_FADE_S:g} s. Press Reset to carry on.")

    def reset(self, who="", screen=""):
        with self._lock:
            if not self._latched:
                return self._refused("Reset", "nothing is aborted.")
            if self._want is not None and self._want["look"] == ABORTED \
                    and self._done_gen != self._gen:
                return self._refused(
                    "Reset", "the Abort is still fading to black. Press "
                    "Reset again once it is dark.")
            self._latched = False
            self._note(f"{self._who(who, screen)} pressed Reset. The rig "
                       f"stays dark; flames stay disarmed until each group "
                       f"is armed again.", action="reset", who=who,
                       screen=screen)
            return done("Reset. The rig stays dark until a show starts.")

    def announce(self, ann_id, who="", screen=""):
        with self._lock:
            refused = self._refuse_if_latched("The announcement")
            if refused:
                return refused
            if self._announcing is not None:
                return self._refused("The announcement", "another "
                                     "announcement is still starting. Only "
                                     "one plays at a time.")
            if self.hold_gate is None or self.announcer is None:
                return self._refused("The announcement", "no scheduler or "
                                     "announcement player is connected.")
        # The gate takes the scheduler's own lock and may journal, so it is
        # called outside this one: a scheduler tick must never wait on an
        # Abort, or an Abort on a scheduler tick.
        try:
            refusal, _epoch = self.hold_gate(
                who, screen, f"played the {ann_id} announcement")
        except Exception as e:
            refusal = f"the Hold could not be asked for ({type(e).__name__}:" \
                      f" {e})"
        with self._lock:
            if refusal:
                return self._refused("The announcement", refusal)
            refused = self._refuse_if_latched("The announcement")
            if refused:
                return refused
            if self._announcing is not None:
                return self._refused("The announcement", "another "
                                     "announcement is still starting.")
            ann = (ann_id, who, screen)
            if self._playing():
                look = DARK
            else:
                # Between shows the Hold only delays the next show: the rig
                # keeps the look it already had (re-reconciling it sends
                # nothing already sent), and the announcement plays at once.
                look = self._look
            self._announcing = ann
            self._accept("Announcement", look, who, screen,
                         fade_s=self._fade(ANNOUNCE_FADE_S), announce=ann)
            return done("The show holds and goes dark, then the "
                        "announcement plays." if look == DARK else
                        "The announcement plays.")

    def set_mode(self, mode):
        with self._lock:
            if mode not in MODES:
                return self._refused("The mode change", f"{mode!r} is not "
                                     f"one of {', '.join(MODES)}.")
            refused = self._refuse_if_latched("The mode change")
            if refused:
                return refused
            self._mode = mode
            return done(f"Mode: {mode}.")

    # -- reading ---------------------------------------------------------------
    @property
    def latched(self):
        return self._latched

    @property
    def mode(self):
        return self._mode

    def snapshot(self):
        with self._lock:
            return {"look": self._look, "latched": self._latched,
                    "busy": self._done_gen != self._gen,
                    "mode": self._mode, "applied": dict(self._applied),
                    "devices_wired": self.wired, "faults": self.faults,
                    "lines": list(self.lines[-20:])}

    def wait_idle(self, timeout=5.0):
        """True once the latest request has finished. Threaded only."""
        with self._lock:
            return self._cv.wait_for(lambda: self._done_gen == self._gen,
                                     timeout)

    def run_pending(self):
        """Not threaded: run effects on this thread until none is due."""
        while True:
            with self._lock:
                if self._done_gen == self._gen or self._want is None:
                    return
                gen, want = self._gen, self._want
            self._run(gen, want)

    def close(self):
        with self._lock:
            self._closed = True
            self._cv.notify_all()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(2.0)

    # -- request helpers, called with the lock held ---------------------------
    def _playing(self):
        r = self._ask("Asking whether a show is playing", self.show.playing)
        return r is True

    def _fade(self, production_s):
        return 0.0 if self._mode == REHEARSAL else production_s

    def _refuse_if_latched(self, what):
        if self._latched:
            return self._refused(what, "the show is aborted. Press Reset "
                                 "first.")
        return None

    def _refused(self, what, why):
        return failed(f"{what} was refused: {why}")

    def _accept(self, label, look, who, screen, **params):
        if self._announcing is not None and \
                params.get("announce") is not self._announcing:
            # An announcement accepted but not yet started never plays once
            # anything newer is pressed, whether or not its effect ran.
            self._note(f"The {self._announcing[0]} announcement did not "
                       f"play: {label} came first.", action="announce",
                       outcome="cancelled")
            self._announcing = None
        self._gen += 1
        want = {"label": label, "look": look, "who": who, "screen": screen}
        want.update(params)
        self._want = want
        self._look = look
        self._note(f"{label}, asked for by {self._who(who, screen)}.",
                   action=label.lower(), who=who, screen=screen)
        self._cv.notify_all()

    @staticmethod
    def _who(who, screen):
        return (who or "Someone") + (f" on the {screen}" if screen else "")

    # -- the executor ------------------------------------------------------------
    def _executor(self):
        while True:
            with self._lock:
                while not self._closed and (self._want is None or
                                            self._done_gen == self._gen):
                    self._cv.wait()
                if self._closed:
                    return
                gen, want = self._gen, self._want
            self._run(gen, want)

    def _run(self, gen, want):
        progress = []
        try:
            look = want["look"]
            if look == ABORTED:
                self._run_abort(gen, want, progress)
            elif look in HOLDING_LOOKS:
                self._run_dark(gen, want, progress)
            elif look == PLAYING:
                self._run_up(gen, want, progress)
            elif look == BETWEEN:
                self._run_between(gen, want, progress)
            if want.get("announce"):
                self._run_announce(gen, want, progress)
        except _Superseded:
            with self._lock:
                newer = self._want["label"] if self._want else "a newer press"
            self._note(f"{want['label']} stopped part way because {newer} "
                       f"came in. Done before it stopped: "
                       f"{', '.join(progress) or 'nothing'}.",
                       action=want["label"].lower(), outcome="superseded")
            return
        except Exception as e:           # a bug here must not kill the rig
            self._note(f"{want['label']} failed inside the conductor "
                       f"({type(e).__name__}: {e}). That is a bug in "
                       f"ltcplay. Done before it failed: "
                       f"{', '.join(progress) or 'nothing'}.", fault=True,
                       action=want["label"].lower(), outcome="error")
        with self._lock:
            if self._gen == gen:
                self._done_gen = gen
                self._cv.notify_all()

    # -- the effects -------------------------------------------------------------
    def _run_dark(self, gen, want, progress):
        """HELD or DARK: flames, lasers, then the music fade, the video and
        pixel fades, and the wait for the clock to freeze."""
        look, fade = want["look"], want["fade_s"]
        a = self._applied
        self._step(gen, "flames", ZERO, "flame cues zeroed", progress,
                   self.show.flames_zero)
        if look == DARK and fade > 0:
            self._step(gen, "lasers", BLACK, "lasers faded", progress,
                       self.devices.lasers_fade_out, fade)
        else:
            self._step(gen, "lasers", BLACK, "lasers blanked", progress,
                       self.devices.lasers_blank)
        froze = a["music"] in (MUSIC_PLAYING, UNKNOWN)
        self._step(gen, "music", MUSIC_HELD, "music fading", progress,
                   self.show.music_hold, fade, only_from=(MUSIC_PLAYING,
                                                          UNKNOWN))
        faded = False
        if look == DARK:
            faded |= self._step(gen, "video", BLACK, "video faded", progress,
                                self.devices.video_fade_out, fade)
            faded |= self._step(gen, "pixels", BLACK, "pixels faded",
                                progress, self.show.pixels_fade_out, fade)
        if froze:
            if not self._await(gen, lambda: self.show.music_frozen() is True,
                               fade + CONFIRM_S):
                with self._lock:
                    self._check(gen)
                    a["music"] = UNKNOWN
                self._note(f"The show clock did not say it had frozen within "
                           f"{fade + CONFIRM_S:g} s of the Hold. Flame cues "
                           f"are zero and the lasers are dark; the music and "
                           f"timecode may still be moving.", fault=True,
                           action="hold", outcome="unconfirmed")
            else:
                progress.append("show frozen")
        elif faded:
            self._pause(gen, fade)
        want["changed"] = bool(progress)

    def _run_up(self, gen, want, progress):
        """PLAYING: music back, video and pixels up, then (once the timecode
        moves) the lasers through the gate and the flame cues last."""
        fade, a = want["fade_s"], self._applied
        if want.get("resume"):
            self._step(gen, "music", MUSIC_PLAYING, "music fading in",
                       progress, self.show.music_resume, fade,
                       only_from=(MUSIC_HELD, UNKNOWN))
        self._step(gen, "video", LIT, "video up", progress,
                   self.devices.video_restore, fade)
        self._step(gen, "pixels", LIT, "pixels up", progress,
                   self.show.pixels_restore, fade)
        if want.get("resume"):
            if not self._await(gen, lambda: self.show.music_frozen() is False,
                               fade + CONFIRM_S):
                with self._lock:
                    self._check(gen)
                    a["music"] = UNKNOWN
                self._note(f"The show clock did not say the timecode was "
                           f"moving within {fade + CONFIRM_S:g} s of Resume, "
                           f"so the lasers stay dark and the flame cues stay "
                           f"at zero. Press Hold, then Resume, to try again.",
                           fault=True, action="resume",
                           outcome="unconfirmed")
                return
            progress.append("timecode moving")
        self._restore_lasers(gen, progress)
        self._step(gen, "flames", LIVE, "flame cues released", progress,
                   self.show.flames_release)

    def _run_between(self, gen, want, progress):
        """BETWEEN: flame cues zero, lasers blanked. Nothing else."""
        self._step(gen, "flames", ZERO, "flame cues zeroed", progress,
                   self.show.flames_zero)
        self._step(gen, "lasers", BLACK, "lasers blanked for intermission",
                   progress, self.devices.lasers_blank)

    def _run_abort(self, gen, want, progress):
        fade = want["fade_s"]
        # Normally already done on the pressing thread; again only if that
        # failed (UNKNOWN).
        self._step(gen, "flames", ZERO, "flame cues zeroed", progress,
                   self.show.flames_zero)
        with self._lock:
            self._check(gen)
            if not self._applied["disarmed"]:
                self._disarm()
        faded = False
        faded |= self._step(gen, "lasers", BLACK, "lasers faded", progress,
                            self.devices.lasers_fade_out, fade)
        faded |= self._step(gen, "video", BLACK, "video faded", progress,
                            self.devices.video_fade_out, fade,
                            only_from=(LIT, UNKNOWN))
        faded |= self._step(gen, "pixels", BLACK, "pixels faded", progress,
                            self.show.pixels_fade_out, fade)
        faded |= self._step(gen, "music", MUSIC_STOPPED, "music faded",
                            progress, self.show.music_halt, fade)
        if faded:
            self._pause(gen, fade)
        self._step(gen, "video", STOPPED, "video stopped", progress,
                   self.devices.video_stop)
        if progress:
            self._note(f"Abort finished: {', '.join(progress)}. Latched "
                       f"until Reset.", action="abort", outcome="done")

    def _run_announce(self, gen, want, progress):
        ann_id, who, screen = want["announce"]
        if want["look"] == DARK and want.get("changed"):
            self._pause(gen, ANNOUNCE_DARK_S)
        with self._lock:
            # The commit point: past here the announcement plays, even if
            # an Abort comes in while its file is read (announcement audio
            # is not a hazard, and it has its own Stop).
            self._check(gen)
            self._announcing = None
        try:
            self.announcer(ann_id, who, screen)
        except Exception as e:
            self._note(f"The {ann_id} announcement did not play: {e}",
                       fault=not isinstance(e, ValueError),
                       action="announce", outcome="refused")
            return
        progress.append("announcement playing")

    def _restore_lasers(self, gen, progress):
        """The ONLY way the lasers are lit. The gate says no, raises, or
        says anything but None: they are made dark (blanked, unless already
        known to be), and the journal says why. Dark is enforced here, not
        assumed: lasers left lit by an earlier look are blanked too.
        The gate is asked OUTSIDE the conductor's lock: it may take the
        scheduler's, and the scheduler may call in here with its own held.
        _step re-checks the generation before the call."""
        with self._lock:
            self._check(gen)
        try:
            why = self.laser_gate()
        except Exception as e:
            why = f"the laser gate failed ({type(e).__name__}: {e})"
        if why is not None:
            self._note(f"The lasers stay dark: {why}", action="lasers",
                       outcome="refused")
            self._step(gen, "lasers", BLACK, "lasers blanked", progress,
                       self.devices.lasers_blank)
            return
        self._step(gen, "lasers", LIT, "lasers back", progress,
                   self.devices.lasers_restore)

    # -- steps and waits ---------------------------------------------------------
    def _check(self, gen):
        if self._gen != gen or self._closed:
            raise _Superseded()

    def _step(self, gen, output, value, label, progress, fn, *args,
              only_from=None):
        """Check the generation and make one call, as one locked step.
        Returns True if a command was sent. Skips an output already at
        `value` (UNKNOWN never is), or not in `only_from` when given."""
        with self._lock:
            self._check(gen)
            now = self._applied[output]
            if now == value:
                return False
            if only_from is not None and now not in only_from:
                return False
            r = self._call(label, fn, *args)
            self._applied[output] = value if r.ok else UNKNOWN
        progress.append(label if r.ok else f"{label} (FAILED)")
        return True

    def _call(self, label, fn, *args):
        """One output call. Never raises; anything but a good Result is a
        failure, written down as a fault with its sentence."""
        t0 = self._clock()
        try:
            r = fn(*args)
        except Exception as e:
            r = failed(f"{label}: {type(e).__name__}: {e}")
        if not isinstance(r, Result):
            r = failed(f"{label}: the output returned {r!r}, not a Result, "
                       f"so it counts as not done.")
        took = self._clock() - t0
        if took > SLOW_CALL_S:
            self._note(f"{label} took {took * 1000:.0f} ms. Output calls "
                       f"must return at once; a slow one delays Abort.",
                       fault=True, action="output", outcome="slow")
        if not r.ok:
            self._note(f"Not done: {r.sentence}", fault=True,
                       action="output", outcome="failed")
        return r

    def _ask(self, label, fn):
        try:
            return fn()
        except Exception as e:
            self._note(f"{label} failed ({type(e).__name__}: {e}).",
                       fault=True, action="output", outcome="failed")
            return None

    def _flames_cut(self):
        """Abort's instant half, called with the lock held."""
        r = self._call("flame cues zeroed", self.show.flames_zero)
        self._applied["flames"] = ZERO if r.ok else UNKNOWN
        self._disarm()

    def _disarm(self):
        r = self._call("disarm every flame group",
                       self.show.flames_disarm_all, "Abort")
        self._applied["disarmed"] = r.ok

    def _pause(self, gen, seconds):
        """Wait, waking at once for a newer request, which abandons this
        effect."""
        end = self._clock() + seconds
        with self._lock:
            while True:
                self._check(gen)
                left = end - self._clock()
                if left <= 0:
                    return
                self._wait(left)

    def _await(self, gen, predicate, timeout):
        """Wait until predicate() or timeout; False on timeout."""
        end = self._clock() + timeout
        while True:
            with self._lock:
                # No generation check here: _pause below makes one before
                # every wait, and every step after this makes its own.
                try:
                    if predicate():
                        return True
                except Exception as e:
                    self._note(f"Asking the show clock failed "
                               f"({type(e).__name__}: {e}).", fault=True,
                               action="output", outcome="failed")
                if self._clock() >= end:
                    return False
            self._pause(gen, min(POLL_S, max(end - self._clock(), 0.0)))

    def _wait(self, left):
        if self._waiter is not None:
            self._waiter(left)
        else:
            self._cv.wait(left)

    # -- the journal ---------------------------------------------------------------
    def _note(self, text, fault=False, **fields):
        with self._lock:           # re-entrant; callers may hold it
            if fault:
                self.faults += 1
            self.lines.append(text)
            del self.lines[:-100]
        if self._journal is None:
            return
        try:
            self._journal(text, fault=fault, **fields)
        except Exception:
            self.journal_errors += 1
