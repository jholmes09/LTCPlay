"""SMPTE/EBU linear timecode (LTC) decoding from an audio stream.

LTC is 80 bits per video frame, biphase-mark coded: every bit cell begins with a
level transition, and a '1' adds a second transition at the centre of the cell.
So a long interval between transitions is a 0, and a pair of short intervals is
a 1.  The last 16 bits of every frame are the sync word 0011111111111101, which
is the only place that pattern can occur, so it both frames the data and tells
you which direction the tape is running.

Bit assignments follow SMPTE 12M.  Everything is transmitted least significant
bit first within each field.

This decoder is streaming: feed it blocks of mono float samples in order and it
returns whatever complete frames finished inside that block.
"""
import collections

SYNC_WORD = (0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 1)
BITS_PER_FRAME = 80

# (label, nominal fps, samples-per-frame is derived from the sample rate)
COMMON_RATES = (23.976, 24.0, 25.0, 29.97, 30.0)


class LTCFrame:
    __slots__ = ("h", "m", "s", "f", "drop", "start_sample", "end_sample")

    def __init__(self, h, m, s, f, drop, start_sample, end_sample):
        self.h, self.m, self.s, self.f = h, m, s, f
        self.drop = drop
        self.start_sample = start_sample   # absolute sample index of bit 0
        self.end_sample = end_sample       # absolute sample index after bit 79

    def frame_number(self, fps):
        """Absolute frame count, non-drop arithmetic."""
        return int(round(((self.h * 60 + self.m) * 60 + self.s) * fps)) + self.f

    def seconds(self, fps):
        return self.frame_number(fps) / fps

    def __str__(self):
        sep = ";" if self.drop else ":"
        return f"{self.h:02d}:{self.m:02d}:{self.s:02d}{sep}{self.f:02d}"

    __repr__ = __str__


def _bcd(bits, lo, n):
    """Little-endian binary field of n bits starting at index lo."""
    v = 0
    for i in range(n):
        v |= bits[lo + i] << i
    return v


def decode_bits(bits):
    """bits: list of 80 ints, bit 0 first. Returns LTCFrame fields or None."""
    if tuple(bits[64:80]) != SYNC_WORD:
        return None
    frame = _bcd(bits, 0, 4) + _bcd(bits, 8, 2) * 10
    sec = _bcd(bits, 16, 4) + _bcd(bits, 24, 3) * 10
    mins = _bcd(bits, 32, 4) + _bcd(bits, 40, 3) * 10
    hours = _bcd(bits, 48, 4) + _bcd(bits, 56, 2) * 10
    drop = bool(bits[10])
    # A reading outside these ranges means we locked onto noise that happened
    # to end in the sync pattern.  Reject rather than hand the player a jump.
    if frame > 39 or sec > 59 or mins > 59 or hours > 23:
        return None
    return hours, mins, sec, frame, drop


class LTCDecoder:
    """Streaming biphase-mark decoder with an adaptive bit-period estimate."""

    def __init__(self, sample_rate, hysteresis=0.02):
        self.sample_rate = float(sample_rate)
        self.hysteresis = hysteresis
        self._state = 0              # current sign, +1 or -1, 0 until armed
        self._last_edge = 0          # absolute sample index of the last transition
        self._pos = 0                # absolute sample index of the next sample
        self._bits = collections.deque(maxlen=BITS_PER_FRAME)
        self._bit_edges = collections.deque(maxlen=BITS_PER_FRAME)
        self._pending_short = None   # first half of a possible '1'
        self._half = None            # running estimate of a half-bit in samples
        self._thr = None             # short/long decision boundary, in samples
        # The two interval classes differ by exactly 2x, so rather than seeding
        # from one arbitrary interval (which lands on the wrong cluster half the
        # time) we classify against the spread of a recent window.  The sync
        # word alone puts both classes inside any 80-bit span, so a 64-interval
        # window always sees both.
        self._win = collections.deque(maxlen=64)
        self._since_recalc = 0
        self.frames_decoded = 0
        self.sync_errors = 0
        self._max_frame_seen = None
        self._prev_frame_val = None
        self._rollover_fps = None
        # Rate measured against the audio sample clock.  The frame digits alone
        # cannot separate 29.97 from 30.00, because both count to 30; the
        # interval between frames can, because 0.1% over a few seconds is
        # thousands of samples and a sound card's own clock is off by tens of
        # parts per million, not a thousand.
        self._anchor = None          # (frame index, end sample, minute key)
        self._prev_idx = None
        self._measured = None
        self._measured_span = 0.0
        self.last_drop = False
        self.last_frame = None

    # -- internals --------------------------------------------------------
    def _push_bit(self, bit, edge_sample):
        self._bits.append(bit)
        self._bit_edges.append(edge_sample)

    def _try_frame(self):
        if len(self._bits) < BITS_PER_FRAME:
            return None
        bits = list(self._bits)
        got = decode_bits(bits)
        if got is None:
            return None
        h, m, s, f, drop = got
        end = self._bit_edges[-1]
        # Bit 0 began one full bit period before the edge that ended it, so
        # anchor off the end of the sync word rather than the first stored edge.
        bit_period = (self._half or 0.0) * 2.0
        start = end - BITS_PER_FRAME * bit_period
        self.frames_decoded += 1
        if self._max_frame_seen is None or f > self._max_frame_seen:
            self._max_frame_seen = f
        # The rate is only known for certain at a second boundary: the frame
        # value just before it rolls to 0 is the last frame of that second, so
        # the rate is that value plus one.  Counting the largest digit seen is
        # not enough, because a short burst of timecode may never reach it.
        if self._prev_frame_val is not None and f == 0 and self._prev_frame_val > 0:
            cand = self._prev_frame_val + 1
            if cand in (24, 25, 30):
                self._rollover_fps = cand
        self._prev_frame_val = f
        self.last_drop = drop
        self._note_rate(h, m, s, f, end)
        # Consume the frame so the next sync has to be a fresh 80 bits; without
        # this a single frame would re-report on every subsequent bit.
        self._bits.clear()
        self._bit_edges.clear()
        return LTCFrame(h, m, s, f, drop, start, end)

    MIN_RATE_WINDOW_S = 2.0

    def _note_rate(self, h, m, s, f, end):
        """Measure the real frame rate against the sample clock.

        Anchored inside a single minute, so drop frame's skipped counts never
        enter the arithmetic.

        The window has to restart whenever the frame numbers stop advancing,
        not only when they go backwards.  A parked deck sends the same frame
        over and over: the numerator freezes while the denominator keeps
        growing, and the measured rate slides smoothly down through every
        plausible value on its way to zero.  Comparing against the PREVIOUS
        frame rather than against the anchor is what catches that, and it is
        how a four second pause used to turn a 30 fps source into a confident
        29.97 on screen."""
        n = self._rollover_fps
        if not n:
            self._anchor = None
            self._prev_idx = None
            return
        idx = ((h * 60 + m) * 60 + s) * n + f
        key = (h, m)
        minute_changed = self._anchor is not None and self._anchor[2] != key
        stalled = self._prev_idx is not None and idx <= self._prev_idx
        self._prev_idx = idx
        if self._anchor is None or minute_changed or stalled:
            self._anchor = (idx, end, key)
            if stalled:
                # A stall or a jump means the last measurement described a feed
                # that no longer exists, so the window is void until a new one
                # matures. A minute rollover does not: the window restarts
                # because drop frame arithmetic does, and the figure already
                # measured still stands until a new one replaces it.
                #
                # Zeroing the span is the ONLY thing that hides the old figure,
                # in measured_fps and detected_rate alike. Clearing _measured
                # here as well would work and would also make that guard
                # untestable, which is how a second mechanism quietly becomes
                # the only one and the first rots.
                self._measured_span = 0.0
            return
        d_frames = idx - self._anchor[0]
        d_samples = end - self._anchor[1]
        if d_samples <= 0:
            return
        span = d_samples / self.sample_rate
        if span < self.MIN_RATE_WINDOW_S:
            return          # too short a window to resolve a tenth of a percent
        self._measured = d_frames / span
        self._measured_span = span

    def _edge(self, idx):
        """A level transition at absolute sample index idx."""
        if self._last_edge == 0 and self._half is None:
            self._last_edge = idx
            return None
        interval = idx - self._last_edge
        self._last_edge = idx
        if interval <= 0:
            return None

        # Plausibility gate: an LTC half-bit at 23.976..31 fps sits inside this
        # range at any sane sample rate.  Anything outside is a dropout, a click
        # or silence, and must not poison the window.
        lo_bound = self.sample_rate / (BITS_PER_FRAME * 31.0 * 2.0) * 0.5
        hi_bound = self.sample_rate / (BITS_PER_FRAME * 23.0) * 1.5
        if not (lo_bound <= interval <= hi_bound):
            self._pending_short = None
            return None

        self._win.append(interval)
        self._since_recalc += 1
        if len(self._win) >= 32 and (self._thr is None or self._since_recalc >= 16):
            self._since_recalc = 0
            s = sorted(self._win)
            k = max(1, len(s) // 8)
            lo = s[k]            # bottom of the short cluster, outliers trimmed
            hi = s[-k - 1]       # top of the long cluster
            if hi >= lo * 1.5:   # both classes present, so the split is real
                self._thr = (lo + hi) / 2.0
                # Take the half-bit from the MEAN of the short cluster, not its
                # low percentile.  Whole-sample intervals straddle the true
                # half-bit (12 and 13 samples for a 12.5 sample half), and a
                # percentile always picks the low side, which biased the frame
                # rate estimate a whole step (24fps read as 25).
                shorts = [v for v in self._win if v <= self._thr]
                if shorts:
                    self._half = sum(shorts) / float(len(shorts))
        if self._thr is None:
            return None

        out = None
        if interval > self._thr:
            if self._pending_short is not None:
                # A short with no partner is corruption; drop it.
                self._pending_short = None
                self.sync_errors += 1
            out = (0, idx)
        else:
            if self._pending_short is None:
                self._pending_short = idx
                return None
            self._pending_short = None
            out = (1, idx)
        self._push_bit(out[0], out[1])
        return self._try_frame()

    # -- public -----------------------------------------------------------
    def feed(self, samples):
        """samples: sequence of floats in -1..1. Returns list of LTCFrame.

        Uses numpy to find the level transitions when it is available.  At
        48kHz the scalar loop is 48,000 Python iterations a second inside the
        audio callback; the vectorised path does the same work on the ~4,800
        transitions that actually exist, which is where the latency was."""
        try:
            import numpy as _np
        except Exception:
            return self._feed_scalar(samples)

        a = _np.asarray(samples, dtype=_np.float32)
        if a.size == 0:
            return []
        hy = self.hysteresis
        hi = a > hy
        lo = a < -hy
        live = _np.flatnonzero(hi | lo)
        pos = self._pos
        self._pos = pos + a.size
        if live.size == 0:
            return []
        vals = _np.where(hi[live], 1, -1).astype(_np.int8)

        edge_idx = []
        if self._state != 0 and vals[0] != self._state:
            edge_idx.append(int(live[0]))
        if vals.size > 1:
            change = _np.flatnonzero(vals[1:] != vals[:-1]) + 1
            edge_idx.extend(live[change].tolist())
        self._state = int(vals[-1])

        got = []
        for i in edge_idx:
            fr = self._edge(pos + i)
            if fr is not None:
                got.append(fr)
        return got

    def _feed_scalar(self, samples):
        got = []
        hy = self.hysteresis
        state = self._state
        pos = self._pos
        for i, v in enumerate(samples):
            if state >= 0:
                if v < -hy:
                    state = -1
                    fr = self._edge(pos + i)
                    if fr is not None:
                        got.append(fr)
                elif v > hy:
                    state = 1
            else:
                if v > hy:
                    state = 1
                    fr = self._edge(pos + i)
                    if fr is not None:
                        got.append(fr)
        self._state = state
        self._pos = pos + len(samples)
        return got

    @property
    def position(self):
        """Absolute index of the next sample the decoder has yet to see."""
        return self._pos

    @property
    def nominal_fps(self):
        """Integer frames per second, from the largest frame digit seen.

        Determined at a second rollover, so it needs up to one second of
        timecode before it reports anything.  It cannot separate 29.97 from 30
        (a 0.1% difference) and never claims to: set the rate in the timeline
        to what your source actually sends."""
        if self._rollover_fps is not None:
            return self._rollover_fps
        # Before a second boundary has been seen we only know a lower bound.
        # Reporting a guess here would let the player build a media position on
        # it, so say nothing instead.
        return None

    @property
    def measured_fps(self):
        """Real frame rate from the sample clock, or None until it is known.

        Needs two seconds of clean, advancing timecode.  This is the number
        that tells 29.97 from 30.00, and it is worth printing: a 30 fps show
        file chased by a 29.97 source slides a frame every 33 seconds, which is
        almost two seconds of lights-behind-music by the end of a half hour
        set.  None while the window is rebuilding, so nothing on screen is ever
        a stale figure presented as a live one."""
        if self._measured_span < self.MIN_RATE_WINDOW_S:
            return None
        return self._measured

    @property
    def measured_span(self):
        """Seconds of timecode behind the current measurement."""
        return self._measured_span

    # 0.04%. The gap between 29.97 and 30 (and between 23.976 and 24) is 0.1%,
    # so the tolerance has to be well under half of that or both candidates
    # qualify and the measurement decides nothing. A sound card's own clock is
    # off by tens of parts per million, a hundredth of this, so there is plenty
    # of room underneath.
    RATE_TOLERANCE = 0.0004

    @property
    def detected_rate(self):
        """Best single answer for the incoming rate: (rate, drop, confident).

        Never snaps to the nearest candidate regardless of distance. A
        measurement that matches nothing is a fact about the feed, not a vote
        for whichever rate happens to be closest, so it reports the frame count
        and confident=False instead of asserting a rate it cannot support."""
        n = self._rollover_fps
        mea = self._measured
        if n is None:
            return (None, self.last_drop, False)
        if mea is None or self._measured_span < self.MIN_RATE_WINDOW_S:
            return (float(n), self.last_drop, False)
        near = [r for r in COMMON_RATES
                if abs(round(r) - n) < 0.5 and abs(r - mea) / r < self.RATE_TOLERANCE]
        if len(near) == 1:
            return (near[0], self.last_drop, True)
        return (float(n), self.last_drop, False)

    @property
    def bit_rate_fps(self):
        """Rate implied by the bit period. Coarse: use it to sanity check the
        signal, never to compute a media position."""
        if not self._half:
            return None
        frame_samples = self._half * 2.0 * BITS_PER_FRAME
        if frame_samples <= 0:
            return None
        raw = self.sample_rate / frame_samples
        best = min(COMMON_RATES, key=lambda r: abs(r - raw))
        return best if abs(best - raw) / best < 0.05 else None


def synthesize(h, m, s, f, fps, sample_rate, frames=1, amplitude=0.5, drop=False,
               start_level=1.0, with_level=False):
    """Generate LTC audio for testing. Returns a list of floats.

    `start_level` and `with_level` let a long file be produced in chunks
    without a phase discontinuity at each join: pass the level the previous
    chunk ended on.  Building a whole 20 minute file in one list is about
    2 GB of Python floats, which is how this got an out-of-memory kill."""
    import math
    out = []
    # Count in frame NUMBERS, not in seconds times the rate. At 29.97 the two
    # differ: 01:00:00:00 is frame 108000 of a 30 count, not 107892, and using
    # the rate here walks the generated timecode back by nearly four seconds.
    count = int(round(fps))
    total_frames = ((h * 60 + m) * 60 + s) * count + f
    samples_per_frame = sample_rate / fps
    level = start_level
    carry = 0.0
    for k in range(frames):
        n = total_frames + k
        fr = int(n % round(fps))
        rest = int(n // round(fps))
        sec = rest % 60
        mins = (rest // 60) % 60
        hours = (rest // 3600) % 24
        bits = [0] * 80
        def put(val, lo, width):
            for i in range(width):
                bits[lo + i] = (val >> i) & 1
        put(fr % 10, 0, 4); put(fr // 10, 8, 2)
        put(sec % 10, 16, 4); put(sec // 10, 24, 3)
        put(mins % 10, 32, 4); put(mins // 10, 40, 3)
        put(hours % 10, 48, 4); put(hours // 10, 56, 2)
        bits[10] = 1 if drop else 0
        for i, b in enumerate(SYNC_WORD):
            bits[64 + i] = b
        # lay the 80 bits down as biphase mark across exactly one frame period
        for i, b in enumerate(bits):
            cell_start = k * samples_per_frame + i * samples_per_frame / 80.0
            cell_end = k * samples_per_frame + (i + 1) * samples_per_frame / 80.0
            level = -level                       # boundary transition
            if b:
                mid = (cell_start + cell_end) / 2.0
                while carry < mid:
                    out.append(level * amplitude); carry += 1.0
                level = -level                   # mid-cell transition
            while carry < cell_end:
                out.append(level * amplitude); carry += 1.0
    return (out, level) if with_level else out
