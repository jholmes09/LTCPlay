"""ltcplay-soak.exe: the bench soak test. A PC-only stress test of the show PC
and of the software, with NO lasers, flames or lights connected.

BENCH BUILD ONLY (branch bench-build): this runs the WHOLE Fire & Ice stack
the way show night does, from the real programs in this install:

  - ltcplay.exe as `ltc serve` with the scheduler and the Fire & Ice show
    conductor (fire_ice.py): a bench schedule starts a show every few
    minutes on its own (scheduler_performs, auto_start when_run_pressed),
    once this program has pressed Run;
  - each show plays its audio through the show audio player on the show's
    interface (picked by exact name; never the Windows default), which is
    the clock: Art-Net timecode to MadMapper's address and the pixels follow
    it; the conductor brings the lasers (BEYOND) and the video (MadMapper)
    up through their real links, and takes them down between shows;
  - the flame link (flamelink.FlameLink, built from flamesafe's config)
    sends the show's flame universe to the real flamesafe.exe; no group can
    arm (nothing presses the Stream Deck), so flamesafe's sACN, forced to
    127.0.0.1, must stay all zeros;
  - ltcplay-deck.exe too, when a Stream Deck Mini is plugged in.

Every output goes to 127.0.0.1, where this program listens and times it:
Art-Net pixels and timecode (6454), BEYOND (its OSC port), MadMapper (its
OSC port), the flame link (through a relay that forwards every frame to
flamesafe unchanged), flamesafe's sACN (5568) and its status frames. Nothing
leaves this PC.

A plain-language report is rewritten every minute (so a crash still leaves
one) and finished at the end, on the Desktop and in the logs folder. It
keeps "nothing attached, expected" separate from real faults.

    ltcplay-soak.exe                 asks how long (1, 8 or 24 hours; Enter = 8)
    ltcplay-soak.exe --hours 8
    ltcplay-soak.exe --audio-device "Focusrite USB ASIO"
    ltcplay-soak.exe --minutes 6 --no-wait --fake-audio   (the CI run: a
        runner has no audio interface, so the engine's show audio uses the
        test suite's stand-in device and the report says so in capitals)

With MadMapper and BEYOND installed (all programs mode) the run is split
into blocks of 1 h 50 min, clear of the demos' 2 hour limit (8 hours is 4
blocks). Each block waits until both programs answer, saying what to click;
between blocks everything stops cleanly and the operator quits and starts
both programs again. One combined report, with a section for each block.

Not exercised: Hold, Resume and Abort (no page route presses them in this
build; they come with the iPad remote, PR #39), announcements, real LTC
input, and anything actually lighting up.
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
import soak_apps
import soak_exercise

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 7878
ARTNET_PORT = 6454
SACN_PORT = 5568
DECK_STATUS_RELAY = 5579
FLAME_RELAY = 5581         # the engine's flame link sends here; relayed on
STATUS_MIRROR = 5583       # flamesafe's status copy for the engine (PR #39)
MM_PORT = 8010             # MadMapper's OSC port, as the bench config says
# All-programs mode (Jeff, 2026-10-04): MadMapper and BEYOND themselves run
# on this PC and get LTC Player's real commands on the show's own ports
# (soak_apps), so this program cannot listen there. The pixel output and a
# copy of the Art-Net timecode then go to this address instead, where this
# program times them; MadMapper and BEYOND keep 127.0.0.1.
SOAK_IP = "127.0.0.9"
# BEYOND's own Art-Net timecode (beyond_blank "timecode", the default:
# BEYOND Essentials has no OSC input). In fallback mode this program
# listens there and times the black zone against the show zone.
BEYOND_TC_IP = "127.0.0.2"
BEYOND_PORT = 8100         # BEYOND's OSC port (bench B8 used 8100)
SHOW_S = 100               # each generated show's length
SHOW_EVERY_MIN = 3         # the bench schedule: a show every 3 minutes
TC_PERIOD_MS = 1000.0 / 30  # Art-Net timecode, one packet per frame at 30
TC_GAP_MS = 100.0          # this soak: inside a show, never 3 frames missing
SHOW_GAP_S = 1.0           # timecode silent this long: between two shows
FAKE_AUDIO_ENV = "LTCPLAY_BENCH_FAKE_AUDIO"
# Fault lines that only mean "nothing is attached on this bench". Each is
# (what to look for in the line, what it means). Anything else is real.
EXPECTED_FAULTS = (
    ("heartbeat", "MadMapper is not running, so its heartbeat never "
                  "arrives"),
    ("madmapper", "MadMapper is not running"),
    ("example key", "the bench uses flamesafe's example key"),
    ("unconfirmed", "flamesafe's example group map is unconfirmed"),
)
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
def udp_listener(port, handle, name, ip="127.0.0.1"):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    except OSError:
        pass
    s.bind((ip, port))
    s.settimeout(0.5)

    def run():
        while not STOP.is_set():
            try:
                data, addr = s.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                if STOP.is_set() or s.fileno() == -1:
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

NETWORKS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Networks computer="bench">
  <Controller Id="1" Name="Pixels" Type="Ethernet" IP="127.0.0.1" ActiveState="Active">
    <network NetworkType="ArtNET" ComPort="127.0.0.1" BaudRate="1" MaxChannels="510" Enabled="Yes" />
  </Controller>
  <Controller Id="2" Name="Flames" Type="Ethernet" IP="127.0.0.9" ActiveState="Inactive">
    <network NetworkType="E131" ComPort="127.0.0.9" BaudRate="1" MaxChannels="512" Enabled="Yes" />
  </Controller>
</Networks>
"""


def note(text):
    NOTES.append((time.time(), text))
    print(f"{now_text()}  {text}", flush=True)


# ------------------------------------------------------------ the soak ---

def windows_update_state(reg=None, services=None):
    """One sentence on whether Windows Update can restart this PC during a
    run. Disabled outright (its services wuauserv, UsoSvc and WaaSMedicSvc
    disabled, or DisableWindowsUpdateAccess=1) is OK; paused says until
    when; anything else is NOT paused. `reg(path, name)` and
    `services(name)` are injectable for tests."""
    if reg is None:
        def reg(path, name):
            import winreg
            try:
                k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
                return winreg.QueryValueEx(k, name)[0]
            except OSError:
                return None
    if services is None:
        def services(name):
            # Start type 4 is Disabled.
            return reg(r"SYSTEM\CurrentControlSet\Services" + "\\" + name,
                       "Start")
    off = [n for n in ("wuauserv", "UsoSvc", "WaaSMedicSvc")
           if services(n) == 4]
    no_access = reg(r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate",
                    "DisableWindowsUpdateAccess") == 1
    if len(off) == 3 or (no_access and "wuauserv" in off):
        why = ", ".join(off) + (" disabled" if off else "")
        if no_access:
            why += (", and " if off else "") + "DisableWindowsUpdateAccess=1"
        return f"Windows Update is disabled on this PC ({why}). OK."
    until = reg(r"SOFTWARE\Microsoft\WindowsUpdate\UX\Settings",
                "PauseUpdatesExpiryTime")
    if until:
        return f"Windows Update is paused until {until}."
    return ("Windows Update is NOT paused or disabled. An update restart "
            "during this run would end it early. (Checklist: pause updates "
            "for the show weeks.)")

class Soak:
    def __init__(self, seconds, audio_device=None, folder=None, block=None):
        self.seconds = seconds
        self.audio_device = audio_device
        stamp = time.strftime("%Y-%m-%d_%H%M")
        import supervisor as sup
        self.sup = sup
        # A block of a longer run (Blocks): its own folder and report there;
        # the combined report goes on the Desktop.
        self.block = block
        self.on_report = None
        self.interrupted = False
        self.notes_from = len(NOTES)
        self.dir = folder or os.path.join(sup.appdata_dir(), "soak", stamp)
        os.makedirs(self.dir, exist_ok=True)
        self.report_name = (f"LTC Player soak report {stamp}.txt" if not block
                            else f"block {block[0]} report.txt")
        self.report_paths = [os.path.join(self.dir, self.report_name)]
        desk = desktop_dir()
        if desk and not block:
            self.report_paths.append(os.path.join(desk, self.report_name))
        self.started = time.time()
        self.ended = None
        self.finished = False
        self.pixel_streams = {}
        self.pixel_quiet_until = float("inf")   # nothing timed before GO
        self.stopping = False
        self.fake_audio = False
        self.audio_device = audio_device
        self.link_gaps = Intervals(1000.0 / 40, None, LINK_GAP_MS)
        self.flame_frames = 0
        self.flame_nonzero = 0
        self.flame_disarms = 0
        self.tc = Intervals(TC_PERIOD_MS, None, TC_GAP_MS)
        self.tc_last = None
        self.show_starts = 0
        self.beyond_cmds = []     # (time, "blank"/"unblank")
        self.beyond_lit_outside = 0
        self.mm_packets = 0
        self.mm_addresses = {}
        self.audio_snap = {}
        self.audio_worst = {}
        self.slots = {}
        self.mode = "fallback"      # or "all programs" (see choose_mode)
        self.apps = {}              # name -> install path, all-programs mode
        self.app_watch = {}         # name -> soak_apps.AppWatch
        self.app_cpu = {}           # name -> [cpu %]
        self.app_mem = {}           # name -> [(hours, MB)]
        self.gpu = []               # [percent]
        self.heat = soak_apps.HeatJudge()
        self.disk = soak_apps.DiskJudge()
        self._disk_since = None
        self.want_mode = "auto"
        self.app_started = {}       # name -> started by this soak?
        self.late_lines = []     # the flame link's own late-frame lines
        # Times when nothing had pressed Run yet: from the start until the
        # first Run, and from an engine restart until Run is pressed again.
        # A show that comes due then fails to start by design ("Run has not
        # been pressed"), and is not a fault of the program under test.
        self.no_run = [[time.time(), None]]
        self.before_run = 0
        self.journal_real = []
        self.journal_expected = {}
        self.engine_env_dir = None
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
        self.virtual_deck = False   # the deck program with no hardware
        self.ex = None              # soak_exercise.Exerciser
        self.judge = None           # soak_exercise.SacnJudge
        self.fs_armed = {}          # group -> armed, from flamesafe status
        self.tc_last_wall = None
        self.hold_continued = 0
        self.measured = ""
        self.bey_black = 0          # BEYOND stream packets in the black zone
        self.bey_show = 0           # ... in the show zone
        self.bey_bad = []           # (time, why): show zone while dark
        self.bey_stats = None       # the engine's own counts (all mode)
        self.outside_timing = True
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
        self.checks.append(windows_update_state())
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
                        else:
                            self.checks.append(f"{name} (plugged in): Never. "
                                               f"OK.")

    def make_show(self):
        """The bench show, generated: one 100 s cue whose FSEQ covers a
        pixel controller (Art-Net to 127.0.0.1) and an Inactive "Flames"
        controller (the flame universe, which only the flame link reads),
        a 48 kHz 24-bit stereo WAV (a quiet tone), the show file with the
        show audio as the clock, the bench schedule, and the Fire & Ice
        settings beside it."""
        import math
        import struct as _st
        import wave
        import test_show_fixtures as fx
        self.show_dir = os.path.join(self.dir, "show")
        os.makedirs(self.show_dir, exist_ok=True)
        with open(os.path.join(self.show_dir, "xlights_networks.xml"), "w",
                  encoding="utf-8") as fh:
            fh.write(NETWORKS_XML if self.mode == "fallback" else
                     NETWORKS_XML.replace('IP="127.0.0.1"',
                                          f'IP="{SOAK_IP}"').replace(
                         'ComPort="127.0.0.1"', f'ComPort="{SOAK_IP}"'))
        fx.write_fseq(os.path.join(self.show_dir, "bench.fseq"),
                      frame_count=SHOW_S * 40, channel_count=1022,
                      step_ms=25, compression="zlib", block_frames=400,
                      fill=lambda i: (i * 3) & 0xFF, media_file="bench.wav")
        n = SHOW_S * 48000
        amp = int(0.03 * 8388607)
        with wave.open(os.path.join(self.show_dir, "bench.wav"), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(3)
            w.setframerate(48000)
            step = 48000
            for start in range(0, n, step):
                frames = bytearray()
                for k in range(start, min(n, start + step)):
                    v = int(amp * math.sin(2 * math.pi * 440 * k / 48000))
                    b = _st.pack("<i", v)[:3]
                    frames += b + b
                w.writeframes(bytes(frames))
        self.audio_name = self.pick_audio_device()
        doc = {"fps": 30, "show_dir": self.show_dir, "on_lost": "freerun",
               "cues": [{"tc": "00:00:00:00", "fseq": "bench.fseq",
                         "name": "Show"}],
               "clock": {"source": "audio_master",
                         "artnet": {"nodes": (
                             {"MadMapper": "127.0.0.1", "BEYOND": BEYOND_TC_IP}
                             if self.mode == "fallback" else
                             {"MadMapper": "127.0.0.1", "BEYOND": "127.0.0.2",
                              "Soak": SOAK_IP})},
                         "audio": {"device": self.audio_name or "none found",
                                   "channels": 2,
                                   "cues": {"show": {
                                       "cue": "Show",
                                       "stems": [{"file": "bench.wav",
                                                  "channels": [1, 2]}]}}}}}
        with open(os.path.join(self.show_dir, "bench.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)

    def pick_audio_device(self):
        """The show audio interface's exact name: --audio-device, else the
        input LTC Player has saved, else a Focusrite or Scarlett output
        offered through ASIO. Only listed, never opened here (ASIO lets one
        program have it, and that is the engine). None when there is none."""
        if self.fake_audio:
            return self.audio_device or "Bench stand-in device"
        try:
            from ltcplay import showaudio, settings as settings_mod
            sd = showaudio.import_sounddevice()
            outs = [d for d in sd.query_devices()
                    if d.get("max_output_channels", 0) > 0]
            apis = [a.get("name", "") for a in sd.query_hostapis()]
            names = [str(d.get("name", "")) for d in outs]
            self.audio_outputs = sorted(set(names))
            if self.audio_device:
                return self.audio_device
            saved = settings_mod.load().get("device")
            if saved and saved in names:
                return saved
            for d in outs:
                h = d.get("hostapi", -1)
                api = apis[h] if 0 <= h < len(apis) else ""
                n = str(d.get("name", ""))
                if api == "ASIO" and any(w in n.lower() for w in
                                         ("focusrite", "scarlett")):
                    return n
        except Exception as e:
            note(f"could not list the audio outputs: {e}")
        return None

    def make_schedule(self):
        """A show every SHOW_EVERY_MIN minutes from 02:00 to midnight (a
        show night's hours: the 2 AM nightly reset comes in between), every
        day, in this PC's own clock time, for today and the next two days,
        and the Fire & Ice settings that make the scheduler perform, with
        BEYOND and MadMapper on this PC."""
        import datetime as _dt
        self.sched_dir = os.path.join(self.dir, "schedule")
        os.makedirs(self.sched_dir, exist_ok=True)
        local = _dt.datetime.now().astimezone()
        hours = round(local.utcoffset().total_seconds() / 3600)
        # Etc/GMT zones count the other way round: UTC-4 is Etc/GMT+4.
        tz = "UTC" if hours == 0 else f"Etc/GMT{-hours:+d}"
        today = local.date()
        night = {"first_start": "02:00", "interval_min": SHOW_EVERY_MIN,
                 "last_end": "23:59"}
        self.tz_name = tz
        rule = {"timezone": tz,
                "season": {"first_date": (today - _dt.timedelta(days=1))
                           .isoformat(),
                           "last_date": (today + _dt.timedelta(days=2))
                           .isoformat()},
                "weekly": {d: dict(night) for d in
                           ("mon", "tue", "wed", "thu", "fri", "sat",
                            "sun")},
                "exceptions": {}, "show_len_s": SHOW_S + 5, "guard_s": 30,
                "late_grace_s": 15}
        self.rule_path = os.path.join(self.sched_dir,
                                      "ltcplay_schedule.json")
        with open(self.rule_path, "w", encoding="utf-8") as fh:
            json.dump(rule, fh, indent=1)
        fi = {"scheduler_performs": True, "auto_start": "when_run_pressed",
              "show_cue": "Show",
              "madmapper": {"host": "127.0.0.1", "port": (
                  MM_PORT if self.mode == "fallback" else
                  soak_apps.MADMAPPER_PORT),
                            "show_bank": "Bank-1", "surfaces": ["Quad-1"]},
              "beyond": {"host": ("127.0.0.1" if self.mode == "fallback"
                                  else soak_apps.BEYOND_IP), "port": (
                  BEYOND_PORT if self.mode == "fallback" else
                  soak_apps.BEYOND_PORT)},
              "flamesafe_config": self.engine_fs_cfg,
              "flame_controller": "Flames",
              "beyond_blank": "timecode",
              "beyond_timecode_ip": BEYOND_TC_IP,
              "show_name": "Ignite the Night",
              "venue": "Thanksgiving Point",
              "notes": "BENCH ONLY, written by the soak test"}
        if self.mode != "fallback":
            # MadMapper's heartbeat track (its project sends it; see the
            # checklist), read by the engine's own watchdog.
            fi["madmapper"]["heartbeat"] = {
                "port": soak_apps.HEARTBEAT_PORT,
                "address": soak_apps.HEARTBEAT_ADDRESS,
                "show_len_s": SHOW_S + 5}
        with open(os.path.join(self.sched_dir, "ltcplay_fire_ice.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(fi, fh, indent=1)

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
        # flamesafe's copy of its status for the engine (PR #39), so the
        # engine's flame link sees flamesafe confirm a disarm.
        cfg["link"]["status_mirror_port"] = STATUS_MIRROR
        cfg["groups"] = cfg.get("groups", [])[:3]
        cfg["log_dir"] = os.path.join(self.dir, "flamesafe-journal")
        self.fs_cfg_doc = cfg
        self.fs_cfg = os.path.join(self.dir, "flamesafe-soak.json")
        with open(self.fs_cfg, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=1)
        # The engine's copy: its flame link sends to the relay, which
        # forwards every frame to flamesafe's own port from one socket.
        eng = json.loads(json.dumps(cfg))
        eng["link"]["listen_port"] = FLAME_RELAY
        self.engine_fs_cfg = os.path.join(self.dir,
                                          "flamesafe-soak-engine.json")
        with open(self.engine_fs_cfg, "w", encoding="utf-8") as fh:
            json.dump(eng, fh, indent=1)
        self.flamesafe_port = int(cfg["link"]["listen_port"])
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
        op = struct.unpack_from("<H", b, 8)[0]
        if op == 0x9700:
            self.on_timecode(t)
            return
        if op != 0x5000:
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

    def on_beyond_tc(self, b, t, addr=None):
        """BEYOND's own timecode stream (fallback mode): hour
        BLACK_HOUR is the black zone; anything else is the show's, which
        is a fault while the lasers must be dark: no show timecode moving
        to MadMapper, or inside the exerciser's Hold or Abort."""
        if self.stopping or len(b) < 19 or b[:8] != b"Art-Net\0" or \
                struct.unpack_from("<H", b, 8)[0] != 0x9700:
            return
        if b[17] == 23:
            self.bey_black += 1
            return
        self.bey_show += 1
        why = None
        if self.tc_last is None or t - self.tc_last > 1.0:
            why = "no show running"
        elif self.ex is not None:
            w = self.ex.in_window(time.time(), ("hold", "aborted"))
            if w:
                why = f"inside the {w}"
        if why and (not self.bey_bad or
                    time.time() - self.bey_bad[-1][0] > 5):
            self.bey_bad.append((time.time(),
                                 f"BEYOND got show timecode "
                                 f"{b[17]:02d}:{b[16]:02d}:{b[15]:02d}:"
                                 f"{b[14]:02d} with {why}"))

    def show_time(self):
        """Seconds into the show now playing, or None between shows."""
        if self.tc_last is None or self.go_wall is None or \
                time.perf_counter() - self.tc_last > SHOW_GAP_S:
            return None
        return time.perf_counter() - self.go_wall

    def on_timecode(self, t):
        """Art-Net timecode: a silence over SHOW_GAP_S is the gap between
        two shows, not a fault; inside a show, every frame is timed. A
        silence the exerciser's own Hold made is the same show going on."""
        wall = time.time()
        if self.tc_last is not None and t - self.tc_last > SHOW_GAP_S and \
                self.ex is not None and self.tc_last_wall is not None and \
                any(w[2] == "hold" and w[0] - 1.0 <= self.tc_last_wall and
                    (w[1] is None or w[1] + 1.0 >= self.tc_last_wall)
                    for w in self.ex.windows):
            self.hold_continued += 1
            self.go_wall += t - self.tc_last      # the show's own clock
            self.tc.reset_gap()
            self.pixel_quiet_until = t + 5.0
            self.tc_last, self.tc_last_wall = t, wall
            return
        self.tc_last_wall = wall
        if self.tc_last is None or t - self.tc_last > SHOW_GAP_S:
            self.show_starts += 1
            self.tc.reset_gap()
            self.go_wall = t
            # The first seconds of each show (the engine starting the cue)
            # are not timed for the pixels.
            self.pixel_quiet_until = t + 5.0
        else:
            self.tc.tick(t)
        if self.tc.last is None:
            self.tc.last = t
        self.tc_last = t

    def on_flame(self, b, t, addr=None):
        """The engine's flame link, relayed to flamesafe unchanged."""
        try:
            self.flame_sock.sendto(b, ("127.0.0.1", self.flamesafe_port))
        except OSError:
            pass
        if self.stopping:
            return
        try:
            doc = json.loads(b.decode("utf-8"))
        except Exception:
            return
        kind = doc.get("t")
        if kind == "flame":
            self.link_gaps.tick(t)
            self.flame_frames += 1
            if any(doc.get("values") or ()):
                self.flame_nonzero += 1
        elif kind == "disarm_all":
            self.flame_disarms += 1

    def on_beyond(self, b, t, addr=None):
        from ltcplay import madmapper as MM
        try:
            address, _ = MM._read_osc_string(b, 0)
            f = MM.decode_float(b)
        except Exception:
            return
        v = None if f is None else f[1]
        what = "blank" if v in (0, 0.0) else "unblank"
        self.beyond_cmds.append((time.time(), what, address))
        if what == "unblank" and (self.tc_last is None or
                                  t - self.tc_last > SHOW_GAP_S + 2):
            # Lasers asked up with no show timecode moving: a real fault.
            self.beyond_lit_outside += 1

    def on_madmapper(self, b, t, addr=None):
        from ltcplay import madmapper as MM
        self.mm_packets += 1
        try:
            address, _ = MM._read_osc_string(b, 0)
        except Exception:
            address = "?"
        key = "/".join(address.split("/")[:3])
        self.mm_addresses[key] = self.mm_addresses.get(key, 0) + 1

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
        vals = b[126:126 + 512]
        if any(vals):
            self.sacn_nonzero += 1
        if self.judge is not None and not self.stopping:
            now = time.time()
            in_show = self.tc_last is not None and \
                t - self.tc_last <= soak_exercise.STALE_TC_S
            self.judge.packet(vals, now, dict(self.fs_armed), in_show,
                              self.ex.in_window(now) if self.ex else None)
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
        for g in st.get("groups") or []:
            if isinstance(g, dict) and g.get("name"):
                self.fs_armed[g["name"]] = g.get("armed") == "armed"
        state = (st.get("frames") or {}).get("state")
        if self.stopping:
            return
        if state == "fresh":
            self.fs_fresh_seen = True
        elif state == "stale" and self.fs_state == "fresh":
            self.fs_stale_events += 1
            note("flamesafe says the flame link went STALE")
        self.fs_state = state or self.fs_state
        if (st.get("frames") or {}).get("seq") is None and \
                self.flame_frames:
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
        # The engine and the deck are started exactly as the installed LTC
        # Player starts them in showpc.json's "fire_ice" show mode (the
        # supervisor's own wanted_args), with this soak's show folder,
        # schedule and port; only the flamesafe configs are the soak's own
        # (they route flamesafe and the deck through this test's relays).
        import supervisor as sup
        want = sup.wanted_args({
            "show_folder": self.show_dir, "flamesafe_config": self.fs_cfg,
            "port": PORT, "run_flamesafe": True, "run_deck": True,
            "show_mode": "fire_ice", "schedule": self.rule_path})
        engine, why = want["engine"]
        if engine is None or "--schedule" not in engine:
            raise RuntimeError(f"the supervisor's fire_ice mode did not give "
                               f"the engine its schedule: {why or engine}")
        deck = want["deck"][0] or []
        if f"http://127.0.0.1:{PORT}" not in deck:
            raise RuntimeError(f"the supervisor's fire_ice mode did not give "
                               f"the deck the engine's address: {want['deck']}")
        deck = ["--flamesafe-config", self.deck_cfg] + \
            deck[deck.index("--ltcplay-url"):]
        args = {"flamesafe": [self.fs_cfg], "engine": engine,
                "deck": deck}[name]
        out = open(os.path.join(self.dir, f"{name}.log"), "a",
                   encoding="utf-8")
        # Each program shares this window's console (its output goes to a
        # log file) in a process group of its own, so Ctrl-Break reaches
        # it alone, and closing this window stops it cleanly too.
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if ltcwin.WINDOWS else 0
        env = dict(os.environ)
        if name == "engine":
            # The engine's own files (tonight's list, the night journal, its
            # lock and saved settings) in the soak's folder, never the show
            # account's real ones.
            self.engine_env_dir = os.path.join(self.dir, "engine-data")
            os.makedirs(self.engine_env_dir, exist_ok=True)
            env["LOCALAPPDATA"] = self.engine_env_dir
            env["XDG_STATE_HOME"] = self.engine_env_dir
            if self.fake_audio:
                env[FAKE_AUDIO_ENV] = self.audio_name or "1"
        if name == "deck" and self.virtual_deck:
            env["LTCPLAY_BENCH_VIRTUAL_DECK"] = "1"
        p = subprocess.Popen(self.program_cmd(name) + args,
                             stdin=subprocess.DEVNULL, stdout=out,
                             stderr=subprocess.STDOUT, cwd=self.dir,
                             creationflags=flags, env=env)
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
        """Press Run on the show file, once: from then on the scheduler
        starts every show itself."""
        deadline = time.time() + 60
        while time.time() < deadline:
            if "error" not in self.engine("/api/state"):
                break
            time.sleep(0.5)
        r = self.engine("/api/start", {"timeline": "bench.json"}, timeout=30)
        if r.get("error"):
            note(f"the engine refused Run: {r['error']}")
            return False
        note("Run pressed on the bench show; the scheduler starts each show "
             "from here on")
        self.loops += 1
        if self.no_run and self.no_run[-1][1] is None:
            self.no_run[-1][1] = time.time()
        return True

    # --------------------------------------------------------- sample ---
    def sample(self, hours):
        self.sample_apps(hours)
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
            aud = ((st.get("clock") or {}).get("audio") or {})
            if aud:
                self.audio_snap = aud
                for k in ("underflows", "losses", "render_errors",
                          "respawns", "clipped", "outliers"):
                    v = aud.get(k)
                    if isinstance(v, (int, float)) and \
                            v > self.audio_worst.get(k, 0):
                        if k in ("underflows", "losses", "render_errors",
                                 "respawns"):
                            note(f"show audio {k}: now {v}")
                        self.audio_worst[k] = v
            _ = nowc
        elif self.engine_last is not None and "error" in st:
            self.engine_errors.append((time.time(), st["error"]))
        tn = self.engine("/api/schedule/tonight")
        for sl in (tn.get("slots") or tn.get("tonight", {}).get("slots")
                   or []) if isinstance(tn, dict) else []:
            if isinstance(sl, dict) and "show" in sl:
                self.slots[sl["show"]] = (sl.get("status"), sl.get("reason"))
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
        STOP.clear()
        note((f"block {self.block[0]} of {self.block[1]} " if self.block
              else "soak test ") +
             f"starting for {self.seconds / 3600:g} hour(s); "
             f"folder {self.dir}")
        self.pc_checks()
        for c in self.checks:
            note("PC check: " + c)
        note(self.choose_mode(self.want_mode))
        self.container = ltcwin.package_name()
        if self.container:
            note(f"WARNING: this soak runs inside another app's container "
                 f"({self.container}): Windows redirects its files there. "
                 f"Start it from the Start menu for a true reading.")
        note(ltcwin.settings_folder_line())
        self.make_show()
        self.flamesafe_config()
        # The deck program always runs: it owns flamesafe's arm link, and
        # the exerciser arms through it (soak_exercise). With no Stream
        # Deck plugged in it runs a virtual one (entry_deck.py, bench only).
        self.virtual_deck = not self.deck_plugged_in()
        self.deck = True
        note("Stream Deck Mini " + ("not plugged in: the deck program runs "
                                    "with a virtual deck, so arming goes "
                                    "through it" if self.virtual_deck else
                                    "found: the deck program runs too"))
        self.judge = soak_exercise.SacnJudge(
            [(g["name"], g["safety"], g["fire"])
             for g in self.fs_cfg_doc["groups"]])
        self.make_schedule()
        self.relay = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.flame_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Every port this program would listen on is checked first: one a
        # real program holds is never taken (show PC, 2026-10-04: a bind
        # MadMapper or BEYOND held crashed the soak).
        need = [("127.0.0.1", SACN_PORT, "flamesafe's sACN"),
                ("127.0.0.1", self.status_port, "flamesafe's status"),
                ("127.0.0.1", FLAME_RELAY, "the flame link relay")]
        if self.mode == "fallback":
            need += [(BEYOND_TC_IP, ARTNET_PORT, "BEYOND's timecode"),
                     ("127.0.0.1", ARTNET_PORT, "Art-Net"),
                     ("127.0.0.1", BEYOND_PORT, "BEYOND's OSC"),
                     ("127.0.0.1", MM_PORT, "MadMapper's OSC")]
        refuse_held_ports(need)
        socks = [udp_listener(SACN_PORT, self.on_sacn, "sacn"),
                 udp_listener(self.status_port, self.on_status, "status"),
                 udp_listener(FLAME_RELAY, self.on_flame, "flame relay")]
        if self.mode == "fallback":
            socks += [udp_listener(ARTNET_PORT, self.on_beyond_tc,
                                   "beyond timecode", ip=BEYOND_TC_IP),
                      udp_listener(ARTNET_PORT, self.on_artnet, "artnet"),
                      udp_listener(BEYOND_PORT, self.on_beyond, "beyond"),
                      udp_listener(MM_PORT, self.on_madmapper, "madmapper")]
            self.measured = ("pixels, timecode, BEYOND's and MadMapper's "
                             "commands: overheard on their own ports on this "
                             "PC (neither program ran)")
        else:
            # MadMapper and BEYOND were started by hand and answered before
            # this block began (wait_for_apps). They hold 6454, 8000 and
            # 8100, so their commands are not overheard; the pixels and a
            # copy of the timecode go to SOAK_IP, timed here when that
            # address can be listened on.
            try:
                socks.append(udp_listener(ARTNET_PORT, self.on_artnet,
                                          "artnet", ip=SOAK_IP))
                self.measured = (f"pixels and timecode: timed here from the "
                                 f"copy the engine sends {SOAK_IP}:6454; "
                                 f"MadMapper and BEYOND: process, window and "
                                 f"OSC port held (their commands are not "
                                 f"overheard: they hold those ports)")
            except OSError:
                who = ", ".join(soak_apps.port_owners(ARTNET_PORT)) or \
                    "another program"
                self.outside_timing = False
                self.measured = (f"pixels and timecode NOT timed from "
                                 f"outside: {SOAK_IP}:6454 could not be "
                                 f"listened on ({who} holds 6454); see the "
                                 f"engine's own counters. MadMapper and "
                                 f"BEYOND: process, window and OSC port held")
                note(f"could not listen on {SOAK_IP}:6454 ({who} holds "
                     f"6454): pixels and timecode are not timed from "
                     f"outside in this run")
        if self.fake_audio:
            note("AUDIO IS FAKE: --fake-audio, so the engine's show audio "
                 "plays to the test suite's stand-in device, not a real "
                 "interface (a CI runner has none)")
        elif self.audio_name:
            note(f"show audio interface: {self.audio_name}")
        else:
            note("NO show audio interface found: the shows cannot start. "
                 "Outputs on this PC: "
                 + ", ".join(getattr(self, "audio_outputs", [])))
        self.start_program("flamesafe")
        time.sleep(2)
        self.start_program("engine")
        if self.deck:
            self.start_program("deck")
        self.start_show()
        self.ex = soak_exercise.Exerciser(
            soak_exercise.Http(f"http://127.0.0.1:{PORT}"),
            [g["name"] for g in self.fs_cfg_doc["groups"]],
            show_time=self.show_time, show_number=lambda: self.show_starts,
            armed=lambda: dict(self.fs_armed), note=note).start()
        t0 = time.perf_counter()
        next_sample = next_report = t0
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
                            self.no_run.append([time.time(), None])
                            self.start_show()
                hours = (time.perf_counter() - t0) / 3600.0
                if time.perf_counter() >= next_sample:
                    self.sample(hours)
                    next_sample += SAMPLE_S
                if time.perf_counter() >= next_report:
                    if ltcwin.WINDOWS:
                        import datetime as _dt
                        since = self._disk_since or _dt.datetime.fromtimestamp(
                            self.started)
                        self._disk_since = _dt.datetime.now()
                        dsk = soak_apps.disk_sample(since)
                        self.disk.add(time.time(), dsk)
                        for ev in dsk["events"]:
                            note(f"STORAGE EVENT {ev[2]} {ev[1]} at {ev[0]}: "
                                 f"{ev[3]}")
                        smp = soak_apps.heat_sample()
                        self.heat.add(time.time(), smp)
                        t = smp.get("temp_c")
                        if t is not None and t > soak_apps.TEMP_LIMIT_C:
                            note(f"CPU at {t:.0f} C")
                    if self.mode != "fallback":
                        g = soak_apps.gpu_percent()
                        if g is not None:
                            self.gpu.append(g)
                    self.write_report()
                    next_report += REPORT_EVERY_S
        except KeyboardInterrupt:
            self.interrupted = True
            note("stopped early by Ctrl-C")
        finally:
            self.ended = time.time()
            if self.ex is not None:
                self.ex.close()
            self.stopping = True
            self.engine("/api/stop", {})
            for name in ("deck", "engine"):
                self.stop_program(name)
            time.sleep(1)
            self.stop_program("flamesafe")
            time.sleep(1)
            STOP.set()
            for s in socks:
                s.close()
            self.read_journal()
            self.power_events()
            self.finished = True
            self.write_report()
            self.samples.close()
        return self.passed

    def stop_all(self):
        """After a failure in this program: stop whatever it started."""
        self.stopping = True
        for name in ("deck", "engine", "flamesafe"):
            self.stop_program(name)

    def read_journal(self):
        """Every fault line the engine's night journal wrote, sorted into
        "nothing attached, expected" and real."""
        root = self.engine_env_dir
        if not root:
            return
        for d, _dirs, names in os.walk(root):
            if os.path.basename(d) != "nights":
                continue
            for n in sorted(names):
                try:
                    lines = open(os.path.join(d, n), encoding="utf-8",
                                 errors="replace").read().splitlines()
                except OSError:
                    continue
                for ln in lines:
                    try:
                        rec = json.loads(ln)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and \
                            rec.get("outcome") == "late_frame":
                        self.late_lines.append(
                            (str(rec.get("at", ""))[11:19],
                             str(rec.get("text") or "")[12:200]))
                    if not isinstance(rec, dict) or not rec.get("fault"):
                        continue
                    if self._before_run(rec.get("at")):
                        self.before_run += 1
                        continue
                    text = str(rec.get("text") or rec.get("reason") or "")
                    low = text.lower()
                    why = next((w for k, w in EXPECTED_FAULTS if k in low
                                and not (self.mode != "fallback" and
                                         k in ("heartbeat", "madmapper"))),
                               None)
                    if why:
                        self.journal_expected[why] = \
                            self.journal_expected.get(why, 0) + 1
                    else:
                        self.journal_real.append(
                            (str(rec.get("at", ""))[:19], text[:300]))

    def _before_run(self, at):
        """True for a journal time inside a stretch when nothing had pressed
        Run (with 3 s either side for the engine's own start and stop)."""
        import datetime as _dt
        try:
            t = _dt.datetime.fromisoformat(str(at)).timestamp()
        except (TypeError, ValueError):
            return False
        return any(a - 3 <= t <= (b if b is not None else float("inf")) + 3
                   for a, b in self.no_run)

    # ----------------------------------------------- all-programs mode ---
    def choose_mode(self, want="auto", names=None):
        """'all programs' when MadMapper and BEYOND are both installed (or
        running) and `want` allows it, else 'fallback'. Sets self.mode and
        self.apps; returns a sentence saying why."""
        self.mode, found, why = resolve_mode(want, names)
        if self.mode == "fallback":
            return why
        self.apps = found
        for app in found:
            self.app_watch[app] = soak_apps.AppWatch(
                app, demo_limit=(app == "BEYOND"))
        return ("all programs mode: MadMapper and BEYOND run on this PC and "
                "get LTC Player's real commands")

    def sample_apps(self, hours):
        """Is each program running and answering; its CPU and memory."""
        if self.mode == "fallback":
            return
        names = soak_apps.tasklist_names()
        hung = soak_apps.hung_names()
        mm = self.engine("/api/madmapper/state")
        wd = (mm.get("watchdog") or {}) if isinstance(mm, dict) else {}
        hb = None
        if wd.get("armed"):
            hb = wd.get("state") != "fault"
        now = time.time()
        for app, watch in self.app_watch.items():
            alive = soak_apps.running(app, names)
            a = soak_apps.APPS[app]
            held = soak_apps.port_held(a["port"], a["ip"])
            why = watch.sample(now, alive, held,
                               hb if app == "MadMapper" else None,
                               hung=any(soak_apps.is_app(app, h)
                                        for h in hung))
            if why and watch.episodes and watch.episodes[-1][0] == now:
                note(f"{app} not answering: {why}")
            if watch.demo_stopped_at == now:
                note(f"{app} stopped after "
                     f"{(now - watch.first_seen) / 3600:.1f} h: the demo's "
                     f"limit, not a fault")
        try:
            import psutil
        except ImportError:
            return
        for proc in psutil.process_iter(["name"]):
            nm = (proc.info.get("name") or "").lower()
            for app in self.app_watch:
                if soak_apps.is_app(app, nm):
                    try:
                        if not hasattr(self, "_app_ps"):
                            self._app_ps = {}
                        ps = self._app_ps.get(proc.pid)
                        if ps is None:
                            ps = self._app_ps[proc.pid] = proc
                            ps.cpu_percent(None)
                            continue
                        self.app_cpu.setdefault(app, []).append(
                            ps.cpu_percent(None))
                        self.app_mem.setdefault(app, []).append(
                            (hours, ps.memory_info().rss / 1e6))
                    except Exception:
                        pass

    def disk_item(self):
        d = self.disk
        if not ltcwin.WINDOWS or not d.samples:
            return ("NOT TESTED", "Drive (SSD)",
                    "not sampled (not Windows, or the run was too short)")
        temps = [smp["temp_c"] for _t, smp in d.samples
                 if smp.get("temp_c") is not None]
        tmax = [smp["temp_max_c"] for _t, smp in d.samples
                if smp.get("temp_max_c") is not None]
        xfer = [smp["xfer_s"] for _t, smp in d.samples
                if smp.get("xfer_s") is not None]
        parts = []
        parts.append(
            f"drive temperature {min(temps):g} to {max(temps):g} C (limit "
            f"{soak_apps.DISK_TEMP_LIMIT_C:g})" if temps else
            "drive temperature NOT readable here (Windows may need the soak "
            "run as administrator for it)")
        if tmax:
            parts.append(f"the drive's own lifetime maximum {max(tmax):g} C")
        if xfer:
            parts.append(f"average transfer up to {max(xfer) * 1000:.1f} ms "
                         f"(limit {soak_apps.DISK_TRANSFER_LIMIT_S:g} s)")
        parts.append(d.latency_line())
        faults = d.faults()
        if faults:
            parts.append("FAULTS: " + "; ".join(
                f"{t if isinstance(t, str) else now_text(t)} {w}"
                for t, w in faults[:6]))
        # The outputs' worst gaps beside any slow drive moment or event.
        near = []
        slow = [(t, f"drive slow ({x * 1000:.0f} ms a transfer)")
                for t, x in d.slow_moments()]
        evs = []
        for at, eid, src, msg in d.events:
            try:
                import datetime as _dt
                evs.append((_dt.datetime.fromisoformat(at).timestamp(),
                            f"{src} event {eid}"))
            except ValueError:
                pass
        for label, iv in (("flame link", self.link_gaps),
                          ("pixels", self.pixels), ("timecode", self.tc)):
            at = iv.longest_at
            if at is None:
                continue
            hits = [w for t, w in slow + evs if abs(t - at) <= 60]
            near.append(f"{label} worst gap {iv.longest:.1f} ms at "
                        f"{now_text(at)}: " + (", ".join(hits) if hits else
                                               "no drive stall within a "
                                               "minute"))
        if near:
            parts.append("; ".join(near))
        return ("FAIL" if faults else "PASS",
                "Drive (SSD): temperature, transfer time, storage events "
                "(sampled every minute)", "; ".join(parts))

    def heat_item(self):
        h = self.heat
        if not ltcwin.WINDOWS or not h.samples:
            return ("NOT TESTED", "CPU temperature and throttling",
                    "not sampled (not Windows, or the run was too short)")
        now = time.time()
        bad, temps = h.verdict(now)
        parts = []
        if temps:
            third = max(1, len(temps) // 3)
            first, last = temps[:third], temps[-third:]
            parts.append(f"CPU temperature {min(temps):.0f} to "
                         f"{max(temps):.0f} C (first third mean "
                         f"{sum(first) / len(first):.0f}, last third "
                         f"{sum(last) / len(last):.0f}); limit "
                         f"{soak_apps.TEMP_LIMIT_C:.0f} C")
        else:
            parts.append("CPU temperature is NOT readable on this PC "
                         "(Windows exposes no thermal zone here)")
        lim = [smp["limit_pct"] for _t, smp in h.samples
               if smp.get("limit_pct") is not None]
        perf = [smp["perf_pct"] for _t, smp in h.samples
                if smp.get("perf_pct") is not None]
        clk = [smp["clock_pct"] for _t, smp in h.samples
               if smp.get("clock_pct") is not None]
        if lim:
            parts.append(f"Windows' performance limit {min(lim):.0f} to "
                         f"{max(lim):.0f}% (100 = nothing holding the CPU "
                         f"back)")
        if perf:
            parts.append(f"processor performance {min(perf):.0f} to "
                         f"{max(perf):.0f}% of base")
        if clk:
            parts.append(f"clock {min(clk):.0f} to {max(clk):.0f}% of max")
        thr = h.throttled(now)
        if thr:
            parts.append("held back under 70% for over a minute: " + "; ".join(
                f"{now_text(a)} to {now_text(b)}" for a, b in thr[:4]))
        if h.hot:
            parts.append("over the limit at " + ", ".join(
                f"{now_text(a)} ({t:.0f} C)" for a, t in h.hot[:4]))
        parts.append("GPU temperature: not readable without installing "
                     "anything (Intel graphics expose none to Windows)")
        return ("FAIL" if bad else "PASS",
                "CPU temperature and throttling (sampled every minute)",
                "; ".join(parts))

    def app_items(self):
        out = []
        if self.mode == "fallback":
            return out
        for app, watch in self.app_watch.items():
            eps = watch.faults()
            text = "; ".join(f"{why} from {now_text(a)} to "
                             f"{now_text(b) if b else 'the end'}"
                             for a, b, why in eps[:6])
            extra = ""
            if watch.demo_stopped_at:
                extra = (f"; stopped {now_text(watch.demo_stopped_at)}, "
                         f"{(watch.demo_stopped_at - watch.first_seen) / 3600:.1f}"
                         f" h after it was first seen: the demo's limit, not "
                         f"a fault (a full license runs the whole time)")
            a = soak_apps.APPS[app]
            how = ("" if app == "MadMapper" else
                   ". BEYOND's response to commands is checked only as far "
                   "as possible: it sends nothing back, so running, its "
                   "window responding and holding its OSC port is all that "
                   "can be known")
            out.append(("FAIL" if eps else "PASS",
                        f"{app} answering (running, "
                        + ("" if app == "MadMapper" else "window responding, ")
                        + f"holding its OSC port {a['ip']}:{a['port']}"
                        + (", heartbeat fresh)" if app == "MadMapper" else ")"),
                        (f"{app} not answering: {text}" if eps else
                         f"answered all block ({watch.samples} checks)"
                         if self.block else
                         f"answered all run ({watch.samples} checks)")
                        + extra + how))
            cpu = self.app_cpu.get(app) or []
            mem = self.app_mem.get(app) or []
            out.append(("INFO", f"{app}: CPU and memory",
                        (f"CPU mean {sum(cpu) / len(cpu):.1f}% of one core, "
                         f"peak {max(cpu):.1f}%" if cpu else "CPU not read")
                        + (f"; memory {mem[0][1]:.0f} MB at start, "
                           f"{mem[-1][1]:.0f} MB at the end" if mem else "")))
        out.append(("INFO", "GPU (3D engine, all programs)",
                    (f"mean {sum(self.gpu) / len(self.gpu):.1f}%, peak "
                     f"{max(self.gpu):.1f}% over {len(self.gpu)} readings")
                    if self.gpu else "not readable on this PC"))
        return out

    def timekeeping(self):
        """{program: what its log says Windows agreed to}, from the
        'Windows timekeeping:' line each program writes at start."""
        out = {}
        if not ltcwin.WINDOWS:
            return out
        for name, tag in (("engine", "ltcplay:"), ("flamesafe", "flamesafe:"),
                          ("deck", "ltcplay-deck:")):
            if name == "deck" and not self.deck:
                continue
            got = ""
            try:
                with open(os.path.join(self.dir, f"{name}.log"),
                          encoding="utf-8", errors="replace") as fh:
                    for ln in fh:
                        if ln.startswith(tag) and \
                                "Windows timekeeping:" in ln:
                            got = ln.split("Windows timekeeping:", 1)[1].strip()
            except OSError:
                pass
            out[name] = got
        return out

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
        if not self.outside_timing:
            out.append(("NOT TESTED", "Pixel frames and Art-Net timecode",
                        self.measured))
        elif px.n < 10:
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
                        f"The first 5 s of each show are not timed."))
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
        done = sum(1 for st, _r in self.slots.values() if st == "DONE")
        early = [n for n, (st, r) in self.slots.items()
                 if st == "FAULT" and "run has not been pressed"
                 in str(r).lower()]
        failed = [(n, r) for n, (st, r) in sorted(self.slots.items())
                  if st == "FAULT" and n not in early]
        out.append(("PASS" if done and not failed else "FAIL",
                    "Scheduled shows (the scheduler starting each show)",
                    f"{done} show(s) played to the end, {len(failed)} failed "
                    f"to start (limit 0)" + (": " + "; ".join(
                        f"show {n}: {r}" for n, r in failed[:5])
                        if failed else "") + (
                        f"; {len(early)} came due before this test pressed "
                        f"Run, so did not start, as designed (not counted)"
                        if early else "") + f"; {self.show_starts} timecode "
                    f"run(s) seen (a silence over 1 s starts a new one)"))
        tc = self.tc
        ok = tc.n > 10 and tc.over_gap == 0 and \
            abs(tc.mean() - TC_PERIOD_MS) <= 3.0
        out.append(("NOT TESTED" if not self.outside_timing else
                    "PASS" if ok else "FAIL",
                    "Art-Net timecode (from the show audio, to MadMapper's "
                    "address)",
                    f"{tc.events} packets in {self.show_starts} show(s), "
                    f"mean {tc.mean():.2f} ms (target {TC_PERIOD_MS:.2f} "
                    f"+/- 3), worst {tc.worst_dev:.1f} ms off, longest gap "
                    f"inside a show {tc.longest:.1f} ms ({tc.over_gap} over "
                    f"{TC_GAP_MS:g} ms, limit 0)"))
        a = self.audio_worst
        snap = self.audio_snap
        what = ("FAKE stand-in device (CI)" if self.fake_audio
                else (snap.get("via") or self.audio_name or "none"))
        if not snap:
            out.append(("FAIL", "Show audio player",
                        f"the engine never reported its show audio "
                        f"(device {self.audio_name!r})"))
        else:
            bad = {k: a.get(k, 0) for k in ("underflows", "losses",
                                            "render_errors", "respawns")}
            ok = not any(bad.values()) and not snap.get("fault")
            out.append(("PASS" if ok else "FAIL", "Show audio player",
                        f"{what}: " + ", ".join(f"{k} {v}" for k, v in
                                                bad.items())
                        + f" (limit 0 each); clipped {a.get('clipped', 0)}"
                        + (f"; fault: {snap.get('fault')}"
                           if snap.get("fault") else "")
                        + (". AUDIO WAS FAKE: this proves the player, not "
                           "an interface." if self.fake_audio else "")))
        out += self.app_items()
        out.append(self.heat_item())
        out.append(self.disk_item())
        ups = sum(1 for _t, w, _a in self.beyond_cmds if w == "unblank")
        downs = sum(1 for _t, w, _a in self.beyond_cmds if w == "blank")
        ok = (self.show_starts == 0 or ups > 0) and \
            self.beyond_lit_outside == 0
        if self.mode != "fallback" or (self.bey_stats or {}).get(
                "blank_mode") == "timecode":
            # BEYOND itself has them ("BEYOND answering"), or the lasers
            # are blanked by timecode, so OSC is not used.
            ok = True
        out.append(("PASS" if ok else "FAIL",
                    "Lasers (BEYOND commands, to this PC only)",
                    f"{ups} unblank and {downs} blank command packets; "
                    f"{self.beyond_lit_outside} unblank(s) with no show "
                    f"running (limit 0)"))
        out.append(("INFO" if self.mode != "fallback" else
                    "PASS" if self.mm_packets and self.show_starts else
                    "FAIL" if self.show_starts else "NOT TESTED",
                    "Video (MadMapper commands, to this PC only)",
                    f"{self.mm_packets} OSC packets: " + ", ".join(
                        f"{k} x{v}" for k, v in
                        sorted(self.mm_addresses.items())[:8])))
        lg = self.link_gaps
        out.append(("PASS" if lg.n > 10 and lg.over_gap == 0 else "FAIL",
                    "Flame link frames (the engine's flame link to "
                    "flamesafe, timed through a relay)",
                    # The numbers first, so a cut-short annotation still
                    # has them; the timecode's and pixels' worst gaps beside
                    # them tell a flame link stall from a whole-process
                    # pause (compare the times).
                    f"longest gap {lg.longest:.1f} ms at "
                    f"{now_text(lg.longest_at)}, {lg.over_gap} over "
                    f"{LINK_GAP_MS:g} ms (limit 0); timecode longest gap "
                    f"{self.tc.longest:.1f} ms at "
                    f"{now_text(self.tc.longest_at)}; pixels longest gap "
                    f"{self.pixels.longest:.1f} ms at "
                    f"{now_text(self.pixels.longest_at)}; {lg.events} frames "
                    f"({self.flame_nonzero} carrying flame cues), mean "
                    f"{lg.mean():.1f} ms"))
        ok = self.fs_fresh_seen and self.fs_stale_events == 0
        out.append(("PASS" if ok else "FAIL", "flamesafe never saw the link "
                    "go stale", f"{self.fs_status_frames} status frames; link "
                    f"went stale {self.fs_stale_events} time(s) after it was "
                    f"first fresh; lock alarms {self.fs_lock_alarms}"))
        sc = self.sacn
        j = self.judge
        viol = j.violations if j else []
        ok = sc.n > 10 and not viol and sc.over_gap == 0
        out.append(("PASS" if ok else "FAIL", "flamesafe output (sACN, sent "
                    "to this PC only), judged per armed group",
                    f"{len(viol)} violation(s) (limit 0)"
                    + (": " + "; ".join(f"{now_text(a)} {w}"
                                        for a, w in viol[:6]) if viol else "")
                    + "; fire packets per group: " + ", ".join(
                        f"{n} {c}" for n, c in
                        (j.fire_frames.items() if j else []))
                    + f"; {sc.events} packets, mean {sc.mean():.1f} ms "
                    f"(target {1000 / self.tick_hz:.0f}), longest gap "
                    f"{sc.longest:.1f} ms (limit {SACN_LATE_MS:g}, "
                    f"flamesafe's overrun_ms), {self.sacn_nonzero} not all "
                    f"zero. A group's channels may carry values only while "
                    f"flamesafe reports it armed, and its fire channels only "
                    f"in a show, never in a Hold, never after an Abort until "
                    f"it is armed again."))
        st = self.engine("/api/conductor")
        las = (st.get("lasers") or {}) if isinstance(st, dict) else {}
        if las.get("timecode"):
            self.bey_stats = las
        las = self.bey_stats or {}
        tc = las.get("timecode") or {}
        heard = self.mode == "fallback"
        out.append((("FAIL" if self.bey_bad else "PASS") if heard else
                    "INFO",
                    "BEYOND's timecode blanking (black zone hour 23 while "
                    "the lasers must be dark)",
                    (f"{len(self.bey_bad)} show-zone moment(s) while the "
                     f"lasers had to be dark (limit 0)"
                     + (": " + "; ".join(f"{now_text(a)} {w}" for a, w in
                                         self.bey_bad[:6])
                        if self.bey_bad else "")
                     + f"; heard here: {self.bey_black / 30:.0f} s in the "
                     f"black zone, {self.bey_show / 30:.0f} s on show "
                     f"timecode; " if heard else
                     "BEYOND holds port 6454, so its stream is not heard "
                     "here; ")
                    + (f"the engine sent {tc.get('black_frames', 0) / 30:.0f}"
                       f" s of black zone and {tc.get('show_frames', 0) / 30:.0f}"
                       f" s of show timecode to {tc.get('ip')}, "
                       f"{tc.get('send_errors', 0)} send error(s); mode "
                       f"{las.get('blank_mode', '?')}" if tc else
                       "the engine reported no BEYOND timecode counts")))
        ex = self.ex
        if ex is not None:
            c = ex.counts
            ok = c["arms"] > 0 and not ex.failures
            out.append(("PASS" if ok else "FAIL",
                        "Flame arming exerciser (signed in on the rack "
                        "screen, arming through the Stream Deck program, "
                        "Hold, Resume, Abort and Reset)",
                        f"{len(ex.failures)} failure(s) (limit 0)"
                        + (": " + "; ".join(f"{now_text(a)} {w}" for a, w in
                                            ex.failures[:6])
                           if ex.failures else "")
                        + f"; {c['arms']} of {c['arm_tries']} arms took ("
                        + ", ".join(f"{n} {k}" for n, k in
                                    ex.arms_by_group.items())
                        + f"); {c['cycles']} arm cycle(s) after an Abort "
                        f"(Disarm, then hold again); {c['holds']} Holds, "
                        f"{c['resumes']} Resumes, "
                        f"{c['aborts']} Aborts, {c['resets']} Resets; "
                        f"{self.hold_continued} Hold silence(s) in the "
                        f"timecode read as the same show"
                        + ("; the Stream Deck program ran with a virtual "
                           "deck" if self.virtual_deck else "")))
        out.append(("PASS" if not self.journal_real else "FAIL",
                    "Real faults in the engine's night journal",
                    f"{len(self.journal_real)} (limit 0)" + (": " + " | ".join(
                        f"{at} {t}" for at, t in self.journal_real[:8])
                        if self.journal_real else "")))
        out.append(("INFO", "Nothing attached, expected (not faults)",
                    "; ".join(f"{w}: {n} line(s)" for w, n in
                              self.journal_expected.items()) or "none"))
        keep = self.timekeeping()
        bad = [f"{n}: {v}" for n, v in keep.items()
               if not v or "NOT" in v or "could not" in v]
        out.append((("FAIL" if bad and ltcwin.WINDOWS else "PASS"),
                    "Windows timekeeping (no power throttling, 1 ms timer, "
                    "engine and flamesafe Above normal)",
                    "; ".join(f"{n}: {v or 'no line in its log'}"
                              for n, v in keep.items()) or "not Windows"))
        out.append(("INFO", "Late flame frames, as the flame link saw them",
                    (f"{len(self.late_lines)} line(s); " + "; ".join(
                        f"{a} {t}" for a, t in self.late_lines[:6]))
                    if self.late_lines else "none journaled (45 ms or more "
                    "after the frame before)"))
        out.append(("INFO", "Before Run was pressed (not faults)",
                    f"{self.before_run} fault line(s) written while nothing "
                    f"had pressed Run yet (a show that comes due then does "
                    f"not start, as designed)"))
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
        text = "\r\n".join(self.report_lines()) + "\r\n"
        for p in self.report_paths:
            try:
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(text)
            except OSError:
                pass
        if self.on_report:
            self.on_report()

    def report_lines(self):
        items = self.items()
        verdict = ("PASSED" if all(v != "FAIL" for v, _t, _d in items)
                   else "FAILED")
        state = ("finished" if self.finished else
                 "STILL RUNNING (this file is rewritten every minute)")
        lines = [
            ("LTC Player bench soak test" if not self.block else
             f"Block {self.block[0]} of {self.block[1]}"),
            ("BENCH ONLY: no lasers, flames or lights connected."
             if self.mode == "fallback" else
             "BENCH ONLY: this PC is isolated; flamesafe's output goes to "
             "this PC only."),
            "",
            f"Result: {verdict if self.finished else verdict + ' so far'}",
            f"Run: {state}. Started {now_text(self.started)}, "
            f"{self.hours():.2f} of {self.seconds / 3600:g} hour(s).",
            f"Program: {ltcwin.version_line('LTC Player')}",
            f"Stream Deck: {'not plugged in, deck program running with a virtual deck' if self.virtual_deck else 'Mini plugged in, deck program running'}",
            "Show audio: " + ("FAKE stand-in device (CI run)"
                              if self.fake_audio else
                              (self.audio_name or "NO interface found")),
            f"Bench schedule: a {SHOW_S} s show every {SHOW_EVERY_MIN} "
            f"minutes from 02:00 to midnight ({getattr(self, 'tz_name', '')}"
            f"), started by the scheduler itself",
            (f"WARNING: ran inside another app's container "
             f"({self.container}); its files went to that app's folder. "
             f"Start the soak from the Start menu." if getattr(
                 self, "container", "") else "Container: none (started as "
                                             "itself)"),
            f"Soak mode: {self.mode}"
            + (" (MadMapper and BEYOND running on this PC, getting LTC "
               "Player's real commands: MadMapper's OSC on 127.0.0.1:8000, "
               "BEYOND's on 127.0.0.2:8100, the Art-Net timecode to both; "
               "BEYOND's laser output may be enabled, this PC is isolated)"
               if self.mode != "fallback"
               else " (MadMapper's and BEYOND's commands counted by this "
                    "program; neither program ran)"),
            f"How measured: {self.measured or 'not started'}",
            "Show mode: fire_ice, the engine and the deck started exactly as "
            "the installed LTC Player starts them in that mode",
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
        lines += [f"  {now_text(t)}  {s}"
                  for t, s in NOTES[self.notes_from:][-200:]]
        lines += ["", "Not exercised by this test: announcements, real LTC "
                  "input, seeks, and anything actually lighting up (every "
                  "output goes to this PC only).", "",
                  f"Every 5 s sample: {os.path.join(self.dir, 'samples.csv')}",
                  f"Program logs: {self.dir}"]
        return lines


def refuse_held_ports(need):
    """Raise a plain-sentence RuntimeError, naming the process, when any
    (ip, port, what) this program must listen on is already held."""
    for ip, port, what in need:
        if soak_apps.port_held(port, ip):
            who = soak_apps.port_owners(port)
            apps = [soak_apps.which_app(w) for w in who]
            hint = (" MadMapper or BEYOND is running, so run the soak in "
                    "all programs mode (leave out --mode fallback)."
                    if any(apps) else " Close it and start the soak again.")
            raise RuntimeError(
                f"Port {port} ({what}) is already held by "
                f"{', '.join(who) or 'another program'}, so the soak cannot "
                f"listen there. Nothing was started.{hint}")


def resolve_mode(want="auto", names=None):
    """(mode, {app: install path}, sentence): 'all programs' when MadMapper
    and BEYOND are both installed (or running) and `want` allows it, else
    'fallback'."""
    if want == "fallback":
        return "fallback", {}, "fallback mode: asked for (--mode fallback)"
    names = soak_apps.tasklist_names() if names is None else names
    found, missing = {}, []
    for app in ("MadMapper", "BEYOND"):
        path = soak_apps.find_app(app)
        on_port = any(soak_apps.is_app(app, o) for o in
                      soak_apps.port_owners(soak_apps.APPS[app]["port"]))
        if path or soak_apps.running(app, names) or on_port:
            found[app] = path
        else:
            missing.append(app)
    if missing:
        if want == "all":
            raise RuntimeError(f"--mode all, but {', '.join(missing)} "
                               f"is not installed or running")
        return ("fallback", {},
                f"fallback mode: {', '.join(missing)} not installed, so its "
                f"commands go to this program's own counters")
    return ("all programs", found,
            "all programs mode: MadMapper and BEYOND run on this PC and get "
            "LTC Player's real commands")


def _enter_pressed():
    """True once when Enter was pressed in this window (Windows), without
    waiting for it."""
    try:
        import msvcrt
    except ImportError:
        return False
    hit = False
    while msvcrt.kbhit():
        if msvcrt.getwch() in ("\r", "\n"):
            hit = True
    return hit


def wait_for_apps(before=None, block=(1, 1), poll_s=2.0, probe=None,
                  sleep=time.sleep, clock=time.time):
    """Wait, however long it takes, until MadMapper and BEYOND both answer
    (soak_apps.readiness), saying what to click once and each change in
    what is still awaited. `before`: {app: PIDs} from the block before,
    which each must have been started again since (Enter skips that, for a
    full license). Returns {app: PIDs} for the next block's check."""
    probe = probe or (lambda: (soak_apps.app_pids(), soak_apps.hung_names()))
    i, n = block
    note(f"Block {i} of {n}: start BEYOND and MadMapper"
         + (" again (quit both first)" if before else "")
         + ". The block starts by itself once both answer. What to click:")
    for c in soak_apps.CLICKS:
        note("  " + c)
    if before:
        note("  (With full licenses there is nothing to restart: press Enter "
             "to go on with the copies already running.)")
    said = {}
    t0 = clock()
    while True:
        pids, hung = probe()
        if before and _enter_pressed():
            note("Enter pressed: the copies already running are used")
            before = None
        waiting = {}
        for app in ("BEYOND", "MadMapper"):
            a = soak_apps.APPS[app]
            held = soak_apps.port_held(a["port"], a["ip"])
            why = soak_apps.readiness(app, pids, hung, held,
                                      (before or {}).get(app))
            if why:
                waiting[app] = why
            if said.get(app) != why:
                note(f"waiting for {app}: {why}" if why else
                     f"{app} answers")
                said[app] = why
        if not waiting:
            note(f"both answer after {(clock() - t0) / 60:.1f} min of "
                 f"waiting; block {i} of {n} starts")
            return {app: soak_apps.pids_of(app, pids)
                    for app in ("BEYOND", "MadMapper")}
        sleep(poll_s)


class Blocks:
    """All programs mode: the run in blocks of 1 h 50 min (soak_apps.
    block_plan), each a whole soak of its own, started only once BEYOND and
    MadMapper answer, with one combined report."""

    def __init__(self, seconds, audio_device=None, fake_audio=False):
        import supervisor as sup
        stamp = time.strftime("%Y-%m-%d_%H%M")
        self.seconds = seconds
        self.audio_device = audio_device
        self.fake_audio = fake_audio
        self.plan = soak_apps.block_plan(seconds)
        self.dir = os.path.join(sup.appdata_dir(), "soak", stamp)
        os.makedirs(self.dir, exist_ok=True)
        name = f"LTC Player soak report {stamp}.txt"
        self.report_paths = [os.path.join(self.dir, name)]
        desk = desktop_dir()
        if desk:
            self.report_paths.append(os.path.join(desk, name))
        self.started = time.time()
        self.blocks = []        # Soak, one per block begun
        self.waited = {}        # block number -> minutes waited for the apps
        self.finished = False
        self.stopped = ""

    def run(self, wait=wait_for_apps):
        n = len(self.plan)
        note(f"all programs mode: {n} block(s) of "
             f"{self.plan[0] / 60:.0f} minutes, clear of the demos' 2 hour "
             f"limit; folder {self.dir}")
        before = None
        try:
            for i, secs in enumerate(self.plan, 1):
                if i > 1:
                    note(f"Block {i - 1} of {n} is done and everything "
                         f"stopped cleanly.")
                t = time.time()
                self.write()
                pids = wait(before, (i, n))
                self.waited[i] = (time.time() - t) / 60
                s = Soak(secs, self.audio_device,
                         folder=os.path.join(self.dir, f"block {i}"),
                         block=(i, n))
                s.fake_audio = self.fake_audio
                s.want_mode = "all"
                s.on_report = self.write
                self.blocks.append(s)
                s.run()
                if s.interrupted:
                    self.stopped = f"stopped by Ctrl-C in block {i}"
                    break
                before = pids
        except KeyboardInterrupt:
            self.stopped = "stopped by Ctrl-C while waiting for the programs"
            note(self.stopped)
        self.finished = True
        self.write()
        return self.passed

    @property
    def passed(self):
        return bool(self.blocks) and not self.stopped and \
            len(self.blocks) == len(self.plan) and \
            all(v != "FAIL" for s in self.blocks for v, _t, _d in s.items())

    def write_report(self):
        self.write()

    def stop_all(self):
        if self.blocks and not self.blocks[-1].finished:
            self.blocks[-1].stop_all()

    def write(self):
        n = len(self.plan)
        per = []
        for s in self.blocks:
            items = s.items()
            bad = [t for v, t, _d in items if v == "FAIL"]
            per.append((s, items, bad))
        failed = any(bad for _s, _i, bad in per)
        verdict = ("FAILED" if failed or (self.finished and not self.passed)
                   else "PASSED")
        lines = [
            "LTC Player bench soak test, all programs, in blocks",
            "BENCH ONLY: this PC is isolated; flamesafe's output goes to "
            "this PC only.",
            "",
            f"Result: {verdict if self.finished else verdict + ' so far'}"
            + (f" ({self.stopped})" if self.stopped else ""),
            f"Plan: {n} block(s) of {self.plan[0] / 60:.0f} minutes for the "
            f"{self.seconds / 3600:g} hour(s) asked for. Each block ends at "
            f"1 h 50 min at most, clear of the demos' 2 hour limit. Between "
            f"blocks everything stops cleanly, BEYOND and MadMapper are "
            f"started again by hand, and the next block starts once both "
            f"answer.",
            f"Started {now_text(self.started)}. Program: "
            f"{ltcwin.version_line('LTC Player')}",
            "BEYOND's response to commands is checked only as far as "
            "possible: it sends nothing back, so running, its window "
            "responding and holding its OSC port (127.0.0.2:8100) is all "
            "that can be known. MadMapper is checked by the engine's "
            "heartbeat watchdog as well.",
            "",
            "Blocks:",
        ]
        for k, (s, _items, bad) in enumerate(per, 1):
            state = ("running" if not s.finished else
                     "FAILED" if bad else "PASSED")
            lines.append(
                f"  Block {k}: {state}, {s.hours():.2f} h from "
                f"{now_text(s.started)}; waited "
                f"{self.waited.get(k, 0):.1f} min for BEYOND and MadMapper "
                f"first" + (": FAIL " + "; ".join(bad) if bad else ""))
        for k in range(len(per) + 1, n + 1):
            lines.append(f"  Block {k}: not started")
        for k, (s, _items, _bad) in enumerate(per, 1):
            lines += ["", "=" * 20 + f" Block {k} of {n} " + "=" * 20]
            lines += s.report_lines()
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


def _self_check():
    yield "the soak test loads"
    for line in soak_apps.self_test():
        yield line
    for line in soak_exercise.self_test():
        yield line


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ltcwin.prepare_stdio()
    rc = ltcwin.common_flags("ltcplay-soak", argv, _self_check)
    if rc is not None:
        return rc
    wait = "--no-wait" not in argv
    seconds = None
    device = None
    fake = False
    mode = "auto"
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
        elif a == "--fake-audio":
            fake = True
        elif a == "--mode":
            mode = argv[i + 1]
            if mode not in ("auto", "all", "fallback"):
                print("--mode is auto, all or fallback")
                return 2
            i += 1
        elif a != "--no-wait":
            print(f"Unknown option {a}. Options: --hours H, --minutes M, "
                  f"--audio-device NAME, --no-wait, --fake-audio, "
                  f"--mode auto|all|fallback")
            return 2
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
    ok = False
    soak = None
    failed = ""
    try:
        resolved, _found, why = resolve_mode(mode)
        if resolved == "all programs":
            note(why)
            soak = Blocks(seconds, device, fake)
        else:
            soak = Soak(seconds, device)
            soak.fake_audio = fake
            soak.want_mode = mode
        ok = soak.run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        failed = f"{type(e).__name__}: {e}" if not isinstance(
            e, RuntimeError) else str(e)
        note(f"THE SOAK TEST ITSELF FAILED: {failed}")
        if soak is not None:
            soak.finished = True
            soak.ended = time.time()
            try:
                soak.stop_all()
            except Exception:
                pass
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
    if failed:
        print("")
        print("THE SOAK TEST ITSELF FAILED: " + failed)
        print("LTC Player was left as it was found"
              + (" (started again)." if was_running else "."))
    print("")
    print("Result: " + ("PASSED" if ok else "FAILED"))
    print("The report is here:")
    for p in (soak.report_paths if soak is not None else []):
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
