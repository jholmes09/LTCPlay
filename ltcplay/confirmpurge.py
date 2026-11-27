"""Tap, then confirm, for a timed purge from a phone (Jeff, 2026-11-27).

"Your solution for Purge is approved. Finger holds on mobile have been
proven to fail." A timed purge from a page needs the page's open beat
every 500 ms while its machines are armed, and a 1 s start hold with no
gap over 0.25 s. Neither survives a phone on park wifi. Here the person's
intent is two deliberate taps (Purge, then Confirm on a different button,
within TICKET_S), and the engine runs the whole purge for them.

How it purges (no new way to purge)
===================================

Every step is the Startup page's own route, through the engine's own
handler (remote.Remote.morning_post) with every rule it has: the purge
switches, the Stream Deck program reading the engine, no show live, the
lockout, Arrange mode, one purge at a time, the longest timed purge this
show machine allows, the flame link's hard end. The Stream Deck program
arms the selection's groups exactly as for a click on the Startup page.

  1. purge-request {machines, seconds, seen}: every rule that can be
     checked before anything is selected. The answer is a one-use ticket,
     good for TICKET_S, for this device and this person, for exactly these
     machines and this many seconds.
  2. purge-confirm {ticket, seen}: the same device, the same person, no
     sooner than CONFIRM_MIN_S and no later than TICKET_S. The ticket is
     spent whatever comes of it. The rules are checked again; then the
     machines are selected (purge-select), all of them or none.
  3. The watcher keeps that page open (morning-open) while it waits, at
     most ARM_WAIT_S, for flamesafe to report every selected machine's
     group ARMED; then it beats the timed purge's own start hold
     (purge-timed) until the engine starts it.
  4. The timed purge runs on the engine's clock and stops by itself, as it
     always did, with its page closed. When it is over the watcher clears
     the selection (purge-clear), so the Stream Deck program lets the
     groups go.

It is bounded at every step: ARM_WAIT_S to arm, HOLD_MAX_S to start, the
purge's own seconds, then the clear. Any refusal, a sign out, the
selection taken away (a Stop, a Hold, Disarm all, a Clear from the phone or
the show machine) ends the run and clears what it selected. It never
selects again and never starts a second purge.

Pure logic with injected handlers and clocks.
"""
import hmac
import secrets
import threading
import time

TICKET_S = 5.0          # a Purge tap waits this long for its Confirm
CONFIRM_MIN_S = 0.3     # a Confirm sooner than this is the same tap
ARM_WAIT_S = 5.0        # the selection's groups armed within this, or off
HOLD_MAX_S = 3.0        # the start hold's beats, at most (it needs 1 s)
OPEN_S = 0.3            # the watcher's page-open beat (the page's: 0.5 s)
BEAT_S = 0.1            # the watcher's pass, and the start hold's beat
END_SLACK_S = 2.0       # past the purge's own seconds: Stop purge is sent
KEEP_S = 15.0           # how long an ended run's words stay readable
ACTION = "purge confirm"

NO_TICKET = ("That Purge was not waiting for a Confirm from this screen. "
             "Tap Purge again. Nothing was fired.")
TOO_LATE = (f"Confirm came more than {TICKET_S:g} s after Purge. Tap Purge "
            f"again. Nothing was fired.")
TOO_SOON = ("Confirm came with the Purge tap itself. Tap Purge, then "
            "Confirm. Nothing was fired.")
BUSY = "A purge is already asked for or running. One purge at a time."
NOT_ARMED = (f"flamesafe did not report every selected group armed within "
             f"{ARM_WAIT_S:g} s. Nothing was fired, and the selection was "
             f"cleared.")
NOT_STARTED = (f"The timed purge did not start within {HOLD_MAX_S:g} s. "
               f"Nothing was fired, and the selection was cleared.")
TAKEN_AWAY = ("The purge selection was cleared before the purge started "
              "(a Stop, a Hold, a disarm or a Clear). Nothing was fired.")
SIGNED_OUT = ("Signed out before the purge started. Nothing was fired, and "
              "the selection was cleared.")
BROKE = ("The purge could not be carried on. It was stopped and the "
         "selection was cleared.")
NOT_SELECTED = ("Not every machine could be selected for the purge, so "
                "none was. Nothing was fired.")
STATES = ("arming", "holding", "firing", "done", "refused", "timeout",
          "stopped")
LIVE = ("arming", "holding", "firing")


class ConfirmPurge:
    """One instance in the engine. `post(name, body, ctx)` is
    Remote.morning_post; `precheck(ctx, ids, seconds, seen_ms)` answers
    (why, whole seconds); `armed(ids)` is True once flamesafe reports every
    one of those machines' groups ARMED on a fresh status; `selected(ids,
    device)` is True while that device still owns a selection holding all
    of them; `running()` is True while a timed purge runs; `last()` is the
    engine's sentence on how the last one ended."""

    def __init__(self, post, precheck, armed, selected, running,
                 last=lambda: "", session_live=lambda ctx: True,
                 journal=None, clock=time.monotonic, wall=time.time,
                 threaded=True,
                 new_ticket=lambda: secrets.token_urlsafe(18)):
        self._post, self._precheck = post, precheck
        self._armed, self._selected = armed, selected
        self._running, self._last = running, last
        self._session_live = session_live
        self._journal = journal
        self._clock, self._wall = clock, wall
        self._new_ticket = new_ticket
        self._lock = threading.RLock()
        self._ticket = None
        self._run = None
        self._ids = 0
        self._stop = threading.Event()
        self._thread = None
        if threaded:
            self._thread = threading.Thread(
                target=self._watch, name="ltcplay-confirm-purge",
                daemon=True)
            self._thread.start()

    def _note(self, who, screen, outcome, text):
        if self._journal is None:
            return
        try:
            self._journal(who, screen, ACTION, outcome, text)
        except Exception:                            # noqa: BLE001
            pass

    def _seen(self):
        return int(self._wall() * 1000)

    def _ask(self, name, body, ctx):
        """One of the Startup page's own routes: (code, body dict)."""
        try:
            code, out = self._post(name, dict(body, seen=self._seen()), ctx)
        except Exception as e:                       # noqa: BLE001
            return 500, {"error": f"{name} could not be asked "
                                  f"({type(e).__name__})."}
        return code, out if isinstance(out, dict) else {}

    def _busy(self, now, token=None):
        r, t = self._run, self._ticket
        if r is not None and r["state"] in LIVE:
            return True
        return t is not None and now - t["at"] <= TICKET_S and \
            t["token"] != token

    # -- step 1: the Purge tap ---------------------------------------------
    def request(self, ctx, who, screen, ids, seconds, label, seen):
        now = self._clock()
        token = ctx.device
        with self._lock:
            if self._busy(now, token):
                return 409, {"error": BUSY}
        why, secs = self._precheck(ctx, ids, seconds, seen)
        if why:
            self._note(who, screen, "refused",
                       f"{who}'s Purge tap on the {screen} was refused. "
                       f"{why}")
            return 409, {"error": why}
        ids = [str(x) for x in ids]
        ticket = str(self._new_ticket())
        with self._lock:
            self._ticket = {"ticket": ticket, "token": token, "who": who,
                            "screen": screen, "at": now, "ids": ids,
                            "seconds": secs, "label": str(label or "")[:64],
                            "session": str(getattr(ctx, "token", "")
                                           or "")}
        self._note(who, screen, "asked",
                   f"{who} tapped Purge on the {screen}: "
                   f"{', '.join(ids)} for {secs} s. Nothing is armed or "
                   f"fired without a Confirm within {TICKET_S:g} s.")
        return 200, {"ok": True, "ticket": ticket, "expires_s": TICKET_S,
                     "min_s": CONFIRM_MIN_S, "machines": ids,
                     "seconds": secs}

    # -- step 2: the Confirm tap -------------------------------------------
    def confirm(self, ctx, who, screen, ticket, seen):
        now = self._clock()
        token = ctx.device
        with self._lock:
            t = self._ticket
            mine = (t is not None and isinstance(ticket, str) and
                    hmac.compare_digest(t["ticket"].encode("utf-8"),
                                        ticket.encode("utf-8")) and
                    t["token"] == token and t["who"] == who and
                    t["session"] == str(getattr(ctx, "token", "") or ""))
            if mine:
                # Spent whatever comes of it: a ticket is never tried
                # twice. Anyone else's Confirm spends nothing.
                self._ticket = None
            live = self._run is not None and self._run["state"] in LIVE

        def refuse(why):
            if mine:
                self._note(who, screen, "refused",
                           f"{who}'s Confirm to purge on the {screen} was "
                           f"refused. {why}")
            return 409, {"error": why}
        if not mine:
            return refuse(NO_TICKET)
        if live:
            return refuse(BUSY)
        age = now - t["at"]
        if age > TICKET_S:
            return refuse(TOO_LATE)
        if age < CONFIRM_MIN_S:
            return refuse(TOO_SOON)
        why, secs = self._precheck(ctx, t["ids"], t["seconds"], seen)
        if why:
            return refuse(why)
        if secs != t["seconds"]:
            return refuse(NO_TICKET)
        with self._lock:
            self._ids += 1
            run = {"id": self._ids, "ctx": ctx, "token": token, "who": who,
                   "screen": screen, "ids": list(t["ids"]),
                   "label": t["label"], "seconds": secs, "state": "arming",
                   "text": "", "start": now, "ended": None,
                   "arm_by": now + ARM_WAIT_S, "hold_by": None,
                   "end_by": None, "open_at": None, "open_seq": 0,
                   "rid": f"confirm-{self._ids}", "seq": 0,
                   "inst": f"confirm-{self._ids}"}
            self._run = run
            self._open(run, now)
            body = {"machines": run["ids"], "on": True}
            if run["label"]:
                body["group"] = run["label"]
            code, out = self._ask("purge-select", body, ctx)
            got = {str(x) for x in (out.get("selected") or [])}
            if code != 200 or not set(run["ids"]) <= got:
                why = str(out.get("error") or out.get("text") or
                          NOT_SELECTED)
                if code == 200:
                    why = f"{NOT_SELECTED} {why}".strip()
                self._end(run, "refused", why, now, clear=True)
                return 409, {"error": why}
        self._note(who, screen, "confirmed",
                   f"{who} confirmed a timed purge on the {screen}: "
                   f"{', '.join(run['ids'])} for {secs} s. The engine "
                   f"keeps the selection, starts the purge once every "
                   f"group is armed and clears the selection after it.")
        return 200, {"ok": True, "run": run["id"], "state": "arming",
                     "machines": run["ids"], "seconds": secs}

    # -- step 3: the watcher ------------------------------------------------
    def _open(self, r, now):
        """The page-open beat the selection lives on, in the phone's name."""
        if r["open_at"] is not None and now - r["open_at"] < OPEN_S:
            return
        r["open_at"] = now
        r["open_seq"] += 1
        self._ask("morning-open", {"arranging": False, "inst": r["inst"],
                                   "seq": r["open_seq"]}, r["ctx"])

    def _end(self, r, state, text, now, clear=False, stop=False):
        r["state"], r["text"], r["ended"] = state, text, now
        if stop:
            self._ask("purge-stop", {}, r["ctx"])
        if clear:
            self._ask("purge-clear", {}, r["ctx"])
        self._note(r["who"], r["screen"], state,
                   f"{r['who']}'s confirmed purge on the {r['screen']} "
                   f"ended: {state}. {text}".rstrip())

    def tick(self, now=None):
        """One pass. Never selects, and never starts a second purge."""
        now = self._clock() if now is None else now
        with self._lock:
            t = self._ticket
            if t is not None and now - t["at"] > TICKET_S:
                self._ticket = None
            r = self._run
            if r is None:
                return
            if r["state"] not in LIVE:
                if now - (r["ended"] or now) > KEEP_S:
                    self._run = None
                return
            try:
                self._pass(r, now)
            except Exception:                        # noqa: BLE001
                # Never stranded: stopped, cleared, said.
                if r["state"] in LIVE:
                    self._end(r, "refused", BROKE, now, clear=True,
                              stop=r["state"] == "firing")

    def _pass(self, r, now):
        if r["state"] == "firing":
            return self._firing(r, now)
        if not self._session_live(r["ctx"]):
            return self._end(r, "refused", SIGNED_OUT, now, clear=True)
        if not self._selected(r["ids"], r["token"]):
            # Taken away by something that outranks this run. Nothing of
            # it is selected again.
            return self._end(r, "stopped", TAKEN_AWAY, now, clear=True)
        self._open(r, now)
        if r["state"] == "arming":
            if not self._armed(r["ids"]):
                if now > r["arm_by"]:
                    self._end(r, "timeout", NOT_ARMED, now, clear=True)
                return
            r["state"] = "holding"
            r["hold_by"] = now + HOLD_MAX_S
        self._hold(r, now)

    def _hold(self, r, now):
        if now > r["hold_by"]:
            self._ask("purge-timed", {"hold": False, "run": r["rid"]},
                      r["ctx"])
            return self._end(r, "timeout", NOT_STARTED, now, clear=True)
        r["seq"] += 1
        code, out = self._ask("purge-timed",
                              {"hold": True, "run": r["rid"],
                               "seq": r["seq"], "seconds": r["seconds"]},
                              r["ctx"])
        if code != 200:
            return self._end(r, "refused",
                             str(out.get("error") or "The timed purge was "
                                 "refused. Nothing was fired."),
                             now, clear=True)
        if out.get("started") is True:
            r["state"] = "firing"
            r["end_by"] = now + r["seconds"] + END_SLACK_S
            r["text"] = str(out.get("text") or "")

    def _firing(self, r, now):
        if self._running():
            if now > r["end_by"]:
                # Never expected (the engine's timer and the flame link's
                # hard end both ended it long ago): said, and stopped.
                self._end(r, "stopped", "The timed purge ran past its "
                          "seconds and was stopped.", now, clear=True,
                          stop=True)
            return
        try:
            said = str(self._last() or "")
        except Exception:                            # noqa: BLE001
            said = ""
        self._end(r, "done", said, now, clear=True)

    # -- what a page reads --------------------------------------------------
    def view(self, device=None):
        now = self._clock()
        with self._lock:
            r, t = self._run, self._ticket
            run = None if r is None else {
                "run": r["id"], "state": r["state"], "text": r["text"],
                "machines": list(r["ids"]), "label": r["label"],
                "seconds": r["seconds"], "who": r["who"],
                "mine": device is not None and r["token"] == device}
            waiting = None
            if t is not None and now - t["at"] <= TICKET_S:
                waiting = {"machines": list(t["ids"]), "label": t["label"],
                           "seconds": t["seconds"], "who": t["who"],
                           "left_s": round(max(0.0, TICKET_S -
                                               (now - t["at"])), 1),
                           "mine": device is not None and
                           t["token"] == device}
        return {"run": run, "waiting": waiting, "ticket_s": TICKET_S,
                "min_s": CONFIRM_MIN_S, "arm_wait_s": ARM_WAIT_S}

    def active(self):
        with self._lock:
            return self._run is not None and self._run["state"] in LIVE

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
