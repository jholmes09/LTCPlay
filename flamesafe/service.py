"""flamesafe service: sockets and pacing around the composer.

One loop, paced on perf_counter.  Each tick: drain ltcplay's datagrams,
poll the arm input, compose, send the universe by sACN at priority 200,
send the status frame.  The composer decides every value; this file only
moves bytes.
"""

from __future__ import annotations

import socket
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


class Service:

    def __init__(self, config, arm_input, clock=now, log=None):
        self.cfg = config
        self.arm_input = arm_input
        self._clock = clock
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
        self._event("start", f"flame universe {self.cfg.universe} to "
                             f"{self.cfg.destination_ip}:"
                             f"{self.cfg.destination_port} at sACN priority "
                             f"{rules.SACN_PRIORITY}, {self.cfg.tick_hz} Hz; "
                             f"frames in on {self.cfg.link_listen_ip}:"
                             f"{self.cfg.link_listen_port}, status out to "
                             f"{self.cfg.link_status_ip}:"
                             f"{self.cfg.link_status_port}")

    def close(self):
        """Zeros on the wire, then the stream terminated, then the sockets."""
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
                data, _addr = rx.recvfrom(65535)
            except BlockingIOError:
                return
            except ConnectionResetError:
                # Windows reports a peer's ICMP port-unreachable here.
                continue
            except OSError:
                return
            try:
                frame = decode_flame(data, self.cfg.universe)
            except LinkError as e:
                self.composer.reject_frame(str(e))
                continue
            self.composer.ingest_frame(frame)

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
            self.composer.assert_arm(a.wanted, a.seq)
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
            if self.send_errors in (1, 10, 100) or self.send_errors % 1000 == 0:
                self._event("send", f"sACN send failed ({self.send_errors} "
                                    f"so far): {e}")

    def _send_status(self, status):
        try:
            status = dict(status)
            status["sacn"] = {"sent": self.sent_packets,
                              "errors": self.send_errors}
            self._status_tx.sendto(encode_status(status),
                                   (self.cfg.link_status_ip,
                                    self.cfg.link_status_port))
        except (OSError, TypeError, ValueError):
            self.status_errors += 1

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
                time.sleep(delay)

    def _event(self, kind, msg):
        if self._log is None:
            return
        try:
            self._log.event(kind, msg)
        except Exception:                               # noqa: BLE001
            pass
