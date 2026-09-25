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
import json
import os
import socket
import struct
import threading
import time as _time
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
    JOURNAL = journal.MEMORY_LINES
    # The ring buffer's pace: 5 samples a second (handoff section 9).
    SAMPLE_S = 1.0 / journal.RING_HZ

    def __init__(self, path, clock=None, ntp_query=None, state_dir=None,
                 clock_limit_s=CLOCK_CHECK_LIMIT_S, log_dir=None,
                 flame_provider=None, logbook=None):
        self.path = path
        self.clock = clock or _utc_now
        self.ntp_query = ntp_query
        self.state_dir = state_dir or data_dir()
        self.clock_limit_s = clock_limit_s
        self.persist_error = ""
        self.lock = threading.RLock()
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
            state=self._state_name)
        # The page's lines ARE the journal's records: one deque, not a copy.
        self.journal = self.logbook.memory
        self._summarised = set()
        self._looked_back = False
        self.operators, why = load_operators(self.state_dir)
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

    # -- the rule -------------------------------------------------------
    def reload(self):
        """Read the rule file. A bad file leaves the scheduler with no night
        and the reason on the page; it never takes the server down with it."""
        with self.lock:
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
            who=(le.who or "unnamed operator") if op else None,
            screen=(le.screen or "unnamed screen") if op else None,
            fault=le.outcome in self.FAULT_OUTCOMES)

    def _record(self, out, now):
        for le in out.log:
            self._record_logevent(le)
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
        if self.machine.state == sch.OFF and before is not None and \
                before.state != sch.OFF and self.machine.slots:
            # The night has closed, by End night or after its last show:
            # the morning read goes beside the journal now.
            how = (f"closed by {ev.who} with End night"
                   if ev.kind == sch.END_NIGHT else
                   "closed after the last show" if before.state != sch.BOOT
                   else "closed at start up, every show having passed")
            self._write_summary(how)
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
        kw = dict(state=m.state, slots=self._slot_rows(m), closed_by=how)
        if self.logbook.threaded():
            # Running for real: the summary reads and writes files, so it
            # happens beside the scheduler, never inside a tick.
            threading.Thread(target=self._log, daemon=True,
                             name="ltcplay-summary",
                             args=(self.logbook.write_summary, m.date),
                             kwargs=kw).start()
            return None
        return self._log(self.logbook.write_summary, m.date, **kw)

    def write_summary(self):
        """Write tonight's summary now, as it stands."""
        with self.lock:
            if self.machine is None:
                raise ValueError("There is no night loaded, so there is no "
                                 "summary to write. " + self.error)
            return self._write_summary("written on request")

    def _look_back(self, d):
        """Once per run: a night that ended without its summary (the
        program was not running when it closed, or the power went) gets
        one from its own journal, so the morning read is always there."""
        if self._looked_back:
            return
        self._looked_back = True
        prev = d - timedelta(days=1)
        folder = self.logbook.folder
        if os.path.exists(os.path.join(folder, journal.machine_name(prev))) \
                and not os.path.exists(
                    os.path.join(folder, journal.summary_name(prev))):
            self._log(self.logbook.write_summary, prev,
                      state=self._state_name(),
                      closed_by="written the next day from the journal, "
                                "because the night never closed while "
                                "ltcplay was running")

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
                                   outcome="failed", fault=True)
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
            if str(self.machine.date) not in self._summarised and \
                    self.machine.slots:
                self._write_summary("written at midnight; the night was "
                                    "never closed", self.machine)
            self._looked_back = True
            self.machine = None
            self._log(self.logbook.prune, d, state=self._state_name())
        if self.machine is None:
            first = not self._looked_back
            self._look_back(d)
            if first:
                self._log(self.logbook.prune, d, state=self._state_name())
            self.machine = replace(self._load_tonight(d, now),
                                   operators=self.operators)
            self._apply(sch.Event(sch.BOOT_DONE, "system"), now)
        return True

    def tick(self):
        with self.lock:
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
        with self.lock:
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
        # From here the journal writes on its own thread, so a slow or full
        # disk never holds up a tick.
        self.logbook.start_writer()
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
            with self.lock:
                self._journal_line(
                    "system", f"The clock check could not run "
                    f"({type(e).__name__}: {e}). Shows still start by this "
                    f"machine's clock, unchecked.", action="clock check",
                    outcome="failed", fault=True)

    def _safe_tick(self):
        try:
            self.tick()
        except Exception as e:                     # never kill the ticker
            with self.lock:
                self._journal_line(
                    "system", f"The scheduler hit a problem and carried "
                    f"on ({type(e).__name__}: {e}). It tries again on the "
                    f"next tick, a quarter of a second later.",
                    action="tick", outcome="failed", fault=True)

    def _run(self):
        while not self._stop.is_set():
            self._safe_tick()
            self._stop.wait(self.TICK_S)

    def stop(self):
        self._stop.set()
        for t in (self._thread, self._sample_thread):
            if t is not None:
                t.join(timeout=2)
        with self.lock:
            self._log(self.logbook.stopping, state=self._state_name(),
                      night=self._night())
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
        with self.lock:
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
        with self.lock:
            prev = previous_path(self.path)
            return {"ok": self.rule is not None, "error": self.error or None,
                    "path": self.path,
                    "previous": prev if os.path.exists(prev) else None,
                    "rule": sch.rule_to_doc(self.rule) if self.rule else None}

    def tonight_view(self):
        with self.lock:
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
        with self.lock:
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
        with self.lock:
            self.tick()
            if self.machine is None:
                raise ValueError("There is no schedule loaded, so there is "
                                 "no list to edit. " + self.error)
            out = self._apply(ev)
            if out.refused:
                raise ValueError(out.refused)
        return self.tonight_view()

    # -- the journal, for the page -------------------------------------------
    def journal_view(self, n=journal.PAGE_LINES):
        """The log strip: the last 20 journal lines, newest first, exactly as
        they are in the file, each with its age."""
        return {"ok": True,
                "as_of": self.logbook.local().isoformat(timespec="seconds"),
                "lines": self.logbook.recent(n)}

    def logging_view(self):
        return self.logbook.health()

    def _config_in_force(self):
        text = None
        try:
            with open(self.path, encoding="utf-8-sig") as fh:
                text = fh.read(1 << 20)
        except OSError as e:
            text = f"(could not be read: {e.strerror or e})"
        m = self.machine
        return {"schedule_file": self.path, "schedule_file_text": text,
                "rule": sch.rule_to_doc(self.rule) if self.rule else None,
                "rule_error": self.error or None,
                "operators": list(self.operators),
                "tonight": sch.machine_to_doc(m) if m else None,
                "tonight_file": (tonight_path(m.date, self.state_dir)
                                 if m else None),
                "dry_run": DRY_RUN, "state_dir": self.state_dir,
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
        with self.lock:
            config = self._config_in_force()
            state, night = self._state_name(), self._night()
        # The copy happens outside the scheduler's lock: saving a folder of
        # files must never hold up a tick.
        return self.logbook.save_incident(
            who=names[who.lower()], screen=screen, state=state, night=night,
            config=config, why=str(body.get("why") or ""))

    # -- the web routes ---------------------------------------------------
    GET_ROUTES = ("/api/schedule", "/api/schedule/tonight",
                  "/api/schedule/state", "/api/schedule/journal",
                  "/api/schedule/logging")
    # Editing tonight's list and saving an incident are the only things that
    # can be posted. There is deliberately no route that starts, stops,
    # holds or arms anything.
    POST_ROUTES = ("/api/schedule/tonight", "/api/schedule/incident")

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
        return 404, {"error": "no such thing here"}

    def post(self, route, body):
        if route == "/api/schedule/tonight":
            return 200, self.edit_tonight(body)
        if route == "/api/schedule/incident":
            out = self.save_incident(body)
            return (200 if out["ok"] else 500), out
        return 404, {"error": "no such thing here"}
