"""The night journal and the machine log: what happened tonight, written for
a person and for a debugger at the same moment, from the same event.

Fire & Ice only (handoff section 9). Imported by schedule_service.py and by
nothing else, so the GPL show, which never passes --schedule, never loads it,
and its own show log (showlog.py) is exactly what it was. The selftest proves
both.

What it keeps, in this machine's own data folder (%LOCALAPPDATA%\\ltcplay\\
nights on Windows), never beside the program and never in a synced folder:

    night_2026-11-14.journal.txt   the night journal: one plain English line
                                   per event, "19:44:12  Andy pressed Abort"
    night_2026-11-14.jsonl         the machine log: the same events, one JSON
                                   object each, every field
    night_2026-11-14.summary.md    the morning read, written when the night
                                   closes
    incidents/incident_2026-11-14_194512/    what "Save the last incident"
                                   wrote

The rules it keeps:

- ONE record per event. The journal line is rendered once, stored in the
  record, written to the file and served to the page as the same string, so
  the screen and the file cannot say different things.
- Every event carries local time with its offset, the state, the actor (one
  of ACTORS, never blank), the action, the outcome and the reason. An
  operator event also carries the operator's name and the screen. Anything
  missing is refused with a sentence, never written blank.
- A fault is a sentence a person can act on. fault() refuses a blank one or
  one that only says "error".
- Append only. The streams are opened for append and nothing else; nothing
  already written is ever rewritten or truncated. A line a full disk cut
  short is finished with a newline before anything else goes after it.
- Bounded. Per-frame data goes to a ring buffer in memory (the last 60 s at
  5 samples a second), never to disk.
- A full or failing disk stops the logging, not the show. The scheduler only
  hands a record over; writing happens on the journal's own thread. A failure
  is a health flag with a sentence; the lines wait in memory and are written,
  in order, when the disk takes them again.
- Night files are kept 90 days. Pruning goes by the date in the file's own
  name, never by its timestamp, so a change of clocks cannot shorten it, and
  it touches nothing whose name it did not write.

A flame provider (the flame bus, a later build) is optional. When there is
one it has two methods, and anything either raises becomes a sentence:

    recent_frames(n)   the last n flame frames, oldest first, each a dict
                       such as {"at": iso time, "seq": 1234,
                       "groups": {"1": {"commanded": 255, "sent": 0}}}
    mismatches(night)  tonight's commanded against sent disagreements, each
                       {"group": "3", "at": iso time, "frames": 5,
                       "duration_s": 0.17}

Until it exists, the bundle and the summary say "not available".
"""
import errno
import itertools
import json
import os
import re
import shutil
import stat
import sys
import threading
import time as _time
from collections import deque
from datetime import date, datetime, timedelta, timezone

from . import appdata

WINDOWS = sys.platform == "win32"

# The same six as the scheduler's, written out here so this module never
# imports the scheduler. The selftest checks the two lists agree.
ACTORS = ("scheduler", "operator", "madmapper", "safety", "reader", "system")

KEEP_DAYS = 90
MEMORY_LINES = 400          # what the page can scroll back through
PAGE_LINES = 20             # the log strip: the last 20, newest first
RING_SECONDS = 60
RING_HZ = 5
RING_SIZE = RING_SECONDS * RING_HZ
FLAME_FRAMES = 20
PENDING_MAX = 5000          # lines waiting for a disk that is not taking them
RETRY_S = 30.0              # how often a stopped disk is tried again
FREE_CHECK_S = 60.0         # how often free space is looked at
FREE_FLOOR_MB = 100         # below this, logging stops to leave the room
LOCK_TRIES = 5
LOCK_WAIT_S = 0.02
SUMMARY_LIST_MAX = 25       # one page: longer lists point at the journal

FOLDER = "nights"
INCIDENTS = "incidents"
LOCK_FILE = ".writing.lock"
_NAME = re.compile(r"^night_(\d{4})-(\d{2})-(\d{2})"
                   r"\.(journal\.txt|jsonl|summary\.md)$")

# Folder names that belong to a sync client. Matched against whole path
# components, so a folder that merely has "dropbox" inside a longer name is
# not mistaken for one.
_SYNCED = {"dropbox": "Dropbox", "onedrive": "OneDrive",
           "icloud drive": "iCloud", "mobile documents": "iCloud",
           "google drive": "Google Drive", "cloudstorage": "a cloud drive",
           "box sync": "Box"}

_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
         "Sunday")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


class FaultSentenceError(ValueError):
    """A fault was logged without a sentence a person can act on."""


class _Locked(OSError):
    """Another program holds the log files' lock."""


# ------------------------------------------------------------ names --

def journal_name(night):
    return f"night_{night}.journal.txt"


def machine_name(night):
    return f"night_{night}.jsonl"


def summary_name(night):
    return f"night_{night}.summary.md"


def default_folder():
    """Where the night files go when nobody says otherwise. On Windows,
    %LOCALAPPDATA%\\ltcplay\\nights, through appdata. Elsewhere this user's
    own application data, never the program folder, which on a Mac may sit
    in Dropbox."""
    if appdata.WINDOWS:
        return os.path.join(appdata.folder(), FOLDER)
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support",
                            appdata.NAME)
    else:
        base = os.path.join(os.environ.get("XDG_STATE_HOME")
                            or os.path.join(home, ".local", "state"),
                            appdata.NAME)
    return os.path.join(base, FOLDER)


def synced_folder(path):
    """The sync client whose folder `path` is inside, or ""."""
    parts = re.split(r"[\\/]+", os.path.abspath(path).lower())
    for p in parts:
        for key, name in _SYNCED.items():
            if p == key or p.startswith(key + " (") or \
                    p.startswith(key + " -"):
                return name
    return ""


# ------------------------------------------------------------ words --

def fmt_span(seconds):
    """4m 12s. 1h 0m 5s. 12s. The scheduler's style: never a colon, which
    reads as a time of day."""
    s = int(round(abs(seconds)))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def long_date(night):
    d = night if isinstance(night, date) else date.fromisoformat(str(night))
    return f"{_DAYS[d.weekday()]} {d.day} {_MONTHS[d.month - 1]} {d.year}"


def one_line(text):
    """One line, whatever it was handed: newlines, tabs and runs of spaces
    become one space, and an em or en dash becomes a plain hyphen, because
    operator text never carries either."""
    text = str(text).replace("\u2014", "-").replace("\u2013", "-")
    return " ".join(text.split())


# Words that carry no information on their own. A fault sentence needs at
# least three words that are not in here.
_EMPTY_WORDS = frozenset("""
    error errors err fault faults failed failure fail fails exception
    exceptions unknown code errno an a the has have had occurred occured
    there was is it something went wrong problem issue oops none null see
    log logs please check details internal unexpected traceback
    """.split())


def check_fault_sentence(text):
    """Refuse a fault that says nothing. Returns the sentence, one line.

    A fault that can only say "error" is not finished (handoff section 9).
    This is the test: at least three words that mean something, so "error",
    "Error 32", "An error occurred" and "Something went wrong" are refused,
    and "Show 4 did not start" is kept."""
    if not isinstance(text, str) or not text.strip():
        raise FaultSentenceError(
            "A fault was logged with no sentence. Every fault says what "
            "happened, what was tried and what happens next.")
    words = re.findall(r"[A-Za-z][A-Za-z']*", text)
    meaningful = [w for w in words if w.lower() not in _EMPTY_WORDS]
    if len(meaningful) < 3:
        raise FaultSentenceError(
            f"{text.strip()!r} is not a sentence a person can act on. A "
            f"fault says what happened, what was tried and what happens "
            f"next.")
    return one_line(text)


def render_line(local, text, actor="", who="", screen=""):
    """The journal line, in the format section 9 sets:

        19:44:12  Andy pressed Abort on the rack screen during show 4.

    Two spaces after the time. An operator line always names the operator:
    when the sentence does not already, "By Andy on the rack screen." is
    added at the end, so the journal alone can say who touched what. The
    screen is always in the machine log; the line names it when the
    sentence does, as the spec's own examples do."""
    line = f"{local:%H:%M:%S}  {text}"
    if actor == "operator" and who.lower() not in text.lower():
        tail = "" if line.rstrip().endswith((".", "!", "?")) else "."
        line = line.rstrip() + f"{tail} By {who} on the {screen}."
    return line


def build_event(*, at, night, state, actor, action, outcome, reason, text,
                to_state=None, show=None, screen=None, who=None,
                fault=False, data=None, id="", tz=None):
    """One event, checked, as the record both streams are written from.

    Raises ValueError with a sentence for anything missing. Nothing here is
    ever written blank: an event that cannot say who did it is a bug, and it
    is found where it is made, not the next morning."""
    if not isinstance(at, datetime) or at.utcoffset() is None:
        raise ValueError("A journal event needs its time with the offset; a "
                         "bare time is ambiguous on the night the clocks "
                         "change.")
    if actor not in ACTORS:
        raise ValueError(f"A journal event's actor has to be one of "
                         f"{', '.join(ACTORS)}, not {actor!r}.")
    for name, v in (("state", state), ("action", action),
                    ("outcome", outcome), ("reason", reason),
                    ("text", text)):
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"A journal event from {actor} has no {name}. "
                             f"Every event carries the state, the action, "
                             f"the outcome, the reason and its sentence.")
    who = one_line(who or "")
    screen = one_line(screen or "")
    if actor == "operator" and not (who and screen):
        raise ValueError("An operator event has to name the operator and the "
                         "screen it came from, so the journal can say who "
                         "did what.")
    text = check_fault_sentence(text) if fault else one_line(text)
    local = at.astimezone(tz) if tz is not None else at
    rec = {"id": id, "at": local.isoformat(timespec="milliseconds"),
           "night": str(night), "state": one_line(state),
           "to_state": one_line(to_state or state), "actor": actor,
           "action": one_line(action), "outcome": one_line(outcome),
           "reason": one_line(reason), "show": show or None,
           "screen": screen or None, "who": who or None,
           "fault": bool(fault), "text": text,
           "line": render_line(local, text, actor, who, screen)}
    if data:
        rec["data"] = data
    return rec


def _jsonl(rec):
    return (json.dumps(rec, ensure_ascii=False, separators=(",", ":"),
                       default=str) + "\n").encode("utf-8")


def _jline(rec):
    return (rec["line"] + "\n").encode("utf-8")


def _parse_at(text):
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _hms(at):
    d = _parse_at(at) if isinstance(at, str) else at
    return d.strftime("%H:%M:%S") if d is not None else "--:--:--"


def version_info():
    """What is running, from the bytes (version.py). Worked out once per
    run: the program in memory does not change when the files do."""
    global _VERSION
    if _VERSION is None:
        info = {"python": sys.version.split()[0], "platform": sys.platform}
        try:
            from . import version
            bid, count, _newest = version.build()
            info.update(status=version.status(), build=bid, files=count,
                        release=version.release())
        except Exception as e:           # never stop a night over this
            info.update(status=f"version unknown ({type(e).__name__}: {e})",
                        build=None, files=None, release=None)
        _VERSION = info
    return dict(_VERSION)


_VERSION = None
_BOOKS = itertools.count(1)


# ------------------------------------------------------------ the disk --

def _open_append(path):
    """The only way a night file is ever opened for writing: append, binary,
    unbuffered, so each line is one write and a line already written can
    never be written over."""
    return open(path, "ab", buffering=0)


def _lock(fh, tries, wait, sleep):
    """Take the log files' lock without ever waiting long. On Windows through
    msvcrt, elsewhere through flock; both refuse at once when another program
    holds it, and after a few short tries this gives up with _Locked rather
    than hold up the scheduler."""
    for i in range(tries):
        try:
            if WINDOWS:
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if i == tries - 1:
                raise _Locked(errno.EAGAIN, "the log files are locked")
            sleep(wait)


def _unlock(fh):
    try:
        if WINDOWS:
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def hold_lock(folder):
    """Take the log files' lock and keep it: returns the open handle, which
    releases the lock when closed. For another process, and for the selftest
    playing one."""
    os.makedirs(folder, exist_ok=True)
    fh = open(os.path.join(folder, LOCK_FILE), "a+b")
    _lock(fh, 1, 0, _time.sleep)
    return fh


def _replace(src, dst, tries=5, sleep=_time.sleep):
    """os.replace, patiently: Windows refuses while a reader has the file
    open, and the refusal is usually gone a moment later."""
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            sleep(0.1 * (i + 1))


def _write_new(path, data):
    """A file that did not exist, written whole. Never replaces one."""
    with open(path, "xb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def _tail(path, limit=65536):
    """The last `limit` bytes of a file, or b"" if it cannot be read."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            return fh.read()
    except OSError:
        return b""


# ------------------------------------------------------------ the book --

class Logbook:
    """Both streams for one machine, the page's last lines, the ring buffer,
    the incident bundle and the nightly summary.

    `clock` returns an aware datetime; `tz` is the zone local times are
    written in (a tzinfo, or a callable returning one, so it can follow the
    rule file). The disk is reached only through `opener`, `disk_usage` and
    the lock, which the selftest replaces to fill the disk on purpose.

    Until start_writer() is called every record is written before record()
    returns, which is what the selftest wants. The scheduler service calls
    start_writer(), after which record() only hands the line over and the
    journal's own thread writes it: a slow or full disk can then never hold
    up a show."""

    def __init__(self, folder=None, clock=None, tz=None,
                 flame_provider=None, memory=MEMORY_LINES,
                 keep_days=KEEP_DAYS, opener=None, disk_usage=None,
                 sleep=None, retry_s=RETRY_S, free_floor_mb=FREE_FLOOR_MB,
                 durable=True, state=None):
        self.folder = os.path.abspath(folder or default_folder())
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._tz = tz
        self._state = state
        self.flames = flame_provider
        self.memory = deque(maxlen=memory)
        self.earlier = []           # the last lines from before this run
        self.ring = deque(maxlen=RING_SIZE)
        self.keep_days = keep_days
        self._opener = opener or _open_append
        self._disk_usage = disk_usage or shutil.disk_usage
        self._sleep = sleep or _time.sleep
        self.retry_s = retry_s
        self.free_floor_mb = free_floor_mb
        self.durable = durable
        self._lock = threading.Lock()       # memory, pending, ring
        self._io = threading.RLock()        # one writer at a time
        self._pending = deque()             # [record, streams written]
        self._ids = itertools.count(1)
        # Unique per logbook, so two runs in one night never share an id.
        self._run = (f"{os.getpid():x}{int(_time.time()) & 0xffffff:06x}"
                     f"{next(_BOOKS)}")
        self.stopped_why = ""               # set while disk logging is off
        self.stopped_at = None
        self._retry_at = None
        self._check_tails = False
        self._dropped = 0
        self._dropped_span = None
        self.last_write_at = None
        self._free_mb = None
        self._free_at = None
        self.writes = 0
        self._writer = None
        self._wake = threading.Event()
        self._closing = False
        where = synced_folder(self.folder)
        self.warning = (f"The night logs are in {self.folder}, which is "
                        f"inside {where}. Logs belong on this machine only; "
                        f"a sync client can lock or rewrite them."
                        if where else "")
        try:
            os.makedirs(self.folder, exist_ok=True)
        except OSError:
            pass            # the first write says what is wrong, as a flag

    # -- time -----------------------------------------------------------
    def tz(self):
        tz = self._tz() if callable(self._tz) else self._tz
        return tz

    def local(self, at=None):
        at = at or self.clock()
        tz = self.tz()
        return at.astimezone(tz) if tz is not None else at.astimezone()

    def current_state(self):
        """The scheduler's state, for lines the journal writes by itself."""
        try:
            st = self._state() if callable(self._state) else self._state
        except Exception:
            st = None
        return st or "UNKNOWN"

    def night_of(self, at=None):
        return self.local(at).date()

    # -- recording --------------------------------------------------------
    def record(self, *, actor, action, outcome, reason, text, state=None,
               night=None, at=None, to_state=None, show=None, screen=None,
               who=None, data=None, fault=False):
        """Write one event to both streams (or queue it for the writer).
        Returns the record. Raises ValueError for a record with anything
        missing: that is a bug where the event was made."""
        at = at or self.clock()
        local = self.local(at)
        rec = build_event(at=local, night=night or local.date(),
                          state=state or self.current_state(),
                          actor=actor, action=action,
                          outcome=outcome, reason=reason, text=text,
                          to_state=to_state, show=show, screen=screen,
                          who=who, fault=fault, data=data,
                          id=f"{self._run}-{next(self._ids)}")
        self._queue(rec)
        self._kick()
        return rec

    def fault(self, actor, sentence, *, action, state=None, outcome="fault",
              reason=None, **kw):
        """A fault, which has to be a sentence: blank or "error" is refused
        with FaultSentenceError before anything is written."""
        sentence = check_fault_sentence(sentence)
        return self.record(actor=actor, action=action, outcome=outcome,
                           reason=reason or sentence, text=sentence,
                           state=state, fault=True, **kw)

    def announcement(self, *, who, screen, name, length_s, ended, state=None,
                     file="", at=None, night=None):
        """An announcement played, in the journal's own words:
        "Andy played the Cannot Continue announcement, 22 s, finished
        normally." `ended` says how it ended, in words."""
        length = (f"{int(round(length_s))} s" if length_s < 60
                  else fmt_span(length_s))
        text = (f"{who} played the {name} announcement, {length}, "
                f"{one_line(ended).rstrip('.')}.")
        return self.record(actor="operator", action="announcement",
                           outcome="played", reason=f"{name}: {ended}",
                           text=text, state=state, who=who, screen=screen,
                           at=at, night=night,
                           data={"name": name, "file": file,
                                 "length_s": length_s, "ended": ended})

    def timecode_dropout(self, *, started, duration_s, state=None, show=None,
                         actor="system", night=None):
        """Timecode stopped arriving for `duration_s` from `started`."""
        s = self.local(started)
        end = s + timedelta(seconds=duration_s)
        during = f" during show {show}" if show else ""
        text = (f"Timecode dropped out for {duration_s:.1f} s{during}, from "
                f"{s:%H:%M:%S} to {end:%H:%M:%S}.")
        return self.record(actor=actor, action="timecode dropout",
                           outcome="recovered", reason=f"no timecode for "
                           f"{duration_s:.1f} s", text=text, state=state,
                           show=show, night=night,
                           data={"started": s.isoformat(
                               timespec="milliseconds"),
                               "duration_s": round(float(duration_s), 3)})

    def _queue(self, rec):
        with self._lock:
            self.memory.append(rec)
            if len(self._pending) >= PENDING_MAX:
                old = self._pending.popleft()[0]
                self._dropped += 1
                first = (self._dropped_span or (old["at"], old["at"]))[0]
                self._dropped_span = (first, old["at"])
            self._pending.append([rec, set()])

    def _kick(self):
        if self._writer is not None:
            self._wake.set()
        else:
            self.drain()

    # -- the writer -------------------------------------------------------
    def start_writer(self):
        """From here on record() never touches the disk itself."""
        if self._writer is None:
            self._writer = threading.Thread(target=self._write_loop,
                                            daemon=True,
                                            name="ltcplay-journal")
            self._writer.start()
        return self

    def threaded(self):
        return self._writer is not None

    def _write_loop(self):
        while not self._closing:
            self._wake.wait(1.0)
            self._wake.clear()
            try:
                self.drain()
            except Exception:        # drain says its own problems; belt
                pass                 # and braces for the thread's sake

    def close(self):
        """Stop the writer and write what is left, if the disk will take
        it."""
        self._closing = True
        self._wake.set()
        t = self._writer
        if t is not None:
            t.join(timeout=2)
        self._writer = None
        self.drain(force=True)

    def pending(self):
        with self._lock:
            return len(self._pending)

    def drain(self, force=False):
        """Write every waiting line, in order. Never raises. True when
        nothing is left waiting. While the disk is stopped it is tried again
        only every retry_s, unless `force`."""
        with self._io:
            now = self.clock()
            if self.stopped_why and not force and self._retry_at is not None \
                    and now < self._retry_at:
                return False
            why = self._low_space(now)
            if why:
                self._stop(now, why)
                return False
            while True:
                with self._lock:
                    batch = list(self._pending)
                if not batch:
                    return True
                try:
                    self._write(batch)
                except OSError as e:
                    self._stop(now, self._why(e))
                    return False
                done = {id(e) for e in batch}
                with self._lock:
                    while self._pending and id(self._pending[0]) in done:
                        self._pending.popleft()
                self.last_write_at = now
                self._check_tails = False
                if self.stopped_why:
                    self._resumed(now)

    def _write(self, batch):
        os.makedirs(self.folder, exist_ok=True)
        with open(os.path.join(self.folder, LOCK_FILE), "a+b") as lk:
            _lock(lk, LOCK_TRIES, LOCK_WAIT_S, self._sleep)
            try:
                nights = []
                for entry in batch:
                    if entry[0]["night"] not in nights:
                        nights.append(entry[0]["night"])
                for night in nights:
                    group = [e for e in batch if e[0]["night"] == night]
                    # The machine log first, then the journal: both streams
                    # carry every event, and a record half written (one
                    # stream done) is finished on the next try, never
                    # written twice.
                    for stream, name, encode in (
                            ("jsonl", machine_name(night), _jsonl),
                            ("journal", journal_name(night), _jline)):
                        todo = [e for e in group if stream not in e[1]]
                        if todo:
                            self._append(os.path.join(self.folder, name),
                                         todo, stream, encode)
            finally:
                _unlock(lk)

    def _append(self, path, todo, stream, encode):
        cut = self._check_tails and not _tail(path, 1).endswith(b"\n") \
            and os.path.exists(path) and os.path.getsize(path) > 0
        fh = self._opener(path)
        try:
            if cut:
                # A full disk cut the last line short. Finish it; never
                # truncate it.
                fh.write(b"\n")
            for e in todo:
                data = encode(e[0])
                n = fh.write(data)
                if n is not None and n < len(data):
                    raise OSError(errno.ENOSPC, "the disk took only part of "
                                                "a line")
                e[1].add(stream)
                self.writes += 1
            if self.durable:
                os.fsync(fh.fileno())
        finally:
            fh.close()

    def _why(self, e):
        if isinstance(e, _Locked):
            return (f"another program is holding the log files (the lock "
                    f"{os.path.join(self.folder, LOCK_FILE)})")
        if e.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)):
            return "the disk is full"
        if e.errno in (errno.EACCES, errno.EPERM):
            return (f"this machine refused to let ltcplay write to "
                    f"{self.folder} ({e.strerror or e})")
        return f"writing to {self.folder} failed ({e.strerror or e})"

    def _low_space(self, now):
        due = self._free_at is None or self.stopped_why or \
            (now - self._free_at).total_seconds() >= FREE_CHECK_S
        if due:
            try:
                self._free_mb = self._disk_usage(self.folder).free / 1e6
                self._free_at = now
            except (OSError, AttributeError, TypeError):
                return ""        # unknown is not full; the write will say
        if self._free_mb is not None and self._free_mb < self.free_floor_mb:
            return (f"only {self._free_mb:.0f} MB is free on the disk, under "
                    f"the {self.free_floor_mb} MB kept for Windows and the "
                    f"show")
        return ""

    def _stop(self, now, why):
        first = not self.stopped_why
        if first:
            self.stopped_at = now
        self.stopped_why = why
        self._retry_at = now + timedelta(seconds=self.retry_s)
        self._check_tails = True
        if first:
            # On the page at once; in the files, in its place, once the disk
            # takes lines again.
            local = self.local(now)
            self._queue(build_event(
                at=local, night=local.date(), state=self.current_state(),
                actor="system", action="logging stopped", outcome="stopped",
                reason=why, fault=True,
                text=(f"Logging to disk stopped at {local:%H:%M:%S} because "
                      f"{why}. The show carries on. Lines wait in memory "
                      f"and are written when the disk takes them again; it "
                      f"is tried every {self.retry_s:g} s."),
                id=f"{self._run}-{next(self._ids)}", data={"why": why}))

    def _resumed(self, now):
        was, since = self.stopped_why, self.stopped_at
        self.stopped_why = ""
        self.stopped_at = None
        self._retry_at = None
        lost = ""
        if self._dropped:
            a, b = self._dropped_span
            lost = (f" {self._dropped} line(s) from {_hms(a)} to {_hms(b)} "
                    f"were not written, because more were waiting than "
                    f"memory keeps.")
            self._dropped = 0
            self._dropped_span = None
        local = self.local(now)
        rec = build_event(
            at=local, night=local.date(), state=self.current_state(),
            actor="system", action="logging resumed", outcome="resumed",
            reason=was,
            text=(f"Logging to disk resumed at {local:%H:%M:%S}. It had "
                  f"stopped at {self.local(since):%H:%M:%S} because {was}. "
                  f"The lines from that time were kept in memory and are "
                  f"written above this one.{lost}"),
            id=f"{self._run}-{next(self._ids)}",
            data={"stopped_at": self.local(since).isoformat(
                timespec="seconds"), "why": was})
        self._queue(rec)

    # -- what the page reads ------------------------------------------------
    def recent(self, n=PAGE_LINES):
        """The last n journal lines, newest first, each with its age. The
        lines are the file's own lines. After a restart the lines from
        before it fill in below this run's."""
        now = self.clock()
        with self._lock:
            rows = list(self.memory)[-n:][::-1]
        rows += self.earlier[::-1][:max(0, n - len(rows))]
        out = []
        for r in rows:
            at = _parse_at(r.get("at"))
            out.append({"line": r["line"], "at": r.get("at"),
                        "age_s": (round((now - at).total_seconds(), 1)
                                  if at is not None else None),
                        "actor": r.get("actor"),
                        "fault": bool(r.get("fault"))})
        return out

    def health(self):
        """Is the logging writing, in one sentence, with the age of every
        number in it."""
        now = self.clock()
        pending = self.pending()

        def age(t):
            return round((now - t).total_seconds(), 1) if t else None

        if self.stopped_why:
            dropped = (f" {self._dropped} line(s) could not be kept and "
                       f"will be missing from the file." if self._dropped
                       else "")
            sentence = (f"Logging to disk stopped at "
                        f"{self.local(self.stopped_at):%H:%M:%S}: "
                        f"{self.stopped_why}. The show is not affected. "
                        f"{pending} line(s) are waiting in memory and will "
                        f"be written when the disk takes them again; it is "
                        f"tried every {self.retry_s:g} s.{dropped}")
        elif self.last_write_at is None:
            sentence = f"Logging to {self.folder}. Nothing written yet."
        else:
            sentence = (f"Logging to {self.folder}. Last written "
                        f"{fmt_span(age(self.last_write_at))} ago.")
        return {"ok": not self.stopped_why, "sentence": sentence,
                "warning": self.warning or None,
                "folder": self.folder,
                "as_of": self.local(now).isoformat(timespec="seconds"),
                "stopped_at": (self.local(self.stopped_at).isoformat(
                    timespec="seconds") if self.stopped_at else None),
                "last_write_at": (self.local(self.last_write_at).isoformat(
                    timespec="seconds") if self.last_write_at else None),
                "last_write_age_s": age(self.last_write_at),
                "free_mb": (round(self._free_mb) if self._free_mb is not None
                            else None),
                "free_checked_age_s": age(self._free_at),
                "pending": pending, "dropped": self._dropped,
                "keep_days": self.keep_days}

    # -- the ring buffer ------------------------------------------------------
    def sample(self, data, at=None):
        """One state sample into the ring buffer. Memory only: nothing here
        ever reaches the disk, so per-frame data cannot grow the files."""
        with self._lock:
            self.ring.append((at or self.clock(), dict(data)))

    def last_state(self, seconds=RING_SECONDS, now=None):
        now = now or self.clock()
        cut = now - timedelta(seconds=seconds)
        with self._lock:
            rows = [(t, d) for t, d in self.ring if t >= cut]
        out = []
        for t, d in rows:
            row = {"at": self.local(t).isoformat(timespec="milliseconds"),
                   "age_s": round((now - t).total_seconds(), 3)}
            row.update(d)
            out.append(row)
        return out

    # -- program start and stop ------------------------------------------
    def started(self, *, state, night=None, build=None):
        """The first line of a run. When tonight's log already has lines,
        this run is a restart, and the line says when the last one was and
        whether the program said it was stopping."""
        night = night or self.night_of()
        build = build or version_info()["status"]
        prev, lines = self._previous(night)
        self.earlier = lines
        now = self.local()
        data = {"restart": prev is not None, "build": build}
        if prev is None:
            text = (f"ltcplay started ({build}). This is the night journal "
                    f"for {long_date(night)}.")
            outcome = "started"
        else:
            at = _parse_at(prev.get("at"))
            data["previous_last_line_at"] = prev.get("at")
            gap = (f", {fmt_span((now - at).total_seconds())} earlier"
                   if at is not None else "")
            if prev.get("action") == "program stop":
                text = (f"ltcplay started again ({build}). It was stopped "
                        f"cleanly at {_hms(prev.get('at'))}{gap}.")
                outcome = "restarted after a clean stop"
            else:
                text = (f"ltcplay started again ({build}). Its last line "
                        f"before this was at {_hms(prev.get('at'))}{gap}, "
                        f"and it did not say it was stopping, so it stopped "
                        f"without warning: a crash, a power cut or someone "
                        f"ending it by force.")
                outcome = "restarted without warning"
        return self.record(actor="system", action="program start",
                           outcome=outcome, reason=outcome, text=text,
                           state=state, night=night, data=data)

    def stopping(self, *, state, night=None):
        return self.record(actor="system", action="program stop",
                           outcome="stopped", reason="asked to stop",
                           text="ltcplay is stopping because it was asked "
                                "to. The scheduler stops with it.",
                           state=state, night=night)

    def _previous(self, night):
        """(the last record in tonight's machine log, the last page of
        journal lines), from the disk."""
        tail = _tail(os.path.join(self.folder, machine_name(night)))
        recs = []
        for raw in tail.split(b"\n"):
            try:
                r = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(r, dict) and "line" in r:
                recs.append(r)
        return (recs[-1] if recs else None), recs[-PAGE_LINES:]

    # -- reading a night back ---------------------------------------------
    def night_records(self, night):
        """Every record for one night: the machine log on disk, plus any
        still waiting for the disk. (records, sentence or "")."""
        night = str(night)
        out, seen, note = [], set(), ""
        path = os.path.join(self.folder, machine_name(night))
        try:
            with open(path, "rb") as fh:
                for raw in fh:
                    try:
                        r = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if isinstance(r, dict) and r.get("id") not in seen:
                        seen.add(r.get("id"))
                        out.append(r)
        except FileNotFoundError:
            pass
        except OSError as e:
            note = (f"The machine log {path} could not be read "
                    f"({e.strerror or e}), so this is built from the lines "
                    f"still in memory.")
            with self._lock:
                for r in self.memory:
                    if r["night"] == night and r["id"] not in seen:
                        seen.add(r["id"])
                        out.append(r)
        with self._lock:
            for r, _done in self._pending:
                if r["night"] == night and r["id"] not in seen:
                    seen.add(r["id"])
                    out.append(r)
        return out, note

    # -- pruning ----------------------------------------------------------
    def prune(self, today, state="BOOT"):
        """Remove night files older than keep_days before `today`, a date.

        By the date in the file's own name, so a change of clocks, a file
        copied in with an old timestamp or a clock set wrong for an hour
        cannot remove the wrong night. Only names this module writes, only
        plain files, only in its own folder; incident folders are never
        touched. Returns the names removed."""
        cutoff = today - timedelta(days=self.keep_days)
        removed, problems = [], []
        try:
            names = sorted(os.listdir(self.folder))
        except OSError:
            return []
        for name in names:
            m = _NAME.match(name)
            if not m:
                continue
            try:
                d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
            if d >= cutoff:
                continue
            p = os.path.join(self.folder, name)
            try:
                if not stat.S_ISREG(os.lstat(p).st_mode):
                    continue
                os.remove(p)
                removed.append((d, name))
            except OSError as e:
                problems.append(f"{name} ({e.strerror or e})")
        if removed:
            first, last = removed[0][0], removed[-1][0]
            self.record(actor="system", action="prune", outcome="removed",
                        reason=f"older than {self.keep_days} days",
                        text=(f"Removed {len(removed)} night log file(s) "
                              f"older than {self.keep_days} days, from "
                              f"{first} to {last}. Nights from {cutoff} on "
                              f"are kept."),
                        state=state, night=today,
                        data={"removed": [n for _d, n in removed]})
        if problems:
            self.record(actor="system", action="prune", outcome="failed",
                        reason="could not remove old files",
                        text=(f"Could not remove {len(problems)} old night "
                              f"log file(s): {', '.join(problems[:5])}. They "
                              f"are left where they are and tried again "
                              f"tomorrow."),
                        state=state, night=today)
        return [n for _d, n in removed]

    # -- the incident bundle --------------------------------------------------
    def save_incident(self, *, who, screen, state, night=None, config=None,
                      why=""):
        """Save the last incident: a new folder, named by date and time,
        holding the night journal so far, the machine log, the config in
        force, the version and build id, the last 60 s of state and the last
        20 flame frames. Returns {"ok", "folder", "files", "sentence"}; a
        failure is a sentence and a fault line, never an exception."""
        night = str(night or self.night_of())
        local = self.local()
        name = f"incident_{local:%Y-%m-%d_%H%M%S}"
        asked = f" ({one_line(why)})" if why and why.strip() else ""
        self.record(actor="operator", action="save incident",
                    outcome="requested", reason=why or "operator asked",
                    text=(f"{who} pressed Save the last incident on the "
                          f"{screen}{asked}."),
                    state=state, who=who, screen=screen, night=night)
        root = os.path.join(self.folder, INCIDENTS)
        final = os.path.join(root, name)
        n = 2
        while os.path.exists(final) or os.path.exists(final + ".partial"):
            final = os.path.join(root, f"{name}_{n}")
            n += 1
        part = final + ".partial"
        try:
            with self._io:
                self.drain(force=True)
                os.makedirs(part, exist_ok=False)
                files = self._bundle(part, night, local, who, screen, why,
                                     config)
            _replace(part, final)
        except OSError as e:
            sentence = (f"The incident could not be saved: {self._why(e)}. "
                        f"It was being written to {part}. The show is not "
                        f"affected, and the night journal still has every "
                        f"line up to now.")
            self.fault("system", sentence, action="save incident",
                       state=state, night=night)
            return {"ok": False, "folder": None, "files": [],
                    "sentence": sentence}
        sentence = (f"The incident was saved to {final}: {len(files)} files, "
                    f"the night so far, the config, the version, the last "
                    f"60 s of state and the last {FLAME_FRAMES} flame "
                    f"frames.")
        self.record(actor="system", action="save incident", outcome="saved",
                    reason=os.path.basename(final), text=sentence,
                    state=state, night=night,
                    data={"folder": final, "files": files})
        return {"ok": True, "folder": final, "files": files,
                "sentence": sentence}

    def _stream_copy(self, night, stream):
        """A night file as it stands, plus the lines still waiting for the
        disk, so the bundle has everything even when the disk stopped."""
        name = (machine_name if stream == "jsonl" else journal_name)(night)
        try:
            with open(os.path.join(self.folder, name), "rb") as fh:
                data = fh.read()
        except OSError:
            data = b""
        if data and not data.endswith(b"\n"):
            data += b"\n"
        encode = _jsonl if stream == "jsonl" else _jline
        with self._lock:
            waiting = [r for r, done in self._pending
                       if r["night"] == night and stream not in done]
        return data + b"".join(encode(r) for r in waiting)

    def _flames(self, n=FLAME_FRAMES):
        if self.flames is None:
            return {"available": False,
                    "note": "Not available: there is no flame bus in this "
                            "build, so no flame frames were recorded."}
        try:
            frames = list(self.flames.recent_frames(n))[-n:]
        except Exception as e:
            return {"available": False,
                    "note": f"Not available: the flame bus did not answer "
                            f"({type(e).__name__}: {e})."}
        now = self.clock()
        for f in frames:
            at = _parse_at(f.get("at")) if isinstance(f, dict) else None
            if at is not None and at.utcoffset() is not None:
                f["age_s"] = round((now - at).total_seconds(), 3)
        return {"available": True, "frames": frames}

    def _bundle(self, part, night, local, who, screen, why, config):
        files = []

        def put(fname, data):
            if isinstance(data, str):
                data = data.encode("utf-8")
            _write_new(os.path.join(part, fname), data)
            files.append(fname)

        def js(doc):
            return json.dumps(doc, indent=2, ensure_ascii=False,
                              default=str) + "\n"

        flames = self._flames()
        state = self.last_state(now=self.clock())
        ver = version_info()
        put("journal.txt", self._stream_copy(night, "journal"))
        put("machine_log.jsonl", self._stream_copy(night, "jsonl"))
        put("config.json", js(config if config is not None else
                              {"note": "No config was handed over."}))
        put("version.json", js(ver))
        put("state_last_60s.jsonl", "".join(
            json.dumps(r, ensure_ascii=False, default=str) + "\n"
            for r in state))
        put("flames_last_20.json", js(flames))
        put("logging_health.json", js(self.health()))
        span = (f"{len(state)} samples, from {_hms(state[0]['at'])} to "
                f"{_hms(state[-1]['at'])}" if state else "no samples")
        readme = [
            f"Incident saved {local:%Y-%m-%d %H:%M:%S} ({local:%z}) by {who} "
            f"on the {screen}.",
            f"Reason given: {one_line(why)}." if why and why.strip()
            else "No reason was given.",
            f"Night: {long_date(night)}. Program: {ver.get('status')}.",
            "",
            "journal.txt            the night journal so far, one line per "
            "event",
            "machine_log.jsonl      the same events with every field",
            "config.json            the schedule, operators and tonight's "
            "list in force",
            "version.json           the version and build id",
            f"state_last_60s.jsonl   the last 60 s of state, 5 samples a "
            f"second ({span}); each has its time and its age at saving",
            "flames_last_20.json    the last 20 flame frames, commanded "
            "against sent"
            + ("" if flames["available"] else ": " + flames["note"]),
            "logging_health.json    whether the logs were being written",
        ]
        put("README.txt", "\n".join(readme) + "\n")
        return files

    # -- the nightly summary ------------------------------------------------
    def write_summary(self, night, *, state, slots=None, closed_by=""):
        """Write the morning read beside the journal. Returns the path, or
        None with a fault line saying why it could not be written.

        The journal is append only; this is not a stream, it is a report
        made from one, so writing it again later in the night (an extra show
        after closing) replaces it whole, in one step."""
        night = str(night)
        self.drain(force=True)
        recs, note = self.night_records(night)
        mism, mnote = self._mismatches(night)
        text = summary_text(night, recs, slots=slots, mismatches=mism,
                            mismatch_note=mnote, health=self.health(),
                            written=self.local(), build=version_info(),
                            read_note=note, closed_by=closed_by)
        path = os.path.join(self.folder, summary_name(night))
        tmp = f"{path}.{os.getpid()}.new"
        try:
            os.makedirs(self.folder, exist_ok=True)
            with open(tmp, "wb") as fh:
                fh.write(text.encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
            _replace(tmp, path, sleep=self._sleep)
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            self.fault("system", f"The nightly summary for {night} could "
                                 f"not be written: {self._why(e)}. The night "
                                 f"journal has every line; the summary can "
                                 f"be written again once the disk takes it.",
                       action="nightly summary", state=state, night=night)
            return None
        self.record(actor="system", action="nightly summary",
                    outcome="written", reason=os.path.basename(path),
                    text=f"The nightly summary for {long_date(night)} was "
                         f"written to {path}.",
                    state=state, night=night)
        return path

    def _mismatches(self, night):
        if self.flames is None:
            return [], ("Not available: there is no flame bus in this build, "
                        "so commanded and sent were not compared.")
        try:
            return list(self.flames.mismatches(night)), ""
        except Exception as e:
            return [], (f"Not available: the flame bus did not answer "
                        f"({type(e).__name__}: {e}).")


# ------------------------------------------------------------ the summary --

# Where a show's status comes from when there is no list of shows: the
# reason the scheduler gave, which always starts with the status.
_STARTED = ("FIRED", "DELAYED START", "STARTED EARLY", "EXTRA SHOW")
_FINAL = ("DONE", "MISSED", "ABORTED", "FAULT", "SKIPPED")


def _status_of(reason):
    r = reason or ""
    for s in _FINAL:
        if r.startswith(s):
            return s
    if any(r.startswith(s) for s in _STARTED):
        return "STARTED"
    if r.startswith("DELAYED (on hold)"):
        return "DELAYED"
    return ""


def _shows_from_records(recs):
    """Each show's last known status, from the journal alone."""
    shows = {}
    for r in recs:
        n = r.get("show")
        if not n or r.get("outcome") == "refused":
            continue
        st = _status_of(r.get("reason"))
        if not st:
            continue
        row = shows.setdefault(n, {"show": n, "planned": "", "status": "",
                                   "reason": "", "started": "", "ended": ""})
        if st == "STARTED":
            row["started"] = _hms(r.get("at"))
            row["status"] = "RUNNING"
            row["reason"] = r.get("reason")
        else:
            row["status"] = st
            if st == "DONE":
                row["ended"] = _hms(r.get("at"))
            else:
                row["reason"] = r.get("reason")
    return [shows[k] for k in sorted(shows)]


def _bullets(rows, empty, limit=SUMMARY_LIST_MAX):
    if not rows:
        return [f"- {empty}"]
    out = [f"- {r}" for r in rows[:limit]]
    if len(rows) > limit:
        out.append(f"- and {len(rows) - limit} more in the journal.")
    return out


def _md(text):
    return str(text or "").replace("|", "/")


def summary_text(night, recs, *, slots=None, mismatches=(),
                 mismatch_note="", health=None, written=None, build=None,
                 read_note="", closed_by=""):
    """The nightly summary, one page of Markdown, from a night's records.

    `slots` is tonight's list as the scheduler holds it, each a dict with
    show, planned, status, reason, started and ended; without it the shows
    are worked out from the journal's own lines."""
    night = str(night)
    rows = list(slots) if slots is not None else _shows_from_records(recs)
    count = {}
    for s in rows:
        count[s["status"]] = count.get(s["status"], 0) + 1
    # What operators did: each press, in the operator's own line, and each
    # refusal. The lines an action writes about every show it touched
    # (eight "will not run" lines for one End night) are in the journal.
    ops = [r for r in recs if r.get("actor") == "operator" and (
        r.get("outcome") == "refused"
        or str(r.get("who") or "").lower() in str(r.get("text")).lower())]
    faults = [r for r in recs if r.get("fault")]
    anns = [r for r in recs if r.get("action") == "announcement"]
    drops = [r for r in recs if r.get("action") == "timecode dropout"]
    flame_recs = [r for r in recs if r.get("action") == "flame mismatch"]
    restarts = [r for r in recs if r.get("action") == "program start"
                and (r.get("data") or {}).get("restart")]
    delays = [r for r in recs if r.get("outcome") != "refused"
              and (str(r.get("reason", "")).startswith("DELAYED")
                   or r.get("action") in ("HOLD", "RESUME"))]
    log_gaps = [r for r in recs if r.get("action") == "logging resumed"]
    # How a show was started is overwritten on its slot when it ends; the
    # journal still has it.
    how_started = {}
    for r in recs:
        reason = str(r.get("reason") or "")
        if r.get("show") and r.get("outcome") == "fired" and \
                reason != "FIRED":
            how_started[r["show"]] = reason
    drop_s = sum(float((r.get("data") or {}).get("duration_s") or 0)
                 for r in drops)

    def n(k):
        return count.get(k, 0)

    shows_line = (f"{len(rows)} on the list: {n('DONE')} ran to the end, "
                  f"{n('ABORTED')} aborted, {n('FAULT')} failed or were cut "
                  f"off (FAULT), {n('MISSED')} missed, {n('SKIPPED')} "
                  f"skipped" + (f", {n('RUNNING')} still running"
                                if n("RUNNING") else "")
                  + (f", {n('DELAYED')} still delayed" if n("DELAYED")
                     else "")
                  + (f", {n('PENDING')} never reached" if n("PENDING")
                     else "") + "."
                  if rows else "No shows were on the list.")
    started_late = [n for n, why in how_started.items()
                    if why.startswith("DELAYED START")]
    flame_count = len(mismatches) + len(flame_recs)
    if mismatch_note and not flame_recs:
        flame_line = mismatch_note.rstrip(".") + "."
    else:
        flame_line = f"{flame_count}."
    if health and not health.get("ok"):
        log_line = health.get("sentence", "stopped")
    elif log_gaps:
        log_line = (f"stopped and resumed {len(log_gaps)} time(s); see "
                    f"Logging below.")
    else:
        log_line = "complete, every line written."
    when = written.strftime("%H:%M:%S (%z)") if written else "--"
    status = (build or {}).get("status", "version unknown")
    out = [f"# Night summary: {long_date(night)}", "",
           f"Written at {when} by ltcplay, {status}"
           + (f", {closed_by}" if closed_by else "") + ".",
           f"Every line of the night is in `{journal_name(night)}` beside "
           f"this file.", ""]
    if read_note:
        out += [f"Note: {read_note}", ""]
    out += ["## At a glance", "",
            f"- Shows: {shows_line}",
            f"- Started after a Hold: {len(started_late)}"
            + (f" (show {', '.join(str(n) for n in sorted(started_late))})"
               if started_late else "")
            + f". Delays and holds: {len(delays)}, listed below.",
            f"- Restarts: {len(restarts)}"
            + (" (" + ", ".join(_hms(r['at']) for r in restarts[:5]) + ")"
               if restarts else "") + ".",
            f"- Operator actions: {len(ops)}. Announcements: {len(anns)}. "
            f"Faults: {len(faults)}.",
            f"- Timecode dropouts: {len(drops)}"
            + (f", {drop_s:.1f} s in total" if drops else "") + ".",
            f"- Flame mismatches: {flame_line}",
            f"- Logging: {log_line}", ""]
    out += ["## Shows", ""]
    if rows:
        out += ["| Show | Time | Result | What happened |",
                "|---|---|---|---|"]
        for s in rows:
            what = []
            if s.get("started"):
                what.append(f"started {s['started']}")
            if s.get("ended"):
                what.append(f"ended {s['ended']}")
            how = how_started.get(s["show"])
            if how and how != s.get("reason"):
                what.append(how)
            reason = s.get("reason") or ""
            if reason and reason != "FIRED":
                what.append(reason)
            out.append(f"| {s['show']} | {_md(s.get('planned'))} | "
                       f"{_md(s.get('status'))} | {_md('; '.join(what))} |")
    else:
        out.append("- No shows were on the list.")
    out += ["", "## Delays and holds", ""]
    out += _bullets([r["line"] for r in delays], "None.")
    out += ["", "## Operator actions", ""]
    out += _bullets([r["line"] for r in ops], "None.")
    out += ["", "## Announcements", ""]
    out += _bullets([r["line"] for r in anns], "None played.")
    out += ["", "## Faults, in their own words", ""]
    out += _bullets([r["line"] for r in faults], "None.")
    out += ["", "## Timecode dropouts", ""]
    out += _bullets([r["line"] for r in drops], "None recorded.")
    out += ["", "## Flame mismatches, commanded against sent", ""]
    flame_rows = [r["line"] for r in flame_recs]
    for m in mismatches:
        try:
            flame_rows.append(
                f"{_hms(m.get('at'))}  Group {m.get('group')}: commanded and "
                f"sent disagreed for {m.get('frames')} frame(s), "
                f"{float(m.get('duration_s') or 0):.2f} s.")
        except (AttributeError, TypeError, ValueError):
            flame_rows.append(f"{m!r}")
    if not flame_rows and mismatch_note:
        out.append(f"- {mismatch_note}")
    else:
        out += _bullets(flame_rows, "None.")
    out += ["", "## Restarts", ""]
    out += _bullets([r["line"] for r in restarts], "None.")
    if log_gaps or (health and not health.get("ok")):
        out += ["", "## Logging", ""]
        out += _bullets([r["line"] for r in log_gaps]
                        + ([health["sentence"]] if health
                           and not health.get("ok") else []), "Complete.")
    return "\n".join(out) + "\n"
