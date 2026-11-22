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
    frame_stale_ms (the link's own flamesafe config, plus HOLD_MARGIN_S:
    hold_s), so up to that much goes on unsure_s
    of each hazer that was on. Never on the counted seconds.
  - frames this tracker could not take (it fell behind), an error in the
    tracker, or LTC Player stopping without closing: a "gap" (every hazer
    reads Not sure until its next refill), and for the stop, SAVE_EVERY_S
    on unsure_s of each hazer that was hazing since the last save.
  - an engine stopping while a hazer was on: hold_s on its
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
litres_per_hour is the fluid used per hour at full output (haze 255). An
admin sets them on the Settings tab's Haze fluid card (POST haze-fluid,
the role matrix's Settings row, the show PC only), written through
hazers.Levels.set_other so the levels are kept; or by hand.

How long a tank lasts (Jeff, 2026-11-19: "evaluating usage and adjusting
available time based on percentages")
=====================================================================

Learned per hazer (flamesafe's entries say only "hazer", never a model,
so there is no kind to share a tank between), the flame fuel learner's
rules (fuel.learned_tank): one tank is the full output seconds between
two refills of that hazer that are not undone; under LEARN_MIN_CYCLES
tanks the SMALLEST, from then the median of the last LEARN_TANKS with
every one far off (under half or over twice that median) left out; a
tank much longer than the others is said (a Refilled press probably
missed). "Learned from N tanks" counts the tanks that number came from. A tank under MIN_TANK_S is a double press, never
learned. Not sure seconds are left out of what a tank taught (it reads
smaller: emptier) and put into what is used (emptier again).

Until a tank is learned, litres_per_hour and tank_litres give one
(tank_litres / litres_per_hour hours at full output), said as
"estimating"; with neither, haze time only.

Percent left = 100 less the full output and not sure seconds since the
refill over the tank, never above 100 or below 0. "Set level now" (an
admin, after looking at the tank) is a CORRECTION: the level from then
is the percent typed, it is kept in its own list (corrections), never a
refill, so it never teaches the learner and never counts as usage; it is
logged with who, the old and the new percent. "Reset learning" starts
the learning again from the next refill and keeps every refill.

Shows left: what is left over the average full output haze of that
hazer's last RECENT_SHOWS full shows (a show is full when it hazed at
least FULL_SHOW of the longest recent one); MIN_SHOWS of them first.

All of it is records and the page's words: nothing here is read by the
flame link, the hazers or flamesafe.
"""
import collections
import json
import math
import os
import threading
import time

from .fuel import (EPISODE_JOIN_S, TC_STEP_MAX_S, tc_seconds, may_refill,
                   may_undo, learned_tank, suspect_cycles)

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
# flamesafe holds the last value for its frame_stale_ms (100 to 2500,
# flamesafe.json), read from the flame link's own config at attach();
# plus a margin. Not known: the longest flamesafe allows, so a stall is
# never left unflagged.
HOLD_MARGIN_S = 0.1
HOLD_DEFAULT_S = 2.5 + HOLD_MARGIN_S
# flamesafe's status counts as fresh for this long (hazers.STATUS_FRESH_S,
# the same number that disarms the hazers).
FLAMESAFE_FRESH_MS = 1000
MIN_SHOW_S = 30             # fuel's default, when fuel's is not known
CLOSE_TIMEOUT_S = 2.0
REFILLS_KEPT = 60
RECENT_BATCHES = 8          # haze refills an admin can undo from the page
SHOW_BURNS_KEPT = 50
RECENT_SHOWS = 10
MIN_SHOWS = 3
AMBER_BELOW = 35
RED_BELOW = 15
LPH_MAX = 50.0
TANK_MAX = 500.0
FLUID_KEYS = frozenset(("litres_per_hour", "tank_litres"))
# Keys of the "fluid" block that are no hazer: kept as they are, never
# offered for Remove.
FLUID_NOT_HAZERS = frozenset(("notes",))
# The tank learner (fuel.learned_tank's rules, fuel's defaults).
LEARN_TANKS = 5             # fuel's learn_refills
OUTLIER_RATIO = 2.0         # fuel's outlier_ratio
MIN_TANK_S = 60.0           # less full output than this: a double press
FULL_SHOW = 0.9             # a full show hazed this much of the longest
CORRECTIONS_KEPT = 20


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
                    not 0 < v <= hi:
                # (NaN and infinity fail the range too.)
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
            "shows": 0, "show_burns": [], "refills": [], "unsure_s": 0.0,
            "corrections": [], "learn_from": 0}


def _read_correction(c, what):
    """One Set level now: never a refill (see the module docstring)."""
    if not isinstance(c, dict) or not isinstance(c.get("who"), str):
        raise ValueError(f"{what} is not a correction")
    old = c.get("old_pct")
    if old is not None:
        old = _count(old, f"{what}.old_pct")
    new = _count(c.get("new_pct"), f"{what}.new_pct")
    if new > 100 or (old is not None and old > 100):
        raise ValueError(f"{what}: a level is 0 to 100 percent")
    return {"id": _count(c.get("id"), f"{what}.id", 1),
            "at": _num(c.get("at"), f"{what}.at"), "who": c["who"],
            "role": str(c.get("role") or ""),
            "screen": str(c.get("screen") or ""),
            "old_pct": old, "new_pct": new,
            "full_total": _num(c.get("full_total"), f"{what}.full_total"),
            "unsure_s": _num(c.get("unsure_s", 0.0), f"{what}.unsure_s")}


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
            und = r.get("undone")
            if und is not None:
                if not isinstance(und, dict) or \
                        not isinstance(und.get("who"), str):
                    raise ValueError(f"{rw}.undone is not who undid it")
                und = {"who": und["who"],
                       "at": _num(und.get("at", 0.0), f"{rw}.undone.at")}
            refills.append({"undone": und,
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
        cs = u.get("corrections", [])
        if not isinstance(cs, list):
            raise ValueError(f"{w}: corrections is a list")
        out[n] = {"on": on, "full": full,
                  "shows": _count(u.get("shows", 0), f"{w}.shows"),
                  "show_burns": [_pair(p, f"{w}.show_burns") for p in sb],
                  "refills": refills,
                  "unsure_s": _num(u.get("unsure_s", 0.0), f"{w}.unsure_s"),
                  "corrections": [_read_correction(
                      c, f"{w}.corrections[{i}]") for i, c in enumerate(cs)],
                  "learn_from": _count(u.get("learn_from", 0),
                                       f"{w}.learn_from")}
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
    ids = [r["id"] for u in out.values()
           for r in u["refills"] + u["corrections"]] + \
        [u["learn_from"] - 1 for u in out.values()]
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
    fluid_writer    (block) -> None: saves the "fluid" block (see above)
    """

    def __init__(self, folder, hazers_get, status_get=lambda: None,
                 min_show_s_get=lambda: MIN_SHOW_S,
                 rehearsal_get=lambda: False, journal=None,
                 clock=time.monotonic, wall=time.time, threaded=True,
                 fluid_writer=None):
        self.folder = folder
        self.path = os.path.join(folder, HAZE_FILE)
        self.levels_path = os.path.join(folder, LEVELS_FILE)
        self._hazers_get = hazers_get
        self._status_get = status_get
        self._min_show_s_get = min_show_s_get
        self._rehearsal_get = rehearsal_get
        self._journal = journal
        # (block or None) -> None, raises ValueError or OSError: writes the
        # "fluid" block of LEVELS_FILE keeping the levels (build(): the
        # engine's hazers.Levels.set_other). None: a Levels of its own.
        self._fluid_writer = fluid_writer
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
        # The "fluid" block as it is in the file (a card save keeps every
        # key in it, "notes" too), and the names in it that are no hazer in
        # flamesafe's config now (the card may remove those).
        self._fluid_raw, self.fluid_gone = {}, []
        self._fluid_stamp = None
        # One Haze fluid card post at a time: its stamp check and its write
        # are one step, so two posts drawn from the same card never both
        # pass. Card posts only: the frames and the counting never take it.
        self._card_lock = threading.Lock()
        self.load_error = ""
        self.save_error = ""
        self.run_error = ""
        self.saved_at = None
        self._clean = False
        self._dirty = False
        self._last_save = clock()
        self._last_t = None
        self._last_on = frozenset()      # hazers with haze in the last frame
        self.hold_s = HOLD_DEFAULT_S
        self._hold_left = self.hold_s
        self._hazing = set()             # hazed since the last save
        self._hazing_saved = set()       # the "hazing" the last save wrote
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
        file changes. Read only here: the Settings card's save goes
        through save_fluid and the levels file's own writer. Never
        raises."""
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
        fluid, err, raw = {}, "", {}
        if stamp is not None:
            try:
                with open(self.levels_path, encoding="utf-8-sig") as fh:
                    doc = json.load(fh)
                blk = doc.get("fluid") if isinstance(doc, dict) else None
                raw = dict(blk) if isinstance(blk, dict) else {}
                fluid, err = parse_fluid(blk, names)
            except (OSError, ValueError) as e:
                fluid, err = {}, (f"{LEVELS_FILE} could not be read for "
                                  f"the haze fluid settings ({e}).")
        gone = sorted(n for n in raw if n != "notes" and n not in names)
        with self._lock:
            if err and err != self.config_error:
                self._note(f"Haze: {err} The page shows haze seconds only "
                           f"until it is fixed.", fault=True)
            self.fluid, self.config_error = fluid, err
            self._fluid_raw, self.fluid_gone = raw, gone

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
        try:
            ms = link.cfg.frame_stale_ms
            if isinstance(ms, (int, float)) and not isinstance(ms, bool) \
                    and 0 < ms <= 60000:
                self.hold_s = ms / 1000.0 + HOLD_MARGIN_S
                self._hold_left = self.hold_s
        except Exception:
            pass                 # not known: HOLD_DEFAULT_S, the longest
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
            if self._hazing - self._hazing_saved:
                # A hazer began hazing since the last save: saved now, so
                # a crash after this still names it as hazing (not sure).
                self.save()
            elif self._dirty and \
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
        # hold_s, so up to that much is NOT SURE for each hazer
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
        self._hold_left = self.hold_s
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
                doc = self._doc()
                text = json.dumps(doc, indent=1, sort_keys=True)
                self._dirty = False
                self._last_save = self._clock()
                self._hazing_saved = set(doc["hazing"])
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
                role=role or "", undone=None)])[-REFILLS_KEPT:]
            self._dirty = True
        self._note(f"Haze: {who or 'someone'} logged a refill of {name}.")
        if not self.save():
            return True, (f"Refill of {name} logged, but haze.json could "
                          f"NOT be written ({self.save_error}). It is kept "
                          f"in memory and tried again every "
                          f"{SAVE_EVERY_S:g} s.")
        return True, f"Refill of {name} logged."

    def undo(self, batch_id, who):
        """Mark haze refill `batch_id` undone (an admin, as fuel's undo):
        never deleted, the record says who undid it and when, and that
        hazer counts from its refill before again. (ok, sentence)."""
        with self._lock:
            hit = None
            for name, u in self.units.items():
                for r in u["refills"]:
                    if r.get("id") == batch_id and not r.get("undone"):
                        r["undone"] = {"who": who or "unsigned",
                                       "at": self._wall()}
                        hit = (name, r)
            if hit is None:
                return False, ("That refill is not in the record or was "
                               "already undone. Nothing was changed.")
            self._dirty = True
        name, r = hit
        self._note(f"Haze: {who or 'someone'} undid the refill of {name} "
                   f"that {r.get('who')} logged.")
        tail = "" if self.save() else (f" haze.json could NOT be written "
                                       f"({self.save_error}); it is tried "
                                       f"again every {SAVE_EVERY_S:g} s.")
        return True, (f"The refill of {name} that {r.get('who')} logged is "
                      f"undone.{tail}")

    def recent_refills(self):
        """The newest RECENT_BATCHES haze refills, for an admin's Undo."""
        out = []
        for name, u in self.units.items():
            for r in u["refills"]:
                out.append({"id": r.get("id"), "who": r.get("who"),
                            "name": name,
                            "at_ms": int(float(r.get("at") or 0) * 1000),
                            "undone": r.get("undone")})
        return sorted(out, key=lambda b: -(b["id"] or 0))[:RECENT_BATCHES]

    # -- the Settings tab's Haze fluid card ---------------------------------
    def stamp(self):
        """What the card was drawn from: the fluid settings, each hazer's
        Reset learning and its Set level now corrections. A save sent with
        another stamp is refused (a stale page never writes over a newer
        change)."""
        import hashlib
        with self._lock:
            key = json.dumps(
                [self.fluid, self.config_error,
                 {n: [u.get("learn_from", 0),
                      [c["id"] for c in u.get("corrections", [])]]
                  for n, u in sorted(self.units.items())
                  if u.get("learn_from") or u.get("corrections")}],
                sort_keys=True)
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

    def _write_fluid(self, block):
        w = self._fluid_writer
        if w is None:
            from . import hazers as hazers_mod
            lv = hazers_mod.Levels(self.folder,
                                   [n for n, _h, _f in self.hazers()])
            w = lambda b: lv.set_other("fluid", b)     # noqa: E731
        w(block)

    def save_fluid(self, name, lph, tank):
        """An admin's litres an hour at full output and tank litres for
        hazer `name` (None clears one), checked as the file is
        (parse_fluid), saved into the "fluid" block of LEVELS_FILE with
        the levels and every other key kept (in the block too: "notes").
        (ok, sentence, (old, new))."""
        names = [n for n, _h, _f in self.hazers()]
        if name not in names:
            return False, (f"There is no hazer called {name!r}. Nothing was "
                           f"changed."), None
        got = {}
        for k, v, hi, what in (("litres_per_hour", lph, LPH_MAX,
                                "Litres per hour at full output"),
                               ("tank_litres", tank, TANK_MAX,
                                "Tank litres")):
            if v is None:
                continue
            ok = not isinstance(v, bool) and isinstance(v, (int, float)) \
                and math.isfinite(v)
            # Checked as it is written (rounded): 0.0004 is written as 0,
            # which the file's own check refuses, so it is refused here.
            r = round(float(v), 3) if ok else None
            if not ok or not 0 < r <= hi:
                return False, (f"{what} is a number over 0 and up to "
                               f"{hi:g} (0.001 or more). Nothing was "
                               f"changed."), None
            got[k] = r
        self._reload_fluid()
        with self._lock:
            err = self.config_error
            old = dict(self.fluid.get(name) or {})
            block = {n: (dict(d) if isinstance(d, dict) else d)
                     for n, d in self._fluid_raw.items()}
        if err:
            return False, (f"{err} Fix the file first. Nothing "
                           f"was changed."), None
        if got:
            block[name] = got
        else:
            block.pop(name, None)
        try:
            self._write_fluid(block or None)
        except (OSError, ValueError) as e:
            return False, (f"The haze fluid settings could not be saved "
                           f"({e}). Nothing was changed."), None
        self._fluid_stamp = None
        self._reload_fluid()
        return True, f"{name}: haze fluid settings saved.", (old, got)

    def remove_gone(self, name, who):
        """The card's Remove: the "fluid" entry of a hazer that is no
        longer in flamesafe's config (renamed or removed), which makes the
        whole block unreadable until it goes. Only such a name; the rest
        of the block is kept as it is. (ok, sentence, what was removed)."""
        self._reload_fluid()
        names = [n for n, _h, _f in self.hazers()]
        with self._lock:
            raw = dict(self._fluid_raw)
        if name in names or name in FLUID_NOT_HAZERS or name not in raw:
            return False, (f"{name} is not a hazer that is gone from "
                           f"flamesafe's config. Nothing was changed."), None
        was = raw.pop(name)
        try:
            self._write_fluid(raw or None)
        except (OSError, ValueError) as e:
            return False, (f"The haze fluid settings could not be saved "
                           f"({e}). Nothing was changed."), None
        self._fluid_stamp = None
        self._reload_fluid()
        self._note(f"Haze: {who or 'someone'} removed the haze fluid "
                   f"settings of {name}, a hazer no longer in flamesafe's "
                   f"config.")
        return True, f"{name}: its haze fluid settings were removed.", was

    def set_level(self, name, pct, who, role="", screen=""):
        """Set level now: an admin looked at the tank and says it is `pct`
        percent full. A CORRECTION: kept apart from the refills, so it is
        never a refill, never usage and never a tank to learn from; the
        level counts from it until the next refill. (ok, sentence, (old
        percent or None, new percent))."""
        names = [n for n, _h, _f in self.hazers()]
        if name not in names:
            return False, (f"There is no hazer called {name!r}. Nothing was "
                           f"changed."), None
        if isinstance(pct, bool) or not isinstance(pct, (int, float)) or \
                not math.isfinite(pct) or not 0 <= pct <= 100 or \
                pct != int(pct):
            return False, ("The level is a whole percent, 0 to 100. Nothing "
                           "was changed."), None
        pct = int(pct)
        with self._lock:
            u = self._unit(name)
            v = self._hazer_view(name, 0, 0)
            if v["tank_s"] is None:
                return False, (f"{name} has no tank size yet: set its litres "
                               f"per hour and tank litres, or log two "
                               f"refills. Nothing was changed."), None
            old = v["percent"]
            snap = self._snapshot(name, u)
            cid = self.next_id
            self.next_id += 1
            u.setdefault("corrections", [])
            u["corrections"] = (u["corrections"] + [{
                "id": cid, "at": self._wall(), "who": who or "unsigned",
                "role": role or "", "screen": screen or "",
                "old_pct": old, "new_pct": pct,
                "full_total": snap["full_total"],
                "unsure_s": snap["unsure_s"]}])[-CORRECTIONS_KEPT:]
            self._dirty = True
        was = "not known" if old is None else f"{old} percent"
        self._note(f"Haze: {who or 'someone'} set the level of {name} from "
                   f"{was} to {pct} percent (a correction, not a refill).")
        if not self.save():
            return True, (f"{name}: level set to {pct} percent, but "
                          f"haze.json could NOT be written "
                          f"({self.save_error}). It is kept in memory and "
                          f"tried again every {SAVE_EVERY_S:g} s."), \
                (old, pct)
        return True, f"{name}: level set to {pct} percent.", (old, pct)

    def reset_learning(self, name, who):
        """Reset learning: hazer `name` learns its tank again from its next
        refill on. Every refill stays in the record. (ok, sentence,
        tanks it had learned from)."""
        names = [n for n, _h, _f in self.hazers()]
        if name not in names:
            return False, (f"There is no hazer called {name!r}. Nothing was "
                           f"changed."), None
        with self._lock:
            u = self._unit(name)
            had = learned_tank(self._cycles(u), LEARN_TANKS,
                               OUTLIER_RATIO)[1]
            u["learn_from"] = self.next_id
            self.next_id += 1
            self._dirty = True
        self._note(f"Haze: {who or 'someone'} reset the learning of {name} "
                   f"(it had learned from {had} tanks). The refills are "
                   f"kept.")
        saved = self.save()
        tail = "" if saved else (f" haze.json could NOT be written "
                                 f"({self.save_error}); it is tried again "
                                 f"every {SAVE_EVERY_S:g} s.")
        return True, (f"{name}: learning starts again from the next refill. "
                      f"The refills are kept.{tail}"), had

    # -- how long a tank lasts (records only) --------------------------------
    def _cycles(self, u):
        """Each tank of this hazer, oldest first: the full output seconds
        between two refills that are not undone, from its Reset learning
        on. Refills only: a Set level now correction is never one, so it
        never teaches a tank length. An undone refill is not one either,
        so an Undo takes back what it taught."""
        rs = sorted((r for r in u["refills"] if not r.get("undone")),
                    key=lambda r: r.get("id", 0))
        out = []
        for a, b in zip(rs, rs[1:]):
            if a.get("id", 0) < u.get("learn_from", 0):
                continue
            c = float(b["full_total"]) - float(a["full_total"])
            if c >= MIN_TANK_S:
                out.append(c)
        return out

    def _tank(self, name, u):
        """(full output seconds of a full tank or None, "learned",
        "estimating" or "", tanks learned from, firm, a tank much longer
        than the others)."""
        cycles = self._cycles(u)
        got, used, firm = learned_tank(cycles, LEARN_TANKS, OUTLIER_RATIO)
        if got is not None:
            # "Learned from N tanks": the tanks the number came from (the
            # last LEARN_TANKS, any far off left out), not every tank.
            return (got, "learned", used, firm,
                    bool(suspect_cycles(cycles, LEARN_TANKS)))
        fl = self.fluid.get(name) or {}
        lph, tank = fl.get("litres_per_hour"), fl.get("tank_litres")
        if lph and tank:
            # The set numbers, until a tank is learned: tank / flow hours.
            return tank / lph * 3600.0, "estimating", 0, False, False
        return None, "", 0, False, False

    def _show_avg(self, u):
        """The average full output haze of this hazer's last RECENT_SHOWS
        full shows (FULL_SHOW of the longest one or more), or None under
        MIN_SHOWS of them."""
        recent = u["show_burns"][-RECENT_SHOWS:]
        longest = max([p[0] for p in recent] or [0.0])
        full = [p[1] for p in recent
                if longest > 0 and p[0] >= FULL_SHOW * longest]
        if len(full) < MIN_SHOWS:
            return None
        return sum(full) / len(full)

    # -- what the page shows --------------------------------------------------
    def _hazer_view(self, name, hs, fs):
        u = self.units.get(name) or new_hazer()
        cur = self._snapshot(name, u)
        # An undone refill never counts (fuel's rule).
        rs = [r for r in u["refills"] if not r.get("undone")]
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
        avg = self._show_avg(u)
        fl = self.fluid.get(name) or {}
        lph, tank = fl.get("litres_per_hour"), fl.get("tank_litres")
        tank_s, basis, tanks, firm, suspect = self._tank(name, u)
        # The level counts from the newest of the last refill (100
        # percent) and a Set level now after it (the percent typed).
        corr = [c for c in u.get("corrections", [])
                if last is None or c["id"] > last["id"]]
        lvl = corr[-1] if corr else None
        v = {"name": name, "haze_slot": hs, "fan_slot": fs,
             "since": since, "on_s": round(on_s, 1),
             "full_s": round(full_s, 1), "unsure_s": round(unsure, 1),
             "shows_since": shows, "refills": len(rs),
             "not_sure": gaps > 0, "avg_show_full_s":
                 None if avg is None else round(avg, 1),
             "litres_per_hour": lph, "tank_litres": tank,
             "litres_used": None, "litres_left": None, "percent": None,
             "color": None, "refill_soon": False,
             "tank_s": None if tank_s is None else round(tank_s, 1),
             "tank_litres_learned": None, "basis": basis,
             "tanks_learned": tanks, "learn_firm": firm,
             "suspect": suspect, "shows_left": None, "sub": "",
             "level_set": None if lvl is None else {
                 "who": lvl["who"], "pct": lvl["new_pct"],
                 "at_ms": int(float(lvl["at"]) * 1000)},
             "last_refill": None if last is None else {
                 "id": last["id"], "who": last["who"],
                 "at_ms": int(float(last["at"]) * 1000)},
             "big": "", "words": ""}
        if tank_s is not None and basis == "learned" and lph:
            v["tank_litres_learned"] = round(tank_s / 3600.0 * lph, 2)
        haze_line = (f"{haze_words(on_s)} of haze "
                     f"({haze_words(full_s)} at full output)")
        if lph:
            # Not sure seconds count as full output: emptier, never fuller.
            used = (full_s + unsure) / 3600.0 * lph
            v["litres_used"] = round(used, 2)
        if tank_s and (last is not None or lvl is not None):
            # A level only from a refill or a Set level now on: before
            # one, how full the tank was is not known. Not sure seconds
            # count as full output (emptier, never fuller).
            if lvl is not None:
                start = float(lvl["new_pct"])
                gone = max(0.0, cur["full_total"] - lvl["full_total"]) + \
                    max(0.0, cur["unsure_s"] - lvl["unsure_s"])
            else:
                start, gone = 100.0, full_s + unsure
            exact = min(100.0, max(0.0, start - 100.0 * gone / tank_s))
            p = int(math.floor(exact))
            v["percent"] = p
            left_s = tank_s * exact / 100.0
            if lph:
                v["litres_left"] = round(left_s / 3600.0 * lph, 2)
            if avg:
                v["shows_left"] = int(math.floor(left_s / avg))
            v["color"] = ("red" if p < RED_BELOW else
                          "amber" if p < AMBER_BELOW else "green")
            if v["color"] == "green" and v["not_sure"]:
                v["color"] = "amber"
            v["refill_soon"] = v["color"] == "red" or v["shows_left"] == 0
        if basis == "learned":
            v["sub"] = (f"Learned from {tanks} "
                        f"{'tank' if tanks == 1 else 'tanks'}" +
                        ("" if firm else ", still learning"))
        elif basis == "estimating":
            v["sub"] = "Estimating (from the set numbers)"
        if v["shows_left"] is not None:
            v["sub"] += (f". {v['shows_left']} "
                         f"{'show' if v['shows_left'] == 1 else 'shows'} "
                         f"left")
        if last is None and lvl is None:
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
            if v["percent"] is not None and v["litres_left"] is not None:
                of = tank if basis == "estimating" else \
                    v["tank_litres_learned"]
                v["words"] += (f" About {v['litres_left']:.2f} L left of "
                               f"{of:g} L ({v['percent']} percent).")
            elif v["percent"] is not None:
                v["words"] += f" About {v['percent']} percent left."
            elif v["litres_used"] is not None:
                v["words"] += (f" About {v['litres_used']:.2f} L used. Set "
                               f"tank_litres to see what is left.")
            if lvl is not None:
                v["words"] += (f" Level set by {lvl['who']} to "
                               f"{lvl['new_pct']} percent.")
        if basis == "learned":
            v["words"] += f" {v['sub'].split('.')[0]}."
        elif basis == "estimating" and v["percent"] is not None:
            v["words"] += (" Estimating from the set numbers until a tank "
                           "is learned.")
        if v["shows_left"] is not None:
            v["words"] += f" About {v['shows_left']} shows left."
        if suspect:
            v["words"] += (" One tank was much longer than the others: a "
                           "Refilled press was probably missed.")
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
            self._reload_fluid()
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
                        "recent_refills": self.recent_refills(),
                        "notes": notes, "shows_total": self.shows_total,
                        "watching": self._link is not None,
                        "config_error": self.config_error,
                        "fluid_gone": list(self.fluid_gone),
                        "stamp": self.stamp(),
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
        hold_s not sure on each hazer still on, save marked a
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
                        self._unit(n)["unsure_s"] += self.hold_s
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

POST_ROUTES = ("haze-refill", "haze-undo")
# The Settings tab's Haze fluid card (Jeff, 2026-11-19): the role matrix's
# Settings row (users.py), on the show PC only (remote.LOCAL_ONLY).
FLUID_ROUTES = ("haze-fluid",)
FLUID_ACTIONS = ("save", "level", "reset", "remove")


def route_view(tracker, ctx=None, users=None):
    """The Hazers section of GET /api/remote/fuel, with whether this
    request may undo a haze refill (fuel.may_undo: an admin)."""
    if tracker is None:
        return {"available": False}
    v = tracker.view()
    ok, why = may_undo(ctx, users)
    v["may_undo"], v["undo_why"] = ok, why
    return v


def route_post(tracker, name, body, ctx, users):
    """POST haze-refill {"unit": hazer name}: the fuel refill's permission
    (fuel.may_refill), its confirmation on the page, its audit line (the
    engine's one line per POST). POST haze-undo {"id": n}: fuel-undo's
    (fuel.may_undo: a signed-in admin; with no users made yet only the
    show machine itself, never a device on the network or over a VPN).
    (code, body)."""
    if tracker is None:
        return 404, {"error": "Haze tracking is not set up in this engine "
                              "(it needs hazers in flamesafe's config)."}
    if name not in POST_ROUTES:
        return 404, {"error": "no such thing here"}
    body = body if isinstance(body, dict) else {}
    s = getattr(ctx, "session", None) or {}
    who = s.get("name") or getattr(ctx, "device", "") or "unsigned"
    if name == "haze-undo":
        ok, why = may_undo(ctx, users)
        if not ok:
            return 403, {"error": why}
        bid = body.get("id")
        if isinstance(bid, bool) or not isinstance(bid, int):
            return 400, {"error": "Say which refill to undo. Nothing was "
                                  "changed."}
        done, text = tracker.undo(bid, who)
        return (200 if done else 409), ({"text": text} if done
                                        else {"error": text})
    ok, why = may_refill(ctx, users)
    if not ok:
        return 403, {"error": why}
    unit = body.get("unit")
    if not isinstance(unit, str) or not unit:
        return 400, {"error": "Say which hazer was refilled. Nothing was "
                              "logged."}
    done, text = tracker.refill(unit, who, s.get("role") or "")
    return (200 if done else 400), ({"text": text} if done
                                    else {"error": text})


def may_set_fluid(ctx, users):
    """The Haze fluid card: the role matrix's Settings row (admin by
    default), signed in; with nobody able to sign in yet, only the show
    machine itself. web.py's role check asks the same first; this is the
    route's own. (ok, why)."""
    active = False
    try:
        active = users is not None and bool(users.active())
    except Exception:                             # noqa: BLE001
        active = True                              # not known: refused
    if not active:
        if getattr(ctx, "local", False):
            return True, ""
        return False, ("The haze fluid settings are changed on the show "
                       "machine itself. Nothing was changed.")
    s = getattr(ctx, "session", None) or {}
    name, role = s.get("name"), s.get("role")
    if not name:
        return False, ("Sign in to change the haze fluid settings (the user "
                       "chip at the top). Nothing was changed.")
    try:
        ok = bool(users.may(role, "settings"))
    except Exception:                             # noqa: BLE001
        ok = False
    if not ok:
        return False, (f"{name} is signed in as {role}; the haze fluid "
                       f"settings need the Settings permission (an admin). "
                       f"Nothing was changed.")
    return True, ""


def _num_or_none(v):
    """A number from the card: blank (None or "") is not set."""
    if v is None or v == "":
        return None, True
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return v, False
    return v, True


def route_fluid(tracker, body, ctx, users, audit=None):
    """POST haze-fluid (the Settings tab's Haze fluid card): {"action":
    "save", "hazer", "litres_per_hour", "tank_litres", "stamp"},
    {"action": "level", "hazer", "percent", "stamp"} (Set level now) or
    {"action": "reset", "hazer", "confirm": true, "stamp"} (Reset
    learning) or {"action": "remove", "hazer", "stamp"} (the settings of a
    hazer no longer in flamesafe's config). Every change is audit logged with who and the screen.
    Refused, with "Nothing was changed.", when the stamp is not the
    settings now (a stale page). Records and words only: nothing here
    reaches a hazer or flamesafe. (code, body)."""
    if tracker is None:
        return 404, {"error": "Haze tracking is not set up in this engine "
                              "(it needs hazers in flamesafe's config)."}
    ok, why = may_set_fluid(ctx, users)
    if not ok:
        return 403, {"error": why}
    body = body if isinstance(body, dict) else {}
    act, name = body.get("action"), body.get("hazer")
    if act not in FLUID_ACTIONS:
        return 400, {"error": "Say what to do. Nothing was changed."}
    if not isinstance(name, str) or not name:
        return 400, {"error": "Say which hazer. Nothing was changed."}
    s = getattr(ctx, "session", None) or {}
    who = s.get("name") or getattr(ctx, "device", "") or "unsigned"
    role = s.get("role") or ""
    screen = (getattr(ctx, "screen", "") if getattr(ctx, "local", False)
              else getattr(ctx, "device", "")) or "Rack screen"
    if act == "save":
        lph, ok1 = _num_or_none(body.get("litres_per_hour"))
        tank, ok2 = _num_or_none(body.get("tank_litres"))
        if not (ok1 and ok2):
            return 400, {"error": "Litres per hour and tank litres are "
                                  "numbers. Nothing was changed."}
    if act == "reset" and body.get("confirm") is not True:
        return 400, {"error": "Reset learning needs its confirm. Nothing "
                              "was changed."}
    extra = {}
    # The stamp check and the change are one step (a second post drawn
    # from the same card waits, then finds the stamp moved on).
    with tracker._card_lock:
        tracker._reload_fluid()          # a hand edit since counts too
        if body.get("stamp") != tracker.stamp():
            return 409, {"error": "The haze fluid settings changed since "
                                  "this page drew them. Look again. Nothing "
                                  "was changed.", "stale": True}
        if act == "save":
            done, text, ch = tracker.save_fluid(name, lph, tank)
            if done:
                (old, new) = ch
                extra = {"old": json.dumps(old, sort_keys=True),
                         "new": json.dumps(new, sort_keys=True)}
        elif act == "level":
            done, text, ch = tracker.set_level(name, body.get("percent"),
                                               who, role, screen)
            if done:
                extra = {"old": "not known" if ch[0] is None else ch[0],
                         "new": ch[1], "correction": True}
        elif act == "remove":
            done, text, was = tracker.remove_gone(name, who)
            if done:
                extra = {"old": json.dumps(was, sort_keys=True),
                         "new": "removed"}
        else:
            done, text, had = tracker.reset_learning(name, who)
            if done:
                extra = {"tanks_before": had}
        stamp = tracker.stamp()
    if not done:
        return 400, {"error": text}
    if audit is not None:
        try:
            audit.event(user=who, surface="web",
                        action=f"haze-fluid-{act}", target=f"hazer={name}",
                        result="accepted", source=screen, screen=screen,
                        **extra)
        except Exception:                         # noqa: BLE001
            pass
    return 200, {"text": text, "stamp": stamp}


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

    def fluid_writer(block):
        # The engine's own levels file writer (hazers.Levels), so a level
        # set and a fluid save share its lock and keep each other's keys.
        lv = getattr(getattr(remote, "hazers", None), "levels", None)
        if lv is None:
            from . import hazers as hazers_mod
            lv = hazers_mod.Levels(remote.folder, [n for n, _s in
                                                   link.cfg.hazers])
        lv.set_other("fluid", block)

    tr = HazeTracker(remote.folder, hazers, status_get=fresh,
                     min_show_s_get=min_show_s, rehearsal_get=rehearsal,
                     journal=journal, threaded=threaded,
                     fluid_writer=fluid_writer)
    tr.attach(link)
    return tr
