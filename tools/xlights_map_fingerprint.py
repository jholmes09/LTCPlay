#!/usr/bin/env python3
"""Prints the live flame programming map fingerprint of an
xlights_networks.xml, the same 8 hex digits the rack screen's LIVE FLAME
PROGRAMMING bar shows (ltcplay/flameprog.py map_fingerprint).

One file, the Python standard library only, so it runs on the Mac with
python3:

    python3 xlights_map_fingerprint.py ~/xLights/show/xlights_networks.xml
    python3 xlights_map_fingerprint.py xlights_networks.xml "Flame Node"

With a controller name it prints that controller's fingerprint. Without
one it prints a line for every controller. The fingerprint is a short
hash of the controller's universe numbers and channel counts, in the
file's order. When the Mac's flame controller line matches the rack
screen, the Mac's xLights lays the flame channels out exactly as the show
PC reads them. LIVE_FLAME_PROGRAMMING.md says how to use it.
"""
import hashlib
import sys
import xml.etree.ElementTree as ET


def rows(controller):
    """(universe, channel count) of every network row with channels."""
    out = []
    for net in controller:
        if net.tag != "network":
            continue
        try:
            count = int(net.attrib.get("MaxChannels", "0") or 0)
        except ValueError:
            count = 0
        if count <= 0:
            continue
        out.append((int(net.attrib.get("BaudRate", "")), count))
    return out


def fingerprint(pairs):
    text = ";".join(f"{int(u)}:{int(c)}" for u, c in pairs)
    return hashlib.sha256(text.encode("ascii")).hexdigest()[:8]


def main(argv):
    if len(argv) not in (2, 3):
        print(__doc__.strip())
        return 2
    path = argv[1]
    want = argv[2] if len(argv) == 3 else None
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as e:
        print(f"{path} could not be read: {e}")
        return 1
    found = 0
    for c in root:
        if c.tag != "Controller":
            continue
        name = c.attrib.get("Name", "")
        if want is not None and name != want:
            continue
        try:
            pairs = rows(c)
        except ValueError:
            print(f"{name}: a universe that is not a number")
            continue
        found += 1
        unis = ", ".join(f"universe {u} x {n}" for u, n in pairs)
        print(f"{name}: {fingerprint(pairs)}  ({unis or 'no channels'})")
    if not found:
        print(f"No controller {want!r} in {path}." if want else
              f"No controllers in {path}.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
