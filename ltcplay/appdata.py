"""Where this machine's own data goes on Windows: the lock, the saved input,
the preferences and the show log.

On a Mac this module decides nothing. Every caller keeps its Mac path exactly
as it was and asks here only when `WINDOWS` is true. On Windows that data lives
under %LOCALAPPDATA%\\ltcplay: never beside the program, which may sit in a
folder the operator cannot write to, and never in a synced folder, where two
machines would share one lock and one idea of what is plugged in.
"""
import os
import sys

WINDOWS = sys.platform == "win32"
NAME = "ltcplay"


def folder():
    """%LOCALAPPDATA%\\ltcplay, created if it is missing.

    LOCALAPPDATA is set for every interactive and scheduled logon. The
    fallback is where Windows puts it anyway, for a process started with a
    stripped environment."""
    base = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    d = os.path.join(base, NAME)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        # The caller's own open() then fails with a sentence it already
        # knows how to say. Failing here would say it twice.
        pass
    return d


def log_path():
    """The default show log on Windows."""
    return os.path.join(folder(), "logs", "ltcplay.log")
