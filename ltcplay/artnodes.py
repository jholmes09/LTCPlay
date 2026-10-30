"""Art-Net timecode receivers on the show PC: one address each, and every
address one that this PC really has (soak 2026-10-21).

In the soak MadMapper and BEYOND both took their Art-Net timecode on this
PC's loopback at port 6454. Windows gave every packet to one socket, so
BEYOND never chased timecode, and it took hours to find. Two receivers on
one PC need two addresses (a Windows loopback adapter each), and MadMapper's
DMX input is a receiver too, so it counts.

Two checks, on the show file's clock.artnet.nodes:

  collisions()   two nodes that are one address on port 6454: the same
                 address, or two loopback spellings (127.0.0.1, localhost
                 and every other 127.x.x.x are all this PC's loopback).
  not_here()     a node whose address no adapter on this PC has: an
                 unplugged cable, an adapter that is off, or a venue
                 network that gave the PC another address.

Pure logic plus one stdlib probe (a UDP bind to the address on port 0,
which works only when the address is on this PC and never touches 6454).
A probe that cannot tell gives a warning-level note, never a fault.
Nothing here blocks a show: the exact-duplicate refusal at show load is
clock.py's and is unchanged.
"""
import errno
import ipaddress
import socket
import sys

PORT = 6454
FIX = ("Give each receiver its own address, for example a Windows loopback "
       "adapter; MadMapper's DMX input counts as a receiver.")
LOOPBACK = "127.0.0.1"
WSAEADDRNOTAVAIL = 10049


def resolve(ip):
    """The IPv4 address a node's setting means: localhost is 127.0.0.1.
    None when it is not an address at all (clock.py refuses those)."""
    if not isinstance(ip, str):
        return None
    s = ip.strip()
    if s.lower() == "localhost":
        return LOOPBACK
    try:
        return str(ipaddress.IPv4Address(s))
    except (ipaddress.AddressValueError, ValueError):
        return None


def is_loopback(addr):
    try:
        return ipaddress.IPv4Address(addr).is_loopback
    except (ipaddress.AddressValueError, ValueError):
        return False


def port_key(addr):
    """Which receiving address `addr` is on port 6454: every 127.x.x.x is
    the one loopback, anything else is itself."""
    return LOOPBACK if is_loopback(addr) else addr


def _named(nodes):
    """[(name, raw setting, resolved address)] for the nodes that are an
    address, in the show file's order."""
    out = []
    if not isinstance(nodes, dict):
        return out
    for name, ip in nodes.items():
        addr = resolve(ip)
        if addr is not None and isinstance(name, str) and name.strip():
            out.append((name.strip(), str(ip).strip(), addr))
    return out


def collisions(nodes):
    """One sentence for each address on port 6454 that two or more nodes
    share, naming them and what to do. [] when every node has its own."""
    groups = {}
    for name, raw, addr in _named(nodes):
        groups.setdefault(port_key(addr), []).append((name, raw, addr))
    out = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        ns = [n for n, _r, _a in members]
        names = ", ".join(ns[:-1]) + " and " + ns[-1]
        spelled = sorted({r for _n, r, _a in members})
        if len(spelled) == 1:
            where = f"are all {spelled[0]}" if len(members) > 2 \
                else f"are both {spelled[0]}"
        else:
            where = (f"are on {', '.join(spelled)}, which are all this PC's "
                     f"own loopback (every 127.x.x.x address and localhost "
                     f"are one address)")
        out.append(f"Art-Net timecode: {names} {where}, so they share one "
                   f"address on port {PORT}. Windows gives every packet to "
                   f"one of them and the other never chases timecode. {FIX}")
    return out


def bind_probe(addr):
    """True when `addr` is on one of this PC's adapters, False when the PC
    says it is not, None when it cannot tell. A UDP bind on port 0: it
    takes whatever port is free, never 6454, and is closed at once."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return None
    try:
        s.bind((addr, 0))
        return True
    except OSError as e:
        if e.errno == errno.EADDRNOTAVAIL or \
                getattr(e, "winerror", None) == WSAEADDRNOTAVAIL:
            return False
        return None
    finally:
        try:
            s.close()
        except OSError:
            pass


def adapter_addresses():
    """This PC's IPv4 addresses as the stdlib can list them (the host
    name's), or None when it cannot. On Windows that is every adapter that
    is up, loopback adapters included; elsewhere it can be short, which is
    why bind_probe is asked before anything is called missing."""
    try:
        _h, _a, addrs = socket.gethostbyname_ex(socket.gethostname())
    except (OSError, UnicodeError):
        return None
    return {a for a in addrs if resolve(a)}


def not_here(nodes, adapters=None, probe=bind_probe, windows=None):
    """[(level, sentence)] for the nodes whose address is not on this PC.

    level "bad": the PC says the address is not one of its own, on Windows
    (the show PC, where every receiver runs). Off Windows a receiver on
    another machine can be right, so the same finding is "warn". A node
    nobody can tell about is "warn" too: unknown is never a fault and never
    shown as fine. `adapters` is adapter_addresses() (None when unknown)."""
    if windows is None:
        windows = sys.platform == "win32"
    out = []
    for name, _raw, addr in _named(nodes):
        if is_loopback(addr):
            continue            # every 127.x.x.x answers on Windows
        if adapters is not None and addr in adapters:
            continue
        try:
            here = probe(addr) if probe is not None else None
        except Exception:                        # noqa: BLE001
            here = None
        if here:
            continue
        if here is None:
            out.append(("warn",
                        f"{name} node {addr}: this PC's network adapters "
                        f"could not be read, so whether it is an address on "
                        f"this PC is not known."))
            continue
        level = "bad" if windows else "warn"
        tail = ("Plug in its cable or turn its adapter back on, or, if the "
                "venue network gave this PC a different address, set "
                "clock.artnet.nodes in the show file to match." if windows
                else "That is right only if this receiver runs on another "
                     "machine.")
        out.append((level, f"{name} node {addr} is not an address on this "
                           f"PC. {tail}"))
    return out


def node_faults(nodes, adapters=None, probe=bind_probe, windows=None):
    """[(level, sentence)]: every collision ("bad"), then not_here()."""
    return [("bad", t) for t in collisions(nodes)] + \
        not_here(nodes, adapters, probe, windows)


def show_file_nodes(path):
    """clock.artnet.nodes of the show file at `path` as written (not
    validated: a collision is named even in a file clock.py refuses), or
    None when it is not a show file naming any."""
    import json
    try:
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or "cues" not in doc:
        return None
    clock = doc.get("clock")
    art = clock.get("artnet") if isinstance(clock, dict) else None
    nodes = art.get("nodes") if isinstance(art, dict) else None
    return nodes if isinstance(nodes, dict) and nodes else None


def folder_faults(folder, adapters=None, probe=bind_probe, windows=None,
                  active=None):
    """node_faults() for every show file in `folder` that names Art-Net
    nodes: [(level, "<file>: sentence")], and how many files were looked
    at. Never raises.

    `active` (Jeff, 2026-10-23: an old, unused soak_show.json was a live
    fault at start-up): the show file names in use (loaded, or the only
    show file there is). A file not among them gives only quiet notes
    (level "note", never a fault or an alert). None: every file counts,
    as before."""
    import os
    out, seen = [], 0
    try:
        names = sorted(os.listdir(folder)) if folder else []
    except OSError:
        return out, seen
    for n in names:
        if not n.lower().endswith(".json") or n.startswith("_superseded"):
            continue
        nodes = show_file_nodes(os.path.join(folder, n))
        if nodes is None:
            continue
        seen += 1
        quiet = active is not None and n not in active
        for level, text in node_faults(nodes, adapters, probe, windows):
            if quiet:
                out.append(("note", f"{n} (not the show in use): {text}"))
            else:
                out.append((level, f"{n}: {text}"))
    return out, seen
