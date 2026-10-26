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

How fuel use is measured
========================

At the flame link, per unit, as SECONDS OF FLAME: the time a frame that
actually went out to flamesafe carried a value at or above that kind's
fire value (fire_at: 229 for a G-Flame, flamesafe/rules.py
GFLAME_FIRE_AT; 254 for a Showven, rules.SHOWVEN_CF2_FIRE_AT) on any of
the unit's fire slots, while flamesafe's last status said the unit's group
was ARMED and the unit was not locked out. flamesafe holds a disarmed
group's fire slots at zero, so a frame for a disarmed group burns nothing.

Each frame is put in one of three piles:

  show    a performance: the show audio clock playing a cue (the flame
          link's own show timecode), not a programming session, and not
          scrubbing. Counted as a show once its timecode has run for
          min_show_s, whether it ran to its end or was aborted, started by
          the schedule or by hand. Shorter (a start that failed) or a
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
"cycle"). A kind's tank is the median of its last learn_refills cycles;
with three or more, a cycle under 1/outlier_ratio or over outlier_ratio
times the median is left out first (a refill logged by mistake, a tank
swapped half full). Until a kind has a cycle the page says "Learning", with
the burn and shows since the refill; Jeff may give a starting estimate in
shows per tank (start_shows_per_tank).

Shows left = what is left of the tank / that unit's average burn per show
(its last recent_shows shows; at least min_shows of them, else the page
says how many more it needs). The health bar is the fraction left; green,
amber and red by amber_below_percent and red_below_percent, and "Refill
soon" at refill_soon_shows shows left or fewer.

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
EPISODE_END_S = 2.0         # no show timecode this long ends a performance
ARMED_KEEP_S = 5.0          # a stale armed list is used this long
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
    """(seconds of flame per tank, how many cycles it used) from `cycles`
    (oldest first): the median of the last `n`, with three or more first
    leaving out any under median/ratio or over median*ratio. (None, 0)
    with no cycle."""
    last = [c for c in cycles if c > 0][-n:]
    if not last:
        return None, 0
    if len(last) >= 3:
        m = median(last)
        kept = [c for c in last if m / ratio <= c <= m * ratio]
        if kept:
            last = kept
    return median(last), len(last)


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
            "show_burns": [], "refills": []}


def _total(burn):
    return sum(float(burn.get(s, 0.0) or 0.0) for s in SOURCES)


# ------------------------------------------------------------ the tracker --

class FuelTracker:
    """The fuel record and its thread. See the module docstring.

    folder         where fuel.json lives (the engine's settings folder)
    machines_get   () -> the Morning config's machines (morning.Machine)
    config_path    ltcplay_morning.json, read again when it changes
    armed_get      () -> set of flamesafe group names ARMED now, or None
                   when flamesafe's state is not known
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
        self._save_lock = threading.Lock()   # the file
        # The frames the flame link sent, appended by the sender thread
        # (observe) and taken by this module's own thread (pump). A deque's
        # append and popleft need no lock; maxlen bounds the memory if this
        # thread ever stops taking them.
        self._q = collections.deque(maxlen=QUEUE_MAX)
        self.observed = 0
        self._link = None
        self.cfg, self.config_error = parse_config(None), ""
        self._cfg_stamp = None
        self.units = {}
        self.next_id = 1
        self.episode = None          # a performance under way
        self.uncounted_s = 0.0       # flame not counted: armed state unknown
        self.shows_total = 0
        self.load_error = ""
        self.save_error = ""
        self.run_error = ""
        self.saved_at = None         # wall clock of the last good save
        self._dirty = False
        self._last_save = clock()
        self._last_t = None
        self._last_purges = None
        self._ep_last_tc = None
        self._armed = (None, None)   # (set, clock it was read)
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
        out. One deque append and nothing else: no lock, no disk."""
        link = self._link
        self._q.append((self._clock(), tc is not None,
                        bool(getattr(link, "rehearse_on", False)),
                        bool(getattr(link, "program_on", False)),
                        getattr(link, "purges_sent", 0), values, ok))
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
        """The armed group names, or None when flamesafe's word is not known
        (a stale answer is used for ARMED_KEEP_S)."""
        now = self._clock()
        try:
            got = self._armed_get()
        except Exception:
            got = None
        if got is not None:
            self._armed = (set(got), now)
            return self._armed[0]
        s, at = self._armed
        if s is not None and now - at <= ARMED_KEEP_S:
            return s
        return None

    def pump(self):
        """Count every frame waiting. Then a performance whose show
        timecode has been gone for EPISODE_END_S ends, frames or not."""
        frames = []
        q = self._q
        while True:
            try:
                frames.append(q.popleft())
            except IndexError:
                break
        machines = self._machines()
        armed = self._armed_now() if frames else None
        rehearsal = None
        with self._lock:
            if self.episode is not None or any(f[1] and not f[2]
                                               for f in frames):
                try:
                    rehearsal = bool(self._rehearsal_get())
                except Exception:
                    rehearsal = None
            for f in frames:
                self._frame(f, machines, armed, rehearsal)
            ep = self.episode
            if ep is not None and self._ep_last_tc is not None and \
                    self._clock() - self._ep_last_tc > EPISODE_END_S:
                self._end_episode(machines, "its show timecode stopped")

    def _frame(self, f, machines, armed, rehearsal):
        t, has_tc, rehearse, program, purges, values, ok = f
        dt = 0.0 if self._last_t is None else \
            min(max(t - self._last_t, 0.0), MAX_FRAME_DT_S)
        self._last_t = t
        purge = self._last_purges is not None and purges != self._last_purges
        self._last_purges = purges
        show_tc = has_tc and not rehearse
        if show_tc:
            if self.episode is None:
                self.episode = {"started": self._wall(), "tc_s": 0.0,
                                "rehearsal": False, "burn": {}}
                self._dirty = True
            else:
                self.episode["tc_s"] += dt
            self._ep_last_tc = t
            if rehearsal:
                self.episode["rehearsal"] = True
        elif self.episode is not None and self._ep_last_tc is not None and \
                t - self._ep_last_tc > EPISODE_END_S:
            self._end_episode(machines, "its show timecode stopped")
        if not ok or not values or dt <= 0 or not any(values):
            return
        if rehearse or program:
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
            if armed is None:
                self.uncounted_s += dt
                continue
            if m.group not in armed:
                continue
            u = self._unit(m)
            if source == "show":
                b = self.episode["burn"]
                b[m.id] = b.get(m.id, 0.0) + dt
            else:
                u["burn"][source] = u["burn"].get(source, 0.0) + dt
            self._dirty = True

    def _end_episode(self, machines, how):
        """A performance is over: a show for every tracked unit not locked
        out if it ran min_show_s and was not a programming session; else
        its burn is "other"."""
        ep, self.episode = self.episode, None
        self._ep_last_tc = None
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
                "uncounted_s": round(self.uncounted_s, 3),
                "shows_total": self.shows_total, "saved_at": self._wall()}

    def save(self):
        """Write fuel.json atomically now. True when it is on disk. Never
        raises: a failure is kept for the page and journaled once."""
        with self._lock:
            text = json.dumps(self._doc(), indent=1, sort_keys=True)
            self._dirty = False
            self._last_save = self._clock()
        try:
            with self._save_lock:
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
                               f"({why}). The record is kept in memory and "
                               f"tried again every {SAVE_EVERY_S:g} s. The "
                               f"flames are not affected.", fault=True)
                self.save_error = why
            return False
        with self._lock:
            if self.save_error:
                self._note("Fuel: fuel.json is being written again.")
            self.save_error = ""
            self.saved_at = self._wall()
        return True

    def _load(self):
        """fuel.json, if there is one. A file that cannot be read is never
        written over: it is kept beside it under another name, the page
        says so, and the record starts again."""
        try:
            with open(self.path, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
            if not isinstance(doc, dict) or \
                    doc.get("fuel_format") != FUEL_FORMAT or \
                    not isinstance(doc.get("units"), dict):
                raise ValueError("it is not a fuel record this program "
                                 "can read")
            units = {}
            for mid, u in doc["units"].items():
                if not isinstance(u, dict):
                    raise ValueError(f"unit {mid!r} is not an object")
                burn = u.get("burn") or {}
                units[str(mid)] = {
                    "kind": str(u.get("kind") or ""),
                    "name": str(u.get("name") or mid),
                    "burn": {s: float(burn.get(s, 0.0) or 0.0)
                             for s in SOURCES},
                    "shows": int(u.get("shows") or 0),
                    "show_burns": [float(x) for x in
                                   u.get("show_burns") or []],
                    "refills": [dict(r) for r in u.get("refills") or []
                                if isinstance(r, dict)]}
            nid = int(doc.get("next_id") or 1)
            ep = doc.get("episode")
        except FileNotFoundError:
            return
        except Exception as e:                    # noqa: BLE001
            stamp = time.strftime("%Y%m%d-%H%M%S",
                                  time.localtime(self._wall()))
            kept = f"{self.path}.unreadable-{stamp}"
            n = 2
            while os.path.exists(kept):
                kept = f"{self.path}.unreadable-{stamp}-{n}"
                n += 1
            try:
                os.replace(self.path, kept)
                where = f"It was kept as {os.path.basename(kept)}"
            except OSError as e2:
                kept = None
                where = f"It could not be moved aside either ({e2})"
            self.load_error = (f"fuel.json could not be read "
                               f"({type(e).__name__}: {e}). {where}, and the "
                               f"fuel record started again: log a refill for "
                               f"each unit when its tank is next full.")
            self._note(f"Fuel: {self.load_error}", fault=True)
            return
        with self._lock:
            self.units = units
            self.next_id = max(nid, 1 + max(
                [int(r.get("id") or 0) for u in units.values()
                 for r in u["refills"]] + [0]))
            self.uncounted_s = float(doc.get("uncounted_s") or 0.0)
            self.shows_total = int(doc.get("shows_total") or 0)
            self.saved_at = doc.get("saved_at")
            if isinstance(ep, dict):
                # The engine stopped during a performance: it is over.
                self.episode = {"started": ep.get("started"),
                                "tc_s": float(ep.get("tc_s") or 0.0),
                                "rehearsal": bool(ep.get("rehearsal")),
                                "burn": {str(k): float(v) for k, v in
                                         (ep.get("burn") or {}).items()}}
                self._end_episode(self._machines(),
                                  "the engine stopped during it")
                self._dirty = True

    # -- refills ---------------------------------------------------------------
    def _snapshot(self, mid, u):
        burn = dict(u["burn"])
        if self.episode is not None:
            burn["show"] += float(self.episode["burn"].get(mid, 0.0))
        return {"burn": {s: round(burn[s], 3) for s in SOURCES},
                "total": round(_total(burn), 3), "shows": u["shows"]}

    def refill(self, which, who, role=""):
        """Log a refill: `which` is a machine id or "all". (ok, sentence).
        Saved to disk before it answers; if the disk refuses, the refill
        is still kept (and saved as soon as it can be) and the sentence
        says so."""
        machines = self._machines()
        by_id = {m.id: m for m in machines}
        if which == "all":
            pick = machines
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

    def _unit_view(self, m, cfg, tank):
        u = self.units.get(m.id) or _new_unit(m)
        kc = cfg["kinds"][m.kind]
        cur = self._snapshot(m.id, u)
        rs = self._live_refills(u)
        last = rs[-1] if rs else None
        base = last or {"burn": {s: 0.0 for s in SOURCES}, "total": 0.0,
                        "shows": 0}
        since = {s: round(max(0.0, cur["burn"][s] - float(
            base["burn"].get(s, 0.0))), 1) for s in SOURCES}
        burned = max(0.0, cur["total"] - float(base.get("total", 0.0)))
        shows = max(0, cur["shows"] - int(base.get("shows", 0)))
        recent = u["show_burns"][-cfg["recent_shows"]:]
        avg = (sum(recent) / len(recent)
               if len(recent) >= cfg["min_shows"] else None)
        v = {"id": m.id, "name": m.name, "kind": m.kind,
             "kind_words": KIND_WORDS[m.kind], "label": kc["label"],
             "group": m.group, "locked": self._locked(m.id),
             "since": since, "burn_s": round(burned, 1),
             "shows_since": shows, "show_samples": len(recent),
             "avg_show_s": None if avg is None else round(avg, 1),
             "last_refill": None if last is None else {
                 "id": last.get("id"), "who": last.get("who"),
                 "at_ms": int(float(last.get("at") or 0) * 1000)},
             "percent": None, "color": None, "shows_left": None,
             "refill_soon": False, "status": "never", "words": ""}
        burn_line = (f"{burn_words(burned)} and "
                     f"{_plural(shows, 'show')}")
        if last is None:
            v["words"] = (f"No refill logged yet: {burn_line} since "
                          f"tracking began. Press Refilled the next time "
                          f"its tank is full.")
            return v
        est = kc["start_shows_per_tank"]
        cap = tank
        how = "learned"
        if cap is None and est is not None and avg:
            cap, how = est * avg, "estimate"
        if cap is None and est is not None:
            # Only a count of shows to go on: the starting estimate.
            left = max(0, est - shows)
            v.update(status="estimate", shows_left=left,
                     percent=int(round(100.0 * left / est)))
            v["words"] = (f"Estimate from your starting number "
                          f"({est} shows a tank): about "
                          f"{_plural(left, 'show')} left. {burn_line} since "
                          f"the refill. Learning the real number.")
        elif cap is None:
            v["status"] = "learning"
            v["words"] = (f"Learning: {burn_line} since the refill. It "
                          f"knows how long a {kc['label']} tank lasts once "
                          f"one {KIND_WORDS[m.kind]} has been refilled "
                          f"twice.")
            return v
        else:
            left_s = max(0.0, cap - burned)
            frac = left_s / cap if cap > 0 else 0.0
            v.update(status="ok" if how == "learned" else "estimate",
                     percent=int(round(100.0 * frac)))
            if avg is not None and avg > 0:
                v["shows_left"] = int(math.floor(left_s / avg))
            pre = ("" if how == "learned" else
                   f"Estimate from your starting number ({est} shows a "
                   f"tank). ")
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
        sl = v["shows_left"]
        v["refill_soon"] = bool(v["color"] == "red" or
                                (sl is not None and
                                 sl <= cfg["refill_soon_shows"]))
        if v["refill_soon"]:
            v["words"] = "Refill soon. " + v["words"]
        return v

    def view(self):
        """The Fuel page's view. Never raises."""
        try:
            machines = self._machines()
            with self._lock:
                cfg = self.cfg
                kinds = {}
                for k in TRACKED_KINDS:
                    tank, used = learned_tank(self._cycles(k),
                                              cfg["learn_refills"],
                                              cfg["outlier_ratio"])
                    kc = cfg["kinds"][k]
                    kinds[k] = {"label": kc["label"],
                                "words": KIND_WORDS[k],
                                "tank_s": None if tank is None
                                else round(tank, 1),
                                "cycles": used,
                                "count_purge": kc["count_purge"],
                                "fire_at": kc["fire_at"],
                                "start_shows_per_tank":
                                    kc["start_shows_per_tank"]}
                units = [self._unit_view(m, cfg, kinds[m.kind]["tank_s"])
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
                ep = self.episode
                return {
                    "units": units, "kinds": kinds,
                    "refill_soon": [u["name"] for u in units
                                    if u["refill_soon"]],
                    "not_listed": len(gone),
                    "recent_refills": recent,
                    "show_running": ep is not None,
                    "shows_total": self.shows_total,
                    "uncounted_s": round(self.uncounted_s, 1),
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
                    "fault": f"The fuel view could not be built "
                             f"({type(e).__name__}: {e})."}

    def summary(self):
        """For the status every screen polls: the nav button's colour."""
        v = self.view()
        return {"refill_soon": len(v.get("refill_soon") or []),
                "fault": bool(v.get("fault") or v.get("config_error"))}

    def close(self):
        """Stop the thread, count what is waiting, end a performance (the
        engine is stopping), save."""
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(2.0)
        self._thread = None
        link = self._link
        if link is not None and getattr(link, "observer", None) == \
                self.observe:
            link.observer = None
        try:
            self.pump()
            with self._lock:
                if self.episode is not None:
                    self._end_episode(self._machines(),
                                      "the engine was stopped during it")
        except Exception:                         # noqa: BLE001
            pass
        self.save()


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
            done, text = tracker.refill("all", who, role)
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
