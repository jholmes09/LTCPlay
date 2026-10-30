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
   person brought forward (a key, touch or click since it came to the
   front, or Open BEYOND pressed just before it came up), never when a
   MadMapper output window is on the rack screen's own display (the
   projection always wins), and a rack screen a person minimized stays
   minimized after the boot settle. At most MAX_RAISES raises per covering
   window; every raise and every refusal is logged. Geometry, not display
   numbers, keeps it off the projection (review A of #176): it never
   raises while any MadMapper output window overlaps the rack screen's
   own area, and an output window never counts as a cover. It takes the
   foreground only from the covering BEYOND or MadMapper window itself,
   and never within INPUT_QUIET_S of a key, touch or click; otherwise it
   only changes the window order, or waits. A minimized rack screen is
   left minimized (where it would come back is not known). The Startup page's Open
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
INPUT_QUIET_S = 5.0       # no foreground taken this soon after any input
FOREGROUND_IN_USE_S = 60.0   # the window in front, used this recently: kept
TASKBAR = ("Shell_TrayWnd", "Shell_SecondaryTrayWnd")
DIALOG_CLASS = "#32770"   # Windows' dialog box class (MessageBox and the like)
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


def rack_display(wins, mons, fallback=1):
    """THE rule for the rack screen's display, used alike by the lamp, the
    rig check, the supervisor's output alert (review B round 2 of #176):
    where the rack window is (not minimized), unless a MadMapper output
    window is on that same display (then that display is the projector's
    and the rack window only sits on it); else `fallback` (showpc.json
    page_monitor); else the main display."""
    outd = {n for _w, n in output_windows(wins, mons)}
    for w in wins or ():
        if is_rack(w) and not w.get("minimized"):
            n = monitor_of(inset(w["rect"]), mons)
            if n and n not in outd:
                return n
            break
    return fallback if isinstance(fallback, int) and \
        not isinstance(fallback, bool) and \
        1 <= fallback <= len(mons or ()) else 1


COVERED_SHARE = 0.25      # of the output's area hidden by windows above it


def covered_by(wins, out):
    """The first window above the output window `out` (Z order) that hides
    at least COVERED_SHARE of it, or None: the audience sees that window,
    not the show (review B round 2 of #176). The taskbar and small windows
    are below the share."""
    area = max(1, (out["rect"][2] - out["rect"][0]) *
               (out["rect"][3] - out["rect"][1]))
    for w in wins:
        if w is out:
            return None
        if w.get("minimized") or w.get("seethrough"):
            # A see-through overlay hides nothing (review A and B round 3).
            continue
        if overlap(inset(w["rect"], 8), out["rect"]) >= COVERED_SHARE * area:
            return w
    return None


def output_state(wins, mons, monitor=0, rack=None):
    """("on" | "off" | "unknown", sentence) for MadMapper's video output,
    from a snapshot() (top window first) and the displays. `monitor` n:
    only display n counts (a number past the displays this PC has is not
    known, and says so). `monitor` 0: any display EXCEPT the rack screen's
    (rack_display, with `rack`, showpc.json's page_monitor, as its
    fallback): a full-screen MadMapper window over the operator's own
    screen while the projector shows the desktop is not "on" (review B of
    #176, S1). The output counts only when no other window hides it (the
    rack screen or BEYOND above it on the projector). With one display
    only (or mirrored displays, which Windows reports as one), the
    projector is not detected: OFF, said plainly
    (madmapper_output_monitor 1 for a mirrored projector)."""
    if wins is None or not mons:
        return "unknown", ("not known: the show PC's window list could not "
                           "be read")
    if monitor and monitor > len(mons):
        return "unknown", (f"not known: madmapper_output_monitor is "
                           f"{monitor}, but this PC has {len(mons)} "
                           f"display{'s' if len(mons) != 1 else ''}; set it "
                           f"to the projector's display, or 0")
    mm = [w for w in wins if image_is(w.get("image"), MADMAPPER)]
    if not mm:
        return "off", ("MadMapper is not running (no MadMapper window), so "
                       "nothing of it is on the projectors")
    if not monitor and len(mons) == 1:
        return "off", ("MadMapper's video output is OFF: this PC has only "
                       "one display, so the projector is not connected or "
                       "not detected by Windows")
    outs = output_windows(wins, mons)
    if monitor:
        outs = [(w, n) for w, n in outs if n == monitor]
        where = f"display {monitor}"
    else:
        r = rack_display(wins, mons, fallback=rack or 1)
        outs = [(w, n) for w, n in outs if n != r]
        where = f"a display other than the rack screen's (display {r})"
    hidden = None
    for w, n in outs:
        c = covered_by(wins, w)
        if c is None:
            m = mons[n - 1]
            return "on", (f"MadMapper's output fills display {n} "
                          f"({m[2] - m[0]}x{m[3] - m[1]})")
        hidden = hidden or (n, c)
    if hidden is not None:
        n, c = hidden
        name = ntpath.basename(str(c.get("image") or "")) or "a window"
        return "off", (f"MadMapper's output on display {n} is hidden by "
                       f"another window in front of it (\"{c.get('title') or ''}"
                       f"\", {name}), so the audience sees that window")
    return "off", (f"MadMapper's video output is OFF: it has no full-screen "
                   f"output window on {where}, so the projectors show the "
                   f"Windows desktop")


def on_this_pc(host):
    """thispc.on_this_pc, the one answer the engine's watches use (#178:
    this PC's own interface addresses count, not only 127.x), so the
    supervisor and the engine can never disagree. thispc.py is stdlib
    only: the supervisor carries it as a module of its own."""
    try:
        from . import thispc as _thispc
    except ImportError:
        import thispc as _thispc
    return _thispc.on_this_pc(host)


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
                (self.u.GetClassNameW, ctypes.c_int,
                 [W.HWND, W.LPWSTR, ctypes.c_int]),
                (self.u.GetShellWindow, W.HWND, []),
                (self.k.GetProcessTimes, W.BOOL,
                 [W.HANDLE] + [ctypes.POINTER(W.FILETIME)] * 4),
                (self.k.GetSystemTimeAsFileTime, None,
                 [ctypes.POINTER(W.FILETIME)]),
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
        if self._cloaked(hwnd):
            # "Visible" to Windows but not drawn (a suspended app's
            # window): it hides nothing.
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
        ex = int(u.GetWindowLongW(hwnd, -20)) & 0xFFFFFFFF
        cbuf = c.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, cbuf, 256)
        frame = self._frame(hwnd)
        return {"hwnd": int(hwnd), "pid": pid, "thread": thread,
                "seethrough": self._seethrough(hwnd, ex),
                "cls": cbuf.value,
                "image": images[pid], "title": buf.value,
                # The visible frame (DWM), not the window's invisible
                # resize borders, where it can be read (review B round 4).
                "rect": frame or (rc.left, rc.top, rc.right, rc.bottom),
                "visible": True, "minimized": bool(u.IsIconic(hwnd)),
                "caption": (style & WS_CAPTION) == WS_CAPTION,
                "owner": int(u.GetWindow(hwnd, 4) or 0)}

    def _seethrough(self, hwnd, ex):
        """A window the eye sees through or clicks through (a game
        overlay): WS_EX_TRANSPARENT, or a layered window at zero alpha. It
        hides nothing. (An opaque topmost tool window does hide.)"""
        if ex & 0x20:                                # WS_EX_TRANSPARENT
            return True
        if ex & 0x80000:                             # WS_EX_LAYERED
            try:
                a = self._c.c_ubyte(255)
                f = self._w.DWORD(0)
                if self.u.GetLayeredWindowAttributes(
                        self._w.HWND(hwnd), None, self._c.byref(a),
                        self._c.byref(f)) and f.value & 0x2 and \
                        a.value == 0:
                    return True
            except (OSError, AttributeError):
                pass
        return False

    def _frame(self, hwnd):
        try:
            if not hasattr(self, "_dwm"):
                self._dwm = self._c.WinDLL("dwmapi")
            r = self._w.RECT()
            old = self._dpi()
            try:
                ok = self._dwm.DwmGetWindowAttribute(
                    self._w.HWND(hwnd), 9, self._c.byref(r),
                    self._c.sizeof(r)) == 0
            finally:
                self._undpi(old)
            if ok and r.right > r.left and r.bottom > r.top:
                return (r.left, r.top, r.right, r.bottom)
        except (OSError, AttributeError):
            pass
        return None

    def session_age_s(self):
        """Seconds since this sign-in's shell (explorer.exe, the desktop
        window's process) started, or None."""
        c, W = self._c, self._w
        self.u.GetShellWindow.restype = W.HWND
        hwnd = self.u.GetShellWindow()
        if not hwnd:
            return None
        pid = W.DWORD(0)
        self.u.GetWindowThreadProcessId(hwnd, c.byref(pid))
        h = self.k.OpenProcess(0x1000, False, pid.value)
        if not h:
            return None
        try:
            ct, et, kt, ut = (W.FILETIME() for _ in range(4))
            if not self.k.GetProcessTimes(h, c.byref(ct), c.byref(et),
                                          c.byref(kt), c.byref(ut)):
                return None
            now = W.FILETIME()
            self.k.GetSystemTimeAsFileTime(c.byref(now))

            def v(f):
                return (f.dwHighDateTime << 32) | f.dwLowDateTime
            return (v(now) - v(ct)) / 1e7
        finally:
            self.k.CloseHandle(h)

    def uptime_s(self):
        """Seconds since Windows started (GetTickCount64)."""
        f = self.k.GetTickCount64
        f.restype = self._c.c_ulonglong
        return f() / 1000.0

    def _cloaked(self, hwnd):
        try:
            if not hasattr(self, "_dwm"):
                self._dwm = self._c.WinDLL("dwmapi")
            v = self._c.c_int(0)
            if self._dwm.DwmGetWindowAttribute(
                    self._w.HWND(hwnd), 14, self._c.byref(v), 4) == 0:
                return bool(v.value)
        except (OSError, AttributeError):
            pass
        return False

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

    def raise_only(self, hwnd):
        """The rack screen to the top of the window order WITHOUT taking
        the foreground or the keyboard (SWP_NOACTIVATE, async)."""
        self.u.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                            0x0001 | 0x0002 | 0x0010 | 0x4000)

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
            # Never attached to a hung window's input (review B of #176):
            # the supervisor's own loop must not wait on it.
            other = int(u.GetWindowThreadProcessId(fg, None) or 0) \
                if fg and not u.IsHungAppWindow(fg) else 0
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


def look(api, monitor=0, rack=None):  # rack: page_monitor, the fallback
    """(state, sentence) of MadMapper's output, read now. Never raises."""
    try:
        mons = api.monitors()
    except Exception:                            # noqa: BLE001
        mons = []
    return output_state(snapshot(api), mons, monitor, rack)


class OutputLook:
    """MadMapper's output for the engine's screens: read every `every_s`
    on a thread of its own (never on a status request), and read fresh for
    the rig check (now()). `api_factory()` builds the Win32 layer; on a
    machine where it cannot, every answer is not known. Display only."""

    def __init__(self, api_factory=Win32, monitor=0, every_s=2.0,
                 stale_s=10.0, clock=time.monotonic, rack=1):
        self.monitor = int(monitor or 0)
        self.rack = rack            # page_monitor: the rack display's fallback
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
            look(api, self.monitor, self.rack)
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
    """True for the rack screen's Edge app window: an msedge.exe top-level
    window whose title is the rack page's, and, when `pid` is given (the
    Edge process the supervisor started), of that process."""
    if not image_is(w.get("image"), EDGE) or w.get("owner"):
        return False
    if pid is not None and w.get("pid") != pid:
        return False
    return bool(re.search(title_re, str(w.get("title") or "")))


def find_rack(wins, title_re=RACK_TITLE, pid=None):
    """The rack screen's window: the one of the supervisor's Edge process
    (`pid`) when there is one, else the first whose title is the rack
    page's (Edge may hand the window to a process already running)."""
    if pid is not None:
        got = next((w for w in wins if is_rack(w, title_re, pid)), None)
        if got is not None:
            return got
    return next((w for w in wins if is_rack(w, title_re)), None)


def inset(rect, px=16):
    """A maximized window reaches a few pixels past its display (its
    hidden borders); its real area is this much inside."""
    l, t, r, b = rect[:4]
    return (l + px, t + px, r - px, b - px)


def is_taskbar(w):
    return w.get("cls") in TASKBAR


def is_dialog(w):
    """A dialog box (Windows' dialog class, #32770: a MessageBox, a
    licence or missing-media prompt), owned or not."""
    return w.get("cls") == DIALOG_CLASS


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
                 wall=time.time, tick_s=2.0):
        self.tick_s = tick_s
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
        self._described = set()  # app windows already logged
        self._said = {}
        # The first look (review 2 of #176): what is open then was opened
        # before this keeper (LTC Player restarted), so it is old.
        self._looked = False

    def _say(self, key, text):
        if self._said.get(key) != text:
            self._said[key] = text
            if text:
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
        first = not self._looked
        self._looked = True
        for w in apps:
            # Open at the first look: old (LTC Player restarted; reviews 1
            # and 2 of #176). Outside the boot settle it is never raised
            # over for having "just appeared".
            self.seen.setdefault(w["hwnd"],
                                 float("-inf") if first else now)
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
            # The click or Alt+Tab that brought it forward came up to one
            # tick before this look saw it (review B round 2).
            if age is not None and now - age >= self.fg_since[fg] - \
                    (2 * self.tick_s + 1.0):
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
        rack = find_rack(wins, self.title_re, self.rack_pid)
        if rack is None:
            self._say("rack", "rack screen keeper: the rack screen's window "
                              "is not found (not open yet, or closed)")
            return "none"
        self._say("rack", "rack screen keeper: the rack screen's window is "
                          f"found (\"{rack.get('title', '')}\")")
        # The boot settle counts from the sign-in (the shell's start,
        # which Windows Fast Startup does not carry over, unlike its
        # uptime; reviews A and B round 4), so a later restart of LTC
        # Player never re-enters it; else from this keeper's start.
        try:
            sess = self.api.session_age_s()
        except Exception:                        # noqa: BLE001
            sess = None
        boot = (sess < self.boot_settle_s) \
            if isinstance(sess, (int, float)) and sess >= 0 \
            else (now - self.started < self.boot_settle_s)
        if rack["minimized"]:
            # Left minimized (review A of #176): where it would come back
            # is not known, and it must never land on the projection.
            self._say("minimized", "rack screen keeper: the rack screen is "
                                   "minimized; left as it is (open it from "
                                   "the taskbar)")
            return "none"
        if not mons:
            # The displays cannot be read: nothing is known about where
            # the projection is, so nothing is moved (review A round 2).
            self._say("nomons", "rack screen keeper: the displays cannot be "
                                "read; nothing is moved")
            return "left"
        self._say("nomons", "")
        area = inset(rack["rect"])
        # Every MadMapper window with no title bar may be its output (one
        # that does not fill a display exactly, during a resolution
        # change, too): never a cover, and never raised over (review A
        # round 2). If unsure, do not raise.
        outs = {w["hwnd"] for w, _n in output_windows(wins, mons)} | {
            w["hwnd"] for w in wins
            if image_is(w.get("image"), MADMAPPER) and not w.get("caption")}
        if any(overlap(w["rect"], area) > 0 for w in wins
               if w["hwnd"] in outs):
            self._say("output", "rack screen keeper: MadMapper's output "
                                "overlaps the rack screen, so the rack screen "
                                "is never raised over it")
            return "left"
        idx = wins.index(rack)
        # What is over the rack screen: not its own pop-ups (owned by it:
        # a raise brings them along), not the taskbar, not see-through
        # overlays.
        above = [w for w in wins[:idx] if not w.get("minimized")
                 and not w.get("seethrough") and not is_taskbar(w)
                 and w.get("owner") != rack["hwnd"]
                 and overlap(w["rect"], area) > 0]
        for w in above:
            if is_app(w) and w["hwnd"] not in self._described:
                # What BEYOND's and MadMapper's windows look like, once
                # each, for the bench (review B round 4).
                self._described.add(w["hwnd"])
                self.log(f"rack screen keeper: {app_name(w)} window "
                         f"\"{w.get('title', '')}\" class "
                         f"{w.get('cls', '?')}, owner {w.get('owner', 0)}, "
                         f"title bar {bool(w.get('caption'))}, at "
                         f"{tuple(w['rect'])}")
        # An owned window (a dialog: missing media, a licence, the
        # preferences) is never buried (review B of #176, S4), a dialog box
        # with no owner (a parentless MessageBox, class #32770) is never a
        # cover (reviews 1 and 2 of #176), and an output window is never a
        # cover (review A).
        cover = [w for w in wins[:idx] if is_app(w) and not w["minimized"]
                 and not w.get("owner") and not is_dialog(w)
                 and w["hwnd"] not in outs
                 and overlap(w["rect"], area) > 0
                 and w["hwnd"] not in self.chosen
                 and (boot or now - self.seen[w["hwnd"]] <=
                      self.app_settle_s)]
        if not cover:
            return "none"
        c = cover[0]
        # Never bury (reviews A and B round 4): a dialog of BEYOND or
        # MadMapper (owned, or another window of the covering program),
        # or the window a person has in front and used in the last
        # FOREGROUND_IN_USE_S. Other windows nobody is using do not stop
        # the rack screen coming back.
        fg_now = self.api.foreground()
        try:
            age = self.api.input_age_s()
        except Exception:                        # noqa: BLE001
            age = None
        why = ""
        for w in above:
            if w is c:
                if w["hwnd"] == fg_now and (age is None or (
                        age < FOREGROUND_IN_USE_S and
                        self.seen.get(w["hwnd"], now) <= now - age)):
                    # In front, and the last key, touch or click (in the
                    # last minute) came while this window was open: a
                    # person's, wherever it was in front then (reviews 2
                    # and B round 2 of #176: open before LTC Player
                    # restarted, or back in front after the lock screen,
                    # unlocked by Windows Hello with no key). A window that
                    # opened after the last input (BEYOND starting up after
                    # the sign-in typing) is not.
                    why = (f"{app_name(w)}'s window \"{w.get('title', '')}"
                           f"\" is in front and was used "
                           f"{'just now' if age is None else f'{age:.0f} s ago'}")
                    break
                continue
            if is_dialog(w) or (is_app(w) and (
                    w.get("owner") or w.get("pid") == c.get("pid"))):
                # Any program's dialog box (an Explorer or LTC Player
                # message box too; review B round 2 of #176) is never
                # buried.
                who = app_name(w) if is_app(w) else "a program"
                why = (f"{who}'s dialog \"{w.get('title', '')}\" is "
                       f"open over it")
                break
            if w["hwnd"] == fg_now and (age is None or
                                        age < FOREGROUND_IN_USE_S):
                why = (f"\"{w.get('title', '')}\" is in front and was used "
                       f"{'just now' if age is None else f'{age:.0f} s ago'}")
                break
        if why:
            self._say("dialog", f"rack screen keeper: {why}; the rack screen "
                                f"is not raised over it")
            return "left"
        self._say("dialog", "")
        n = self.raises.get(c["hwnd"], 0)
        if n >= MAX_RAISES:
            self._say(("gave up", c["hwnd"]),
                      f"rack screen keeper: {app_name(c)}'s window is still "
                      f"over the rack screen after {MAX_RAISES} tries; left "
                      f"as it is (is it set to stay on top?)")
            return "left"
        if age is None or age < INPUT_QUIET_S:
            # Someone is typing or touching (review A of #176): their keys
            # never land in the rack page. Tried again once it is quiet.
            self._say("busy", "rack screen keeper: waiting, a key, touch or "
                              "click was just made")
            return "waiting"
        self._say("busy", "")
        self.raises[c["hwnd"]] = n + 1
        # The foreground only from the covering window itself; anything
        # else a person has in front keeps the keyboard.
        take = self.api.foreground() == c["hwnd"]
        return self._raise(rack, c, "raised", take)

    def _raise(self, rack, cover, what, take):
        try:
            if self.api.hung(rack["hwnd"]):
                self._say("hung", "rack screen keeper: the rack screen is "
                                  "not responding; not moved")
                return "left"
            if take:
                ok = self.api.bring_to_front(rack["hwnd"],
                                             rack.get("thread"))
            else:
                self.api.raise_only(rack["hwnd"])
                ok = True
        except Exception as e:                   # noqa: BLE001
            ok = False
            self._say("raise error", f"rack screen keeper: could not raise "
                                     f"the rack screen ({type(e).__name__})")
        why = f"{app_name(cover)}'s window \"{cover.get('title', '')}\" " \
              f"covered it"
        self.log(f"rack screen keeper: {what} the rack screen ({why})"
                 + ("" if take else ", in the window order only (the "
                                    "keyboard stays where it is)")
                 + ("" if ok else "; Windows did not give it the "
                                  "foreground"))
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
    once per off spell (the engine's "output" alert, a kind only this
    watch raises). When the output comes on, the alert it raised is
    cleared again (`clear()`; review B of #176: a stale alert held a
    healthy show). `rack` is the rack screen's display (page_monitor),
    which never counts as the projector's with `monitor` 0. Only reads
    windows."""

    def __init__(self, api, monitor=0, log=None, alert=None, clear=None,
                 clock=time.monotonic, settle_s=180.0, debounce_s=10.0,
                 retry_s=30.0, rack=None, epoch=None, config=None):
        self.api = api
        # config() -> (check, monitor, why_no_alert), read on every look
        # (review B round 4: a changed ltcplay_fire_ice.json and Reload
        # engine apply without restarting LTC Player). None: as given.
        self.config = config
        self._config = None
        self.quiet = ""
        # epoch() -> the engine's start count: a new engine has lost the
        # alert, so it is raised again (review B round 2 of #176).
        self.epoch = epoch or (lambda: 0)
        self._epoch = None
        self.backoff = retry_s
        self.monitor = monitor
        self.rack = rack
        self.log = log or (lambda s: None)
        self.alert = alert or (lambda text: True)
        self.clear = clear or (lambda: True)
        self._clock = clock
        self.settle_s = settle_s
        self.debounce_s = debounce_s
        self.first_seen = None     # MadMapper first seen running
        self.off_since = None
        self.alerted = False       # this off spell's alert is accepted
        # An alert of ours may be open: true at start, so an alert a
        # supervisor before this one raised is cleared once the output is
        # on (a clear with nothing open counts as done).
        self.raised = True
        self.retry_s = retry_s     # an alert or clear the engine did not take
        self.next_try = 0.0
        self.state = None

    def tick(self):
        try:
            return self._tick()
        except Exception as e:                   # noqa: BLE001
            self.log(f"MadMapper output watch: {type(e).__name__}: {e}")
            return None

    def _call(self, fn, *a):
        try:
            return bool(fn(*a))
        except Exception:                        # noqa: BLE001
            return False

    def _fail(self, now):
        self.next_try = now + self.backoff
        self.backoff = min(self.backoff * 2, 600.0)

    def _ok(self):
        self.next_try = 0.0
        self.backoff = self.retry_s

    def _tick(self):
        now = self._clock()
        if self.config is not None:
            try:
                cfg = tuple(self.config())
            except Exception:                    # noqa: BLE001
                cfg = self._config
            if cfg is not None and cfg != self._config:
                chk, mon, quiet = cfg
                if self._config is not None:
                    self.log(f"MadMapper output watch: the settings changed: "
                             f"madmapper_output_check "
                             f"{'true' if chk else 'false'}, "
                             f"madmapper_output_monitor {mon}"
                             + (f"; no alert: {quiet}" if quiet else ""))
                self._config = cfg
                self.monitor, self.quiet = mon, quiet
            if self._config is not None and not self._config[0]:
                # Switched off: its alert goes, and nothing more.
                if self.raised and now >= self.next_try:
                    if self._call(self.clear):
                        self.raised = False
                        self._ok()
                    else:
                        self._fail(now)
                self.state = "switched off"
                return "switched off"
        try:
            ep = self.epoch()
        except Exception:                        # noqa: BLE001
            ep = None
        if ep != self._epoch:
            if self._epoch is not None:
                self.log("MadMapper output watch: the engine started again; "
                         "its alerts start empty")
                self.alerted = False
                self.raised = True
                self._ok()
            self._epoch = ep
        wins = snapshot(self.api)
        try:
            mons = self.api.monitors()
        except Exception:                        # noqa: BLE001
            mons = []
        state, why = output_state(wins, mons, self.monitor, self.rack)
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
            if self.raised and now >= self.next_try:
                if self._call(self.clear):
                    self.raised = False
                    self._ok()
                    self.log("MadMapper output: on, so any output alert is "
                             "cleared")
                else:
                    self._fail(now)
                    self.log(f"MadMapper output: on, but its alert could not "
                             f"be cleared yet; trying again in "
                             f"{self.next_try - now:g} s")
            return state
        if self.off_since is None:
            self.off_since = now
        due = (now - self.first_seen >= self.settle_s and
               now - self.off_since >= self.debounce_s)
        if self.state != "off" and due:
            self.log(f"MadMapper output: OFF. {why}. {ENABLE_STEPS}")
            self.state = "off"
        if due and not self.alerted and self.quiet:
            self.log(f"MadMapper output: no alert raised, {self.quiet}")
            self.alerted = True
        if due and not self.alerted and now >= self.next_try:
            if self.raised:
                # The last spell's alert is still open (its clear never
                # landed): cleared first, so this spell's alert is new,
                # with its own id and time (review A round 2).
                if not self._call(self.clear):
                    self._fail(now)
                    self.log(f"MadMapper output: the last output alert could "
                             f"not be cleared before a new one; trying again "
                             f"in {self.next_try - now:g} s")
                    return "off"
                self.raised = False
            self.alerted = self._call(self.alert, f"{why}. {ENABLE_STEPS}")
            if self.alerted:
                self.raised = True
                self._ok()
            else:
                self._fail(now)
                self.log(f"MadMapper output: the alert could not be raised "
                         f"yet; trying again in {self.next_try - now:g} s")
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
