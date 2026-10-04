"""One sender on the rig at a time, across processes.

`Run ltcplay.command` and `Web ltcplay.command` are two double-clicks one
keypress apart, and nothing stopped an operator having both. Two players on
the same universes alternate frame by frame at 40fps each; on the rig that
reads as a hardware fault, and the obvious response (restart something) makes
it worse. The in-process guard in web.Control only ever covered one process.

Implemented as an exclusive flock on a file beside the launcher. A flock is
released by the kernel when the holder dies, so a crash or a force-quit does
not leave a stale lock the operator has to know about -- which a PID file
would.

Windows has no flock. There it is a byte-range lock through msvcrt.locking
on the same file, which Windows also drops when the holder's handle closes,
including when the process is killed. Same guarantee, same stale-lock
immunity, same holder note in the file.
"""
import errno
import os
import sys

from . import appdata

WINDOWS = sys.platform == "win32"

# Windows byte-range locks are mandatory: nobody else can READ locked bytes.
# The holder note at the start of the file has to stay readable, because the
# refusal names who is holding the rig. So the lock sits on one byte far past
# anything ever written. Locking past the end of a file is allowed and does
# not grow it.
_WIN_LOCK_AT = 1 << 30

FILENAME = "ltcplay_output.lock"

# Windows: every lock is also a named kernel mutex (show PC, 2026-10-04).
# A copy started from inside another app's MSIX container (the Claude
# desktop app's shell) has its AppData redirected into that container, so
# its lock FILE is a different file and the two copies never saw each
# other. Named kernel objects are not redirected: every process on the
# machine sees the same name. The mutex exists for as long as some process
# holds a handle to it, and Windows closes that handle when the process
# ends, however it ends.
NAMED = WINDOWS


def _win_create_named(name):
    """(handle, already_existed) for the named mutex `name`; Global\ first,
    Local\ (this logon session) if Global\ is refused. None when neither
    can be made (the file lock still guards)."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL,
                                 wintypes.LPCWSTR)
    for ns in ("Global\\", "Local\\"):
        h = k32.CreateMutexW(None, False, ns + name)
        if h:
            return h, ctypes.get_last_error() == 183   # ALREADY_EXISTS
    return None


def _win_close_named(handle):
    import ctypes
    ctypes.WinDLL("kernel32").CloseHandle(handle)


_create_named = _win_create_named
_close_named = _win_close_named


def path():
    """One lock per user on this Mac, not one per copy of the folder.

    A lock beside the launcher is a lock per FOLDER, and the whole point of
    `bundle` is that there are now two folders: the Dropbox original and the
    copy on the show machine. Both would have taken their own lock and both
    would have driven the rig. Round 2 of the audit, 2026-09-13.
    """
    if WINDOWS:
        return os.path.join(appdata.folder(), FILENAME)
    home = os.path.expanduser("~")
    for base in (os.path.join(home, "Library", "Application Support"),
                 home, "/tmp"):
        if os.path.isdir(base):
            d = os.path.join(base, "ltcplay")
            try:
                os.makedirs(d, exist_ok=True)
                return os.path.join(d, FILENAME)
            except OSError:
                continue
    return os.path.join("/tmp", FILENAME)


# Every lock this process holds. Without this, an OutputLock whose last
# reference goes out of scope is garbage collected, its file object closes,
# and the kernel releases the flock -- silently, while the process carries on
# sending to the rig. A guard that can be collected is not a guard.
_HELD = set()


class AlreadyRunning(Exception):
    def __init__(self, holder=""):
        self.holder = holder
        super().__init__(holder or "another ltcplay is already sending")


class OutputLock:
    """Held for as long as this process may send to the rig."""

    def __init__(self, where=None, note=""):
        self.path = where or path()
        self.note = note
        self._fh = None
        self._named = None

    def _take_named(self):
        """The named mutex for this lock (NAMED): refuses when another
        process anywhere on the machine holds it, whatever folder its
        files went to."""
        name = "ltcplay-" + os.path.basename(self.path)
        try:
            got = _create_named(name)
        except Exception:
            got = None
        if got is None:
            return
        handle, existed = got
        if existed:
            try:
                _close_named(handle)
            except Exception:
                pass
            raise AlreadyRunning(
                "another copy (it may have been started from inside "
                "another app, whose files go to that app's own folder)")
        self._named = handle

    def acquire(self):
        if NAMED:
            self._take_named()
            try:
                if WINDOWS:
                    return self._acquire_windows()
                return self._acquire_posix()
            except BaseException:
                self._drop_named()
                raise
        if WINDOWS:
            return self._acquire_windows()
        return self._acquire_posix()

    def _drop_named(self):
        h, self._named = self._named, None
        if h is not None:
            try:
                _close_named(h)
            except Exception:
                pass

    def _acquire_posix(self):
        import fcntl
        try:
            fh = open(self.path, "a+")
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EROFS, errno.ENOENT):
                # A read-only or missing folder is not a reason to refuse to
                # run a show. Carry on unlocked rather than failing closed on
                # a guard.
                return self
            raise
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            import errno as _e
            if e.errno not in (_e.EACCES, _e.EAGAIN, _e.EWOULDBLOCK):
                # The filesystem does not support locking. That is not a
                # reason to refuse to run a show: fail open, the way a
                # missing folder does.
                fh.close()
                return self
            try:
                fh.seek(0)
                holder = fh.read(400).strip()
            except OSError:
                holder = ""
            fh.close()
            raise AlreadyRunning(holder)
        return self._hold(fh)

    def _acquire_windows(self):
        """The same contract as the flock path, through msvcrt.locking."""
        import msvcrt
        try:
            # UTF-8, not the Windows code page: the note carries the show
            # file's name, and a name cp1252 cannot spell would otherwise
            # raise after the lock was already taken.
            fh = open(self.path, "a+", encoding="utf-8", errors="replace")
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EROFS, errno.ENOENT):
                # Fail open on a folder that cannot hold the file, as above.
                return self
            raise
        try:
            os.lseek(fh.fileno(), _WIN_LOCK_AT, os.SEEK_SET)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as e:
            # LK_NBLCK reports a lock someone else holds as EACCES; some
            # runtimes say EDEADLOCK. Anything else means this filesystem
            # cannot lock, and that fails open like the flock path does.
            held = (errno.EACCES, getattr(errno, "EDEADLOCK", errno.EACCES),
                    getattr(errno, "EDEADLK", errno.EACCES))
            if e.errno not in held:
                fh.close()
                return self
            try:
                fh.seek(0)
                holder = fh.read(400).strip()
            except OSError:
                holder = ""
            fh.close()
            raise AlreadyRunning(holder)
        return self._hold(fh)

    def _hold(self, fh):
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid {os.getpid()}: {self.note}\n")
        fh.flush()
        self._fh = fh
        _HELD.add(self)
        return self

    def release(self):
        _HELD.discard(self)
        self._drop_named()
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if WINDOWS:
                import msvcrt
                os.lseek(fh.fileno(), _WIN_LOCK_AT, os.SEEK_SET)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            fh.close()
        except OSError:
            pass

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *a):
        self.release()


# ---------------------------------------------------------------- one copy
#
# Second-copy guard (Jeff, 2026-10-03). The show machines are dedicated and
# the flame and arm links bind 127.0.0.1 only, so the realistic second
# sender on those links is a second copy of this program: a launcher
# double-clicked twice, autostart plus a manual launch, an old copy still
# running after a restart. So the show process (`ltc run` and `ltc serve`,
# one lock between them) and the Stream Deck process (`ltc deck`) each take
# a lock for their whole life and refuse to start while another copy holds
# it. Same mechanism as the output lock above, in the same folder: the
# kernel drops the lock when the holder dies, however it dies, so a crashed
# copy never blocks a restart.

SHOW_LOCK = "ltcplay_show.lock"
DECK_LOCK = "ltcplay_deck.lock"

def instance_path(filename):
    """Beside the output lock: one per user on this machine, whichever
    folder the program was started from."""
    return os.path.join(os.path.dirname(path()), filename)


def only_copy(filename, note):
    """Take this program's one-copy lock and hold it until release() or
    the process ends. Raises AlreadyRunning, carrying the running copy's
    own note, if another copy holds it."""
    return OutputLock(where=instance_path(filename), note=note).acquire()


def refusal(filename, holder):
    """The plain sentences a refused second copy prints: what is running,
    and how to stop it. Fix round 1 of PR #40: the app and the autostart
    engine have no window, so "use the window that is already open" told
    the operator nothing."""
    here = "computer" if WINDOWS else "Mac"
    said = f"\nThe copy that is running says: {holder}" if holder else ""
    if filename == DECK_LOCK:
        return (f"ltc deck is already running on this {here}, so this copy "
                f"has stopped. Only one copy may run at a time: two would "
                f"both send on the Stream Deck's arm link, and flamesafe "
                f"would refuse every arm cycle while both were there."
                f"{said}\nThat copy is already driving the Stream Deck. To "
                f"start this one instead, stop that one first (Ctrl-C where "
                f"it is running, or end the process with the pid above), "
                f"then start this one again.")
    return (f"ltcplay's show program is already running on this {here}, so "
            f"this copy has stopped. Only one copy may run at a time: two "
            f"copies would both talk to the rig and the flame safety "
            f"program.{said}\nIt is the LTC Player app, the autostart "
            f"engine, or a Run or Web window. To use it: for the app, "
            f"autostart or a Web window, open its page in a browser at "
            f"http://127.0.0.1 and the port in the line above; for a Run "
            f"window, go to that window. To run this copy "
            f"instead, stop that one first: quit the LTC Player app, turn "
            f"autostart off (Autostart ltcplay.command, then R), or press "
            f"Ctrl-C in the window it runs in.\nRehearse (option 5 in Run "
            f"ltcplay.command) is a copy too, so it is refused while the "
            f"app or autostart is running. Stop that first, as above, and "
            f"then rehearse.")
