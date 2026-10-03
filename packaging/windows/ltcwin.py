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
