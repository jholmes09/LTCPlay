"""The flame frame timing harness: how late the engine's flame frames are,
and what else was running in the engine when one was.

BENCH TOOL. It runs the repo's own soak (packaging/windows/soak.py: the
real engine, flamesafe and deck programs from this checkout, the fake show
audio, the scheduler starting a show every few minutes, nothing leaving
this machine) and adds, around it:

  - N status pollers, as the rack screens and the iPad do: GET
    /api/remote/status, /api/state and /api/conductor, each every 250 ms;
  - inside the engine (a sitecustomize module this script writes into its
    own folder and puts first on PYTHONPATH, so nothing in the repo changes
    for it): every flame frame the sender thread sends, timed on that
    thread; Python's garbage collector, each pass timed; an announcement's
    work every 20 s (a 45 s stereo WAV read and decoded with announce's own
    _wav_info, then resampled for the show with its own to_show_mix, to 19
    outputs at 48 kHz); a config watch pass of the schedule files every
    second (configwatch.file_hash); the HTTP requests, each timed; and a
    2 ms "GIL probe" thread that notes every wake 10 ms or more late;
  - in the soak's own flame link relay (the UDP listener standing in for
    flamesafe): the time each flame frame arrived.

At the end it prints, for the sender thread and for the listener: the
interval histogram, the worst, the 99th percentile, and the count over 50,
75, 100 and 150 ms; and for every interval over 50 ms on the sender, what
overlapped it (a GC pass, an announcement, a request, a GIL probe stall).
Everything is also in flame_timing.json in the output folder.

    python3 tools/stress/flame_timing/flame_timing.py --minutes 8
    python3 tools/stress/flame_timing/flame_timing.py --minutes 8 \\
        --pollers 6 --universes 170 --no-announce

Windows or Linux, Python 3.11 or later, numpy and zstandard installed (the
same as the selftest). It refuses to start while anything answers on the
engine's port 7878.
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
PORT = 7878
LIMITS_MS = (50, 75, 100, 150)

SITE = r'''
import atexit, gc, json, os, sys, threading, time
_OUT = os.environ.get("FLAME_TIMING_DIR")
_ROLE = os.path.basename(sys.argv[0]) if sys.argv else ""
_pc = time.perf_counter
if _OUT and _ROLE == "entry_engine.py":
    _lk = threading.Lock()
    _D = {"frames": [], "took": [], "gc": [], "acts": [], "gil": [],
          "gc_total": 0}
    _gc_t = [0.0]

    def _gc_cb(phase, info):
        if phase == "start":
            _gc_t[0] = _pc()
        else:
            d = _pc() - _gc_t[0]
            _D["gc_total"] += 1
            if d >= 0.001:
                _D["gc"].append((_gc_t[0], d * 1000.0, info.get("generation"),
                                 info.get("collected", 0)))
    gc.callbacks.append(_gc_cb)

    def _act(name, t0):
        d = _pc() - t0
        if d >= 0.002:
            with _lk:
                _D["acts"].append((t0, d * 1000.0, name,
                                   threading.current_thread().name))

    def _wrap(obj, attr, name):
        f = getattr(obj, attr)

        def w(*a, **k):
            t0 = _pc()
            try:
                return f(*a, **k)
            finally:
                _act(name if not callable(name) else name(a), t0)
        setattr(obj, attr, w)

    def _dump():
        try:
            with _lk:
                doc = dict(_D)
            tmp = os.path.join(_OUT, "engine_timing.json.tmp")
            with open(tmp, "w") as fh:
                json.dump(doc, fh)
            os.replace(tmp, os.path.join(_OUT, "engine_timing.json"))
        except Exception as e:
            print("flame_timing: dump failed", e, flush=True)

    def _gil_probe():
        while True:
            t0 = _pc()
            time.sleep(0.002)
            late = _pc() - t0 - 0.002
            if late >= 0.010:
                _D["gil"].append((t0, late * 1000.0))

    def _ann_load():
        import numpy as np
        from ltcplay import announce as A
        import wave
        path = os.path.join(_OUT, "ann.wav")
        n = 44100 * 45
        x = (np.sin(np.arange(n) * 0.05) * 8000).astype(np.int16)
        st = np.stack([x, x], axis=1)
        with wave.open(path, "wb") as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100)
            w.writeframes(st.tobytes())
        while True:
            time.sleep(20.0)
            t0 = _pc()
            pcm, ch, rate, _l = A._wav_info(path, decode=True)
            _act("announce: read and decode a 45 s WAV", t0)
            t0 = _pc()
            A.to_show_mix(pcm, rate, 48000, 19)
            _act("announce: resample for the show (to_show_mix)", t0)

    def _cfg_load():
        from ltcplay import configwatch as CW
        while True:
            time.sleep(1.0)
            d = os.environ.get("LOCALAPPDATA", "")
            t0 = _pc()
            for root, _dirs, files in os.walk(os.path.dirname(d)):
                for f in files:
                    if f.endswith(".json") and "timing" not in f:
                        CW.file_hash(os.path.join(root, f))
            _act("config watch: hash every settings file", t0)

    def _boot():
        while "ltcplay.flamelink" not in sys.modules:
            time.sleep(0.05)
        from ltcplay import flamelink as FL
        orig = FL.FlameLink.send_frame

        def send_frame(self, *a, **k):
            t0 = _pc()
            try:
                return orig(self, *a, **k)
            finally:
                if threading.current_thread().name == "ltcplay-flame-link":
                    _D["frames"].append(t0)
                    d = _pc() - t0
                    if d >= 0.002:
                        _D["took"].append((t0, d * 1000.0))
        FL.FlameLink.send_frame = send_frame
        while "ltcplay.web" not in sys.modules:
            time.sleep(0.05)
        from ltcplay import web as W
        _wrap(W.Handler, "do_GET",
              lambda a: "request GET " + str(getattr(a[0], "path", ""))
              .split("?")[0])
        _wrap(W.Handler, "do_POST",
              lambda a: "request POST " + str(getattr(a[0], "path", ""))
              .split("?")[0])
        if os.environ.get("FLAME_TIMING_ANNOUNCE") == "1":
            threading.Thread(target=_ann_load, daemon=True,
                             name="timing-announce-load").start()
        if os.environ.get("FLAME_TIMING_CONFIG") == "1":
            threading.Thread(target=_cfg_load, daemon=True,
                             name="timing-config-load").start()
        while True:
            time.sleep(5.0)
            _dump()
    threading.Thread(target=_boot, daemon=True, name="timing-boot").start()
    threading.Thread(target=_gil_probe, daemon=True,
                     name="timing-gil-probe").start()
    atexit.register(_dump)

if _OUT and _ROLE == "soak.py":
    _arr = []

    def _patch():
        while True:
            m = sys.modules.get("__main__")
            S = getattr(m, "Soak", None)
            if S is not None:
                break
            time.sleep(0.05)
        orig = S.on_flame

        def on_flame(self, b, t, addr=None):
            if b[:160].find(b'"t":"flame"') >= 0:
                _arr.append(t)
            return orig(self, b, t, addr)
        S.on_flame = on_flame
        while True:
            time.sleep(5.0)
            _dump()

    def _dump():
        try:
            tmp = os.path.join(_OUT, "listener_timing.json.tmp")
            with open(tmp, "w") as fh:
                json.dump({"frames": list(_arr)}, fh)
            os.replace(tmp, os.path.join(_OUT, "listener_timing.json"))
        except Exception:
            pass
    threading.Thread(target=_patch, daemon=True, name="timing-boot").start()
    atexit.register(_dump)
'''


def stats(times, start=None, end=None):
    """The intervals between `times` (seconds) as a summary in ms."""
    ts = sorted(t for t in times if (start is None or t >= start)
                and (end is None or t <= end))
    gaps = [(b - a) * 1000.0 for a, b in zip(ts, ts[1:])]
    if not gaps:
        return {"frames": len(ts), "intervals": 0}
    s = sorted(gaps)
    hist = {}
    for g in gaps:
        k = ("<30" if g < 30 else "30-40" if g < 40 else "40-50" if g < 50
             else "50-75" if g < 75 else "75-100" if g < 100
             else "100-150" if g < 150 else ">=150")
        hist[k] = hist.get(k, 0) + 1
    return {"frames": len(ts), "intervals": len(gaps),
            "mean_ms": round(sum(gaps) / len(gaps), 2),
            "p99_ms": round(s[min(len(s) - 1, int(len(s) * 0.99))], 1),
            "p999_ms": round(s[min(len(s) - 1, int(len(s) * 0.999))], 1),
            "worst_ms": round(s[-1], 1),
            "over_ms": {str(L): sum(1 for g in gaps if g > L)
                        for L in LIMITS_MS},
            "histogram": hist}


def late_windows(times, over_ms=50.0):
    ts = sorted(times)
    return [(a, b, (b - a) * 1000.0) for a, b in zip(ts, ts[1:])
            if (b - a) * 1000.0 > over_ms]


def overlaps(win, eng):
    a, b, _ms = win
    out = []
    for t0, ms, gen, _c in eng.get("gc", []):
        if t0 < b and t0 + ms / 1000.0 > a and ms >= 2:
            out.append(f"GC gen {gen} {ms:.0f} ms")
    for t0, ms, name, th in eng.get("acts", []):
        if t0 < b and t0 + ms / 1000.0 > a:
            out.append(f"{name} {ms:.0f} ms ({th})")
    for t0, ms in eng.get("gil", []):
        if t0 < b and t0 + ms / 1000.0 > a:
            out.append(f"GIL probe {ms:.0f} ms late")
    for t0, ms in eng.get("took", []):
        if a <= t0 <= b:
            out.append(f"the frame itself took {ms:.0f} ms")
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def poller(path, stop, counts):
    while not stop.is_set():
        t0 = time.time()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}",
                                        timeout=3) as r:
                r.read()
            counts[path] = counts.get(path, 0) + 1
        except Exception:
            counts["errors"] = counts.get("errors", 0) + 1
        stop.wait(max(0.0, 0.25 - (time.time() - t0)))


def engine_up():
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
            return True
    except OSError:
        return False


def report(out, label):
    def load(n):
        try:
            with open(os.path.join(out, n)) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}
    eng, lis = load("engine_timing.json"), load("listener_timing.json")
    frames = eng.get("frames", [])
    # The first 20 s are start-up (imports, the first show load); counted
    # apart so a cold start is not read as the steady state.
    t0 = frames[0] + 20.0 if frames else None
    doc = {"label": label,
           "sender": stats(frames, start=t0),
           "listener": stats(lis.get("frames", []), start=t0),
           "sender_startup": stats(frames, end=t0),
           "gc_passes": eng.get("gc_total", 0),
           "gc_over_10ms": sorted(round(g[1], 1) for g in eng.get("gc", [])
                                  if g[1] >= 10)[-10:],
           "acts_worst": sorted(((round(a[1], 1), a[2]) for a in
                                 eng.get("acts", [])), reverse=True)[:12],
           "gil_probe_late": len(eng.get("gil", [])),
           "late": [{"at_s": round(a - frames[0], 2), "ms": round(ms, 1),
                     "with": overlaps((a, b, ms), eng)}
                    for a, b, ms in late_windows(frames)] if frames else []}
    with open(os.path.join(out, "flame_timing.json"), "w") as fh:
        json.dump(doc, fh, indent=1)
    print(json.dumps(doc, indent=1))
    return doc


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--minutes", type=float, default=8.0)
    ap.add_argument("--pollers", type=int, default=4)
    ap.add_argument("--universes", type=int, default=1)
    ap.add_argument("--show-seconds", type=int, default=100)
    ap.add_argument("--every-min", type=int, default=3)
    ap.add_argument("--no-announce", action="store_true")
    ap.add_argument("--no-config", action="store_true")
    ap.add_argument("--label", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--report-only", default="")
    a = ap.parse_args(argv)
    if a.report_only:
        report(a.report_only, a.label)
        return 0
    if engine_up():
        print(f"Something already answers on 127.0.0.1:{PORT}; stop it "
              f"first.")
        return 2
    out = a.out or tempfile.mkdtemp(prefix="flame_timing_")
    os.makedirs(out, exist_ok=True)
    site = os.path.join(out, "site")
    os.makedirs(site, exist_ok=True)
    with open(os.path.join(site, "sitecustomize.py"), "w") as fh:
        fh.write(SITE)
    home = os.path.join(out, "home")
    os.makedirs(home, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([site, REPO] + (
        [env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    env["FLAME_TIMING_DIR"] = out
    env["FLAME_TIMING_ANNOUNCE"] = "0" if a.no_announce else "1"
    env["FLAME_TIMING_CONFIG"] = "0" if a.no_config else "1"
    env["HOME"] = home
    env["USERPROFILE"] = home
    env.pop("LOCALAPPDATA", None)
    soak = [sys.executable, os.path.join(REPO, "packaging", "windows",
                                         "soak.py"),
            "--minutes", str(a.minutes), "--no-wait", "--fake-audio",
            "--mode", "fallback", "--show-seconds", str(a.show_seconds),
            "--every-min", str(a.every_min)]
    if a.universes > 1:
        soak += ["--universes", str(a.universes)]
    print(f"flame_timing: output in {out}", flush=True)
    log = open(os.path.join(out, "soak.out"), "w")
    p = subprocess.Popen(soak, cwd=os.path.join(REPO, "packaging", "windows"),
                         env=env, stdout=log, stderr=subprocess.STDOUT)
    stop = threading.Event()
    counts = {}
    while p.poll() is None and not engine_up():
        time.sleep(0.5)
    ths = []
    for i in range(a.pollers):
        for path in ("/api/remote/status", "/api/state", "/api/conductor"):
            t = threading.Thread(target=poller, args=(path, stop, counts),
                                 daemon=True)
            t.start()
            ths.append(t)
    p.wait()
    stop.set()
    print(f"flame_timing: soak exited {p.returncode}; polls {counts}",
          flush=True)
    report(out, a.label)
    return 0


if __name__ == "__main__":
    sys.exit(main())
