"""Audio input: finding the right one, holding onto it, and noticing when it goes.

A headphone jack is one device with one input and it is either there or it is
not.  A USB interface is none of those things, and every difference is a way
for a show to fail quietly:

  * It has several inputs.  Timecode arrives on exactly one of them, and
    listening to the wrong one looks identical to no signal at all.
  * Its index moves.  Unplug a webcam and the interface that was device 3 is
    device 2, so an index written down yesterday points somewhere else today.
  * It owns its own clock and may refuse the rate you ask for.
  * It can be unplugged, bumped, or put to sleep mid-show, and a dead input
    stream does not announce itself: the display keeps drawing and the
    timecode simply stops.

This module is the part that deals with all four.
"""
import threading
import time


# ------------------------------------------------------------- classifying ---
# A Mac accumulates audio inputs. Teams, Zoom, Loom, a webcam app and a
# loopback driver each install one, and Continuity adds the phone in your
# pocket. None of them can carry timecode, but they sit in the same list as the
# one device that can, and on a machine with nine inputs that list is noise
# rather than information.
#
# These patterns LABEL and ORDER devices. They never exclude one: a wrong guess
# that hides the real interface would be far worse than a cluttered list, so an
# unrecognised device is always treated as hardware and scanned first.
_VIRTUAL = ("blackhole", "soundflower", "loopback", "teams", "zoom", "loom",
            "webex", "discord", "obs", "krisp", "icontact", "camo", "ndi",
            "virtual", "aggregate", "multi-output", "vb-cable", "existential",
            "chrome", "screenflow", "descript", "riverside")
_BUILTIN = ("macbook", "imac", "mac mini", "mac studio", "built-in",
            "display audio")
# The headphone socket on a Mac becomes an INPUT only while a TRRS headset or
# adapter is in it, and macOS then shows it under one of these names. It is a
# separate answer from the built-in microphone and worth naming as such.
_JACK = ("external microphone", "headset microphone", "headphone",
         "usb-c to 3.5", "audio jack")
_PHONE = ("iphone", "ipad", "continuity")


def classify(name):
    n = (name or "").lower()
    for pat in _JACK:
        if pat in n:
            return "jack"
    for pat in _PHONE:
        if pat in n:
            return "phone"
    for pat in _VIRTUAL:
        if pat in n:
            return "virtual"
    for pat in _BUILTIN:
        if pat in n:
            return "built in"
    return "hardware"


_KIND_NOTE = {
    "virtual": "software device, cannot carry timecode from outside this Mac",
    "built in": "this Mac's own microphone",
    "jack": "the headphone socket, with a TRRS adapter in it",
    "phone": "your phone over Continuity",
    "hardware": "",
}

# The only kinds that can carry timecode in from the outside world. Everything
# else is a Mac talking to itself, and scanning it is nine seconds a person
# spends watching zeros.
CANDIDATE_KINDS = ("hardware", "jack", "built in")


def candidates(inputs):
    return [d for d in hardware_first(inputs) if d["kind"] in CANDIDATE_KINDS]


# ---------------------------------------------------------------- listing ---
def list_inputs(sd):
    """Every device that can capture, with what it can actually do."""
    out = []
    try:
        default_in = sd.default.device[0]
    except Exception:
        default_in = None
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) <= 0:
            continue
        name = d.get("name", "?")
        out.append({
            "index": i,
            "name": name,
            "channels": d["max_input_channels"],
            "rate": int(d.get("default_samplerate") or 48000),
            "default": i == default_in,
            "api": _api_name(sd, d),
            "kind": classify(name),
        })
    return out


def hardware_first(inputs):
    """Real interfaces, then the phone, then the built-in mic, then software."""
    order = {"hardware": 0, "jack": 1, "built in": 2, "phone": 3, "virtual": 4}
    return sorted(inputs, key=lambda d: (order.get(d["kind"], 0), d["index"]))


def _api_name(sd, d):
    try:
        return sd.query_hostapis(d["hostapi"])["name"]
    except Exception:
        return ""


class DeviceError(Exception):
    """Raised with the list of what IS there, because 'device not found' on its
    own is the least useful thing to read at a console."""


def resolve_device(sd, spec):
    """spec: None (system default), an index, or part of a name.

    Names are matched case-insensitively on a substring, because a name is
    stable across replug and an index is not.  An ambiguous name is an error
    rather than a guess: picking one of two interfaces for someone is how a
    show ends up chasing the wrong input."""
    inputs = list_inputs(sd)
    if not inputs:
        raise DeviceError("This Mac has no audio input at all. A USB interface "
                          "has to be plugged in and powered before it appears, "
                          "and the headphone jack is only an input while a TRRS "
                          "adapter is in it.")
    if spec is None or spec == "":
        for d in inputs:
            if d["default"]:
                return d
        return inputs[0]

    try:
        idx = int(spec)
    except (TypeError, ValueError):
        idx = None
    if idx is not None:
        for d in inputs:
            if d["index"] == idx:
                return d
        raise DeviceError(f"There is no input device {idx}.\n" + describe(inputs))

    want = str(spec).lower()
    hits = [d for d in inputs if want in d["name"].lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise DeviceError(f"No input device matches {spec!r}.\n" + describe(inputs))
    raise DeviceError(f"{spec!r} matches {len(hits)} inputs, so it is not "
                      f"specific enough:\n" + describe(hits))


def describe(inputs, group=True):
    if not group:
        lines = ["Inputs on this Mac:"]
        for d in inputs:
            lines.append(f"  [{d['index']}] {d['name']}   {d['channels']} in "
                         f"@ {d['rate']}Hz"
                         + ("   (system default)" if d["default"] else "")
                         + (f"   {d['api']}" if d["api"] else ""))
        return "\n".join(lines)

    real = candidates(inputs)
    rest = [d for d in hardware_first(inputs) if d["kind"] not in CANDIDATE_KINDS]
    lines = []
    if real:
        lines.append("Inputs that can carry timecode from outside this Mac:")
        for d in real:
            note = _KIND_NOTE.get(d["kind"], "")
            lines.append(f"  [{d['index']}] {d['name']}   {d['channels']} in "
                         f"@ {d['rate']}Hz"
                         + ("   (system default)" if d["default"] else "")
                         + (f"   {note}" if note else ""))
    else:
        lines.append("Nothing here can carry timecode from outside this Mac. "
                     "Everything below is\nsoftware or your phone. Plug the "
                     "interface in, or put a TRRS adapter in the\nheadphone "
                     "socket, and run this again.")
    if rest:
        lines.append("")
        lines.append("Everything else:")
        for d in rest:
            note = _KIND_NOTE.get(d["kind"], "")
            lines.append(f"  [{d['index']}] {d['name']}   {d['channels']} in "
                         f"@ {d['rate']}Hz"
                         + ("   (system default)" if d["default"] else "")
                         + (f"   {note}" if note else ""))
    return "\n".join(lines)


def negotiate_rate(sd, device, want, channels=1):
    """Return a rate the device will actually accept.

    An interface running its own clock at 44100 will refuse 48000 outright.
    Asking first and falling back beats a stack trace at the console."""
    tries = []
    if want:
        tries.append(int(want))
    if device["rate"] not in tries:
        tries.append(device["rate"])
    for r in (48000, 44100, 96000, 88200):
        if r not in tries:
            tries.append(r)
    for r in tries:
        try:
            sd.check_input_settings(device=device["index"], channels=channels,
                                    samplerate=r, dtype="float32")
            return r
        except Exception:
            continue
    raise DeviceError(f"{device['name']} refused every sample rate tried "
                      f"({', '.join(str(t) for t in tries)}).")


# ------------------------------------------------------------------ level ---
class Level:
    """Peak and a slow decay, so a glance tells you the trim is wrong.

    Timecode that is too quiet decodes nothing and timecode that clips decodes
    badly; both read on screen as 'the feed is broken' unless the level is
    shown next to it."""

    __slots__ = ("peak", "hold", "_decay_at", "clipped")

    def __init__(self):
        self.peak = 0.0
        self.hold = 0.0
        self.clipped = 0
        self._decay_at = time.monotonic()

    def feed(self, samples):
        try:
            p = float(max(abs(float(s)) for s in samples)) if len(samples) else 0.0
        except (TypeError, ValueError):
            return
        self.peak = p
        if p >= 0.99:
            self.clipped += 1
        now = time.monotonic()
        if p >= self.hold:
            self.hold = p
            self._decay_at = now
        elif now - self._decay_at > 1.5:
            self.hold = max(p, self.hold * 0.85)
            self._decay_at = now

    def feed_numpy(self, block):
        import numpy as np
        if block.size == 0:
            return
        p = float(np.max(np.abs(block)))
        self.peak = p
        if p >= 0.99:
            self.clipped += 1
        now = time.monotonic()
        if p >= self.hold:
            self.hold = p
            self._decay_at = now
        elif now - self._decay_at > 1.5:
            self.hold = max(p, self.hold * 0.85)
            self._decay_at = now

    def verdict(self):
        if self.hold < 0.02:
            return "silent"
        if self.hold < 0.05:
            return "very low"
        if self.clipped:
            return "clipping"
        if self.hold > 0.9:
            return "hot"
        return "ok"


# ----------------------------------------------------------------- source ---
class InputSource:
    """An input stream that puts itself back together.

    PortAudio does not raise when a USB interface is unplugged; the callbacks
    simply stop.  Nothing downstream can tell that from a timecode generator
    being switched off, so this watches its own callback clock and rebuilds the
    stream when it goes quiet, no faster than once a second."""

    SILENCE_BEFORE_REOPEN_S = 2.0
    REOPEN_BACKOFF_S = 1.0
    # How many failed opens before PortAudio itself is rebuilt. Low, because
    # every retry below this number is a second the rig is not chasing.
    FAILURES_BEFORE_RESET = 3

    def __init__(self, sd, device, channel, rate, blocksize, on_block, log=None,
                 on_rate_change=None):
        self.sd = sd
        self.device = device
        # The show starts whether or not this opens, so the first open is not
        # special: it is the first of however many it takes. on_rate_change
        # fires when the device that finally appears runs a different clock
        # from the one the decoder was built for, which would otherwise decode
        # as steady nonsense rather than as silence.
        self.on_rate_change = on_rate_change
        self.wanted_rate = rate
        self.attached = False
        self._fails_since_reset = 0
        self.pa_resets = 0
        self.down_since = None
        # (rate, channels) that last actually opened. Tried first every time.
        self._good = None
        self.channel = channel            # 1-based
        self.rate = rate
        self.blocksize = blocksize
        self.on_block = on_block
        self.log = log
        self.level = Level()
        self.blocks = 0
        self.reopens = 0
        self._opens = 0
        self.open_errors = 0
        self.last_error = ""
        self.last_error_at = None
        self.last_block_at = None
        self._stream = None
        self._running = False
        self._thread = None
        self._last_reopen_at = None
        # Ask for every channel up to the one we want, then take that column.
        # Opening `channels=1` gives input 1 and nothing else on most drivers,
        # which is why timecode on input 2 of an interface decodes as silence.
        self._open_channels = max(1, int(channel))

    # -- lifecycle --------------------------------------------------------
    def _callback(self, indata, frames, tinfo, status):
        try:
            self.last_block_at = time.monotonic()
            self.blocks += 1
            if indata.ndim > 1:
                col = min(self._open_channels, indata.shape[1]) - 1
                mono = indata[:, col]
            else:
                mono = indata
            self.level.feed_numpy(mono)
            self.on_block(mono, self.last_block_at)
        except Exception as e:
            # An exception out of a CoreAudio callback stops the stream with no
            # error anywhere a person can see it.
            self.last_error = f"{type(e).__name__}: {e}"
            self._event("audio", self.last_error, throttle=5.0)

    def _reindex(self):
        """Find this device again by NAME before reopening.

        Reopening by the index captured at start is how a replug ends in
        silence: macOS re-enumerates, the index that was the MOTU is now the
        webcam, the stream opens happily, decodes nothing, and the screen
        blames the cable. The name is what the operator chose and the name is
        what survives a replug. Found by an adversarial audit, 2026-09-13.
        """
        want = (self.device.get("name") or "").lower()
        if not want:
            # No name was ever chosen, so this is the system default input.
            # It has no name to follow and it may not exist yet: a Mac with
            # nothing plugged in has no input at all. Re-resolve it every
            # time rather than holding an index that meant something once.
            try:
                d = resolve_device(self.sd, None)
            except DeviceError:
                self.device = dict(self.device, index=None)
                return
            if d["channels"] >= self._open_channels:
                self.device = dict(self.device, index=d["index"],
                                   name=d["name"], channels=d["channels"],
                                   rate=d.get("rate"))
            else:
                self.device = dict(self.device, index=None)
            return
        try:
            devices = list(self.sd.query_devices())
        except Exception:
            return
        for i, d in enumerate(devices):
            if int(d.get("max_input_channels", 0)) < self._open_channels:
                continue
            if str(d.get("name", "")).lower() == want:
                self.device = dict(self.device,
                                   channels=int(d.get("max_input_channels", 0)),
                                   rate=int(d.get("default_samplerate")
                                            or self.device.get("rate") or 0)
                                        or self.device.get("rate"))
                if i != self.device.get("index"):
                    self._event("audio", f"{self.device['name']} came back as "
                                         f"device {i}, not "
                                         f"{self.device.get('index')}; "
                                         f"following the name")
                    self.device = dict(self.device, index=i)
                return
        # Still not there. Do not open SOMETHING ELSE on the old index.
        self.device = dict(self.device, index=None)

    def _reset_portaudio(self):
        """Tear PortAudio down and build it again.

        When CoreAudio's device list changes under a live PortAudio instance,
        which is what unplugging a USB interface or a Dante dock does, the
        cached device table goes stale and EVERY later open fails with
        paInternalError (-9986) for the life of the process. Reopening the
        stream cannot fix it; only re-initialising PortAudio can.

        Jeff pulled a USB-C dock carrying his network and his Dante input on
        2026-09-14. The supervisor retried once a second for minutes, logged
        -9986 every time, and Stop and Run did not help either, because the
        engine process survives a stop and the poisoned PortAudio went with
        it. Quitting the program was the only cure.
        """
        try:
            self.sd._terminate()
            self.sd._initialize()
        except Exception as e:
            self.last_error = f"could not rebuild the audio system: {e}"
            self._event("audio", self.last_error, throttle=10.0)
            return False
        self.pa_resets += 1
        # Every index PortAudio had is now meaningless. Force a fresh lookup.
        self.device = dict(self.device, index=None)
        self._good = None
        self._event("audio", f"the audio system was rebuilt after "
                             f"{self._fails_since_reset} failed opens "
                             f"(recovery #{self.pa_resets})")
        return True

    def _open(self):
        if self.down_since is None and self._opens:
            self.down_since = time.monotonic()
        # Reindex before every attempt except the very first one that already
        # holds a resolved device. A device that was absent at start has no
        # index at all, so waiting for _opens to be non-zero would never look
        # for it again and the input could never come back.
        if self._opens or self.device.get("index") is None:
            self._reindex()
        if self.device.get("index") is None:
            self.attached = False
            self.open_errors += 1
            named = self.device.get("name") or "The timecode input"
            self.last_error = (f"{named} is not attached. "
                               f"Nothing else will be opened in its place.")
            self._event("audio", self.last_error, throttle=5.0)
            self._stream = None
            self._fails_since_reset += 1
            if self._fails_since_reset >= self.FAILURES_BEFORE_RESET:
                self._fails_since_reset = 0
                self._reset_portaudio()
            return False
        # Candidate configurations, best first. The FIRST one is whatever
        # actually worked last time: re-negotiating the rate on every retry
        # made the player hunt 96000, 88200, 48000, 44100 over and over,
        # rebuilding the decoder each time, because check_input_settings
        # blesses rates that InputStream then refuses. Jeff's log,
        # 2026-09-14. check_input_settings is a hint; the open is the test.
        chans = int(self._open_channels)
        have = int(self.device.get("channels") or chans)
        tries = []

        def want(rate, ch):
            if rate and ch and ch >= chans and (rate, ch) not in tries:
                tries.append((rate, ch))

        if self._good:
            want(self._good[0], self._good[1])
        want(self.rate, chans)
        try:
            want(negotiate_rate(self.sd, self.device, self.wanted_rate,
                                channels=chans), chans)
        except Exception:
            pass
        # The whole ladder, ONCE per attempt. check_input_settings says yes to
        # rates AUHAL then refuses, so the open has to be the test. The
        # known-good entry above is what stops this running every retry.
        for r in (self.device.get("rate"), 48000, 44100, 96000, 88200):
            want(r, chans)
        # -10851, Invalid Property Value, is classically the channel count
        # rather than the rate: a Dante or aggregate device whose channel
        # count changed under us. Ask for what it says it has now.
        if have > chans:
            if self._good:
                want(self._good[0], have)
            for r in (self.rate, self.device.get("rate"), 48000, 44100,
                      96000, 88200):
                want(r, have)
        del tries[12:]          # a bounded hunt, not an open-ended one

        self._opens += 1
        s = None
        last = None
        for rate, ch in tries:
            try:
                s = self.sd.InputStream(device=self.device["index"],
                                        channels=ch,
                                        samplerate=rate,
                                        blocksize=self.blocksize,
                                        dtype="float32",
                                        callback=self._callback)
                s.start()
            except Exception as e:
                last = e
                s = None
                continue
            self._open_channels = ch
            if rate != self.rate:
                self._event("audio",
                            f"{self.device.get('name')} opened at {rate}Hz, "
                            f"not {self.rate}Hz; following the device")
                self.rate = rate
                if self.on_rate_change:
                    try:
                        self.on_rate_change(rate)
                    except Exception as e:
                        self._event("audio", f"rate change: {e}")
            self._good = (rate, ch)
            break

        if s is None:
            self.attached = False
            self.open_errors += 1
            self._fails_since_reset += 1
            self.last_error = f"open: {last}"
            self.last_error_at = time.monotonic()
            # Say what the device claims to be RIGHT NOW. PortAudio's own
            # output is a wall of AUHAL line numbers that names nothing.
            self._event("audio",
                        f"{self.device.get('name')} would not open. Tried "
                        + ", ".join(f"{r}Hz x{c}" for r, c in tries)
                        + f". The device reports {have} input(s) at "
                        f"{self.device.get('rate')}Hz. Last: {last}",
                        throttle=5.0)
            self._stream = None
            if self._fails_since_reset >= self.FAILURES_BEFORE_RESET:
                self._fails_since_reset = 0
                self._reset_portaudio()
            return False
        self._stream = s
        self.attached = True
        self._fails_since_reset = 0
        self.down_since = None
        self.last_error = ""
        self.last_block_at = time.monotonic()
        return True

    def _close(self):
        self.attached = False
        s, self._stream = self._stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:
                pass

    def _supervise(self):
        while self._running:
            time.sleep(0.25)
            now = time.monotonic()
            quiet = (self.last_block_at is None or
                     now - self.last_block_at > self.SILENCE_BEFORE_REOPEN_S)
            if not quiet and self._stream is not None:
                continue
            if self._last_reopen_at is not None and \
                    now - self._last_reopen_at < self.REOPEN_BACKOFF_S:
                continue
            self._last_reopen_at = now
            self._event("audio", f"no audio for "
                                 f"{self.SILENCE_BEFORE_REOPEN_S:.0f}s, "
                                 f"rebuilding the input")
            self._close()
            if self._open():
                self.reopens += 1

    def start(self):
        """Bring the input up, or start trying to.

        This does not raise. A missing interface used to refuse the whole
        start, which left the rig dark over a problem the preshow loop was
        built to cover. The show runs; this keeps knocking until the input
        answers. Asked for by Jeff, 2026-09-14.
        """
        self._running = True
        opened = self._open()
        self._thread = threading.Thread(target=self._supervise, daemon=True,
                                        name="ltcplay-audio")
        self._thread.start()
        return opened

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        self._close()

    # After this long of failing to open, the loop is not "recovering", it is
    # stuck, and saying "rebuilding the input" once a second is a lie that
    # reads as progress. Jeff watched exactly that for minutes.
    STUCK_AFTER_S = 20.0

    @property
    def seconds_since_error(self):
        if self.last_error_at is None:
            return None
        return time.monotonic() - self.last_error_at

    @property
    def seconds_down(self):
        if self.down_since is None:
            return None
        return time.monotonic() - self.down_since

    @property
    def stuck(self):
        d = self.seconds_down
        return d is not None and d >= self.STUCK_AFTER_S

    @property
    def seconds_since_block(self):
        if self.last_block_at is None:
            return None
        return time.monotonic() - self.last_block_at

    def _event(self, kind, msg, throttle=0.0):
        if self.log:
            try:
                self.log.event(kind, msg, throttle_s=throttle)
            except Exception:
                pass


# ------------------------------------------------------------------- find ---
def scan(sd, LTCDecoder, seconds=3.0, device=None, blocksize=512):
    """Listen to every input channel and report where timecode actually is.

    This exists because 'plug the interface in and set the device' hides the
    real question, which is WHICH of its inputs the timecode arrived on. On a
    four-in box that is a one-in-four guess, and a wrong guess is
    indistinguishable from a dead cable."""
    import numpy as np
    devices = [device] if device else candidates(list_inputs(sd))
    results = []
    for d in devices:
        chans = min(d["channels"], 8)     # past 8 this stops being useful
        entry = {"device": d, "channels": [], "error": None}
        try:
            rate = negotiate_rate(sd, d, None, channels=chans)
        except Exception as e:
            entry["error"] = str(e)
            results.append(entry)
            continue
        decs = [LTCDecoder(rate) for _ in range(chans)]
        levels = [Level() for _ in range(chans)]
        frames = [[] for _ in range(chans)]

        def cb(indata, n, tinfo, status, _d=decs, _l=levels, _f=frames):
            for c in range(min(chans, indata.shape[1] if indata.ndim > 1 else 1)):
                col = indata[:, c] if indata.ndim > 1 else indata
                _l[c].feed_numpy(col)
                for fr in _d[c].feed(col):
                    _f[c].append(fr)

        try:
            with sd.InputStream(device=d["index"], channels=chans,
                                samplerate=rate, blocksize=blocksize,
                                dtype="float32", callback=cb):
                time.sleep(seconds)
        except Exception as e:
            entry["error"] = str(e)
            results.append(entry)
            continue
        for c in range(chans):
            rate_seen, drop, confident = decs[c].detected_rate
            entry["channels"].append({
                "channel": c + 1,
                "level": levels[c].hold,
                "verdict": levels[c].verdict(),
                "frames": decs[c].frames_decoded,
                "last": str(frames[c][-1]) if frames[c] else None,
                "rate": rate_seen,
                "drop": drop,
                "confident": confident,
                "sync_errors": decs[c].sync_errors,
            })
        entry["rate"] = rate
        results.append(entry)
    return results
