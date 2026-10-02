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
    __slots__ = ("wanted", "seq", "names", "forced")

    def __init__(self, wanted, seq, names=None, forced=None):
        self.wanted = tuple(bool(w) for w in wanted)
        self.seq = int(seq)
        self.names = None if names is None else tuple(str(n) for n in names)
        self.forced = (None if forced is None
                       else tuple(bool(f) for f in forced))


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
        # {"at": our clock, "count": total rejections, "addrs": set()} or
        # None between episodes.
        self._foreign_episode = None

    def open(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((self._ip, self._port))
        sock.setblocking(False)
        self._sock = sock
        self._sender = None
        self._sender_at = None
        self._foreign = {}
        self._foreign_episode = None

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
        # Close out the foreign-sender rejection EPISODE once nothing
        # foreign has been rejected, from ANY address, for stale_ms (item
        # 10, strengthened round 3 item 5: one summary line per episode
        # however many distinct addresses were involved, not one per
        # address -- see _foreign_episode's own comment).
        ep = self._foreign_episode
        if ep is not None and (now - ep["at"]) * 1000.0 > self._stale_ms:
            self._foreign_episode = None
            if ep["count"] > 1:
                n_addrs = len(ep["addrs"])
                self._event("arm-link",
                            f"arm frames from another sender ({n_addrs} "
                            f"distinct source address"
                            f"{'es' if n_addrs != 1 else ''}) stopped "
                            f"after {ep['count']} rejected in a row")
        # Separately, drop any per-address AND-clear tracking that has gone
        # stale -- this never touches the journal, only which `wanted`
        # vectors are still ANDed in below.
        for addr in [a for a, e in self._foreign.items()
                    if (now - e["at"]) * 1000.0 > self._stale_ms]:
            del self._foreign[addr]
        best = None
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
            addr = tuple(addr[:2])
            try:
                wanted, seq, names = link.decode_arm(data, self._n, self._key)
            except link.LinkError as e:
                self._event("arm-link", f"arm frame rejected: {e}")
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
            best = ArmAssertion(wanted, seq, names)
        if best is not None:
            best = self._apply_foreign_clears(best, now)
        return best

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
        # address (round 3, item 5 -- see _foreign_episode's own comment:
        # keying this on the address, even indirectly via "is this address
        # new", is exactly what a rogue varying its own source port defeats).
        ep = self._foreign_episode
        if ep is None:
            self._foreign_episode = {"at": now, "count": 1, "addrs": {addr}}
            self._event("arm-link",
                        f"arm frame rejected: another sender ({addr[0]}:"
                        f"{addr[1]} is not the locked sender "
                        f"{self._sender[0]}:{self._sender[1]}); its "
                        f"disarm bits still apply. Further rejections for "
                        f"this same reason -- from this or any other "
                        f"source address -- will not be logged "
                        f"individually until it stops for {self._stale_ms} "
                        f"ms.")
        else:
            ep["at"] = now
            ep["count"] += 1
            ep["addrs"].add(addr)

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
                            forced=forced)

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
        self._foreign_episode = None

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
