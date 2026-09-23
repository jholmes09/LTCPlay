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

    def acquire(self):
        if WINDOWS:
            return self._acquire_windows()
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
            fh = open(self.path, "a+")
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
