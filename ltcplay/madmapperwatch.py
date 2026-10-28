"""Is MadMapper running? The rack screen's Video lamp asks (Jeff,
2026-10-26: the Video lamp read "not known" until a show had sent
something, and he wants the lamps to say the rig is ready BEFORE a show
plays).

MadMapper answers nothing over OSC, ever (madmapper.py, bench B2.4). So
nothing here can confirm that the screens show the picture. What can be
told, on the show PC, is whether the MadMapper program is running at all:
Windows' own process list, read on a slow beat on this module's own thread,
exactly as the BEYOND watch does (beyondwatch.py, copied rather than
shared so the GPL path, which never loads anything BEYOND, stays so; the
soak's check, soak_apps.APPS["MadMapper"], names the image
MadMapper*.exe, which also matches the demo's MadMapperDemo.exe). That is
a LINK check, never a picture check:

  "up"       MadMapper's process was seen in the last STALE_S.
  "lost"     the last fresh look found no MadMapper process: the lamp says
             NOT RUNNING, and an alert is raised once per loss.
  "unknown"  it cannot be told: not Windows, MadMapper on another machine
             (its OSC host is not this PC's loopback), the process list
             could not be read, or no look in the last STALE_S.

The heartbeat. When the show file has a "madmapper.heartbeat" block,
madmapper.py's Watchdog already listens for MadMapper's heartbeat OSC
track; heartbeat_view() reads how long ago any packet arrived. MadMapper
sends it only while a show's video is moving (bench B3, B9): silence
between shows, on Hold or on the intermission bank is normal, so silence
is only worth a word while a show is playing. No heartbeat block: it is
never mentioned.

Display only. Nothing here sends anything to MadMapper or the rig, pings
anything, or changes any output.
"""
import fnmatch
import ipaddress
import subprocess
import sys
import threading
import time

INTERVAL_S = 3.0      # one look at the process list every this long
STALE_S = 10.0        # a look older than this says nothing any more
IMAGES = ("madmapper*.exe",)  # soak_apps.APPS["MadMapper"]["exe"], lower case


def on_this_pc(host):
    """True when MadMapper's OSC host is this PC's loopback (127.0.0.1, or
    localhost): only then can its process be looked for here. False for
    anything else, including no host."""
    h = str(host or "").strip().lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.IPv4Address(h).is_loopback
    except (ipaddress.AddressValueError, ValueError):
        return False


def windows_probe(run=subprocess.run):
    """True when a MadMapper*.exe process is in Windows' process list,
    False when the list was read and has none, None when it could not be
    read. Never raises."""
    try:
        out = run(["tasklist", "/fo", "csv", "/nh"], capture_output=True,
                  text=True, timeout=10).stdout
    except Exception:                            # noqa: BLE001
        return None
    if not isinstance(out, str) or not out.strip():
        return None
    for ln in out.splitlines():
        name = ln.strip().split('","')[0].strip('"').lower()
        if any(fnmatch.fnmatch(name, p) for p in IMAGES):
            return True
    return False


def probe_for(host, windows=None):
    """The probe for a MadMapper whose OSC host is `host`: windows_probe on
    the show PC with MadMapper on its loopback, else one that cannot
    tell."""
    if windows is None:
        windows = sys.platform == "win32"
    if windows and on_this_pc(host):
        return windows_probe
    return lambda: None


def unknown_why(host, windows=None):
    """Why the watch cannot tell, in a sentence, for a probe_for(host)
    that cannot: MadMapper on another machine, or not the Windows show
    PC. None when it can look."""
    if windows is None:
        windows = sys.platform == "win32"
    if not on_this_pc(host):
        return (f"unknown, MadMapper is on another machine (its OSC host "
                f"is {host}), so whether it is running cannot be told "
                f"from here")
    if not windows:
        return ("unknown, this is not the Windows show PC, so whether "
                "MadMapper is running cannot be told from here")
    return None


def watch(host, windows=None, **kw):
    """A MadMapperWatch for a MadMapper whose OSC host is `host`."""
    return MadMapperWatch(probe_for(host, windows),
                          unknown_why=unknown_why(host, windows), **kw)


class MadMapperWatch:
    """`probe()` -> True (running), False (not running), None (cannot
    tell). `on_change(link, why)` is called on this module's thread each
    time the link word changes. `clock` is injected for the tests;
    `unknown_why` is the sentence for a probe that cannot tell."""

    def __init__(self, probe, clock=time.monotonic, interval_s=INTERVAL_S,
                 stale_s=STALE_S, on_change=None, unknown_why=None):
        self._probe = probe
        self._clock = clock
        self.interval_s = interval_s
        self.stale_s = stale_s
        self.on_change = on_change
        self.unknown_why = unknown_why or (
            "whether MadMapper is running cannot be told from here")
        self._last = None          # (result, clock()) of the last look
        self._said = None          # the link word last handed on_change
        self._stop = threading.Event()
        self._thread = None

    def check_once(self):
        try:
            got = self._probe()
        except Exception:                        # noqa: BLE001
            got = None
        if got not in (True, False):
            got = None
        self._last = (got, self._clock())
        self._tell()
        return got

    def state(self):
        """{"link": "up" | "lost" | "unknown", "why": sentence,
        "age_s": seconds since the last look or None}. Never raises."""
        last = self._last
        if last is None:
            return {"link": "unknown", "age_s": None,
                    "why": "MadMapper has not been looked for yet"}
        got, at = last
        age = self._clock() - at
        if age > self.stale_s:
            return {"link": "unknown", "age_s": round(age, 1),
                    "why": f"MadMapper was last looked for {age:.0f} s ago"}
        if got is True:
            return {"link": "up", "age_s": round(age, 1),
                    "why": "MadMapper is running on this PC"}
        if got is False:
            return {"link": "lost", "age_s": round(age, 1),
                    "why": "MadMapper is not running on this PC"}
        return {"link": "unknown", "age_s": round(age, 1),
                "why": self.unknown_why}

    def _tell(self):
        st = self.state()
        if st["link"] != self._said:
            self._said = st["link"]
            cb = self.on_change
            if cb is not None:
                try:
                    cb(st["link"], st["why"])
                except Exception:                # noqa: BLE001
                    pass

    def start(self):
        if self._thread is not None:
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="ltcplay-madmapper-watch")
        self._thread.start()
        return self

    def _loop(self):
        while not self._stop.is_set():
            self.check_once()
            self._stop.wait(self.interval_s)

    def stop(self):
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout=2.0)


# The heartbeat track is told to the operator as wrong when its measured
# length is further than this from the show's real length.
TRACK_TOLERANCE_S = 0.5


def _secs(x):
    return f"{round(float(x), 2):g} s"


def show_reading(session):
    """(the show's real length in seconds, ltcplay's position in the show's
    own cue while it is moving) from the running session's show audio
    clock (clock.AudioMaster), each None when it cannot be told: no
    session, another kind of clock, nothing moving. Never raises."""
    try:
        clk = getattr(session, "clock", None)
        tl = getattr(session, "tl", None)
        if clk is None or tl is None or \
                getattr(clk, "source", None) != "audio_master":
            return None, None
        # Already loaded: this is an audio_master clock.
        from . import clock as clock_mod
        length = clock_mod.show_len_of(clk, tl)
        cue = getattr(clk, "_cue", None)
        pos = None
        if cue and not clk.paused and \
                getattr(clk, "_mode", None) in ("follow", "return",
                                                "freerun", "tail") and \
                cue.get("label") == clock_mod.show_cue_of(clk, tl):
            p = clk._pos(clk._clock())
            pos = p if p is not None and p >= 0 else None
        return length, pos
    except Exception:                            # noqa: BLE001
        return None, None


def track_view(watchdog, cfg):
    """(sentence, wrong) about MadMapper's heartbeat track length (Jeff's
    field report, 2026-10-28, item 14): what it must end at (the show's
    real length, Watchdog.show_len_s) and, once measured, what it ends at
    now. `wrong` is False, or the lamp's few words for a measured track
    that does not end at the show's end (the heartbeat's value would
    then read wrong by the end of the show; the watchdog's drift alarm
    is not armed by anything in this build, so no drift alert is raised,
    and this lamp is how it is told). Never raises."""
    try:
        need = watchdog.show_len_s()
        got = watchdog.track_len_s()
    except Exception:                            # noqa: BLE001
        return None, False
    if not need:
        return None, False
    addr = getattr(cfg, "address", None) or "/float-1"
    if got is not None and abs(got - need) > TRACK_TOLERANCE_S:
        return (f"set MadMapper's {addr} heartbeat track to end at "
                f"{_secs(need)}, the show's real length; it ends at "
                f"{_secs(got)} now"), \
            f"set {addr} to end at {_secs(need)}, it ends at {_secs(got)}"
    if got is not None:
        return (f"MadMapper's {addr} heartbeat track ends at {_secs(got)}, "
                f"the show's real length"), False
    said = getattr(cfg, "show_len_s", None)
    tail = ""
    if said and abs(float(said) - need) > 0.005:
        tail = (f" (ltcplay_fire_ice.json says {_secs(said)}; the heartbeat "
                f"is read against {_secs(need)})")
    return (f"MadMapper's {addr} heartbeat track must end at "
            f"{_secs(need)}, the show's real length{tail}"), False


def heartbeat_view(watchdog, cfg, playing, session=None):
    """What the rack screen says about MadMapper's heartbeat, or None when
    there is nothing to say: no watchdog, or no "madmapper.heartbeat"
    block in the show file (then it is never mentioned, never an alarm).

    {"heartbeat": "ok", "why"}      a packet within cfg.timeout_s.
    {"heartbeat": "silent", "why"}  none, while a show is `playing` (its
                                    video should be moving), or the
                                    listener could not open its port.
    {"heartbeat": "quiet", "why"}   none, and no show playing: normal.

    Each also has "track" (track_view's sentence, or None) and
    "track_wrong". `session` is the running session, or None: its show
    length is the one the heartbeat is read against, and its moving
    position is what measures MadMapper's track (show_reading).
    Never raises."""
    try:
        if watchdog is None or cfg is None or \
                getattr(cfg, "show_len_s", None) is None:
            return None
        try:
            length, pos = show_reading(session)
            watchdog.follow_show_length(length)
            watchdog.note_position(pos)
        except Exception:                        # noqa: BLE001
            pass
        out = _heartbeat_word(watchdog, cfg, playing)
        out["track"], out["track_wrong"] = track_view(watchdog, cfg)
        return out
    except Exception as e:                       # noqa: BLE001
        return {"heartbeat": "quiet",
                "why": f"the heartbeat could not be read: "
                       f"{type(e).__name__}", "track": None,
                "track_wrong": False}


def _heartbeat_word(watchdog, cfg, playing):
    try:
        err = getattr(watchdog, "bind_error", None)
        if err:
            return {"heartbeat": "silent", "why": str(err)}
        age = watchdog.heard_age_s()
        limit = float(getattr(cfg, "timeout_s", 3.0) or 3.0)
        if age is not None and 0 <= age <= limit:
            return {"heartbeat": "ok",
                    "why": f"MadMapper's heartbeat arrived {age:.1f} s ago"}
        if playing:
            return {"heartbeat": "silent",
                    "why": (f"a show is playing and MadMapper's heartbeat "
                            f"has not arrived for more than {limit:.0f} s: "
                            f"its video may be frozen, or its heartbeat "
                            f"track is not sending")}
        return {"heartbeat": "quiet",
                "why": "MadMapper sends its heartbeat only while a show's "
                       "video is moving"}
    except Exception as e:                       # noqa: BLE001
        return {"heartbeat": "quiet",
                "why": f"the heartbeat could not be read: "
                       f"{type(e).__name__}"}
