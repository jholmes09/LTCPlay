#!/usr/bin/env python3
"""flamesafe's own tests.  Run:  python -m flamesafe.test_flamesafe

selftest.py runs this in a SUBPROCESS so the wall holds: this process never
has ltcplay loaded, and the last check here proves it.  Every rule in
rules.py has a test that fails when that rule breaks; mutate.py carries a
mutation for each one and proves it is caught.

No sACN leaves this machine: every socket test binds and sends on loopback.
"""

from __future__ import annotations

import ast
import copy
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from flamesafe import rules, config, composer, link, sacn, arminput  # noqa: E402
from flamesafe.composer import Composer  # noqa: E402
from flamesafe.service import Service  # noqa: E402

FAILS = []
RAN = set()


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print(f"  FAIL  {msg}")
    return bool(cond)


def section(name):
    RAN.add(sys._getframe(1).f_code.co_name)
    print(f"\n== {name}")


def example_dict():
    with open(os.path.join(HERE, "flamesafe.example.json"),
              encoding="utf-8") as fh:
        return json.load(fh)


def make_config(**over):
    d = example_dict()
    d.update(over)
    return config.from_dict(d)


ARM = 78
FRONT_SAFETY, FRONT_FIRE = 401, [411, 412, 413, 414, 415]
N_GROUPS = 6


class Log:
    def __init__(self):
        self.events = []

    def event(self, kind, msg):
        self.events.append((kind, msg))

    def kinds(self):
        return [k for k, _ in self.events]


class Rig:
    """A composer on a fake clock with the scripted arm input."""

    def __init__(self, **over):
        self.cfg = make_config(**over)
        self.t = 0.0
        self.log = Log()
        self.c = Composer(self.cfg, clock=lambda: self.t, log=self.log)
        self.inp = arminput.ScriptedArmInput(self.cfg.n)
        self.period = self.cfg.tick_period_s
        self.seq = 0
        self.out = None

    def step(self, dt=None, n=1):
        for _ in range(n):
            self.t += self.period if dt is None else dt
            a = self.inp.poll()
            if a is not None:
                self.c.assert_arm(a.wanted, a.seq)
            self.out = self.c.tick()
        return self.out

    def wait(self, seconds):
        """Let time pass one tick at a time, as it does for real.  A single
        jump of more than overrun_ms is an overrun, by design."""
        return self.step(n=int(round(seconds / self.period)))

    def run(self, n, slots=None):
        """n ticks with ltcplay sending `slots` every tick."""
        for _ in range(n):
            self.frame(slots)
            self.step()
        return self.out

    def frame(self, slots=None, seq=None, mono=None):
        vals = bytearray(512)
        for slot, v in (slots or {}).items():
            vals[slot - 1] = v
        self.seq = self.seq + 1 if seq is None else seq
        f = link.FlameFrame(self.seq, "00:00:01:00",
                            self.t if mono is None else mono,
                            self.cfg.universe, bytes(vals))
        return self.c.ingest_frame(f)

    def prove_alive(self):
        """Two ticks with everything down: the counter is seen advancing
        and a down edge has been seen while alive."""
        self.step(n=2)

    def group(self, i=0):
        return self.out.status["groups"][i]

    def safety(self, i=0):
        return self.out.universe[self.cfg.groups[i].safety - 1]

    def fire(self, i=0):
        return [self.out.universe[f - 1] for f in self.cfg.groups[i].fire]


def armed_rig(**over):
    r = Rig(**over)
    r.prove_alive()
    r.inp.set(0)
    r.step()
    assert r.safety(0) == ARM, "the fixture could not arm group 0"
    return r


# =========================================================================
# rule 1: the arm value is derived, not chosen
# =========================================================================

def test_rule1_arm_value_is_derived():
    section("rule 1: the arm value is derived from the G-Flame range")
    for rng, (val, above) in rules.ARM_OPTIONS.items():
        lo, hi = rules.GFLAME_SAFETY_RANGES[rng]
        check(lo <= val <= hi, f"{val} is inside the G-Flame {rng} window")
        check(rules.SHOWVEN_ENABLE_LO <= val <= rules.SHOWVEN_ENABLE_HI,
              f"{val} is inside the Showven enable window")
        check(val < rules.GFLAME_FIRE_AT and val < rules.SHOWVEN_CF2_FIRE_AT,
              f"{val} is below every sourced fire threshold")
        check(all((val ^ (1 << b)) < rules.GFLAME_FIRE_AT for b in range(8)),
              f"every single-bit neighbour of {val} is below 229")
        check((val >= rules.SHOWVEN_ASSUMED_LOWEST_FIRE_AT) == above,
              f"ARM_OPTIONS is honest about {val} and the unsourced 111")
    check(rules.ARM_OPTIONS["30-50%"][0] == 78, "the rev 6 arm value is 78")
    check(rules.GFLAME_FIRE_AT == 229 and rules.GFLAME_EDGE_BELOW == 15
          and rules.GFLAME_REARM_BELOW == 16,
          "the G-Flame constants are the manual's: fire at 229, edge below "
          "15, re-trigger below 16")
    check(rules.SACN_PRIORITY == 200, "sACN priority is 200")

    def refused(msg, **over):
        try:
            make_config(**over)
        except config.ConfigError as e:
            check(str(e).strip().endswith(".") and "\n" not in str(e),
                  f"the refusal is one plain sentence: {e}")
            return check(True, msg)
        return check(False, f"NOT refused: {msg}")

    refused("an arm value that is not the validated one for the range",
            arm_value=79)
    refused("an arm value at the fire threshold", arm_value=229)
    refused("an arm value outside the G-Flame window", arm_value=60)
    # The physical checks guard against a wrong TABLE too.  Each one is
    # reached with a table entry that passes everything before it.
    real = rules.ARM_OPTIONS
    try:
        rules.ARM_OPTIONS = {"30-50%": (101, False)}
        refused("a table value whose bit-7 neighbour is 229", arm_value=101)
        rules.ARM_OPTIONS = {"60-80%": (157, False)}
        refused("a table that lies about the unsourced threshold",
                gflame_range="60-80%", arm_value=157,
                accept_unsourced_risk=True)
        rules.ARM_OPTIONS = {"70-90%": (210, True)}
        refused("a table value outside the Showven enable window",
                gflame_range="70-90%", arm_value=210,
                accept_unsourced_risk=True)
        rules.ARM_OPTIONS = {"30-50%": (100, False)}
        check(make_config(arm_value=100).arm_value == 100,
              "a table value that passes every physical check is accepted")
    finally:
        rules.ARM_OPTIONS = real
    refused("a range the manual does not offer", gflame_range="35-55%")
    refused("a range with no validated arm value", gflame_range="40-60%",
            arm_value=128)
    refused("60-80% without acknowledging the unsourced risk",
            gflame_range="60-80%", arm_value=157)
    c = make_config(gflame_range="60-80%", arm_value=157,
                    accept_unsourced_risk=True)
    check(c.arm_value == 157 and c.lights_warning_led,
          "60-80% with the risk acknowledged loads and lights the warning LED")
    c = make_config()
    check(not c.lights_warning_led,
          "under 30-50% the G-Flame warning LED never lights, and the config "
          "says so")
    d = example_dict()
    d["groups"][1]["arm_value"] = 157
    try:
        config.from_dict(d)
        check(False, "a per-group arm value is validated like the global one")
    except config.ConfigError:
        check(True, "")
    d = example_dict()
    d["groups"][1]["arm_value"] = 78
    check(config.from_dict(d).groups[1].arm_value == 78,
          "a per-group arm value equal to the validated one is accepted")


# =========================================================================
# rule 8 and the rest of the config: refuse every bad table
# =========================================================================

def test_rule8_config_refuses_every_bad_table():
    section("rule 8: the config refuses every bad table with one sentence")

    def refused(msg, mutate):
        d = example_dict()
        mutate(d)
        try:
            config.from_dict(d)
        except config.ConfigError as e:
            s = str(e)
            check(s.strip().endswith(".") and "\n" not in s and len(s) < 400,
                  f"one plain sentence: {s}")
            return check(True, msg)
        return check(False, f"NOT refused: {msg}")

    def g(d, i):
        return d["groups"][i]

    refused("two groups share a fire slot",
            lambda d: g(d, 1)["fire"].append(411))
    refused("a fire slot is its own safety slot",
            lambda d: g(d, 0)["fire"].append(401))
    refused("a fire slot is another group's safety slot",
            lambda d: g(d, 0)["fire"].append(402))
    refused("two groups share a safety slot",
            lambda d: g(d, 1).__setitem__("safety", 401))
    refused("a group with no fire slots",
            lambda d: g(d, 0).__setitem__("fire", []))
    refused("a fire slot listed twice in one group",
            lambda d: g(d, 0)["fire"].append(411))
    refused("a slot past 512", lambda d: g(d, 0)["fire"].append(513))
    refused("a slot at 0", lambda d: g(d, 0).__setitem__("safety", 0))
    refused("a slot that is not a number",
            lambda d: g(d, 0)["fire"].append("411"))
    refused("a boolean where a slot goes",
            lambda d: g(d, 0)["fire"].append(True))
    refused("two groups with one name",
            lambda d: g(d, 1).__setitem__("name", "front row"))
    refused("a group with no name", lambda d: g(d, 1).__setitem__("name", " "))
    refused("no groups at all", lambda d: d.__setitem__("groups", []))
    refused("a wrong config format number",
            lambda d: d.__setitem__("flamesafe_config", 2))
    refused("a missing universe", lambda d: d.pop("universe"))
    refused("a universe of 0", lambda d: d.__setitem__("universe", 0))
    refused("a multicast destination",
            lambda d: d["destination"].__setitem__("ip", "239.255.0.1"))
    refused("a broadcast destination",
            lambda d: d["destination"].__setitem__("ip", "255.255.255.255"))
    refused("an unspecified destination",
            lambda d: d["destination"].__setitem__("ip", "0.0.0.0"))
    refused("a destination that is not an address",
            lambda d: d["destination"].__setitem__("ip", "pixlite"))
    refused("a destination port of 0",
            lambda d: d["destination"].__setitem__("port", 0))
    refused("a link that is not loopback",
            lambda d: d["link"].__setitem__("listen_ip", "10.0.0.5"))
    refused("a status address that is not loopback",
            lambda d: d["link"].__setitem__("status_ip", "10.0.0.5"))
    refused("listen and status on one port",
            lambda d: d["link"].__setitem__("status_port", 5571))
    refused("a tick rate below the floor", lambda d: d.__setitem__("tick_hz", 5))
    refused("a tick rate above the ceiling",
            lambda d: d.__setitem__("tick_hz", 100))
    refused("an arm staleness above the ceiling",
            lambda d: d.__setitem__("arm_stale_ms", 5000))
    refused("an arm staleness below the floor",
            lambda d: d.__setitem__("arm_stale_ms", 10))
    refused("a frame staleness above the ceiling",
            lambda d: d.__setitem__("frame_stale_ms", 9999))
    refused("a negative dwell", lambda d: d.__setitem__("min_arm_dwell_ms", -1))
    refused("an overrun shorter than two ticks",
            lambda d: d.__setitem__("overrun_ms", 40))
    refused("a text where a number goes",
            lambda d: d.__setitem__("tick_hz", "40"))
    refused("a boolean where a number goes",
            lambda d: d.__setitem__("tick_hz", True))
    refused("confirmed that is not a boolean",
            lambda d: d.__setitem__("confirmed", "yes"))
    refused("a config that is a list", lambda d: d.clear())

    for text, why in (("{not json", "not valid JSON"),
                      ("[1, 2]", "not a JSON object")):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(text)
            path = fh.name
        try:
            config.load(path)
            check(False, f"a file that is {why} was accepted")
        except config.ConfigError as e:
            check(why in str(e), f"a file that is {why} is refused: {e}")
        finally:
            os.unlink(path)
    try:
        config.load(os.path.join(HERE, "no_such_file.json"))
        check(False, "a missing file was accepted")
    except config.ConfigError as e:
        check("cannot be read" in str(e), f"a missing file is refused: {e}")


def test_example_config_loads_and_is_marked_unconfirmed():
    section("the example config is the rev 6 fixture table, unconfirmed")
    c = config.load(os.path.join(HERE, "flamesafe.example.json"))
    check(c.n == 6 and [g.safety for g in c.groups] == [401, 402, 403, 404,
                                                        405, 406],
          "six groups on safety slots 401 to 406")
    check(c.groups[1].fire == [421, 422, 423, 424, 425, 426],
          "cat-walk has its six fire slots")
    check(c.arm_value == 78 and c.gflame_range == "30-50%",
          "arm value 78 in the 30-50% window")
    check(c.confirmed is False and "UNCONFIRMED" in c.note,
          "the example is marked unconfirmed")
    check(c.destination_ip == "127.0.0.1",
          "the example sends to loopback until Andy confirms the node")
    check(c.universe == 1 and c.destination_port == 5568,
          "universe 1 to the sACN port")
    cards = rules.required_head_settings()
    check(any("Max. Flame Duration" in row[0] for row in cards["G-Flame"])
          and any("Flame Monitor" in row[0] for row in cards["Showven"]),
          "the required head settings card carries the two settings that "
          "ship wrong")


# =========================================================================
# rule 10: startup is all zeros
# =========================================================================

def test_rule10_startup_is_all_zeros():
    section("rule 10: startup is all zeros whatever the input says")
    r = Rig()
    r.inp.set_all(True)
    r.frame({411: 255, 401: 78, 1: 200})
    for _ in range(10):
        o = r.step()
        check(o.universe == bytes(512),
              f"all zeros at startup, tick {r.c.heartbeat}")
    check(all(g["armed"] == "held" for g in o.status["groups"]),
          "an input that boots up asking for arm is held, not honoured")
    check(o.status["groups"][0]["amber"] == "flashing"
          and o.status["groups"][0]["reason"] == "cycle the arm",
          f"the operator is told to cycle the arm: {o.status['groups'][0]}")


# =========================================================================
# rule 6: consent
# =========================================================================

def test_rule6_consent():
    section("rule 6: a group arms only after a live down edge")
    r = Rig()
    # One assertion proves nothing: the counter has not been seen to move.
    r.step(n=1)
    r.inp.set(0)
    o = r.step()
    check(r.safety(0) == 0 and r.group(0)["reason"] == "cycle the arm",
          "a down edge in the FIRST assertion is not consent")
    r.inp.set(0, on=False)
    r.step()
    r.inp.set(0)
    o = r.step()
    check(r.safety(0) == 0 and r.group(0)["reason"] == "re-arm dwell",
          "the cycle itself starts the dwell")
    r.wait(1.0)
    check(r.safety(0) == ARM and r.group(0)["armed"] == "armed",
          f"a down edge after the counter was seen to advance is consent: "
          f"{r.group(0)}")
    check(all(r.safety(i) == 0 for i in range(1, N_GROUPS)),
          "only the group asked for arms")
    # A down edge carried by a stalled counter is not consent, even when an
    # earlier live one was: the latest down edge is the one that counts.
    r2 = Rig()
    r2.prove_alive()
    r2.inp.freeze()
    r2.inp.set_all(False)
    r2.step()
    r2.inp.set(0)
    r2.step()
    check(r2.safety(0) == 0,
          "a down edge on a stalled counter overwrites earlier consent")
    r2.inp.thaw()
    r2.step()
    check(r2.safety(0) == 0, "and the counter moving again does not arm it")
    r3 = Rig()
    r3.step(n=1)
    r3.inp.freeze()
    r3.step(n=3)                 # counter never advanced
    r3.inp.set(0)
    r3.step()
    check(r3.safety(0) == 0,
          "a down edge on a counter that never advanced is not consent")


# =========================================================================
# rule 2: the rising edge must be clean
# =========================================================================

def test_rule2_dirty_edge_holds_the_arm():
    section("rule 2: no rise while a fire slot is at or above 15")
    r = Rig()
    r.prove_alive()
    r.frame({411: rules.GFLAME_EDGE_BELOW})
    r.inp.set(0)
    o = r.step()
    g = r.group(0)
    check(r.safety(0) == 0, "a fire slot at exactly 15 blocks the rise")
    check(g["armed"] == "held" and g["reason"] == "dirty edge"
          and g["amber"] == "flashing",
          f"held, dirty edge, flashing amber: {g}")
    check(r.c.stats["edge_blocks"] >= 1 and "edge-block" in r.log.kinds(),
          "the refusal is counted and logged")
    r.frame({411: rules.GFLAME_EDGE_BELOW - 1})
    o = r.step()
    check(r.safety(0) == ARM,
          "at 14 the edge is clean and the group arms without a cycle")
    # An established arm is not re-gated by a high fire slot.
    r.frame({411: 255})
    r.step(n=6)
    check(r.safety(0) == ARM and r.fire(0)[0] == 255,
          "holding an established arm is not a rising edge")
    # Another group's fire slot does not gate this group.
    r4 = Rig()
    r4.prove_alive()
    r4.frame({421: 200})
    r4.inp.set(0)
    r4.step()
    check(r4.safety(0) == ARM, "only the group's own fire slots gate its edge")


# =========================================================================
# rule 3: the edge is held quiet
# =========================================================================

def test_rule3_edge_quiet_frames():
    section("rule 3: fire slots are zero for the rise tick and 3 more")
    r = Rig()
    r.prove_alive()
    r.frame({411: 10})               # below 15, so the edge is clean
    r.inp.set(0)
    o = r.step()
    check(r.safety(0) == ARM and r.fire(0)[0] == 0,
          "rise tick: armed, fire slot quiet even though 10 was commanded")
    r.frame({411: 255, 412: 255})
    for k in range(rules.EDGE_QUIET_FRAMES):
        o = r.step()
        check(r.fire(0) == [0] * 5,
              f"tick {k + 2} after the rise is still quiet")
        check(r.safety(0) == ARM, "and still armed")
    o = r.step()
    check(r.fire(0)[:2] == [255, 255],
          f"tick {rules.EDGE_QUIET_FRAMES + 2}: the fire slots pass through")
    check(r.c.stats["fire_slots_quieted"] >= rules.EDGE_QUIET_FRAMES,
          "the quieted slots are counted")
    check(rules.EDGE_QUIET_FRAMES == 3, "the quiet window is 3 frames")


# =========================================================================
# rule 4: the re-arm dwell
# =========================================================================

def test_rule4_dwell():
    section("rule 4: the re-arm dwell, its countdown, and that cycling "
            "restarts it")
    r = armed_rig(min_arm_dwell_ms=3000)
    r.frame({411: 200})
    r.step(n=5)
    check(r.fire(0)[0] == 200, "firing while armed")
    r.inp.set(0, on=False)
    o = r.step()
    check(r.safety(0) == 0 and r.fire(0) == [0] * 5,
          "lowering is never delayed: safety and fire are zero on the same "
          "tick as the disarm")
    check(r.group(0)["armed"] == "disarmed", "and the state is disarmed")
    r.frame({})
    r.inp.set(0)
    seen = []
    ticks = 0
    while True:
        o = r.step()
        ticks += 1
        g = r.group(0)
        if g["armed"] == "armed":
            break
        seen.append((g["reason"], g["amber"], g["dwell_s"]))
        if ticks > 200:
            break
    check(all(s[0] == "re-arm dwell" and s[1] == "steady" for s in seen),
          f"held with steady amber and the dwell reason: {set(seen)}")
    countdown = [s[2] for s in seen]
    check(countdown[0] == 3 and countdown[-1] == 1
          and countdown == sorted(countdown, reverse=True)
          and set(countdown) == {3, 2, 1},
          f"the countdown reads whole seconds 3, 2, 1: {countdown[:3]}..."
          f"{countdown[-3:]}")
    elapsed_ms = ticks * r.period * 1000
    check(2999 < elapsed_ms <= 3000 + r.period * 1000 + 0.01,
          f"armed the moment the dwell passed: {elapsed_ms:.0f} ms")
    # Cycling during the dwell restarts it.
    r.inp.set(0, on=False)
    r.step()
    r.inp.set(0)
    r.step(n=40)                     # 1 s into the dwell
    check(r.group(0)["dwell_s"] == 2, "1 s in, 2 s to go")
    r.inp.set(0, on=False)
    r.step()
    r.inp.set(0)
    r.step()
    check(r.group(0)["dwell_s"] == 3 and r.safety(0) == 0,
          f"cycling restarted the dwell: {r.group(0)['dwell_s']} s to go")
    # The default dwell is 1 s (flame panel spec 7a).
    r1 = armed_rig()
    r1.inp.set(0, on=False)
    r1.step()
    r1.inp.set(0)
    n = 0
    while r1.safety(0) == 0 and n < 100:
        r1.step()
        n += 1
    check(abs(n * r1.period - 1.0) <= r1.period + 1e-9,
          f"the default dwell is one second ({n} ticks)")


# =========================================================================
# rule 5: chatter
# =========================================================================

def test_rule5_chatter():
    section("rule 5: more than 3 rises inside 2 s is refused and held")
    r = Rig(min_arm_dwell_ms=100)
    r.prove_alive()

    def cycle():
        r.inp.set(0, on=False)
        r.step()
        r.inp.set(0)
        r.step(n=5)                  # 125 ms: past the 100 ms dwell
        return r.safety(0)

    rises = [cycle() for _ in range(3)]
    check(rises == [ARM] * 3, f"three rises inside 2 s are allowed: {rises}")
    check(r.c.stats["chatter_holds"] == 0, "no hold yet")
    fourth = cycle()
    g = r.group(0)
    check(fourth == 0 and g["reason"] in ("chatter", "re-arm dwell")
          and g["amber"] == "steady",
          f"the fourth rise is refused and held steady: {g}")
    check(r.c.stats["chatter_holds"] >= 1 and "chatter" in r.log.kinds(),
          "the chatter hold is counted and logged")
    # With the request still up, the hold lasts until the window slides:
    # bounded, and no cycle needed.
    first_rise = r.t - 3 * 6 * r.period - r.period
    n = 0
    while r.safety(0) == 0 and n < 200:
        r.step()
        n += 1
    check(r.safety(0) == ARM and (r.t - first_rise) <= 2.0 + 2 * r.period,
          f"the hold ends by itself inside the 2 s window "
          f"({r.t - first_rise:.3f} s after the first rise)")
    # After a genuinely quiet spell, three more rises are fine again.
    r.inp.set(0, on=False)
    r.step()
    r.wait(2.5)
    rises = [cycle() for _ in range(3)]
    check(rises == [ARM] * 3, f"after a quiet 2 s three rises pass: {rises}")
    check(rules.CHATTER_RISES == 3 and rules.CHATTER_WINDOW_MS == 2000,
          "the chatter constants are 3 rises in 2000 ms")


# =========================================================================
# rule 7: interruptions clear the latches
# =========================================================================

def test_rule7_interruptions_clear_the_latches():
    section("rule 7: silence, a stalled counter, a restart and an overrun "
            "all disarm and need a cycle")

    def needs_cycle(r, what):
        g = r.group(0)
        check(r.safety(0) == 0, f"{what}: the safety slot is zero")
        check(g["armed"] == "held" and g["reason"] == "cycle the arm"
              and g["amber"] == "flashing",
              f"{what}: held with 'cycle the arm', flashing: {g}")
        r.inp.set(0, on=False)
        r.step()
        r.inp.set(0)
        r.step()
        check(r.safety(0) == 0, f"{what}: the cycle starts the dwell")
        r.wait(1.0)
        check(r.safety(0) == ARM, f"{what}: armed again after the cycle")
        check("latch-reset" in r.log.kinds(), f"{what}: the reset was logged")

    # (a) the input goes silent
    r = armed_rig()
    r.inp.silent = True
    r.step(n=int(0.5 / r.period) + 2)
    g = r.group(0)
    check(r.safety(0) == 0 and g["reason"] == "arm input stale"
          and g["amber"] == "steady",
          f"silent input: zero, steady amber, 'arm input stale': {g}")
    r.inp.silent = False
    r.step(n=2)
    needs_cycle(r, "silence")

    # (b) assertions keep coming but the counter does not move
    r = armed_rig()
    r.inp.freeze()
    r.step(n=int(0.5 / r.period) + 2)
    check(r.safety(0) == 0 and r.group(0)["reason"] == "arm input stale",
          "a stalled counter disarms after arm_stale_ms")
    r.inp.thaw()
    r.step(n=2)
    needs_cycle(r, "stalled counter")

    # (c) the counter goes backwards: an input restart
    r = armed_rig()
    r.inp.reboot()
    r.step()
    check(r.safety(0) == 0, "a counter that went backwards disarms at once")
    r.step()
    needs_cycle(r, "restart")

    # (d) our own tick overran
    r = armed_rig()
    r.frame({411: 200})
    r.step(n=5)
    check(r.fire(0)[0] == 200, "firing before the overrun")
    o = r.step(dt=r.cfg.overrun_ms / 1000.0 + 0.001)
    check(o.universe == bytes(512), "the overrunning tick is all zeros")
    check(o.fault.startswith("safety program overran")
          and o.status["fault"] == o.fault,
          f"the fault is named in the status frame: {o.status['fault']}")
    check(r.c.stats["overruns"] == 1 and "overrun" in r.log.kinds(),
          "the overrun is counted and logged")
    r.frame({})
    r.step()
    needs_cycle(r, "overrun")
    # A gap just inside the limit is not an overrun.
    r = armed_rig()
    o = r.step(dt=r.cfg.overrun_ms / 1000.0 - 0.001)
    check(r.safety(0) == ARM and r.c.stats["overruns"] == 0,
          "a late tick inside overrun_ms is not an overrun")


def test_liveness_loss_zeros_within_a_bounded_time():
    section("liveness: losing the arm input zeros inside arm_stale_ms plus "
            "one tick, from any moment")
    rnd = random.Random(20260925)
    bound = 0.5 + 0.025 + 1e-9
    for case in range(60):
        r = armed_rig()
        r.run(rnd.randint(5, 30), {411: 255})
        check(r.fire(0)[0] == 255, "firing")
        how = rnd.choice(("silent", "frozen", "garbage"))
        t_lost = r.t
        if how == "silent":
            r.inp.silent = True
        elif how == "frozen":
            r.inp.freeze()
        else:
            r.inp.silent = True
        zero_at = None
        while r.t - t_lost < 2.0:
            r.step()
            if how == "garbage":
                # well-formed-looking rubbish every tick
                r.c.assert_arm([True] * 7, r.inp.seq + 1)
                r.c.assert_arm("yes", "no")
                r.c.assert_arm([True] * 6, -1)
                r.c.assert_arm([1] * 6, r.inp.seq + 5)
            if r.out.universe == bytes(512):
                zero_at = r.t - t_lost
                break
        if not check(zero_at is not None and zero_at <= bound,
                     f"case {case} ({how}): all zeros after {zero_at} s"):
            break
        check(r.c.stats["arm_rejected"] >= (4 if how == "garbage" else 0),
              "garbage assertions are all rejected")
        check(all(r.safety(i) == 0 for i in range(N_GROUPS)),
              "and stays zero")


def test_ltcplay_stale_zeros_fire_and_keeps_the_arm():
    section("liveness: ltcplay going quiet zeros the fire slots inside "
            "frame_stale_ms and does not touch arming")
    r = armed_rig()
    r.frame({411: 255})
    r.step(n=4)
    check(r.fire(0)[0] == 255, "firing")
    t_last = r.t
    r.frame({411: 255})
    zero_at = None
    while r.t - t_last < 2.0:
        r.step()
        if r.fire(0)[0] == 0:
            zero_at = r.t - t_last
            break
    check(zero_at is not None and zero_at <= 0.5 + 0.025 + 1e-9,
          f"the fire slot is zero after {zero_at} s")
    check(r.safety(0) == ARM and r.group(0)["armed"] == "armed",
          "the arm is unaffected by ltcplay going quiet")
    check(r.out.status["frames"]["state"] == "stale",
          "the status frame says the frames are stale")
    # A new ltcplay (sequence restarted) is accepted once the link is stale.
    check(r.frame({411: 100}, seq=0) == "",
          "after the link went stale a restarted sequence is accepted")
    r.step()
    check(r.fire(0)[0] == 100, "and its values pass")


# =========================================================================
# the link: reject everything that is not the contract
# =========================================================================

def test_link_rejects_malformed_datagrams():
    section("the link rejects malformed, wrong-version and wrong-type "
            "datagrams with a reason")
    good = {"v": 1, "t": "flame", "seq": 5, "tc": "00:01:02:03", "mono": 1.5,
            "universe": 1, "values": [0] * 512}
    f = link.decode_flame(json.dumps(good).encode(), 1)
    check(f.seq == 5 and f.timecode == "00:01:02:03" and f.mono == 1.5
          and len(f.values) == 512, "a good frame decodes")
    check(link.decode_flame(link.encode_flame(7, None, 2.0, 1, [3] * 512),
                            1).values == bytes([3] * 512),
          "encode_flame round-trips")

    def bad(msg, obj=None, raw=None):
        data = raw if raw is not None else json.dumps(obj).encode()
        try:
            link.decode_flame(data, 1)
        except link.LinkError as e:
            return check(str(e), f"{msg}: {e}")
        return check(False, f"NOT rejected: {msg}")

    def variant(**kw):
        d = dict(good)
        for k, v in kw.items():
            if v is KeyError:
                d.pop(k)
            else:
                d[k] = v
        return d

    bad("not JSON", raw=b"\xff\xfe hello")
    bad("not an object", raw=b"[1,2,3]")
    bad("empty", raw=b"")
    bad("too long", raw=b"{" + b" " * 20000 + b"}")
    bad("wrong version", variant(v=2))
    bad("missing version", variant(v=KeyError))
    bad("wrong type", variant(t="status"))
    bad("missing type", variant(t=KeyError))
    bad("negative seq", variant(seq=-1))
    bad("float seq", variant(seq=1.5))
    bad("boolean seq", variant(seq=True))
    bad("missing seq", variant(seq=KeyError))
    bad("timecode text", variant(tc="one"))
    bad("timecode too short", variant(tc="0:0:0:0"))
    bad("mono text", variant(mono="now"))
    bad("mono missing", variant(mono=KeyError))
    bad("mono NaN", raw=b'{"v":1,"t":"flame","seq":1,"tc":null,"mono":NaN,'
                        b'"universe":1,"values":' +
                        json.dumps([0] * 512).encode() + b"}")
    bad("wrong universe", variant(universe=2))
    bad("universe text", variant(universe="1"))
    bad("values short", variant(values=[0] * 511))
    bad("values long", variant(values=[0] * 513))
    bad("values not a list", variant(values="0" * 512))
    bad("a value of 256", variant(values=[256] + [0] * 511))
    bad("a negative value", variant(values=[-1] + [0] * 511))
    bad("a float value", variant(values=[1.0] + [0] * 511))
    bad("a boolean value", variant(values=[True] + [0] * 511))
    bad("a null value", variant(values=[None] + [0] * 511))
    check(link.decode_flame(json.dumps(variant(tc=None)).encode(), 1).timecode
          is None, "a null timecode is allowed")
    check(link.decode_flame(json.dumps(variant(tc="01:02:03;04")).encode(),
                            1).timecode == "01:02:03;04",
          "drop-frame timecode is allowed")
    # A rejected datagram changes nothing on the wire.
    r = armed_rig()
    r.frame({411: 200})
    r.step(n=5)
    check(r.fire(0)[0] == 200, "firing")
    r.c.reject_frame("rubbish")
    check(r.c.ingest_frame("not a frame") != "", "a non-frame is rejected")
    check(r.c.ingest_frame(link.FlameFrame(99, None, 0.0, 1, b"\x00" * 3))
          != "", "a short frame is rejected")
    r.step()
    check(r.fire(0)[0] == 200 and r.c.stats["frames_rejected"] == 3
          and r.out.status["frames"]["rejected"] == 3,
          "rejections are counted and the last good frame still stands")
    check(r.out.status["frames"]["last_reject"],
          "the status frame names the last rejection")


def test_link_sequence_and_clock_rules():
    section("the link rejects out-of-order frames and a sender clock that "
            "goes backwards while the link is live")
    r = armed_rig()
    check(r.frame({411: 50}, seq=10, mono=5.0) == "", "seq 10 accepted")
    check("out of order" in r.frame({411: 60}, seq=10, mono=5.1),
          "a repeated seq is rejected")
    check("out of order" in r.frame({411: 60}, seq=9, mono=5.2),
          "an older seq is rejected")
    check("backwards" in r.frame({411: 60}, seq=11, mono=4.9),
          "a sender clock going backwards is rejected")
    check(r.frame({411: 60}, seq=11, mono=5.0) == "",
          "an equal sender clock is fine")
    r.step(n=5)
    check(r.fire(0)[0] == 60, "only the accepted frame reached the wire")
    check(r.c.stats["frames_rejected"] == 3, "three rejections counted")


# =========================================================================
# rule 9: only the writer
# =========================================================================

def test_rule9_only_the_writer():
    section("rule 9: unused channels are always zero and fire passes only "
            "through an armed group")
    r = Rig()
    r.prove_alive()
    everything = {s: 200 for s in range(1, 513)}
    r.frame(everything)
    o = r.step()
    check(o.universe == bytes(512),
          "disarmed: ltcplay's 200 on every channel produces all zeros")
    check(r.c.stats["fire_refused"] >= 1 and "fire-refused" in r.log.kinds(),
          "fire commanded on a disarmed group is refused and logged")
    check(r.group(0)["commanded_fire"] == [200] * 5
          and r.group(0)["sent_fire"] == [0] * 5,
          "COMMANDED and SENT both appear in the status frame")
    r.frame({s: 200 for s in range(1, 513) if s not in FRONT_FIRE})
    r.inp.set(0)
    r.step(n=5)
    r.frame(everything)
    o = r.step()
    slots_of_groups = set(r.cfg.all_slots())
    check(all(o.universe[s - 1] == 0 for s in range(1, 513)
              if s not in slots_of_groups),
          "every channel that belongs to no group is zero while armed")
    check(all(o.universe[f - 1] == 200 for f in FRONT_FIRE),
          "the armed group's fire slots carry ltcplay's values")
    check(o.universe[FRONT_SAFETY - 1] == ARM, "its safety slot carries 78")
    check(all(o.universe[g.safety - 1] == 0 and
              all(o.universe[f - 1] == 0 for f in g.fire)
              for g in r.cfg.groups[1:]),
          "every other group is zero on safety and fire")
    check(o.universe[FRONT_SAFETY - 1] != 200,
          "ltcplay's value on a safety slot is never passed through")


# =========================================================================
# rule 10: compose never raises
# =========================================================================

def test_rule10_compose_never_raises():
    section("rule 10: a fault inside compose produces zeros, clears the "
            "latches and is reported")
    r = armed_rig()
    r.frame({411: 200})
    r.step(n=5)
    check(r.fire(0)[0] == 200, "firing")
    orig = r.c._fire_is_quiet
    r.c._fire_is_quiet = lambda *a: 1 / 0
    r.inp.set(1)                     # a second group tries to rise
    o = r.step()
    check(o.universe == bytes(512), "the faulting tick is all zeros")
    check("compose fault" in o.status["fault"] and "ZeroDivisionError"
          in o.status["fault"], f"the fault is named: {o.status['fault']}")
    check(r.c.stats["compose_faults"] == 1, "counted")
    r.c._fire_is_quiet = orig
    r.step()
    check(r.safety(0) == 0 and r.group(0)["reason"] == "cycle the arm",
          "after a fault every group needs a cycle")
    check(r.out.status["heartbeat"] == r.c.heartbeat,
          "the heartbeat keeps counting")


# =========================================================================
# property: a disarmed group never emits a fire value, ever
# =========================================================================

def test_property_a_disarmed_group_never_fires():
    section("property: over random sequences a disarmed group never emits "
            "fire, unused channels stay zero, and a lost input zeros in time")
    seed = int(os.environ.get("FLAMESAFE_SEED", "2026"))
    cases = int(os.environ.get("FLAMESAFE_CASES", "250"))
    rnd = random.Random(seed)
    worst = 0
    for case in range(cases):
        dwell = rnd.choice((0, 100, 1000, 3000))
        r = Rig(min_arm_dwell_ms=dwell)
        cfg = r.cfg
        group_slots = set(cfg.all_slots())
        last_fresh_advance = None
        delivered_seq = None
        last_frame = None
        last_frame_at = None
        prev_safety = [0] * N_GROUPS
        quiet_left = [0] * N_GROUPS
        for step in range(300):
            act = rnd.random()
            if act < 0.15:
                r.inp.wanted[rnd.randrange(N_GROUPS)] ^= True
            elif act < 0.20:
                r.inp.set_all(rnd.random() < 0.5)
            elif act < 0.24:
                r.inp.silent = not r.inp.silent
            elif act < 0.28:
                r.inp.alive = not r.inp.alive
            elif act < 0.30:
                r.inp.reboot()
            if rnd.random() < 0.7:
                vals = bytearray(512)
                for _ in range(rnd.randint(0, 12)):
                    vals[rnd.randrange(512)] = rnd.randrange(256)
                if rnd.random() < 0.3:
                    for f in cfg.groups[rnd.randrange(N_GROUPS)].fire:
                        vals[f - 1] = rnd.choice((0, 14, 15, 255))
                r.seq += 1
                f = link.FlameFrame(r.seq, None, r.t, cfg.universe,
                                    bytes(vals))
                if r.c.ingest_frame(f) == "":
                    last_frame, last_frame_at = bytes(vals), r.t
            dt = rnd.choice((0.025, 0.025, 0.025, 0.025, 0.05, 0.3, 0.6))
            silent_before = r.inp.silent
            o = r.step(dt=dt)
            if not silent_before:
                # Mirror the composer's own rule: an assertion is fresh only
                # when its counter is above the last one delivered.  A first
                # assertion, or the first after a reboot, proves nothing.
                if delivered_seq is not None and r.inp.seq > delivered_seq:
                    last_fresh_advance = r.t
                delivered_seq = r.inp.seq
            u = o.universe
            ok = True
            ok &= check(len(u) == 512, f"case {case} step {step}: 512 slots")
            ok &= check(all(u[s - 1] == 0 for s in range(1, 513)
                            if s not in group_slots),
                        f"case {case} step {step}: an unused channel was "
                        f"not zero")
            fresh = (last_frame_at is not None
                     and (r.t - last_frame_at) * 1000 <= cfg.frame_stale_ms)
            commanded = last_frame if fresh else bytes(512)
            for i, g in enumerate(cfg.groups):
                s = u[g.safety - 1]
                fire = [u[f - 1] for f in g.fire]
                ok &= check(s in (0, g.arm_value),
                            f"case {case} step {step}: {g.name} safety "
                            f"slot carried {s}")
                if s == 0:
                    ok &= check(fire == [0] * len(fire),
                                f"case {case} step {step}: {g.name} is "
                                f"disarmed and emitted fire {fire}")
                    quiet_left[i] = 0
                else:
                    if prev_safety[i] == 0:
                        quiet_left[i] = rules.EDGE_QUIET_FRAMES + 1
                    if quiet_left[i] > 0:
                        ok &= check(fire == [0] * len(fire),
                                    f"case {case} step {step}: {g.name} "
                                    f"fired inside the edge-quiet window")
                        quiet_left[i] -= 1
                    else:
                        ok &= check(all(v in (0, commanded[f - 1])
                                        for f, v in zip(g.fire, fire)),
                                    f"case {case} step {step}: {g.name} "
                                    f"sent a fire value ltcplay did not "
                                    f"command")
                    ok &= check(r.inp.wanted[i] or r.inp.silent,
                                f"case {case} step {step}: {g.name} armed "
                                f"while not wanted")
                st = o.status["groups"][i]
                ok &= check((st["armed"] == "armed") == (s != 0)
                            and st["sent_safety"] == s
                            and st["sent_fire"] == fire,
                            f"case {case} step {step}: the status frame "
                            f"disagrees with the wire for {g.name}")
                prev_safety[i] = s
            if last_fresh_advance is not None:
                age = (r.t - last_fresh_advance) * 1000
                if age > cfg.arm_stale_ms + 0.001:
                    ok &= check(all(u[g.safety - 1] == 0
                                    for g in cfg.groups),
                                f"case {case} step {step}: armed {age:.0f} "
                                f"ms after the last fresh assertion")
            worst = max(worst, 0)
            if not ok:
                print(f"  (stopping case {case} at step {step}; dwell "
                      f"{dwell} ms, seed {seed})")
                break
        if FAILS:
            break
    print(f"  {cases} random cases, seed {seed}")


# =========================================================================
# sACN packet bytes, priority 200
# =========================================================================

def test_sacn_packet_bytes():
    section("sACN: the packet bytes carry priority 200, the universe and "
            "all 512 slots")
    vals = bytes(range(256)) * 2
    p = sacn.build_packet(1, vals, 7)
    check(len(p) == 638, f"638 bytes: {len(p)}")
    check(p[0:2] == b"\x00\x10" and p[4:16] == b"ASC-E1.17\x00\x00\x00",
          "the ACN preamble")
    check(p[18:22] == b"\x00\x00\x00\x04", "VECTOR_ROOT_E131_DATA")
    check(p[40:44] == b"\x00\x00\x00\x02", "VECTOR_E131_DATA_PACKET")
    check(p[16] == 0x70 | ((622 >> 8) & 0x0F) and p[17] == 622 & 0xFF,
          "root PDU length")
    check(p[38] == 0x70 | ((600 >> 8) & 0x0F) and p[39] == 600 & 0xFF,
          "framing PDU length")
    check(p[115] == 0x70 | ((523 >> 8) & 0x0F) and p[116] == 523 & 0xFF,
          "DMP PDU length")
    check(p[108] == 200, f"PRIORITY BYTE 108 IS 200: {p[108]}")
    check(p[111] == 7, "sequence byte")
    check(p[112] == 0, "options: not terminated")
    check(p[113] == 0 and p[114] == 1, "universe 1")
    check(p[117] == 0x02 and p[118] == 0xA1 and p[122] == 0x01
          and p[123] == 0x02 and p[124] == 0x01 and p[125] == 0x00,
          "DMP header and start code")
    check(p[126:] == vals, "all 512 slots follow the start code")
    check(p[44:53] == b"flamesafe" and p[53] == 0, "the source name")
    check(len(sacn.CID) == 16 and p[22:38] == sacn.CID, "the CID")
    p2 = sacn.build_packet(300, bytes(512), 255, terminated=True)
    check(p2[113] == 1 and p2[114] == 44, "universe 300 big-endian")
    check(p2[112] & 0x40, "the stream-terminated bit")
    check(p2[111] == 255, "sequence 255")
    for bad in (bytes(511), bytes(513)):
        try:
            sacn.build_packet(1, bad, 0)
            check(False, "a frame that is not 512 slots was built")
        except ValueError:
            check(True, "")
    try:
        sacn.build_packet(1, bytes(512), 0, priority=201)
        check(False, "a priority above 200 was built")
    except ValueError:
        check(True, "")


# =========================================================================
# the service over loopback
# =========================================================================

def _udp(port=0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", port))
    s.settimeout(2.0)
    return s


def _drain(sock):
    out = []
    sock.settimeout(0.05)
    while True:
        try:
            out.append(sock.recv(65535))
        except (socket.timeout, TimeoutError, BlockingIOError):
            break
        except ConnectionResetError:
            continue
    sock.settimeout(2.0)
    return out


def test_service_over_loopback():
    section("service: frames in, priority-200 sACN and status frames out, "
            "all on loopback")
    node = _udp()
    ltc_status = _udp()
    ltc_tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listen_port = _udp()
    lp = listen_port.getsockname()[1]
    listen_port.close()
    cfg = make_config(destination={"ip": "127.0.0.1",
                                   "port": node.getsockname()[1]},
                      link={"listen_ip": "127.0.0.1", "listen_port": lp,
                            "status_ip": "127.0.0.1",
                            "status_port": ltc_status.getsockname()[1]})
    t = [0.0]
    log = Log()
    inp = arminput.ScriptedArmInput(cfg.n)
    svc = Service(cfg, inp, clock=lambda: t[0], log=log)
    svc.open()
    try:
        def tick():
            t[0] += cfg.tick_period_s
            time.sleep(0.005)
            return svc.run_once()

        def send(seq, slots, universe=1, mono=None):
            vals = [0] * 512
            for s, v in slots.items():
                vals[s - 1] = v
            ltc_tx.sendto(link.encode_flame(seq, "00:00:00:01",
                                            t[0] if mono is None else mono,
                                            universe, vals),
                          ("127.0.0.1", lp))
            time.sleep(0.01)

        send(1, {411: 200, 1: 77, 401: 78})
        tick()
        pk = _drain(node)
        st = _drain(ltc_status)
        check(len(pk) >= 1, f"a packet reached the node: {len(pk)}")
        p = pk[-1]
        check(p[108] == 200, f"priority 200 on the wire: {p[108]}")
        check(p[113:115] == b"\x00\x01", "universe 1")
        check(p[126:] == bytes(512),
              "disarmed: all zeros even though ltcplay sent 200, 77 and 78")
        check(len(st) >= 1, "a status frame reached ltcplay's port")
        s = link.decode_status(st[-1])
        check(s["groups"][0]["armed"] == "disarmed" and s["heartbeat"] >= 1
              and s["priority"] == 200 and s["frames"]["seq"] == 1
              and s["frames"]["timecode"] == "00:00:00:01",
              f"the status frame is right: {s['groups'][0]}, "
              f"{s['frames']}")
        check("sacn" in s and s["sacn"]["sent"] >= 1,
              "the status frame counts sent packets")
        # arm group 0 with consent, clean edge
        tick()
        send(2, {})
        tick()
        inp.set(0)
        send(3, {})
        tick()
        p = _drain(node)[-1]
        check(p[126 + 400] == 78, "the safety slot carries 78 on the wire")
        for k in range(4, 8):
            send(k, {411: 200})
            tick()
        p = _drain(node)[-1]
        check(p[126 + 410] == 200 and p[126 + 400] == 78,
              "after the quiet window the fire value passes")
        check(p[126 + 0] == 0, "and channel 1, no group's, is zero")
        s = link.decode_status(_drain(ltc_status)[-1])
        check(s["groups"][0]["armed"] == "armed"
              and s["groups"][0]["sent_fire"][0] == 200,
              "the status frame says armed with SENT 200")
        # rubbish and wrong-universe frames are rejected and change nothing
        ltc_tx.sendto(b"\x00\xff garbage", ("127.0.0.1", lp))
        ltc_tx.sendto(b'{"v":2,"t":"flame"}', ("127.0.0.1", lp))
        send(8, {411: 0}, universe=2)
        time.sleep(0.02)
        tick()
        p = _drain(node)[-1]
        s = link.decode_status(_drain(ltc_status)[-1])
        check(p[126 + 410] == 200, "the good frame still stands")
        check(s["frames"]["rejected"] == 3 and s["frames"]["last_reject"],
              f"three rejections reported: {s['frames']}")
        # ltcplay stops: the fire slot zeros, the arm stays
        for _ in range(int(0.5 / cfg.tick_period_s) + 2):
            tick()
        p = _drain(node)[-1]
        check(p[126 + 410] == 0 and p[126 + 400] == 78,
              "ltcplay quiet: fire zero, arm kept")
        # the arm input goes: everything zeros
        inp.silent = True
        for _ in range(int(0.5 / cfg.tick_period_s) + 2):
            tick()
        p = _drain(node)[-1]
        check(p[126:] == bytes(512), "arm input gone: all zeros")
        check(p[112] == 0, "still not terminated")
        # back, cycled, armed and firing again, so that the shutdown below
        # has something to zero
        inp.silent = False
        inp.set(0, on=False)
        tick()
        tick()
        inp.set(0)
        for k in range(9, 60):
            send(k, {411: 0})
            tick()
        for k in range(60, 66):
            send(k, {411: 200})
            tick()
        p = _drain(node)[-1]
        check(p[126 + 400] == 78 and p[126 + 410] == 200,
              "armed and firing again before the stop")
        _drain(ltc_status)
    finally:
        svc.close()
    pk = _drain(node)
    check(len(pk) == 6, f"close sends 6 packets: {len(pk)}")
    check(all(p[126:] == bytes(512) for p in pk), "all of them zeros")
    check([p[112] & 0x40 for p in pk] == [0, 0, 0, 0x40, 0x40, 0x40],
          "three zero frames, then three stream-terminated frames")
    check(("stop", ) == tuple(k for k, _ in log.events if k == "stop"),
          "the stop is logged once")
    for s_ in (node, ltc_status, ltc_tx):
        s_.close()
    # No socket in the package is ever bound or sent anywhere but where the
    # config says.  The config refuses a non-loopback link, and the tests
    # only ever configure a loopback destination.
    check(cfg.destination_ip == "127.0.0.1", "this test sent to loopback only")


def test_service_paces_on_perf_counter():
    section("service: run_forever ticks at tick_hz on perf_counter and stops")
    node = _udp()
    ltc_status = _udp()
    listen = _udp()
    lp = listen.getsockname()[1]
    listen.close()
    cfg = make_config(tick_hz=40,
                      destination={"ip": "127.0.0.1",
                                   "port": node.getsockname()[1]},
                      link={"listen_ip": "127.0.0.1", "listen_port": lp,
                            "status_ip": "127.0.0.1",
                            "status_port": ltc_status.getsockname()[1]})
    svc = Service(cfg, arminput.NullArmInput())
    svc.open()
    stop = threading.Event()
    th = threading.Thread(target=svc.run_forever, args=(stop,), daemon=True)
    t0 = time.perf_counter()
    th.start()
    time.sleep(0.5)
    stop.set()
    th.join(2.0)
    el = time.perf_counter() - t0
    n = svc.composer.heartbeat
    check(not th.is_alive(), "the loop stopped when asked")
    check(0.5 * 40 * 0.6 <= n <= el * 40 + 2,
          f"{n} ticks in {el:.2f} s at 40 Hz")
    check(svc.composer.stats["overruns"] == 0,
          "no overrun while idling on this machine")
    pk = _drain(node)
    check(len(pk) >= n * 0.5 and all(p[126:] == bytes(512) for p in pk),
          f"{len(pk)} packets, every one all zeros with no arm input")
    check(all(p[108] == 200 for p in pk), "every packet at priority 200")
    svc.close()
    for s_ in (node, ltc_status):
        s_.close()
    check(composer.now.__code__.co_names and "perf_counter" in
          composer.now.__code__.co_names, "the clock is perf_counter")


def test_the_clock_is_perf_counter_everywhere():
    section("no flamesafe module paces on anything but perf_counter")
    for name in sorted(os.listdir(HERE)):
        if not name.endswith(".py") or name.startswith("test_"):
            continue
        src = open(os.path.join(HERE, name), encoding="utf-8").read()
        tree = ast.parse(src)
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and \
                    isinstance(node.value, ast.Name) and \
                    node.value.id == "time" and \
                    node.attr in ("monotonic", "monotonic_ns", "time",
                                  "time_ns", "perf_counter_ns"):
                bad.append(f"{name}:{node.lineno} time.{node.attr}")
        check(not bad, f"only perf_counter: {bad}")


def test_status_frame_matches_the_contract():
    section("the status frame carries what CONTRACT.md promises")
    r = armed_rig()
    r.frame({411: 200})
    r.step(n=5)
    s = r.out.status
    for key in ("v", "t", "heartbeat", "tick_ms", "universe", "priority",
                "arm_value", "confirmed", "fault", "fault_age_ms",
                "arm_input", "frames", "stats", "groups"):
        check(key in s, f"top-level {key}")
    check(s["v"] == link.CONTRACT_VERSION == 1 and s["t"] == "status",
          "version 1, type status")
    check(s["arm_input"]["state"] == "live" and s["frames"]["state"] == "fresh",
          f"input states: {s['arm_input']}, {s['frames']}")
    g = s["groups"][0]
    for key in ("name", "safety_slot", "fire_slots", "wanted", "armed",
                "reason", "amber", "dwell_s", "sent_safety", "sent_fire",
                "commanded_fire"):
        check(key in g, f"per-group {key}")
    check(g["armed"] == "armed" and g["sent_safety"] == 78
          and g["sent_fire"][0] == 200 and g["commanded_fire"][0] == 200
          and g["amber"] == "" and g["reason"] == "" and g["dwell_s"] == 0,
          f"an armed, firing group: {g}")
    check(json.loads(link.encode_status(s).decode()) == s,
          "the status frame survives the wire")
    h1 = s["heartbeat"]
    r.step()
    check(r.out.status["heartbeat"] == h1 + 1, "the heartbeat counts ticks")
    doc = open(os.path.join(HERE, "CONTRACT.md"), encoding="utf-8").read()
    for word in ("Contract version 1", '"flame"', '"status"', "priority 200",
                 "dwell_s", "sent_fire", "commanded_fire", "flashing",
                 "steady", "heartbeat", "arm_stale_ms", "frame_stale_ms"):
        check(word in doc, f"CONTRACT.md mentions {word}")


def test_main_refuses_a_bad_config_with_a_sentence():
    section("python -m flamesafe refuses to start on a bad config")
    d = example_dict()
    d["groups"][1]["fire"].append(411)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as fh:
        json.dump(d, fh)
        path = fh.name
    try:
        r = subprocess.run([sys.executable, "-m", "flamesafe", path],
                           cwd=ROOT, capture_output=True, text=True,
                           timeout=60)
    finally:
        os.unlink(path)
    check(r.returncode == 2, f"exit 2: {r.returncode}")
    check("flamesafe will not start" in r.stdout and "share fire slot 411"
          in r.stdout, f"the sentence: {r.stdout.strip()[:200]}")
    r = subprocess.run([sys.executable, "-m", "flamesafe"], cwd=ROOT,
                       capture_output=True, text=True, timeout=60)
    check(r.returncode == 2 and "Usage" in r.stdout, "no argument: usage")


def test_the_wall_from_this_side():
    section("the wall: nothing in flamesafe imports ltcplay")
    loaded = sorted(m for m in sys.modules if m.split(".")[0] == "ltcplay")
    check(loaded == [], f"ltcplay is not loaded in this process: {loaded}")
    for name in sorted(os.listdir(HERE)):
        if not name.endswith(".py"):
            continue
        src = open(os.path.join(HERE, name), encoding="utf-8").read()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for n in names:
                check(n.split(".")[0] != "ltcplay",
                      f"{name} imports {n}")
        import re
        check(not re.search(r"\bltcplay\.[A-Za-z_]", src),
              f"{name} names no ltcplay module by dotted path")


if __name__ == "__main__":
    t0 = time.perf_counter()
    test_rule1_arm_value_is_derived()
    test_rule8_config_refuses_every_bad_table()
    test_example_config_loads_and_is_marked_unconfirmed()
    test_rule10_startup_is_all_zeros()
    test_rule6_consent()
    test_rule2_dirty_edge_holds_the_arm()
    test_rule3_edge_quiet_frames()
    test_rule4_dwell()
    test_rule5_chatter()
    test_rule7_interruptions_clear_the_latches()
    test_liveness_loss_zeros_within_a_bounded_time()
    test_ltcplay_stale_zeros_fire_and_keeps_the_arm()
    test_link_rejects_malformed_datagrams()
    test_link_sequence_and_clock_rules()
    test_rule9_only_the_writer()
    test_rule10_compose_never_raises()
    test_property_a_disarmed_group_never_fires()
    test_sacn_packet_bytes()
    test_service_over_loopback()
    test_service_paces_on_perf_counter()
    test_the_clock_is_perf_counter_everywhere()
    test_status_frame_matches_the_contract()
    test_main_refuses_a_bad_config_with_a_sentence()
    test_the_wall_from_this_side()
    defined = {n for n, v in list(globals().items())
               if n.startswith("test_") and callable(v)}
    never = sorted(defined - RAN)
    if never:
        FAILS.extend(f"{n} is defined but was never called" for n in never)
        for n in never:
            print(f"  FAIL  {n} is defined but was never called")
    print(f"\n{'-' * 50}")
    if FAILS:
        print(f"flamesafe: {len(FAILS)} FAILURES in "
              f"{time.perf_counter() - t0:.1f}s")
        for f in FAILS:
            print(f"  - {f}")
        sys.exit(1)
    print(f"flamesafe: all checks passed in {time.perf_counter() - t0:.1f}s")
