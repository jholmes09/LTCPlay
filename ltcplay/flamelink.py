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
     over never fires at all: it stays zero until it has ended.

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
LOCK_ALARM_S = 1.0          # CONTRACT.md: "for more than 1 s"
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
                 frame_stale_ms=FRAME_STALE_MS_DEFAULT):
        self.ip = ip
        self.port = port
        self.universe = universe
        self.key = key
        self.send_hz = send_hz
        self.frame_stale_ms = frame_stale_ms

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
        return cls.parse({"ip": link.get("listen_ip", "127.0.0.1"),
                          "port": link.get("listen_port"),
                          "universe": doc.get("universe"),
                          "key": link.get("key"), "send_hz": send_hz,
                          "frame_stale_ms": doc.get("frame_stale_ms")},
                         where=str(path))


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

def encode_flame(seq, tc, mono, universe, values, key):
    return json.dumps({
        "v": CONTRACT_VERSION, "k": key, "t": "flame", "seq": int(seq),
        "tc": tc, "mono": float(mono), "universe": int(universe),
        "values": [int(v) for v in values],
    }, separators=(",", ":")).encode("utf-8")


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
        self._blocked = set(range(UNIVERSE_SIZE))
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
        try:
            while not self._stop.is_set():
                try:
                    if next_at is None:
                        next_at = self._clock()
                    self.send_frame()
                    self._run_ok()
                    next_at += period
                    now = self._clock()
                    if next_at < now - period:
                        # Fell behind (the OS woke us late): the overdue
                        # frame has just gone, the next is one period on.
                        # No burst to catch up, not even one extra frame.
                        next_at = now + period
                    delay = next_at - now
                except Exception as e:
                    self._run_failed(e)
                    next_at = None
                    delay = period
                if delay > 0:
                    # The injected sleep (time.sleep, as the pixel loop
                    # paces), so the schedule can be proved on a fake
                    # clock. At most 50 ms at a time, so stop() is never
                    # kept waiting.
                    self._sleep(min(delay, 0.05))
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
        """(tc, values) for a frame sent now. Never raises; zeros on
        anything uncertain. Calls the providers: never under self._lock."""
        zeros = bytes(UNIVERSE_SIZE)
        try:
            tc, live = self.show_state()
        except Exception as e:
            self._cue_episode("state", f"the show state could not be read "
                                       f"({_exc_text(e)})")
            return None, zeros
        if not (isinstance(tc, str) and _TC.fullmatch(tc)):
            tc = None
        # Is the timecode MOVING (fix round 1, item 5)? Tracked on every
        # valid timecode, live or not, so a resume is not a false stall.
        now = self._clock()
        if tc is not None and tc != self._tc_last:
            self._tc_last, self._tc_moved_at = tc, now
        settled = self._seek_guard(tc, now) if self.seek_guard else True
        if self.zeroed or live is not True or tc is None:
            self._cue_episode("", "")
            return tc, zeros
        if self._tc_moved_at is None or now - self._tc_moved_at > TC_STILL_S:
            self._cue_episode("still", f"the show timecode has not moved for "
                                       f"more than {TC_STILL_S:g} s (stuck at "
                                       f"{tc}) though the show reads as "
                                       f"playing")
            return tc, zeros
        try:
            v = self.cues(tc)
        except Exception as e:
            self._cue_episode("raised", f"the flame cue provider raised "
                                        f"{_exc_text(e)}")
            return tc, zeros
        if v is None:
            self._cue_episode("", "")
            return tc, zeros
        try:
            vals = list(v)
            if len(vals) != UNIVERSE_SIZE or \
                    any(not _is_int(x) or not 0 <= x <= 255 for x in vals):
                raise ValueError
        except Exception:
            self._cue_episode("garbage", f"the flame cue provider did not "
                                         f"answer {UNIVERSE_SIZE} whole "
                                         f"numbers 0 to 255")
            return tc, zeros
        self._cue_episode("", "")
        # A channel seen at zero since the last seek may fire again: its
        # next rise is a cue start this show actually played through.
        blocked = self._blocked if self.seek_guard else ()
        if blocked:
            for i in [i for i in blocked if vals[i] == 0]:
                blocked.discard(i)
        if not settled:
            return tc, zeros
        if blocked:
            for i in blocked:
                vals[i] = 0
        return tc, bytes(vals)

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
        self._blocked = set(range(UNIVERSE_SIZE))
        if frm is not None:
            self._note(f"Flame link: the show timecode jumped from "
                       f"{frm:.2f} s to {to:.2f} s. Flame values are zero "
                       f"until it has run steadily for {SEEK_SETTLE_S:g} s, "
                       f"and a flame cue already under way at the landing "
                       f"point stays zero until it ends.",
                       action="flame_link", outcome="seek")

    def _cue_episode(self, kind, problem):
        """One line when a KIND of problem starts and one when it clears:
        keyed by the kind, never the text, so a provider whose message
        changes every frame is still one line."""
        if kind == self._cue_kind:
            return
        if kind:
            self._note(f"Flame link: {problem}, so flame cues are zero. "
                       f"This line will not repeat until it clears.",
                       fault=True, action="flame_link", outcome="cues_zero")
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
        with self._read_lock:
            tc, values = self.values_now()
        with self._lock:
            if self.zeroed or gen != self._zero_gen:
                values = bytes(UNIVERSE_SIZE)
            seq, mono = self._next()
            self.last_values_nonzero = any(values)
            ok = self._send(encode_flame(seq, tc, mono, self.cfg.universe,
                                         values, self.cfg.key))
            self._last_frame_at = self._clock()
            self._repeat_abort()
            return ok

    def _send_zero_frame(self):
        """An all-zero flame frame, now, built from nothing but zeros: no
        provider is asked (fix round 1, item 7). Under self._lock."""
        seq, mono = self._next()
        self.last_values_nonzero = False
        return self._send(encode_flame(seq, None, mono, self.cfg.universe,
                                       bytes(UNIVERSE_SIZE), self.cfg.key))

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
                       f"with the Stream Deck's Abort or its group keys.",
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
                elif now - pend[1] > CONFIRM_S and \
                        not self._abort_unconfirmed:
                    self._abort_unconfirmed = True
                    self._note(f"Flame link: flamesafe has not confirmed the "
                               f"disarm (abort {pend[0]}) after "
                               f"{CONFIRM_S:g} s. Check the flame groups on "
                               f"the Stream Deck.", fault=True,
                               action="flame_link",
                               outcome="disarm_unconfirmed")
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
                "cue_problem": self._cue_problem,
                "lock_alarm": self.lock_alarm,
                "abort_id": self.abort_id,
                "abort": self.abort_state(),
                "disarm_unconfirmed": self._abort_unconfirmed}
