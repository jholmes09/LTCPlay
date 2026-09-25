"""flamesafe link: the localhost message format shared with ltcplay.

This is the ONLY thing the two programs share, and they share it as a
document (CONTRACT.md), not as code: ltcplay never imports this module.
Every message is one UDP datagram carrying one JSON object.  Decoding is
strict: anything not exactly as the contract says is rejected with a reason,
and a rejected datagram changes nothing.

Contract version 2 adds the shared key.  Any local process can write a
datagram to a loopback port, and in version 1 a rogue frame with a large
sequence number was accepted and then locked the real ltcplay out until the
link went stale.  Now every flame frame and every status frame carries
`k`, the value of `link.key` from flamesafe's config, and a frame without
the right key is rejected before anything else is looked at.  ltcplay reads
the same value from its own config and must reject status frames without
it.  The composer adds a second guard, the sender lock: while the link is
live, only the first accepted sender's address is accepted.
"""

from __future__ import annotations

import json
import re

from . import rules

CONTRACT_VERSION = 2
MAX_DATAGRAM = 16384
KEY_MIN, KEY_MAX = 16, 128
_TC = re.compile(r"^\d{2}:\d{2}:\d{2}[:;]\d{2}$")
_KEY = re.compile(r"^[\x21-\x7e]+$")     # printable ASCII, no spaces


class LinkError(ValueError):
    """A datagram that is not a well-formed contract message."""


class FlameFrame:
    """One decoded flame frame from ltcplay."""
    __slots__ = ("seq", "timecode", "mono", "universe", "values")

    def __init__(self, seq, timecode, mono, universe, values):
        self.seq = seq
        self.timecode = timecode
        self.mono = mono
        self.universe = universe
        self.values = values


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def valid_key(key):
    return (isinstance(key, str) and KEY_MIN <= len(key) <= KEY_MAX
            and bool(_KEY.match(key)))


def decode_flame(data, expect_universe, key):
    """Bytes off the wire to a FlameFrame, or LinkError with the reason."""
    if not isinstance(data, (bytes, bytearray)):
        raise LinkError("not bytes")
    if len(data) > MAX_DATAGRAM:
        raise LinkError(f"datagram too long: {len(data)} bytes")
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise LinkError("not valid JSON") from None
    if not isinstance(obj, dict):
        raise LinkError("not a JSON object")
    if obj.get("v") != CONTRACT_VERSION:
        raise LinkError(f"wrong contract version {obj.get('v')!r}, "
                        f"this program speaks {CONTRACT_VERSION}")
    if not isinstance(obj.get("k"), str) or obj.get("k") != key:
        raise LinkError("wrong key")
    if obj.get("t") != "flame":
        raise LinkError(f"wrong message type {obj.get('t')!r}")
    seq = obj.get("seq")
    if not _is_int(seq) or seq < 0:
        raise LinkError("seq is not a whole number at or above 0")
    tc = obj.get("tc")
    if tc is not None and not (isinstance(tc, str) and _TC.match(tc)):
        raise LinkError("tc is not HH:MM:SS:FF or null")
    mono = obj.get("mono")
    if isinstance(mono, bool) or not isinstance(mono, (int, float)) \
            or mono != mono or mono in (float("inf"), float("-inf")):
        raise LinkError("mono is not a finite number")
    universe = obj.get("universe")
    if not _is_int(universe):
        raise LinkError("universe is not a whole number")
    if universe != expect_universe:
        raise LinkError(f"universe {universe} is not the flame universe "
                        f"{expect_universe}")
    values = obj.get("values")
    if not isinstance(values, list) or len(values) != rules.UNIVERSE_SIZE:
        raise LinkError(f"values is not a list of exactly "
                        f"{rules.UNIVERSE_SIZE} numbers")
    for v in values:
        if not _is_int(v) or not (0 <= v <= 255):
            raise LinkError("a channel value is not a whole number 0 to 255")
    return FlameFrame(seq, tc, float(mono), universe, bytes(values))


def encode_flame(seq, timecode, mono, universe, values, key):
    """A flame frame as ltcplay would send it.  Used by the tests and by
    the test driver only; ltcplay writes its own encoder from CONTRACT.md."""
    return json.dumps({
        "v": CONTRACT_VERSION, "k": key, "t": "flame", "seq": int(seq),
        "tc": timecode, "mono": float(mono), "universe": int(universe),
        "values": [int(v) for v in values],
    }, separators=(",", ":")).encode("utf-8")


def encode_status(status, key):
    """The status frame, as bytes for the wire, carrying the key."""
    status = dict(status)
    status["k"] = key
    return json.dumps(status, separators=(",", ":")).encode("utf-8")


def decode_status(data, key):
    """A status frame off the wire, for the tests.  ltcplay writes its own,
    and it must reject a status frame whose key is not its own."""
    obj = json.loads(bytes(data).decode("utf-8"))
    if not isinstance(obj, dict) or obj.get("v") != CONTRACT_VERSION \
            or obj.get("t") != "status":
        raise LinkError("not a status frame")
    if obj.get("k") != key:
        raise LinkError("status frame without the key")
    return obj
