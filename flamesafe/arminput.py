"""flamesafe arm input: SocketArmInput is the real build step 7b driver,
fed over a keyed loopback link by the Stream Deck (ltcplay/streamdeck.py,
part of ltcplay's own process, never this one); ScriptedArmInput is the
test driver; NullArmInput is what flamesafe runs with when no arm link is
configured at all (every group stays disarmed, on purpose).

Arming is a value asserted continuously.  An input does not send "arm" once;
it keeps saying "I want these groups armed" with a counter that advances,
and the composer treats the absence of a fresh assertion as disarmed.  So an
input that is unplugged, hung, or has crashed disarms every group inside
arm_stale_ms without anyone doing anything.

RULES EVERY DRIVER FOLLOWS, each one backed by a composer rule:

  1. poll() returns None while the device is not connected or the driver
     is not ready.  It NEVER synthesises a report: no "all down" on boot,
     no "all down" on unplug.  The composer treats a first assertion as
     proof of nothing, so a synthetic down edge would not be consent, but
     silence is the honest signal and it disarms in arm_stale_ms.
  2. `wanted` is POSITIONAL: one bool per group in config order.  Pass
     `names` too (the group names in the same order); the composer rejects
     an assertion whose names do not match its config exactly, so a deck
     built against an older group map cannot arm the wrong head.
  3. `seq` is a plain int that starts at 0 on every connect and reconnect
     and goes up by one per assertion.  A counter that restarts is how the
     composer knows the input restarted.  Never a time-based counter: one
     that jumps UP on a restart looks like nothing happened.
  4. Assert at 10 Hz or faster.  The default arm_stale_ms is 500 ms.
  5. On Windows only Ctrl-C and Ctrl-Break reach the service's stop
     handler.  The driver must offer an in-band stop (a key, or a message)
     that sets the service's stop event, so a clean stop, which zeros the
     wire, is always one press away.  (SocketArmInput itself has no stop
     button; the Stream Deck's own in-band stop, if any, is ltcplay's
     concern, not this file's.)

SocketArmInput does NOT decide anything about arming.  It only gets a well
formed assertion from the wire onto this interface; every consent, dwell,
chatter and edge-quiet rule in rules.py runs in the composer exactly as it
does for ScriptedArmInput in the tests.  It is deliberately dumb for the
same reason composer.py is the only place that writes the flame universe:
one place to get the safety rules right, not two.

SENDER LOCK (added 2026-10-01, after a second safety review proved a
forged local datagram is not harmless -- see SocketArmInput's own
docstring for the full reasoning): while SocketArmInput has accepted a
datagram from an (ip, port) inside arm_stale_ms of another, a datagram
from anywhere else is rejected as "another sender" and journaled, exactly
like the flame-frame link's own lock (composer.ingest_frame). The lock
lives in this file, not in the composer: consent (rule 2 below) still has
to be proved by whoever holds it, unchanged.

FOREIGN DISARM (added after a THIRD safety review, round 2): the lock
above closes the obvious hole -- a rogue cannot be ACCEPTED as the sender
once one is locked in -- but it left open a worse one: if the real deck
is silent for arm_stale_ms (a reconnect, a restart, ordinary startup
ordering before the real deck has sent its first frame), a rogue can take
the lock itself, for real, and arm groups the operator never asked for.
When the real deck then reconnects and sends Abort, ITS frames are the
ones now rejected as "another sender" -- so the Abort visibly fails to
disarm what the rogue armed, which is exactly the MASKED-Abort failure
the lock was supposed to prevent, just with the roles swapped. So: a
frame from any sender OTHER than the currently-locked one may still never
ARM anything, but it must always be able to DISARM. poll() now tracks
each foreign sender's last-reported `wanted` vector (while it keeps
re-asserting inside its own stale_ms) and ANDs every tracked foreign
vector, bit for bit, into whatever assertion this call returns: a foreign
sender saying a group is wanted=False forces that group's bit to False in
the result no matter what the locked sender is asking for, restoring "a
foreign frame can only ever disarm, never arm" even while a rogue holds
the nominal lock. This does not and cannot re-arm anything, does not
change seq or names (those still come from the locked sender, so consent
in the composer is unaffected), and a foreign sender still never becomes
the lock holder by sending this way.

FORCED-EDGE TRACKING (added after a FOURTH safety review, round 3): the
foreign-disarm AND above closes "a rogue can mask an Abort", but it opened
a different hole, proven by running the actual attack: a foreign sender
that sends a group's `wanted` False for a while and then True again puts a
False-then-True sequence in front of the composer -- and the composer's
own consent rule (rule 6) reads ANY False-then-True sequence on a counter
it already trusts as "the operator cycled the arm", because it has no way
to tell a FORCED low (the real deck never stopped asking for True; this
file's AND just clipped the bit for a while) from a GENUINE one (the real
deck's own key actually went up then down). The locked sender's own report
never changed through the whole attack, so the composer armed a group the
operator never touched. poll() now reports, per group, whether its False
bit in the returned ArmAssertion was forced here rather than genuinely
asserted by the locked sender (`ArmAssertion.forced`, see its docstring);
composer.assert_arm uses that to refuse to treat a forced low as proof of
anything -- a group that was only ever forced low must stay disarmed/held
until the LOCKED sender ITSELF reports a fresh, un-forced low-to-high
transition. This is additive: it changes nothing about which bits reach
the composer (still the same AND as round 2, a foreign sender can still
only ever clear a bit, never set one), only what the composer is allowed
to infer from a bit that got cleared this way.
"""

from __future__ import annotations

import socket
import time

from . import link

# Datagrams drained per poll.  A flood beyond this waits for the next tick;
# only the last one decoded this call is kept, matching service.py's own
# _drain() for the flame-frame link.
DRAIN_PER_TICK = 200

# The sender lock's own staleness window (see SocketArmInput below), the
# same default CONTRACT.md gives arm_stale_ms.  __main__.py passes the
# config's real arm_stale_ms instead; this is only what you get if nobody
# does.
DEFAULT_STALE_MS = 500

# A FLOOD (round 4 of the safety review, item B): more datagrams waiting on
# the arm port in one poll than any honest sender could have produced.  The
# real deck sends at 20 Hz and flamesafe polls at 40 Hz, so a poll normally
# finds 0 to 2; even a tick stalled for a whole second finds about 20.  A
# flood big enough to crowd the real deck's frames out of the kernel's
# receive buffer (which is how the round-4 review took the sender lock without
# any deck gap at all) has to put hundreds there per tick.  While one has
# been seen inside the input's own stale_ms, `flooded` is True and the
# composer refuses every NEW consent edge (composer.assert_arm).
FLOOD_DATAGRAMS_PER_POLL = 50

# Round 5 of the safety review, item 4: the kernel's receive buffer is
# limited in BYTES, not datagrams.  The round-5 review filled the default
# 212,992-byte buffer with 12 maximum-size valid frames, far under the
# 50-datagram line above, so a flood of big frames crowded the deck out
# without ever reading as a flood.  So a poll that reads more than
# FLOOD_BYTES_PER_POLL bytes is a flood too (an honest deck frame is a few
# hundred bytes; a tick stalled for a whole second finds about 20 of them),
# and open() asks for ARM_RCVBUF_BYTES of receive buffer.  Together: to fill
# even the default buffer a sender has to put more than 50 datagrams or
# more than 64 KiB there between two polls (50 datagrams of kernel
# overhead plus 64 KiB of payload is well under 212,992 bytes), and either
# one is a flood.
FLOOD_BYTES_PER_POLL = 64 * 1024
ARM_RCVBUF_BYTES = 4 * 1024 * 1024

# Journal throttling for every arm-link rejection (round 4 of the safety
# review, item C).  One line per REASON when an episode starts, one closing
# line with the count once that reason has been quiet for EPISODE_QUIET_S,
# and never more than LINES_PER_MINUTE lines per reason in any 60 s, however
# the rejections are spaced.  EPISODE_QUIET_S is ten times the default
# arm_stale_ms on purpose: round 3 closed an episode after stale_ms (500
# ms), so one datagram every 520 ms opened a new episode, and wrote a new
# line, every single time.
EPISODE_QUIET_S = 5.0
LINES_PER_MINUTE = 4
# Round 5 of the safety review, item 5: and never more than this many in
# any 60 s across ALL reasons together.  The per-reason cap alone let 13
# reasons write 52 lines a minute, enough to fill the 1000-line journal
# queue behind a blocked console in about 19 minutes.  These are only ever
# rejection lines: arm and disarm events, faults and "show program stopped
# answering" are written elsewhere and never pass through here (and
# journal.Journal keeps half its queue that arm-link lines cannot use).
GLOBAL_LINES_PER_MINUTE = 8
_ADDRS_TRACKED = 1000      # distinct source addresses counted per episode

# The stable reason a decode rejection is throttled under.  link.decode_arm's
# messages can carry text the SENDER chose (a contract version, a message
# type, a length), so the raw message is never the key: a sender varying it
# would get a fresh episode, and a fresh line, every datagram.
_DECODE_REASONS = ("not bytes", "datagram too long", "not valid JSON",
                   "not a JSON object", "wrong contract version",
                   "wrong key", "wrong message type", "seq is not",
                   "wanted is not", "names is not")


def _decode_reason(msg):
    for r in _DECODE_REASONS:
        if msg.startswith(r):
            return r
    return "other"


class _RejectJournal:
    """One journal line per reason per episode, plus a closing count, with
    a hard per-reason cap on lines per minute (round 4, item C).  Pure
    bookkeeping on a clock the caller passes in; writes through `event`
    (kind, msg) and never raises."""

    def __init__(self, event, quiet_s=EPISODE_QUIET_S,
                 per_minute=LINES_PER_MINUTE,
                 global_per_minute=GLOBAL_LINES_PER_MINUTE):
        self._event = event
        self.quiet_s = quiet_s
        self.per_minute = per_minute
        self.global_per_minute = global_per_minute
        self._episodes = {}     # reason -> {"at", "count", "addrs", "opened"}
        self._lines = {}        # reason -> [clock of each line, last 60 s]
        self._all_lines = []    # clock of every line, any reason, last 60 s
        self._unlogged = {}     # reason -> rejections no line has counted

    def active(self, reason):
        return reason in self._episodes

    def _allow(self, reason, now):
        times = [t for t in self._lines.get(reason, ()) if now - t < 60.0]
        self._lines[reason] = times
        self._all_lines = [t for t in self._all_lines if now - t < 60.0]
        if len(times) >= self.per_minute or \
                len(self._all_lines) >= self.global_per_minute:
            return False
        times.append(now)
        self._all_lines.append(now)
        return True

    def _carry(self, reason):
        n = self._unlogged.pop(reason, 0)
        if not n:
            return ""
        return (f" ({n} earlier rejection{'s' if n != 1 else ''} for this "
                f"same reason went unlogged while arm-link rejection lines "
                f"were capped at {self.per_minute} a minute per reason and "
                f"{self.global_per_minute} a minute in all)")

    def note(self, reason, now, addr, opening):
        """One rejection.  `opening` is the sentence written if this starts
        a new episode (and the per-minute cap allows a line)."""
        ep = self._episodes.get(reason)
        if ep is None:
            ep = {"at": now, "count": 0, "addrs": set(), "opened": False}
            self._episodes[reason] = ep
            if self._allow(reason, now):
                ep["opened"] = True
                self._event("arm-link",
                            f"{opening} Further rejections for this same "
                            f"reason, from this or any other source "
                            f"address, will not be logged individually "
                            f"until none has arrived for "
                            f"{self.quiet_s:g} s.{self._carry(reason)}")
        ep["at"] = now
        ep["count"] += 1
        if len(ep["addrs"]) < _ADDRS_TRACKED:
            ep["addrs"].add(addr)

    def sweep(self, now, closing):
        """Close every episode quiet for quiet_s.  `closing(reason, n_addrs,
        count)` builds the closing sentence."""
        for reason in [r for r, e in self._episodes.items()
                       if now - e["at"] > self.quiet_s]:
            ep = self._episodes.pop(reason)
            unlogged = ep["count"] - (1 if ep["opened"] else 0)
            if unlogged <= 0:
                continue
            if self._allow(reason, now):
                n = len(ep["addrs"])
                self._event("arm-link",
                            closing(reason, f"{n}{'+' if n >= _ADDRS_TRACKED else ''}"
                                    f" distinct source address"
                                    f"{'es' if n != 1 else ''}",
                                    ep["count"]) + self._carry(reason))
            else:
                self._unlogged[reason] = (self._unlogged.get(reason, 0)
                                          + unlogged)

    def reset(self):
        self._episodes = {}
        self._lines = {}
        self._all_lines = []
        self._unlogged = {}


class ArmAssertion:
    """What an input currently asserts.  `names` is optional and, when
    given, must match the composer's group names in order.

    `forced` (added round 3 of the safety review, item 1) is optional: one
    bool per group, True where THIS assertion's False bit was produced by
    the foreign-disarm AND below, not actually reported as False by the
    locked sender itself.  None (the default) means "nothing forced",
    exactly like a plain False vector -- every driver except
    SocketArmInput (when a foreign sender is interfering) leaves this
    unset.  The composer uses it to tell a FORCED low from a genuine one:
    only a genuine low may ever set up a future consent edge (see
    composer.assert_arm and the module docstring's FORCED-EDGE section
    below)."""
    __slots__ = ("wanted", "seq", "names", "forced", "sender")

    def __init__(self, wanted, seq, names=None, forced=None, sender=None):
        self.wanted = tuple(bool(w) for w in wanted)
        self.seq = int(seq)
        self.names = None if names is None else tuple(str(n) for n in names)
        self.forced = (None if forced is None
                       else tuple(bool(f) for f in forced))
        # Round 4 of the safety review: who this came from, for an input
        # that has more than one possible sender (SocketArmInput: the
        # locked (ip, port)). None for every other input. The composer
        # never lets a consent edge span two different senders.
        self.sender = sender


class ArmInput:
    """The interface.  poll() is called once per tick and returns the
    input's current assertion, or None when it has nothing to assert (not
    connected, not started).  See the module docstring for the rules."""

    def open(self):
        pass

    def poll(self):
        return None

    def close(self):
        pass

    @property
    def foreign_count(self):
        """How many OTHER senders this input is currently tracking as
        still fresh (round 3 of the safety review, item 6).  Zero for
        every input that has no concept of one; only SocketArmInput
        overrides this.  The service reads it every tick, independent of
        poll()'s own return value, and feeds it to the composer's status
        frame so the deck can raise its own alarm the instant a foreign
        sender is interacting with the link at all -- even on a tick where
        the AND happens to leave `wanted` looking exactly like what the
        deck itself expects, which is the case the second line of defence
        (streamdeck.py's _spoof_reason) could not see without this."""
        return 0

    @property
    def flooded(self):
        """True while the input has seen a flood of datagrams recently
        (round 4 of the safety review, item B).  False for every input
        that has no socket; only SocketArmInput overrides this."""
        return False


class NullArmInput(ArmInput):
    """Never asserts anything.  Every group stays disarmed.  What flamesafe
    runs with when link.arm_port is not configured at all."""


class ScriptedArmInput(ArmInput):
    """The test driver.  Holds a wanted vector and advances its counter on
    every poll while `alive`; the tests set it, freeze it, silence it and
    reboot it to exercise every liveness rule."""

    def __init__(self, n, names=None):
        self.n = n
        self.names = None if names is None else tuple(names)
        self.wanted = [False] * n
        self.forced = [False] * n   # test-only: which bits to mark forced
                                   # on the NEXT poll() (round 3, item 1)
        self.seq = 0
        self.alive = True        # advance the counter on each poll
        self.silent = False      # return None on each poll
        self.polls = 0

    def set(self, *groups, on=True):
        for g in groups:
            self.wanted[g] = on

    def set_forced(self, *groups, on=True):
        """Test-only: mark `groups` as forced on the next poll(), simulating
        what SocketArmInput's foreign-disarm AND would report. Never clears
        itself; the test sets it back to off once the simulated foreign
        interference stops."""
        for g in groups:
            self.forced[g] = on

    def set_all(self, on):
        self.wanted = [bool(on)] * self.n

    def freeze(self):
        self.alive = False

    def thaw(self):
        self.alive = True

    def reboot(self):
        self.seq = 0
        self.alive = True
        self.silent = False

    def poll(self):
        self.polls += 1
        if self.silent:
            return None
        if self.alive:
            self.seq += 1
        return ArmAssertion(self.wanted, self.seq, self.names,
                            forced=self.forced)


class SocketArmInput(ArmInput):
    """The real build-step-7b driver: one keyed loopback UDP socket, bound
    on open(), read non-blockingly on every poll().  The Stream Deck lives
    in ltcplay's own process (ltcplay/streamdeck.py) and sends one arm
    frame (link.encode_arm, hand-written there from CONTRACT.md -- it never
    imports this module, see test_the_wall_from_this_side) at 10 Hz or
    faster, always carrying every group's current wanted state and the
    group names, whether or not anything changed.

    poll() drains everything waiting and returns only the LAST one it could
    decode, as one ArmAssertion; a flood never backs up into a queue that
    grows faster than it drains.  A datagram this config cannot even decode
    (wrong key, wrong shape) is rejected here and journaled.  A datagram
    that decodes fine but whose group NAMES do not match this config's is a
    separate check, one layer up in composer.assert_arm; it is also
    journaled, by the composer itself (build step 7b's safety review found
    the prior code dropped that one in total silence, despite an earlier
    version of this docstring claiming it was "journaled exactly like a
    rejected flame frame" -- it was not, and the claim is fixed here along
    with the code).

    THE SENDER LOCK (added after a second safety review, 2026-10-01).  An
    earlier version of this class had none, on the reasoning: "the cost of
    being wrong here is a DISARM ... never a fire." Running the actual code
    proved that false. `wanted` can ask for EITHER state, so a second local
    process that has read flamesafe's config -- the key lives in a file,
    it is not a secret in the cryptographic sense, CONTRACT.md says so --
    can send `wanted=True` exactly as easily as `wanted=False`. Worse,
    service.py's own drain keeps only the LAST datagram it could decode
    each tick: a rogue sender racing the real Stream Deck can win a tick
    outright. If the operator's own Abort sends `wanted` all false and a
    rogue frame lands after it in the same tick, or the rogue simply keeps
    re-asserting `True` faster than anyone is watching for it, the composer
    sees only the rogue's `True` that tick, and a group can read ARMED
    again a moment after the operator just told it not to be: an Abort
    visibly undone by a datagram the operator never sent, on the very
    screen they are watching. That is not "only ever a disarm"; it is a
    way to MASK an Abort, which is the worst of both failure directions at
    once.

    So poll() now does exactly what composer.ingest_frame already does for
    the flame-frame link: while a datagram has been accepted inside
    `stale_ms` of another, only that SAME (ip, port) is accepted; a
    datagram from anywhere else is rejected as `another sender` and
    journaled, and changes nothing here or in the composer. Once nothing
    has been accepted for `stale_ms`, the lock releases and the next
    sender to decode cleanly takes it over. **This does NOT bound how long
    a rogue can hold the lock once it is in**: a rogue that keeps
    re-asserting faster than `stale_ms` holds the lock indefinitely, same
    as the real deck would. The lock only ever disallows a SECOND sender
    from being accepted while a first one is live; it says nothing about
    how the first one got there. A third safety review found this the hard
    way (round 2): if the real deck goes quiet for `stale_ms` -- a
    reconnect, a restart, ordinary boot ordering -- a rogue racing it can
    become the locked sender itself, for real, and arm groups the operator
    never asked for; the real deck's own Abort then arrives as "another
    sender" and is rejected outright. See the module docstring's FOREIGN
    DISARM section for the fix: a rejected foreign frame can still force a
    group's `wanted` bit to False in whatever this call returns, which is
    what actually closes that hole; the lock by itself only ever answered
    "is this the sender I already trust", never "should a rogue's ARM be
    trusted", and never claimed to. This lock lives entirely in this file,
    never in composer.py: every arm/disarm/dwell/chatter/consent rule there
    is unchanged by this fix on purpose, and the lock holder still has to
    prove consent (rule 6) all over again, exactly as before."""

    def __init__(self, listen_ip, listen_port, key, n, log=None,
                stale_ms=DEFAULT_STALE_MS, clock=time.perf_counter):
        self._ip = listen_ip
        self._port = listen_port
        self._key = key
        self._n = n
        self._log = log
        self._stale_ms = stale_ms
        self._clock = clock
        self._sock = None
        self._sender = None       # (ip, port) locked while a datagram from
                                  # it has been accepted inside stale_ms
        self._sender_at = None    # our own clock, last accepted datagram
        # Foreign-disarm tracking (round 2 of the safety review): addr ->
        # {"wanted": tuple, "at": our clock}. Never the lock holder; only
        # ever ANDed (bits cleared, never set) into whatever this poll()
        # call returns. Purely for that AND -- journaling is tracked
        # separately below (round 3, item 5).
        self._foreign = {}
        # Journal rate-limiting for "another sender" rejections (round 3 of
        # the safety review, item 5): one line per REASON per episode, with
        # a running count, not one per rejecting ADDRESS. Round 2's own
        # fix (the per-address dict above) still logged once per NEW
        # address the first time it was seen, which a rogue varying its own
        # source port on every single frame turns back into a flood (a
        # real attack proved this: ~26,000 lines in 5 s from 4,000 ports).
        # Round 4 (item C) moved this, and every decode rejection too, into
        # one _RejectJournal: one line per reason per episode, an episode
        # ends only after EPISODE_QUIET_S of quiet (not stale_ms, which a
        # datagram every 520 ms defeated), and a hard per-minute cap.
        self._rejects = _RejectJournal(self._event)
        # Round 4 (item B): our clock at the last poll that found a flood
        # (more than FLOOD_DATAGRAMS_PER_POLL datagrams waiting), and
        # whether that is still inside stale_ms as of the last poll.
        self._flood_at = None
        self._flooded = False
        self.rcvbuf = None        # what the kernel really gave open() (r5)

    def open(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((self._ip, self._port))
        sock.setblocking(False)
        # Round 5, item 4: see ARM_RCVBUF_BYTES.  The kernel may cap it
        # (Linux at net.core.rmem_max); `rcvbuf` says what it really gave.
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF,
                            ARM_RCVBUF_BYTES)
        except OSError:
            pass
        try:
            self.rcvbuf = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        except OSError:
            self.rcvbuf = None
        self._sock = sock
        self._sender = None
        self._sender_at = None
        self._foreign = {}
        self._rejects.reset()
        self._flood_at = None
        self._flooded = False

    def poll(self):
        sock = self._sock
        if sock is None:
            return None
        now = self._clock()
        if self._sender is not None and self._sender_at is not None and \
                (now - self._sender_at) * 1000.0 > self._stale_ms:
            # Nothing accepted from the locked sender for stale_ms: release
            # it, exactly as the flame-frame link releases its own lock
            # once frame_stale_ms has passed (CONTRACT.md).
            self._sender = None
            self._sender_at = None
        # Close out every rejection EPISODE that has been quiet for
        # EPISODE_QUIET_S (item 10, round 3 item 5, round 4 item C: one
        # summary line per reason per episode, however many distinct
        # addresses were involved and however the datagrams were spaced).
        self._rejects.sweep(now, self._closing_line)
        # Separately, drop any per-address AND-clear tracking that has gone
        # stale -- this never touches the journal, only which `wanted`
        # vectors are still ANDed in below.
        for addr in [a for a, e in self._foreign.items()
                    if (now - e["at"]) * 1000.0 > self._stale_ms]:
            del self._foreign[addr]
        best = None
        n_read = 0
        n_bytes = 0
        for _ in range(DRAIN_PER_TICK):
            try:
                data, addr = sock.recvfrom(65535)
            except BlockingIOError:
                break
            except ConnectionResetError:
                # Windows: a peer's ICMP port-unreachable from an earlier
                # send landing on a read. Not this link's business.
                continue
            except OSError:
                break
            n_read += 1
            n_bytes += len(data)
            addr = tuple(addr[:2])
            try:
                wanted, seq, names = link.decode_arm(data, self._n, self._key)
            except link.LinkError as e:
                # Round 4, item C: this used to journal one line per
                # datagram -- no key needed, ~39,000 lines in 5 s, enough
                # to push "show program stopped answering" out of the
                # bounded journal queue behind a blocked console.
                msg = str(e)
                self._rejects.note(
                    "decode:" + _decode_reason(msg), now, addr,
                    f"arm frame rejected: {msg[:120]} (from {addr[0]}:"
                    f"{addr[1]}).")
                continue
            if self._sender is None:
                self._sender = addr
            elif addr != self._sender:
                # Round 2 of the safety review (item 1): rejected for every
                # purpose EXCEPT disarming -- this sender never becomes the
                # lock holder, never advances seq/names/consent -- but its
                # `wanted` is remembered so a real "disarm" from it still
                # takes effect below, even while a rogue holds the lock.
                self._note_foreign(addr, wanted, now)
                continue
            self._sender_at = now
            best = ArmAssertion(wanted, seq, names, sender=addr)
        if n_read > FLOOD_DATAGRAMS_PER_POLL or \
                n_bytes > FLOOD_BYTES_PER_POLL:
            # Round 4, item B: see FLOOD_DATAGRAMS_PER_POLL.  Counted over
            # EVERY datagram, keyed or not: a flood of garbage crowds the
            # real deck out of the receive buffer just as well as a keyed
            # one, and either way no honest sender produced it.  Round 5,
            # item 4: in bytes as well (FLOOD_BYTES_PER_POLL), since the
            # buffer fills by bytes.
            self._flood_at = now
            self._rejects.note(
                "flood", now, ("*", 0),
                f"arm link flooded: {n_read} datagrams ({n_bytes} bytes) "
                f"were waiting in one poll (an honest deck sends a few "
                f"small ones). No group can be newly "
                f"armed while this lasts, nor until it has stopped for "
                f"{self._stale_ms} ms; a group already armed stays armed "
                f"only as long as its own input stays live.")
        self._flooded = (self._flood_at is not None and
                         (now - self._flood_at) * 1000.0 <= self._stale_ms)
        if best is not None:
            best = self._apply_foreign_clears(best, now)
        return best

    def _closing_line(self, reason, addrs, count):
        if reason == "another sender":
            return (f"arm frames from another sender ({addrs}) stopped "
                    f"after {count} rejected in a row")
        if reason == "flood":
            return (f"arm link flood ended after {count} flooded "
                    f"poll{'s' if count != 1 else ''}")
        return (f"arm frames rejected for '{reason[len('decode:'):]}' "
                f"({addrs}) stopped after {count} rejected")

    @property
    def flooded(self):
        """True while a flood (FLOOD_DATAGRAMS_PER_POLL) has been seen
        inside stale_ms, as of the last poll().  The service passes this
        to composer.note_arm_link_flooded every tick (round 4, item B)."""
        return self._flooded

    def _note_foreign(self, addr, wanted, now):
        # AND-clear tracking: per-address, since each foreign sender's own
        # `wanted` vector has to be ANDed in separately (round 2).
        e = self._foreign.get(addr)
        if e is None:
            self._foreign[addr] = {"wanted": tuple(bool(w) for w in wanted),
                                   "at": now}
        else:
            e["wanted"] = tuple(bool(w) for w in wanted)
            e["at"] = now
        # Journal rate-limiting: by REASON, across every address, not per
        # address (round 3, item 5: keying this on the address, even
        # indirectly via "is this address new", is exactly what a rogue
        # varying its own source port defeats).
        # Round 4 (item C): the same _RejectJournal every other arm-link
        # rejection now goes through, with its longer quiet window and its
        # per-minute cap.
        self._rejects.note(
            "another sender", now, addr,
            f"arm frame rejected: another sender ({addr[0]}:{addr[1]} is "
            f"not the locked sender {self._sender[0]}:{self._sender[1]}); "
            f"its disarm bits still apply, and no group can be newly armed "
            f"while it is on the link.")

    def _apply_foreign_clears(self, assertion, now):
        """AND every still-fresh foreign sender's `wanted` into `assertion`,
        bit for bit: a foreign False forces that group False in the result,
        a foreign True never sets anything (the locked sender's own value
        stands). seq and names are always the locked sender's own; only the
        wanted vector can be narrowed here.

        Round 3 of the safety review (item 1): also records, per group,
        whether THIS call actually forced that bit from True to False --
        the `forced` vector on the returned ArmAssertion.  A third review
        found that round 2's AND-only-ever-clears fix, by itself, was
        exploitable the other way: a foreign sender sending a group's bit
        False for a while and then True again put a False-then-True
        sequence in front of the composer that looks exactly like the
        operator cycling the arm, even though the locked sender (the real
        deck) never stopped asking for True the whole time.  The composer
        cannot tell a forced low from a genuine one unless this file says
        so; `forced` is that signal, and it must never be set for a group
        whose False came from the locked sender's own report (there is
        nothing to force in that case: the AND changes nothing)."""
        wanted = list(assertion.wanted)
        forced = [False] * len(wanted)
        changed = False
        for e in self._foreign.values():
            if (now - e["at"]) * 1000.0 > self._stale_ms:
                continue
            fw = e["wanted"]
            for i in range(min(len(wanted), len(fw))):
                if not fw[i] and wanted[i]:
                    wanted[i] = False
                    forced[i] = True
                    changed = True
        if not changed:
            return assertion
        return ArmAssertion(wanted, assertion.seq, assertion.names,
                            forced=forced, sender=assertion.sender)

    def close(self):
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        self._sender = None
        self._sender_at = None
        self._foreign = {}
        self._rejects.reset()
        self._flood_at = None
        self._flooded = False

    @property
    def foreign_count(self):
        """How many other senders are currently tracked as still fresh
        (round 3 of the safety review, item 6), as of the last poll()'s own
        cleanup.  Reading this right after poll() (as service.py does,
        every tick) means it reflects THIS tick's own view, whether or not
        poll() itself had anything new to decode and return."""
        return len(self._foreign)

    def _event(self, kind, msg):
        if self._log is None:
            return
        try:
            self._log.event(kind, msg)
        except Exception:                               # noqa: BLE001
            pass
