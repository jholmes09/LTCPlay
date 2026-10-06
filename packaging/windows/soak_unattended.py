"""BENCH BUILD ONLY: the soak test unattended, for days (show PC, 2026-10-06;
install is 14 October and the PC must soak while nobody is there).

    ltcplay-soak.exe --forever        until told to stop
    ltcplay-soak.exe --days 5         for five days from the first start
    ltcplay-soak.exe --unattended-off stop after the block now running

Blocks of 110 minutes run back to back (under the BEYOND demo's 2 hours),
each a whole soak of its own in a new stamped folder, with every check the
soak makes (flame violations, engine stalls, send gaps, ...). Nothing waits
for a key: before each block the restart command from unattended.json runs
(the show PC's wrapper that rebuilds MadMapper and BEYOND through
explorer.exe; LTC Player never starts them itself), then the soak waits,
at most app_wait_min, for both to answer. A restart command that hangs is
stopped after restart_timeout_min and tried again; whatever happens, the
next block runs and the status says what went wrong. A program that
crashes in the middle of a block is brought back with its recovery command
and written down as recovered, not as a failed block.

It survives a reboot or a crash: unattended_state.json holds where the run
is, and the scheduled task (register_soak_task.ps1) starts this program
again at sign-in and after a crash; it carries on from there. Programs a
crashed soak left running are stopped first.

Every minute status.json says the block, when it started, the last block's
verdict and failures, the last restart, free disk and the last error. One
summary line per block goes in a daily text file (the soak folder and the
Desktop). Soak folders and Desktop files older than keep_days, or past
max_soak_gb in all, are deleted, oldest first, so the disk never fills.
"""
import json
import os
import shutil
import subprocess
import threading
import time

DEFAULTS = {
    "block_minutes": 110,
    "keep_days": 7,
    "max_soak_gb": 20,
    # Run before every block, e.g. "powershell -NoProfile
    # -ExecutionPolicy Bypass -File C:\\soak\\restart_apps.ps1".
    "restart_cmd": "",
    "restart_timeout_min": 20,
    "restart_tries": 2,
    "app_wait_min": 15,
    # A program not running for recover_after_s in a block: its command.
    "recover_cmds": {"BEYOND": "", "MadMapper": ""},
    "recover_after_s": 30,
    "contain": False,
    "show_seconds": None,
    "every_min": None,
}
STATE = "unattended_state.json"
STATUS = "status.json"
STOP = "STOP_UNATTENDED"
CONFIG = "unattended.json"


def run_command(cmd, timeout_s, run=subprocess.run):
    """(ok, sentence): one command through the shell, never longer than
    timeout_s; its output's last lines in the sentence."""
    t = time.time()
    try:
        r = run(cmd, shell=True, capture_output=True, text=True,
                errors="replace", timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return False, (f"did not finish within {timeout_s / 60:.0f} min "
                       f"and was stopped")
    except Exception as e:
        return False, f"could not run: {type(e).__name__}: {e}"
    tail = " | ".join((r.stdout or "").strip().splitlines()[-3:] +
                      (r.stderr or "").strip().splitlines()[-2:])
    return r.returncode == 0, (f"exit {r.returncode} after "
                               f"{time.time() - t:.0f} s"
                               + (f": {tail[:300]}" if tail else ""))


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            v = json.load(fh)
        return v if isinstance(v, dict) else default
    except (OSError, ValueError):
        return default


def save_json(path, doc):
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
        os.replace(path + ".tmp", path)
        return True
    except OSError:
        return False


def load_config(folder):
    """unattended.json in the soak folder, written with the defaults the
    first time; unknown keys are ignored, missing ones defaulted."""
    p = os.path.join(folder, CONFIG)
    doc = load_json(p)
    if doc is None:
        save_json(p, DEFAULTS)
        doc = {}
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in doc.items() if k in DEFAULTS})
    rc = dict(DEFAULTS["recover_cmds"])
    rc.update(cfg.get("recover_cmds") or {})
    cfg["recover_cmds"] = {k: v for k, v in rc.items() if v}
    cfg["block_minutes"] = max(5.0, min(float(cfg["block_minutes"]), 115.0))
    return cfg


def prune(folder, keep_days, max_gb, desktop=None, keep=(), now=None):
    """Delete stamped soak folders (and Desktop soak files) older than
    keep_days, then the oldest folders until all of them fit in max_gb.
    Never the folders in `keep`. Returns what was deleted."""
    now = now or time.time()
    gone = []
    dirs = []
    try:
        names = os.listdir(folder)
    except OSError:
        names = []
    for n in names:
        p = os.path.join(folder, n)
        if os.path.isdir(p) and n[:4].isdigit() and p not in keep:
            dirs.append((os.path.getmtime(p), p, _size(p)))
    dirs.sort()
    for m, p, _s in list(dirs):
        if now - m > keep_days * 86400:
            shutil.rmtree(p, ignore_errors=True)
            gone.append(p)
    dirs = [d for d in dirs if d[1] not in gone]
    total = sum(d[2] for d in dirs)
    while dirs and total > max_gb * 1e9:
        _m, p, s = dirs.pop(0)
        shutil.rmtree(p, ignore_errors=True)
        gone.append(p)
        total -= s
    if desktop:
        try:
            for n in os.listdir(desktop):
                p = os.path.join(desktop, n)
                if n.startswith("LTC Player soak") and n.endswith(".txt") \
                        and now - os.path.getmtime(p) > keep_days * 86400:
                    os.remove(p)
                    gone.append(p)
        except OSError:
            pass
    return gone


def _size(p):
    total = 0
    for root, _d, names in os.walk(p):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(root, n))
            except OSError:
                pass
    return total


class Unattended:
    def __init__(self, soak_mod, days=None, folder=None, desktop=None,
                 clock=time.time, sleep=time.sleep, run=run_command):
        self.m = soak_mod
        import supervisor as sup
        self.sup = sup
        self.folder = folder or os.path.join(sup.appdata_dir(), "soak")
        os.makedirs(self.folder, exist_ok=True)
        self.desktop = desktop
        self.cfg = load_config(self.folder)
        self.clock = clock
        self.sleep = sleep
        self.run_cmd = run
        st = load_json(os.path.join(self.folder, STATE))
        now = clock()
        if st and (st.get("until") is None or st["until"] > now) and \
                not os.path.exists(os.path.join(self.folder, STOP)):
            self.state = st
            self.resumed = True
        else:
            self.state = {"started": now, "block": 0,
                          "until": now + days * 86400 if days else None}
            self.resumed = False
        self.state["pid"] = os.getpid()
        self.status = {"block": self.state["block"], "block_started": None,
                       "phase": "starting", "last_verdict": None,
                       "last_failures": [], "last_restart": None,
                       "last_error": None, "recoveries": [],
                       "run_started": self.state["started"],
                       "until": self.state["until"]}
        self.current = None
        self.stopped = False
        self._status_stop = threading.Event()
        self.report_paths = []
        self.finished = False

    # ---------------------------------------------------------- files ---
    def path(self, name):
        return os.path.join(self.folder, name)

    def write_status(self):
        s = dict(self.status)
        s["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            s["free_disk_gb"] = round(shutil.disk_usage(self.folder).free
                                      / 1e9, 1)
        except OSError:
            s["free_disk_gb"] = None
        cur = self.current
        if cur is not None:
            s["block_hours_so_far"] = round(cur.hours(), 2)
        save_json(self.path(STATUS), s)

    def _status_loop(self):
        while not self._status_stop.wait(60.0):
            self.write_status()

    def daily(self, line):
        day = time.strftime("%Y-%m-%d")
        os.makedirs(self.path("daily"), exist_ok=True)
        targets = [os.path.join(self.path("daily"), f"{day}.txt")]
        if self.desktop:
            targets.append(os.path.join(
                self.desktop, f"LTC Player soak daily {day}.txt"))
        for p in targets:
            try:
                new = not os.path.exists(p)
                with open(p, "a", encoding="utf-8") as fh:
                    if new:
                        fh.write(f"LTC Player unattended soak, {day}. BENCH "
                                 f"ONLY. One line per block.\r\n")
                    fh.write(line + "\r\n")
            except OSError:
                pass
        self.report_paths = targets

    def note(self, text):
        self.m.note(text)

    # ------------------------------------------------------- the run ---
    def stop_requested(self):
        return os.path.exists(self.path(STOP)) or (
            self.state.get("until") is not None and
            self.clock() >= self.state["until"])

    def restart_apps(self):
        cmd = self.cfg["restart_cmd"]
        if not cmd:
            return True
        tries = max(1, int(self.cfg["restart_tries"]))
        for k in range(1, tries + 1):
            self.status["phase"] = f"restart command, try {k} of {tries}"
            self.write_status()
            self.note(f"unattended: restart command, try {k} of {tries}: "
                      f"{cmd}")
            ok, why = self.run_cmd(cmd, self.cfg["restart_timeout_min"] * 60)
            self.status["last_restart"] = {
                "at": time.strftime("%Y-%m-%d %H:%M:%S"), "ok": ok,
                "try": k, "result": why}
            self.note(f"unattended: restart command {'OK' if ok else 'FAILED'}"
                      f" ({why})")
            if ok:
                return True
            self.status["last_error"] = f"restart command: {why}"
        return False

    def wait_apps(self, n):
        self.status["phase"] = "waiting for MadMapper and BEYOND"
        self.write_status()
        got = self.m.wait_for_apps(
            None, (n, 0), typed=lambda: None,
            give_up_s=self.cfg["app_wait_min"] * 60)
        return not got.get("timeout")

    def one_block(self, n):
        resolved, _f, why = self.m.resolve_mode("auto")
        if resolved == "all programs":
            if not self.restart_apps() or not self.wait_apps(n):
                # Once more from the top; then the block runs whatever
                # happened, and its report says which program was missing.
                if not (self.restart_apps() and self.wait_apps(n)):
                    self.status["last_error"] = (
                        "MadMapper or BEYOND did not answer before block "
                        f"{n}; the block ran anyway")
                    self.note("unattended: " + self.status["last_error"])
        stamp = time.strftime("%Y-%m-%d_%H%M")
        s = self.m.Soak(self.cfg["block_minutes"] * 60, None,
                        folder=os.path.join(self.folder, f"{stamp} block {n}"),
                        block=(n, "an unattended run"))
        if self.cfg["show_seconds"]:
            s.show_s = float(self.cfg["show_seconds"])
        if self.cfg["every_min"]:
            s.every_min = float(self.cfg["every_min"])
        s.want_mode = "all" if resolved == "all programs" else "fallback"
        s.priority = True
        s.contain = bool(self.cfg["contain"])
        s.recover = dict(self.cfg["recover_cmds"])
        s.recover_after_s = float(self.cfg["recover_after_s"])
        self.current = s
        self.status.update(block=n, phase="block running",
                           block_started=time.strftime("%Y-%m-%d %H:%M:%S"),
                           block_folder=s.dir)
        self.write_status()
        crashed = ""
        try:
            s.run()
        except Exception as e:
            crashed = f"the block itself stopped: {type(e).__name__}: {e}"
            self.status["last_error"] = (f"block {n} stopped: "
                                         f"{type(e).__name__}: {e}")
            self.note("unattended: " + self.status["last_error"])
            try:
                s.finished = True
                s.stop_all()
                s.write_report()
            except Exception:
                pass
        items = []
        try:
            items = s.items()
        except Exception as e:
            self.status["last_error"] = f"block {n} report: {e}"
        fails = ([crashed] if crashed else []) + \
            [t for v, t, _d in items if v == "FAIL"]
        verdict = "FAILED" if fails or not items else "PASSED"
        rec = getattr(s, "recoveries", {})
        self.status.update(
            last_verdict=verdict, last_failures=fails[:12],
            last_block=n, last_block_report=s.report_paths[0],
            recoveries=[f"{a} {now} {why}" for a, lst in rec.items()
                        for now, why in lst][-8:],
            last_details={t: d[:300] for v, t, d in items
                          if v == "FAIL" or "Memory" in t or "Storage" in t
                          or "storage" in t or "Heat" in t or "heat" in t
                          or "Engine stalls" in t})
        self.daily(f"{time.strftime('%H:%M')} block {n} "
                   f"({s.hours():.2f} h): {verdict}"
                   + (": " + "; ".join(fails[:6]) if fails else "")
                   + ("; recovered: " + ", ".join(
                       f"{a} x{len(lst)}" for a, lst in rec.items())
                      if rec else "")
                   + f"; report {s.report_paths[0]}")
        return s

    def kill_orphans(self):
        """The show programs a crashed soak left running from this app
        folder: Ctrl-Break, then stopped (bench only, isolated PC)."""
        exes = [self.sup.EXE[n] for n in ("deck", "engine", "flamesafe")]
        try:
            found = self.sup.running_pids(exes)
        except Exception:
            return
        for exe in exes:
            for pid in found.get(exe, []):
                self.note(f"unattended: {exe} (pid {pid}) left running by a "
                          f"soak that stopped; stopping it")
                self.sup.send_ctrl_break(pid)
                for _ in range(40):
                    if not self.sup.pid_alive(pid):
                        break
                    self.sleep(0.5)
                else:
                    try:
                        import psutil
                        psutil.Process(pid).kill()
                    except Exception:
                        pass

    def run(self):
        self.note(("unattended soak resumed" if self.resumed else
                   "unattended soak started")
                  + (f", until {time.strftime('%Y-%m-%d %H:%M', time.localtime(self.state['until']))}"
                     if self.state.get("until") else ", until told to stop")
                  + f"; blocks of {self.cfg['block_minutes']:.0f} min; "
                  f"settings {self.path(CONFIG)}; status {self.path(STATUS)}")
        if not self.cfg["restart_cmd"]:
            self.note("unattended: no restart_cmd in unattended.json, so "
                      "MadMapper and BEYOND are not restarted between blocks")
        if os.path.exists(self.path(STOP)):
            self.note("unattended soak: stopped (Stop the unattended soak "
                      "was used). Run register_soak_task.ps1 again to start "
                      "it.")
            self.finished = True
            return True
        self.kill_orphans()
        threading.Thread(target=self._status_loop, daemon=True,
                         name="soak-status").start()
        try:
            while not self.stop_requested():
                gone = prune(self.folder, self.cfg["keep_days"],
                             self.cfg["max_soak_gb"], self.desktop)
                if gone:
                    self.note(f"unattended: deleted {len(gone)} old soak "
                              f"folder(s) and file(s)")
                self.state["block"] += 1
                save_json(self.path(STATE), self.state)
                s = self.one_block(self.state["block"])
                if s.interrupted:
                    self.stopped = True
                    break
        except KeyboardInterrupt:
            self.stopped = True
        self._status_stop.set()
        done = self.stop_requested()
        self.status["phase"] = ("finished" if done else
                                "stopped by Ctrl-C (resumes at next sign-in)")
        self.write_status()
        if done:
            # STOP stays: the task finds it at every sign-in and does
            # nothing, until register_soak_task.ps1 is run again.
            try:
                os.remove(self.path(STATE))
            except OSError:
                pass
        self.finished = True
        return True

    # Soak-like for main()'s ending.
    @property
    def passed(self):
        return True

    def stop_all(self):
        if self.current is not None and not self.current.finished:
            self.current.stop_all()

    def write_report(self):
        self.write_status()


def stop_file_path(folder):
    return os.path.join(folder, STOP)


def self_test():
    import tempfile
    d = tempfile.mkdtemp()
    try:
        old = os.path.join(d, "2026-09-01_0101 block 1")
        new = os.path.join(d, "2026-10-06_0101 block 9")
        for p in (old, new):
            os.makedirs(p)
            with open(os.path.join(p, "engine.log"), "w") as fh:
                fh.write("x" * 1000)
        os.utime(old, (time.time() - 9 * 86400,) * 2)
        os.makedirs(os.path.join(d, "daily"))
        gone = prune(d, 7, 20)
        assert gone == [old] and os.path.isdir(new) and \
            os.path.isdir(os.path.join(d, "daily")), gone
        gone = prune(d, 7, 0.0000005)        # 500 bytes: over the cap
        assert gone == [new], gone
        yield ("unattended soak: old and over-cap soak folders are deleted, "
               "oldest first, nothing else")
        cfg = load_config(d)
        assert cfg["block_minutes"] == 110 and cfg["recover_cmds"] == {}
        save_json(os.path.join(d, CONFIG), {"block_minutes": 500,
                                            "recover_cmds": {"BEYOND": "x"}})
        cfg = load_config(d)
        assert cfg["block_minutes"] == 115 and \
            cfg["recover_cmds"] == {"BEYOND": "x"}, cfg
        yield ("unattended soak: settings default, and a block is never "
               "longer than 115 minutes (the BEYOND demo stops at 2 hours)")
        ok, why = run_command("exit 3", 10)
        assert not ok and "exit 3" in why, why
        yield "unattended soak: a restart command's failure is caught and said"
    finally:
        shutil.rmtree(d, ignore_errors=True)
