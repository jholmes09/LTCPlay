"""The fireball's charge state (Jeff, 2026-10-30): is the FUEL tank and is
the AIR tank charged?

"The air and fuel charges are building up pressure ... if air and fuel
were pressurized, but there was no shot, the stop button doesn't
depressurize that. So you need to know that something was pressurized and
no shot was fired. The way this is reset is either the shot is fired or
purge is clicked. Do not under any circumstances double charge the air or
fuel."

What it is
==========

One record per tank, "fuel" and "air", each EMPTY, CHARGED or UNKNOWN,
with when it was charged, how many times it was topped off since, and the
last thing that happened to it. It is kept in FILE in the settings folder,
written whole and atomically on every change, so it survives Stop, Hold,
the end of a show, an engine restart and a power cut. Every change is one
journal line and one audit event.

It only WATCHES. It reads each flame frame after the flame link has sent
it (FlameLink.observer, the same hook the fuel tracker uses) and
flamesafe's status (is the fireball group ARMED?). It never sends, never
changes a value and never slows the sender: the sender's call does a few
comparisons in memory; the file, the journal and the audit log are
written on this module's own thread. Using it to skip an automatic charge
is the engine's job (a later change).

The evidence (what counts)
==========================

flamesafe passes a fire value only while its group is ARMED; it is the
only writer of the flame universe, and its status reports, per group and
every tick, the fire values it SENT (sent_fire). A charge counts when the
frame went out (the send worked) AND flamesafe's status, under
STATUS_FRESH_S old, said the fireball group was ARMED (cautious: a charge
is never missed, even one shorter than a status frame). A release (the
valve or the purge) resets the tanks only once flamesafe's own status has
also reported SENDING it: one it never reported within CONFIRM_S of
closing leaves the tanks UNKNOWN (cautious: a reset is never assumed):

  - ARMED: the value reached the fireball. Evidence.
  - not armed (a fresh status saying so): flamesafe sent zero. Nothing
    happened.
  - not known (no fresh status, no fireball group in it): it may have
    reached the fireball. Never counted as a reset; a charge sent then
    makes an EMPTY tank UNKNOWN.

Any value above 0 counts as ON (the most cautious reading). A status
with no sent_fire (an older flamesafe) confirms a release by the armed
state alone.

The transitions
===============

  Fuel Charge (55) ON          fuel CHARGED (a charge by the show, or by
                               the Fireball page's button). Already
                               CHARGED and switched on again by the
                               Fireball page: a top-off by that person,
                               counted, not a double charge.
  Air Charge (56) ON           air CHARGED, the same.
  Valve Open (57) ON, then     the shot was fired: both tanks EMPTY
  off again                    ("the valve opened and released the
                               pressure"; the Igniter is independent and
                               plays no part).
  Purge (59) ON, then off      purged: both tanks EMPTY.

A tank whose charge was on while the valve or the purge was open is
CHARGED (or UNKNOWN) again when it closes, not EMPTY. NOTHING ELSE resets
a tank: not Stop, not Hold, not the end of a show, not an engine restart.

The record's states, as the screens say them: EMPTY, CHARGED, NOT KNOWN
(UNKNOWN here), and PENDING: the record says an automatic charge may be
going out (round-5 review, write-ahead). Before any automatic charge (a
timeline's Fuel or Air stage, an FSEQ's guarded 55 or 56) goes out, the
flame cues ask charge_ready(): the PENDING mark is written and on disk
first, or the charge is not sent ("the charge record cannot be saved:
automatic charging is off"). A restart that reads PENDING makes an EMPTY
tank NOT KNOWN. A shot, a Purge or an admin's Tanks are empty clears it.
The Fireball page's presses ask for the mark but never wait on it. Each
record's revision is above the clock in milliseconds and above every
revision seen, so a later write always outranks an earlier one.

UNKNOWN: the file could not be read; the file says the valve or the purge
was open when the engine stopped (the release may not have finished); a
charge, valve or purge value sent while flamesafe's status was not known.
Only a shot or a purge seen for certain makes an UNKNOWN tank EMPTY (a
charge seen for certain makes it CHARGED). A missing file (the very first
run) is EMPTY (Jeff, 2026-10-30: "tanks start empty"), and the journal
says so loudly, with the folder (a lost settings folder, or LTC Player
under another Windows user, also finds no record); the record is written
at once, and one that cannot be written there makes both tanks NOT KNOWN.
An admin can confirm NOT KNOWN tanks empty (confirm_empty). A newer
record left in FILE.tmp by a write that could not finish (Windows: the
file held when it was to be replaced) is the one read at start (of two
readable records, the higher revision); one that cannot be read makes
both tanks NOT KNOWN and says what FILE last said. Every new revision is
above the highest readable one. A charge that went off again while the
valve or the purge was still open was released with it.
"""
import collections
import datetime
import json
import os
import threading
import time

NAME = "Fireball charge"
FILE = "ltcplay_fireball_charge.json"
FORMAT = 1
TANKS = (("fuel", "Fuel", 55), ("air", "Air", 56))
TANK_WORD = {k: w for k, w, _s in TANKS}
TANK_SLOT = {k: s for k, _w, s in TANKS}
VALVE_SLOT = 57
PURGE_SLOT = 59
FIRE_SLOTS = (55, 56, 57, 58, 59)
EMPTY, CHARGED, UNKNOWN = "empty", "charged", "unknown"
STATES = (EMPTY, CHARGED, UNKNOWN)
GROUP_NAME = "fireball"
STATUS_FRESH_S = 0.5        # flamesafe's status, as the Fireball page
# A release (the valve or the purge) is only counted once flamesafe's own
# status has reported SENDING it (its group's sent_fire): this long after
# it closed, a release flamesafe never reported leaves the tanks UNKNOWN.
CONFIRM_S = 0.5
MANUAL_FRESH_S = 0.5        # a Fireball page press names who, this long
WATCH_S = 0.02
# A record that could not be written (Windows: an indexer or antivirus
# holding the file) is kept in memory and written again this often until
# it is, with a fault shown meanwhile (review of #185).
RETRY_S = 1.0
# close()'s last write is given this long at most (a hung disk).
CLOSE_S = 2.0
QUEUE_MAX = 2000
_TANK_KEYS = {"state", "since", "charged_at", "topoffs", "last_event", "by"}
_DOC_KEYS = {"fireball_charge_format", "rev", "tanks", "releasing",
             "pending"}


def _utc(t):
    return datetime.datetime.fromtimestamp(
        t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_tank(state, t, event):
    return {"state": state, "since": _utc(t), "charged_at": "",
            "topoffs": 0, "last_event": event, "by": ""}


def armed_from_status(view, group=GROUP_NAME, fresh_s=STATUS_FRESH_S):
    """"armed", "off" or "unknown" for the fireball group from the
    remote's FlameStatus view: "armed" or "off" only from a status under
    `fresh_s` old that lists the group."""
    try:
        fl = view or {}
        age = fl.get("age_ms")
        if not fl or fl.get("stale") or not fl.get("connected") or \
                isinstance(age, bool) or \
                not isinstance(age, (int, float)) or age > fresh_s * 1000:
            return "unknown"
        for g in fl.get("groups") or []:
            if str(g.get("name", "")).strip().lower() == group:
                return "armed" if g.get("armed") == "armed" else "off"
    except Exception:                            # noqa: BLE001
        pass
    return "unknown"


def read_doc(doc):
    """FILE's contents checked strictly: (rev, tanks, releasing, pending).
    ValueError otherwise. A record from before the write-ahead mark has
    no "pending": False."""
    if not isinstance(doc, dict) or set(doc) - _DOC_KEYS or \
            doc.get("fireball_charge_format") != FORMAT:
        raise ValueError("not a fireball charge file")
    rev = doc.get("rev")
    if isinstance(rev, bool) or not isinstance(rev, int) or rev < 0:
        raise ValueError("rev is not a whole number")
    tanks = doc.get("tanks")
    if not isinstance(tanks, dict) or set(tanks) != set(TANK_WORD):
        raise ValueError("the tanks are not fuel and air")
    out = {}
    for k, t in tanks.items():
        if not isinstance(t, dict) or set(t) != _TANK_KEYS or \
                t["state"] not in STATES or \
                isinstance(t["topoffs"], bool) or \
                not isinstance(t["topoffs"], int) or t["topoffs"] < 0 or \
                not all(isinstance(t[x], str) for x in
                        ("since", "charged_at", "last_event", "by")):
            raise ValueError(f"the {k} record is not right")
        out[k] = dict(t)
    rel = doc.get("releasing")
    if rel not in ("", "shot", "purge"):
        raise ValueError("releasing is not right")
    pending = doc.get("pending", False)
    if not isinstance(pending, bool):
        raise ValueError("pending is not right")
    return rev, out, rel, pending


class ChargeTracker:
    """The two tanks, watched from the flame link's frames.

    folder        the settings folder (FILE), or None (memory only)
    flames_get    () -> the remote's FlameStatus view, or None
    journal       (who, screen, outcome, text, fault=False) or None
    audit         audit.AuditLog or None
    threaded      False for the selftest: pump() by hand"""

    def __init__(self, folder, flames_get=lambda: None, journal=None,
                 audit=None, wall=time.time, clock=time.monotonic,
                 threaded=True):
        self.path = os.path.join(folder, FILE) if folder else None
        self._flames = flames_get
        self._journal = journal
        self.audit = audit
        self._wall = wall
        self._clock = clock
        self._lock = threading.Lock()
        self._events = collections.deque()
        self.dropped = 0
        self.rev = 0
        # The write-ahead mark (round-5 review): an automatic charge may be
        # going out, so EMPTY on this record may not be true. Written and
        # on disk (fsync) BEFORE any automatic charge is sent; cleared by
        # a shot, a Purge, or an admin's Tanks are empty.
        self.pending = False
        self._pending_rev = None    # the revision that first carried it
        self._durable_rev = -1      # the newest revision known on disk
        self._failed_rev = None     # the newest revision a write refused
        self.fault = ""
        self.write_fault = ""
        # flamesafe's fireball group on other channels than 55 to 59: what
        # goes out on 55 to 59 is not the fireball, so nothing is recorded
        # (round-4 review), and this says why.
        self.group_fault = ""
        self.first_run = False
        self._armed = "unknown"
        self._armed_at = None
        self._prev = {k: 0 for k in TANK_WORD}    # last value seen passing
        self._release = None    # {"kind", "certain", "during": {tank: lvl}}
        self._ending = None     # (that release, when it closed)
        # flamesafe's own report of what it SENT on the fireball's slots
        # (its status's per group sent_fire): None until a status that
        # carries it is read; then when it last showed the valve or the
        # purge above 0.
        self._sent_supported = None
        self._sent_release_at = None
        self._manual = {}       # tank -> (who, screen, at)
        # tank -> clock: the show's own charge carrying on after a gap of
        # one frame or so (fireball_cues.GAP_S): its edge is that charge.
        self._continuing = {}
        self._unwritten = None  # the newest record not yet on disk
        self._retry_at = 0.0
        # One writer at a time: the writer thread, or close()'s last try.
        self._wlock = threading.Lock()
        self.last_sent = bytes(512)
        self.frames = 0
        self._load()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._writer = None
        if threaded:
            # Two threads (review C-6): flamesafe's status is read on one;
            # the file (fsync), the journal and the audit log are written on
            # the other, so a slow disk never makes the status reading old
            # (which would leave a good shot NOT KNOWN).
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="ltcplay-fireball-charge")
            self._writer = threading.Thread(
                target=self._write_run, daemon=True,
                name="ltcplay-fireball-charge-write")
            self._thread.start()
            self._writer.start()

    # -- the file --------------------------------------------------------
    def _read_file(self, path):
        """(rev, tanks, releasing) from a record file, the exception reading
        it, or None when there is no such file."""
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8-sig") as fh:
                return read_doc(json.load(fh))
        except (OSError, ValueError, TypeError, AttributeError) as e:
            return e

    @staticmethod
    def _said(tanks):
        return ", ".join(
            f"{TANK_WORD[k]} "
            f"{'not known' if tanks[k]['state'] == UNKNOWN else tanks[k]['state']}"
            for k in TANK_WORD)

    def _not_known(self, why, now, fault):
        self.tanks = {k: _new_tank(UNKNOWN, now, why) for k in TANK_WORD}
        self.releasing = ""
        self.fault = fault
        self._event("system", "engine", "unknown", fault, fault=True,
                    persist=False)

    def _next_rev(self):
        """The next revision: above every revision this tracker has seen
        AND above the clock in milliseconds (round-5 review), so a later
        write outranks any earlier one even when the other file could not
        be read at start. A clock set back can only make an older record
        look newer for a while: that costs at most an extra NOT KNOWN,
        never an EMPTY that is not true, because two readable records that
        disagree are never trusted on EMPTY (_load)."""
        try:
            ms = int(self._wall() * 1000)
        except (TypeError, ValueError, OverflowError):
            ms = 0
        self.rev = max(self.rev, ms) + 1
        return self.rev

    def _load(self):
        now = self._wall()
        if not self.path:
            # Memory only (no settings folder given): EMPTY, never kept.
            self.tanks = {k: _new_tank(EMPTY, now, "memory only")
                          for k in TANK_WORD}
            self.releasing = ""
            return
        folder = os.path.dirname(self.path)
        tmp = self.path + ".tmp"
        main = self._read_file(self.path)
        got = self._read_file(tmp)
        self.rev = max([x[0] for x in (main, got) if isinstance(x, tuple)]
                       or [0])
        if main is None and got is None:
            self._first_run(folder, now)
            return
        if isinstance(got, Exception):
            last = (f" {FILE} itself last said: {self._said(main[1])} "
                    f"(revision {main[0]}), but the write that did not "
                    f"finish may have been newer." if isinstance(main, tuple)
                    else "")
            self._not_known(f"{FILE}.tmp could not be read", now,
                            f"A write of {FILE} that did not finish left "
                            f"{FILE}.tmp, which could not be read ({got})."
                            f"{last} Both tanks are NOT KNOWN; no automatic "
                            f"charge runs until a shot, a Purge, or an "
                            f"admin confirms the tanks are empty.")
            return
        if main is None:
            # Only a .tmp, no record beside it (round-5 review, F4: the
            # first run's write left an EMPTY .tmp it could not rename):
            # never trusted on its own.
            self._not_known(f"only {FILE}.tmp was found", now,
                            f"Only {FILE}.tmp was found in {folder}, with no "
                            f"{FILE} beside it (a write that did not "
                            f"finish). Both tanks are NOT KNOWN; no "
                            f"automatic charge runs until a shot, a Purge, "
                            f"or an admin confirms the tanks are empty.")
            return
        if isinstance(main, Exception):
            # Which of the two is newer cannot be told: never the .tmp
            # alone (round-5 review, F2).
            self._not_known(f"{FILE} could not be read", now,
                            f"{FILE} could not be read ({main}). Both tanks "
                            f"are UNKNOWN; no automatic charge runs until a "
                            f"shot, a Purge, or an admin confirms the tanks "
                            f"are empty.")
            return
        src = main
        if isinstance(got, tuple) and got[0] > main[0]:
            # A write that could not finish left the newest record here
            # (review of #185: a restart read the older one and charged
            # again). Two complete records: the higher revision wins.
            src = got
            self._event("system", "engine", "recovered",
                        f"Fireball charge: the newest record was in "
                        f"{FILE}.tmp (a write that could not finish, "
                        f"revision {got[0]}); it is the one used.",
                        fault=True, persist=False)
        _rev, tanks, rel, pending = src
        self.tanks, self.releasing = tanks, rel
        self._durable_rev = _rev
        other = main if src is got else got
        if isinstance(other, tuple):
            # A .tmp is only ever left by a write that failed, so two
            # readable records that disagree are not trusted on EMPTY
            # (round-5 review B: a purge's or a shot's EMPTY left in .tmp
            # by a rename that failed, then a charge whose write failed
            # outright; and a clock set back, which can rank the older
            # record higher): a tank the one used says EMPTY and the other
            # does not is NOT KNOWN.
            odd = [k for k in TANK_WORD if tanks[k]["state"] == EMPTY and
                   other[1][k]["state"] != EMPTY]
            for k in odd:
                self._set(k, UNKNOWN, f"{FILE} and {FILE}.tmp disagree",
                          now)
            if odd:
                self.fault = (f"Fireball charge: {FILE} and {FILE}.tmp (a "
                              f"write that did not finish) disagree on "
                              f"{' and '.join(TANK_WORD[k] for k in odd)}: "
                              f"NOT KNOWN; no automatic charge runs until a "
                              f"shot, a Purge, or an admin confirms the "
                              f"tanks are empty.")
                self._event("system", "engine", "unknown", self.fault,
                            fault=True, persist=True)
        if pending:
            # An automatic charge may have gone out after this record was
            # written: an EMPTY tank here is not known.
            self.pending = True
            self._pending_rev = _rev
            unk = [k for k in TANK_WORD if self.tanks[k]["state"] == EMPTY]
            for k in unk:
                self._set(k, UNKNOWN, "the engine stopped while an automatic "
                                      "charge could have been going out",
                          now)
            if unk:
                self.fault = (f"Fireball charge: the engine stopped while an "
                              f"automatic charge could have been going out. "
                              f"{' and '.join(TANK_WORD[k] for k in unk)} "
                              f"NOT KNOWN; no automatic charge runs until a "
                              f"shot, a Purge, or an admin confirms the "
                              f"tanks are empty.")
                self._event("system", "engine", "unknown", self.fault,
                            fault=True, persist=True)
        if rel:
            what = "valve" if rel == "shot" else "purge"
            for k in TANK_WORD:
                self._set(k, UNKNOWN, f"the engine stopped while the "
                                      f"{what} was open, so whether the "
                                      f"pressure was released is not "
                                      f"known", now)
            self.releasing = ""
            self._event("system", "engine", "unknown",
                        f"Fireball charge: the engine stopped while the "
                        f"{what} was open. Both tanks are UNKNOWN; no "
                        f"automatic charge runs until a shot or a purge.",
                        fault=True, persist=True)

    def _first_run(self, folder, now):
        """No record in the folder: both tanks START EMPTY (Jeff,
        2026-10-30), said loudly with the folder, and the record is written
        at once. One that cannot be written there (round-4 review: a lost
        write and a restart read "no record" while the tanks were charged)
        makes both tanks NOT KNOWN instead."""
        self.tanks = {k: _new_tank(EMPTY, now, "first run: no charge "
                                   "record yet") for k in TANK_WORD}
        self.releasing = ""
        self._next_rev()
        try:
            self._write(self._doc())
            self._durable_rev = self.rev
        except OSError as e:
            self._not_known("the charge record could not be written", now,
                            f"No fireball charge record was found in "
                            f"{folder}, and one could not be written there "
                            f"({e}). Both tanks are NOT KNOWN: check them by "
                            f"eye, then an admin confirms them empty or "
                            f"presses Purge.")
            self._unwritten = self._doc()
            self._retry_at = self._clock()
            return
        self.first_run = True
        self._event("system", "engine", "first_run",
                    f"No charge record found: starting both tanks EMPTY "
                    f"({FILE} in {folder}). If the settings folder was "
                    f"lost, or LTC Player runs under another Windows user, "
                    f"this is not the record from before: check the tanks "
                    f"by eye before a show, and press Purge on the "
                    f"Fireball page if one is pressurised.", fault=True,
                    persist=False)

    def _doc(self):
        return {"fireball_charge_format": FORMAT, "rev": self.rev,
                "tanks": {k: dict(v) for k, v in self.tanks.items()},
                "releasing": self.releasing, "pending": self.pending}

    def _write(self, doc):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    # -- the record ------------------------------------------------------
    def _set(self, tank, state, event, now, by="", charged=False,
             topoff=False):
        """One tank's record changed in memory. Under self._lock (or in
        __init__)."""
        t = self.tanks[tank]
        if t["state"] != state:
            t["since"] = _utc(now)
        t["state"] = state
        t["last_event"] = event
        if charged:
            t["charged_at"] = _utc(now)
            t["by"] = by
        if topoff:
            t["topoffs"] += 1
        if state == EMPTY:
            t["topoffs"] = 0
            t["by"] = ""

    def _event(self, who, screen, outcome, text, fault=False,
               persist=True):
        """Queue a journal line, an audit event and (with `persist`) a
        write of the file, for this module's thread. Never blocks."""
        if persist:
            self._next_rev()
            doc = self._doc()
        else:
            doc = None
        if len(self._events) >= QUEUE_MAX:
            self.dropped += 1
            # The file write is never the one dropped: the last doc wins.
            self._events.append((None, None, None, None, False, doc))
            return
        self._events.append((who, screen, outcome, text, fault, doc))
        w = getattr(self, "_wake", None)
        if w is not None:
            w.set()

    # -- what the Fireball page says ---------------------------------------
    def note_manual(self, on_keys, who="", screen=""):
        """The Fireball page's run now holds `on_keys` ON (fireball.CONTROLS
        keys), pressed by `who`: a charge seen on the wire next is that
        person's. Cheap; never raises."""
        now = self._clock()
        try:
            for k in TANK_WORD:
                if k in on_keys:
                    self._manual[k] = (who or "someone", screen, now)
                else:
                    self._manual.pop(k, None)
            if any(k in on_keys for k in TANK_WORD):
                # The write-ahead mark is asked for, never waited on: the
                # page's presses are never held back by the disk (round-5
                # review), but a restart after this charge then finds it.
                self.charge_ready()
        except Exception:                        # noqa: BLE001
            pass

    def note_continuing(self, tank):
        """The show's own charge of `tank` carries on after a short gap
        (fireball_cues): the edge seen next is that same charge, not a
        double charge. Cheap; never raises."""
        try:
            self._continuing[tank] = self._clock()
        except Exception:                        # noqa: BLE001
            pass

    def _manual_by(self, tank, now):
        m = self._manual.get(tank)
        if m is None or now - m[2] > MANUAL_FRESH_S:
            return None
        return m

    # -- the flame link's side ---------------------------------------------
    def armed_now(self):
        """The fireball group's state, as the thread last read it, or
        "unknown" when that reading is older than STATUS_FRESH_S."""
        at = self._armed_at
        if at is None or self._clock() - at > STATUS_FRESH_S:
            return "unknown"
        return self._armed

    def _read_armed(self):
        try:
            v = self._flames()
        except Exception:                        # noqa: BLE001
            v = None
        self._armed = armed_from_status(v)
        self._armed_at = self._clock()
        try:
            for g in (v or {}).get("groups") or []:
                if str(g.get("name", "")).strip().lower() != GROUP_NAME:
                    continue
                slots, sent = g.get("fire_slots"), g.get("sent_fire")
                if isinstance(slots, list) and slots:
                    self._group_check(slots)
                if self.group_fault:
                    break
                if isinstance(slots, list) and isinstance(sent, list) and \
                        slots and len(slots) == len(sent) and \
                        not v.get("stale"):
                    self._sent_supported = True
                    got = dict(zip(slots, sent))
                    if (got.get(VALVE_SLOT) or 0) > 0 or \
                            (got.get(PURGE_SLOT) or 0) > 0:
                        # Stamped with the status's own time (when flamesafe
                        # made it), not when it was read: one status read
                        # twice never confirms two releases (review C-7).
                        age = v.get("age_ms")
                        at = self._armed_at
                        if isinstance(age, (int, float)) and \
                                not isinstance(age, bool) and age >= 0:
                            at -= age / 1000.0
                        self._sent_release_at = at
                    # flamesafe's own report that it SENT a charge is
                    # evidence whatever the armed state read before
                    # (review of #185: a status up to 0.5 s old saying
                    # disarmed hid a short charge right after arming).
                    for k in TANK_WORD:
                        if (got.get(TANK_SLOT[k]) or 0) > 0:
                            self._sent_charge(k)
        except Exception:                        # noqa: BLE001
            pass
        with self._lock:
            self._finish_release(self._clock())

    def _group_check(self, slots):
        """flamesafe's fireball group's fire slots, from its status: not
        55 to 59, nothing is recorded until they are (said once)."""
        try:
            got = sorted(int(x) for x in slots)
        except (TypeError, ValueError):
            got = None
        bad = got != list(FIRE_SLOTS)
        text = (f"flamesafe's fireball group is on {got if got else slots}, "
                f"not 55 to 59, so the fuel and air charge record cannot "
                f"watch the fireball: both tanks are NOT KNOWN until a "
                f"shot, a Purge, or an admin confirms them empty."
                if bad else "")
        if text != self.group_fault:
            self.group_fault = text
            if text:
                # Both tanks NOT KNOWN, and written (round-5 review, F3):
                # what the page or a show did on the group's own channels
                # meanwhile is not seen, so a group back on 55 to 59 never
                # finds a stale EMPTY.
                now_w = self._wall()
                with self._lock:
                    for k in TANK_WORD:
                        if self.tanks[k]["state"] != UNKNOWN:
                            self._set(k, UNKNOWN, "flamesafe's fireball "
                                      "group was not on 55 to 59", now_w)
                    self._event("system", "engine", "group_fault", text,
                                fault=True, persist=True)

    def observe(self, tc, values, ok):
        """Called on the flame link's sender thread after each frame went
        out (FlameLink.observer). Comparisons in memory only: no disk, no
        journal, no wait on anything slow. Never raises."""
        try:
            vals = bytes(values)
            if len(vals) < PURGE_SLOT:
                return
            self.last_sent = vals if ok else bytes(512)
            self.frames += 1
            if not ok:
                return
            self.see(vals, self.armed_now())
        except Exception:                        # noqa: BLE001
            pass

    def see(self, vals, armed):
        """One frame that went out, with the fireball group `armed`
        ("armed", "off", "unknown"). The transitions in the module's
        docstring."""
        if self.group_fault:
            return          # 55 to 59 are not the fireball (round 4)
        now_w, now = self._wall(), self._clock()
        if armed == "off":
            lv = {k: 0 for k in TANK_WORD}
            valve = purge = 0
        else:
            lv = {k: vals[TANK_SLOT[k] - 1] for k in TANK_WORD}
            valve, purge = vals[VALVE_SLOT - 1], vals[PURGE_SLOT - 1]
        certain = armed == "armed"
        with self._lock:
            rel = self._release
            if (valve or purge) and rel is None and self._ending is not None:
                # Opened again before the last one was confirmed: the
                # same release carries on.
                rel = self._release = self._ending[0]
                self._ending = None
            if valve or purge:
                kind = "purge" if purge else "shot"
                if rel is None:
                    rel = self._release = {"kind": kind, "certain": certain,
                                           "during": {}, "at": now}
                    if self.releasing != kind:
                        self.releasing = kind
                        self._event("system", "engine", "releasing",
                                    f"Fireball: the "
                                    f"{'purge' if kind == 'purge' else 'valve'}"
                                    f" opened{'' if certain else ' (flamesafe status not known)'}.",
                                    persist=True)
                else:
                    rel["certain"] = rel["certain"] or certain
                    if kind == "purge":
                        rel["kind"] = "purge"
            for k in TANK_WORD:
                v = lv[k]
                rising = v and not self._prev[k]
                # The value on the wire, certain or not (review F3): a charge
                # that went on while flamesafe's status was not known and is
                # still on is the same charge, never a new edge (a false
                # double charge once the status is back).
                self._prev[k] = v
                if not v:
                    if rel is not None and (valve or purge):
                        # Off again while the valve or the purge is still
                        # open: released with it (round-4 review, s90).
                        rel["during"].pop(k, None)
                    continue
                # A release still open, or closed and not yet confirmed
                # (review of #185): a charge seen meanwhile is not wiped
                # when the release is counted.
                tgt = rel if rel is not None else (
                    self._ending[0] if self._ending is not None else None)
                if tgt is not None:
                    tgt["during"][k] = CHARGED if certain or \
                        tgt["during"].get(k) == CHARGED else UNKNOWN
                self._charge(k, certain, rising, now_w, now)
            if rel is not None and not (valve or purge):
                self._release = None
                self._ending = (rel, now)
            self._finish_release(now)

    def _finish_release(self, now):
        """A closed release counted once flamesafe has reported sending it
        (or at once when its status carries no sent_fire); after CONFIRM_S
        with no such report, counted as not certain (UNKNOWN). Under
        self._lock."""
        end = self._ending
        if end is None:
            return
        rel, closed = end
        seen = self._sent_release_at
        if self._sent_supported is not True:
            confirmed = True          # nothing more to wait for
        else:
            confirmed = seen is not None and seen >= rel.get("at", 0) - 0.1
        if confirmed or now - closed > CONFIRM_S:
            self._ending = None
            if not confirmed:
                rel = dict(rel, certain=False, unreported=True)
            self._released(rel, self._wall())

    def _sent_charge(self, k):
        """flamesafe reported sending tank k's charge: CHARGED if it is not
        already (said once, credited to whoever holds it on the Fireball
        page, else the show)."""
        with self._lock:
            if self.tanks[k]["state"] == CHARGED:
                return
            now_w, now = self._wall(), self._clock()
            tgt = self._release if self._release is not None else (
                self._ending[0] if self._ending is not None else None)
            # A charge that ended while the valve was still open was
            # released with it (round-4 review, s90): only one still on
            # (or on after it closed) counts.
            if tgt is not None and (tgt is not self._release or
                                    self._prev[k]):
                tgt["during"][k] = CHARGED
            # The charge on the wire now is this one: frames read under a
            # disarmed status just before (arming lag) are no new edge
            # (round-4 review, s62: a false double charge or top-off).
            self._prev[k] = self._prev[k] or 255
            m = self._manual_by(k, now)
            by = m[0] if m is not None else "the show"
            self._set(k, CHARGED, f"charged by {by} (flamesafe reported "
                      f"sending it)", now_w, by=by, charged=True)
            self._event(m[0] if m else "system", m[1] if m else "engine",
                        "charged", f"Fireball charge: {TANK_WORD[k]} "
                        f"CHARGED by {by} (flamesafe reported sending it).")

    def charge_ready(self):
        """Asked by the flame cues before an AUTOMATIC charge (a timeline's
        Fuel or Air stage, an FSEQ's guarded 55 or 56) goes out (round-5
        review, write-ahead): "ok" once a record saying "an automatic
        charge may be going out" (pending) is on disk; "wait" while that
        write is on its way (the first time: it is queued now); "failed"
        when it could not be written: the automatic charge is not sent.
        The Fireball page's presses never ask. Memory only: "ok". Cheap,
        on the sender thread; never raises."""
        if not self.path:
            return "ok"
        try:
            with self._lock:
                if not self.pending:
                    self.pending = True
                    self._event("system", "engine", "pending", None,
                                persist=True)
                    self._pending_rev = self.rev
                    return "wait"
                pr = self._pending_rev if self._pending_rev is not None \
                    else self.rev
                if self._durable_rev >= pr:
                    return "ok"
                if self._failed_rev is not None and self._failed_rev >= pr:
                    return "failed"
                return "wait"
        except Exception:                        # noqa: BLE001
            return "failed"

    def confirm_empty(self, who, screen=""):
        """An admin says both tanks are known to be empty (no record, or
        NOT KNOWN): those tanks become EMPTY, journaled and audited with
        who. A CHARGED tank is not changed by this: fire it or Purge.
        (ok, sentence)."""
        with self._lock:
            unk = [k for k in TANK_WORD if self.tanks[k]["state"] == UNKNOWN]
            if not unk:
                return False, ("No tank is NOT KNOWN, so there is nothing "
                               "to confirm. A charged tank is emptied by the "
                               "shot or by Purge.")
            now_w = self._wall()
            for k in unk:
                self._set(k, EMPTY, f"confirmed empty by {who}", now_w)
            self.pending = False
            words = " and ".join(TANK_WORD[k] for k in unk)
            text = (f"Fireball charge: {words} confirmed EMPTY by "
                    f"{who or 'someone'} on the {screen or 'engine'}.")
            self._event(who or "someone", screen or "engine",
                        "confirmed_empty", text)
            if self.fault and not any(
                    self.tanks[k]["state"] == UNKNOWN for k in TANK_WORD):
                self.fault = ""
        return True, text

    def _charge(self, k, certain, rising, now_w, now):
        word = TANK_WORD[k]
        t = self.tanks[k]
        m = self._manual_by(k, now)
        if not certain:
            if t["state"] == EMPTY:
                self._set(k, UNKNOWN, f"{word} Charge was sent while "
                          f"flamesafe's status was not known", now_w)
                self._event("system", "engine", "unknown",
                            f"Fireball charge: {word} Charge was sent "
                            f"while flamesafe's status was not known; the "
                            f"{word.lower()} tank is UNKNOWN.", fault=True)
            return
        if t["state"] == CHARGED:
            if not rising:
                return      # the same charge, still on
            c = self._continuing.pop(k, None)
            if c is not None and 0 <= now - c <= MANUAL_FRESH_S:
                return      # the show's same charge after a short gap
            if m is not None:
                self._set(k, CHARGED, f"top-off by {m[0]}", now_w,
                          topoff=True)
                self._event(m[0], m[1], "topoff",
                            f"Top-off by {m[0]} ({word.lower()} already "
                            f"charged; top-off {t['topoffs']} since the "
                            f"charge at {t['charged_at']}).")
            else:
                self._set(k, CHARGED, f"{word} Charge on again while "
                          f"already charged, not from the Fireball page",
                          now_w, topoff=True)
                self._event("system", "engine", "double_charge",
                            f"Fireball charge: {word} Charge went ON again "
                            f"while the {word.lower()} tank was already "
                            f"charged, and not from the Fireball page.",
                            fault=True)
            return
        by = m[0] if m is not None else "the show"
        self._set(k, CHARGED, f"charged by {by}", now_w, by=by,
                  charged=True)
        self._event(m[0] if m else "system", m[1] if m else "engine",
                    "charged", f"Fireball charge: {word} CHARGED by {by}.")

    def _released(self, rel, now_w):
        what = "the shot was fired (Valve Open)" if rel["kind"] == "shot" \
            else "purged"
        self.releasing = ""
        # The write-ahead mark ends with the release: the next automatic
        # charge writes it again first.
        self.pending = False
        for k in TANK_WORD:
            during = rel["during"].get(k)
            if during is not None:
                self._set(k, during, f"{what}, but {TANK_WORD[k]} Charge "
                          f"was on meanwhile", now_w, charged=during ==
                          CHARGED, by="the show" if during == CHARGED else "")
            elif rel["certain"]:
                self._set(k, EMPTY, what, now_w)
            elif self.tanks[k]["state"] != EMPTY:
                self._set(k, UNKNOWN, f"{what}, but flamesafe's status was "
                          f"not known", now_w)
        why = "" if rel["certain"] else (
            " (flamesafe did not report sending it)" if rel.get("unreported")
            else " (flamesafe status not known)")
        self._event("system", "engine", rel["kind"],
                    f"Fireball charge: {what}{why}. {self.sentence()}",
                    fault=not rel["certain"])

    # -- views ---------------------------------------------------------------
    def state(self, tank):
        return self.tanks[tank]["state"]

    def _clock_text(self, iso):
        try:
            t = datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
            t = t.replace(tzinfo=datetime.timezone.utc).astimezone()
            return t.strftime("%H:%M")
        except (ValueError, TypeError):
            return ""

    def sentence(self):
        """'Fuel: charged 14:32. Air: charged 14:35. No shot fired.' or
        'Fuel: empty. Air: empty.'"""
        parts = []
        for k in TANK_WORD:
            t = self.tanks[k]
            if t["state"] == CHARGED:
                at = self._clock_text(t["charged_at"])
                top = (f", topped off {t['topoffs']} time"
                       f"{'s' if t['topoffs'] != 1 else ''}"
                       if t["topoffs"] else "")
                parts.append(f"{TANK_WORD[k]}: charged{' ' + at if at else ''}"
                             f"{top}.")
            elif t["state"] == UNKNOWN:
                parts.append(f"{TANK_WORD[k]}: not known.")
            else:
                parts.append(f"{TANK_WORD[k]}: empty.")
        if any(self.tanks[k]["state"] != EMPTY for k in TANK_WORD):
            parts.append("No shot fired." if all(
                self.tanks[k]["state"] != UNKNOWN for k in TANK_WORD) else
                "Purge or fire to reset.")
        return " ".join(parts)

    def view(self):
        with self._lock:
            tanks = {k: dict(v, word=TANK_WORD[k], slot=TANK_SLOT[k])
                     for k, v in self.tanks.items()}
            text = self.sentence()
            releasing = self.releasing
        return {"tanks": tanks, "text": text, "releasing": releasing,
                "fault": " ".join(x for x in (self.fault, self.write_fault,
                                              self.group_fault) if x),
                "first_run": self.first_run, "rev": self.rev,
                "pending": self.pending,
                # A slot for live pressure later (Jeff): never invented.
                "psi": None}

    # -- this module's thread ------------------------------------------------
    def pump(self):
        """Write what is queued: the file (the last doc), then each
        journal line and audit event. Returns how many events. One
        writer at a time."""
        with self._wlock:
            return self._pump()

    def _pump(self):
        n = 0
        doc = None
        while self._events:
            try:
                who, screen, outcome, text, fault, d = self._events.popleft()
            except IndexError:
                break
            n += 1
            if d is not None:
                doc = d
            if text is not None:
                if doc is not None:
                    self._flush(doc)
                    doc = None
                self._say(who, screen, outcome, text, fault)
        if doc is not None:
            self._flush(doc)
        elif self._unwritten is not None and \
                self._clock() - self._retry_at >= RETRY_S:
            self._flush(self._unwritten)
        if self.dropped:
            d, self.dropped = self.dropped, 0
            self._say("system", "engine", "dropped",
                      f"Fireball charge: {d} journal line(s) were dropped "
                      f"(the record itself was still written).", True)
        return n

    def _flush(self, doc):
        try:
            self._write(doc)
            self._durable_rev = max(self._durable_rev, doc["rev"])
            self._unwritten = None
            if self.write_fault:
                self.write_fault = ""
                self._say("system", "engine", "written",
                          f"Fireball charge: {FILE} written again.", False)
        except OSError as e:
            # Kept, and written again every RETRY_S until it is: a
            # restart must never read an older record (review of #185).
            self._failed_rev = doc["rev"]
            self._unwritten = doc
            self._retry_at = self._clock()
            text = (f"{FILE} could not be written ({e}); the charge state "
                    f"is kept in memory and written again every "
                    f"{RETRY_S:g} s until it is. Do not restart LTC Player "
                    f"until this clears.")
            if text != self.write_fault:
                self.write_fault = text
                self._say("system", "engine", "write_failed", text, True)

    def _say(self, who, screen, outcome, text, fault):
        if self._journal is not None:
            try:
                self._journal(who, screen, outcome, text, fault=fault)
            except Exception:                    # noqa: BLE001
                pass
        log = self.audit
        if log is not None:
            try:
                from . import audit as audit_mod
                log.event(user=audit_mod.SYSTEM if who in ("", "system")
                          else who, surface="engine",
                          action="fireball charge", target=outcome,
                          result="error" if fault else "accepted",
                          reason=text, source=screen or "engine")
            except Exception:                    # noqa: BLE001
                pass

    def _run(self):
        """flamesafe's status, every WATCH_S. Never writes anything."""
        while not self._stop.wait(WATCH_S):
            try:
                self._read_armed()
            except Exception:                    # noqa: BLE001
                pass

    def _write_run(self):
        """The file, the journal and the audit log: at once when told, and
        every WATCH_S for the retry timer."""
        while not self._stop.is_set():
            try:
                self.pump()
            except Exception:                    # noqa: BLE001
                pass
            self._wake.wait(WATCH_S)
            self._wake.clear()

    def close(self):
        """Stop the threads, then one last write of what is queued, on its
        own thread and given CLOSE_S at most: a hung disk never hangs the
        engine's stop, and never two writers at once (round-4 review)."""
        self._stop.set()
        self._wake.set()
        for t in (self._thread, self._writer):
            if t is not None and t is not threading.current_thread():
                t.join(2.0)

        def last():
            try:
                with self._wlock:
                    self._pump()
                    if self._unwritten is not None:
                        # One last try now, whatever the retry timer says.
                        self._flush(self._unwritten)
            except Exception:                    # noqa: BLE001
                pass
        t = threading.Thread(target=last, daemon=True,
                             name="ltcplay-fireball-charge-close")
        t.start()
        t.join(CLOSE_S)
