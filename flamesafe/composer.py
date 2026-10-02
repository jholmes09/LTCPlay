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
import re       # after time, so mutate.py's "flamesafe imports ltcplay"
                # pattern (import math / import time) still matches once

from . import rules
from .link import DisarmAll, FlameFrame, CONTRACT_VERSION

DISARM = rules.DISARM_VALUE
FAULT_CLEAR_S = 5.0
# The words on the ARMED lamp when the show program (ltcplay) is not
# answering.  Both steady amber: cycling now fixes nothing; the fix is the
# show program coming back, and then a cycle.
LINK_LOST = ("Show program stopped answering: disarmed. Cycle the arm to "
             "re-arm once it is back.")
# Round 4, item B: why a cycle is not being accepted right now.
OTHER_SENDER = ("Another sender is on the arm link: a cycle cannot arm "
                "until it stops. Cycle the arm again once it has gone.")
LINK_NEVER = ("Show program has not answered yet: disarmed. Cycle the arm "
              "once it is running.")
# The words on the ARMED lamp of a group the show program's Abort disarmed
# (disarm_all, CONTRACT.md).  Flashing amber: cycling the arm IS the fix,
# and the only one.  Shown until that group latches again.
ABORT_DISARMED = "Disarmed by the show's Abort. Cycle the arm to re-arm."
# Rejections on the flame link are journaled once per episode per kind of
# reason (an episode ends after frame_stale_ms with no rejection of that
# kind), never once per datagram: a flood at 40 Hz or faster would push
# everything else out of the bounded journal queue.  At most this many
# kinds are tracked at once; any further kind is counted under "other".
REJECT_KINDS_MAX = 16


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
        self._arm_sender = None         # round 4: who the last one came from
        self._arm_fresh_at = None       # our clock, last time seq advanced
        self._arm_seen_at = None        # our clock, last assertion of any kind
        self._arm_live = False
        self._link_live = False         # ltcplay's frames fresh last tick
        self._link_lost_at = None       # tick clock when the link went stale
        # Round 3 of the safety review, item 6: how many OTHER senders
        # SocketArmInput is currently tracking on the arm link (fresh
        # inside its own stale_ms), as of the last note_foreign_arm_senders
        # call. Zero for every input that never has one (NullArmInput,
        # ScriptedArmInput). Carried in the status frame so the deck can
        # raise its own alarm the instant a foreign sender is interacting
        # with the link at all, not only once a divergence it can actually
        # observe (a forced bit that happens to already match what this
        # deck expects leaves nothing else to notice).
        self._foreign_arm_senders = 0
        # Round 4 of the safety review, item B: whether the arm input has
        # seen a flood (arminput.FLOOD_DATAGRAMS_PER_POLL) inside its own
        # stale_ms, as of the last note_arm_link_flooded call.
        self._arm_link_flooded = False

        # frames from ltcplay
        self._frame = None              # bytes(512) or None
        self._frame_at = None
        self._frame_seq = None
        self._frame_mono = None
        self._frame_tc = None
        self._frame_sender = None       # (ip, port) locked while live
        self._last_reject = ""
        # Rejection episodes, by kind of reason: {kind: {"at", "count"}}.
        # See REJECT_KINDS_MAX.
        self._reject_episodes = {}

        # disarm_all from the show program (CONTRACT.md, 2026-10-02).
        # _aborted[i] only changes the WORDS on a held group's lamp; it is
        # never read by anything that decides a safety value.
        self._aborted = [False] * self.n
        self._disarm_count = 0          # accepted disarm_all datagrams
        self._disarm_last_id = None
        self._disarm_last_reason = ""
        self._disarm_at = None

        # composing
        self._last_sent = [DISARM] * self.n
        self._disarmed_at = [None] * self.n
        self._chatter_at = [None] * self.n
        self._edge_quiet = [0] * self.n
        self._rise_times = [[] for _ in range(self.n)]
        self._held = [("", "")] * self.n   # (reason, amber mode) per group
        self._fire_refused = [False] * self.n
        self._name_mismatch_logging = False   # item 10: once per episode
        self._name_mismatch_count = 0
        self._last_tick = None
        self._fault = ""
        self._fault_at = None
        self.heartbeat = 0

        self.stats = {k: 0 for k in (
            "ticks", "ticks_armed", "frames_accepted", "frames_rejected",
            "arm_assertions", "arm_rejected", "overruns", "compose_faults",
            "edge_blocks", "latch_resets", "dwell_blocks", "chatter_holds",
            "fire_slots_quieted", "fire_refused", "arm_input_stale",
            "link_lost", "disarm_all", "disarm_all_rejected",
            "faults_noted", "faults_cleared")}

    # ------------------------------------------------------------ arm input

    def assert_arm(self, wanted, seq, names=None, forced=None, sender=None):
        """The arm input says: I want these groups armed, and my liveness
        counter is `seq`.  Returns True if the assertion was well formed.

        `wanted` is positional: one bool per group, in config order.  An
        input that knows the group names passes them as `names`, and the
        assertion is rejected unless they match the config exactly, in
        order, so a deck built against a different group map cannot arm
        the wrong head.

        `forced` (added round 3 of the safety review, item 1) is optional:
        one bool per group, True where this call's False bit is not a
        genuine report from the input -- SocketArmInput sets it where its
        own foreign-disarm AND (arminput.py's FOREIGN DISARM section)
        cleared a bit that the locked sender itself was not asking to
        clear.  A forced low still disarms this tick (that is the entire
        point of letting a foreign frame clear a bit), but it must never
        be read as the operator's own down edge: see the consent loop
        below.  None (every other input) means nothing is forced, exactly
        like an all-False vector.

        `sender` (round 4 of the safety review) is optional: who this
        assertion came from (SocketArmInput: the locked (ip, port)).  When
        it differs from the last assertion's sender, this is treated exactly
        like an input restart: every latch and every pending down edge is
        cleared, so no consent edge can ever be half proved by one sender
        and finished by another.

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
            if forced is None:
                f = [False] * self.n
            else:
                f = list(forced)
                if len(f) != self.n or any(not isinstance(x, bool) for x in f):
                    raise ValueError("forced")
            if names is not None:
                want_names = [g.name for g in self.groups]
                if list(names) != want_names:
                    # Safety review of PR #31, item 6: this used to be
                    # folded into the generic except below, which counted
                    # it and journaled NOTHING -- a deck built against a
                    # different group map failed to arm anything and the
                    # night journal never said why. Named here instead, so
                    # it says which names, and against what.
                    self.stats["arm_rejected"] += 1
                    self._name_mismatch_count += 1
                    if not self._name_mismatch_logging:
                        # Round 2 of the safety review, item 10: logged once
                        # per continuous episode, with a running count, not
                        # once per assertion -- at 10 Hz or faster a
                        # misconfigured deck would otherwise flood the
                        # bounded journal queue (1000 lines) within a
                        # couple of minutes, pushing out everything else.
                        self._name_mismatch_logging = True
                        self._event(
                            "arm-link",
                            f"arm assertion rejected: its group names "
                            f"{list(names)!r} do not match this config's "
                            f"{want_names!r}; a deck built against a "
                            f"different group map cannot arm the wrong "
                            f"head. Further rejections for this same "
                            f"reason will not be logged individually "
                            f"until it stops.")
                    return False
                elif self._name_mismatch_logging:
                    self._name_mismatch_logging = False
                    self._event(
                        "arm-link",
                        f"arm assertions are matching this config's group "
                        f"names again, after {self._name_mismatch_count} "
                        f"rejected for a mismatch")
                    self._name_mismatch_count = 0
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
        elif sender is not None and self._arm_sender is not None and \
                sender != self._arm_sender:
            # Round 4: the arm input's sender lock changed hands.  Whatever
            # the counter says, this is a different input: start over.
            self._reset_latches("arm input changed sender")
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
        self._arm_sender = sender
        self._arm_seen_at = t
        if advanced:
            self._arm_fresh_at = t
        self.stats["arm_assertions"] += 1

        # A down edge only counts as consent when the input is PROVEN alive:
        # this very assertion advanced the counter, and the counter was
        # already fresh before it.  An input that boots up already asking
        # for arm has not asked this program for anything.
        #
        # Round 4 of the safety review, item B: and nobody else is on the
        # link.  A foreign sender's frames can only ever clear bits (the
        # AND in arminput.py), but the round-4 review proved a sender can
        # still BECOME the locked one with no deck gap at all, by flooding
        # the port until the real deck's frames are crowded out for
        # arm_stale_ms, and then forge its own low-then-high.  Once it holds
        # the lock the real deck is the foreign one, still sending, so
        # "nobody else on the link and no flood" is false for as long as
        # the real deck is alive -- the rogue can never collect consent.
        # The cost: a genuine cycle made while another sender is on the
        # link does not count, and the operator cycles again once it has
        # gone (CONTRACT.md, the arm link).
        disturbed = (self._foreign_arm_senders != 0
                     or self._arm_link_flooded)
        consent_ok = advanced and was_live and not disturbed
        if disturbed:
            # And no down edge seen BEFORE the other sender turned up may be
            # finished while it is here: the operator cycles again once it
            # has gone (the deck keeps re-asserting its own False, so a
            # genuine low is re-proved on the first frame after it goes).
            self._seen_down = [False] * self.n
        for i in range(self.n):
            if not w[i]:
                if self._wanted[i]:
                    # Something disarmed this group (the operator, or a
                    # foreign sender's forced clear).  The dwell applies
                    # either way: a value that just went to zero must not
                    # bounce straight back up, forced or not.
                    self._disarmed_at[i] = t
                # Round 3 of the safety review (item 1): seen_down is the
                # "the operator pulled this down for real" flag a future
                # True consumes as consent (just below).  A FORCED low --
                # this bit went to False only because a foreign sender's
                # AND cleared it, never because the locked sender itself
                # reported it -- must never set that flag: it is not the
                # operator cycling anything, and letting it count is
                # exactly how a foreign False-then-True sequence used to
                # forge a consent edge while the locked sender's own report
                # never changed. A forced low also CLEARS any seen_down a
                # genuine low already set: the bit the composer is looking
                # at right now did not come from the locked sender, so
                # there is nothing left here that proves the operator did
                # anything, forced or not.
                self._seen_down[i] = consent_ok and not f[i]
                self._latched[i] = False
            elif self._seen_down[i] and consent_ok:
                self._latched[i] = True
                self._aborted[i] = False
            self._wanted[i] = w[i]
        return True

    def note_foreign_arm_senders(self, count):
        """How many OTHER senders the arm input is currently tracking on
        the link (round 3 of the safety review, item 6).  Called by the
        service every tick, independent of whether assert_arm was also
        called this tick (a locked sender that goes briefly quiet must not
        make this number look stale just because nothing else moved).
        Never raises: a bad value is simply not counted, which is the safe
        side -- the deck losing this one extra signal is never worse than
        the deck crashing."""
        try:
            self._foreign_arm_senders = max(0, int(count))
        except (TypeError, ValueError):
            pass

    def note_arm_link_flooded(self, flooded):
        """Whether the arm input has seen a flood inside its own stale_ms
        (round 4 of the safety review, item B).  Called by the service every
        tick, BEFORE assert_arm, like note_foreign_arm_senders.  Never
        raises."""
        self._arm_link_flooded = bool(flooded) if isinstance(
            flooded, bool) else True

    def _reset_latches(self, why, journal=True):
        # journal=False clears just the same but neither counts nor writes
        # a line: for a reset that repeats every tick while a link is down.
        if any(self._latched) or any(self._seen_down):
            if journal:
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
            self._note_reject(self._last_reject)
            return self._last_reject

    def reject_frame(self, why):
        """The link layer could not even decode a datagram."""
        self.stats["frames_rejected"] += 1
        self._last_reject = str(why)
        self._note_reject(self._last_reject)

    def disarm_all(self, msg, sender=None):
        """The show program says: disarm every group, now (its Abort).
        Returns "" if accepted, otherwise the reason it was refused.  Never
        raises.

        Accepted only from the live, locked flame-link sender, in order,
        exactly as a flame frame would be: the right key and shape were
        already checked by link.decode_disarm_all, and here the sender
        lock, the sequence and the sender's clock are checked against the
        same record the flame frames use.  With no live flame link there
        is nothing to accept it from (and nothing armed: link loss already
        disarmed every group), so it is refused.

        What it does, and all it does: every latch and every pending
        consent edge (`_seen_down`) is cleared, and every group that was up
        or latched gets the re-arm dwell from now.  It never sets a latch,
        never sets `_seen_down`, never touches `_wanted`: a group comes
        back only through a fresh, genuine, un-forced low-to-high cycle
        from the arm input AFTER this message (assert_arm, rule 6), and
        then only once the dwell has passed.  It does not refresh the flame
        link's liveness or its fire values (it carries none)."""
        try:
            if not isinstance(msg, DisarmAll):
                raise TypeError("not a DisarmAll")
            t = self._clock()
            if not self._frame_is_fresh(t):
                raise ValueError("no live flame link to accept it from")
            if sender != self._frame_sender:
                raise ValueError("another sender")
            if msg.seq <= self._frame_seq:
                raise ValueError(f"out of order: seq {msg.seq} after "
                                 f"{self._frame_seq}")
            if msg.mono < self._frame_mono:
                raise ValueError("sender clock went backwards")
        except Exception as e:                          # noqa: BLE001
            self.stats["disarm_all_rejected"] += 1
            why = f"disarm_all: {str(e) or type(e).__name__}"
            self._last_reject = why
            self._note_reject(why)
            return why
        self._frame_seq = msg.seq
        self._frame_mono = msg.mono
        was_up = [self._latched[i] or self._last_sent[i] != DISARM
                  for i in range(self.n)]
        for i in range(self.n):
            if was_up[i]:
                self._disarmed_at[i] = t
            self._aborted[i] = True
        self._latched = [False] * self.n
        self._seen_down = [False] * self.n
        self.stats["disarm_all"] += 1
        self._disarm_count += 1
        new_abort = msg.abort_id != self._disarm_last_id
        self._disarm_last_id = msg.abort_id
        self._disarm_last_reason = msg.reason
        self._disarm_at = t
        if new_abort:
            # The sender repeats one Abort a few times in case a datagram
            # is lost; each copy is applied (it can only clear), but only
            # the first is written.
            up = [g.name for g, u in zip(self.groups, was_up) if u]
            armed = ("armed until now: " + ", ".join(up)) if up \
                else "none was armed"
            self._event("disarm-all",
                        f"the show program's Abort disarmed every group "
                        f"({msg.reason}; abort {msg.abort_id}; {armed}). "
                        f"Each group needs a fresh arm cycle from the "
                        f"Stream Deck.")
        return ""

    def _note_reject(self, why):
        """Journal a rejection once per episode per kind of reason."""
        try:
            kind = _reject_kind(why)
            t = self._clock()
            ep = self._reject_episodes.get(kind)
            if ep is None and len(self._reject_episodes) >= REJECT_KINDS_MAX:
                kind = "other"
                ep = self._reject_episodes.get(kind)
            if ep is not None:
                ep["at"] = t
                ep["count"] += 1
                return
            self._reject_episodes[kind] = {"at": t, "count": 1}
            self._event("link-reject",
                        f"flame link datagram rejected: {why}. Further "
                        f"rejections of this kind are counted, not written, "
                        f"until none for {self.cfg.frame_stale_ms} ms.")
        except Exception:                               # noqa: BLE001
            pass

    def _close_reject_episodes(self, t):
        for kind in [k for k, ep in self._reject_episodes.items()
                     if (t - ep["at"]) * 1000.0 > self.cfg.frame_stale_ms]:
            ep = self._reject_episodes.pop(kind)
            if ep["count"] > 1:
                self._event("link-reject",
                            f"flame link rejections ({kind}) stopped after "
                            f"{ep['count']} rejected")

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

        self._close_reject_episodes(t)

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
            self._link_lost_at = t
            self._event("link", "show program stopped answering: every group "
                                "disarmed; cycle the arm to re-arm once it "
                                "is back")
        if link_live and not self._link_live and \
                self._link_lost_at is not None:
            # The other end of the outage, so its length is readable in the
            # journal the next morning.  Startup has no outage to close.
            self._event("link", "show program answering again after "
                                f"{t - self._link_lost_at:.1f} s; every "
                                "group stays disarmed until a cycle")
            self._link_lost_at = None
        if not link_live:
            # Every stale tick clears, but only the first one is journaled
            # and counted: the deck re-reports its OFF keys every tick, so
            # there is always a down edge to forget, and one line per tick
            # is a flood (safety review of PR #23).
            self._reset_latches("show program link lost",
                                journal=self._link_live)
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
        if not self._latched[i] and (self._foreign_arm_senders
                                     or self._arm_link_flooded):
            # Round 4, item B: cycling now would not count; say so instead
            # of flashing "cycle the arm" at an operator whose cycle is
            # being refused.
            return (OTHER_SENDER, "steady")
        if not self._latched[i]:
            if self._aborted[i]:
                return (ABORT_DISARMED, "flashing")
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
                "foreign_senders": self._foreign_arm_senders,
                "flooded": self._arm_link_flooded,
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
            "disarm_all": {
                "accepted": self._disarm_count,
                "last_id": self._disarm_last_id,
                "last_reason": self._disarm_last_reason,
                "age_ms": (None if self._disarm_at is None
                           else int((t - self._disarm_at) * 1000)),
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


def _reject_kind(why):
    """A short, bounded name for the kind of a rejection reason: quoted
    text and numbers (which a sender controls, and which change on every
    datagram) are blanked, and only the first few words are kept."""
    s = re.sub(r"'[^']*'|\"[^\"]*\"|\d+(\.\d+)?", "#", str(why))
    return " ".join(s.split()[:4]) or "unknown"
