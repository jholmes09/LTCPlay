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

Contract for PR 3, the code that performs the effects
------------------------------------------------------
Whatever drives this engine and carries out its effects must:

1. Save tonight BEFORE performing anything. After every step whose machine
   changed, write machine_to_doc() to tonight's file (the service does this
   in _apply) and only then perform the Outcome's effects. A crash between
   performing START_SHOW and saving would otherwise restore the slot as
   still to come and, inside the grace window, start the same show twice.
   If the save fails, say so in the journal and still perform the effects:
   a show that runs beats a show that silently does not.
2. Perform the effects in the order given. ZERO_FLAME_CUES is immediate.
   FADE_PIXELS carries its length; an INTERMISSION listed after a fade
   starts when the fade has finished, never over it.
   Pause (Hold during a show), in this order: ZERO_FLAME_CUES and
   BLANK_LASERS at once (a real blank command to BEYOND, not only frozen
   timecode: a frozen laser cue is a static beam), then FREEZE_SHOW (pixels,
   video and timecode hold the current frame, and timecode keeps SENDING
   that frozen frame so receivers hold instead of timing out), then
   FADE_MUSIC_OUT. Resume: RESUME_SHOW carries on from the frozen frame,
   FADE_MUSIC_IN, and UNBLANK_LASERS only once timecode is moving again.
   Lasers blanked by a pause stay blanked until a RESUME_SHOW or the next
   START_SHOW. Arming is never touched by any of it.
3. Never perform anything for a refused Outcome (Outcome.refused is set).
4. Send SHOW_CONFIRMED as soon as timecode is seen advancing after a
   START_SHOW. Until then the show counts as not yet started.
5. Send SHOW_FAILED only for a show that did not start: never confirmed,
   and within CONFIRM_WINDOW_S of the start. A later SHOW_FAILED is not a
   show failure and is recorded as a fault with the show left running.
6. Never send SHOW_FAILED for a fault during a show. Send FAULT_RAISED,
   which sets the FAULT flag and leaves the show running: a show that loses
   MadMapper midway free runs to its end.
7. Send SHOW_ENDED, with the show number, when a show finishes.
8. Send CLOSING_DONE when the closing effects have finished.
9. Send BOOT_DONE once, after restoring tonight, then TICK at least twice
   a second from the OS wall clock with a zone attached. The late rule
   counts whole seconds, so a tick has to land inside every second.
10. Send operator events only with `who` and `screen` filled in, and send
    Abort and End night with confirmed=True only after the operator has
    answered the confirm question from ACTIONS.
11. Never read flame arm state for the scheduler's sake, and never set the
    OS clock.
"""
import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone

try:
    import zoneinfo
except ImportError:                       # pragma: no cover, 3.9+ has it
    zoneinfo = None


# ------------------------------------------------------------ vocabulary --

BOOT, IDLE, STANDBY, SHOW, PAUSED, CLOSING, OFF, HOLD = (
    "BOOT", "IDLE", "STANDBY", "SHOW", "PAUSED", "CLOSING", "OFF", "HOLD")
STATES = (BOOT, IDLE, STANDBY, SHOW, PAUSED, CLOSING, OFF, HOLD)

ACTORS = ("scheduler", "operator", "madmapper", "safety", "reader", "system")

# Slot statuses. PENDING is a show still to come; the view calls the first
# of those NEXT. FIRED is a transition reason, never a resting status.
PENDING, RUNNING, DONE, MISSED, SKIPPED, ABORTED, FAULT = (
    "PENDING", "RUNNING", "DONE", "MISSED", "SKIPPED", "ABORTED", "FAULT")
NEXT = "NEXT"
# A show whose time passed during a Hold. It waits, never starts by itself,
# and starts only when the operator presses Start now.
DELAYED = "DELAYED"
STATUSES = (DONE, RUNNING, NEXT, PENDING, DELAYED, MISSED, SKIPPED, ABORTED,
            FAULT)
FINAL = frozenset((DONE, MISSED, SKIPPED, ABORTED, FAULT))

# Effects. Somebody else performs these; this module only names them.
PRESHOW_LOOK = "PRESHOW_LOOK"        # IDLE: the preshow look, no loop
INTERMISSION = "INTERMISSION"        # STANDBY: the intermission timeline
START_SHOW = "START_SHOW"            # select the bank, play from beginning
STOP_CONDUCTOR = "STOP_CONDUCTOR"    # MadMapper conductor stop
FADE_PIXELS = "FADE_PIXELS"          # pixels to black over `seconds`
ZERO_FLAME_CUES = "ZERO_FLAME_CUES"  # zero on every flame cue channel, now
BLACKOUT = "BLACKOUT"                # everything dark
# Hold during a show pauses it in place (Jeff, 2026-09-23).
FREEZE_SHOW = "FREEZE_SHOW"          # pixels, video and timecode hold the
                                     # current frame; timecode keeps sending
                                     # the frozen frame
FADE_MUSIC_OUT = "FADE_MUSIC_OUT"    # show music fades out
BLANK_LASERS = "BLANK_LASERS"        # a real blank command to BEYOND
RESUME_SHOW = "RESUME_SHOW"          # carry on from the frozen frame
FADE_MUSIC_IN = "FADE_MUSIC_IN"      # show music fades back up
UNBLANK_LASERS = "UNBLANK_LASERS"    # once timecode is moving again
EFFECTS = (PRESHOW_LOOK, INTERMISSION, START_SHOW, STOP_CONDUCTOR,
           FREEZE_SHOW, FADE_MUSIC_OUT, BLANK_LASERS, RESUME_SHOW,
           FADE_MUSIC_IN, UNBLANK_LASERS,
           FADE_PIXELS, ZERO_FLAME_CUES, BLACKOUT)

# Abort, precisely, per the handoff. Flames first because the handoff says
# "immediately" for them and they are the one that matters for safety; the
# performer is free to run the three at once. Aborting never disarms.
ABORT_FADE_S = 1.0
CLOSING_FADE_S = 1.0

# OPEN QUESTION FOR JEFF, and the one place it is decided. After a show is
# stopped early (Abort, a start that failed, a restart during a show) the
# machine lands in STANDBY, which section 5 defines as "intermission
# running", while Abort itself says stop the conductor and fade to black.
# True: after the fade the intermission starts again. False: the rig stays
# dark until the next show ends or the operator resumes.
INTERMISSION_AFTER_A_STOPPED_SHOW = True

# How long after START_SHOW a show may still be reported as not having
# started. Past this, a SHOW_FAILED is recorded as a fault and the show
# keeps running: six minutes into a show is not a failed start.
CONFIRM_WINDOW_S = 10

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

# Who may press things, until the operators file says otherwise.
DEFAULT_OPERATORS = ("Andy", "Jeff")


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
    paused_s: float = 0.0    # time spent paused, which moves its end later


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
    paused_at: object = None     # when the running show was paused
    held_from: str = ""          # where Resume goes back to
    faults: tuple = ()           # FAULT is a flag: these are its sentences
    shows_started: int = 0
    night_end: object = None     # tonight's last_end, UTC; None when dark
    resumed_from: str = ""       # the state saved before a restart
    rule_id: str = ""            # which rule tonight was built from
    operators: tuple = DEFAULT_OPERATORS   # who may press things

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

    def delayed(self):
        """The show waiting after a Hold, or None. There is never more than
        one."""
        for s in self.slots:
            if s.status == DELAYED:
                return s
        return None

    def waiting(self):
        """Is any show still to come, scheduled or delayed?"""
        return self.next_slot() is not None or self.delayed() is not None

    def expected_end(self, now=None):
        """When the running show should finish: its start, its length, and
        every second it has spent paused (still counting while paused)."""
        s = self.slot(self.running)
        if s is None:
            return None
        paused = s.paused_s
        if self.paused_at is not None and now is not None:
            paused += (_utc(now) - self.paused_at) / ONE_SECOND
        return s.fired_at + timedelta(seconds=self.show_len_s + paused)

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
                   slots=slots, night_end=end, rule_id=rule_fingerprint(rule))


def rule_fingerprint(rule):
    """What the rule says, as a short hash. The version number is left out,
    so a hand edit that forgot to bump it still counts as a change, and a
    save that changed nothing does not."""
    doc = rule_to_doc(rule)
    doc.pop("version", None)
    text = json.dumps(doc, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


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

    def note_actor(self):
        return "operator" if self.ev.actor == "operator" else "scheduler"

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
def entry_effects(state, show=0, after_stop=False, resume=False):
    if state == IDLE:
        return [Effect(PRESHOW_LOOK)]
    if state == PAUSED:
        # Flames and lasers first, because they are the ones that must not
        # linger; then the freeze, then the music.
        return [Effect(ZERO_FLAME_CUES, show=show),
                Effect(BLANK_LASERS, show=show),
                Effect(FREEZE_SHOW, show=show),
                Effect(FADE_MUSIC_OUT, show=show)]
    if state == SHOW and resume:
        return [Effect(RESUME_SHOW, show=show), Effect(FADE_MUSIC_IN, show=show),
                Effect(UNBLANK_LASERS, show=show)]
    if state in (STANDBY, HOLD):
        if after_stop and not INTERMISSION_AFTER_A_STOPPED_SHOW:
            return []
        return [Effect(INTERMISSION)]
    if state == SHOW:
        return [Effect(START_SHOW, show=show)]
    if state == CLOSING:
        return _closing_effects()
    return []


def _enter(tx, state, show=0, after_stop=False, resume=False):
    """Change state and add the effects that belong to arriving there."""
    tx._set_state(state)
    tx.effects.extend(entry_effects(state, show, after_stop, resume))


def _after_show(tx, abort=False, stopped=False):
    """Where a show that has just finished, or been stopped, leaves the
    night. `stopped` is a show cut short (Abort, a failed start)."""
    m = tx.m
    if abort or m.waiting():
        # Abort stays in STANDBY even when no show is left; the next tick
        # closes the night if so.
        _enter(tx, STANDBY, after_stop=stopped)
    else:
        _enter(tx, CLOSING)


# Start now's three reasons (Jeff, 2026-09-23).
DELAYED_START = "DELAYED START (operator hold)"
STARTED_EARLY = "STARTED EARLY (operator)"
EXTRA_SHOW = "EXTRA SHOW (operator)"


def _fire(tx, s, reason="FIRED"):
    """Start one show. `reason` is FIRED for the schedule, or one of Start
    now's three reasons."""
    now = tx.when
    operator = reason != "FIRED"
    tx.set_slot(s.n, status=RUNNING, reason=reason, fired_at=now)
    tx.m = replace(tx.m, running=s.n, shows_started=tx.m.shows_started + 1,
                   held_from="", paused_at=None)
    _enter(tx, SHOW, show=s.n)
    if operator:
        what = {DELAYED_START: f"the delayed show {s.n}, planned for "
                               f"{clock(_local(tx.m, s.start))}",
                STARTED_EARLY: f"show {s.n} early; it was due at "
                               f"{clock(_local(tx.m, s.start))}",
                EXTRA_SHOW: f"an extra show, {s.n}"}[reason]
        text = (f"{_operator_name(tx.ev)} pressed Start now"
                f"{_screen(tx.ev)}. Started {what}, at "
                f"{clock(_local(tx.m, now))}.")
    else:
        late = lateness_s(s.start, now)
        text = (f"Show {s.n} started on schedule at "
                f"{clock(_local(tx.m, s.start))}"
                + (f", {late} s after its time." if late else "."))
    tx.note(START_NOW if operator else "fire", "fired", reason, text,
            show=s.n, actor="operator" if operator else "scheduler")
    # A show that was waiting after a Hold has now been overtaken.
    d = tx.m.delayed()
    if d is not None and d.n != s.n:
        why = f"MISSED (show {s.n} started on schedule)" if not operator \
            else f"MISSED (show {s.n} was started instead)"
        tx.set_slot(d.n, status=MISSED, reason=why)
        tx.note("miss", "missed", why,
                f"The delayed show {d.n} will not run: show {s.n} started "
                f"instead.", show=d.n, actor=tx.note_actor())


def _screen(ev):
    return f" on the {ev.screen}" if ev.screen else ""


def _miss(tx, s, why):
    tx.set_slot(s.n, status=MISSED, reason=why)
    tx.note("miss", "missed", why,
            f"Show {s.n} at {clock(_local(tx.m, s.start))} did not start: "
            f"{why}. It will not start by itself; Start now is still there.",
            show=s.n, actor="scheduler")


def _hold_back(tx, s):
    """A show's time passed during a Hold: it waits instead of being missed.
    Only the most recent one waits; an earlier one still waiting is MISSED."""
    old = tx.m.delayed()
    if old is not None:
        why = "MISSED (on hold, a later show was delayed)"
        tx.set_slot(old.n, status=MISSED, reason=why)
        tx.note("miss", "missed", why,
                f"Show {old.n} at {clock(_local(tx.m, old.start))} will not "
                f"run: a later show's time has also passed during the Hold, "
                f"and only the most recent one waits.", show=old.n,
                actor="scheduler")
    why = "DELAYED (on hold)"
    tx.set_slot(s.n, status=DELAYED, reason=why)
    tx.note("delay", "delayed", why,
            f"Show {s.n} at {clock(_local(tx.m, s.start))} is delayed by the "
            f"Hold. It waits, and starts only when someone presses Start "
            f"now.", show=s.n, actor="scheduler")


def _sweep(tx, held=None):
    """Walk the pending shows against the clock: mark what has passed,
    fire at most one that is due. Used by BOOT_DONE and TICK. `held` says
    whether the time passed during a Hold (a paused show is a Hold too)."""
    now = tx.when
    if held is None:
        held = tx.m.state in (HOLD, PAUSED)
    for s in tx.m.pending():
        late = lateness_s(s.start, now)
        if late < 0:
            break
        state = tx.m.state
        if late > tx.m.late_grace_s:
            if held:
                _hold_back(tx, s)
                continue
            if state == SHOW:
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
    cut = bool(was and m.running)
    if cut:
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
    # only inside the window. A restored night goes through the same rule;
    # one that was on hold treats the time it was down as more Hold.
    tx.m = replace(tx.m, paused_at=None)
    _sweep(tx, held=(was == HOLD))
    if not tx.m.waiting():
        _enter(tx, CLOSING)
        why = "every show tonight has already passed"
    elif was == HOLD:
        tx.m = replace(tx.m, held_from=STANDBY)
        _enter(tx, HOLD, after_stop=cut)
        why = "it was on hold before the restart"
    elif any(s.status != PENDING for s in tx.m.slots):
        _enter(tx, STANDBY, after_stop=cut)
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
    d = tx.m.delayed()
    if d is not None and now >= midnight(tx.m):
        why = "MISSED (still delayed at midnight)"
        tx.set_slot(d.n, status=MISSED, reason=why)
        tx.note("miss", "missed", why,
                f"The delayed show {d.n} was never started, and the night is "
                f"over.", show=d.n, actor="scheduler")
    _sweep(tx)
    st = tx.m.state
    if st in (IDLE, STANDBY) and not tx.m.waiting():
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
    s = m.slot(n)
    paused = s.paused_s + ((now - m.paused_at) / ONE_SECOND
                           if m.paused_at is not None else 0.0)
    tx.set_slot(n, status=status, reason=reason, ended_at=now,
                paused_s=paused)
    tx.m = replace(tx.m, running=0, last_end=now, paused_at=None)
    if status == FAULT:
        tx.m = replace(tx.m, faults=tx.m.faults + (text,))
    tx.effects.extend(effects)
    _after_show(tx, abort=abort, stopped=bool(effects))
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
    after = lateness_s(s.fired_at, now)
    if after > CONFIRM_WINDOW_S:
        # Too late to be a failed start. Refused as a show failure and
        # recorded as a fault instead: the flag goes up, nothing is stopped.
        tx = _Tx(m, ev, now)
        sentence = (f"A failed start for show {s.n} was reported "
                    f"{fmt_span(after)} after the start, past the "
                    f"{CONFIRM_WINDOW_S} s window, so it is not treated as a "
                    f"failed start. It is recorded as a fault and show "
                    f"{s.n} keeps running.")
        tx.m = replace(tx.m, faults=tx.m.faults + (what,))
        tx.note(SHOW_FAILED, "recorded as a fault",
                f"too late for a failed start: {what}",
                f"{sentence} {ev.actor} said: {what}.", show=s.n)
        return tx.done()
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
    """Start now has no restriction except that it does nothing while a show
    is running or paused (refused in step). It ignores guard_s, and it works
    straight after an Abort. It starts the DELAYED show if there is one,
    otherwise the next show now (using up that slot), otherwise an extra
    show. Jeff, 2026-09-23."""
    tx = _Tx(m, ev, now)
    d, nxt = m.delayed(), m.next_slot()
    if d is not None:
        _fire(tx, d, DELAYED_START)
    elif nxt is not None:
        _fire(tx, nxt, STARTED_EARLY)
    else:
        n = max((s.n for s in m.slots), default=0) + 1
        tx.m = replace(tx.m, slots=tx.m.slots + (
            Slot(n=n, start=now, origin="operator"),))
        _fire(tx, tx.m.slot(n), EXTRA_SHOW)
    return tx.done()


def _hold(m, ev, now):
    tx = _Tx(m, ev, now)
    if m.state == SHOW:
        # Hold during a show pauses it where it is (Jeff, 2026-09-23).
        n = m.running
        tx.m = replace(tx.m, paused_at=now)
        _enter(tx, PAUSED, show=n)
        tx.note(HOLD_ON, "paused", "PAUSED (operator hold)",
                f"{_operator_name(ev)} pressed Hold{_screen(ev)} during show "
                f"{n}. The show is paused at its current frame: flame cues "
                f"zeroed, lasers blanked, music fading out. Resume carries "
                f"on from there.", show=n)
        return tx.done()
    tx.m = replace(tx.m, held_from=m.state)
    _enter(tx, HOLD)
    tx.note(HOLD_ON, "done", "schedule on hold",
            f"{_operator_name(ev)} pressed Hold{_screen(ev)}. No show starts "
            f"by itself until Resume; a show whose time passes meanwhile is "
            f"delayed and waits for Start now.")
    return tx.done()


def _resume(m, ev, now):
    tx = _Tx(m, ev, now)
    if m.state == PAUSED:
        n = m.running
        paused = (now - m.paused_at) / ONE_SECOND
        tx.set_slot(n, paused_s=m.slot(n).paused_s + paused)
        tx.m = replace(tx.m, paused_at=None)
        _enter(tx, SHOW, show=n, resume=True)
        tx.note(RESUME, "resumed", f"RESUMED (paused {fmt_span(paused)})",
                f"{_operator_name(ev)} pressed Resume{_screen(ev)}. Show {n} "
                f"carries on from where it froze after {fmt_span(paused)} "
                f"paused; it now ends at "
                f"{clock(_local(tx.m, tx.m.expected_end()))}.", show=n)
        return tx.done()
    back = m.held_from if m.held_from in (IDLE, STANDBY) else STANDBY
    if back == IDLE and any(s.status != PENDING for s in m.slots):
        back = STANDBY
    tx.m = replace(tx.m, held_from="")
    _enter(tx, back)
    nxt, d = tx.m.next_slot(), tx.m.delayed()
    tx.note(RESUME, "done", "schedule resumed",
            f"{_operator_name(ev)} pressed Resume{_screen(ev)}."
            + (f" The delayed show {d.n} still waits for Start now."
               if d else "")
            + (f" Next on the schedule is show {nxt.n} at "
               f"{clock(_local(m, nxt.start))}."
               if nxt else " There are no more scheduled shows tonight."))
    return tx.done()


def _need_next(m, ev, now):
    nxt = m.next_slot()
    if nxt is None:
        return None, _refuse(m, ev, now, "There is no show still to come "
                                         "tonight.")
    return nxt, None


def _skip_next(m, ev, now):
    """Skips the delayed show if one is waiting, since that is the one that
    would run next; otherwise the next scheduled show."""
    nxt = m.delayed()
    if nxt is None:
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
        a_start = (a.fired_at + timedelta(seconds=a.paused_s)
                   if a.status == RUNNING else a.start)
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
    for s in m.pending() + ([m.delayed()] if m.delayed() else []):
        tx.set_slot(s.n, status=SKIPPED, reason="SKIPPED (operator, End night)")
        skipped.append(s.n)
    tx.m = replace(tx.m, held_from="")
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
_LIVE = frozenset((IDLE, STANDBY, HOLD, SHOW, PAUSED))

# Which states accept which event. Anything else is refused with a sentence
# from _why_not. The selftest checks every state against every event with
# its own table, written out by hand, so this and that have to agree.
ALLOWED = {
    BOOT_DONE: frozenset((BOOT,)),
    TICK: _ALL - {BOOT},
    SHOW_CONFIRMED: frozenset((SHOW, PAUSED)),
    SHOW_ENDED: frozenset((SHOW,)),
    SHOW_FAILED: frozenset((SHOW,)),
    FAULT_RAISED: _ALL,
    CLEAR_FAULT: _ALL,
    CLOSING_DONE: frozenset((CLOSING,)),
    START_NOW: frozenset((IDLE, STANDBY, HOLD, CLOSING, OFF)),
    HOLD_ON: frozenset((IDLE, STANDBY, SHOW)),
    RESUME: frozenset((HOLD, PAUSED)),
    SKIP_NEXT: _LIVE,
    DELAY_NEXT: _LIVE,
    DELAY_REST: _LIVE,
    ABORT: frozenset((SHOW, PAUSED)),
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
    if st == PAUSED and k in (SHOW_ENDED, SHOW_FAILED):
        return (f"Show {m.running} is paused, so it has neither ended nor "
                f"failed to start. Resume or Abort it first.")
    if k in (SHOW_CONFIRMED, SHOW_ENDED, SHOW_FAILED, ABORT):
        return f"No show is running; the scheduler is in {st}."
    if k == CLOSING_DONE:
        return f"The scheduler is not closing; it is in {st}."
    if k == START_NOW:
        how = "paused" if st == PAUSED else "running"
        return (f"Show {m.running} is {how}. Start now does nothing while a "
                f"show is running or paused.")
    if k == HOLD_ON:
        if st == HOLD:
            return "The schedule is already on hold."
        if st == PAUSED:
            return f"Show {m.running} is already paused."
        return "The night is over, so there is nothing to hold."
    if k == RESUME:
        return "Nothing is on hold or paused."
    if k == END_NIGHT:
        if st in (SHOW, PAUSED):
            how = "paused" if st == PAUSED else "running"
            return (f"Show {m.running} is {how}. Abort it first, then End "
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
    if ev.actor == "operator" and ev.who.strip().lower() not in \
            {n.lower() for n in m.operators}:
        return _refuse(m, ev, now,
                       f"{ev.who.strip()!r} is not on the operator list "
                       f"({', '.join(m.operators) or 'empty'}). Pick a name "
                       f"from the list. Nothing was changed.")
    if m.state not in ALLOWED[ev.kind]:
        return _refuse(m, ev, now, _why_not(m, ev))
    if ev.kind in (ABORT, END_NIGHT) and not ev.confirmed:
        return _refuse(m, ev, now, f"{_verb(ev)} has to be confirmed first. "
                                   f"Nothing was changed.")
    if ev.kind == CLEAR_FAULT and not m.faults:
        return _refuse(m, ev, now, "There is no fault to clear.")
    return _HANDLERS[ev.kind](m, ev, now)


# ------------------------------------------------ tonight, as data --

TONIGHT_FORMAT = 3
TONIGHT_KEYS = frozenset((
    "format", "date", "rule_id", "state", "running", "last_end",
    "held_from", "faults", "shows_started", "slots"))
SLOT_KEYS = frozenset((
    "n", "start", "status", "reason", "origin", "planned", "fired_at",
    "confirmed_at", "ended_at", "paused_s"))
ORIGINS = ("rule", "edit", "operator")


def _iso(dt):
    return _utc(dt).isoformat() if dt is not None else None


def machine_to_doc(m):
    """Tonight as JSON, so a restart can pick it up. Only what happened
    tonight is kept: the slots, their statuses and edits, when the last show
    ended, hold and faults. show_len_s, guard_s, late_grace_s and the zone
    always come from the rule file, never from here. Pure."""
    return {
        "format": TONIGHT_FORMAT, "date": m.date.isoformat(),
        "rule_id": m.rule_id, "state": m.state, "running": m.running,
        "last_end": _iso(m.last_end),
        "held_from": m.held_from, "faults": list(m.faults),
        "shows_started": m.shows_started,
        "slots": [{"n": s.n, "start": _iso(s.start), "status": s.status,
                   "reason": s.reason, "origin": s.origin,
                   "planned": _iso(s.planned), "fired_at": _iso(s.fired_at),
                   "confirmed_at": _iso(s.confirmed_at),
                   "ended_at": _iso(s.ended_at),
                   "paused_s": s.paused_s} for s in m.slots],
    }


def machine_from_doc(doc, rule, d, now, notes=None):
    """Tonight's saved list, checked, on a machine built from the CURRENT
    rule. It comes back in BOOT with `resumed_from` set, so BOOT_DONE runs
    the late rule over it exactly as it does over a fresh night.

    Everything in the file is checked, because a file is only a file: slot
    times on tonight's date in the rule's zone and not past midnight,
    statuses from the known set, no show still to come that carries a start
    or an end, and at most one running show and it is the one named. Raises
    ValueError with a sentence.

    A recorded time AFTER now is not corruption: it is the OS clock having
    been stepped back (the Windows time service does this) since it was
    written. Such times are taken as now, so the show they describe still
    counts as having happened, and a sentence with the size of the step is
    appended to `notes`."""
    now = _utc(_aware(now))
    ahead = []
    base = new_night(rule, d)
    tz = base.tz
    start_of_day = _utc(datetime.combine(d, time(0), tzinfo=tz))
    end_of_day = midnight(base)

    def when(v, what, optional=True):
        if v is None:
            if optional:
                return None
            raise ValueError(f"{what} is missing.")
        try:
            dt = datetime.fromisoformat(v)
        except (TypeError, ValueError):
            raise ValueError(f"{what} is not a time: {v!r}.")
        if dt.tzinfo is None:
            raise ValueError(f"{what} has no time zone.")
        dt = _utc(dt)
        if dt > now:
            ahead.append(dt)
            dt = now
        return dt

    def tonight(v, what):
        try:
            dt = _utc(datetime.fromisoformat(v))
        except (TypeError, ValueError):
            raise ValueError(f"{what} is not a time: {v!r}.")
        if not (start_of_day <= dt < end_of_day):
            raise ValueError(f"{what} is not on {d} in {rule.timezone}.")
        return dt

    if not isinstance(doc, dict) or doc.get("format") != TONIGHT_FORMAT:
        raise ValueError("It is not a saved night this version can read.")
    unknown = sorted(k for k in doc if k not in TONIGHT_KEYS)
    missing = sorted(k for k in TONIGHT_KEYS if k not in doc)
    if unknown or missing:
        raise ValueError("It has " + "; ".join(
            x for x in (unknown and f"settings it should not: "
                                    f"{', '.join(unknown)}",
                        missing and f"settings missing: "
                                    f"{', '.join(missing)}") if x) + ".")
    if doc["date"] != d.isoformat():
        raise ValueError(f"It is for {doc['date']}, not {d}.")
    state = doc["state"]
    if state not in STATES or state == BOOT:
        raise ValueError(f"{state!r} is not a state it can resume in.")
    if doc["held_from"] not in ("", IDLE, STANDBY):
        raise ValueError(f"held_from {doc['held_from']!r} is not a state "
                         f"Resume can return to.")
    if not isinstance(doc["faults"], list) or \
            not all(isinstance(f, str) for f in doc["faults"]):
        raise ValueError("faults has to be a list of sentences.")
    for k in ("running", "shows_started"):
        if isinstance(doc[k], bool) or not isinstance(doc[k], int) \
                or doc[k] < 0:
            raise ValueError(f"{k} has to be a whole number.")
    if not isinstance(doc["slots"], list):
        raise ValueError("slots has to be a list.")
    slots, seen = [], set()
    for x in doc["slots"]:
        if not isinstance(x, dict) or set(x) != SLOT_KEYS:
            raise ValueError(f"A show on the list is not in the right shape: "
                             f"{x!r}.")
        n = x["n"]
        if isinstance(n, bool) or not isinstance(n, int) or n < 1 \
                or n in seen:
            raise ValueError(f"Show number {n!r} is not a new whole number.")
        seen.add(n)
        status = x["status"]
        if status not in (PENDING, RUNNING, DELAYED) + tuple(sorted(FINAL)):
            raise ValueError(f"Show {n} has status {status!r}.")
        if x["origin"] not in ORIGINS or not isinstance(x["reason"], str):
            raise ValueError(f"Show {n} has origin {x['origin']!r}.")
        start = tonight(x["start"], f"Show {n}'s start")
        if start + timedelta(seconds=rule.show_len_s) > end_of_day \
                and x["origin"] != "operator":
            raise ValueError(f"Show {n} would finish after midnight.")
        sl = Slot(n=n, start=start, status=status, reason=x["reason"],
                  origin=x["origin"],
                  planned=(tonight(x["planned"], f"Show {n}'s planned start")
                           if x["planned"] is not None else None),
                  fired_at=when(x["fired_at"], f"Show {n}'s start time"),
                  confirmed_at=when(x["confirmed_at"],
                                    f"Show {n}'s confirm time"),
                  ended_at=when(x["ended_at"], f"Show {n}'s end time"),
                  paused_s=x["paused_s"])
        if isinstance(sl.paused_s, bool) or \
                not isinstance(sl.paused_s, (int, float)) or sl.paused_s < 0:
            raise ValueError(f"Show {n}'s paused time is not a number of "
                             f"seconds.")
        if status in (PENDING, DELAYED) and (sl.fired_at or sl.confirmed_at
                                             or sl.ended_at or sl.paused_s):
            raise ValueError(f"Show {n} is still to come but has already "
                             f"started or ended.")
        if status == RUNNING and (sl.fired_at is None or sl.ended_at):
            raise ValueError(f"Show {n} is running but has no start, or "
                             f"has already ended.")
        slots.append(sl)
    if sum(1 for sl in slots if sl.status == DELAYED) > 1:
        raise ValueError("More than one show is delayed; only one ever "
                         "waits.")
    running = [sl.n for sl in slots if sl.status == RUNNING]
    if running != ([doc["running"]] if doc["running"] else []):
        raise ValueError(f"It says show {doc['running'] or 'none'} is "
                         f"running, but the list says "
                         f"{', '.join(map(str, running)) or 'none'}.")
    if (state in (SHOW, PAUSED)) != bool(running):
        raise ValueError(f"It is in {state} with "
                         f"{'a' if running else 'no'} show running.")
    last_end = when(doc["last_end"], "The last show's end")
    if last_end is not None and last_end < start_of_day:
        raise ValueError("The last show's end is before tonight.")
    if ahead and notes is not None:
        step = (max(ahead) - now) / ONE_SECOND
        notes.append(
            f"The clock seems to have been set back by about {step:.0f} s: "
            f"tonight's record has times up to "
            f"{max(ahead).astimezone(tz):%H:%M:%S}, and it is now "
            f"{now.astimezone(tz):%H:%M:%S}. Those times are taken as now, "
            f"so nothing already started is started again.")
    return replace(
        base, slots=tuple(slots), running=doc["running"],
        last_end=last_end, held_from=doc["held_from"], faults=tuple(doc["faults"]),
        shows_started=doc["shows_started"], resumed_from=state,
        rule_id=str(doc["rule_id"]))


UNREADABLE = "MISSED (tonight's record was unreadable)"


def assume_the_worst(m, now):
    """A fresh night for when tonight's record existed but could not be
    used. What already ran is unknown, so assume the worst: the last show
    ended just now, so guard_s holds off anything immediate, and every show
    at or before now counts as passed, whatever the grace. Returns
    (machine, sentences). Pure."""
    now = _utc(_aware(now))
    notes, slots = [], []
    for sl in m.slots:
        if sl.status == PENDING and sl.start <= now:
            sl = replace(sl, status=MISSED, reason=UNREADABLE)
            notes.append(f"Show {sl.n} at {m.hm(sl.start)} is marked "
                         f"{UNREADABLE}: it may already have run.")
        slots.append(sl)
    notes.insert(0, f"Tonight's record could not be used, so the scheduler "
                    f"assumes a show may have just ended: nothing starts "
                    f"before {m.hm(now + timedelta(seconds=m.guard_s))}, "
                    f"and no show at or before {m.hm(now)} starts at all.")
    return replace(m, slots=tuple(slots), last_end=now), notes


def rebuild_night(rule, saved):
    """The rule changed since tonight was saved. Tonight is built again from
    the new rule, keeping only what already happened: every show with a
    final status (and one that was running, or delayed and waiting for
    Start now) is carried over onto the new list, matched by the time it
    was planned for. Operator edits to shows
    still to come are dropped. Returns (machine, sentences), one sentence per
    change, for the journal. Pure."""
    fresh = new_night(rule, saved.date)
    hm = fresh.hm
    by_time = {sl.start: sl for sl in fresh.slots}
    slots = {sl.n: sl for sl in fresh.slots}
    extra_n = max((sl.n for sl in fresh.slots), default=0)
    notes = []
    running = 0
    kept_times = set()
    for sl in sorted(saved.slots, key=lambda x: (x.start, x.n)):
        planned = sl.planned or sl.start
        if sl.status == PENDING:
            if sl.origin != "rule":
                notes.append(f"The show added tonight at {hm(sl.start)} is "
                             f"dropped because the schedule changed.")
            elif sl.planned is not None:
                notes.append(f"Tonight's move of the {hm(planned)} show to "
                             f"{hm(sl.start)} is dropped because the "
                             f"schedule changed.")
            elif planned not in by_time:
                notes.append(f"The {hm(planned)} show is no longer in the "
                             f"schedule.")
            continue
        match = by_time.get(planned) if sl.origin == "rule" else None
        if match is not None and match.start not in kept_times:
            n = match.n
            kept_times.add(match.start)
        else:
            extra_n += 1
            n = extra_n
        slots[n] = replace(sl, n=n)
        if sl.status == RUNNING:
            running = n
        notes.append(f"Show {n} at {hm(sl.start)} keeps its status, "
                     f"{sl.status}"
                     + (f", from show {sl.n} on the old list."
                        if n != sl.n else "."))
    saved_times = {(x.planned or x.start) for x in saved.slots}
    for sl in fresh.slots:
        if sl.start not in saved_times:
            notes.append(f"Show {sl.n} at {hm(sl.start)} is new in the "
                         f"schedule.")
    m = replace(fresh, slots=tuple(sorted(slots.values(), key=lambda x: x.n)),
                running=running, last_end=saved.last_end,
                held_from=saved.held_from,
                faults=saved.faults, shows_started=saved.shows_started,
                resumed_from=saved.resumed_from)
    return m, notes


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
        "paused": m.state == PAUSED,
        "expected_end": (clock(_local(m, m.expected_end(now)))
                         if m.running else None),
        "delayed": ({"show": m.delayed().n,
                     "planned": clock(_local(m, m.delayed().start))}
                    if m.delayed() else None),
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
