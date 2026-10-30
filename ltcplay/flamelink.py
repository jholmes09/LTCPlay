"""The flame link, ltcplay's side: flame cue frames to flamesafe, and the
show's Abort as a disarm_all.

Imported ONLY by code that runs the Fire & Ice show. The GPL show at
Dollywood never reaches it (selftest: test_the_gpl_path_never_loads_the_
flame_link). Nothing in this build constructs a FlameLink outside the
tests: a coordinator wires it into fire_ice.FireIceShow after review.

Written from flamesafe/CONTRACT.md, never from flamesafe's code: this module
never imports flamesafe (the wall, test_the_wall_between_ltcplay_and_
flamesafe), exactly as ltcplay/streamdeck.py writes its own arm frames.

What it sends
=============

One flame frame on ONE socket for its whole life (the contract's sender
lock is on (ip, port), so a new socket per frame would be "another sender"
every time), to flamesafe's `link.listen_port` on loopback, at `send_hz`
(default 40, never below the contract's 20 Hz floor), whatever the show is
doing. flamesafe reads silence as failure: after `fire_hold_ms` every fire
slot is zero, after `frame_stale_ms` every group disarms and needs a fresh
cycle. So this keeps sending zeros when there is nothing to fire.

The values in each frame are ALL ZEROS unless every one of these holds:

  1. the show conductor has released the flame cues (release(), its
     flames_release) and not zeroed them since (zero(), flames_zero, also
     done by disarm_all). A FlameLink starts zeroed: nothing is released
     until the conductor says a show is running;
  2. `show_state()` says a show cue is playing and its timecode is moving
     (not stopped, not held or fading into a hold, not fading out on an
     Abort). No show, intermission (ltcplay sends no show timecode between
     shows), Hold and Abort are all zeros by this rule alone, even if the
     conductor's own zero() never arrived. "Moving" is checked here, not
     trusted: a timecode that has not changed for TC_STILL_S (0.1 s) is
     zeros, journaled once per episode (fix round 1 of PR #34, item 5: a
     stalled clock thread kept its last frame and a cue at that frame went
     out for as long as it stayed stalled). Design note (fix round 2): it
     means CHANGED, not ADVANCING. A source stuck bouncing between two
     frames, or stepping backwards, still reads as moving here (review
     probe p9b); this rule catches a clock that has stopped, not one that
     is wrong. Telling a wrong clock from a seek is the clock's job;

  3. the cue provider answered with exactly 512 whole numbers 0 to 255 for
     that timecode, without raising. Anything else is zeros for that frame,
     journaled once per episode;

  4. the seek guard (2026-10-03, programming sessions scrub and loop): the
     timecode has run steadily for SEEK_SETTLE_S (0.5 s) since the last
     seek, jump, loop wrap or resume, and, channel by channel, that channel
     has been seen at zero since the last seek. A timecode that moves by
     more than SEEK_JUMP_S more or less than the time that passed is a
     seek; the first timecode ever seen counts as one. So a scrub never
     fires anything on the way, and a flame cue whose start was jumped
     over never fires at all: it stays zero until it has ended. Every
     Programming transport move (landing(), playhead.py) counts as a seek
     too, however small, so a jump shorter than SEEK_JUMP_S is one.

THE CUE PROVIDER IS A STUB IN THIS BUILD. `cues(tc)` is "the flame universe
for timecode tc": a sequence of 512 ints, or None for all zeros. Handoff
section 7 says the xLights FSEQ carries the flame channels and ltcplay
extracts that universe from the sequence, removes it from its own pixel
output map, and refuses to start if it is still mapped. That extraction is
build step 7's other half and is not in this module: which FSEQ channels
are the flame universe depends on the flame universe number and group map
Andy has not confirmed yet. Until it exists the only provider is
zero_cues, which always answers None, so every frame this module sends is
all zeros. See the PR's open questions.

The show's Abort: disarm_all
============================

disarm_all(reason) zeroes the cues, sends one all-zero flame frame at once,
then DISARM_COPIES copies of one disarm_all message (one abort id, each its
own seq) on the same socket, and then one more copy of that same abort id
after every flame frame for abort_repeat_s (at least ABORT_REPEAT_MIN_S,
0.75 s, and always longer than flamesafe's frame_stale_ms). Fix round 1 of
PR #34, item 1: a 0.3 s burst of junk on the port lost all three
back-to-back copies in 3 runs out of 3 and every group stayed armed.
Repeating past frame_stale_ms means that losing every copy also loses
enough flame frames for flamesafe to call the link lost, which disarms
every group anyway. flamesafe accepts it only from the live, locked
flame-link sender with the right key; it clears every group's latch and
every pending consent edge, so each group needs a fresh, genuine arm cycle
from the Stream Deck afterwards. It can never arm anything.

Abort ids and seq start at a random large number per FlameLink (fix round
1, items 3 and 4): a restarted ltcplay never reuses the last run's ids, so
flamesafe's `last_id` from an earlier run cannot confirm a new Abort, and a
rogue sender counting up from 1 is never mistaken for this program.

zero() and disarm_all() never call the show state or cue providers (fix
round 1, item 7): their zero frame is built from nothing but zeros, and the
sender thread asks the providers outside the link's lock, so a provider
that hangs cannot delay an Abort.

Why a new message on THIS link, and not "all false" on the Stream Deck's
arm link: the arm link is locked to the deck's own socket, and a frame from
any other sender is journaled as a rogue and raises the deck's spoof alarm
(arm_input.foreign_senders). ltcplay sending "all false" there would trip
that alarm on every screen Abort, training the operator to ignore the one
alarm that means someone else is on the arm link. It would also be a
second voice saying what is WANTED, which only the operator's deck may say.
The flame link is the one ltcplay already holds, keyed and sender-locked;
"stop, everything" belongs on it.

Failing loud
============

A send that fails writes one journal line when the outage starts and one
when it ends (with how long and how many frames), never one per frame, the
same as streamdeck.ArmSocket. A provider that raises or answers garbage:
one line per episode. An exception anywhere in the sender thread's loop:
one line when it starts and one when it clears, and the loop carries on;
snapshot() says the sender is failing, or dead if the thread ever stops
without stop() (fix round 1, item 6), or stalled if no frame has gone out
for more than half of frame_stale_ms without anything raising (a provider
or the journal blocking; fix round 2, item 2), journaled once per episode. A key still equal to the repo's
example key: a fault line at open. note_status() takes flamesafe's status
frames (from whoever reads `link.status_port`; see the PR) and raises the
contract's lock alarm when the last accepted frame is not one of ours for
more than 1 s, journals flamesafe's confirmation of an Abort (its
`disarm_all.last_id` equal to the id sent), and a fault when one is not
confirmed within 1 s. Until a status frame confirms it, an Abort is
journaled and shown as "sent, not confirmed by flamesafe", never as done.
"""
import collections
import json
import math
import re
import secrets
import socket
import sys
import threading
import time

CONTRACT_VERSION = 2
UNIVERSE_SIZE = 512
SEND_HZ_DEFAULT = 40
SEND_HZ_MIN = 20            # CONTRACT.md: "20 Hz or faster, idle included"
SEND_HZ_MAX = 100
DISARM_COPIES = 3           # one Abort, a few datagrams at once...
ABORT_REPEAT_MIN_S = 0.75   # ...then one per frame for at least this long
ABORT_REPEAT_MARGIN_S = 0.25  # and always this much past frame_stale_ms
STALE_MS_MIN, STALE_MS_MAX = 100, 2500   # flamesafe's own bounds
# Fix round 2, item 1: parse() REQUIRES flamesafe's frame_stale_ms (a
# flame_link block without it is refused). Only a FlameLinkConfig built
# directly in code falls back to this, the largest value flamesafe accepts,
# so a missing copy can only make the Abort repeat longer, never too short.
FRAME_STALE_MS_DEFAULT = STALE_MS_MAX
TC_STILL_S = 0.1            # a timecode unchanged this long is not moving
# After an operator Hold the show reads as playing again before its timecode
# moves: the audio fades back in and the clock holds the frozen frame until
# the sound reaches the next one (by design: the timecode never runs
# backwards). A stop on that same frame within this long of the Resume is
# journaled as a note, not a fault (Jeff, 2026-10-07). The cues are zeros
# exactly as for any other stop; only the journal line's class differs.
RESUME_SETTLE_S = 1.0
# The seek guard (2026-10-03, Jeff: programming sessions scrub and loop).
# A timecode that moves by more than SEEK_JUMP_S more or less than the
# time that passed is a seek (a GO, a skip, a loop wrap, a locate on the
# timecode feed). From a seek on, every flame value is zero until the
# timecode has run steadily for SEEK_SETTLE_S, and each channel stays zero
# after that until it has been seen at zero since the seek: a cue whose
# start was jumped over never fires. The first timecode this link ever
# sees counts as a seek too (a GO into the middle of a show is a landing).
SEEK_JUMP_S = 0.25
SEEK_SETTLE_S = 0.5
TC_FPS_DEFAULT = 30.0
# The purge frame (Jeff, 2026-10-17, the Morning and Shutdown page): a 512
# channel frame the engine re-asserts with purge_set() every 100 ms while
# Space or the deck's FIRE key is held. It is sent ONLY while the show is
# not live, and only for PURGE_HOLD_S after the last purge_set, on this
# link's own clock, so a page that dies with the key down, a heartbeat
# gap, a connection drop or an engine thread that stops all read as "let
# go" here, whatever the web layer does. zero() and disarm_all() clear it.
PURGE_HOLD_S = 0.25
# A timed purge (Jeff, 2026-10-29: "a designated time length purge"): the
# engine also gives the link the purge's hard end (purge_limit), on this
# link's own clock. From that moment the purge frame is zeros on every
# frame and purge_set() is refused until purge_clear(), whatever keeps
# calling it: the link's own deadline, checked on every frame it sends.
# Live flame programming (flameprog.py, LIVE_FLAME_PROGRAMMING.md): fire
# values xLights sends on loopback while the mode is on, re-set by the
# listener on every accepted packet. Like the purge frame they go out ONLY
# while the show is not live, and only for PROGRAM_HOLD_S after the last
# program_set on this link's own clock, so xLights stopping, the listener
# dying or the engine hanging all read as zeros here. They are taken only
# between program_begin() and program_end(), and only with the session
# number program_begin() gave (an old listener can never set the next
# session's frame). zero() and disarm_all() end them too: the conductor's
# Hold, Abort and every other zero stop xLights at the link at once, like
# fire. While the mode is on the show's own cues are never sent (one
# source at a time).
PROGRAM_HOLD_S = 0.25
# The Fireball page's own frame (fireball.py; Jeff, 2026-10-30: "Andy may
# manually start any phase with toggle buttons at any time, including
# during a show"). The page re-asserts it with manual_set() every 100 ms
# while a button is on. It carries values ONLY on the slots manual_slots
# names (the fireball group's fire slots, from flamesafe's config; empty
# until the engine sets them, so nothing passes before then), and only for
# MANUAL_HOLD_S after the last manual_set on this link's own clock, like
# the purge frame. Unlike the purge frame it is read whether or not a show
# is live: on those slots each frame carries the HIGHER of the show's own
# value (the FSEQ or the fireball timeline) and the page's, channel by
# channel, so neither source can turn the other's channel off. zero() and
# disarm_all() (Hold, Stop, Abort, a show's end) drop it at once, the
# lockout zeroes its slots, and flamesafe still passes them only while the
# fireball group is ARMED.
MANUAL_HOLD_S = 0.25
# Programming playback (playhead.py, Jeff 2026-10-29: the switch "Fire
# flames while scrubbing" is gone). The show's own flame cues follow the
# Programming screen's transport through this link's show path, exactly as
# in a show. Every transport move (Play from, Back, Ahead, Continue, the
# loop) is a landing however small (landing(), called before and after the
# move): the seek guard starts again, so nothing fires for SEEK_SETTLE_S
# and a channel already up where it lands stays zero until it has been
# seen at zero. The landing lasts until the show timecode differs from the
# one the clock had sent when landing() was called: the show audio keeps
# its last frame while it starts on the new sample, and that old frame
# must never clear a channel. A frame read across a landing goes out as
# zeros (_land_gen).
# The hazers (Jeff, 2026-10-29; flamesafe/CONTRACT.md, "Passthrough: the
# hazers"): flamesafe's "passthrough" entries, read from its own config,
# each a haze and a fan channel. Their values never come from the show's
# FSEQ, xLights or the Programming transport: every hazer channel is zero
# in whatever those carry, and this link writes the hazer's stored Haze and
# Fan levels (hazer_levels_set) on its two channels only while it is ARMED
# here (hazers_set) and a show is playing (the show state live AND the
# engine's haze_gate: a scheduled show in SHOW, not on Hold). Every frame
# carries "hazers_on", the names ARMED. DISARMED at start; disarm_all
# (Abort, Stop) disarms every one before its first copy goes out.
HAZERS_MAX = 16
HAZER_NAME_MAX = 64
LOCK_ALARM_S = 1.0         # CONTRACT.md: "for more than 1 s"
CONFIRM_S = 1.0             # a disarm_all not confirmed by then is a fault
KEY_MIN, KEY_MAX = 16, 128
REASON_MAX = 200
# seq and abort ids start at a random number in this range per FlameLink
# (fix round 1 of PR #34, items 3 and 4). Under 2**53, so a JSON reader in
# any language reads it exactly.
RANDOM_START_MIN, RANDOM_START_SPAN = 10 ** 6, 2 ** 40
# The key in flamesafe/flamesafe.example.json. Fine on a bench; a show
# config must carry its own (CONTRACT.md). Written here as a string to
# compare against, never used as a default.
EXAMPLE_KEY = "fire-and-ice-2026-replace-this-key"
CONFIG_KEYS = frozenset(("ip", "port", "universe", "key", "send_hz",
                         "frame_stale_ms"))
# Used with fullmatch, ASCII digits only (fix round 1 of PR #34, item 9):
# "$" also matched before a trailing newline, and \d takes any Unicode digit.
_KEY = re.compile(r"[\x21-\x7e]+")
_TC = re.compile(r"[0-9]{2}:[0-9]{2}:[0-9]{2}[:;][0-9]{2}")
SIO_UDP_CONNRESET = 0x9800000C


class FlameLinkConfigError(ValueError):
    """The flame link settings are wrong, in a sentence."""


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def valid_key(key):
    return (isinstance(key, str) and KEY_MIN <= len(key) <= KEY_MAX
            and bool(_KEY.fullmatch(key)))


class FlameLinkConfig:
    """Where flamesafe listens, its flame universe, the shared key, and the
    send rate. The key is read from a config file at run time, never
    written in this repo (CLAUDE.md: never commit keys)."""

    def __init__(self, ip, port, universe, key, send_hz=SEND_HZ_DEFAULT,
                 frame_stale_ms=FRAME_STALE_MS_DEFAULT, aim_slots=(),
                 hazers=(), fire_hold_ms=None):
        self.ip = ip
        self.port = port
        self.universe = universe
        self.key = key
        self.send_hz = send_hz
        self.frame_stale_ms = frame_stale_ms
        # The flame universe slots (1 to 512) that only aim a head, from
        # flamesafe's groups' "aim" lists. The zero-first hold skips them.
        self.aim_slots = frozenset(aim_slots)
        # The hazers, ((name, (slot, ...)), ...), slots 1 to 512, from
        # flamesafe's "passthrough" (hazers_of). Empty: none.
        self.hazers = tuple((str(n), tuple(int(x) for x in sl))
                            for n, sl in hazers)
        # flamesafe's fire_hold_ms (2026-10-29), or None: only the frame
        # timing's limit (FrameTiming); nothing sent depends on it.
        self.fire_hold_ms = fire_hold_ms

    @property
    def abort_repeat_s(self):
        """How long one Abort is repeated, once per frame: past flamesafe's
        frame_stale_ms, so losing every copy means losing the link too."""
        return max(ABORT_REPEAT_MIN_S,
                   self.frame_stale_ms / 1000.0 + ABORT_REPEAT_MARGIN_S)

    @classmethod
    def parse(cls, doc, where="flame_link"):
        """A "flame_link" block: {"ip": "127.0.0.1", "port": 5571,
        "universe": 1, "key": "<flamesafe's link.key>", "send_hz": 40,
        "frame_stale_ms": 500}. `ip` and `send_hz` may be left out.
        `frame_stale_ms` may not (fix round 2, item 1): it must be
        flamesafe's own, because it decides how long an Abort is repeated,
        and a copy that defaulted to 500 while flamesafe ran 2500 left a
        1 s junk burst able to lose every copy with the link still up
        (review probe p11). Prefer from_flamesafe_config, which reads it
        from the one file flamesafe itself reads."""
        if not isinstance(doc, dict):
            raise FlameLinkConfigError(f"{where}: it has to be one JSON "
                                       f"object.")
        unknown = sorted(str(k) for k in doc if k not in CONFIG_KEYS)
        if unknown:
            raise FlameLinkConfigError(
                f"{where}: {', '.join(repr(k) for k in unknown)} is not a "
                f"setting the flame link has. It takes: "
                f"{', '.join(sorted(CONFIG_KEYS))}.")
        ip = doc.get("ip", "127.0.0.1")
        if not isinstance(ip, str) or not ip.startswith("127.") or \
                not _ipv4(ip):
            raise FlameLinkConfigError(
                f"{where}: 'ip' has to be a loopback address (127.x.x.x); "
                f"the flame link never leaves this machine.")
        port = doc.get("port")
        if not _is_int(port) or not 1 <= port <= 65535:
            raise FlameLinkConfigError(f"{where}: 'port' has to be "
                                       f"flamesafe's link.listen_port, a "
                                       f"whole number 1 to 65535.")
        universe = doc.get("universe")
        if not _is_int(universe) or not 1 <= universe <= 63999:
            raise FlameLinkConfigError(f"{where}: 'universe' has to be "
                                       f"flamesafe's flame universe.")
        key = doc.get("key")
        if not valid_key(key):
            raise FlameLinkConfigError(
                f"{where}: 'key' has to be flamesafe's link.key, {KEY_MIN} "
                f"to {KEY_MAX} printable characters without spaces.")
        hz = doc.get("send_hz", SEND_HZ_DEFAULT)
        if not _is_int(hz) or not SEND_HZ_MIN <= hz <= SEND_HZ_MAX:
            raise FlameLinkConfigError(
                f"{where}: 'send_hz' has to be a whole number "
                f"{SEND_HZ_MIN} to {SEND_HZ_MAX}; flamesafe disarms a "
                f"sender slower than {SEND_HZ_MIN} Hz.")
        stale = doc.get("frame_stale_ms")
        if not _is_int(stale) or not STALE_MS_MIN <= stale <= STALE_MS_MAX:
            raise FlameLinkConfigError(
                f"{where}: 'frame_stale_ms' has to be flamesafe's own "
                f"frame_stale_ms, a whole number {STALE_MS_MIN} to "
                f"{STALE_MS_MAX}.")
        return cls(ip, port, universe, key, hz, stale)

    @classmethod
    def from_flamesafe_config(cls, path, send_hz=SEND_HZ_DEFAULT):
        """The same settings read from flamesafe's own config file, as
        plain JSON (never flamesafe.config: the wall), the way
        streamdeck.load_flamesafe_link reads its arm link. One file holds
        the key, so the two programs cannot disagree about it."""
        try:
            with open(path, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            raise FlameLinkConfigError(f"{path}: could not be read: {e}")
        link = doc.get("link") if isinstance(doc, dict) else None
        if not isinstance(link, dict):
            raise FlameLinkConfigError(f"{path}: has no link block.")
        cfg = cls.parse({"ip": link.get("listen_ip", "127.0.0.1"),
                         "port": link.get("listen_port"),
                         "universe": doc.get("universe"),
                         "key": link.get("key"), "send_hz": send_hz,
                         "frame_stale_ms": doc.get("frame_stale_ms")},
                        where=str(path))
        cfg.aim_slots = aim_slots_of(doc, path)
        cfg.hazers = hazers_of(doc, path)
        hold = doc.get("fire_hold_ms")
        cfg.fire_hold_ms = (hold if _is_int(hold)
                            and 1 <= hold < cfg.frame_stale_ms else None)
        return cfg


def hazers_of(doc, path="flamesafe's config"):
    """flamesafe's "passthrough" list (2026-10-29, the hazers) as ((name,
    (haze slot, fan slot)), ...), read as plain JSON (the wall). Left out:
    none. A malformed list is refused, never skipped, so `ltc serve` does
    not start on a file flamesafe would refuse: a list of at most
    HAZERS_MAX objects, each a name of 1 to HAZER_NAME_MAX characters used
    once, and a haze and a fan slot, 1 to 512, two different slots, none a
    group's safety or fire slot or another hazer's. flamesafe checks the
    same file on its own; this is the second reader, not the guard."""
    raw = doc.get("passthrough", []) if isinstance(doc, dict) else []
    where = f"{path}: 'passthrough'"
    if not isinstance(raw, list) or len(raw) > HAZERS_MAX:
        raise FlameLinkConfigError(f"{where} has to be a list of at most "
                                   f"{HAZERS_MAX} hazers. flamesafe refuses "
                                   f"this file too.")
    taken = set()
    for g in doc.get("groups") or []:
        if isinstance(g, dict):
            saf = g.get("safety")
            for x in (saf if isinstance(saf, list) else [saf]) + list(
                    g.get("fire") or []):
                if _is_int(x):
                    taken.add(x)
    names, out = set(), []
    for e in raw:
        name = e.get("name") if isinstance(e, dict) else None
        if not isinstance(name, str) or not name.strip() or \
                len(name) > HAZER_NAME_MAX or name in names:
            raise FlameLinkConfigError(f"{where}: {name!r} is not a hazer "
                                       f"name of its own. flamesafe refuses "
                                       f"this file too.")
        if "switch" in e:
            raise FlameLinkConfigError(
                f"{where}: {name!r} is a switch entry (the fuel pump), which "
                f"needs a newer LTC Player. Take it out of flamesafe.json or "
                f"update LTC Player.")
        slots = [e.get("haze"), e.get("fan")]
        if any(not _is_int(x) or not 1 <= x <= UNIVERSE_SIZE or x in taken
               for x in slots) or slots[0] == slots[1]:
            raise FlameLinkConfigError(f"{where}: {name!r} needs a haze and "
                                       f"a fan slot, 1 to {UNIVERSE_SIZE}, "
                                       f"two different slots, neither a "
                                       f"flame group's or another hazer's. "
                                       f"flamesafe refuses this file too.")
        names.add(name)
        taken.update(slots)
        out.append((name, tuple(slots)))
    return tuple(out)


def aim_slots_of(doc, path="flamesafe's config"):
    """Every group's "aim" slots (2026-10-28): fire slots that only point a
    head, like the Showven's angle channel, which rests at 128 (straight
    up) and so is almost never seen at zero. Read as plain JSON (the wall).
    An aim slot that is not one of its group's fire slots is ignored here
    (flamesafe refuses that file at start). The per fixture rules are
    flamesafe's own and are checked again here, so a typo that would free
    a fuel slot stops `ltc serve` too: aim only in a group with a
    safety_profile (the wave flamer), at most one aim slot per fixture,
    each channel 1 of its own fixture (the angle, 5 below the fixture's
    safety slot, channel 6), and the fixture's fire slots exactly that
    slot and the 4 after it.
    A malformed list is refused, never skipped. This is a second reader,
    not the guard: flamesafe still refuses the file on its own, and it
    sends an aim slot only while the group is armed."""
    out = set()
    groups = doc.get("groups") if isinstance(doc, dict) else None
    for g in groups if isinstance(groups, list) else ():
        if not isinstance(g, dict) or "aim" not in g:
            continue
        aim, fire = g.get("aim"), g.get("fire")
        if not isinstance(aim, list) or not isinstance(fire, list) or any(
                not _is_int(a) or not 1 <= a <= UNIVERSE_SIZE for a in aim):
            raise FlameLinkConfigError(
                f"{path}: group {g.get('name')!r} has an 'aim' that is not "
                f"a list of slots 1 to {UNIVERSE_SIZE}.")
        aim = [a for a in aim if a in fire]
        bad = (_aim_fixture_fault(aim, fire, g.get("safety"))
               if "safety_profile" in g else
               "aim is only for a group with a safety_profile.")
        if bad:
            raise FlameLinkConfigError(
                f"{path}: group {g.get('name')!r} 'aim': {bad} flamesafe "
                f"refuses this file too.")
        out.update(aim)
    return frozenset(out)


def _aim_fixture_fault(aim, fire, safety):
    """Why these aim slots would free a flame channel, or None. The same
    rules as flamesafe's config._aim_per_fixture."""
    saf = safety if isinstance(safety, list) else [safety]
    if not saf or not all(_is_int(s) for s in saf):
        return "the group's safety slots cannot be read."
    saf = sorted(saf)
    block = {}
    for a in aim:
        k = 0 if len(saf) == 1 else next(
            (i for i, s in enumerate(saf) if a < s), None)
        if k is None:
            return f"slot {a} is after the last safety slot."
        if k in block:
            return f"slots {block[k]} and {a} are in one fixture."
        block[k] = a
    if len({a - saf[k] for k, a in block.items()}) > 1:
        return f"{sorted(aim)} are not the same channel of each fixture."
    for k, a in block.items():
        if a != saf[k] - 5:
            return f"slot {a} is not channel 1 of its fixture."
        lo = saf[k - 1] if k else 0
        if {f for f in fire if len(saf) == 1 or lo < f < saf[k]} != set(
                range(a, a + 5)):
            return f"the fixture of slot {a} is not one 6 channel block."
    return None


def _exc_text(e):
    """An exception as "Name: message", never raising: a provider's
    exception whose str() itself raises used to kill the sender thread from
    inside its own error handling (fix round 1 of PR #34, item 6)."""
    try:
        name = type(e).__name__
    except Exception:
        name = "an exception"
    try:
        text = str(e)
    except Exception:
        text = "(its message could not be read)"
    return f"{name}: {text}"[:300]


def _random_start():
    return RANDOM_START_MIN + secrets.randbelow(RANDOM_START_SPAN)


def _ipv4(text):
    parts = text.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255
                                   for p in parts)


# -- frame timing (2026-10-29) -------------------------------------------------
# Jeff's Pico, 2026-10-29: "flamesafe keeps a fire value on the output for at
# most 100 ms after the last frame it received (fire_hold_ms)", and the
# journal had 11 frames 78 to 178 ms late in 2.5 hours. A late frame can
# drop or shorten a 25 ms pulse on every wave flamer at once. So every
# interval between two frames this sender sends is counted, on the sender
# thread (one compare and two additions a frame; nothing it can wait on),
# and summed up per show and per hour for the journal, the status and the
# rack screen. Display and journal only: nothing here changes what is sent.
TIMING_LIMITS_MS = (50, 75, 100, 150)
# The limit the alert, the rack screen and the soak judge by: flamesafe's
# own fire_hold_ms, read from its config with the rest of the link
# (FlameLinkConfig.fire_hold_ms), the gap after which flamesafe zeroes the
# fire slots. This is only the fallback when that is not known (a config
# built in code). Read here, never changed here.
TIMING_ALERT_MS = 100
TIMING_HOUR_S = 3600.0
# next_deadline: no two frames closer together than this share of a period
# (5 ms at 40 a second).
MIN_GAP_SHARE = 0.2
_TIMING_BINS = 301              # 0 to 299 ms by the millisecond, then more


class FrameTiming:
    """The intervals between flame frames, in one window (a show, an hour,
    since start). add() is the sender thread's; everything else only
    reads. Never raises."""

    __slots__ = ("since", "frames", "worst_ms", "over", "bins", "total_ms",
                 "limit_ms", "over_lim")

    def __init__(self, since=None, limit_ms=TIMING_ALERT_MS):
        self.since = since
        self.limit_ms = limit_ms
        self.over_lim = 0
        self.frames = 0
        self.worst_ms = 0.0
        self.over = [0] * len(TIMING_LIMITS_MS)
        self.bins = [0] * _TIMING_BINS
        self.total_ms = 0.0

    def add(self, gap_s):
        try:
            ms = gap_s * 1000.0
            self.frames += 1
            self.total_ms += ms
            if ms > self.worst_ms:
                self.worst_ms = ms
            self.bins[min(_TIMING_BINS - 1, max(0, int(ms)))] += 1
            if ms > self.limit_ms:
                self.over_lim += 1
            if ms > TIMING_LIMITS_MS[0]:
                for i, lim in enumerate(TIMING_LIMITS_MS):
                    if ms > lim:
                        self.over[i] += 1
        except Exception:
            pass

    def p99_ms(self):
        """The interval 99 in 100 were at or under, to the millisecond
        above (the last bin: at least 300)."""
        n = self.frames
        if not n:
            return 0
        want = n - n // 100
        run = 0
        for ms, c in enumerate(self.bins):
            run += c
            if run >= want:
                return ms + 1
        return _TIMING_BINS

    def view(self):
        """A plain dict for the status JSON and the soak report."""
        return {"intervals": self.frames,
                "worst_ms": round(self.worst_ms, 1),
                "p99_ms": self.p99_ms(),
                "mean_ms": round(self.total_ms / self.frames, 2)
                if self.frames else 0.0,
                "over_ms": {str(lim): self.over[i]
                            for i, lim in enumerate(TIMING_LIMITS_MS)},
                "limit_ms": self.limit_ms,
                "over_limit": self.over_lim}

    def sentence(self, what):
        """One plain journal line: what (e.g. "this hour") and the counts."""
        v = self.view()
        o = v["over_ms"]
        return (f"Flame frame timing, {what}: {v['intervals']} intervals, "
                f"worst {v['worst_ms']:.0f} ms, 99 in 100 within "
                f"{v['p99_ms']} ms; over 50 ms: {o['50']}, over 75 ms: "
                f"{o['75']}, over 100 ms: {o['100']}, over 150 ms: "
                f"{o['150']}; over flamesafe's fire_hold_ms "
                f"({v['limit_ms']} ms): {v['over_limit']}.")


def next_deadline(next_at, now, period):
    """When the sender sends next: always on the grid of its first frame,
    one period apart (absolute deadlines: a sleep that wakes late is made
    up on the next one, so no drift ever builds up and the rate stays 40 a
    second). The next grid point is skipped while it is less than
    MIN_GAP_SHARE of a period away (or already gone): after a stall the
    frame just sent was the overdue one, it went once, and the cadence
    carries on at the next grid point. A missed tick is never sent late,
    so there is never a burst to catch up, and no two frames ever go
    closer together than MIN_GAP_SHARE of a period."""
    nxt = next_at + period
    gap = period * MIN_GAP_SHARE
    if nxt - now >= gap:
        return nxt
    return nxt + math.ceil((now + gap - nxt) / period) * period


# -- the two inputs ----------------------------------------------------------

def zero_cues(tc):
    """The cue provider in this build: no flame cue values exist yet, so
    the answer is always None (all zeros). See the module docstring."""
    return None


def no_show():
    """The show state when nothing is wired: no timecode, not live."""
    return None, False


def audio_master_state(get_clock):
    """A show_state() reading clock.AudioMaster (duck-typed, never
    imported): `get_clock()` returns the running session's clock or None.
    Live only while a cue is playing, not paused or fading into a pause
    (AudioMaster.paused is True from the request on), and not fading out
    on an Abort (_halting). The timecode is the frame the clock last
    sent, which is what the pixels and MadMapper are showing."""
    def state():
        clk = get_clock()
        if clk is None:
            return None, False
        last = getattr(clk, "last_sent", None)
        playing = bool(getattr(clk, "playing", False))
        tc = None
        if playing and last:
            h, m, s, f = last
            tc = f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"
        live = (playing and not getattr(clk, "paused", True)
                and not getattr(clk, "_halting", False) and tc is not None)
        return tc, live
    return state


# -- the wire ----------------------------------------------------------------

def encode_flame(seq, tc, mono, universe, values, key, hazers_on=None):
    """A flame frame. `hazers_on` (2026-10-29): the names of the hazers
    switched ON, [] for none, sent whenever flamesafe's config lists any
    hazers; None (no hazers configured) leaves the field out, so the frame
    is exactly the one before hazers existed (CONTRACT.md: left out or
    empty is every hazer off)."""
    # Byte for byte what json.dumps of the whole object gives (the selftest
    # compares the two), built from parts so that the 512 values, which
    # are the same frame after frame, are turned into text once and not
    # on every frame (2026-10-29: the sender's own time per frame is time
    # another thread can take the interpreter from it).
    head = json.dumps({
        "v": CONTRACT_VERSION, "k": key, "t": "flame", "seq": int(seq),
        "tc": tc, "mono": float(mono), "universe": int(universe),
    }, separators=(",", ":"))
    tail = "" if hazers_on is None else ',"hazers_on":' + json.dumps(
        sorted(str(h) for h in hazers_on), separators=(",", ":"))
    return (head[:-1] + ',"values":' + _values_json(values) + tail
            + "}").encode("utf-8")


_VALUES_TEXT = (None, None)     # (the last values as bytes, their JSON)


def _values_json(values):
    """The values as JSON text, "[0,0,...]", the same as json.dumps of
    [int(v) for v in values]. The last bytes seen are remembered (one
    tuple, swapped whole, so two threads can never mix a key and a text)."""
    global _VALUES_TEXT
    if isinstance(values, (bytes, bytearray)):
        b = bytes(values)
        last = _VALUES_TEXT
        if last[0] == b:
            return last[1]
        text = json.dumps(list(b), separators=(",", ":"))
        _VALUES_TEXT = (b, text)
        return text
    return json.dumps([int(v) for v in values], separators=(",", ":"))


def encode_disarm_all(seq, mono, abort_id, reason, key):
    return json.dumps({
        "v": CONTRACT_VERSION, "k": key, "t": "disarm_all", "seq": int(seq),
        "mono": float(mono), "id": int(abort_id), "reason": str(reason),
    }, separators=(",", ":")).encode("utf-8")


def decode_status(data, key):
    """flamesafe's status frame as a dict, or None if it is not one with
    our key (CONTRACT.md: ltcplay MUST reject a status frame whose k is not
    its own). Never raises."""
    try:
        obj = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or obj.get("v") != CONTRACT_VERSION \
            or obj.get("t") != "status" or obj.get("k") != key:
        return None
    return obj


def _no_connreset(sock):
    """Windows: without this, an ICMP port-unreachable from a send while
    flamesafe is not running makes the NEXT send raise. The same ioctl
    flamesafe/service.py makes, written again here (the wall)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        ws2 = ctypes.WinDLL("ws2_32", use_last_error=True)
        f = ws2.WSAIoctl
        f.argtypes = [ctypes.c_size_t, ctypes.c_ulong, ctypes.c_void_p,
                      ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                      ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p,
                      ctypes.c_void_p]
        f.restype = ctypes.c_int
        off = ctypes.c_ulong(0)
        ret = ctypes.c_ulong(0)
        return f(sock.fileno(), SIO_UDP_CONNRESET, ctypes.byref(off),
                 ctypes.sizeof(off), None, 0, ctypes.byref(ret), None,
                 None) == 0
    except Exception:
        return False


class FlameLink:
    """ltcplay's flame-link sender. See the module docstring.

    cfg         FlameLinkConfig
    cues        (tc) -> 512 ints or None. zero_cues in this build.
    show_state  () -> (tc text or None, live). no_show unless wired.
    journal     (text, **fields) -> anything; its failures never stop it.
    clock       perf_counter: `mono` in every frame, and the pacing.

    zero(), release() and disarm_all() return True only when what they
    promise went out (or, for release, will go out on the next frame), so
    fire_ice.FireIceShow can turn them into a conductor Result. They never
    raise."""

    def __init__(self, cfg, cues=zero_cues, show_state=no_show, journal=None,
                 clock=time.perf_counter, sleep=time.sleep,
                 tc_fps=TC_FPS_DEFAULT, seek_guard=True):
        self.cfg = cfg
        # Aim slots as 0-based channel indexes: never held by the zero-
        # first rule (see _guarded).
        self._aim = frozenset(s - 1 for s in getattr(cfg, "aim_slots", ()))
        # The hazers (2026-10-29): name -> its 0-based (haze, fan)
        # channels, every hazer channel, the names ARMED (none at start),
        # each one's stored (haze, fan) levels 0 to 255 (0 until the engine
        # says), the engine's gate (() -> True while a show is playing and
        # not held; None: never), and whether this pass's show state was
        # live (values_now).
        self._hazers = {n: tuple(x - 1 for x in sl)
                        for n, sl in getattr(cfg, "hazers", ())}
        self._hazer_chans = frozenset(c for sl in self._hazers.values()
                                      for c in sl)
        self._hazers_on = frozenset()
        self._hazer_levels = {}
        self.haze_gate = None
        self._haze_live = False
        self.tc_fps = float(tc_fps)
        # Always on in the program. Only the selftest's tests of the OTHER
        # rules (moving, released, providers) turn it off, so their cue
        # values are not held back by a settle they are not about.
        self.seek_guard = bool(seek_guard)
        # The seek guard: (seconds, clock) of the last timecode, when the
        # current steady run began, and the channels still waiting to be
        # seen at zero since the last seek. All channels wait at first.
        self._seek_last = None
        self._steady_since = None
        self._blocked = self._guarded()
        self.seeks = 0
        self.cues = cues
        self.show_state = show_state
        self._journal = journal
        self._clock = clock
        self._sleep = sleep
        # _lock guards the socket, seq/mono and what is sent. The providers
        # are NEVER called under it (fix round 1, item 7): _read_lock only
        # keeps two send_frame() callers from asking them at once.
        self._lock = threading.RLock()
        self._read_lock = threading.Lock()
        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        # Random large starts (fix round 1, items 3 and 4): seq so that a
        # rogue counting from 1 is never "ours" to the lock alarm, abort
        # ids so that no other run's Abort can confirm this run's.
        self.seq = _random_start()
        self.first_seq = None
        self._mono_last = None
        self.zeroed = True          # until the conductor releases the cues
        self._zero_gen = 0          # +1 on every zero()/disarm_all()
        # The purge frame (PURGE_HOLD_S): (bytes(512), clock when set), or
        # None. Read by values_now while the show is not live.
        self._purge = None
        self.purges_sent = 0
        # A timed purge's hard end on this link's clock (purge_limit), or
        # None: past it the purge frame is zeros until purge_clear().
        self._purge_until = None
        # Live flame programming (PROGRAM_HOLD_S): on between program_begin
        # and program_end (or a disarm_all); the frame is (bytes(512),
        # clock when set) or None.
        self._program_on = False
        self._program = None
        self._program_session = 0
        self.programs_sent = 0
        # The Fireball page's frame (MANUAL_HOLD_S): (bytes(512), clock when
        # set, the zero count then), or None; the only slots it may carry;
        # how many frames it went out on. Every zero() and disarm_all()
        # (Hold, a show's end, Stop, Abort) moves the zero count, and a
        # frame set before that is never sent again (_with_manual).
        self._manual = None
        self.manual_slots = frozenset()
        self.manuals_sent = 0
        # The show path's own values (cues, purge, programming) of the
        # frame being read, before the Fireball page's frame is merged, and
        # of the last frame that went out (all zero when it did not):
        # fireball_cues judges "the show's charge carrying on" from this
        # alone, never from Andy's presses (third review, D-F1).
        self._show_part = bytes(UNIVERSE_SIZE)
        self.show_sent = bytes(UNIVERSE_SIZE)
        # Programming transport moves (landing()): +1 on each, so a frame
        # whose values were read across one goes out as zeros.
        self._land_gen = 0
        self.landings = 0
        self._land_open = False     # a landing not yet over (_landing_now)
        self._land_tc = None        # the timecode when it was called
        # Lockout (2026-10-17): channels (1-based slots) zeroed in every
        # frame this link sends, cue or purge. flamesafe enforces the same
        # set from the lockout file; this is the belt to its braces.
        self._locked = frozenset()
        self.abort_id = _random_start()
        self._abort_repeat = None   # (abort_id, reason, repeat until)
        self.sent = 0
        self.send_errors = 0
        self.last_values_nonzero = False
        self._fail_since = None     # clock when the current outage began
        self._fail_count = 0
        self._cue_problem = ""      # the current provider episode, if any
        self._cue_kind = ""         # ...and its kind, which is what repeats
        self._tc_last = None        # the last valid timecode seen...
        self._tc_moved_at = None    # ...and when it last changed
        self._was_live = False      # live on the frame before
        self._held_tc = None        # the frame a Hold froze on (not live,
                                    # timecode still there)
        self._resumed_at = None     # when the show read as playing again
        # the sender thread's own health (fix round 1, item 6)
        self.run_errors = 0
        self._run_fail_since = None
        self._run_fail_count = 0
        self._run_dead = ""         # why the thread stopped without stop()
        # Fix round 2, item 2: a sender that stops sending WITHOUT raising
        # (a provider or the journal blocking) is "stalled" once no frame
        # has gone out for half of frame_stale_ms. A small watch thread
        # journals it once per episode; snapshot() works it out from the
        # clock, so it is right even while the journal itself is stuck.
        self._started_at = None
        self._last_frame_at = None  # clock after each send_frame's send
        self._watch = None
        self._stall_noted = False
        # from flamesafe's status frames (note_status)
        self.lock_alarm = ""
        self._not_ours_since = None
        self._pending_abort = None  # (abort_id, sent at)
        self._abort_unconfirmed = False
        self._abort_confirmed_id = None
        # False when flamesafe's config names no link.status_mirror_port
        # (fire_ice.build_flame_link sets it, and says so once when the link
        # starts): no status frame can ever reach this program, so an
        # unconfirmed disarm is expected, not a fault on every Abort (safety
        # audit of bench-build, P2 a). The Stream Deck still confirms it.
        self.status_mirrored = True
        # The fuel tracker (fuel.py, 2026-10-26): (tc, values, ok) after
        # each frame send_frame sent. It only appends to a queue; it can
        # neither change nor hold up a frame. None: nothing is called.
        self.observer = None
        # Frame timing (2026-10-29, FrameTiming): every interval between
        # two frames of the sender thread, since start, this hour and this
        # show. Only the sender thread writes them. A finished hour or show
        # goes on _timing_done, which the watch thread journals; every
        # interval over TIMING_ALERT_MS also goes on _timing_late (when,
        # how long), for "N in the last hour". deque append and popleft
        # are atomic, so neither side ever waits on the other.
        # The limit: flamesafe's own fire_hold_ms when its config said it.
        self.timing_limit_ms = int(getattr(cfg, "fire_hold_ms", None)
                                   or TIMING_ALERT_MS)
        self._timing_total = FrameTiming(None, self.timing_limit_ms)
        self._timing_shows = FrameTiming(None, self.timing_limit_ms)
        self._timing_hour = None
        self._timing_last_hour = None
        self._timing_show = None
        self._timing_done = collections.deque()
        self._timing_late = collections.deque(maxlen=2000)
        self._frame_tc = None

    # -- the journal -----------------------------------------------------------
    def _note(self, text, **fields):
        if self._journal is None:
            return
        try:
            self._journal(text, **fields)
        except Exception:
            pass

    # -- lifetime --------------------------------------------------------------
    def open(self):
        """The one socket, for the whole life of this link."""
        with self._lock:
            if self._sock is not None:
                return
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if _no_connreset(s) is False:
                self._note("Flame link: SIO_UDP_CONNRESET could not be "
                           "switched off; a send while flamesafe is not "
                           "running may fail the next one.", fault=True,
                           action="flame_link", outcome="socket")
            self._sock = s
        self._note(f"Flame link: sending flame cue frames to flamesafe at "
                   f"{self.cfg.ip}:{self.cfg.port}, universe "
                   f"{self.cfg.universe}, {self.cfg.send_hz} a second. "
                   f"Every frame is zero until a show is running and its "
                   f"flame cues are released.", action="flame_link",
                   outcome="open")
        if self.cfg.key == EXAMPLE_KEY:
            self._note("Flame link: the key is still the example key from "
                       "the repo. Fine on a bench; a show needs its own key, "
                       "the same in flamesafe's config and this one.",
                       fault=True, action="flame_link", outcome="example_key")

    def start(self):
        """Open, then send at send_hz on a thread of its own until stop()."""
        self.open()
        if self._thread is None:
            self._stop.clear()
            self._run_dead = ""
            self._started_at = self._clock()
            self._stall_noted = False
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="ltcplay-flame-link")
            self._thread.start()
            self._watch = threading.Thread(target=self._watch_run,
                                           daemon=True,
                                           name="ltcplay-flame-link-watch")
            self._watch.start()
        return self

    def stall_s(self):
        """No frame for longer than this (half of flamesafe's
        frame_stale_ms) and a running sender is stalled."""
        return self.cfg.frame_stale_ms / 2000.0

    def _stalled_now(self):
        last = self._last_frame_at
        if last is None:
            last = self._started_at
        if last is None:
            return False
        try:
            return self._clock() - last > self.stall_s()
        except Exception:
            return True

    def _watch_run(self):
        """Journals a stall once when it starts and once when it ends.
        Never sends anything and never touches the link's locks."""
        while not self._stop.wait(0.05):
            self._timing_journal()
            try:
                stalled = self.sender_state() == "stalled"
                if stalled and not self._stall_noted:
                    self._stall_noted = True
                    self._note(f"Flame link: no flame frame has gone to "
                               f"flamesafe for more than "
                               f"{self.stall_s():g} s, though the sender "
                               f"has not failed (something it waits on is "
                               f"stuck). flamesafe disarms every group "
                               f"after {self.cfg.frame_stale_ms} ms of "
                               f"this. This line will not repeat until it "
                               f"recovers.", fault=True,
                               action="flame_link", outcome="sender_stalled")
                elif not stalled and self._stall_noted:
                    self._stall_noted = False
                    self._note("Flame link: flame frames are going to "
                               "flamesafe again after a stall.",
                               action="flame_link",
                               outcome="sender_unstalled")
            except Exception:
                pass

    def stop(self):
        """Stop sending. flamesafe then disarms every group within its own
        frame_stale_ms, exactly as if ltcplay had died."""
        self._stop.set()
        for t in (self._thread, self._watch):
            if t is not None and t is not threading.current_thread():
                t.join(2.0)
        self._thread = None
        self._watch = None
        with self._lock:
            s, self._sock = self._sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass
        self._note("Flame link: stopped sending; flamesafe disarms every "
                   "group once it notices.", action="flame_link",
                   outcome="stopped")

    close = stop

    def _run(self):
        """The sender thread. Fix round 1 of PR #34, item 6: an exception
        anywhere in one pass is journaled (one line when an episode starts,
        one when it clears, with the count) and the loop carries on; it
        used to end the thread with no line at all while snapshot() still
        said sending was fine. If the thread ends any other way than
        stop(), that is journaled and snapshot() says the sender is dead."""
        period = 1.0 / self.cfg.send_hz
        next_at = None
        why = "it returned without being stopped"
        last_start = slept = None
        try:
            while not self._stop.is_set():
                try:
                    if next_at is None:
                        next_at = self._clock()
                    start = self._clock()
                    self.send_frame()
                    took = self._clock() - start
                    if last_start is not None:
                        self._timing_add(start - last_start, start)
                        self._note_late(start - last_start, slept, took)
                    last_start = start
                    self._run_ok()
                    now = self._clock()
                    # Fell behind (the OS woke us late): the overdue frame
                    # has just gone, the next is one period on. No burst to
                    # catch up, not even one extra frame (next_deadline).
                    next_at = next_deadline(next_at, now, period)
                    delay = next_at - now
                except Exception as e:
                    self._run_failed(e)
                    next_at = None
                    delay = period
                slept = None
                if delay > 0:
                    # The injected sleep (time.sleep, as the pixel loop
                    # paces), so the schedule can be proved on a fake
                    # clock. At most 50 ms at a time, so stop() is never
                    # kept waiting.
                    want = min(delay, 0.05)
                    t_sleep = self._clock()
                    self._sleep(want)
                    slept = (want, self._clock() - t_sleep)
                    self._note_oversleep(slept[1] - slept[0])
        except BaseException as e:
            # Not re-raised: the line below says it, and a daemon thread's
            # traceback on stderr would say nothing more to anyone.
            why = _exc_text(e)
        finally:
            if not self._stop.is_set():
                self._run_dead = why
                self._note(f"Flame link: the sender thread STOPPED ({why}). "
                           f"No flame frames are going to flamesafe, so it "
                           f"disarms every group. Restart ltcplay.",
                           fault=True, action="flame_link",
                           outcome="sender_dead")

    def _timing_add(self, gap, now):
        """The sender thread's count of one interval (FrameTiming): since
        start, this hour, this show. A show is counted from its first live
        frame to the first frame with no show timecode at all (a Hold is
        still the show). Never raises, never waits."""
        try:
            self._timing_total.add(gap)
            hour = self._timing_hour
            if hour is None:
                hour = self._timing_hour = FrameTiming(
                    now, self.timing_limit_ms)
            elif now - hour.since >= TIMING_HOUR_S:
                self._timing_done.append(("in the last hour", hour))
                self._timing_last_hour = hour
                hour = self._timing_hour = FrameTiming(
                    now, self.timing_limit_ms)
            hour.add(gap)
            show = self._timing_show
            if show is not None:
                show.add(gap)
                self._timing_shows.add(gap)
            if gap * 1000.0 > self.timing_limit_ms:
                self._timing_late.append((now, gap * 1000.0))
            if self._haze_live:
                if show is None:
                    self._timing_show = FrameTiming(
                        now, self.timing_limit_ms)
            elif show is not None and self._frame_tc is None:
                self._timing_show = None
                self._timing_done.append(("in the show that just ended",
                                          show))
        except Exception:
            pass

    def _timing_journal(self):
        """The watch thread's half: one journal line for each finished hour
        and show, and one more when it had a frame over TIMING_ALERT_MS.
        Notes, not faults: the rack screen's alert (remote.py) is the
        fault, raised as it happens. Never raises."""
        try:
            while self._timing_done:
                what, w = self._timing_done.popleft()
                v = w.view()
                self._note(w.sentence(what), action="flame_link",
                           outcome="frame_timing", timing=v)
                if v["over_limit"]:
                    self._note(f"Flame frames late: {v['over_limit']} "
                               f"{what} more than {v['limit_ms']} ms "
                               f"after the one before, worst "
                               f"{v['worst_ms']:.0f} ms. flamesafe holds a "
                               f"fire value for its fire_hold_ms after the "
                               f"last frame, so a pulse may have been cut "
                               f"short.", action="flame_link",
                               outcome="frames_late")
        except Exception:
            pass

    def late_last_hour(self, now=None):
        """(how many intervals over TIMING_ALERT_MS in the last hour, the
        worst of them in ms). Never raises."""
        try:
            now = self._clock() if now is None else now
            got = []
            for _ in range(3):
                try:
                    got = list(self._timing_late)
                    break
                except RuntimeError:
                    continue
            got = [ms for t, ms in got if now - t <= TIMING_HOUR_S]
            return len(got), (round(max(got), 1) if got else 0.0)
        except Exception:
            return 0, 0.0

    def timing_view(self):
        """The status's "frame_timing": since start, in every show so far,
        this hour, the last whole hour, this show (None outside one), and
        the last hour's frames over the limit. Plain reads; never
        raises."""
        try:
            n, worst = self.late_last_hour()
            out = {"limit_ms": self.timing_limit_ms,
                   "late_last_hour": n, "late_last_hour_worst_ms": worst}
            for k, w in (("since_start", self._timing_total),
                         ("in_shows", self._timing_shows),
                         ("this_hour", self._timing_hour),
                         ("last_hour", self._timing_last_hour),
                         ("this_show", self._timing_show)):
                out[k] = w.view() if w is not None else None
            return out
        except Exception:
            return {"limit_ms": self.timing_limit_ms}

    LATE_S = 0.045      # a frame this long after the last one is noted
    LATE_NOTE_EVERY_S = 10.0

    def _note_oversleep(self, over):
        """Diagnostics only (show PC, 2026-10-04): the worst amount the
        sender's sleep overran, per wall-clock minute, the last 15 minutes.
        One dict write; never raises."""
        try:
            m = int(time.time() // 60)
            d = self.__dict__.setdefault("_oversleep", {})
            if over > d.get(m, 0.0):
                d[m] = over
            while len(d) > 15:
                del d[min(d)]
        except Exception:
            pass

    def _note_late(self, gap, slept, took):
        """A frame that went out LATE_S or more after the one before (the
        contract's floor is 50 ms) is journaled, with where the time went:
        the sender's own sleep waking late (the OS: timer resolution, power
        throttling, an overloaded core) or the frame itself taking long
        (another thread holding the interpreter, a slow provider). At most
        one line per LATE_NOTE_EVERY_S, with the count since the last.
        Show PC, 2026-10-04: a 71 ms gap with no show running."""
        self.late_frames = getattr(self, "late_frames", 0)
        if gap < self.LATE_S:
            return
        self.late_frames += 1
        worst = getattr(self, "late_worst", 0.0)
        if gap > worst:
            self.late_worst = gap
        now = self._clock()
        last = getattr(self, "_late_noted_at", None)
        if last is not None and now - last < self.LATE_NOTE_EVERY_S:
            self._late_unnoted = getattr(self, "_late_unnoted", 0) + 1
            return
        self._late_noted_at = now
        more = getattr(self, "_late_unnoted", 0)
        self._late_unnoted = 0
        if slept is not None:
            want, got = slept
            where = (f"the sender's sleep of {want * 1000:.1f} ms took "
                     f"{got * 1000:.1f} ms")
        else:
            where = "the sender did not sleep before it"
        self._note(f"Flame link: a frame went out {gap * 1000:.1f} ms after "
                   f"the one before (the contract's floor is 50 ms): "
                   f"{where}, and sending the frame took {took * 1000:.1f} "
                   f"ms." + (f" {more} more late frame(s) since the last "
                             f"such line." if more else ""),
                   action="flame_link", outcome="late_frame")

    def _run_failed(self, e):
        self.run_errors += 1
        self._run_fail_count += 1
        if self._run_fail_since is None:
            try:
                self._run_fail_since = self._clock()
            except Exception:
                self._run_fail_since = 0.0
            self._note(f"Flame link: the sender failed ({_exc_text(e)}). "
                       f"It keeps trying every frame; flamesafe disarms "
                       f"every group if no frame gets through. This line "
                       f"will not repeat until it recovers.", fault=True,
                       action="flame_link", outcome="sender_failed")

    def _run_ok(self):
        if self._run_fail_since is None:
            return
        self._note(f"Flame link: the sender is working again after "
                   f"{self._run_fail_count} failed pass(es).",
                   action="flame_link", outcome="sender_recovered")
        self._run_fail_since = None
        self._run_fail_count = 0

    # -- what goes in a frame --------------------------------------------------
    def values_now(self):
        """(tc, values) for a frame sent now: the show's (or the purge's, or
        live flame programming's) values, with the Fireball page's frame
        merged on its slots (_with_manual). Never raises; zeros on anything
        uncertain. Calls the providers: never under self._lock."""
        tc, vals, manual_ok = self._values_show()
        self._show_part = bytes(vals)
        if not manual_ok:
            return tc, vals
        return tc, self._with_manual(vals, self._clock())

    def _values_show(self):
        """(tc, values, whether the Fireball page's frame may be merged)
        before that frame: the show path, the purge and live flame
        programming exactly as before it existed."""
        self._haze_live = False
        zeros = bytes(UNIVERSE_SIZE)
        try:
            tc, live = self.show_state()
        except Exception as e:
            self._cue_episode("state", f"the show state could not be read "
                                       f"({_exc_text(e)})")
            return None, zeros, False
        if not (isinstance(tc, str) and _TC.fullmatch(tc)):
            tc = None
        # Is the timecode MOVING (fix round 1, item 5)? Tracked on every
        # valid timecode, live or not, so a resume is not a false stall.
        now = self._clock()
        if tc is not None and tc != self._tc_last:
            self._tc_last, self._tc_moved_at = tc, now
        # A Programming transport move still landing (landing()): the
        # timecode is still the one from before it, so it is never used.
        land = self.seek_guard and self._landing_now(tc)
        if not self.seek_guard:
            settled = True
        elif land:
            settled = False
        else:
            settled = self._seek_guard(tc, now)
        # A Hold: not live with the show's timecode still there. Its Resume
        # is the first live frame after it.
        if live is True:
            # A show cue is playing: the hazers may run (send_frame also
            # asks the engine's haze_gate).
            self._haze_live = True
            if not self._was_live:
                self._resumed_at = now if self._held_tc is not None else None
            self._was_live = True
        else:
            self._was_live = False
            self._held_tc = tc
            self._resumed_at = None
        if live is not True:
            # No show cue playing (idle, between shows, a Hold, an Abort
            # fading out): the purge frame may go, if one is fresh. This is
            # the ONLY place the purge is read, so a live show is never
            # fed two sources of fire values (MORNING.md, T1). With live
            # flame programming on, its frame and never the purge's (one
            # source at a time).
            self._cue_episode("", "")
            if self._program_on:
                # Live flame programming's frame alone (one source at a
                # time): not the Fireball page's either.
                return tc, self._program_values(now), False
            return tc, self._purge_values(now), True
        if self._program_on:
            # Live flame programming is on: the show's own cues are held
            # off (one source at a time) and its frame is not read either.
            # The mode ends as soon as the engine sees the show start.
            # Nor the Fireball page's frame: one source at a time.
            return tc, zeros, False
        if self.zeroed or tc is None or land:
            self._cue_episode("", "")
            return tc, zeros, True
        if self._tc_moved_at is None or now - self._tc_moved_at > TC_STILL_S:
            settling = (self._resumed_at is not None
                        and tc == self._held_tc
                        and now - self._resumed_at <= RESUME_SETTLE_S)
            if settling:
                self._cue_episode(
                    "settling", f"the show timecode is still on the frame the "
                                f"Hold froze ({tc}) while the Resume settles",
                    fault=False)
            else:
                self._cue_episode(
                    "still", f"the show timecode has not moved for more than "
                             f"{TC_STILL_S:g} s (stuck at {tc}) though the "
                             f"show reads as playing")
            return tc, zeros, True
        try:
            v = self.cues(tc)
        except Exception as e:
            self._cue_episode("raised", f"the flame cue provider raised "
                                        f"{_exc_text(e)}")
            return tc, zeros, True
        if v is None:
            self._cue_episode("", "")
            return tc, zeros, True
        try:
            vals = list(v)
            if len(vals) != UNIVERSE_SIZE or \
                    any(not _is_int(x) or not 0 <= x <= 255 for x in vals):
                raise ValueError
        except Exception:
            self._cue_episode("garbage", f"the flame cue provider did not "
                                         f"answer {UNIVERSE_SIZE} whole "
                                         f"numbers 0 to 255")
            return tc, zeros, True
        self._cue_episode("", "")
        # A channel seen at zero since the last seek may fire again: its
        # next rise is a cue start this show actually played through.
        blocked = self._blocked if self.seek_guard else ()
        if blocked:
            for i in [i for i in blocked if vals[i] == 0]:
                blocked.discard(i)
        if not settled:
            return tc, zeros, True
        if blocked:
            for i in blocked:
                vals[i] = 0
        return tc, self._unlocked(bytes(vals)), True

    # -- the purge frame and the lockout (2026-10-17) -------------------------
    def _purge_values(self, now):
        """The purge frame, while one has been set within PURGE_HOLD_S on
        this link's own clock; else zeros. A stale purge is dropped here,
        so nothing above this line has to remember to clear it."""
        p = self._purge
        if p is None:
            return bytes(UNIVERSE_SIZE)
        if self._purge_until is not None and \
                (now - p[1] > PURGE_HOLD_S or now < p[1]):
            # A timed purge whose frame went stale (its timer stalled)
            # never comes back by itself: its hard end is now, so
            # purge_set() is refused until purge_clear() (re-review of
            # #172, B1). Under _lock, and only for that same frame: a
            # purge_clear() or purge_limit() since wins (round 3).
            with self._lock:
                if self._purge is p and self._purge_until is not None:
                    self._purge_until = min(self._purge_until, now)
        if now - p[1] > PURGE_HOLD_S or now < p[1]:
            self._purge = None
            return bytes(UNIVERSE_SIZE)
        until = self._purge_until
        if until is not None and now >= until:
            # A timed purge's hard end (purge_limit): zeros from here on,
            # whatever is still calling purge_set().
            self._purge = None
            return bytes(UNIVERSE_SIZE)
        self.purges_sent += 1
        return self._unlocked(p[0])

    def purge_set(self, values, gen=None):
        """The engine's purge heartbeat: `values` (512 ints 0 to 255) go out
        on the next frames for PURGE_HOLD_S, then stop by themselves unless
        set again. Only while the show is not live (values_now). Returns
        True when the frame was taken. Never raises."""
        try:
            vals = bytes(values)
            if len(vals) != UNIVERSE_SIZE:
                raise ValueError("wrong length")
        except Exception:
            return False
        with self._lock:
            if gen is not None and gen != self._zero_gen:
                # A zero() or disarm_all() (Stop, Hold, Abort) landed after
                # the caller read the count: under this lock, never set.
                self._purge = None
                return False
            now = self._clock()
            until = self._purge_until
            p = self._purge
            if until is not None and p is not None and \
                    (now - p[1] > PURGE_HOLD_S or now < p[1]):
                # The timed frame this one would replace has gone stale
                # (its timer stalled) whether or not a frame was sent
                # meanwhile: never relit, as _purge_values (round 3 of
                # #172).
                self._purge_until = min(until, now)
                self._purge = None
                return False
            if until is not None and now >= until:
                # Past a timed purge's hard end: refused until purge_clear().
                self._purge = None
                return False
            self._purge = (vals, now)
        return True

    def purge_limit(self, seconds, deadline=None, clock=None):
        """A timed purge's hard end: `seconds` from now on this link's own
        clock, the purge frame stops on every frame sent after it and
        purge_set() is refused, until purge_clear() (which every end of a
        timed purge calls). With the caller's `deadline` and `clock` (its
        own clock, which need not be this link's), the end is that
        deadline, worked out under this lock from this link's clock read
        FIRST and the caller's second: a pause between them only brings
        it earlier, never later (round 3 of #172). True when taken. Never
        raises."""
        try:
            s = float(seconds)
            if not s > 0 or not math.isfinite(s):
                return False
            with self._lock:
                mine = self._clock()
                if deadline is not None and clock is not None:
                    left = float(deadline) - float(clock())
                    if not math.isfinite(left):
                        return False
                    s = min(s, left)
                    if not s > 0:
                        return False
                # A timed purge starts from no frame: nothing older is
                # carried into it (or judged stale against it).
                self._purge = None
                self._purge_until = mine + s
            return True
        except Exception:
            return False

    def purge_clear(self):
        """Stop a purge now: the next frame carries zeros (or the show's
        cues, if a show is live). Also done by zero() and disarm_all().
        Lifts a timed purge's hard end (purge_limit) too."""
        with self._lock:
            self._purge = None
            self._purge_until = None

    def purge_active(self):
        """True while a purge frame set within PURGE_HOLD_S is held (and
        before a timed purge's hard end)."""
        p = self._purge
        until = self._purge_until
        try:
            now = self._clock()
            return p is not None and 0 <= now - p[1] <= PURGE_HOLD_S and \
                (until is None or now < until)
        except Exception:
            return False

    # -- the Fireball page's frame (fireball.py, 2026-10-30) ------------------
    def set_manual_slots(self, slots):
        """The only slots the Fireball page's frame may carry (the fireball
        group's fire slots). Anything that is not a slot 1 to 512 is left
        out. Never raises."""
        try:
            self.manual_slots = frozenset(
                int(s) for s in slots
                if not isinstance(s, bool) and 1 <= int(s) <= UNIVERSE_SIZE)
        except Exception:
            self.manual_slots = frozenset()

    def manual_set(self, values, gen=None):
        """The Fireball page's heartbeat: `values` (512 ints 0 to 255) on
        manual_slots go out on the next frames for MANUAL_HOLD_S, live or
        not, merged with the show's own by the higher value per channel.
        With `gen`, refused (False) when a zero() or disarm_all() has
        happened since the caller read _zero_gen. True when taken."""
        try:
            vals = bytes(values)
            if len(vals) != UNIVERSE_SIZE:
                return False
            keep = bytearray(UNIVERSE_SIZE)
            for s in self.manual_slots:
                keep[s - 1] = vals[s - 1]
            with self._lock:
                if gen is not None and self._zero_gen != gen:
                    self._manual = None
                    return False
                self._manual = (bytes(keep), self._clock(), self._zero_gen)
            return True
        except Exception:
            return False

    def manual_clear(self):
        """The Fireball page's frame off now: the next frame carries only
        the show's own values on its slots. zero() and disarm_all() end it
        too, by the zero count."""
        with self._lock:
            self._manual = None

    def manual_active(self):
        """True while a Fireball page frame set within MANUAL_HOLD_S is
        held."""
        m = self._manual
        try:
            return m is not None and m[2] == self._zero_gen and \
                0 <= self._clock() - m[1] <= MANUAL_HOLD_S
        except Exception:
            return False

    def _with_manual(self, vals, now):
        """`vals` with the Fireball page's frame merged: on each of its
        slots the higher of the two values, then the lockout. A frame older
        than MANUAL_HOLD_S (or from a clock that went back), or set before
        the last zero() or disarm_all(), is dropped here."""
        m = self._manual
        if m is None:
            return vals
        if m[2] != self._zero_gen or now - m[1] > MANUAL_HOLD_S or \
                now < m[1]:
            with self._lock:
                if self._manual is m:
                    self._manual = None
            return vals
        out = bytearray(vals)
        hit = False
        for s in self.manual_slots:
            v = m[0][s - 1]
            if v > out[s - 1]:
                out[s - 1] = v
                hit = True
        if not hit:
            return vals
        self.manuals_sent += 1
        return self._unlocked(bytes(out))

    # -- live flame programming (flameprog.py) -------------------------------
    def _program_values(self, now):
        """The live flame programming frame, while one has been set within
        PROGRAM_HOLD_S on this link's own clock; else zeros."""
        p = self._program
        if p is None or not self._program_on:
            return bytes(UNIVERSE_SIZE)
        if now - p[1] > PROGRAM_HOLD_S or now < p[1]:
            self._program = None
            return bytes(UNIVERSE_SIZE)
        self.programs_sent += 1
        return self._unlocked(p[0])

    def program_begin(self):
        """Live flame programming is on: program_set is taken from now on,
        with the session number this returns. Starts with no frame."""
        with self._lock:
            self._program = None
            self._program_session += 1
            self._program_on = True
            return self._program_session

    def program_end(self):
        """Live flame programming is off: the frame is dropped now and
        program_set is refused until the next program_begin."""
        with self._lock:
            self._program_on = False
            self._program = None

    @property
    def program_on(self):
        return self._program_on

    def program_set(self, values, session):
        """The listener's frame: `values` (512 ints 0 to 255) go out on the
        next frames for PROGRAM_HOLD_S, then stop by themselves unless set
        again. Only while the mode is on, for the session program_begin
        gave, and while the show is not live. True when the frame was
        taken. Never raises."""
        try:
            vals = bytes(values)
            if len(vals) != UNIVERSE_SIZE:
                raise ValueError("wrong length")
        except Exception:
            return False
        with self._lock:
            if not self._program_on:
                return False
            if session != self._program_session:
                return False
            self._program = (vals, self._clock())
        return True

    def program_clear(self):
        """Zeros from the next frame on (the mode stays on)."""
        with self._lock:
            self._program = None

    def program_active(self):
        """True while a live flame programming frame set within
        PROGRAM_HOLD_S is held."""
        p = self._program
        try:
            return (self._program_on and p is not None
                    and 0 <= self._clock() - p[1] <= PROGRAM_HOLD_S)
        except Exception:
            return False

    def _guarded(self):
        """Every channel the zero-first rule holds after a landing: all but
        the aim slots. An aim slot (the Showven's angle) rests at 128 and
        is almost never seen at zero, so holding it pinned the head to its
        end stop for the whole show (Jeff, 2026-10-28). It lights nothing;
        flamesafe still sends it only while its group is armed."""
        return set(range(UNIVERSE_SIZE)) - self._aim

    # -- Programming transport moves (playhead.py) ----------------------------
    def _seek_reset(self):
        """The seek guard as a new link has it: the next timecode is a
        landing and every channel waits to be seen at zero."""
        self._seek_last = None
        self._steady_since = None
        self._blocked = self._guarded()

    def landing(self):
        """A Programming transport move (Play from, Back, Ahead, Continue,
        the loop; playhead.py calls it before and after each): the seek
        guard starts again however small the move, so every flame value is
        zero until the timecode has run steadily for SEEK_SETTLE_S, and a
        channel already up where it lands stays zero until it has been
        seen at zero. Until the show timecode differs from the one the
        clock has sent now, every frame is zero and nothing is learned
        from it (_landing_now). A frame read across it goes out as zeros
        (send_frame). It only takes fire away. Never raises. It reads the
        show state once, outside the lock, and never the cues."""
        try:
            tc = self.show_state()[0]
            if not isinstance(tc, str) or not _TC.fullmatch(tc):
                tc = None
        except Exception:
            tc = None
        try:
            with self._lock:
                self._land_gen += 1
                self.landings += 1
                self._land_tc = tc
                self._land_open = True
                self._seek_reset()
        except Exception:
            pass

    def _landing_now(self, tc):
        """True while a landing is not over: the timecode is none, or still
        the one the clock had sent at landing(). Each such frame starts the
        seek guard again. The first other timecode ends it, and is the
        guard's first timecode, as on a new link."""
        if not self._land_open:
            return False
        if tc is None or tc == self._land_tc:
            self._seek_reset()
            return True
        self._land_open = False
        self._seek_reset()
        return False

    # -- the hazers (2026-10-29) ---------------------------------------------
    def hazer_levels_set(self, levels):
        """Each hazer's stored levels, {name: (haze, fan)}, whole numbers 0
        to 255, from the next frame. A name this link does not have, or a
        value that is not a whole number 0 to 255, is dropped (that hazer
        then outputs 0). Never raises."""
        out = {}
        try:
            for n, (h, f) in dict(levels).items():
                if n in self._hazers and all(
                        _is_int(v) and 0 <= v <= 255 for v in (h, f)):
                    out[n] = (h, f)
        except Exception:
            out = {}
        with self._lock:
            self._hazer_levels = out
        return out

    def _haze_running(self):
        """True when the hazers may output this frame: the show state was
        live on this pass AND the engine's gate says a show is playing and
        not held. Asked outside self._lock. Never raises."""
        if not self._haze_live:
            return False
        gate = self.haze_gate
        try:
            return gate is not None and gate() is True
        except Exception:
            return False

    def hazer_names(self):
        """Every hazer's name, in flamesafe's config order."""
        return [n for n, _sl in getattr(self.cfg, "hazers", ())]

    @property
    def hazers_on(self):
        return self._hazers_on

    def abort_repeating(self):
        """True while an Abort (or Stop) is still being repeated to
        flamesafe (disarm_all): flamesafe zeroes every hazer on each copy,
        so none is switched on until the copies have stopped."""
        rep = self._abort_repeat
        try:
            return rep is not None and self._clock() <= rep[2]
        except Exception:
            return True

    def hazers_set(self, names):
        """The hazers switched ON from the next frame: every other one OFF.
        A name this link does not have is dropped, and so is any hazer not
        already ON while an Abort is being repeated (abort_repeating).
        Returns what is ON."""
        with self._lock:
            want = frozenset(n for n in names if n in self._hazers)
            if self.abort_repeating():
                want &= self._hazers_on
            self._hazers_on = want
            return self._hazers_on

    def _hazers_said(self):
        """The frame's hazers_on: the names ON, or None (the field left
        out) when flamesafe's config lists no hazers."""
        return self._hazers_on if self._hazers else None

    def _with_haze(self, values, run):
        """`values` with every hazer channel replaced: zero, whatever the
        FSEQ or xLights carried there; then, when `run`, each
        ARMED hazer's stored haze and fan levels. Under self._lock."""
        if not self._hazer_chans:
            return values
        out = bytearray(values)
        for c in self._hazer_chans:
            out[c] = 0
        if run:
            for n in self._hazers_on:
                ch = self._hazers.get(n)
                lv = self._hazer_levels.get(n)
                if ch and lv:
                    out[ch[0]], out[ch[1]] = lv[0], lv[1]
        return bytes(out)

    def set_locked_channels(self, slots):
        """The locked out fire slots (1-based), zeroed in every frame this
        link sends from now on. Rubbish locks nothing extra here: flamesafe
        is the enforcement, this is the belt. Never raises."""
        try:
            self._locked = frozenset(int(s) for s in slots
                                     if 1 <= int(s) <= UNIVERSE_SIZE)
        except Exception:
            pass

    def _unlocked(self, vals):
        locked = self._locked
        if not locked or not any(vals):
            return vals
        out = bytearray(vals)
        for s in locked:
            out[s - 1] = 0
        return bytes(out)

    def _tc_seconds(self, tc):
        h, m, s, f = int(tc[0:2]), int(tc[3:5]), int(tc[6:8]), int(tc[9:11])
        return h * 3600 + m * 60 + s + f / self.tc_fps

    def _seek_guard(self, tc, now):
        """True once the timecode has run steadily for SEEK_SETTLE_S since
        the last seek. Called with every frame's timecode, live or not."""
        if tc is None:
            return False
        secs = self._tc_seconds(tc)
        last = self._seek_last          # (seconds, when it was first seen)
        if last is None:
            self._seek_last = (secs, now)
            self._seek(None, secs)
            return False
        dtc = secs - last[0]
        if dtc == 0:
            # The same frame again. Frames are slower than sends, so this
            # is routine; only a timecode still for longer than TC_STILL_S
            # (a pause or a hold) ends the steady run.
            if now - last[1] > TC_STILL_S:
                self._steady_since = None
        else:
            dt = now - last[1]
            self._seek_last = (secs, now)
            if dtc < 0:
                self._seek(last[0], secs)
            elif dt > TC_STILL_S:
                # Moving again after standing still: a resume carries on
                # from where it stood (it is not a seek) but it has to run
                # steadily again before any flame value goes out. Anything
                # that moved further than a resume can is a seek.
                if dtc > SEEK_JUMP_S:
                    self._seek(last[0], secs)
                else:
                    self._steady_since = now
            elif abs(dtc - dt) > SEEK_JUMP_S:
                self._seek(last[0], secs)
            elif self._steady_since is None:
                self._steady_since = now
        return (self._steady_since is not None
                and now - self._steady_since >= SEEK_SETTLE_S)

    def _seek(self, frm, to):
        self.seeks += 1
        self._steady_since = None
        self._blocked = self._guarded()
        if frm is not None:
            self._note(f"Flame link: the show timecode jumped from "
                       f"{frm:.2f} s to {to:.2f} s. Flame values are zero "
                       f"until it has run steadily for {SEEK_SETTLE_S:g} s, "
                       f"and a flame cue already under way at the landing "
                       f"point stays zero until it ends.",
                       action="flame_link", outcome="seek")

    def _cue_episode(self, kind, problem, fault=True):
        """One line when a KIND of problem starts and one when it clears:
        keyed by the kind, never the text, so a provider whose message
        changes every frame is still one line. `fault` False (a Resume
        settling) journals it as a note; the cues are zeros either way."""
        if kind == self._cue_kind:
            return
        if kind:
            self._note(f"Flame link: {problem}, so flame cues are zero. "
                       f"This line will not repeat until it clears.",
                       fault=fault, action="flame_link",
                       outcome="cues_zero" if fault else "cues_zero_note")
        else:
            self._note("Flame link: the flame cue values are readable "
                       "again.", action="flame_link", outcome="cues_back")
        self._cue_kind = kind
        self._cue_problem = problem

    # -- sending ---------------------------------------------------------------
    def _next(self):
        """seq and mono for the next datagram, under the lock: seq goes up
        by one per datagram and mono never goes backwards, whichever thread
        sends."""
        self.seq += 1
        if self.first_seq is None:
            self.first_seq = self.seq
        mono = self._clock()
        if self._mono_last is not None and mono < self._mono_last:
            mono = self._mono_last
        self._mono_last = mono
        return self.seq, mono

    def _send(self, data):
        s = self._sock
        if s is None:
            return False
        try:
            s.sendto(data, (self.cfg.ip, self.cfg.port))
        except OSError as e:
            self.send_errors += 1
            self._fail_count += 1
            if self._fail_since is None:
                self._fail_since = self._clock()
                self._note(f"Flame link: a frame to flamesafe could not be "
                           f"sent ({type(e).__name__}: {e}). flamesafe "
                           f"disarms every group if this lasts. Retrying "
                           f"every frame; this line will not repeat until "
                           f"it recovers.", fault=True, action="flame_link",
                           outcome="send_failed")
            return False
        self.sent += 1
        if self._fail_since is not None:
            gone = self._clock() - self._fail_since
            self._note(f"Flame link: sending again after {gone:.1f} s; "
                       f"{self._fail_count} frame(s) were not sent.",
                       action="flame_link", outcome="send_recovered")
            self._fail_since = None
            self._fail_count = 0
        return True

    def send_frame(self):
        """One flame frame, now, and while an Abort is being repeated one
        more copy of it. True when the flame frame went out.

        The providers are asked OUTSIDE self._lock (fix round 1, item 7),
        so zero() and disarm_all() never wait on them. A frame whose values
        were read before a zero() or disarm_all() that landed meanwhile
        goes out as zeros: `_zero_gen` says one did."""
        gen = self._zero_gen
        lgen = self._land_gen
        with self._read_lock:
            tc, values = self.values_now()
            run = self._haze_running()
            self._frame_tc = tc         # frame timing's show window only
        with self._lock:
            if lgen != self._land_gen:
                # A Programming transport move landed while this frame was
                # read: its position may be from before the move, and what
                # it saw (a channel at zero, a steady run) is not true
                # where the move landed. Zeros, and the guard starts again.
                values = bytes(UNIVERSE_SIZE)
                self._seek_reset()
            if gen != self._zero_gen or (self.zeroed and self._purge is None
                                         and self._program is None
                                         and self._manual is None):
                # A zero() or disarm_all() that landed since the values were
                # read wins (gen); and with the cues zeroed, only a purge
                # frame still held may carry a value (the purge frame is the
                # one thing that goes out while the conductor has the cues
                # zeroed, and only while the show is not live: values_now;
                # so is a live flame programming frame, by the same rule).
                values = bytes(UNIVERSE_SIZE)
            # The hazers (2026-10-29): their channels zero in the flame
            # values (fire, the fuel tracker, "firing"), then each ARMED
            # hazer's stored levels while a show plays. A frame read before
            # a zero() or disarm_all() that landed meanwhile carries none.
            values = self._with_haze(values, False)
            self.last_values_nonzero = any(values)
            sp = self._show_part
            show = bytes(map(min, sp, values)) \
                if len(sp) == len(values) else bytes(UNIVERSE_SIZE)
            wire = self._with_haze(values, run and gen == self._zero_gen)
            seq, mono = self._next()
            ok = self._send(encode_flame(seq, tc, mono, self.cfg.universe,
                                         wire, self.cfg.key,
                                         self._hazers_said()))
            self._last_frame_at = self._clock()
            self.show_sent = show if ok else bytes(UNIVERSE_SIZE)
            self._repeat_abort()
            late = self._abort_overdue()
        if late is not None:
            self._note_unconfirmed(late)
        obs = self.observer
        if obs is not None:
            # The fuel tracker (fuel.py): told what went out, after it went.
            try:
                obs(tc, values, ok)
            except Exception:
                pass
        return ok

    def _send_zero_frame(self):
        """An all-zero flame frame, now, built from nothing but zeros: no
        provider is asked (fix round 1, item 7). Under self._lock."""
        seq, mono = self._next()
        self.last_values_nonzero = False
        return self._send(encode_flame(seq, None, mono, self.cfg.universe,
                                       bytes(UNIVERSE_SIZE), self.cfg.key,
                                       self._hazers_said()))

    def _abort_overdue(self):
        """The pending Abort's id the first time it is overdue (sent more
        than CONFIRM_S ago and no status frame has confirmed it), else
        None. Under self._lock. Asked on every flame frame as well as on
        every status frame (PR #43 review, finding 5): with flamesafe
        stopped or frozen, or with no status mirror, no status frame ever
        comes, and the fault must still be raised."""
        pend = self._pending_abort
        if pend is None or self._abort_unconfirmed:
            return None
        if not self.status_mirrored:
            return None
        if self._clock() - pend[1] > CONFIRM_S:
            self._abort_unconfirmed = True
            return pend[0]
        return None

    def _note_unconfirmed(self, aid):
        self._note(f"Flame link: flamesafe has not confirmed the disarm "
                   f"(abort {aid}) after {CONFIRM_S:g} s: no status frame "
                   f"from flamesafe has said it took it. Check the flame "
                   f"groups on the Stream Deck.", fault=True,
                   action="flame_link", outcome="disarm_unconfirmed")

    def _repeat_abort(self):
        """One more copy of the Abort being repeated, if any (fix round 1,
        item 1). Under self._lock."""
        rep = self._abort_repeat
        if rep is None:
            return
        aid, why, until = rep
        if self._clock() > until:
            self._abort_repeat = None
            return
        seq, mono = self._next()
        self._send(encode_disarm_all(seq, mono, aid, why, self.cfg.key))

    # -- the conductor's three calls -------------------------------------------
    def zero(self):
        """flames_zero: every cue value is zero from now until release(),
        and one zero frame goes out at once rather than at the next tick.
        True when that frame went out. Never asks the providers, so a hung
        one cannot delay it (fix round 1, item 7)."""
        with self._lock:
            self._purge = None
            # Any zero (a Hold, a show's end, the dark sequence) ends live
            # flame programming at the link too: xLights stops with the
            # fire (flameprog.py ends the mode on its next pass).
            # No timed purge's hard end outlives a zero (review of #172).
            self._purge_until = None
            self._program = None
            self._program_on = False
            self.zeroed = True
            self._zero_gen += 1
            return self._send_zero_frame()

    def release(self):
        """flames_release: cue values go out again from the next frame, and
        still only while the show is live (values_now). True if the link
        is open to carry them."""
        with self._lock:
            self.zeroed = False
            return self._sock is not None

    def disarm_all(self, reason):
        """The show's Abort: cues to zero, one zero frame, then
        DISARM_COPIES disarm_all datagrams for one new abort id at once,
        and one more copy after every flame frame for abort_repeat_s (fix
        round 1, item 1). True when at least one disarm_all went out now:
        SENT, which is not the same as flamesafe having taken it; only its
        status frame can say that (note_status). Never raises, and never
        asks the providers (fix round 1, item 7)."""
        try:
            why = " ".join(str(reason or "Abort").split())[:REASON_MAX] \
                or "Abort"
            with self._lock:
                # Abort and Stop switch every hazer OFF (Jeff, 2026-10-29),
                # here at the link before the zero frame and the first
                # copy, whichever surface sent it. flamesafe does the same
                # on the disarm_all itself.
                self._hazers_on = frozenset()
                self._purge = None
                # An Abort ends live flame programming at the link, at
                # once, whichever surface sent it (flameprog.py sees it).
                # No timed purge's hard end outlives it (review of #172).
                self._purge_until = None
                self._program = None
                self._program_on = False
                self.zeroed = True
                self._zero_gen += 1
                self._send_zero_frame()
                self.abort_id += 1
                aid = self.abort_id
                ok = False
                for _ in range(DISARM_COPIES):
                    seq, mono = self._next()
                    ok |= self._send(encode_disarm_all(seq, mono, aid, why,
                                                       self.cfg.key))
                # Repeated whether or not these went out: a send failing
                # now may work on the next frame.
                repeat_s = self.cfg.abort_repeat_s
                self._abort_repeat = (aid, why, self._clock() + repeat_s)
                self._pending_abort = (aid, self._clock())
                self._abort_unconfirmed = False
                self._abort_confirmed_id = None
        except Exception as e:
            self._note(f"Flame link: the disarm could not be built "
                       f"({_exc_text(e)}).", fault=True,
                       action="flame_link", outcome="disarm_failed")
            return False
        if ok:
            self._note(f"Flame link: {why}: asked flamesafe to disarm every "
                       f"flame group (abort {aid}), repeating it every frame "
                       f"for {repeat_s:g} s. Sent, NOT yet confirmed by "
                       f"flamesafe: its status frame says whether it took "
                       f"it. Each group it took needs a fresh arm cycle "
                       f"from the Stream Deck.",
                       action="flame_link", outcome="disarm_sent")
        else:
            self._note(f"Flame link: {why}: the disarm could NOT be sent to "
                       f"flamesafe (abort {aid}; it is retried every frame "
                       f"for {repeat_s:g} s). Flame cues are zero. Disarm "
                       f"with Disarm all flame groups on the Rack screen, "
                       f"the Stream Deck's group keys, or the hardware "
                       f"e-stop.",
                       fault=True, action="flame_link",
                       outcome="disarm_failed")
        return ok

    # -- flamesafe's answer ----------------------------------------------------
    def note_status(self, status):
        """One status frame from flamesafe (decode_status's dict). Raises
        the contract's lock alarm, and the unconfirmed-disarm fault. Never
        raises; returns the lock alarm sentence ("" when fine)."""
        try:
            now = self._clock()
            frames = status.get("frames") or {}
            fseq = frames.get("seq")
            sending = self._sock is not None and self.first_seq is not None
            ours = (_is_int(fseq) and self.first_seq is not None
                    and self.first_seq <= fseq <= self.seq)
            if not sending or ours:
                self._not_ours_since = None
                if self.lock_alarm:
                    self._note("Flame link: flamesafe is taking this "
                               "program's frames again.",
                               action="flame_link", outcome="lock_back")
                    self.lock_alarm = ""
            else:
                if self._not_ours_since is None:
                    self._not_ours_since = now
                if now - self._not_ours_since > LOCK_ALARM_S and \
                        not self.lock_alarm:
                    self.lock_alarm = (
                        f"flamesafe's last accepted flame frame (seq "
                        f"{fseq!r}) is not one this program sent: something "
                        f"else holds the flame link")
                    self._note(f"Flame link ALARM: {self.lock_alarm}. That "
                               f"is a person's problem: find what else on "
                               f"this machine has the key.", fault=True,
                               action="flame_link", outcome="lock_alarm")
            pend = self._pending_abort
            if pend is not None:
                da = status.get("disarm_all") or {}
                last = da.get("last_id")
                # Equality, never ">=" (fix round 1, item 4): ids start at
                # a random number per run, so only THIS Abort's id
                # confirms it; another run's larger id says nothing.
                if _is_int(last) and last == pend[0]:
                    self._pending_abort = None
                    self._abort_confirmed_id = pend[0]
                    late = ", late" if self._abort_unconfirmed else ""
                    self._note(f"Flame link: flamesafe confirmed the "
                               f"disarm (abort {pend[0]}){late}: every "
                               f"flame group is disarmed.",
                               action="flame_link",
                               outcome="disarm_confirmed")
                    self._abort_unconfirmed = False
                else:
                    with self._lock:
                        late = self._abort_overdue()
                    if late is not None:
                        self._note_unconfirmed(late)
            return self.lock_alarm
        except Exception:
            return self.lock_alarm

    def sender_state(self):
        """"running", "failing" (passes raising, still trying), "stalled"
        (no frame for more than half of frame_stale_ms without anything
        raising: something it waits on is stuck), "dead" (the thread ended
        without stop()), "stopped", or "not started"."""
        t = self._thread
        if self._run_dead or (t is not None and not t.is_alive()
                              and not self._stop.is_set()):
            return "dead"
        if t is None:
            return "stopped" if self._stop.is_set() else "not started"
        if self._run_fail_since is not None:
            return "failing"
        if self._stalled_now():
            return "stalled"
        return "running"

    def abort_state(self):
        """What is known about the last Abort, in words: "" before any,
        then "sent, not yet confirmed by flamesafe", "sent but NOT
        confirmed by flamesafe" (after CONFIRM_S), or "confirmed by
        flamesafe". Fix round 1, item 1: never more than was proven."""
        if self._pending_abort is not None:
            return ("sent but NOT confirmed by flamesafe"
                    if self._abort_unconfirmed
                    else "sent, not yet confirmed by flamesafe")
        if self._abort_confirmed_id is not None:
            return "confirmed by flamesafe"
        return ""

    def snapshot(self):
        """For the page and the health panel. `sending_ok` is False while
        sends fail, while the sender thread's passes fail, and once that
        thread has died (fix round 1, item 6)."""
        sender = self.sender_state()
        # No lock (fix round 2, item 2): this must answer while the sender
        # is stuck, and a stuck journal call can be holding the link lock.
        # Every field is one plain read.
        return {"open": self._sock is not None,
                "seq": self.seq, "sent": self.sent,
                "send_errors": self.send_errors,
                "sender": sender,
                "run_errors": self.run_errors,
                "sending_ok": (self._fail_since is None
                               and sender not in ("failing", "dead",
                                                  "stalled")),
                "zeroed": self.zeroed,
                "nonzero": self.last_values_nonzero,
                "seeks": self.seeks,
                "late_frames": getattr(self, "late_frames", 0),
                "late_worst_ms": round(getattr(self, "late_worst", 0.0)
                                       * 1000.0, 1),
                "oversleep_ms_by_minute": {
                    str(k): round(v * 1000.0, 1) for k, v in
                    list(getattr(self, "_oversleep", {}).items())},
                "frame_timing": self.timing_view(),
                "cue_problem": self._cue_problem,
                "purge": self.purge_active(),
                "purges_sent": self.purges_sent,
                "program_on": self._program_on,
                "program": self.program_active(),
                "programs_sent": self.programs_sent,
                "manual": self.manual_active(),
                "manuals_sent": self.manuals_sent,
                "landings": self.landings,
                "locked_channels": sorted(self._locked),
                "hazers_on": sorted(self._hazers_on),
                "lock_alarm": self.lock_alarm,
                "abort_id": self.abort_id,
                "abort": self.abort_state(),
                "disarm_unconfirmed": self._abort_unconfirmed}
