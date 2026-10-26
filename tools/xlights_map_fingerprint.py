#!/usr/bin/env python3
"""Prints the live flame programming map fingerprint of an
xlights_networks.xml, the same 8 hex digits the rack screen's LIVE FLAME
PROGRAMMING bar shows (ltcplay/flameprog.py file_fingerprint), and says
in plain words how each controller is set.

One file, the Python standard library only, so it runs on the Mac with
python3:

    python3 xlights_map_fingerprint.py ~/xLights/show/xlights_networks.xml
    python3 xlights_map_fingerprint.py xlights_networks.xml "Flame Node"

The fingerprint is a short hash of the WHOLE file, canonicalized: every
controller's name, state, addresses, protocols, universes and channel
counts. Any difference changes it. When it matches the rack screen, the
Mac's file is the same as the show PC's. It cannot show what the Mac's
xLights actually sends; LIVE_FLAME_PROGRAMMING.md says what covers that.
"""
import hashlib
import json
import sys
import xml.etree.ElementTree as ET


def _canonical(el):
    return [el.tag, sorted(el.attrib.items()), [_canonical(c) for c in el]]


def fingerprint(root):
    text = json.dumps(_canonical(root), separators=(",", ":"),
                      ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()[:8]


def describe(c):
    """One controller in plain words."""
    a = c.attrib
    parts = []
    slot = 1
    for net in c:
        if net.tag != "network":
            continue
        n = net.attrib
        proto = n.get("NetworkType") or "no protocol"
        dest = n.get("ComPort") or a.get("IP") or "no address"
        kind = "multicast" if dest.upper() == "MULTICAST" else "to " + dest
        try:
            count = int(n.get("MaxChannels", "0") or 0)
        except ValueError:
            count = 0
        over = ""
        if count > 0 and slot + count - 1 > 512:
            over = " (past 512 slots: not used)"
        parts.append(f"{proto} {kind}, universe {n.get('BaudRate', '?')}, "
                     f"{count} channels{over}")
        slot += max(count, 0)
    state = a.get("ActiveState", "Active")
    return (f"{a.get('Name', '')}: state {state}; "
            + ("; ".join(parts) or "no network rows"))


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
    print(f"Map fingerprint: {fingerprint(root)}")
    found = 0
    for c in root:
        if c.tag != "Controller":
            continue
        if want is not None and c.attrib.get("Name", "") != want:
            continue
        found += 1
        print(describe(c))
    if not found:
        print(f"No controller {want!r} in {path}." if want else
              f"No controllers in {path}.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
