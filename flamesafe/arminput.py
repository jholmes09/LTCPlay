"""flamesafe arm input: the interface the Stream Deck (build step 7b) will
drive, and a scripted driver for the tests.

Arming is a value asserted continuously.  An input does not send "arm" once;
it keeps saying "I want these groups armed" with a counter that advances,
and the composer treats the absence of a fresh assertion as disarmed.  So an
input that is unplugged, hung, or has crashed disarms every group inside
arm_stale_ms without anyone doing anything.

NOTHING IN THIS FILE ARMS FROM A REAL INPUT.  NullArmInput is what the
service runs with until 7b lands; ScriptedArmInput exists for the tests.
"""

from __future__ import annotations


class ArmAssertion:
    """What an input currently asserts."""
    __slots__ = ("wanted", "seq")

    def __init__(self, wanted, seq):
        self.wanted = tuple(bool(w) for w in wanted)
        self.seq = int(seq)


class ArmInput:
    """The interface.  poll() is called once per tick and returns the
    input's current assertion, or None when it has nothing to assert (not
    connected, not started).  The assertion's seq must advance at least
    every arm_stale_ms while the input is alive; a driver that asserts at
    10 Hz or faster is comfortably inside the default 500 ms."""

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

    def __init__(self, n):
        self.n = n
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
        return ArmAssertion(self.wanted, self.seq)
