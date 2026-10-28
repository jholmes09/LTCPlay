"""The fuel tracker (Jeff, 2026-10-26): how much fuel each flame unit has
left, as a health bar and the number of shows left. FUEL.md says it in
plain words; read it first.

What is tracked
===============

Every G-Flame (machines of kind "gflame") has its own 100 lb propane tank,
every Showven wave flamer (kind "waver") its own 15 gallon tank. Fireballs
are not tracked (their sensors are being looked at separately). The machine
list is the Morning page's (morning.py, ltcplay_morning.json), read again
on every pass, so a changed list is followed: a new unit starts with no
refill logged, and a unit that left the list keeps its record in fuel.json
but is not shown.

IT ERRS TOWARD EMPTY SOONER (review of #123): wherever it cannot know,
it counts more flame, learns a smaller tank, or says "not sure" and is
never green. It never errs toward "plenty".

How fuel use is measured
========================

At the flame link, per unit, as SECONDS OF FLAME: the time a frame that
actually went out to flamesafe carried a value at or above that kind's
fire value (fire_at: 229 for a G-Flame, flamesafe/rules.py
GFLAME_FIRE_AT; 254 for a Showven, rules.SHOWVEN_CF2_FIRE_AT) on any of
the unit's fire slots, unless flamesafe's status says the unit's group is
NOT armed or the unit is locked out (flamesafe holds those fire slots at
zero). When flamesafe's armed state is not known (no status mirror, or a
stale one) the flame is COUNTED anyway: that can only count too much. A
Showven firing from its CH5 preset sequence is NOT counted (only its fire
slot at fire_at); set fire_at per kind if a show uses another channel.

Each frame is put in one of three piles:

  show    a performance: the show audio clock playing a cue (the flame
          link's own show timecode), not a programming session, and not
          scrubbing. Counted as a show once its timecode has ADVANCED for
          min_show_s (a Hold's frozen frame adds nothing), whether it ran to
          its end or was aborted, started by the schedule or by hand. It
          ends when the timecode has been gone EPISODE_JOIN_S; back sooner
          and carrying on, it is the same show. Shorter (a start that failed) or a
          programming session: its burn goes to "other", never a show.
  purge   the Morning page's purge frame. Counted for a kind only when its
          count_purge is true: true for G-Flames (they purge from their own
          tank), false for Showven units (they have dedicated purge fuel).
  other   scrubbing with flames, live flame programming, a rehearsal run:
          real fuel, never a show.

How it never touches the flames
===============================

FlameLink.send_frame hands each frame it sent to `observer` (one line in
flamelink.py, inside try/except, after the frame is out). The observer
here only appends a tuple to a bounded deque: no lock, no disk, no
journal. Everything else (the counting, fuel.json, the page) runs on this
module's own thread under its own lock. A failure here (a full disk, a
broken fuel.json, a bug) is journaled once and shown on the Fuel page; it
never raises into the sender, never slows it and never changes a value.

How it learns
=============

Learning is per kind (every G-Flame tank is the same 100 lb tank): the
burn seconds between two refills of one unit is one tank's worth (a
"cycle"). With fewer than LEARN_MIN_CYCLES (3) cycles a kind's tank is the
SMALLEST cycle and the page says it is still learning (a forgotten
Refilled press makes one cycle span two tanks: only a small one is
trusted). From three on it is the median of the last learn_refills, any
cycle under 1/outlier_ratio or over outlier_ratio times it left out first.
A cycle more than SUSPECT_RATIO times the others is named on the page.
Until a kind has a cycle the page says "Learning"; Jeff may give a starting
estimate in shows per tank (start_shows_per_tank), shown as rough and
never green when it is only a count of shows.

Shows left = what is left of the tank / that unit's average burn per show
(its last recent_shows shows; at least min_shows of them, else the page
says how many more it needs). The health bar is the fraction left; green,
amber and red by amber_below_percent and red_below_percent, and "Refill
soon" at refill_soon_shows shows left or fewer. Percent and shows left
are rounded DOWN.

Flame that may have gone uncounted (frames dropped because this thread fell
behind, the last SAVE_EVERY_S before a crash, frames lost to an error) is a
"gap": the page says so at the top, and every unit with a gap since its
refill reads "Not sure" and is never green.

What is kept, and where
=======================

fuel.json in the engine's settings folder (%LOCALAPPDATA%\\ltcplay on the
show PC, appdata.py; the same folder as the Morning page's files), written
atomically (a temporary file, fsync, then replace) with a format number.
Written at once on every refill and undo (a refill is never only in
memory if the disk takes it), at least every SAVE_EVERY_S while anything
changed, and on close. Burn is counted in cumulative seconds per source
that never go down; a refill records the counters at that moment, so
"since the refill" and every cycle are differences, and an undo (an admin
marks a refill undone, never deletes it) puts everything back exactly. A
performance under way is saved too: after an engine restart it is closed
and counted if it had run long enough. Times on the page are wall clock
for reading only; every duration is counted on the monotonic clock, so a
clock change never adds or takes away burn.
"""
import collections
import json
import math
import os
import threading
import time

FUEL_FILE = "fuel.json"
FUEL_FORMAT = 1
CONFIG_FILE = "ltcplay_morning.json"
TRACKED_KINDS = ("gflame", "waver")
SAVE_EVERY_S = 30.0         # at least this often while anything changed
DRAIN_S = 0.25              # the tracker's own pass
MAX_FRAME_DT_S = 0.1        # one frame never counts for more than this
QUEUE_MAX = 4000            # frames waiting (100 s at 40 a second)
# A performance ends once its show timecode has been gone this long. If the
# timecode comes back sooner, carrying on from where it was, it is the same
# performance (a short gap never splits one show into two).
EPISODE_JOIN_S = 30.0
# Show length is the time the show timecode actually ADVANCES: each change
# counts the time since the last change, at most this much, so a Hold's
# frozen frame adds nothing to a show's length.
TC_STEP_MAX_S = 0.5
TC_FPS = 30.0               # the show audio's timecode (clock.MASTER_FPS)
# Until a kind has this many tanks between refills, its tank is the
# SMALLEST one seen (one forgotten Refilled press can only make a tank look
# bigger) and the page says it is still learning.
LEARN_MIN_CYCLES = 3
SUSPECT_RATIO = 1.6         # a tank this much longer than the others: say so
CLOSE_TIMEOUT_S = 2.0       # close() never waits longer than this on disk
REFILLS_KEPT = 60           # per unit, in fuel.json
SHOW_BURNS_KEPT = 50        # per unit
RECENT_BATCHES = 8          # refills an admin can undo from the page
LABEL_MAX = 40

# The defaults (Jeff, 2026-10-26). Every one can be set in
# ltcplay_morning.json under "fuel".
DEFAULT_CONFIG = {
    "kinds": {
        "gflame": {"label": "100 lb propane", "fire_at": 229,
                   "count_purge": True, "start_shows_per_tank": None},
        "waver": {"label": "15 gal", "fire_at": 254,
                  "count_purge": False, "start_shows_per_tank": None},
    },
    "learn_refills": 5,
    "outlier_ratio": 2.0,
    "recent_shows": 10,
    "min_shows": 3,
    "min_show_s": 30,
    "amber_below_percent": 35,
    "red_below_percent": 15,
    "refill_soon_shows": 3,
}
TOP_KEYS = frozenset(("notes", "kinds", "learn_refills", "outlier_ratio",
                      "recent_shows", "min_shows", "min_show_s",
                      "amber_below_percent", "red_below_percent",
                      "refill_soon_shows"))
KIND_KEYS = frozenset(("label", "fire_at", "count_purge",
                       "start_shows_per_tank"))
KIND_WORDS = {"gflame": "G-Flame", "waver": "Showven"}
SOURCES = ("show", "purge", "other")


class FuelConfigError(ValueError):
    """The fuel block of ltcplay_morning.json is wrong, in a sentence."""


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _whole(doc, key, lo, hi, where, what):
    v = doc[key]
    if not _is_int(v) or not lo <= v <= hi:
        raise FuelConfigError(f"{where}: fuel.{key} is {what}, a whole "
                              f"number {lo} to {hi}, not {v!r}.")
    return v


def _only(doc, keys, where):
    unknown = sorted(str(k) for k in doc if k not in keys)
    if unknown:
        raise FuelConfigError(
            f"{where} has no setting {', '.join(repr(k) for k in unknown)}; "
            f"it takes {', '.join(sorted(keys))}.")


def parse_config(doc, where=CONFIG_FILE):
    """The "fuel" block, checked strictly, merged over DEFAULT_CONFIG.
    None (no block) is the defaults. Raises FuelConfigError."""
    out = json.loads(json.dumps(DEFAULT_CONFIG))
    if doc is None:
        return out
    if not isinstance(doc, dict):
        raise FuelConfigError(f"{where}: fuel has to be an object.")
    _only(doc, TOP_KEYS, f"{where}: fuel")
    if "notes" in doc and not isinstance(doc["notes"], str):
        raise FuelConfigError(f"{where}: fuel.notes has to be text.")
    kinds = doc.get("kinds", {})
    if not isinstance(kinds, dict):
        raise FuelConfigError(f"{where}: fuel.kinds has to be an object "
                              f"with gflame and waver in it.")
    for k, kd in kinds.items():
        if k == "fireball":
            raise FuelConfigError(
                f"{where}: fuel.kinds has fireball. Fireballs are not "
                f"tracked by the fuel page; take it out.")
        if k not in TRACKED_KINDS:
            raise FuelConfigError(
                f"{where}: fuel.kinds has {k!r}; the kinds tracked are "
                f"gflame and waver.")
        w = f"{where}: fuel.kinds.{k}"
        if not isinstance(kd, dict):
            raise FuelConfigError(f"{w} has to be an object.")
        _only(kd, KIND_KEYS, w)
        o = out["kinds"][k]
        if "label" in kd:
            lab = kd["label"]
            if not isinstance(lab, str) or not lab.strip() or \
                    len(lab) > LABEL_MAX:
                raise FuelConfigError(f"{w}.label has to be words, up to "
                                      f"{LABEL_MAX} characters, such as "
                                      f"\"100 lb propane\".")
            o["label"] = lab.strip()
        if "fire_at" in kd:
            v = kd["fire_at"]
            if not _is_int(v) or not 1 <= v <= 255:
                raise FuelConfigError(f"{w}.fire_at is the lowest fire "
                                      f"value that makes flame, a whole "
                                      f"number 1 to 255, not {v!r}.")
            o["fire_at"] = v
        if "count_purge" in kd:
            if not isinstance(kd["count_purge"], bool):
                raise FuelConfigError(f"{w}.count_purge has to be true or "
                                      f"false.")
            o["count_purge"] = kd["count_purge"]
        if "start_shows_per_tank" in kd:
            v = kd["start_shows_per_tank"]
            if v is not None and (not _is_int(v) or not 1 <= v <= 10000):
                raise FuelConfigError(f"{w}.start_shows_per_tank is your "
                                      f"starting guess of shows per tank, a "
                                      f"whole number 1 to 10000, or null.")
            o["start_shows_per_tank"] = v
    for key, lo, hi, what in (
            ("learn_refills", 1, 50, "how many recent refills it learns "
                                     "from"),
            ("recent_shows", 1, 100, "how many recent shows the average "
                                     "burn per show is taken over"),
            ("min_shows", 1, 50, "how many shows a unit needs before it "
                                 "says shows left"),
            ("min_show_s", 1, 7200, "how many seconds of show timecode "
                                    "make a performance count as a show"),
            ("amber_below_percent", 1, 99, "the percent left under which "
                                           "the bar is amber"),
            ("red_below_percent", 0, 98, "the percent left under which the "
                                         "bar is red"),
            ("refill_soon_shows", 0, 100, "the shows left at or under "
                                          "which it says Refill soon")):
        if key in doc:
            out[key] = _whole(doc, key, lo, hi, where, what)
    if "outlier_ratio" in doc:
        v = doc["outlier_ratio"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or \
                not 1.2 <= v <= 10:
            raise FuelConfigError(f"{where}: fuel.outlier_ratio is how far "
                                  f"from the usual a tank may be and still "
                                  f"be learned from, a number 1.2 to 10, "
                                  f"not {v!r}.")
        out["outlier_ratio"] = float(v)
    if out["min_shows"] > out["recent_shows"]:
        raise FuelConfigError(f"{where}: fuel.min_shows ({out['min_shows']}) "
                              f"cannot be more than fuel.recent_shows "
                              f"({out['recent_shows']}).")
    if out["red_below_percent"] >= out["amber_below_percent"]:
        raise FuelConfigError(f"{where}: fuel.red_below_percent has to be "
                              f"under fuel.amber_below_percent.")
    return out


def load_config(path):
    """(config, problem sentence or ""). A missing file or no fuel block is
    the defaults; a wrong block is the defaults AND its sentence, which the
    page shows."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return parse_config(None), ""
    except (OSError, ValueError) as e:
        return parse_config(None), f"{path} could not be read: {e}"
    try:
        block = doc.get("fuel") if isinstance(doc, dict) else None
        return parse_config(block, os.path.basename(path)), ""
    except FuelConfigError as e:
        return parse_config(None), str(e)


# ------------------------------------------------------------- the maths --

def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0


def learned_tank(cycles, n, ratio):
    """(seconds of flame per tank, how many cycles it used, firm) from
    `cycles` (oldest first), erring toward EMPTY SOONER:

      fewer than LEARN_MIN_CYCLES: the SMALLEST cycle, not firm (a
      forgotten Refilled press makes a cycle span two tanks, so only a
      small one can be trusted);
      LEARN_MIN_CYCLES or more: the median of the last `n`, any cycle
      under median/ratio or over median*ratio left out first; firm.

    (None, 0, False) with no cycle."""
    last = [c for c in cycles if c > 0][-n:]
    if not last:
        return None, 0, False
    if len(last) < LEARN_MIN_CYCLES:
        return min(last), len(last), False
    m = median(last)
    kept = [c for c in last if m / ratio <= c <= m * ratio]
    if kept:
        last = kept
    return median(last), len(last), True


def suspect_cycles(cycles, n):
    """The cycles among the last `n` more than SUSPECT_RATIO times the
    median of the others: likely a Refilled press that was missed."""
    last = [c for c in cycles if c > 0][-n:]
    out = []
    for i, c in enumerate(last):
        others = last[:i] + last[i + 1:]
        if others and c > SUSPECT_RATIO * median(others):
            out.append(c)
    return out


def burn_words(s):
    s = max(0.0, float(s or 0.0))
    if s < 90:
        return f"{s:.0f} s of flame"
    return f"{s / 60.0:.1f} min of flame"


def _plural(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _new_unit(m):
    return {"kind": m.kind, "name": m.name,
            "burn": {s: 0.0 for s in SOURCES}, "shows": 0,
            "show_burns": [], "refills": [], "unsure_s": 0.0}


def _total(burn):
    return sum(float(burn.get(s, 0.0) or 0.0) for s in SOURCES)


def tc_seconds(tc):
    """"HH:MM:SS:FF" (or ;FF) as seconds at TC_FPS, or None."""
    try:
        h, m, s, f = int(tc[0:2]), int(tc[3:5]), int(tc[6:8]), int(tc[9:11])
    except (TypeError, ValueError, IndexError):
        return None
    return h * 3600 + m * 60 + s + f / TC_FPS


# ------------------------------------------------- reading fuel.json back --

def _num(v, what, lo=0.0):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or \
            not math.isfinite(v) or v < lo:
        raise ValueError(f"{what} is not a number of {lo:g} or more: "
                         f"{v!r}")
    return float(v)


def _count(v, what, lo=0):
    if not _is_int(v) or v < lo:
        raise ValueError(f"{what} is not a whole number of {lo} or more: "
                         f"{v!r}")
    return v


def _burn(d, what):
    if not isinstance(d, dict):
        raise ValueError(f"{what} is not an object")
    return {s: _num(d.get(s, 0.0), f"{what}.{s}") for s in SOURCES}


def _read_refill(r, what):
    if not isinstance(r, dict):
        raise ValueError(f"{what} is not an object")
    und = r.get("undone")
    if und is not None:
        if not isinstance(und, dict) or not isinstance(und.get("who"), str):
            raise ValueError(f"{what}.undone is not who undid it")
        und = {"who": und["who"], "at": _num(und.get("at", 0.0),
                                             f"{what}.undone.at")}
    if not isinstance(r.get("who"), str):
        raise ValueError(f"{what}.who is not a name")
    return {"id": _count(r.get("id"), f"{what}.id", 1),
            "at": _num(r.get("at"), f"{what}.at"),
            "who": r["who"], "role": str(r.get("role") or ""),
            "burn": _burn(r.get("burn"), f"{what}.burn"),
            "total": _num(r.get("total"), f"{what}.total"),
            "shows": _count(r.get("shows"), f"{what}.shows"),
            "unsure_s": _num(r.get("unsure_s", 0.0), f"{what}.unsure_s"),
            "gaps": _count(r.get("gaps", 0), f"{what}.gaps"),
            "undone": und}


def read_record(doc):
    """fuel.json's content, every field checked; ValueError (with what is
    wrong) for anything this program would not count with."""
    if not isinstance(doc, dict) or doc.get("fuel_format") != FUEL_FORMAT:
        raise ValueError("it is not a fuel record this program can read")
    units = doc.get("units")
    if not isinstance(units, dict):
        raise ValueError("it has no units")
    out = {}
    for mid, u in units.items():
        w = f"unit {mid!r}"
        if not isinstance(mid, str) or not mid or not isinstance(u, dict):
            raise ValueError(f"{w} is not an object")
        if u.get("kind") not in TRACKED_KINDS:
            raise ValueError(f"{w} has kind {u.get('kind')!r}")
        refills = u.get("refills", [])
        sb = u.get("show_burns", [])
        if not isinstance(refills, list) or not isinstance(sb, list):
            raise ValueError(f"{w}: refills and show_burns are lists")
        out[mid] = {
            "kind": u["kind"], "name": str(u.get("name") or mid),
            "burn": _burn(u.get("burn"), f"{w}.burn"),
            "shows": _count(u.get("shows", 0), f"{w}.shows"),
            "show_burns": [_num(x, f"{w}.show_burns") for x in sb],
            "refills": [_read_refill(r, f"{w}.refills[{i}]")
                        for i, r in enumerate(refills)],
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
              "burn": {str(k): _num(v, f"episode.burn[{k!r}]")
                       for k, v in ep["burn"].items()}}
    ids = [r["id"] for u in out.values() for r in u["refills"]]
    nid = _count(doc.get("next_id", 1), "next_id", 1)
    return {"units": out, "episode": ep,
            "next_id": max([nid] + [i + 1 for i in ids]),
            "shows_total": _count(doc.get("shows_total", 0), "shows_total"),
            "gaps": _count(doc.get("gaps", 0), "gaps"),
            "clean": doc.get("clean", True) is not False,
            "saved_at": doc.get("saved_at")
            if isinstance(doc.get("saved_at"), (int, float)) else None}


# ------------------------------------------------------------ the tracker --

class FuelTracker:
    """The fuel record and its thread. See the module docstring.

    folder         where fuel.json lives (the engine's settings folder)
    machines_get   () -> the Morning config's machines (morning.Machine)
    config_path    ltcplay_morning.json, read again when it changes
    armed_get      () -> set of flamesafe group names ARMED now, or None
                   when flamesafe's state is not known (no status, or a
                   stale one): flame is then COUNTED, never skipped
    locked_get     (machine id) -> True while it is locked out
    rehearsal_get  () -> True in a programming session (remote.programming)
    journal        (text, fault=False) -> anything; failures ignored
    """

    def __init__(self, folder, machines_get, config_path=None,
                 armed_get=lambda: None, locked_get=lambda mid: False,
                 rehearsal_get=lambda: False, journal=None,
                 clock=time.monotonic, wall=time.time, threaded=True):
        self.folder = folder
        self.path = os.path.join(folder, FUEL_FILE)
        self._machines_get = machines_get
        self.config_path = config_path
        self._armed_get = armed_get
        self._locked_get = locked_get
        self._rehearsal_get = rehearsal_get
        self._journal = journal
        self._clock = clock
        self._wall = wall
        self._lock = threading.RLock()       # the record
        self._save_lock = threading.Lock()   # the file: one writer at once
        # The frames the flame link sent, appended by the sender thread
        # (observe) and taken by this module's own thread (pump). A deque's
        # append and popleft need no lock; maxlen bounds the memory if this
        # thread ever stops taking them (and `dropped` counts what fell off).
        self._q = collections.deque(maxlen=QUEUE_MAX)
        self.observed = 0
        self.dropped = 0
        # True while the wave flamer cleaning (cleaning.py) holds the purge
        # frame: its frames are burns of the show fuel, not a purge. A
        # plain attribute: observe() reads it without a lock.
        self.purge_is_burn = False
        self._dropped_seen = 0
        self._link = None
        self.cfg, self.config_error = parse_config(None), ""
        self._cfg_stamp = None
        self.units = {}
        self.next_id = 1
        self.episode = None          # a performance under way
        self._ep_last_tc = None      # clock when its show timecode was seen
        self._ep_moved_at = None     # clock when its timecode last advanced
        self.shows_total = 0
        # Times some flame may not have been counted (frames dropped, a
        # crash before the last save, frames lost to a failure). A refill
        # remembers the number; a unit with more since its refill is "not
        # sure" on the page, never green.
        self.gaps = 0
        self.gap_notes = []
        self.armed_unknown = False   # flamesafe's armed state, last pass
        self.load_error = ""
        self.save_error = ""
        self.run_error = ""
        self.saved_at = None         # wall clock of the last good save
        self._clean = False          # written True only by close()
        self._dirty = False
        self._last_save = clock()
        self._last_t = None
        self._last_purges = None
        self._stop = threading.Event()
        self._thread = None
        self._reload_config()
        self._load()
        if threaded:
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="ltcplay-fuel")
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
        """Some flame may not have been counted: every unit is "not sure"
        until its next refill, and the page says why."""
        with self._lock:
            self.gaps += 1
            self.gap_notes = (self.gap_notes + [why])[-5:]
            self._dirty = True
        self._note(f"Fuel: {why}", fault=True)

    def _reload_config(self):
        """The fuel block, read again whenever the file's size or time
        changes (a config reload). Never raises."""
        p = self.config_path
        if not p:
            return
        try:
            st = os.stat(p)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if stamp == self._cfg_stamp and self._cfg_stamp is not None:
            return
        self._cfg_stamp = stamp
        cfg, err = load_config(p)
        with self._lock:
            self.cfg = cfg
            if err != self.config_error and err:
                self._note(f"Fuel: {err} The fuel page runs on the default "
                           f"settings until it is fixed.", fault=True)
            self.config_error = err

    def _machines(self):
        """The tracked machines, in config order. Never raises."""
        try:
            ms = list(self._machines_get() or [])
        except Exception:
            return []
        return [m for m in ms if getattr(m, "kind", None) in TRACKED_KINDS]

    def _locked(self, mid):
        try:
            return bool(self._locked_get(mid))
        except Exception:
            return False

    def _unit(self, m):
        u = self.units.get(m.id)
        if u is None:
            u = self.units[m.id] = _new_unit(m)
            self._dirty = True
        u["kind"], u["name"] = m.kind, m.name
        return u

    # -- the flame link's side -----------------------------------------------
    def attach(self, link):
        """Watch `link` (flamelink.FlameLink): every frame it sends is
        handed to observe() after it is out."""
        self._link = link
        link.observer = self.observe

    def observe(self, tc, values, ok):
        """Called on the flame link's sender thread after each frame went
        out. Reads a monotonic clock and three plain attributes and appends
        one tuple: no lock, no disk, no journal, no sleep (selftest)."""
        q = self._q
        if len(q) >= QUEUE_MAX:
            self.dropped += 1
        link = self._link
        q.append((self._clock(), tc,
                  bool(getattr(link, "rehearse_on", False)),
                  bool(getattr(link, "program_on", False)),
                  getattr(link, "purges_sent", 0), values, ok,
                  self.purge_is_burn))
        self.observed += 1

    # -- this module's thread -------------------------------------------------
    def _run(self):
        while not self._stop.wait(DRAIN_S):
            self.tick()

    def tick(self):
        """One pass: take the frames, end a performance whose timecode has
        gone, save when due. Never raises; a failure is journaled once and
        shown on the page."""
        try:
            self._reload_config()
            self.pump()
            if self._dirty and \
                    self._clock() - self._last_save >= SAVE_EVERY_S:
                self.save()
            if self.run_error:
                self._note("Fuel: counting is working again.")
                self.run_error = ""
        except Exception as e:                    # noqa: BLE001
            why = f"{type(e).__name__}: {e}"
            if not self.run_error:
                self._note(f"Fuel: the tracker hit an error and carried on "
                           f"({why}). The flames are not affected.",
                           fault=True)
            self.run_error = why

    def _armed_now(self):
        """The armed group names flamesafe reports right now, or None when
        that is not known. Never raises."""
        try:
            got = self._armed_get()
        except Exception:
            got = None
        return None if got is None else set(got)

    def pump(self):
        """Count every frame waiting. Then a performance whose show
        timecode has been gone for EPISODE_JOIN_S ends, frames or not."""
        frames = []
        q = self._q
        while True:
            try:
                frames.append(q.popleft())
            except IndexError:
                break
        if self.dropped != self._dropped_seen:
            n = self.dropped - self._dropped_seen
            self._dropped_seen = self.dropped
            self._gap(f"{n} flame frame(s) could not be counted (this "
                      f"tracker fell behind), so every tank may be emptier "
                      f"than it shows until its next refill.")
        machines = self._machines()
        armed = self._armed_now() if frames else None
        rehearsal = None
        with self._lock:
            if frames:
                self.armed_unknown = armed is None
            if self.episode is not None or any(
                    f[1] is not None and not f[2] for f in frames):
                try:
                    rehearsal = bool(self._rehearsal_get())
                except Exception:
                    rehearsal = None
            try:
                for f in frames:
                    self._frame(f, machines, armed, rehearsal)
            except Exception:
                self.gaps += 1
                self.gap_notes = (self.gap_notes + [
                    "Some flame frames were lost to an error in the "
                    "tracker, so every tank may be emptier than it shows "
                    "until its next refill."])[-5:]
                self._dirty = True
                raise
            ep = self.episode
            if ep is not None and self._ep_last_tc is not None and \
                    self._clock() - self._ep_last_tc > EPISODE_JOIN_S:
                self._end_episode(machines, "its show timecode stopped")

    def _frame(self, f, machines, armed, rehearsal):
        t, tc, rehearse, program, purges, values, ok, burn = f
        dt = 0.0 if self._last_t is None else \
            min(max(t - self._last_t, 0.0), MAX_FRAME_DT_S)
        self._last_t = t
        purge = self._last_purges is not None and purges != self._last_purges
        self._last_purges = purges
        secs = tc_seconds(tc) if tc is not None and not rehearse else None
        show_tc = secs is not None
        if show_tc:
            self._show_tc(t, secs, machines, rehearsal)
        if not ok or not values or dt <= 0 or not any(values):
            return
        if rehearse or program or (purge and burn):
            # The wave flamer cleaning's bursts (cleaning.py) go out on the
            # purge frame but burn the show fuel: "other", like scrubbing.
            source = "other"
        elif purge:
            source = "purge"
        elif show_tc:
            source = "show"
        else:
            source = "other"
        n = len(values)
        for m in machines:
            kc = self.cfg["kinds"][m.kind]
            if source == "purge" and not kc["count_purge"]:
                continue
            at = kc["fire_at"]
            if not any(0 < s <= n and values[s - 1] >= at for s in m.fire):
                continue
            if self._locked(m.id):
                continue
            if armed is not None and m.group not in armed:
                # flamesafe says this group is not armed: it holds the
                # group's fire slots at zero, so nothing burned.
                continue
            u = self._unit(m)
            if armed is None:
                # Not known: counted anyway. flamesafe zeroes a disarmed
                # group, so this can only count too much (empty sooner).
                u["unsure_s"] = u.get("unsure_s", 0.0) + dt
            if source == "show":
                b = self.episode["burn"]
                b[m.id] = b.get(m.id, 0.0) + dt
            else:
                u["burn"][source] = u["burn"].get(source, 0.0) + dt
            self._dirty = True

    def _show_tc(self, t, secs, machines, rehearsal):
        """A frame with the show's timecode. A performance starts, carries
        on, or (the timecode back from the start after a gap) a new one
        starts. Only the timecode advancing adds to its length."""
        ep = self.episode
        if ep is not None and ep.get("last_tc") is not None and \
                secs < ep["last_tc"] - 1.0:
            self._end_episode(machines, "a new performance started")
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

    def _end_episode(self, machines, how):
        """A performance is over: a show for every tracked unit not locked
        out if it ran min_show_s and was not a programming session; else
        its burn is "other"."""
        ep, self.episode = self.episode, None
        self._ep_last_tc = self._ep_moved_at = None
        if ep is None:
            return
        self._dirty = True
        is_show = (not ep.get("rehearsal")
                   and ep.get("tc_s", 0.0) >= self.cfg["min_show_s"])
        burn = ep.get("burn") or {}
        ids = [m.id for m in machines]
        for mid in list(burn) + [i for i in ids if i not in burn]:
            u = self.units.get(mid)
            if u is None:
                m = next((x for x in machines if x.id == mid), None)
                if m is None:
                    continue
                u = self._unit(m)
            b = float(burn.get(mid, 0.0))
            u["burn"]["show" if is_show else "other"] += b
            if is_show and mid in ids and not self._locked(mid):
                u["shows"] += 1
                u["show_burns"] = (u["show_burns"] + [round(b, 3)])[
                    -SHOW_BURNS_KEPT:]
        if is_show:
            self.shows_total += 1
            self._note(f"Fuel: a show was counted ({how}; "
                       f"{ep.get('tc_s', 0.0):.0f} s of show timecode).")

    # -- the file --------------------------------------------------------------
    def _doc(self):
        return {"fuel_format": FUEL_FORMAT, "next_id": self.next_id,
                "units": self.units, "episode": self.episode,
                "shows_total": self.shows_total, "gaps": self.gaps,
                "clean": self._clean, "saved_at": self._wall()}

    def save(self):
        """Write fuel.json atomically now. True when it is on disk. Never
        raises: a failure is kept for the page and journaled once. The text
        is made inside the file lock, so an older one is never written
        after a newer one."""
        with self._save_lock:
            with self._lock:
                text = json.dumps(self._doc(), indent=1, sort_keys=True)
                self._dirty = False
                self._last_save = self._clock()
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
                        self._note(f"Fuel: {self.path} could not be written "
                                   f"({why}). The record is kept in memory "
                                   f"and tried again every "
                                   f"{SAVE_EVERY_S:g} s. The flames are not "
                                   f"affected.", fault=True)
                    self.save_error = why
                return False
        with self._lock:
            if self.save_error:
                self._note("Fuel: fuel.json is being written again.")
            self.save_error = ""
            self.saved_at = self._wall()
        return True

    def _set_aside(self, why):
        """A fuel.json that cannot be used is never written over: it is
        kept beside it under another name and the page says so."""
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
        self.load_error = (f"fuel.json could not be read ({why}). {where}, "
                           f"and the fuel record started again: log a refill "
                           f"for each unit when its tank is next full.")
        self._note(f"Fuel: {self.load_error}", fault=True)

    def _load(self):
        """fuel.json, if there is one, every field checked (read_record).
        A file that is not readable, or holds a value this program would
        not count with, is set aside and the record starts again."""
        try:
            with open(self.path, encoding="utf-8-sig") as fh:
                rec = read_record(json.load(fh))
        except FileNotFoundError:
            return
        except Exception as e:                    # noqa: BLE001
            self._set_aside(f"{type(e).__name__}: {e}")
            return
        with self._lock:
            self.units = rec["units"]
            self.next_id = rec["next_id"]
            self.shows_total = rec["shows_total"]
            self.gaps = rec["gaps"]
            self.saved_at = rec["saved_at"]
            if not rec["clean"]:
                when = ("" if self.saved_at is None else time.strftime(
                    " (last saved %Y-%m-%d %H:%M:%S)",
                    time.localtime(self.saved_at)))
                self.gaps += 1
                self.gap_notes.append(
                    f"LTC Player stopped without closing{when}; up to "
                    f"{SAVE_EVERY_S:g} s of flame before that may not be "
                    f"counted, so every tank may be emptier than it shows "
                    f"until its next refill.")
                self._dirty = True
            if rec["episode"] is not None:
                # The engine stopped during a performance: it is over.
                self.episode = rec["episode"]
                self._end_episode(self._machines(),
                                  "the engine stopped during it")
                self._dirty = True

    # -- refills ---------------------------------------------------------------
    def _snapshot(self, mid, u):
        burn = dict(u["burn"])
        if self.episode is not None:
            burn["show"] += float(self.episode["burn"].get(mid, 0.0))
        return {"burn": {s: round(burn[s], 3) for s in SOURCES},
                "total": round(_total(burn), 3), "shows": u["shows"],
                "unsure_s": round(u.get("unsure_s", 0.0), 3),
                "gaps": self.gaps}

    def refill(self, which, who, role="", expect=None):
        """Log a refill: `which` is a machine id or "all". (ok, sentence).
        For "all", `expect` is the ids the page showed: refused if the
        machine list has changed since. Saved to disk before it answers;
        if the disk refuses, the refill is still kept (and saved as soon
        as it can be) and the sentence says so."""
        machines = self._machines()
        by_id = {m.id: m for m in machines}
        if which == "all":
            pick = machines
            if expect is not None and sorted(expect) != sorted(by_id):
                return False, ("The machine list changed since this page was "
                               "drawn. Look at the units again, then press "
                               "Refill all again. Nothing was logged.")
        elif which in by_id:
            pick = [by_id[which]]
        else:
            return False, (f"There is no tracked unit called {which!r}. "
                           f"Nothing was logged.")
        if not pick:
            return False, "No unit is tracked, so nothing was logged."
        with self._lock:
            bid = self.next_id
            self.next_id += 1
            at = self._wall()
            for m in pick:
                u = self._unit(m)
                snap = self._snapshot(m.id, u)
                u["refills"] = (u["refills"] + [dict(
                    snap, id=bid, at=at, who=who or "unsigned",
                    role=role or "", undone=None)])[-REFILLS_KEPT:]
            self._dirty = True
        names = ("every unit" if which == "all" and len(pick) > 1
                 else pick[0].name)
        self._note(f"Fuel: {who or 'someone'} logged a refill of {names}.")
        ok = self.save()
        if not ok:
            return True, (f"Refill of {names} logged, but fuel.json could "
                          f"NOT be written ({self.save_error}). It is kept "
                          f"in memory and tried again every "
                          f"{SAVE_EVERY_S:g} s; do not restart the engine "
                          f"until the page says it is saved.")
        return True, f"Refill of {names} logged."

    def undo(self, batch_id, who):
        """Mark the refill `batch_id` (one press: a unit, or every unit)
        undone. Never deleted: the record says who undid it and when.
        (ok, sentence)."""
        with self._lock:
            hit = []
            for u in self.units.values():
                for r in u["refills"]:
                    if r.get("id") == batch_id and not r.get("undone"):
                        r["undone"] = {"who": who or "unsigned",
                                       "at": self._wall()}
                        hit.append((u, r))
            if not hit:
                return False, ("That refill is not in the record or was "
                               "already undone. Nothing was changed.")
            self._dirty = True
        r0 = hit[0][1]
        names = (hit[0][0]["name"] if len(hit) == 1
                 else _plural(len(hit), "unit"))
        self._note(f"Fuel: {who or 'someone'} undid the refill of {names} "
                   f"that {r0.get('who')} logged.")
        ok = self.save()
        tail = "" if ok else (f" fuel.json could NOT be written "
                              f"({self.save_error}); it is tried again "
                              f"every {SAVE_EVERY_S:g} s.")
        return True, (f"The refill of {names} that {r0.get('who')} logged "
                      f"is undone.{tail}")

    # -- what the page shows ---------------------------------------------------
    def _live_refills(self, u):
        return sorted((r for r in u["refills"] if not r.get("undone")),
                      key=lambda r: r.get("id", 0))

    def _cycles(self, kind):
        """Every tank's worth of burn of `kind`, oldest first (by refill)."""
        out = []
        for u in self.units.values():
            if u.get("kind") != kind:
                continue
            rs = self._live_refills(u)
            for a, b in zip(rs, rs[1:]):
                out.append((b.get("id", 0),
                            float(b.get("total", 0.0)) -
                            float(a.get("total", 0.0))))
        return [c for _i, c in sorted(out)]

    def _unit_view(self, m, cfg, kind):
        u = self.units.get(m.id) or _new_unit(m)
        kc = cfg["kinds"][m.kind]
        cur = self._snapshot(m.id, u)
        rs = self._live_refills(u)
        last = rs[-1] if rs else None
        base = last or {"burn": {s: 0.0 for s in SOURCES}, "total": 0.0,
                        "shows": 0, "unsure_s": 0.0, "gaps": 0}
        since = {s: round(max(0.0, cur["burn"][s] - float(
            base["burn"].get(s, 0.0))), 1) for s in SOURCES}
        burned = max(0.0, cur["total"] - float(base.get("total", 0.0)))
        shows = max(0, cur["shows"] - int(base.get("shows", 0)))
        unsure = max(0.0, cur["unsure_s"] - float(base.get("unsure_s", 0.0)))
        gaps = (self.gaps - int(base.get("gaps", 0))) if last else 0
        recent = u["show_burns"][-cfg["recent_shows"]:]
        avg = (sum(recent) / len(recent)
               if len(recent) >= cfg["min_shows"] else None)
        v = {"id": m.id, "name": m.name, "kind": m.kind,
             "kind_words": KIND_WORDS[m.kind], "label": kc["label"],
             "group": m.group, "locked": self._locked(m.id),
             "since": since, "burn_s": round(burned, 1),
             "shows_since": shows, "show_samples": len(recent),
             "avg_show_s": None if avg is None else round(avg, 1),
             "unsure_s": round(unsure, 1), "not_sure": gaps > 0,
             "last_refill": None if last is None else {
                 "id": last.get("id"), "who": last.get("who"),
                 "at_ms": int(float(last.get("at") or 0) * 1000)},
             "percent": None, "color": None, "shows_left": None,
             "refill_soon": False, "status": "never", "words": ""}
        burn_line = (f"{burn_words(burned)} and "
                     f"{_plural(shows, 'show')}")

        def done(v):
            # Every path ends here: what it cannot vouch for is said.
            if v["not_sure"] and v["status"] != "not_counting":
                v["words"] = ("Not sure: some flame since the refill could "
                              "not be counted (see the note at the top), so "
                              "the tank may be emptier than this. " +
                              v["words"])
            if v["refill_soon"]:
                v["words"] = "Refill soon. " + v["words"]
            if unsure > 0:
                v["words"] += (f" Includes {burn_words(unsure)} counted "
                               f"while flamesafe's armed state was not "
                               f"known, so it may read low.")
            return v
        if self._link is None:
            v["status"] = "not_counting"
            v["not_sure"] = True
            v["words"] = ("Not counting: this engine has no flame link, so "
                          "no flame is being measured. The level is not "
                          "known.")
            return v
        if last is None:
            v["words"] = (f"No refill logged yet: {burn_line} since "
                          f"tracking began. Press Refilled the next time "
                          f"its tank is full.")
            return done(v)
        est = kc["start_shows_per_tank"]
        tank, firm = kind["tank_s"], kind["firm"]
        cap, how = tank, ("learned" if firm else "learning")
        if cap is None and est is not None and avg:
            cap, how = est * avg, "estimate"
        rough = False
        if cap is None and est is not None:
            # Only a count of shows to go on, and purges and other flame
            # are not in it: rough, and it may read high.
            left = max(0, est - shows)
            rough = True
            v.update(status="estimate", shows_left=left,
                     percent=int(math.floor(100.0 * left / est)))
            v["words"] = (f"Rough estimate, may be high: about "
                          f"{_plural(left, 'show')} left from your starting "
                          f"number ({est} shows a tank), not counting purges "
                          f"or other flame. {burn_line} since the refill.")
        elif cap is None:
            v["status"] = "learning"
            v["words"] = (f"Learning: {burn_line} since the refill. It "
                          f"knows how long a {kc['label']} tank lasts once "
                          f"one {KIND_WORDS[m.kind]} has been refilled "
                          f"twice.")
            return done(v)
        else:
            left_s = max(0.0, cap - burned)
            frac = left_s / cap if cap > 0 else 0.0
            v.update(status={"learned": "ok"}.get(how, how),
                     percent=int(math.floor(100.0 * frac)))
            if avg is not None and avg > 0:
                v["shows_left"] = int(math.floor(left_s / avg))
            pre = {"learned": "",
                   "learning": (f"Still learning the tank size (from "
                                f"{_plural(kind['cycles'], 'tank')}), so "
                                f"this reads on the low side. "),
                   "estimate": (f"Estimate from your starting number "
                                f"({est} shows a tank). ")}[how]
            if v["shows_left"] is not None:
                v["words"] = (f"{pre}About {_plural(v['shows_left'], 'show')}"
                              f" left ({v['percent']} percent).")
            elif avg is not None:
                v["words"] = (f"{pre}{v['percent']} percent left. It has not "
                              f"burned in a show yet, so shows left is not "
                              f"known.")
            else:
                need = cfg["min_shows"] - len(recent)
                v["words"] = (f"{pre}{v['percent']} percent left. Shows left "
                              f"appears after {_plural(need, 'more show')}.")
        p = v["percent"]
        v["color"] = ("red" if p < cfg["red_below_percent"] else
                      "amber" if p < cfg["amber_below_percent"] else "green")
        if v["color"] == "green" and (rough or v["not_sure"]):
            # Never green on a level it cannot vouch for.
            v["color"] = "amber"
        sl = v["shows_left"]
        v["refill_soon"] = bool(v["color"] == "red" or
                                (sl is not None and
                                 sl <= cfg["refill_soon_shows"]))
        return done(v)

    def view(self):
        """The Fuel page's view. Never raises."""
        try:
            machines = self._machines()
            with self._lock:
                cfg = self.cfg
                kinds = {}
                for k in TRACKED_KINDS:
                    cyc = self._cycles(k)
                    tank, used, firm = learned_tank(
                        cyc, cfg["learn_refills"], cfg["outlier_ratio"])
                    sus = suspect_cycles(cyc, cfg["learn_refills"])
                    kc = cfg["kinds"][k]
                    kinds[k] = {"label": kc["label"],
                                "words": KIND_WORDS[k],
                                "tank_s": None if tank is None
                                else round(tank, 1),
                                "cycles": used, "firm": firm,
                                "suspect": [round(c, 1) for c in sus],
                                "count_purge": kc["count_purge"],
                                "fire_at": kc["fire_at"],
                                "start_shows_per_tank":
                                    kc["start_shows_per_tank"]}
                units = [self._unit_view(m, cfg, kinds[m.kind])
                         for m in machines]
                gone = sorted(set(self.units) - {m.id for m in machines})
                batches = {}
                for u in self.units.values():
                    for r in u["refills"]:
                        b = batches.setdefault(r.get("id"), {
                            "id": r.get("id"), "who": r.get("who"),
                            "at_ms": int(float(r.get("at") or 0) * 1000),
                            "units": [], "undone": r.get("undone")})
                        b["units"].append(u["name"])
                recent = sorted(batches.values(),
                                key=lambda b: -(b["id"] or 0))[
                    :RECENT_BATCHES]
                notes = list(self.gap_notes)
                if self._link is None:
                    notes.insert(0, "No flame link in this engine, so no "
                                    "flame is being counted.")
                if self.armed_unknown:
                    notes.append("flamesafe's armed state is not known right "
                                 "now, so every flame frame is counted as "
                                 "if its group were armed. Tanks may read "
                                 "lower than they are, never higher.")
                for k, d in kinds.items():
                    if d["suspect"]:
                        notes.append(
                            f"A {d['words']} tank lasted much longer than "
                            f"the others ({burn_words(max(d['suspect']))}): "
                            f"a Refilled press may have been missed. Check "
                            f"Recent refills.")
                ep = self.episode
                return {
                    "units": units, "kinds": kinds,
                    "unit_ids": [m.id for m in machines],
                    "refill_soon": [u["name"] for u in units
                                    if u["refill_soon"]],
                    "not_sure": [u["name"] for u in units
                                 if u["not_sure"]],
                    "notes": notes,
                    "not_listed": len(gone),
                    "recent_refills": recent,
                    "show_running": ep is not None,
                    "shows_total": self.shows_total,
                    "watching": self._link is not None,
                    "config_error": self.config_error,
                    "fault": " ".join(x for x in (
                        self.load_error,
                        f"fuel.json could not be written: {self.save_error}."
                        if self.save_error else "",
                        f"The tracker hit an error: {self.run_error}."
                        if self.run_error else "") if x),
                    "saved_at_ms": None if self.saved_at is None
                    else int(float(self.saved_at) * 1000),
                    "settings": {k: cfg[k] for k in (
                        "learn_refills", "recent_shows", "min_shows",
                        "min_show_s", "amber_below_percent",
                        "red_below_percent", "refill_soon_shows")}}
        except Exception as e:                    # noqa: BLE001
            return {"units": [], "kinds": {}, "refill_soon": [],
                    "not_sure": [], "notes": [],
                    "fault": f"The fuel view could not be built "
                             f"({type(e).__name__}: {e})."}

    def summary(self):
        """For the status every screen polls: the nav button's colour."""
        v = self.view()
        return {"refill_soon": len(v.get("refill_soon") or []),
                "fault": bool(v.get("fault") or v.get("config_error")
                              or v.get("not_sure"))}

    def close(self, timeout=CLOSE_TIMEOUT_S):
        """Stop the thread, then on a thread of its own: count what is
        waiting, end a performance (the engine is stopping), save, marked
        a clean stop. Waits at most `timeout` for that (web.serve calls
        this AFTER the flame link and the conductor have stopped), and
        never raises."""
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)
        self._thread = None
        link = self._link
        if link is not None and getattr(link, "observer", None) == \
                self.observe:
            link.observer = None

        def finish():
            try:
                self.pump()
                with self._lock:
                    if self.episode is not None:
                        self._end_episode(self._machines(),
                                          "the engine was stopped during it")
                    self._clean = True
            except Exception:                     # noqa: BLE001
                pass
            self.save()
        th = threading.Thread(target=finish, daemon=True,
                              name="ltcplay-fuel-close")
        th.start()
        th.join(timeout)
        if th.is_alive():
            self._note(f"Fuel: saving fuel.json took longer than "
                       f"{timeout:g} s at close; the engine did not wait "
                       f"for it.", fault=True)
            return False
        return True


# ------------------------------------------------------- who may do what --

REFILL_ROLES = ("operator", "admin")


def may_refill(ctx, users):
    """(True, "") when this request may log a refill: a signed-in operator
    or admin. Without user sign-in in this engine (no users, or none made
    yet), only on the show machine itself. Else (False, why)."""
    return _may(ctx, users, REFILL_ROLES, "log a refill")


def may_undo(ctx, users):
    """Undoing a refill: an admin, signed in."""
    return _may(ctx, users, ("admin",), "undo a refill")


def _may(ctx, users, roles, what):
    active = False
    try:
        active = users is not None and bool(users.active())
    except Exception:                             # noqa: BLE001
        active = False
    if not active:
        if getattr(ctx, "local", False):
            return True, ""
        return False, (f"Only on the show machine itself: nobody can sign "
                       f"in yet, so who may {what} cannot be checked from "
                       f"here.")
    s = getattr(ctx, "session", None) or {}
    name, role = s.get("name"), s.get("role")
    if not name:
        need = "an admin" if roles == ("admin",) else "an operator or admin"
        return False, (f"Sign in as {need} to {what} (the user chip at the "
                       f"top). Nothing was changed.")
    if role not in roles:
        need = "an admin" if roles == ("admin",) else "an operator or admin"
        return False, (f"{name} is signed in as {role}; only {need} may "
                       f"{what}. Nothing was changed.")
    return True, ""


GET_ROUTES = ("fuel",)
POST_ROUTES = ("fuel-refill", "fuel-undo")


def route_get(tracker, ctx, users, wall=time.time):
    """GET /api/remote/fuel: anyone on the show network may read it."""
    if tracker is None:
        return 404, {"error": "The fuel page is not set up in this engine "
                              "(it needs the Fire & Ice engine and its "
                              "machine list)."}
    v = tracker.view()
    ok, why = may_refill(ctx, users)
    v["may_refill"], v["refill_why"] = ok, why
    ok, why = may_undo(ctx, users)
    v["may_undo"], v["undo_why"] = ok, why
    v["served_at"] = int(wall() * 1000)
    return 200, v


def route_post(tracker, name, body, ctx, users):
    """POST fuel-refill {"unit": id} or {"all": true, "confirm": true};
    fuel-undo {"id": n}. (code, body)."""
    if tracker is None:
        return 404, {"error": "The fuel page is not set up in this engine."}
    body = body if isinstance(body, dict) else {}
    s = getattr(ctx, "session", None) or {}
    who = s.get("name") or getattr(ctx, "device", "") or "unsigned"
    role = s.get("role") or ""
    if name == "fuel-refill":
        ok, why = may_refill(ctx, users)
        if not ok:
            return 403, {"error": why}
        if body.get("all") is True:
            if body.get("confirm") is not True:
                return 400, {"error": "Refill all needs its confirm. "
                                      "Nothing was logged."}
            shown = body.get("units")
            if not isinstance(shown, list) or \
                    not all(isinstance(x, str) for x in shown):
                return 400, {"error": "Refill all has to say which units "
                                      "the page showed. Nothing was "
                                      "logged."}
            done, text = tracker.refill("all", who, role, expect=shown)
            if not done:
                return 409, {"error": text}
        else:
            unit = body.get("unit")
            if not isinstance(unit, str) or not unit:
                return 400, {"error": "Say which unit was refilled. "
                                      "Nothing was logged."}
            done, text = tracker.refill(unit, who, role)
        return (200 if done else 400), ({"text": text} if done
                                        else {"error": text})
    if name == "fuel-undo":
        ok, why = may_undo(ctx, users)
        if not ok:
            return 403, {"error": why}
        bid = body.get("id")
        if not _is_int(bid):
            return 400, {"error": "Say which refill to undo. Nothing was "
                                  "changed."}
        done, text = tracker.undo(bid, who)
        return (200 if done else 409), ({"text": text} if done
                                        else {"error": text})
    return 404, {"error": "no such thing here"}


def build(remote, morning, link, journal=None, threaded=True):
    """The engine's tracker (web.serve): the Morning page's machines,
    lockout and config file, flamesafe's armed groups from the remote's
    flame status, programming sessions from remote.programming, and the
    flame link watched when there is one."""
    def armed():
        fs = getattr(remote, "flame_status", None)
        fl = fs.view() if fs is not None else None
        if not fl or fl.get("stale") or not fl.get("connected"):
            return None
        return {g.get("name") for g in fl.get("groups", [])
                if g.get("armed") == "armed"}

    def rehearsal():
        try:
            return bool(remote.programming()[0])
        except Exception:                         # noqa: BLE001
            return False

    tr = FuelTracker(
        remote.folder, lambda: morning.cfg.machines,
        config_path=morning.config_path, armed_get=armed,
        locked_get=lambda mid: morning.lockout.is_locked("machine", mid),
        rehearsal_get=rehearsal, journal=journal, threaded=threaded)
    if link is not None:
        tr.attach(link)
    return tr
