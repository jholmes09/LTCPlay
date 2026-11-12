"""Haze fluid (Jeff, 2026-11-12): "track haze fluid consumption the same
way flame fuel is tracked today". The hazers' side of the Fuel page; read
fuel.py first, this follows it.

What is measured
================

Per hazer (flamesafe's hazer entries: a haze slot and a fan slot each,
flamelink.hazers_of), the seconds its HAZE channel was above 0 in a frame
that actually went out to flamesafe, read from the very bytes the flame
link sent (FlameLink.haze_observer: the frame on the wire, after the
hazer levels and the hazer test were written into it). Fan only time is
never counted: a fan moves air, it uses no fluid.

Each second is also weighted by the haze value sent (value / 255): the
record keeps both the raw on-seconds ("on") and the full-output-equivalent
seconds ("full"). 140 for one second is 140/255 of a full second.

Each frame goes in one of three piles, as fuel's show/purge/other:

  show    an ARMED hazer running during a performance that counts as a
          show (the same rule as fuel: its show timecode advanced for
          fuel's min_show_s, not a programming session). Per show, the
          haze of that show is kept (show_burns, [on, full] each).
  manual  the hazer test (hazertest.py): every hazer at its levels by
          hand, with no show.
  other   everything else (haze during a performance too short to be a
          show).

"Not sure" (never dropped, never invented)
==========================================

  - flamesafe's status not fresh (stale, lost, not connected) while a
    hazer was sent haze: counted, AND the same seconds go on the hazer's
    unsure_s (flamesafe may have held it off).
  - a frame that could not be sent, or a gap between frames, after a
    frame that carried haze: flamesafe holds the last value until its
    frame_stale_ms (FLAMESAFE_HOLD_S), so up to that much goes on unsure_s
    of each hazer that was on. Never on the counted seconds.
  - frames this tracker could not take (it fell behind), an error in the
    tracker, or LTC Player stopping without closing: a "gap" (every hazer
    reads Not sure until its next refill), and for the stop, SAVE_EVERY_S
    on unsure_s of each hazer that was hazing since the last save.
  - an engine stopping while a hazer was on: FLAMESAFE_HOLD_S on its
    unsure_s.

The litres estimate (only when set) counts unsure seconds as full output:
it reads emptier, never fuller.

How it never touches the haze
=============================

FlameLink.send_frame hands (tc, wire, test, ok) to `haze_observer` after
the frame is out, inside try/except (flamelink.py). observe() here only
appends a tuple to a bounded deque: no lock, no disk, no journal. All the
rest runs on this module's own thread under its own lock, which nothing on
the link or frame path ever takes. A failure is journaled once and shown
on the Fuel page; it never raises into the sender and never changes,
delays or blocks a value.

What is kept, and where
=======================

haze.json in the settings folder, beside fuel.json, written atomically (a
temporary file, fsync, replace) with its own format number (haze_format).
A separate file, not a section of fuel.json, on purpose: an older build
reads fuel.json happily but writes back only the keys it knows, so a
section of it would be erased by a rollback; and a hazer record that
cannot be read is set aside on its own, never taking fuel.json with it.
fuel.json is not changed at all (fuel_format stays 1).

Settings (optional): in ltcplay_hazers.json, beside the levels,
  "fluid": {"Hazer 1": {"litres_per_hour": 0.6, "tank_litres": 2.5}}
litres_per_hour is the fluid used per hour at full output (haze 255). With
it, the page shows litres used since the refill; with tank_litres too,
litres and percent left. Without them it shows only seconds.
"""
import collections
import json
import math
import os
import threading
import time

from .fuel import (EPISODE_JOIN_S, TC_STEP_MAX_S, tc_seconds, may_refill,
                   median)

HAZE_FILE = "haze.json"
HAZE_FORMAT = 1             # the hazer record's version
LEVELS_FILE = "ltcplay_hazers.json"
SOURCES = ("show", "manual", "other")
FULL = 255.0                # a haze value of 255 is full output
SAVE_EVERY_S = 30.0
DRAIN_S = 0.25
MAX_FRAME_DT_S = 0.1        # one frame never counts for more than this
QUEUE_MAX = 4000            # frames waiting (100 s at 40 a second)
# flamesafe's frame_stale_ms: with no fresh frame it holds the last value
# this long, then zeroes every hazer slot (flamesafe/CONTRACT.md).
FLAMESAFE_HOLD_S = 0.5
# flamesafe's status counts as fresh for this long (hazers.STATUS_FRESH_S,
# the same number that disarms the hazers).
FLAMESAFE_FRESH_MS = 1000
MIN_SHOW_S = 30             # fuel's default, when fuel's is not known
CLOSE_TIMEOUT_S = 2.0
REFILLS_KEPT = 60
SHOW_BURNS_KEPT = 50
RECENT_SHOWS = 10
MIN_SHOWS = 3
AMBER_BELOW = 35
RED_BELOW = 15
LPH_MAX = 50.0
TANK_MAX = 500.0
FLUID_KEYS = frozenset(("litres_per_hour", "tank_litres"))


# ------------------------------------------------------------ settings --

def parse_fluid(doc, names, where=LEVELS_FILE):
    """ltcplay_hazers.json's "fluid" block: {name: {"litres_per_hour": x,
    "tank_litres": y}}, every key optional. (settings, problem sentence or
    ""). A wrong block is no settings AND its sentence: the page then shows
    seconds only, never a guess."""
    if doc is None:
        return {}, ""
    if not isinstance(doc, dict):
        return {}, f"{where}: fluid has to be an object, one per hazer."
    out = {}
    for n, d in doc.items():
        if n == "notes":
            continue
        w = f"{where}: fluid.{n}"
        if n not in names:
            return {}, (f"{w}: there is no hazer called {n!r} here (the "
                        f"hazers are {', '.join(names) or 'none'}).")
        if not isinstance(d, dict):
            return {}, f"{w} has to be an object."
        bad = sorted(str(k) for k in d if k not in FLUID_KEYS)
        if bad:
            return {}, (f"{w} has no setting {', '.join(bad)}; it takes "
                        f"litres_per_hour and tank_litres.")
        got = {}
        for k, hi, what in (("litres_per_hour", LPH_MAX,
                             "litres an hour at full output"),
                            ("tank_litres", TANK_MAX, "litres in a full "
                                                      "tank")):
            if k not in d or d[k] is None:
                continue
            v = d[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or \
                    not math.isfinite(v) or not 0 < v <= hi:
                return {}, (f"{w}.{k} is the {what}, a number over 0 and "
                            f"up to {hi:g}, not {v!r}.")
            got[k] = float(v)
        out[n] = got
    return out, ""


# ------------------------------------------------- reading haze.json back --

def _num(v, what):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or \
            not math.isfinite(v) or v < 0:
        raise ValueError(f"{what} is not a number of 0 or more: {v!r}")
    return float(v)


def _count(v, what, lo=0):
    if isinstance(v, bool) or not isinstance(v, int) or v < lo:
        raise ValueError(f"{what} is not a whole number of {lo} or more: "
                         f"{v!r}")
    return v


def _split(d, what):
    if not isinstance(d, dict):
        raise ValueError(f"{what} is not an object")
    return {s: _num(d.get(s, 0.0), f"{what}.{s}") for s in SOURCES}


def _pair(p, what):
    if not isinstance(p, list) or len(p) != 2:
        raise ValueError(f"{what} is not [on, full]")
    on, full = _num(p[0], what), _num(p[1], what)
    if full > on + 1e-6:
        raise ValueError(f"{what} has more full output than haze time")
    return [on, full]


def new_hazer():
    return {"on": {s: 0.0 for s in SOURCES},
            "full": {s: 0.0 for s in SOURCES},
            "shows": 0, "show_burns": [], "refills": [], "unsure_s": 0.0}


def read_record(doc):
    """haze.json's content, every field checked; ValueError (with what is
    wrong) for anything this program would not count with."""
    if not isinstance(doc, dict) or doc.get("haze_format") != HAZE_FORMAT:
        raise ValueError("it is not a hazer record this program can read")
    hz = doc.get("hazers")
    if not isinstance(hz, dict):
        raise ValueError("it has no hazers")
    out = {}
    for n, u in hz.items():
        w = f"hazer {n!r}"
        if not isinstance(n, str) or not n or not isinstance(u, dict):
            raise ValueError(f"{w} is not an object")
        rs, sb = u.get("refills", []), u.get("show_burns", [])
        if not isinstance(rs, list) or not isinstance(sb, list):
            raise ValueError(f"{w}: refills and show_burns are lists")
        refills = []
        for i, r in enumerate(rs):
            rw = f"{w}.refills[{i}]"
            if not isinstance(r, dict) or not isinstance(r.get("who"), str):
                raise ValueError(f"{rw} is not a refill")
            refills.append({
                "id": _count(r.get("id"), f"{rw}.id", 1),
                "at": _num(r.get("at"), f"{rw}.at"), "who": r["who"],
                "role": str(r.get("role") or ""),
                "on": _split(r.get("on"), f"{rw}.on"),
                "on_total": _num(r.get("on_total"), f"{rw}.on_total"),
                "full_total": _num(r.get("full_total"),
                                   f"{rw}.full_total"),
                "shows": _count(r.get("shows"), f"{rw}.shows"),
                "unsure_s": _num(r.get("unsure_s", 0.0), f"{rw}.unsure_s"),
                "gaps": _count(r.get("gaps", 0), f"{rw}.gaps")})
        on, full = _split(u.get("on"), f"{w}.on"), \
            _split(u.get("full"), f"{w}.full")
        for s in SOURCES:
            if full[s] > on[s] + 1e-6:
                raise ValueError(f"{w} has more full output than haze time")
        out[n] = {"on": on, "full": full,
                  "shows": _count(u.get("shows", 0), f"{w}.shows"),
                  "show_burns": [_pair(p, f"{w}.show_burns") for p in sb],
                  "refills": refills,
                  "unsure_s": _num(u.get("unsure_s", 0.0), f"{w}.unsure_s")}
    ep = doc.get("episode")
    if ep is not None:
        if not isinstance(ep, dict) or not isinstance(ep.get("burn"), dict):
            raise ValueError("the performance under way is not readable")
        last = ep.get("last_tc")
        ep = {"started": _num(ep.get("started", 0.0), "episode.started"),
              "tc_s": _num(ep.get("tc_s", 0.0), "episode.tc_s"),
              "rehearsal": ep.get("rehearsal") is True,
              "last_tc": None if last is None else
              _num(last, "episode.last_tc"),
              "burn": {str(k): _pair(v, f"episode.burn[{k!r}]")
                       for k, v in ep["burn"].items()}}
    hazing = doc.get("hazing", [])
    if not isinstance(hazing, list) or \
            not all(isinstance(x, str) for x in hazing):
        raise ValueError("hazing is not a list of names")
    ids = [r["id"] for u in out.values() for r in u["refills"]]
    nid = _count(doc.get("next_id", 1), "next_id", 1)
    return {"hazers": out, "episode": ep,
            "next_id": max([nid] + [i + 1 for i in ids]),
            "shows_total": _count(doc.get("shows_total", 0), "shows_total"),
            "gaps": _count(doc.get("gaps", 0), "gaps"),
            "clean": doc.get("clean", True) is not False,
            "hazing": list(hazing),
            "saved_at": doc.get("saved_at")
            if isinstance(doc.get("saved_at"), (int, float)) else None}


def haze_words(s):
    s = max(0.0, float(s or 0.0))
    if s < 90:
        return f"{s:.0f} s"
    return f"{s / 60.0:.1f} min"


# ------------------------------------------------------------ the tracker --

class HazeTracker:
    """The hazer record and its thread. See the module docstring.

    folder          where haze.json lives (the engine's settings folder)
    hazers_get      () -> [(name, haze slot, fan slot)], flamesafe's order
    status_get      () -> True while flamesafe's status is fresh, False
                    while it is stale or lost, None when not known
    min_show_s_get  () -> fuel's min_show_s (the same rule for a show)
    rehearsal_get   () -> True in a programming session
    journal         (text, fault=False) -> anything; failures ignored
    """

    def __init__(self, folder, hazers_get, status_get=lambda: None,
                 min_show_s_get=lambda: MIN_SHOW_S,
                 rehearsal_get=lambda: False, journal=None,
                 clock=time.monotonic, wall=time.time, threaded=True):
        self.folder = folder
        self.path = os.path.join(folder, HAZE_FILE)
        self.levels_path = os.path.join(folder, LEVELS_FILE)
        self._hazers_get = hazers_get
        self._status_get = status_get
        self._min_show_s_get = min_show_s_get
        self._rehearsal_get = rehearsal_get
        self._journal = journal
        self._clock = clock
        self._wall = wall
        self._lock = threading.RLock()       # the record, this thread only
        self._save_lock = threading.Lock()   # the file
        # Appended by the flame link's sender (observe), taken here (pump).
        self._q = collections.deque(maxlen=QUEUE_MAX)
        self.observed = 0
        self.dropped = 0
        self._dropped_seen = 0
        self._link = None
        self.units = {}
        self.next_id = 1
        self.episode = None
        self._ep_last_tc = None
        self._ep_moved_at = None
        self.shows_total = 0
        self.gaps = 0
        self.gap_notes = []
        self.status_unknown = False
        self.fluid, self.config_error = {}, ""
        self._fluid_stamp = None
        self.load_error = ""
        self.save_error = ""
        self.run_error = ""
        self.saved_at = None
        self._clean = False
        self._dirty = False
        self._last_save = clock()
        self._last_t = None
        self._last_on = frozenset()      # hazers with haze in the last frame
        self._hold_left = FLAMESAFE_HOLD_S
        self._hazing = set()             # hazed since the last save
        self._stop = threading.Event()
        self._thread = None
        self._reload_fluid()
        self._load()
        if threaded:
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="ltcplay-haze")
            self._thread.start()

    # -- plumbing ------------------------------------------------------------
    def _note(self, text, fault=False):
        if self._journal is None:
            return
        try:
            self._journal(text, fault=fault)
        except Exception:
            pass

    def _gap(self, why):
        with self._lock:
            self.gaps += 1
            self.gap_notes = (self.gap_notes + [why])[-5:]
            self._dirty = True
        self._note(f"Haze: {why}", fault=True)

    def hazers(self):
        """[(name, haze slot, fan slot)]. Never raises."""
        try:
            out = []
            for n, hs, fs in list(self._hazers_get() or []):
                out.append((str(n), int(hs), int(fs)))
            return out
        except Exception:
            return []

    def _unit(self, name):
        u = self.units.get(name)
        if u is None:
            u = self.units[name] = new_hazer()
            self._dirty = True
        return u

    def _reload_fluid(self):
        """The "fluid" block of ltcplay_hazers.json, read again when the
        file changes. Read only: this module never writes that file.
        Never raises."""
        try:
            st = os.stat(self.levels_path)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        names = [n for n, _h, _f in self.hazers()]
        key = (stamp, tuple(names))
        if key == self._fluid_stamp:
            return
        self._fluid_stamp = key
        fluid, err = {}, ""
        if stamp is not None:
            try:
                with open(self.levels_path, encoding="utf-8-sig") as fh:
                    doc = json.load(fh)
                fluid, err = parse_fluid(
                    doc.get("fluid") if isinstance(doc, dict) else None,
                    names)
            except (OSError, ValueError) as e:
                fluid, err = {}, (f"{LEVELS_FILE} could not be read for "
                                  f"the haze fluid settings ({e}).")
        with self._lock:
            if err and err != self.config_error:
                self._note(f"Haze: {err} The page shows haze seconds only "
                           f"until it is fixed.", fault=True)
            self.fluid, self.config_error = fluid, err

    def _min_show_s(self):
        try:
            v = self._min_show_s_get()
            return float(v) if v else float(MIN_SHOW_S)
        except Exception:
            return float(MIN_SHOW_S)

    # -- the flame link's side -----------------------------------------------
    def attach(self, link):
        """Watch `link`: every frame it sends is handed to observe(),
        exactly as it went on the wire, after it went."""
        self._link = link
        link.haze_observer = self.observe

    def observe(self, tc, wire, test, ok):
        """On the flame link's sender thread, after a frame went out. A
        clock reading, two plain attribute reads and one append: no lock,
        no disk, no journal, no sleep (selftest)."""
        q = self._q
        if len(q) >= QUEUE_MAX:
            self.dropped += 1
        q.append((self._clock(), tc, wire, bool(test), ok,
                  bool(getattr(self._link, "rehearse_on", False))))
        self.observed += 1

    # -- this module's thread -------------------------------------------------
    def _run(self):
        while not self._stop.wait(DRAIN_S):
            self.tick()

    def tick(self):
        """One pass. Never raises; a failure is journaled once and shown."""
        try:
            self._reload_fluid()
            self.pump()
            if self._dirty and \
                    self._clock() - self._last_save >= SAVE_EVERY_S:
                self.save()
            if self.run_error:
                self._note("Haze: counting is working again.")
                self.run_error = ""
        except Exception as e:                    # noqa: BLE001
            why = f"{type(e).__name__}: {e}"
            if not self.run_error:
                self._note(f"Haze: the tracker hit an error and carried on "
                           f"({why}). The hazers are not affected.",
                           fault=True)
            self.run_error = why

    def _status_now(self):
        try:
            got = self._status_get()
        except Exception:
            return None
        return None if got is None else bool(got)

    def pump(self):
        """Count every frame waiting; end a performance whose timecode has
        been gone EPISODE_JOIN_S."""
        frames = []
        q = self._q
        while True:
            try:
                frames.append(q.popleft())
            except IndexError:
                break
        hz = self.hazers()
        if self.dropped != self._dropped_seen:
            n = self.dropped - self._dropped_seen
            self._dropped_seen = self.dropped
            self._gap(f"{n} frame(s) could not be counted (this tracker fell "
                      f"behind), so every hazer may have used more fluid "
                      f"than it shows until its next refill.")
        fresh = self._status_now() if frames else None
        rehearsal = None
        with self._lock:
            if frames:
                self.status_unknown = fresh is not True
            if self.episode is not None or any(
                    f[1] is not None and not f[5] for f in frames):
                try:
                    rehearsal = bool(self._rehearsal_get())
                except Exception:
                    rehearsal = None
            try:
                for f in frames:
                    self._frame(f, hz, fresh, rehearsal)
            except Exception:
                self.gaps += 1
                self.gap_notes = (self.gap_notes + [
                    "Some frames were lost to an error in the haze "
                    "tracker, so every hazer may have used more fluid than "
                    "it shows until its next refill."])[-5:]
                self._dirty = True
                raise
            if self.episode is not None and self._ep_last_tc is not None \
                    and self._clock() - self._ep_last_tc > EPISODE_JOIN_S:
                self._end_episode("its show timecode stopped")

    def _frame(self, f, hz, fresh, rehearsal):
        t, tc, wire, test, ok, rehearse = f
        raw = 0.0 if self._last_t is None else max(t - self._last_t, 0.0)
        dt = min(raw, MAX_FRAME_DT_S)
        self._last_t = t
        secs = tc_seconds(tc) if tc is not None and not rehearse else None
        if secs is not None:
            self._show_tc(t, secs, rehearsal)
        # Time no frame vouched for after one that carried haze (a frame
        # not sent, or a gap): flamesafe holds the last value up to
        # FLAMESAFE_HOLD_S, so up to that much is NOT SURE for each hazer
        # that was on. Never added to the counted seconds.
        lost = min((raw - dt) if ok else raw, self._hold_left)
        if lost > 0 and self._last_on:
            self._hold_left -= lost
            for n in self._last_on:
                u = self._unit(n)
                u["unsure_s"] += lost
            self._dirty = True
        if not ok:
            return
        self._hold_left = FLAMESAFE_HOLD_S
        on_now = {}
        n_wire = len(wire or b"")
        for name, hs, _fs in hz:
            # The HAZE channel only: fan only time uses no fluid.
            v = wire[hs - 1] if 0 < hs <= n_wire else 0
            if v > 0:
                on_now[name] = v
        self._last_on = frozenset(on_now)
        self._hazing |= self._last_on
        if dt <= 0 or not on_now:
            return
        source = "manual" if test else ("show" if secs is not None
                                        else "other")
        for name, v in on_now.items():
            u = self._unit(name)
            w = dt * (v / FULL)
            if fresh is not True:
                # flamesafe's status stale, lost or not known: counted,
                # and said as not sure.
                u["unsure_s"] += dt
            if source == "show" and self.episode is not None:
                b = self.episode["burn"].setdefault(name, [0.0, 0.0])
                b[0] += dt
                b[1] += w
            else:
                src = "other" if source == "show" else source
                u["on"][src] += dt
                u["full"][src] += w
        self._dirty = True

    def _show_tc(self, t, secs, rehearsal):
        """fuel.FuelTracker._show_tc's rule, for the hazers' own record."""
        ep = self.episode
        if ep is not None and ep.get("last_tc") is not None and \
                secs < ep["last_tc"] - 1.0:
            self._end_episode("a new performance started")
            ep = None
        if ep is None:
            ep = self.episode = {"started": self._wall(), "tc_s": 0.0,
                                 "rehearsal": False, "burn": {},
                                 "last_tc": secs}
            self._ep_moved_at = t
            self._dirty = True
        elif secs != ep.get("last_tc"):
            if secs > (ep.get("last_tc") or 0.0) and \
                    self._ep_moved_at is not None:
                ep["tc_s"] += min(max(t - self._ep_moved_at, 0.0),
                                  TC_STEP_MAX_S)
            ep["last_tc"] = secs
            self._ep_moved_at = t
        self._ep_last_tc = t
        if rehearsal:
            ep["rehearsal"] = True

    def _end_episode(self, how):
        """A performance is over: a show for every hazer if it ran fuel's
        min_show_s and was not a programming session; else its haze is
        "other"."""
        ep, self.episode = self.episode, None
        self._ep_last_tc = self._ep_moved_at = None
        if ep is None:
            return
        self._dirty = True
        is_show = (not ep.get("rehearsal")
                   and ep.get("tc_s", 0.0) >= self._min_show_s())
        burn = ep.get("burn") or {}
        names = [n for n, _h, _f in self.hazers()]
        for n in list(burn) + [x for x in names if x not in burn]:
            u = self._unit(n)
            on, full = burn.get(n, [0.0, 0.0])
            src = "show" if is_show else "other"
            u["on"][src] += float(on)
            u["full"][src] += float(full)
            if is_show and n in names:
                u["shows"] += 1
                u["show_burns"] = (u["show_burns"] + [
                    [round(on, 3), round(full, 3)]])[-SHOW_BURNS_KEPT:]
        if is_show:
            self.shows_total += 1
            self._note(f"Haze: a show was counted ({how}).")

    # -- the file -------------------------------------------------------------
    def _doc(self):
        return {"haze_format": HAZE_FORMAT, "next_id": self.next_id,
                "hazers": self.units, "episode": self.episode,
                "shows_total": self.shows_total, "gaps": self.gaps,
                "clean": self._clean,
                "hazing": sorted(self._hazing | self._last_on),
                "saved_at": self._wall()}

    def save(self):
        """Write haze.json atomically now. True when it is on disk. Never
        raises."""
        with self._save_lock:
            with self._lock:
                text = json.dumps(self._doc(), indent=1, sort_keys=True)
                self._dirty = False
                self._last_save = self._clock()
                self._hazing = set(self._last_on)
            try:
                tmp = f"{self.path}.{os.getpid()}.tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(text)
                    fh.write("\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            except Exception as e:                    # noqa: BLE001
                why = f"{type(e).__name__}: {e}"
                with self._lock:
                    self._dirty = True
                    if not self.save_error:
                        self._note(f"Haze: {self.path} could not be written "
                                   f"({why}). The record is kept in memory "
                                   f"and tried again every "
                                   f"{SAVE_EVERY_S:g} s. The hazers are not "
                                   f"affected.", fault=True)
                    self.save_error = why
                return False
        with self._lock:
            if self.save_error:
                self._note("Haze: haze.json is being written again.")
            self.save_error = ""
            self.saved_at = self._wall()
        return True

    def _set_aside(self, why):
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self._wall()))
        kept = f"{self.path}.unreadable-{stamp}"
        n = 2
        while os.path.exists(kept):
            kept = f"{self.path}.unreadable-{stamp}-{n}"
            n += 1
        try:
            os.replace(self.path, kept)
            where = f"It was kept as {os.path.basename(kept)}"
        except OSError as e2:
            where = f"It could not be moved aside either ({e2})"
        self.load_error = (f"haze.json could not be read ({why}). {where}, "
                           f"and the haze record started again: log a refill "
                           f"for each hazer when its tank is next full.")
        self.gaps += 1
        self.gap_notes.append("The haze record was lost and started again, "
                              "so every hazer is not sure until its next "
                              "refill.")
        self._note(f"Haze: {self.load_error}", fault=True)

    def _load(self):
        try:
            with open(self.path, encoding="utf-8-sig") as fh:
                rec = read_record(json.load(fh))
        except FileNotFoundError:
            return
        except Exception as e:                    # noqa: BLE001
            self._set_aside(f"{type(e).__name__}: {e}")
            return
        with self._lock:
            self.units = rec["hazers"]
            self.next_id = rec["next_id"]
            self.shows_total = rec["shows_total"]
            self.gaps = rec["gaps"]
            self.saved_at = rec["saved_at"]
            if not rec["clean"]:
                # LTC Player stopped without closing: up to SAVE_EVERY_S of
                # haze of each hazer hazing at the last save is not counted.
                when = ("" if self.saved_at is None else time.strftime(
                    " (last saved %Y-%m-%d %H:%M:%S)",
                    time.localtime(self.saved_at)))
                for n in rec["hazing"]:
                    self._unit(n)["unsure_s"] += SAVE_EVERY_S
                self.gaps += 1
                self.gap_notes.append(
                    f"LTC Player stopped without closing{when}; up to "
                    f"{SAVE_EVERY_S:g} s of haze before that may not be "
                    f"counted, so every hazer may have used more fluid than "
                    f"it shows until its next refill.")
                self._dirty = True
            if rec["episode"] is not None:
                self.episode = rec["episode"]
                self._end_episode("the engine stopped during it")
                self._dirty = True

    # -- refills --------------------------------------------------------------
    def _snapshot(self, name, u):
        on = dict(u["on"])
        full_total = sum(u["full"].values())
        if self.episode is not None:
            b = self.episode["burn"].get(name)
            if b:
                on["show"] += float(b[0])
                full_total += float(b[1])
        return {"on": {s: round(on[s], 3) for s in SOURCES},
                "on_total": round(sum(on.values()), 3),
                "full_total": round(full_total, 3),
                "shows": u["shows"],
                "unsure_s": round(u.get("unsure_s", 0.0), 3),
                "gaps": self.gaps}

    def refill(self, name, who, role=""):
        """Log a refill of hazer `name`: (ok, sentence). Saved before it
        answers; if the disk refuses, kept in memory and said."""
        names = [n for n, _h, _f in self.hazers()]
        if name not in names:
            return False, (f"There is no hazer called {name!r}. Nothing was "
                           f"logged.")
        with self._lock:
            bid = self.next_id
            self.next_id += 1
            u = self._unit(name)
            snap = self._snapshot(name, u)
            u["refills"] = (u["refills"] + [dict(
                snap, id=bid, at=self._wall(), who=who or "unsigned",
                role=role or "")])[-REFILLS_KEPT:]
            self._dirty = True
        self._note(f"Haze: {who or 'someone'} logged a refill of {name}.")
        if not self.save():
            return True, (f"Refill of {name} logged, but haze.json could "
                          f"NOT be written ({self.save_error}). It is kept "
                          f"in memory and tried again every "
                          f"{SAVE_EVERY_S:g} s.")
        return True, f"Refill of {name} logged."

    # -- what the page shows --------------------------------------------------
    def _hazer_view(self, name, hs, fs):
        u = self.units.get(name) or new_hazer()
        cur = self._snapshot(name, u)
        rs = u["refills"]
        last = rs[-1] if rs else None
        base = last or {"on": {s: 0.0 for s in SOURCES}, "on_total": 0.0,
                        "full_total": 0.0, "shows": 0, "unsure_s": 0.0,
                        "gaps": 0}
        since = {s: round(max(0.0, cur["on"][s] - base["on"].get(s, 0.0)), 1)
                 for s in SOURCES}
        on_s = max(0.0, cur["on_total"] - base["on_total"])
        full_s = max(0.0, cur["full_total"] - base["full_total"])
        unsure = max(0.0, cur["unsure_s"] - base["unsure_s"])
        shows = max(0, cur["shows"] - base["shows"])
        gaps = self.gaps - int(base.get("gaps", 0)) if last else 0
        recent = [p[1] for p in u["show_burns"][-RECENT_SHOWS:]]
        avg = median(recent) if len(recent) >= MIN_SHOWS else None
        fl = self.fluid.get(name) or {}
        lph, tank = fl.get("litres_per_hour"), fl.get("tank_litres")
        v = {"name": name, "haze_slot": hs, "fan_slot": fs,
             "since": since, "on_s": round(on_s, 1),
             "full_s": round(full_s, 1), "unsure_s": round(unsure, 1),
             "shows_since": shows, "refills": len(rs),
             "not_sure": gaps > 0, "avg_show_full_s":
                 None if avg is None else round(avg, 1),
             "litres_per_hour": lph, "tank_litres": tank,
             "litres_used": None, "litres_left": None, "percent": None,
             "color": None, "refill_soon": False,
             "last_refill": None if last is None else {
                 "id": last["id"], "who": last["who"],
                 "at_ms": int(float(last["at"]) * 1000)},
             "big": "", "words": ""}
        haze_line = (f"{haze_words(on_s)} of haze "
                     f"({haze_words(full_s)} at full output)")
        if lph:
            # Not sure seconds count as full output: emptier, never fuller.
            used = (full_s + unsure) / 3600.0 * lph
            v["litres_used"] = round(used, 2)
            if tank and last is not None:
                # A level only from a refill on: before one, how full the
                # tank was is not known.
                left = max(0.0, tank - used)
                v["litres_left"] = round(left, 2)
                p = int(math.floor(100.0 * left / tank))
                v["percent"] = p
                v["color"] = ("red" if p < RED_BELOW else
                              "amber" if p < AMBER_BELOW else "green")
                if v["color"] == "green" and v["not_sure"]:
                    v["color"] = "amber"
                v["refill_soon"] = v["color"] == "red"
        if last is None:
            v["big"] = f"{haze_words(on_s)} haze"
            v["words"] = (f"No refill logged yet: {haze_line} since "
                          f"tracking began. Press Refilled the next time its "
                          f"tank is full.")
        else:
            v["big"] = (f"{v['percent']}% left" if v["percent"] is not None
                        else f"{v['litres_used']:.2f} L used"
                        if v["litres_used"] is not None
                        else f"{haze_words(on_s)} haze")
            v["words"] = (f"{haze_line} and {shows} "
                          f"{'show' if shows == 1 else 'shows'} since the "
                          f"refill.")
            if v["percent"] is not None:
                v["words"] += (f" About {v['litres_left']:.2f} L left of "
                               f"{tank:g} L ({v['percent']} percent).")
            elif v["litres_used"] is not None:
                v["words"] += (f" About {v['litres_used']:.2f} L used. Set "
                               f"tank_litres to see what is left.")
        if v["not_sure"]:
            v["words"] = ("Not sure: some haze since the refill could not be "
                          "counted (see the note at the top). " + v["words"])
        if unsure > 0:
            v["words"] += (f" Includes {haze_words(unsure)} not sure (a "
                           f"restart, or the link to flamesafe stale or "
                           f"lost).")
        return v

    def view(self):
        """The Fuel page's Hazers section. Never raises."""
        try:
            hz = self.hazers()
            with self._lock:
                hazers = [self._hazer_view(n, h, f) for n, h, f in hz]
                notes = list(self.gap_notes)
                if self._link is None:
                    notes.insert(0, "No flame link in this engine, so no "
                                    "haze is being counted.")
                if self.status_unknown:
                    notes.append("flamesafe's status is not fresh right now, "
                                 "so haze sent is counted and marked not "
                                 "sure.")
                return {"available": bool(hazers), "hazers": hazers,
                        "names": [n for n, _h, _f in hz],
                        "notes": notes, "shows_total": self.shows_total,
                        "watching": self._link is not None,
                        "config_error": self.config_error,
                        "refill_soon": [h["name"] for h in hazers
                                        if h["refill_soon"]],
                        "not_sure": [h["name"] for h in hazers
                                     if h["not_sure"]],
                        "fault": " ".join(x for x in (
                            self.load_error,
                            f"haze.json could not be written: "
                            f"{self.save_error}." if self.save_error else "",
                            f"The haze tracker hit an error: "
                            f"{self.run_error}." if self.run_error else "")
                            if x),
                        "saved_at_ms": None if self.saved_at is None
                        else int(float(self.saved_at) * 1000)}
        except Exception as e:                    # noqa: BLE001
            return {"available": False, "hazers": [], "notes": [],
                    "fault": f"The haze view could not be built "
                             f"({type(e).__name__}: {e})."}

    def close(self, timeout=CLOSE_TIMEOUT_S):
        """Stop the thread; count what waits, end a performance, mark
        FLAMESAFE_HOLD_S not sure on each hazer still on, save marked a
        clean stop. Waits at most `timeout`; never raises."""
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)
        self._thread = None
        link = self._link
        if link is not None and getattr(link, "haze_observer", None) == \
                self.observe:
            link.haze_observer = None

        def finish():
            try:
                self.pump()
                with self._lock:
                    if self.episode is not None:
                        self._end_episode("the engine was stopped during it")
                    for n in self._last_on:
                        self._unit(n)["unsure_s"] += FLAMESAFE_HOLD_S
                    self._last_on = frozenset()
                    self._clean = True
            except Exception:                     # noqa: BLE001
                pass
            self.save()
        th = threading.Thread(target=finish, daemon=True,
                              name="ltcplay-haze-close")
        th.start()
        th.join(timeout)
        if th.is_alive():
            self._note(f"Haze: saving haze.json took longer than "
                       f"{timeout:g} s at close; the engine did not wait "
                       f"for it.", fault=True)
            return False
        return True


# ------------------------------------------------------- the page's routes --

POST_ROUTES = ("haze-refill",)


def route_view(tracker):
    """The Hazers section of GET /api/remote/fuel."""
    if tracker is None:
        return {"available": False}
    return tracker.view()


def route_post(tracker, name, body, ctx, users):
    """POST haze-refill {"unit": hazer name}: the fuel refill's permission
    (fuel.may_refill), its confirmation on the page, its audit line (the
    engine's one line per POST). (code, body)."""
    if tracker is None:
        return 404, {"error": "Haze tracking is not set up in this engine "
                              "(it needs hazers in flamesafe's config)."}
    if name != "haze-refill":
        return 404, {"error": "no such thing here"}
    body = body if isinstance(body, dict) else {}
    ok, why = may_refill(ctx, users)
    if not ok:
        return 403, {"error": why}
    unit = body.get("unit")
    if not isinstance(unit, str) or not unit:
        return 400, {"error": "Say which hazer was refilled. Nothing was "
                              "logged."}
    s = getattr(ctx, "session", None) or {}
    who = s.get("name") or getattr(ctx, "device", "") or "unsigned"
    done, text = tracker.refill(unit, who, s.get("role") or "")
    return (200 if done else 400), ({"text": text} if done
                                    else {"error": text})


def build(remote, link, fuel=None, journal=None, threaded=True):
    """The engine's haze tracker (web.serve): the flame link's hazers,
    flamesafe's status freshness from the remote, fuel's min_show_s, and
    the flame link watched."""
    def hazers():
        return [(n, sl[0], sl[1]) for n, sl in link.cfg.hazers]

    def fresh():
        fs = getattr(remote, "flame_status", None)
        fl = fs.view() if fs is not None else None
        if not fl:
            return None
        age = fl.get("age_ms")
        return bool(fl.get("connected") and not fl.get("stale") and
                    age is not None and age <= FLAMESAFE_FRESH_MS)

    def rehearsal():
        try:
            return bool(remote.programming()[0])
        except Exception:                         # noqa: BLE001
            return False

    def min_show_s():
        return (getattr(fuel, "cfg", None) or {}).get("min_show_s",
                                                      MIN_SHOW_S)

    tr = HazeTracker(remote.folder, hazers, status_get=fresh,
                     min_show_s_get=min_show_s, rehearsal_get=rehearsal,
                     journal=journal, threaded=threaded)
    tr.attach(link)
    return tr
