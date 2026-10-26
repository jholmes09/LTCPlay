"""The checker: reads a witness run together with the engine's machine log
and flamesafe's journal, and flags every nonzero flame channel that had no
right to be there, and every gap or stall in flamesafe's stream.

BENCH TOOL. Reads files, writes a report. Touches nothing else.

    python check_witness.py --witness "C:\\...\\witness\\2026-10-19_1800"
        --nights "%LOCALAPPDATA%\\ltcplay\\nights"
        --flamesafe-log "%LOCALAPPDATA%\\ltcplay\\logs\\flamesafe.log"

What it flags (each one an episode with its first time, its length and
how many packets):

  FIRE WITHOUT ARM      a fire slot nonzero while its group's safety slot in
                        the SAME packet is not at the arm value (wire only)
  BAD ARM VALUE         a safety slot carrying anything but 0 or the arm value
  UNKNOWN SLOT          a nonzero slot that is in no group of the config
  ARMED WITHOUT PRESS   a safety slot at the arm value with no arm press on
                        record for that group (deck lines in the machine log)
                        since the last disarm, Abort, link loss or restart
  FIRE OUTSIDE SHOW     a fire slot nonzero while the scheduler was not in
                        SHOW (Hold, between shows, before Run) and no purge
                        was held (purge lines in the machine log)
  WHILE ABORT LATCHED   anything nonzero between an accepted Abort and its
                        Reset (both the engine's record and flamesafe's
                        disarm-all count as the Abort)
  LOCKED SLOT           a locked slot (--locked-slots, or the lockout file)
                        nonzero while a lock was on record
  GAP / STALL           a gap between packets over --gap-ms (250, flamesafe's
                        overrun) / over --stall-s (2 s) while flamesafe was
                        running (its start and stop lines)
  LEFT ON THE WIRE      the stream stopped and its last packet was nonzero

With the laser listener's files in the run folder (laser.csv, laser.json,
from laser_listener.ps1 or .py standing where BEYOND would), the engine's
journal is the oracle for the lasers too:

  LASER BLANK MISSING   an Abort the conductor accepted, or a Hold that
                        paused a show, with no brightness blank (0, or
                        --blank-value) heard within --blank-within-s (2 s)
  LASER BLANK NOT HEARD the journal says a blank was sent (Lasers blanked,
                        BEYOND blanked) and the listener heard none
  LASER SILENCE         the journal says laser commands were sent and the
                        listener heard nothing at all
  LASER LIT OUTSIDE SHOW  a brightness restore (anything but the blank
                        value) while the scheduler was not in SHOW, PAUSED
                        or REHEARSAL
  LASER LIT WHILE ABORT LATCHED  a restore or a laser test ON between an
                        accepted Abort and its Reset
  LASER TEST WITHOUT RECORD  a laser's test ON message (the exact address
                        AND value ltcplay_morning.json gives) with no
                        Morning page test on record for that laser
  LASER TEST WHILE SHOW LIVE  a test ON message while the scheduler was in
                        SHOW or PAUSED
  LASER TEST ON LOCKED LASER  a test ON message for a laser locked out on
                        record
  LASER TEST NOT HEARD  a test ON on record with no ON message heard
  LASER TEST OFF MISSING  a test on record with no off (its off or lock
                        message, or a blank) within --blank-within-s of its
                        OFF, a show start or an Abort
  UNKNOWN LASER MESSAGE any OSC that is neither the brightness blank or
                        restore nor a configured laser message, a test ON
                        at the wrong value included
  FORBIDDEN LASER ADDRESS  BEYOND's BlackOut or MasterPause

The standing fault "Lasers: blanking not set up" (beyond_blank "osc" with
no "beyond" block, the show PC's state while BEYOND is absent) is EXPECTED:
then the conductor sends the lasers nothing, the blank rules are reported
UNVERIFIED rather than failed, and the fault is counted apart from every
other fault, which still counts.

Without --nights the state rules cannot be judged and the report says
"UNVERIFIED" for them rather than PASS. The witness run is the only source
for the wire; the journals are the only source for what was allowed.
"""
import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import laser_listener as ll  # noqa: E402  (the same table the listener used)

GRACE_S = 1.5
PURGE_MAX_S = 30.0
SHOW_STATES = ("SHOW",)
LASER_STATES = ("SHOW", "PAUSED", "REHEARSAL")   # conductor.LASER_STATES
LIVE_STATES = ("SHOW", "PAUSED")                 # no laser test while live
BLANK_WITHIN_S = 2.0        # beyond.py's 3 packets go out in about 40 ms
BLANK_BEFORE_S = 1.0        # the Abort's journal line is written AFTER the blank
MERGE_S = 1.5               # one press makes several journal records
NOT_SET_UP = "blanking not set up"               # beyondtc.NOT_SET_UP
TIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.(\d{1,3}))?\s\s([^:]+): (.*)$")


# ----------------------------------------------------------------- time ---

def _safeties(g):
    """Every safety slot of a group: one number, or a list (flamesafe
    multi-safety, 2026-10-26), or a "safeties" list from witness.py."""
    v = g.get("safeties") or g.get("safety")
    if isinstance(v, list):
        return [int(x) for x in v]
    return [int(v)]

def parse_time(s):
    """Local wall clock as written, offset dropped (the files were written
    on the show PC; its wall clock is the one axis everything shares)."""
    s = s.strip()
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


# -------------------------------------------------------------- loading ---
def load_witness(folder):
    """The run's files, as plain rows."""
    out = {"folder": folder, "meta": {}, "nonzero": [], "gaps": [],
           "stream": [], "log": [], "companion": False, "laser": [],
           "laser_meta": {}, "has_laser": False}
    try:
        with open(os.path.join(folder, "witness.json"), encoding="utf-8-sig") as fh:
            out["meta"] = json.load(fh)
            out["companion"] = bool(out["meta"].get("companion"))
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(folder, "laser.json"), encoding="utf-8-sig") as fh:
            out["laser_meta"] = json.load(fh)
            out["has_laser"] = True
    except (OSError, ValueError):
        pass
    for name in ("nonzero", "gaps", "stream", "laser"):
        p = os.path.join(folder, f"{name}.csv")
        if os.path.exists(p):
            if name == "laser":
                out["has_laser"] = True
            with open(p, encoding="utf-8-sig", newline="") as fh:
                for r in csv.DictReader(fh):
                    r["_t"] = parse_time(r.get("time", ""))
                    if r["_t"] is not None:
                        out[name].append(r)
    logs = sorted((f for f in os.listdir(folder)
                   if f.startswith("witness.log")),
                  key=lambda f: (-int(f.rsplit(".", 1)[1])
                                 if f[-1].isdigit() else 0))
    for name in logs:
        with open(os.path.join(folder, name), encoding="utf-8",
                  errors="replace") as fh:
            for line in fh:
                m = TIME_RE.match(line.rstrip("\n"))
                if m:
                    t = parse_time(m.group(1) + ("." + m.group(2).ljust(3, "0")
                                                 if m.group(2) else ""))
                    out["log"].append((t, m.group(3), m.group(4)))
    return out


def load_nights(folder):
    """Every record of every night_*.jsonl, oldest first."""
    recs = []
    if not folder or not os.path.isdir(folder):
        return recs
    for name in sorted(os.listdir(folder)):
        if not (name.startswith("night_") and name.endswith(".jsonl")):
            continue
        with open(os.path.join(folder, name), encoding="utf-8",
                  errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict):
                    continue
                r["_t"] = parse_time(str(r.get("at", "")))
                if r["_t"] is not None:
                    recs.append(r)
    recs.sort(key=lambda r: r["_t"])
    return recs


def load_flamesafe_log(path):
    """[(time, kind, msg)] from flamesafe's journal (its stdout, kept by
    the supervisor in logs\\flamesafe.log, or log_dir\\flamesafe.log)."""
    out = []
    if not path or not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = TIME_RE.match(line.rstrip("\n"))
            if m:
                out.append((parse_time(m.group(1)), m.group(3), m.group(4)))
    out.sort(key=lambda x: x[0])
    return out


def load_lockout_slots(path):
    """Every whole number 1..512 found under keys that look like slots or
    channels in a lockout file. Best effort: the file's exact layout is
    PR #68's (MORNING.md); --locked-slots overrides this."""
    slots = set()
    if not path or not os.path.exists(path):
        return slots
    try:
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return slots

    def walk(o, key=""):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, str(k).lower())
        elif isinstance(o, list):
            for v in o:
                walk(v, key)
        elif isinstance(o, int) and not isinstance(o, bool) and \
                1 <= o <= 512 and any(w in key for w in
                                      ("slot", "fire", "channel", "safety")):
            slots.add(o)
    walk(doc)
    return slots


# ------------------------------------------------------------- the state ---
class Timeline:
    """What was allowed when, from the journals. Every question takes a
    time and answers from the last record before it."""

    def __init__(self, recs, fs_lines, groups, purge_max_s=PURGE_MAX_S):
        self.groups = groups
        names = [g["name"] for g in groups]
        self.events = []       # (t, kind, payload) sorted
        for r in recs:
            t = r["_t"]
            action = str(r.get("action", "")).lower()
            outcome = str(r.get("outcome", "")).lower()
            text = str(r.get("text", ""))
            low = text.lower()
            st = str(r.get("to_state") or r.get("state") or "").upper()
            if st:
                self.events.append((t, "state", st))
            if action == "abort":
                # Every Abort disarms the flames first, whatever else it
                # was refused for; the latch only stands when it was taken.
                self.events.append((t, "disarm_all", "engine Abort"))
                if "refused" not in outcome:
                    self.events.append((t, "latch", True))
            elif action == "reset" and "refused" not in outcome:
                self.events.append((t, "latch", False))
            elif action in ("disarm-all", "disarm_all"):
                self.events.append((t, "disarm_all", "disarm-all pressed"))
            elif action == "arm":
                for n in names:
                    if re.search(re.escape(n) + r"\s+arm pressed", text,
                                 re.IGNORECASE):
                        self.events.append((t, "arm", n))
            elif action == "disarm":
                hit = False
                for n in names:
                    if re.search(re.escape(n) + r"\s+disarm pressed", text,
                                 re.IGNORECASE):
                        self.events.append((t, "disarm", n))
                        hit = True
                if not hit:
                    self.events.append((t, "disarm_all", "disarm"))
            if "purge" in action or "purge" in low:
                ended = any(w in low or w in outcome for w in
                            ("released", "stopped", "cleared", "ended",
                             "refused", "let go", "dropped"))
                self.events.append((t, "purge", not ended))
            if re.search(r"\block(ed|out)\b", low) and "unlock" not in low:
                self.events.append((t, "lock", True))
            elif "unlock" in low:
                self.events.append((t, "lock", False))
            if action in ("program start", "program stop", "start", "stop") \
                    and "flamesafe" in low:
                self.events.append((t, "disarm_all", "flamesafe " + action))
        for t, kind, msg in fs_lines:
            if kind == "disarm-all":
                self.events.append((t, "disarm_all", "flamesafe disarm-all"))
                self.events.append((t, "latch", True))
            elif kind == "link" and "stopped answering" in msg:
                self.events.append((t, "disarm_all", "flamesafe link lost"))
            elif kind == "start":
                self.events.append((t, "fs_running", True))
                self.events.append((t, "disarm_all", "flamesafe started"))
            elif kind == "stop":
                self.events.append((t, "fs_running", False))
                self.events.append((t, "disarm_all", "flamesafe stopped"))
            elif kind == "arm-input" and "stale" in msg:
                self.events.append((t, "disarm_all", "arm input stale"))
        self.events.sort(key=lambda e: e[0])
        self.purge_max = timedelta(seconds=purge_max_s)
        self.has_state = any(e[1] == "state" for e in self.events)
        self.has_fs = any(e[1] == "fs_running" for e in self.events)
        self.has_arms = any(e[1] in ("arm", "disarm", "disarm_all")
                            for e in self.events)

    def at(self, t):
        """The state at time t: {"state", "latched", "armed": {name: since},
        "purge_until", "locked", "fs_running"}."""
        s = {"state": None, "latched": False, "armed": {},
             "purge_until": None, "locked": False, "fs_running": None,
             "last_disarm": None}
        for et, kind, v in self.events:
            if et > t:
                break
            if kind == "state":
                s["state"] = v
            elif kind == "latch":
                s["latched"] = v
            elif kind == "arm":
                s["armed"][v] = et
            elif kind == "disarm":
                s["armed"].pop(v, None)
                s["last_disarm"] = et
            elif kind == "disarm_all":
                s["armed"] = {}
                s["last_disarm"] = et
            elif kind == "purge":
                s["purge_until"] = (et + self.purge_max) if v else et
            elif kind == "lock":
                s["locked"] = v
            elif kind == "fs_running":
                s["fs_running"] = v
        return s


# ------------------------------------------------------------ episodes ---
class Episodes:
    """Violations collapsed: the same rule and slot within 1 s is one
    episode."""

    def __init__(self):
        self.items = {}

    def add(self, rule, slot, t, detail):
        key = (rule, slot)
        ep = self.items.get(key)
        if ep and (t - ep[-1]["last"]).total_seconds() <= 1.0:
            ep[-1]["last"] = t
            ep[-1]["n"] += 1
            return
        self.items.setdefault(key, []).append(
            {"rule": rule, "slot": slot, "first": t, "last": t, "n": 1,
             "detail": detail})

    def all(self):
        out = [e for lst in self.items.values() for e in lst]
        out.sort(key=lambda e: e["first"])
        return out


# --------------------------------------------------------------- lasers ---
def laser_table(meta, morning_path=None, blank_value=None):
    """What a laser message may look like: {"brightness_addr",
    "blank_value", "lasers": [{"name", "on", "off", "lock", ...}]}, from the
    listener's laser.json (what it knew when it listened), overridden by
    --morning (ltcplay_morning.json) and --blank-value."""
    lasers = list(meta.get("lasers") or [])
    if morning_path:
        lasers, _how = ll.read_lasers(morning_path, str(meta.get("host") or
                                                       ll.DEFAULT_HOST),
                                      int(meta.get("port") or ll.DEFAULT_PORT))
    if blank_value is None:
        try:
            blank_value = float(meta.get("blank_value", ll.BLANK_VALUE))
        except (TypeError, ValueError):
            blank_value = ll.BLANK_VALUE
    return {"brightness_addr": str(meta.get("brightness_addr") or
                                   ll.BRIGHTNESS_ADDR),
            "blank_value": blank_value, "lasers": lasers}


def parse_laser_args(types, text):
    """The listener's args field back into values, by type tag."""
    parts = text.split(";") if text != "" else []
    out = []
    for i, tag in enumerate(types or ""):
        if tag in "[]":
            continue
        s = parts[i] if i < len(parts) else ""
        if tag in "ifhd":
            try:
                out.append(float(s) if tag in "fd" else int(float(s)))
            except ValueError:
                out.append(s)
        elif tag == "T":
            out.append(True)
        elif tag == "F":
            out.append(False)
        elif tag == "N":
            out.append(None)
        else:
            out.append(s)
    if not types and parts:
        out = parts
    return out


def _merge(times, within=MERGE_S):
    """Times within `within` s of the previous kept one are the same press."""
    out = []
    for t in sorted(times):
        if not out or (t - out[-1]).total_seconds() > within:
            out.append(t)
    return out


def laser_journal(recs, table):
    """What the engine said about the lasers, from the machine log:
    accepted Aborts, Holds that paused a show, show starts, every blank it
    says it sent, laser test on and off per laser, laser lockouts, and
    whether OSC blanking was set up at all."""
    names = [l.get("name") or l.get("id") for l in table["lasers"]]
    ev = {"aborts": [], "holds": [], "show_starts": [], "blank_claims": [],
          "tests": [], "locks": [], "not_set_up": [], "osc_mode": None}
    open_tests = {}          # name -> t_on
    prev_state = None
    for r in recs:
        t = r["_t"]
        action = str(r.get("action", "")).lower()
        outcome = str(r.get("outcome", "")).lower()
        text = str(r.get("text", ""))
        low = text.lower()
        st = str(r.get("to_state") or r.get("state") or "").upper()
        refused = "refused" in outcome or "refused" in low
        if st == "SHOW" and prev_state is not None and prev_state != "SHOW":
            ev["show_starts"].append(t)
        if action == "show start" and not refused:
            ev["show_starts"].append(t)
        if action == "abort" and not refused and (
                low.startswith("abort, asked for by") or
                prev_state in LIVE_STATES):
            ev["aborts"].append(t)
        if action == "hold" and not refused and (
                low.startswith("hold, asked for by") or st == "PAUSED"):
            ev["holds"].append(t)
        if action == "lasers":
            if outcome in ("blanked", "reblanked"):
                ev["blank_claims"].append(t)
            if outcome == "not_set_up" or NOT_SET_UP in low:
                ev["not_set_up"].append(t)
            m = re.search(r'beyond_blank "(\w+)"', text)
            if outcome == "blank_mode" and m:
                ev["osc_mode"] = m.group(1)
        if action == "blank" and "failed" not in outcome and \
                "beyond blanked" in low:
            ev["blank_claims"].append(t)
        if action == "laser-test":
            m = re.search(r"turned the (.+?) calibration test (ON|OFF)", text)
            if m and outcome in ("on", "off"):
                name = m.group(1)
                if outcome == "on":
                    open_tests[name] = t
                else:
                    ev["tests"].append((name, open_tests.pop(name, t), t))
            elif outcome == "all_off":
                for name, t_on in list(open_tests.items()):
                    ev["tests"].append((name, t_on, t))
                open_tests.clear()
        if action == "lockout":
            m = re.match(r"(.+?) \(laser\) (LOCKED OUT|UNLOCKED)", text)
            if m:
                ev["locks"].append((m.group(1), t, m.group(2) == "LOCKED OUT"))
        if st:
            prev_state = st
    for name, t_on in open_tests.items():
        ev["tests"].append((name, t_on, None))
    for k in ("aborts", "holds", "show_starts", "blank_claims"):
        ev[k] = _merge(ev[k])
    ev["names"] = names
    return ev


def _locked_at(locks, name, t):
    state = False
    for n, lt, on in locks:
        if n == name and lt <= t:
            state = on
    return state


def _test_on_at(tests, name, t, grace):
    for n, t_on, t_off in tests:
        if n != name:
            continue
        if t_on - grace <= t and (t_off is None or t <= t_off + grace):
            return True
    return False


def laser_check(w, recs, tl, ep, table, within_s=BLANK_WITHIN_S,
                grace_s=GRACE_S, before_s=BLANK_BEFORE_S):
    """The laser rules. Adds episodes to `ep`; returns the summary the
    report prints."""
    within = timedelta(seconds=within_s)
    before = timedelta(seconds=before_s)
    grace = timedelta(seconds=grace_s)
    ev = laser_journal(recs, table)
    rows = []
    for r in w["laser"]:
        args = parse_laser_args(r.get("types", ""), r.get("args", ""))
        kind, who = ll.classify(r.get("address", ""), args, table["lasers"],
                                table["blank_value"], table["brightness_addr"])
        rows.append((r["_t"], r.get("address", ""), r.get("types", ""),
                     r.get("args", ""), args, kind, who))
    counts = {"messages": len(rows), "blanks": 0, "unblanks": 0, "tests": 0,
              "other": 0, "forbidden": 0}
    blank_times = []
    dark_for = {}            # laser name -> [t] of TEST OFF / LOCK
    on_for = {}              # laser name -> [t] of TEST ON
    for t, address, types, text, args, kind, who in rows:
        if ll.is_blank(address, args, table["blank_value"],
                       table["brightness_addr"]):
            blank_times.append(t)
        if kind == "BLANK":
            counts["blanks"] += 1
        elif kind == "UNBLANK":
            counts["unblanks"] += 1
        elif kind in ("TEST ON", "TEST OFF", "LOCK"):
            counts["tests"] += 1
        elif kind == "FORBIDDEN":
            counts["forbidden"] += 1
        else:
            counts["other"] += 1
        if kind == "TEST ON":
            on_for.setdefault(who, []).append(t)
        elif kind in ("TEST OFF", "LOCK"):
            dark_for.setdefault(who, []).append(t)
        st = tl.at(t)
        st_g = tl.at(t - grace)
        latched = st["latched"] and st_g["latched"]
        shown = f"{address} ,{types} {text}"
        if kind == "FORBIDDEN":
            ep.add("FORBIDDEN LASER ADDRESS", 0, t,
                   f"{shown}: BEYOND's BlackOut or MasterPause must never be "
                   f"sent")
        elif kind == "OTHER":
            ep.add("UNKNOWN LASER MESSAGE", 0, t,
                   f"{shown}: not the brightness blank or restore, and not a "
                   f"configured laser test message (address AND value)")
        elif kind == "UNBLANK":
            if latched:
                ep.add("LASER LIT WHILE ABORT LATCHED", 0, t,
                       f"{shown} while an Abort stands")
            elif tl.has_state and st["state"] not in LASER_STATES and \
                    st_g["state"] not in LASER_STATES:
                ep.add("LASER LIT OUTSIDE SHOW", 0, t,
                       f"{shown} in state {st['state'] or 'unknown'}")
        elif kind == "TEST ON":
            if tl.has_state and not _test_on_at(ev["tests"], who, t, grace):
                ep.add("LASER TEST WITHOUT RECORD", 0, t,
                       f"{who}: {shown} with no Morning page test on record")
            if latched:
                ep.add("LASER LIT WHILE ABORT LATCHED", 0, t,
                       f"{who}: {shown} while an Abort stands")
            if _locked_at(ev["locks"], who, t):
                ep.add("LASER TEST ON LOCKED LASER", 0, t,
                       f"{who}: {shown} while locked out on record")
            if tl.has_state and st["state"] in LIVE_STATES and \
                    st_g["state"] in LIVE_STATES:
                ep.add("LASER TEST WHILE SHOW LIVE", 0, t,
                       f"{who}: {shown} in state {st['state']}")

    def heard(times, t0):
        return any(t0 - before <= x <= t0 + within for x in times)

    osc_used = ev["osc_mode"] in (None, "osc", "both")
    set_up = osc_used and not ev["not_set_up"]
    by_name = {(l.get("name") or l.get("id")): l for l in table["lasers"]}
    unverified = []
    claims = len(ev["blank_claims"]) + sum(
        1 for n, _a, _b in ev["tests"] if (by_name.get(n) or {}).get("on"))
    if tl.has_state:
        if claims and not rows:
            ep.add("LASER SILENCE", 0, ev["blank_claims"][0] if
                   ev["blank_claims"] else ev["tests"][0][1],
                   f"the journal says {claims} laser command(s) were sent "
                   f"(blanks and test ONs) and the listener heard nothing at "
                   f"all: check the beyond block's host and port against the "
                   f"listener's, and that it was running")
        elif set_up:
            for t in ev["aborts"]:
                if not heard(blank_times, t):
                    ep.add("LASER BLANK MISSING", 0, t,
                           f"no brightness {table['blank_value']:g} within "
                           f"{within_s:g} s of the Abort at {t:%H:%M:%S}")
            for t in ev["holds"]:
                if not heard(blank_times, t):
                    ep.add("LASER BLANK MISSING", 0, t,
                           f"no brightness {table['blank_value']:g} within "
                           f"{within_s:g} s of the Hold at {t:%H:%M:%S}")
            for t in ev["blank_claims"]:
                if not heard(blank_times, t):
                    ep.add("LASER BLANK NOT HEARD", 0, t,
                           f"the journal says a blank was sent at "
                           f"{t:%H:%M:%S.%f}; the listener heard none within "
                           f"{within_s:g} s")
        elif not osc_used:
            unverified.append(
                f"beyond_blank is \"{ev['osc_mode']}\": the lasers are blanked "
                f"by Art-Net timecode, which the laser listener does not "
                f"hear, so blank-after-Abort was not judged from the wire.")
        else:
            unverified.append(
                f"EXPECTED while BEYOND is absent: the journal says 'Lasers: "
                f"{NOT_SET_UP}' {len(ev['not_set_up'])} time(s) (beyond_blank "
                f"osc with no beyond block), so the conductor sent the lasers "
                f"nothing and blank-after-Abort and blank-after-Hold could not "
                f"be judged from the wire. Every other fault still counts.")
        if rows or claims:
            for name, t_on, t_off in ev["tests"]:
                laser = by_name.get(name)
                if laser is None:
                    unverified.append(f"{name}: a test on record, but no such "
                                      f"laser in the listener's table, so its "
                                      f"messages could not be judged.")
                    continue
                if laser.get("on") and not heard(on_for.get(name, []), t_on):
                    ep.add("LASER TEST NOT HEARD", 0, t_on,
                           f"{name}: test ON on record at {t_on:%H:%M:%S}; no "
                           f"ON message heard within {within_s:g} s")
                ends = [("its OFF", t_off)] if t_off is not None else []
                for t in ev["show_starts"]:
                    if t_on <= t and (t_off is None or t <= t_off + grace):
                        ends.append(("the show start", t))
                for t in ev["aborts"]:
                    if t_on <= t and (t_off is None or t <= t_off + grace):
                        ends.append(("the Abort", t))
                dark = sorted(dark_for.get(name, []) + blank_times)
                for why, t in ends:
                    if not laser.get("off") and not laser.get("lock") and \
                            not set_up:
                        unverified.append(
                            f"{name}: its off list is empty and OSC blanking "
                            f"is not set up, so nothing can show its test "
                            f"went dark at {why} ({t:%H:%M:%S}).")
                        continue
                    if not heard(dark, t):
                        ep.add("LASER TEST OFF MISSING", 0, t,
                               f"{name}: no off or lock message and no blank "
                               f"within {within_s:g} s of {why} at "
                               f"{t:%H:%M:%S}")
    return {"counts": counts, "aborts": len(ev["aborts"]),
            "holds": len(ev["holds"]), "show_starts": len(ev["show_starts"]),
            "blank_claims": len(ev["blank_claims"]), "tests": len(ev["tests"]),
            "set_up": set_up, "osc_mode": ev["osc_mode"],
            "not_set_up": len(ev["not_set_up"]), "unverified": unverified,
            "lasers_known": [l.get("name") or l.get("id")
                             for l in table["lasers"]]}


# ---------------------------------------------------------------- check ---
def check(w, recs, fs_lines, groups, arm_value, gap_ms=250.0, stall_s=2.0,
          grace_s=GRACE_S, locked_slots=(), purge_max_s=PURGE_MAX_S,
          laser_table_=None, blank_within_s=BLANK_WITHIN_S):
    tl = Timeline(recs, fs_lines, groups, purge_max_s)
    ep = Episodes()
    notes = []
    grace = timedelta(seconds=grace_s)
    safety_of = {}
    fire_of = {}
    arm_of = {}
    for g in groups:
        for s in _safeties(g):
            safety_of[s] = g["name"]
        arm_of[g["name"]] = int(g.get("arm_value", arm_value))
        for f in g["fire"]:
            fire_of[f] = g["name"]
    safety_slots = {g["name"]: _safeties(g) for g in groups}
    locked = set(locked_slots)
    fire_packets = 0
    armed_packets = 0
    for r in w["nonzero"]:
        t = r["_t"]
        try:
            slots = [(int(a), int(b)) for a, b in
                     (x.split("=") for x in r["slots"].split(";") if x)]
        except ValueError:
            continue
        values = dict(slots)
        st = tl.at(t)
        st_g = tl.at(t - grace)   # the state a moment earlier, for grace
        fired = False
        for slot, v in slots:
            if slot in safety_of:
                name = safety_of[slot]
                armed_packets += 1
                if v != arm_of[name]:
                    ep.add("BAD ARM VALUE", slot, t,
                           f"{name}: safety slot {slot} = {v}, arm value is "
                           f"{arm_of[name]}")
                if tl.has_arms and name not in st["armed"] \
                        and name not in st_g["armed"]:
                    ep.add("ARMED WITHOUT PRESS", slot, t,
                           f"{name} armed on the wire with no arm press on "
                           f"record (last disarm {st['last_disarm']})")
                if st["latched"] and st_g["latched"]:
                    ep.add("WHILE ABORT LATCHED", slot, t,
                           f"{name} armed on the wire while an Abort stands")
            elif slot in fire_of:
                name = fire_of[slot]
                fired = True
                # Every safety slot of the group not locked out must carry
                # the arm value (multi-safety: a locked one stays at zero).
                down = [(s, values.get(s, 0)) for s in safety_slots[name]
                        if s not in locked and values.get(s, 0) != arm_of[name]]
                if down:
                    ep.add("FIRE WITHOUT ARM", slot, t,
                           f"{name}: fire slot {slot} = {v} while its safety "
                           f"slot {down[0][0]} = {down[0][1]}")
                if tl.has_state:
                    purging = st["purge_until"] is not None and \
                        t <= st["purge_until"]
                    if st["state"] not in SHOW_STATES and \
                            st_g["state"] not in SHOW_STATES and not purging:
                        ep.add("FIRE OUTSIDE SHOW", slot, t,
                               f"{name}: fire slot {slot} = {v} in state "
                               f"{st['state'] or 'unknown'}, no purge held")
                if st["latched"] and st_g["latched"]:
                    ep.add("WHILE ABORT LATCHED", slot, t,
                           f"{name}: fire slot {slot} = {v} while an Abort "
                           f"stands")
                if slot in locked and st["locked"]:
                    ep.add("LOCKED SLOT", slot, t,
                           f"{name}: locked slot {slot} = {v}")
            else:
                ep.add("UNKNOWN SLOT", slot, t,
                       f"slot {slot} = {v} is in no group")
        if fired:
            fire_packets += 1
    # Gaps and stalls: only while flamesafe was running, when that is known.
    for r in w["gaps"]:
        try:
            g = float(r["gap_ms"])
        except (TypeError, ValueError):
            continue
        t = r["_t"]
        if tl.has_fs:
            st = tl.at(t - timedelta(milliseconds=g) - grace)
            if st["fs_running"] is False:
                continue
        if g >= stall_s * 1000.0:
            ep.add("STALL", 0, t, f"{g / 1000:.1f} s with no packet "
                                   f"(seq {r.get('last_seq')} to "
                                   f"{r.get('next_seq')}); the last packet "
                                   f"before it carried {r.get('last_nonzero')} "
                                   f"nonzero slot(s)")
        elif g > gap_ms:
            ep.add("GAP", 0, t, f"{g:.0f} ms between seq {r.get('last_seq')} "
                                 f"and {r.get('next_seq')}")
    for t, kind, msg in w["log"]:
        if kind == "STREAM STOPPED WITH NONZERO ON THE WIRE":
            ep.add("LEFT ON THE WIRE", 0, t, msg)
        elif kind == "MULTICAST":
            ep.add("MULTICAST", 0, t, msg)
        elif kind == "CANNOT LISTEN":
            notes.append(f"{t}  the witness could not bind the port: {msg}")
        elif kind == "COMPANION":
            notes.append(f"{t}  companion mode: {msg}")
    laser = None
    if w.get("has_laser"):
        table = laser_table_ or laser_table(w.get("laser_meta") or {})
        laser = laser_check(w, recs, tl, ep, table, blank_within_s, grace_s)
    packets = sum(int(r.get("packets") or 0) for r in w["stream"])
    nonzero = len(w["nonzero"])
    return {"episodes": ep.all(), "notes": notes, "packets": packets,
            "nonzero_packets": nonzero, "fire_packets": fire_packets,
            "armed_packets": armed_packets, "has_state": tl.has_state,
            "has_arms": tl.has_arms, "has_fs": tl.has_fs,
            "events": len(tl.events), "companion": w["companion"],
            "laser": laser}


def journal_counts(recs, fs_lines):
    c = {"arm presses": 0, "disarm presses": 0, "aborts": 0, "resets": 0,
         "faults (engine), other than the standing laser fault": 0,
         "faults (engine), 'Lasers: blanking not set up' (EXPECTED while "
         "BEYOND is absent)": 0,
         "laser blanks the journal says were sent": 0,
         "laser tests turned on": 0,
         "flamesafe disarm-all": 0,
         "flamesafe link lost": 0, "flamesafe faults": 0,
         "flamesafe overruns": 0, "flamesafe starts": 0,
         "flamesafe stops": 0}
    for r in recs:
        a = str(r.get("action", "")).lower()
        o = str(r.get("outcome", "")).lower()
        low = str(r.get("text", "")).lower()
        if a == "arm" and "arm pressed" in str(r.get("text", "")):
            c["arm presses"] += 1
        elif a == "disarm":
            c["disarm presses"] += 1
        elif a == "abort":
            c["aborts"] += 1
        elif a == "reset":
            c["resets"] += 1
        if a == "lasers" and o in ("blanked", "reblanked"):
            c["laser blanks the journal says were sent"] += 1
        if a == "laser-test" and o == "on":
            c["laser tests turned on"] += 1
        if r.get("fault"):
            if NOT_SET_UP in low:
                c["faults (engine), 'Lasers: blanking not set up' (EXPECTED "
                  "while BEYOND is absent)"] += 1
            else:
                c["faults (engine), other than the standing laser fault"] += 1
    for _t, kind, msg in fs_lines:
        if kind == "disarm-all":
            c["flamesafe disarm-all"] += 1
        elif kind == "link" and "stopped answering" in msg:
            c["flamesafe link lost"] += 1
        elif kind in ("fault", "compose-fault"):
            c["flamesafe faults"] += 1
        elif kind == "overrun":
            c["flamesafe overruns"] += 1
        elif kind == "start":
            c["flamesafe starts"] += 1
        elif kind == "stop":
            c["flamesafe stops"] += 1
    return c


# --------------------------------------------------------------- report ---
def report(res, counts, w, args_text, span):
    eps = res["episodes"]
    hard = [e for e in eps if e["rule"] not in ("GAP",)]
    verdict = "FAIL" if hard else "PASS"
    lines = ["WIRE WITNESS CHECK", "BENCH TOOL: this judges a recording; it "
             "changes nothing.", "",
             f"Verdict: {verdict}" + ("" if hard else (
                 " (GAPS noted, see below)" if eps else "")),
             f"Witness run: {w['folder']}",
             f"Checked: {args_text}",
             f"Recording spans {span[0]} to {span[1]}" if span[0] else
             "Recording spans: nothing recorded",
             f"Packets heard: {res['packets']}; packets with any nonzero "
             f"slot: {res['nonzero_packets']}; with an armed safety slot: "
             f"{res['armed_packets']}; with a fire slot: "
             f"{res['fire_packets']}", ""]
    if res["companion"]:
        lines.append("COMPANION RUN: the witness did not bind the flame port "
                     "(the soak owned it). The wire was judged by the soak's "
                     "own listener; see its flame-violations.txt. Only the "
                     "process and disk samples here are independent.")
        lines.append("")
    if not res["has_state"]:
        lines.append("UNVERIFIED: no machine log (--nights) was given or it "
                     "had no records, so FIRE OUTSIDE SHOW and WHILE ABORT "
                     "LATCHED could not be judged from the engine's side.")
    if not res["has_arms"]:
        lines.append("UNVERIFIED: no arm or disarm presses on record, so "
                     "ARMED WITHOUT PRESS could not be judged.")
    if not res["has_fs"]:
        lines.append("UNVERIFIED: no flamesafe journal (--flamesafe-log), so "
                     "gaps are judged over the whole recording and "
                     "flamesafe's own Aborts and link losses are not in "
                     "the timeline.")
    if res["packets"] == 0 and not res["companion"]:
        lines.append("FAIL: no packets were heard at all. Either flamesafe "
                     "was not running, its destination is not this PC, or "
                     "the witness could not bind the port (see notes).")
        verdict = "FAIL"
        lines[3] = "Verdict: FAIL"
    lz = res.get("laser")
    if lz is None:
        lines.append("UNVERIFIED: no laser listener files (laser.csv, "
                     "laser.json) in this run, so the laser rules were not "
                     "judged. Start the laser listener with the witness.")
    else:
        c = lz["counts"]
        known = ", ".join(lz["lasers_known"]) or \
            "none (no ltcplay_morning.json lasers)"
        lines.append(f"LASER LISTENER: {c['messages']} OSC message(s) heard: "
                     f"{c['blanks']} blank, {c['unblanks']} restore, "
                     f"{c['tests']} laser test (on, off or lock), "
                     f"{c['other']} unknown, {c['forbidden']} forbidden. "
                     f"Journal: {lz['aborts']} accepted Abort(s), "
                     f"{lz['holds']} Hold(s) that paused a show, "
                     f"{lz['show_starts']} show start(s), "
                     f"{lz['blank_claims']} blank(s) said sent, "
                     f"{lz['tests']} laser test(s) on record; lasers known "
                     f"to the listener: {known}.")
        if not res["has_state"]:
            lines.append("UNVERIFIED: without the machine log the laser "
                         "rules that need the journal (blank after Abort "
                         "and Hold, test on record, latched) were not "
                         "judged; only unknown and forbidden messages were.")
        for u in lz["unverified"]:
            lines.append(f"UNVERIFIED: {u}")
    lines.append("")
    lines.append(f"Violation episodes: {len(hard)}"
                 + (f" (and {len(eps) - len(hard)} gap episodes)"
                    if len(eps) != len(hard) else ""))
    for e in eps[:200]:
        dur = (e["last"] - e["first"]).total_seconds()
        lines.append(f"  {e['first']}  {e['rule']}"
                     + (f" slot {e['slot']}" if e["slot"] else "")
                     + f": {e['detail']}"
                     + (f" [{e['n']} packets over {dur:.1f} s]"
                        if e["n"] > 1 else ""))
    if len(eps) > 200:
        lines.append(f"  ... and {len(eps) - 200} more")
    lines += ["", "From the journals:"]
    lines += [f"  {k}: {v}" for k, v in counts.items()]
    if res["notes"]:
        lines += ["", "Witness notes:"] + [f"  {n}" for n in res["notes"]]
    lines += ["", "What PASS means here: every nonzero flame channel the "
              "witness heard was armed by a press on record, fired only in "
              "SHOW (or under a purge on record), never while an Abort "
              "stood, never on a locked slot, and the stream never stalled "
              "or stopped with values on the wire. With the laser listener: "
              "every accepted Abort and every Hold that paused a show was "
              "followed by BEYOND's brightness blank on the wire, every "
              "blank the journal says it sent was heard, the lasers were "
              "never restored outside SHOW or while an Abort stood, a laser "
              "test's ON message appeared only while the Morning page had "
              "that test on, at its configured value, never while live, "
              "latched or locked out, and went dark at its OFF, at a show "
              "start and at an Abort; nothing else was sent to the lasers. "
              "What it cannot mean: anything about packets the witness or "
              "the listener could not hear (a destination that is not this "
              "PC, a port it could not bind), or what real BEYOND would do "
              "with a command (no BEYOND ran: the listener only proves the "
              "command left this program at the right moment)."]
    return verdict, "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--witness", required=True, help="the witness run folder")
    ap.add_argument("--nights", help="the engine's nights folder "
                                     "(%%LOCALAPPDATA%%\\ltcplay\\nights)")
    ap.add_argument("--flamesafe-log", help="logs\\flamesafe.log")
    ap.add_argument("--config", help="flamesafe.json, if witness.json has no "
                                     "groups")
    ap.add_argument("--lockout", help="ltcplay_lockout.json (best effort)")
    ap.add_argument("--locked-slots", help="slots locked during the test, "
                                           "comma separated")
    ap.add_argument("--gap-ms", type=float, default=250.0)
    ap.add_argument("--stall-s", type=float, default=2.0)
    ap.add_argument("--grace-s", type=float, default=GRACE_S)
    ap.add_argument("--purge-max-s", type=float, default=PURGE_MAX_S)
    ap.add_argument("--morning", help="ltcplay_morning.json: the laser test "
                                      "messages (default: what laser.json "
                                      "recorded)")
    ap.add_argument("--laser-csv", help="a laser.csv outside the witness "
                                        "folder")
    ap.add_argument("--blank-within-s", type=float, default=BLANK_WITHIN_S,
                    help="how soon after an Abort or Hold the blank must be "
                         "heard (2 s)")
    ap.add_argument("--blank-value", type=float,
                    help="the brightness value that is a blank (default: "
                         "laser.json's, else 0)")
    ap.add_argument("--out", help="report path (default: check_report.txt in "
                                  "the witness folder)")
    a = ap.parse_args(argv)
    w = load_witness(a.witness)
    if a.laser_csv:
        w["has_laser"] = True
        w["laser"] = []
        with open(a.laser_csv, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                r["_t"] = parse_time(r.get("time", ""))
                if r["_t"] is not None:
                    w["laser"].append(r)
    table = laser_table(w.get("laser_meta") or {}, a.morning, a.blank_value) \
        if w.get("has_laser") else None
    groups = w["meta"].get("groups") or []
    arm_value = int(w["meta"].get("arm_value") or 0)
    if (not groups) and a.config:
        with open(a.config, encoding="utf-8-sig") as fh:
            d = json.load(fh)
        arm_value = int(d.get("arm_value", 0))
        groups = [{"name": g["name"], "safety": g["safety"],
                   "fire": [int(f) for f in g["fire"]],
                   "arm_value": int(g.get("arm_value", arm_value))}
                  for g in d.get("groups", [])]
    recs = load_nights(a.nights)
    fs = load_flamesafe_log(a.flamesafe_log)
    locked = set()
    if a.locked_slots:
        locked = {int(x) for x in a.locked_slots.split(",") if x.strip()}
    elif a.lockout:
        locked = load_lockout_slots(a.lockout)
    res = check(w, recs, fs, groups, arm_value, a.gap_ms, a.stall_s,
                a.grace_s, locked, a.purge_max_s, table, a.blank_within_s)
    counts = journal_counts(recs, fs)
    times = [r["_t"] for r in w["stream"]] + [r["_t"] for r in w["nonzero"]] \
        + [r["_t"] for r in w["laser"]]
    span = (min(times), max(times)) if times else (None, None)
    verdict, text = report(res, counts, w,
                           f"nights={a.nights or 'none'}; flamesafe-log="
                           f"{a.flamesafe_log or 'none'}; gap {a.gap_ms:g} "
                           f"ms; stall {a.stall_s:g} s; locked slots "
                           f"{sorted(locked) or 'none'}; laser blank within "
                           f"{a.blank_within_s:g} s; morning="
                           f"{a.morning or 'laser.json'}", span)
    out = a.out or os.path.join(a.witness, "check_report.txt")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)
    print(f"Report written: {out}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
