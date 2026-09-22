"""Read xlights_networks.xml and build the absolute-channel -> universe map.

xLights assigns absolute channels by walking the controllers in document order
and, inside each controller, its <network> children in order; each consumes
MaxChannels channels starting at 1.  That is the same order the sequencer uses
when it renders an FSEQ, so reading this file is what keeps the player aligned
with the show without any separate configuration.

Only Ethernet controllers with a udp-style protocol are usable here.  Anything
else (serial, null, an inactive controller) still consumes its channel span --
dropping it silently would shift every controller after it.
"""
import xml.etree.ElementTree as ET
import os

UDP_PROTOCOLS = {"artnet": "artnet", "e131": "e131", "sacn": "e131"}


class Universe:
    __slots__ = ("ip", "protocol", "universe", "start", "count", "controller")

    def __init__(self, ip, protocol, universe, start, count, controller):
        self.ip = ip
        self.protocol = protocol
        self.universe = universe
        self.start = start          # 1-based absolute start channel
        self.count = count
        self.controller = controller

    @property
    def end(self):
        return self.start + self.count - 1

    def __repr__(self):
        return (f"<{self.protocol} {self.ip} u{self.universe} "
                f"ch {self.start}..{self.end} ({self.controller})>")


class NetMap:
    def __init__(self, universes, total_channels, skipped):
        self.universes = universes
        self.total_channels = total_channels
        self.skipped = skipped      # [(controller, reason, channel span)]

    def summary(self):
        by_proto = {}
        for u in self.universes:
            by_proto[u.protocol] = by_proto.get(u.protocol, 0) + 1
        parts = ", ".join(f"{n} x {p}" for p, n in sorted(by_proto.items()))
        ips = sorted({u.ip for u in self.universes})
        s = (f"{len(self.universes)} universes ({parts}) across {len(ips)} "
             f"controllers, {self.total_channels} channels total")
        if self.skipped:
            s += f"; {len(self.skipped)} controller(s) not reachable over UDP"
        return s


def load(path):
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    root = ET.parse(path).getroot()
    universes = []
    skipped = []
    chan = 1
    for c in root:
        if c.tag != "Controller":
            continue
        a = c.attrib
        name = a.get("Name", "?")
        ip = a.get("IP", "")
        active = (a.get("ActiveState", "Active") == "Active")
        nets = [n for n in c if n.tag == "network"]
        span = 0
        usable = []
        for n in nets:
            na = n.attrib
            count = int(na.get("MaxChannels", "0") or 0)
            if count <= 0:
                continue
            proto = UDP_PROTOCOLS.get((na.get("NetworkType") or "").lower())
            # ComPort carries the destination for ethernet rows, BaudRate the
            # universe number.  xLights reuses the serial field names here.
            univ_raw = na.get("BaudRate", "")
            dest = na.get("ComPort", "") or ip
            usable.append((proto, dest, univ_raw, count))
            span += count

        if not nets:
            # A controller with no network rows still may declare a span
            # elsewhere; nothing to consume, nothing to send.
            continue

        if not active:
            skipped.append((name, "controller is inactive", span))
            chan += span
            continue

        for proto, dest, univ_raw, count in usable:
            if proto is None:
                skipped.append((name, "not a UDP protocol", count))
            elif not dest:
                skipped.append((name, "no destination address", count))
            else:
                try:
                    univ = int(univ_raw)
                except ValueError:
                    skipped.append((name, f"universe {univ_raw!r} is not a number", count))
                    univ = None
                if univ is not None:
                    universes.append(Universe(dest, proto, univ, chan, count, name))
            chan += count

    return NetMap(universes, chan - 1, skipped)
