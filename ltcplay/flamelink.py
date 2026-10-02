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
     conductor's own zero() never arrived;
  3. the cue provider answered with exactly 512 whole numbers 0 to 255 for
     that timecode, without raising. Anything else is zeros for that frame,
     journaled once per episode.

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
own seq) on the same socket. flamesafe accepts it only from the live,
locked flame-link sender with the right key; it clears every group's latch
and every pending consent edge, so each group needs a fresh, genuine arm
cycle from the Stream Deck afterwards. It can never arm anything.

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
one line per episode. A key still equal to the repo's example key: a fault
line at open. note_status() takes flamesafe's status frames (from whoever
reads `link.status_port`; see the PR) and raises the contract's lock alarm
when the last accepted frame is not one of ours for more than 1 s, and a
fault when a disarm_all is not confirmed within 1 s.
"""
import json
import math
import re
import socket
import sys
import threading
import time

CONTRACT_VERSION = 2
UNIVERSE_SIZE = 512
SEND_HZ_DEFAULT = 40
SEND_HZ_MIN = 20            # CONTRACT.md: "20 Hz or faster, idle included"
SEND_HZ_MAX = 100
DISARM_COPIES = 3           # one Abort, a few datagrams, in case one is lost
LOCK_ALARM_S = 1.0          # CONTRACT.md: "for more than 1 s"
CONFIRM_S = 1.0             # a disarm_all not confirmed by then is a fault
KEY_MIN, KEY_MAX = 16, 128
REASON_MAX = 200
# The key in flamesafe/flamesafe.example.json. Fine on a bench; a show
# config must carry its own (CONTRACT.md). Written here as a string to
# compare against, never used as a default.
EXAMPLE_KEY = "fire-and-ice-2026-replace-this-key"
CONFIG_KEYS = frozenset(("ip", "port", "universe", "key", "send_hz"))
_KEY = re.compile(r"^[\x21-\x7e]+$")
_TC = re.compile(r"^\d{2}:\d{2}:\d{2}[:;]\d{2}$")
SIO_UDP_CONNRESET = 0x9800000C


class FlameLinkConfigError(ValueError):
    """The flame link settings are wrong, in a sentence."""


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def valid_key(key):
    return (isinstance(key, str) and KEY_MIN <= len(key) <= KEY_MAX
            and bool(_KEY.match(key)))


class FlameLinkConfig:
    """Where flamesafe listens, its flame universe, the shared key, and the
    send rate. The key is read from a config file at run time, never
    written in this repo (CLAUDE.md: never commit keys)."""

    def __init__(self, ip, port, universe, key, send_hz=SEND_HZ_DEFAULT):
        self.ip = ip
        self.port = port
        self.universe = universe
        self.key = key
        self.send_hz = send_hz

    @classmethod
    def parse(cls, doc, where="flame_link"):
        """A "flame_link" block: {"ip": "127.0.0.1", "port": 5571,
        "universe": 1, "key": "<flamesafe's link.key>", "send_hz": 40}.
        `ip` and `send_hz` may be left out."""
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
        return cls(ip, port, universe, key, hz)

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
                          "key": link.get("key"), "send_hz": send_hz},
                         where=str(path))


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
                 clock=time.perf_counter, sleep=time.sleep):
        self.cfg = cfg
        self.cues = cues
        self.show_state = show_state
        self._journal = journal
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.RLock()
        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self.seq = 0
        self.first_seq = None
        self._mono_last = None
        self.zeroed = True          # until the conductor releases the cues
        self.abort_id = 0
        self.sent = 0
        self.send_errors = 0
        self.last_values_nonzero = False
        self._fail_since = None     # clock when the current outage began
        self._fail_count = 0
        self._cue_problem = ""      # the current provider episode, if any
        # from flamesafe's status frames (note_status)
        self.lock_alarm = ""
        self._not_ours_since = None
        self._pending_abort = None  # (abort_id, sent at)
        self._abort_unconfirmed = False

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
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="ltcplay-flame-link")
            self._thread.start()
        return self

    def stop(self):
        """Stop sending. flamesafe then disarms every group within its own
        frame_stale_ms, exactly as if ltcplay had died."""
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(2.0)
        self._thread = None
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
        period = 1.0 / self.cfg.send_hz
        next_at = self._clock()
        while not self._stop.is_set():
            self.send_frame()
            next_at += period
            now = self._clock()
            if next_at < now - period:
                next_at = now           # fell behind: no burst to catch up
            delay = next_at - now
            if delay > 0:
                self._stop.wait(delay)

    # -- what goes in a frame --------------------------------------------------
    def values_now(self):
        """(tc, values) for a frame sent now. Never raises; zeros on
        anything uncertain."""
        zeros = bytes(UNIVERSE_SIZE)
        try:
            tc, live = self.show_state()
        except Exception as e:
            self._cue_episode(f"the show state could not be read "
                              f"({type(e).__name__}: {e})")
            return None, zeros
        if not (isinstance(tc, str) and _TC.match(tc)):
            tc = None
        if self.zeroed or live is not True or tc is None:
            self._cue_episode("")
            return tc, zeros
        try:
            v = self.cues(tc)
        except Exception as e:
            self._cue_episode(f"the flame cue provider raised "
                              f"{type(e).__name__}: {e}")
            return tc, zeros
        if v is None:
            self._cue_episode("")
            return tc, zeros
        try:
            vals = list(v)
            if len(vals) != UNIVERSE_SIZE or \
                    any(not _is_int(x) or not 0 <= x <= 255 for x in vals):
                raise ValueError
        except Exception:
            self._cue_episode(f"the flame cue provider did not answer "
                              f"{UNIVERSE_SIZE} whole numbers 0 to 255")
            return tc, zeros
        self._cue_episode("")
        return tc, bytes(vals)

    def _cue_episode(self, problem):
        if problem == self._cue_problem:
            return
        if problem:
            self._note(f"Flame link: {problem}, so flame cues are zero. "
                       f"This line will not repeat until it clears.",
                       fault=True, action="flame_link", outcome="cues_zero")
        else:
            self._note("Flame link: the flame cue values are readable "
                       "again.", action="flame_link", outcome="cues_back")
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
        """One flame frame, now. True when it went out."""
        with self._lock:
            tc, values = self.values_now()
            seq, mono = self._next()
            self.last_values_nonzero = any(values)
            return self._send(encode_flame(seq, tc, mono, self.cfg.universe,
                                           values, self.cfg.key))

    # -- the conductor's three calls -------------------------------------------
    def zero(self):
        """flames_zero: every cue value is zero from now until release(),
        and one zero frame goes out at once rather than at the next tick.
        True when that frame went out."""
        with self._lock:
            self.zeroed = True
            return self.send_frame()

    def release(self):
        """flames_release: cue values go out again from the next frame, and
        still only while the show is live (values_now). True if the link
        is open to carry them."""
        with self._lock:
            self.zeroed = False
            return self._sock is not None

    def disarm_all(self, reason):
        """The show's Abort: cues to zero, one zero frame, then
        DISARM_COPIES disarm_all datagrams for one new abort id. True when
        at least one disarm_all went out. Never raises."""
        try:
            why = " ".join(str(reason or "Abort").split())[:REASON_MAX] \
                or "Abort"
            with self._lock:
                self.zeroed = True
                self.send_frame()
                self.abort_id += 1
                aid = self.abort_id
                ok = False
                for _ in range(DISARM_COPIES):
                    seq, mono = self._next()
                    ok |= self._send(encode_disarm_all(seq, mono, aid, why,
                                                       self.cfg.key))
                if ok:
                    self._pending_abort = (aid, self._clock())
                    self._abort_unconfirmed = False
        except Exception as e:
            self._note(f"Flame link: the disarm could not be built "
                       f"({type(e).__name__}: {e}).", fault=True,
                       action="flame_link", outcome="disarm_failed")
            return False
        if ok:
            self._note(f"Flame link: {why}: disarm every flame group sent to "
                       f"flamesafe (abort {aid}). Each group needs a fresh "
                       f"arm cycle from the Stream Deck.",
                       action="flame_link", outcome="disarm_sent")
        else:
            self._note(f"Flame link: {why}: the disarm could NOT be sent to "
                       f"flamesafe. Flame cues are zero. Disarm with the "
                       f"Stream Deck's Abort or its group keys.", fault=True,
                       action="flame_link", outcome="disarm_failed")
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
                if _is_int(last) and last >= pend[0]:
                    self._pending_abort = None
                    if self._abort_unconfirmed:
                        self._note(f"Flame link: flamesafe confirmed the "
                                   f"disarm (abort {pend[0]}), late.",
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

    def snapshot(self):
        """For the page and the health panel."""
        with self._lock:
            return {"open": self._sock is not None,
                    "seq": self.seq, "sent": self.sent,
                    "send_errors": self.send_errors,
                    "sending_ok": self._fail_since is None,
                    "zeroed": self.zeroed,
                    "nonzero": self.last_values_nonzero,
                    "cue_problem": self._cue_problem,
                    "lock_alarm": self.lock_alarm,
                    "abort_id": self.abort_id,
                    "disarm_unconfirmed": self._abort_unconfirmed}
