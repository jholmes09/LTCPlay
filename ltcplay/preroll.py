"""The pre-roll (Jeff, 2026-10-28): before a show starts, prove the rig is
working, so a dead video or audio path is found before the audience hears
the opening announcement, not after.

"I think it would be wise to implement a two or three second pre-roll in
which the system can confirm that everything is functioning because the
show starts with an immediate announcement so if the video isn't working,
we don't actually know until it's too late." Then, ruling: the window is
`preroll_s` in ltcplay_fire_ice.json, default 10 s, 3 to 15 s; the
scheduler starts it that many seconds BEFORE the scheduled time, so a show
whose rig is healthy still starts at its time to the second.

When it runs
  - A scheduled show (schedule_service.Service.tick): PREROLL_S before its
    time, only while Run is pressed and the scheduler would start it by
    itself. Passed: the show starts at its time. Failed: a "preroll" alert
    is raised and, at the show's time, the existing alert hold applies
    (alerthold.py: Held at once, 60 s, then one Delayed on the PA). Could
    not run before the time (Run pressed late, LTC Player started inside
    the window, the schedule changed): it runs then, and the show starts
    late, said in the journal; checks are never skipped.
  - Start now (an operator): the checks run first, then the show starts
    (adds the time the checks take, at most the window). Failed: the
    start is refused with the reason.
  - Continue anyway (Jeff, 2026-10-28): after a failed check, a signed-in
    person with the run permission holds the button; the show starts now
    with every failed output left off for that one show
    (schedule_service.Service.continue_anyway). Never by itself.
  - Programming: skipped. The Programming screen's Play from never runs
    it either (it is not a Start now).
  - Pre-check (the rack screen, an admin, between shows): the same checks,
    nothing started. With no failure in force it raises or clears nothing.
  - A failure cleared by a later pass (Jeff's field report, 2026-10-29:
    "I fixed the issue and then hit Check Again. It said it passed but
    the main page still says it failed."): while a failure is in force (a
    failed check before a show, and/or the open "preroll" alert), a check
    that PASSES on a fresh run (Check again, Check the rig, Start now's own
    check, a show's own check) clears the failed verdict and the preroll
    alert, journaled "Rig check passed, failure cleared (who)". It starts
    nothing: a show held for that failure stays held, and the scheduler
    never starts it by itself until a person presses Resume (then the show
    is checked again before it starts) or Start now. A pass never clears a
    failure raised after it began (the failure sequence and the alert's id
    are compared), and never touches any other alert kind. A Pre-check
    that FAILS while a failure is in force refreshes it with the new
    reasons (the alert text, and what Continue anyway leaves off).

What it may do to the rig: nothing the audience can see or hear. It never
arms, fires or plays a sound. It blanks the lasers again (a blank only
darkens), takes MadMapper's surfaces to black, and sends MadMapper ALONE a
short test timecode (clock.AudioMaster.preroll_frame: never BEYOND, never
the pixels, never broadcast) to prove the show bank plays; then it parks
MadMapper on frame 0. The surfaces stay black until the show's own start
brings them up (conductor show_starting), so a failed pre-roll leaves the
video black, which is what a Hold does too.

Unknown is failure: a check that cannot say, says too late (the window's
budget), or reads a stale status, fails.

Pure logic with injected checks and clock; each check runs on a thread of
its own so a hung device never holds the rest.
"""
import math
import threading
import time

DEFAULT_S = 10
MIN_S = 3
MAX_S = 15
# A pass counts for the show it was made for this long after it finished
# (plus the window): a pass from long ago proves nothing now.
FRESH_MARGIN_S = 5.0
# How late (past the window) a show may start because its pre-roll had to
# run at showtime: schedule.Event.late_ok_s.
LATE_MARGIN_S = 5
KIND = "preroll"           # the alert kind (alerts.KINDS)
WHO = "LTC Player"

# Continue anyway (Jeff, 2026-10-28): a person with the run permission,
# signed in, holds the button this long (the arm buttons' hold).
CONTINUE_HOLD_S = 1.0
# Continue anyway answers a failed check this recent at most (a held show
# waits for a person; a check older than this says nothing about now).
CONTINUE_MAX_AGE_S = 600.0
# What a failed check leaves OFF for the show Continue anyway starts:
# "video" (no bank select, no fade up, no timecode to MadMapper),
# "lasers" (the laser gate says no: blanked, no output enable) and
# "flames" (the flame cues are never released: zero frames only). Audio
# is never faked: a show does not start without its sound (clock.play).
OFF_FOR = {"Video": "video", "MadMapper output": "video",
           "Lasers": "lasers", "Flame link": "flames"}
OFF_WORDS = {"MadMapper": "video", "BEYOND": "lasers",
             "flame node": "flames"}


def outputs_off(items):
    """The outputs a Continue anyway leaves off, from a failed run's
    items: a failed Video, Lasers or Flame link check, and any Network or
    Timecode failure that names MadMapper, BEYOND or the flame node."""
    off = set()
    for i in items or ():
        if i.get("ok"):
            continue
        if i.get("name") in OFF_FOR:
            off.add(OFF_FOR[i["name"]])
        if i.get("name") == STANDIN:
            # Nothing could be proven: everything it could leave off.
            off.update(OFF_FOR.values())
        if i.get("name") in ("Network", "Timecode"):
            for word, out in OFF_WORDS.items():
                if word in str(i.get("text", "")):
                    off.add(out)
    return frozenset(off)


# The one check of the stand-in used when the real ones could not be built
# (delta review of #162): it always fails, so nothing starts unchecked.
STANDIN = "Rig check"


def standin(why, journal=None, alert=None, clear_alert=None,
            open_alert=None):
    """A PreRoll whose only check always fails with `why`: Start now and
    scheduled shows stay refused or held until the real check is set up,
    and Continue anyway leaves every output it could leave off off."""
    return PreRoll([(STANDIN, True,
                     lambda p, _w=str(why): (False, _w))],
                   window_s=DEFAULT_S, journal=journal, alert=alert,
                   clear_alert=clear_alert, open_alert=open_alert)


# The checks, in the order the rack screen lists them.
NAMES = ("Audio", "Announcements", "Booth speaker", "Timecode", "Video",
         "MadMapper output", "Flame link", "Lasers", "Network")


def budget_s(window_s):
    """How long the checks may take inside a window of `window_s`: one
    second is kept for the start itself."""
    return max(2.0, float(window_s) - 1.0)


class Probe:
    """What a check is handed: the deadline, cancel, and a sleep that
    stops early when cancelled."""

    def __init__(self, deadline, cancelled, clock, show=None):
        self.deadline = deadline
        self._cancelled = cancelled
        self.clock = clock
        self.show = show

    def cancelled(self):
        try:
            return bool(self._cancelled())
        except Exception:                        # noqa: BLE001
            return True

    def left(self):
        return self.deadline - self.clock()

    def sleep(self, s):
        """Sleep `s` seconds in slices; False if cancelled or past the
        deadline."""
        end = self.clock() + s
        while True:
            if self.cancelled() or self.clock() >= self.deadline:
                return False
            now = self.clock()
            if now >= end:
                return True
            time.sleep(min(0.02, end - now))


def item_text(it):
    if it["ok"]:
        return f"{it['name']} OK" + (f" ({it['text']})" if it.get("note")
                                     else "")
    return f"{it['name']} FAILED: {it['text']}"


def summary(items):
    """'Audio OK, Timecode OK, Lasers FAILED: BEYOND is not running'."""
    return ", ".join(item_text(i) for i in items)


class PreRoll:
    """`checks` is [(name, required, fn)]: fn(probe) -> (ok, sentence),
    ok True, False, or None (not used by this show: counts as OK and says
    so). `required` False: a failure shows on the rack screen and in the
    journal and never holds a show. `journal(text, fault=, outcome=)`,
    `alert(kind, text)` and `clear_alert(kind)` are the engine's.
    `skip()` -> a sentence when the pre-roll does not apply (Programming),
    else ""."""

    def __init__(self, checks, window_s=DEFAULT_S, journal=None, alert=None,
                 clear_alert=None, skip=None, clock=time.monotonic,
                 open_alert=None, alert_id=None, on_pass=None):
        self.checks = list(checks)
        self.window_s = float(window_s)
        self._journal = journal or (lambda text, **kw: None)
        self._alert = alert
        self._clear_alert = clear_alert
        self._open_alert = open_alert
        # alert_id() -> the open "preroll" alert's id, or None. With it, a
        # pass clears only the alert that was open when it began, by id.
        self._alert_id = alert_id
        # on_pass(run) after every passed run: web.py clears the "output"
        # alert (MadMapper's output, showwindows.py) raised before it began
        # when its MadMapper output check passed.
        self._on_pass = on_pass
        self.skip = skip or (lambda: "")
        self._clock = clock
        self._lock = threading.Lock()
        self._one = threading.Lock()       # one run at a time
        self._runs = {}                    # key -> run record
        self._cancel = 0
        self._cancel_why = ""
        self._last = None                  # the latest run, for the screen
        self._last_show = None             # the latest run before a show
        # The show a person chose to Continue anyway, for the screens:
        # {"label", "by", "at", "off", "failed"}; None once a new check
        # runs (it applies to that one show only).
        self.continued = None
        self._holds = {}                   # Continue anyway holds
        # A failure cleared by a later pass, for the screens: {"by",
        # "label", "at", "failed"}; None once a show's own check starts, a
        # new failure is recorded, Resume is pressed or the night ends.
        self.cleared = None
        # The order of events: every run's start and every failure takes
        # the next number, so a pass knows whether a failure came after it
        # began (it never clears one that did).
        self._seq = 0
        self._fail_seq = 0
        self._alert_lock = threading.Lock()

    # -- the scheduler's side ---------------------------------------------
    def late_ok_s(self):
        """schedule.Event.late_ok_s: how late a show whose pre-roll ran at
        its time may still start."""
        return int(math.ceil(self.window_s)) + LATE_MARGIN_S

    def claim(self, key, label=""):
        """Mark `key` as about to run (the scheduler, under its lock).
        True if it was not running, queued or freshly done already."""
        with self._lock:
            r = self._runs.get(key)
            if r is not None and r["state"] in ("queued", "checking"):
                return False
            if r is not None and r["state"] == "passed" and \
                    self._fresh(r):
                return False
            if r is not None and r["state"] in ("failed", "cleared"):
                # A failure stands for its show: the alert hold decides. A
                # cleared one waits for a person (Resume or Start now).
                return False
            self._runs[key] = {"key": key, "label": label,
                               "state": "queued", "items": [],
                               "at": self._clock(), "done": None}
            return True

    def verdict(self, key):
        """'passed' (fresh), 'failed', 'cleared' (failed, then a later
        check passed: the show waits for Resume or Start now), 'checking'
        (or queued), or None (never ran, cancelled, or a pass gone
        stale)."""
        with self._lock:
            r = self._runs.get(key)
            if r is None:
                return None
            st = r["state"]
            if st == "queued":
                return "checking"
            if st == "passed" and not self._fresh(r):
                return None
            if st == "cancelled":
                return None
            return st

    def failure_text(self, key):
        with self._lock:
            r = self._runs.get(key)
            return (r or {}).get("alert", "")

    def _fresh(self, r):
        return r["done"] is not None and \
            self._clock() - r["done"] <= self.window_s + FRESH_MARGIN_S

    def cancel(self, why):
        """Hold, Stop or Abort: any check under way stops; nothing it began
        is left running (the video test stops sending at once). Cheap,
        never raises."""
        self._cancel += 1
        self._cancel_why = str(why or "")

    def forget(self):
        """A new night: nothing from the last one counts."""
        with self._lock:
            self._runs = {k: v for k, v in self._runs.items()
                          if v["state"] in ("queued", "checking")}
            self.cleared = None

    def release_cleared(self):
        """Resume was pressed (schedule_service, under its lock): every
        show whose failure a later pass cleared is no longer held back by
        it; the scheduler checks it again before it starts it. Returns
        the keys released."""
        with self._lock:
            keys = [k for k, v in self._runs.items()
                    if v["state"] == "cleared"]
            for k in keys:
                del self._runs[k]
            self.cleared = None
        return keys

    def failure_in_force(self):
        """True while a failed check stands (any key) or the preroll alert
        is open."""
        with self._lock:
            failed = any(v["state"] == "failed" and not v.get("manual")
                         for v in self._runs.values())
        return failed or self.alert_open()

    def mark_failure(self):
        """A failure of the rig check raised from outside a run (the
        scheduler's "could not run"): numbered, so no pass that began
        before it can clear it."""
        with self._lock:
            self._seq += 1
            self._fail_seq = self._seq
            self.cleared = None

    # -- a run ------------------------------------------------------------
    def run(self, key, label="", show=None, cancelled=None, alerts=True,
            manual=False, by=""):
        """Run every check now, on this thread (each check on its own),
        and return the run record {"state", "items", "text", ...}. State
        is 'passed', 'failed' or 'cancelled'. `by` names who asked (for
        the journal line when a pass clears a failure). Never raises."""
        gen = self._cancel

        def stop():
            if self._cancel != gen:
                return True
            if cancelled is not None:
                try:
                    return bool(cancelled())
                except Exception:                # noqa: BLE001
                    return True
            return False
        with self._lock:
            r = self._runs.get(key)
            if r is None or r["state"] not in ("queued",):
                r = {"key": key, "label": label, "state": "queued",
                     "items": [], "at": self._clock(), "done": None}
                self._runs[key] = r
            if not manual:
                # A new show's check: the last Continue anyway is over, and
                # so is the last "failure cleared" note.
                self.continued = None
                self.cleared = None
            r["by"] = str(by or "")
        # One run at a time: a Start now during a scheduled pre-roll waits
        # for it (its budget at most).
        if not self._one.acquire(timeout=budget_s(self.window_s) + 2.0):
            return self._finish(r, "failed", [self._item(
                "Pre-roll", True, False, "another pre-roll did not finish "
                "in time")], alerts, manual)
        try:
            with self._lock:
                r["state"] = "checking"
                r["at"] = self._clock()
                r["manual"] = manual
                # Where this run stands in the order of events, and the
                # preroll alert open as it began (a pass clears only that
                # one, and only when no failure came after this number).
                self._seq += 1
                r["seq"] = self._seq
                r["wall_at"] = time.time()
                r["alert_at_start"] = self._open_alert_id()
                self._last = r
                if not manual:
                    self._last_show = r
            if stop():
                return self._finish(r, "cancelled", [], alerts, manual,
                                    why=self._cancel_why or "it was "
                                    "cancelled")
            items = self._run_checks(stop, show)
            if stop():
                return self._finish(r, "cancelled", items, alerts, manual,
                                    why=self._cancel_why or "it was "
                                    "cancelled")
            bad = [i for i in items if i["required"] and not i["ok"]]
            return self._finish(r, "failed" if bad else "passed", items,
                                alerts, manual)
        finally:
            self._one.release()

    @staticmethod
    def _item(name, required, ok, text, note=False):
        return {"name": name, "required": bool(required), "ok": bool(ok),
                "text": str(text or "").strip().rstrip("."), "note": note}

    def _run_checks(self, stop, show):
        start = self._clock()
        deadline = start + budget_s(self.window_s)
        probe = Probe(deadline, stop, self._clock, show)
        got = {}
        threads = []
        for name, required, fn in self.checks:
            def one(_name=name, _fn=fn):
                try:
                    got[_name] = _fn(probe)
                except Exception as e:           # noqa: BLE001
                    got[_name] = (False, f"the check itself failed "
                                         f"({type(e).__name__}: {e})")
            t = threading.Thread(target=one, daemon=True,
                                 name=f"ltcplay-preroll-{name}")
            t.start()
            threads.append(t)
        for t in threads:
            t.join(max(0.0, deadline - self._clock()) + 0.25)
        items = []
        for name, required, _fn in self.checks:
            res = got.get(name)
            if res is None:
                # Unknown is failure (Jeff): not answered in time.
                items.append(self._item(
                    name, required, False,
                    f"no answer within {budget_s(self.window_s):g} s, so "
                    f"it is not known to work"))
                continue
            ok, text = res
            if ok is None:
                items.append(self._item(name, required, True,
                                        text or "not used by this show",
                                        note=True))
            else:
                items.append(self._item(name, required, ok is True, text,
                                        note=ok is True and bool(text)))
        return items

    def _finish(self, r, state, items, alerts, manual, why=""):
        label = r.get("label") or "the show"
        text = summary(items)
        with self._lock:
            r["state"] = state
            r["items"] = items
            r["text"] = text
            r["done"] = self._clock()
            r["manual"] = bool(manual)
            self._last = r
        if state == "cancelled":
            self._note(f"Pre-roll for {label} cancelled: {why}. Nothing was "
                       f"started.", outcome="cancelled")
            return r
        bad = [i for i in items if i["required"] and not i["ok"]]
        if state == "passed":
            self._note(f"Pre-roll for {label} passed: {text}.",
                       outcome="passed")
            self._clear_failure(r)
            if self._on_pass is not None:
                try:
                    self._on_pass(r)
                except Exception:                # noqa: BLE001
                    pass
            return r
        r["failed"] = frozenset(i["name"] for i in bad)
        r["alert"] = (
            f"The rig check before {label} FAILED: "
            + "; ".join(item_text(i) for i in bad)
            + ". The show does not start by itself while this alert is "
              "open. Fix it, then press Resume and Start now (Start now "
              "checks the rig again).")
        in_force = manual and self.failure_in_force()
        with self._lock:
            self._seq += 1
            self._fail_seq = self._seq
            self.cleared = None
        self._note(f"Pre-roll for {label} FAILED: {text}.", fault=True,
                   outcome="failed")
        if alerts and not manual:
            self.raise_alert(r["alert"])
        elif in_force:
            self._refresh_failure(r, bad)
        return r

    def _refresh_failure(self, r, bad):
        """A Pre-check failed while a failure is in force: that failure
        now carries this check's reasons (its items, so Continue anyway
        leaves off what fails NOW, and its alert text). Its show stays
        held; one Continue anyway per failure, as before."""
        why = "; ".join(item_text(i) for i in bad)
        with self._lock:
            for v in self._runs.values():
                if v["state"] != "failed" or v.get("manual"):
                    continue
                lbl = v.get("label") or "the show"
                v["items"] = [dict(i) for i in r["items"]]
                v["text"] = r.get("text", "")
                v["failed"] = r["failed"]
                v["done"] = r["done"]
                v["alert"] = (
                    f"The rig check before {lbl} FAILED, and failed again "
                    f"on {r.get('label') or 'a later check'}: {why}. The "
                    f"show does not start by itself while this alert is "
                    f"open. Fix it, then press Check again, or Resume and "
                    f"Start now (Start now checks the rig again).")
            sh = self._last_show
            text = sh.get("alert") if sh is not None and \
                sh["state"] == "failed" and sh.get("alert") else (
                f"The rig check FAILED again on "
                f"{r.get('label') or 'a later check'}: {why}. The show "
                f"does not start by itself while this alert is open. Fix "
                f"it, then press Check again, or Resume and Start now.")
        again = r.get("label") or "a later check"
        self._note(f"The rig check failure stays: {again} FAILED again "
                   f"({why}).", fault=True, outcome="failed again")
        if self.alert_open():
            self.raise_alert(text)

    def _clear_failure(self, r):
        """A check passed on a fresh run. While a failure is in force it
        is cleared: every failed verdict becomes 'cleared' (the scheduler
        still never starts that show by itself: Resume or Start now) and
        the preroll alert that was open when this check began is cleared
        by its id. Nothing is cleared when a failure was recorded or
        raised after this check began, or when a different preroll alert
        is open now. Never touches another alert kind. Never raises."""
        try:
            if not self.failure_in_force():
                return
            by = r.get("by") or r.get("label") or WHO
            with self._alert_lock:
                with self._lock:
                    if self._fail_seq > r.get("seq", 0):
                        newer = True
                    else:
                        newer = False
                    now_id = self._open_alert_id()
                    if not newer and now_id is not None and \
                            self._alert_id is not None and \
                            now_id != r.get("alert_at_start"):
                        newer = True
                    if newer:
                        cleared = []
                    else:
                        cleared = [v for v in self._runs.values()
                                   if v["state"] == "failed" and
                                   not v.get("manual")]
                        for v in cleared:
                            v["state"] = "cleared"
                            v["cleared_by"] = by
                        names = sorted({n for v in cleared
                                        for n in v.get("failed", ())})
                        self.cleared = {
                            "by": by, "label": ", ".join(
                                v.get("label") or "the show"
                                for v in cleared) or "the rig",
                            "at": self._clock(), "failed": names}
                if newer:
                    self._note(f"Rig check passed ({by}), but a rig check "
                               f"failure was raised after it began, so that "
                               f"failure stays.", outcome="not cleared")
                    return
                if self.alert_open():
                    self._clear(now_id)
            self._note(f"Rig check passed, failure cleared ({by}). "
                       f"Nothing was started: a show held for it waits for "
                       f"Resume or Start now.", outcome="failure cleared")
        except Exception as e:                   # noqa: BLE001
            self._note(f"A passed rig check could not clear the failure "
                       f"({type(e).__name__}: {e}); it stays.", fault=True,
                       outcome="not cleared")

    def raise_alert(self, text):
        if self._alert is None or not text:
            return
        with self._lock:
            # Numbered first: a pass that began before this never clears it.
            self._seq += 1
            self._fail_seq = self._seq
            self.cleared = None
        try:
            with self._alert_lock:
                self._alert(KIND, text)
        except Exception as e:                   # noqa: BLE001
            self._note(f"The pre-roll alert could not be raised "
                       f"({type(e).__name__}: {e}); this line is the only "
                       f"notice.", fault=True, outcome="not raised")

    def alert_open(self):
        try:
            return bool(self._open_alert()) if self._open_alert else False
        except Exception:                        # noqa: BLE001
            return False

    def _open_alert_id(self):
        if self._alert_id is None:
            return None
        try:
            return self._alert_id()
        except Exception:                        # noqa: BLE001
            return None

    def _clear(self, alert_id=None):
        if self._clear_alert is None:
            return
        try:
            if self._alert_id is not None and alert_id is not None:
                self._clear_alert(KIND, alert_id)
            else:
                self._clear_alert(KIND)
        except Exception:                        # noqa: BLE001
            pass

    def _note(self, text, fault=False, outcome=""):
        try:
            self._journal(text, fault=fault, outcome=outcome)
        except Exception:                        # noqa: BLE001
            pass

    # -- the screen ---------------------------------------------------------
    def view(self):
        """{"state", "label", "items", "text", "age_s", "window_s",
        "manual"} of the latest run. Display only; never raises."""
        with self._lock:
            r = self._last
            if r is None:
                return {"state": "idle", "window_s": self.window_s,
                        "items": [], "text": ""}
            age = None if r["done"] is None else \
                round(self._clock() - r["done"], 1)
            out = {"state": r["state"], "label": r.get("label", ""),
                   "items": [dict(i) for i in r["items"]],
                   "text": r.get("text", ""), "age_s": age,
                   "window_s": self.window_s,
                   "manual": bool(r.get("manual")),
                   "continue_hold_s": CONTINUE_HOLD_S}
            sh = self._last_show
            if sh is not None and sh["state"] == "failed" and \
                    not sh.get("consumed"):
                # What Continue anyway is about, even after a Pre-check.
                out["show_check"] = {
                    "label": sh.get("label", ""),
                    "items": [dict(i) for i in sh["items"]],
                    "off": sorted(outputs_off(sh["items"]))}
            c = self.continued
            if c is not None:
                out["continued"] = dict(c, off=sorted(c["off"]))
            cl = self.cleared
            if cl is not None:
                out["cleared"] = {"by": cl["by"], "label": cl["label"],
                                  "failed": list(cl["failed"]),
                                  "age_s": round(self._clock() - cl["at"],
                                                 1)}
            return out

    def last_failed(self):
        """The latest run when it is a failed check before a show (not a
        Pre-check), else None: what Continue anyway is about."""
        with self._lock:
            r = self._last_show
            if r is None or r["state"] != "failed" or r.get("consumed"):
                return None
            if r["done"] is None or \
                    self._clock() - r["done"] > CONTINUE_MAX_AGE_S:
                # A failure from long ago says nothing about the rig now.
                return None
            return r

    def consume(self, r):
        """Continue anyway used this failed check: never again."""
        with self._lock:
            r["consumed"] = True

    # -- Continue anyway's hold (engine timed, like the arm buttons) -------
    def hold_begin(self, token, who):
        with self._lock:
            self._holds = {k: v for k, v in self._holds.items()
                           if self._clock() - v[0] < 30.0}
            self._holds[str(token)] = (self._clock(), str(who))

    def hold_done(self, token, who):
        """True when `who` began this hold at least CONTINUE_HOLD_S ago
        (and not more than 30 s ago). Used once."""
        with self._lock:
            got = self._holds.pop(str(token), None)
        if got is None or got[1] != str(who):
            return False
        held = self._clock() - got[0]
        return CONTINUE_HOLD_S <= held <= 30.0

    def checking(self):
        with self._lock:
            return self._last is not None and \
                self._last["state"] in ("queued", "checking")


# ------------------------------------------------------------- the checks --
# Each takes the probe first and returns (ok, sentence); ok None means the
# show does not use it. Every object is read the way its own lamp reads
# it; nothing here arms, fires or plays.

def check_audio(p, clk, refusal=""):
    """The show audio interface is open AND taking audio (a new buffer
    from the audio process within a second), and the show cue's audio is
    loaded. Nothing is played."""
    if clk is None or getattr(clk, "source", None) != "audio_master":
        return False, ("the show audio is not this program's: Run a show "
                       "file whose clock is \"audio_master\"")
    if refusal:
        return False, refusal
    if getattr(clk, "_cue", None) is not None:
        return False, "a cue is already playing on the show audio"
    if not getattr(clk, "_live", False) or \
            getattr(clk, "_device_ok", False) is not True:
        why = getattr(clk, "_fault", None) or "it has not opened"
        return False, f"the show audio interface is not open: {why}"
    seq0 = getattr(clk, "_seen_seq", None)
    end = min(p.deadline, p.clock() + 1.0)
    while p.clock() < end:
        if p.cancelled():
            return False, "cancelled"
        if getattr(clk, "_seen_seq", None) != seq0 and \
                getattr(clk, "_device_ok", False) is True:
            dev = getattr(clk, "_device_desc", "") or "the interface"
            return True, f"{dev} is taking audio"
        p.sleep(0.02)
    return False, ("the show audio interface is open but took no audio for "
                   "a second")


def check_announcements(p, ann):
    """The Delayed announcement could play now: announcements running,
    their output attached, the recording sound. Nothing is played."""
    if ann is None:
        return False, ("announcements are not running in this engine, so "
                       "the Delayed message could not play")
    st = ann.status()
    if not st.get("ok", True):
        return False, str(st.get("error") or "the announcements file "
                                             "could not be used")
    if not st.get("device_available"):
        return False, (f"the announcement output is not usable: "
                       f"{st.get('device_reason') or 'not attached'}")
    for it in st.get("announcements") or []:
        if it.get("id") == "delayed" and not it.get("available"):
            return False, (f"the Delayed recording cannot play: "
                           f"{it.get('reason') or 'missing'}")
    return True, ""


def check_booth(p, booth, resolve=None):
    """The booth speaker, when one is set up: its output is attached."""
    if booth is None or not getattr(booth, "enabled", False):
        return None, "not set up"
    try:
        resolve(booth.device_name)
    except Exception as e:                       # noqa: BLE001
        return False, f"booth speaker {booth.device_name} is not attached: {e}"
    return True, ""


def check_flame_link(p, fstatus, link, deck_chip):
    """flamesafe answering now (a NEW status frame during the pre-roll, and
    fresh), no fault, the flame link sending and nobody else holding it,
    and the Stream Deck program's arm stream live. Read only: nothing is
    sent to flamesafe or the flame node."""
    if fstatus is None and link is None:
        return None, "no flame link in this show"
    if fstatus is None:
        return False, "flamesafe's status is not read by this engine"
    t0 = getattr(fstatus, "last_at", None)
    end = min(p.deadline, p.clock() + 1.5)
    while getattr(fstatus, "last_at", None) == t0:
        if p.cancelled():
            return False, "cancelled"
        if p.clock() >= end:
            return False, ("flamesafe sent no status during the check, so "
                           "the flame link is not known to work")
        p.sleep(0.02)
    v = fstatus.view()
    if v.get("stale"):
        return False, "flamesafe's status is stale"
    if v.get("fault"):
        return False, f"flamesafe fault: {v['fault']}"
    if link is not None:
        snap = link.snapshot()
        if not snap.get("sending_ok"):
            return False, (f"the flame link is not sending "
                           f"({snap.get('sender')})")
        if snap.get("lock_alarm"):
            return False, f"the flame link is locked: {snap['lock_alarm']}"
    if deck_chip is not None:
        colour, word = deck_chip()
        if colour != "ok":
            return False, f"the Stream Deck program is not live ({word})"
    return True, ""


def check_lasers(p, blanking, arm, conductor, beyond_running=None):
    """Lasers: blanking set up, the laser arm readable, a blank sent NOW
    that got out (a blank only darkens), BEYOND's black zone timecode
    flowing at 30 frames a second when blanking by timecode, and BEYOND
    running. Never arms or lights anything."""
    if blanking is None:
        return None, "no lasers in this show"
    fault = getattr(blanking, "fault", None)
    if fault:
        return False, str(fault)
    if arm is None:
        return False, "laser arming is not set up (the Startup page)"
    try:
        arm.view()
    except Exception as e:                       # noqa: BLE001
        return False, f"the laser arm cannot be read ({type(e).__name__})"
    r = conductor.lasers_dark_now("the pre-roll")
    if not getattr(r, "ok", False):
        return False, (f"the laser blank did not get out: "
                       f"{getattr(r, 'sentence', '')}")
    gate = getattr(blanking, "gate", None)
    if gate is not None:
        if getattr(gate, "lit", False):
            return False, "BEYOND's timecode is not in the black zone"
        n0, e0 = gate.black_frames, gate.send_errors
        if not p.sleep(0.5):
            return False, "cancelled or out of time"
        if gate.send_errors != e0 or gate.black_frames - n0 < 9:
            return False, (f"BEYOND's black zone timecode is not flowing "
                           f"({gate.black_frames - n0} frames in 0.5 s)")
    if beyond_running is not None:
        got = beyond_running()
        if got is False:
            return False, "BEYOND is not running"
        if got is None:
            return True, ("BEYOND is on another computer: its address is "
                          "in the Network check")
    return True, ""


def check_video(p, link, watchdog, clk, conductor, running=None,
                unconfirmed_ok=False, frames_s=2.0):
    """MadMapper plays THIS show's bank: every surface to black first
    (through the conductor, recorded), the show bank selected, then a test
    timecode from 00:00:00:00 to MadMapper alone; its heartbeat track
    (/float-1) has to answer AND advance at about real time. Then MadMapper
    is parked on frame 0. This proves the timeline plays, not that a
    projector shows a picture. Without a heartbeat it cannot be proven:
    failed, unless preroll_video_unconfirmed_ok says otherwise."""
    if link is None:
        return None, "no MadMapper in this show"
    if running is not None and running() is False:
        return False, "MadMapper is not running"
    if watchdog is None or getattr(watchdog, "bind_error", None):
        why = ("MadMapper's heartbeat is not set up, so the video cannot be "
               "proven to play: add the /float-1 heartbeat track to the show "
               "bank and the heartbeat block to the show file")
        if unconfirmed_ok:
            return True, (f"NOT confirmed, allowed by "
                          f"preroll_video_unconfirmed_ok: {why}")
        return False, why
    if clk is None or not hasattr(clk, "preroll_frame"):
        return False, "the show clock cannot send MadMapper a test timecode"
    r = conductor.preroll_video_dark()
    if not getattr(r, "ok", False):
        return False, (f"the screens could not be taken to black first, so "
                       f"the video was not tested: "
                       f"{getattr(r, 'sentence', '')}")
    end = min(p.deadline, p.clock() + 1.0)
    while conductor.snapshot()["applied"].get("video") != "black":
        if p.clock() >= end or p.cancelled():
            return False, ("MadMapper did not confirm the screens went black, "
                           "so the video was not tested")
        p.sleep(0.02)
    try:
        link.select_bank(link.cfg.show_bank, wait=False)
        # Set following timecode again, as every show start does
        # (fire_ice.follow_show_bank, #161): after an Abort or a failed
        # start the bank is stopped and would not chase the test timecode.
        link.play(link.cfg.show_bank, wait=False)
    except Exception as e:                       # noqa: BLE001
        return False, f"the show bank could not be selected: {e}"
    seen = []
    period = 1.0 / 30.0
    t0 = p.clock()
    count0, _v, _a, hb_t0 = watchdog.heard()
    last = count0
    n = 0
    sent = False
    try:
        while p.clock() - t0 < frames_s:
            if p.cancelled():
                return False, "cancelled"
            if p.clock() >= p.deadline - 0.3:
                break
            if not clk.preroll_frame(n):
                return False, ("the test timecode could not be sent to "
                               "MadMapper")
            sent = True
            n += 1
            count, value, at, _now = watchdog.heard()
            # Bench B9: the bank select makes MadMapper re-send its last
            # value (the end of the last show) once: the first 0.3 s of
            # answers, and any older reading before the position starts
            # rising, are not the test timecode's.
            if count != last and at is not None and at >= hb_t0 + 0.3:
                seen.append((at, value))
            last = count
            p.sleep(max(0.0, t0 + n * period - p.clock()))
    finally:
        if sent:
            # Parked on frame 0, the show's own first frame, whatever
            # happened above.
            for _ in range(3):
                clk.preroll_frame(0)
    good = [(a, v) for a, v in seen if isinstance(v, (int, float)) and
            not isinstance(v, bool) and math.isfinite(v)]
    drops = [i for i in range(len(good) - 1) if good[i + 1][1] < good[i][1]]
    if drops and drops[-1] < len(good) // 2:
        good = good[drops[-1] + 1:]
    if len(good) < 3:
        return False, "MadMapper did not answer the timecode"
    span = good[-1][0] - good[0][0]
    rise = good[-1][1] - good[0][1]
    if span < 0.4 or rise <= 0 or any(b[1] < a[1] for a, b in
                                      zip(good, good[1:])):
        return False, ("MadMapper answered but its position did not move "
                       "with the timecode")
    length = watchdog.show_len_s()
    if length:
        rate = rise * length / span
        if not 0.5 <= rate <= 2.0:
            return False, (f"MadMapper's position moved at {rate:.2f} times "
                           f"real time, so the heartbeat track or the show "
                           f"bank is wrong")
    return True, "the show bank plays; the screens stay black until the show"


def check_madmapper_output(p, look, why_not=""):
    """MadMapper's video output is on: a full-screen MadMapper output
    window on the projector display (showwindows.OutputLook.now, read from
    Windows' window list; Jeff's field report, 2026-10-29: after a reboot
    the output was off and the projectors showed the Windows desktop).
    Off fails, with what to do; not known fails (unknown is failure).
    `look` None: not checked here, said why (`why_not`). Reads windows
    only: nothing is sent to MadMapper."""
    if look is None:
        if str(why_not).startswith("not known"):
            # The check could not be set up: unknown is failure.
            return False, why_not
        return None, why_not or "not checked here"
    try:
        state, why = look()
    except Exception as e:                       # noqa: BLE001
        return False, f"not known: {type(e).__name__}: {e}"
    if state == "on":
        return True, why
    if state == "off":
        return False, (f"{why}. Turn it on in MadMapper: Output menu, "
                       f"Fullscreen")
    return False, why or "not known"


def check_timecode(p, out, gate=None):
    """Art-Net timecode (30 frames a second, type 3) going out: the
    timecode socket sending to every receiver without errors during the
    pre-roll (the video test's frames to MadMapper), and BEYOND's black
    zone stream flowing when there is one."""
    if out is None or not getattr(out, "dests", None):
        if gate is None:
            return None, "the show file sends no Art-Net timecode"
    if out is not None and getattr(out, "dests", None):
        sent0 = out.packets_sent
        held = out.held_dests() if hasattr(out, "held_dests") else []
        if held:
            end = min(p.deadline, p.clock() + 1.5)
            while out.packets_sent == sent0:
                if p.clock() >= end or p.cancelled():
                    return False, ("no Art-Net timecode packet went out to "
                                   "MadMapper during the check")
                p.sleep(0.02)
        if out.failing_labels:
            return False, (f"not taking Art-Net timecode: "
                           f"{', '.join(out.failing_labels)}")
        since = out.seconds_since_error
        if since is not None and since < 5.0:
            return False, f"the timecode socket failed: {out.last_error}"
    if gate is not None:
        if getattr(gate, "ip", None) is None:
            return False, "BEYOND's timecode has no address"
        n0 = gate.black_frames + gate.show_frames
        if not p.sleep(0.5):
            return False, "cancelled or out of time"
        if gate.black_frames + gate.show_frames - n0 < 9:
            return False, "BEYOND's timecode stream is not flowing"
    return True, ""


def check_network(p, hosts, pinger, parallel=16):
    """Every device the show uses answers on the network now: each
    (label, address) pinged once, in parallel. No answer is a failure."""
    hosts = [(lbl, ip) for lbl, ip in hosts if ip]
    if not hosts:
        return None, "no network devices to reach"
    seen, todo = set(), []
    for lbl, ip in hosts:
        if ip not in seen:
            seen.add(ip)
            todo.append((lbl, ip))
    got = {}

    def one(lbl, ip):
        try:
            got[ip] = bool(pinger(ip))
        except Exception:                        # noqa: BLE001
            got[ip] = False
    threads = []
    for lbl, ip in todo:
        while sum(t.is_alive() for t in threads) >= parallel:
            time.sleep(0.01)
        t = threading.Thread(target=one, args=(lbl, ip), daemon=True,
                             name="ltcplay-preroll-ping")
        t.start()
        threads.append(t)
    for t in threads:
        t.join(max(0.0, p.deadline - p.clock()))
    dead = [f"{lbl} {ip}" for lbl, ip in todo if got.get(ip) is not True]
    if dead:
        return False, (f"no answer from {', '.join(dead[:6])}"
                       + (f" and {len(dead) - 6} more" if len(dead) > 6
                          else ""))
    return True, f"{len(todo)} answered"
