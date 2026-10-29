"""Wave flamer cleaning: the nightly shutdown routine on the Shutdown part
of the Morning and Shutdown page (Andy's request via Jeff, 2026-10-28;
Jeff's ruling on PR #159 the same day). MORNING.md, "Wave flamer
cleaning", and OPERATOR.md say it in plain words.

What it does (the defaults; ltcplay_morning.json "cleaning" may change
each within the limits in LIMITS)
==================================================================

  Start: one deliberate HOLD of the button, the same hold as a group's
  Hold to arm (the person's beats press the hold, and the Stream Deck arms
  only after its full hold time). Let go before the hold completes and
  nothing starts. Once it completes (the deck took the hold) the person
  may let go: phase 1 runs on its own.
  Phase 1, pressurize and depressurize the pump, NO fire: three times,
  armed 3 s (from flamesafe's ARMED report), disarm, disarmed 3 s (from
  flamesafe's disarmed report). Only the wave flamers' safety slots move:
  no frame is set at all in this phase.
  Pause: the routine ends disarmed and waits. Nothing continues by itself.
  Another fresh deliberate hold of the button ("Cycles done. Hold to start
  the purge bursts"), the same kind of hold, is the go-ahead and is itself
  the arm press for phase 2. A release before it completes leaves it
  paused. Nobody holds it: after pause_max_min it goes back to idle.
  Phase 2, fire the leftover fuel to clear it: once flamesafe reports
  ARMED, every head straight up (the angle channel, an aim slot, at 128;
  speed, duration and preset 0), at least aim_settle_s counted from the
  first frame written with the heads up, then the ignition (channel 3)
  255 for 1 s, 0 for 1 s, over and over until Stop or Abort (or
  max_burst_min). No finger needs to stay on it.

How it arms (no new way to arm)
===============================

Every arm is the engine's own hold to arm handler (remote.Remote.arm_hold),
the one the rack screen's Hold to arm button posts to, with all its rules,
and the Stream Deck presses the group's key exactly as for a held screen
button: only after SCREEN_HOLD_S of beat-evidenced holding, with its re-arm
refractory window, the latch and the operator gate; flamesafe applies
min_arm_dwell_ms, the clean rising edge and the edge-quiet window.

  - Phase 1's first arm and phase 2's arm: the holding person's own beats
    call arm_hold, inside their request, with their device, window,
    sign-in and page status time.
  - Phase 1's second and third arms: the walk-away Jeff approved for
    phase 1 only (WALK_AWAY_PHASE1). The watcher calls arm_hold on behalf
    of the person whose completed hold started the routine, with that
    request's context and the engine's own time, every WATCH_S until the
    deck takes it. Never in phase 2, never before a completed start hold,
    never while paused.

Disarming (phase 1's, and every stop) is the screen's own per-group
Disarm (remote.Remote.group_disarm); a stop's disarm that flamesafe has
not confirmed within DISARM_CONFIRM_S gets Disarm all flame groups.

The fire values
===============

Phase 2 only, after the go-ahead hold, once flamesafe reports ARMED: the
Startup page's purge frame on the engine's flame link (FlameLink.purge_set),
like the Fireball page, written by the watcher every WATCH_S (the link
drops it by itself PURGE_HOLD_S after the last write). flamesafe stays the
only writer of the flame universe. Each fixture's channels come from
flamesafe's config: the group named wave flamer, with a safety_profile and
aim slots, each safety slot s one 6 channel Showven block: angle s-5,
speed s-4, ignition s-3, duration s-2, preset s-1.

STOP, at once, ignition 0 first and then the disarm, from any state: Stop
(this page's on any screen, the rack screen's, the Stream Deck's: the
deck reads the cleaning as running so its key is Stop), Abort, Hold, any
disarm anywhere, the group disarming by itself, flamesafe's status stale,
the flame link zeroed, a wave flamer locked or unlocked, a show starting,
live flame programming, another group armed, an alert of a blocking kind,
screen arming switched off, the Stream Deck not reading, the person who
started it (phase 1) or gave the go-ahead (phase 2) signing out, any
refusal, a bound running out. A stopped run never comes back.
"""
import collections
import json
import os
import threading
import time

GROUP_NAME = "wave flamer"
# The defaults (Andy's design, Jeff's timing, 2026-10-28) and the limits a
# file value must sit within (low, high). disarmed_s must comfortably
# cover the Stream Deck's re-arm refractory window (2 s) and flamesafe's
# min_arm_dwell_ms (1 s); the sequencer waits for the real state anyway.
DEFAULTS = {"cycles": 3, "armed_s": 3.0, "disarmed_s": 3.0,
            "arm_wait_s": 8.0, "disarm_wait_s": 2.0, "aim": 128,
            "aim_settle_s": 1.0, "burst_on_s": 1.0, "burst_off_s": 1.0,
            "max_burst_min": 10.0, "pause_max_min": 10.0}
LIMITS = {"cycles": (1, 10), "armed_s": (1.0, 30.0),
          "disarmed_s": (2.5, 30.0), "arm_wait_s": (3.0, 20.0),
          "disarm_wait_s": (1.0, 5.0), "aim": (118, 138),
          "aim_settle_s": (1.0, 5.0), "burst_on_s": (0.2, 5.0),
          "burst_off_s": (0.5, 10.0), "max_burst_min": (1.0, 30.0),
          "pause_max_min": (1.0, 60.0)}
WHOLE = ("cycles", "aim")
# The angle stays within a few counts of straight up (128): a typo must
# never fire the bursts sideways (safety review of #159, 2). The heads get
# at least 1 s to come up, counted from the first frame actually written
# with the angle up (review of #159, 3): disarmed they sit at angle 0, the
# end stop, so 0 would be heads up and ignition in one frame.
#
# Jeff's ruling on #159 (2026-10-28): after one completed hold, phase 1
# (arming only, no fire) runs with nobody holding; phase 2 (fire) needs a
# second completed hold, and then runs until Stop. Phase 1's walk-away is
# the only arm the engine presses on a person's behalf.
WALK_AWAY_PHASE1 = True
# A hold is credited only when the Stream Deck says it took THAT hold
# (arm_hold's "taken"). The group armed or asked for while a hold is not
# credited: the deck's word on a hold it took follows within
# remote.TAKEN_WAIT_S (0.5 s); past this, the run stops (review of #159).
FOREIGN_GRACE_S = 1.0
ARMED_ELSEWHERE = ("the wave flamer was armed or asked for some other way "
                   "during the hold (a Stream Deck key, another screen), "
                   "not by this hold, so it does not count")
IGNITION_ON = 255            # a Showven fires at 254 or more on channel 3
UNIVERSE_SIZE = 512
BEAT_STALE_S = 0.25          # a hold with no beat for this long let go
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
# The states a view reports (the page's words come from them).
STATES = ("idle", "starting", "phase1", "paused", "acking", "phase2")


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
      session_live(ctx)    whether that request's sign-in still stands
      burn_flag(on)        tell the fuel tracker a purge frame is a burn
      wall()               the engine's wall clock (a walk-away press's
                           page time)
    """

    def __init__(self, group, problem="", link_get=lambda: None,
                 flames_get=lambda: None, blockers=lambda: "",
                 locked_slots=lambda: set(),
                 config_get=lambda: (dict(DEFAULTS), ""),
                 press_arm=None, release_arm=None, disarm_group=None,
                 disarm_all=None, disarm_count=lambda: 0,
                 may_disarm_group=lambda: True,
                 session_live=lambda ctx: True, burn_flag=None,
                 journal=None, clock=time.monotonic, wall=time.time,
                 threaded=True):
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
        self._session_live = session_live
        self._burn = burn_flag
        self._journal = journal
        self._clock = clock
        self._wall = wall
        # One lock for everything: a beat, the watcher and a stop never
        # interleave. Abort never waits on it (on_abort).
        self._lock = threading.RLock()
        self._run = None
        self._ended = collections.OrderedDict()
        self._confirm = None     # a stop's disarm waiting for flamesafe
        self._aborted = ""       # an Abort landed while the lock was busy
        self.runs = 0
        self.last_cut = ""
        self.last_note = ""      # a go-ahead hold that did not complete
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
        """Why it may not start a hold (`starting`) or carry on, or ""."""
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
        """One beat of a person's hold on the button (every 100 ms while
        the finger is down): the start hold from idle, or the go-ahead
        hold while paused. (code, body). The body names the hold (a new
        id for each press) and numbers its beats (seq); "hold" is the JSON
        word true while the finger is down, anything else is a release."""
        body = body if isinstance(body, dict) else {}
        owner = (ctx.device, str(getattr(ctx, "window", "") or "")[:64])
        rid = body.get("run")
        rid = rid if isinstance(rid, str) and 0 < len(rid) <= 64 else None
        seq = body.get("seq")
        seq = seq if isinstance(seq, int) and not isinstance(seq, bool) \
            else None
        now = self._clock()
        with self._lock:
            r = self._run
            h = r.get("hold") if r is not None else None
            if body.get("hold") is not True:
                # A release: never refused. Before the hold completed it
                # starts nothing (or leaves the routine paused); after, it
                # changes nothing: the routine runs on its own.
                self._remember(owner, rid, seq)
                if h is not None and h["owner"] == owner and \
                        (rid is None or rid == h["id"]):
                    self._hold_ended(r, f"{who or owner[0]} let go before the "
                                        f"hold was complete")
                return 200, self.view(owner)
            if rid is None:
                return 400, {"error": "A hold has to name itself. Nothing "
                                      "was done.", "cut": True}
            if self._aborted and r is not None:
                self.cut(self._aborted, zeroed=True)
                return 409, {"error": f"The cleaning stopped "
                                      f"({self.last_cut}).", "cut": True}
            if r is not None and h is None and r["step"] != "paused":
                if rid == r.get("done_hold") and \
                        owner == r.get("done_owner"):
                    # The finger is still down after its hold completed:
                    # nothing to do, the routine runs on its own.
                    return 200, self.view(owner)
                return 409, {"error": "The cleaning is already running on its "
                                      "own. Press Stop to end it.",
                             "cut": True, "running": True}
            if h is not None and h["owner"] != owner:
                return 409, {"error": f"{h['owner'][0]} is holding the "
                                      f"cleaning button. One screen at a "
                                      f"time.", "cut": True}
            if self._is_dead(owner, rid, seq, h):
                return 409, {"error": "That hold has ended. Let go and hold "
                                      "the button again.", "cut": True}
            if h is not None and h["id"] != rid:
                self._hold_ended(r, "a new press from the same screen")
                return 409, {"error": "Let go and hold the button again.",
                             "cut": True}
            if h is not None and seq is not None and h["seq"] is not None \
                    and seq <= h["seq"]:
                # Overtaken by a later beat: says nothing about now.
                return 200, dict(self.view(owner), overtaken=True)
            if h is not None and now - h["beat"] > BEAT_STALE_S:
                self._hold_ended(r, f"no beat for {BEAT_STALE_S:g} s")
                return 409, {"error": "The hold was interrupted. Let go and "
                                      "hold again.", "cut": True}
            why = ""
            if not seen_ok:
                why = ("The page's status is more than 1 s old. Wait for it "
                       "to be live, then hold again.")
            if not why:
                why = self.refusal(starting=h is None)
            if why:
                if h is not None:
                    self._hold_ended(r, f"refused: {why}")
                elif r is None:
                    self._note(f"{who or owner[0]}'s wave flamer cleaning on "
                               f"the {owner[0]} was refused: {why}",
                               outcome="refused", who=who, screen=screen)
                return 409, {"error": why, "cut": True}
            hold = {"id": rid, "owner": owner, "seq": seq, "beat": now,
                    "start": now, "ctx": ctx, "seen": body.get("seen"),
                    "who": who, "screen": screen}
            if r is None:
                cfg, cwhy = self._config()
                link = self._link()
                gen = self._gen(link)
                count = self._count()
                if cfg is None:
                    return 409, {"error": cwhy, "cut": True}
                if gen is None or count is None:
                    return 409, {"error": "The flame link's or the arm "
                                          "path's state could not be read, "
                                          "so the cleaning does not start.",
                                 "cut": True}
                r = self._run = {
                    "owner": owner, "start": now, "gen": gen,
                    "disarms": count, "cfg": cfg, "phase": 1, "cycle": 1,
                    "step": "arm", "step_at": now, "hold_id": None,
                    "asked": False, "authorized": False, "bursts": 0,
                    "fire_on": False, "aimed_at": None, "hold": hold,
                    "ctx": ctx, "who": who, "screen": screen,
                    "locked": self._locked_fixtures()}
                self._remember(owner, None, seq, make=True)
                self.runs += 1
                self.last_note = ""
                self._set_burn(True)
                self._note(f"{who or owner[0]} is holding to start the wave "
                           f"flamer cleaning on the {owner[0]}: phase 1 "
                           f"(pressurize and depressurize, no fire) runs on "
                           f"its own once the hold is complete.",
                           outcome="hold", who=who, screen=screen)
            elif h is None:
                # Paused: this hold is the go-ahead, and phase 2's arm.
                self._to(r, "arm", now, phase=2, hold=hold, asked=False,
                         hold_id=None, ctx=ctx, who=who, screen=screen,
                         owner=owner)
                self._remember(owner, None, seq, make=True)
                self.last_note = ""
                self._note(f"{who or owner[0]} is holding for the go-ahead "
                           f"to fire the purge bursts on the {owner[0]}.",
                           outcome="hold", who=who, screen=screen)
            else:
                h.update(seq=seq, beat=now, ctx=ctx, seen=body.get("seen"))
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

    # -- holds ------------------------------------------------------------------
    def _hold_ended(self, r, why):
        """A person's hold ended before it was complete (let go, no beat,
        refused). Phase 1's start hold: nothing starts (the run ends, and
        the group is disarmed if the deck took it at the last moment).
        Phase 2's go-ahead hold: back to paused, or, if the group was
        armed or asked for meanwhile, the run stops disarmed. Under the
        lock."""
        h = r.get("hold")
        if h is None:
            return
        self._remember(h["owner"], h["id"], h["seq"])
        self._let_go(r, h["ctx"].device)
        r["hold"] = None
        if r["phase"] == 1:
            self.cut(f"{why}; nothing started", outcome="not started")
            return
        state, _o, _w = self.status()
        if state != "disarmed":
            # Armed or asked for during a go-ahead that never completed:
            # not by this hold. The run stops, disarmed (fix 1).
            self.cut(f"{why}; the wave flamer was armed or asked for "
                     f"during it, not by this hold")
            return
        self._to(r, "paused", r.get("paused_at", self._clock()),
                 asked=False, hold_id=None)
        self.last_note = (f"The go-ahead hold ended before it was complete "
                          f"({why}). Still paused: hold the button again to "
                          f"start the purge bursts.")
        self._note(f"Wave flamer cleaning: {self.last_note}",
                   outcome="still paused", who=h["who"], screen=h["screen"])

    def _press_arm(self, r, ctx, seen):
        """One arm-hold beat through the engine's own handler. "" or the
        refusal."""
        if self._press is None:
            return "No hold to arm path is connected in this engine."
        body = {"group": self.group["name"], "seen": seen}
        if r["hold_id"] is not None:
            body["hold_id"] = r["hold_id"]
        try:
            code, out = self._press(body, ctx)
        except Exception as e:                           # noqa: BLE001
            return f"the hold to arm could not be made ({type(e).__name__})."
        out = out if isinstance(out, dict) else {}
        if code != 200:
            return str(out.get("error") or f"the hold to arm was refused "
                                               f"({code}).")
        if out.get("hold_id") is not None:
            r["hold_id"] = out["hold_id"]
        if out.get("armed"):
            r["hold_id"] = None
            if out.get("taken") is not True:
                # Armed or asked for, but the deck did not say THIS hold
                # did it (a deck key, another screen): never credited.
                return ARMED_ELSEWHERE
            # The deck took this hold: complete. Wait for flamesafe.
            r["asked"] = True
        return ""

    def _let_go(self, r, token):
        if r.get("hold_id") is None or self._release is None:
            return
        r["hold_id"] = None
        try:
            self._release(token)
        except Exception:                                # noqa: BLE001
            pass

    def _phase_disarm(self, r):
        """The routine's own per-group disarm: its disarm is ours, so the
        count it bumps is not a disarm someone else pressed. "" or why it
        could not go."""
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

    # -- the sequence ------------------------------------------------------------
    def _to(self, r, step, now, **kw):
        r["step"] = step
        r["step_at"] = now
        r.update(kw)

    def _step(self, r, now, press=False):
        """Move the routine on. `press` is True only inside a person's hold
        beat. Returns "" or the reason it stopped. Under self._lock."""
        if self._aborted:
            return self._stop_run(self._aborted, zeroed=True)
        link = self._link()
        if link is None or self._gen(link) != r["gen"]:
            return self._stop_run("the flame link was zeroed (Stop, Hold, "
                                  "Abort or a disarm)", zeroed=True)
        if self._count() != r["disarms"]:
            return self._stop_run("a disarm was pressed")
        if self._locked_fixtures() != r["locked"]:
            return self._stop_run("a wave flamer was locked or unlocked on "
                                  "the Startup page")
        cfg = r["cfg"]
        if r["hold"] is None and r["step"] != "paused":
            try:
                live = bool(self._session_live(r["ctx"]))
            except Exception:                            # noqa: BLE001
                live = False
            if not live:
                return self._stop_run(f"{r['who'] or 'the person running it'}"
                                      f" signed out")
        state, _o, fs_why = self.status()
        step = r["step"]
        n = cfg["cycles"]
        if step == "arm":
            h = r["hold"]
            if r["asked"]:
                # The deck confirmed it took THIS hold: wait for ARMED.
                if state == "armed":
                    self._let_go(r, r["ctx"].device)
                    if r["phase"] == 1:
                        self._to(r, "armed", now)
                    else:
                        # The first frame goes on the next step, so the
                        # settle counts from a frame actually written.
                        self._to(r, "burst", now, burst_at=now,
                                 aimed_at=None)
                elif now - r["step_at"] > cfg["arm_wait_s"]:
                    said = (f"flamesafe says: {fs_why}" if fs_why
                            else "flamesafe never reported it armed")
                    return self._stop_run(
                        f"the wave flamer did not arm within "
                        f"{_fmt_s(cfg['arm_wait_s'])} ({said})")
                return ""
            # Not credited yet. Armed or asked for by anything but this
            # hold (review of #159, fix 1: a deck key during a short
            # hold) never counts: past the deck's own word on a hold it
            # took (TAKEN_WAIT_S), the run stops and disarms.
            if state in ("armed", "wanted"):
                r.setdefault("foreign_at", now)
                if now - r["foreign_at"] > FOREIGN_GRACE_S:
                    return self._stop_run(ARMED_ELSEWHERE)
            else:
                r.pop("foreign_at", None)
            if now - r["step_at"] > cfg["arm_wait_s"]:
                said = (f"flamesafe says: {fs_why}" if fs_why
                        else "flamesafe never reported it armed")
                return self._stop_run(
                    f"the wave flamer did not arm within "
                    f"{_fmt_s(cfg['arm_wait_s'])} ({said})")
            if h is not None:
                if not press:
                    return ""
                why = self._press_arm(r, h["ctx"], h["seen"])
                if why == ARMED_ELSEWHERE:
                    return self._stop_run(why)
                if why:
                    self._hold_ended(r, f"the arm was refused: {why}")
                    return f"the arm was refused: {why}"
                if r["asked"]:
                    # The deck took this hold: complete. The person may
                    # let go.
                    r["hold"] = None
                    r["done_hold"] = h["id"]
                    r["done_owner"] = h["owner"]
                    r["authorized"] = True
                    self._note(
                        f"Wave flamer cleaning: {h['who'] or 'the'} hold on "
                        f"the {h['owner'][0]} is complete; "
                        f"{'phase 1 runs on its own' if r['phase'] == 1 else 'the purge bursts start once armed'}.",
                        outcome="hold complete", who=h["who"],
                        screen=h["screen"])
                return ""
            if r["phase"] != 1 or not WALK_AWAY_PHASE1 or \
                    not r["authorized"]:
                return self._stop_run("an arm with nobody's hold behind it")
            # Phase 1's walk-away (Jeff's ruling): the hold to arm pressed
            # for the person whose completed hold started it.
            why = self._press_arm(r, r["ctx"], int(self._wall() * 1000))
            if why:
                return self._stop_run(why if why == ARMED_ELSEWHERE else
                                      f"the arm was refused: {why}")
            return ""
        if step == "armed":
            if state != "armed":
                return self._stop_run("the wave flamer is not armed any more "
                                      f"({fs_why or state})")
            if now - r["step_at"] >= cfg["armed_s"]:
                why = self._phase_disarm(r)
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
                    self._to(r, "paused", now, paused_at=now, asked=False,
                             hold_id=None)
                    self._note("Wave flamer cleaning: the cycles are done; "
                               "disarmed, waiting for a hold to start the "
                               "purge bursts.", outcome="paused",
                               who=r["who"], screen=r["screen"])
                else:
                    self._to(r, "arm", now, cycle=r["cycle"] + 1,
                             asked=False, hold_id=None)
            return ""
        if step == "paused":
            if state != "disarmed":
                return self._stop_run("the wave flamer was armed while the "
                                      "cleaning was paused")
            if now - r["paused_at"] >= cfg["pause_max_min"] * 60:
                return self._stop_run(
                    f"nobody gave the go-ahead within "
                    f"{cfg['pause_max_min']:g} min, so it went back to idle")
            return ""
        if step == "burst":
            if state != "armed":
                return self._stop_run("the wave flamer is not armed any more "
                                      f"({fs_why or state})")
            if now - r["step_at"] >= cfg["max_burst_min"] * 60:
                return self._stop_run(f"the purge bursts ran their longest "
                                      f"time, {cfg['max_burst_min']:g} min "
                                      f"(max_burst_min)")
            if r["aimed_at"] is None:
                r["aimed_at"] = now   # this write: the heads up, no fire
            on = self._burst_on(r, now)
            vals = frame(self.group, cfg["aim"], IGNITION_ON if on else 0)
            link.purge_set(vals)
            r["fire_on"] = on
            if self._gen(link) != r["gen"]:
                return self._stop_run("the flame link was zeroed (Stop, Hold, "
                                      "Abort or a disarm)", zeroed=True)
            state, _o, fs_why = self.status()
            if state != "armed":
                return self._stop_run("the wave flamer is not armed any more "
                                      f"({fs_why or state})")
            return ""
        return ""

    def _burst_on(self, r, now):
        """Whether the ignition is on at `now`, and the bursts counted.
        Off until aim_settle_s after the first frame written with the
        heads up."""
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
        """Stop now: ignition 0 first (the angle left where it is), any
        hold let go, then the disarm. The run can never carry on. True when
        a run was live."""
        with self._lock:
            r, self._run = self._run, None
            self._aborted = ""
            if r is None:
                return False
            self.last_cut = why
            h = r.get("hold")
            if h is not None:
                self._remember(h["owner"], h["id"], h["seq"])
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
            self._let_go(r, (h["ctx"] if h is not None else r["ctx"]).device)
            state, _o, _w = self.status()
            need = r["step"] not in ("disarmed", "paused") or \
                state != "disarmed"
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
        if r["step"] == "paused":
            return "paused after the cycles"
        if r["phase"] == 1:
            return f"phase 1, {r['step']}, {r['cycle']} of {r['cfg']['cycles']}"
        return f"phase 2, {r['step']}, {r['bursts']} bursts"

    def stop(self, why, who="", screen=""):
        """Stop, from any screen, the rack screen's Stop and the Stream
        Deck's: never refused."""
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
        """Every WATCH_S: a person's hold with no beat for BEAT_STALE_S
        ends; any refusal, the flame link zeroed, a disarm pressed, a
        lockout change, a sign-out or the group disarming stops the
        routine; the timed steps run (phase 1's walk-away arms included,
        and phase 2's frames). Then a stop's disarm is checked."""
        now = self._clock() if now is None else now
        why = ""
        with self._lock:
            r = self._run
            if r is not None:
                h = r.get("hold")
                if h is not None and now - h["beat"] > BEAT_STALE_S:
                    why = (f"no beat for {BEAT_STALE_S:g} s (the button was "
                           f"let go, the page left or the connection lost)")
                    self._hold_ended(r, why)
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

    @staticmethod
    def state_of(r):
        if r is None:
            return "idle"
        if r.get("hold") is not None:
            return "starting" if r["phase"] == 1 else "acking"
        if r["step"] == "paused":
            return "paused"
        return "phase1" if r["phase"] == 1 else "phase2"

    def progress(self, r, now):
        """The page's words for where a run is: state, phase, cycle n of N,
        the next step, bursts, seconds left, elapsed."""
        cfg = r["cfg"]
        n, c = cfg["cycles"], r["cycle"]
        left = None
        step = r["step"]
        st = self.state_of(r)
        if st == "starting":
            text = "Keep holding: arming, 1 of " + str(n)
            nxt = "Let go once it says phase 1 is running"
        elif st == "acking":
            text = "Keep holding: arming for the purge bursts"
            nxt = "Let go once it says the bursts are starting"
        elif st == "paused":
            text = "Cycles done. Hold to start the purge bursts"
            left = cfg["pause_max_min"] * 60 - (now - r["paused_at"])
            nxt = "Hold the button to fire the leftover fuel, or Stop"
        elif r["phase"] == 1:
            if step == "arm":
                text = f"Pressurizing: arming, {c} of {n}"
                nxt = f"Armed {_fmt_s(cfg['armed_s'])}"
            elif step == "armed":
                text = f"Pressurizing: armed {_fmt_s(cfg['armed_s'])}, {c} of {n}"
                left = cfg["armed_s"] - (now - r["step_at"])
                nxt = "Disarm"
            elif step == "disarming":
                text = f"Depressurizing: disarming, {c} of {n}"
                nxt = f"Disarmed {_fmt_s(cfg['disarmed_s'])}"
            else:
                text = (f"Depressurizing: disarmed "
                        f"{_fmt_s(cfg['disarmed_s'])}, {c} of {n}")
                left = cfg["disarmed_s"] - (now - r["step_at"])
                nxt = (f"Arm again, {c + 1} of {n}" if c < n else
                       "Pause and wait for a hold to start the purge bursts")
        elif step == "arm":
            text = "Arming for the purge bursts"
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
                text = (f"Purge bursts: {_fmt_s(cfg['burst_on_s'])} on, "
                        f"{_fmt_s(cfg['burst_off_s'])} off")
                nxt = "Press Stop to end"
        return {"state": st, "phase": 1 if st in ("starting", "phase1",
                                                  "paused") else 2,
                "step": step, "cycle": c, "cycles": n,
                "text": text, "next": nxt, "bursts": r["bursts"],
                "fire_on": r["fire_on"],
                "left_s": None if left is None else round(max(0.0, left), 1),
                "elapsed_s": round(now - r["start"], 1)}

    def summary(self):
        """For every screen's status (the banner, and the Stream Deck's
        word that something is running)."""
        r = self._run
        if r is None:
            return {"available": self.available, "state": "idle", "text": ""}
        try:
            p = self.progress(r, self._clock())
        except Exception:                                # noqa: BLE001
            return {"available": self.available, "state": "phase1",
                    "text": "Running"}
        return {"available": self.available, "state": p["state"],
                "text": p["text"]}

    def view(self, owner=None):
        now = self._clock()
        with self._lock:
            r = self._run
            prog = self.progress(r, now) if r is not None else None
            h = r.get("hold") if r is not None else None
            holder = h["owner"][0] if h is not None else None
            mine = h is not None and owner is not None and h["owner"] == owner
            hold_id = h["id"] if mine else None
            cfg = dict(r["cfg"]) if r is not None else None
            state = self.state_of(r)
        cwhy = ""
        if cfg is None:
            cfg, cwhy = self._config()
        out = {"available": self.available, "problem": self.problem,
               "state": state, "running": state != "idle",
               "holder": holder, "mine": mine, "run": hold_id,
               "progress": prog, "last_cut": self.last_cut,
               "last_note": self.last_note, "config_error": cwhy,
               "settings": cfg, "beat_stale_ms": int(BEAT_STALE_S * 1000)}
        if self.group is None:
            return out
        out["group"] = {"name": self.group["name"],
                        "fixtures": [dict(f) for f in
                                     self.group["fixtures"]]}
        st, _others, why = self.status()
        out["armed"] = st
        out["armed_why"] = why
        locked = self._locked_fixtures() or []
        out["locked"] = locked
        out["locked_note"] = (
            f"Locked out, stays off: the wave flamer with safety channel"
            f"{'s' if len(locked) > 1 else ''} "
            f"{', '.join(str(s) for s in locked)}."
            if locked and len(locked) < len(self.group["fixtures"]) else "")
        # Whether a hold may start now (from idle, or the go-ahead).
        out["blocked"] = (cwhy or self.refusal(starting=True)) \
            if state in ("idle", "paused") else ""
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
        # Handed over and answered at once (confirm=False): the cleaning
        # confirms it itself, with Disarm all after DISARM_CONFIRM_S.
        code, out = remote.group_disarm({"group": name}, ctx, confirm=False)
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

    def session_live(ctx):
        """With users on, the sign-in of the request that started the
        routine (or gave the go-ahead) must still stand: signing out stops
        it (Jeff's ruling, the walk-away)."""
        if not remote.users_active():
            return True
        token = getattr(ctx, "token", "")
        return bool(token) and remote.users.session(token) is not None

    panel = CleaningPanel(
        group, problem, link_get=lambda: link, flames_get=flames,
        blockers=lambda: engine_blockers(remote, link, name, arming),
        locked_slots=locked,
        config_get=lambda: load_config(morning.config_path),
        press_arm=remote.arm_hold, release_arm=release,
        disarm_group=disarm_group, disarm_all=disarm_all,
        disarm_count=lambda: remote._disarm_ids, may_disarm_group=may_group,
        session_live=session_live, burn_flag=burn, journal=journal,
        clock=clock, wall=remote.wall, threaded=threaded)
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
    (a person's hold: the start, or the go-ahead) and cleaning-stop.
    (code, body)."""
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
                                  "Sign in to start the cleaning.",
                         "cut": True}
        try:
            seen = float(body.get("seen"))
            seen_ok = abs(remote.wall() - seen / 1000.0) <= PAGE_FRESH_S
        except (TypeError, ValueError):
            seen_ok = False
        return panel.beat(body, ctx, seen_ok=seen_ok, who=who, screen=screen)
    return 404, {"error": "no such thing here"}
