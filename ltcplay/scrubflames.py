"""Fire flames while scrubbing (Jeff, 2026-10-26; LIVE_FLAME_PROGRAMMING.md,
"Fire flames while scrubbing").

In a programming session (Run with Programming chosen on the Show file
card, then the Programming screen's transport: Play from, Jump, Pause,
Continue, the A/B loop) the show's own flame cues, read from the show's
FSEQ at the transport position (fire_ice.FlameCues.at_transport), go to
flamesafe on the engine's existing flame link (flamelink.FlameLink
rehearse_begin, beside program_set and purge_set). flamesafe is not
changed and stays the only program that talks to the flame node: it fires
only a group that is armed, and its lockouts, Abort latch, stale cutoffs
and hold to arm all apply exactly as in a show.

The rules, in one place:

  - OFF every time the engine starts, and off until someone with the
    Settings permission turns it on, confirmed, on the show machine itself
    (never from an iPad). Anyone may turn it off, from any screen, signed
    in or not: it only takes risk away.
  - Only in a programming session with no scheduled show live, no Hold
    and no Abort standing, the show clock playing no cue, the flame link
    sending, no purge, and live flame programming off. Live flame
    programming refuses to start while this is on, and this refuses while
    it is on: one source of fire values at a time.
  - Values only while the Programming screen's transport is PLAYING: a
    free run that is not paused, with no Blackout, Preshow or look on the
    pixels. Paused, stopped, following timecode, or the show clock playing:
    zero.
  - The seek guard, always (flamelink.SEEK_SETTLE_S, SEEK_JUMP_S): after
    turning it on, after every Play from, Jump, loop wrap, Continue or any
    other move, every flame value is zero until the position has run
    steadily for 0.5 s, and a flame cue already under way where it landed
    stays zero until it has ended. A scrub drag never fires anything on
    the way, and a cue whose start was jumped over never fires.
  - The live show's own cue path (FlameCues.__call__, FlameLink.values_now
    below its first line) is not used and not changed.
  - It ends by itself, with the reason in the journal and the audit log,
    on Abort, Hold, Start now, Run, Stop, a show going live (the scheduler
    in SHOW or PAUSED, or the show clock playing a cue), anything that
    zeroes the flame cues at the link, live flame programming starting,
    or the session no longer being a programming session.
  - Ending it sets every flame value to zero at once. It does not disarm
    any group: the groups armed for the session stay armed, as after a
    show; disarm them on the Stream Deck.
"""
import threading
import time

NAME = "Fire flames while scrubbing"
WATCH_S = 0.05            # how often the watch thread checks the rules
TC_FPS = 30               # the flame link's seek guard counts 30 a second


def tc_text(seconds):
    """Show seconds as the flame link's timecode text, 30 frames a second
    ("hh:mm:ss:ff"), or None for no position."""
    try:
        t = float(seconds)
    except (TypeError, ValueError):
        return None
    if t != t or t < 0 or t >= 100 * 3600:
        return None
    frames = int(t * TC_FPS + 1e-6)
    h, rem = divmod(frames, TC_FPS * 3600)
    m, rem = divmod(rem, TC_FPS * 60)
    s, f = divmod(rem, TC_FPS)
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


class ScrubFlames:
    """The mode: on and off, its per frame position, and its watch.

    link_get   () -> the engine's FlameLink, or None
    cues_get   () -> the engine's fire_ice.FlameCues, or None
    control    web.Control (its .session is the show file running)
    blockers   () -> why the engine forbids the mode now, or ""
               (remote.scrub_flames_blockers: entry and every watch pass)
    journal    (who, screen, outcome, text, fault=False)
    audit      audit.AuditLog or None
    threaded   False for the selftest: check() by hand"""

    def __init__(self, link_get, cues_get, control, blockers, journal=None,
                 audit=None, clock=time.monotonic, wall=time.time,
                 threaded=True):
        self._link_get = link_get
        self._cues_get = cues_get
        self.control = control
        self._blockers = blockers
        self._journal = journal
        self.audit = audit
        self._clock = clock
        self._wall = wall
        self._threaded = threaded
        self._lock = threading.RLock()
        self.on = False
        self._gen = 0
        self._cut_epoch = 0           # +1 on every cut(), lock-free
        self._session = None          # the show file's Session it began on
        self._link_session = None     # the flame link's rehearse session
        self._thread = None
        self.who = ""
        self.screen = ""
        self.began = None
        self.last_end = None          # {"reason", "who", "at", "started_by"}
        self.ends = 0

    # -- plumbing ---------------------------------------------------------
    def _note(self, who, screen, outcome, text, fault=False):
        if self._journal is None:
            return
        try:
            self._journal(who, screen, outcome, text, fault=fault)
        except Exception:
            pass

    def _audit(self, who, screen, action, result, reason, system=False):
        log = self.audit
        if log is None:
            return
        try:
            from . import audit as audit_mod
            log.event(user=audit_mod.SYSTEM if system else
                      (who or audit_mod.UNSIGNED), surface="engine",
                      action=action, target="fire flames while scrubbing",
                      result=result, reason=reason, source=screen or "engine")
        except Exception:
            pass

    def link(self):
        try:
            return self._link_get()
        except Exception:
            return None

    def cues(self):
        try:
            return self._cues_get()
        except Exception:
            return None

    # -- on -----------------------------------------------------------------
    def refusal(self):
        """Why it may not be turned on now, in a sentence, or ""."""
        try:
            why = self._blockers() or ""
        except Exception as e:
            why = (f"the engine's state could not be read "
                   f"({type(e).__name__}).")
        if why:
            return why
        link = self.link()
        if link is None or not hasattr(link, "rehearse_begin"):
            return ("no flame link is connected in this engine, so nothing "
                    "could reach flamesafe.")
        c = self.cues()
        if c is None or not hasattr(c, "at_transport"):
            return ("no flame controller is set in the Fire & Ice settings, "
                    "so the show has no flame cues to read.")
        return ""

    def _refused(self, who, screen, why):
        self._note(who, screen, "refused",
                   f"{who or 'Someone'} asked to turn {NAME} on on the "
                   f"{screen}; refused: {why}")
        self._audit(who, screen, "scrub flames on", "refused", why)
        return False, f"{NAME} was not turned on: {why}"

    def begin(self, who, screen):
        """(ok, sentence). On after every check."""
        with self._lock:
            if self.on:
                return False, f"{NAME} is already on (since {self.who})."
            epoch = self._cut_epoch
        why = self.refusal()
        if why:
            return self._refused(who, screen, why)
        link, c = self.link(), self.cues()
        sess = getattr(self.control, "session", None)
        with self._lock:
            if self.on:
                return False, f"{NAME} is already on (since {self.who})."
            n = link.rehearse_begin(self.state, c.at_transport)
            if n is None:
                why = ("live flame programming is on. Turn it off first: "
                       "only one of the two can be on at a time.")
            elif self._cut_epoch != epoch:
                # An Abort, Hold or Start now landed while this was being
                # turned on: it wins.
                link.rehearse_end()
                why = ("an Abort, Hold or Start now was pressed while it "
                       "was starting.")
            else:
                self.on = True
                self._gen += 1
                gen = self._gen
                self._session = sess
                self._link_session = n
                self.who, self.screen = who or "unsigned", screen
                self.began = self._clock()
                if self._threaded:
                    self._thread = threading.Thread(
                        target=self._watch, args=(gen,), daemon=True,
                        name="ltcplay-scrub-flames")
                    self._thread.start()
        if why:
            return self._refused(who, screen, why)
        text = (f"{self.who} turned {NAME} ON on the {screen}. The show's "
                f"flame cues now go to flamesafe at the Programming "
                f"screen's position while it is playing. Only an armed "
                f"group fires; flamesafe's lockouts and Abort apply. Zero "
                f"while paused, and for 0.5 s after every jump or loop. "
                f"Abort, Hold, Start now, Run, Stop and a show going live "
                f"turn it off.")
        self._note(self.who, screen, "on", text)
        self._audit(self.who, screen, "scrub flames on", "accepted", "")
        return True, text

    # -- every frame (the flame link's sender) --------------------------------
    def state(self):
        """The transport position as the flame link's timecode text, or
        None (zeros): off, another session than the one it was turned on
        in, nothing running, no player, a Blackout, Preshow or look, not
        on the transport's free run, or paused. Never raises, never waits
        (plain reads only)."""
        try:
            if not self.on:
                return None
            s = getattr(self.control, "session", None)
            if s is None or s is not self._session or \
                    not getattr(s, "running", False):
                return None
            p = getattr(s, "player", None)
            if p is None or getattr(p, "override", None) is not None:
                return None
            if getattr(p, "freerun_epoch", None) is None or \
                    getattr(p, "freerun_paused_at", None) is not None:
                return None
            return tc_text(getattr(p, "tc_seconds", None))
        except Exception:
            return None

    # -- off ----------------------------------------------------------------
    def cut(self, reason):
        """The cheap half of ending, for an Abort, Hold or Start now press:
        the flame link's slot shut now (one zero frame, no journal, no
        disk), never taking the mode's lock. end() does the rest. True when
        it was on. Never raises."""
        self._cut_epoch += 1
        was = self.on
        link = self.link()
        if link is not None:
            try:
                link.rehearse_end()
            except Exception:
                pass
        return was

    def end(self, reason, who="", screen="", system=True):
        """Off, for any reason: the link's slot shut and every value zero.
        No group is disarmed. True when it was on. Never raises."""
        why = " ".join(str(reason or "turned off").split())[:200]
        by = who or ("system" if system else "unsigned")
        with self._lock:
            if not self.on:
                return False
            self.on = False
            self.ends += 1
            self.last_end = {"reason": why, "who": by,
                             "at": int(self._wall() * 1000),
                             "started_by": self.who}
            self._gen += 1
            self._session = None
            self._thread = None
        link = self.link()
        if link is not None:
            try:
                link.rehearse_end()
            except Exception:
                pass
        self._note(by, screen or "engine", "off",
                   f"{NAME} is OFF: {why}. Every flame value from the scrub "
                   f"is zero. No flame group was disarmed: disarm on the "
                   f"Stream Deck when you are done.")
        self._audit(by, screen or "engine", "scrub flames off", "accepted",
                    why, system=system and not who)
        return True

    def end_reason(self):
        """Why the mode must end now, or "". The watch pass."""
        if not self.on:
            return ""
        link = self.link()
        if link is None or not getattr(link, "rehearse_on", False) or \
                getattr(link, "rehearse_session", None) != \
                self._link_session:
            return ("the flame cues were zeroed at the flame link (a Hold, "
                    "an Abort, a show's own zero, or live flame programming "
                    "starting)")
        s = getattr(self.control, "session", None)
        if s is None or s is not self._session or \
                not getattr(s, "running", False):
            return "the show file stopped (Stop or Run)"
        try:
            why = self._blockers() or ""
        except Exception as e:
            why = (f"the engine's state could not be read "
                   f"({type(e).__name__})")
        return why

    def check(self):
        """One watch pass: end the mode if anything says so. True when it
        ended it. Never raises."""
        try:
            why = self.end_reason()
        except Exception as e:
            why = f"its check failed ({type(e).__name__})"
        if why and self.on:
            return self.end(why)
        return False

    def _watch(self, gen):
        while True:
            time.sleep(WATCH_S)
            with self._lock:
                if not self.on or self._gen != gen:
                    return
            self.check()

    # -- the screens --------------------------------------------------------
    def view(self):
        out = {"available": True, "on": self.on, "who": self.who,
               "screen": self.screen, "last_end": self.last_end}
        if self.on:
            began = self.began
            out["since_s"] = (round(self._clock() - began, 1)
                              if began is not None else None)
            out["position"] = self.state()
            link = self.link()
            out["firing"] = bool(getattr(link, "last_values_nonzero",
                                         False))
            c = self.cues()
            out["fault"] = str(getattr(c, "scrub_fault", "") or "")
        else:
            out["blocked"] = self.refusal()
        return out
