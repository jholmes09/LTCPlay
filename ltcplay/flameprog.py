"""Live flame programming: xLights drives the flames through flamesafe while
Jeff programs (LIVE_FLAME_PROGRAMMING.md).

Jeff, 2026-10-22: "while programming, arm/disarm works in LTC Player but I
send the flame commands direct from xLights." Not to be confused with the
rack screen's Programming screen or a programming session's scrub
transport: those play a show file; this listens to xLights.

What it is
==========

A listener inside the engine (`ltc serve`, Fire & Ice only) that, ONLY
while the mode is on, hears xLights' E1.31 (sACN) data on 127.0.0.1 and
hands the flame controller's channels to the engine's own flame link
(flamelink.FlameLink.program_set), exactly as the Morning page's purge
hands it a frame (purge_set). Nothing here sends anything to the flame
node or to flamesafe: the values leave on the flame link's one keyed,
sender-locked socket, and flamesafe still applies every rule it has
(arming per group, hold to arm, lockouts, the Abort latch, the stale
cutoffs). flamesafe is not changed and never sees a second sender.

The guards, each a named refusal or a named end
===============================================

  - Bound to 127.0.0.1 only. Any other listen address is refused before a
    socket is made (check_listen_ip). A packet from anywhere but loopback
    ends the mode.
  - Programming from a Mac (optional, allowed_source_ip): exactly one
    configured private IPv4 address, the Mac's, may also send. Then the
    listener binds listen_ip (this PC's own show network address) AND
    127.0.0.1, never 0.0.0.0, and never joins a multicast group. A packet
    from any address but loopback and that one ends the mode. The Mac's
    address may not be loopback, multicast, public, this PC's own, the
    flame node's (flamesafe's destination) or any device in the configs or
    in xlights_networks.xml (check_show_ip, LiveFlame._source_problem).
    The flame controller must then be E1.31 unicast to listen_ip.
  - Entry is refused unless the engine says nothing blocks it (no show
    live or held, Run not pressed, no Abort latched, flamesafe reporting,
    no purge), the flame controller in xlights_networks.xml is E1.31
    unicast to 127.0.0.1 only and not Active (FlameMap.from_networks), no
    other Active controller sends E1.31 to this PC, flamesafe's own
    destination is not this listener's address and port (its output would
    loop back in), the port is free, and at most one flame group is armed.
  - One sender. The first E1.31 source (its CID and priority) is the only
    one. Another CID or priority, flamesafe's own packets (its CID or
    source name: its output looped back) or a non-loopback source: zeros,
    every group disarmed and the mode ENDS, with the reason.
  - Zero on silence. A universe with no fresh packet for HOLD_S (0.25 s)
    is zeros, and the flame link drops the whole frame on its own clock
    0.25 s after the last program_set. A channel that is not zero when its
    stream (re)starts stays zero until xLights has sent it at zero once.
  - Sanity. Strict E1.31 data packet parsing (parse_e131); preview data
    and other start codes are ignored; the sACN sequence rule drops late
    and repeated packets; a universe sending more than RATE_MAX packets a
    second ends the mode (a flood); only the flame controller's own
    universes and channel counts are read (clamped to 512 slots).
  - One group at a time (setting, default on): two flame groups armed
    while the mode is on ends it, with every group disarmed.
  - Auto off: no xLights data for idle_off_s (default 600) or a session
    longer than max_session_s (default 4 hours) ends it.
  - Leaving the mode for ANY reason disarms every flame group (the flame
    link's disarm_all, the show's own Abort disarm) and zeroes the values.
  - Abort from any surface ends it at the link at once (disarm_all clears
    program_on); Hold, Start now, Run and a show going live end it on the
    engine's next watch pass (WATCH_S), and the link never reads its frame
    while the show is live in any case.
"""
import collections
import json
import os
import select
import socket
import threading
import time
import uuid

E131_PORT = 5568
LISTEN_IP = "127.0.0.1"
UNIVERSE_SIZE = 512
HOLD_S = 0.25             # a universe with no packet this long is zeros
WATCH_S = 0.05            # how often the listener thread checks the rules
RATE_MAX = 200            # packets a second per universe; more is a flood
RATE_WINDOW_S = 1.0
# Every datagram, junk included, counts against this before it is parsed
# (review of PR #96, P3-5): a local flood ends the mode instead of keeping
# the listener busy.
TOTAL_MAX = 1000
WATCHDOG_S = 0.2          # how often the watchdog looks at the listener
LISTENER_STALL_S = 1.0    # a listener quiet this long is dead to it
SEQ_WINDOW = 20           # the E1.31 sequence rule (section 6.7.2)
IDLE_OFF_S_DEFAULT = 600
MAX_SESSION_S_DEFAULT = 4 * 3600
SETTINGS_KEY = "live_flame"
SETTINGS_KEYS = frozenset(("listen_ip", "port", "idle_off_s",
                           "max_session_s", "one_group_at_a_time",
                           "networks_xml", "allowed_source_ip"))
# The only ranges a Mac on the show network may be in (RFC 1918).
PRIVATE_NETS = (("10.0.0.0", 8), ("172.16.0.0", 12), ("192.168.0.0", 16))
NAME = "Live flame programming"

# E1.31 layout (ANSI E1.31-2018, section 4 and 6).
ACN_ID = b"ASC-E1.17\x00\x00\x00"
HEADER_LEN = 126
PACKET_MAX = HEADER_LEN + UNIVERSE_SIZE
VECTOR_ROOT_DATA = 0x00000004
VECTOR_ROOT_EXTENDED = 0x00000008
VECTOR_FRAMING_DATA = 0x00000002
OPT_PREVIEW = 0x80
OPT_TERMINATED = 0x40
# flamesafe's own sACN identity (flamesafe/sacn.py: CID and source name),
# written here from the contract, never imported (the wall).
FLAMESAFE_CID = uuid.uuid5(uuid.NAMESPACE_DNS,
                           "flamesafe.jeffholmespresents").bytes
FLAMESAFE_NAME = "flamesafe"


class PacketError(ValueError):
    """Not an E1.31 data packet this listener takes, in a sentence."""


class NotData(PacketError):
    """A well formed E1.31 packet that carries no levels (universe
    discovery, synchronization): ignored without counting as malformed."""


class FlameMapError(ValueError):
    """The flame controller cannot be used for live flame programming, in
    a sentence."""


class SettingsError(ValueError):
    """The live_flame block in ltcplay_remote.json is wrong, in a
    sentence."""


# -------------------------------------------------------------- settings --

def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def check_show_ip(ip, what):
    """`ip` as a show network address (the Mac's, or this PC's listen_ip):
    one IPv4 address written plainly, in 10/8, 172.16/12 or 192.168/16,
    not ending in .0 or .255. Anything else (a name, loopback, multicast,
    broadcast, link local, public, 0.0.0.0, IPv6) raises SettingsError."""
    import ipaddress
    bad = SettingsError(
        f"{what} has to be one private IPv4 address on the show network, "
        f"like 10.0.0.20 (10.x, 172.16.x to 172.31.x or 192.168.x, not "
        f"ending in .0 or .255), never {ip!r}: no name, no loopback, no "
        f"multicast, no public address.")
    if not isinstance(ip, str):
        raise bad
    try:
        a = ipaddress.IPv4Address(ip)
    except ValueError:
        raise bad
    if str(a) != ip or ip.split(".")[3] in ("0", "255"):
        raise bad
    if not any(a in ipaddress.IPv4Network(f"{n}/{m}")
               for n, m in PRIVATE_NETS):
        raise bad
    return ip


def check_listen_ip(ip, allowed_source_ip=None):
    """The address the listener may bind: 127.0.0.1, and with an allowed
    Mac (allowed_source_ip) also this PC's own show network address,
    never the Mac's. Anything else (0.0.0.0, a name) raises SettingsError:
    a listener open to the network would let any device drive the flame
    values."""
    if allowed_source_ip is not None:
        check_show_ip(ip, "'listen_ip' (this PC's own show network address)")
        if ip == allowed_source_ip:
            raise SettingsError(
                f"'listen_ip' is this PC's own address and "
                f"'allowed_source_ip' is the Mac's; they cannot both be "
                f"{ip}.")
        return ip
    if ip != LISTEN_IP:
        raise SettingsError(
            f"{NAME} listens on {LISTEN_IP} only, never {ip!r}: xLights runs "
            f"on this show PC and nothing on the network may send flame "
            f"values.")
    return ip


class Settings:
    """The live_flame block: {"port": 5568, "idle_off_s": 600,
    "max_session_s": 14400, "one_group_at_a_time": true, "networks_xml":
    null, "allowed_source_ip": null}. Every key may be left out. With
    "allowed_source_ip" (the Mac's address), "listen_ip" (this PC's own
    show network address) is required."""

    def __init__(self, port=E131_PORT, idle_off_s=IDLE_OFF_S_DEFAULT,
                 max_session_s=MAX_SESSION_S_DEFAULT, one_group=True,
                 networks_xml=None, listen_ip=LISTEN_IP,
                 allowed_source_ip=None):
        if allowed_source_ip is not None:
            check_show_ip(allowed_source_ip, "'allowed_source_ip'")
        self.allowed_source_ip = allowed_source_ip
        self.listen_ip = check_listen_ip(listen_ip, allowed_source_ip)
        self.port = port
        self.idle_off_s = idle_off_s
        self.max_session_s = max_session_s
        self.one_group = one_group
        self.networks_xml = networks_xml

    @classmethod
    def parse(cls, doc, where=SETTINGS_KEY):
        if doc is None:
            return cls()
        if not isinstance(doc, dict):
            raise SettingsError(f"{where} has to be one JSON object.")
        unknown = sorted(str(k) for k in doc if k not in SETTINGS_KEYS)
        if unknown:
            raise SettingsError(
                f"{where}: {', '.join(repr(k) for k in unknown)} is not a "
                f"setting {NAME} has. It takes: "
                f"{', '.join(sorted(SETTINGS_KEYS))}.")
        allowed = doc.get("allowed_source_ip")
        if allowed is not None and "listen_ip" not in doc:
            raise SettingsError(
                f"{where}: 'allowed_source_ip' needs 'listen_ip', this PC's "
                f"own show network address, for the Mac to send to.")
        ip = doc.get("listen_ip", LISTEN_IP)
        try:
            if allowed is not None:
                check_show_ip(allowed, "'allowed_source_ip'")
            check_listen_ip(ip, allowed)
        except SettingsError as e:
            raise SettingsError(f"{where}: {e}")
        port = doc.get("port", E131_PORT)
        if not _is_int(port) or not 1024 <= port <= 65535:
            raise SettingsError(f"{where}: 'port' has to be a whole number "
                                f"1024 to 65535 (E1.31 is 5568).")
        idle = doc.get("idle_off_s", IDLE_OFF_S_DEFAULT)
        if not _is_int(idle) or not 10 <= idle <= 3600:
            raise SettingsError(f"{where}: 'idle_off_s' has to be a whole "
                                f"number of seconds, 10 to 3600.")
        most = doc.get("max_session_s", MAX_SESSION_S_DEFAULT)
        if not _is_int(most) or not 60 <= most <= 12 * 3600:
            raise SettingsError(f"{where}: 'max_session_s' has to be a whole "
                                f"number of seconds, 60 to 43200.")
        one = doc.get("one_group_at_a_time", True)
        if one is not True and one is not False:
            raise SettingsError(f"{where}: 'one_group_at_a_time' has to be "
                                f"true or false.")
        xml = doc.get("networks_xml")
        if xml is not None and (not isinstance(xml, str) or not xml):
            raise SettingsError(f"{where}: 'networks_xml' has to be the full "
                                f"path of xlights_networks.xml, or null.")
        return cls(port, idle, most, one, xml, ip, allowed)

    def binds(self):
        """Every (address, port) the listener binds: 127.0.0.1, and with
        an allowed Mac this PC's listen_ip too. Never 0.0.0.0."""
        if self.allowed_source_ip is None:
            return [(LISTEN_IP, self.port)]
        return [(LISTEN_IP, self.port), (self.listen_ip, self.port)]

    def source(self):
        """Who may send, for the page: "this PC" or "Mac 10.x.x.x"."""
        if self.allowed_source_ip is None:
            return "this PC"
        return f"Mac {self.allowed_source_ip}"

    def view(self):
        return {"listen": " and ".join(f"{a}:{p}" for a, p in self.binds()),
                "allowed_source_ip": self.allowed_source_ip,
                "idle_off_s": self.idle_off_s,
                "max_session_s": self.max_session_s,
                "one_group_at_a_time": self.one_group,
                "networks_xml": self.networks_xml}


def load_settings(path):
    """Settings from ltcplay_remote.json's live_flame block (a missing
    file or block: the defaults). Raises SettingsError."""
    if not path or not os.path.exists(path):
        return Settings()
    try:
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as e:
        raise SettingsError(f"{path} could not be read: {e}")
    if not isinstance(doc, dict):
        raise SettingsError(f"{path} is not a settings object.")
    return Settings.parse(doc.get(SETTINGS_KEY), f"{path} {SETTINGS_KEY}")


# ---------------------------------------------------------------- E1.31 --

class Packet:
    __slots__ = ("cid", "source", "priority", "seq", "options", "universe",
                 "values")

    def __init__(self, cid, source, priority, seq, options, universe,
                 values):
        self.cid = cid
        self.source = source
        self.priority = priority
        self.seq = seq
        self.options = options
        self.universe = universe
        self.values = values

    @property
    def preview(self):
        return bool(self.options & OPT_PREVIEW)

    @property
    def terminated(self):
        return bool(self.options & OPT_TERMINATED)


def _len12(b, at):
    return ((b[at] & 0x0F) << 8) | b[at + 1]


def parse_e131(data):
    """One E1.31 data packet as a Packet, or PacketError naming what is
    wrong (NotData for a well formed packet that carries no levels). Every
    length and vector is checked; nothing is guessed. Never raises
    anything else."""
    try:
        b = bytes(data)
    except Exception:
        raise PacketError("not bytes")
    n = len(b)
    if n < 22:
        raise PacketError(f"too short ({n} bytes)")
    if b[0:2] != b"\x00\x10" or b[2:4] != b"\x00\x00":
        raise PacketError("not an ACN packet (preamble)")
    if b[4:16] != ACN_ID:
        raise PacketError("not an ACN packet (identifier)")
    vector = int.from_bytes(b[18:22], "big")
    if vector == VECTOR_ROOT_EXTENDED:
        raise NotData("an E1.31 discovery or sync packet")
    if vector != VECTOR_ROOT_DATA:
        raise PacketError(f"root vector {vector} is not E1.31 data")
    if n < HEADER_LEN:
        raise PacketError(f"too short for E1.31 data ({n} bytes)")
    if n > PACKET_MAX:
        raise PacketError(f"too long ({n} bytes; at most {PACKET_MAX})")
    if b[16] & 0xF0 != 0x70 or _len12(b, 16) != n - 16:
        raise PacketError("root layer length does not match the packet")
    if b[38] & 0xF0 != 0x70 or _len12(b, 38) != n - 38:
        raise PacketError("framing layer length does not match the packet")
    if int.from_bytes(b[40:44], "big") != VECTOR_FRAMING_DATA:
        raise NotData("an E1.31 framing layer that is not data")
    raw_name = b[44:108]
    name = raw_name.split(b"\x00", 1)[0].decode("utf-8", "replace")
    priority = b[108]
    if priority > 200:
        raise PacketError(f"priority {priority} is over 200")
    seq = b[111]
    options = b[112]
    universe = (b[113] << 8) | b[114]
    if not 1 <= universe <= 63999:
        raise PacketError(f"universe {universe} is outside 1 to 63999")
    if b[115] & 0xF0 != 0x70 or _len12(b, 115) != n - 115:
        raise PacketError("DMP layer length does not match the packet")
    if b[117] != 0x02 or b[118] != 0xA1:
        raise PacketError("DMP layer is not set property, 0xA1")
    if b[119:121] != b"\x00\x00" or b[121:123] != b"\x00\x01":
        raise PacketError("DMP first address and increment are not 0 and 1")
    count = (b[123] << 8) | b[124]
    if count < 1 or count != n - 125:
        raise PacketError("DMP property count does not match the packet")
    if b[125] != 0x00:
        raise NotData(f"start code {b[125]} is not levels")
    return Packet(b[22:38], name, priority, seq, options, universe,
                  b[126:])


def build_e131(universe, values, seq=0, cid=None, source="xLights",
               priority=100, options=0):
    """An E1.31 data packet, for the selftest and the bench: the listener
    never sends anything."""
    values = bytes(values)
    count = len(values)
    total = HEADER_LEN + count
    b = bytearray(total)
    b[1] = 0x10
    b[4:16] = ACN_ID
    b[16] = 0x70 | (((total - 16) >> 8) & 0x0F)
    b[17] = (total - 16) & 0xFF
    b[18:22] = VECTOR_ROOT_DATA.to_bytes(4, "big")
    b[22:38] = (cid or uuid.uuid5(uuid.NAMESPACE_DNS, "xlights.test").bytes)
    b[38] = 0x70 | (((total - 38) >> 8) & 0x0F)
    b[39] = (total - 38) & 0xFF
    b[40:44] = VECTOR_FRAMING_DATA.to_bytes(4, "big")
    nm = source.encode("utf-8")[:63]
    b[44:44 + len(nm)] = nm
    b[108] = priority
    b[111] = seq & 0xFF
    b[112] = options
    b[113] = (universe >> 8) & 0xFF
    b[114] = universe & 0xFF
    b[115] = 0x70 | (((total - 115) >> 8) & 0x0F)
    b[116] = (total - 115) & 0xFF
    b[117] = 0x02
    b[118] = 0xA1
    b[122] = 0x01
    b[123] = ((count + 1) >> 8) & 0xFF
    b[124] = (count + 1) & 0xFF
    b[126:] = values
    return bytes(b)


# --------------------------------------------------------- the channel map --

class FlameMap:
    """Which xLights universe and channel is which flamesafe slot: the flame
    controller's network rows in xlights_networks.xml, in order, each from
    channel 1 of its universe; row k's channel n is slot (the earlier
    rows' counts) + n. The same slots FlameCues reads from a render
    (fire_ice.py), so live and rendered agree by construction."""

    def __init__(self, rows, path=None, stamp=None, dest=LISTEN_IP,
                 raw=None, ips=None, print_=None, bad_addrs=()):
        self.rows = tuple(rows)       # (universe, first slot, count)
        self.path = path
        self.stamp = stamp
        self.dest = dest              # where the flame controller sends
        # (universe, MaxChannels) as written, for the fingerprint.
        self.raw = tuple(raw if raw is not None else
                         ((u, c) for u, _f, c in rows))
        self.ips = dict(ips or {})    # every controller's address -> name
        # Addresses in the file that are not one plain IPv4 address (a
        # name, leading zeros, spaces): the Mac mode refuses on any.
        self.bad_addrs = tuple(bad_addrs)
        self._print = print_
        self.by_universe = {u: (first, count) for u, first, count in rows}

    @property
    def fingerprint(self):
        """A short hash of the whole xlights_networks.xml, canonicalized
        (every controller's name, state, addresses, protocols, universes
        and channel counts), as tools/xlights_map_fingerprint.py prints
        it."""
        if self._print is None and self.path:
            try:
                self._print = file_fingerprint(self.path)
            except Exception:
                self._print = "unreadable"
        return self._print or ""

    @property
    def slots(self):
        out = set()
        for _u, first, count in self.rows:
            out.update(range(first, first + count))
        return out

    def key(self):
        return self.rows

    def view(self):
        return [{"universe": u, "channels": f"1 to {c}",
                 "slots": f"{f} to {f + c - 1}"} for u, f, c in self.rows]

    @classmethod
    def from_networks(cls, path, name, dest=LISTEN_IP):
        """The map, or FlameMapError naming why the flame controller cannot
        be used: missing, ambiguous or Active (fire_ice.flame_channels'
        own refusals), any row not E1.31 unicast to exactly `dest`
        (127.0.0.1, or this PC's listen_ip when a Mac may send; multicast,
        another address, Art-Net), a universe twice, and any OTHER Active
        controller sending E1.31 to this PC (it would reach this listener
        too)."""
        from . import fire_ice, netmap
        if not path or not os.path.isfile(path):
            raise FlameMapError(f"xlights_networks.xml was not found "
                                f"({path or 'no path'}).")
        try:
            st = os.stat(path)
            stamp = (st.st_mtime_ns, st.st_size)
            fire_ice.flame_channels(path, name)
            ctrls = fire_ice._controllers(path)
        except fire_ice.FlameControllerError as e:
            raise FlameMapError(str(e))
        except Exception as e:
            raise FlameMapError(f"{path} could not be read "
                                f"({type(e).__name__}: {e}).")
        want = str(dest or "")
        if want != LISTEN_IP:
            try:
                check_show_ip(want, "this PC's listen_ip")
            except SettingsError as e:
                raise FlameMapError(str(e))
        rows = []
        raw = []
        ips = {}
        bad = []
        seen = set()
        slot = 1
        others = []
        for c, _first, _span in ctrls:
            a = c.attrib
            for net in [c] + [n for n in c if n.tag == "network"]:
                udp = net is c or netmap.UDP_PROTOCOLS.get(
                    (net.attrib.get("NetworkType") or "").lower())
                for k in ("IP", "ComPort"):
                    v = net.attrib.get(k) or ""
                    if not v or (k == "ComPort" and not udp):
                        continue
                    if v.upper() == "MULTICAST":
                        continue
                    if not _plain_ipv4(v):
                        bad.append(f"{v!r} ({a.get('Name', '?')})")
                        continue
                    ips.setdefault(v, a.get("Name", "?"))
            if a.get("Name", "") != name:
                others.append(c)
                continue
            for net in c:
                if net.tag != "network":
                    continue
                na = net.attrib
                try:
                    count = int(na.get("MaxChannels", "0") or 0)
                except ValueError:
                    count = 0
                if count <= 0:
                    continue
                proto = (na.get("NetworkType") or "").lower()
                dest = na.get("ComPort", "") or a.get("IP", "")
                if netmap.UDP_PROTOCOLS.get(proto) != "e131":
                    raise FlameMapError(
                        f"The flame controller {name!r} sends "
                        f"{na.get('NetworkType') or 'nothing'} in {path}. "
                        f"Set it to E1.31 in xLights (Art-Net on this PC "
                        f"belongs to MadMapper and BEYOND).")
                if dest != want:
                    what = ("multicast" if _multicast(dest)
                            else repr(dest) if dest else "no address")
                    raise FlameMapError(
                        f"The flame controller {name!r} sends to {what} in "
                        f"{path}. For {NAME} it must be E1.31 unicast to "
                        f"{want} and nothing else: any other address "
                        f"could reach the flame node around flamesafe.")
                try:
                    univ = int(na.get("BaudRate", ""))
                except ValueError:
                    raise FlameMapError(
                        f"The flame controller {name!r} has a universe "
                        f"that is not a number in {path}.")
                if not 1 <= univ <= 63999 or univ in seen:
                    raise FlameMapError(
                        f"The flame controller {name!r} uses universe "
                        f"{univ} twice, or outside 1 to 63999, in {path}.")
                seen.add(univ)
                raw.append((univ, count))
                if slot > UNIVERSE_SIZE:
                    break
                count = min(count, UNIVERSE_SIZE - slot + 1)
                rows.append((univ, slot, count))
                slot += count
        # Every OTHER controller (review of PR #96, P2-1): xLights sends any
        # controller that is not "Inactive" ("Active" and "xLights Only"
        # alike), and every sender in one xLights shares one CID and
        # priority, so the second-sender rule cannot tell its streams
        # apart. None may send E1.31 to this PC, and none may use a flame
        # universe number at all, whatever its state or address.
        for c in others:
            a = c.attrib
            cname = a.get("Name", "?")
            sends = a.get("ActiveState", "Active") != "Inactive"
            for net in c:
                if net.tag != "network":
                    continue
                na = net.attrib
                proto = netmap.UDP_PROTOCOLS.get(
                    (na.get("NetworkType") or "").lower())
                if proto != "e131":
                    continue
                dest = (na.get("ComPort", "") or a.get("IP", "")).strip()
                try:
                    univ = int(na.get("BaudRate", ""))
                except ValueError:
                    univ = None
                if univ is not None and univ in seen:
                    raise FlameMapError(
                        f"Controller {cname!r} uses E1.31 universe {univ}, "
                        f"which is one of the flame controller's, in "
                        f"{path}. Give it another universe in xLights: a "
                        f"flame universe belongs to the flame controller "
                        f"only.")
                if sends and (dest.startswith("127.") or
                              dest.lower() in ("localhost", "0.0.0.0") or
                              dest == want or _multicast(dest)):
                    raise FlameMapError(
                        f"Controller {cname!r} is not Inactive and sends "
                        f"E1.31 to {dest or 'no address'} in {path}: its "
                        f"packets could reach the {NAME} listener with the "
                        f"same sender identity as the flame controller. "
                        f"Give it its real address in xLights, or set it "
                        f"Inactive.")
        if not rows:
            raise FlameMapError(f"The flame controller {name!r} has no "
                                f"channels in {path}.")
        try:
            fp = file_fingerprint(path)
        except Exception as e:
            raise FlameMapError(f"{path} could not be read ({e}).")
        return cls(rows, path, stamp, want, raw, ips, fp, bad)

    def changed(self):
        """True when xlights_networks.xml is not the file this map was
        read from any more (or cannot be read)."""
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size) != self.stamp
        except Exception:
            return True


def _plain_ipv4(v):
    """True for one IPv4 address written plainly (no name, no spaces, no
    leading zeros)."""
    import ipaddress
    try:
        return isinstance(v, str) and str(ipaddress.IPv4Address(v)) == v
    except ValueError:
        return False


def _canonical(el):
    """An element as [tag, sorted attributes, children], text ignored
    (xLights keeps everything here in attributes)."""
    return [el.tag, sorted(el.attrib.items()), [_canonical(c) for c in el]]


def file_fingerprint(path):
    """8 hex digits from the whole xlights_networks.xml, canonicalized:
    every element, every attribute (each controller's name, ActiveState,
    IP, protocol, every network row's NetworkType, ComPort, universe and
    MaxChannels, past 512 slots or not). Any difference in the file
    changes it. tools/xlights_map_fingerprint.py computes the same from
    any xlights_networks.xml (the selftest holds them equal)."""
    import hashlib
    import xml.etree.ElementTree as ET
    root = ET.parse(path).getroot()
    text = json.dumps(_canonical(root), separators=(",", ":"),
                      ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()[:8]


def _multicast(dest):
    """True for xLights' MULTICAST word or a 224 to 239 address."""
    d = str(dest or "").strip().lower()
    if d == "multicast":
        return True
    head = d.split(".")[0]
    return head.isdigit() and 224 <= int(head) <= 239


def find_map(folder, name, show_networks=None, explicit=None,
             dest=LISTEN_IP):
    """The FlameMap for live flame programming: from `explicit` (the
    networks_xml setting) when given, else from every layout the engine
    knows (the show folder's xlights_networks.xml and each show file's
    own), which must all agree. FlameMapError otherwise. `dest` is where
    the flame controller must send (Settings.listen_ip)."""
    if explicit:
        return FlameMap.from_networks(explicit, name, dest)
    paths = []
    if folder:
        paths.append(os.path.join(folder, "xlights_networks.xml"))
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            names = []
        for n in names:
            if n.lower().endswith(".json") and show_networks is not None:
                try:
                    p = show_networks(os.path.join(folder, n))
                except Exception:
                    p = None
                if p:
                    paths.append(p)
    found = {}
    errors = []
    for p in paths:
        p = os.path.abspath(p)
        if p in found or not os.path.isfile(p):
            continue
        try:
            found[p] = FlameMap.from_networks(p, name, dest)
        except FlameMapError as e:
            errors.append(str(e))
    if errors:
        raise FlameMapError(errors[0])
    if not found:
        raise FlameMapError(
            f"No xlights_networks.xml with the flame controller {name!r} "
            f"was found in the show folder ({folder or 'none'}). Set "
            f"live_flame.networks_xml in ltcplay_remote.json to the one "
            f"xLights uses.")
    maps = list(found.values())
    if any(m.key() != maps[0].key() for m in maps[1:]):
        raise FlameMapError(
            "The show folder has more than one xlights_networks.xml and "
            "they lay the flame controller out differently, so which one "
            "xLights is using cannot be told. Set live_flame.networks_xml "
            "in ltcplay_remote.json.")
    return maps[0]


def flamesafe_destination(path):
    """(ip, port) flamesafe sends the flame node, from its own config, or
    None when it cannot be read."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
        d = doc["destination"]
        return str(d["ip"]), int(d.get("port", E131_PORT))
    except Exception:
        return None


def feedback_refusal(dest, settings):
    """A sentence when flamesafe's own output would arrive at this
    listener (its destination is this PC's loopback, on the listener's
    port), else "". Windows treats every 127.x address as one here."""
    if dest is None:
        return ("flamesafe's destination could not be read from its config, "
                "so whether its own output would loop back into the "
                "listener cannot be told.")
    ip, port = dest
    local = (ip.startswith("127.") or ip.lower() == "localhost"
             or ip == "0.0.0.0" or ip == settings.listen_ip)
    if local and port == settings.port:
        return (f"flamesafe sends its own output to {ip}:{port}, which is "
                f"this listener's address and port, so its own packets "
                f"would come back in. Point flamesafe's destination at the "
                f"flame node (or a spare address on the bench) first.")
    return ""


ADDRESS_KEYS = ("ip", "host", "address", "addr", "dest", "destination")


def _address_key(k):
    k = str(k).lower()
    return k in ADDRESS_KEYS or k.endswith(("_ip", "_host", "_address"))


def configured_ips(paths):
    """{IPv4 address: file name} for every IPv4 address anywhere in these
    JSON files (ltcplay_fire_ice.json, flamesafe's config): the devices
    the allowed Mac may not be. A missing path is skipped. Raises
    SettingsError (the mode is then refused) on a file that cannot be
    read, on anything that looks like an IPv4 address but is not written
    plainly (leading zeros, spaces), and on an address setting (ip, host,
    *_ip, ...) that is not one plain IPv4 address ("localhost" is
    loopback and allowed): a name cannot be compared, so it fails
    closed."""
    import re
    out = {}
    quad = re.compile(r"^\s*\d+\s*(\.\s*\d+\s*){3}$")

    def walk(v, where, key=""):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, where, k)
        elif isinstance(v, list):
            for x in v:
                walk(x, where, key)
        elif isinstance(v, str):
            if _plain_ipv4(v):
                out.setdefault(v, where)
            elif quad.match(v) or (_address_key(key) and v != "localhost"):
                raise SettingsError(
                    f"{where} has {key or 'an address'} {v!r}, which is not "
                    f"one plain IPv4 address, so whether it is the Mac "
                    f"cannot be told. Write it like 10.0.0.60.")
    for path in paths:
        if not path or not os.path.exists(path):
            continue
        with open(path, encoding="utf-8-sig") as fh:
            walk(json.load(fh), os.path.basename(path))
    return out


def is_this_pc(ip):
    """True when `ip` is an address of this PC: a UDP socket can be bound
    to it (port 0, closed at once, nothing sent or received)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


# --------------------------------------------------------------- the mode --

class _CutWhileStarting(Exception):
    """A cut() landed while begin() was turning the mode on."""


class LiveFlame:
    """The mode: on/off, the listener socket and its thread, the rules.

    link_get      () -> the engine's FlameLink, or None
    blockers      () -> why the engine forbids the mode now, or ""
    map_get       () -> FlameMap (raises FlameMapError)
    armed_groups  () -> names of the flame groups flamesafe reports armed,
                         or None when its status is not fresh
    fs_dest       () -> flamesafe's (ip, port) destination, or None
    settings_get  () -> Settings (raises SettingsError)
    disarm        (reason) -> True when the disarm of every group went out
    journal       (who, screen, outcome, text, fault=False)
    audit         audit.AuditLog or None
    threaded      False for the selftest: feed() and tick() by hand
    is_local      (ip) -> True when ip is an address of this PC
    device_ips    () -> {ip: where} of every configured device (the allowed
                  Mac may be none of them; raises when unreadable)"""

    def __init__(self, link_get, blockers, map_get, armed_groups,
                 fs_dest=lambda: None, settings_get=Settings,
                 disarm=None, journal=None, audit=None,
                 clock=time.monotonic, wall=time.time, threaded=True,
                 sock_factory=None, is_local=is_this_pc,
                 device_ips=lambda: {}):
        self._is_local = is_local
        self._device_ips = device_ips
        self._link_get = link_get
        self._blockers = blockers
        self._map_get = map_get
        self._armed = armed_groups
        self._fs_dest = fs_dest
        self._settings_get = settings_get
        self._disarm = disarm
        self._journal = journal
        self.audit = audit
        self._clock = clock
        self._wall = wall
        self._threaded = threaded
        self._sock_factory = sock_factory or (
            lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        self._lock = threading.RLock()
        self.on = False
        self._gen = 0
        self._sock = None
        self._socks = []              # every bound socket (_sock first)
        self._sock_kinds = []         # "loopback" or "listen_ip", by _socks
        self._thread = None
        self._watchdog = None
        self._thread_factory = threading.Thread   # the selftest's to swap
        self._session = None          # the flame link's program session
        self._cut_reason = ""         # why cut() stopped it, for end()
        self._cut_epoch = 0           # +1 on every cut(), lock-free
        self._starting = False        # a begin() is under way
        self._reset()
        self.last_end = None          # {"reason", "who", "at"}
        self.ends = 0
        self.datagrams = 0            # every datagram feed() took, ever

    def _reset(self):
        self.settings = None
        self.map = None
        self.began = None             # clock
        self.began_wall = None
        self.who = ""
        self.screen = ""
        self.sender = None            # (cid bytes, priority, source name)
        self.last_data = None         # clock of the last accepted packet
        self._univ = {}               # universe -> (values bytes, clock)
        self._seq = {}                # universe -> last sequence
        self._rate = collections.defaultdict(collections.deque)
        self._blocked = set()         # slots not yet seen at zero
        self.counts = collections.Counter()
        self._bad_said_at = None
        self._all = collections.deque()   # clock of every datagram, 1 s
        self._beat = None             # the listener loop's last pass
        self._last_tick = None

    # -- plumbing ---------------------------------------------------------
    def _note(self, who, screen, outcome, text, fault=False):
        if self._journal is None:
            return
        try:
            self._journal(who, screen, outcome, text, fault=fault)
        except Exception:
            pass

    def _audit(self, who, screen, action, result, reason, system=False):
        log = self.audit
        if log is None:
            return
        try:
            from . import audit as audit_mod
            log.event(user=audit_mod.SYSTEM if system else
                      (who or audit_mod.UNSIGNED), surface="engine",
                      action=action, target="live flame programming",
                      result=result, reason=reason, source=screen or "engine")
        except Exception:
            pass

    def _link(self):
        try:
            return self._link_get()
        except Exception:
            return None

    def link(self):
        """The engine's flame link, or None. Never raises."""
        return self._link()

    # -- on -----------------------------------------------------------------
    def refusal(self):
        """Why the mode may not start now, in a sentence, or "". Every
        check the entry makes except binding the port."""
        return self._check()[0]

    def _check(self):
        """(why, settings, map): every entry check, reading the engine's
        state, flamesafe's config and the show folder. Never under the
        mode's lock (second review of PR #96, N1): a slow disk here must
        never hold up a cut() at an Abort or a Hold."""
        try:
            s = self._settings_get()
        except SettingsError as e:
            return str(e), None, None
        except Exception as e:
            return (f"its settings could not be read ({type(e).__name__}).",
                    None, None)
        try:
            why = self._blockers() or ""
        except Exception as e:
            why = f"the engine's state could not be read ({type(e).__name__})."
        if why:
            return why, s, None
        link = self._link()
        if link is None or not hasattr(link, "program_begin"):
            return ("no flame link is connected in this engine, so nothing "
                    "could reach flamesafe."), s, None
        try:
            armed = self._armed()
        except Exception:
            armed = None
        if armed is None:
            return ("flamesafe has not reported in the last second, so its "
                    "armed groups are not known."), s, None
        if s.one_group and len(armed) > 1:
            return (f"{len(armed)} flame groups are armed "
                    f"({', '.join(armed)}). {NAME} allows one at a time: "
                    f"disarm the others first."), s, None
        try:
            why = feedback_refusal(self._fs_dest(), s)
        except Exception as e:
            why = f"flamesafe's destination could not be read ({e})."
        if why:
            return why, s, None
        try:
            fmap = self._map_get()
        except FlameMapError as e:
            return str(e), s, None
        except Exception as e:
            return (f"the flame controller's channels could not be read "
                    f"({type(e).__name__}: {e})."), s, None
        if getattr(fmap, "dest", LISTEN_IP) != s.listen_ip:
            return (f"the flame controller sends to "
                    f"{getattr(fmap, 'dest', '?')} but the listener is "
                    f"{s.listen_ip}."), s, None
        try:
            why = self._source_problem(s, fmap)
        except Exception as e:
            why = (f"the allowed Mac's address could not be checked "
                   f"({type(e).__name__}: {e}).")
        if why:
            return why, s, None
        return "", s, fmap

    def _source_problem(self, s, fmap):
        """With an allowed Mac: why its address (or this PC's listen_ip)
        cannot be used, or "". Fails closed."""
        mac, me = s.allowed_source_ip, s.listen_ip
        if mac is None:
            return ""
        if not self._is_local(me):
            return (f"'listen_ip' {me} is not an address of this PC. Set it "
                    f"to this PC's own show network address.")
        if self._is_local(mac):
            return (f"'allowed_source_ip' {mac} is an address of this PC "
                    f"itself, not the Mac's.")
        bad = getattr(fmap, "bad_addrs", ())
        if bad:
            return (f"{fmap.path} has an address that is not one plain IPv4 "
                    f"address: {bad[0]}. Whether it is the Mac cannot be "
                    f"told; write it like 10.0.0.60 in xLights.")
        fs = self._fs_dest()
        if fs is not None and not _plain_ipv4(fs[0]) and fs[0] != "localhost":
            return (f"flamesafe's destination {fs[0]!r} is not one plain "
                    f"IPv4 address, so whether it is the Mac cannot be told.")
        if fs is None or fs[0] == mac:
            return (f"'allowed_source_ip' {mac} is flamesafe's destination "
                    f"(the flame node), or that destination could not be "
                    f"read. The Mac must have its own address.")
        if mac in fmap.ips:
            return (f"'allowed_source_ip' {mac} is the address of controller "
                    f"{fmap.ips[mac]!r} in {fmap.path}. The Mac must have its "
                    f"own address.")
        try:
            devices = self._device_ips() or {}
        except SettingsError as e:
            return str(e)
        if mac in devices:
            return (f"'allowed_source_ip' {mac} is a device's address in "
                    f"{devices[mac]}. The Mac must have its own address.")
        return ""

    def _refused(self, who, screen, why, result="refused"):
        self._note(who, screen, "refused",
                   f"{who or 'Someone'} asked to turn {NAME} on on the "
                   f"{screen}; refused: {why}", fault=result != "refused")
        self._audit(who, screen, "live flame on", result, why)
        return False, f"{NAME} was not turned on: {why}"

    def begin(self, who, screen):
        """(ok, sentence). Turns the mode on after every check; the socket
        is bound only now, on 127.0.0.1 only. Every slow step (the checks,
        the show folder, the bind, the journal) is done outside the mode's
        lock, which is taken only to flip the state (second review of PR
        #96, N1)."""
        with self._lock:
            if self.on:
                return False, f"{NAME} is already on (since {self.who})."
            if self._starting:
                return False, f"{NAME} is already being turned on."
            self._starting = True
            epoch = self._cut_epoch
        try:
            return self._begin(who, screen, epoch)
        finally:
            with self._lock:
                self._starting = False

    def _begin(self, who, screen, epoch):
        why, s, fmap = self._check()
        if why:
            return self._refused(who, screen, why)
        socks = []
        where = ""
        try:
            check_listen_ip(s.listen_ip, s.allowed_source_ip)
            for addr in s.binds():
                where = f"{addr[0]}:{addr[1]}"
                # Unicast only: never the any address, never a multicast
                # group (nothing here joins one).
                if addr[0] not in (LISTEN_IP, s.listen_ip) or \
                        addr[0] in ("", "0.0.0.0") or _multicast(addr[0]):
                    raise SettingsError(f"{NAME} never binds {addr[0]!r}.")
                sock = self._sock_factory()
                socks.append(sock)
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    sock.setsockopt(socket.SOL_SOCKET,
                                    socket.SO_EXCLUSIVEADDRUSE, 1)
                sock.bind(addr)
                sock.settimeout(WATCH_S)
        except Exception as e:
            for sock in socks:
                try:
                    sock.close()
                except Exception:
                    pass
            if isinstance(e, OSError):
                why = (f"{where} could not be opened ({e}). "
                       f"Another program on this PC is using it.")
            else:
                why = f"it could not be set up ({type(e).__name__}: {e})."
            return self._refused(who, screen, why)
        # From here on it is all or nothing (review of PR #96, P3-6): any
        # failure leaves the mode OFF, the socket closed and the link's
        # program slot shut.
        link = self._link()
        why = ""
        with self._lock:
            try:
                self._reset()
                self.settings, self.map = s, fmap
                self._blocked = set(fmap.slots)
                self.began = self.last_data = self._beat = self._clock()
                self.began_wall = self._wall()
                self.who, self.screen = who or "unsigned", screen
                self._cut_reason = ""
                self._session = link.program_begin()
                # A cut() since the checks (an Abort, a Hold, Start now) wins:
                # checked AFTER the link's slot opened, so a cut that lands
                # now either is seen here or shuts the slot itself.
                if self._cut_epoch != epoch:
                    raise _CutWhileStarting()
                self._socks = socks
                self._sock_kinds = [
                    "loopback" if a == LISTEN_IP else "listen_ip"
                    for a, _p in s.binds()]
                self._sock = socks[0]
                self._gen += 1
                self.on = True
                gen = self._gen
                if self._threaded:
                    self._thread = self._thread_factory(
                        target=self._run, args=(gen,), daemon=True,
                        name="ltcplay-live-flame")
                    self._thread.start()
                    self._watchdog = self._thread_factory(
                        target=self._watch, args=(gen,), daemon=True,
                        name="ltcplay-live-flame-watchdog")
                    self._watchdog.start()
            except Exception as e:
                self.on = False
                self._gen += 1
                self._sock = None
                self._socks = []
                for sock in socks:
                    try:
                        sock.close()
                    except Exception:
                        pass
                try:
                    link.program_end()
                except Exception:
                    pass
                self._reset()
                why = ("an Abort, Hold or Start now was pressed while it was "
                       "starting." if isinstance(e, _CutWhileStarting) else
                       f"it could not be started ({type(e).__name__}: {e}).")
                cut = isinstance(e, _CutWhileStarting)
        if why:
            return self._refused(who, screen, why,
                                 "refused" if cut else "error")
        rows = "; ".join(f"universe {u} channels 1 to {c} = flamesafe slots "
                         f"{f} to {f + c - 1}" for u, f, c in fmap.rows)
        listen = " and ".join(f"{a}:{p}" for a, p in s.binds())
        src = ("from this PC only" if s.allowed_source_ip is None else
               f"from this PC and the Mac at {s.allowed_source_ip} only")
        text = (f"{self.who} turned {NAME} ON on the {screen} (session "
                f"{self._session}). Listening for xLights E1.31 on "
                f"{listen} only, {src}: {rows}; map fingerprint "
                f"{fmap.fingerprint}. "
                f"flamesafe fires only an armed group; "
                f"{'one group at a time; ' if s.one_group else ''}off after "
                f"{s.idle_off_s} s with no xLights data or {s.max_session_s} "
                f"s in all. Leaving it disarms every flame group.")
        self._note(self.who, screen, "on", text)
        self._audit(self.who, screen, "live flame on", "accepted",
                    f"{listen}, {s.source()}")
        return True, text

    # -- off ----------------------------------------------------------------
    def cut(self, reason):
        """The cheap half of ending, for an Abort or a Hold press (review of
        PR #96, P3-3): the flame link's program slot is shut now (zeros
        from the next frame, nothing more taken), with no journal line, no
        disk and no wait. It never takes the mode's lock (second review,
        N1), so a begin() reading a slow disk cannot hold it up; a begin()
        under way sees the cut and does not turn the mode on. end() does
        the rest, off the press's thread. True when it was on. Never
        raises."""
        self._cut_epoch += 1
        was_on = self.on
        if was_on and not self._cut_reason:
            self._cut_reason = " ".join(str(reason or "").split())[:200]
        link = self._link()
        if link is not None:
            try:
                link.program_end()
            except Exception:
                pass
        return was_on

    def end(self, reason, who="", screen="", system=True):
        """Off, for any reason: the listener closed, the link's frame
        dropped, and every flame group disarmed (D2). True when it was on.
        Never raises."""
        why = " ".join(str(reason or "turned off").split())[:200]
        by = who or ("system" if system else "unsigned")
        with self._lock:
            if not self.on:
                return False
            self.on = False
            # Said in the same step as "off" (CI shard 6 of run
            # 39883993911: a reader that saw on false before end() had
            # finished read no reason at all).
            self.ends += 1
            self.last_end = {"reason": why, "who": by,
                             "at": int(self._wall() * 1000),
                             "started_by": self.who}
            self._gen += 1
            my_gen = self._gen
            session = self._session
            socks, self._socks = list(self._socks), []
            if self._sock is not None and self._sock not in socks:
                socks.append(self._sock)
            self._sock = None
            link = self._link()
        if link is not None:
            try:
                link.program_end()
            except Exception:
                pass
        for sock in socks:
            try:
                sock.close()
            except Exception:
                pass
        ok = False
        try:
            if self._disarm is not None:
                ok = bool(self._disarm(f"{NAME} ended: {why}"))
            elif link is not None:
                ok = bool(link.disarm_all(f"{NAME} ended: {why}"))
        except Exception:
            ok = False
        self._note(by, screen or "engine", "off",
                   f"{NAME} is OFF (session {session}): {why}. Every flame value is zero and "
                   f"{'a disarm of every flame group went out' if ok else 'the disarm of every flame group did NOT go out: disarm from the Stream Deck'}.",
                   fault=not ok)
        self._audit(by, screen or "engine", "live flame off", "accepted",
                    why, system=system and not who)
        # Never joined here (review of PR #96, P3-3): the listener sees the
        # new generation within WATCH_S and stops by itself, and the flame
        # link takes nothing from it any more (its session is over).
        # Only this session's handles (second review of PR #96, N2): an Off
        # then a quick On must not have this end() wipe the new session's.
        with self._lock:
            if self._gen == my_gen:
                self._thread = None
                self._watchdog = None
        return True

    # -- the listener thread ----------------------------------------------
    def _run(self, gen):
        try:
            while True:
                with self._lock:
                    if not self.on or self._gen != gen:
                        return
                    socks = list(self._socks)
                    kinds = dict(zip(map(id, socks), self._sock_kinds))
                got = []
                try:
                    if len(socks) == 1:
                        ready = socks
                    else:
                        ready = select.select(socks, [], [], WATCH_S)[0]
                    for sock in ready:
                        try:
                            data, addr = sock.recvfrom(PACKET_MAX + 64)
                            got.append((data, addr,
                                        kinds.get(id(sock), "unknown")))
                        except socket.timeout:
                            pass
                except OSError:
                    if not self.on or self._gen != gen:
                        return
                    time.sleep(WATCH_S)
                now = self._clock()
                self._beat = now
                for data, addr, via in got:
                    self.feed(data, addr, now, via)
                last = self._last_tick
                if last is None or now - last >= WATCH_S or now < last:
                    self._last_tick = now
                    self.tick(now)
        except Exception as e:
            self.end(f"the listener failed ({type(e).__name__}: {e})")

    def _watch(self, gen):
        """The watchdog thread (review of PR #96, P3-6)."""
        while True:
            time.sleep(WATCHDOG_S)
            with self._lock:
                if not self.on or self._gen != gen:
                    return
            self.watchdog_check()

    def watchdog_check(self, now=None):
        """Ends the mode when the listener thread is gone or has not made a
        pass for LISTENER_STALL_S. Returns the reason, or "". Never
        raises."""
        now = self._clock() if now is None else now
        with self._lock:
            if not self.on:
                return ""
            t, beat = self._thread, self._beat
        why = ""
        if self._threaded and (t is None or not t.is_alive()):
            why = "the listener thread stopped"
        elif beat is not None and now - beat > LISTENER_STALL_S:
            why = (f"the listener made no pass for more than "
                   f"{LISTENER_STALL_S:g} s")
        if why:
            self.end(why)
        return why

    # -- one packet ----------------------------------------------------------
    def feed(self, data, addr, now=None, via=None):
        """One datagram from the socket `via` ("loopback": 127.0.0.1,
        "listen_ip": this PC's show network address; None means loopback
        when no Mac is allowed). Returns what was done with it, a word,
        for the selftest. Never raises."""
        now = self._clock() if now is None else now
        with self._lock:
            if not self.on:
                return "off"
            self.datagrams += 1
            q = self._all
            q.append(now)
            while q and now - q[0] > RATE_WINDOW_S:
                q.popleft()
            flood = len(q) > TOTAL_MAX
        if flood:
            self.end(f"more than {TOTAL_MAX} datagrams in a second reached "
                     f"the listener (a flood)")
            return "ended"
        try:
            host = addr[0] if addr else ""
        except Exception:
            host = ""
        host = str(host)
        st = self.settings
        mac = getattr(st, "allowed_source_ip", None)
        if via is None and mac is None:
            via = "loopback"
        # Where it came from must match the socket it arrived on: loopback
        # only on the 127.0.0.1 socket, the Mac only on listen_ip's.
        if host.startswith("127.") and via == "loopback":
            origin = "this PC"
        elif mac is not None and host == mac and via == "listen_ip":
            origin = f"Mac {mac}"
        elif host.startswith("127.") or (mac is not None and host == mac):
            self.end(f"a packet from {host} arrived on the "
                     f"{via or 'unknown'} socket; this PC's packets are taken "
                     f"on 127.0.0.1 only and the Mac's on listen_ip only")
            return "ended"
        else:
            if mac is None:
                self.end(f"a packet came from {host or 'an unknown address'}"
                         f", not from this PC; the listener takes loopback "
                         f"only")
            else:
                self.end(f"a packet came from {host or 'an unknown address'}"
                         f", not from this PC and not from the allowed Mac "
                         f"{mac}; nothing else on the network may send")
            return "ended"
        try:
            p = parse_e131(data)
        except NotData:
            self.counts["not_data"] += 1
            return "ignored"
        except PacketError as e:
            self.counts["malformed"] += 1
            self._bad(now, str(e))
            return "malformed"
        if p.cid == FLAMESAFE_CID or p.source.strip().lower() == \
                FLAMESAFE_NAME:
            self.end("flamesafe's own E1.31 output came back into the "
                     "listener (a loopback feedback); its destination must "
                     "not be this PC's listener")
            return "ended"
        # One sender: its CID, its priority and where it sends from (this
        # PC or the Mac), across every socket.
        ident = (p.cid, p.priority, origin)
        with self._lock:
            if not self.on:
                return "off"
            if self.sender is None:
                self.sender = (p.cid, p.priority, p.source, origin)
                first = True
            else:
                first = False
            held = self.sender
            other = (held[0], held[1], held[3]) != ident
        if other:
            self.end(f"a second E1.31 source appeared ({p.source!r} from "
                     f"{origin}, CID {p.cid.hex()[:8]}, priority "
                     f"{p.priority}, while {held[2]!r} from {held[3]}, CID "
                     f"{held[0].hex()[:8]}, priority {held[1]} held the "
                     f"listener); only one sender is taken")
            return "ended"
        if first:
            self._note(self.who, self.screen, "sender",
                       f"{NAME}: the first E1.31 source is "
                       f"{p.source!r} from {origin} at priority "
                       f"{p.priority}. Any other source ends the mode.")
        # A flood ends the mode, whatever universe it is on.
        if p.universe not in self._rate and len(self._rate) >= 64:
            self._rate.clear()
        q = self._rate[p.universe]
        q.append(now)
        while q and now - q[0] > RATE_WINDOW_S:
            q.popleft()
        if len(q) > RATE_MAX:
            self.end(f"universe {p.universe} sent more than {RATE_MAX} "
                     f"packets in a second (a flood)")
            return "ended"
        fmap = self.map
        if fmap is None or p.universe not in fmap.by_universe:
            self.counts["other_universe"] += 1
            return "other universe"
        if p.preview:
            self.counts["preview"] += 1
            return "preview"
        last = self._seq.get(p.universe)
        if last is not None:
            d = (p.seq - last) & 0xFF
            if d == 0 or d > 256 - SEQ_WINDOW:
                self.counts["out_of_order"] += 1
                return "out of order"
        self._seq[p.universe] = p.seq
        with self._lock:
            if not self.on:
                return "off"
            if p.terminated:
                self._univ.pop(p.universe, None)
                self._reblock(p.universe)
                self._seq.pop(p.universe, None)
                self.counts["terminated"] += 1
            else:
                self._univ[p.universe] = (p.values, now)
                self.last_data = now
                self.counts["taken"] += 1
        self._push(now)
        return "terminated" if p.terminated else "taken"

    def _bad(self, now, why):
        if self._bad_said_at is None or now - self._bad_said_at > 10.0:
            self._bad_said_at = now
            self._note(self.who, self.screen, "malformed",
                       f"{NAME}: a packet that is not an E1.31 data packet "
                       f"was ignored ({why}). This line will not repeat for "
                       f"10 s.", fault=True)

    def _reblock(self, universe):
        fmap = self.map
        if fmap is None or universe not in fmap.by_universe:
            return
        first, count = fmap.by_universe[universe]
        self._blocked.update(range(first, first + count))

    def compose(self, now):
        """The 512 slot frame from every fresh universe, with each slot not
        yet seen at zero held at zero. A stale universe is dropped and its
        slots wait for a zero again."""
        vals = bytearray(UNIVERSE_SIZE)
        fmap = self.map
        if fmap is None:
            return bytes(vals)
        heard = []          # (first, count) of universes with fresh data
        for u, (first, count) in fmap.by_universe.items():
            got = self._univ.get(u)
            if got is None:
                continue
            data, at = got
            if now - at > HOLD_S or now < at:
                del self._univ[u]
                self._reblock(u)
                continue
            n = min(count, len(data))
            vals[first - 1:first - 1 + n] = data[:n]
            heard.append((first, n))
        blocked = self._blocked
        if blocked:
            # Seen at zero means SENT at zero by xLights: a slot is freed
            # only by a fresh packet of its own universe, never by the
            # zeros a stale or missing universe composes to.
            for first, count in heard:
                for s in range(first, first + count):
                    if s in blocked and vals[s - 1] == 0:
                        blocked.discard(s)
            for s in blocked:
                vals[s - 1] = 0
        return bytes(vals)

    def _push(self, now):
        why = self._groups_problem()
        if why:
            # Seen on the packet that would carry the values, before any
            # value is set (review of PR #96, P3-1): zero, disarm, end.
            self.end(why)
            return
        with self._lock:
            if not self.on:
                return
            frame = self.compose(now)
            link = self._link()
            session = self._session
        if link is None:
            return
        if any(frame):
            link.program_set(frame, session)
        else:
            link.program_clear()

    def _groups_problem(self):
        """Why the armed (or being armed) flame groups end the mode, or
        "": flamesafe not reporting, or more than one group armed or asked
        for with one group at a time."""
        try:
            armed = self._armed()
        except Exception as e:
            return f"flamesafe's armed groups could not be read ({e})"
        if armed is None:
            return ("flamesafe stopped reporting, so its armed groups are not "
                    "known")
        s = self.settings
        if s is not None and s.one_group and len(armed) > 1:
            return (f"{len(armed)} flame groups were armed or being armed at "
                    f"once ({', '.join(armed)}); {NAME} allows one at a time")
        return ""

    # -- the rules, every WATCH_S --------------------------------------------
    def tick(self, now=None):
        """Ends the mode when anything says so. Returns the reason it ended
        for, or "". Never raises."""
        now = self._clock() if now is None else now
        with self._lock:
            if not self.on:
                return ""
            s = self.settings
            link = self._link()
        why = ""
        try:
            if link is None or not getattr(link, "program_on", False):
                why = self._cut_reason or (
                    "the show zeroed or disarmed the flames (a Hold, an "
                    "Abort, a disarm of every group or a show ending)")
            if not why:
                why = self._blockers() or ""
            if not why:
                why = self._groups_problem()
            if not why and self.map is not None and self.map.changed():
                why = "xlights_networks.xml changed while it was on"
            if not why and now - self.last_data > s.idle_off_s:
                why = (f"no xLights data for {s.idle_off_s} s (auto off)")
            if not why and now - self.began > s.max_session_s:
                why = (f"it had been on for {s.max_session_s} s, the most "
                       f"one session may last (auto off)")
        except Exception as e:
            why = f"a rule could not be checked ({type(e).__name__}: {e})"
        if why:
            self.end(why)
            return why
        # Partial staleness: a universe that went quiet is zeros now.
        with self._lock:
            stale = [u for u, (_d, at) in self._univ.items()
                     if now - at > HOLD_S]
        if stale:
            self._push(now)
        return ""

    # -- the page ----------------------------------------------------------
    def view(self):
        now = self._clock()
        with self._lock:
            on = self.on
            out = {"on": on, "name": NAME, "last_end": self.last_end}
            if not on:
                return out
            s, fmap = self.settings, self.map
            sender = self.sender
            out.update({
                "who": self.who, "screen": self.screen,
                "since": (int(self.began_wall * 1000)
                          if self.began_wall else None),
                "on_s": round(now - self.began, 1),
                "listen": " and ".join(f"{a}:{p}" for a, p in s.binds()),
                "source": s.source(),
                "fingerprint": fmap.fingerprint if fmap else "",
                "map": fmap.view() if fmap else [],
                "sender": ({"source": sender[2], "priority": sender[1],
                            "from": sender[3]} if sender else None),
                "last_data_s": round(now - self.last_data, 1),
                "receiving": any(now - at <= HOLD_S
                                 for _d, at in self._univ.values()),
                "idle_off_s": s.idle_off_s,
                "max_session_s": s.max_session_s,
                "one_group_at_a_time": s.one_group,
                "counts": dict(self.counts)})
        return out
