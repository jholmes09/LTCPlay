"""The show scheduler: which shows run tonight, and what happens when.

Pure. Nothing in here reads a file, opens a socket, looks at the clock or
starts anything. Time is handed in as `now`; the rule is handed in as a dict
or JSON text; every decision comes back as data: the new state, the effects
somebody else has to perform, and the log events to write. That is what makes
the late rule and the state machine testable second by second without a
single sleep, and it is why a bug in here cannot reach the rig by itself.

The engine DECIDES. It does not ACT. In this build nothing performs the
effects it returns; the MadMapper transport arrives in a later update.

The only thing it looks up is the time zone, through `zoneinfo`, which on
Windows needs the `tzdata` package.

It never looks at flame arm state. Flames are gated by the safety process,
not by the calendar, so there is no arm field, arm event or arm effect here.
"""
import json
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone

try:
    import zoneinfo
except ImportError:                       # pragma: no cover, 3.9+ has it
    zoneinfo = None


# ------------------------------------------------------------ vocabulary --

BOOT, IDLE, STANDBY, SHOW, CLOSING, OFF, HOLD = (
    "BOOT", "IDLE", "STANDBY", "SHOW", "CLOSING", "OFF", "HOLD")
STATES = (BOOT, IDLE, STANDBY, SHOW, CLOSING, OFF, HOLD)

ACTORS = ("scheduler", "operator", "madmapper", "safety", "reader", "system")

# Slot statuses. PENDING is a show still to come; the view calls the first
# of those NEXT. FIRED is a transition reason, never a resting status.
PENDING, RUNNING, DONE, MISSED, SKIPPED, ABORTED, FAULT = (
    "PENDING", "RUNNING", "DONE", "MISSED", "SKIPPED", "ABORTED", "FAULT")
NEXT = "NEXT"
STATUSES = (DONE, RUNNING, NEXT, PENDING, MISSED, SKIPPED, ABORTED, FAULT)
FINAL = frozenset((DONE, MISSED, SKIPPED, ABORTED, FAULT))

# Effects. Somebody else performs these; this module only names them.
PRESHOW_LOOK = "PRESHOW_LOOK"        # IDLE: the preshow look, no loop
INTERMISSION = "INTERMISSION"        # STANDBY: the intermission timeline
START_SHOW = "START_SHOW"            # select the bank, play from beginning
STOP_CONDUCTOR = "STOP_CONDUCTOR"    # MadMapper conductor stop
FADE_PIXELS = "FADE_PIXELS"          # pixels to black over `seconds`
ZERO_FLAME_CUES = "ZERO_FLAME_CUES"  # zero on every flame cue channel, now
BLACKOUT = "BLACKOUT"                # everything dark
EFFECTS = (PRESHOW_LOOK, INTERMISSION, START_SHOW, STOP_CONDUCTOR,
           FADE_PIXELS, ZERO_FLAME_CUES, BLACKOUT)

# Abort, precisely, per the handoff. Flames first because the handoff says
# "immediately" for them and they are the one that matters for safety; the
# performer is free to run the three at once. Aborting never disarms.
ABORT_FADE_S = 1.0
CLOSING_FADE_S = 1.0

# Events, and who may send each one. A blank or unknown actor is a
# programming error and raises; an event sent in the wrong state is refused
# with a sentence.
BOOT_DONE = "BOOT_DONE"
TICK = "TICK"
SHOW_CONFIRMED = "SHOW_CONFIRMED"   # timecode seen: the show really started
SHOW_ENDED = "SHOW_ENDED"
SHOW_FAILED = "SHOW_FAILED"         # the show did NOT start; see _show_failed
FAULT_RAISED = "FAULT_RAISED"
CLEAR_FAULT = "CLEAR_FAULT"
CLOSING_DONE = "CLOSING_DONE"
START_NOW = "START_NOW"
HOLD_ON = "HOLD"
RESUME = "RESUME"
SKIP_NEXT = "SKIP_NEXT"
DELAY_NEXT = "DELAY_NEXT"
DELAY_REST = "DELAY_REST"
ABORT = "ABORT"
END_NIGHT = "END_NIGHT"
EDIT_MOVE = "EDIT_MOVE"
EDIT_ADD = "EDIT_ADD"
EDIT_REMOVE = "EDIT_REMOVE"

DEVICES = ("madmapper", "reader", "system")
EVENT_ACTORS = {
    BOOT_DONE: ("system",),
    TICK: ("scheduler",),
    SHOW_CONFIRMED: DEVICES,
    SHOW_ENDED: DEVICES,
    SHOW_FAILED: DEVICES,
    FAULT_RAISED: ("scheduler", "madmapper", "safety", "reader", "system"),
    CLEAR_FAULT: ("operator", "system"),
    CLOSING_DONE: ("system",),
    START_NOW: ("operator",),
    HOLD_ON: ("operator",),
    RESUME: ("operator",),
    SKIP_NEXT: ("operator",),
    DELAY_NEXT: ("operator",),
    DELAY_REST: ("operator",),
    ABORT: ("operator",),
    END_NIGHT: ("operator",),
    EDIT_MOVE: ("operator",),
    EDIT_ADD: ("operator",),
    EDIT_REMOVE: ("operator",),
}
EVENTS = tuple(EVENT_ACTORS)
DELAY_CHOICES = (5, 10)

# The late rule's ceiling. A bigger number in the rule file is refused with a
# sentence rather than quietly clamped: a silent clamp is a setting that says
# one thing and does another.
MAX_LATE_GRACE_S = 15


# -------------------------------------------------------------- the rule --

class RuleError(ValueError):
    """The rule file is wrong. Carries every problem found, each a sentence."""

    def __init__(self, problems, where=""):
        self.problems = list(problems)
        head = f"{where}: " if where else ""
        if len(self.problems) == 1:
            msg = head + self.problems[0]
        else:
            msg = (head + f"{len(self.problems)} things are wrong with the "
                   f"schedule:\n  " + "\n  ".join(self.problems))
        super().__init__(msg)


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
RULE_KEYS = frozenset(("timezone", "season", "weekly", "exceptions",
                       "show_len_s", "guard_s", "late_grace_s", "version"))
SEASON_KEYS = frozenset(("first_date", "last_date"))
NIGHT_KEYS = frozenset(("first_start", "interval_min", "last_end"))

_TIME = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


@dataclass(frozen=True)
class NightRule:
    first_start: time
    interval_min: int
    last_end: time


@dataclass(frozen=True)
class Rule:
    timezone: str
    first_date: date
    last_date: date
    weekly: dict             # "thu" -> NightRule
    exceptions: dict         # date -> NightRule, or None for a closed night
    show_len_s: int
    guard_s: int
    late_grace_s: int = 0
    version: int = 0
    tz: object = field(default=None, compare=False, repr=False)


def zone(name):
    """The time zone, or a sentence saying why it is not there."""
    if not isinstance(name, str) or not name:
        raise RuleError(["timezone has to name a time zone, such as "
                         "\"America/Denver\"."])
    if zoneinfo is None:                  # pragma: no cover
        raise RuleError(["This Python has no time zone support, so the "
                         "schedule cannot run on it."])
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        raise RuleError([
            f"The time zone {name!r} is not known on this machine. Check the "
            f"spelling. On Windows, time zones come from the tzdata package, "
            f"and the schedule cannot run until it is installed."])


def _whole(v, name, low, high=None, unit="seconds"):
    """A whole number in range, or a sentence. True and 3.5 are not numbers
    of seconds, whatever JSON thinks."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) \
            or (isinstance(v, float) and not v.is_integer()):
        return None, f"{name} has to be a whole number of {unit}, not {v!r}."
    v = int(v)
    if v < low:
        return None, f"{name} is {v}, and it cannot be less than {low}."
    if high is not None and v > high:
        return None, f"{name} is {v}, and the most it can be is {high}."
    return v, None


def parse_time(text, name="time"):
    """'17:30' or '17:30:15' as a time of day, or ValueError with a sentence."""
    m = _TIME.match(text.strip()) if isinstance(text, str) else None
    if not m:
        raise ValueError(f"{name} has to be a time of day like \"17:30\", "
                         f"not {text!r}.")
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if h > 23 or mi > 59 or s > 59:
        raise ValueError(f"{name} is {text!r}, which is not a time of day.")
    return time(h, mi, s)


def _date(text, name):
    try:
        if not isinstance(text, str):
            raise ValueError
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{name} has to be a date like \"2026-11-14\", "
                         f"not {text!r}.")


def _night(doc, where, show_len_s, guard_s, problems):
    if not isinstance(doc, dict):
        problems.append(f"{where} has to be a block with first_start, "
                        f"interval_min and last_end.")
        return None
    unknown = sorted(k for k in doc if k not in NIGHT_KEYS)
    if unknown:
        problems.append(f"{where}: {', '.join(repr(k) for k in unknown)} is "
                        f"not a setting a night has. It takes: "
                        f"{', '.join(sorted(NIGHT_KEYS))}.")
    missing = sorted(k for k in NIGHT_KEYS if k not in doc)
    if missing:
        problems.append(f"{where} is missing {', '.join(missing)}.")
        return None
    try:
        first = parse_time(doc["first_start"], f"{where} first_start")
        last = parse_time(doc["last_end"], f"{where} last_end")
    except ValueError as e:
        problems.append(str(e))
        return None
    interval, why = _whole(doc["interval_min"], f"{where} interval_min", 1,
                           unit="minutes")
    if why:
        problems.append(why)
        return None
    if last <= first:
        problems.append(f"{where}: last_end {_hm(last)} is not after "
                        f"first_start {_hm(first)}. A night cannot run past "
                        f"midnight.")
        return None
    if show_len_s is None or guard_s is None:
        return NightRule(first, interval, last)
    need = show_len_s + guard_s
    if interval * 60 < need:
        problems.append(
            f"{where}: interval_min is {interval}, but a show takes "
            f"{show_len_s} s and the guard after it is {guard_s} s, so shows "
            f"would run into each other. The interval has to be at least "
            f"{-(-need // 60)} minutes.")
        return None
    span = (datetime.combine(date(2000, 1, 3), last)
            - datetime.combine(date(2000, 1, 3), first)).total_seconds()
    if span < need:
        problems.append(
            f"{where}: from {_hm(first)} to {_hm(last)} there is not room for "
            f"one show, which needs {show_len_s} s plus the {guard_s} s guard "
            f"before last_end.")
        return None
    return NightRule(first, interval, last)


def parse_rule(doc, where=""):
    """Validate a rule document and return a Rule, or raise RuleError naming
    every problem at once. Unknown keys fail loudly: a setting spelled wrong
    loads clean and silently does nothing, which is worse than missing."""
    if isinstance(doc, (str, bytes)):
        try:
            doc = json.loads(doc)
        except ValueError as e:
            raise RuleError([f"This is not readable JSON: {e}."], where)
    if not isinstance(doc, dict):
        raise RuleError(["The schedule has to be one JSON object."], where)
    problems = []
    unknown = sorted(k for k in doc if k not in RULE_KEYS)
    if unknown:
        problems.append(f"{', '.join(repr(k) for k in unknown)} is not a "
                        f"setting the schedule has. It takes: "
                        f"{', '.join(sorted(RULE_KEYS))}.")
    for k in ("timezone", "season", "weekly", "show_len_s", "guard_s"):
        if k not in doc:
            problems.append(f"The schedule is missing {k}.")

    tz = None
    if "timezone" in doc:
        try:
            tz = zone(doc["timezone"])
        except RuleError as e:
            problems.extend(e.problems)

    show_len_s = guard_s = None
    grace = 0
    version = 0
    if "show_len_s" in doc:
        show_len_s, why = _whole(doc["show_len_s"], "show_len_s", 1)
        if why:
            problems.append(why)
    if "guard_s" in doc:
        guard_s, why = _whole(doc["guard_s"], "guard_s", 0)
        if why:
            problems.append(why)
    if "late_grace_s" in doc:
        grace, why = _whole(doc["late_grace_s"], "late_grace_s", 0,
                            MAX_LATE_GRACE_S)
        if why:
            if isinstance(doc["late_grace_s"], (int, float)) \
                    and not isinstance(doc["late_grace_s"], bool) \
                    and doc["late_grace_s"] > MAX_LATE_GRACE_S:
                why += (" A show that far behind its time does not start by "
                        "itself; the operator can press Start now.")
            problems.append(why)
            grace = 0
    if "version" in doc:
        version, why = _whole(doc["version"], "version", 0, unit="saves")
        if why:
            problems.append(why)
            version = 0

    first_date = last_date = None
    season = doc.get("season")
    if "season" in doc:
        if not isinstance(season, dict):
            problems.append("season has to be a block with first_date and "
                            "last_date.")
        else:
            unknown = sorted(k for k in season if k not in SEASON_KEYS)
            if unknown:
                problems.append(f"season: {', '.join(repr(k) for k in unknown)}"
                                f" is not a setting the season has. It takes: "
                                f"first_date, last_date.")
            try:
                first_date = _date(season.get("first_date"),
                                   "season first_date")
                last_date = _date(season.get("last_date"), "season last_date")
            except ValueError as e:
                problems.append(str(e))
                first_date = last_date = None
            if first_date and last_date and last_date < first_date:
                problems.append(f"The season ends on {last_date} before it "
                                f"starts on {first_date}.")

    weekly = {}
    if "weekly" in doc:
        if not isinstance(doc["weekly"], dict):
            problems.append("weekly has to be a block of weekdays, such as "
                            "\"thu\", \"fri\" and \"sat\".")
        else:
            for k, v in doc["weekly"].items():
                if k not in WEEKDAYS:
                    problems.append(f"weekly: {k!r} is not a weekday. Use "
                                    f"{', '.join(WEEKDAYS)}.")
                    continue
                n = _night(v, f"weekly {k}", show_len_s, guard_s, problems)
                if n is not None:
                    weekly[k] = n

    exceptions = {}
    exc = doc.get("exceptions", {})
    if not isinstance(exc, dict):
        problems.append("exceptions has to be a block of dates.")
        exc = {}
    for k, v in exc.items():
        try:
            d = _date(k, "An exception")
        except ValueError as e:
            problems.append(str(e))
            continue
        if first_date and last_date and not (first_date <= d <= last_date):
            problems.append(f"The exception for {d} is outside the season "
                            f"({first_date} to {last_date}), so it would never "
                            f"be used.")
            continue
        if v is None:
            exceptions[d] = None
            continue
        n = _night(v, f"exception {d}", show_len_s, guard_s, problems)
        if n is not None:
            exceptions[d] = n

    if problems:
        raise RuleError(problems, where)
    return Rule(timezone=doc["timezone"], first_date=first_date,
                last_date=last_date, weekly=weekly, exceptions=exceptions,
                show_len_s=show_len_s, guard_s=guard_s, late_grace_s=grace,
                version=version, tz=tz)


def rule_to_doc(rule):
    """The rule back as the JSON it came from, normalised."""
    def night(n):
        if n is None:
            return None
        return {"first_start": _hms(n.first_start),
                "interval_min": n.interval_min,
                "last_end": _hms(n.last_end)}
    return {
        "timezone": rule.timezone,
        "season": {"first_date": rule.first_date.isoformat(),
                   "last_date": rule.last_date.isoformat()},
        "weekly": {k: night(rule.weekly[k]) for k in WEEKDAYS
                   if k in rule.weekly},
        "exceptions": {d.isoformat(): night(n)
                       for d, n in sorted(rule.exceptions.items())},
        "show_len_s": rule.show_len_s,
        "guard_s": rule.guard_s,
        "late_grace_s": rule.late_grace_s,
        "version": rule.version,
    }


# ------------------------------------------------------------- expansion --

@dataclass(frozen=True)
class Plan:
    """One date, expanded. `source` says why it looks the way it does."""
    date: date
    source: str              # weekly, exception, closed, dark, off-season
    night: object            # the NightRule used, or None
    starts: tuple            # aware datetimes in the rule's zone
    why: str                 # a sentence for the page


def _wall(d, t, tz):
    """A wall clock time on a date, in the zone. A time that does not exist
    (inside the spring gap) comes out one hour later, which is what the wall
    clock will read; a time that happens twice (the autumn repeat) is the
    first one. Neither can happen with evening shows, but the rule is written
    for any hours."""
    return datetime.combine(d, t, tzinfo=tz).astimezone(timezone.utc) \
        .astimezone(tz)


def expand(rule, d):
    """The shows for one date. No I/O; the answer depends only on the rule and
    the date. Slots are first_start, then every interval_min in real elapsed
    time, and one is kept only if start + show_len_s + guard_s <= last_end,
    because last_end is when the last show must be finished."""
    wd = WEEKDAYS[d.weekday()]
    if not (rule.first_date <= d <= rule.last_date):
        return Plan(d, "off-season", None, (),
                    f"{d} is outside the season, so there are no shows.")
    if d in rule.exceptions:
        n = rule.exceptions[d]
        if n is None:
            return Plan(d, "closed", None, (),
                        f"{d} is closed by an exception in the schedule.")
        source, why = "exception", f"{d} has its own hours in the schedule."
    elif wd in rule.weekly:
        n = rule.weekly[wd]
        source, why = "weekly", f"{d} runs the usual {wd} hours."
    else:
        return Plan(d, "dark", None, (),
                    f"{d} is a {wd}, which is a dark night.")
    tz = rule.tz or zone(rule.timezone)
    start = datetime.combine(d, n.first_start, tzinfo=tz).astimezone(
        timezone.utc)
    end = datetime.combine(d, n.last_end, tzinfo=tz).astimezone(timezone.utc)
    step = timedelta(minutes=n.interval_min)
    need = timedelta(seconds=rule.show_len_s + rule.guard_s)
    out = []
    s = start
    while s + need <= end:
        out.append(s.astimezone(tz))
        s += step
    return Plan(d, source, n, tuple(out), why)


def expand_season(rule):
    """Every date of the season, expanded. For tests and for the page."""
    d, out = rule.first_date, []
    while d <= rule.last_date:
        out.append(expand(rule, d))
        d += timedelta(days=1)
    return out


# -------------------------------------------------------- the late rule --

ONE_SECOND = timedelta(seconds=1)


def _utc(dt):
    """Every instant the machine keeps is in UTC. Python subtracts and
    compares two datetimes that share a zone as bare wall clock times, which
    is an hour wrong across a clock change; in UTC there is no such thing."""
    return dt.astimezone(timezone.utc)


def lateness_s(slot_start, now):
    """Whole seconds past the slot's start, rounded down.

    DESIGN CHOICE, for Jeff: a show is on time for the whole second it names.
    With late_grace_s 0, a show at 18:00:00 still starts at 18:00:00.999 and
    does not start at 18:00:01.000. Grace N widens that to the end of second
    N. Without this, a scheduler that looks a few times a second would
    almost never land on the exact instant, and grace 0 would mean never."""
    return (_utc(now) - _utc(slot_start)) // ONE_SECOND


def may_fire(slot_start, now, grace_s):
    """The late rule. A slot fires only while now <= start + late_grace_s,
    counted in whole seconds (see lateness_s), and never before its start."""
    return 0 <= lateness_s(slot_start, now) <= grace_s


def fmt_span(seconds):
    """4m 12s. 1h 0m 5s. 12s. Never a colon: a colon reads as a time of day."""
    s = int(seconds)
    if s < 0:
        s = -s
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _hm(t):
    return t.strftime("%H:%M") if t.second == 0 else t.strftime("%H:%M:%S")


_hms = _hm


def clock(dt):
    """A time for a sentence: 18:00, or 18:00:07 when the seconds matter."""
    return _hm(dt.timetz())


# ------------------------------------------------------ the state machine --

@dataclass(frozen=True)
class Slot:
    n: int                   # the show number tonight, fixed once given
    start: datetime          # in UTC; shown in the rule's zone
    status: str = PENDING
    reason: str = ""
    origin: str = "rule"     # rule, edit (added tonight), operator (Start now)
    planned: object = None   # the start it had before tonight's edits
    fired_at: object = None
    confirmed_at: object = None   # timecode seen after the start
    ended_at: object = None


@dataclass(frozen=True)
class Event:
    kind: str
    actor: str
    detail: str = ""         # what a device reported, in words
    minutes: int = 0         # Delay next / Delay the rest
    confirmed: bool = False  # Abort and End night
    show: int = 0            # which show an edit or an end is about
    at: str = ""             # a wall clock time for an edit, "18:25"
    screen: str = ""         # operator events: which screen, from config
    who: str = ""            # operator events: the operator's name


@dataclass(frozen=True)
class Effect:
    kind: str
    show: int = 0
    seconds: float = 0.0


@dataclass(frozen=True)
class LogEvent:
    at: datetime             # local time with offset
    state: str               # the state the system was in
    to_state: str            # the state it is in afterwards
    actor: str
    action: str
    outcome: str             # done, refused, fired, missed, ...
    reason: str              # never blank
    show: int = 0
    screen: str = ""
    who: str = ""
    text: str = ""           # the plain English line for the journal

    def to_dict(self):
        return {"at": self.at.isoformat(timespec="seconds"),
                "state": self.state, "to_state": self.to_state,
                "actor": self.actor, "action": self.action,
                "outcome": self.outcome, "reason": self.reason,
                "show": self.show or None, "screen": self.screen or None,
                "who": self.who or None, "text": self.text}


@dataclass(frozen=True)
class Machine:
    date: date
    tz: object
    show_len_s: int
    guard_s: int
    late_grace_s: int
    state: str = BOOT
    slots: tuple = ()
    running: int = 0             # the show number running, 0 for none
    last_end: object = None      # when the last show ended, for guard_s
    hold_pending: bool = False   # Hold pressed during a show
    held_from: str = ""          # where Resume goes back to
    faults: tuple = ()           # FAULT is a flag: these are its sentences
    shows_started: int = 0
    night_end: object = None     # tonight's last_end, UTC; None when dark
    resumed_from: str = ""       # the state saved before a restart

    @property
    def fault(self):
        return bool(self.faults)

    def slot(self, n):
        for s in self.slots:
            if s.n == n:
                return s
        return None

    def pending(self):
        return sorted((s for s in self.slots if s.status == PENDING),
                      key=lambda s: (s.start, s.n))

    def next_slot(self):
        p = self.pending()
        return p[0] if p else None

    def hm(self, dt):
        """An instant as tonight's wall clock reads it: 18:20."""
        return clock(dt.astimezone(self.tz))


@dataclass(frozen=True)
class Outcome:
    machine: Machine
    effects: tuple = ()
    log: tuple = ()
    refused: str = ""

    @property
    def accepted(self):
        return not self.refused


def new_night(rule, d):
    """A fresh machine for one date, in BOOT, with the rule's shows on it."""
    plan = expand(rule, d)
    slots = tuple(Slot(n=i + 1, start=_utc(s))
                  for i, s in enumerate(plan.starts))
    tz = rule.tz or zone(rule.timezone)
    end = (_utc(datetime.combine(d, plan.night.last_end, tzinfo=tz))
           if plan.night is not None else None)
    return Machine(date=d, tz=tz, show_len_s=rule.show_len_s,
                   guard_s=rule.guard_s, late_grace_s=rule.late_grace_s,
                   slots=slots, night_end=end)


def midnight(m):
    """The end of tonight's date: no show on tonight's list may run past it."""
    return _utc(datetime.combine(m.date + timedelta(days=1), time(0),
                                 tzinfo=m.tz))


def runs_late(m, s):
    """True when a show still to come finishes, with its guard, after
    tonight's last_end. Allowed, because an operator may need to run late,
    but said out loud."""
    return (m.night_end is not None and s.status == PENDING and
            s.start + timedelta(seconds=m.show_len_s + m.guard_s)
            > m.night_end)


def late_warnings(m):
    return [f"Show {s.n} at {m.hm(s.start)} finishes after tonight's "
            f"last_end of {m.hm(m.night_end)}, so the night runs late."
            for s in m.pending() if runs_late(m, s)]


def _local(m, now):
    return now.astimezone(m.tz)


class _Tx:
    """Collects one transition's changes. Local to `step`; never escapes."""

    def __init__(self, m, ev, now):
        self.m0 = m
        self.m = m
        self.ev = ev
        self.when = now
        self.effects = []
        self.log = []

    def set_slot(self, n, **kw):
        slots = tuple(replace(s, **kw) if s.n == n else s
                      for s in self.m.slots)
        self.m = replace(self.m, slots=slots)

    def note(self, action, outcome, reason, text, show=0, actor=None,
             state=None):
        ev = self.ev
        actor = actor or ev.actor
        op = actor == "operator"
        self.log.append(LogEvent(
            at=_local(self.m, self.when), state=state or self.m0.state,
            to_state=self.m.state, actor=actor, action=action,
            outcome=outcome, reason=reason, show=show,
            screen=ev.screen if op else "", who=ev.who if op else "",
            text=text))

    def _set_state(self, state):
        # Only _enter calls this, so no state is ever entered without the
        # effects that belong to it.
        self.m = replace(self.m, state=state)

    def done(self):
        # Every log line records where the machine ended up, even the ones
        # written before the last state change in this transition.
        final = self.m.state
        log = tuple(replace(e, to_state=final) for e in self.log)
        return Outcome(self.m, tuple(self.effects), log)


def _operator_name(ev):
    return ev.who or "The operator"


def _refuse(m, ev, now, sentence):
    op = ev.actor == "operator"
    le = LogEvent(at=_local(m, now), state=m.state, to_state=m.state,
                  actor=ev.actor, action=ev.kind, outcome="refused",
                  reason=sentence, screen=ev.screen if op else "",
                  who=ev.who if op else "",
                  text=f"{_who(ev)} {_verb(ev)} was refused. {sentence}")
    return Outcome(m, (), (le,), sentence)


def _who(ev):
    if ev.actor == "operator":
        return _operator_name(ev) + "'s"
    return {"scheduler": "The scheduler's", "madmapper": "MadMapper's",
            "safety": "The safety process's", "reader": "The wire reader's",
            "system": "The system's"}[ev.actor]


def _verb(ev):
    return {
        BOOT_DONE: "start up", TICK: "clock tick",
        SHOW_CONFIRMED: "show running report",
        SHOW_ENDED: "show ended report", SHOW_FAILED: "show failed report",
        FAULT_RAISED: "fault report", CLEAR_FAULT: "clear fault",
        CLOSING_DONE: "closing finished report", START_NOW: "Start now",
        HOLD_ON: "Hold", RESUME: "Resume", SKIP_NEXT: "Skip next",
        DELAY_NEXT: f"Delay next +{ev.minutes}",
        DELAY_REST: f"Delay the rest of the night +{ev.minutes}",
        ABORT: "Abort show", END_NIGHT: "End night",
        EDIT_MOVE: "move of a show", EDIT_ADD: "added show",
        EDIT_REMOVE: "removal of a show",
    }[ev.kind]


def _check_event(ev):
    if not isinstance(ev, Event):
        raise TypeError("step() takes an Event")
    if ev.kind not in EVENT_ACTORS:
        raise ValueError(f"{ev.kind!r} is not an event the scheduler knows. "
                         f"It knows: {', '.join(EVENTS)}.")
    if not ev.actor:
        raise ValueError(f"The {ev.kind} event has no actor. Every event says "
                         f"who did it: {', '.join(ACTORS)}.")
    if ev.actor not in ACTORS:
        raise ValueError(f"{ev.actor!r} is not an actor. It has to be one of "
                         f"{', '.join(ACTORS)}.")
    if ev.actor not in EVENT_ACTORS[ev.kind]:
        raise ValueError(f"A {ev.kind} event cannot come from {ev.actor}; it "
                         f"comes from {', '.join(EVENT_ACTORS[ev.kind])}.")


def _aware(now):
    if not isinstance(now, datetime) or now.tzinfo is None \
            or now.utcoffset() is None:
        raise ValueError("now has to be a datetime with a time zone. A bare "
                         "time is ambiguous on the night the clocks change.")
    return now


def _guard_left(m, now):
    """Seconds still to wait after the previous show before another may
    start, or 0."""
    if m.last_end is None:
        return 0
    left = (m.last_end + timedelta(seconds=m.guard_s) - now) / ONE_SECOND
    return max(0.0, left)


def _closing_effects():
    return [Effect(ZERO_FLAME_CUES), Effect(STOP_CONDUCTOR),
            Effect(FADE_PIXELS, seconds=CLOSING_FADE_S), Effect(BLACKOUT)]


def _abort_effects(n):
    return [Effect(ZERO_FLAME_CUES, show=n), Effect(STOP_CONDUCTOR, show=n),
            Effect(FADE_PIXELS, show=n, seconds=ABORT_FADE_S)]


# What arriving in each state asks for. The ONE place states are entered:
# IDLE is the preshow look; STANDBY is the intermission timeline running; HOLD
# keeps the intermission running; SHOW starts the show; CLOSING fades out,
# stops MadMapper, blacks out and zeroes the flame cues; OFF asks for nothing.
def entry_effects(state, show=0):
    if state == IDLE:
        return [Effect(PRESHOW_LOOK)]
    if state in (STANDBY, HOLD):
        return [Effect(INTERMISSION)]
    if state == SHOW:
        return [Effect(START_SHOW, show=show)]
    if state == CLOSING:
        return _closing_effects()
    return []


def _enter(tx, state, show=0):
    """Change state and add the effects that belong to arriving there."""
    tx._set_state(state)
    tx.effects.extend(entry_effects(state, show))


def _after_show(tx, abort=False):
    """Where a show that has just stopped leaves the night."""
    m = tx.m
    if m.hold_pending:
        tx.m = replace(tx.m, hold_pending=False, held_from=STANDBY)
        _enter(tx, HOLD)
    elif abort or m.next_slot() is not None:
        # Abort stays in STANDBY even when no show is left; the next tick
        # closes the night if so.
        _enter(tx, STANDBY)
    else:
        _enter(tx, CLOSING)


def _fire(tx, s, operator=False):
    now = tx.when
    reason = "FIRED (operator)" if operator else "FIRED"
    tx.set_slot(s.n, status=RUNNING, reason=reason, fired_at=now)
    tx.m = replace(tx.m, running=s.n, shows_started=tx.m.shows_started + 1,
                   hold_pending=(tx.m.state == HOLD))
    _enter(tx, SHOW, show=s.n)
    if operator:
        text = (f"{_operator_name(tx.ev)} pressed Start now"
                f"{_screen(tx.ev)}. Show {s.n} started at "
                f"{clock(_local(tx.m, now))}.")
    else:
        late = lateness_s(s.start, now)
        text = (f"Show {s.n} started on schedule at "
                f"{clock(_local(tx.m, s.start))}"
                + (f", {late} s after its time." if late else "."))
    tx.note(START_NOW if operator else "fire", "fired", reason, text,
            show=s.n, actor="operator" if operator else "scheduler")


def _screen(ev):
    return f" on the {ev.screen}" if ev.screen else ""


def _miss(tx, s, why):
    tx.set_slot(s.n, status=MISSED, reason=why)
    tx.note("miss", "missed", why,
            f"Show {s.n} at {clock(_local(tx.m, s.start))} did not start: "
            f"{why}. It will not start by itself; Start now is still there.",
            show=s.n, actor="scheduler")


def _sweep(tx):
    """Walk the pending shows against the clock: mark what has passed,
    fire at most one that is due. Used by BOOT_DONE and TICK."""
    now = tx.when
    for s in tx.m.pending():
        late = lateness_s(s.start, now)
        if late < 0:
            break
        state = tx.m.state
        if late > tx.m.late_grace_s:
            if state == HOLD:
                why = "MISSED (on hold)"
            elif state == SHOW:
                why = f"MISSED (show {tx.m.running} was running)"
            elif state in (BOOT, IDLE, STANDBY) and \
                    _guard_left(tx.m, s.start
                                + timedelta(seconds=tx.m.late_grace_s)) > 0:
                why = (f"MISSED (inside the {tx.m.guard_s} s guard after the "
                       f"previous show)")
            else:
                why = f"MISSED (late by {fmt_span(late)})"
            _miss(tx, s, why)
            continue
        # Inside the window. Only a waiting machine fires, and only if the
        # guard after the previous show has run out.
        if state in (IDLE, STANDBY) and _guard_left(tx.m, now) == 0:
            _fire(tx, s)
        break


def _boot_done(m, ev, now):
    tx = _Tx(m, ev, now)
    if not m.slots:
        _enter(tx, OFF)
        tx.note(BOOT_DONE, "done", "no shows tonight",
                f"The scheduler started. There are no shows on "
                f"{m.date}, so it is off for the night.")
        return tx.done()
    was = m.resumed_from
    if was and m.running:
        # The program stopped during a show. Nothing is playing now (ltcplay
        # is the clock), so that show is over: FAULT, the rig made safe, and
        # the guard counted from now so the next show cannot crowd it.
        n = m.running
        why = "FAULT (ltcplay restarted during the show)"
        tx.set_slot(n, status=FAULT, reason=why, ended_at=now)
        tx.m = replace(tx.m, running=0, last_end=now,
                       faults=tx.m.faults + (
                           f"Show {n} was cut off when ltcplay restarted.",))
        tx.effects.extend(_abort_effects(n))
        tx.note(BOOT_DONE, "fault", why,
                f"ltcplay restarted during show {n}. The show is over and "
                f"marked FAULT; flame cues zeroed, MadMapper stopped, pixels "
                f"faded.", show=n)
    tx.m = replace(tx.m, resumed_from="")
    # Anything already past its window is MISSED before choosing a state,
    # which is the restart rule: a reboot at 18:04 marks 18:00 MISSED and
    # waits for 18:20. It never starts a show here; the first TICK does, and
    # only inside the window. A restored night goes through the same rule.
    _sweep(tx)
    if tx.m.next_slot() is None:
        _enter(tx, CLOSING)
        why = "every show tonight has already passed"
    elif was == HOLD or tx.m.hold_pending:
        tx.m = replace(tx.m, hold_pending=False, held_from=STANDBY)
        _enter(tx, HOLD)
        why = "it was on hold before the restart"
    elif any(s.status != PENDING for s in tx.m.slots):
        _enter(tx, STANDBY)
        why = "shows have already passed tonight"
    else:
        _enter(tx, IDLE)
        why = "before the first show"
    nxt = tx.m.next_slot()
    head = ("The scheduler restarted and picked up tonight's list as it was"
            if was else "The scheduler started")
    tx.note(BOOT_DONE, "done", why,
            f"{head}, in {tx.m.state}: {why}."
            + (f" Next is show {nxt.n} at {clock(_local(m, nxt.start))}."
               if nxt else ""))
    return tx.done()


def _tick(m, ev, now):
    if m.state in (CLOSING, OFF):
        return Outcome(m)
    tx = _Tx(m, ev, now)
    before = tx.m.state
    _sweep(tx)
    st = tx.m.state
    if st in (IDLE, STANDBY) and tx.m.next_slot() is None:
        _enter(tx, CLOSING)
        tx.note("close", "done", "no more shows tonight",
                "The last show of the night has gone by. Closing: flame cues "
                "to zero, MadMapper stopped, pixels faded, blackout.",
                actor="scheduler")
    elif before == IDLE and st == IDLE and \
            any(s.status == MISSED for s in tx.m.slots):
        # The first show went by without starting; the night is under way.
        _enter(tx, STANDBY)
        tx.note("standby", "done", "the first show was missed",
                "Waiting for the next show with the intermission running.",
                actor="scheduler")
    return tx.done()


def _show_stopped(m, ev, now, status, reason, text, effects, abort=False):
    tx = _Tx(m, ev, now)
    n = m.running
    tx.set_slot(n, status=status, reason=reason, ended_at=now)
    tx.m = replace(tx.m, running=0, last_end=now)
    if status == FAULT:
        tx.m = replace(tx.m, faults=tx.m.faults + (text,))
    tx.effects.extend(effects)
    _after_show(tx, abort=abort)
    tx.note(ev.kind, status.lower(), reason, text, show=n)
    return tx.done()


def _show_ended(m, ev, now):
    if ev.show and ev.show != m.running:
        return _refuse(m, ev, now, f"The report is about show {ev.show}, but "
                                   f"show {m.running} is the one running.")
    d = f" ({ev.detail})" if ev.detail else ""
    return _show_stopped(m, ev, now, DONE, f"DONE{d}",
                         f"Show {m.running} finished at "
                         f"{clock(_local(m, now))}{d}.", [])


def _show_confirmed(m, ev, now):
    s = m.slot(m.running)
    if ev.show and ev.show != m.running:
        return _refuse(m, ev, now, f"The report is about show {ev.show}, but "
                                   f"show {m.running} is the one running.")
    if s.confirmed_at is not None:
        return _refuse(m, ev, now, f"Show {s.n} was already confirmed "
                                   f"running.")
    tx = _Tx(m, ev, now)
    tx.set_slot(s.n, confirmed_at=now)
    late = (now - s.fired_at) / ONE_SECOND
    tx.note(SHOW_CONFIRMED, "done", "running",
            f"Show {s.n} is running: {ev.actor} confirmed it {late:.1f} s "
            f"after the start was sent.", show=s.n)
    return tx.done()


def _show_failed(m, ev, now):
    """The show did NOT start: the start went out and it never confirmed
    (for example no timecode after start). Only then is a show stopped by a
    fault. A fault DURING a confirmed show is FAULT_RAISED: it sets the flag
    and the show keeps running, because a show that loses MadMapper midway
    free runs to its end (bench tests 3 and 5)."""
    s = m.slot(m.running)
    if ev.show and ev.show != m.running:
        return _refuse(m, ev, now, f"The report is about show {ev.show}, but "
                                   f"show {m.running} is the one running.")
    if s.confirmed_at is not None:
        return _refuse(m, ev, now,
                       f"Show {s.n} has already started. A problem during a "
                       f"show is reported as a fault and the show keeps "
                       f"running.")
    what = ev.detail or "no reason given"
    return _show_stopped(
        m, ev, now, FAULT, f"FAULT ({what})",
        f"Show {m.running} did not start: {what}. Reported by {ev.actor}. "
        f"Slot marked FAULT; flame cues zeroed, MadMapper stopped, pixels "
        f"faded. The next show is still attempted.",
        _abort_effects(m.running))


def _fault_raised(m, ev, now):
    tx = _Tx(m, ev, now)
    what = ev.detail or f"{ev.actor} reported a fault with no detail"
    tx.m = replace(tx.m, faults=tx.m.faults + (what,))
    during = (f" Show {m.running} keeps running." if m.state == SHOW else "")
    tx.note(FAULT_RAISED, "flagged", what,
            f"Fault from {ev.actor}: {what}.{during} The schedule carries "
            f"on.")
    return tx.done()


def _clear_fault(m, ev, now):
    tx = _Tx(m, ev, now)
    tx.m = replace(tx.m, faults=())
    tx.note(CLEAR_FAULT, "done", "fault acknowledged",
            f"{_operator_name(ev) if ev.actor == 'operator' else 'The system'}"
            f" cleared the fault flag{_screen(ev)}.")
    return tx.done()


def _closing_done(m, ev, now):
    tx = _Tx(m, ev, now)
    _enter(tx, OFF)
    tx.note(CLOSING_DONE, "done", "the rig is dark",
            "Closing finished. The scheduler is off until tomorrow.")
    return tx.done()


def _start_now(m, ev, now):
    left = _guard_left(m, now)
    if left > 0:
        return _refuse(m, ev, now,
                       f"The last show ended {fmt_span((now - m.last_end) / ONE_SECOND)} "
                       f"ago and the guard after a show is {m.guard_s} s, so "
                       f"Start now works again in {fmt_span(-(-left // 1))}.")
    tx = _Tx(m, ev, now)
    n = max((s.n for s in m.slots), default=0) + 1
    tx.m = replace(tx.m, slots=tx.m.slots + (
        Slot(n=n, start=now, origin="operator"),))
    _fire(tx, tx.m.slot(n), operator=True)
    return tx.done()


def _hold(m, ev, now):
    tx = _Tx(m, ev, now)
    if m.state == SHOW:
        tx.m = replace(tx.m, hold_pending=True)
        tx.note(HOLD_ON, "done", "hold after this show",
                f"{_operator_name(ev)} pressed Hold{_screen(ev)} during show "
                f"{m.running}. The show carries on; no further show starts "
                f"until Resume.")
        return tx.done()
    tx.m = replace(tx.m, held_from=m.state)
    _enter(tx, HOLD)
    tx.note(HOLD_ON, "done", "schedule suspended",
            f"{_operator_name(ev)} pressed Hold{_screen(ev)}. No show starts "
            f"until Resume.")
    return tx.done()


def _resume(m, ev, now):
    tx = _Tx(m, ev, now)
    if m.state == SHOW:
        tx.m = replace(tx.m, hold_pending=False)
        tx.note(RESUME, "done", "hold cancelled",
                f"{_operator_name(ev)} pressed Resume{_screen(ev)} during "
                f"show {m.running}. The schedule carries on after it.")
        return tx.done()
    back = m.held_from if m.held_from in (IDLE, STANDBY) else STANDBY
    if back == IDLE and any(s.status != PENDING for s in m.slots):
        back = STANDBY
    tx.m = replace(tx.m, held_from="")
    _enter(tx, back)
    nxt = tx.m.next_slot()
    tx.note(RESUME, "done", "schedule resumed",
            f"{_operator_name(ev)} pressed Resume{_screen(ev)}."
            + (f" Next is show {nxt.n} at {clock(_local(m, nxt.start))}."
               if nxt else " There are no more shows tonight."))
    return tx.done()


def _need_next(m, ev, now):
    nxt = m.next_slot()
    if nxt is None:
        return None, _refuse(m, ev, now, "There is no show still to come "
                                         "tonight.")
    return nxt, None


def _skip_next(m, ev, now):
    nxt, bad = _need_next(m, ev, now)
    if bad:
        return bad
    tx = _Tx(m, ev, now)
    tx.set_slot(nxt.n, status=SKIPPED, reason="SKIPPED (operator)")
    tx.note(SKIP_NEXT, "done", "SKIPPED (operator)",
            f"{_operator_name(ev)} pressed Skip next{_screen(ev)}. Show "
            f"{nxt.n} at {clock(_local(m, nxt.start))} will not run.",
            show=nxt.n)
    return tx.done()


def _spacing_problem(m, slots, moved, now):
    """A sentence if any show this edit touched would start too close to its
    neighbour, or in the past; None if the night still works. Only pairs that
    involve a moved show are judged, so an edit is never refused over a
    problem it did not make."""
    need = timedelta(seconds=m.show_len_s + m.guard_s)
    guard = timedelta(seconds=m.guard_s)
    length = timedelta(seconds=m.show_len_s)
    for s in slots:
        if s.n in moved and s.start <= now:
            return (f"Show {s.n} would be at {clock(_local(m, s.start))}, "
                    f"which has already passed.")
        if s.n in moved and s.start + length > midnight(m):
            return (f"Show {s.n} would start at {clock(_local(m, s.start))} "
                    f"and finish at {clock(_local(m, s.start + length))}, "
                    f"after midnight. Tonight's list cannot run into "
                    f"tomorrow; Start now still works.")
    seq = sorted((s for s in slots if s.status in (PENDING, RUNNING)),
                 key=lambda s: (s.start if s.status == PENDING
                                else s.fired_at, s.n))
    for a, b in zip(seq, seq[1:]):
        if a.n not in moved and b.n not in moved:
            continue
        a_start = a.fired_at if a.status == RUNNING else a.start
        if b.start < a_start + need:
            return (f"Show {b.n} at {clock(_local(m, b.start))} would start "
                    f"before show {a.n} has finished and its "
                    f"{m.guard_s} s guard has run out. It can be no earlier "
                    f"than {clock(_local(m, a_start + need))}.")
    if m.last_end is not None and not m.running:
        first = next((s for s in seq if s.status == PENDING), None)
        if first is not None and first.n in moved \
                and first.start < m.last_end + guard:
            return (f"Show {first.n} at {clock(_local(m, first.start))} "
                    f"would start inside the {m.guard_s} s guard after the "
                    f"last show.")
    return None


def _note_late(tx, m_before):
    """Journal every show an edit has just pushed past last_end. Allowed, and
    never silent."""
    was = {s.n for s in m_before.pending() if runs_late(m_before, s)}
    for s in tx.m.pending():
        if runs_late(tx.m, s) and s.n not in was:
            end = s.start + timedelta(seconds=tx.m.show_len_s)
            tx.note(tx.ev.kind, "runs late",
                    f"RUNS LATE (past last_end {tx.m.hm(tx.m.night_end)})",
                    f"Show {s.n} at {tx.m.hm(s.start)} now finishes at "
                    f"{tx.m.hm(end)}, and with its {tx.m.guard_s} s guard "
                    f"that is after tonight's last_end of "
                    f"{tx.m.hm(tx.m.night_end)}. Allowed; the night runs "
                    f"late.", show=s.n)


def _shift(m, slots, which, minutes):
    d = timedelta(minutes=minutes)
    return tuple(replace(s, start=s.start + d,
                         planned=s.planned or s.start)
                 if s.n in which else s for s in slots)


def _delay(m, ev, now, rest):
    if ev.minutes not in DELAY_CHOICES:
        return _refuse(m, ev, now, f"A delay is +5 or +10 minutes, not "
                                   f"{ev.minutes!r}.")
    nxt, bad = _need_next(m, ev, now)
    if bad:
        return bad
    which = {s.n for s in m.pending()} if rest else {nxt.n}
    slots = _shift(m, m.slots, which, ev.minutes)
    why = _spacing_problem(m, slots, which, now)
    if why:
        if not rest:
            why += (" Delay the rest of the night moves every show after it "
                    "as well.")
        return _refuse(m, ev, now, why)
    tx = _Tx(m, ev, now)
    tx.m = replace(tx.m, slots=slots)
    reason = f"DELAYED (operator, +{ev.minutes} min)"
    for n in sorted(which):
        tx.note(ev.kind, "done", reason,
                f"Show {n} moved from "
                f"{clock(_local(m, m.slot(n).start))} to "
                f"{clock(_local(m, tx.m.slot(n).start))}.", show=n)
    head = ("Delay the rest of the night" if rest else "Delay next")
    tx.note(ev.kind, "done", reason,
            f"{_operator_name(ev)} pressed {head} +{ev.minutes}"
            f"{_screen(ev)}. {len(which)} show(s) moved later.",
            show=0 if rest else nxt.n)
    _note_late(tx, m)
    return tx.done()


def _abort(m, ev, now):
    n = m.running
    return _show_stopped(
        m, ev, now, ABORTED, "ABORTED (operator)",
        f"{_operator_name(ev)} pressed Abort{_screen(ev)} during show {n}. "
        f"Flame cues zeroed, MadMapper stopped, pixels fading to black over "
        f"{ABORT_FADE_S:g} s. Nothing was disarmed.",
        _abort_effects(n), abort=True)


def _end_night(m, ev, now):
    tx = _Tx(m, ev, now)
    skipped = []
    for s in m.pending():
        tx.set_slot(s.n, status=SKIPPED, reason="SKIPPED (operator, End night)")
        skipped.append(s.n)
    tx.m = replace(tx.m, held_from="", hold_pending=False)
    _enter(tx, CLOSING)
    for n in skipped:
        tx.note(END_NIGHT, "skipped", "SKIPPED (operator, End night)",
                f"Show {n} will not run: the night was ended.", show=n)
    tx.note(END_NIGHT, "done", "night ended by the operator",
            f"{_operator_name(ev)} pressed End night{_screen(ev)}. "
            f"{len(skipped)} show(s) skipped. Closing: flame cues to zero, "
            f"MadMapper stopped, pixels faded, blackout.")
    return tx.done()


def _edit_time(m, ev):
    t = parse_time(ev.at, "The new time")
    return _utc(_wall(m.date, t, m.tz))


def _edit_move(m, ev, now):
    s = m.slot(ev.show)
    if s is None:
        return _refuse(m, ev, now, f"There is no show {ev.show} tonight.")
    if s.status != PENDING:
        return _refuse(m, ev, now, f"Show {s.n} is {s.status} and can no "
                                   f"longer be moved.")
    try:
        when = _edit_time(m, ev)
    except ValueError as e:
        return _refuse(m, ev, now, str(e))
    slots = tuple(replace(x, start=when, planned=x.planned or x.start)
                  if x.n == s.n else x for x in m.slots)
    why = _spacing_problem(m, slots, {s.n}, now)
    if why:
        return _refuse(m, ev, now, why)
    tx = _Tx(m, ev, now)
    tx.m = replace(tx.m, slots=slots)
    reason = (f"MOVED (operator, {clock(_local(m, s.start))} to "
              f"{clock(_local(m, when))})")
    tx.note(EDIT_MOVE, "done", reason,
            f"{_operator_name(ev)} moved show {s.n} from "
            f"{clock(_local(m, s.start))} to {clock(_local(m, when))}"
            f"{_screen(ev)}, for tonight only.", show=s.n)
    _note_late(tx, m)
    return tx.done()


def _edit_add(m, ev, now):
    try:
        when = _edit_time(m, ev)
    except ValueError as e:
        return _refuse(m, ev, now, str(e))
    n = max((s.n for s in m.slots), default=0) + 1
    slots = m.slots + (Slot(n=n, start=when, origin="edit"),)
    why = _spacing_problem(m, slots, {n}, now)
    if why:
        return _refuse(m, ev, now, why)
    tx = _Tx(m, ev, now)
    tx.m = replace(tx.m, slots=slots)
    tx.note(EDIT_ADD, "done", "ADDED (operator)",
            f"{_operator_name(ev)} added show {n} at "
            f"{clock(_local(m, when))}{_screen(ev)}, for tonight only.",
            show=n)
    _note_late(tx, m)
    return tx.done()


def _edit_remove(m, ev, now):
    s = m.slot(ev.show)
    if s is None:
        return _refuse(m, ev, now, f"There is no show {ev.show} tonight.")
    if s.status != PENDING:
        return _refuse(m, ev, now, f"Show {s.n} is {s.status} and can no "
                                   f"longer be taken off tonight's list.")
    tx = _Tx(m, ev, now)
    reason = "SKIPPED (operator, removed from tonight)"
    tx.set_slot(s.n, status=SKIPPED, reason=reason)
    tx.note(EDIT_REMOVE, "done", reason,
            f"{_operator_name(ev)} took show {s.n} at "
            f"{clock(_local(m, s.start))} off tonight's list{_screen(ev)}.",
            show=s.n)
    return tx.done()


_ALL = frozenset(STATES)
_LIVE = frozenset((IDLE, STANDBY, HOLD, SHOW))

# Which states accept which event. Anything else is refused with a sentence
# from _why_not. The selftest checks every state against every event with
# its own table, written out by hand, so this and that have to agree.
ALLOWED = {
    BOOT_DONE: frozenset((BOOT,)),
    TICK: _ALL - {BOOT},
    SHOW_CONFIRMED: frozenset((SHOW,)),
    SHOW_ENDED: frozenset((SHOW,)),
    SHOW_FAILED: frozenset((SHOW,)),
    FAULT_RAISED: _ALL,
    CLEAR_FAULT: _ALL,
    CLOSING_DONE: frozenset((CLOSING,)),
    START_NOW: frozenset((IDLE, STANDBY, HOLD, CLOSING, OFF)),
    HOLD_ON: frozenset((IDLE, STANDBY, SHOW)),
    RESUME: frozenset((HOLD, SHOW)),
    SKIP_NEXT: _LIVE,
    DELAY_NEXT: _LIVE,
    DELAY_REST: _LIVE,
    ABORT: frozenset((SHOW,)),
    END_NIGHT: frozenset((IDLE, STANDBY, HOLD)),
    EDIT_MOVE: _LIVE,
    EDIT_ADD: _LIVE,
    EDIT_REMOVE: _LIVE,
}

_HANDLERS = {
    BOOT_DONE: _boot_done, TICK: _tick, SHOW_CONFIRMED: _show_confirmed,
    SHOW_ENDED: _show_ended,
    SHOW_FAILED: _show_failed, FAULT_RAISED: _fault_raised,
    CLEAR_FAULT: _clear_fault, CLOSING_DONE: _closing_done,
    START_NOW: _start_now, HOLD_ON: _hold, RESUME: _resume,
    SKIP_NEXT: _skip_next, DELAY_NEXT: lambda m, e, n: _delay(m, e, n, False),
    DELAY_REST: lambda m, e, n: _delay(m, e, n, True), ABORT: _abort,
    END_NIGHT: _end_night, EDIT_MOVE: _edit_move, EDIT_ADD: _edit_add,
    EDIT_REMOVE: _edit_remove,
}


def _why_not(m, ev):
    """The sentence for an event the current state does not take."""
    k, st = ev.kind, m.state
    if st == BOOT:
        return ("The scheduler is still starting up and has not worked out "
                "tonight yet. Try again in a moment.")
    if k == BOOT_DONE:
        return "The scheduler has already started."
    if k in (SHOW_CONFIRMED, SHOW_ENDED, SHOW_FAILED, ABORT):
        return f"No show is running; the scheduler is in {st}."
    if k == CLOSING_DONE:
        return f"The scheduler is not closing; it is in {st}."
    if k == START_NOW:
        return (f"Show {m.running} is running, and a second show never "
                f"starts over it.")
    if k == HOLD_ON:
        if st == HOLD:
            return "The schedule is already on hold."
        return "The night is over, so there is nothing to hold."
    if k == RESUME:
        return "The schedule is not on hold."
    if k == END_NIGHT:
        if st == SHOW:
            return (f"Show {m.running} is running. Abort it first, then End "
                    f"night.")
        return "The night has already been ended."
    if k in (SKIP_NEXT, DELAY_NEXT, DELAY_REST, EDIT_MOVE, EDIT_ADD,
             EDIT_REMOVE):
        return ("The night is over. Tonight's list can no longer change; "
                "Start now still works.")
    return f"{k} does not apply in {st}."   # pragma: no cover


def step(m, ev, now):
    """The whole state machine: (machine, event, now) in, Outcome out.

    Nothing is performed. The Outcome carries the new machine, the effects a
    performer should carry out in order, and the log events to write. An
    event the state does not take comes back refused, with a sentence and a
    log line, and the machine unchanged."""
    _check_event(ev)
    now = _utc(_aware(now))
    if ev.actor == "operator" and not (ev.who.strip() and ev.screen.strip()):
        missing = " and ".join(x for x, v in (("who pressed it", ev.who),
                                             ("which screen it came from",
                                              ev.screen)) if not v.strip())
        return _refuse(m, ev, now,
                       f"The {_verb(ev)} does not say {missing}. Every "
                       f"operator "
                       f"action names the operator and the screen, so the "
                       f"night journal can say who did what. Nothing was "
                       f"changed.")
    if m.state not in ALLOWED[ev.kind]:
        return _refuse(m, ev, now, _why_not(m, ev))
    if ev.kind in (ABORT, END_NIGHT) and not ev.confirmed:
        return _refuse(m, ev, now, f"{_verb(ev)} has to be confirmed first. "
                                   f"Nothing was changed.")
    if ev.kind == CLEAR_FAULT and not m.faults:
        return _refuse(m, ev, now, "There is no fault to clear.")
    if ev.kind == HOLD_ON and m.state == SHOW and m.hold_pending:
        return _refuse(m, ev, now, "Hold is already set for after this show.")
    if ev.kind == RESUME and m.state == SHOW and not m.hold_pending:
        return _refuse(m, ev, now, "Hold was not pressed, so there is "
                                   "nothing to resume.")
    return _HANDLERS[ev.kind](m, ev, now)


# ------------------------------------------------ tonight, as data --

TONIGHT_FORMAT = 1


def _iso(dt):
    return _utc(dt).isoformat() if dt is not None else None


def machine_to_doc(m):
    """Tonight's machine as JSON, so a restart can pick it up. Pure."""
    return {
        "format": TONIGHT_FORMAT, "date": m.date.isoformat(),
        "timezone": getattr(m.tz, "key", str(m.tz)), "state": m.state,
        "show_len_s": m.show_len_s, "guard_s": m.guard_s,
        "late_grace_s": m.late_grace_s, "running": m.running,
        "last_end": _iso(m.last_end), "hold_pending": m.hold_pending,
        "held_from": m.held_from, "faults": list(m.faults),
        "shows_started": m.shows_started, "night_end": _iso(m.night_end),
        "slots": [{"n": s.n, "start": _iso(s.start), "status": s.status,
                   "reason": s.reason, "origin": s.origin,
                   "planned": _iso(s.planned), "fired_at": _iso(s.fired_at),
                   "confirmed_at": _iso(s.confirmed_at),
                   "ended_at": _iso(s.ended_at)} for s in m.slots],
    }


def machine_from_doc(doc):
    """The saved machine, back in BOOT with `resumed_from` set, so BOOT_DONE
    runs the late rule over it exactly as it does over a fresh night. Raises
    ValueError with a sentence when the document is not one of ours."""
    def when(v, what):
        if v is None:
            return None
        try:
            dt = datetime.fromisoformat(v)
        except (TypeError, ValueError):
            raise ValueError(f"{what} is not a time: {v!r}.")
        if dt.tzinfo is None:
            raise ValueError(f"{what} has no time zone.")
        return _utc(dt)

    if not isinstance(doc, dict) or doc.get("format") != TONIGHT_FORMAT:
        raise ValueError("It is not a saved night this version can read.")
    try:
        slots = []
        for x in doc["slots"]:
            if x["status"] not in (PENDING, RUNNING) + tuple(FINAL):
                raise ValueError(f"show {x['n']} has status "
                                 f"{x['status']!r}.")
            slots.append(Slot(
                n=int(x["n"]), start=when(x["start"], "a start"),
                status=x["status"], reason=str(x["reason"]),
                origin=str(x["origin"]),
                planned=when(x["planned"], "a planned time"),
                fired_at=when(x["fired_at"], "a start time"),
                confirmed_at=when(x["confirmed_at"], "a confirm time"),
                ended_at=when(x["ended_at"], "an end time")))
        state = doc["state"]
        if state not in STATES:
            raise ValueError(f"{state!r} is not a state.")
        m = Machine(
            date=date.fromisoformat(doc["date"]), tz=zone(doc["timezone"]),
            show_len_s=int(doc["show_len_s"]), guard_s=int(doc["guard_s"]),
            late_grace_s=int(doc["late_grace_s"]), state=BOOT,
            slots=tuple(slots), running=int(doc["running"]),
            last_end=when(doc["last_end"], "the last show's end"),
            hold_pending=bool(doc["hold_pending"]),
            held_from=str(doc["held_from"]),
            faults=tuple(str(f) for f in doc["faults"]),
            shows_started=int(doc["shows_started"]),
            night_end=when(doc["night_end"], "last_end"),
            resumed_from=state)
    except (KeyError, TypeError) as e:
        raise ValueError(f"It is missing {e}.")
    if m.running and (m.slot(m.running) is None
                      or m.slot(m.running).status != RUNNING):
        raise ValueError(f"It says show {m.running} is running, but that "
                         f"show is not on its list as running.")
    return m


# --------------------------------------------------- operator actions --

# The transport panel, as data. The GUI draws from this; `confirm` is the
# question to ask before sending, or None for actions that go straight out.
ACTIONS = (
    {"id": "start_now", "label": "Start now", "event": START_NOW,
     "minutes": 0, "confirm": None},
    {"id": "hold", "label": "Hold", "event": HOLD_ON, "minutes": 0,
     "confirm": None},
    {"id": "resume", "label": "Resume", "event": RESUME, "minutes": 0,
     "confirm": None},
    {"id": "skip_next", "label": "Skip next", "event": SKIP_NEXT,
     "minutes": 0, "confirm": None},
    {"id": "delay_next_5", "label": "Delay next +5", "event": DELAY_NEXT,
     "minutes": 5, "confirm": None},
    {"id": "delay_next_10", "label": "Delay next +10", "event": DELAY_NEXT,
     "minutes": 10, "confirm": None},
    {"id": "delay_rest_5", "label": "Delay the rest of the night +5",
     "event": DELAY_REST, "minutes": 5, "confirm": None},
    {"id": "delay_rest_10", "label": "Delay the rest of the night +10",
     "event": DELAY_REST, "minutes": 10, "confirm": None},
    {"id": "abort", "label": "Abort show", "event": ABORT, "minutes": 0,
     "confirm": ("Abort show {show}? MadMapper stops, the pixels fade to "
                 "black and every flame cue goes to zero. This does not "
                 "disarm the flames; the E-stop and the Stream Deck do "
                 "that.")},
    {"id": "end_night", "label": "End night", "event": END_NIGHT,
     "minutes": 0,
     "confirm": ("End the night? Every show still to come tonight is "
                 "skipped, and the rig fades out and goes dark.")},
)


def action_event(action_id, confirmed=False, screen="", who=""):
    for a in ACTIONS:
        if a["id"] == action_id:
            return Event(a["event"], "operator", minutes=a["minutes"],
                         confirmed=bool(confirmed), screen=screen, who=who)
    raise ValueError(f"{action_id!r} is not an operator action. They are: "
                     f"{', '.join(a['id'] for a in ACTIONS)}.")


def actions_for(m):
    """The transport panel for the machine as it stands: each action, whether
    the state takes it, and the confirm question with the show filled in."""
    out = []
    for a in ACTIONS:
        # Start now inside the guard is time dependent: the button stays
        # live and the press itself says how long is left.
        ok = m.state in ALLOWED[a["event"]]
        if a["event"] in (SKIP_NEXT, DELAY_NEXT, DELAY_REST) and \
                m.next_slot() is None:
            ok = False
        q = a["confirm"]
        out.append({"id": a["id"], "label": a["label"], "allowed": ok,
                    "confirm": q.format(show=m.running) if q else None})
    return out


# ---------------------------------------------------------------- views --

def slot_view(m, now=None):
    """Tonight's list for the page, in start order, with NEXT picked out."""
    nxt = m.next_slot()
    now = _utc(now) if now is not None else None
    out = []
    for s in sorted(m.slots, key=lambda s: (s.start, s.n)):
        status = NEXT if (nxt is not None and s.n == nxt.n) else s.status
        row = {"show": s.n, "start": clock(_local(m, s.start)),
               "start_iso": _local(m, s.start).isoformat(timespec="seconds"),
               "status": status, "reason": s.reason, "origin": s.origin,
               "planned": (clock(_local(m, s.planned))
                           if s.planned is not None else None),
               "past_last_end": runs_late(m, s)}
        if now is not None and s.status == PENDING:
            row["in_s"] = int((s.start - now) // ONE_SECOND)
        out.append(row)
    return out


def machine_view(m, now=None):
    nxt = m.next_slot()
    now = _utc(now) if now is not None else None
    return {
        "date": m.date.isoformat(), "state": m.state, "fault": m.fault,
        "faults": list(m.faults), "running": m.running or None,
        "hold_pending": m.hold_pending,
        "runs_late": bool(late_warnings(m)),
        "warnings": late_warnings(m),
        "last_end": m.hm(m.night_end) if m.night_end else None,
        "next": ({"show": nxt.n, "start": clock(_local(m, nxt.start)),
                  "in_s": (int((nxt.start - now) // ONE_SECOND)
                           if now is not None else None)} if nxt else None),
        "guard_left_s": (int(-(-_guard_left(m, now) // 1))
                         if now is not None else None),
        "actions": actions_for(m),
    }


# ------------------------------------------------------------ the clock --

CLOCK_WARN_S = 2.0


def judge_clock_offset(offset_s, server=""):
    """(level, sentence) for an NTP offset. The clock is never set here or
    anywhere else; a bad clock is said out loud and left alone."""
    src = f" from {server}" if server else ""
    if offset_s is None:
        return ("unknown", f"The clock could not be checked against a time "
                           f"server{src}, so it is running unchecked. Shows "
                           f"start by this machine's clock.")
    ahead = "ahead of" if offset_s < 0 else "behind"
    size = abs(offset_s)
    if size > CLOCK_WARN_S:
        return ("warn", f"This machine's clock is {size:.1f} s {ahead} the "
                        f"time server{src}. Shows start by this clock, so "
                        f"they will be {size:.1f} s off. It was not changed; "
                        f"fix the time setting on this machine.")
    return ("ok", f"This machine's clock is within {size:.2f} s of the time "
                  f"server{src}.")
