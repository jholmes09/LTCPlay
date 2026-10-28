"""The Windows installer's smoke test, run by .github/workflows/windows-app.yml
on a clean windows-latest runner. Every failure is printed as a GitHub
`::error::` annotation so it can be read without the job log.

    python packaging/windows/ci_smoke.py "LTC Player Setup <version>.exe" [<version>]

What it proves, in order:
  1. Setup refuses while a show is running (a stand-in engine on port 7878
     says so) and installs nothing.
  2. Setup installs silently: files, Start menu, the sign-in task. The
     .sha256 file beside the installer (the workflow writes it) matches it.
  3. Each program answers --self-check from where it was installed, and
     says the version the build was stamped with.
  4. LTC Player starts all three; flamesafe puts zeros on the wire at
     priority 200, the engine's page answers, and the rack screen
     (/remote and its /api/remote/status) answers and names the version.
  5. Running Setup again (an update) stops flamesafe the proper way: zeros,
     then the stream-terminated flag. Then everything comes back, and the
     settings (showpc.json) and the show folder's files are untouched.
  5c. The engine is killed hard (the one program that may be: flamesafe is
     left alone) and the supervisor starts it again by itself.
  5d. --diagnostics writes a bundle on the Desktop with the logs, settings
     and versions and nothing it must not carry; --update --check-only
     answers with a sentence.
  6. "Stop LTC Player" stops it the same way and nothing is left running.
  7. "Stop LTC Player" refuses while a show is running.
  8. The uninstaller removes the app and the task, and keeps the settings.
Along the way (2026-10-28): Setup adds exactly one Windows Firewall rule,
"LTC Player rack page" (TCP 7878, ltcplay.exe, LocalSubnet only), an update
leaves exactly one, and the uninstaller removes it. With this runner's
private address saved as the show network address, the updated engine
answers on it, gives a request by another name the short page naming that
address, and its log names both addresses it listens on.
"""
import glob
import hashlib
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
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
    return set(pids().keys())


def pids():
    """{exe name (lower case): [pid, ...]} of every process."""
    r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                       text=True, errors="replace")
    out = {}
    for line in r.stdout.splitlines():
        if line.startswith('"'):
            cells = [c.strip('"') for c in line.split('","')]
            try:
                out.setdefault(cells[0].lower(), []).append(int(cells[1]))
            except (IndexError, ValueError):
                pass
    return out


OURS = {"ltc player.exe", "ltcplay.exe", "flamesafe.exe", "ltcplay-deck.exe"}


def engine_answers():
    try:
        with urllib.request.urlopen("http://127.0.0.1:7878/api/state",
                                    timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def get(path, timeout=5):
    """(status, body text) of a GET on the engine, or (0, error)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:7878{path}",
                                    timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_text(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


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
    # Four groups (2026-10-26): three on the deck's arm keys, the fourth
    # (pump, in the example) screen-only, so that path runs here too.
    cfg["groups"] = cfg["groups"][:4]
    cfg["destination"] = {"ip": "127.0.0.1", "port": SACN_PORT}
    fs = os.path.join(LOCAL, "flamesafe.json")
    with open(fs, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    with open(os.path.join(LOCAL, "showpc.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"show_folder": shows, "flamesafe_config": fs,
                   "port": 7878, "open_page_at_sign_in": False}, fh)


FIREWALL_RULE = "LTC Player rack page"


def firewall_rule():
    """(exit code, netsh's words) for the installer's firewall rule."""
    try:
        r = subprocess.run(f'netsh advfirewall firewall show rule '
                           f'name="{FIREWALL_RULE}" verbose',
                           capture_output=True, text=True, timeout=60,
                           errors="replace")
    except subprocess.TimeoutExpired:
        return 999, "netsh did not answer"
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def check_firewall_rule(when):
    """Exactly one rule by that name: inbound, allow, TCP 7878, ltcplay.exe,
    from LocalSubnet only."""
    rc, out = firewall_rule()
    if not check(rc == 0, f"{when}: no firewall rule named "
                          f"'{FIREWALL_RULE}': {out.strip()[:300]!r}"):
        return
    fields = {}
    for ln in out.splitlines():
        k, sep, v = ln.partition(":")
        if sep:
            fields.setdefault(k.strip().lower(), []).append(v.strip())
    n = len(fields.get("rule name", []))
    check(n == 1, f"{when}: {n} firewall rules named '{FIREWALL_RULE}'; "
                  f"there must be exactly one")
    want = {"direction": "in", "action": "allow", "protocol": "tcp",
            "localport": "7878", "remoteip": "localsubnet",
            "enabled": "yes"}
    for k, v in want.items():
        got = [x.lower() for x in fields.get(k, [])]
        check(got and all(x == v for x in got),
              f"{when}: the firewall rule's {k} is {got}, not {v!r}")
    prog = [x.lower() for x in fields.get("program", [])]
    check(prog and all(x.endswith("\\ltcplay.exe") for x in prog),
          f"{when}: the firewall rule is not for ltcplay.exe only: {prog}")
    NOTES.append(f"{when}: one firewall rule '{FIREWALL_RULE}', TCP 7878, "
                 f"ltcplay.exe, LocalSubnet, profiles "
                 f"{fields.get('profiles', ['?'])[0]}")


def private_ipv4():
    """This runner's own RFC 1918 address, or None."""
    import ipaddress
    try:
        k = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            k.connect(("192.0.2.1", 9))        # nothing is sent
            a = k.getsockname()[0]
        finally:
            k.close()
    except OSError:
        return None
    ip = ipaddress.ip_address(a)
    nets = [ipaddress.ip_network(n) for n in
            ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
    return a if any(ip in n for n in nets) else None


def save_show_address(ip):
    path = os.path.join(LOCAL, "ltcplay_remote.json")
    try:
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        doc = {}
    doc["show_network_address"] = ip
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)


def get_at(ip, path, host=None, timeout=5):
    """(status, body) of a GET on the show network address, as another
    machine would send it (Host: `host` when given)."""
    req = urllib.request.Request(f"http://{ip}:7878{path}")
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def network_check(ip, logs):
    """The engine started by the update listens on the saved address."""
    ok = wait(lambda: get_at(ip, "/remote")[0] == 200, 60)
    st, body = get_at(ip, "/remote")
    check(ok and "<title>" in body, f"the page did not answer on the saved "
                                    f"show network address {ip}:7878: {st} "
                                    f"{body[:200]!r}")
    st, body = get_at(ip, "/", host="ltc-show-pc:7878")
    check(st == 403 and f'href="http://{ip}:7878/"' in body and
          "instead" in body,
          f"a request by another name did not get the short page naming "
          f"http://{ip}:7878/: {st} {body[:300]!r}")
    log_text = (read_text(os.path.join(logs, "engine.log")) or
                b"").decode("utf-8", "replace")
    check(f"listens on 127.0.0.1:7878 and {ip}:7878" in log_text,
          f"the engine's log does not name both addresses it listens on "
          f"(127.0.0.1:7878 and {ip}:7878)")
    # The Show network card's view, on the PC itself: listening on the
    # address, the rule read as present, the network's category read.
    st, body = get("/api/remote/network", timeout=40)
    try:
        card = json.loads(body) if st == 200 else {}
    except ValueError:
        card = {}
    check(card.get("on_saved") is True and
          card.get("url") == f"http://{ip}:7878/" and
          (card.get("firewall") or {}).get("state") == "present" and
          card.get("category") in ("Public", "Private",
                                   "DomainAuthenticated"),
          f"the Show network card does not say it listens on {ip}, the "
          f"rule is there and the network's category: {st} {body[:600]!r}")
    if card:
        NOTES.append(f"Show network card: {' '.join(card.get('lines') or [])}"
                     [:600])
    if ok:
        NOTES.append(f"the engine answered on the saved show network "
                     f"address {ip}:7878, and another name got the short "
                     f"page")


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
    # A short show on CI (a runner has 7 minutes); the show PC's soak runs
    # the full-length one by default.
    rc, out = run([os.path.join(APP, "ltcplay-soak.exe"), "--minutes", "7",
                   "--no-wait", "--fake-audio", "--mode", "fallback",
                   "--show-seconds", "100", "--every-min", "3"],
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
                 "Show audio player", "Lasers (BEYOND", "Video (MadMapper",
                 # The installed engine ran its stall probe (bench_probe
                 # frozen into ltcplay.exe) and wrote its readings.
                 "[INFO] Engine stalls, inside the engine"):
        check(need in text, f"the soak report has no '{need}' item")
    check(wait(all_running, 90), "LTC Player did not come back after the "
                                 "soak test")
    body = "%0A".join(x.replace("%", "%25") for x in lines[:60])
    print(f"::notice title=Soak report (7 minutes, full stack, fake audio, "
          f"CI runner)::{body}", flush=True)
    NOTES.append(f"full-stack soak test ran 7 minutes and wrote "
                 f"{reports[-1]}")


# ---------------------------------------------------------------- main ---
def main(installer, version=""):
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
        say("0. the published checksum matches the installer")
        sums = installer + ".sha256"
        if check(os.path.isfile(sums), f"no checksum file beside the "
                                       f"installer: {sums}"):
            want = open(sums, encoding="utf-8").read().split()[0].lower()
            got = sha256_of(installer)
            if check(want == got, f"the .sha256 file says {want}, the "
                                  f"installer is {got}"):
                NOTES.append(f"SHA-256 {got} matches the published .sha256")

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
        check_firewall_rule("after install")

        say("3. self-checks of the installed programs")
        for exe in ("ltcplay.exe", "flamesafe.exe", "ltcplay-deck.exe"):
            rc, out = run([os.path.join(APP, exe), "--self-check"])
            check(rc == 0 and "self-check passed" in out,
                  f"{exe} --self-check failed (exit {rc}): "
                  f"{out.strip().splitlines()[-1:] if out else 'no output'}")
            if exe == "ltcplay.exe":
                # The Morning and Shutdown page's modules and its PDF
                # writer (pdfmini, standard library only) are in the
                # installed engine: the self-check imports them and writes
                # a one page PDF (MORNING.md).
                check("checklist PDF writer (pdfmini" in out,
                      "ltcplay.exe --self-check did not write a checklist "
                      "PDF: the Morning page's modules are not bundled")
            rc, out = run([os.path.join(APP, exe), "--version"])
            check(rc == 0 and out.strip(), f"{exe} --version failed")
            if version:
                check(version in out, f"{exe} --version does not say "
                                      f"{version}: {out.strip()!r}")
        rc, _ = run([SUP, "--self-check"])
        check(rc == 0, f"LTC Player.exe --self-check failed (exit {rc})")
        if version:
            NOTES.append(f"every program says it is {version}")

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

        say("4b. the rack screen answers")
        st, body = get("/remote")
        check(st == 200 and "<html" in body.lower() and "<title>" in body,
              f"GET /remote (the rack screen page): {st} {body[:120]!r}")
        st, body = get("/api/remote/status")
        try:
            status = json.loads(body) if st == 200 else {}
        except ValueError:
            status = {}
        check(st == 200 and isinstance(status, dict) and "served_at" in status
              and "schedule" in status,
              f"GET /api/remote/status (what the rack screen draws): {st} "
              f"{body[:200]!r}")
        if version:
            said = str(status.get("version") or "")
            if check(version in said, f"the rack screen's status does not "
                                      f"name {version}: {said!r}"):
                NOTES.append(f"the rack screen's status says {said!r}")
        # First run (2026-10-18): the supervisor wrote the example schedule
        # and the dry-run Fire & Ice file (flamesafe.json was seeded above,
        # so it is left alone), recorded them, and says per program whether
        # it runs; the rack screen's status carries the setup card's view.
        for n in ("ltcplay_schedule.json", "ltcplay_fire_ice.json",
                  "first-run.json", "programs.json"):
            check(os.path.isfile(os.path.join(LOCAL, n)),
                  f"first run: {n} was not written in {LOCAL}")
        try:
            with open(os.path.join(LOCAL, "first-run.json"),
                      encoding="utf-8") as fh:
                first = json.load(fh)
        except (OSError, ValueError):
            first = {}
        wrote = set((first.get("written") or {}))
        check({"schedule", "fire_ice", "announce"} <= wrote <=
              {"schedule", "fire_ice", "announce", "booth"},
              f"first run wrote the schedule, the Fire & Ice file and the "
              f"announcements file (no output device yet) and left the "
              f"seeded flamesafe.json alone: {first}")
        an = status.get("announce") or {}
        check(an.get("configured") is True and an.get("device_set") is False
              and all(i.get("missing") for i in an.get("items") or [{}]),
              f"the engine runs announcements with no flag typed by "
              f"anyone: no output device set, every recording missing, "
              f"nothing playable: {str(an)[:300]}")
        check(isinstance(status.get("setup"), dict) and
              status["setup"].get("schedule_ok") is True and
              isinstance(status.get("deck"), dict),
              f"the status carries the setup view with the schedule loaded, "
              f"and the deck's sentence: {str(status.get('setup'))[:200]}")

        say("5. an update stops flamesafe the proper way, then restarts, "
            "and the settings and show files are untouched")
        settings_before = read_text(os.path.join(LOCAL, "showpc.json"))
        show_file = os.path.join(shows, "smoke show file.txt")
        with open(show_file, "w", encoding="utf-8") as fh:
            fh.write("a show file left here by the smoke test\n")
        # The show network (2026-10-28): this runner's private address
        # saved as the show PC's; the engine the update starts uses it.
        net_ip = private_ipv4()
        if net_ip:
            save_show_address(net_ip)
        else:
            print("::warning title=show network::this runner has no "
                  "RFC 1918 address; the show network check is skipped",
                  flush=True)
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
        check(read_text(os.path.join(LOCAL, "showpc.json")) ==
              settings_before and settings_before is not None,
              "the update changed showpc.json; an update must not touch "
              "the settings")
        check(read_text(show_file) is not None and
              os.path.isfile(os.path.join(LOCAL, "flamesafe.json")),
              "the update removed a show file or flamesafe.json")
        check(wait(engine_answers, 60), "the engine's page never answered "
                                        "after the update")
        NOTES.append("update in place kept showpc.json, flamesafe.json and "
                     "the show folder's files")
        check_firewall_rule("after the update")
        if net_ip:
            network_check(net_ip, logs)

        say("5b. the bench soak test, 7 minutes")
        listener.sock.close()      # the soak test listens on 5568 itself
        time.sleep(1)
        try:
            soak_check()
        finally:
            listener = Listener()

        say("5c. a crashed engine is started again by the supervisor")
        # Only the engine is ever killed here. flamesafe is never hard
        # killed, not even in a test: End task sends no safe zeros.
        check(wait(engine_answers, 60), "the engine is not answering before "
                                        "the crash test")
        before = pids()
        fs_before = before.get("flamesafe.exe", [])
        eng_before = before.get("ltcplay.exe", [])
        if check(eng_before, "no ltcplay.exe to crash"):
            run(["taskkill", "/F", "/PID", str(eng_before[0])])
            gone = wait(lambda: eng_before[0] not in
                        pids().get("ltcplay.exe", []), 30)
            check(gone, "the engine did not end after taskkill")
            back = wait(lambda: any(p not in eng_before for p in
                                    pids().get("ltcplay.exe", [])), 60)
            check(back, "the supervisor did not start the engine again "
                        "within 60 s of its crash")
            check(wait(engine_answers, 60), "the restarted engine's page "
                                            "never answered")
            check(pids().get("flamesafe.exe", []) == fs_before and fs_before,
                  f"flamesafe was disturbed by the engine's crash: "
                  f"{fs_before} -> {pids().get('flamesafe.exe')}")
            sup_log = (read_text(os.path.join(logs, "supervisor.log"))
                       or b"").decode("utf-8", "replace")
            check("stopped by itself" in sup_log,
                  "the supervisor's log does not say the engine stopped by "
                  "itself and was started again")
            if back:
                NOTES.append("engine killed hard: started again by the "
                             "supervisor, flamesafe untouched")

        say("5d. the diagnostics bundle and the update check")
        desktop = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
        before_zips = set(glob.glob(os.path.join(desktop, "LTC Player "
                                                 "diagnostics *.zip")))
        rc, out = run([SUP, "--diagnostics", "--quiet"], timeout=180)
        check(rc == 0, f"--diagnostics failed (exit {rc})")
        zips = sorted(set(glob.glob(os.path.join(
            desktop, "LTC Player diagnostics *.zip"))) - before_zips)
        if check(zips, "no diagnostics bundle appeared on the Desktop"):
            import zipfile
            with zipfile.ZipFile(zips[-1]) as z:
                names = z.namelist()
            check("settings/showpc.json" in names and
                  "logs/supervisor.log" in names and
                  any(n.startswith("logs/engine.log") for n in names) and
                  "app/versions.txt" in names,
                  f"the bundle lacks the settings, logs or versions: "
                  f"{names[:30]}")
            check(not any(n.lower().endswith((".wav", ".fseq", ".lock",
                                               ".exe")) or "pin" in
                          os.path.basename(n).lower() for n in names),
                  f"the bundle carries media, a lock, a program or a PIN "
                  f"file: {names[:30]}")
            mb = os.path.getsize(zips[-1]) / 1024 ** 2
            NOTES.append(f"diagnostics bundle written, {len(names)} files, "
                         f"{mb:.1f} MB")
        rc, out = run([SUP, "--update", "--check-only", "--quiet"],
                      timeout=120)
        check(rc in (0, 10, 20, 21), f"--update --check-only exited {rc}")
        sup_log = (read_text(os.path.join(logs, "supervisor.log"))
                   or b"").decode("utf-8", "replace")
        check("update check:" in sup_log, "the update check wrote no "
                                          "sentence to the supervisor's log")
        NOTES.append(f"update check answered with exit {rc}")

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
        rc, out = firewall_rule()
        check(rc != 0, f"the firewall rule '{FIREWALL_RULE}' is still there "
                       f"after uninstall: {out.strip()[:300]!r}")
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
    if len(sys.argv) not in (2, 3):
        print(__doc__)
        sys.exit(2)
    try:
        main(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else "")
    except Exception as e:
        error(f"the smoke test itself crashed: {type(e).__name__}: {e}")
        raise
    sys.exit(1 if FAILED else 0)
