"""The hazers (Jeff, 2026-10-29): three haze machines on the flame
universe, run by LTC Player, never by the FSEQ.

"I want haze control to be in LTC Play, not the FSEQ. I want Andy to be
able to set the show levels of both Haze and Fan, in addition to arming
which ones run during the show. Then during the actual show, they are
always on at those levels unless disarmed."

What it is
==========

flamesafe's config lists the hazers under "passthrough": a name, a haze
slot and a fan slot each (flamesafe/CONTRACT.md, "Passthrough: the
hazers"). The engine reads the same list from the same file
(flamelink.hazers_of). Per hazer it keeps:

  - ARMED or DISARMED, held on the flame link itself (FlameLink.hazers_set,
    one copy), carried in every flame frame as "hazers_on";
  - a Haze level and a Fan level, 0 to 100 percent (stored 0 to 255), in
    LEVELS_FILE in the settings folder (written whole and atomically,
    validated when read; a file that cannot be read is all 0 and said
    once). Changes apply at once, during a show too.

The flame link writes zero on every hazer channel, whatever the FSEQ,
xLights or the Programming transport carries there, and then each ARMED
hazer's levels, only while a show is playing: the show audio playing a
cue AND the scheduled show in SHOW (show_state "playing"). Not between
shows, not during the rig check before one, not in Programming, not on
Hold (haze stops; it starts again on Resume). flamesafe passes those slots
only while the frame says that hazer is armed, the link is fresh and no
Abort has stood since it last saw it disarmed.

The rules
=========

  - DISARMED at start, after Abort, after Stop, after an engine restart,
    while flamesafe's status is not fresh or the flame link is not
    sending (this module's watch disarms them then), and when live flame
    programming ends (it sends the Abort's disarm to flamesafe). Arming
    stays across shows otherwise: Andy arms once a night.
  - ARM needs: the role matrix's arm row (users.classify), from the show
    network screen_arming too (remote.may_act_from), no Abort standing or
    still being sent, live flame programming off, the flame link sending,
    flamesafe's status fresh, and flamesafe's hazers the same as the
    engine's. A plain tap: a hazer is not a flame.
  - Levels: the same permission as arming, from the same places.
  - DISARM and Hazers off: always, anyone, signed in or not.
"""
import json
import os
import threading
import time

NAME = "Hazers"
LEVELS_FILE = "ltcplay_hazers.json"
LEVELS_FORMAT = 1
STATUS_FRESH_S = 1.0        # flamesafe's status, as for arming (ARM_FRESH_S)
WATCH_S = 0.25


def pct_to_value(p):
    """0 to 100 percent as 0 to 255, rounded; anything else ValueError."""
    if isinstance(p, bool) or not isinstance(p, (int, float)) or p != p:
        raise ValueError("a level is a number 0 to 100")
    if not 0 <= p <= 100:
        raise ValueError("a level is 0 to 100 percent")
    return int(round(p * 255 / 100.0))


def value_to_pct(v):
    return int(round(int(v) * 100 / 255.0))


def label(name, armed):
    """A hazer's state words for a tile or a key: "Hazer 1 ARMED"."""
    return f"{name} {'ARMED' if armed else 'DISARMED'}"


class Levels:
    """Each hazer's (haze, fan), 0 to 255, in LEVELS_FILE. Never raises
    on read: a missing file is all 0; a bad one is all 0 and `fault` says
    why until the next good write."""

    def __init__(self, folder, names):
        self.path = os.path.join(folder, LEVELS_FILE) if folder else None
        self.names = list(names)
        self.fault = ""
        self.levels = {n: (0, 0) for n in self.names}
        self._lock = threading.Lock()
        self.load()

    def load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
            if not isinstance(doc, dict) or \
                    doc.get("hazers_format") != LEVELS_FORMAT or \
                    not isinstance(doc.get("levels"), dict):
                raise ValueError("not a hazer levels file")
            got = {}
            for n, lv in doc["levels"].items():
                if n not in self.names:
                    continue
                h, f = lv.get("haze"), lv.get("fan")
                for v in (h, f):
                    if isinstance(v, bool) or not isinstance(v, int) or \
                            not 0 <= v <= 255:
                        raise ValueError(f"{n}: a level is not 0 to 255")
                got[n] = (h, f)
            self.levels.update(got)
        except (OSError, ValueError, AttributeError, TypeError) as e:
            self.fault = (f"{self.path} could not be read ({e}); every "
                          f"hazer level is 0 until they are set again.")

    def set(self, name, haze=None, fan=None):
        """Set one hazer's levels (0 to 255; None keeps one). Written to
        the file before it counts. Returns the new (haze, fan)."""
        with self._lock:
            h, f = self.levels[name]
            new = dict(self.levels)
            new[name] = (h if haze is None else int(haze),
                         f if fan is None else int(fan))
            self._write(new)
            self.levels = new
            return new[name]

    def _write(self, levels):
        if not self.path:
            return
        doc = {"hazers_format": LEVELS_FORMAT,
               "levels": {n: {"haze": h, "fan": f}
                          for n, (h, f) in levels.items()}}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        self.fault = ""


class Hazers:
    """ARMED/DISARMED and levels per hazer, and the watch that disarms.

    link_get        () -> the engine's FlameLink, or None
    flames_get      () -> remote.FlameStatus.view() or None
    abort_standing  () -> True while an Abort is latched
    show_state      () -> "playing", "held" or "idle" (the scheduled show)
    blocker         () -> why arming is refused right now, or "" (live
                    flame programming on)
    folder          the settings folder (LEVELS_FILE), or None
    journal         (who, screen, outcome, text, fault=False)
    audit           audit.AuditLog or None
    threaded        False for the selftest: check() by hand"""

    def __init__(self, link_get, flames_get, abort_standing, show_state=None,
                 blocker=None, folder=None, journal=None, audit=None,
                 clock=time.monotonic, threaded=True):
        self._link_get = link_get
        self._flames_get = flames_get
        self._abort_standing = abort_standing
        self._show_state = show_state or (lambda: "idle")
        self._blocker = blocker or (lambda: "")
        self._journal = journal
        self.audit = audit
        self._clock = clock
        self._threaded = threaded
        self._lock = threading.RLock()
        self._watching = False
        self.last_off = None        # {"reason", "who", "was"}
        self.levels = Levels(folder, self.names())
        if self.levels.fault:
            self._note("system", "engine", "levels", self.levels.fault,
                       fault=True)
        self.push_levels()
        link = self.link()
        if link is not None:
            # The flame link asks this, every frame, whether haze may run.
            link.haze_gate = self.running

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
        """{name: [haze slot, fan slot]}."""
        link = self.link()
        try:
            return {n: list(sl) for n, sl in link.cfg.hazers}
        except Exception:
            return {}

    def on(self):
        """The names ARMED (the flame link's own set)."""
        link = self.link()
        try:
            return set(link.hazers_on) if link is not None else set()
        except Exception:
            return set()

    def show(self):
        try:
            s = self._show_state()
        except Exception:
            return "idle"
        return s if s in ("playing", "held") else "idle"

    def running(self):
        """The flame link's haze_gate: a show playing, not held."""
        return self.show() == "playing"

    def push_levels(self):
        link = self.link()
        if link is not None:
            try:
                link.hazer_levels_set(dict(self.levels.levels))
            except Exception:
                pass

    def _flames(self):
        try:
            return self._flames_get() or None
        except Exception:
            return None

    def flamesafe_list(self, fl=None):
        """flamesafe's own hazers [(name, [haze, fan])] from a fresh status,
        or None (no status, stale, or an older flamesafe)."""
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
        """Why every hazer must be DISARMED right now, in a sentence, or "".
        The watch's test, and the first thing ARM is refused for."""
        link = self.link()
        if link is None or not self.names():
            return "there are no hazers in flamesafe's config for this engine."
        try:
            snap = link.snapshot()
        except Exception:
            snap = {}
        if not snap.get("open") or not snap.get("sending_ok"):
            return "the flame link to flamesafe is not sending."
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
        """Why `name` may not be ARMED now, in a sentence, or ""."""
        try:
            if self._abort_standing():
                return ("an Abort is standing. Press Reset, then arm it "
                        "again.")
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
                    "Arm it again in a moment.")
        try:
            why = self._blocker() or ""
        except Exception:
            why = "the engine's state could not be read."
        if why:
            return why
        theirs = self.flamesafe_list()
        mine = [(n, sl) for n, sl in self.slots().items()]
        if theirs is None:
            return ("flamesafe does not report its hazers (an older "
                    "flamesafe?), so they cannot be armed from here.")
        if sorted(theirs) != sorted(mine):
            return ("flamesafe's hazers and LTC Player's differ (see the "
                    "hazers alert). Restart both from the same "
                    "flamesafe.json.")
        return ""

    # -- the presses -------------------------------------------------------
    def set(self, name, on, who="", screen=""):
        """ARM (on is True) or DISARM one hazer: (ok, sentence). DISARM is
        never refused (an unknown name aside)."""
        name = str(name or "")
        if name not in self.names():
            return False, f"{name!r} is not a hazer in flamesafe's config."
        if on is not True:
            return self._disarm(name, who, screen)
        why = self.refusal(name)
        if why:
            text = (f"{who or 'Someone'} asked to arm {name} on the "
                    f"{screen}; refused: {why}")
            self._note(who, screen, "refused", text)
            self._audit(who, screen, "hazer arm", name, "refused", why)
            return False, f"{name} was not armed: {why}"
        with self._lock:
            got = self.link().hazers_set(self.on() | {name})
            ok = name in got
            if ok:
                self._start_watch()
        if not ok:
            return False, f"{name} was not armed: the flame link refused."
        h, f = self.levels.levels.get(name, (0, 0))
        text = (f"{who or 'Someone'} armed {name} on the {screen}. It runs "
                f"at haze {value_to_pct(h)}%, fan {value_to_pct(f)}% while "
                f"a show plays. Abort, Stop and a lost link to flamesafe "
                f"disarm it.")
        self._note(who, screen, "armed", text)
        self._audit(who, screen, "hazer arm", name, "accepted", "")
        return True, f"{name} is ARMED."

    def _disarm(self, name, who, screen):
        with self._lock:
            link = self.link()
            was = name in self.on()
            if link is not None:
                try:
                    link.hazers_set(self.on() - {name})
                except Exception:
                    pass
        if was:
            self._note(who, screen, "disarmed",
                       f"{who or 'Someone'} disarmed {name} on the "
                       f"{screen}.")
            self._audit(who, screen, "hazer disarm", name, "accepted", "")
        return True, f"{name} is DISARMED."

    def set_levels(self, name, haze_pct=None, fan_pct=None, who="",
                   screen=""):
        """Set the Haze and/or Fan level of one hazer, or of every hazer
        with name "all", in percent: (ok, sentence). Applied at once."""
        names = self.names() if name == "all" else [str(name or "")]
        if not names or any(n not in self.names() for n in names):
            return False, f"{name!r} is not a hazer in flamesafe's config."
        try:
            h = None if haze_pct is None else pct_to_value(haze_pct)
            f = None if fan_pct is None else pct_to_value(fan_pct)
        except ValueError as e:
            return False, f"Not changed: {e}."
        if h is None and f is None:
            return False, "Not changed: give a haze or a fan level."
        try:
            for n in names:
                self.levels.set(n, haze=h, fan=f)
        except OSError as e:
            return False, (f"Not changed: the levels could not be saved "
                           f"({e}).")
        finally:
            self.push_levels()
        what = ", ".join(
            f"{n} haze {value_to_pct(self.levels.levels[n][0])}% fan "
            f"{value_to_pct(self.levels.levels[n][1])}%" for n in names)
        self._note(who, screen, "levels",
                   f"{who or 'Someone'} set the hazer levels on the "
                   f"{screen}: {what}.")
        self._audit(who, screen, "hazer levels", name, "accepted", what)
        return True, f"Set: {what}."

    def all_off(self, reason, who="", screen="", system=True, was=None):
        """Every hazer DISARMED now, for any reason. `was` names the ones
        armed before a cut() already cleared them. True when one was
        armed. Never raises."""
        why = " ".join(str(reason or "Hazers off").split())[:200]
        with self._lock:
            before = sorted(set(was or ()) | self.on())
            link = self.link()
            if link is not None:
                try:
                    link.hazers_set(())
                except Exception:
                    pass
        if not before:
            return False
        by = who or ("system" if system else "unsigned")
        self.last_off = {"reason": why, "who": by, "was": before}
        self._note(by, screen or "engine", "all off",
                   f"Hazers off ({', '.join(before)}): {why}.")
        self._audit(by, screen or "engine", "hazers off", ", ".join(before),
                    "accepted", why, system=system and not who)
        return True

    def cut(self):
        """The cheap half, for an Abort or Stop press: every hazer DISARMED
        at the link now, no journal, no disk. Returns the names that were
        armed, for all_off's line. Never raises."""
        link = self.link()
        try:
            was = sorted(link.hazers_on)
            link.hazers_set(())
            return was
        except Exception:
            return []

    # -- the watch ----------------------------------------------------------
    def check(self):
        """One watch pass: every hazer DISARMED if down() says so. True
        when it disarmed one. Never raises."""
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
        """For the rack screen and the Stream Deck: each hazer's name, its
        haze and fan slots, ARMED or not, its levels, and its state in
        words: "disarmed", "waiting" (armed here, flamesafe has not taken
        it yet), "held" (a Hold: haze stops until Resume), "running" (a
        show plays) or "standby" (armed, no show playing)."""
        fl = self._flames() if fl is None else fl
        on = self.on()
        show = self.show()
        theirs = {}
        if fl and not fl.get("stale") and isinstance(fl.get("passthrough"),
                                                     list):
            theirs = {str(p.get("name")): p for p in fl["passthrough"]
                      if isinstance(p, dict)}
        out = []
        for n, sl in self.slots().items():
            h, f = self.levels.levels.get(n, (0, 0))
            armed = n in on
            p = theirs.get(n)
            if not armed:
                state = "disarmed"
            elif not p or not p.get("enabled"):
                # Reviewer B: flamesafe's word, never the engine's own bit.
                state = "waiting"
            elif show == "held":
                state = "held"
            elif show == "playing":
                state = "running"
            else:
                state = "standby"
            out.append({"name": n, "haze_slot": sl[0], "fan_slot": sl[1],
                        "armed": armed, "label": label(n, armed),
                        "state": state, "haze": h, "fan": f,
                        "haze_pct": value_to_pct(h),
                        "fan_pct": value_to_pct(f),
                        "why": str(p.get("why", "")) if p else None})
        down = self.down() if out else ""
        return {"available": bool(out), "hazers": out, "down": down,
                "show": show, "levels_fault": self.levels.fault,
                "last_off": self.last_off}
