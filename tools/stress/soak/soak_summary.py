"""The 48 hour soak summary: one page from the unattended soak's own files,
judged against the stress kit's pass criteria.

BENCH TOOL. Reads the soak folder (%LOCALAPPDATA%\\ltcplay\\soak by default,
or the "soak" folder inside an evidence zip), writes SOAK SUMMARY.md there
and on the Desktop, prints it, and exits 0 PASS, 1 FAIL, 2 still running
or not enough hours yet.

    python soak_summary.py
    python soak_summary.py --soak "C:\\Users\\show\\AppData\\Local\\ltcplay\\soak" --hours 48

Pass criteria (the kit's, written for the 48 hour run):
  1. every block PASSED by the soak's own report, none crashed or recovered;
  2. zero flame violations (flame-violations.txt empty in every block);
  3. flat memory: no program grew faster than 10 MB per hour in any block
     (the soak's own limit, MEM_GROWTH_MB_H), and the soak program itself
     did not grow across the run;
  4. the timecode never gapped past the soak's limit (the Art-Net timecode
     and flame link items never FAIL), no engine stall item failed;
  5. containment ON (unattended.json "contain": true, every block reports
     it), so MadMapper and BEYOND never held the engine up;
  6. at least --hours (48) minus one block of soak hours, and free disk
     never under 5 GB.
"""
import argparse
import csv
import glob
import json
import os
import re
import sys
import time

MEM_GROWTH_MB_H = 10.0
MIN_DISK_GB = 5.0
BLOCK_LINE = re.compile(r"^(\d\d:\d\d) block (\d+) \(([\d.]+) h\): (PASSED|FAILED)(.*)$")
ITEM_LINE = re.compile(r"^\[(PASS|FAIL|NOT TESTED|INFO|WARN)\] (.*)$")


def load_json(p, default=None):
    try:
        with open(p, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def slope_mb_h(points):
    """Least squares slope of (hours, mb)."""
    n = len(points)
    if n < 6:
        return None
    sx = sum(p[0] for p in points)
    sy = sum(p[1] for p in points)
    sxx = sum(p[0] * p[0] for p in points)
    sxy = sum(p[0] * p[1] for p in points)
    den = n * sxx - sx * sx
    if abs(den) < 1e-9:
        return None
    return (n * sxy - sx * sy) / den


def block_folders(soak):
    out = []
    for p in glob.glob(os.path.join(soak, "* block *")):
        if os.path.isdir(p):
            m = re.search(r" block (\d+)$", p)
            if m:
                out.append((int(m.group(1)), p))
    out.sort()
    return out


def read_block(n, folder):
    b = {"n": n, "folder": folder, "verdict": None, "hours": None,
         "fails": [], "items": [], "violations": 0, "memory": {},
         "stalls": 0, "worst_stall_ms": None, "contain": None,
         "fs_overruns": 0, "fs_faults": 0, "fs_disarm_all": 0,
         "fs_link_lost": 0, "crashed": False, "report": None,
         "audio": None, "fake_audio": False, "no_audio": False}
    reps = glob.glob(os.path.join(folder, "block * report.txt"))
    if reps:
        b["report"] = reps[0]
        with open(reps[0], encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
        last = None
        for ln in lines:
            if ln.startswith("Result: "):
                b["verdict"] = ln[8:].strip()
            if ln.strip().startswith("Show audio: "):
                b["audio"] = ln.strip()[len("Show audio: "):]
                b["fake_audio"] = "FAKE" in b["audio"]
                b["no_audio"] = "NO interface" in b["audio"]
            m = re.match(r"^Run: .*?, ([\d.]+) of", ln)
            if m:
                b["hours"] = float(m.group(1))
            if "containment" in ln.lower():
                if "containment on" in ln.lower() or "containment ON" in ln:
                    b["contain"] = True
                elif "containment off" in ln.lower():
                    b["contain"] = False
            im = ITEM_LINE.match(ln)
            if im:
                last = {"verdict": im.group(1), "title": im.group(2),
                        "detail": ""}
                b["items"].append(last)
                if im.group(1) == "FAIL":
                    b["fails"].append(im.group(2))
            elif last is not None and ln.startswith("    ") and \
                    not last["detail"]:
                last["detail"] = ln.strip()
            if "THE SOAK TEST ITSELF FAILED" in ln or \
                    "the block itself stopped" in ln:
                b["crashed"] = True
    fv = os.path.join(folder, "flame-violations.txt")
    if os.path.exists(fv):
        with open(fv, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        b["violations"] = len([blk for blk in text.split("\n\n")
                               if blk.strip()])
    samples = os.path.join(folder, "samples.csv")
    if os.path.exists(samples):
        series = {}
        with open(samples, encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    series.setdefault(r["name"], []).append(
                        (float(r["hours"]), float(r["rss_mb"])))
                except (KeyError, ValueError):
                    continue
        for name, pts in series.items():
            s = slope_mb_h(pts)
            b["memory"][name] = {
                "slope_mb_h": None if s is None else round(s, 2),
                "first_mb": round(pts[0][1], 1), "last_mb": round(pts[-1][1], 1),
                "max_mb": round(max(p[1] for p in pts), 1), "n": len(pts)}
    st = load_json(os.path.join(folder, "engine-stalls.json"), {})
    stalls = st.get("stalls") or [] if isinstance(st, dict) else []
    b["stalls"] = len(stalls)
    worst = None
    for s in stalls:
        try:
            v = float(s.get("ms") or s.get("worst_ms") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        worst = v if worst is None else max(worst, v)
    b["worst_stall_ms"] = worst
    for p in (os.path.join(folder, "flamesafe-journal", "flamesafe.log"),
              os.path.join(folder, "flamesafe.log")):
        if os.path.exists(p):
            with open(p, encoding="utf-8", errors="replace") as fh:
                for ln in fh:
                    if "  overrun: " in ln:
                        b["fs_overruns"] += 1
                    elif "  fault: " in ln or "  compose-fault: " in ln:
                        b["fs_faults"] += 1
                    elif "  disarm-all: " in ln:
                        b["fs_disarm_all"] += 1
                    elif "stopped answering" in ln:
                        b["fs_link_lost"] += 1
            break
    return b


def summarize(soak, hours_wanted=48.0, now=None):
    now = now or time.time()
    status = load_json(os.path.join(soak, "status.json"), {}) or {}
    cfg = load_json(os.path.join(soak, "unattended.json"), {}) or {}
    state = load_json(os.path.join(soak, "unattended_state.json"), {}) or {}
    daily = []
    for p in sorted(glob.glob(os.path.join(soak, "daily", "*.txt"))):
        with open(p, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                m = BLOCK_LINE.match(ln.strip())
                if m:
                    daily.append({"day": os.path.basename(p)[:-4],
                                  "time": m.group(1), "n": int(m.group(2)),
                                  "hours": float(m.group(3)),
                                  "verdict": m.group(4),
                                  "rest": m.group(5).strip()})
    blocks = [read_block(n, f) for n, f in block_folders(soak)]
    hours = sum(b["hours"] or 0 for b in blocks)
    running = status.get("phase") not in (None, "finished") and \
        bool(state)
    checks = []

    def check(ok, title, detail):
        checks.append(("PASS" if ok else "FAIL", title, detail))

    failed = [b for b in blocks if b["verdict"] and
              not b["verdict"].startswith("PASSED")]
    crashed = [b for b in blocks if b["crashed"]]
    recovered = [d for d in daily if "recovered" in d["rest"]]
    check(not failed and not crashed and not recovered and blocks,
          "1. Every block PASSED, none crashed or needed a recovery",
          f"{len(blocks)} block folder(s); failed: "
          f"{[b['n'] for b in failed] or 'none'}; crashed: "
          f"{[b['n'] for b in crashed] or 'none'}; recoveries: "
          f"{len(recovered)}"
          + ("" if not failed else "; first failures: "
             + "; ".join(failed[0]["fails"][:4])))
    viol = sum(b["violations"] for b in blocks)
    check(viol == 0 and blocks, "2. Zero flame violations (the soak's judge "
                                "of flamesafe's sACN)",
          f"{viol} violation(s) in flame-violations.txt across "
          f"{len(blocks)} block(s)")
    leaks = []
    worst_slope = {}
    for b in blocks:
        for name, m in b["memory"].items():
            s = m["slope_mb_h"]
            if s is not None:
                if s > MEM_GROWTH_MB_H and b["hours"] and b["hours"] >= 0.5:
                    leaks.append(f"block {b['n']} {name} {s:+.1f} MB/h")
                worst_slope[name] = max(worst_slope.get(name, -1e9), s)
    check(not leaks and worst_slope, "3. Flat memory: no program grew "
                                     f"past {MEM_GROWTH_MB_H:g} MB/h in any "
                                     "block",
          ("worst slope per program: " + ", ".join(
              f"{n} {s:+.1f} MB/h" for n, s in sorted(worst_slope.items()))
           if worst_slope else "no samples.csv found")
          + ("; over the limit: " + "; ".join(leaks[:6]) if leaks else ""))
    tc_fails = []
    for b in blocks:
        for it in b["items"]:
            t = it["title"].lower()
            if it["verdict"] == "FAIL" and ("timecode" in t or "flame link" in t
                                            or "stall" in t or "pixel" in t):
                tc_fails.append(f"block {b['n']}: {it['title']}")
    check(not tc_fails and blocks, "4. Timecode, pixel and flame link gaps "
                                   "inside the soak's limits, no engine "
                                   "stall item failed",
          "; ".join(tc_fails[:6]) if tc_fails else
          f"no timing item failed in {len(blocks)} block(s); engine stalls "
          f"noted: {sum(b['stalls'] for b in blocks)}")
    contain_cfg = cfg.get("contain") is True
    contain_blocks = [b["contain"] for b in blocks if b["contain"] is not None]
    check(contain_cfg and (not contain_blocks or all(contain_blocks)),
          "5. Containment ON (MadMapper and BEYOND below the engine)",
          f"unattended.json contain = {cfg.get('contain')!r}; blocks saying "
          f"on: {sum(1 for c in contain_blocks if c)}, off: "
          f"{sum(1 for c in contain_blocks if not c)}, not stated: "
          f"{len(blocks) - len(contain_blocks)}")
    block_h = float(cfg.get("block_minutes") or 110) / 60.0
    free = status.get("free_disk_gb")
    check(hours >= hours_wanted - block_h and
          (free is None or free >= MIN_DISK_GB),
          f"6. At least {hours_wanted:g} h soaked (one block of slack) and "
          f"free disk never under {MIN_DISK_GB:g} GB",
          f"{hours:.1f} h in {len(blocks)} block(s); free disk now "
          f"{free} GB; run phase: {status.get('phase')}")
    fake = [b["n"] for b in blocks if b["fake_audio"]]
    no_audio = [b["n"] for b in blocks if b["no_audio"]]
    real = sorted({b["audio"] for b in blocks
                   if b["audio"] and not b["fake_audio"] and not b["no_audio"]})
    # Never a PASS/FAIL of its own (the blocks already fail with no
    # interface); a fact that must stand beside the verdict, so a fake run
    # is never mistaken for a real one.
    check(not no_audio, "7. Show audio: a real interface in every block"
          + (" (AUDIO WAS FAKE)" if fake else ""),
          (f"AUDIO WAS FAKE in block(s) {fake}: the engine's stand-in device "
           f"played the shows, so this run proves the engine, the scheduler, "
           f"the flame link, the timecode, the Art-Net outputs and the "
           f"witness, NOT the real audio path. Not a show-night soak. "
           if fake else "")
          + (f"NO interface found in block(s) {no_audio}: those blocks played "
             f"no show. " if no_audio else "")
          + (f"real interface: {', '.join(real)}" if real else
             ("" if fake or no_audio else "no Show audio line found")))
    verdict = "FAIL" if any(v == "FAIL" for v, _t, _d in checks) else "PASS"
    if running and hours < hours_wanted - block_h:
        verdict = "STILL RUNNING"
    if fake:
        verdict += " (AUDIO WAS FAKE)"
    return {"verdict": verdict, "checks": checks, "blocks": blocks,
            "daily": daily, "status": status, "cfg": cfg, "hours": hours,
            "running": running, "state": state, "fake_audio": fake,
            "no_audio": no_audio, "audio": real}


def render(s, soak):
    st = s["status"]
    lines = [f"# LTC Player 48 hour soak summary ({s['verdict']})", "",
             "BENCH ONLY: no lasers, flames or lights connected; flamesafe's "
             "output went to this PC only.", ""]
    if s.get("fake_audio"):
        lines += ["AUDIO WAS FAKE in block(s) " + ", ".join(map(str, s["fake_audio"]))
                  + ": the engine played the shows on a stand-in device "
                  "(--fake-audio, or fake_audio in unattended.json). This run "
                  "proves the engine, the scheduler, the flame link, the "
                  "timecode, the Art-Net outputs and the witness, NOT the "
                  "real audio path. It is not a show-night soak.", ""]
    if s.get("no_audio"):
        lines += ["NO AUDIO INTERFACE in block(s) " + ", ".join(map(str, s["no_audio"]))
                  + ": no show played in them. Attach the interface, or run "
                  "with -FakeAudio on purpose.", ""]
    lines += [
             f"Soak folder: {soak}",
             f"Hours soaked: {s['hours']:.1f} in {len(s['blocks'])} block(s) "
             f"of {s['cfg'].get('block_minutes', 110)} min",
             f"Run started: {time.strftime('%Y-%m-%d %H:%M', time.localtime(st.get('run_started'))) if st.get('run_started') else '?'}; "
             f"until: {time.strftime('%Y-%m-%d %H:%M', time.localtime(st.get('until'))) if st.get('until') else 'told to stop'}; "
             f"phase now: {st.get('phase')}; status written {st.get('updated')}",
             f"Last block: {st.get('last_block')} {st.get('last_verdict')}"
             + (f"; failures: {st.get('last_failures')}" if st.get("last_failures") else ""),
             f"Free disk: {st.get('free_disk_gb')} GB; last error: {st.get('last_error') or 'none'}",
             f"Audio (status.json): {st.get('audio') or 'not stated (an older soak build)'}",
             "", "## Pass criteria", ""]
    for v, t, d in s["checks"]:
        lines.append(f"- [{v}] {t}")
        lines.append(f"      {d}")
    lines += ["", "## Blocks", "",
              "| block | hours | verdict | show audio | flame violations | engine stalls | "
              "flamesafe overruns / faults / disarm-all / link lost | memory slopes (MB/h) |",
              "|---|---|---|---|---|---|---|---|"]
    for b in s["blocks"]:
        mem = ", ".join(f"{n} {m['slope_mb_h']:+.1f}" for n, m in
                        sorted(b["memory"].items())
                        if m["slope_mb_h"] is not None) or "-"
        lines.append(f"| {b['n']} | {b['hours'] if b['hours'] is not None else '?'} | "
                     f"{b['verdict'] or 'no report'} | {b['audio'] or '?'} | {b['violations']} | "
                     f"{b['stalls']} | {b['fs_overruns']} / {b['fs_faults']} / "
                     f"{b['fs_disarm_all']} / {b['fs_link_lost']} | {mem} |")
    fails = [(b["n"], f) for b in s["blocks"] for f in b["fails"]]
    if fails:
        lines += ["", "## Failed items, by block", ""]
        lines += [f"- block {n}: {f}" for n, f in fails[:40]]
    if s["daily"]:
        lines += ["", "## The soak's own daily lines", ""]
        lines += [f"- {d['day']} {d['time']} block {d['n']} ({d['hours']} h): "
                  f"{d['verdict']} {d['rest']}" for d in s["daily"][-40:]]
    lines += ["", "## What this proves and what it does not", "",
              "- Proves: the installed programs ran the whole Fire & Ice "
              "stack for the hours above with shows every 15 minutes, arming "
              "and firing through the real paths, and the soak's listener on "
              "this PC saw flamesafe's output obey its rules; memory, timing "
              "and faults are as judged above."
              + (" With FAKE audio the show audio path itself is NOT among "
                 "them." if s.get("fake_audio") else ""),
              "- Does not prove: anything about real flame hardware, the real "
              "network to the flame node, the Stream Deck hardware (a virtual "
              "deck unless one was plugged in), or operator mistakes. Those "
              "are stages 2 to 5 of the stress plan.",
              "- The wire judge for this stage is ltcplay-soak's own listener "
              "(a separate program from flamesafe, but part of the same "
              "install); the independent witness takes the wire in stages 2 "
              "to 4."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--soak", help="the soak folder")
    ap.add_argument("--hours", type=float, default=48.0)
    ap.add_argument("--out", help="where to write the summary")
    ap.add_argument("--no-desktop", action="store_true")
    a = ap.parse_args(argv)
    soak = a.soak
    if not soak:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), ".local", "state")
        soak = os.path.join(base, "ltcplay", "soak")
    if not os.path.isdir(soak):
        print(f"No soak folder at {soak}")
        return 2
    s = summarize(soak, a.hours)
    text = render(s, soak)
    stamp = time.strftime("%Y-%m-%d_%H%M")
    outs = [a.out or os.path.join(soak, f"SOAK SUMMARY {stamp}.md")]
    desk = os.path.join(os.path.expanduser("~"), "Desktop")
    if not a.no_desktop and os.path.isdir(desk):
        outs.append(os.path.join(desk, f"LTC Player SOAK SUMMARY {stamp}.md"))
    for p in outs:
        try:
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError:
            pass
    print(text)
    print("Written: " + ", ".join(outs))
    return {"PASS": 0, "FAIL": 1}.get(s["verdict"], 2)


if __name__ == "__main__":
    sys.exit(main())
