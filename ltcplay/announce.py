"""Operator announcements: three pre-recorded messages, played one at a time
on their own output device, gated by whatever the scheduler says is true.

Imported ONLY when an announcements config is given (`ltc serve --announce`).
The GPL show never passes that, so on the Mac this module is never even
imported, and the selftest proves it, the same inertness the scheduler
itself relies on (see schedule_service.py).

This module does not decide whether a show is running; it ASKS. The
scheduler is the only thing that knows, and it may not even be configured, so
the answer comes through a one-method provider (`state_provider`, a plain
callable returning one of the scheduler's state names, or None) rather than a
hard import of schedule.py. Wiring that provider to the real scheduler is
web.py's job, once both are configured; this module never imports
schedule.py or schedule_service.py. See `interlock_refusal` for exactly what
that provider is used for.

A show starting while an announcement is playing is the OTHER half of that
same coupling, in the other direction: the scheduler ticks on a thread of its
own, with no lock shared with this service, so a status() poll here could be
seconds behind a show actually starting. `on_show_started` is a push hook
web.py wires the scheduler to call the moment it starts a show; it is not
something this module polls for. See SHOW_START_STOPS_ANNOUNCEMENT.

The interlock itself is check-then-act around real work (a device query, a
file read), so play() re-checks it a second time immediately before the
output stream actually starts, inside the same locked section: see the
comment in play() beside that second check.

The output device is never the system default. A show's device is picked by
config, by an EXACT name (never a substring: a substring can silently land on
a different physical device whose name merely contains the configured text),
and stays that device even if it briefly disappears: see
`resolve_output_device`. The real reason a name is required rather than a
default is section 10 of the handoff: the system default on the show PC is
whatever MadMapper's own show audio is on, and grabbing that silently is the
one failure this whole module exists to rule out.
"""
import json
import os
import threading
import time
import wave
from collections import deque
from datetime import datetime

from . import appdata
from . import settings as settings_mod

# Stable ids, never shown to an operator; LABELS is what the page prints.
DELAYED, CANCELLATION, CANNOT_CONTINUE = (
    "delayed", "cancellation", "cannot_continue")
IDS = (DELAYED, CANCELLATION, CANNOT_CONTINUE)
LABELS = {DELAYED: "Delayed", CANCELLATION: "Cancellation",
          CANNOT_CONTINUE: "Cannot continue"}

CONFIG_FILE = "ltcplay_announce.json"
OPERATORS_FILE = "ltcplay_operators.json"
DEFAULT_OPERATORS = ("Andy", "Jeff")

# Only these two scheduler states refuse a play. The moment Abort is pressed
# during either one, the scheduler's own state machine leaves that state (it
# lands in STANDBY), so nothing here has to remember that Abort happened: it
# falls out of asking the scheduler what is true right now.
BLOCKED_STATES = frozenset(("SHOW", "PAUSED"))

JOURNAL = 400

# Jeff has not signed off on this (see the PR's Questions section): the
# coordinator's default, implemented here, is that a show starting stops a
# playing announcement so the show starts on time. The alternative -- hold
# the show until the announcement finishes -- is deliberately NOT built.
# Flip this one constant to False and a show starts regardless of what is
# playing, exactly as if on_show_started did not exist.
SHOW_START_STOPS_ANNOUNCEMENT = True
# How long the announcement's own audio takes to ramp down once a show
# starts it over. The show itself is never held up by this: only the
# announcement's own output fades, in the background, on its own stream.
SHOW_START_FADE_S = 0.15

# A wrong file pointed at by mistake, or a device that has quietly stopped
# answering: both are caught by a plain limit rather than an unbounded read
# or an announcement that never lets the lock go.
MAX_FILE_BYTES = 50 * 1024 * 1024      # 50 MB: generous for a spoken line
MAX_LENGTH_S = 300.0                   # 5 minutes: a sentence, not a show
STALL_S = 3.0            # no callback at all for this long means the device
                         # has gone away, not that the file is just long
CALLBACK_ERROR_LIMIT = 20      # the device answers, but every time with a
                               # reported problem: also treated as a failure

_DTYPE_BY_SAMPWIDTH = {1: "uint8", 2: "int16", 4: "int32"}
_DASHES = ("—", "–")          # em dash, en dash


def _clean(text):
    """Strip em and en dashes from anything that ends up in operator facing
    text but did not originate as a sentence written by this module: an
    exception's own message, a path, an operator-typed screen name.
    CLAUDE.md: operator-facing text carries no em or en dash, and this
    module cannot vouch for what a path or a third-party error string
    contains."""
    s = str(text)
    for d in _DASHES:
        s = s.replace(d, "-")
    return s


# ---------------------------------------------------------- where things live
def data_dir():
    """The same machine folder the scheduler uses: beside the launcher on a
    Mac, %LOCALAPPDATA%\\ltcplay on Windows."""
    return appdata.folder() if appdata.WINDOWS else settings_mod.folder()


def default_config_path():
    return os.path.join(data_dir(), CONFIG_FILE)


def operators_path(folder=None):
    """The SAME file the scheduler reads (schedule_service.OPERATORS_FILE).
    Read directly, by name, rather than by importing schedule_service, so an
    announcements config alone never pulls the scheduler in."""
    return os.path.join(folder or data_dir(), OPERATORS_FILE)


def parse_operators(doc):
    if not isinstance(doc, dict) or set(doc) != {"operators"}:
        raise ValueError('It has to be {"operators": [names]} and nothing '
                         'else.')
    names = doc["operators"]
    if not isinstance(names, list) or not names:
        raise ValueError("operators has to be a list of at least one name.")
    out, seen = [], set()
    for n in names:
        if not isinstance(n, str) or not n.strip():
            raise ValueError(f"{n!r} is not a name.")
        if n.strip().lower() in seen:
            raise ValueError(f"{n.strip()!r} is on the list twice.")
        seen.add(n.strip().lower())
        out.append(n.strip())
    return tuple(out)


def load_operators(folder=None):
    """(names, sentence). A missing file quietly uses the defaults; whoever
    configures the scheduler is the one that writes it, so this never races
    to create it."""
    path = operators_path(folder)
    if not os.path.exists(path):
        return DEFAULT_OPERATORS, ""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            names = parse_operators(json.load(fh))
    except (OSError, ValueError) as e:
        return DEFAULT_OPERATORS, (
            f"The operator list {_clean(path)} could not be used: "
            f"{_clean(str(e)).rstrip('.')}. Using "
            f"{', '.join(DEFAULT_OPERATORS)} until it is fixed.")
    return names, ""


# ------------------------------------------------------------- the config --
def parse_config(doc, where):
    """{"device": name, "files": {"delayed": path, "cancellation": path,
    "cannot_continue": path}}. Raises ValueError with a sentence."""
    where = _clean(where)
    if not isinstance(doc, dict) or set(doc) != {"device", "files"}:
        raise ValueError(f'{where} has to be {{"device": name, "files": '
                         f'{{...}}}} and nothing else.')
    device = doc.get("device")
    if not isinstance(device, str) or not device.strip():
        raise ValueError(f"{where}: device has to be a named output "
                         f"device. There is no default; announcements "
                         f"never use the system output.")
    files = doc.get("files")
    if not isinstance(files, dict) or set(files) != set(IDS):
        raise ValueError(f"{where}: files has to name exactly "
                         f"{', '.join(IDS)}.")
    for aid in IDS:
        v = files.get(aid)
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{where}: files.{aid} has to be a file path.")
    return device.strip(), {k: v.strip() for k, v in files.items()}


# ---------------------------------------------------------------- devices --
def list_outputs(sd):
    """Every device that can play sound, the way audio.list_inputs lists
    devices that can capture it."""
    out = []
    try:
        default_out = sd.default.device[1]
    except Exception:
        default_out = None
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_output_channels", 0) <= 0:
            continue
        out.append({"index": i, "name": d.get("name", "?"),
                    "channels": d["max_output_channels"],
                    "rate": int(d.get("default_samplerate") or 48000),
                    "default": i == default_out})
    return out


def resolve_output_device(sd, name):
    """By an EXACT name, case-insensitive, never by index and never by a
    partial match. An index shifts when something else on the machine
    unplugs or replugs, and following the name is what lets a device that
    goes away mid-run come back on its own; a SUBSTRING match, this
    function's previous behaviour, can silently land on a completely
    different physical device whose Windows-assigned name merely happens to
    contain the configured text (found in review: audit13_device_wrong_
    match.py, where the show's own DSP output was matched by mistake). No
    name at all is a config error, not a fallback to the system default."""
    if not name or not str(name).strip():
        raise ValueError("No output device is named for announcements. "
                         "There is no default: announcements never use "
                         "the system output, which is the device the show "
                         "audio is on.")
    outputs = list_outputs(sd)
    want = str(name).strip().lower()
    hits = [d for d in outputs if d["name"].strip().lower() == want]
    if len(hits) == 1:
        return hits[0]
    names = ", ".join(d["name"] for d in outputs) or "nothing"
    if not hits:
        raise ValueError(f"{_clean(name)!r} is not attached. Nothing else "
                         f"will be used in its place. Outputs on this "
                         f"machine: {_clean(names)}.")
    raise ValueError(f"{_clean(name)!r} is the exact name of {len(hits)} "
                     f"outputs on this machine, so it is not specific "
                     f"enough: {_clean(names)}.")


# ------------------------------------------------------------- the interlock
def interlock_refusal(state):
    """None when an announcement may play; a plain sentence otherwise.

    `state` is whatever the scheduler's provider returns: one of its state
    names, or None when there is no scheduler configured, or one is
    configured but has not settled on a state yet. With no state to read,
    the safe answer is to refuse: an announcement that could play without
    knowing whether a show is running is worse than one that never plays at
    all, so announcements are inert until a scheduler is wired in.
    """
    if state is None:
        return ("No scheduler is configured, so ltcplay does not know "
                "whether a show is running. Announcements are inert until "
                "the scheduler is set up.")
    if state in BLOCKED_STATES:
        how = "paused" if state == "PAUSED" else "running"
        return (f"A show is {how}. Press Abort first, or wait for the "
                f"show to end, before playing an announcement.")
    return None


# ------------------------------------------------------------------ wav I/O
def _wav_format_tag(path):
    """The format tag from the WAV's own fmt chunk: 1 is PCM, 3 is IEEE
    float, 0xFFFE is extensible with a further sub-format. Python's `wave`
    module always assumes PCM and never looks at this, so a 32-bit float
    file opens exactly like a 32-bit int one, and its sample bytes would be
    reinterpreted as integers: loud noise, with no error anywhere (review:
    audit13_probe_weaker_than_open.py, part b). Returns None if it cannot be
    read, which is treated as PCM, the safer reading of a file the standard
    library reader already accepted."""
    try:
        with open(path, "rb") as fh:
            riff = fh.read(12)
            if len(riff) < 12 or riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
                return None
            while True:
                header = fh.read(8)
                if len(header) < 8:
                    return None
                chunk_id = header[:4]
                size = int.from_bytes(header[4:8], "little")
                if chunk_id == b"fmt ":
                    body = fh.read(2)
                    return (int.from_bytes(body, "little")
                            if len(body) == 2 else None)
                fh.seek(size + (size & 1), 1)
    except OSError:
        return None


def _wav_info(path, decode=True):
    """Open, validate, and (when `decode`) fully read a WAV file:
    (pcm_or_None, channels, rate, length_s). Raises ValueError with a plain
    sentence for anything that could not actually be played, so the startup
    probe (`decode=False`) and a real Play press (`decode=True`) can never
    disagree: they are the same function.

    The format tag is checked BEFORE Python's own `wave` module ever opens
    the file. `wave` rejects a non-PCM file on its own on a modern Python,
    but with its own message ("unknown format: 3"), which is not a
    sentence an operator should have to read, and tying this module's
    wording to whatever a given Python version's `wave` module happens to
    check would be exactly the kind of disagreement between the probe and
    a real Play press that this function exists to rule out (review:
    audit13_probe_weaker_than_open.py, part b)."""
    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise ValueError(f"{_clean(path)} could not be read: "
                         f"{_clean(str(e))}.")
    if size > MAX_FILE_BYTES:
        raise ValueError(f"{_clean(path)} is {size / 1e6:.0f} MB, over "
                         f"the {MAX_FILE_BYTES / 1e6:.0f} MB limit for an "
                         f"announcement.")
    tag = _wav_format_tag(path)
    if tag == 3:
        raise ValueError(f"{_clean(path)} is a 32-bit floating point WAV, "
                         f"which is not supported. Export 16-bit or "
                         f"32-bit PCM (integer), not float, instead.")
    if tag is not None and tag not in (1, 0xFFFE):
        raise ValueError(f"{_clean(path)} is WAV format {tag}, which is "
                         f"not supported. Export 16-bit or 32-bit PCM "
                         f"instead.")
    try:
        with wave.open(path, "rb") as w:
            n = w.getnframes()
            rate = w.getframerate()
            channels = w.getnchannels()
            sampwidth = w.getsampwidth()
            raw = w.readframes(n) if decode else b""
    except (OSError, EOFError, wave.Error) as e:
        raise ValueError(f"{_clean(path)} could not be read as a WAV "
                         f"file: {_clean(str(e))}.")
    if sampwidth not in _DTYPE_BY_SAMPWIDTH:
        raise ValueError(f"{_clean(path)} is a {sampwidth * 8}-bit WAV, "
                         f"which is not supported. Export 16-bit or "
                         f"32-bit PCM instead.")
    length_s = n / float(rate) if rate else 0.0
    if length_s > MAX_LENGTH_S:
        raise ValueError(f"{_clean(path)} is {length_s:.0f} s long, over "
                         f"the {MAX_LENGTH_S:g} s limit for an "
                         f"announcement.")
    if not decode:
        return None, channels, rate, length_s
    import numpy as np
    np_dtype = {"uint8": np.uint8, "int16": np.int16,
               "int32": np.int32}[_DTYPE_BY_SAMPWIDTH[sampwidth]]
    pcm = np.frombuffer(raw, dtype=np_dtype)
    pcm = pcm.reshape(-1, channels) if channels > 1 else pcm.reshape(-1, 1)
    return pcm, channels, rate, length_s


# ------------------------------------------------------------------ player --
class _Player:
    """How much of a loaded announcement has actually been handed to the
    output device, and whether the device is still actually taking it.

    Driven by the real audio callback in production, one block at a time. A
    test drives it the same way, by calling next_block itself: no clock
    inside the frame math, no thread, no sleep needed to prove what a block
    of audio did. last_progress_at is the one place real time enters, and
    only to notice a device that has stopped calling back at all (see
    AnnounceService._settle and STALL_S); it is always read through an
    injectable clock so a test never has to sleep for real seconds to prove
    a stall is caught.
    """

    __slots__ = ("pcm", "channels", "rate", "total_frames", "frames_written",
                "done", "_clock", "last_progress_at", "callback_errors",
                "_fade_total", "_fade_pos", "stop_reason")

    def __init__(self, pcm, channels, rate, clock=None):
        self.pcm = pcm
        self.channels = channels
        self.rate = rate
        self.total_frames = pcm.shape[0]
        self.frames_written = 0
        self.done = self.total_frames == 0
        # perf_counter, not monotonic: player.py switched to it (as _now())
        # for the same reason this file should not disagree with it --
        # time.monotonic() ticks in ~15.6 ms steps on Windows under
        # Python 3.12, too coarse once any timestamp here is ever compared
        # against one of ltcplay's own. Nothing here needs that
        # resolution yet (STALL_S is 3 s), but there is no reason to be
        # the one clock in the codebase that could disagree.
        self._clock = clock or time.perf_counter
        self.last_progress_at = self._clock()
        self.callback_errors = 0
        self._fade_total = 0
        self._fade_pos = 0
        # Set by AnnounceService when something other than the file simply
        # ending is why playback is about to stop ("a show started", for
        # instance). None means a natural finish.
        self.stop_reason = None

    def start_fade(self, frames):
        """Begin fading out over the given number of frames; once that many
        more frames have been produced, `done` becomes True even if the
        file itself has not finished."""
        self._fade_total = max(1, int(frames))
        self._fade_pos = 0

    def next_block(self, n, status=None):
        import numpy as np
        self.last_progress_at = self._clock()
        if status:
            self.callback_errors += 1
        start = self.frames_written
        end = min(start + n, self.total_frames)
        block = np.zeros((n, self.channels), dtype=self.pcm.dtype)
        if end > start:
            block[:end - start] = self.pcm[start:end]
        self.frames_written = end
        if self._fade_total:
            take = min(n, self._fade_total - self._fade_pos)
            if take > 0:
                ramp = 1.0 - (np.arange(self._fade_pos, self._fade_pos + take)
                             / float(self._fade_total))
                block[:take] = (block[:take].astype(np.float64)
                               * ramp[:, None]).astype(block.dtype)
            if take < n:
                block[take:] = 0
            self._fade_pos += n
            if self._fade_pos >= self._fade_total:
                self.done = True
        elif end >= self.total_frames:
            self.done = True
        return block

    @property
    def elapsed_s(self):
        if not self.rate:
            return 0.0
        return self.frames_written / float(self.rate)


# ------------------------------------------------------------------ service
class AnnounceService:
    """One of these per running program, or none at all when `--announce`
    was never passed. Holds the three announcements, the output device, and
    the single play/stop interlock."""

    GET_ROUTES = ("/api/announce/status",)
    POST_ROUTES = ("/api/announce/play", "/api/announce/stop")

    def __init__(self, config_path, sd=None, state_provider=None,
                 operators_folder=None, clock=None):
        self.config_path = config_path
        self._sd_obj = sd
        # A plain callable, or None. See interlock_refusal and the module
        # docstring: this is the whole coupling to the scheduler in the
        # "may I play" direction.
        self.state_provider = state_provider
        # Where to read ltcplay_operators.json from. None means the real
        # machine folder (data_dir()); a test points this at a tempdir so
        # it never touches, or depends on, anything really on disk.
        self.operators_folder = operators_folder
        # perf_counter, injectable so the stall watchdog never needs a
        # test to sleep for real seconds; see the same note in _Player.
        self._clock = clock or time.perf_counter
        self.lock = threading.RLock()
        self.journal = deque(maxlen=JOURNAL)
        self.error = ""
        self.device_name = None
        self.files = {}
        self.status_by_id = {}
        # Bumped on every claim (a Play that gets past the interlock and
        # the operator checks) and on every teardown (_finish). Phase B of
        # play() compares against the value it captured at claim time, not
        # self.playing's VALUE, so a Play that was Stopped and then played
        # again for the SAME id cannot be mistaken for the same attempt
        # (review round 2, should-fix 2: audit13b_reentrant_claim.py).
        self._claim_gen = 0
        self.operators = DEFAULT_OPERATORS
        self.last_good_device = None
        self.playing = None
        self._player = None
        self._stream = None
        self._load()

    # -- loading, once, at startup ---------------------------------------
    def _load(self):
        self.operators, why = load_operators(self.operators_folder)
        if why:
            self._emit(actor="system", action="operators", outcome="note",
                       reason=why, text=why)
        try:
            with open(self.config_path, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
        except FileNotFoundError:
            self.error = (f"There is no announcements file at "
                         f"{_clean(self.config_path)}.")
        except OSError as e:
            self.error = (f"The announcements file "
                         f"{_clean(self.config_path)} cannot be read: "
                         f"{_clean(str(e))}.")
        except ValueError as e:
            self.error = (f"The announcements file "
                         f"{_clean(self.config_path)} is not valid JSON: "
                         f"{_clean(str(e))}.")
        else:
            try:
                self.device_name, files = parse_config(doc, self.config_path)
            except ValueError as e:
                self.error = str(e)
            else:
                base = os.path.dirname(os.path.abspath(self.config_path))
                for aid in IDS:
                    raw = files[aid]
                    self.files[aid] = (raw if os.path.isabs(raw) else
                                       os.path.join(base, raw))
        for aid in IDS:
            if self.error:
                self.status_by_id[aid] = {"available": False,
                                          "reason": self.error,
                                          "length_s": None}
            else:
                self.status_by_id[aid] = self._probe_file(self.files[aid])

    @staticmethod
    def _probe_file(path):
        """A missing or unreadable file is decided HERE, once, so a press
        later never has to discover it: the button is already grey with a
        reason by the time anyone could press it. Shares _wav_info with the
        code that actually plays a file (see _decode), so nothing can be
        reported available here and then fail at the press (review:
        audit13_probe_weaker_than_open.py)."""
        if not os.path.exists(path):
            return {"available": False,
                    "reason": f"{_clean(path)} does not exist.",
                    "length_s": None}
        try:
            _pcm, _channels, _rate, length_s = _wav_info(path, decode=False)
        except ValueError as e:
            return {"available": False, "reason": str(e), "length_s": None}
        return {"available": True, "reason": None, "length_s": length_s}

    # -- audio ------------------------------------------------------------
    def _sd(self):
        if self._sd_obj is None:
            from .session import _import_sounddevice
            self._sd_obj = _import_sounddevice()
        return self._sd_obj

    def _probe_device(self):
        if self.error:
            return False, self.error
        try:
            sd = self._sd()
        except Exception as e:
            return False, (f"The audio system could not be reached: "
                           f"{_clean(str(e))}")
        try:
            resolve_output_device(sd, self.device_name)
        except ValueError as e:
            return False, str(e)
        return True, None

    def _decode(self, ann_id):
        """Read and validate the file off disk: the one part of a Play
        press that touches disk. Deliberately called WITHOUT self.lock held
        (see play()): self.files never changes after startup, so this is
        safe to run unlocked, and it must never hold up Stop, a status
        poll, or another operator's press for as long as it takes (review,
        minor: do not read a big file while holding the lock)."""
        pcm, channels, rate, _length_s = _wav_info(self.files[ann_id],
                                                    decode=True)
        return pcm, channels, rate

    # -- the journal, one call site ---------------------------------------
    def _emit(self, *, actor, action, outcome, reason, text, ann_id=None,
              who=None, screen=None, state=None):
        """The one place a log row is produced. A logging PR is being built
        in parallel to redirect this to the two stream logger in section 9;
        until then it feeds this service's own journal, in the scheduler's
        own shape (actor, action, outcome, reason, a plain sentence).

        text and reason are run through _clean() HERE too, as a backstop:
        every call site that builds one from a path or an exception's own
        message is expected to clean it itself, but load_operators() once
        did not (review round 2, minor), and a future call site could miss
        it again. Routing every operator-facing sentence through this one
        helper, on top of cleaning at the source, is what actually closes
        that class of leak rather than trusting each call site to remember."""
        row = {"at": datetime.now().astimezone().isoformat(
                   timespec="seconds"),
               "actor": actor, "action": action, "outcome": outcome,
               "reason": _clean(reason) if reason else reason,
               "text": _clean(text) if text else text, "who": who or None,
               "screen": screen or None, "show_state": state,
               "announcement": ann_id,
               "file": self.files.get(ann_id) if ann_id else None}
        self.journal.append(row)
        return row

    def _current_state(self):
        if self.state_provider is None:
            return None
        try:
            return self.state_provider()
        except Exception:
            return None

    @staticmethod
    def _operator_problem(who, screen):
        missing = " and ".join(
            x for x, v in (("who pressed it", who),
                          ("which screen it came from", screen)) if not v)
        return missing or None

    # -- playing ------------------------------------------------------------
    def _finish(self):
        # Every teardown ends an epoch, not just a new claim: a stale,
        # still-decoding Play attempt for the SAME id must never mistake
        # itself for the current claim just because self.playing happens
        # to read the same value again by the time it reacquires the lock.
        self._claim_gen += 1
        stream, self._stream = self._stream, None
        self._player = None
        self.playing = None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass

    def _settle(self):
        """Notice a file that finished, was faded out because a show
        started, or has simply stopped answering, since the last look.

        Real playback runs on the audio driver's own thread; ltcplay only
        learns about any of this the next time something asks (a status
        poll, another press), the same way the scheduler only advances when
        ticked. The page already polls status continuously, which is what
        drives this in practice, and on_show_started puts the fade in
        motion synchronously the moment a show starts, so the
        announcement's OWN audio stops in real time regardless of when this
        catches up.
        """
        if self._player is None or self.playing is None:
            return
        ann_id = self.playing
        label = LABELS[ann_id]
        if self._player.done:
            elapsed = self._player.elapsed_s
            stop_reason = self._player.stop_reason
            state = self._current_state()
            self._finish()
            if stop_reason:
                self._emit(actor="system", action="stop", outcome="stopped",
                          reason=stop_reason,
                          text=f"{label} was stopped after {elapsed:.0f} s, "
                               f"faded over {SHOW_START_FADE_S:g} s, "
                               f"because {stop_reason}.",
                          ann_id=ann_id, state=state)
            else:
                self._emit(actor="system", action="play", outcome="finished",
                          reason="finished normally",
                          text=f"{label} finished playing, {elapsed:.0f} s, "
                               f"finished normally.",
                          ann_id=ann_id, state=state)
            return
        stalled = (self._clock() - self._player.last_progress_at) > STALL_S
        too_many_errors = self._player.callback_errors >= CALLBACK_ERROR_LIMIT
        if stalled or too_many_errors:
            elapsed = self._player.elapsed_s
            why = (f"no audio for over {STALL_S:g} s" if stalled else
                  f"the output device reported a problem on "
                  f"{self._player.callback_errors} callbacks in a row")
            state = self._current_state()
            self._finish()
            self._emit(actor="system", action="play", outcome="failed",
                      reason=why,
                      text=f"{label} stopped answering after "
                           f"{elapsed:.0f} s ({why}); the output device "
                           f"may have gone away. Marked failed so another "
                           f"announcement can be played.",
                      ann_id=ann_id, state=state)

    def on_show_started(self, state=None):
        """Called by whoever wires this service to the scheduler (web.py's
        serve(), see its module docstring) the instant the scheduler
        decides to start a show. A push hook, not something this module
        polls for: schedule_service.py ticks on a thread of its own with no
        lock shared with this service, so an announcement that must stop
        the moment a show starts has to be told, not merely discovered on
        the next status() poll (review, blocker 1b).

        This method must return almost instantly and must NEVER touch the
        output stream. It is called from inside schedule_service.py's own
        _apply(), which is very possibly still nested inside
        Service.lock at the moment it runs (schedule_service.py queues
        the call and only invokes it after that lock is fully released,
        see Service._locked, but this method must not depend on that for
        its own safety either). All it does is flip in-memory state: start
        the fade and record why. Tearing the stream down for real
        (stream.stop()/close(), which can block on real device I/O, as
        round 2 of review found with a synthetic 2 s hang) happens
        strictly on this module's OWN path -- the next status() poll,
        Play, Stop, or the stall watchdog in _settle() -- never here
        (review round 2, blocker 1a: audit13b_scheduler_stall.py).

        See SHOW_START_STOPS_ANNOUNCEMENT for the one place the decision
        to stop at all lives; it is not Jeff's call yet.
        """
        with self.lock:
            if not SHOW_START_STOPS_ANNOUNCEMENT:
                return
            if self.playing is None or self._player is None:
                return
            if self._player.stop_reason is not None:
                return                      # already stopping
            fade_frames = max(1, int(SHOW_START_FADE_S *
                                     (self._player.rate or 1)))
            self._player.stop_reason = "a show started"
            self._player.start_fade(fade_frames)

    def play(self, ann_id, who, screen):
        who = _clean((who or "").strip())
        screen = _clean((screen or "").strip())
        if ann_id not in IDS:
            raise ValueError(f"{ann_id!r} is not one of {', '.join(IDS)}.")
        label = LABELS[ann_id]
        screen_txt = f" on the {screen}"

        with self.lock:
            self._settle()
            state = self._current_state()
            missing = self._operator_problem(who, screen)
            if missing:
                text = (f"A Play press on {label} did not say {missing}. "
                        f"Nothing was changed.")
                self._emit(actor="operator", action="play",
                          outcome="refused", reason=missing, text=text,
                          ann_id=ann_id, who=who or None,
                          screen=screen or None, state=state)
                raise ValueError(text)
            if who.lower() not in {n.lower() for n in self.operators}:
                reason = (f"{who!r} is not on the operator list "
                         f"({', '.join(self.operators) or 'empty'}).")
                text = (f"{who} pressed Play on {label} on the {screen}, "
                        f"but is not on the operator list. Nothing was "
                        f"changed.")
                self._emit(actor="operator", action="play",
                          outcome="refused", reason=reason, text=text,
                          ann_id=ann_id, who=who, screen=screen, state=state)
                raise ValueError(reason + " Pick a name from the list. "
                                 "Nothing was changed.")
            if self.playing is not None:
                other = LABELS[self.playing]
                text = (f"{who} pressed Play on {label}{screen_txt}, but "
                        f"{other} is already playing. Only one "
                        f"announcement plays at a time. Nothing was "
                        f"changed.")
                self._emit(actor="operator", action="play",
                          outcome="refused",
                          reason="another announcement is already playing",
                          text=text, ann_id=ann_id, who=who, screen=screen,
                          state=state)
                raise ValueError(text)
            st = self.status_by_id.get(ann_id, {})
            if not st.get("available"):
                text = (f"{who} pressed Play on {label}{screen_txt}, but "
                        f"it is not available: {st.get('reason')}")
                self._emit(actor="operator", action="play",
                          outcome="refused", reason=st.get("reason"),
                          text=text, ann_id=ann_id, who=who, screen=screen,
                          state=state)
                raise ValueError(text)
            refusal = interlock_refusal(state)
            if refusal:
                text = (f"{who} pressed Play on {label}{screen_txt}. "
                        f"Refused: {refusal}")
                self._emit(actor="operator", action="play",
                          outcome="refused", reason=refusal, text=text,
                          ann_id=ann_id, who=who, screen=screen, state=state)
                raise ValueError(text)
            try:
                sd = self._sd()
                dev = resolve_output_device(sd, self.device_name)
            except Exception as e:
                text = (f"{who} pressed Play on {label}{screen_txt}. It "
                        f"failed: {_clean(str(e))}")
                self._emit(actor="operator", action="play",
                          outcome="failed", reason=_clean(str(e)),
                          text=text, ann_id=ann_id, who=who, screen=screen,
                          state=state)
                raise ValueError(_clean(str(e)))
            # Claim the slot now, before the lock is released for the file
            # read below: a second press must be refused as "already
            # playing" even while this one is still decoding, and nothing
            # may act on self.playing except while holding self.lock.
            # _claim_gen is what makes this attempt provably MINE: a plain
            # value comparison against self.playing further down cannot
            # tell "still my claim" from "a LATER claim that happens to be
            # for the same id" apart, which let a stale decode overwrite a
            # live stream with nothing left able to stop it (review round
            # 2, should-fix 2: audit13b_reentrant_claim.py).
            self._claim_gen += 1
            my_gen = self._claim_gen
            self.playing = ann_id

        # The file read happens WITHOUT the lock held: self.files never
        # changes after startup, so this is safe to do unlocked, and it is
        # the one part of a Play press that touches disk. Holding the lock
        # across it would block Stop, a status poll and every other
        # operator action for as long as the read takes (review, minor).
        try:
            pcm, channels, rate = self._decode(ann_id)
        except Exception as e:
            with self.lock:
                # Only touch shared state if this is still the current
                # claim: a Stop and a fresh Play could have already run
                # while this decode was failing, and that fresh claim's
                # own outcome is not this attempt's to overwrite or log
                # over.
                if self._claim_gen == my_gen and self.playing == ann_id:
                    self.playing = None
                    text = (f"{who} pressed Play on {label}{screen_txt}. "
                            f"It failed: {_clean(str(e))}")
                    self._emit(actor="operator", action="play",
                              outcome="failed", reason=_clean(str(e)),
                              text=text, ann_id=ann_id, who=who,
                              screen=screen, state=self._current_state())
            raise ValueError(_clean(str(e)))

        with self.lock:
            if self._claim_gen != my_gen or self.playing != ann_id:
                # A LATER claim (Stop, then Play again, possibly for the
                # same id) has already taken over while this attempt was
                # reading its file. This attempt has built nothing yet
                # (the stream is opened further down, after this check),
                # so there is nothing of its own to close; it only needs
                # to say so and get out of the way.
                text = (f"{who} pressed Play on {label}{screen_txt}, but "
                        f"it was stopped and played again before this "
                        f"press finished reading its file. Nothing was "
                        f"changed.")
                self._emit(actor="operator", action="play",
                          outcome="refused",
                          reason="a later claim already started",
                          text=text, ann_id=ann_id, who=who, screen=screen,
                          state=self._current_state())
                raise ValueError(text)
            # Re-check the interlock immediately before the stream actually
            # starts, INSIDE the same locked section as start(): the window
            # since the first check included a device query and the file
            # read above, both real wall time, during which the
            # scheduler's own tick thread runs independently, on its own
            # lock (review, blocker 1a: audit13_toctou_race.py).
            state = self._current_state()
            refusal = interlock_refusal(state)
            if refusal:
                self.playing = None
                text = (f"{who} pressed Play on {label}{screen_txt}. "
                        f"Refused: {refusal}")
                self._emit(actor="operator", action="play",
                          outcome="refused", reason=refusal, text=text,
                          ann_id=ann_id, who=who, screen=screen, state=state)
                raise ValueError(text)
            out_ch = min(channels, dev["channels"]) or 1
            if out_ch < channels:
                self._emit(actor="system", action="play", outcome="note",
                          reason="channels dropped",
                          text=f"{label} is {channels}-channel but "
                               f"{_clean(self.device_name)} only has "
                               f"{dev['channels']}; only the first "
                               f"{out_ch} will play.", ann_id=ann_id,
                          state=state)
            player = _Player(pcm[:, :out_ch] if out_ch < channels else pcm,
                             out_ch, rate, clock=self._clock)

            def cb(outdata, frames, tinfo, status):
                block = player.next_block(frames, status)
                outdata[:, :block.shape[1]] = block
                if outdata.shape[1] > block.shape[1]:
                    outdata[:, block.shape[1]:] = 0

            try:
                stream = sd.OutputStream(device=dev["index"], channels=out_ch,
                                         samplerate=rate, blocksize=1024,
                                         dtype=pcm.dtype.name, callback=cb)
                stream.start()
            except Exception as e:
                self.playing = None
                text = (f"{who} pressed Play on {label}{screen_txt}. It "
                        f"failed to open {_clean(self.device_name)}: "
                        f"{_clean(str(e))}")
                self._emit(actor="operator", action="play",
                          outcome="failed", reason=_clean(str(e)),
                          text=text, ann_id=ann_id, who=who, screen=screen,
                          state=state)
                raise ValueError(f"{label} could not be played: "
                                 f"{_clean(str(e))}")
            self.last_good_device = dev["name"]
            self._player = player
            self._stream = stream
            length = st.get("length_s") or 0.0
            text = (f"{who} played {label}{screen_txt}. It runs "
                    f"{length:.0f} s.")
            self._emit(actor="operator", action="play", outcome="started",
                      reason=f"show state {state or 'no scheduler'}",
                      text=text, ann_id=ann_id, who=who, screen=screen,
                      state=state)
            return self.status()

    def stop(self, who, screen):
        with self.lock:
            self._settle()
            who = _clean((who or "").strip())
            screen = _clean((screen or "").strip())
            state = self._current_state()
            missing = self._operator_problem(who, screen)
            if missing:
                raise ValueError(f"The Stop press does not say {missing}. "
                                 f"Nothing was changed.")
            if who.lower() not in {n.lower() for n in self.operators}:
                raise ValueError(
                    f"{who!r} is not on the operator list "
                    f"({', '.join(self.operators) or 'empty'}). Pick a "
                    f"name from the list. Nothing was changed.")
            screen_txt = f" on the {screen}"
            if self.playing is None:
                self._emit(actor="operator", action="stop", outcome="no-op",
                          reason="nothing was playing",
                          text=f"{who} pressed Stop{screen_txt}, but "
                               f"nothing was playing.", who=who,
                          screen=screen, state=state)
                return self.status()
            ann_id = self.playing
            label = LABELS[ann_id]
            elapsed = self._player.elapsed_s if self._player else 0.0
            self._finish()
            text = (f"{who} pressed Stop on {label}{screen_txt} after "
                    f"{elapsed:.0f} s.")
            self._emit(actor="operator", action="stop", outcome="stopped",
                      reason="operator stop", text=text, ann_id=ann_id,
                      who=who, screen=screen, state=state)
            return self.status()

    # -- views --------------------------------------------------------------
    def status(self):
        with self.lock:
            self._settle()
            state = self._current_state()
            device_ok, device_reason = self._probe_device()
            items = []
            for aid in IDS:
                st = self.status_by_id.get(
                    aid, {"available": False, "reason": "not loaded",
                         "length_s": None})
                items.append({"id": aid, "name": LABELS[aid],
                             "available": bool(st.get("available")),
                             "reason": st.get("reason"),
                             "length_s": st.get("length_s")})
            playing = None
            if self.playing is not None and self._player is not None:
                playing = {"id": self.playing, "name": LABELS[self.playing],
                          "elapsed_s": self._player.elapsed_s,
                          "length_s": self.status_by_id[self.playing]
                                            .get("length_s")}
            return {"ok": not self.error, "error": self.error or None,
                    "device": self.device_name, "device_available": device_ok,
                    "device_reason": device_reason,
                    "last_good_device": self.last_good_device,
                    "announcements": items, "playing": playing,
                    "show_state": state, "blocked": interlock_refusal(state),
                    "journal": list(self.journal)[-40:][::-1]}

    def close(self):
        """Close whatever is playing. Called on shutdown, so a Ctrl-C does
        not leave an announcement looping on the output device forever."""
        with self.lock:
            self._finish()

    # -- the web routes -------------------------------------------------
    def get(self, route):
        if route == "/api/announce/status":
            return 200, self.status()
        return 404, {"error": "no such thing here"}

    def post(self, route, body):
        body = body or {}
        if route == "/api/announce/play":
            return 200, self.play(body.get("id"), body.get("who"),
                                  body.get("screen"))
        if route == "/api/announce/stop":
            return 200, self.stop(body.get("who"), body.get("screen"))
        return 404, {"error": "no such thing here"}
