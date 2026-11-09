"""Fireball practice (Jeff, 2026-11-06 to 2026-11-08; design rev 5).

"Being able to run through the triggers and confirm its timing (with
actual firing of the flame to test timing)."

What it is
==========

On the Programming screen's playhead (playhead.py), never as a show: the
schedule is not touched by a step (no slot, no show count, no Hold
consumed). Open practice presses Stop: every group and laser disarmed,
the schedule on a Stop's Hold (only Resume releases it, and Resume is
refused while practice is open). The fireball is then armed by hand, once
(hold to arm). Each Step plays the show's one fireball shot (the timeline:
Fuel, Air, Igniter and Valve Open, each at most once) from a lead-in
before it to 2 s after it, then stops the playback (Programming's Follow:
flame cues zero, nothing disarmed), so the next Step fires the same shot
again without arming again.

Every frame still goes the show's own way: FlameCues, FireballCues (with
a practice play's stricter rules: fireball_cues.py), the flame link and
flamesafe, which fires only an armed group. Practice writes nothing to
the flame link. It only refuses more, and ends sooner.

A step is refused (nothing sent) unless, checked at the press and again
under the playhead's move lock right before the play starts:

  - Programming runs on the show audio clock and the show's fireball is
    the timeline, read clean, through the very plan the flame cues hold;
  - the shot is whole: Valve Open with an Igniter lit when it opens
    (REFUSE_VALVE_UNLIT), no charge without Valve Open, the lead-in
    LEAD_IN_MIN_S to LEAD_IN_MAX_S, the start past the cue's top and the
    end inside the cue;
  - both tanks EMPTY, no charge mark pending, no charge fault;
  - Live: the fireball armed AFTER practice opened, and nothing else;
    Dry: nothing armed at all; no laser armed, no hazer on, no spoof
    ALARM from the Stream Deck, no second sender or flood on either link
    (the Stream Deck alert, unplugged or failing, is only noted);
  - the schedule on the Stop's Hold (HOLD with stop_hold) or OFF with no
    show waiting, the same night and night load as when it opened;
  - the press from the device and window that opened practice, with the
    Show and Arm rows (any screen; with sign-in off, the show PC).

What ends practice: Done; Stop or any disarm; any arm change of any
group or laser; a hazer; a lockout of the fireball; the Stream Deck's
spoof ALARM; a second sender or flood; flamesafe not reporting for
FLAMESAFE_STALE_S; the flame link not sending; a fault in the flame
cues, the fireball timeline or the charge record; a play practice did not
bind; a Hold or a pause during a step; the step overrunning; the schedule
leaving the Stop's Hold, a show starting, the night loaded again; Run off
or another show file; the opening screen silent for PAGE_BEAT_OFF_S;
IDLE_OFF_S with no step; the watcher or the watchdog stopping. Ending is
a Stop (every group and laser disarmed). A Stop that does not complete
keeps practice ENDING: the flames alert, and a new Stop every RETRY_S.
Only NOTED (a journal line and the card), never an end: the Stream Deck
unplugged (its alert).

Fuel: every charge and shot of a step goes out on the flame link like a
show's, so the charge record (and its double-charge guard) counts it as
it counts a show's, and the fuel tracker books it as it books any
Programming playback: in the "other" pile, which the totals include,
never as a show.

Every change of state is a compare-and-set under the lock, tied to the
practice's generation (one per open), so an end that lands while open,
Step or a step's end is under way is never undone by it: a step that
finds practice ended while its play was starting presses Stop again.

Two threads run while practice is open: the watcher (every WATCH_S: the
end triggers, the step's end, the timing record) and the watchdog (every
WATCHDOG_S, no lock, never the playhead's move lock), which presses Stop
when the watcher's beat is older than BEAT_STALE_S. Each reads the
other's beat.

The timing record: the flame link hands each frame's own mono time, its
timecode and the show part's values to on_sent (under its lock, then
called after the frame went out); the clock's heard epoch is read with
it. Per stage: programmed ms from the top of the show cue, sent ms on the
show audio, late = sent - programmed. Software cannot see flamesafe's
tick, the igniter, the valve or the gas: TIMING_NOTE says so on the card.
"""
import collections
import itertools
import os
import threading
import time

from . import fireball_cues as fbc
from . import fireball_timeline as fbtl

NAME = "Fireball practice"

# -- Jeff's answers (2026-11-08) -------------------------------------------
# No least time between two shots: Andy sees to that himself.
# Open and Step: anyone with the Show and Arm rows, on any screen
# (operators on an iPad too; remote._practice_may). The narrower rule for
# moving Programming playback (the show PC or an admin) stays for every
# other move, which practice refuses anyway.
# The Stream Deck unplugged (its alert) is NOTED, never an end: an
# unplugged deck must not disarm the flames by itself (ruling 4). Its spoof
# ALARM does end practice.
DECK_ALERT_ENDS = False
# A step is not a show (no slot, no show count). Its frames go out
# through the flame link, whose observers are never touched here: the
# charge record counts every charge and shot as it counts a show's, and
# the fuel tracker books the flame time as Programming ("other"), which
# the fuel totals include.
COUNTS_AS_SHOW = False

LEAD_IN_MIN_S = 4
LEAD_IN_DEFAULT_S = 5
LEAD_IN_MAX_S = 30
TAIL_S = 2.0
FPS = fbtl.FPS
WATCH_S = 0.05
WATCHDOG_S = 0.25
BEAT_STALE_S = 0.5
PAGE_BEAT_OFF_S = 5.0
IDLE_OFF_S = 600.0
OVERRUN_S = 5.0
FLAMESAFE_STALE_S = 1.0
RETRY_S = 1.0
SEND_PERIOD_MS = 25.0               # the flame link's 40 Hz
TIMER_JITTER_MS = 16.0              # one Windows timer tick
LATE_OK_MS = SEND_PERIOD_MS + TIMER_JITTER_MS
EARLY_OK_MS = 5.0
RESULTS_KEPT = 10
# A Step press holds "starting" at most this long (its play start waits
# for the show audio); longer, and practice ends.
STARTING_MAX_S = 30.0
RECORD_MAX = 20000
GROUP = "fireball"
SLOTS = fbtl.FIREBALL_FIRE_SLOTS
STAGE_ORDER = fbtl.STAGE_KEYS

TIMING_NOTE = ("This is when LTC Player sent each stage to flamesafe, "
               "measured against the show audio. flamesafe adds up to "
               "25 ms, and the igniter, valve and gas add their own delay. "
               "No software can see that.")
REFUSE_VALVE_UNLIT = ("This step is refused: Valve Open has no Igniter lit "
                      "when it opens. Tick Igniter, start it at least 3 "
                      "frames (0.1 s) before Valve Open, and keep it on "
                      "until Valve Open starts. Nothing was sent.")
REFUSE_CHARGE_NO_VALVE = ("This step is refused: it charges a tank but never "
                          "opens the valve. Nothing was sent.")
REFUSE_TANKS = "Tanks charged: Purge on the Fireball page before the next step."
# The charge record's mark (an automatic charge may be going out) is
# cleared by a shot, a Purge, or PENDING_IDLE_S (5 s) with no charge asked:
# after a dry step, or a step whose shot was skipped, that wait.
REFUSE_SETTLING = ("The charge record is settling after the last step: Step "
                   "again in a few seconds.")
DECK_NOTE = ("the Stream Deck alert is open (unplugged or failing). "
             "Practice carries on and nothing is disarmed for it; Stop, "
             "Hold and Done still work on every screen.")
FUEL_NOTE = ("Every charge and shot of a step is recorded in the fuel and "
             "air charge record like a show's (never a double charge), and "
             "its flame time is in the fuel totals as Programming (Other). "
             "A step is not a show.")
OPEN_SAYS = ("Fireball practice opened: Stop was pressed, so every group "
             "and laser is disarmed and the schedule holds until Resume.")
REFUSED_WHILE_OPEN = "Fireball practice is open: press Done first."


class Refused(ValueError):
    """A step or an open that is refused. str() is the sentence."""


def _step_refused(why):
    return Refused(f"This step is refused: {why} Nothing was sent.")


def plan_step(entry, cue_start_s, cue_len_s, lead_in_s):
    """The step for one show's timeline `entry` in a cue that starts at
    show second `cue_start_s` and is `cue_len_s` long: {"stages": {k: (s0,
    s1)} in frames from the cue's top, "start_f", "end_f" (cue frames),
    "start_show_s", "end_show_s", "lead_in_s"}. Refused with the sentence
    otherwise. Pure."""
    if not isinstance(lead_in_s, int) or isinstance(lead_in_s, bool) or \
            not LEAD_IN_MIN_S <= lead_in_s <= LEAD_IN_MAX_S:
        raise _step_refused(f"the lead-in is whole seconds, "
                            f"{LEAD_IN_MIN_S} to {LEAD_IN_MAX_S}.")
    if not entry or entry.get("source") != "timeline":
        raise _step_refused("this show's fireball is not the timeline (it "
                            "comes from the FSEQ). Practice steps only a "
                            "timeline shot.")
    stages = {}
    for st in entry.get("stages") or []:
        s0 = fbtl.parse_tc(st["start"])
        stages[st["stage"]] = (s0, s0 + float(st["dur_s"]) * FPS)
    if not stages:
        raise _step_refused("the fireball timeline has no stage ticked.")
    va, ig = stages.get("valve"), stages.get("igniter")
    if va is not None and (ig is None or not (
            ig[0] + fbtl.LEAD_FRAMES <= va[0] < ig[1])):
        raise Refused(REFUSE_VALVE_UNLIT)
    if va is None and ("fuel" in stages or "air" in stages):
        raise Refused(REFUSE_CHARGE_NO_VALVE)
    e0 = min(s0 for s0, _s1 in stages.values())
    e1 = max(s1 for _s0, s1 in stages.values())
    start_f = e0 - lead_in_s * FPS
    if start_f <= fbc.TOP_FRAMES:
        raise _step_refused(f"the shot starts too close to the top of the "
                            f"show for a {lead_in_s} s lead-in.")
    end_f = e1 + TAIL_S * FPS
    if cue_len_s is None or end_f / FPS > float(cue_len_s):
        raise _step_refused("the shot and its 2 s after it do not fit "
                            "inside the show cue.")
    return {"stages": stages, "start_f": start_f, "end_f": end_f,
            "start_show_s": float(cue_start_s) + start_f / FPS,
            "end_show_s": float(cue_start_s) + end_f / FPS,
            "lead_in_s": lead_in_s}


def stage_rows(stages, edges, skipped, against_audio, armed):
    """The table: one row per stage of the step, in Jeff's order. `edges`
    {k: {"on": sent ms or None, "off": sent ms or None}}, `skipped` {k:
    why}, `armed` whether flamesafe said the fireball was armed while it
    went out. Pure."""
    rows = []
    for k in STAGE_ORDER:
        if k not in stages:
            continue
        s0, s1 = stages[k]
        prog = s0 * 1000.0 / FPS
        plen = (s1 - s0) * 1000.0 / FPS
        e = edges.get(k) or {}
        on, off = e.get("on"), e.get("off")
        row = {"stage": k, "word": fbtl.STAGE_WORD[k],
               "programmed_tc": fbtl.tc_text(s0),
               "programmed_ms": round(prog, 1),
               "length_ms": round(plen, 1), "sent_ms": None,
               "late_ms": None, "length_sent_ms": None, "flag": False}
        if on is None:
            why = skipped.get(k)
            row["result"] = f"Skipped: {why}" if why else "Not sent"
            row["flag"] = True
            rows.append(row)
            continue
        late = on - prog
        row["sent_ms"] = round(on, 1)
        row["late_ms"] = round(late, 1)
        if off is not None:
            row["length_sent_ms"] = round(off - on, 1)
        if late > LATE_OK_MS:
            row["result"], row["flag"] = f"LATE {late:.0f} ms", True
        elif late < -EARLY_OK_MS:
            row["result"], row["flag"] = f"EARLY {-late:.0f} ms", True
        else:
            row["result"] = "OK"
        if not armed:
            row["result"] = "Sent, group not armed: nothing fired"
        if not against_audio:
            row["result"] += " (not against the show audio)"
        rows.append(row)
    return rows


class Practice:
    """The engine side. `remote` is remote.Remote: its playhead, schedule,
    flame status, fireball cues and charge record, morning (lockout,
    lasers), hazers, alerts and stop_press are read through it, every one
    with getattr so a part this engine lacks refuses rather than raises."""

    def __init__(self, remote, link_get=lambda: None, journal=None,
                 clock=time.monotonic, threaded=True):
        self.remote = remote
        self._link = link_get
        self._journal = journal
        self._clock = clock
        self._threaded = threaded
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.state = "closed"   # closed, opening, arming, ready, starting,
        #                         step, ending
        self.mode = "live"
        self.owner = None
        self.who = ""
        self.screen = ""
        self.ended_why = ""
        self.said = ""
        self.results = collections.deque(maxlen=RESULTS_KEPT)
        self._base = None
        self._seen_off = False
        self._sched_reached = False
        self._step = None
        self._end_asked = ""
        self._retry_at = None
        self._alerted = False
        self._stopping = False
        self._deck_alarm = ""
        self.deck_note = ""
        self.last_beat = None
        self.last_activity = None
        self.watcher_beat = None
        self.watchdog_beat = None
        self._rec = collections.deque(maxlen=RECORD_MAX)
        self._recording = False
        self._rec_clock = None
        self._threads_gen = 0
        self._gen = 0            # one per open: every change is tied to it
        self._stop_seq = 0
        self._starting_at = None

    def _cas(self, frm, to, gen=None):
        """Compare and set: the state becomes `to` only while it is one of
        `frm` and (with `gen`) practice is still the one that was opened
        then. True when it changed."""
        with self._lock:
            if self.state not in frm or (gen is not None and
                                         gen != self._gen):
                return False
            self.state = to
            return True

    # -- what other parts ask, lock-free ---------------------------------------
    @property
    def blocking(self):
        return self.state != "closed"

    def refusal(self):
        """The veto's sentence while practice is open, else "". One
        attribute read: no lock, no move lock."""
        return REFUSED_WHILE_OPEN if self.state != "closed" else ""

    def deck_event(self, action, text, fault):
        """A line the Stream Deck posted (schedule_service.deck_event): its
        spoof ALARM ends practice (the watcher, within WATCH_S). No lock."""
        if self.state != "closed" and action == "spoof-alarm" and fault:
            self._deck_alarm = str(text or "")[:200]

    def deck_alert(self):
        """True while the Stream Deck alert (unplugged, failing) is open."""
        try:
            al = getattr(self.remote, "alerts", None)
            return al is not None and any(
                getattr(a, "kind", "") == "deck" for a in al.open())
        except Exception:                        # noqa: BLE001
            return False

    def _deck_tick(self):
        """The deck alert is noted once when it opens and once when it
        clears: practice carries on, nothing disarms (ruling 4)."""
        on = self.deck_alert()
        if on and not self.deck_note:
            self.deck_note = DECK_NOTE
            self._note(f"Fireball practice: {DECK_NOTE}", "deck")
        elif not on and self.deck_note:
            self.deck_note = ""
            self._note("Fireball practice: the Stream Deck is back.",
                       "deck")

    def request_end(self, why):
        """Asked by a press that must end practice (a Hold, Follow): the
        watcher ends it within WATCH_S. No lock."""
        if self.state != "closed":
            self._end_asked = str(why or "it was asked to end")

    # -- plumbing ---------------------------------------------------------------
    def _note(self, text, outcome="done", fault=False):
        if self._journal is not None:
            try:
                self._journal(text, outcome=outcome, who=self.who,
                              screen=self.screen, fault=fault)
            except Exception:                    # noqa: BLE001
                pass

    def _cues(self):
        return getattr(self.remote, "fireball_cues", None)

    def _charge(self):
        return getattr(self.remote, "fireball_charge", None)

    def _ph(self):
        return getattr(self.remote, "playhead", None)

    def _sched(self):
        svc = getattr(self.remote, "schedule", None)
        return svc, getattr(svc, "machine", None)

    def _session(self):
        s = getattr(getattr(self.remote, "control", None), "session", None)
        return s if s is not None and getattr(s, "running", False) else None

    def _flames(self):
        fs = getattr(self.remote, "flame_status", None)
        try:
            return (fs.view() if fs is not None else None), \
                (getattr(fs, "last", None) or {})
        except Exception:                        # noqa: BLE001
            return None, {}

    # -- the readings both checks use -----------------------------------------
    def _armed(self, view):
        """(fireball armed, other groups armed) from flamesafe's status
        view: ARMED only. A group still wanted after the opening Stop reads
        "held" (cycle the arm) and needs a cycle, which is an ARMED edge
        seen here."""
        fb = False
        others = []
        for g in (view or {}).get("groups") or []:
            name = str(g.get("name", ""))
            on = g.get("armed") == "armed"
            if name.strip().lower() == GROUP:
                fb = on
            elif on:
                others.append(name)
        return fb, others

    def _flamesafe_why(self, view, raw):
        if not view or view.get("stale") or not view.get("connected"):
            return "flamesafe has not reported"
        age = view.get("age_ms")
        if not isinstance(age, (int, float)) or \
                age > FLAMESAFE_STALE_S * 1000:
            return (f"flamesafe has not reported for more than "
                    f"{FLAMESAFE_STALE_S:g} s")
        ai = raw.get("arm_input") if isinstance(raw.get("arm_input"),
                                                dict) else {}
        fr = raw.get("frames") if isinstance(raw.get("frames"), dict) else {}
        if ai.get("foreign_senders") or ai.get("flooded"):
            return "another sender is on the arm link"
        if fr.get("foreign_senders") or fr.get("new_sender") or \
                fr.get("flooded"):
            return "another sender is on the flame link"
        return ""

    def _schedule_ok(self, m):
        """Exactly: HOLD with a Stop's Hold, or OFF with nothing waiting."""
        if m is None:
            return False
        st = getattr(m, "state", None)
        if st == "HOLD":
            return bool(getattr(m, "stop_hold", False))
        if st == "OFF":
            try:
                return not m.waiting()
            except Exception:                    # noqa: BLE001
                return False
        return False

    def _schedule_settling(self, m):
        """On the way to OFF right after the opening Stop with nothing left
        tonight (STANDBY, IDLE or CLOSING with nothing waiting): no show
        can come due, but Step waits for OFF."""
        try:
            return m is not None and m.state in ("STANDBY", "IDLE",
                                                 "CLOSING") and \
                not m.waiting()
        except Exception:                        # noqa: BLE001
            return False

    def _common_why(self, in_step=False):
        """Why practice may not carry on right now, or "": everything the
        watcher ends it for and a step is refused for, but the arming and
        the page."""
        if self._end_asked:
            return self._end_asked
        cues = self._cues()
        if cues is None:
            return "the fireball cues are not running"
        if getattr(cues, "practice_fault", ""):
            return f"the flame cues refused a play: {cues.practice_fault}"
        if getattr(cues, "fault", ""):
            return f"the fireball timeline has a fault: {cues.fault}"
        svc, m = self._sched()
        if svc is None or m is None:
            return "the schedule is not loaded"
        b = self._base or {}
        if (getattr(m, "date", None), getattr(svc, "night_loads", None)) != \
                b.get("night"):
            return ("the schedule's night was loaded again (a save, a "
                    "restart or the nightly hand over)")
        if getattr(m, "state", None) in ("SHOW", "PAUSED"):
            return "a show started"
        if self._schedule_ok(m):
            self._sched_reached = True
        elif self._sched_reached or not self._schedule_settling(m):
            return "the schedule left the Stop's Hold"
        s = self._session()
        if s is None or s is not b.get("session"):
            return "Run was turned off or another show file was chosen"
        view, raw = self._flames()
        why = self._flamesafe_why(view, raw)
        if why:
            return why
        link = self._link()
        if link is None:
            return "there is no flame link"
        if getattr(link, "abort_id", None) != b.get("abort_id"):
            return "every group was disarmed (Stop or Disarm all)"
        try:
            sender = link.sender_state()
        except Exception:                        # noqa: BLE001
            sender = "unknown"
        if sender != "running":
            return f"the flame link is not sending ({sender})"
        if getattr(link, "lock_alarm", ""):
            return f"the flame link has an alarm: {link.lock_alarm}"
        if in_step:
            fc = getattr(link, "cues", None)
            if getattr(fc, "fault", ""):
                return f"CUES FAULT: {fc.fault}"
        fb, others = self._armed(view)
        if others:
            return f"another flame group was armed ({', '.join(others)})"
        if self.mode == "dry" and fb:
            return "the fireball was armed during a dry run"
        if self.mode == "live" and self.state in ("ready", "starting",
                                                  "step") and not fb:
            return "the fireball was disarmed"
        m2 = getattr(self.remote, "morning", None)
        try:
            locked = set(m2.lockout.channels(m2.cfg)) | set(
                m2.lockout.file_channels or ()) if m2 is not None else set()
        except Exception:                        # noqa: BLE001
            return "the lockout could not be read"
        if locked & set(SLOTS[:4]):
            return "the fireball is locked out"
        try:
            la = getattr(m2, "laser_arm", None)
            if la is not None and la.armed_ids():
                return "a laser was armed"
        except Exception:                        # noqa: BLE001
            return "the laser arm could not be read"
        try:
            hz = getattr(self.remote, "hazers", None)
            if hz is not None and hz.on():
                return "a hazer was switched on"
        except Exception:                        # noqa: BLE001
            return "the hazers could not be read"
        if self._deck_alarm:
            return f"the Stream Deck's spoof ALARM: {self._deck_alarm}"
        if DECK_ALERT_ENDS and self.deck_alert():
            return "the Stream Deck alert is open"
        tr = self._charge()
        if tr is None:
            return "the fireball charge record is not running"
        try:
            v = tr.view()
        except Exception:                        # noqa: BLE001
            return "the fireball charge record could not be read"
        if v.get("fault") or v.get("auto_off"):
            return (f"the fireball charge record has a fault: "
                    f"{v.get('fault') or 'automatic charging is off'}")
        if any(t.get("state") == "unknown"
               for t in (v.get("tanks") or {}).values()):
            return "a fireball tank is not known"
        return ""

    # -- open, step, done, beat -------------------------------------------------
    def open(self, mode, owner, who, screen):
        """Open practice: Stop first (every group and laser disarmed, the
        schedule on the Stop's Hold). (ok, sentence)."""
        if mode not in ("live", "dry"):
            raise Refused("Practice is Live (real flame) or Dry (nothing "
                          "armed). Nothing was changed.")
        with self._lock:
            if self.state != "closed":
                raise Refused("Fireball practice is already open.")
            self.state = "opening"
            self._gen += 1
            gen = self._gen
            # Reset here, at the claim: a Hold or Follow asked while the
            # opening Stop runs is kept and ends practice.
            self._end_asked = ""
            self._deck_alarm = ""
            self._stopping = False
            self._retry_at = None
        try:
            why = self._open_why()
        except Exception as e:                   # noqa: BLE001
            why = f"the engine could not be read ({type(e).__name__})"
        if why:
            self._cas(("opening",), "closed", gen)
            raise Refused(f"Fireball practice was not opened: {why}. "
                          f"Nothing was changed.")
        cues = self._cues()
        with self._lock:
            if self.state != "opening" or gen != self._gen:
                # Ended (Done) while the engine was checked: it stays
                # ended, and nothing of a later open is touched.
                return (f"Fireball practice ended while it was opening: "
                        f"{self.ended_why or 'it was ended'}.")
            self.mode, self.owner, self.who, self.screen = mode, owner, \
                who, screen
            self.ended_why = ""
            self._alerted = False
            self.deck_note = ""
            cues.practice = None
            cues.practice_fault = ""
            cues.practice_snap = None
            cues.practice_open = True
        code, out = self._stop(f"{who or 'someone'} opened Fireball practice")
        svc, m = self._sched()
        link = self._link()
        base = {"abort_id": getattr(link, "abort_id", None),
                "night": (getattr(m, "date", None),
                          getattr(svc, "night_loads", None)),
                "session": self._session()}
        now = self._clock()
        with self._lock:
            if self.state != "opening" or gen != self._gen:
                # Ended (Done, a disarm) while its own Stop ran: it stays
                # ended; the cues' practice guards stay as that end left
                # them, and a later open's are not touched.
                return (f"Fireball practice ended while it was opening: "
                        f"{self.ended_why or 'it was ended'}.")
            self._base = base
            self._seen_off = False
            self._sched_reached = False
            self.last_beat = self.last_activity = now
            self.watcher_beat = self.watchdog_beat = now
            self.state = "arming" if mode == "live" else "ready"
        if code != 200 or (out or {}).get("failed"):
            self._ending("the Stop that opens practice did not complete",
                         stop_first=False, failed=True)
        else:
            self._note(f"{OPEN_SAYS} Mode: "
                       f"{'Live' if mode == 'live' else 'Dry'}. By "
                       f"{who or 'someone'} on the {screen}.", "opened")
        self._start_threads()
        return OPEN_SAYS

    def _open_why(self):
        ok, why = self.remote.programming()
        if not ok:
            return why.rstrip(".")
        ph = self._ph()
        if ph is None or not ph.applies():
            return "this show's clock is not the show audio"
        if self._cues() is None or self._charge() is None:
            return "the fireball timeline or charge record is not running"
        if self._link() is None:
            return "there is no flame link"
        if getattr(self._cues(), "fault", ""):
            return f"the fireball timeline has a fault: {self._cues().fault}"
        lf = getattr(self.remote, "live_flame", None)
        if lf is not None and getattr(lf, "on", False):
            return "live flame programming is on"
        cl = getattr(self.remote, "cleaning", None)
        if cl is not None and cl.active():
            return "the wave flamer cleaning is running"
        view, raw = self._flames()
        return self._flamesafe_why(view, raw)

    def _start_threads(self):
        if not self._threaded:
            return
        self._threads_gen += 1
        gen = self._threads_gen
        threading.Thread(target=self._watch_loop, args=(gen,), daemon=True,
                         name="ltcplay-practice-watch").start()
        threading.Thread(target=self._dog_loop, args=(gen,), daemon=True,
                         name="ltcplay-practice-watchdog").start()

    def beat(self, owner):
        """The opening screen is still there. (ok, sentence)."""
        if self.state == "closed":
            return False, "Fireball practice is not open."
        if owner != self.owner:
            return False, ("Only the screen that opened practice keeps it "
                           "open.")
        self.last_beat = self._clock()
        return True, ""

    def done(self, who, screen):
        if self.state == "closed":
            return "Fireball practice is not open."
        self._ending(f"{who or 'someone'} pressed Done on the {screen}",
                     user=True)
        return "Fireball practice ended: every group is disarmed."

    def step_why(self, lead_in_s=LEAD_IN_DEFAULT_S, claimed=False):
        """Why Step is refused right now (the plan aside), or "".
        `claimed`: asked by the step press itself, which holds the state
        "starting"."""
        if self.state == "arming":
            return "Arm the fireball first (hold to arm)."
        if self.state != ("starting" if claimed else "ready"):
            return f"Fireball practice is {self.state}."
        why = self._common_why()
        if why:
            return f"This step is refused: {why}."
        svc, m = self._sched()
        if not self._schedule_ok(m):
            return ("This step is refused: the schedule is not on the "
                    "Stop's Hold yet.")
        view, _raw = self._flames()
        fb, _o = self._armed(view)
        if self.mode == "live" and not (fb and self._seen_off):
            return "This step is refused: the fireball is not armed."
        if self.mode == "dry" and fb:
            return "This step is refused: the fireball is armed (Dry)."
        tr = self._charge()
        v = tr.view()
        if v.get("releasing") or any(
                t.get("state") != "empty"
                for t in (v.get("tanks") or {}).values()):
            return REFUSE_TANKS
        if v.get("pending"):
            return REFUSE_SETTLING
        now = self._clock()
        for b in (self.watcher_beat, self.watchdog_beat):
            if self._threaded and (b is None or now - b > BEAT_STALE_S):
                return ("This step is refused: the practice watcher is not "
                        "running.")
        return ""

    def _plan_now(self, lead_in_s):
        """(plan, token) from the plan object the flame cues hold."""
        cues = self._cues()
        cues.refresh()
        plan = cues._plan
        if plan is None or plan[1]:
            raise _step_refused("the fireball timeline cannot be read.")
        s = self._session()
        show = os.path.basename(
            str(getattr(s, "timeline_path", "") or ""))
        entry = plan[2].get(show)
        if entry is None:
            raise _step_refused(f"{show or 'this show'} has no fireball "
                                f"timeline.")
        cue = None
        for c in list(getattr(getattr(s, "tl", None), "cues", None) or ()):
            if getattr(c, "name", None) == entry.get("cue"):
                cue = c
                break
        if cue is None:
            raise _step_refused(f"the timeline's cue {entry.get('cue')!r} "
                                f"is not in the running show file.")
        p = plan_step(entry, float(cue.tc_seconds),
                      getattr(cue, "duration", None), lead_in_s)
        tok = {"id": next(self._ids), "cue": None, "show": show,
               "entry": entry, "hash": fbc.entry_hash(entry)}
        return p, tok

    def step(self, owner, who, screen, lead_in_s=LEAD_IN_DEFAULT_S):
        """One step. The sentence, or Refused."""
        if owner != self.owner:
            raise Refused("This step is refused: only the screen that "
                          "opened practice may Step. Nothing was sent.")
        with self._lock:
            if self.state != "ready":
                busy = True
            else:
                busy = False
                self.state = "starting"
                self._starting_at = self._clock()
                gen = self._gen
        if busy:
            raise Refused(self.step_why(lead_in_s) or
                          "This step is refused. Nothing was sent.")
        ph = self._ph()
        try:
            with ph._op:
                why = self.step_why(lead_in_s, claimed=True)
                if why:
                    raise Refused(why)
                plan, tok = self._plan_now(lead_in_s)
                cues = self._cues()
                cues.practice = tok

                def bind(cue_obj, _tok=tok):
                    if cue_obj is None:
                        raise Refused("the play could not be bound")
                    with self._lock:
                        if self.state != "starting" or gen != self._gen:
                            # Practice ended while this step was starting:
                            # the play is refused before its cues go out.
                            raise Refused("practice ended while the step "
                                          "was starting")
                        _tok["cue"] = cue_obj
                    self._rec.clear()
                    clk = ph.clock()
                    self._rec_clock = clk
                    self._recording = True
                self._step = {"plan": plan, "tok": tok, "at": self._clock(),
                              "moving_at": None, "who": who}
                ph.play_from(plan["start_show_s"], who, screen, bind=bind)
        except Refused:
            self._recording = False
            if self._cues() is not None:
                self._cues().practice = None
            self._step = None
            self._cas(("starting",), "ready", gen)
            raise
        except Exception as e:                   # noqa: BLE001
            self._recording = False
            self._step = None
            bound = tok_bound(self)
            if self._cues() is not None:
                self._cues().practice = None
            self._cas(("starting",), "ready", gen)
            if bound:
                # The play may have started: practice ends (Stop).
                self._ending(f"the step could not start ({e})")
                raise Refused(f"The step failed after it started ({e}): "
                              f"practice ended and every group is "
                              f"disarmed.")
            raise Refused(f"This step is refused: {e} Nothing was sent.")
        if not self._cas(("starting",), "step", gen):
            # Ended while the play was starting (Done, a disarm, the
            # watchdog): the play is cut and stopped again.
            tok["cut"] = True
            self._recording = False
            self._step = None
            self._stop("a step that started after practice ended")
            raise Refused("Practice ended while the step was starting: the "
                          "step was stopped and every group is disarmed.")
        self.last_activity = self._clock()
        self._note(f"Fireball practice step {tok['id']} by "
                   f"{who or 'someone'}: played from "
                   f"{fbtl.tc_text(plan['start_f'])} with a "
                   f"{plan['lead_in_s']} s lead-in "
                   f"({'Live' if self.mode == 'live' else 'Dry'}).", "step")
        return (f"Step {tok['id']}: the shot plays from "
                f"{fbtl.tc_text(plan['start_f'])}.")

    # -- the timing record (the flame link's sender thread) ---------------------
    def on_sent(self, mono, tc, show, ok):
        if not self._recording:
            return
        clk = self._rec_clock
        try:
            epoch, following = clk.heard_epoch() if clk is not None else \
                (None, False)
        except Exception:                        # noqa: BLE001
            epoch, following = None, False
        try:
            vals = bytes(show[SLOTS[0] - 1:SLOTS[-1]]) if ok else \
                bytes(len(SLOTS))
        except Exception:                        # noqa: BLE001
            vals = bytes(len(SLOTS))
        self._rec.append((mono, vals, epoch, following))

    def _digest(self, plan, snap, armed):
        """The rows from the record. Pure apart from reading it."""
        edges, against = {}, True
        slot_of = {k: fbtl.STAGE_SLOT[k] for k in plan["stages"]}
        prev = {k: 0 for k in slot_of}
        for mono, vals, epoch, following in list(self._rec):
            if epoch is None:
                # The show audio starts every play waiting for its first
                # buffer (no epoch yet): those frames are not timed.
                continue
            if not following:
                # Running on this computer's clock (freerun, a return).
                against = False
            for k, slot in slot_of.items():
                v = vals[slot - SLOTS[0]]
                e = edges.setdefault(k, {"on": None, "off": None})
                ms = (mono - epoch) * 1000.0
                if v and not prev[k] and e["on"] is None:
                    e["on"] = ms
                if not v and prev[k] and e["on"] is not None and \
                        e["off"] is None:
                    e["off"] = ms
                prev[k] = v
        skipped = dict((snap or {}).get("skipped") or {})
        return stage_rows(plan["stages"], edges, skipped, against, armed), \
            edges

    # -- the watcher and the watchdog ------------------------------------------
    def _watch_loop(self, gen):
        while self.state != "closed" and gen == self._threads_gen:
            self.watcher_beat = self._clock()
            try:
                self.tick()
            except Exception as e:               # noqa: BLE001
                try:
                    self._ending(f"the practice watcher failed "
                                 f"({type(e).__name__}: {e})")
                except Exception:                # noqa: BLE001
                    pass
            time.sleep(WATCH_S)

    def _dog_loop(self, gen):
        while self.state != "closed" and gen == self._threads_gen:
            self.watchdog_beat = self._clock()
            self.dog_tick()
            time.sleep(WATCHDOG_S)

    def dog_tick(self):
        """The watchdog: no lock, never the move lock. A watcher whose beat
        is older than BEAT_STALE_S: Stop."""
        if self.state == "closed":
            return
        wb = self.watcher_beat
        if wb is None or self._clock() - wb > BEAT_STALE_S:
            self._ending("the practice watcher stopped")

    def tick(self):
        """One pass of the watcher."""
        if self.state == "closed":
            return
        now = self._clock()
        if self.state == "ending":
            if self._retry_at is not None and now >= self._retry_at:
                self._ending(self.ended_why, retry=True)
            return
        if self.state == "starting" and self._starting_at is not None and \
                now - self._starting_at > STARTING_MAX_S:
            self._ending("the step took too long to start")
            return
        if self.state in ("opening", "starting"):
            return
        db = self.watchdog_beat
        if self._threaded and (db is None or now - db > BEAT_STALE_S * 4):
            self._ending("the practice watchdog stopped")
            return
        self._deck_tick()
        why = self._common_why(in_step=self.state == "step")
        if not why and self.last_beat is not None and \
                now - self.last_beat > PAGE_BEAT_OFF_S:
            why = "the screen that opened practice stopped answering"
        if not why and self.state in ("arming", "ready") and \
                now - (self.last_activity or now) > IDLE_OFF_S:
            why = f"{IDLE_OFF_S / 60:g} minutes with no step"
        if why:
            self._ending(why)
            return
        view, _raw = self._flames()
        fb, _o = self._armed(view)
        if self.state == "arming":
            if not fb:
                self._seen_off = True
            elif fb and self._seen_off and \
                    self._cas(("arming",), "ready", self._gen):
                self.last_activity = now
                self._note("Fireball practice: the fireball is armed; Step "
                           "is ready.", "armed")
            return
        if self.state == "step":
            self._step_tick(now, fb)

    def _step_tick(self, now, fb):
        st = self._step
        ph = self._ph()
        s = self._session()
        clk = ph.clock() if ph is not None else None
        here = s.clock_here() if s is not None else None
        plan = st["plan"]
        if clk is not None and getattr(clk, "paused", False):
            self._ending("a Hold or a pause during the step")
            return
        if here is not None and st["moving_at"] is None and \
                here > plan["start_show_s"] + 0.1:
            st["moving_at"] = now
        planned = (plan["end_show_s"] - plan["start_show_s"])
        t0 = st["moving_at"] if st["moving_at"] is not None else st["at"]
        if now - t0 > planned + OVERRUN_S:
            self._ending("the step ran over its time")
            return
        done = here is not None and here >= plan["end_show_s"]
        if not done and not ph.active():
            if here is None or here < plan["end_show_s"]:
                self._ending("the step's playback ended early")
                return
            done = True
        if not done:
            return
        try:
            ph.follow("Fireball practice", "")
        except Exception as e:                   # noqa: BLE001
            self._ending(f"the step's playback could not be stopped ({e})")
            return
        self._recording = False
        snap = self._cues().run_view()
        rows, edges = self._digest(plan, snap, self.mode == "live" and fb)
        res = {"id": st["tok"]["id"], "rows": rows,
               "flagged": sum(1 for r in rows if r["flag"]),
               "mode": self.mode}
        self.results.appendleft(res)
        self._cues().practice = None
        self._step = None
        if not self._cas(("step",), "ready", self._gen):
            return              # ended meanwhile: it stays ended
        self.last_activity = now
        worst = max((r["late_ms"] for r in rows if r["late_ms"] is not None),
                    default=None)
        self._note(f"Fireball practice step {res['id']}: "
                   f"{len(rows)} stages, "
                   f"{res['flagged']} flagged"
                   + (f", latest {worst:.0f} ms" if worst is not None
                      else "") + ". "
                   + " ".join(f"{r['word']}: {r['result']}." for r in rows),
                   "step done", fault=bool(res["flagged"]))

    # -- ending --------------------------------------------------------------
    def _stop(self, why):
        """The ordinary Stop, pressed by practice (`why` is in practice's
        own journal line)."""
        try:
            return self.remote.stop_press(
                f"Fireball practice ({self.who or 'someone'})",
                self.screen or "engine")
        except Exception as e:                   # noqa: BLE001
            return 500, {"failed": ["stop"], "error": str(e)}

    def _cut_plays(self):
        """Every practice play still bound stops firing at once (its token
        marked cut; FireballCues sends nothing for it), whether or not a
        Stop gets through."""
        cues = self._cues()
        for tok in ((self._step or {}).get("tok"),
                    getattr(cues, "practice", None)):
            if isinstance(tok, dict):
                tok["cut"] = True

    def _alert_once(self):
        if self._alerted:
            return
        self._alerted = True
        al = getattr(self.remote, "alerts", None)
        try:
            if al is not None:
                al.raise_("flames", "Fireball practice could not complete "
                          "its Stop: press Stop or Disarm all flame groups "
                          "now.")
        except Exception:                        # noqa: BLE001
            pass
        self._note("Fireball practice: the Stop did not complete; it is "
                   "pressed again every second.", "failed", fault=True)

    def _ending(self, why, stop_first=True, failed=False, retry=False,
                user=False):
        """End practice: Stop (every group and laser disarmed). A Stop that
        fails, or has not come back within RETRY_S (one that hangs after
        its first step, the flame disarm), keeps practice ENDING: the
        flames alert, and Stop pressed again every RETRY_S, whoever asks
        (the watcher or the watchdog). Never more than one new Stop per
        RETRY_S: the claim is made under the lock, whatever became of the
        last Stop (on its way, or come back failed). `user`: a person's
        Done, which may always press Stop again."""
        now = self._clock()
        with self._lock:
            if self.state == "closed":
                return
            if stop_first and not user and self.state == "ending" and \
                    self._retry_at is not None and now < self._retry_at:
                return          # the last Stop was pressed under RETRY_S ago
            late = stop_first and self._stopping
            first = self.state != "ending"
            self.state = "ending"
            gen = self._gen
            if first:
                self.ended_why = why
            if stop_first:
                self._stopping = True
                self._stop_seq += 1
                seq = self._stop_seq
            self._retry_at = now + RETRY_S
        self._recording = False
        self._cut_plays()
        if first:
            self._note(f"Fireball practice ended: {why}. Every group is "
                       f"being disarmed (Stop).", "ended")
        if late:
            self._alert_once()
        if stop_first:
            try:
                code, out = self._stop(f"Fireball practice ended: "
                                       f"{self.ended_why}")
            finally:
                with self._lock:
                    if seq == self._stop_seq:
                        self._stopping = False
            failed = code != 200 or bool((out or {}).get("failed"))
        if failed:
            self._alert_once()
            return
        cues = self._cues()
        with self._lock:
            if self.state != "ending" or gen != self._gen:
                return
            if cues is not None:
                cues.practice = None
                cues.practice_open = False
                cues.practice_fault = ""
            self._step = None
            self._retry_at = None
            self._stopping = False
            self.state = "closed"
            self._threads_gen += 1

    def close(self):
        """The engine is stopping (its flame link already closed, so
        flamesafe disarms on link loss): the threads end."""
        self._threads_gen += 1
        self._recording = False

    # -- the page --------------------------------------------------------------
    def view(self, owner=None):
        tr = self._charge()
        try:
            tanks = tr.view().get("text") if tr is not None else ""
        except Exception:                        # noqa: BLE001
            tanks = ""
        view, _raw = self._flames()
        fb, others = self._armed(view)
        why = ""
        if self.state in ("arming", "ready"):
            try:
                why = self.step_why()
            except Exception as e:               # noqa: BLE001
                why = f"{type(e).__name__}"
        return {"state": self.state, "mode": self.mode,
                "yours": owner is not None and owner == self.owner,
                "who": self.who, "screen": self.screen,
                "ended_why": self.ended_why, "step_why": why,
                "tanks": tanks, "fireball_armed": fb,
                "others_armed": others, "deck_note": self.deck_note,
                "results": list(self.results), "note": TIMING_NOTE,
                "lead_in": {"min": LEAD_IN_MIN_S, "max": LEAD_IN_MAX_S,
                            "default": LEAD_IN_DEFAULT_S},
                "late_ok_ms": LATE_OK_MS, "early_ok_ms": EARLY_OK_MS,
                "fuel_note": FUEL_NOTE}


def tok_bound(p):
    """True once a step's play was bound (its cue known)."""
    tok = (p._cues().practice if p._cues() is not None else None) or {}
    return tok.get("cue") is not None
