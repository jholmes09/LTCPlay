"""BENCH BUILD ONLY: the bench soak's "all programs" mode, the part that
can be proved without MadMapper or BEYOND (CI has neither): finding them,
telling whether each is running and answering, BEYOND's demo limit, and the
GPU reading. soak.py does the rest.

"Answering":
  MadMapper  its process is running, it holds its OSC input port (8000, the
             show's), and, once its project sends the heartbeat track, the
             engine's own watchdog says the heartbeat is fresh.
  BEYOND     its process is running, its window is responding, and it
             holds its OSC input port (127.0.0.2:8100, the bench plan's).
             BEYOND sends nothing back at all (bench B8), so that is as far
             as its answering can be checked.

Blocks (Jeff, 2026-10-04): with the demos, the soak runs in blocks of 1 h
50 min, clear of their 2 hour limit. Between blocks the operator quits and
starts BEYOND and MadMapper again, and each block waits until both answer.
"""
import glob as _glob
import os
import socket
import subprocess

MADMAPPER_PORT = 8000         # madmapper.DEFAULT_PORT: what the show uses
BEYOND_PORT = 8100            # beyond.DEFAULT_PORT
HEARTBEAT_PORT = 9001         # madmapper.DEFAULT_HEARTBEAT_PORT
HEARTBEAT_ADDRESS = "/float-1"
# The bench plan's addresses: MadMapper's OSC on 127.0.0.1, BEYOND's on
# 127.0.0.2 (the Art-Net timecode goes to the same two addresses).
MADMAPPER_IP = "127.0.0.1"
BEYOND_IP = "127.0.0.2"
BLOCK_S = 6600.0              # 1 h 50 min: clear of the demos' 2 h limit

# BEYOND Essentials Demo stops after about 2 hours (Jeff, 2026-10-04). An
# exit inside this window after it was first seen running is the demo's
# limit, not a fault; with a full license it never comes.
DEMO_LIMIT_S = (6600.0, 7800.0)

# Matched by image name in any folder (show PC, 2026-10-04: the demo runs
# as "C:\Program Files\MadMapperDemo 6.1.5\MadMapperDemo.exe", BEYOND as
# BEYOND.exe in C:\BEYOND55_Demo).
APPS = {
    "MadMapper": {"exe": ("MadMapper*.exe",),
                  "globs": (r"{pf}\MadMapper*\MadMapper*.exe",
                            r"{pf}\MadMapper*\*\MadMapper*.exe",
                            r"{pf86}\MadMapper*\MadMapper*.exe"),
                  "port": MADMAPPER_PORT, "ip": MADMAPPER_IP},
    "BEYOND": {"exe": ("BEYOND*.exe",),
               "globs": (r"{pf}\Pangolin\BEYOND*\BEYOND*.exe",
                         r"{pf86}\Pangolin\BEYOND*\BEYOND*.exe",
                         r"C:\Pangolin\BEYOND*\BEYOND*.exe",
                         r"C:\BEYOND*\BEYOND*.exe",
                         r"{pf}\BEYOND*\BEYOND*.exe",
                         r"{pf86}\BEYOND*\BEYOND*.exe"),
               "port": BEYOND_PORT, "ip": BEYOND_IP},
}


def is_app(app, image):
    """True when the image name (e.g. "MadMapperDemo.exe") is `app`'s."""
    import fnmatch
    n = (image or "").lower()
    return any(fnmatch.fnmatch(n, p.lower()) for p in APPS[app]["exe"])


def which_app(image):
    return next((a for a in APPS if is_app(a, image)), None)


def port_owners(port, net=None):
    """Image names of the processes holding UDP `port` on any address,
    [] when none or when it cannot be told (psutil)."""
    try:
        if net is None:
            import psutil
            conns = psutil.net_connections(kind="udp")
            name = lambda pid: psutil.Process(pid).name()  # noqa: E731
        else:
            conns, name = net
        out = []
        for c in conns:
            if c.laddr and c.laddr[1] == port and c.pid:
                try:
                    out.append(name(c.pid))
                except Exception:
                    out.append(f"process {c.pid}")
        return sorted(set(out))
    except Exception:
        return []

# What to click, said once at the start of each block (Jeff, 2026-10-04).
CLICKS = (
    "BEYOND (Demo, Advanced): start BEYOND.exe (C:\\BEYOND55_Demo). On the "
    "startup settings window click Go BEYOND. In the version picker choose "
    "Advanced (OSC and Art-Net need it). Close the VLC window that opens. "
    "If it offers to recover after a crash, decline it. Settings, OSC: "
    "incoming OSC on, port 8100; OUTGOING OSC OFF (it defaults to "
    "broadcast 255.255.255.255:8000, which hits MadMapper). Show "
    "Properties: Enable incoming timecode ON (it can reset each launch). "
    "Laser output is not available in the demo, so the lasers stay off.",
    "MadMapper (Demo): start MadMapperDemo 6.1.5 from the Start menu. "
    "Application, Audio Output: None, AND Audio Input: None (both reset to "
    "Default; the input otherwise grabs the Scarlett). Project, OSC: input "
    "port 8000 (the default is 8010); OSC Output-1 to 127.0.0.1 port 9001. "
    "Project, DMX input: ArtNet In on Loopback Pseudo-Interface 1 - "
    "127.0.0.1 (the default is the Wi-Fi). Its DMX blacking out every 30 s "
    "is the demo, not a fault.",
    "What the soak waits for: MadMapper running and holding OSC port 8000; "
    "BEYOND running, its window responding, and holding OSC port 8100. "
    "No six-track timeline is needed (the demo cannot load one). If "
    "MadMapper sends OSC /float-1 to 127.0.0.1:9001 the engine's heartbeat "
    "watchdog checks that too; if it sends nothing there, that check is "
    "simply not made and the report says so.",
)


def block_plan(total_s, block_s=BLOCK_S):
    """The block lengths for a run of `total_s` seconds: one block for each
    2 hours asked for (at least one), each at most `block_s`. 8 hours is 4
    blocks of 1 h 50 min."""
    n = max(1, int(round(total_s / 7200.0)))
    return [min(block_s, total_s / n)] * n


def app_pids(run=subprocess.run):
    """{lower-case image name: set of PIDs} of every running process."""
    try:
        out = run(["tasklist", "/fo", "csv", "/nh"], capture_output=True,
                  text=True, timeout=20).stdout
    except Exception:
        return {}
    got = {}
    for ln in out.splitlines():
        parts = [p.strip('"') for p in ln.strip().split('","')]
        if len(parts) >= 2 and parts[1].isdigit():
            got.setdefault(parts[0].lower(), set()).add(int(parts[1]))
    return got


def hung_names(run=subprocess.run):
    """Lower-case image names whose window Windows says is not responding."""
    try:
        out = run(["tasklist", "/fi", "STATUS eq NOT RESPONDING", "/fo",
                   "csv", "/nh"], capture_output=True, text=True,
                  timeout=20).stdout
    except Exception:
        return set()
    return {ln.split('","')[0].strip('"').lower()
            for ln in out.splitlines() if ln.startswith('"')}


def pids_of(app, pids):
    found = set()
    for image, ids in pids.items():
        if is_app(app, image):
            found |= ids
    return found


def readiness(app, pids, hung, held, before=None):
    """Why `app` is not ready for a block yet, or "" when it is: running,
    started again since the block before (`before`: the PIDs it had then),
    its window responding, and holding its OSC port."""
    mine = pids_of(app, pids)
    a = APPS[app]
    if not mine:
        return "not running yet"
    if before and mine & before:
        return ("still the copy from the block before: quit it and start it "
                "again")
    if any(is_app(app, h) for h in hung):
        return "its window is not responding"
    if not held:
        return f"not holding its OSC port {a['ip']}:{a['port']} yet"
    return ""


def find_app(name, env=None, glob=_glob.glob):
    """The newest-looking install path of `name` (a key of APPS), or None."""
    env = os.environ if env is None else env
    pf = env.get("ProgramFiles", r"C:\Program Files")
    pf86 = env.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    hits = []
    for pat in APPS[name]["globs"]:
        hits += glob(pat.format(pf=pf, pf86=pf86))
    return sorted(hits)[-1] if hits else None


def tasklist_names(run=subprocess.run):
    """The lower-case image names of every running process (Windows)."""
    try:
        out = run(["tasklist", "/fo", "csv", "/nh"], capture_output=True,
                  text=True, timeout=20).stdout
    except Exception:
        return set()
    return {ln.split('","')[0].strip('"').lower()
            for ln in out.splitlines() if ln.strip()}


def running(name, names):
    return any(is_app(name, n) for n in names)


WILDCARDS = ("0.0.0.0", "::", "")


def _udp_table(net=None):
    """([endpoint with .laddr and .pid], name(pid)) for every UDP endpoint
    on this PC, from Windows' own table (psutil); None when it cannot be
    read."""
    if net is not None:
        return net
    try:
        import psutil
        return (psutil.net_connections(kind="udp"),
                lambda pid: psutil.Process(pid).name())
    except Exception:
        return None


def endpoint_owners(port, ip, net=None):
    """Image names of the processes with a UDP endpoint on `port` at `ip`
    or on the wildcard (0.0.0.0 or ::), [] when none; None when the table
    cannot be read. Show PC, 2026-10-04: MadMapperDemo and BEYOND listen on
    0.0.0.0, and Windows lets a bind to one address sit beside a wildcard
    bind, so binding a port to test it says nothing."""
    tab = _udp_table(net)
    if tab is None:
        return None
    conns, name = tab
    out = []
    for c in conns:
        la = getattr(c, "laddr", None)
        if not la or la[1] != port or not c.pid:
            continue
        if la[0] in WILDCARDS or la[0] == ip:
            try:
                out.append(name(c.pid))
            except Exception:
                out.append(f"process {c.pid}")
    return sorted(set(out))


def app_holds(app, net=None):
    """True when `app`'s own process has a UDP endpoint on its OSC port, at
    its address or the wildcard. Falls back to a test bind only when the
    endpoint table cannot be read."""
    a = APPS[app]
    who = endpoint_owners(a["port"], a["ip"], net)
    if who is None:
        return port_held(a["port"], a["ip"])
    return any(is_app(app, w) for w in who)


def show_windows(app, how):
    """ShowWindow(how) on every visible top-level window of `app`'s
    processes (3 maximize, 9 restore). Returns how many. Windows only."""
    try:
        import ctypes
        from ctypes import wintypes
        import psutil
        pids = {p.pid for p in psutil.process_iter(["name"])
                if is_app(app, p.info.get("name") or "")}
        u32 = ctypes.WinDLL("user32")
        found = []
        PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                  wintypes.LPARAM)

        def cb(hwnd, _lp):
            pid = wintypes.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and u32.IsWindowVisible(hwnd) and \
                    u32.GetWindowTextLengthW(hwnd) > 0:
                found.append(hwnd)
            return True
        u32.EnumWindows(PROC(cb), 0)
        for h in found:
            u32.ShowWindow(h, how)
        return len(found)
    except Exception:
        return 0


def port_held(port, ip="127.0.0.1", sock=socket.socket):
    """True when another program holds UDP `port` on `ip` (binding it here
    fails), which for MadMapper and BEYOND means they are listening."""
    s = sock(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        s.bind((ip, port))
        return False
    except OSError:
        return True
    finally:
        s.close()


class AppWatch:
    """One program's answering record over a run: episodes of not
    answering (with times), and BEYOND's demo limit."""

    def __init__(self, name, demo_limit=False):
        self.name = name
        self.demo_limit = demo_limit
        self.first_seen = None
        self.samples = 0
        self.down_since = None
        self.episodes = []           # (start, end or None, why)
        self.demo_stopped_at = None

    def sample(self, now, alive, held, heartbeat=None, hung=False):
        """`heartbeat`: None when not configured or not known, else True
        (fresh) or False (stale). `hung`: Windows says its window is not
        responding. Returns the sentence of what is wrong, or ""."""
        self.samples += 1
        if alive and self.first_seen is None:
            self.first_seen = now
        if self.demo_stopped_at is not None:
            return ""
        why = ("not running" if not alive else
               "window not responding" if hung else
               "not holding its OSC port" if not held else
               "heartbeat stale" if heartbeat is False else "")
        if why == "not running" and self.demo_limit and \
                self.first_seen is not None and \
                DEMO_LIMIT_S[0] <= now - self.first_seen <= DEMO_LIMIT_S[1]:
            self.demo_stopped_at = now
            if self.down_since is not None:
                self._close(now)
            return ""
        if why and self.down_since is None:
            self.down_since = now
            self.episodes.append((now, None, why))
        elif not why and self.down_since is not None:
            self._close(now)
        return why

    def _close(self, now):
        start, _end, why = self.episodes[-1]
        self.episodes[-1] = (start, now, why)
        self.down_since = None

    def faults(self):
        return list(self.episodes)


def gpu_percent(run=subprocess.run):
    """Total 3D-engine GPU use in percent, one reading (Windows performance
    counters through typeperf), or None when it cannot be read."""
    try:
        out = run(["typeperf", r"\GPU Engine(*engtype_3D)\Utilization "
                   r"Percentage", "-sc", "1"], capture_output=True, text=True,
                  timeout=30).stdout
    except Exception:
        return None
    rows = [ln for ln in out.splitlines() if ln.startswith('"') and
            "/" in ln.split(",")[0]]
    if not rows:
        return None
    total = 0.0
    for v in rows[-1].split(",")[1:]:
        try:
            total += float(v.strip('"'))
        except ValueError:
            pass
    return round(total, 1)


# ---------------------------------------------------------------- heat ---
TEMP_LIMIT_C = 90.0
LIMIT_PCT = 70.0          # "% Performance Limit" or clock under this: throttled
LIMIT_FOR_S = 60.0        # for longer than this: a fault

_PS = (
    "$t = $null; try { $t = (Get-CimInstance -Namespace root/wmi "
    "-ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop | "
    "ForEach-Object { $_.CurrentTemperature }) } catch {}; "
    "$p = Get-CimInstance Win32_Processor | Select-Object -First 1; "
    "$c = (Get-Counter -ErrorAction SilentlyContinue -Counter "
    "'\\Processor Information(_Total)\\% Processor Performance',"
    "'\\Processor Information(_Total)\\% Performance Limit').CounterSamples; "
    "[pscustomobject]@{temps=@($t); cur=$p.CurrentClockSpeed; "
    "max=$p.MaxClockSpeed; perf=($c | Where-Object {$_.Path -like "
    "'*processor performance'}).CookedValue; limit=($c | Where-Object "
    "{$_.Path -like '*performance limit'}).CookedValue} | ConvertTo-Json "
    "-Compress")


def heat_sample(run=subprocess.run):
    """{"temp_c": hottest ACPI thermal zone in C or None, "clock_pct":
    current/max clock in percent or None, "perf_pct": % Processor
    Performance or None, "limit_pct": % Performance Limit (100 = nothing
    holding the CPU back) or None}. Windows only, nothing installed."""
    import json
    out = {"temp_c": None, "clock_pct": None, "perf_pct": None,
           "limit_pct": None}
    try:
        txt = run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                   _PS], capture_output=True, text=True, timeout=45).stdout
        doc = json.loads(txt.strip().splitlines()[-1])
    except Exception:
        return out
    temps = [t for t in (doc.get("temps") or []) if isinstance(t, (int, float))
             and t > 0]
    if temps:
        # Tenths of a kelvin.
        out["temp_c"] = round(max(temps) / 10.0 - 273.15, 1)
    cur, mx = doc.get("cur"), doc.get("max")
    if isinstance(cur, (int, float)) and isinstance(mx, (int, float)) and mx:
        out["clock_pct"] = round(100.0 * cur / mx, 1)
    for k, src in (("perf_pct", "perf"), ("limit_pct", "limit")):
        v = doc.get(src)
        if isinstance(v, (int, float)):
            out[k] = round(float(v), 1)
    return out


class HeatJudge:
    """Every minute's heat sample, judged: a fault above TEMP_LIMIT_C, or
    the CPU held back (Windows' "% Performance Limit", else the clock)
    under LIMIT_PCT for longer than LIMIT_FOR_S. A low clock on its own
    is power saving at light load, not throttling, so the limit counter
    is what is judged when it exists."""

    def __init__(self):
        self.samples = []          # (time, sample)
        self.hot = []              # times above the limit
        self.held_since = None
        self.held_episodes = []    # (start, end)

    def add(self, now, smp):
        self.samples.append((now, smp))
        t = smp.get("temp_c")
        if t is not None and t > TEMP_LIMIT_C:
            self.hot.append((now, t))
        held = smp.get("limit_pct")
        if held is None:
            held = smp.get("clock_pct")
        low = held is not None and held < LIMIT_PCT
        if low and self.held_since is None:
            self.held_since = now
        elif not low and self.held_since is not None:
            self.held_episodes.append((self.held_since, now))
            self.held_since = None

    def throttled(self, now=None):
        eps = list(self.held_episodes)
        if self.held_since is not None and now is not None:
            eps.append((self.held_since, now))
        return [(a, b) for a, b in eps if b - a > LIMIT_FOR_S]

    def verdict(self, now):
        temps = [smp["temp_c"] for _t, smp in self.samples
                 if smp.get("temp_c") is not None]
        bad = bool(self.hot or self.throttled(now))
        return bad, temps


# ---------------------------------------------------------------- disk ---
DISK_TEMP_LIMIT_C = 70.0
DISK_TRANSFER_LIMIT_S = 1.0
STORAGE_PROVIDERS = ("disk", "stornvme", "storahci", "Ntfs",
                     "Microsoft-Windows-Ntfs")

_PS_DISK = (
    "$since = [datetime]::Parse('{since}'); "
    "$r = $null; try {{ $r = Get-PhysicalDisk | Get-StorageReliabilityCounter "
    "-ErrorAction Stop | Select-Object Temperature, TemperatureMax, "
    "ReadLatencyMax, WriteLatencyMax }} catch {{}}; "
    "$c = (Get-Counter -ErrorAction SilentlyContinue -Counter "
    "'\\PhysicalDisk(_Total)\\Avg. Disk sec/Transfer',"
    "'\\PhysicalDisk(_Total)\\Current Disk Queue Length').CounterSamples; "
    "$e = @(); foreach ($p in @({providers})) {{ try {{ $e += @(Get-WinEvent "
    "-ErrorAction Stop -MaxEvents 20 -FilterHashtable @{{LogName='System'; "
    "ProviderName=$p; StartTime=$since}} | ForEach-Object {{ "
    "[pscustomobject]@{{at=$_.TimeCreated.ToString('s'); id=$_.Id; "
    "src=$_.ProviderName; msg=(($_.Message -split "
    "[Environment]::NewLine)[0])}} }}) }} catch {{}} }}; "
    "[pscustomobject]@{{rel=@($r); xfer=($c | Where-Object {{$_.Path -like "
    "'*sec/transfer'}}).CookedValue; queue=($c | Where-Object {{$_.Path -like "
    "'*queue length'}}).CookedValue; events=@($e)}} | ConvertTo-Json "
    "-Compress -Depth 4")


def disk_sample(since, run=subprocess.run):
    """{"temp_c", "temp_max_c" (None when Windows will not say: it may need
    an administrator), "xfer_s" (average seconds per transfer, this
    moment), "queue", "events": [(time, id, provider, first line)]} of
    storage events in the System log since `since` (a datetime)."""
    import json
    out = {"temp_c": None, "temp_max_c": None, "xfer_s": None,
           "queue": None, "events": [], "read_lat_max_ms": None,
           "write_lat_max_ms": None}
    ps = _PS_DISK.format(since=since.strftime("%Y-%m-%dT%H:%M:%S"),
                         providers=",".join(f"'{p}'"
                                            for p in STORAGE_PROVIDERS))
    try:
        txt = run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                   ps], capture_output=True, text=True, timeout=45).stdout
        doc = json.loads(txt.strip().splitlines()[-1])
    except Exception:
        return out
    temps = [r.get("Temperature") for r in (doc.get("rel") or [])
             if isinstance(r, dict) and isinstance(r.get("Temperature"),
                                                   (int, float))
             and r.get("Temperature") > 0]
    tmax = [r.get("TemperatureMax") for r in (doc.get("rel") or [])
            if isinstance(r, dict) and isinstance(r.get("TemperatureMax"),
                                                  (int, float))
            and r.get("TemperatureMax") > 0]
    out["temp_c"] = max(temps) if temps else None
    out["temp_max_c"] = max(tmax) if tmax else None
    for k, src in (("read_lat_max_ms", "ReadLatencyMax"),
                   ("write_lat_max_ms", "WriteLatencyMax")):
        vals = [r.get(src) for r in (doc.get("rel") or [])
                if isinstance(r, dict) and isinstance(r.get(src),
                                                      (int, float))]
        out[k] = max(vals) if vals else None
    for k, src in (("xfer_s", "xfer"), ("queue", "queue")):
        v = doc.get(src)
        if isinstance(v, (int, float)):
            out[k] = float(v)
    for e in doc.get("events") or []:
        if isinstance(e, dict):
            out["events"].append((str(e.get("at")), e.get("id"),
                                  str(e.get("src")), str(e.get("msg"))[:160]))
    return out


class DiskJudge:
    """Every minute's drive sample: a fault at DISK_TEMP_LIMIT_C or above,
    any transfer averaging over DISK_TRANSFER_LIMIT_S, or any storage event
    in the System log."""

    def __init__(self):
        self.samples = []            # (time, sample)
        self.events = []

    def add(self, now, smp):
        self.samples.append((now, smp))
        self.events += smp.get("events") or []

    def faults(self):
        out = []
        for now, smp in self.samples:
            t = smp.get("temp_c")
            if t is not None and t >= DISK_TEMP_LIMIT_C:
                out.append((now, f"drive at {t:g} C"))
            x = smp.get("xfer_s")
            if x is not None and x > DISK_TRANSFER_LIMIT_S:
                out.append((now, f"a transfer averaged {x:.2f} s"))
        for at, eid, src, msg in self.events:
            out.append((at, f"System log {src} event {eid}: {msg}"))
        # The drive's own worst read and write since its counters began
        # (show PC, 2026-10-04: a 15,284 ms write). A rise during the run
        # past DISK_TRANSFER_LIMIT_S is a stall in this run.
        for k, what in (("read_lat_max_ms", "read"),
                        ("write_lat_max_ms", "write")):
            vals = [(t, smp[k]) for t, smp in self.samples
                    if smp.get(k) is not None]
            if len(vals) >= 2 and vals[-1][1] > vals[0][1] and \
                    vals[-1][1] > DISK_TRANSFER_LIMIT_S * 1000:
                when = next(t for t, v in vals if v == vals[-1][1])
                out.append((when, f"the drive's worst {what} rose to "
                                  f"{vals[-1][1]:,} ms during this run"))
        return out

    def latency_line(self):
        parts = []
        for k, what in (("read_lat_max_ms", "read"),
                        ("write_lat_max_ms", "write")):
            vals = [smp[k] for _t, smp in self.samples
                    if smp.get(k) is not None]
            if vals:
                parts.append(f"the drive's worst {what} {vals[0]:,} ms at the "
                             f"start, {vals[-1]:,} ms at the end")
        return "; ".join(parts) or ("the drive's own worst read and write "
                                    "times are NOT readable (they may need "
                                    "the soak run as administrator)")

    def slow_moments(self, limit_s=0.1):
        return [(now, smp["xfer_s"]) for now, smp in self.samples
                if smp.get("xfer_s") is not None and smp["xfer_s"] > limit_s]


# ----------------------------------------------------------- self-test ---
def self_test():
    """The all-programs logic proved with fakes (CI has neither MadMapper
    nor BEYOND). Yields what passed; raises AssertionError on the first
    thing that does not."""
    import types
    # Finding them.
    env = {"ProgramFiles": r"C:\PF", "ProgramFiles(x86)": r"C:\PF86"}
    have = {r"C:\PF\MadMapper 6.1.5\MadMapper.exe",
            r"C:\PF86\Pangolin\BEYOND 5.5\BEYOND.exe"}
    import fnmatch

    def fake_glob(pat):
        return [h for h in have if fnmatch.fnmatch(h.lower(), pat.lower())]
    assert find_app("MadMapper", env, fake_glob).endswith("MadMapper.exe")
    assert find_app("BEYOND", env, fake_glob).endswith("BEYOND.exe")
    assert find_app("BEYOND", env, lambda p: []) is None
    yield "all-programs mode: MadMapper and BEYOND are found where installed"
    # Running.
    out = '"MadMapper.exe","1","Console","1","200 K"\n"System","4"\n'
    names = tasklist_names(lambda *a, **k: types.SimpleNamespace(stdout=out))
    assert running("MadMapper", names) and not running("BEYOND", names)
    assert running("MadMapper", {"madmapperdemo.exe"}) and \
        running("BEYOND", {"beyond.exe"}) and \
        which_app("MadMapperDemo.exe") == "MadMapper" and \
        which_app("chrome.exe") is None
    hits = {r"C:\Program Files\MadMapperDemo 6.1.5\MadMapperDemo.exe",
            r"C:\BEYOND55_Demo\BEYOND.exe"}
    env2 = {"ProgramFiles": r"C:\Program Files"}
    assert find_app("MadMapper", env2, lambda p: [h for h in hits if
                    fnmatch.fnmatch(h.lower(), p.lower())]) and \
        find_app("BEYOND", env2, lambda p: [h for h in hits if
                 fnmatch.fnmatch(h.lower(), p.lower())])
    C = types.SimpleNamespace
    net = ([C(laddr=("0.0.0.0", 8100), pid=7), C(laddr=("127.0.0.1", 6454),
                                                 pid=8)],
           lambda pid: {7: "BEYOND.exe", 8: "MadMapperDemo.exe"}[pid])
    assert port_owners(8100, net) == ["BEYOND.exe"] and \
        port_owners(9999, net) == []
    wild = ([C(laddr=("0.0.0.0", 8100), pid=7),
             C(laddr=("0.0.0.0", 8000), pid=8),
             C(laddr=("127.0.0.1", 6454), pid=8),
             C(laddr=("0.0.0.0", 6454), pid=7)],
            lambda pid: {7: "BEYOND.exe", 8: "MadMapperDemo.exe"}[pid])
    assert app_holds("BEYOND", wild) and app_holds("MadMapper", wild), \
        "the show PC's own table: both listen on the wildcard"
    other = ([C(laddr=("127.0.0.1", 8000), pid=9)], lambda pid: "chrome.exe")
    assert not app_holds("MadMapper", other) and \
        endpoint_owners(8000, "127.0.0.1", other) == ["chrome.exe"]
    assert endpoint_owners(6454, "127.0.0.2", wild) == ["BEYOND.exe"]
    yield ("MadMapperDemo.exe and BEYOND.exe in any folder are found, and "
           "a port held on the wildcard (0.0.0.0) counts as held by the "
           "program that owns it")
    yield "running programs are read from tasklist"
    # Answering, and its episodes.
    w = AppWatch("MadMapper")
    assert w.sample(0, True, True, True) == ""
    assert w.sample(60, True, False, None) == "not holding its OSC port"
    assert w.sample(120, True, True, False) == "not holding its OSC port" or \
        w.episodes
    w.sample(180, True, True, True)
    assert w.faults() and w.faults()[0][0] == 60 and w.faults()[0][1] == 180
    yield "a program not answering is an episode with its times"
    # BEYOND's demo limit: an exit about 2 h in is not a fault; earlier is.
    b = AppWatch("BEYOND", demo_limit=True)
    b.sample(0, True, True)
    b.sample(7200, False, False)
    assert b.demo_stopped_at == 7200 and not b.faults()
    b.sample(7260, False, False)
    assert not b.faults()
    c = AppWatch("BEYOND", demo_limit=True)
    c.sample(0, True, True)
    c.sample(1800, False, False)
    assert c.faults() and c.demo_stopped_at is None
    yield "BEYOND stopping about 2 h in is the demo's limit; earlier is a fault"
    hw = AppWatch("BEYOND", demo_limit=True)
    assert hw.sample(0, True, True, hung=True) == "window not responding"
    hw.sample(30, True, True)
    assert hw.faults() == [(0, 30, "window not responding")]
    yield "BEYOND's window not responding is an episode with its times"
    assert block_plan(8 * 3600) == [6600.0] * 4
    assert block_plan(3600) == [3600.0]
    assert block_plan(2 * 3600) == [6600.0]
    assert block_plan(24 * 3600) == [6600.0] * 12
    yield "8 hours is 4 blocks of 1 h 50 min; 1 hour is one block of 1 hour"
    tl = ('"BEYOND.exe","4120","Console","1","90,000 K"\n'
          '"MadMapper.exe","5000","Console","1","300,000 K"\n')
    pids = app_pids(lambda *a, **k: types.SimpleNamespace(stdout=tl))
    assert pids_of("BEYOND", pids) == {4120}, pids
    hung = hung_names(lambda *a, **k: types.SimpleNamespace(
        stdout='"BEYOND.exe","4120","Console","1","90,000 K"\n'))
    none = hung_names(lambda *a, **k: types.SimpleNamespace(
        stdout="INFO: No tasks are running which match the specified "
               "criteria.\n"))
    assert hung == {"beyond.exe"} and none == set()
    assert readiness("BEYOND", {}, set(), False) == "not running yet"
    assert "window is not responding" in readiness("BEYOND", pids, hung, True)
    assert "127.0.0.2:8100" in readiness("BEYOND", pids, set(), False)
    assert readiness("BEYOND", pids, set(), True) == ""
    assert "start it again" in readiness("BEYOND", pids, set(), True,
                                         before={4120})
    assert readiness("MadMapper", pids, set(), True, before={4999}) == ""
    yield ("a block waits for each program: running, started again since "
           "the block before, window responding, holding its OSC port")
    # GPU and heat readings parse.
    tp = ('"(PDH-CSV 4.0)","\\\\PC\\GPU Engine(pid_1_engtype_3D)"\n'
          '"10/04/2026 10:00:00.000","12.5","3.5"\n')
    assert gpu_percent(lambda *a, **k: types.SimpleNamespace(stdout=tp)) \
        == 16.0
    js = ('{"temps":[3301,3221],"cur":1200,"max":1600,"perf":80.5,'
          '"limit":100}')
    h = heat_sample(lambda *a, **k: types.SimpleNamespace(stdout=js))
    assert h == {"temp_c": 57.0, "clock_pct": 75.0, "perf_pct": 80.5,
                 "limit_pct": 100.0}, h
    h2 = heat_sample(lambda *a, **k: types.SimpleNamespace(
        stdout='{"temps":[],"cur":1600,"max":1600,"perf":null,"limit":null}'))
    assert h2["temp_c"] is None and h2["clock_pct"] == 100.0
    yield "GPU use and CPU temperature, clock and limit readings parse"
    j = HeatJudge()
    j.add(0, {"temp_c": 60, "limit_pct": 100})
    j.add(60, {"temp_c": 92, "limit_pct": 60})
    j.add(120, {"temp_c": 70, "limit_pct": 65})
    j.add(180, {"temp_c": 70, "limit_pct": 100})
    bad, temps = j.verdict(240)
    assert bad and j.hot == [(60, 92)] and j.throttled(240) == [(60, 180)]
    j2 = HeatJudge()
    j2.add(0, {"temp_c": None, "limit_pct": 50})
    j2.add(30, {"temp_c": None, "limit_pct": 100})
    assert not j2.verdict(60)[0]
    yield ("over 90 C, or held back under 70% for over a minute, is a "
           "fault; a moment is not")
    import datetime
    js = ('{"rel":[{"Temperature":48,"TemperatureMax":71}],"xfer":0.0021,'
          '"queue":0,"events":[{"at":"2026-10-04T10:01:02","id":129,'
          '"src":"stornvme","msg":"Reset to device was issued."}]}')
    d = disk_sample(datetime.datetime(2026, 10, 4),
                    lambda *a, **k: types.SimpleNamespace(stdout=js))
    assert d["temp_c"] == 48 and d["temp_max_c"] == 71 and \
        d["events"][0][1] == 129, d
    dj = DiskJudge()
    dj.add(0, d)
    dj.add(60, {"temp_c": 71, "xfer_s": 1.5, "events": []})
    f = dj.faults()
    assert len(f) == 3 and any("129" in w for _t, w in f) and \
        dj.slow_moments() == [(60, 1.5)]
    d0 = disk_sample(datetime.datetime(2026, 10, 4),
                     lambda *a, **k: types.SimpleNamespace(
                         stdout='{"rel":[],"xfer":0.001,"queue":0,'
                                '"events":[]}'))
    assert d0["temp_c"] is None and not DiskJudge().faults()
    dl = DiskJudge()
    dl.add(0, {"write_lat_max_ms": 15284, "read_lat_max_ms": 9895})
    dl.add(60, {"write_lat_max_ms": 15284, "read_lat_max_ms": 9895})
    assert not dl.faults()
    dl.add(120, {"write_lat_max_ms": 16000, "read_lat_max_ms": 9895})
    assert any("worst write rose to 16,000 ms" in w for _t, w in dl.faults())
    yield ("drive temperature, transfer time and storage events parse and "
           "are judged (70 C, 1 s, any event)")
