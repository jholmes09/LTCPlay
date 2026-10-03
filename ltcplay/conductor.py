"""The show conductor: the one place Hold, Resume, Abort and an announcement
are carried out on the rig, fire, lasers, video, pixels and music together.

Imported ONLY by code that runs the Fire & Ice show. The GPL show at
Dollywood never reaches it (test_the_gpl_path_never_loads_the_conductor).
In this build nothing constructs a Conductor outside the selftest: the
scheduler still runs dry (schedule_service.DRY_RUN) and ShowOutputs is not
built yet. The real laser and video side is ConductorDevices, below: PR
#17's beyond.py and madmapper.py objects, handed in already built, wired to
DeviceOutputs one primitive per method. devices.py's module docstring says
why it does not call devices.on_hold()/on_resume()/on_abort().

What it does, from the handoff and Jeff's decisions
===================================================

Abort (sections 4a and 5, Jeff 2026-09-26 and 2026-09-27): the software
E-stop. Every flame cue channel goes to zero and every flame group is asked
to disarm INSTANTLY, on the pressing thread, before this call returns. The
lasers (a BEYOND brightness ramp, not an instant blank), video, pixels and
music then fade to black TOGETHER over 1 s, and the video stops. It LATCHES:
nothing else is accepted until one Reset press. Only while something plays.

A show cut short by ltcplay restarting: show_stopped(). The same fade to
black, but no disarm and no latch, so the rig stays dark only until Start
now or the next show (Jeff's rule).

A show that failed to start (no timecode within the confirm window):
failed_start(). show_stopped()'s dark, and every flame group disarmed at
once, the same disarm-all an Abort sends, on the calling thread (Jeff,
2026-10-03: "disarm the flame units while we are troubleshooting"). It does
NOT latch: no Reset, and Start now stays allowed; flames fire again only
once each group is re-armed by hand, off then on, on the deck.

Hold, production (section 5, Jeff 2026-09-27): flames to zero and the lasers
blanked by a real command (a frozen laser cue is a static beam), then the
music fades over 0.25 s and the clock freezes on the frame where the fade
ends. That fade then freeze is clock.py's own (AudioMaster.pause); this
module asks for it and waits for the clock to say it froze. Video and pixels
fade to black together with the lasers, the same as an announcement and an
Abort (Jeff 2026-09-30, resolving the handoff's open question).
Rehearsal (Jeff 2026-09-27, video/pixels decided 2026-09-30): the same Hold
with no fade, a mode flag, not a second code path: video and pixels freeze
in place on the current frame instead of fading to black.

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
   no fewer, and an Abort after a Hold does not fade video that is already
   black. A call that fails, raises, or returns something that is not a
   Result leaves that output UNKNOWN, which never counts as done, so the
   next effect that wants it dark sends the command again. The one
   exception is the lasers' dark command, which is sent every time a look
   wants them dark, whatever the record says (ALWAYS_RESENT): BEYOND never
   confirms anything, so "already dark" is never taken on trust.

Abort's flame cut AND laser blank do not wait for the executor at all
(independent review of PR #29, finding D): abort() bumps the generation,
zeroes and disarms the flames and stops any video fade where it is, all
inside `_lock`, and then blanks the lasers on the calling thread, outside
it. Only ltcplay's own outputs (flames, music, pixels: ShowOutputs) and
MadMapper's queueing calls (which never wait for MadMapper) are made with
`_lock` held. BEYOND is called OUTSIDE it, so a slow or stalled BEYOND
socket never holds up an Abort's flame cut, and a laser restore cannot
outrun an Abort's blank: it is cut short by the conductor's restore guard
(the restore stops before its next packet once a newer request has been
accepted, and the guard turns false before the Abort's blank is sent) and
by beyond.Beyond itself (a blank stops an unblank that has already
started counting blanks before its next packet; see that class's
docstring for the one window only the restore guard closes). The laser gate and the announcement player are asked on
helper threads, never on the executor's, so neither can delay an Abort
either. A second Abort while latched starts no new generation and no
second fade, but sends the laser blank again (finding B): a blank only
makes things darker.

MadMapper's sends happen later, on its Link's worker (wait=False). Their
real outcome comes back through ConductorDevices' report (finding A): a
send that fails, or a worker that has not finished in time, is a fault,
and the video record goes to UNKNOWN. The record only says black, lit or
stopped once MadMapper's Link says the packets went out. Abort never
trusts it anyway: it always fades the video from wherever it is now
(finding C).
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
GATE_TIMEOUT_S = 1.0      # a laser gate that has not answered by then is
                          # a no: the lasers stay dark
VIDEO_STALL_S = 1.0       # MadMapper's worker not done this long after a
                          # command's own length: a fault, video UNKNOWN

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
STOPPED_DARK = "STOPPED_DARK"  # a show that did not start, or was cut by a
                          # restart: everything dark like ABORTED, but no
                          # latch (show_stopped; failed_start also disarms)
HOLDING_LOOKS = (HELD, DARK)

# What each output was last told. UNKNOWN is never "done".
UNKNOWN = "unknown"
LIT, BLACK, STOPPED = "lit", "black", "stopped"
LIVE, ZERO = "live", "zero"
MUSIC_PLAYING, MUSIC_HELD, MUSIC_STOPPED = "playing", "held", "stopped"

# Outputs whose "dark" command is sent EVERY time a look wants them dark,
# even when the record says the last one already landed. Only the lasers.
# BEYOND answers nothing (bench B8), so "applied" only ever means a packet
# left this machine, never that BEYOND acted on it, and someone at the
# BEYOND console may have brought brightness back up by hand since. This is
# devices.py's own rule ("never assume the earlier blank actually landed",
# PR #17), kept when the conductor took over the sequencing: a repeated
# blank costs about 40 ms and is never wrong; a skipped one could be.
# Lighting things (and every other output) still reconciles as before.
ALWAYS_RESENT = frozenset((("lasers", BLACK),))
AGAIN = "(re-sent, already dark)"

# Device outputs called WITHOUT the conductor's lock held: BEYOND's sends
# block (3 packets, 20 ms apart, or far longer on a stalled socket), and an
# Abort must never wait for one (finding D). A restore is still safe: see
# Conductor._restore_wanted and beyond.Beyond's blank epoch.
UNLOCKED_OUTPUTS = frozenset(("lasers",))

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
        retries 20 ms apart are fine.) A slow call is journaled as a fault.
        The laser calls are made WITHOUT the conductor's lock (an Abort
        must never wait for one); the video calls with it, so they must
        never wait for the device.
      - let the latest command to an output supersede any fade still running
        on it (madmapper.Link's ramp generation already does this).
      - be safe to repeat: the conductor re-sends after a failure.
      - be safe to call from two threads at once: Abort blanks the lasers
        on the pressing thread while the executor may be in another call.

    Optional, all set or read by the Conductor when present:
      async_outputs  outputs whose good Result only means "queued"; their
                     real outcome comes later through report(). The
                     conductor records them UNKNOWN until it does.
      report         set by the Conductor: report(output, ok, value,
                     sentence, seq). ok False is a fault and sets the
                     output UNKNOWN; ok True sets it to `value` (one of
                     this module's LIT, BLACK, STOPPED) if `seq` is still
                     the device's latest command for it (video_seq).
      video_seq      the number of the latest video command or cancel.
      restore_guard  set by the Conductor: () -> True while a laser
                     restore is still wanted. lasers_restore() checks it
                     before every packet and stops once it is not.
      video_cancel() stop any video fade at once, leaving the level where
                     it is (an Abort's or Hold's first video step).

    Nothing here decides WHEN; the conductor does. Nothing here may light the
    lasers except lasers_restore(), and the conductor only calls that after
    its laser gate says yes."""

    wired = True
    async_outputs = frozenset()
    report = None
    restore_guard = None
    video_seq = 0

    def video_cancel(self):
        return done("No video fade to stop.")

    def lasers_blank(self):
        """Lasers dark at once (BEYOND brightness 0; never BlackOut, never
        MasterPause). Hold, and an instant rehearsal announcement."""
        raise NotImplementedError

    def lasers_fade_out(self, seconds):
        """Ramp BEYOND's brightness to 0 over `seconds` (Abort: 1 s, Jeff
        2026-09-27; an announcement: 0.25 s). beyond.Beyond sends only 0
        or 100 and refuses anything between (its reviewed allow-list), so
        ConductorDevices (below) blanks at once instead and journals that
        it was not a fade. A real ramp needs its own reviewed change to
        beyond.py."""
        raise NotImplementedError

    def lasers_restore(self):
        """BEYOND brightness back to the show level (100)."""
        raise NotImplementedError

    def video_fade_out(self, seconds):
        """Every MadMapper surface's opacity to 0 over `seconds` (0 means at
        once), starting from wherever the opacity is now, never from a
        fixed 1.0 (finding C: a fade from 1.0 flashes the picture up
        first)."""
        raise NotImplementedError

    def video_restore(self, seconds):
        """Opacity back to 1 over `seconds` (0 means at once), from
        wherever it is now."""
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


def _device_note(journal, text, **fields):
    """ConductorDevices' own journal lines. Never raises."""
    if journal:
        try:
            journal(text, **fields)
        except Exception:
            pass

class ConductorDevices(DeviceOutputs):
    """The conductor's lasers (BEYOND) and video (MadMapper): conductor.py's
    DeviceOutputs, one primitive per method, no sequencing of its own.
    devices.py's module docstring says why this calls beyond.py and
    madmapper.py directly instead of devices.on_hold()/on_resume()/
    on_abort(), and where each of their ordering guarantees now lives.

    `madmapper` is an already-built madmapper.Link or None, `beyond` an
    already-built beyond.Beyond or None. This module imports neither (nor
    devices.py): it is handed the objects.
    `wired` is True only when both are given, so a Conductor built with a
    show that lacks either one writes its "not connected" fault line at
    start rather than passing for a rig whose lasers it blanks.

    Every method follows DeviceOutputs' rules: returns a conductor Result,
    never raises, and returns at once. Video fades are started with
    wait=False and run on the Link's own worker; a newer one supersedes
    the old (the Link's ramp generation). beyond.py's blank() and
    unblank() send 3 packets 20 ms apart and return after them, about
    40 ms, so the lasers are dark before the conductor's next call.

    Video outcomes come back later (finding A). Each video command gets a
    number (video_seq) and an on_done from the Link's worker: a failed
    send is reported at once (fault, video UNKNOWN), a good one only if it
    is still the latest command. A timer reports a worker that has not
    finished VIDEO_STALL_S after the command's own length.

    Every video fade starts from the current level (finding C): this
    object keeps each ramp's start level, end level, length and start
    time, and a new fade starts from the estimated level now, or from the
    level the Link last actually sent if that is further along the new
    fade's direction, so the picture never jumps the wrong way."""

    def __init__(self, madmapper=None, beyond=None, *, show=None,
                 journal=None, clock=None):
        self.mm = madmapper
        self.beyond = beyond
        self.show = show
        self._journal = journal
        # The Link's own clock, so the level estimate runs on the same time
        # as the ramps it estimates.
        self._clock = clock or getattr(madmapper, "_clock", None) or \
            time.perf_counter
        self.async_outputs = (frozenset(("video",)) if madmapper is not None
                              else frozenset())
        self._vlock = threading.RLock()
        self.video_seq = 0
        self._ramp = None            # (start, end, seconds, started at)
        self._open = set()           # video command numbers not yet done
        self.wired = madmapper is not None and beyond is not None
        if beyond is None:
            _device_note(journal, "No BEYOND is configured for this show: the "
                  "conductor's laser commands reach nothing.",
                  action="devices", outcome="not_configured", fault=True)
        if madmapper is None:
            _device_note(journal, "No MadMapper is configured for this show: the "
                  "conductor's video commands reach nothing.",
                  action="devices", outcome="not_configured", fault=True)

    # -- lasers: beyond.py ---------------------------------------------------
    _BLANK_FAILED = "The lasers may still be showing whatever they were."

    def _beyond(self, what, method, failed_means, **kw):
        if self.beyond is None:
            return done(f"{what}: no BEYOND is configured for this show.")
        try:
            ok = getattr(self.beyond, method)(show=self.show, **kw)
        except Exception as e:
            return failed(f"{what} failed: {type(e).__name__}: {e}. "
                          f"{failed_means}")
        if ok is True:
            return done(f"{what}: sent to BEYOND.")
        return failed(f"{what}: no packet got out to BEYOND. {failed_means}")

    def lasers_blank(self):
        return self._beyond("Laser blank", "blank", self._BLANK_FAILED)

    def lasers_fade_out(self, seconds):
        """NOT a fade: an instant blank, the same command as lasers_blank().
        beyond.py's allow-list only ever lets brightness 0.0 or 100.0 off
        the machine (a safety audit, S5), so there is no ramp to send.
        Going dark at once is never later than the asked-for fade would
        have been. Changing this to a real ramp is a laser-safety-relevant
        change to beyond.py that needs its own review; it is not made here.
        Journaled every time, so the record never says "faded" alone."""
        r = self._beyond("Laser blank", "blank", self._BLANK_FAILED)
        if self.beyond is not None and r.ok:
            _device_note(self._journal,
                  f"BEYOND was blanked at once, not faded over {seconds:g} "
                  f"s: beyond.py only allows brightness 0 or 100, and a "
                  f"brightness ramp has not been reviewed.",
                  action="lasers", outcome="blanked_not_faded")
        return r

    def lasers_restore(self):
        """The only unblank. in_show=True is the conductor's laser gate's
        answer: the conductor calls this only after the gate said yes
        (conductor.Conductor._restore_lasers), and beyond.unblank() still
        refuses anything but the real bool True on its own."""
        kw = {"in_show": True}
        if self.restore_guard is not None:
            # Checked by beyond.Beyond before every packet: a restore
            # overtaken by a newer request (an Abort above all) stops.
            kw["still_wanted"] = self.restore_guard
        r = self._beyond("Laser restore", "unblank",
                         "The lasers stay dark.", **kw)
        if not r.ok and getattr(self.beyond, "last_result", None) == "cut":
            return failed("Laser restore stopped part way: a newer request "
                          "came in first.")
        return r

    # -- video: madmapper.py -------------------------------------------------
    def _madmapper(self, what, send, value=None, seconds=0.0):
        """Queue one MadMapper command. `send(on_done)` queues it on the
        Link with that callback; `value` is what the conductor records
        once the Link says it went out. Returns at once."""
        if self.mm is None:
            return done(f"{what}: no MadMapper is configured for this show.")
        if getattr(self.mm, "_closed", False):
            return failed(f"{what}: the MadMapper link is closed, so nothing "
                          f"was sent.")
        with self._vlock:
            self.video_seq += 1
            seq = self.video_seq
            self._open.add(seq)
        timer = threading.Timer(max(seconds, 0.0) + self._behind() +
                                VIDEO_STALL_S,
                                self._video_stalled, (seq, what))
        timer.daemon = True
        timer.start()

        def on_done(ok, why):
            timer.cancel()
            self._video_done(seq, what, value, ok, why)
        try:
            send(on_done)
        except Exception as e:
            timer.cancel()
            with self._vlock:
                self._open.discard(seq)
            return failed(f"{what} failed: {type(e).__name__}: {e}.")
        return done(f"{what}: queued for MadMapper.")

    def _behind(self):
        """Seconds the running ramp still has to go: a command queued now
        runs after it on the Link's one worker."""
        with self._vlock:
            ramp = self._ramp
        if ramp is None:
            return 0.0
        _s, _e, seconds, t0 = ramp
        return max(0.0, t0 + seconds - self._clock())

    def _tell(self, ok, value, sentence, seq):
        if self.report is not None:
            try:
                self.report("video", ok, value, sentence, seq)
                return
            except Exception:
                pass
        if not ok:
            _device_note(self._journal, sentence, action="video",
                         outcome="failed", fault=True)

    def _video_done(self, seq, what, value, ok, why):
        """On the Link's worker, once the command has run."""
        with self._vlock:
            self._open.discard(seq)
        if not ok:
            why = why or "no reason given"
            self._tell(False, None, f"{what}: not sent to MadMapper ({why})."
                       f" The video may not be where the conductor asked.",
                       seq)
        elif value is not None:
            self._tell(True, value, f"{what}: sent to MadMapper.", seq)

    def _video_stalled(self, seq, what):
        """On a timer thread: the Link's worker has not run this command
        in time. Reported whatever happens to it later."""
        with self._vlock:
            if seq not in self._open:
                return
        self._tell(False, None, f"{what}: MadMapper's sender has not sent "
                   f"it yet, well past when it should have. It may be stuck; "
                   f"the video may not be where the conductor asked.", seq)

    def video_level(self):
        """The surfaces' opacity now, estimated from the last ramp asked
        for (its start, end, length and start time, and the show's video
        curve), or None before any."""
        with self._vlock:
            ramp = self._ramp
        if ramp is None:
            return None
        start, end, seconds, t0 = ramp
        if seconds <= 0 or start == end:
            return end
        frac = min(1.0, max(0.0, (self._clock() - t0) / seconds))
        curve = getattr(getattr(self.mm, "cfg", None), "video_curve",
                        "linear")
        if curve == "perceptual":
            # madmapper._shape_perceptual, by time rather than by step.
            if start > end:
                return end + (1.0 - frac) ** 2 * (start - end)
            return start + (1.0 - (1.0 - frac) ** 2) * (end - start)
        return start + (end - start) * frac

    def _from_level(self, end):
        """Where a new fade to `end` starts: the estimated level now, or the
        level the Link last actually sent, whichever is nearer `end`. So a
        fade down never starts above the picture, nor a fade up below it.
        Unknown (nothing sent yet): the far end, as before this fix."""
        levels = [v for v in (self.video_level(),
                              getattr(self.mm, "surfaces_level", None))
                  if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if not levels:
            return 1.0 if end <= 0.0 else 0.0
        return min(levels) if end <= 0.0 else max(levels)

    def _surfaces(self, end, seconds, on_done):
        # Stop any ramp still running first (it checks the Link's
        # generation before every step), then start from where it got to.
        # Everything is queued behind that ramp's job on the Link's single
        # worker, so it always lands after the ramp's last step.
        self.mm.cancel()
        start = self._from_level(end)
        with self._vlock:
            self._ramp = (start, end, max(seconds, 0.0), self._clock())
        if seconds <= 0 or start == end:
            # One level, at once, still cancellable by a newer command.
            self.mm.fade_surfaces(end, end, seconds=0.0, steps=1,
                                  wait=False, on_done=on_done)
        else:
            self.mm.fade_surfaces(start, end, seconds=seconds, wait=False,
                                  on_done=on_done)

    def video_cancel(self):
        """Stop any video fade at once, where it is (an Abort's or a Hold's
        first video step, on the pressing thread). Never waits."""
        if self.mm is None:
            return done("Video: no MadMapper is configured for this show.")
        try:
            self.mm.cancel()
        except Exception as e:
            return failed(f"Stopping the video fade failed: "
                          f"{type(e).__name__}: {e}.")
        level = self.video_level()
        with self._vlock:
            self.video_seq += 1      # no older command's success counts now
            if level is not None:
                self._ramp = (level, level, 0.0, self._clock())
        return done("Video fade stopped where it was.")

    def video_fade_out(self, seconds):
        return self._madmapper(
            f"Video fade to black over {seconds:g} s",
            lambda cb: self._surfaces(0.0, seconds, cb), BLACK, seconds)

    def video_restore(self, seconds):
        return self._madmapper(
            f"Video back up over {seconds:g} s",
            lambda cb: self._surfaces(1.0, seconds, cb), LIT, seconds)

    def video_stop(self):
        return self._madmapper(
            "Video stop",
            lambda cb: self.mm.stop_bank(self.mm.cfg.show_bank, wait=False,
                                         on_done=cb), STOPPED)


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
      - flames_disarm_all() is flamelink.FlameLink.disarm_all(reason): the
        contract's disarm_all message on the flame link (flamesafe/
        CONTRACT.md, "Disarm every group", 2026-10-02). flames_zero() is
        FlameLink.zero() and flames_release() is FlameLink.release(). Until
        a FlameLink is wired into fire_ice.FireIceShow, an Abort from the
        Stream Deck disarms (it owns the arm link) but one from the screen
        only zeroes the cues."""

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
        # Bumped on every write to an output's record, so a laser call made
        # outside the lock only records its outcome if nothing newer has.
        self._ver = dict.fromkeys(self._applied, 0)
        self._restore_gen = None     # the generation a laser restore is for
        self._async_seq = {}         # output -> the device's latest seq
        self._async = frozenset(getattr(devices, "async_outputs", ()) or ())
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
        # The device layer's ways back in (DeviceOutputs, "Optional").
        for name, fn in (("report", self._device_report),
                         ("restore_guard", self._restore_wanted)):
            if hasattr(devices, name):
                try:
                    setattr(devices, name, fn)
                except Exception:
                    pass
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
            held = self._look in HOLDING_LOOKS
            if not held:
                if not self._playing():
                    return done("Nothing is playing, so the rig was left as "
                                "it is.")
                fade = self._fade(HOLD_FADE_S)
                # Stop a video fade-up (a Resume's) where it is, now, so
                # the picture never rises after the press (finding C).
                self._video_cancel()
                self._accept("Hold", HELD, who, screen, fade_s=fade)
                return done(f"Hold: flames to zero and lasers blanked, then "
                            f"the music fades over {fade:g} s and the show "
                            f"freezes.")
        # Already on hold: nothing new starts, but the lasers are blanked
        # again, on this thread (finding B): a blank only makes it darker.
        return done(f"Already on hold. {self._reblank('Hold')}")

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

    def music_started(self):
        """A fact, not a command: the show cue has just been started on the
        show audio, before anything confirms the show (the scheduler tells
        show_starting() only on SHOW_CONFIRMED). From here a Hold freezes
        the music and an Abort or a failed start stops it, whatever an
        earlier Abort, stop or failed start recorded (PR #43 review,
        finding 4: an Abort or Hold before the show was confirmed left the
        music and timecode running, because the record still said
        stopped). Starts nothing and changes no look."""
        with self._lock:
            self._set("music", MUSIC_PLAYING)

    def intermission(self, who="", screen=""):
        """The show has been left (intermission, preshow, closing): flame
        cues to zero and the lasers blanked with a real command, so "no
        lasers during intermission" does not depend on how the last show
        ended. For the scheduler to call on leaving SHOW or PAUSED. While
        aborted it starts nothing new (a new request must not cut the
        Abort's fade short), but it does send the laser blank again, on
        this thread (finding B): an Abort whose blank did not get out is
        not left that way until Reset."""
        with self._lock:
            latched = self._latched
            if not latched:
                if self._look == STOPPED_DARK:
                    # Darker than BETWEEN already; a new request would only
                    # cut the stop's own fade short.
                    return done("The show was stopped, so the rig is "
                                "already dark.")
                self._accept("Intermission", BETWEEN, who, screen,
                             fade_s=0.0)
                return done("Out of the show: flame cues zero, lasers "
                            "dark.")
        return done(f"The show is aborted, so nothing else changes until "
                    f"Reset. {self._reblank('Intermission')}")

    def show_stopped(self, who="", screen=""):
        """A show cut short by ltcplay restarting (and the dark sequence
        sent again by a start that finds the rig meant to be dark). The rig
        goes dark and stays dark until an operator acts or the next show
        starts: flame cues zero, lasers blanked, video, pixels and music
        faded out over ABORT_FADE_S, the video stopped.

        Unlike abort() it does NOT disarm any flame group and does NOT
        latch, so Start now or the next scheduled show brings the rig up
        with no Reset (Jeff's rule is "dark until the operator acts"; a real
        restart already disarms every group, because the flame controller
        disarms once ltcplay stops sending). A failed start is
        failed_start(), which also disarms. It does not need a show
        playing: after a restart nothing is, and every output is UNKNOWN,
        so everything is sent. While aborted it changes nothing, like
        intermission()."""
        with self._lock:
            if self._latched:
                return done("Already aborted: the rig is dark, and nothing "
                            "else was sent.")
            self._accept("Show stopped", STOPPED_DARK, who, screen,
                         fade_s=ABORT_FADE_S)
            return done(f"The show stopped: flame cues zeroed and lasers "
                        f"blanked; video, pixels and music fade to black "
                        f"over {ABORT_FADE_S:g} s. Flame groups are not "
                        f"disarmed and nothing is latched: Start now or the "
                        f"next show brings the rig back.")

    FAILED_START = "the show failed to start"

    def failed_start(self, who="", screen=""):
        """A show that failed to start (no timecode within the confirm
        window): everything show_stopped() does (the rig dark, no latch, no
        Reset, Start now allowed at once), AND every flame group disarmed,
        the same disarm-all an Abort sends (Jeff, 2026-10-03: "disarm the
        flame units while we are troubleshooting"). Like Abort, the flame
        cues are zeroed and the disarm is sent here, on the calling thread,
        before this returns; the executor sends the disarm again only if
        that failed. Flames fire again only once each group is re-armed by
        hand, off then on, on the deck. It does not need a show playing (a
        show that never started may not be). While aborted it changes
        nothing: the Abort already disarmed."""
        with self._lock:
            if self._latched:
                return done("Already aborted: the rig is dark and every "
                            "flame group was disarmed by the Abort.")
            self._flames_cut(self.FAILED_START)
            self._accept("Failed start", STOPPED_DARK, who, screen,
                         fade_s=ABORT_FADE_S, disarm=self.FAILED_START)
            return done(f"The show failed to start: flame cues zeroed and "
                        f"a disarm sent to every flame group, because the "
                        f"show failed to start; lasers blanked; video, "
                        f"pixels and music fade to black over "
                        f"{ABORT_FADE_S:g} s. Nothing is latched: Start now "
                        f"works at once, and each flame group must be "
                        f"armed again by hand (off, then on) before flames "
                        f"can fire.")

    def abort(self, who="", screen=""):
        with self._lock:
            latched = self._latched
            if not latched:
                if not self._playing():
                    return self._refused("Abort", "nothing is playing, so "
                                         "there is nothing to abort.")
                self._latched = True
                self._latch_who = who
                # The flames do not wait for the executor, or even for the
                # journal line: cut them here, on the pressing thread,
                # inside the lock, so no stale step can land between the
                # cut and the new generation that makes every older step
                # stale. The video fade, if one is running, stops where it
                # is (finding C). Neither call waits for a device.
                self._flames_cut()
                self._video_cancel()
                blanked = threading.Event()
                line = self._accept("Abort", ABORTED, who, screen,
                                    fade_s=ABORT_FADE_S, blanked=blanked,
                                    quiet=True)
                ver = self._ver["lasers"]
        if latched:
            # No new generation, no second fade (finding B: but the blank
            # again, in case the first one never got out).
            return done(f"Already aborted. {self._reblank('Abort')} Press "
                        f"Reset to carry on.")
        # The lasers, on this thread, at once, outside the lock (finding D):
        # never queued behind the executor, never behind a BEYOND call the
        # executor is making. A restore under way stops before its next
        # packet (beyond.Beyond's blank epoch, and _restore_wanted).
        try:
            # lasers_fade_out: Jeff's Abort is a brightness ramp; the real
            # device layer blanks at once (beyond.py allows only 0 or 100)
            # and journals that it did. Either way it starts here, now.
            r = self._call("lasers blanked", self.devices.lasers_fade_out,
                           ABORT_FADE_S)
            with self._lock:
                if self._ver["lasers"] == ver:
                    self._set("lasers", BLACK if r.ok else UNKNOWN)
        finally:
            with self._lock:
                blanked.set()
                self._cv.notify_all()
        self._note(*line[0], **line[1])
        lasers = ("lasers blanked" if r.ok else "the laser blank did NOT "
                  "get out and is being tried again")
        return done(f"Abort: flames zeroed, disarm sent, {lasers}; video, "
                    f"pixels and music fade to black over {ABORT_FADE_S:g} "
                    f"s. Press Reset to carry on.")

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
                # nothing already sent, except the lasers' blank, which is
                # always re-sent: ALWAYS_RESENT), and the announcement plays
                # at once. After an Abort and its Reset that look is no
                # longer ABORTED (finding E: re-running the Abort would
                # journal "Latched until Reset" with nothing latched): it
                # is out of the show, dark.
                look = BETWEEN if self._look == ABORTED else self._look
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

    def _accept(self, label, look, who, screen, quiet=False, **params):
        """Make this the latest request. With `quiet`, its journal line is
        returned as ((text,), fields) for the caller to write later
        (Abort: after the lasers are dark, not before)."""
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
        line = ((f"{label}, asked for by {self._who(who, screen)}.",),
                dict(action=label.lower(), who=who, screen=screen))
        if not quiet:
            self._note(*line[0], **line[1])
        self._cv.notify_all()
        return line

    def _set(self, output, value):
        """Write an output's record. Called with the lock held."""
        self._applied[output] = value
        self._ver[output] += 1

    def _video_cancel(self):
        """Stop a running video fade where it is (Abort, Hold), with the
        lock held: video_cancel() never waits for MadMapper. The level is
        then not known for sure, so the record says UNKNOWN."""
        fn = getattr(self.devices, "video_cancel", None)
        if fn is None:
            return
        self._call("video fade stopped where it is", fn)
        self._set("video", UNKNOWN)
        self._async_seq["video"] = getattr(self.devices, "video_seq", None)

    def _reblank(self, what):
        """A laser blank, sent again on the pressing thread, outside the
        lock, and the sentence that says how the lasers stand now (finding
        B). Used where a press starts nothing new (already aborted,
        already on hold): a blank only ever makes the rig darker, so it is
        safe even part way through an Abort's fade."""
        with self._lock:
            ver = self._ver["lasers"]
        r = self._call(f"lasers blanked again ({what})",
                       self.devices.lasers_blank)
        with self._lock:
            if self._ver["lasers"] == ver:
                self._set("lasers", BLACK if r.ok else UNKNOWN)
            now = self._applied["lasers"]
        self._note(f"{what} pressed again: the laser blank was sent again"
                   f"{'' if r.ok else ' and did NOT get out'}.",
                   fault=False, action="lasers",
                   outcome="reblanked" if r.ok else "failed")
        if now == BLACK:
            return "The laser blank was sent again: the lasers are dark."
        if now == LIT:
            return ("The laser blank was sent again, but the lasers are "
                    "recorded as lit.")
        return ("The laser blank was sent again but did not get out: the "
                "lasers may still be lit.")

    def _restore_wanted(self):
        """The laser restore guard (DeviceOutputs.restore_guard), asked by
        beyond.Beyond before every unblank packet, from the executor's
        thread, without the lock: plain reads. False once any newer
        request is accepted, or once aborted."""
        return (self._restore_gen is not None
                and self._restore_gen == self._gen
                and not self._latched and not self._closed)

    def _device_report(self, output, ok, value, sentence, seq=None):
        """DeviceOutputs.report: a device's real outcome, arriving later on
        its own thread (finding A). Never blocks on a device."""
        with self._lock:
            if not ok:
                self._set(output, UNKNOWN)
            elif seq is None or seq == self._async_seq.get(output):
                self._set(output, value)
        if not ok:
            self._note(f"Not done: {sentence}", fault=True, action="output",
                       outcome="failed")

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
            elif look == STOPPED_DARK:
                self._run_stopped(gen, want, progress)
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
            self._step(gen, "lasers", BLACK, "lasers blanked", progress,
                       self.devices.lasers_fade_out, fade)
        else:
            self._step(gen, "lasers", BLACK, "lasers blanked", progress,
                       self.devices.lasers_blank)
        froze = a["music"] in (MUSIC_PLAYING, UNKNOWN)
        self._step(gen, "music", MUSIC_HELD, "music fading", progress,
                   self.show.music_hold, fade, only_from=(MUSIC_PLAYING,
                                                          UNKNOWN))
        faded = False
        # An announcement (DARK) always takes video and pixels to black,
        # fading over `fade` if there is one, at once if not (rehearsal).
        # A Hold (HELD) does the same only when there IS a fade: that is
        # production, where Jeff wants video and pixels faded to black with
        # the lasers, same as an announcement or an Abort. In rehearsal
        # `fade` is 0 (mode flag, not a second code path, see _fade), so
        # this is skipped and video and pixels hold on the frozen frame.
        if look == DARK or fade > 0:
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
        # A laser blank re-sent to lasers already dark (ALWAYS_RESENT) is
        # not a change: an announcement while already held and dark does
        # not wait another 0.5 s for it.
        want["changed"] = any(AGAIN not in p for p in progress)

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
        # Always, whatever the record says (finding C): the fade starts
        # from wherever the picture is now, and if it is already black
        # that is one more 0 sent, never a flash.
        faded |= self._step(gen, "video", BLACK, "video faded", progress,
                            self.devices.video_fade_out, fade, force=True)
        faded |= self._step(gen, "pixels", BLACK, "pixels faded", progress,
                            self.show.pixels_fade_out, fade)
        faded |= self._step(gen, "music", MUSIC_STOPPED, "music faded",
                            progress, self.show.music_halt, fade)
        # The lasers were blanked on the pressing thread (abort()), at the
        # same time as the fades above began. Once that call has returned
        # (normally long since), send the blank again here only if it did
        # not get out.
        blanked = want.get("blanked")
        if blanked is not None:
            self._await(gen, blanked.is_set, fade)
        with self._lock:
            lasers_dark = blanked is not None and blanked.is_set() and \
                self._applied["lasers"] == BLACK
        if lasers_dark:
            progress.append("lasers blanked at the press")
        else:
            self._step(gen, "lasers", BLACK, "lasers blanked", progress,
                       self.devices.lasers_blank)
        if faded:
            self._pause(gen, fade)
        self._step(gen, "video", STOPPED, "video stopped", progress,
                   self.devices.video_stop, force=True)
        if progress:
            with self._lock:
                latched = self._latched
            self._note(f"Abort finished: {', '.join(progress)}."
                       + (" Latched until Reset." if latched else ""),
                       action="abort", outcome="done")

    def _run_stopped(self, gen, want, progress):
        """STOPPED_DARK: Abort's sequence without the latch, and without the
        disarm unless it is a failed start (want["disarm"], its reason)."""
        fade = want["fade_s"]
        why = want.get("disarm")
        self._step(gen, "flames", ZERO, "flame cues zeroed", progress,
                   self.show.flames_zero)
        if why:
            with self._lock:
                self._check(gen)
                if not self._applied["disarmed"]:
                    self._disarm(why)
        self._step(gen, "lasers", BLACK, "lasers blanked", progress,
                   self.devices.lasers_blank)
        faded = False
        faded |= self._step(gen, "video", BLACK, "video faded", progress,
                            self.devices.video_fade_out, fade,
                            only_from=(LIT, UNKNOWN))
        faded |= self._step(gen, "pixels", BLACK, "pixels faded", progress,
                            self.show.pixels_fade_out, fade)
        faded |= self._step(gen, "music", MUSIC_STOPPED, "music faded",
                            progress, self.show.music_halt, fade)
        if faded:
            self._pause(gen, fade)
        self._step(gen, "video", STOPPED, "video bank stopped", progress,
                   self.devices.video_stop)
        if progress and why:
            self._note(f"The rig is dark after a failed start: "
                       f"{', '.join(progress)}. Every flame group was sent "
                       f"a disarm because {why}; nothing is latched.",
                       action="failed start", outcome="done")
        elif progress:
            self._note(f"The rig is dark after a stopped show: "
                       f"{', '.join(progress)}. Nothing was disarmed and "
                       f"nothing is latched.", action="show stopped",
                       outcome="done")

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
        if self.threaded:
            # announce.play reads the file before it returns: on its own
            # thread, so the executor is free at once and an Abort's fade
            # never waits behind a file read (finding D).
            threading.Thread(target=self._play, args=(ann_id, who, screen),
                             name="ltcplay-conductor-announce",
                             daemon=True).start()
            progress.append("announcement starting")
        elif self._play(ann_id, who, screen):
            progress.append("announcement playing")

    def _play(self, ann_id, who, screen):
        try:
            self.announcer(ann_id, who, screen)
        except Exception as e:
            self._note(f"The {ann_id} announcement did not play: {e}",
                       fault=not isinstance(e, ValueError),
                       action="announce", outcome="refused")
            return False
        return True

    def _restore_lasers(self, gen, progress):
        """The ONLY way the lasers are lit. The gate says no, raises, or
        says anything but None: they are made dark (blanked, unless already
        known to be), and the journal says why. Dark is enforced here, not
        assumed: lasers left lit by an earlier look are blanked too.
        The gate is asked OUTSIDE the conductor's lock: it may take the
        scheduler's, and the scheduler may call in here with its own held.
        It is asked on a helper thread (finding D), and the executor waits
        for it the way it waits for anything, waking at once for a newer
        press, so a slow gate never holds up an Abort's fade. A gate that
        has not answered within GATE_TIMEOUT_S is a no.
        _step re-checks the generation before the call."""
        with self._lock:
            self._check(gen)
        why = self._ask_gate(gen)
        if why is not None:
            self._note(f"The lasers stay dark: {why}", action="lasers",
                       outcome="refused")
            self._step(gen, "lasers", BLACK, "lasers blanked", progress,
                       self.devices.lasers_blank)
            return
        self._step(gen, "lasers", LIT, "lasers back", progress,
                   self.devices.lasers_restore)

    def _ask_gate(self, gen):
        """The laser gate's answer: None (lasers may light) or why not."""
        def ask():
            try:
                return self.laser_gate()
            except Exception as e:
                why = f"the laser gate failed ({type(e).__name__}: {e})"
                return why
        if not self.threaded:
            return ask()
        box = []

        def run():
            v = ask()
            with self._lock:
                box.append(v)
                self._cv.notify_all()
        threading.Thread(target=run, name="ltcplay-conductor-gate",
                         daemon=True).start()
        if self._await(gen, lambda: bool(box), GATE_TIMEOUT_S):
            return box[0]
        return (f"the laser gate did not answer within "
                f"{GATE_TIMEOUT_S:g} s")

    # -- steps and waits ---------------------------------------------------------
    def _check(self, gen):
        if self._gen != gen or self._closed:
            raise _Superseded()

    def _step(self, gen, output, value, label, progress, fn, *args,
              only_from=None, force=False):
        """Check the generation and make one call, as one locked step.
        Returns True if a command was sent. Skips an output already at
        `value` (UNKNOWN never is), or not in `only_from` when given,
        EXCEPT lasers dark (ALWAYS_RESENT): see that constant, and anything
        with `force`.

        The lasers (UNLOCKED_OUTPUTS) are the exception to "one locked
        step": the generation is checked under the lock, the call is made
        after it is released, and its outcome is recorded only if nothing
        newer has written the lasers' record since (finding D). A restore
        stays safe without the lock: _restore_wanted turns false the
        moment a newer request is accepted, and BEYOND stops the unblank
        before its next packet.

        An async output (the video through ConductorDevices) is UNKNOWN
        until the device reports what really happened (finding A)."""
        with self._lock:
            self._check(gen)
            now = self._applied[output]
            again = now == value
            if again and not force and (output, value) not in ALWAYS_RESENT:
                return False
            if only_from is not None and now not in only_from:
                return False
            unlocked = output in UNLOCKED_OUTPUTS
            if not unlocked:
                r = self._call(label, fn, *args)
                if output in self._async:
                    self._set(output, UNKNOWN)
                    self._async_seq[output] = getattr(self.devices,
                                                      "video_seq", None)
                else:
                    self._applied[output] = value if r.ok else UNKNOWN
                    self._ver[output] += 1
            else:
                if value == LIT:
                    self._restore_gen = gen
                ver = self._ver[output]
        if unlocked:
            r = self._call(label, fn, *args, gen=gen)
            with self._lock:
                if self._ver[output] == ver:
                    self._set(output, value if r.ok else UNKNOWN)
        if again:
            label = f"{label} {AGAIN}"
        progress.append(label if r.ok else f"{label} (FAILED)")
        return True

    def _call(self, label, fn, *args, gen=None):
        """One output call. Never raises; anything but a good Result is a
        failure, written down as a fault with its sentence, unless `gen`
        is given and a newer request has come in since (a laser restore
        cut short by an Abort is what the Abort wanted, not a fault)."""
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
            if gen is not None and gen != self._gen:
                self._note(f"Stopped: {r.sentence}", action="output",
                           outcome="superseded")
            else:
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

    def _flames_cut(self, reason="Abort"):
        """Abort's (and a failed start's) instant half, called with the lock
        held."""
        r = self._call("flame cues zeroed", self.show.flames_zero)
        self._applied["flames"] = ZERO if r.ok else UNKNOWN
        self._disarm(reason)

    def _disarm(self, reason="Abort"):
        r = self._call("disarm every flame group",
                       self.show.flames_disarm_all, reason)
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
