"""flamesafe service: sockets and pacing around the composer.

One loop, paced on perf_counter.  Each tick: drain ltcplay's datagrams,
poll the arm input, compose, send the universe by sACN at priority 200,
send the status frame.  The composer decides every value; this file only
moves bytes.

WHAT A HARD KILL DOES.  A clean stop (Ctrl-C, SIGTERM, the stop event)
sends zeros and then the stream-terminated flag.  A hard kill (Task
Manager's End task, a crash of the interpreter, power) sends nothing: the
last packet on the wire stands until the node's own sACN-loss timeout, and
a flame that was on stays on until the head's Max. Flame Duration ends it.
Those two settings are the bounds, and both are bench items (CONTRACT.md).
On Windows only Ctrl-C and Ctrl-Break reach the stop handler; build step 7b
must give the operator an in-band stop (a key, or a message) that sets the
stop event.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time

from . import rules
from .composer import (Composer, now, FLAME_FLOOD_BYTES_PER_TICK,
                       FLAME_FLOOD_DATAGRAMS_PER_TICK)
from .link import DisarmAll, LinkError, decode_from_ltcplay, encode_status
from .sacn import build_packet

# Datagrams drained per tick.  A flood beyond this waits for the next tick
# rather than stalling this one; staleness and sequence checks do the rest.
DRAIN_PER_TICK = 200
SHUTDOWN_ZERO_FRAMES = 3
SHUTDOWN_TERMINATE_FRAMES = 3


SIO_UDP_CONNRESET = 0x9800000C      # _WSAIOW(IOC_VENDOR, 12), winsock2

# The lockout file (Jeff, 2026-10-17): how often its date is looked at, and
# the one format read.  ltcplay's engine is the one writer (an atomic
# replace); this program only ever reads it.
LOCKOUT_POLL_S = 0.5
LOCKOUT_FORMAT = 1
LOCKOUT_MAX_BYTES = 256 * 1024


class LockoutFile:
    """Reads the lockout file and says which fire slots are locked.

    poll(now) returns (slots, state).  `state` is "none" (no path), "read",
    "missing" (no file: nothing locked) or "unreadable" (the file is there
    but cannot be read or is not the format: the LAST set read is kept,
    the safe side, and it is journaled once per episode).  Never raises."""

    def __init__(self, path, log=None, clock=None):
        self.path = path
        self._log = log
        self._clock = clock or time.perf_counter
        self._next_at = None
        self._stamp = None
        self._slots = frozenset()
        self._state = "none" if not path else "missing"
        self._unreadable_said = False

    def poll(self, now=None):
        if not self.path:
            return self._slots, "none"
        t = self._clock() if now is None else now
        if self._next_at is not None and t < self._next_at:
            return self._slots, self._state
        self._next_at = t + LOCKOUT_POLL_S
        try:
            st = os.stat(self.path)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if stamp == self._stamp and self._state in ("read", "missing"):
            return self._slots, self._state
        self._stamp = stamp
        if stamp is None:
            if self._slots:
                self._event("lockout", f"the lockout file {self.path} is "
                                       f"gone: nothing is locked out any "
                                       f"more")
            self._slots = frozenset()
            self._state = "missing"
            self._unreadable_said = False
            return self._slots, self._state
        try:
            if stamp[1] > LOCKOUT_MAX_BYTES:
                raise ValueError("too large")
            with open(self.path, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
            slots = _lockout_slots(doc)
        except (OSError, ValueError, TypeError) as e:
            if not self._unreadable_said:
                self._unreadable_said = True
                self._event("lockout", f"the lockout file {self.path} could "
                                       f"not be read ({type(e).__name__}: "
                                       f"{e}); keeping the last lock set, "
                                       f"{sorted(self._slots) or 'none'}")
            self._state = "unreadable"
            return self._slots, self._state
        self._unreadable_said = False
        self._slots = slots
        self._state = "read"
        return self._slots, self._state

    def _event(self, kind, msg):
        if self._log is None:
            return
        try:
            self._log.event(kind, msg)
        except Exception:                               # noqa: BLE001
            pass


def _lockout_slots(doc):
    """The locked fire slots in a lockout document, or ValueError."""
    if not isinstance(doc, dict) or doc.get("lockout_format") != LOCKOUT_FORMAT:
        raise ValueError("not a lockout file (lockout_format)")
    chans = doc.get("flame_channels")
    if not isinstance(chans, list):
        raise ValueError("flame_channels is not a list")
    out = set()
    for c in chans:
        if isinstance(c, bool) or not isinstance(c, int) \
                or not 1 <= c <= rules.UNIVERSE_SIZE:
            raise ValueError(f"flame channel {c!r} is not a slot 1 to "
                             f"{rules.UNIVERSE_SIZE}")
        out.add(c)
    return frozenset(out)


def _no_connreset(sock):
    """Windows: a UDP socket that has sent to a closed port gets an ICMP
    port-unreachable back and then raises ConnectionResetError on its NEXT
    operation, including a send to somewhere else.  SIO_UDP_CONNRESET off
    stops that.  CPython's socket module exposes no constant for it, so
    this goes straight to WSAIoctl through ctypes, as asyncio does.

    Returns True when the ioctl succeeded, False when it failed (with the
    Winsock error journaled by the caller), None on any other platform."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        ws2 = ctypes.WinDLL("ws2_32", use_last_error=True)
        wsaioctl = ws2.WSAIoctl
        wsaioctl.argtypes = [ctypes.c_size_t,          # SOCKET
                             ctypes.c_ulong,           # DWORD dwIoControlCode
                             ctypes.c_void_p,          # LPVOID lpvInBuffer
                             ctypes.c_ulong,           # DWORD cbInBuffer
                             ctypes.c_void_p,          # LPVOID lpvOutBuffer
                             ctypes.c_ulong,           # DWORD cbOutBuffer
                             ctypes.POINTER(ctypes.c_ulong),  # LPDWORD
                             ctypes.c_void_p,          # LPWSAOVERLAPPED
                             ctypes.c_void_p]          # completion routine
        wsaioctl.restype = ctypes.c_int
        off = ctypes.c_ulong(0)                        # BOOL FALSE
        returned = ctypes.c_ulong(0)
        rc = wsaioctl(sock.fileno(), SIO_UDP_CONNRESET, ctypes.byref(off),
                      ctypes.sizeof(off), None, 0, ctypes.byref(returned),
                      None, None)
        return rc == 0
    except Exception:                                   # noqa: BLE001
        return False


class Service:

    def __init__(self, config, arm_input, clock=now, log=None,
                 sleep=time.sleep):
        self.cfg = config
        self.arm_input = arm_input
        self._clock = clock
        self._sleep = sleep         # injected with the clock, for the tests
        self._log = log
        self.composer = Composer(config, clock=clock, log=log)
        self.lockout = LockoutFile(getattr(config, "lockout_file", None),
                                   log=log, clock=clock)
        self._rx = None
        self._tx = None
        self._status_tx = None
        self.sacn_seq = 0
        self.sent_packets = 0
        self.send_errors = 0
        self.status_errors = 0
        self.mirror_errors = 0
        self.input_errors = 0
        self.last_output = None

    # -------------------------------------------------------------- sockets

    def open(self):
        self.arm_input.open()
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # On Windows SO_REUSEADDR would let a second program bind our port
        # and take the frames; SO_EXCLUSIVEADDRUSE forbids that.  On POSIX a
        # plain bind is already exclusive for UDP.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            rx.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        rx.bind((self.cfg.link_listen_ip, self.cfg.link_listen_port))
        rx.setblocking(False)
        self._rx = rx
        self._tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._status_tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for name, s in (("frames", rx), ("sACN", self._tx),
                        ("status", self._status_tx)):
            if _no_connreset(s) is False:
                self._event("socket", f"SIO_UDP_CONNRESET could not be "
                                      f"switched off on the {name} socket; "
                                      f"an ICMP port-unreachable may raise "
                                      f"on it")
        self._event("start", f"flame universe {self.cfg.universe} to "
                             f"{self.cfg.destination_ip}:"
                             f"{self.cfg.destination_port} at sACN priority "
                             f"{rules.SACN_PRIORITY}, {self.cfg.tick_hz} Hz; "
                             f"frames in on {self.cfg.link_listen_ip}:"
                             f"{self.cfg.link_listen_port}, status out to "
                             f"{self.cfg.link_status_ip}:"
                             f"{self.cfg.link_status_port}")

    def close(self):
        """Zeros on the wire, then the stream terminated, then the sockets.
        Only a clean stop gets here; see the module docstring."""
        try:
            if self._tx is not None:
                zeros = bytes(rules.UNIVERSE_SIZE)
                for _ in range(SHUTDOWN_ZERO_FRAMES):
                    self._send_universe(zeros)
                for _ in range(SHUTDOWN_TERMINATE_FRAMES):
                    self._send_universe(zeros, terminated=True)
        finally:
            for s in (self._rx, self._tx, self._status_tx):
                try:
                    if s is not None:
                        s.close()
                except OSError:
                    pass
            self._rx = self._tx = self._status_tx = None
            try:
                self.arm_input.close()
            except Exception:                           # noqa: BLE001
                pass
            self._event("stop", "flame universe zeroed and the stream "
                                "terminated")

    # ----------------------------------------------------------------- tick

    def _drain(self):
        rx = self._rx
        if rx is None:
            return
        # Fix round 1 of PR #40: count what is waiting, keyed or not, so a
        # flood blocks consent the way it does on the arm link.
        counts = [0, 0]
        try:
            self._drain_into(rx, counts)
        finally:
            n_read, n_bytes = counts
            self.composer.note_flame_link_flooded(
                n_read > FLAME_FLOOD_DATAGRAMS_PER_TICK
                or n_bytes > FLAME_FLOOD_BYTES_PER_TICK, n_read, n_bytes)

    def _drain_into(self, rx, counts):
        for _ in range(DRAIN_PER_TICK):
            try:
                data, addr = rx.recvfrom(65535)
            except BlockingIOError:
                return
            except ConnectionResetError:
                # Windows reports a peer's ICMP port-unreachable here.
                continue
            except OSError:
                return
            counts[0] += 1
            counts[1] += len(data)
            try:
                msg = decode_from_ltcplay(data, self.cfg.universe,
                                          self.cfg.link_key)
            except LinkError as e:
                self.composer.reject_frame(str(e), sender=tuple(addr[:2]))
                continue
            if isinstance(msg, DisarmAll):
                # The show program's Abort (CONTRACT.md, disarm_all).
                # Applied here, before this tick composes, so every group
                # is off the wire on the tick it arrived in.
                self.composer.disarm_all(msg, sender=tuple(addr[:2]))
                continue
            self.composer.ingest_frame(msg, sender=tuple(addr[:2]))

    def _poll_arm(self):
        try:
            a = self.arm_input.poll()
        except Exception as e:                          # noqa: BLE001
            self.input_errors += 1
            self._event("arm-input", f"the arm input raised "
                                     f"{type(e).__name__}: {e}")
            return
        # Round 3 of the safety review, item 6: this runs whether or not
        # poll() itself had a fresh assertion to return, so the status
        # frame's foreign-sender count never goes stale just because the
        # locked sender was briefly quiet this tick.
        try:
            self.composer.note_foreign_arm_senders(
                getattr(self.arm_input, "foreign_count", 0))
        except Exception:                               # noqa: BLE001
            pass
        # Round 4, item B: a flood blocks consent too (composer.assert_arm).
        # Also before assert_arm, so the very tick that saw it is covered.
        try:
            self.composer.note_arm_link_flooded(
                bool(getattr(self.arm_input, "flooded", False)))
        except Exception:                               # noqa: BLE001
            self.composer.note_arm_link_flooded(True)
        if a is None:
            return
        try:
            self.composer.assert_arm(a.wanted, a.seq,
                                     names=getattr(a, "names", None),
                                     forced=getattr(a, "forced", None),
                                     sender=getattr(a, "sender", None))
        except Exception as e:                          # noqa: BLE001
            # assert_arm's own contract is "never raises" (composer.py); if
            # it ever does anyway, that is a bug in the composer, and the
            # old code here dropped the assertion with nothing but a
            # counter bumped (safety review of PR #31, item 6: named
            # alongside composer.py's own silent drop of a name mismatch,
            # because both left the same kind of rejection invisible).
            self.input_errors += 1
            self._event("arm-input", f"assert_arm raised "
                                     f"{type(e).__name__}: {e}; this is a "
                                     f"bug in the composer, which must "
                                     f"never raise here. The assertion "
                                     f"was dropped.")

    def run_once(self):
        """One tick.  Returns the composer's Output."""
        self._drain()
        self._poll_arm()
        self._poll_lockout()
        out = self.composer.tick()
        self.last_output = out
        self._send_universe(out.universe)
        self._send_status(out.status)
        return out

    def _poll_lockout(self):
        """The lockout file (Jeff, 2026-10-17), before this tick composes:
        a lock written by ltcplay's engine is enforced on the next packet
        after it is read.  Never raises; a reader that fails locks every
        slot (composer.note_locked_slots), the safe side."""
        try:
            slots, state = self.lockout.poll(self._clock())
        except Exception as e:                          # noqa: BLE001
            self._event("lockout", f"the lockout reader raised "
                                   f"{type(e).__name__}: {e}; every fire "
                                   f"slot is held at zero until it reads")
            slots, state = None, "unreadable"
        self.composer.note_locked_slots(slots, state)

    def _send_universe(self, values, terminated=False):
        pkt = build_packet(self.cfg.universe, values, self.sacn_seq,
                           terminated=terminated)
        self.sacn_seq = (self.sacn_seq + 1) & 0xFF
        try:
            self._tx.sendto(pkt, (self.cfg.destination_ip,
                                  self.cfg.destination_port))
            self.sent_packets += 1
        except OSError as e:
            self.send_errors += 1
            # The wire is not being written.  That is a fault the status
            # frame must carry, or ltcplay would show an armed group as
            # fine while nothing reaches the node.
            self.composer.note_fault(f"sACN send failed ({self.send_errors} "
                                     f"so far): {e}")

    def _send_status(self, status):
        try:
            status = dict(status)
            status["sacn"] = {"sent": self.sent_packets,
                              "errors": self.send_errors,
                              "status_errors": self.status_errors}
            pkt = encode_status(status, self.cfg.link_key)
            self._status_tx.sendto(pkt, (self.cfg.link_status_ip,
                                         self.cfg.link_status_port))
        except (OSError, TypeError, ValueError) as e:
            self.status_errors += 1
            self.composer.note_fault(f"status frame not sent "
                                     f"({self.status_errors} so far): {e}")
            return
        mirror = getattr(self.cfg, "link_status_mirror_port", None)
        if mirror is not None:
            # The same bytes again, for the engine's remote page. Display
            # only: a failure here is counted, never a fault, because the
            # wire and the deck's own status are untouched by it (the page
            # shows its lamps as stale on its own when these stop).
            try:
                self._status_tx.sendto(pkt, (self.cfg.link_status_ip, mirror))
            except OSError:
                self.mirror_errors += 1

    def run_forever(self, stop):
        """Tick at tick_hz until `stop` (a threading.Event) is set."""
        period = self.cfg.tick_period_s
        next_at = self._clock()
        while not stop.is_set():
            self.run_once()
            next_at += period
            t = self._clock()
            if next_at < t - period:
                # Fell behind by more than a whole tick: do not burst to
                # catch up, the composer has already counted the overrun.
                next_at = t
            delay = next_at - t
            if delay > 0:
                self._sleep(delay)

    def _event(self, kind, msg):
        if self._log is None:
            return
        try:
            self._log.event(kind, msg)
        except Exception:                               # noqa: BLE001
            pass
