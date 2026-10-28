"""Wave flamer cleaning: the nightly shutdown routine on the Shutdown part
of the Morning and Shutdown page (Andy's design, via Jeff, 2026-10-28).
MORNING.md, "Wave flamer cleaning", and OPERATOR.md say it in plain words.

What it does (the defaults; ltcplay_morning.json "cleaning" may change
each within the limits in LIMITS)
==================================================================

  Phase 1, three times: arm the wave flamer group, wait until flamesafe
  reports it ARMED, keep it armed 3 s, disarm it, wait until flamesafe
  reports it disarmed, keep it disarmed 3 s.
  Phase 2: arm it again, wait until flamesafe reports it ARMED, point
  every head straight up (the angle channel, an aim slot, at 128; speed,
  duration and preset stay 0), wait aim_settle_s, then fire in bursts:
  ignition (channel 3) 255 for 1 s, 0 for 1 s, over and over until the
  operator stops it.

Every run ends by itself after max_run_min (10 min) whatever else holds.

It is a DEAD-MAN sequence
=========================

(authorization "dead_man", the default and, until Jeff approves the
other, the only mode that runs: see MODES.) It runs only while a person
keeps holding the button. The page beats
every 100 ms while the finger is down; a run with no beat for
BEAT_STALE_S is stopped by this module's watcher (every WATCH_S), and the
flame link drops the frame by itself PURGE_HOLD_S after the last beat
whatever this module does. Nothing here ever starts by itself, on a
schedule or from a timer.

How it arms (no new way to arm)
===============================

An arm press here is the rack screen's own hold to arm: each beat of the
cleaning button, while the sequence wants the group armed, calls the
engine's existing arm-hold handler (remote.Remote.arm_hold) in that same
request, with that request's own device, window, signed-in user and page
status time. That handler is what the rack screen's Hold to arm button
posts to, and it runs every rule it always runs (screen arming on, the
page's and flamesafe's status fresh, Abort, live flame programming,
Arrange mode, the lockout, one hold at a time, the deck reading the
engine). The Stream Deck process then presses the group's key exactly as
for a held screen button: only after SCREEN_HOLD_S of beat-evidenced
holding, with its re-arm refractory window, the latch and the operator
gate, and flamesafe applies min_arm_dwell_ms, the clean rising edge and
the edge-quiet window. Only a beat (a person holding) ever calls it: the
watcher thread never presses anything. The cleaning-beat route is an arm
press for the role matrix (users.classify), so signed out is view only.

Disarming (phase 1's, and every stop) is the screen's own per-group
Disarm (remote.Remote.group_disarm, an instant tap of the group's key on
the deck); a stop whose per-group disarm cannot go, or that flamesafe has
not confirmed within DISARM_CONFIRM_S, gets Disarm all flame groups
(remote.Remote.disarm_all, which itself falls back to the Abort's disarm).

The fire values
===============

They go out only as the Startup page's purge frame on the engine's flame
link (FlameLink.purge_set), like the Fireball page: flamesafe stays the
only writer of the flame universe and sends a fire value only for a group
that is ARMED. Nothing is set until flamesafe reports the group ARMED in
phase 2; in phase 1 no frame is set at all. Each fixture's channels come
from flamesafe's config, never from a slot number written here: the
group must be named wave flamer, have a safety_profile and aim slots, and
each safety slot s is one 6 channel Showven block: angle s-5 (its aim
slot), speed s-4, ignition s-3, duration s-2, preset s-1 (flamesafe's own
_aim_per_fixture rule). Anything else is refused in a sentence.

STOP, at once, on: letting go (the page's release, or no beat for
BEAT_STALE_S: a closed or hidden page, a lost connection, sign out, whose
beats the web layer refuses), the page's Stop, Abort, a Hold, the rack
screen's Stop, any disarm pressed anywhere, the group disarming by itself,
flamesafe's status stale, an alert of a blocking kind, a lockout of every
fixture, a show starting, live flame programming, any refusal. A stop
puts ignition to 0 first (the angle left where it is), then disarms, and
a stopped run never comes back: hold the button again.
"""
import collections
import json
import os
import threading
import time

GROUP_NAME = "wave flamer"
# The defaults (Andy's design, Jeff's timing, 2026-10-28) and the limits a
# file value must sit within (low, high). arm_wait_s and disarm_wait_s
# bound the waits for flamesafe's own word; disarmed_s must comfortably
# cover the Stream Deck's re-arm refractory window (2 s) and flamesafe's
# min_arm_dwell_ms (1 s), and the sequencer waits for the real armed state
# on every re-arm whatever the timing.
DEFAULTS = {"cycles": 3, "armed_s": 3.0, "disarmed_s": 3.0,
            "arm_wait_s": 8.0, "disarm_wait_s": 2.0, "aim": 128,
            "aim_settle_s": 1.0, "burst_on_s": 1.0, "burst_off_s": 1.0,
            "max_run_min": 10.0, "authorization": "dead_man"}
LIMITS = {"cycles": (1, 10), "armed_s": (1.0, 30.0),
          "disarmed_s": (2.5, 30.0), "arm_wait_s": (3.0, 20.0),
          "disarm_wait_s": (1.0, 5.0), "aim": (118, 138),
          "aim_settle_s": (1.0, 5.0), "burst_on_s": (0.2, 5.0),
          "burst_off_s": (0.5, 10.0), "max_run_min": (1.0, 30.0)}
WHOLE = ("cycles", "aim")
# The angle stays within a few counts of straight up (128): a typo must
# never fire the bursts sideways (safety review of #159, 2). The heads get
# at least 1 s to come up, counted from the first frame actually written
# with the angle up (review of #159, 3): disarmed they sit at angle 0, the
# end stop, so 0 would be heads up and ignition in one frame.
# What authorizes the routine's arms (Jeff, 2026-10-28: one switch).
#   dead_man       the default and the only one that runs: the button is
#                  held the whole time, and each arm is a hold to arm
#                  pressed inside that person's own beats.
#   hold_to_start  one deliberate hold of Start authorizes the whole
#                  routine, which then runs with no finger down. NOT
#                  APPROVED: refused in a sentence until Jeff says yes
#                  (HOLD_TO_START_APPROVED), and not built: its re-arms
#                  would have no person's beats to press the hold with.
MODES = ("dead_man", "hold_to_start")
HOLD_TO_START_APPROVED = False
HOLD_TO_START_REFUSED = (
    "Hold to start (one hold starts the whole cleaning, then it runs with no "
    "finger on the button) is not switched on: Jeff has not approved it. "
    "Set authorization to dead_man in ltcplay_morning.json's cleaning "
    "block, or leave it out, and hold the button for the whole cleaning.")
IGNITION_ON = 255            # a Showven fires at 254 or more on channel 3
UNIVERSE_SIZE = 512
BEAT_STALE_S = 0.25          # the same as flamelink.PURGE_HOLD_S
STATUS_FRESH_S = 0.5         # flamesafe's status, as arm_stale_ms
PAGE_FRESH_S = 1.0           # the page's own status (seen)
WATCH_S = 0.05
DISARM_CONFIRM_S = 1.0       # remote.PLAIN_CONFIRM_S
SETTINGS_EVERY_S = 0.5       # the screen arming switch, read this often
# Open alerts of these kinds stop it and keep it from starting: the flame
# path, the flame link and the Stream Deck (which arms and disarms).
BLOCKING_ALERTS = ("flames", "link", "deck")
DEAD_RUNS = 64
OWNERS_KEPT = 64
CONFIG_FILE = "ltcplay_morning.json"


class CleaningConfigError(ValueError):
    """The cleaning block of ltcplay_morning.json is wrong, in a sentence."""


def parse_config(block, where=CONFIG_FILE):
    """The "cleaning" block over DEFAULTS, every value checked against
    LIMITS. CleaningConfigError, in a sentence, on anything wrong."""
    cfg = dict(DEFAULTS)
    if block is None:
        return cfg
    if not isinstance(block, dict):
        raise CleaningConfigError(f"{where}: cleaning has to be a JSON "
                                  f"object.")
    unknown = sorted(k for k in block if k not in DEFAULTS and k != "notes")
    if unknown:
        raise CleaningConfigError(
            f"{where}: cleaning has a setting this program does not know: "
            f"{', '.join(unknown)}. The settings are "
            f"{', '.join(DEFAULTS)}.")
    if "authorization" in block:
        if block["authorization"] not in MODES:
            raise CleaningConfigError(
                f"{where}: cleaning authorization has to be "
                f"{' or '.join(MODES)}.")
        cfg["authorization"] = block["authorization"]
    for k in LIMITS:
        if k not in block:
            continue
        v = block[k]
        lo, hi = LIMITS[k]
        whole = k in WHOLE
        if isinstance(v, bool) or not isinstance(v, (int, float)) or \
                (whole and not isinstance(v, int)) or v != v:
            raise CleaningConfigError(
                f"{where}: cleaning {k} has to be a "
                f"{'whole number' if whole else 'number'} from {lo:g} to "
                f"{hi:g}.")
        if not lo <= v <= hi:
            raise CleaningConfigError(
                f"{where}: cleaning {k} is {v:g}; it has to be from {lo:g} "
                f"to {hi:g}.")
        cfg[k] = int(v) if whole else float(v)
    return cfg


def load_config(path):
    """(cfg, "") from the "cleaning" block of ltcplay_morning.json (no
    file, or no block: the defaults), or (None, why)."""
    if not path or not os.path.exists(path):
        return dict(DEFAULTS), ""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as e:
        return None, f"{os.path.basename(path)} could not be read ({e})."
    try:
        return parse_config(doc.get("cleaning") if isinstance(doc, dict)
                            else None, os.path.basename(path)), ""
    except CleaningConfigError as e:
        return None, str(e)


def _int_list(v):
    vs = v if isinstance(v, list) else [v]
    if not vs or any(isinstance(x, bool) or not isinstance(x, int)
                     for x in vs):
        return None
    return vs


def find_group(doc):
    """({"name", "index", "safeties", "fixtures"}, "") for flamesafe's
    wave flamer group in its config `doc` (plain JSON: ltcplay never
    imports flamesafe), or (None, why) in a sentence. Each fixture is
    {"safety", "angle", "speed", "ignition", "duration", "preset"}, from
    the fixed 6 channel Showven layout under its safety slot, and only for
    a group with a safety_profile and aim slots that match it exactly."""
    groups = doc.get("groups") if isinstance(doc, dict) else None
    if not isinstance(groups, list) or not groups:
        return None, ("flamesafe's config could not be read, so there is no "
                      "wave flamer to clean.")
    for i, g in enumerate(groups):
        if not isinstance(g, dict) or \
                str(g.get("name", "")).strip().lower() != GROUP_NAME:
            continue
        name = g["name"]
        bad = (f"The wave flamer group in flamesafe's config does not match "
               f"the cleaning's channel layout, so the cleaning will not "
               f"run: ")
        if not isinstance(g.get("safety_profile"), dict) or \
                not isinstance(g.get("aim"), list):
            return None, bad + ("it needs a safety_profile and aim slots "
                                "(the angle channels), as Jeff's file has.")
        saf = _int_list(g.get("safety"))
        fire = _int_list(g.get("fire"))
        aim = _int_list(g.get("aim"))
        if saf is None or fire is None or aim is None:
            return None, bad + "its safety, fire or aim slots are not numbers."
        fixtures = []
        want_fire = set()
        for s in sorted(saf):
            block = list(range(s - 5, s))
            if s - 5 < 1 or s > UNIVERSE_SIZE:
                return None, bad + f"safety slot {s} has no 6 channel block."
            fixtures.append({"safety": s, "angle": s - 5, "speed": s - 4,
                             "ignition": s - 3, "duration": s - 2,
                             "preset": s - 1})
            want_fire.update(block)
        if set(fire) != want_fire or len(fire) != len(want_fire):
            return None, bad + (f"its fire slots {sorted(fire)} are not "
                                f"channels 1 to 5 under each safety slot "
                                f"{sorted(saf)}.")
        if set(aim) != {f["angle"] for f in fixtures} or \
                len(aim) != len(fixtures):
            return None, bad + (f"its aim slots {sorted(aim)} are not channel "
                                f"1 (the angle) of each fixture.")
        return {"name": name, "index": i, "safeties": sorted(saf),
                "fixtures": fixtures}, ""
    return None, ("flamesafe's config has no group named wave flamer, so "
                  "there is nothing to clean.")


def load_group(path):
    """find_group() on flamesafe's config file, or (None, why)."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    except (OSError, ValueError, TypeError) as e:
        return None, (f"flamesafe's config could not be read "
                      f"({type(e).__name__}), so there is no wave flamer to "
                      f"clean.")
    return find_group(doc)


def frame(group, aim, ignition):
    """The 512 slot frame: `aim` on each fixture's angle, `ignition` on
    each fixture's ignition, zero everywhere else (speed, duration and
    preset 0, every safety slot 0: flamesafe writes those from its arm
    state, never from this)."""
    vals = bytearray(UNIVERSE_SIZE)
    for f in group["fixtures"]:
        vals[f["angle"] - 1] = aim
        vals[f["ignition"] - 1] = ignition
    return bytes(vals)


def _fmt_s(v):
    return f"{v:g} s"


class CleaningPanel:
    """The engine side. Pure apart from the providers it is built with:

      link_get()           the flame link, or None
      flames_get()         the remote's FlameStatus view, or None
      blockers()           why it may not run right now, or ""
      locked_slots()       the slots the lockout table holds
      config_get()         (cfg, "") or (None, why)
      press_arm(body, ctx) the engine's arm-hold handler: (code, out)
      release_arm(token)   let go of the hold that device has on the group
      disarm_group(ctx)    the screen's per-group Disarm: (ok, text)
      disarm_all(who, screen)  Disarm all flame groups: (ok, text)
      disarm_count()       how many disarms the engine has handed the deck
      may_disarm_group()   whether a per-group disarm can go now
      burn_flag(on)        tell the fuel tracker a purge frame is a burn
    """

    def __init__(self, group, problem="", link_get=lambda: None,
                 flames_get=lambda: None, blockers=lambda: "",
                 locked_slots=lambda: set(),
                 config_get=lambda: (dict(DEFAULTS), ""),
                 press_arm=None, release_arm=None, disarm_group=None,
                 disarm_all=None, disarm_count=lambda: 0,
                 may_disarm_group=lambda: True, burn_flag=None,
                 journal=None, clock=time.monotonic, threaded=True):
        self.group = group
        self.problem = problem
        self._link = link_get
        self._flames = flames_get
        self._blockers = blockers
        self._locked = locked_slots
        self._config = config_get
        self._press = press_arm
        self._release = release_arm
        self._disarm_group = disarm_group
        self._disarm_all = disarm_all
        self._disarm_count = disarm_count
        self._may_disarm_group = may_disarm_group
        self._burn = burn_flag
        self._journal = journal
        self._clock = clock
        # One lock for everything: a beat, the watcher and a stop never
        # interleave. Views read a snapshot taken under it.
        self._lock = threading.RLock()
        self._run = None
        self._ended = collections.OrderedDict()
        self._confirm = None     # a stop's disarm waiting for flamesafe
        self._aborted = ""       # an Abort landed while the lock was busy
        self.runs = 0
        self.last_cut = ""
        self._stop = threading.Event()
        self._thread = None
        if threaded and group is not None:
            self._thread = threading.Thread(target=self._watch, daemon=True,
                                            name="ltcplay-cleaning-watch")
            self._thread.start()

    # -- plumbing ------------------------------------------------------------
    def _note(self, text, outcome="done", who="", screen="", fault=False):
        if self._journal is None:
            return
        try:
            self._journal(text, outcome=outcome, who=who, screen=screen,
                          fault=fault)
        except Exception:                                # noqa: BLE001
            pass

    @property
    def available(self):
        return self.group is not None

    def _gen(self, link):
        """The flame link's zero count (zero() and disarm_all(): Stop,
        Hold, Abort, a show ending), or None when it cannot be read."""
        try:
            g = getattr(link, "_zero_gen")
            return g if isinstance(g, int) else None
        except Exception:                                # noqa: BLE001
            return None

    def _count(self):
        try:
            n = self._disarm_count()
            return n if isinstance(n, int) else None
        except Exception:                                # noqa: BLE001
            return None

    def _set_burn(self, on):
        if self._burn is None:
            return
        try:
            self._burn(on)
        except Exception:                                # noqa: BLE001
            pass

    def status(self):
        """(state, others, why) from flamesafe: state is "armed",
        "disarmed", "wanted" (asked for, not armed yet: flamesafe holding
        it, say a dwell or a dirty edge), "held" (not disarmed, not armed)
        or "unknown"; others the other groups that are armed or asked
        for; why flamesafe's reason or a sentence."""
        try:
            fl = self._flames() or {}
        except Exception:                                # noqa: BLE001
            fl = {}
        age = fl.get("age_ms")
        if not fl or fl.get("stale") or not fl.get("connected") or \
                not isinstance(age, (int, float)) or \
                age > STATUS_FRESH_S * 1000:
            return "unknown", [], ("flamesafe has not reported in the last "
                                   "half second, so the wave flamer's real "
                                   "state is not known.")
        mine = None
        others = []
        for g in fl.get("groups") or []:
            name = str(g.get("name", ""))
            if self.group is not None and name == self.group["name"]:
                mine = g
            elif g.get("armed") != "disarmed" or g.get("wanted"):
                others.append(name)
        if mine is None:
            return "unknown", others, ("flamesafe's status does not list the "
                                       "wave flamer group.")
        armed = mine.get("armed")
        reason = str(mine.get("reason") or "")
        if armed == "armed":
            return "armed", others, ""
        if armed == "disarmed" and not mine.get("wanted"):
            return "disarmed", others, reason
        if mine.get("wanted"):
            return "wanted", others, reason
        return "held", others, reason or str(armed)

    def _locked_fixtures(self):
        """The fixtures (by safety slot) with any slot locked out, or None
        when the lock state cannot be read."""
        try:
            locked = set(self._locked() or ())
        except Exception:                                # noqa: BLE001
            return None
        out = []
        for f in self.group["fixtures"]:
            slots = {f["safety"], f["angle"], f["speed"], f["ignition"],
                     f["duration"], f["preset"]}
            if slots & locked:
                out.append(f["safety"])
        return out

    def refusal(self, starting=False):
        """Why it may not run (start, or carry on) right now, or ""."""
        if self.group is None:
            return self.problem or "There is no wave flamer group."
        try:
            why = self._blockers() or ""
        except Exception as e:                           # noqa: BLE001
            why = (f"the engine's state could not be read "
                   f"({type(e).__name__}).")
        if why:
            return why
        if self._link() is None:
            return ("No flame link is connected in this engine, so nothing "
                    "could reach the wave flamers.")
        locked = self._locked_fixtures()
        if locked is None:
            return "the lock state could not be read."
        if len(locked) == len(self.group["fixtures"]):
            return ("Every wave flamer is locked out on the Startup page, so "
                    "there is nothing to clean. Unlock one there first.")
        state, others, why = self.status()
        if state == "unknown":
            return why
        if others:
            return (f"{', '.join(others)} {'is' if len(others) == 1 else 'are'}"
                    f" armed or being armed. The cleaning runs with no other "
                    f"flame group armed: disarm it first.")
        if starting and state != "disarmed":
            return ("The wave flamer is armed or being armed. The cleaning "
                    "starts with it disarmed: disarm it, then hold again.")
        return ""

    # -- the routes ------------------------------------------------------------
    def beat(self, body, ctx, seen_ok=True, who="", screen=""):
        """One beat of the cleaning button (every 100 ms while held):
        (code, body). The body names its run (a new one for each press) and
        numbers its beats (seq); "hold" is the JSON word true while the
        finger is down, anything else is a release."""
        body = body if isinstance(body, dict) else {}
        owner = (ctx.device, str(getattr(ctx, "window", "") or "")[:64])
        rid = body.get("run")
        rid = rid if isinstance(rid, str) and 0 < len(rid) <= 64 else None
        seq = body.get("seq")
        seq = seq if isinstance(seq, int) and not isinstance(seq, bool) \
            else None
        now = self._clock()
        if body.get("hold") is not True:
            # A release: ends this screen's run, never refused.
            with self._lock:
                r = self._run
                mine = r is not None and r["owner"] == owner and \
                    (rid is None or rid == r["id"])
                self._remember(owner, rid, seq)
                if mine:
                    self.cut(f"{who or owner[0]} let go of the button",
                             outcome="off")
            return 200, self.view(owner)
        if rid is None:
            return 400, {"error": "A beat that runs the cleaning has to name "
                                  "its run. Nothing was done.", "cut": True}
        with self._lock:
            r = self._run
            if self._aborted and r is not None:
                self.cut(self._aborted, zeroed=True)
                return 409, {"error": f"The cleaning stopped "
                                      f"({self.last_cut}).", "cut": True}
            if r is not None and r["owner"] != owner:
                return 409, {"error": f"{r['owner'][0]} is running the wave "
                                      f"flamer cleaning. One screen at a "
                                      f"time.", "cut": True}
            if self._is_dead(owner, rid, seq, r):
                return 409, {"error": f"The cleaning stopped "
                                      f"({self.last_cut or 'it ended'}). Let "
                                      f"go and hold the button again.",
                             "cut": True}
            if r is not None and r["id"] != rid:
                self.cut("a new press from the same screen while the last "
                         "one was still running")
                return 409, {"error": "The last run was stopped. Let go and "
                                      "hold the button again.", "cut": True}
            if r is not None and seq is not None and r["seq"] is not None \
                    and seq <= r["seq"]:
                # Overtaken by a later beat: says nothing about now.
                return 200, dict(self.view(owner), overtaken=True)
            if r is not None and now - r["beat"] > BEAT_STALE_S:
                self.cut(f"no heartbeat for {BEAT_STALE_S:g} s")
                return 409, {"error": f"The cleaning stopped "
                                      f"({self.last_cut}). Let go and hold "
                                      f"the button again.", "cut": True}
            why = ""
            if not seen_ok:
                why = ("The page's status is more than 1 s old. Wait for it "
                       "to be live, then hold again.")
            if not why:
                why = self.refusal(starting=r is None)
            if why:
                if r is not None:
                    self.cut(f"refused: {why}")
                else:
                    self._note(f"{who or owner[0]}'s wave flamer cleaning on "
                               f"the {owner[0]} was refused: {why}",
                               outcome="refused", who=who, screen=screen)
                return 409, {"error": why, "cut": True}
            if r is None:
                cfg, cwhy = self._config()
                link = self._link()
                gen = self._gen(link)
                count = self._count()
                if cfg is None:
                    return 409, {"error": cwhy, "cut": True}
                if cfg["authorization"] != "dead_man" and \
                        not HOLD_TO_START_APPROVED:
                    return 409, {"error": HOLD_TO_START_REFUSED,
                                 "cut": True}
                if gen is None or count is None:
                    return 409, {"error": "The flame link's or the arm "
                                          "path's state could not be read, "
                                          "so the cleaning does not start.",
                                 "cut": True}
                r = self._run = {
                    "id": rid, "owner": owner, "seq": seq, "beat": now,
                    "start": now, "gen": gen, "disarms": count, "cfg": cfg,
                    "phase": 1, "cycle": 1, "step": "arm", "step_at": now,
                    "hold_id": None, "asked": False, "at": now,
                    "bursts": 0, "fire_on": False,
                    "ctx": ctx, "who": who, "screen": screen,
                    "token": ctx.device, "said": ""}
                self._remember(owner, None, seq, make=True)
                self.runs += 1
                self._set_burn(True)
                self._note(f"{who or owner[0]} started the wave flamer "
                           f"cleaning on the {owner[0]} (holding the "
                           f"button): {cfg['cycles']} times armed "
                           f"{_fmt_s(cfg['armed_s'])} and disarmed "
                           f"{_fmt_s(cfg['disarmed_s'])}, then bursts of "
                           f"{_fmt_s(cfg['burst_on_s'])} on and "
                           f"{_fmt_s(cfg['burst_off_s'])} off until it is "
                           f"stopped.", outcome="started", who=who,
                           screen=screen)
            else:
                r["seq"] = seq
            r["beat"] = now
            r["ctx"] = ctx
            r["seen"] = body.get("seen")
            why = self._step(r, now, press=True)
            if why:
                return 409, {"error": why, "cut": True}
        return 200, self.view(owner)

    def _remember(self, owner, rid, seq, make=False):
        e = self._ended.get(owner)
        if e is None:
            if not make:
                return
            e = self._ended[owner] = {"ids": collections.deque(
                maxlen=DEAD_RUNS), "seq": None}
            while len(self._ended) > OWNERS_KEPT:
                self._ended.popitem(last=False)
        self._ended.move_to_end(owner)
        if rid is not None and rid not in e["ids"]:
            e["ids"].append(rid)
        if seq is not None and (e["seq"] is None or seq > e["seq"]):
            e["seq"] = seq

    def _is_dead(self, owner, rid, seq, live):
        e = self._ended.get(owner)
        if e is None:
            return False
        if rid in e["ids"]:
            return True
        starting = live is None or live["id"] != rid
        return starting and seq is not None and e["seq"] is not None and \
            seq <= e["seq"]

    # -- the sequence ------------------------------------------------------------
    def _to(self, r, step, now, **kw):
        r["step"] = step
        r["step_at"] = now
        r["said"] = ""
        r.update(kw)

    def _press_arm(self, r):
        """One arm-hold beat for this run's person, through the engine's
        own handler. "" or the refusal."""
        if self._press is None:
            return "No hold to arm path is connected in this engine."
        body = {"group": self.group["name"], "seen": r.get("seen")}
        if r["hold_id"] is not None:
            body["hold_id"] = r["hold_id"]
        try:
            code, out = self._press(body, r["ctx"])
        except Exception as e:                           # noqa: BLE001
            return f"the hold to arm could not be made ({type(e).__name__})."
        out = out if isinstance(out, dict) else {}
        if code != 200:
            return str(out.get("error") or f"the hold to arm was refused "
                                               f"({code}).")
        if out.get("hold_id") is not None:
            r["hold_id"] = out["hold_id"]
        if out.get("armed"):
            # The deck set it: the hold is over. Wait for flamesafe's word.
            r["asked"] = True
            r["hold_id"] = None
        return ""

    def _let_go(self, r):
        if r.get("hold_id") is None or self._release is None:
            return
        r["hold_id"] = None
        try:
            self._release(r["token"])
        except Exception:                                # noqa: BLE001
            pass

    def _phase1_disarm(self, r):
        """Phase 1's own disarm: the per-group tap. Its disarm is ours, so
        the count it bumps is not a disarm someone else pressed."""
        if self._disarm_group is None or not self._may_disarm_group():
            return ("the wave flamer's own Disarm cannot go from here "
                    "(screen arming off, or the Stream Deck not reading)")
        try:
            ok, text = self._disarm_group(r["ctx"])
        except Exception as e:                           # noqa: BLE001
            ok, text = False, f"{type(e).__name__}: {e}"
        n = self._count()
        if n is not None:
            r["disarms"] = n
        return "" if ok else (text or "the disarm did not go")

    def _step(self, r, now, press=False):
        """Move the sequence on. `press` is True only inside a beat (a
        person holding): the only time an arm hold is pressed. Returns ""
        or the reason it stopped. Under self._lock."""
        if self._aborted:
            return self._stop_run(self._aborted, zeroed=True)
        link = self._link()
        if link is None or self._gen(link) != r["gen"]:
            return self._stop_run("the flame link was zeroed (Stop, Hold, "
                                  "Abort or a disarm)", zeroed=True)
        if self._count() != r["disarms"]:
            return self._stop_run("a disarm was pressed")
        cfg = r["cfg"]
        if now - r["start"] >= cfg["max_run_min"] * 60:
            return self._stop_run(f"it ran its longest time, "
                                  f"{cfg['max_run_min']:g} min "
                                  f"(max_run_min)")
        state, _o, fs_why = self.status()
        step = r["step"]
        n = cfg["cycles"]
        if step == "arm":
            if state == "armed":
                self._let_go(r)
                if r["phase"] == 1:
                    self._to(r, "armed", now)
                else:
                    # The settle counts from the first frame written with
                    # the angle up (aimed_at), never from this moment.
                    self._to(r, "burst", now, burst_at=now, aimed_at=None)
            elif now - r["step_at"] > cfg["arm_wait_s"]:
                said = r["said"] or (f"flamesafe says: {fs_why}" if fs_why
                                     else "flamesafe never reported it "
                                          "armed")
                return self._stop_run(
                    f"the wave flamer did not arm within "
                    f"{_fmt_s(cfg['arm_wait_s'])} ({said})")
            elif press and not r["asked"]:
                why = self._press_arm(r)
                if why:
                    return self._stop_run(f"the arm was refused: {why}")
            if r["step"] == "arm":
                return ""
            step = r["step"]
        if step == "armed":
            if state != "armed":
                return self._stop_run("the wave flamer is not armed any more "
                                      f"({fs_why or state})")
            if now - r["step_at"] >= cfg["armed_s"]:
                why = self._phase1_disarm(r)
                if why:
                    return self._stop_run(f"the disarm could not go: {why}")
                self._to(r, "disarming", now)
            return ""
        if step == "disarming":
            if state == "disarmed":
                self._to(r, "disarmed", now)
            elif now - r["step_at"] > cfg["disarm_wait_s"]:
                return self._stop_run(
                    f"flamesafe did not report the wave flamer disarmed "
                    f"within {_fmt_s(cfg['disarm_wait_s'])}")
            return ""
        if step == "disarmed":
            if state != "disarmed":
                return self._stop_run("the wave flamer was armed again by "
                                      "something else during the gap")
            if now - r["step_at"] >= cfg["disarmed_s"]:
                if r["cycle"] >= n:
                    self._to(r, "arm", now, phase=2, asked=False,
                             hold_id=None)
                else:
                    self._to(r, "arm", now, cycle=r["cycle"] + 1,
                             asked=False, hold_id=None)
                if press:
                    return self._step(r, now, press=True)
            return ""
        if step == "burst":
            if state != "armed":
                return self._stop_run("the wave flamer is not armed any more "
                                      f"({fs_why or state})")
            if r.get("aimed_at") is None and not press:
                return ""            # only a beat writes the first frame
            if r.get("aimed_at") is None:
                r["aimed_at"] = now  # this beat writes it: angle up, no fire
            on = self._burst_on(r, now)
            if press or (r["fire_on"] and not on):
                # A beat writes the frame; the watcher only ever takes the
                # ignition off on time (never on).
                vals = frame(self.group, cfg["aim"], IGNITION_ON if on else 0)
                link.purge_set(vals)
                r["fire_on"] = on
                if self._gen(link) != r["gen"]:
                    return self._stop_run("the flame link was zeroed (Stop, "
                                          "Hold, Abort or a disarm)",
                                          zeroed=True)
                state, _o, fs_why = self.status()
                if state != "armed":
                    return self._stop_run("the wave flamer is not armed any "
                                          f"more ({fs_why or state})")
            return ""
        return ""

    def _burst_on(self, r, now):
        """Whether the ignition is on at `now`, and the bursts counted."""
        cfg = r["cfg"]
        if r.get("aimed_at") is None:
            return False
        t = now - r["aimed_at"] - cfg["aim_settle_s"]
        if t < 0:
            return False
        period = cfg["burst_on_s"] + cfg["burst_off_s"]
        k = int(t // period)
        r["bursts"] = max(r["bursts"], k + 1)
        return (t - k * period) < cfg["burst_on_s"]

    def _stop_run(self, why, zeroed=False):
        self.cut(why, zeroed=zeroed)
        return why

    def cut(self, why, outcome="stopped", zeroed=False):
        """Stop now: ignition 0 first (the angle left where it is), the
        hold let go, then the disarm. The run can never carry on. True when
        a run was live."""
        with self._lock:
            r, self._run = self._run, None
            self._aborted = ""
            if r is None:
                return False
            self.last_cut = why
            self._remember(r["owner"], r["id"], r["seq"])
            link = self._link()
            if link is not None:
                try:
                    if r.get("aimed_at") is not None and not zeroed:
                        link.purge_set(frame(self.group, r["cfg"]["aim"], 0))
                    else:
                        link.purge_clear()
                except Exception:                        # noqa: BLE001
                    pass
            self._set_burn(False)
            self._let_go(r)
            state, _o, _w = self.status()
            need = r["step"] != "disarmed" or state != "disarmed"
            said = ""
            if need:
                said = self._disarm_after(r)
            held = self._clock() - r["start"]
            self._note(f"Wave flamer cleaning stopped after {held:.1f} s "
                       f"({self._where(r)}): {why}.{said}", outcome=outcome,
                       who=r["who"], screen=r["screen"])
        return True

    def _disarm_after(self, r):
        """A stop's disarm: the per-group tap, confirmed by flamesafe within
        DISARM_CONFIRM_S (tick), else Disarm all flame groups. Under
        self._lock."""
        ok = False
        if self._disarm_group is not None and self._may_disarm_group():
            try:
                ok, _t = self._disarm_group(r["ctx"])
            except Exception:                            # noqa: BLE001
                ok = False
        if ok:
            self._confirm = {"at": self._clock(), "who": r["who"],
                             "screen": r["screen"] or r["owner"][0]}
            return " The wave flamer was sent its Disarm."
        return " " + self._escalate(r["who"], r["screen"] or r["owner"][0],
                                    "its own Disarm could not go")

    def _escalate(self, who, screen, why):
        self._confirm = None
        if self._disarm_all is None:
            return ("Nothing could disarm it from here: disarm on the "
                    "Stream Deck.")
        try:
            code_ok, text = self._disarm_all(who, screen)
        except Exception as e:                           # noqa: BLE001
            code_ok, text = False, f"{type(e).__name__}: {e}"
        self._note(f"Wave flamer cleaning: {why}, so Disarm all flame groups "
                   f"was sent. {text}", outcome="disarm all",
                   who=who, screen=screen, fault=not code_ok)
        return (f"Disarm all flame groups was sent ({why})." if code_ok else
                f"Disarm all flame groups did not go ({text}): disarm on the "
                f"Stream Deck.")

    @staticmethod
    def _where(r):
        if r["phase"] == 1:
            return f"phase 1, {r['step']}, {r['cycle']} of {r['cfg']['cycles']}"
        return f"phase 2, {r['step']}, {r['bursts']} bursts"

    def stop(self, why, who="", screen=""):
        """The page's Stop, from any screen: never refused."""
        self.cut(why)
        return 200, {"ok": True, "text": "The wave flamer cleaning is "
                                         "stopped."}

    def on_abort(self, who="", screen=""):
        """Abort (Morning.on_abort, before the Abort's own flame disarm):
        never waits (review of #159, 4). The purge frame is dropped at once
        and the run marked aborted, so a beat or the watcher holding the
        panel's lock stops at its next look; the stop and its disarm run
        on a thread of their own, after the lock is free."""
        why = f"Abort pressed by {who or screen or 'someone'}"
        if self._run is None:
            return
        self._aborted = why
        link = self._link()
        if link is not None:
            try:
                link.purge_clear()
            except Exception:                            # noqa: BLE001
                pass
        threading.Thread(target=self._abort_cut, args=(why,), daemon=True,
                         name="ltcplay-cleaning-abort").start()

    def _abort_cut(self, why):
        try:
            self.cut(why, zeroed=True)
        except Exception:                                # noqa: BLE001
            pass

    # -- the watcher -------------------------------------------------------------
    def tick(self, now=None):
        """Every WATCH_S: a run with no beat for BEAT_STALE_S, any refusal,
        the flame link zeroed, a disarm pressed or the group disarming is
        stopped; the sequence's own timed steps that only take risk away
        (a disarm, the ignition going off) happen here too. Never arms.
        Then a stop's disarm is checked."""
        now = self._clock() if now is None else now
        why = ""
        with self._lock:
            r = self._run
            if r is not None:
                if now - r["beat"] > BEAT_STALE_S:
                    why = (f"no heartbeat for {BEAT_STALE_S:g} s (the button "
                           f"was let go, the page left or the connection "
                           f"lost)")
                    self.cut(why)
                else:
                    why = self.refusal()
                    if why:
                        self.cut(why)
                    else:
                        why = self._step(r, now, press=False)
            self._check_confirm(now)
        return why

    def _check_confirm(self, now):
        c = self._confirm
        if c is None:
            return
        state, _o, _w = self.status()
        if state == "disarmed":
            self._confirm = None
            return
        if now - c["at"] > DISARM_CONFIRM_S:
            self._escalate(c["who"], c["screen"],
                           f"flamesafe did not report the wave flamer "
                           f"disarmed within {DISARM_CONFIRM_S:g} s")

    def _watch(self):
        while not self._stop.wait(WATCH_S):
            try:
                self.tick()
            except Exception:                            # noqa: BLE001
                try:
                    self.cut("the cleaning watcher raised")
                except Exception:                        # noqa: BLE001
                    pass

    def close(self):
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(1.0)
        self.cut("the engine is stopping")

    # -- views -----------------------------------------------------------------
    def active(self):
        return self._run is not None

    def progress(self, r, now):
        """The page's words for where a run is: phase, cycle n of N, next
        step, bursts, elapsed."""
        cfg = r["cfg"]
        n, c = cfg["cycles"], r["cycle"]
        left = None
        step = r["step"]
        if r["phase"] == 1:
            if step == "arm":
                text = f"Arming, {c} of {n}"
                nxt = f"Armed {_fmt_s(cfg['armed_s'])}"
            elif step == "armed":
                text = f"Armed {_fmt_s(cfg['armed_s'])}, {c} of {n}"
                left = cfg["armed_s"] - (now - r["step_at"])
                nxt = "Disarm"
            elif step == "disarming":
                text = f"Disarming, {c} of {n}"
                nxt = f"Disarmed {_fmt_s(cfg['disarmed_s'])}"
            else:
                text = f"Disarmed {_fmt_s(cfg['disarmed_s'])}, {c} of {n}"
                left = cfg["disarmed_s"] - (now - r["step_at"])
                nxt = (f"Arm again, {c + 1} of {n}" if c < n else
                       "Arm for the bursts")
        elif step == "arm":
            text = "Arming for the bursts"
            nxt = "Point the heads straight up"
        else:
            at = r.get("aimed_at")
            t = -cfg["aim_settle_s"] if at is None else \
                now - at - cfg["aim_settle_s"]
            if t < 0:
                text = "Armed: pointing the heads straight up"
                left = -t
                nxt = "First burst"
            else:
                text = (f"Bursts: {_fmt_s(cfg['burst_on_s'])} on, "
                        f"{_fmt_s(cfg['burst_off_s'])} off")
                nxt = "Let go or press Stop to end"
        return {"phase": r["phase"], "step": step, "cycle": c, "cycles": n,
                "text": text, "next": nxt, "bursts": r["bursts"],
                "fire_on": r["fire_on"],
                "left_s": None if left is None else round(max(0.0, left), 1),
                "elapsed_s": round(now - r["start"], 1)}

    def view(self, owner=None):
        now = self._clock()
        with self._lock:
            r = self._run
            prog = self.progress(r, now) if r is not None else None
            run_owner = r["owner"][0] if r is not None else None
            mine = r is not None and owner is not None and r["owner"] == owner
            run_id = r["id"] if r is not None else None
            cfg = dict(r["cfg"]) if r is not None else None
        cwhy = ""
        if cfg is None:
            cfg, cwhy = self._config()
        out = {"available": self.available, "problem": self.problem,
               "running": prog is not None, "owner": run_owner, "mine": mine,
               "run": run_id if mine else None, "progress": prog,
               "last_cut": self.last_cut, "config_error": cwhy,
               "settings": cfg, "beat_stale_ms": int(BEAT_STALE_S * 1000)}
        if self.group is None:
            return out
        out["group"] = {"name": self.group["name"],
                        "fixtures": [dict(f) for f in
                                     self.group["fixtures"]]}
        state, others, why = self.status()
        out["armed"] = state
        out["armed_why"] = why
        locked = self._locked_fixtures() or []
        out["locked"] = locked
        out["locked_note"] = (
            f"Locked out, stays off: the wave flamer with safety channel"
            f"{'s' if len(locked) > 1 else ''} "
            f"{', '.join(str(s) for s in locked)}."
            if locked and len(locked) < len(self.group["fixtures"]) else "")
        out["blocked"] = "" if prog is not None else \
            (cwhy or self.refusal(starting=True))
        return out


# ------------------------------------------------------------ the engine --

def engine_blockers(remote, link, name, arming=None):
    """Why the engine forbids the cleaning right now, in a sentence, or "":
    the Fireball page's rules (a show live or paused, an Abort standing, a
    Hold, Run pressed, the show clock playing, live flame programming or
    fire while scrubbing, Arrange mode, the lock state not trusted, a purge
    selected or firing, the flame link not sending), the Fireball page
    with a channel on, arming from a screen switched off, the Stream Deck
    not reading the engine, an open alert of a blocking kind, and anyone
    holding to arm another group. Fails closed."""
    from . import fireball as fireball_mod
    try:
        why = fireball_mod.engine_blockers(remote, link, cleaning=False)
        if why:
            return why
        fb = getattr(remote, "fireball", None)
        if fb is not None and fb.active():
            return ("the Fireball page has a channel on; turn it off there "
                    "first.")
        if not (arming or remote.arming_enabled)():
            return ("arming from a screen is switched off on this show "
                    "machine (Programming, Arming from a screen), and the "
                    "cleaning arms the way a screen does.")
        seen = remote._deck_seen
        if seen is None or remote.clock() - seen > remote.DECK_QUIET_S:
            return ("the Stream Deck program has not read this engine in the "
                    "last 2 s, and it is what arms and disarms the wave "
                    "flamer. Look at the Deck chip at the top.")
        alerts = getattr(remote, "alerts", None)
        for a in (alerts.open() if alerts is not None else []):
            if getattr(a, "kind", "") in BLOCKING_ALERTS:
                return (f"an alert is open ({a.kind}): "
                        f"{str(getattr(a, 'text', '')).rstrip('.')}. "
                        f"Deal with it and Acknowledge it first.")
        with remote._arm_lock:
            holds = [h for h in remote._holds.values()
                     if h.get("group") != name and
                     remote.clock() - h["beat"] <= 0.25]
        if holds:
            return (f"{holds[0]['who'] or holds[0]['device']} is holding to "
                    f"arm {holds[0]['group']}. One flame group at a time.")
    except Exception as e:                               # noqa: BLE001
        return f"the engine's state could not be read ({type(e).__name__})."
    return ""


def build(remote, morning, link, journal=None, threaded=True,
          clock=time.monotonic, fuel=None):
    """The engine's panel (web.serve): the wave flamer group from
    flamesafe's config (the Startup page's file), the cleaning block of
    ltcplay_morning.json, the Startup page's lockout, flamesafe's status
    from the remote, the engine's hold to arm and disarm handlers, and the
    flame link."""
    path = getattr(morning, "flamesafe_config", None)
    if path:
        group, problem = load_group(path)
    else:
        group, problem = None, ("flamesafe's config is not known to this "
                                "engine, so there is no wave flamer to "
                                "clean.")

    def locked():
        return morning.lockout.channels(morning.cfg) | \
            set(morning.lockout.file_channels or ())

    def flames():
        fs = getattr(remote, "flame_status", None)
        return fs.view() if fs is not None else None

    name = group["name"] if group is not None else ""

    def release(token):
        try:
            i, _n = remote._group_index({"group": name})
        except ValueError:
            return
        remote._drop_hold(i, token)

    def disarm_group(ctx):
        code, out = remote.group_disarm({"group": name}, ctx)
        return code == 200, str(out.get("text") or out.get("error") or "")

    def disarm_all(who, screen):
        code, out = remote.disarm_all(who, screen)
        return code == 200, str(out.get("text") or out.get("error") or "")

    # The settings file read at most every SETTINGS_EVERY_S, never on
    # every tick (review of #159, 4).
    cache = {"at": None, "on": False}

    def arming():
        now = remote.clock()
        if cache["at"] is None or not 0 <= now - cache["at"] < \
                SETTINGS_EVERY_S:
            cache["on"] = bool(remote.arming_enabled())
            cache["at"] = now
        return cache["on"]

    def may_group():
        seen = remote._deck_seen
        return arming() and seen is not None and \
            remote.clock() - seen <= 0.5

    def burn(on):
        if fuel is not None:
            fuel.purge_is_burn = bool(on)

    panel = CleaningPanel(
        group, problem, link_get=lambda: link, flames_get=flames,
        blockers=lambda: engine_blockers(remote, link, name, arming),
        locked_slots=locked,
        config_get=lambda: load_config(morning.config_path),
        press_arm=remote.arm_hold, release_arm=release,
        disarm_group=disarm_group, disarm_all=disarm_all,
        disarm_count=lambda: remote._disarm_ids, may_disarm_group=may_group,
        burn_flag=burn, journal=journal, clock=clock, threaded=threaded)
    # The Startup page's purge waits while it runs (one source of purge
    # frames at a time), and an Abort stops it.
    before = morning.purge_blocker

    def purge_blocker():
        why = before() or ""
        if why:
            return why
        return ("the wave flamer cleaning is running; stop it first."
                if panel.active() else "")
    morning.purge_blocker = purge_blocker
    morning.abort_hooks.append(panel.on_abort)
    return panel


def _owner(ctx):
    return (ctx.device, str(getattr(ctx, "window", "") or "")[:64])


def route_get(panel, ctx, wall=time.time):
    """GET /api/remote/cleaning: anyone may read it."""
    if panel is None:
        return 404, {"error": "The wave flamer cleaning is not set up in "
                              "this engine (it needs the Fire & Ice engine "
                              "and its flame link)."}
    v = panel.view(_owner(ctx))
    v["served_at"] = int(wall() * 1000)
    return 200, v


def route_post(panel, remote, name, body, ctx):
    """POST cleaning-beat {"hold": true, "run": id, "seq": n, "seen": ms}
    and cleaning-stop. (code, body)."""
    if panel is None:
        return 404, {"error": "The wave flamer cleaning is not set up in "
                              "this engine."}
    body = body if isinstance(body, dict) else {}
    who, screen, _s = remote._actor(ctx, body)
    if name == "cleaning-stop":
        return panel.stop(f"{who or 'someone'} pressed Stop on the "
                          f"{ctx.device}", who=who, screen=screen)
    if name == "cleaning-beat":
        if body.get("hold") is True and remote.users_active() and \
                not ctx.user:
            # Belt: the web layer refuses this for nobody signed in first.
            return 403, {"error": "Signed out: this screen is view only. "
                                  "Sign in to run the cleaning.",
                         "cut": True}
        try:
            seen = float(body.get("seen"))
            seen_ok = abs(remote.wall() - seen / 1000.0) <= PAGE_FRESH_S
        except (TypeError, ValueError):
            seen_ok = False
        return panel.beat(body, ctx, seen_ok=seen_ok, who=who, screen=screen)
    return 404, {"error": "no such thing here"}
