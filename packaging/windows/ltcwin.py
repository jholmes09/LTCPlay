"""Shared by the Windows app's four entry points (packaging/windows/entry_*.py
and supervisor.py). Nothing in ltcplay/ or flamesafe/ imports this, and this
imports neither of them at module level: flamesafe.exe is built without the
ltcplay package in it at all.
"""
import json
import os
import sys

APP = "LTC Player"
WINDOWS = sys.platform == "win32"

# Console control events (wincon.h).
CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1
CTRL_CLOSE_EVENT = 2
CTRL_LOGOFF_EVENT = 5
CTRL_SHUTDOWN_EVENT = 6


def frozen():
    return bool(getattr(sys, "frozen", False))


def app_dir():
    """The folder holding the four .exe files (or this file, from source)."""
    if frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def internal_dir():
    """Where ltcplay.version looks for VERSION: one above the package, which
    in the built app is PyInstaller's _internal folder."""
    if frozen():
        return getattr(sys, "_MEIPASS", app_dir())
    return os.path.dirname(os.path.dirname(app_dir()))


def release():
    """The VERSION stamp written by the build (stamp_version.py), or {}."""
    try:
        with open(os.path.join(internal_dir(), "VERSION"),
                  encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def version_line(program):
    rel = release()
    name = rel.get("release") or "unreleased"
    build = rel.get("build")
    return f"{program} {name}" + (f" (build {build})" if build else "")


def prepare_stdio():
    """When the supervisor starts a program its output goes to a log file.
    Python would then pick the Windows code page and buffer by the block:
    one character outside it crashes a print, and a crash loses the last
    lines. UTF-8, line by line, whatever it is attached to."""
    for name in ("stdout", "stderr"):
        s = getattr(sys, name, None)
        if s is None:
            continue
        try:
            s.reconfigure(encoding="utf-8", errors="replace",
                          line_buffering=True)
        except (AttributeError, ValueError, OSError):
            pass


_KEEP = []


def clean_stop_on_logoff():
    """Turn Windows' logoff, shutdown and console-close events into the same
    Ctrl-Break the program already treats as a clean stop.

    Without this, a Windows Update restart or a sign-out ends the process
    with nothing run: for flamesafe that is no safe zeros, for the engine no
    blackout. Windows gives a console program about five seconds after one
    of these events; this hands the program its own stop signal and then
    waits, so the main thread has that time to finish. Ctrl-C and
    Ctrl-Break themselves are passed straight on, untouched."""
    if not WINDOWS:
        return
    import ctypes
    import signal
    import time
    from ctypes import wintypes

    HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    def handler(event):
        if event in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
            try:
                signal.raise_signal(signal.SIGBREAK)
            except Exception:
                return False
            time.sleep(4.5)
            return True
        return False

    h = HANDLER(handler)
    _KEEP.append(h)
    ctypes.windll.kernel32.SetConsoleCtrlHandler(h, True)


def package_name():
    """The full name of the MSIX package this process runs inside (another
    app's container, e.g. the Claude desktop app's shell), or "" when it
    runs as itself. Inside one, AppData is redirected into that app's own
    folder, so settings, locks and journals would land where a normally
    started LTC Player never looks (show PC, 2026-10-04)."""
    if not WINDOWS:
        return ""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32")
        n = wintypes.UINT(0)
        rc = k32.GetCurrentPackageFullName(ctypes.byref(n), None)
        if rc == 15700:                # APPMODEL_ERROR_NO_PACKAGE
            return ""
        buf = ctypes.create_unicode_buffer(max(n.value, 1))
        rc = k32.GetCurrentPackageFullName(ctypes.byref(n), buf)
        return buf.value if rc == 0 else "an unnamed package"
    except Exception:
        local = os.environ.get("LOCALAPPDATA", "")
        return "a package" if "\\Packages\\" in local else ""


CONTAINER_REFUSAL = (
    "LTC Player was started from inside another app's container ({pkg}). "
    "Windows would put its settings and files in that app's own folder, "
    "where LTC Player started from the Start menu never looks. Start LTC "
    "Player from the Start menu instead. Nothing was started.")


def settings_folder_line():
    """Which folder this process's settings and journals really go to."""
    base = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    pkg = package_name()
    if not pkg:
        return f"settings and journals: {os.path.join(base, 'ltcplay')}"
    family = pkg.split("_")[0]
    return (f"settings and journals: {os.path.join(base, 'ltcplay')}, which "
            f"Windows REDIRECTS into the {pkg} container (under "
            f"{os.path.join(base, 'Packages')}\\{family}*\\LocalCache"
            f"\\Local\\ltcplay)")


def keep_time(above_normal=True):
    """Windows only: ask Windows to keep this process on time.

    The show programs run with no window, and Windows 11 treats a windowless
    process as background work: it may run it on the efficiency cores at a
    low clock and coalesce its timers ("power throttling", EcoQoS). The show
    PC's first soak saw a flame frame 71 ms late with nothing else to do
    (2026-10-04). So:
      - power throttling is turned off for this process, both the
        execution-speed part and the timer-resolution part;
      - the system timer is asked for 1 ms (timeBeginPeriod), so every wait
        in the process wakes within a millisecond, not 15.6;
      - the priority class is Above normal (above_normal=True), so a busy
        browser or Windows' own work never stands in front of a frame.
    Returns one sentence per step, saying whether it took. Never raises."""
    if not WINDOWS:
        return []
    out = []
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        # The pseudo-handle is -1, which comes back as a 64-bit unsigned int;
        # passed bare it overflows ctypes' default int (windows-latest,
        # 2026-10-04). Typed as a HANDLE it goes through whole.
        proc = wintypes.HANDLE(k32.GetCurrentProcess())
        k32.SetProcessInformation.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                              wintypes.LPVOID, wintypes.DWORD)
        k32.SetProcessInformation.restype = wintypes.BOOL
        k32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k32.SetPriorityClass.restype = wintypes.BOOL

        class _State(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG),
                        ("ControlMask", wintypes.ULONG),
                        ("StateMask", wintypes.ULONG)]
        # PROCESS_POWER_THROTTLING_EXECUTION_SPEED | ..._IGNORE_TIMER_RESOLUTION,
        # with StateMask 0: both turned OFF. ProcessPowerThrottling = 4.
        st = _State(1, 0x1 | 0x4, 0)
        ok = k32.SetProcessInformation(proc, 4, ctypes.byref(st),
                                       ctypes.sizeof(st))
        out.append("power throttling off" if ok else
                   f"power throttling NOT turned off (error "
                   f"{ctypes.get_last_error()})")
        rc = ctypes.WinDLL("winmm").timeBeginPeriod(1)
        out.append("timer 1 ms" if rc == 0 else
                   f"timer NOT set to 1 ms (timeBeginPeriod returned {rc})")
        if above_normal == "high":
            # Show PC, 2026-10-04: MadMapper decoding six 1080p videos on
            # the CPU starved the engine for 0.4 s at a time. High, never Realtime; third-party programs are never
            # touched.
            ok = k32.SetPriorityClass(proc, 0x80)     # HIGH
            out.append("priority High" if ok else
                       f"priority NOT raised to High (error "
                       f"{ctypes.get_last_error()})")
        elif above_normal:
            ok = k32.SetPriorityClass(proc, 0x8000)   # ABOVE_NORMAL
            out.append("priority Above normal" if ok else
                       f"priority NOT raised (error "
                       f"{ctypes.get_last_error()})")
    except Exception as e:
        out.append(f"could not ask Windows to keep time: "
                   f"{type(e).__name__}: {e}")
    return out


PRIORITY_ENV = "LTCPLAY_PRIORITY"      # "high": set by the supervisor


def boosted():
    """True when the supervisor (or the bench soak) asked for the
    scheduling protection (showpc.json "priority_boost", on by default)."""
    return os.environ.get(PRIORITY_ENV) == "high"


def thread_priority(native_id, level, k32=None):
    """Set one thread's priority (by its native id; 2 is Highest). True
    when Windows took it."""
    if not WINDOWS:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        k32 = k32 or ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenThread.restype = wintypes.HANDLE
        k32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL,
                                   wintypes.DWORD)
        k32.SetThreadPriority.argtypes = (wintypes.HANDLE, ctypes.c_int)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        h = k32.OpenThread(0x0020 | 0x0040, False, native_id)  # SET/QUERY
        if not h:
            return False
        try:
            return bool(k32.SetThreadPriority(h, level))
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def boost_threads(names, level=2, log=print, every_s=0.5, setter=None,
                  threads=None, stop=None):
    """A daemon thread that raises every thread named in `names` (as it
    appears, and again whenever a new one with that name starts) to `level`
    (2, Highest), logging each once. Fails soft."""
    import threading as _t
    setter = setter or thread_priority
    threads = threads or _t.enumerate
    done = set()
    halt = stop or _t.Event()

    def run():
        while not halt.is_set():
            for th in threads():
                nid = getattr(th, "native_id", None)
                if th.name in names and nid and nid not in done:
                    done.add(nid)
                    ok = setter(nid, level)
                    log(f"thread {th.name} (id {nid}): priority "
                        + ("Highest" if ok else "NOT raised (Windows "
                           "refused)"))
            if halt.wait(every_s):
                return
    th = _t.Thread(target=run, daemon=True, name="ltcwin-boost")
    th.start()
    return th


def say_keep_time(program, above_normal=True):
    """keep_time(), and one line on stdout (the program's log) saying what
    took, for the soak report to read. With the scheduling protection on
    (boosted()), the engine and flamesafe go to High and the deck to Above
    normal."""
    if boosted():
        above_normal = "high" if above_normal else True
    got = keep_time(above_normal)
    if got:
        print(f"{program}: Windows timekeeping: {', '.join(got)}",
              flush=True)
    if WINDOWS:
        print(f"{program}: {settings_folder_line()}", flush=True)
    return got


def common_flags(program, argv, self_check, emit=print):
    """Handle --version and --self-check. Returns an exit code, or None when
    neither was asked for and the program should run normally. `emit` is
    print for the console programs; LTC Player.exe has no console and
    writes these lines to its log instead."""
    if argv[:1] == ["--version"]:
        emit(version_line(program))
        return 0
    if argv[:1] == ["--self-check"]:
        emit(version_line(program))
        try:
            for line in self_check():
                emit(f"  ok  {line}")
        except Exception as e:
            emit(f"SELF-CHECK FAILED: {type(e).__name__}: {e}")
            return 1
        emit("self-check passed")
        return 0
    return None


def spawn_probe():
    """The child half of the engine's self-check: proves a spawned process
    starts inside the built app (the show audio runs in one)."""
    return None
