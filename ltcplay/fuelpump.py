"""The fireball's fuel pump (Jeff, 2026-10-30): one switch on the flame
universe, turned on by hand for a duration Andy chooses first, and off by
itself at the end of it.

"Somewhere in the fire controls, we need a toggle for a fuel pump. This is
something for Andy to manually control as needed. It does not need to be
armed or disarmed but it should be affected by the stop button."
And: "Hold should not turn off the pump. Off on Stop, NOT Hold. Pump
should auto-stop after 5 minutes. Allow Andy to set duration before he
triggers it."

What it is
==========

flamesafe's config may list one passthrough entry that is a switch,
{"name": "Fireball Fuel Pump", "switch": 100, "on": 255}
(flamesafe/CONTRACT.md, "A switch"). The engine reads the same entry from
the same file (flamelink.pump_of); no entry, no pump and no button.
flamesafe writes the entry's own on value on the slot while the flame
frame names the pump in "hazers_on", the link is fresh and nothing has
taken it away since it was last seen OFF; the engine never writes a value
there (the slot is zero in every frame it sends).

How it always ends (the timed purge's machinery, #172)
======================================================

Three clocks, each enough on its own:

  1. This engine's timer (SEND_S): every pass checks every ending cause
     first (its time up, a stall since the last pass, an Abort standing, a
     Stop or Abort since it started, flamesafe's status stale, the flame
     link not sending), then re-asserts the pump on the flame link
     (pump_beat) under this module's lock with a fresh clock reading (a
     stall inside the pass ends it too), then checks the Abort count again
     (a Stop that landed during the pass ends it).
  2. The flame link's own hard end (pump_start), on the link's clock: past
     it, or with no beat for flamelink.PUMP_HOLD_S, the frame stops naming
     the pump on every frame it sends, and it never comes back without a
     new start, whatever keeps calling pump_beat. A wrecked or hung timer
     cannot keep it on.
  3. flamesafe: an engine that dies sends no frames, and flamesafe zeroes
     the slot at frame_stale_ms (500 ms).

The rules
=========

  - OFF at start, after Stop, after Abort (both send disarm_all, which
    switches it OFF at the link before the first copy, and flamesafe
    zeroes it on the disarm_all itself), when live flame programming ends
    (it sends disarm_all too), after an engine restart, when its time is
    up, and while flamesafe's status is not fresh or the flame link is not
    sending. NOT on Hold (Jeff: "Off on Stop, NOT Hold").
  - The duration: chosen before the press, a whole number of seconds, 5
    minutes unless Andy sets another before he turns it on (every screen
    starts at 5:00; nothing is remembered as a new default). The longest is
    pump_max_s there, MAX_DEFAULT_S (60 minutes) unless an admin sets it,
    never more than MAX_LIMIT_S: a longer one is refused in a sentence,
    never cut down silently. There is no unlimited.
  - It may run during a show (Jeff, 2026-10-30), between shows, in
    Programming: there is no show gate.
  - ON needs: the role matrix's arm row (users.classify), from the show
    network screen_arming too (remote.may_act_from), a press id never used
    before (a replayed or repeated request starts nothing), the pump not
    already on (a second ON never extends it), no Abort standing, no
    Abort or Stop still being sent to flamesafe (at least 0.75 s after the
    press), live flame programming off, the flame link sending,
    flamesafe's status fresh, its pump the same as the engine's (name,
    slot, on value), and the pump's slot not in the lockout file. A plain
    tap: the pump is not a flame.
  - OFF: always, anyone, signed in or not. An OFF press is its own body
    ("on" anything but true) and can never start the pump.
"""
import collections
import threading
import time


NAME = "Fuel pump"
STATUS_FRESH_S = 1.0        # flamesafe's status, as for the hazers
# A run flamesafe has not passed for this long ends (review A of #188, 5):
# flamesafe took it away (a frame gap, a restart) and needs it OFF then ON,
# so the screen would say "waiting" for the rest of the run.
NOT_PASSING_S = 1.5
# The scheduler's states for the end of the night (laserarm.py's): a run
# ends while the night is CLOSING, and when the night is over (OFF) if it
# began before that; ON is refused while CLOSING.
NIGHT_CLOSING = ("CLOSING",)
NIGHT_OVER = ("OFF",)
SEND_S = 0.1                # the timer's pass; the link drops it at PUMP_HOLD_S
# flamelink.PUMP_HOLD_S, the same number (the selftest holds them equal):
# not imported, so the remote's routes never load the flame link on an
# engine without one.
BEAT_STALE_S = 0.5
DEFAULT_S = 300             # Jeff: "auto-stop after 5 minutes"
MIN_S = 1
MAX_DEFAULT_S = 3600        # pump_max_s unless an admin sets it
MAX_LIMIT_S = 4 * 3600      # pump_max_s can never be more
RUN_IDS_KEPT = 64


def clock_text(seconds):
    """Whole seconds as m:ss ("5:00", "62:05")."""
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


def pump_seconds(v, max_s=MAX_DEFAULT_S):
    """(seconds, "") for the pump's duration as a screen sends it, a whole
    number from MIN_S to `max_s`, or (None, why) in a sentence. A longer
    one is refused, never cut down silently."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or \
            (isinstance(v, float) and not v.is_integer()):
        return None, ("The fuel pump needs its run time in whole seconds "
                      "(minutes and seconds on the screen). It was not "
                      "turned on.")
    v = int(v)
    if v < MIN_S:
        return None, (f"The fuel pump runs at least {MIN_S} s. It was not "
                      f"turned on.")
    if v > max_s:
        return None, (f"{clock_text(v)} is longer than this show machine "
                      f"allows for the fuel pump, {clock_text(max_s)} "
                      f"(pump_max_s in ltcplay_remote.json, which an admin "
                      f"can raise to at most {clock_text(MAX_LIMIT_S)}). It "
                      f"was not turned on.")
    return v, ""


class FuelPump:
    """The fuel pump's run, its refusals and its timer.

    link_get        () -> the engine's FlameLink, or None
    flames_get      () -> remote.FlameStatus.view() or None
    abort_standing  () -> True while an Abort is latched
    blocker         () -> why ON is refused right now, or "" (live flame
                    programming on)
    journal         (who, screen, outcome, text, fault=False)
    audit           audit.AuditLog or None
    clock           the engine's monotonic clock
    threaded        False for the selftest: pump_send() by hand"""

    def __init__(self, link_get, flames_get, abort_standing, blocker=None,
                 journal=None, audit=None, clock=time.monotonic,
                 threaded=True, night_state=None):
        self._link_get = link_get
        self._flames_get = flames_get
        self._abort_standing = abort_standing
        self._blocker = blocker or (lambda: "")
        self._journal = journal
        self.audit = audit
        self._clock = clock
        self._threaded = threaded
        # () -> the scheduler's state ("CLOSING", "OFF", ...), or None: the
        # night closing ends a run, and so does the night being over
        # (review of #188: end of night turns the pump off).
        self._night_state = night_state or (lambda: None)
        self._lock = threading.RLock()
        self.run = None             # the pump's run while it is ON here
        self._used = collections.deque(maxlen=RUN_IDS_KEPT)
        self.last_off = None        # {"reason", "who"}

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
                      action=action, target=self.name() or NAME,
                      result=result, reason=reason, source=screen or "engine")
        except Exception:
            pass

    def link(self):
        try:
            return self._link_get()
        except Exception:
            return None

    def config(self):
        """(name, slot, on value) from flamesafe's config, or None."""
        link = self.link()
        try:
            return link.cfg.pump if link is not None else None
        except Exception:
            return None

    def name(self):
        c = self.config()
        return c[0] if c else None

    def on(self):
        """ON here: a run under way and the flame link naming it. Read
        without the link's lock (pump_live_now), so the status never waits
        on a stuck sender (review B of #188, F5)."""
        link = self.link()
        try:
            return self.run is not None and link is not None and \
                bool(link.pump_live_now())
        except Exception:
            return False

    def _disarm_words(self, link):
        """Why the Abort count moved, in words: the flame link's own reason
        for the disarm_all when it had the pump on (Stop, Abort, Disarm
        all's fallback, the cleaning's disarm, live flame programming
        ending), else all of those (review B of #188, F6)."""
        try:
            ended = link.pump_ended()
        except Exception:
            ended = ""
        return ended or ("the disarm to every flame group was sent (a Stop, "
                         "an Abort, Disarm all's fallback, the cleaning's "
                         "disarm or the end of live flame programming)")

    def _flames(self):
        try:
            return self._flames_get() or None
        except Exception:
            return None

    def flamesafe_pump(self, fl=None):
        """flamesafe's own switch entries [(name, [slot], on)] from a fresh
        status, or None (no status, stale, or a flamesafe that reports no
        passthrough)."""
        fl = self._flames() if fl is None else fl
        if not fl or fl.get("stale"):
            return None
        pt = fl.get("passthrough")
        if not isinstance(pt, list):
            return None
        return [(str(p.get("name", "")), list(p.get("slots") or []),
                 p.get("on")) for p in pt if isinstance(p, dict)
                and p.get("kind") == "switch"]

    def mine(self):
        c = self.config()
        return [] if not c else [(c[0], [c[1]], c[2])]

    def _entry(self, fl):
        """flamesafe's status entry for the pump, or None."""
        n = self.name()
        if not fl or fl.get("stale") or n is None:
            return None
        for p in fl.get("passthrough") or []:
            if isinstance(p, dict) and p.get("name") == n:
                return p
        return None

    # -- why not ------------------------------------------------------------
    def down(self):
        """Why the pump must be OFF right now, in a sentence, or "". Checked
        on every pass of the timer, and the first thing ON is refused for."""
        link = self.link()
        if link is None or self.config() is None:
            return "there is no fuel pump in flamesafe's config."
        try:
            snap = link.snapshot()
        except Exception:
            snap = {}
        if not snap.get("open") or not snap.get("sending_ok"):
            return "the flame link to flamesafe is not sending."
        fl = self._flames()
        if not fl or not fl.get("connected"):
            return ("flamesafe's status is not connected to this engine, "
                    "so whether the pump runs is not known.")
        age = fl.get("age_ms")
        if fl.get("stale") or age is None or age > STATUS_FRESH_S * 1000:
            return (f"flamesafe has not reported in the last "
                    f"{STATUS_FRESH_S:g} s.")
        return ""

    def refusal(self):
        """Why the pump may not be turned ON now, in a sentence, or ""."""
        try:
            if self._abort_standing():
                return ("an Abort is standing. Press Reset, then turn it on "
                        "again.")
        except Exception:
            return "whether an Abort is standing could not be read."
        why = self.down()
        if why:
            return why
        link = self.link()
        try:
            repeating = link.abort_repeating()
        except Exception:
            repeating = True
        if repeating:
            return ("a Stop or Abort is still being sent to flamesafe. Turn "
                    "it on again in a moment.")
        try:
            why = self._blocker() or ""
        except Exception:
            why = "the engine's state could not be read."
        if why:
            return why
        if self._night() in NIGHT_CLOSING:
            return "the night is closing."
        c = self.config()
        try:
            locked = c[1] in (link.snapshot().get("locked_channels") or ())
        except Exception:
            locked = True
        if locked:
            return (f"its channel ({c[1]}) is in the lockout file, so "
                    f"flamesafe holds it at zero.")
        theirs = self.flamesafe_pump()
        if not theirs:
            return ("flamesafe does not report a fuel pump (an older "
                    "flamesafe, or another flamesafe.json), so it cannot be "
                    "turned on from here.")
        if sorted(theirs) != self.mine():
            return ("flamesafe's fuel pump and LTC Player's differ. Restart "
                    "both from the same flamesafe.json.")
        if not callable(getattr(link, "pump_start", None)):
            return "the flame link cannot take the pump's end time."
        return ""

    # -- the presses -------------------------------------------------------
    def set(self, on, who="", screen="", seconds=None, run=None,
            max_s=MAX_DEFAULT_S):
        """ON (on is True) for `seconds`, named by the press id `run`, or
        OFF: (ok, sentence). OFF is never refused and never starts it."""
        name = self.name()
        if name is None:
            return False, "There is no fuel pump in flamesafe's config."
        if on is not True:
            self.stop(f"{who or 'someone'} pressed Pump off on the {screen}",
                      who=who, screen=screen, system=False)
            return True, "The fuel pump is OFF."
        rid = run if isinstance(run, str) and 0 < len(run) <= 64 else None
        if rid is None:
            return False, ("The fuel pump was not turned on: the press has "
                           "to name itself. Press again.")
        with self._lock:
            used = rid in self._used
            self._used.append(rid)
        if used:
            return False, ("The fuel pump was not turned on: that press was "
                           "already used (a repeated request). Press again "
                           "if you mean to turn it on.")
        secs, why = pump_seconds(seconds, max_s)
        if not why:
            why = self.refusal()
            if why:
                why = f"The fuel pump was not turned on: {why}"
        if why:
            self._note(who, screen, "refused",
                       f"{who or 'Someone'} asked to turn the fuel pump on "
                       f"on the {screen}; refused: {why}")
            self._audit(who, screen, "fuel pump on", "refused", why)
            return False, why
        link = self.link()
        with self._lock:
            # The check and the reservation in one critical section (review
            # of #188, F1): a second ON never replaces or extends a run,
            # however close the two presses are.
            running = self.run
            ok = False
            if running is None:
                gen = link.pump_gen()
                now = self._clock()
                r = {"id": rid, "seconds": secs, "start": now, "beat": now,
                     "deadline": now + secs, "gen": gen, "who": who,
                     "screen": screen, "np": None,
                     "night": self._night()}
                # The hard end is the deadline itself, turned into the
                # link's clock under the link's lock (the link reads its
                # own clock first, this one second): a pause only tightens
                # it.
                ok = link.pump_start(secs, gen, deadline=r["deadline"],
                                     clock=self._clock) is True
                if ok:
                    self.run = r
        if running is not None:
            left = max(0.0, running["deadline"] - self._clock())
            return False, (f"The fuel pump is already on ({clock_text(left)} "
                           f"left). A second press never adds time: press "
                           f"Pump off, then start it again.")
        if not ok:
            why = ("The fuel pump was not turned on: the flame link did not "
                   "take it (a Stop or Abort landed). Press again in a "
                   "moment.")
            self._note(who, screen, "refused",
                       f"{who or 'Someone'} asked to turn the fuel pump on "
                       f"on the {screen}; refused: {why}")
            return False, why
        c = self.config()
        self._note(who, screen, "on",
                   f"{who or 'Someone'} turned the fuel pump ({name}, "
                   f"channel {c[1]}) on on the {screen} for "
                   f"{clock_text(secs)}. It turns off by itself then; Stop, "
                   f"Abort, Pump off and a lost link to flamesafe turn it "
                   f"off sooner. Hold does not.")
        self._audit(who, screen, "fuel pump on", "accepted",
                    f"{secs} s")
        why = self.pump_send()
        if why:
            return False, f"The fuel pump turned off at once: {why}."
        if self._threaded:
            threading.Thread(target=self._loop, args=(r,), daemon=True,
                             name="ltcplay-fuel-pump").start()
        return True, (f"The fuel pump is ON for {clock_text(secs)}. It "
                      f"turns off by itself.")

    def _night(self):
        try:
            return self._night_state()
        except Exception:
            return None

    def _end_reason(self, r, now):
        """Why a run must end now, or ""."""
        if now - r["beat"] > BEAT_STALE_S or now < r["beat"]:
            # The timer stalled (a pause, a hung thread): the flame link
            # has dropped it already. It never comes back by itself.
            return (f"the engine's timer stalled (nothing sent for "
                    f"{now - r['beat']:.2f} s)")
        if now >= r["deadline"]:
            return f"its {clock_text(r['seconds'])} were up"
        try:
            if self._abort_standing():
                return "an Abort is standing"
        except Exception:
            return "whether an Abort is standing could not be read"
        night = self._night()
        if night in NIGHT_CLOSING:
            return "the night is closing"
        if night in NIGHT_OVER and r.get("night") not in NIGHT_OVER:
            return "the night is over"
        link = self.link()
        try:
            if link.pump_gen() != r["gen"]:
                return self._disarm_words(link)
        except Exception:
            return "the flame link could not be read"
        why = self.down()
        if why:
            return why.rstrip(".")
        p = self._entry(self._flames())
        if p is not None and not p.get("passing"):
            since = r.get("np")
            if since is None:
                r["np"] = now
            elif now - since > NOT_PASSING_S:
                return (f"flamesafe did not pass it for {NOT_PASSING_S:g} s "
                        f"({p.get('why') or 'no reason given'}); press Fuel "
                        f"pump: On to start it again")
        else:
            r["np"] = None
        return ""

    def pump_send(self, now=None):
        """One pass of the timer (every SEND_S while it runs): every ending
        cause first, then the beat on the flame link with a fresh clock
        reading under this lock, then the Abort count again. "" or why it
        ended."""
        now = self._clock() if now is None else now
        with self._lock:
            r = self.run
        if r is None:
            return "it is not on"
        why = self._end_reason(r, now)
        if why:
            self._finish(r, why)
            return why
        link = self.link()
        with self._lock:
            if self.run is not r:
                return "it was turned off"
            # A fresh reading, taken here: the checks above can take long
            # (a pause inside this pass), and the gap since the last beat
            # is judged on it, never on `now` (as the timed purge, #172).
            fresh = self._clock()
            if fresh >= r["deadline"]:
                lost = f"its {clock_text(r['seconds'])} were up"
            elif fresh - r["beat"] > BEAT_STALE_S or fresh < r["beat"]:
                lost = (f"the engine's timer stalled (nothing sent for "
                        f"{fresh - r['beat']:.2f} s)")
            elif link.pump_beat(r["gen"]) is not True:
                ended = ""
                try:
                    ended = link.pump_ended()
                except Exception:
                    pass
                lost = ("the flame link turned it off" +
                        (f" ({ended})" if ended else ""))
            else:
                r["beat"] = fresh
                lost = "" if link.pump_gen() == r["gen"] else \
                    self._disarm_words(link)
        if lost:
            self._finish(r, lost)
        return lost

    def _loop(self, r):
        """The timer for one run. If it dies, the flame link stops naming
        the pump PUMP_HOLD_S after its last beat."""
        wait = threading.Event()
        try:
            while self.run is r:
                if self.pump_send():
                    return
                wait.wait(SEND_S)
        except Exception as e:
            self._finish(r, f"its timer raised {type(e).__name__}")

    def _finish(self, r, why, who="", screen="", system=True):
        """End run `r` (if it is still the run): OFF at the link, then the
        journal line. True when it ended it."""
        with self._lock:
            mine = self.run is r
            if mine:
                self.run = None
                link = self.link()
                if link is not None:
                    try:
                        link.pump_stop()
                    except Exception:
                        pass
        if mine:
            self._said_off(why, who, screen, system)
        return mine

    def _said_off(self, why, who="", screen="", system=True):
        why = " ".join(str(why or "Fuel pump off").split())[:200]
        by = who or ("system" if system else "unsigned")
        self.last_off = {"reason": why, "who": by}
        self._note(by, screen or "engine", "off", f"Fuel pump off: {why}.")
        self._audit(by, screen or "engine", "fuel pump off", "accepted", why,
                    system=system and not who)

    def stop(self, reason, who="", screen="", system=True):
        """The pump OFF now, for any reason. True when it was on."""
        with self._lock:
            r = self.run
        if r is not None:
            return self._finish(r, reason, who, screen, system)
        link = self.link()
        if link is not None:
            try:
                link.pump_stop()
            except Exception:
                pass
        return False

    def off(self, reason, who="", screen="", system=True, was=False):
        """For a Stop or Abort press, after cut(): the journal line when it
        was on. Never raises."""
        try:
            self.stop(reason, who, screen, system)
            if was:
                self._said_off(reason, who, screen, system)
            return bool(was)
        except Exception:
            return False

    def cut(self):
        """The cheap half, for a Stop or Abort press: OFF at the link and
        the run ended now, no journal, no disk. Returns whether it was on.
        Never raises."""
        try:
            link = self.link()
            if link is not None:
                link.pump_stop()
            with self._lock:
                was, self.run = self.run is not None, None
            return was
        except Exception:
            return False

    # -- the screens --------------------------------------------------------
    def view(self, fl=None):
        """For the screens: the pump's name, slot and on value; ON here or
        not; its state in words ("off", "waiting": ON here and flamesafe
        has not taken it yet, "on": flamesafe passes it); the time left on
        the engine's clock; flamesafe's why; why ON would be refused now
        ("" when it would be taken); whether flamesafe's pump agrees with
        the engine's; and the last thing that turned it off."""
        fl = self._flames() if fl is None else fl
        c = self.config()
        if c is None:
            return {"available": False}
        r = self.run
        on = self.on()
        p = self._entry(fl)
        if not on and p and p.get("passing"):
            # flamesafe still passes it while the engine has it off (review
            # A of #188, 7): said, never hidden; Pump off sends OFF again.
            state = "flamesafe_on"
        elif not on:
            state = "off"
        elif not p or not p.get("passing"):
            # flamesafe's word, never the engine's own bit.
            state = "waiting"
        else:
            state = "on"
        theirs = self.flamesafe_pump(fl)
        try:
            refusal = "" if on else self.refusal()
        except Exception as e:
            refusal = f"{type(e).__name__}"
        left = None
        if on and r is not None:
            left = round(max(0.0, r["deadline"] - self._clock()), 1)
        return {"available": True, "name": c[0], "slot": c[1],
                "on_value": c[2], "on": on, "state": state,
                "left_s": left,
                "seconds": r["seconds"] if on and r is not None else None,
                "why": str(p.get("why", "")) if p else None,
                "refusal": refusal, "down": self.down(),
                "agree": None if theirs is None else
                sorted(theirs) == self.mine(),
                "last_off": self.last_off}
