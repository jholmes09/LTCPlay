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
import re
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
KEY = example_dict()["link"]["key"]
NAMES = [g["name"] for g in example_dict()["groups"]]
SENDER = ("127.0.0.1", 40001)


class Log:
    def __init__(self):
        self.events = []

    def event(self, kind, msg):
        self.events.append((kind, msg))

    def kinds(self):
        return [k for k, _ in self.events]


class Rig:
    """A composer on a fake clock with the scripted arm input."""

    def __init__(self, test_only_dwell_ms=None, **over):
        self.cfg = make_config(**over)
        if test_only_dwell_ms is not None:
            # The file loader floors the dwell at 1000 ms; the chatter and
            # property tests need it shorter to reach their rules.
            self.cfg.test_only_override_dwell_ms(test_only_dwell_ms)
        self.t = 0.0
        self.log = Log()
        self.c = Composer(self.cfg, clock=lambda: self.t, log=self.log)
        self.inp = arminput.ScriptedArmInput(self.cfg.n, names=NAMES)
        self.period = self.cfg.tick_period_s
        self.seq = 0
        self.out = None
        # ltcplay is alive unless a test says otherwise: every step sends
        # the current cue as a frame, because since 2026-09-26 a lost link
        # disarms every group and nothing could arm in a rig without one.
        self.link_alive = True
        self.cue = {}
        self.mono_floor = 0.0

    def step(self, dt=None, n=1):
        for _ in range(n):
            self.t += self.period if dt is None else dt
            if self.link_alive:
                self.frame(self.cue)
            a = self.inp.poll()
            if a is not None:
                self.c.assert_arm(a.wanted, a.seq, names=a.names,
                                  forced=a.forced)
            self.out = self.c.tick()
        return self.out

    def wait(self, seconds):
        """Let time pass one tick at a time, as it does for real.  A single
        jump of more than overrun_ms is an overrun, by design."""
        return self.step(n=int(round(seconds / self.period)))

    def run(self, n, slots=None):
        """n ticks with ltcplay sending `slots` every tick."""
        self.cue = dict(slots or {})
        return self.step(n=n)

    def frame(self, slots=None, seq=None, mono=None, sender=SENDER):
        """One frame from ltcplay, which also becomes the cue the rig keeps
        sending on every step while the link is alive."""
        self.cue = dict(slots or {})
        vals = bytearray(512)
        for slot, v in self.cue.items():
            vals[slot - 1] = v
        before = self.seq
        self.seq = self.seq + 1 if seq is None else seq
        if mono is not None:
            self.mono_floor = max(self.mono_floor, mono)
        f = link.FlameFrame(self.seq, "00:00:01:00",
                            max(self.t, self.mono_floor) if mono is None
                            else mono,
                            self.cfg.universe, bytes(vals))
        why = self.c.ingest_frame(f, sender=sender)
        if why and seq is not None and seq > before:
            # a rejected explicit seq must not move the rig's own counter
            self.seq = before
        return why

    def prove_alive(self):
        """Three ticks with everything down: the counter is seen
        advancing (tick 2), and then a down edge arrives on a counter that
        was already live (tick 3), which is consent."""
        self.step(n=3)

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
        rules.ARM_OPTIONS = {"30-50%": (60, False)}
        refused("a table value outside the G-Flame window", arm_value=60)
        # 130 passes every later check (Showven window, thresholds, every
        # single-bit neighbour below 229, honest about the unsourced 111,
        # risk acknowledged, equals the table) and fails ONLY the window.
        rules.ARM_OPTIONS = {"30-50%": (130, True)}
        refused("a table value above the G-Flame window that passes every "
                "other check", arm_value=130, accept_unsourced_risk=True)
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
    refused("a dwell shorter than the spec's one second",
            lambda d: d.__setitem__("min_arm_dwell_ms", 999))
    check(make_config(min_arm_dwell_ms=1000).min_arm_dwell_ms == 1000,
          "a dwell of exactly one second loads")
    refused("an overrun below the floor",
            lambda d: d.__setitem__("overrun_ms", 40))
    # Past the floor (50 ms) but under two ticks at 10 Hz (200 ms): only
    # the two-ticks rule can refuse this one.  CI found the old case (40 ms
    # at 40 Hz) was refused by the floor whether or not the rule existed.
    refused("an overrun shorter than two ticks",
            lambda d: (d.__setitem__("tick_hz", 10),
                       d.__setitem__("fire_hold_ms", 200),
                       d.__setitem__("overrun_ms", 150)))
    check(make_config(tick_hz=10, overrun_ms=200,
                      fire_hold_ms=200).overrun_ms == 200,
          "exactly two ticks is accepted")
    refused("a fire hold shorter than two ticks",
            lambda d: d.__setitem__("fire_hold_ms", 40))
    refused("a fire hold not below frame_stale_ms",
            lambda d: d.__setitem__("fire_hold_ms", 500))
    refused("a missing fire hold", lambda d: d.pop("fire_hold_ms"))
    refused("a missing link key", lambda d: d["link"].pop("key"))
    refused("a link key that is too short",
            lambda d: d["link"].__setitem__("key", "short"))
    refused("a link key with a space",
            lambda d: d["link"].__setitem__("key", "fire and ice 2026 key"))
    refused("a link key that is not text",
            lambda d: d["link"].__setitem__("key", 12345678901234567890))
    refused("a group name longer than 64 characters",
            lambda d: g(d, 0).__setitem__("name", "x" * 65))
    check(config.from_dict(dict(example_dict(), groups=[
        dict(example_dict()["groups"][0], name="x" * 64)])).groups[0].name
        == "x" * 64, "a 64-character name is fine")
    refused("a loopback destination on the frame port",
            lambda d: d["destination"].__setitem__("port", 5571))
    refused("a loopback destination on the status port",
            lambda d: d["destination"].__setitem__("port", 5572))
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
    check(c.link_arm_port == 5573 and c.link_arm_ip == "127.0.0.1",
          "the example config now wires the arm link too (build step 7b)")


def test_config_validates_the_arm_link():
    section("config: the arm link is optional, and when present must not "
            "collide with anything else")
    d = example_dict()
    del d["link"]["arm_port"]
    c = config.from_dict(d)
    check(c.link_arm_port is None and c.link_arm_ip is None,
          "no arm_port in the file: the arm link is simply not configured")
    d2 = example_dict()
    d2["link"]["arm_port"] = d2["link"]["listen_port"]
    try:
        config.from_dict(d2)
        check(False, "arm_port same as listen_port was accepted")
    except config.ConfigError as e:
        check("arm_port" in str(e) and "listen_port" in str(e), str(e))
    d3 = example_dict()
    d3["link"]["arm_port"] = d3["link"]["status_port"]
    try:
        config.from_dict(d3)
        check(False, "arm_port same as status_port was accepted")
    except config.ConfigError as e:
        check("arm_port" in str(e) and "status_port" in str(e), str(e))
    d4 = example_dict()
    del d4["link"]["arm_port"]
    d4["link"]["arm_ip"] = "127.0.0.1"
    try:
        config.from_dict(d4)
        check(False, "arm_ip without arm_port was accepted")
    except config.ConfigError as e:
        check("arm_ip" in str(e) and "arm_port" in str(e), str(e))
    d5 = example_dict()
    d5["link"]["arm_ip"] = "8.8.8.8"
    try:
        config.from_dict(d5)
        check(False, "a non-loopback arm_ip was accepted")
    except config.ConfigError as e:
        check("loopback" in str(e), str(e))


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
    # Rule 10 belongs to the composer, not to the service's polling order.
    # Two assertions asking for arm BEFORE the first tick never pass
    # through a not-live tick, so nothing has cleared any latch: the first
    # compose must still be zeros with no consent (review of 120ce04: a
    # composer whose latches started out set sent 78 on its first tick).
    cfg = make_config()
    c = Composer(cfg, clock=lambda: 0.0)
    c.assert_arm([True] * 6, 1, names=NAMES)
    c.assert_arm([True] * 6, 2, names=NAMES)
    o = c.tick()
    check(o.universe == bytes(512),
          "two arm requests before the first tick: the first frame is zeros")
    check(all(g["armed"] == "held" and g["reason"] == composer.LINK_NEVER
              and g["amber"] == "steady" for g in o.status["groups"]),
          f"every group held, and with no show program yet the lamp says "
          f"so: {[(g['armed'], g['reason']) for g in o.status['groups']]}")
    check(c.stats["latch_resets"] == 0,
          "and no latch had to be cleared, because none was ever set")
    # The same two requests, with the show program answering: still zeros,
    # and now the operator is told to cycle.
    c = Composer(cfg, clock=lambda: 0.0)
    c.ingest_frame(link.FlameFrame(1, None, 0.0, 1, bytes(512)), SENDER)
    c.assert_arm([True] * 6, 1, names=NAMES)
    c.assert_arm([True] * 6, 2, names=NAMES)
    o = c.tick()
    check(o.universe == bytes(512) and all(
        g["reason"] == "cycle the arm" for g in o.status["groups"]),
        "with a live link the first frame is still zeros, held for a cycle")


# =========================================================================
# rule 6: consent
# =========================================================================

def test_rule6_consent():
    section("rule 6: a group arms only after a live down edge")
    r = Rig()
    # One assertion proves nothing: the counter has not been seen to move,
    # and the status must not claim otherwise.
    r.step(n=1)
    check(r.out.status["arm_input"]["state"] == "never",
          f"after one assertion the input is 'never', not live: "
          f"{r.out.status['arm_input']}")
    r.inp.set(0)
    o = r.step()
    check(r.out.status["arm_input"]["state"] == "live",
          "after the counter was seen to advance the input is live")
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

    # Review: an input that resumes after a stale gap, with its counter
    # still climbing, asserting all-down and then arm, re-armed with nobody
    # touching a key.  After a gap the counter is forgotten: the first
    # assertion back is a first one and proves nothing.
    r4 = armed_rig()
    r4.inp.silent = True
    r4.wait(0.6)
    check(r4.safety(0) == 0, "stale gap: disarmed")
    r4.inp.silent = False
    r4.inp.seq += 1000           # a counter that kept climbing meanwhile
    r4.inp.set_all(False)
    r4.step()
    r4.inp.set(0)
    r4.step()
    r4.wait(1.25)
    check(r4.safety(0) == 0 and r4.group(0)["reason"] == "cycle the arm",
          f"the first assertion after a stale gap is not consent: "
          f"{r4.group(0)['reason']!r}")
    r4.inp.set(0, on=False)
    r4.step()
    r4.inp.set(0)
    r4.wait(1.25)
    check(r4.safety(0) == ARM, "a real cycle after the gap arms it")

    # Review: an input that restarts (counter back to 0) asserting all-down
    # and then arm, with the operator never touching a key.
    r5 = armed_rig()
    r5.inp.reboot()
    r5.inp.set_all(False)
    r5.step()
    check(r5.safety(0) == 0 and "latch-reset" in r5.log.kinds(),
          "a restart disarms at once")
    r5.inp.set(0)
    r5.step()
    r5.wait(1.25)
    check(r5.safety(0) == 0 and r5.group(0)["reason"] == "cycle the arm",
          f"the first assertion after a restart is not consent: "
          f"{r5.group(0)['reason']!r}")

    # Review: latched, the counter freezes, the operator cycles while it is
    # frozen, then it thaws.  The frozen down edge cleared the latch and
    # was not consent, so the group needs a real cycle.
    r6 = armed_rig()
    r6.inp.freeze()
    r6.inp.set(0, on=False)
    r6.step()
    r6.inp.set(0)
    r6.step()
    r6.inp.thaw()
    r6.wait(1.25)
    check(r6.safety(0) == 0 and r6.group(0)["reason"] == "cycle the arm",
          f"a down edge on a frozen counter clears the latch and is not "
          f"consent: {r6.safety(0)}, {r6.group(0)['reason']!r}")

    # The assertion's own shape: a negative seq, a wrong length, non-bools
    # and wrong group names are all rejected and leave the counter alone.
    r7 = armed_rig()
    before = r7.c._arm_seq
    bad = [r7.c.assert_arm([True] * 6, -1),
           r7.c.assert_arm([True] * 7, before + 1),
           r7.c.assert_arm([1] * 6, before + 1),
           r7.c.assert_arm("yes", "no"),
           r7.c.assert_arm([True] * 6, before + 1, names=NAMES[::-1]),
           r7.c.assert_arm([True] * 6, before + 1, names=NAMES[:5])]
    check(bad == [False] * 6 and r7.c.stats["arm_rejected"] == 6
          and r7.c._arm_seq == before,
          f"six malformed assertions rejected, counter untouched: {bad}")
    # Safety review of PR #31, item 6: a group-name mismatch used to be
    # dropped in total silence (the generic except below just counted it,
    # like a malformed shape); it is now its own journal line, naming the
    # names it got and what it expected. The other four malformed shapes
    # above are driver bugs, not a group-map mismatch, and stay uncounted
    # here on purpose: only the name-mismatch rejections should have
    # written anything.
    #
    # Round 2 of the safety review, item 10: a sustained mismatch (a
    # misconfigured deck asserting 10+ Hz) used to write one line PER
    # assertion, which could flood flamesafe's bounded (1000-line) journal
    # queue and push other lines out. It is now logged once for the whole
    # continuous episode -- the SECOND mismatch here, even though its
    # names differ from the first, is still the same ongoing episode (the
    # reason, "names do not match", has not cleared in between) -- with a
    # running count, and a single recovery line once a good assertion
    # finally arrives.
    name_lines = [m for k, m in r7.log.events if k == "arm-link"]
    check(len(name_lines) == 1
          and "do not match this config's" in name_lines[0]
          and repr(list(NAMES[::-1])) in name_lines[0]
          and "not be logged individually" in name_lines[0],
          f"only the FIRST group-name mismatch opens the episode and is "
          f"journaled, naming the names and that they do not match: "
          f"{name_lines}")
    check(r7.c.stats["arm_rejected"] == 6,
          "both mismatches (and the other four malformed shapes) still "
          "count in stats even though only one opened the journal line")
    check(r7.c.assert_arm([True] * 6, before + 1, names=NAMES),
          "the same assertion with the right names is accepted")
    recovery_lines = [m for k, m in r7.log.events if k == "arm-link"
                      and "matching this config's group names again" in m]
    check(len(recovery_lines) == 1 and "2 rejected" in recovery_lines[0],
          f"and closes the episode with one recovery line naming the "
          f"total rejected (2, the two name mismatches above): "
          f"{recovery_lines}")


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
    r.run(6, {411: 255})
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
    r.run(5, {411: 200})
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
    check(seen, "the re-arm request was held at all (a dwell that never "
                "applies arms on the first tick)")
    check(all(s[0] == "re-arm dwell" and s[1] == "steady" for s in seen),
          f"held with steady amber and the dwell reason: {set(seen)}")
    countdown = [s[2] for s in seen] or [0]
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
    r = Rig(test_only_dwell_ms=100)
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
    check(fourth == 0 and g["reason"] == "chatter" and g["amber"] == "steady",
          f"the fourth rise is refused and held steady as chatter: {g}")
    check(r.c.stats["chatter_holds"] >= 1 and "chatter" in r.log.kinds(),
          "the chatter hold is counted and logged")
    # The refusal starts the dwell from now: the next ticks are a hold with
    # a countdown, and they still read "chatter", not "re-arm dwell".
    seen = set()
    for _ in range(3):
        r.step()
        g = r.group(0)
        seen.add((g["reason"], g["dwell_s"] >= 1, g["amber"]))
    check(seen == {("chatter", True, "steady")},
          f"the hold chatter started reads chatter with a countdown: {seen}")
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
    r.run(5, {411: 200})
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


def test_ltcplay_stale_zeros_fire_then_disarms():
    section("liveness: ltcplay going quiet zeros the fire slots inside "
            "fire_hold_ms and disarms every group at frame_stale_ms")
    hold = 0.1 + 0.025 + 1e-9           # fire_hold_ms plus one tick
    lost = 0.5 + 0.025 + 1e-9           # frame_stale_ms plus one tick
    for how in ("dead", "stuck seq"):
        r = armed_rig()
        r.run(4, {411: 255})
        check(r.fire(0)[0] == 255, f"{how}: firing")
        r.link_alive = False
        t_last = r.t
        r.frame({411: 255})
        zero_at = None
        while r.t - t_last < 2.0:
            r.step()
            if how == "stuck seq" and r.t - t_last <= 0.5:
                # ltcplay keeps sending, with the same seq every time
                check("out of order" in r.frame({411: 255}, seq=r.seq),
                      "a stuck seq is rejected while the link is live")
            if zero_at is None and r.fire(0)[0] == 0:
                zero_at = r.t - t_last
                check(r.safety(0) == ARM and r.group(0)["armed"] == "armed",
                      f"{how}: at {zero_at:.3f} s the fire is zero and the "
                      f"arm is still up")
                check(r.out.status["frames"]["fire"] == "zeroed"
                      and r.out.status["frames"]["state"] == "fresh",
                      f"{how}: the status says fire zeroed, link fresh: "
                      f"{r.out.status['frames']}")
            if r.safety(0) == 0:
                break
        check(zero_at is not None and zero_at <= hold,
              f"{how}: the fire slot is zero after {zero_at} s (rev 5 had "
              f"no hold; fire_hold_ms is 100)")
        gone_at = r.t - t_last
        check(r.safety(0) == 0 and gone_at <= lost,
              f"{how}: the arm value came off the safety slot after "
              f"{gone_at:.3f} s")
        check(r.out.status["frames"]["state"] == "stale",
              f"{how}: and the status says the link is stale")
    # A new ltcplay (sequence restarted) is accepted once the link is stale.
    r.wait(0.6)
    check(r.frame({411: 100}, seq=0) == "",
          "after the link went stale a restarted sequence is accepted")
    r.step()
    check(r.out.status["frames"]["fire"] == "passing" and r.fire(0)[0] == 0,
          "its frames pass, but nothing fires: the group is disarmed")


# =========================================================================
# the link: reject everything that is not the contract
# =========================================================================

def test_link_rejects_malformed_datagrams():
    section("the link rejects malformed, wrong-version and wrong-type "
            "datagrams with a reason")
    good = {"v": 2, "k": KEY, "t": "flame", "seq": 5, "tc": "00:01:02:03",
            "mono": 1.5, "universe": 1, "values": [0] * 512}
    f = link.decode_flame(json.dumps(good).encode(), 1, KEY)
    check(f.seq == 5 and f.timecode == "00:01:02:03" and f.mono == 1.5
          and len(f.values) == 512, "a good frame decodes")
    check(link.decode_flame(link.encode_flame(7, None, 2.0, 1, [3] * 512,
                                              KEY), 1, KEY).values
          == bytes([3] * 512), "encode_flame round-trips")

    def bad(msg, obj=None, raw=None):
        data = raw if raw is not None else json.dumps(obj).encode()
        try:
            link.decode_flame(data, 1, KEY)
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
    bad("wrong version", variant(v=1))
    bad("missing version", variant(v=KeyError))
    bad("wrong key", variant(k=KEY + "x"))
    bad("missing key", variant(k=KeyError))
    bad("key of the wrong type", variant(k=12345))
    bad("empty key", variant(k=""))
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
    bad("mono NaN", raw=b'{"v":2,"k":"' + KEY.encode() +
                        b'","t":"flame","seq":1,"tc":null,"mono":NaN,'
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
    check(link.decode_flame(json.dumps(variant(tc=None)).encode(), 1,
                            KEY).timecode is None,
          "a null timecode is allowed")
    check(link.decode_flame(json.dumps(variant(tc="01:02:03;04")).encode(),
                            1, KEY).timecode == "01:02:03;04",
          "drop-frame timecode is allowed")
    # The status frame carries the key, and a reader that checks it
    # rejects one without it.
    s = link.encode_status({"v": 2, "t": "status", "heartbeat": 1}, KEY)
    check(link.decode_status(s, KEY)["k"] == KEY,
          "the status frame carries the key")
    try:
        link.decode_status(s, KEY + "x")
        check(False, "a status frame with another key was accepted")
    except link.LinkError:
        check(True, "")
    try:
        link.decode_status(b'{"v":2,"t":"status","heartbeat":1}', KEY)
        check(False, "a status frame without a key was accepted")
    except link.LinkError:
        check(True, "")
    # A rejected datagram changes nothing on the wire.
    r = armed_rig()
    r.run(5, {411: 200})
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


def test_arm_link_rejects_malformed_datagrams_and_round_trips():
    section("the arm link (build step 7b) rejects malformed datagrams and "
            "round-trips a good one")
    good = {"v": 2, "k": KEY, "t": "arm", "seq": 3,
            "wanted": [True, False, False, False, False, False],
            "names": list(NAMES)}
    w, seq, names = link.decode_arm(json.dumps(good).encode(), 6, KEY)
    check(w == (True, False, False, False, False, False) and seq == 3
          and names == tuple(NAMES), "a good arm frame decodes")
    enc = link.encode_arm(9, [True] * 6, NAMES, KEY)
    w2, seq2, names2 = link.decode_arm(enc, 6, KEY)
    check(w2 == (True,) * 6 and seq2 == 9 and names2 == tuple(NAMES),
          "encode_arm round-trips")

    def bad(msg, obj=None, raw=None, n=6):
        data = raw if raw is not None else json.dumps(obj).encode()
        try:
            link.decode_arm(data, n, KEY)
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
    bad("too long", raw=b"{" + b" " * 20000 + b"}")
    bad("wrong version", variant(v=1))
    bad("wrong key", variant(k=KEY + "x"))
    bad("missing key", variant(k=KeyError))
    bad("wrong type", variant(t="flame"))
    bad("missing type", variant(t=KeyError))
    bad("negative seq", variant(seq=-1))
    bad("float seq", variant(seq=1.5))
    bad("boolean seq", variant(seq=True))
    bad("missing seq", variant(seq=KeyError))
    bad("wanted too short", variant(wanted=[True] * 5))
    bad("wanted too long", variant(wanted=[True] * 7))
    bad("wanted not booleans", variant(wanted=[1, 0, 0, 0, 0, 0]))
    bad("wanted not a list", variant(wanted="no"))
    bad("missing wanted", variant(wanted=KeyError))
    bad("names too short", variant(names=NAMES[:5]))
    bad("names not strings", variant(names=[1, 2, 3, 4, 5, 6]))
    bad("missing names", variant(names=KeyError))
    # decode_arm itself does not compare the names against any config: that
    # is the composer's job (assert_arm), so the wrong names for THIS
    # config still decode here, and are rejected one layer up instead.
    w3, _seq3, names3 = link.decode_arm(
        json.dumps(variant(names=["a", "b", "c", "d", "e", "f"])).encode(),
        6, KEY)
    check(names3 == ("a", "b", "c", "d", "e", "f"),
          "decode_arm itself does not police the names; the composer does")


def test_socket_arm_input_is_the_real_build_step_7b_driver():
    section("SocketArmInput: a keyed loopback link that really arms a "
            "group, rejects what it must, and goes silent on close")
    deck = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    inp = arminput.SocketArmInput("127.0.0.1", 0, KEY, 6)
    inp.open()
    port = inp._sock.getsockname()[1]

    def send(seq, wanted, names=NAMES, key=KEY, v=2, t="arm", sock=deck):
        sock.sendto(link.encode_arm(seq, wanted, names, key)
                    if (key == KEY and t == "arm" and v == 2) else
                    json.dumps({"v": v, "k": key, "t": t, "seq": seq,
                               "wanted": list(wanted),
                               "names": list(names)}).encode(),
                    ("127.0.0.1", port))
        time.sleep(0.01)

    check(inp.poll() is None, "nothing sent yet: poll() returns None")
    send(1, [False] * 6)
    a = inp.poll()
    check(a is not None and a.wanted == (False,) * 6 and a.seq == 1
          and a.names == tuple(NAMES), f"a good frame is read back: {a}")
    check(inp.poll() is None, "nothing NEW since the last poll: None again")
    # A flood: only the last one decoded this poll is kept.
    for s in range(2, 8):
        send(s, [s % 2 == 0] * 6)
    a = inp.poll()
    check(a.seq == 7, f"a flood keeps only the last one decoded: {a.seq}")
    # Rejected: wrong key, wrong shape. Each changes nothing.
    log = Log()
    inp2 = arminput.SocketArmInput("127.0.0.1", 0, KEY, 6, log=log)
    inp2.open()
    port2 = inp2._sock.getsockname()[1]
    deck.sendto(link.encode_arm(1, [True] * 6, NAMES, KEY + "x"),
               ("127.0.0.1", port2))
    deck.sendto(json.dumps({"v": 2, "k": KEY, "t": "arm", "seq": 1,
                           "wanted": [True] * 5, "names": list(NAMES)}
                          ).encode(), ("127.0.0.1", port2))
    time.sleep(0.02)
    check(inp2.poll() is None, "wrong key and wrong shape: both rejected")
    check(len(log.events) == 2 and all(k == "arm-link" for k, _ in log.events),
          f"each rejection is journaled once: {log.events}")
    inp.close()
    inp2.close()
    deck.close()
    check(inp.poll() is None, "closed: poll() returns None, not an error")


def test_socket_arm_input_sender_lock():
    section("SocketArmInput: a second local sender is rejected while the "
            "first is live, and a stale lock releases for a new one "
            "(safety review of PR #31, item 1)")
    t = [0.0]
    log = Log()
    inp = arminput.SocketArmInput("127.0.0.1", 0, KEY, 6, log=log,
                                  stale_ms=200, clock=lambda: t[0])
    inp.open()
    port = inp._sock.getsockname()[1]
    deck = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    deck.bind(("127.0.0.1", 0))
    rogue = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rogue.bind(("127.0.0.1", 0))

    def send(sock, seq, wanted):
        sock.sendto(link.encode_arm(seq, wanted, NAMES, KEY),
                   ("127.0.0.1", port))
        time.sleep(0.01)

    send(deck, 1, [False] * 6)
    a = inp.poll()
    check(a is not None and a.seq == 1, "the real deck's first frame locks "
                                       "it in as the sender")
    send(rogue, 10 ** 6, [True] * 6)
    a = inp.poll()
    check(a is None, "a rogue sender's frame is rejected outright: nothing "
                     "to decode this poll")
    check(any(k == "arm-link" and "another sender" in m
              for k, m in log.events),
          f"the rejection is journaled, not dropped in silence: "
          f"{log.events}")
    send(deck, 2, [True, False, False, False, False, False])
    a = inp.poll()
    check(a is not None and a.seq == 2 and a.wanted[0] is True,
          f"the real deck's own next frame still goes through: {a.wanted}")
    check(a.wanted != [True] * 6,
          "the rogue's all-True frame from before never reached the "
          "composer: Abort could not have been masked by it")
    # Once the lock goes stale (nothing accepted for stale_ms), a new
    # sender -- even the same rogue -- is accepted, exactly like the
    # flame-frame link's own lock (CONTRACT.md).
    t[0] += 0.3
    send(rogue, 1, [False] * 6)
    a = inp.poll()
    check(a is not None and a.seq == 1,
          f"after the lock goes stale a new sender is taken: {a}")
    inp.close()
    deck.close()
    rogue.close()


def test_socket_arm_input_foreign_sender_can_still_disarm():
    section("SocketArmInput: once a rogue holds the lock (the real deck "
            "was briefly quiet), the real deck's own Abort is rejected as "
            "'another sender' but still forces every group it says False "
            "for to False in whatever poll() returns -- a foreign frame "
            "can only ever disarm, never arm, even while it is NOT the "
            "locked sender (round 2 of the safety review, item 1: the "
            "sender lock alone left this hole -- a rogue that becomes the "
            "lock holder while the real deck is quiet for stale_ms can "
            "hold it indefinitely, and the real deck's own Abort used to "
            "be rejected outright, doing nothing)")
    t = [0.0]
    log = Log()
    inp = arminput.SocketArmInput("127.0.0.1", 0, KEY, 6, log=log,
                                  stale_ms=200, clock=lambda: t[0])
    inp.open()
    port = inp._sock.getsockname()[1]
    deck = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    deck.bind(("127.0.0.1", 0))
    rogue = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rogue.bind(("127.0.0.1", 0))

    def send(sock, seq, wanted):
        sock.sendto(link.encode_arm(seq, wanted, NAMES, KEY),
                   ("127.0.0.1", port))
        time.sleep(0.01)

    # The real deck is quiet past stale_ms (a reconnect, a restart, boot
    # ordering): the rogue sends first and becomes the LOCKED sender, for
    # real -- exactly as a real deck reconnecting later would.
    send(rogue, 1, [True] * 6)
    a = inp.poll()
    check(a is not None and a.wanted == (True,) * 6,
          f"the rogue is accepted as the sender (nothing has locked it out "
          f"yet) and arms every group: {a}")

    # The real deck reconnects and sends Abort (all False). Its frame is
    # rejected as "another sender" -- the rogue already holds the lock --
    # but its disarm must still take effect.
    send(deck, 1, [False] * 6)
    a = inp.poll()
    check(a is None, "the real deck's Abort is rejected outright as "
                     "'another sender': it is not the locked sender")
    check(any(k == "arm-link" and "another sender" in m
              and "disarm bits still apply" in m for k, m in log.events),
          f"the rejection is journaled, and says its disarm still counts: "
          f"{log.events}")

    # The rogue keeps re-asserting True to hold the lock and mask the
    # Abort. Without the fix this is exactly how an Abort gets masked.
    send(rogue, 2, [True] * 6)
    a = inp.poll()
    check(a is not None and a.wanted == (False,) * 6,
          f"the rogue's own next frame is accepted (it is still the "
          f"locked sender, seq={a.seq if a else None}), but the real "
          f"deck's tracked foreign False is ANDed in: every group reads "
          f"False, not the rogue's True: {a}")
    check(a.seq == 2, "seq still comes from the locked (rogue) sender: "
                      "the composer's own consent/liveness math is "
                      "untouched by the foreign AND")

    # The real deck's foreign assertion goes stale after stale_ms with no
    # further frames from it: the AND then stops applying, since there is
    # nothing left to honestly track.
    t[0] += 0.3
    send(rogue, 3, [True] * 6)
    a = inp.poll()
    check(a is not None and a.wanted == (True,) * 6,
          f"once the real deck's foreign assertion has gone stale, the "
          f"rogue's True is no longer clipped: {a}")

    inp.close()
    deck.close()
    rogue.close()


def test_socket_arm_input_foreign_sender_episode_logged_once():
    section("SocketArmInput: a sustained foreign-sender flood logs once "
            "for the episode plus a running count, not once per datagram "
            "(item 10, round 2 of the safety review)")
    t = [0.0]
    log = Log()
    inp = arminput.SocketArmInput("127.0.0.1", 0, KEY, 6, log=log,
                                  stale_ms=200, clock=lambda: t[0])
    inp.open()
    port = inp._sock.getsockname()[1]
    deck = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    deck.bind(("127.0.0.1", 0))
    rogue = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rogue.bind(("127.0.0.1", 0))

    def send(sock, seq, wanted):
        sock.sendto(link.encode_arm(seq, wanted, NAMES, KEY),
                   ("127.0.0.1", port))
        time.sleep(0.005)

    send(deck, 1, [False] * 6)
    inp.poll()
    for s in range(2, 22):
        send(rogue, s, [False] * 6)
        inp.poll()
    rejections = [m for k, m in log.events
                 if k == "arm-link" and "another sender" in m]
    check(len(rejections) == 1,
          f"20 rejections from the same foreign sender produce ONE "
          f"journal line while it keeps re-asserting, not 20: "
          f"{len(rejections)}")
    # Once it has gone quiet for stale_ms, the episode closes with a
    # summary line naming how many were rejected.
    t[0] += arminput.EPISODE_QUIET_S + 0.1   # round 4: episodes close after this, not stale_ms
    send(deck, 2, [False] * 6)
    inp.poll()
    closers = [m for k, m in log.events
              if k == "arm-link" and "stopped after" in m]
    check(len(closers) == 1 and "20 rejected" in closers[0],
          f"and a single closing line gives the running count once the "
          f"foreign sender goes quiet: {closers}")
    inp.close()
    deck.close()
    rogue.close()


def test_socket_arm_input_foreign_flood_from_varying_source_ports_logged_once():
    section("SocketArmInput: a foreign sender varying its OWN source port "
            "on every single frame still produces only ONE opening "
            "journal line and one closing summary for the whole episode, "
            "never one per address (round 3 of the safety review, item 5 "
            "-- a PROVEN attack: round 2's own rate limit, just above, "
            "keyed off whether an ADDRESS was already being tracked, "
            "which a rogue defeats by simply never reusing one; an "
            "independent review ran this for real and produced ~26,000 "
            "journal lines in 5 s from about 4,000 distinct source ports)")
    t = [0.0]
    log = Log()
    inp = arminput.SocketArmInput("127.0.0.1", 0, KEY, 6, log=log,
                                  stale_ms=200, clock=lambda: t[0])
    inp.open()
    port = inp._sock.getsockname()[1]
    deck = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    deck.bind(("127.0.0.1", 0))

    def send(sock, seq, wanted):
        sock.sendto(link.encode_arm(seq, wanted, NAMES, KEY),
                   ("127.0.0.1", port))
        time.sleep(0.002)

    send(deck, 1, [False] * 6)
    inp.poll()

    n_rogues = 40
    rogues = []
    for i in range(n_rogues):
        r = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        r.bind(("127.0.0.1", 0))         # a BRAND NEW source port every time
        rogues.append(r)
        send(r, i + 1, [False] * 6)
        inp.poll()

    rejections = [m for k, m in log.events
                 if k == "arm-link" and "another sender" in m]
    check(len(rejections) == 1,
          f"{n_rogues} rejections, every one from a DIFFERENT source "
          f"port, still produce exactly ONE journal line, not {n_rogues}: "
          f"{len(rejections)}")

    # Once every one of them has gone quiet for stale_ms, the episode
    # closes with ONE summary line naming the total count AND how many
    # distinct addresses were actually involved.
    t[0] += arminput.EPISODE_QUIET_S + 0.1   # round 4: episodes close after this, not stale_ms
    send(deck, 2, [False] * 6)
    inp.poll()
    closers = [m for k, m in log.events
              if k == "arm-link" and "stopped after" in m]
    check(len(closers) == 1 and f"{n_rogues} rejected" in closers[0]
          and f"{n_rogues} distinct source address" in closers[0],
          f"a single closing line gives the running count and the "
          f"distinct-address count, not {n_rogues} separate lines: "
          f"{closers}")

    inp.close()
    deck.close()
    for r in rogues:
        r.close()


def test_socket_arm_input_foreign_count_tracks_live_foreign_senders():
    section("SocketArmInput.foreign_count: how many OTHER senders are "
            "currently tracked as fresh, read right after poll() (round 3 "
            "of the safety review, item 6) -- the composer's status frame "
            "carries this so the deck can raise an alarm the instant "
            "anyone else is on the link, even on a tick where the AND "
            "happens to leave `wanted` looking exactly like what the deck "
            "itself expects")
    t = [0.0]
    log = Log()
    inp = arminput.SocketArmInput("127.0.0.1", 0, KEY, 6, log=log,
                                  stale_ms=200, clock=lambda: t[0])
    inp.open()
    port = inp._sock.getsockname()[1]
    deck = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    deck.bind(("127.0.0.1", 0))
    rogue1 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rogue1.bind(("127.0.0.1", 0))
    rogue2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rogue2.bind(("127.0.0.1", 0))

    def send(sock, seq, wanted):
        sock.sendto(link.encode_arm(seq, wanted, NAMES, KEY),
                   ("127.0.0.1", port))
        time.sleep(0.01)

    send(deck, 1, [True] * 6)
    inp.poll()
    check(inp.foreign_count == 0, "nobody else has ever sent a frame")

    send(rogue1, 1, [False] * 6)
    inp.poll()
    check(inp.foreign_count == 1, f"one foreign sender is now tracked: "
                                  f"{inp.foreign_count}")

    send(rogue2, 1, [False] * 6)
    inp.poll()
    check(inp.foreign_count == 2, f"and a second, distinct one: "
                                  f"{inp.foreign_count}")

    # Once both have gone quiet for stale_ms, poll()'s own cleanup drops
    # them, and the count reads zero again without anyone re-locking.
    t[0] += 0.3
    send(deck, 2, [True] * 6)
    inp.poll()
    check(inp.foreign_count == 0,
          f"both foreign episodes have closed out: {inp.foreign_count}")

    inp.close()
    deck.close()
    rogue1.close()
    rogue2.close()


def test_composer_status_carries_foreign_arm_senders():
    section("composer status: arm_input.foreign_senders carries what the "
            "service last reported from the arm input, independent of "
            "whether assert_arm was ALSO called that tick (round 3 of the "
            "safety review, item 6)")
    r = armed_rig()
    check(r.group(0)["armed"] == "armed", "setup: group 0 is armed")
    check(r.out.status["arm_input"]["foreign_senders"] == 0,
          f"nothing foreign has ever been reported: "
          f"{r.out.status['arm_input']}")
    r.c.note_foreign_arm_senders(3)
    r.step()
    check(r.out.status["arm_input"]["foreign_senders"] == 3,
          f"the composer carries whatever the service last told it: "
          f"{r.out.status['arm_input']}")
    # A bad value is never counted, and never raises: this is a display
    # signal, not a safety one, and must not be able to crash a tick.
    r.c.note_foreign_arm_senders("not a number")
    r.step()
    check(r.out.status["arm_input"]["foreign_senders"] == 3,
          f"a bad value is ignored, not crashed on: "
          f"{r.out.status['arm_input']}")
    r.c.note_foreign_arm_senders(0)
    r.step()
    check(r.out.status["arm_input"]["foreign_senders"] == 0,
          "and it can be told the episode has ended")


def test_service_journals_a_raising_assert_arm():
    section("service: if assert_arm ever raised (it must not, by its own "
            "contract), the service journals it instead of dropping it in "
            "silence (safety review of PR #31, item 6)")
    cfg = make_config()
    log = Log()

    class _OneAssertion(arminput.ArmInput):
        def __init__(self):
            self.polled = False

        def poll(self):
            if self.polled:
                return None
            self.polled = True
            return arminput.ArmAssertion([False] * cfg.n, 1)

    svc = Service(cfg, _OneAssertion(), log=log)

    def boom(*a, **kw):
        raise RuntimeError("deliberately broken for this test")

    svc.composer.assert_arm = boom
    svc._poll_arm()
    check(svc.input_errors == 1, "the bad assertion is counted")
    check(any(k == "arm-input" and "RuntimeError" in m
              for k, m in log.events),
          f"and journaled, not dropped in silence: {log.events}")


def test_socket_arm_input_really_arms_a_group_end_to_end():
    section("the arm link end to end: a real UDP frame arms a real group "
            "through a real Service and Composer tick")
    node = _udp()
    ltc_status = _udp()
    listen_port = _udp()
    lp = listen_port.getsockname()[1]
    listen_port.close()
    arm_port_sock = _udp()
    ap = arm_port_sock.getsockname()[1]
    arm_port_sock.close()
    cfg = make_config(destination={"ip": "127.0.0.1",
                                   "port": node.getsockname()[1]},
                      link={"listen_ip": "127.0.0.1", "listen_port": lp,
                            "status_ip": "127.0.0.1",
                            "status_port": ltc_status.getsockname()[1],
                            "arm_port": ap, "key": KEY})
    check(cfg.link_arm_port == ap, "the config carries the arm port")
    t = [0.0]
    log = Log()
    arm_input = arminput.SocketArmInput(cfg.link_arm_ip, cfg.link_arm_port,
                                        cfg.link_key, cfg.n, log=log)
    svc = Service(cfg, arm_input, clock=lambda: t[0], log=log)
    svc.open()
    deck = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ltc_tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def tick():
        t[0] += cfg.tick_period_s
        time.sleep(0.005)
        return svc.run_once()

    def send_arm(seq, wanted):
        deck.sendto(link.encode_arm(seq, wanted, NAMES, KEY), ("127.0.0.1", ap))
        time.sleep(0.01)

    def send_flame(seq, slots):
        vals = [0] * 512
        for s, v in slots.items():
            vals[s - 1] = v
        ltc_tx.sendto(link.encode_flame(seq, "00:00:00:01", t[0], 1, vals, KEY),
                     ("127.0.0.1", lp))
        time.sleep(0.01)

    fseq = [0]

    def keep_flame_alive():
        fseq[0] += 1
        send_flame(fseq[0], {})

    try:
        keep_flame_alive()
        send_arm(1, [False] * 6)
        tick()
        s = link.decode_status(_drain(ltc_status)[-1], KEY)
        check(s["groups"][0]["armed"] == "disarmed",
              "the first assertion proves nothing: still disarmed")
        for seq in (2, 3):
            send_arm(seq, [False] * 6)
            tick()
        send_arm(4, [True, False, False, False, False, False])
        keep_flame_alive()
        tick()
        s = link.decode_status(_drain(ltc_status)[-1], KEY)
        check(s["groups"][0]["armed"] == "armed",
              f"a real Stream Deck datagram, decoded by a real socket, "
              f"really arms the group: {s['groups'][0]}")
        # Unplugging the deck (no more datagrams): every group disarms
        # within arm_stale_ms, with nothing more sent on this link.
        stale_ticks = int(cfg.arm_stale_ms / 1000.0 / cfg.tick_period_s) + 3
        for _ in range(stale_ticks):
            keep_flame_alive()  # the FLAME link stays alive; only the arm
                                # link goes silent, isolating what disarms it
            tick()
        s = link.decode_status(_drain(ltc_status)[-1], KEY)
        g = s["groups"][0]
        # Still asking (wanted stays True: nobody touched the key), but
        # refused and off the wire: sent_safety 0 is the fact that matters,
        # "held"/"arm input stale" is the lamp that explains why (CONTRACT.md).
        check(g["sent_safety"] == 0 and g["armed"] != "armed"
              and "stale" in g["reason"],
              f"the deck going silent (unplugged, crashed, killed) disarms "
              f"the group's real output within arm_stale_ms, with no new "
              f"code for it -- the EXISTING staleness rule did this: {g}")
        check(s["arm_input"]["state"] == "stale",
              f"the status frame itself says the arm input is stale: "
              f"{s['arm_input']}")
    finally:
        svc.close()
        deck.close()
        ltc_tx.close()
        node.close()
        ltc_status.close()


def test_socket_arm_input_reports_which_bits_were_forced():
    section("SocketArmInput: poll() reports, per group, which bits in "
            "`wanted` it forced False by the foreign-disarm AND, versus "
            "genuinely reported by the locked sender (round 3 of the "
            "safety review, item 1) -- composer.assert_arm needs this to "
            "tell a FORCED low from a real one, which is the whole fix: "
            "see test_round3_foreign_forced_edge_is_not_consent_end_to_end "
            "for why that distinction matters")
    t = [0.0]
    log = Log()
    inp = arminput.SocketArmInput("127.0.0.1", 0, KEY, 6, log=log,
                                  stale_ms=200, clock=lambda: t[0])
    inp.open()
    port = inp._sock.getsockname()[1]
    deck = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    deck.bind(("127.0.0.1", 0))
    rogue = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rogue.bind(("127.0.0.1", 0))

    def send(sock, seq, wanted):
        sock.sendto(link.encode_arm(seq, wanted, NAMES, KEY),
                   ("127.0.0.1", port))
        time.sleep(0.01)

    send(deck, 1, [True] * 6)
    a = inp.poll()
    check(a is not None and a.forced is None,
          f"nothing is forced yet: forced is None, exactly like a driver "
          f"that never forces anything: {a.forced}")

    send(rogue, 1, [False, True, True, True, True, True])
    a = inp.poll()
    check(a is None, "the rogue's own frame is rejected outright: it is "
                     "not the locked sender")
    send(deck, 2, [True] * 6)
    a = inp.poll()
    check(a is not None and a.wanted == (False, True, True, True, True, True),
          f"the foreign False clips group 0 in the result, exactly as "
          f"round 2 already proved: {a.wanted}")
    check(a.forced == (True, False, False, False, False, False),
          f"and ONLY group 0 is reported forced -- the locked sender's own "
          f"report for every other group was already True and was never "
          f"touched by the AND: {a.forced}")

    # The rogue flips its OWN forged bit back to True: the clip stops at
    # once (no need to wait out stale_ms), and nothing is forced any more.
    send(rogue, 2, [True] * 6)
    send(deck, 3, [True] * 6)
    a = inp.poll()
    check(a is not None and a.wanted == (True,) * 6 and a.forced is None,
          f"once the rogue stops forcing, forced reads None again: "
          f"wanted={a.wanted} forced={a.forced}")

    inp.close()
    deck.close()
    rogue.close()


def test_round3_foreign_forced_edge_is_not_consent_end_to_end():
    section("round 3 of the safety review, item 1 (the proven attack, run "
            "here against the real Service/SocketArmInput/Composer): a "
            "foreign sender that forces a group's wanted bit False for a "
            "while, then lets it go True again, must NOT read as the "
            "operator cycling the arm -- even though the LOCKED sender "
            "(the real deck) never stopped asking for True the whole "
            "time. Round 2's foreign-disarm fix made a foreign frame able "
            "to only ever CLEAR a bit, specifically so a rogue holding "
            "the lock could not mask a real Abort; this is the attack "
            "that fix opened back up (an independent review ran it for "
            "real and the group ended up ARMED with no operator action).")
    node = _udp()
    ltc_status = _udp()
    listen_port = _udp()
    lp = listen_port.getsockname()[1]
    listen_port.close()
    arm_port_sock = _udp()
    ap = arm_port_sock.getsockname()[1]
    arm_port_sock.close()
    cfg = make_config(destination={"ip": "127.0.0.1",
                                   "port": node.getsockname()[1]},
                      link={"listen_ip": "127.0.0.1", "listen_port": lp,
                            "status_ip": "127.0.0.1",
                            "status_port": ltc_status.getsockname()[1],
                            "arm_port": ap, "key": KEY})
    t = [0.0]
    log = Log()
    arm_input = arminput.SocketArmInput(cfg.link_arm_ip, cfg.link_arm_port,
                                        cfg.link_key, cfg.n, log=log,
                                        stale_ms=cfg.arm_stale_ms,
                                        clock=lambda: t[0])
    svc = Service(cfg, arm_input, clock=lambda: t[0], log=log)
    svc.open()
    deck = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rogue = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ltc_tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def tick():
        t[0] += cfg.tick_period_s
        time.sleep(0.002)
        return svc.run_once()

    def send_arm(sock, seq, wanted):
        sock.sendto(link.encode_arm(seq, wanted, NAMES, KEY), ("127.0.0.1", ap))
        time.sleep(0.005)

    fseq = [0]

    def keep_flame_alive():
        fseq[0] += 1
        ltc_tx.sendto(link.encode_flame(fseq[0], "00:00:00:01", t[0], 1,
                                        [0] * 512, KEY), ("127.0.0.1", lp))
        time.sleep(0.002)

    def status():
        return link.decode_status(_drain(ltc_status)[-1], KEY)

    want0 = [True] + [False] * (cfg.n - 1)
    all_false = [False] * cfg.n
    dseq = [0]

    def deck_send(wanted):
        dseq[0] += 1
        send_arm(deck, dseq[0], wanted)

    dwell_ticks = int(cfg.min_arm_dwell_ms / 1000.0 / cfg.tick_period_s) + 3
    try:
        # Setup: a genuine cycle really arms group 0, with the deck as the
        # only sender that has ever touched the arm link.
        keep_flame_alive(); deck_send(all_false); tick()
        keep_flame_alive(); deck_send(all_false); tick()
        keep_flame_alive(); deck_send(all_false); tick()
        keep_flame_alive(); deck_send(want0); tick()
        for _ in range(dwell_ticks):
            keep_flame_alive(); deck_send(want0); tick()
        s = status()
        check(s["groups"][0]["armed"] == "armed",
              f"setup: a genuine cycle really arms the group: "
              f"{s['groups'][0]}")

        # THE ATTACK. A rogue on this machine (a brand new source port --
        # no sender lock has ever been contested) forces the bit False for
        # one assertion, then flips its OWN forged bit back to True. The
        # real deck never once stops asking for True.
        rseq = [0]

        def rogue_send(wanted):
            rseq[0] += 1
            send_arm(rogue, rseq[0], wanted)

        rogue_send(all_false)
        keep_flame_alive(); deck_send(want0); tick()
        s = status()
        check(s["groups"][0]["armed"] != "armed",
              f"the forced False really disarms the output on the wire -- "
              f"round 2's fix, unweakened by this one: {s['groups'][0]}")

        rogue_send(want0)              # stops forcing; never ARMS by itself
        keep_flame_alive(); deck_send(want0); tick()
        # Enough ticks for the dwell window to fully clear, so what this
        # proves is about CONSENT, not a dwell countdown still running.
        for _ in range(dwell_ticks):
            keep_flame_alive(); deck_send(want0); tick()
        s = status()
        check(s["groups"][0]["armed"] != "armed"
              and s["groups"][0]["reason"] == "cycle the arm",
              f"round 3 fix: a foreign sender forcing a False-then-True "
              f"sequence through the arm link must NOT read as the "
              f"operator cycling the arm, no matter how long the dwell "
              f"window has had to clear -- the locked sender's own report "
              f"never actually changed, so there is nothing here to call "
              f"consent: {s['groups'][0]}")

        # The fix must not be a one-way ratchet: a REAL cycle from the
        # locked sender itself still arms the group afterwards.
        deck_send(all_false); keep_flame_alive(); tick()
        for _ in range(2):
            deck_send(all_false); keep_flame_alive(); tick()
        deck_send(want0); keep_flame_alive(); tick()
        for _ in range(dwell_ticks):
            deck_send(want0); keep_flame_alive(); tick()
        s = status()
        check(s["groups"][0]["armed"] == "armed",
              f"a REAL cycle from the locked sender itself still arms the "
              f"group afterwards -- the fix only refuses a FORCED edge, "
              f"never a genuine one: {s['groups'][0]}")
    finally:
        svc.close()
        deck.close()
        rogue.close()
        ltc_tx.close()
        node.close()
        ltc_status.close()


def test_round4_no_consent_while_another_sender_or_a_flood_is_on_the_link():
    section("round 4 of the safety review, item B: no consent edge counts "
            "while ANOTHER sender is on the arm link, or a flood has been "
            "seen on it, and no down edge seen before it turned up can be "
            "finished while it is there -- the round-4 review flooded the "
            "port until the real deck was crowded out, became the locked "
            "sender itself, and forged its own low-then-high")
    for label, disturb, calm in (
            ("another sender",
             lambda r: r.c.note_foreign_arm_senders(1),
             lambda r: r.c.note_foreign_arm_senders(0)),
            ("a flood",
             lambda r: r.c.note_arm_link_flooded(True),
             lambda r: r.c.note_arm_link_flooded(False))):
        r = Rig()
        r.prove_alive()          # genuine lows on a live counter
        disturb(r)
        r.inp.set(0)
        r.step(n=3)
        g = r.group(0)
        check(r.safety(0) != ARM,
              f"{label}: a low-then-high finished while {label} is on the "
              f"link does not arm: {g}")
        check(g["armed"] == "held" and g["reason"] == composer.OTHER_SENDER
              and g["amber"] == "steady",
              f"{label}: held, saying why, steady (cycling now would not "
              f"help): {g}")
        calm(r)
        r.wait(r.cfg.min_arm_dwell_ms / 1000.0 + 0.5)
        g = r.group(0)
        check(r.safety(0) != ARM and g["reason"] == "cycle the arm",
              f"{label}: once it has gone the group does NOT arm by "
              f"itself -- the down edge from before it turned up was "
              f"cleared, so the operator cycles again: {g}")
        r.inp.set(0, on=False)
        r.step(n=2)
        r.inp.set(0)
        r.wait(r.cfg.min_arm_dwell_ms / 1000.0 + 0.5)
        check(r.safety(0) == ARM,
              f"{label}: and a real cycle afterwards arms it: "
              f"{r.group(0)}")


def test_round4_a_forced_bit_never_blocks_a_genuine_cycle_on_another_group():
    section("round 4 (hand mutation the review found surviving): a FORCED "
            "low on one group must not stop a GENUINE low on another "
            "group from setting up that group's consent edge")
    r = Rig()
    r.inp.set_forced(1)          # group 1 reads forced low on every poll
    r.prove_alive()              # group 0's lows are genuine
    r.inp.set(0)
    r.step(n=2)
    check(r.safety(0) == ARM,
          f"group 0's genuine cycle arms it although group 1 is forced: "
          f"{r.group(0)}")
    check(r.safety(1) != ARM, "group 1 stays disarmed")


def test_round4_a_forced_low_clears_an_earlier_genuine_down_edge():
    section("round 4: in the composer itself, a FORCED low never sets up a "
            "consent edge and also clears one a genuine low set up earlier "
            "(round 3's rule, tested directly: since round 4 the service "
            "also blocks consent whenever a foreign sender is on the link, "
            "which hid this rule from the end-to-end test)")
    r = Rig()
    r.prove_alive()              # genuine lows: a pending down edge
    r.inp.set_forced(0)          # group 0's low is now FORCED
    r.step()
    r.inp.set_forced(0, on=False)
    r.inp.set(0)
    r.step(n=3)
    check(r.safety(0) != ARM,
          f"a True right after a forced low does not arm, even though a "
          f"genuine low came before it: {r.group(0)}")
    r.inp.set(0, on=False)
    r.step(n=2)
    r.inp.set(0)
    r.wait(r.cfg.min_arm_dwell_ms / 1000.0 + 0.5)
    check(r.safety(0) == ARM, f"a genuine cycle afterwards arms: "
                              f"{r.group(0)}")


def test_round4_a_malformed_forced_vector_is_rejected():
    section("round 4: assert_arm rejects a `forced` vector of the wrong "
            "length or with non-bool entries, like a malformed `wanted`")
    r = Rig()
    before = r.c.stats["arm_rejected"]
    w = [False] * N_GROUPS
    check(r.c.assert_arm(w, 1, names=NAMES, forced=[False]) is False,
          "too short: rejected")
    check(r.c.assert_arm(w, 2, names=NAMES, forced=[0] * N_GROUPS) is False,
          "not bools: rejected")
    check(r.c.stats["arm_rejected"] == before + 2, "and both are counted")
    check(r.c.assert_arm(w, 3, names=NAMES, forced=[False] * N_GROUPS)
          is True, "a well formed one is accepted")


def test_round4_genuine_lows_are_never_reported_forced():
    section("round 4 (hand mutation the review found surviving): a bit the "
            "LOCKED sender itself reports False is never reported forced, "
            "even while a foreign sender also says False for it -- only a "
            "bit the AND actually cleared is")
    t = [0.0]
    inp = arminput.SocketArmInput("127.0.0.1", 0, KEY, 6, log=Log(),
                                  stale_ms=200, clock=lambda: t[0])
    inp.open()
    port = inp._sock.getsockname()[1]
    deck = _udp()
    rogue = _udp()

    def send(sock, seq, wanted):
        sock.sendto(link.encode_arm(seq, wanted, NAMES, KEY),
                    ("127.0.0.1", port))
        time.sleep(0.01)

    try:
        send(deck, 1, [False] + [True] * 5)
        inp.poll()
        send(rogue, 1, [False] * 6)
        send(deck, 2, [False] + [True] * 5)
        a = inp.poll()
        check(a is not None and a.wanted == (False,) * 6,
              f"the rogue's False clears groups 1 to 5: {a and a.wanted}")
        check(a is not None and a.forced == (False,) + (True,) * 5,
              f"group 0's False is the deck's own, never 'forced'; groups "
              f"1 to 5 are: {a and a.forced}")
        check(a is not None and a.sender == deck.getsockname(),
              f"the assertion names its (locked) sender: {a and a.sender}")
    finally:
        inp.close()
        deck.close()
        rogue.close()


def test_round4_consent_never_spans_two_senders():
    section("round 4: when the arm input's sender lock changes hands, a "
            "down edge the OLD sender set up can never be finished by the "
            "new one -- every latch and pending edge is cleared, exactly "
            "as for an input restart")
    r = Rig()
    A, B = ("127.0.0.1", 40100), ("127.0.0.1", 40200)
    seq = [0]

    def send(wanted, sender, n=1):
        for _ in range(n):
            r.t += r.period
            r.frame(r.cue)
            seq[0] += 1
            r.c.assert_arm(list(wanted), seq[0], names=NAMES, sender=sender)
            r.out = r.c.tick()

    down = [False] * N_GROUPS
    want0 = [True] + [False] * (N_GROUPS - 1)
    send(down, A, n=3)            # A proves itself live and genuinely low
    send(want0, B, n=5)           # B takes over and asks for True
    check(r.safety(0) != ARM,
          f"B finishing A's edge does not arm: {r.group(0)}")
    send(down, B, n=2)
    send(want0, B, n=int((r.cfg.min_arm_dwell_ms / 1000.0 + 0.5)
                         / r.period))
    check(r.safety(0) == ARM,
          f"B's own genuine cycle does arm: {r.group(0)}")


def test_round4_socket_arm_input_flags_a_flood():
    section("round 4, item B: SocketArmInput reports `flooded` for stale_ms "
            "after a poll finds more datagrams waiting than any honest "
            "deck could have sent (keyed or not), and journals it once")
    t = [0.0]
    log = Log()
    inp = arminput.SocketArmInput("127.0.0.1", 0, KEY, 6, log=log,
                                  stale_ms=200, clock=lambda: t[0])
    inp.open()
    port = inp._sock.getsockname()[1]
    deck = _udp()
    rogue = _udp()
    try:
        deck.sendto(link.encode_arm(1, [False] * 6, NAMES, KEY),
                    ("127.0.0.1", port))
        time.sleep(0.01)
        inp.poll()
        check(not inp.flooded, "one frame a poll is not a flood")
        for _ in range(arminput.FLOOD_DATAGRAMS_PER_POLL + 20):
            rogue.sendto(b"\xff not json", ("127.0.0.1", port))
        time.sleep(0.05)
        inp.poll()
        check(inp.flooded, "a poll that found more than "
                           "FLOOD_DATAGRAMS_PER_POLL datagrams: flooded")
        t[0] += 0.15
        inp.poll()
        check(inp.flooded, "still flooded inside stale_ms of it")
        t[0] += 0.1
        inp.poll()
        check(not inp.flooded, "clear once stale_ms has passed with no "
                               "flood")
        floods = [m for k, m in log.events if "flooded" in m]
        check(len(floods) == 1, f"journaled once: {floods}")
    finally:
        inp.close()
        deck.close()
        rogue.close()


def test_round5_flood_thresholds_are_pinned_in_datagrams_and_bytes():
    section("round 5, item 4: a flood is MORE than 50 datagrams or MORE "
            "than 64 KiB in one poll (the kernel buffer fills by bytes: 12 "
            "maximum-size frames filled Linux's default one, far under 50 "
            "datagrams); the arm socket asks for a 4 MiB receive buffer, and "
            "where the kernel grants less the byte limit drops to a quarter "
            "of what it did grant, so a flood still shows before a small "
            "buffer fills.  Only 8 KiB datagrams here: macOS refuses to "
            "send a UDP datagram over 9216 bytes by default")
    # Literal numbers, not the module's constants: a test that reads the
    # constant moves along with it when someone changes it.
    check(arminput.FLOOD_DATAGRAMS_PER_POLL == 50
          and arminput.FLOOD_BYTES_PER_POLL == 65536
          and arminput.FLOOD_BYTES_FLOOR == 4096
          and arminput.ARM_RCVBUF_BYTES == 4 * 1024 * 1024,
          f"thresholds: {arminput.FLOOD_DATAGRAMS_PER_POLL} datagrams, "
          f"{arminput.FLOOD_BYTES_PER_POLL} bytes (floor "
          f"{arminput.FLOOD_BYTES_FLOOR}), receive buffer "
          f"{arminput.ARM_RCVBUF_BYTES}")
    check(arminput.flood_bytes_for(4 * 1024 * 1024) == 65536
          and arminput.flood_bytes_for(425984) == 65536
          and arminput.flood_bytes_for(65536) == 16384
          and arminput.flood_bytes_for(8192) == 4096
          and arminput.flood_bytes_for(None) == 4096,
          "the byte limit: 64 KiB where the buffer is 256 KiB or more, a "
          "quarter of the buffer below that, never under 4 KiB, and 4 KiB "
          "when the buffer size is unknown")

    def chunks(total):
        """`total` bytes as datagrams of at most 8 KiB."""
        out = []
        while total > 0:
            out.append(min(8192, total))
            total -= out[-1]
        return out

    def one_poll(sizes, rcvbuf=None):
        """A fresh input (asking for `rcvbuf` instead of the usual 4 MiB
        if given); send datagrams of these sizes from one rogue, poll
        once, and return (flooded, granted buffer, byte limit)."""
        t = [0.0]
        saved = arminput.ARM_RCVBUF_BYTES
        if rcvbuf is not None:
            arminput.ARM_RCVBUF_BYTES = rcvbuf
        try:
            inp = arminput.SocketArmInput("127.0.0.1", 0, KEY, 6,
                                          log=Log(), stale_ms=200,
                                          clock=lambda: t[0])
            inp.open()
        finally:
            arminput.ARM_RCVBUF_BYTES = saved
        rogue = _udp()
        try:
            port = inp._sock.getsockname()[1]
            for n in sizes:
                rogue.sendto(b"\xff" * n, ("127.0.0.1", port))
            time.sleep(0.05)
            inp.poll()
            return inp.flooded, inp.rcvbuf, inp.flood_bytes
        finally:
            inp.close()
            rogue.close()

    f50, _, _ = one_poll([20] * 50)
    f51, _, _ = one_poll([20] * 51)
    check(not f50, "exactly 50 small datagrams in one poll: not a flood")
    check(f51, "51 small datagrams in one poll: a flood")

    _, granted, limit = one_poll([])
    check(limit == arminput.flood_bytes_for(granted),
          f"the byte limit follows the buffer this kernel granted "
          f"({granted}): {limit}")
    at, _, _ = one_poll(chunks(limit))
    over, _, _ = one_poll(chunks(limit) + [1])
    check(not at, f"exactly the byte limit ({limit}) in one poll: not a "
                  f"flood")
    check(over, f"the byte limit and one byte, in only "
                f"{len(chunks(limit)) + 1} datagrams: a flood")
    big, _, _ = one_poll([8192] * 24)
    check(big, "the round-5 review's 12 maximum-size frames' worth of bytes "
               "(196 KiB, sent as 8 KiB datagrams): a flood")

    # A buffer the kernel caps or refuses: ask for only 16 KiB.  The limit
    # drops with it, and a flood well inside that small buffer still shows.
    _, small, small_limit = one_poll([], rcvbuf=16384)
    check(small_limit <= max(arminput.FLOOD_BYTES_FLOOR, small // 4),
          f"with a {small}-byte buffer the byte limit is at most a quarter "
          f"of it: {small_limit}")
    fs, _, _ = one_poll(chunks(small_limit) + [1], rcvbuf=16384)
    check(fs, f"and {small_limit + 1} bytes, which fit in that buffer "
              f"several times over, read as a flood")

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF,
                             4 * 1024 * 1024)
        except OSError:
            pass
        asked = probe.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    finally:
        probe.close()
    check(granted == asked,
          f"the arm socket asks for 4 MiB of receive buffer: it got what "
          f"this kernel gives any socket that asks for 4 MiB ({asked}): "
          f"{granted}")


def test_round5_arm_link_lines_have_a_global_ceiling():
    section("round 5, item 5: arm-link rejection lines are capped at 8 a "
            "minute across EVERY reason together, not only 4 a minute per "
            "reason (13 reasons at 4 a minute each filled the 1000-line "
            "journal queue behind a blocked console in about 19 minutes)")
    check(arminput.GLOBAL_LINES_PER_MINUTE == 8,
          f"the overall cap is 8 a minute: "
          f"{arminput.GLOBAL_LINES_PER_MINUTE}")
    reasons = (["decode:" + r for r in arminput._DECODE_REASONS]
               + ["decode:other", "another sender", "flood"])
    check(len(reasons) == 13, f"set up: 13 reasons: {len(reasons)}")

    # Every reason opens an episode inside one minute: 8 lines, not 13.
    lines = []
    j = arminput._RejectJournal(lambda k, m: lines.append(m))
    for i, r in enumerate(reasons):
        j.note(r, i * 0.1, ("127.0.0.1", 1000 + i), f"opening {r}.")
    check(len(lines) == 8,
          f"13 reasons opening inside one minute write 8 lines: "
          f"{len(lines)}")

    # The review's worst pattern: all 13 rotating, every 10 ms, for 10
    # minutes (fake clock), with episodes swept as the service would.
    lines = []
    j = arminput._RejectJournal(lambda k, m: lines.append(m))
    t = 0.0
    i = 0
    per_min = {}
    while t < 600.0:
        j.note(reasons[i % 13], t, ("127.0.0.1", 1000 + i % 50), "opening.")
        j.sweep(t, lambda reason, a, c: f"close {reason} {c}")
        t += 0.01
        i += 1
    # and a burst-then-quiet pattern that keeps reopening episodes
    t2 = 600.0
    for k in range(13 * 12):
        j.note(reasons[k % 13], t2, ("127.0.0.1", 1), "opening.")
        t2 += arminput.EPISODE_QUIET_S + 0.1
        j.sweep(t2, lambda reason, a, c: f"close {reason} {c}")
    total_minutes = t2 / 60.0
    check(len(lines) <= 8 * (int(total_minutes) + 1),
          f"{len(lines)} lines in {total_minutes:.0f} minutes of every "
          f"reason at once: never more than 8 a minute")
    stamps = []
    lines2 = []
    j = arminput._RejectJournal(lambda k, m: (lines2.append(m),
                                              stamps.append(now[0])))
    now = [0.0]
    while now[0] < 300.0:
        for k, r in enumerate(reasons):
            j.note(r, now[0], ("127.0.0.1", 1), "opening.")
        now[0] += arminput.EPISODE_QUIET_S + 0.1
        j.sweep(now[0], lambda reason, a, c: f"close {reason} {c}")
    worst = max(sum(1 for s in stamps if a <= s < a + 60.0) for a in stamps)
    check(worst <= 8,
          f"in any 60 s window, at most 8 lines: worst window had {worst}")
    check(any("went unlogged" in m for m in lines2),
          "and a later line says how many went unlogged")


def test_round5_arm_link_lines_never_crowd_out_important_ones():
    section("round 5, item 5: behind a blocked console, arm-link lines can "
            "never use more than half the journal queue, so 'show program "
            "stopped answering', arm and disarm lines always have room")
    from flamesafe import journal as jmod

    gate = threading.Event()
    written = []

    class _Blocked:
        def write(self, s):
            gate.wait(10.0)
            written.append(s)

        def flush(self):
            pass

    j = jmod.Journal(stream=_Blocked())
    try:
        for i in range(jmod.QUEUE_MAX * 2):
            j.event("arm-link", f"arm frame rejected {i}")
        rejected_dropped = j.dropped
        n = jmod.QUEUE_MAX // 4 - 5      # both kinds together fill most of
        for i in range(n):               # the half kept for them
            j.event("link", f"show program stopped answering {i}")
            j.event("arm-input", f"armed {i}")
    finally:
        gate.set()
    j.flush(10.0)
    text = "".join(written)
    important = sum(1 for i in range(n)
                    if f"stopped answering {i}\n" in text)
    arms = sum(1 for i in range(n) if f"armed {i}\n" in text)
    check(rejected_dropped >= jmod.QUEUE_MAX * 2 - jmod.QUEUE_MAX // 2 - 1,
          f"{jmod.QUEUE_MAX * 2} arm-link lines behind a blocked console: "
          f"no more than half the queue kept ({rejected_dropped} dropped)")
    check(important == n and arms == n,
          f"every 'stopped answering' and arm line queued after them was "
          f"still written: {important} and {arms} of {n}")
    check(j.dropped == rejected_dropped,
          f"and nothing but arm-link lines was dropped: "
          f"{j.dropped - rejected_dropped} other lines lost")


def test_round4_service_passes_the_flood_flag_before_assert_arm():
    section("round 4, item B: the service hands the arm input's `flooded` "
            "to the composer every tick, before assert_arm")

    class _Flooded(arminput.ArmInput):
        flooded = True
        foreign_count = 2

        def poll(self):
            return None          # the locked sender is quiet this tick

    svc = Service(make_config(), _Flooded())
    svc._poll_arm()
    check(svc.composer._arm_link_flooded is True,
          "the composer knows the link is flooded")
    check(svc.composer._foreign_arm_senders == 2,
          "and how many other senders are on it, even on a tick where "
          "poll() had no assertion to return")
    svc.arm_input.flooded = False
    svc.arm_input.foreign_count = 0
    svc._poll_arm()
    check(svc.composer._arm_link_flooded is False
          and svc.composer._foreign_arm_senders == 0, "and when not")


def test_round4_decode_rejections_are_throttled_per_reason():
    section("round 4, item C: wrong-key, wrong-shape and garbage datagrams "
            "are journaled once per reason per episode with a count, an "
            "episode survives a datagram every 520 ms, and no reason "
            "writes more than LINES_PER_MINUTE lines a minute (the round-4 "
            "review needed no key at all to write ~39,000 lines in 5 s)")
    t = [0.0]
    log = Log()
    inp = arminput.SocketArmInput("127.0.0.1", 0, KEY, 6, log=log,
                                  stale_ms=500, clock=lambda: t[0])
    inp.open()
    port = inp._sock.getsockname()[1]
    tx = _udp()

    def raw(obj):
        tx.sendto(json.dumps(obj).encode(), ("127.0.0.1", port))

    def lines():
        return [m for k, m in log.events if k == "arm-link"]

    try:
        for i in range(30):
            tx.sendto(link.encode_arm(i, [True] * 6, NAMES, KEY + "x"),
                      ("127.0.0.1", port))
            raw({"v": 2, "k": KEY, "t": "arm", "seq": i,
                 "wanted": [True] * 5, "names": NAMES})
            tx.sendto(b"\xff garbage", ("127.0.0.1", port))
            raw({"v": 1000 + i, "k": KEY, "t": "arm", "seq": i,
                 "wanted": [True] * 6, "names": NAMES})
            time.sleep(0.002)
            inp.poll()
            t[0] += 0.025
        opened = lines()
        check(len(opened) == 4,
              f"120 rejections for 4 reasons (one of them with a different "
              f"contract version in every datagram) write 4 lines: "
              f"{len(opened)} {opened[:6]}")
        t[0] += arminput.EPISODE_QUIET_S + 0.1
        inp.poll()
        closers = lines()[4:]
        check(len(closers) == 4 and all("30 rejected" in m for m in closers),
              f"and one closing line each, with the count: {closers}")

        # One datagram every 520 ms for 30 s: ONE episode, not 58.  A
        # minute on first (round 5): the 8 lines above already used this
        # minute's share of the global cap (GLOBAL_LINES_PER_MINUTE), and
        # each part here measures only its own rule.
        t[0] += 60.0
        before = len(lines())
        for i in range(58):
            tx.sendto(link.encode_arm(i, [True] * 6, NAMES, KEY + "x"),
                      ("127.0.0.1", port))
            time.sleep(0.002)
            inp.poll()
            t[0] += 0.52
        check(len(lines()) - before == 1,
              f"a datagram every 520 ms keeps one episode open: "
              f"{len(lines()) - before} lines")
        t[0] += arminput.EPISODE_QUIET_S + 0.1
        inp.poll()

        # One every EPISODE_QUIET_S + 0.1 s for ~150 s: each is its own
        # episode, but the per-minute cap holds the line count down.
        t[0] += 60.0
        before = len(lines())
        for i in range(30):
            tx.sendto(link.encode_arm(i, [True] * 6, NAMES, KEY + "x"),
                      ("127.0.0.1", port))
            time.sleep(0.002)
            inp.poll()
            t[0] += arminput.EPISODE_QUIET_S + 0.1
        n = len(lines()) - before
        # Literal numbers, not the module's constants: a test that reads
        # the constant moves along with it when someone changes it.
        check(arminput.LINES_PER_MINUTE == 4
              and arminput.EPISODE_QUIET_S == 5.0,
              f"the throttle is 4 lines a minute per reason and a 5 s quiet "
              f"window: {arminput.LINES_PER_MINUTE}, "
              f"{arminput.EPISODE_QUIET_S}")
        check(n <= 12,
              f"30 widely spaced rejections over ~150 s write at most 4 "
              f"lines a minute (12 in 3 minutes), not 30: {n}")
        check(any("went unlogged" in m for m in lines()[before:]),
              "and a line after the cap says how many went unlogged")
    finally:
        inp.close()
        tx.close()


def test_round4_flood_takeover_cannot_rearm_end_to_end():
    section("round 4, item B, the proven attack against the real Service/"
            "SocketArmInput/Composer: a flood crowds the real deck out, "
            "the rogue becomes the locked sender, then forges a "
            "low-then-high on a group the deck still wants. Nothing arms: "
            "the real deck, still sending, is now the foreign sender")
    node = _udp()
    ltc_status = _udp()
    listen_port = _udp()
    lp = listen_port.getsockname()[1]
    listen_port.close()
    arm_port_sock = _udp()
    ap = arm_port_sock.getsockname()[1]
    arm_port_sock.close()
    cfg = make_config(destination={"ip": "127.0.0.1",
                                   "port": node.getsockname()[1]},
                      link={"listen_ip": "127.0.0.1", "listen_port": lp,
                            "status_ip": "127.0.0.1",
                            "status_port": ltc_status.getsockname()[1],
                            "arm_port": ap, "key": KEY})
    t = [0.0]
    log = Log()
    arm_input = arminput.SocketArmInput(cfg.link_arm_ip, cfg.link_arm_port,
                                        cfg.link_key, cfg.n, log=log,
                                        stale_ms=cfg.arm_stale_ms,
                                        clock=lambda: t[0])
    svc = Service(cfg, arm_input, clock=lambda: t[0], log=log)
    svc.open()
    deck = _udp()
    rogue = _udp()
    ltc_tx = _udp()
    fseq = [0]

    def tick():
        fseq[0] += 1
        ltc_tx.sendto(link.encode_flame(fseq[0], "00:00:00:01", t[0], 1,
                                        [0] * 512, KEY), ("127.0.0.1", lp))
        time.sleep(0.004)
        t[0] += cfg.tick_period_s
        return svc.run_once()

    def send(sock, seq, wanted):
        sock.sendto(link.encode_arm(seq, wanted, NAMES, KEY),
                    ("127.0.0.1", ap))

    want0 = [True] + [False] * (cfg.n - 1)
    dseq, rseq = [0], [10 ** 6]

    def deck_send(w):
        dseq[0] += 1
        send(deck, dseq[0], w)

    def rogue_send(w):
        rseq[0] += 1
        send(rogue, rseq[0], w)

    dwell_ticks = int(cfg.min_arm_dwell_ms / 1000.0 / cfg.tick_period_s) + 5
    try:
        for _ in range(3):
            deck_send([False] * cfg.n); tick()
        for _ in range(dwell_ticks):
            deck_send(want0); tick()
        check(link.decode_status(_drain(ltc_status)[-1], KEY)["groups"][0]
              ["armed"] == "armed", "setup: the deck genuinely armed group 0")
        # The flood, then the deck crowded out (silent) past arm_stale_ms
        # while the rogue keeps sending: the lock lapses and the rogue
        # takes it.
        for _ in range(arminput.FLOOD_DATAGRAMS_PER_POLL * 3):
            send(rogue, rseq[0], [True] * cfg.n)
        time.sleep(0.02)
        out = tick()
        check(out.status["arm_input"]["flooded"] is True,
              f"the status frame says the link is flooded: "
              f"{out.status['arm_input']}")
        for _ in range(int(cfg.arm_stale_ms / 1000.0 / cfg.tick_period_s)
                       + 4):
            rogue_send([True] * cfg.n); tick()
        check(arm_input._sender == rogue.getsockname(),
              "the rogue now holds the sender lock")
        # Now the real deck is back to sending (20 Hz, still wanting
        # group 0) and is the FOREIGN one. The rogue forges the cycle.
        for _ in range(dwell_ticks):
            deck_send(want0); rogue_send([True] * cfg.n); tick()
        for _ in range(6):
            deck_send(want0); rogue_send([False] * cfg.n); tick()
        for _ in range(dwell_ticks * 2):
            deck_send(want0); rogue_send([True] * cfg.n); out = tick()
        g = out.status["groups"][0]
        check(g["armed"] != "armed",
              f"the rogue's forged low-then-high does NOT re-arm group 0: "
              f"{g}")
        check(out.status["arm_input"]["foreign_senders"] >= 1
              and g["reason"] == composer.OTHER_SENDER,
              f"and the status says why (the real deck is the other "
              f"sender): {out.status['arm_input']} {g['reason']}")
        check(all(out.universe[gr.safety - 1] == 0 for gr in cfg.groups),
              "nothing is armed on the wire")
    finally:
        svc.close()
        for s_ in (deck, rogue, ltc_tx, node, ltc_status):
            s_.close()


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
    r.step(n=4)                      # the quiet window, inside the fire hold
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
    r.run(5, {411: 200})
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
        r = Rig(test_only_dwell_ms=dwell)
        r.link_alive = False              # this loop sends its own frames
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
                     and (r.t - last_frame_at) * 1000 <= cfg.fire_hold_ms)
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
            link_age = (None if last_frame_at is None
                        else (r.t - last_frame_at) * 1000)
            if link_age is None or link_age > cfg.frame_stale_ms + 0.001:
                ok &= check(all(u[g.safety - 1] == 0 for g in cfg.groups),
                            f"case {case} step {step}: armed with the show "
                            f"program silent for {link_age} ms")
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
                            "status_port": ltc_status.getsockname()[1],
                            "key": KEY})
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

        def send(seq, slots, universe=1, mono=None, key=KEY, sock=None):
            vals = [0] * 512
            for s, v in slots.items():
                vals[s - 1] = v
            (sock or ltc_tx).sendto(
                link.encode_flame(seq, "00:00:00:01",
                                  t[0] if mono is None else mono,
                                  universe, vals, key),
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
        s = link.decode_status(st[-1], KEY)
        check(s["k"] == KEY and s["v"] == 2,
              "the status frame on the wire carries the key, version 2")
        check(s["groups"][0]["armed"] == "disarmed" and s["heartbeat"] >= 1
              and s["priority"] == 200 and s["frames"]["seq"] == 1
              and s["frames"]["timecode"] == "00:00:00:01",
              f"the status frame is right: {s['groups'][0]}, "
              f"{s['frames']}")
        check("sacn" in s and s["sacn"]["sent"] >= 1,
              "the status frame counts sent packets")
        # A frame with another key, and a frame from another socket while
        # the link is live, are both rejected and change nothing.
        rogue = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        send(50, {411: 255}, key="not-the-key-not-the-key")
        send(60, {411: 255}, sock=rogue)
        tick()
        s = link.decode_status(_drain(ltc_status)[-1], KEY)
        check(s["frames"]["rejected"] == 2 and s["frames"]["seq"] == 1
              and s["frames"]["last_reject"] == "another sender",
              f"wrong key and another sender rejected: {s['frames']}")
        rogue.close()
        # Second-copy guard (2026-10-03): the rogue counts as on the link
        # for frame_stale_ms after its datagram, and no cycle counts while
        # it does.  Let it go first, with ltcplay still sending.
        for k in range(2, 2 + int(0.6 / cfg.tick_period_s)):
            send(k, {})
            tick()
        # arm group 0 with consent, clean edge: the counter must be seen
        # advancing, then a down edge on a live counter, then the request
        tick()
        send(102, {})
        tick()
        tick()
        inp.set(0)
        send(103, {})
        tick()
        p = _drain(node)[-1]
        check(p[126 + 400] == 78, "the safety slot carries 78 on the wire")
        for k in range(104, 108):
            send(k, {411: 200})
            tick()
        p = _drain(node)[-1]
        check(p[126 + 410] == 200 and p[126 + 400] == 78,
              "after the quiet window the fire value passes")
        check(p[126 + 0] == 0, "and channel 1, no group's, is zero")
        s = link.decode_status(_drain(ltc_status)[-1], KEY)
        check(s["groups"][0]["armed"] == "armed"
              and s["groups"][0]["sent_fire"][0] == 200,
              "the status frame says armed with SENT 200")
        # rubbish and wrong-universe frames are rejected and change nothing
        ltc_tx.sendto(b"\x00\xff garbage", ("127.0.0.1", lp))
        ltc_tx.sendto(b'{"v":2,"t":"flame"}', ("127.0.0.1", lp))
        send(108, {411: 0}, universe=2)
        time.sleep(0.02)
        tick()
        p = _drain(node)[-1]
        s = link.decode_status(_drain(ltc_status)[-1], KEY)
        check(p[126 + 410] == 200, "the good frame still stands")
        check(s["frames"]["rejected"] == 5 and s["frames"]["last_reject"],
              f"five rejections reported so far: {s['frames']}")
        # ltcplay stops: the fire slot zeros inside fire_hold_ms, and after
        # frame_stale_ms the link is lost and the group is disarmed
        for _ in range(int(0.1 / cfg.tick_period_s) + 1):
            tick()
        p = _drain(node)[-1]
        check(p[126 + 410] == 0 and p[126 + 400] == 78,
              "ltcplay quiet for the fire hold: fire zero, arm still up")
        for _ in range(int(0.4 / cfg.tick_period_s) + 2):
            tick()
        p = _drain(node)[-1]
        check(p[126 + 400] == 0, "ltcplay quiet past frame_stale_ms: disarmed")
        s = link.decode_status(_drain(ltc_status)[-1], KEY)
        check(s["groups"][0]["armed"] == "held"
              and s["groups"][0]["reason"] == composer.LINK_LOST
              and s["groups"][0]["amber"] == "steady",
              f"and the lamp says the show program stopped answering: "
              f"{s['groups'][0]}")
        # the arm input goes: everything zeros
        inp.silent = True
        for _ in range(int(0.5 / cfg.tick_period_s) + 2):
            tick()
        p = _drain(node)[-1]
        check(p[126:] == bytes(512), "arm input gone: all zeros")
        check(p[112] == 0, "still not terminated")
        # back, cycled, armed and firing again, so that the shutdown below
        # has something to zero: ltcplay first (the link must be live before
        # a cycle counts), then the cycle
        inp.silent = False
        for k in range(109, 114):
            send(k, {411: 0})
            tick()
        inp.set(0, on=False)
        send(114, {411: 0})
        tick()
        send(115, {411: 0})
        tick()
        inp.set(0)
        for k in range(116, 160):
            send(k, {411: 0})
            tick()
        for k in range(160, 166):
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
                            "status_port": ltc_status.getsockname()[1],
                            "key": KEY})
    # The pacing is proved on an injected clock and an injected sleep, so no
    # runner's wall clock is trusted: a starved macOS runner once managed 7
    # ticks in 0.59 s and failed a real-time version of this check.
    t = [100.0]
    sleeps = []
    stop = threading.Event()

    def fake_sleep(d):
        sleeps.append(d)
        t[0] += d                     # time passes exactly as asked
        if len(sleeps) >= 40:
            stop.set()

    svc = Service(cfg, arminput.NullArmInput(), clock=lambda: t[0],
                  sleep=fake_sleep)
    svc.open()
    svc.run_forever(stop)
    n = svc.composer.heartbeat
    check(n == 40, f"40 ticks, one before each of the 40 sleeps: {n}")
    check(all(0 < d <= 0.025 + 1e-9 for d in sleeps),
          f"every sleep is at most one period: {sorted(sleeps)[-1]:.4f}")
    check(abs((t[0] - 100.0) - 40 * 0.025) < 1e-6,
          f"40 periods of 25 ms took exactly 1.000 s of clock: {t[0] - 100:.4f}")
    check(svc.composer.stats["overruns"] == 0, "no overrun on a kept clock")
    # A tick that runs long is not caught up in a burst.
    t[0] += 0.4                       # the next tick sees a 400 ms gap
    sleeps.clear()
    stop.clear()
    svc.run_forever(stop)
    check(svc.composer.stats["overruns"] == 1,
          "the late tick was counted as an overrun")
    check(all(0 < d <= 0.025 + 1e-9 for d in sleeps),
          "and the loop did not burst to catch up")
    pk = _drain(node)
    check(len(pk) >= 80 and all(p[126:] == bytes(512) for p in pk),
          f"{len(pk)} packets, every one all zeros with no arm input")
    check(all(p[108] == 200 for p in pk), "every packet at priority 200")
    svc.close()
    # And the real loop, on the real clock, only has to start and stop; it
    # makes no claim about the rate this machine can manage right now.
    svc2 = Service(cfg, arminput.NullArmInput())
    svc2.open()
    stop2 = threading.Event()
    th = threading.Thread(target=svc2.run_forever, args=(stop2,), daemon=True)
    th.start()
    time.sleep(0.2)
    stop2.set()
    th.join(5.0)
    check(not th.is_alive(), "the real loop stopped when asked")
    check(svc2.composer.heartbeat >= 1, "and ticked at least once")
    svc2.close()
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
    r.run(5, {411: 200})
    s = r.out.status
    for key in ("v", "t", "heartbeat", "tick_ms", "universe", "priority",
                "arm_value", "confirmed", "fault", "fault_age_ms",
                "arm_input", "frames", "stats", "groups"):
        check(key in s, f"top-level {key}")
    check(s["v"] == link.CONTRACT_VERSION == 2 and s["t"] == "status",
          "version 2, type status")
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
    wire = json.loads(link.encode_status(s, KEY).decode())
    check(wire.pop("k") == KEY and wire == s,
          "the status frame survives the wire, with the key added")
    h1 = s["heartbeat"]
    r.step()
    check(r.out.status["heartbeat"] == h1 + 1, "the heartbeat counts ticks")
    doc = open(os.path.join(HERE, "CONTRACT.md"), encoding="utf-8").read()
    for word in ("Contract version 2", '"flame"', '"status"', "priority 200",
                 "fire_hold_ms", "another sender", '"k"', "sacn.errors",
                 "hard kill", "Max. Flame Duration", "HTP", "positional",
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
    # A bounded wait: if the config were accepted the service would run
    # for ever, and that must read as a failure in seconds, not a hang.
    proc = subprocess.Popen([sys.executable, "-m", "flamesafe", path],
                            cwd=ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        out, _err = proc.communicate(timeout=15)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _err = proc.communicate()
        rc = "still running after 15 s"
    finally:
        os.unlink(path)
    check(rc == 2, f"exit 2: {rc}")
    check("flamesafe will not start" in out and "share fire slot 411" in out,
          f"the sentence: {out.strip()[:200]}")
    r = subprocess.run([sys.executable, "-m", "flamesafe"], cwd=ROOT,
                       capture_output=True, text=True, timeout=60)
    check(r.returncode == 2 and "Usage" in r.stdout, "no argument: usage")


def test_review_sender_lock():
    section("review: while the link is live only the first sender's frames "
            "are taken, so one rogue datagram cannot fire a head or lock "
            "ltcplay out")
    r = armed_rig()
    other = ("127.0.0.1", 40002)
    check(r.frame({411: 0}) == "", "the real sender's frame is accepted")
    why = r.frame({411: 255}, seq=10 ** 9, sender=other)
    check(why == "another sender",
          f"a rogue frame with a huge seq is rejected: {why!r}")
    r.step(n=2)
    check(r.fire(0)[0] == 0, "and nothing of it reached the wire")
    check(r.frame({411: 0}) == "",
          "the real sender is not locked out by the rogue's seq")
    check(r.out.status["frames"]["seq"] < 10 ** 9,
          "the rogue seq never became the link's seq")
    # once stale, the lock is released and a new sender takes it
    r.link_alive = False
    r.wait(0.6)
    check(r.frame({411: 0}, seq=0, sender=other) == "",
          "after the link went stale another sender is accepted")
    check(r.frame({411: 0}, seq=1) != "",
          "and the old sender is now the other one")


def test_review_send_failures_are_faults():
    section("review: a failed sACN send or status send is a fault in the "
            "status frame, never armed and fine")
    node = _udp()
    ltc_status = _udp()
    listen = _udp()
    lp = listen.getsockname()[1]
    listen.close()
    cfg = make_config(destination={"ip": "127.0.0.1",
                                   "port": node.getsockname()[1]},
                      link={"listen_ip": "127.0.0.1", "listen_port": lp,
                            "status_ip": "127.0.0.1",
                            "status_port": ltc_status.getsockname()[1],
                            "key": KEY})
    t = [0.0]
    inp = arminput.ScriptedArmInput(cfg.n, names=NAMES)
    log = Log()
    svc = Service(cfg, inp, clock=lambda: t[0], log=log)
    svc.open()
    try:
        def tick():
            t[0] += cfg.tick_period_s
            return svc.run_once()

        ltc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        seq = [0]

        def frame():
            seq[0] += 1
            ltc.sendto(link.encode_flame(seq[0], None, t[0], 1, [0] * 512,
                                         KEY), ("127.0.0.1", lp))
            time.sleep(0.005)

        for _ in range(3):
            frame()
            tick()
        inp.set(0)
        frame()
        tick()
        check(svc.last_output.universe[400] == 78, "armed")
        check(svc.last_output.status["fault"] == "", "no fault yet")

        class Broken:
            def sendto(self, *a):
                raise OSError(65, "No route to host")

            def close(self):
                pass

        real_tx = svc._tx
        svc._tx = Broken()
        frame()
        tick()
        frame()
        out = tick()
        check(svc.send_errors == 2, f"two failed sends: {svc.send_errors}")
        check("sACN send failed" in out.status["fault"]
              and out.status["fault_age_ms"] is not None,
              f"the fault names the failed send: {out.status['fault']!r}")
        check(out.status["groups"][0]["armed"] == "armed",
              "the group is still armed (the fault is the red, not a disarm)")
        check(("fault", ) == tuple(k for k, _ in log.events if k == "fault")
              or sum(1 for k, _ in log.events if k == "fault") >= 1,
              "the fault is journaled")
        s = link.decode_status(_drain(ltc_status)[-1], KEY)
        check(s["sacn"]["errors"] == 2 and "sACN send failed" in s["fault"],
              f"and it is on the wire to ltcplay: {s['sacn']}, {s['fault']!r}")
        svc._tx = real_tx
        real_status = svc._status_tx
        svc._status_tx = Broken()
        frame()
        out = tick()
        check(svc.status_errors == 1 and "status frame not sent"
              in out.status["fault"] or "status frame not sent"
              in svc.composer._fault,
              f"a failed status send is a fault too: {svc.composer._fault!r}")
        svc._status_tx = real_status
        ltc.close()
    finally:
        svc.close()
    for s_ in (node, ltc_status):
        s_.close()


def test_review_journal_never_blocks_the_tick():
    section("review: a console that blocks writers cannot stall the tick "
            "loop (Windows QuickEdit)")
    from flamesafe.journal import Journal

    class Sticky:
        """A stream whose every write takes 50 ms.  Inline, 20 writes
        would take a second; through the queue they take microseconds."""
        def __init__(self):
            self.writes = 0

        def write(self, s):
            self.writes += 1
            time.sleep(0.05)

        def flush(self):
            pass

    stream = Sticky()
    j = Journal(stream=stream)
    t0 = time.perf_counter()
    for i in range(20):
        j.event("test", f"line {i}")
    el = time.perf_counter() - t0
    check(el < 0.5, f"20 events queued in {el * 1000:.0f} ms while every "
                    f"write blocks for 50 ms")
    check(len(j.lines) == 20, "the in-memory copy has every line")
    # And through the composer: events during ticks do not slow the ticks.
    r = Rig()
    r.c._log = j
    t0 = time.perf_counter()
    r.prove_alive()
    r.inp.set(0)
    for _ in range(40):
        r.frame({411: 255})
        r.step()                     # edge-block event on every tick
    el = time.perf_counter() - t0
    check(el < 1.0, f"40 ticks with a journal event each took "
                    f"{el * 1000:.0f} ms against a blocking console")
    check(r.c.stats["edge_blocks"] == 40, "every tick was composed")
    time.sleep(0.6)
    check(stream.writes >= 1, "the writer thread is draining the queue")


def test_review_panic_status_is_honest():
    section("review: a panic status reports the real input states and a "
            "compose fault carries its age")
    r = armed_rig()
    r.frame({411: 200})
    r.step(n=5)
    o = r.step(dt=0.3)               # an overrun, with live input, fresh frame
    check(o.status["arm_input"]["state"] == "live"
          and o.status["frames"]["state"] == "fresh",
          f"the overrun status says the input is live and the frame fresh: "
          f"{o.status['arm_input']}, {o.status['frames']}")
    check(o.status["fault_age_ms"] == 0, "the overrun fault is 0 ms old")
    r = armed_rig()
    r.c._fire_is_quiet = lambda *a: 1 / 0
    r.inp.set(1)
    o = r.step()
    check(isinstance(o.status["fault_age_ms"], int),
          f"a compose fault has an age: {o.status['fault_age_ms']!r}")
    r.step(n=4)
    check(r.out.status["fault_age_ms"] >= 90,
          f"and it grows: {r.out.status['fault_age_ms']}")


def test_review2_frozen_counter_then_synthetic_down():
    section("review 2: a counter frozen past arm_stale_ms with reports "
            "flowing, then all-down, then arm: no consent")
    # The second review's verified re-arm: freeze 550 ms, thaw, a synthetic
    # all-down, then the persisted arm state; 1.25 s later the slot read 78.
    r = armed_rig()
    r.inp.freeze()
    r.wait(0.55)
    check(r.safety(0) == 0, "frozen past the window: disarmed")
    r.inp.thaw()
    r.inp.set_all(False)
    r.step()
    r.inp.set(0)
    r.step()
    r.wait(1.25)
    check(r.safety(0) == 0 and r.group(0)["reason"] == "cycle the arm",
          f"the first advancing assertion after a frozen spell is not "
          f"consent: {r.safety(0)}, {r.group(0)['reason']!r}")
    # A real cycle, with the counter live before the down edge, arms it.
    r.inp.set(0, on=False)
    r.step()
    r.inp.set(0)
    r.wait(1.25)
    check(r.safety(0) == ARM, "a real cycle afterwards arms it")
    # And the same through every interruption OF THE INPUT, in one place:
    # for each, the first assertion back carries all-down, the second asks
    # for arm, and nothing may arm without a further cycle.  (An overrun is
    # an interruption of this program, not of the input: the input stayed
    # live, so a down edge from it after the overrun IS a cycle; that case
    # is in test_rule7.)
    for how in ("silent", "frozen", "reboot"):
        r = armed_rig()
        if how == "silent":
            r.inp.silent = True
            r.wait(0.55)
            r.inp.silent = False
            r.inp.seq += 5000
        elif how == "frozen":
            r.inp.freeze()
            r.wait(0.55)
            r.inp.thaw()
        else:
            r.inp.reboot()
        r.inp.set_all(False)
        r.step()
        r.inp.set(0)
        r.step()
        r.wait(1.25)
        check(r.safety(0) == 0,
              f"{how}: all-down then arm on the first assertions back does "
              f"not arm")


def test_review2_a_fault_clears_after_five_clean_seconds():
    section("review 2: a fault is red for 5 s of clean ticks and sends, "
            "then clears; the counts stay")
    r = armed_rig()
    r.c.note_fault("sACN send failed (1 so far): test")
    r.step()
    check(r.out.status["fault"].startswith("sACN send failed")
          and r.c.stats["faults_noted"] == 1, "the fault is up")
    r.wait(4.9)
    check(r.out.status["fault"] != "", "still red at 4.9 s")
    r.wait(0.2)
    check(r.out.status["fault"] == "" and r.out.status["fault_age_ms"] is None,
          f"clear after 5 s: {r.out.status['fault']!r}")
    check(r.c.stats["faults_cleared"] == 1
          and r.out.status["stats"]["faults_noted"] == 1,
          "the counts stay in the stats")
    check("fault-cleared" in r.log.kinds() and "fault" in r.log.kinds(),
          "the journal has the fault and its clearing")
    check(r.safety(0) == ARM, "the group stayed armed throughout")
    # A fault that keeps being refreshed never clears.
    r.c.note_fault("sACN send failed (2 so far): test")
    for _ in range(int(6 / r.period)):
        r.step()
        if r.c.heartbeat % 40 == 0:
            r.c.note_fault("sACN send failed (n so far): test")
    check(r.out.status["fault"] != "", "a fault refreshed every second stays")
    # An overrun's fault clears the same way.
    r = armed_rig()
    r.step(dt=0.3)
    check(r.out.status["fault"].startswith("safety program overran"), "overran")
    r.wait(5.1)
    check(r.out.status["fault"] == "", "the overrun fault cleared after 5 s")


def test_review2_journal_drops_are_counted_and_written_up():
    section("review 2: dropped journal lines are counted in the status, "
            "and written up once the console drains")
    from flamesafe.journal import Journal, QUEUE_MAX

    class Gate:
        """A stream that blocks every write until released.  The wait is
        short and bounded so that a journal writing INLINE (the mutation)
        fails this test in seconds instead of hanging the suite."""
        def __init__(self):
            self.open = threading.Event()
            self.writes = []

        def write(self, s):
            # Block only the journal's own writer thread.  A journal that
            # writes INLINE (the mutation) calls this from the test thread,
            # is not blocked, drops nothing, and fails the drop check at
            # once instead of hanging the suite.
            if threading.current_thread().name == "flamesafe-journal":
                self.open.wait(5.0)
            self.writes.append(s)

        def flush(self):
            pass

    stream = Gate()
    j = Journal(stream=stream)
    for i in range(QUEUE_MAX + 250):
        j.event("test", f"line {i}")
    check(j.dropped >= 200, f"lines beyond the queue are dropped and "
                            f"counted: {j.dropped}")
    r = Rig()
    r.c._log = j
    r.step()
    check(r.out.status["stats"]["journal_dropped"] == j.dropped,
          f"the status frame carries the drop count: "
          f"{r.out.status['stats']['journal_dropped']}")
    stream.open.set()
    j.flush(timeout=10.0)
    time.sleep(0.2)
    joined = "".join(stream.writes)
    check(f"{j.dropped} journal lines were dropped while the console was "
          f"blocked" in joined,
          "after draining, the journal says how many lines were lost")
    check(joined.count("were dropped") == 1, "and says it once")
    src = open(os.path.join(HERE, "__main__.py"), encoding="utf-8").read()
    check("journal.flush()" in src, "__main__ flushes the journal on stop")


def test_review2_udp_connreset_is_really_switched_off():
    section("review 2: SIO_UDP_CONNRESET is switched off through WSAIoctl "
            "on Windows, and a no-op elsewhere")
    from flamesafe.service import _no_connreset, SIO_UDP_CONNRESET
    check(SIO_UDP_CONNRESET == 0x9800000C, "the winsock control code")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("127.0.0.1", 0))
        rc = _no_connreset(s)
        if sys.platform == "win32":
            check(rc is True, f"WSAIoctl(SIO_UDP_CONNRESET, FALSE) returned "
                              f"success on Windows: {rc!r}")
            # And it took: a send to a closed port, then a recv, must not
            # raise ConnectionResetError any more.
            closed = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            closed.bind(("127.0.0.1", 0))
            port = closed.getsockname()[1]
            closed.close()
            s.settimeout(0.2)
            for _ in range(3):
                s.sendto(b"x", ("127.0.0.1", port))
            time.sleep(0.1)
            try:
                s.recvfrom(64)
                check(True, "")
            except (socket.timeout, TimeoutError):
                check(True, "")
            except ConnectionResetError:
                check(False, "ConnectionResetError still raised after "
                             "SIO_UDP_CONNRESET off")
        else:
            check(rc is None, f"not Windows: no-op, {rc!r}")
    finally:
        s.close()


def test_review2_keys():
    section("review 2: the example key is refused once confirmed, and a "
            "key of your own travels in both directions")
    d = example_dict()
    d["confirmed"] = True
    try:
        config.from_dict(d)
        check(False, "a confirmed config with the example key was accepted")
    except config.ConfigError as e:
        check("example key" in str(e), f"refused: {e}")
    own = "tp-2026-north-field-7f3a9c"
    d["link"]["key"] = own
    check(config.from_dict(d).link_key == own,
          "a confirmed config with its own key loads")
    # unknown keys are refused, at every level
    for where, mutate in (("top", lambda d: d.__setitem__("fire_hold", 100)),
                          ("link", lambda d: d["link"].__setitem__("listen", 1)),
                          ("destination",
                           lambda d: d["destination"].__setitem__("host", "x")),
                          ("group",
                           lambda d: d["groups"][0].__setitem__("fires", []))):
        d = example_dict()
        mutate(d)
        try:
            config.from_dict(d)
            check(False, f"an unknown {where} key was accepted")
        except config.ConfigError as e:
            check("does not know" in str(e), f"unknown {where} key: {e}")
    # a second, non-example key on the wire in both directions
    node = _udp()
    ltc_status = _udp()
    listen = _udp()
    lp = listen.getsockname()[1]
    listen.close()
    cfg = make_config(destination={"ip": "127.0.0.1",
                                   "port": node.getsockname()[1]},
                      link={"listen_ip": "127.0.0.1", "listen_port": lp,
                            "status_ip": "127.0.0.1",
                            "status_port": ltc_status.getsockname()[1],
                            "key": own})
    t = [0.0]
    svc = Service(cfg, arminput.NullArmInput(), clock=lambda: t[0])
    svc.open()
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        tx.sendto(link.encode_flame(1, None, 0.0, 1, [0] * 512, KEY),
                  ("127.0.0.1", lp))
        tx.sendto(link.encode_flame(2, None, 0.0, 1, [0] * 512, own),
                  ("127.0.0.1", lp))
        time.sleep(0.02)
        t[0] += cfg.tick_period_s
        svc.run_once()
        st = _drain(ltc_status)[-1]
        s = link.decode_status(st, own)
        check(s["k"] == own, "the status frame carries the config's key")
        try:
            link.decode_status(st, KEY)
            check(False, "the status frame carried the example key")
        except link.LinkError:
            check(True, "")
        check(s["frames"]["rejected"] == 1 and s["frames"]["accepted"] == 1
              and s["frames"]["last_reject"] == "wrong key",
              f"the example-key frame was rejected, the own-key frame "
              f"accepted: {s['frames']}")
    finally:
        svc.close()
        for s_ in (node, ltc_status, tx):
            s_.close()


def test_link_loss_disarms_every_group():
    section("Jeff, 2026-09-26: losing the show program disarms every group, "
            "nothing re-arms when it is back, and a cycle then works")
    # 1. Link loss disarms: latches cleared, arm value off, sentence on the
    # lamp and in the journal, inside frame_stale_ms plus one tick.
    r = armed_rig()
    r.inp.set(1)
    r.run(45, {411: 255})
    check(r.safety(0) == ARM and r.safety(1) == ARM and r.fire(0)[0] == 255,
          "two groups armed, one firing")
    resets_before = r.c.stats["latch_resets"]
    r.link_alive = False
    t_lost = r.t
    gone_at = None
    while r.t - t_lost < 2.0:
        r.step()
        if r.safety(0) == 0 and r.safety(1) == 0:
            gone_at = r.t - t_lost
            break
    check(gone_at is not None and gone_at <= 0.5 + 0.025 + 1e-9,
          f"every group disarmed {gone_at} s after the last frame")
    check(r.out.universe == bytes(512), "the whole universe is zero")
    check(r.c.stats["latch_resets"] == resets_before + 1
          and r.c.stats["link_lost"] == 1,
          "the latches were cleared once and the loss counted")
    check(("link", ) == tuple(k for k, _ in r.log.events if k == "link")
          and any("stopped answering" in m for k, m in r.log.events
                  if k == "link"),
          "the journal has one sentence about the show program")
    g = r.group(0)
    check(g["armed"] == "held" and g["reason"] == composer.LINK_LOST
          and g["amber"] == "steady" and g["reason"].startswith(
              "Show program stopped answering: disarmed."),
          f"the lamp reads steady amber with the sentence: {g}")
    # 2. Recovery does not re-arm: frames come back, the request is still
    # up, and nothing arms, for as long as you like.
    r.wait(1.0)
    r.link_alive = True
    r.frame({}, seq=0)                # a restarted ltcplay
    r.wait(2.0)
    check(r.safety(0) == 0 and r.safety(1) == 0,
          "the show program is back and nothing re-armed by itself")
    check(r.group(0)["reason"] == "cycle the arm"
          and r.group(0)["amber"] == "flashing",
          f"the lamp now asks for a cycle: {r.group(0)}")
    check(r.out.status["frames"]["state"] == "fresh",
          "and the status says the link is fresh again")
    # 3. The cycle after recovery works, then fire passes again.
    r.inp.set(0, on=False)
    r.step()
    r.inp.set(0)
    r.wait(1.25)
    check(r.safety(0) == ARM and r.safety(1) == 0,
          "a cycle on group 0 arms it, and group 1 (not cycled) stays down")
    r.run(6, {411: 200})
    check(r.fire(0)[0] == 200, "and it fires again")
    # A cycle made while the link is still down does not count: the down
    # edge is forgotten on every stale tick, so the operator cycles again.
    r.link_alive = False
    r.wait(0.6)
    check(r.safety(0) == 0, "lost again")
    r.inp.set(0, on=False)
    r.step()
    r.inp.set(0)
    r.step()
    r.link_alive = True
    r.frame({}, seq=0)
    r.wait(1.5)
    check(r.safety(0) == 0 and r.group(0)["reason"] == "cycle the arm",
          "a cycle made while the show program was down does not count")
    # 4. Startup with no link stays disarmed, whatever the input asks.
    r = Rig()
    r.link_alive = False
    r.prove_alive()
    r.inp.set_all(True)
    r.wait(2.0)
    check(r.out.universe == bytes(512), "no show program yet: all zeros")
    g = r.group(0)
    check(g["armed"] == "held" and g["reason"] == composer.LINK_NEVER
          and g["amber"] == "steady", f"the lamp says it has not answered: "
                                       f"{g}")
    r.inp.set_all(False)
    r.step()
    r.inp.set_all(True)
    r.wait(1.25)
    check(r.out.universe == bytes(512),
          "a cycle before the show program answers changes nothing")
    r.link_alive = True
    r.wait(0.5)
    check(r.out.universe == bytes(512)
          and r.group(0)["reason"] == "cycle the arm",
          "once it answers, still zeros, and the lamp asks for a cycle")
    r.inp.set_all(False)
    r.step()
    r.inp.set_all(True)
    r.wait(1.25)
    check(all(r.safety(i) == ARM for i in range(N_GROUPS)),
          "a cycle with the show program answering arms every group asked")


def test_review3_link_loss_is_one_line_and_the_return_is_one_line():
    section("review 3: a lost show program is one journal line and one count "
            "however long the outage, and its return is one line with the "
            "length")
    # The deck keeps re-reporting its OFF keys while the link is down, so
    # the per-tick reset always finds a down edge to forget.  Clearing on
    # every stale tick is right (a cycle made while the link is down must
    # not count); journaling and counting it on every tick, 40 lines a
    # second for the whole outage, is not.
    def resets(r):
        return [m for k, m in r.log.events if k == "latch-reset"]

    def links(r):
        return [m for k, m in r.log.events if k == "link"]

    r = armed_rig()
    r.run(20, {411: 0})
    n_resets, n_links = len(resets(r)), len(links(r))
    counted = r.c.stats["latch_resets"]
    r.link_alive = False
    t0 = r.t
    t_lost = None
    while r.t - t0 < 2.0:
        r.step()
        if t_lost is None and r.safety(0) == 0:
            t_lost = r.t
    check(t_lost is not None and r.safety(0) == 0, "lost, and still lost")
    new = resets(r)[n_resets:]
    check(len(new) == 1 and "show program link lost" in new[0],
          f"exactly one latch-reset line over a 2 s outage, not one per "
          f"tick: {len(new)}")
    check(r.c.stats["latch_resets"] == counted + 1,
          f"and latch_resets moved by one: {r.c.stats['latch_resets']}")
    check(len(links(r)) == n_links + 1, "one link line while it is down")
    check(not any(r.c._seen_down) and not any(r.c._latched),
          "the down edges the deck keeps reporting are still forgotten on "
          "every stale tick")
    # The return: one line, with the outage length, and the groups still
    # need a cycle.
    r.link_alive = True
    r.frame({}, seq=0)
    r.step()
    back = links(r)[n_links + 1:]
    check(len(back) == 1 and "answering again" in back[0],
          f"one link line on the return: {back}")
    m = re.search(r"after ([0-9.]+) s", back[0] if back else "")
    check(m is not None and abs(float(m.group(1)) - (r.t - t_lost)) <= 0.06,
          f"and it carries the outage length: {back}")
    check(r.safety(0) == 0 and r.group(0)["reason"] == "cycle the arm",
          "still disarmed after the return")
    r.wait(2.0)
    check(len(links(r)) == n_links + 2 and len(resets(r)) == n_resets + 1,
          "and nothing more is journaled while it stays back")
    # Startup, before the first frame ever: the deck's OFF keys are
    # forgotten every tick just the same, but there is no outage to
    # journal or count, and the first frame is not a return.
    r = Rig()
    r.link_alive = False
    r.prove_alive()
    r.inp.set_all(True)
    r.wait(1.0)
    check(not resets(r) and r.c.stats["latch_resets"] == 0
          and not links(r),
          "startup before the first frame: no latch-reset line, count 0, "
          "no link line")
    r.link_alive = True
    r.wait(0.5)
    check(not links(r) and r.out.status["frames"]["state"] == "fresh",
          "the first frame ever closes no outage: no link line")


# =========================================================================
# disarm_all: the show program's Abort (CONTRACT.md, 2026-10-02)
# =========================================================================

def _disarm(r, abort_id=1, reason="Abort from the rack screen", seq=None,
            mono=None, sender=SENDER):
    """One disarm_all from the rig's own ltcplay, next in its sequence."""
    if seq is None:
        r.seq += 1
        seq = r.seq
    m = link.DisarmAll(seq, max(r.t, r.mono_floor) if mono is None else mono,
                       abort_id, reason)
    return r.c.disarm_all(m, sender=sender)


def _two_armed():
    r = Rig()
    r.prove_alive()
    r.inp.set(0, 1)
    r.step()
    assert r.safety(0) == ARM and r.safety(1) == ARM
    return r


def test_disarm_all_link_decoding():
    section("disarm_all on the wire: strict shape, key first, exact field "
            "set, routed by decode_from_ltcplay")
    good = link.encode_disarm_all(7, 12.5, 3, "Abort", KEY)
    m = link.decode_disarm_all(good, KEY)
    check(isinstance(m, link.DisarmAll) and m.seq == 7 and m.mono == 12.5
          and m.abort_id == 3 and m.reason == "Abort",
          "a well-formed disarm_all decodes")
    r = link.decode_from_ltcplay(good, 1, KEY)
    check(isinstance(r, link.DisarmAll),
          "decode_from_ltcplay routes a disarm_all to the disarm decoder")
    f = link.decode_from_ltcplay(
        link.encode_flame(1, None, 1.0, 1, [0] * 512, KEY), 1, KEY)
    check(isinstance(f, link.FlameFrame),
          "decode_from_ltcplay still decodes a flame frame exactly as before")

    def obj(**over):
        d = {"v": 2, "k": KEY, "t": "disarm_all", "seq": 7, "mono": 1.0,
             "id": 1, "reason": "Abort"}
        for k, v in over.items():
            if v is _DROP:
                d.pop(k, None)
            else:
                d[k] = v
        return json.dumps(d).encode()

    cases = [
        (obj(v=1), "wrong contract version"),
        (obj(v=3), "wrong contract version"),
        (obj(k="x" * 20), "wrong key"),
        (obj(k=_DROP), "wrong key"),
        (obj(k="x" * 20, seq="garbage", id=-5), "wrong key"),
        (obj(t="flame"), "wrong message type"),
        (obj(arm=True), "field this contract does not describe"),
        (obj(wanted=[True]), "field this contract does not describe"),
        (obj(seq=-1), "seq"),
        (obj(seq=True), "seq"),
        (obj(seq=1.5), "seq"),
        (obj(seq=_DROP), "seq"),
        (obj(mono="1"), "mono"),
        (obj(mono=True), "mono"),
        (obj(mono=_DROP), "mono"),
        (obj(id=0), "id"),
        (obj(id=True), "id"),
        (obj(id=_DROP), "id"),
        (obj(reason=""), "reason"),
        (obj(reason="   "), "reason"),
        (obj(reason="x" * 201), "reason"),
        (obj(reason=5), "reason"),
        (obj(reason=_DROP), "reason"),
        (b"[1,2]", "not a JSON object"),
        (b"\xff\xfe", "not valid JSON"),
        (b"x" * 20000, "too long"),
    ]
    for data, want in cases:
        try:
            link.decode_disarm_all(data, KEY)
            check(False, f"accepted a bad disarm_all: {data[:80]!r}")
        except link.LinkError as e:
            check(want in str(e), f"{data[:80]!r} rejected for {want!r}, "
                                  f"said {e}")
    nan = (b'{"v":2,"k":"' + KEY.encode() + b'","t":"disarm_all","seq":1,'
           b'"mono":NaN,"id":1,"reason":"Abort"}')
    try:
        link.decode_disarm_all(nan, KEY)
        check(False, "a NaN mono was accepted")
    except link.LinkError as e:
        check("mono" in str(e), f"NaN mono rejected: {e}")
    try:
        link.decode_from_ltcplay(obj(k="y" * 20), 1, KEY)
        check(False, "decode_from_ltcplay accepted a wrong-key disarm_all")
    except link.LinkError as e:
        check(str(e) == "wrong key",
              f"decode_from_ltcplay: a wrong-key disarm_all is 'wrong key': "
              f"{e}")


_DROP = object()


def test_disarm_all_disarms_every_group_and_needs_a_fresh_cycle():
    section("disarm_all: every armed group off the wire on the next tick, "
            "the abort words on the lamp, and nothing re-arms until a "
            "fresh genuine cycle from the arm input")
    r = _two_armed()
    why = _disarm(r)
    check(why == "", f"disarm_all from the live, locked sender is accepted: "
                     f"{why!r}")
    r.step()
    check(all(r.safety(i) == 0 for i in range(N_GROUPS)),
          f"every safety slot is zero on the very next tick: "
          f"{[r.safety(i) for i in range(N_GROUPS)]}")
    g = r.group(0)
    check(g["armed"] == "held" and g["reason"] == composer.ABORT_DISARMED
          and g["amber"] == "flashing",
          f"the lamp says the Abort disarmed it and that cycling is the "
          f"fix: {g}")
    check(r.group(2)["armed"] == "disarmed",
          f"a group nobody wanted stays plain disarmed: {r.group(2)}")
    st = r.out.status["disarm_all"]
    check(st["accepted"] == 1 and st["last_id"] == 1
          and st["last_reason"] == "Abort from the rack screen"
          and isinstance(st["age_ms"], int),
          f"the status frame says which Abort it took: {st}")
    check(r.c.stats["disarm_all"] == 1, "counted")
    lines = [m for k, m in r.log.events if k == "disarm-all"]
    check(len(lines) == 1 and "front row" in lines[0]
          and "cat-walk" in lines[0] and "fresh arm cycle" in lines[0],
          f"one journal line naming what was armed: {lines}")
    # The deck never stops asking (nobody touched the keys): nothing comes
    # back, however long, and well past the dwell.
    r.wait(3.0)
    check(r.safety(0) == 0 and r.safety(1) == 0,
          "still asking for arm, 3 s later: still disarmed")
    check(r.group(0)["reason"] == composer.ABORT_DISARMED,
          f"and still says why: {r.group(0)}")
    # A genuine cycle of group 0 only: down, then up.
    r.inp.set(0, on=False)
    r.step(n=2)
    r.inp.set(0)
    r.wait(1.2)                   # the operator's own disarm starts a dwell
    check(r.safety(0) == ARM,
          f"a fresh genuine cycle re-arms that group: {r.group(0)}")
    check(r.group(0)["reason"] == "" and r.safety(1) == 0
          and r.group(1)["reason"] == composer.ABORT_DISARMED,
          f"and only that group: {r.group(0)} {r.group(1)}")
    # Once re-armed, the Abort is history for that group: a later,
    # unrelated loss of the latch (the arm input restarting) says what
    # really happened, not "the show's Abort".
    r.inp.reboot()
    r.step()
    check(r.safety(0) == 0 and r.group(0)["reason"] == "cycle the arm",
          f"a later latch loss is not blamed on the old Abort: "
          f"{r.group(0)}")


def test_disarm_all_dwell_and_pending_edges():
    section("disarm_all: a cycle straight after the Abort still waits out "
            "the re-arm dwell, and a consent edge begun before the Abort "
            "cannot complete after it")
    r = _two_armed()
    _disarm(r)
    r.step()
    r.inp.set(0, on=False)
    r.step()
    r.inp.set(0)
    r.step()
    check(r.safety(0) == 0 and r.group(0)["reason"] == "re-arm dwell",
          f"a cycle straight after the Abort waits out the dwell: "
          f"{r.group(0)}")
    r.wait(1.1)
    check(r.safety(0) == ARM, "and arms once the dwell has passed")

    # A pending edge: group 2 has been reported down while live (consent
    # set up), the operator's arm press lands on the same tick as the
    # Abort, after it. It must not arm.
    r = Rig()
    r.prove_alive()
    r.inp.set(2)                  # the "up" half, polled on the next step
    _disarm(r)                    # ...but the Abort is drained first
    r.wait(2.0)
    check(r.safety(2) == 0 and r.group(2)["reason"] == composer.ABORT_DISARMED,
          f"a down edge seen before the Abort is forgotten by it: "
          f"{r.group(2)}")
    # A forced low after the Abort is not a cycle either (round 3's rule
    # still holds on top of this one).
    r.inp.set(2, on=False)
    r.inp.set_forced(2)
    r.step(n=2)
    r.inp.set_forced(2, on=False)
    r.inp.set(2)
    r.wait(2.0)
    check(r.safety(2) == 0,
          f"a FORCED low after the Abort does not complete a cycle: "
          f"{r.group(2)}")

    # Fix round 1 of PR #34, item 2 (the review's p2 s2/s3): the same edge
    # begun up to 1 s BEFORE the Abort.  The Stream Deck reports a group
    # low all through an arm-HOLD (0.6 s), so the low simply carries on
    # through the Abort, re-proved by every frame after it, and the high
    # lands when the hold completes.  It must not arm, whenever inside
    # min_arm_dwell_ms of the Abort the high lands.
    bad = []
    for begun in (0.0, 0.1, 0.3, 0.6, 1.0):
        for after in (0.025, 0.1, 0.3, 0.6, 0.95):
            r = Rig()
            r.prove_alive()
            r.wait(begun)             # the low, reported every tick
            check(_disarm(r) == "", "the Abort is taken")
            r.wait(after)             # still low, frames still arriving
            r.inp.set(2)              # the hold completes
            r.wait(2.0)
            if r.safety(2) != 0 or \
                    r.group(2)["reason"] != composer.ABORT_DISARMED:
                bad.append((begun, after, r.group(2)))
    check(not bad, f"a hold begun up to 1 s before the Abort and completed "
                   f"inside min_arm_dwell_ms after it never arms: {bad}")
    # The window ends: a low still going on min_arm_dwell_ms after the
    # Abort is a fresh one, and a press made after that arms as usual.
    r = Rig()
    r.prove_alive()
    _disarm(r)
    r.wait(1.1)
    r.inp.set(2)
    r.step(n=2)
    check(r.safety(2) == ARM,
          f"a press after the window arms as usual: {r.group(2)}")
    # Fix round 2 of PR #34, item 3: every repeat copy of an Abort restarts
    # the window (ltcplay repeats one Abort after every frame for
    # frame_stale_ms + 0.25 s).  A high landing 1.3 s after the FIRST copy
    # but 0.6 s after a repeat is still inside the window, and refused.
    r = Rig()
    r.prove_alive()
    check(_disarm(r, abort_id=7) == "", "the first copy is taken")
    r.wait(0.7)
    check(_disarm(r, abort_id=7) == "", "a repeat copy is taken")
    r.wait(0.6)
    r.inp.set(2)
    r.wait(2.0)
    check(r.safety(2) == 0 and r.group(2)["reason"] == composer.ABORT_DISARMED,
          f"a repeat copy restarts the window: a hold completing 0.6 s after "
          f"it is refused, 1.3 s after the first: {r.group(2)}")


def test_disarm_all_sequence_and_liveness():
    section("disarm_all: shares the flame frames' sequence (a same-seq one "
            "is refused, and it moves the sequence on) and never keeps the "
            "flame link alive by itself")
    r = _two_armed()
    why = _disarm(r, seq=r.seq)
    check("out of order" in why and r.safety(0) == ARM,
          f"a disarm_all with the same seq as the last frame is refused: "
          f"{why!r}")
    check(_disarm(r) == "", "the next seq is taken")
    why = r.frame(seq=r.seq)
    check("out of order" in why,
          f"and a flame frame reusing that seq is then refused: {why!r}")
    # Liveness comes from flame frames only.
    r = _two_armed()
    r.link_alive = False          # flame frames stop here
    r.wait(0.3)
    check(_disarm(r) == "", "0.3 s after the last frame: still taken")
    r.wait(0.25)                  # 0.55 s after the frame, 0.25 after it
    check(r.out.status["frames"]["state"] == "stale",
          f"the link goes stale frame_stale_ms after the last FLAME frame, "
          f"however recent the disarm_all: {r.out.status['frames']}")


def test_link_text_fields_match_whole():
    section("link: the timecode and the key are matched whole (fix round 1 "
            "of PR #34, item 9): no trailing newline, ASCII digits only")
    check(link.valid_key(KEY) and not link.valid_key(KEY + "\n"),
          "a key with a trailing newline is not a valid key")
    for tc in ("00:00:01:00\n", "00:00:01:00:00",
               "٠٠:٠٠:٠١:٠٠"):
        try:
            link.decode_flame(link.encode_flame(1, tc, 1.0, 1, [0] * 512,
                                                KEY), 1, KEY)
            check(False, f"a frame with timecode {tc!r} was decoded")
        except link.LinkError as e:
            check("tc" in str(e), f"timecode {tc!r} refused: {e}")
    f = link.decode_flame(link.encode_flame(1, "00:00:01:00", 1.0, 1,
                                            [0] * 512, KEY), 1, KEY)
    check(f.timecode == "00:00:01:00", "a plain timecode is still taken")


def test_disarm_all_can_never_arm():
    section("disarm_all can only take arm away: a random mix of Aborts and "
            "an arm input that never genuinely cycles never arms anything")
    rnd = random.Random(20261002)
    for trial in range(30):
        r = Rig()
        r.inp.set_all(True)          # asking for everything, from boot
        for step in range(120):
            x = rnd.random()
            if x < 0.15:
                _disarm(r, abort_id=rnd.randint(1, 4))
            elif x < 0.2:
                r.inp.set_all(rnd.random() < 0.5)
                r.inp.forced = [True] * N_GROUPS
                r.step()
                r.inp.forced = [False] * N_GROUPS
                r.inp.set_all(True)
            r.step()
            if any(r.safety(i) for i in range(N_GROUPS)):
                check(False, f"trial {trial} step {step}: a group armed "
                             f"without a genuine cycle: "
                             f"{[r.group(i) for i in range(N_GROUPS)]}")
                return
    check(True, "never armed")
    # And with a group really armed, every disarm_all only ever lowers it.
    r = _two_armed()
    for k in range(20):
        _disarm(r, abort_id=1 + k % 3)
        r.step()
        check(r.safety(0) == 0 and r.safety(1) == 0,
              f"disarm_all number {k + 1} left it disarmed")


def test_disarm_all_rejections():
    section("disarm_all: refused unless from the live, locked flame-link "
            "sender, in order; a refused one changes nothing and is "
            "journaled once per episode")
    r = _two_armed()
    for label, kw in (("another sender", {"sender": ("127.0.0.1", 40999)}),
                      ("out of order", {"seq": 1}),
                      ("clock went backwards", {"mono": -5.0})):
        before = r.c.stats["disarm_all"]
        why = _disarm(r, **kw)
        r.step()
        check(label in why and r.safety(0) == ARM and r.safety(1) == ARM
              and r.c.stats["disarm_all"] == before,
              f"{label}: refused ({why!r}), both groups still armed")
        check(r.out.status["frames"]["last_reject"] == why,
              "and the reason is in the status frame")
    check(r.c.stats["disarm_all_rejected"] == 3, "every refusal is counted")
    why = r.c.disarm_all("not a message", sender=SENDER)
    check(why and r.c.stats["disarm_all_rejected"] == 4,
          f"something that is not a DisarmAll is refused: {why!r}")
    # A rogue flood: one journal line for the whole episode, then one
    # closing line with the count once it stops.
    r.wait(5.2)                   # close the episodes the refusals above began
    r.log.events.clear()
    for i in range(50):
        _disarm(r, sender=("127.0.0.1", 41000 + i), seq=10 ** 6 + i)
        if i % 5 == 0:
            r.step()
    rej = [m for k, m in r.log.events if k == "link-reject"]
    check(len(rej) == 1 and "another sender" in rej[0],
          f"a 50-datagram foreign flood from 50 ports is one line: {rej}")
    r.wait(2.0)
    check(len([k for k, _ in r.log.events if k == "link-reject"]) == 1,
          "a 2 s pause does not end the episode (5 s does)")
    r.wait(3.2)
    rej = [m for k, m in r.log.events if k == "link-reject"]
    check(len(rej) == 2 and "stopped after 50" in rej[1]
          and "50 distinct source addresses" in rej[1],
          f"and one more when it stops, with the count: {rej}")
    check(r.safety(0) == ARM, "the flood disarmed nothing")
    # With the flame link stale, there is no sender to take it from.
    r.link_alive = False
    r.wait(0.7)
    why = _disarm(r)
    check("no live flame link" in why,
          f"refused with no live flame link: {why!r}")


def test_disarm_all_duplicates_journal_once():
    section("disarm_all: the sender's repeat copies of one Abort are all "
            "applied but journaled once; a new Abort is a new line")
    r = _two_armed()
    for _ in range(3):
        check(_disarm(r, abort_id=1) == "", "a copy is accepted")
    r.step()
    check(len([k for k, _ in r.log.events if k == "disarm-all"]) == 1,
          "three copies of abort 1: one line")
    check(r.out.status["disarm_all"]["accepted"] == 3, "all three counted")
    _disarm(r, abort_id=2)
    r.step()
    check(len([k for k, _ in r.log.events if k == "disarm-all"]) == 2,
          "abort 2: a second line")
    # Fix round 1 of PR #34, item 4: a restarted ltcplay is a new sender.
    # Its Abort is a new one even if its id happens to equal the last.
    r.link_alive = False
    r.wait(0.7)
    other = ("127.0.0.1", 41555)
    check(r.frame(sender=other) == "", "a new sender takes the stale link")
    check(_disarm(r, abort_id=2, sender=other) == "", "and its Abort is taken")
    check(len([k for k, _ in r.log.events if k == "disarm-all"]) == 3,
          "the same id from a new sender: a new line")


def test_flame_link_rejections_journal_once_per_episode():
    section("flame link: rejected flame frames are journaled once per "
            "episode per kind of reason, with a closing count")
    r = Rig()
    r.prove_alive()
    r.log.events.clear()
    for i in range(30):
        r.frame(sender=("127.0.0.1", 42000 + i), seq=10 ** 6 + i)
    r.c.reject_frame("wrong key")
    r.c.reject_frame("wrong key")
    r.c.reject_frame("wrong contract version 9, this program speaks 2")
    rej = [m for k, m in r.log.events if k == "link-reject"]
    check(len(rej) == 3 and "another sender" in rej[0]
          and "wrong key" in rej[1] and "wrong contract version" in rej[2],
          f"one line per kind: {rej}")
    r.wait(5.2)
    rej = [m for k, m in r.log.events if k == "link-reject"]
    check(len(rej) == 5 and any("stopped after 30" in m for m in rej)
          and any("stopped after 2" in m for m in rej),
          f"after 5 s quiet, closing lines only for reasons that repeated: "
          f"{rej}")
    # Text a sender controls never opens a new episode: a `v` nested in
    # lists to a different depth each time is a different MESSAGE straight
    # out of the real decoder, but one REASON.
    r.wait(5.2)
    r.log.events.clear()
    msgs = set()
    for depth in range(1, 61):
        bad = json.dumps({"v": json.loads("[" * depth + "]" * depth),
                          "k": KEY, "t": "flame"}).encode()
        try:
            link.decode_from_ltcplay(bad, 1, KEY)
        except link.LinkError as e:
            msgs.add(str(e))
            r.c.reject_frame(str(e), sender=("127.0.0.1", 43000 + depth))
    rej = [m for k, m in r.log.events if k == "link-reject"]
    check(len(msgs) == 60 and len(rej) == 1
          and "wrong contract version" in rej[0],
          f"60 different messages, one reason, one line: {len(msgs)} "
          f"messages, {rej}")
    long = "wrong key" + "x" * 5000
    r.wait(5.2)
    r.log.events.clear()
    r.c.reject_frame(long)
    rej = [m for k, m in r.log.events if k == "link-reject"]
    check(len(rej) == 1 and len(rej[0]) < 600,
          f"a sender's long text is cut short in the line: {len(rej[0])}")
    # And the per-minute cap: episodes one after another, each opened and
    # closed, never write more than 4 lines for one reason in a minute.
    # (60 s first: since #31's round 5 there is also a cap of 8 lines a
    # minute across all reasons, and the lines above would use it up.)
    r.wait(60.5)
    r.log.events.clear()
    for _ in range(6):
        r.c.reject_frame("tc is not HH:MM:SS:FF or null")
        r.c.reject_frame("tc is not HH:MM:SS:FF or null")
        r.wait(5.2)
    rej = [m for k, m in r.log.events if k == "link-reject"]
    check(len(rej) == 4, f"6 episodes in 31 s: 4 lines, the cap: {rej}")


def test_disarm_all_over_loopback():
    section("disarm_all over real UDP into a real Service: disarmed on the "
            "tick it arrives, and refused from another socket or a wrong "
            "key")
    node = _udp()
    ltc_status = _udp()
    lp_sock = _udp()
    lp = lp_sock.getsockname()[1]
    lp_sock.close()
    cfg = make_config(destination={"ip": "127.0.0.1",
                                   "port": node.getsockname()[1]},
                      link={"listen_ip": "127.0.0.1", "listen_port": lp,
                            "status_ip": "127.0.0.1",
                            "status_port": ltc_status.getsockname()[1],
                            "key": KEY})
    t = [0.0]
    log = Log()
    inp = arminput.ScriptedArmInput(cfg.n, names=NAMES)
    svc = Service(cfg, inp, clock=lambda: t[0], log=log)
    svc.open()
    ltc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rogue = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    seq = [0]

    def nxt():
        seq[0] += 1
        return seq[0]

    def frame():
        ltc.sendto(link.encode_flame(nxt(), "00:00:01:00", t[0], 1,
                                     [0] * 512, KEY), ("127.0.0.1", lp))

    def tick():
        t[0] += cfg.tick_period_s
        frame()
        time.sleep(0.004)
        return svc.run_once()

    try:
        for _ in range(3):
            tick()
        inp.set(0, 1)
        out = tick()
        check(out.universe[400] == ARM and out.universe[401] == ARM,
              "setup: two groups armed through a real Service")
        rogue.sendto(link.encode_disarm_all(10 ** 6, t[0], 1, "Abort", KEY),
                     ("127.0.0.1", lp))
        ltc.sendto(link.encode_disarm_all(nxt(), t[0], 1, "Abort",
                                          "wrong-key-wrong-key-x"),
                   ("127.0.0.1", lp))
        out = tick()
        check(out.universe[400] == ARM and out.universe[401] == ARM,
              "a disarm_all from another socket, or with the wrong key from "
              "the right one, disarms nothing")
        check(svc.composer.stats["disarm_all_rejected"] == 1
              and svc.composer.stats["frames_rejected"] >= 1,
              f"both refused and counted: {svc.composer.stats}")
        kinds = [m for k, m in log.events if k == "link-reject"]
        check(any("another sender" in m for m in kinds)
              and any("wrong key" in m for m in kinds),
              f"both journaled: {kinds}")
        # Second-copy guard (2026-10-03): the other socket counts as on the
        # link for frame_stale_ms, and the lamp would say so rather than
        # the Abort sentence below.  Let it go; the groups stay armed.
        for _ in range(int(0.6 / cfg.tick_period_s)):
            out = tick()
        check(out.universe[400] == ARM and out.universe[401] == ARM,
              "another sender turning up does not disarm what was armed")
        ltc.sendto(link.encode_disarm_all(nxt(), t[0], 1, "Abort", KEY),
                   ("127.0.0.1", lp))
        time.sleep(0.01)
        t[0] += cfg.tick_period_s
        out = svc.run_once()
        check(out.universe[400] == 0 and out.universe[401] == 0,
              "the real ltcplay's disarm_all takes every group off the wire "
              "on the tick it arrives in")
        s = link.decode_status(_drain(ltc_status)[-1], KEY)
        check(s["disarm_all"]["last_id"] == 1
              and s["groups"][0]["reason"] == composer.ABORT_DISARMED,
              f"the status frame on the wire says so: {s['disarm_all']} "
              f"{s['groups'][0]}")
    finally:
        svc.close()
        ltc.close()
        rogue.close()
        node.close()
        ltc_status.close()


# =========================================================================
# Second-copy guard (Jeff, 2026-10-03; PR #34 open question 10, probe p2 s5)
# =========================================================================

OTHER = ("127.0.0.1", 40777)


def _step_both(r, first, second=None, n=1):
    """n ticks with `first` sending a frame every tick and, if given,
    `second` sending one right after it (refused while `first` holds the
    lock).  The arm input is polled exactly as Rig.step does."""
    for _ in range(n):
        r.t += r.period
        r.frame(r.cue, sender=first)
        if second is not None:
            r.frame({}, sender=second)
        a = r.inp.poll()
        if a is not None:
            r.c.assert_arm(a.wanted, a.seq, names=a.names, forced=a.forced)
        r.out = r.c.tick()
    return r.out


def test_second_copy_another_flame_sender_blocks_consent():
    section("second-copy guard: while another sender with the key is on the "
            "flame link no cycle counts, a down edge from before it cannot "
            "be finished, and once it has gone a genuine off-then-on arms "
            "(the arm link's round-4 veto, on the flame link)")
    stale = make_config().frame_stale_ms / 1000.0
    r = Rig()
    r.prove_alive()                      # a pending genuine down edge
    r.link_alive = False                 # this test sends the frames itself
    _step_both(r, SENDER, OTHER)         # a second copy turns up
    r.inp.set(0)
    _step_both(r, SENDER, OTHER, n=3)
    g = r.group(0)
    check(r.safety(0) != ARM,
          f"a cycle finished while another sender is on the flame link does "
          f"not arm: {g}")
    check(g["armed"] == "held" and g["reason"] == composer.FLAME_OTHER_SENDER
          and g["amber"] == "steady",
          f"held, saying why, steady: {g}")
    f = r.out.status["frames"]
    check(f["foreign_senders"] == 1 and f["new_sender"] is False
          and f["state"] == "fresh",
          f"the status frame counts the other sender: {f}")
    r.inp.set(0, on=False)
    _step_both(r, SENDER, OTHER, n=3)
    r.inp.set(0)
    _step_both(r, SENDER, OTHER, n=int(1.5 / r.period))
    check(r.safety(0) != ARM,
          f"a fresh cycle made while it is still there does not count "
          f"either: {r.group(0)}")
    # It stops.  Within frame_stale_ms it still counts as there.
    _step_both(r, SENDER, n=int(stale / r.period) - 2)
    check(r.out.status["frames"]["foreign_senders"] == 1,
          f"inside frame_stale_ms of its last datagram it still counts: "
          f"{r.out.status['frames']}")
    _step_both(r, SENDER, n=int(1.0 / r.period))
    g = r.group(0)
    check(r.out.status["frames"]["foreign_senders"] == 0,
          f"after frame_stale_ms quiet it is gone: {r.out.status['frames']}")
    check(r.safety(0) != ARM and g["reason"] == "cycle the arm",
          f"and the group does NOT arm by itself: {g}")
    r.inp.set(0, on=False)
    _step_both(r, SENDER, n=2)
    r.inp.set(0)
    _step_both(r, SENDER, n=int((r.cfg.min_arm_dwell_ms / 1000.0 + 0.5)
                                / r.period))
    check(r.safety(0) == ARM,
          f"a genuine off-then-on afterwards arms it: {r.group(0)}")
    # An armed group is not disarmed by another sender turning up (the arm
    # link's rule too): only new arming is refused.
    _step_both(r, SENDER, OTHER, n=4)
    check(r.safety(0) == ARM,
          f"a group armed before the other sender came stays armed: "
          f"{r.group(0)}")
    # A keyed disarm_all from another sender is a second sender too.
    r2 = Rig()
    r2.prove_alive()
    r2.c.disarm_all(link.DisarmAll(10 ** 6, r2.t, 7, "x"), sender=OTHER)
    r2.inp.set(0)
    r2.step(n=2)
    check(r2.safety(0) != ARM
          and r2.group(0)["reason"] == composer.FLAME_OTHER_SENDER,
          f"a refused disarm_all from another socket blocks consent too: "
          f"{r2.group(0)}")
    msgs = [m for k, m in r.log.events if k == "link-reject"]
    check(any("another sender" in m and "newly armed" in m for m in msgs),
          f"the journal line says no group can be newly armed: {msgs[:2]}")


def test_second_copy_flame_lock_changing_hands_blocks_consent():
    section("second-copy guard: when the flame link's lock passes to a "
            "different sender, no cycle counts until the newcomer has been "
            "the only sender for frame_stale_ms; the same sender coming "
            "back is not a change")
    r = Rig()
    r.prove_alive()
    r.link_alive = False
    r.wait(0.6)                          # the link goes stale
    r.inp.set(0, on=False)
    _step_both(r, OTHER)                 # a different sender takes the lock
    f = r.out.status["frames"]
    check(f["state"] == "fresh" and f["new_sender"] is True
          and f["foreign_senders"] == 0,
          f"the status frame says the lock just changed hands: {f}")
    check(any(k == "link" and "taken by 127.0.0.1:40777" in m
              and "newly armed" in m for k, m in r.log.events),
          f"journaled once, naming both: {r.log.events[-3:]}")
    _step_both(r, OTHER, n=3)
    r.inp.set(0)
    _step_both(r, OTHER, n=3)
    g = r.group(0)
    check(r.safety(0) != ARM and g["reason"] == composer.FLAME_OTHER_SENDER,
          f"a cycle inside frame_stale_ms of the change does not arm: {g}")
    _step_both(r, OTHER, n=int(1.0 / r.period))
    check(r.safety(0) != ARM and r.out.status["frames"]["new_sender"] is False,
          f"nor by itself once it has settled: {r.group(0)}")
    r.inp.set(0, on=False)
    _step_both(r, OTHER, n=2)
    r.inp.set(0)
    _step_both(r, OTHER, n=int((r.cfg.min_arm_dwell_ms / 1000.0 + 0.5)
                               / r.period))
    check(r.safety(0) == ARM,
          f"a genuine off-then-on after it has settled arms: {r.group(0)}")
    # A change of hands inside one tick: the link goes stale just after a
    # tick and the newcomer's frame arrives before the next one, so no tick
    # ever saw the link lost.  The change itself must disarm.
    r = armed_rig()
    r.link_alive = False
    t0 = r.t                             # the last frame was on this tick
    stale = r.cfg.frame_stale_ms / 1000.0
    while r.t + r.period <= t0 + stale:
        r.step()
    check(r.safety(0) == ARM and r.out.status["frames"]["state"] == "fresh",
          f"setup: still armed on the last tick inside frame_stale_ms: "
          f"{r.out.status['frames']}")
    r.t = t0 + stale + 0.002             # stale now, before the next tick
    check(r.frame({}, sender=OTHER) == "", "the newcomer takes the lock")
    r.t = t0 + stale + 0.010
    a = r.inp.poll()
    r.c.assert_arm(a.wanted, a.seq, names=a.names, forced=a.forced)
    r.out = r.c.tick()
    check(r.out.status["frames"]["state"] == "fresh" and r.safety(0) != ARM,
          f"a group armed under the old sender is disarmed by the change of "
          f"hands even though no tick saw the link lost: {r.group(0)}")
    # The same sender back after a gap: no change of hands.
    r = Rig()
    r.prove_alive()
    r.link_alive = False
    r.wait(0.6)
    r.link_alive = True
    r.step()
    check(r.out.status["frames"]["new_sender"] is False,
          f"the same sender taking the lock back is not a change: "
          f"{r.out.status['frames']}")
    # The first sender ever is not a change either.
    r = Rig()
    r.step()
    check(r.out.status["frames"]["new_sender"] is False,
          f"the first sender ever is not a change: {r.out.status['frames']}")


def test_second_copy_restart_gap_takeover_over_loopback():
    section("second-copy guard, end to end over real UDP (probe p2 s5): a "
            "second sender with the key takes the flame link in an ltcplay "
            "restart gap; the real ltcplay comes back and is refused; the "
            "operator cycles the arm; nothing arms and none of the second "
            "sender's fire values reach the wire")
    node = _udp()
    ltc_status = _udp()
    lp_sock = _udp()
    lp = lp_sock.getsockname()[1]
    lp_sock.close()
    cfg = make_config(destination={"ip": "127.0.0.1",
                                   "port": node.getsockname()[1]},
                      link={"listen_ip": "127.0.0.1", "listen_port": lp,
                            "status_ip": "127.0.0.1",
                            "status_port": ltc_status.getsockname()[1],
                            "key": KEY})
    t = [0.0]
    log = Log()
    inp = arminput.ScriptedArmInput(cfg.n, names=NAMES)
    svc = Service(cfg, inp, clock=lambda: t[0], log=log)
    svc.open()
    socks = {"ltc": socket.socket(socket.AF_INET, socket.SOCK_DGRAM),
             "rogue": socket.socket(socket.AF_INET, socket.SOCK_DGRAM)}
    seqs = {"ltc": 0, "rogue": 0}
    fire = [0] * 512
    for s in FRONT_FIRE:
        fire[s - 1] = 255

    def send(who, values=None):
        seqs[who] += 1
        socks[who].sendto(link.encode_flame(seqs[who], "00:00:01:00", t[0],
                                            1, values or [0] * 512, KEY),
                          ("127.0.0.1", lp))

    def tick(*who, values=None):
        t[0] += cfg.tick_period_s
        for w in who:
            send(w, values if w == "rogue" else None)
        time.sleep(0.003)
        return svc.run_once()

    try:
        for _ in range(3):
            tick("ltc")
        inp.set(0)
        out = tick("ltc")
        check(out.universe[FRONT_SAFETY - 1] == ARM,
              "setup: group 0 armed through a real Service")
        # ltcplay restarts: nothing for 0.6 s, so the link is lost
        for _ in range(int(0.6 / cfg.tick_period_s)):
            out = tick()
        check(out.universe[FRONT_SAFETY - 1] == 0, "link lost: disarmed")
        # the second sender gets in first; ltcplay comes back on a new port
        tick("rogue")
        socks["ltc"].close()
        socks["ltc"] = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for _ in range(10):
            out = tick("rogue", "ltc")
        check("another sender" in out.status["frames"]["last_reject"],
              f"the real ltcplay is refused: {out.status['frames']}")
        # the operator cycles the arm, as the lamp asked, more than once
        armed = False
        for _ in range(3):
            inp.set(0, on=False)
            for _ in range(int(1.2 / cfg.tick_period_s)):
                out = tick("rogue", "ltc")
                armed |= out.universe[FRONT_SAFETY - 1] == ARM
            inp.set(0)
            for _ in range(int(1.2 / cfg.tick_period_s)):
                out = tick("rogue", "ltc", values=fire)
                armed |= out.universe[FRONT_SAFETY - 1] == ARM
                armed |= any(out.universe[s - 1] for s in FRONT_FIRE)
        check(not armed,
              "nothing armed and no fire on the wire while both are sending")
        g = out.status["groups"][0]
        check(g["armed"] == "held"
              and g["reason"] == composer.FLAME_OTHER_SENDER
              and out.status["frames"]["foreign_senders"] == 1,
              f"held, saying another sender is on the show program link: "
              f"{g['reason']!r} {out.status['frames']}")
    finally:
        svc.close()
        for s in socks.values():
            s.close()
        node.close()
        ltc_status.close()
def _free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_status_mirror_port():
    section("status_mirror_port (2026-10-03, the iPad remote): optional, a "
            "byte-for-byte copy of every status frame, display only, never "
            "a fault, never on a port that means something else")
    doc = example_dict()
    check("status_mirror_port" not in doc["link"],
          "the example config has no mirror: off unless configured")
    check(config.from_dict(doc).link_status_mirror_port is None,
          "absent means none")
    a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    a.bind(("127.0.0.1", 0))
    b.bind(("127.0.0.1", 0))
    a.settimeout(2)
    b.settimeout(2)
    try:
        d = copy.deepcopy(doc)
        d["link"]["status_port"] = a.getsockname()[1]
        d["link"]["status_mirror_port"] = b.getsockname()[1]
        d["link"]["listen_port"] = _free_udp_port()
        d["link"]["arm_port"] = _free_udp_port()
        d["link"]["key"] = "test-mirror-key-0123456"
        cfg = config.from_dict(d)
        check(cfg.link_status_mirror_port == b.getsockname()[1],
              "the mirror port is read")
        for clash in ("status_port", "listen_port", "arm_port"):
            bad = copy.deepcopy(d)
            bad["link"]["status_mirror_port"] = bad["link"][clash]
            try:
                config.from_dict(bad)
                check(False, f"a mirror on {clash} was accepted")
            except config.ConfigError as e:
                check("status_mirror_port" in str(e), f"{clash}: {e}")
        bad = copy.deepcopy(d)
        bad["destination"] = {"ip": "127.0.0.1",
                              "port": d["link"]["status_mirror_port"]}
        try:
            config.from_dict(bad)
            check(False, "a loopback destination on the mirror was accepted")
        except config.ConfigError:
            pass
        svc = Service(cfg, arminput.NullArmInput())
        svc.open()
        try:
            svc.run_once()
            one = a.recv(65535)
            two = b.recv(65535)
            check(one == two and b'"status"' in one,
                  "the mirror gets the same keyed status frame")
            b.close()
            svc.cfg.link_status_mirror_port = 9   # nothing listens there
            for _ in range(3):
                svc.run_once()
            last = None
            a.settimeout(0.2)
            try:
                while True:
                    last = a.recv(65535)
            except OSError:
                pass
            st = json.loads(last.decode()) if last else {}
            check(svc.status_errors == 0 and st.get("fault") == "",
                  f"a mirror that is not there is never a status error or "
                  f"a fault: {svc.status_errors} {st.get('fault')!r}")
        finally:
            svc.close()
    finally:
        a.close()
        try:
            b.close()
        except OSError:
            pass


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
    test_config_validates_the_arm_link()
    test_rule10_startup_is_all_zeros()
    test_rule6_consent()
    test_rule2_dirty_edge_holds_the_arm()
    test_rule3_edge_quiet_frames()
    test_rule4_dwell()
    test_rule5_chatter()
    test_rule7_interruptions_clear_the_latches()
    test_liveness_loss_zeros_within_a_bounded_time()
    test_ltcplay_stale_zeros_fire_then_disarms()
    test_link_rejects_malformed_datagrams()
    test_arm_link_rejects_malformed_datagrams_and_round_trips()
    test_socket_arm_input_is_the_real_build_step_7b_driver()
    test_socket_arm_input_sender_lock()
    test_socket_arm_input_foreign_sender_can_still_disarm()
    test_socket_arm_input_foreign_sender_episode_logged_once()
    test_socket_arm_input_foreign_flood_from_varying_source_ports_logged_once()
    test_socket_arm_input_foreign_count_tracks_live_foreign_senders()
    test_composer_status_carries_foreign_arm_senders()
    test_service_journals_a_raising_assert_arm()
    test_socket_arm_input_really_arms_a_group_end_to_end()
    test_socket_arm_input_reports_which_bits_were_forced()
    test_round3_foreign_forced_edge_is_not_consent_end_to_end()
    test_round4_no_consent_while_another_sender_or_a_flood_is_on_the_link()
    test_round4_a_forced_bit_never_blocks_a_genuine_cycle_on_another_group()
    test_round4_a_forced_low_clears_an_earlier_genuine_down_edge()
    test_round4_a_malformed_forced_vector_is_rejected()
    test_round4_genuine_lows_are_never_reported_forced()
    test_round4_consent_never_spans_two_senders()
    test_round4_socket_arm_input_flags_a_flood()
    test_round5_flood_thresholds_are_pinned_in_datagrams_and_bytes()
    test_round5_arm_link_lines_have_a_global_ceiling()
    test_round5_arm_link_lines_never_crowd_out_important_ones()
    test_round4_service_passes_the_flood_flag_before_assert_arm()
    test_round4_decode_rejections_are_throttled_per_reason()
    test_round4_flood_takeover_cannot_rearm_end_to_end()
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
    test_review_sender_lock()
    test_review_send_failures_are_faults()
    test_review_journal_never_blocks_the_tick()
    test_review_panic_status_is_honest()
    test_review2_frozen_counter_then_synthetic_down()
    test_review2_a_fault_clears_after_five_clean_seconds()
    test_review2_journal_drops_are_counted_and_written_up()
    test_review2_udp_connreset_is_really_switched_off()
    test_review2_keys()
    test_link_loss_disarms_every_group()
    test_review3_link_loss_is_one_line_and_the_return_is_one_line()
    test_disarm_all_link_decoding()
    test_disarm_all_disarms_every_group_and_needs_a_fresh_cycle()
    test_disarm_all_dwell_and_pending_edges()
    test_disarm_all_sequence_and_liveness()
    test_link_text_fields_match_whole()
    test_disarm_all_can_never_arm()
    test_disarm_all_rejections()
    test_disarm_all_duplicates_journal_once()
    test_flame_link_rejections_journal_once_per_episode()
    test_disarm_all_over_loopback()
    test_second_copy_another_flame_sender_blocks_consent()
    test_second_copy_flame_lock_changing_hands_blocks_consent()
    test_second_copy_restart_gap_takeover_over_loopback()
    test_status_mirror_port()
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
