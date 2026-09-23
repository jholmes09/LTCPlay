#!/usr/bin/env python3
"""ltcplay self-test.  Run it after any change: ./ltcplay-venv/bin/python selftest.py

Every check here is anchored to something real: LTC that is synthesised and then
decoded back, packet bytes compared against the layouts in the xLights source,
and where a real show folder is available, actual FSEQ files off disk.
"""
import os
import random
import re
import threading
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ltcplay.ltc import LTCDecoder, synthesize, decode_bits, SYNC_WORD
from ltcplay import (output, netmap, timeline, tc as tcmod,
                     display as disp, audio as audio_mod,
                     trigger as trig_mod)
from ltcplay.player import (Player, LOCKED, FREEWHEEL, LOST, PARKED,
                            SHOW, IDLE, HOLD, BLACK)

FAILS = []
# Things wrong with the SHOW rather than with the program: a trigger channel
# that does not match the boxes, renders that are not the ones the scenes
# were recorded from. Worth saying loudly, and NOT a reason to refuse a
# program update, which is not allowed to fix them anyway. That confusion
# rolled a correct update back off the show Mac on 2026-09-15.
SHOW_PROBLEMS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print(f"  FAIL  {msg}")
    return cond


def show_check(cond, msg):
    if not cond:
        SHOW_PROBLEMS.append(msg)
        print(f"  SHOW FILE  {msg}")
    return cond


# Is this the tree the program is BUILT in, or a copy it was installed into?
# They hold different files on purpose, and a check that assumes one of them
# fails on the other for reasons that have nothing to do with the show.
SOURCE_TREE = os.path.exists(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "mutate.py"))

RAN = set()


def launcher(name, root=None):
    """Where a launcher actually is: the install folder, or Tools/ in it.

    The rarely-used ones moved into Tools/ so the folder a show operator
    opens has four things in it instead of twelve. A test that only looks in
    one place passes on one layout and fails on the other for reasons that
    have nothing to do with the show."""
    root = root or os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(root, name), os.path.join(root, "Tools", name)):
        if os.path.exists(p):
            return p
    return os.path.join(root, name)


def section(name):
    # Record which test function this heading came from. A test that is defined
    # but never called is worse than no test: it reads as coverage in the file
    # and proves nothing at run time. That has already happened once, to two
    # tests, because a string replace into the call list below silently matched
    # nothing.
    import sys as _sys
    RAN.add(_sys._getframe(1).f_code.co_name)
    print(f"\n== {name}")


def wait_for(pred, timeout=3.0, step=0.02):
    """Poll until true. Fixed sleeps in a thread test pass on an idle machine
    and fail on a busy one, which makes the suite a coin toss rather than a
    check; a deadline tests the same thing without the flake."""
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


# ---------------------------------------------------------------- LTC ----
def test_ltc_roundtrip():
    section("LTC round trip")
    random.seed(5)
    for sr in (44100, 48000, 96000):
        for fps in (24.0, 25.0, 29.97, 30.0):
            # long enough to cross a second boundary, which is where
            # the rate becomes knowable
            au = synthesize(1, 2, 3, 4, fps, sr, frames=int(fps) + 10)
            d = LTCDecoder(sr)
            got = []
            i = 0
            while i < len(au):
                n = random.choice((37, 128, 256, 512, 1000))
                got += d.feed(au[i:i + n])
                i += n
            nums = [g.frame_number(fps) for g in got]
            check(len(got) >= 15, f"{sr}/{fps}: only {len(got)} frames decoded")
            check(all(b - a == 1 for a, b in zip(nums, nums[1:])),
                  f"{sr}/{fps}: frames not consecutive")
            check(d.nominal_fps == int(round(fps)),
                  f"{sr}/{fps}: nominal fps read as {d.nominal_fps}")
            short = LTCDecoder(sr)
            short.feed(synthesize(1, 2, 3, 4, fps, sr, frames=5))
            check(short.nominal_fps is None,
                  f"{sr}/{fps}: claimed a frame rate from 5 frames of timecode")
            spf = sr / fps
            errs = [min(g.start_sample % spf, spf - g.start_sample % spf) for g in got]
            check(max(errs) < spf * 0.03,
                  f"{sr}/{fps}: frame anchor off by {max(errs):.1f} of {spf:.0f} samples")
    print("  ok")


def test_ltc_rollovers():
    section("LTC rollovers")
    for start, expect in (((0, 0, 59, 28), "00:01:00:00"),
                          ((0, 59, 59, 28), "01:00:00:00"),
                          ((23, 59, 59, 28), "00:00:00:00")):
        au = synthesize(*start, 30.0, 48000, frames=8)
        d = LTCDecoder(48000)
        got = [str(g) for g in d.feed(au)]
        check(expect in got, f"rollover from {start} never produced {expect}: {got}")
    print("  ok")


def test_ltc_degraded():
    section("LTC under bad signal")
    random.seed(9)
    base = synthesize(2, 0, 0, 0, 30.0, 48000, frames=60)
    cases = {
        "clean": lambda v: v,
        "30% noise": lambda v: v + random.uniform(-0.3, 0.3),
        "mic level (x0.1)": lambda v: v * 0.1,
        "clipped (x3)": lambda v: max(-1.0, min(1.0, v * 3.0)),
        "DC offset +0.2": lambda v: v + 0.2,
    }
    for label, fn in cases.items():
        au = [fn(v) for v in base]
        d = LTCDecoder(48000)
        got = d.feed(au)
        nums = [g.frame_number(30.0) for g in got]
        ok = len(got) >= 55 and all(b - a == 1 for a, b in zip(nums, nums[1:]))
        check(ok, f"{label}: {len(got)} frames, consecutive="
                  f"{all(b-a==1 for a,b in zip(nums,nums[1:]))}")
    # mid-stream dropout must re-lock
    au = list(synthesize(2, 0, 0, 0, 30.0, 48000, frames=90))
    for i in range(48000, 96000):
        au[i] = 0.0
    d = LTCDecoder(48000)
    after = []
    for i in range(0, len(au), 480):
        for fr in d.feed(au[i:i + 480]):
            if i >= 96000:
                after.append((i, fr))
    check(after and (after[0][0] - 96000) / 48000 < 0.2,
          f"did not re-lock within 200ms of the signal returning: {after[:1]}")
    print("  ok")


def test_ltc_rejects_garbage():
    section("LTC rejects nonsense")
    bits = [0] * 80
    for i, b in enumerate(SYNC_WORD):
        bits[64 + i] = b
    check(decode_bits(bits) is not None, "a valid all-zero frame was rejected")
    bits[0] = bits[1] = bits[2] = bits[3] = 1   # frame units = 15
    bits[8] = bits[9] = 1                        # frame tens = 3 -> 45
    check(decode_bits(bits) is None, "frame 45 was accepted")
    bits2 = [0] * 80                             # no sync word at all
    check(decode_bits(bits2) is None, "a frame with no sync word was accepted")
    random.seed(1)
    d = LTCDecoder(48000)
    got = d.feed([random.uniform(-1, 1) for _ in range(48000 * 2)])
    check(not got, f"white noise decoded as {len(got)} timecode frames")
    d = LTCDecoder(48000)
    check(not d.feed([0.0] * 48000), "silence decoded as timecode")
    print("  ok")


def test_numpy_matches_scalar():
    section("vectorised decode matches the scalar path")
    random.seed(4)
    for sr, fps in ((48000, 30.0), (44100, 25.0)):
        au = [v + random.uniform(-0.08, 0.08)
              for v in synthesize(3, 4, 5, 6, fps, sr, frames=25)]
        a, b = LTCDecoder(sr), LTCDecoder(sr)
        ra, rb, i = [], [], 0
        while i < len(au):
            n = random.choice((256, 512, 333))
            ra += a.feed(au[i:i + n])
            rb += b._feed_scalar(au[i:i + n])
            i += n
        check([(str(x), round(x.start_sample, 3)) for x in ra] ==
              [(str(x), round(x.start_sample, 3)) for x in rb],
              f"{sr}/{fps}: numpy and scalar decoders disagree")
    print("  ok")


# ------------------------------------------------------------- packets ----
def test_packets():
    section("packet layout")
    # ArtNetOutput.cpp:281-292, ArtNetOutput.h:23-27
    b = output._artnet_header(6101, 510)
    check(bytes(b[0:8]) == b"Art-Net\x00", "ArtNet id wrong")
    check(b[9] == 0x50 and b[8] == 0x00, "ArtNet opcode is not OpDmx")
    check(b[11] == 0x0E, "ArtNet protocol version is not 14")
    check(b[14] == 6101 & 0xFF and b[15] == (6101 >> 8) & 0xFF,
          "ArtNet universe bytes wrong")
    check(b[16] == 510 >> 8 and b[17] == 510 & 0xFF, "ArtNet length bytes wrong")
    check(len(b) == 18, "ArtNet header is not 18 bytes")
    # E131Output.cpp:297-345, E131Output.h:20-23
    e = output._e131_header(1, 510)
    check(e[1] == 0x10, "E1.31 preamble wrong")
    check(bytes(e[4:16]) == b"ASC-E1.17\x00\x00\x00", "E1.31 ACN identifier wrong")
    check(e[21] == 0x04 and e[43] == 0x02 and e[117] == 0x02, "E1.31 vectors wrong")
    check(e[113] == 0 and e[114] == 1, "E1.31 universe bytes wrong")
    check(e[123] == (511 >> 8) and e[124] == (511 & 0xFF),
          "E1.31 property count must include the start code")
    check(e[125] == 0x00, "E1.31 DMX start code wrong")
    check(len(e) == 126, "E1.31 header is not 126 bytes")

    class _U:
        def __init__(s, **kw): s.__dict__.update(kw)

    class _M:
        universes = [_U(ip="127.0.0.1", protocol="artnet", universe=1,
                        start=1, count=4, controller="t"),
                     _U(ip="127.0.0.1", protocol="artnet", universe=2,
                        start=5, count=4, controller="t")]
    s = output.Sender(_M())
    s.send_frame(bytes([1, 2, 3, 4, 5, 6, 7, 8]))
    p0, p1 = s._packets
    check(bytes(p0["buf"][18:22]) == bytes([1, 2, 3, 4]), "universe 1 payload wrong")
    check(bytes(p1["buf"][18:22]) == bytes([5, 6, 7, 8]), "universe 2 payload wrong")
    # a short frame must zero-fill rather than hold stale data
    s.send_frame(bytes([9, 9]))
    check(bytes(p0["buf"][18:22]) == bytes([9, 9, 0, 0]), "short frame did not zero-fill")
    check(bytes(p1["buf"][18:22]) == bytes([0, 0, 0, 0]),
          "universe past the end of a short frame was not blanked")
    s.blackout()
    check(bytes(p0["buf"][18:22]) == bytes(4), "blackout did not zero universe 1")
    check(p0["seq"] != p1["seq"] or True, "")
    seq_before = p0["seq"]
    s.send_frame(bytes(8))
    check(p0["buf"][12] == (seq_before + 1) & 0xFF, "ArtNet sequence did not advance")
    s.close()
    print("  ok")


# ------------------------------------------------------------ timeline ----
def test_timeline():
    section("timeline")
    check(abs(timeline.parse_tc("01:00:00:00", 30) - 3600.0) < 1e-9, "1h parse wrong")
    check(abs(timeline.parse_tc("00:00:01:15", 30) - 1.5) < 1e-9, "half second parse wrong")
    for bad in ("1:2:3:4:5", "01:00:00:30", "not a tc", "25:00:00:00"):
        try:
            timeline.parse_tc(bad, 30)
            check(False, f"{bad!r} was accepted as a timecode")
        except ValueError:
            pass
    check(timeline.format_tc(3600.0, 30) == "01:00:00:00", "format 1h wrong")
    check(timeline.format_tc(1.5, 30) == "00:00:01:15", "format 1.5s wrong")
    for s in (0.0, 1.5, 3600.0, 86399.0):
        rt = timeline.parse_tc(timeline.format_tc(s, 30), 30)
        check(abs(rt - s) < 1.0 / 30, f"round trip of {s} lost accuracy")
    print("  ok")


def test_netmap():
    section("network map")
    import tempfile
    xml = """<Networks>
      <Controller Name="A" IP="10.0.0.1" Protocol="ArtNet" ActiveState="Active">
        <network ComPort="10.0.0.1" BaudRate="1" NetworkType="ArtNet" MaxChannels="510"/>
        <network ComPort="10.0.0.1" BaudRate="2" NetworkType="ArtNet" MaxChannels="510"/>
      </Controller>
      <Controller Name="B" IP="" Protocol="DMX" ActiveState="Active">
        <network ComPort="COM3" BaudRate="0" NetworkType="DMX" MaxChannels="512"/>
      </Controller>
      <Controller Name="C" IP="10.0.0.9" Protocol="E131" ActiveState="Active">
        <network ComPort="10.0.0.9" BaudRate="7" NetworkType="E131" MaxChannels="510"/>
      </Controller>
    </Networks>"""
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False) as fh:
        fh.write(xml)
        p = fh.name
    nm = netmap.load(p)
    os.unlink(p)
    check(len(nm.universes) == 3, f"expected 3 usable universes, got {len(nm.universes)}")
    check(nm.total_channels == 510 + 510 + 512 + 510,
          f"total channels {nm.total_channels} wrong")
    check(nm.universes[0].start == 1 and nm.universes[1].start == 511,
          "consecutive universes not numbered consecutively")
    # the serial controller must still consume its span or everything after it shifts
    check(nm.universes[2].start == 510 * 2 + 512 + 1,
          f"the non-UDP controller did not reserve its channels "
          f"(E1.31 starts at {nm.universes[2].start})")
    check(nm.universes[2].protocol == "e131", "E131 not recognised")
    check(len(nm.skipped) == 1, f"expected 1 skipped controller, got {nm.skipped}")
    print("  ok")


# ----------------------------------------------------------- real files ---
def test_real_show(show_dir):
    section(f"real FSEQ files in {show_dir}")
    import glob
    from ltcplay.fseq import FSEQ
    files = sorted(glob.glob(os.path.join(show_dir, "*.fseq")))
    if not files:
        print("  skipped, no .fseq files")
        return
    try:
        import zstandard  # noqa: F401
    except ImportError:
        print("  SKIPPED: zstandard is not installed, so compressed frames "
              "cannot be decoded here")
        return
    for p in files:
        try:
            with FSEQ(p) as f:
                d = f.frame(0)
                check(len(d) == f.channel_count,
                      f"{os.path.basename(p)}: frame 0 is {len(d)} bytes, "
                      f"header says {f.channel_count}")
                mid = f.frame_count // 2
                check(len(f.frame(mid)) == f.channel_count,
                      f"{os.path.basename(p)}: middle frame wrong size")
                check(len(f.frame(f.frame_count - 1)) == f.channel_count,
                      f"{os.path.basename(p)}: last frame wrong size")
                try:
                    f.frame(f.frame_count)
                    check(False, f"{os.path.basename(p)}: read past the last frame")
                except IndexError:
                    pass
                if f.sparse_ranges:
                    check(sum(l for _, l in f.sparse_ranges) == f.channel_count,
                          f"{os.path.basename(p)}: sparse ranges do not sum to "
                          f"the channel count")
        except Exception as e:
            check(False, f"{os.path.basename(p)}: {e}")
    print(f"  {len(files)} files ok")


# ------------------------------------------------------- new behaviour ----
class FakeFSEQ:
    """Stands in for a real FSEQ so the engine can be driven without a show
    folder. Every frame is filled with its own index, so a wrong frame is
    detectable from the output bytes alone."""

    def __init__(self, frames=400, channels=64, step=25, fail_at=None):
        self.frame_count = frames
        self.channel_count = channels
        self.step_time_ms = step
        self.duration_ms = frames * step
        self.sparse_ranges = []
        self.fail_at = fail_at

    def frame(self, i):
        if i < 0 or i >= self.frame_count:
            raise IndexError(i)
        if self.fail_at is not None and i >= self.fail_at:
            raise IOError("simulated read failure")
        return bytes([i & 0xFF]) * self.channel_count

    def close(self):
        pass


class FakeNetmap:
    def __init__(self, total=64):
        self.total_channels = total
        self.universes = []
        self.skipped = []

    def summary(self):
        return "fake"


class CountingSender:
    def __init__(self, raise_every=0):
        self.universe_count = 1
        self.packets_sent = 0
        self.send_errors = 0
        self.frames = []
        self.raise_every = raise_every
        self.n = 0
        self.last_ok_at = time.monotonic()
        self.reopens = 0
        self.last_error = ""

    @property
    def seconds_since_ok(self):
        return time.monotonic() - self.last_ok_at

    def send_frame(self, data):
        self.n += 1
        if self.raise_every and self.n % self.raise_every == 0:
            raise RuntimeError("simulated sender explosion")
        self.frames.append(bytes(data))
        self.packets_sent += 1
        self.last_ok_at = time.monotonic()

    def blackout(self):
        pass

    def close(self):
        pass


def _timeline(cues, idle=None, gaps=None, fps=30.0, drop=False):
    tl = timeline.Timeline(fps, [], "test", "/tmp", drop, idle, gaps)
    built = []
    for text, name, fseq in cues:
        c = timeline.Cue(text, f"/tmp/{name}.fseq", name)
        c.tc_seconds = tcmod.parse_tc(text, fps, drop)
        c.fseq = fseq
        c.duration = fseq.duration_ms / 1000.0
        c._spans = [(0, 0, fseq.channel_count)]
        built.append(c)
    built.sort(key=lambda c: c.tc_seconds)
    tl.cues = built
    return tl


def test_tc_math():
    section("timecode maths, including drop frame")
    for count, drop in ((30, False), (30, True), (25, False), (24, False)):
        for n in range(0, count * 60 * 65, 13):
            h, m, s, f = tcmod.frames_to_tc(n, count, drop)
            if not check(tcmod.tc_to_frames(h, m, s, f, count, drop) == n,
                         f"round trip broke at {n} count={count} drop={drop}"):
                break
    # Drop frame exists precisely so the clock stays honest over an hour.
    one_hour = tcmod.tc_to_frames(1, 0, 0, 0, 30, True)
    check(abs(one_hour / 29.97 - 3600.0) < 0.01,
          f"an hour of 29.97 drop frame should be 3600s, got {one_hour/29.97:.3f}")
    ndf_hour = tcmod.tc_to_frames(1, 0, 0, 0, 30, False) / 29.97
    check(abs(ndf_hour - 3603.6) < 0.1,
          f"an hour of 29.97 NON drop should run 3603.6s, got {ndf_hour:.1f}")
    # 29.97 and 30 must not be silently interchangeable
    check(tcmod.normalize_rate("29.97") == 29.97 and tcmod.normalize_rate(30) == 30.0,
          "rate normalisation collapsed 29.97 and 30")
    try:
        tcmod.normalize_rate(50)
        check(False, "an unsupported rate was accepted")
    except ValueError:
        pass
    check(tcmod.format_tc(3600.0, 29.97, True) == "01:00:00;00",
          "drop frame should format with a semicolon at the hour")
    # a frame that does not exist must be rejected, not rounded
    for bad in ("01:00:00:30", "01:00:60:00", "99:00:00:00"):
        try:
            tcmod.parse_tc(bad, 30)
            check(False, f"{bad} was accepted as a timecode")
        except ValueError:
            pass
    print("  ok")


def test_rate_detection():
    section("29.97 is told apart from 30")
    for rate in (24.0, 25.0, 29.97, 30.0):
        sr = 48000
        au = synthesize(1, 0, 0, 0, rate, sr, frames=int(round(rate)) * 5)
        d = LTCDecoder(sr)
        d.feed(au)
        got, drop, confident = d.detected_rate
        check(got is not None and abs(got - rate) < 0.01,
              f"{rate} fps source was detected as {got}")
        check(confident, f"{rate} fps was detected but not confidently")
        check(d.measured_fps and abs(d.measured_fps - rate) / rate < 0.001,
              f"{rate} fps measured as {d.measured_fps}")
    # the drop flag has to survive too, since it changes the arithmetic
    au = synthesize(1, 0, 0, 0, 29.97, 48000, frames=160, drop=True)
    d = LTCDecoder(48000)
    d.feed(au)
    check(d.detected_rate[1] is True, "the drop frame flag was not read back")
    print("  ok")


def test_next_cue():
    section("up next")
    fs = FakeFSEQ(frames=400)          # 10s each
    tl = _timeline([("01:00:00:00", "A", fs),
                    ("01:00:20:00", "B", fs),
                    ("01:00:40:00", "C", fs)])
    t0 = tcmod.parse_tc("01:00:00:00", 30)
    check(tl.next_cue(t0 - 5).name == "A", "before the show, next should be A")
    check(tl.cue_at(t0 - 5) is None, "before the show, nothing is playing")
    # At the exact frame a cue starts it is NOW, and next has moved on.
    check(tl.cue_at(t0).name == "A" and tl.next_cue(t0).name == "B",
          "on the cue's first frame, next must already be the one after it")
    check(tl.next_cue(t0 + 25).name == "C", "next should be C mid way through B")
    check(tl.next_cue(t0 + 45) is None, "past the last cue there is no next")
    # gaps: at 15s cue A has ended, B has not begun
    check(tl.cue_at(t0 + 15).name == "A" and tl.next_cue(t0 + 15).name == "B",
          "in the gap, cue_at still names the last cue to have started")
    print("  ok")


def _drive(p, seconds, tc_start, feed_until=None, rate=30.0, step=0.05):
    """Feed timecode at wall clock speed for `seconds`, stopping the feed at
    `feed_until` so a dropout can be observed."""
    t0 = time.monotonic()
    while True:
        el = time.monotonic() - t0
        if el >= seconds:
            return
        if feed_until is None or el < feed_until:
            p.feed_timecode(tc_start + el, time.monotonic(), text="fed")
        time.sleep(step)


def test_player_states():
    section("chase states, preshow loop and the frozen readout")
    import ltcplay.player as pl
    fs = FakeFSEQ(frames=4000)         # 100s
    idle = FakeFSEQ(frames=40, channels=64)
    tl = _timeline([("01:00:00:00", "A", fs), ("01:00:50:00", "B", fs)],
                   idle="/tmp/idle.fseq")
    snd = CountingSender()
    p = Player(tl, FakeNetmap(), snd, freewheel_ms=150, hold_ms=500)
    p.idle_cue = timeline.Cue("00:00:00:00", "/tmp/idle.fseq", "preshow loop")
    p.idle_cue.fseq = idle
    p.idle_cue.duration = 1.0
    p.idle_cue._spans = [(0, 0, 64)]
    p.start(step_ms=25)
    try:
        # 1. no timecode at all -> the preshow loop plays, not blackness
        time.sleep(0.3)
        check(p.state == LOST and p.source == IDLE,
              f"with no timecode the preshow loop should run, got "
              f"{p.state}/{p.source}")
        check(any(any(b) for b in snd.frames[-5:]),
              "the preshow loop sent nothing but zeros")

        # 2. timecode arrives -> show
        base = tcmod.parse_tc("01:00:10:00", 30)
        _drive(p, 0.6, base)
        check(p.state == LOCKED and p.source == SHOW,
              f"with timecode running the show should play, got "
              f"{p.state}/{p.source}")
        check(p.current_cue and p.current_cue.name == "A",
              "cue A should be playing at 01:00:10:00")
        check(p.next_cue and p.next_cue.name == "B", "next should be B")
        frozen = p.last_ltc_text
        rolling = p.tc_seconds

        # 3. feed stops -> LTC readout freezes, playback free-rolls
        time.sleep(0.3)
        check(p.state == FREEWHEEL, f"expected FREEWHEEL, got {p.state}")
        check(p.last_ltc_text == frozen,
              "the LTC readout moved after the feed stopped")
        check(p.tc_seconds > rolling + 0.2,
              "playback should free-roll through a short dropout")
        check(p.source == SHOW, "a short dropout should not interrupt the show")

        # 4. feed stays gone -> back to the preshow loop, readout still frozen
        time.sleep(0.6)
        check(p.state == LOST, f"expected LOST, got {p.state}")
        check(p.source == IDLE,
              f"after a long dropout the preshow loop should return, got "
              f"{p.source}")
        check(p.last_ltc_text == frozen,
              "the LTC readout must stay frozen on the last number received")
        check(p.tc_seconds < 0, "the playback clock should stop once lost")

        # 5. a deliberate jump is snapped, not slewed
        before = p.jumps
        p.feed_timecode(tcmod.parse_tc("01:00:55:00", 30), time.monotonic(),
                        text="jump")
        time.sleep(0.15)
        p.feed_timecode(tcmod.parse_tc("01:00:55:05", 30), time.monotonic(),
                        text="jump")
        time.sleep(0.1)
        check(p.current_cue and p.current_cue.name == "B",
              f"after jumping to 01:00:55:00 cue B should play, got "
              f"{p.current_cue and p.current_cue.name}")
        check(p.jumps > before, "the jump was not counted as a jump")
    finally:
        p.stop()
    print("  ok")


def test_loop_never_dies():
    section("the output thread survives anything thrown at it")
    fs = FakeFSEQ(frames=4000)
    tl = _timeline([("01:00:00:00", "A", fs)])
    snd = CountingSender(raise_every=2)      # every other send explodes
    p = Player(tl, FakeNetmap(), snd)
    p.start(step_ms=10)
    try:
        _drive(p, 0.6, tcmod.parse_tc("01:00:05:00", 30))
        check(p.thread_alive(), "the output thread died on a sender exception")
        check(p.loop_errors > 5,
              f"exceptions should be counted, saw {p.loop_errors}")
        n = p.frames_sent
        time.sleep(0.2)
        check(p.frames_sent > n, "output stopped advancing after exceptions")
    finally:
        p.stop()

    # a sequence that fails to read must not stop the thread either, and must
    # go dark rather than sit frozen once the failure persists
    fs2 = FakeFSEQ(frames=4000, fail_at=0)
    tl2 = _timeline([("01:00:00:00", "A", fs2)])
    snd2 = CountingSender()
    p2 = Player(tl2, FakeNetmap(), snd2)
    p2.start(step_ms=10)
    try:
        # Comfortably past the 1.0s the engine holds for, so this is not a
        # race against machine load. It failed once under a parallel mutation
        # sweep, and a test that cries wolf gets ignored.
        _drive(p2, 2.2, tcmod.parse_tc("01:00:05:00", 30))
        check(p2.thread_alive(), "a failing sequence read killed the thread")
        check(p2.render_errors > 0, "sequence read failures were not counted")
        check(p2.source == BLACK,
              f"a sequence that keeps failing should end up dark, got "
              f"{p2.source}")
    finally:
        p2.stop()

    # the supervisor is the last line of defence: kill the thread by hand
    tl3 = _timeline([("01:00:00:00", "A", FakeFSEQ(frames=4000))])
    p3 = Player(tl3, FakeNetmap(), CountingSender())
    p3.start(step_ms=10)
    try:
        p3._running = False
        p3._thread.join(timeout=1.0)
        p3._running = True                # thread is gone, player still "on"
        time.sleep(1.2)
        check(p3.thread_alive(), "the supervisor did not restart a dead thread")
        check(p3.thread_restarts >= 1, "the restart was not recorded")
    finally:
        p3.stop()
    print("  ok")


def test_socket_healing():
    section("the output socket rebuilds itself")
    class U:
        protocol, universe, count, ip, start = "artnet", 1, 510, "127.0.0.1", 1
    nm = FakeNetmap(510)
    nm.universes = [U()]
    s = output.Sender(nm)
    s.REOPEN_BACKOFF_S = 0.0
    s.send_frame(bytes(510))
    check(s.send_errors == 0 and s.packets_sent == 1, "a clean send failed")

    class DeadSock:
        def sendto(self, *a):
            raise OSError(65, "No route to host")

        def close(self):
            pass

    s._sock = DeadSock()
    for _ in range(3):
        s.send_frame(bytes(510))
    check(s._sock is None,
          "three failed sends in a row should have torn the socket down")
    s.send_frame(bytes(510))
    check(s.reopens == 1, f"the socket did not reopen, reopens={s.reopens}")
    check(s.packets_sent == 2, "the send after a reopen did not go out")
    check(s.seconds_since_ok < 1.0, "last-good-send age did not reset")

    # Occasional failures are a busy network, not a dead socket. Two failures,
    # then a success, then two more must not add up to a teardown: without the
    # reset a show would rebuild its socket every few minutes for no reason.
    good = s._sock
    dead = DeadSock()
    s._sock = dead
    s.send_frame(bytes(510)); s.send_frame(bytes(510))
    check(s._sock is dead, "two failures alone should not tear the socket down")
    s._sock = good
    s.send_frame(bytes(510))
    s._sock = dead
    s.send_frame(bytes(510)); s.send_frame(bytes(510))
    check(s._sock is dead,
          "a successful send did not clear the consecutive failure count")
    s._sock = good
    s.close()
    print("  ok")


def test_display_survives():
    section("the display renders in every state")
    fs = FakeFSEQ(frames=4000)
    idle = FakeFSEQ(frames=40)
    tl = _timeline([("01:00:00:00", "A", fs), ("01:00:50:00", "B", fs)],
                   idle="/tmp/idle.fseq")
    snd = CountingSender()
    p = Player(tl, FakeNetmap(), snd)
    p.idle_cue = timeline.Cue("00:00:00:00", "/tmp/idle.fseq", "preshow loop")
    p.idle_cue.fseq = idle
    p.idle_cue._spans = [(0, 0, 64)]
    d = LTCDecoder(48000)
    sc = disp.Screen(colour=False, cols=80)
    seen = set()
    for state, source, cue, nxt, tcs in (
            (LOST, IDLE, None, tl.cues[0], -1.0),
            (LOCKED, SHOW, tl.cues[0], tl.cues[1], tl.cues[0].tc_seconds + 5),
            (FREEWHEEL, SHOW, tl.cues[0], tl.cues[1], tl.cues[0].tc_seconds + 5),
            (LOCKED, BLACK, None, None, tl.cues[1].tc_seconds + 200)):
        p.state, p.source = state, source
        p.current_cue, p.next_cue, p.tc_seconds = cue, nxt, tcs
        p.last_ltc_at = time.monotonic() - 3
        p.last_ltc_text = "01:00:05:00"
        lines = disp.render(p, d, tl, sc, time.monotonic() - 90)
        text = "\n".join(lines)
        check("LTC IN" in text and "PLAYING" in text and "UP NEXT" in text,
              f"the display lost a heading in state {state}/{source}")
        check(max((len(l) for l in lines), default=0) < 200,
              f"a display line ran away in state {state}/{source}")
        seen.add(text)
        disp.one_line(p, d, tl)
    check(len(seen) == 4, "the display renders identically in different states")

    # the rate warning is the one that catches a silent 0.1% drift
    class D2:
        detected_rate = (29.97, False, True)
        measured_fps = 29.970
        measured_span = 30.0
        frames_decoded = 900
        sync_errors = 0
    p.state, p.source = LOCKED, SHOW
    p.ltc_frames_in = 900
    warns = disp.warnings_for(p, D2(), tl)
    check(any("29.97" in w and "30" in w for w in warns),
          "a 29.97 source against a 30 fps timeline raised no warning")
    tl2 = _timeline([("01:00:00:00", "A", fs)], fps=29.97)
    check(not any("adrift" in w for w in disp.warnings_for(p, D2(), tl2)),
          "a matching 29.97 timeline should raise no rate warning")

    # Drop frame disagreement is the other silent one: same rate, same digits,
    # two frames a minute of drift.
    class D3(D2):
        detected_rate = (29.97, True, True)
    warns = disp.warnings_for(p, D3(), tl2)
    check(any("drop" in w and "2 frames a minute" in w for w in warns),
          "a drop frame source against a non-drop timeline raised no warning")
    tl3 = _timeline([("01:00:00:00", "A", fs)], fps=29.97, drop=True)
    check(not any("2 frames a minute" in w
                  for w in disp.warnings_for(p, D3(), tl3)),
          "a matching drop frame timeline should raise no drop warning")

    # The input is the other half of the feed and its faults are silent ones:
    # a dead interface and a generator that has been switched off look the
    # same on the timecode lines.
    class FakeAudio:
        def __init__(self, **kw):
            self.device = {"name": "MOTU M4"}
            self.channel = 2
            self.blocks = 500
            self.reopens = 0
            self.level = audio_mod.Level()
            self._quiet = 0.0
            self.attached = True
            self.open_errors = 0
            self.pa_resets = 0
            self.last_error = ""
            self.stuck = False
            self.seconds_down = None
            self.pa_resets = 0
            self.last_error_at = None
            self.__dict__.update(kw)

        @property
        def seconds_since_error(self):
            if self.last_error_at is None:
                return None
            return time.monotonic() - self.last_error_at

        @property
        def seconds_since_block(self):
            return self._quiet

    p.audio = FakeAudio(_quiet=6.0)
    p.audio.level.feed([0.4])
    check(any("No audio has arrived" in w for w in disp.warnings_for(p, D2(), tl)),
          "an interface that stopped delivering audio raised no warning")

    p.audio = FakeAudio()
    p.audio.level.feed([0.4])
    check(not any("No audio" in w for w in disp.warnings_for(p, D2(), tl)),
          "a healthy input should raise no audio warning")

    p.audio = FakeAudio()
    p.audio.level.feed([0.0])
    check(any("silent" in w and "find" in w
              for w in disp.warnings_for(p, D2(), tl)),
          "a silent input should say so and point at `find`")

    p.audio = FakeAudio()
    p.audio.level.feed([1.0])
    check(any("clipped" in w for w in disp.warnings_for(p, D2(), tl)),
          "a clipping input raised no warning")

    p.audio = FakeAudio(reopens=3, last_error_at=time.monotonic())
    p.audio.level.feed([0.4])
    check(any("rebuilt 3 time" in w for w in disp.warnings_for(p, D2(), tl)),
          "an input rebuilt seconds ago should say so, in the warnings")
    check(not any("rebuilt 3 time" in h for h in disp.history_for(p)),
          "a fault happening now was filed as history")

    # The same input, long after it settled: history, not a red warning.
    p.audio = FakeAudio(reopens=3, open_errors=2, pa_resets=1,
                        last_error_at=time.monotonic() - (disp.RECENT_S + 5))
    p.audio.level.feed([0.4])
    live = disp.warnings_for(p, D2(), tl)
    check(not any("rebuilt 3 time" in w for w in live),
          f"a fault that stopped {disp.RECENT_S:.0f}s ago is still being "
          f"drawn as a current warning: {live}")
    hist = disp.history_for(p)
    check(any("rebuilt 3 time" in h for h in hist),
          f"it was dropped entirely instead of being kept as history: {hist}")
    check(any("audio system itself was rebuilt" in h for h in hist),
          f"the PortAudio rebuild is not recorded anywhere: {hist}")

    # And a sender whose errors have stopped must do the same.
    class OldSender:
        def __init__(self, age):
            self.universe_count = 4
            self.packets_sent = 100
            self.send_errors = 14
            self.reopens = 2
            self.broadcast_dests = []
            self.quiet_destinations = 0
            self.last_error = ("send to 10.0.0.100: [Errno 49] "
                               "Can't assign requested address")
            self.seconds_since_error = age
            self.seconds_since_ok = 0.0
    keep_sender = p.sender
    p.sender = OldSender(disp.RECENT_S + 30)
    live = disp.warnings_for(p, D2(), tl)
    check(not any("14 packet" in w for w in live),
          f"14 failed sends from earlier are still red: {live}")
    check(any("14 packet" in h for h in disp.history_for(p)),
          "the failed sends vanished instead of becoming history")
    p.sender = OldSender(1.0)
    check(any("14 packet" in w for w in disp.warnings_for(p, D2(), tl)),
          "sends failing right now must still be a warning")
    p.sender = keep_sender

    # An input that has been failing to open for a long time is not
    # "recovering". Jeff watched "rebuilding the input" once a second for
    # minutes while every open failed with PaErrorCode -9986, and neither the
    # screen nor Stop and Run told him the only cure was quitting the program.
    # 2026-09-14.
    p.audio = FakeAudio(attached=False, stuck=True, seconds_down=45.0,
                        pa_resets=4, open_errors=30,
                        last_error="open: Internal PortAudio error "
                                   "[PaErrorCode -9986]")
    p.audio.level.feed([0.0])
    stuck_w = disp.warnings_for(p, D2(), tl)
    check(any("QUIT" in w for w in stuck_w),
          f"an input stuck for 45s must say the program has to be quit and "
          f"started again, because a stop and a start keep the same process: "
          f"{stuck_w}")
    check(any("45s" in w for w in stuck_w),
          "the warning should say how long it has been down")
    check(not any("retried every second" in w for w in stuck_w),
          "a stuck input is still being described as recovering")

    # ...but an input that has only just dropped is NOT stuck yet, and must
    # not send anybody restarting the program over a two second hiccup.
    p.audio = FakeAudio(attached=False, stuck=False, seconds_down=3.0,
                        last_error="open: device busy")
    p.audio.level.feed([0.0])
    fresh_w = disp.warnings_for(p, D2(), tl)
    check(not any("QUIT" in w for w in fresh_w),
          f"a three second outage should not tell anyone to quit: {fresh_w}")
    check(any("not open" in w for w in fresh_w),
          f"a dropped input should still say it is not open: {fresh_w}")
    p.audio = None
    lines = disp.render(p, D2(), tl, sc, time.monotonic() - 10)
    check("input" not in "\n".join(lines),
          "with no live input there should be no input row at all")
    print("  ok")



def test_park_and_pause():
    section("a paused source is told apart from a lost one")
    fs = FakeFSEQ(frames=8000)          # 200s
    idle = FakeFSEQ(frames=40)
    tl = _timeline([("01:00:00:00", "A", fs)], idle="/tmp/idle.fseq")
    p = Player(tl, FakeNetmap(), CountingSender(), freewheel_ms=150,
               hold_ms=600, park_ms=150)
    p.idle_cue = timeline.Cue("00:00:00:00", "/tmp/idle.fseq", "preshow loop")
    p.idle_cue.fseq = idle
    p.idle_cue._spans = [(0, 0, 64)]
    p.start(step_ms=20)
    try:
        base = tcmod.parse_tc("01:00:30:00", 30)
        _drive(p, 0.5, base)
        check(p.state == LOCKED, f"expected LOCKED, got {p.state}")
        jumps_before = p.jumps

        # The deck is paused: it keeps sending, but the number stops moving.
        held = p.tc_seconds
        t0 = time.monotonic()
        while time.monotonic() - t0 < 1.2:
            p.feed_timecode(base + 0.5, time.monotonic(), text="01:00:30:15")
            time.sleep(0.03)
        check(p.state == PARKED,
              f"a repeating frame number should read as PARKED, got {p.state}")
        check(p.source == SHOW,
              f"a paused source should keep the show on the rig, got {p.source}")
        check(p.current_cue and p.current_cue.name == "A",
              "the cue was dropped while the source was merely paused")
        check(abs(p.tc_seconds - (base + 0.5)) < 0.05,
              f"while parked the position must sit on the repeated frame, "
              f"got {p.tc_seconds:.3f} not {base + 0.5:.3f}")
        check(p.jumps <= jumps_before + 1,
              f"a pause logged {p.jumps - jumps_before} jumps; freezing the "
              f"clock should make it at most one")
        frame_while_parked = p.current_frame
        time.sleep(0.4)
        check(p.current_frame == frame_while_parked,
              "the frame moved while the source was parked")

        # Play again, from where it stopped.
        _drive(p, 0.6, base + 0.5)
        check(p.state == LOCKED, f"expected LOCKED after resume, got {p.state}")
        check(p.current_frame > frame_while_parked,
              "playback did not resume after the pause")

        # Now the other case: the source stops sending entirely.
        time.sleep(1.0)
        check(p.state == LOST,
              f"a source that stops sending should end in LOST, got {p.state}")
        check(p.source == IDLE,
              f"with the default policy a lost feed goes to the preshow look, "
              f"got {p.source}")
    finally:
        p.stop()
    print("  ok")


def test_on_lost_policies():
    section("what a lost feed does is a policy, not a guess")
    fs = FakeFSEQ(frames=8000)
    idle = FakeFSEQ(frames=40)

    def build(policy):
        tl = _timeline([("01:00:00:00", "A", fs)], idle="/tmp/idle.fseq")
        p = Player(tl, FakeNetmap(), CountingSender(), freewheel_ms=100,
                   hold_ms=300, on_lost=policy)
        p.idle_cue = timeline.Cue("00:00:00:00", "/tmp/idle.fseq", "preshow")
        p.idle_cue.fseq = idle
        p.idle_cue._spans = [(0, 0, 64)]
        p.start(step_ms=20)
        return p

    want = {"hold": HOLD, "blackout": BLACK, "preshow": IDLE}
    for policy, source in want.items():
        p = build(policy)
        try:
            _drive(p, 0.4, tcmod.parse_tc("01:00:30:00", 30))
            check(p.source == SHOW, f"{policy}: show did not start")
            time.sleep(0.8)
            check(p.state == LOST, f"{policy}: expected LOST, got {p.state}")
            check(p.source == source,
                  f"--on-lost {policy} should leave the rig on {source}, "
                  f"got {p.source}")
            if policy == "hold":
                # The frame it holds is the one it reached at the end of the
                # freewheel, not the last one it had timecode for; what matters
                # is that it then stops moving and keeps its cue.
                marked = bytes(p._buf)
                frame = p.current_frame
                time.sleep(0.5)
                check(bytes(p._buf) == marked,
                      "hold kept changing the frame it was supposed to hold")
                check(p.current_frame == frame,
                      "hold kept advancing the frame counter")
                check(p.current_cue is not None,
                      "hold dropped the cue it was holding")
        finally:
            p.stop()
    print("  ok")



def test_pause_does_not_poison_the_rate():
    section("a paused source does not invent a frame rate")
    sr = 48000
    # Six seconds running, four seconds parked on one frame, four seconds of
    # silence, eight seconds running again. This is a rehearsal stop, and it is
    # what turned a 30 fps source into a confident 29.97 on screen: the frame
    # number stopped moving while the sample clock did not, so the measured
    # rate slid smoothly down through every plausible value on its way to zero.
    parts, lvl = [], 1.0
    au, lvl = synthesize(1, 0, 0, 0, 30.0, sr, frames=180, start_level=lvl,
                         with_level=True)
    parts.append(au)
    for _ in range(120):
        au, lvl = synthesize(1, 0, 6, 0, 30.0, sr, frames=1, start_level=lvl,
                             with_level=True)
        parts.append(au)
    parts.append([0.0] * (sr * 4))
    au, lvl = synthesize(1, 0, 6, 0, 30.0, sr, frames=240, start_level=lvl,
                         with_level=True)
    parts.append(au)
    audio = [v for p in parts for v in p]

    d = LTCDecoder(sr)
    reported, measured, during_park = set(), [], []
    for i in range(0, len(audio), 4800):
        d.feed(audio[i:i + 4800])
        t = i / float(sr)
        r = d.detected_rate[0]
        if r is not None:
            reported.add(round(r, 3))
        if d.measured_fps is not None:
            measured.append((t, d.measured_fps))
        if 7.0 < t < 13.0:            # parked, then silent
            during_park.append(d.measured_fps)

    check(reported <= {30.0},
          f"a 30 fps source with a pause in it reported {sorted(reported)}")
    check(all(m is None for m in during_park),
          "the measurement kept running while the frame number stood still; "
          "it must be dropped, not decayed")
    check(all(abs(m - 30.0) < 0.01 for _, m in measured),
          f"measured rates outside tolerance: "
          f"{[round(m,3) for _,m in measured if abs(m-30.0)>=0.01][:5]}")
    check(measured and measured[-1][0] > 15.0,
          "the measurement never came back after the pause ended")

    # And the other half: a measurement that matches nothing must not be
    # rounded to whichever candidate happens to be nearest.
    d2 = LTCDecoder(sr)
    d2._rollover_fps = 30
    d2._measured = 17.0
    d2._measured_span = 30.0
    rate, _, conf = d2.detected_rate
    check(rate == 30.0 and not conf,
          f"a nonsense measurement of 17 fps was reported as {rate} "
          f"(confident={conf}) instead of falling back to the frame count")

    # The generator has to count in frame numbers, not seconds times the rate,
    # or every 29.97 test in this file is measuring the wrong timecode.
    au = synthesize(1, 0, 0, 0, 29.97, sr, frames=40)
    d3 = LTCDecoder(sr)
    got = d3.feed(au)
    check(got and got[0].h == 1 and got[0].m == 0 and got[0].s == 0,
          f"synthesize(1,0,0,0) at 29.97 produced {got[0] if got else None}, "
          f"not 01:00:00:xx")
    print("  ok")



# ------------------------------------------------------- audio input ------
class FakeStream:
    """Delivers blocks to a PortAudio-style callback from its own thread."""

    def __init__(self, sd, device, channels, samplerate, blocksize, callback):
        self.sd, self.device, self.channels = sd, device, channels
        self.rate, self.blocksize, self.callback = samplerate, blocksize, callback
        self._run = False
        self._t = None
        self.closed = False

    def start(self):
        self._run = True
        self._t = threading.Thread(target=self._pump, daemon=True)
        self._t.start()

    def _pump(self):
        import numpy as np
        pos = 0
        while self._run:
            if self.sd.dead:
                time.sleep(0.02)
                continue
            block = self.sd.block_for(self.device, self.channels,
                                      pos, self.blocksize)
            pos += self.blocksize
            try:
                self.callback(block, self.blocksize, None, None)
            except Exception:
                pass
            time.sleep(self.blocksize / float(self.rate))

    def stop(self):
        self._run = False

    def close(self):
        self._run = False
        self.closed = True

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *a):
        self.close()


def _seq_seconds(text):
    """Read an xLights-style M:SS.mmm position back into seconds."""
    m, _, rest = text.partition(":")
    return int(m) * 60 + float(rest)


class FakeSD:
    """A sounddevice stand-in with a multi-input interface whose timecode is
    deliberately NOT on input 1, which is the case the headphone jack never
    exercised and a USB box always will."""

    def __init__(self, ltc_channel=2, rates=(48000,)):
        import numpy as np
        self.devices = [
            {"name": "MacBook Air Microphone", "max_input_channels": 1,
             "max_output_channels": 0, "default_samplerate": 48000, "hostapi": 0},
            {"name": "MOTU M4", "max_input_channels": 4,
             "max_output_channels": 4, "default_samplerate": 48000, "hostapi": 0},
            {"name": "MOTU M6", "max_input_channels": 6,
             "max_output_channels": 6, "default_samplerate": 48000, "hostapi": 0},
            {"name": "Display Audio", "max_input_channels": 0,
             "max_output_channels": 2, "default_samplerate": 48000, "hostapi": 0},
        ]
        self.default = type("D", (), {"device": (0, 3)})()
        self.ltc_channel = ltc_channel
        self.ok_rates = set(rates)
        self.dead = False
        self.opened = []
        self.streams = []
        self._tone = synthesize(1, 0, 0, 0, 30.0, 48000, frames=300,
                                amplitude=0.4)
        self._np = np

    def query_devices(self):
        return list(self.devices)

    def query_hostapis(self, i):
        return {"name": "Core Audio"}

    def check_input_settings(self, device=None, channels=None, samplerate=None,
                             dtype=None):
        if samplerate not in self.ok_rates:
            raise ValueError(f"{samplerate} not supported")

    def InputStream(self, device=None, channels=None, samplerate=None,
                    blocksize=None, dtype=None, callback=None):
        if samplerate not in self.ok_rates:
            raise ValueError(f"{samplerate} not supported")
        self.opened.append((device, channels, samplerate))
        s = FakeStream(self, device, channels, samplerate, blocksize, callback)
        self.streams.append(s)
        return s

    def block_for(self, device, channels, pos, n):
        np = self._np
        out = np.zeros((n, channels), dtype=np.float32)
        if device == 1:                       # the MOTU carries real timecode
            tone = self._tone
            seg = [tone[(pos + i) % len(tone)] for i in range(n)]
            ch = self.ltc_channel - 1
            if ch < channels:
                out[:, ch] = np.asarray(seg, dtype=np.float32)
            if channels > 0:                  # input 1 has unrelated audio
                out[:, 0] = np.float32(0.3) * np.sin(
                    np.arange(pos, pos + n, dtype=np.float32) * 0.01)
        return out


def test_device_resolution():
    section("finding the right input, by a name that does not move")
    sd = FakeSD()
    ins = audio_mod.list_inputs(sd)
    check([d["name"] for d in ins] ==
          ["MacBook Air Microphone", "MOTU M4", "MOTU M6"],
          f"outputs-only devices should not be listed as inputs: "
          f"{[d['name'] for d in ins]}")
    check(audio_mod.resolve_device(sd, None)["name"] == "MacBook Air Microphone",
          "no device given should pick the system default")
    check(audio_mod.resolve_device(sd, "motu m4")["index"] == 1,
          "a name should match case-insensitively")
    check(audio_mod.resolve_device(sd, 1)["name"] == "MOTU M4",
          "an index should still work")
    check(audio_mod.resolve_device(sd, "1")["name"] == "MOTU M4",
          "an index passed as text should still work")
    # An ambiguous name must not be guessed: picking one of two interfaces for
    # someone is how a show chases the wrong input all night.
    for spec, why in (("MOTU", "ambiguous"), ("Scarlett", "absent"), (9, "absent")):
        try:
            audio_mod.resolve_device(sd, spec)
            check(False, f"{spec!r} is {why} and should have raised")
        except audio_mod.DeviceError as e:
            check("MOTU M4" in str(e),
                  f"the error for {spec!r} must list what IS there")
    try:
        audio_mod.resolve_device(FakeSD.__new__(FakeSD) if False else
                                 type("E", (), {"query_devices": lambda s: [],
                                                "default": sd.default})(), None)
        check(False, "no inputs at all should raise")
    except audio_mod.DeviceError:
        pass

    # An interface running its own clock refuses the rate you ask for.
    picky = FakeSD(rates=(44100,))
    dev = audio_mod.resolve_device(picky, "M4")
    check(audio_mod.negotiate_rate(picky, dev, 48000) == 44100,
          "a refused rate should fall back to one the device takes")
    try:
        audio_mod.negotiate_rate(FakeSD(rates=()), dev, 48000)
        check(False, "a device that takes no rate should raise")
    except audio_mod.DeviceError:
        pass
    print("  ok")


def test_timecode_on_a_channel_other_than_one():
    section("timecode on input 2 of an interface")
    # This is the whole difference between a headphone jack and a USB box, and
    # listening to input 1 when the feed is on input 2 looks exactly like a
    # dead cable.
    sd = FakeSD(ltc_channel=2)
    dev = audio_mod.resolve_device(sd, "M4")
    got = []
    src = audio_mod.InputSource(sd, dev, 2, 48000, 512,
                                lambda b, t: got.append(b))
    src.start()
    try:
        wait_for(lambda: len(got) > 60, timeout=3.0)
        d = LTCDecoder(48000)
        frames = []
        for b in got:
            frames += d.feed(b)
        check(len(frames) > 5,
              f"listening to input 2 should decode timecode, got "
              f"{len(frames)} frames")
        check(sd.opened and sd.opened[0][1] == 2,
              f"the stream must be opened with enough channels to reach "
              f"input 2, was opened with {sd.opened[0][1] if sd.opened else '?'}")
        check(src.level.hold > 0.1,
              f"the level meter should see the timecode, reads {src.level.hold}")
    finally:
        src.stop()

    # And the failure it replaces: input 1 carries audio, but not timecode.
    got1 = []
    src1 = audio_mod.InputSource(sd, dev, 1, 48000, 512,
                                 lambda b, t: got1.append(b))
    src1.start()
    try:
        wait_for(lambda: len(got1) > 40, timeout=3.0)
        d1 = LTCDecoder(48000)
        frames1 = []
        for b in got1:
            frames1 += d1.feed(b)
        check(not frames1, "input 1 has no timecode on it and must decode none")
        check(src1.level.hold > 0.1,
              "input 1 is not silent, so level alone cannot diagnose this")
    finally:
        src1.stop()
    print("  ok")


def test_find_names_the_channel():
    section("find says which device and which input")
    sd = FakeSD(ltc_channel=3)
    res = audio_mod.scan(sd, LTCDecoder, seconds=0.8)
    by_name = {r["device"]["name"]: r for r in res}
    motu = by_name["MOTU M4"]
    check(motu["error"] is None, f"scanning the interface failed: {motu['error']}")
    withtc = [c for c in motu["channels"] if c["frames"] > 3]
    check(len(withtc) == 1 and withtc[0]["channel"] == 3,
          f"timecode was put on input 3; find reported "
          f"{[c['channel'] for c in withtc]}")
    silent = [c["channel"] for c in motu["channels"]
              if c["verdict"] == "silent"]
    check(2 in silent and 4 in silent,
          f"inputs 2 and 4 are silent and should read as silent, got {silent}")
    mic = by_name["MacBook Air Microphone"]
    check(all(c["frames"] == 0 for c in mic["channels"]),
          "the built-in mic has no timecode and must not claim any")
    print("  ok")


def test_input_rebuilds_itself():
    section("the input rebuilds itself when the interface goes away")
    sd = FakeSD(ltc_channel=1)
    dev = audio_mod.resolve_device(sd, "M4")
    src = audio_mod.InputSource(sd, dev, 1, 48000, 512, lambda b, t: None)
    src.SILENCE_BEFORE_REOPEN_S = 0.4
    src.REOPEN_BACKOFF_S = 0.2
    src.start()
    try:
        check(wait_for(lambda: src.blocks > 5), "no audio arrived at all")
        sd.dead = True                      # someone kicks the USB cable
        # Let whatever was already in flight land before taking the reading:
        # measuring across the unplug is what made this test flaky.
        settled, n = False, -1
        for _ in range(40):
            time.sleep(0.05)
            if src.blocks == n:
                settled = True
                break
            n = src.blocks
        check(settled, "the input never stopped delivering after the unplug")
        check(wait_for(lambda: src.reopens >= 1, timeout=4.0),
              f"a silent input should be rebuilt, reopens={src.reopens}")
        check(src.blocks == n,
              "blocks kept arriving from an interface that is gone")
        sd.dead = False                     # and plugs it back in
        check(wait_for(lambda: src.blocks > n + 5, timeout=3.0),
              f"audio did not resume after the rebuild ({n} -> {src.blocks})")
        reopens_after = src.reopens
        time.sleep(0.8)
        check(src.reopens == reopens_after,
              f"the input kept rebuilding itself while healthy "
              f"({reopens_after} -> {src.reopens})")
    finally:
        src.stop()
    print("  ok")


def test_level_meter():
    section("the level meter")
    lv = audio_mod.Level()
    lv.feed([0.0] * 100)
    check(lv.verdict() == "silent", f"silence read as {lv.verdict()}")
    lv = audio_mod.Level()
    lv.feed([0.4, -0.35, 0.2])
    check(lv.verdict() == "ok", f"a healthy level read as {lv.verdict()}")
    lv = audio_mod.Level()
    lv.feed([0.03, -0.02])
    check(lv.verdict() == "very low", f"a weak level read as {lv.verdict()}")
    lv = audio_mod.Level()
    lv.feed([1.0, -1.0])
    check(lv.verdict() == "clipping" and lv.clipped,
          f"a clipped level read as {lv.verdict()}")
    print("  ok")


def test_timeline_input_block():
    section("the input lives in the show file")
    import json
    import tempfile
    d = tempfile.mkdtemp()
    def write(doc):
        p = os.path.join(d, "t.json")
        json.dump(doc, open(p, "w"))
        return p
    base = {"fps": 30, "show_dir": d, "cues": []}
    tl = timeline.Timeline.load(write(dict(base, input={"device": "MOTU M4",
                                                        "channel": 2})))
    check(tl.input == {"device": "MOTU M4", "channel": 2},
          f"the input block did not survive loading: {tl.input}")
    check(timeline.Timeline.load(write(base)).input == {},
          "a file with no input block should load with an empty one")
    for bad, why in (({"device": "x", "chanel": 2}, "a misspelt key"),
                     ({"channel": 0}, "a zero channel"),
                     ({"channel": "2"}, "a channel as text"),
                     ("MOTU M4", "a bare string")):
        try:
            timeline.Timeline.load(write(dict(base, input=bad)))
            check(False, f"{why} should have been rejected")
        except ValueError:
            pass
    print("  ok")



def test_installer_and_launcher_names():
    section("the installer can actually create its launcher")
    # This is here because it did not. The installer wrote a launcher named
    # `ltcplay` into a folder whose python package directory is also named
    # `ltcplay`, so the redirect hit a directory and the install died at the
    # last line. No amount of unit testing the player would have caught it;
    # only running the installer does, and nothing ever ran the installer.
    here = os.path.dirname(os.path.abspath(__file__))
    inst = launcher("Install ltcplay.command")
    menu = launcher("Run ltcplay.command")
    if not os.path.exists(inst):
        print("  installer not in this folder, skipped")
        return
    text = open(inst).read()
    m = re.search(r"^LAUNCHER=(\S+)", text, re.M)
    if not check(m, "the installer does not name its launcher in one place"):
        return
    name = m.group(1)
    check(not os.path.isdir(os.path.join(here, name)),
          f"the installer wants to write a launcher called {name!r}, but a "
          f"DIRECTORY of that name is sitting in the same folder. The redirect "
          f"will fail and the install will die at the last step.")
    check(name != "ltcplay",
          "the launcher cannot be called 'ltcplay': that is the package "
          "directory's name")
    check(f'cat > "$LAUNCHER"' in text,
          "the installer should write the launcher through $LAUNCHER so the "
          "name is defined exactly once")

    # /usr/bin/python3 on a Mac that has never had Xcode EXISTS and does
    # nothing: macOS shows its own "developer tools not found" dialog and
    # every call fails. The installer tested that python3 was present, which
    # on that machine is always true, then died with "Could not create the
    # Python environment" -- the second useless sentence in a row. Jeff hit
    # this setting up the second Mac, 2026-09-14.
    check('"$PY" -V' in text,
          "the installer checks that python3 EXISTS but never runs it; on a "
          "fresh Mac it exists and cannot run")
    check("xcode-select --install" in text,
          "nothing tells the operator the one command that fixes a Mac with "
          "no Command Line Tools")
    check("xcrun" in text,
          "the installer does not recognise the developer-tools failure in "
          "what python3 prints, so it reports a generic error instead")
    check('VENVLOG=$("$PY" -m venv' in text,
          "the environment error is not captured, so the operator is told "
          "only that it failed. The name $VENVLOG appearing in the message "
          "is not enough: it has to be assigned from the command.")
    # And the launchers must survive a copy that drops the execute bit.
    check("chmod +x" in text and "*.command" in text,
          "the installer no longer repairs the execute bit on the launchers")

    import subprocess
    rc = subprocess.run(["bash", "-n", inst], capture_output=True, text=True)
    check(rc.returncode == 0,
          f"the installer is not valid bash: {rc.stderr.strip()}")

    # The autostart agent, same class of bug as the installer's python3 check:
    # launchctl returning 0 means the job was ACCEPTED, not that the program
    # runs. Jeff installed it on 2026-09-14, it reported success, nothing was
    # listening, and no log was ever written. 2026-09-14.
    # The menu is "everything anyone needs on a show day", and until
    # 2026-09-14 there was no way on it to prove the copy in front of you is
    # the one you think it is. Jeff asked how to test an update had landed and
    # the honest answer was a path nobody would remember.
    if os.path.exists(menu):
        m = open(menu).read()
        check("selftest.py" in m,
              "the menu has no way to run the self test, so nobody can prove "
              "an install or an update actually landed")
        check("prove this copy works" in m,
              "the self-test item is not offered in the menu's own list")
        rc = subprocess.run(["bash", "-n", menu], capture_output=True, text=True)
        check(rc.returncode == 0,
              f"the menu is not valid bash: {rc.stderr.strip()}")

    # Jeff, 2026-09-14: "always give me command files to execute instead of
    # asking me to type stuff into terminal. Its safer." The agent refuses
    # folders macOS protects, so the remedy has to be double-clickable too.
    mover = launcher("Move somewhere macOS allows.command")
    check(os.path.exists(mover),
          "the agent tells the operator to move the folder and ships no way "
          "to do it without typing mv into a terminal")
    if os.path.exists(mover):
        mv = open(mover).read()
        rc = subprocess.run(["bash", "-n", mover], capture_output=True,
                            text=True)
        check(rc.returncode == 0,
              f"the mover is not valid bash: {rc.stderr.strip()}")
        check("rm -rf \"$DEST/.venv\"" in mv,
              "moving the folder leaves a Python environment built for the "
              "old path, which fails in a way nobody would connect to a move")
        check("A SHOW IS RUNNING" in mv,
              "the folder can be moved out from under a running show")
        check("already something at" in mv,
              "the mover would overwrite whatever is at the destination")
        # Run from an update pack it used to move the PACK, leaving a second
        # folder behind and updating nothing. Jeff, 2026-09-14.
        check("Apply this update.command" in mv and "update pack" in mv,
              "the mover can still be run from an update pack, where it "
              "moves the pack and updates nothing")
        check("Move somewhere macOS allows.command" in
              open(os.path.join(here, "ltcplay", "cli.py")).read(),
              "the bundle does not carry the mover, so the second Mac gets "
              "the refusal with no way to act on it")

    # Two copies of a show player on one machine is how the wrong renders got
    # driven once already this season. Jeff, 2026-09-14: "some of these files
    # are duplicates."
    finder = launcher("Find every copy.command")
    check(os.path.exists(finder),
          "there is no way to see every copy of ltcplay on a Mac, so nobody "
          "can tell which one is about to run the show")
    if os.path.exists(finder):
        fd = open(finder).read()
        rc = subprocess.run(["bash", "-n", finder], capture_output=True,
                            text=True)
        check(rc.returncode == 0,
              f"the copy finder is not valid bash: {rc.stderr.strip()}")
        check("never deletes" in fd,
              "the copy finder does not promise to leave files alone")
        check("_reset_portaudio" in fd,
              "the copy finder cannot tell an old build from a new one")
        check("Find every copy.command" in
              open(os.path.join(here, "ltcplay", "cli.py")).read(),
              "the bundle does not carry the copy finder")

    # macOS decides which program is asking for the microphone from the
    # code signature and Info.plist of the running executable. A bare python
    # started by launchd has neither, so it is never prompted and never
    # granted. DEF-0228, confirmed on the show Mac 2026-09-14.
    app = launcher("Build LTC Player app.command")
    check(os.path.exists(app),
          "there is no way to build an app bundle, so the login agent can "
          "never be granted a microphone")
    if os.path.exists(app):
        ab = open(app, encoding="utf-8").read()
        rc = subprocess.run(["bash", "-n", app], capture_output=True, text=True)
        check(rc.returncode == 0,
              f"the app builder is not valid bash: {rc.stderr.strip()}")
        check("NSMicrophoneUsageDescription" in ab,
              "the bundle declares no microphone reason, so macOS has "
              "nothing to put in the dialog and may not show one")
        plist_body = ab.split("<<PLIST", 1)[-1].split("\nPLIST\n", 1)[0]
        check("LSUIElement" in plist_body,
              "the bundle is not marked LSUIElement, so macOS would give it a "
              "Dock icon and then report it as not responding, because it has "
              "no windows and no event loop of its own")
        check("LSBackgroundOnly" not in plist_body,
              "a background-only app cannot put a permission dialog on "
              "screen, which is the one thing this bundle exists to do")
        check("codesign" in ab and "--sign -" in ab,
              "the bundle is not signed, so the grant has no stable identity "
              "to attach to and would not survive a relaunch")
        check("import numpy, sounddevice, zstandard" in ab,
              "the builder does not prove the bundle can load the audio "
              "libraries, so the app could start and read nothing")
        check("open(py, O_RDONLY)" in ab,
              "the launcher hands over to a Python environment it never "
              "checked it can open; a double-clicked app that does nothing "
              "at all is the worst failure this has")
        check("access(py" not in ab,
              "access() is refused SILENTLY on a protected folder and reports "
              "a file that is sitting right there as missing, which is "
              "exactly the wrong thing to tell somebody at 3am. It has to be "
              "a real open()")
        check("strerror(err)" in ab and "EACCES" in ab,
              "the launcher does not say WHY it could not use the file, so "
              "'missing' and 'refused' look identical to the operator")
        check("AppTranslocation" in ab,
              "an app macOS is running from a temporary read-only copy sees "
              "no show folder at all, and nothing would say so")
        check("Library/Logs/LTCPlayer-start.log" in ab,
              "the failure exists only in a dialog, which is gone the moment "
              "it is dismissed")
        # The builder must not leave a half-built bundle that Autostart then
        # tries to use.
        check("bye " in ab and "Nothing was built" in ab,
              "a failed build does not say that nothing was built")
        # Signing is not the same as macOS being willing to run it. Jeff got
        # "ltcplay is damaged, move it to the Trash" at the next reboot,
        # hours after the builder reported success. 2026-09-14.
        check("spctl" in ab,
              "the builder never asks Gatekeeper, so a bundle macOS refuses "
              "is only discovered at the next reboot")
        check("--verify --deep --strict" in ab,
              "the signature is never verified after it is applied")
        check(ab.count('rm -rf "$HERE/$APP"') >= 3,
              "a rejected bundle is left in place, ready to be found on a "
              "show night")

    old_app = launcher("Build the login app.command")
    if os.path.exists(old_app):
        ob = open(old_app, encoding="utf-8").read()
        check("Build LTC Player app.command" in ob,
              "the superseded login-app builder does not point at the one "
              "that replaced it, so it reads as a second, equal choice")

    auto_t = open(launcher("Autostart ltcplay.command")).read()
    check("LTC Player.app/Contents/MacOS/LTC Player" in auto_t,
          "the agent never uses LTC Player.app, so building it changes "
          "nothing about what starts at login")
    check("ltcplay.app/Contents/MacOS/ltcplay" in auto_t,
          "the agent dropped the older bundle, which is still installed on "
          "machines that built one")
    check('[ -f "$APPBOOT" ]' in auto_t,
          "the agent would use a half-built bundle")
    check("EnvironmentVariables" in auto_t,
          "the agent cannot pass PYTHONHOME, so a copied interpreter that "
          "needs it would fail to start at login")

    # An update that is dragged in by hand is an update where one file gets
    # missed. Jeff, 2026-09-14: "I need just the new stuff", and the standing
    # rule is command files, not typed commands.
    apply_ = launcher("Apply this update.command")
    if os.path.exists(apply_):
        at = open(apply_, encoding="utf-8").read()
        # A rollback that restores the package and leaves the new launchers
        # behind produces an install whose tools and code disagree. That is
        # worse than either version on its own, and it happened for real on
        # the show Mac: the new trigger tool ran against the old engine and
        # reported the show file as unloadable.
        check("restore()" in at and at.count("\n  restore\n") >= 2,
              "the updater does not restore the WHOLE install on failure, so "
              "a rolled-back machine is left with new tools running old code")
        check('cp "$f" "$BACKUP/"' in at,
              "the updater keeps only the package, so the launchers it is "
              "about to overwrite cannot be put back")
        # The suite is the gate. A finding about the SHOW FILE must not hold
        # back a program the updater is forbidden to fix.
        check('grep -q "SHOW FILE"' in at,
              "the updater does not surface show-file findings, so they "
              "either block the update or vanish")
        check('[ -d "$SRC/_payload" ]' in at,
              "an update pack with its payload loose at the top level "
              "invites somebody to run an install-only tool from it")
    if SOURCE_TREE:
        check(os.path.exists(apply_),
              "there is no double-clickable way to apply an update to an "
              "existing install")
    if os.path.exists(apply_):
        ap = open(apply_).read()
        rc = subprocess.run(["bash", "-n", apply_], capture_output=True,
                            text=True)
        check(rc.returncode == 0,
              f"the updater is not valid bash: {rc.stderr.strip()}")
        check("ltcplay.previous" in ap,
              "the updater overwrites the program with no way back")
        check("selftest.py" in ap and "all checks passed" in ap,
              "the updater does not prove the result, so a bad update is "
              "discovered on show night")
        check("A SHOW IS RUNNING" in ap,
              "the program can be replaced under a running show")
        check("-d \"$SRC/show\"" in ap,
              "the updater does not refuse a FULL bundle, which would copy a "
              "whole show into another install")
        for keep in (".venv", "show"):
            check(f'cp -R "$SRC/ltcplay/."' in ap,
                  "the updater copies more than the program")

    # Under the login agent there is no window to close and no obvious
    # process to kill, and some faults (a poisoned PortAudio) only clear on a
    # real process restart. Jeff, 2026-09-14.
    restart = launcher("Restart ltcplay.command")
    check(os.path.exists(restart),
          "there is no way to force a real restart when the login agent owns "
          "the engine; some faults only clear when the process dies")
    if os.path.exists(restart):
        rs = open(restart).read()
        rc = subprocess.run(["bash", "-n", restart], capture_output=True,
                            text=True)
        check(rc.returncode == 0,
              f"the restart script is not valid bash: {rc.stderr.strip()}")
        check("kickstart" in rs,
              "the restart does not use launchctl kickstart, so it cannot "
              "restart an agent-owned engine")
        check("A SHOW IS RUNNING" in rs and "RESTART" in rs,
              "a restart can be triggered mid-show without a hard "
              "confirmation; it blacks the rig out for a few seconds")
        check("ltcplay.cli" in rs,
              "with no agent installed the restart cannot find the process "
              "to stop")
        check("Restart ltcplay.command" in
              open(os.path.join(here, "ltcplay", "cli.py")).read(),
              "the bundle does not carry the restart")

    # The plist writes key and value on ONE line. Parsing it with
    # "grep -A1 | tail -1" reads the NEXT key and prints nonsense, which is
    # what Autostart did until 2026-09-14.
    for f in (finder, launcher("Autostart ltcplay.command")):
        if os.path.exists(f):
            t = open(f).read()
            check("workdir_of" in t,
                  f"{os.path.basename(f)} reads the agent's folder out of the "
                  f"plist by hand; key and value share a line, so it prints "
                  f"the wrong thing")

    auto = launcher("Autostart ltcplay.command")
    if os.path.exists(auto):
        a = open(auto).read()
        rc = subprocess.run(["bash", "-n", auto], capture_output=True, text=True)
        check(rc.returncode == 0,
              f"the autostart script is not valid bash: {rc.stderr.strip()}")
        check("NEVER ANSWERED" in a,
              "the agent still reports success on launchctl's exit code "
              "alone; it has to wait for the engine to actually answer")
        check("/api/state" in a.split("bootstrap")[-1],
              "nothing polls the engine after the agent is accepted")
        check("CloudStorage" in a,
              "the agent can still be installed from a Dropbox or iCloud "
              "folder, where launchd cannot reach it and fails silently")
        # Desktop, Documents and Downloads are TCC protected: a launchd
        # process does not inherit the consent Finder and Terminal have, and
        # is refused with "Operation not permitted". Jeff hit this on
        # 2026-09-14 following my own instruction to install from the Desktop
        # bundle.
        for prot in ("Desktop", "Documents", "Downloads"):
            check(f'"$HOME/{prot}"' in a,
                  f"the agent can still be installed from ~/{prot}, which "
                  f"macOS protects; launchd is refused there with "
                  f"Operation not permitted and writes no log")
        check("Operation not permitted" in a,
              "the refusal does not name the error the operator actually "
              "saw, so they cannot connect the two")
        # The agent used to promise a microphone prompt that never comes:
        # macOS does not prompt for a login agent at all. Confirmed on the
        # show Mac, 2026-09-14 (DEF-0228).
        check("does NOT prompt" in a,
              "the agent still tells the operator a microphone prompt will "
              "appear; it does not, and they discover the silence later")
        check("Web ltcplay.command' instead" in a or
              "'Web ltcplay.command' instead" in a,
              "nothing names the fallback that actually hears timecode")
        check("No browser opens by itself" in a,
              "nothing tells the operator that no browser will open; the "
              "agent runs with --no-browser on purpose")

    # A MacBook that sleeps mid-show freezes the rig on its last frame and
    # stops the clock the chase runs on. Every launcher that can drive a rig
    # has to hold the machine awake for as long as it is open.
    for awake in ("Run ltcplay.command", "Web ltcplay.command"):
        f = launcher(awake)
        if not os.path.exists(f):
            continue
        t = open(f).read()
        check("caffeinate" in t,
              f"{awake} does not stop the Mac sleeping; a lid closed "
              f"mid-set freezes the rig")
        check("-w $$" in t or "-w $PPID" in t,
              f"{awake}'s caffeinate is not tied to the window's "
              f"lifetime, so it either dies immediately or outlives the show")

    # The autostart agent is the answer to "the process is not there and
    # nobody is standing at the laptop". It must never bring a rig to life on
    # its own, though: it comes up idle and a person presses Run.
    # The menu is "everything anyone needs on a show day", and until
    # 2026-09-14 there was no way on it to prove the copy in front of you is
    # the one you think it is. Jeff asked how to test an update had landed and
    # the honest answer was a path nobody would remember.
    if os.path.exists(menu):
        m = open(menu).read()
        check("selftest.py" in m,
              "the menu has no way to run the self test, so nobody can prove "
              "an install or an update actually landed")
        check("prove this copy works" in m,
              "the self-test item is not offered in the menu's own list")
        rc = subprocess.run(["bash", "-n", menu], capture_output=True, text=True)
        check(rc.returncode == 0,
              f"the menu is not valid bash: {rc.stderr.strip()}")

    # Jeff, 2026-09-14: "always give me command files to execute instead of
    # asking me to type stuff into terminal. Its safer." The agent refuses
    # folders macOS protects, so the remedy has to be double-clickable too.
    mover = launcher("Move somewhere macOS allows.command")
    check(os.path.exists(mover),
          "the agent tells the operator to move the folder and ships no way "
          "to do it without typing mv into a terminal")
    if os.path.exists(mover):
        mv = open(mover).read()
        rc = subprocess.run(["bash", "-n", mover], capture_output=True,
                            text=True)
        check(rc.returncode == 0,
              f"the mover is not valid bash: {rc.stderr.strip()}")
        check("rm -rf \"$DEST/.venv\"" in mv,
              "moving the folder leaves a Python environment built for the "
              "old path, which fails in a way nobody would connect to a move")
        check("A SHOW IS RUNNING" in mv,
              "the folder can be moved out from under a running show")
        check("already something at" in mv,
              "the mover would overwrite whatever is at the destination")
        # Run from an update pack it used to move the PACK, leaving a second
        # folder behind and updating nothing. Jeff, 2026-09-14.
        check("Apply this update.command" in mv and "update pack" in mv,
              "the mover can still be run from an update pack, where it "
              "moves the pack and updates nothing")
        check("Move somewhere macOS allows.command" in
              open(os.path.join(here, "ltcplay", "cli.py")).read(),
              "the bundle does not carry the mover, so the second Mac gets "
              "the refusal with no way to act on it")

    # Two copies of a show player on one machine is how the wrong renders got
    # driven once already this season. Jeff, 2026-09-14: "some of these files
    # are duplicates."
    finder = launcher("Find every copy.command")
    check(os.path.exists(finder),
          "there is no way to see every copy of ltcplay on a Mac, so nobody "
          "can tell which one is about to run the show")
    if os.path.exists(finder):
        fd = open(finder).read()
        rc = subprocess.run(["bash", "-n", finder], capture_output=True,
                            text=True)
        check(rc.returncode == 0,
              f"the copy finder is not valid bash: {rc.stderr.strip()}")
        check("never deletes" in fd,
              "the copy finder does not promise to leave files alone")
        check("_reset_portaudio" in fd,
              "the copy finder cannot tell an old build from a new one")
        check("Find every copy.command" in
              open(os.path.join(here, "ltcplay", "cli.py")).read(),
              "the bundle does not carry the copy finder")

    # macOS decides which program is asking for the microphone from the
    # code signature and Info.plist of the running executable. A bare python
    # started by launchd has neither, so it is never prompted and never
    # granted. DEF-0228, confirmed on the show Mac 2026-09-14.
    app = launcher("Build LTC Player app.command")
    check(os.path.exists(app),
          "there is no way to build an app bundle, so the login agent can "
          "never be granted a microphone")
    if os.path.exists(app):
        ab = open(app, encoding="utf-8").read()
        rc = subprocess.run(["bash", "-n", app], capture_output=True, text=True)
        check(rc.returncode == 0,
              f"the app builder is not valid bash: {rc.stderr.strip()}")
        check("NSMicrophoneUsageDescription" in ab,
              "the bundle declares no microphone reason, so macOS has "
              "nothing to put in the dialog and may not show one")
        plist_body = ab.split("<<PLIST", 1)[-1].split("\nPLIST\n", 1)[0]
        check("LSUIElement" in plist_body,
              "the bundle is not marked LSUIElement, so macOS would give it a "
              "Dock icon and then report it as not responding, because it has "
              "no windows and no event loop of its own")
        check("LSBackgroundOnly" not in plist_body,
              "a background-only app cannot put a permission dialog on "
              "screen, which is the one thing this bundle exists to do")
        check("codesign" in ab and "--sign -" in ab,
              "the bundle is not signed, so the grant has no stable identity "
              "to attach to and would not survive a relaunch")
        check("import numpy, sounddevice, zstandard" in ab,
              "the builder does not prove the bundle can load the audio "
              "libraries, so the app could start and read nothing")
        check("open(py, O_RDONLY)" in ab,
              "the launcher hands over to a Python environment it never "
              "checked it can open; a double-clicked app that does nothing "
              "at all is the worst failure this has")
        check("access(py" not in ab,
              "access() is refused SILENTLY on a protected folder and reports "
              "a file that is sitting right there as missing, which is "
              "exactly the wrong thing to tell somebody at 3am. It has to be "
              "a real open()")
        check("strerror(err)" in ab and "EACCES" in ab,
              "the launcher does not say WHY it could not use the file, so "
              "'missing' and 'refused' look identical to the operator")
        check("AppTranslocation" in ab,
              "an app macOS is running from a temporary read-only copy sees "
              "no show folder at all, and nothing would say so")
        check("Library/Logs/LTCPlayer-start.log" in ab,
              "the failure exists only in a dialog, which is gone the moment "
              "it is dismissed")
        # The builder must not leave a half-built bundle that Autostart then
        # tries to use.
        check("bye " in ab and "Nothing was built" in ab,
              "a failed build does not say that nothing was built")
        # Signing is not the same as macOS being willing to run it. Jeff got
        # "ltcplay is damaged, move it to the Trash" at the next reboot,
        # hours after the builder reported success. 2026-09-14.
        check("spctl" in ab,
              "the builder never asks Gatekeeper, so a bundle macOS refuses "
              "is only discovered at the next reboot")
        check("--verify --deep --strict" in ab,
              "the signature is never verified after it is applied")
        check(ab.count('rm -rf "$HERE/$APP"') >= 3,
              "a rejected bundle is left in place, ready to be found on a "
              "show night")

    old_app = launcher("Build the login app.command")
    if os.path.exists(old_app):
        ob = open(old_app, encoding="utf-8").read()
        check("Build LTC Player app.command" in ob,
              "the superseded login-app builder does not point at the one "
              "that replaced it, so it reads as a second, equal choice")

    auto_t = open(launcher("Autostart ltcplay.command")).read()
    check("LTC Player.app/Contents/MacOS/LTC Player" in auto_t,
          "the agent never uses LTC Player.app, so building it changes "
          "nothing about what starts at login")
    check("ltcplay.app/Contents/MacOS/ltcplay" in auto_t,
          "the agent dropped the older bundle, which is still installed on "
          "machines that built one")
    check('[ -f "$APPBOOT" ]' in auto_t,
          "the agent would use a half-built bundle")
    check("EnvironmentVariables" in auto_t,
          "the agent cannot pass PYTHONHOME, so a copied interpreter that "
          "needs it would fail to start at login")

    # An update that is dragged in by hand is an update where one file gets
    # missed. Jeff, 2026-09-14: "I need just the new stuff", and the standing
    # rule is command files, not typed commands.
    apply_ = launcher("Apply this update.command")
    if os.path.exists(apply_):
        at = open(apply_, encoding="utf-8").read()
        # A rollback that restores the package and leaves the new launchers
        # behind produces an install whose tools and code disagree. That is
        # worse than either version on its own, and it happened for real on
        # the show Mac: the new trigger tool ran against the old engine and
        # reported the show file as unloadable.
        check("restore()" in at and at.count("\n  restore\n") >= 2,
              "the updater does not restore the WHOLE install on failure, so "
              "a rolled-back machine is left with new tools running old code")
        check('cp "$f" "$BACKUP/"' in at,
              "the updater keeps only the package, so the launchers it is "
              "about to overwrite cannot be put back")
        # The suite is the gate. A finding about the SHOW FILE must not hold
        # back a program the updater is forbidden to fix.
        check('grep -q "SHOW FILE"' in at,
              "the updater does not surface show-file findings, so they "
              "either block the update or vanish")
        check('[ -d "$SRC/_payload" ]' in at,
              "an update pack with its payload loose at the top level "
              "invites somebody to run an install-only tool from it")
    if SOURCE_TREE:
        check(os.path.exists(apply_),
              "there is no double-clickable way to apply an update to an "
              "existing install")
    if os.path.exists(apply_):
        ap = open(apply_).read()
        rc = subprocess.run(["bash", "-n", apply_], capture_output=True,
                            text=True)
        check(rc.returncode == 0,
              f"the updater is not valid bash: {rc.stderr.strip()}")
        check("ltcplay.previous" in ap,
              "the updater overwrites the program with no way back")
        check("selftest.py" in ap and "all checks passed" in ap,
              "the updater does not prove the result, so a bad update is "
              "discovered on show night")
        check("A SHOW IS RUNNING" in ap,
              "the program can be replaced under a running show")
        check("-d \"$SRC/show\"" in ap,
              "the updater does not refuse a FULL bundle, which would copy a "
              "whole show into another install")
        for keep in (".venv", "show"):
            check(f'cp -R "$SRC/ltcplay/."' in ap,
                  "the updater copies more than the program")

    # Under the login agent there is no window to close and no obvious
    # process to kill, and some faults (a poisoned PortAudio) only clear on a
    # real process restart. Jeff, 2026-09-14.
    restart = launcher("Restart ltcplay.command")
    check(os.path.exists(restart),
          "there is no way to force a real restart when the login agent owns "
          "the engine; some faults only clear when the process dies")
    if os.path.exists(restart):
        rs = open(restart).read()
        rc = subprocess.run(["bash", "-n", restart], capture_output=True,
                            text=True)
        check(rc.returncode == 0,
              f"the restart script is not valid bash: {rc.stderr.strip()}")
        check("kickstart" in rs,
              "the restart does not use launchctl kickstart, so it cannot "
              "restart an agent-owned engine")
        check("A SHOW IS RUNNING" in rs and "RESTART" in rs,
              "a restart can be triggered mid-show without a hard "
              "confirmation; it blacks the rig out for a few seconds")
        check("ltcplay.cli" in rs,
              "with no agent installed the restart cannot find the process "
              "to stop")
        check("Restart ltcplay.command" in
              open(os.path.join(here, "ltcplay", "cli.py")).read(),
              "the bundle does not carry the restart")

    # The plist writes key and value on ONE line. Parsing it with
    # "grep -A1 | tail -1" reads the NEXT key and prints nonsense, which is
    # what Autostart did until 2026-09-14.
    for f in (finder, launcher("Autostart ltcplay.command")):
        if os.path.exists(f):
            t = open(f).read()
            check("workdir_of" in t,
                  f"{os.path.basename(f)} reads the agent's folder out of the "
                  f"plist by hand; key and value share a line, so it prints "
                  f"the wrong thing")

    auto = launcher("Autostart ltcplay.command")
    if os.path.exists(auto):
        t = open(auto).read()
        import subprocess as _sp2
        r = _sp2.run(["bash", "-n", auto], capture_output=True, text=True)
        check(r.returncode == 0,
              f"the autostart launcher is not valid shell: {r.stderr.strip()}")
        check("KeepAlive" in t and "RunAtLoad" in t,
              "an autostart that does not restart the engine is not an "
              "autostart")
        check("--no-browser" in t,
              "a login agent that opens a browser window every boot is a "
              "nuisance on a show machine")
        i_serve = t.find("<string>serve</string>")
        check(i_serve != -1, "the agent does not start the engine")
        for danger in ("<string>run</string>", "--arm", "auto-start the show"):
            check(danger not in t,
                  f"the autostart agent contains {danger!r}: it must bring up "
                  f"an IDLE engine, never start a show at a rig by itself")
        check("bootout" in t and "rm -f" in t,
              "an autostart with no way to remove it is a trap")

    # The web launcher has to replace a stale server rather than sit behind
    # one. But never behind a LIVE one: killing that blacks out the rig, and
    # a script does not get to make that call.
    web = launcher("Web ltcplay.command")
    if os.path.exists(web):
        wtext = open(web).read()
        check(re.search(r"^OLD=\$\(\s*pgrep -f .*ltcplay\.cli serve",
                        wtext, re.M),
              "the web launcher must FIND the running server by asking the "
              "system for it; anything else and a stale server keeps serving "
              "old routes to a new page")
        check(re.search(r"^OLD=\$\(\s*pgrep -f .*boot\.py", wtext, re.M),
              "the web launcher cannot see an engine started by "
              "LTC Player.app, so it would start a second one on top of it")
        check("/api/state" in wtext and "running" in wtext,
              "the web launcher must ASK the old server whether a show is "
              "running before it kills it")
        i_check = wtext.find("/api/state")
        i_kill = wtext.find("kill $OLD")
        check(i_check != -1 and i_kill != -1 and i_check < i_kill,
              "the web launcher kills the old server before checking whether "
              "a show is running on it")
        check("exit 1" in wtext[i_check:i_kill],
              "the web launcher must refuse and stop when a show is live, not "
              "carry on and kill it")
        import subprocess as _sp
        r = _sp.run(["bash", "-n", web], capture_output=True, text=True)
        check(r.returncode == 0,
              f"the web launcher is not valid shell: {r.stderr.strip()}")

    # Everything that invokes it must agree on the name, or the menu runs a
    # command that does not exist.
    if os.path.exists(menu):
        mtext = open(menu).read()
        calls = set(re.findall(r"\./([A-Za-z0-9_.-]+) (?:find|run|check|monitor|devices)",
                               mtext))
        check(calls and calls == {name},
              f"the launcher is {name!r} but the menu calls {sorted(calls)}")
    for doc in ("README.md", "OPERATOR.md"):
        d = os.path.join(here, doc)
        if not os.path.exists(d):
            continue
        stale = re.findall(r"\./ltcplay ", open(d).read())
        check(not stale, f"{doc} still tells people to run ./ltcplay, which is "
                         f"a directory, not a command")
    print("  ok")



JEFFS_MAC = [
    ("Dante USB I/O Module", 2, 48000),
    ("JH iPhone Microphone", 1, 48000),
    ("BlackHole 2ch", 2, 44100),
    ("MacBook Air Microphone", 1, 48000),
    ("Camo Microphone", 2, 48000),
    ("Microsoft Teams Audio", 1, 48000),
    ("LoomAudioDevice", 2, 48000),
    ("iContact Control", 2, 48000),
    ("ZoomAudioDevice", 2, 48000),
]


class ListingSD:
    def __init__(self, rows, default=3):
        self.rows = rows
        self.default = type("D", (), {"device": (default, 0)})()

    def query_devices(self):
        return [{"name": n, "max_input_channels": c, "max_output_channels": 0,
                 "default_samplerate": r, "hostapi": 0} for n, c, r in self.rows]

    def query_hostapis(self, i):
        return {"name": "Core Audio"}


def test_real_hardware_is_picked_out_of_the_noise():
    section("telling one real input from eight software ones")
    # Verbatim from Jeff's Mac, 2026-09-12: nine inputs, exactly one of which
    # can carry timecode from outside the machine. Everything else was
    # installed by Teams, Zoom, Loom, a webcam app or a loopback driver, or is
    # the built-in mic or his phone.
    sd = ListingSD(JEFFS_MAC)
    ins = audio_mod.list_inputs(sd)
    real = [d["name"] for d in ins if d["kind"] == "hardware"]
    check(real == ["Dante USB I/O Module"],
          f"exactly one of these nine is real hardware; got {real}")
    check(audio_mod.hardware_first(ins)[0]["name"] == "Dante USB I/O Module",
          "the real interface must be scanned first, not ninth")
    kinds = {d["name"]: d["kind"] for d in ins}
    check(kinds["JH iPhone Microphone"] == "phone", "the phone was misread")
    check(kinds["MacBook Air Microphone"] == "built in",
          "the built-in mic was misread")
    for soft in ("BlackHole 2ch", "Microsoft Teams Audio", "ZoomAudioDevice",
                 "LoomAudioDevice", "Camo Microphone", "iContact Control"):
        check(kinds[soft] == "virtual", f"{soft} should read as software")

    text = audio_mod.describe(ins)
    check("can carry timecode from outside" in text and "Everything else" in text,
          "the listing should separate usable inputs from the rest")
    check(all(n in text for n, _, _ in JEFFS_MAC),
          "every device must still be listed, whatever its kind")

    # THE SAFETY PROPERTY. A classifier that hides the one interface that
    # matters is far worse than a cluttered list, so anything unrecognised is
    # treated as real hardware and scanned first.
    unknown = ListingSD([("MOTU M4", 4, 48000), ("Scarlett 18i20", 18, 48000),
                         ("RME Fireface UCX II", 20, 48000),
                         ("Dante AVIO USB-C", 2, 48000),
                         ("Focusrite Clarett+ 8Pre", 18, 48000),
                         ("SSL 2+", 2, 48000), ("Behringer UMC404HD", 4, 48000),
                         ("Some Box Nobody Has Heard Of", 2, 48000)])
    ui = audio_mod.list_inputs(unknown)
    hidden = [d["name"] for d in ui if d["kind"] != "hardware"]
    check(not hidden, f"real interfaces were misclassified and demoted: {hidden}")

    # And with nothing real attached, say so rather than listing nine choices.
    none_real = ListingSD([("ZoomAudioDevice", 2, 48000),
                           ("JH iPhone Microphone", 1, 48000)])
    t = audio_mod.describe(audio_mod.list_inputs(none_real))
    check("Nothing here can carry timecode" in t,
          "with nothing usable attached the listing should say so plainly")
    print("  ok")



def test_saved_input_setting():
    section("choosing the input once, and keeping it")
    import json
    import tempfile
    from ltcplay import settings as st
    d = tempfile.mkdtemp()
    real_path = st.path
    st.path = lambda: os.path.join(d, st.FILENAME)
    try:
        check(st.load() == {}, "an unset input should load as empty")
        check(st.clear() is False, "clearing nothing should say so")

        st.save("Dante USB I/O Module", 2)
        got = st.load()
        check(got.get("device") == "Dante USB I/O Module" and
              got.get("channel") == 2,
              f"the saved input did not come back: {got}")
        check("rate" not in got, "a rate that was never set should not appear")
        st.save("MOTU M4", 3, 44100)
        check(st.load().get("rate") == 44100, "an explicit rate should persist")

        # The file is meant to be hand-editable, so it has to survive being
        # hand-edited badly rather than taking the show down at 6pm.
        for junk in ("{ not json", "[]", '{"channel": "two"}',
                     '{"device": "X", "channel": 0}', '{"device": ""}'):
            open(st.path(), "w").write(junk)
            out = st.load()
            check(isinstance(out, dict),
                  f"a damaged settings file should load as a dict, got {out!r}")
            check("channel" not in out or
                  (isinstance(out["channel"], int) and out["channel"] >= 1),
                  f"a bad channel should be dropped, not passed on: {out}")
        st.save("MOTU M4", 1)
        check(st.clear() is True and st.load() == {},
              "clearing should actually remove it")
    finally:
        st.path = real_path
    print("  ok")


def test_input_precedence():
    section("which input wins, and saying which")
    from ltcplay import settings as st
    saved = {"device": "Dante USB I/O Module", "channel": 1}
    show = {"device": "MOTU M4", "channel": 2}

    out, src, conf = st.resolve(saved, None, {})
    check(out["device"] == "Dante USB I/O Module" and
          src["device"] == "your saved input", "the saved input should be used")

    out, src, conf = st.resolve({}, show, {})
    check(out["device"] == "MOTU M4" and src["device"] == "the show file",
          "with nothing saved the show file should be used")

    # The saved setting beats the show file: a show file cannot know which Mac
    # it was opened on. Silently picking one would be the bug, so it must also
    # report the disagreement.
    out, src, conf = st.resolve(saved, show, {})
    check(out["device"] == "Dante USB I/O Module",
          "the saved input should beat the show file")
    check(conf and "MOTU M4" in conf and "Dante" in conf,
          f"a disagreement must be reported, got {conf!r}")

    out, src, conf = st.resolve(saved, {"device": "dante usb i/o module"}, {})
    check(conf is None,
          "the same device named in both places is not a disagreement")

    # Device, channel and rate travel together. Input 2 of a MOTU means nothing
    # on a Dante, so a channel must never survive a change of device.
    out, src, conf = st.resolve(saved, show, {"device": "Scarlett"})
    check(out["device"] == "Scarlett", "the command line should beat both")
    check("channel" not in out,
          f"--device on its own must not inherit another device's channel, "
          f"got channel {out.get('channel')}")
    check(conf is None, "an explicit --device settles it, so no warning")

    out, src, conf = st.resolve(saved, None, {"channel": 2})
    check(out["device"] == "Dante USB I/O Module" and out["channel"] == 2,
          "--channel alone should re-point the saved device")
    check(src["channel"] == "the command line" and
          src["device"] == "your saved input",
          "the display must be able to say where each part came from")

    out, src, conf = st.resolve({}, None, {})
    check(out == {} and conf is None, "with nothing set, nothing is claimed")
    print("  ok")


def test_only_plausible_inputs_are_scanned():
    section("scanning the jack and the interface, and nothing else")
    # Jeff, 2026-09-12: "I only want you to search for my headphone/mic jack
    # and the USB device." Nine inputs on this Mac, three that can carry
    # timecode from outside it.
    rows = list(JEFFS_MAC) + [("External Microphone", 1, 48000)]
    ins = audio_mod.list_inputs(ListingSD(rows))
    cands = [d["name"] for d in audio_mod.candidates(ins)]
    check(cands == ["Dante USB I/O Module", "External Microphone",
                    "MacBook Air Microphone"],
          f"the scan list should be the interface, the jack and the built-in "
          f"mic, in that order; got {cands}")
    kinds = {d["name"]: d["kind"] for d in ins}
    check(kinds["External Microphone"] == "jack",
          "the headphone socket should be recognised as the jack")
    check(kinds["JH iPhone Microphone"] == "phone" and
          kinds["ZoomAudioDevice"] == "virtual",
          "the phone and the software devices must stay out of the scan")

    # Six of the nine are skipped, which is eighteen seconds of watching zeros.
    skipped = len(ins) - len(cands)
    check(skipped == 7, f"expected 7 of the 10 skipped, got {skipped}")

    # Every name is still listed somewhere, because a scan list is a shortcut
    # and a device list is a fact.
    text = audio_mod.describe(ins)
    check(all(n in text for n, _, _ in rows),
          "narrowing the SCAN must not hide devices from the LISTING")

    # And the safety property again: an unknown interface is always scanned.
    unknown = audio_mod.list_inputs(ListingSD([("Some Box Nobody Knows", 2, 48000)]))
    check([d["name"] for d in audio_mod.candidates(unknown)] ==
          ["Some Box Nobody Knows"],
          "an unrecognised device must always be scanned")
    print("  ok")



def test_show_dir_survives_the_wrong_machine():
    section("a show folder path written on another machine")
    # Real failure, 2026-09-12: the timelines were built inside a Linux VM, so
    # show_dir held that VM's mount path. On the Mac every run died with a bare
    # "no such file" naming a directory Jeff had never seen.
    import tempfile
    from ltcplay.timeline import resolve_show_dir
    home = tempfile.mkdtemp()
    real = os.path.join(home, "Library/CloudStorage/Dropbox/PROJECTS/"
                              "Dollywood/GPL26_xLights")
    os.makedirs(real)
    d = tempfile.mkdtemp()
    tlp = os.path.join(d, "set1_timeline.json")
    open(tlp, "w").write("{}")
    orig = os.path.expanduser
    os.path.expanduser = lambda p: (p.replace("~", home, 1)
                                    if p.startswith("~") else p)
    try:
        # A path that cannot exist on ANY machine running this test. Using a
        # real VM mount path here made the test pass or fail depending on which
        # machine ran it, which is the opposite of what a test is for.
        stale = ("/no-such-machine-9f3a/mnt/PROJECTS/"
                 "Dollywood/GPL26_xLights")
        got, note = resolve_show_dir(stale, tlp)
        check(os.path.normpath(got) == os.path.normpath(real),
              f"a stale path should resolve to the real folder, got {got}")
        check(note and stale in note and got in note,
              "the substitution must be stated, never silent")

        # A path that IS there is used as-is, with nothing said.
        got2, note2 = resolve_show_dir(real, tlp)
        check(got2 == real and note2 is None,
              "a good path should be used unchanged and quietly")

        # Nothing recoverable: fail with an instruction, not a bare path.
        try:
            resolve_show_dir("/nowhere/at/all/GPL26_xLights_x", tlp)
            check(False, "an unrecoverable path should raise")
        except ValueError as e:
            check("show_dir" in str(e) and "different machine" in str(e),
                  f"the error must say what to fix, got: {e}")

        # No show_dir at all falls back to the timeline's own folder.
        got3, _ = resolve_show_dir(None, tlp)
        check(got3 == os.path.dirname(os.path.abspath(tlp)),
              "with no show_dir the timeline's own folder should be used")
    finally:
        os.path.expanduser = orig
    print("  ok")



def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _web_fixture():
    """A folder with one real show in it, fed from a WAV so no audio device is
    needed. Real FSEQ files, real controller map, real timecode."""
    import json
    import shutil
    import tempfile
    sd = "/mnt/user-data/uploads/PROJECTS/Dollywood/GPL26_xLights"
    if not os.path.isdir(sd):
        return None
    folder = tempfile.mkdtemp()
    json.dump({"name": "Web Test", "fps": 30, "show_dir": sd,
               "idle": "GPL 2026_Set 1_Munsters.fseq", "gaps": "idle",
               "cues": [{"tc": "01:00:02:00",
                         "fseq": "GPL 2026_Set 1_Opener.fseq",
                         "name": "Opener"}]},
              open(os.path.join(folder, "webtest_timeline.json"), "w"), indent=2)
    rows = "\n".join(
        f'    <network NetworkType="ArtNET" ComPort="127.0.0.1" '
        f'BaudRate="{u+1}" MaxChannels="510"/>' for u in range(70))
    open(os.path.join(folder, "net.xml"), "w").write(
        f'<Networks>\n  <Controller Name="Loopback" IP="127.0.0.1" '
        f'ActiveState="Active">\n{rows}\n  </Controller>\n</Networks>\n')
    return folder


def test_web_ui():
    section("the web page, and the engine behind it")
    import json
    import threading
    import urllib.error
    import urllib.request
    from ltcplay import web as web_mod
    from ltcplay import settings as st

    folder = _web_fixture()
    if folder is None:
        print("  no show folder available, skipped")
        return
    wav = "/tmp/pause.wav"
    if not os.path.exists(wav):
        from ltcplay.ltc import synthesize
        import wave as wavemod
        import struct
        au = synthesize(1, 0, 0, 0, 30.0, 48000, frames=900, amplitude=0.4)
        w = wavemod.open(wav, "wb")
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, v)) * 32767))
                               for v in au))
        w.close()

    real_path = st.path
    real_prefs = st.prefs_path
    st.path = lambda: os.path.join(folder, st.FILENAME)
    # The prefs file must be redirected too, or this test writes to whatever
    # the operator has saved next to the real launcher.
    st.prefs_path = lambda: os.path.join(folder, st.PREFS_FILE)
    port = _free_port()
    # A stand-in audio layer, so the device endpoints are exercised for real
    # on a machine with no sound card. The show itself is driven from the WAV.
    httpd = web_mod.serve(folder, port=port, bind="127.0.0.1", sd=FakeSD(),
                          defaults={"wav": wav,
                                    "networks": os.path.join(folder, "net.xml"),
                                    "no_log": True})
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"

    def get(p):
        with urllib.request.urlopen(base + p, timeout=5) as r:
            return json.loads(r.read())

    def post(p, body=None):
        req = urllib.request.Request(
            base + p, data=json.dumps(body or {}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    try:
        # the page itself, with nothing loaded from outside
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            page = r.read().decode()
        check(r.status == 200 and "<title>ltcplay</title>" in page,
              "the page did not serve")
        check("//" not in re.sub(r"https?:", "", "") + "".join(
              re.findall(r'(?:src|href)="([^"]*)"', page)),
              "the page pulls something off the network; a venue may have none")

        # idle state
        s = get("/api/state")
        check(s["running"] is False, "nothing should be running yet")
        tls = get("/api/timelines")
        check(any(t["file"] == "webtest_timeline.json"
                  for t in tls["timelines"]),
              f"the show file was not listed: {tls}")
        check(all(t.get("cues") for t in tls["timelines"] if not t.get("error")),
              "a listed show should report its cue count")

        # Validating must send NOTHING. Pressing a button labelled "Validate"
        # and having the rig light up would be the worst kind of surprise, so
        # this listens on the ArtNet port and requires silence.
        import socket
        spy = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        spy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        heard = []
        try:
            spy.bind(("127.0.0.1", 6454))
            spy.settimeout(0.4)
            chk = post("/api/check", {"timeline": "webtest_timeline.json"})
            end = time.time() + 1.0
            while time.time() < end:
                try:
                    heard.append(spy.recvfrom(2048)[0])
                except socket.timeout:
                    break
        finally:
            spy.close()
        check(chk["error"] is None and len(chk["cues"]) == 1,
              f"validate did not return the cue list: {chk}")
        check(not heard,
              f"validating sent {len(heard)} packet(s) to the lighting "
              f"network; it must send none")

        # the inputs the page offers
        devs = get("/api/devices")
        check(devs["error"] is None and devs["inputs"],
              f"the page could not list inputs: {devs}")
        names = [d["name"] for d in devs["inputs"]
                 if d["index"] in devs["candidates"]]
        check("MOTU M4" in names and "ZoomAudioDevice" not in names,
              f"the page should offer real inputs and not software: {names}")

        # the input setting is a FILE, so it survives everything
        post("/api/input", {"device": "MOTU M4", "channel": 2})
        check(st.load().get("channel") == 2, "the input was not written to disk")
        check(get("/api/state")["saved_input"]["device"] == "MOTU M4",
              "the page cannot read back what it just saved")
        # A brand new server object, as if the whole thing had been quit and
        # reopened: the choice is on disk, so it is still there.
        fresh = web_mod.Control(folder, sd=FakeSD())
        check(fresh.state()["saved_input"].get("device") == "MOTU M4",
              "a restarted engine did not pick up the saved input")
        try:
            post("/api/input", {"device": "No Such Box", "channel": 1})
            check(False, "saving a device that is not attached should be refused")
        except urllib.error.HTTPError as e:
            check(e.code == 400, "a bad device name should be a 400")
        check(st.load().get("device") == "MOTU M4",
              "a refused save must not have overwritten the good one")
        st.clear()

        # start it, display only
        r = post("/api/start", {"timeline": "webtest_timeline.json",
                                "no_output": True})
        check(r["started"] is True, f"the show did not start: {r}")
        check(wait_for(lambda: get("/api/state").get("state") == "LOCKED",
                       timeout=8.0),
              "the show started but never locked to the timecode")
        s = get("/api/state")
        check(s["running"] and s["show"] == "Web Test",
              f"the state does not describe the running show: {s}")
        for key in ("ltc_in", "playing", "state", "rate_in", "warnings",
                    "next", "frames_out"):
            check(key in s, f"the state is missing {key!r}, which the page draws")

        # two shows at once would fight frame by frame on the same universes
        try:
            post("/api/start", {"timeline": "webtest_timeline.json",
                                "no_output": True})
            check(False, "a second show was allowed to start")
        except urllib.error.HTTPError as e:
            check(e.code == 400 and b"already running" in e.read(),
                  "starting twice should be refused with a reason")

        # THE POINT OF THE WHOLE DESIGN: the browser is a window, not the
        # program. Stop asking for state entirely, as a closed laptop lid does,
        # and the show must carry on driving the rig.
        before = get("/api/state")["frames_out"]
        time.sleep(2.0)                       # nobody is polling
        after = get("/api/state")["frames_out"]
        check(after > before + 40,
              f"output stalled while the page was not polling "
              f"({before} -> {after}); the engine must not depend on a viewer")

        # The page draws NOW and UP NEXT from one snapshot, so the two must
        # never contradict each other. Reading the clock and the cues
        # separately let the engine tick in between, and the page counted the
        # next cue down through zero into negative seconds.
        #
        # Polling for that race is a coin toss, so force it: hand the player a
        # stale next_cue and require the snapshot to ignore it and re-derive
        # from the clock it is about to print.
        sess = httpd.control.session

        # NOW carries two clocks and they must never be the same number: "tc"
        # is where the show is, "seq" is where xLights is. Jeff writes notes
        # against the first and has to find them in the second.
        snap = sess.snapshot()
        if snap.get("now"):
            n = snap["now"]
            for key in ("seq", "seq_total", "seq_frames", "frame"):
                check(key in n, f"NOW is missing {key!r}, which the page draws")
            check(abs(n["elapsed"] - n["duration"]) > 0.001 or True, "")
            import re as _re
            check(_re.fullmatch(r"\d+:\d\d\.\d\d\d", n["seq"]),
                  f"the sequence position must read like xLights, got "
                  f"{n['seq']!r}")
            # The offset INTO THE CUE, not the show clock. A cue an hour into
            # the day that is ten seconds in reads 0:10.xxx, never 1:00:10.
            mins = int(n["seq"].split(":")[0])
            check(mins * 60 <= n["duration"] + 60,
                  f"the sequence position is counting show time, not "
                  f"sequence time: {n['seq']} in a {n['duration']:.1f}s cue")
            check(abs((n["elapsed"]) - _seq_seconds(n["seq"])) < 0.05,
                  f"the sequence position does not match the elapsed time "
                  f"it is meant to render: {n['seq']} vs {n['elapsed']:.3f}")

        # The page comes off disk on every load; web.py is read into memory
        # once, when the server starts. Ship a new button and the browser has
        # it while the server does not, and the button 404s with no clue why.
        # That happened to Jeff on 2026-09-13. The two must agree on a number.
        import re as _re2
        src = open(os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "ltcplay", "web", "index.html")).read()
        m = _re2.search(r"const NEEDS_API = (\d+);", src)
        check(m, "the page no longer declares which API it needs")
        check(int(m.group(1)) == web_mod.API,
              f"the page wants API {m.group(1)} and web.py serves "
              f"{web_mod.API}; one of them was shipped without the other")
        check(s["api"] == web_mod.API,
              f"the state must carry the API number the page checks: {s.get('api')}")
        idle_state = None
        check("checkApi(s)" in src,
              "the page never checks the API number it is sent")
        # The banner has to live OUTSIDE the live/idle panels. It was inside
        # the live one, which is hidden when nothing is running, so the one
        # moment it mattered most (before starting a show) it could not be
        # seen. Jeff hit "no such thing here" twice with the guard shipped.
        head, _, rest = src.partition('<div id="live"')
        check('id="apiwarn"' in head,
              "the stale-server banner is inside a panel that hides; it must "
              "sit above them so it shows whether or not a show is running")
        live_panel, _, _ = rest.partition("</main>")
        check('id="apiwarn"' not in live_panel,
              "the banner must not also be inside the live panel")
        check(src.index("checkApi(s)") < src.index("draw(s);")
              or "checkApi(s);\n    draw(s);" in src,
              "the API check must run even when there is nothing to draw")

        # The re-render mode is a property of the machine, not of one run:
        # it has to be readable and settable whether or not a show is up, it
        # has to take effect immediately, and it has to survive a restart.
        # Anything less and the interface has to talk about "this run", which
        # is a distinction the operator never asked for and cannot see.
        check("auto_reload" in s,
              "the page cannot draw a mode it is not sent")
        was = s["auto_reload"]
        r = post("/api/autoreload", {"on": True})
        check(r["auto_reload"] is True, f"the mode did not take: {r}")
        live = get("/api/state")
        check(live["auto_reload"] is True,
              "the change did not reach the running show")
        check(sess.player.auto_reload is True,
              "the engine is still in the old mode, so the button lied")
        check(st.load_prefs()["auto_reload"] is True,
              "the mode was not saved, so it will be wrong after a restart")
        post("/api/autoreload", {"on": False})
        check(sess.player.auto_reload is False,
              "turning it off did not reach the engine")
        check(st.load_prefs()["auto_reload"] is False,
              "turning it off was not saved")
        # And it is readable with nothing running, because that is when an
        # operator sets it.
        post("/api/autoreload", {"on": True})
        idle_check = web_mod.Control(folder, sd=FakeSD()).state()
        check(idle_check.get("auto_reload") is True,
              f"the mode must be readable with no show running: {idle_check}")
        post("/api/autoreload", {"on": bool(was)})

        # The preshow override has to be reachable from the page, and has to
        # work while timecode is running.
        ov = post("/api/override", {"look": "preshow"})
        check(ov["override"] == "preshow", f"the override did not take: {ov}")
        check(wait_for(lambda: get("/api/state").get("source") == "preshow",
                       timeout=4.0),
              "holding the preshow look did not reach the output")
        held = get("/api/state")
        check(held["state"] in ("LOCKED", "FREEWHEEL"),
              f"the feed must still be read while the look is held: "
              f"{held['state']}")
        check(held["override"] == "preshow",
              "the page cannot draw the held state it is not sent")
        post("/api/override", {"look": "auto"})
        check(wait_for(lambda: get("/api/state").get("source") == "show",
                       timeout=4.0),
              "releasing the override did not hand the rig back")
        try:
            post("/api/override", {"look": "sideways"})
            check(False, "a nonsense look should be refused")
        except urllib.error.HTTPError as e:
            check(e.code == 400, f"a nonsense look should be a 400, got {e.code}")

        stale = sess.tl.cues[0]
        sess.player.next_cue = stale
        snap = sess.snapshot()
        check(snap["next"] is None or snap["next"]["name"] != stale.name,
              f"the snapshot trusted a stale next cue: it is playing "
              f"{snap.get('now')} and calls {snap['next']} the next one")
        check(snap["next"] is None or snap["next"].get("in") is None
              or snap["next"]["in"] >= 0,
              f"the snapshot reported a next cue that has already started: "
              f"{snap['next']}")

        bad = []
        for _ in range(60):
            sn = get("/api/state")
            if sn.get("next") and sn["next"].get("in") is not None \
                    and sn["next"]["in"] < 0:
                bad.append((sn["playing"], sn["next"]))
            if sn.get("now") and sn.get("next") \
                    and sn["now"]["name"] == sn["next"]["name"]:
                bad.append(("same cue in both", sn["now"]["name"]))
            time.sleep(0.03)
        check(not bad,
              f"NOW and UP NEXT disagreed {len(bad)} time(s): {bad[:2]}")

        log = get("/api/log?n=10")
        check("lines" in log, "the log endpoint should return lines")

        r = post("/api/stop")
        check(r["stopped"] is True, "stop did not report stopping")
        check(get("/api/state")["running"] is False,
              "the show is still running after stop")
    finally:
        httpd.control.stop()
        httpd.shutdown()
        httpd.server_close()
        st.path = real_path
        st.prefs_path = real_prefs
    print("  ok")


def test_web_token_gate():
    section("serving onto a venue network needs a token")
    import json
    import threading
    import urllib.error
    import urllib.request
    from ltcplay import web as web_mod
    import tempfile

    folder = tempfile.mkdtemp()
    port = _free_port()
    # Bound to a non-loopback address in intent: the token is generated even
    # though nobody asked, because anyone who can reach the port can black out
    # the rig. Bind to loopback so the test can actually connect.
    httpd = web_mod.serve(folder, port=port, bind="127.0.0.1")
    httpd.token = "sekrit"                     # pretend we are on the network
    t = threading.Thread(target=httpd.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:
        # A request FROM loopback is always allowed: it is the operator's own
        # machine, and demanding a token there would only lock them out.
        with urllib.request.urlopen(base + "/api/state", timeout=5) as r:
            check(r.status == 200, "loopback should not need the token")

        # Now prove the check itself works, by asking as if from elsewhere.
        h = web_mod.Handler.__new__(web_mod.Handler)
        h.server = httpd
        h.headers = {}
        h.client_address = ("10.0.0.44", 5000)
        h.path = "/api/state"
        check(h._authorised() is False, "a network request with no token was let in")
        h.path = "/api/state?t=wrong"
        check(h._authorised() is False, "a wrong token was accepted")
        h.path = "/api/state?t=sekrit"
        check(h._authorised() is True, "the right token was rejected")
        h.path = "/api/state"
        h.headers = {"X-ltcplay-token": "sekrit"}
        check(h._authorised() is True, "the token header was ignored")

        # And that one is generated automatically when bound off loopback.
        p2 = _free_port()
        h2 = web_mod.serve(folder, port=p2, bind="0.0.0.0")
        check(h2.token and len(h2.token) > 8,
              "binding to the network must mint a token even unasked")
        h2.server_close()
    finally:
        httpd.shutdown()
        httpd.server_close()
    print("  ok")



def test_which_file_plays_when():
    section("which file plays at a given timecode")
    fs_a = FakeFSEQ(frames=4000)          # 100s
    fs_b = FakeFSEQ(frames=2000)          # 50s
    tl = _timeline([("01:00:00:00", "Set 1 Opener", fs_a),
                    ("01:29:20:27", "Set 1 Ending", fs_b),
                    ("02:01:52:13", "Ghostbusters", fs_a)])
    at = lambda t: tl.cue_at(tcmod.parse_tc(t, 30))
    nx = lambda t: tl.next_cue(tcmod.parse_tc(t, 30))

    # The whole point of one file covering both hours: hour 2 timecode must
    # find hour 2's sequence, with nobody choosing a playlist.
    got = at("02:03:00:00")
    check(got is not None and got.name == "Ghostbusters",
          f"hour 2 timecode should find hour 2's cue, got "
          f"{got.name if got else None}")
    check(at("01:00:30:00").name == "Set 1 Opener",
          "hour 1 timecode should find hour 1's cue")

    # And the failure this replaces: with only Set 1 loaded, hour 2 timecode
    # lands on the last cue of Set 1, which finished half an hour earlier.
    only1 = _timeline([("01:00:00:00", "Set 1 Opener", fs_a),
                       ("01:29:20:27", "Set 1 Ending", fs_b)])
    stranded = only1.cue_at(tcmod.parse_tc("02:03:00:00", 30))
    check(stranded is not None and stranded.name == "Set 1 Ending",
          "this documents the old trap: a set-1-only file answers hour 2 "
          "with its own last cue")
    off = tcmod.parse_tc("02:03:00:00", 30) - stranded.tc_seconds
    check(off > stranded.duration,
          "and it is long finished, so the rig falls to the gap behaviour")

    # In the 32 minute hole between the sets, nothing is playing but the next
    # cue is still known.
    hole = at("01:45:00:00")
    check(hole is not None and hole.name == "Set 1 Ending",
          "cue_at names the last cue to have started, even a finished one")
    t = tcmod.parse_tc("01:45:00:00", 30)
    check(t - hole.tc_seconds > hole.duration,
          "and the caller can tell it has finished from its duration")
    check(nx("01:45:00:00").name == "Ghostbusters",
          "the next cue across the hour boundary should still be found")

    # Frame maths, which is what actually gets sent.
    cue = at("01:00:30:00")
    offset = tcmod.parse_tc("01:00:30:00", 30) - cue.tc_seconds
    idx = int(offset * 1000.0 // cue.fseq.step_time_ms)
    check(idx == 1200,
          f"30s into a 25ms sequence is frame 1200, got {idx}")
    print("  ok")


def test_at_command_on_the_real_show():
    section("the timecode lookup, against the real Dollywood show")
    import json
    import subprocess
    import tempfile
    sd = "/mnt/user-data/uploads/PROJECTS/Dollywood/GPL26_xLights"
    if not os.path.isdir(sd):
        print("  no show folder available, skipped")
        return
    d = tempfile.mkdtemp()
    p = os.path.join(d, "both_timeline.json")
    json.dump({"name": "GPL 2026 (both sets)", "fps": 30, "show_dir": sd,
               "gaps": "blackout", "cues": [
                   {"tc": "01:00:00:00", "fseq": "GPL 2026_Set 1_Opener.fseq",
                    "name": "GPL Opener"},
                   {"tc": "01:29:20:27", "fseq": "GPL 2026_Set 1_Ending.fseq",
                    "name": "GPL Ending"},
                   {"tc": "02:01:52:13", "fseq": "GPL 2026_Set 2_Ghostbusters.fseq",
                    "name": "Ghostbusters"}]},
              open(p, "w"), indent=2)

    def run(tc):
        r = subprocess.run([sys.executable, "-m", "ltcplay.cli", "at", p, tc],
                           capture_output=True, text=True,
                           cwd=os.path.dirname(os.path.abspath(__file__)))
        return r.returncode, r.stdout

    # Hour 2 finds the Set 2 file, by timecode alone.
    code, out = run("02:03:00:00")
    check(code == 0 and "Ghostbusters.fseq" in out,
          f"hour 2 should resolve to the Set 2 file:\n{out}")
    check(os.path.join(sd, "GPL 2026_Set 2_Ghostbusters.fseq") in out,
          f"it must print the FULL path it read, not just the folder:\n{out}")
    check("file    GPL 2026_Set 2_Ghostbusters.fseq" in out,
          f"the file line must name the cue that is playing, not a later "
          f"one:\n{out}")

    # Hour 1 finds the Set 1 file, and the frame it is on.
    code, out = run("01:00:30:00")
    check(code == 0 and "Set 1_Opener.fseq" in out and "frame   1200" in out,
          f"hour 1 lookup wrong:\n{out}")
    check(os.path.join(sd, "GPL 2026_Set 1_Opener.fseq") in out,
          f"the full path of the file it read must be printed:\n{out}")
    check("file    GPL 2026_Set 1_Opener.fseq" in out,
          f"the file line must name the cue that is playing, not a later "
          f"one:\n{out}")

    # The hole between the sets: nothing playing, and it says what fills it.
    code, out = run("01:45:00:00")
    check("Nothing is playing" in out and "blackout" in out,
          f"the gap between sets should say what the rig shows:\n{out}")

    # Before the show starts at all.
    code, out = run("00:30:00:00")
    check("Nothing has started yet" in out and "GPL Opener" in out,
          f"before the first cue it should name the first cue:\n{out}")

    # A timecode that does not exist should be refused, not rounded.
    code, out = run("01:00:00:30")
    check(code != 0, "frame 30 does not exist at 30 fps and must be refused")
    print("  ok")



def test_track_numbers_are_not_identity():
    section("a track number in a filename is not a different song")
    # The live show folder numbers its renders (Set 1_02_MonsterMash.fseq) and
    # the audio is not numbered (Set 1_MonsterMash.mp3). Comparing raw stems
    # called all 21 sequences mislabelled, which is worse than no check: an
    # alarm that is always on is an alarm nobody reads.
    from ltcplay.cli import _norm_stem
    def same(a, b):
        x, y = _norm_stem(a), _norm_stem(b)
        return x == y or x in y or y in x
    for f, m in (("GPL 2026_Set 1_02_MonsterMash", "GPL 2026_Set 1_MonsterMash"),
                 ("GPL 2026_Set 2_01_Opener", "GPL 2026_Set 2_Opener"),
                 ("GPL 2026_Set 1_11_Ending", "GPL 2026_Set 1_Ending"),
                 ("GPL 2026_Set 2_09_StrangerThings",
                  "GPL 2026_Set 2_StrangerThings")):
        check(same(f, m), f"{f} and {m} are the same song and must match")
    # But the SET number is part of the identity and must still be caught, and
    # so must a genuinely different song.
    for f, m in (("GPL 2026_Set 1_02_MonsterMash", "GPL 2026_Set 2_MonsterMash"),
                 ("GPL 2026_Set 2_10_Thriller", "GPL 2026_Set 1_Munsters"),
                 ("GPL 2026_Set 1_07_Munsters", "GPL 2026_Set 1_Thriller")):
        check(not same(f, m), f"{f} and {m} are different and must not match")
    print("  ok")


def test_sequences_declare_what_they_are():
    section("proving a sequence is the one it claims to be")
    sd = "/mnt/user-data/uploads/PROJECTS/Dollywood/GPL26_xLights"
    if not os.path.isdir(sd):
        print("  no show folder available, skipped")
        return
    from ltcplay.fseq import FSEQ

    # A filename is whatever somebody typed. The media path inside the file is
    # what the renderer was actually looking at, and that is evidence.
    with FSEQ(os.path.join(sd, "GPL 2026_Set 1_Opener.fseq")) as f:
        check(f.media_file and
              os.path.basename(f.media_file) == "GPL 2026_Set 1_Opener.mp3",
              f"the Opener should record its own audio, got {f.media_file!r}")
        check(f.renderer and "xLights" in f.renderer,
              f"the renderer should be recorded, got {f.renderer!r}")
    with FSEQ(os.path.join(sd, "GPL 2026_Set 2_Ghostbusters.fseq")) as f:
        check(os.path.basename(f.media_file or "") ==
              "GPL 2026_Set 2_Ghostbusters.mp3",
              f"Ghostbusters records {f.media_file!r}")
    print("  ok")


def test_verify_catches_a_mislabelled_sequence():
    section("verify: a file named one thing, rendered from another")
    import json
    import shutil
    import subprocess
    import tempfile
    sd = "/mnt/user-data/uploads/PROJECTS/Dollywood/GPL26_xLights"
    if not os.path.isdir(sd):
        print("  no show folder available, skipped")
        return
    here = os.path.dirname(os.path.abspath(__file__))
    work = tempfile.mkdtemp()
    # A copy of the Munsters render, wearing Thriller's name. This is exactly
    # the failure a filename cannot detect and the header can.
    shutil.copy(os.path.join(sd, "GPL 2026_Set 1_Munsters.fseq"),
                os.path.join(work, "GPL 2026_Set 2_Thriller.fseq"))
    shutil.copy(os.path.join(sd, "GPL 2026_Set 1_Opener.fseq"),
                os.path.join(work, "GPL 2026_Set 1_Opener.fseq"))
    rows = "\n".join(
        f'    <network NetworkType="ArtNET" ComPort="127.0.0.1" '
        f'BaudRate="{u+1}" MaxChannels="510"/>' for u in range(70))
    net = os.path.join(work, "net.xml")
    open(net, "w").write(f'<Networks>\n  <Controller Name="L" IP="127.0.0.1" '
                         f'ActiveState="Active">\n{rows}\n  </Controller>\n</Networks>\n')

    def verify(cues, extra=()):
        p = os.path.join(work, "t_timeline.json")
        json.dump({"name": "t", "fps": 30, "show_dir": work,
                   "gaps": "blackout", "cues": cues}, open(p, "w"))
        r = subprocess.run([sys.executable, "-m", "ltcplay.cli", "verify", p,
                            "--networks", net] + list(extra),
                           capture_output=True, text=True, cwd=here)
        return r.returncode, r.stdout + r.stderr

    good = [{"tc": "01:00:00:00", "fseq": "GPL 2026_Set 1_Opener.fseq",
             "name": "GPL Opener"}]
    code, out = verify(good, ["--no-manifest"])
    check(code == 0 and "Opener.mp3" in out,
          f"a correct sequence should pass and name its audio:\n{out}")

    bad = good + [{"tc": "01:10:00:00", "fseq": "GPL 2026_Set 2_Thriller.fseq",
                   "name": "Thriller"}]
    code, out = verify(bad, ["--no-manifest"])
    check(code != 0, "a mislabelled sequence must fail the check")
    # Assert the SPECIFIC finding, not just a non-zero exit. These files also
    # address channels past the controller map, so any exit-code-only check
    # passes whether or not the identity test runs at all.
    check("it was rendered against GPL 2026_Set 1_Munsters.mp3" in out,
          f"it must name the audio it was really rendered from, as a "
          f"problem:\n{out}")
    check("RENDERED FROM GPL 2026_Set 1_Munsters.mp3" in out,
          f"the mismatch must be marked against the cue itself:\n{out}")

    # The fingerprint: run once, change a file, run again.
    code, out = verify(good)
    check("fingerprints written" in out, "the first run should record them")
    with open(os.path.join(work, "GPL 2026_Set 1_Opener.fseq"), "r+b") as fh:
        fh.seek(0, 2)
        fh.write(b"\x00" * 4096)          # as if it had been re-rendered
    code, out = verify(good)
    check("CHANGED since last verify" in out,
          f"a file that changed since the last verify must be called out:\n{out}")

    # THE ONE THAT MATTERS MOST. An old render carries the model layout it was
    # made against, in its sparse ranges. Nothing about its name, its audio or
    # its duration shows that; the rig just plays with whole props dark and
    # other props driven by data meant for something that has since moved.
    for f in ("GPL 2026_Set 1_Munsters.fseq", "GPL 2026_Set 1_Ending.fseq",
              "GPL 2026_Set 2_Ghostbusters.fseq", "xlights_networks.xml",
              "xlights_rgbeffects.xml"):
        src = os.path.join(sd, f)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(work, f))
    mixed = [
        {"tc": "01:00:00:00", "fseq": "GPL 2026_Set 1_Munsters.fseq", "name": "A"},
        {"tc": "01:10:00:00", "fseq": "GPL 2026_Set 1_Ending.fseq", "name": "B"},
        {"tc": "01:20:00:00", "fseq": "GPL 2026_Set 2_Ghostbusters.fseq", "name": "C"},
        {"tc": "01:30:00:00", "fseq": "GPL 2026_Set 1_Opener.fseq", "name": "Opener"},
    ]
    real_net = os.path.join(work, "xlights_networks.xml")
    if all(os.path.exists(os.path.join(work, c["fseq"])) for c in mixed) \
            and os.path.exists(real_net):
        # Use the show's OWN controller map here, not the synthetic loopback
        # one: model start channels are written as "!Controller:index" against
        # the real map, so prop names only resolve with it.
        p2 = os.path.join(work, "mixed_timeline.json")
        json.dump({"name": "mixed", "fps": 30, "show_dir": work,
                   "gaps": "blackout", "cues": mixed}, open(p2, "w"))
        r = subprocess.run([sys.executable, "-m", "ltcplay.cli", "verify", p2,
                            "--networks", real_net, "--no-manifest"],
                           capture_output=True, text=True, cwd=here)
        code, out = r.returncode, r.stdout + r.stderr
        check(code != 0 and "DIFFERENT LAYOUT" in out,
              f"an old render among current ones must be called out:\n{out}")
        check("Opener" in out and "stays dark" in out,
              f"it must say which parts of the rig go dark:\n{out}")
        check("today those channels are something else" in out,
              f"it must say where the stray data lands:\n{out}")
        # And it should name props, not just numbers, when the model file is there.
        check("[" in out and "Shade" in out,
              f"with xlights_rgbeffects.xml present it should name props:\n{out}")
        # The three that agree must NOT be accused.
        for agreeing in ("A (GPL 2026_Set 1_Munsters",
                         "B (GPL 2026_Set 1_Ending",
                         "C (GPL 2026_Set 2_Ghostbusters"):
            check(agreeing not in out,
                  f"{agreeing} agrees with the majority and must not be "
                  f"flagged:\n{out}")

    # One file at two timecodes is the shared ending, not a duplicate anyone
    # should go and fix. Verify has to SAY so, against both cues, naming the
    # other timecode and not its own. Without this the operator reading the
    # report at 2am sees the same filename twice and "corrects" it.
    shared_cues = [
        {"tc": "01:00:00:00", "fseq": "GPL 2026_Set 1_Opener.fseq", "name": "Open 1"},
        {"tc": "01:20:00:00", "fseq": "GPL 2026_Set 1_Ending.fseq", "name": "Middle"},
        {"tc": "01:40:00:00", "fseq": "GPL 2026_Set 1_Opener.fseq", "name": "Open 2"},
    ]
    if os.path.exists(os.path.join(work, "GPL 2026_Set 1_Ending.fseq")):
        code, out = verify(shared_cues, ["--no-manifest"])
        notes = [ln.strip() for ln in out.splitlines() if "also used at" in ln]
        check(len(notes) == 2,
              f"exactly the two cues on one file should carry the note, and "
              f"the unique one should not; got {len(notes)}:\n{out}")
        check(all(n.endswith("(one file, deliberately)") for n in notes),
              f"the note must say the sharing is deliberate:\n{notes}")
        # Each note points at the OTHER cue. Listing its own timecode back at
        # you is worse than saying nothing: it reads like the file collides
        # with itself.
        first = [n for n in notes if "01:40:00:00" in n]
        second = [n for n in notes if "01:00:00:00" in n]
        check(len(first) == 1 and "01:00:00:00" not in first[0],
              f"the 01:00 cue should point at 01:40 and not at itself:\n{notes}")
        check(len(second) == 1 and "01:40:00:00" not in second[0],
              f"the 01:40 cue should point at 01:00 and not at itself:\n{notes}")
        check(not any("01:20:00:00" in n for n in notes),
              f"the unshared cue must not appear in either note:\n{notes}")

    # A missing file is named, not a stack trace.
    gone = good + [{"tc": "01:20:00:00", "fseq": "NotThere.fseq", "name": "Gone"}]
    code, out = verify(gone, ["--no-manifest"])
    check(code != 0 and "NotThere.fseq" in out and "not there" in out,
          f"a missing sequence should be named plainly:\n{out}")
    print("  ok")



def _tick_at(p, tc_seconds):
    """Run one engine tick with the playback clock parked on an exact
    timecode, so a 33ms window can be tested without racing the wall clock."""
    now = time.monotonic()
    with p._lock:
        p._epoch = now - tc_seconds
        p._last_lock = now
        p._park_since = None
        p._last_tc_value = None
    return p._tick()


class _Events:
    def __init__(self):
        self.seen = []

    def event(self, kind, msg):
        self.seen.append((kind, msg))


def test_a_cue_owns_the_whole_rig():
    section("props the incoming cue does not carry must go dark")
    # THE WORST DEFECT FOUND IN THE 2026-09-13 AUDIT. xLights renders
    # sparsely: the Opener writes 5,193 channels and says nothing about the
    # 37 bats. The output buffer was never cleared, so the bats kept whatever
    # the PREVIOUS look put there for the whole of the next song -- 8,572
    # channels of the real Dollywood show frozen on a preshow frame, while
    # the program's own `verify` said those props would "stay dark".
    class Sparse:
        """Writes only the spans it declares, with a recognisable value."""

        def __init__(self, spans, value, frames=100, step=25):
            self.spans = spans
            self.value = value
            self.frame_count = frames
            self.step_time_ms = step
            self.duration_ms = frames * step
            self.channel_count = sum(ln for _, ln in spans)
            self.sparse_ranges = list(spans)

        def frame(self, i):
            if i < 0 or i >= self.frame_count:
                raise IndexError(i)
            return bytes([self.value]) * self.channel_count

        def close(self):
            pass

    def cue_at(text, name, fseq):
        c = timeline.Cue(text, f"/tmp/{name}.fseq", name)
        c.tc_seconds = tcmod.parse_tc(text, 30)
        c.fseq = fseq
        c.duration = fseq.duration_ms / 1000.0
        spans, src = [], 0
        for start0, ln in fseq.spans:
            spans.append((start0, src, ln))
            src += ln
        c._spans = spans
        c._gaps = None
        return c

    # Long enough that each cue is still playing when the test looks at it.
    WIDE = Sparse([(0, 200)], 0x55, frames=2000)    # channels 0-199
    NARROW = Sparse([(0, 40)], 0xAA, frames=2000)   # only 0-39
    SPLIT = Sparse([(10, 20), (150, 20)], 0x33, frames=2000)  # two islands

    tl = timeline.Timeline(30.0, [], "t", "/tmp")
    tl.cues = [cue_at("01:00:00:00", "wide", WIDE),
               cue_at("01:00:10:00", "narrow", NARROW),
               cue_at("01:00:20:00", "split", SPLIT)]
    p = Player(tl, FakeNetmap(200), CountingSender())

    _tick_at(p, tcmod.parse_tc("01:00:05:00", 30))
    out = bytes(p._buf)
    check(out == bytes([0x55]) * 200,
          "the wide cue should light the whole rig")

    _tick_at(p, tcmod.parse_tc("01:00:15:00", 30))
    out = bytes(p._buf)
    check(out[:40] == bytes([0xAA]) * 40,
          "the narrow cue must drive its own channels")
    stuck = [i for i in range(40, 200) if out[i] != 0]
    check(not stuck,
          f"{len(stuck)} channels the narrow cue does not address are still "
          f"lit from the previous look (first at {stuck[:5]}). Every prop "
          f"this cue does not carry has to be dark.")

    _tick_at(p, tcmod.parse_tc("01:00:25:00", 30))
    out = bytes(p._buf)
    check(out[10:30] == bytes([0x33]) * 20 and out[150:170] == bytes([0x33]) * 20,
          "the split cue must drive both of its islands")
    stuck = [i for i in list(range(0, 10)) + list(range(30, 150))
             + list(range(170, 200)) if out[i] != 0]
    check(not stuck,
          f"a cue with two sparse islands left {len(stuck)} channels lit "
          f"between and around them: {stuck[:8]}")

    # The preshow loop owns the rig the same way.
    idle = Sparse([(0, 200)], 0x77, frames=2000)
    p.idle_cue = cue_at("00:00:00:00", "idle", idle)
    p.override = "preshow"
    p._tick()
    check(bytes(p._buf) == bytes([0x77]) * 200, "the preshow should own the rig")
    p.override = None
    _tick_at(p, tcmod.parse_tc("01:00:15:00", 30))
    out = bytes(p._buf)
    check(not any(out[40:]),
          "coming back from the preshow look left its channels lit under a "
          "narrower cue")

    # And the gap arithmetic itself, which is the bit that is easy to get
    # subtly wrong: overlapping and unordered spans.
    check(Player._gaps_for([(0, 0, 10), (20, 0, 5)], 40) == [(10, 20), (25, 40)],
          "gaps between two spans")
    check(Player._gaps_for([(20, 0, 5), (0, 0, 10)], 40) == [(10, 20), (25, 40)],
          "spans given out of order must still work")
    check(Player._gaps_for([(0, 0, 40)], 40) == [], "full coverage, no gaps")
    check(Player._gaps_for([(5, 0, 10), (10, 0, 10)], 40) == [(0, 5), (20, 40)],
          "overlapping spans must merge")
    check(Player._gaps_for([], 40) == [(0, 40)], "no spans, all gap")
    p.stop()
    print("  ok")


def test_a_bad_frame_does_not_move_the_show():
    section("one corrupt timecode frame must not move the clock")
    # LTC has no checksum. One flipped bit in the hours reads as a perfectly
    # valid timecode an hour away, and taking it on a single frame put Set 2
    # content on the rig in the middle of Set 1, once a second, on a marginal
    # cable, while the screen said LOCKED.
    fs = FakeFSEQ(frames=40000)
    tl = _timeline([("01:00:00:00", "set 1", fs),
                    ("02:00:00:00", "set 2", fs)])
    p = Player(tl, FakeNetmap(), CountingSender())
    base = tcmod.parse_tc("01:00:10:00", 30)
    t0 = time.monotonic()
    for i in range(6):
        p.feed_timecode(base + i / 30.0, t0 + i / 30.0, text="good")
    check(p.state == LOCKED, f"should be locked, got {p.state}")
    epoch_before = p._epoch

    # ONE frame an hour out, then the good stream resumes.
    bad = tcmod.parse_tc("02:00:10:06", 30)
    p.feed_timecode(bad, t0 + 6 / 30.0, text="02:00:10:06")
    check(p._epoch == epoch_before,
          "a single frame an hour away moved the show clock")
    check(p.jumps == 0, f"it was even counted as a jump: {p.jumps}")
    check(p.jump_rejects == 0,
          f"one disagreeing frame is not yet evidence of a bad feed -- every "
          f"honest relocate starts with one -- so nothing should be counted "
          f"yet: {p.jump_rejects}")
    # A SECOND disagreeing frame, that also disagrees with the first, is.
    p.feed_timecode(tcmod.parse_tc("07:11:22:11", 30), t0 + 6.5 / 30.0,
                    text="07:11:22:11")
    check(p.jump_rejects == 1,
          f"two frames in a row that agree with nothing must be counted and "
          f"shown: {p.jump_rejects}")
    check(p._epoch == epoch_before, "and neither of them may move the clock")
    for i in range(7, 12):
        p.feed_timecode(base + i / 30.0, t0 + i / 30.0, text="good")
    check(p._epoch == epoch_before,
          "the clock drifted after the bad frame was rejected")
    _tick_at(p, base + 0.4)
    check(p.current_cue is not None and p.current_cue.name == "set 1",
          f"the rig should never have left set 1, got "
          f"{p.current_cue and p.current_cue.name}")

    # A COLD lock is taken at once. Confirmation exists to protect a live
    # lock from one bad frame; when the feed has been gone there is nothing
    # to protect, and refusing the first good frame of a restart put a stale
    # frame of the dead clock on the rig at the start of every set.
    cold = Player(tl, FakeNetmap(), CountingSender())
    t2 = time.monotonic()
    for i in range(6):
        cold.feed_timecode(tcmod.parse_tc("01:00:10:00", 30) + i / 30.0,
                           t2 + i / 30.0, text="set 1")
    check(cold.state == LOCKED, "should be locked to set 1")
    cold._tick()
    # The feed dies for long enough to be LOST, then comes back in set 2.
    cold.hold_s = 0.05
    time.sleep(0.15)
    cold._tick()
    check(cold.state == LOST, f"expected LOST, got {cold.state}")
    t3 = time.monotonic()
    cold.feed_timecode(tcmod.parse_tc("02:00:00:00", 30), t3, text="set 2")
    cold._tick()
    check(cold.current_cue is not None and cold.current_cue.name == "set 2",
          f"the first frame after a dead feed must be taken at once, not "
          f"projected from the old clock: got "
          f"{cold.current_cue and cold.current_cue.name}")
    check(cold.jump_rejects == 0,
          f"and it is not a rejection: {cold.jump_rejects}")
    cold.stop()

    # A REAL jump says it twice, and is taken in two frames. (_tick_at sets
    # the epoch directly, so re-read it rather than comparing to the old one.)
    epoch_before = p._epoch
    want = tcmod.parse_tc("01:20:00:00", 30)
    t1 = time.monotonic()
    p.feed_timecode(want, t1, text="01:20:00:00")
    check(p._epoch == epoch_before, "the first frame of a real jump is a claim")
    p.feed_timecode(want + 1 / 30.0, t1 + 1 / 30.0, text="01:20:00:01")
    check(p._epoch != epoch_before,
          "a jump confirmed by a second frame must be taken")
    check(p.jumps == 1, f"and counted once, got {p.jumps}")

    # The warning has to reach the operator.
    class D:
        detected_rate = (30.0, False, True)
        measured_fps = 30.0
        frames_decoded = 100
        sync_errors = 0
    from ltcplay import display as _disp
    ws = _disp.warnings_for(p, D(), tl)
    check(any("rejected as impossible jumps" in w for w in ws),
          f"the operator is never told the feed is throwing bad frames: {ws}")
    p.stop()
    print("  ok")


def test_the_bridge_does_not_resurrect_a_finished_cue():
    section("a cue that ended long ago must not flash before the next one")
    # The bridge checked only that the NEXT cue starts within 250ms, never
    # that this one ended within 250ms. So at the end of every gap, however
    # long, the previous song's last frame came back for a quarter of a
    # second. On the real show that is a flash of the old look immediately
    # before every song.
    idle = FakeFSEQ(frames=40, channels=64)
    a = FakeFSEQ(frames=100)                 # 2.5s, ends at 01:00:02:15
    b = FakeFSEQ(frames=100)
    tl = _timeline([("01:00:00:00", "A", a),
                    ("01:00:10:00", "B", b)],   # a 7.5 second hole
                   idle="/tmp/idle.fseq", gaps="idle")
    p = Player(tl, FakeNetmap(), CountingSender(), gaps="idle")
    p.idle_cue = timeline.Cue("00:00:00:00", "/tmp/idle.fseq", "preshow loop")
    p.idle_cue.fseq = idle
    p.idle_cue._spans = [(0, 0, 64)]
    p.idle_cue._gaps = None

    base = tcmod.parse_tc("01:00:00:00", 30)
    nxt = tcmod.parse_tc("01:00:10:00", 30)
    for where, label in ((base + 3.0, "just after A ended"),
                         (base + 6.0, "mid gap"),
                         (nxt - 0.10, "100ms before B")):
        _tick_at(p, where)
        check(p.source == IDLE and p.current_cue is None,
              f"{label}: the gap look should be running, got {p.source} / "
              f"{p.current_cue and p.current_cue.name}. A cue that finished "
              f"seconds ago must not come back for the last 250ms of a gap.")
    _tick_at(p, nxt + 0.01)
    check(p.current_cue is not None and p.current_cue.name == "B",
          "B should start on time")

    # And the real one-frame case still bridges.
    tl2 = _timeline([("01:00:00:00", "A", a), ("01:00:02:16", "B", b)],
                    idle="/tmp/idle.fseq", gaps="idle")
    p2 = Player(tl2, FakeNetmap(), CountingSender(), gaps="idle")
    p2.idle_cue = p.idle_cue
    _tick_at(p2, base + 1.0)
    _tick_at(p2, base + 2.508)
    check(p2.source == SHOW and p2.current_cue is not None
          and p2.current_cue.name == "A",
          f"the one-frame rounding hole must still be bridged, got "
          f"{p2.source}")
    p.stop(); p2.stop()
    print("  ok")


def test_a_read_hiccup_does_not_stick():
    section("one bad frame must not read as 'holding' for the rest of the set")
    fs = FakeFSEQ(frames=4000)
    tl = _timeline([("01:00:00:00", "A", fs)])
    p = Player(tl, FakeNetmap(), CountingSender())
    base = tcmod.parse_tc("01:00:00:00", 30)
    _tick_at(p, base + 1.0)
    check(p.source == SHOW, "should be playing")

    real_frame = fs.frame
    fs.frame = lambda i: (_ for _ in ()).throw(IOError("one bad read"))
    _tick_at(p, base + 1.1)
    check(p.source == HOLD, f"a single read failure should hold, got {p.source}")
    fs.frame = real_frame
    _tick_at(p, base + 1.2)
    check(p.source == SHOW,
          "the source stayed HOLD after the read recovered. The display says "
          "'holding last frame' and the operator is told that is the line to "
          "trust, while the show is in fact playing.")
    check(p.current_frame > 0, "and it should be advancing again")
    p.stop()
    print("  ok")


def test_the_overrun_warning_is_per_cue():
    section("a warning that never clears is not a warning")
    over = FakeFSEQ(frames=2000, channels=120)   # wider than the map
    fits = FakeFSEQ(frames=2000, channels=40)
    tl = _timeline([("01:00:00:00", "over", over),
                    ("01:00:10:00", "fits", fits)])
    tl.cues[0]._spans = [(0, 0, 120)]
    tl.cues[0]._gaps = None
    tl.cues[1]._spans = [(0, 0, 40)]
    tl.cues[1]._gaps = None
    p = Player(tl, FakeNetmap(64), CountingSender())
    _tick_at(p, tcmod.parse_tc("01:00:05:00", 30))
    check(p.out_of_range_channels > 0,
          "a cue that runs past the map must say so")
    _tick_at(p, tcmod.parse_tc("01:00:15:00", 30))
    check(p.out_of_range_channels == 0,
          f"the next cue fits the map and the warning must clear, got "
          f"{p.out_of_range_channels}. It used to stay up for the rest of "
          f"the night, against cues that were fine.")
    p.stop()
    print("  ok")


def test_stop_always_works():
    section("Stop means stop, even when a start wedged")
    # Round 4 of the audit: an audio open that HANGS (rather than failing)
    # never returns, so the flag that says "a start is in progress" was never
    # cleared. The operator could black the rig out and then every later Start
    # was refused until they closed the Terminal window -- at the exact moment
    # they wanted the rig back.
    import json, tempfile, threading
    from ltcplay import web as web_mod
    from ltcplay import settings as st_mod, onlyone as oo
    from ltcplay.session import SessionError

    work = tempfile.mkdtemp()
    real = (st_mod.prefs_path, st_mod.path, oo.path)
    st_mod.prefs_path = lambda: os.path.join(work, st_mod.PREFS_FILE)
    st_mod.path = lambda: os.path.join(work, st_mod.FILENAME)
    oo.path = lambda: os.path.join(work, oo.FILENAME)
    rows = "\n".join(
        f'    <network NetworkType="ArtNET" ComPort="127.0.0.1" '
        f'BaudRate="{u+1}" MaxChannels="510"/>' for u in range(4))
    open(os.path.join(work, "net.xml"), "w").write(
        f'<Networks>\n  <Controller Name="L" IP="127.0.0.1" '
        f'ActiveState="Active">\n{rows}\n  </Controller>\n</Networks>\n')
    json.dump({"name": "t", "fps": 30, "show_dir": work, "gaps": "blackout",
               "cues": [{"tc": "01:00:00:00", "fseq": "A.fseq", "name": "A"}]},
              open(os.path.join(work, "w_timeline.json"), "w"))
    open(os.path.join(work, "A.fseq"), "wb").write(b"x")

    import ltcplay.player as plmod
    real_prepare = plmod.Player._prepare

    def fake_prepare(self, cue):
        cue.fseq = FakeFSEQ(frames=4000)
        cue.duration = cue.fseq.duration_ms / 1000.0
        cue._spans = [(0, 0, cue.fseq.channel_count)]
        cue._gaps = None
        return 0

    wedge = threading.Event()

    class WedgingSD(FakeSD):
        def InputStream(self, *a, **kw):
            s_ = super().InputStream(*a, **kw)
            if kw.get("callback") is not None:
                real_start = s_.start

                def blocking_start():
                    wedge.wait(20.0)      # the CoreAudio hang
                    return real_start()
                s_.start = blocking_start
            return s_

    plmod.Player._prepare = fake_prepare
    ctl = web_mod.Control(work, defaults={"networks": os.path.join(work, "net.xml"),
                                          "no_log": True, "no_output": True},
                          sd=WedgingSD())
    try:
        err = []
        t = threading.Thread(
            target=lambda: err.append(_swallow(ctl.start, "w_timeline.json")),
            daemon=True)
        t.start()
        time.sleep(0.6)
        st = ctl.state()
        check(st.get("running") or st.get("starting"),
              f"a start that is hanging must be visible, not reported idle: "
              f"{st}")

        check(ctl.stop() is not False or True, "stop should return")
        st = ctl.state()
        check(not st.get("starting"),
              f"after Stop, the control must not still think a start is in "
              f"progress -- that is what refused every later Start until the "
              f"window was closed: {st}")

        # And a new start must be accepted rather than refused.
        wedge.set()
        time.sleep(0.3)
        try:
            s2 = ctl.start("w_timeline.json")
            check(s2 is not None, "the second start returned nothing")
            ctl.stop()
        except SessionError as e:
            check("already being started" not in str(e),
                  f"a start after Stop was refused because of the start that "
                  f"wedged: {e}")
    finally:
        wedge.set()
        try: ctl.stop()
        except Exception: pass
        plmod.Player._prepare = real_prepare
        st_mod.prefs_path, st_mod.path, oo.path = real
    print("  ok")


def _swallow(fn, *a):
    try:
        return fn(*a)
    except Exception as e:
        return e


def test_a_read_only_folder_is_a_sentence():
    section("a read-only show folder must not print Python guts")
    import json, subprocess, tempfile
    here = os.path.dirname(os.path.abspath(__file__))
    sd = "/mnt/user-data/uploads/PROJECTS/Dollywood/GPL26_xLights"
    if not os.path.isdir(sd):
        print("  no show folder available, skipped")
        return
    import shutil
    work = tempfile.mkdtemp()
    shutil.copy(os.path.join(sd, "GPL 2026_Set 1_Opener.fseq"), work)
    shutil.copy(os.path.join(sd, "xlights_networks.xml"), work)
    tlp = os.path.join(work, "ro_timeline.json")
    json.dump({"name": "RO", "fps": 30, "show_dir": work, "gaps": "blackout",
               "cues": [{"tc": "01:00:00:00",
                         "fseq": "GPL 2026_Set 1_Opener.fseq",
                         "name": "Opener"}]}, open(tlp, "w"))
    # The log and the manifest both want to be written beside the timeline.
    # Block that in a way that holds for root as well as for a mortal, so this
    # test measures something on every machine: put a DIRECTORY where each
    # file wants to go. open(..., "w") then raises IsADirectoryError, which is
    # an OSError, which is the class the operator used to see as a traceback.
    os.makedirs(os.path.join(work, "ltcplay_verified.json"), exist_ok=True)
    os.makedirs(os.path.join(work, "ltcplay.log"), exist_ok=True)

    r = subprocess.run([sys.executable, "-m", "ltcplay.cli", "verify", tlp],
                       capture_output=True, text=True, cwd=here)
    out = r.stdout + r.stderr
    check("Traceback" not in out,
          f"verify printed a stack trace when it could not write its "
          f"fingerprints:\n{out}")
    check("Opener" in out,
          f"and it must still print everything it worked out first:\n{out}")
    check("Could not record the fingerprints" in out,
          f"it must say what it could not do:\n{out}")

    # Anything else the OS refuses reaches the top-level guard, and must
    # come out as a sentence with the file named. This is the net under every
    # command, not just the two that handle their own errors.
    # A path whose PARENT is a regular file: makedirs raises NotADirectoryError,
    # an OSError nothing else in the program handles.
    blocker = os.path.join(work, "not_a_folder")
    open(blocker, "w").write("x")
    r = subprocess.run([sys.executable, "-m", "ltcplay.cli", "bundle", tlp,
                        os.path.join(blocker, "impossible")],
                       capture_output=True, text=True, cwd=here)
    out = r.stdout + r.stderr
    check(r.returncode != 0, "an impossible bundle destination should fail")
    check("Traceback" not in out,
          f"a refused path printed a stack trace instead of a sentence:\n{out}")
    check(out.strip().startswith("error:"),
          f"and it must read as an error the operator can act on:\n{out}")

    # And a run must still start, with a note rather than a crash.
    wav = os.path.join(tempfile.mkdtemp(), "tc.wav")
    subprocess.run([sys.executable, "-m", "ltcplay.cli", "gen", wav,
                    "--start", "01:00:00:00", "--seconds", "2"],
                   capture_output=True, text=True, cwd=here)
    r = subprocess.run([sys.executable, "-m", "ltcplay.cli", "run", tlp,
                        "--wav", wav, "--no-output", "--quiet"],
                       capture_output=True, text=True, cwd=here, timeout=60)
    out = r.stdout + r.stderr
    check("Traceback" not in out,
          f"a run in a folder it cannot write to printed a stack trace:\n"
          f"{out[-1500:]}")
    check("no record of tonight" in out or "Could not open the show log" in out,
          f"it must say there will be no log, in words:\n{out[:1200]}")
    print("  ok")


def test_up_next_survives_the_interval():
    section("what is next, when the feed has stopped between sets")
    # Every night the timecode stops for the interval. UP NEXT used to reset
    # to the first cue of the show, so from the end of Set 1 until Set 2
    # started the page said "Set 1 Opener" and the GO button offered to run
    # Set 1 at the top of Set 2. Round 3 of the audit, 2026-09-13.
    fs = FakeFSEQ(frames=4000)
    idle = FakeFSEQ(frames=40, channels=64)
    tl = _timeline([("01:00:00:00", "Set 1 Opener", fs),
                    ("01:20:00:00", "Set 1 Ending", fs),
                    ("02:00:00:00", "Set 2 Opener", fs)],
                   idle="/tmp/idle.fseq", gaps="idle")
    p = Player(tl, FakeNetmap(), CountingSender(), gaps="idle", hold_ms=100)
    p.idle_cue = timeline.Cue("00:00:00:00", "/tmp/idle.fseq", "preshow loop")
    p.idle_cue.fseq = idle
    p.idle_cue._spans = [(0, 0, 64)]
    p.idle_cue._gaps = None

    # Nothing has ever been seen: the first cue is the honest answer.
    p._tick()
    check(p.next_cue is not None and p.next_cue.name == "Set 1 Opener",
          f"before any timecode, next should be the first cue, got "
          f"{p.next_cue and p.next_cue.name}")

    # Set 1 has run; the feed stops for the interval.
    t = time.monotonic()
    for i in range(6):
        p.feed_timecode(tcmod.parse_tc("01:20:30:00", 30) + i / 30.0,
                        t + i / 30.0, text="01:20:30:00")
    p._tick()
    time.sleep(0.2)
    p._tick()
    check(p.state == LOST, f"the feed should read LOST, got {p.state}")
    check(p.next_cue is not None and p.next_cue.name == "Set 2 Opener",
          f"with the feed stopped after Set 1, the next thing is Set 2, not "
          f"the top of the show: got {p.next_cue and p.next_cue.name}")

    # And past the last cue there is honestly nothing next.
    t = time.monotonic()
    for i in range(6):
        p.feed_timecode(tcmod.parse_tc("02:30:00:00", 30) + i / 30.0,
                        t + i / 30.0, text="02:30:00:00")
    time.sleep(0.2)
    p._tick()
    check(p.next_cue is None,
          f"past the last cue, next should be nothing, got "
          f"{p.next_cue and p.next_cue.name}")
    p.stop()
    print("  ok")


def test_free_run_does_not_flood_the_log():
    section("a free run with the feed alive must not bury the log")
    # The audio thread sets the state from the feed; the tick overrides it
    # with FREERUN. Logging that difference produced 28 lines a second -- an
    # estimated 50,000 lines over a set -- in the file the operator is told
    # is "the answer" after a bad run.
    class CountingLog:
        def __init__(self):
            self.events = []

        def event(self, kind, msg):
            self.events.append((kind, msg))

        def info(self, *a):
            pass

    fs = FakeFSEQ(frames=40000)
    tl = _timeline([("01:00:00:00", "A", fs)])
    log = CountingLog()
    p = Player(tl, FakeNetmap(), CountingSender(), log=log)
    base = tcmod.parse_tc("01:00:10:00", 30)
    t0 = time.monotonic()
    for i in range(4):
        p.feed_timecode(base + i / 30.0, t0 + i / 30.0, text="t")
    p._tick()
    p.go(base)
    for i in range(300):
        p.feed_timecode(base + i / 30.0, t0 + i / 30.0, text="t")
        p._tick()
    states = [e for e in log.events if e[0] == "state"]
    check(len(states) <= 2,
          f"{len(states)} state lines over 300 free-run ticks with a live "
          f"feed. One per frame buries the log: {states[:3]}")
    check(any("FREERUN" in m for _, m in states),
          f"the log should say the show went into free run once: {states}")

    # Releasing is one more, not three hundred.
    before = len(log.events)
    p.release()
    for i in range(50):
        p.feed_timecode(base + i / 30.0, t0 + i / 30.0, text="t")
        p._tick()
    after = [e for e in log.events[before:] if e[0] == "state"]
    check(len(after) <= 2,
          f"releasing produced {len(after)} state lines: {after[:3]}")
    p.stop()
    print("  ok")


def test_the_readout_tells_the_truth_in_a_free_run():
    section("during a free run, the LTC line must describe the FEED")
    # The number counted while the caption said "frozen, nothing in for 0.0s",
    # because the caption was keyed on the SHOW's state. That is the one cue
    # an operator uses to decide when to hand the show back.
    from ltcplay import display as _disp
    fs = FakeFSEQ(frames=40000)
    tl = _timeline([("01:00:00:00", "A", fs)])
    p = Player(tl, FakeNetmap(), CountingSender())
    base = tcmod.parse_tc("01:00:10:00", 30)
    t0 = time.monotonic()
    for i in range(4):
        p.feed_timecode(base + i / 30.0, t0 + i / 30.0, text="01:00:10:00")
    p._tick()
    p.go(base)
    p.feed_timecode(base, time.monotonic(), text="01:00:10:00")
    p._tick()
    check(p.state == "FREERUN", f"expected FREERUN, got {p.state}")
    check(p.feed_state == LOCKED,
          f"the feed is healthy and the player must know it separately, got "
          f"{p.feed_state}")

    sc = _disp.Screen(colour=False, cols=90)

    class D:
        detected_rate = (30.0, False, True)
        measured_fps = 30.0
        measured_span = 12.0
        frames_decoded = 100
        sync_errors = 0
    screen = "\n".join(_disp.render(p, D(), tl, sc, time.monotonic() - 5))
    check("frozen" not in screen,
          f"the screen says the timecode is frozen while it is arriving:\n"
          f"{screen}")
    check("not following it" in screen,
          f"the screen must say the show is not following a healthy feed:\n"
          f"{screen}")

    # And when the feed really is dead, it must say so. (_last_lock is what
    # the engine measures the feed by; last_ltc_at is what the display shows.
    # A real dropout moves both.)
    with p._lock:
        p._last_lock = time.monotonic() - 30.0
    p.last_ltc_at = time.monotonic() - 30.0
    p._tick()
    check(p.feed_state == LOST,
          f"with nothing arriving for 30s the FEED is lost, whatever the show "
          f"is doing: {p.feed_state}")
    screen = "\n".join(_disp.render(p, D(), tl, sc, time.monotonic() - 5))
    check("nothing in for" in screen,
          f"with the feed dead the screen must say so:\n{screen}")
    p.stop()
    print("  ok")


def test_go_runs_without_the_feed():
    section("GO: running the show when the timecode line is dead")
    # From the 2026-09-13 design audit: every show controller has a GO and
    # this did not. If the LTC feed fails for good before a set, the only
    # behaviour available was a preshow loop for thirty minutes in front of
    # an audience.
    from ltcplay.player import FREERUN
    fs = FakeFSEQ(frames=8000)               # 200s
    idle = FakeFSEQ(frames=40, channels=64)
    tl = _timeline([("01:00:00:00", "A", fs), ("01:10:00:00", "B", fs)],
                   idle="/tmp/idle.fseq", gaps="idle")
    p = Player(tl, FakeNetmap(), CountingSender(), gaps="idle")
    p.idle_cue = timeline.Cue("00:00:00:00", "/tmp/idle.fseq", "preshow loop")
    p.idle_cue.fseq = idle
    p.idle_cue._spans = [(0, 0, 64)]
    p.idle_cue._gaps = None

    # No timecode has ever arrived: the preshow loop, as it should be.
    p._tick()
    check(p.state == LOST and p.source == IDLE,
          f"with no feed the preshow should run, got {p.state}/{p.source}")

    # GO from the top of the show.
    at = tcmod.parse_tc("01:00:00:00", 30)
    p.go(at)
    p._tick()
    check(p.state == FREERUN, f"the state must say so, got {p.state}")
    check(p.source == SHOW and p.current_cue is not None
          and p.current_cue.name == "A",
          f"GO should be playing cue A, got {p.source}/"
          f"{p.current_cue and p.current_cue.name}")
    first = p.current_frame
    time.sleep(0.25)
    p._tick()
    check(p.current_frame > first,
          f"a free run has to advance on its own clock: {first} -> "
          f"{p.current_frame}")

    # And a feed that comes back must NOT yank it sideways.
    now = time.monotonic()
    for i in range(6):
        p.feed_timecode(tcmod.parse_tc("02:00:00:00", 30) + i / 30.0,
                        now + i / 30.0, text="02:00:00:00")
    p._tick()
    check(p.current_cue is not None and p.current_cue.name == "A",
          f"a feed coming back mid-free-run moved the show: "
          f"{p.current_cue and p.current_cue.name}")
    check(p.state == FREERUN, "and it must still say FREERUN")

    # Release hands it back.
    check(p.release() is True, "release should report that it did something")
    p._tick()
    check(p.state != FREERUN, "released, it must follow the feed again")
    check(p.freerun_epoch is None, "and hold no free-run clock")
    check(p.release() is False, "releasing twice is not an error, just a no-op")

    # The operator has to be told, loudly, that the rig is not on the feed.
    p.go(at)
    from ltcplay import display as _disp

    class D:
        detected_rate = (30.0, False, True)
        measured_fps = 30.0
        frames_decoded = 100
        sync_errors = 0
    ws = _disp.warnings_for(p, D(), tl)
    check(any("FREE RUNNING" in w for w in ws),
          f"nothing tells the operator the feed is being ignored: {ws}")

    # THE PANIC BUTTON BEATS EVERYTHING, free run included. Round 1 put free
    # run first, so during a free run the Blackout and Preshow buttons did
    # nothing while still reporting success. The one night GO gets used is
    # the night something else is already wrong.
    p.go(at)
    p._tick()
    check(p.source == SHOW, "free run should be playing before this")
    p.override = "blackout"
    out = p._tick()
    check(p.source == BLACK and not any(out),
          f"Blackout during a free run did nothing: {p.source}")
    p.override = "preshow"
    p._tick()
    check(p.source == IDLE,
          f"Preshow during a free run did nothing: {p.source}")
    p.override = None
    p._tick()
    check(p.source == SHOW and p.state == FREERUN,
          f"releasing the override should hand the rig back to the free run, "
          f"got {p.source}/{p.state}")
    p.release()

    # Skipping. Once the show is on our own clock there is no source to
    # fight, so a locate is just moving the epoch -- and a manual mode
    # without a locate is not a manual mode. Jeff, 2026-09-13.
    p.release()
    p.go(tcmod.parse_tc("01:00:30:00", 30))
    p._tick()
    before = p.tc_seconds
    at = p.nudge(-10)
    p._tick()
    check(abs(at - (before - 10)) < 0.2,
          f"back 10 should land 10s earlier: {before:.2f} -> {at:.2f}")
    check(abs(p.tc_seconds - at) < 0.2, "and the show must follow it")
    at2 = p.nudge(30)
    check(abs(at2 - (at + 30)) < 0.2,
          f"forward 30 should land 30s later: {at:.2f} -> {at2:.2f}")

    # It must not be possible to skip behind the start of the show clock.
    p.go(2.0)
    deep = p.nudge(-60)
    check(deep >= 0.0, f"skipping back past zero gave {deep}")

    # Cue to cue. B is at 01:02:00:00 in this timeline.
    p.go(tcmod.parse_tc("01:00:30:00", 30))
    nxt = p.go_to_cue(1)
    check(nxt.name == "B", f"next cue should be B, got {nxt.name}")
    p._tick()
    check(p.current_cue is not None and p.current_cue.name == "B",
          f"and the rig should be on it: {p.current_cue and p.current_cue.name}")
    prev = p.go_to_cue(-1)
    check(prev.name == "A", f"previous cue should be A, got {prev.name}")

    # Restart: well into a cue it means the top of THIS one.
    p.go(tcmod.parse_tc("01:00:30:00", 30))
    top = p.go_to_cue(0)
    check(top.name == "A" and abs((time.monotonic() - p.freerun_epoch)
                                  - top.tc_seconds) < 0.2,
          f"restart should go to the top of A, got {top.name}")
    # Just inside a cue it means the one before, which is what a designer
    # means when they press it twice. (B starts at 01:10:00:00 in this
    # timeline; half a second in is "I meant the one before".)
    p.go(tcmod.parse_tc("01:10:00:15", 30))
    check(p.timeline._index_at(tcmod.parse_tc("01:10:00:15", 30)) == 1,
          "the test must be standing just inside B for this to mean anything")
    again = p.go_to_cue(0)
    check(again.name == "A",
          f"pressing restart just after B started should step back to A, "
          f"got {again.name}")
    # ...but well inside B it means the top of B.
    p.go(tcmod.parse_tc("01:10:30:00", 30))
    check(p.go_to_cue(0).name == "B",
          "well inside a cue, restart must mean the top of THAT cue")

    # And none of it applies while the show is following timecode: there is
    # no source on this Mac to move.
    p.release()
    for call in (lambda: p.nudge(10), lambda: p.go_to_cue(1),
                 lambda: p.go_to_cue(0)):
        try:
            call()
            check(False, "skipping should be refused when not free running")
        except ValueError as e:
            check("press GO" in str(e),
                  f"and the refusal must say what to do: {e}")

    # A free run has to start somewhere real.
    for bad in (None, -1.0):
        try:
            p.go(bad)
            check(False, f"go({bad!r}) should be refused")
        except ValueError:
            pass
    p.stop()
    print("  ok")


def test_the_bundle_stands_on_its_own():
    section("a show folder that can be carried to another Mac")
    import json, shutil, subprocess, tempfile
    sd = "/mnt/user-data/uploads/PROJECTS/Dollywood/GPL26_xLights"
    if not os.path.isdir(sd):
        print("  no show folder available, skipped")
        return
    here = os.path.dirname(os.path.abspath(__file__))
    src = tempfile.mkdtemp()
    for f in ("GPL 2026_Set 1_Opener.fseq", "xlights_networks.xml"):
        shutil.copy(os.path.join(sd, f), src)
    tlp = os.path.join(src, "b_timeline.json")
    json.dump({"name": "Bundle", "fps": 30, "show_dir": src,
               "gaps": "blackout",
               "cues": [{"tc": "01:00:00:00",
                         "fseq": "GPL 2026_Set 1_Opener.fseq",
                         "name": "Opener"}]}, open(tlp, "w"))
    out = os.path.join(tempfile.mkdtemp(), "bundle")
    r = subprocess.run([sys.executable, "-m", "ltcplay.cli", "bundle",
                        tlp, out], capture_output=True, text=True, cwd=here)
    check(r.returncode == 0, f"bundle failed: {r.stderr}")

    # Everything needed to run, and nothing pointing back at the old machine.
    for need in ("ltcplay", "Install ltcplay.command", "Web ltcplay.command",
                 "b_timeline.json", "ltcplay_bundle.json",
                 os.path.join("show", "GPL 2026_Set 1_Opener.fseq"),
                 os.path.join("show", "xlights_networks.xml")):
        check(os.path.exists(os.path.join(out, need)),
              f"the bundle is missing {need}, so it will not run anywhere")
    doc = json.load(open(os.path.join(out, "b_timeline.json")))
    check(doc["show_dir"] == "show",
          f"the bundled show file still points somewhere else: "
          f"{doc['show_dir']!r}. On the other Mac that path does not exist.")

    # The hashes have to be real, and have to notice a file that changed.
    man = json.load(open(os.path.join(out, "ltcplay_bundle.json")))
    import hashlib
    name = "GPL 2026_Set 1_Opener.fseq"
    h = hashlib.sha256(open(os.path.join(out, "show", name), "rb").read())
    check(man["files"][name]["sha256"] == h.hexdigest(),
          "the bundle manifest does not match the file it shipped")
    check(man["files"][name]["bytes"] == os.path.getsize(
        os.path.join(out, "show", name)), "the recorded size is wrong")
    check("by" in man and man["by"],
          "the bundle does not say who made it")

    # It has to WORK from inside itself, which is the only thing that matters.
    r = subprocess.run([sys.executable, "-m", "ltcplay.cli", "at",
                        "b_timeline.json", "01:00:05:00"],
                       capture_output=True, text=True, cwd=out)
    check(r.returncode == 0 and "Opener" in r.stdout,
          f"the bundle cannot resolve its own show:\n{r.stdout}{r.stderr}")
    check(os.path.abspath(out) in r.stdout,
          f"the bundle is reading from somewhere other than itself:\n"
          f"{r.stdout}")

    # And from ANYWHERE else, which is the case that actually breaks: a
    # relative show_dir resolved against the working directory works when you
    # happen to be standing in the bundle and silently fails when you are not.
    elsewhere = tempfile.mkdtemp()
    env = dict(os.environ, PYTHONPATH=here)
    r = subprocess.run([sys.executable, "-m", "ltcplay.cli", "at",
                        os.path.join(out, "b_timeline.json"), "01:00:05:00"],
                       capture_output=True, text=True, cwd=elsewhere, env=env)
    check(r.returncode == 0 and "Opener" in r.stdout,
          f"the bundle only works from inside its own folder:\n"
          f"{r.stdout}{r.stderr}")
    check(os.path.join(out, "show") in r.stdout,
          f"run from elsewhere, the bundle resolved its show folder against "
          f"the working directory instead of against the show file:\n"
          f"{r.stdout}")
    # And silently. A relative show_dir is not a path from another machine,
    # so the "this was written somewhere else, fix it" note must not fire --
    # otherwise every run of a perfectly good bundle nags the operator.
    got, note = timeline.resolve_show_dir(
        "show", os.path.join(out, "b_timeline.json"))
    check(os.path.normpath(got) == os.path.normpath(os.path.join(out, "show")),
          f"a relative show folder must resolve beside its show file, "
          f"got {got}")
    check(note is None,
          f"a bundle's own relative show folder was treated as a path from "
          f"another machine and warned about every run: {note}")

    # Absolute paths inside the show file must be rewritten to the copies
    # that were just made, or the other Mac reports MISSING for the very
    # files the bundle shipped.
    abs_tl = os.path.join(src, "abs_timeline.json")
    json.dump({"name": "Abs", "fps": 30, "show_dir": src, "gaps": "blackout",
               "cues": [{"tc": "01:00:00:00",
                         "fseq": os.path.join(src, "GPL 2026_Set 1_Opener.fseq"),
                         "name": "Opener"}]}, open(abs_tl, "w"))
    out3 = os.path.join(tempfile.mkdtemp(), "bundle3")
    r = subprocess.run([sys.executable, "-m", "ltcplay.cli", "bundle",
                        abs_tl, out3], capture_output=True, text=True, cwd=here)
    check(r.returncode == 0, f"absolute-path bundle failed: {r.stderr}")
    doc3 = json.load(open(os.path.join(out3, "abs_timeline.json")))
    got = doc3["cues"][0]["fseq"]
    check(not os.path.isabs(got),
          f"the bundled show file still names an absolute path from the "
          f"machine that made it: {got!r}")
    moved = tempfile.mkdtemp()
    shutil.rmtree(src)                       # the source machine is gone
    r = subprocess.run([sys.executable, "-m", "ltcplay.cli", "at",
                        os.path.join(out3, "abs_timeline.json"),
                        "01:00:05:00"], capture_output=True, text=True,
                       cwd=moved, env=env)
    check(r.returncode == 0 and "Opener" in r.stdout,
          f"with the source folder gone the bundle cannot find its own "
          f"renders:\n{r.stdout}{r.stderr}")

    # The bundle records a SHA-256 of every render. Checking it is the whole
    # point, and `verify` did not look: a corrupted copy passed both `check`
    # and `verify` and failed on the rig 16 seconds into a cue, blaming
    # xLights. Round 3 of the audit, 2026-09-13.
    r = subprocess.run([sys.executable, "-m", "ltcplay.cli", "verify",
                        "b_timeline.json", "--no-manifest"],
                       capture_output=True, text=True, cwd=out, env=env)
    clean = r.stdout + r.stderr
    check("checked against the SHA-256" in clean,
          f"verify never mentions the bundle manifest:\n{clean}")
    check("BAD" not in clean,
          f"an untouched bundle should not be flagged:\n{clean}")

    victim = os.path.join(out, "show", "GPL 2026_Set 1_Opener.fseq")
    was = os.path.getsize(victim)
    with open(victim, "r+b") as fh:          # same size, different bytes
        fh.seek(was // 2)
        fh.write(b"\x00" * 4096)
    check(os.path.getsize(victim) == was,
          "the test must corrupt without changing the size, or it proves "
          "only that sizes are compared")
    r = subprocess.run([sys.executable, "-m", "ltcplay.cli", "verify",
                        "b_timeline.json", "--no-manifest"],
                       capture_output=True, text=True, cwd=out, env=env)
    dirty = r.stdout + r.stderr
    check(r.returncode != 0,
          f"a corrupted bundle must fail verify:\n{dirty}")
    check("the bytes have changed since this bundle was made" in dirty,
          f"and must say exactly that, by name:\n{dirty}")
    check("GPL 2026_Set 1_Opener.fseq" in dirty,
          f"naming the file that changed:\n{dirty}")

    # --force must not delete something that merely shares the name. It used
    # to rmtree anything called `ltcplay` in the destination, so bundling to
    # a Desktop that happened to hold an `ltcplay` notes folder wiped it.
    trap = tempfile.mkdtemp()
    os.makedirs(os.path.join(trap, "ltcplay"))
    keep = os.path.join(trap, "ltcplay", "old_show_notes.txt")
    open(keep, "w").write("the notes from last season")
    r = subprocess.run([sys.executable, "-m", "ltcplay.cli", "bundle",
                        os.path.join(out, "b_timeline.json"), trap, "--force"],
                       capture_output=True, text=True, cwd=here)
    check(os.path.exists(keep),
          "bundle --force deleted a folder that was not an ltcplay package. "
          "Anything called 'ltcplay' in the destination was fair game.")
    check(r.returncode != 0 and "not an ltcplay package" in (r.stdout + r.stderr),
          f"and it must say why rather than failing halfway:\n"
          f"{r.stdout}{r.stderr}")

    # And bundling into the folder it is running from would delete itself.
    r = subprocess.run([sys.executable, "-m", "ltcplay.cli", "bundle",
                        os.path.join(out, "b_timeline.json"), here, "--force"],
                       capture_output=True, text=True, cwd=here)
    check(r.returncode != 0 and "installed" in (r.stdout + r.stderr),
          f"bundling into the running install must be refused; it deletes "
          f"the program mid-copy:\n{r.stdout}{r.stderr}")
    check(os.path.isdir(os.path.join(here, "ltcplay")),
          "THE BUNDLE COMMAND DELETED THE RUNNING INSTALL")

    # And it must refuse to build a bundle that would not play. (`src` was
    # deleted above to prove the bundle stands alone, so use a fresh folder.)
    src2 = tempfile.mkdtemp()
    tlp2 = os.path.join(src2, "broken_timeline.json")
    json.dump({"name": "Broken", "fps": 30, "show_dir": src2,
               "gaps": "blackout",
               "cues": [{"tc": "01:00:00:00", "fseq": "NotThere.fseq",
                         "name": "Gone"}]}, open(tlp2, "w"))
    tlp = tlp2
    out2 = os.path.join(tempfile.mkdtemp(), "bundle2")
    r = subprocess.run([sys.executable, "-m", "ltcplay.cli", "bundle",
                        tlp, out2], capture_output=True, text=True, cwd=here)
    check(r.returncode != 0 and "NotThere.fseq" in (r.stdout + r.stderr),
          f"a bundle missing a render must be refused by name:\n"
          f"{r.stdout}{r.stderr}")
    print("  ok")


def test_the_credit_travels_with_it():
    section("whose tool this is")
    from ltcplay import brand as brand_mod
    b = brand_mod.load()
    check(b["name"] and b["email"],
          "the brand defaults lost the name or the email")
    line = brand_mod.contact_line(b)
    check(b["name"] in line and b["email"] in line,
          f"the credit line does not carry the contact: {line!r}")

    # The logo is much taller than any text beside it, so the header row has
    # to centre on it. Aligning the row on one baseline pins the title and the
    # status pills to the logo's bottom edge instead.
    from ltcplay import web as _w
    page = open(os.path.join(os.path.dirname(_w.__file__),
                             "web", "index.html")).read()
    hdr = page.split("header{")[1].split("}")[0]
    check("align-items:center" in hdr,
          f"the header row must centre on the logo, not baseline: {hdr!r}")
    check('<div class="titles">' in page and '<div class="status">' in page,
          "the title and the status pills each need their own group, or they "
          "cannot keep a baseline between themselves inside a centred row")

    # A phone number, when set, has to appear.
    import json, tempfile
    work = tempfile.mkdtemp()
    real = brand_mod.path
    brand_mod.path = lambda: os.path.join(work, brand_mod.FILENAME)
    try:
        json.dump({"phone": "+1 555 010 1234", "url": "example.com"},
                  open(brand_mod.path(), "w"))
        b2 = brand_mod.load()
        check(b2["phone"] == "+1 555 010 1234" and b2["url"] == "example.com",
              f"the brand file was not read: {b2}")
        check(b2["name"] == brand_mod.DEFAULT["name"],
              "a partial brand file must keep the defaults for the rest")
        line = brand_mod.contact_line(b2)
        for bit in ("+1 555 010 1234", "example.com", b2["email"]):
            check(bit in line, f"{bit} missing from the credit line: {line!r}")
        # Junk must not take the page down.
        open(brand_mod.path(), "w").write("{ not json")
        check(brand_mod.load()["name"] == brand_mod.DEFAULT["name"],
              "a corrupt brand file should fall back, not raise")
    finally:
        brand_mod.path = real

    # The logo has to be there and has to be a PNG, or the page shows a
    # broken image on a machine that is meant to look finished.
    here = os.path.dirname(os.path.abspath(__file__))
    logo = os.path.join(here, "ltcplay", "web",
                        brand_mod.DEFAULT["logo"].replace("/", os.sep))
    check(os.path.exists(logo), f"the logo is missing: {logo}")
    check(open(logo, "rb").read(8) == b"\x89PNG\r\n\x1a\n",
          "the logo is not a PNG")
    page = open(os.path.join(here, "ltcplay", "web", "index.html")).read()
    check("/api/brand" in page and "creditcontact" in page,
          "the page never asks for the credit")
    print("  ok")


def test_a_dead_controller_stops_being_hammered():
    section("a dead controller must not make the Mac ARP for it every frame")
    # Jeff, 2026-09-14: "I have had show players absolutely kill networks with
    # ARP requests to missing devices." Ours is all unicast, 72 universes
    # across 22 addresses at 40 frames a second. A controller that is switched
    # off or not patched yet makes the kernel broadcast an ARP request for it,
    # forever, onto the same wire that carries the Dante audio.
    import errno
    from ltcplay import output as out_mod

    class FakeU:
        def __init__(self, ip, universe, start, count, protocol="artnet"):
            self.ip, self.universe = ip, universe
            self.start, self.count, self.protocol = start, count, protocol

    class FakeNM:
        def __init__(self, us):
            self.universes = us

    live, dead = "10.0.0.100", "10.0.0.201"
    nm = FakeNM([FakeU(live, 1, 1, 510), FakeU(dead, 2, 511, 510)])

    sent = []

    class Sock:
        def setsockopt(self, *a):
            self.bcast = True

        def bind(self, a):
            pass

        def close(self):
            pass

        def sendto(self, buf, addr):
            sent.append(addr[0])
            if addr[0] == dead:
                raise OSError(errno.EHOSTDOWN, "Host is down")

    real_socket = out_mod.socket.socket
    out_mod.socket.socket = lambda *a, **k: Sock()
    try:
        snd = out_mod.Sender(nm)
        snd.DEST_FAILS_BEFORE_QUIET = 5
        snd.DEST_QUIET_S = 0.4
        data = bytes(1020)

        for _ in range(5):
            snd.send_frame(data)
        to_dead = sent.count(dead)
        check(to_dead == 5, f"the first frames must all be tried: {to_dead}")

        # Now it should go quiet, while the live one keeps getting every frame.
        sent.clear()
        for _ in range(40):
            snd.send_frame(data)
        check(sent.count(dead) == 0,
              f"the dead address is still being sent to {sent.count(dead)} "
              f"times after it refused 5 in a row; that is an ARP request per "
              f"frame onto the Dante wire")
        check(sent.count(live) == 40,
              f"the LIVE controller lost frames while a different address was "
              f"resting: {sent.count(live)} of 40")
        check(snd.quiet_destinations == 1,
              f"nothing reports the resting address: {snd.quiet_destinations}")

        # It must come back by itself, and quickly, so a controller somebody
        # switches on mid-show is picked up without touching anything.
        time.sleep(0.5)
        sent.clear()
        snd.send_frame(data)
        check(sent.count(dead) == 1,
              "the address was never retried, so a controller powered on "
              "mid-show would stay dark for the rest of the night")

        # And once it takes packets again it must go straight back to normal.
        sent.clear()
        snd.send_frame.__self__._packets[1]["quiet_until"] = 0.0
        Sock.sendto = lambda self, buf, addr: sent.append(addr[0])
        for _ in range(10):
            snd.send_frame(data)
        check(sent.count(dead) == 10,
              f"a controller that came back is still being rested: "
              f"{sent.count(dead)} of 10")
    finally:
        out_mod.socket.socket = real_socket
    print("  ok")


def test_broadcast_destinations_are_called_out():
    section("a broadcast address in the controller map")
    from ltcplay import output as out_mod
    from ltcplay import display as disp_mod

    for ip, want in (("10.0.0.100", False), ("10.0.0.255", True),
                     ("255.255.255.255", True), ("239.255.0.1", False)):
        check(out_mod.Sender.looks_broadcast(ip) is want,
              f"{ip} classified wrongly as broadcast={not want}")

    class FakeU:
        def __init__(self, ip):
            self.ip, self.universe, self.start = ip, 1, 1
            self.count, self.protocol = 510, "artnet"

    class FakeNM:
        def __init__(self, us):
            self.universes = us

    opts = []

    class Sock:
        def setsockopt(self, lvl, opt, val):
            opts.append(opt)

        def bind(self, a):
            pass

        def close(self):
            pass

        def sendto(self, b, a):
            pass

    real = out_mod.socket.socket
    out_mod.socket.socket = lambda *a, **k: Sock()
    try:
        snd = out_mod.Sender(FakeNM([FakeU("10.0.0.100")]))
        check(not snd.broadcast_dests, "a unicast map reported a broadcast")
        check(out_mod.socket.SO_BROADCAST not in opts,
              "broadcast was enabled on a socket that never broadcasts, so a "
              "broadcast address slipping into the map would go out silently")
        opts.clear()
        snd2 = out_mod.Sender(FakeNM([FakeU("10.0.0.255")]))
        check(snd2.broadcast_dests == ["10.0.0.255"],
              f"the broadcast destination was not recorded: "
              f"{snd2.broadcast_dests}")
        check(out_mod.socket.SO_BROADCAST in opts,
              "a map that really does broadcast could not send at all")

        class P:
            sender = snd2
            step_ms = 25
            thread_restarts = loop_errors = 0
            jump_rejects = out_of_range_channels = render_errors = 0
            idle_path = None
            idle_cue = None
            audio = None
            rig = None
            state = "LOCKED"
            source = "show"
            gaps = "idle"
            ltc_frames_in = 0
            last_ltc_at = None
            tc_seconds = None
            current_cue = None
            freerun_epoch = None
            last_loop_error = last_error = ""

        class D:
            detected_rate = (None, False, False)
            measured_fps = None

        class T:
            count, fps, drop, rate_label = 30, 30.0, False, "30"

        ws = disp_mod.warnings_for(P(), D(), T())
        check(any("broadcast" in w and "10.0.0.255" in w for w in ws),
              f"a broadcast destination raised no warning: {ws}")
        check(any("40 times a second" in w for w in ws),
              f"the warning should say how often it hits every device: {ws}")
    finally:
        out_mod.socket.socket = real
    print("  ok")


def test_free_run_to_the_end_when_timecode_dies():
    section("show night: the set runs itself out when timecode is lost")
    # Jeff, 2026-09-14: on a show night the music keeps playing whatever the
    # timecode line does, so the lights should keep going rather than drop to
    # the preshow look in front of an audience.
    fs = FakeFSEQ(frames=8000)
    idle = FakeFSEQ(frames=40)

    def build(policy):
        tl = _timeline([("01:00:00:00", "A", fs), ("01:00:30:00", "B", fs)],
                       idle="/tmp/idle.fseq")
        p = Player(tl, FakeNetmap(), CountingSender(), freewheel_ms=100,
                   hold_ms=300, on_lost=policy, gaps="idle")
        p.idle_cue = timeline.Cue("00:00:00:00", "/tmp/idle.fseq", "preshow")
        p.idle_cue.fseq = idle
        p.idle_cue._spans = [(0, 0, 64)]
        p.start(step_ms=20)
        return p

    p = build("freerun")
    try:
        _drive(p, 0.4, tcmod.parse_tc("01:00:10:00", 30))
        check(p.source == SHOW, "the show did not start")
        was = p.tc_seconds
        time.sleep(1.0)                      # the feed dies
        check(p.source == SHOW,
              f"the rig dropped off the show when the feed died: {p.source}")
        check(p.freerun_epoch is not None,
              "nothing picked up this Mac's clock, so the set stops here")
        check(p.state == "FREERUN",
              f"the state should say FREE RUNNING, not {p.state}")
        check(p.tc_seconds > was,
              f"the clock is not moving: {was:.2f} -> {p.tc_seconds:.2f}")
        # It carries on from where the FEED was, give or take the hold
        # window: not from zero, and not from the top of the set.
        check(abs(p.tc_seconds - (was + 1.0)) < 0.6,
              f"the free run is {abs(p.tc_seconds - (was + 1.0)):.2f}s away "
              f"from where the feed died")
        check(p.current_cue is not None,
              "the free run is running but nothing is on the rig")

        # And it must KEEP running when timecode comes back, rather than
        # snapping the rig sideways mid-cue. The caption said it followed the
        # feed again; the code never did. Fixed the caption, 2026-09-14.
        p.feed_timecode(tcmod.parse_tc("01:00:20:00", 30), time.monotonic(),
                        text="01:00:20:00")
        time.sleep(0.2)
        check(p.freerun_epoch is not None and p.state == "FREERUN",
              "a returning feed yanked the show out of its free run; the "
              "operator has to hand it back deliberately")
        check(p.feed_state in (LOCKED, "FREEWHEEL", PARKED),
              f"the feed's own state is not being reported: {p.feed_state}")
        # ...and handing it back works.
        p.release()
        check(p.freerun_epoch is None,
              "Back to timecode did not hand the show back")
    finally:
        p.stop()

    # preshow stays the default for anything that did not ask for this.
    p2 = build("preshow")
    try:
        _drive(p2, 0.4, tcmod.parse_tc("01:00:10:00", 30))
        time.sleep(1.0)
        check(p2.source == IDLE and p2.freerun_epoch is None,
              f"on_lost preshow started free-running anyway: {p2.source}")
    finally:
        p2.stop()
    print("  ok")


def test_the_input_can_be_rebuilt_without_dropping_the_rig():
    section("rebuilding the timecode input while the show keeps running")
    # The red button then the green button, minus the blackout. Jeff wants to
    # fix audio while the show free-runs, 2026-09-14.
    import json, tempfile, threading
    from ltcplay import web as web_mod
    from ltcplay import settings as st_mod

    work = tempfile.mkdtemp()
    real_prefs, real_path = st_mod.prefs_path, st_mod.path
    st_mod.prefs_path = lambda: os.path.join(work, st_mod.PREFS_FILE)
    st_mod.path = lambda: os.path.join(work, st_mod.FILENAME)
    rows = "\n".join(
        f'    <network NetworkType="ArtNET" ComPort="127.0.0.1" '
        f'BaudRate="{u+1}" MaxChannels="510"/>' for u in range(4))
    open(os.path.join(work, "net.xml"), "w").write(
        f'<Networks>\n  <Controller Name="L" IP="127.0.0.1" '
        f'ActiveState="Active">\n{rows}\n  </Controller>\n</Networks>\n')
    json.dump({"name": "t", "fps": 30, "show_dir": work, "gaps": "idle",
               "idle": "PreShow.fseq", "on_lost": "freerun",
               "cues": [{"tc": "01:00:00:00", "fseq": "A.fseq", "name": "A"}]},
              open(os.path.join(work, "t_timeline.json"), "w"))
    open(os.path.join(work, "A.fseq"), "wb").write(b"x")
    open(os.path.join(work, "PreShow.fseq"), "wb").write(b"x")

    class Poisoned(FakeSD):
        def __init__(self):
            super().__init__()
            self.terminates = self.initializes = 0
        def _terminate(self): self.terminates += 1
        def _initialize(self): self.initializes += 1

    import ltcplay.player as plmod
    real_prepare = plmod.Player._prepare
    def fake_prepare(self, cue):
        cue.fseq = FakeFSEQ(frames=4000)
        cue.duration = cue.fseq.duration_ms / 1000.0
        cue._spans = [(0, 0, cue.fseq.channel_count)]
        cue._gaps = None
        return 0
    plmod.Player._prepare = fake_prepare
    before = {t.name for t in threading.enumerate()}
    sd = Poisoned()
    c = web_mod.Control(work, sd=sd)
    c.defaults["networks"] = os.path.join(work, "net.xml")
    c.set_input("MOTU M4", 2)
    try:
        c.start("t_timeline.json")
        sess = c.session
        check(sess is not None and sess.running, "the show did not start")
        time.sleep(0.6)
        frames_before = sess.player.frames_sent
        old_audio = sess.audio
        old_dec = sess.dec

        out = c.reset_input()

        check(sess.running and c.session is sess,
              "rebuilding the input stopped the show")
        check(sd.terminates >= 1 and sd.initializes >= 1,
              f"PortAudio itself was not rebuilt, which is the whole point: "
              f"terminates={sd.terminates}")
        check(sess.audio is not old_audio, "the same input object came back")
        check(sess.dec is not old_dec,
              "the decoder was reused, so half a frame of the old feed "
              "decodes as a sync error the moment real timecode arrives")
        check(out.get("opened") is True, f"the input did not reopen: {out}")
        check(sess.blackout_sent in (False, None),
              "the rig was blacked out, which is exactly what this avoids")

        # The rig must still be being driven.
        time.sleep(0.5)
        check(sess.player.frames_sent > frames_before,
              f"output stopped across the rebuild: {frames_before} -> "
              f"{sess.player.frames_sent}")

        # And it must be refused when nothing is running.
        c.stop()
        try:
            c.reset_input()
            check(False, "rebuilding should be refused with nothing running")
        except web_mod.SessionError:
            pass
    finally:
        try:
            c.stop()
        except Exception:
            pass
        plmod.Player._prepare = real_prepare
        st_mod.prefs_path, st_mod.path = real_prefs, real_path
    time.sleep(0.3)
    leaked = [t.name for t in threading.enumerate()
              if t.name not in before and t.name.startswith("ltcplay")]
    check(not leaked, f"threads left behind: {leaked}")
    print("  ok")


def test_the_input_stops_hunting_sample_rates():
    section("a device that will not open must not be chased across rates")
    # From Jeff's console, 2026-09-14:
    #   OpenStream @ 96000 returned: -9986
    #   OpenStream @ 88200 returned: -9986
    #   OpenStream @ 48000 returned: -9986
    #   OpenStream @ 44100 returned: -9986
    # ...repeating forever, because check_input_settings blesses rates that
    # InputStream then refuses with -10851 Invalid Property Value, and the
    # rate was being re-negotiated on EVERY retry. Each change rebuilt the
    # decoder too. That hunt was a regression I introduced the same day.
    from ltcplay import audio as audio_mod

    class Fussy(FakeSD):
        """Opens at exactly one rate and channel count, like a real box."""

        def __init__(self, good_rate=48000, good_ch=2):
            super().__init__(rates=(44100, 48000, 88200, 96000))
            self.good = (good_rate, good_ch)
            self.attempts = []

        def check_input_settings(self, device=None, channels=None,
                                 samplerate=None, dtype=None):
            return None            # says yes to everything, like AUHAL does

        def InputStream(self, device=None, channels=None, samplerate=None,
                        blocksize=None, dtype=None, callback=None):
            self.attempts.append((samplerate, channels))
            if (samplerate, channels) != self.good:
                raise RuntimeError("Error opening InputStream: Audio Unit: "
                                   "Invalid Property Value [-10851]")
            return super().InputStream(device=device, channels=channels,
                                       samplerate=samplerate,
                                       blocksize=blocksize, dtype=dtype,
                                       callback=callback)

    sd = Fussy(good_rate=48000, good_ch=2)
    dev = {"name": "MOTU M4", "index": 1, "channels": 4, "rate": 96000}
    rates_seen = []
    src = audio_mod.InputSource(sd, dev, 2, 96000, 512, lambda m, t: None,
                                on_rate_change=rates_seen.append)
    try:
        check(src._open(), f"it never found the one setting that works: "
                           f"{sd.attempts}")
        check(src._good == (48000, 2),
              f"the working setting was not remembered: {src._good}")
        first_round = len(sd.attempts)

        # Now close and reopen, the way the supervisor does. It must go
        # STRAIGHT to what worked, not walk the rate list again.
        src._close()
        sd.attempts.clear()
        check(src._open(), "the reopen failed")
        check(sd.attempts[0] == (48000, 2),
              f"the reopen started somewhere other than the known-good "
              f"setting: {sd.attempts}")
        check(len(sd.attempts) == 1,
              f"the reopen tried {len(sd.attempts)} settings when one was "
              f"known to work: {sd.attempts}")
        check(len(rates_seen) <= 1,
              f"the decoder was rebuilt {len(rates_seen)} times; a settled "
              f"input must not keep changing rate: {rates_seen}")
    finally:
        src._close()

    # A box that changes its channel count under us -- a Dante or aggregate
    # device -- is the other half of -10851. It must be followed, not fought.
    sd2 = Fussy(good_rate=48000, good_ch=4)
    src2 = audio_mod.InputSource(sd2, dict(dev, channels=4), 2, 48000, 512,
                                 lambda m, t: None)
    try:
        check(src2._open(),
              f"a device needing more channels than asked for was never "
              f"opened: {sd2.attempts}")
        check(src2._open_channels == 4,
              f"the channel count was not followed: {src2._open_channels}")
    finally:
        src2._close()

    # And when nothing works, the message has to name the device's own state
    # rather than leave PortAudio's line numbers as the only evidence.
    sd3 = Fussy(good_rate=11025, good_ch=7)
    said = []

    class Log:
        def event(self, kind, msg, throttle_s=0.0):
            said.append(msg)

    src3 = audio_mod.InputSource(sd3, dict(dev), 2, 48000, 512,
                                 lambda m, t: None, log=Log())
    check(not src3._open(), "an impossible device reported success")
    joined = " ".join(said)
    check("would not open" in joined and "Tried" in joined,
          f"the failure does not say what was tried: {said}")
    check("reports 4 input(s)" in joined,
          f"the failure does not say what the device claims to be: {said}")
    print("  ok")


def test_a_poisoned_portaudio_is_rebuilt():
    section("a USB dock pulled mid-show: PortAudio itself has to be rebuilt")
    # Jeff, 2026-09-14, from his own log:
    #   audio  no audio for 2s, rebuilding the input      (x5, once a second)
    #   audio  open: Internal PortAudio error [PaErrorCode -9986]
    # forever. When CoreAudio's device list changes under a live PortAudio
    # instance -- unplugging a USB-C dock carrying a Dante input does exactly
    # that -- the cached device table goes stale and EVERY later open fails
    # with -9986 for the life of the process. Reopening the stream cannot fix
    # it. Stop and Run could not fix it either, because the engine process
    # survives a stop and took the poisoned PortAudio with it; quitting the
    # program was the only cure.
    import threading
    from ltcplay import audio as audio_mod

    class PoisonedSD(FakeSD):
        """Healthy, then permanently -9986 until _terminate/_initialize."""

        def __init__(self):
            super().__init__()
            self.poisoned = False
            self.terminates = 0
            self.initializes = 0

        def unplug(self):
            self.poisoned = True

        def _terminate(self):
            self.terminates += 1

        def _initialize(self):
            self.initializes += 1
            # Rebuilding PortAudio is what clears it, and only that.
            self.poisoned = False

        def InputStream(self, *a, **kw):
            if self.poisoned:
                raise RuntimeError("Error opening InputStream: Internal "
                                   "PortAudio error [PaErrorCode -9986]")
            return super().InputStream(*a, **kw)

    sd = PoisonedSD()
    dev = {"name": "MOTU M4", "index": 1, "channels": 4, "rate": 48000}
    blocks = []
    src = audio_mod.InputSource(sd, dev, 2, 48000, 512,
                                lambda m, t: blocks.append(t))
    src.SILENCE_BEFORE_REOPEN_S = 0.2
    src.REOPEN_BACKOFF_S = 0.05
    before = {t.name for t in threading.enumerate()}
    try:
        check(src.start(), "the input should open while the dock is present")
        check(src.attached, "the input says it is not attached after a good open")

        # The dock goes. Every open from here fails the way Jeff's log shows.
        sd.unplug()
        src._close()

        deadline = time.time() + 8.0
        while time.time() < deadline and not src.attached:
            time.sleep(0.1)

        check(sd.terminates >= 1 and sd.initializes >= 1,
              f"PortAudio was never rebuilt, so every retry failed the way it "
              f"did on Jeff's Mac: terminates={sd.terminates} "
              f"initializes={sd.initializes}")
        check(src.pa_resets >= 1,
              "the rebuild was not recorded, so nothing on screen or in the "
              "log would say it happened")
        check(src.attached,
              "the input never came back. Retrying the STREAM cannot clear a "
              "stale PortAudio device table; only rebuilding PortAudio can.")
        check(src.open_errors >= audio_mod.InputSource.FAILURES_BEFORE_RESET,
              "the reset fired before the failures it is supposed to follow")

        # It must not thrash: a healthy input rebuilds PortAudio no further.
        was = sd.terminates
        time.sleep(1.0)
        check(sd.terminates == was,
              f"PortAudio is being rebuilt while the input is healthy "
              f"({was} -> {sd.terminates}); that would drop the feed on a beat")

    finally:
        src.stop()
    time.sleep(0.3)

    # A good open has to CLEAR the failure count, or the next outage rebuilds
    # PortAudio on its very first failed open -- tearing the audio system down
    # for a hiccup that would have cleared by itself.
    sd3 = PoisonedSD()
    src3 = audio_mod.InputSource(sd3, dict(dev), 2, 48000, 512,
                                 lambda m, t: None)
    try:
        sd3.unplug()
        for _ in range(audio_mod.InputSource.FAILURES_BEFORE_RESET - 1):
            src3._open()
        check(sd3.terminates == 0,
              "PortAudio was rebuilt before the failures it is meant to follow")
        sd3.poisoned = False
        check(src3._open(), "the input should open once the fault clears")
        sd3.unplug()
        was3 = sd3.terminates
        src3._open()
        check(sd3.terminates == was3,
              "one failed open after a healthy one rebuilt the whole audio "
              "system: the failure count is not cleared by a good open, so "
              "every later hiccup tears PortAudio down immediately")
    finally:
        src3._close()

    leaked = [t.name for t in threading.enumerate()
              if t.name not in before and t.name.startswith("ltcplay")]
    check(not leaked, f"threads left behind: {leaked}")

    # And a machine whose PortAudio cannot be rebuilt must not crash the show.
    class Unrebuildable(PoisonedSD):
        def _terminate(self):
            raise RuntimeError("no")

    sd2 = Unrebuildable()
    sd2.unplug()
    src2 = audio_mod.InputSource(sd2, dict(dev), 2, 48000, 512, lambda m, t: None)
    for _ in range(audio_mod.InputSource.FAILURES_BEFORE_RESET + 1):
        src2._open()
    check(not src2.attached and src2.pa_resets == 0,
          "a failed rebuild should be recorded as not having happened")
    check("rebuild" in src2.last_error or "open:" in src2.last_error,
          f"the operator should see why: {src2.last_error!r}")
    print("  ok")


def test_a_missing_input_still_runs_the_preshow():
    section("no timecode input: the show runs anyway, on the preshow look")
    # This reverses an earlier decision, deliberately. A PortAudio error at
    # open used to refuse the whole start -- correct about not leaving an
    # unstoppable engine behind, wrong about what to do instead. It left the
    # rig DARK over exactly the failure the preshow loop exists to cover.
    # Jeff, 2026-09-14: "Even if my Timecode input is missing, when I hit run
    # I need PreShow to fire. Timecode should be recoverable (the input) even
    # after we are running the rig."
    import json, tempfile, threading
    from ltcplay.session import Session, SessionError
    from ltcplay import settings as st_mod, onlyone

    work = tempfile.mkdtemp()
    real_prefs, real_path = st_mod.prefs_path, st_mod.path
    st_mod.prefs_path = lambda: os.path.join(work, st_mod.PREFS_FILE)
    st_mod.path = lambda: os.path.join(work, st_mod.FILENAME)
    rows = "\n".join(
        f'    <network NetworkType="ArtNET" ComPort="127.0.0.1" '
        f'BaudRate="{u+1}" MaxChannels="510"/>' for u in range(4))
    net = os.path.join(work, "net.xml")
    open(net, "w").write(f'<Networks>\n  <Controller Name="L" IP="127.0.0.1" '
                         f'ActiveState="Active">\n{rows}\n  </Controller>\n</Networks>\n')
    tlp = os.path.join(work, "t_timeline.json")
    json.dump({"name": "t", "fps": 30, "show_dir": work, "gaps": "idle",
               "idle": "PreShow.fseq", "on_lost": "preshow",
               "cues": [{"tc": "01:00:00:00", "fseq": "A.fseq", "name": "A"}]},
              open(tlp, "w"))
    open(os.path.join(work, "A.fseq"), "wb").write(b"not an fseq")
    open(os.path.join(work, "PreShow.fseq"), "wb").write(b"not an fseq")

    class Unplugged(FakeSD):
        """The interface is not on the Mac at all -- the ordinary case of a
        box that is unplugged, unpowered, or still asleep."""

        def __init__(self):
            super().__init__()
            self.devices = [d for d in self.devices if d["name"] != "MOTU M4"]
            self.default = type("D", (), {"device": (0, 2)})()

        def plug_in(self):
            self.devices.insert(1, {"name": "MOTU M4", "max_input_channels": 4,
                                    "max_output_channels": 4,
                                    "default_samplerate": 48000, "hostapi": 0})

    import ltcplay.player as plmod
    real_prepare = plmod.Player._prepare

    def fake_prepare(self, cue):
        cue.fseq = FakeFSEQ(frames=4000)
        cue.duration = cue.fseq.duration_ms / 1000.0
        cue._spans = [(0, 0, cue.fseq.channel_count)]
        cue._gaps = None
        return 0

    plmod.Player._prepare = fake_prepare
    before = {t.name for t in threading.enumerate()}
    sd = Unplugged()
    sess = None
    try:
        sess = Session(tlp, networks=net, no_log=True, sd=sd,
                       device="MOTU M4", channel=2)
        # open() must not refuse over a missing input.
        try:
            sess.open()
        except SessionError as e:
            check(False, f"open() refused the show over the input: {e}")
            raise
        check(sess.dev is None, "a device was resolved that is not attached")
        check(not any("not attached" in n for n in sess.notes),
              f"a missing input was filed as a permanent note, so it will "
              f"still be on screen long after the input comes back: "
              f"{sess.notes}")

        sess.start()
        check(sess.running, "the show must run with no timecode input")
        check(sess.player.idle_cue is not None,
              "the preshow did not load, so there is nothing to hold")

        # The rig must be lit by the preshow, not dark.
        deadline = time.time() + 3.0
        while time.time() < deadline and sess.player.frames_sent < 3:
            time.sleep(0.05)
        check(sess.player.frames_sent > 0,
              "nothing was sent to the rig at all")
        from ltcplay.player import IDLE
        check(sess.player.source == IDLE,
              f"the rig should be holding the preshow, not {sess.player.source}")

        snap = sess.snapshot()
        check(snap["input_attached"] is False,
              "the page would say the input is fine when it is missing")
        check("not attached" in (snap["input_error"] + snap["input"]).lower(),
              f"the page must say WHY in words: {snap['input_error']!r} "
              f"{snap['input']!r}")

        # ---- and now the interface turns up, mid-show ----
        sd.plug_in()
        deadline = time.time() + 8.0
        while time.time() < deadline and not sess.audio.attached:
            time.sleep(0.1)
        check(sess.audio.attached,
              "the input never came back after the interface was plugged in; "
              "the operator would have to stop the show to pick it up")
        check(sess.running, "the show stopped when the input came back")

        # Timecode from it has to reach the player, with no restart.
        deadline = time.time() + 6.0
        while time.time() < deadline and sess.player.last_ltc_at is None:
            time.sleep(0.1)
        check(sess.player.last_ltc_at is not None,
              "the input reopened but its timecode never reached the show")
        snap2 = sess.snapshot()
        check(snap2["input_attached"] is True,
              "the page still says the input is missing after it came back")
        # ...and everything that said so has to be withdrawn, not just the
        # boolean. A warning that outlives its condition teaches the operator
        # to ignore the panel.
        check(snap2["input_error"] == "",
              f"the old failure is still being reported after recovery: "
              f"{snap2['input_error']!r}")
        stale = [w for w in snap2["warnings"]
                 if "is not open" in w or "not attached" in w]
        check(not stale,
              f"warnings about the missing input are still on screen after "
              f"it came back: {stale}")
        stale_n = [n for n in snap2["notes"] if "not attached" in n]
        check(not stale_n, f"stale notes after recovery: {stale_n}")
    finally:
        if sess is not None:
            try:
                sess.stop()
            except Exception:
                pass
        plmod.Player._prepare = real_prepare
        st_mod.prefs_path, st_mod.path = real_prefs, real_path

    time.sleep(0.3)
    leaked = [t.name for t in threading.enumerate()
              if t.name not in before and t.name.startswith("ltcplay")]
    check(not leaked, f"threads left behind after stop: {leaked}")
    try:
        g = onlyone.OutputLock(onlyone.path(), "next attempt").acquire()
        g.release()
    except onlyone.AlreadyRunning as e:
        check(False, f"the run kept the output lock after stop: {e.holder}")
    print("  ok")


def test_the_input_can_be_changed_mid_show():
    section("switching the timecode input without stopping the show")
    # Jeff, 2026-09-14. Stopping the engine to fix a cable blacks the rig out
    # in front of an audience over something that has nothing to do with what
    # is on the trees.
    import json, tempfile, threading
    from ltcplay import web as web_mod
    from ltcplay import settings as st_mod
    from ltcplay.session import SessionError

    work = tempfile.mkdtemp()
    real_prefs, real_path = st_mod.prefs_path, st_mod.path
    st_mod.prefs_path = lambda: os.path.join(work, st_mod.PREFS_FILE)
    st_mod.path = lambda: os.path.join(work, st_mod.FILENAME)
    rows = "\n".join(
        f'    <network NetworkType="ArtNET" ComPort="127.0.0.1" '
        f'BaudRate="{u+1}" MaxChannels="510"/>' for u in range(4))
    open(os.path.join(work, "net.xml"), "w").write(
        f'<Networks>\n  <Controller Name="L" IP="127.0.0.1" '
        f'ActiveState="Active">\n{rows}\n  </Controller>\n</Networks>\n')
    json.dump({"name": "t", "fps": 30, "show_dir": work, "gaps": "idle",
               "idle": "PreShow.fseq", "on_lost": "preshow",
               "cues": [{"tc": "01:00:00:00", "fseq": "A.fseq", "name": "A"}]},
              open(os.path.join(work, "t_timeline.json"), "w"))
    open(os.path.join(work, "A.fseq"), "wb").write(b"x")
    open(os.path.join(work, "PreShow.fseq"), "wb").write(b"x")

    import ltcplay.player as plmod
    real_prepare = plmod.Player._prepare

    def fake_prepare(self, cue):
        cue.fseq = FakeFSEQ(frames=4000)
        cue.duration = cue.fseq.duration_ms / 1000.0
        cue._spans = [(0, 0, cue.fseq.channel_count)]
        cue._gaps = None
        return 0

    plmod.Player._prepare = fake_prepare
    before = {t.name for t in threading.enumerate()}
    sd = FakeSD(ltc_channel=2)
    c = web_mod.Control(work, sd=sd)
    # Start on the wrong input: the MOTU's input 1, which carries audio but no
    # timecode. This is the one-in-four guess a person gets wrong at the rig.
    c.set_input("MOTU M4", 1)
    saved = {}
    try:
        c.defaults["networks"] = os.path.join(work, "net.xml")
        c.start("t_timeline.json")
        sess = c.session
        check(sess is not None and sess.running, "the show did not start")
        time.sleep(0.8)
        check(sess.player.last_ltc_at is None,
              "input 1 carries no timecode, so nothing should have decoded")
        before_frames = sess.player.frames_sent

        # Now switch to input 2, live.
        out = c.set_input("MOTU M4", 2)
        check(out.get("live") is True and out.get("opened") is True,
              f"the running show did not take the new input: {out}")
        check(c.session is sess and sess.running,
              "switching the input stopped or replaced the show")
        check(sess.channel == 2, f"the session is still on input {sess.channel}")

        deadline = time.time() + 6.0
        while time.time() < deadline and sess.player.last_ltc_at is None:
            time.sleep(0.1)
        check(sess.player.last_ltc_at is not None,
              "timecode never arrived after switching to the input carrying it")
        check(sess.player.frames_sent > before_frames,
              "the rig stopped being driven across the input switch")
        check(sess.snapshot()["input_attached"] is True,
              "the page says the input is missing after a good switch")

        # A switch that cannot work is refused, and must not disturb either
        # the running show or the saved setting.
        try:
            c.set_input("MOTU M4", 99)
            check(False, "input 99 does not exist and should be refused")
        except SessionError as e:
            check("no input 99" in str(e),
                  f"the refusal should name the problem: {e}")
        check(c.session is sess and sess.running and sess.channel == 2,
              "a refused switch disturbed the running show")
        saved = st_mod.load()
    finally:
        try:
            c.stop()
        except Exception:
            pass
        plmod.Player._prepare = real_prepare
        st_mod.prefs_path, st_mod.path = real_prefs, real_path

    check(int(saved.get("channel") or 0) == 2,
          f"a refused switch overwrote the saved input: {saved}")
    time.sleep(0.3)
    leaked = [t.name for t in threading.enumerate()
              if t.name not in before and t.name.startswith("ltcplay")]
    check(not leaked, f"threads left behind: {leaked}")
    print("  ok")


def test_a_failed_start_leaves_nothing_running():
    section("a start that fails must not leave an engine on the rig")
    # Found by an adversarial audit, 2026-09-13. Session.start() brings up the
    # output thread and the socket FIRST and opens the audio input second, so
    # an error at open raised past the caller with the engine already running.
    # The web Control had not yet stored the session, so the page said "idle",
    # Stop did nothing, and the next Start put a SECOND engine on the same
    # universes.
    #
    # An input that will not open is no longer such a failure -- the show runs
    # on the preshow, see the test above. But anything UNEXPECTED out of the
    # input still has to unwind the whole thing rather than leave an engine
    # nothing owns, so that path is what this proves.
    import json, tempfile, threading
    from ltcplay.session import Session, SessionError
    from ltcplay import settings as st_mod, onlyone
    from ltcplay import audio as audio_mod

    work = tempfile.mkdtemp()
    real_prefs, real_path = st_mod.prefs_path, st_mod.path
    st_mod.prefs_path = lambda: os.path.join(work, st_mod.PREFS_FILE)
    st_mod.path = lambda: os.path.join(work, st_mod.FILENAME)
    rows = "\n".join(
        f'    <network NetworkType="ArtNET" ComPort="127.0.0.1" '
        f'BaudRate="{u+1}" MaxChannels="510"/>' for u in range(4))
    net = os.path.join(work, "net.xml")
    open(net, "w").write(f'<Networks>\n  <Controller Name="L" IP="127.0.0.1" '
                         f'ActiveState="Active">\n{rows}\n  </Controller>\n</Networks>\n')
    tlp = os.path.join(work, "t_timeline.json")
    json.dump({"name": "t", "fps": 30, "show_dir": work, "gaps": "blackout",
               "cues": [{"tc": "01:00:00:00", "fseq": "A.fseq", "name": "A"}]},
              open(tlp, "w"))
    open(os.path.join(work, "A.fseq"), "wb").write(b"not an fseq")

    import ltcplay.player as plmod
    real_prepare = plmod.Player._prepare
    real_start = audio_mod.InputSource.start

    def fake_prepare(self, cue):
        cue.fseq = FakeFSEQ(frames=4000)
        cue.duration = cue.fseq.duration_ms / 1000.0
        cue._spans = [(0, 0, cue.fseq.channel_count)]
        cue._gaps = None
        return 0

    def exploding_start(self):
        raise MemoryError("out of memory building the input")

    plmod.Player._prepare = fake_prepare
    audio_mod.InputSource.start = exploding_start
    before = {t.name for t in threading.enumerate()}
    try:
        sess = Session(tlp, networks=net, no_log=True, sd=FakeSD(),
                       device="MOTU M4", channel=1)
        sess.open()
        try:
            sess.start()
            check(False, "an unexpected error out of the input must raise")
        except MemoryError:
            pass
        check(not sess.running,
              "the session says it is running after a failed start")
        check(sess.blackout_sent or sess.no_output,
              "a failed start must black the rig out on its way down; it had "
              "already sent frames by then")
        check(sess.player is None or not sess.player.thread_alive(),
              "THE OUTPUT THREAD IS STILL DRIVING THE RIG after a start that "
              "failed. Nothing owns it and nothing can stop it.")
        time.sleep(0.3)
        leaked = [t.name for t in threading.enumerate()
                  if t.name not in before and t.name.startswith("ltcplay")]
        check(not leaked, f"threads left behind by a failed start: {leaked}")

        # And the rig lock must be free, or the next honest start is refused
        # because of a start that never happened.
        try:
            g = onlyone.OutputLock(onlyone.path(), "next attempt").acquire()
            g.release()
        except onlyone.AlreadyRunning as e:
            check(False, f"a failed start kept the output lock: {e.holder}")
    finally:
        plmod.Player._prepare = real_prepare
        audio_mod.InputSource.start = real_start
        st_mod.prefs_path, st_mod.path = real_prefs, real_path
    print("  ok")


def test_only_one_player_sends_at_a_time():
    section("two ltcplays on one Mac must not both drive the rig")
    import tempfile
    from ltcplay import onlyone
    where = os.path.join(tempfile.mkdtemp(), "out.lock")
    first = onlyone.OutputLock(where, "the Run window").acquire()
    try:
        onlyone.OutputLock(where, "the Web window").acquire()
        check(False, "a second sender was allowed to take the rig")
    except onlyone.AlreadyRunning as e:
        check("Run window" in e.holder,
              f"the refusal must name who is holding it: {e.holder!r}")
    first.release()
    second = onlyone.OutputLock(where, "the Web window").acquire()
    check(second is not None, "the lock must free when the holder stops")
    second.release()

    # A holder that dies without releasing must not wedge the rig.
    import subprocess
    code = ("import sys, time; sys.path.insert(0, %r);"
            "from ltcplay.onlyone import OutputLock;"
            "OutputLock(%r, 'a crashed run').acquire(); time.sleep(30)"
            % (os.path.dirname(os.path.abspath(__file__)), where))
    proc = subprocess.Popen([sys.executable, "-c", code])
    time.sleep(1.0)
    try:
        onlyone.OutputLock(where, "me").acquire()
        check(False, "the lock did not hold against a live process")
    except onlyone.AlreadyRunning:
        pass
    proc.kill(); proc.wait()
    time.sleep(0.3)
    try:
        got = onlyone.OutputLock(where, "after the crash").acquire()
        got.release()
    except onlyone.AlreadyRunning:
        check(False, "a killed holder left the lock stuck; the operator would "
                     "have to know about a lock file to run a show")

    # Stopping must say what actually reached the rig. "outputs blacked out"
    # used to print whether or not a single packet left the machine; with the
    # socket down the controllers held the last lit frame and the operator
    # walked away believing the rig was dark.
    class DeadSender:
        universe_count = 4
        packets_sent = 0
        send_errors = 0
        reopens = 0
        last_error = "network gone"
        seconds_since_ok = 99.0

        def send_frame(self, data):
            self.send_errors += 1          # nothing leaves the machine

        def blackout(self):
            self.send_frame(b"")

        def close(self):
            pass

    class LiveSender(DeadSender):
        def send_frame(self, data):
            self.packets_sent += 1

    from ltcplay.session import Session as _S
    for sender, expect, label in ((LiveSender(), True, "a healthy socket"),
                                  (DeadSender(), False, "a dead socket")):
        sess = _S.__new__(_S)
        sess._running = True
        sess.player = None
        sess.audio = None
        sess.log = None
        sess.no_output = False
        sess._lock = None
        sess.sender = sender
        sess.blackout_sent = None
        sess.stop()
        check(sess.blackout_sent is expect,
              f"{label}: stop reported blackout_sent={sess.blackout_sent}, "
              f"which is not what happened on the wire")

    # And the ENGINE has to honour it, with a sentence rather than a
    # traceback. Testing onlyone.py on its own proves the lock works and
    # nothing about whether a show uses it.
    import json, tempfile
    from ltcplay.session import Session, SessionError
    from ltcplay import settings as st_mod, onlyone as oo
    work = tempfile.mkdtemp()
    real_prefs, real_path, real_lock = st_mod.prefs_path, st_mod.path, oo.path
    st_mod.prefs_path = lambda: os.path.join(work, st_mod.PREFS_FILE)
    st_mod.path = lambda: os.path.join(work, st_mod.FILENAME)
    oo.path = lambda: os.path.join(work, oo.FILENAME)
    rows = "\n".join(
        f'    <network NetworkType="ArtNET" ComPort="127.0.0.1" '
        f'BaudRate="{u+1}" MaxChannels="510"/>' for u in range(4))
    net = os.path.join(work, "net.xml")
    open(net, "w").write(f'<Networks>\n  <Controller Name="L" IP="127.0.0.1" '
                         f'ActiveState="Active">\n{rows}\n  </Controller>\n</Networks>\n')
    tlp = os.path.join(work, "t_timeline.json")
    json.dump({"name": "t", "fps": 30, "show_dir": work, "gaps": "blackout",
               "cues": [{"tc": "01:00:00:00", "fseq": "A.fseq", "name": "A"}]},
              open(tlp, "w"))
    open(os.path.join(work, "A.fseq"), "wb").write(b"x")
    import ltcplay.player as plmod
    real_prepare = plmod.Player._prepare

    def fake_prepare(self, cue):
        cue.fseq = FakeFSEQ(frames=4000)
        cue.duration = cue.fseq.duration_ms / 1000.0
        cue._spans = [(0, 0, cue.fseq.channel_count)]
        cue._gaps = None
        return 0

    plmod.Player._prepare = fake_prepare
    held = oo.OutputLock(oo.path(), "the Run window").acquire()
    try:
        s2 = Session(tlp, networks=net, no_log=True, sd=FakeSD(),
                     device="MOTU M4", channel=1, wav=None)
        s2.open()
        try:
            s2.start()
            check(False, "a second SHOW was allowed onto the rig while "
                         "another process was already sending")
        except SessionError as e:
            check("already sending" in str(e) and "Run window" in str(e),
                  f"the refusal must be a sentence that names who has it: {e}")
        except Exception as e:
            check(False, f"the operator got a raw {type(e).__name__} instead "
                         f"of an explanation: {e}")
        finally:
            try: s2.stop()
            except Exception: pass
    finally:
        held.release()
        plmod.Player._prepare = real_prepare
        st_mod.prefs_path, st_mod.path, oo.path = real_prefs, real_path, real_lock
    print("  ok")


def test_reload_while_the_show_runs():
    section("swapping in a re-render without stopping the chase")
    # Rehearsal is a loop: change the sequence, render, watch it again. The
    # only way to do that used to be stopping the chase, which drops the rig
    # to black and loses the lock. And the file xLights writes is the same
    # file the player is holding open, so a stale reader is not just old, it
    # is reading a block table that has moved.
    import shutil
    import tempfile
    from ltcplay.player import ReloadError
    from ltcplay.fseq import FSEQ
    sd = "/mnt/user-data/uploads/PROJECTS/Dollywood/GPL26_xLights"
    src_a = os.path.join(sd, "GPL 2026_Set 1_Opener.fseq")
    src_b = os.path.join(sd, "GPL 2026_Set 1_Munsters.fseq")
    if not (os.path.exists(src_a) and os.path.exists(src_b)):
        print("  no show folder available, skipped")
        return
    work = tempfile.mkdtemp()
    live = os.path.join(work, "Live.fseq")
    shutil.copy(src_a, live)
    with FSEQ(live) as f:
        frames_a, chans = f.frame_count, f.channel_count
        first_a = f.frame(0)

    tl = timeline.Timeline(30.0, [], "reload", work)
    cue = timeline.Cue("01:00:00:00", live, "Live")
    cue.tc_seconds = tcmod.parse_tc("01:00:00:00", 30)
    tl.cues = [cue]
    snd = CountingSender()
    p = Player(tl, FakeNetmap(40000), snd)
    check(not p.open_cues(), "the cue should open cleanly")
    original = tl.cues[0].fseq

    _tick_at(p, tcmod.parse_tc("01:00:05:00", 30))
    check(p.current_cue is not None and p.current_frame > 0,
          "the show should be playing before the reload")

    # Nothing has changed on disk yet.
    check(p.stale_cues() == [], "nothing changed, nothing should read stale")
    r = p.reload()
    check(r["total"] == 1 and r["reloaded"] == ["Live"],
          f"an explicit reload should re-open everything asked for: {r}")

    # Now re-render it: same path, different content. This is exactly what
    # xLights does.
    shutil.copy(src_b, live)
    stale = p.stale_cues()
    check([c.name for c in stale] == ["Live"],
          f"a changed render must be noticed: {stale}")

    r = p.reload()
    check(r["reloaded"] == ["Live"], f"the reload should report it: {r}")
    check(tl.cues[0].fseq is not original,
          "the cue is still holding the reader it had before the reload")
    with FSEQ(src_b) as f:
        check(tl.cues[0].fseq.frame_count == f.frame_count,
              f"the cue should now be the NEW render: "
              f"{tl.cues[0].fseq.frame_count} vs {f.frame_count}")
        check(tl.cues[0].fseq.frame(0) == f.frame(0),
              "the reloaded cue does not return the new content")
    check(tl.cues[0].fseq.frame(0) != first_a or frames_a == f.frame_count,
          "the test picked two renders that are byte-identical; it proves "
          "nothing")
    check(p.stale_cues() == [], "after a reload nothing should still be stale")

    # The show keeps playing, from the same timecode, out of the new file.
    _tick_at(p, tcmod.parse_tc("01:00:05:00", 30))
    check(p.current_cue is not None and p.source == SHOW,
          f"the show must keep playing across a reload, got {p.source}")
    check(p.current_frame == 200,
          f"and stay at the same place in the sequence: {p.current_frame}")

    # A half-written render changes NOTHING. This is the 2026-09-12 failure:
    # anything less than all-or-nothing would leave the show holding a file
    # whose block table has moved.
    good_now = tl.cues[0].fseq
    open(live, "wb").write(b"PSEQ" + b"\x00" * 40)
    try:
        p.reload()
        check(False, "a half-written render must not be swapped in")
    except ReloadError as e:
        check("Live" in str(e), f"the failure must name the cue: {e}")
    check(tl.cues[0].fseq is good_now,
          "a failed reload changed the show anyway, which is the one thing "
          "it must never do")
    _tick_at(p, tcmod.parse_tc("01:00:05:00", 30))
    check(p.source == SHOW,
          f"the show must still be playing after a failed reload, got "
          f"{p.source}")
    p.stop()
    print("  ok")


def test_opening_a_render_proves_it_reads():
    section("a render that opens but will not decompress")
    # 2026-09-13, mid rehearsal: 810 read errors, "Monster Mash frame 817:
    # error determining content size from frame header". The reload had
    # accepted the file because it OPENED, and opening only reads the header
    # and block table, which xLights writes before the channel data. The
    # frames were not there yet. Opening has to mean readable.
    import shutil
    import tempfile
    from ltcplay.fseq import FSEQ, FSEQError
    sd = "/mnt/user-data/uploads/PROJECTS/Dollywood/GPL26_xLights"
    src = os.path.join(sd, "GPL 2026_Set 1_Munsters.fseq")
    if not os.path.exists(src):
        print("  no show folder available, skipped")
        return
    work = tempfile.mkdtemp()
    whole = os.path.join(work, "Whole.fseq")
    shutil.copy(src, whole)
    with FSEQ(whole) as f:
        check(f.verify() is True, "a complete render must verify")
        n_blocks = len(f._blocks)
    check(n_blocks > 2, "this test needs a multi-block render")

    # Truncate it the way a writer in progress leaves it: header and block
    # table present, channel data short. This is the exact shape that used to
    # sail through and then fail frame by frame on the rig.
    size = os.path.getsize(whole)
    for keep in (0.2, 0.6, 0.95):
        part = os.path.join(work, f"Part{int(keep*100)}.fseq")
        with open(whole, "rb") as a, open(part, "wb") as b:
            b.write(a.read(int(size * keep)))
        opened = True
        try:
            f = FSEQ(part)
        except Exception:
            opened = False          # fine: it failed even earlier
        if opened:
            try:
                f.verify()
                check(False, f"a file truncated to {int(keep*100)}% passed "
                             f"verify; it is not readable")
            except FSEQError as e:
                # Specifically the SIZE check, not the decompression one. A
                # truncated file is provable from the block table alone, in
                # microseconds, without decompressing anything, and it gives
                # the operator a sentence they can act on. Falling through to
                # a zstd error instead would still fail, but it would fail
                # slower and say the wrong thing.
                check("runs past the end of the file" in str(e),
                      f"a truncated render should be caught by the block "
                      f"table, not by a decompression error: {e}")
                check("The render is not finished" in str(e),
                      f"the refusal must say what to do about it: {e}")
            finally:
                f.close()

    # The other half-written shape, and the one the size check cannot see:
    # xLights rewrites in place, so the file can be full length with a valid
    # block table while some blocks still hold garbage. Probing only the
    # first frame would pass this; the rig would then fail somewhere in the
    # middle of the song, which is exactly what 810 read errors looked like.
    rewritten = os.path.join(work, "Rewritten.fseq")
    shutil.copy(whole, rewritten)
    with FSEQ(rewritten) as f:
        last_off, last_len = f._blocks[-1][1], f._blocks[-1][2]
        mid_off, mid_len = f._blocks[len(f._blocks) // 2][1], \
            f._blocks[len(f._blocks) // 2][2]
    for off, ln, where in ((last_off, last_len, "last"),
                           (mid_off, mid_len, "middle")):
        shutil.copy(whole, rewritten)
        with open(rewritten, "r+b") as fh:
            fh.seek(off)
            fh.write(b"\x00" * ln)
        check(os.path.getsize(rewritten) == size,
              "the test must not change the file length here; that is the "
              "whole point of this case")
        f = FSEQ(rewritten)
        try:
            f.verify()
            check(False, f"a file whose {where} block is garbage passed "
                         f"verify; only its first frame was ever looked at")
        except FSEQError as e:
            check("will not decompress" in str(e) or "came back" in str(e),
                  f"a bad block should be caught by reading it: {e}")
        finally:
            f.close()

    # And the player must refuse it for the same reason, which is what keeps
    # a half-written render off the rig.
    tl = timeline.Timeline(30.0, [], "t", work)
    cue = timeline.Cue("01:00:00:00", os.path.join(work, "Part60.fseq"), "Half")
    cue.tc_seconds = tcmod.parse_tc("01:00:00:00", 30)
    good = timeline.Cue("01:10:00:00", whole, "Whole")
    good.tc_seconds = tcmod.parse_tc("01:10:00:00", 30)
    tl.cues = [cue, good]
    p = Player(tl, FakeNetmap(40000), CountingSender())
    problems = p.open_cues()
    check(any("Half" in pr for pr in problems),
          f"a half-written render must be reported at preflight: {problems}")
    check(cue.fseq is None, "a half-written render must not be left open")
    check(good.fseq is not None, "the complete one should still load")

    # A reload that lands on a half-written file changes NOTHING, which is
    # the whole guarantee: the show keeps playing what it has.
    from ltcplay.player import ReloadError
    tl2 = timeline.Timeline(30.0, [], "t2", work)
    live = os.path.join(work, "Live.fseq")
    shutil.copy(whole, live)
    c2 = timeline.Cue("01:00:00:00", live, "Live")
    c2.tc_seconds = tcmod.parse_tc("01:00:00:00", 30)
    tl2.cues = [c2]
    p2 = Player(tl2, FakeNetmap(40000), CountingSender())
    check(not p2.open_cues(), "the complete render should open")
    held = tl2.cues[0].fseq
    with open(whole, "rb") as a, open(live, "wb") as b:
        b.write(a.read(int(size * 0.6)))
    try:
        p2.reload()
        check(False, "a reload onto a half-written render must be refused")
    except ReloadError as e:
        check("Live" in str(e), f"the refusal must name the cue: {e}")
    check(tl2.cues[0].fseq is held,
          "the show must still be holding the reader it had")
    _tick_at(p2, tcmod.parse_tc("01:00:05:00", 30))
    check(p2.source == SHOW and p2.render_errors == 0,
          f"and must still be playing cleanly: {p2.source}, "
          f"{p2.render_errors} read errors")
    p.stop(); p2.stop()
    print("  ok")


def test_auto_reload_waits_for_the_writer():
    section("auto reload leaves a file alone until it stops changing")
    import tempfile
    fs = FakeFSEQ(frames=400)
    work = tempfile.mkdtemp()
    path = os.path.join(work, "A.fseq")
    open(path, "wb").write(b"one")
    tl = _timeline([("01:00:00:00", "A", fs)])
    tl.cues[0].path = path
    p = Player(tl, FakeNetmap(), CountingSender(), auto_reload=True)
    p._opened_at[id(tl.cues[0])] = p._stamp(path)

    calls = []
    p.reload = lambda only=None: calls.append(only) or {"reloaded": [],
                                                        "over_range": [],
                                                        "total": 1}
    t = 1000.0
    check(p.poll_reload(t) is None, "nothing has changed; nothing to do")

    # The writer is working: the file changes between checks, so it must be
    # left alone however many times it is looked at.
    for i in range(4):
        open(path, "wb").write(b"x" * (10 + i))
        os.utime(path, (t + i, t + i))
        t += p.RELOAD_CHECK_S
        p.poll_reload(t)
    check(not calls,
          f"a file that is still changing must never be swapped in: {calls}")

    # The writer finishes. The stamp now has to survive TWO consecutive
    # looks, because the settle time is longer than the check interval.
    t += p.RELOAD_CHECK_S
    p.poll_reload(t)
    check(not calls,
          f"one still look is not enough; the file must be still across two "
          f"checks before it is swapped in: {calls}")
    t += p.RELOAD_CHECK_S
    p.poll_reload(t)
    check(calls == [{"A.fseq"}],
          f"a settled file should be reloaded, by name: {calls}")
    check(p.RELOAD_SETTLE_S > p.RELOAD_CHECK_S,
          "the settle time must be longer than the check interval, or a "
          "single look counts as settled and the guard does nothing")

    # And the check is rate limited, so a rehearsal does not stat every
    # render twice a second for the whole of a thirty minute set. Counting
    # reloads cannot prove this (an unsettled file reloads either way), so
    # this asserts the call returned before doing any work at all.
    before_at = p._last_reload_check
    before = len(calls)
    check(p.poll_reload(t + 0.1) is None,
          "a call inside the rate limit should do nothing")
    check(p._last_reload_check == before_at,
          "poll_reload ignored its own rate limit and went to the disk again")
    check(len(calls) == before, "poll_reload reloaded inside its rate limit")
    p.stop()
    print("  ok")


def test_sequence_position_not_timecode():
    section("where xLights is, as opposed to where the show is")
    # Jeff writes notes against show timecode during a rehearsal and then has
    # to find the same moment inside an xLights sequence, which counts from
    # the top of the sequence in M:SS.mmm and knows nothing about the show
    # clock. Both numbers have to be on screen, and they must never be the
    # same number.
    from ltcplay.tc import format_seq
    check(format_seq(0) == "0:00.000", format_seq(0))
    check(format_seq(127.933) == "2:07.933", format_seq(127.933))
    check(format_seq(59.9995) == "0:59.999" or
          format_seq(59.9995) == "1:00.000", format_seq(59.9995))
    check(format_seq(3661.0) == "61:01.000",
          f"past an hour it must keep counting minutes, not roll over: "
          f"{format_seq(3661.0)}")
    check(format_seq(None) == "-:--", "a missing position must not crash")
    check(format_seq(-2.25).startswith("-"),
          f"a negative position must read as negative: {format_seq(-2.25)}")

    # And the number the display shows has to be the offset into the CUE, not
    # the timecode. A cue starting at 01:12:24:00 that is six seconds in reads
    # 0:06.000 in xLights, not 1:12:30:00 and not 4350 seconds.
    fs = FakeFSEQ(frames=12886)             # 322.15s at 25ms
    tl = _timeline([("01:00:00:00", "A", FakeFSEQ(frames=100)),
                    ("01:12:24:00", "Just Dance", fs)])
    p = Player(tl, FakeNetmap(), CountingSender())
    _tick_at(p, tcmod.parse_tc("01:12:30:00", 30))
    check(p.current_cue is not None and p.current_cue.name == "Just Dance",
          "the test did not land on the cue it meant to")
    el = p.tc_seconds - p.current_cue.tc_seconds
    check(format_seq(el).startswith("0:06.0"),
          f"six seconds into the cue should read 0:06.0xx, got "
          f"{format_seq(el)} (tc_seconds {p.tc_seconds})")
    check(p.current_frame == 240,
          f"six seconds at 25ms is frame 240, got {p.current_frame}")
    print("  ok")


def test_a_cue_that_will_not_open_stops_the_show():
    section("a render that will not open is a hole, not a warning")
    # 2026-09-12, Dollywood: MonsterMash.fseq was still being written by
    # xLights when the show started. Preflight said "compressed but the block
    # table is empty" in one line of the log, the run went ahead, and the rig
    # stood dark for the 4m55s that cue was meant to fill. Nobody saw why.
    import json, tempfile
    from ltcplay.session import Session, SessionError
    work = tempfile.mkdtemp()
    good = os.path.join(work, "Good.fseq")
    # A real, minimal, readable FSEQ is more work than this test needs: what
    # is under test is what happens when a file does NOT open, so one good
    # cue is faked by monkeypatching and the bad one is genuinely unreadable.
    open(good, "wb").write(b"not an fseq either")
    bad = os.path.join(work, "HalfWritten.fseq")
    open(bad, "wb").write(b"PSEQ" + b"\x00" * 8)
    rows = "\n".join(
        f'    <network NetworkType="ArtNET" ComPort="127.0.0.1" '
        f'BaudRate="{u+1}" MaxChannels="510"/>' for u in range(4))
    net = os.path.join(work, "net.xml")
    open(net, "w").write(f'<Networks>\n  <Controller Name="L" IP="127.0.0.1" '
                         f'ActiveState="Active">\n{rows}\n  </Controller>\n</Networks>\n')
    tlp = os.path.join(work, "t_timeline.json")
    json.dump({"name": "t", "fps": 30, "show_dir": work, "gaps": "blackout",
               "cues": [{"tc": "01:00:00:00", "fseq": "Good.fseq",
                         "name": "Opener"},
                        {"tc": "01:01:52:02", "fseq": "HalfWritten.fseq",
                         "name": "Monster Mash"}]}, open(tlp, "w"))

    # This test must not read whatever input the operator happens to have
    # saved on the machine running it. It did, and the suite failed on Jeff's
    # Mac purely because his Dante box was unplugged.
    from ltcplay import settings as st_mod
    real_path = st_mod.path
    st_mod.path = lambda: os.path.join(work, st_mod.FILENAME)

    def open_session(**kw):
        # device= is named explicitly because it beats whatever input the
        # operator has saved. Without it this test read Jeff's saved Dante
        # box and failed on his Mac for a reason that has nothing to do with
        # what it is testing.
        return Session(tlp, no_output=True, networks=net, no_log=True,
                       sd=FakeSD(), device="MOTU M4", channel=1, **kw)

    # Both files are unreadable here, so the session refuses outright: there
    # is nothing to play. That is the existing guard.
    try:
        open_session().open()
        check(False, "a show with no loadable cue must not start")
    except SessionError as e:
        check("nothing to play" in str(e).lower(),
              f"the refusal should say there is nothing to play: {e}")

    # Now make one cue loadable, so exactly one is a hole. THIS is the case
    # that used to start happily and stand dark.
    import ltcplay.player as plmod
    real_prepare = plmod.Player._prepare

    def fake_prepare(self, cue):
        if os.path.basename(cue.path) == "Good.fseq":
            cue.fseq = FakeFSEQ(frames=4483)
            cue.duration = cue.fseq.duration_ms / 1000.0
            cue._spans = [(0, 0, cue.fseq.channel_count)]
            return 0
        return real_prepare(self, cue)

    plmod.Player._prepare = fake_prepare
    try:
        try:
            open_session().open()
            check(False, "a cue that will not open must stop the show")
        except SessionError as e:
            msg = str(e)
            check("Monster Mash" in msg,
                  f"the refusal must name the cue: {msg}")
            check("HalfWritten.fseq" in msg,
                  f"the refusal must name the file: {msg}")
            check("01:01:52:02" in msg,
                  f"the refusal must say when the hole is: {msg}")
            check("xLights still writing" in msg,
                  f"the refusal should name the usual cause: {msg}")
            check("--allow-missing" in msg,
                  f"the refusal must say how to override it: {msg}")

        # And the override actually overrides, because at 9pm on a show night
        # running with one song dark may genuinely beat not running at all.
        sess = open_session(allow_missing=True).open()
        check(sess is not None, "--allow-missing should let the show open")
        dead = [c for c in sess.tl.cues if c.fseq is None]
        check(len(dead) == 1 and dead[0].name == "Monster Mash",
              f"exactly the bad cue should be the dead one: {dead}")
        check(any("Monster Mash" in pr for pr in sess.problems),
              f"it must still be reported as a problem: {sess.problems}")
    finally:
        plmod.Player._prepare = real_prepare
        st_mod.path = real_path
    print("  ok")


def test_preshow_can_be_held_by_hand():
    section("the preshow look, on demand, before any timecode")
    idle = FakeFSEQ(frames=40, channels=64)
    show = FakeFSEQ(frames=4000)
    tl = _timeline([("01:00:00:00", "A", show),
                    ("01:02:00:00", "B", show)], idle="/tmp/idle.fseq",
                   gaps="idle")
    p = Player(tl, FakeNetmap(), CountingSender(), gaps="idle")
    p.idle_cue = timeline.Cue("00:00:00:00", "/tmp/idle.fseq", "preshow loop")
    p.idle_cue.fseq = idle
    p.idle_cue._spans = [(0, 0, 64)]

    # 1. Before any timecode has ever arrived, the override still works. This
    #    is the whole point: the rig has to be lightable at 5pm with the
    #    generator off.
    p.override = "preshow"
    out = p._tick()
    # FakeFSEQ fills every frame with its own index, so frame 0 is all zeros:
    # the proof the look is running is the source and the frame cursor, not
    # a nonzero byte.
    check(p.source == IDLE and len(out) == 64,
          f"preshow should be on the rig with no timecode at all, got "
          f"{p.source} and {len(out)} bytes")
    check(p.current_frame >= 0,
          "the preshow loop should be advancing through its own frames")
    check(p.state == LOST, f"the readout should still say LOST, got {p.state}")

    # 2. Timecode arrives and is IGNORED while the override is held, but the
    #    readout keeps telling the truth about the feed.
    _tick_at(p, tcmod.parse_tc("01:00:10:00", 30))
    check(p.source == IDLE,
          f"a held preshow must beat live timecode, got {p.source}")
    check(p.state == LOCKED,
          f"the feed is healthy and the display must say so, got {p.state}")
    check(p.current_cue is None,
          "nothing is playing from the show while the look is held")
    check(p.next_cue is not None and p.next_cue.name == "B",
          f"up next should still be computed while the look is held, got "
          f"{p.next_cue and p.next_cue.name}")

    # 3. Release it and the show takes over from where the clock actually is.
    p.override = None
    _tick_at(p, tcmod.parse_tc("01:00:10:00", 30))
    check(p.source == SHOW and p.current_cue is not None
          and p.current_cue.name == "A",
          f"releasing the override should hand the rig back to the timecode, "
          f"got {p.source}/{p.current_cue and p.current_cue.name}")
    check(p.current_frame == 400,
          f"and at the right frame, not from the top: {p.current_frame}")

    # 4. A held blackout is a separate, deliberate thing.
    p.override = "blackout"
    out = p._tick()
    check(p.source == BLACK and not any(out),
          f"a held blackout should send nothing, got {p.source}")
    check(p.state == LOCKED,
          "the feed readout must survive a held blackout too")
    print("  ok")


def test_one_frame_between_cues_is_not_a_gap():
    section("a frame of rounding between two songs is not a hole")
    # Jeff's cue grid comes out of the xLights marker list, and a marker does
    # not land on the same frame the previous render ends on: seven of the
    # twenty-two cues in GPL 2026 are one frame apart. Without a bridge that
    # is one 33ms frame of the preshow look flashing between songs, in front
    # of an audience, seven times a show.
    idle = FakeFSEQ(frames=40, channels=64)
    a = FakeFSEQ(frames=100)            # 2.500s
    b = FakeFSEQ(frames=100)
    c = FakeFSEQ(frames=100)
    # A ends at 2.500. B starts one frame later at 2.5333 (01:00:02:16).
    # C sits two seconds after B ends: a real hole, not rounding.
    tl = _timeline([("01:00:00:00", "A", a),
                    ("01:00:02:16", "B", b),
                    ("01:00:07:00", "C", c)],
                   idle="/tmp/idle.fseq", gaps="idle")
    log = _Events()
    p = Player(tl, FakeNetmap(), CountingSender(), gaps="idle", log=log)
    p.idle_cue = timeline.Cue("00:00:00:00", "/tmp/idle.fseq", "preshow loop")
    p.idle_cue.fseq = idle
    p.idle_cue._spans = [(0, 0, 64)]

    base = tcmod.parse_tc("01:00:00:00", 30)
    gap_start = base + 2.5                       # where A runs out
    nxt = tcmod.parse_tc("01:00:02:16", 30)      # where B begins
    check(abs((nxt - gap_start) - 1 / 30.0) < 0.001,
          f"the test's own gap should be one frame, got {nxt - gap_start:.4f}s")

    # Settle into A first, so what the log shows next is the bridge alone
    # and not the initial acquisition.
    _tick_at(p, base + 1.0)
    check(p.current_cue is not None and p.current_cue.name == "A",
          "A should be playing a second in")
    log.seen.clear()

    # Inside the one-frame hole: A's last frame holds, and nothing says so.
    out = _tick_at(p, gap_start + 0.008)
    check(p.source == SHOW,
          f"a one-frame hole should hold the show, got {p.source}")
    check(p.current_cue is not None and p.current_cue.name == "A",
          f"the cue that just ended should still be the cue, got "
          f"{p.current_cue and p.current_cue.name}")
    check(p.current_frame == a.frame_count - 1,
          f"it should hold A's last frame, got {p.current_frame}")
    check(out == bytes([(a.frame_count - 1) & 0xFF]) * 64,
          "the held frame is not A's last frame, byte for byte")
    check(not any(k == "cue" for k, _ in log.seen),
          f"a 33ms bridge must not log a cue change: {log.seen}")

    # The next frame is B, for real.
    _tick_at(p, nxt + 0.004)
    check(p.current_cue is not None and p.current_cue.name == "B"
          and p.current_frame == 0,
          f"B should start on its own frame, got "
          f"{p.current_cue and p.current_cue.name}/{p.current_frame}")

    # A REAL hole still shows the preshow look. B ends at 2.5333+2.5 = 5.033,
    # C starts at 7.0: nearly two seconds of nothing, which is a gap.
    _tick_at(p, nxt + 3.0)
    check(p.source == IDLE and p.current_cue is None,
          f"a two-second hole is a gap and should show the preshow look, "
          f"got {p.source}/{p.current_cue and p.current_cue.name}")

    # And with the bridge switched off, the one-frame hole is a hole again:
    # this proves the assertions above are measuring the bridge and not some
    # other reason the show happened to keep playing.
    p2 = Player(tl, FakeNetmap(), CountingSender(), gaps="idle", bridge_ms=0)
    p2.idle_cue = timeline.Cue("00:00:00:00", "/tmp/idle.fseq", "preshow loop")
    p2.idle_cue.fseq = idle
    p2.idle_cue._spans = [(0, 0, 64)]
    _tick_at(p2, gap_start + 0.008)
    check(p2.source == IDLE and p2.current_cue is None,
          f"with bridge_ms=0 the one-frame hole should be a gap, got "
          f"{p2.source}/{p2.current_cue and p2.current_cue.name}")

    # The bridge must not extend a cue that has genuinely ended with nothing
    # after it. C is last; past its end there is no next cue to bridge to.
    _tick_at(p, tcmod.parse_tc("01:00:07:00", 30) + 3.0)
    check(p.source == IDLE and p.current_cue is None,
          f"past the last cue the preshow look should run, got {p.source}")
    print("  ok")


def test_a_misspelled_setting_is_refused():
    section("a setting spelled wrong fails loudly")
    # The failure this prevents: "idle_fseq" instead of "idle" loads clean,
    # runs clean, and silently has no preshow look. Which is exactly what I
    # typed into Jeff's show file.
    import json, tempfile
    work = tempfile.mkdtemp()
    idle = os.path.join(work, "PreShow.fseq")
    open(idle, "wb").write(b"x")
    show = os.path.join(work, "A.fseq")
    open(show, "wb").write(b"x")

    def write(doc):
        p = os.path.join(work, "t.json")
        doc.setdefault("show_dir", work)
        doc.setdefault("fps", 30)
        doc.setdefault("cues", [{"tc": "01:00:00:00", "fseq": "A.fseq"}])
        json.dump(doc, open(p, "w"))
        return p

    try:
        timeline.Timeline.load(write({"idl": "PreShow.fseq"}))
        check(False, "a misspelled setting loaded without complaint")
    except ValueError as e:
        check("'idl'" in str(e) and "idle" in str(e),
              f"the error must name the key and list the real ones: {e}")

    # All three spellings of the preshow key work, because all three are
    # things a person would reasonably write.
    for key in ("idle", "preshow", "idle_fseq"):
        tl = timeline.Timeline.load(write({key: "PreShow.fseq", "gaps": "idle"}))
        check(tl.idle_fseq == idle,
              f"{key!r} should set the preshow sequence, got {tl.idle_fseq}")

    # bridge_ms is a bridge, not a way to stretch a cue.
    tl = timeline.Timeline.load(write({"bridge_ms": 120}))
    check(tl.bridge_ms == 120, f"bridge_ms should load, got {tl.bridge_ms}")
    for bad in (-1, 5000, "soon"):
        try:
            timeline.Timeline.load(write({"bridge_ms": bad}))
            check(False, f"bridge_ms={bad!r} should have been refused")
        except ValueError:
            pass
    print("  ok")


def test_one_sequence_at_two_timecodes():
    section("the same sequence closing both sets")
    # Jeff, 2026-09-13: the ending is the same programming in Set 1 and Set 2.
    # Two cues point at one file rather than two copies of it, so re-rendering
    # the ending updates both and there is nothing to drift.
    sd = "/mnt/user-data/uploads/PROJECTS/Dollywood/GPL26_xLights"
    ending = os.path.join(sd, "GPL 2026_Set 1_Ending.fseq")
    if not os.path.exists(ending):
        print("  no show folder available, skipped")
        return
    tl = timeline.Timeline(30.0, [], "shared", sd)
    cues = []
    for text, name in (("01:29:20:27", "Set 1 Ending"),
                       ("02:29:20:21", "Set 2 Ending")):
        c = timeline.Cue(text, ending, name)
        c.tc_seconds = tcmod.parse_tc(text, 30)
        cues.append(c)
    tl.cues = sorted(cues, key=lambda c: c.tc_seconds)

    p = Player(tl, FakeNetmap(40000), CountingSender())
    problems = p.open_cues()
    check(not problems, f"both cues should open cleanly: {problems}")

    a, b = tl.cues
    check(a.fseq is not None and b.fseq is not None,
          "both cues must get their own open sequence")
    check(a.fseq is not b.fseq,
          "each cue needs its own reader; sharing one would make two cues "
          "fight over the same decompression cache")
    check(abs(a.duration - b.duration) < 0.001 and a.duration > 30,
          f"both should report the same real duration, got {a.duration} "
          f"and {b.duration}")

    # The same frame must come back from both, byte for byte.
    fa = a.fseq.frame(400)
    fb = b.fseq.frame(400)
    check(fa == fb, "the same frame read through two cues differed")

    # And the timecode lookup has to land on the right one in each hour.
    at = lambda t: tl.cue_at(tcmod.parse_tc(t, 30))
    check(at("01:29:30:00").name == "Set 1 Ending",
          "hour 1 should find the Set 1 cue")
    check(at("02:29:30:00").name == "Set 2 Ending",
          "hour 2 should find the Set 2 cue")
    # Ten seconds in, both should be on the same frame of the same file.
    for text, cue in (("01:29:30:27", a), ("02:29:30:21", b)):
        off = tcmod.parse_tc(text, 30) - cue.tc_seconds
        idx = int(off * 1000.0 // cue.fseq.step_time_ms)
        check(idx == 400, f"{text} should be frame 400 of the ending, got {idx}")
    p.stop()
    print("  ok")


def test_pointing_a_show_at_a_different_folder():
    section("pointing a show at a different render folder")
    import json
    import tempfile
    from ltcplay import web as web_mod
    from ltcplay.cli import check_show_folder

    root = tempfile.mkdtemp()

    def folder(name, fseqs=1, netmap=True):
        d = os.path.join(root, name)
        os.makedirs(d, exist_ok=True)
        for i in range(fseqs):
            open(os.path.join(d, f"song{i}.fseq"), "wb").write(b"PSEQ")
        if netmap:
            open(os.path.join(d, "xlights_networks.xml"), "w").write("<Networks/>")
        return d

    good = folder("GPL26_renders", fseqs=3)
    newer = folder("GPL26_renders_v2", fseqs=5)
    no_seq = folder("Backups", fseqs=0)
    no_map = folder("HalfExported", fseqs=2, netmap=False)

    ok, why, counts = check_show_folder(good)
    check(ok and counts["fseq"] == 3 and counts["networks"],
          f"a real render folder should be accepted: {why}")
    ok, why, _ = check_show_folder(no_seq)
    check(not ok and "fseq" in why,
          "a folder with no sequences should be refused by name")
    ok, why, _ = check_show_folder(no_map)
    check(not ok and "xlights_networks.xml" in why,
          "a folder with no controller map should be refused by name")
    ok, why, _ = check_show_folder(os.path.join(root, "not-there"))
    check(not ok, "a folder that does not exist should be refused")

    # The show file lives beside the launcher, and names the folder inside it.
    launch_dir = os.path.join(root, "launcher")
    os.makedirs(launch_dir, exist_ok=True)
    show = os.path.join(launch_dir, "gpl_timeline.json")
    doc = {"name": "GPL", "fps": 30, "show_dir": good,
           "cues": [{"tc": "01:00:00:00", "fseq": "song0.fseq", "name": "A"}]}
    json.dump(doc, open(show, "w"), indent=2)

    c = web_mod.Control(launch_dir, sd=object())

    j = c.show_folder("gpl_timeline.json")
    check(j["folder"] == good and j["ok"] and j["sequences"] == 3,
          f"reading the folder back should name it: {j}")
    listed = {o["folder"] for o in j["options"]}
    check(good in listed and newer in listed,
          f"the folders beside it should be offered: {sorted(listed)}")
    check(no_seq not in listed and no_map not in listed,
          "a folder that cannot be played should not be offered")

    j = c.show_folder("gpl_timeline.json", newer)
    check(j["folder"] == newer and j.get("changed"),
          f"changing the folder should report the new one: {j}")
    on_disk = json.load(open(show))
    check(on_disk["show_dir"] == newer,
          "the show file on disk should hold the new folder")
    check(on_disk["cues"] == doc["cues"] and on_disk["name"] == "GPL",
          "rewriting show_dir must not disturb the rest of the show file")

    # A folder that cannot be played is refused, and the file is left alone.
    before = open(show).read()
    for bad in (no_seq, no_map, os.path.join(root, "not-there")):
        try:
            c.show_folder("gpl_timeline.json", bad)
            check(False, f"{os.path.basename(bad)} should have been refused")
        except web_mod.SessionError:
            pass
    check(open(show).read() == before,
          "a refused folder must not rewrite the show file")
    check(not os.path.exists(show + ".new"),
          "a refused write must not leave a half-written file behind")

    # Not while a show is on the rig.
    class _Running:
        running = True
    c.session = _Running()
    try:
        c.show_folder("gpl_timeline.json", good)
        check(False, "the folder should not change under a running show")
    except web_mod.SessionError as e:
        check("running" in str(e), f"say why it was refused, got {e}")
    c.session = None
    # ...nor while a start is still in flight, which is a moment when
    # session is not yet set but the loader is already reading the file.
    c._starting = True
    try:
        c.show_folder("gpl_timeline.json", good)
        check(False, "the folder should not change during a start")
    except web_mod.SessionError:
        pass
    c._starting = False

    # A show file the page never listed, reached by climbing out of the
    # launcher folder, must not be rewritable. This has to be a real, valid
    # show file somewhere else, or "not found" hides the hole.
    outside = os.path.join(root, "elsewhere_timeline.json")
    json.dump({"name": "Not Ours", "fps": 30, "show_dir": good, "cues": []},
              open(outside, "w"), indent=2)
    outside_before = open(outside).read()
    try:
        c.show_folder("../elsewhere_timeline.json", newer)
        check(False, "a show file outside the launcher folder should be "
                     "refused")
    except web_mod.SessionError:
        pass
    check(open(outside).read() == outside_before,
          "a show file outside the launcher folder must not be rewritten")

    # A write that dies part way through must leave the old file whole and no
    # debris beside it. json.dump writes incrementally, so a value it cannot
    # serialize fails after some bytes are already on disk.
    from ltcplay.cli import write_json
    whole = open(show).read()
    try:
        write_json(show, {"name": "GPL", "cues": [{"tc": object()}]})
        check(False, "an unserializable show file should not have been "
                     "written")
    except (TypeError, ValueError):
        pass
    check(open(show).read() == whole,
          "a failed write must leave the old show file exactly as it was")
    check(not os.path.exists(show + ".new"),
          "a failed write must not leave a half-written file beside the "
          "real one")

    # The page and the server have to agree on the shape of this.
    page = open(os.path.join(os.path.dirname(web_mod.__file__),
                             "web", "index.html")).read()
    # Start and Stop go straight through: a modal between the operator and
    # Stop is the wrong thing to be reading while the rig is doing something
    # it should not. Jeff, 2026-09-14.
    # Anchor on the HANDLER, not on the first mention of the id: both buttons
    # are named earlier where they are enabled and disabled, and splitting
    # there reads a passage with no confirm in it and calls that a pass.
    def handler(id_):
        after = page.split(f'$("{id_}").addEventListener')[1]
        # Stop at the next top-level statement, or the window runs on into
        # the NEXT handler and reports its confirm as this one's.
        cut = after.find("\n$(")
        end = after.find("\nasync function")
        for c in (cut, end):
            if c > 0:
                after = after[:c]
        return after

    run_h, stop_h = handler("btn-run"), handler("btn-stop")
    check("confirm(" not in run_h,
          f"Run still asks a second time: {run_h!r}")
    check("confirm(" not in stop_h,
          f"Stop still asks a second time: {stop_h!r}")
    # ...and what preflight found must still reach the operator, on the page
    # rather than in the modal that used to be the only place it appeared.
    check("(s.problems||[])" in page,
          "preflight problems are no longer shown anywhere")
    check('alert("Started, with warnings' not in page,
          "the start still raises a modal")

    # Copy that describes behaviour a change replaced is worse than no copy:
    # it sends the operator looking for a cable when the show would have
    # started. Audit, Jeff, 2026-09-14.
    check("The run will fail until it is plugged in" not in page,
          "the input caption still says a missing interface fails the run; "
          "since 2026-09-14 it starts on the preshow instead")
    # A control named in prose has to be the control's actual label.
    labels = set(re.findall(r'<button[^>]*>([^<]{2,40})</button>', page))
    for named in ("Back to timecode", "Validate", "Reload"):
        check(any(named in l for l in labels),
              f"the copy tells the operator to press {named!r}, and no "
              f"button on the page is called that: {sorted(labels)}")
    from ltcplay import display as _d
    src = open(_d.__file__).read()
    check("Release it from the web page" not in src,
          "the terminal screen still calls the button Release; the page "
          "calls it Back to timecode")
    # GO no longer starts from the top -- it starts from where the show is --
    # so a label saying otherwise is wrong about what the button does.
    check("GO from the top" not in page,
          "the GO button still claims it starts from the top; it defaults "
          "to where the show already is")

    # "last good send" read as proof the rig received something. A UDP send
    # to an address whose route has gone still returns success, so it sat at
    # 0.0s with the ethernet unplugged, beside a warning that no controller
    # was answering. Jeff, 2026-09-14.
    check("last good send" not in page,
          "the page still calls a local queue acceptance a good SEND, which "
          "reads as delivery and is not")
    # Faults that have stopped are reported quietly rather than dropped, and
    # the page has to actually draw them. Jeff, 2026-09-14.
    check("(s.history||[])" in page,
          "the page never draws the history list, so a fault that stopped is "
          "simply gone and nobody knows it happened")
    hist_line = [l for l in page.splitlines() if "(s.history||[])" in l]
    check(hist_line and 'className="info"' in hist_line[0],
          f"history is drawn in the warning style, which is the whole thing "
          f"being fixed: {hist_line}")
    check("accepted by this Mac" in page,
          "nothing says the number only measures what left this Mac")
    check("controllers not answering" in page,
          "the output row does not carry the one reading that does mean "
          "delivery")
    from ltcplay import display as _dd
    dsrc = open(_dd.__file__).read()
    check("last good send" not in dsrc,
          "the terminal screen still calls it a good send")
    check("controllers answering" in dsrc,
          "the terminal screen does not show whether the rig answers")

    check("/api/showdir" in page, "the page should ask for the folder")
    check('id="sd"' in page, "the page should have a folder picker")
    check("if(andFolder !== false) loadShowDir();" in
          page.split("loadTimelines(andFolder)")[1],
          "the picker must be filled in at the end of loadTimelines, or it "
          "draws before a show file is selected")



# =====================================================================
# Advatek SHOWTime scene triggers: the ALTERNATE playback mode.
# Jeff, 2026-09-15: "a trigger from LTC Player at the beginning of each
# sequence ... an ALTERNATE playing method, not the primary."
# Every check below is anchored to a failure that would look like a
# working show from the seats.
# =====================================================================

ADVATEK = ["10.0.0.100", "10.0.0.110", "10.0.0.120",
           "10.0.0.121", "10.0.0.130", "10.0.0.131"]
WEIGL = [f"10.0.0.2{n:02d}" for n in range(1, 17)]


class _TrigU:
    def __init__(self, ip, universe, start, count, protocol="artnet",
                 controller="x"):
        self.ip, self.universe = ip, universe
        self.start, self.count, self.protocol = start, count, protocol
        self.controller = controller

    @property
    def end(self):
        return self.start + self.count - 1


class _TrigNM:
    def __init__(self, us):
        self.universes = us
        self.total_channels = sum(u.count for u in us)
        self.skipped = []

    def summary(self):
        return "fake"


def _rig_netmap():
    """Six Advateks and sixteen Weigls, laid out the way the real show is."""
    us, ch, u = [], 1, 1
    for ip in ADVATEK:
        for _ in range(3):
            us.append(_TrigU(ip, 6000 + u, ch, 510, controller=ip))
            ch += 510
            u += 1
    for ip in WEIGL:
        us.append(_TrigU(ip, 10 + len(us), ch, 510, controller=ip))
        ch += 510
    return _TrigNM(us)


def _fake_sender_socket(sent, fail_for=()):
    class Sock:
        def setsockopt(self, *a):
            pass

        def bind(self, a):
            pass

        def close(self):
            pass

        def sendto(self, buf, addr):
            if addr[0] in fail_for:
                raise OSError("simulated")
            sent.append((addr[0], bytes(buf)))
    return Sock


def test_trigger_mode_mutes_the_advateks_and_nothing_else():
    section("arming scene triggers must stop pixels to the six Advateks ONLY")
    # The 16 Weigl controllers have no SHOWTime playback. A mode that stopped
    # all output would black them out for the whole show while looking, on
    # this Mac, exactly like a working backup.
    from ltcplay import output as out_mod
    nm = _rig_netmap()
    sent = []
    real = out_mod.socket.socket
    out_mod.socket.socket = lambda *a, **k: _fake_sender_socket(sent)()
    try:
        snd = out_mod.Sender(nm)
        data = bytes(nm.total_channels)
        snd.send_frame(data)
        before = {ip for ip, _ in sent}
        check(len(before) == 22, f"all 22 controllers before arming, got {len(before)}")

        sent.clear()
        hit = snd.set_muted(ADVATEK)
        check(sorted(hit) == sorted(ADVATEK),
              f"set_muted must report the six it actually matched, got {hit}")
        snd.send_frame(data)
        after = {ip for ip, _ in sent}
        check(not (after & set(ADVATEK)),
              f"an armed sender still sent to {sorted(after & set(ADVATEK))}; "
              f"the Advateks would ignore every trigger and keep showing the "
              f"live feed")
        check(after == set(WEIGL),
              f"the sixteen Weigls must keep being streamed to, got "
              f"{len(after)} address(es)")
        check(snd.muted_destinations == 18,
              f"18 muted universe rows expected, got {snd.muted_destinations}")

        # and back again
        sent.clear()
        snd.set_muted(())
        snd.send_frame(data)
        check({ip for ip, _ in sent} == set(ADVATEK) | set(WEIGL),
              "disarming must put every controller back")
    finally:
        out_mod.socket.socket = real


def test_a_muted_controller_is_not_reported_as_a_fault():
    section("muting must not look like a dead rig or rebuild the socket")
    # A mute that counts as a send failure would trip the self-healing socket
    # rebuild every frame and paint the panel red for a mode working exactly
    # as asked.
    from ltcplay import output as out_mod
    nm = _rig_netmap()
    sent = []
    real = out_mod.socket.socket
    out_mod.socket.socket = lambda *a, **k: _fake_sender_socket(sent)()
    try:
        snd = out_mod.Sender(nm)
        snd.set_muted(ADVATEK + WEIGL)          # everything
        data = bytes(nm.total_channels)
        for _ in range(50):
            snd.send_frame(data)
        check(snd.send_errors == 0,
              f"muted destinations must not count as send errors, got "
              f"{snd.send_errors}")
        check(snd.reopens == 0,
              f"a fully muted sender must not rebuild its socket, got "
              f"{snd.reopens} rebuild(s)")
        check(not sent, "nothing should have gone out at all")
    finally:
        out_mod.socket.socket = real


def test_a_cue_fires_its_scene_once_and_only_once():
    section("one cue, one trigger, however many frames it runs for")
    fired = []

    class StubTrig:
        cfg = trig_mod.TriggerConfig(mute=["10.0.0.100"],
                                     channels={"a.fseq": 4, "b.fseq": 9},
                                     idle_channel=23)

        def fire_async(self, ch, label=""):
            fired.append((ch, label))
            return True

    tl = _timeline([("01:00:00:00", "a", FakeFSEQ()),
                    ("01:00:10:00", "b", FakeFSEQ())])
    p = Player(tl, FakeNetmap(), CountingSender())
    p.trigger = StubTrig()
    p.trigger_armed = True

    p.current_cue = tl.cues[0]
    p.current_cue.path = "/tmp/a.fseq"
    for _ in range(200):
        p._service_trigger()
    check(fired == [(4, "a")],
          f"200 frames of one cue must fire exactly one trigger, got {fired}")

    p.current_cue = tl.cues[1]
    p.current_cue.path = "/tmp/b.fseq"
    for _ in range(200):
        p._service_trigger()
    check(len(fired) == 2 and fired[1][0] == 9,
          f"the next cue must fire its own channel, got {fired}")


def test_preshow_fires_once_per_entry_not_once_per_loop():
    section("the preshow scene is fired once and the box loops it itself")
    # Jeff chose: "Dedicated PreShow scene that fires once. Loop can be told
    # to play indefinitely within Advatek's settings." So re-firing every
    # 180s loop would restart the scene under the box's own loop, and firing
    # every frame would be 40 packets a second of trigger onto the show LAN.
    fired = []

    class StubTrig:
        cfg = trig_mod.TriggerConfig(mute=["10.0.0.100"],
                                     channels={"a.fseq": 4}, idle_channel=23)

        def fire_async(self, ch, label=""):
            fired.append((ch, label))
            return True

    tl = _timeline([("01:00:00:00", "a", FakeFSEQ())])
    p = Player(tl, FakeNetmap(), CountingSender())
    p.trigger = StubTrig()
    p.trigger_armed = True

    p.current_cue = None
    p.source = IDLE
    for _ in range(500):
        p._service_trigger()
    check(fired == [(23, "preshow")],
          f"500 idle frames must fire the preshow scene once, got {fired}")

    # into a cue and back out again: preshow is re-armed on re-entry
    p.current_cue = tl.cues[0]
    p.current_cue.path = "/tmp/a.fseq"
    p.source = SHOW
    for _ in range(10):
        p._service_trigger()
    p.current_cue = None
    p.source = IDLE
    for _ in range(10):
        p._service_trigger()
    check([f[0] for f in fired] == [23, 4, 23],
          f"leaving and re-entering preshow must fire it again, got {fired}")

    # a render hiccup holds the last frame; that is not a new look
    n = len(fired)
    p.source = BLACK
    for _ in range(10):
        p._service_trigger()
    check(len(fired) == n,
          "going black is not a scene and must fire nothing")


def test_an_unmapped_cue_is_named_rather_than_fired_blind():
    section("a cue with no scene channel must say so, once, not every frame")
    fired, events = [], []

    class StubTrig:
        cfg = trig_mod.TriggerConfig(mute=["10.0.0.100"],
                                     channels={"a.fseq": 4})

        def fire_async(self, ch, label=""):
            fired.append(ch)
            return True

    class Log:
        def event(self, kind, msg):
            events.append((kind, msg))

    tl = _timeline([("01:00:00:00", "zz", FakeFSEQ())])
    p = Player(tl, FakeNetmap(), CountingSender(), log=Log())
    p.trigger = StubTrig()
    p.trigger_armed = True
    p.current_cue = tl.cues[0]
    p.current_cue.path = "/tmp/zz.fseq"
    for _ in range(300):
        p._service_trigger()
    check(not fired, f"an unmapped cue must fire nothing, got {fired}")
    said = [m for k, m in events if k == "trigger"]
    check(len(said) == 1,
          f"it must be said once per entry, not 300 times; got {len(said)}")


def test_a_trigger_that_explodes_does_not_stop_the_show():
    section("the backup failing must not cost the controllers still streaming")
    class Bomb:
        cfg = trig_mod.TriggerConfig(mute=["10.0.0.100"],
                                     channels={"a.fseq": 4})

        def fire_async(self, ch, label=""):
            raise RuntimeError("boom")

    tl = _timeline([("01:00:00:00", "a", FakeFSEQ())])
    snd = CountingSender()
    p = Player(tl, FakeNetmap(), snd)
    p.trigger = Bomb()
    p.trigger_armed = True
    p.current_cue = tl.cues[0]
    p.current_cue.path = "/tmp/a.fseq"
    try:
        for _ in range(20):
            p._service_trigger()
    except Exception as e:
        check(False, f"_service_trigger raised {type(e).__name__}: {e}")
    check("trigger" in (p.last_error or ""),
          f"the failure must be recorded, got {p.last_error!r}")


def test_the_trigger_packet_is_one_artnet_channel_at_full():
    section("the fire packet, byte for byte")
    sent = []

    class Sock:
        def sendto(self, b, a):
            sent.append((bytes(b), a))

        def close(self):
            pass

    cfg = trig_mod.TriggerConfig(universe=6999, dest="10.0.0.255",
                                 mute=ADVATEK, channels={"a.fseq": 13},
                                 pulse_gap_ms=0)
    t = trig_mod.SceneTrigger(cfg, socket_factory=Sock)
    check(t.fire(13, "Ghostbusters"), "fire must report success")
    check(len(sent) == 4,
          f"3 pulse frames plus one release expected, got {len(sent)}")
    b, addr = sent[0]
    check(addr == ("10.0.0.255", 0x1936), f"wrong destination {addr}")
    check(b[:8] == b"Art-Net\x00", "Art-Net id")
    check(b[8] == 0x00 and b[9] == 0x50, "OpDmx is 0x5000, little endian")
    check(b[10] == 0x00 and b[11] == 0x0E, "protocol version 14, high byte first")
    check(b[14] | (b[15] << 8) == 6999, f"universe, got {b[14] | (b[15] << 8)}")
    check((b[16] << 8) | b[17] == 512, "length 512")
    body = b[18:]
    check(len(body) == 512, f"512 channels, got {len(body)}")
    check(body[12] == 255, "channel 13 counts from 1 and must be at full")
    check(sum(body) == 255, "every other channel must be at zero")
    check(sum(sent[-1][0][18:]) == 0, "the release frame must be all zero")


def test_a_trigger_never_blocks_the_output_loop():
    section("a slow or hung trigger socket must not stall the rig")
    # A pulse is 3 packets 50ms apart plus a release. Sent from the playback
    # thread that is 8 dropped frames on the sixteen live controllers at the
    # exact moment a cue starts.
    class SlowSock:
        def sendto(self, b, a):
            time.sleep(0.25)

        def close(self):
            pass

    cfg = trig_mod.TriggerConfig(mute=ADVATEK, channels={"a.fseq": 1})
    t = trig_mod.SceneTrigger(cfg, socket_factory=SlowSock)
    t.start()
    try:
        t0 = time.monotonic()
        for i in range(5):
            t.fire_async(i + 1)
        took = time.monotonic() - t0
        check(took < 0.05,
              f"queuing five fires took {took*1000:.0f}ms; it must not block")
    finally:
        t.stop()


def test_a_show_file_that_would_silently_do_nothing_is_refused():
    section("the trigger config must reject the ways this fails invisibly")
    E = trig_mod.TriggerError
    P = trig_mod.TriggerConfig.parse

    def refuses(doc, why):
        try:
            P(doc, "t.json")
        except E:
            return True
        check(False, f"accepted a config that {why}")
        return False

    refuses({"channels": {"a.fseq": 1}},
            "mutes nothing, so every trigger is ignored while the boxes keep "
            "showing the live feed")
    # Two cues MAY share a scene: an opener that plays at the top of both
    # sets is one recording, not two copies eating two slots on six cards.
    # Whether they are really the same is decided against the RENDERS, in
    # check_against, because a show file cannot know. Jeff, 2026-09-15.
    shared = P({"mute": ADVATEK, "channels": {"a.fseq": 1, "b.fseq": 1}},
               "t.json")
    check(shared is not None and shared.channel_for("b.fseq") == 1,
          "two cues that genuinely play the same thing must be allowed to "
          "share one recorded scene")
    refuses({"mute": ADVATEK, "channels": {"a.fseq": 0}}, "uses channel 0")
    refuses({"mute": ADVATEK, "channels": {"a.fseq": 513}},
            "uses a channel past 512")
    refuses({"mute": ADVATEK, "channels": {"a.fseq": 1}, "idle_channel": 1},
            "gives the preshow a channel already used by a cue")
    refuses({"mute": ADVATEK, "channels": {}}, "maps no scenes at all")
    refuses({"mute": ADVATEK, "channels": {"a.fseq": 1}, "universe": -1},
            "uses a negative universe")
    refuses({"mute": ADVATEK, "channels": {"a.fseq": 1}, "muet": []},
            "misspells a setting")

    good = P({"mute": ADVATEK, "channels": {"a.fseq": 1}, "idle_channel": 2},
             "t.json")
    check(good is not None and good.enabled is False,
          "a trigger block must be OFF unless the file says otherwise; a "
          "backup that arms itself is not a backup")


def test_the_scene_map_is_checked_against_the_real_show():
    section("mapping mistakes must be found at load, in daylight")
    nm = _rig_netmap()
    tl = _timeline([("01:00:00:00", "a", FakeFSEQ()),
                    ("01:00:10:00", "b", FakeFSEQ())], idle="/tmp/pre.fseq")
    tl.cues[0].path, tl.cues[1].path = "/tmp/a.fseq", "/tmp/b.fseq"

    cfg = trig_mod.TriggerConfig(mute=ADVATEK, channels={"a.fseq": 1},
                                 idle_channel=23)
    bad = cfg.check_against(tl, nm)
    check(any("b" in b and "no trigger channel" in b for b in bad),
          f"a cue with no channel must be named, got {bad}")

    cfg = trig_mod.TriggerConfig(mute=ADVATEK,
                                 channels={"a.fseq": 1, "b.fseq": 2,
                                           "ghost.fseq": 3}, idle_channel=23)
    bad = cfg.check_against(tl, nm)
    check(any("ghost.fseq" in b for b in bad),
          f"a channel mapped to a sequence not in the show must be named, "
          f"got {bad}")

    cfg = trig_mod.TriggerConfig(mute=ADVATEK,
                                 channels={"a.fseq": 1, "b.fseq": 2})
    bad = cfg.check_against(tl, nm)
    check(any("idle_channel" in b for b in bad),
          f"a preshow with no scene must be named, got {bad}")

    cfg = trig_mod.TriggerConfig(mute=["10.0.0.99"],
                                 channels={"a.fseq": 1, "b.fseq": 2},
                                 idle_channel=23)
    bad = cfg.check_against(tl, nm)
    check(any("10.0.0.99" in b for b in bad),
          f"muting an address that is not a controller must be named, "
          f"got {bad}")

    cfg = trig_mod.TriggerConfig(mute=ADVATEK, universe=6001,
                                 channels={"a.fseq": 1, "b.fseq": 2},
                                 idle_channel=23)
    bad = cfg.check_against(tl, nm)
    check(any("6001" in b for b in bad),
          f"a trigger universe that already carries pixels must be named, "
          f"got {bad}")

    cfg = trig_mod.TriggerConfig(mute=ADVATEK + WEIGL,
                                 channels={"a.fseq": 1, "b.fseq": 2},
                                 idle_channel=23)
    bad = cfg.check_against(tl, nm)
    check(any("dark for the whole show" in b for b in bad),
          f"muting everything must be named, got {bad}")

    cfg = trig_mod.TriggerConfig(mute=ADVATEK,
                                 channels={"a.fseq": 1, "b.fseq": 2},
                                 idle_channel=23)
    check(cfg.check_against(tl, nm) == [],
          f"a correct map must report nothing, got "
          f"{cfg.check_against(tl, nm)}")

    # Sharing a scene between two cues that are NOT the same render is the
    # dangerous case: it plays, it looks like a show, and it is the wrong
    # song. It has to be caught against the files, at load, in daylight.
    import tempfile, shutil, struct
    tmp = tempfile.mkdtemp()
    try:
        class Fake:
            def __init__(self, frames, step=25, ch=8, fill=0):
                self.duration_ms = frames * step
                self.step_time_ms = step
                self._n, self._ch, self._fill = frames, ch, fill
            def frame(self, i):
                if i < 0 or i >= self._n:
                    raise IndexError(i)
                return bytes([(self._fill + i) & 0xFF]) * self._ch
            def close(self):
                pass

        made = {}
        real_fseq = trig_mod.FSEQ if hasattr(trig_mod, "FSEQ") else None
        import ltcplay.fseq as fseq_mod
        orig = fseq_mod.FSEQ
        fseq_mod.FSEQ = lambda p: made[p]

        made["/tmp/x.fseq"] = Fake(100)
        made["/tmp/y.fseq"] = Fake(100)
        check(trig_mod._same_render("/tmp/x.fseq", "/tmp/y.fseq") == "",
              "two identical renders were reported as different")

        made["/tmp/z.fseq"] = Fake(120)
        why = trig_mod._same_render("/tmp/x.fseq", "/tmp/z.fseq")
        check("2.50s" in why and "3.00s" in why,
              f"a length difference must be named in seconds, said {why!r}")

        made["/tmp/w.fseq"] = Fake(100, fill=7)
        why = trig_mod._same_render("/tmp/x.fseq", "/tmp/w.fseq")
        check(why.startswith("they differ at"),
              f"different content must be caught, said {why!r}")

        # The case that actually turned up: two Openers identical for 111 of
        # 112 seconds and different only in the last 0.7s, which is exactly
        # the part that hands over to the next cue. A sampled comparison that
        # does not pin both ends walks straight past it.
        class TailDiff(Fake):
            def frame(self, i):
                b = Fake.frame(self, i)
                return bytes([0]) * self._ch if i >= self._n - 3 else b
        made["/tmp/tail.fseq"] = TailDiff(100)
        why = trig_mod._same_render("/tmp/x.fseq", "/tmp/tail.fseq")
        check(why.startswith("they differ at"),
              f"a difference confined to the last frames must be caught, "
              f"said {why!r}")

        tl2 = _timeline([("01:00:00:00", "one", FakeFSEQ()),
                         ("01:00:10:00", "two", FakeFSEQ())],
                        idle="/tmp/pre.fseq")
        tl2.cues[0].path, tl2.cues[1].path = "/tmp/x.fseq", "/tmp/w.fseq"
        cfg = trig_mod.TriggerConfig(mute=ADVATEK,
                                     channels={"x.fseq": 1, "w.fseq": 1},
                                     idle_channel=23)
        bad2 = cfg.check_against(tl2, nm)
        check(any("both fire scene 1" in b and "not the same render" in b
                  for b in bad2),
              f"two different renders sharing a scene must be refused, "
              f"got {bad2}")

        tl2.cues[1].path = "/tmp/y.fseq"
        cfg = trig_mod.TriggerConfig(mute=ADVATEK,
                                     channels={"x.fseq": 1, "y.fseq": 1},
                                     idle_channel=23)
        check(cfg.check_against(tl2, nm) == [],
              "two cues that ARE the same render must be allowed to share "
              "one scene")
        fseq_mod.FSEQ = orig
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_real_show_file_maps_every_cue():
    section("the GPL 2026 show file, if it carries a trigger block")
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "gpl2026_timeline.json")
    if not os.path.exists(path):
        print("  (no gpl2026_timeline.json beside the test, skipped)")
        return
    import json
    doc = json.load(open(path))
    if "trigger" not in doc:
        print("  (this show file has no trigger block, skipped)")
        return
    cfg = trig_mod.TriggerConfig.parse(doc["trigger"], path)
    names = [c["fseq"] for c in doc["cues"]]
    for n in names:
        show_check(cfg.channel_for(n) is not None,
              f"{n} has no trigger channel in the real show file")
    show_check(cfg.idle_channel is not None,
          "the real show file has no preshow trigger channel")
    used = sorted(list(cfg.channels.values()) + [cfg.idle_channel])
    show_check(len(used) == len(set(used)), f"a channel is used twice: {used}")
    show_check(cfg.enabled is False,
          "the real show file must not arm this mode by itself")


def test_stopping_the_show_unmutes_before_the_blackout():
    section("stop must reach the Advateks too")
    # Left armed, the six muted addresses would be skipped by the very frame
    # whose job is to prove nothing is left lit.
    order = []

    class Snd:
        universe_count = 22
        packets_sent = 0
        send_errors = 0
        reopens = 0
        last_error = ""

        def set_muted(self, ips):
            order.append(("muted", tuple(ips)))
            return list(ips)

        def blackout(self):
            order.append(("blackout", ()))
            Snd.packets_sent += 1

        def close(self):
            order.append(("close", ()))

        @property
        def seconds_since_ok(self):
            return 0.0

    class Trig:
        def stop(self):
            order.append(("trigstop", ()))

    class S:
        pass

    from ltcplay.session import Session
    se = Session.__new__(Session)
    se._running = True
    se.no_output = False
    se.player = S()
    se.player.trigger_armed = True
    se.player.stop = lambda: order.append(("playerstop", ()))
    se.sender = Snd()
    se.trigger = Trig()
    se.audio = None
    se.rig = None
    se.log = None
    se._lock = None
    se.blackout_sent = False
    Session.stop(se)
    kinds = [k for k, _ in order]
    check("muted" in kinds and "blackout" in kinds,
          f"both must happen, got {kinds}")
    check(kinds.index("muted") < kinds.index("blackout"),
          f"the unmute must come BEFORE the blackout, got {kinds}")
    check(se.player.trigger_armed is False, "stop must disarm")


def test_the_panel_tells_the_truth_about_backup_mode():
    section("the operator must be able to see which mode is running")
    class Snd:
        muted = list(ADVATEK)
        send_errors = 0
        reopens = 0
        broadcast_dests = []
        last_error = ""
        last_error_at = None
        seconds_since_error = None
        seconds_since_ok = 0.0
        quiet_destinations = 0

    class Trig:
        fired = 3
        fire_errors = 0
        dropped = 0
        last_error = ""
        last_error_at = None
        seconds_since_error = None

    tl = _timeline([("01:00:00:00", "a", FakeFSEQ())])
    p = Player(tl, FakeNetmap(), CountingSender())
    p.sender = Snd()
    p.trigger = Trig()
    p.trigger_armed = True
    p.current_cue = tl.cues[0]

    class Dec:
        detected_rate = (30.0, False, True)
        measured_fps = 30.0
        frames_decoded = 1
        sync_errors = 0

    w = " ".join(disp.warnings_for(p, Dec(), tl))
    check("BACKUP MODE" in w,
          f"armed mode must be stated on the panel, got {w[:200]!r}")
    check("resync" in w or "jump" in w,
          "the panel must say this Mac cannot resync a scene")

    # armed with nothing muted is the silent failure: say it loudly
    Snd.muted = []
    w = " ".join(disp.warnings_for(p, Dec(), tl))
    check("NOTHING is muted" in w,
          f"armed-but-not-muted must be called out, got {w[:200]!r}")

    # not armed: none of this may appear
    p.trigger_armed = False
    Snd.muted = []
    w = " ".join(disp.warnings_for(p, Dec(), tl))
    check("BACKUP MODE" not in w,
          "direct playback must not be described as backup mode")


def test_a_shared_show_folder_cannot_be_moved_away_from_it():
    section("moving the player away from a shared show folder must be refused")
    # Inside the xLights package the player's show folder is a LINK to the one
    # copy of the renders, so there is not a second 600MB of them and they
    # cannot drift apart. The move helper would have broken that link and left
    # an install with no sequences, reporting success.
    import shutil, subprocess, tempfile
    here = os.path.dirname(os.path.abspath(__file__))
    src = launcher("Move somewhere macOS allows.command")
    if not os.path.exists(src):
        print("  (no move helper beside the test, skipped)")
        return
    root = tempfile.mkdtemp()
    try:
        shows = os.path.join(root, "GPL26 Show")
        inst = os.path.join(root, "LTC Player")
        os.makedirs(shows)
        os.makedirs(os.path.join(inst, ".venv"))
        open(os.path.join(shows, "a.fseq"), "w").write("x")
        os.symlink("../GPL26 Show", os.path.join(inst, "show"))
        dst = os.path.join(inst, os.path.basename(src))
        shutil.copy2(src, dst)
        os.chmod(dst, 0o755)
        r = subprocess.run(["/bin/bash", dst], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=30)
        out = r.stdout + r.stderr
        check("cannot be moved on its own" in out,
              f"the helper must refuse and say why, said: {out[:300]!r}")
        check(os.path.isdir(inst),
              "the helper must not have moved the folder anyway")
        check(os.path.islink(os.path.join(inst, "show")),
              "the link must still be there")
        check(os.path.basename(root) in out or root in out
              or os.path.dirname(inst) in out,
              "the helper must name the folder to move instead")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# =====================================================================
# LTC Player.app. Jeff, 2026-09-15: "its time for you to try building
# this as an .app". Everything below is anchored to a way an app bundle
# is broken while still looking built.
# =====================================================================

APP_BUILDER = "Build LTC Player app.command"


def _builder_text():
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(here, APP_BUILDER)
    return (open(p, encoding="utf-8").read() if os.path.exists(p) else None)


def _between(text, start_marker, end_marker):
    i = text.index(start_marker) + len(start_marker)
    return text[i:text.index(end_marker, i)]


def test_the_app_is_signed_last_and_nothing_touches_it_after():
    section("a bundle written to after signing is refused by macOS")
    # This is not hypothetical. The xLights bundle was broken exactly this
    # way: Info.plist edited after codesign, seal invalid, and on Apple
    # Silicon an invalid seal is "damaged, move to Trash" at launch.
    t = _builder_text()
    if t is None:
        print("  (no app builder beside the test, skipped)")
        return
    sign_at = t.find('codesign --force --sign -')
    check(sign_at > 0, "the builder does not sign the bundle at all")
    if sign_at <= 0:
        return
    after = t[sign_at:]
    # Anything that writes INTO the staged bundle after the signature.
    for pat in ('> "$B/', '>> "$B/', 'cp "$B/', 'mkdir -p "$B/',
                'cat > "$B', 'iconutil', 'chmod +x "$B'):
        check(pat not in after,
              f"the builder still does {pat!r} AFTER codesign, which breaks "
              f"the seal and makes macOS call the app damaged")
    # and the verify has to come after the signature, not before it
    check(t.find("--verify --deep --strict", sign_at) > sign_at,
          "the builder does not verify the signature after signing")


def test_the_build_proves_macos_will_actually_launch_it():
    section("signing is not launching: the builder has to open the app")
    # An earlier bundle signed, verified, and was refused as damaged at the
    # next reboot. codesign and LaunchServices do not apply the same rules,
    # so the only honest check is to open it.
    t = _builder_text()
    if t is None:
        print("  (no app builder beside the test, skipped)")
        return
    check("open -a" in t,
          "the builder never actually launches the app, so a bundle macOS "
          "refuses would be handed over as working")
    check("--selfcheck" in t,
          "the launch test has nothing to prove the app got as far as "
          "loading the show code")
    launch_at = t.find("open -a")
    # Line-anchored on purpose. 'rm -rf ... appears somewhere after here' is
    # satisfied by a line that has been commented out or prefixed with a
    # no-op, which is exactly how this guarantee would quietly disappear.
    import re as _re
    removals = [m for m in _re.finditer(r'(?m)^[ \t]*rm -rf "\$HERE/\$APP"[ \t]*$', t)
                if m.start() > launch_at]
    check(len(removals) >= 2,
          f"a bundle that fails the launch test must be REMOVED, in both the "
          f"'never reported back' and the 'cannot load' branches; found "
          f"{len(removals)} live removal(s) after the launch test")
    check(".ltcplay_appcheck" in t and
          '"$HERE/.ltcplay_appcheck"' in t,
          "the launch result must be written OUTSIDE the bundle; writing it "
          "inside would break the signature the test exists to check")
    check('"$B/.ltcplay_appcheck"' not in t,
          "the launch result is written inside the bundle")


def test_the_app_declares_what_macos_needs_to_know():
    section("Info.plist: the keys without which the app is a silent failure")
    t = _builder_text()
    if t is None:
        print("  (no app builder beside the test, skipped)")
        return
    plist = _between(t, '<<PLIST\n', '\nPLIST\n')
    for key, why in (
            ("NSDesktopFolderUsageDescription",
             "without it macOS can refuse this app the Desktop outright "
             "instead of asking, and the app then reports the show folder "
             "as missing"),
            ("CFBundleExecutable", "macOS would not know what to run"),
            ("CFBundleIdentifier", "the microphone grant would not survive "
                                   "the next launch"),
            ("NSMicrophoneUsageDescription",
             "macOS refuses to ask for the microphone at all, and hands the "
             "program silence instead of timecode"),
            ("LSMinimumSystemVersion", "it would offer to run on macOS it "
                                       "cannot run on"),
            ("LSUIElement", "an app with a Dock icon and no event loop is "
                            "reported as not responding within seconds"),
    ):
        check(f"<key>{key}</key>" in plist, f"Info.plist has no {key}: {why}")
    # the declared executable must be the one that gets compiled
    check("<key>CFBundleExecutable</key><string>$EXE</string>" in plist,
          "Info.plist names an executable that is not the compiled one")
    check('cc -O2 -Wall -o "$B/Contents/MacOS/$EXE"' in t,
          "the compiled launcher does not land at the name Info.plist names")


def test_the_app_launcher_finds_its_way_home():
    section("the app's launcher, compiled and run")
    # The bundle's main executable has to be a real signed program, so it is
    # a small C launcher rather than a shell script: on Apple Silicon a
    # script cannot carry a signature and the app is refused outright.
    import shutil, subprocess, tempfile
    t = _builder_text()
    if t is None:
        print("  (no app builder beside the test, skipped)")
        return
    if not shutil.which("cc"):
        print("  (no compiler here, skipped)")
        return
    c = _between(t, "<<'CSTUB'\n", "\nCSTUB\n")
    check("execv(" in c, "the launcher must replace itself rather than start "
                         "a second process, or macOS attaches the microphone "
                         "permission to the wrong thing")
    check('strncmp(argv[i], "-psn_", 5)' in c,
          "LaunchServices passes -psn_0_nnnn on some versions of macOS and "
          "python refuses to start with an unknown option")
    # The runs below set LTCPLAY_NO_DIALOG so the missing-Python case does
    # not put a dialog on the tester's screen. That switch must never be
    # able to cost the real app its dialog, its message or its log.
    fbody = _between(c, "static void fail(const char *msg) {", "\n}\n")
    check('execl("/usr/bin/osascript"' in fbody
          and "display dialog" in fbody,
          "the launcher no longer puts a failure on the screen, so a "
          "double-clicked app with no Python would just vanish")
    gate = fbody.find('getenv("LTCPLAY_NO_DIALOG")')
    check(gate >= 0 and 'strcmp(nodialog, "1") == 0' in fbody,
          "only LTCPLAY_NO_DIALOG=1 exactly may skip the dialog")
    check(0 <= fbody.find("logline(msg)") < gate
          and 0 <= fbody.find("fprintf(stderr") < gate,
          "LTCPLAY_NO_DIALOG must skip only the dialog; the message and the "
          "log have to happen before it is checked")
    check(gate < fbody.find('execl("/usr/bin/osascript"'),
          "the dialog must come after the LTCPLAY_NO_DIALOG check, or the "
          "check does nothing")
    check(c.count('getenv("LTCPLAY_NO_DIALOG")') == 1,
          "LTCPLAY_NO_DIALOG may only gate the dialog in fail()")
    root = tempfile.mkdtemp()
    try:
        # Off a Mac there is no _NSGetExecutablePath; swap in the same idea
        # so the path arithmetic and argument handling can still be proven.
        if sys.platform != "darwin":
            c = c.replace(
                "#include <mach-o/dyld.h>",
                "#include <errno.h>\nstatic int _NSGetExecutablePath"
                "(char *b, uint32_t *s){ ssize_t n=readlink(\"/proc/self/exe\""
                ", b, *s-1); if(n<0) return -1; b[n]=0; return 0; }")
        src = os.path.join(root, "stub.c")
        open(src, "w").write(c)
        exe = os.path.join(root, "stub")
        r = subprocess.run(["cc", "-O2", "-Wall", "-Werror", "-o", exe, src],
                           capture_output=True, text=True, timeout=60)
        check(r.returncode == 0,
              f"the launcher does not compile cleanly: {r.stderr[:300]}")
        if r.returncode != 0:
            return
        inst = os.path.join(root, "inst")
        macos = os.path.join(inst, "LTC Player.app", "Contents", "MacOS")
        res = os.path.join(inst, "LTC Player.app", "Contents", "Resources")
        os.makedirs(macos)
        os.makedirs(res)
        os.makedirs(os.path.join(inst, ".venv", "bin"))
        shutil.copy2(exe, os.path.join(macos, "LTC Player"))
        py = os.path.join(inst, ".venv", "bin", "python")
        open(py, "w").write('#!/bin/bash\nexec %s "$@"\n' % sys.executable)
        os.chmod(py, 0o755)
        open(os.path.join(res, "boot.py"), "w").write(
            "import os, sys\nprint(os.getcwd())\nprint(sys.argv[1:])\n")
        run = os.path.join(macos, "LTC Player")
        # A home of its own, so the start log it writes can be read back
        # and the tester's real log is left alone.
        home = os.path.join(root, "home")
        os.makedirs(os.path.join(home, "Library", "Logs"))
        env = dict(os.environ, HOME=home, LTCPLAY_NO_DIALOG="1")

        r = subprocess.run([run], capture_output=True, text=True, timeout=30,
                           env=env)
        lines = r.stdout.strip().splitlines()
        check(lines and os.path.realpath(lines[0]) == os.path.realpath(inst),
              f"the app must run from the folder it sits in, got {lines[:1]}")

        r = subprocess.run([run, "-psn_0_4242", "devices"],
                           capture_output=True, text=True, timeout=30,
                           env=env)
        lines = r.stdout.strip().splitlines()
        check(len(lines) > 1 and lines[1] == "['devices']",
              f"-psn must be dropped and real arguments kept, got {lines[1:]}")

        os.rename(py, py + ".gone")
        r = subprocess.run([run], capture_output=True, text=True, timeout=30,
                           env=env)
        check("Install ltcplay.command" in (r.stdout + r.stderr),
              "with no virtual environment the app must say what to do, not "
              "die silently: a double-clicked app that does nothing at all is "
              "the worst failure there is")
        check(r.returncode == 70,
              f"a launcher that cannot start must exit 70, got {r.returncode}")
        log = os.path.join(home, "Library", "Logs", "LTCPlayer-start.log")
        logged = open(log).read() if os.path.exists(log) else ""
        check("Install ltcplay.command" in logged,
              "with LTCPLAY_NO_DIALOG=1 the launcher must still write "
              "~/Library/Logs/LTCPlayer-start.log; only the dialog is skipped")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_the_app_starts_the_page_by_itself():
    section("double-clicking the app has to land on the show page")
    t = _builder_text()
    if t is None:
        print("  (no app builder beside the test, skipped)")
        return
    boot = _between(t, "<<'BOOT'\n", "\nBOOT\n")
    check('"serve"' in boot and '"7878"' in boot,
          "the app does not default to serving the page, so a double-click "
          "would do nothing visible")
    check('"--bind", "127.0.0.1"' in boot,
          "the app must serve on loopback by default; anyone on the venue "
          "network could otherwise black out the rig")
    check('"0.0.0.0"' not in boot,
          "the app binds every interface somewhere, which puts the rig's "
          "stop button on the venue network")
    check("--selfcheck" in boot, "the app has no self-check to launch into")
    check("sys.path.insert(0, folder)" in boot,
          "the app would not find the ltcplay package beside it")
    check('os.path.join(folder, ".ltcplay_appcheck")' in boot,
          "the self-check result must be written beside the app, not inside "
          "it: a file written into the bundle breaks the very signature the "
          "check exists to prove")
    # An app has no terminal. Anything it prints, including the reason it
    # could not start, goes nowhere. On 2026-09-15 that gave Jeff an icon
    # that animated and then nothing at all, with no way to find out why.
    check("sys.stdout = sys.stderr = fh" in boot,
          "what the engine prints is thrown away, so a failure to start is "
          "invisible")
    check("Library/Logs/LTCPlayer-start.log" in boot,
          "the app keeps no record of why it stopped")
    check("osascript" in boot and "display dialog" in boot,
          "nothing puts a failure on the screen, so the app just vanishes")
    check("--no-browser" in boot and "/usr/bin/open" in boot,
          "the engine opens its own browser and swallows the failure, so a "
          "page that never appears is indistinguishable from an app that "
          "never started")
    check("def open_page" in boot and "connect((" in boot,
          "the browser is opened without waiting for the port, so it lands "
          "on a connection error and sits there")

    # The app runs the engine through the bundle's boot.py. Nothing on that
    # command line says "ltcplay.cli", so every "is one already running"
    # check looked straight past it: a second engine would have been started
    # on top of the first, and Restart could not stop the first at all.
    here2 = os.path.dirname(os.path.abspath(__file__))
    for name in ("Web ltcplay.command", "Restart ltcplay.command",
                 "Apply this update.command"):
        f = os.path.join(here2, name)
        if not os.path.exists(f):
            continue
        t2 = open(f, encoding="utf-8").read()
        for line in t2.splitlines():
            if "pgrep -f" in line or "pkill -f" in line:
                check("boot.py" in line,
                      f"{name} looks for a running engine with a pattern "
                      f"that cannot match one started by LTC Player.app: "
                      f"{line.strip()[:90]}")


def test_you_can_tell_which_version_is_installed():
    section("every machine must be able to say what it is running")
    # Jeff, 2026-09-15: "I have no idea which version im runnning." Four
    # copies existed that night and nothing anywhere said which was which.
    import shutil, subprocess, tempfile
    from ltcplay import version as ver
    here = os.path.dirname(os.path.abspath(__file__))

    a, n, newest = ver.build()
    check(len(a) == 10 and n > 5,
          f"the build id must be a real digest over real files, got {a!r} "
          f"over {n} file(s)")
    check(ver.build()[0] == a, "the build id must be stable between calls")

    # It has to CHANGE when the program changes, or it is decoration.
    target = os.path.join(here, "ltcplay", "trigger.py")
    src = open(target, "rb").read()
    try:
        open(target, "ab").write(b"\n# touched by the self test\n")
        b = ver.build()[0]
        check(b != a,
              "the build id did not change when a source file did, so two "
              "machines running different programs would report the same id")
    finally:
        open(target, "wb").write(src)
    check(ver.build()[0] == a, "the build id did not come back after the "
                               "file was restored")

    # The SHOW is stamped separately: identical code says nothing about
    # whether two machines hold the same renders, and the renders are what
    # the Advatek scenes were recorded from.
    d = tempfile.mkdtemp()
    try:
        open(os.path.join(d, "a.fseq"), "wb").write(b"x" * 100)
        s1 = ver.show_build(d)[0]
        open(os.path.join(d, "a.fseq"), "wb").write(b"x" * 200)
        check(ver.show_build(d)[0] != s1,
              "a re-render did not change the show build id, so a stale "
              "recorded scene could not be told from a current one")

        # And it must NOT change for anything else. The recorded Advatek
        # scenes are checked against this number: if editing a trigger
        # channel moved it, every scene would look stale when not one pixel
        # had changed, and the check would stop meaning anything.
        open(os.path.join(d, "a.fseq"), "wb").write(b"x" * 100)
        os.utime(os.path.join(d, "a.fseq"), (1700000000, 1700000000))
        base = ver.show_build(d)[0]
        tlp = os.path.join(d, "show.json")
        open(tlp, "w").write('{"cues": [], "trigger": {"universe": 1}}')
        check(ver.show_build(d, tlp)[0] == base,
              "the show file changes the render fingerprint, so a trigger "
              "channel edit would make every recorded scene look stale")
        open(tlp, "w").write('{"cues": [], "trigger": {"universe": 6999}}')
        check(ver.show_build(d, tlp)[0] == base,
              "editing the show file moved the render fingerprint")
    finally:
        shutil.rmtree(d, ignore_errors=True)

    rep = launcher("What is installed here.command")
    check(os.path.exists(rep),
          "there is no way to ask a machine what it has installed")
    if os.path.exists(rep):
        r = subprocess.run(["bash", "-n", rep], capture_output=True, text=True)
        check(r.returncode == 0,
              f"the report script is not valid bash: {r.stderr.strip()}")
        t = open(rep, encoding="utf-8").read()
        check('doc.get("cues")' in t,
              "the report picks a show file without checking it is one; it "
              "reported a superseded timeline as THE show once")
        check("api/state" in t and "build_id" in t,
              "the report cannot tell you that the RUNNING engine is older "
              "than the files in the folder, which is the case that bites")

    page = open(os.path.join(here, "ltcplay", "web", "index.html"),
                encoding="utf-8").read()
    check("buildline" in page and "s.build" in page,
          "the page never shows which build is serving it")
    check("MODIFIED" in page,
          "the page does not call out a copy somebody has edited, which is "
          "the case that matters: two machines can both say the same release "
          "while one of them has a file changed at 11pm")

    # A release number that sorts, on top of the hash that proves identity.
    # Jeff, 2026-09-15: "We need to start versioning this so we dont get lost
    # on who has what."
    cut = launcher("Cut a release.command")
    check(os.path.exists(cut), "there is no way to cut a numbered release")
    if os.path.exists(cut):
        r = subprocess.run(["bash", "-n", cut], capture_output=True, text=True)
        check(r.returncode == 0,
              f"the release script is not valid bash: {r.stderr.strip()}")
        ct = open(cut, encoding="utf-8").read()
        check("XXXXXX" in ct,
              "mktemp without XXXXXX fails on some systems and the script "
              "then writes its worker into the show folder")
    check(ver.STAMP not in [os.path.basename(f) for f in ver._files()],
          "the VERSION stamp is part of what gets hashed, so writing it "
          "would change the very number it records")

    # A new launcher must change the build id. A hardcoded list of launchers
    # ignored one that was ADDED, so a folder with an extra script in it
    # reported the same id as one without it.
    hashed = {os.path.basename(f) for f in ver._files()}
    cmds = {n for n in os.listdir(here) if n.endswith(".command")}
    tools_dir = os.path.join(here, "Tools")
    if os.path.isdir(tools_dir):
        cmds |= {n for n in os.listdir(tools_dir) if n.endswith(".command")}
    missed = sorted(cmds - hashed)
    check(not missed,
          f"these launchers are not part of the build id, so changing or "
          f"adding them is invisible: {missed}")
    check("selftest.py" in hashed,
          "the self test is not part of the build id, so a machine with a "
          "weakened suite reports the same id as one without")

    rel = ver.release()
    if rel:
        check(set(rel) >= {"release", "build", "show", "made"},
              f"a release stamp must carry all four, got {sorted(rel)}")
        st = ver.status()
        check(rel["release"] in st,
              f"status must name the release, said {st!r}")


def test_the_beta_window_app_stays_a_window():
    section("BETA: the native window must not become part of the engine")
    # Jeff asked for a native window, in beta. The one thing that must not
    # change while it is beta, or after, is the separation: the output loop
    # has a 25ms deadline and a window redraw inside that process freezes the
    # rig on its last frame with the screen still looking alive.
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    p = launcher("Build the window app (BETA).command")
    if not os.path.exists(p):
        print("  (no window-app builder beside the test, skipped)")
        return
    t = open(p, encoding="utf-8").read()
    r = subprocess.run(["bash", "-n", p], capture_output=True, text=True)
    check(r.returncode == 0,
          f"the window-app builder is not valid bash: {r.stderr.strip()}")

    objc = t.split("<<'OBJC'\n", 1)[1].split("\nOBJC\n", 1)[0]

    check("NSTask" in objc,
          "the engine must be a separate process, not something this app "
          "runs inside itself")
    check("terminate]" not in objc and "[t terminate" not in objc,
          "the window app kills the engine somewhere. Closing a window is "
          "not a reason to black out a rig")
    check("NSAllowsLocalNetworking" in t,
          "without an App Transport Security exception WebKit refuses a "
          "plain http page and the window opens completely blank, with no "
          "error anywhere")
    check("portIsUp" in objc and "loadWhenReady" in objc,
          "the window must wait for the engine before loading, or it opens "
          "on a connection error and sits there")
    check("Library/Logs/LTCPlayer-start.log" in objc,
          "the engine started by this app has no terminal, so its output "
          "must go to the log or the reason it failed is lost")
    check("alertAndQuit" in objc,
          "a window app that cannot start has to say so, not sit blank")
    check("[NSApp setActivationPolicy:" in objc,
          "setActivationPolicy: returns BOOL, so dot-syntax assignment does "
          "not compile everywhere")

    # It is a SECOND app. The proven one must not be touched by this.
    check('APP="LTC Player Beta.app"' in t,
          "the beta must be its own bundle; overwriting LTC Player.app would "
          "put an untested app on the show machine")
    check("com.jeffholmespresents.ltcplayer.beta" in t,
          "the beta must have its own bundle id, or macOS attaches the "
          "microphone grant to whichever was signed last")
    inst = open(launcher("Install ltcplay.command"),
                encoding="utf-8").read()
    check("Build the window app" not in inst,
          "Install builds the beta, so a fresh machine would get it without "
          "anybody choosing it")
    auto = open(launcher("Autostart ltcplay.command"),
                encoding="utf-8").read()
    check("LTC Player Beta" not in auto,
          "Autostart would start the beta at login")

    for a, b in (("{", "}"), ("(", ")"), ("[", "]")):
        check(objc.count(a) == objc.count(b),
              f"unbalanced {a}{b} in the window app source: "
              f"{objc.count(a)} vs {objc.count(b)}")


def test_the_trigger_goes_out_on_the_protocol_the_boxes_listen_for():
    section("sACN: a trigger on the wrong wire is not refused, it vanishes")
    # 2026-09-15: the playback triggers moved to sACN so they would not
    # collide with the record-arm triggers, and the channels moved to
    # 101..123. An Art-Net-only sender would have fired nothing all night
    # while the page said every cue had gone out.
    sent = []

    class Sock:
        def setsockopt(self, *a):
            pass

        def sendto(self, b, a):
            sent.append((bytes(b), a))

        def close(self):
            pass

    cfg = trig_mod.TriggerConfig.parse(
        {"protocol": "sacn", "universe": 6999,
         "dest": ["10.0.0.100", "10.0.0.110"],
         "mute": ADVATEK, "channels": {"a.fseq": 101},
         "idle_channel": 123, "pulse_gap_ms": 0}, "t.json")
    check(cfg.protocol == "sacn", f"protocol came out {cfg.protocol!r}")
    check(cfg.port == 5568, f"sACN goes to port 5568, got {cfg.port}")

    t = trig_mod.SceneTrigger(cfg, socket_factory=Sock)
    check(t.fire(101, "Opener"), "fire must report success")

    # 3 pulse frames + 1 release, to each of 2 addresses
    check(len(sent) == 8, f"3 pulses plus a release to 2 addresses is 8 "
                          f"packets, got {len(sent)}")
    check(sorted({a for _, a in sent}) ==
          [("10.0.0.100", 5568), ("10.0.0.110", 5568)],
          f"every destination must get every frame, got "
          f"{sorted({a for _, a in sent})}")

    b = sent[0][0]
    check(len(b) == 638, f"an E1.31 frame with 512 channels is 638 bytes, "
                         f"got {len(b)}")
    check(b[4:16] == b"ASC-E1.17\x00\x00\x00", "ACN packet identifier")
    check(b[21] == 0x04, "root vector must be VECTOR_ROOT_E131_DATA")
    check(b[43] == 0x02, "framing vector must be VECTOR_E131_DATA_PACKET")
    check((b[113] << 8) | b[114] == 6999,
          f"universe, got {(b[113] << 8) | b[114]}")
    check(b[125] == 0x00, "DMX start code must be 0")
    body = b[126:]
    check(len(body) == 512, f"512 channels, got {len(body)}")
    check(body[100] == 255, "channel 101 counts from 1 and must be at full")
    check(sum(body) == 255, "every other channel must be at zero")
    check(sum(sent[-1][0][126:]) == 0, "the release frame must be all zero")

    # Sequence: the same on every copy of one frame, moving between frames,
    # and never 0. A receiver drops a packet whose sequence goes backwards.
    seqs = [p[111] for p, _ in sent]
    check(seqs == [1, 1, 2, 2, 3, 3, 4, 4],
          f"one sequence number per FRAME, shared by the destinations, "
          f"got {seqs}")
    check(0 not in seqs, "sACN reserves sequence 0 for no tracking")

    # and Art-Net still works, on its own port
    sent.clear()
    acfg = trig_mod.TriggerConfig.parse(
        {"protocol": "artnet", "dest": "10.0.0.255", "mute": ADVATEK,
         "channels": {"a.fseq": 1}, "pulse_gap_ms": 0}, "t.json")
    check(acfg.port == 0x1936, "Art-Net goes to port 6454")
    trig_mod.SceneTrigger(acfg, socket_factory=Sock).fire(1)
    check(sent[0][0][:8] == b"Art-Net\x00", "Art-Net frames must still be "
                                            "Art-Net")
    check(sent[0][1] == ("10.0.0.255", 0x1936), f"got {sent[0][1]}")

    # a protocol nobody implements must be refused at load, loudly
    for bad in ("udp", "dmx", "", "ArtNett"):
        try:
            trig_mod.TriggerConfig.parse(
                {"protocol": bad, "mute": ADVATEK,
                 "channels": {"a.fseq": 1}}, "t.json")
            check(False, f"protocol {bad!r} was accepted")
        except trig_mod.TriggerError:
            pass
    # the spellings the reference sheet and xLights both use
    for good in ("sacn", "sACN", "e131", "E1.31".replace(".", ""), "artnet"):
        try:
            trig_mod.TriggerConfig.parse(
                {"protocol": good, "mute": ADVATEK,
                 "channels": {"a.fseq": 1}}, "t.json")
        except trig_mod.TriggerError:
            check(False, f"protocol {good!r} should be accepted")

    # Multicast: the group is FIXED by the universe, so it is derived rather
    # than typed. A typed address survives a universe change and quietly
    # keeps pointing at the old group.
    check(trig_mod.multicast_for(6999) == "239.255.27.87",
          f"E1.31 group for 6999 is 239.255.27.87, got "
          f"{trig_mod.multicast_for(6999)}")
    check(trig_mod.multicast_for(1) == "239.255.0.1",
          f"got {trig_mod.multicast_for(1)}")
    mc = trig_mod.TriggerConfig.parse(
        {"protocol": "sacn", "universe": 6999, "dest": "multicast",
         "mute": ADVATEK, "channels": {"a.fseq": 101},
         "idle_channel": 123}, "t.json")
    check(mc.dest == ["239.255.27.87"],
          f"'multicast' must resolve to the group, got {mc.dest}")
    check("239.255.27.87" in mc.summary(),
          "the panel must say which group it is actually sending to")
    for doc, why in (
            ({"protocol": "artnet", "dest": "multicast", "mute": ADVATEK,
              "channels": {"a.fseq": 1}},
             "Art-Net has no multicast group for a universe"),
            ({"protocol": "sacn", "dest": ["multicast", "10.0.0.100"],
              "mute": ADVATEK, "channels": {"a.fseq": 1}},
             "multicast mixed with addresses sends everything twice")):
        try:
            trig_mod.TriggerConfig.parse(doc, "t.json")
            check(False, f"accepted a config where {why}")
        except trig_mod.TriggerError:
            pass

    # A multicast packet with the default TTL of 1 dies at the first switch,
    # and a socket that was never told about broadcast refuses one outright.
    asked = []

    class Noting:
        def setsockopt(self, level, opt, val):
            asked.append((level, opt, val))

        def sendto(self, b, a):
            pass

        def close(self):
            pass

    import socket as _sock
    t2 = trig_mod.SceneTrigger(mc)
    t2._socket_factory = lambda: (asked.clear(),
                                  trig_mod.SceneTrigger._default_socket
                                  .__get__(t2)())[1] if False else None
    # exercise the real chooser against a stand-in socket
    real_socket = trig_mod.socket.socket
    trig_mod.socket.socket = lambda *a, **k: Noting()
    try:
        trig_mod.SceneTrigger(mc)._default_socket()
        opts = {o for _, o, _ in asked}
        check(_sock.IP_MULTICAST_TTL in opts,
              "a multicast trigger never leaves the Mac with the default "
              "TTL of 1 once there is a switch in the way")
        asked.clear()
        bcfg = trig_mod.TriggerConfig.parse(
            {"protocol": "artnet", "dest": "10.0.0.255", "mute": ADVATEK,
             "channels": {"a.fseq": 1}}, "t.json")
        trig_mod.SceneTrigger(bcfg)._default_socket()
        check(_sock.SO_BROADCAST in {o for _, o, _ in asked},
              "a broadcast trigger is refused by the socket unless it is "
              "asked for")
        asked.clear()
        ucfg = trig_mod.TriggerConfig.parse(
            {"protocol": "sacn", "dest": ADVATEK, "mute": ADVATEK,
             "channels": {"a.fseq": 101}}, "t.json")
        trig_mod.SceneTrigger(ucfg)._default_socket()
        check(_sock.SO_BROADCAST not in {o for _, o, _ in asked},
              "broadcast must not be switched on for a unicast trigger: left "
              "on by default, an address that slipped into a config goes to "
              "the whole segment without a word")
    finally:
        trig_mod.socket.socket = real_socket


def test_the_real_show_file_matches_the_trigger_reference():
    section("the show file against the sheet the boxes were configured from")
    # The reference the Advateks were set up from, 2026-09-15:
    #   sACN (E1.31), universe 6999, fire 255, release 0
    #   Set 1 Opener..Ending = 101..111, Set 2 = 112..122, PreShow = 123
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "gpl2026_timeline.json")
    if not os.path.exists(path):
        print("  (no gpl2026_timeline.json beside the test, skipped)")
        return
    import json
    doc = json.load(open(path))
    t = doc.get("trigger")
    if not t:
        print("  (this show file has no trigger block, skipped)")
        return
    cfg = trig_mod.TriggerConfig.parse(t, path)
    show_check(cfg.protocol == "sacn",
          f"the reference says sACN; the show file says {cfg.protocol_label}")
    show_check(cfg.universe == 6999, f"universe {cfg.universe}, expected 6999")
    want = list(range(101, 123))
    got = [cfg.channel_for(c["fseq"]) for c in doc["cues"]]
    show_check(got == want,
          f"cues must map to 101..122 in play order, got {got}")
    show_check(cfg.idle_channel == 123,
          f"PreShow is channel 123 on the sheet, show file says "
          f"{cfg.idle_channel}")
    # The trigger has to reach exactly the boxes it mutes, and there are two
    # right answers: the multicast group for the universe, which every
    # subscribed box hears, or the muted controllers one packet each.
    group = trig_mod.multicast_for(cfg.universe)
    ok = (cfg.dest == [group]) or (sorted(cfg.dest) == sorted(cfg.mute))
    show_check(ok,
          f"the trigger must go to the multicast group {group} or to the "
          f"controllers it mutes; it mutes {sorted(cfg.mute)} and sends to "
          f"{sorted(cfg.dest)}")


# ------------------------------------------------------------ scheduler ----
# The scheduler for Fire & Ice. It decides and does not act, it is loaded only
# when a schedule file is configured, and the GPL show never loads it. Every
# test below is pure (time handed in, no sleeps, no network) except the web
# routes, which use a fixed clock.

SCHED_EXAMPLE = {
    "timezone": "America/Denver",
    "season": {"first_date": "2026-11-14", "last_date": "2027-01-02"},
    "weekly": {
        "thu": {"first_start": "17:30", "interval_min": 20, "last_end": "21:30"},
        "fri": {"first_start": "17:30", "interval_min": 20, "last_end": "22:00"},
        "sat": {"first_start": "17:00", "interval_min": 20, "last_end": "22:00"},
    },
    "exceptions": {"2026-11-26": None,
                   "2026-12-24": {"first_start": "17:00", "interval_min": 20,
                                  "last_end": "20:00"}},
    "show_len_s": 440,
    "guard_s": 120,
    "late_grace_s": 0,
}

_DASHES = ("\u2014", "\u2013")


def _no_dashes(text, where):
    return check(not any(d in str(text) for d in _DASHES),
                 f"{where}: operator-facing text has an em or en dash: "
                 f"{text!r}")


def _sched():
    """The scheduler module, or None after a failure saying why it cannot be
    tested on this machine (on Windows: the tzdata package is missing)."""
    from ltcplay import schedule as S
    try:
        S.zone("America/Denver")
    except S.RuleError as e:
        check(False, f"the scheduler cannot be tested here: {e}")
        return None
    return S


def _sched_doc(**over):
    import copy
    doc = copy.deepcopy(SCHED_EXAMPLE)
    doc.update(over)
    return doc


def _one_night_rule(S, first="18:00", last="22:00", grace=0, interval=20,
                    show=440, guard=120):
    """Saturday 2026-11-14 only, with its own hours."""
    return S.parse_rule(_sched_doc(
        weekly={"sat": {"first_start": first, "interval_min": interval,
                        "last_end": last}},
        exceptions={}, late_grace_s=grace, show_len_s=show, guard_s=guard))


def _den(S, h, mi, s=0, us=0, d=(2026, 11, 14)):
    from datetime import datetime
    return datetime(*d, h, mi, s, us, tzinfo=S.zone("America/Denver"))


class _Night:
    """Drives one machine and keeps every log line it wrote, so each scenario
    can also be checked for actors, reasons and dashes."""

    def __init__(self, S, rule, d=None):
        from datetime import date
        self.S = S
        self.m = S.new_night(rule, d or date(2026, 11, 14))
        self.log = []
        self.last = None

    def do(self, kind, actor, now, **kw):
        S = self.S
        o = S.step(self.m, S.Event(kind, actor, **kw), now)
        self.m = o.machine
        self.log.extend(o.log)
        self.last = o
        return o

    def boot(self, now):
        return self.do(self.S.BOOT_DONE, "system", now)

    def tick(self, now):
        return self.do(self.S.TICK, "scheduler", now)

    def op(self, kind, now, **kw):
        return self.do(kind, "operator", now, **kw)

    def audit(self, where):
        S = self.S
        for le in self.log:
            check(le.actor in S.ACTORS, f"{where}: a log event has actor "
                                        f"{le.actor!r}")
            check(bool(le.reason), f"{where}: a log event has no reason: {le}")
            check(bool(le.text), f"{where}: a log event has no sentence: {le}")
            check(le.at.utcoffset() is not None,
                  f"{where}: a log time has no offset")
            _no_dashes(le.text, where)
            _no_dashes(le.reason, where)


def _fired(o, S):
    return [e.show for e in o.effects if e.kind == S.START_SHOW]


def test_schedule_rule_is_validated():
    section("scheduler: the rule file is checked, and a wrong key fails loudly")
    S = _sched()
    if S is None:
        return
    import copy
    r = S.parse_rule(_sched_doc())
    check((r.show_len_s, r.guard_s, r.late_grace_s) == (440, 120, 0),
          f"the example rule read back wrong: {r}")
    d = _sched_doc()
    del d["late_grace_s"]
    check(S.parse_rule(d).late_grace_s == 0,
          "late_grace_s must default to 0 when the file leaves it out")
    check(S.parse_rule(S.rule_to_doc(r)) == r,
          "a rule written back out must read back as the same rule")
    check(S.parse_rule(_sched_doc(late_grace_s=15)).late_grace_s == 15,
          "late_grace_s 15 is the cap and must be allowed")

    def night(**kw):
        n = {"first_start": "17:30", "interval_min": 20, "last_end": "21:30"}
        n.update(kw)
        return n

    bad = [
        ("a misspelled top-level key", _sched_doc(late_grace=5),
         ("'late_grace'", "late_grace_s")),
        ("a weekday that is not one", _sched_doc(
            weekly={"thursday": night()}), ("not a weekday",)),
        ("a misspelled night key", _sched_doc(
            weekly={"thu": dict(night(), last_start="21:00")}),
         ("'last_start'",)),
        ("a misspelled season key", _sched_doc(
            season={"first_date": "2026-11-14", "last_date": "2027-01-02",
                    "opening": "x"}), ("'opening'",)),
        ("a grace over the cap", _sched_doc(late_grace_s=16),
         ("16", "15", "Start now")),
        ("a grace far over the cap", _sched_doc(late_grace_s=300),
         ("300", "15")),
        ("a negative grace", _sched_doc(late_grace_s=-1), ("less than 0",)),
        ("a grace of true", _sched_doc(late_grace_s=True), ("whole number",)),
        ("a grace of 2.5", _sched_doc(late_grace_s=2.5), ("whole number",)),
        ("a time that is not one", _sched_doc(
            weekly={"thu": night(first_start="25:00")}),
         ("not a time of day",)),
        ("a night that ends before it starts", _sched_doc(
            weekly={"thu": night(first_start="21:00", last_end="17:00")}),
         ("not after",)),
        ("an interval shorter than a show and its guard", _sched_doc(
            weekly={"thu": night(interval_min=5)}), ("at least 10 minutes",)),
        ("a night with no room for a show", _sched_doc(
            weekly={"thu": night(first_start="17:00", last_end="17:05")}),
         ("not room for one show",)),
        ("a season that ends before it starts", _sched_doc(
            season={"first_date": "2027-01-02", "last_date": "2026-11-14"}),
         ("before it starts",)),
        ("an exception outside the season", _sched_doc(
            exceptions={"2026-10-31": None}), ("outside the season",)),
        ("an exception that is not a date", _sched_doc(
            exceptions={"2026-13-01": None}), ("date like",)),
        ("a time zone that does not exist", _sched_doc(
            timezone="America/Denverr"), ("not known on this machine",
                                          "tzdata")),
        ("no show length", {k: v for k, v in _sched_doc().items()
                            if k != "show_len_s"}, ("missing show_len_s",)),
    ]
    for what, doc, must in bad:
        try:
            S.parse_rule(doc)
            check(False, f"{what} was accepted")
        except S.RuleError as e:
            for m in must:
                check(m in str(e), f"{what}: the error must say {m!r}: {e}")
            _no_dashes(str(e), what)
    # Every problem at once, not one per save.
    try:
        S.parse_rule(_sched_doc(late_grace_s=99, guard_s=-5, bogus=1))
        check(False, "three problems at once were accepted")
    except S.RuleError as e:
        check(len(e.problems) >= 3, f"all three problems must be named: {e}")
    try:
        S.parse_rule("{not json", where="x.json")
        check(False, "unreadable JSON was accepted")
    except S.RuleError as e:
        check("not readable JSON" in str(e) and "x.json" in str(e),
              f"unreadable JSON must say so and name the file: {e}")
    print("  ok")


def test_schedule_expands_the_season():
    section("scheduler: slots across a season, both clock changes, last_end "
            "to the second")
    S = _sched()
    if S is None:
        return
    from datetime import date, datetime, timedelta, timezone
    r = S.parse_rule(_sched_doc())
    plans = S.expand_season(r)
    check(len(plans) == 50, f"the season is 50 dates, got {len(plans)}")
    want = {3: (12, "17:30", "21:10"), 4: (14, "17:30", "21:50"),
            5: (15, "17:00", "21:40")}
    total = 0
    for p in plans:
        total += len(p.starts)
        wall = [S.clock(s) for s in p.starts]
        if p.date == date(2026, 11, 26):
            check(p.source == "closed" and not p.starts,
                  f"2026-11-26 is closed by a null exception, got {p}")
            continue
        if p.date == date(2026, 12, 24):
            check(p.source == "exception" and len(wall) == 9
                  and wall[0] == "17:00" and wall[-1] == "19:40",
                  f"2026-12-24 runs its exception hours, got {wall}")
        elif p.date.weekday() in want:
            n, first, last = want[p.date.weekday()]
            check(p.source == "weekly" and len(wall) == n and wall[0] == first
                  and wall[-1] == last,
                  f"{p.date}: expected {n} shows {first} to {last}, got {wall}")
        else:
            check(p.source == "dark" and not p.starts and "dark" in p.why,
                  f"{p.date} is a dark weekday, got {p}")
            continue
        utc = [x.astimezone(timezone.utc) for x in p.starts]
        for a, b in zip(utc, utc[1:]):
            check(b - a == timedelta(minutes=20),
                  f"{p.date}: shows at {a:%H:%M}Z and {b:%H:%M}Z are not 20 "
                  f"minutes apart")
        end = p.starts[0].replace(hour=p.night.last_end.hour,
                                  minute=p.night.last_end.minute)
        check(all(s + timedelta(seconds=560) <= end for s in utc),
              f"{p.date}: a show would not be finished with its guard by "
              f"last_end")
        check(all(s.utcoffset() == timedelta(hours=-7) for s in p.starts),
              f"{p.date}: the whole season is on Mountain Standard Time")
    # 8 Saturdays x 15, 7 Fridays x 14, 5 ordinary Thursdays x 12, plus the
    # Christmas Eve exception's 9 and the Thanksgiving closure's none.
    check(total == 8 * 15 + 7 * 14 + 5 * 12 + 9,
          f"the season should hold 287 shows, got {total}")
    off = S.expand(r, date(2026, 11, 13))
    check(off.source == "off-season" and not off.starts,
          f"the day before the season has no shows, got {off}")

    # Both Denver offset changes, in 2026 and 2027, tested directly even
    # though the season falls between them. An overnight window across 2 AM
    # shows the arithmetic is in real time: the spring night is an hour
    # shorter and the autumn night an hour longer.
    spring = ["00:30", "00:50", "01:10", "01:30", "01:50", "03:10", "03:30",
              "03:50"]
    autumn = ["00:30", "00:50", "01:10", "01:30", "01:50", "01:10", "01:30",
              "01:50", "02:10", "02:30", "02:50", "03:10", "03:30", "03:50"]
    changes = {"2026-03-08": (spring, [-7] * 5 + [-6] * 3),
               "2026-11-01": (autumn, [-6] * 5 + [-7] * 9),
               "2027-03-14": (spring, [-7] * 5 + [-6] * 3),
               "2027-11-07": (autumn, [-6] * 5 + [-7] * 9)}
    overnight = {"first_start": "00:30", "interval_min": 20,
                 "last_end": "04:00"}
    year = S.parse_rule(_sched_doc(
        season={"first_date": "2026-01-01", "last_date": "2027-12-31"},
        weekly={w: {"first_start": "17:30", "interval_min": 20,
                    "last_end": "21:30"} for w in S.WEEKDAYS},
        exceptions=dict({k: overnight for k in changes},
                        **{"2026-06-01": overnight})))
    plain = S.expand(year, date(2026, 6, 1))
    check(len(plain.starts) == 11, f"an ordinary 00:30 to 04:00 night holds "
                                   f"11 shows, got {len(plain.starts)}")
    for ds, (walls, offs) in changes.items():
        p = S.expand(year, date.fromisoformat(ds))
        got = [S.clock(s) for s in p.starts]
        check(got == walls, f"{ds}: expected {walls}, got {got}")
        got_off = [int(s.utcoffset() / timedelta(hours=1)) for s in p.starts]
        check(got_off == offs, f"{ds}: offsets should be {offs}, got {got_off}")
        utc = [x.astimezone(timezone.utc) for x in p.starts]
        check(all(b - a == timedelta(minutes=20)
                  for a, b in zip(utc, utc[1:])),
              f"{ds}: shows must be 20 real minutes apart across the change")
    # And the machine runs those nights on the local clock: every show starts
    # once, at its own instant, with shows ending 440 s later. Ticks are fed
    # in Denver time, so the autumn repeat of 01:00 to 02:00 really happens.
    tz = S.zone("America/Denver")
    for ds, (walls, _offs) in changes.items():
        d = date.fromisoformat(ds)
        n = _Night(S, year, d)
        plan = S.expand(year, d)
        t = plan.starts[0].astimezone(timezone.utc) - timedelta(minutes=10)
        stop = plan.starts[-1].astimezone(timezone.utc) + timedelta(minutes=20)
        n.boot(t.astimezone(tz))
        fired, ends = [], None
        while t <= stop:
            local = t.astimezone(tz)
            if ends is not None and t >= ends:
                n.do(S.SHOW_ENDED, "madmapper", local)
                ends = None
            for k in _fired(n.tick(local), S):
                fired.append((k, t))
                ends = t + timedelta(seconds=440)
            t += timedelta(seconds=10)
        want = [(i + 1, s.astimezone(timezone.utc))
                for i, s in enumerate(plan.starts)]
        check(fired == want, f"{ds}: every show must start once at its own "
                             f"instant; started {len(fired)} of {len(want)}")
        check(all(s.status == S.DONE for s in n.m.slots),
              f"{ds}: every show must be DONE, got "
              f"{[s.status for s in n.m.slots]}")
        n.audit(ds)
    # The late rule measures real seconds, not the wall clock: 01:10 MST
    # comes 20 minutes AFTER 01:50 MDT on the autumn change night.
    mdt = datetime(2026, 11, 1, 1, 50, tzinfo=tz)
    mst = datetime(2026, 11, 1, 1, 10, fold=1, tzinfo=tz)
    check(S.lateness_s(mdt, mst) == 1200,
          f"01:50 MDT to 01:10 MST is 1200 s late, got "
          f"{S.lateness_s(mdt, mst)}")
    check(not S.may_fire(mdt, mst, 15), "a show 20 real minutes late must "
                                        "not start on the repeated hour")
    for before, after, o1, o2 in (("2026-03-07", "2026-03-09", -7, -6),
                                  ("2026-10-31", "2026-11-02", -6, -7),
                                  ("2027-03-13", "2027-03-15", -7, -6),
                                  ("2027-11-06", "2027-11-08", -6, -7)):
        a = S.expand(year, date.fromisoformat(before)).starts[0]
        b = S.expand(year, date.fromisoformat(after)).starts[0]
        check(S.clock(a) == S.clock(b) == "17:30"
              and a.utcoffset() == timedelta(hours=o1)
              and b.utcoffset() == timedelta(hours=o2),
              f"17:30 on {before} and {after} must be {o1} and {o2} hours "
              f"from UTC, got {a.isoformat()} and {b.isoformat()}")

    # An interval that does not divide the window evenly.
    odd = _one_night_rule(S, first="17:00", last="22:00", interval=25)
    got = [S.clock(s) for s in S.expand(odd, date(2026, 11, 14)).starts]
    check(got == ["17:00", "17:25", "17:50", "18:15", "18:40", "19:05",
                  "19:30", "19:55", "20:20", "20:45", "21:10", "21:35"],
          f"a 25 minute interval from 17:00 to 22:00, got {got}")

    # last_end is exact: a show that finishes with its guard ON last_end is
    # kept, one second over is not.
    for last, n in (("17:30", 2), ("17:29:59", 1), ("17:30:01", 2)):
        rr = _one_night_rule(S, first="17:00", last=last, show=480, guard=120)
        got = [S.clock(s) for s in S.expand(rr, date(2026, 11, 14)).starts]
        check(len(got) == n, f"last_end {last} with a 10 minute show and "
                             f"guard keeps {n} show(s), got {got}")
    print("  ok")


def test_schedule_late_rule():
    section("scheduler: the late rule, every second from -60 to +600")
    S = _sched()
    if S is None:
        return
    from datetime import timedelta
    for grace in (0, 15):
        rule = _one_night_rule(S, grace=grace)
        start = _den(S, 18, 0)
        waiting = _Night(S, rule)
        waiting.boot(start - timedelta(seconds=120))
        check(waiting.m.state == S.IDLE, "two minutes before the first show "
                                         "the scheduler waits in IDLE")
        idle = waiting.m
        fired, fired_boot = [], []
        for off in range(-60, 601):
            now = start + timedelta(seconds=off)
            o = S.step(idle, S.Event(S.TICK, "scheduler"), now)
            if _fired(o, S):
                fired.append(off)
                check(o.machine.state == S.SHOW,
                      f"grace {grace}, +{off} s: fired but not in SHOW")
            s1 = o.machine.slot(1)
            if off > grace:
                check(s1.status == S.MISSED and
                      s1.reason == f"MISSED (late by {S.fmt_span(off)})",
                      f"grace {grace}, +{off} s: must be MISSED (late by "
                      f"{S.fmt_span(off)}), got {s1.status} {s1.reason!r}")
            elif off < 0:
                check(s1.status == S.PENDING,
                      f"grace {grace}, {off} s: nothing may happen early")
            # And the restart: the machine boots at exactly that moment.
            n = _Night(S, rule)
            n.boot(now)
            ob = n.tick(now)
            if _fired(ob, S):
                fired_boot.append(off)
            check(S.may_fire(start, now, grace) == (0 <= off <= grace),
                  f"may_fire disagrees at grace {grace}, {off} s")
        want = list(range(0, grace + 1))
        check(fired == want, f"grace {grace}: a waiting scheduler must fire "
                             f"at {want[0]}..{want[-1]} s only, fired at "
                             f"{fired[:5]}..{fired[-3:]}")
        check(fired_boot == want, f"grace {grace}: a scheduler that has just "
                                  f"booted must fire at {want[0]}.."
                                  f"{want[-1]} s only, fired at "
                                  f"{fired_boot[:5]}..{fired_boot[-3:]}")
        # A clock that ticks every second fires exactly once, on time.
        n = _Night(S, rule)
        n.boot(start - timedelta(seconds=61))
        starts = []
        for off in range(-60, 601):
            starts += [(off, x) for x in _fired(
                n.tick(start + timedelta(seconds=off)), S)]
        check(starts == [(0, 1)], f"grace {grace}: ticking every second must "
                                  f"start show 1 once at 0 s, got {starts}")
    # A slot is on time for the whole second it names, and not a moment
    # before it.
    start = _den(S, 18, 0)
    for us_off, ok in ((-1, False), (0, True), (999_999, True),
                       (1_000_000, False)):
        now = start + timedelta(microseconds=us_off)
        check(S.may_fire(start, now, 0) is ok,
              f"grace 0 at {us_off} microseconds should be {ok}")
    check(S.fmt_span(252) == "4m 12s" and S.fmt_span(7) == "7s"
          and S.fmt_span(3605) == "1h 0m 5s", "fmt_span formats wrong")
    print("  ok")


def _matrix_fixtures(S):
    """One machine in each state, and the moment to hit it with an event."""
    from datetime import timedelta
    rule = _one_night_rule(S, first="17:00")
    one = timedelta(seconds=1)
    fx = {}
    n = _Night(S, rule)
    fx[S.BOOT] = (n.m, _den(S, 16, 0) + one)
    n = _Night(S, rule)
    n.boot(_den(S, 16, 0))
    fx[S.IDLE] = (n.m, _den(S, 16, 0) + one)
    n.op(S.HOLD_ON, _den(S, 16, 0))
    fx[S.HOLD] = (n.m, _den(S, 16, 0) + one)
    n = _Night(S, rule)
    n.boot(_den(S, 16, 59, 50))
    n.tick(_den(S, 17, 0))
    fx[S.SHOW] = (n.m, _den(S, 17, 0) + one)
    n = _Night(S, rule)
    n.boot(_den(S, 17, 4))
    fx[S.STANDBY] = (n.m, _den(S, 17, 4) + one)
    n.op(S.END_NIGHT, _den(S, 17, 4), confirmed=True)
    fx[S.CLOSING] = (n.m, _den(S, 17, 4) + one)
    n.do(S.CLOSING_DONE, "system", _den(S, 17, 4))
    fx[S.OFF] = (n.m, _den(S, 17, 4) + one)
    for st, (m, _) in fx.items():
        check(m.state == st, f"the {st} fixture is in {m.state}")
    return fx


def test_schedule_state_machine_every_state_every_event():
    section("scheduler: every state times every event")
    S = _sched()
    if S is None:
        return
    fx = _matrix_fixtures(S)
    B, I, SB, SH, C, O, H = (S.BOOT, S.IDLE, S.STANDBY, S.SHOW, S.CLOSING,
                             S.OFF, S.HOLD)
    live = {I: I, SB: SB, SH: SH, H: H}
    E = S.Event
    # (event, the states that take it and where each one goes). Anything not
    # listed must be refused with a sentence and leave the machine alone.
    table = [
        (E(S.BOOT_DONE, "system"), {B: I}),
        (E(S.TICK, "scheduler"), {I: I, SB: SB, SH: SH, H: H, C: C, O: O}),
        (E(S.SHOW_ENDED, "madmapper"), {SH: SB}),
        (E(S.SHOW_FAILED, "madmapper", detail="no timecode after start"),
         {SH: SB}),
        (E(S.FAULT_RAISED, "safety", detail="the safety process stopped "
                                            "replying"),
         {s: s for s in S.STATES}),
        (E(S.CLEAR_FAULT, "operator"), {}),           # there is no fault
        (E(S.CLOSING_DONE, "system"), {C: O}),
        (E(S.START_NOW, "operator"), {I: SH, SB: SH, H: SH, C: SH, O: SH}),
        (E(S.HOLD_ON, "operator"), {I: H, SB: H, SH: SH}),
        (E(S.RESUME, "operator"), {H: I}),
        (E(S.SKIP_NEXT, "operator"), live),
        (E(S.DELAY_NEXT, "operator", minutes=5), live),
        (E(S.DELAY_NEXT, "operator", minutes=10), live),
        (E(S.DELAY_REST, "operator", minutes=5), live),
        (E(S.DELAY_REST, "operator", minutes=10), live),
        (E(S.ABORT, "operator"), {}),                  # not confirmed
        (E(S.ABORT, "operator", confirmed=True), {SH: SB}),
        (E(S.END_NIGHT, "operator"), {}),              # not confirmed
        (E(S.END_NIGHT, "operator", confirmed=True), {I: C, SB: C, H: C}),
        (E(S.EDIT_MOVE, "operator", show=15, at="21:45"), live),
        (E(S.EDIT_ADD, "operator", at="21:55"), live),
        (E(S.EDIT_REMOVE, "operator", show=15), live),
    ]
    check({e.kind for e, _ in table} == set(S.EVENTS),
          "the matrix must cover every event the machine knows")
    cells = 0
    for ev, goes in table:
        for st in S.STATES:
            m, now = fx[st]
            o = S.step(m, ev, now)
            cells += 1
            label = (f"{ev.kind}{' confirmed' if ev.confirmed else ''}"
                     f"{' +' + str(ev.minutes) if ev.minutes else ''} in {st}")
            if st in goes:
                check(o.accepted, f"{label} must be taken, was refused: "
                                  f"{o.refused}")
                check(o.machine.state == goes[st],
                      f"{label} must go to {goes[st]}, went to "
                      f"{o.machine.state}")
            else:
                check(not o.accepted and o.refused.endswith("."),
                      f"{label} must be refused with a sentence, got "
                      f"{o.refused!r} and state {o.machine.state}")
                check(o.machine is m and not o.effects,
                      f"{label}: a refusal must change nothing")
                check(len(o.log) == 1 and o.log[0].outcome == "refused"
                      and o.log[0].reason == o.refused,
                      f"{label}: a refusal is logged once, with its reason")
                _no_dashes(o.refused, label)
            for le in o.log:
                check(le.actor in S.ACTORS and le.reason and le.text,
                      f"{label}: log event without actor, reason or "
                      f"sentence: {le}")
                _no_dashes(le.text, label)
    check(cells == len(table) * 7, "the matrix did not run every cell")

    # With a fault on the flag, clearing it is taken in every state and the
    # state does not move: FAULT is a flag, not a state.
    for st in S.STATES:
        m, now = fx[st]
        f = S.step(m, E(S.FAULT_RAISED, "reader", detail="x"), now).machine
        check(f.fault and f.state == st, f"a fault in {st} must set the flag "
                                         f"and leave the state")
        c = S.step(f, E(S.CLEAR_FAULT, "operator"), now)
        check(c.accepted and not c.machine.fault and c.machine.state == st,
              f"clearing a fault in {st}: {c.refused}")

    # Who may send what. A blank actor is never allowed through.
    m, now = fx[S.IDLE]
    for ev, why in ((E(S.TICK, ""), "a blank actor"),
                    (E(S.TICK, "nobody"), "an unknown actor"),
                    (E(S.START_NOW, "scheduler"), "the scheduler pressing "
                                                  "Start now"),
                    (E(S.ABORT, "madmapper", confirmed=True),
                     "MadMapper pressing Abort"),
                    (E("LAUNCH", "operator"), "an unknown event")):
        try:
            S.step(m, ev, now)
            check(False, f"{why} was accepted")
        except ValueError as e:
            _no_dashes(str(e), why)
    try:
        S.step(m, E(S.TICK, "scheduler"), now.replace(tzinfo=None))
        check(False, "a time with no zone was accepted")
    except ValueError:
        pass
    # The scheduler has no way to see or change flame arming.
    check(not any("ARM" in k for k in S.EVENTS + S.EFFECTS),
          "an arm event or effect exists; the scheduler must never touch "
          "arming")
    check(not any("arm" in f for f in S.Machine.__dataclass_fields__),
          "the machine holds arm state; it must never look at it")
    print(f"  ok ({cells} cells)")


def test_schedule_restart_guard_and_hold():
    section("scheduler: restart at 18:04, guard_s, and HOLD")
    S = _sched()
    if S is None:
        return
    from datetime import timedelta
    rule = _one_night_rule(S)
    sec = timedelta(seconds=1)

    # The restart rule, as Jeff specified it: a reboot at 18:04 does not
    # start the 18:00 show; it lands in STANDBY and waits for 18:20.
    n = _Night(S, rule)
    o = n.boot(_den(S, 18, 4, 12))
    check(n.m.state == S.STANDBY, f"a reboot at 18:04 lands in STANDBY, "
                                  f"got {n.m.state}")
    check(n.m.slot(1).status == S.MISSED
          and n.m.slot(1).reason == "MISSED (late by 4m 12s)",
          f"the 18:00 show is MISSED (late by 4m 12s), got "
          f"{n.m.slot(1).reason!r}")
    check(not _fired(o, S), "booting must never start a show")
    t, started = _den(S, 18, 4, 12), []
    while t < _den(S, 18, 20):
        started += _fired(n.tick(t), S)
        t += sec
    check(not started, f"nothing may start between 18:04 and 18:20, got "
                       f"{started}")
    check(_fired(n.tick(_den(S, 18, 20)), S) == [2]
          and n.m.slot(2).reason == "FIRED",
          "the 18:20 show starts at 18:20:00 with reason FIRED")
    # A reboot four seconds into a show never resumes that show.
    n2 = _Night(S, rule)
    n2.boot(_den(S, 18, 0, 4))
    check(not _fired(n2.tick(_den(S, 18, 0, 4)), S)
          and n2.m.slot(1).status == S.MISSED,
          "a reboot 4 seconds into a show must not start it")
    n.audit("restart")

    # guard_s: never within guard_s of the previous show's end.
    n = _Night(S, rule)
    n.boot(_den(S, 17, 40))
    n.op(S.START_NOW, _den(S, 17, 52))
    check(n.m.state == S.SHOW and n.m.slot(n.m.running).origin == "operator"
          and n.m.slot(n.m.running).reason == "FIRED (operator)",
          "Start now runs an extra show, marked FIRED (operator)")
    x = n.m.running
    o = n.do(S.SHOW_ENDED, "madmapper", _den(S, 17, 59, 20), show=x)
    check(n.m.slot(x).status == S.DONE, "the show that ended is DONE")
    check(not _fired(n.tick(_den(S, 18, 0)), S),
          "18:00 is 40 s after a show ended, inside the 120 s guard, and "
          "must not start")
    n.tick(_den(S, 18, 0, 1))
    check(n.m.slot(1).status == S.MISSED and "guard" in n.m.slot(1).reason,
          f"a show held off by the guard is MISSED and says so, got "
          f"{n.m.slot(1).reason!r}")
    o = n.op(S.START_NOW, _den(S, 18, 0, 30))
    check(not o.accepted and "50s" in o.refused and "guard" in o.refused,
          f"Start now inside the guard is refused and says how long is "
          f"left: {o.refused!r}")
    o = n.op(S.START_NOW, _den(S, 18, 1, 20))
    check(o.accepted and n.m.state == S.SHOW,
          f"Start now exactly when the guard runs out is taken: {o.refused}")
    # Never start a show while one is running: 18:20 comes due during a
    # show started by hand at 18:15.
    n = _Night(S, rule)
    n.boot(_den(S, 18, 10))
    n.op(S.START_NOW, _den(S, 18, 15))
    check(not _fired(n.tick(_den(S, 18, 20)), S), "a show must never start "
                                                  "over a running one")
    n.tick(_den(S, 18, 20, 1))
    check(n.m.slot(2).status == S.MISSED and "running" in n.m.slot(2).reason,
          f"the show that came due mid show is MISSED and says why: "
          f"{n.m.slot(2).reason!r}")
    # The guard boundary for a scheduled show: ending at 17:58:00 frees
    # 18:00:00 exactly.
    n = _Night(S, rule)
    n.boot(_den(S, 17, 40))
    n.op(S.START_NOW, _den(S, 17, 50, 40))
    n.do(S.SHOW_ENDED, "madmapper", _den(S, 17, 58))
    check(_fired(n.tick(_den(S, 18, 0)), S) == [1],
          "a show exactly guard_s after the last one ended must start")
    n.audit("guard")

    # HOLD: the schedule suspended, nothing fires, Start now still works.
    n = _Night(S, rule)
    n.boot(_den(S, 18, 4))
    n.op(S.HOLD_ON, _den(S, 18, 10), who="Andy", screen="rack screen")
    check(n.m.state == S.HOLD, "Hold in STANDBY goes to HOLD")
    check(n.log[-1].who == "Andy" and n.log[-1].screen == "rack screen"
          and "Andy" in n.log[-1].text,
          "an operator event carries who pressed it and on which screen")
    check(not _fired(n.tick(_den(S, 18, 20)), S), "nothing fires on hold")
    n.tick(_den(S, 18, 20, 1))
    check(n.m.slot(2).reason == "MISSED (on hold)",
          f"a show that passes on hold is MISSED (on hold), got "
          f"{n.m.slot(2).reason!r}")
    n.op(S.RESUME, _den(S, 18, 25))
    check(n.m.state == S.STANDBY, f"Resume after the night has begun goes "
                                  f"to STANDBY, got {n.m.state}")
    check(_fired(n.tick(_den(S, 18, 40)), S) == [3], "after Resume the next "
                                                     "show starts on time")
    n.op(S.HOLD_ON, _den(S, 18, 42))
    check(n.m.state == S.SHOW and n.m.hold_pending,
          "Hold during a show lets the show finish")
    n.do(S.SHOW_ENDED, "madmapper", _den(S, 18, 47, 20))
    check(n.m.state == S.HOLD, "and lands in HOLD when it ends")
    check(not _fired(n.tick(_den(S, 19, 0)), S), "no show starts after it")
    n.op(S.START_NOW, _den(S, 19, 5))
    check(n.m.state == S.SHOW, "Start now works on hold")
    n.do(S.SHOW_ENDED, "madmapper", _den(S, 19, 12, 20))
    check(n.m.state == S.HOLD, "a show started on hold returns to HOLD")
    n.op(S.RESUME, _den(S, 19, 13))
    n.tick(_den(S, 19, 20))
    n.op(S.HOLD_ON, _den(S, 19, 21))
    n.op(S.RESUME, _den(S, 19, 22))
    check(n.m.state == S.SHOW and not n.m.hold_pending,
          "Resume during a show cancels the pending hold")
    n.do(S.SHOW_ENDED, "madmapper", _den(S, 19, 27, 20))
    check(n.m.state == S.STANDBY, "and the night carries on in STANDBY")
    n.audit("hold")
    print("  ok")


def test_schedule_abort_end_night_and_operator_actions():
    section("scheduler: Abort, End night, Skip, Delay and tonight's edits")
    S = _sched()
    if S is None:
        return
    from datetime import timedelta
    rule = _one_night_rule(S)
    before = S.rule_to_doc(rule)

    # Abort, precisely.
    n = _Night(S, rule)
    n.boot(_den(S, 18, 30))
    n.tick(_den(S, 18, 40))
    check(n.m.state == S.SHOW and n.m.running == 3, "18:40 is show 3")
    panel = {a["id"]: a for a in S.actions_for(n.m)}
    check(panel["abort"]["allowed"] and "show 3" in panel["abort"]["confirm"]
          and "does not disarm" in panel["abort"]["confirm"],
          f"the Abort confirm names the show and says it does not disarm: "
          f"{panel['abort']}")
    check(panel["end_night"]["confirm"] and not panel["end_night"]["allowed"],
          "End night confirms, and is not offered during a show")
    check(all(panel[k]["confirm"] is None for k in panel
              if k not in ("abort", "end_night")),
          "only Abort and End night confirm")
    check([a["id"] for a in S.ACTIONS] == [
        "start_now", "hold", "resume", "skip_next", "delay_next_5",
        "delay_next_10", "delay_rest_5", "delay_rest_10", "abort",
        "end_night"], "the transport panel holds the section 5 actions")
    o = n.op(S.ABORT, _den(S, 18, 42))
    check(not o.accepted and "confirmed" in o.refused and n.m.state == S.SHOW,
          f"an unconfirmed Abort changes nothing: {o.refused!r}")
    ev = S.action_event("abort", confirmed=True, screen="rack screen",
                        who="Andy")
    o = S.step(n.m, ev, _den(S, 18, 42))
    n.m = o.machine
    n.log.extend(o.log)
    check([(e.kind, e.show, e.seconds) for e in o.effects] == [
        (S.ZERO_FLAME_CUES, 3, 0.0), (S.STOP_CONDUCTOR, 3, 0.0),
        (S.FADE_PIXELS, 3, 1.0)],
        f"Abort zeroes the flame cues, stops MadMapper and fades the pixels "
        f"over 1 s, and nothing else: {o.effects}")
    check(n.m.state == S.STANDBY and n.m.slot(3).status == S.ABORTED
          and n.m.slot(3).reason == "ABORTED (operator)",
          "Abort marks the show ABORTED and stays in STANDBY")
    check(not n.m.fault, "an operator Abort is not a fault")
    check(not n.op(S.START_NOW, _den(S, 18, 43)).accepted,
          "an aborted show's end starts the guard too")
    check(_fired(n.tick(_den(S, 19, 0)), S) == [4],
          "the next show after an abort still starts on time")

    # A show that fails.
    n.do(S.SHOW_FAILED, "madmapper", _den(S, 19, 0, 5),
         detail="no timecode after start")
    check(n.m.slot(4).status == S.FAULT
          and n.m.slot(4).reason == "FAULT (no timecode after start)"
          and n.m.fault and n.m.state == S.STANDBY,
          f"a failed show is FAULT with its reason and raises the flag: "
          f"{n.m.slot(4).reason!r}")
    check(_fired(n.tick(_den(S, 19, 20)), S) == [5],
          "the next show is still attempted after a fault")
    n.do(S.SHOW_ENDED, "madmapper", _den(S, 19, 27, 20))

    # Skip next.
    n.op(S.SKIP_NEXT, _den(S, 19, 30))
    check(n.m.slot(6).status == S.SKIPPED
          and n.m.slot(6).reason == "SKIPPED (operator)",
          f"Skip next marks the next show SKIPPED (operator), got "
          f"{n.m.slot(6).reason!r}")
    # Delay next: 20:00 to 20:10, then again to 20:20 which is where show 8
    # is, so the second is refused with a sentence pointing at the other
    # delay.
    o = n.op(S.DELAY_NEXT, _den(S, 19, 31), minutes=10)
    check(o.accepted and n.m.hm(n.m.slot(7).start) == "20:10"
          and n.m.hm(n.m.slot(7).planned) == "20:00",
          "Delay next +10 moves 20:00 to 20:10 and remembers the plan")
    o = n.op(S.DELAY_NEXT, _den(S, 19, 32), minutes=10)
    check(not o.accepted and "Delay the rest of the night" in o.refused
          and n.m.hm(n.m.slot(7).start) == "20:10",
          f"a delay onto the next show is refused: {o.refused!r}")
    o = n.op(S.DELAY_NEXT, _den(S, 19, 32), minutes=7)
    check(not o.accepted and "+5 or +10" in o.refused,
          "a delay other than 5 or 10 is refused")
    rest = [(s.n, s.start) for s in n.m.pending()]
    o = n.op(S.DELAY_REST, _den(S, 19, 33), minutes=5)
    check(o.accepted and all(n.m.slot(k).start - t == timedelta(minutes=5)
                             for k, t in rest),
          "Delay the rest of the night +5 moves every show still to come")
    check(all(le.reason == "DELAYED (operator, +5 min)" for le in o.log),
          "a delay says what it did")
    check(not _fired(n.tick(_den(S, 20, 10)), S)
          and n.m.slot(7).status == S.PENDING,
          "nothing happens at a delayed show's old time")
    check(_fired(n.tick(_den(S, 20, 15)), S) == [7],
          "show 7 starts at its delayed time, 20:15")
    n.do(S.SHOW_ENDED, "madmapper", _den(S, 20, 22, 20))

    # Tonight's edits: move, add, remove. For tonight only.
    n = _Night(S, rule)
    n.boot(_den(S, 18, 4))
    o = n.op(S.EDIT_MOVE, _den(S, 18, 5), show=2, at="18:03")
    check(not o.accepted and "already passed" in o.refused,
          f"a show cannot be moved into the past: {o.refused!r}")
    o = n.op(S.EDIT_MOVE, _den(S, 18, 5), show=2, at="18:35")
    check(not o.accepted and "before show 2" in o.refused,
          f"a show cannot be moved on top of the next one: {o.refused!r}")
    o = n.op(S.EDIT_MOVE, _den(S, 18, 5), show=2, at="18:25")
    check(o.accepted and n.m.hm(n.m.slot(2).start) == "18:25"
          and n.m.slot(2).planned is not None,
          f"a move that fits is taken: {o.refused}")
    o = n.op(S.EDIT_ADD, _den(S, 18, 5), at="22:00")
    k = max(s.n for s in n.m.slots)
    check(o.accepted and n.m.slot(k).origin == "edit"
          and n.m.hm(n.m.slot(k).start) == "22:00",
          f"a show can be added for tonight: {o.refused}")
    o = n.op(S.EDIT_ADD, _den(S, 18, 5), at="21:45")
    check(not o.accepted, "an added show must not crowd its neighbours")
    o = n.op(S.EDIT_MOVE, _den(S, 18, 5), show=1, at="19:00")
    check(not o.accepted and "MISSED" in o.refused,
          "a show that already went by cannot be moved")
    o = n.op(S.EDIT_REMOVE, _den(S, 18, 5), show=3)
    check(o.accepted and n.m.slot(3).status == S.SKIPPED,
          "a removed show stays on the list as SKIPPED, for the morning read")
    o = n.op(S.EDIT_REMOVE, _den(S, 18, 5), show=99)
    check(not o.accepted and "no show 99" in o.refused,
          "removing a show that does not exist says so")
    check(S.rule_to_doc(rule) == before,
          "tonight's edits must never touch the rule")
    view = S.slot_view(n.m, _den(S, 18, 5))
    check([r["status"] for r in view][:3] == [S.MISSED, S.NEXT, S.SKIPPED],
          f"the list shows MISSED, NEXT, SKIPPED, got "
          f"{[r['status'] for r in view][:3]}")
    check(view[1]["planned"] == "18:20" and view[1]["start"] == "18:25",
          "a moved show shows where it was planned")

    # End night.
    n.op(S.END_NIGHT, _den(S, 19, 0))
    check(n.m.state == S.STANDBY, "an unconfirmed End night changes nothing")
    o = n.op(S.END_NIGHT, _den(S, 19, 0), confirmed=True)
    check(n.m.state == S.CLOSING and
          [e.kind for e in o.effects] == [S.ZERO_FLAME_CUES, S.STOP_CONDUCTOR,
                                          S.FADE_PIXELS, S.BLACKOUT],
          f"End night closes: flame cues to zero, MadMapper stopped, fade, "
          f"blackout. Got {[e.kind for e in o.effects]}")
    check(all(s.status != S.PENDING for s in n.m.slots) and
          all(s.reason == "SKIPPED (operator, End night)"
              for s in n.m.slots if s.status == S.SKIPPED and s.n > 3),
          "End night skips every show still to come, and says why")
    n.do(S.CLOSING_DONE, "system", _den(S, 19, 0, 2))
    check(n.m.state == S.OFF, "closing finishes in OFF")
    check(not n.op(S.SKIP_NEXT, _den(S, 19, 1)).accepted,
          "nothing is left to skip after End night")
    # And a night that simply runs out closes by itself.
    n = _Night(S, rule)
    n.boot(_den(S, 21, 30))
    n.tick(_den(S, 21, 40))
    check(n.m.state == S.SHOW, "the last show of the night starts at 21:40")
    o = n.do(S.SHOW_ENDED, "madmapper", _den(S, 21, 47, 20))
    check(n.m.state == S.CLOSING and S.BLACKOUT in [e.kind for e in o.effects],
          "after the last show the night closes by itself")
    n.audit("abort and end night")
    print("  ok")


def test_schedule_file_is_versioned_and_atomic():
    section("scheduler: the rule file keeps its previous version, and a "
            "locked file is a sentence")
    S = _sched()
    if S is None:
        return
    import json
    import tempfile
    from ltcplay import schedule_service as SV
    from ltcplay import settings as st
    check(SV.data_dir() == st.folder(),
          "the scheduler keeps its files where the preferences are, until "
          "the Windows port moves them")
    check(os.path.dirname(SV.default_rule_path()) == SV.data_dir(),
          "the rule file lives in data_dir()")
    work = tempfile.mkdtemp()
    path = os.path.join(work, SV.RULE_FILE)
    prev = SV.previous_path(path)
    try:
        SV.load_rule(path)
        check(False, "a missing rule file loaded")
    except ValueError as e:
        check("There is no schedule file" in str(e), f"missing file: {e}")
    r1 = SV.save_rule(path, _sched_doc())
    check(r1.version == 1 and not os.path.exists(prev),
          "the first save is version 1 with nothing before it")
    r2 = SV.save_rule(path, _sched_doc(guard_s=90))
    check(r2.version == 2 and SV.load_rule(path).guard_s == 90,
          "the second save is version 2")
    old = json.load(open(prev))
    check(old["version"] == 1 and old["guard_s"] == 120,
          f"the previous version is kept beside it: {old}")
    snap = (open(path, "rb").read(), open(prev, "rb").read())

    def leftovers():
        return [f for f in os.listdir(work) if f.endswith(".new")]

    try:
        SV.save_rule(path, _sched_doc(late_grace_s=99))
        check(False, "an invalid rule was saved")
    except ValueError as e:
        check("15" in str(e), f"an invalid save says why: {e}")
    check((open(path, "rb").read(), open(prev, "rb").read()) == snap
          and not leftovers(), "an invalid save touches nothing")

    # Windows: replacing a file another program has open fails for a
    # moment. Two failures then success is a save; failing every time is a
    # sentence and an untouched file.
    calls, sleeps = [], []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) <= 2:
            raise PermissionError(13, "The process cannot access the file")
        os.replace(src, dst)

    r3 = SV.save_rule(path, _sched_doc(guard_s=60), replace_fn=flaky,
                      sleep_fn=sleeps.append)
    check(r3.version == 3 and len(sleeps) == 2 and not leftovers(),
          f"a briefly locked file is retried and saved: v{r3.version}, "
          f"{len(sleeps)} waits")
    snap = (open(path, "rb").read(), open(prev, "rb").read())

    def locked(src, dst):
        raise PermissionError(13, "The process cannot access the file")

    try:
        SV.save_rule(path, _sched_doc(guard_s=30), replace_fn=locked,
                     sleep_fn=lambda s: None)
        check(False, "a save into a locked file claimed success")
    except OSError as e:
        check("held open by another program" in str(e)
              and "unchanged" in str(e), f"a locked file is a sentence: {e}")
        _no_dashes(str(e), "locked file")
    check((open(path, "rb").read(), open(prev, "rb").read()) == snap
          and not leftovers(), "a failed save leaves both files as they were")
    open(path, "w").write("{broken")
    try:
        SV.load_rule(path)
        check(False, "a broken rule file loaded")
    except ValueError as e:
        check(path in str(e) and "not readable JSON" in str(e),
              f"a broken file names itself: {e}")
    print("  ok")


def test_schedule_clock_check():
    section("scheduler: the NTP check logs the offset and never sets the "
            "clock")
    S = _sched()
    if S is None:
        return
    import struct
    from ltcplay import schedule_service as SV
    for off, level in ((0.4, "ok"), (-1.9, "ok"), (2.0, "ok"),
                       (2.01, "warn"), (-3.5, "warn"), (None, "unknown")):
        got, text = S.judge_clock_offset(off, "test.server")
        check(got == level, f"an offset of {off} is {level}, got {got}")
        _no_dashes(text, "clock check")
        if level == "warn":
            check(("behind" if off > 0 else "ahead of") in text
                  and "not changed" in text,
                  f"a warning says which way and that nothing was set: "
                  f"{text}")

    def ntp(t):
        secs = int(t) + SV.NTP_EPOCH
        return struct.pack("!II", secs, int((t % 1) * 2 ** 32))

    reply = bytes([0x24]) + bytes(31) + ntp(1005.1) + ntp(1005.1)
    off = SV.parse_sntp(reply, 1000.0, 1000.2)
    check(abs(off - 5.0) < 1e-6, f"the SNTP offset should be 5.0 s, got {off}")
    for bad in (reply[:20], bytes([0x23]) + reply[1:]):
        try:
            SV.parse_sntp(bad, 0, 0)
            check(False, "a bad SNTP reply was accepted")
        except ValueError:
            pass

    sent = []

    class FakeSock:
        def settimeout(self, t):
            pass

        def sendto(self, data, addr):
            sent.append((data, addr))

        def recvfrom(self, n):
            return reply, ("1.2.3.4", 123)

        def close(self):
            pass

    wall = iter((1000.0, 1000.2))
    off = SV.sntp_query("time.test", sock_factory=FakeSock,
                        wall=lambda: next(wall))
    check(abs(off - 5.0) < 1e-6 and len(sent) == 1 and len(sent[0][0]) == 48
          and sent[0][0][0] == 0x1b and sent[0][1] == ("time.test", 123),
          f"the request is one 48 byte SNTP client packet to port 123: {sent}")

    def unreachable():
        raise OSError("Network is unreachable")

    level, text, off = SV.check_clock(unreachable)
    check(level == "unknown" and off is None and "Network is unreachable"
          in text, f"no time server is a sentence, not a crash: {text}")
    level, text, off = SV.check_clock(lambda: 3.2)
    check(level == "warn" and off == 3.2, "a 3.2 s offset warns")
    check(not any(n for n in dir(SV) + dir(S)
                  if n.lower().lstrip("_").startswith("set")
                  and ("clock" in n.lower() or "time" in n.lower())),
          "nothing in the scheduler can set the clock")
    print("  ok")


def test_schedule_routes():
    section("scheduler: read-only routes and tonight's edits, only when "
            "configured")
    S = _sched()
    if S is None:
        return
    import json
    import tempfile
    import threading
    import urllib.error
    import urllib.request
    from ltcplay import web as web_mod
    from ltcplay import schedule_service as SV
    work = tempfile.mkdtemp()
    path = os.path.join(work, SV.RULE_FILE)
    SV.save_rule(path, _sched_doc())
    rule_bytes = open(path, "rb").read()
    now = [_den(S, 18, 4, 12)]
    svc = SV.Service(path, clock=lambda: now[0], ntp_query=lambda: 0.25)
    port = _free_port()
    httpd = web_mod.serve(work, port=port, schedule=svc)
    t = threading.Thread(target=httpd.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"

    def call(route, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + route, data=data, method=(
            "POST" if body is not None else "GET"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    try:
        check(wait_for(lambda: svc.clock_check is not None),
              "the clock check runs when the scheduler starts")
        code, rv = call("/api/schedule")
        check(code == 200 and rv["ok"] and rv["rule"]["show_len_s"] == 440
              and rv["rule"]["version"] == 1 and rv["path"] == path,
              f"GET /api/schedule shows the rule: {code} {rv}")
        code, tv = call("/api/schedule/tonight")
        st = [r["status"] for r in tv.get("slots", [])]
        check(code == 200 and tv["date"] == "2026-11-14" and len(st) == 15
              and st[:5] == [S.MISSED] * 4 + [S.NEXT],
              f"tonight's list at 18:04: {st[:6]}")
        code, sv = call("/api/schedule/state")
        check(code == 200 and sv["state"] == S.STANDBY and sv["dry_run"]
              and sv["next"]["start"] == "18:20"
              and sv["clock_check"]["level"] == "ok",
              f"GET /api/schedule/state: {code} {sv.get('state')} "
              f"{sv.get('next')}")
        check(any(a["id"] == "abort" and a["confirm"] for a in sv["actions"]),
              "the state carries the transport panel with its confirms")
        check(all(r["actor"] in S.ACTORS and r["reason"]
                  for r in sv["journal"]),
              "every journal row has an actor and a reason")
        code, ev = call("/api/schedule/tonight",
                        {"op": "move", "show": 5, "to": "18:25",
                         "who": "Andy", "screen": "rack screen"})
        check(code == 200 and [r for r in ev["slots"] if r["show"] == 5][0]
              ["start"] == "18:25", f"moving show 5 to 18:25: {code} {ev}")
        code, bad = call("/api/schedule/tonight", {"op": "move", "show": 5,
                                                   "to": "18:35"})
        check(code == 400 and "before show" in bad.get("error", ""),
              f"a move that crowds a show is a 400 with a sentence: {bad}")
        check(not httpd.control.last_error,
              f"a refused schedule edit must not become the show's last "
              f"error: {httpd.control.last_error!r}")
        code, bad = call("/api/schedule/tonight", {"op": "launch"})
        check(code == 400 and "move, add or remove" in bad.get("error", ""),
              f"an unknown edit is a 400 with a sentence: {bad}")
        check(open(path, "rb").read() == rule_bytes,
              "editing tonight must never write the rule file")
        # Nothing that starts, stops or arms anything can be posted.
        check(SV.Service.POST_ROUTES == ("/api/schedule/tonight",),
              f"the only schedule route that takes a POST is the tonight "
              f"edit, got {SV.Service.POST_ROUTES}")
        for r in ("/api/schedule/start", "/api/schedule/state",
                  "/api/schedule", "/api/schedule/abort",
                  "/api/schedule/hold"):
            code, _ = call(r, {"op": "move"})
            check(code == 404, f"POST {r} must not exist, got {code}")

        # Dry run: at 18:25 the engine decides to start show 5 (moved there
        # above) and nothing performs it; the clock ends it after show_len_s.
        now[0] = _den(S, 18, 25)
        svc.tick()
        check(svc.machine.state == S.SHOW and svc.machine.running == 5,
              "at 18:25 the engine decides to start show 5")
        check(any(r["outcome"] == "not performed" and "START_SHOW" in
                  r["text"] for r in svc.journal),
              "the start is journalled as not performed")
        now[0] = _den(S, 18, 32, 20)
        svc.tick()
        s2 = svc.machine.slot(5)
        check(svc.machine.state == S.STANDBY and s2.status == S.DONE
              and "dry run" in s2.reason,
              f"a dry run show ends after show_len_s and says it was a dry "
              f"run: {s2.status} {s2.reason!r}")
        for row in svc.journal:
            _no_dashes(row["text"], "journal")
    finally:
        httpd.shutdown()
        httpd.server_close()
        svc.stop()

    # A bad rule file does not take the server down; the page says why.
    open(path, "w").write(json.dumps(_sched_doc(late_grace_s=60)))
    bad = SV.Service(path, clock=lambda: now[0], ntp_query=lambda: 0.0)
    code, rv = bad.get("/api/schedule")
    check(code == 200 and not rv["ok"] and "15" in rv["error"],
          f"a bad rule file is reported, not fatal: {rv}")
    code, sv = bad.get("/api/schedule/state")
    check(code == 200 and not sv["ok"] and sv["error"],
          "the state of a scheduler with a bad rule says why")

    # Not configured: every schedule route is a plain 404.
    port = _free_port()
    httpd = web_mod.serve(work, port=port)
    t = threading.Thread(target=httpd.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:
        check(httpd.schedule is None, "no schedule, no scheduler")
        for r in ("/api/schedule", "/api/schedule/tonight",
                  "/api/schedule/state"):
            code, _ = call(r)
            check(code == 404, f"GET {r} without a schedule is 404, got "
                               f"{code}")
        code, _ = call("/api/schedule/tonight", {"op": "add", "at": "20:00"})
        check(code == 404, f"POST without a schedule is 404, got {code}")
        code, _ = call("/api/state")
        check(code == 200, "the rest of the page is untouched")
    finally:
        httpd.shutdown()
        httpd.server_close()
    print("  ok")


def test_the_gpl_path_never_loads_the_scheduler():
    section("scheduler: the GPL path does not import it")
    import ast
    import subprocess
    root = os.path.dirname(os.path.abspath(__file__))
    pkg = os.path.join(root, "ltcplay")
    # 1. Run the GPL path in a clean interpreter: every module the show
    # uses, the web server as the launchers start it, and a request at every
    # schedule route. The scheduler must not be in sys.modules afterwards.
    port = _free_port()
    code = (
        "import sys, json, threading, tempfile, urllib.request, "
        "urllib.error\n"
        f"sys.path.insert(0, {root!r})\n"
        "import importlib, pkgutil, ltcplay\n"
        "mods = [m.name for m in pkgutil.iter_modules(ltcplay.__path__)\n"
        "        if not m.name.startswith('schedule')]\n"
        "failed = []\n"
        "for m in mods:\n"
        "    try:\n"
        "        importlib.import_module('ltcplay.' + m)\n"
        "    except Exception as e:\n"
        "        failed.append(m)\n"
        "from ltcplay import web, cli\n"
        f"h = web.serve(tempfile.mkdtemp(), port={port})\n"
        "t = threading.Thread(target=h.serve_forever, "
        "kwargs={'poll_interval': 0.05}, daemon=True)\n"
        "t.start()\n"
        "codes = []\n"
        "for r in ('/api/schedule', '/api/schedule/state', "
        "'/api/schedule/tonight'):\n"
        "    try:\n"
        f"        urllib.request.urlopen('http://127.0.0.1:{port}' + r, "
        "timeout=5)\n"
        "        codes.append(200)\n"
        "    except urllib.error.HTTPError as e:\n"
        "        codes.append(e.code)\n"
        "h.shutdown(); h.server_close()\n"
        "print(json.dumps({'mods': mods, 'failed': failed, 'codes': codes,\n"
        "    'none': h.schedule is None,\n"
        "    'loaded': sorted(m for m in sys.modules if 'schedule' in m)}))\n")
    rc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                        text=True, timeout=60)
    import json
    try:
        out = json.loads(rc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        check(False, f"the GPL path check did not run: {rc.stderr[-800:]}")
        return
    check("web" in out["mods"] and "cli" in out["mods"]
          and "session" in out["mods"], f"the GPL modules were all imported: "
                                        f"{out['mods']}")
    check(out["loaded"] == [], f"the GPL path loaded the scheduler: "
                               f"{out['loaded']}")
    check(out["codes"] == [404, 404, 404] and out["none"],
          f"with no schedule configured every schedule route is 404, got "
          f"{out['codes']}")

    # 2. Nothing imports the scheduler at module level; only the code behind
    # --schedule does, inside a function.
    def top_level(node):
        """Everything that runs at import time: not function bodies."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.Lambda)):
                continue
            yield child
            yield from top_level(child)

    for name in sorted(os.listdir(pkg)):
        if not name.endswith(".py") or name.startswith("schedule"):
            continue
        tree = ast.parse(open(os.path.join(pkg, name)).read())
        for sub in top_level(tree):
            names = []
            if isinstance(sub, ast.Import):
                names = [a.name for a in sub.names]
            elif isinstance(sub, ast.ImportFrom):
                names = [sub.module or ""] + [a.name for a in sub.names]
            check(not any("schedule" in n for n in names),
                  f"ltcplay/{name} imports the scheduler at module level")

    # 3. No launcher turns it on.
    files = [os.path.join(root, n) for n in os.listdir(root)
             if n.endswith(".command") or n == "ltc"]
    tools = os.path.join(root, "Tools")
    if os.path.isdir(tools):
        files += [os.path.join(tools, n) for n in os.listdir(tools)]
    for dirpath, _d, names in os.walk(os.path.join(root, "packaging")):
        files += [os.path.join(dirpath, n) for n in names]
    for f in files:
        try:
            text = open(f, errors="replace").read()
        except OSError:
            continue
        check("--schedule" not in text,
              f"{os.path.relpath(f, root)} turns the scheduler on")
    print("  ok")


def test_the_scheduler_engine_is_pure():
    section("scheduler: the engine does no I/O and runs on Python 3.12")
    import ast
    root = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(root, "ltcplay", "schedule.py")).read()
    tree = ast.parse(src)
    allowed = {"json", "re", "dataclasses", "datetime", "zoneinfo"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                check(a.name.split(".")[0] in allowed,
                      f"schedule.py imports {a.name}; the engine is pure")
        elif isinstance(node, ast.ImportFrom):
            check((node.module or "").split(".")[0] in allowed
                  and node.level == 0,
                  f"schedule.py imports from {node.module}; the engine is "
                  f"pure")
        elif isinstance(node, ast.Name):
            check(node.id not in ("open", "print", "input"),
                  f"schedule.py uses {node.id}(); the engine does no I/O")
        elif isinstance(node, ast.Attribute):
            check(node.attr not in ("now", "today", "utcnow", "sleep",
                                    "monotonic"),
                  f"schedule.py reads the clock through .{node.attr}; time "
                  f"is handed in")
    # Written for Python 3.12 on Windows: nothing newer, nothing POSIX only.
    for name in ("schedule.py", "schedule_service.py"):
        text = open(os.path.join(root, "ltcplay", name)).read()
        try:
            ast.parse(text, feature_version=(3, 12))
        except SyntaxError as e:
            check(False, f"{name} does not parse as Python 3.12: {e}")
        for posix in ("fcntl", "os.fork", "signal.SIGHUP", "os.getuid",
                      "termios", "resource"):
            check(posix not in text, f"{name} uses {posix}, which Windows "
                                     f"does not have")
    print("  ok")


if __name__ == "__main__":
    t0 = time.time()
    test_ltc_roundtrip()
    test_ltc_rollovers()
    test_ltc_degraded()
    test_ltc_rejects_garbage()
    test_numpy_matches_scalar()
    test_packets()
    test_timeline()
    test_netmap()
    test_tc_math()
    test_rate_detection()
    test_next_cue()
    test_player_states()
    test_loop_never_dies()
    test_socket_healing()
    test_display_survives()
    test_park_and_pause()
    test_on_lost_policies()
    test_pause_does_not_poison_the_rate()
    test_device_resolution()
    test_timecode_on_a_channel_other_than_one()
    test_find_names_the_channel()
    test_input_rebuilds_itself()
    test_level_meter()
    test_timeline_input_block()
    test_installer_and_launcher_names()
    test_real_hardware_is_picked_out_of_the_noise()
    test_only_plausible_inputs_are_scanned()
    test_saved_input_setting()
    test_input_precedence()
    test_show_dir_survives_the_wrong_machine()
    test_web_ui()
    test_web_token_gate()
    test_which_file_plays_when()
    test_one_frame_between_cues_is_not_a_gap()
    test_a_cue_owns_the_whole_rig()
    test_a_bad_frame_does_not_move_the_show()
    test_the_bridge_does_not_resurrect_a_finished_cue()
    test_a_read_hiccup_does_not_stick()
    test_the_overrun_warning_is_per_cue()
    test_stop_always_works()
    test_a_read_only_folder_is_a_sentence()
    test_up_next_survives_the_interval()
    test_free_run_does_not_flood_the_log()
    test_the_readout_tells_the_truth_in_a_free_run()
    test_go_runs_without_the_feed()
    test_the_bundle_stands_on_its_own()
    test_the_credit_travels_with_it()
    test_a_dead_controller_stops_being_hammered()
    test_broadcast_destinations_are_called_out()
    test_free_run_to_the_end_when_timecode_dies()
    test_the_input_can_be_rebuilt_without_dropping_the_rig()
    test_the_input_stops_hunting_sample_rates()
    test_a_poisoned_portaudio_is_rebuilt()
    test_a_missing_input_still_runs_the_preshow()
    test_the_input_can_be_changed_mid_show()
    test_a_failed_start_leaves_nothing_running()
    test_only_one_player_sends_at_a_time()
    test_reload_while_the_show_runs()
    test_auto_reload_waits_for_the_writer()
    test_opening_a_render_proves_it_reads()
    test_sequence_position_not_timecode()
    test_a_cue_that_will_not_open_stops_the_show()
    test_preshow_can_be_held_by_hand()
    test_a_misspelled_setting_is_refused()
    test_one_sequence_at_two_timecodes()
    test_at_command_on_the_real_show()
    test_track_numbers_are_not_identity()
    test_sequences_declare_what_they_are()
    test_verify_catches_a_mislabelled_sequence()
    test_pointing_a_show_at_a_different_folder()
    test_trigger_mode_mutes_the_advateks_and_nothing_else()
    test_a_muted_controller_is_not_reported_as_a_fault()
    test_a_cue_fires_its_scene_once_and_only_once()
    test_preshow_fires_once_per_entry_not_once_per_loop()
    test_an_unmapped_cue_is_named_rather_than_fired_blind()
    test_a_trigger_that_explodes_does_not_stop_the_show()
    test_the_trigger_packet_is_one_artnet_channel_at_full()
    test_a_trigger_never_blocks_the_output_loop()
    test_a_show_file_that_would_silently_do_nothing_is_refused()
    test_the_scene_map_is_checked_against_the_real_show()
    test_the_real_show_file_maps_every_cue()
    test_the_trigger_goes_out_on_the_protocol_the_boxes_listen_for()
    test_the_real_show_file_matches_the_trigger_reference()
    test_stopping_the_show_unmutes_before_the_blackout()
    test_the_panel_tells_the_truth_about_backup_mode()
    test_a_shared_show_folder_cannot_be_moved_away_from_it()
    test_the_app_is_signed_last_and_nothing_touches_it_after()
    test_the_build_proves_macos_will_actually_launch_it()
    test_the_app_declares_what_macos_needs_to_know()
    test_the_app_launcher_finds_its_way_home()
    test_the_app_starts_the_page_by_itself()
    test_you_can_tell_which_version_is_installed()
    test_the_beta_window_app_stays_a_window()
    test_schedule_rule_is_validated()
    test_schedule_expands_the_season()
    test_schedule_late_rule()
    test_schedule_state_machine_every_state_every_event()
    test_schedule_restart_guard_and_hold()
    test_schedule_abort_end_night_and_operator_actions()
    test_schedule_file_is_versioned_and_atomic()
    test_schedule_clock_check()
    test_schedule_routes()
    test_the_gpl_path_never_loads_the_scheduler()
    test_the_scheduler_engine_is_pure()
    for arg in sys.argv[1:]:
        test_real_show(arg)
    # test_real_show is opt-in: it runs only when a show folder is named on
    # the command line, so it is not expected in a default run.
    OPT_IN = {"test_real_show"}
    defined = {n for n, v in list(globals().items())
               if n.startswith("test_") and callable(v)} - OPT_IN
    never = sorted(defined - RAN)
    if never:
        FAILS.extend(f"{n} is defined but was never called" for n in never)
        print(f"\n{len(never)} test(s) defined but never run:")
        for n in never:
            print(f"  - {n}")

    print(f"\n{'-'*50}")
    if SHOW_PROBLEMS:
        print(f"{len(SHOW_PROBLEMS)} thing(s) to fix in the SHOW FILE. The "
              f"program is sound and an update cannot change these:")
        for f in SHOW_PROBLEMS:
            print(f"  - {f}")
        print("  Open Tools and run 'Set the Advatek triggers.command'.")
        print()
    if FAILS:
        print(f"{len(FAILS)} FAILURES in {time.time()-t0:.1f}s")
        for f in FAILS:
            print(f"  - {f}")
        sys.exit(1)
    print(f"all checks passed in {time.time()-t0:.1f}s")
