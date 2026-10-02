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

Build step 7b (2026-10-01) adds a third frame, `"t": "arm"`, from the
Stream Deck (part of ltcplay's own process, see ltcplay/streamdeck.py) to
flamesafe's `link.arm_port`.  It carries the same key and a strict shape
(decode_arm), but no sender lock: there is exactly one Stream Deck, the key
already keeps out anything that has not read flamesafe's config, and the
composer's own consent and liveness rules (arminput.py, rules.py) are what
actually decide whether a group arms, not this module.  (The arm link
gained a sender lock later the same day; see arminput.py and CONTRACT.md.)

2026-10-02 adds `"t": "disarm_all"` on the flame link (decode_disarm_all,
routed by decode_from_ltcplay): the show program's Abort.  It can only
ever clear arm state; composer.disarm_all applies it, sender-locked and in
sequence with the flame frames.
"""

from __future__ import annotations

import json
import re

from . import rules

CONTRACT_VERSION = 2
MAX_DATAGRAM = 16384
KEY_MIN, KEY_MAX = 16, 128
# The key in flamesafe.example.json.  Fine on a bench; a config marked
# confirmed must not still carry it, because everyone who has read the
# repo knows it.
EXAMPLE_KEY = "fire-and-ice-2026-replace-this-key"
# Used with fullmatch, ASCII digits only (fix round 1 of PR #34, item 9):
# "$" also matched before a trailing newline, and \d takes any Unicode digit.
_TC = re.compile(r"[0-9]{2}:[0-9]{2}:[0-9]{2}[:;][0-9]{2}")
_KEY = re.compile(r"[\x21-\x7e]+")       # printable ASCII, no spaces


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
            and bool(_KEY.fullmatch(key)))


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
    if tc is not None and not (isinstance(tc, str) and _TC.fullmatch(tc)):
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


class DisarmAll:
    """One decoded disarm_all message from ltcplay (CONTRACT.md, "Disarm
    every group: the show program's Abort", added 2026-10-02).  It can only
    ever take arm AWAY: composer.disarm_all clears every latch and every
    pending consent edge, and has no path that sets one."""
    __slots__ = ("seq", "mono", "abort_id", "reason")

    def __init__(self, seq, mono, abort_id, reason):
        self.seq = seq
        self.mono = mono
        self.abort_id = abort_id
        self.reason = reason


# Exactly these fields, no more: a disarm_all with anything extra is not a
# message this contract describes, and is refused rather than guessed at.
DISARM_ALL_FIELDS = frozenset(("v", "k", "t", "seq", "mono", "id", "reason"))
REASON_MAX = 200


def decode_disarm_all(data, key):
    """Bytes off the wire to a DisarmAll, or LinkError with the reason.
    Same order of checks as decode_flame: size, JSON, object, version, key
    (before anything else is looked at), type, then every field."""
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
    if obj.get("t") != "disarm_all":
        raise LinkError(f"wrong message type {obj.get('t')!r}")
    extra = sorted(str(f) for f in obj if f not in DISARM_ALL_FIELDS)
    if extra:
        raise LinkError("disarm_all has a field this contract does not "
                        "describe")
    seq = obj.get("seq")
    if not _is_int(seq) or seq < 0:
        raise LinkError("seq is not a whole number at or above 0")
    mono = obj.get("mono")
    if isinstance(mono, bool) or not isinstance(mono, (int, float)) \
            or mono != mono or mono in (float("inf"), float("-inf")):
        raise LinkError("mono is not a finite number")
    abort_id = obj.get("id")
    if not _is_int(abort_id) or abort_id < 1:
        raise LinkError("id is not a whole number at or above 1")
    reason = obj.get("reason")
    if not isinstance(reason, str) or not reason.strip() \
            or len(reason) > REASON_MAX:
        raise LinkError(f"reason is not 1 to {REASON_MAX} characters")
    return DisarmAll(seq, float(mono), abort_id, reason)


def encode_disarm_all(seq, mono, abort_id, reason, key):
    """A disarm_all as ltcplay sends it.  Used by the tests only; ltcplay
    writes its own encoder from CONTRACT.md (ltcplay/flamelink.py)."""
    return json.dumps({
        "v": CONTRACT_VERSION, "k": key, "t": "disarm_all", "seq": int(seq),
        "mono": float(mono), "id": int(abort_id), "reason": str(reason),
    }, separators=(",", ":")).encode("utf-8")


def decode_from_ltcplay(data, expect_universe, key):
    """Anything that arrives on the flame link (`link.listen_port`): a
    FlameFrame or a DisarmAll, or LinkError with the reason.  Only a
    datagram that is a JSON object saying `"t": "disarm_all"` goes to the
    disarm decoder; everything else, including garbage, goes to
    decode_flame exactly as before this message existed, so every flame
    frame rule and every rejection reason is unchanged."""
    try:
        obj = json.loads(bytes(data).decode("utf-8")) \
            if isinstance(data, (bytes, bytearray)) \
            and len(data) <= MAX_DATAGRAM else None
    except (UnicodeDecodeError, ValueError):
        obj = None
    if isinstance(obj, dict) and obj.get("t") == "disarm_all":
        return decode_disarm_all(data, key)
    return decode_flame(data, expect_universe, key)


def decode_arm(data, expect_n, key):
    """Bytes off the wire to (wanted, seq, names), or LinkError with the
    reason.  One arm frame from the Stream Deck (build step 7b): it says
    only what the deck WANTS right now, continuously, exactly like
    arminput.ArmAssertion.  Every consent, dwell, chatter and edge rule
    still runs in the composer afterwards, unchanged; this function only
    gets a well-formed assertion onto arminput's interface.

    `names` is required on this frame, unlike arminput.ArmAssertion where
    it is optional: a deck that does not say which groups it means must
    never be trusted to mean the right ones."""
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
    if obj.get("t") != "arm":
        raise LinkError(f"wrong message type {obj.get('t')!r}")
    seq = obj.get("seq")
    if not _is_int(seq) or seq < 0:
        raise LinkError("seq is not a whole number at or above 0")
    wanted = obj.get("wanted")
    if not isinstance(wanted, list) or len(wanted) != expect_n \
            or any(not isinstance(w, bool) for w in wanted):
        raise LinkError(f"wanted is not a list of exactly {expect_n} "
                        f"true/false values")
    names = obj.get("names")
    if not isinstance(names, list) or len(names) != expect_n \
            or any(not isinstance(n, str) for n in names):
        raise LinkError(f"names is not a list of exactly {expect_n} group "
                        f"names")
    return tuple(wanted), seq, tuple(names)


def encode_arm(seq, wanted, names, key):
    """An arm frame as the Stream Deck driver sends it.  Used by the tests
    and by the test driver only; the real driver (ltcplay/streamdeck.py)
    writes its own encoder from CONTRACT.md, the same way ltcplay's flame
    frames are written -- this module is never imported across the wall."""
    return json.dumps({
        "v": CONTRACT_VERSION, "k": key, "t": "arm", "seq": int(seq),
        "wanted": [bool(w) for w in wanted],
        "names": [str(n) for n in names],
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
