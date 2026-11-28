"""Tap, then confirm, to arm a flame group from a phone (Jeff, 2026-11-27).

"We already tried the 1-second hold on a phone and it didn't work. Tap and
Confirm is a MUST." A hold to arm is a finger kept down while the page
beats ten times a second; on park wifi or a cell link those beats arrive
late or not at all, and the hold is let go. Here the person's intent is two
deliberate taps instead (Arm, then Confirm on a different button, within
TICKET_S), and the engine itself holds the key for them.

How it arms (no new way to arm)
===============================

Every arm is the engine's own hold to arm handler (remote.Remote.arm_hold),
the one the rack screen's Hold to arm button posts to, with all its rules.
The Stream Deck program presses the group's key exactly as for a held
screen button, with its re-arm wait, the latch and the operator gate, and
flamesafe applies all of its own. This is the cleaning's walk-away
(cleaning.py, WALK_AWAY_PHASE1) for one arm: the watcher calls arm_hold on
behalf of the person whose Confirm started it, with that request's context
and the engine's own time, every BEAT_S until the deck takes it.

  1. arm-request {group, seen}: every rule a hold's first press is checked
     against is checked now. The answer is a one-use ticket, good for
     TICKET_S, for this device, this person and this group only.
  2. arm-confirm {group, ticket, seen}: the same device, the same person,
     the same group, no sooner than CONFIRM_MIN_S after the request (one
     tap is never both) and no later than TICKET_S. The ticket is spent
     whatever comes of it. Every rule is checked again, then the first
     press of a hold is made with the PAGE's status time, so a Stop or a
     disarm that came after the status the person was looking at refuses
     it (remote.ARM_CUT).
  3. The watcher beats that hold, and only that hold, until the Stream
     Deck program takes it or DRIVE_MAX_S is up. It never starts a hold:
     a hold that was cut (a Stop, a disarm, a sign out, arming switched
     off) is refused on its next beat and the run ends, and so is a beat
     after any cut stamped since the Confirm, whatever became of the hold.
     Lifting a finger does not end it: Disarm and Stop do. A Hold (the
     schedule's) cuts no hold to arm, a finger's or this one: it never
     has.

What it never does: arm with one tap, arm for a different device or person
than the one that asked, carry on after a cut, or keep trying past
DRIVE_MAX_S. Pure logic with injected handlers and clocks.
"""
import hmac
import secrets
import threading
import time

TICKET_S = 5.0          # an Arm tap waits this long for its Confirm
CONFIRM_MIN_S = 0.3     # a Confirm sooner than this is the same tap
DRIVE_MAX_S = 3.0       # the engine holds the key this long at most
BEAT_S = 0.1            # remote.ARM_BEAT_PERIOD_S: the watcher's beat
KEEP_S = 15.0           # how long an ended run's words stay readable
ACTION = "arm confirm"

NO_TICKET = ("That Arm was not waiting for a Confirm from this screen. "
             "Tap Arm again. Nothing was armed.")
TOO_LATE = (f"Confirm came more than {TICKET_S:g} s after Arm. Tap Arm "
            f"again. Nothing was armed.")
TOO_SOON = ("Confirm came with the Arm tap itself. Tap Arm, then Confirm. "
            "Nothing was armed.")
TIMED_OUT = (f"The Stream Deck program did not take the arm within "
             f"{DRIVE_MAX_S:g} s. Nothing was armed by this press. Check "
             f"the group's lamp.")
ELSEWHERE = ("The group was armed or asked for by another screen or the "
             "deck, not by this press. Check the group's lamp.")
SIGNED_OUT = "Signed out before the arm was taken. Nothing was armed."
ARMED_ANYWAY = ("{g} is armed or asked for, though this press was not "
                "told so. Check the group's lamp, and Disarm it if it "
                "should not be.")
CUT = ("A Stop or a disarm came after Confirm. Nothing was armed by this "
       "press.")
BROKE = "The arm could not be held. Nothing was armed by this press."
STATES = ("holding", "armed", "elsewhere", "refused", "timeout")


class ConfirmArm:
    """One instance in the engine. `press(body, ctx)` is Remote.arm_hold,
    `release(i, token)` drops that device's hold on group i,
    `precheck(i, name, ctx, seen_ms, since)` is "" or why a hold could
    not start now (`since`: this clock at the Arm tap, None at the tap),
    `session_live(ctx)` is False once the person signed out,
    `cut_since(i, since)` is True when a Stop or a disarm cut group i's
    holds at or after `since` on this clock, `asked(i)` is True while
    flamesafe reports group i armed or asked for."""

    def __init__(self, press, release, precheck,
                 session_live=lambda ctx: True,
                 cut_since=lambda i, since: False,
                 asked=lambda i: False, journal=None,
                 clock=time.monotonic, wall=time.time, threaded=True,
                 new_ticket=lambda: secrets.token_urlsafe(18)):
        self._press, self._release = press, release
        self._precheck, self._session_live = precheck, session_live
        self._cut_since, self._asked = cut_since, asked
        self._journal = journal
        self._clock, self._wall = clock, wall
        self._new_ticket = new_ticket
        self._lock = threading.RLock()
        self._tickets = {}      # group index -> ticket record
        self._runs = {}         # group index -> run record
        self._ids = 0
        self._stop = threading.Event()
        self._thread = None
        if threaded:
            self._thread = threading.Thread(
                target=self._watch, name="ltcplay-confirm-arm", daemon=True)
            self._thread.start()

    # -- journal -----------------------------------------------------------
    def _note(self, who, screen, outcome, text):
        if self._journal is None:
            return
        try:
            self._journal(who, screen, ACTION, outcome, text)
        except Exception:                            # noqa: BLE001
            pass

    # -- step 1: the Arm tap ----------------------------------------------
    def request(self, i, name, ctx, who, screen, seen):
        now = self._clock()
        token = ctx.device
        with self._lock:
            r = self._runs.get(i)
            if r is not None and r["state"] == "holding":
                return 409, {"error": f"{r['who']} is already arming "
                                      f"{name}. Nothing was changed."}
            t = self._tickets.get(i)
            if t is not None and now - t["at"] <= TICKET_S and \
                    t["token"] != token:
                return 409, {"error": f"{t['who']} is already arming "
                                      f"{name}. Nothing was changed."}
        why = self._precheck(i, name, ctx, seen, None)
        if why:
            self._note(who, screen, "refused",
                       f"{who}'s Arm tap for {name} on the {screen} was "
                       f"refused. {why}")
            return 409, {"error": why}
        ticket = str(self._new_ticket())
        with self._lock:
            self._tickets[i] = {"ticket": ticket, "token": token,
                                "who": who, "screen": screen, "at": now,
                                "group": name,
                                "session": str(getattr(ctx, "token", "")
                                               or "")}
        self._note(who, screen, "asked",
                   f"{who} tapped Arm for {name} on the {screen}. Nothing "
                   f"arms without a Confirm within {TICKET_S:g} s.")
        return 200, {"ok": True, "ticket": ticket, "expires_s": TICKET_S,
                     "min_s": CONFIRM_MIN_S, "group": name}

    # -- step 2: the Confirm tap ------------------------------------------
    def confirm(self, i, name, ctx, who, screen, seen, ticket):
        now = self._clock()
        token = ctx.device
        with self._lock:
            t = self._tickets.get(i)
            mine = (t is not None and isinstance(ticket, str) and
                    hmac.compare_digest(t["ticket"].encode("utf-8"),
                                        ticket.encode("utf-8")) and
                    t["token"] == token and t["who"] == who and
                    t["session"] == str(getattr(ctx, "token", "") or ""))
            if mine:
                # Spent whatever comes of it: a ticket is never tried
                # twice. Anyone else's Confirm spends nothing: it cannot
                # take the Arm tap away from the one who made it.
                del self._tickets[i]

        def refuse(why):
            if mine:
                self._note(who, screen, "refused",
                           f"{who}'s Confirm to arm {name} on the {screen} "
                           f"was refused. {why}")
            return 409, {"error": why}
        if not mine:
            return refuse(NO_TICKET)
        age = now - t["at"]
        if age > TICKET_S:
            return refuse(TOO_LATE)
        if age < CONFIRM_MIN_S:
            return refuse(TOO_SOON)
        why = self._precheck(i, name, ctx, seen, t["at"])
        if why:
            return refuse(why)
        # The first press of the hold, with the page's own status time: a
        # Stop or a disarm after that status refuses it (ARM_CUT).
        try:
            code, out = self._press({"group": i, "seen": seen}, ctx)
        except Exception as e:                       # noqa: BLE001
            return refuse(f"The arm could not be asked for "
                          f"({type(e).__name__}). Nothing was armed.")
        out = out if isinstance(out, dict) else {}
        if code != 200 or out.get("hold_id") is None:
            return code if code != 200 else 409, {
                "error": str(out.get("error") or "The arm was refused. "
                                                 "Nothing was armed.")}
        with self._lock:
            self._ids += 1
            run = {"id": self._ids, "group": i, "name": name, "ctx": ctx,
                   "token": token, "who": who, "screen": screen,
                   "hold_id": out["hold_id"], "start": now,
                   "deadline": now + DRIVE_MAX_S, "state": "holding",
                   "text": "", "ended": None}
            self._runs[i] = run
        self._note(who, screen, "confirmed",
                   f"{who} confirmed the arm of {name} on the {screen}. "
                   f"The engine holds its key for {DRIVE_MAX_S:g} s at "
                   f"most; the Stream Deck program arms it only if every "
                   f"rule allows it.")
        return 200, {"ok": True, "run": run["id"], "state": "holding",
                     "group": name, "max_s": DRIVE_MAX_S}

    # -- step 3: the watcher's beats ---------------------------------------
    def _end(self, r, state, text, now, let_go=False):
        if state in ("refused", "timeout"):
            # Never "nothing was armed" over a lamp that says armed: the
            # Stream Deck program may have taken it as this run ended.
            try:
                asked = self._asked(r["group"]) is True
            except Exception:                        # noqa: BLE001
                asked = False
            if asked:
                text = ARMED_ANYWAY.format(g=r["name"])
        r["state"], r["text"], r["ended"] = state, text, now
        hold_id, r["hold_id"] = r["hold_id"], None
        if let_go and hold_id is not None:
            try:
                self._release(r["group"], r["token"])
            except Exception:                        # noqa: BLE001
                pass
        self._note(r["who"], r["screen"], state,
                   f"{r['who']}'s confirmed arm of {r['name']} on the "
                   f"{r['screen']} ended: {state}. {text}".rstrip())

    def tick(self, now=None):
        """One beat of every run under way. Never starts a hold."""
        now = self._clock() if now is None else now
        with self._lock:
            for i, t in list(self._tickets.items()):
                if now - t["at"] > TICKET_S:
                    del self._tickets[i]
            for i, r in list(self._runs.items()):
                if r["state"] != "holding":
                    if now - (r["ended"] or now) > KEEP_S:
                        del self._runs[i]
                    continue
                try:
                    self._beat(r, now)
                except Exception:                    # noqa: BLE001
                    if r["state"] == "holding":
                        self._end(r, "refused", BROKE, now, let_go=True)

    def _beat(self, r, now):
        if r["hold_id"] is None:
            # No hold of this run's own to beat: a beat without one would
            # start a new hold, which only a Confirm may do.
            return self._end(r, "refused", "The hold was interrupted. "
                                           "Nothing was armed.", now)
        if now > r["deadline"]:
            return self._end(r, "timeout", TIMED_OUT, now, let_go=True)
        if self._cut_since(r["group"], r["start"]):
            return self._end(r, "refused", CUT, now, let_go=True)
        if not self._session_live(r["ctx"]):
            return self._end(r, "refused", SIGNED_OUT, now, let_go=True)
        body = {"group": r["group"], "hold_id": r["hold_id"],
                "seen": int(self._wall() * 1000)}
        try:
            code, out = self._press(body, r["ctx"])
        except Exception as e:                       # noqa: BLE001
            return self._end(r, "refused",
                             f"The arm could not be held "
                             f"({type(e).__name__}). Nothing was armed.",
                             now, let_go=True)
        out = out if isinstance(out, dict) else {}
        if code != 200:
            # arm_hold let the hold go itself (a Stop, a disarm, a rule).
            r["hold_id"] = None
            return self._end(r, "refused",
                             str(out.get("error") or "The arm was refused. "
                                                     "Nothing was armed."),
                             now)
        if out.get("armed"):
            r["hold_id"] = None
            if out.get("taken") is True:
                return self._end(r, "armed", str(out.get("text") or ""),
                                 now)
            return self._end(r, "elsewhere", ELSEWHERE, now)
        if out.get("hold_id") != r["hold_id"]:
            # Not this run's hold any more: never adopt another.
            r["hold_id"] = None
            return self._end(r, "refused", "The hold was interrupted. "
                                           "Nothing was armed.", now)

    # -- what a page reads -------------------------------------------------
    def view(self, device=None):
        """[{group, name, run, state, text, mine}] for runs under way or
        just ended, and this device's own waiting tickets (never the
        ticket itself)."""
        now = self._clock()
        with self._lock:
            runs = [{"group": r["group"], "name": r["name"],
                     "run": r["id"], "state": r["state"],
                     "text": r["text"], "who": r["who"],
                     "mine": device is not None and r["token"] == device}
                    for r in self._runs.values()]
            waiting = [{"group": i, "name": t["group"], "who": t["who"],
                        "left_s": round(max(0.0, TICKET_S -
                                            (now - t["at"])), 1),
                        "mine": device is not None and
                        t["token"] == device}
                       for i, t in self._tickets.items()
                       if now - t["at"] <= TICKET_S]
        return {"runs": runs, "waiting": waiting, "ticket_s": TICKET_S,
                "min_s": CONFIRM_MIN_S, "max_s": DRIVE_MAX_S}

    def active(self):
        with self._lock:
            return any(r["state"] == "holding" for r in self._runs.values())

    # -- the thread --------------------------------------------------------
    def _watch(self):
        while not self._stop.wait(BEAT_S):
            try:
                self.tick()
            except Exception:                        # noqa: BLE001
                pass

    def close(self):
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=1.0)
