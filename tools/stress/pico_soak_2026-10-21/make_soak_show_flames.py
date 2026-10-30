"""Build the 2026-10-20 soak show: 19 mono 24-bit stems to 7 outputs, a 200-universe sACN FSEQ, 420 s."""
import json, os, struct, sys
import numpy as np
import zstandard

OUT = r"C:\Users\VIOSO\Documents\LTC Shows\soak_show"
SECONDS = 420
RATE = 48000
UNIVERSES = 200
UNI = 510
PIXCH = UNIVERSES * UNI       # 102,000 channels = 34,000 RGB pixels
FLAME_CH = 512                # an Inactive "Flames" controller after the pixels
CH = PIXCH + FLAME_CH
STEP_MS = 25
FRAMES = SECONDS * 1000 // STEP_MS   # 16,800
BLOCK = 10
PIX_IP = "127.0.0.5"
FLAME_IP = "127.0.0.9"
FIRE = {401: [], 402: [], 403: []}
GROUPS = [(401, [411,412,413,414,415]), (402, [421,422,423,424,425,426]), (403, [431,432])]
# feed -> output (1-based): 3-3-3-3-3-2-2 across outputs 1..7
OUTS = [1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4, 4, 5, 5, 5, 6, 6, 7, 7]
NAMES = ["Music L", "Music C", "Music R", "Music Atmo L", "Music Atmo R",
         "Lily L", "Lily C", "Lily R", "Jack L", "Jack C", "Jack R",
         "Narrator L", "Narrator C", "Narrator R",
         "SFX L", "SFX C", "SFX R", "SFX Atmo L", "SFX Atmo R"]


def write_wav24(path, k):
    n = SECONDS * RATE
    f = 110.0 * k
    with open(path, "wb") as fh:
        data_len = n * 3
        fh.write(b"RIFF" + struct.pack("<I", 36 + data_len) + b"WAVEfmt ")
        fh.write(struct.pack("<IHHIIHH", 16, 1, 1, RATE, RATE * 3, 3, 24))
        fh.write(b"data" + struct.pack("<I", data_len))
        step = RATE * 10
        for s in range(0, n, step):
            m = min(step, n - s)
            t = (np.arange(s, s + m, dtype=np.float64)) / RATE
            # a quiet tone with a slow 0.25 Hz swell so it is not a flat fill
            x = 0.08 * np.sin(2 * np.pi * f * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 0.25 * t + k))
            v = np.round(x * 8388607).astype(np.int32)
            b = np.empty((m, 3), dtype=np.uint8)
            b[:, 0] = v & 0xFF
            b[:, 1] = (v >> 8) & 0xFF
            b[:, 2] = (v >> 16) & 0xFF
            fh.write(b.tobytes())


def frame(i, base):
    f = ((base + i * 5) & 0xFF).astype(np.uint8)
    rng = np.random.default_rng(i)
    idx = rng.integers(4, CH, CH // 4)
    f[idx] = rng.integers(0, 256, idx.size, dtype=np.uint8)
    f[0:4] = np.frombuffer(struct.pack("<I", i), dtype=np.uint8)
    f[PIXCH:] = 0
    sec = i // 40
    for gi, (safety, fires) in enumerate(GROUPS):
        if (sec + gi * 3) % 10 < 1:           # each group pulses 1 s in every 10 s
            for slot in fires:
                f[PIXCH + slot - 1] = 255
    return f.tobytes()


def write_fseq(path):
    ch = np.arange(CH, dtype=np.int64)
    base = (ch // 3) * 2 + (ch % 3) * 85
    cctx = zstandard.ZstdCompressor(level=3)
    blocks = []
    for first in range(0, FRAMES, BLOCK):
        n = min(BLOCK, FRAMES - first)
        raw = b"".join(frame(i, base) for i in range(first, first + n))
        blocks.append((first, cctx.compress(raw)))
        if first % 2000 == 0:
            print("fseq frame", first, flush=True)
    tbl = b"".join(struct.pack("<II", fb, len(d)) for fb, d in blocks)
    name = b"soak_show 200 universes (synthetic)\x00"
    var = struct.pack("<H", 4 + len(name)) + b"sp" + name
    data_off = 32 + len(tbl) + len(var)
    assert data_off <= 0xFFFF and len(blocks) <= 0xFFF
    head = bytearray(32)
    head[0:4] = b"PSEQ"
    struct.pack_into("<H", head, 4, data_off)
    head[6], head[7] = 0, 2
    struct.pack_into("<H", head, 8, 32 + len(tbl))
    struct.pack_into("<I", head, 10, CH)
    struct.pack_into("<I", head, 14, FRAMES)
    head[18] = STEP_MS
    head[20] = (((len(blocks) >> 8) & 0x0F) << 4) | 1
    head[21] = len(blocks) & 0xFF
    head[24:32] = os.urandom(8)
    with open(path, "wb") as fh:
        fh.write(head); fh.write(tbl); fh.write(var)
        for _, d in blocks:
            fh.write(d)


def write_networks(path):
    rows = [f'    <network NetworkType="E131" ComPort="{PIX_IP}" BaudRate="{u}" '
            f'MaxChannels="{UNI}" Enabled="Yes" />' for u in range(1, UNIVERSES + 1)]
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           f'<Networks computer="soak" GlobalForceLocalIP="127.0.0.1">\n'
           f'  <Controller Id="1" Name="Soak pixels" Type="Ethernet" IP="{PIX_IP}" ActiveState="Active">\n'
           + "\n".join(rows) + "\n  </Controller>\n</Networks>\n")
    open(path, "w", encoding="utf-8").write(xml)


def write_show(path):
    stems = [{"file": f"audio/Feed_{k:02d}_{NAMES[k-1].replace(' ', '_')}.wav",
              "gain_db": -6, "channels": [OUTS[k - 1]]} for k in range(1, 20)]
    doc = {"name": "Soak show: 19 audio feeds, 6 video, 4 lasers, 200 universes sACN",
           "fps": 30, "show_dir": ".", "on_lost": "freerun",
           "cues": [{"tc": "00:00:00:00", "fseq": "soak_show_flames.fseq", "name": "Show"}],
           "clock": {"source": "audio_master",
                     "artnet": {"nodes": {"MadMapper": "127.0.0.1", "BEYOND": "127.0.0.2"}},
                     "audio": {"device": "StudioLive Series III ASIO", "rate": 48000, "channels": 7,
                               "cues": {"show": {"cue": "Show", "stems": stems}}}},
           "madmapper": {"host": "127.0.0.1", "port": 8000, "show_bank": "Bank-1",
                         "surfaces": [f"Quad-{i}" for i in range(1, 7)],
                         "heartbeat": {"bind": "127.0.0.1", "port": 9001, "address": "/float-1",
                                       "show_len_s": float(SECONDS)}}}
    json.dump(doc, open(path, "w", encoding="utf-8"), indent=2)


if __name__ == "__main__":
    os.makedirs(os.path.join(OUT, "audio"), exist_ok=True)
    what = sys.argv[1:] or ["audio", "fseq", "json"]
    if "json" in what:
        write_networks(os.path.join(OUT, "xlights_networks.xml"))
        write_show(os.path.join(OUT, "soak_show_flames.json"))
        print("wrote networks + show json", flush=True)
    if "audio" in what:
        for k in range(1, 20):
            p = os.path.join(OUT, "audio", f"Feed_{k:02d}_{NAMES[k-1].replace(' ', '_')}.wav")
            write_wav24(p, k)
            print("wav", k, flush=True)
    if "fseq" in what:
        write_fseq(os.path.join(OUT, "soak_show_flames.fseq"))
        print("fseq done", os.path.getsize(os.path.join(OUT, "soak_show_flames.fseq")), flush=True)
