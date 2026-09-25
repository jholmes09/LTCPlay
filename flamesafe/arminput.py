"""flamesafe arm input: the interface the Stream Deck (build step 7b) will
drive, and a scripted driver for the tests.

Arming is a value asserted continuously.  An input does not send "arm" once;
it keeps saying "I want these groups armed" with a counter that advances,
and the composer treats the absence of a fresh assertion as disarmed.  So an
input that is unplugged, hung, or has crashed disarms every group inside
arm_stale_ms without anyone doing anything.

RULES FOR A REAL DRIVER (7b), each one backed by a composer rule:

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
     wire, is always one press away.

NOTHING IN THIS FILE ARMS FROM A REAL INPUT.  NullArmInput is what the
service runs with until 7b lands; ScriptedArmInput exists for the tests.
"""

from __future__ import annotations


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

    def poll(self):
        return None

    def close(self):
        pass


class NullArmInput(ArmInput):
    """Never asserts anything.  Every group stays disarmed."""


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
