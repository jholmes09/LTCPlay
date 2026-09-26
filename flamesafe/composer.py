"""flamesafe composer: the arm state machine and the one place the flame
universe is written.  Pure: no sockets, no threads, a clock passed in.

Inputs, all pushed by the service:
  assert_arm(wanted, seq)   the arm input's current request, per group, with
                            a counter that must advance.  Arming is a value
                            asserted continuously; its liveness is the arm
                            signal.  No fresh assertion means disarmed.
  ingest_frame(frame)       ltcplay's flame channel block for one frame.
  tick()                    compose one universe frame and one status frame.

Every rule in rules.py is enforced here.  compose never raises: any exception
inside tick() produces an all-zero universe, clears every latch, and is
counted as a fault.
"""

from __future__ import annotations

import math
import time

from . import rules
from .link import FlameFrame, CONTRACT_VERSION

DISARM = rules.DISARM_VALUE
FAULT_CLEAR_S = 5.0
# The words on the ARMED lamp when the show program (ltcplay) is not
# answering.  Both steady amber: cycling now fixes nothing; the fix is the
# show program coming back, and then a cycle.
LINK_LOST = ("Show program stopped answering: disarmed. Cycle the arm to "
             "re-arm once it is back.")
LINK_NEVER = ("Show program has not answered yet: disarmed. Cycle the arm "
              "once it is running.")


def now():
    """The safety program's one clock: perf_counter, on every platform.
    ltcplay/player.py explains why monotonic is not good enough on Windows
    (15.6 ms ticks).  Nothing in flamesafe compares this clock with any
    timestamp from another process."""
    return time.perf_counter()


class Output:
    """What one tick produced."""
    __slots__ = ("universe", "status", "fault")

    def __init__(self, universe, status, fault):
        self.universe = universe        # bytes, exactly 512
        self.status = status            # dict, the status frame
        self.fault = fault              # "" or a sentence


class Composer:

    def __init__(self, config, clock=now, log=None):
        self.cfg = config
        self.groups = config.groups
        self.n = len(self.groups)
        self._clock = clock
        self._log = log

        # arm input
        self._wanted = [False] * self.n
        self._seen_down = [False] * self.n
        self._latched = [False] * self.n
        self._arm_seq = None
        self._arm_fresh_at = None       # our clock, last time seq advanced
        self._arm_seen_at = None        # our clock, last assertion of any kind
        self._arm_live = False
        self._link_live = False         # ltcplay's frames fresh last tick

        # frames from ltcplay
        self._frame = None              # bytes(512) or None
        self._frame_at = None
        self._frame_seq = None
        self._frame_mono = None
        self._frame_tc = None
        self._frame_sender = None       # (ip, port) locked while live
        self._last_reject = ""

        # composing
        self._last_sent = [DISARM] * self.n
        self._disarmed_at = [None] * self.n
        self._chatter_at = [None] * self.n
        self._edge_quiet = [0] * self.n
        self._rise_times = [[] for _ in range(self.n)]
        self._held = [("", "")] * self.n   # (reason, amber mode) per group
        self._fire_refused = [False] * self.n
        self._last_tick = None
        self._fault = ""
        self._fault_at = None
        self.heartbeat = 0

        self.stats = {k: 0 for k in (
            "ticks", "ticks_armed", "frames_accepted", "frames_rejected",
            "arm_assertions", "arm_rejected", "overruns", "compose_faults",
            "edge_blocks", "latch_resets", "dwell_blocks", "chatter_holds",
            "fire_slots_quieted", "fire_refused", "arm_input_stale",
            "link_lost",
            "faults_noted", "faults_cleared")}

    # ------------------------------------------------------------ arm input

    def assert_arm(self, wanted, seq, names=None):
        """The arm input says: I want these groups armed, and my liveness
        counter is `seq`.  Returns True if the assertion was well formed.

        `wanted` is positional: one bool per group, in config order.  An
        input that knows the group names passes them as `names`, and the
        assertion is rejected unless they match the config exactly, in
        order, so a deck built against a different group map cannot arm
        the wrong head.

        Never raises.  A malformed assertion is rejected and counted; the
        staleness rule then disarms within arm_stale_ms if nothing well
        formed follows.
        """
        try:
            w = list(wanted)
            if len(w) != self.n or any(not isinstance(x, bool) for x in w):
                raise ValueError("wanted")
            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
                raise ValueError("seq")
            if names is not None:
                if list(names) != [g.name for g in self.groups]:
                    raise ValueError("names")
        except Exception:                               # noqa: BLE001
            self.stats["arm_rejected"] += 1
            return False

        t = self._clock()
        # Was the input live BEFORE this assertion?  Consent needs both: a
        # counter that advanced now, and one that was already fresh.  The
        # first assertion after any interruption (a boot, a restart, a gap,
        # a counter frozen for longer than arm_stale_ms with reports still
        # flowing) fails the second test, whatever its counter says.  The
        # second review found the frozen-counter case re-arming through a
        # transition-based reset; this rule closes every case but one, a
        # counter that jumps UP across a reboot with no gap longer than
        # arm_stale_ms, which is why the 7b driver restarts its counter at 0.
        was_live = self._arm_is_live(t)
        advanced = False
        if self._arm_seq is None:
            # The first assertion proves nothing: a counter is alive only
            # once it has been SEEN to advance.  Rev 1 latched on the
            # synthetic all-down report a booting watcher emits.
            advanced = False
        elif seq < self._arm_seq:
            # The counter went BACKWARDS: the input rebooted.  That is an
            # interruption even though the assertions kept arriving, and
            # this assertion is a first one again.
            self._reset_latches("arm input restarted")
            advanced = False
        elif seq > self._arm_seq:
            advanced = True
        self._arm_seq = seq
        self._arm_seen_at = t
        if advanced:
            self._arm_fresh_at = t
        self.stats["arm_assertions"] += 1

        # A down edge only counts as consent when the input is PROVEN alive:
        # this very assertion advanced the counter, and the counter was
        # already fresh before it.  An input that boots up already asking
        # for arm has not asked this program for anything.
        consent_ok = advanced and was_live
        for i in range(self.n):
            if not w[i]:
                if self._wanted[i]:
                    # The operator disarmed this group.  The dwell applies.
                    self._disarmed_at[i] = t
                self._seen_down[i] = consent_ok
                self._latched[i] = False
            elif self._seen_down[i] and consent_ok:
                self._latched[i] = True
            self._wanted[i] = w[i]
        return True

    def _reset_latches(self, why):
        if any(self._latched) or any(self._seen_down):
            self.stats["latch_resets"] += 1
            self._event("latch-reset", why)
        self._latched = [False] * self.n
        self._seen_down = [False] * self.n

    # --------------------------------------------------------------- frames

    def ingest_frame(self, frame, sender=None):
        """Take one decoded flame frame from ltcplay.  Returns "" if it was
        accepted, otherwise the reason it was rejected.  Never raises.

        `sender` is the (ip, port) the datagram came from.  While the link
        is live only the first accepted sender is accepted: a second local
        process cannot slip a frame in, and cannot lock ltcplay out with a
        large sequence number either.  Once the link is stale the lock is
        released and the next accepted sender takes it."""
        try:
            if not isinstance(frame, FlameFrame):
                raise TypeError("not a FlameFrame")
            if len(frame.values) != rules.UNIVERSE_SIZE:
                raise ValueError("wrong length")
            t = self._clock()
            fresh = self._frame_is_fresh(t)
            if fresh:
                if sender != self._frame_sender:
                    raise ValueError("another sender")
                # While the link is live, frames must arrive in order and the
                # sender's own clock must not go backwards.  Once the link
                # has gone stale, any sequence is a new ltcplay and is taken.
                if frame.seq <= self._frame_seq:
                    raise ValueError(f"out of order: seq {frame.seq} after "
                                     f"{self._frame_seq}")
                if frame.mono < self._frame_mono:
                    raise ValueError("sender clock went backwards")
            self._frame = bytes(frame.values)
            self._frame_at = t
            self._frame_seq = frame.seq
            self._frame_mono = frame.mono
            self._frame_tc = frame.timecode
            self._frame_sender = sender
            self.stats["frames_accepted"] += 1
            return ""
        except Exception as e:                          # noqa: BLE001
            self.stats["frames_rejected"] += 1
            self._last_reject = str(e) or type(e).__name__
            return self._last_reject

    def reject_frame(self, why):
        """The link layer could not even decode a datagram."""
        self.stats["frames_rejected"] += 1
        self._last_reject = str(why)

    def note_fault(self, sentence):
        """Something outside the composer failed (a send, a status write).
        It goes into the status frame's fault so that ltcplay never shows
        an armed group as fine while the wire is not being written."""
        self._fault = str(sentence)
        self._fault_at = self._clock()
        self.stats["faults_noted"] += 1
        self._event("fault", self._fault)

    def _frame_is_fresh(self, t):
        return (self._frame_at is not None and
                (t - self._frame_at) * 1000.0 <= self.cfg.frame_stale_ms)

    def _fire_is_live(self, t):
        return (self._frame_at is not None and
                (t - self._frame_at) * 1000.0 <= self.cfg.fire_hold_ms)

    def _arm_is_live(self, t):
        return (self._arm_fresh_at is not None and
                (t - self._arm_fresh_at) * 1000.0 <= self.cfg.arm_stale_ms)

    # ----------------------------------------------------------------- tick

    def tick(self):
        """Compose one frame.  NEVER RAISES."""
        try:
            return self._tick()
        except Exception as e:                          # noqa: BLE001
            self.stats["compose_faults"] += 1
            self._fault = f"compose fault: {type(e).__name__}: {e}"
            try:
                self._fault_at = self._clock()
            except Exception:                           # noqa: BLE001
                self._fault_at = None
            self._event("compose-fault", self._fault)
            return self._panic()

    def _tick(self):
        t = self._clock()
        self.heartbeat += 1
        self.stats["ticks"] += 1
        fault = ""

        # 1. Our own liveness.  A tick that comes late by more than
        # overrun_ms means this program was not in control of the wire for
        # that long.  Zero everything and require the operator to cycle.
        if self._last_tick is not None and \
                (t - self._last_tick) * 1000.0 > self.cfg.overrun_ms:
            self.stats["overruns"] += 1
            fault = (f"safety program overran: {int((t - self._last_tick) * 1000)} "
                     f"ms between ticks")
            self._event("overrun", fault)
            self._reset_latches("overrun")
            self._last_tick = t
            self._fault = fault
            self._fault_at = t
            return self._panic()
        self._last_tick = t

        # 2. Arm input liveness.  Fresh means the counter advanced inside
        # arm_stale_ms.  Not fresh means disarmed, and the latches go too.
        # Consent after the gap is assert_arm's business: it needs the
        # counter to have been fresh BEFORE the assertion that carries the
        # down edge, so the first one back proves nothing.
        live = self._arm_is_live(t)
        if not live and self._arm_live:
            self.stats["arm_input_stale"] += 1
            self._event("arm-input", "arm input stale: no fresh assertion "
                                     f"for {self.cfg.arm_stale_ms} ms")
        if not live:
            self._reset_latches("arm input stale")
        self._arm_live = live

        # A fault clears itself after FAULT_CLEAR_S of clean ticks and clean
        # sends (every fault, including a failed send, refreshes _fault_at).
        # One failed send must not be red all night; the counts stay in the
        # stats and the status frame, and the journal records the clearing.
        if self._fault and self._fault_at is not None and \
                (t - self._fault_at) >= FAULT_CLEAR_S:
            self.stats["faults_cleared"] += 1
            self._event("fault-cleared", f"after {FAULT_CLEAR_S:.0f} s "
                                         f"clean: {self._fault}")
            self._fault = ""
            self._fault_at = None

        # 3. ltcplay's frame.  A fire value is kept on the wire for at most
        # fire_hold_ms after the last accepted frame; after that we know
        # nothing about the cue and the fire slots are zero.  After
        # frame_stale_ms the link itself is lost, and that DISARMS every
        # group (Jeff, 2026-09-26): the latches go, the arm value comes off
        # every safety slot, and a fresh arm cycle is needed once the show
        # program is back, exactly as for a stale arm input.  A group never
        # arms before the show program has answered at all.
        frame_fresh = self._frame_is_fresh(t)
        fire_live = self._fire_is_live(t)
        commanded = self._frame if fire_live else None
        link_live = frame_fresh
        if not link_live and self._link_live:
            self.stats["link_lost"] += 1
            self._event("link", "show program stopped answering: every group "
                                "disarmed; cycle the arm to re-arm once it "
                                "is back")
        if not link_live:
            self._reset_latches("show program link lost")
        self._link_live = link_live

        # 4. The safety slots.
        want = [live and link_live and self._wanted[i] and self._latched[i]
                for i in range(self.n)]
        values = []
        held = []
        for i, g in enumerate(self.groups):
            prev = self._last_sent[i]
            if not want[i]:
                values.append(DISARM)
                held.append(self._why_not(i, live, link_live))
                continue
            if prev != DISARM:
                # Already up.  Holding an established arm is not a rising
                # edge, so the precondition does not re-apply.
                values.append(g.arm_value)
                held.append(("", ""))
                continue
            da = self._disarmed_at[i]
            if da is not None and (t - da) * 1000.0 < self.cfg.min_arm_dwell_ms:
                # Too soon after a disarm.  Raising now would be the up half
                # of a chatter cycle.  Steady amber with the countdown.  A
                # hold that chatter started keeps saying so for its whole
                # length.
                values.append(DISARM)
                if self._chatter_at[i] is not None and self._chatter_at[i] == da:
                    held.append(("chatter", "steady"))
                else:
                    held.append(("re-arm dwell", "steady"))
                self.stats["dwell_blocks"] += 1
                continue
            if self._fire_is_quiet(commanded, g):
                # Record the rise before allowing it.  A single transient
                # costs nothing; a slot that keeps rising is chattering a
                # Showven's enable window, an emergency stop and a
                # depressurisation every cycle, whatever caused it.  Rev 5
                # let the chattering rise through and only delayed the next
                # one; rev 9 refuses it, which is stricter, and holds the
                # group for the dwell from now.
                rt = self._rise_times[i]
                cutoff = t - rules.CHATTER_WINDOW_MS / 1000.0
                while rt and rt[0] < cutoff:
                    rt.pop(0)
                if len(rt) >= rules.CHATTER_RISES:
                    # This would be rise number CHATTER_RISES + 1 inside the
                    # window.  Refused, not recorded (only rises that went
                    # out count), so the hold ends when the window slides.
                    self._disarmed_at[i] = t
                    self._chatter_at[i] = t
                    self.stats["chatter_holds"] += 1
                    self._event("chatter",
                                f"{g.name}: {len(rt) + 1} rises inside "
                                f"{rules.CHATTER_WINDOW_MS} ms, holding it "
                                f"down for {self.cfg.min_arm_dwell_ms} ms")
                    values.append(DISARM)
                    held.append(("chatter", "steady"))
                    continue
                rt.append(t)
                values.append(g.arm_value)
                held.append(("", ""))
                # +1 because this tick consumes one count and this tick is
                # clean by construction; the count is about the ticks AFTER
                # the rise.
                self._edge_quiet[i] = rules.EDGE_QUIET_FRAMES + 1
            else:
                # Raising here would produce a dirty rising edge.  The head
                # would silently refuse to arm.  Hold at zero and say so.
                values.append(DISARM)
                held.append(("dirty edge", "flashing"))
                self.stats["edge_blocks"] += 1
                self._event("edge-block", f"{g.name}: held disarmed, a flame "
                                          f"slot is at or above "
                                          f"{rules.GFLAME_EDGE_BELOW}")

        # 5. The universe.  Zeros everywhere, then our slots.  Every channel
        # that belongs to no group is always zero: Galaxis require an
        # exclusive universe with zeros on unused channels.
        buf = bytearray(rules.UNIVERSE_SIZE)
        sent_fire = []
        commanded_fire = []
        for i, g in enumerate(self.groups):
            buf[g.safety - 1] = values[i]
            quiet = self._edge_quiet[i] > 0
            if quiet:
                self._edge_quiet[i] -= 1
            armed_now = values[i] != DISARM
            cf = [commanded[f - 1] if commanded is not None else 0
                  for f in g.fire]
            sf = []
            for f, v in zip(g.fire, cf):
                # A fire value goes out only while this group's safety slot
                # carries the arm value on this same frame and the edge-quiet
                # window has passed.  Everything else is zero.
                if armed_now and not quiet:
                    out = v
                elif armed_now and quiet and v != 0:
                    out = 0
                    self.stats["fire_slots_quieted"] += 1
                else:
                    out = 0
                buf[f - 1] = out
                sf.append(out)
            if not armed_now and any(v != 0 for v in cf):
                # ltcplay commanded fire on a disarmed group.  Refused above;
                # logged as a fault, once per episode, never an operator alarm.
                self.stats["fire_refused"] += 1
                if not self._fire_refused[i]:
                    self._fire_refused[i] = True
                    self._event("fire-refused",
                                f"{g.name}: a fire value was commanded while "
                                f"disarmed and was not sent")
            else:
                self._fire_refused[i] = False
            sent_fire.append(sf)
            commanded_fire.append(cf)

        self._last_sent = values
        self._held = held
        if any(v != DISARM for v in values):
            self.stats["ticks_armed"] += 1
        if fault:
            self._fault = fault
            self._fault_at = t
        status = self._status(t, values, sent_fire, commanded_fire, held,
                              live, frame_fresh, fire_live)
        return Output(bytes(buf), status, fault)

    def _why_not(self, i, live, link_live):
        """Why a group the input wants armed is not: (reason, amber mode).
        Flashing amber means cycling the arm is the fix.  Steady amber means
        wait, or fix something else; cycling would only restart the dwell."""
        if not self._wanted[i]:
            return ("", "")
        if self._arm_fresh_at is None:
            return ("arm input has never asserted", "steady")
        if not live:
            return ("arm input stale", "steady")
        if not link_live:
            if self._frame_at is None:
                return (LINK_NEVER, "steady")
            return (LINK_LOST, "steady")
        if not self._latched[i]:
            return ("cycle the arm", "flashing")
        return ("not composing", "steady")

    def _fire_is_quiet(self, commanded, g):
        """True when every fire slot this group owns is strictly below
        GFLAME_EDGE_BELOW in the frame we would send, i.e. the rising edge
        would satisfy the manual's precondition.  No frame means zeros."""
        if commanded is None:
            return True
        for f in g.fire:
            if commanded[f - 1] >= rules.GFLAME_EDGE_BELOW:
                return False
        return True

    def _panic(self):
        """All zeros, every latch cleared, every established arm forgotten
        so the next tick sees a rising edge and checks it."""
        try:
            self._last_sent = [DISARM] * self.n
            self._edge_quiet = [0] * self.n
            self._reset_latches("panic")
            self._held = [("safety program fault", "steady")
                          if self._wanted[i] else ("", "")
                          for i in range(self.n)]
            t = self._clock()
            # The input states in a panic status are the real ones: the
            # frames may well be fresh and the input live; it is this
            # program that faulted.
            status = self._status(t, self._last_sent,
                                  [[0] * len(g.fire) for g in self.groups],
                                  [[0] * len(g.fire) for g in self.groups],
                                  self._held, self._arm_is_live(t),
                                  self._frame_is_fresh(t), False)
        except Exception:                               # noqa: BLE001
            status = {"v": CONTRACT_VERSION, "t": "status",
                      "heartbeat": self.heartbeat, "fault": self._fault,
                      "groups": []}
        return Output(bytes(rules.UNIVERSE_SIZE), status, self._fault)

    # --------------------------------------------------------------- status

    def _dwell_seconds(self, i, t):
        da = self._disarmed_at[i]
        if da is None:
            return 0
        left_ms = self.cfg.min_arm_dwell_ms - (t - da) * 1000.0
        if left_ms <= 0:
            return 0
        return int(math.ceil(left_ms / 1000.0))

    def _status(self, t, values, sent_fire, commanded_fire, held, live,
                frame_fresh, fire_live):
        groups = []
        for i, g in enumerate(self.groups):
            reason, amber = held[i]
            if values[i] != DISARM:
                state = "armed"
            elif self._wanted[i]:
                state = "held"
            else:
                state = "disarmed"
            dwell_s = self._dwell_seconds(i, t) if state == "held" else 0
            if state == "held" and reason == "re-arm dwell":
                dwell_s = max(dwell_s, 1)
            groups.append({
                "name": g.name,
                "safety_slot": g.safety,
                "fire_slots": list(g.fire),
                "wanted": self._wanted[i],
                "armed": state,
                "reason": reason,
                "amber": amber if state == "held" else "",
                "dwell_s": dwell_s,
                "sent_safety": values[i],
                "sent_fire": list(sent_fire[i]),
                "commanded_fire": list(commanded_fire[i]),
            })
        arm_age = (None if self._arm_fresh_at is None
                   else int((t - self._arm_fresh_at) * 1000))
        frame_age = (None if self._frame_at is None
                     else int((t - self._frame_at) * 1000))
        return {
            "v": CONTRACT_VERSION,
            "t": "status",
            "heartbeat": self.heartbeat,
            "tick_ms": round(1000.0 / self.cfg.tick_hz, 3),
            "universe": self.cfg.universe,
            "priority": rules.SACN_PRIORITY,
            "arm_value": self.cfg.arm_value,
            "confirmed": self.cfg.confirmed,
            "fault": self._fault,
            "fault_age_ms": (None if self._fault_at is None
                             else int((t - self._fault_at) * 1000)),
            "arm_input": {
                "state": ("never" if self._arm_fresh_at is None
                          else "live" if live else "stale"),
                "seq": self._arm_seq,
                "age_ms": arm_age,
            },
            "frames": {
                "state": ("never" if self._frame_at is None
                          else "fresh" if frame_fresh else "stale"),
                "fire": "passing" if fire_live else "zeroed",
                "seq": self._frame_seq,
                "timecode": self._frame_tc,
                "age_ms": frame_age,
                "accepted": self.stats["frames_accepted"],
                "rejected": self.stats["frames_rejected"],
                "last_reject": self._last_reject,
            },
            "stats": dict(self.stats,
                          journal_dropped=int(getattr(self._log, "dropped",
                                                      0) or 0)),
            "groups": groups,
        }

    def _event(self, kind, msg):
        if self._log is None:
            return
        try:
            self._log.event(kind, msg)
        except Exception:                               # noqa: BLE001
            pass
