"""Is BEYOND still running? The rack screen's Lasers lamp asks (Jeff,
2026-10-23: BEYOND exited at 03:27 and the Lasers lamp said "lit" for
hours).

BEYOND sends nothing back over OSC, ever (beyond.py: bench B8, its own OSC
Monitor only ever showed what ltcplay sent). So nothing here can confirm
that the lasers are lit. What can be told, on the show PC, is whether the
BEYOND program is running at all: Windows' own process list, read on a
slow beat on this module's own thread (the soak's check, soak_apps.py,
without psutil). That is a LINK check, never a laser check:

  "up"       BEYOND's process was seen in the last STALE_S. Commands can
             reach it; whether the lasers are lit is still only what was
             sent ("lit, unconfirmed" on the lamp).
  "lost"     the last fresh look found no BEYOND process. Nothing sent to
             it lands: the lamp says LINK LOST, and an alert is raised once
             per loss.
  "unknown"  it cannot be told: not Windows, BEYOND on another machine
             (its OSC host is not this PC's loopback), the process list
             could not be read, or no look in the last STALE_S.

Display only. Nothing here sends anything to BEYOND or the rig, and
nothing here can blank, unblank, arm or disarm a laser.
"""
import fnmatch
import ipaddress
import subprocess
import sys
import threading
import time

INTERVAL_S = 3.0      # one look at the process list every this long
STALE_S = 10.0        # a look older than this says nothing any more
IMAGES = ("beyond*.exe",)   # soak_apps.APPS["BEYOND"]["exe"], lower case


def on_this_pc(host):
    """True when BEYOND's OSC host is this PC's loopback (the show PC's
    127.0.0.2, or localhost): only then can its process be looked for
    here. False for anything else, including no host."""
    h = str(host or "").strip().lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.IPv4Address(h).is_loopback
    except (ipaddress.AddressValueError, ValueError):
        return False


def windows_probe(run=subprocess.run):
    """True when a BEYOND*.exe process is in Windows' process list, False
    when the list was read and has none, None when it could not be read.
    Never raises."""
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
    """The probe for a BEYOND whose OSC host is `host`: windows_probe on
    the show PC with BEYOND on its loopback, else one that cannot tell."""
    if windows is None:
        windows = sys.platform == "win32"
    if windows and on_this_pc(host):
        return windows_probe
    return lambda: None


class BeyondWatch:
    """`probe()` -> True (running), False (not running), None (cannot
    tell). `on_change(link, why)` is called on this module's thread each
    time the link word changes. `clock` is injected for the tests."""

    def __init__(self, probe, clock=time.monotonic, interval_s=INTERVAL_S,
                 stale_s=STALE_S, on_change=None):
        self._probe = probe
        self._clock = clock
        self.interval_s = interval_s
        self.stale_s = stale_s
        self.on_change = on_change
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
                    "why": "BEYOND has not been looked for yet"}
        got, at = last
        age = self._clock() - at
        if age > self.stale_s:
            return {"link": "unknown", "age_s": round(age, 1),
                    "why": f"BEYOND was last looked for {age:.0f} s ago"}
        if got is True:
            return {"link": "up", "age_s": round(age, 1),
                    "why": "BEYOND is running on this PC"}
        if got is False:
            return {"link": "lost", "age_s": round(age, 1),
                    "why": "BEYOND is not running on this PC"}
        return {"link": "unknown", "age_s": round(age, 1),
                "why": "whether BEYOND is running cannot be told from "
                       "here"}

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
                                        name="ltcplay-beyond-watch")
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
