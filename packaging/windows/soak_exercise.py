"""BENCH BUILD ONLY: the soak's flame-arming exerciser and its judge of
flamesafe's output (Jeff, 2026-10-04: the show PC is isolated, so simulate
everything).

The exerciser presses only the real paths, with the real consent rules:

  - it signs in on the rack screen's own routes with an operator's PIN and
    picks that operator, exactly as a person on the show machine would;
  - it arms each flame group by HOLDING its Arm button on the screen
    (/api/remote/arm-hold, a heartbeat every 100 ms, then arm-release):
    a remote press of the group's key on the real Stream Deck program,
    which owns flamesafe's arm link (the soak runs that program with a
    virtual deck when no Stream Deck is plugged in). The deck's own rules
    (operator chosen, hold time, re-arm refractory, nothing while an Abort
    stands) and flamesafe's (cycle the arm after an Abort) all apply;
  - Hold, Resume, Abort and Reset go through the same screen routes a
    person presses.

Each show (the bench schedule's 100 s show): arm every group not armed at
ARM_AT_S into the show, Hold at HOLD_AT_S for HOLD_FOR_S, then Resume; on
every ABORT_EVERY-th show, Abort at ABORT_AT_S, then Reset once the
conductor takes it. The next show arms the groups again.

SacnJudge judges every flamesafe sACN packet against what it may carry:
a group's channels non-zero only while flamesafe reports that group armed,
and its fire channels non-zero only during a show, never inside a Hold,
never after an Abort until it has been re-armed, never outside a show.
Fire packets are counted per group. Everything else is a violation, with
its time.
"""
import json
import threading
import time
import urllib.error
import urllib.request

OPERATOR = "Andy"
PIN = "2468"
ARM_AT_S = 8.0
HOLD_AT_S = 30.0
HOLD_FOR_S = 5.0
ABORT_AT_S = 60.0
ABORT_EVERY = 2          # shows 2, 4, 6, ...
BEAT_S = 0.1             # the page's own heartbeat while a finger is down
GRACE_S = 0.3            # a change reaching flamesafe and back
REFRACTORY_S = 2.5       # the deck's re-arm refractory (2 s) and a margin
STALE_TC_S = 1.0         # timecode silent this long: not in a show


class Http:
    """JSON over HTTP to the engine on this machine, with the session
    cookie once signed in."""

    def __init__(self, base, timeout=5.0):
        self.base = base
        self.timeout = timeout
        self.cookie = None

    def __call__(self, method, path, body=None):
        data = None if method == "GET" else json.dumps(body or {}).encode()
        h = {"Content-Type": "application/json"}
        if self.cookie:
            h["Cookie"] = self.cookie
        req = urllib.request.Request(self.base + path, data=data, headers=h,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                sc = r.headers.get("Set-Cookie")
                if sc:
                    self.cookie = sc.split(";")[0]
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"null")
            except ValueError:
                return e.code, {"error": f"HTTP {e.code}"}
        except Exception as e:
            return 0, {"error": f"{type(e).__name__}: {e}"}


class Exerciser:
    """Runs on its own thread. `http(method, path, body)` -> (status, doc);
    `show_time()` -> seconds into the current show, or None between shows;
    `show_number()` -> how many shows have started; `armed()` -> {group
    name: True/False} from flamesafe's own status; `note(text)`."""

    def __init__(self, http, groups, show_time, show_number, armed, note,
                 clock=time.monotonic, sleep=time.sleep):
        self.http = http
        self.groups = list(groups)
        self.show_time = show_time
        self.show_number = show_number
        self.armed = armed
        self.note = note
        self.clock = clock
        self.sleep = sleep
        self.stop = threading.Event()
        self.counts = {"arm_tries": 0, "arms": 0, "cycles": 0, "holds": 0,
                       "resumes": 0, "aborts": 0, "resets": 0}
        self.arms_by_group = {g: 0 for g in self.groups}
        self.rearmed = {}            # group -> wall time it was last armed
        self.hold_tails = {}         # hold window start -> timings
        self.failures = []           # (time, sentence)
        self.windows = []            # (start, end or None, why): no fire
        self.ready = False
        self._done = {}              # show number -> set of steps done
        self.thread = None

    # -- the screen ---------------------------------------------------------
    def _seen(self):
        st, doc = self.http("GET", "/api/remote/status")
        return doc.get("served_at") if st == 200 and isinstance(doc, dict) \
            else None

    def _fail(self, what, doc):
        why = (doc or {}).get("error") if isinstance(doc, dict) else doc
        self.failures.append((time.time(), f"{what}: {why}"))
        self.note(f"exerciser: {what} failed: {why}")

    def sign_in(self):
        st, doc = self.http("POST", "/api/remote/pin",
                            {"who": OPERATOR, "pin": PIN})
        if st != 200:
            self._fail("setting the operator's PIN", doc)
            return False
        st, doc = self.http("POST", "/api/remote/login",
                            {"who": OPERATOR, "pin": PIN,
                             "device": "Rack screen"})
        if st != 200:
            self._fail("signing in on the rack screen", doc)
            return False
        st, doc = self.http("POST", "/api/remote/operator",
                            {"pick": OPERATOR, "seen": self._seen()})
        if st != 200:
            self._fail("picking the operator", doc)
            return False
        self.note(f"exerciser: signed in on the rack screen as {OPERATOR} "
                  f"and picked {OPERATOR} as the operator")
        self.ready = True
        return True

    def press(self, route, **extra):
        body = dict(extra, seen=self._seen())
        return self.http("POST", "/api/remote/" + route, body)

    def arm(self, i):
        """Hold group i's Arm button on the screen until flamesafe reports
        it armed, then let go. A group still asked for but not armed (an
        Abort from a screen disarmed it at flamesafe; the deck still wants
        it) is cycled first, as an operator does: Disarm, wait out the
        deck's re-arm refractory window, then hold again."""
        name = self.groups[i]
        self.counts["arm_tries"] += 1
        ok = self._hold(i, name)
        if ok == "cycle":
            self.counts["cycles"] += 1
            self.http("POST", "/api/remote/group-disarm",
                      {"group": i, "seen": self._seen()})
            self.sleep(REFRACTORY_S)
            ok = self._hold(i, name)
        ok = ok is True
        if ok:
            self.counts["arms"] += 1
            self.arms_by_group[name] += 1
            self.rearmed[name] = time.time()
        elif not self.failures or "Arm button" not in self.failures[-1][1]:
            self._fail(f"arming {name}", {"error": "flamesafe never "
                                                   "reported it armed"})
        return ok

    def _hold(self, i, name):
        """True once armed, "cycle" when it is asked for but held, else
        False."""
        hold_id = None
        t0 = self.clock()
        ok = False
        while self.clock() - t0 < 4.0 and not self.stop.is_set():
            body = {"group": i, "seen": self._seen()}
            if hold_id is not None:
                body["hold_id"] = hold_id
            st, doc = self.http("POST", "/api/remote/arm-hold", body)
            if st != 200:
                err = str((doc or {}).get("error", ""))
                if self.armed().get(name) or "asked for" in err:
                    # The deck took the hold and asked flamesafe to arm:
                    # the hold is over. flamesafe reports armed once its
                    # own arm dwell has passed; if it never does, the group
                    # was asked for already and needs a cycle.
                    ok = self._wait_armed(name) or (
                        "cycle" if hold_id is None else False)
                    break
                self._fail(f"holding {name}'s Arm button", doc)
                break
            hold_id = doc.get("hold_id", hold_id)
            if self.armed().get(name):
                ok = True
                break
            self.sleep(BEAT_S)
        self.http("POST", "/api/remote/arm-release", {"group": i})
        if ok is False and self.armed().get(name):
            ok = True
        return ok

    def _wait_armed(self, name, within=4.0):
        t0 = self.clock()
        while self.clock() - t0 < within and not self.stop.is_set():
            if self.armed().get(name):
                return True
            self.sleep(BEAT_S)
        return bool(self.armed().get(name))

    # -- one pass of the plan -----------------------------------------------
    def step(self):
        t = self.show_time()
        if t is None:
            return
        k = self.show_number()
        done = self._done.setdefault(k, set())
        if t >= ARM_AT_S and "arm" not in done:
            done.add("arm")
            for i, g in enumerate(self.groups):
                if not self.armed().get(g):
                    self.arm(i)
            self._close("abort")       # re-armed: an Abort's window ends
        if t >= HOLD_AT_S and "hold" not in done:
            done.add("hold")
            sent = time.time()
            st, doc = self.press("hold")
            if st == 200:
                self.counts["holds"] += 1
                start = time.time()
                self.windows.append([start, None, "hold"])
                # How long fire went on after the press (diagnostics, show
                # PC 2026-10-04): the Hold's flame cut runs on the show
                # conductor's own thread after the press returns.
                self.hold_tails[start] = {"sent": sent, "back": start,
                                          "last_fire": None}
                self.sleep(HOLD_FOR_S)
                st, doc = self.press("resume")
                if st == 200:
                    self.counts["resumes"] += 1
                else:
                    self._fail("Resume", doc)
                self._close("hold")
            else:
                self._fail("Hold", doc)
        if t >= ABORT_AT_S and k % ABORT_EVERY == 0 and "abort" not in done:
            done.add("abort")
            st, doc = self.press("abort", confirmed=True)
            if st != 200:
                self._fail("Abort", doc)
                return
            self.counts["aborts"] += 1
            now = time.time()
            self.windows.append([now, None, "abort"])
            self.windows.append([now, None, "aborted"])     # until Reset
            t0 = self.clock()
            while self.clock() - t0 < 15 and not self.stop.is_set():
                self.sleep(1.0)
                st, doc = self.press("reset")
                if st == 200:
                    self.counts["resets"] += 1
                    self._close("aborted")
                    break
            else:
                self._fail("Reset", doc)

    def _close(self, why):
        for w in self.windows:
            if w[2] == why and w[1] is None:
                w[1] = time.time()

    def run(self):
        while not self.stop.is_set() and not self.ready:
            if self.sign_in():
                break
            self.stop.wait(5.0)
        while not self.stop.is_set():
            try:
                self.step()
            except Exception as e:
                self._fail("the exerciser itself", {"error": repr(e)})
            self.stop.wait(0.5)

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True,
                                       name="soak-exerciser")
        self.thread.start()
        return self

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(10)

    def fire_quiet(self, name, at, armed_since=None):
        """Why group `name` may not fire at wall time `at`, or None: inside
        a Hold, or after an Abort until that group has been armed again.
        `armed_since`: when flamesafe's own status last turned it armed (it
        can fire a moment before the exerciser's hold returns)."""
        rearmed = max(self.rearmed.get(name, 0), armed_since or 0)
        for s, e, why in self.windows:
            h = self.hold_tails.get(s) if why == "hold" else None
            if h is not None and at >= h["sent"] and (e is None or at <= e):
                if h["last_fire"] is None or at > h["last_fire"]:
                    h["last_fire"] = at
        # A Hold names itself first: an older Abort's window is open only
        # until the group is armed again, and must never label a Hold.
        for s, e, why in self.windows:
            if why == "hold" and s + GRACE_S <= at and (e is None or at <= e):
                return "hold"
        for s, e, why in self.windows:
            if why == "abort" and s + GRACE_S <= at and rearmed < s:
                return (f"time after the Abort at "
                        f"{time.strftime('%H:%M:%S', time.localtime(s))}, "
                        f"{name} not armed again since")
        return None

    def hold_tail_line(self):
        """How long fire went on after each Hold press, worst first."""
        rows = []
        for h in self.hold_tails.values():
            if h["last_fire"] is not None:
                rows.append((h["last_fire"] - h["sent"], h))
        if not self.hold_tails:
            return "no Hold pressed"
        if not rows:
            return (f"{len(self.hold_tails)} Hold(s); no fire after any press "
                    f"(no group was firing)")
        rows.sort(key=lambda r: -r[0])
        worst, h = rows[0]
        return (f"{len(self.hold_tails)} Hold(s); fire went on at most "
                f"{worst * 1000:.0f} ms after a press was sent ("
                f"{(h['back'] - h['sent']) * 1000:.0f} ms of that waiting "
                f"for the engine to answer the press, at "
                f"{time.strftime('%H:%M:%S', time.localtime(h['sent']))}); "
                f"the judge allows {GRACE_S * 1000:.0f} ms after the answer")

    def in_window(self, at, kinds=("hold", "abort")):
        """The reason fire (by default) must be zero at wall time `at`, or
        None. The lasers' kinds are ("hold", "aborted"): dark from an Abort
        until its Reset."""
        for s, e, why in self.windows:
            if why in kinds and s + GRACE_S <= at and (e is None or at <= e):
                return why
        return None


class SacnJudge:
    """flamesafe's sACN, packet by packet. `groups`: [(name, safety slot,
    [fire slots])] from flamesafe's own config (slots are 1-based)."""

    def __init__(self, groups):
        self.groups = [(n, s, list(f)) for n, s, f in groups]
        self.fire_frames = {n: 0 for n, _s, _f in self.groups}
        self.violations = []         # (time, sentence)
        self.packets = 0
        self.nonzero = 0
        self._changed = {}           # group -> wall time its state changed
        self._last = {}
        self._mismatch = {}          # group -> [since, reported]
        # Evidence (show PC, 2026-10-04, 18:33:34): the last 2 s of what was
        # judged, written out with every violation, and every sACN source
        # seen, so a violation can be settled from the files alone.
        import collections
        self.trace = collections.deque(maxlen=80)
        self.sources = {}            # (address, universe, CID) -> packets
        self.evidence = None         # callable(lines) the soak writes
        self._status_seen = {}       # group -> (wall time, status dict)

    def armed_state(self, armed, at):
        """Remember when each group's reported state last changed."""
        for n, v in armed.items():
            if self._last.get(n) != v:
                self._last[n] = v
                self._changed[n] = at

    def packet(self, values, at, armed, in_show, quiet, src=None):
        """`values`: the 512 slot values; `armed`: {name: bool};
        `in_show`: timecode moving; `quiet`: the reason fire must be zero
        now (a Hold, an Abort), or None, or a callable(group) giving it per
        group."""
        self.packets += 1
        self.armed_state(armed, at)
        self.trace.append(self._trace_row(values, at, armed, in_show, src))
        if any(values):
            self.nonzero += 1
        mine = set()
        for n, safety, fire in self.groups:
            slots = [safety] + fire
            mine.update(slots)
            settling = at - self._changed.get(n, -1e9) < GRACE_S
            on = [s for s in slots if values[s - 1]]
            if not on or armed.get(n):
                self._mismatch.pop(n, None)
            if not on:
                continue
            if not armed.get(n) and not settling:
                # flamesafe's status and its sACN are two streams: an arm
                # shows on the wire a moment before its status says so. A
                # violation is one that lasts past GRACE_S.
                since = self._mismatch.setdefault(n, [at, False])
                if at - since[0] >= GRACE_S and not since[1]:
                    since[1] = True
                    self._bad(at, f"{n} is not armed but channel(s) "
                                  f"{on[:4]} carry values")
                continue
            fire_on = [s for s in fire if values[s - 1]]
            if not fire_on:
                continue
            self.fire_frames[n] += 1
            if not in_show:
                self._bad(at, f"{n} fired (channel {fire_on[0]}) outside "
                              f"a show")
            else:
                why = quiet(n, self._changed.get(n)) if callable(quiet) \
                    else quiet
                if why:
                    self._bad(at, f"{n} fired (channel {fire_on[0]}) during "
                                  f"the {why}")
        stray = [i + 1 for i, v in enumerate(values) if v and
                 (i + 1) not in mine]
        if stray:
            self._bad(at, f"channel(s) {stray[:4]} belong to no group but "
                          f"carry values")

    def status(self, groups, at):
        """flamesafe's own status frame, group by group, for the trace."""
        for g in groups or ():
            if isinstance(g, dict) and g.get("name"):
                self._status_seen[g["name"]] = (at, {
                    k: g.get(k) for k in ("armed", "wanted", "reason",
                                          "sent_safety", "sent_fire",
                                          "commanded_fire")})

    def source(self, addr, universe, cid):
        k = (addr, universe, cid)
        self.sources[k] = self.sources.get(k, 0) + 1

    def _trace_row(self, values, at, armed, in_show, src):
        row = [f"{at:.3f}", src or "", "show" if in_show else "no show"]
        for n, safety, fire in self.groups:
            st = self._status_seen.get(n)
            row.append(f"{n}: judged {'armed' if armed.get(n) else 'not'}"
                       f" (changed {at - self._changed.get(n, at):.3f} s "
                       f"ago); sACN safety {values[safety - 1]} fire "
                       f"{[values[f - 1] for f in fire]}; status "
                       + (f"{st[1]} {at - st[0]:.3f} s old" if st else
                          "none"))
        return " | ".join(row)

    def _bad(self, at, text):
        first = len(self.violations) < 500
        if first:
            self.violations.append((at, text))
        if self.evidence is not None and len(self.violations) <= 50:
            try:
                self.evidence([f"VIOLATION {at:.3f} {text}"]
                              + list(self.trace))
            except Exception:
                pass


def self_test():
    """Proved with fakes: no engine, no flamesafe."""
    groups = [("front row", 401, [411, 412]), ("cat-walk", 402, [421])]
    j = SacnJudge(groups)
    v = [0] * 512
    j.packet(v, 0.0, {"front row": False, "cat-walk": False}, False, None)
    assert not j.violations
    v1 = list(v)
    v1[400] = 78
    j.packet(v1, 1.0, {"front row": True, "cat-walk": False}, False, None)
    assert not j.violations, j.violations
    v2 = list(v1)
    v2[410] = 200
    j.packet(v2, 2.0, {"front row": True, "cat-walk": False}, True, None)
    assert not j.violations and j.fire_frames["front row"] == 1
    j.packet(v2, 3.0, {"front row": True, "cat-walk": False}, True, "hold")
    assert "during the hold" in j.violations[-1][1]
    j.packet(v2, 4.0, {"front row": True, "cat-walk": False}, False, None)
    assert "outside a show" in j.violations[-1][1]
    v3 = list(v)
    v3[401] = 78
    nv = len(j.violations)
    j.packet(v3, 5.0, {"front row": True, "cat-walk": False}, True, None)
    assert len(j.violations) == nv, "a moment before the status: not yet"
    j.packet(v3, 5.01 + GRACE_S, {"front row": True, "cat-walk": False},
             True, None)
    assert "cat-walk is not armed" in j.violations[-1][1]
    j.packet(v3, 5.9, {"front row": True, "cat-walk": False}, True, None)
    assert len(j.violations) == nv + 1, "one line per episode"
    v4 = list(v)
    v4[99] = 1
    j.packet(v4, 6.0, {"front row": True, "cat-walk": False}, True, None)
    assert "belong to no group" in j.violations[-1][1]
    ex0 = Exerciser(lambda *a: (200, {}), ["front row"], lambda: None,
                    lambda: 0, lambda: {}, lambda t: None)
    ex0.windows.append([100.0, None, "abort"])
    va = [0] * 512
    va[400], va[410] = 78, 9
    ja = SacnJudge(groups)
    ja.packet(va, 50.0, {"front row": False}, True, None)
    ja.packet(va, 101.0, {"front row": True}, True,
              lambda g, since: ex0.fire_quiet(g, 101.0, since))
    assert not ja.violations, "armed again (flamesafe says so) after it"
    jb = SacnJudge(groups)
    jb.packet(va, 50.0, {"front row": True}, True, None)
    jb.packet(va, 101.0, {"front row": True}, True,
              lambda g, since: ex0.fire_quiet(g, 101.0, since))
    assert "not armed again since" in jb.violations[-1][1], \
        "still armed from before the Abort: a violation"
    # Show PC 18:33:34: a Hold, long after an Abort the group was armed
    # again since, is named a Hold and judged as one.
    ex0.windows.append([200.0, None, "hold"])
    assert ex0.fire_quiet("front row", 201.0, 150.0) == "hold"
    jc = SacnJudge(groups)
    jc.packet(va, 150.0, {"front row": True}, True, None)
    jc.packet(va, 201.0, {"front row": True}, True,
              lambda g, since: ex0.fire_quiet(g, 201.0, since))
    assert "during the hold" in jc.violations[-1][1]
    yield ("flamesafe's output is judged per group: values only on armed "
           "groups, fire only in a show and never in a Hold, stray "
           "channels caught")

    # The exerciser against a fake engine and flamesafe.
    state = {"armed": {"front row": False, "cat-walk": False}, "t": 0.0,
             "beats": {}, "calls": []}

    def http(method, path, body=None):
        state["calls"].append((method, path, dict(body or {})))
        if path == "/api/remote/status":
            return 200, {"served_at": 1}
        if path == "/api/remote/arm-hold":
            i = body["group"]
            n = state["beats"][i] = state["beats"].get(i, 0) + 1
            if n >= 12:                  # wanted now: the route refuses,
                state["armed"][groups[i][0]] = True    # and flamesafe arms
                return 409, {"error": "front row is already armed or "
                                      "asked for.", "let_go": True}
            return 200, {"ok": True, "hold_id": "h"}
        if path == "/api/remote/reset":
            r = state.setdefault("resets", 0)
            state["resets"] = r + 1
            return (409, {"error": "still fading"}) if r == 0 else (200, {})
        return 200, {}
    clock = [0.0]

    def sleep(s):
        clock[0] += s
    show = {"t": 0.0, "k": 2}
    ex = Exerciser(http, [g[0] for g in groups],
                   show_time=lambda: show["t"], show_number=lambda: show["k"],
                   armed=lambda: dict(state["armed"]), note=lambda t: None,
                   clock=lambda: clock[0], sleep=sleep)
    assert ex.sign_in()
    paths = [p for _m, p, _b in state["calls"]]
    assert paths.index("/api/remote/pin") < paths.index("/api/remote/login") \
        < paths.index("/api/remote/operator")
    show["t"] = 9.0
    ex.step()
    assert all(state["armed"].values()) and ex.counts["arms"] == 2
    holds = [b for _m, p, b in state["calls"] if p == "/api/remote/arm-hold"]
    assert holds[0].get("hold_id") is None and holds[1]["hold_id"] == "h" \
        and all("seen" in b for b in holds)
    assert ("POST", "/api/remote/arm-release", {"group": 0}) in \
        state["calls"]
    show["t"] = 31.0
    ex.step()
    assert ex.counts["holds"] == 1 and ex.counts["resumes"] == 1
    assert ex.windows[0][2] == "hold" and ex.windows[0][1] is not None
    show["t"] = 61.0
    ex.step()
    ab = [b for _m, p, b in state["calls"] if p == "/api/remote/abort"]
    assert ab and ab[0]["confirmed"] is True and ex.counts["resets"] == 1
    assert ex.in_window(time.time() + 1) == "abort"
    assert ex.in_window(time.time() + 1, ("hold", "aborted")) is None, \
        "the lasers' Abort window ends at Reset"
    show["k"] = 3
    show["t"] = 9.0
    state["armed"] = {"front row": False, "cat-walk": False}
    state["beats"] = {0: 99, 1: 99}      # still asked for: held, not armed
    real_http = http

    def http2(method, path, body=None):
        if path == "/api/remote/group-disarm":
            state["beats"][body["group"]] = 0
        if path == "/api/remote/arm-hold" and \
                state["beats"].get(body["group"], 0) >= 99:
            state["calls"].append((method, path, dict(body or {})))
            return 409, {"error": "already armed or asked for."}
        return real_http(method, path, body)
    ex.http = http2
    ex.step()
    assert ex.counts["arms"] == 4 and ex.counts["cycles"] == 2, ex.counts
    assert ex.in_window(time.time() + 1) is None
    assert ex.fire_quiet("front row", time.time() + 1) is None, \
        "armed again after the Abort: it may fire"
    ex.windows.append([time.time(), None, "abort"])
    assert "after the Abort" in ex.fire_quiet("front row", time.time() + 1)
    assert not ex.failures, ex.failures
    yield ("the exerciser signs in with a PIN, picks the operator, holds "
           "each Arm button with heartbeats until flamesafe reports it "
           "armed, Holds and Resumes, Aborts (confirmed), Resets once it "
           "is taken, and re-arms on the next show")
