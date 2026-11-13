"""Shows per refill (Jeff, 2026-11-13): on the Fuel page, every flame unit
of a type with a "Shows per refill" number shows "Shows left: X" (0 reads
"Refuel now"). FUEL.md says it in plain words.

  N   "Shows per refill", one whole number PER MACHINE TYPE (Wave Flamer,
      G-Flame: fuel.py's tracked kinds), 1 to N_MAX, set by an admin on
      the Fuel page. A type with no N is tracking only: its tiles stay the
      fuel tracker's own (shows since the refill, the learned tank), with
      no countdown and no Refuel now.
  X   shows left before the unit needs fuel, 0 to N, never outside.

How X goes down (exactly once per show)
=======================================

fuel.py decides what a show is (a performance whose show timecode ADVANCED
min_show_s, not a programming session, not scrubbing, not live flame
programming, not a purge) and calls show_ended() once when a performance
ends. X goes down by one for each unit that BURNED in that show (its flame
seconds in the show above 0, the same burn fuel.py counts; not locked
out). A unit that sat the show out keeps its X.

Exactly once, even across restarts: each performance is known by its start
stamp (fuel.json's episode "started"), and a stamp already counted is never
counted again (a crash after this file was written but before fuel.json was
replays the same performance at the next start). This file is written
BEFORE fuel.json in the same save. If this file was not written and
fuel.json was, the unit's fuel.py show count is ahead of the one this file
last saw: those shows are counted down at the next start and the unit is
marked Not sure (they may include a show it sat out: emptier, never
fuller).

A performance cut by a restart (the engine stopped or crashed during it)
counts for every unit that burned in it, even if it was shorter than
min_show_s, and marks that unit Not sure. A cut show is never silently
dropped. Not sure also follows fuel.py's gaps since the unit was last at N.

Refueled +1, and its one log entry
==================================

Each Refueled +1 press adds ONE to X at once (saved at once, crash safe);
after N the next press goes round to 0 and on (0, 1, 2), so an extra press
is fixed by pressing on round the loop. The LOG entry (journal, audit, the
record's refill event) is not written per press: the presses on one unit
by one person are one batch, written once its X has sat unchanged for
COMMIT_S seconds, in words for what happened ("set from 0 to 3 shows
left", or, when it went round past full, that the lower number is a
correction, not usage). A batch still open when LTC Player stops or
crashes is written at the next start, marked not confirmed. A show ending,
or another person pressing that unit, writes the open batch first. One
batch is one refill event for planning, never one per press. Undo (an
admin, Recent refills) works on the written entry, while that unit has not
changed since. A +1 never logs a fuel.py refill (a +1 is not a full tank,
so the tank learning is not touched).

Fill selected (fuel.py FuelTracker.fill) logs a fuel.py refill of every
selected flame unit, as Refill all does, and puts each one whose type has N
at X = N, under one refill number (one Undo).

Kept in fuel_shows.json beside fuel.json, its own format number, written
atomically. fuel.json is not changed, so an older LTC Player reads and
writes fuel.json as before and never sees this file.

Every change is made under the FuelTracker's lock (it calls in); this
module has no lock and no thread of its own.
"""
import json
import os
import time

SHOWS_FILE = "fuel_shows.json"
SHOWS_FORMAT = 1
KINDS = ("gflame", "waver")          # fuel.TRACKED_KINDS
KIND_NAMES = {"gflame": "G-Flame", "waver": "Wave Flamer"}
N_MIN = 1
N_MAX = 99
COMMIT_S = 5.0         # a +1 batch is written once X sat this long
COUNTED_KEPT = 50      # performance stamps remembered as counted
ACTIONS_KEPT = 300     # written entries kept in the file
NOT_CONFIRMED = ("Not confirmed: LTC Player stopped before this was "
                 "finished.")


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


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
    """One Refueled +1 press: X + 1, and after N round to 0."""
    x = max(0, min(int(x), n))
    return 0 if x >= n else x + 1


def _times(n):
    return {1: "once", 2: "twice"}.get(n, f"{n} times")


def left_words(n):
    return f"{n} show{'' if n == 1 else 's'} left"


def batch_words(who, name, start, left, clicks, wraps, confirmed=True):
    """The one log entry of a batch of Refueled +1 presses."""
    k = f"clicked {clicks} time{'' if clicks == 1 else 's'}"
    if wraps:
        s = (f"{who} refilled {name}: {left_words(left)} ({k}; wrapped "
             f"past full {_times(wraps)}, so the lower number is a "
             f"correction, not usage).")
    else:
        s = (f"{who} refilled {name}: set from {start} to "
             f"{left_words(left)} ({k}).")
    return s if confirmed else f"{s} {NOT_CONFIRMED}"


def _new_state(m):
    # A unit never filled reads 0 (Refuel now): never more than is known.
    return {"name": m.name, "kind": m.kind, "left": 0, "fuel_shows": None,
            "unsure": False, "gaps": None, "rev": 0, "pending": None}


def _read_pending(p, w):
    if p is None:
        return None
    if not isinstance(p, dict) or not isinstance(p.get("who"), str):
        raise ValueError(f"{w}.pending is not a batch")
    for k in ("start", "clicks", "wraps"):
        if not _is_int(p.get(k)) or p[k] < 0:
            raise ValueError(f"{w}.pending.{k} is {p.get(k)!r}")
    return {"who": p["who"], "role": str(p.get("role") or ""),
            "start": p["start"], "clicks": p["clicks"], "wraps": p["wraps"],
            "at": p.get("at") if isinstance(p.get("at"), (int, float))
            else 0.0, "prev_unsure": p.get("prev_unsure") is True,
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
        left, top = u.get("left"), n.get(u["kind"])
        if not _is_int(left) or left < 0 or (top is not None and
                                             left > top):
            raise ValueError(f"{w}.left is {left!r}")
        for k in ("fuel_shows", "gaps"):
            v = u.get(k)
            if v is not None and (not _is_int(v) or v < 0):
                raise ValueError(f"{w}.{k} is {v!r}")
        if not _is_int(u.get("rev", 0)) or u.get("rev", 0) < 0:
            raise ValueError(f"{w}.rev is {u.get('rev')!r}")
        out[mid] = {"name": str(u.get("name") or mid), "kind": u["kind"],
                    "left": left, "fuel_shows": u.get("fuel_shows"),
                    "unsure": u.get("unsure") is True, "gaps": u.get("gaps"),
                    "rev": u.get("rev", 0),
                    "pending": _read_pending(u.get("pending"), w)}
    counted = doc.get("counted", [])
    if not isinstance(counted, list) or not all(
            isinstance(c, (int, float)) and not isinstance(c, bool)
            for c in counted):
        raise ValueError("counted is not a list of performance stamps")
    actions = doc.get("actions", [])
    if not isinstance(actions, list):
        raise ValueError("actions is not a list")
    for i, a in enumerate(actions):
        w = f"actions[{i}]"
        if not isinstance(a, dict) or not _is_int(a.get("id")) or \
                a.get("what") not in ("refuel", "fill", "set") or \
                not isinstance(a.get("who"), str) or \
                not isinstance(a.get("changes", {}), dict):
            raise ValueError(f"{w} is not an entry this program knows")
        for mid, c in a.get("changes", {}).items():
            if not isinstance(c, list) or len(c) != 5:
                raise ValueError(f"{w}.changes[{mid!r}] is {c!r}")
    return {"n": n, "units": out, "counted": list(counted)[-COUNTED_KEPT:],
            "actions": [dict(a) for a in actions][-ACTIONS_KEPT:]}


class ShowsCounter:
    """Shows left per flame unit. See the module docstring.

    folder   where fuel_shows.json lives (fuel.json's folder)
    clock    () -> monotonic seconds (when a +1 batch is due)
    wall     () -> wall clock seconds, for the record's times only
    note     (text, fault=False) -> journal line; never raises here
    """

    def __init__(self, folder, clock=time.monotonic, wall=time.time,
                 note=None):
        self.path = os.path.join(folder, SHOWS_FILE)
        self._clock = clock
        self._wall = wall
        self._note_fn = note
        self.n = {}
        self.units = {}
        self.counted = []
        self.actions = []
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

    # -- the record --------------------------------------------------------------
    def max_id(self):
        return max([0] + [a.get("id", 0) for a in self.actions])

    def top(self, kind):
        return self.n.get(kind)

    def state(self, m):
        st = self.units.get(m.id)
        if st is None:
            st = self.units[m.id] = _new_state(m)
            self.dirty = True
        st["name"], st["kind"] = m.name, m.kind
        return st

    def _down(self, st, unsure):
        st["left"] = max(0, st["left"] - 1)
        st["rev"] += 1
        if unsure:
            st["unsure"] = True
        self.dirty = True

    def show_ended(self, ep, burn, is_show, cut, ms, locked, fuel_units,
                   new_id):
        """A performance ended (fuel.py _end_episode, under its lock). `ms`
        the tracked machines listed now, `locked` (id) -> bool,
        `fuel_units` fuel.py's units (their "shows" counts), `new_id` ()
        -> a refill number for a batch written first."""
        self.commit_all(new_id)
        key = ep.get("started")
        if not ep.get("rehearsal") and (is_show or cut) and \
                key not in self.counted:
            hit = []
            for m in ms:
                if float(burn.get(m.id, 0.0) or 0.0) > 0.0 and \
                        not locked(m.id):
                    self._down(self.state(m), cut)
                    hit.append(m.name)
            self.counted = (self.counted + [key])[-COUNTED_KEPT:]
            self.dirty = True
            if hit:
                self._note(f"Fuel: shows left down by one for "
                           f"{', '.join(hit)}"
                           f"{' (cut by a restart, so not sure)' if cut else ''}"
                           f".")
        self.sync(ms, fuel_units)

    def sync(self, ms, fuel_units):
        """What fuel.py's show count is now, per unit."""
        for m in ms:
            u = fuel_units.get(m.id)
            st = self.state(m)
            if u is not None and st["fuel_shows"] != u.get("shows", 0):
                st["fuel_shows"] = u.get("shows", 0)
                self.dirty = True

    def reconcile(self, ms, fuel_units, new_id):
        """At start, after fuel.json is read. A batch still open when LTC
        Player stopped is written, not confirmed. Shows fuel.py counted
        that this file never saw (it was not written, or an older LTC
        Player ran) are counted down now, not sure. A fuel.json that
        started again (fewer shows than seen) marks the unit not sure."""
        self.commit_all(new_id)            # read back: not confirmed
        for m in ms:
            u = fuel_units.get(m.id)
            if u is None:
                continue
            st = self.state(m)
            seen, now = st["fuel_shows"], u.get("shows", 0)
            if seen is None:
                st["fuel_shows"] = now
                self.dirty = True
                continue
            if now > seen:
                for _ in range(now - seen):
                    self._down(st, True)
                self._note(f"Fuel: {m.name}: {now - seen} show(s) counted "
                           f"while this record was not kept; shows left "
                           f"counted down and marked not sure.", fault=True)
            elif now < seen:
                st["unsure"] = True
                self._note(f"Fuel: {m.name}: the fuel record has fewer "
                           f"shows than before; shows left marked not "
                           f"sure.", fault=True)
            st["fuel_shows"] = now
            self.dirty = True

    def _entry(self, bid, what, who, role, changes, **extra):
        a = dict(id=bid, what=what, at=self._wall(), who=who or "unsigned",
                 role=role or "", changes=changes, undone=None, **extra)
        self.actions = (self.actions + [a])[-ACTIONS_KEPT:]
        self.dirty = True
        return a

    def _set(self, st, new, gaps):
        """X to `new`; at N what came before is no longer in question."""
        st["left"] = new
        if new == self.top(st["kind"]):
            st["unsure"] = False
            st["gaps"] = gaps
        st["rev"] += 1
        self.dirty = True

    def step(self, m, who, role, gaps, new_id):
        """Refueled +1 on machine `m` (its type has N): (old, new, the
        entry a different person's open batch was written as, or None)."""
        n = self.top(m.kind)
        st = self.state(m)
        first = None
        p = st["pending"]
        if p is not None and p["who"] != (who or "unsigned"):
            first = self.commit(m.id, new_id)
            p = None
        if p is None:
            p = st["pending"] = {
                "who": who or "unsigned", "role": role or "",
                "start": st["left"], "clicks": 0, "wraps": 0,
                "at": self._wall(), "prev_unsure": st["unsure"],
                "prev_gaps": st["gaps"]}
        old = st["left"]
        new = step_value(old, n)
        if old >= n:
            p["wraps"] += 1
        p["clicks"] += 1
        self._set(st, new, gaps)
        self._last_t[m.id] = self._clock()
        return old, new, first

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
        words = batch_words(p["who"], st["name"], p["start"], st["left"],
                            p["clicks"], p["wraps"], confirmed)
        a = self._entry(new_id(), "refuel", p["who"], p["role"],
                        {mid: [p["start"], st["left"], st["rev"],
                               p["prev_unsure"], p["prev_gaps"]]},
                        clicks=p["clicks"], wraps=p["wraps"],
                        confirmed=confirmed, words=words)
        self._note(f"Fuel: {words}")
        self.written.append(a)
        return a

    def commit_all(self, new_id, confirmed=True):
        return [a for a in (self.commit(mid, new_id, confirmed)
                            for mid in list(self.units)) if a]

    def fill(self, ms, bid, who, role, gaps):
        """Every unit in `ms` whose type has N to N: {id: (old, N)}."""
        out, changes = {}, {}
        for m in ms:
            n = self.top(m.kind)
            if n is None:
                continue
            st = self.state(m)
            ch = [st["left"], n, 0, st["unsure"], st["gaps"]]
            self._set(st, n, gaps)
            ch[2] = st["rev"]
            changes[m.id] = ch
            out[m.id] = (ch[0], n)
        if changes:
            self._entry(bid, "fill", who, role, changes)
        return out

    def set_n(self, kind, n, bid, who, role):
        """Shows per refill of `kind` to `n` (None: tracking only): (old N,
        {id: (old X, new X)} lowered to the new N)."""
        old = self.n.get(kind)
        if n is None:
            self.n.pop(kind, None)
        else:
            self.n[kind] = n
        lowered, changes = {}, {}
        for mid, st in self.units.items():
            if st["kind"] == kind and n is not None and st["left"] > n:
                changes[mid] = [st["left"], n, 0, st["unsure"], st["gaps"]]
                lowered[mid] = (st["left"], n)
                st["left"] = n
                st["rev"] += 1
                changes[mid][2] = st["rev"]
        self._entry(bid, "set", who, role, changes, kind=kind, n_old=old,
                    n_new=n)
        return old, lowered

    def find(self, bid):
        for a in self.actions:
            if a.get("id") == bid and a.get("what") in ("refuel", "fill"):
                return a
        return None

    def undo(self, bid, who):
        """Undo written entry `bid`: None when it is not one of these.
        Else (ok, put back names, kept names). A Refueled +1 entry is
        undone only while its unit has not changed since (a show, another
        press); Fill selected puts back every unit not changed since and
        keeps the rest."""
        a = self.find(bid)
        if a is None or a.get("undone"):
            return None
        back, kept = [], []
        for mid, ch in a["changes"].items():
            st = self.units.get(mid)
            (back if st is not None and st["rev"] == ch[2] and
             st["pending"] is None else kept).append(mid)
        names = [self.units.get(m, {}).get("name", m) for m in kept]
        if a["what"] == "refuel" and kept:
            return False, [], names
        for mid in back:
            st, ch = self.units[mid], a["changes"][mid]
            top = self.top(st["kind"])
            st["left"] = max(0, ch[0] if top is None else min(ch[0], top))
            st["unsure"], st["gaps"] = ch[3], ch[4]
            st["rev"] += 1
        a["undone"] = {"who": who or "unsigned", "at": self._wall()}
        self.dirty = True
        return True, [self.units[m]["name"] for m in back], names

    # -- what the page shows -----------------------------------------------------
    def refills(self, mid):
        """Written refill events of unit `mid` (a +1 batch or a fill), not
        undone: what planning counts, never one per press."""
        return sum(1 for a in self.actions
                   if a.get("what") in ("refuel", "fill") and
                   not a.get("undone") and mid in a.get("changes", {}))

    def unit_view(self, m, gaps):
        """None for a type with no N (tracking only)."""
        n = self.top(m.kind)
        if n is None:
            return None
        st = self.units.get(m.id) or _new_state(m)
        left = max(0, min(st["left"], n))
        not_sure = bool(st["unsure"] or (st["gaps"] is not None and
                                         gaps > st["gaps"]) or
                        (st["gaps"] is None and gaps > 0))
        words = "Refuel now" if left <= 0 else f"Shows left: {left}"
        return {"left": left, "n": n, "refuel_now": left <= 0,
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
                "units": [f"{name}: {ch[0]} to {left_words(ch[1])}" +
                          (" (wrapped: a correction)" if a.get("wraps")
                           else "") +
                          ("" if a.get("confirmed", True)
                           else " (not confirmed)")],
                "undone": a.get("undone")}
        return out
