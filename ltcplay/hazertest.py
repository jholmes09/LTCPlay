"""The hazer test (Jeff, 2026-11-11, per hazer 2026-11-13): "add a hazer
purge button, so to speak. Same page as the other purges but more like a
hazer test that toggles on/off even when the show isn't running." And:
"Hazers are meant to toggle. On the Purge page, we select the hazers, and
the levels are shown and the option to turn OFF hazers but not ON."

What it is
==========

The operator selects any of the hazers in flamesafe's config (each shown
with its stored Haze and Fan level, the Levels sheet's numbers) and turns
the SELECTED ones on at those levels, with no show playing, or off. Each
hazer the test turns on runs for MAX_S from its own press, never longer: a
second press never extends a hazer already running. Hazers not selected
stay exactly as they were. It never turns a hazer On in the show sense
(hazers.py "ARMED": runs at its levels while a show plays); that is
untouched, and when the test ends only those are named to flamesafe again
(with no show playing, at zero).

The flame link carries it exactly as it carries the fuel pump
(flamelink.hazer_test_start / hazer_test_beat): the frame names each test
hazer in "hazers_on" and writes its levels on its two slots only while the
engine's timer beats it, inside that hazer's own hard end on the link's
clock. flamesafe needs nothing new (flamesafe/CONTRACT.md, "Passthrough:
the hazers").

How it always ends
==================

Three clocks, each enough on its own, as the fuel pump:

  1. This engine's timer (SEND_S): every pass checks every ending cause
     (below), then beats the link.
  2. The flame link's hard end per hazer and its beat (HAZER_TEST_HOLD_S):
     a wrecked or hung timer cannot keep one on.
  3. flamesafe: an engine that dies (or shuts down) sends no frames, and
     flamesafe zeroes every hazer slot at frame_stale_ms.

The rules
=========

  - OFF at start and after an engine restart.
  - Each hazer ends by itself MAX_S (60 s) after the press that turned it
    on, with a countdown on the screen; its starter's sign-in ending, or
    flamesafe not passing it for NOT_PASSING_S, ends that hazer.
  - Every hazer of the test ends on Stop and Abort (disarm_all ends it at
    the link before its first copy), a Hold press (hazer_test_cut) and
    a Hold's zero, Hazers off, the page that started it going quiet for
    PAGE_QUIET_S (its beat), a show starting or paused mid show, show
    control turning on, live flame programming, flamesafe's status not fresh, the flame
    link not sending, or any error (fail closed: OFF, and said).
  - ON names its hazers (validated against the engine's own hazer list; a
    name it does not have refuses the whole press) and needs the role
    matrix's arm row (users.classify "hazer-test", the row that turns a
    hazer On for the show), from the show network screen_arming too
    (remote.may_act_from), every check that turning a hazer On has today
    (hazers.Hazers.refusal), no show playing or paused mid show (on the
    schedule's Hold, after a Stop, and with no schedule loaded it may run:
    Jeff, 2026-11-14), show control off, and a Haze level above 0 on
    each selected hazer.
  - OFF: always, anyone, signed in or not; for the named hazers, or all.
    An OFF naming a hazer the engine does not have turns every one off.
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
NAMES_MAX = 16


def names_of(raw, known):
    """(names, "") for a press's "hazers": a non-empty list of names the
    engine has (duplicates dropped, in the engine's order), or (None,
    why) in a sentence. A name the engine does not have refuses the whole
    list."""
    if not isinstance(raw, list) or not raw or len(raw) > NAMES_MAX or \
            not all(isinstance(n, str) for n in raw):
        return None, "select one hazer or more."
    bad = [n for n in raw if n not in known]
    if bad:
        return None, (f"{', '.join(repr(n) for n in bad[:3])} is not a "
                      f"hazer in flamesafe's config. Nothing was changed.")
    return [n for n in known if n in raw], ""


class HazerTest:
    """The hazer test's runs, its refusals and its timer.

    hazers          () -> the engine's hazers.Hazers, or None
    show_busy       () -> why a show owns the hazers now, or ""
    signed_out      (token, who) -> why a starter's sign-in ended, or ""
    journal         (who, screen, outcome, text, fault=False)
    audit           audit.AuditLog or None
    clock           the engine's monotonic clock
    threaded        False for the selftest: send() by hand
    gate            () -> True while the test may haze, asked by the flame
                    link on every frame (flamelink.hazer_test_gate)"""

    def __init__(self, hazers, show_busy=None, signed_out=None,
                 journal=None, audit=None, clock=time.monotonic,
                 threaded=True, gate=None):
        self._hazers = hazers
        self._gate = gate
        self._show_busy = show_busy or (lambda: "")
        self._signed_out = signed_out or (lambda token, who: "")
        self._journal = journal
        self.audit = audit
        self._clock = clock
        self._threaded = threaded
        self._lock = threading.RLock()
        # The run while any hazer is on: {"beat", "gen", "page", "ids",
        # "names": {name: {"deadline", "who", "token", "screen", "np"}}}.
        self.run = None
        self._off_gen = 0
        self._used = collections.deque(maxlen=RUN_IDS_KEPT)
        self.last_off = None        # {"reason", "who", "names", "n", "boot"}
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

    def _audit(self, who, screen, action, target, result, reason,
               system=False):
        log = self.audit
        if log is None:
            return
        try:
            from . import audit as audit_mod
            log.event(user=audit_mod.SYSTEM if system else
                      (who or audit_mod.UNSIGNED), surface="engine",
                      action=action, target=target or NAME, result=result,
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

    def running(self):
        """The names on here: the run's AND the flame link's."""
        link = self.link()
        r = self.run
        try:
            if r is None or link is None:
                return set()
            return set(r["names"]) & set(link.hazer_test_live_now())
        except Exception:
            return set()

    def on(self):
        return bool(self.running())

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
    def refusal(self, names=None):
        """Why `names` (every hazer when None) may not be turned on now, in
        a sentence, or ""."""
        hz = self.hz()
        known = self.names()
        if hz is None or not known:
            return "there are no hazers in flamesafe's config."
        names = known if names is None else names
        busy = self._busy()
        if busy:
            return busy.rstrip(".") + "."
        for n in names:
            why = hz.refusal(n)
            if why:
                return why
        lv = hz.levels.levels
        zero = [n for n in names if not lv.get(n, (0, 0))[0] > 0]
        if zero:
            return (f"{', '.join(zero)}: the Haze level is 0. Set a level "
                    f"first.")
        if not callable(getattr(self.link(), "hazer_test_start", None)):
            return "the flame link cannot run a hazer test."
        return ""

    # -- the presses -------------------------------------------------------
    def start(self, names, who="", screen="", run=None, token="",
              page=None):
        """The SELECTED hazers ON for MAX_S each, named by the press id
        `run`, from the screen `page` (its own id; the press id when none):
        (ok, sentence). A hazer already on keeps its own end and its own
        screen."""
        rid = run if isinstance(run, str) and 0 < len(run) <= 64 else None
        if rid is None:
            return False, "Not started: press again."
        names, why = names_of(names, self.names())
        if why:
            why = f"Hazer test not started: {why}"
            self._note(who, screen, "refused",
                       f"{who or 'Someone'} asked for the hazer test on the "
                       f"{screen}; refused: {why}")
            self._audit(who, screen, "hazer test on", "", "refused", why)
            return False, why
        with self._lock:
            used = rid in self._used
            self._used.append(rid)
            off0 = self._off_gen
        if used:
            return False, "Not started: that press was already used."
        target = ", ".join(names)
        new = [n for n in names if n not in self.running()]
        if not new:
            return True, f"Already on: {target}."
        why = self.refusal(new) or self._sign_in_gone(token, who)
        if why:
            why = f"Hazer test not started: {why}"
            self._note(who, screen, "refused",
                       f"{who or 'Someone'} asked for the hazer test on the "
                       f"{screen} ({target}); refused: {why}")
            self._audit(who, screen, "hazer test on", target, "refused",
                        why)
            return False, why
        link = self.link()
        ok = False
        with self._lock:
            if self._off_gen == off0:
                try:
                    now = self._clock()
                    r = self.run
                    gen = link.abort_id if r is None else r["gen"]
                    link.hazer_test_gate = self._gate
                    ok = link.hazer_test_start(
                        MAX_S, gen, deadline=now + MAX_S,
                        clock=self._clock, names=new) is True
                    if ok:
                        if r is None:
                            r = {"beat": now, "gen": gen,
                                 "ids": collections.deque(
                                     maxlen=RUN_IDS_KEPT), "names": {}}
                        r["ids"].append(rid)
                        owner = page if isinstance(page, str) and \
                            0 < len(page) <= 64 else rid
                        for n in new:
                            # Each hazer's own silence clock: only the
                            # screen that turned it on keeps it.
                            r["names"][n] = {"deadline": now + MAX_S,
                                             "who": who, "token": token or "",
                                             "screen": screen, "np": None,
                                             "owner": owner, "rid": rid,
                                             "page": now}
                        self.run = r
                except Exception:
                    ok = False
                    self._link_off(new)
        if not ok:
            why = ("Hazer test not started: the flame link did not take it "
                   "(a Stop landed?). Press again in a moment.")
            self._note(who, screen, "refused",
                       f"{who or 'Someone'} asked for the hazer test on the "
                       f"{screen} ({', '.join(new)}); refused: {why}")
            self._audit(who, screen, "hazer test on", ", ".join(new),
                        "refused", why)
            return False, why
        self._note(who, screen, "on",
                   f"{who or 'Someone'} turned the hazer test on on the "
                   f"{screen} for {', '.join(new)} for {MAX_S} s at their "
                   f"levels. It turns off by itself then; Stop, Hazer test "
                   f"off and a quiet screen turn it off sooner.")
        self._audit(who, screen, "hazer test on", ", ".join(new),
                    "accepted", f"{MAX_S} s")
        why = self.send()
        if why:
            return False, f"The hazer test turned off at once: {why}."
        if self._threaded:
            threading.Thread(target=self._loop, daemon=True,
                             name="ltcplay-hazer-test").start()
        return True, f"Hazer test ON: {', '.join(new)}, {MAX_S} s."

    def beat(self, run, page=None):
        """A screen that turned hazers on is still there: the hazers IT
        turned on (its `page` id, or else the press `run`) stay on; no
        other screen's. (ok, sentence). A beat never starts anything and
        never moves an end."""
        with self._lock:
            r = self.run
            if r is None or not isinstance(run, str) or run not in r["ids"]:
                return False, "The hazer test is off."
            now = self._clock()
            kept = 0
            for e in r["names"].values():
                if (isinstance(page, str) and page and e["owner"] == page) \
                        or e["rid"] == run:
                    e["page"] = now
                    kept += 1
            if not kept:
                return False, "The hazer test is off."
        return True, "Hazer test on."

    def _end_reason(self, r, now):
        """(why every test hazer ends now or "", [(name, why)] for the ones
        that end on their own)."""
        if now - r["beat"] > BEAT_STALE_S or now < r["beat"]:
            return (f"the engine's timer stalled (nothing sent for "
                    f"{now - r['beat']:.2f} s)"), []
        busy = self._busy()
        if busy:
            return busy.rstrip("."), []
        link = self.link()
        try:
            if link.abort_id != r["gen"]:
                return (link.hazer_test_ended() or
                        "the disarm to every flame group was sent (a Stop)"), []
        except Exception:
            return "the flame link could not be read", []
        hz = self.hz()
        try:
            why = hz.down() if hz is not None else "the hazers are gone"
            if not why:
                why = hz._blocker() or ""
        except Exception as e:
            why = f"its check failed ({type(e).__name__})"
        if why:
            return why.rstrip("."), []
        ends = []
        passing = self._passing()
        for n, e in list(r["names"].items()):
            if now >= e["deadline"]:
                ends.append((n, f"its {MAX_S} s were up"))
                continue
            if now - e["page"] > PAGE_QUIET_S:
                ends.append((n, f"the screen that turned it on went quiet "
                                f"for {PAGE_QUIET_S:g} s"))
                continue
            gone = self._sign_in_gone(e.get("token", ""), e.get("who", ""))
            if gone:
                ends.append((n, gone))
                continue
            if not passing.get(n):
                since = e.get("np")
                if since is None:
                    e["np"] = now
                elif now - since > NOT_PASSING_S:
                    ends.append((n, f"flamesafe did not pass it for "
                                    f"{NOT_PASSING_S:g} s"))
            else:
                e["np"] = None
        return "", ends

    def send(self, now=None):
        """One pass of the timer: every ending cause, then the beat on the
        flame link. "" or why the last hazer ended. Never raises."""
        try:
            now = self._clock() if now is None else now
            with self._lock:
                r = self.run
            if r is None:
                return "it is not on"
            why, ends = self._end_reason(r, now)
            if why:
                self._finish(r, why)
                return why
            last = ""
            for n, w in ends:
                self._end_names(r, [n], w)
                last = w
            if self.run is not r:
                return last or "it was turned off"
            link = self.link()
            with self._lock:
                if self.run is not r:
                    return "it was turned off"
                fresh = self._clock()
                if fresh - r["beat"] > BEAT_STALE_S or fresh < r["beat"]:
                    lost = (f"the engine's timer stalled (nothing sent for "
                            f"{fresh - r['beat']:.2f} s)")
                elif link.hazer_test_beat(r["gen"]) is not True:
                    lost = ("the flame link turned it off" +
                            (f" ({link.hazer_test_ended()})"
                             if link.hazer_test_ended() else ""))
                else:
                    r["beat"] = fresh
                    lost = ""
                    live = set(link.hazer_test_live_now())
                    gone = [n for n in r["names"] if n not in live]
            if lost:
                self._finish(r, lost)
                return lost
            if gone:
                self._end_names(r, gone, "the flame link ended it")
                if self.run is not r:
                    return "the flame link ended it"
            return ""
        except Exception as e:
            why = f"its check failed ({type(e).__name__})"
            self.stop(why)
            return why

    def _loop(self):
        wait = threading.Event()
        try:
            while self.run is not None:
                if self.run is None or (self.send() and self.run is None):
                    return
                wait.wait(SEND_S)
        except Exception as e:
            self.stop(f"its timer raised {type(e).__name__}")

    def _end_names(self, r, names, why, who="", screen="", system=True):
        """End `names` of run `r` (if it is still the run)."""
        with self._lock:
            if self.run is not r:
                return False
            gone = [n for n in names if n in r["names"]]
            for n in gone:
                del r["names"][n]
            if not r["names"]:
                self.run = None
            self._link_off(gone if self.run is not None else None)
        if gone:
            self._said_off(why, gone, who, screen, system)
        return bool(gone)

    def _finish(self, r, why, who="", screen="", system=True):
        with self._lock:
            mine = self.run is r
            gone = sorted(r["names"]) if mine else []
            if mine:
                self.run = None
            self._link_off()
        if mine:
            self._said_off(why, gone, who, screen, system)
        return mine

    def _link_off(self, names=None):
        link = self.link()
        if link is not None:
            try:
                link.hazer_test_stop(names)
            except Exception:
                pass

    def _said_off(self, why, names, who="", screen="", system=True):
        why = " ".join(str(why or "Hazer test off").split())[:200]
        by = who or ("system" if system else "unsigned")
        target = ", ".join(names or ())
        with self._lock:
            self._offs += 1
            self.last_off = {"reason": why, "who": by, "names": list(names or ()),
                             "n": self._offs, "boot": self.boot}
        self._note(by, screen or "engine", "off",
                   f"Hazer test off ({target or 'every hazer'}): {why}.")
        self._audit(by, screen or "engine", "hazer test off", target,
                    "accepted", why, system=system and not who)

    def stop_names(self, names, reason, who="", screen="", system=False):
        """The named hazers OFF now (the others as they were). An OFF
        naming a hazer the engine does not have turns every one off.
        (ok, sentence). Never refused, never raises."""
        try:
            got, bad = names_of(names, self.names())
            if bad:
                self.stop(f"{reason} (a hazer it named is not known, so "
                          f"every one)", who, screen, system)
                return True, "Hazer test OFF for every hazer."
            with self._lock:
                self._off_gen += 1
                r = self.run
            if r is not None:
                self._end_names(r, got, reason, who, screen, system)
            self._link_off(got)
            return True, f"Hazer test OFF: {', '.join(got)}."
        except Exception:
            self.stop(reason, who, screen, system)
            return True, "Hazer test OFF for every hazer."

    def stop(self, reason, who="", screen="", system=True):
        """Every hazer of the test OFF now, for any reason. True when one
        was on. Never raises."""
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
                self._said_off(reason, [], who, screen, system)
            return bool(was)
        except Exception:
            return False

    def end_for_token(self, token, why, who="", screen=""):
        """A sign-in ended: the hazers it turned on go off now."""
        try:
            r = self.run
            if r is None or not token:
                return False
            mine = [n for n, e in r["names"].items()
                    if e.get("token") == token]
            if not mine:
                return False
            return self._end_names(r, mine, why, who, screen,
                                   system=not who)
        except Exception:
            return False

    # -- the screens --------------------------------------------------------
    def view(self):
        """For the screens: each hazer's name, its stored Haze and Fan
        levels in percent (the Levels sheet's numbers), ON or not in the
        test (the engine's word: the run AND the flame link carrying it),
        "off", "waiting" (flamesafe has not passed it yet) or "on", its
        time left; whether any is on; why turning hazers on would be
        refused now; and the last thing that turned any off. Fails closed:
        an error is OFF."""
        try:
            names = self.names()
            if not names:
                return {"available": False}
            hz = self.hz()
            r = self.run
            live = self.running()
            passing = self._passing()
            from .hazers import value_to_pct
            rows = []
            for n in names:
                h, f = hz.levels.levels.get(n, (0, 0))
                on = n in live
                e = r["names"].get(n) if r is not None and on else None
                left = None if e is None else \
                    round(max(0.0, e["deadline"] - self._clock()), 1)
                try:
                    why = "" if on else self.refusal([n])
                except Exception as e:
                    why = f"its check failed ({type(e).__name__})."
                rows.append({"name": n, "on": on,
                             "state": "off" if not on else (
                                 "on" if passing.get(n) else "waiting"),
                             "left_s": left, "haze_pct": value_to_pct(h),
                             "fan_pct": value_to_pct(f), "refusal": why})
            try:
                refusal = self.refusal(
                    [x["name"] for x in rows if not x["on"]] or names)
            except Exception as e:
                refusal = f"its check failed ({type(e).__name__})."
            lefts = [x["left_s"] for x in rows if x["left_s"] is not None]
            return {"available": True, "names": names, "hazers": rows,
                    "on": bool(live), "names_on": sorted(live),
                    "state": "off" if not live else (
                        "on" if all(passing.get(n) for n in live)
                        else "waiting"),
                    "left_s": min(lefts) if lefts else None,
                    "max_s": MAX_S, "beat_s": PAGE_BEAT_S,
                    "refusal": refusal, "last_off": self.last_off,
                    "boot": self.boot}
        except Exception as e:
            # Fail closed: a state that cannot be read is turned OFF.
            self.stop(f"its state could not be read ({type(e).__name__})")
            return {"available": True, "on": False, "state": "unknown",
                    "error": f"{type(e).__name__}", "max_s": MAX_S}
