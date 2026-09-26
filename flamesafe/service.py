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

import socket
import sys
import time

from . import rules
from .composer import Composer, now
from .link import LinkError, decode_flame, encode_status
from .sacn import build_packet

# Datagrams drained per tick.  A flood beyond this waits for the next tick
# rather than stalling this one; staleness and sequence checks do the rest.
DRAIN_PER_TICK = 200
SHUTDOWN_ZERO_FRAMES = 3
SHUTDOWN_TERMINATE_FRAMES = 3


SIO_UDP_CONNRESET = 0x9800000C      # _WSAIOW(IOC_VENDOR, 12), winsock2


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
        self._rx = None
        self._tx = None
        self._status_tx = None
        self.sacn_seq = 0
        self.sent_packets = 0
        self.send_errors = 0
        self.status_errors = 0
        self.input_errors = 0
        self.last_output = None

    # -------------------------------------------------------------- sockets

    def open(self):
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
            try:
                frame = decode_flame(data, self.cfg.universe,
                                     self.cfg.link_key)
            except LinkError as e:
                self.composer.reject_frame(str(e))
                continue
            self.composer.ingest_frame(frame, sender=tuple(addr[:2]))

    def _poll_arm(self):
        try:
            a = self.arm_input.poll()
        except Exception as e:                          # noqa: BLE001
            self.input_errors += 1
            self._event("arm-input", f"the arm input raised "
                                     f"{type(e).__name__}: {e}")
            return
        if a is None:
            return
        try:
            self.composer.assert_arm(a.wanted, a.seq,
                                     names=getattr(a, "names", None))
        except Exception:                               # noqa: BLE001
            self.input_errors += 1

    def run_once(self):
        """One tick.  Returns the composer's Output."""
        self._drain()
        self._poll_arm()
        out = self.composer.tick()
        self.last_output = out
        self._send_universe(out.universe)
        self._send_status(out.status)
        return out

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
            self._status_tx.sendto(encode_status(status, self.cfg.link_key),
                                   (self.cfg.link_status_ip,
                                    self.cfg.link_status_port))
        except (OSError, TypeError, ValueError) as e:
            self.status_errors += 1
            self.composer.note_fault(f"status frame not sent "
                                     f"({self.status_errors} so far): {e}")

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
