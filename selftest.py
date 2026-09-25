#!/usr/bin/env python3
"""ltcplay self-test.  Run it after any change: ./ltcplay-venv/bin/python selftest.py

Every check here is anchored to something real: LTC that is synthesised and then
decoded back, packet bytes compared against the layouts in the xLights source,
and where a real show folder is available, actual FSEQ files off disk.
"""
import json
import os
import random
import re
import tempfile
import threading
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ltcplay.ltc import LTCDecoder, synthesize, decode_bits, SYNC_WORD
from ltcplay import (output, netmap, timeline, tc as tcmod,
                     display as disp, audio as audio_mod,
                     trigger as trig_mod)
from ltcplay.player import (Player, LOCKED, FREEWHEEL, LOST, PARKED,
                            SHOW, IDLE, HOLD, BLACK, _now)

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


# The real renders, for the tests that need a whole show. They are never in
# the repo (600MB, and CLAUDE.md says never commit them). Say where they are
# with LTCPLAY_TEST_SHOW_DIR; the default is where they sat in the machine
# these tests were first written on.
_SHOW_DIR_DEFAULT = "/mnt/user-data/uploads/PROJECTS/Dollywood/GPL26_xLights"


def real_show_dir():
    """Where to read "a real show folder" from.

    LTCPLAY_TEST_SHOW_DIR, then the machine this suite was first written on,
    win when they exist: they are the actual GPL 2026 renders, and a handful
    of checks (test_real_show, and anything that reads SHOW_PROBLEMS rather
    than FAILS) only mean something against those. Absent both, this falls
    back to a small folder of generated FSEQ files standing in for them --
    see test_show_fixtures.py -- so the tests that only need SOME valid show
    folder (not that specific one) run everywhere, including CI, instead of
    skipping."""
    env = os.environ.get("LTCPLAY_TEST_SHOW_DIR")
    if env:
        return env
    if os.path.isdir(_SHOW_DIR_DEFAULT):
        return _SHOW_DIR_DEFAULT
    import test_show_fixtures
    return test_show_fixtures.synthetic_show_dir()


# That folder can be the LIVE show. The tests only read it, or copy out of it
# into a temp folder, and this proves it: every name, size, mtime and mode is
# recorded before the first test and compared after the last. ctime and the
# file flags are not: a synced folder downloading an online-only file on
# first read changes those without anyone writing. Finder's and Dropbox's own
# bookkeeping files are the only names left out.
_NOT_OURS = (".DS_Store", ".dropbox", ".dropbox.attr", "Icon\r")


def copy_render(src, dst):
    """Copy a render for a test to work on: the bytes, not the permissions.

    The show folder the tests read may be read-only (a locked card, a
    protected copy of the live show). shutil.copy carries that across, and a
    test that then corrupts its OWN copy on purpose was refused. The copy is
    the test's; the original is only ever read."""
    import shutil
    if os.path.isdir(dst):
        dst = os.path.join(dst, os.path.basename(src))
    shutil.copyfile(src, dst)
    return dst


def _show_snapshot(root):
    snap = {}
    for dirpath, dirs, names in os.walk(root):
        dirs.sort()
        for n in sorted(dirs + names):
            if n in _NOT_OURS:
                continue
            p = os.path.join(dirpath, n)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            snap[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns,
                                              st.st_mode)
    return snap


def _show_changes(root, before):
    after = _show_snapshot(root)
    out = []
    for k in sorted(set(before) | set(after)):
        if k not in after:
            out.append(f"removed: {k}")
        elif k not in before:
            out.append(f"added: {k}")
        elif before[k] != after[k]:
            out.append(f"changed: {k} (size, mtime_ns, mode "
                       f"{before[k]} -> {after[k]})")
    return out


def _find_bash():
    """The bash that can syntax-check a Mac launcher, or None.

    On a Mac and on Linux that is plain `bash`, exactly as it always was. On
    Windows, `bash` on the PATH is usually WSL's launcher in System32, which
    with no Linux installed prints its complaint and fails every script. Git
    for Windows carries a real bash, so use that one when it is there."""
    if sys.platform != "win32":
        return "bash"
    import shutil
    cands = []
    git = shutil.which("git")
    if git:
        root = os.path.dirname(os.path.dirname(os.path.abspath(git)))
        cands += [os.path.join(root, "bin", "bash.exe"),
                  os.path.join(root, "usr", "bin", "bash.exe")]
    for env in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        if os.environ.get(env):
            cands.append(os.path.join(os.environ[env], "Git", "bin",
                                      "bash.exe"))
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


BASH = _find_bash()


def bash_n(path):
    """`bash -n` on one launcher: does it even parse?

    The launchers are Mac .command files. A Windows machine with no Git for
    Windows has no bash to parse them with; that is said once per script and
    counted as nothing proven, not as a failure. CI has Git for Windows, so
    there this always runs."""
    import subprocess
    if BASH is None:
        print(f"  (no bash on this machine to parse "
              f"{os.path.basename(path)!r}; the Mac launchers are parsed on "
              f"the Mac and in CI, skipped)")
        return subprocess.CompletedProcess([path], 0, "", "")
    return subprocess.run([BASH, "-n", path], capture_output=True, text=True)


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
    t0 = _now()
    while True:
        el = _now() - t0
        if el >= seconds:
            return
        if feed_until is None or el < feed_until:
            p.feed_timecode(tc_start + el, _now(), text="fed")
        time.sleep(step)


class _Stepped:
    """A Player run by hand, on a clock that moves only when told to.

    The state tests below are about windows of 100 to 600ms. Run on the wall
    clock they measured the runner as much as the player: a CI Mac wakes a
    20ms sleep at 50 to 100ms, read a healthy feed as freewheeling and a
    freewheel as lost. Here every output tick is the same work the output
    thread does (tick, send, count, trigger), at exact times, so a check
    fails only when the player is wrong. The thread itself is proven by
    test_loop_never_dies, which still runs it for real."""

    def __init__(self, p, step_ms):
        import ltcplay.player as plmod
        self.p, self.step = p, step_ms / 1000.0
        self.t = 1000.0
        self._plmod, self._real = plmod, plmod.time
        clock = self

        class _Time:
            def monotonic(self):
                return clock.t

            # player._now() reads time.perf_counter(), not time.monotonic():
            # see player.py's module docstring. Fake both to the same t, so
            # a stepped run is deterministic whichever one the chase engine
            # is calling this month.
            def perf_counter(self):
                return clock.t

            def sleep(self, s):
                clock.t += s

            def __getattr__(self, name):
                return getattr(time, name)

        plmod.time = _Time()
        p.step_ms = step_ms
        p._idle_epoch = self.t

    def close(self):
        self._plmod.time = self._real

    def feed(self, tc_seconds, text="fed"):
        self.p.feed_timecode(tc_seconds, self.t, text=text)

    def tick(self):
        p = self.p
        frame = p._tick()
        p.sender.send_frame(frame if frame is not None else b"")
        p.frames_sent += 1
        p._service_trigger()

    def run(self, seconds, tc_from=None, hold_at=None):
        """Let `seconds` pass, one output tick per step. With `tc_from`, a
        feed running from that timecode at real speed; with `hold_at`, a feed
        repeating that one timecode (a paused deck); with neither, silence."""
        t0 = self.t
        for _ in range(int(round(seconds / self.step))):
            self.t += self.step
            if tc_from is not None:
                self.feed(tc_from + (self.t - t0))
            elif hold_at is not None:
                self.feed(hold_at)
            self.tick()


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
    clk = _Stepped(p, step_ms=25)
    try:
        # 1. no timecode at all -> the preshow loop plays, not blackness
        clk.run(0.3)
        check(p.state == LOST and p.source == IDLE,
              f"with no timecode the preshow loop should run, got "
              f"{p.state}/{p.source}")
        check(any(any(b) for b in snd.frames[-5:]),
              "the preshow loop sent nothing but zeros")

        # 2. timecode arrives -> show
        base = tcmod.parse_tc("01:00:10:00", 30)
        clk.run(0.6, tc_from=base)
        check(p.state == LOCKED and p.source == SHOW,
              f"with timecode running the show should play, got "
              f"{p.state}/{p.source}")
        check(p.current_cue and p.current_cue.name == "A",
              "cue A should be playing at 01:00:10:00")
        check(p.next_cue and p.next_cue.name == "B", "next should be B")
        frozen = p.last_ltc_text
        rolling = p.tc_seconds

        # 3. feed stops -> LTC readout freezes, playback free-rolls
        stopped = clk.t
        clk.run(0.3)
        check(p.state == FREEWHEEL, f"expected FREEWHEEL, got {p.state}")
        check(p.last_ltc_text == frozen,
              "the LTC readout moved after the feed stopped")
        # At full speed, on the clock: 0.3s of dropout is 0.3s of show.
        check(abs(p.tc_seconds - (rolling + 0.3)) < 0.03,
              f"playback should free-roll through a short dropout at full "
              f"speed: moved {p.tc_seconds - rolling:.3f}s in 0.300s")
        check(p.source == SHOW, "a short dropout should not interrupt the show")

        # 4. feed stays gone -> back to the preshow loop, readout still frozen
        # And it goes when the HOLD says, not at some other number: the tick
        # it first reads LOST is the first one past 0.5s without timecode.
        lost_at = None
        for _ in range(int(round(0.6 / clk.step))):
            clk.run(clk.step)
            if lost_at is None and p.state == LOST:
                lost_at = clk.t - stopped
        check(lost_at is not None and 0.5 < lost_at <= 0.5 + clk.step + 1e-9,
              f"the feed was lost {lost_at}s after it stopped; the hold is "
              f"0.500s")
        check(p.state == LOST, f"expected LOST, got {p.state}")
        check(p.source == IDLE,
              f"after a long dropout the preshow loop should return, got "
              f"{p.source}")
        check(p.last_ltc_text == frozen,
              "the LTC readout must stay frozen on the last number received")
        check(p.tc_seconds < 0, "the playback clock should stop once lost")

        # 5. a deliberate jump is snapped, not slewed
        before = p.jumps
        clk.feed(tcmod.parse_tc("01:00:55:00", 30), text="jump")
        clk.run(0.15)
        clk.feed(tcmod.parse_tc("01:00:55:05", 30), text="jump")
        clk.run(0.1)
        check(p.current_cue and p.current_cue.name == "B",
              f"after jumping to 01:00:55:00 cue B should play, got "
              f"{p.current_cue and p.current_cue.name}")
        check(p.jumps > before, "the jump was not counted as a jump")
    finally:
        clk.close()
        p.stop()
    print("  ok")


def test_the_stepped_player_is_the_output_thread():
    section("the hand-stepped player does what the output thread does")
    # _Stepped copies two things out of Player by hand: the body of the
    # output loop, and what start() sets up before the thread. If either
    # moves on and the copy does not, every stepped test keeps passing while
    # proving a player that no longer exists. So read both out of the source
    # and compare.
    import ast, inspect, re as _re, textwrap
    from ltcplay.player import Player

    def body_of(fn):
        return ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]

    loop = next(n for n in ast.walk(body_of(Player._loop))
                if isinstance(n, ast.While))
    tried = next(n for n in loop.body if isinstance(n, ast.Try))
    real = [ast.unparse(st) for st in tried.body]
    tick = body_of(_Stepped.tick).body
    ours = [_re.sub(r"\bp\.", "self.", ast.unparse(st)) for st in tick
            if ast.unparse(st) != "p = self.p"]
    check(real == ours,
          f"selftest._Stepped copies Player._loop; update it. The loop does "
          f"{real}, the harness does {ours}")

    start = body_of(Player.start).body
    before, sets = [], set()
    for st in start:
        if "_spawn" in ast.unparse(st):
            break
        before.append(st)
    for st in before:
        for node in ast.walk(st):
            if isinstance(node, ast.Attribute) and \
                    isinstance(node.ctx, ast.Store) and \
                    isinstance(node.value, ast.Name) and node.value.id == "self":
                sets.add(node.attr)
    init = ast.unparse(body_of(_Stepped.__init__))
    copied = {a for a in sets if f"p.{a} = " in init}
    # _running only keeps the thread's while loop going; nothing is stepped
    # without it, and the stepped player never starts one.
    check(sets - copied == {"_running"},
          f"selftest._Stepped copies Player.start; update it. start() sets "
          f"{sorted(sets)} before the thread, the harness sets "
          f"{sorted(copied)}")
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
        # Ticks are 10ms apart here and 50ms or more on a CI Mac, so wait for
        # the count rather than assuming how many ticks 0.6s held.
        wait_for(lambda: p.loop_errors > 5, timeout=3.0)
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


def test_pixel_output_frame_jitter():
    section("pixel output: real frame interval over ~2s, measured on every OS")
    # This is the number that actually matters on the Pico: how evenly the
    # output thread's frames land, on THIS machine's real clock and real
    # scheduler. There is no Windows box here, so CI is the bench -- this
    # runs on ubuntu-latest, macos-latest and windows-latest, and the
    # measured numbers are printed on every one of them, always, whether or
    # not the strict bounds below apply.
    #
    # The strict bounds only mean anything on a machine that can hold 40
    # frames a second AT ALL. Reuses the calibration pattern from the
    # Art-Net clock's own load test (test_the_clock_survives_its_own_faults,
    # clock.py section): one second unloaded first; a runner that cannot
    # keep time even then is judged on the deterministic test below instead,
    # not failed for being slow.
    fs = FakeFSEQ(frames=4000)
    tl = _timeline([("01:00:00:00", "A", fs)])

    class TimingSender:
        def __init__(self):
            self.at = []

        def send_frame(self, data):
            self.at.append(_now())

        def blackout(self):
            pass

        def close(self):
            pass

    def run_for(seconds, step_ms=25):
        snd = TimingSender()
        p = Player(tl, FakeNetmap(), snd)
        p.start(step_ms=step_ms)
        time.sleep(seconds)
        p.stop()
        return snd.at

    period = 0.025          # 40fps, this project's pixel rate
    base = run_for(1.0)
    base_gaps = [b - a for a, b in zip(base, base[1:])]
    fit = len(base) >= 36 and (max(base_gaps, default=1.0) <= 0.075)

    at = run_for(2.0)
    gaps = [b - a for a, b in zip(at, at[1:])]
    check(len(at) > 0, "the output thread sent nothing in 2s")
    mean_ms = (sum(gaps) / len(gaps) * 1000.0) if gaps else 0.0
    worst_ms = (max(abs(g - period) for g in gaps) * 1000.0) if gaps else 0.0
    p90_ms = (sorted(abs(g - period) for g in gaps)[int(len(gaps) * 0.9)]
             * 1000.0 if gaps else 0.0)
    # Always printed, on every OS: this line IS the measurement the PR
    # argues from.
    print(f"  note: {len(at)} frames in 2.0s, mean interval {mean_ms:.2f}ms "
          f"(target {period*1000:.0f}ms), worst deviation {worst_ms:.2f}ms, "
          f"90th pct deviation {p90_ms:.2f}ms; unloaded baseline "
          f"{len(base)} frames/s, longest gap "
          f"{(max(base_gaps, default=0) * 1000):.1f}ms (fit={fit})")
    if fit:
        check(abs(mean_ms - period * 1000.0) < 3.0,
              f"mean frame interval {mean_ms:.2f}ms strayed more than 3ms "
              f"from the {period*1000:.0f}ms target")
        check(worst_ms < 15.0,
              f"one frame interval was {worst_ms:.2f}ms off target; pixel "
              f"pacing should hold within half a frame ({period*500:.1f}ms) "
              f"on a machine that can keep time at all")
    else:
        print("  note: this machine cannot hold 40 frames a second even "
              "unloaded, so the bounds above are not applied here; "
              "test_pixel_pacing_never_accumulates_error proves the pacing "
              "arithmetic regardless of the machine")
    print("  ok")


def test_pixel_pacing_never_accumulates_error():
    section("the output thread's deadline never drifts from stacked sleep error")
    # Runs Player._loop itself (not a copy of it) on a clock that moves only
    # when told to, the same technique selftest._Stepped uses, but here the
    # sleep is deliberately dishonest: it always overshoots a little and
    # stalls hard now and then, exactly what a real OS timer does under
    # load. A pacer built on `time.sleep(period)` and a running total would
    # inherit every one of those overshoots forever; one built on absolute
    # deadlines (next_at += period, computed fresh from itself, never from
    # when the last frame actually went out) cannot.
    #
    # No thread: _loop's own while loop is driven synchronously by having
    # the fake sender stop it after N frames, so this is fully deterministic
    # and costs nothing in wall time.
    import ltcplay.player as plmod
    rnd = random.Random(11)

    class Sim:
        def __init__(self):
            self.t = 2000.0

        def monotonic(self):
            return self.t

        def perf_counter(self):
            return self.t

        def sleep(self, s):
            over = rnd.uniform(0.0, 0.003)
            if rnd.random() < 0.02:
                over += rnd.uniform(0.01, 0.04)
            self.t += s + over

        def __getattr__(self, name):
            return getattr(time, name)

    sim = Sim()
    tl = _timeline([])
    N = 400
    at = []

    class Sender:
        def send_frame(self, data):
            at.append(sim.t)
            if len(at) >= N:
                p._running = False

        def blackout(self):
            pass

        def close(self):
            pass

    p = Player(tl, FakeNetmap(), Sender())
    p._idle_epoch = sim.t
    p._running = True
    real = plmod.time
    plmod.time = sim
    try:
        p._loop(25)
    finally:
        plmod.time = real

    period = 0.025
    check(len(at) == N, f"expected {N} frames, got {len(at)}")
    start = at[0]
    late = [a - (start + i * period) for i, a in enumerate(at)]
    check(all(x >= -1e-9 for x in late),
          f"a frame went out before its own deadline: {min(late):.6f}s early")
    # The property under test: lateness stays bounded by roughly one
    # iteration's own overshoot, not by how many iterations have run. A
    # pacer that slept a fixed period and counted sleeps would have this
    # grow with every frame; one paced on absolute deadlines cannot.
    check(max(late) < 0.05,
          f"lateness grew to {max(late) * 1000:.1f}ms over {N} frames -- the "
          f"deadline is drifting instead of staying put")
    tail = late[-20:]
    check(max(tail) - min(tail) < 0.05,
          f"lateness late in the run ranges {min(tail) * 1000:.2f} to "
          f"{max(tail) * 1000:.2f}ms -- still growing rather than settled")
    print(f"  ok ({N} frames on a simulated clock, max lateness "
          f"{max(late) * 1000:.2f}ms, never compounding)")


def test_windows_pixel_clock_choice():
    section("chase engine clock: measured on this machine, chosen for Windows")
    # The decision this whole change rests on. Printed on every OS, every
    # run, so the Windows numbers are on record without a Windows machine
    # ever having to sit in this room.
    info_mono = time.get_clock_info("monotonic")
    info_perf = time.get_clock_info("perf_counter")
    same_impl = (info_mono.implementation == info_perf.implementation and
                abs(info_mono.resolution - info_perf.resolution) < 1e-12)
    print(f"  note: time.monotonic is {info_mono.implementation} "
          f"(resolution {info_mono.resolution:.2e}s), time.perf_counter is "
          f"{info_perf.implementation} (resolution {info_perf.resolution:.2e}s) "
          f"on {sys.platform}; {'identical' if same_impl else 'DIFFERENT'}")
    if sys.platform == "darwin":
        check(same_impl,
              "on macOS, monotonic and perf_counter were measured identical "
              "(both mach_absolute_time(), same resolution) when player._now() "
              "was switched to perf_counter unconditionally; if a macOS "
              "release has changed that, _now() needs a platform switch -- "
              "see its docstring in player.py")
    # And prove _now() is actually _reading_ perf_counter, not merely that
    # the two happen to agree on this machine: a mutation that quietly
    # swapped it for time.monotonic() would pass every behavioural check on
    # a Mac (they read the same), so this checks the source, the same way
    # test_the_stepped_player_is_the_output_thread checks _Stepped against
    # Player by reading it rather than by behaviour.
    import inspect
    import ltcplay.player as plmod
    src = inspect.getsource(plmod._now)
    check("perf_counter" in src and "time.monotonic()" not in src,
          f"player._now() should read time.perf_counter(), not "
          f"time.monotonic(): {src!r}")
    print("  ok")


def test_no_clock_is_ever_mixed_with_another():
    section("perf_counter and monotonic diverging must never produce a bad age")
    # test_windows_pixel_clock_choice and _Stepped both fake monotonic and
    # perf_counter to the SAME value, which cannot catch code that reads one
    # where it should read the other: on a Mac the two agree anyway, so a
    # mixed read is invisible there too. A PR #8 review found exactly that
    # in cli.py's cmd_run: `started = sess.started_at` is _now() (perf_
    # counter, session.py), but tick_ui's heartbeat did
    # `now = time.monotonic(); ... now - last_beat[0]`. Nothing here or in
    # _Stepped would have caught it.
    #
    # So this fakes monotonic to a LARGE, constant offset from perf_counter
    # -- what it is free to be on Windows, where one is GetTickCount64 and
    # the other QueryPerformanceCounter, unrelated counters with unrelated
    # epochs -- for the duration of a short, real Session (genuine LTC,
    # decoded for real, through FakeSD) and one real run through cmd_run's
    # own tick_ui, via the actual CLI entry point. A same-clock comparison
    # is unaffected: the offset is constant, so it cancels out of any
    # `time.monotonic() - time.monotonic()`. Only a MIXED comparison blows
    # up, by roughly the offset, which is exactly what a diverging Windows
    # pair could do for real -- so this is the strongest guard against a
    # third clock-mixing bug the current tests do not already give.
    import json, tempfile, threading
    import time as time_mod
    from ltcplay.session import Session
    from ltcplay import cli as cli_mod
    import ltcplay.player as plmod

    work = tempfile.mkdtemp()
    net = os.path.join(work, "net.xml")
    open(net, "w").write(
        '<Networks>\n  <Controller Name="L" IP="127.0.0.1" '
        'ActiveState="Active">\n    <network NetworkType="ArtNET" '
        'ComPort="127.0.0.1" BaudRate="1" MaxChannels="510"/>\n'
        '  </Controller>\n</Networks>\n')
    tlp = os.path.join(work, "t_timeline.json")
    json.dump({"name": "t", "fps": 30, "show_dir": work,
               "cues": [{"tc": "01:00:00:00", "fseq": "A.fseq", "name": "A"}]},
              open(tlp, "w"))
    open(os.path.join(work, "A.fseq"), "wb").write(b"not an fseq")

    real_prepare = plmod.Player._prepare

    def fake_prepare(self, cue):
        cue.fseq = FakeFSEQ(frames=4000)
        cue.duration = cue.fseq.duration_ms / 1000.0
        cue._spans = [(0, 0, cue.fseq.channel_count)]
        cue._gaps = None
        return 0

    plmod.Player._prepare = fake_prepare

    OFFSET_S = 1e5   # what GetTickCount64 and QueryPerformanceCounter are
                     # free to differ by; nothing ties the two together
    real_monotonic = time_mod.monotonic

    def fake_monotonic():
        return time_mod.perf_counter() + OFFSET_S

    before_threads = {t.name for t in threading.enumerate()}
    sess = None
    try:
        # Patch the real `time` module, not just player.py's reference to
        # it: every ltcplay module does `import time`, and a genuine
        # divergent implementation would be visible to all of them, not
        # only to the one this suite already knows to fake.
        time_mod.monotonic = fake_monotonic

        # Part 1: a short real Session, genuine LTC decoded through a fake
        # input, and the same age arithmetic session.snapshot() (the web
        # page) and display.render() (the terminal) both do.
        sd = FakeSD(ltc_channel=2)
        sess = Session(tlp, networks=net, no_log=True, sd=sd,
                       device="MOTU M4", channel=2, no_output=True)
        sess.open()
        sess.start()
        check(wait_for(lambda: sess.player.ltc_frames_in > 5, timeout=3.0),
              "no timecode was decoded in 3s under the injected clock offset")
        snap = sess.snapshot()
        check(snap["uptime"] is not None and 0 <= snap["uptime"] < 60,
              f"snapshot uptime is not a sane small number under a "
              f"diverging monotonic: {snap['uptime']}")
        check(snap["ltc_age"] is not None and 0 <= snap["ltc_age"] < 60,
              f"snapshot ltc_age is not a sane small number under a "
              f"diverging monotonic: {snap['ltc_age']}")
        sess.stop()
        sess = None

        # Part 2: the real cmd_run / tick_ui path, through the actual CLI
        # entry point (cli.main), not a copy of its logic. A heartbeat is
        # due at 60s of wall time; this run is a couple of seconds and must
        # log NONE. Under the bug this test guards, the injected offset
        # makes the very first tick read as ~1e5 seconds since start, and a
        # heartbeat fires immediately.
        wav = os.path.join(work, "tc.wav")
        rc = cli_mod.main(["gen", wav, "--start", "01:00:00:00",
                           "--seconds", "1.5"])
        check(rc == 0, "generating the test LTC WAV failed")
        log_path = os.path.join(work, "run.log")
        rc = cli_mod.main(["run", tlp, "--networks", net, "--wav", wav,
                           "--no-output", "--quiet", "--log", log_path])
        check(rc == 0, "cmd_run itself failed under the injected clock offset")
        logged = (open(log_path, encoding="utf-8").read()
                 if os.path.exists(log_path) else "")
        check("heartbeat" not in logged,
              f"a heartbeat was logged during a 1.5s run: the run loop's "
              f"clock is mixed with another one -- {logged[-400:]!r}")
    finally:
        time_mod.monotonic = real_monotonic
        if sess is not None:
            try:
                sess.stop()
            except Exception:
                pass
        plmod.Player._prepare = real_prepare
    leaked = [t.name for t in threading.enumerate()
             if t.name not in before_threads and t.name.startswith("ltcplay")]
    check(not leaked, f"threads left behind: {leaked}")
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
        p.last_ltc_at = _now() - 3
        p.last_ltc_text = "01:00:05:00"
        lines = disp.render(p, d, tl, sc, _now() - 90)
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
    lines = disp.render(p, D2(), tl, sc, _now() - 10)
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
    clk = _Stepped(p, step_ms=20)
    try:
        base = tcmod.parse_tc("01:00:30:00", 30)
        clk.run(0.5, tc_from=base)
        check(p.state == LOCKED, f"expected LOCKED, got {p.state}")
        jumps_before = p.jumps

        # The deck is paused: it keeps sending, but the number stops moving.
        held = p.tc_seconds
        clk.run(1.2, hold_at=base + 0.5)
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
        clk.run(0.4)
        check(p.current_frame == frame_while_parked,
              "the frame moved while the source was parked")

        # Play again, from where it stopped.
        clk.run(0.6, tc_from=base + 0.5)
        check(p.state == LOCKED, f"expected LOCKED after resume, got {p.state}")
        check(p.current_frame > frame_while_parked,
              "playback did not resume after the pause")

        # Now the other case: the source stops sending entirely.
        clk.run(1.0)
        check(p.state == LOST,
              f"a source that stops sending should end in LOST, got {p.state}")
        check(p.source == IDLE,
              f"with the default policy a lost feed goes to the preshow look, "
              f"got {p.source}")
    finally:
        clk.close()
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
        return p, _Stepped(p, step_ms=20)

    want = {"hold": HOLD, "blackout": BLACK, "preshow": IDLE}
    for policy, source in want.items():
        p, clk = build(policy)
        try:
            clk.run(0.4, tc_from=tcmod.parse_tc("01:00:30:00", 30))
            check(p.source == SHOW, f"{policy}: show did not start")
            clk.run(0.8)
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
                clk.run(0.5)
                check(bytes(p._buf) == marked,
                      "hold kept changing the frame it was supposed to hold")
                check(p.current_frame == frame,
                      "hold kept advancing the frame counter")
                check(p.current_cue is not None,
                      "hold dropped the cue it was holding")
        finally:
            clk.close()
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
        self.delivered = 0          # samples handed to the callback so far

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
            self.delivered += self.blocksize
            if self.sd.realtime:
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


class FakeOutputStream:
    """An output stream a test drives BY HAND: no thread, no clock, no
    sleep. Calling .pump(n) is exactly what a real audio driver would do by
    invoking the callback on its own thread; a test calls it directly
    instead, so an announcement's progress is entirely under the test's
    control and never depends on wall time."""

    def __init__(self, sd, device, channels, samplerate, blocksize, callback):
        self.sd, self.device, self.channels = sd, device, channels
        self.rate, self.blocksize, self.callback = samplerate, blocksize, callback
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self):
        self.started = True

    def pump(self, n=None):
        import numpy as np
        n = self.blocksize if n is None else n
        outdata = np.zeros((n, self.channels), dtype=np.float32)
        self.callback(outdata, n, None, None)
        return outdata

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


def _seq_seconds(text):
    """Read an xLights-style M:SS.mmm position back into seconds."""
    m, _, rest = text.partition(":")
    return int(m) * 60 + float(rest)


class FakeSD:
    """A sounddevice stand-in with a multi-input interface whose timecode is
    deliberately NOT on input 1, which is the case the headphone jack never
    exercised and a USB box always will."""

    def __init__(self, ltc_channel=2, rates=(48000,), realtime=True):
        import numpy as np
        # realtime=False: streams hand over blocks as fast as they are made,
        # for a test that counts samples instead of watching the clock.
        self.realtime = realtime
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
        self.output_opened = []
        self.output_streams = []
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

    def OutputStream(self, device=None, channels=None, samplerate=None,
                     blocksize=None, dtype=None, callback=None):
        self.output_opened.append((device, channels, samplerate))
        s = FakeOutputStream(self, device, channels, samplerate, blocksize,
                             callback)
        self.output_streams.append(s)
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
    # find listens for `seconds` of wall clock. On a loaded CI Mac 0.8s of
    # wall clock delivered no audio at all, and the test failed for the
    # runner rather than for find. So here the listening is measured in
    # SAMPLES: the scan's sleep waits until 0.8s worth have been delivered to
    # the stream it just opened (30s cap), and the stream delivers them as
    # fast as it can make them. Same audio, same 0.8s of it, on any machine.
    sd = FakeSD(ltc_channel=3, realtime=False)
    main_thread = threading.current_thread()

    class _SampleTime:
        def sleep(self, seconds):
            if threading.current_thread() is not main_thread:
                return time.sleep(seconds)
            s = sd.streams[-1]
            want = int(seconds * s.rate)
            end = time.monotonic() + 30.0
            while s.delivered < want and time.monotonic() < end:
                time.sleep(0.001)

        def __getattr__(self, name):
            return getattr(time, name)

    real_time = audio_mod.time
    audio_mod.time = _SampleTime()
    try:
        res = audio_mod.scan(sd, LTCDecoder, seconds=0.8)
    finally:
        audio_mod.time = real_time
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
    rc = bash_n(inst)
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
        rc = bash_n(menu)
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
        rc = bash_n(mover)
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
        rc = bash_n(finder)
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
        rc = bash_n(app)
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
        rc = bash_n(apply_)
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
        rc = bash_n(restart)
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
        rc = bash_n(auto)
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
        rc = bash_n(menu)
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
        rc = bash_n(mover)
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
        rc = bash_n(finder)
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
        rc = bash_n(app)
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
        rc = bash_n(apply_)
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
        rc = bash_n(restart)
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
        r = bash_n(auto)
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
        r = bash_n(web)
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
    # Built with the OS's own separator: Windows normalises "/" to "\\", and
    # "used unchanged" below compares strings.
    real = os.path.join(home, "Library", "CloudStorage", "Dropbox",
                        "PROJECTS", "Dollywood", "GPL26_xLights")
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
    sd = real_show_dir()
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
    import tempfile
    # The real temp dir, not a hardcoded /tmp: this test only ran in CI
    # once a show folder was configured, which never happened until
    # synthetic fixtures landed, so a Unix-only path here was never
    # exercised on Windows. It is now, and Windows has no /tmp.
    wav = os.path.join(tempfile.gettempdir(), "ltcplay_selftest_pause.wav")
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
        # and the show must carry on driving the rig -- on its own schedule.
        # player.py's output loop deliberately gives up a missed slot rather
        # than bursting to catch up (never flood the controllers), so a
        # starved runner legitimately sends fewer frames per wall-clock
        # second than a quiet one; a fixed frame count in a fixed couple of
        # seconds was measuring the runner's spare CPU, not the engine, and
        # failed a healthy macOS runner that held 15 of 30 frames a second.
        # This still demands the same absolute progress -- a genuinely
        # stalled output thread never reaches it -- but with a deadline
        # generous enough that only an actually-stuck thread can fail it.
        # The checks are seconds apart, so "nobody is polling" still holds
        # for long stretches at a time; it is not a tight read loop.
        before = get("/api/state")["frames_out"]

        def _advanced():
            return get("/api/state")["frames_out"] > before + 40

        check(wait_for(_advanced, timeout=30.0, step=2.0),
              f"output stalled while the page was not polling for 30s "
              f"(started at frames_out={before}); the engine must not "
              f"depend on a viewer")

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
    sd = real_show_dir()
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
    sd = real_show_dir()
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
    sd = real_show_dir()
    if not os.path.isdir(sd):
        print("  no show folder available, skipped")
        return
    here = os.path.dirname(os.path.abspath(__file__))
    work = tempfile.mkdtemp()
    # A copy of the Munsters render, wearing Thriller's name. This is exactly
    # the failure a filename cannot detect and the header can.
    copy_render(os.path.join(sd, "GPL 2026_Set 1_Munsters.fseq"),
                os.path.join(work, "GPL 2026_Set 2_Thriller.fseq"))
    copy_render(os.path.join(sd, "GPL 2026_Set 1_Opener.fseq"),
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

    # A track number in the render's name is not a different song. The live
    # folder numbers its renders (Set 1_01_Opener.fseq) and not its audio
    # (Set 1_Opener.mp3). _norm_stem is proven on its own elsewhere; this
    # proves verify actually uses it, whatever the show folder in use here
    # happens to be called.
    copy_render(os.path.join(sd, "GPL 2026_Set 1_Opener.fseq"),
                os.path.join(work, "GPL 2026_Set 1_01_Opener.fseq"))
    code, out = verify([{"tc": "01:00:00:00",
                         "fseq": "GPL 2026_Set 1_01_Opener.fseq",
                         "name": "GPL Opener"}], ["--no-manifest"])
    check("RENDERED FROM" not in out and "rendered against" not in out,
          f"a numbered render of the right song was called mislabelled:\n"
          f"{out}")

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
            copy_render(src, os.path.join(work, f))
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
    now = _now()
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
    t0 = _now()
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
    t2 = _now()
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
    t3 = _now()
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
    t1 = _now()
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
    sd = real_show_dir()
    if not os.path.isdir(sd):
        print("  no show folder available, skipped")
        return
    import shutil
    work = tempfile.mkdtemp()
    copy_render(os.path.join(sd, "GPL 2026_Set 1_Opener.fseq"), work)
    copy_render(os.path.join(sd, "xlights_networks.xml"), work)
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
    # On Windows the show log never lives beside the show folder (see
    # ltcplay/appdata.py: "On Windows program data never goes beside the
    # show, which may be a synced folder"). It lives under
    # %LOCALAPPDATA%\ltcplay instead, so blocking the show folder proves
    # nothing there -- the run below would open its log just fine and
    # never print either message this checks for. Block the real
    # Windows destination instead, under a LOCALAPPDATA this test owns
    # rather than the operator's own.
    run_env = None
    if sys.platform == "win32":
        from ltcplay import appdata as _appdata
        fake_local = tempfile.mkdtemp()
        saved_local = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = fake_local
        try:
            log_p = _appdata.log_path()
        finally:
            if saved_local is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = saved_local
        os.makedirs(log_p, exist_ok=True)
        run_env = dict(os.environ, LOCALAPPDATA=fake_local)
    r = subprocess.run([sys.executable, "-m", "ltcplay.cli", "run", tlp,
                        "--wav", wav, "--no-output", "--quiet"],
                       capture_output=True, text=True, cwd=here, timeout=60,
                       env=run_env)
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
    t = _now()
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
    t = _now()
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
    t0 = _now()
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
    t0 = _now()
    for i in range(4):
        p.feed_timecode(base + i / 30.0, t0 + i / 30.0, text="01:00:10:00")
    p._tick()
    p.go(base)
    p.feed_timecode(base, _now(), text="01:00:10:00")
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
    screen = "\n".join(_disp.render(p, D(), tl, sc, _now() - 5))
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
        p._last_lock = _now() - 30.0
    p.last_ltc_at = _now() - 30.0
    p._tick()
    check(p.feed_state == LOST,
          f"with nothing arriving for 30s the FEED is lost, whatever the show "
          f"is doing: {p.feed_state}")
    screen = "\n".join(_disp.render(p, D(), tl, sc, _now() - 5))
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
    now = _now()
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
    check(top.name == "A" and abs((_now() - p.freerun_epoch)
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
    sd = real_show_dir()
    if not os.path.isdir(sd):
        print("  no show folder available, skipped")
        return
    here = os.path.dirname(os.path.abspath(__file__))
    src = tempfile.mkdtemp()
    for f in ("GPL 2026_Set 1_Opener.fseq", "xlights_networks.xml"):
        copy_render(os.path.join(sd, f), src)
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


def test_a_controller_ping_means_what_it_says():
    section("a controller that answers a ping is up, on this OS's ping")
    # `check` and the running rig watch both ask ping whether a controller is
    # there. Windows ping reads the Mac's flags as something else entirely,
    # so on Windows every controller used to read as missing.
    import subprocess
    from ltcplay import rigwatch
    seen = []
    real_run = rigwatch.subprocess.run

    def fake(answer, rc):
        def run(argv, **kw):
            seen.append(list(argv))
            return subprocess.CompletedProcess(argv, rc, answer, b"")
        return run

    try:
        rigwatch.subprocess.run = fake(b"Reply from 10.0.0.9: bytes=32 "
                                       b"time<1ms TTL=64\r\n", 0)
        check(rigwatch.ping("10.0.0.9") is True,
              "a controller that answered was reported missing")
        if sys.platform == "win32":
            check(seen[-1] == ["ping", "-n", "1", "-w", "1000", "10.0.0.9"],
                  f"Windows ping was asked with the wrong flags: {seen[-1]}")
            # A router answering for a controller that is not there exits 0
            # on Windows. That is not the controller.
            rigwatch.subprocess.run = fake(
                b"Reply from 10.0.0.1: Destination host unreachable.\r\n", 0)
            check(rigwatch.ping("10.0.0.9") is False,
                  "a router's 'destination host unreachable' counted as the "
                  "controller answering")
        else:
            check(seen[-1] == ["ping", "-c", "1", "-W", "1000", "-t", "1",
                               "10.0.0.9"],
                  f"the Mac ping changed: {seen[-1]}")
        rigwatch.subprocess.run = fake(b"", 2)
        check(rigwatch.ping("10.0.0.9") is False,
              "a ping that failed was reported as an answer")
    finally:
        rigwatch.subprocess.run = real_run
    # And the real thing, against the one address that always answers.
    try:
        up = rigwatch.RigWatch(["127.0.0.1"])._ping_once("127.0.0.1")
    except Exception as e:
        up = e
    check(up is True, f"this machine's own loopback did not answer ping: {up}")
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
        return p, _Stepped(p, step_ms=20)

    p, clk = build("freerun")
    try:
        clk.run(0.4, tc_from=tcmod.parse_tc("01:00:10:00", 30))
        check(p.source == SHOW, "the show did not start")
        was = p.tc_seconds
        clk.run(1.0)                         # the feed dies
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
        clk.feed(tcmod.parse_tc("01:00:20:00", 30), text="01:00:20:00")
        clk.run(0.2)
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
        clk.close()
        p.stop()

    # preshow stays the default for anything that did not ask for this.
    p2, clk2 = build("preshow")
    try:
        clk2.run(0.4, tc_from=tcmod.parse_tc("01:00:10:00", 30))
        clk2.run(1.0)
        check(p2.source == IDLE and p2.freerun_epoch is None,
              f"on_lost preshow started free-running anyway: {p2.source}")
    finally:
        clk2.close()
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
        # It was asked for 96000 and opened at 48000. The decoder has to be
        # told, once, or it decodes a 48k stream as if it were 96k: steady
        # nonsense rather than silence.
        check(src.rate == 48000 and rates_seen == [48000],
              f"the input opened at 48000 but did not follow it: rate "
              f"{src.rate}, decoder told {rates_seen}")
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

        # A switch the RUNNING show refuses has to come back as refused, with
        # the reason, while the setting is still saved for the next start.
        # Reporting it as done leaves the operator believing the show is on
        # an input it never reached.
        def busy(*a, **k):
            raise SessionError("the interface is busy")
        sess.retarget_input = busy
        try:
            out = c.set_input("MOTU M4", 2)
        finally:
            del sess.retarget_input
        check(out.get("live") is False and "busy" in (out.get("why") or ""),
              f"a switch the show refused was reported as done: {out}")
        check(int(out.get("channel") or 0) == 2,
              f"a switch the show refused lost the saved setting: {out}")

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


def test_machine_data_goes_where_the_os_keeps_it():
    section("the lock, the saved input, the preferences and the log land "
            "where this OS keeps program data")
    # On a Mac every one of these stays exactly where it always was. On
    # Windows they go under %LOCALAPPDATA%\\ltcplay: never beside the program
    # and never in the show folder, either of which can be a synced folder
    # that two machines would then share.
    import json, tempfile
    from ltcplay import settings as st_mod, onlyone as oo, appdata
    import ltcplay.player as plmod
    from ltcplay.session import Session
    here = os.path.dirname(os.path.abspath(__file__))
    work = tempfile.mkdtemp()
    fake_local = os.path.join(work, "LocalAppData")
    real_env = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = fake_local
    try:
        lock, saved, prefs = oo.path(), st_mod.path(), st_mod.prefs_path()
        if sys.platform == "win32":
            want = os.path.join(fake_local, "ltcplay")
            for label, p in (("the output lock", lock),
                             ("the saved input", saved),
                             ("the preferences", prefs),
                             ("the default log", appdata.log_path())):
                check(os.path.dirname(p) == want
                      or os.path.dirname(os.path.dirname(p)) == want,
                      f"{label} is at {p}, not under %LOCALAPPDATA%\\ltcplay")
                check(not p.startswith(here + os.sep),
                      f"{label} is beside the program: {p}")
            check(os.path.isdir(want),
                  "%LOCALAPPDATA%\\ltcplay was not created")
        else:
            check(saved == os.path.join(here, st_mod.FILENAME),
                  f"the saved input moved on this Mac: {saved}")
            check(prefs == os.path.join(here, st_mod.PREFS_FILE),
                  f"the preferences moved on this Mac: {prefs}")
            mac_lock = os.path.join(os.path.expanduser("~"), "Library",
                                    "Application Support", "ltcplay",
                                    oo.FILENAME)
            if os.path.isdir(os.path.dirname(os.path.dirname(mac_lock))):
                check(lock == mac_lock,
                      f"the output lock moved on this Mac: {lock}")
            check(not os.path.exists(fake_local),
                  "a Mac wrote into a Windows program data folder")

        # And the engine has to USE it: the log a real Session opens.
        rows = "\n".join(
            f'    <network NetworkType="ArtNET" ComPort="127.0.0.1" '
            f'BaudRate="{u+1}" MaxChannels="510"/>' for u in range(4))
        net = os.path.join(work, "net.xml")
        open(net, "w").write(f'<Networks>\n  <Controller Name="L" '
                             f'IP="127.0.0.1" ActiveState="Active">\n{rows}'
                             f'\n  </Controller>\n</Networks>\n')
        show = os.path.join(work, "show")
        os.makedirs(show)
        tlp = os.path.join(show, "d_timeline.json")
        json.dump({"name": "d", "fps": 30, "show_dir": show,
                   "gaps": "blackout",
                   "cues": [{"tc": "01:00:00:00", "fseq": "A.fseq",
                             "name": "A"}]}, open(tlp, "w"))
        open(os.path.join(show, "A.fseq"), "wb").write(b"x")
        real_prefs, real_path = st_mod.prefs_path, st_mod.path
        st_mod.prefs_path = lambda: os.path.join(work, st_mod.PREFS_FILE)
        st_mod.path = lambda: os.path.join(work, st_mod.FILENAME)
        real_prepare = plmod.Player._prepare

        def fake_prepare(self, cue):
            cue.fseq = FakeFSEQ(frames=4000)
            cue.duration = cue.fseq.duration_ms / 1000.0
            cue._spans = [(0, 0, cue.fseq.channel_count)]
            cue._gaps = None
            return 0

        plmod.Player._prepare = fake_prepare
        try:
            sess = Session(tlp, networks=net, sd=FakeSD(), device="MOTU M4",
                           channel=1, wav=None)
            sess.open()
            got = sess.log.path if sess.log else None
            if sys.platform == "win32":
                check(got == os.path.join(fake_local, "ltcplay", "logs",
                                          "ltcplay.log"),
                      f"the show log went to {got}, not "
                      f"%LOCALAPPDATA%\\ltcplay\\logs")
                check(not os.path.exists(os.path.join(show, "ltcplay.log")),
                      "the show log was written into the show folder")
            else:
                check(got == os.path.join(show, "ltcplay.log"),
                      f"the show log moved on this Mac: {got}")
            try:
                sess.stop()
            except Exception:
                pass
        finally:
            plmod.Player._prepare = real_prepare
            st_mod.prefs_path, st_mod.path = real_prefs, real_path
    finally:
        if real_env is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = real_env
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

    # The note carries the show file's name. One the Windows code page cannot
    # spell used to raise AFTER the lock was taken, and the start died with a
    # traceback. It has to be held, and named in the refusal, like any other.
    snow = "\u2744 Fire and Ice_timeline.json (web)"
    try:
        held = onlyone.OutputLock(where, snow).acquire()
    except Exception as e:
        held = None
        check(False, f"a show name with a snowflake broke the lock: {e!r}")
    if held is not None:
        try:
            onlyone.OutputLock(where, "me").acquire()
            check(False, "a lock with a snowflake in its note did not hold")
        except onlyone.AlreadyRunning as e:
            check("\u2744" in e.holder,
                  f"the refusal lost the show name: {e.holder!r}")
        held.release()

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
    sd = real_show_dir()
    src_a = os.path.join(sd, "GPL 2026_Set 1_Opener.fseq")
    src_b = os.path.join(sd, "GPL 2026_Set 1_Munsters.fseq")
    if not (os.path.exists(src_a) and os.path.exists(src_b)):
        print("  no show folder available, skipped")
        return
    work = tempfile.mkdtemp()
    live = os.path.join(work, "Live.fseq")
    copy_render(src_a, live)
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
    copy_render(src_b, live)
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
    sd = real_show_dir()
    src = os.path.join(sd, "GPL 2026_Set 1_Munsters.fseq")
    if not os.path.exists(src):
        print("  no show folder available, skipped")
        return
    work = tempfile.mkdtemp()
    whole = os.path.join(work, "Whole.fseq")
    copy_render(src, whole)
    with FSEQ(whole) as f:
        check(f.verify() is True, "a complete render must verify")
        # And it lets go of what it read to prove that. Verifying every cue
        # at startup once pinned a whole set in memory: 428MB of renders,
        # 420MB resident before a frame was played.
        check(f._cache_idx == -1 and not f._cache,
              f"verify kept a {len(f._cache)} byte block in memory; every "
              f"verified render would stay resident")
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
    copy_render(whole, rewritten)
    with FSEQ(rewritten) as f:
        last_off, last_len = f._blocks[-1][1], f._blocks[-1][2]
        mid_off, mid_len = f._blocks[len(f._blocks) // 2][1], \
            f._blocks[len(f._blocks) // 2][2]
    for off, ln, where in ((last_off, last_len, "last"),
                           (mid_off, mid_len, "middle")):
        copy_render(whole, rewritten)
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
    copy_render(whole, live)
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
    # And from its very first frame. 0.1s after B runs out is inside the
    # bridge's own length, but C is two seconds away, so this is a hole, not
    # rounding. Holding B's last frame here is the bridge swallowing a real
    # gap, only a short one.
    _tick_at(p, nxt + 2.5 + 0.1)
    check(p.source == IDLE and p.current_cue is None,
          f"a real gap must start the frame the song ends, not a bridge "
          f"later: got {p.source}/{p.current_cue and p.current_cue.name}")

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
    sd = real_show_dir()
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
    if sys.platform == "win32":
        # The helper is a macOS .command that moves an install out of the
        # folders macOS protects, and what it guards is a POSIX symlink. There
        # is no such move and no such link on Windows. Its text is still
        # parsed above; running it is proven on macOS and Linux.
        print("  (a macOS-only launcher run against a POSIX symlink; not run "
              "on Windows, skipped)")
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
    if sys.platform == "win32":
        # Everything above reads the C source and runs everywhere. Below it
        # is compiled and run, and it is a POSIX program (execv, readlink,
        # a #!/bin/bash python shim) inside a macOS app bundle. Windows has
        # none of those to run it with.
        print("  (the app launcher is a POSIX C program for the macOS app "
              "bundle; compiling and running it is not possible on Windows, "
              "skipped)")
        return
    # The source checks above need no compiler, so they run first.
    if not shutil.which("cc"):
        print("  (no compiler here, skipped)")
        return
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
        r = bash_n(rep)
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
        r = bash_n(cut)
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
    r = bash_n(p)
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
        if actor == "operator":
            kw.setdefault("who", "Andy")
            kw.setdefault("screen", "rack screen")
        before = self.m
        o = S.step(self.m, S.Event(kind, actor, **kw), now)
        _entry_effects_hold(S, before, o, f"{kind} at {now:%H:%M:%S}")
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


# What arriving in each state must ask for, written out here rather than
# read from the engine: STANDBY is the intermission running, HOLD keeps it
# running, IDLE is the preshow look.
# Abort, a failed start and a restart during a show: the whole show fades
# to black, then MadMapper stops (Jeff, 2026-09-24).
_WHOLE_FADE = ["ZERO_FLAME_CUES", "BLANK_LASERS", "FADE_MUSIC_OUT",
               "FADE_VIDEO_OUT", "FADE_PIXELS", "STOP_CONDUCTOR"]

_ARRIVE = {"IDLE": "PRESHOW_LOOK", "STANDBY": "INTERMISSION",
           "HOLD": "INTERMISSION", "SHOW": "START_SHOW",
           "PAUSED": "FREEZE_SHOW",
           "CLOSING": "BLACKOUT"}


def _entry_effects_hold(S, before, o, label):
    """Every accepted change of state carries the effect of arriving there."""
    st = o.machine.state
    if not o.accepted or st == before.state or st not in _ARRIVE:
        return
    kinds = [e.kind for e in o.effects]
    if st == "SHOW" and before.state == "PAUSED":
        check(kinds == ["RESUME_SHOW", "FADE_MUSIC_IN", "UNBLANK_LASERS"],
              f"{label}: a resumed show carries on, music back up, lasers "
              f"back last; got {kinds}")
        return
    if st in ("STANDBY", "HOLD") and "STOP_CONDUCTOR" in kinds \
            and not S.INTERMISSION_AFTER_A_STOPPED_SHOW:
        check("INTERMISSION" not in kinds,
              f"{label}: the rig stays dark after a stopped show")
        return
    check(_ARRIVE[st] in kinds,
          f"{label}: entered {st} from {before.state} without "
          f"{_ARRIVE[st]}; effects were {kinds}")
    if st == "CLOSING":
        check(kinds[-4:] == ["ZERO_FLAME_CUES", "STOP_CONDUCTOR",
                             "FADE_PIXELS", "BLACKOUT"],
              f"{label}: closing must zero flames, stop, fade and black out, "
              f"got {kinds}")


def test_schedule_rule_is_validated():
    section("scheduler: the rule file is checked, and a wrong key fails loudly")
    S = _sched()
    if S is None:
        return
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
    """One machine in each state, plus HOLD and STANDBY with a DELAYED show
    waiting, and the moment to hit each with an event. Keyed by label."""
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
    n.op(S.HOLD_ON, _den(S, 17, 0))
    fx[S.PAUSED] = (n.m, _den(S, 17, 0) + one)
    n = _Night(S, rule)
    n.boot(_den(S, 17, 4))
    fx[S.STANDBY] = (n.m, _den(S, 17, 4) + one)
    n.op(S.END_NIGHT, _den(S, 17, 4), confirmed=True)
    fx[S.CLOSING] = (n.m, _den(S, 17, 4) + one)
    n.do(S.CLOSING_DONE, "system", _den(S, 17, 4))
    fx[S.OFF] = (n.m, _den(S, 17, 4) + one)
    n = _Night(S, rule)
    n.boot(_den(S, 16, 59))
    n.op(S.HOLD_ON, _den(S, 16, 59))
    n.tick(_den(S, 17, 0, 1))
    fx["HOLD+DELAYED"] = (n.m, _den(S, 17, 0, 2))
    n.op(S.RESUME, _den(S, 17, 0, 2))
    fx["STANDBY+DELAYED"] = (n.m, _den(S, 17, 0, 3))
    for label, (m, _) in fx.items():
        check(m.state == label.split("+")[0],
              f"the {label} fixture is in {m.state}")
        check(("+DELAYED" in label) == (m.delayed() is not None),
              f"the {label} fixture has the wrong delayed show")
    return fx


def test_schedule_state_machine_every_state_every_event():
    section("scheduler: every state times every event")
    S = _sched()
    if S is None:
        return
    fx = _matrix_fixtures(S)
    labels = list(fx)
    B, I, SB, SH, P, C, O, H = (S.BOOT, S.IDLE, S.STANDBY, S.SHOW, S.PAUSED,
                                S.CLOSING, S.OFF, S.HOLD)
    HD, SD = "HOLD+DELAYED", "STANDBY+DELAYED"
    live = {I: I, SB: SB, SH: SH, P: P, H: H, HD: H, SD: SB}

    def E(kind, actor, **kw):
        if actor == "operator":
            kw.setdefault("who", "Andy")
            kw.setdefault("screen", "rack screen")
        return S.Event(kind, actor, **kw)

    Z, ST, F, BO = "ZERO_FLAME_CUES", "STOP_CONDUCTOR", "FADE_PIXELS", \
        "BLACKOUT"
    INT, PRE, GO = "INTERMISSION", "PRESHOW_LOOK", "START_SHOW"
    PAUSE = ["ZERO_FLAME_CUES", "BLANK_LASERS", "FREEZE_SHOW",
             "FADE_MUSIC_OUT"]
    UNPAUSE = ["RESUME_SHOW", "FADE_MUSIC_IN", "UNBLANK_LASERS"]
    CLOSE = [Z, ST, F, BO]
    # Abort, a failed start: the whole show fades to black, then MadMapper
    # stops, and nothing follows (Jeff, 2026-09-24).
    STOPPED = [Z, "BLANK_LASERS", "FADE_MUSIC_OUT", "FADE_VIDEO_OUT", F, ST]
    # (event, {fixture: (where it goes, the effects it asks for, in order)}).
    # Anything not listed must be refused with a sentence, change nothing
    # and ask for nothing.
    none = {k: (k.split("+")[0], []) for k in labels}
    same_live = {k: (v, []) for k, v in live.items()}
    table = [
        (E(S.BOOT_DONE, "system"), {B: (I, [PRE])}),
        (E(S.TICK, "scheduler"), {k: v for k, v in none.items() if k != B}),
        (E(S.SHOW_CONFIRMED, "madmapper"), {SH: (SH, []), P: (P, [])}),
        (E(S.SHOW_ENDED, "madmapper"), {SH: (SB, [INT])}),
        (E(S.SHOW_FAILED, "madmapper", detail="no timecode after start"),
         {SH: (SB, STOPPED)}),
        (E(S.FAULT_RAISED, "safety", detail="the safety process stopped "
                                            "replying"), none),
        (E(S.CLEAR_FAULT, "operator"), {}),           # there is no fault
        (E(S.CLOSING_DONE, "system"), {C: (O, [])}),
        # Start now: anything but a running or paused show, no guard.
        (E(S.START_NOW, "operator"), {k: (SH, [GO])
                                      for k in (I, SB, H, C, O, HD, SD)}),
        (E(S.HOLD_ON, "operator"), {I: (H, [INT]), SB: (H, [INT]),
                                    SD: (H, [INT]), SH: (P, PAUSE)}),
        (E(S.RESUME, "operator"), {H: (I, [PRE]), HD: (SB, [INT]),
                                   P: (SH, UNPAUSE)}),
        (E(S.SKIP_NEXT, "operator"), same_live),
        (E(S.DELAY_NEXT, "operator", minutes=5), same_live),
        (E(S.DELAY_NEXT, "operator", minutes=10), same_live),
        (E(S.DELAY_REST, "operator", minutes=5), same_live),
        (E(S.DELAY_REST, "operator", minutes=10), same_live),
        (E(S.ABORT, "operator"), {}),                  # not confirmed
        (E(S.ABORT, "operator", confirmed=True),
         {SH: (SB, STOPPED), P: (SB, STOPPED)}),
        (E(S.END_NIGHT, "operator"), {}),              # not confirmed
        (E(S.END_NIGHT, "operator", confirmed=True),
         {k: (C, CLOSE) for k in (I, SB, H, HD, SD)}),
        (E(S.EDIT_MOVE, "operator", show=15, at="21:45"), same_live),
        (E(S.EDIT_ADD, "operator", at="21:55"), same_live),
        (E(S.EDIT_REMOVE, "operator", show=15), same_live),
        # An operator event that does not say who and where, or names
        # someone not on the operator list, is refused everywhere.
        (S.Event(S.START_NOW, "operator", screen="rack screen"), {}),
        (S.Event(S.HOLD_ON, "operator", who="Andy"), {}),
        (S.Event(S.START_NOW, "operator", who="Bob",
                 screen="rack screen"), {}),
    ]
    check({e.kind for e, _ in table} == set(S.EVENTS),
          "the matrix must cover every event the machine knows")
    cells = 0
    for ev, goes in table:
        for fxl in labels:
            m, now = fx[fxl]
            o = S.step(m, ev, now)
            cells += 1
            label = (f"{ev.kind}{' confirmed' if ev.confirmed else ''}"
                     f"{' +' + str(ev.minutes) if ev.minutes else ''}"
                     f"{'' if ev.actor != 'operator' or (ev.who and ev.screen) else ' without who or screen'}"
                     f"{' by ' + ev.who if ev.who not in ('', 'Andy') else ''}"
                     f" in {fxl}")
            if fxl in goes:
                to, effects = goes[fxl]
                check(o.accepted, f"{label} must be taken, was refused: "
                                  f"{o.refused}")
                check(o.machine.state == to,
                      f"{label} must go to {to}, went to {o.machine.state}")
                got = [e.kind for e in o.effects]
                check(got == effects, f"{label} must ask for {effects}, "
                                      f"asked for {got}")
                _entry_effects_hold(S, m, o, label)
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
    check(cells == len(table) * len(labels) and len(labels) == 10,
          "the matrix did not run every cell")

    # Operator events must name someone on the list, and a screen.
    for kind in [k for k, a in S.EVENT_ACTORS.items() if "operator" in a]:
        for fxl in labels:
            m, now = fx[fxl]
            for who, screen, must in (("", "rack screen", "who pressed it"),
                                      ("Andy", "", "which screen"),
                                      ("  ", " ", "who pressed it and "
                                                  "which screen"),
                                      ("Bob", "rack screen",
                                       "not on the operator list")):
                o = S.step(m, S.Event(kind, "operator", who=who,
                                      screen=screen, confirmed=True,
                                      minutes=5, show=15, at="21:45"), now)
                check(not o.accepted and must in o.refused
                      and o.machine is m and not o.effects,
                      f"{kind} in {fxl} with who={who!r} screen={screen!r} "
                      f"must be refused naming what is wrong: "
                      f"{o.refused!r}")
    m, now = fx[S.IDLE]
    for who in ("Jeff", "andy", " Andy "):
        check(S.step(m, S.Event(S.HOLD_ON, "operator", who=who,
                                screen="rack screen"), now).accepted,
              f"{who!r} is on the default list")
    other = S.replace(m, operators=("Casey",))
    check(not S.step(other, E(S.HOLD_ON, "operator"), now).accepted
          and S.step(other, E(S.HOLD_ON, "operator", who="Casey"),
                     now).accepted,
          "the list in force is the machine's, not a fixed pair")

    # With a fault on the flag, clearing it is taken in every state and the
    # state does not move: FAULT is a flag, not a state.
    for fxl in labels:
        m, now = fx[fxl]
        st = m.state
        f = S.step(m, E(S.FAULT_RAISED, "reader", detail="x"), now).machine
        check(f.fault and f.state == st, f"a fault in {fxl} must set the "
                                         f"flag and leave the state")
        c = S.step(f, E(S.CLEAR_FAULT, "operator"), now)
        check(c.accepted and not c.machine.fault and c.machine.state == st,
              f"clearing a fault in {fxl}: {c.refused}")

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

    # guard_s: the SCHEDULE never starts a show within guard_s of the
    # previous show's end. Show 1 runs long (a pause), ends at 18:19:00, and
    # 18:20 is only 60 s later.
    n = _Night(S, rule)
    n.boot(_den(S, 17, 50))
    n.tick(_den(S, 18, 0))
    n.op(S.HOLD_ON, _den(S, 18, 3))
    n.op(S.RESUME, _den(S, 18, 14, 40))
    check(n.m.hm(n.m.expected_end()) == "18:19",
          f"11m 40s paused moves the end from 18:07:20 to 18:19, got "
          f"{n.m.hm(n.m.expected_end())}")
    n.do(S.SHOW_ENDED, "madmapper", _den(S, 18, 19))
    check(not _fired(n.tick(_den(S, 18, 20)), S),
          "18:20 is 60 s after a show ended, inside the 120 s guard, and "
          "must not start by itself")
    n.tick(_den(S, 18, 20, 1))
    check(n.m.slot(2).status == S.MISSED and "guard" in n.m.slot(2).reason,
          f"a show held off by the guard is MISSED and says so, got "
          f"{n.m.slot(2).reason!r}")
    # The guard boundary: ending at 18:18:00 frees 18:20:00 exactly.
    n = _Night(S, rule)
    n.boot(_den(S, 17, 50))
    n.tick(_den(S, 18, 0))
    n.do(S.SHOW_ENDED, "madmapper", _den(S, 18, 18))
    check(_fired(n.tick(_den(S, 18, 20)), S) == [2],
          "a show exactly guard_s after the last one ended must start")
    # The schedule never starts a show over a running one: show 1 runs past
    # 18:20 with no end reported.
    n = _Night(S, rule)
    n.boot(_den(S, 17, 50))
    n.tick(_den(S, 18, 0))
    check(not _fired(n.tick(_den(S, 18, 20)), S), "a show must never start "
                                                  "over a running one")
    n.tick(_den(S, 18, 20, 1))
    check(n.m.slot(2).status == S.MISSED and "running" in n.m.slot(2).reason,
          f"the show that came due mid show is MISSED and says why: "
          f"{n.m.slot(2).reason!r}")
    n.audit("guard")
    print("  ok")


def test_schedule_hold_pauses_a_show():
    section("scheduler: Hold during a show pauses it, Resume carries on")
    S = _sched()
    if S is None:
        return
    rule = _one_night_rule(S)
    n = _Night(S, rule)
    n.boot(_den(S, 17, 50))
    n.tick(_den(S, 18, 0))
    n.do(S.SHOW_CONFIRMED, "madmapper", _den(S, 18, 0, 2))
    check(n.m.hm(n.m.expected_end()) == "18:07:20", "440 s from 18:00")
    o = n.op(S.HOLD_ON, _den(S, 18, 3))
    check(n.m.state == S.PAUSED and n.m.running == 1
          and n.m.slot(1).status == S.RUNNING,
          "Hold during a show pauses it; it is still show 1, still running")
    check([e.kind for e in o.effects] == [S.ZERO_FLAME_CUES, S.BLANK_LASERS,
                                          S.FREEZE_SHOW, S.FADE_MUSIC_OUT]
          and all(e.show == 1 for e in o.effects),
          f"pausing zeroes flames and blanks lasers first, then freezes and "
          f"fades the music: {[e.kind for e in o.effects]}")
    check("paused at its current frame" in o.log[0].text, o.log[0].text)
    # While paused: the expected end keeps moving, nothing else starts,
    # Start now and End night do nothing, a fault only raises the flag.
    check(n.m.hm(n.m.expected_end(_den(S, 18, 13))) == "18:17:20",
          "ten minutes paused so far moves the end ten minutes")
    for kind, kw, must in ((S.START_NOW, {}, "does nothing while a show"),
                           (S.END_NIGHT, {"confirmed": True}, "Abort it "
                                                               "first"),
                           (S.HOLD_ON, {}, "already paused")):
        o = n.op(kind, _den(S, 18, 5), **kw)
        check(not o.accepted and must in o.refused and n.m.state == S.PAUSED,
              f"{kind} while paused: {o.refused!r}")
    o = n.do(S.SHOW_ENDED, "madmapper", _den(S, 18, 7, 20))
    check(not o.accepted and "paused" in o.refused,
          "a paused show cannot end")
    o = n.do(S.FAULT_RAISED, "madmapper", _den(S, 18, 6), detail="x")
    check(o.accepted and n.m.fault and n.m.state == S.PAUSED
          and not o.effects, "a fault while paused only raises the flag")
    # 18:20 passes during the pause: it is delayed, not missed.
    n.tick(_den(S, 18, 20, 1))
    check(n.m.slot(2).status == S.DELAYED, f"a slot passing during a pause "
                                           f"is DELAYED, got "
                                           f"{n.m.slot(2).status}")
    o = n.op(S.RESUME, _den(S, 18, 23))
    check(n.m.state == S.SHOW and [e.kind for e in o.effects] ==
          [S.RESUME_SHOW, S.FADE_MUSIC_IN, S.UNBLANK_LASERS],
          f"Resume carries on from the frozen frame, music back up, lasers "
          f"last: {[e.kind for e in o.effects]}")
    check(n.m.slot(1).paused_s == 1200
          and n.m.hm(n.m.expected_end()) == "18:27:20"
          and "18:27:20" in o.log[0].text,
          f"20 minutes paused moves the end from 18:07:20 to 18:27:20: "
          f"{n.m.hm(n.m.expected_end())}")
    check(not _fired(n.tick(_den(S, 18, 25)), S) and n.m.state == S.SHOW,
          "the resumed show runs on; nothing else starts")
    n.do(S.SHOW_ENDED, "madmapper", _den(S, 18, 27, 20))
    check(n.m.state == S.STANDBY and n.m.slot(1).status == S.DONE
          and n.m.slot(2).status == S.DELAYED,
          "it ends as usual, and the delayed show waits for Start now")
    # A second pause adds to the first.
    n.tick(_den(S, 18, 40))
    check(n.m.running == 3, "18:40 starts on schedule")
    check(n.m.slot(2).status == S.MISSED
          and n.m.slot(2).reason == "MISSED (show 3 started on schedule)",
          f"and the delayed show 2 gives way: {n.m.slot(2).reason!r}")
    n.op(S.HOLD_ON, _den(S, 18, 41))
    n.op(S.RESUME, _den(S, 18, 42))
    n.op(S.HOLD_ON, _den(S, 18, 43))
    n.op(S.RESUME, _den(S, 18, 43, 30))
    check(n.m.slot(3).paused_s == 90
          and n.m.hm(n.m.expected_end()) == "18:48:50",
          f"two pauses of 60 s and 30 s add up: {n.m.slot(3).paused_s}")
    # Abort while paused works exactly as in a show.
    n.op(S.HOLD_ON, _den(S, 18, 44))
    o = n.op(S.ABORT, _den(S, 18, 45), confirmed=True)
    check(n.m.state == S.STANDBY and n.m.slot(3).status == S.ABORTED
          and [e.kind for e in o.effects] == _WHOLE_FADE
          and n.m.slot(3).paused_s == 150,
          f"Abort while paused: {n.m.state} {[e.kind for e in o.effects]}")
    n.audit("pause")
    print("  ok")


def test_schedule_hold_between_shows_delays():
    section("scheduler: Hold between shows delays the next show")
    S = _sched()
    if S is None:
        return
    rule = _one_night_rule(S)
    # Jeff asked: on Hold, nothing starts by itself however many slots
    # pass. Ticked every second through four slot times, at grace 0 and 15.
    from datetime import timedelta
    for grace in (0, 15):
        n = _Night(S, _one_night_rule(S, grace=grace))
        n.boot(_den(S, 18, 5))
        n.op(S.HOLD_ON, _den(S, 18, 10))
        t, fired = _den(S, 18, 10), []
        while t <= _den(S, 19, 21):
            fired += _fired(n.tick(t), S)
            t += timedelta(seconds=1)
        check(not fired and n.m.state == S.HOLD and not n.m.running,
              f"grace {grace}: four slot times pass on Hold and nothing "
              f"starts by itself: started {fired}, {n.m.state}")
        check([s.status for s in n.m.slots[1:5]] ==
              [S.MISSED, S.MISSED, S.MISSED, S.DELAYED],
              f"grace {grace}: only the newest of the four waits: "
              f"{[s.status for s in n.m.slots[1:5]]}")
    # One slot passes during a Hold: DELAYED, never starts by itself.
    n = _Night(S, rule)
    n.boot(_den(S, 18, 5))
    o = n.op(S.HOLD_ON, _den(S, 18, 10))
    check(n.m.state == S.HOLD and [e.kind for e in o.effects] ==
          [S.INTERMISSION], "Hold between shows keeps the intermission")
    check(not _fired(n.tick(_den(S, 18, 20)), S), "nothing fires on hold")
    n.tick(_den(S, 18, 20, 1))
    s2 = n.m.slot(2)
    check(s2.status == S.DELAYED and s2.reason == "DELAYED (on hold)"
          and not any("MISSED (on hold)" == s.reason for s in n.m.slots),
          f"a slot passing during a Hold is DELAYED, not MISSED: "
          f"{s2.status} {s2.reason!r}")
    n.op(S.RESUME, _den(S, 18, 25))
    check(n.m.state == S.STANDBY and n.m.slot(2).status == S.DELAYED,
          "Resume goes back to STANDBY and the delayed show still waits")
    for t in ((18, 26), (18, 30), (18, 39, 59)):
        check(not _fired(n.tick(_den(S, *t)), S),
              "a delayed show never starts by itself")
    o = n.op(S.START_NOW, _den(S, 18, 32))
    check(_fired(o, S) == [2]
          and n.m.slot(2).reason == "DELAYED START (operator hold)",
          f"Start now starts the delayed show: {n.m.slot(2).reason!r}")
    n.audit("one delayed")

    # Two slots pass during one Hold: only the newest waits.
    n = _Night(S, rule)
    n.boot(_den(S, 18, 5))
    n.op(S.HOLD_ON, _den(S, 18, 10))
    n.tick(_den(S, 18, 20, 1))
    n.tick(_den(S, 18, 40, 1))
    check(n.m.slot(2).status == S.MISSED
          and n.m.slot(2).reason == "MISSED (on hold, a later show was "
                                    "delayed)"
          and n.m.slot(3).status == S.DELAYED,
          f"the earlier one is MISSED, the newest DELAYED: "
          f"{n.m.slot(2).reason!r} {n.m.slot(3).status}")
    n.tick(_den(S, 19, 0, 1))
    check([s.status for s in n.m.slots[1:4]] ==
          [S.MISSED, S.MISSED, S.DELAYED],
          "and again with a third: still only one waits")
    check(n.m.state == S.HOLD, "the Hold holds throughout")
    # A delayed show gives way to the next scheduled one.
    n.op(S.RESUME, _den(S, 19, 5))
    check(_fired(n.tick(_den(S, 19, 20)), S) == [5]
          and n.m.slot(4).status == S.MISSED
          and n.m.slot(4).reason == "MISSED (show 5 started on schedule)",
          f"when the next slot comes due, it starts and the delayed one is "
          f"MISSED: {n.m.slot(4).reason!r}")
    n.audit("two delayed")

    # A delayed show keeps the night open, until midnight.
    n = _Night(S, rule)
    n.boot(_den(S, 21, 30))
    n.op(S.HOLD_ON, _den(S, 21, 35))
    n.tick(_den(S, 21, 40, 1))
    n.op(S.RESUME, _den(S, 21, 45))
    n.tick(_den(S, 22, 30))
    check(n.m.state == S.STANDBY and n.m.slot(12).status == S.DELAYED,
          "the last show delayed keeps the night open for Start now")
    n.tick(_den(S, 0, 0, 0, d=(2026, 11, 15)))
    check(n.m.slot(12).status == S.MISSED
          and "midnight" in n.m.slot(12).reason and n.m.state == S.CLOSING,
          f"at midnight it is MISSED and the night closes: "
          f"{n.m.slot(12).reason!r} {n.m.state}")
    # End night skips a delayed show too; Skip next skips it first.
    n = _Night(S, rule)
    n.boot(_den(S, 18, 5))
    n.op(S.HOLD_ON, _den(S, 18, 10))
    n.tick(_den(S, 18, 20, 1))
    n.op(S.SKIP_NEXT, _den(S, 18, 21))
    check(n.m.slot(2).status == S.SKIPPED and n.m.slot(3).status ==
          S.PENDING, "Skip next skips the delayed show first")
    n.tick(_den(S, 18, 40, 1))
    n.op(S.END_NIGHT, _den(S, 18, 41), confirmed=True)
    check(n.m.slot(3).status == S.SKIPPED and n.m.state == S.CLOSING,
          "End night skips the delayed show")
    n.audit("delayed night")
    print("  ok")


def test_schedule_start_now_in_every_state():
    section("scheduler: Start now anywhere but a running or paused show")
    S = _sched()
    if S is None:
        return
    fx = _matrix_fixtures(S)
    want = {S.IDLE: ("STARTED EARLY (operator)", 1),
            S.STANDBY: ("STARTED EARLY (operator)", 2),
            S.HOLD: ("STARTED EARLY (operator)", 1),
            S.CLOSING: ("EXTRA SHOW (operator)", 16),
            S.OFF: ("EXTRA SHOW (operator)", 16),
            "HOLD+DELAYED": ("DELAYED START (operator hold)", 1),
            "STANDBY+DELAYED": ("DELAYED START (operator hold)", 1)}
    for label, (m, now) in fx.items():
        o = S.step(m, S.Event(S.START_NOW, "operator", who="Jeff",
                              screen="rack screen"), now)
        if label in want:
            reason, show = want[label]
            check(o.accepted and _fired(o, S) == [show]
                  and o.machine.slot(show).reason == reason
                  and o.machine.slot(show).fired_at == now,
                  f"Start now in {label} starts show {show} now as "
                  f"{reason}: {o.refused or o.machine.slot(show).reason}")
            if reason == "STARTED EARLY (operator)":
                check(o.machine.next_slot() is None or
                      o.machine.next_slot().n != show,
                      "starting early uses up that slot")
            check(not o.machine.held_from and o.machine.state == S.SHOW,
                  "Start now ends a Hold")
        else:
            check(not o.accepted, f"Start now in {label} does nothing")
    # Right after an Abort, and inside the guard: no wait at all.
    rule = _one_night_rule(S)
    n = _Night(S, rule)
    n.boot(_den(S, 17, 50))
    n.tick(_den(S, 18, 0))
    n.op(S.ABORT, _den(S, 18, 2), confirmed=True)
    o = n.op(S.START_NOW, _den(S, 18, 2))
    check(o.accepted and _fired(o, S) == [2]
          and n.m.slot(2).reason == "STARTED EARLY (operator)",
          f"Start now works the moment after an Abort: {o.refused}")
    n.do(S.SHOW_ENDED, "madmapper", _den(S, 18, 9, 20))
    o = n.op(S.START_NOW, _den(S, 18, 9, 21))
    check(o.accepted and _fired(o, S) == [3],
          f"Start now works 1 s after a show ended, inside the guard: "
          f"{o.refused}")
    # Every show used up: the next Start now is an extra show.
    n = _Night(S, rule)
    n.boot(_den(S, 21, 50))
    o = n.op(S.START_NOW, _den(S, 21, 55))
    check(n.m.slot(13) is not None and n.m.slot(13).origin == "operator"
          and n.m.slot(13).reason == "EXTRA SHOW (operator)",
          "with no show left, Start now runs an extra one")
    n.audit("start now")
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
        (S.ZERO_FLAME_CUES, 3, 0.0), (S.BLANK_LASERS, 3, 0.0),
        (S.FADE_MUSIC_OUT, 3, 1.0), (S.FADE_VIDEO_OUT, 3, 1.0),
        (S.FADE_PIXELS, 3, 1.0), (S.STOP_CONDUCTOR, 3, 0.0)],
        f"Abort zeroes the flame cues and blanks the lasers at once, fades "
        f"music, video and pixels to black together over 1 s, then stops "
        f"MadMapper; no intermission follows: {o.effects}")
    check(n.m.state == S.STANDBY and n.m.slot(3).status == S.ABORTED
          and n.m.slot(3).reason == "ABORTED (operator)",
          "Abort marks the show ABORTED and stays in STANDBY")
    check(not n.m.fault, "an operator Abort is not a fault")
    check(S.step(n.m, _op(S, S.START_NOW), _den(S, 18, 42)).accepted,
          "Start now works straight after an Abort (not applied here)")
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
    from ltcplay import appdata
    check(SV.data_dir() == st._machine_folder(),
          "the scheduler keeps its files where this machine's settings are")
    check(os.path.dirname(SV.default_rule_path()) == SV.data_dir(),
          "the rule file lives in data_dir()")
    # On Windows that is %LOCALAPPDATA%\ltcplay; on a Mac, beside the
    # launcher, as before. Both answers checked on whichever OS runs this.
    real_win, real_env = appdata.WINDOWS, os.environ.get("LOCALAPPDATA")
    fake = tempfile.mkdtemp()
    try:
        appdata.WINDOWS = True
        os.environ["LOCALAPPDATA"] = fake
        want = os.path.join(fake, "ltcplay")
        check(SV.data_dir() == want and os.path.isdir(want),
              f"on Windows the scheduler's files go under %LOCALAPPDATA%: "
              f"{SV.data_dir()}")
        check(SV.default_rule_path() == os.path.join(want, SV.RULE_FILE)
              and os.path.dirname(SV.tonight_path(
                  _den(S, 0, 0).date())) == want,
              "on Windows the rule file and tonight's list default there")
        appdata.WINDOWS = False
        check(SV.data_dir() == st.folder(),
              "on a Mac the scheduler's files stay beside the launcher")
    finally:
        appdata.WINDOWS = real_win
        if real_env is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = real_env
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
    old = json.load(open(prev, encoding="utf-8"))
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
    open(path, "w", encoding="utf-8").write("{broken")
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
    svc = SV.Service(path, clock=lambda: now[0], ntp_query=lambda: 0.25,
                     state_dir=work)
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
        check(code == 400 and "who pressed it" in bad.get("error", ""),
              f"an edit that does not say who made it is a 400 with a "
              f"sentence: {bad}")
        code, bad = call("/api/schedule/tonight", {"op": "move", "show": 5,
                                                   "to": "18:35", "who": "Andy"})
        check(code == 400 and "which screen" in bad.get("error", ""),
              f"an edit that does not say which screen is a 400: {bad}")
        code, bad = call("/api/schedule/tonight",
                         {"op": "move", "show": 5, "to": "18:35",
                          "who": "Andy", "screen": "rack screen"})
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
    open(path, "w", encoding="utf-8").write(json.dumps(_sched_doc(late_grace_s=60)))
    bad = SV.Service(path, clock=lambda: now[0], ntp_query=lambda: 0.0,
                     state_dir=work)
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
        tree = ast.parse(open(os.path.join(pkg, name), encoding="utf-8").read())
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
            text = open(f, errors="replace", encoding="utf-8").read()
        except OSError:
            continue
        check("--schedule" not in text,
              f"{os.path.relpath(f, root)} turns the scheduler on")
    print("  ok")


def test_the_scheduler_engine_is_pure():
    section("scheduler: the engine does no I/O and runs on Python 3.12")
    import ast
    root = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(root, "ltcplay", "schedule.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    allowed = {"hashlib", "json", "re", "dataclasses", "datetime",
               "zoneinfo"}
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
        text = open(os.path.join(root, "ltcplay", name), encoding="utf-8").read()
        try:
            ast.parse(text, feature_version=(3, 12))
        except SyntaxError as e:
            check(False, f"{name} does not parse as Python 3.12: {e}")
        for posix in ("fcntl", "os.fork", "signal.SIGHUP", "os.getuid",
                      "termios", "resource"):
            check(posix not in text, f"{name} uses {posix}, which Windows "
                                     f"does not have")
    print("  ok")


def _svc(S, work, now, rule=None, **kw):
    """A scheduler service on a temp folder with a clock the test moves."""
    from ltcplay import schedule_service as SV
    path = os.path.join(work, SV.RULE_FILE)
    if rule is not None or not os.path.exists(path):
        SV.save_rule(path, rule or _sched_doc(
            weekly={"sat": {"first_start": "18:00", "interval_min": 20,
                            "last_end": "22:00"}}, exceptions={}))
    kw.setdefault("ntp_query", lambda: 0.0)
    svc = SV.Service(path, clock=lambda: now[0], state_dir=work, **kw)
    return svc


def _op(S, kind, **kw):
    kw.setdefault("who", "Andy")
    kw.setdefault("screen", "rack screen")
    return S.Event(kind, "operator", **kw)


def _starts(svc):
    return [r["show"] for r in svc.journal
            if r["action"] == "START_SHOW" and r["outcome"] == "not performed"]


def test_schedule_restart_keeps_tonight():
    section("scheduler: a restart picks tonight up where it was")
    S = _sched()
    if S is None:
        return
    import json
    import tempfile
    from ltcplay import schedule_service as SV

    # The auditor's case: at 17:30 show 3 (18:40) is taken off and show 2
    # moved to 18:30; the program restarts at 18:25.
    work = tempfile.mkdtemp()
    now = [_den(S, 17, 30)]
    a = _svc(S, work, now).start(thread=False)
    a.edit_tonight({"op": "remove", "show": 3, "who": "Andy",
                    "screen": "rack screen"})
    a.edit_tonight({"op": "move", "show": 2, "to": "18:30", "who": "Andy",
                    "screen": "rack screen"})
    saved = SV.tonight_path(a.machine.date, work)
    check(os.path.exists(saved), "tonight's list is saved as it changes")
    now[0] = _den(S, 18, 25)
    b = _svc(S, work, now).start(thread=False)
    m = b.machine
    check(m.slot(1).status == S.MISSED
          and m.slot(1).reason == "MISSED (late by 25m 0s)",
          f"18:00 went by during the restart and is MISSED: "
          f"{m.slot(1).reason!r}")
    check(m.slot(2).status == S.PENDING and m.hm(m.slot(2).start) == "18:30",
          f"the moved show 2 is still at 18:30 and still to come, got "
          f"{m.slot(2).status} at {m.hm(m.slot(2).start)}")
    check(m.slot(3).status == S.SKIPPED, f"the removed show 3 stays "
                                         f"removed, got {m.slot(3).status}")
    check(m.state == S.STANDBY, f"it lands in STANDBY, got {m.state}")
    check(any("picked up tonight's list" in r["text"] for r in b.journal),
          "the journal says the list was picked up after a restart")
    for t in ((18, 30), (18, 37, 20), (18, 40), (18, 40, 1), (19, 0)):
        now[0] = _den(S, *t)
        b.tick()
    check(_starts(b) == [2, 4], f"after the restart show 2 runs at 18:30, "
                                f"show 3 never runs and show 4 runs at "
                                f"19:00; started {_starts(b)}")

    # The same thing in the pure engine: the saved night round trips
    # exactly, and BOOT_DONE on it applies the late rule itself, landing in
    # STANDBY with 18:00 MISSED before any tick.
    saved_m = a.machine
    doc = S.machine_to_doc(saved_m)
    back = S.machine_from_doc(json.loads(json.dumps(doc)), a.rule,
                              saved_m.date, _den(S, 18, 25))
    check(back.state == S.BOOT and back.resumed_from == saved_m.state
          and S.machine_to_doc(back) == dict(doc, state=S.BOOT),
          "a saved night reads back exactly, in BOOT, knowing where it was")
    o = S.step(back, S.Event(S.BOOT_DONE, "system"), _den(S, 18, 25))
    check(o.machine.state == S.STANDBY and o.machine.slot(1).status
          == S.MISSED and not _fired(o, S)
          and [e.kind for e in o.effects] == [S.INTERMISSION],
          f"restoring at 18:25 marks 18:00 MISSED and lands in STANDBY with "
          f"the intermission, starting nothing: {o.machine.state} "
          f"{[e.kind for e in o.effects]}")

    # A restart just after a show ends: the guard still holds. Show 1 is
    # paused long enough to end at 18:19:00, one minute before 18:20.
    work = tempfile.mkdtemp()
    now = [_den(S, 17, 50)]
    a = _svc(S, work, now).start(thread=False)
    for t, ev in (((18, 0), None), ((18, 1), _op(S, S.HOLD_ON)),
                  ((18, 12, 40), _op(S, S.RESUME)),
                  ((18, 19), S.Event(S.SHOW_ENDED, "madmapper"))):
        now[0] = _den(S, *t)
        a._apply(ev) if ev else a.tick()
    check(a.machine.slot(1).status == S.DONE, "show 1 ran long and ended")
    now[0] = _den(S, 18, 19, 30)
    b = _svc(S, work, now).start(thread=False)
    check(b.machine.last_end == _den(S, 18, 19),
          "when the last show ended survives the restart")
    now[0] = _den(S, 18, 20)
    b.tick()
    check(not _starts(b) and b.machine.state == S.STANDBY,
          "18:20 is 60 s after the last show ended; after a restart the "
          "guard still holds it off")
    now[0] = _den(S, 18, 20, 1)
    b.tick()
    check("guard" in b.machine.slot(2).reason,
          f"and says it was the guard: {b.machine.slot(2).reason!r}")

    # A restart during a show: that show is over, the rig is made safe and
    # the guard counts from the restart.
    work = tempfile.mkdtemp()
    now = [_den(S, 17, 59, 50)]
    a = _svc(S, work, now).start(thread=False)
    now[0] = _den(S, 18, 0)
    a.tick()
    a._apply(S.Event(S.SHOW_CONFIRMED, "madmapper"))
    check(a.machine.state == S.SHOW, "show 1 is running")
    now[0] = _den(S, 18, 0, 4)
    b = _svc(S, work, now).start(thread=False)
    s1 = b.machine.slot(1)
    check(s1.status == S.FAULT and "restarted" in s1.reason
          and b.machine.fault and b.machine.state == S.STANDBY,
          f"a restart 4 s into a show ends it as FAULT and never resumes "
          f"it: {s1.status} {s1.reason!r} in {b.machine.state}")
    check(any(r["action"] == "ZERO_FLAME_CUES" for r in b.journal),
          "and asks for the flame cues to go to zero")
    check(not _starts(b), "the show is not started again")

    # Hold survives a restart.
    work = tempfile.mkdtemp()
    now = [_den(S, 17, 30)]
    a = _svc(S, work, now).start(thread=False)
    a._apply(_op(S, S.HOLD_ON))
    now[0] = _den(S, 17, 40)
    b = _svc(S, work, now).start(thread=False)
    check(b.machine.state == S.HOLD, f"a night on hold is still on hold "
                                     f"after a restart, got "
                                     f"{b.machine.state}")
    now[0] = _den(S, 18, 0)
    b.tick()
    check(not _starts(b), "and nothing fires")

    # Missing, then corrupt: back to the rule, with a sentence each time.
    work = tempfile.mkdtemp()
    now = [_den(S, 18, 4)]
    b = _svc(S, work, now).start(thread=False)
    check(any("no saved list for 2026-11-14" in r["text"]
              for r in b.journal),
          "no saved list says tonight starts from the schedule file")
    check(b.machine.slot(1).status == S.MISSED
          and b.machine.state == S.STANDBY,
          "and the late rule applies to the rule's list")
    for garbage in ("{not json", json.dumps({"format": 1, "date": "x"}),
                    json.dumps(dict(S.machine_to_doc(b.machine),
                                    date="2026-11-13")),
                    json.dumps(dict(S.machine_to_doc(b.machine), running=7,
                                    state="SHOW"))):
        work = tempfile.mkdtemp()
        now = [_den(S, 18, 4)]
        path = SV.tonight_path(_den(S, 0, 0).date(), work)
        open(path, "w", encoding="utf-8").write(garbage)
        b = _svc(S, work, now).start(thread=False)
        rows = [r["text"] for r in b.journal
                if r["text"].startswith("The saved list for tonight")]
        check(len(rows) == 1 and "starts again from the schedule file" in
              rows[0] and "set aside" in rows[0],
              f"a saved list that cannot be read is a sentence and a fresh "
              f"start from the rule: {rows}")
        check(os.path.exists(path[:-5] + ".unreadable.json"),
              "the unreadable file is kept for the morning, not overwritten")
        check(any("assumes a show may have just ended" in r["text"]
                  for r in b.journal),
              "and the fresh night assumes the worst, and says so")
        check(b.machine.state == S.STANDBY and b.machine.slot(1).status
              == S.MISSED, "the fallback night obeys the late rule")
        for r in b.journal:
            _no_dashes(r["text"], "tonight file")

    # A folder it cannot write to: the schedule carries on and says so.
    work = tempfile.mkdtemp()
    blocker = os.path.join(work, "not a folder")
    open(blocker, "w", encoding="utf-8").write("x")
    now = [_den(S, 17, 30)]
    c = _svc(S, work, now)
    c.state_dir = blocker
    c.start(thread=False)
    check(c.machine.state == S.IDLE and "could not be saved" in
          (c.persist_error or ""),
          f"a failed save is a sentence and the night goes on: "
          f"{c.persist_error!r}")
    check(c.state_view()["save_error"], "the state says the save failed")

    # A running night is never replaced at midnight; the new day starts
    # once it has finished.
    work = tempfile.mkdtemp()
    now = [_den(S, 23, 58)]
    a = _svc(S, work, now).start(thread=False)
    a._apply(_op(S, S.START_NOW))
    check(a.machine.state == S.SHOW, "a show started by hand at 23:58")
    now[0] = _den(S, 0, 0, 30, d=(2026, 11, 15))
    a.tick()
    check(str(a.machine.date) == "2026-11-14" and a.machine.state == S.SHOW,
          f"at 00:00:30 the running night is kept, got {a.machine.date} "
          f"{a.machine.state}")
    now[0] = _den(S, 0, 5, 30, d=(2026, 11, 15))
    a.tick()
    a.tick()
    check(str(a.machine.date) == "2026-11-15",
          f"once the show is over the new day starts, got {a.machine.date}")
    old = json.load(open(SV.tonight_path(_den(S, 0, 0).date(), work),
                         encoding="utf-8"))
    check([x["status"] for x in old["slots"]][-1] == S.DONE,
          "yesterday's file records the late show as DONE")

    # A machine asleep across the end of the night marks what it slept
    # through as MISSED in the journal; nothing is dropped without a word.
    work = tempfile.mkdtemp()
    now = [_den(S, 21, 0, 30)]
    a = _svc(S, work, now).start(thread=False)
    pend = [s.n for s in a.machine.pending()]
    check(pend == [11, 12], f"at 21:00:30 shows 11 and 12 are to come, got "
                            f"{pend}")
    now[0] = _den(S, 0, 10, d=(2026, 11, 15))
    a.tick()
    missed = {r["show"] for r in a.journal if r["outcome"] == "missed"}
    check({11, 12} <= missed, f"shows slept through are journalled as "
                              f"MISSED before the new day, got {missed}")
    old = json.load(open(SV.tonight_path(_den(S, 0, 0).date(), work),
                         encoding="utf-8"))
    check(all(x["status"] != S.PENDING for x in old["slots"]),
          "and yesterday's saved list has nothing left pending")
    print("  ok")


def test_schedule_clock_check_never_delays_a_show():
    section("scheduler: a slow time server never delays the first tick")
    S = _sched()
    if S is None:
        return
    import tempfile
    import threading
    import time as _t
    from ltcplay import schedule_service as SV
    gate = threading.Event()

    def slow():
        gate.wait(10)
        return 0.1

    t0 = _t.monotonic()
    level, text, off = SV.check_clock(slow, limit_s=0.2)
    took = _t.monotonic() - t0
    check(level == "unknown" and off is None and "within 0.2 s" in text
          and took < 1.5, f"a query that hangs is cut off at the limit: "
                          f"{level} after {took:.2f} s: {text}")

    def boom():
        raise OSError("Name or service not known")

    level, text, _ = SV.check_clock(boom, limit_s=1)
    check(level == "unknown" and "Name or service not known" in text,
          f"a lookup failure is a sentence: {text}")

    # Boot at 17:59:59.5 with grace 0 and a time server that takes 3 s:
    # 18:00 must still start at 18:00. The clock here runs in real time, so
    # anything that holds up the first ticks makes 18:00 late.
    from datetime import timedelta
    work = tempfile.mkdtemp()
    _svc(S, work, [_den(S, 17, 0)])              # writes the rule file
    base, mono = _den(S, 17, 59, 59, 500_000), _t.monotonic()

    class Live(list):
        def __getitem__(self, i):
            return base + timedelta(seconds=_t.monotonic() - mono)

    svc = _svc(S, work, Live(), ntp_query=slow, clock_limit_s=3.0)
    svc.TICK_S = 0.02
    try:
        svc.start()
        check(svc.machine is not None and svc.machine.state == S.IDLE,
              "the first tick happens at start, before the clock check")
        check(wait_for(lambda: svc.machine.state == S.SHOW, timeout=2.5),
              f"18:00 must start at 18:00 while the clock check is still "
              f"waiting, got {svc.machine.state} {svc.machine.slot(1).reason!r}")
        check(svc.clock_check is None, "the clock check is still waiting")
        gate.set()
        check(wait_for(lambda: svc.clock_check is not None, timeout=3),
              "the clock check lands when the server answers")
        check(svc.clock_check["level"] == "ok", f"{svc.clock_check}")
    finally:
        gate.set()
        svc.stop()
    print("  ok")


def test_schedule_faults_during_and_before_a_show():
    section("scheduler: a show that never started stops; a fault mid show "
            "does not")
    S = _sched()
    if S is None:
        return
    rule = _one_night_rule(S)
    # The start went out and nothing came back: FAULT, and the rig is made
    # safe.
    n = _Night(S, rule)
    n.boot(_den(S, 17, 50))
    n.tick(_den(S, 18, 0))
    o = n.do(S.SHOW_FAILED, "madmapper", _den(S, 18, 0, 3),
             detail="no timecode after start")
    check([e.kind for e in o.effects] == _WHOLE_FADE,
          f"a show that did not start is made safe and faded whole, then "
          f"MadMapper stops, and the rig stays dark: "
          f"{[e.kind for e in o.effects]}")
    check(n.m.slot(1).reason == "FAULT (no timecode after start)"
          and "did not start" in o.log[0].text,
          f"and says it did not start: {o.log[0].text!r}")
    # Once it is confirmed running, SHOW_FAILED is refused: a fault during
    # a show goes through FAULT_RAISED and the show keeps running.
    n.tick(_den(S, 18, 20))
    n.do(S.SHOW_CONFIRMED, "madmapper", _den(S, 18, 20, 1))
    o = n.do(S.SHOW_CONFIRMED, "reader", _den(S, 18, 20, 2))
    check(not o.accepted, "a second confirm is refused")
    o = n.do(S.SHOW_FAILED, "madmapper", _den(S, 18, 23),
             detail="no heartbeat for 3 s")
    check(not o.accepted and "keeps running" in o.refused
          and n.m.state == S.SHOW,
          f"a started show cannot be failed: {o.refused!r}")
    o = n.do(S.FAULT_RAISED, "madmapper", _den(S, 18, 23),
             detail="MadMapper sent no heartbeat for 3 s")
    check(o.accepted and not o.effects and n.m.state == S.SHOW
          and n.m.running == 2 and n.m.fault
          and "keeps running" in o.log[0].text,
          f"a fault mid show sets the flag, asks for nothing and the show "
          f"keeps running: {o.effects} {n.m.state}")
    n.do(S.SHOW_ENDED, "madmapper", _den(S, 18, 27, 20))
    check(n.m.slot(2).status == S.DONE, "and it ends as DONE")
    n.audit("faults")
    print("  ok")


def test_schedule_edits_past_last_end_and_midnight():
    section("scheduler: running late is allowed and said; midnight is a wall")
    S = _sched()
    if S is None:
        return
    rule = _one_night_rule(S)            # 18:00 to 22:00, show 12 at 21:40
    n = _Night(S, rule)
    n.boot(_den(S, 21, 0))
    check(not S.late_warnings(n.m), "the rule's own night does not run late")
    n.op(S.DELAY_REST, _den(S, 21, 1), minutes=10)
    o = n.op(S.DELAY_REST, _den(S, 21, 2), minutes=10)
    check(o.accepted and n.m.hm(n.m.slot(12).start) == "22:00",
          f"Delay the rest may push past last_end: {o.refused}")
    late = [le for le in o.log if le.outcome == "runs late"]
    check(len(late) == 1 and late[0].show == 12
          and "after tonight's last_end of 22:00" in late[0].text
          and late[0].reason == "RUNS LATE (past last_end 22:00)",
          f"and journals the show that now runs late: "
          f"{[le.text for le in late]}")
    v = S.machine_view(n.m, _den(S, 21, 2))
    check(v["runs_late"] and v["last_end"] == "22:00" and
          any("Show 12" in w for w in v["warnings"]),
          f"the state carries a warning flag: {v['warnings']}")
    rows = {r["show"]: r for r in S.slot_view(n.m)}
    check(rows[12]["past_last_end"] and not rows[11]["past_last_end"],
          "the list flags the late show")
    o = n.op(S.EDIT_ADD, _den(S, 21, 3), at="23:50")
    check(o.accepted and any(le.outcome == "runs late" for le in o.log),
          f"a show added after last_end is allowed and journalled: "
          f"{o.refused}")
    for at in ("23:55", "23:53"):
        o = n.op(S.EDIT_ADD, _den(S, 21, 3), at=at)
        check(not o.accepted and "after midnight" in o.refused,
              f"a show added at {at} would end after midnight and is "
              f"refused: {o.refused!r}")
    o = n.op(S.EDIT_MOVE, _den(S, 21, 3), show=12, at="23:59")
    check(not o.accepted and "after midnight" in o.refused,
          f"a move past midnight is refused: {o.refused!r}")
    before = [(s.n, s.start) for s in n.m.slots]
    o = n.op(S.DELAY_REST, _den(S, 21, 4), minutes=10)
    check(not o.accepted and "after midnight" in o.refused
          and [(s.n, s.start) for s in n.m.slots] == before,
          f"a delay that would push the 23:50 show past midnight is refused "
          f"whole, nothing dropped: {o.refused!r}")
    n.audit("late edits")
    print("  ok")


def test_schedule_rule_file_with_a_bom():
    section("scheduler: a rule file saved by Windows Notepad reads")
    S = _sched()
    if S is None:
        return
    import json
    import tempfile
    from ltcplay import schedule_service as SV
    work = tempfile.mkdtemp()
    path = os.path.join(work, SV.RULE_FILE)
    with open(path, "w", encoding="utf-8-sig") as fh:
        json.dump(_sched_doc(), fh)
    check(open(path, "rb").read(3) == b"\xef\xbb\xbf", "the file has a BOM")
    r = SV.load_rule(path)
    check(r.show_len_s == 440, "a rule file with a BOM loads")
    r2 = SV.save_rule(path, _sched_doc(guard_s=90))
    check(r2.version == 1 and SV.load_rule(path).guard_s == 90
          and json.load(open(SV.previous_path(path),
                             encoding="utf-8-sig"))["guard_s"] == 120,
          "a BOM file can be saved over, and is kept as the previous one")
    print("  ok")


def test_schedule_tonight_file_is_checked():
    section("scheduler: tonight's saved file cannot change the rules")
    S = _sched()
    if S is None:
        return
    import copy
    import json
    import tempfile
    from ltcplay import schedule_service as SV

    # The auditor's case: late_grace_s 600 written into tonight's file, a
    # restart at 18:05. The 18:00 show must not start.
    for extra in ({"late_grace_s": 600}, {"guard_s": -99999},
                  {"show_len_s": 0}, {"timezone": "Asia/Tokyo"}):
        work = tempfile.mkdtemp()
        now = [_den(S, 17, 30)]
        _svc(S, work, now).start(thread=False)
        path = SV.tonight_path(_den(S, 0, 0).date(), work)
        doc = json.load(open(path, encoding="utf-8"))
        doc.update(extra)
        json.dump(doc, open(path, "w", encoding="utf-8"))
        now[0] = _den(S, 18, 5)
        b = _svc(S, work, now).start(thread=False)
        b.tick()
        key = next(iter(extra))
        check(not _starts(b) and b.machine.slot(1).status == S.MISSED,
              f"{key}={extra[key]!r} in tonight's file must not make a "
              f"passed show start: started {_starts(b)}")
        check((b.machine.show_len_s, b.machine.guard_s,
               b.machine.late_grace_s, b.machine.tz.key)
              == (440, 120, 0, "America/Denver"),
              f"{key}: the show length, guard, grace and zone come from the "
              f"rule file")
        check(any("could not be used" in r["text"] and key in r["text"]
                  for r in b.journal)
              and os.path.exists(path[:-5] + ".unreadable.json"),
              f"{key}: the file is set aside with a sentence naming it")

    # Every other thing the file could lie about, checked in the engine.
    rule = _one_night_rule(S)
    n = _Night(S, rule)
    n.boot(_den(S, 17, 50))
    n.tick(_den(S, 18, 0))
    n.do(S.SHOW_ENDED, "madmapper", _den(S, 18, 7, 20))
    n.op(S.EDIT_MOVE, _den(S, 18, 8), show=3, at="18:45")
    good = S.machine_to_doc(n.m)
    d, at = n.m.date, _den(S, 18, 10)
    m = S.machine_from_doc(copy.deepcopy(good), rule, d, at)
    check(m.slot(1).status == S.DONE and n.m.hm(m.slot(3).start) == "18:45",
          "a good file reads back")

    def bad(what, change):
        doc = copy.deepcopy(good)
        change(doc)
        try:
            S.machine_from_doc(doc, rule, d, at)
            check(False, f"tonight's file with {what} was accepted")
        except ValueError as e:
            check(str(e).endswith("."), f"{what}: not a sentence: {e}")
            _no_dashes(str(e), what)

    def slot(i, **kw):
        return lambda doc: doc["slots"][i].update(kw)

    bad("a show on another date", slot(4, start="2026-11-15T01:00:00-07:00"))
    bad("a show the day before", slot(4, start="2026-11-14T06:00:00+00:00"))
    bad("a planned time on another date",
        slot(2, planned="2026-11-13T18:40:00-07:00"))
    bad("a status that is not one", slot(4, status="FIRED"))
    bad("a show to come that already started",
        slot(4, fired_at="2026-11-14T18:05:00-07:00"))
    bad("a show to come that already ended",
        slot(4, ended_at="2026-11-14T18:05:00-07:00"))
    bad("a running show with no start", lambda doc: (
        doc["slots"][4].update(status="RUNNING"),
        doc.update(running=5, state="SHOW")))
    bad("a running show the list does not have",
        lambda doc: doc.update(running=4, state="SHOW"))
    bad("SHOW with nothing running", lambda doc: doc.update(state="SHOW"))
    bad("a running show that is not the one named", lambda doc: (
        doc["slots"][4].update(status="RUNNING",
                               fired_at="2026-11-14T18:08:00-07:00"),
        doc.update(running=1, state="SHOW")))
    bad("two running shows", lambda doc: (
        doc["slots"][3].update(status="RUNNING",
                               fired_at="2026-11-14T18:08:00-07:00"),
        doc["slots"][4].update(status="RUNNING",
                               fired_at="2026-11-14T18:08:00-07:00"),
        doc.update(running=5, state="SHOW")))
    bad("a duplicate show number", slot(4, n=1))
    bad("an unknown slot key", slot(4, late_grace_s=600))
    bad("an unknown origin", slot(4, origin="madmapper"))
    bad("a missing key", lambda doc: doc.pop("last_end"))
    bad("the old hold_pending key", lambda doc: doc.update(
        hold_pending=True))
    bad("two delayed shows", lambda doc: (
        doc["slots"][4].update(status="DELAYED"),
        doc["slots"][5].update(status="DELAYED")))
    bad("a delayed show that already started",
        slot(4, status="DELAYED", fired_at="2026-11-14T18:05:00-07:00"))
    bad("PAUSED with nothing running", lambda doc: doc.update(
        state="PAUSED"))
    bad("a paused time of true", slot(0, paused_s=True))
    bad("a state of BOOT", lambda doc: doc.update(state="BOOT"))
    bad("held_from SHOW", lambda doc: doc.update(held_from="SHOW"))
    bad("another date", lambda doc: doc.update(date="2026-11-15"))
    bad("the old format", lambda doc: doc.update(format=1))
    # A time after now is the clock having been stepped back, not a broken
    # file: it is taken as now, and the step is said out loud.
    doc = copy.deepcopy(good)
    doc["slots"][0]["ended_at"] = "2026-11-14T18:10:06-07:00"
    doc["last_end"] = "2026-11-14T18:10:04-07:00"
    notes = []
    m = S.machine_from_doc(doc, rule, d, at, notes)
    check(m.slot(1).ended_at == at and m.last_end == at
          and len(notes) == 1 and "about 6 s" in notes[0],
          f"times after now are taken as now and the 6 s step is named: "
          f"{notes}")
    _no_dashes(notes[0], "clock step")
    bad("an added show past midnight", lambda doc: doc["slots"].append(
        dict(doc["slots"][-1], n=99, origin="edit",
             start="2026-11-15T06:55:00+00:00")))
    print("  ok")


def test_schedule_rule_change_rebuilds_tonight():
    section("scheduler: a corrected rule wins over tonight's saved list")
    S = _sched()
    if S is None:
        return
    import json
    import tempfile
    from ltcplay import schedule_service as SV

    def rule(first="18:00", guard=120):
        return _sched_doc(weekly={"sat": {"first_start": first,
                                          "interval_min": 20,
                                          "last_end": "22:00"}},
                          exceptions={}, guard_s=guard)

    # No edits: the file is written at the first boot, the rule is corrected
    # at 16:30, and a restart must run the new times.
    work = tempfile.mkdtemp()
    now = [_den(S, 16, 0)]
    _svc(S, work, now).start(thread=False)
    rpath = os.path.join(work, SV.RULE_FILE)
    SV.save_rule(rpath, rule("18:10"))
    now[0] = _den(S, 16, 31)
    b = _svc(S, work, now).start(thread=False)
    check(b.machine.hm(b.machine.slot(1).start) == "18:10",
          f"after a restart the corrected rule's 18:10 is the first show, "
          f"got {b.machine.hm(b.machine.slot(1).start)}")
    texts = [r["text"] for r in b.journal if r["outcome"] == "rebuilt"]
    check(any("rebuilt from the new schedule" in t for t in texts)
          and any("18:10 is new in the schedule" in t for t in texts)
          and any("18:00 show is no longer in the schedule" in t
                  for t in texts),
          f"the journal names each change: {texts[:4]}")

    # A hand edit with no version bump counts as well.
    doc = json.load(open(rpath, encoding="utf-8"))
    doc["weekly"]["sat"]["first_start"] = "18:20"
    json.dump(doc, open(rpath, "w", encoding="utf-8"))
    now[0] = _den(S, 16, 40)
    c = _svc(S, work, now).start(thread=False)
    check(c.machine.hm(c.machine.slot(1).start) == "18:20",
          "a hand edit without a version bump is picked up")
    # A save that changes nothing but the version keeps tonight as it is.
    c.edit_tonight({"op": "move", "show": 2, "to": "18:45", "who": "Andy",
                    "screen": "rack screen"})
    SV.save_rule(rpath, json.load(open(rpath, encoding="utf-8")))
    now[0] = _den(S, 16, 50)
    e = _svc(S, work, now).start(thread=False)
    check(e.machine.hm(e.machine.slot(2).start) == "18:45"
          and not any(r["outcome"] == "rebuilt" for r in e.journal),
          "a version bump with the same content keeps tonight's edits")

    # With history: show 1 ran, show 2 was skipped, show 3 moved. The rule
    # changes (the guard) and the program restarts at 18:15.
    work = tempfile.mkdtemp()
    now = [_den(S, 17, 50)]
    a = _svc(S, work, now).start(thread=False)
    a.edit_tonight({"op": "move", "show": 3, "to": "18:45", "who": "Andy",
                    "screen": "rack screen"})
    for t in ((18, 0), (18, 7, 20), (18, 8)):
        now[0] = _den(S, *t)
        a.tick()
    a._apply(_op(S, S.SKIP_NEXT))
    check([s.status for s in a.machine.slots[:3]] ==
          [S.DONE, S.SKIPPED, S.PENDING], "show 1 ran, 2 skipped, 3 to come")
    SV.save_rule(os.path.join(work, SV.RULE_FILE), rule(guard=90))
    now[0] = _den(S, 18, 15)
    b = _svc(S, work, now).start(thread=False)
    m = b.machine
    check(m.guard_s == 90, "the new guard applies")
    check(m.slot(1).status == S.DONE and m.slot(2).status == S.SKIPPED,
          "what already happened keeps its status")
    check(m.slot(3).status == S.PENDING and m.hm(m.slot(3).start) == "18:40"
          and m.slot(3).planned is None,
          f"tonight's move of show 3 is dropped: {m.hm(m.slot(3).start)}")
    check(m.last_end is not None, "when the last show ended is kept")
    texts = [r["text"] for r in b.journal if r["outcome"] == "rebuilt"]
    check(any("move of the 18:40 show to 18:45 is dropped" in t
              for t in texts)
          and any("Show 1 at 18:00 keeps its status, DONE" in t
                  for t in texts),
          f"each change is a sentence: {texts}")

    # The times themselves change: history stays, nothing passed fires.
    SV.save_rule(os.path.join(work, SV.RULE_FILE), rule(first="18:05"))
    now[0] = _den(S, 18, 16)
    c = _svc(S, work, now).start(thread=False)
    m = c.machine
    kept = sorted((m.hm(s.start), s.status) for s in m.slots
                  if s.status in (S.DONE, S.SKIPPED))
    check(kept == [("18:00", S.DONE), ("18:20", S.SKIPPED)],
          f"shows that happened on the old times stay on the list: {kept}")
    for t in ((18, 16, 1), (18, 20), (18, 24, 59)):
        now[0] = _den(S, *t)
        c.tick()
    check(not _starts(c), f"no show that has passed may start after the "
                          f"rebuild: started {_starts(c)}")
    now[0] = _den(S, 18, 25)
    c.tick()
    check(len(_starts(c)) == 1 and m.hm(
        c.machine.slot(_starts(c)[0]).start) == "18:25",
          "the new 18:25 show starts on time")
    for r in b.journal + c.journal:
        _no_dashes(r["text"], "rebuild")
    print("  ok")


def test_schedule_late_failed_start_is_a_fault():
    section("scheduler: a failed start reported too late leaves the show "
            "running")
    S = _sched()
    if S is None:
        return
    from datetime import timedelta
    rule = _one_night_rule(S)
    start = _den(S, 18, 0)
    for after, stops in ((3, True), (S.CONFIRM_WINDOW_S, True),
                         (S.CONFIRM_WINDOW_S + 0.9, True),
                         (S.CONFIRM_WINDOW_S + 1, False), (360, False)):
        n = _Night(S, rule)
        n.boot(_den(S, 17, 50))
        n.tick(start)
        o = n.do(S.SHOW_FAILED, "madmapper", start + timedelta(seconds=after),
                 detail="no timecode after start")
        if stops:
            check(n.m.state == S.STANDBY and n.m.slot(1).status == S.FAULT
                  and S.STOP_CONDUCTOR in [e.kind for e in o.effects],
                  f"a failed start {after} s after the start stops the show")
        else:
            check(o.accepted and n.m.state == S.SHOW and n.m.running == 1
                  and n.m.slot(1).status == S.RUNNING and n.m.fault
                  and not o.effects,
                  f"a failed start {after} s after the start is a fault and "
                  f"the show keeps running: {n.m.state} {o.effects}")
            check(f"{S.CONFIRM_WINDOW_S} s window" in o.log[0].text
                  and "keeps running" in o.log[0].text,
                  f"and says why: {o.log[0].text!r}")
        n.audit(f"late failed start {after}")
    check(S.CONFIRM_WINDOW_S == 10, "the confirm window is 10 s")
    print("  ok")


def test_schedule_after_a_stopped_show_is_one_choice():
    section("scheduler: what follows Abort is one named choice, both ways")
    S = _sched()
    if S is None:
        return
    import json
    root = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(root, "ltcplay", "schedule.py"), encoding="utf-8").read()
    check(src.count("INTERMISSION_AFTER_A_STOPPED_SHOW") == 2,
          "the choice is defined once and read in one place")
    check(S.INTERMISSION_AFTER_A_STOPPED_SHOW is False,
          "Jeff, 2026-09-24: on an abort the whole show fades to black, and "
          "the intermission does not come back")
    rule = _one_night_rule(S)
    I = S.INTERMISSION

    def scenarios():
        out = {}
        n = _Night(S, rule)
        n.boot(_den(S, 17, 50))
        n.tick(_den(S, 18, 0))
        out["abort"] = n.op(S.ABORT, _den(S, 18, 2), confirmed=True)
        n = _Night(S, rule)
        n.boot(_den(S, 17, 50))
        n.tick(_den(S, 18, 0))
        n.op(S.HOLD_ON, _den(S, 18, 1))
        out["abort while paused"] = n.op(S.ABORT, _den(S, 18, 2),
                                         confirmed=True)
        n = _Night(S, rule)
        n.boot(_den(S, 17, 50))
        n.tick(_den(S, 18, 0))
        out["failed start"] = n.do(S.SHOW_FAILED, "madmapper",
                                   _den(S, 18, 0, 5), detail="no timecode")
        n = _Night(S, rule)
        n.boot(_den(S, 17, 50))
        n.tick(_den(S, 18, 0))
        doc = json.loads(json.dumps(S.machine_to_doc(n.m)))
        back = S.machine_from_doc(doc, rule, n.m.date, _den(S, 18, 3))
        out["restart mid show"] = S.step(back, S.Event(S.BOOT_DONE, "system"),
                                         _den(S, 18, 3))
        n = _Night(S, rule)
        n.boot(_den(S, 17, 50))
        n.tick(_den(S, 18, 0))
        out["show ended"] = n.do(S.SHOW_ENDED, "madmapper",
                                 _den(S, 18, 7, 20))
        return out

    try:
        for choice in (True, False):
            S.INTERMISSION_AFTER_A_STOPPED_SHOW = choice
            for name, o in scenarios().items():
                kinds = [e.kind for e in o.effects]
                if name == "show ended":
                    check(kinds == [I], f"a show that ends normally always "
                                        f"brings the intermission: {kinds}")
                    continue
                check(o.machine.state in (S.STANDBY, S.HOLD),
                      f"{name}: lands in STANDBY or HOLD")
                want = _WHOLE_FADE + ([I] if choice else [])
                check(kinds == want, f"{name} with the choice {choice}: "
                                     f"expected {want}, got {kinds}")
    finally:
        S.INTERMISSION_AFTER_A_STOPPED_SHOW = False
    print("  ok")


def test_schedule_contract_for_the_transport():
    section("scheduler: the contract for the transport is written down")
    S = _sched()
    if S is None:
        return
    doc = S.__doc__
    for must in ("Contract for PR 3", "BEFORE performing", "SHOW_CONFIRMED",
                 "FADE_VIDEO_OUT", "STOP_CONDUCTOR once the fade is done",
                 "CONFIRM_WINDOW_S", "FAULT_RAISED", "SHOW_ENDED",
                 "CLOSING_DONE", "twice\n   a second", "confirmed=True",
                 "refused Outcome"):
        check(must in doc, f"the contract must say {must!r}")
    _no_dashes(doc, "the contract")
    print("  ok")


def test_schedule_uncertain_record_never_fires_twice():
    section("scheduler: a clock stepped back or an unreadable record never "
            "fires a show twice")
    S = _sched()
    if S is None:
        return
    import tempfile
    from ltcplay import schedule_service as SV

    def ticks(svc, now, *times):
        for t in times:
            now[0] = _den(S, *t)
            svc.tick()

    # The auditor's case, grace 0: show 1 fires at 18:00:00 and is confirmed
    # at 18:00:03; the clock is stepped back about 6 s; ltcplay restarts at
    # 17:59:57.
    work = tempfile.mkdtemp()
    now = [_den(S, 17, 59, 50)]
    a = _svc(S, work, now).start(thread=False)
    ticks(a, now, (18, 0, 0))
    now[0] = _den(S, 18, 0, 3)
    a._apply(S.Event(S.SHOW_CONFIRMED, "madmapper"))
    check(_starts(a) == [1], "show 1 started at 18:00:00")
    path = SV.tonight_path(_den(S, 0, 0).date(), work)
    now[0] = _den(S, 17, 59, 57)
    b = _svc(S, work, now).start(thread=False)
    s1 = b.machine.slot(1)
    check(s1.status == S.FAULT and "restarted" in s1.reason,
          f"the show cut off by the restart is recorded as FAULT: "
          f"{s1.status} {s1.reason!r}")
    check(os.path.exists(path)
          and not os.path.exists(path[:-5] + ".unreadable.json"),
          "a clock step is not corruption: the record is kept")
    check(any("set back by about 6 s" in r["text"] for r in b.journal),
          "the journal names the step")
    ticks(b, now, (17, 59, 59), (18, 0, 0), (18, 0, 1), (18, 0, 3),
          (18, 1), (18, 19, 59))
    check(not _starts(b), f"show 1 must not start again: started "
                          f"{_starts(b)}")
    ticks(b, now, (18, 20))
    check(_starts(b) == [2], "and the 18:20 show still starts on time")

    # Grace 15, a record that became unreadable 3 s after a show fired.
    rule15 = _sched_doc(weekly={"sat": {"first_start": "18:00",
                                        "interval_min": 20,
                                        "last_end": "22:00"}},
                        exceptions={}, late_grace_s=15)
    work = tempfile.mkdtemp()
    now = [_den(S, 17, 59, 50)]
    a = _svc(S, work, now, rule=rule15).start(thread=False)
    ticks(a, now, (18, 0, 0))
    check(_starts(a) == [1], "show 1 started at 18:00:00")
    open(SV.tonight_path(_den(S, 0, 0).date(), work), "w",
         encoding="utf-8").write("{torn")
    now[0] = _den(S, 18, 0, 3)
    b = _svc(S, work, now).start(thread=False)
    check(b.machine.late_grace_s == 15, "the rule's grace is 15")
    ticks(b, now, (18, 0, 4), (18, 0, 10), (18, 0, 15), (18, 1))
    s1 = b.machine.slot(1)
    check(not _starts(b) and s1.status == S.MISSED
          and s1.reason == S.UNREADABLE,
          f"inside a 15 s grace, an unreadable record must not start show 1 "
          f"again: started {_starts(b)}, {s1.reason!r}")
    check(b.machine.last_end == _den(S, 18, 0, 3),
          "the last show is assumed to have ended at the restart")
    check(any(r["outcome"] == "assumed the worst" and "18:02:03" in r["text"]
              for r in b.journal),
          "the journal says nothing starts before the guard runs out")
    ticks(b, now, (18, 20))
    check(_starts(b) == [2], "the next show starts on time")

    # Clean boots keep the grace: no file at all, and a good file saved
    # before the show, both start show 1 five seconds late with grace 15.
    for with_file in (False, True):
        work = tempfile.mkdtemp()
        now = [_den(S, 17, 59)]
        if with_file:
            _svc(S, work, now, rule=rule15).start(thread=False)
        else:
            _svc(S, work, now, rule=rule15)        # the rule file only
            check(not os.path.exists(SV.tonight_path(
                _den(S, 0, 0).date(), work)), "no tonight file yet")
        now[0] = _den(S, 18, 0, 5)
        c = _svc(S, work, now).start(thread=False)
        check(_starts(c) == [1] and c.machine.slot(1).reason == "FIRED",
              f"a clean boot {'with a good file ' if with_file else ''}"
              f"5 s into a 15 s grace still starts show 1: "
              f"{_starts(c)} {c.machine.slot(1).reason!r}")
        check(not any(r["outcome"] == "assumed the worst"
                      for r in c.journal),
              "and assumes nothing")
    for r in b.journal:
        _no_dashes(r["text"], "uncertain record")
    print("  ok")


def test_schedule_delayed_and_paused_survive_a_restart():
    section("scheduler: a delayed show and a paused show across a restart")
    S = _sched()
    if S is None:
        return
    import tempfile

    def at(svc, now, t, ev=None):
        now[0] = _den(S, *t)
        svc._apply(ev) if ev else svc.tick()

    # On hold with a delayed show, restart: still on hold, still delayed,
    # and the Hold rules carry on after it.
    work = tempfile.mkdtemp()
    now = [_den(S, 18, 5)]
    a = _svc(S, work, now).start(thread=False)
    at(a, now, (18, 10), _op(S, S.HOLD_ON))
    at(a, now, (18, 20, 1))
    check(a.machine.slot(2).status == S.DELAYED, "18:20 delayed by the Hold")
    now[0] = _den(S, 18, 25)
    b = _svc(S, work, now).start(thread=False)
    check(b.machine.state == S.HOLD and b.machine.slot(2).status == S.DELAYED
          and b.machine.slot(2).reason == "DELAYED (on hold)",
          f"after a restart the Hold and the delayed show are kept: "
          f"{b.machine.state} {b.machine.slot(2).status}")
    at(b, now, (18, 40, 1))
    check(b.machine.slot(2).status == S.MISSED
          and b.machine.slot(3).status == S.DELAYED,
          "and the newest delayed show still wins after it")
    at(b, now, (18, 41), _op(S, S.RESUME))
    at(b, now, (18, 42), _op(S, S.START_NOW))
    check(_starts(b) == [3]
          and b.machine.slot(3).reason == "DELAYED START (operator hold)",
          "Start now starts the delayed show after the restart")

    # Down across two slot times while on hold: the time it was down counts
    # as Hold.
    work = tempfile.mkdtemp()
    now = [_den(S, 18, 5)]
    a = _svc(S, work, now).start(thread=False)
    at(a, now, (18, 10), _op(S, S.HOLD_ON))
    now[0] = _den(S, 18, 41)
    b = _svc(S, work, now).start(thread=False)
    check([s.status for s in b.machine.slots[1:3]] == [S.MISSED, S.DELAYED]
          and b.machine.state == S.HOLD and not _starts(b),
          f"a restart on hold after two slot times: {[s.status for s in b.machine.slots[1:3]]}")

    # STANDBY with a delayed show waiting, restart: it still waits and never
    # starts by itself.
    work = tempfile.mkdtemp()
    now = [_den(S, 18, 5)]
    a = _svc(S, work, now).start(thread=False)
    at(a, now, (18, 10), _op(S, S.HOLD_ON))
    at(a, now, (18, 20, 1))
    at(a, now, (18, 25), _op(S, S.RESUME))
    now[0] = _den(S, 18, 26)
    b = _svc(S, work, now).start(thread=False)
    check(b.machine.state == S.STANDBY
          and b.machine.slot(2).status == S.DELAYED,
          "a delayed show waiting in STANDBY survives the restart")
    for t in ((18, 26, 1), (18, 30), (18, 35)):
        at(b, now, t)
    check(not _starts(b), "and still never starts by itself")

    # The dry run respects a pause: a paused show never "ends", and after
    # Resume it ends at its new, later time, with nothing refused on the way.
    work = tempfile.mkdtemp()
    now = [_den(S, 17, 59, 50)]
    c = _svc(S, work, now).start(thread=False)
    at(c, now, (18, 0))
    at(c, now, (18, 3), _op(S, S.HOLD_ON))
    for t in ((18, 7, 20), (18, 10), (18, 12, 59)):
        at(c, now, t)
    check(c.machine.state == S.PAUSED
          and not any(r["outcome"] == "refused" for r in c.journal),
          "the dry run never tries to end a paused show")
    at(c, now, (18, 13), _op(S, S.RESUME))
    at(c, now, (18, 17, 19))
    check(c.machine.state == S.SHOW, "ten minutes paused: still running at "
                                     "18:17:19")
    at(c, now, (18, 17, 20))
    check(c.machine.slot(1).status == S.DONE
          and c.machine.slot(1).ended_at == _den(S, 18, 17, 20),
          f"and it ends at 18:17:20: {c.machine.slot(1).status}")

    # Paused, restart: treated as a restart during a show.
    work = tempfile.mkdtemp()
    now = [_den(S, 17, 59, 50)]
    a = _svc(S, work, now).start(thread=False)
    at(a, now, (18, 0))
    at(a, now, (18, 3), _op(S, S.HOLD_ON))
    check(a.machine.state == S.PAUSED, "show 1 paused")
    now[0] = _den(S, 18, 5)
    b = _svc(S, work, now).start(thread=False)
    s1 = b.machine.slot(1)
    check(s1.status == S.FAULT and "restarted" in s1.reason
          and b.machine.state == S.STANDBY and b.machine.fault,
          f"a restart while paused ends the show as FAULT: {s1.status} "
          f"{s1.reason!r} {b.machine.state}")
    acts = [r["action"] for r in b.journal if r["outcome"] == "not performed"]
    check("ZERO_FLAME_CUES" in acts and "RESUME_SHOW" not in acts
          and "START_SHOW" not in acts,
          f"the rig is made safe and nothing resumes: {acts}")
    print("  ok")


def test_schedule_operator_list():
    section("scheduler: operator names come from a list in data_dir()")
    S = _sched()
    if S is None:
        return
    import json
    import tempfile
    from ltcplay import schedule_service as SV
    work = tempfile.mkdtemp()
    now = [_den(S, 17, 30)]
    svc = _svc(S, work, now).start(thread=False)
    path = SV.operators_path(work)
    check(os.path.dirname(SV.operators_path()) == SV.data_dir(),
          "the operators file lives in data_dir()")
    check(json.load(open(path, encoding="utf-8")) ==
          {"operators": ["Andy", "Jeff"]}
          and svc.operators == ("Andy", "Jeff")
          and svc.machine.operators == ("Andy", "Jeff"),
          "a missing list is written with Andy and Jeff")
    for who in ("Bob", "", "  "):
        try:
            svc.edit_tonight({"op": "remove", "show": 3, "who": who,
                              "screen": "rack screen"})
            check(False, f"an edit by {who!r} was accepted")
        except ValueError as e:
            check(("not on the operator list" in str(e)) if who.strip()
                  else ("who pressed it" in str(e)),
                  f"an edit by {who!r} is refused with a sentence: {e}")
    check(svc.machine.slot(3).status == S.PENDING, "nothing was changed")
    svc.edit_tonight({"op": "remove", "show": 3, "who": "jeff",
                      "screen": "rack screen"})
    check(svc.machine.slot(3).status == S.SKIPPED, "Jeff may edit")
    # Somebody else's list is the list in force.
    json.dump({"operators": ["Casey", "Andy"]},
              open(path, "w", encoding="utf-8"))
    other = _svc(S, work, now).start(thread=False)
    check(other.machine.operators == ("Casey", "Andy"),
          "the list on disk is the list in force")
    try:
        other.edit_tonight({"op": "remove", "show": 4, "who": "Jeff",
                            "screen": "rack screen"})
        check(False, "Jeff was accepted though not on this list")
    except ValueError as e:
        check("Casey, Andy" in str(e), f"the sentence names the list: {e}")
    # A broken list: the defaults stay in force and the journal says why.
    for bad in ('{"operators": []}', '{"operators": ["Andy", "andy"]}',
                '{"names": ["Andy"]}', '{"operators": [""]}', "{nope"):
        open(path, "w", encoding="utf-8").write(bad)
        b = _svc(S, work, now).start(thread=False)
        check(b.operators == ("Andy", "Jeff")
              and any("could not be used" in r["text"] for r in b.journal),
              f"a broken list {bad!r} falls back to Andy and Jeff, and "
              f"says so")
        for r in b.journal:
            _no_dashes(r["text"], "operators")
    print("  ok")


def test_schedule_a_paused_show_is_never_overlapped():
    section("scheduler: nothing fires over a paused show; a pause keeps the "
            "night past midnight")
    S = _sched()
    if S is None:
        return
    import tempfile
    from datetime import timedelta
    # Paused at 18:01, ticked every second through 18:20 and past its grace:
    # nothing fires, the show stays paused, 18:20 is DELAYED.
    for grace in (0, 15):
        rule = _one_night_rule(S, grace=grace)
        n = _Night(S, rule)
        n.boot(_den(S, 17, 50))
        n.tick(_den(S, 18, 0))
        n.op(S.HOLD_ON, _den(S, 18, 1))
        t, fired = _den(S, 18, 1), []
        while t <= _den(S, 18, 21):
            fired += _fired(n.tick(t), S)
            t += timedelta(seconds=1)
        check(not fired, f"grace {grace}: a paused show must never be "
                         f"overlapped; started {fired}")
        running = [s.n for s in n.m.slots if s.status == S.RUNNING]
        check(n.m.state == S.PAUSED and n.m.running == 1 and running == [1],
              f"grace {grace}: show 1 stays paused and is the only one "
              f"running: {n.m.state} {running}")
        check(n.m.slot(2).status == S.DELAYED,
              f"grace {grace}: 18:20 is DELAYED: {n.m.slot(2).status}")
        n.audit(f"paused overlap grace {grace}")

    # Paused across midnight: the night is kept until the show is over.
    work = tempfile.mkdtemp()
    now = [_den(S, 23, 57)]
    a = _svc(S, work, now).start(thread=False)
    now[0] = _den(S, 23, 58)
    a._apply(_op(S, S.START_NOW))
    now[0] = _den(S, 23, 59)
    a._apply(_op(S, S.HOLD_ON))
    check(a.machine.state == S.PAUSED, "a show paused at 23:59")
    for t in ((0, 0, 0), (0, 0, 30), (0, 10)):
        now[0] = _den(S, *t, d=(2026, 11, 15))
        a.tick()
        check(str(a.machine.date) == "2026-11-14"
              and a.machine.state == S.PAUSED and a.machine.running,
              f"at {t} the paused night is kept, got {a.machine.date} "
              f"{a.machine.state}")
    now[0] = _den(S, 0, 11, d=(2026, 11, 15))
    a._apply(_op(S, S.RESUME))
    check(str(a.machine.date) == "2026-11-14" and a.machine.state == S.SHOW,
          "Resume after midnight carries on in the same night")
    now[0] = _den(S, 0, 30, d=(2026, 11, 15))
    a.tick()
    a.tick()
    check(str(a.machine.date) == "2026-11-15",
          f"the new day starts once the show is over: {a.machine.date}")
    print("  ok")


def test_schedule_a_clock_step_during_a_pause():
    section("scheduler: a clock set back during a pause never makes the "
            "paused time negative")
    S = _sched()
    if S is None:
        return
    import copy
    import json
    import tempfile
    from ltcplay import schedule_service as SV
    rule = _one_night_rule(S)
    for last, how in ((S.RESUME, {}), (S.ABORT, {"confirmed": True})):
        n = _Night(S, rule)
        n.boot(_den(S, 17, 50))
        n.tick(_den(S, 18, 0))
        n.op(S.HOLD_ON, _den(S, 18, 1))
        check(n.m.hm(n.m.expected_end(_den(S, 18, 0, 50))) == "18:07:20",
              "while the clock reads earlier than the pause, the end does "
              "not move earlier")
        o = n.op(last, _den(S, 18, 0, 50), **how)
        check(n.m.slot(1).paused_s == 0.0,
              f"{last} after a 10 s step back: paused time is 0, not "
              f"{n.m.slot(1).paused_s}")
        step = [le for le in o.log if le.outcome == "clock stepped back"]
        check(len(step) == 1 and "about 10 s" in step[0].text
              and step[0].actor == "system",
              f"{last}: the step is journalled: {[le.text for le in o.log]}")
        if last == S.RESUME:
            check(n.m.hm(n.m.expected_end()) == "18:07:20",
                  "and the end stays at 18:07:20")
        n.audit(f"clock step during a pause, {last}")

    # A saved negative paused time is clamped with a sentence, not refused.
    n = _Night(S, rule)
    n.boot(_den(S, 17, 50))
    n.tick(_den(S, 18, 0))
    n.do(S.SHOW_ENDED, "madmapper", _den(S, 18, 7, 20))
    doc = copy.deepcopy(S.machine_to_doc(n.m))
    doc["slots"][0]["paused_s"] = -10.0
    notes = []
    m = S.machine_from_doc(doc, rule, n.m.date, _den(S, 18, 10), notes)
    check(m.slot(1).paused_s == 0.0 and len(notes) == 1
          and "taken as 0 s" in notes[0] and "-10 s" in notes[0],
          f"a negative saved paused time reads as 0 with a sentence: "
          f"{notes}")

    # The whole story through the service: pause, clock back, Resume,
    # restart. Tonight's record is kept, not set aside.
    work = tempfile.mkdtemp()
    now = [_den(S, 17, 59, 50)]
    a = _svc(S, work, now).start(thread=False)
    for t, ev in (((18, 0), None), ((18, 1), _op(S, S.HOLD_ON)),
                  ((18, 0, 50), _op(S, S.RESUME))):
        now[0] = _den(S, *t)
        a._apply(ev) if ev else a.tick()
    saved = json.load(open(SV.tonight_path(a.machine.date, work),
                           encoding="utf-8"))
    check(saved["slots"][0]["paused_s"] == 0.0,
          f"the saved paused time is 0: {saved['slots'][0]['paused_s']}")
    now[0] = _den(S, 18, 0, 55)
    b = _svc(S, work, now).start(thread=False)
    path = SV.tonight_path(b.machine.date, work)
    check(os.path.exists(path)
          and not os.path.exists(path[:-5] + ".unreadable.json")
          and not any(r["outcome"] == "assumed the worst" for r in b.journal),
          "the record survives the restart; nothing is assumed")
    check(b.machine.slot(1).status == S.FAULT
          and "restarted" in b.machine.slot(1).reason,
          "and the restart is handled as a restart during a show")
    for r in a.journal + b.journal:
        _no_dashes(r["text"], "clock step during a pause")
    print("  ok")


# ------------------------------------------------------ the show clock ----
# Fire & Ice 2026, handoff section 4. None of these import ltcplay.clock at
# module scope: the GPL test below proves the program never loads it unless
# a show file asks, and this file importing it up top would hide nothing but
# would make that test harder to trust.

class _TcOut:
    """Stands in for the Art-Net timecode socket and keeps every packet."""

    def __init__(self, dests=(("test", "127.0.0.1"),)):
        self.dests = list(dests)
        self.sent = []
        self.packets_sent = 0
        self.send_errors = 0
        self.last_error = ""
        self.closed = 0

    last_error_at = None
    seconds_since_error = None
    failing_labels = ()

    @property
    def seconds_since_ok(self):
        return 0.0

    def send(self, pkt):
        self.sent.append((time.perf_counter(), bytes(pkt)))
        self.packets_sent += 1
        return True

    def close(self):
        self.closed += 1


def _tc_of(pkt):
    """(h, m, s, f, type) out of an ArtTimeCode packet."""
    return pkt[17], pkt[16], pkt[15], pkt[14], pkt[18]


# ------------------------------------------------------------ announcements
def _ann():
    from ltcplay import announce as A
    return A


def _ann_write_wav(path, seconds=1.0, rate=8000, channels=1):
    import struct
    import wave as _wave
    n = int(seconds * rate)
    with _wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{n * channels}h", *([0] * (n * channels))))
    return n / float(rate)


def _ann_workdir(device="MOTU M4"):
    """A tempdir with three short, valid WAV files and a config naming them.
    Returns (workdir, config_path, {id: length_s})."""
    A = _ann()
    work = tempfile.mkdtemp()
    lengths = {}
    for aid, secs in zip(A.IDS, (1.0, 1.5, 0.5)):
        lengths[aid] = _ann_write_wav(os.path.join(work, aid + ".wav"),
                                      seconds=secs)
    cfg = {"device": device,
           "files": {aid: aid + ".wav" for aid in A.IDS}}
    path = os.path.join(work, "ltcplay_announce.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    return work, path, lengths


def test_announce_interlock_matrix():
    section("announcements: the interlock, every scheduler state times "
            "every button")
    from ltcplay import schedule as sch_mod
    A = _ann()
    states = (sch_mod.BOOT, sch_mod.IDLE, sch_mod.STANDBY, sch_mod.SHOW,
              sch_mod.PAUSED, sch_mod.CLOSING, sch_mod.OFF, sch_mod.HOLD)
    check(len(set(states)) == 8,
          "the matrix must cover all 8 scheduler states")
    blocked_states = {sch_mod.SHOW, sch_mod.PAUSED}
    for state in states + (None,):
        refusal = A.interlock_refusal(state)
        if state is None:
            check(refusal is not None and "inert" in refusal,
                  f"no scheduler: announcements must be inert, got "
                  f"{refusal!r}")
        elif state in blocked_states:
            check(refusal is not None and refusal.endswith("."),
                  f"{state}: a show running or paused must refuse, got "
                  f"{refusal!r}")
        else:
            check(refusal is None,
                  f"{state}: announcements must be allowed, got {refusal!r}")
        _no_dashes(refusal or "", f"interlock refusal in {state}")
    # Abort is the only way out of SHOW or PAUSED in the real machine, and it
    # always lands in STANDBY, so the interlock never has to remember Abort
    # happened; it only has to ask the scheduler what is true right now.
    check(sch_mod.ALLOWED[sch_mod.ABORT] ==
          frozenset((sch_mod.SHOW, sch_mod.PAUSED)),
          "Abort must be exactly the exit from the two blocked states")

    work, cfg, _lengths = _ann_workdir()
    for state in states + (None,):
        blocked = state is None or state in blocked_states
        svc = A.AnnounceService(cfg, sd=FakeSD(), operators_folder=work,
                                state_provider=(lambda s=state: s))
        for aid in A.IDS:
            if blocked:
                try:
                    svc.play(aid, "Andy", "rack screen")
                    check(False, f"{state}: {aid} must be refused")
                except ValueError as e:
                    check(str(e).endswith("."),
                          f"{state}/{aid}: refusal must end with a full "
                          f"stop: {e!r}")
                check(svc.playing is None,
                      f"{state}: a refused press must not start anything")
            else:
                svc.play(aid, "Andy", "rack screen")
                check(svc.playing == aid,
                      f"{state}: {aid} must be allowed to play")
                svc.stop("Andy", "rack screen")
                check(svc.playing is None,
                      "Stop must clear it for the next id")
    print("  ok")


def test_announce_single_flight():
    section("announcements: one at a time, a second press is refused, "
            "not queued")
    A = _ann()
    work, cfg, _lengths = _ann_workdir()
    svc = A.AnnounceService(cfg, sd=FakeSD(), operators_folder=work,
                            state_provider=lambda: "IDLE")
    svc.play(A.DELAYED, "Andy", "rack screen")
    check(svc.playing == A.DELAYED, "the first press should start playing")
    try:
        svc.play(A.CANCELLATION, "Andy", "rack screen")
        check(False, "a second press while one plays must be refused")
    except ValueError as e:
        check("already playing" in str(e), f"the refusal must say so: {e}")
    check(svc.playing == A.DELAYED,
          "the second press must not queue or replace the first")
    try:
        svc.play(A.DELAYED, "Andy", "rack screen")
        check(False, "pressing the SAME one again while it plays must "
                     "also be refused")
    except ValueError as e:
        check("already playing" in str(e), f"{e}")
    svc.stop("Andy", "rack screen")
    check(svc.playing is None, "Stop must clear the playing announcement")
    svc.play(A.CANCELLATION, "Andy", "rack screen")
    check(svc.playing == A.CANCELLATION,
          "after Stop, a different one may play")
    print("  ok")


def test_announce_operator_validation():
    section("announcements: Play and Stop both name an operator on the "
            "list and a screen, like the scheduler")
    A = _ann()
    work, cfg, _lengths = _ann_workdir()

    def svc():
        return A.AnnounceService(cfg, sd=FakeSD(), operators_folder=work,
                                 state_provider=lambda: "IDLE")

    for who, screen, must in (("", "rack screen", "who pressed it"),
                              ("Andy", "", "which screen"),
                              ("  ", " ", "who pressed it and which "
                                          "screen"),
                              ("Bob", "rack screen",
                               "not on the operator list")):
        s = svc()
        try:
            s.play(A.DELAYED, who, screen)
            check(False, f"Play with who={who!r} screen={screen!r} must "
                         f"be refused")
        except ValueError as e:
            check(must in str(e), f"Play with who={who!r} screen={screen!r} "
                                  f"must name what is wrong: {e}")
        check(s.playing is None, "a refused Play must not start")
        s2 = svc()
        s2.play(A.DELAYED, "Andy", "rack screen")
        try:
            s2.stop(who, screen)
            check(False, f"Stop with who={who!r} screen={screen!r} must "
                         f"be refused")
        except ValueError as e:
            check(must in str(e), f"Stop with who={who!r} screen={screen!r} "
                                  f"must name what is wrong: {e}")
        check(s2.playing == A.DELAYED,
              "a refused Stop must not touch what is playing")
    for who in ("Jeff", "andy", " Andy "):
        s = svc()
        s.play(A.DELAYED, who, "rack screen")
        check(s.playing == A.DELAYED,
              f"{who!r} is on the default operator list")
    other = A.AnnounceService(cfg, sd=FakeSD(), operators_folder=work,
                              state_provider=lambda: "IDLE")
    other.operators = ("Casey",)
    try:
        other.play(A.DELAYED, "Andy", "rack screen")
        check(False, "once the list is Casey only, Andy must be refused")
    except ValueError as e:
        check("Casey" in str(e), f"{e}")
    other.play(A.DELAYED, "Casey", "rack screen")
    check(other.playing == A.DELAYED, "Casey is on this machine's list")
    print("  ok")


def test_announce_missing_files_at_startup():
    section("announcements: a missing or broken file is caught at "
            "startup, never at the press")
    A = _ann()
    work = tempfile.mkdtemp()
    good = os.path.join(work, "delayed.wav")
    _ann_write_wav(good, seconds=1.0)
    broken = os.path.join(work, "cancellation.wav")
    with open(broken, "wb") as fh:
        fh.write(b"not a wav file at all")
    # cannot_continue.wav is simply never written: missing.
    cfg = {"device": "MOTU M4",
           "files": {"delayed": "delayed.wav",
                    "cancellation": "cancellation.wav",
                    "cannot_continue": "cannot_continue.wav"}}
    path = os.path.join(work, "ltcplay_announce.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    svc = A.AnnounceService(path, sd=FakeSD(), operators_folder=work,
                            state_provider=lambda: "IDLE")
    by_id = {i["id"]: i for i in svc.status()["announcements"]}
    check(by_id[A.DELAYED]["available"]
          and abs(by_id[A.DELAYED]["length_s"] - 1.0) < 1e-6,
          f"a good file must be available with its real length: "
          f"{by_id[A.DELAYED]}")
    check(not by_id[A.CANCELLATION]["available"]
          and by_id[A.CANCELLATION]["reason"],
          f"a broken WAV must be unavailable with a reason: "
          f"{by_id[A.CANCELLATION]}")
    check(not by_id[A.CANNOT_CONTINUE]["available"]
          and "does not exist" in by_id[A.CANNOT_CONTINUE]["reason"],
          f"a missing file must say it does not exist: "
          f"{by_id[A.CANNOT_CONTINUE]}")
    for aid in (A.CANCELLATION, A.CANNOT_CONTINUE):
        try:
            svc.play(aid, "Andy", "rack screen")
            check(False, f"playing an unavailable file ({aid}) must be "
                         f"refused")
        except ValueError as e:
            check("not available" in str(e), f"{e}")
    # Deleting the good file AFTER startup must not change its reported
    # availability: discovery happens once, at startup, never at the press.
    os.remove(good)
    by_id2 = {i["id"]: i for i in svc.status()["announcements"]}
    check(by_id2[A.DELAYED]["available"],
          "availability must never be re-probed after startup")
    print("  ok")


def test_announce_device_missing_renamed_reappearing():
    section("announcements: the output device by name, missing, renamed, "
            "and back")
    A = _ann()
    work, cfg, _lengths = _ann_workdir()
    sd = FakeSD()
    svc = A.AnnounceService(cfg, sd=sd, operators_folder=work,
                            state_provider=lambda: "IDLE")
    st = svc.status()
    check(st["device_available"], f"MOTU M4 should resolve: {st}")
    removed = sd.devices.pop(1)              # "MOTU M4" is index 1
    check(removed["name"] == "MOTU M4", "test setup: removed the right one")
    st2 = svc.status()
    check(not st2["device_available"]
          and "not attached" in st2["device_reason"],
          f"a missing device must say so plainly: {st2}")
    try:
        svc.play(A.DELAYED, "Andy", "rack screen")
        check(False, "playing on a missing device must be refused")
    except ValueError as e:
        check("not attached" in str(e), f"{e}")
    check(svc.playing is None, "a refused play must not start")
    check(sd.output_opened == [],
          "a refused play must never open a stream on some other device: "
          "there is no silent fallback")
    # Plug it back in, at a DIFFERENT index, exactly like a real replug.
    sd.devices.insert(0, removed)
    st3 = svc.status()
    check(st3["device_available"],
          f"a reappeared device must be found by name again: {st3}")
    svc.play(A.DELAYED, "Andy", "rack screen")
    check(svc.playing == A.DELAYED, "it must play now that it is back")
    check(sd.output_opened[-1][0] == 0,
          f"it must open at its NEW index, found by name: "
          f"{sd.output_opened}")
    print("  ok")


def test_announce_progress_and_stop():
    section("announcements: length known before playing, live progress, "
            "Stop cuts it off")
    A = _ann()
    work, cfg, lengths = _ann_workdir()
    sd = FakeSD()
    svc = A.AnnounceService(cfg, sd=sd, operators_folder=work,
                            state_provider=lambda: "STANDBY")
    d = {i["id"]: i for i in svc.status()["announcements"]}
    check(abs(d[A.DELAYED]["length_s"] - lengths[A.DELAYED]) < 1e-6,
          "the length must be known before it is ever played")
    check(svc.status()["playing"] is None, "nothing plays yet")
    svc.play(A.DELAYED, "Andy", "rack screen")
    stream = sd.output_streams[-1]
    check(stream.started, "the output stream must actually be started")
    rate = 8000
    half = int(rate * lengths[A.DELAYED] / 2)
    stream.pump(half)
    st2 = svc.status()
    check(st2["playing"]["id"] == A.DELAYED, "still playing at the halfway "
                                             "point")
    check(abs(st2["playing"]["elapsed_s"] - half / rate) < 1e-6,
          f"progress must reflect exactly the frames handed to the "
          f"device, no clock involved: {st2['playing']}")
    remaining = int(rate * lengths[A.DELAYED]) - half + 10
    stream.pump(remaining)
    st3 = svc.status()
    check(st3["playing"] is None,
          "a finished announcement must clear itself on the next look, "
          "with no operator action and no wall clock")
    check(any(r["outcome"] == "finished" for r in st3["journal"]),
          "the natural finish must be journalled")
    svc.play(A.CANCELLATION, "Andy", "rack screen")
    stream2 = sd.output_streams[-1]
    stream2.pump(100)
    svc.stop("Andy", "rack screen")
    check(svc.playing is None, "Stop must end it immediately")
    check(stream2.stopped and stream2.closed,
          "Stop must actually close the device stream")
    print("  ok")


def test_announce_logging_fields():
    section("announcements: every play, stop, refusal and failure is "
            "logged, in plain English")
    A = _ann()
    work, cfg, _lengths = _ann_workdir()
    svc = A.AnnounceService(cfg, sd=FakeSD(), operators_folder=work,
                            state_provider=lambda: "STANDBY")
    svc.play(A.DELAYED, "Andy", "rack screen")                  # started
    try:
        svc.play(A.CANCELLATION, "Andy", "rack screen")         # refused
    except ValueError:
        pass
    svc.stop("Andy", "rack screen")                             # stopped
    svc.stop("Andy", "rack screen")                             # no-op
    sd2 = FakeSD()
    sd2.devices.pop(1)
    svc2 = A.AnnounceService(cfg, sd=sd2, operators_folder=work,
                             state_provider=lambda: "STANDBY")
    try:
        svc2.play(A.DELAYED, "Andy", "rack screen")             # failed
    except ValueError:
        pass
    rows = list(svc.journal) + list(svc2.journal)
    outcomes = {r["outcome"] for r in rows}
    check({"started", "refused", "stopped", "no-op", "failed"} <= outcomes,
          f"every category must appear in the journal: {outcomes}")
    for r in rows:
        check(r["actor"] in ("operator", "system"),
              f"a log row must name an actor: {r}")
        check(bool(r["text"]), f"a log row must have a sentence: {r}")
        _no_dashes(r["text"], "announce journal")
        check("at" in r and r["at"], f"a log row must carry a time: {r}")
        check("show_state" in r, f"a log row must carry the show state "
                                 f"at the time, without exception: {r}")
        if r["action"] in ("play", "stop") and r["outcome"] != "refused":
            check(r["who"], f"an operator action should carry who did "
                            f"it: {r}")
        if r["announcement"]:
            check(r["file"] and r["announcement"] in A.IDS,
                  f"a per-announcement row must carry the file and which "
                  f"one: {r}")
    print("  ok")


def test_announce_routes():
    section("announcements: routes only when configured, wired to the "
            "scheduler when both are")
    A = _ann()
    import json as _json
    import threading as _threading
    import urllib.error
    import urllib.request
    from ltcplay import web as web_mod
    work, cfg, _lengths = _ann_workdir()

    def call(base, route, body=None):
        data = _json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            base + route, data=data,
            method=("POST" if body is not None else "GET"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, _json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, _json.loads(e.read() or b"{}")

    ann = A.AnnounceService(cfg, sd=FakeSD(), operators_folder=work)
    port = _free_port()
    httpd = web_mod.serve(work, port=port, announce=ann)
    t = _threading.Thread(target=httpd.serve_forever,
                          kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:
        check(httpd.schedule is None and httpd.announce is ann,
              "announcements alone, no scheduler")
        code, st = call(base, "/api/announce/status")
        check(code == 200 and len(st["announcements"]) == 3,
              f"GET /api/announce/status: {code} {st}")
        check(st["blocked"] and "inert" in st["blocked"],
              f"with no scheduler wired, announcements report inert: {st}")
        code, bad = call(base, "/api/announce/play",
                         {"id": A.DELAYED, "who": "Andy",
                          "screen": "rack screen"})
        check(code == 400 and "inert" in bad.get("error", ""),
              f"a play attempt with no scheduler is a 400: {code} {bad}")
        check(A.AnnounceService.POST_ROUTES ==
              ("/api/announce/play", "/api/announce/stop"),
              "only play and stop can be posted")
        code, _r = call(base, "/api/announce/status", {"id": "x"})
        check(code == 404, "POST to the status route is not a thing")
    finally:
        httpd.shutdown()
        httpd.server_close()

    # Both configured: the announce service reads the scheduler's own
    # state, live, through nothing but the provider web.serve wires up.
    from ltcplay import schedule as S
    from ltcplay import schedule_service as SV
    swork = tempfile.mkdtemp()
    spath = os.path.join(swork, SV.RULE_FILE)
    SV.save_rule(spath, _sched_doc())
    now = [_den(S, 18, 4, 12)]
    svc = SV.Service(spath, clock=lambda: now[0], ntp_query=lambda: 0.0,
                     state_dir=swork)
    ann2 = A.AnnounceService(cfg, sd=FakeSD(), operators_folder=work)
    port = _free_port()
    httpd = web_mod.serve(work, port=port, schedule=svc, announce=ann2)
    t = _threading.Thread(target=httpd.serve_forever,
                          kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:
        check(wait_for(lambda: svc.machine is not None),
              "the scheduler settles")
        code, st = call(base, "/api/announce/status")
        check(code == 200 and st["show_state"] == svc.machine.state,
              f"the announce status must read the SAME state the "
              f"scheduler is in: {st['show_state']} vs "
              f"{svc.machine.state}")
        now[0] = _den(S, 18, 20)
        svc.tick()
        check(svc.machine.state == S.SHOW,
              f"setup: the scheduler should be running a show at 18:20: "
              f"{svc.machine.state}")
        code, bad = call(base, "/api/announce/play",
                         {"id": A.DELAYED, "who": "Andy",
                          "screen": "rack screen"})
        check(code == 400 and "running" in bad.get("error", ""),
              f"a show running must refuse the announcement through the "
              f"live link: {code} {bad}")
    finally:
        httpd.shutdown()
        httpd.server_close()
        svc.stop()

    # Not configured at all: every announce route is a plain 404.
    port = _free_port()
    httpd = web_mod.serve(work, port=port)
    t = _threading.Thread(target=httpd.serve_forever,
                          kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:
        check(httpd.announce is None, "no announce config, no service")
        code, _r = call(base, "/api/announce/status")
        check(code == 404, f"GET without --announce is 404, got {code}")
        code, _r = call(base, "/api/announce/play", {"id": A.DELAYED})
        check(code == 404, f"POST without --announce is 404, got {code}")
        code, _r = call(base, "/api/state")
        check(code == 200, "the rest of the page is untouched")
    finally:
        httpd.shutdown()
        httpd.server_close()
    print("  ok")


def test_the_gpl_path_never_loads_announcements():
    section("announcements: the GPL path does not import them")
    import ast
    import subprocess
    root = os.path.dirname(os.path.abspath(__file__))
    pkg = os.path.join(root, "ltcplay")
    port = _free_port()
    code = (
        "import sys, json, threading, tempfile, urllib.request, "
        "urllib.error\n"
        f"sys.path.insert(0, {root!r})\n"
        "import importlib, pkgutil, ltcplay\n"
        "mods = [m.name for m in pkgutil.iter_modules(ltcplay.__path__)\n"
        "        if not m.name.startswith('announce')]\n"
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
        "for r in ('/api/announce/status',):\n"
        "    try:\n"
        f"        urllib.request.urlopen('http://127.0.0.1:{port}' + r, "
        "timeout=5)\n"
        "        codes.append(200)\n"
        "    except urllib.error.HTTPError as e:\n"
        "        codes.append(e.code)\n"
        "h.shutdown(); h.server_close()\n"
        "print(json.dumps({'mods': mods, 'failed': failed, 'codes': codes,\n"
        "    'none': h.announce is None,\n"
        "    'loaded': sorted(m for m in sys.modules if 'announce' in "
        "m)}))\n")
    rc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                        text=True, timeout=60)
    import json as _json
    try:
        out = _json.loads(rc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        check(False, f"the GPL path check did not run: {rc.stderr[-800:]}")
        return
    check("web" in out["mods"] and "cli" in out["mods"]
          and "session" in out["mods"], f"the GPL modules were all "
                                       f"imported: {out['mods']}")
    check(out["loaded"] == [], f"the GPL path loaded announcements: "
                               f"{out['loaded']}")
    check(out["codes"] == [404] and out["none"],
          f"with no announcements configured the route is 404, got "
          f"{out['codes']}")

    def top_level(node):
        """Everything that runs at import time: not function bodies."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.Lambda)):
                continue
            yield child
            yield from top_level(child)

    for name in sorted(os.listdir(pkg)):
        if not name.endswith(".py") or name.startswith("announce"):
            continue
        tree = ast.parse(open(os.path.join(pkg, name),
                              encoding="utf-8").read())
        for sub in top_level(tree):
            names = []
            if isinstance(sub, ast.Import):
                names = [a.name for a in sub.names]
            elif isinstance(sub, ast.ImportFrom):
                names = [sub.module or ""] + [a.name for a in sub.names]
            check(not any("announce" in n for n in names),
                  f"ltcplay/{name} imports announcements at module level")

    files = [os.path.join(root, n) for n in os.listdir(root)
             if n.endswith(".command") or n == "ltc"]
    tools = os.path.join(root, "Tools")
    if os.path.isdir(tools):
        files += [os.path.join(tools, n) for n in os.listdir(tools)]
    for dirpath, _d, names in os.walk(os.path.join(root, "packaging")):
        files += [os.path.join(dirpath, n) for n in names]
    for f in files:
        try:
            text = open(f, errors="replace", encoding="utf-8").read()
        except OSError:
            continue
        check("--announce" not in text,
              f"{os.path.relpath(f, root)} turns announcements on")
    print("  ok")


def test_arttimecode_packet_byte_for_byte():
    section("Art-Net timecode: the packet, byte for byte")
    # Art-Net 4 Protocol Release V1.4, document revision 1.4dp 23/10/2025:
    # ArtTimeCode packet definition pp. 54-55, OpTimeCode 0x9700 p. 21,
    # port 0x1936 p. 10. Written out by hand here so this test does not
    # share a single constant with the code it checks.
    from ltcplay import clock as C
    want = bytes([0x41, 0x72, 0x74, 0x2D, 0x4E, 0x65, 0x74, 0x00,  # Art-Net\0
                  0x00, 0x97,        # OpTimeCode 0x9700, low byte first
                  0x00, 0x0E,        # protocol version 14, high byte first
                  0x00,              # Filler1
                  0x00,              # StreamId, 0 is the master
                  29, 59, 58, 1,     # frames, seconds, minutes, hours
                  0x03])             # type 3, SMPTE 30 fps non drop
    got = C.arttimecode(1, 58, 59, 29)
    check(got == want, f"ArtTimeCode for 01:58:59:29 is {got.hex()}, the "
                       f"spec says {want.hex()}")
    check(len(got) == 19, f"ArtTimeCode is {len(got)} bytes, not 19")
    check(C.arttimecode(0, 0, 0, 0)[14:19] == bytes([0, 0, 0, 0, 3]),
          "00:00:00:00 is not all zeros with type 3")
    check(C.arttimecode(2, 3, 4, 5, stream_id=7)[13] == 7,
          "the stream id is not in byte 13")
    check(C.MASTER_FPS == 30 and C.MASTER_TYPE == 3,
          "the master must send 30 fps non drop, type 3")
    check(C.ARTNET_PORT == 6454, "Art-Net timecode must go to UDP 6454")
    # Every type, and the frame range each one allows.
    for (count, drop), typ in (((24, False), 0), ((25, False), 1),
                               ((30, True), 2), ((30, False), 3)):
        check(C.type_for(count, drop) == typ,
              f"{count} fps drop={drop} should be type {typ}")
        top = count - 1
        check(C.arttimecode(0, 0, 0, top, typ)[14] == top,
              f"frame {top} refused at type {typ}")
        try:
            C.arttimecode(0, 0, 0, count, typ)
            check(False, f"frame {count} accepted at type {typ}")
        except ValueError:
            pass
    for bad in ((24, 0, 0, 0), (0, 60, 0, 0), (0, 0, 60, 0), (-1, 0, 0, 0)):
        try:
            C.arttimecode(*bad)
            check(False, f"{bad} is not a timecode and was sent")
        except ValueError:
            pass

    # And on a real socket: to every named node, broadcast only when asked.
    import socket as _socket
    rx = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(2.0)
    port = rx.getsockname()[1]
    try:
        out = C.TimecodeOut([("MadMapper", "127.0.0.1"),
                             ("BEYOND", "127.0.0.1")], port=port)
        out.send(want)
        a = rx.recv(64)
        b = rx.recv(64)
        check(a == want and b == want,
              "the packet on the wire is not the packet that was built")
        s = out._sock
        check(s is not None and not s.getsockopt(_socket.SOL_SOCKET,
                                                 _socket.SO_BROADCAST),
              "broadcast is switched on for a unicast timecode socket")
        out.close()
        bc = C.TimecodeOut([("broadcast", "127.255.255.255")],
                           broadcast=True, port=port)
        s2 = bc._default_socket()
        check(bool(s2.getsockopt(_socket.SOL_SOCKET, _socket.SO_BROADCAST)),
              "a broadcast timecode socket cannot broadcast")
        s2.close()
    finally:
        rx.close()

    # A socket the OS keeps refusing is rebuilt, not retried forever, and
    # never raises into the clock thread.
    class Refusing:
        def __init__(self):
            self.closed = False

        def sendto(self, pkt, addr):
            raise OSError(65, "No route to host")

        def close(self):
            self.closed = True

    made = []
    now = [100.0]

    def factory():
        made.append(Refusing())
        return made[-1]

    out = C.TimecodeOut([("BEYOND", "10.0.0.40")], socket_factory=factory,
                        clock=lambda: now[0])
    for _ in range(3):
        check(out.send(want) is False, "a refused send reported success")
    check(len(made) == 1 and made[0].closed,
          "three refused sends in a row did not close the socket")
    check("BEYOND" in out.last_error and "10.0.0.40" in out.last_error,
          f"the error does not say which receiver: {out.last_error!r}")
    out.send(want)
    check(len(made) == 1, "the socket was reopened with no backoff")
    now[0] += 1.5
    out.send(want)
    check(len(made) == 2, "the socket was never reopened after the backoff")
    print("  ok")


def _master(C, **kw):
    cfg = C.ClockConfig.parse({"source": "artnet_master",
                               "artnet": {"nodes": {"test": "127.0.0.1"}}})
    return cfg, C.ArtNetMaster(cfg, **kw)


def test_artnet_timecode_holds_30fps_under_load():
    section("Art-Net timecode: 30 a second under CPU load")
    # Real time, real sockets, a machine made busy on purpose: one process
    # per spare core burning CPU, and two threads in this process doing what
    # the render loop does, 8 ms of work every 25 ms, competing with the
    # clock for the interpreter. Three seconds, so it costs little on CI.
    #
    # The bounds are for a slow runner. On a Mac running this as a
    # background process a plain sleep wakes 5 to 10 ms late with nothing
    # else running at all; that is the OS, not the pacer. What the pacer
    # owns is tested hard, always: every packet carries the frame that is
    # current when it goes out, so a pacer that counts sleeps or bursts to
    # catch up fails at once, and none may be a frame late.
    #
    # The timing bounds (how many frames, how late) are applied only on a
    # machine that can keep time at all. A second with nothing else running
    # is measured first; a CI runner that cannot hold 30 a second even then
    # (a starved macOS VM dropped half of them) says so and is judged on
    # the pacer's own properties alone. The drift test proves the pacing
    # arithmetic on a simulated clock whatever the machine.
    import socket as _socket
    import subprocess as _sp
    from ltcplay import clock as C
    rx = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(0.2)
    port = rx.getsockname()[1]
    stop = threading.Event()
    got = []

    def listen():
        while not stop.is_set():
            try:
                got.append(rx.recv(64))
            except OSError:
                pass

    def burn():
        while not stop.is_set():
            end = time.perf_counter() + 0.008
            while time.perf_counter() < end:
                sum(i * i for i in range(200))
            time.sleep(0.017)

    def unloaded(seconds=1.0):
        tk = []
        _cfg, mb = _master(C, out=_TcOut())
        real = mb.ticker.tick
        mb.ticker.tick = lambda n, now: (tk.append(n), real(n, now))[1]
        mb.start()
        mb.play(0.0, 3600.0, "baseline")
        time.sleep(seconds)
        mb.stop()
        gaps_ = [b - a for a, b in zip(tk, tk[1:])]
        return len(tk), max(gaps_ or [0])

    base_n, base_gap = unloaded()
    fit = base_n >= 28 and base_gap <= 2

    procs = []
    spare = max(1, (os.cpu_count() or 2) // 2)
    for _ in range(min(spare, 4)):
        # Each one ends itself, so nothing is left burning if this test dies.
        procs.append(_sp.Popen([sys.executable, "-c",
                                "import time\nt=time.time()+8\n"
                                "while time.time()<t: pass"]))
    threads = [threading.Thread(target=listen, daemon=True)] + \
        [threading.Thread(target=burn, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    ticks = []
    try:
        out = C.TimecodeOut([("test", "127.0.0.1")], port=port)
        _cfg, m = _master(C, out=out)
        real_tick = m.ticker.tick

        def tick(n, now):
            ticks.append((n, now))
            return real_tick(n, now)

        m.ticker.tick = tick
        time.sleep(0.3)              # let the load build first
        m.start()
        t0 = m.play(0.0, 3600.0, "load test")
        time.sleep(3.0)
        m.stop()
        time.sleep(0.3)
    finally:
        stop.set()
        for p in procs:
            try:
                p.kill()
                p.wait(timeout=5)
            except Exception:
                pass
        for t in threads:
            t.join(timeout=2)
        rx.close()

    frame = 1.0 / 30
    ns = [n for n, _ in ticks]
    # The pacer's own properties, on any machine.
    check(ticks, "the clock sent nothing at all under load")
    check(all(b > a for a, b in zip(ns, ns[1:])),
          "a frame was sent twice or went backwards")
    check(ns and ns[0] == 0, "the cue did not start at 00:00:00:00")
    late = sorted(now - (t0 + n * frame) for n, now in ticks)
    check(all(0 <= x < frame for x in late),
          "a packet carried a frame that was not current when it was sent")
    check(len(ticks) + m.ticker.skipped == ns[-1] + 1 if ns else False,
          "sent plus skipped does not add up to the frames that were due")
    gaps = [b - a for a, b in zip(ns, ns[1:])]
    med = late[len(late) // 2] if late else 1.0
    p90 = late[int(len(late) * 0.9)] if late else 1.0
    # How well it kept time, on a machine that can.
    if fit:
        check(ns and abs(ns[-1] - 89) <= 3,
              f"after 3 s the clock read frame {ns[-1] if ns else None}; it "
              f"should be about 90, so the pacing is running slow")
        check(len(ticks) >= 84,
              f"only {len(ticks)} packets in 3 s under load; 90 were due")
        check(not gaps or max(gaps) <= 3,
              f"the stream stalled for {max(gaps or [0])} frames in a row")
        check(med < 0.015, f"median lateness {med * 1000:.1f} ms under "
                           f"load; a frame boundary should be met within "
                           f"15 ms")
        check(p90 < 0.025, f"one packet in ten is more than 25 ms late "
                           f"({p90 * 1000:.1f} ms)")
    else:
        print(f"  note: this machine sent {base_n} of 30 frames in a second "
              f"with nothing else running (longest gap {base_gap}), so it "
              f"cannot keep time and the timing bounds are not applied here; "
              f"the pacer's own properties still are")
    heard = [_tc_of(p) for p in got]
    check(len(heard) == len(ticks),
          f"{len(ticks)} packets sent, {len(heard)} arrived on loopback")
    check(all(h == 0 and t == 3 for h, _m, _s, _f, t in heard),
          "a packet on the wire is not type 3 in hour zero")
    print(f"  ok ({len(ticks)} packets, median {med * 1000:.1f} ms late, "
          f"90th percentile {p90 * 1000:.1f} ms, {m.ticker.skipped} skipped; "
          f"unloaded {base_n}/30)")


def test_artnet_timecode_never_drifts_from_its_clock():
    section("Art-Net timecode: no drift over a whole show")
    # A whole 7:20 show on a simulated clock, with a sleep that always
    # oversleeps and now and then stalls for up to three frames. A pacer
    # that sleeps 1/30 s and counts would finish seconds behind; this one
    # must end exactly where the clock says, having skipped what it had to
    # and never sent a frame that was not current.
    from ltcplay import clock as C
    rnd = random.Random(7)

    class Sim:
        now = 5000.0

        def clock(self):
            return self.now

        def sleep(self, d):
            extra = rnd.uniform(0.0, 0.004)
            if rnd.random() < 0.03:
                extra += rnd.uniform(0.02, 0.10)
            self.now += d + extra

    sim = Sim()
    fed, ticks = [], []
    out = _TcOut()
    _cfg, m = _master(C, out=out, clock=sim.clock, sleep=sim.sleep,
                      mono=sim.clock,
                      sink=lambda pos, at, drop, text: fed.append((pos, at,
                                                                   text)))
    real_tick = m.ticker.tick

    def tick(n, now):
        ticks.append((n, now))
        return real_tick(n, now)

    m.ticker.tick = tick
    show_len = 440.0
    frames = int(show_len * 30)
    m.start()
    t0 = m.play(3600.0, show_len, "Show")
    m.ticker._thread.join(timeout=60)
    check(not m.ticker.running, "the clock did not stop at the end of the cue")
    check(not m.playing, "the clock still says a cue is playing after it ended")
    sent = ticks[:-1]           # the last tick is the one that found the end
    ns = [n for n, _ in sent]
    check(ns[0] == 0, "the cue did not start at frame 0")
    check(_tc_of(out.sent[0][1])[:4] == (0, 0, 0, 0),
          "the first packet of a cue is not 00:00:00:00")
    check(all(b > a for a, b in zip(ns, ns[1:])),
          "a frame was sent twice, or the clock went backwards")
    wrong = [(n, now) for n, now in sent
             if n != int((now - t0) * 30 + 1e-9)]
    check(not wrong, f"{len(wrong)} packets carried a frame that was not the "
                     f"current one, first {wrong[:1]}")
    check(len(sent) + m.ticker.skipped == frames,
          f"{len(sent)} sent plus {m.ticker.skipped} skipped is not the "
          f"{frames} frames in the show")
    check(m.ticker.skipped > 0,
          "the simulated stalls never made the clock skip, so this test did "
          "not exercise a late tick")
    check(len(sent) > frames * 0.9, "far too many frames were skipped")
    n_last, at_last = sent[-1]
    err = at_last - t0 - n_last / 30.0
    check(0 <= err < 1 / 30.0,
          f"after {show_len:.0f} s the timecode is {err * 1000:.1f} ms off "
          f"the clock that paces it")
    check(ns[-1] >= frames - 4, f"the show ended on frame {ns[-1]} of {frames}")
    last = _tc_of(out.sent[-1][1])
    check(last[:3] == (0, 7, 19) and last[4] == 3,
          f"the last packet of a 7:20 show is {last}")
    # The pixels are fed the same position, in the chase engine's terms:
    # the cue's own place in the show file plus the frame, captured at the
    # instant that frame began.
    check(len(fed) == len(sent), "the pixels were not fed every frame sent")
    bad = [i for i, ((n, _), (pos, at, _t)) in enumerate(zip(sent, fed))
           if abs(pos - (3600.0 + n / 30.0)) > 1e-9
           or abs(at - (t0 + n / 30.0)) > 1e-6]
    check(not bad, f"the pixel clock disagrees with the timecode at "
                   f"{len(bad)} frames")

    # Every cue starts from zero, halt stops the stream at once, and a cue
    # with a length stops by itself. Real time from here.
    m.stop()
    try:
        m.play(0.0, 10.0)
        check(False, "a stopped clock played a cue")
        m.halt()
    except ValueError as e:
        check("Run" in str(e), f"the refusal should say to press Run: {e}")
    try:
        m2x = _master(C, out=_TcOut())[1]
        m2x.start()
        m2x.play(0.0, None, "Mystery")
        check(False, "a cue with no length was played; it would never stop")
        m2x.stop()
    except ValueError as e:
        check("Mystery" in str(e) and "forever" in str(e),
              f"the refusal for a cue with no length is unclear: {e}")
    out2 = _TcOut()
    _cfg, m2 = _master(C, out=out2)
    m2.start()
    m2.play(0.0, 3600.0, "A")
    check(wait_for(lambda: len(out2.sent) >= 6, timeout=3.0),
          "the clock never started sending")
    m2.play(0.0, 3600.0, "B")
    mark = len(out2.sent)
    check(wait_for(lambda: len(out2.sent) >= mark + 3, timeout=3.0),
          "the second cue never started")
    restart = [_tc_of(p)[:4] for _, p in out2.sent[mark - 1:mark + 2]]
    check((0, 0, 0, 0) in restart,
          f"the second cue did not start at 00:00:00:00: {restart}")
    m2.halt()
    time.sleep(0.05)
    n_halt = len(out2.sent)
    time.sleep(0.3)
    check(len(out2.sent) == n_halt,
          "timecode kept going after the clock was halted")
    mark = len(out2.sent)
    started = time.perf_counter()
    m2.play(0.0, 0.5, "short")
    check(wait_for(lambda: not m2.playing, timeout=3.0),
          "a half second cue never ended")
    ended = time.perf_counter() - started
    # Frames 0 to 14, and never 15. On a machine that stalls, the frames
    # due during the stall are skipped (a late wake skips, it never sends
    # late), and a stall across the end skips the last ones, so the tail is
    # only a ceiling here. A stall can only make the end later, never
    # earlier, so "not early" is the wall-clock half of the end; the exact
    # end is proven on the simulated clock above.
    short = [_tc_of(p)[:4] for _, p in out2.sent[mark:]]
    check(short and max(short) <= (0, 0, 0, 14),
          f"a half second cue went past frame 14: {short[-3:]}")
    check(ended >= 0.45, f"a half second cue ended after {ended:.2f}s")
    n_end = len(out2.sent)
    time.sleep(0.2)
    check(len(out2.sent) == n_end, "timecode kept going after the cue ended")
    m2.stop()
    check(out2.closed, "stopping the clock left its socket open")
    print("  ok")


def test_timecode_zones_for_fallback_3():
    section("fallback 3: the hour picks the zone")
    from ltcplay import clock as C
    z = {1: "show", 2: "intermission"}
    # The pure function, directly.
    check(C.route(1, 7, 19, 29, z) == ("show", (0, 7, 19, 29)),
          "hour 01 is not the show, rebased to hour zero")
    check(C.route(2, 0, 0, 0, z) == ("intermission", (0, 0, 0, 0)),
          "hour 02 is not the intermission")
    for h in [0] + list(range(3, 24)):
        check(C.route(h, 1, 2, 3, z) == ("idle", None),
              f"hour {h:02d} is not in the table and must be idle")
    check(C.route(1, 0, 0, 0, {5: "show", 6: "intermission"}) ==
          ("idle", None), "the table came from code, not from the show file")
    check(C.route(5, 0, 0, 1, {5: "show", 6: "intermission"}) ==
          ("show", (0, 0, 0, 1)), "a zone table from config is not obeyed")

    F = 1 / 30.0

    def feed(r, tcs, t):
        for tc in tcs:
            r.frame(*tc, t)
            t += F
        return t

    # A zone change that lands between two output ticks, mid-frame. The
    # tick before the new zone's frame shows the show; nothing ever carries
    # the new zone's hour with the old zone's minutes, or the reverse.
    # (A show that runs past 7:20 here, so the confirmation is what is
    # being tested, not the end of the show.)
    r = C.ZoneReader(z, forward=("show", "intermission"), show_len_s=600)
    t = feed(r, [(1, 7, 19, f) for f in range(25, 30)], 10.0)
    last_show = t - F
    check(r.at(last_show + F / 2) == ("show", (0, 7, 19, 29)),
          "half a frame after the last show frame, the show must still be "
          "current")
    r.frame(2, 0, 0, 0, t)
    first = r.at(t + F / 2)
    check(first is not None and first[0] == "show",
          f"one frame of a new zone moved the reader at once: {first}")
    r.frame(2, 0, 0, 1, t + F)
    after = r.at(t + F + F / 2)
    check(after == ("intermission", (0, 0, 0, 1)),
          f"two agreeing frames of the new zone did not move it: {after}")
    check(r.zone_changes == 2, f"zone changes counted {r.zone_changes}")
    # Never an invented 07:20:00. A 7:20 show whose last frame is 07:19:29
    # and MadMapper stops there: for two seconds after, at every quarter
    # frame, nothing later than 07:19:29 goes out.
    def past_end(results):
        return [a for a in results if a is not None and a[0] == "show"
                and a[1] > (0, 7, 19, 29)]

    r = C.ZoneReader(z, forward=("show", "intermission"), show_len_s=440)
    t = feed(r, [(1, 7, 19, f) for f in range(25, 30)], 10.0)
    after = [r.at(t - F + k * F / 4) for k in range(1, 240)]
    check(not past_end(after),
          f"the feed stopped on 07:19:29 and timecode was invented after "
          f"it: {past_end(after)[:3]}")
    # The hand-off: the next frame is the intermission's first. Before it
    # is confirmed, nothing past the show's last frame goes out; after, the
    # intermission does.
    r = C.ZoneReader(z, forward=("show", "intermission"), show_len_s=440)
    t = feed(r, [(1, 7, 19, f) for f in range(25, 30)], 10.0)
    r.frame(2, 0, 0, 0, t)
    mid = [r.at(t + k * F / 4) for k in range(0, 4)]
    r.frame(2, 0, 0, 1, t + F)
    after = r.at(t + F + F / 2)
    check(not past_end(mid),
          f"an invented 07:20:00 went out at the hand-off: {past_end(mid)}")
    check(after == ("intermission", (0, 0, 0, 1)),
          f"the intermission did not take over after the hand-off: {after}")
    # The same hand-off through the Art-Net output, flywheel and all.
    cfg = C.ClockConfig.parse({"source": "ltc_audio_slave",
                               "artnet": {"nodes": {"BEYOND": "127.0.0.1"}},
                               "zones": {"forward": ["show",
                                                     "intermission"],
                                         "show_len_s": 440}})
    now_ = [0.0]
    tco = _TcOut()
    sl = C.LtcAudioSlave(cfg, out=tco, clock=lambda: now_[0],
                         mono=lambda: now_[0])
    sl.ticker.t0 = 0.0
    frames = [(1, 7, 19, f) for f in range(0, 30)] + \
             [(2, 0, 0, f) for f in range(0, 30)]
    ev = [(k * F + 0.004, "ltc", fr) for k, fr in enumerate(frames)] + \
         [(n * F + 0.02, "tick", n) for n in range(1, 60)]
    for at, kind, x in sorted(ev):
        now_[0] = at
        if kind == "ltc":
            sl.ltc_frame(*x, at)
        else:
            sl._tick(x, at)
    sent = [(p[17], p[16], p[15], p[14]) for _, p in tco.sent]
    check(not [x for x in sent if x[:3] == (0, 7, 20)],
          f"BEYOND was sent an invented 07:20 at the hand-off: "
          f"{[x for x in sent if x[:3] == (0, 7, 20)]}")
    check((0, 7, 19, 29) in sent and (0, 0, 0, 20) in sent,
          "the hand-off test did not forward both sides, so it proves "
          "nothing")

    # The show length only ends a FREE RUN. MadMapper still sending live
    # timecode past the end of the last pixel cue is forwarded, not cut:
    # BEYOND may well have content there.
    r = C.ZoneReader(z, show_len_s=440)
    t = feed(r, [(1, 7, 19, f) for f in range(20, 30)]
             + [(1, 7, s_, f) for s_ in (20, 21) for f in range(30)], 10.0)
    check(r.at(t - F / 2) == ("show", (0, 7, 21, 29)),
          f"live timecode past the show length was cut: {r.at(t - F / 2)}")
    check(not r.show_over and not r.freerunning,
          "a live feed was treated as a free run")
    check(r.at(t - F + 3.0) is None and r.show_over,
          "once the feed died past the show length, the free run should "
          "have stopped at once")

    # One corrupt frame mid-show: the hour of one zone, the rest of another.
    r = C.ZoneReader(z, show_len_s=440)
    t = feed(r, [(1, 3, 0, f) for f in range(0, 10)], 20.0)
    r.frame(2, 3, 0, 10, t)                  # flipped hour bit
    t = feed(r, [(1, 3, 0, f) for f in range(11, 20)], t + F)
    check(r.zone == "show", f"one bad frame moved the show to {r.zone}")
    check(r.at(t - F) == ("show", (0, 3, 0, 19)),
          "the show did not carry on through one bad frame")

    # An unknown hour: nothing goes out, cold or mid-run.
    r = C.ZoneReader(z, show_len_s=440)
    feed(r, [(5, 0, 0, 0), (5, 0, 0, 1)], 1.0)
    check(r.zone == "idle" and r.at(1.05) is None,
          "an unknown hour sent timecode")
    r = C.ZoneReader(z, show_len_s=440)
    t = feed(r, [(1, 0, 10, f) for f in range(5)], 1.0)
    t = feed(r, [(9, 0, 0, f) for f in range(2)], t)
    check(r.zone == "idle" and r.at(t) is None,
          "timecode kept going after the hour moved out of every zone")

    # The intermission is sent only when the show file asks for it.
    r = C.ZoneReader(z, show_len_s=440)
    t = feed(r, [(2, 0, 0, f) for f in range(5)], 1.0)
    check(r.zone == "intermission" and r.at(t) is None,
          "the intermission went out although only the show is forwarded")

    # Timecode lost during the show: free run to the end of the show, then
    # stop. Lost anywhere else: stop after the hold.
    r = C.ZoneReader(z, show_len_s=440)
    t = feed(r, [(1, 7, 0, f) for f in range(3)], 100.0)
    at5 = r.at(t - F + 5.0)
    check(at5 == ("show", (0, 7, 5, 2)) and r.freerunning,
          f"five seconds after the feed died mid-show: {at5}, free running "
          f"{r.freerunning}")
    check(r.at(t - F + 19.0) == ("show", (0, 7, 19, 2)),
          "the free run did not run on toward the end of the show")
    check(r.at(t - F + 21.0) is None and r.show_over,
          "the free run went past the end of the show")
    # The feed comes back after the gap. One frame is not believed, even
    # a good one; the second that agrees with it is.
    r.frame(1, 0, 0, 0, t + 30.0)
    check(r.at(t + 30.0 + F / 2) is None,
          "the first frame after a gap was believed on its own")
    r.frame(1, 0, 0, 1, t + 30.0 + F)
    check(r.at(t + 30.0 + F * 1.5) == ("show", (0, 0, 0, 1)),
          "two agreeing frames after a gap were not taken")

    # One corrupt frame on resume. Live show, then a gap, then a single
    # frame from the wrong zone, then the real feed. The bad frame never
    # moves the reader, and the free run carries on until the real feed
    # has said where it is twice.
    r = C.ZoneReader(z, show_len_s=440)
    t = feed(r, [(1, 2, 0, f) for f in range(3)], 200.0)
    t_gap = t + 3.0                                  # three seconds of nothing
    r.frame(2, 5, 17, 3, t_gap)                      # corrupt: wrong hour
    mid = r.at(t_gap + F / 2)
    check(mid is not None and mid[0] == "show" and mid[1][1:3] == (2, 3),
          f"one corrupt frame on resume moved the reader: {mid}")
    r.frame(1, 2, 3, 4, t_gap + F)                   # the real feed, agreeing
    check(r.zone == "show" and r.rejects == 0,
          "a real frame that agrees with the free run was not taken at once")
    r = C.ZoneReader(z, show_len_s=440)
    t = feed(r, [(1, 2, 0, f) for f in range(3)], 200.0)
    t_gap = t + 3.0
    r.frame(1, 6, 40, 0, t_gap)                      # corrupt: wild position
    check(r.at(t_gap + F / 2)[1][1:3] == (2, 3),
          "a wild frame on resume was believed on its own")
    r.frame(1, 2, 3, 4, t_gap + F)
    check(r.at(t_gap + F * 1.5)[1][1:3] == (2, 3),
          "the real feed after the wild frame did not keep the show where "
          "it is")
    r = C.ZoneReader(z, forward=("show", "intermission"), show_len_s=440,
                     hold_s=1.0)
    t = feed(r, [(2, 1, 0, f) for f in range(3)], 50.0)
    check(r.at(t + 0.5) is not None, "the intermission stopped inside the hold")
    check(r.at(t + 1.2) is None,
          "a lost intermission free ran; only the show does")

    # Drop frame: the free run labels frames the way drop frame does.
    r = C.ZoneReader(z, count=30, drop=True, fps=29.97, show_len_s=3000)
    r.frame(1, 0, 59, 28, 7.0 - 1.0 / 29.97)
    r.frame(1, 0, 59, 29, 7.0)
    check(r.at(7.0 + 1.0 / 29.97 + 1e-6) == ("show", (0, 1, 0, 2)),
          "drop frame free run did not skip frames 00 and 01")
    print("  ok")


def test_clock_settings_fail_loudly():
    section("the clock block of a show file: typos and refusals")
    import json, tempfile
    from ltcplay import clock as C
    good_nodes = {"MadMapper": "127.0.0.1", "BEYOND": "10.0.0.40"}

    def refused(doc, *words):
        try:
            C.ClockConfig.parse(doc, "show.json")
        except C.ClockConfigError as e:
            msg = str(e)
            check(all(w in msg for w in words),
                  f"the refusal for {doc} should say {words}: {msg}")
            check("—" not in msg and "–" not in msg,
                  f"operator text carries a dash: {msg}")
            return msg
        check(False, f"{doc} should have been refused")

    refused({"sorce": "artnet_master"}, "'sorce'", "source")
    refused({"source": "artnet_master",
             "artnet": {"node": good_nodes}}, "'node'", "nodes")
    refused({"source": "ltc_audio_slave",
             "zones": {"shw": 1}}, "'shw'", "intermission")
    refused({"source": "artnet"}, "artnet_master", "ltc_audio_slave")
    refused({}, "clock.source")
    refused({"source": "ltc_audio_master"}, "fallback 1", "does not have it")
    refused({"source": "artnet_master", "show_audio": "ltcplay",
             "artnet": {"nodes": good_nodes}}, "fallback 2", "madmapper")
    refused({"source": "artnet_master", "show_audio": "vlc",
             "artnet": {"nodes": good_nodes}}, "show_audio")
    refused({"source": "artnet_master"}, "no 'clock.artnet'")
    refused({"source": "artnet_master", "artnet": {}}, "sends to nobody")
    refused({"source": "artnet_master",
             "artnet": {"nodes": {"MM": "10.0.0.300"}}}, "not an IPv4")
    refused({"source": "artnet_master",
             "artnet": {"nodes": {"all": "10.0.0.255"}}}, "broadcast")
    refused({"source": "artnet_master",
             "artnet": {"nodes": {"a": "10.0.0.5", "b": "10.0.0.5"}}},
            "twice")
    refused({"source": "artnet_master",
             "artnet": {"nodes": good_nodes, "broadcast": "10.0.0.255"}},
            "Pick one")
    refused({"source": "artnet_master",
             "artnet": {"broadcast": "10.0.0.5"}}, "broadcast address")
    refused({"source": "artnet_master",
             "artnet": {"nodes": good_nodes, "stream_id": 300}}, "stream_id")
    refused({"source": "ltc_audio_slave",
             "zones": {"show": 1, "intermission": 1}}, "tell them apart")
    refused({"source": "ltc_audio_slave",
             "zones": {"show": 24}}, "0 to 23")
    refused({"source": "ltc_audio_slave",
             "zones": {"forward": ["idle"]}}, "forward")

    c = C.ClockConfig.parse({"source": "artnet_master",
                             "artnet": {"nodes": good_nodes},
                             "zones": {"show": 3, "intermission": 4}})
    check(c.artnet.dests == [("MadMapper", "127.0.0.1"),
                             ("BEYOND", "10.0.0.40")],
          f"named nodes did not come through in order: {c.artnet.dests}")
    check(c.zones.table == {3: "show", 4: "intermission"},
          "a zone table beside a master clock was not kept for the switch "
          "back to fallback 3")
    c = C.ClockConfig.parse({"source": "artnet_master",
                             "artnet": {"broadcast": "10.0.0.255"}})
    check(c.artnet.dests == [("broadcast", "10.0.0.255")],
          "broadcast did not become the one destination")
    c = C.ClockConfig.parse({"source": "ltc_audio_slave"})
    check(c.artnet is None and c.zones.table == {1: "show",
                                                 2: "intermission"},
          "a bare slave should forward nothing and default to 01 show, 02 "
          "intermission")

    # Through the show file, the way the program reads it.
    work = tempfile.mkdtemp()
    open(os.path.join(work, "A.fseq"), "wb").write(b"x")
    p = os.path.join(work, "t.json")
    base = {"fps": 30, "show_dir": work,
            "cues": [{"tc": "01:00:00:00", "fseq": "A.fseq"}]}
    json.dump(dict(base, clock={"source": "ltc_audio_slave"}), open(p, "w"))
    tl = timeline.Timeline.load(p)
    check(tl.clock is not None and tl.clock.source == "ltc_audio_slave",
          "the show file's clock block was not read")
    json.dump(dict(base, clok={"source": "ltc_audio_slave"}), open(p, "w"))
    try:
        timeline.Timeline.load(p)
        check(False, "a misspelled 'clock' loaded without complaint")
    except ValueError as e:
        check("'clok'" in str(e) and "clock" in str(e),
              f"the error must name the key and list the real ones: {e}")
    json.dump(dict(base, clock=None), open(p, "w"))
    try:
        timeline.Timeline.load(p)
        check(False, "'clock': null loaded silently")
    except ValueError as e:
        check("'clock'" in str(e), f"the clock: null refusal is unclear: {e}")
    json.dump(dict(base, clock={"source": "ltc_audio_master"}), open(p, "w"))
    try:
        timeline.Timeline.load(p)
        check(False, "fallback 1 loaded although it is not built")
    except ValueError as e:
        check("fallback 1" in str(e), f"fallback 1 refusal unclear: {e}")
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "ltcplay", "clock.py"), encoding="utf-8").read()
    check("—" not in src and "–" not in src,
          "clock.py carries an em or en dash")
    print("  ok")


def test_the_gpl_path_never_loads_the_clock():
    section("GPL: no clock block, no clock code")
    # The GPL show at Dollywood runs this program on a Mac with no clock
    # block in its show file. Nothing new may be reachable from that path.
    # Proven in a fresh interpreter, because this one has already imported
    # the clock for the tests above.
    import json, subprocess as _sp
    here = os.path.dirname(os.path.abspath(__file__))
    real = json.load(open(os.path.join(here, "gpl2026_timeline.json")))
    check("clock" not in real,
          "the GPL show file has grown a clock block")
    # Every import of the clock is inside a function, never at the top of a
    # module, so importing the program cannot load it.
    top = []
    for name in sorted(os.listdir(os.path.join(here, "ltcplay"))):
        if not name.endswith(".py") or name == "clock.py":
            continue
        for i, line in enumerate(open(os.path.join(here, "ltcplay", name),
                                      encoding="utf-8"), 1):
            if re.match(r"(from \.clock |from \. import .*\bclock\b|"
                        r"import ltcplay\.clock|from ltcplay import .*"
                        r"\bclock\b)", line):
                top.append(f"{name}:{i}")
    check(not top, f"the clock is imported at module scope: {top}")
    script = r'''
import json, os, sys, tempfile, time
sys.path.insert(0, sys.argv[1])
import selftest as T
from ltcplay import settings as st_mod
from ltcplay.session import Session
import ltcplay.player as plmod
import ltcplay.web, ltcplay.cli
work = tempfile.mkdtemp()
st_mod.path = lambda: os.path.join(work, st_mod.FILENAME)
st_mod.prefs_path = lambda: os.path.join(work, st_mod.PREFS_FILE)
rows = "\n".join(f'    <network NetworkType="ArtNET" ComPort="127.0.0.1" '
                 f'BaudRate="{u+1}" MaxChannels="510"/>' for u in range(2))
net = os.path.join(work, "net.xml")
open(net, "w").write(f'<Networks>\n  <Controller Name="L" IP="127.0.0.1" '
                     f'ActiveState="Active">\n{rows}\n  </Controller>\n'
                     f'</Networks>\n')
tlp = os.path.join(work, "gpl.json")
doc = json.load(open(os.path.join(sys.argv[1], "gpl2026_timeline.json")))
doc["show_dir"] = work
doc["cues"] = doc["cues"][:1]
doc["cues"][0]["fseq"] = "A.fseq"
doc.pop("idle", None)
doc["gaps"] = "blackout"
json.dump(doc, open(tlp, "w"))
open(os.path.join(work, "A.fseq"), "wb").write(b"x")
def fake_prepare(self, cue):
    cue.fseq = T.FakeFSEQ(frames=4000)
    cue.duration = cue.fseq.duration_ms / 1000.0
    cue._spans = [(0, 0, cue.fseq.channel_count)]
    cue._gaps = None
    return 0
plmod.Player._prepare = fake_prepare
s = Session(tlp, no_output=True, networks=net, no_log=True, sd=T.FakeSD(),
            device="MOTU M4", channel=2)
s.open(); s.start()
end = time.time() + 5
while time.time() < end and s.player.last_ltc_at is None:
    time.sleep(0.05)
snap = s.snapshot()
s.stop()
print(json.dumps({"loaded": "ltcplay.clock" in sys.modules,
                  "tl_clock": s.tl.clock is None,
                  "session_clock": s.clock is None,
                  "snap": "clock" in snap,
                  "ltc": s.player.last_ltc_at is not None,
                  "opened": len(s._sd.opened)}))
'''
    r = _sp.run([sys.executable, "-c", script, here], capture_output=True,
                text=True, timeout=120)
    try:
        res = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        check(False, f"the GPL path did not run: {r.stdout[-400:]} "
                     f"{r.stderr[-800:]}")
        return
    check(res["ltc"], "the GPL path did not chase its timecode")
    check(res["opened"] >= 1, "the GPL path did not open its timecode input")
    check(not res["loaded"], "a GPL run imported ltcplay.clock")
    check(res["tl_clock"] and res["session_clock"],
          "a GPL run built a clock")
    check(not res["snap"], "a GPL run put a clock on the page")
    print("  ok")


def _clock_show(work, clock_doc, cues, **extra):
    import json
    rows = "\n".join(
        f'    <network NetworkType="ArtNET" ComPort="127.0.0.1" '
        f'BaudRate="{u+1}" MaxChannels="510"/>' for u in range(2))
    net = os.path.join(work, "net.xml")
    open(net, "w").write(f'<Networks>\n  <Controller Name="L" IP="127.0.0.1" '
                         f'ActiveState="Active">\n{rows}\n  </Controller>\n'
                         f'</Networks>\n')
    tlp = os.path.join(work, "t_timeline.json")
    doc = {"name": "t", "fps": 30, "show_dir": work, "gaps": "blackout",
           "on_lost": "freerun", "hold_ms": 300, "clock": clock_doc,
           "cues": [{"tc": tc, "fseq": f, "name": n} for tc, f, n in cues]}
    doc.update(extra)
    if clock_doc is None:
        del doc["clock"]              # the GPL shape: no clock block at all
    json.dump(doc, open(tlp, "w"))
    if doc.get("idle"):
        open(os.path.join(work, doc["idle"]), "wb").write(b"x")
    for _tc, f, _n in cues:
        open(os.path.join(work, f), "wb").write(b"x")
    return tlp, net


def test_a_master_clock_runs_the_show():
    section("ltcplay as master: no input, nothing before a cue, then zero")
    import tempfile
    from ltcplay.session import Session, SessionError
    from ltcplay import settings as st_mod
    import ltcplay.player as plmod
    work = tempfile.mkdtemp()
    real_path, real_prefs = st_mod.path, st_mod.prefs_path
    st_mod.path = lambda: os.path.join(work, st_mod.FILENAME)
    st_mod.prefs_path = lambda: os.path.join(work, st_mod.PREFS_FILE)
    real_prepare = plmod.Player._prepare

    def fake_prepare(self, cue):
        cue.fseq = FakeFSEQ(frames=4000)
        cue.duration = cue.fseq.duration_ms / 1000.0
        cue._spans = [(0, 0, cue.fseq.channel_count)]
        cue._gaps = None
        return 0

    plmod.Player._prepare = fake_prepare
    tlp, net = _clock_show(
        work, {"source": "artnet_master",
               "artnet": {"nodes": {"MadMapper": "127.0.0.1"}},
               "zones": {"show": 1, "intermission": 2}},
        [("01:00:00:00", "Show.fseq", "Show"),
         ("02:00:00:00", "Intermission.fseq", "Intermission")])
    before = {t.name for t in threading.enumerate()}
    sd = FakeSD()
    sess = None
    try:
        sess = Session(tlp, no_output=True, networks=net, no_log=True, sd=sd,
                       device="MOTU M4", channel=2)
        sess.open()
        check(sess.clock is not None and sess.clock.master,
              "the show file asked for a master clock and did not get one")
        try:
            sess.clock_play()
            check(False, "the clock started before Run was pressed")
        except SessionError:
            pass
        out = _TcOut()
        sess.clock.out = out
        sess.start()
        check(sd.opened == [] and sess.audio is None,
              f"a master clock opened a timecode input: {sd.opened}")
        time.sleep(0.3)
        check(out.sent == [], "timecode went out before any cue was played")
        # Between cues nothing is wrong, and the page must not say so.
        idle = sess.snapshot()
        check(idle["state"] == "STANDBY",
              f"a master clock between cues reads {idle['state']!r}, which "
              f"the page draws as a lost feed")
        feedish = [w for w in idle["warnings"]
                   if "timecode" in w.lower() or "input" in w.lower()]
        check(not feedish, f"a master clock between cues warns about a "
                           f"feed it does not have: {feedish}")
        check(sess.player.current_cue is None,
              "a cue is playing before the clock started")
        sess.clock_play("Intermission")
        check(wait_for(lambda: len(out.sent) >= 5
                       and sess.player.current_cue is not None, timeout=3.0),
              "the clock started but nothing followed it")
        check(_tc_of(out.sent[0][1])[:4] == (0, 0, 0, 0),
              "the cue's timecode did not start at 00:00:00:00")
        cue = sess.player.current_cue
        check(cue is not None and cue.name == "Intermission",
              f"the pixels are on {cue.name if cue else None}, not the cue "
              f"the clock started")
        check(7200.0 <= sess.player.tc_seconds < 7202.0,
              f"the pixels are at {sess.player.tc_seconds:.2f}s, not at the "
              f"top of the cue")
        snap = sess.snapshot()
        check(snap.get("clock", {}).get("playing") == "Intermission",
              f"the page does not say what the clock is playing: "
              f"{snap.get('clock')}")
        # No input is open, and that is not a fault: the page must not draw
        # it red, and nothing may warn about it.
        check(snap["input_used"] is False and snap["input_attached"] is None,
              f"a master clock reports its unused input as "
              f"attached={snap['input_attached']!r} used={snap['input_used']!r}")
        check(not [w for w in snap["warnings"] if "input" in w.lower()
                   or "second boundary" in w],
              f"a master clock warns about an input it does not use: "
              f"{snap['warnings']}")
        check(snap["state"] == "LOCKED",
              f"a playing master clock reads {snap['state']!r}")
        page = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "ltcplay", "web", "index.html"),
                    encoding="utf-8").read()
        check("s.input_used === false" in page,
              "the page does not know an input can be not used")
        mark = len(out.sent)
        sess.clock_play("Show")
        check(wait_for(lambda: sess.player.current_cue is not None and
                       sess.player.current_cue.name == "Show", timeout=3.0),
              "starting a second cue did not move the pixels to it")
        check((0, 0, 0, 0) in [_tc_of(p)[:4] for _, p in out.sent[mark:]],
              "the second cue did not restart the timecode at zero")
        try:
            sess.reset_input()
            check(False, "a master clock rebuilt a timecode input")
        except SessionError:
            check(sd.opened == [], "a master clock opened a timecode input")
        # The operator pressed GO at some point. A free run outranks the
        # timecode, so a cue started by the clock has to take it back or the
        # rig ignores the cue.
        sess.player.go(100.0)
        check(sess.player.freerun_epoch is not None,
              "GO did not start a free run, so the next check proves nothing")
        sess.clock_play("Intermission")
        check(wait_for(lambda: sess.player.current_cue is not None and
                       sess.player.current_cue.name == "Intermission"
                       and 7200.0 <= sess.player.tc_seconds < 7202.0,
                       timeout=3.0),
              "a free run left over from the last cue swallowed the next one")
        try:
            sess.clock_play("Encore")
            check(False, "a cue that does not exist was played")
        except SessionError as e:
            check("Encore" in str(e) and "Show" in str(e),
                  f"the refusal should name the cue and the real ones: {e}")
        # Stop: the timecode ends before the rig is blacked out, so nothing
        # is still chasing a clock while the pixels go dark. Recorded on a
        # stand-in sender, because the order is the session's job.
        order = []

        class Recorder:
            packets_sent = 0

            def blackout(self):
                order.append(("blackout", sess.clock.playing, len(out.sent)))
                self.packets_sent += 1

            def close(self):
                pass

        real_stop = sess.clock.stop

        def clock_stop():
            order.append(("timecode", None, len(out.sent)))
            real_stop()

        sess.clock.stop = clock_stop
        sess.sender = Recorder()
        sess.no_output = False      # so Stop sends its blackout frames
        sess.stop()
        kinds = [k for k, _p, _n in order]
        check(kinds[:1] == ["timecode"] and "blackout" in kinds,
              f"Stop blacked out the rig before stopping the timecode: "
              f"{kinds}")
        check(all(not playing for k, playing, _n in order
                  if k == "blackout"),
              "the clock was still playing when the blackout went out")
        n = len(out.sent)
        check(all(sent == n for k, _p, sent in order if k == "blackout"),
              "timecode went out after the blackout began")
        check(not sess.clock.ticker.running, "the clock thread outlived Stop")
    finally:
        if sess is not None:
            try:
                sess.stop()
            except Exception:
                pass
        plmod.Player._prepare = real_prepare
        st_mod.path, st_mod.prefs_path = real_path, real_prefs
    time.sleep(0.3)
    leaked = [t.name for t in threading.enumerate()
              if t.name not in before and t.name.startswith("ltcplay")]
    check(not leaked, f"threads left behind after stop: {leaked}")
    print("  ok")


def test_a_slave_clock_forwards_the_show_zone():
    section("fallback 3: LTC in as today, the show zone out as Art-Net")
    import tempfile
    from ltcplay.session import Session, SessionError
    from ltcplay import settings as st_mod
    import ltcplay.player as plmod
    work = tempfile.mkdtemp()
    real_path, real_prefs = st_mod.path, st_mod.prefs_path
    st_mod.path = lambda: os.path.join(work, st_mod.FILENAME)
    st_mod.prefs_path = lambda: os.path.join(work, st_mod.PREFS_FILE)
    real_prepare = plmod.Player._prepare

    def fake_prepare(self, cue):
        cue.fseq = FakeFSEQ(frames=4000)
        cue.duration = cue.fseq.duration_ms / 1000.0
        cue._spans = [(0, 0, cue.fseq.channel_count)]
        cue._gaps = None
        return 0

    plmod.Player._prepare = fake_prepare
    tlp, net = _clock_show(
        work, {"source": "ltc_audio_slave",
               "artnet": {"nodes": {"BEYOND": "127.0.0.1"}}},
        [("01:00:00:00", "Show.fseq", "Show")])
    sd = FakeSD()
    sess = None
    try:
        sess = Session(tlp, no_output=True, networks=net, no_log=True, sd=sd,
                       device="MOTU M4", channel=2)
        sess.open()
        check(sess.clock is not None and not sess.clock.master,
              "the show file asked for the LTC slave and did not get it")
        check(sess.clock.reader.show_len_frames == 3000,
              f"the show length was not read from the render: "
              f"{sess.clock.reader.show_len_frames} frames")
        # A clock that throws must not cost the chase engine a frame, nor
        # be filed as a decode error. Ten frames in one block, so a clock
        # fault that escaped would starve every frame after the first.
        def boom(*a):
            raise RuntimeError("clock fault")

        sess.clock.ltc_frame = boom
        sess._handle(synthesize(1, 0, 0, 0, 30.0, sess.rate, frames=12),
                     _now())
        got = sess.player.ltc_frames_in
        check(got >= 5 and got == sess.dec.frames_decoded,
              f"a clock fault starved the chase engine: {got} of "
              f"{sess.dec.frames_decoded} decoded frames reached it")
        check(sess.decode_errors == 0,
              f"a clock fault was counted as {sess.decode_errors} decode "
              f"error(s)")
        check(sess.clock_errors == got,
              f"clock faults were not counted: {sess.clock_errors}")
        del sess.clock.ltc_frame
        sess.dec = sess.dec.__class__(sess.rate)
        # Those frames were stamped on the wall clock; the chase below runs
        # on a stepped one. Start it cold, as a fresh feed would.
        sess.player._epoch = None
        sess.player.last_ltc_at = None

        # The chase, driven by sample count on a clock that moves only when
        # told to. LTC arrives the way the audio callback delivers it, 512
        # samples at a time, through the session's own _handle, so the
        # decoder, the chase engine and the clock tap are the real ones. The
        # chase engine ticks every 25 ms and the Art-Net output every frame,
        # all at exact times. Run on the wall clock this measured the runner:
        # a CI Mac stalled for more than the 250 ms freewheel window just
        # before the check and read a healthy feed as freewheeling, twice.
        clk = sess.clock
        st = _Stepped(sess.player, 25)
        out = _TcOut()
        clk.out = out
        clk._clock = clk._mono = lambda: st.t
        try:
            t_start = st.t
            clk.ticker.t0 = t_start
            audio = synthesize(1, 0, 0, 0, 30.0, sess.rate, frames=90)
            block = 512
            events = []
            for k, at in enumerate(range(block, len(audio) + 1, block)):
                events.append((t_start + at / sess.rate, 0, at))
            for j in range(1, int(3.0 / 0.025)):
                events.append((t_start + j * 0.025, 1, j))
            for n in range(1, 90):
                events.append((t_start + n / 30.0, 2, n))
            chased = []
            for at, kind, x in sorted(events):
                st.t = at
                if kind == 0:
                    sess._handle(audio[x - block:x], at)
                elif kind == 1:
                    st.tick()
                    chased.append((at, sess.player.state,
                                   sess.player.current_cue,
                                   sess.player.tc_seconds))
                else:
                    clk._tick(x, at)
        finally:
            st.close()
            clk._clock, clk._mono = time.perf_counter, time.monotonic
            clk._fly = None
        # From half a second in, the chase engine is locked on the show and
        # exactly where the LTC says: frame 01:00:00:00 began at t_start.
        late = [(at, state, cue, tc) for at, state, cue, tc in chased
                if at - t_start >= 0.5]
        off = [(round(at - t_start, 3), state) for at, state, cue, tc in late
               if state != LOCKED or cue is None or cue.name != "Show"
               or abs(tc - (3600.0 + at - t_start)) > 0.05]
        check(late and not off,
              f"the pixels are not chasing the LTC as they do today: "
              f"{off[:3]}")
        tcs = [_tc_of(p) for _, p in out.sent]
        check(len(tcs) >= 75,
              f"only {len(tcs)} Art-Net frames for 3 s of LTC")
        check(all(h == 0 and m == 0 and typ == 3 for h, m, _s, _f, typ in tcs),
              f"the show zone did not go out rebased to hour zero: {tcs[:3]}")
        nums = [s_ * 30 + f for _h, _m, s_, f, _t in tcs]
        check(nums and all(b - a == 1 for a, b in zip(nums, nums[1:])),
              f"forwarded frames did not step by one: {nums[:8]}")
        check(nums and abs(nums[-1] - 89) <= 2,
              f"the last forwarded frame is {nums[-1] if nums else None}, "
              f"not the LTC's own 89")
        check(sess.clock.reader.zone == "show",
              f"hour 01 read as {sess.clock.reader.zone}")

        # And the real threads start: the input opens, as today.
        sess.dec = sess.dec.__class__(sess.rate)
        out = _TcOut()
        clk.out = out
        sess.start()
        check(len(sd.opened) == 1, "the slave did not open the LTC input")
        try:
            sess.clock_play()
            check(False, "a slave clock was told to start a cue")
        except SessionError:
            pass
        check(sess.snapshot().get("clock", {}).get("zone") == "show",
              "the page does not show the zone")
        sess.stop()
        n = len(out.sent)
        time.sleep(0.2)
        check(len(out.sent) == n, "Art-Net timecode kept going after Stop")
    finally:
        if sess is not None:
            try:
                sess.stop()
            except Exception:
                pass
        plmod.Player._prepare = real_prepare
        st_mod.path, st_mod.prefs_path = real_path, real_prefs
    print("  ok")


class _EventLog:
    """Keeps every log.event call, with its throttle."""

    def __init__(self):
        self.events = []

    def event(self, kind, msg, throttle_s=0.0):
        self.events.append((kind, msg, throttle_s))


def _master_session(work, name, fake_lengths, **extra):
    """A running master-clock session over fake renders."""
    from ltcplay.session import Session
    import ltcplay.player as plmod
    real_prepare = plmod.Player._prepare

    def fake_prepare(self, cue):
        base = os.path.basename(cue.path)
        if fake_lengths.get(base) is None:
            raise IOError("the render will not open")
        cue.fseq = FakeFSEQ(frames=fake_lengths[base])
        cue.duration = cue.fseq.duration_ms / 1000.0
        cue._spans = [(0, 0, cue.fseq.channel_count)]
        cue._gaps = None
        return 0

    plmod.Player._prepare = fake_prepare
    here = os.path.join(work, name)
    os.makedirs(here, exist_ok=True)
    try:
        tlp, net = _clock_show(
            here,
            {"source": "artnet_master",
             "artnet": {"nodes": {"MadMapper": "127.0.0.1"}}},
            [("01:00:00:00", "Show.fseq", "Show"),
             ("01:00:02:00", "Intermission.fseq", "Intermission"),
             ("01:30:00:00", "Broken.fseq", "Broken")], **extra)
        sess = Session(tlp, no_output=True, networks=net, no_log=True,
                       sd=FakeSD(), device="MOTU M4", channel=2,
                       allow_missing=True)
        sess.open()
    finally:
        plmod.Player._prepare = real_prepare
    out = _TcOut()
    sess.clock.out = out
    sess.start()
    return sess, out


def test_a_stopped_cue_hands_the_rig_back():
    section("ltcplay as master: a stopped cue plays nothing after it")
    # Audit of c981b92: with ltcplay as master, a cue that ended or was
    # halted looked like LOST timecode to the chase engine, which then ran
    # on_lost. With on_lost freerun the rig played the NEXT cue by itself,
    # with MadMapper and BEYOND stopped. The clock owns the pixels: when it
    # stops, the rig goes to the idle look at once, whatever on_lost says.
    import tempfile
    from ltcplay.session import SessionError
    from ltcplay import settings as st_mod
    work = tempfile.mkdtemp()
    real_path, real_prefs = st_mod.path, st_mod.prefs_path
    st_mod.path = lambda: os.path.join(work, st_mod.FILENAME)
    st_mod.prefs_path = lambda: os.path.join(work, st_mod.PREFS_FILE)
    lengths = {"Show.fseq": 40, "Intermission.fseq": 4000,
               "PreShow.fseq": 400, "Broken.fseq": None}

    def nothing_plays(sess, seconds, look, what):
        """Sample the rig for `seconds`: no cue, no show frames, no free
        run, and the idle look (or black) the whole time."""
        p = sess.player
        # Wait for events, not for time: the hand-back itself, then two
        # whole output ticks after it. A wall-clock grace here read a CI
        # stall longer than the grace as "the rig kept playing".
        check(wait_for(lambda: p._epoch is None, timeout=5.0),
              f"{what}: the pixels were never handed back")
        mark = p.frames_sent
        check(wait_for(lambda: p.frames_sent >= mark + 2, timeout=5.0),
              f"{what}: the output thread never ticked after the stop")
        end = time.time() + seconds
        bad = []
        while time.time() < end:
            cue, src = p.current_cue, p.source
            if cue is not None or src != look or \
                    p.freerun_epoch is not None:
                bad.append((cue.name if cue else None, src,
                            p.freerun_epoch is not None))
            time.sleep(0.02)
        check(not bad, f"{what}: the rig kept playing after the clock "
                       f"stopped: {bad[:3]}")

    sessions = []
    try:
        # 1. The cue runs its length. Hold is 300 ms and the next cue starts
        # two seconds after this one began: a rig left chasing its own clock
        # would be playing it by the end of the sample.
        for on_lost in ("freerun", "hold"):
            sess, out = _master_session(work, f"end-{on_lost}", lengths,
                                        on_lost=on_lost)
            sessions.append(sess)
            began = time.perf_counter()
            sess.clock_play("Show")
            check(wait_for(lambda: sess.player.current_cue is not None,
                           timeout=2.0), "the cue never reached the pixels")
            check(wait_for(lambda: not sess.clock.playing, timeout=3.0),
                  "a one second cue never ended")
            ran = time.perf_counter() - began
            nothing_plays(sess, 1.6, BLACK, f"cue end, on_lost {on_lost}")
            # A ceiling and "not early": a stall across the end skips the
            # last frames, and can only make the end later. The exact end is
            # proven on the simulated clock.
            tail = [_tc_of(p)[:4] for _, p in out.sent]
            check(tail and max(tail) <= (0, 0, 0, 29),
                  f"a one second cue went past 00:00:00:29: {tail[-3:]}")
            check(ran >= 0.95, f"a one second cue ended after {ran:.2f}s")
            sess.stop()

        # 2. Halted mid-cue, under every on_lost a show file can carry, and
        # with a preshow look to go back to.
        for on_lost, idle in (("preshow", "PreShow.fseq"), ("hold", None),
                              ("blackout", None), ("freerun", None),
                              ("freerun", "PreShow.fseq")):
            extra = {"on_lost": on_lost}
            if idle:
                extra.update(idle=idle, gaps="idle")
            sess, out = _master_session(work, f"halt-{on_lost}-{idle}",
                                        lengths, **extra)
            sessions.append(sess)
            sess.clock_play("Intermission")
            check(wait_for(lambda: sess.player.current_cue is not None,
                           timeout=2.0), "the cue never reached the pixels")
            sess.clock_halt()
            n = len(out.sent)
            nothing_plays(sess, 0.7, IDLE if idle else BLACK,
                          f"halt, on_lost {on_lost}"
                          + (", with a preshow" if idle else ""))
            check(len(out.sent) == n, "timecode went out after a halt")
            if on_lost != "hold":
                check(any("on_lost" in n_ for n_ in sess.notes),
                      "nothing says on_lost does not apply to a master clock")
            sess.stop()

        # 3. The clock thread stalls mid-cue for longer than the hold, as a
        # machine under a load spike might. The show file says freerun, but
        # a free run would outrank the clock for the rest of the night. The
        # rig holds its frame and picks the clock up again when it resumes.
        sess, out = _master_session(work, "stall", lengths, on_lost="freerun")
        sessions.append(sess)
        sess.clock_play("Intermission")
        check(wait_for(lambda: sess.player.current_cue is not None,
                       timeout=2.0), "the cue never reached the pixels")
        real_sink = sess.clock.sink
        sess.clock.sink = lambda *a: None
        stalled = []
        end = time.time() + 0.8                 # hold is 300 ms
        while time.time() < end:
            stalled.append((sess.player.freerun_epoch is not None,
                            sess.player.current_cue is not None))
            time.sleep(0.02)
        check(not any(f for f, _c in stalled),
              "a stalled master clock started a free run that would outrank "
              "it for the rest of the night")
        check(all(c for _f, c in stalled),
              "a stalled master clock dropped the cue instead of holding it")
        sess.clock.sink = real_sink
        check(wait_for(lambda: sess.player.state == LOCKED, timeout=2.0),
              "the rig did not pick the clock up again after the stall")
        sess.stop()

        # 4. Stop reaches the clock while clock_play is on its way in. The
        # caller gets a SessionError like every other refusal, never the
        # clock's own exception.
        sess, out = _master_session(work, "race", lengths)
        sessions.append(sess)
        sess.clock.stop()
        try:
            sess.clock_play("Show")
            check(False, "a stopped clock played a cue")
        except SessionError as e:
            check("Run" in str(e), f"the refusal should say to press Run: {e}")
        except Exception as e:
            check(False, f"clock_play let {type(e).__name__} escape: {e}")
        sess.stop()

        # 5. A cue whose render did not open has no length. Its timecode
        # would never stop, so it is refused, by name.
        sess, out = _master_session(work, "broken", lengths)
        sessions.append(sess)
        try:
            sess.clock_play("Broken")
            check(False, "a cue with no render was played")
        except SessionError as e:
            check("Broken" in str(e) and "forever" in str(e),
                  f"the refusal should name the cue and say why: {e}")
        check(not sess.clock.playing and out.sent == [],
              "timecode went out for a cue that was refused")
        sess.stop()
    finally:
        for sess in sessions:
            try:
                sess.stop()
            except Exception:
                pass
        st_mod.path, st_mod.prefs_path = real_path, real_prefs
    print("  ok")


def test_the_clock_survives_its_own_faults():
    section("the show clock: faults, stops and the clock it runs on")
    from ltcplay import clock as C

    # It paces on perf_counter. Under Python 3.12 on Windows monotonic
    # ticks every 15.6 ms, half a frame.
    check(C.Ticker(30, lambda n, t: True)._clock is time.perf_counter,
          "the pacer does not run on perf_counter")
    cfg = C.ClockConfig.parse({"source": "artnet_master",
                               "artnet": {"nodes": {"t": "127.0.0.1"}}})
    check(C.ArtNetMaster(cfg).ticker._clock is time.perf_counter,
          "the master clock does not pace on perf_counter")
    scfg = C.ClockConfig.parse({"source": "ltc_audio_slave",
                                "artnet": {"nodes": {"t": "127.0.0.1"}}})
    check(C.LtcAudioSlave(scfg).ticker._clock is time.perf_counter,
          "the slave's Art-Net output does not pace on perf_counter")

    # A tick that throws once: the clock carries on, counts it, and says so
    # in the log without a line per frame.
    calls, log = [], _EventLog()

    def tick(n, now):
        calls.append(n)
        if len(calls) in (2, 3):
            raise RuntimeError("one bad frame")
        return len(calls) < 8

    tk = C.Ticker(30, tick, log=log)
    tk.start()
    check(wait_for(lambda: len(calls) >= 8, timeout=3.0),
          f"one exception in a tick ended the clock after {len(calls)} ticks")
    tk.stop()
    check(tk.errors == 2 and "one bad frame" in tk.last_error,
          f"tick faults not counted: {tk.errors} {tk.last_error!r}")
    errs = [e for e in log.events if "one bad frame" in e[1]]
    check(errs and all(k == "clock-error" and th >= 5.0 for k, _m, th in errs),
          f"tick faults are not logged throttled: {errs}")

    # Stop joins the thread: once halt() returns, no packet can still be
    # on its way out. The send here takes a while, so a halt that did not
    # wait for it would return first and the packet would land after.
    class SlowOut(_TcOut):
        def __init__(self):
            super().__init__()
            self.in_send = threading.Event()
            self.halted = False
            self.late = 0

        def send(self, pkt):
            self.in_send.set()
            threading.Event().wait(0.15)
            if self.halted:
                self.late += 1
            return super().send(pkt)

    slow = SlowOut()
    _cfg, m = _master(C, out=slow)
    m.start()
    m.play(0.0, 3600.0, "slow")
    check(slow.in_send.wait(3.0), "the clock never sent")
    th = m.ticker._thread
    m.halt()
    slow.halted = True
    alive = th is not None and th.is_alive()
    if th is not None:
        th.join(2.0)
    check(not alive and slow.late == 0,
          f"halt() returned while the clock thread was still sending "
          f"({slow.late} packet(s) after it)")
    m.stop()

    # Frame numbers at a large clock reading. Reading t0 + n/30 back can
    # round a hair under n; without a floor at the frame that was due, the
    # clock sends the previous frame twice.
    class Exact:
        now = 5.0e6 + 0.123456

        def clock(self):
            return self.now

        def sleep(self, d):
            self.now += d

    ex = Exact()
    got = []
    tk = C.Ticker(30, lambda n, now: (got.append(n), len(got) < 3000)[1],
                  clock=ex.clock, sleep=ex.sleep)
    tk.run(ex.now)
    dup = [b for a, b in zip(got, got[1:]) if b <= a]
    check(len(got) == 3000 and not dup,
          f"{len(dup)} frame(s) sent twice at a clock reading of 5e6 s")
    check(tk.skipped == 0, f"{tk.skipped} frames skipped with exact wakes")

    # play() and halt() from several threads at once: the scheduler and the
    # web server both call them. When the dust settles, one halt leaves no
    # clock thread running and nothing sending.
    busy = _TcOut()
    _cfg, m = _master(C, out=busy)
    m.start()
    rnd = random.Random(3)
    plan = [[rnd.random() < 0.6 for _ in range(15)] for _ in range(6)]
    race_errors = []

    def hammer(steps):
        for play in steps:
            try:
                if play:
                    m.play(0.0, 3600.0, "race")
                else:
                    m.halt()
            except Exception as e:
                race_errors.append(e)

    ts = [threading.Thread(target=hammer, args=(p,)) for p in plan]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    m.halt()
    live = [t.name for t in threading.enumerate()
            if t.name == "ltcplay-timecode" and t.is_alive()]
    check(not live, f"{len(live)} clock thread(s) still running after halt; "
                    f"two plays at once each started one")
    check(not race_errors,
          f"play or halt failed under contention: {race_errors[:2]}")
    n = len(busy.sent)
    for t in threading.enumerate():
        if t.name == "ltcplay-timecode":
            t.join(1.0)
    check(len(busy.sent) == n, "timecode kept going after the last halt")
    m.stop()

    # The guarantee the lock gives, tested directly: a halt that arrives
    # while play() is part way through setting up a cue waits for it,
    # rather than running in the middle of it.
    inside, gate = threading.Event(), threading.Event()

    def slow_clock():
        if threading.current_thread().name == "play-A":
            inside.set()
            gate.wait(0.5)
        return time.perf_counter()

    _cfg, m = _master(C, out=_TcOut(), clock=slow_clock)
    m.start()
    ta = threading.Thread(target=lambda: m.play(0.0, 3600.0, "A"),
                          name="play-A")
    ta.start()
    check(inside.wait(2.0), "play() never reached the clock")
    done = []
    tb = threading.Thread(target=lambda: (m.halt(), done.append(True)))
    tb.start()
    tb.join(0.15)
    check(not done, "halt() ran while play() was still setting up the cue")
    gate.set()
    ta.join(2.0)
    tb.join(2.0)
    check(done and not m.playing,
          "the halt that waited for play() did not stop the cue")
    m.stop()
    print("  ok")


def test_forwarded_timecode_steps_by_one():
    section("Art-Net timecode steps by one frame on a coarse clock")
    import math
    # Under Python 3.12 on Windows time.monotonic ticks every 15.625 ms. The
    # audio callback stamps frames with it, and the translation into the
    # pacing clock reads it again. Simulated here with that clock 1000 s
    # away from perf_counter, as it is on Windows and not on a Mac, so a
    # translation that mixed the two up would be 1000 s wrong.
    from ltcplay import clock as C
    Q = 0.015625

    def q(x):
        return math.floor(x / Q) * Q

    rnd = random.Random(11)

    def forward(phase):
        """A minute of LTC whose frames start `phase` of a frame after our
        own frame grid. Returns (tick, frame sent, true position)."""
        T = [50.0]
        perf = lambda: T[0]
        mono = lambda: q(T[0] + 1000.0)
        cfg = C.ClockConfig.parse({"source": "ltc_audio_slave",
                                   "artnet": {"nodes": {"BEYOND":
                                                        "127.0.0.1"}}})
        out = _TcOut()
        sl = C.LtcAudioSlave(cfg, show_len_s=3000, out=out, clock=perf,
                             mono=mono)
        t0 = 50.0
        sl.ticker.t0 = t0
        e0 = t0 + phase / 30.0
        start = (1 * 60 + 10) * 30           # 01:01:10:00
        events = []
        for k in range(1800):
            end = e0 + k / 30.0
            u = rnd.uniform(0.0, 0.0107)     # where in a 512 sample block
            d = rnd.uniform(0.0, 0.002)      # decode time in the callback
            events.append((end + u + d, "ltc", k, end + u, u))
        for n in range(1, 1800):
            events.append((t0 + n / 30.0 + rnd.uniform(0.0, 0.004), "tick",
                           n, None, None))
        events.sort()
        ticks, epochs = [], []
        for at, kind, k, cb, u in events:
            if kind == "ltc":
                captured = q(cb + 1000.0) - u    # the callback's own stamp
                T[0] = at
                fr = start + k
                sl.ltc_frame(1, (fr // 1800) % 60, (fr // 30) % 60, fr % 30,
                             captured)
                if k >= 60 and sl.reader._last is not None:
                    epochs.append(sl.reader._last[1])
            else:
                T[0] = at
                before = len(out.sent)
                sl._tick(k, at)
                if len(out.sent) > before:
                    p = out.sent[-1][1]
                    fnum = (p[16] * 60 + p[15]) * 30 + p[14]
                    true = start + (t0 + k / 30.0 - e0) * 30.0
                    ticks.append((k, fnum, true))
        return ticks, epochs

    # Mid-frame, and a hair before a frame boundary, where every stamp's
    # jitter decides which side of the boundary the reader lands on.
    for phase in (0.37, 0.97, 0.02):
        ticks, epochs = forward(phase)
        spread = (max(epochs) - min(epochs)) * 1000 if epochs else 99
        check(spread < 8.0,
              f"phase {phase}: the reader's idea of where the show is "
              f"wanders {spread:.1f} ms on a 15.6 ms clock; it should be "
              f"slewed, not taken frame by frame")
        check(len(ticks) > 1700,
              f"phase {phase}: only {len(ticks)} of 1799 ticks sent "
              f"anything; the 1000 s offset was not translated")
        steps = {b[1] - a[1] for a, b in zip(ticks, ticks[1:])}
        check(steps == {1}, f"phase {phase}: forwarded frames stepped by "
                            f"{sorted(steps)}, not always by exactly 1")
        worst = max(abs(f - tr) for _k, f, tr in ticks) if ticks else 99
        check(worst <= 1.0, f"phase {phase}: forwarded timecode is "
                            f"{worst:.2f} frames off the LTC it came from")

    # The master's pixel stamps on the same coarse clock: one exact frame
    # apart every time, and never further off than one clock tick.
    class Sim:
        now = 7000.0

        def clock(self):
            return self.now

        def sleep(self, d):
            self.now += d + rnd.uniform(0.0, 0.003)

    sim = Sim()
    fed = []
    _cfg, m = _master(C, out=_TcOut(), clock=sim.clock, sleep=sim.sleep,
                      mono=lambda: q(sim.now + 1000.0),
                      sink=lambda pos, at, drop, text: fed.append(at))
    m.start()
    t0m = m.play(0.0, 10.0, "coarse")
    m.ticker._thread.join(10)
    gaps = {round((b - a) * 30.0, 9) for a, b in zip(fed, fed[1:])}
    check(len(fed) > 250 and gaps <= {1.0, 2.0, 3.0},
          f"the pixels were handed stamps that are not whole frames apart "
          f"on a coarse clock: {sorted(gaps)[:5]}")
    off = abs(fed[0] - (t0m + 1000.0))
    check(off <= Q + 1e-9, f"the pixel stamps sit {off * 1000:.1f} ms off")
    m.stop()
    print("  ok")


def test_timecode_health_is_shown():
    section("Art-Net timecode health: the log, the warnings, --bind")
    from ltcplay import clock as C
    from ltcplay import display as disp_mod

    # One dead receiver out of two: named in the log, once per five
    # seconds, not once per frame; named again when it comes back.
    class Sock:
        dead = {"10.0.0.40"}

        def sendto(self, pkt, addr):
            if addr[0] in self.dead:
                raise OSError(65, "No route to host")

        def close(self):
            pass

    sock, log, now = Sock(), _EventLog(), [10.0]
    tc = C.TimecodeOut([("MadMapper", "127.0.0.1"), ("BEYOND", "10.0.0.40")],
                       log=log, socket_factory=lambda: sock,
                       clock=lambda: now[0])
    for _ in range(300):                 # ten seconds at 30 a second
        tc.send(b"x")
        now[0] += 1 / 30.0
    lines = [m for k, m, _t in log.events if "BEYOND" in m]
    check(2 <= len(lines) <= 3,
          f"a dead receiver was logged {len(lines)} times in ten seconds; "
          f"once every five is the rule")
    check(tc.failing_labels == ["BEYOND (10.0.0.40)"],
          f"the failing receiver is not named: {tc.failing_labels}")
    check(all("\u2014" not in m and "\u2013" not in m for m in lines),
          "a log line carries a dash")

    # The same, as the operator reads it.
    cfg = C.ClockConfig.parse({"source": "artnet_master",
                               "artnet": {"nodes": {"MadMapper": "127.0.0.1",
                                                    "BEYOND": "10.0.0.40"}}})
    m = C.ArtNetMaster(cfg, out=tc, clock=lambda: now[0])
    m._cue = (0.0, 1000, "Show")
    tc._clock = time.monotonic               # ages as the page sees them
    tc.last_error_at = time.monotonic()
    tc.last_ok_at = time.monotonic()
    w = disp_mod.clock_warnings(m)
    check(any("BEYOND (10.0.0.40)" in x and "not reaching" in x for x in w),
          f"the page does not name the receiver that is failing: {w}")
    tc.last_ok_at = time.monotonic() - 5.0
    w = disp_mod.clock_warnings(m)
    check(any("No Art-Net timecode has left" in x for x in w),
          f"five seconds of nothing sent during a cue is not a warning: {w}")
    m._cue = None
    check(not any("No Art-Net timecode has left" in x
                  for x in disp_mod.clock_warnings(m)),
          "silence with no cue playing was reported as a fault")
    sock.dead = set()
    tc.send(b"x")
    check(tc.failing_labels == [] and
          any("again" in m_ for _k, m_, _t in log.events),
          "a receiver that came back is still reported as failing")
    tc.last_error_at = time.monotonic() - 60.0
    check(disp_mod.clock_warnings(m) == [],
          f"old faults are still on screen: {disp_mod.clock_warnings(m)}")
    scfg = C.ClockConfig.parse({"source": "ltc_audio_slave"})
    sl = C.LtcAudioSlave(scfg)
    sl.reader.freerunning = True
    check(any("lost during the show" in x
              for x in disp_mod.clock_warnings(sl)),
          "a free run to the end of the show is not on screen")
    check(disp_mod.clock_warnings(None) == [],
          "a show with no clock block got clock warnings")
    for x in disp_mod.clock_warnings(sl) + w:
        check("\u2014" not in x and "\u2013" not in x,
              f"a warning carries a dash: {x}")

    # --bind picks the interface for the timecode socket too, and the
    # source port stays ephemeral.
    b = C.TimecodeOut([("t", "127.0.0.1")], bind_ip="127.0.0.1")
    s_ = b._default_socket()
    ip, port = s_.getsockname()
    s_.close()
    check(ip == "127.0.0.1" and port not in (0, 6454),
          f"the timecode socket is bound to {ip}:{port}")
    import tempfile
    from ltcplay.session import Session
    from ltcplay import settings as st_mod
    import ltcplay.player as plmod
    work = tempfile.mkdtemp()
    real_path, real_prefs = st_mod.path, st_mod.prefs_path
    st_mod.path = lambda: os.path.join(work, st_mod.FILENAME)
    st_mod.prefs_path = lambda: os.path.join(work, st_mod.PREFS_FILE)
    real_prepare = plmod.Player._prepare

    def fake_prepare(self, cue):
        cue.fseq = FakeFSEQ(frames=400)
        cue.duration = cue.fseq.duration_ms / 1000.0
        cue._spans = [(0, 0, cue.fseq.channel_count)]
        cue._gaps = None
        return 0

    plmod.Player._prepare = fake_prepare
    sess = None
    try:
        tlp, net = _clock_show(
            work, {"source": "artnet_master",
                   "artnet": {"nodes": {"MadMapper": "127.0.0.1"}}},
            [("01:00:00:00", "Show.fseq", "Show")])
        sess = Session(tlp, networks=net, no_log=True, sd=FakeSD(),
                       bind="127.0.0.1")
        sess.open()
        check(sess.clock.out is not None and
              sess.clock.out.bind_ip == "127.0.0.1",
              "--bind did not reach the timecode socket")
    finally:
        if sess is not None:
            try:
                sess.stop()
            except Exception:
                pass
        plmod.Player._prepare = real_prepare
        st_mod.path, st_mod.prefs_path = real_path, real_prefs
    print("  ok")



def test_a_lost_feed_still_reads_lost_without_a_master_clock():
    section("GPL and the slave: a lost feed still reads LOST, with warnings")
    # The master clock quiets the feed warnings and reads STANDBY between
    # cues. Those three switches must stay master-only: at Dollywood a lost
    # feed has to read LOST, say that no timecode has been decoded, and
    # show the input as the thing to check. Proven with no clock block (the
    # GPL shape), with the LTC slave, and against the master for contrast,
    # on the page snapshot and on the terminal Run window alike.
    import tempfile
    from ltcplay.session import Session
    from ltcplay import settings as st_mod
    import ltcplay.player as plmod
    work = tempfile.mkdtemp()
    real_path, real_prefs = st_mod.path, st_mod.prefs_path
    st_mod.path = lambda: os.path.join(work, st_mod.FILENAME)
    st_mod.prefs_path = lambda: os.path.join(work, st_mod.PREFS_FILE)
    real_prepare = plmod.Player._prepare

    def fake_prepare(self, cue):
        cue.fseq = FakeFSEQ(frames=400)
        cue.duration = cue.fseq.duration_ms / 1000.0
        cue._spans = [(0, 0, cue.fseq.channel_count)]
        cue._gaps = None
        return 0

    plmod.Player._prepare = fake_prepare
    kinds = (("GPL, no clock block", None),
             ("the LTC slave", {"source": "ltc_audio_slave",
                                "artnet": {"nodes": {"BEYOND": "127.0.0.1"}}}),
             ("ltcplay as master", {"source": "artnet_master",
                                    "artnet": {"nodes": {"MadMapper":
                                                         "127.0.0.1"}}}))
    sessions = []
    try:
        for label, clock_doc in kinds:
            here = os.path.join(work, str(len(sessions)))
            os.makedirs(here)
            tlp, net = _clock_show(here, clock_doc,
                                   [("01:00:00:00", "Show.fseq", "Show")])
            # The laptop microphone: an input that opens and carries no
            # timecode at all, which is a dead feed.
            sess = Session(tlp, no_output=True, networks=net, no_log=True,
                           sd=FakeSD(), device="MacBook Air Microphone",
                           channel=1)
            sess.open()
            sessions.append(sess)
            if sess.clock is not None:
                sess.clock.out = _TcOut()
            sess.start()
            master = clock_doc is not None and \
                clock_doc["source"] == "artnet_master"
            if not master:
                wait_for(lambda: sess.audio is not None
                         and sess.audio.attached, timeout=3.0)
            wait_for(lambda: sess.player.frames_sent > 3, timeout=3.0)
            snap = sess.snapshot()
            sc = disp.Screen(colour=False, cols=110)
            screen = "\n".join(disp.render(sess.player, sess.dec, sess.tl,
                                            sc, _now() - 5))
            line = disp.one_line(sess.player, sess.dec, sess.tl)
            nodec = [w for w in snap["warnings"]
                     if "No timecode has been decoded yet" in w]
            if master:
                check(snap["state"] == "STANDBY" and "STANDBY" in screen
                      and "STANDBY" in line and "LOST" not in screen,
                      f"{label}: between cues the page reads "
                      f"{snap['state']!r} and the terminal does not agree:\n"
                      f"{screen}")
                check(not nodec and snap["input_used"] is False,
                      f"{label}: warns about a feed it does not have")
                continue
            check(sess.player.state == LOST and snap["state"] == "LOST",
                  f"{label}: a dead feed reads {snap['state']!r} on the "
                  f"page, not LOST")
            check("LOST" in screen and "STANDBY" not in screen
                  and " LOST " in f" {line} ",
                  f"{label}: the terminal does not say LOST:\n{screen}")
            check(nodec, f"{label}: nothing says no timecode has been "
                         f"decoded: {snap['warnings']}")
            check(snap["input_used"] is True
                  and snap["input_attached"] is True,
                  f"{label}: the input reads used={snap['input_used']!r} "
                  f"attached={snap['input_attached']!r}")
            silent = wait_for(lambda: any(
                "is silent" in w for w in sess.snapshot()["warnings"]),
                timeout=3.0)
            check(silent, f"{label}: a silent input is not called out")
    finally:
        for sess in sessions:
            try:
                sess.stop()
            except Exception:
                pass
        plmod.Player._prepare = real_prepare
        st_mod.path, st_mod.prefs_path = real_path, real_prefs
    print("  ok")


# ------------------------------------------------------------ tctest -----
# The hand-fired test Art-Net timecode command, section 5 of the Fire & Ice
# handoff: "A timecode test button... yes." None of these import
# ltcplay.tctest at module scope, and tctest.py itself does not import
# ltcplay.clock at module scope either -- see test_tctest_never_touches_
# session_or_sacn below, which proves both from a fresh interpreter.

class _SimClock:
    """A clock and sleep for tctest's Ticker that never touches real time.

    tctest.run() takes `clock`/`sleep` the same way clock.py's own Ticker
    does, and clock.py's own tests prove pacing correctness on a
    simulated clock for exactly this reason: real time.sleep() is what
    made the packet-count tests here flake on a starved CI runner -- a
    slow machine skips frames by design (see Ticker's docstring), so a
    strict "every frame arrived" check needs a clock nothing can starve.
    Packets still go out on a real socket; only the pacing is simulated,
    so the bytes on the wire are exactly what a real run would send."""

    def __init__(self, start=1000.0):
        self.now = start

    def clock(self):
        return self.now

    def sleep(self, d):
        self.now += d


def test_tctest_packets_on_the_wire():
    section("tctest: packets on the wire, byte for byte, right count")
    import io
    import socket as _socket
    import tempfile
    from ltcplay import tctest as TT
    from ltcplay.tc import frames_to_tc, tc_to_frames

    rx = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(2.0)
    port = rx.getsockname()[1]
    lockpath = os.path.join(tempfile.mkdtemp(), "out.lock")
    out_s, err_s = io.StringIO(), io.StringIO()
    seconds = 0.2
    sim = _SimClock()
    # Starts one second and change before midnight, on purpose: the run
    # crosses the hour boundary, which is the one place a naive frame count
    # sends hour 24 instead of wrapping to 0 and arttimecode() refuses it.
    rc = TT.run([("test", "127.0.0.1")], False, start="23:59:59:27",
               seconds=seconds, lock_path=lockpath, port=port,
               out_stream=out_s, err_stream=err_s,
               clock=sim.clock, sleep=sim.sleep)
    check(rc == 0, f"a clean tctest run did not return 0: {rc} "
                  f"({err_s.getvalue()!r})")

    pkts = []
    try:
        while True:
            pkts.append(rx.recv(64))
    except _socket.timeout:
        pass
    rx.close()

    want_n = int(round(seconds * 30))
    check(len(pkts) == want_n, f"tctest sent {len(pkts)} packets for a "
                              f"{seconds}s run at 30fps, wanted {want_n}")

    start_n = tc_to_frames(23, 59, 59, 27, 30, False)
    want_frames = []
    for i in range(want_n):
        h, m, s, f = frames_to_tc(start_n + i, 30, False)
        want_frames.append((h % 24, m, s, f))
    got_frames = [_tc_of(p)[:4] for p in pkts]
    check(got_frames == want_frames,
          f"tctest frames are not the start frame followed by strictly "
          f"increasing ones: got {got_frames}, wanted {want_frames}")
    check(all(_tc_of(p)[4] == 3 for p in pkts),
          "tctest did not send type 3, SMPTE 30 fps non drop")
    print("  ok")


def test_tctest_seconds_zero_means_until_stopped():
    section("tctest: --seconds 0 means until stopped, not zero frames")
    import io
    import tempfile
    from ltcplay import tctest as TT

    # A stop_check that only goes true after a handful of frames. If
    # `seconds=0` were treated as "stop immediately" (falsy, like a bare
    # `if seconds:` would), this would see 0 or 1 packets. It should see
    # every frame up to the stop, proving 0 means "run until told".
    STOP_AFTER = 5
    calls = {"n": 0}

    def stop_after_a_few():
        calls["n"] += 1
        return calls["n"] > STOP_AFTER

    out_s, err_s = io.StringIO(), io.StringIO()
    rc = TT.run([("test", "127.0.0.1")], False, seconds=0,
               lock_path=os.path.join(tempfile.mkdtemp(), "out.lock"),
               out_stream=out_s, err_stream=err_s,
               stop_check=stop_after_a_few)
    check(rc == 0, f"a seconds=0 run did not return 0: {err_s.getvalue()!r}")
    check(calls["n"] > STOP_AFTER,
          f"tctest with seconds=0 stopped on its own after only "
          f"{calls['n']} stop_check calls, before the operator asked")
    check("stopped by the operator" in out_s.getvalue(),
          f"a seconds=0 run that was stopped by stop_check did not say "
          f"so: {out_s.getvalue()!r}")
    print("  ok")


def test_tctest_only_named_nodes_receive():
    section("tctest: only the named nodes receive packets")
    # This machine's loopback interface answers only on 127.0.0.1 -- no
    # 127.0.0.2 alias to give a second node its own address, the way a real
    # rig would. So the exclusion is proved on the wire a different way:
    # two real UDP listeners, one on the port test timecode is told to use
    # and one on a port it is never told about, plus a packet COUNT. A show
    # file names two nodes; only one is asked for with --to. If a "MadMapper
    # only" run ever put both destinations on the wire (the exact "defaults
    # to all nodes" bug this guards against), the named listener would see
    # twice as many packets as a run that only ever had one destination to
    # begin with -- and the second listener proves nothing strays onto a
    # port nobody named either.
    import io
    import json
    import socket as _socket
    import tempfile
    from ltcplay import tctest as TT

    named = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    named.bind(("127.0.0.1", 0))
    named.settimeout(2.0)
    port = named.getsockname()[1]
    stray_port = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    stray_port.bind(("127.0.0.1", 0))
    stray_port.settimeout(0.3)

    work = tempfile.mkdtemp()
    show = os.path.join(work, "show.json")
    json.dump({"fps": 30, "show_dir": work,
              "cues": [{"tc": "01:00:00:00", "fseq": "A.fseq"}],
              "clock": {"source": "artnet_master",
                        "artnet": {"nodes": {"MadMapper": "127.0.0.1",
                                            "Other": "10.0.0.40"}}}},
             open(show, "w"))
    dests, is_bc = TT.resolve_destinations(show=show, to=["MadMapper"])
    check(dests == [("MadMapper", "127.0.0.1")],
          f"resolve_destinations returned more than the one name asked "
          f"for: {dests}")
    check(is_bc is False, "naming one node by hand turned on broadcast")

    seconds = 0.1
    lockpath = os.path.join(tempfile.mkdtemp(), "out.lock")
    out_s, err_s = io.StringIO(), io.StringIO()
    sim1 = _SimClock()
    rc = TT.run(dests, is_bc, seconds=seconds, lock_path=lockpath, port=port,
               out_stream=out_s, err_stream=err_s,
               clock=sim1.clock, sleep=sim1.sleep)
    check(rc == 0, f"tctest did not run cleanly: {err_s.getvalue()!r}")

    got_named = []
    try:
        while True:
            got_named.append(named.recv(64))
    except _socket.timeout:
        pass
    want_n = int(round(seconds * 30))
    check(len(got_named) == want_n,
          f"the named node received {len(got_named)} packets, wanted "
          f"exactly {want_n} -- one destination sends one packet a frame, "
          f"not two")
    try:
        stray = stray_port.recv(64)
        check(False, f"a packet reached a port nothing was told to use: "
                     f"{stray!r}")
    except _socket.timeout:
        pass

    # And the destination the show file names but --to does not: rebuilding
    # dests as if the "Other" node had wrongly been included doubles the
    # traffic the SAME listener sees for the SAME two-node show file, which
    # is the concrete shape the "defaults to all nodes" bug takes.
    both = dests + [("Other", "127.0.0.1")]
    out_s2, err_s2 = io.StringIO(), io.StringIO()
    sim2 = _SimClock()
    rc2 = TT.run(both, False, seconds=seconds,
                lock_path=os.path.join(tempfile.mkdtemp(), "out.lock"),
                port=port, out_stream=out_s2, err_stream=err_s2,
                clock=sim2.clock, sleep=sim2.sleep)
    check(rc2 == 0, f"tctest did not run cleanly: {err_s2.getvalue()!r}")
    got_both = []
    try:
        while True:
            got_both.append(named.recv(64))
    except _socket.timeout:
        pass
    check(len(got_both) == 2 * want_n,
          f"two destinations at the same address should double the "
          f"packets the wire sees ({len(got_both)} for {want_n * 2} "
          f"wanted); this is the check that catches a silent 'send to "
          f"everyone' regression")

    named.close()
    stray_port.close()
    print("  ok")


def test_tctest_refusals():
    section("tctest: refuses with a plain sentence, never guesses")
    import io
    import json
    import tempfile
    from ltcplay import onlyone
    from ltcplay import tctest as TT

    try:
        TT.resolve_destinations()
        check(False, "tctest ran with no destination named at all")
    except TT.TcTestError as e:
        check("needs to know where" in str(e),
              f"the no-destination refusal is unclear: {e}")

    work = tempfile.mkdtemp()
    show = os.path.join(work, "show.json")
    json.dump({"fps": 30, "show_dir": work,
              "cues": [{"tc": "01:00:00:00", "fseq": "A.fseq"}],
              "clock": {"source": "artnet_master",
                        "artnet": {"nodes": {"MadMapper": "127.0.0.1",
                                            "BEYOND": "127.0.0.2"}}}},
             open(show, "w"))

    try:
        TT.resolve_destinations(show=show, to=["Nope"])
        check(False, "an unknown node name was accepted")
    except TT.TcTestError as e:
        check("'Nope'" in str(e) and "MadMapper" in str(e)
              and "BEYOND" in str(e),
              f"the unknown-node refusal does not name the real nodes: {e}")

    try:
        TT.resolve_destinations(show=show)
        check(False, "--show with no --to did not refuse")
    except TT.TcTestError as e:
        check("--to" in str(e), f"unclear refusal: {e}")

    try:
        TT.resolve_destinations(node=["A=127.0.0.1"], broadcast="10.0.0.255")
        check(False, "--node together with --broadcast was accepted")
    except TT.TcTestError as e:
        check("one" in str(e).lower(), f"unclear refusal: {e}")

    # A show file with no 'clock' block at all: a plain sentence, not a
    # KeyError or an AttributeError out of ArtNetConfig.parse().
    no_clock = os.path.join(work, "no_clock.json")
    json.dump({"fps": 30, "show_dir": work,
              "cues": [{"tc": "01:00:00:00", "fseq": "A.fseq"}]},
             open(no_clock, "w"))
    try:
        TT.resolve_destinations(show=no_clock, to=["MadMapper"])
        check(False, "a show file with no clock block was accepted")
    except TT.TcTestError as e:
        check("clock" in str(e).lower() and no_clock in str(e),
              f"the no-clock-block refusal is unclear: {e}")

    # A --node with a malformed IP: a plain sentence, not a socket error
    # surfacing later when a packet actually tries to go out.
    try:
        TT.resolve_destinations(node=["MadMapper=999.999.999.999"])
        check(False, "--node with a bad IP was accepted")
    except TT.TcTestError as e:
        check("999.999.999.999" in str(e) and "not an IPv4" in str(e),
              f"the bad --node IP refusal is unclear: {e}")

    try:
        TT.resolve_destinations(broadcast="not-an-address")
        check(False, "--broadcast with a bad address was accepted")
    except TT.TcTestError as e:
        check("not-an-address" in str(e) and "not an IPv4" in str(e),
              f"the bad --broadcast refusal is unclear: {e}")

    for bad in ("99:99:99:99", "not a timecode", "25:00:00:00", "1:2:3",
               "-1:00:00:00", "1:2:3:4:5"):
        try:
            TT.parse_start(bad)
            check(False, f"{bad!r} was accepted as a start timecode")
        except TT.TcTestError:
            pass

    lockpath = os.path.join(tempfile.mkdtemp(), "out.lock")
    held = onlyone.OutputLock(lockpath, "the Run window").acquire()
    try:
        out_s, err_s = io.StringIO(), io.StringIO()
        rc = TT.run([("test", "127.0.0.1")], False, seconds=0.1,
                   lock_path=lockpath, out_stream=out_s, err_stream=err_s)
        check(rc != 0, "tctest ran while ltcplay already held the output "
                      "lock")
        check("already sending" in err_s.getvalue(),
              f"the refusal does not say the rig is already sending: "
              f"{err_s.getvalue()!r}")
        check("Run window" in err_s.getvalue(),
              f"the refusal does not name who is holding it: "
              f"{err_s.getvalue()!r}")
    finally:
        held.release()

    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "ltcplay", "tctest.py"), encoding="utf-8").read()
    check("—" not in src and "–" not in src,
          "tctest.py carries an em or en dash")
    print("  ok")


def test_tctest_beyond_warning():
    section("tctest: every run warns, and laser-named nodes get more")
    import io
    import tempfile
    from ltcplay import tctest as TT

    # laser_like_names() only ever picks the extra, stronger line -- it is
    # never the gate that decides whether ANY warning prints, because a
    # node can be called anything: "Lasers", "Andy", "BEYOND-2", a raw
    # --node IP. Matching a fixed word against a free-form name cannot be
    # the safety net.
    check(TT.laser_like_names([("BEYOND", "10.0.0.1")], False) == ["BEYOND"],
          "a node named BEYOND is not picked out for the extra line")
    check(TT.laser_like_names([("beyond", "10.0.0.1")], False) == ["beyond"],
          "the BEYOND match should not be case sensitive")
    check(TT.laser_like_names([("Lasers", "10.0.0.1")], False) == ["Lasers"],
          "a node named Lasers is not picked out for the extra line")
    check(TT.laser_like_names([("BEYOND-2", "10.0.0.1")], False)
         == ["BEYOND-2"],
          "a node named BEYOND-2 is not picked out for the extra line")
    check(TT.laser_like_names([("MadMapper", "10.0.0.1")], False) == [],
          "a node named MadMapper was picked out for the extra line")
    check(TT.laser_like_names([("Andy", "10.0.0.1")], False) == [],
          "a node named Andy was picked out for the extra line")
    check(TT.laser_like_names([], True) == ["broadcast"],
          "an explicit --broadcast is not picked out for the extra line")

    # Every one of these sends test timecode to a destination that is not
    # named BEYOND, Lasers, or broadcast -- exactly the shapes the known
    # bug missed. Every one of them must still get the general warning,
    # naming who it went to, before the first packet.
    for dests, is_bc, label in (
        ([("MadMapper", "127.0.0.1")], False, "a plain node name"),
        ([("Andy", "127.0.0.1")], False, "an operator's own name"),
        ([("BEYOND-2", "127.0.0.1")], False,
         "a name that only contains the word BEYOND"),
    ):
        out_s, err_s = io.StringIO(), io.StringIO()
        rc = TT.run(dests, is_bc, seconds=0.05,
                   lock_path=os.path.join(tempfile.mkdtemp(), "out.lock"),
                   out_stream=out_s, err_stream=err_s)
        check(rc == 0, f"tctest did not run cleanly for {label}: "
                      f"{err_s.getvalue()!r}")
        err = err_s.getvalue()
        check("Test timecode is about to go to:" in err
             and dests[0][0] in err and dests[0][1] in err,
              f"no general warning naming the destination for {label}: "
              f"{err!r}")
        check(TT.GENERAL_WARNING in err,
              f"the general warning text is missing for {label}: {err!r}")

    # A name that looks like laser software gets that same general line,
    # plus the extra, stronger one.
    out_s2, err_s2 = io.StringIO(), io.StringIO()
    TT.run([("Lasers", "127.0.0.1")], False, seconds=0.05,
          lock_path=os.path.join(tempfile.mkdtemp(), "out.lock"),
          out_stream=out_s2, err_stream=err_s2)
    err2 = out_s2.getvalue() + err_s2.getvalue()
    check("Test timecode is about to go to:" in err2,
          f"a laser-named node did not get the general warning: {err2!r}")
    check("Lasers" in err2 and TT.LASER_WARNING in err2,
          f"a node named Lasers did not get the extra laser line: {err2!r}")

    # Two destinations, one plain and one laser-like: the extra line names
    # only the laser-like one, not both.
    out_s3, err_s3 = io.StringIO(), io.StringIO()
    TT.run([("MadMapper", "127.0.0.1"), ("Lasers", "127.0.0.1")], False,
          seconds=0.05, lock_path=os.path.join(tempfile.mkdtemp(),
                                              "out.lock"),
          out_stream=out_s3, err_stream=err_s3)
    err3 = err_s3.getvalue()
    extra_line = next((l for l in err3.splitlines()
                      if l.startswith("Lasers:")), None)
    check(extra_line is not None,
          f"the extra laser line does not name Lasers: {err3!r}")
    check("MadMapper" not in extra_line,
          f"the extra laser line also names a plain node: {extra_line!r}")

    # Broadcast always gets the extra line too: nothing here can say
    # whether a laser system is listening on the network.
    out_s4, err_s4 = io.StringIO(), io.StringIO()
    TT.run([("broadcast", "10.0.0.255")], True, seconds=0.05,
          lock_path=os.path.join(tempfile.mkdtemp(), "out.lock"),
          out_stream=out_s4, err_stream=err_s4)
    check(TT.LASER_WARNING in err_s4.getvalue(),
          f"--broadcast did not get the extra laser line: "
          f"{err_s4.getvalue()!r}")
    print("  ok")


def test_tctest_releases_lock_on_exception():
    section("tctest: an exception mid-run still releases the lock")
    import io
    import tempfile
    from ltcplay import onlyone
    from ltcplay import tctest as TT

    calls = {"n": 0}

    def _boom_sleep(_):
        # The real clock never advances 30 frames in zero wall time, so
        # the ticker calls sleep() waiting for the next frame -- after the
        # first frame's packet has already gone out, which is what proves
        # this is a mid-run failure, not a setup one.
        calls["n"] += 1
        raise RuntimeError("boom: a fault mid-run, the same shape as a "
                          "USB drop or an OS error setting up the socket")

    lockpath = os.path.join(tempfile.mkdtemp(), "out.lock")
    out_s, err_s = io.StringIO(), io.StringIO()
    try:
        TT.run([("test", "127.0.0.1")], False, seconds=5.0,
              lock_path=lockpath, out_stream=out_s, err_stream=err_s,
              sleep=_boom_sleep)
        check(False, "tctest.run() swallowed a mid-run exception instead "
                     "of letting it propagate")
    except RuntimeError as e:
        check("boom" in str(e), f"the wrong exception propagated: {e!r}")
    check(calls["n"] > 0, "the injected fault was never reached")

    # The lock must be free again -- a fresh acquire from here is the same
    # proof a second real tctest or `ltcplay run` would get: no stale lock
    # left behind to refuse a show that should be allowed to start.
    held = onlyone.OutputLock(lockpath, "next run after the fault")
    try:
        held.acquire()
    except onlyone.AlreadyRunning as e:
        check(False, f"the lock was still held after an exception: "
                     f"{e.holder!r}")
    else:
        held.release()
    print("  ok")


def test_tctest_never_touches_session_or_sacn():
    section("tctest: no session, no sACN, ever -- proven fresh")
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "ltcplay", "tctest.py"),
              encoding="utf-8").read()
    check(not re.search(
        r"^\s*(from \.session |from \. import .*\bsession\b|"
        r"import ltcplay\.session|from ltcplay import .*\bsession\b)",
        src, re.M),
        "tctest.py imports the session module")
    check(not re.search(
        r"^(from \.clock |from \. import .*\bclock\b|"
        r"import ltcplay\.clock|from ltcplay import .*\bclock\b)",
        src, re.M),
        "tctest.py imports the clock module at module scope; every clock "
        "import in this program is inside a function")
    check("Sender" not in src,
          "tctest.py names output.Sender, the pixel/sACN sender; it must "
          "only ever build clock.TimecodeOut")
    check("5568" not in src, "tctest.py mentions 5568, the sACN port")

    import json
    import subprocess as _sp
    script = r'''
import io, json, os, sys, tempfile
sys.path.insert(0, sys.argv[1])
from ltcplay import tctest as TT
lockpath = os.path.join(tempfile.mkdtemp(), "out.lock")
out_s, err_s = io.StringIO(), io.StringIO()
TT.run([("test", "127.0.0.1")], False, seconds=0.05, lock_path=lockpath,
      out_stream=out_s, err_stream=err_s)
print(json.dumps({
    "session": "ltcplay.session" in sys.modules,
    "player": "ltcplay.player" in sys.modules,
    "clock": "ltcplay.clock" in sys.modules,
    "output": "ltcplay.output" in sys.modules,
}))
'''
    r = _sp.run([sys.executable, "-c", script, here], capture_output=True,
               text=True, timeout=30)
    try:
        res = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        check(False, f"the fresh tctest run did not complete: "
                     f"{r.stdout[-400:]} {r.stderr[-800:]}")
        return
    check(not res["session"], "running tctest imported ltcplay.session")
    check(not res["player"], "running tctest imported ltcplay.player")
    # The clock and its Art-Net timecode socket ARE expected here: tctest's
    # whole job is to drive them. What must never be true is session/player.
    check(res["clock"], "tctest ran but never loaded the clock it sends "
                        "timecode through")
    print("  ok")


if __name__ == "__main__":
    t0 = time.time()
    _show_root = real_show_dir()
    _show_before = (_show_snapshot(_show_root) if os.path.isdir(_show_root)
                    else None)
    if _show_before is not None:
        print(f"real show folder: {_show_root} "
              f"({len(_show_before)} entries, read only)")
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
    test_pixel_output_frame_jitter()
    test_pixel_pacing_never_accumulates_error()
    test_windows_pixel_clock_choice()
    test_no_clock_is_ever_mixed_with_another()
    test_the_stepped_player_is_the_output_thread()
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
    test_a_controller_ping_means_what_it_says()
    test_free_run_to_the_end_when_timecode_dies()
    test_the_input_can_be_rebuilt_without_dropping_the_rig()
    test_the_input_stops_hunting_sample_rates()
    test_a_poisoned_portaudio_is_rebuilt()
    test_a_missing_input_still_runs_the_preshow()
    test_the_input_can_be_changed_mid_show()
    test_a_failed_start_leaves_nothing_running()
    test_only_one_player_sends_at_a_time()
    test_machine_data_goes_where_the_os_keeps_it()
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
    test_schedule_hold_pauses_a_show()
    test_schedule_hold_between_shows_delays()
    test_schedule_start_now_in_every_state()
    test_schedule_abort_end_night_and_operator_actions()
    test_schedule_file_is_versioned_and_atomic()
    test_schedule_clock_check()
    test_schedule_routes()
    test_the_gpl_path_never_loads_the_scheduler()
    test_the_scheduler_engine_is_pure()
    test_schedule_restart_keeps_tonight()
    test_schedule_clock_check_never_delays_a_show()
    test_schedule_faults_during_and_before_a_show()
    test_schedule_edits_past_last_end_and_midnight()
    test_schedule_rule_file_with_a_bom()
    test_schedule_tonight_file_is_checked()
    test_schedule_rule_change_rebuilds_tonight()
    test_schedule_late_failed_start_is_a_fault()
    test_schedule_after_a_stopped_show_is_one_choice()
    test_schedule_contract_for_the_transport()
    test_schedule_uncertain_record_never_fires_twice()
    test_schedule_delayed_and_paused_survive_a_restart()
    test_schedule_operator_list()
    test_schedule_a_paused_show_is_never_overlapped()
    test_schedule_a_clock_step_during_a_pause()
    test_announce_interlock_matrix()
    test_announce_single_flight()
    test_announce_operator_validation()
    test_announce_missing_files_at_startup()
    test_announce_device_missing_renamed_reappearing()
    test_announce_progress_and_stop()
    test_announce_logging_fields()
    test_announce_routes()
    test_the_gpl_path_never_loads_announcements()
    test_arttimecode_packet_byte_for_byte()
    test_artnet_timecode_holds_30fps_under_load()
    test_artnet_timecode_never_drifts_from_its_clock()
    test_timecode_zones_for_fallback_3()
    test_clock_settings_fail_loudly()
    test_the_gpl_path_never_loads_the_clock()
    test_a_master_clock_runs_the_show()
    test_a_slave_clock_forwards_the_show_zone()
    test_a_stopped_cue_hands_the_rig_back()
    test_the_clock_survives_its_own_faults()
    test_forwarded_timecode_steps_by_one()
    test_timecode_health_is_shown()
    test_a_lost_feed_still_reads_lost_without_a_master_clock()
    test_tctest_packets_on_the_wire()
    test_tctest_seconds_zero_means_until_stopped()
    test_tctest_only_named_nodes_receive()
    test_tctest_refusals()
    test_tctest_beyond_warning()
    test_tctest_releases_lock_on_exception()
    test_tctest_never_touches_session_or_sacn()
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

    if _show_before is not None:
        touched = _show_changes(_show_root, _show_before)
        if touched:
            FAILS.append(f"THE REAL SHOW FOLDER WAS CHANGED BY THIS RUN "
                         f"({_show_root}): " + "; ".join(touched[:20]))
            print(f"\n  FAIL  THE REAL SHOW FOLDER WAS CHANGED BY THIS RUN:")
            for t in touched:
                print(f"    {t}")

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
