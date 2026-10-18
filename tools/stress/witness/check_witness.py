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

GRACE_S = 1.5
PURGE_MAX_S = 30.0
SHOW_STATES = ("SHOW",)
TIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.(\d{1,3}))?\s\s([^:]+): (.*)$")


# ----------------------------------------------------------------- time ---
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
           "stream": [], "log": [], "companion": False}
    try:
        with open(os.path.join(folder, "witness.json"), encoding="utf-8-sig") as fh:
            out["meta"] = json.load(fh)
            out["companion"] = bool(out["meta"].get("companion"))
    except (OSError, ValueError):
        pass
    for name in ("nonzero", "gaps", "stream"):
        p = os.path.join(folder, f"{name}.csv")
        if os.path.exists(p):
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


# ---------------------------------------------------------------- check ---
def check(w, recs, fs_lines, groups, arm_value, gap_ms=250.0, stall_s=2.0,
          grace_s=GRACE_S, locked_slots=(), purge_max_s=PURGE_MAX_S):
    tl = Timeline(recs, fs_lines, groups, purge_max_s)
    ep = Episodes()
    notes = []
    grace = timedelta(seconds=grace_s)
    safety_of = {}
    fire_of = {}
    arm_of = {}
    for g in groups:
        safety_of[g["safety"]] = g["name"]
        arm_of[g["name"]] = int(g.get("arm_value", arm_value))
        for f in g["fire"]:
            fire_of[f] = g["name"]
    safety_slot = {g["name"]: g["safety"] for g in groups}
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
                sv = values.get(safety_slot[name], 0)
                if sv != arm_of[name]:
                    ep.add("FIRE WITHOUT ARM", slot, t,
                           f"{name}: fire slot {slot} = {v} while its safety "
                           f"slot {safety_slot[name]} = {sv}")
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
    packets = sum(int(r.get("packets") or 0) for r in w["stream"])
    nonzero = len(w["nonzero"])
    return {"episodes": ep.all(), "notes": notes, "packets": packets,
            "nonzero_packets": nonzero, "fire_packets": fire_packets,
            "armed_packets": armed_packets, "has_state": tl.has_state,
            "has_arms": tl.has_arms, "has_fs": tl.has_fs,
            "events": len(tl.events), "companion": w["companion"]}


def journal_counts(recs, fs_lines):
    c = {"arm presses": 0, "disarm presses": 0, "aborts": 0, "resets": 0,
         "faults (engine)": 0, "flamesafe disarm-all": 0,
         "flamesafe link lost": 0, "flamesafe faults": 0,
         "flamesafe overruns": 0, "flamesafe starts": 0,
         "flamesafe stops": 0}
    for r in recs:
        a = str(r.get("action", "")).lower()
        if a == "arm" and "arm pressed" in str(r.get("text", "")):
            c["arm presses"] += 1
        elif a == "disarm":
            c["disarm presses"] += 1
        elif a == "abort":
            c["aborts"] += 1
        elif a == "reset":
            c["resets"] += 1
        if r.get("fault"):
            c["faults (engine)"] += 1
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
              "or stopped with values on the wire. What it cannot mean: "
              "anything about packets the witness could not hear (a "
              "destination that is not this PC, a port it could not bind)."]
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
    ap.add_argument("--out", help="report path (default: check_report.txt in "
                                  "the witness folder)")
    a = ap.parse_args(argv)
    w = load_witness(a.witness)
    groups = w["meta"].get("groups") or []
    arm_value = int(w["meta"].get("arm_value") or 0)
    if (not groups) and a.config:
        with open(a.config, encoding="utf-8-sig") as fh:
            d = json.load(fh)
        arm_value = int(d.get("arm_value", 0))
        groups = [{"name": g["name"], "safety": int(g["safety"]),
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
                a.grace_s, locked, a.purge_max_s)
    counts = journal_counts(recs, fs)
    times = [r["_t"] for r in w["stream"]] + [r["_t"] for r in w["nonzero"]]
    span = (min(times), max(times)) if times else (None, None)
    verdict, text = report(res, counts, w,
                           f"nights={a.nights or 'none'}; flamesafe-log="
                           f"{a.flamesafe_log or 'none'}; gap {a.gap_ms:g} "
                           f"ms; stall {a.stall_s:g} s; locked slots "
                           f"{sorted(locked) or 'none'}", span)
    out = a.out or os.path.join(a.witness, "check_report.txt")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)
    print(f"Report written: {out}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
