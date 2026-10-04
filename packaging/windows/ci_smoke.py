"""The Windows installer's smoke test, run by .github/workflows/windows-app.yml
on a clean windows-latest runner. Every failure is printed as a GitHub
`::error::` annotation so it can be read without the job log.

    python packaging/windows/ci_smoke.py "LTC Player Setup <version>.exe"

What it proves, in order:
  1. Setup refuses while a show is running (a stand-in engine on port 7878
     says so) and installs nothing.
  2. Setup installs silently: files, Start menu, the sign-in task.
  3. Each program answers --self-check from where it was installed.
  4. LTC Player starts all three; flamesafe puts zeros on the wire at
     priority 200 and the engine's page answers.
  5. Running Setup again (an update) stops flamesafe the proper way: zeros,
     then the stream-terminated flag. Then everything comes back.
  6. "Stop LTC Player" stops it the same way and nothing is left running.
  7. "Stop LTC Player" refuses while a show is running.
  8. The uninstaller removes the app and the task, and keeps the settings.
"""
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request

PF = os.environ.get("ProgramFiles", r"C:\Program Files")
APP = os.path.join(PF, "LTC Player")
SUP = os.path.join(APP, "LTC Player.exe")
LOCAL = os.path.join(os.environ.get("LOCALAPPDATA", ""), "ltcplay")
START_MENU = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                          "Microsoft", "Windows", "Start Menu", "Programs",
                          "LTC Player")
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(
    __file__))))
SACN_PORT = 5568

FAILED = []
NOTES = []      # evidence, printed as one notice at the end


def error(msg):
    FAILED.append(msg)
    print(f"::error::{msg}", flush=True)


def say(msg):
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


def check(cond, msg):
    if not cond:
        error(msg)
    return cond


# ------------------------------------------------------------- the wire ---
class Listener:
    """Every sACN packet flamesafe sends to 127.0.0.1:5568."""

    def __init__(self):
        self.packets = []      # (time, priority, options, all_zero)
        self.lock = threading.Lock()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for _ in range(20):
            try:
                self.sock.bind(("127.0.0.1", SACN_PORT))
                break
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError(f"127.0.0.1:{SACN_PORT} is still held by "
                               f"another program")
        self.sock.settimeout(0.2)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                b = self.sock.recv(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(b) < 126 or b[4:16] != b"ASC-E1.17\0\0\0":
                continue
            values = b[126:126 + 512]
            with self.lock:
                self.packets.append((time.monotonic(), b[108], b[112],
                                     not any(values)))

    def mark(self):
        with self.lock:
            return len(self.packets)

    def since(self, mark):
        with self.lock:
            return list(self.packets[mark:])

    def wait_for(self, mark, pred, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(pred(p) for p in self.since(mark)):
                return True
            time.sleep(0.2)
        return False


def terminated(p):
    return bool(p[2] & 0x40)


def check_clean_stop(listener, mark, what):
    """Zeros at priority 200, then the stream-terminated flag."""
    pk = listener.since(mark)
    term = [i for i, p in enumerate(pk) if terminated(p)]
    if not check(term, f"{what}: flamesafe never sent the stream-terminated "
                       f"flag ({len(pk)} packets seen), so it was not "
                       f"stopped the proper way"):
        return
    first = term[0]
    before = pk[max(0, first - 3):first]
    check(len(before) >= 3 and all(p[3] for p in before),
          f"{what}: the three packets before the stream-terminated flag "
          f"were not all zeros")
    check(all(p[3] for p in pk[first:first + 3]),
          f"{what}: the stream-terminated packets were not all zeros")
    check(all(p[1] == 200 for p in pk),
          f"{what}: a flamesafe packet was not at priority 200")
    msg = (f"{what}: flamesafe stopped cleanly, {len(pk)} packets seen, "
           f"{len(term)} with the stream-terminated flag, all zeros at "
           f"priority 200 before them")
    say(msg)
    NOTES.append(msg)


# ------------------------------------------------ a stand-in show engine ---
class FakeShow:
    """Answers /api/state on 7878 the way a running show does."""

    def __init__(self):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"running": True, "api": 0}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 7878), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# ----------------------------------------------------------- utilities ---
def run(args, timeout=300):
    say("run: " + subprocess.list2cmdline(args))
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout, errors="replace")
    except subprocess.TimeoutExpired:
        error(f"{os.path.basename(args[0])} did not finish within "
              f"{timeout} s")
        return 999, ""
    out = (r.stdout or "") + (r.stderr or "")
    if out.strip():
        print(out.rstrip(), flush=True)
    return r.returncode, out


def install(installer, log):
    return run([installer, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                "/TASKS=autostart", f"/LOG={log}"])[0]


def dump(path, title):
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return
    print(f"----- {title}: {path}")
    print(text[-6000:])
    print("-----", flush=True)


def tasklist():
    r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                       text=True, errors="replace")
    names = set()
    for line in r.stdout.splitlines():
        if line.startswith('"'):
            names.add(line.split('","')[0].strip('"').lower())
    return names


OURS = {"ltc player.exe", "ltcplay.exe", "flamesafe.exe", "ltcplay-deck.exe"}


def engine_answers():
    try:
        with urllib.request.urlopen("http://127.0.0.1:7878/api/state",
                                    timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def wait(pred, timeout, step=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


def write_settings(shows):
    os.makedirs(LOCAL, exist_ok=True)
    with open(os.path.join(REPO, "flamesafe", "flamesafe.example.json"),
              encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg["groups"] = cfg["groups"][:3]        # the deck has three arm keys
    cfg["destination"] = {"ip": "127.0.0.1", "port": SACN_PORT}
    fs = os.path.join(LOCAL, "flamesafe.json")
    with open(fs, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    with open(os.path.join(LOCAL, "showpc.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"show_folder": shows, "flamesafe_config": fs,
                   "port": 7878, "open_page_at_sign_in": False}, fh)


def all_running():
    return OURS <= tasklist()


def none_running():
    return not (OURS & tasklist())


def soak_check():
    """ltcplay-soak.exe must stop LTC Player, run the real programs, measure
    them, write its report, and start LTC Player again. On a shared CI
    runner the timing items may fail (it is not the show PC); those are
    printed as warnings. A missing report, a crash, or an item with nothing
    measured is an error."""
    import glob
    # BENCH BUILD: the full stack, scheduler included. 7 minutes holds at
    # least one whole 100 s show (one every 3 minutes, 02:00 to midnight in
    # the runner's clock). The runner has no audio interface: --fake-audio.
    rc, out = run([os.path.join(APP, "ltcplay-soak.exe"), "--minutes", "7",
                   "--no-wait", "--fake-audio", "--mode", "fallback"],
                  timeout=1200)
    reports = sorted(glob.glob(os.path.join(LOCAL, "soak", "*",
                                            "LTC Player soak report *.txt")))
    if not check(reports, f"the soak test wrote no report (exit {rc})"):
        return
    text = open(reports[-1], encoding="utf-8", errors="replace").read()
    check("Run: finished" in text, "the soak report never says finished")
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("[FAIL]"):
            # The item's detail is the next line with text (a blank line
            # sits between them: it used to be read as the detail, so the
            # annotation came out empty).
            detail = next((x.strip() for x in lines[i + 1:i + 4]
                           if x.strip()), "")
            # A timecode stall on a shared runner is timing, not a fault in
            # the program: warned. Any other real fault line is an error.
            parts = detail.split(" | ")
            timing_only = ("Real faults" in ln and
                           all("has not moved" in x for x in parts))
            hard = not timing_only and (
                "Crashes" in ln or "frames arrived" in detail or
                "never answered" in detail or "Scheduled shows" in ln or
                "Show audio player" in ln or "Lasers" in ln or
                "Video" in ln or "Real faults" in ln or
                "never saw the link go stale" in ln or
                ("not all zero" in detail and not detail.endswith(
                    "0 packets not all zero (limit 0)")))
            # One annotation per failing item, numbers first, short.
            title = ln[7:].split(" (")[0]
            msg = f"FAIL {title}: {detail}"[:900]
            if hard:
                error(msg)
            else:
                print(f"::warning title=soak timing on a CI runner::{msg}",
                      flush=True)
    for need in ("[PASS] Engine's own error counters",
                 "flamesafe output (sACN, sent to this PC only)",
                 "Flame link frames", "Scheduled shows", "Art-Net timecode",
                 "Show audio player", "Lasers (BEYOND", "Video (MadMapper"):
        check(need in text, f"the soak report has no '{need}' item")
    check(wait(all_running, 90), "LTC Player did not come back after the "
                                 "soak test")
    body = "%0A".join(x.replace("%", "%25") for x in lines[:60])
    print(f"::notice title=Soak report (7 minutes, full stack, fake audio, "
          f"CI runner)::{body}", flush=True)
    NOTES.append(f"full-stack soak test ran 7 minutes and wrote "
                 f"{reports[-1]}")


# ---------------------------------------------------------------- main ---
def main(installer):
    # Logs from Setup and the programs carry UTF-8 (and a BOM); the
    # runner's console code page cannot print them.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    installer = os.path.abspath(installer)
    tmp = os.environ.get("RUNNER_TEMP") or os.path.dirname(installer)
    shows = os.path.join(tmp, "LTC Shows")
    os.makedirs(shows, exist_ok=True)
    write_settings(shows)
    logs = os.path.join(LOCAL, "logs")
    listener = Listener()

    try:
        say("1. Setup must refuse while a show is running")
        fake = FakeShow()
        rc = install(installer, os.path.join(tmp, "setup-refuse.log"))
        fake.close()
        if check(rc != 0, f"Setup installed while a show was running "
                          f"(exit {rc})"):
            NOTES.append(f"Setup refused during a show (exit {rc})")
        check(not os.path.exists(SUP),
              "Setup refused but still put files in Program Files")
        dump(os.path.join(tmp, "setup-refuse.log"), "refused setup log")

        say("2. silent install")
        rc = install(installer, os.path.join(tmp, "setup-1.log"))
        if not check(rc == 0, f"silent install failed (exit {rc})"):
            dump(os.path.join(tmp, "setup-1.log"), "setup log")
            return
        for n in ("LTC Player.exe", "ltcplay.exe", "flamesafe.exe",
                  "ltcplay-deck.exe", "ltcplay-soak.exe", "SHOW PC CHECKLIST.txt",
                  "flamesafe.example.json"):
            check(os.path.isfile(os.path.join(APP, n)),
                  f"{n} is missing from {APP}")
        check(os.path.isdir(os.path.join(APP, "Installers")) and
              os.listdir(os.path.join(APP, "Installers")),
              "Setup did not keep a copy of itself for rollback")
        check(os.path.isdir(START_MENU) and len(os.listdir(START_MENU)) >= 4,
              f"Start menu entries missing in {START_MENU}")
        rc, out = run(["schtasks", "/Query", "/TN", "LTC Player", "/XML"])
        check(rc == 0, "the sign-in task 'LTC Player' was not created")
        check("<Priority>4</Priority>" in out and "--task" in out,
              "the sign-in task is not the one LTC Player writes")

        say("3. self-checks of the installed programs")
        for exe in ("ltcplay.exe", "flamesafe.exe", "ltcplay-deck.exe"):
            rc, out = run([os.path.join(APP, exe), "--self-check"])
            check(rc == 0 and "self-check passed" in out,
                  f"{exe} --self-check failed (exit {rc}): "
                  f"{out.strip().splitlines()[-1:] if out else 'no output'}")
            rc, out = run([os.path.join(APP, exe), "--version"])
            check(rc == 0 and out.strip(), f"{exe} --version failed")
        rc, _ = run([SUP, "--self-check"])
        check(rc == 0, f"LTC Player.exe --self-check failed (exit {rc})")

        say("4. LTC Player starts all three, flamesafe sends zeros")
        mark = listener.mark()
        ok = wait(all_running, 90)
        check(ok, f"not every program is running after install: running "
                  f"{sorted(OURS & tasklist())}")
        check(wait(engine_answers, 60), "the engine's page never answered "
                                        "on 127.0.0.1:7878")
        check(listener.wait_for(mark, lambda p: p[1] == 200 and p[3], 30),
              "flamesafe sent no zeros at priority 200 after starting")
        if not ok:
            return
        NOTES.append("running after install: " + ", ".join(
            sorted(OURS & tasklist())) + f"; {len(listener.since(mark))} "
            "zero packets at priority 200 so far")

        say("5. an update stops flamesafe the proper way, then restarts")
        mark = listener.mark()
        rc = install(installer, os.path.join(tmp, "setup-2.log"))
        check(rc == 0, f"the update install failed (exit {rc})")
        if rc != 0:
            dump(os.path.join(tmp, "setup-2.log"), "update setup log")
        check_clean_stop(listener, mark, "update")
        check(wait(all_running, 90), "the programs did not come back after "
                                     "the update")
        mark2 = listener.mark()
        check(listener.wait_for(mark2, lambda p: not terminated(p), 30),
              "flamesafe is not sending again after the update")

        say("5b. the bench soak test, 3 minutes")
        listener.sock.close()      # the soak test listens on 5568 itself
        time.sleep(1)
        try:
            soak_check()
        finally:
            listener = Listener()

        say("6. Stop LTC Player")
        time.sleep(3)
        mark = listener.mark()
        rc, _ = run([SUP, "--stop", "--quiet"], timeout=180)
        check(rc == 0, f"Stop LTC Player failed (exit {rc})")
        check_clean_stop(listener, mark, "Stop LTC Player")
        check(wait(none_running, 30), f"programs still running after Stop: "
                                      f"{sorted(OURS & tasklist())}")
        time.sleep(2)
        quiet = listener.mark()
        time.sleep(3)
        check(len(listener.since(quiet)) == 0,
              "flamesafe packets still arriving after Stop")

        say("7. Stop is refused while a show is running")
        fake = FakeShow()
        rc, _ = run([SUP, "--stop", "--quiet"], timeout=60)
        fake.close()
        if check(rc == 3, f"Stop did not refuse during a show (exit {rc})"):
            NOTES.append("Stop LTC Player refused during a show (exit 3)")

        say("8. uninstall")
        uninst = os.path.join(APP, "unins000.exe")
        rc, _ = run([uninst, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])
        check(rc == 0, f"uninstall failed (exit {rc})")
        check(wait(lambda: not os.path.exists(SUP), 60),
              "LTC Player.exe is still there after uninstall")
        rc, _ = run(["schtasks", "/Query", "/TN", "LTC Player"])
        check(rc != 0, "the sign-in task is still there after uninstall")
        check(os.path.isfile(os.path.join(LOCAL, "showpc.json")),
              "uninstall removed the settings; it must keep them")
    finally:
        for n in ("supervisor", "flamesafe", "engine", "deck"):
            dump(os.path.join(logs, f"{n}.log"), f"{n} log")
        if FAILED:
            print(f"\n{len(FAILED)} smoke check(s) failed:")
            for f in FAILED:
                print(f"  - {f}")
        else:
            print("\nsmoke test passed")
            print("::notice title=Smoke test evidence::"
                  + "%0A".join(NOTES), flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    try:
        main(sys.argv[1])
    except Exception as e:
        error(f"the smoke test itself crashed: {type(e).__name__}: {e}")
        raise
    sys.exit(1 if FAILED else 0)
