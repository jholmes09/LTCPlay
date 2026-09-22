"""ArtNet and E1.31 packet construction and sending.

Byte layouts taken from the xLights source of record:
  ArtNet  src-core/outputs/ArtNetOutput.cpp:281-292, header length 18 and port
          0x1936 from ArtNetOutput.h:23-27
  E1.31   src-core/outputs/E131Output.cpp:297-345, header length 126 and port
          5568 from E131Output.h:20-23
"""
import socket
import struct
import time
import uuid

ARTNET_PORT = 0x1936          # 6454
ARTNET_HEADER_LEN = 18
E131_PORT = 5568
E131_HEADER_LEN = 126

# A stable CID for this sender.  E1.31 receivers use it to tell sources apart;
# it must not change while the player is running.
_CID = uuid.uuid5(uuid.NAMESPACE_DNS, "ltcplay.jeffholmespresents").bytes
_SOURCE_NAME = b"ltcplay"


def _artnet_header(universe, count):
    b = bytearray(ARTNET_HEADER_LEN)
    b[0:8] = b"Art-Net\x00"
    b[8] = 0x00
    b[9] = 0x50               # OpDmx, little endian opcode 0x5000
    b[10] = 0x00
    b[11] = 0x0E              # protocol version 14, high byte then low
    b[12] = 0                 # sequence, filled per send
    b[13] = 0                 # physical
    b[14] = universe & 0xFF
    b[15] = (universe >> 8) & 0xFF
    b[16] = (count >> 8) & 0xFF
    b[17] = count & 0xFF
    return b


def _e131_header(universe, count, priority=100):
    b = bytearray(E131_HEADER_LEN)
    b[1] = 0x10
    b[4:16] = b"ASC-E1.17\x00\x00\x00"
    total = E131_HEADER_LEN + count
    b[16] = 0x70 | (((total - 16) >> 8) & 0x0F)
    b[17] = (total - 16) & 0xFF
    b[21] = 0x04              # VECTOR_ROOT_E131_DATA
    b[22:38] = _CID
    b[38] = 0x70 | (((total - 38) >> 8) & 0x0F)
    b[39] = (total - 38) & 0xFF
    b[43] = 0x02              # VECTOR_E131_DATA_PACKET
    name = _SOURCE_NAME[:63]
    b[44:44 + len(name)] = name
    b[108] = priority
    b[111] = 0                # sequence, filled per send
    b[113] = (universe >> 8) & 0xFF
    b[114] = universe & 0xFF
    b[115] = 0x70 | (((total - 115) >> 8) & 0x0F)
    b[116] = (total - 115) & 0xFF
    b[117] = 0x02             # DMP set property
    b[118] = 0xA1             # address type and data type
    b[122] = 0x01             # address increment
    n = count + 1             # property value count includes the start code
    b[123] = (n >> 8) & 0xFF
    b[124] = n & 0xFF
    b[125] = 0x00             # DMX start code
    return b


class Sender:
    """Holds one prebuilt packet per universe and rewrites only the payload.

    The socket heals itself.  macOS will hand back EHOSTDOWN, ENETDOWN or
    ENOBUFS on a UDP send when the interface flaps, a switch reboots or the
    ARP entry for a controller goes stale, and once that happens the socket
    stays poisoned: every later send fails on a descriptor that looks fine.
    That is the same defect that stalls xLights output until it is restarted.
    Here, three consecutive failures close the socket and the next send opens
    a fresh one, no faster than once a second.
    """

    FAILURES_BEFORE_REOPEN = 3
    REOPEN_BACKOFF_S = 1.0
    # Per-DESTINATION quiet time. A controller that is switched off or not
    # patched yet makes the kernel broadcast an ARP request for it, and at 40
    # frames a second against 22 destinations that is a steady stream of
    # broadcast onto a network that is also carrying Dante audio. macOS rate
    # limits its own ARP, so this is not a storm, but it is constant noise on
    # the one wire the show cannot afford to have noisy, and it never stops
    # because nothing ever gave up on the dead address.
    #
    # This backs off only from destinations the OS has EXPLICITLY refused --
    # host down, host or network unreachable, no buffer space. It never backs
    # off from a controller that merely fails to answer a ping: plenty of
    # ArtNet nodes never answer ICMP, and going quiet on one of those would
    # black out a working prop. Asked for by Jeff, 2026-09-14.
    DEST_FAILS_BEFORE_QUIET = 20
    DEST_QUIET_S = 2.0
    DEST_QUIET_MAX_S = 30.0

    def __init__(self, netmap, bind_ip=None, log=None):
        self.broadcast_dests = sorted({
            u.ip for u in netmap.universes if self.looks_broadcast(u.ip)})
        self._bind_ip = bind_ip
        self.log = log
        self._sock = None
        self._consecutive_failures = 0
        self._last_reopen_at = None
        self.reopens = 0
        self.open_errors = 0
        self.last_ok_at = None
        self.last_error = ""
        self.last_error_at = None
        if not self._open():
            # A bind that fails at startup is a wrong --bind address or an
            # interface that is not up, and healing round it would leave a
            # program that looks alive and sends nothing. Fail here instead.
            raise OSError(self.last_error or "could not open the output socket")
        self._packets = []
        for u in netmap.universes:
            if u.protocol == "artnet":
                head = _artnet_header(u.universe, u.count)
                seq_index = 12
                payload_at = ARTNET_HEADER_LEN
            else:
                head = _e131_header(u.universe, u.count)
                seq_index = 111
                payload_at = E131_HEADER_LEN
            buf = bytearray(head) + bytearray(u.count)
            self._packets.append({
                "buf": buf, "seq_index": seq_index, "payload_at": payload_at,
                "addr": (u.ip, ARTNET_PORT if u.protocol == "artnet" else E131_PORT),
                "start": u.start - 1, "count": u.count, "seq": 0,
                "fails": 0, "quiet_until": 0.0, "quiet_for": 0.0,
            })
        self.universe_count = len(self._packets)
        # Addresses this sender deliberately does not send to. Used by the
        # Advatek scene-trigger mode: those boxes only play a recorded scene
        # while no live pixel data is arriving, so arming that mode has to
        # actually STOP the stream to them, not merely ignore them. Every
        # other controller keeps being streamed off the same FSEQ.
        self._muted = frozenset()
        self.muted_destinations = 0
        self.quiet_destinations = 0
        self.packets_sent = 0
        self.send_errors = 0
        self.last_ok_at = time.monotonic()

    def set_muted(self, ips):
        """Stop sending to these addresses; send to everything else.

        Takes effect on the next frame. Returns the addresses that are now
        muted AND are actually in this controller map, so a caller can tell
        the difference between muting six boxes and muting six typos."""
        self._muted = frozenset(x.strip() for x in (ips or ()) if x and x.strip())
        known = {p["addr"][0] for p in self._packets}
        return sorted(self._muted & known)

    @property
    def muted(self):
        return sorted(self._muted)

    # -- socket lifecycle -------------------------------------------------
    @staticmethod
    def looks_broadcast(ip):
        """A destination every device on the wire has to read and discard.

        One broadcast universe at 40 frames a second is 40 frames a second of
        work for every phone, laptop and Dante box on the segment. It is the
        usual reason a show player is accused of killing a network."""
        ip = (ip or "").strip()
        return ip == "255.255.255.255" or ip.endswith(".255")

    def _open(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # Asked for ONLY when a destination needs it. Left on by default,
            # a broadcast address that slipped into the controller map is sent
            # without a word of complaint.
            if self.broadcast_dests:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            if self._bind_ip:
                s.bind((self._bind_ip, 0))
        except OSError as e:
            self.open_errors += 1
            self.last_error = f"open: {e}"
            self.last_error_at = time.monotonic()
            self._event("socket", self.last_error)
            self._sock = None
            return False
        self._sock = s
        self._consecutive_failures = 0
        return True

    def _drop_socket(self):
        s, self._sock = self._sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

    def _ensure_socket(self, now):
        """Reopen at most once a second, so a dead network does not spin."""
        if self._sock is not None:
            return True
        if self._last_reopen_at is not None and \
                now - self._last_reopen_at < self.REOPEN_BACKOFF_S:
            return False
        self._last_reopen_at = now
        if self._open():
            self.reopens += 1
            self._event("socket", f"reopened output socket "
                                  f"(recovery #{self.reopens})")
            return True
        return False

    def _event(self, kind, msg):
        if self.log:
            try:
                self.log.event(kind, msg)
            except Exception:
                pass

    @property
    def seconds_since_error(self):
        """How long ago the last failure was, or None if there never was one.

        A counter on its own cannot tell a fault happening now from one that
        happened once an hour ago, and a red line that never clears teaches
        the operator to ignore the panel. Jeff, 2026-09-14."""
        if self.last_error_at is None:
            return None
        return time.monotonic() - self.last_error_at

    @property
    def seconds_since_ok(self):
        if self.last_ok_at is None:
            return None
        return time.monotonic() - self.last_ok_at

    def send_frame(self, channels):
        """channels: a bytes-like of absolute channel values, index 0 = channel 1.

        Universes that run past the end of `channels` are sent zero-padded
        rather than skipped, so a short frame goes dark instead of freezing."""
        now = time.monotonic()
        if not self._ensure_socket(now):
            self.send_errors += sum(1 for p in self._packets
                                    if p["addr"][0] not in self._muted)
            return
        sock = self._sock
        any_ok = False
        quiet = 0
        muted = 0
        n = len(channels)
        muted_ips = self._muted
        for p in self._packets:
            if p["addr"][0] in muted_ips:
                muted += 1
                continue
            if p["quiet_until"] > now:
                quiet += 1
                continue
            s = p["start"]
            c = p["count"]
            buf = p["buf"]
            at = p["payload_at"]
            if s >= n:
                for i in range(at, at + c):
                    buf[i] = 0
            else:
                avail = min(c, n - s)
                buf[at:at + avail] = channels[s:s + avail]
                if avail < c:
                    for i in range(at + avail, at + c):
                        buf[i] = 0
            p["seq"] = (p["seq"] + 1) & 0xFF
            # E1.31 reserves sequence 0 for "no sequence tracking"
            if p["seq_index"] == 111 and p["seq"] == 0:
                p["seq"] = 1
            buf[p["seq_index"]] = p["seq"]
            try:
                sock.sendto(buf, p["addr"])
                self.packets_sent += 1
                any_ok = True
                if p["fails"]:
                    p["fails"] = 0
                    p["quiet_for"] = 0.0
                    self._event("socket", f"{p['addr'][0]} is taking packets "
                                          f"again")
            except OSError as e:
                self.send_errors += 1
                self.last_error = f"send to {p['addr'][0]}: {e}"
                self.last_error_at = now
                p["fails"] += 1
                if p["fails"] >= self.DEST_FAILS_BEFORE_QUIET:
                    p["fails"] = 0
                    p["quiet_for"] = min(
                        self.DEST_QUIET_MAX_S,
                        (p["quiet_for"] or self.DEST_QUIET_S) * 2
                        if p["quiet_for"] else self.DEST_QUIET_S)
                    p["quiet_until"] = now + p["quiet_for"]
                    self._event("socket",
                                f"{p['addr'][0]} refused "
                                f"{self.DEST_FAILS_BEFORE_QUIET} packets in a "
                                f"row ({e}); pausing that address for "
                                f"{p['quiet_for']:.0f}s so it stops making the "
                                f"Mac ARP for it every frame")

        self.quiet_destinations = quiet
        self.muted_destinations = muted
        if any_ok:
            self._consecutive_failures = 0
            self.last_ok_at = now
            return
        if quiet + muted == len(self._packets):
            # Everything is resting. Not a failure to escalate: the backoff is
            # doing what it was asked to.
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.FAILURES_BEFORE_REOPEN:
            self._event("socket", f"{self._consecutive_failures} sends in a row "
                                  f"failed ({self.last_error}); rebuilding the "
                                  f"socket")
            self._drop_socket()
            self._last_reopen_at = now

    def blackout(self):
        self.send_frame(b"")

    def close(self):
        self._drop_socket()
