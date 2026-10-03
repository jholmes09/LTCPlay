"""ltcplay-soak.exe: the bench soak test. A PC-only stress test of the show PC
and of the software, with NO lasers, flames or lights connected.

It runs the real programs from this install (ltcplay.exe, flamesafe.exe, and
ltcplay-deck.exe when a Stream Deck is plugged in) playing a generated show
on a loop, for 1, 8 or 24 hours, and measures them from the outside the whole
time. A plain-language report is rewritten every minute (so a crash still
leaves one) and finished at the end, on the Desktop and in the logs folder.

    ltcplay-soak.exe                 asks how long (1, 8 or 24 hours; Enter = 8)
    ltcplay-soak.exe --hours 8
    ltcplay-soak.exe --minutes 3 --no-wait      (the CI run)
    ltcplay-soak.exe --audio-device "Focusrite USB ASIO"

What is real and what is not, said in the report too:
  - The engine is the real ltcplay.exe, running a generated four-cue show on
    its own clock (GO), sending real Art-Net pixel frames to 127.0.0.1, where
    this program listens and times every frame.
  - flamesafe is the real flamesafe.exe, with a soak copy of the config whose
    sACN destination is forced to 127.0.0.1 (this program listens there:
    every packet must be zero). Nothing can reach a flame node.
  - The flame link is ltcplay's real FlameLink sender, run inside this
    program, because the engine does not send flame frames yet in this
    build. flamesafe's own status frames say whether it ever saw the link
    go stale.
  - Audio: a silent test stream on the show's audio interface (picked by
    exact name, never the Windows default), opened the way the show audio
    opens it (ASIO first, never Windows' shared mixer). It is not the show
    audio player itself.
  - Not exercised: BEYOND, MadMapper and Art-Net timecode (nothing in the
    running engine sends them in this build), and real LTC input.
"""
import csv
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request

import ltcwin

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 7878
ARTNET_PORT = 6454
SACN_PORT = 5568
DECK_STATUS_RELAY = 5579
LOOP_S = 240.0             # the generated show is about 3 min 45 s
SAMPLE_S = 5.0
REPORT_EVERY_S = 60.0

# Limits, with where each comes from.
PIXEL_PERIOD_MS = 25.0     # the show's 25 ms output step (40 frames a second)
PIXEL_MEAN_TOL_MS = 3.0    # selftest test_pixel_output_frame_jitter
PIXEL_DEV_MS = 15.0        # selftest: a frame within half a frame of target
PIXEL_DEV_RATE = 0.001     # this soak: fewer than 1 in 1000 frames past it
PIXEL_GAP_MS = 100.0       # this soak: never four frames' worth of nothing
LINK_GAP_MS = 50.0         # CONTRACT.md: never more than 50 ms between frames
SACN_LATE_MS = 250.0       # flamesafe overrun_ms: a tick this late is a fault
MEM_GROWTH_MB_H = 10.0     # this soak: steady growth past this is a leak
DRIFT_MS = 50.0            # this soak: one and a half frames at 30 fps
DECK_VID, DECK_PID = 0x0FD9, 0x0063   # the Stream Deck Mini streamdeck.py drives


def now_text(t=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t or time.time()))


# --------------------------------------------------------------- stats ---
class Intervals:
    """Streaming statistics of the gaps between events."""

    def __init__(self, period_ms, dev_ms=None, gap_ms=None):
        self.period = period_ms
        self.dev_ms = dev_ms
        self.gap_ms = gap_ms
        self.lock = threading.Lock()
        self.last = None
        self.n = 0
        self.total = 0.0
        self.worst_dev = 0.0
        self.worst_dev_at = None
        self.longest = 0.0
        self.longest_at = None
        self.longest_t = None
        self.longest_loop_s = None
        self.over_dev = 0
        self.over_gap = 0
        self.events = 0

    def tick(self, t=None):
        t = time.perf_counter() if t is None else t
        with self.lock:
            self.events += 1
            if self.last is not None:
                g = (t - self.last) * 1000.0
                self.n += 1
                self.total += g
                d = abs(g - self.period)
                if d > self.worst_dev:
                    self.worst_dev, self.worst_dev_at = d, time.time()
                if g > self.longest:
                    self.longest, self.longest_at = g, time.time()
                    self.longest_t = t
                if self.dev_ms is not None and d >= self.dev_ms:
                    self.over_dev += 1
                if self.gap_ms is not None and g > self.gap_ms:
                    self.over_gap += 1
            self.last = t

    def reset_gap(self):
        """The next event starts a new run (after a restart)."""
        with self.lock:
            self.last = None

    def mean(self):
        return self.total / self.n if self.n else 0.0


# ----------------------------------------------------------- listeners ---
def udp_listener(port, handle, name):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    except OSError:
        pass
    s.bind(("127.0.0.1", port))
    s.settimeout(0.5)

    def run():
        while not STOP.is_set():
            try:
                data, addr = s.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                if STOP.is_set():
                    return
                continue
            try:
                handle(data, time.perf_counter(), addr)
            except Exception as e:
                note(f"{name} listener: {type(e).__name__}: {e}")
    threading.Thread(target=run, daemon=True, name=name).start()
    return s


STOP = threading.Event()
NOTES = []        # (time, sentence): the run's events, for the report


def note(text):
    NOTES.append((time.time(), text))
    print(f"{now_text()}  {text}", flush=True)


# ------------------------------------------------------------ the soak ---
class Soak:
    def __init__(self, seconds, audio_device=None):
        self.seconds = seconds
        self.audio_device = audio_device
        stamp = time.strftime("%Y-%m-%d_%H%M")
        import supervisor as sup
        self.sup = sup
        self.dir = os.path.join(sup.appdata_dir(), "soak", stamp)
        os.makedirs(self.dir, exist_ok=True)
        self.report_name = f"LTC Player soak report {stamp}.txt"
        self.report_paths = [os.path.join(self.dir, self.report_name)]
        desk = desktop_dir()
        if desk:
            self.report_paths.append(os.path.join(desk, self.report_name))
        self.started = time.time()
        self.ended = None
        self.finished = False
        self.pixel_streams = {}
        self.pixel_quiet_until = float("inf")   # nothing timed before GO
        self.stopping = False
        self.link = None
        self.link_gaps = Intervals(1000.0 / 40, None, LINK_GAP_MS)
        self.sacn = Intervals(25.0, None, SACN_LATE_MS)
        self.sacn_nonzero = 0
        self.sacn_terminated = 0
        self.fs_state = "never"
        self.fs_fresh_seen = False
        self.fs_stale_events = 0
        self.fs_status_frames = 0
        self.fs_lock_alarms = 0
        self.audio_desc = ""
        self.audio_why_not = ""
        self.audio_underflows = 0
        self.audio_callbacks = 0
        self.audio_gaps = Intervals(512 / 48.0, None, 3 * 512 / 48.0)
        self.procs = {}           # name -> Child
        self.mem = {}             # name -> [(hours, MB)]
        self.cpu = {}             # name -> [percent]
        self.engine_first = None
        self.engine_last = None
        self.engine_errors = []
        self.drift_worst = 0.0
        self.drift_worst_at = None
        self.loops = 0
        self.go_wall = None
        self.go_tc = 0.0
        self.pauses = []          # (when, seconds)
        self.cpu_freq_low = None
        self.disk_start = None
        self.disk_now = None
        self.checks = []          # pre-run warnings about the PC
        self.deck = False
        self.fs_source = "(not read yet)"
        self.tick_hz = 40.0
        self.samples = open(os.path.join(self.dir, "samples.csv"), "w",
                            newline="", encoding="utf-8")
        self.csv = csv.writer(self.samples)
        self.csv.writerow(["time", "hours", "name", "cpu_pct", "rss_mb",
                           "frames_out", "send_errors", "loop_errors",
                           "drift_ms"])

    # ---------------------------------------------------------- setup ---
    def pc_checks(self):
        """Things about the PC that can end a long run early."""
        if not ltcwin.WINDOWS:
            return
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                               r"SOFTWARE\Microsoft\WindowsUpdate\UX\Settings")
            try:
                until, _ = winreg.QueryValueEx(k, "PauseUpdatesExpiryTime")
            except OSError:
                until = ""
            if not until:
                self.checks.append(
                    "Windows Update is NOT paused. An update restart during "
                    "this run would end it early. (Checklist: pause updates "
                    "for the show weeks.)")
            else:
                self.checks.append(f"Windows Update is paused until {until}.")
        except OSError:
            self.checks.append("Could not read whether Windows Update is "
                               "paused.")
        out = run_text(["powercfg", "/a"])
        if out:
            head = out.split("not available", 1)[0].lower() \
                if "not available" in out.lower() else out.lower()
            if "hibernate" in head:
                self.checks.append(
                    "Hibernate is available on this PC. If it is set to "
                    "start after a time, it would end this run. "
                    "(Checklist: Sleep and Hibernate: Never.)")
        q = run_text(["powercfg", "/q", "SCHEME_CURRENT", "SUB_SLEEP"])
        if q:
            for name, guid in (("Sleep", "29f6c1db-86da-48c5-9fdb-f2b67b1f44da"),
                               ("Hibernate",
                                "9d7815a6-7ee4-497e-8888-515a05f02364")):
                part = q.split(guid, 1)
                if len(part) == 2:
                    ac = [ln for ln in part[1].splitlines()
                          if "AC Power Setting Index" in ln][:1]
                    if ac:
                        secs = int(ac[0].rsplit(":", 1)[1].strip(), 16)
                        if secs:
                            self.checks.append(
                                f"{name} after {secs // 60} minutes is set "
                                f"(plugged in). LTC Player keeps the PC "
                                f"awake while it runs, but set it to Never.")

    def make_show(self):
        import tempfile
        import test_show_fixtures as fx
        self.fx = fx
        saved, tempfile.tempdir = tempfile.tempdir, self.dir
        try:
            show = fx.synthetic_show_dir()
            self.fixture_dir = show
        finally:
            tempfile.tempdir = saved
        cues = [("01:00:00:00", "GPL 2026_Set 1_Opener.fseq"),
                ("01:01:00:00", "GPL 2026_Set 1_Munsters.fseq"),
                ("01:01:30:00", "GPL 2026_Set 1_Ending.fseq"),
                ("01:02:10:00", "GPL 2026_Set 2_Ghostbusters.fseq")]
        doc = {"fps": 30, "show_dir": show, "on_lost": "freerun",
               "cues": [{"tc": tc, "fseq": f} for tc, f in cues]}
        self.show_dir = os.path.join(self.dir, "show")
        os.makedirs(self.show_dir, exist_ok=True)
        with open(os.path.join(self.show_dir, "soak.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)

    def flamesafe_config(self):
        """A soak copy of flamesafe's config: the user's if there is one
        (else the example), the sACN destination forced to loopback."""
        sup = self.sup
        src = sup.load_settings()["flamesafe_config"]
        used = src
        if not os.path.isfile(src):
            for used in (os.path.join(ltcwin.app_dir(),
                                      "flamesafe.example.json"),
                         os.path.join(ltcwin.internal_dir(),
                                      "flamesafe.example.json"),
                         os.path.join(ltcwin.internal_dir(), "flamesafe",
                                      "flamesafe.example.json")):
                if os.path.isfile(used):
                    break
        with open(used, encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
        cfg["destination"] = {"ip": "127.0.0.1", "port": SACN_PORT}
        cfg["groups"] = cfg.get("groups", [])[:3]
        cfg["log_dir"] = os.path.join(self.dir, "flamesafe-journal")
        self.fs_cfg_doc = cfg
        self.fs_cfg = os.path.join(self.dir, "flamesafe-soak.json")
        with open(self.fs_cfg, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=1)
        deck_cfg = json.loads(json.dumps(cfg))
        deck_cfg["link"]["status_port"] = DECK_STATUS_RELAY
        self.deck_cfg = os.path.join(self.dir, "flamesafe-soak-deck.json")
        with open(self.deck_cfg, "w", encoding="utf-8") as fh:
            json.dump(deck_cfg, fh, indent=1)
        self.fs_source = used
        self.key = cfg["link"]["key"]
        self.status_port = int(cfg["link"]["status_port"])
        self.tick_hz = float(cfg.get("tick_hz", 40))
        self.sacn = Intervals(1000.0 / self.tick_hz, None, SACN_LATE_MS)

    def deck_plugged_in(self):
        try:
            import hid
            return bool(hid.enumerate(DECK_VID, DECK_PID))
        except Exception as e:
            note(f"could not look for a Stream Deck: {e}")
            return False

    # ------------------------------------------------------ listeners ---
    def on_artnet(self, b, t, addr=None):
        if self.stopping:
            return
        if len(b) < 18 or b[:8] != b"Art-Net\0":
            return
        if struct.unpack_from("<H", b, 8)[0] != 0x5000:
            return
        uni = struct.unpack_from("<H", b, 14)[0]
        # One stream per sending socket and universe: the engine's frame
        # loop is one; a stray packet from another socket (a blackout, a
        # restart) must not read as a 0 ms frame interval.
        key = (addr[1] if addr else 0, uni)
        st = self.pixel_streams.get(key)
        if st is None:
            st = self.pixel_streams[key] = Intervals(
                PIXEL_PERIOD_MS, PIXEL_DEV_MS, PIXEL_GAP_MS)
        if t < self.pixel_quiet_until:
            st.reset_gap()
            return
        before = st.longest
        st.tick(t)
        if st.longest > before and self.go_wall is not None:
            st.longest_loop_s = t - self.go_wall

    @property
    def pixels(self):
        """The busiest stream: the engine's own frame loop."""
        if not self.pixel_streams:
            return Intervals(PIXEL_PERIOD_MS, PIXEL_DEV_MS, PIXEL_GAP_MS)
        return max(self.pixel_streams.values(), key=lambda x: x.events)

    def on_sacn(self, b, t, addr=None):
        if len(b) < 126 or b[4:16] != b"ASC-E1.17\0\0\0":
            return
        self.sacn.tick(t)
        if any(b[126:126 + 512]):
            self.sacn_nonzero += 1
        if b[112] & 0x40:
            self.sacn_terminated += 1

    def on_status(self, b, t, addr=None):
        if self.deck:
            try:
                self.relay.sendto(b, ("127.0.0.1", DECK_STATUS_RELAY))
            except OSError:
                pass
        from ltcplay import flamelink
        st = flamelink.decode_status(b, self.key)
        if st is None:
            return
        self.fs_status_frames += 1
        state = (st.get("frames") or {}).get("state")
        if self.stopping:
            return
        if state == "fresh":
            self.fs_fresh_seen = True
        elif state == "stale" and self.fs_state == "fresh":
            self.fs_stale_events += 1
            note("flamesafe says the flame link went STALE")
        self.fs_state = state or self.fs_state
        if self.link is not None and self.link.note_status(st):
            self.fs_lock_alarms += 1

    # ---------------------------------------------------------- audio ---
    def start_audio(self):
        box = {}

        def opener():
            try:
                from ltcplay import showaudio, settings as settings_mod
                sd = showaudio.import_sounddevice()
                names = []
                if self.audio_device:
                    names.append(self.audio_device)
                saved = settings_mod.load().get("device")
                if saved:
                    names.append(saved)
                outs = [d for d in sd.query_devices()
                        if d.get("max_output_channels", 0) > 0]
                apis = [a.get("name", "") for a in sd.query_hostapis()]
                for d in outs:
                    n = str(d.get("name", ""))
                    api = apis[d["hostapi"]] if d.get("hostapi", -1) < len(
                        apis) else ""
                    if api == "ASIO" and any(w in n.lower() for w in (
                            "focusrite", "scarlett")):
                        names.append(n)
                box["outs"] = sorted({str(d.get("name")) for d in outs})
                tried = []
                for n in dict.fromkeys(names):
                    try:
                        stream, desc, _shared = showaudio.open_output_stream(
                            sd, n, 2, 48000, self._audio_cb)
                    except Exception as e:
                        tried.append(f"{n}: {e}")
                        continue
                    stream.start()
                    box["stream"], box["desc"] = stream, desc
                    return
                box["why"] = ("; ".join(tried) if tried else
                              "no show audio interface was named or found "
                              "(none saved in LTC Player's settings, and no "
                              "Focusrite or Scarlett ASIO output)")
            except Exception as e:
                box["why"] = f"{type(e).__name__}: {e}"
        th = threading.Thread(target=opener, daemon=True, name="soak-audio")
        th.start()
        th.join(30)
        if th.is_alive():
            self.audio_why_not = ("opening the audio device did not finish "
                                  "within 30 s (an ASIO driver with no "
                                  "hardware can do this)")
        elif "stream" in box:
            self.audio_stream = box["stream"]
            self.audio_desc = box["desc"]
            note(f"audio test stream open: {self.audio_desc}")
            return
        else:
            self.audio_why_not = box.get("why", "unknown")
            if box.get("outs"):
                self.audio_why_not += (". Outputs on this PC: "
                                       + ", ".join(box["outs"]))
        note(f"audio NOT tested: {self.audio_why_not}")

    def _audio_cb(self, outdata, frames, time_info, status):
        outdata.fill(0)
        self.audio_callbacks += 1
        self.audio_gaps.period = frames / 48.0
        self.audio_gaps.gap_ms = 3 * frames / 48.0
        self.audio_gaps.tick()
        if status and getattr(status, "output_underflow", False):
            self.audio_underflows += 1

    # ------------------------------------------------------- programs ---
    def program_cmd(self, name):
        exe = {"engine": "ltcplay", "flamesafe": "flamesafe",
               "deck": "ltcplay-deck"}[name]
        if ltcwin.frozen():
            return [os.path.join(ltcwin.app_dir(),
                                 exe + (".exe" if ltcwin.WINDOWS else ""))]
        entry = {"engine": "entry_engine.py", "flamesafe":
                 "entry_flamesafe.py", "deck": "entry_deck.py"}[name]
        return [sys.executable, os.path.join(HERE, entry)]

    def start_program(self, name):
        args = {"flamesafe": [self.fs_cfg],
                "engine": ["serve", "--folder", self.show_dir, "--port",
                           str(PORT), "--no-browser"],
                "deck": ["--flamesafe-config", self.deck_cfg,
                         "--ltcplay-url", f"http://127.0.0.1:{PORT}"]}[name]
        out = open(os.path.join(self.dir, f"{name}.log"), "a",
                   encoding="utf-8")
        # Each program shares this window's console (its output goes to a
        # log file) in a process group of its own, so Ctrl-Break reaches
        # it alone, and closing this window stops it cleanly too.
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if ltcwin.WINDOWS else 0
        p = subprocess.Popen(self.program_cmd(name) + args,
                             stdin=subprocess.DEVNULL, stdout=out,
                             stderr=subprocess.STDOUT, cwd=self.dir,
                             creationflags=flags)
        old = self.procs.get(name)
        self.procs[name] = {"p": p, "out": out, "started": time.time(),
                            "restarts": (old["restarts"] + 1) if old else 0,
                            "crashes": old["crashes"] if old else [],
                            "ps": None}
        note(f"started {name} (pid {p.pid})")

    def stop_program(self, name):
        c = self.procs.get(name)
        if not c or c["p"].poll() is not None:
            return True
        import signal
        why = ""
        try:
            c["p"].send_signal(signal.CTRL_BREAK_EVENT if ltcwin.WINDOWS
                               else signal.SIGINT)
        except OSError as e:
            why = str(e)
        try:
            c["p"].wait(20)
            note(f"{name} stopped cleanly")
            return True
        except subprocess.TimeoutExpired:
            how = why or "Ctrl-Break sent"
            note(f"{name} did NOT stop within 20 s ({how}); it was not "
                 f"forced to quit")
            return False

    def engine(self, route, body=None, timeout=5):
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}{route}",
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            try:
                return {"error": json.loads(e.read()).get("error")}
            except Exception:
                return {"error": f"HTTP {e.code}"}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    def start_show(self):
        deadline = time.time() + 60
        while time.time() < deadline:
            if "error" not in self.engine("/api/state"):
                break
            time.sleep(0.5)
        r = self.engine("/api/start", {"timeline": "soak.json"}, timeout=30)
        if r.get("error"):
            note(f"the engine refused Run: {r['error']}")
            return False
        self.go()
        return True

    def go(self):
        r = self.engine("/api/go", {"at": "01:00:00:00"})
        if r.get("error"):
            note(f"the engine refused GO: {r['error']}")
            return
        self.go_wall = time.perf_counter()
        self.loops += 1
        # The first seconds after a GO (the engine loading the top of the
        # show) are not timed; every frame after them is.
        self.pixel_quiet_until = time.perf_counter() + 5.0

    # --------------------------------------------------------- sample ---
    def sample(self, hours):
        try:
            import psutil
        except ImportError:
            psutil = None
        rows = []
        for name, c in self.procs.items():
            p = c["p"]
            if p.poll() is not None:
                continue
            cpu = rss = None
            if psutil is not None:
                try:
                    if c["ps"] is None or c["ps"].pid != p.pid:
                        c["ps"] = psutil.Process(p.pid)
                        c["ps"].cpu_percent(None)
                    cpu = c["ps"].cpu_percent(None)
                    rss = c["ps"].memory_info().rss / 1e6
                except Exception:
                    pass
            if rss is not None:
                self.mem.setdefault(name, []).append((hours, rss))
            if cpu is not None:
                self.cpu.setdefault(name, []).append(cpu)
            rows.append([name, cpu, rss])
        st = self.engine("/api/state")
        drift = None
        if st.get("running"):
            snap = {k: st.get(k) for k in ("frames_out", "send_errors",
                                           "loop_errors", "restarts",
                                           "jumps", "socket_reopens")}
            if self.engine_first is None:
                self.engine_first = snap
            self.engine_last = snap
            nowc = st.get("now") or {}
            if self.go_wall is not None and st.get("state") == "FREERUN" \
                    and st.get("playing"):
                try:
                    h, m, s, f = (int(x) for x in st["playing"].replace(
                        ";", ":").split(":"))
                    pos = (h - 1) * 3600 + m * 60 + s + f / 30.0
                    wall = time.perf_counter() - self.go_wall
                    # the page's answer may be up to 0.2 s old, and has
                    # whole frames: allowed for, not counted as drift
                    drift = (wall - pos) * 1000.0
                    slack = 200.0 + 1000.0 / 30
                    eff = max(0.0, abs(drift) - slack)
                    if eff > self.drift_worst:
                        self.drift_worst = eff
                        self.drift_worst_at = time.time()
                except (ValueError, KeyError):
                    pass
            _ = nowc
        elif self.engine_last is not None and "error" in st:
            self.engine_errors.append((time.time(), st["error"]))
        if psutil is not None:
            try:
                fr = psutil.cpu_freq()
                if fr and fr.max:
                    pct = fr.current / fr.max * 100
                    if self.cpu_freq_low is None or pct < self.cpu_freq_low[0]:
                        self.cpu_freq_low = (pct, time.time(), fr.current,
                                             fr.max)
            except Exception:
                pass
        self.disk_now = folder_mb([self.sup.log_dir(), self.dir])
        if self.disk_start is None:
            self.disk_start = self.disk_now
        t = now_text()
        for name, cpu, rss in rows:
            self.csv.writerow([t, f"{hours:.4f}", name,
                               "" if cpu is None else f"{cpu:.1f}",
                               "" if rss is None else f"{rss:.1f}",
                               (self.engine_last or {}).get("frames_out", ""),
                               (self.engine_last or {}).get("send_errors", ""),
                               (self.engine_last or {}).get("loop_errors", ""),
                               "" if drift is None else f"{drift:.1f}"])
        self.samples.flush()

    # ------------------------------------------------------------ run ---
    def run(self):
        note(f"soak test starting for {self.seconds / 3600:g} hour(s); "
             f"folder {self.dir}")
        self.pc_checks()
        for c in self.checks:
            note("PC check: " + c)
        self.make_show()
        self.flamesafe_config()
        self.deck = self.deck_plugged_in()
        note("Stream Deck Mini " + ("found: the deck program runs too" if
                                    self.deck else "not plugged in: the "
                                    "deck program is not run"))
        self.relay = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        socks = [udp_listener(ARTNET_PORT, self.on_artnet, "artnet"),
                 udp_listener(SACN_PORT, self.on_sacn, "sacn"),
                 udp_listener(self.status_port, self.on_status, "status")]
        self.start_audio()
        self.start_program("flamesafe")
        time.sleep(2)
        from ltcplay import flamelink

        soak = self

        class TimedLink(flamelink.FlameLink):
            def send_frame(self):
                r = super().send_frame()
                soak.link_gaps.tick()
                return r
        lcfg = flamelink.FlameLinkConfig.from_flamesafe_config(self.fs_cfg)
        self.link = TimedLink(lcfg, journal=lambda text, **kw: note(
            "flame link: " + text) if kw.get("fault") else None)
        self.link.start()
        self.start_program("engine")
        if self.deck:
            self.start_program("deck")
        self.start_show()
        t0 = time.perf_counter()
        next_sample = next_report = t0
        last_loop = time.perf_counter()
        last_wall = time.time()
        try:
            while time.perf_counter() - t0 < self.seconds:
                time.sleep(0.5)
                nw = time.time()
                if nw - last_wall > 15:
                    self.pauses.append((last_wall, nw - last_wall))
                    note(f"this PC paused or slept for {nw - last_wall:.0f} s")
                last_wall = nw
                for name, c in list(self.procs.items()):
                    if c["p"].poll() is not None:
                        code = c["p"].returncode
                        c["crashes"].append((time.time(), code))
                        note(f"{name} STOPPED BY ITSELF (exit code {code}); "
                             f"starting it again")
                        c["out"].close()
                        self.start_program(name)
                        if name == "engine":
                            self.start_show()
                            last_loop = time.perf_counter()
                if time.perf_counter() - last_loop >= LOOP_S:
                    self.go()
                    last_loop = time.perf_counter()
                hours = (time.perf_counter() - t0) / 3600.0
                if time.perf_counter() >= next_sample:
                    self.sample(hours)
                    next_sample += SAMPLE_S
                if time.perf_counter() >= next_report:
                    self.write_report()
                    next_report += REPORT_EVERY_S
        except KeyboardInterrupt:
            note("stopped early by Ctrl-C")
        finally:
            self.ended = time.time()
            self.stopping = True
            self.engine("/api/stop", {})
            for name in ("deck", "engine"):
                self.stop_program(name)
            self.link.stop()
            time.sleep(1)
            self.stop_program("flamesafe")
            time.sleep(1)
            STOP.set()
            for s in socks:
                s.close()
            st = getattr(self, "audio_stream", None)
            if st is not None:
                try:
                    st.stop()
                    st.close()
                except Exception:
                    pass
            self.power_events()
            self.finished = True
            self.write_report()
            self.samples.close()
            import shutil
            shutil.rmtree(getattr(self, "fixture_dir", ""),
                          ignore_errors=True)
        return self.passed

    def power_events(self):
        """Sleep, wake and restarts Windows logged during the run."""
        self.power = []
        if not ltcwin.WINDOWS:
            return
        ms = int((time.time() - self.started) * 1000) + 60000
        q = ("*[System[(Provider[@Name='Microsoft-Windows-Kernel-Power'] or "
             "Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] or "
             "Provider[@Name='Microsoft-Windows-Kernel-Processor-Power']) "
             f"and TimeCreated[timediff(@SystemTime) <= {ms}]]]")
        out = run_text(["wevtutil", "qe", "System", f"/q:{q}", "/f:text",
                        "/c:50", "/rd:true"])
        for block in (out or "").split("Event["):
            lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
            date = next((ln.split(":", 1)[1].strip() for ln in lines
                         if ln.startswith("Date:")), "")
            eid = next((ln.split(":", 1)[1].strip() for ln in lines
                        if ln.startswith("Event ID:")), "")
            desc = next((lines[i + 1] for i, ln in enumerate(lines)
                         if ln.startswith("Description:") and
                         i + 1 < len(lines)), "")
            if eid:
                self.power.append(f"{date} event {eid}: {desc[:160]}")

    # --------------------------------------------------------- report ---
    def items(self):
        """(verdict, title, detail) for every item."""
        out = []
        px = self.pixels
        if px.n < 10:
            out.append(("FAIL", "Pixel frames (Art-Net, timed from outside "
                        "the engine)", f"only {px.events} frames arrived"))
        else:
            rate = px.over_dev / px.n
            ok = (abs(px.mean() - PIXEL_PERIOD_MS) <= PIXEL_MEAN_TOL_MS and
                  rate < PIXEL_DEV_RATE and px.over_gap == 0)
            out.append(("PASS" if ok else "FAIL",
                        "Pixel frames (Art-Net, timed from outside the "
                        "engine)",
                        f"{px.events} frames, mean interval {px.mean():.2f} ms "
                        f"(target {PIXEL_PERIOD_MS:g} +/- {PIXEL_MEAN_TOL_MS:g}"
                        f"); {px.over_dev} frames ({rate * 100:.3f}%) off by "
                        f"{PIXEL_DEV_MS:g} ms or more (limit "
                        f"{PIXEL_DEV_RATE * 100:g}%); worst {px.worst_dev:.1f} "
                        f"ms off at {now_text(px.worst_dev_at)}; longest gap "
                        f"{px.longest:.1f} ms at {now_text(px.longest_at)}"
                        + (f", {px.longest_loop_s:.1f} s into the "
                           f"show loop" if px.longest_loop_s
                           is not None else "") +
                        f" ({px.over_gap} over {PIXEL_GAP_MS:g} ms, limit 0). "
                        f"The 5 s after each GO back to the top are not timed."))
        e0, e1 = self.engine_first or {}, self.engine_last or {}
        if e1:
            d = {k: (e1.get(k) or 0) - (e0.get(k) or 0)
                 for k in ("send_errors", "loop_errors", "restarts",
                           "socket_reopens")}
            ok = not any(d.values())
            out.append(("PASS" if ok else "FAIL", "Engine's own error "
                        "counters", ", ".join(f"{k} +{v}" for k, v in
                                              d.items())
                        + f"; {self.loops} loops of the show"))
        else:
            out.append(("FAIL", "Engine's own error counters",
                        "the engine never answered with a running show"))
        out.append(("PASS" if self.drift_worst <= DRIFT_MS else "FAIL",
                    "Show position against the wall clock",
                    f"worst {self.drift_worst:.0f} ms beyond what the page's "
                    f"0.2 s cache and whole frames allow (limit "
                    f"{DRIFT_MS:g} ms)" + (f", at "
                    f"{now_text(self.drift_worst_at)}" if self.drift_worst_at
                    else "") + ". Free-running on this PC's clock: there is "
                    "no LTC input on the bench."))
        lg = self.link_gaps
        out.append(("PASS" if lg.n > 10 and lg.over_gap == 0 else "FAIL",
                    "Flame link frames (ltcplay's FlameLink to flamesafe)",
                    f"{lg.events} frames, mean {lg.mean():.1f} ms, longest gap "
                    f"{lg.longest:.1f} ms at {now_text(lg.longest_at)}; "
                    f"{lg.over_gap} gaps over {LINK_GAP_MS:g} ms (CONTRACT.md: "
                    f"never more than 50 ms)"))
        ok = self.fs_fresh_seen and self.fs_stale_events == 0
        out.append(("PASS" if ok else "FAIL", "flamesafe never saw the link "
                    "go stale", f"{self.fs_status_frames} status frames; link "
                    f"went stale {self.fs_stale_events} time(s) after it was "
                    f"first fresh; lock alarms {self.fs_lock_alarms}"))
        sc = self.sacn
        ok = sc.n > 10 and self.sacn_nonzero == 0 and sc.over_gap == 0
        out.append(("PASS" if ok else "FAIL", "flamesafe output (sACN, sent "
                    "to this PC only)", f"{sc.events} packets, mean "
                    f"{sc.mean():.1f} ms (target {1000 / self.tick_hz:.0f}), "
                    f"longest gap {sc.longest:.1f} ms (limit "
                    f"{SACN_LATE_MS:g}, flamesafe's overrun_ms), "
                    f"{self.sacn_nonzero} packets not all zero (limit 0)"))
        if self.audio_desc:
            ok = self.audio_underflows == 0 and self.audio_gaps.over_gap == 0
            out.append(("PASS" if ok else "FAIL", "Audio interface",
                        f"{self.audio_desc}: {self.audio_callbacks} "
                        f"callbacks, {self.audio_underflows} underruns, "
                        f"{self.audio_gaps.over_gap} callback gaps over 3 "
                        f"blocks (longest {self.audio_gaps.longest:.1f} ms). "
                        f"A silent test stream, not the show audio player."))
        else:
            out.append(("NOT TESTED", "Audio interface", self.audio_why_not))
        crashes = sum(len(c["crashes"]) for c in self.procs.values())
        out.append(("PASS" if crashes == 0 else "FAIL", "Crashes and restarts",
                    "; ".join(f"{n}: {len(c['crashes'])} crash(es)"
                              for n, c in self.procs.items()) or "none ran"))
        for name, pts in sorted(self.mem.items()):
            pts = [p for p in pts if p[0] >= min(0.1, self.hours() / 4)]
            if len(pts) < 3:
                out.append(("NOT TESTED", f"Memory: {name}",
                            "too short a run to tell"))
                continue
            slope = _slope(pts)
            grow = pts[-1][1] - pts[0][1]
            ok = slope <= MEM_GROWTH_MB_H or grow < 20
            out.append(("PASS" if ok else "FAIL", f"Memory: {name}",
                        f"{pts[0][1]:.0f} MB to {pts[-1][1]:.0f} MB, trend "
                        f"{slope:+.1f} MB per hour (limit "
                        f"{MEM_GROWTH_MB_H:g}, after the first few minutes)"))
        for name, vals in sorted(self.cpu.items()):
            if vals:
                avg = sum(vals) / len(vals)
                out.append(("INFO", f"CPU: {name}",
                            f"average {avg:.0f}%, peak {max(vals):.0f}% of "
                            f"one core"))
        if self.disk_start is not None:
            h = max(self.hours(), 1e-6)
            rate = (self.disk_now - self.disk_start) / h
            out.append(("PASS" if rate < 50 else "FAIL", "Disk used by logs",
                        f"{self.disk_start:.1f} MB to {self.disk_now:.1f} MB "
                        f"({rate:.1f} MB per hour; limit 50)"))
        if self.pauses:
            out.append(("FAIL", "PC paused or slept", "; ".join(
                f"{p[1]:.0f} s at {now_text(p[0])}" for p in self.pauses)))
        else:
            out.append(("PASS", "PC paused or slept", "never"))
        if self.cpu_freq_low:
            pct, at, cur, mx = self.cpu_freq_low
            out.append(("INFO", "Processor speed (throttling)",
                        f"lowest reading {cur:.0f} MHz of {mx:.0f} "
                        f"({pct:.0f}%) at {now_text(at)}"))
        if getattr(self, "power", None):
            out.append(("INFO", "Windows power events during the run",
                        " | ".join(self.power[:10])))
        return out

    def hours(self):
        return ((self.ended or time.time()) - self.started) / 3600.0

    @property
    def passed(self):
        return all(v != "FAIL" for v, _t, _d in self.items())

    def write_report(self):
        items = self.items()
        verdict = ("PASSED" if all(v != "FAIL" for v, _t, _d in items)
                   else "FAILED")
        state = ("finished" if self.finished else
                 "STILL RUNNING (this file is rewritten every minute)")
        lines = [
            "LTC Player bench soak test",
            "BENCH ONLY: no lasers, flames or lights connected.",
            "",
            f"Result: {verdict if self.finished else verdict + ' so far'}",
            f"Run: {state}. Started {now_text(self.started)}, "
            f"{self.hours():.2f} of {self.seconds / 3600:g} hour(s).",
            f"Program: {ltcwin.version_line('LTC Player')}",
            f"Stream Deck: {'Mini plugged in, deck program running' if self.deck else 'not plugged in, deck program not run'}",
            f"Audio device: {self.audio_desc or 'none (see Audio interface below)'}",
            f"flamesafe config: copied from {self.fs_source}, sACN forced "
            f"to 127.0.0.1",
            "",
        ]
        if self.checks:
            lines.append("About this PC:")
            lines += [f"  - {c}" for c in self.checks]
            lines.append("")
        for v, title, detail in items:
            lines.append(f"[{v}] {title}")
            lines.append(f"    {detail}")
        lines += ["", "What happened, in order:"]
        lines += [f"  {now_text(t)}  {s}" for t, s in NOTES[-200:]]
        lines += ["", "Not exercised by this test: BEYOND, MadMapper and "
                  "Art-Net timecode (nothing in the running engine sends "
                  "them in this build), real LTC input, and the show audio "
                  "player itself.", "",
                  f"Every 5 s sample: {os.path.join(self.dir, 'samples.csv')}",
                  f"Program logs: {self.dir}"]
        text = "\r\n".join(lines) + "\r\n"
        for p in self.report_paths:
            try:
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(text)
            except OSError:
                pass


def _slope(pts):
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    den = sum((p[0] - mx) ** 2 for p in pts)
    return sum((p[0] - mx) * (p[1] - my) for p in pts) / den if den else 0.0


def folder_mb(folders):
    total = 0
    for f in folders:
        for root, _d, names in os.walk(f):
            for n in names:
                try:
                    total += os.path.getsize(os.path.join(root, n))
                except OSError:
                    pass
    return total / 1e6


def run_text(args):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=30,
                           errors="replace")
        return r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def desktop_dir():
    if ltcwin.WINDOWS:
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0,
                                                      buf) == 0:
                return buf.value
        except Exception:
            pass
    d = os.path.join(os.path.expanduser("~"), "Desktop")
    return d if os.path.isdir(d) else None


def ask_hours():
    print("LTC Player bench soak test. BENCH ONLY: nothing may be connected "
          "to lasers, flames or lights.\n")
    while True:
        try:
            a = input("How long? Type 1, 8 or 24 hours and press Enter "
                      "(just Enter = 8): ").strip()
        except EOFError:
            return 8
        if a in ("", "8"):
            return 8
        if a in ("1", "24"):
            return int(a)
        print("Please type 1, 8 or 24.")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ltcwin.prepare_stdio()
    rc = ltcwin.common_flags("ltcplay-soak", argv, lambda: iter(
        ["the soak test loads"]))
    if rc is not None:
        return rc
    wait = "--no-wait" not in argv
    seconds = None
    device = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--hours":
            seconds = float(argv[i + 1]) * 3600
            i += 1
        elif a == "--minutes":
            seconds = float(argv[i + 1]) * 60
            i += 1
        elif a == "--audio-device":
            device = argv[i + 1]
            i += 1
        i += 1
    if seconds is None:
        seconds = ask_hours() * 3600
    # The show audio's way of loading sounddevice (ASIO on Windows), before
    # anything else can load it another way.
    try:
        from ltcplay import showaudio
        showaudio.import_sounddevice()
    except Exception:
        pass
    import supervisor as sup
    sup.QUIET = True
    was_running = sup.supervisor_running()
    had_stop = os.path.exists(sup.stop_file())
    if not was_running:
        # Keep the sign-in task from starting LTC Player in the middle of
        # the run: it would find these programs and adopt them.
        try:
            with open(sup.stop_file(), "w", encoding="utf-8") as fh:
                fh.write("soak test\n")
        except OSError:
            pass
    if was_running:
        busy, why = sup.show_running(sup.load_settings()["port"])
        if busy:
            print(why + " The soak test was not started.")
            return 3
        print("Stopping LTC Player first (the safe way)...")
        if sup.cmd_stop() != 0:
            print("LTC Player did not stop, so the soak test was not "
                  "started. See " + sup.log_dir())
            return 3
    ltcwin.clean_stop_on_logoff()
    import signal
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    soak = Soak(seconds, device)
    ok = False
    try:
        ok = soak.run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        note(f"THE SOAK TEST ITSELF FAILED: {type(e).__name__}: {e}")
        soak.finished = True
        soak.ended = time.time()
        try:
            soak.write_report()
        except Exception:
            pass
    finally:
        if was_running:
            print("Starting LTC Player again...")
            sup.cmd_start()
        elif not had_stop:
            try:
                os.remove(sup.stop_file())
            except OSError:
                pass
    print("")
    print("Result: " + ("PASSED" if ok else "FAILED"))
    print("The report is here:")
    for p in soak.report_paths:
        if os.path.exists(p):
            print("    " + p)
    if wait:
        try:
            input("\nPress Enter to close this window. ")
        except EOFError:
            pass
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
