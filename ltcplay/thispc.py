"""Is an address this PC's own? The one answer the BEYOND watch, the
MadMapper watch, Open BEYOND and the pre-roll's Lasers check all use (Jeff,
2026-10-30: his show PC feeds BEYOND over an internal loopback adapter,
and ltcplay said BEYOND was "on another computer").

A program's OSC host is on this PC when it is:

  - "localhost", or any 127.x.x.x loopback address (127.0.0.2 is the show
    PC's BEYOND, bench B8.0), as before; or
  - one of THIS PC's own interface addresses: a LAN address, or the
    address of a Windows loopback adapter such as "Microsoft KM-TEST
    Loopback Adapter" (often 10.x, 169.254.x or 192.168.x).

Anything else is another machine, exactly as before: an address that is
not one of this PC's is never taken for this PC. "0.0.0.0", a broadcast or
multicast address, a name other than localhost, or no host at all is not
this PC (none of them is a place a program can be).

This PC's addresses are read with the standard library only, never more
than once every CACHE_S, and never for longer than GATHER_TIMEOUT_S:

  - on Windows, every adapter's IPv4 addresses (GetAdaptersAddresses, the
    same list ipconfig prints, loopback adapters included), through ctypes;
  - everywhere, the addresses this PC's own name resolves to, and the
    address this PC would send from (a UDP socket connected to a
    documentation address: connecting sends nothing).

Never raises. When the list cannot be read at all, only the loopback rule
above is used (the old answer), and addresses() says so with None.

Reads only. Nothing here sends a packet, opens a listener or changes any
output.
"""
import ipaddress
import socket
import sys
import threading
import time

CACHE_S = 30.0           # this PC's addresses are read at most this often
GATHER_TIMEOUT_S = 2.0   # and a read that takes longer counts as failed
# A documentation address (RFC 5737): connecting a UDP socket to it picks
# the address this PC would send from, and sends nothing.
_ROUTE_PROBE = ("192.0.2.1", 9)

_lock = threading.Lock()
_cache = None            # (frozenset of addresses or None, clock() of read)
_clock = time.monotonic


def _ipv4(text):
    """The IPv4Address of `text`, or None."""
    try:
        return ipaddress.IPv4Address(str(text).strip())
    except (ipaddress.AddressValueError, ValueError):
        return None


def _usable(a):
    """An address a program can be on: not 0.0.0.0, multicast or
    broadcast."""
    return a is not None and not (a.is_unspecified or a.is_multicast or
                                  str(a) == "255.255.255.255")


def _windows_adapters():
    """Every adapter's IPv4 addresses on Windows (GetAdaptersAddresses),
    loopback adapters included, as a set of strings. Raises on failure
    (gather() catches it)."""
    import ctypes
    from ctypes import wintypes

    class SOCKADDR(ctypes.Structure):
        _fields_ = [("family", ctypes.c_ushort),
                    ("data", ctypes.c_ubyte * 14)]

    class SOCKET_ADDRESS(ctypes.Structure):
        _fields_ = [("lpSockaddr", ctypes.POINTER(SOCKADDR)),
                    ("iSockaddrLength", ctypes.c_int)]

    class UNICAST(ctypes.Structure):
        pass
    UNICAST._fields_ = [("Length", wintypes.ULONG),
                        ("Flags", wintypes.DWORD),
                        ("Next", ctypes.POINTER(UNICAST)),
                        ("Address", SOCKET_ADDRESS)]

    class ADAPTER(ctypes.Structure):
        pass
    ADAPTER._fields_ = [("Length", wintypes.ULONG),
                        ("IfIndex", wintypes.DWORD),
                        ("Next", ctypes.POINTER(ADAPTER)),
                        ("AdapterName", ctypes.c_char_p),
                        ("FirstUnicastAddress", ctypes.POINTER(UNICAST))]

    AF_INET = 2
    # Skip anycast, multicast and DNS server addresses: unicast only.
    FLAGS = 0x0002 | 0x0004 | 0x0008
    ERROR_BUFFER_OVERFLOW = 111
    fn = ctypes.windll.iphlpapi.GetAdaptersAddresses
    size = wintypes.ULONG(16384)
    for _ in range(3):
        buf = ctypes.create_string_buffer(size.value)
        rc = fn(AF_INET, FLAGS, None, buf, ctypes.byref(size))
        if rc == ERROR_BUFFER_OVERFLOW:
            continue
        if rc != 0:
            raise OSError(f"GetAdaptersAddresses returned {rc}")
        break
    else:
        raise OSError("GetAdaptersAddresses kept asking for more room")
    out = set()
    p = ctypes.cast(buf, ctypes.POINTER(ADAPTER))
    while p:
        u = p.contents.FirstUnicastAddress
        while u:
            sa = u.contents.Address.lpSockaddr
            if sa and sa.contents.family == AF_INET:
                # sockaddr_in: 2 bytes of port, then the 4 address bytes.
                out.add(".".join(str(b) for b in sa.contents.data[2:6]))
            u = u.contents.Next
        p = p.contents.Next
    return out


def _by_name():
    """The IPv4 addresses this PC's own name resolves to."""
    out = set()
    for fam, _t, _p, _c, sa in socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET):
        if fam == socket.AF_INET and sa:
            out.add(sa[0])
    return out


def _routed():
    """The address this PC would send from. Connecting a UDP socket sends
    nothing."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(_ROUTE_PROBE)
        return {s.getsockname()[0]}
    finally:
        s.close()


def gather(windows=None):
    """This PC's own IPv4 addresses, as a set of strings, or None when not
    one source could be read. Each source is tried on its own: one that
    fails leaves the others. Never raises."""
    if windows is None:
        windows = sys.platform == "win32"
    sources = [_by_name, _routed]
    if windows:
        sources.insert(0, _windows_adapters)
    out, read = set(), False
    for src in sources:
        try:
            got = src()
        except Exception:                        # noqa: BLE001
            continue
        read = True
        for t in got or ():
            a = _ipv4(t)
            if _usable(a):
                out.add(str(a))
    return out if read else None


def _gather_bounded():
    """gather(), on its own thread, given GATHER_TIMEOUT_S: a name lookup
    that hangs never holds the caller. None when it failed or took too
    long. Never raises."""
    box = {}

    def run():
        try:
            box["v"] = gather()
        except Exception:                        # noqa: BLE001
            box["v"] = None
    try:
        t = threading.Thread(target=run, name="ltcplay-thispc", daemon=True)
        t.start()
        t.join(GATHER_TIMEOUT_S)
    except Exception:                            # noqa: BLE001
        return None
    return box.get("v")


def addresses():
    """This PC's own addresses as a sorted tuple of strings (loopback ones
    included when found), read at most once every CACHE_S; None when they
    could not be read. Never raises."""
    global _cache
    try:
        now = _clock()
        with _lock:
            c = _cache
        if c is not None and 0 <= now - c[1] < CACHE_S:
            return c[0]
        got = _gather_bounded()
        got = tuple(sorted(got, key=lambda s: ipaddress.IPv4Address(s))) \
            if got is not None else None
        with _lock:
            _cache = (got, now)
        return got
    except Exception:                            # noqa: BLE001
        return None


def forget():
    """Drop the cached list, so the next answer reads it again (the
    selftest)."""
    global _cache
    with _lock:
        _cache = None


def on_this_pc(host):
    """True when `host` is this PC: "localhost", a 127.x.x.x loopback
    address, or one of this PC's own interface addresses (addresses()).
    False for anything else, including no host, a name, 0.0.0.0, broadcast
    and multicast, and for every address that is not loopback when this
    PC's addresses cannot be read. Never raises."""
    try:
        h = str(host or "").strip().lower()
        if h == "localhost":
            return True
        a = _ipv4(h)
        if not _usable(a):
            return False
        if a.is_loopback:
            return True
        mine = addresses()
        return bool(mine) and str(a) in mine
    except Exception:                            # noqa: BLE001
        return False


def listed():
    """This PC's addresses in words, for a sentence that says where a
    program is set up (how to see which address ltcplay uses, and what
    counts as this PC). Never raises."""
    try:
        mine = addresses()
    except Exception:                            # noqa: BLE001
        mine = None
    if not mine:
        return ("this PC's own addresses could not be read, so only "
                "127.x.x.x and localhost count as this PC")
    return "this PC has " + ", ".join(mine)


def not_mine():
    """The words "not an address of this PC (this PC has ...)", for a
    sentence about a program whose OSC host is not this PC. Never
    raises."""
    return f"not an address of this PC ({listed()})"
