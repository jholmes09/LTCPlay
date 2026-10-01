"""Hold/Resume/Abort for the device layer: MadMapper (madmapper.py) and
BEYOND (beyond.py), composed together.

Imported only by whatever calls it -- like madmapper.py and beyond.py, this
module has no idea when a show starts, holds or aborts, and is never
imported by the GPL path (see test_the_gpl_path_never_loads_devices). It
takes an already-built madmapper.py Link (or None) and an already-built
beyond.py Beyond (or None) and knows nothing else about the rest of the
program.

These three functions are NOT scheduler hooks. madmapper.py's own module
docstring explains why hold()/resume()/abort()/on_transition() were removed
from THAT module (and never added to beyond.py either): deriving "what to
do" from a pair of scheduler states, reached on schedule_service.py's own
unordered per-transition hook threads, produced two real races (a Hold
immediately followed by a quick Resume could run in either order or
interleave). That problem is about WHEN and HOW OFTEN something gets
called, not what it sends once it is actually called -- so it is not fixed
by moving the code here. It is fixed by never calling these from an
unordered hook thread again. When this was written, the plan was that the
future show conductor's single, serialized executor would call on_hold(),
on_resume() and on_abort() as whole units. That is NOT how it was wired:
the conductor sequences each output itself and reaches the rig through
ConductorDevices, at the end of this module. See "The conductor does NOT
call on_hold(), on_resume() or on_abort()" below for why, and for where
each ordering guarantee these three functions gave now lives. Nothing
calls these three functions.

What each one sends, and why (handoff section 5 and section 4a; Jeff's
2026-09-26 and 2026-09-27 decisions):

  on_hold() -- "Hold during a show pauses it where it is. Fade the show
  music out over 0.25 s, then freeze the show at the frame where the fade
  ends... send zero on every flame cue channel, and blank the lasers.
  Blanking must be a real command to BEYOND, not only frozen timecode."
  BEYOND is blanked FIRST (bench: a blank/unblank round-trips in about
  40 ms across its 3-times retry, so this happens essentially at once),
  THEN the music fades over HOLD_FADE_S. Freezing the video itself is NOT
  this function's job: MadMapper is never told to fade its surfaces on
  Hold (the handoff: "pixels hold, video holds" -- the picture freezes
  because the Art-Net timecode itself freezes, which is the clock's job,
  not a MadMapper OSC command). Flame cues are also not this function's
  job -- flamesafe alone ever writes the flame universe (section 7); a
  conductor zeroes them through that channel, not through here.

  on_resume() -- the reverse: the music fades back up over HOLD_FADE_S,
  then BEYOND is unblanked -- but ONLY if `in_show` is True. The handoff
  is explicit and absolute: "No lasers during intermission." A Resume
  between shows (intermission, or before the first show of the night) is
  a real, named case (section 5: "Hold between shows delays the next
  show"), and BEYOND must stay dark through it. `in_show` has NO default:
  a caller has to say which case this is, rather than this module
  guessing from a state it is never given. Refusing is done HERE, not left
  to the caller to remember -- see `in_show`'s own docstring below for
  exactly what "refuse" means when it isn't a show, including the
  defensive re-blank it sends rather than trusting an earlier blank
  landed. beyond.py's own unblank() also refuses an in_show=False on its
  own, a second guard for any caller that reaches it directly instead of
  through here -- see beyond.py's own module docstring.

  on_abort() -- "flame cues zero and every group disarms instantly; music,
  video, pixels and lasers fade to black together over 1 s (lasers by a
  BEYOND brightness ramp, not an instant blank), then everything stops"
  (Jeff, 2026-09-27). This function sends BEYOND's blank command FIRST
  (again, effectively at once) and then fades MadMapper's audio and every
  surface together, over ABORT_FADE_S (the show's own `madmapper.fade_s`,
  1.0 s by default) -- using Link.fade_all() so the two genuinely run
  together rather than audio finishing before video starts. This is a
  KNOWN, DELIBERATE deviation from the handoff's literal words: the
  handoff wants BEYOND's brightness ramped down over the same 1 s, not
  blanked at once, but beyond.py's allow-list (S5) only ever permits the
  two exact values 0.0 and 100.0 -- built that way on purpose, after an
  audit found a deny-list-only design let near-miss addresses through.
  Loosening it to admit a ramp of arbitrary brightness values is a
  laser-safety-relevant change to a module that earned its current shape
  from a safety audit, and this task did not make that change unreviewed.
  Blanking BEYOND immediately, ahead of the video/audio fade rather than
  in step with it, is the conservative reading when in doubt: it is never
  wrong to go dark on the lasers SOONER than the handoff's own 1 s window,
  only to go dark LATER, and instant-blank is beyond.py's own proven,
  audited behavior (bench B8.3). Flag for whoever reviews this: if a real
  BEYOND ramp is wanted for Abort, it needs its own reviewed change to
  beyond.py's allow-list, not a workaround here.

None of the three ever touches the flame universe, ever disarms anything,
or ever talks to the scheduler. Each degrades gracefully when the show has
no MadMapper block, no BEYOND block, or neither (a show file need not
configure either device layer): a None `madmapper` or `beyond` is simply
skipped, exactly like web.py's own serve() already treats a None link
everywhere else in this codebase.

The conductor does NOT call on_hold(), on_resume() or on_abort()
================================================================

Everything above was written before the conductor (conductor.py, PR #28)
existed. When it arrived, its DeviceOutputs interface turned out to be
granular (lasers_blank, lasers_fade_out, lasers_restore, video_fade_out,
video_restore, video_stop), and the conductor does its own sequencing: one
step at a time, under its generation guard, with its own record of what
each output was last told. ConductorDevices, below, is the conductor's
device layer. It calls beyond.py's and madmapper.py's own primitives
directly, one primitive per method, and composes nothing itself. The three
functions above are kept, tested, but nothing calls them; they are not a
second way into the rig, and must not become one. Calling them from the
conductor would be wrong in five ways:

  1. Double sequencing. on_hold() blanks BEYOND and fades music in one
     call; the conductor would also be stepping lasers, music and video
     itself, under its own generation guard. A Resume that supersedes a
     Hold half way can only undo the steps the conductor knows were taken.
  2. The wrong music. on_hold()/on_resume()/on_abort() fade MadMapper's
     master audio level. With clock source "audio_master" (handoff section
     4a, Jeff 2026-09-27), ltcplay plays the show music itself and
     MadMapper plays the video only; the conductor fades the music through
     ShowOutputs.music_hold/music_resume/music_halt (AudioMaster).
  3. Superseded video rule. on_hold() never fades MadMapper's surfaces
     ("pixels hold, video holds", 2026-09-27). Jeff changed that on
     2026-09-30: a production Hold fades video and pixels to black with
     the lasers, a rehearsal Hold freezes them. The conductor carries the
     newer decision.
  4. Lasers too early on Resume. on_resume() unblanks BEYOND straight after
     starting the music fade. The conductor waits until the show clock says
     the timecode is MOVING again, then asks its laser gate, then lights
     them. Later is the safe direction.
  5. Blocking. With wait=True they sleep through the fade; the conductor
     holds its lock across every device call and needs each to return in
     well under 0.1 s, or an Abort waits behind a Hold's fade.

Every guarantee the three functions gave is kept on the conductor path,
and selftest's test_conductor_devices_* tests drive a real Conductor
through ConductorDevices into fake BEYOND and MadMapper sockets to prove
it:

  - BEYOND goes dark BEFORE any music or video fade starts, on Hold, on an
    announcement and on Abort. The conductor's own order is flames, then
    lasers, then the rest, and beyond.blank() returns only after its
    packets have gone (about 40 ms), so the blank is out before MadMapper
    is sent anything.
  - Abort's lasers are an instant blank, never a ramp: lasers_fade_out()
    here blanks at once and journals that it was not a fade. beyond.py's
    allow-list (0.0 and 100.0 only) is untouched. A real ramp for Abort is
    still a separate, laser-safety-relevant change for review.
  - Hold's 0.25 s fade, then freeze: the conductor asks AudioMaster for it
    (music_hold) and waits for the clock to report frozen.
  - No lasers in intermission: lasers_restore() is the only unblank, and
    the conductor calls it only after its laser gate says yes; a gate that
    says no (or fails) gets a real blank sent instead, every time.
  - A blank is never assumed to have landed: the conductor re-sends the
    lasers' dark command whenever a look wants them dark, even when its
    record says they already are (conductor.ALWAYS_RESENT)."""

import time

from .conductor import DeviceOutputs, done, failed

# The Hold fade (handoff, Jeff 2026-09-27: "Hold in production: fade 0.25 s,
# THEN freeze at the frame where the fade ends"). Deliberately its own
# constant, separate from madmapper.py's `cfg.fade_s` (the show's Abort/
# general-purpose fade length, 1.0 s by default): Hold and Abort are
# different fades with different durations by Jeff's own numbers, and nothing
# here should silently follow a change to one and not the other.
HOLD_FADE_S = 0.25


def _for_show(show):
    return f" for show {show}" if show else ""


def _note(journal, text, **extra):
    if journal:
        try:
            journal(text, **extra)
        except Exception:
            pass


def on_hold(madmapper=None, beyond=None, *, show=None,
           fade_seconds=HOLD_FADE_S, wait=True, journal=None):
    """Blank BEYOND (a real command), then fade MadMapper's show music down
    over `fade_seconds` (0.25 s by default, the handoff's Hold number).
    Never touches MadMapper's surfaces (video freezes via the timecode
    itself, not an OSC fade) and never touches the flame universe. `show`
    is only used to name the show in the journal line, if any is
    configured on the links themselves; this function does no logging of
    its own beyond what madmapper.py's fade_audio() and beyond.py's
    blank() already do through their own `journal` callbacks -- `journal`
    here is for what THIS function decides, which for on_hold() is
    nothing extra.

    Returns BEYOND's own blank() result: True if the blank got out, False
    if it did NOT (the lasers may still be live -- the caller must treat
    that as a fault, not as "blanked"), None if no BEYOND is configured.
    The music fade still runs either way."""
    blanked = None
    if beyond is not None:
        blanked = beyond.blank(show=show)
    if madmapper is not None:
        madmapper.fade_audio(1.0, 0.0, seconds=fade_seconds, wait=wait)
    return blanked


def on_resume(madmapper=None, beyond=None, *, in_show, show=None,
             fade_seconds=HOLD_FADE_S, wait=True, journal=None):
    """Fade MadMapper's show music back up over `fade_seconds`, then
    unblank BEYOND -- but ONLY when `in_show` is True.

    `in_show` has no default on purpose: the handoff's rule ("No lasers
    during intermission") is absolute, and a caller has to say which case
    this is rather than this function guessing. When `in_show` is False,
    BEYOND is NOT sent an unblank command -- this is the refusal the task
    asks for, enforced here in the device layer rather than left to every
    future caller to remember. But "leave BEYOND as it was" trusts nothing:
    the earlier blank (on Hold, or whenever BEYOND was last blanked) might
    itself have failed, silently, with nobody the wiser until this moment.
    So this branch does not just skip; it re-sends the blank command
    defensively, every time, through beyond.py's own blank() -- the exact
    same fail-loud reporting (through beyond.py's own `journal`) that a
    real Hold or Abort blank already gets, not a special silent case for
    Resume. THIS function's own `journal` additionally gets one sentence
    naming why the unblank itself was refused (Resume between shows), and,
    if the defensive re-blank failed, a second, explicitly fault-flagged
    sentence -- a failed re-blank during intermission is never folded into
    the calm "stays blanked" wording.

    `in_show` must be the real bool True or False: anything else (a state
    name, a slot number, 1, "false", None) raises TypeError BEFORE anything
    is sent, rather than being read as truthy -- a non-empty string such as
    "STANDBY" or "false" would otherwise silently unblank the lasers during
    intermission.

    Returns BEYOND's unblank() result (True/False) when `in_show` is True,
    or the defensive re-blank's own result (True/False) when it is False --
    either way, False means a laser-safety command did NOT get out and
    must be treated as a fault, the same rule on_hold()/on_abort() already
    follow for their own blank(). None only when no BEYOND is configured
    at all."""
    if not isinstance(in_show, bool):
        raise TypeError(
            f"on_resume() needs in_show=True or in_show=False, not "
            f"{in_show!r}: whether the lasers may come back is never "
            f"guessed from a truthy value.")
    if madmapper is not None:
        madmapper.fade_audio(0.0, 1.0, seconds=fade_seconds, wait=wait)
    if beyond is not None:
        if in_show is True:
            return beyond.unblank(show=show, in_show=True)
        else:
            # Refused here, not by calling beyond.unblank(in_show=False):
            # this in_show check is done and refused BEFORE ever asking
            # beyond.py for an unblank at all. beyond.py's unblank() carries
            # the identical in_show check as a second, independent layer
            # for any caller that reaches it directly instead of through
            # here -- see its own docstring. What IS sent here is a
            # defensive re-blank (see the docstring above): never assume
            # the earlier blank actually landed.
            reblanked = beyond.blank(show=show)
            if reblanked:
                _note(journal,
                     f"BEYOND stays blanked{_for_show(show)} (re-sent as "
                     f"a defensive check): Resume is between shows (no "
                     f"lasers during intermission), not during a show.",
                     action="unblank", outcome="refused", show=show)
            else:
                _note(journal,
                     f"BEYOND was told to stay blanked{_for_show(show)} "
                     f"(Resume is between shows, no lasers during "
                     f"intermission), but the defensive re-blank FAILED: "
                     f"no packet got out. The lasers may still be live "
                     f"through intermission.", action="unblank",
                     outcome="refused", show=show, fault=True)
            return reblanked


def on_abort(madmapper=None, beyond=None, *, show=None,
            fade_seconds=None, wait=True, journal=None):
    """Blank BEYOND at once, then fade MadMapper's music and every surface
    to black TOGETHER over `fade_seconds` (the show's own
    `madmapper.fade_s`, 1.0 s by default, if `fade_seconds` is not given
    explicitly) using Link.fade_all() -- see the module docstring for why
    BEYOND is blanked instantly here rather than ramped over the same
    window, a deliberate, documented deviation from the handoff's literal
    words pending a reviewed change to beyond.py's allow-list.

    Returns BEYOND's blank() result, exactly as on_hold() does: False means
    the blank did NOT get out and must be treated as a fault."""
    blanked = None
    if beyond is not None:
        blanked = beyond.blank(show=show)
    if madmapper is not None:
        kwargs = {"wait": wait}
        if fade_seconds is not None:
            kwargs["seconds"] = fade_seconds
        madmapper.fade_all(1.0, 0.0, **kwargs)
    return blanked


class ConductorDevices(DeviceOutputs):
    """The conductor's lasers (BEYOND) and video (MadMapper): conductor.py's
    DeviceOutputs, one primitive per method, no sequencing of its own. See
    the module docstring for why this calls beyond.py and madmapper.py
    directly instead of on_hold()/on_resume()/on_abort().

    `madmapper` is an already-built madmapper.Link or None, `beyond` an
    already-built beyond.Beyond or None; this module still imports neither.
    `wired` is True only when both are given, so a Conductor built with a
    show that lacks either one writes its "not connected" fault line at
    start rather than passing for a rig whose lasers it blanks.

    Every method follows DeviceOutputs' rules: returns a conductor Result,
    never raises, and returns at once. Video fades are started with
    wait=False and run on the Link's own worker; a newer one supersedes
    the old (the Link's ramp generation). beyond.py's blank() and
    unblank() send 3 packets 20 ms apart and return after them, about
    40 ms, so the lasers are dark before the conductor's next call."""

    def __init__(self, madmapper=None, beyond=None, *, show=None,
                 journal=None):
        self.mm = madmapper
        self.beyond = beyond
        self.show = show
        self._journal = journal
        self.wired = madmapper is not None and beyond is not None
        if beyond is None:
            _note(journal, "No BEYOND is configured for this show: the "
                  "conductor's laser commands reach nothing.",
                  action="devices", outcome="not_configured", fault=True)
        if madmapper is None:
            _note(journal, "No MadMapper is configured for this show: the "
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
            _note(self._journal,
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
        return self._beyond("Laser restore", "unblank",
                            "The lasers stay dark.", in_show=True)

    # -- video: madmapper.py -------------------------------------------------
    def _madmapper(self, what, send):
        if self.mm is None:
            return done(f"{what}: no MadMapper is configured for this show.")
        if getattr(self.mm, "_closed", False):
            return failed(f"{what}: the MadMapper link is closed, so nothing "
                          f"was sent.")
        try:
            send()
        except Exception as e:
            return failed(f"{what} failed: {type(e).__name__}: {e}.")
        return done(f"{what}: sent to MadMapper.")

    def _surfaces(self, start, end, seconds):
        if seconds <= 0:
            # At once: stop any ramp still running (it checks the Link's
            # generation before every step), then one level. The level is
            # queued behind that ramp's job on the Link's single worker, so
            # it always lands after the ramp's last step, never under it.
            self.mm.cancel()
            self.mm.set_surfaces(end, wait=False)
        else:
            self.mm.fade_surfaces(start, end, seconds=seconds, wait=False)

    def video_fade_out(self, seconds):
        return self._madmapper(
            f"Video fade to black over {seconds:g} s",
            lambda: self._surfaces(1.0, 0.0, seconds))

    def video_restore(self, seconds):
        return self._madmapper(
            f"Video back up over {seconds:g} s",
            lambda: self._surfaces(0.0, 1.0, seconds))

    def video_stop(self):
        return self._madmapper(
            "Video stop",
            lambda: self.mm.stop_bank(self.mm.cfg.show_bank, wait=False))
