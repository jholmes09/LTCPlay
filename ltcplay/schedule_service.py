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
"""
import json
import os
import socket
import struct
import threading
import time as _time
from collections import deque
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone

from . import appdata
from . import schedule as sch
from . import settings as settings_mod

RULE_FILE = "ltcplay_schedule.json"
PREVIOUS_SUFFIX = ".previous.json"
TONIGHT_PREFIX = "ltcplay_tonight_"
OPERATORS_FILE = "ltcplay_operators.json"
NTP_SERVER = "pool.ntp.org"
# The whole clock check, name lookup included, gets this long. It runs on its
# own thread, so even this never holds up a show.
CLOCK_CHECK_LIMIT_S = 5.0

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


class Service:
    """One scheduler, ticking, with the answers the page needs.

    `clock` returns an aware datetime and `ntp_query` returns an offset; both
    are injected so the selftest never waits and never touches the network.
    """

    TICK_S = 0.25
    JOURNAL = 400

    def __init__(self, path, clock=None, ntp_query=None, state_dir=None,
                 clock_limit_s=CLOCK_CHECK_LIMIT_S):
        self.path = path
        self.clock = clock or _utc_now
        self.ntp_query = ntp_query
        self.state_dir = state_dir or data_dir()
        self.clock_limit_s = clock_limit_s
        self.persist_error = ""
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
        self.journal = deque(maxlen=self.JOURNAL)
        self.clock_check = None
        self.operators, why = load_operators(self.state_dir)
        if why:
            self._journal_line("system", why, action="operators")
        # A plain callable, or None: set by web.py when an announcements
        # service is ALSO configured (serve()'s own job, see its module
        # docstring). Called the instant a show starts, so a playing
        # announcement can be told to stop without waiting on its own
        # next status() poll. schedule_service.py never imports announce.py
        # to make this call; it only ever calls whatever was set here.
        self.on_show_started = None
        # A second, more general hook, added the same way, for the
        # MadMapper link (madmapper.py's Link.on_transition; see web.py's
        # serve()): called after EVERY real state change, not only a show
        # starting, since Hold, Resume, Abort and Closing each need to know
        # too. schedule_service.py never imports madmapper.py either; see
        # _apply()'s own comment beside where this is queued.
        self.on_transition = None
        self._stop = threading.Event()
        self._thread = None
        self._clock_thread = None
        self._started = False
        self.reload()

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
            try:
                self.rule = load_rule(self.path)
                self.error = ""
            except (ValueError, OSError) as e:
                self.rule = None
                self.error = str(e)
                self.machine = None
                self._journal_line("system", "The schedule file was not "
                                   "loaded, so no show is scheduled. " +
                                   self.error)
            return self.rule

    # -- journal ----------------------------------------------------------
    def _journal_line(self, actor, text, **extra):
        now = self.clock()
        tz = self.rule.tz if self.rule else timezone.utc
        row = {"at": now.astimezone(tz).isoformat(timespec="seconds"),
               "state": self.machine.state if self.machine else "",
               "to_state": self.machine.state if self.machine else "",
               "actor": actor, "action": extra.pop("action", "note"),
               "outcome": extra.pop("outcome", "done"),
               "reason": extra.pop("reason", text), "show": None,
               "screen": None, "who": None, "text": text}
        row.update(extra)
        self.journal.append(row)

    def _record(self, out, now):
        for le in out.log:
            self.journal.append(le.to_dict())
        for eff in out.effects:
            desc = eff.kind + (f" show {eff.show}" if eff.show else "") + \
                (f" over {eff.seconds:g} s" if eff.seconds else "")
            self._journal_line(
                "system", f"Not performed, dry run: {desc}.",
                action=eff.kind, outcome="not performed",
                reason="dry run, no transport in this build",
                show=eff.show or None)

    def _apply(self, ev, now=None):
        now = now or self.clock()
        before = self.machine
        out = sch.step(self.machine, ev, now)
        self.machine = out.machine
        self._record(out, now)
        if DRY_RUN and self.machine.state == sch.CLOSING:
            # Nothing to wait for: nothing was faded.
            out2 = sch.step(self.machine,
                            sch.Event(sch.CLOSING_DONE, "system"), now)
            self.machine = out2.machine
            self._record(out2, now)
        if self.machine is not before:
            self._save_tonight()
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
            self._pending_hooks.append(lambda: hook(state_now))
        # Same shape, same reason, for madmapper.py's Link.on_transition:
        # queued here so it too only ever runs after self.lock is truly
        # free (see _locked()'s docstring), and on its own thread, since a
        # 1 s fade is exactly the kind of "hook that does real work" that
        # comment already anticipates. `before` can be None (the very
        # first _apply() of the process); `show` favours whichever side of
        # the transition actually names one, since a show that just ended
        # or was aborted has already been reset to 0 on `self.machine`.
        if self.on_transition is not None and self.machine is not before:
            before_state = before.state if before is not None else None
            before_show = before.running if before is not None else 0
            after_state, after_show = self.machine.state, self.machine.running
            ev_kind = ev.kind
            show_now = after_show or before_show
            fn = self.on_transition
            self._pending_hooks.append(
                lambda: fn(before_state, after_state, ev_kind, show_now))
        return out

    # -- tonight on disk --------------------------------------------------
    def _save_tonight(self):
        m = self.machine
        path = tonight_path(m.date, self.state_dir)
        try:
            write_json_atomic(path, sch.machine_to_doc(m))
        except OSError as e:
            msg = (f"Tonight's list could not be saved to {path}: "
                   f"{e.strerror or e}. The schedule carries on, but a "
                   f"restart now would go back to the schedule file and "
                   f"lose tonight's changes.")
            if msg != self.persist_error:
                self._journal_line("system", msg, action="save tonight",
                                   outcome="failed")
            self.persist_error = msg
            return False
        if self.persist_error:
            self._journal_line("system", f"Tonight's list is being saved "
                               f"to {path} again.", action="save tonight")
        self.persist_error = ""
        return True

    def _load_tonight(self, d, now):
        """Tonight's saved machine, or a fresh one from the rule with a
        sentence saying why. The show length, guard, grace and zone always
        come from the rule file; the saved list only says what happened
        tonight, and it is checked before it is believed. A file that fails
        the checks is set aside, never overwritten, so the morning read can
        still see it. If the rule changed since the list was saved, tonight
        is rebuilt from the new rule and only what already happened is kept."""
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
        aside = path[:-5] + ".unreadable.json"
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
            action="load tonight", outcome="failed")
        # Zero on anything uncertain: a show may have fired moments ago.
        m, notes = sch.assume_the_worst(sch.new_night(self.rule, d), now)
        for text in notes:
            self._journal_line("system", text, action="load tonight",
                               outcome="assumed the worst")
        return m

    # -- the night ------------------------------------------------------
    def _tonight(self, now):
        return now.astimezone(self.rule.tz).date()

    def _ensure_night(self, now):
        if self.rule is None:
            return False
        d = self._tonight(now)
        m = self.machine
        if m is not None and m.date != d:
            # A new day. Settle yesterday first: a show still on its list
            # (the machine was asleep across it) is marked MISSED in the
            # journal rather than dropped without a word.
            self._apply(sch.Event(sch.TICK, "scheduler"), now)
            if self.machine.state in (sch.SHOW, sch.PAUSED):
                # Never replace a running night: a show started by hand at
                # 23:58 finishes on yesterday's list.
                return True
            self.machine = None
        if self.machine is None:
            self.machine = replace(self._load_tonight(d, now),
                                   operators=self.operators)
            self._apply(sch.Event(sch.BOOT_DONE, "system"), now)
        return True

    def tick(self):
        with self._locked():
            now = self.clock()
            if not self._ensure_night(now):
                return None
            m = self.machine
            # Dry run: nothing was started, so the show "ends" when it would
            # have, which a pause moves later. A paused show never ends.
            if DRY_RUN and m.state == sch.SHOW and \
                    now >= m.expected_end(now):
                self._apply(sch.Event(sch.SHOW_ENDED, "system",
                                      detail="dry run, nothing was started",
                                      show=m.running), now)
            self._apply(sch.Event(sch.TICK, "scheduler"), now)
            return self.machine

    def check_clock(self):
        level, text, offset = check_clock(self.ntp_query,
                                          limit_s=self.clock_limit_s)
        with self._locked():
            self.clock_check = {"level": level, "text": text,
                                "offset_s": offset}
            self._journal_line("system", text, action="clock check",
                               outcome=level)
        return self.clock_check

    # -- running --------------------------------------------------------
    def start(self, thread=True):
        """Tick first, then check the clock on a thread of its own. A slow
        time server must never hold up the first look at the schedule: a
        3 s lookup at 17:59:59 would otherwise miss the 18:00 show. Safe to
        call twice."""
        if self._started:
            return self
        self._started = True
        self._safe_tick()
        if not thread:
            self.check_clock()
            return self
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="ltcplay-scheduler")
        self._thread.start()
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
                self._journal_line("system", f"The clock check failed: {e}.",
                                   outcome="error")

    def _safe_tick(self):
        try:
            self.tick()
        except Exception as e:                     # never kill the ticker
            with self._locked():
                self._journal_line(
                    "system", f"The scheduler hit a problem and carried "
                    f"on: {type(e).__name__}: {e}.", outcome="error")

    def _run(self):
        while not self._stop.is_set():
            self._safe_tick()
            self._stop.wait(self.TICK_S)

    def stop(self):
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2)

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
        ev = sch.Event(kinds[op], "operator", show=show,
                       at=str(body.get("at") or body.get("to") or ""),
                       screen=str(body.get("screen") or ""),
                       who=str(body.get("who") or ""))
        with self._locked():
            self.tick()
            if self.machine is None:
                raise ValueError("There is no schedule loaded, so there is "
                                 "no list to edit. " + self.error)
            out = self._apply(ev)
            if out.refused:
                raise ValueError(out.refused)
        return self.tonight_view()

    # -- the web routes ---------------------------------------------------
    GET_ROUTES = ("/api/schedule", "/api/schedule/tonight",
                  "/api/schedule/state")
    # Editing tonight's list is the only thing that can be posted. There is
    # deliberately no route that starts, stops, holds or arms anything.
    POST_ROUTES = ("/api/schedule/tonight",)

    def get(self, route):
        if route == "/api/schedule":
            return 200, self.rule_view()
        if route == "/api/schedule/tonight":
            return 200, self.tonight_view()
        if route == "/api/schedule/state":
            return 200, self.state_view()
        return 404, {"error": "no such thing here"}

    def post(self, route, body):
        if route == "/api/schedule/tonight":
            return 200, self.edit_tonight(body)
        return 404, {"error": "no such thing here"}
