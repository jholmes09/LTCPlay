"""Laser ARM and DISARM (Jeff's rulings, 2026-10-22).

What it is
==========

Each laser is ARMED or DISARMED, by itself or by a group (a fast
multi-select of lasers, like the machine groups on the Morning page;
nothing is saved). The state lives in memory only: every engine start and
every restart begins with every laser DISARMED, and the first thing this
module does is send BEYOND's DisableLaserOutput (ruling 2).

Arming is a deliberate operator act: one click and one confirm on the
Morning and Shutdown page (the same confirm box a lockout uses), refused
with its reason when it cannot be done. It is not a safety press: the role
matrix's "arm" row (users.py) decides who may, as for arming flames, and
from the show network it needs screen_arming, as a laser test ON does.
Disarming is safety-reducing: never refused, from every surface, signed in
or not, with no confirm.

What BEYOND is told, and why it is all or nothing
=================================================

BEYOND's only laser output commands this repo has verified are GLOBAL:
/beyond/general/EnableLaserOutput and /beyond/general/DisableLaserOutput,
no argument (beyond.py, bench B8.3: both accepted by BEYOND 5.5, from
Pangolin's OSC list). They switch BEYOND's output for every projector and
zone at once. No per-projector or per-zone enable, disable or brightness
command is verified anywhere in this repo (the per-zone addresses in the
selftest and the witness tools are made-up fixtures). So:

  - EnableLaserOutput (and the show's brightness 100) goes out only when
    EVERY configured laser is armed and NONE is locked out, the blanking
    is set up (a BEYOND OSC target), and nothing blocks it (an Abort
    latch, a Hold, the night closing, a lockout or settings problem).
    With one laser disarmed and another armed, LTC Player sends BEYOND
    NOTHING that lights anything: the global command cannot leave one
    projector dark and light another, so lighting the armed one would
    light the disarmed one too. Per-laser arm state still gates that
    laser's own cues (its calibration test, lasertest.py).
  - Every disarm, of one laser or of all, sends the blank (brightness 0)
    and DisableLaserOutput at once: with any laser disarmed the rule
    above no longer holds, so BEYOND's output goes off for every laser.
  - Each laser may carry PLACEHOLDER per-laser `arm` and `disarm` OSC
    messages (lasertest.py), empty by default: nothing is sent until Andy
    fills them in, and they never loosen the rule above.

When EnableLaserOutput is sent (ruling 6, decided here): once when the
last laser needed is armed (so the laser output is on, still blanked at
brightness 0, ready for the show), and again at every show start and
Resume, right before the unblank (beyondtc.Blanking.unblank, through the
conductor's laser gate). Never otherwise.

Abort and Hold (ruling 3): both disarm every laser at the press, before
anything else is asked, and the blank and DisableLaserOutput go out on
this module's own sender thread: the press never waits on a lock, a disk
or BEYOND. Abort's existing blank on the pressing thread stays the fast
path. A disarm is never undone by Resume or Reset: the lasers come back
only once an operator arms them again (refused while the Abort latch
stands or the show is on Hold). The night closing disarms them too
(ruling 4). Nothing here starts a show or presses Run (ruling 5).

The sender
==========

One thread, one ordered list of jobs: "dark" (blank, then
DisableLaserOutput, retried a few times if no packet got out) and
"enable" (asked again at the moment it is sent, so a disarm in between
wins). A dark job is never dropped or reordered behind an enable; an
enable still queued when a dark job is queued behind it is skipped. A
disarm also cuts short an EnableLaserOutput already part way through its
three packets (beyond.Beyond's disable epoch). Nothing in here holds a
lock across a send.
"""
import collections
import threading
import time

# How many times a dark job tries again when no DisableLaserOutput packet
# got out (each try is beyond.py's own 3 packets 20 ms apart), and how
# long it waits between tries.
DARK_RETRIES = 3
DARK_RETRY_S = 0.25
# What the closing of the night is called, by the scheduler's own states.
# CLOSING disarms while it lasts; OFF (the night over) disarms on the way
# in: a dry-run scheduler steps CLOSING to OFF inside one call, so the
# watcher may never see CLOSING at all (review of PR #87, P1). Arming in
# OFF stays allowed afterwards (the morning's calibration).
CLOSING_STATES = ("CLOSING",)
OFF_STATES = ("OFF",)
# A laser group name, like the machine groups.
GROUP_NAME_MAX = 48


class LaserArmConfigError(ValueError):
    """A laser_groups block that cannot be used, in a sentence."""


def parse_groups(doc, where, laser_ids):
    """ltcplay_morning.json "laser_groups": {name: [laser id, ...]}. Left
    out: one group, "All lasers", with every laser. Raises
    LaserArmConfigError."""
    if doc is None:
        return {"All lasers": list(laser_ids)} if laser_ids else {}
    if not isinstance(doc, dict):
        raise LaserArmConfigError(f"{where}: laser_groups has to be an "
                                  f"object of group name to laser ids.")
    out = {}
    for name, ids in doc.items():
        if not isinstance(name, str) or not name.strip() or \
                len(name) > GROUP_NAME_MAX:
            raise LaserArmConfigError(f"{where}: a laser group needs a "
                                      f"name, at most {GROUP_NAME_MAX} "
                                      f"letters.")
        if not isinstance(ids, list) or not ids:
            raise LaserArmConfigError(f"{where}: laser group {name!r} has "
                                      f"to list at least one laser id.")
        for lid in ids:
            if lid not in laser_ids:
                raise LaserArmConfigError(
                    f"{where}: laser group {name!r} names {lid!r}, which is "
                    f"not a laser in lasers.")
        out[name.strip()] = list(dict.fromkeys(ids))
    return out


def _ask(fn, *args):
    """(value, error sentence or None). Never raises."""
    try:
        return fn(*args), None
    except Exception as e:                       # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


class LaserArm:
    """Every laser's arm state, and the one sender of BEYOND's laser
    output switch.

    lasers      [(id, name), ...] in page order.
    groups      {name: [id, ...]}.
    locked      callable(id) -> bool, the engine's lockout. Raises or
                anything but a real False: locked.
    output      callable() -> beyondtc.Blanking (or None). Its
                output_fault, output_enable() and output_disable() are the
                only way this module reaches BEYOND.
    blockers    callable() -> "" or a sentence: why nothing may be armed
                or enabled now (an Abort latch, a Hold, the night closing,
                a settings or lockout problem). Raises: blocked.
    blank_now   callable(what) or None: the lasers' blank on the caller's
                thread (the conductor's lasers_dark_now, which keeps its
                record). None: output().blank().
    per_laser   lasertest.LaserTests or None: a disarmed laser's test goes
                off, and its PLACEHOLDER arm/disarm messages.
    on_permitted callable(who, screen) or None: told once every laser is
                armed and the output enable is asked for, so a show that
                is already up lights its lasers again (the conductor's
                lasers_armed).
    journal     (text, **fields). audit: an audit.AuditLog or None.
    threaded    False: nothing runs until run_pending() (the selftest)."""

    def __init__(self, lasers, groups=None, locked=lambda lid: False,
                 output=lambda: None, blockers=lambda: "", blank_now=None,
                 per_laser=None, on_permitted=None, journal=None,
                 audit=None, threaded=True, clock=time.monotonic,
                 sleep=time.sleep, wall=time.time):
        self.order = [lid for lid, _n in lasers]
        self.names = dict(lasers)
        self.groups = dict(groups or {})
        self._locked = locked
        self._output = output
        self._blockers = blockers
        self.blank_now = blank_now
        self.per_laser = per_laser
        self.on_permitted = on_permitted
        self._journal = journal
        self.audit = audit
        self._clock = clock
        self._sleep = sleep
        self._wall = wall
        self._lock = threading.Lock()
        self.armed = {}          # id -> {"since", "who", "screen"}
        self._cv = threading.Condition()
        self._jobs = collections.deque()
        self._running = None     # the job the sender is on
        self._closed = False
        self.last_output = None  # {"what", "ok", "why", "at"}
        self.output_errors = 0
        self._was_closing = False
        self._last_state = None
        self._was_permitted = False
        # Every disarm bumps it; an arm that saw another value at its start
        # writes nothing (review of PR #87, P2-2: an arm racing an Abort).
        self._disarm_gen = 0
        # Abort and Hold presses being handled right now (press_begin and
        # press_end): no laser arms while one is.
        self._presses = 0
        self._threaded = threaded
        self._thread = None
        if threaded:
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="ltcplay-laser-arm")
            self._thread.start()
        # Ruling 2: every engine start begins disarmed, and BEYOND is told.
        self._queue("dark", "the engine started: every laser starts "
                            "disarmed", "", "engine")

    # -- reading -------------------------------------------------------------
    def is_armed(self, lid):
        with self._lock:
            return lid in self.armed

    def armed_ids(self):
        with self._lock:
            return [lid for lid in self.order if lid in self.armed]

    def _is_locked(self, lid):
        v, err = _ask(self._locked, lid)
        return err is not None or v is not False

    def _output_obj(self):
        v, err = _ask(self._output)
        return None if err is not None else v

    def output_fault(self):
        """None while BEYOND's output switch can be sent, else why not."""
        out = self._output_obj()
        if out is None:
            return "Lasers: blanking not set up"
        v, err = _ask(lambda: out.output_fault)
        if err is not None:
            return f"the laser blanking could not be read ({err})"
        return v

    def _blocked(self):
        v, err = _ask(self._blockers)
        if err is not None:
            return f"the show state could not be read ({err})"
        if v is None or v == "":
            return ""
        return str(v)

    def refusal(self, ids):
        """Why `ids` cannot be armed now, or None."""
        if not ids:
            return "no laser was named"
        with self._lock:
            pressing = self._presses
        if pressing:
            return ("an Abort or Hold is being pressed right now; arm again "
                    "once it has landed")
        unknown = [lid for lid in ids if lid not in self.names]
        if unknown:
            return f"there is no laser called {', '.join(unknown)}"
        why = self.output_fault()
        if why is not None:
            return (f"{why}: BEYOND's laser output cannot be switched from "
                    f"here (the \"beyond\" block with its host and port in "
                    f"ltcplay_fire_ice.json)")
        locked = [self.names[lid] for lid in ids if self._is_locked(lid)]
        if locked:
            return (f"{', '.join(locked)} {'is' if len(locked) == 1 else 'are'}"
                    f" locked out; unlock first")
        why = self._blocked()
        if why:
            return why
        return None

    def may_enable(self):
        """None while BEYOND's laser output may be on and the lasers lit:
        every configured laser armed, none locked out, the blanking set
        up, nothing blocking. Else the sentence. The conductor's laser gate
        and Blanking.arm_check both ask this; unknown is never yes."""
        if not self.order:
            return "no lasers are configured"
        why = self.output_fault()
        if why is not None:
            return why
        with self._lock:
            missing = [self.names[lid] for lid in self.order
                       if lid not in self.armed]
        if missing:
            return (f"not every laser is armed ({', '.join(missing)} "
                    f"{'is' if len(missing) == 1 else 'are'} not), and "
                    f"BEYOND's only verified laser output command is for "
                    f"every laser at once")
        locked = [self.names[lid] for lid in self.order
                  if self._is_locked(lid)]
        if locked:
            return f"{', '.join(locked)} locked out"
        blocked = self._blocked()
        if blocked:
            return blocked
        return None

    # -- the presses ---------------------------------------------------------
    def arm(self, ids, who="", screen="", confirmed=False):
        """Arm `ids` (one laser, or a group's), all or none. (ok, sentence).
        Needs confirmed True: the page's confirm box."""
        ids = list(dict.fromkeys(str(i) for i in (ids or [])))
        label = who or screen or "someone"
        if confirmed is not True:
            return False, ("Arming lasers needs a confirm on the page. "
                           "Nothing was armed.")
        with self._lock:
            gen = self._disarm_gen
        why = self.refusal(ids)
        if why is not None:
            names = ", ".join(self.names.get(i, i) for i in ids) or "nothing"
            self._note(f"{label} asked to arm {names}, refused: {why}.",
                       action="laser-arm", outcome="refused", who=who,
                       screen=screen)
            self._audit(who, screen, "laser-arm", ",".join(ids), "refused",
                        why)
            return False, f"Not armed: {why}. Nothing was armed."
        now = self._wall()
        with self._lock:
            # The refusal was asked without the lock: a disarm (an Abort, a
            # Hold) that landed since, or a press now under way, wins.
            raced = self._disarm_gen != gen or self._presses
            new = [] if raced else [lid for lid in ids
                                    if lid not in self.armed]
            for lid in new:
                self.armed[lid] = {"since": now, "who": label,
                                   "screen": screen}
        if raced:
            self._note(f"{label}'s arm was refused: a disarm (Abort, Hold or "
                       f"a disarm press) came in while it was being armed.",
                       action="laser-arm", outcome="refused", who=who,
                       screen=screen)
            self._audit(who, screen, "laser-arm", ",".join(ids), "refused",
                        "a disarm came in first")
            return False, ("Not armed: a disarm (Abort, Hold or a disarm "
                           "press) came in while arming. Nothing was armed.")
        names = ", ".join(self.names[i] for i in ids)
        self._note(f"{names} ARMED by {label}"
                   f"{' on the ' + screen if screen and who else ''}.",
                   action="laser-arm", outcome="armed", who=who,
                   screen=screen)
        self._audit(who, screen, "laser-arm", ",".join(ids), "accepted",
                    "armed")
        if self.per_laser is not None:
            for lid in new:
                try:
                    self.per_laser.send_placeholder(lid, "arm", f"armed by "
                                                                f"{label}")
                except Exception:                # noqa: BLE001
                    pass
        why = self.may_enable()
        if why is None:
            self._queue("enable", f"every laser armed, the last by {label}",
                        who, screen)
            if self.on_permitted is not None:
                try:
                    self.on_permitted(who, screen)
                except Exception:                # noqa: BLE001
                    pass
            return True, (f"{names} armed. Every laser is armed: BEYOND's "
                          f"laser output is switched on (still dark until "
                          f"the show lights it).")
        return True, (f"{names} armed. BEYOND's laser output stays OFF: "
                      f"{why}.")

    def disarm(self, ids, why="", who="", screen="", defer=False):
        """Disarm `ids`. Never refused. The blank and DisableLaserOutput go
        out on the sender, whatever was armed (a disarm never trusts that
        the last one landed). `defer`: the journal and audit lines are
        written by the sender too (Abort and Hold: nothing on the pressing
        thread but the state change). (True, sentence)."""
        ids = [str(i) for i in (ids or [])]
        label = who or screen or "someone"
        with self._lock:
            self._disarm_gen += 1
            was = [lid for lid in self.order if lid in ids and
                   lid in self.armed]
            for lid in ids:
                self.armed.pop(lid, None)
        reason = why or f"disarmed by {label}"
        names = ", ".join(self.names.get(i, i) for i in was)
        text = (f"{names} DISARMED ({reason})." if was else
                f"Lasers disarmed ({reason}): none was armed.")
        note = (text, dict(action="laser-disarm", outcome="disarmed",
                           who=who, screen=screen))
        audit = (who, screen, "laser-disarm", ",".join(ids), "accepted",
                 reason)
        self._queue("dark", reason, who, screen, ids=ids,
                    notes=[note] if defer else None,
                    audits=[audit] if defer else None)
        if not defer:
            self._note(text, **note[1])
            self._audit(*audit)
        return True, (f"{names or 'Nothing armed'} disarmed. BEYOND is "
                      f"blanked and its laser output switched off.")

    def disarm_all(self, why, who="", screen="", defer=True):
        """Every laser disarmed: Abort, Hold, the night closing, the engine
        stopping, Disarm all lasers. Only the state change happens on the
        caller's thread unless `defer` is False."""
        return self.disarm(list(self.order), why, who, screen, defer=defer)

    def on_lock(self, lid, who="", screen=""):
        """A laser locked out is disarmed at once (a locked unit cannot be
        armed, MORNING.md)."""
        return self.disarm([lid], f"{self.names.get(lid, lid)} was locked "
                                  f"out", who, screen)

    def press_begin(self):
        """An Abort or Hold press has started (remote.py, before any other
        work): until press_end(), nothing arms."""
        with self._lock:
            self._presses += 1
            self._disarm_gen += 1

    def press_end(self):
        with self._lock:
            self._presses = max(0, self._presses - 1)

    def tick(self, state):
        """The watcher's pass with the scheduler's state (ruling 4): while
        the night is CLOSING nothing stays armed, and the night being over
        (OFF) disarms on the way in, whether or not CLOSING was ever seen.
        And a blocker that appears while every laser is armed and lit (a
        lockout or settings problem, the blanking lost) disarms them too:
        the gate would only stop the NEXT light."""
        closing = state in CLOSING_STATES
        off_in = state in OFF_STATES and self._last_state not in OFF_STATES
        if (closing and (not self._was_closing or self.armed_ids())) or \
                off_in:
            self.disarm_all("the night is closing" if closing else
                            "the night is over", "", "engine", defer=False)
        self._was_closing = closing
        self._last_state = state
        why = self.may_enable()
        if self._was_permitted and why is not None and \
                len(self.armed_ids()) == len(self.order):
            self.disarm_all(f"the lasers may no longer be lit: {why}", "",
                            "engine", defer=False)
        self._was_permitted = why is None

    # -- the sender ----------------------------------------------------------
    def _queue(self, kind, why, who, screen, ids=None, notes=None,
               audits=None):
        job = {"kind": kind, "why": why, "who": who, "screen": screen,
               "ids": ids, "notes": notes or [], "audits": audits or []}
        with self._cv:
            if kind == "dark":
                # An enable still waiting never goes once a dark is queued
                # behind it; a dark already waiting carries this one's lines.
                waiting = [j for j in self._jobs if j["kind"] == "dark"]
                self._jobs = collections.deque(
                    j for j in self._jobs if j["kind"] == "dark")
                if waiting:
                    # And the lasers it covers (review of PR #87, round 2,
                    # P3-1): None is every laser.
                    w = waiting[-1]
                    if w["ids"] is None or ids is None:
                        w["ids"] = None
                    else:
                        w["ids"] = list(dict.fromkeys(list(w["ids"]) +
                                                      list(ids)))
                    waiting[-1]["notes"].extend(job["notes"])
                    waiting[-1]["audits"].extend(job["audits"])
                    self._cv.notify_all()
                    return
            self._jobs.append(job)
            self._cv.notify_all()

    def pending(self):
        with self._cv:
            return [j["kind"] for j in self._jobs]

    def run_pending(self):
        """Not threaded: run every queued job on this thread."""
        while True:
            with self._cv:
                if not self._jobs:
                    return
                job = self._jobs.popleft()
                self._running = job
            try:
                self._run(job)
            finally:
                with self._cv:
                    self._running = None
                    self._cv.notify_all()

    def wait_idle(self, timeout=2.0):
        end = self._clock() + timeout
        with self._cv:
            while self._jobs or self._running is not None:
                left = end - self._clock()
                if left <= 0:
                    return False
                self._cv.wait(left)
        return True

    def _loop(self):
        while True:
            with self._cv:
                while not self._jobs and not self._closed:
                    self._cv.wait()
                if not self._jobs and self._closed:
                    return
                job = self._jobs.popleft()
                self._running = job
            try:
                self._run(job)
            except Exception as e:               # noqa: BLE001
                self._note(f"The laser arm's sender hit an error and carried "
                           f"on ({type(e).__name__}: {e}). That is a bug in "
                           f"ltcplay.", fault=True, action="laser-output",
                           outcome="error")
            finally:
                with self._cv:
                    self._running = None
                    self._cv.notify_all()

    def _run(self, job):
        # The blank and DisableLaserOutput first, before any journal or
        # audit line or test-off message (review of PR #87, P3-4).
        if job["kind"] == "dark":
            self._dark(job)
        else:
            self._enable(job)
        for text, fields in job["notes"]:
            self._note(text, **fields)
        for a in job["audits"]:
            self._audit(*a)

    def _blank(self, why):
        fn = self.blank_now
        try:
            if fn is not None:
                r = fn(f"lasers disarmed: {why}")
                return bool(getattr(r, "ok", r))
            out = self._output_obj()
            if out is None:
                return False
            return out.blank() is True
        except Exception:                        # noqa: BLE001
            return False

    def _dark(self, job):
        why = job["why"]
        blanked = self._blank(why)
        out = self._output_obj()
        sendable = out is not None and getattr(out, "osc", None) is not None
        ok = False
        if sendable:
            for i in range(DARK_RETRIES):
                ok, err = _ask(out.output_disable)
                ok = ok is True and err is None
                if ok:
                    break
                with self._cv:
                    newer = any(j["kind"] == "dark" for j in self._jobs)
                if newer:
                    break                # the next dark job tries again
                if i < DARK_RETRIES - 1:
                    self._sleep(DARK_RETRY_S)
        # Then each disarmed laser's test off and its placeholder disarm
        # messages: they only darken, and a test grid must not outlive its
        # arm.
        pl = self.per_laser
        if pl is not None:
            for lid in (job["ids"] or self.order):
                try:
                    if lid in getattr(pl, "on", {}):
                        pl.set(lid, False, who=f"the engine ({why})")
                    pl.send_placeholder(lid, "disarm", why)
                except Exception:                # noqa: BLE001
                    pass
        if not sendable:
            # No OSC link at all: the standing "blanking not set up" fault
            # already says so on every screen; nothing more can be sent.
            self._record("disable", False, why, sent=False)
            return
        self._record("disable", ok, why)
        if not ok:
            self._note(f"BEYOND's laser output could NOT be switched off "
                       f"({why}): no DisableLaserOutput packet got out"
                       f"{'' if blanked else ', and the blank did not either'}"
                       f". Blank the lasers at BEYOND.", fault=True,
                       action="laser-output", outcome="disable_failed")
        self._audit("", "engine", "laser-output-disable", why,
                    "accepted" if ok else "error",
                    "" if ok else "no packet got out", system=True)

    def _enable(self, job):
        with self._cv:
            if any(j["kind"] == "dark" for j in self._jobs):
                return                   # a disarm came after it: it wins
        why = self.may_enable()
        if why is not None:
            self._note(f"BEYOND's laser output stays off: {why}.",
                       action="laser-output", outcome="refused")
            return
        out = self._output_obj()
        if out is None:
            return

        def still():
            with self._cv:
                if any(j["kind"] == "dark" for j in self._jobs):
                    return False
            return self.may_enable() is None
        ok, err = _ask(lambda: out.output_enable(still_wanted=still))
        ok = ok is True and err is None
        self._record("enable", ok, job["why"])
        if not ok and still():
            self._note(f"BEYOND's laser output enable did not go out "
                       f"({job['why']}). The lasers stay dark; it is sent "
                       f"again at the next show start.", fault=True,
                       action="laser-output", outcome="enable_failed")
        self._audit("", "engine", "laser-output-enable", job["why"],
                    "accepted" if ok else "refused",
                    "" if ok else "not sent, or cut by a disarm",
                    system=True)

    def _record(self, what, ok, why, sent=True):
        if not ok:
            self.output_errors += 1
        self.last_output = {"what": what, "ok": bool(ok), "why": why,
                            "sent": sent, "at": self._wall()}

    # -- notes ---------------------------------------------------------------
    def _note(self, text, **fields):
        if self._journal is None:
            return
        try:
            self._journal(text, **fields)
        except Exception:                        # noqa: BLE001
            pass

    def _audit(self, who, screen, action, target, result, reason,
               system=False):
        log = self.audit
        if log is None:
            return
        try:
            from . import audit as audit_mod
            log.event(user=audit_mod.SYSTEM if system else
                      (who or audit_mod.UNSIGNED),
                      surface="engine", action=action, target=target,
                      result=result, reason=reason,
                      source=screen or "engine")
        except Exception:                        # noqa: BLE001
            pass

    # -- the page --------------------------------------------------------------
    def view(self):
        with self._lock:
            armed = {k: dict(v) for k, v in self.armed.items()}
        lasers = []
        for lid in self.order:
            lasers.append({"id": lid, "name": self.names[lid],
                           "armed": lid in armed,
                           "locked": self._is_locked(lid),
                           "since": (armed.get(lid) or {}).get("since"),
                           "who": (armed.get(lid) or {}).get("who")})
        n = sum(1 for l in lasers if l["armed"])
        return {"lasers": lasers, "groups": {k: list(v) for k, v in
                                             self.groups.items()},
                "armed": n, "total": len(lasers),
                "output_permitted": self.may_enable() is None,
                "why_off": self.may_enable(),
                "arm_refusal": self.refusal(list(self.order)),
                "output_fault": self.output_fault(),
                "last_output": dict(self.last_output or {}) or None,
                "per_laser_osc": "not verified: a placeholder per laser, "
                                 "empty unless set in ltcplay_morning.json"}

    def status_view(self):
        """The compact part every screen shows."""
        armed = self.armed_ids()
        return {"armed": [self.names[l] for l in armed],
                "total": len(self.order),
                "output_permitted": self.may_enable() is None,
                "output_fault": self.output_fault()}

    def close(self):
        """The engine stopping: every laser disarmed and BEYOND told, on
        this thread, before the sender stops (bounded)."""
        self.disarm_all("the engine is stopping", "", "engine", defer=False)
        if self._threaded:
            self.wait_idle(2.0)
        else:
            self.run_pending()
        with self._cv:
            self._closed = True
            self._cv.notify_all()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(1.0)
