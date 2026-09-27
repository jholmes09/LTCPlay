"""The show audio for the audio_master clock: ltcplay plays the show's
multi-track audio itself, and the timecode is read off the audio device.

Handoff section 4a (Jeff, 2026-09-27). Imported only when a show file's clock
block says "source": "audio_master"; clock.py imports it inside the functions
that need it. The GPL show (LTC chase) and artnet_master never load a line of
it, and selftest proves that in a fresh interpreter.

Four parts:

  config       the "clock.audio" block, checked when the show file loads,
               and the WAV files it names, checked when the session opens.
               Every refusal is a sentence.
  the mixer    pre-decoded stems, each with its gain and output channels,
               summed into one float32 buffer, with fades. Pure: no device,
               no clock, no thread. The tests drive it block by block.
  the device   open_output_stream(), the one function that decides how the
               audio reaches the interface: never through Windows' shared
               mixer (bench B18).
  the process  the audio runs in its own OS process (multiprocessing,
               spawn), so the web page and the pixel loop can never starve
               it of the interpreter lock (bench B11). AudioProcess is that
               process's loop; it owns the stream callback and the mixer and
               publishes where playback is through shared memory.
               AudioEngine is the main program's handle on it: it spawns
               it, sends it commands, reads the position, and respawns it if
               it dies.

Shared memory: a RawArray of doubles. The audio callback is its only writer
of the position slots and guards them with a sequence counter (odd while a
write is in progress, the classic seqlock); the process's main thread is the
only writer of the heartbeat slots. A reader retries until it sees the same
even counter before and after, so it never sees half a callback.
"""
import os
import sys
import threading
import time
from collections import deque, namedtuple

RATE = 48000                 # 48 kHz end to end (handoff section 4a)
MAX_OUTPUTS = 8              # Jeff, 2026-09-27: at most 8 outputs,
MAX_STEMS = 8                # and at most 8 stems a cue
LOCK_FILE = "ltcplay_show_audio.lock"
LOCK_WAIT_S = 3.0            # a closing audio process gets this long to go
ROLES = ("show", "intermission")

# Mixer and shared memory state codes.
IDLE, PLAYING, PAUSED, ENDED = 0, 1, 2, 3
STATE_NAMES = {IDLE: "idle", PLAYING: "playing", PAUSED: "paused",
               ENDED: "ended"}

# Device state, written by the audio process's main thread.
DEV_CLOSED, DEV_OPEN, DEV_REFUSED = 0, 1, 2

_DASHES = ("—", "–")


class AudioConfigError(ValueError):
    """A show audio setting or file that cannot run, with the sentence."""


class Refusal(Exception):
    """The device is there but cannot run this show as configured.
    Permanent until someone changes the interface or the show file."""


class Unavailable(Exception):
    """The device is not attached, or would not open. Worth retrying."""


def _clean(text):
    text = str(text)
    for d in _DASHES:
        text = text.replace(d, "-")
    return text


def fade_frames(ms, rate=RATE):
    return max(0, int(round(float(ms) / 1000.0 * rate)))


def db_to_gain(db):
    return 10.0 ** (float(db) / 20.0)


def fmt_len(frames, rate=RATE):
    s = frames / float(rate)
    m, s = divmod(s, 60.0)
    return f"{int(m)}:{s:06.3f}"


# ------------------------------------------------------------------ config --
def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _no_typos(doc, keys, where, what):
    unknown = sorted(k for k in doc if k not in keys)
    if unknown:
        raise AudioConfigError(
            f"{where}: {what} has no setting "
            f"{', '.join(repr(k) for k in unknown)}; it takes: "
            f"{', '.join(sorted(keys))}.")


class Stem:
    """One file, its gain, and the outputs its channels go to."""

    KEYS = frozenset(("file", "gain_db", "channels", "notes"))

    def __init__(self, file, gain_db=0.0, channels=(1,)):
        self.file = file
        self.gain_db = float(gain_db)
        self.channels = list(channels)

    @property
    def gain(self):
        return db_to_gain(self.gain_db)

    @classmethod
    def parse(cls, doc, where, what, out_channels):
        if not isinstance(doc, dict):
            raise AudioConfigError(
                f"{where}: each of {what} is an object like "
                f"{{\"file\": \"music.wav\", \"gain_db\": 0, "
                f"\"channels\": [1, 2]}}.")
        _no_typos(doc, cls.KEYS, where, f"a stem in {what}")
        f = doc.get("file")
        if not isinstance(f, str) or not f.strip():
            raise AudioConfigError(f"{where}: a stem in {what} has no "
                                   f"'file'. Name the WAV file it plays.")
        g = doc.get("gain_db", 0)
        if not _num(g) or not -60.0 <= g <= 12.0:
            raise AudioConfigError(
                f"{where}: {f!r} in {what} has 'gain_db' {g!r}. It is a "
                f"number of decibels from -60 to 12, and 0 plays the file "
                f"as it is.")
        ch = doc.get("channels")
        if not isinstance(ch, list) or not ch or any(
                not isinstance(c, int) or isinstance(c, bool) for c in ch):
            raise AudioConfigError(
                f"{where}: {f!r} in {what} needs 'channels', the output "
                f"numbers its channels play on, counting from 1, like "
                f"[1, 2].")
        for c in ch:
            if c < 1:
                raise AudioConfigError(
                    f"{where}: {f!r} in {what} lists output {c}. Outputs "
                    f"count from 1.")
            if c > out_channels:
                raise AudioConfigError(
                    f"{where}: {f!r} in {what} plays on output {c}, but "
                    f"'clock.audio.channels' is {out_channels}, so there "
                    f"is no output {c}.")
        if len(set(ch)) != len(ch):
            raise AudioConfigError(
                f"{where}: {f!r} in {what} lists the same output twice.")
        return cls(f.strip(), g, ch)


class AudioCue:
    """The show or the intermission: which timeline cue it belongs to, and
    its stems."""

    KEYS = frozenset(("cue", "stems", "allow_different_lengths", "notes"))

    def __init__(self, role, cue, stems, allow_different_lengths=False):
        self.role = role
        self.cue = cue
        self.stems = list(stems)
        self.allow_different_lengths = bool(allow_different_lengths)

    @classmethod
    def parse(cls, role, doc, where, out_channels):
        what = f"'clock.audio.cues.{role}'"
        if not isinstance(doc, dict):
            raise AudioConfigError(f"{where}: {what} must be an object like "
                                   f"{{\"cue\": \"Show\", \"stems\": "
                                   f"[...]}}.")
        _no_typos(doc, cls.KEYS, where, what)
        cue = doc.get("cue")
        if not isinstance(cue, str) or not cue.strip():
            raise AudioConfigError(
                f"{where}: {what} needs 'cue', the name of the cue in this "
                f"show file whose pixels this audio goes with.")
        stems = doc.get("stems")
        if not isinstance(stems, list) or not stems:
            raise AudioConfigError(f"{where}: {what} has no 'stems'. List "
                                   f"the WAV files it plays.")
        if len(stems) > MAX_STEMS:
            raise AudioConfigError(
                f"{where}: {what} lists {len(stems)} stems. A cue plays at "
                f"most {MAX_STEMS}: mix some of them together first.")
        adl = doc.get("allow_different_lengths", False)
        if not isinstance(adl, bool):
            raise AudioConfigError(f"{where}: {what}.allow_different_lengths"
                                   f" is true or false.")
        parsed = [Stem.parse(s, where, f"{what}.stems", out_channels)
                  for s in stems]
        return cls(role, cue.strip(), parsed, adl)


class AudioConfig:
    """The "clock.audio" block of a show file, validated.

    Structure only: the files it names are checked by check_show(), when the
    session opens and the show folder is known."""

    KEYS = frozenset(("device", "rate", "channels", "cues", "hold_fade_ms",
                      "abort_fade_ms", "return_fade_ms", "allow_shared_mode",
                      "notes"))

    def __init__(self, device, channels, cues, rate=RATE, hold_fade_ms=250,
                 abort_fade_ms=1000, return_fade_ms=1000,
                 allow_shared_mode=False):
        self.device = device
        self.channels = channels
        self.cues = dict(cues)            # role -> AudioCue
        self.rate = rate
        self.hold_fade_ms = hold_fade_ms
        self.abort_fade_ms = abort_fade_ms
        self.return_fade_ms = return_fade_ms
        self.allow_shared_mode = allow_shared_mode

    def summary(self):
        parts = [f"{r} {len(c.stems)} stem(s)" for r, c in self.cues.items()]
        return (f"show audio on {self.device}, {self.channels} outputs at "
                f"{self.rate} Hz ({', '.join(parts)})")

    @classmethod
    def parse(cls, doc, where):
        what = "'clock.audio'"
        if not isinstance(doc, dict):
            raise AudioConfigError(f"{where}: {what} must be an object like "
                                   f"{{\"device\": \"...\", ...}}.")
        _no_typos(doc, cls.KEYS, where, what)
        dev = doc.get("device")
        if not isinstance(dev, str) or not dev.strip():
            raise AudioConfigError(
                f"{where}: 'clock.audio.device' names the show's audio "
                f"interface exactly as this computer lists it. There is no "
                f"default: the show audio never goes to whatever the system "
                f"output happens to be.")
        rate = doc.get("rate", RATE)
        if rate != RATE or isinstance(rate, bool):
            raise AudioConfigError(
                f"{where}: 'clock.audio.rate' is {rate!r}. The show audio "
                f"runs at 48000 Hz end to end, so it must be 48000, and "
                f"every stem must be exported at 48 kHz.")
        ch = doc.get("channels")
        if not isinstance(ch, int) or isinstance(ch, bool) \
                or not 1 <= ch <= MAX_OUTPUTS:
            raise AudioConfigError(
                f"{where}: 'clock.audio.channels' is how many outputs of "
                f"the interface the show uses, 1 to {MAX_OUTPUTS}.")
        fades = {}
        for key, default, top in (("hold_fade_ms", 250, 2000),
                                  ("abort_fade_ms", 1000, 5000),
                                  ("return_fade_ms", 1000, 5000)):
            v = doc.get(key, default)
            if not _num(v) or not 0 <= v <= top:
                raise AudioConfigError(
                    f"{where}: 'clock.audio.{key}' is a number of "
                    f"milliseconds from 0 to {top}.")
            fades[key] = v
        shared = doc.get("allow_shared_mode", False)
        if not isinstance(shared, bool):
            raise AudioConfigError(
                f"{where}: 'clock.audio.allow_shared_mode' is true or false. "
                f"Leave it out for a show.")
        cues = doc.get("cues")
        if not isinstance(cues, dict) or not cues:
            raise AudioConfigError(
                f"{where}: 'clock.audio.cues' lists the audio for the "
                f"\"show\" and the \"intermission\".")
        bad = sorted(k for k in cues if k not in ROLES)
        if bad:
            raise AudioConfigError(
                f"{where}: 'clock.audio.cues' has {', '.join(map(repr, bad))}"
                f". It takes \"show\" and \"intermission\".")
        parsed = {r: AudioCue.parse(r, cues[r], where, ch)
                  for r in ROLES if r in cues}
        names = [c.cue.lower() for c in parsed.values()]
        if len(set(names)) != len(names):
            raise AudioConfigError(
                f"{where}: the show and the intermission audio both name the "
                f"cue {parsed['show'].cue!r}.")
        return cls(dev.strip(), ch, parsed, RATE, fades["hold_fade_ms"],
                   fades["abort_fade_ms"], fades["return_fade_ms"], shared)


# --------------------------------------------------------------- wav files --
# The 12 bytes every standard WAVE_FORMAT_EXTENSIBLE SubFormat GUID shares;
# the leading 4 bytes are the classic format tag (1 PCM, 3 IEEE float).
_EXT_TAIL = bytes.fromhex("00001000800000aa00389b71")

WavInfo = namedtuple("WavInfo", "tag channels rate bits frames offset size")


def wav_info(path):
    """Read a WAV file's header without its samples. Accepts PCM 16, 24 and
    32-bit and 32-bit float, plain or WAVE_FORMAT_EXTENSIBLE. Raises
    AudioConfigError with a sentence for anything else."""
    name = os.path.basename(path)
    try:
        fh = open(path, "rb")
    except FileNotFoundError:
        raise AudioConfigError(f"{name} is missing: there is no file at "
                               f"{_clean(path)}.")
    except OSError as e:
        raise AudioConfigError(f"{name} could not be read: "
                               f"{_clean(e.strerror or e)}.")
    with fh:
        riff = fh.read(12)
        if len(riff) < 12 or riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
            raise AudioConfigError(f"{name} is not a WAV file.")
        fmt = None
        while True:
            head = fh.read(8)
            if len(head) < 8:
                break
            cid = head[:4]
            size = int.from_bytes(head[4:8], "little")
            if cid == b"fmt ":
                body = fh.read(size)
                if len(body) < 16:
                    raise AudioConfigError(f"{name} has a broken header.")
                tag = int.from_bytes(body[0:2], "little")
                if tag == 0xFFFE and len(body) >= 40 and \
                        body[28:40] == _EXT_TAIL:
                    tag = int.from_bytes(body[24:28], "little")
                fmt = (tag, int.from_bytes(body[2:4], "little"),
                       int.from_bytes(body[4:8], "little"),
                       int.from_bytes(body[14:16], "little"))
                if size & 1:
                    fh.read(1)
            elif cid == b"data":
                if fmt is None:
                    raise AudioConfigError(f"{name} has its samples before "
                                           f"its header.")
                # A size of 0 or 0xFFFFFFFF is a placeholder a recorder
                # writes while it is still recording; one bigger than the
                # file is a file cut off. Either would play silence, or run
                # the cue on past its sound. PR 15's rule for announcements.
                left = os.fstat(fh.fileno()).st_size - fh.tell()
                if size == 0 or size == 0xFFFFFFFF:
                    raise AudioConfigError(
                        f"{name} says its audio is {size} bytes long, which "
                        f"is a placeholder: the file looks cut off, or was "
                        f"still being written. Export it again.")
                if size > left:
                    raise AudioConfigError(
                        f"{name} says its audio is {size} bytes long, but "
                        f"only {left} are in the file: it looks cut off, or "
                        f"was still being written. Export it again.")
                tag, ch, rate, bits = fmt
                if tag not in (1, 3) or (tag == 1 and bits not in
                                         (16, 24, 32)) or \
                        (tag == 3 and bits != 32) or ch < 1:
                    kind = ("floating point" if tag == 3 else
                            "PCM" if tag == 1 else f"format {tag}")
                    raise AudioConfigError(
                        f"{name} is {bits}-bit {kind}, which the show audio "
                        f"does not play. Export 16-bit, 24-bit or 32-bit "
                        f"PCM, or 32-bit float.")
                align = ch * bits // 8
                return WavInfo(tag, ch, rate, bits, size // align,
                               fh.tell(), size - size % align)
            else:
                fh.seek(size + (size & 1), 1)
    raise AudioConfigError(f"{name} has no audio in it.")


READ_CHUNK_FRAMES = 1 << 18


def _decode(raw, info):
    import numpy as np
    if info.tag == 3:
        return np.frombuffer(raw, dtype="<f4")
    if info.bits == 16:
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if info.bits == 32:
        return (np.frombuffer(raw, dtype="<i4").astype(np.float64)
                / 2147483648.0).astype(np.float32)
    b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
    v = b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) \
        | (b[:, 2].astype(np.int8).astype(np.int32) << 16)   # signed top
    return v.astype(np.float32) / 8388608.0


def read_wav(path):
    """The whole file as float32, shape (frames, channels), -1.0 to 1.0.

    Read and converted a chunk at a time into the one array it returns, so
    the most memory it ever needs is that array plus one chunk: about 4
    bytes a sample, whatever the file's format."""
    import numpy as np
    info = wav_info(path)
    align = info.channels * info.bits // 8
    out = np.empty((info.frames, info.channels), dtype=np.float32)
    flat = out.reshape(-1)
    with open(path, "rb") as fh:
        fh.seek(info.offset)
        done = 0
        while done < info.frames:
            n = min(READ_CHUNK_FRAMES, info.frames - done)
            raw = fh.read(n * align)
            n = len(raw) // align
            if n <= 0:
                break
            flat[done * info.channels:(done + n) * info.channels] = \
                _decode(raw[:n * align], info)
            done += n
    return out[:done]


def _find_cue(timeline, want):
    w = want.strip().lower()
    return [c for c in timeline.cues
            if w in (c.name.lower(), os.path.basename(c.path).lower(),
                     c.tc_text.lower())]


def check_show(acfg, timeline):
    """Check every file the audio block names, against the show folder.

    Returns {role: {"label": timeline cue name, "frames": length,
    "stems": [(path, gain, [0-based outputs])]}}. Raises AudioConfigError
    with a sentence for a missing file, a stem at the wrong rate, stems of
    different lengths (unless the cue allows it), a stem whose channels do
    not match its outputs, or a cue that is not in the show."""
    show_dir = getattr(timeline, "show_dir", "") or ""
    out = {}
    for role, cue in acfg.cues.items():
        hits = _find_cue(timeline, cue.cue)
        if len(hits) != 1:
            names = ", ".join(c.name for c in timeline.cues) or "none"
            raise AudioConfigError(
                f"The {role} audio goes with the cue {cue.cue!r}, which "
                + ("matches more than one cue" if hits else
                   "is not a cue in this show")
                + f". The cues are: {names}.")
        lengths, stems = [], []
        for st in cue.stems:
            path = st.file if os.path.isabs(st.file) else \
                os.path.join(show_dir, st.file)
            if not os.path.exists(path):
                raise AudioConfigError(
                    f"The {role} audio file {st.file} is missing: there is "
                    f"no file at {_clean(path)}. The show cannot play "
                    f"without it.")
            info = wav_info(path)
            if info.rate != RATE:
                raise AudioConfigError(
                    f"{st.file} is {info.rate} Hz. The show audio runs at "
                    f"48000 Hz end to end, so every stem has to be exported "
                    f"at 48 kHz.")
            if info.channels != 1 and info.channels != len(st.channels):
                raise AudioConfigError(
                    f"{st.file} has {info.channels} channels but its "
                    f"'channels' lists {len(st.channels)} output(s). List "
                    f"one output for each channel, or use a mono file.")
            lengths.append((info.frames, st.file))
            stems.append((path, st.gain, [c - 1 for c in st.channels]))
        frames = [n for n, _ in lengths]
        if len(set(frames)) > 1 and not cue.allow_different_lengths:
            which = ", ".join(f"{f} is {fmt_len(n)}" for n, f in lengths)
            raise AudioConfigError(
                f"The {role} stems are not all the same length: {which}. "
                f"Export them from the same region, or set "
                f"\"allow_different_lengths\": true for this cue if that is "
                f"on purpose.")
        if max(frames) <= 0:
            raise AudioConfigError(f"The {role} audio has no samples in it.")
        out[role] = {"label": hits[0].name, "frames": max(frames),
                     "stems": stems}
    return out


# -------------------------------------------------------------------- mixer --
def _ramp(cur, to, left, n):
    """A linear ramp from `cur` reaching `to` after `left` frames, over the
    next `n` frames. Returns (vector, value after, frames still to go,
    index where the ramp finished inside this block or None)."""
    import numpy as np
    if left <= 0:
        return np.full(n, cur, dtype=np.float32), cur, 0, None
    k = min(n, left)
    v = np.empty(n, dtype=np.float32)
    v[:k] = cur + (to - cur) * (np.arange(1, k + 1, dtype=np.float64) / left)
    if k < n:
        v[k:] = to
    rest = left - k
    if rest == 0:
        return v, float(to), 0, k
    return v, float(v[k - 1]), rest, None


class Mixer:
    """Stems in, one float32 block out: gain, channel routing, summing,
    fades, clipping protection.

    Two gains multiply: `env`, the transport's own fades (play, pause,
    resume, stop), and `level`, the master level (Abort fades it). A pause or
    a stop is a fade of `env` to zero that ENDS the playback exactly where
    it reaches zero: `frame` stops there, every later sample is silence, and
    `stop_frame` says where that is from the moment the fade starts, so the
    clock can freeze on that exact point. No run-on.

    Owned by the audio callback. The process's main thread only queues
    commands for it (see AudioProcess)."""

    def __init__(self, out_channels, rate=RATE):
        self.out_channels = int(out_channels)
        self.rate = rate
        self.cues = {}
        self.cue = None
        self.token = 0
        self.state = IDLE
        self.frame = 0
        self.stop_frame = -1
        self.env = 0.0
        self._env_to, self._env_left, self._after = 0.0, 0, None
        self.level = 1.0
        self._lvl_to, self._lvl_left = 1.0, 0
        self.clipped = 0
        self.ignored = 0

    def add_cue(self, key, stems):
        """stems: [(float32 array (frames, channels), gain, [outputs])]"""
        for pcm, _g, outs in stems:
            if max(outs) >= self.out_channels:
                raise ValueError("a stem is routed past the last output")
            if pcm.shape[1] not in (1, len(outs)):
                raise ValueError("a stem's channels do not match its outputs")
        frames = max(p.shape[0] for p, _g, _o in stems)
        self.cues[key] = (list(stems), frames)
        return frames

    @property
    def cue_frames(self):
        c = self.cues.get(self.cue)
        return c[1] if c else 0

    def _env_ramp(self, to, frames, after=None):
        frames = int(frames)
        if frames <= 0:
            self.env, self._env_left, self._after = float(to), 0, None
            if after is not None:
                self._finish(after, self.frame)
            return
        self._env_to, self._env_left, self._after = float(to), frames, after

    def _finish(self, after, at_frame):
        self.frame = at_frame
        self.stop_frame = at_frame
        self.env = 0.0
        self._env_left, self._after = 0, None
        if after == IDLE:
            self.state = IDLE
            self.cue = None
        else:
            self.state = PAUSED

    def play(self, key, start_frame=0, fade=0, token=0):
        if key not in self.cues:
            self.ignored += 1
            return False
        self.cue = key
        self.token = token
        total = self.cues[key][1]
        self.frame = max(0, min(int(start_frame), total))
        self.stop_frame = -1
        self.state = PLAYING if self.frame < total else ENDED
        self.env = 0.0 if fade > 0 else 1.0
        self._after = None
        self._env_ramp(1.0, fade)
        return True

    def _mine(self, token):
        return token is None or token == self.token

    def pause(self, fade, token=None):
        if self.state != PLAYING or not self._mine(token):
            return False
        fade = int(fade)
        self.stop_frame = min(self.frame + max(fade, 0), self.cue_frames)
        self._env_ramp(0.0, fade, after=PAUSED)
        return True

    def resume(self, fade, token=None):
        """From PAUSED, carry on from the exact frame it stopped on. During
        a pause's fade, cancel it and come back up from where it is."""
        if not self._mine(token):
            return False
        if self.state == PAUSED:
            self.state = PLAYING
            self.env = 0.0
        elif not (self.state == PLAYING and self._after == PAUSED):
            return False
        self.stop_frame = -1
        self._after = None
        self._env_ramp(1.0, fade)
        return True

    def stop(self, fade, token=None):
        if not self._mine(token):
            return False
        if self.state != PLAYING:
            self.hard_stop()
            return True
        fade = int(fade)
        self.stop_frame = min(self.frame + max(fade, 0), self.cue_frames)
        self._env_ramp(0.0, fade, after=IDLE)
        return True

    def set_level(self, target, fade):
        target = min(max(float(target), 0.0), 1.0)
        if int(fade) <= 0:
            self.level, self._lvl_left = target, 0
        else:
            self._lvl_to, self._lvl_left = target, int(fade)

    def hard_stop(self):
        self.state = IDLE
        self.cue = None
        self.stop_frame = -1
        self.env = 0.0
        self._env_left, self._after = 0, None

    def render(self, n):
        import numpy as np
        out = np.zeros((n, self.out_channels), dtype=np.float32)
        if self.state != PLAYING:
            return out
        stems, total = self.cues[self.cue]
        a = self.frame
        b = min(a + n, total)
        for pcm, gain, outs in stems:
            end = min(b, pcm.shape[0])
            if end <= a:
                continue
            seg = pcm[a:end]
            k = end - a
            if pcm.shape[1] == 1:
                col = seg[:, 0] * np.float32(gain)
                for o in outs:
                    out[:k, o] += col
            else:
                for i, o in enumerate(outs):
                    out[:k, o] += seg[:, i] * np.float32(gain)
        env, self.env, self._env_left, done = _ramp(
            self.env, self._env_to, self._env_left, n)
        lvl, self.level, self._lvl_left, _ = _ramp(
            self.level, self._lvl_to, self._lvl_left, n)
        out *= (env * lvl)[:, None]
        if done is not None and self._after is not None \
                and a + done <= b:
            out[done:] = 0.0
            self._finish(self._after, a + done)
        else:
            self.frame = b
            if b >= total:
                self.state = ENDED
        over = np.abs(out) > 1.0
        if over.any():
            self.clipped += int(np.count_nonzero(over))
            np.clip(out, -1.0, 1.0, out=out)
        return out


MIXER_COMMANDS = ("play", "pause", "resume", "stop", "level")


def apply_command(m, msg):
    kind = msg[0]
    if kind == "play":
        return m.play(msg[1], msg[2], msg[3], msg[4])
    if kind == "pause":
        return m.pause(msg[1], msg[2])
    if kind == "resume":
        return m.resume(msg[1], msg[2])
    if kind == "stop":
        return m.stop(msg[1], msg[2])
    if kind == "level":
        return m.set_level(msg[1], msg[2])
    return False


# ----------------------------------------------------------- shared memory --
SEQ, TOKEN, STATE, PLAYING_AT, FRAME, STOP, CUE_FRAMES, PERF, LATENCY, \
    LEVEL, CALLBACKS, UNDERFLOWS, CLIPPED, ERRORS = range(14)
HEARTBEAT, DEVSTATE, OPENS = 16, 17, 18
SLOTS = 20

Position = namedtuple("Position", "seq token state playing frame stop_frame "
                                  "cue_frames perf latency level callbacks "
                                  "underflows clipped errors")


class Publisher:
    """The audio process's side of the shared memory."""

    def __init__(self, arr):
        self.arr = arr

    def publish(self, token, state, playing, frame, stop_frame, cue_frames,
                perf, latency, level, callbacks, underflows, clipped,
                errors=0):
        a = self.arr
        s = a[SEQ]
        a[SEQ] = s + 1.0                 # odd: a write is in progress
        a[TOKEN] = token
        a[STATE] = state
        a[PLAYING_AT] = 1.0 if playing else 0.0
        a[FRAME] = frame
        a[STOP] = stop_frame
        a[CUE_FRAMES] = cue_frames
        a[PERF] = perf
        a[LATENCY] = latency
        a[LEVEL] = level
        a[CALLBACKS] = callbacks
        a[UNDERFLOWS] = underflows
        a[CLIPPED] = clipped
        a[ERRORS] = errors
        a[SEQ] = s + 2.0                 # even: done

    def heartbeat(self, now, devstate, opens):
        a = self.arr
        a[HEARTBEAT] = now
        a[DEVSTATE] = devstate
        a[OPENS] = opens


def read_position(arr, offset=0.0, tries=100):
    """The last callback's report, or None if nothing has played yet.
    `perf` comes back on the reader's own perf_counter (offset removed)."""
    for _ in range(tries):
        s1 = arr[SEQ]
        if s1 <= 0:
            return None
        if int(s1) & 1:
            continue
        v = arr[TOKEN:ERRORS + 1]
        if arr[SEQ] != s1:
            continue                     # a callback wrote meanwhile
        return Position(int(s1), int(v[0]), int(v[1]), bool(v[2]),
                        int(v[3]), int(v[4]), int(v[5]), v[6] - offset,
                        v[7], v[8], int(v[9]), int(v[10]), int(v[11]),
                        int(v[12]))
    return None


# -------------------------------------------------------------- the device --
def import_sounddevice(platform=None):
    """sounddevice, imported the way the show audio needs it.

    On Windows, python-sounddevice loads its ASIO-enabled PortAudio only
    when SD_ENABLE_ASIO=1 is set BEFORE it is first imported, so this sets it
    first. Called only inside the audio process, which never imports
    sounddevice any other way, so the main program's own sounddevice (the
    LTC input, the announcements) is not touched."""
    if (platform or sys.platform) == "win32":
        os.environ["SD_ENABLE_ASIO"] = "1"
    import sounddevice
    return sounddevice


# The order the Windows host APIs are tried in: (PortAudio's name for it,
# WASAPI exclusive?, what it is called on the page, mixer-free?). The first
# three never go through Windows' shared audio engine. The last three do,
# and are tried only when the show file sets allow_shared_mode.
WINDOWS_APIS = (
    ("ASIO", False, "ASIO", True),
    ("Windows WASAPI", True, "WASAPI exclusive", True),
    # Kernel streaming: mixer-free, and what a class-driver interface with
    # no ASIO driver often takes at 48 kHz when WASAPI will not (bench B23,
    # the Scarlett). It only opens when nothing else holds the endpoint.
    ("Windows WDM-KS", False, "WDM-KS", True),
)
WINDOWS_SHARED_APIS = (
    ("Windows WASAPI", False, "WASAPI shared", False),
    ("Windows DirectSound", False, "DirectSound", False),
    ("MME", False, "MME", False),
)


def open_output_stream(sd, name, channels, rate, callback, allow_shared=False,
                       platform=None, finished_callback=None):
    """Open the show's audio output, never through the Windows shared mixer.

    The one place this rule lives:

      Windows  Every host API the device appears under is tried in order
               until one takes `channels` outputs at `rate` Hz and opens:
               ASIO (see import_sounddevice for how python-sounddevice is
               made to see ASIO at all), then WASAPI exclusive
               (sd.WasapiSettings(exclusive=True)), then WDM-KS. Never
               WASAPI shared, DirectSound or MME, which go through Windows'
               shared audio engine, the one that broke up 3 to 6 times a
               show on the bench (B18), unless the show file sets
               allow_shared_mode for a bench test; then those are tried
               last, in that order, and what comes back says so.
      macOS    CoreAudio, the only host API there.
      other    whatever PortAudio offers (Linux CI).

    A device one API refuses (WASAPI would not run the bench Scarlett at
    48 kHz, WDM-KS and DirectSound would: B23) is not the end: the next is
    tried. When every one refuses, the sentence names each one tried and
    why.

    The device is found by its EXACT name, case-insensitive, never by index
    and never by a substring: announce.py's rule, for the reason it gives.

    Returns (stream, description, shared). The stream is not started.
    Raises Refusal for a device that is there but cannot run this show, and
    Unavailable for one that is not attached or would not open."""
    plat = platform or sys.platform
    want = str(name).strip().lower()
    apis = [a.get("name", "") for a in sd.query_hostapis()]
    devs = list(sd.query_devices())
    outs = [(i, d) for i, d in enumerate(devs)
            if d.get("max_output_channels", 0) > 0]
    named = [(i, d) for i, d in outs
             if str(d.get("name", "")).strip().lower() == want]
    if not named:
        listed = ", ".join(sorted({str(d.get("name")) for _, d in outs}))
        raise Unavailable(f"{_clean(name)!r} is not attached. Nothing else "
                          f"will be used in its place. Outputs on this "
                          f"computer: {_clean(listed) or 'none'}.")

    def api(d):
        h = d.get("hostapi", 0)
        return apis[h] if 0 <= h < len(apis) else ""

    if plat == "win32":
        order = list(WINDOWS_APIS) + (list(WINDOWS_SHARED_APIS)
                                      if allow_shared else [])
    else:
        order = [(None, False, "CoreAudio" if plat == "darwin"
                  else "PortAudio", True)]
    tried = []                       # (what it is called, why it refused)
    refused = False                  # a real "cannot", not just "busy"
    advice = []
    for want_api, exclusive, label, mixer_free in order:
        hits = [(i, d) for i, d in named
                if want_api is None or api(d) == want_api]
        if not hits:
            continue
        if len(hits) > 1:
            tried.append((label, f"{len(hits)} outputs have that exact "
                                 f"name, so it is not specific enough"))
            refused = True
            continue
        index, dev = hits[0]
        have = int(dev.get("max_output_channels", 0))
        if channels > have:
            tried.append((label, f"it has {have} output(s), and the show "
                                 f"uses {channels}"))
            refused = True
            advice.append("Use an interface with enough outputs, or route "
                          "the stems to fewer.")
            continue
        extra = sd.WasapiSettings(exclusive=True) if exclusive else None
        try:
            sd.check_output_settings(device=index, channels=channels,
                                     samplerate=rate, dtype="float32",
                                     extra_settings=extra)
        except Exception as e:
            tried.append((label, f"it will not play {channels} output(s) "
                                 f"at {rate} Hz ({_clean(e)})"))
            refused = True
            advice.append(f"The show audio runs at {rate} Hz: set the "
                          f"interface to 48 kHz in its own control panel.")
            continue
        kw = dict(device=index, channels=channels, samplerate=rate,
                  dtype="float32", latency="high", callback=callback)
        if extra is not None:
            kw["extra_settings"] = extra
        if finished_callback is not None:
            kw["finished_callback"] = finished_callback
        try:
            stream = sd.OutputStream(**kw)
        except Exception as e:
            tried.append((label, f"it would not open ({_clean(e)})"))
            continue
        how = label + (", mixer-free" if mixer_free and plat == "win32"
                       else "" if plat != "win32"
                       else ", through Windows' shared audio engine")
        desc = (f"{dev.get('name')}, {how}, {channels} outputs at "
                f"{rate} Hz")
        return stream, desc, not mixer_free
    dev_name = _clean(named[0][1].get("name"))
    if not tried:
        via = ", ".join(sorted({api(d) for _, d in named}))
        raise Refusal(
            f"{_clean(name)!r} can only be reached through Windows' shared "
            f"audio engine ({via}) on this computer, which broke up 3 to 6 "
            f"times a show on the bench. Install the interface's ASIO "
            f"driver, or make sure it is offered to WASAPI or WDM-KS. For "
            f"a bench test only, 'allow_shared_mode' lets it play anyway.")
    said = "; ".join(f"{label}: {why}" for label, why in tried)
    shared_left = plat == "win32" and not allow_shared and any(
        api(d) in {a[0] for a in WINDOWS_SHARED_APIS} for _, d in named)
    more = (" It is also offered to Windows' shared audio engine, which the "
            "show does not use unless 'allow_shared_mode' is on, for a bench "
            "test only." if shared_left else "")
    if refused:
        tips = " ".join(dict.fromkeys(advice))
        raise Refusal(f"{dev_name} cannot play the show audio: every way "
                      f"to it was tried and refused. {said}.{more}"
                      + (f" {tips}" if tips else ""))
    raise Unavailable(f"{dev_name} would not open. {said}.{more}")


# ------------------------------------------------------- the audio process --
class AudioProcess:
    """The audio process's own loop, as an object: the real process runs it
    (child_main below), and the tests run it in-process, step by step, with
    a fake device and a fake clock.

    The callback owns the mixer. The main thread only queues commands for
    it, and looks after the device: it opens it, notices when it has died
    (the driver stopped it, or it stopped calling back), and reopens it,
    every 1 s at first, backing off to every 3 s. A stream that is running
    is never touched, whatever else appears or disappears on the machine
    (a monitor's HDMI audio, on Windows): PortAudio's device list is only
    refreshed when there is no stream to disturb."""

    STALL_S = 0.5
    START_S = 1.5
    RETRY_S = (1.0, 2.0, 3.0)

    def __init__(self, spec, sd, pub, send, clock=time.perf_counter,
                 sync_load=False):
        self.spec = spec
        self.sd = sd
        self.pub = pub
        self.send = send
        self._clock = clock
        self.sync_load = sync_load
        self.name = spec["device"]
        self.mixer = Mixer(spec["channels"], spec.get("rate", RATE))
        self.cmdq = deque()
        self.stream = None
        self.opened_at = None
        self.last_cb = None
        self.callbacks = 0
        self.underflows = 0
        self.render_errors = 0
        self.next_try = 0.0
        self.fails = 0
        self.opens = 0
        self.devstate = DEV_CLOSED
        self._said = None
        self._loads = []
        self._load_q = None
        self._loader = None
        self._reinit = False
        self._fallback_latency = 0.0

    # -- commands from the main program
    def command(self, msg):
        kind = msg[0]
        if kind == "ping":
            self.send(("pong", msg[1], self._clock()))
        elif kind == "load":
            self._load(msg[1], msg[2])
        elif kind == "fake":
            ctl = getattr(self.sd, "control", None)
            if ctl is not None:
                ctl(*msg[1:])
        elif kind in MIXER_COMMANDS:
            if self.stream is not None:
                self.cmdq.append(msg)
            else:
                apply_command(self.mixer, msg)

    def _load(self, role, stems):
        """Decode a cue's stems. One cue at a time, one stem at a time, on
        one thread: the show and the intermission are never both being
        decoded at once, so the most memory it needs is what is loaded
        plus one stem's chunk."""
        def work(role, stems):
            try:
                dec = [(read_wav(p), float(g), list(o)) for p, g, o in stems]
                self._loads.append((role, dec, None))
            except Exception as e:
                self._loads.append((role, None, _clean(e)))
        if self.sync_load:
            work(role, stems)
            return
        if self._loader is None:
            import queue
            self._load_q = queue.Queue()

            def drain(q):
                while True:
                    work(*q.get())
            self._loader = threading.Thread(target=drain,
                                            args=(self._load_q,),
                                            daemon=True,
                                            name="ltcplay-audio-load")
            self._loader.start()
        self._load_q.put((role, stems))

    def _finish_loads(self):
        while self._loads:
            role, dec, err = self._loads.pop(0)
            if err is None:
                try:
                    frames = self.mixer.add_cue(role, dec)
                except Exception as e:
                    err = _clean(e)
            if err is not None:
                self.send(("load_failed", role,
                           f"The {role} audio could not be loaded: {err}"))
            else:
                self.send(("loaded", role, frames))

    # -- the stream callback, on the device's own thread
    def callback(self, outdata, frames, time_info, status):
        now = self._clock()
        self.last_cb = now
        m = self.mixer
        q = self.cmdq
        while q:
            try:
                apply_command(m, q.popleft())
            except Exception:
                self.render_errors += 1
        playing = m.state == PLAYING
        start = m.frame
        try:
            outdata[:] = m.render(frames)
        except Exception:
            self.render_errors += 1
            outdata.fill(0)
        if status and getattr(status, "output_underflow", False):
            self.underflows += 1
        self.callbacks += 1
        lat = self._fallback_latency
        try:
            dac = float(time_info.outputBufferDacTime)
            cur = float(time_info.currentTime)
            if dac > 0 and cur > 0 and 0.0 <= dac - cur < 1.0:
                lat = dac - cur
        except Exception:
            pass
        self.pub.publish(m.token, m.state, playing, start, m.stop_frame,
                         m.cue_frames, now, lat, m.level * m.env,
                         self.callbacks, self.underflows, m.clipped,
                         self.render_errors)

    # -- the device, on the main thread
    def _say(self, kind, text):
        if self._said != (kind, text):
            self._said = (kind, text)
            self.send((kind, text))

    def _active(self):
        try:
            return bool(self.stream.active)
        except Exception:
            return False

    def _drop(self):
        s, self.stream = self.stream, None
        try:
            s.abort()
        except Exception:
            pass
        try:
            s.close()
        except Exception:
            pass
        self.cmdq.clear()
        self.mixer.hard_stop()
        self.devstate = DEV_CLOSED
        self._reinit = True

    def step(self, now):
        self.pub.heartbeat(now, self.devstate, self.opens)
        self._finish_loads()
        if self.stream is not None:
            why = None
            if not self._active():
                why = "was stopped by its driver"
            elif self.last_cb is None and now - self.opened_at > self.START_S:
                why = "opened but never started playing"
            elif self.last_cb is not None and \
                    now - self.last_cb > self.STALL_S:
                why = (f"stopped playing: no audio went to it for "
                       f"{now - self.last_cb:.1f} s")
            if why is None:
                return
            self._drop()
            self._said = None
            self.send(("lost", f"The show audio interface "
                               f"{_clean(self.name)} {why}."))
            self.fails = 0
            self.next_try = now + self.RETRY_S[0]
            return
        if now < self.next_try:
            return
        self._try_open(now)

    def _try_open(self, now):
        if self._reinit:
            # Only here, with no stream open: this is how a device that was
            # unplugged and plugged back in becomes visible to PortAudio.
            for f in ("_terminate", "_initialize"):
                try:
                    getattr(self.sd, f)()
                except Exception:
                    pass
            self._reinit = False
        self.cmdq.clear()
        self.mixer.hard_stop()
        spec = self.spec
        try:
            stream, desc, shared = open_output_stream(
                self.sd, self.name, spec["channels"], spec.get("rate", RATE),
                self.callback, allow_shared=spec.get("allow_shared", False),
                platform=spec.get("platform"))
            self.last_cb = None
            try:
                self._fallback_latency = float(stream.latency or 0.0)
            except Exception:
                self._fallback_latency = 0.0
            stream.start()
        except Refusal as e:
            self.devstate = DEV_REFUSED
            self._retry(now)
            self._say("refused", _clean(e))
            return
        except Exception as e:
            self.devstate = DEV_CLOSED
            self._reinit = True
            self._retry(now)
            msg = str(e) if isinstance(e, Unavailable) else \
                f"{_clean(self.name)} would not start: {_clean(e)}."
            self._say("unavailable", _clean(msg))
            return
        self.stream = stream
        self.opened_at = now
        self.opens += 1
        self.devstate = DEV_OPEN
        self.fails = 0
        self._said = None
        self.send(("opened", desc, self._fallback_latency, bool(shared)))

    def _retry(self, now):
        self.fails += 1
        self.next_try = now + self.RETRY_S[min(self.fails - 1,
                                               len(self.RETRY_S) - 1)]

    def close(self, wait_s=0.2):
        if self.stream is not None:
            end = self._clock() + wait_s
            while self.mixer.state == PLAYING and self._clock() < end:
                time.sleep(0.01)
            s, self.stream = self.stream, None
            for f in ("stop", "close"):
                try:
                    getattr(s, f)()
                except Exception:
                    pass
        self.devstate = DEV_CLOSED


def make_sd(spec):
    fake = spec.get("fake")
    if fake is not None:
        return FakeSoundDevice(**fake)
    return import_sounddevice(spec.get("platform"))


def lock_path(spec=None):
    """One show audio process per user on this computer: the lock sits
    beside ltcplay's own "one sender on the rig" lock (onlyone.path())."""
    if spec and spec.get("lock"):
        return spec["lock"]
    from . import onlyone
    return os.path.join(os.path.dirname(onlyone.path()), LOCK_FILE)


def take_lock(spec, clock=time.monotonic, sleep=time.sleep):
    """The audio process's own single-instance lock, the same kind as
    onlyone.OutputLock (a flock, or a byte-range lock on Windows), so the
    operating system drops it the moment the process holding it dies.
    Waits LOCK_WAIT_S for one that is closing. Returns the held lock, or
    raises onlyone.AlreadyRunning."""
    from . import onlyone
    lock = onlyone.OutputLock(where=lock_path(spec),
                              note=f"show audio on {spec.get('device')}")
    end = clock() + float(spec.get("lock_wait_s", LOCK_WAIT_S))
    while True:
        try:
            return lock.acquire()
        except onlyone.AlreadyRunning:
            if clock() >= end:
                raise
            sleep(0.05)


def child_main(conn, arr, spec):
    """The audio process. Runs until told to close, or until the main
    program goes away (its end of the pipe closes), so the sound never
    outlives the program that started it. Only one runs at a time on this
    computer: a second refuses, says so, and exits."""
    try:
        from . import onlyone
        try:
            held = take_lock(spec)
        except onlyone.AlreadyRunning as e:
            who = f" ({_clean(e.holder)})" if e.holder else ""
            conn.send(("refused", f"Another show audio process is already "
                                  f"running on this computer{who}. Only one "
                                  f"may play the show audio at a time: stop "
                                  f"the other ltcplay first."))
            return
        sd = make_sd(spec)
        import numpy  # noqa: F401
    except Exception as e:
        try:
            conn.send(("refused", f"The show audio cannot start on this "
                                  f"computer: {_clean(e)}. Run the "
                                  f"installer again; it installs numpy and "
                                  f"sounddevice."))
        except Exception:
            pass
        return
    proc = AudioProcess(spec, sd, Publisher(arr), conn.send)
    conn.send(("hello", os.getpid()))
    try:
        while True:
            if conn.poll(0.02):
                msg = conn.recv()
                if msg[0] == "close":
                    break
                proc.command(msg)
            proc.step(time.perf_counter())
    except (EOFError, OSError, KeyboardInterrupt):
        pass
    finally:
        proc.close()
        held.release()


class AudioEngine:
    """The main program's handle on the audio process.

    start() spawns it (multiprocessing, spawn: a fresh interpreter, on every
    OS), measures how its perf_counter lines up with this one, sends it the
    cues to load, and waits for its first word on the device. A watch thread
    then drains what it says into events(), and notices if it dies or hangs:
    that is reported as ("crashed", sentence) and it is started again, after
    1 s, then 2, then every 3. read() is the last callback's report, on this
    process's perf_counter.

    Each start() is a generation with its own stop flag, handed to its own
    watch thread, and only one process is ever being spawned at a time. A
    watch thread whose generation was closed while it was spawning kills
    what it spawned and says nothing, so Stop then Run can never leave two
    audio processes or two watch threads behind."""

    HELLO_S = 30.0
    HANG_S = 5.0
    RESPAWN_S = (1.0, 2.0, 3.0)
    WATCH_S = 0.05
    CLOSE_S = 0.5
    VERDICTS = ("opened", "refused", "unavailable")

    def __init__(self, spec, log=None, clock=time.perf_counter):
        self.spec = spec
        self.log = log
        self._clock = clock
        self._events = deque()
        self._send_lock = threading.Lock()
        self._spawn_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._ctx = None
        self._arr = None
        self._proc = None
        self._conn = None
        self._watch = None
        self._stop = threading.Event()
        self._first = None
        self._first_evt = threading.Event()
        self.offset = 0.0
        self.spawns = 0
        self.respawns = 0
        self.crashes = 0
        self.pid = None

    def _take(self, msg, stop):
        if stop.is_set():
            return
        if msg and msg[0] in self.VERDICTS and self._first is None:
            self._first = msg
            self._first_evt.set()
        self._events.append(msg)

    def _spawn(self, stop):
        """Start a process and wait for it to answer. Returns
        (process, connection, None), or (None, None, sentence)."""
        with self._spawn_lock:
            if stop.is_set():
                return None, None, "stopped"
            ctx = self._ctx
            arr = self._arr
            for i in range(len(arr)):
                arr[i] = 0.0
            mine, theirs = ctx.Pipe()
            p = ctx.Process(target=child_main, args=(theirs, arr, self.spec),
                            name="ltcplay-show-audio", daemon=True)
            _start_without_main(p)
            theirs.close()
            self.spawns += 1
            why, pid = self._hello(p, mine, stop)
            if why is not None:
                self._kill(p)
                try:
                    mine.close()
                except Exception:
                    pass
                return None, None, why
            self.pid = pid
            for role, stems in self.spec["cues"].items():
                mine.send(("load", role, stems))
            return p, mine, None

    def _hello(self, p, mine, stop):
        if not mine.poll(self.HELLO_S):
            return (f"The show audio process did not answer within "
                    f"{self.HELLO_S:.0f} s.", None)
        try:
            msg = mine.recv()
        except (EOFError, OSError):
            return "The show audio process stopped as it started.", None
        if msg[0] != "hello":
            self._take(msg, stop)
            return (msg[1] if len(msg) > 1 else
                    "The show audio did not start."), None
        # Where its perf_counter sits against ours: on every OS this ships on
        # both are the same system-wide counter, and then this measures
        # zero within a round trip, which is taken as exactly zero.
        best = None
        for _ in range(5):
            t0 = self._clock()
            mine.send(("ping", t0))
            got = None
            while self._clock() < t0 + 2.0 and mine.poll(0.5):
                m = mine.recv()
                if m[0] == "pong" and m[1] == t0:
                    got = m
                    break
                self._take(m, stop)
            if got is None:
                continue
            t1 = self._clock()
            rtt = t1 - t0
            off = got[2] - (t0 + t1) / 2.0
            if best is None or rtt < best[0]:
                best = (rtt, off)
        if best is not None:
            self.offset = 0.0 if abs(best[1]) <= best[0] else best[1]
        return None, msg[1]

    def _install(self, p, conn, stop):
        """Make a freshly spawned process the current one, unless its
        generation has been closed meanwhile."""
        with self._state_lock:
            if stop.is_set():
                orphan = True
            else:
                orphan = False
                self._proc, self._conn = p, conn
        if orphan:
            self._reap(p, conn)
            return False
        return True

    def start(self, wait_s=5.0):
        """Spawn the process and return its first word on the device:
        ("opened", ...), ("refused", sentence) or ("unavailable", sentence),
        or ("refused", sentence) when it did not start at all."""
        import multiprocessing
        self._ctx = multiprocessing.get_context("spawn")
        if self._arr is None:
            self._arr = self._ctx.RawArray("d", SLOTS)
        stop = threading.Event()
        self._stop = stop
        self._first, self._first_evt = None, threading.Event()
        p, conn, why = self._spawn(stop)
        if why is not None:
            return self._first or ("refused", why)
        if not self._install(p, conn, stop):
            return None
        self._watch = threading.Thread(target=self._watch_loop,
                                       args=(stop,),
                                       name="ltcplay-audio-watch",
                                       daemon=True)
        self._watch.start()
        self._first_evt.wait(wait_s)
        return self._first

    @staticmethod
    def _kill(p):
        try:
            p.terminate()
            p.join(1.0)
            if p.is_alive():
                p.kill()
                p.join(1.0)
        except Exception:
            pass

    def _reap(self, p, conn, wait_s=0.0):
        """Tell a process to close and make sure it goes, off the caller's
        thread unless it goes at once."""
        if conn is not None:
            try:
                conn.send(("close",))
            except Exception:
                pass
        if p is not None:
            p.join(wait_s)
        if p is not None and p.is_alive():
            def finish():
                p.join(1.0)
                if p.is_alive():
                    self._kill(p)
                try:
                    conn.close()
                except Exception:
                    pass
            threading.Thread(target=finish, daemon=True,
                             name="ltcplay-audio-reap").start()
        elif conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _watch_loop(self, stop):
        backoff = 0
        while not stop.is_set():
            with self._state_lock:
                conn, p = self._conn, self._proc
            dead = None
            if conn is not None:
                try:
                    while conn.poll(0):
                        self._take(conn.recv(), stop)
                except (EOFError, OSError):
                    dead = "its connection closed"
            if dead is None and p is not None and not p.is_alive():
                dead = f"it exited (code {p.exitcode})"
            if dead is None and p is not None:
                hb = self._arr[HEARTBEAT]
                if hb > 0 and self._clock() - (hb - self.offset) > self.HANG_S:
                    dead = (f"it stopped answering for more than "
                            f"{self.HANG_S:.0f} s")
            if dead is None:
                backoff = 0
                stop.wait(self.WATCH_S)
                continue
            if stop.is_set():
                return
            self.crashes += 1
            self._take(("crashed", f"The show audio process stopped: {dead}. "
                                   f"It is being started again."), stop)
            with self._state_lock:
                if self._proc is p:
                    self._proc = self._conn = None
            self._reap(p, conn)
            while not stop.is_set():
                wait = self.RESPAWN_S[min(backoff, len(self.RESPAWN_S) - 1)]
                backoff += 1
                if stop.wait(wait):
                    return
                np_, nc, why = self._spawn(stop)
                if why is None:
                    if not self._install(np_, nc, stop):
                        return
                    self.respawns += 1
                    self._take(("respawned", self.pid), stop)
                    break
                self._take(("unavailable", why), stop)

    def send(self, msg):
        with self._send_lock:
            c = self._conn
            if c is None:
                return False
            try:
                c.send(msg)
                return True
            except Exception:
                return False

    def read(self):
        if self._arr is None:
            return None
        return read_position(self._arr, self.offset)

    def events(self):
        out = []
        q = self._events
        while q:
            out.append(q.popleft())
        return out

    @property
    def alive(self):
        p = self._proc
        return bool(p is not None and p.is_alive())

    def close(self):
        """Stop this generation now. The process is told to close and given
        CLOSE_S to go; anything slower is finished off in the background,
        so Stop is never held up by it."""
        self._stop.set()
        with self._state_lock:
            p, c = self._proc, self._conn
            self._proc = self._conn = None
        self._reap(p, c, self.CLOSE_S)
        self._watch = None


_MAIN_LOCK = threading.Lock()


def _start_without_main(p):
    """Start a spawned process WITHOUT it re-running this program's main
    script.

    spawn normally re-imports the parent's __main__ in the child, as
    "__mp_main__", so that the target function can be found. The audio
    process's target lives in this module, so it has no need of it, and a
    main script without an `if __name__ == "__main__":` guard (the Mac
    app's boot.py was one) would run all over again inside the audio
    process: a second web server, a second dialog. So for the moment the
    process starts, __main__ says nothing about where it came from, and
    the child is left with nothing to re-import."""
    main = sys.modules.get("__main__")
    with _MAIN_LOCK:
        saved = {}
        for attr in ("__file__", "__spec__"):
            if main is not None and attr in main.__dict__:
                saved[attr] = main.__dict__[attr]
        try:
            if main is not None:
                main.__dict__.pop("__file__", None)
                main.__dict__["__spec__"] = None
            p.start()
        finally:
            if main is not None:
                main.__dict__.pop("__spec__", None)
                main.__dict__.update(saved)


def engine_spec(acfg, checked, fake=None, platform=None):
    """What the audio process needs, as plain data it can be sent."""
    spec = {"device": acfg.device, "channels": acfg.channels,
            "rate": acfg.rate, "allow_shared": acfg.allow_shared_mode,
            "cues": {role: c["stems"] for role, c in checked.items()}}
    if fake is not None:
        spec["fake"] = fake
    if platform is not None:
        spec["platform"] = platform
    return spec


# ------------------------------------------------------ a stand-in device --
class _Flags:
    def __init__(self, underflow=False):
        self.output_underflow = underflow

    def __bool__(self):
        return self.output_underflow


class _TimeInfo:
    def __init__(self, dac, cur):
        self.outputBufferDacTime = dac
        self.currentTime = cur


class FakeSoundDevice:
    """A stand-in for the sounddevice module, for the tests and a dry run
    with no interface: no sound, no PortAudio. Selected only by a "fake"
    entry in the engine spec, which no show file can set.

    Like PortAudio, the device list it answers with is the one it saw at
    its last _initialize(); control() changes what is really attached.
    `threaded` streams call back from their own thread in real time;
    otherwise a test calls pump() itself, with its own clock."""

    class WasapiSettings:
        def __init__(self, exclusive=False):
            self.exclusive = exclusive

    def __init__(self, devices=None, hostapis=("Core Audio",), threaded=True,
                 block=480, latency=0.02, rates=(48000,), drift_ppm=0.0,
                 clock=None, keep=False, reported_latency=None):
        self.hostapis = list(hostapis)
        self.devices = [dict(d) for d in (devices or [
            {"name": "Fake Interface", "hostapi": 0,
             "max_output_channels": 8}])]
        for d in self.devices:
            d.setdefault("max_input_channels", 0)
            d.setdefault("default_samplerate", 48000.0)
            d.setdefault("rates", list(rates))
            d.setdefault("present", True)
        self.threaded = threaded
        self.block = block
        self.latency = latency
        # What stream.latency says, which on a real driver is often not
        # what the callback's own time info shows.
        self.reported_latency = latency if reported_latency is None \
            else reported_latency
        self.drift = 1.0 + drift_ppm * 1e-6
        self._clock = clock or time.perf_counter
        self.keep = keep
        self.streams = []
        self.blocks = []
        self.inits = 0
        self._listed = [d for d in self.devices if d["present"]]

    # the parts of sounddevice's API the show audio uses
    def query_hostapis(self):
        return tuple({"name": n} for n in self.hostapis)

    def query_devices(self):
        return [{k: v for k, v in d.items()
                 if k not in ("rates", "present", "busy")}
                for d in self._listed]

    def _dev(self, index):
        return self._listed[index]

    def check_output_settings(self, device=None, channels=None,
                              samplerate=None, dtype=None,
                              extra_settings=None):
        d = self._dev(device)
        if not d["present"]:
            raise RuntimeError("Device unavailable [PaErrorCode -9985]")
        if samplerate not in d["rates"]:
            raise RuntimeError("Invalid sample rate [PaErrorCode -9997]")
        if channels > d["max_output_channels"]:
            raise RuntimeError("Invalid number of channels")

    def OutputStream(self, device=None, channels=None, samplerate=None,
                     dtype=None, latency=None, callback=None,
                     extra_settings=None, finished_callback=None,
                     blocksize=None):
        d = self._dev(device)
        if not d["present"]:
            raise RuntimeError("Device unavailable [PaErrorCode -9985]")
        if d.get("busy"):
            raise RuntimeError("Unanticipated host error [PaErrorCode "
                               "-9999]: the endpoint is in use")
        s = _FakeStream(self, d, channels, samplerate, callback,
                        extra_settings)
        self.streams.append(s)
        return s

    def _terminate(self):
        pass

    def _initialize(self):
        self.inits += 1
        self._listed = [d for d in self.devices if d["present"]]

    # what a test or a dry run can do to it
    def control(self, what, *args):
        if what == "unplug":
            for d in self.devices:
                if not args or d["name"] == args[0]:
                    d["present"] = False
        elif what == "plug":
            for d in self.devices:
                if not args or d["name"] == args[0]:
                    d["present"] = True
        elif what == "add":
            self.devices.append({"name": args[0], "hostapi": 0,
                                 "max_output_channels": 2,
                                 "max_input_channels": 0,
                                 "default_samplerate": 48000.0,
                                 "rates": [48000], "present": True})
        elif what == "remove":
            self.devices = [d for d in self.devices if d["name"] != args[0]]
        elif what == "underflow":
            for s in self.streams:
                s.underflow_next = True
        elif what == "crash":
            os._exit(3)


class _FakeStream:
    def __init__(self, sd, dev, channels, rate, callback, extra):
        self.sd = sd
        self.dev = dev
        self.channels = channels
        self.rate = rate
        self.callback = callback
        self.extra = extra
        self.latency = sd.reported_latency
        self.running = False
        self.closed = False
        self.frames = 0
        self.underflow_next = False
        self._thread = None
        self.t0 = None

    @property
    def active(self):
        return self.running and not self.closed

    def start(self):
        self.running = True
        if self.sd.threaded:
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="fake-audio-device")
            self._thread.start()

    def pump(self, now):
        """One callback, as the device would make it at `now`. Returns the
        block, or None when the device has gone (a dead device stops
        calling back)."""
        import numpy as np
        if not self.running or self.closed or not self.dev["present"]:
            return None
        out = np.zeros((self.sd.block, self.channels), dtype=np.float32)
        flags = _Flags(self.underflow_next)
        self.underflow_next = False
        self.callback(out, self.sd.block,
                      _TimeInfo(now + self.sd.latency, now), flags)
        self.frames += self.sd.block
        if self.sd.keep:
            self.sd.blocks.append((now, out.copy()))
        return out

    def _run(self):
        clock = self.sd._clock
        self.t0 = clock()
        period = self.sd.block / float(self.rate) / self.sd.drift
        n = 0
        while self.running and not self.closed:
            due = self.t0 + n * period
            wait = due - clock()
            if wait > 0:
                time.sleep(min(wait, 0.01))
                continue
            if self.dev["present"]:
                self.pump(clock())
            n += 1

    def stop(self):
        self.running = False

    def abort(self):
        self.running = False

    def close(self):
        self.running = False
        self.closed = True
