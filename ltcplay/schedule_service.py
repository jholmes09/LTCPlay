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
import itertools
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
from . import benchhooks
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
# "iPad": the show-network remote page (ltcplay/remote.py) signs a device in
# under one of these names, so a list written fresh already has it.
DEFAULT_SCREENS = ("Rack screen", "Stream Deck", "Phone", "iPad")
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
# The one way out of the dry run, per Service, never this module's constant:
# fire_ice.py sets Service.dry_run False, and attaches the performer that
# then has to report what the dry run used to make up (a show ending, the
# closing finishing), only when the Fire & Ice config says
# "scheduler_performs": true. Every other Service, and the GPL path, which
# never builds one, keeps DRY_RUN.
PERFORMING_NOTE = ("The scheduler performs: it starts the show cue on "
                   "schedule (only once Run has been pressed), and the "
                   "show ends when its audio does.")


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

SOAK_ONLY_REFUSED = (
    "This schedule was written by the soak test (\"soak_only\": true) and "
    "only the soak's own engine may use it, so no show is scheduled from "
    "it and nothing is started, faded or stopped. Put the show's own "
    "schedule file back, then quit and start LTC Player again.")


def load_rule(path, env=None, ppid=None):
    """Read and validate the rule file. Every failure is one sentence (or a
    list of them) naming the file.

    A schedule marked "soak_only" (the soak kit's, packaging/windows/
    soak.py) is refused unless the soak itself started this engine
    (benchhooks.soak_started): a soak schedule left behind on a show PC
    must never start anything unattended (Pico, 2026-10-23). `env` and
    `ppid` are the selftest's."""
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
    rule = sch.parse_rule(text, where=path)
    if json.loads(text).get("soak_only") is True and \
            not benchhooks.soak_started(env, ppid):
        raise sch.RuleError([SOAK_ONLY_REFUSED], where=path)
    return rule


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
        self.show = 0       # start_show only: which show
        self.stops = None   # show_starting only: the Stop count then
        self.ok, self.sentence = None, ""
        self.done = threading.Event()


# The start of the Hold's detail when a Stop between shows holds the
# schedule (Remote.halt, "stop-hold"): the journal then reads as the Stop.
STOP_HOLD_DETAIL = sch.STOP_HOLD_DETAILS[0]


class _ConductorCalls:
    """Every show conductor request, in the order the scheduler decided
    them, made on ONE thread of its own, never with the scheduler's lock
    held. One FIFO and one thread is what keeps Hold, Resume and Abort in
    order now that they no longer run inside the lock: two requests can
    never be made at once or overtake each other. A conductor request that
    is slow (its lock is held across a device call) holds up only the
    requests behind it, never a tick or a status poll.

    With ONE exception: an Abort, or a Stop (Jeff, 2026-10-30: Stop has
    every guarantee Abort had), never waits more than URGENT_WAIT_S behind
    another request. If the line has not reached it by then (the request
    in front of it is stuck, or the line's thread has died), it is taken
    out of the line and made at once on a thread of its own. The requests
    still waiting in front of it that the Abort supersedes (a Hold, a
    Resume, a show start, and above all a Reset, which was pressed before
    this Abort and must not clear it) are not sent at all; intermission and
    show_stopped stay in the line (they only ever take the rig dark, and
    the conductor ignores them while latched; failed_start too, for the
    same reason). A Stop supersedes the same, except a Reset
    (SUPERSEDED_BY_STOP). Requests decided after the Abort or Stop stay
    in the line, in order.

    The thread starts with the first request, so a Service with no
    conductor never has one."""

    URGENT_WAIT_S = 0.25
    # What a Stop (STOP_SHOW, every Stop: the screens' double tap and the
    # Stream Deck's halt) takes out of the line if it is still waiting
    # there, decided before it: a show start, so the music and video never
    # start only to fade (review of #130, second pass, 4), and a Hold or
    # Resume, so nothing pressed before the Stop runs after it (Jeff,
    # 2026-10-30: Stop has every guarantee Abort had). Never a Reset: Stop
    # latches nothing, so a Reset has nothing of the Stop's to clear.
    STOP_DROPS = ("hold", "resume", "show_starting", "start_show")
    SUPERSEDED_BY_ABORT = ("reset", "hold", "resume", "show_starting",
                           "start_show")
    SUPERSEDED_BY_STOP = STOP_DROPS
    # The conductor requests that never wait more than URGENT_WAIT_S
    # behind the line: an Abort, and a Stop (Conductor.stop).
    URGENT = ("abort", "stop")

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
            urgent = call.method in self.URGENT and (
                self._busy or len(self._q) > 1 or not self._alive())
        if urgent:
            threading.Thread(target=self._mind, args=(call,), daemon=True,
                             name=f"ltcplay-conductor-{call.method}").start()

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
        """An Abort or a Stop that found the line busy: give the line
        URGENT_WAIT_S to reach it, then make it here instead."""
        with self._cv:
            if self._cv.wait_for(lambda: call not in self._q,
                                 self.URGENT_WAIT_S):
                return                  # the line reached it in time
            self._q.remove(call)
            ahead = self._current
            age = (self._clock() - self._since) if ahead is not None else 0.0
            gone = (self.SUPERSEDED_BY_ABORT if call.method == "abort"
                    else self.SUPERSEDED_BY_STOP)
            dropped = [c for c in self._q if c.seq < call.seq and
                       c.method in gone]
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

    def drop(self, methods, before_seq):
        """Take the requests still waiting in the line whose method is in
        `methods` and that were decided before `before_seq` out of it, and
        return them: they are not sent. For Stop (STOP_DROPS): a Hold, a
        Resume or a show start still waiting is not sent once the show has
        been stopped. Nothing else: no latch, and the request the line is
        making now is left alone (an urgent Stop goes beside it after
        URGENT_WAIT_S)."""
        with self._cv:
            gone = [c for c in self._q
                    if c.method in methods and c.seq < before_seq]
            for c in gone:
                self._q.remove(c)
                c.ok, c.sentence = False, "not sent: Stop came first"
                c.done.set()
            self._cv.notify_all()
            return gone

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
        # Press order across every way in (review of #46/#48: the screen
        # Abort now does work of its own before it reaches this service, so
        # the order presses reach the lock is not the order they were
        # pressed in). stamp_press() is taken the moment a press arrives;
        # a Reset stamped before the latest Abort never ends it.
        self._press_counter = itertools.count(1)
        self._press_stamp = None
        self._abort_pressed = 0
        # A stuck or dead line of conductor requests, once it has been
        # journaled as a fault: {"key", "text"}. See _watch_conductor.
        self._conductor_trouble = None
        # Dry run or not, for THIS service (see PERFORMING_NOTE), and what
        # starts a show cue and reports back once it is not. Both are set
        # together by fire_ice.attach() and nothing else; a performer is
        # only ever asked for anything while dry_run is False.
        self.dry_run = DRY_RUN
        self.performer = None
        # Start now waiting for the show audio (_start_now_refusal): how
        # many wait now, and (number, why) bumped by cancel_start_wait.
        self._start_waiters = 0
        self._start_cancel = (0, "")
        # () -> a sentence that refuses Start now, or "": the Programming
        # playhead's (remote.programming_playback_refusal, set by the
        # remote). Asked under this lock after the tick, so a Play from
        # that landed while a Start now waited is never started over.
        self.start_veto = None
        # Stops under way (halt_begin / halt_end).
        self._halts = 0
        # A Stop's Hold that stands until Resume (see _keep_stop_hold),
        # read back from its file at start.
        self._stop_carry = None
        self._stop_carry_dirty = False
        self._stop_carry_failing = False
        self._halt_count_lock = threading.Lock()
        # Whether the operator has pressed Run (a show file is running), set
        # by fire_ice.attach() with the conductor. While it says no, nothing
        # the scheduler decides by itself reaches the show conductor, so no
        # fade, blank, MadMapper or BEYOND command and no flame cue goes out
        # unattended (Jeff's standing rule: no auto-Run; Pico, 2026-10-23).
        # Only the show start itself is still handed on, to be refused by
        # the performer, which checks Run on its own. None (no conductor,
        # or one built without fire_ice) gates nothing, as before.
        self.run_gate = None
        # alerts.Alerts.raise_(kind, text), set by remote.Remote: a failed
        # start raises a "start" alert for the rack screen and the booth
        # (Jeff, 2026-10-12: the groups are left as they are, so a person
        # has to be told). None: journaled only, as before.
        self.alert_sink = None
        # () -> a sentence naming the open alerts, or None, set by
        # remote.Remote (alerthold.py): at showtime, with an alert open,
        # the schedule Holds instead of starting the show (Jeff,
        # 2026-10-26). A running show is never touched. `on_alert_hold`
        # (epoch, why) is told after such a Hold, outside the lock.
        # showtime_gate returns None or (sentence, the open alerts' ids).
        self.showtime_gate = None
        self.on_alert_hold = None
        # (night, show) -> the open alert id sets it was held for, or that
        # an operator Resumed it with (_alert_handled).
        self._alert_held = {}
        # Called after every tick of the running scheduler, outside the
        # lock (_run): remote.Remote._alert_tick.
        self.tick_hooks = []
        # The pre-roll (preroll.PreRoll, Jeff 2026-10-28), set by web.serve
        # for a Fire & Ice engine whose scheduler performs: the rig is
        # checked PreRoll.window_s before a show's time, and before Start
        # now. None: no pre-roll, every start exactly as before.
        self.preroll = None
        # (night, show) -> late_ok_s, for a show whose pre-roll could only
        # run at its time: it starts late rather than unchecked.
        self._preroll_late = {}
        self._preroll_seq = 0
        self.ntp_query = ntp_query
        # Injected so a test can move the wall clock and perf_counter apart
        # on purpose, deterministically, with nothing asleep and no real
        # time passing. See _watch_clock().
        self._perf_counter = perf_counter or _time.perf_counter
        self._last_wall = None
        self._last_perf = None
        self._clock_trust_lost = False
        self.state_dir = state_dir or data_dir()
        # A Stop's Hold kept on disk (_keep_stop_hold): read back at start.
        self._stop_carry = self._stop_carry_load()
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
            Abort. A failed start fades the same way but goes to
            failed_start(): dark, the flame groups left as they are, no latch
            (Jeff, 2026-10-03). A show cut by a restart goes to
            show_stopped(): dark, no disarm, no latch.
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
            elif ev.kind == sch.STOP_SHOW and \
                    ev.detail != sch.RESET_DETAIL:
                # Every operator's Stop (Jeff, 2026-10-30): Abort's whole
                # sequence at the conductor, urgent in the line, with no
                # latch (Conductor.stop). Reset's end of a paused show
                # stays the plain dark of show_stopped.
                plan.append(("Stop", "stop", self._ABORT_EFFECTS))
            elif ev.kind == sch.SHOW_FAILED:
                plan.append(("Failed start", "failed_start",
                             self._ABORT_EFFECTS))
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
                not any(p[1] in ("show_stopped", "failed_start")
                        for p in plan):
            # A start (or a restart) while the rig is meant to be dark: the
            # dark sequence it was sent before may never have gone out (the
            # process can die after the Abort or failed start was saved but
            # before the conductor call ran), and a fresh conductor knows
            # nothing of it. So it is always sent again.
            plan.append((self.DARK_AGAIN, "show_stopped", frozenset()))
        if sch.START_SHOW in kinds and not self.dry_run and \
                self.performer is not None:
            # Fire & Ice, scheduler performing (fire_ice.ShowRunner): the
            # show cue is started on the show audio, as one request in the
            # same ordered line as every conductor request, off the
            # scheduler's lock, after tonight is saved. The conductor is
            # NOT told a show started here: that stays on SHOW_CONFIRMED,
            # once the runner has seen the timecode moving (above). A start
            # the runner refuses (Run not pressed, the conductor latched,
            # no show audio) is its to report as SHOW_FAILED, which brings
            # failed_start(). An Abort decided before it ran supersedes it
            # (_ConductorCalls.SUPERSEDED_BY_ABORT). The scheduler itself
            # never even gets here while its Abort latch is set: a show
            # that comes due then is MISSED.
            shows = [e.show for e in out.effects if e.kind == sch.START_SHOW]
            plan.append(("Start the show", "start_show",
                         frozenset((sch.START_SHOW,)), shows[0]))
        return plan

    RUN_NOT_PRESSED = ("Run has not been pressed, so the scheduler sends "
                       "nothing to the rig: no fade, no blank, no MadMapper "
                       "or BEYOND command and no flame cue.")

    def _run_pressed(self):
        """True while the operator's Run is in force, or when this service
        has no Run to wait for (run_gate None). A gate that raises counts as
        not pressed: nothing goes out on a guess."""
        gate = self.run_gate
        if gate is None:
            return True
        try:
            return gate() is True
        except Exception:      # noqa: BLE001  never the scheduler
            return False

    def _hold_back_unattended(self, plan, ev):
        """(sent, held back): what the scheduler decided BY ITSELF (every
        event but an operator's press) is not sent to the show conductor
        while Run is not pressed. The show start alone still goes, to the
        performer, which refuses it with nothing sent (and reports that as
        a failed start that sent nothing)."""
        if not plan or ev.actor == "operator" or self._run_pressed():
            return plan, []
        return ([p for p in plan if p[1] == "start_show"],
                [p for p in plan if p[1] != "start_show"])

    def _reworded(self, le):
        """A log event as this engine says it: with a show conductor
        attached, the engine's "nothing was disarmed" sentence is replaced
        by what the conductor does (it disarms)."""
        if self.conductor is not None and le.action == sch.ABORT and \
                sch.NOTHING_DISARMED in le.text:
            # The engine never disarms; the conductor's abort() does.
            le = replace(le, text=le.text.replace(
                sch.NOTHING_DISARMED, self.CONDUCTOR_DISARMS))
        # A failed start's line is the same with or without a conductor
        # (ruling 1, 2026-10-12): neither disarms a flame group.
        return le

    FAILED_START_ALERT = ("The rig is dark and every flame group stays as "
                          "it was. Press Acknowledge once you have seen it; "
                          "Start now or the next show brings the rig back.")

    REFUSED_START_ALERT = ("Nothing was sent to the rig for it: no fade, no "
                           "MadMapper or BEYOND command, and every flame "
                           "group stays as it was. Press Acknowledge once "
                           "you have seen it.")

    @staticmethod
    def start_alert(show, why, tail):
        """A failed or refused start's alert (Jeff's field report,
        2026-10-28): which show, the real reason in plain words with what
        to press (the runner's own sentence, the same one the slot's FAULT
        reason carries), then what was or was not sent. Never only "The
        show did not start"."""
        why = str(why or "").strip().rstrip(".")
        if not why:
            why = ("no reason was given; the journal has the lines around "
                   "it")
        head = f"Show {show}" if show else "The show"
        return f"{head} did not start: {why}. {tail}"

    def _record(self, out, now, plan=(), held=(), ev=None):
        for le in out.log:
            self._record_logevent(self._reworded(le))
            # A failure reported too late to be a failed start is a fault
            # with the show still running (schedule._show_failed): it is no
            # "did not start" alert (review of #144, F8).
            if le.action == sch.SHOW_FAILED and \
                    le.outcome != "recorded as a fault" and \
                    self.alert_sink is not None:
                tail = (self.REFUSED_START_ALERT
                        if ev is not None and ev.nothing_sent and
                        not out.effects else self.FAILED_START_ALERT)
                alert = self.start_alert(
                    le.show or (ev.show if ev is not None else 0),
                    ev.detail if ev is not None else "", tail)
                try:
                    self.alert_sink("start", alert)
                except Exception as e:   # noqa: BLE001  never the scheduler
                    self._journal_line(
                        "system", f"The failed-start alert could not be "
                                  f"raised ({type(e).__name__}: {e}); the "
                                  f"journal line above is the only notice.",
                        action="alert", outcome="not raised", fault=True)
        for entry in held:
            self._journal_line(
                "system", f"Not sent to the show conductor, {entry[0]}: "
                          f"{self.RUN_NOT_PRESSED}",
                action="conductor", outcome="held back",
                reason=f"{entry[1]} held back, Run not pressed")
        claimed = set()
        for entry in plan:
            label, method, kinds = entry[:3]
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
                "system",
                f"Not performed, dry run: {self._desc(eff)}." if self.dry_run
                else f"Not performed: nothing in this build carries out "
                     f"{self._desc(eff)}.",
                action=eff.kind, outcome="not performed",
                reason="dry run, no transport in this build" if self.dry_run
                else "no performer for this effect in this build",
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
        "stop": (" An operator's Stop: flame cues zeroed and every flame "
                 "group sent a disarm at once, the lasers blanked at once "
                 "and again after the fade, video, pixels and music faded "
                 "to black, the video stopped. Nothing is latched; with a "
                 "show still to come the schedule is on Hold until "
                 "Resume."),
        "show_stopped": (" The rig goes dark and stays dark until an "
                         "operator presses Start now or the next show "
                         "starts. No flame group is disarmed and nothing "
                         "is latched, so no Reset is needed."),
        "failed_start": (" The show failed to start: the rig goes dark "
                         "until an operator presses Start now or the next "
                         "show starts, and every flame group is left as it "
                         "was (Jeff, 2026-10-12). Nothing is latched, so no "
                         "Reset is needed. An alert on the rack screen "
                         "waits for an Acknowledge."),
        "intermission": (" Out of the show: flame cues to zero and the "
                         "lasers blanked."),
    }

    @staticmethod
    def _desc(eff):
        return eff.kind + (f" show {eff.show}" if eff.show else "") + \
            (f" over {eff.seconds:g} s" if eff.seconds else "")

    # -- the show conductor's calls, in order, off the scheduler's lock -------
    def _decide_conductor(self, plan, ev):
        """The conductor requests a step decided, numbered now, in the order
        they will be sent (_queue_conductor sends them, after the save). An
        Abort among them is stamped here, in the same locked instant _apply
        sets the Abort latch: from that instant every Reset numbered or
        pressed before it is older than it (_after_reset, reset_conductor).
        The stamps used to be set only when the Abort was queued, after
        tonight's save, so a Reset the conductor answered during a slow
        save (0.2 s of fsync on a shared CI runner) found the latch set
        but the Abort not yet numbered, and cleared it (#46's Windows run,
        2026-10-08)."""
        calls = []
        if not plan:
            return calls
        op = ev.actor == "operator"
        who = ev.who if op else "the scheduler"
        screen = ev.screen if op else ""
        for entry in plan:
            label, method = entry[0], entry[1]
            call = self._new_call(label, method, who, screen)
            if len(entry) > 3:
                call.show = entry[3]
            if method == "show_starting":
                # The Stop count now: a Stop that lands before this call
                # is made refuses it (cold review of #191 round 2).
                call.stops = getattr(self.conductor, "stops", None)
            if method == "abort":
                self._abort_seq = call.seq
                self._abort_pressed = (self._press_stamp
                                       if self._press_stamp is not None
                                       else self.stamp_press())
            calls.append(call)
        return calls

    def _queue_conductor(self, plan, ev, calls=None):
        if not plan:
            return
        if calls is None:
            calls = self._decide_conductor(plan, ev)
        for call in calls:
            self._calls.put(call)

    def stamp_press(self):
        """A number for a press, taken the moment it arrives, before any
        lock or other work: later presses get larger numbers."""
        return next(self._press_counter)

    def _new_call(self, label, method, who, screen):
        self._call_seq += 1
        return _ConductorCall(label, method, who, screen, self._call_seq)

    def _abort_overtook(self, call, ahead, age, dropped):
        """On the Abort's (or the Stop's) own thread: it was sent ahead of a
        request that had not answered, and the requests in `dropped`,
        decided before it and superseded by it, are not sent at all."""
        what_kind = "a Stop" if call.method == "stop" else "an Abort"
        with self._locked():
            what = (f"{ahead.label}, which had not answered for {age:.1f} s"
                    if ahead is not None else
                    "the requests in front of it, because the line of "
                    "requests to the show conductor was not moving")
            others = [c.label for c in dropped if c.method != "reset"]
            self._journal_line(
                "system", f"{call.label} was sent to the show conductor at "
                f"once, ahead of {what}."
                + (f" Not sent, because the {call.label} supersedes them: "
                   f"{', '.join(others)}." if others else ""),
                action="conductor", outcome="sent ahead")
            for r in dropped:
                r.ok = False
                r.sentence = (f"{r.label} was not sent: {what_kind} "
                              f"overtook it.")
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
        if call.who == "the scheduler" and call.method != "start_show" and \
                not self._run_pressed():
            # Decided while Run was in force, but it no longer is (Stop
            # was pressed before this call's turn): still nothing goes out.
            call.ok = False
            call.sentence = f"{call.label} was not sent: " \
                            f"{self.RUN_NOT_PRESSED}"
            with self._locked():
                self._journal_line(
                    "system", f"Not sent to the show conductor, "
                              f"{call.label}: {self.RUN_NOT_PRESSED}",
                    action="conductor", outcome="held back",
                    reason=f"{call.method} held back, Run not pressed")
            return
        try:
            if call.method == "start_show":
                # The Fire & Ice show runner, not the conductor (see
                # _drive_conductor).
                r = self.performer.start_show(call.show, who=call.who)
            elif call.method == "show_starting" and \
                    getattr(call, "stops", None) is not None:
                r = self.conductor.show_starting(call.who, call.screen,
                                                 stops=call.stops)
            else:
                r = getattr(self.conductor, call.method)(call.who,
                                                         call.screen)
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
            elif call.method == "start_show":
                # A refused start is the runner's to report, as SHOW_FAILED
                # (one report, from one place); this only says what happened.
                # An automatic start is always journaled, done or not.
                what = ("Automatic show start" if call.who ==
                        "the scheduler" else f"Start now by {call.who}")
                self._journal_line(
                    "system", f"{what}, show {call.show}: {said}".strip(),
                    action="automatic start" if call.who == "the scheduler"
                    else "start now",
                    outcome="done" if ok else "refused",
                    reason=said or ("done" if ok else "refused"),
                    show=call.show or None, fault=not ok)
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
        pressed = getattr(call, "pressed", None)
        if call.seq < self._abort_seq or (
                pressed is not None and pressed < self._abort_pressed):
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

    def reset_conductor(self, who, screen, wait_s=2.0, pressed=None,
                        surface=False):
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
        stamp = self.stamp_press()
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
        if surface:
            # The remote page's Reset: `who` is the surface (see
            # operator_press).
            names[who.lower()] = who
        if who.lower() not in names:
            self._refuse_reset(who, screen, f"{who!r} is not on the operator "
                               f"list ({', '.join(self.operators)}). Nothing "
                               f"was reset.")
        screen = self._check_screen(screen, who, "reset", "Reset")
        when = pressed if pressed is not None else stamp
        with self._locked():
            late = when < self._abort_pressed
            if not late:
                call = self._new_call("Reset", "reset", names[who.lower()],
                                      screen)
                call.pressed = when
                self._calls.put(call)
        if late:
            # An Abort pressed after this Reset has already been decided
            # (it reached the scheduler first): the Reset is never sent, so
            # it can neither end that Abort nor reset the conductor behind
            # its back.
            self._refuse_reset(who, screen, "Reset was pressed before the "
                               "latest Abort, so that Abort is still in "
                               "force. Press Reset again.")
        if not call.done.wait(wait_s):
            out = {"ok": False, "text": "Reset is queued behind the show "
                                        "conductor's earlier work and has "
                                        "not answered yet."}
        else:
            out = {"ok": call.ok, "text": call.sentence}
        return out

    def reset_hold(self, who, screen, surface=False):
        """The rack screen's Reset while the schedule is on Hold or has a
        fault flag up and no Abort stands (Jeff's field report, 2026-10-28:
        "I play the show, press Hold, and Reset does nothing"). The remote
        route asks this first and reset_conductor only when this returns
        None, so while an Abort stands Reset is the Abort's alone, exactly
        as before (a second Reset then ends a Hold). The scheduler's own
        events, each journaled with who and where:

          PAUSED  the paused show ends: STOP_SHOW, faded to dark like a
                  Stop, nothing latched and no flame group disarmed;
          HOLD    the Hold between shows ends: RESUME, back to waiting;
          faults  the fault flag is cleared: CLEAR_FAULT.

        Reset never starts or continues a show by itself (a delayed show
        still waits for Start now). {"ok", "text"}, or None when there was
        nothing of these to do. Raises ValueError, journaled, for a blank
        or unknown operator or screen, as reset_conductor does."""
        m = self.machine
        if m is None or self._aborted() or not (
                m.state in (sch.PAUSED, sch.HOLD) or m.faults):
            return None
        who = str(who or "").strip()
        screen = str(screen or "").strip()
        if not who or not screen:
            self._refuse_reset(who, screen, "Reset has to say who pressed it "
                               "and which screen it came from. Nothing was "
                               "reset.")
        names = {n.lower(): n for n in self.operators}
        if surface:
            names[who.lower()] = who
        if who.lower() not in names:
            self._refuse_reset(who, screen, f"{who!r} is not on the operator "
                               f"list ({', '.join(self.operators)}). Nothing "
                               f"was reset.")
        who = names[who.lower()]
        screen = self._check_screen(screen, who, "reset", "Reset")
        texts, ok = [], True
        with self._locked():
            self.tick()
            m = self.machine
            if m is None or self._aborted():
                # An Abort decided meanwhile: Reset is its, with its own
                # ordering rules (reset_conductor).
                return None
            kinds = []
            stop_held = m.state == sch.HOLD and (
                m.stop_hold or self._stop_carry is not None)
            if m.state == sch.PAUSED:
                kinds.append(sch.STOP_SHOW)
            elif m.state == sch.HOLD and not stop_held:
                kinds.append(sch.RESUME)
            if m.faults:
                kinds.append(sch.CLEAR_FAULT)
            if stop_held:
                # A Stop's Hold ends only with Resume (Jeff, 2026-10-30;
                # cold review of #191, A-F4): Reset may clear the fault
                # flag, never this Hold. Said, and journaled.
                texts.append(self.RESET_KEEPS_STOP_HOLD)
                self.journal_press(who, screen, "reset",
                                   "done" if kinds else "refused",
                                   f"{who}'s Reset on the {screen} left the "
                                   f"Stop's Hold on. "
                                   f"{self.RESET_KEEPS_STOP_HOLD}")
                if not kinds:
                    return {"ok": False, "text": texts[0],
                            "stop_hold": True}
            if not kinds:
                return None
            for kind in kinds:
                out = self._apply(sch.Event(
                    kind, "operator", who=who, screen=screen,
                    confirmed=True, surface=bool(surface),
                    detail=sch.RESET_DETAIL))
                if out.refused:
                    ok = False
                    texts.append(out.refused)
                else:
                    texts.append(" ".join(self._reworded(le).text
                                          for le in out.log if le.text))
        return {"ok": ok, "text": " ".join(t for t in texts if t)}

    RESET_KEEPS_STOP_HOLD = ("Reset does not end the Hold a Stop put on: "
                             "press Resume when it is safe. No show starts "
                             "until then.")

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
        # Every caller already holds the lock; this makes it so for any
        # that does not (the selftest steps the machine directly), because
        # the Abort latch, the Abort's number and the save that records
        # them have to be one step to a Reset answered on the conductor's
        # thread (_after_reset runs under the same lock). The lock is
        # reentrant and _locked() counts depth, so nothing changes for a
        # caller inside it.
        with self._locked():
            return self._apply_locked(ev, now)

    def _apply_locked(self, ev, now=None):
        now = now or self.clock()
        before = self.machine
        if ev.kind in self.LATCH_EVENTS and self._aborted():
            # Jeff: after an Abort the rig stays dark until the operator
            # acts, and Reset is that act. The engine misses a show that
            # comes due, refuses Start now, and keeps a Hold or Resume dark
            # until then.
            ev = replace(ev, latched=True)
        out = sch.step(self.machine, ev, now)
        if not out.refused and ev.kind in (sch.HOLD_ON, sch.ABORT,
                                          sch.STOP_SHOW):
            # Any Hold that is taken (an operator's, an announcement's, the
            # engine's own at showtime for an open alert), an Abort or a
            # Stop ends a Start now waiting for the show audio: it is
            # refused under this lock before it can start a show over the
            # Hold (re-review of #144, N1).
            if not (ev.kind == sch.HOLD_ON and str(ev.detail or "")
                    .startswith(sch.STOP_HOLD_DETAILS[1])):
                # (A Stop's Hold put back by the engine is not a new
                # press: it never cancels an operator's Start now, which
                # may start its one show from it. Round 3, L1.)
                self.cancel_start_wait(
                    "Stop was pressed" if ev.kind == sch.HOLD_ON and
                    str(ev.detail or "").startswith(sch.STOP_HOLD_DETAILS)
                    else "the schedule was put on Hold"
                    if ev.kind == sch.HOLD_ON
                    else "the show was aborted" if ev.kind == sch.ABORT
                    else "Stop was pressed")
        self.machine = out.machine
        plan, held = self._hold_back_unattended(
            self._drive_conductor(out, ev), ev)
        # Numbered (and an Abort stamped) now, with the latch, not when
        # they are sent after the save: see _decide_conductor.
        calls = self._decide_conductor(plan, ev)
        if ev.kind == sch.STOP_SHOW and calls:
            # Stop: a show start still waiting in the conductor's line is
            # not sent (review of #130, second pass, 4). No latch.
            gone = self._calls.drop(_ConductorCalls.STOP_DROPS,
                                    calls[0].seq)
            for c in gone:
                self._journal_line(
                    "system", f"Not sent to the show conductor, {c.label}: "
                              f"Stop was pressed first.",
                    action="conductor", outcome="not sent",
                    reason=f"{c.method} dropped by Stop")
        if any(c.method == "abort" for c in calls):
            # The latch is saved with the Abort itself, before the conductor
            # is asked, so a restart in between still knows (and only a
            # Reset clears it, see _after_reset).
            self.machine = replace(self.machine, abort_latched=True)
            self._marker_clear_pending = False
        self._record(out, now, plan, held, ev)
        if ev.kind == sch.RESUME and ev.actor == "operator" and \
                ev.detail != sch.RESET_DETAIL and \
                not out.refused and before is not None and \
                before.state == sch.HOLD:
            # Reset's end of a Hold is not the deliberate Resume: it starts
            # nothing, so it speaks for no show (review of #144, F1).
            # Only a Resume of a schedule Hold (the operator's or the
            # engine's) speaks for the next show; a Resume of a paused,
            # running show says nothing about the one after it (review of
            # PR #133).
            self._note_resumed_at_showtime(now)
        if self.dry_run and self.machine.state == sch.CLOSING:
            # Nothing to wait for: nothing was faded.
            out2 = sch.step(self.machine,
                            sch.Event(sch.CLOSING_DONE, "system"), now)
            self.machine = out2.machine
            self._record(out2, now)
        # A Stop's conductor call never waits on the disk (Jeff's rule;
        # cold review of #191 round 2, item 4): it is queued before
        # tonight's save. It only takes the rig dark, so nothing a restart
        # could repeat depends on the save landing first.
        early = [c for c in calls if c.method == "stop"]
        if early:
            self._queue_conductor(plan, ev, early)
            calls = [c for c in calls if c not in early]
        if self.machine is not before:
            self._save_tonight()
        # Only now, saved and journaled, is the conductor asked for
        # anything else: a conductor that raises or hangs can no longer
        # lose the save or the "Show N started" line, which is what keeps
        # a restart inside the grace from starting the same show twice.
        if calls:
            self._queue_conductor(plan, ev, calls)
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
        """The night `now` belongs to: the local date from the nightly
        reset on, the day before until then (sch.night_of, Jeff,
        2026-10-03)."""
        return sch.night_of(now, self.rule.tz)

    # How many days back a start looks for a night left open (a delayed
    # show waiting, or a show running) to close it, see _open_night_before.
    OPEN_NIGHT_LOOK_BACK = 7

    def _ensure_night(self, now):
        """The night the scheduler is on, loaded or moved on at the
        nightly reset. A night runs until sch.NIGHT_RESET (2 AM local) on
        the next calendar day, always (Jeff, 2026-10-03): until then a
        delayed show waits for Start now or Close for the night (Jeff,
        2026-10-01); at it the night closes and a show still delayed is
        MISSED, out loud (_still_open). Only a show running or paused at
        the reset keeps its night open, until it ends.

        On start it first looks for a night the last run left open
        (_open_night_before): it is past its reset by then, so it is picked
        up only to be closed out loud, a show that was running cut (the rig
        dark) and a delayed show MISSED, rather than left behind without a
        word."""
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
            # goes through the same nightly reset rule as a live run, which
            # closes it.
            old = self._boot_latch(old)
            self.machine = replace(old, operators=self.operators)
            self._apply(sch.Event(sch.BOOT_DONE, "system"), now)
        if self.machine.date == d:
            return True
        # A new day. Settle yesterday first: a show still on its list
        # (the machine was asleep across it) is marked MISSED in the
        # journal rather than dropped without a word. This tick never
        # starts a show, so it needs no alert gate: a night cannot run past
        # midnight (schedule.parse_rule refuses it) and the reset is at
        # NIGHT_RESET (2 AM), so every slot on yesterday's list is long
        # past by now; BOOT_DONE below starts nothing either, and the next
        # TICK goes through the gate (review of PR #133, 7 and 8).
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
        # at the nightly reset, with or without a delayed show waiting.
        if self.machine.state == sch.HOLD:
            self.hold_epoch += 1
        # An Abort nobody has Reset outlives its night, exactly as the
        # conductor's own latch does in a run that never restarts.
        latched = self.machine.abort_latched
        # A Stop's Hold nobody has Resumed outlives it too: _stop_carry,
        # enforced on every tick (_keep_stop_hold).
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

    STOP_HOLD_CARRIED = ("kept the Hold of a Stop pressed on the night of "
                         "{night}, which nobody has Resumed")
    STOP_HOLD_CARRIED_EARLIER = ("kept the Hold of a Stop pressed earlier, "
                                 "which nobody has Resumed")
    STOP_HOLD_FILE = "ltcplay_stop_hold.json"

    # -- a Stop's Hold, kept until Resume (Jeff, 2026-10-30) ----------------
    # Every Stop sets it, whatever the scheduler was doing (a show live,
    # between shows, the last show of the night, closing, a night with no
    # shows; cold review of #191 round 2, blocker 1). While it is set, any
    # state a show could start from is put on Hold at the next tick, on
    # this night and every later one, and after an engine restart (it is
    # kept in STOP_HOLD_FILE next to tonight's file, as the Abort latch
    # is). Only an operator's Resume clears it (or Resume pressed when
    # nothing is held, which just clears it). A Start now from it starts
    # that one show; the Hold comes back once the show ends.

    def _stop_hold_path(self):
        d = self.state_dir
        return os.path.join(d, self.STOP_HOLD_FILE) if d else None

    def _stop_carry_load(self):
        """The marker file, read once at start. A file that cannot be
        read counts as set: to be safe, nothing starts until Resume."""
        path = self._stop_hold_path()
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            if not isinstance(doc, dict):
                raise ValueError("not an object")
            return {"night": str(doc.get("night") or ""),
                    "who": str(doc.get("who") or "")}
        except Exception:                        # noqa: BLE001
            return {"night": "", "who": "", "unreadable": True}

    def _stop_carry_set(self, who, screen):
        """Every Stop (lock held). Kept in memory at once; the file is
        written after the conductor has the Stop (never before it)."""
        m = self.machine
        self._stop_carry = {"night": str(m.date) if m is not None else "",
                            "who": str(who or ""),
                            "screen": str(screen or "")}
        self._stop_carry_dirty = True

    STOP_HOLD_UNSAVED = ("The Stop's Hold could not be saved to {path} "
                         "({why}). It stands while ltcplay runs, but would "
                         "be lost if ltcplay restarted now: it is tried "
                         "again every tick until it is saved.")
    STOP_HOLD_UNREMOVED = ("The Stop's Hold was released, but its file "
                           "{path} could not be removed ({why}). It is "
                           "tried again every tick; until then a restart "
                           "would come back held.")

    def _stop_carry_write(self):
        """Write (or remove) the marker file if it changed (lock held).
        Called after a Stop or Resume and again on every tick until it has
        worked (round 3 of the cold review, M1): a failure leaves it to
        do, raises the fault flag once with a sentence (shown on every
        screen), and the in-memory Hold stands meanwhile. Never raises."""
        if not getattr(self, "_stop_carry_dirty", False):
            return
        path = self._stop_hold_path()
        if not path:
            self._stop_carry_dirty = False
            return
        try:
            if self._stop_carry is None:
                if os.path.exists(path):
                    os.remove(path)
            else:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self._stop_carry, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
        except Exception as e:                   # noqa: BLE001
            # Left dirty: the next tick tries again.
            if not self._stop_carry_failing:
                self._stop_carry_failing = True
                text = (self.STOP_HOLD_UNSAVED if self._stop_carry is not None
                        else self.STOP_HOLD_UNREMOVED).format(
                    path=path, why=f"{type(e).__name__}: {e}")
                if self.machine is not None:
                    self._apply(sch.Event(sch.FAULT_RAISED, "system",
                                          detail=text))
                else:
                    self._journal_line("system", text, action="stop hold",
                                       outcome="not saved", fault=True)
            return
        self._stop_carry_dirty = False
        if self._stop_carry_failing:
            self._stop_carry_failing = False
            self._journal_line(
                "system", f"The Stop's Hold file {path} is "
                          f"{'saved' if self._stop_carry is not None else 'removed'}"
                          f" now.", action="stop hold", outcome="saved")

    def _stop_carry_clear(self, who, screen, why):
        if self._stop_carry is None:
            return False
        self._stop_carry = None
        self._stop_carry_dirty = True
        self._journal_line(
            "system", f"{who or 'Someone'} {why} on the "
                      f"{screen or 'screen'}: the Stop's Hold is released. "
                      f"Shows start at their times again.",
            action="stop hold", outcome="released")
        return True

    def stop_held(self):
        """True while a Stop's Hold stands (set, not yet Resumed)."""
        m = self.machine
        return self._stop_carry is not None or bool(
            m is not None and m.state == sch.HOLD and m.stop_hold)

    def _keep_stop_hold(self, now):
        """Each tick, lock held, before the tick's own event: while a
        Stop's Hold stands, a state a show could start from goes on Hold
        (dark, marked as the Stop's) and an existing Hold is marked as the
        Stop's. Not while a Stop is still under way (its own step decides)."""
        m = self.machine
        if self._stop_carry is not None and \
                self._stop_carry.pop("unreadable", False):
            self._journal_line(
                "system", f"The Stop's Hold file {self._stop_hold_path()} "
                          f"is there but could not be read. To be safe it "
                          f"counts as a Stop's Hold: no show starts by "
                          f"itself until someone presses Resume.",
                action="stop hold", outcome="unreadable", fault=True)
        self._stop_carry_write()
        if self._stop_carry is None or m is None or self.halting():
            return
        if m.state == sch.HOLD:
            if not m.stop_hold:
                self.machine = replace(m, stop_hold=True)
                self._save_tonight()
            return
        if m.state not in self.SHOWTIME_FROM or not m.waiting():
            # Nothing left tonight: the tick closes the night as it would
            # (cold review of #191 round 3, F2); the Stop's Hold stands
            # for the next state a show could start from.
            return
        night = self._stop_carry.get("night")
        ev = sch.Event(
            sch.HOLD_ON, "system", who="LTC Player", screen="engine",
            detail=(self.STOP_HOLD_CARRIED.format(night=night) if night
                    else self.STOP_HOLD_CARRIED_EARLIER))
        out = self._apply(ev, now)
        if not out.refused and self.conductor is not None and \
                getattr(self.conductor, "_look", None) not in (
                    "STOPPED_DARK", "ABORTED"):
            # The Hold is back (after a Start now's show, or on a later
            # night): the look goes dark again, as at the Stop (round 3 of
            # the cold review, M2). The plain dark of a stopped show: no
            # disarm, so nothing armed since, and the fuel pump, is touched
            # (a Hold never ends the pump).
            calls = self._decide_conductor(
                [("Dark again for the Stop's Hold", "show_stopped",
                  frozenset())], ev)
            self._journal_line(
                "system", "Sent to the show conductor, dark again for the "
                          "Stop's Hold. Its own line says what it did.",
                action="conductor", outcome="sent",
                reason="show_stopped asked of the show conductor")
            for c in calls:
                self._calls.put(c)

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

    def _still_open(self, now, d):
        """None while the night on the machine (a date other than `d`)
        must stay open; otherwise the words for its summary, once it may
        be set aside.

        A running or paused show is never set aside: it is not cut by the
        reset, and the night closes once it has ended. Otherwise the night
        closes at the nightly reset (Jeff, 2026-10-03): a delayed show
        still waiting is MISSED, with a fault line naming it and saying it
        was still waiting at the reset, and the night is set aside. Before
        its reset (only a clock set back gets here) it stays."""
        m = self.machine
        if m.state in (sch.SHOW, sch.PAUSED):
            return None
        words = sch.reset_words()
        if m.delayed() is None:
            return (f"written at the {words} nightly reset; the night was "
                    f"never closed")
        if now < sch.night_reset(m.date, m.tz):
            return None
        n = m.delayed().n
        out = sch.close_at_reset(m, now)
        self.machine = out.machine
        self._record(out, now)
        self._save_tonight()
        return (f"closed at the {words} nightly reset; the delayed show {n} "
                f"never started")

    def _open_night_before(self, d, now):
        """The most recent night saved before `d`, picked up again if it
        was left open (a delayed show waiting, or a show running or paused),
        or None. Every night before `d` is past its nightly reset, so it is
        picked up only to be closed by _still_open's rule, out loud: before
        2026-10-03 (the next-night-preshow rule) it could be picked up and
        run days later. Journaled. An unreadable file is left where it is
        and said out loud; tonight's list then starts as it always did."""
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
                "system", f"ltcplay started on the night of {d}, and the "
                f"night of {y} was left open when it stopped: "
                f"{'; '.join(waiting)}. That night ended at the "
                f"{sch.reset_words()} nightly reset on "
                f"{y + timedelta(days=1)}, so it is picked up only to close "
                f"it: nothing from it will run.",
                action="load tonight", outcome="closing open night")
            return m
        return None

    def tick(self):
        with self._locked():
            now = self.clock()
            self._watch_clock(now)
            self._watch_conductor()
            if not self._ensure_night(now):
                return None
            self._keep_stop_hold(now)
            self._keep_latch_on_disk()
            m = self.machine
            # Dry run: nothing was started, so the show "ends" when it would
            # have, which a pause moves later. A paused show never ends.
            if self.dry_run and m.state == sch.SHOW and \
                    now >= m.expected_end(now):
                self._apply(sch.Event(sch.SHOW_ENDED, "system",
                                      detail="dry run, nothing was started",
                                      show=m.running), now)
            ev = self._tick_event()
            self._preroll_lookahead(now)
            self._preroll_before_gate(now, ev)
            self._hold_at_showtime_for_alerts(now, ev)
            if self._halt_defers(now, ev):
                pass
            elif not self._preroll_defers(now, ev):
                self._apply(ev, now)
            self._sync_latch_marker()
            m = self.machine
        self._after_tick()
        return m

    def _halt_defers(self, now, ev):
        """True while a Stop is under way (halting) and this tick would
        start a show (Jeff, 2026-10-30; cold review of #191, A-F1): the
        tick is left for after the Stop, which holds the schedule, so a
        show coming due at the press is never started (and never started
        without its rig check: the pre-roll does not apply while halting).
        Lock held. Never raises."""
        m = self.machine
        if not self.halting() or m is None or \
                m.state not in self.SHOWTIME_FROM:
            return False
        try:
            return bool(self._starts(sch.step(m, ev, now)))
        except Exception:                        # noqa: BLE001
            return True

    def halt_schedule(self, who, screen, detail=""):
        """A Stop's step at the scheduler (Remote.halt; Jeff, 2026-10-30),
        decided and applied in ONE locked step with NO tick before it
        (cold review of #191, A-F1: a tick first could start the show
        coming due, and the Hold would then only pause it). Every Stop
        sets the Stop's Hold (_stop_carry), whatever the state, so no show
        starts by itself on this or a later night until Resume (round 2,
        blocker 1). Then:

          SHOW, PAUSED  STOP_SHOW: the show ends through the conductor's
                        Stop (Abort's sequence), and the schedule holds;
          IDLE, STANDBY the schedule's Hold, said as the Stop, dark: the
                        conductor's Stop takes the idle or intermission
                        look down too (round 2, item 5);
          HOLD          already held: the Hold is marked as a Stop's;
          otherwise     (closing, the night over, booting) nothing to hold
                        now; the Stop's Hold waits for the next state a
                        show could start from.

        {"ok", "kind" ("stop", "hold", "held" or "carried"), "text",
        "state"}. Never raises for who or where: Stop is never refused."""
        who = str(who or "").strip() or "An unnamed operator"
        screen = str(screen or "").strip() or "screen"
        screen = {x.lower(): x for x in self.screens}.get(screen.lower(),
                                                           screen)
        with self._locked():
            self._stop_carry_set(who, screen)
            try:
                return self._halt_schedule_locked(who, screen, detail)
            finally:
                # After the conductor has the Stop, never before it.
                self._stop_carry_write()

    STOP_HELD_LATER = ("Nothing is held right now (the night is closing, "
                       "over, or has no shows), but the Stop's Hold stands: "
                       "no show starts by itself, tonight or on a later "
                       "night, until someone presses Resume.")

    def _halt_schedule_locked(self, who, screen, detail):
        m = self.machine
        if m is None:
            return {"ok": True, "kind": "carried", "state": None,
                    "text": self.STOP_HELD_LATER}
        st = m.state
        if st == sch.HOLD:
            if not m.stop_hold:
                self.machine = replace(m, stop_hold=True)
                self._save_tonight()
            return {"ok": True, "kind": "held", "state": st,
                    "text": "The schedule was already on Hold; it stays on "
                            "Hold until Resume."}
        if st in (sch.SHOW, sch.PAUSED):
            ev = sch.Event(sch.STOP_SHOW, "operator", who=who,
                           screen=screen, confirmed=True, surface=True)
            kind = "stop"
        elif st in self.SHOWTIME_FROM and not m.waiting():
            # No show left tonight (the last one just ended): the night
            # closes as it would (round 3, F2); the Stop's Hold stands for
            # the next night with shows.
            return {"ok": True, "kind": "carried", "state": st,
                    "text": self.STOP_HELD_LATER}
        elif st in self.SHOWTIME_FROM:
            ev = sch.Event(sch.HOLD_ON, "operator", who=who, screen=screen,
                           surface=True,
                           detail=detail or f"{STOP_HOLD_DETAIL} on the "
                                            f"{screen}, so the schedule is "
                                            f"on Hold")
            kind = "hold"
        else:
            return {"ok": True, "kind": "carried", "state": st,
                    "text": self.STOP_HELD_LATER}
        self._press_stamp = self.stamp_press()
        try:
            out = self._apply(ev)
        finally:
            self._press_stamp = None
        if out.refused:
            return {"ok": False, "kind": kind, "state": self.machine.state,
                    "text": out.refused}
        # (Between shows the look goes dark by the conductor's Stop, which
        # Remote.halt asks for at the press, never behind this lock or
        # tonight's save: cold review of #191 round 3, F1 and F3.)
        text = " ".join(self._reworded(le).text for le in out.log if le.text)
        return {"ok": True, "kind": kind, "state": self.machine.state,
                "text": text or "Done."}

    def _alert_handled(self, key, ids):
        """True when the open alerts are all among a set this showtime was
        already held for, or Resumed with: an Acknowledge that leaves a
        subset open never re-holds it (final review of PR #133)."""
        ids = frozenset(ids)
        return any(ids <= rec for rec in self._alert_held.get(key, ()))

    def _note_resumed_at_showtime(self, now):
        """An operator's Resume (from any Hold, theirs or the engine's)
        while alerts are open is the operator's choice (Jeff): the show the
        next tick would start, AND the next pending show of the night
        (a Resume pressed before showtime, or a guard running out inside
        the grace window), are recorded as handled for those alerts, so
        the gate never holds them again for them. Never raises."""
        gate = self.showtime_gate
        m = self.machine
        if gate is None or m is None:
            return
        try:
            got = gate()
            if not got:
                return
            probe = sch.step(m, self._tick_event(), now)
            shows = [e.show for e in probe.effects
                     if e.kind == sch.START_SHOW][:1]
            if not shows and m.delayed() is None:
                # Nothing starts now and no delayed show waits for Start
                # now: the Resume is for the next show (one pressed before
                # showtime, or a guard running out inside the grace). With
                # a delayed show waiting, the Resume is for THAT one, and
                # never covers a show beyond it (review of PR #133).
                pending = m.pending()
                if pending:
                    shows = [pending[0].n]
        except Exception:                        # noqa: BLE001
            return
        for n in set(shows):
            self._alert_held.setdefault((str(m.date), n), []).append(
                frozenset(got[1]))

    # The states a show can START from on a tick; a running (SHOW) or
    # paused show, or one already held, is never touched here.
    SHOWTIME_FROM = (sch.IDLE, sch.STANDBY)

    def _hold_at_showtime_for_alerts(self, now, ev=None):
        """With the lock held, just before the tick: if this tick would
        start a show and an alert is open (showtime_gate), the schedule
        Holds instead, as the engine's own system action, and the show
        waits for an operator's Resume (Jeff, 2026-10-26). sch.step is
        pure, so the tick is tried on the side first. Never mid-show:
        only from IDLE or STANDBY. Never raises."""
        gate = self.showtime_gate
        m = self.machine
        if gate is None or m is None or m.state not in self.SHOWTIME_FROM:
            return
        try:
            probe = sch.step(m, ev or sch.Event(sch.TICK, "scheduler"), now)
            shows = [e.show for e in probe.effects
                     if e.kind == sch.START_SHOW]
            if not shows:
                return
            got = gate()
        except Exception:                        # noqa: BLE001
            return
        if not got:
            return
        why, ids = got
        # Once this showtime was held for these alerts, or an operator
        # Resumed it with these alerts open, it is never held again for
        # them (review of PR #133): the Resume was the choice. A NEW alert
        # at the same showtime holds again.
        key = (str(m.date), shows[0])
        if self._alert_handled(key, ids):
            return
        out = self._apply(sch.Event(
            sch.HOLD_ON, "system", who="LTC Player", screen="engine",
            detail=f"held the schedule at showtime because an alert is "
                   f"open ({why})"), now)
        if out.refused or self.machine.state != sch.HOLD:
            return
        self._alert_held.setdefault(key, []).append(frozenset(ids))
        hook = self.on_alert_hold
        if hook is not None:
            epoch = self.hold_epoch
            self._pending_hooks.append(lambda: hook(epoch, why))

    # -- the pre-roll (preroll.py, Jeff 2026-10-28) -------------------------
    def _tick_event(self):
        """The scheduler's TICK, with the late allowance (late_ok_s) for
        the next show when its pre-roll could only run at its time."""
        ev = sch.Event(sch.TICK, "scheduler")
        m = self.machine
        if self._preroll_late and m is not None:
            nxt = m.next_slot()
            if nxt is not None:
                late = self._preroll_late.get((str(m.date), nxt.n))
                if late:
                    ev = replace(ev, late_show=nxt.n, late_ok_s=late)
        return ev

    def _preroll_applies(self):
        """A pre-roll is attached, the scheduler performs, Run is pressed,
        no Abort stands and no Stop is under way. Programming does not
        skip it (review of #162): only Programming's own Play from, which
        never comes here, runs unchecked. Never raises."""
        pr = self.preroll
        if pr is None or self.dry_run or self.performer is None:
            return False
        return not (self.halting() or self._aborted() or
                    not self._run_pressed())

    def _preroll_scheduled(self):
        """The pre-roll before a show the scheduler starts by itself: only
        from IDLE or STANDBY, and only when it starts shows by itself
        (auto_start). Lock held."""
        m = self.machine
        if m is None or m.state not in self.SHOWTIME_FROM:
            return False
        cfg = getattr(self.performer, "cfg", None)
        if getattr(cfg, "auto_start", "when_run_pressed") == "off":
            return False
        return self._preroll_applies()

    @staticmethod
    def _starts(probe):
        shows = [e.show for e in probe.effects if e.kind == sch.START_SHOW]
        return shows[0] if shows else 0

    def _preroll_kick(self, n):
        """Queue the pre-roll for show `n` (lock held): it runs on a
        thread of its own once the lock is free, never under it."""
        m, pr = self.machine, self.preroll
        key = (str(m.date), n)
        slot = m.slot(n)
        label = f"show {n}" + (f" ({m.hm(slot.start)})" if slot else "")
        if pr.claim(key, label):
            self._pending_hooks.append(
                lambda: pr.run(key, label, show=n))

    def _preroll_lookahead(self, now):
        """With the lock held, before the tick: a show the scheduler will
        start within the pre-roll window has its pre-roll begun now, so a
        healthy rig still starts it at its time. Never raises."""
        try:
            if not self._preroll_scheduled() or self.preroll.skip():
                return
            m = self.machine
            nxt = m.next_slot()
            if nxt is None or (nxt.start - now).total_seconds() > \
                    self.preroll.window_s:
                return
            # Would the scheduler start it at its time (no guard, nothing
            # in the way)? Asked of the pure machine at that instant.
            n = self._starts(sch.step(m, sch.Event(sch.TICK, "scheduler"),
                                      max(now, nxt.start)))
            if n:
                self._preroll_kick(n)
        except Exception:                        # noqa: BLE001
            return

    def _preroll_before_gate(self, now, ev):
        """At showtime, a failed pre-roll's alert is open, so the alert
        hold (showtime_gate) holds the show: raised again if someone
        acknowledged it before the time. Never raises."""
        try:
            if not self._preroll_scheduled():
                return
            n = self._starts(sch.step(self.machine, ev, now))
            if not n:
                return
            pr = self.preroll
            key = (str(self.machine.date), n)
            if pr.verdict(key) == "failed" and not pr.alert_open():
                pr.raise_alert(pr.failure_text(key))
        except Exception:                        # noqa: BLE001
            return

    def _preroll_defers(self, now, ev):
        """True when this tick must NOT start the show that is due: its
        pre-roll is still running, never ran (it is started now: the show
        starts late, said once), or failed. A failed check is never
        started by the scheduler, not even after an operator's Resume:
        only Start now (which checks again) or Continue anyway (failed
        outputs left off) goes on (Jeff, 2026-10-28). A passed pre-roll
        starts it.
        Lock held. Never raises (a fault here defers nothing: the start
        then goes as before, to the performer's own checks)."""
        try:
            if not self._preroll_scheduled():
                return False
            m, pr = self.machine, self.preroll
            n = self._starts(sch.step(m, ev, now))
            if not n:
                return False
            key = (str(m.date), n)
            if pr.skip():
                # A Programming Run: the scheduler never starts a show
                # unchecked or over a rehearsal (review of #162).
                if key not in self._preroll_late:
                    self._preroll_late[key] = 0
                    self._journal_line(
                        "system", f"Show {n} is not started: a Programming "
                                  f"Run is in force. Press Stop, choose Show, "
                                  f"then Run, and Start now.",
                        action="pre-roll", outcome="programming", show=n)
                return True
            v = pr.verdict(key)
            if v == "passed":
                return False
            if v in ("failed", "cleared"):
                # Failed, or failed and then cleared by a later pass (Jeff's
                # field report, 2026-10-29): never started unattended, and
                # never journaled as a check that is late. Resume (a cleared
                # show is then checked again) or Start now.
                return True
            if key not in self._preroll_late:
                self._preroll_late[key] = pr.late_ok_s()
                self._journal_line(
                    "system", f"The rig check (pre-roll) for show {n} could "
                              f"not finish before its time, so show {n} "
                              f"starts late, as soon as the rig is checked "
                              f"(at most {pr.late_ok_s()} s late). Checks "
                              f"are never skipped.",
                    action="pre-roll", outcome="late", show=n)
            if v is None:
                self._preroll_kick(n)
            return True
        except Exception as e:                   # noqa: BLE001
            # Fail closed (review of #162): a rig check that cannot even be
            # asked never lets the show start. Its alert holds it.
            self._preroll_broken(f"{type(e).__name__}: {e}")
            return True

    def _preroll_broken(self, why):
        """The rig check itself could not run: journaled and alerted once
        per reason (the alert hold then holds the show), and once more
        after a passing check cleared it (review 1 of #176: still broken,
        still said). Never raises."""
        once = (why, getattr(self.preroll, "clears", 0))
        if getattr(self, "_preroll_broke", None) == once:
            return
        self._preroll_broke = once
        try:
            # Numbered, so a check that began before it never clears it.
            self.preroll.mark_failure()
        except Exception:                        # noqa: BLE001
            pass
        text = (f"The rig check before the show could not run ({why}), so "
                f"the show does not start by itself. Press Start now to try "
                f"again; the journal has the details.")
        try:
            self._journal_line("system", text, action="pre-roll",
                               outcome="error", fault=True)
            if self.alert_sink is not None:
                self.alert_sink("preroll", text)
        except Exception:                        # noqa: BLE001
            pass

    def _preroll_release(self, who, screen):
        """Resume was pressed (lock held): a show whose rig check failed
        and then passed on a later check (preroll.PreRoll._clear_failure)
        is checked again now and starts when that passes, at most
        late_ok_s late; it is never started on the old pass. Never
        raises."""
        pr = self.preroll
        if pr is None:
            return
        try:
            keys = pr.release_cleared()
            m = self.machine
            night = str(m.date) if m is not None else None
            for key in keys:
                # A scheduled show's key is (night, show); Start now's and
                # a Pre-check's are not shows (review A of #176).
                if not isinstance(key, tuple) or len(key) != 2 or \
                        key[0] != night or not isinstance(key[1], int):
                    continue
                self._preroll_late[key] = pr.late_ok_s()
                d = m.delayed() if m is not None else None
                if d is not None and d.n == key[1]:
                    # Its time has gone by: nothing checks it by itself
                    # (review A round 2 of #176).
                    then = (f"Its time has passed, so show {key[1]} waits "
                            f"for Start now, which checks the rig first.")
                else:
                    then = (f"Show {key[1]} is checked again at its next "
                            f"tick and starts only if that check passes (at "
                            f"most {pr.late_ok_s()} s late).")
                self._journal_line(
                    "system", f"{who or 'Someone'} pressed Resume after the "
                              f"rig check for show {key[1]} failed and then "
                              f"passed. {then}",
                    action="pre-roll", outcome="released", show=key[1])
        except Exception:                        # noqa: BLE001
            return

    def _preroll_for_start_now(self, who, screen, gen):
        """Start now: the rig is checked first, on the pressing thread,
        outside this service's lock (at most the pre-roll's budget). A
        Stop, Hold or Abort meanwhile cancels it. Returns a refusal or
        None. A failure is never started past here: Continue anyway (a
        signed-in hold, failed outputs left off) is the only way on."""
        if not self._preroll_applies():
            return None
        pr = self.preroll
        self._preroll_seq += 1
        who_ = who or "someone"
        r = pr.run(("start now", self._preroll_seq), f"Start now by {who_}",
                   by=f"Start now by {who_}",
                   cancelled=lambda: (self._start_cancel[0] != gen or
                                      self.halting()))
        if r["state"] == "passed":
            return None
        if r["state"] == "cancelled":
            said = self._start_cancel[1] or "it was cancelled"
            sentence = (f"Start now was refused: {said} while the rig was "
                        f"being checked. Nothing was started.")
            self.journal_press(who, screen, "start-now", "refused",
                               f"{who_}'s Start now was refused. {sentence}")
            return {"ok": False, "text": sentence, "cancelled": True}
        bad = "; ".join(f"{i['name']}: {i['text']}" for i in r["items"]
                        if i["required"] and not i["ok"])
        sentence = (f"Start now was refused: the rig check failed ({bad}). "
                    f"Nothing was started. Fix it and press Start now "
                    f"again, or hold Continue anyway to start without what "
                    f"failed.")
        self.journal_press(who, screen, "start-now", "refused",
                           f"{who_}'s Start now was refused. {sentence}",
                           fault=True)
        return {"ok": False, "text": sentence, "preroll": True}

    def continue_anyway(self, who, screen):
        """Continue anyway (Jeff, 2026-10-28): after a failed rig check, a
        signed-in person with the run permission held the button (the
        remote checks both). Starts the show NOW (late, and said so), with
        every output whose check failed left off for this show only
        (preroll.outputs_off, fire_ice.ShowRunner._outputs_off): it never
        arms, enables or lights anything. Never past an Abort, a Stop
        under way or a live show; Resume first when the schedule is held.
        Nothing is played on the PA (Jeff, 2026-10-30). Journaled with
        who, when and the failed checks. Returns {"ok", "text"}."""
        pr = self.preroll
        r = pr.last_failed() if pr is not None else None
        who_ = who or "someone"
        if pr is not None and pr.continued is not None:
            # Once per failed check (review of #162, B2): never replayed by
            # a stale page or a second POST for a later start.
            r = None
        if r is None or self.performer is None or self.dry_run:
            return {"ok": False, "text": "Continue anyway was refused: there "
                                         "is no failed rig check to continue "
                                         "past. Nothing was changed."}
        m = self.machine
        why = ""
        if m is None:
            why = "there is no schedule loaded"
        elif m.state in (sch.SHOW, sch.PAUSED):
            why = "a show is live"
        elif self._aborted():
            why = "the show is aborted; press Reset first"
        elif self.halting():
            why = self.HALTING
        elif not self._run_pressed():
            why = "Run has not been pressed"
        if why:
            return {"ok": False, "text": f"Continue anyway was refused: "
                                         f"{why}. Nothing was changed."}
        from .preroll import outputs_off
        off = outputs_off(r["items"])
        bad = [i for i in r["items"] if i["required"] and not i["ok"]]
        failed = "; ".join(f"{i['name']}: {i['text']}" for i in bad)
        left = (", ".join(sorted(off)) + " left off for this show"
                if off else "nothing left off")
        if any(i["name"] == "MadMapper output" for i in bad):
            # Not yet bench-proven, so it switches nothing off (review A
            # round 2 of #176).
            left += (". The MadMapper output check failed and stays "
                     "failed; nothing is switched off for it")
        elif not off:
            left += " (no output's check failed)"
        self.journal_press(
            who, screen, "continue anyway", "chosen",
            f"{who_} chose Continue anyway on the {screen or 'screen'} after "
            f"the rig check failed ({failed}). The show starts now, late, "
            f"with {left}.", fault=True)
        self.performer.continue_off = (off, who_)
        if m.state == sch.HOLD and not self.stop_held():
            # An operator's own Hold is resumed first (a Stop's Hold is
            # not: Start now starts this one show from it, and the Hold
            # comes back once the show ends; only Resume ends it. Cold
            # review of #191 round 2, item 8).
            got = self.operator_press("resume", who, screen, surface=True)
            if not got.get("ok"):
                self.performer.continue_off = None
                return got
        got = self.operator_press("start-now", who, screen, surface=True,
                                  preroll=False)
        if self.machine is not None and self.machine.state == sch.SHOW:
            pr.consume(r)
            pr.continued = {"label": r.get("label", ""), "by": who_,
                            "at": self.clock().isoformat(timespec="seconds"),
                            "off": off, "failed": failed}
            return {"ok": True, "text": f"Continue anyway: the show starts "
                                        f"now, late. Left off for this show: "
                                        f"{', '.join(sorted(off)) or 'nothing'}."}
        self.performer.continue_off = None
        return got

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
            look = getattr(self.conductor, "_look", None)
            return {"attached": self.conductor is not None,
                    "aborted": self._aborted(),
                    "trouble": t["text"] if t else None,
                    # The rig is up (the conductor's look), whatever the
                    # scheduler's state says: the screens then show Stop.
                    "lit": look == "PLAYING",
                    # A Stop's Hold stands until Resume.
                    "stop_held": self.stop_held()}

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
            # After the tick, outside the lock: the alert thread's work
            # (remote.Remote._alert_tick: the booth re-sound). A hook that
            # raises never stops the ticks.
            for hook in list(self.tick_hooks):
                try:
                    hook()
                except Exception:                # noqa: BLE001
                    pass
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
                   "dry_run": self.dry_run,
                   "note": (DRY_RUN_NOTE if self.dry_run
                            else PERFORMING_NOTE),
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
        # What Start now would be refused with now (the rack screen's
        # Loading state and its reason), read off this service's lock.
        out["start_blocked"] = self.start_blocked()
        out["start_waiting"] = self.start_waiting()
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

    # What a performer may report back (schedule.py's "Contract for PR 3",
    # items 4 to 8). Operator presses never come this way.
    REPORTS = frozenset((sch.SHOW_CONFIRMED, sch.SHOW_ENDED,
                         sch.SHOW_FAILED, sch.FAULT_RAISED,
                         sch.CLOSING_DONE))

    def report(self, kind, detail="", show=0, nothing_sent=False):
        """A performer's report (fire_ice.ShowRunner): applied as a
        "system" event exactly like the dry run's own made-up SHOW_ENDED
        and CLOSING_DONE, journaled, refused with a sentence when the state
        does not take it. Only while not dry_run: in a dry run nothing was
        started, so nothing can report on it, and nothing does."""
        if kind not in self.REPORTS:
            raise ValueError(f"{kind} is not something a performer reports.")
        with self._locked():
            if self.dry_run or self.machine is None:
                return None
            ev = sch.Event(kind, "system", detail=detail, show=show,
                           nothing_sent=(nothing_sent is True and
                                         kind == "SHOW_FAILED"))
            self._apply(ev)
            return None

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
            if self.machine.state == sch.SHOW:
                # A show is running, possibly one this very tick just
                # started (an announcement pressed at showtime): it is
                # never paused by an announcement (Jeff, 2026-10-26). No
                # Hold; the caller reads the state again and plays over
                # the show (announce.AnnounceService.play).
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
                "dry_run": self.dry_run, "state_dir": self.state_dir,
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
            # The reason used to be "", which the journal refuses (a reason
            # is never blank), so this line was lost and only a "could not
            # be written" fault was journaled. Found by the remote's tests.
            text = (f"{self._who_text(screen)} chose {name} as the "
                    f"operator." if name else
                    f"{self._who_text(screen)} cleared the operator "
                    f"selection.")
            self._log(self.logbook.record, actor="operator", action="operator",
                      outcome="done", reason=text, text=text,
                      state=self._state_name(), night=self._night(),
                      who=name or "unnamed operator",
                      screen=screen or "unnamed screen")
        return self.operator_view()

    # The operator presses the remote page sends (ltcplay/remote.py), by its
    # route name. These are the engine's own operator events: the same
    # schedule.step every other press goes through, journaled by the engine
    # with who and which screen, the Abort latch saved before the conductor
    # is asked (see _apply). Reset is reset_conductor, under its own name.
    PRESSES = {"start-now": sch.START_NOW, "hold": sch.HOLD_ON,
               "resume": sch.RESUME, "abort": sch.ABORT,
               # The engine's own Stop during a show (web.py, the Stream
               # Deck's Stop held; Jeff, 2026-10-26): never a remote route.
               "stop-show": sch.STOP_SHOW,
               # The engine's own Stop between shows (Remote.halt; Jeff,
               # 2026-10-30: Stop also holds): the schedule's Hold, said
               # as the Stop it was. Never a remote route either.
               "stop-hold": sch.HOLD_ON}
    # The states in which a refused Hold or Resume found nothing to do:
    # nothing can start by itself (on hold, paused, the night over), or
    # nothing is held. BOOT is not among them: there the scheduler has not
    # worked out tonight yet, a real refusal.
    HOLD_ALREADY = frozenset((sch.HOLD, sch.PAUSED, sch.CLOSING, sch.OFF))
    RESUME_ALREADY = frozenset((sch.IDLE, sch.STANDBY, sch.SHOW,
                                sch.CLOSING, sch.OFF))

    def operator_press(self, what, who, screen, confirmed=False,
                       pressed=None, surface=False, preroll=True,
                       detail=""):
        """One operator press from a screen: Start now, Hold, Resume or
        Abort. `who` must be on the operator list and `screen` on the
        screen list; anything else is refused in the journal and raised as
        ValueError with the sentence. Abort needs confirmed=True (the
        engine refuses it otherwise, as it always has). Returns {"ok",
        "text"}: ok False with the engine's own refusal sentence.
        surface=True: the remote page's press, where `who` names the
        surface it came from (Jeff, 2026-10-07: no sign-in), so it is not
        looked up on the operator list; the screen still is."""
        kind = self.PRESSES.get(what)
        if kind is None:
            raise ValueError(f"{what!r} is not a press this program knows. "
                             f"Nothing was changed.")
        who = str(who or "").strip()
        screen = str(screen or "").strip()
        names = {n.lower(): n for n in self.operators}
        always = kind in sch.ALWAYS_TAKEN
        if always and who.lower() not in names:
            # Abort and Hold are never refused for who pressed them (PR #43
            # fix round 1): no operator chosen, or a name not on the list,
            # still stops the show; the journal says which (schedule.step).
            names[who.lower()] = who
        if surface and who:
            names[who.lower()] = who
        if who.lower() not in names:
            sentence = (f"{who or 'Nobody'!r} is not on the operator list "
                        f"({', '.join(self.operators)}). Nothing was "
                        f"changed.")
            self.journal_press(who, screen, what, "refused",
                               f"{what} was refused. {sentence}")
            raise ValueError(sentence)
        who = names[who.lower()]
        if always:
            screen = {x.lower(): x for x in self.screens}.get(
                screen.lower(), screen)
        else:
            screen = self._check_screen(screen, who, what, what)
        if kind in (sch.HOLD_ON, sch.ABORT, sch.STOP_SHOW):
            # A Start now waiting for the show audio never outlives these.
            self.cancel_start_wait(f"{what} was pressed")
        gen = None
        if kind == sch.RESUME and self.halting():
            # A Resume never lands in the middle of a Stop (Jeff,
            # 2026-10-30: Stop holds the schedule, and has every guarantee
            # an Abort had; this is the Stop's analogue of a Reset pressed
            # before an Abort landed).
            sentence = (f"Resume was refused: {self.HALTING}. Nothing was "
                        f"resumed.")
            self.journal_press(who, screen, "resume", "refused",
                               f"{who}'s Resume was refused. {sentence}")
            return {"ok": False, "text": sentence, "halting": True}
        if kind == sch.START_NOW and self.halting():
            sentence = (f"Start now was refused: {self.HALTING}. Nothing "
                        f"was started.")
            self.journal_press(who, screen, "start-now", "refused",
                               f"{who}'s Start now was refused. {sentence}")
            return {"ok": False, "text": sentence, "halting": True}
        if kind == sch.START_NOW:
            # Outside this service's lock: the performer's check reads the
            # show's clock (start_blocked), and it may wait for the audio.
            refused, gen = self._start_now_refusal(who, screen, preroll)
            if refused is not None:
                return refused
        with self._locked():
            # The tick first: a Hold it takes (the engine's own at
            # showtime for an open alert) cancels the start below (C1).
            self.tick()
            if gen is not None and (self._start_cancel[0] != gen or
                                    self.halting()):
                # A Stop, Hold or Abort landed after the wait ended and
                # before this: the start is not made.
                why = self._start_cancel[1] or "it was cancelled"
                sentence = (f"Start now was refused: {why} just before it "
                            f"started. Nothing was started.")
                self.journal_press(who, screen, "start-now", "refused",
                                   f"{who}'s Start now was refused. "
                                   f"{sentence}")
                return {"ok": False, "text": sentence, "cancelled": True}
            veto = ""
            if kind == sch.START_NOW and self.start_veto is not None:
                try:
                    veto = str(self.start_veto() or "")
                except Exception:                    # noqa: BLE001
                    veto = ""
            if veto:
                sentence = f"Start now was refused: {veto}"
                self.journal_press(who, screen, "start-now", "refused",
                                   f"{who}'s Start now was refused. "
                                   f"{sentence}")
                return {"ok": False, "text": sentence}
            if self.machine is None:
                sentence = (self.error or "There is no schedule loaded, so "
                            "there is no show to press anything on.")
                self.journal_press(who, screen, what, "refused",
                                   f"{who}'s {what} was refused. {sentence}")
                return {"ok": False, "text": sentence}
            if kind == sch.RESUME and self._stop_carry is not None and \
                    self.machine.state not in (sch.HOLD, sch.PAUSED):
                # Nothing is held right now (the night closing, over, or
                # with no shows), but a Stop's Hold stands: Resume
                # releases it, so later shows start at their times again.
                self._stop_carry_clear(who, screen, "pressed Resume")
                self._stop_carry_write()
                return {"ok": True, "text": "Resume: the Stop's Hold is "
                                            "released. Shows start at their "
                                            "times again."}
            self._press_stamp = (pressed if pressed is not None
                                 else self.stamp_press())
            before = self.machine.state
            try:
                out = self._apply(sch.Event(kind, "operator", who=who,
                                            screen=screen,
                                            confirmed=bool(confirmed),
                                            surface=bool(surface),
                                            detail=str(detail or "")))
                if kind == sch.RESUME and not out.refused:
                    if before == sch.HOLD:
                        # Only an operator's Resume ends a Stop's Hold.
                        self._stop_carry_clear(who, screen, "pressed Resume")
                        self._stop_carry_write()
                    self._preroll_release(who, screen)
            finally:
                self._press_stamp = None
        if out.refused:
            # nothing_to_do: what the press asks for is already so (a Hold
            # with the schedule on hold, a show paused or the night over;
            # a Resume with nothing held). Still refused and journaled as
            # before; the Stream Deck reads the flag so a press that found
            # the rig as asked is not drawn as ENGINE FAULT (2026-10-21).
            done = ((kind == sch.HOLD_ON and before in self.HOLD_ALREADY) or
                    (kind == sch.RESUME and before in self.RESUME_ALREADY))
            return {"ok": False, "text": out.refused,
                    "nothing_to_do": bool(done)}
        # The answer the page, the iPad and the deck see says what the
        # journal says (fix round 2, E: it said "nothing was disarmed"
        # while the conductor did disarm).
        text = " ".join(self._reworded(le).text for le in out.log if le.text)
        return {"ok": True, "text": text or "Done."}

    def start_blocked(self):
        """What would stop an operator's Start now before anything is
        decided, as {"text", "loading"}, or None: the performer's own check
        (fire_ice.ShowRunner.start_blocked: Run not pressed, the show audio
        still loading or not loaded, its interface missing). None with no
        performer or in a dry run, where nothing is started anyway. Takes
        no lock of this service. Never raises."""
        perf = self.performer
        fn = getattr(perf, "start_blocked", None)
        if fn is None or self.dry_run:
            return None
        try:
            got = fn()
        except Exception:                        # noqa: BLE001
            return None
        if not isinstance(got, dict) or not got.get("text"):
            return None
        return {"text": str(got["text"]), "loading": got.get("loading") is
                True}

    # Start now while the show audio loads waits for it, this long at most
    # (review of #144, F2), checking every START_WAIT_POLL_S. A Stop, Hold
    # or Abort pressed meanwhile ends the wait: nothing starts.
    START_WAIT_S = 20.0
    START_WAIT_POLL_S = 0.1

    def cancel_start_wait(self, why):
        """End any Start now waiting for the show audio (a Stop, Hold or
        Abort was pressed): it is refused, saying `why`, and starts
        nothing. Any pre-roll under way stops too. Cheap, takes no lock,
        never raises."""
        self._start_cancel = (self._start_cancel[0] + 1, str(why or ""))
        pr = self.preroll
        if pr is not None:
            pr.cancel(why)
        perf = self.performer
        if perf is not None and getattr(perf, "continue_off", None):
            # A Continue anyway whose start a Stop, Hold or Abort cut off
            # never carries to a later start (review of #162).
            perf.continue_off = None

    def halt_begin(self):
        """A Stop's complete halt is under way (Remote.stop_press, web.py's
        /api/stop with "halt"): until halt_end, Start now is refused, and
        so is Run (re-review of #144, N2). Also ends any waiting Start
        now. Never raises."""
        with self._halt_count_lock:
            self._halts += 1
        self.cancel_start_wait("Stop was pressed")

    def halt_end(self):
        with self._halt_count_lock:
            self._halts = max(0, self._halts - 1)

    def halting(self):
        """True while a Stop's halt is under way (halt_begin)."""
        return self._halts > 0

    HALTING = ("a Stop is under way (the rig is being disarmed and faded "
               "to dark); wait until it has finished, then press it again")

    def start_waiting(self):
        """True while a Start now waits for the show audio to load."""
        return self._start_waiters > 0

    def _start_now_refusal(self, who, screen, preroll=True):
        """Start now that cannot work, said up front with the real reason
        and what to press (Jeff's field report, 2026-10-28: Start now with
        Run off was "refused" only by a FAULT slot and an alert that said
        "The show did not start"). While the show audio is still loading it
        waits for it instead (review of #144, F2), at most START_WAIT_S,
        and a Stop, Hold or Abort pressed meanwhile (cancel_start_wait)
        ends the wait with nothing started. Only while the scheduler would
        take a Start now at all; its own refusals (a show running, an Abort
        not Reset) keep their own sentences. A refusal is journaled.
        Returns (refusal or None, the cancel number the start must still
        match under the lock)."""
        gen = self._start_cancel[0]
        m = self.machine
        if m is None or m.state not in sch.ALLOWED[sch.START_NOW]:
            return None, gen
        blocked = self.start_blocked()
        if blocked is not None and blocked["loading"]:
            with self._locked():
                self._journal_line(
                    "system", f"Start now by {who or 'someone'} is "
                              f"waiting for the show audio to load (at most "
                              f"{self.START_WAIT_S:g} s); Stop, Hold or "
                              f"Abort cancels it.",
                    action="start now", outcome="waiting",
                    reason="show audio loading")
            self._start_waiters += 1
            try:
                end = _time.monotonic() + self.START_WAIT_S
                while True:
                    if self._start_cancel[0] != gen:
                        said = self._start_cancel[1] or "it was cancelled"
                        blocked = {"text": f"{said} while it waited for the "
                                           f"show audio", "loading": False,
                                   "cancelled": True}
                        break
                    blocked = self.start_blocked()
                    if blocked is None or not blocked["loading"]:
                        break
                    if _time.monotonic() >= end:
                        blocked = {"text": f"the show audio was still loading "
                                           f"after {self.START_WAIT_S:g} s; "
                                           f"check the show audio files and "
                                           f"the journal, then press Start "
                                           f"now again", "loading": True}
                        break
                    _time.sleep(self.START_WAIT_POLL_S)
            finally:
                self._start_waiters -= 1
        if blocked is None:
            if not preroll:
                # Continue anyway: the person has just seen the failed
                # check and chosen; it is not run again.
                return None, gen
            return self._preroll_for_start_now(who, screen, gen), gen
        why = blocked["text"].rstrip(".")
        sentence = (f"Start now was refused: {why}. Nothing was started and "
                    f"nothing was sent to the rig.")
        self.journal_press(who, screen, "start-now", "refused",
                           f"{who or 'Someone'}'s Start now was refused. "
                           f"{sentence}")
        return {"ok": False, "text": sentence,
                "loading": blocked["loading"],
                "cancelled": bool(blocked.get("cancelled"))}, gen

    def journal_press(self, who, screen, action, outcome, text,
                      fault=False):
        """One journal line for a press that is not a schedule event (the
        remote page's disarm-all, a sign-in), with who and which screen."""
        if fault:
            return self._log(self.logbook.fault, "operator", text,
                             action=action, outcome=outcome,
                             state=self._state_name(), night=self._night(),
                             who=who or "unnamed operator",
                             screen=screen or "unnamed screen")
        return self._log(self.logbook.record, actor="operator",
                         action=action, outcome=outcome, reason=text,
                         text=text, state=self._state_name(),
                         night=self._night(),
                         who=who or "unnamed operator",
                         screen=screen or "unnamed screen")

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
