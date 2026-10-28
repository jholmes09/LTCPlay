"""The Programming screen's playhead on the show audio (Jeff, 2026-10-28).

Jeff's field report, items 7 and 8: Play from on the Programming screen
gave no audio. It only set a free-run start time for the pixels and the
flame scrub; the show audio, the Art-Net timecode (MadMapper, BEYOND) and
the show's flame cues were never started, because only Run + Start now
starts the show cue on the show audio.

Here the Programming transport drives the SAME show path Start now does,
with a start point: one playhead, the show clock (clock.AudioMaster), and
everything follows it exactly as it follows a show.

  Play from   the show runner's start (fire_ice.ShowRunner.start_show):
              MadMapper's show bank selected, the cue played on the show
              audio (now from a sample part way in, Session.clock_play_from),
              conductor.music_started(), then conductor.show_starting(),
              the call the scheduler makes once a show is confirmed: video
              and pixels up, the lasers through the laser gate, the flame
              cues released.
  Back, Ahead the clock's seek (Session.clock_seek): the audio starts on the
              new sample, the timecode on the new frame, the pixels follow.
              Paused, the frozen frame moves and Continue plays from it.
  Pause       conductor.hold(): the show's own Hold, unchanged.
  Continue    conductor.resume(): the show's own Resume, unchanged.
  A/B loop    the clock's loop: past B it seeks to A, like Back.
  Follow      conductor.intermission() (flame cues zero, lasers blanked),
              then the clock's stop.

Nothing here arms, disarms, blanks, unblanks or fires anything itself. Every
flame and laser output still goes through the paths a show uses, unchanged:
the conductor's steps, the laser gate and the laser arm, the flame link's
rules (its seek guard sees every move of the timecode), and flamesafe, which
fires only a group an operator has armed. A cue the playhead jumps over is
simply not played.

Used only when the running show's clock can start part way and move
(Session.playhead_clock: this machine is the clock, AudioMaster). Anything
else (GPL, a show that follows timecode) keeps the free run, as before.

The lasers stay dark in Programming: the laser gate lights them only while
a scheduled show is live (SHOW or PAUSED), and Programming is locked then.
BEYOND's own timeline still follows the timecode.

Review of #142, the ways playback ends that are not this module's own:

  Hold and Abort from a screen or the Stream Deck, while Programming
  playback is on: Hold is this Pause (the scheduler's Hold would only take
  the rig out of the show and leave the music running), Abort is Stop
  (the flame disarm and the laser disarm at the press stay exactly as
  they are; remote.py calls hold_press and abort_press after them).
  Any Stop (the Rack screen's, the Stream Deck's), the cue ending, Run or
  Stop of the session: poll() sees the playback gone and takes the
  conductor out of the show (intermission), so its record of the flame
  cues is "zero" again and the next start, Programming or a real show,
  releases them again. A Stop that zeroed the flame link directly would
  otherwise leave the record at "released", and the next start would
  skip the release.
  No screen at all: with no operator page asking the engine's status for
  WATCHDOG_S, poll() stops the playback (an A/B loop would otherwise go
  round for ever), with a journal line.
"""
import threading
import time

WATCHDOG_S = 60.0     # no operator page for this long: playback stops
POLL_S = 0.25


class PlayheadError(ValueError):
    """A transport press that cannot be done, in a sentence."""


class ShowAudioPlayhead:
    """The Programming transport on the show audio. `control` is
    web.Control (the running session is looked up on every call);
    `schedule` the scheduler service, whose `conductor` (Fire & Ice) is
    used when there is one."""

    def __init__(self, control, schedule=None, journal=None,
                 clock=time.monotonic, threaded=False):
        self.control = control
        self.schedule = schedule
        self._journal = journal          # (text, outcome) -> anything
        self._clock = clock
        self.threaded = threaded
        self._lock = threading.Lock()
        # One move of the playhead at a time: Play from, a move, Stop and
        # the watch's poll each run whole, so a poll that found the last
        # playback gone can never take the rig out of the show under a
        # Play from made meanwhile (re-review of #142, P3).
        self._op = threading.RLock()
        # (session, the clock's cue Play from started): a fresh tuple per
        # Play from, compared by identity, so it is a start token too. A
        # cue on the clock that is not this one (a real show started over
        # it) is not Programming playback.
        self._ours = None
        self._seen = clock()
        self._thread = None

    def _note(self, text, outcome):
        if self._journal is not None:
            try:
                self._journal(text, outcome)
            except Exception:                       # noqa: BLE001
                pass

    def seen(self):
        """An operator page asked for the engine's status: the
        watchdog's heartbeat."""
        self._seen = self._clock()

    def _live(self):
        """True while a scheduled show is live (SHOW or PAUSED)."""
        m = getattr(self.schedule, "machine", None)
        return getattr(m, "state", None) in ("SHOW", "PAUSED")

    def _mine(self, ours):
        """True while `ours` is what the show audio is playing now: the
        same session, the very cue Play from started, no scheduled show
        live."""
        if ours is None or self._live():
            return False
        s = self._session()
        clk = self.clock()
        return (s is ours[0] and clk is not None and
                getattr(clk, "playing", False) and
                getattr(clk, "_cue", None) is ours[1])

    def active(self):
        """True while Programming playback started by Play from is what the
        show audio is playing."""
        return self._mine(self._ours)

    def _adopt(self, s):
        """The cue now on the clock is ours (Play from, a move into another
        cue): a new token."""
        clk = self.clock()
        with self._lock:
            self._ours = (s, getattr(clk, "_cue", None))

    # -- what is there ------------------------------------------------------
    def _session(self):
        s = getattr(self.control, "session", None)
        if s is None or not getattr(s, "running", False):
            return None
        return s

    def clock(self):
        s = self._session()
        if s is None:
            return None
        get = getattr(s, "playhead_clock", None)
        return get() if callable(get) else None

    def applies(self):
        """True when the show audio is the playhead for this session."""
        return self.clock() is not None

    def _conductor(self):
        return getattr(self.schedule, "conductor", None)

    @staticmethod
    def _refused(result, what):
        if result is None:
            return
        if getattr(result, "ok", True) is False:
            raise PlayheadError(f"{what} was refused: "
                                f"{getattr(result, 'sentence', '')}")

    # -- reading -------------------------------------------------------------
    def here(self):
        """Show seconds of the playhead, or None with no cue playing."""
        s = self._session()
        return s.clock_here() if s is not None else None

    def view(self):
        clk = self.clock()
        s = self._session()
        if clk is None or s is None:
            return None
        cue = getattr(clk, "_cue", None)
        loop = getattr(clk, "loop", None)
        base = cue["position_s"] if cue else 0.0
        return {"playing": cue is not None,
                "paused": bool(cue is not None and clk.paused),
                "loop": ([s.tl.format(base + loop[0]),
                          s.tl.format(base + loop[1])]
                         if cue is not None and loop else None)}

    # -- the transport -------------------------------------------------------
    def resolve(self, at):
        """Show seconds for a cue name, a cue's timecode or a timecode."""
        s = self._session()
        tl = s.tl
        text = str(at or "").strip()
        match = [c for c in tl.cues
                 if c.name.lower() == text.lower() or c.tc_text == text]
        if match:
            return match[0].tc_seconds
        return tl.parse(text)

    def play_from(self, show_s, who="", screen=""):
        """The show start, from `show_s`. Returns show seconds."""
        with self._op:
            return self._play_from(show_s, who, screen)

    def _play_from(self, show_s, who, screen):
        # A Start now waiting for the show audio is cancelled: it must not
        # start a show over this playback (#144's wait; Start now asks
        # again under the scheduler's lock too, start_veto).
        cancel = getattr(self.schedule, "cancel_start_wait", None)
        if cancel is not None:
            try:
                cancel("Play from was pressed on the Programming screen")
            except Exception:                       # noqa: BLE001
                pass
        s = self._session()
        cond = self._conductor()
        if cond is not None and getattr(cond, "latched", False):
            # The show runner's own rule: nothing starts while an Abort
            # stands.
            raise PlayheadError("An Abort is standing, so nothing plays "
                                "until someone presses Reset.")
        # Every refusal the show audio has (still loading, past the end,
        # before the first cue) before MadMapper's bank is touched.
        s.clock_play_from(show_s, check_only=True)
        # A Stop or an Abort since the last Play from: the conductor's
        # record is put right before the show comes up again.
        self.poll()
        mm = getattr(getattr(cond, "devices", None), "mm", None)
        if mm is not None:
            try:
                mm.select_bank(mm.cfg.show_bank, wait=False)
            except Exception as e:                  # noqa: BLE001
                note = getattr(cond, "_note", None)
                if callable(note):
                    note(f"Selecting the MadMapper show bank failed: {e}",
                         fault=True, action="video", outcome="failed")
            # Following timecode again after a Stop or an Abort stopped
            # the bank (fire_ice.follow_show_bank says why).
            from .fire_ice import follow_show_bank
            follow_show_bank(mm, getattr(cond, "_note", None) or
                             (lambda *a, **k: None))
        _cue, at = s.clock_play_from(show_s)
        self._adopt(s)
        with self._lock:
            self._seen = self._clock()
        self._watch()
        if cond is not None:
            told = getattr(cond, "music_started", None)
            if told is not None:
                told()
            r = cond.show_starting(who, screen)
            if getattr(r, "ok", True) is False:
                # An Abort landed while the cue was starting (the show
                # runner's rule): the music stops here too.
                s.clock_halt()
                self._refused(r, "Play from")
        return at

    def jump(self, seconds):
        s = self._session()
        here = s.clock_here()
        if here is None:
            raise PlayheadError("Nothing is playing to move. Play from a "
                                "timecode first.")
        with self._op:
            was = self.active()
            at = s.clock_seek(max(0.0, here + float(seconds)))
            if was:
                self._adopt(s)      # a move into another cue is ours too
            return at

    def pause(self, who="", screen=""):
        s = self._session()
        if s.clock_here() is None:
            raise PlayheadError("Nothing is playing to pause. Play from a "
                                "timecode first.")
        cond = self._conductor()
        if cond is not None:
            self._refused(cond.hold(who, screen), "Pause")
        else:
            s.clock_pause()
        return s.clock_here()

    def cont(self, who="", screen=""):
        s = self._session()
        if s.clock_here() is None:
            raise PlayheadError("Nothing is playing to continue. Play from "
                                "a timecode first.")
        cond = self._conductor()
        if cond is not None:
            self._refused(cond.resume(who, screen), "Continue")
        else:
            s.clock_resume()
        return s.clock_here()

    def loop(self, a, b, who="", screen=""):
        """Loop between show seconds a and b, starting at A when nothing
        is playing; None, None turns it off."""
        s = self._session()
        if a is None and b is None:
            s.clock_set_loop(None, None)
            return None
        if s.clock_here() is None:
            self.play_from(a, who, screen)
        return s.clock_set_loop(a, b)

    def follow(self, who="", screen="", only=None):
        """Stop the playhead: the conductor's out-of-the-show look first
        (flame cues zero, lasers blanked), then the show audio stops.
        `only` (the watchdog): stop only while that playback is still the
        one playing and no show is live, checked again here; False when it
        was not."""
        with self._op:
            if only is not None and not self._mine(only):
                return False
            s = self._session()
            with self._lock:
                self._ours = None
            cond = self._conductor()
            if cond is not None:
                cond.intermission(who, screen)
            if s is not None and s.clock_here() is not None:
                s.clock_halt()
            return None

    # -- Hold, Abort, Stop and the watchdog (review of #142) ----------------
    def hold_press(self, who="", screen=""):
        """A Hold from any surface while Programming playback is on: the
        Programming Pause. Returns the sentence."""
        at = self.pause(who, screen)
        s = self._session()
        return (f"Programming playback paused at "
                f"{s.tl.format(at) if at is not None else 'its frame'}: "
                f"flame cues zero, lasers blanked, music and timecode "
                f"frozen. Continue on the Programming screen carries on.")

    def abort_press(self, who="", screen=""):
        """An Abort from any surface while Programming playback is on,
        after remote.py has taken the flames and lasers off: Stop the
        playback."""
        self.follow(who, screen)
        return ("Programming playback stopped: the music, the timecode and "
                "the pixels stop, flame cues zero, lasers blanked.")

    def poll(self):
        """Once a quarter second while Programming playback is on (the
        watch thread, or a test). "ended": the playback is gone however it
        went, and the conductor was taken out of the show; "watchdog": no
        operator page for WATCHDOG_S, so it was stopped; "dropped": a
        scheduled show is live, or the clock is playing a cue Play from did
        not start (a real show started over it): that is not Programming
        playback, so it is let go with nothing sent at all; None
        otherwise."""
        with self._op:
            return self._poll()

    def _poll(self):
        with self._lock:
            ours = self._ours
        if ours is None:
            return None
        s = self._session()
        clk = self.clock()
        cue = getattr(clk, "_cue", None) if clk is not None else None
        if self._live() or (s is ours[0] and cue is not None and
                            cue is not ours[1]):
            with self._lock:
                if self._ours is not ours:
                    return None
                self._ours = None
            self._note("Programming playback was ended by the show "
                       "starting.", "taken_over")
            return "dropped"
        if s is not ours[0] or clk is None or \
                not getattr(clk, "playing", False):
            with self._lock:
                if self._ours is not ours:
                    return None
                self._ours = None
            cond = self._conductor()
            if cond is not None:
                cond.intermission("the Programming screen", "")
                self._settle(cond)
            self._note("Programming playback ended. The show conductor "
                       "took the rig out of the show (flame cues zero, "
                       "lasers dark), so the next start releases the flame "
                       "cues again.", "ended")
            return "ended"
        if self._clock() - self._seen > WATCHDOG_S:
            if self.follow("the Programming watchdog", "", only=ours) \
                    is False:
                return self._poll()     # a show took over meanwhile
            self._note(f"Programming playback was stopped: no operator "
                       f"page has been open for {WATCHDOG_S:g} s. Press "
                       f"Play from to carry on.", "watchdog")
            return "watchdog"
        return None

    def settle_conductor(self):
        """Let the conductor's latest request land (a schedule Hold's step
        before the Programming Pause that follows it)."""
        cond = self._conductor()
        if cond is not None:
            self._settle(cond)

    @staticmethod
    def _settle(cond):
        """Let the conductor's out-of-the-show step land before anything
        newer can supersede it."""
        try:
            if getattr(cond, "threaded", False):
                cond.wait_idle(1.0)
            else:
                cond.run_pending()
        except Exception:                           # noqa: BLE001
            pass

    def _watch(self):
        if not self.threaded:
            return
        with self._lock:
            t = self._thread
            if t is not None and t.is_alive():
                return
            t = threading.Thread(target=self._run, daemon=True,
                                 name="ltcplay-programming-playhead")
            self._thread = t
        t.start()

    def _run(self):
        while True:
            time.sleep(POLL_S)
            try:
                self.poll()
            except Exception:                       # noqa: BLE001
                pass
            with self._lock:
                if self._ours is None:
                    self._thread = None
                    return
