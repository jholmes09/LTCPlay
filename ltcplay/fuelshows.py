"""Shows per refill (Jeff, 2026-11-13): on the Fuel page, every flame unit
of a type with a "Shows per refill" number shows "Shows left: X" (below 1:
"Refuel now"), worked out from ALL the flame it actually fired, not from a
count of shows. FUEL.md says it in plain words.

What is measured (every flame second, in one labelled pile)
===========================================================

fuel.py watches each frame the flame link actually sent and, per unit,
counts the seconds a fire slot was at or above that kind's fire value
(fire_at: 229 G-Flame, 254 Showven ignition, duration or preset), unless
flamesafe says the unit's group is not armed or the unit is locked out.
Every such second goes in exactly ONE pile here (add(), from fuel.py's
_frame, or show_ended() at the end of a performance):

  show       a performance of 30 s of show timecode or more: the schedule,
             Start now, or Start at a cue (a part of the show)
  short      a performance shorter than that (a failed start, a short
             Start at a cue)
  purge      the Morning page's purge and timed purge (also a Showven's,
             which fuel.json leaves out: it has its own purge fuel)
  cleaning   the wave flamer cleaning's bursts
  rehearsal  Programming and rehearsal playback, scrubbing, Fireball
             practice's steps
  live       live flame programming
  other      anything else flamesafe let through (tests, a frame with no
             timecode)
  unsure     time that may have burned but could not be seen (below)

Seconds are full output seconds: a flame unit's fire value is a threshold,
not a size (229 and 255 make the same flame), so one second at or above it
is one second of full output; there is no level to weigh, unlike the hazers.
fuel.json's own piles (show, purge, other) are not changed.

Not sure, on the safe side
==========================

- LTC Player stopped without closing: each unit that was burning in the
  last SAVE_EVERY_S before the last save gets SAVE_EVERY_S seconds of
  "unsure" flame (full output), and reads Not sure.
- fuel.json counted flame this file never saw (this file was not written,
  or an older LTC Player ran): the difference is "unsure" flame.
- A show cut by a restart counts, and the unit reads Not sure.
- flamesafe's armed state not known: fuel.py counts the flame anyway (it
  is in its pile here). Flame fuel.py could not count (its gaps): Not
  sure.

Shows left
==========

  N   "Shows per refill", one whole number PER MACHINE TYPE (Wave Flamer,
      G-Flame), 1 to N_MAX, set by an admin. A type with no N is tracking
      only: every pile is still counted, and its tiles stay the fuel
      tracker's own.
  X   shows left = (X at the last refill or Refueled +1) - (every flame
      second since then, all piles) / (that unit's flame per show)

Flame per show: the unit's average over its last RECENT_SHOWS full shows
it fired in (a show is full when its show timecode ran at least FULL_SHARE
of the longest of them, so a Start at a cue part way in is never taken for
a whole show). With fewer than MIN_SHOWS of its own: the type's average
(every unit of the type), then its own, and the page says "estimating".
With no show at all yet, X goes down one per show it fired in, as a count
(estimating); the flame before that is converted as soon as a show is
known. A purge, live flame programming or a short Start at a cue therefore
takes shows left down in proportion; nothing that fired is left out. X is
shown rounded DOWN to a whole show (below 1: Refuel now).

Refueled +1, and its one log entry
==================================

Each Refueled +1 press adds ONE show to X at once (saved at once, crash
safe), up to N; a press at N goes round to 0 and on (0, 1, 2), so an extra
press is fixed by pressing on round the loop. The LOG entry (journal,
audit, the record's refill event) is not written per press: the presses on
one unit by one person are one batch, written once its X has sat unchanged
for COMMIT_S seconds, in words for what happened. A wrap is called a
correction, not usage. A batch still open when LTC Player stops is written
at the next start, marked not confirmed. A show ending, a refill, or
another person pressing that unit writes the open batch first. One batch is
one refill event, never one per press. Undo (an admin, Recent refills) puts
back the X before the batch while no press or refill has changed that unit
since; flame burned since stays counted. A +1 never logs a fuel.py refill
(the tank learning is not touched).

A fuel.py refill (Refilled, Fill selected, 1.0's Refill all) puts X at N
and starts "since the refill" again, under the refill's own number, so its
Undo takes both back.

Exactly once
============

A performance is known by its start stamp (fuel.json's episode "started");
one already counted is never counted again (a crash after this file was
written but before fuel.json was replays it at the next start). Each frame
is counted in one pile only. This file is written BEFORE fuel.json.

Kept in fuel_shows.json beside fuel.json, its own format number, written
atomically. fuel.json is not changed, so an older LTC Player reads and
writes fuel.json as before and never sees this file.

Every change is made under the FuelTracker's lock (it calls in); this
module has no lock and no thread of its own.
"""
import json
import math
import os
import time

SHOWS_FILE = "fuel_shows.json"
SHOWS_FORMAT = 1
KINDS = ("gflame", "waver")          # fuel.TRACKED_KINDS
KIND_NAMES = {"gflame": "G-Flame", "waver": "Wave Flamer"}
BUCKETS = ("show", "short", "purge", "cleaning", "rehearsal", "live",
           "other", "unsure")
BUCKET_WORDS = {"show": "Shows", "short": "Short starts",
                "purge": "Purges", "cleaning": "Cleaning",
                "rehearsal": "Rehearsal", "live": "Live programming",
                "other": "Other", "unsure": "Not sure"}
N_MIN = 1
N_MAX = 99
COMMIT_S = 5.0         # a +1 batch is written once X sat this long
RECENT_SHOWS = 10      # the full shows the flame per show is taken over
MIN_SHOWS = 3          # of its own before it is no longer "estimating"
FULL_SHARE = 0.9       # of the longest recent show: a full show
SHOWS_KEPT = 30        # per unit, its recent shows
COUNTED_KEPT = 50      # performance stamps remembered as counted
ACTIONS_KEPT = 300     # written entries kept in the file
EPS = 1e-6
NOT_CONFIRMED = ("Not confirmed: LTC Player stopped before this was "
                 "finished.")


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and \
        math.isfinite(v)


def check_n(v):
    """(N, "") for a good shows per refill (None clears it), else
    (False, sentence)."""
    if v is None:
        return None, ""
    if not _is_int(v) or not N_MIN <= v <= N_MAX:
        return False, (f"Shows per refill is a whole number {N_MIN} to "
                       f"{N_MAX}. Nothing was changed.")
    return v, ""


def step_value(x, n):
    """One Refueled +1 press: X + 1 show, never above N; at N, round to 0."""
    x = max(0.0, min(float(x), float(n)))
    return 0.0 if x >= n - EPS else min(float(n), x + 1.0)


def whole(x):
    """Shows left as shown: rounded DOWN to a whole show."""
    return int(math.floor(max(0.0, x) + EPS))


def _times(n):
    return {1: "once", 2: "twice"}.get(n, f"{n} times")


def left_words(n):
    return f"{n} show{'' if n == 1 else 's'} left"


def batch_words(who, name, start, left, clicks, wraps, confirmed=True):
    """The one log entry of a batch of Refueled +1 presses."""
    k = f"clicked {clicks} time{'' if clicks == 1 else 's'}"
    if wraps:
        s = (f"{who} refilled {name}: {left_words(whole(left))} ({k}; "
             f"wrapped past full {_times(wraps)}, so the lower number is a "
             f"correction, not usage).")
    else:
        s = (f"{who} refilled {name}: set from {whole(start)} to "
             f"{left_words(whole(left))} ({k}).")
    return s if confirmed else f"{s} {NOT_CONFIRMED}"


def _piles(d=None):
    d = d or {}
    return {b: float(d.get(b, 0.0) or 0.0) for b in BUCKETS}


def _sum(p):
    return sum(p.values())


def _new_state(m):
    return {"name": m.name, "kind": m.kind, "life": _piles(),
            "anchor": None, "shows": 0, "recent": [], "fuel_part": 0.0,
            "fuel_shows": 0, "last_burn_at": None, "unsure": False,
            "gaps": None, "rev": 0, "pending": None}


def _read_piles(d, w):
    if not isinstance(d, dict) or any(k not in BUCKETS for k in d) or \
            not all(_is_num(v) and v >= 0 for v in d.values()):
        raise ValueError(f"{w} is not flame seconds per pile")
    return _piles(d)


def _read_anchor(a, w):
    if a is None:
        return None
    if not isinstance(a, dict):
        raise ValueError(f"{w}.anchor is not an object")
    x = a.get("x")
    if x is not None and (not _is_num(x) or not 0 <= x <= N_MAX):
        raise ValueError(f"{w}.anchor.x is {x!r}")
    if not _is_int(a.get("shows", 0)) or a.get("shows", 0) < 0:
        raise ValueError(f"{w}.anchor.shows is {a.get('shows')!r}")
    return {"x": None if x is None else float(x),
            "at": a.get("at") if _is_num(a.get("at")) else 0.0,
            "life": _read_piles(a.get("life", {}), f"{w}.anchor.life"),
            "shows": a.get("shows", 0)}


def _read_pending(p, w):
    if p is None:
        return None
    if not isinstance(p, dict) or not isinstance(p.get("who"), str) or \
            not _is_num(p.get("start")):
        raise ValueError(f"{w}.pending is not a batch")
    for k in ("clicks", "wraps"):
        if not _is_int(p.get(k)) or p[k] < 0:
            raise ValueError(f"{w}.pending.{k} is {p.get(k)!r}")
    return {"who": p["who"], "role": str(p.get("role") or ""),
            "start": float(p["start"]), "clicks": p["clicks"],
            "wraps": p["wraps"],
            "at": p.get("at") if _is_num(p.get("at")) else 0.0,
            "prev": _read_anchor(p.get("prev"), w),
            "prev_unsure": p.get("prev_unsure") is True,
            "prev_gaps": p.get("prev_gaps")
            if _is_int(p.get("prev_gaps")) else None,
            # Read back from the file: LTC Player stopped while it was
            # open, so it is written as not confirmed.
            "restarted": True}


def read_doc(doc):
    """fuel_shows.json's content, every field checked; ValueError (with
    what is wrong) for anything this program would not count with."""
    if not isinstance(doc, dict) or doc.get("shows_format") != SHOWS_FORMAT:
        raise ValueError("it is not a shows per refill record this program "
                         "can read")
    ns = doc.get("shows_per_refill", {})
    if not isinstance(ns, dict):
        raise ValueError("shows_per_refill is not an object")
    n = {}
    for k, v in ns.items():
        if k not in KINDS or check_n(v)[0] in (None, False):
            raise ValueError(f"shows_per_refill {k!r} is {v!r}")
        n[k] = v
    units = doc.get("units", {})
    if not isinstance(units, dict):
        raise ValueError("units is not an object")
    out = {}
    for mid, u in units.items():
        w = f"unit {mid!r}"
        if not isinstance(mid, str) or not mid or not isinstance(u, dict) \
                or u.get("kind") not in KINDS:
            raise ValueError(f"{w} is not a flame unit")
        anchor = _read_anchor(u.get("anchor"), w)
        top = n.get(u["kind"])
        if anchor and anchor["x"] is not None and top is not None and \
                anchor["x"] > top + EPS:
            raise ValueError(f"{w}: shows left {anchor['x']} is over {top}")
        rec = u.get("recent", [])
        if not isinstance(rec, list) or not all(
                isinstance(r, list) and len(r) == 2 and
                all(_is_num(x) and x >= 0 for x in r) for r in rec):
            raise ValueError(f"{w}.recent is not [flame s, show s] pairs")
        for k in ("shows", "rev"):
            if not _is_int(u.get(k, 0)) or u.get(k, 0) < 0:
                raise ValueError(f"{w}.{k} is {u.get(k)!r}")
        for k in ("fuel_shows", "gaps"):
            v = u.get(k)
            if v is not None and (not _is_int(v) or v < 0):
                raise ValueError(f"{w}.{k} is {v!r}")
        if not _is_num(u.get("fuel_part", 0.0)) or u.get("fuel_part", 0) < 0:
            raise ValueError(f"{w}.fuel_part is {u.get('fuel_part')!r}")
        for k in ("last_burn_at",):
            v = u.get(k)
            if v is not None and (not _is_num(v) or v < 0):
                raise ValueError(f"{w}.{k} is {v!r}")
        out[mid] = {"name": str(u.get("name") or mid), "kind": u["kind"],
                    "life": _read_piles(u.get("life", {}), f"{w}.life"),
                    "anchor": anchor, "shows": u.get("shows", 0),
                    "recent": [list(map(float, r)) for r in rec][
                        -SHOWS_KEPT:],
                    "fuel_part": float(u.get("fuel_part", 0.0)),
                    "fuel_shows": u.get("fuel_shows") or 0,
                    "last_burn_at": u.get("last_burn_at"),
                    "unsure": u.get("unsure") is True, "gaps": u.get("gaps"),
                    "rev": u.get("rev", 0),
                    "pending": _read_pending(u.get("pending"), w)}
    counted = doc.get("counted", [])
    if not isinstance(counted, list) or not all(_is_num(c) for c in counted):
        raise ValueError("counted is not a list of performance stamps")
    actions = doc.get("actions", [])
    if not isinstance(actions, list):
        raise ValueError("actions is not a list")
    for i, a in enumerate(actions):
        if not isinstance(a, dict) or not _is_int(a.get("id")) or \
                a.get("what") not in ("refuel", "fill", "set") or \
                not isinstance(a.get("who"), str) or \
                not isinstance(a.get("changes", {}), dict):
            raise ValueError(f"actions[{i}] is not an entry this program "
                             f"knows")
    saved = doc.get("saved_at")
    return {"n": n, "units": out, "counted": list(counted)[-COUNTED_KEPT:],
            "actions": [dict(a) for a in actions][-ACTIONS_KEPT:],
            "saved_at": saved if _is_num(saved) else None}


class ShowsCounter:
    """Flame per pile and shows left per flame unit. See the module
    docstring.

    folder   where fuel_shows.json lives (fuel.json's folder)
    clock    () -> monotonic seconds (when a +1 batch is due)
    wall     () -> wall clock seconds, for the record's times only
    note     (text, fault=False) -> journal line; never raises here
    """

    def __init__(self, folder, clock=time.monotonic, wall=time.time,
                 note=None, fuel_get=lambda mid: None):
        self.path = os.path.join(folder, SHOWS_FILE)
        self._clock = clock
        self._wall = wall
        self._note_fn = note
        self._fuel_get = fuel_get    # (id) -> fuel.json's unit, or None
        self.n = {}
        self.units = {}
        self.counted = []
        self.actions = []
        self.saved_at = None
        self.load_error = ""
        self.save_error = ""
        self.fault = ""
        self.dirty = False
        self._last_t = {}            # unit id -> clock of its last +1
        self.written = []            # entries written, for the audit log
        self._load()

    def _note(self, text, fault=False):
        if self._note_fn is not None:
            try:
                self._note_fn(text, fault=fault)
            except Exception:                     # noqa: BLE001
                pass

    # -- the file --------------------------------------------------------------
    def _load(self):
        try:
            with open(self.path, encoding="utf-8-sig") as fh:
                rec = read_doc(json.load(fh))
        except FileNotFoundError:
            return
        except Exception as e:                    # noqa: BLE001
            why = f"{type(e).__name__}: {e}"
            stamp = time.strftime("%Y%m%d-%H%M%S",
                                  time.localtime(self._wall()))
            kept = f"{self.path}.unreadable-{stamp}"
            i = 2
            while os.path.exists(kept):
                kept = f"{self.path}.unreadable-{stamp}-{i}"
                i += 1
            try:
                os.replace(self.path, kept)
                where = f"It was kept as {os.path.basename(kept)}"
            except OSError as e2:
                where = f"It could not be moved aside either ({e2})"
            self.load_error = (f"{SHOWS_FILE} could not be read ({why}). "
                               f"{where}. Set shows per refill again and "
                               f"fill the units when they are full.")
            self._note(f"Fuel: {self.load_error}", fault=True)
            return
        self.n = rec["n"]
        self.units = rec["units"]
        self.counted = rec["counted"]
        self.actions = rec["actions"]
        self.saved_at = rec["saved_at"]

    def text(self):
        """The file's text. Called under the tracker's lock."""
        self.dirty = False
        return json.dumps({"shows_format": SHOWS_FORMAT,
                           "shows_per_refill": self.n, "units": self.units,
                           "counted": self.counted[-COUNTED_KEPT:],
                           "actions": self.actions[-ACTIONS_KEPT:],
                           "saved_at": self._wall()},
                          indent=1, sort_keys=True)

    def write(self, text):
        """Write `text` atomically. True when it is on disk; never raises."""
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
            self.dirty = True
            if not self.save_error:
                self._note(f"Fuel: {self.path} could not be written "
                           f"({why}). Shows left is kept in memory and tried "
                           f"again.", fault=True)
            self.save_error = why
            return False
        self.save_error = ""
        return True

    # -- the flame -----------------------------------------------------------
    def max_id(self):
        return max([0] + [a.get("id", 0) for a in self.actions])

    def top(self, kind):
        return self.n.get(kind)

    def state(self, m, less=None):
        """Unit `m`'s record. Made on first sight with what fuel.json had
        already counted as its history (show, purge, other), less
        `less` = (fuel.json pile, seconds) it was just given and is about
        to be added here, so nothing is counted twice."""
        st = self.units.get(m.id)
        if st is None:
            st = self.units[m.id] = _new_state(m)
            self.dirty = True
            try:
                u = self._fuel_get(m.id)
            except Exception:                     # noqa: BLE001
                u = None
            if u is not None:
                burn = {k: float(u.get("burn", {}).get(k, 0.0) or 0.0)
                        for k in ("show", "purge", "other")}
                if less is not None:
                    burn[less[0]] = max(0.0, burn[less[0]] - less[1])
                for k, v in burn.items():
                    st["life"][k] += v
                st["fuel_part"] = sum(burn.values())
                st["fuel_shows"] = u.get("shows", 0) - (
                    1 if less is not None and less[2] else 0)
        st["name"], st["kind"] = m.name, m.kind
        return st

    def add(self, m, pile, s, fuel, wall, unsure=False):
        """`s` seconds of flame of unit `m` in `pile` (fuel.py _frame, per
        frame). `fuel`: fuel.json counted it too. `unsure`: flamesafe's
        armed state was not known (counted once, in its pile, and the
        unit reads Not sure)."""
        st = self.state(m)
        st["life"][pile] += s
        if unsure:
            st["unsure"] = True
        if fuel:
            st["fuel_part"] += s
        st["last_burn_at"] = wall
        self.dirty = True

    def show_ended(self, ep, burn, is_show, cut, ms, locked, fuel_units,
                   new_id):
        """A performance ended (fuel.py _end_episode, under its lock): its
        flame goes in its pile (show, short, or rehearsal) once. `ms` the
        tracked machines listed now, `locked` (id) -> bool, `fuel_units`
        fuel.py's units, `new_id` () -> a refill number."""
        self.commit_all(new_id)
        key = ep.get("started")
        if key in self.counted:
            self.sync(ms, fuel_units)
            return
        self.counted = (self.counted + [key])[-COUNTED_KEPT:]
        self.dirty = True
        reh = bool(ep.get("rehearsal"))
        pile = "rehearsal" if reh else "show" if is_show else "short"
        tc_s = float(ep.get("tc_s", 0.0) or 0.0)
        by_id = {m.id: m for m in ms}
        hit = []
        for mid, b in (burn or {}).items():
            b = float(b or 0.0)
            m = by_id.get(mid)
            if b <= 0.0 or m is None:
                continue
            st = self.state(m, less=("show" if is_show else "other", b,
                                     is_show and not locked(mid)))
            st["life"][pile] += b
            st["fuel_part"] += b
            st["last_burn_at"] = self._wall()
            if not reh and (is_show or cut) and not locked(mid):
                # A show it fired in (a cut one too: the safe side) for
                # the count with no history; a whole show, not cut, is
                # one to learn the flame per show from.
                st["shows"] += 1
                if cut:
                    st["unsure"] = True
                if is_show and not cut:
                    st["recent"] = (st["recent"] + [[round(b, 3),
                                                     round(tc_s, 3)]])[
                        -SHOWS_KEPT:]
                hit.append(m.name)
        if hit and cut:
            self._note(f"Fuel: a show cut by a restart is counted for "
                       f"{', '.join(hit)}, marked not sure.")
        self.sync(ms, fuel_units)

    def sync(self, ms, fuel_units):
        """What fuel.py has counted now, per unit: its show count."""
        for m in ms:
            u = fuel_units.get(m.id)
            if u is not None:
                self.state(m)["fuel_shows"] = u.get("shows", 0)
                self.dirty = True

    def reconcile(self, ms, fuel_units, new_id, unclean=False, lost_s=0.0):
        """At start, after fuel.json is read. A batch still open is written
        (not confirmed). Flame fuel.json counted that this file never saw
        goes in "unsure"; shows likewise for the count with no history. A
        stop without closing adds `lost_s` of "unsure" flame to each unit
        that burned in the `lost_s` before the last save. Each marks the
        unit not sure."""
        self.commit_all(new_id)            # read back: not confirmed
        for m in ms:
            st = self.state(m)
            if unclean and st["last_burn_at"] is not None and \
                    self.saved_at is not None and \
                    self.saved_at - st["last_burn_at"] <= lost_s:
                st["life"]["unsure"] += lost_s
                st["unsure"] = True
                st["last_burn_at"] = None    # once, not at every restart
                self._note(f"Fuel: {m.name} was burning when LTC Player "
                           f"stopped without closing: {lost_s:g} s counted "
                           f"as not sure.", fault=True)
            u = fuel_units.get(m.id)
            if u is None:
                continue
            tot = sum(float(v or 0.0) for v in u.get("burn", {}).values())
            gap = tot - st["fuel_part"]
            if gap > 0.01:
                st["life"]["unsure"] += gap
                st["unsure"] = True
                self._note(f"Fuel: {m.name}: {gap:.0f} s of flame counted "
                           f"while this record was not kept, counted as not "
                           f"sure.", fault=True)
            elif gap < -0.01:
                st["unsure"] = True
            more = u.get("shows", 0) - (st["fuel_shows"] or 0)
            if more > 0:
                st["shows"] += more
                st["unsure"] = True
            st["fuel_part"] = tot
            st["fuel_shows"] = u.get("shows", 0)
            self.dirty = True

    # -- shows left ------------------------------------------------------------
    def _full(self, recent):
        """The flame of the full shows among `recent` (oldest first)."""
        if not recent:
            return []
        longest = max(r[1] for r in recent)
        return [r[0] for r in recent if r[1] >= FULL_SHARE * longest]

    def per_show(self, st):
        """(flame seconds per show, "learned" | "estimating" | "counting")
        for unit state `st`."""
        own = self._full(st["recent"])[-RECENT_SHOWS:]
        if len(own) >= MIN_SHOWS:
            return sum(own) / len(own), "learned"
        pool = []
        for o in self.units.values():
            if o["kind"] == st["kind"]:
                pool += self._full(o["recent"])[-RECENT_SHOWS:]
        if len(pool) >= MIN_SHOWS:
            return sum(pool) / len(pool), "estimating"
        if own:
            return sum(own) / len(own), "estimating"
        return None, "counting"

    def x_now(self, st, extra=0.0):
        """Shows left now (a float, 0 to N; `extra` flame of a show under
        way, not yet in a pile)."""
        n = self.top(st["kind"])
        a = st["anchor"]
        if n is None or a is None or a["x"] is None:
            return 0.0
        per, _mode = self.per_show(st)
        if per:
            used = (_sum(st["life"]) + extra - _sum(a["life"])) / per
        else:
            used = st["shows"] - a["shows"]
        return max(0.0, min(float(n), a["x"] - used))

    def _anchor(self, st, x, extra=0.0):
        """X at this moment. Its flame totals take in `extra`, the flame of
        a performance still under way (not yet in a pile), as fuel.json's
        _snapshot does: when that performance ends and its flame goes in
        the Shows pile, only flame after this moment counts against X."""
        life = dict(st["life"])
        life["show"] += float(extra or 0.0)
        return {"x": x, "at": self._wall(), "life": life,
                "shows": st["shows"]}

    # -- presses ---------------------------------------------------------------
    def _entry(self, bid, what, who, role, changes, **extra):
        a = dict(id=bid, what=what, at=self._wall(), who=who or "unsigned",
                 role=role or "", changes=changes, undone=None, **extra)
        self.actions = (self.actions + [a])[-ACTIONS_KEPT:]
        self.dirty = True
        return a

    def step(self, m, who, role, gaps, new_id, extra=0.0):
        """Refueled +1 on machine `m` (its type has N): (old X, new X)."""
        n = self.top(m.kind)
        st = self.state(m)
        p = st["pending"]
        if p is not None and p["who"] != (who or "unsigned"):
            self.commit(m.id, new_id)
            p = None
        old = self.x_now(st, extra)
        if p is None:
            p = st["pending"] = {
                "who": who or "unsigned", "role": role or "", "start": old,
                "clicks": 0, "wraps": 0, "at": self._wall(),
                "prev": st["anchor"], "prev_unsure": st["unsure"],
                "prev_gaps": st["gaps"]}
        new = step_value(old, n)
        if old >= n - EPS:
            p["wraps"] += 1
        p["clicks"] += 1
        st["anchor"] = self._anchor(st, new, extra)
        if new >= n - EPS:
            st["unsure"], st["gaps"] = False, gaps
        st["rev"] += 1
        self.dirty = True
        self._last_t[m.id] = self._clock()
        return old, new

    def due(self, now=None):
        """Unit ids whose open batch has sat COMMIT_S (or longer)."""
        now = self._clock() if now is None else now
        return [mid for mid, st in self.units.items()
                if st["pending"] is not None and
                now - self._last_t.get(mid, float("-inf")) >= COMMIT_S]

    def commit(self, mid, new_id, confirmed=True):
        """Write unit `mid`'s open batch as ONE entry. The entry, or None."""
        st = self.units.get(mid)
        p = st and st["pending"]
        if not p:
            return None
        st["pending"] = None
        self._last_t.pop(mid, None)
        confirmed = confirmed and not p.get("restarted")
        left = st["anchor"]["x"] if st["anchor"] else 0.0
        words = batch_words(p["who"], st["name"], p["start"], left,
                            p["clicks"], p["wraps"], confirmed)
        a = self._entry(new_id(), "refuel", p["who"], p["role"],
                        {mid: {"old": round(p["start"], 3),
                               "new": round(left, 3), "rev": st["rev"],
                               "prev": p["prev"],
                               "unsure": p["prev_unsure"],
                               "gaps": p["prev_gaps"]}},
                        clicks=p["clicks"], wraps=p["wraps"],
                        confirmed=confirmed, words=words)
        self._note(f"Fuel: {words}")
        self.written.append(a)
        return a

    def commit_all(self, new_id):
        return [a for a in (self.commit(mid, new_id)
                            for mid in list(self.units)) if a]

    def filled(self, ms, bid, who, role, gaps, new_id, extra=None):
        """A fuel.py refill of each unit in `ms` (Refilled, Fill selected,
        Refill all), refill number `bid`: X to N (or, with no N, only "since
        the refill" starts again). {id: (old X, new X or None)}."""
        extra = extra or {}
        self.commit_all(new_id)
        out, changes = {}, {}
        for m in ms:
            st = self.state(m)
            n = self.top(m.kind)
            old = self.x_now(st, extra.get(m.id, 0.0))
            changes[m.id] = {"old": round(old, 3), "new": n,
                             "prev": st["anchor"], "unsure": st["unsure"],
                             "gaps": st["gaps"]}
            st["anchor"] = self._anchor(st, None if n is None
                                        else float(n), extra.get(m.id, 0.0))
            st["unsure"], st["gaps"] = False, gaps
            st["rev"] += 1
            changes[m.id]["rev"] = st["rev"]
            out[m.id] = (old, n)
        self._entry(bid, "fill", who, role, changes)
        return out

    def set_n(self, kind, n, bid, who, role):
        """Shows per refill of `kind` to `n` (None: tracking only): (old N,
        {id: (old X, new X)} for units lowered to the new N)."""
        old = self.n.get(kind)
        if n is None:
            self.n.pop(kind, None)
        else:
            self.n[kind] = n
        lowered = {}
        for mid, st in self.units.items():
            a = st["anchor"]
            if st["kind"] == kind and n is not None and a and \
                    a["x"] is not None and a["x"] > n:
                lowered[mid] = (a["x"], n)
                a["x"] = float(n)
        self._entry(bid, "set", who, role, {}, kind=kind, n_old=old,
                    n_new=n)
        return old, lowered

    def undo(self, bid, who):
        """Undo written entry `bid`: None when it is not one of these.
        Else (ok, put back names, kept names). Shows left goes back to what
        it was before the entry while no press or refill has changed that
        unit since; flame burned since stays counted. A Refueled +1 entry
        with a unit changed since is refused; a refill puts back the units
        not changed since and keeps the rest."""
        a = next((x for x in self.actions if x.get("id") == bid and
                  x.get("what") in ("refuel", "fill")), None)
        if a is None or a.get("undone"):
            return None
        back, kept = [], []
        for mid, ch in a["changes"].items():
            st = self.units.get(mid)
            (back if st is not None and st["rev"] == ch["rev"] and
             st["pending"] is None else kept).append(mid)
        names = [self.units.get(m, {}).get("name", m) for m in kept]
        if a["what"] == "refuel" and kept:
            return False, [], names
        for mid in back:
            st, ch = self.units[mid], a["changes"][mid]
            st["anchor"] = ch["prev"]
            st["unsure"], st["gaps"] = ch["unsure"], ch["gaps"]
            st["rev"] += 1
        a["undone"] = {"who": who or "unsigned", "at": self._wall()}
        self.dirty = True
        return True, [self.units[m]["name"] for m in back], names

    # -- what the page shows -----------------------------------------------------
    def refills(self, mid):
        """Written refill events of unit `mid` (a +1 batch or a refill),
        not undone: what planning counts, never one per press."""
        return sum(1 for a in self.actions
                   if a.get("what") in ("refuel", "fill") and
                   not a.get("undone") and mid in a.get("changes", {}))

    def use_view(self, m, extra=0.0):
        """Where the flame went: seconds per pile since the refill and in
        all, shows, flame per show. Any tracked unit, N or not."""
        st = self.units.get(m.id) or _new_state(m)
        a = st["anchor"]
        base = a["life"] if a else _piles()
        life = dict(st["life"])
        life["show"] += extra                  # a show under way
        per, mode = self.per_show(st)
        return {"since": {b: round(max(0.0, life[b] - base[b]), 1)
                          for b in BUCKETS},
                "life": {b: round(life[b], 1) for b in BUCKETS},
                "shows_since": st["shows"] - (a["shows"] if a else 0),
                "shows": st["shows"],
                "per_show_s": None if per is None else round(per, 1),
                "mode": mode,
                "since_at_ms": int(float(a["at"]) * 1000) if a else None}

    def unit_view(self, m, gaps, extra=0.0):
        """Shows left; None for a type with no N (tracking only)."""
        n = self.top(m.kind)
        if n is None:
            return None
        st = self.units.get(m.id) or _new_state(m)
        x = self.x_now(st, extra)
        k = whole(x)
        _per, mode = self.per_show(st)
        not_sure = bool(st["unsure"] or (st["gaps"] is not None and
                                         gaps > st["gaps"]) or
                        (st["gaps"] is None and gaps > 0))
        words = "Refuel now" if k < 1 else f"Shows left: {k}"
        if mode != "learned" and k >= 1:
            words += ", estimating"
        return {"left": k, "x": round(x, 2), "n": n, "refuel_now": k < 1,
                "estimating": mode != "learned", "mode": mode,
                "not_sure": not_sure, "pending": st["pending"] is not None,
                "refills": self.refills(m.id),
                "words": words + (". Not sure" if not_sure else "")}

    def recent(self):
        """The written Refueled +1 entries, for Recent refills."""
        out = {}
        for a in self.actions:
            if a.get("what") != "refuel":
                continue
            (mid, ch), = a["changes"].items()
            name = self.units.get(mid, {}).get("name", mid)
            out[a["id"]] = {
                "id": a["id"], "who": a.get("who"),
                "at_ms": int(float(a.get("at") or 0) * 1000),
                "units": [f"{name}: {whole(ch['old'])} to "
                          f"{left_words(whole(ch['new']))}" +
                          (" (wrapped: a correction)" if a.get("wraps")
                           else "") +
                          ("" if a.get("confirmed", True)
                           else " (not confirmed)")],
                "undone": a.get("undone")}
        return out
