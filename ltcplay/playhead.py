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
"""


class PlayheadError(ValueError):
    """A transport press that cannot be done, in a sentence."""


class ShowAudioPlayhead:
    """The Programming transport on the show audio. `control` is
    web.Control (the running session is looked up on every call);
    `schedule` the scheduler service, whose `conductor` (Fire & Ice) is
    used when there is one."""

    def __init__(self, control, schedule=None):
        self.control = control
        self.schedule = schedule

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
        s = self._session()
        cond = self._conductor()
        if cond is not None and getattr(cond, "latched", False):
            # The show runner's own rule: nothing starts while an Abort
            # stands.
            raise PlayheadError("An Abort is standing, so nothing plays "
                                "until someone presses Reset.")
        mm = getattr(getattr(cond, "devices", None), "mm", None)
        if mm is not None:
            try:
                mm.select_bank(mm.cfg.show_bank, wait=False)
            except Exception as e:                  # noqa: BLE001
                note = getattr(cond, "_note", None)
                if callable(note):
                    note(f"Selecting the MadMapper show bank failed: {e}",
                         fault=True, action="video", outcome="failed")
        _cue, at = s.clock_play_from(show_s)
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
        return s.clock_seek(max(0.0, here + float(seconds)))

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

    def follow(self, who="", screen=""):
        """Stop the playhead: the conductor's out-of-the-show look first
        (flame cues zero, lasers blanked), then the show audio stops."""
        s = self._session()
        cond = self._conductor()
        if cond is not None:
            cond.intermission(who, screen)
        if s.clock_here() is not None:
            s.clock_halt()
        return None
