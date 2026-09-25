"""Synthetic show fixtures for selftest.py.

Nine of selftest's checks (the web page, verify, the bundle, reload, the `at`
timecode lookup, the read-only-folder guard, and a couple more) need "a real
show folder": FSEQ renders plus an xlights_networks.xml. The real one is the
GPL 2026 Dollywood show, 600MB, and CLAUDE.md says never commit show renders
or media. So those tests read it from LTCPLAY_TEST_SHOW_DIR and skip when it
is not set, which in CI is always -- CI never proves any of the mutations
those nine tests are the only thing that catches.

This module builds a tiny, generated stand-in instead: a handful of FSEQ
files (seconds long, dozens of channels, not hundreds of thousands) plus a
matching xlights_networks.xml and xlights_rgbeffects.xml, written fresh into
a temp folder every time selftest runs. Nothing here is ever written into the
repo, so there is nothing for the "never commit show renders" rule to catch --
these bytes do not exist until a test asks for them, and they are gone again
when the OS cleans up its temp folder.

Deliberately at the top level, next to selftest.py, and NOT inside the
ltcplay/ package: ltcplay/version.py hashes every file under ltcplay/ into
the program's build id (see its `_files()`), because that hash is meant to
answer "are two machines running the same PROGRAM". This file has nothing to
do with what the program does, only with how it is tested, so it stays out
of that hash the same way selftest.py's own test bodies do.

The FSEQ v2 layout written here is exactly the one ltcplay/fseq.py reads,
documented at the top of that file from the xLights source
(src-core/render/FSEQFile.cpp). Every byte is generated; nothing here is
copied from a real render.
"""
import os
import struct
import tempfile
import zlib

MAGIC = b"PSEQ"
_COMP_NONE, _COMP_ZLIB = 0, 2


def _u16(v):
    return struct.pack("<H", v)


def _u24(v):
    return bytes((v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF))


def _u32(v):
    return struct.pack("<I", v)


def _var_header(code, text):
    """One xLights "variable header": length-prefixed, a 2-letter code, then
    a nul-terminated string. fseq.py's _read_variables() is the reader."""
    body = text.encode("utf-8") + b"\x00"
    length = 4 + len(body)
    return _u16(length) + code.encode("ascii") + body


def write_fseq(path, *, frame_count, channel_count=None, step_ms=25,
               compression="zlib", block_frames=None, sparse_ranges=None,
               media_file=None, renderer="xLights Testbench 2026.1",
               fill=None):
    """Write one small, valid FSEQ v2 file. Returns `path`.

    sparse_ranges: [(start0, length), ...], absolute 0-based channel spans,
      the same shape ltcplay/fseq.py's `sparse_ranges` and cli.py's verify
      read back. channel_count is their total when sparse_ranges is given;
      otherwise one range covering the whole requested width is used, which
      is what a non-sparse render looks like to every reader in this program.

    fill: a byte value, or a callable frame_index -> byte value, used to
      fill every channel of every frame (every channel gets the same byte
      within one frame -- the frame index is what varies). Two fixtures
      given different `fill` functions are guaranteed to differ in content,
      which is what the reload test and "two renders that are byte-identical
      prove nothing" check for. Default: the frame's own index -- the same
      trick selftest.py's own FakeFSEQ uses, so a wrong frame is detectable
      from the bytes alone.

    compression: "none" (one uncompressed block spanning every frame, the
      shape a v1 render or an unsparsed v2 one takes) or "zlib" (several
      compressed blocks, one per `block_frames` frames). Never "zstd": that
      decoder is an optional dependency of the real program (see
      ltcplay/fseq.py, README.md), and a generated fixture has no reason to
      need it when the format documents a second, stdlib-only compression
      that reads back exactly the same way.
    """
    if frame_count <= 0:
        raise ValueError("frame_count must be positive")
    if sparse_ranges is None:
        if channel_count is None:
            channel_count = 32
        sparse_ranges = [(0, channel_count)]
    else:
        channel_count = sum(ln for _, ln in sparse_ranges)
        if channel_count <= 0:
            raise ValueError("sparse_ranges must cover at least one channel")

    if fill is None:
        fill_fn = lambda i: i & 0xFF
    elif callable(fill):
        fill_fn = fill
    else:
        _v = fill & 0xFF
        fill_fn = lambda i: _v

    def frame_bytes(i):
        return bytes((fill_fn(i) & 0xFF,)) * channel_count

    if compression not in ("none", "zlib"):
        raise ValueError(f"unsupported compression {compression!r}")

    blocks = []  # [(first_frame, payload_bytes), ...]
    if compression == "none":
        body = b"".join(frame_bytes(i) for i in range(frame_count))
    else:
        bf = block_frames or max(1, frame_count // 4)
        i = 0
        while i < frame_count:
            n = min(bf, frame_count - i)
            raw = b"".join(frame_bytes(f) for f in range(i, i + n))
            blocks.append((i, zlib.compress(raw, 6)))
            i += n
        body = b"".join(data for _, data in blocks)

    n_blocks = len(blocks)
    n_sparse = len(sparse_ranges)
    tbl = b"".join(_u32(fb) + _u32(len(data)) for fb, data in blocks)
    sparse_bytes = b"".join(_u24(s) + _u24(ln) for s, ln in sparse_ranges)
    var_bytes = b""
    if media_file:
        var_bytes += _var_header("mf", media_file)
    if renderer:
        var_bytes += _var_header("sp", renderer)

    chan_data_offset = 32 + len(tbl) + len(sparse_bytes) + len(var_bytes)
    if chan_data_offset > 0xFFFF or n_blocks > 0xFFF or n_sparse > 0xFF:
        # None of the fixtures this module builds come close; this is a
        # guard against a future caller asking for something the v2 header's
        # fixed-width fields cannot hold.
        raise ValueError("fixture too large for the FSEQ v2 fixed header")

    head = bytearray(32)
    head[0:4] = MAGIC
    struct.pack_into("<H", head, 4, chan_data_offset)
    head[6] = 2                                  # version minor
    head[7] = 2                                  # version major
    struct.pack_into("<H", head, 8, 32 + len(tbl) + len(sparse_bytes))
    struct.pack_into("<I", head, 10, channel_count)
    struct.pack_into("<I", head, 14, frame_count)
    head[18] = step_ms
    head[19] = 0                                 # flags
    comp_id = _COMP_NONE if compression == "none" else _COMP_ZLIB
    head[20] = (((n_blocks >> 8) & 0x0F) << 4) | (comp_id & 0x0F)
    head[21] = n_blocks & 0xFF
    head[22] = n_sparse & 0xFF
    head[23] = 0                                 # reserved
    head[24:32] = os.urandom(8)                  # uuid

    with open(path, "wb") as fh:
        fh.write(bytes(head))
        fh.write(tbl)
        fh.write(sparse_bytes)
        fh.write(var_bytes)
        fh.write(body)
    return path


# ---------------------------------------------------------------- show ----

_NETWORKS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Networks computer="fixture" GlobalForceLocalIP="127.0.0.1">
  <Controller Id="1" Name="Fixture Controller 1" Type="Ethernet" IP="127.0.0.1" ActiveState="Active">
    <network NetworkType="ArtNET" ComPort="127.0.0.1" BaudRate="1" MaxChannels="510" Enabled="Yes" />
  </Controller>
  <Controller Id="2" Name="Fixture Controller 2" Type="Ethernet" IP="127.0.0.1" ActiveState="Active">
    <network NetworkType="ArtNET" ComPort="127.0.0.1" BaudRate="2" MaxChannels="510" Enabled="Yes" />
  </Controller>
</Networks>
"""

# One named prop, at the 1-based channel the "missing" half of MINORITY
# below leaves dark (see synthetic_show_dir). Read by ltcplay/models.py,
# which only cares about <models><model name=.. StartChannel=..>.
_RGBEFFECTS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<xrgb>
  <models>
    <model name="Shade 1" StartChannel="201" DisplayAs="Single Line" />
  </models>
</xrgb>
"""

# The layout three of the four fixture renders agree on: two islands,
# channels 1..100 and 201..300 (1-based, the form a human-readable report
# uses; sparse_ranges itself is 0-based, so that is (0, 100) and (200, 100)).
MAJORITY_LAYOUT = [(0, 100), (200, 100)]
# The odd one out. Shares channels 1..100 with the majority but not
# 201..300, and writes 401..500 instead -- one range the majority has that
# this one does not ("stays dark") and one this one has that the majority
# does not ("today those channels are something else"), so a verify report
# against a mix of these exercises both messages cli.py's layout check can
# print, in one file.
MINORITY_LAYOUT = [(0, 100), (400, 100)]

_cache = {}


def synthetic_show_dir():
    """A small generated show folder, built once per process and reused.

    Stands in for the real GPL 2026 Dollywood show folder wherever a test
    asks for "a real show folder" and LTCPLAY_TEST_SHOW_DIR is not set. The
    file names match the ones the real show uses for the sequences these
    tests actually touch, so a test written against the real folder mostly
    needs no change at all beyond where it gets `show_dir` from -- only the
    content is synthetic.

    Returns the folder's path (created under the OS temp dir; never inside
    this repo, so there is nothing for CLAUDE.md's "never commit show
    renders" rule to catch)."""
    key = "dir"
    cached = _cache.get(key)
    if cached and os.path.isdir(cached):
        return cached

    root = tempfile.mkdtemp(prefix="ltcplay_fixture_show_")

    # Opener: uncompressed (a v1-shaped render), long enough that "30
    # seconds in" lands on a real frame (frame 1200 at 25ms/frame -- what
    # test_at_command_on_the_real_show checks for), and on the MINORITY
    # layout so a timeline that mixes it with the others below has
    # something to disagree about.
    write_fseq(os.path.join(root, "GPL 2026_Set 1_Opener.fseq"),
               frame_count=2000, step_ms=25, compression="none",
               sparse_ranges=MINORITY_LAYOUT, fill=lambda i: i & 0xFF,
               media_file="GPL 2026_Set 1_Opener.mp3")

    # Munsters: compressed, and deliberately 20 EQUAL-sized blocks (800
    # frames / 40) so frame_count//2 -- what FSEQ.verify() actually probes --
    # falls at the exact start of block index n_blocks//2. That is the block
    # test_opening_a_render_proves_it_reads corrupts and calls "the middle
    # one"; if the arithmetic did not line up, corrupting the literal middle
    # BLOCK could miss every frame verify() probes, and the test would be
    # asserting something this fixture never does.
    #
    # 20 seconds, not 8: the same test also plays 5 seconds into a copy of
    # this file after truncating it to 60% of its bytes, and expects that to
    # still work -- which only proves anything if 5 seconds' worth of frames
    # sits well inside the retained 60%. On the real show a render runs
    # minutes and 5 seconds is a sliver of it; this keeps that same shape
    # instead of shrinking it away.
    write_fseq(os.path.join(root, "GPL 2026_Set 1_Munsters.fseq"),
               frame_count=800, step_ms=25, compression="zlib",
               block_frames=40, sparse_ranges=MAJORITY_LAYOUT,
               fill=lambda i: (i + 77) & 0xFF,
               media_file="GPL 2026_Set 1_Munsters.mp3")

    # Ending: on the majority layout, and over 30 seconds (1300 * 25ms) so
    # test_one_sequence_at_two_timecodes' `duration > 30` holds; frame 400
    # is exactly 10s in at this step, which is the other thing it checks.
    write_fseq(os.path.join(root, "GPL 2026_Set 1_Ending.fseq"),
               frame_count=1300, step_ms=25, compression="zlib",
               block_frames=260, sparse_ranges=MAJORITY_LAYOUT,
               fill=lambda i: (i + 133) & 0xFF,
               media_file="GPL 2026_Set 1_Ending.mp3")

    # Ghostbusters: uncompressed, majority layout. test_at_command_on_the_
    # real_show looks it up at a timecode 67.6s after this cue's start, so
    # it has to still be playing then -- on the real show that is nothing
    # (a multi-minute song), so this needs to run past it too.
    write_fseq(os.path.join(root, "GPL 2026_Set 2_Ghostbusters.fseq"),
               frame_count=3600, step_ms=25, compression="none",
               sparse_ranges=MAJORITY_LAYOUT, fill=lambda i: (i + 201) & 0xFF,
               media_file="GPL 2026_Set 2_Ghostbusters.mp3")

    with open(os.path.join(root, "xlights_networks.xml"), "w",
              encoding="utf-8") as fh:
        fh.write(_NETWORKS_XML)
    with open(os.path.join(root, "xlights_rgbeffects.xml"), "w",
              encoding="utf-8") as fh:
        fh.write(_RGBEFFECTS_XML)

    _cache[key] = root
    return root
