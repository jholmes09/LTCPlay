"""The scheduler's contact with the world: the rule file on disk, tonight's
list on disk so a restart picks it up, the NTP check, a clock ticking the
engine, and the web routes onto it.

Imported ONLY when a schedule file is configured (`ltc serve --schedule`).
The GPL show never passes that, so on the Mac none of this is loaded, and the
selftest proves it.

It does not act. The engine in schedule.py returns effects; nothing here
performs them, because the MadMapper transport does not exist yet. Every
effect is written to the journal as "not performed" and a show the engine
starts is ended by the clock after show_len_s, marked as a dry run. There is
no route here that starts, stops or arms anything, and no switch to make it.

Everything it decides goes to the night journal and the machine log
(journal.py), written together from the same event, with the last lines
kept in memory for the page.
"""
import collections
import json
import os
import socket
import struct
import threading
import time as _time
import traceback
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from . import appdata
from . import journal
from . import schedule as sch
from . import settings as settings_mod

RULE_FILE = "ltcplay_schedule.json"
PREVIOUS_SUFFIX = ".previous.json"
TONIGHT_PREFIX = "ltcplay_tonight_"
OPERATORS_FILE = "ltcplay_operators.json"
# Who is currently operating the rig (Stream Deck operator gate, 2026-10-01).
# Separate from OPERATORS_FILE: that file is the LIST of names allowed to
# press things; this one is which of them is doing it right now.
CURRENT_OPERATOR_FILE = "ltcplay_current_operator.json"
# Which screens an operator can press things from. Beside the operator list,
# and checked the same way, so the journal never names a screen nobody has.
SCREENS_FILE = "ltcplay_screens.json"
DEFAULT_SCREENS = ("Rack screen", "Stream Deck", "Phone")
NTP_SERVER = "pool.ntp.org"
# The whole clock check, name lookup included, gets this long. It runs on its
# own thread, so even this never holds up a show.
CLOCK_CHECK_LIMIT_S = 5.0

# NTP is only ever queried once, at start. Nothing else would ever notice
# the wall clock stepping later -- an NTP step, an RTC glitch, someone
# setting it by hand -- so every tick compares how far the wall clock moved
# since the last one against how far perf_counter moved, which the OS
# clock cannot step. A disagreement past this many seconds is an
# impossible jump, not drift, and the clock stops being trusted until it
# is checked again (round 1 review of PR 25, blocker).
CLOCK_JUMP_LIMIT_S = 3600.0

# This build decides and does not act. A constant, not a setting: there is
# nothing to act WITH until the transport lands, and a switch that does
# nothing is a switch someone will flip on show night and trust.
DRY_RUN = True
DRY_RUN_NOTE = ("This build decides but does not act. No show is started, "
                "stopped or faded by the scheduler; the MadMapper link "
                "arrives in a later update.")


# ------------------------------------------------------------ where --

def data_dir():
    """Where the scheduler keeps its files: the rule file by default, and
    tonight's list. The same place as this machine's other settings: beside
    the launcher on a Mac, %LOCALAPPDATA%\\ltcplay on Windows, where the
    program folder may not be writable and must never be a synced one."""
    return appdata.folder() if appdata.WINDOWS else settings_mod.folder()


def default_rule_path():
    return os.path.join(data_dir(), RULE_FILE)


def previous_path(path):
    base = path[:-5] if path.endswith(".json") else path
    return base + PREVIOUS_SUFFIX


def tonight_path(d, folder=None):
    """Tonight's list as it stands, one file per date, so a restart picks up
    the edits, the statuses and when the last show ended."""
    return os.path.join(folder or data_dir(),
                        f"{TONIGHT_PREFIX}{d.isoformat()}.json")


def set_aside_path(path):
    """Where a saved night that could not be used is moved to."""
    return path[:-5] + ".unreadable.json"


# The Abort latch, in a file of its own (fix round 3 of #30, 2026-10-03).
# While this file EXISTS, an Abort has not been Reset, and a start with a
# show conductor attached is latched and dark. Only Reset removes it. What
# is written inside it is for a person reading the folder; nothing reads it
# back, so a file left empty by a power cut, or by a disk too full to take
# its words, still latches. That is the point of keeping it apart from
# tonight's list:
#   - creating an empty file needs no room for data, so it usually still
#     lands on a disk too full to save tonight's list;
#   - it is never held open by the program that has tonight's list open;
#   - it has no date, so a night file for the wrong date (a clock that ran
#     ahead) or none at all cannot hide it;
#   - no older ltcplay knows its name, so a downgrade that sets tonight's
#     list aside leaves it where it is.
LATCH_FILE = "ltcplay_abort_latch.json"


def latch_path(folder=None):
    return os.path.join(folder or data_dir(), LATCH_FILE)


def _fsync_folder(folder):
    """Make a new or removed name in `folder` survive a power cut. Windows
    cannot open a folder this way and does not need to."""
    if os.name == "nt":
        return
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def create_latch_marker(path, words):
    """Create the latch file if it is not there. The name is what counts:
    once the file exists it is done, even if the words could not be
    written. Raises OSError only when the file could not be created.
    True when it was created just now.

    Nothing is flushed to disk here: every flush before the show conductor
    hears the Abort is time the flames are still lit, and a slow disk can
    take 300 ms a flush. Tonight's list is flushed next, as before, and the
    caller flushes the folder (sync_latch_marker), which makes the NAME
    survive a power cut, once the conductor has been asked. The words are
    never flushed on their own: nothing reads them."""
    if os.path.exists(path):
        return False
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    try:
        os.write(fd, (json.dumps(words, indent=2) + "\n").encode("utf-8"))
    except OSError:
        pass                            # a full disk: the name still counts
    finally:
        os.close(fd)
    return True


def sync_latch_marker(path):
    """Flush the latch file's folder, so its name survives a power cut."""
    _fsync_folder(os.path.dirname(os.path.abspath(path)))


def remove_latch_marker(path, sleep_fn=None, tries=5):
    """Remove the latch file, patiently while Windows says another program
    has it open. Raises OSError if it is still there after that."""
    sleep_fn = sleep_fn or _time.sleep
    for i in range(tries):
        try:
            os.remove(path)
            break
        except FileNotFoundError:
            return
        except PermissionError:
            if i == tries - 1:
                raise
            sleep_fn(0.1 * (i + 1))
    _fsync_folder(os.path.dirname(os.path.abspath(path)))


def operators_path(folder=None):
    return os.path.join(folder or data_dir(), OPERATORS_FILE)


def parse_operators(doc):
    """The operator list from its file: {"operators": ["Andy", "Jeff"]}.
    Raises ValueError with a sentence."""
    if not isinstance(doc, dict) or set(doc) != {"operators"}:
        raise ValueError('It has to be {"operators": [names]} and nothing '
                         'else.')
    names = doc["operators"]
    if not isinstance(names, list) or not names:
        raise ValueError("operators has to be a list of at least one name.")
    out, seen = [], set()
    for n in names:
        if not isinstance(n, str) or not n.strip():
            raise ValueError(f"{n!r} is not a name.")
        if n.strip().lower() in seen:
            raise ValueError(f"{n.strip()!r} is on the list twice.")
        seen.add(n.strip().lower())
        out.append(n.strip())
    return tuple(out)


def screens_path(folder=None):
    return os.path.join(folder or data_dir(), SCREENS_FILE)


def parse_screens(doc):
    """The screen list from its file: {"screens": ["Rack screen", ...]}.
    Raises ValueError with a sentence."""
    if not isinstance(doc, dict) or set(doc) != {"screens"}:
        raise ValueError('It has to be {"screens": [names]} and nothing '
                         'else.')
    names = doc["screens"]
    if not isinstance(names, list) or not names:
        raise ValueError("screens has to be a list of at least one name.")
    out, seen = [], set()
    for n in names:
        if not isinstance(n, str) or not n.strip():
            raise ValueError(f"{n!r} is not a screen name.")
        if n.strip().lower() in seen:
            raise ValueError(f"{n.strip()!r} is on the list twice.")
        seen.add(n.strip().lower())
        out.append(n.strip())
    return tuple(out)


def load_screens(folder=None):
    """(names, sentence). The screens things may be pressed from. A missing
    file is written with the defaults; a broken one leaves the defaults in
    force and says why."""
    path = screens_path(folder)
    default = ", ".join(DEFAULT_SCREENS)
    if not os.path.exists(path):
        try:
            write_json_atomic(path, {"screens": list(DEFAULT_SCREENS)})
            why = f"The screen list was written to {path} with {default}."
        except OSError as e:
            why = (f"The screen list could not be written to {path}: {e}. "
                   f"Using {default}.")
        return DEFAULT_SCREENS, why
    try:
        with open(path, encoding="utf-8-sig") as fh:
            names = parse_screens(json.load(fh))
    except (OSError, ValueError) as e:
        return DEFAULT_SCREENS, (
            f"The screen list {path} could not be used: "
            f"{str(e).rstrip('.')}. Using {default} until it is fixed.")
    return names, ""


def load_operators(folder=None):
    """(names, sentence). The list of people who may press things. A missing
    file is written with the defaults, Andy and Jeff, so there is something
    to edit; a broken one leaves the defaults in force and says why.
    Adding and removing names from a page comes later."""
    path = operators_path(folder)
    if not os.path.exists(path):
        try:
            write_json_atomic(path, {"operators": list(
                sch.DEFAULT_OPERATORS)})
            why = (f"The operator list was written to {path} with "
                   f"{', '.join(sch.DEFAULT_OPERATORS)}.")
        except OSError as e:
            why = (f"The operator list could not be written to {path}: "
                   f"{e}. Using {', '.join(sch.DEFAULT_OPERATORS)}.")
        return sch.DEFAULT_OPERATORS, why
    try:
        with open(path, encoding="utf-8-sig") as fh:
            names = parse_operators(json.load(fh))
    except (OSError, ValueError) as e:
        return sch.DEFAULT_OPERATORS, (
            f"The operator list {path} could not be used: "
            f"{str(e).rstrip('.')}. Using "
            f"{', '.join(sch.DEFAULT_OPERATORS)} until it is fixed.")
    return names, ""


def current_operator_path(folder=None):
    return os.path.join(folder or data_dir(), CURRENT_OPERATOR_FILE)


def load_current_operator(folder, operators):
    """(name, sentence). Who is currently operating the rig, for the Stream
    Deck's operator gate (Jeff, 2026-10-01: a name is chosen before the
    deck's actions unlock, with no separate password -- Andy's own physical
    key is the accountability control). No file, or a name no longer on the
    operator list (the list was edited since it was chosen): "" and a
    sentence, never a guess. This is bookkeeping, not a safety rule: an
    empty current operator blocks nothing on its own; see streamdeck.py."""
    path = current_operator_path(folder)
    if not os.path.exists(path):
        return "", ""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict) or set(doc) != {"current_operator"}:
            raise ValueError('it has to be {"current_operator": name} and '
                             'nothing else.')
        name = doc["current_operator"]
        if not isinstance(name, str):
            raise ValueError("current_operator has to be a name.")
    except (OSError, ValueError) as e:
        return "", (f"The current operator file {path} could not be used: "
                    f"{str(e).rstrip('.')}. No operator is selected until "
                    f"one is chosen again.")
    if not name:
        return "", ""
    names = {n.lower(): n for n in operators}
    if name.lower() not in names:
        return "", (f"{name!r} was the current operator but is no longer "
                    f"on the operator list. No operator is selected until "
                    f"one is chosen again.")
    return names[name.lower()], ""


def save_current_operator(folder, name):
    write_json_atomic(current_operator_path(folder),
                      {"current_operator": name})


# ------------------------------------------------------------ the file --

def load_rule(path):
    """Read and validate the rule file. Every failure is one sentence (or a
    list of them) naming the file."""
    try:
        # utf-8-sig: Notepad on Windows puts a byte order mark at the front of
        # a UTF-8 file, and plain utf-8 reads that as garbage before the {.
        with open(path, encoding="utf-8-sig") as fh:
            text = fh.read()
    except FileNotFoundError:
        raise sch.RuleError([f"There is no schedule file at {path}."])
    except OSError as e:
        raise sch.RuleError([f"The schedule file {path} cannot be read: "
                             f"{e.strerror or e}."])
    return sch.parse_rule(text, where=path)


def _replace(src, dst, replace_fn, sleep_fn, tries):
    """os.replace, patiently. On Windows a file that another program has
    open (an editor, a virus scanner, a sync client) cannot be replaced, and
    the refusal is usually gone a moment later."""
    for i in range(tries):
        try:
            replace_fn(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            sleep_fn(0.1 * (i + 1))


def write_json_atomic(path, doc, replace_fn=None, sleep_fn=None, tries=5):
    """Write JSON in one step or not at all: a temp file beside it, flushed
    to disk, then os.replace, retried while Windows says another program has
    the file open. Raises OSError with a sentence if it never got through."""
    replace_fn = replace_fn or os.replace
    sleep_fn = sleep_fn or _time.sleep
    tmp = _write_temp(path, doc)
    try:
        _replace(tmp, path, replace_fn, sleep_fn, tries)
    except PermissionError:
        raise OSError(f"{path} is held open by another program, so it could "
                      f"not be replaced after {tries} tries.")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _write_temp(path, doc):
    tmp = f"{path}.{os.getpid()}.new"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    return tmp


def save_rule(path, doc, replace_fn=None, sleep_fn=None, tries=5):
    """Validate and write a new rule file, keeping the one it replaces.

    The new version number is one more than the file on disk. The old file is
    copied to <name>.previous.json first, then the new one replaces it in one
    step, so a crash at any point leaves a readable rule file. Returns the
    Rule as saved. Tonight's list is NOT touched: edits to tonight live in
    memory, and a rule change applies from the next night."""
    replace_fn = replace_fn or os.replace
    sleep_fn = sleep_fn or _time.sleep
    if isinstance(doc, (str, bytes)):
        doc = json.loads(doc)
    doc = dict(doc)
    old_version = 0
    old_text = None
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as fh:
            old_text = fh.read()
        try:
            old_version = int(json.loads(old_text).get("version") or 0)
        except (ValueError, AttributeError, TypeError):
            old_version = 0
    doc["version"] = old_version + 1
    rule = sch.parse_rule(doc, where=path)          # refuse before writing
    temps = []
    try:
        if old_text is not None:
            prev = previous_path(path)
            ptmp = f"{prev}.{os.getpid()}.new"
            with open(ptmp, "w", encoding="utf-8") as fh:
                fh.write(old_text)
                fh.flush()
                os.fsync(fh.fileno())
            temps.append(ptmp)
            _replace(ptmp, prev, replace_fn, sleep_fn, tries)
            temps.remove(ptmp)
        tmp = _write_temp(path, sch.rule_to_doc(rule))
        temps.append(tmp)
        _replace(tmp, path, replace_fn, sleep_fn, tries)
        temps.remove(tmp)
    except PermissionError:
        raise OSError(
            f"The schedule was not saved. {path} is held open by another "
            f"program, so it could not be replaced after {tries} tries. Close "
            f"whatever has it open and save again. The file on disk is "
            f"unchanged.")
    finally:
        for t in temps:
            try:
                os.unlink(t)
            except OSError:
                pass
    return rule


# ------------------------------------------------------------ NTP --

NTP_EPOCH = 2208988800          # seconds from 1900 to 1970


def _ntp_ts(b):
    secs, frac = struct.unpack("!II", b)
    return secs - NTP_EPOCH + frac / 2 ** 32


def parse_sntp(packet, t1, t4):
    """Offset in seconds from an SNTP reply: positive means this machine is
    behind. t1 is when the request left, t4 when the reply came, both on
    this machine's clock."""
    if len(packet) < 48:
        raise ValueError(f"The time server's reply was {len(packet)} bytes, "
                         f"not 48.")
    mode = packet[0] & 7
    if mode not in (4, 5):
        raise ValueError(f"The time server's reply is mode {mode}, not a "
                         f"server reply.")
    t2 = _ntp_ts(packet[32:40])
    t3 = _ntp_ts(packet[40:48])
    if t3 <= 0:
        raise ValueError("The time server sent no time.")
    return ((t2 - t1) + (t3 - t4)) / 2.0


def sntp_query(server=NTP_SERVER, timeout=2.0, sock_factory=None,
               wall=None):
    """Ask one time server once. Returns the offset in seconds. Raises
    OSError or ValueError; the caller turns that into a sentence."""
    wall = wall or _time.time
    make = sock_factory or (lambda: socket.socket(socket.AF_INET,
                                                  socket.SOCK_DGRAM))
    s = make()
    try:
        s.settimeout(timeout)
        req = b"\x1b" + 47 * b"\0"          # version 3, client
        t1 = wall()
        s.sendto(req, (server, 123))
        data, _ = s.recvfrom(512)
        t4 = wall()
    finally:
        s.close()
    return parse_sntp(data, t1, t4)


def check_clock(query=None, server=NTP_SERVER, limit_s=CLOCK_CHECK_LIMIT_S):
    """(level, sentence, offset). Never sets the clock.

    The query runs on a thread of its own and gets `limit_s` in total. The
    socket timeout does not cover the name lookup, which can hang for far
    longer on a rack network with no way out, so the limit is enforced from
    outside; a query that overruns is left to finish on its own."""
    query = query or (lambda: sntp_query(server))
    box = {}

    def run():
        try:
            box["offset"] = float(query())
        except Exception as e:           # anything it raises is a sentence
            box["error"] = e

    t = threading.Thread(target=run, daemon=True, name="ltcplay-ntp")
    t.start()
    t.join(limit_s)
    if t.is_alive() or "offset" not in box:
        level, text = sch.judge_clock_offset(None, server)
        why = (f"it did not answer within {limit_s:g} s"
               if t.is_alive() else f"{box.get('error')}")
        return level, f"{text} The time server check failed: {why}.", None
    level, text = sch.judge_clock_offset(box["offset"], server)
    return level, text, box["offset"]


# ------------------------------------------------------------ the runner --

def _utc_now():
    return datetime.now(timezone.utc)


class _ConductorCall:
    """One request to the show conductor, as the scheduler decided it."""

    def __init__(self, label, method, who, screen, seq=0):
        self.label, self.method = label, method
        self.who, self.screen = who, screen
        self.seq = seq
        self.ok, self.sentence = None, ""
        self.done = threading.Event()


class _ConductorCalls:
    """Every show conductor request, in the order the scheduler decided
    them, made on ONE thread of its own, never with the scheduler's lock
    held. One FIFO and one thread is what keeps Hold, Resume and Abort in
    order now that they no longer run inside the lock: two requests can
    never be made at once or overtake each other. A conductor request that
    is slow (its lock is held across a device call) holds up only the
    requests behind it, never a tick or a status poll.

    With ONE exception: an Abort never waits more than URGENT_WAIT_S behind
    another request. If the line has not reached it by then (the request
    in front of it is stuck, or the line's thread has died), it is taken
    out of the line and made at once on a thread of its own. The requests
    still waiting in front of it that the Abort supersedes (a Hold, a
    Resume, a show start, and above all a Reset, which was pressed before
    this Abort and must not clear it) are not sent at all; intermission and
    show_stopped stay in the line (they only ever take the rig dark, and
    the conductor ignores them while latched). Requests decided after the
    Abort stay in the line, in order.

    The thread starts with the first request, so a Service with no
    conductor never has one."""

    URGENT_WAIT_S = 0.25
    SUPERSEDED_BY_ABORT = ("reset", "hold", "resume", "show_starting")

    def __init__(self, run_one, overtaken=None, clock=None):
        self._run_one = run_one
        self._overtaken = overtaken or (lambda *a: None)
        self._clock = clock or _time.monotonic
        self._q = collections.deque()
        self._cv = threading.Condition()
        self._busy = False
        self._current = None        # the call the line is making now
        self._since = None          # since when, on self._clock
        self._side = {}             # Aborts sent beside the line: start time
        self._thread = None

    def put(self, call):
        with self._cv:
            self._q.append(call)
            if self._thread is None:
                self._start()
            self._cv.notify_all()
            urgent = call.method == "abort" and (
                self._busy or len(self._q) > 1 or not self._alive())
        if urgent:
            threading.Thread(target=self._mind, args=(call,), daemon=True,
                             name="ltcplay-conductor-abort").start()

    def _start(self):
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="ltcplay-conductor-calls")
        self._thread.start()

    def _alive(self):
        return self._thread is None or self._thread.is_alive()

    def _loop(self):
        while True:
            with self._cv:
                while not self._q:
                    self._cv.wait()
                call = self._q.popleft()
                self._busy = True
                self._current, self._since = call, self._clock()
                self._cv.notify_all()
            try:
                self._run_one(call)
            except BaseException:       # never kill the only caller
                pass
            finally:
                call.done.set()
                with self._cv:
                    self._busy = False
                    self._current = self._since = None
                    self._cv.notify_all()

    def _mind(self, call):
        """An Abort that found the line busy: give the line URGENT_WAIT_S to
        reach it, then make it here instead."""
        with self._cv:
            if self._cv.wait_for(lambda: call not in self._q,
                                 self.URGENT_WAIT_S):
                return                  # the line reached it in time
            self._q.remove(call)
            ahead = self._current
            age = (self._clock() - self._since) if ahead is not None else 0.0
            dropped = [c for c in self._q if c.seq < call.seq and
                       c.method in self.SUPERSEDED_BY_ABORT]
            for c in dropped:
                self._q.remove(c)
            self._side[call] = self._clock()
        try:
            self._overtaken(call, ahead, age, dropped)
        except Exception:
            pass
        try:
            self._run_one(call)
        except BaseException:
            pass
        finally:
            call.done.set()
            with self._cv:
                self._side.pop(call, None)
                self._cv.notify_all()

    def health(self):
        """{"alive", "stuck", "age_s", "waiting"}: whether the line's thread
        is running, the request that has gone longest without an answer
        (the line's, or an Abort sent beside it) and for how long."""
        with self._cv:
            now = self._clock()
            running = list(self._side.items())
            if self._current is not None:
                running.append((self._current, self._since))
            stuck, age = None, 0.0
            for c, since in running:
                if now - since >= age:
                    stuck, age = c, now - since
            return {"alive": self._alive(), "stuck": stuck, "age_s": age,
                    "waiting": len(self._q)}

    def aborts_beside(self):
        """The Aborts sent beside the line that have not answered yet."""
        with self._cv:
            return [c for c in self._side if c.method == "abort"]

    def revive(self):
        """Start the line again if its thread has died. True if it had."""
        with self._cv:
            if self._alive():
                return False
            self._busy = False
            self._current = self._since = None
            self._start()
            self._cv.notify_all()
            return True

    def flush(self, timeout):
        with self._cv:
            return self._cv.wait_for(
                lambda: not self._q and not self._busy and not self._side,
                timeout)


class Service:
    """One scheduler, ticking, with the answers the page needs.

    `clock` returns an aware datetime and `ntp_query` returns an offset; both
    are injected so the selftest never waits and never touches the network.
    """

    TICK_S = 0.25
    JOURNAL = journal.MEMORY_LINES
    # The ring buffer's pace: 5 samples a second (handoff section 9).
    SAMPLE_S = 1.0 / journal.RING_HZ

    def __init__(self, path, clock=None, ntp_query=None, state_dir=None,
                 clock_limit_s=CLOCK_CHECK_LIMIT_S, log_dir=None,
                 flame_provider=None, logbook=None, perf_counter=None,
                 conductor=None):
        self.path = path
        self.clock = clock or _utc_now
        # The Fire & Ice show conductor (ltcplay.conductor.Conductor), or
        # None (the GPL path, and every build before PR #17's device layer
        # lands). See _drive_conductor: without one, every effect is only
        # journaled as not performed, exactly as before this was added.
        self.conductor = conductor
        # Requests to it, in order, off the scheduler's lock. See
        # _ConductorCalls and _queue_conductor.
        self._calls = _ConductorCalls(self._run_conductor_call,
                                      overtaken=self._abort_overtook)
        # Every conductor request gets the next number, so a Reset can tell
        # whether an Abort was decided after it was pressed.
        self._call_seq = 0
        self._abort_seq = 0
        # A stuck or dead line of conductor requests, once it has been
        # journaled as a fault: {"key", "text"}. See _watch_conductor.
        self._conductor_trouble = None
        self.ntp_query = ntp_query
        # Injected so a test can move the wall clock and perf_counter apart
        # on purpose, deterministically, with nothing asleep and no real
        # time passing. See _watch_clock().
        self._perf_counter = perf_counter or _time.perf_counter
        self._last_wall = None
        self._last_perf = None
        self._clock_trust_lost = False
        self.state_dir = state_dir or data_dir()
        self.clock_limit_s = clock_limit_s
        self.persist_error = ""
        # The Abort latch on disk (fix round 3): the loud line said while it
        # is not safely saved, a Reset's removal of the latch file still to
        # do, when to try either again, and the night whose saved list could
        # not be read this run (which latches, see _boot_latch).
        self._latch_trouble = None
        self._marker_clear_pending = False
        self._latch_retry_at = None
        self._latch_unsynced = False
        self._unreadable_night = None
        self.lock = threading.RLock()
        # Depth of this thread's own nesting of _locked() (tonight_view()
        # and state_view() both call tick() while already holding the
        # lock, so a plain "did the innermost `with` exit" test is not
        # enough to know the RLock has actually been released). Only ever
        # touched while self.lock is held, so a plain int is safe: no two
        # threads can be adjusting it at once by construction of the lock
        # itself. See _locked().
        self._lock_depth = 0
        # Calls queued by _apply() (on_show_started, so far) to run once
        # self.lock is TRULY free, never while any caller on this thread
        # still holds it at any depth. See _locked().
        self._pending_hooks = []
        self.rule = None
        self.error = ""
        self.machine = None
        self.clock_check = None
        # The night journal and the machine log. Given a state folder (the
        # selftest's), the logs go in it; otherwise in this machine's own
        # data folder, %LOCALAPPDATA%\ltcplay\nights on Windows.
        if log_dir is None and state_dir is not None:
            log_dir = os.path.join(state_dir, journal.FOLDER)
        self.logbook = logbook or journal.Logbook(
            log_dir, clock=self.clock, tz=self._tz,
            flame_provider=flame_provider, memory=self.JOURNAL,
            state=self._state_name, night=self._night)
        # The page's lines ARE the journal's records: one deque, not a copy.
        self.journal = self.logbook.memory
        self._summarised = set()
        self._looked_back = False
        self._pruned_for = None
        self._hk_busy = False
        self._tick_fault = None
        self._born = self.clock()
        self.operators, why = load_operators(self.state_dir)
        self.screens, why_screens = load_screens(self.state_dir)
        self.current_operator, why_current = load_current_operator(
            self.state_dir, self.operators)
        # A plain callable, or None: set by web.py when an announcements
        # service is ALSO configured (serve()'s own job, see its module
        # docstring). Called the instant a show starts, so a playing
        # announcement can be told to stop without waiting on its own
        # next status() poll. schedule_service.py never imports announce.py
        # to make this call; it only ever calls whatever was set here.
        self.on_show_started = None
        # Bumped by _apply() every time the machine crosses into or out of
        # a held or paused state. See hold_for_announcement and
        # hold_still_claimed: the second, read-only check an announcement
        # makes right before its stream opens compares against the epoch
        # it captured at claim time, not just the state, so a Resume that
        # happens during the file read is never silently undone (review
        # round 2, 2026-09-26: audit15_resume_race.py).
        self.hold_epoch = 0
        self._stop = threading.Event()
        self._thread = None
        self._clock_thread = None
        self._sample_thread = None
        self._started = False
        failed = self._load_rule()
        self._log(self.logbook.started, state=self._state_name(),
                  night=self._night())
        if failed:
            self._journal_rule_error()
        if why:
            self._journal_line("system", why, action="operators")
        if why_screens:
            self._journal_line("system", why_screens, action="screens")
        if why_current:
            self._journal_line("system", why_current, action="operator")

    @contextmanager
    def _locked(self):
        """Exactly like `with self.lock:`, except anything _apply() queued
        onto self._pending_hooks (on_show_started, so far) runs strictly
        AFTER self.lock is truly free -- not after some inner, reentrant
        `with` exits while an OUTER one (tonight_view() and state_view()
        both call tick() while already holding the lock) still holds it.
        _lock_depth is what tells the two apart: it only reaches zero, and
        only then is the pending list drained, on the outermost exit.

        This exists because a hook can do real work (announce.py's
        on_show_started starts a fade; even that is meant to be
        instantaneous, but nothing here should have to trust every future
        hook to be). self.lock gates tick() itself and every operator
        action -- Abort, Hold, Start now, every status poll -- so ANY hook
        run while it is held stalls all of them for as long as the hook
        takes. Round 2 of review measured this directly: a synthetic 2 s
        stream.stop() inside on_show_started, called the old way (in-line,
        inside _apply()), stalled Service.lock for the full 2 s
        (audit13b_scheduler_stall.py). The hook itself was also fixed to
        never do that work in the first place; this is the second,
        independent half of the fix, so a future hook cannot reopen the
        same hole.

        Each pending hook is started on a thread of its own, not called
        in-line even here: releasing the lock is not enough on its own to
        make tick() itself "return in under 50 ms" (the review's own
        phrasing) if it then sits waiting for the hook to finish anyway.
        The lock and tick()'s own return are both freed from the hook's
        timing; only the hook's OWN thread ever waits on it.
        """
        self.lock.acquire()
        self._lock_depth += 1
        try:
            yield
        finally:
            self._lock_depth -= 1
            outermost = self._lock_depth == 0
            pending = []
            if outermost:
                pending, self._pending_hooks = self._pending_hooks, []
            self.lock.release()
            # Reviewer note: each hook gets its own unpooled daemon thread,
            # so a hook must never block -- nothing here caps how many run
            # at once or reaps them when they finish.
            for hook in pending:
                threading.Thread(target=self._run_hook, args=(hook,),
                                 daemon=True,
                                 name="ltcplay-announce-hook").start()

    def _run_hook(self, hook):
        try:
            hook()
        except Exception as e:
            with self._locked():
                self._journal_line(
                    "system", f"The announcements hook failed: {e}.",
                    action="announce hook", outcome="error")

    # -- the rule -------------------------------------------------------
    def reload(self):
        """Read the rule file. A bad file leaves the scheduler with no night
        and the reason on the page; it never takes the server down with it."""
        with self._locked():
            if self._load_rule():
                self._journal_rule_error()
            return self.rule

    def _load_rule(self):
        """True when the rule file could not be used."""
        try:
            self.rule = load_rule(self.path)
            self.error = ""
            return False
        except (ValueError, OSError) as e:
            self.rule = None
            self.error = str(e)
            self.machine = None
            return True

    def _journal_rule_error(self):
        self._journal_line("system", "The schedule file was not loaded, so "
                           "no show is scheduled. " + self.error,
                           action="load schedule", outcome="failed",
                           fault=True)

    # -- journal ----------------------------------------------------------
    # What the engine calls a fault. Each of these lines is written as a
    # fault, so it has to be a sentence, and it lands in the summary's list.
    FAULT_OUTCOMES = ("fault", "flagged", "recorded as a fault")

    def _tz(self):
        return self.rule.tz if self.rule else None

    def _state_name(self):
        m = self.machine
        if m is not None:
            return m.state
        return "NO SCHEDULE" if self.rule is None else sch.BOOT

    def _night(self, now=None):
        """Which night a line belongs to: tonight's list while there is
        one, so a show that runs past midnight stays on its own night."""
        if self.machine is not None:
            return self.machine.date
        return self.logbook.night_of(now)

    def _log(self, fn, *args, **kw):
        """Call the journal. Nothing it does, or fails to do, may stop the
        scheduler: a bug in a log line is itself written down, as a fault,
        and the night carries on."""
        try:
            return fn(*args, **kw)
        except Exception as e:
            try:
                return self.logbook.fault(
                    "system", f"A journal line could not be written as it "
                    f"was made ({type(e).__name__}: {e}). That is a bug in "
                    f"ltcplay; the scheduler carried on.",
                    action="journal", state=self._state_name(),
                    night=self._night())
            except Exception:
                return None

    def _journal_line(self, actor, text, **extra):
        fault = extra.pop("fault", False)
        kw = dict(actor=actor, action=extra.pop("action", "note"),
                  outcome=extra.pop("outcome", "done"),
                  reason=extra.pop("reason", text), text=text,
                  state=self._state_name(), night=self._night(),
                  show=extra.pop("show", None), data=extra or None)
        if fault:
            kw["fault"] = True
        return self._log(self.logbook.record, **kw)

    def _record_logevent(self, le):
        op = le.actor == "operator"
        return self._log(
            self.logbook.record, actor=le.actor, action=le.action,
            outcome=le.outcome, reason=le.reason, text=le.text,
            state=le.state, to_state=le.to_state, at=le.at,
            night=self._night(), show=le.show or None,
            # An operator event the engine refused for not naming who or
            # which screen still says so in the journal, in words.
            who=(le.who.strip() or "unnamed operator") if op else None,
            screen=(le.screen.strip() or "unnamed screen") if op else None,
            fault=le.outcome in self.FAULT_OUTCOMES)

    # Effect-kind bundles schedule.py always emits together for one show
    # conductor action (see its module docstring, and entry_effects and
    # _abort_effects). Checked by subset, most specific first, so the
    # abort and closing bundles -- which both carry ZERO_FLAME_CUES and
    # STOP_CONDUCTOR -- are never confused: closing never also carries
    # BLANK_LASERS or FADE_MUSIC_OUT, but the check order makes that true
    # by construction rather than by relying on it.
    _HOLD_EFFECTS = frozenset((sch.ZERO_FLAME_CUES, sch.BLANK_LASERS,
                               sch.FREEZE_SHOW, sch.FADE_MUSIC_OUT))
    _RESUME_EFFECTS = frozenset((sch.RESUME_SHOW, sch.FADE_MUSIC_IN,
                                 sch.UNBLANK_LASERS))
    _ABORT_EFFECTS = frozenset((sch.ZERO_FLAME_CUES, sch.BLANK_LASERS,
                                sch.FADE_MUSIC_OUT, sch.FADE_VIDEO_OUT,
                                sch.FADE_PIXELS, sch.STOP_CONDUCTOR))
    # Of the closing bundle, the conductor's intermission() performs only
    # the flame cue zero (and blanks the lasers, which closing does not
    # list). STOP_CONDUCTOR, FADE_PIXELS and BLACKOUT have no conductor
    # method, so they stay "not performed".
    _CLOSING_CLAIMS = frozenset((sch.ZERO_FLAME_CUES,))

    def _drive_conductor(self, out, ev):
        """What the show conductor (conductor.Conductor) is to be asked to
        do for this outcome: a list of (label, method, claimed effect
        kinds), in order, possibly empty. Nothing is CALLED here: _apply
        queues these only after tonight is saved and the journal written
        (schedule.py's effects contract, rule 1), and _ConductorCalls calls
        them in order, on its own thread, outside the scheduler's lock.

        Empty without a conductor (every build before PR #17's device layer
        lands, and the GPL path, which never even passes a schedule), and
        for a refused outcome.

        Matched mostly by which effect KINDS came back (schedule.py's
        contract for whatever performs its effects), with three
        exceptions that the kinds alone cannot tell apart:
          - the Abort bundle is an Abort only when the operator pressed
            Abort. A failed start and a show cut by a restart fade the
            same way, but go to show_stopped(): dark, no disarm, no latch
            (DEFAULT pending Jeff's confirmation, 2026-10-02).
          - START_SHOW is NOT the conductor's: it brings the rig up only
            for a show cue that is playing (show_starting refuses "no show
            cue is playing" otherwise). It is told once the show is
            confirmed running, on SHOW_CONFIRMED, and only if the show is
            not paused by then.
          - every way out of a show tells it so, the last show of the
            night and Close for the night included (CLOSING), not only the
            intermission: intermission() zeroes the flame cues and blanks
            the lasers.
          - a start (BOOT_DONE) that leaves the rig dark sends the dark
            sequence again, show_stopped(), whatever happened before.
        `claimed` is only the effects the conductor really performs; the
        rest are journaled as not performed."""
        if self.conductor is None or out.refused:
            return []
        kinds = {e.kind for e in out.effects}
        plan = []
        if kinds >= self._HOLD_EFFECTS:
            plan.append(("Hold", "hold", self._HOLD_EFFECTS))
        elif kinds >= self._RESUME_EFFECTS:
            plan.append(("Resume", "resume", self._RESUME_EFFECTS))
        elif kinds >= self._ABORT_EFFECTS:
            if ev.kind == sch.ABORT:
                plan.append(("Abort", "abort", self._ABORT_EFFECTS))
            else:
                plan.append(("Show stopped", "show_stopped",
                             self._ABORT_EFFECTS))
        if sch.BLACKOUT in kinds:
            plan.append(("Out of the show", "intermission",
                         self._CLOSING_CLAIMS))
        elif sch.INTERMISSION in kinds or sch.PRESHOW_LOOK in kinds:
            plan.append(("Out of the show", "intermission", frozenset()))
        if ev.kind == sch.SHOW_CONFIRMED and \
                out.machine.state == sch.SHOW:
            plan.append(("Show start", "show_starting", frozenset()))
        if ev.kind == sch.BOOT_DONE and out.machine.dark and \
                not any(p[1] == "show_stopped" for p in plan):
            # A start (or a restart) while the rig is meant to be dark: the
            # dark sequence it was sent before may never have gone out (the
            # process can die after the Abort or failed start was saved but
            # before the conductor call ran), and a fresh conductor knows
            # nothing of it. So it is always sent again.
            plan.append((self.DARK_AGAIN, "show_stopped", frozenset()))
        return plan

    def _record(self, out, now, plan=()):
        for le in out.log:
            if self.conductor is not None and le.action == sch.ABORT and \
                    sch.NOTHING_DISARMED in le.text:
                # The engine never disarms; the conductor's abort() does.
                le = replace(le, text=le.text.replace(
                    sch.NOTHING_DISARMED, self.CONDUCTOR_DISARMS))
            self._record_logevent(le)
        claimed = set()
        for label, method, kinds in plan:
            mine = [e for e in out.effects if e.kind in kinds]
            claimed |= set(kinds)
            what = (f": {', '.join(self._desc(e) for e in mine)}"
                    if mine else "")
            extra = self.CONDUCTOR_SAYS.get(
                label, self.CONDUCTOR_SAYS.get(method, ""))
            self._journal_line(
                "system", f"Sent to the show conductor, {label}{what}."
                          f"{extra} Its own line says what it did.",
                action="conductor", outcome="sent",
                reason=f"{method} asked of the show conductor")
        for eff in out.effects:
            if eff.kind in claimed:
                continue
            self._journal_line(
                "system", f"Not performed, dry run: {self._desc(eff)}.",
                action=eff.kind, outcome="not performed",
                reason="dry run, no transport in this build",
                show=eff.show or None)

    CONDUCTOR_DISARMS = ("The show conductor also sends a disarm to every "
                         "flame group; its own line says whether it went.")
    DARK_AGAIN = "Dark again after the start"
    CONDUCTOR_SAYS = {
        DARK_AGAIN: (" ltcplay started while the rig was meant to be dark "
                     "(after an Abort, a failed start or a cut show), so the "
                     "dark sequence is sent again: flame cues zero, lasers "
                     "blanked, video, pixels and music down. This does not "
                     "disarm anything; an Abort not yet Reset stays latched "
                     "here until Reset."),
        "show_stopped": (" The rig goes dark and stays dark until an "
                         "operator presses Start now or the next show "
                         "starts. No flame group is disarmed and nothing "
                         "is latched, so no Reset is needed (DEFAULT "
                         "pending Jeff's confirmation)."),
        "intermission": (" Out of the show: flame cues to zero and the "
                         "lasers blanked."),
    }

    @staticmethod
    def _desc(eff):
        return eff.kind + (f" show {eff.show}" if eff.show else "") + \
            (f" over {eff.seconds:g} s" if eff.seconds else "")

    # -- the show conductor's calls, in order, off the scheduler's lock -------
    def _queue_conductor(self, plan, ev):
        if not plan:
            return
        op = ev.actor == "operator"
        who = ev.who if op else "the scheduler"
        screen = ev.screen if op else ""
        for label, method, _kinds in plan:
            call = self._new_call(label, method, who, screen)
            if method == "abort":
                self._abort_seq = call.seq
            self._calls.put(call)

    def _new_call(self, label, method, who, screen):
        self._call_seq += 1
        return _ConductorCall(label, method, who, screen, self._call_seq)

    def _abort_overtook(self, call, ahead, age, dropped):
        """On the Abort's own thread: it was sent ahead of a request that
        had not answered, and the requests in `dropped`, decided before it
        and superseded by it, are not sent at all."""
        with self._locked():
            what = (f"{ahead.label}, which had not answered for {age:.1f} s"
                    if ahead is not None else
                    "the requests in front of it, because the line of "
                    "requests to the show conductor was not moving")
            others = [c.label for c in dropped if c.method != "reset"]
            self._journal_line(
                "system", f"Abort was sent to the show conductor at once, "
                f"ahead of {what}."
                + (f" Not sent, because the Abort supersedes them: "
                   f"{', '.join(others)}." if others else ""),
                action="conductor", outcome="sent ahead")
            for r in dropped:
                r.ok = False
                r.sentence = f"{r.label} was not sent: an Abort overtook it."
                if r.method == "reset":
                    r.sentence = ("Reset was not sent: an Abort was pressed "
                                  "after it. Press Reset again once the rig "
                                  "is dark.")
                    self._log(self.logbook.record, actor="operator",
                              action="reset", outcome="refused",
                              reason=r.sentence,
                              text=f"{r.who}'s Reset on the {r.screen} was "
                                   f"not sent. {r.sentence}",
                              state=self._state_name(), night=self._night(),
                              who=r.who, screen=r.screen)
                r.done.set()

    def _run_conductor_call(self, call):
        """On _ConductorCalls' thread. One conductor request, whatever it
        does or raises, then one journal line for it. Anything but a good
        Result is a fault, raised on the scheduler so the page shows it."""
        try:
            r = getattr(self.conductor, call.method)(call.who, call.screen)
            ok = getattr(r, "ok", None) is True
            said = str(getattr(r, "sentence", "") or "")
            if not hasattr(r, "ok"):
                said = (f"it returned {r!r}, not a Result, so it counts as "
                        f"not done")
        except BaseException as e:      # SystemExit too: never the thread
            ok, said = False, f"it raised {type(e).__name__}: {e}"
        with self._locked():
            if call.method == "reset":
                ok, said = self._after_reset(call, ok, said)
            call.ok, call.sentence = ok, said
            if call.method == "reset":
                self._log(self.logbook.record, actor="operator",
                          action="reset", outcome="done" if ok else "refused",
                          reason=said or "Reset",
                          text=f"{call.who} pressed Reset on the "
                               f"{call.screen}. {said}".strip(),
                          state=self._state_name(), night=self._night(),
                          who=call.who, screen=call.screen)
            elif ok:
                self._journal_line(
                    "system", f"Show conductor, {call.label}: {said}".strip(),
                    action="conductor", outcome="done", reason=said or "done")
            else:
                what = (f"The show conductor did not carry out "
                        f"{call.label}: {said}")
                if self.machine is not None:
                    self._apply(sch.Event(sch.FAULT_RAISED, "system",
                                          detail=what))
                else:
                    self._journal_line("system", what, action="conductor",
                                       outcome="failed", fault=True)

    def _after_reset(self, call, ok, said):
        """The scheduler's own Abort latch (saved in tonight's file) after
        the conductor answered a Reset. Cleared by a Reset that worked, and
        also by one the conductor refused only because it has nothing
        latched (ltcplay restarted since the Abort, so this conductor never
        saw it, or the Abort never reached it): the operator's Reset is
        what ends the Abort either way. Never cleared by a Reset pressed
        before the latest Abort was decided."""
        m = self.machine
        if m is None or not m.abort_latched:
            return ok, said
        if self._calls.aborts_beside():
            # The reverse race (review round 3, r7): an Abort sent beside a
            # stuck line is itself waiting on the conductor, and this
            # Reset, pressed after it, got there first. The Abort lands
            # next and latches the conductor again, so this Reset ends
            # nothing: the scheduler keeps its latch.
            return False, self.RESET_BEFORE_ABORT_LANDED
        try:
            still = bool(getattr(self.conductor, "latched", False))
        except Exception:
            still = True
        if not ok and still:
            return ok, said             # e.g. the Abort is still fading
        if call.seq < self._abort_seq:
            return False, ("Reset was pressed before the latest Abort, so "
                           "that Abort is still in force. Press Reset "
                           "again.")
        if not ok:
            ok, said = True, ("Reset. The show conductor had nothing "
                              "latched (ltcplay restarted since the Abort, "
                              "or the Abort never reached it), so the "
                              "scheduler's own Abort latch is what was "
                              "cleared. The rig stays dark until a show "
                              "starts.")
        self.machine = replace(m, abort_latched=False)
        self._unreadable_night = None
        self._marker_clear_pending = True
        if self._save_tonight():
            self._clear_latch_files(m.date)
        else:
            said += (" The cleared latch could not be saved, so if ltcplay "
                     "restarts before it is, the rig starts dark again and "
                     "needs Reset again.")
        return ok, said

    RESET_BEFORE_ABORT_LANDED = (
        "Reset was refused: the Abort pressed before it has not reached the "
        "show conductor yet (it was sent on its own, past a request that has "
        "not answered), so there is nothing to Reset yet. Press Reset again "
        "once the Abort has gone through.")

    def _clear_latch_files(self, d):
        """After a Reset whose cleared latch is saved in tonight's file: the
        latch file goes, and a set-aside night file for tonight is renamed so
        it no longer latches a restart. Kept, not deleted, for the morning
        read. Said in the journal if either cannot be done."""
        path = latch_path(self.state_dir)
        try:
            remove_latch_marker(path)
            self._marker_clear_pending = False
        except OSError as e:
            self._journal_line(
                "system", f"Reset is done, but the Abort latch file {path} "
                f"could not be removed ({e.strerror or e}). If ltcplay "
                f"restarts while it is there, the rig starts dark and needs "
                f"Reset again. ltcplay keeps trying.",
                action="reset", outcome="latch file kept", fault=True)
        aside = set_aside_path(tonight_path(d, self.state_dir))
        if os.path.exists(aside):
            stamp = self.clock().astimezone(self._tz()).strftime("%H%M%S")
            done = aside[:-5] + f".reset-{stamp}.json"
            try:
                os.replace(aside, done)
            except OSError as e:
                self._journal_line(
                    "system", f"Reset is done, but {aside} could not be "
                    f"renamed ({e.strerror or e}), so a restart tonight "
                    f"starts dark again and needs Reset again.",
                    action="reset", outcome="set aside kept", fault=True)

    def flush_conductor(self, timeout=5.0):
        """True once every conductor call queued so far has been made and
        journaled. For tests, and for stop()."""
        return self._calls.flush(timeout)

    def reset_conductor(self, who, screen, wait_s=2.0):
        """The operator's Reset, for the Abort latch: the same
        Conductor.reset() an operator's own Reset press calls, queued
        behind every conductor call already decided, so it can never land
        before the Abort it is meant to clear. It also clears the
        scheduler's own Abort latch, saved in tonight's file (see
        _after_reset). Journaled with who and which screen.

        Returns {"ok", "text"}: ok False, with the conductor's sentence,
        when there is nothing to Reset or the Abort is still fading, and
        when no answer has come back within wait_s. Raises ValueError with
        a sentence, written to the journal as a refused press like every
        other press's refusal, when no conductor is attached or the
        operator or screen is blank or not on its list."""
        who = str(who or "").strip()
        screen = str(screen or "").strip()
        if self.conductor is None:
            self._refuse_reset(who, screen, "There is no show conductor "
                               "attached, so there is nothing to Reset.")
        if not who or not screen:
            self._refuse_reset(who, screen, "Reset has to say who pressed it "
                               "and which screen it came from. Nothing was "
                               "reset.")
        names = {n.lower(): n for n in self.operators}
        if who.lower() not in names:
            self._refuse_reset(who, screen, f"{who!r} is not on the operator "
                               f"list ({', '.join(self.operators)}). Nothing "
                               f"was reset.")
        screen = self._check_screen(screen, who, "reset", "Reset")
        with self._locked():
            call = self._new_call("Reset", "reset", names[who.lower()],
                                  screen)
            self._calls.put(call)
        if not call.done.wait(wait_s):
            return {"ok": False, "text": "Reset is queued behind the show "
                                         "conductor's earlier work and has "
                                         "not answered yet."}
        return {"ok": call.ok, "text": call.sentence}

    def _refuse_reset(self, who, screen, sentence):
        with self._locked():
            self._log(self.logbook.record, actor="operator", action="reset",
                      outcome="refused", reason=sentence,
                      text=f"{who or 'An unnamed operator'}'s Reset was "
                           f"refused. {sentence}",
                      state=self._state_name(), night=self._night(),
                      who=who or "unnamed operator",
                      screen=screen or "unnamed screen")
        raise ValueError(sentence)

    def _aborted(self):
        """An operator's Abort was sent to the show conductor (or is still
        on its way to it) and nobody has pressed Reset: the scheduler's own
        latch, saved in tonight's file so a restart keeps it, or the
        conductor's. Never with no conductor."""
        if self.conductor is None:
            return False
        if self.machine is not None and self.machine.abort_latched:
            return True
        try:
            return bool(getattr(self.conductor, "latched", False))
        except Exception:
            return False

    # Events the Abort latch changes: a show coming due (TICK, BOOT_DONE),
    # Start now, and a Hold or Resume (which must not bring a look back
    # while aborted).
    LATCH_EVENTS = (sch.TICK, sch.BOOT_DONE, sch.START_NOW, sch.HOLD_ON,
                    sch.RESUME)

    def _apply(self, ev, now=None):
        now = now or self.clock()
        before = self.machine
        if ev.kind in self.LATCH_EVENTS and self._aborted():
            # Jeff: after an Abort the rig stays dark until the operator
            # acts, and Reset is that act. The engine misses a show that
            # comes due, refuses Start now, and keeps a Hold or Resume dark
            # until then.
            ev = replace(ev, latched=True)
        out = sch.step(self.machine, ev, now)
        self.machine = out.machine
        plan = self._drive_conductor(out, ev)
        if any(p[1] == "abort" for p in plan):
            # The latch is saved with the Abort itself, before the conductor
            # is asked, so a restart in between still knows (and only a
            # Reset clears it, see _after_reset).
            self.machine = replace(self.machine, abort_latched=True)
            self._marker_clear_pending = False
        self._record(out, now, plan)
        if DRY_RUN and self.machine.state == sch.CLOSING:
            # Nothing to wait for: nothing was faded.
            out2 = sch.step(self.machine,
                            sch.Event(sch.CLOSING_DONE, "system"), now)
            self.machine = out2.machine
            self._record(out2, now)
        if self.machine is not before:
            self._save_tonight()
        # Only now, saved and journaled, is the conductor asked for
        # anything: a conductor that raises or hangs can no longer lose
        # the save or the "Show N started" line, which is what keeps a
        # restart inside the grace from starting the same show twice.
        self._queue_conductor(plan, ev)
        # A latch file created by this save is made to survive a power cut
        # only now, once the conductor has the Abort (fix round 3).
        self._sync_latch_marker()
        # Bumped every time the machine crosses INTO or OUT OF a held or
        # paused state, whoever does it: an operator's own Hold or Resume,
        # or an announcement's hold_for_announcement. hold_still_claimed
        # compares against this, not just against the state, so a Resume
        # followed by a fresh Hold (state looks the same again) still
        # shows as a DIFFERENT claim -- the operator's Resume always wins
        # over an announcement still loading (review round 2, 2026-09-26:
        # audit15_resume_race.py).
        if before is not None and self.machine is not None:
            was_held = before.state in (sch.HOLD, sch.PAUSED)
            is_held = self.machine.state in (sch.HOLD, sch.PAUSED)
            if was_held != is_held:
                self.hold_epoch += 1
        if self.machine.state == sch.OFF and before is not None and \
                before.state != sch.OFF and self.machine.slots:
            # The night has closed, by Close for the night or after its
            # last show:
            # the morning read goes beside the journal now.
            how = (f"closed by {ev.who} with Close for the night"
                   if ev.kind == sch.END_NIGHT else
                   "closed after the last show" if before.state != sch.BOOT
                   else "closed at start up, every show having passed")
            self._write_summary(how)
        # A push, not a poll: an announcement playing must be told the
        # instant a show starts, not found out about on its own next
        # status() look, which could be seconds behind. See on_show_started
        # in __init__ and web.py's serve(), which is the only place this
        # attribute is ever set.
        #
        # Queued, not called here: _apply() always runs with self.lock
        # held, quite possibly nested several levels deep (tonight_view()
        # and state_view() both call tick() while already holding it), and
        # a hook must never run while ANY caller on this thread still
        # holds the lock, at any depth -- self.lock gates tick() itself
        # and every operator action (Abort, Hold, Start now, every status
        # poll). _locked() is the only place this list is ever drained,
        # strictly after the lock is truly released (review round 2,
        # blocker 1b: audit13b_scheduler_stall.py, where a slow
        # stream.stop() reached in-line from here stalled Service.lock for
        # its full duration).
        if before is not None and before.state != sch.SHOW \
                and self.machine is not None \
                and self.machine.state == sch.SHOW \
                and self.on_show_started is not None:
            state_now, hook = self.machine.state, self.on_show_started
            # PAUSED -> SHOW (a Resume) fires this hook exactly like a
            # genuinely new show starting, on purpose: either way, an
            # announcement still playing must fade and stop, because the
            # show is moving. Only the WORDING differs, so the journal
            # says why (review round 2, 2026-09-26:
            # audit15_resume_fires_showstart.py).
            reason = "resume" if ev.kind == sch.RESUME else "new"
            self._pending_hooks.append(lambda: hook(state_now, reason))
        return out

    # -- the nightly summary ------------------------------------------------
    def _slot_rows(self, m):
        def hms(t):
            return t.astimezone(m.tz).strftime("%H:%M:%S") if t else ""
        rows = []
        for s in sorted(m.slots, key=lambda s: (s.start, s.n)):
            planned = sch.clock(s.start.astimezone(m.tz))
            if s.planned is not None:
                planned += f" (was {sch.clock(s.planned.astimezone(m.tz))})"
            rows.append({"show": s.n, "planned": planned,
                         "status": s.status, "reason": s.reason,
                         "started": hms(s.fired_at),
                         "ended": hms(s.ended_at)})
        return rows

    def _write_summary(self, how="", m=None):
        m = m or self.machine
        self._summarised.add(str(m.date))
        # The night, its state and its shows, taken now, under the lock:
        # the summary is written from these, whenever it is written.
        date, kw = m.date, dict(state=m.state, slots=self._slot_rows(m),
                                closed_by=how)
        if self.logbook.threaded():
            # Running for real: the summary reads and writes files, so it
            # happens beside the scheduler, never inside a tick: queued as
            # a hook, run once the lock is truly free (see _locked()).
            self._pending_hooks.append(
                lambda: self._log(self.logbook.write_summary, date, **kw))
            return None
        return self._log(self.logbook.write_summary, date, **kw)

    def write_summary(self):
        """Write tonight's summary now, as it stands."""
        with self._locked():
            if self.machine is None:
                raise ValueError("There is no night loaded, so there is no "
                                 "summary to write. " + self.error)
            return self._write_summary("written on request")

    # How many earlier nights a start looks back over for a missing summary.
    LOOK_BACK_NIGHTS = 14

    def _look_back(self, d, state):
        """Once per run (housekeeping decides when): every earlier night
        that ended without its summary (the program was not running when it
        closed, or the power went, perhaps for days) gets one from its own
        journal, so the morning read is always there. It never waits for a
        button."""
        folder = self.logbook.folder
        try:
            names = os.listdir(folder)
        except OSError:
            return
        nights = []
        for name in names:
            m = journal._NAME.match(name)
            if not m or m.group(4) != "jsonl":
                continue
            try:
                n = datetime(int(m.group(1)), int(m.group(2)),
                             int(m.group(3))).date()
            except ValueError:
                continue
            if n < d and not os.path.exists(
                    os.path.join(folder, journal.summary_name(n))):
                nights.append(n)
        for prev in sorted(nights)[-self.LOOK_BACK_NIGHTS:]:
            self._log(self.logbook.write_summary, prev, state=state,
                      closed_by="written later from the journal, "
                                "because the night never closed while "
                                "ltcplay was running")

    # -- tonight on disk --------------------------------------------------
    def _save_tonight(self, tries=5):
        m = self.machine
        latched = self.conductor is not None and m.abort_latched
        # The latch file first: it is the one a full disk or a held file is
        # least likely to stop, and the one every restart reads.
        marker_error = self._write_latch_marker(m) if latched else None
        path = tonight_path(m.date, self.state_dir)
        try:
            write_json_atomic(path, sch.machine_to_doc(m), tries=tries)
        except OSError as e:
            msg = (f"Tonight's list could not be saved to {path}: "
                   f"{e.strerror or e}. The schedule carries on, but a "
                   f"restart now would go back to the schedule file and "
                   f"lose tonight's changes.")
            if msg != self.persist_error:
                self._journal_line("system", msg, action="save tonight",
                                   outcome="failed", fault=True)
            self.persist_error = msg
            if latched:
                self._say_latch_saved(marker_error, False)
            return False
        if self.persist_error:
            self._journal_line("system", f"Tonight's list is being saved "
                               f"to {path} again.", action="save tonight")
        self.persist_error = ""
        if latched:
            self._say_latch_saved(marker_error, True)
        return True

    def _write_latch_marker(self, m):
        """Create the latch file, trying twice. None once it is there, or
        the sentence for why it is not."""
        path = latch_path(self.state_dir)
        words = {"what": "An Abort was pressed and nobody has pressed Reset. "
                         "While this file is here, ltcplay starts dark and "
                         "starts no show. Reset removes it.",
                 "night": m.date.isoformat(),
                 "written": self.clock().isoformat()}
        err = None
        for _ in range(2):
            try:
                if create_latch_marker(path, words):
                    self._latch_unsynced = True
                return None
            except OSError as e:
                err = e
        return f"{path}: {err.strerror or err}"

    def _sync_latch_marker(self):
        """The folder flush a just created latch file still needs: after
        the conductor has been asked, never before (see
        create_latch_marker)."""
        if self._latch_unsynced:
            self._latch_unsynced = False
            sync_latch_marker(latch_path(self.state_dir))

    LATCH_LOST = ("The Abort latch could not be saved; if ltcplay restarts "
                  "tonight the next show would start: press nothing, fix the "
                  "disk. Neither the latch file nor tonight's list could be "
                  "written ({why}). The Abort itself went to the show "
                  "conductor and the rig is dark in this run; ltcplay keeps "
                  "trying to save the latch every few seconds.")
    LATCH_HALF = ("The Abort latch file could not be written ({why}). "
                  "Tonight's list holds the latch, so a restart tonight still "
                  "starts dark, but a damaged list would lose it. Fix the "
                  "disk. ltcplay keeps trying every few seconds.")
    LATCH_SAVED = ("The Abort latch is saved now ({path}). A restart starts "
                   "dark and starts no show until Reset.")

    def _say_latch_saved(self, marker_error, list_saved):
        """One loud line when the Abort latch is not safely on disk, and one
        when it is again."""
        if marker_error is None:
            text = None
        elif list_saved:
            text = self.LATCH_HALF.format(why=marker_error)
        else:
            text = self.LATCH_LOST.format(why=marker_error)
        if text == self._latch_trouble:
            return
        if text is not None:
            self._journal_line("system", text, action="save abort latch",
                               outcome="failed", fault=True)
        else:
            self._journal_line(
                "system", self.LATCH_SAVED.format(
                    path=latch_path(self.state_dir)),
                action="save abort latch", outcome="saved")
        self._latch_trouble = text

    # How often a latch that could not be saved is tried again.
    LATCH_RETRY_S = 2.0

    def _keep_latch_on_disk(self):
        """Once per tick, with the lock held: while the Abort latch is not
        safely on disk (the latch file or tonight's list could not be
        written), or a Reset's removal of the latch file did not go through,
        try again, every LATCH_RETRY_S, once and without waiting, so a full
        disk or a held file never stalls the tick."""
        m = self.machine
        if self.conductor is None or m is None:
            return
        if m.abort_latched:
            if self._latch_trouble is None and not self.persist_error:
                return
        elif not self._marker_clear_pending:
            return
        t = _time.monotonic()
        if self._latch_retry_at is not None and t < self._latch_retry_at:
            return
        self._latch_retry_at = t + self.LATCH_RETRY_S
        if self._save_tonight(tries=1) and not m.abort_latched:
            self._clear_latch_files(m.date)

    def _load_tonight(self, d, now, set_aside=True):
        """Tonight's saved machine, or a fresh one from the rule with a
        sentence saying why. The show length, guard, grace and zone always
        come from the rule file; the saved list only says what happened
        tonight, and it is checked before it is believed. A file that fails
        the checks is set aside, never overwritten, so the morning read can
        still see it. If the rule changed since the list was saved, tonight
        is rebuilt from the new rule and only what already happened is kept.
        With set_aside False (an earlier night being picked up again, see
        _open_night_before) a file that fails the checks raises instead and
        is left exactly where it is."""
        path = tonight_path(d, self.state_dir)
        if not os.path.exists(path):
            self._journal_line(
                "system", f"There is no saved list for {d}, so tonight "
                f"starts from the schedule file.", action="load tonight")
            return sch.new_night(self.rule, d)
        notes = []
        try:
            with open(path, encoding="utf-8-sig") as fh:
                m = sch.machine_from_doc(json.load(fh), self.rule, d, now,
                                         notes)
        except (OSError, ValueError, sch.RuleError) as e:
            if not set_aside:
                raise
            return self._set_aside(path, e, d, now)
        for text in notes:
            self._journal_line("system", text, action="load tonight",
                               outcome="clock stepped back")
        rid = sch.rule_fingerprint(self.rule)
        if m.rule_id != rid:
            m, notes = sch.rebuild_night(self.rule, m)
            self._journal_line(
                "system", "The schedule file changed after tonight's list "
                "was saved, so tonight is rebuilt from the new schedule. "
                "Shows that already happened keep their status; tonight's "
                "changes to shows still to come are dropped.",
                action="load tonight", outcome="rebuilt")
            for text in notes:
                self._journal_line("system", text, action="load tonight",
                                   outcome="rebuilt")
        return m

    def _set_aside(self, path, e, d, now):
        aside = set_aside_path(path)
        # Whatever was in it, an Abort may have been: see _boot_latch.
        self._unreadable_night = d
        try:
            os.replace(path, aside)
            where = f"It was set aside as {aside}."
        except OSError:
            where = "It could not be moved aside."
        self._journal_line(
            "system", f"The saved list for tonight, {path}, could not be "
            f"used: {str(e).rstrip('.')}. {where} Tonight starts again from "
            f"the schedule file, so edits made earlier tonight are lost. "
            f"Check tonight's list.",
            action="load tonight", outcome="failed", fault=True)
        # Zero on anything uncertain: a show may have fired moments ago.
        m, notes = sch.assume_the_worst(sch.new_night(self.rule, d), now)
        for text in notes:
            self._journal_line("system", text, action="load tonight",
                               outcome="assumed the worst")
        return m

    # -- the night ------------------------------------------------------
    def _tonight(self, now):
        return now.astimezone(self.rule.tz).date()

    # How many days back a start looks for a night left open past midnight
    # (a delayed show waiting, or a show running), see _open_night_before.
    OPEN_NIGHT_LOOK_BACK = 7

    def _ensure_night(self, now):
        """The night the scheduler is on, loaded or moved on as the date
        changes. A night stays open past midnight while a show runs or is
        paused, and while a delayed show waits for Start now or Close for
        the night (Jeff, 2026-10-01), but only until the NEXT night's
        preshow lead begins (_still_open, DEFAULT pending Jeff, 2026-10-02).

        On start it first looks for such a night left open by the last run
        (_open_night_before): before 2026-10-02 a start after midnight only
        ever loaded the calendar day's file, so a delayed show waiting from
        last night vanished without a word, and a delayed show running
        after midnight was lost with no fault."""
        if self.rule is None:
            return False
        d = self._tonight(now)
        if self.machine is None:
            old = self._open_night_before(d, now)
            if old is None:
                fresh = not os.path.exists(tonight_path(d, self.state_dir))
                m = self._load_tonight(d, now)
                if fresh and self._latched_before(d):
                    m = replace(m, abort_latched=True)
                m = self._boot_latch(m)
                self.machine = replace(m, operators=self.operators)
                self._apply(sch.Event(sch.BOOT_DONE, "system"), now)
                return True
            # The open night boots exactly as tonight's would after a
            # restart: a show that was running is cut (FAULT, the rig goes
            # dark, nothing resumes), a delayed show keeps waiting. Then it
            # goes through the same "is it still open" rule as at midnight.
            old = self._boot_latch(old)
            self.machine = replace(old, operators=self.operators)
            self._apply(sch.Event(sch.BOOT_DONE, "system"), now)
        if self.machine.date == d:
            return True
        # A new day. Settle yesterday first: a show still on its list
        # (the machine was asleep across it) is marked MISSED in the
        # journal rather than dropped without a word.
        self._apply(sch.Event(sch.TICK, "scheduler"), now)
        how = self._still_open(now, d)
        if how is None:
            return True
        if str(self.machine.date) not in self._summarised and \
                self.machine.slots:
            self._write_summary(how, self.machine)
        # Yesterday's summary was just seen to; no looking back needed.
        self._looked_back = True
        # This assignment, not _apply(), is what actually drops a night
        # left on Hold (SHOW and PAUSED are never dropped; see
        # _still_open). It bypasses _apply's own before/after bump, because
        # by the time BOOT_DONE below runs _apply again, `before` is
        # already the new night's fresh machine, not this HOLD one -- so
        # the crossing has to be bumped here, by hand, or an announcement's
        # claim from this night could still look current after it was
        # dropped (merge with #14, 2026-09-26). Reached by a held night
        # with nothing waiting at midnight, and by a held night whose
        # delayed show is given up when the next night's preshow begins.
        if self.machine.state == sch.HOLD:
            self.hold_epoch += 1
        # An Abort nobody has Reset outlives its night, exactly as the
        # conductor's own latch does in a run that never restarts.
        latched = self.machine.abort_latched
        m = self._load_tonight(d, now)
        if latched and not m.abort_latched:
            m = replace(m, abort_latched=True)
            self._journal_line(
                "system", self.LATCH_CARRIED.format(night=self.machine.date),
                action="load tonight", outcome="still aborted")
        m = self._boot_latch(m)
        self.machine = replace(m, operators=self.operators)
        self._apply(sch.Event(sch.BOOT_DONE, "system"), now)
        return True

    LATCH_CARRIED = ("The night of {night} ended with an Abort that nobody "
                     "has Reset, so tonight starts dark: no show starts and "
                     "Start now is refused until an operator presses Reset.")
    LATCH_FILE_FOUND = ("The Abort latch file {path} is there: an Abort was "
                        "pressed and nobody has pressed Reset. ltcplay "
                        "starts dark: no show starts and Start now is "
                        "refused until an operator presses Reset.")
    LATCH_UNREADABLE = ("Tonight's saved list ({night}) could not be read "
                        "this time or an earlier time tonight (it is set "
                        "aside as {aside}), so ltcplay cannot tell whether "
                        "an Abort was pressed tonight. To be safe it starts "
                        "dark, as if one was: no show starts and Start now "
                        "is refused until an operator presses Reset.")
    LATCH_EARLIER_UNREADABLE = (
        "The saved list for {night}, the last night before this one, could "
        "not be read, so ltcplay cannot tell whether that night ended with "
        "an Abort nobody Reset. To be safe it starts dark, as if it did: no "
        "show starts and Start now is refused until an operator presses "
        "Reset.")

    def _boot_latch(self, m):
        """`m`, latched, when the disk says an Abort may not have been Reset
        and its own list does not already say so (fix round 3 of #30), each
        reason said in the journal. Only with a show conductor attached:
          - the latch file is there;
          - this night's saved list could not be read, now or earlier
            tonight (it was set aside): it may have held an Abort, and an
            older ltcplay that set a format 4 list aside and wrote its own
            leaves exactly this behind."""
        if self.conductor is None:
            return m
        whys = []
        marker = latch_path(self.state_dir)
        if os.path.exists(marker) and not m.abort_latched:
            whys.append((self.LATCH_FILE_FOUND.format(path=marker), False))
        aside = set_aside_path(tonight_path(m.date, self.state_dir))
        if self._unreadable_night == m.date or os.path.exists(aside):
            whys.append((self.LATCH_UNREADABLE.format(night=m.date,
                                                      aside=aside), True))
        for why, fault in whys:
            self._journal_line("system", why, action="load tonight",
                               outcome="still aborted", fault=fault)
        return replace(m, abort_latched=True) if whys else m

    def _latched_before(self, d):
        """True when the most recent night saved before `d` ended with an
        Abort nobody Reset (its file says abort_latched), whatever its age:
        a run that never restarted would still be latched too. Said in the
        journal. With a show conductor attached, a most recent file that
        cannot be read counts as latched too, and says so (fix round 3).
        The latch file (_boot_latch) is what normally carries the latch;
        this is for a list saved before it existed."""
        try:
            names = os.listdir(self.state_dir)
        except OSError:
            return False
        dates = []
        for name in names:
            if not (name.startswith(TONIGHT_PREFIX) and
                    name.endswith(".json")):
                continue
            try:
                y = datetime.strptime(name[len(TONIGHT_PREFIX):-5],
                                      "%Y-%m-%d").date()
            except ValueError:
                continue
            if y < d:
                dates.append(y)
        if not dates:
            return False
        y = max(dates)
        try:
            with open(tonight_path(y, self.state_dir),
                      encoding="utf-8-sig") as fh:
                latched = json.load(fh).get("abort_latched") is True
        except (OSError, ValueError, AttributeError):
            if self.conductor is None:
                return False
            # Fix round 3: it may have held an Abort nobody Reset.
            self._journal_line(
                "system", self.LATCH_EARLIER_UNREADABLE.format(night=y),
                action="load tonight", outcome="still aborted", fault=True)
            return True
        if latched:
            self._journal_line("system", self.LATCH_CARRIED.format(night=y),
                               action="load tonight",
                               outcome="still aborted")
        return latched

    def _next_lead(self, after, upto):
        """(lead, date): when the first night after `after`, up to and
        including `upto`, that has shows begins its preshow lead; (None,
        None) when none of them has shows."""
        x = after + timedelta(days=1)
        while x <= upto:
            lead = sch.next_night_lead(self.rule, x)
            if lead is not None:
                return lead, x
            x += timedelta(days=1)
        return None, None

    def _still_open(self, now, d):
        """None while the night on the machine (an earlier date than `d`)
        must stay open; otherwise the words for its summary, once it may
        be set aside.

        A running or paused show is never set aside. A delayed show keeps
        the night open (Jeff, 2026-10-01) until the next night's preshow
        lead begins: then the delayed show is MISSED, with a fault line
        naming it and why, and the night is set aside so the next night's
        first show can fire (DEFAULT pending Jeff's confirmation,
        2026-10-02; without it the next night never started at all)."""
        m = self.machine
        if m.state in (sch.SHOW, sch.PAUSED):
            return None
        if m.delayed() is None:
            return "written at midnight; the night was never closed"
        lead, when = self._next_lead(m.date, d)
        if lead is None or now < lead:
            return None
        n = m.delayed().n
        out = sch.close_for_next_night(m, now, when, lead)
        self.machine = out.machine
        self._record(out, now)
        self._save_tonight()
        return (f"closed when the night of {when} began its preshow; the "
                f"delayed show {n} never started")

    def _open_night_before(self, d, now):
        """The most recent night saved before `d`, picked up again if it
        was left open (a delayed show waiting, or a show running or paused),
        or None. Only while it would still be open by _still_open's rule:
        once a later night's preshow lead has begun, it is said out loud
        and left. Journaled either way it matters. An unreadable file is
        left where it is and said out loud; tonight's list then starts as
        it always did."""
        for back in range(1, self.OPEN_NIGHT_LOOK_BACK + 1):
            y = d - timedelta(days=back)
            path = tonight_path(y, self.state_dir)
            if not os.path.exists(path):
                continue
            try:
                with open(path, encoding="utf-8-sig") as fh:
                    doc = json.load(fh)
                state = doc["state"]
                slots = [(x["n"], x["status"]) for x in doc["slots"]]
            except (OSError, ValueError, TypeError, KeyError) as e:
                self._journal_line(
                    "system", f"The saved list for {y}, {path}, could not "
                    f"be read to see whether that night was left open "
                    f"({type(e).__name__}: {e}). It is left as it is, and "
                    f"{d} starts from its own list.",
                    action="load tonight", outcome="failed", fault=True)
                return None
            # A show that was running counts only from the day before: it
            # is cut either way, and older than that the look-back summary
            # (_look_back) is how that night is closed, as it always was.
            waiting = [f"show {n} is delayed and waits for Start now"
                       for n, st in slots if st == sch.DELAYED] + \
                      [f"show {n} was running" for n, st in slots
                       if st == sch.RUNNING and back == 1]
            if state in (sch.CLOSING, sch.OFF) or not waiting:
                return None
            lead, when = self._next_lead(y, d)
            if lead is not None and now >= lead:
                # Too late to pick it up: by the same rule as at midnight it
                # would already have given way to the night of `when`.
                self._journal_line(
                    "system", f"The night of {y} was left open when ltcplay "
                    f"stopped ({'; '.join(waiting)}), but the night of "
                    f"{when} has already begun its preshow, so it is not "
                    f"picked up again. Nothing from it will run.",
                    action="load tonight", outcome="not restored",
                    fault=True)
                return None
            try:
                m = self._load_tonight(y, now, set_aside=False)
            except (OSError, ValueError, sch.RuleError) as e:
                self._journal_line(
                    "system", f"The night of {y} was left open ("
                    f"{'; '.join(waiting)}), but its saved list {path} "
                    f"could not be used: {str(e).rstrip('.')}. It is left "
                    f"as it is, and {d} starts from its own list.",
                    action="load tonight", outcome="failed", fault=True)
                return None
            self._journal_line(
                "system", f"ltcplay started on {d} while the night of {y} "
                f"was still open: {'; '.join(waiting)}. That night is picked "
                f"up again first, instead of being dropped.",
                action="load tonight", outcome="restored open night")
            return m
        return None

    def tick(self):
        with self._locked():
            now = self.clock()
            self._watch_clock(now)
            self._watch_conductor()
            if not self._ensure_night(now):
                return None
            self._keep_latch_on_disk()
            m = self.machine
            # Dry run: nothing was started, so the show "ends" when it would
            # have, which a pause moves later. A paused show never ends.
            if DRY_RUN and m.state == sch.SHOW and \
                    now >= m.expected_end(now):
                self._apply(sch.Event(sch.SHOW_ENDED, "system",
                                      detail="dry run, nothing was started",
                                      show=m.running), now)
            self._apply(sch.Event(sch.TICK, "scheduler"), now)
            self._sync_latch_marker()
            m = self.machine
        self._after_tick()
        return m

    # A conductor request with no answer after this long is a fault: they
    # are meant to return at once (the conductor does its fades on its own
    # thread), so this is a hang, not a slow fade.
    CONDUCTOR_STUCK_S = 3.0

    def _watch_conductor(self):
        """Once per tick, with the lock held: a show conductor request that
        has gone CONDUCTOR_STUCK_S without an answer, or a line of requests
        whose thread has died, is a fault, written once, on the page
        (the fault flag and state_view's "conductor") and in the journal,
        and a line says when it is over. A dead line is started again at
        once. Before 2026-10-02 both were silent: every request behind a
        hung one (an Abort included) just waited, and a Reset answered
        "queued" forever."""
        if self.conductor is None:
            return
        h = self._calls.health()
        problem = None
        if not h["alive"]:
            self._calls.revive()
            problem = ("dead", (
                f"The line of requests to the show conductor stopped: its "
                f"thread ended, with {h['waiting']} request(s) waiting. "
                f"ltcplay started it again; anything that was waiting goes "
                f"out now, in order. That is a bug in ltcplay."))
        elif h["stuck"] is not None and \
                h["age_s"] >= self.CONDUCTOR_STUCK_S:
            c = h["stuck"]
            problem = (("stuck", id(c)), (
                f"The show conductor has not answered {c.label} for "
                f"{h['age_s']:.0f} s. Every request behind it is waiting "
                f"({h['waiting']} so far); an Abort does not wait, it is "
                f"sent on its own after {_ConductorCalls.URGENT_WAIT_S:g} "
                f"s. Check the lasers, video and flame link, and the "
                f"conductor's own lines."))
        was = self._conductor_trouble
        if problem is not None:
            if was is not None and was["key"] == problem[0]:
                return
            self._conductor_trouble = {"key": problem[0],
                                       "text": problem[1]}
            if self.machine is not None:
                self._apply(sch.Event(sch.FAULT_RAISED, "system",
                                      detail=problem[1]))
            else:
                self._journal_line("system", problem[1], action="conductor",
                                   outcome="failed", fault=True)
            return
        if was is not None:
            self._conductor_trouble = None
            self._journal_line(
                "system", "The show conductor is answering again; the "
                "requests that were waiting have gone out in order.",
                action="conductor", outcome="recovered")

    def conductor_view(self):
        """For the page: whether a conductor is attached, whether an Abort
        has not been Reset, and what is wrong with the line of requests to
        it, if anything."""
        with self._locked():
            t = self._conductor_trouble
            return {"attached": self.conductor is not None,
                    "aborted": self._aborted(),
                    "trouble": t["text"] if t else None}

    def check_clock(self):
        level, text, offset = check_clock(self.ntp_query,
                                          limit_s=self.clock_limit_s)
        with self._locked():
            self.clock_check = {"level": level, "text": text,
                                "offset_s": offset}
            # Consulting the time server again is what restores trust,
            # whatever it comes back saying: _prune_allowed() below then
            # decides on the fresh answer, the same as it always has.
            self._clock_trust_lost = False
            self._journal_line("system", text, action="clock check",
                               outcome=level)
        self._after_tick()
        return self.clock_check

    def _watch_clock(self, now):
        """Notice a clock that steps while this process is running.

        NTP is only ever queried once, at start (check_clock(), called from
        start()). Nothing else would ever notice the wall clock jumping
        later -- an NTP step, an RTC glitch, someone setting it by hand --
        so this compares how far the wall clock moved since the last tick
        against how far perf_counter moved, which the OS clock cannot
        step. The two have to agree to within CLOCK_JUMP_LIMIT_S; past
        that it is an impossible jump, not drift, and pruning stops
        trusting this machine's clock until it has been checked again
        (round 1 review of PR 25, blocker: a clock that was fine at boot
        and then jumped kept pruning as if the boot-time check still
        applied, and Logbook.prune()'s own newest-keep_days floor was, at
        the time, conditional on the caller saying the clock was
        untrusted -- see prune()'s docstring for that half of the fix)."""
        perf = self._perf_counter()
        if self._last_wall is not None:
            wall_elapsed = (now - self._last_wall).total_seconds()
            perf_elapsed = perf - self._last_perf
            if abs(wall_elapsed - perf_elapsed) > CLOCK_JUMP_LIMIT_S:
                self._clock_trust_lost = True
                self._journal_line(
                    "system",
                    f"This machine's clock moved "
                    f"{journal.fmt_span(wall_elapsed)} between two ticks "
                    f"that were only {journal.fmt_span(perf_elapsed)} "
                    f"apart by perf_counter, which the operating system's "
                    f"clock cannot step. Something set the clock, or "
                    f"stepped it: it is not trusted for pruning until it "
                    f"has been checked again.", action="clock check",
                    outcome="jumped", fault=True)
                self._recheck_clock()
        self._last_wall = now
        self._last_perf = perf

    def _recheck_clock(self):
        """Ask the time server again, right away: on its own thread when
        the service is running for real (an NTP lookup must never hold up
        a tick), or in line when it is not (start(thread=False), and every
        selftest), so a test sees the fresh answer before its next call."""
        if self._thread is not None:
            threading.Thread(target=self._check_clock_safely, daemon=True,
                             name="ltcplay-clock-recheck").start()
        else:
            self._check_clock_safely()

    # -- housekeeping: disk work that is not a scheduling decision ----------
    # Pruning waits for a clock this machine can trust: the time server said
    # it is right, or it has been running this long. A clock a year ahead
    # would otherwise call every night old on the first tick.
    PRUNE_AFTER_S = 600

    def _clock_trusted(self):
        """Live, not cached: computed fresh from the current state every
        time it is asked, never from a snapshot taken once at start or
        once at the last check (round 2 review of PR 25). True only when
        the time server agreed with this clock AND nothing has moved it
        out from under that agreement since (_watch_clock's own jump
        detection resets this the instant it notices one, and restores it
        the instant the clock is re-checked -- see check_clock())."""
        return not self._clock_trust_lost and \
            (self.clock_check or {}).get("level") == "ok"

    def _prune_allowed(self):
        if self._clock_trusted():
            return True
        if self._clock_trust_lost:
            return False
        return (self.clock() - self._born).total_seconds() >= \
            self.PRUNE_AFTER_S

    def _housekeeping_due(self):
        m = self.machine
        if m is None or self._hk_busy:
            return False
        return not self._looked_back or (
            self._pruned_for != m.date and self._prune_allowed())

    def _after_tick(self):
        """Pruning and a missing summary, outside the scheduler's lock, and
        on a thread of their own when running for real, so reading and
        deleting files never holds up a tick."""
        with self._locked():
            # Tested and set together: the tick and the clock check both
            # come here, and only one of them may start the housekeeping.
            if not self._housekeeping_due():
                return
            self._hk_busy = True
            if self.logbook.threaded():
                # A hook, run on a thread of its own once the lock is truly
                # free (see _locked()); it takes the night and the state
                # under the lock itself, before any file is touched.
                self._pending_hooks.append(self._housekeeping)
                return
        self._housekeeping()

    def _housekeeping(self):
        try:
            with self._locked():
                # Decided and recorded under the lock, so no second caller
                # can decide the same work before this one has done it.
                m = self.machine
                if m is None:
                    return
                d, state = m.date, m.state
                look = not self._looked_back
                self._looked_back = True
                prune = self._pruned_for != d and self._prune_allowed()
                if prune:
                    self._pruned_for = d
                # Computed fresh, right here, never from a snapshot taken
                # earlier (round 2 review of PR 25): a trusted clock (the
                # time server agreed, and _watch_clock has noticed no jump
                # since) prunes by age alone; anything else keeps the
                # newest nights and incident folders that exist and
                # removes nothing by age, so a wrong clock can never call
                # good history old.
                floor = not self._clock_trusted()
            if look:
                self._look_back(d, state)
            if prune:
                self._log(self.logbook.prune, d, state=state, floor=floor)
        finally:
            self._hk_busy = False

    # -- running --------------------------------------------------------
    def start(self, thread=True):
        """Tick first, then check the clock on a thread of its own. A slow
        time server must never hold up the first look at the schedule: a
        3 s lookup at 17:59:59 would otherwise miss the 18:00 show. Safe to
        call twice."""
        if self._started:
            return self
        self._started = True
        if thread:
            # Before the first tick: from here the journal writes on its own
            # thread, so a slow or full disk never holds up a tick, the
            # first one included.
            self.logbook.start_writer()
        self._safe_tick()
        if not thread:
            self.check_clock()
            return self
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="ltcplay-scheduler")
        self._thread.start()
        self._sample_thread = threading.Thread(
            target=self._sample_loop, daemon=True, name="ltcplay-state-ring")
        self._sample_thread.start()
        self._clock_thread = threading.Thread(
            target=self._check_clock_safely, daemon=True,
            name="ltcplay-clock-check")
        self._clock_thread.start()
        return self

    def _check_clock_safely(self):
        try:
            self.check_clock()
        except Exception as e:
            with self._locked():
                self._journal_line(
                    "system", f"The clock check could not run "
                    f"({type(e).__name__}: {e}). Shows still start by this "
                    f"machine's clock, unchecked.", action="clock check",
                    outcome="failed", fault=True)

    # A tick that keeps failing is written once per kind of failure, then
    # the repeats are counted and written at most once a minute, never two
    # such lines closer than FAULT_GAP_S. A failure of a kind not seen in
    # this run of failures is always written at once. The kind is the
    # exception's type and where it was raised, not its message, so a
    # message that carries a number, or two failures taking turns, cannot
    # defeat the count.
    FAULT_REPEAT_S = 60
    FAULT_GAP_S = 10
    FAULT_KINDS_MAX = 20

    @staticmethod
    def _fault_key(e):
        tb = traceback.extract_tb(e.__traceback__)
        where = (f"{os.path.basename(tb[-1].filename)} line {tb[-1].lineno}"
                 if tb else "an unknown place")
        return f"{type(e).__name__} at {where}"

    def _safe_tick(self):
        try:
            self.tick()
        except Exception as e:                     # never kill the ticker
            self._tick_failed(self._fault_key(e), f"{type(e).__name__}: {e}")
        else:
            if self._tick_fault is not None:
                with self._locked():
                    tf, self._tick_fault = self._tick_fault, None
                    self._journal_repeats(tf)
                    self._journal_line(
                        "system", f"The scheduler's problem "
                        f"({', '.join(tf['kinds'])}) has stopped; ticks are "
                        f"working again.", action="tick", outcome="recovered")

    def _tick_failed(self, key, text):
        with self._locked():
            now = self.clock()
            tf = self._tick_fault
            if tf is None:
                tf = self._tick_fault = {"kinds": {}, "since": now,
                                         "last_line": None}
            kinds = tf["kinds"]
            if key not in kinds and len(kinds) < self.FAULT_KINDS_MAX:
                kinds[key] = 0
                tf["last_line"] = now
                self._journal_line(
                    "system", f"The scheduler hit a problem and carried on "
                    f"({text}, {key}). It tries again on the next tick, a "
                    f"quarter of a second later.", action="tick",
                    outcome="failed", fault=True, fault_key=key)
                return
            kinds[key] = kinds.get(key, 0) + 1
            quiet = tf["last_line"] is None or \
                (now - tf["last_line"]).total_seconds() >= self.FAULT_GAP_S
            if quiet and (now - tf["since"]).total_seconds() >= \
                    self.FAULT_REPEAT_S:
                self._journal_repeats(tf, now)

    def _journal_repeats(self, tf, now=None):
        counted = {k: n for k, n in tf["kinds"].items() if n}
        if not counted:
            return
        since = f"{self.logbook.local(tf['since']):%H:%M:%S}"
        if len(counted) == 1:
            (k, n), = counted.items()
            text = (f"The same scheduler problem happened {n} more time(s) "
                    f"since {since} ({k}). It is tried again every tick.")
        else:
            parts = "; ".join(f"{k}, {n} time(s)" for k, n in counted.items())
            k = "tick problems: " + ", ".join(sorted(counted))
            text = (f"The scheduler's problems happened again since {since}: "
                    f"{parts}. They are tried again every tick.")
        self._journal_line("system", text, action="tick",
                           outcome="failed again", fault=True,
                           repeats=sum(counted.values()), fault_key=k)
        for key in tf["kinds"]:
            tf["kinds"][key] = 0
        now = now or self.clock()
        tf["since"] = tf["last_line"] = now

    def _run(self):
        while not self._stop.is_set():
            self._safe_tick()
            self._stop.wait(self.TICK_S)

    def halt(self):
        """Stop ticking at once, so nothing new can start: the first thing
        on the way out, before the rig is stopped. The slow part, closing
        the journal, is stop()'s."""
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=0.2)

    def stop(self):
        """Stop ticking and close the journal. Returns within a couple of
        seconds whatever the disk is doing: a disk that has stopped
        answering must never hold up the rest of the program's stop."""
        self._stop.set()
        for t in (self._thread, self._sample_thread):
            if t is not None:
                t.join(timeout=1)
        # The conductor's last requests (a closing, an Abort) get their
        # journal line before the journal closes, within a second.
        self._calls.flush(1.0)
        # From here no line is written in this thread's time; close()
        # writes what is left within its own time limit.
        self.logbook.begin_close()
        if self.lock.acquire(timeout=1):
            try:
                self._log(self.logbook.stopping, state=self._state_name(),
                          night=self._night())
            finally:
                self.lock.release()
        self.logbook.close()

    # -- the ring buffer ----------------------------------------------------
    def _snapshot(self):
        """What the scheduler looks like right now, for the ring buffer. Kept
        small: it is taken 5 times a second and never written to disk."""
        m, now = self.machine, self.clock()
        snap = {"state": self._state_name(),
                "logging_ok": not self.logbook.stopped_why}
        if m is not None:
            nxt = m.next_slot()
            snap.update(
                running=m.running or None, paused=m.state == sch.PAUSED,
                fault=m.fault, faults=len(m.faults),
                delayed=(m.delayed().n if m.delayed() else None),
                next_show=nxt.n if nxt else None,
                next_in_s=(int((nxt.start - now).total_seconds())
                           if nxt else None))
        return snap

    def sample(self):
        with self._locked():
            snap = self._snapshot()
        self.logbook.sample(snap)

    def _sample_loop(self):
        nxt = _time.monotonic()
        while True:
            nxt += self.SAMPLE_S
            if self._stop.wait(max(0.0, nxt - _time.monotonic())):
                return
            try:
                self.sample()
            except Exception:       # a sample is never worth a thread
                pass

    # -- views ----------------------------------------------------------
    def rule_view(self):
        with self._locked():
            prev = previous_path(self.path)
            return {"ok": self.rule is not None, "error": self.error or None,
                    "path": self.path,
                    "previous": prev if os.path.exists(prev) else None,
                    "rule": sch.rule_to_doc(self.rule) if self.rule else None}

    def tonight_view(self):
        with self._locked():
            self.tick()
            now = self.clock()
            if self.machine is None:
                return {"ok": False, "error": self.error, "slots": []}
            plan = sch.expand(self.rule, self.machine.date)
            return {"ok": True, "date": self.machine.date.isoformat(),
                    "why": plan.why, "state": self.machine.state,
                    "slots": sch.slot_view(self.machine, now),
                    "runs_late": bool(sch.late_warnings(self.machine)),
                    "warnings": sch.late_warnings(self.machine),
                    "note": "Edits here are for tonight only and are never "
                            "written to the schedule file."}

    def state_view(self, journal=40):
        with self._locked():
            self.tick()
            now = self.clock()
            out = {"ok": self.machine is not None,
                   "error": self.error or None,
                   "dry_run": DRY_RUN, "note": DRY_RUN_NOTE,
                   "now": now.astimezone(
                       self.rule.tz if self.rule else timezone.utc)
                   .isoformat(timespec="seconds"),
                   "clock_check": self.clock_check,
                   "saved_to": (tonight_path(self.machine.date,
                                             self.state_dir)
                                if self.machine else None),
                   "save_error": self.persist_error or None,
                   "logging": self.logbook.health(),
                   "conductor": self.conductor_view(),
                   "journal": list(self.journal)[-int(journal):][::-1]}
            if self.machine is not None:
                out.update(sch.machine_view(self.machine, now))
            return out

    def edit_tonight(self, body):
        """Move, add or remove one of tonight's shows. Tonight only."""
        op = (body or {}).get("op")
        kinds = {"move": sch.EDIT_MOVE, "add": sch.EDIT_ADD,
                 "remove": sch.EDIT_REMOVE}
        if op not in kinds:
            raise ValueError(f"op has to be one of move, add or remove, not "
                             f"{op!r}.")
        try:
            show = int(body.get("show") or 0)
        except (TypeError, ValueError):
            raise ValueError(f"show has to be a show number, not "
                             f"{body.get('show')!r}.")
        what = {"move": "move of a show", "add": "added show",
                "remove": "removal of a show"}[op]
        screen = self._check_screen(str(body.get("screen") or ""),
                                    str(body.get("who") or ""), kinds[op],
                                    what)
        ev = sch.Event(kinds[op], "operator", show=show,
                       at=str(body.get("at") or body.get("to") or ""),
                       screen=screen, who=str(body.get("who") or ""))
        with self._locked():
            self.tick()
            if self.machine is None:
                raise ValueError("There is no schedule loaded, so there is "
                                 "no list to edit. " + self.error)
            out = self._apply(ev)
            if out.refused:
                raise ValueError(out.refused)
        return self.tonight_view()

    def hold_for_announcement(self, who, screen, detail=None):
        """Put the scheduler on Hold exactly as the operator's own Hold
        does, `who` and `screen` carried through so the journal attributes
        it the same way (Jeff, 2026-09-26: "any announcement actually just
        auto triggers a hold"). `detail`, when given, replaces the journal
        line's own "pressed Hold" wording (see schedule.py's _hold): an
        announcement's own claim reads as held FOR the announcement, not
        as an indistinguishable operator Hold press (review round 2,
        2026-09-26: audit15_journal_noise2.py). Wired by web.py's serve()
        as AnnounceService.hold_requester, alongside state_provider and
        on_show_started; see announce.py's play().

        Checks the state FIRST: already on Hold or already paused is
        success without ever issuing HOLD_ON, so the routine case (a
        second announcement, or this same call succeeding twice) never
        writes a "Hold was refused" line for something that was not
        actually refused from the operator's point of view (review round
        2, audit15_journal_noise.py / audit15_journal_noise2.py).

        Returns (refusal_or_None, epoch): epoch is self.hold_epoch right
        after this call, for hold_still_claimed to compare against later
        -- seeing THIS claim through, not just seeing the same state
        again by coincidence (review round 2: audit15_resume_race.py)."""
        with self._locked():
            self.tick()
            if self.machine is None:
                return self.error or "There is no schedule loaded.", \
                    self.hold_epoch
            if self.machine.state in (sch.HOLD, sch.PAUSED):
                return None, self.hold_epoch
            out = self._apply(sch.Event(sch.HOLD_ON, "operator", who=who,
                                        screen=screen, detail=detail or ""))
            return out.refused or None, self.hold_epoch

    def hold_still_claimed(self, claim_epoch):
        """True if the schedule is still on Hold or paused AND nothing has
        crossed a Hold/Resume boundary since `claim_epoch` was captured
        (see hold_for_announcement). Read-only: this NEVER issues Hold
        itself, unlike the old second check it replaces -- an operator's
        own Resume, pressed while an announcement is still loading, always
        wins, rather than being silently undone by the announcement's own
        recheck re-Holding the show it was just resumed from (review round
        2, 2026-09-26: audit15_resume_race.py). Comparing the epoch, not
        only the state, is what catches a Resume immediately followed by a
        fresh Hold from someone else: the state looks the same again, but
        this claim is still stale."""
        with self._locked():
            self.tick()
            if self.machine is None:
                return False
            return (self.hold_epoch == claim_epoch
                    and self.machine.state in (sch.HOLD, sch.PAUSED))

    # -- the journal, for the page -------------------------------------------
    def journal_view(self, n=journal.PAGE_LINES):
        """The log strip: the last 20 journal lines, newest first, exactly as
        they are in the file, each with its age."""
        return {"ok": True,
                "as_of": self.logbook.local().isoformat(timespec="seconds"),
                "lines": self.logbook.recent(n)}

    def logging_view(self):
        return self.logbook.health()

    def _check_screen(self, screen, who, action, what):
        """The screen's name as the list spells it. A blank one is left for
        the engine to refuse with its own sentence; one not on the list is
        refused here, in the journal and to the page."""
        if not screen.strip():
            return screen
        names = {n.lower(): n for n in self.screens}
        got = names.get(screen.strip().lower())
        if got is not None:
            return got
        sentence = (f"{screen.strip()!r} is not on the screen list "
                    f"({', '.join(self.screens)}). Pick a screen from the "
                    f"list. Nothing was changed.")
        with self._locked():
            self._log(self.logbook.record, actor="operator", action=action,
                      outcome="refused", reason=sentence,
                      text=f"{who.strip() or 'An unnamed operator'}'s "
                           f"{what} was refused. {sentence}",
                      state=self._state_name(), night=self._night(),
                      who=who.strip() or "unnamed operator",
                      screen=screen.strip())
        raise ValueError(sentence)

    def _read_rule_text(self):
        try:
            with open(self.path, encoding="utf-8-sig") as fh:
                return fh.read(1 << 20)
        except OSError as e:
            return f"(could not be read: {e.strerror or e})"

    def _config_in_force(self, text=None):
        """Everything in force, from memory; `text` is the rule file as read
        from disk, which the caller reads outside the scheduler's lock."""
        m = self.machine
        return {"schedule_file": self.path, "schedule_file_text": text,
                "rule": sch.rule_to_doc(self.rule) if self.rule else None,
                "rule_error": self.error or None,
                "operators": list(self.operators),
                "tonight": sch.machine_to_doc(m) if m else None,
                "tonight_file": (tonight_path(m.date, self.state_dir)
                                 if m else None),
                "dry_run": DRY_RUN, "state_dir": self.state_dir,
                "screens": list(self.screens),
                "log_folder": self.logbook.folder,
                "clock_check": self.clock_check}

    def save_incident(self, body):
        """Save the last incident, for an operator on the list, from a named
        screen. Starts, stops and arms nothing."""
        body = body or {}
        who = str(body.get("who") or "").strip()
        screen = str(body.get("screen") or "").strip()
        if not who or not screen:
            raise ValueError("Save the last incident has to say who pressed "
                             "it and which screen it came from, so the "
                             "journal can say who did what. Nothing was "
                             "saved.")
        names = {n.lower(): n for n in self.operators}
        if who.lower() not in names:
            raise ValueError(f"{who!r} is not on the operator list "
                             f"({', '.join(self.operators)}). Pick a name "
                             f"from the list. Nothing was saved.")
        screen = self._check_screen(screen, who, "save incident",
                                    "Save the last incident")
        text = self._read_rule_text()
        with self._locked():
            config = self._config_in_force(text)
            state, night = self._state_name(), self._night()
        # The copy happens outside the scheduler's lock: saving a folder of
        # files must never hold up a tick.
        return self.logbook.save_incident(
            who=names[who.lower()], screen=screen, state=state, night=night,
            config=config, why=str(body.get("why") or ""))

    def operator_view(self):
        return {"ok": True, "current_operator": self.current_operator,
               "operators": list(self.operators)}

    def set_operator(self, body):
        """Choose who is currently operating the rig (Jeff, 2026-10-01: a
        name before the Stream Deck's actions unlock, no password -- see
        load_current_operator). Starts, stops, holds and arms nothing by
        itself; it only names who is about to press something. An empty
        name clears the selection (nobody operating)."""
        body = body or {}
        name = str(body.get("who") or "").strip()
        screen = str(body.get("screen") or "").strip()
        if name:
            names = {n.lower(): n for n in self.operators}
            if name.lower() not in names:
                raise ValueError(f"{name!r} is not on the operator list "
                                 f"({', '.join(self.operators)}). Pick a "
                                 f"name from the list. Nothing was changed.")
            name = names[name.lower()]
        screen = self._check_screen(screen, name, "operator",
                                    "Choosing the operator")
        with self._locked():
            try:
                save_current_operator(self.state_dir, name)
            except OSError as e:
                raise ValueError(f"The current operator could not be saved: "
                                 f"{e}. Nothing was changed.") from None
            self.current_operator = name
            self._log(self.logbook.record, actor="operator", action="operator",
                      outcome="done", reason="",
                      text=(f"{self._who_text(screen)} chose {name} as the "
                            f"operator." if name else
                            f"{self._who_text(screen)} cleared the operator "
                            f"selection."),
                      state=self._state_name(), night=self._night(),
                      who=name or "unnamed operator", screen=screen)
        return self.operator_view()

    @staticmethod
    def _who_text(screen):
        return f"The {screen}" if screen else "Someone"

    def deck_event(self, body):
        """One line from the REAL Stream Deck (ltcplay/streamdeck.py, a
        SEPARATE process, never this one): every arm, disarm, Abort and
        refusal it journals locally is also posted here (safety review of
        PR #31, item 9), off the deck's own main loop exactly like its
        operator lookup, so a hung or slow web server can never delay a
        key read or an arm-frame send on the deck's side. This writes ONE
        journal line and nothing else: it starts, stops, holds and arms
        nothing, so it does not touch the "no route that starts, stops,
        holds or arms anything" rule above.

        `who` is the operator the deck itself last read, or "" when none
        was chosen or the deck could not say (e.g. Abort and disarm, which
        are never gated on one). An empty `who` is written as the
        "system" actor, never as an unnamed operator: an operator event
        with no name is refused by build_event, and rightly so."""
        body = body or {}
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError("A deck event needs its text. Nothing was "
                             "written.")
        who = str(body.get("who") or "").strip()
        screen = str(body.get("screen") or "Stream Deck").strip()
        action = str(body.get("action") or "deck").strip() or "deck"
        fault = bool(body.get("fault"))
        actor = "operator" if who else "system"
        kw = dict(action=action, state=self._state_name(),
                  night=self._night())
        if actor == "operator":
            kw["who"], kw["screen"] = who, screen
        if fault:
            self._log(self.logbook.fault, actor, text, **kw)
        else:
            self._log(self.logbook.record, actor=actor, outcome="done",
                      reason=text, text=text, **kw)
        return {"ok": True}

    # -- the web routes ---------------------------------------------------
    GET_ROUTES = ("/api/schedule", "/api/schedule/tonight",
                  "/api/schedule/state", "/api/schedule/journal",
                  "/api/schedule/logging", "/api/schedule/operator")
    # Editing tonight's list, saving an incident, choosing the operator and
    # recording a line the real Stream Deck process posts about its own
    # arm/disarm/Abort actions (2026-10-01, review item 9) are the only
    # things that can be posted. deck-event only writes a journal line;
    # there is still deliberately no route that starts, stops, holds or
    # arms anything.
    POST_ROUTES = ("/api/schedule/tonight", "/api/schedule/incident",
                   "/api/schedule/operator", "/api/schedule/deck-event")

    def get(self, route):
        if route == "/api/schedule":
            return 200, self.rule_view()
        if route == "/api/schedule/tonight":
            return 200, self.tonight_view()
        if route == "/api/schedule/state":
            return 200, self.state_view()
        if route == "/api/schedule/journal":
            return 200, self.journal_view()
        if route == "/api/schedule/logging":
            return 200, self.logging_view()
        if route == "/api/schedule/operator":
            return 200, self.operator_view()
        return 404, {"error": "no such thing here"}

    def post(self, route, body):
        if route == "/api/schedule/tonight":
            return 200, self.edit_tonight(body)
        if route == "/api/schedule/incident":
            out = self.save_incident(body)
            return (200 if out["ok"] else 500), out
        if route == "/api/schedule/operator":
            return 200, self.set_operator(body)
        if route == "/api/schedule/deck-event":
            return 200, self.deck_event(body)
        return 404, {"error": "no such thing here"}
