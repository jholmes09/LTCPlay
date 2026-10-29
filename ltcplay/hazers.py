"""The hazers (Jeff, 2026-10-29): three haze machines on the flame
universe, switched ON and OFF from the rack screen and the Stream Deck.

"Abort turns off the hazers. Hazers don't have an arm/disarm channel, but
I'm OK with us having to turn them on and off. I have 3 hazers and I could
give Andy the same control as the Wavers."

What it is
==========

flamesafe's config lists the hazers under "passthrough" (name and slots;
flamesafe/CONTRACT.md, "Passthrough: the hazers"). The engine reads the
same list from the same file (flamelink.hazers_of) and holds one ON/OFF
per hazer, kept on the flame link itself (FlameLink.hazers_set), so there
is exactly one copy of it. Every flame frame carries "hazers_on", and
flamesafe passes a hazer's slots only while the locked sender, this
engine, has it ON. While a hazer is ON its slots carry the show's own
values from the FSEQ, from whichever source is running (show playback,
live flame programming, fire flames while scrubbing, or the Programming
screen's transport); OFF they are zero.

The rules
=========

  - OFF at start, after Abort, after Stop, after an engine restart, and
    while flamesafe's status is not fresh (no status for STATUS_FRESH_S,
    or none at all) or the flame link is not sending: this module's watch
    switches every hazer OFF then. Abort and Stop also switch them OFF at
    the flame link itself (FlameLink.disarm_all), and flamesafe zeroes them
    on the disarm_all: each needs OFF then ON again there.
  - Hold changes nothing. A show ending changes nothing.
  - ON needs: the role matrix's arm row (users.classify: "hazer" with "on"
    the JSON word true is an arm press; everything that switches off is
    safety), the rack screen or, from the show network, screen_arming on
    (remote.may_act_from, as a purge or a laser arm), no Abort standing,
    the flame link sending, flamesafe's status fresh, and flamesafe's list
    of hazers the same as this engine's. A plain tap: a hazer is not a
    flame, so there is no hold to arm.
  - OFF and All off: always, from any screen, signed in or not.
"""
import threading
import time

NAME = "Hazers"
STATUS_FRESH_S = 1.0        # flamesafe's status, as for arming (ARM_FRESH_S)
WATCH_S = 0.25


def label(name, on):
    """A hazer's state words for a tile or a key: "Hazer 1 ON"."""
    return f"{name} {'ON' if on else 'OFF'}"


class Hazers:
    """ON and OFF per hazer, and the watch that switches them OFF.

    link_get        () -> the engine's FlameLink, or None
    flames_get      () -> remote.FlameStatus.view() or None
    abort_standing  () -> True while an Abort is latched
    journal         (who, screen, outcome, text, fault=False)
    audit           audit.AuditLog or None
    threaded        False for the selftest: check() by hand"""

    def __init__(self, link_get, flames_get, abort_standing, journal=None,
                 audit=None, clock=time.monotonic, threaded=True):
        self._link_get = link_get
        self._flames_get = flames_get
        self._abort_standing = abort_standing
        self._journal = journal
        self.audit = audit
        self._clock = clock
        self._threaded = threaded
        self._lock = threading.RLock()
        self._watching = False
        self.last_off = None        # {"reason", "who"} of the last all off

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
                      action=action, target=target, result=result,
                      reason=reason, source=screen or "engine")
        except Exception:
            pass

    def link(self):
        try:
            return self._link_get()
        except Exception:
            return None

    def names(self):
        link = self.link()
        try:
            return list(link.hazer_names()) if link is not None else []
        except Exception:
            return []

    def slots(self):
        link = self.link()
        try:
            return {n: list(sl) for n, sl in link.cfg.hazers}
        except Exception:
            return {}

    def on(self):
        link = self.link()
        try:
            return set(link.hazers_on) if link is not None else set()
        except Exception:
            return set()

    def _flames(self):
        try:
            return self._flames_get() or None
        except Exception:
            return None

    def flamesafe_list(self, fl=None):
        """flamesafe's own hazers [(name, slots)] from a fresh status, or
        None (no status, stale, or an older flamesafe)."""
        fl = self._flames() if fl is None else fl
        if not fl or fl.get("stale"):
            return None
        pt = fl.get("passthrough")
        if not isinstance(pt, list):
            return None
        return [(str(p.get("name", "")), list(p.get("slots") or []))
                for p in pt if isinstance(p, dict)]

    # -- why not ------------------------------------------------------------
    def down(self):
        """Why every hazer must be OFF right now, in a sentence, or "". The
        watch's test, and the first thing ON is refused for."""
        link = self.link()
        if link is None or not self.names():
            return "there are no hazers in flamesafe's config for this engine."
        try:
            snap = link.snapshot()
        except Exception:
            snap = {}
        if not snap.get("open") or not snap.get("sending_ok"):
            return ("the flame link to flamesafe is not sending, so the "
                    "hazers cannot be switched on.")
        fl = self._flames()
        if not fl or not fl.get("connected"):
            return ("flamesafe's status is not connected to this engine, "
                    "so whether the hazers run is not known.")
        age = fl.get("age_ms")
        if fl.get("stale") or age is None or age > STATUS_FRESH_S * 1000:
            return (f"flamesafe has not reported in the last "
                    f"{STATUS_FRESH_S:g} s.")
        return ""

    def refusal(self, name):
        """Why `name` may not be switched ON now, in a sentence, or ""."""
        try:
            if self._abort_standing():
                return ("an Abort is standing. Press Reset, then switch it "
                        "on again.")
        except Exception:
            return "whether an Abort is standing could not be read."
        why = self.down()
        if why:
            return why
        if name not in self.names():
            return f"{name!r} is not a hazer in flamesafe's config."
        try:
            repeating = self.link().abort_repeating()
        except Exception:
            repeating = True
        if repeating:
            return ("an Abort or Stop is still being sent to flamesafe. "
                    "Switch it on again in a moment.")
        theirs = self.flamesafe_list()
        mine = [(n, sl) for n, sl in self.slots().items()]
        if theirs is None:
            return ("flamesafe does not report its hazers (an older "
                    "flamesafe?), so they cannot be switched on from here.")
        if sorted(theirs) != sorted(mine):
            return ("flamesafe's hazers and LTC Player's differ (see the "
                    "hazers alert). Restart both from the same "
                    "flamesafe.json.")
        return ""

    # -- the presses -------------------------------------------------------
    def set(self, name, on, who="", screen=""):
        """(ok, sentence). OFF is never refused (an unknown name aside)."""
        name = str(name or "")
        if name not in self.names():
            return False, f"{name!r} is not a hazer in flamesafe's config."
        if on is not True:
            return self._switch_off(name, who, screen)
        why = self.refusal(name)
        if why:
            text = (f"{who or 'Someone'} asked to switch {name} on on the "
                    f"{screen}; refused: {why}")
            self._note(who, screen, "refused", text)
            self._audit(who, screen, "hazer on", name, "refused", why)
            return False, f"{name} was not switched on: {why}"
        with self._lock:
            link = self.link()
            got = link.hazers_set(self.on() | {name})
            ok = name in got
            if ok:
                self._start_watch()
        if not ok:
            return False, f"{name} was not switched on: the flame link refused."
        text = (f"{who or 'Someone'} switched {name} ON on the {screen}. Its "
                f"slots carry the show's haze values while it stays on. "
                f"Abort, Stop and a lost link to flamesafe switch it off.")
        self._note(who, screen, "on", text)
        self._audit(who, screen, "hazer on", name, "accepted", "")
        return True, f"{name} is ON."

    def _switch_off(self, name, who, screen):
        with self._lock:
            link = self.link()
            was = name in self.on()
            if link is not None:
                try:
                    link.hazers_set(self.on() - {name})
                except Exception:
                    pass
        if was:
            self._note(who, screen, "off", f"{who or 'Someone'} switched "
                                           f"{name} OFF on the {screen}.")
            self._audit(who, screen, "hazer off", name, "accepted", "")
        return True, f"{name} is OFF."

    def all_off(self, reason, who="", screen="", system=True):
        """Every hazer OFF now, for any reason. True when one was on. Never
        raises."""
        why = " ".join(str(reason or "Hazers off").split())[:200]
        with self._lock:
            was = sorted(self.on())
            link = self.link()
            if link is not None:
                try:
                    link.hazers_set(())
                except Exception:
                    pass
        if not was:
            return False
        by = who or ("system" if system else "unsigned")
        self.last_off = {"reason": why, "who": by}
        self._note(by, screen or "engine", "all off",
                   f"Hazers off ({', '.join(was)}): {why}.")
        self._audit(by, screen or "engine", "hazers off", ", ".join(was),
                    "accepted", why, system=system and not who)
        return True

    def cut(self):
        """The cheap half, for an Abort or Stop press: every hazer OFF at
        the link now, no journal, no disk. The flame link's disarm_all
        does this too. True when one was on. Never raises."""
        link = self.link()
        try:
            was = bool(link.hazers_on)
            link.hazers_set(())
            return was
        except Exception:
            return False

    # -- the watch ----------------------------------------------------------
    def check(self):
        """One watch pass: every hazer OFF if down() says so. True when it
        switched one off. Never raises."""
        try:
            if not self.on():
                return False
            why = self.down()
        except Exception as e:
            why = f"its check failed ({type(e).__name__})"
        if why:
            return self.all_off(why)
        return False

    def _start_watch(self):
        if not self._threaded or self._watching:
            return
        self._watching = True
        try:
            threading.Thread(target=self._watch, daemon=True,
                             name="ltcplay-hazers").start()
        except Exception:
            self._watching = False
            self.all_off("its watch could not be started")

    def _watch(self):
        try:
            while True:
                time.sleep(WATCH_S)
                self.check()
                with self._lock:
                    if not self.on():
                        self._watching = False
                        return
        except Exception:
            self._watching = False
            self.all_off("its watch stopped")

    # -- the screens --------------------------------------------------------
    def view(self, fl=None):
        """For the rack screen and the Stream Deck: each hazer's name,
        slots, ON or OFF here, and what flamesafe says of it."""
        fl = self._flames() if fl is None else fl
        on = self.on()
        theirs = {}
        if fl and not fl.get("stale") and isinstance(fl.get("passthrough"),
                                                     list):
            theirs = {str(p.get("name")): p for p in fl["passthrough"]
                      if isinstance(p, dict)}
        out = []
        for n, sl in self.slots().items():
            p = theirs.get(n)
            out.append({"name": n, "slots": sl, "on": n in on,
                        "label": label(n, n in on),
                        "passing": bool(p.get("passing")) if p else None,
                        "why": str(p.get("why", "")) if p else None})
        down = self.down() if out else ""
        return {"available": bool(out), "hazers": out, "down": down,
                "last_off": self.last_off}
