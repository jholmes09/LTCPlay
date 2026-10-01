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
"""

from __future__ import annotations

import socket

from . import link

# Datagrams drained per poll.  A flood beyond this waits for the next tick;
# only the last one decoded this call is kept, matching service.py's own
# _drain() for the flame-frame link.
DRAIN_PER_TICK = 200


class ArmAssertion:
    """What an input currently asserts.  `names` is optional and, when
    given, must match the composer's group names in order."""
    __slots__ = ("wanted", "seq", "names")

    def __init__(self, wanted, seq, names=None):
        self.wanted = tuple(bool(w) for w in wanted)
        self.seq = int(seq)
        self.names = None if names is None else tuple(str(n) for n in names)


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
        self.seq = 0
        self.alive = True        # advance the counter on each poll
        self.silent = False      # return None on each poll
        self.polls = 0

    def set(self, *groups, on=True):
        for g in groups:
            self.wanted[g] = on

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
        return ArmAssertion(self.wanted, self.seq, self.names)


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
    grows faster than it drains.  A datagram this config cannot accept
    (wrong key, wrong shape, wrong group names) is rejected and journaled,
    exactly like a rejected flame frame, and changes nothing: it is simply
    not there, which is the same as the deck not having sent it.

    No sender lock, unlike the flame-frame link.  There is exactly one
    Stream Deck in this show, the key already keeps out anything that has
    not read flamesafe's config, and the cost of being wrong here is a
    DISARM (rule 6, consent, still has to be re-proved), never a fire --
    the asymmetry that justifies the flame link's own extra lock does not
    apply to an input that can only ever ask for less."""

    def __init__(self, listen_ip, listen_port, key, n, log=None):
        self._ip = listen_ip
        self._port = listen_port
        self._key = key
        self._n = n
        self._log = log
        self._sock = None

    def open(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((self._ip, self._port))
        sock.setblocking(False)
        self._sock = sock

    def poll(self):
        sock = self._sock
        if sock is None:
            return None
        best = None
        for _ in range(DRAIN_PER_TICK):
            try:
                data, _addr = sock.recvfrom(65535)
            except BlockingIOError:
                break
            except ConnectionResetError:
                # Windows: a peer's ICMP port-unreachable from an earlier
                # send landing on a read. Not this link's business.
                continue
            except OSError:
                break
            try:
                wanted, seq, names = link.decode_arm(data, self._n, self._key)
            except link.LinkError as e:
                self._event("arm-link", f"arm frame rejected: {e}")
                continue
            best = ArmAssertion(wanted, seq, names)
        return best

    def close(self):
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _event(self, kind, msg):
        if self._log is None:
            return
        try:
            self._log.event(kind, msg)
        except Exception:                               # noqa: BLE001
            pass
