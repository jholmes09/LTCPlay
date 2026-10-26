"""Open BEYOND: bring BEYOND's window to the front of the show PC (Jeff,
2026-10-26, the Startup page).

The Startup page's Open BEYOND button asks this module to find BEYOND's
window and put it in front of the rack screen, so the laser calibration can
be checked by eye in BEYOND itself. It only moves a window: it never sends
anything to BEYOND, to the lasers or to the rig, and it never starts BEYOND.

How BEYOND's window is found: every visible top-level window (EnumWindows)
whose process's program file matches BEYOND's (beyond*.exe, the same names
and the same lower-case file name match as beyondwatch.py). A window
owned by another window (a dialog, a tool window) is passed over when a
main window is there.

How it is brought forward: restored if it was minimized, then
SetForegroundWindow. Windows does not let a background program take the
foreground, so the usual workaround goes first: this thread is attached to
the input of the window that has the foreground now (AttachThreadInput)
and the Alt key is tapped, which releases Windows' foreground lock for the
next call. If Windows still refuses, BEYOND's taskbar button is flashed
(FlashWindowEx) and the answer says so plainly: click it.

The Win32 calls are behind a small object (`Win32` here, a fake in the
selftest), so every rule is tested on any machine.
"""
import fnmatch
import ntpath
import sys

from . import beyondwatch

IMAGES = beyondwatch.IMAGES

SW_RESTORE = 9
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002
GW_OWNER = 4
FLASHW_ALL = 0x3
FLASHW_TIMERNOFG = 0xC
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def is_beyond_image(path):
    """True when a program file (a full path or a bare name) is BEYOND's:
    its file name, lower case, matches beyondwatch.IMAGES."""
    name = ntpath.basename(str(path or "")).strip().lower()
    return bool(name) and any(fnmatch.fnmatch(name, p) for p in IMAGES)


class Win32:
    """The few user32 and kernel32 calls this module needs, through ctypes.
    Windows only: building one anywhere else raises OSError."""

    def __init__(self):
        if sys.platform != "win32":
            raise OSError("not Windows")
        import ctypes
        from ctypes import wintypes
        self._c = ctypes
        self._w = wintypes
        self.u = ctypes.windll.user32
        self.k = ctypes.windll.kernel32
        W = wintypes
        # Handles are pointer sized: said so, or 64-bit Windows could cut
        # one short.
        for fn, res, args in (
                (self.u.IsWindowVisible, W.BOOL, [W.HWND]),
                (self.u.GetWindow, W.HWND, [W.HWND, W.UINT]),
                (self.u.GetWindowTextW, ctypes.c_int,
                 [W.HWND, W.LPWSTR, ctypes.c_int]),
                (self.u.GetWindowThreadProcessId, W.DWORD,
                 [W.HWND, ctypes.POINTER(W.DWORD)]),
                (self.u.IsIconic, W.BOOL, [W.HWND]),
                (self.u.ShowWindow, W.BOOL, [W.HWND, ctypes.c_int]),
                (self.u.GetForegroundWindow, W.HWND, []),
                (self.u.AttachThreadInput, W.BOOL,
                 [W.DWORD, W.DWORD, W.BOOL]),
                (self.u.SetForegroundWindow, W.BOOL, [W.HWND]),
                (self.u.BringWindowToTop, W.BOOL, [W.HWND]),
                (self.k.OpenProcess, W.HANDLE, [W.DWORD, W.BOOL, W.DWORD]),
                (self.k.QueryFullProcessImageNameW, W.BOOL,
                 [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)]),
                (self.k.CloseHandle, W.BOOL, [W.HANDLE]),
                (self.k.GetCurrentThreadId, W.DWORD, [])):
            fn.restype = res
            fn.argtypes = args

    def windows(self):
        """Every top-level window handle, in Z order."""
        out = []
        proto = self._c.WINFUNCTYPE(self._w.BOOL, self._w.HWND,
                                    self._w.LPARAM)

        def each(hwnd, _lp):
            out.append(int(hwnd))
            return True
        self.u.EnumWindows(proto(each), 0)
        return out

    def visible(self, hwnd):
        return bool(self.u.IsWindowVisible(hwnd))

    def owner(self, hwnd):
        return int(self.u.GetWindow(hwnd, GW_OWNER) or 0)

    def title(self, hwnd):
        buf = self._c.create_unicode_buffer(512)
        self.u.GetWindowTextW(hwnd, buf, 512)
        return buf.value

    def pid(self, hwnd):
        pid = self._w.DWORD(0)
        self.u.GetWindowThreadProcessId(hwnd, self._c.byref(pid))
        return int(pid.value)

    def thread(self, hwnd):
        return int(self.u.GetWindowThreadProcessId(hwnd, None) or 0)

    def image(self, pid):
        """The process's program file, or "" when it cannot be read."""
        h = self.k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
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

    def minimized(self, hwnd):
        return bool(self.u.IsIconic(hwnd))

    def restore(self, hwnd):
        self.u.ShowWindow(hwnd, SW_RESTORE)

    def foreground(self):
        return int(self.u.GetForegroundWindow() or 0)

    def current_thread(self):
        return int(self.k.GetCurrentThreadId())

    def attach(self, me, other, on):
        return bool(self.u.AttachThreadInput(me, other, bool(on)))

    def alt_tap(self):
        self.u.keybd_event(VK_MENU, 0, 0, 0)
        self.u.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)

    def set_foreground(self, hwnd):
        return bool(self.u.SetForegroundWindow(hwnd))

    def bring_to_top(self, hwnd):
        self.u.BringWindowToTop(hwnd)

    def flash(self, hwnd):
        class FLASHWINFO(self._c.Structure):
            _fields_ = [("cbSize", self._w.UINT), ("hwnd", self._w.HWND),
                        ("dwFlags", self._w.DWORD),
                        ("uCount", self._w.UINT),
                        ("dwTimeout", self._w.DWORD)]
        fi = FLASHWINFO(self._c.sizeof(FLASHWINFO), hwnd,
                        FLASHW_ALL | FLASHW_TIMERNOFG, 0, 0)
        self.u.FlashWindowEx(self._c.byref(fi))
        return True


def find_beyond_window(api):
    """The handle of BEYOND's main window, or None: the first visible
    top-level window (in Z order) whose process is BEYOND's, a window that
    has no owner before one that has. Never raises."""
    images = {}
    best = None
    try:
        hwnds = api.windows()
    except Exception:                            # noqa: BLE001
        return None
    for h in hwnds:
        try:
            if not api.visible(h):
                continue
            pid = api.pid(h)
            if pid not in images:
                images[pid] = api.image(pid)
            if not is_beyond_image(images[pid]):
                continue
            if not api.owner(h):
                return h
            if best is None:
                best = h
        except Exception:                        # noqa: BLE001
            continue
    return best


def bring_to_front(api, hwnd):
    """(True, "") when `hwnd` is the foreground window afterwards, else
    (False, why). Restores a minimized window first. Always lets go of an
    input attach it made. Never raises."""
    try:
        if api.minimized(hwnd):
            api.restore(hwnd)
    except Exception:                            # noqa: BLE001
        pass
    attached = None
    try:
        fg = api.foreground()
        me = api.current_thread()
        other = api.thread(fg) if fg else 0
        if other and other != me and api.attach(me, other, True):
            attached = (me, other)
        api.alt_tap()
        api.set_foreground(hwnd)
        api.bring_to_top(hwnd)
    except Exception as e:                       # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    finally:
        if attached is not None:
            try:
                api.attach(attached[0], attached[1], False)
            except Exception:                    # noqa: BLE001
                pass
    try:
        if api.foreground() == hwnd:
            return True, ""
    except Exception:                            # noqa: BLE001
        pass
    return False, "Windows would not let it come to the front"


def open_beyond(host=None, windows=None, api=None, probe=None):
    """(ok, sentence). Brings BEYOND's window to the front of this PC.
    `host` is BEYOND's OSC host from the settings (None: not set, BEYOND
    is looked for here). `windows` is whether this is Windows (default:
    this PC's own answer). `api` is the Win32 layer (default: Win32()).
    `probe` says whether BEYOND is running at all, for the sentence when
    no window is found (default: beyondwatch.windows_probe). Sends nothing
    to BEYOND, ever. Never raises."""
    if windows is None:
        windows = sys.platform == "win32"
    if not windows:
        return False, ("Open BEYOND works only on the Windows show PC, "
                       "where BEYOND runs. Nothing was done.")
    if host is not None and not beyondwatch.on_this_pc(host):
        return False, (f"BEYOND is set up on another machine ({host}), so "
                       f"its window cannot be brought forward from this PC. "
                       f"Open it on that machine. Nothing was done.")
    if api is None:
        try:
            api = Win32()
        except Exception as e:                   # noqa: BLE001
            return False, (f"The window controls of this PC could not be "
                           f"reached ({type(e).__name__}). Open BEYOND "
                           f"from the taskbar. Nothing was done.")
    hwnd = find_beyond_window(api)
    if hwnd is None:
        running = (probe or beyondwatch.windows_probe)()
        if running is True:
            return False, ("BEYOND is running but has no window that can "
                           "be shown. Open it from the taskbar or the "
                           "notification area.")
        if running is False:
            return False, ("BEYOND is not running on this PC. Start "
                           "BEYOND first, then press Open BEYOND again.")
        return False, ("No BEYOND window was found on this PC. If BEYOND "
                       "is running, open it from the taskbar.")
    ok, why = bring_to_front(api, hwnd)
    if ok:
        return True, "BEYOND is in front on the show PC."
    flashed = False
    try:
        flashed = bool(api.flash(hwnd))
    except Exception:                            # noqa: BLE001
        flashed = False
    if flashed:
        return False, (f"{why}, so BEYOND's button on the taskbar is "
                       f"flashing. Click it to open BEYOND.")
    return False, (f"{why}. Click BEYOND on the taskbar to open it.")
