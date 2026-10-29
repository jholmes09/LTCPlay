"""The show PC's windows after a reboot (Jeff's field report, 2026-10-29):
"on reboot, MadMapper opens to the right project but video output is
turned off and BEYOND was in front of your rack screen. You need to make
sure you turn on video output or we end up with the Windows background on
our projection."

Two things, both read from Windows' own window list. Nothing here sends a
key, a click or a message to MadMapper or BEYOND, and nothing reaches the
rig.

1. MadMapper's video output: on, OFF or not known.
   MadMapper answers nothing over OSC (madmapper.py, bench B2.4), so the
   output cannot be asked about. What can be seen is its output window:
   with the output on, MadMapper shows a full-screen window on the
   projector display. "On" here means a visible, not minimized, top-level
   window of a MadMapper*.exe process with no title bar (WS_CAPTION) that
   covers a whole display (to EDGE_TOL pixels), on the display numbered
   `monitor` when one is set (1 the main display, 2 the next one Windows
   lists, as showpc.json's page_monitor). MadMapper running with no such
   window: OFF, and the projectors show the Windows desktop. The window
   list unreadable, or not Windows: not known.
   Turning the output on is a person's job, by hand, in MadMapper (no
   documented OSC message or command line for it could be verified; see
   ENABLE_STEPS). The supervisor watches for it after MadMapper starts and
   raises a video alert when it stays off (supervisor.OutputWatch); the
   rig check before every show fails closed on it (preroll
   check_madmapper_output); the rack screen's lamps say it.

2. The rack screen in front (RackKeeper, run by the supervisor). After a
   reboot BEYOND's and MadMapper's windows came up over the rack screen.
   The keeper raises the rack screen (the Edge app window the supervisor
   opened) when a BEYOND or MadMapper window covers it on its display:
   any such window during the boot settle (BOOT_SETTLE_S after the
   supervisor started), and afterwards only a window that appeared in the
   last APP_SETTLE_S (an app started or restarted). Never over a window a
   person brought forward (Open BEYOND, a taskbar click: a key, touch or
   click since it came to the front, or just before), never when a
   MadMapper output window is on the rack screen's own display (the
   projection always wins), and a rack screen a person minimized stays
   minimized after the boot settle. At most MAX_RAISES raises per covering
   window; every raise and every refusal is logged. The Startup page's Open
   BEYOND writes OPENED_FILE in the settings folder, and a BEYOND window
   that came forward after it counts as a person's choice.

Stdlib and ctypes only, and no import of the rest of ltcplay: the
supervisor (packaging/windows/supervisor.py, which never bundles the
ltcplay package) imports this file as a module of its own, as it does
audit.py. The Win32 calls are behind `Win32` (a fake in the selftest), so
every rule is tested on any machine.
"""
import fnmatch
import ntpath
import re
import sys
import threading
import time

MADMAPPER = ("madmapper*.exe",)     # madmapperwatch.IMAGES
BEYOND = ("beyond*.exe",)           # beyondwatch.IMAGES
EDGE = ("msedge.exe",)
# The rack screen page's own title: "<show> remote" (remote.html's
# loadBrand) or "Show remote 2.0" (remote2.html), as an Edge app window
# shows it.
RACK_TITLE = r"(?i)\bremote( 2\.0)?( - [^-]*Edge)?$"
EDGE_TOL = 2              # px: a window this close to a display's edges fills it
WS_CAPTION = 0x00C00000
BOOT_SETTLE_S = 300.0     # after the supervisor starts: any cover is undone
APP_SETTLE_S = 60.0       # after a BEYOND/MadMapper window appears
OPENED_S = 10.0           # an Open BEYOND press this long before it came up
MAX_RAISES = 5            # per covering window, then said once and left
OPENED_FILE = "beyond-opened"   # written by the engine's Open BEYOND

# What a person does in MadMapper to turn the output on, for the alert,
# the rig check and the docs. From MadMapper's own documentation (the PR
# names the pages); the labels can differ between MadMapper versions.
ENABLE_STEPS = ("In MadMapper on the show PC, turn the projector output on: "
                "Output menu, Fullscreen (or the Outputs tab, Ctrl+5, set "
                "the projector output to Fullscreen). So it comes back by "
                "itself after a reboot: MadMapper Preferences, Misc, tick "
                "Start fullscreen when opening a project at startup.")


def stamp_opened(folder):
    """Open BEYOND was pressed (remote.Remote.open_beyond): the keeper
    leaves BEYOND in front. Never raises."""
    try:
        import os
        with open(os.path.join(folder, OPENED_FILE), "w",
                  encoding="utf-8") as fh:
            fh.write(f"{time.time():.3f}\n")
    except OSError:
        pass


def opened_at(folder):
    """time.time() of the last Open BEYOND press, or None."""
    try:
        import os
        return os.path.getmtime(os.path.join(folder, OPENED_FILE))
    except OSError:
        return None


def image_is(path, globs):
    """True when a program file (a full path or a bare name) matches one
    of `globs`, lower case."""
    name = ntpath.basename(str(path or "")).strip().lower()
    return bool(name) and any(fnmatch.fnmatch(name, g) for g in globs)


def covers(rect, mon, tol=EDGE_TOL):
    """True when `rect` covers the whole display rectangle `mon`."""
    l, t, r, b = rect[:4]
    ml, mt, mr, mb = mon[:4]
    return l <= ml + tol and t <= mt + tol and r >= mr - tol and \
        b >= mb - tol


def overlap(a, b):
    """The area two rectangles share."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0


def monitor_of(rect, mons):
    """The 1-based number of the display holding `rect`'s centre, or 0."""
    cx = (rect[0] + rect[2]) / 2.0
    cy = (rect[1] + rect[3]) / 2.0
    for i, m in enumerate(mons):
        if m[0] <= cx < m[2] and m[1] <= cy < m[3]:
            return i + 1
    return 0


def output_windows(wins, mons):
    """[(window, display number)] of every MadMapper output window: a
    visible, not minimized MadMapper window with no title bar that fills a
    whole display."""
    out = []
    for w in wins or ():
        if not image_is(w.get("image"), MADMAPPER) or w.get("minimized") \
                or not w.get("visible", True):
            continue
        if w.get("caption"):
            continue
        for i, m in enumerate(mons or ()):
            if covers(w["rect"], m):
                out.append((w, i + 1))
                break
    return out


def output_state(wins, mons, monitor=0):
    """("on" | "off" | "unknown", sentence) for MadMapper's video output,
    from a snapshot() and the displays. `monitor` 0: any display; n: only
    display n counts."""
    if wins is None or not mons:
        return "unknown", ("not known: the show PC's window list could not "
                           "be read")
    mm = [w for w in wins if image_is(w.get("image"), MADMAPPER)]
    if not mm:
        return "off", ("MadMapper is not running (no MadMapper window), so "
                       "nothing of it is on the projectors")
    outs = output_windows(wins, mons)
    if monitor:
        outs = [(w, n) for w, n in outs if n == monitor]
    if outs:
        w, n = outs[0]
        m = mons[n - 1]
        return "on", (f"MadMapper's output fills display {n} "
                      f"({m[2] - m[0]}x{m[3] - m[1]})")
    where = f"display {monitor}" if monitor else "any display"
    return "off", (f"MadMapper's video output is OFF: it has no full-screen "
                   f"output window on {where}, so the projectors show the "
                   f"Windows desktop")


class Win32:
    """The user32 and kernel32 calls this module needs, through ctypes.
    Windows only: building one anywhere else raises OSError."""

    def __init__(self):
        if sys.platform != "win32":
            raise OSError("not Windows")
        import ctypes
        from ctypes import wintypes as W
        self._c = ctypes
        self._w = W
        self.u = ctypes.WinDLL("user32")
        self.k = ctypes.WinDLL("kernel32")
        for fn, res, args in (
                (self.u.IsWindowVisible, W.BOOL, [W.HWND]),
                (self.u.IsIconic, W.BOOL, [W.HWND]),
                (self.u.GetWindow, W.HWND, [W.HWND, W.UINT]),
                (self.u.GetWindowTextW, ctypes.c_int,
                 [W.HWND, W.LPWSTR, ctypes.c_int]),
                (self.u.GetWindowThreadProcessId, W.DWORD,
                 [W.HWND, ctypes.POINTER(W.DWORD)]),
                (self.u.GetWindowRect, W.BOOL,
                 [W.HWND, ctypes.POINTER(W.RECT)]),
                (self.u.GetWindowLongW, ctypes.c_long, [W.HWND, ctypes.c_int]),
                (self.u.GetForegroundWindow, W.HWND, []),
                (self.u.SetForegroundWindow, W.BOOL, [W.HWND]),
                (self.u.AttachThreadInput, W.BOOL,
                 [W.DWORD, W.DWORD, W.BOOL]),
                (self.u.ShowWindowAsync, W.BOOL, [W.HWND, ctypes.c_int]),
                (self.u.SetWindowPos, W.BOOL,
                 [W.HWND, W.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                  ctypes.c_int, W.UINT]),
                (self.u.IsHungAppWindow, W.BOOL, [W.HWND]),
                (self.k.OpenProcess, W.HANDLE, [W.DWORD, W.BOOL, W.DWORD]),
                (self.k.QueryFullProcessImageNameW, W.BOOL,
                 [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)]),
                (self.k.CloseHandle, W.BOOL, [W.HANDLE]),
                (self.k.GetCurrentThreadId, W.DWORD, []),
                (self.k.GetTickCount, W.DWORD, [])):
            fn.restype = res
            fn.argtypes = args
        try:
            # Real pixels, whatever this process's own DPI setting, so the
            # window and display rectangles agree.
            self.u.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            self.u.SetThreadDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
            self._aware = True
        except AttributeError:
            self._aware = False

    def _dpi(self):
        if not self._aware:
            return None
        return self.u.SetThreadDpiAwarenessContext(self._c.c_void_p(-4))

    def _undpi(self, old):
        if old:
            self.u.SetThreadDpiAwarenessContext(self._c.c_void_p(old))

    def windows(self):
        """Every top-level window handle, in Z order (top first)."""
        out = []
        proto = self._c.WINFUNCTYPE(self._w.BOOL, self._w.HWND,
                                    self._w.LPARAM)

        def each(hwnd, _lp):
            out.append(int(hwnd))
            return True
        self.u.EnumWindows(proto(each), 0)
        return out

    def info(self, hwnd, images):
        """A window as snapshot() lists it, or None when it is not a
        visible top-level window."""
        u, c = self.u, self._c
        if not u.IsWindowVisible(hwnd):
            return None
        pid = self._w.DWORD(0)
        thread = int(u.GetWindowThreadProcessId(hwnd, c.byref(pid)) or 0)
        pid = int(pid.value)
        if pid not in images:
            images[pid] = self.image(pid)
        rc = self._w.RECT()
        old = self._dpi()
        try:
            u.GetWindowRect(hwnd, c.byref(rc))
        finally:
            self._undpi(old)
        buf = c.create_unicode_buffer(512)
        u.GetWindowTextW(hwnd, buf, 512)
        style = int(u.GetWindowLongW(hwnd, -16)) & 0xFFFFFFFF
        return {"hwnd": int(hwnd), "pid": pid, "thread": thread,
                "image": images[pid], "title": buf.value,
                "rect": (rc.left, rc.top, rc.right, rc.bottom),
                "visible": True, "minimized": bool(u.IsIconic(hwnd)),
                "caption": (style & WS_CAPTION) == WS_CAPTION,
                "owner": int(u.GetWindow(hwnd, 4) or 0)}

    def image(self, pid):
        h = self.k.OpenProcess(0x1000, False, pid)
        if not h:
            return ""
        try:
            buf = self._c.create_unicode_buffer(1024)
            size = self._w.DWORD(1024)
            if not self.k.QueryFullProcessImageNameW(
                    h, 0, buf, self._c.byref(size)):
                return ""
            return buf.value
        finally:
            self.k.CloseHandle(h)

    def monitors(self):
        """[(left, top, right, bottom)] of every whole display in real
        pixels, the main one first, then as Windows lists them by place
        (supervisor.monitors' order, which page_monitor counts in)."""
        c, W = self._c, self._w
        found = []

        class MONITORINFO(c.Structure):
            _fields_ = [("cbSize", W.DWORD), ("rcMonitor", W.RECT),
                        ("rcWork", W.RECT), ("dwFlags", W.DWORD)]
        self.u.GetMonitorInfoW.argtypes = (W.HMONITOR,
                                           c.POINTER(MONITORINFO))
        self.u.GetMonitorInfoW.restype = W.BOOL
        proto = c.WINFUNCTYPE(W.BOOL, W.HMONITOR, W.HDC, c.POINTER(W.RECT),
                              W.LPARAM)

        def cb(hmon, _hdc, _rc, _lp):
            mi = MONITORINFO()
            mi.cbSize = c.sizeof(MONITORINFO)
            if self.u.GetMonitorInfoW(hmon, c.byref(mi)):
                r = mi.rcMonitor
                found.append(((r.left, r.top, r.right, r.bottom),
                              bool(mi.dwFlags & 1)))
            return True
        old = self._dpi()
        try:
            self.u.EnumDisplayMonitors(None, None, proto(cb), 0)
        finally:
            self._undpi(old)
        found.sort(key=lambda m: (not m[1], m[0][0], m[0][1]))
        return [r for r, _main in found]

    def foreground(self):
        return int(self.u.GetForegroundWindow() or 0)

    def input_age_s(self):
        """Seconds since the last key, touch or click on this PC, or None."""
        class LASTINPUTINFO(self._c.Structure):
            _fields_ = [("cbSize", self._w.UINT), ("dwTime", self._w.DWORD)]
        li = LASTINPUTINFO(self._c.sizeof(LASTINPUTINFO), 0)
        if not self.u.GetLastInputInfo(self._c.byref(li)):
            return None
        return ((int(self.k.GetTickCount()) - int(li.dwTime)) & 0xFFFFFFFF) \
            / 1000.0

    def hung(self, hwnd):
        return bool(self.u.IsHungAppWindow(hwnd))

    def bring_to_front(self, hwnd, thread):
        """The rack screen to the top of the Z order and the foreground,
        as windowfront.bring_to_front does for Open BEYOND: restored if
        minimized (async), attached to the foreground's input for the one
        call and always let go, raised with SWP_ASYNCWINDOWPOS. No key is
        pressed. True when it is the foreground afterwards."""
        u = self.u
        if u.IsIconic(hwnd):
            u.ShowWindowAsync(hwnd, 9)            # SW_RESTORE
        attached = None
        try:
            fg = int(u.GetForegroundWindow() or 0)
            me = int(self.k.GetCurrentThreadId())
            other = int(u.GetWindowThreadProcessId(fg, None) or 0) \
                if fg else 0
            if other and other != me and u.AttachThreadInput(me, other, True):
                attached = (me, other)
            u.SetForegroundWindow(hwnd)
            u.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x4000)
        finally:
            if attached is not None:
                u.AttachThreadInput(attached[0], attached[1], False)
        return int(u.GetForegroundWindow() or 0) == hwnd


def snapshot(api):
    """Every visible top-level window, top first, as dicts (hwnd, pid,
    image, title, rect, minimized, caption, owner), or None when the list
    cannot be read. Never raises."""
    try:
        hwnds = api.windows()
    except Exception:                            # noqa: BLE001
        return None
    images = {}
    out = []
    for h in hwnds:
        try:
            w = api.info(h, images)
        except Exception:                        # noqa: BLE001
            continue
        if w is not None:
            out.append(w)
    return out


def look(api, monitor=0):
    """(state, sentence) of MadMapper's output, read now. Never raises."""
    try:
        mons = api.monitors()
    except Exception:                            # noqa: BLE001
        mons = []
    return output_state(snapshot(api), mons, monitor)


class OutputLook:
    """MadMapper's output for the engine's screens: read every `every_s`
    on a thread of its own (never on a status request), and read fresh for
    the rig check (now()). `api_factory()` builds the Win32 layer; on a
    machine where it cannot, every answer is not known. Display only."""

    def __init__(self, api_factory=Win32, monitor=0, every_s=2.0,
                 stale_s=10.0, clock=time.monotonic):
        self.monitor = int(monitor or 0)
        self.every_s = every_s
        self.stale_s = stale_s
        self._clock = clock
        self._factory = api_factory
        self._api = None
        self._why = ""
        self._last = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def _get_api(self):
        if self._api is None:
            try:
                self._api = self._factory()
            except Exception as e:               # noqa: BLE001
                self._why = f"not known: {type(e).__name__}: {e}"
                return None
        return self._api

    def now(self):
        api = self._get_api()
        got = ("unknown", self._why or "not known") if api is None else \
            look(api, self.monitor)
        with self._lock:
            self._last = (got, self._clock())
        return got

    def state(self):
        """The last reading, or not known when it is older than stale_s."""
        with self._lock:
            last = self._last
        if last is None or self._clock() - last[1] > self.stale_s:
            return "unknown", "not known: not read yet"
        return last[0]

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="ltcplay-mm-output")
            self._thread.start()
        return self

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.now()
            except Exception:                    # noqa: BLE001
                pass
            self._stop.wait(self.every_s)

    def stop(self):
        self._stop.set()


def is_rack(w, title_re=RACK_TITLE, pid=None):
    """True for the rack screen's Edge app window: an msedge.exe window
    whose title is the rack page's, or any window of the Edge process the
    supervisor started (`pid`)."""
    if not image_is(w.get("image"), EDGE) or w.get("owner"):
        return False
    if pid is not None and w.get("pid") == pid:
        return True
    return bool(re.search(title_re, str(w.get("title") or "")))


def is_app(w):
    return image_is(w.get("image"), MADMAPPER) or \
        image_is(w.get("image"), BEYOND)


def app_name(w):
    return "MadMapper" if image_is(w.get("image"), MADMAPPER) else "BEYOND"


class RackKeeper:
    """The rack screen kept in front of BEYOND and MadMapper (see the
    module docstring). `api` the Win32 layer; `monitor` the rack screen's
    display number (showpc.json page_monitor); `log(sentence)`; `clock`
    seconds. tick() once every couple of seconds; it never raises."""

    def __init__(self, api, monitor=1, log=None, clock=time.monotonic,
                 boot_settle_s=BOOT_SETTLE_S, app_settle_s=APP_SETTLE_S,
                 title_re=RACK_TITLE, rack_pid=None, opened=None,
                 wall=time.time):
        self.api = api
        # opened() -> wall time of the last Open BEYOND press, or None.
        self.opened = opened or (lambda: None)
        self._wall = wall
        self.monitor = monitor
        self.log = log or (lambda s: None)
        self._clock = clock
        self.started = clock()
        self.boot_settle_s = boot_settle_s
        self.app_settle_s = app_settle_s
        self.title_re = title_re
        self.rack_pid = rack_pid
        self.seen = {}           # hwnd -> when it was first seen (apps)
        self.fg_since = {}       # hwnd -> when it became the foreground
        self.chosen = set()      # app windows a person brought forward
        self.raises = {}         # covering hwnd -> raises for it
        self._said = {}

    def _say(self, key, text):
        if self._said.get(key) != text:
            self._said[key] = text
            self.log(text)

    def tick(self):
        """One look; returns what it did ("raised", "restored", "left",
        "none") for the tests. Never raises."""
        try:
            return self._tick()
        except Exception as e:                   # noqa: BLE001
            self._say("error", f"rack screen keeper: {type(e).__name__}: "
                               f"{e}; carrying on")
            return "none"

    def _tick(self):
        now = self._clock()
        wins = snapshot(self.api)
        if wins is None:
            return "none"
        try:
            mons = self.api.monitors()
        except Exception:                        # noqa: BLE001
            mons = []
        apps = [w for w in wins if is_app(w)]
        alive = {w["hwnd"] for w in apps}
        for h in list(self.seen):
            if h not in alive:
                self.seen.pop(h, None)
                self.fg_since.pop(h, None)
                self.chosen.discard(h)
                self.raises.pop(h, None)
        for w in apps:
            self.seen.setdefault(w["hwnd"], now)
        fg = self.api.foreground()
        if fg in alive and fg not in self.chosen:
            self.fg_since.setdefault(fg, now)
            try:
                age = self.api.input_age_s()
            except Exception:                    # noqa: BLE001
                age = None
            try:
                pressed = self.opened()
            except Exception:                    # noqa: BLE001
                pressed = None
            # Seconds ago it came forward, on the wall clock.
            fwd = self._wall() - (now - self.fg_since[fg])
            why = ""
            if age is not None and now - age >= self.fg_since[fg]:
                why = "a key, touch or click since it came to the front"
            elif pressed is not None and app_name_of(apps, fg) == \
                    "BEYOND" and pressed >= fwd - OPENED_S:
                why = "Open BEYOND was pressed"
            if why:
                self.chosen.add(fg)
                self.log(f"rack screen keeper: {app_name_of(apps, fg)} is in "
                         f"front by a person's choice ({why}); it is left "
                         f"there")
        for h in list(self.fg_since):
            if h != fg:
                self.fg_since.pop(h, None)
        rack = next((w for w in wins if is_rack(w, self.title_re,
                                                self.rack_pid)), None)
        if rack is None:
            return "none"
        boot = now - self.started < self.boot_settle_s
        mon = mons[self.monitor - 1] if isinstance(self.monitor, int) and \
            1 <= self.monitor <= len(mons) else (mons[0] if mons else None)
        if mon is not None:
            on_mine = [w for w, n in output_windows(wins, mons)
                       if mons[n - 1] == mon]
            if on_mine:
                self._say("output", "rack screen keeper: MadMapper's output "
                                    "is on the rack screen's own display, so "
                                    "the rack screen is not raised over it")
                return "left"
        if rack["minimized"]:
            if not boot:
                return "none"       # a person minimized it
            self._say("restore", "rack screen keeper: the rack screen was "
                                 "minimized during start up; restored")
            return self._raise(rack, None, "restored")
        area = rack["rect"]
        idx = wins.index(rack)
        cover = [w for w in wins[:idx] if is_app(w) and not w["minimized"]
                 and overlap(w["rect"], area) > 0
                 and w["hwnd"] not in self.chosen
                 and (boot or now - self.seen[w["hwnd"]] <=
                      self.app_settle_s)]
        if not cover:
            return "none"
        c = cover[0]
        n = self.raises.get(c["hwnd"], 0)
        if n >= MAX_RAISES:
            self._say(("gave up", c["hwnd"]),
                      f"rack screen keeper: {app_name(c)}'s window is still "
                      f"over the rack screen after {MAX_RAISES} tries; left "
                      f"as it is (is it set to stay on top?)")
            return "left"
        self.raises[c["hwnd"]] = n + 1
        return self._raise(rack, c, "raised")

    def _raise(self, rack, cover, what):
        try:
            if self.api.hung(rack["hwnd"]):
                self._say("hung", "rack screen keeper: the rack screen is "
                                  "not responding; not moved")
                return "left"
            ok = self.api.bring_to_front(rack["hwnd"], rack.get("thread"))
        except Exception as e:                   # noqa: BLE001
            ok = False
            self._say("raise error", f"rack screen keeper: could not raise "
                                     f"the rack screen ({type(e).__name__})")
        why = (f"{app_name(cover)}'s window \"{cover.get('title', '')}\" "
               f"covered it") if cover else "it was minimized"
        self.log(f"rack screen keeper: {what} the rack screen ({why})"
                 + ("" if ok else "; Windows did not give it the "
                                  "foreground, raised in the window order "
                                  "only"))
        return what


def app_name_of(apps, hwnd):
    for w in apps:
        if w["hwnd"] == hwnd:
            return app_name(w)
    return "a window"


class OutputWatch:
    """The supervisor's MadMapper output watch: once MadMapper is seen
    running, its output is given `settle_s` to come on (the project loads,
    and MadMapper's own "Start fullscreen" preference puts it up). Still
    off then, or off again later for `debounce_s`: logged and `alert(text)`
    once per off spell (the engine's video alert). Logged when it comes on.
    Only reads windows."""

    def __init__(self, api, monitor=0, log=None, alert=None,
                 clock=time.monotonic, settle_s=120.0, debounce_s=10.0,
                 retry_s=30.0):
        self.api = api
        self.monitor = monitor
        self.log = log or (lambda s: None)
        self.alert = alert or (lambda text: True)
        self._clock = clock
        self.settle_s = settle_s
        self.debounce_s = debounce_s
        self.first_seen = None     # MadMapper first seen running
        self.off_since = None
        self.alerted = False       # this off spell's alert is accepted
        self.retry_s = retry_s     # an alert the engine did not take
        self.next_alert = 0.0
        self.state = None

    def tick(self):
        try:
            return self._tick()
        except Exception as e:                   # noqa: BLE001
            self.log(f"MadMapper output watch: {type(e).__name__}: {e}")
            return None

    def _tick(self):
        now = self._clock()
        wins = snapshot(self.api)
        try:
            mons = self.api.monitors()
        except Exception:                        # noqa: BLE001
            mons = []
        state, why = output_state(wins, mons, self.monitor)
        if state == "unknown":
            if self.state != "unknown":
                self.log(f"MadMapper output: {why}")
            self.state = "unknown"
            return state
        running = any(image_is(w.get("image"), MADMAPPER) for w in wins)
        if not running:
            if self.state != "not running":
                self.log("MadMapper output: MadMapper is not running")
            self.first_seen = None
            self.off_since = None
            self.alerted = False
            self.state = "not running"
            return "not running"
        if self.first_seen is None:
            self.first_seen = now
            self.log(f"MadMapper output: MadMapper is running; its video "
                     f"output is watched (up to {self.settle_s:g} s for it "
                     f"to come on)")
        if state == "on":
            if self.state != "on":
                self.log(f"MadMapper output: on. {why}")
            self.state = "on"
            self.off_since = None
            self.alerted = False
            self.next_alert = 0.0
            return state
        if self.off_since is None:
            self.off_since = now
        due = (now - self.first_seen >= self.settle_s and
               now - self.off_since >= self.debounce_s)
        if self.state != "off" and due:
            self.log(f"MadMapper output: OFF. {why}. {ENABLE_STEPS}")
            self.state = "off"
        if due and not self.alerted and now >= self.next_alert:
            self.next_alert = now + self.retry_s
            try:
                self.alerted = bool(self.alert(f"{why}. {ENABLE_STEPS}"))
            except Exception:                    # noqa: BLE001
                self.alerted = False
        return "off" if due else "waiting"


def self_test():
    """Pure decisions on a made-up window list (the supervisor's
    --self-check: this file is bundled and its rules hold). Yields lines;
    raises RuntimeError on a wrong answer."""
    mons = [(0, 0, 1920, 1080), (1920, 0, 3840, 1080)]
    mm_ui = {"hwnd": 1, "image": r"C:\MadMapper\MadMapper.exe",
             "title": "MadMapper", "rect": (-8, -8, 1928, 1088),
             "minimized": False, "caption": True, "owner": 0}
    out = {"hwnd": 2, "image": "MadMapper.exe", "title": "",
           "rect": (1920, 0, 3840, 1080), "minimized": False,
           "caption": False, "owner": 0}
    if output_state([mm_ui], mons)[0] != "off" or \
            output_state([mm_ui, out], mons)[0] != "on" or \
            output_state([mm_ui, out], mons, monitor=1)[0] != "off" or \
            output_state([], mons)[0] != "off" or \
            output_state(None, mons)[0] != "unknown":
        raise RuntimeError("MadMapper's output is read wrongly")
    yield ("MadMapper output: a full-screen MadMapper window with no title "
           "bar on a display is on; none is OFF")
    if sys.platform == "win32":
        try:
            api = Win32()
            got = snapshot(api)
            n = len(api.monitors())
        except Exception as e:                   # noqa: BLE001
            raise RuntimeError(f"the window list cannot be read: "
                               f"{type(e).__name__}: {e}")
        if got is None:
            raise RuntimeError("the window list cannot be read")
        yield f"the window list reads: {len(got)} windows, {n} displays"
