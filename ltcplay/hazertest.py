"""The hazer test (Jeff, 2026-11-11): "add a hazer purge button, so to
speak. Same page as the other purges but more like a hazer test that
toggles on/off even when the show isn't running."

What it is
==========

One tap turns every hazer in flamesafe's config ON at its stored Haze and
Fan levels (hazers.py, the rack screen's Levels), with no show playing; a
second tap turns it OFF. It never arms a hazer: ARMED (which hazers run
during a show) is untouched, and when the test ends only the ARMED ones
are named to flamesafe again (with no show playing, at zero).

The flame link carries it exactly as it carries the fuel pump
(flamelink.hazer_test_start / hazer_test_beat): the frame names every
hazer in "hazers_on" and writes its levels on its two slots only while the
engine's timer beats it and inside its hard end on the link's own clock.
flamesafe needs nothing new: it passes a hazer's slots while the frame
names it, the link is fresh and no Abort has stood since it last saw it
OFF (flamesafe/CONTRACT.md, "Passthrough: the hazers").

How it always ends
==================

Three clocks, each enough on its own, as the fuel pump:

  1. This engine's timer (SEND_S): every pass checks every ending cause
     (below), then beats the link.
  2. The flame link's hard end and beat (HAZER_TEST_HOLD_S): a wrecked or
     hung timer cannot keep it on.
  3. flamesafe: an engine that dies (or shuts down) sends no frames, and
     flamesafe zeroes every hazer slot at frame_stale_ms.

The rules
=========

  - OFF at start and after an engine restart.
  - Ends by itself after MAX_S (60 s), with a countdown on the screen.
  - Ends on Stop and Abort (disarm_all ends it at the link before its
    first copy), the page that started it going quiet for PAGE_QUIET_S
    (its beat), a show starting or held, show control turning on, live
    flame programming, the starter's sign-in ending, flamesafe's status
    not fresh, the flame link not sending, flamesafe not passing it for
    NOT_PASSING_S, or any error (fail closed: OFF, and said).
  - ON needs the role matrix's arm row (users.classify "hazer-test", the
    same row as arming a hazer), from the show network screen_arming too
    (remote.may_act_from), and every check that arming a hazer has today
    (hazers.Hazers.refusal: the flame link sending, flamesafe fresh, no
    Stop still being sent, live flame programming off, flamesafe's hazers
    the same as the engine's), and no show playing or held and show
    control off (during a show the show owns the hazers: ARMED ones run at
    their levels, DISARMED ones are off on purpose), and at least one
    hazer with a Haze level above 0.
  - OFF: always, anyone, signed in or not.
"""
import collections
import secrets
import threading
import time

NAME = "Hazer test"
MAX_S = 60                  # Jeff decides; a forgotten test never fogs on
SEND_S = 0.1                # the timer's pass
# flamelink.HAZER_TEST_HOLD_S, the same number (the selftest holds them
# equal): not imported, so the routes never load the flame link.
BEAT_STALE_S = 0.5
PAGE_QUIET_S = 10.0         # the page's beat missing this long ends it
PAGE_BEAT_S = 2.0           # how often the page beats (remote2.html)
NOT_PASSING_S = 1.5         # as the fuel pump
RUN_IDS_KEPT = 64


class HazerTest:
    """The hazer test's run, its refusals and its timer.

    hazers          () -> the engine's hazers.Hazers, or None
    show_busy       () -> why a show owns the hazers now, or ""
    signed_out      (token, who) -> why the starter's sign-in ended, or ""
    journal         (who, screen, outcome, text, fault=False)
    audit           audit.AuditLog or None
    clock           the engine's monotonic clock
    threaded        False for the selftest: send() by hand"""

    def __init__(self, hazers, show_busy=None, signed_out=None,
                 journal=None, audit=None, clock=time.monotonic,
                 threaded=True, gate=None):
        self._hazers = hazers
        # () -> True while the test may haze, asked by the flame link on
        # every frame (flamelink.hazer_test_gate). None: no gate.
        self._gate = gate
        self._show_busy = show_busy or (lambda: "")
        self._signed_out = signed_out or (lambda token, who: "")
        self._journal = journal
        self.audit = audit
        self._clock = clock
        self._threaded = threaded
        self._lock = threading.RLock()
        self.run = None
        self._off_gen = 0
        self._used = collections.deque(maxlen=RUN_IDS_KEPT)
        self.last_off = None        # {"reason", "who", "n", "boot"}
        self._offs = 0
        self.boot = secrets.token_hex(4)

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
                      action=action, target=NAME, result=result,
                      reason=reason, source=screen or "engine")
        except Exception:
            pass

    def hz(self):
        try:
            return self._hazers()
        except Exception:
            return None

    def link(self):
        hz = self.hz()
        return hz.link() if hz is not None else None

    def names(self):
        hz = self.hz()
        return hz.names() if hz is not None else []

    def on(self):
        """ON here: a run under way and the flame link carrying it."""
        link = self.link()
        try:
            return self.run is not None and link is not None and \
                bool(link.hazer_test_live_now())
        except Exception:
            return False

    def _busy(self):
        try:
            return self._show_busy() or ""
        except Exception:
            return "the show's state could not be read"

    def _sign_in_gone(self, token, who):
        try:
            return self._signed_out(token or "", who or "") or ""
        except Exception:
            return "whether the sign-in is still live could not be read"

    def _passing(self):
        """{name: passing} from flamesafe's fresh status."""
        hz = self.hz()
        try:
            fl = hz._flames()
        except Exception:
            fl = None
        if not fl or fl.get("stale"):
            return {}
        return {str(p.get("name")): bool(p.get("passing"))
                for p in fl.get("passthrough") or [] if isinstance(p, dict)}

    # -- why not ------------------------------------------------------------
    def refusal(self):
        """Why the test may not start now, in a sentence, or ""."""
        hz = self.hz()
        names = self.names()
        if hz is None or not names:
            return "there are no hazers in flamesafe's config."
        busy = self._busy()
        if busy:
            return busy.rstrip(".") + "."
        for n in names:
            why = hz.refusal(n)
            if why:
                return why
        lv = hz.levels.levels
        if not any(lv.get(n, (0, 0))[0] > 0 for n in names):
            return "every hazer's Haze level is 0. Set a level first."
        if not callable(getattr(self.link(), "hazer_test_start", None)):
            return "the flame link cannot run a hazer test."
        return ""

    # -- the presses -------------------------------------------------------
    def start(self, who="", screen="", run=None, token=""):
        """ON for MAX_S, named by the press id `run`: (ok, sentence)."""
        rid = run if isinstance(run, str) and 0 < len(run) <= 64 else None
        if rid is None:
            return False, "Not started: press again."
        with self._lock:
            used = rid in self._used
            self._used.append(rid)
            off0 = self._off_gen
            running = self.run
        if used:
            return False, "Not started: that press was already used."
        if running is not None:
            return False, "The hazer test is already on."
        why = self.refusal() or self._sign_in_gone(token, who)
        if why:
            why = f"Hazer test not started: {why}"
            self._note(who, screen, "refused",
                       f"{who or 'Someone'} asked for the hazer test on the "
                       f"{screen}; refused: {why}")
            self._audit(who, screen, "hazer test on", "refused", why)
            return False, why
        link = self.link()
        ok = False
        with self._lock:
            if self.run is None and self._off_gen == off0:
                try:
                    gen = link.abort_id
                    link.hazer_test_gate = self._gate
                    now = self._clock()
                    r = {"id": rid, "start": now, "beat": now, "page": now,
                         "deadline": now + MAX_S, "gen": gen, "who": who,
                         "screen": screen, "np": None, "token": token or ""}
                    ok = link.hazer_test_start(MAX_S, gen,
                                               deadline=r["deadline"],
                                               clock=self._clock) is True
                except Exception:
                    ok = False
                    try:
                        link.hazer_test_stop()
                    except Exception:
                        pass
                if ok:
                    self.run = r
        if not ok:
            why = ("Hazer test not started: the flame link did not take it "
                   "(a Stop landed?). Press again in a moment.")
            self._note(who, screen, "refused",
                       f"{who or 'Someone'} asked for the hazer test on the "
                       f"{screen}; refused: {why}")
            self._audit(who, screen, "hazer test on", "refused", why)
            return False, why
        self._note(who, screen, "on",
                   f"{who or 'Someone'} turned the hazer test on on the "
                   f"{screen} ({', '.join(self.names())}) for {MAX_S} s at "
                   f"their levels. It turns off by itself then; Stop, Hazer "
                   f"test off and a quiet screen turn it off sooner.")
        self._audit(who, screen, "hazer test on", "accepted", f"{MAX_S} s")
        why = self.send()
        if why:
            return False, f"The hazer test turned off at once: {why}."
        if self._threaded:
            threading.Thread(target=self._loop, args=(r,), daemon=True,
                             name="ltcplay-hazer-test").start()
        return True, f"Hazer test ON for {MAX_S} s."

    def beat(self, run):
        """The page that started it is still there. (ok, sentence). A beat
        never starts anything and never moves the end."""
        with self._lock:
            r = self.run
            if r is None or not isinstance(run, str) or r["id"] != run:
                return False, "The hazer test is off."
            r["page"] = self._clock()
        return True, "Hazer test on."

    def _end_reason(self, r, now):
        if now - r["beat"] > BEAT_STALE_S or now < r["beat"]:
            return (f"the engine's timer stalled (nothing sent for "
                    f"{now - r['beat']:.2f} s)")
        if now >= r["deadline"]:
            return f"its {MAX_S} s were up"
        if now - r["page"] > PAGE_QUIET_S:
            return (f"the screen that started it went quiet for "
                    f"{PAGE_QUIET_S:g} s")
        gone = self._sign_in_gone(r.get("token", ""), r.get("who", ""))
        if gone:
            return gone
        busy = self._busy()
        if busy:
            return busy.rstrip(".")
        link = self.link()
        try:
            if link.abort_id != r["gen"]:
                return (link.hazer_test_ended() or
                        "the disarm to every flame group was sent (a Stop)")
        except Exception:
            return "the flame link could not be read"
        hz = self.hz()
        try:
            why = hz.down() if hz is not None else "the hazers are gone"
            if not why:
                why = hz._blocker() or ""
        except Exception as e:
            why = f"its check failed ({type(e).__name__})"
        if why:
            return why.rstrip(".")
        passing = self._passing()
        if not all(passing.get(n) for n in self.names()):
            since = r.get("np")
            if since is None:
                r["np"] = now
            elif now - since > NOT_PASSING_S:
                return f"flamesafe did not pass it for {NOT_PASSING_S:g} s"
        else:
            r["np"] = None
        return ""

    def send(self, now=None):
        """One pass of the timer: every ending cause, then the beat on the
        flame link. "" or why it ended. Never raises."""
        try:
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
                fresh = self._clock()
                if fresh >= r["deadline"]:
                    lost = f"its {MAX_S} s were up"
                elif fresh - r["beat"] > BEAT_STALE_S or fresh < r["beat"]:
                    lost = (f"the engine's timer stalled (nothing sent for "
                            f"{fresh - r['beat']:.2f} s)")
                elif link.hazer_test_beat(r["gen"]) is not True:
                    lost = ("the flame link turned it off" +
                            (f" ({link.hazer_test_ended()})"
                             if link.hazer_test_ended() else ""))
                else:
                    r["beat"] = fresh
                    lost = ""
            if lost:
                self._finish(r, lost)
            return lost
        except Exception as e:
            why = f"its check failed ({type(e).__name__})"
            self.stop(why)
            return why

    def _loop(self, r):
        wait = threading.Event()
        try:
            while self.run is r:
                if self.send():
                    return
                wait.wait(SEND_S)
        except Exception as e:
            self._finish(r, f"its timer raised {type(e).__name__}")

    def _finish(self, r, why, who="", screen="", system=True):
        with self._lock:
            mine = self.run is r
            if mine:
                self.run = None
            self._link_off()
        if mine:
            self._said_off(why, who, screen, system)
        return mine

    def _link_off(self):
        link = self.link()
        if link is not None:
            try:
                link.hazer_test_stop()
            except Exception:
                pass

    def _said_off(self, why, who="", screen="", system=True):
        why = " ".join(str(why or "Hazer test off").split())[:200]
        by = who or ("system" if system else "unsigned")
        with self._lock:
            self._offs += 1
            self.last_off = {"reason": why, "who": by, "n": self._offs,
                             "boot": self.boot}
        self._note(by, screen or "engine", "off", f"Hazer test off: {why}.")
        self._audit(by, screen or "engine", "hazer test off", "accepted",
                    why, system=system and not who)

    def stop(self, reason, who="", screen="", system=True):
        """OFF now, for any reason. True when it was on. Never raises."""
        try:
            with self._lock:
                self._off_gen += 1
                r = self.run
            if r is not None:
                return self._finish(r, reason, who, screen, system)
            self._link_off()
        except Exception:
            pass
        return False

    def cut(self):
        """For a Stop or Abort press: OFF at the link now, no journal.
        Returns whether it was on. Never raises."""
        try:
            self._link_off()
            with self._lock:
                self._off_gen += 1
                was, self.run = self.run is not None, None
            return was
        except Exception:
            return False

    def off(self, reason, who="", screen="", system=True, was=False):
        """After cut(): the journal line when it was on. Never raises."""
        try:
            self.stop(reason, who, screen, system)
            if was:
                self._said_off(reason, who, screen, system)
            return bool(was)
        except Exception:
            return False

    def end_for_token(self, token, why, who="", screen=""):
        try:
            r = self.run
            if r is None or not token or r.get("token") != token:
                return False
            return self._finish(r, why, who, screen, system=not who)
        except Exception:
            return False

    # -- the screens --------------------------------------------------------
    def view(self):
        """For the screens: ON or not (the engine's word: the run AND the
        flame link carrying it), "off", "waiting" (flamesafe has not passed
        it yet) or "on", the time left, why ON would be refused now, and the
        last thing that turned it off. Fails closed: an error is OFF."""
        try:
            names = self.names()
            if not names:
                return {"available": False}
            r = self.run
            on = self.on()
            passing = self._passing()
            state = "off" if not on else (
                "on" if all(passing.get(n) for n in names) else "waiting")
            left = None
            if on and r is not None:
                left = round(max(0.0, r["deadline"] - self._clock()), 1)
            try:
                refusal = "" if on else self.refusal()
            except Exception as e:
                refusal = f"its check failed ({type(e).__name__})."
            return {"available": True, "names": names, "on": on,
                    "state": state, "left_s": left, "max_s": MAX_S,
                    "beat_s": PAGE_BEAT_S, "refusal": refusal,
                    "last_off": self.last_off, "boot": self.boot}
        except Exception as e:
            # Fail closed: a state that cannot be read is turned OFF.
            self.stop(f"its state could not be read ({type(e).__name__})")
            return {"available": True, "on": False, "state": "unknown",
                    "error": f"{type(e).__name__}", "max_s": MAX_S}
