"""Operator announcements: three pre-recorded messages, played one at a time
through the show audio (or, with no show audio running, their own output).

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
schedule.py or schedule_service.py. The provider decides only whether a
Hold is asked for first, and what the journal says; never whether an
announcement plays.

An announcement is independent of the schedule (Jeff's field report,
2026-10-28: "It refuses because the night is over. That should not be
this way. Those things are independent."). It plays whatever state the
scheduler is in: before the night starts (BOOT), between shows, during a
show, on Hold, paused, after the night is over, or with no schedule loaded
at all. The schedule's state only decides whether a Hold is taken FIRST,
never whether the announcement plays.

Between shows (IDLE or STANDBY) a press asks the scheduler to Hold, through
the SAME path the operator's own Hold uses, with this press's own who and
screen, before the file reaches the output (Jeff, 2026-10-26): Hold is a
schedule pause, so the next show does not start until the operator is
back. Only a Hold actually taken there brings the operator Hold's safety
cuts with it (remote.announce_hold). When it ends nothing resumes the
schedule by itself; only an operator's own Resume does. Already on Hold,
or paused, counts as held: nothing more is asked. When the scheduler cannot
take a Hold (the night is over or closing, it is still starting up, there
is no schedule) the announcement plays anyway with no Hold, and the
journal and the press's answer say so in one line (hold_note).

While a show is running (SHOW, or PAUSED by an operator's own Hold), an
announcement plays WITHOUT pausing, holding or stopping anything, decided
by the scheduler under its own lock, after its own tick, so a press at
the second a show starts never pauses it (review of PR #129): pausing a
show mid-way makes no sense for a cancellation or a cannot-continue, and
the operator ends the show with Stop (or Abort) when that is what is meant.
An announcement never starts a show, and never disarms, stops or latches
anything itself; see SHOW_STATES and play().

The Hold request is a one-method coupling to the scheduler
(`hold_requester`, a plain callable `(who, screen) -> refusal or None`),
wired by web.py alongside `state_provider` and on_show_started. With no
scheduler configured it is never set, and an announcement simply plays
with nothing held.

A show starting while an announcement is playing: `on_show_started` is a
push hook web.py wires the scheduler to call the moment it starts a show;
the announcement's own audio then fades and stops so the show starts on
time. See SHOW_START_STOPS_ANNOUNCEMENT.

The Hold request is check-then-act around real work (the file read), so
play() looks again, read-only, immediately before the sound starts: an
operator's Resume pressed during the file read always wins (the
announcement then plays with no Hold, it never Holds again).

Where the sound goes (Jeff's field report, 2026-10-28, item 9): through the
SHOW AUDIO, mixed into the show's own front left and right outputs (the
Main L/R, "show_outputs", [1, 2] unless the file says otherwise), on the
stream the show audio process already has open (showaudio.AnnounceBus).
There is no second device to name, an announcement can play during a show,
and neither a show nor Hold nor Stop ever has to let go of the interface
for one. The show's own audio is not ducked or changed: the announcement
is added on top at "gain_db" (0 unless the file says otherwise), with a
soft clip guard on the summed outputs while it plays (showaudio.soft_clip).
`show_route` is the provider web.py wires: a callable returning a
showaudio.ShowRoute while a show audio process is running, or None.

When there is no show audio running (the machine is idle, nothing has been
Run, or the show file plays no audio of its own), an announcement plays on
its own output instead, the one named in "device" (and "hostapi", when that
name is on more than one output). It is never the system default: the
system default on the show PC is whatever MadMapper's own show audio is on.
That output is found by an EXACT name (never a substring: a substring can
silently land on a different physical device whose name merely contains
the configured text) and stays that device even if it briefly disappears:
see `resolve_output_device`. While a show audio process IS running but
its interface is not open (unplugged, still opening), an announcement is
refused with that sentence rather than opening the same interface a second
way. A name that is on more than one output is
refused with the list to pick from; the rack screen's picker (Programming,
Announcement output) saves the choice (save_device).

A config written by the installer's first run names no device yet
(`"device": ""`): the service loads, shows each recording as present or
missing, plays through the show audio whenever it runs, and otherwise
says plainly that no output is picked.
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
# The rack screen's Test tone (Jeff's field report, 2026-10-28): 1 s of a
# soft 440 Hz at -20 dBFS through the same route and output an
# announcement would use, so the route can be checked with no recording.
# Never one of IDS: it is never a button of its own and never Holds.
TEST_TONE = "test_tone"
LABELS[TEST_TONE] = "Test tone"
TONE_HZ, TONE_DBFS, TONE_S, TONE_RATE = 440.0, -20.0, 1.0, 48000
# A recording whose loudest sample is below this is silent (or decoded to
# nothing): it is never reported as playing (Jeff's field report,
# 2026-10-28: the screen said playing and nothing was heard).
SILENT_DBFS = -80.0
# A device that opened but has taken no audio after this long has failed:
# "playing" is only ever said once audio has actually gone to it.
START_S = 1.5

CONFIG_FILE = "ltcplay_announce.json"
OPERATORS_FILE = "ltcplay_operators.json"
DEFAULT_OPERATORS = ("Andy", "Jeff")

JOURNAL = 400

# The scheduler states in which a show is running (schedule.SHOW and
# schedule.PAUSED, by name, so this module never imports schedule.py). An
# announcement pressed in one of these plays over the show and Holds
# nothing (Jeff, 2026-10-26); in any other state it Holds the schedule
# first.
SHOW_STATES = ("SHOW", "PAUSED")

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
# How long a Stop fades an announcement playing on the show audio, so the
# PA never clicks. The show's own audio is never faded by it.
BUS_STOP_FADE_S = 0.05

# A wrong file pointed at by mistake, or a device that has quietly stopped
# answering: both are caught by a plain limit rather than an unbounded read
# or an announcement that never lets the lock go.
MAX_FILE_BYTES = 50 * 1024 * 1024      # 50 MB: generous for a spoken line
MAX_LENGTH_S = 300.0                   # 5 minutes: a sentence, not a show
# Through the show audio an announcement is at most a minute (safety review
# of #158): the recording is held in the show audio process's memory and
# sent to it in parts, beside the show's own audio. A longer recording is
# refused there with a plain sentence; on its own output (no show audio
# running) MAX_LENGTH_S still applies.
SHOW_ROUTE_MAX_S = 60.0
STALL_S = 3.0            # no callback at all for this long means the device
                         # has gone away, not that the file is just long
CALLBACK_ERROR_LIMIT = 20      # the device answers, but every time with a
                               # reported problem: also treated as a failure

_DTYPE_BY_SAMPWIDTH = {1: "uint8", 2: "int16", 4: "int32"}
# 3 (24-bit) is accepted too, decoded by hand in _wav_info: neither numpy
# nor sounddevice has a 3-byte dtype, so it is never a key in the map above.
_PCM_SAMPWIDTHS = frozenset(_DTYPE_BY_SAMPWIDTH) | {3}
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
# The keys ltcplay_announce.json may have besides "device" and "files",
# each optional (Jeff's field report, 2026-10-28, items 9 and 10):
#   "hostapi"       which of the outputs with that exact name, when more than
#                   one has it (Windows lists one device under each of its
#                   audio systems); written by the rack screen's picker
#   "show_outputs"  the show audio outputs an announcement plays on, counted
#                   from 1: the front left and right, the Main L/R
#   "gain_db"       the announcement's level in the show mix, -40 to +6 dB
OPTIONAL_KEYS = ("hostapi", "show_outputs", "gain_db")
DEFAULT_SHOW_OUTPUTS = (1, 2)
# The announcement's level in the show mix unless the file says otherwise:
# -6 dB, so a recording normalised to full scale over a loud passage of
# the show is not driven into the soft clip guard (safety review of #158).
DEFAULT_GAIN_DB = -6.0
GAIN_DB_RANGE = (-40.0, 6.0)


def parse_config(doc, where):
    """{"device": name, "files": {"delayed": path, "cancellation": path,
    "cannot_continue": path}}, plus the OPTIONAL_KEYS. Raises ValueError
    with a sentence. device "" means not set yet: ("", files) comes back,
    and nothing plays except through the show audio."""
    where = _clean(where)
    if not isinstance(doc, dict) or not {"device", "files"} <= set(doc) \
            or set(doc) - {"device", "files"} - set(OPTIONAL_KEYS):
        raise ValueError(f'{where} has to be {{"device": name, "files": '
                         f'{{...}}}}, and may also have '
                         f'{", ".join(OPTIONAL_KEYS)}; nothing else.')
    device = doc.get("device")
    if not isinstance(device, str):
        raise ValueError(f"{where}: device has to be a named output "
                         f"device. There is no default; announcements "
                         f"never use the system output.")
    # "" is allowed: not set yet (the installer's first run writes it that
    # way). Nothing plays until it is set; it is never read as "the system
    # default". See AnnounceService.device_set.
    files = doc.get("files")
    if not isinstance(files, dict) or set(files) != set(IDS):
        raise ValueError(f"{where}: files has to name exactly "
                         f"{', '.join(IDS)}.")
    for aid in IDS:
        v = files.get(aid)
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{where}: files.{aid} has to be a file path.")
    parse_options(doc, where)
    return device.strip(), {k: v.strip() for k, v in files.items()}


def parse_options(doc, where):
    """The OPTIONAL_KEYS, checked, with their defaults:
    {"hostapi": str or None, "show_outputs": (a, b), "gain_db": float}."""
    where = _clean(where)
    host = doc.get("hostapi")
    if host is not None and (not isinstance(host, str) or not host.strip()):
        raise ValueError(f"{where}: hostapi has to be the name of the audio "
                         f"system the output is on, like \"Windows "
                         f"WASAPI\", or left out.")
    outs = doc.get("show_outputs", list(DEFAULT_SHOW_OUTPUTS))
    if not (isinstance(outs, list) and 1 <= len(outs) <= 2 and
            all(isinstance(o, int) and not isinstance(o, bool) and o >= 1
                for o in outs) and len(set(outs)) == len(outs)):
        raise ValueError(f"{where}: show_outputs has to be one or two "
                         f"different show audio outputs counted from 1, "
                         f"like [1, 2] (the front left and right).")
    g = doc.get("gain_db", DEFAULT_GAIN_DB)
    lo, hi = GAIN_DB_RANGE
    if not (isinstance(g, (int, float)) and not isinstance(g, bool) and
            lo <= g <= hi):
        raise ValueError(f"{where}: gain_db has to be a number of decibels "
                         f"from {lo:g} to {hi:g} ({DEFAULT_GAIN_DB:g} "
                         f"unless set); 0 plays the recording as it is.")
    return {"hostapi": host.strip() if host else None,
            "show_outputs": tuple(outs), "gain_db": float(g)}


# ---------------------------------------------------------------- devices --
def _hostapi_names(sd):
    try:
        return [str(h.get("name", "")) for h in sd.query_hostapis()]
    except Exception:                            # noqa: BLE001
        return []


def list_outputs(sd):
    """Every device that can play sound, the way audio.list_inputs lists
    devices that can capture it, each with the audio system ("hostapi") it
    is listed under: Windows lists one physical output once per audio
    system, under the same name."""
    out = []
    try:
        default_out = sd.default.device[1]
    except Exception:
        default_out = None
    apis = _hostapi_names(sd)
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_output_channels", 0) <= 0:
            continue
        h = d.get("hostapi")
        out.append({"index": i, "name": d.get("name", "?"),
                    "channels": d["max_output_channels"],
                    "rate": int(d.get("default_samplerate") or 48000),
                    "hostapi": (apis[h] if isinstance(h, int) and
                                0 <= h < len(apis) else ""),
                    "default": i == default_out})
    return out


def describe_output(d):
    """One output as the picker and a refusal say it: its name, then the
    audio system and its outputs in brackets."""
    bits = [b for b in (d.get("hostapi") or "",
                        f"{d.get('channels')} outputs") if b]
    return f"{_clean(d.get('name'))} ({', '.join(bits)})"


def resolve_output_device(sd, name, hostapi=None):
    """By an EXACT name, case-insensitive, never by index and never by a
    partial match. An index shifts when something else on the machine
    unplugs or replugs, and following the name is what lets a device that
    goes away mid-run come back on its own; a SUBSTRING match, this
    function's previous behaviour, can silently land on a completely
    different physical device whose Windows-assigned name merely happens to
    contain the configured text (found in review: audit13_device_wrong_
    match.py, where the show's own DSP output was matched by mistake). No
    name at all is a config error, not a fallback to the system default.

    `hostapi` picks among outputs that share the exact name (one per
    Windows audio system). A name on more than one output with no hostapi
    to tell them apart is refused with the list to pick from (Jeff's field
    report, 2026-10-28, item 10)."""
    if not name or not str(name).strip():
        raise ValueError("No output device is named for announcements. "
                         "There is no default: announcements never use "
                         "the system output, which is the device the show "
                         "audio is on.")
    outputs = list_outputs(sd)
    want = str(name).strip().lower()
    hits = [d for d in outputs if d["name"].strip().lower() == want]
    if hostapi and len(hits) > 1:
        hits = [d for d in hits
                if d["hostapi"].strip().lower() == str(hostapi).strip()
                .lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ValueError(f"{_clean(name)!r} is not attached. Nothing else "
                         f"will be used in its place.")
    if hostapi and len(hits) > 1:
        raise ValueError(same_api_sentence(name, hits))
    choices = "; ".join(describe_output(d) for d in hits)
    raise ValueError(f"{_clean(name)} matches {len(hits)} outputs on this "
                     f"PC: pick one: {choices}.")


def same_api_sentence(name, hits):
    """Two or more outputs with the same name on the same audio system:
    nothing here can tell them apart, so the operator renames one."""
    api = hits[0].get("hostapi") or "the same audio system"
    return (f"{_clean(name)} is the name of {len(hits)} outputs on "
            f"{_clean(api)}, so they cannot be told apart. Rename one in "
            f"Windows: Sound settings, More sound settings, Playback, its "
            f"Properties, then the name at the top. Then pick it again.")


def _write_json_atomic(path, doc):
    """Write `doc` to `path` through a temp file of its own beside it,
    flushed to the disk, then put in place in one step: a reader sees the
    old file or the new one, never half of one."""
    import tempfile
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".ltcplay_announce.", suffix=".tmp",
                               dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ------------------------------------------------------------ the schedule
def no_hold_note(state, refusal=None):
    """The one line said when an announcement plays with no Hold taken: why
    none was needed, or why none was possible. Never a reason not to play
    (Jeff's field report, 2026-10-28)."""
    if refusal:
        return (f"No hold was possible ({_clean(refusal).rstrip('.')}), so "
                f"it plays with nothing held.")
    if state in SHOW_STATES:
        return "It plays over the running show; nothing was paused or stopped."
    return "No schedule is running here, so nothing was held."


# ------------------------------------------------------------------ wav I/O
# The 12 bytes every standard WAVE_FORMAT_EXTENSIBLE SubFormat GUID shares;
# only the leading 4 bytes (little-endian) vary, and match the classic
# format tag (1 PCM, 3 IEEE float, ...). KSDATAFORMAT_SUBTYPE_* in the
# Windows SDK; review round 2, 2026-09-26 (should-fix 5): many DAWs,
# Audacity among them, write 32-bit float this way.
_EXTENSIBLE_SUBFORMAT_TAIL = bytes.fromhex("00001000800000aa00389b71")


def _wav_format_tag(path):
    """The format tag from the WAV's own fmt chunk: 1 is PCM, 3 is IEEE
    float. Python's `wave` module always assumes PCM and never looks at
    this, so a 32-bit float file opens exactly like a 32-bit int one, and
    its sample bytes would be reinterpreted as integers unless this module
    reads the tag itself and decodes float samples as float (review:
    audit13_probe_weaker_than_open.py, part b; recordings are WAV, possibly
    24-bit or 32-bit float, Jeff, 2026-09-26).

    WAVE_FORMAT_EXTENSIBLE (0xFFFE) is resolved to the classic tag hiding
    in its own SubFormat GUID, so an extensible-wrapped float or PCM file
    is treated exactly like a plain one; an extensible file wrapping
    anything else this module does not recognize still comes back as
    0xFFFE, which _wav_info refuses with a plain "WAV format N" sentence,
    never `wave`'s own raw GUID text (review round 2, 2026-09-26,
    should-fix 5: audit15_wav_edgecases.py).

    Returns None if it cannot be read, which is treated as PCM, the safer
    reading of a file the standard library reader already accepted."""
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
                    body = fh.read(size)
                    if len(body) < 2:
                        return None
                    tag = int.from_bytes(body[:2], "little")
                    if tag == 0xFFFE and len(body) >= 40:
                        guid = body[24:40]
                        if guid[4:] == _EXTENSIBLE_SUBFORMAT_TAIL:
                            return int.from_bytes(guid[:4], "little")
                    return tag
                fh.seek(size + (size & 1), 1)
    except OSError:
        return None


def _read_float_wav(path, decode):
    """A minimal RIFF/WAVE reader for the one case Python's own `wave`
    module cannot be trusted to open at all: a 32-bit IEEE float file
    (format tag 3). Some Python builds refuse it outright ("unknown
    format: 3") rather than merely mis-decoding it as integers, so relying
    on `wave.open` for this case would make this module's own behaviour
    depend on which Python it happens to run under -- exactly the kind of
    probe/play disagreement this module exists to rule out (review:
    audit13_probe_weaker_than_open.py, part b). Reads only what play()
    needs off the fmt and data chunks: (channels, rate, bits, raw bytes or
    b"" when not decoding, data chunk size in bytes). Raises ValueError
    with a plain sentence."""
    with open(path, "rb") as fh:
        riff = fh.read(12)
        if len(riff) < 12 or riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
            raise ValueError(f"{_clean(path)} is not a WAV file.")
        channels = rate = bits = None
        data, data_size = b"", 0
        while True:
            header = fh.read(8)
            if len(header) < 8:
                break
            chunk_id = header[:4]
            size = int.from_bytes(header[4:8], "little")
            if chunk_id == b"fmt ":
                body = fh.read(size)
                if len(body) < 16:
                    raise ValueError(f"{_clean(path)}'s fmt chunk is too "
                                     f"short to read.")
                channels = int.from_bytes(body[2:4], "little")
                rate = int.from_bytes(body[4:8], "little")
                bits = int.from_bytes(body[14:16], "little")
                if size & 1:
                    fh.read(1)
            elif chunk_id == b"data":
                data_size = size
                if decode:
                    data = fh.read(size)
                else:
                    fh.seek(size, 1)
                if size & 1:
                    fh.read(1)
            else:
                fh.seek(size + (size & 1), 1)
    if channels is None or rate is None or bits is None:
        raise ValueError(f"{_clean(path)} has no fmt chunk to read.")
    if not channels or not rate:
        raise ValueError(f"{_clean(path)}'s fmt chunk names no channels "
                         f"or no sample rate.")
    return channels, rate, bits, data, data_size


def _data_chunk_size_problem(path, size_on_disk):
    """None if the WAV's own "data" chunk declares a plausible size: not
    0, not the streaming placeholder 0xFFFFFFFF, and not more bytes than
    are actually left in the file. Otherwise a plain sentence.

    Checked once, by hand, ahead of BOTH the PCM path (Python's `wave`
    module trusts the declared size exactly the same way) and the float
    path (_read_float_wav), so the two can never disagree on this, and a
    file that looks cut off or was still being written when it was read
    is refused loudly instead of quietly serving silence (a declared size
    of 0, with real audio actually sitting right after it) or a length
    that overstates what is really playable (declared size bigger than
    the bytes on disk) (review round 2, 2026-09-26, should-fix 6:
    audit15_wav_floatparser.py / audit15_wav_edgecases.py)."""
    try:
        with open(path, "rb") as fh:
            riff = fh.read(12)
            if len(riff) < 12 or riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
                return None      # let the normal reader say what is wrong
            while True:
                header = fh.read(8)
                if len(header) < 8:
                    return None
                chunk_id = header[:4]
                size = int.from_bytes(header[4:8], "little")
                if chunk_id == b"data":
                    remaining = size_on_disk - fh.tell()
                    if size == 0 or size == 0xFFFFFFFF:
                        return (f"{_clean(path)}'s data chunk declares a "
                               f"placeholder size ({size}); the file "
                               f"looks cut off, or was still being "
                               f"written when it was read.")
                    if size > remaining:
                        return (f"{_clean(path)}'s data chunk declares "
                               f"{size} bytes, but only {remaining} are "
                               f"actually in the file; it looks cut off, "
                               f"or was still being written when it was "
                               f"read.")
                    return None
                fh.seek(size + (size & 1), 1)
    except OSError:
        return None


def _wav_info(path, decode=True):
    """Open, validate, and (when `decode`) fully read a WAV file:
    (pcm_or_None, channels, rate, length_s). Raises ValueError with a plain
    sentence for anything that could not actually be played, so the startup
    probe (`decode=False`) and a real Play press (`decode=True`) can never
    disagree: they are the same function.

    Accepted: 16-bit, 24-bit and 32-bit PCM (integer), mono or stereo, any
    common sample rate, plus 32-bit IEEE float (Jeff, 2026-09-26: recordings
    are WAV, possibly 24-bit; float is accepted too since a field recorder
    or a DAW may hand one over), plain or wrapped in WAVE_FORMAT_EXTENSIBLE
    (review round 2, 2026-09-26, should-fix 5: many DAWs, Audacity among
    them, write 32-bit float that way). Nothing else.

    A data chunk that declares an impossible size -- 0, the streaming
    placeholder 0xFFFFFFFF, or more bytes than are actually left in the
    file -- is refused outright, for both paths, rather than read as a
    healthy but truncated or silent file (review round 2, 2026-09-26,
    should-fix 6: audit15_wav_floatparser.py / audit15_wav_edgecases.py).

    The format tag is checked BEFORE Python's own `wave` module ever opens
    the file, and a float file is never handed to `wave` at all (see
    _read_float_wav): `wave` rejects a non-PCM file on its own on a modern
    Python, but with its own message ("unknown format: 3"), and on some
    Python builds it refuses to open the file at all rather than merely
    mis-decode it, which is not a sentence an operator should have to read,
    and not a way of failing this module can offer consistently across
    Python versions (review: audit13_probe_weaker_than_open.py, part b).
    Without checking the tag here, a float file `wave` DID accept would
    have its samples silently reinterpreted as integers."""
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
    is_float = tag == 3
    if tag is not None and tag not in (1, 3):
        raise ValueError(f"{_clean(path)} is WAV format {tag}, which is "
                         f"not supported. Export 16-bit, 24-bit or 32-bit "
                         f"PCM, or 32-bit float, instead.")
    problem = _data_chunk_size_problem(path, size)
    if problem:
        raise ValueError(problem)
    if is_float:
        try:
            channels, rate, bits, raw, data_size = _read_float_wav(path,
                                                                    decode)
        except OSError as e:
            raise ValueError(f"{_clean(path)} could not be read as a WAV "
                             f"file: {_clean(str(e))}.")
        if bits != 32:
            raise ValueError(f"{_clean(path)} is a {bits}-bit floating "
                             f"point WAV, which is not supported. 32-bit "
                             f"float is the only floating point WAV this "
                             f"reads.")
        sampwidth = 4
        n = data_size // (channels * sampwidth)
    else:
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
        if sampwidth not in _PCM_SAMPWIDTHS:
            raise ValueError(f"{_clean(path)} is a {sampwidth * 8}-bit "
                             f"WAV, which is not supported. Export 16-bit, "
                             f"24-bit or 32-bit PCM, or 32-bit float, "
                             f"instead.")
    length_s = n / float(rate) if rate else 0.0
    if length_s > MAX_LENGTH_S:
        raise ValueError(f"{_clean(path)} is {length_s:.0f} s long, over "
                         f"the {MAX_LENGTH_S:g} s limit for an "
                         f"announcement.")
    if not decode:
        return None, channels, rate, length_s
    import numpy as np
    if is_float:
        # A real 32-bit IEEE float WAV, decoded as float, not reinterpreted
        # as int32 (what this module did before this decision: loud noise,
        # no error anywhere; review, audit13_probe_weaker_than_open.py,
        # part b).
        pcm = np.frombuffer(raw, dtype=np.float32).copy()
    elif sampwidth == 3:
        # 24-bit PCM: 3 bytes per sample, little-endian. Neither numpy nor
        # sounddevice's OutputStream has a 3-byte dtype, so each sample is
        # left-justified into an int32 (padded with a zero LOW byte): the
        # same value, shifted up 8 bits, at full 32-bit scale, with the
        # original sign bit landing exactly on int32's own sign bit.
        n_samples = len(raw) // 3
        padded = np.zeros((n_samples, 4), dtype=np.uint8)
        padded[:, 1:] = np.frombuffer(raw, dtype=np.uint8)[
            :n_samples * 3].reshape(-1, 3)
        pcm = padded.view(np.int32).reshape(-1)
    else:
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
                "_fade_total", "_fade_pos", "stop_reason", "started_at")

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
        self.started_at = self.last_progress_at
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


def as_float(pcm):
    """Decoded samples as float32 at full scale 1.0, (frames, channels)."""
    import numpy as np
    if pcm.dtype == np.float32:
        f = pcm.astype(np.float32, copy=False)
    elif pcm.dtype == np.int16:
        f = pcm.astype(np.float32) / 32768.0
    elif pcm.dtype == np.int32:
        f = pcm.astype(np.float32) / 2147483648.0
    elif pcm.dtype == np.uint8:
        f = (pcm.astype(np.float32) - 128.0) / 128.0
    else:
        f = pcm.astype(np.float32)
    return f.reshape(f.shape[0], -1)


def peak_dbfs(pcm):
    """The loudest sample of a decoded recording, in dBFS (-inf when every
    sample is zero, or there are none)."""
    import math
    import numpy as np
    f = as_float(pcm)
    if not f.size:
        return float("-inf")
    p = float(np.max(np.abs(f)))
    return 20.0 * math.log10(p) if p > 0 else float("-inf")


def test_tone_pcm(rate=TONE_RATE):
    """TONE_S of TONE_HZ at TONE_DBFS, mono float32, with 10 ms ramps so it
    never clicks."""
    import numpy as np
    n = int(TONE_S * rate)
    t = np.arange(n, dtype=np.float64) / rate
    amp = 10.0 ** (TONE_DBFS / 20.0)
    x = amp * np.sin(2.0 * np.pi * TONE_HZ * t)
    r = int(0.01 * rate)
    env = np.ones(n)
    env[:r] = np.linspace(0.0, 1.0, r)
    env[-r:] = np.linspace(1.0, 0.0, r)
    return (x * env).astype(np.float32).reshape(-1, 1)


def _db_text(db):
    return "silence" if db == float("-inf") else f"{db:.1f} dBFS"


def to_show_mix(pcm, rate, out_rate, n_outs):
    """A decoded recording as the show audio's announcement bus takes it:
    float32 at full scale 1.0, at the show's rate (linear interpolation:
    plenty for a spoken line), and one channel (played on every one of the
    show outputs) or exactly `n_outs` (left to the first, right to the
    second). A recording with more channels than that keeps its first
    `n_outs`."""
    import numpy as np
    f = as_float(pcm)
    if f.shape[1] > 1:
        f = f[:, :n_outs] if f.shape[1] >= n_outs else f[:, :1]
    if rate != out_rate and f.shape[0]:
        n_out = int(round(f.shape[0] * float(out_rate) / rate))
        src = np.arange(f.shape[0], dtype=np.float64)
        at = np.arange(n_out, dtype=np.float64) * (float(rate) / out_rate)
        f = np.stack([np.interp(at, src, f[:, c]) for c in
                      range(f.shape[1])], axis=1).astype(np.float32)
    return np.ascontiguousarray(f, dtype=np.float32)


class _BusPlayer:
    """The same face as _Player, for an announcement playing on the show
    audio's own bus (showaudio.AnnounceBus): what it has played, whether it
    is done, when it last moved, read from what the audio process
    publishes. Nothing here touches the audio: the audio process plays it."""

    def __init__(self, route, token, total_frames, rate, clock):
        self.route = route
        self.token = token
        self.total_frames = total_frames
        self.rate = rate
        self._clock = clock
        self.last_progress_at = clock()
        self.started_at = self.last_progress_at
        self.callback_errors = 0
        self.stop_reason = None
        self.frames_written = 0
        self._ended = False
        self._fading = False
        self._seen = False
        self.lost = None

    def poll(self):
        r = self.route.read()
        if r is None or r.get("token") != self.token:
            if self._seen and self.lost is None:
                # It was playing, and the bus no longer reports it: the
                # show audio process restarted (its shared memory starts
                # again from nothing). Failed at once, said plainly.
                self.lost = ("the show audio process restarted while it "
                             "played")
            return
        self._seen = True
        frame = int(r.get("frame") or 0)
        if frame != self.frames_written:
            self.frames_written = frame
            self.last_progress_at = self._clock()
        if r.get("state") == "ended":
            self._ended = True

    @property
    def done(self):
        self.poll()
        return self._ended or self.total_frames == 0

    def start_fade(self, frames):
        self._fading = True
        self.route.stop(self.token, frames)

    @property
    def elapsed_s(self):
        return self.frames_written / float(self.rate) if self.rate else 0.0


class _BusStream:
    """What _finish() stops: the bus's own announcement, faded over
    BUS_STOP_FADE_S so a Stop never clicks. The show's audio is not
    touched."""

    def __init__(self, route, token, rate):
        self.route = route
        self.token = token
        self.rate = rate

    def stop(self):
        self.route.stop(self.token, int(BUS_STOP_FADE_S * self.rate))

    def close(self):
        pass


# ------------------------------------------------------------------ service
class AnnounceService:
    """One of these per running program, or none at all when `--announce`
    was never passed. Holds the three announcements, the output device, and
    the single play/stop interlock."""

    GET_ROUTES = ("/api/announce/status",)
    POST_ROUTES = ("/api/announce/play", "/api/announce/stop")

    def __init__(self, config_path, sd=None, state_provider=None,
                 operators_folder=None, clock=None, show_route=None):
        self.config_path = config_path
        self._sd_obj = sd
        # A plain callable returning a showaudio.ShowRoute while a show
        # audio process is running, or None (web.py wires it to the
        # session's show clock). None, or a callable answering None: there
        # is no show audio, and the announcement's own output is used.
        self.show_route = show_route
        self._route_token = 0
        # configwatch.ConfigWatch, or None (web.py sets it, #155): told of
        # this service's own save (save_device), so the rack screen never
        # reads the picker's save as an edit the engine is behind on.
        self.config_watch = None
        # A plain callable, or None: the scheduler's state name. It decides
        # only whether a Hold is asked for first and what the journal says,
        # never whether an announcement plays (Jeff's field report,
        # 2026-10-28).
        self.state_provider = state_provider
        # A plain callable `(who, screen, detail=None) -> (refusal_or_None,
        # epoch)`, or None. The other new coupling, in the "hold the show
        # first" direction; see _request_hold and the module docstring.
        # None until web.py wires it (only when a scheduler is ALSO
        # configured), and an announcement with no scheduler never tries
        # to call it.
        self.hold_requester = None
        # A plain callable `(claim_epoch) -> bool`, or None. The read-only
        # partner to hold_requester, used for the SECOND check right
        # before the stream opens: never re-Holds, only asks whether the
        # claim from the first check still stands, so an operator's own
        # Resume during the file read always wins (review round 2,
        # 2026-09-26: audit15_resume_race.py). See _check_still_held.
        self.hold_still_claimed = None
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
        self.options = parse_options({}, "defaults")
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
        # True while the playing announcement Held the schedule (pressed
        # between shows); False while it plays over a running show.
        self.held_schedule = False
        # {"id", "name", "why"} while a press Held the schedule and then
        # could not play: the rack screen says "Held, announcement failed"
        # until the schedule leaves Hold (only Resume does that) or an
        # announcement plays. See _note_held_failed.
        self.held_failed = None
        self._held_failed_epoch = None
        # The one line said when the playing announcement Held nothing:
        # why no Hold was needed or possible (no_hold_note).
        self.hold_note = ""
        # What the playing (or last) announcement actually sent: route,
        # device, audio system, channels, seconds, peak level (dBFS).
        self.sent = None
        self._player = None
        self._stream = None
        self._load()

    # -- loading, once, at startup ---------------------------------------
    def _load(self):
        self.operators, why = load_operators(self.operators_folder)
        if why:
            self._emit(actor="system", action="operators", outcome="note",
                       reason=why, text=why)
        for k, v in self._read_config(self.config_path).items():
            setattr(self, k, v)

    @classmethod
    def _read_config(cls, config_path):
        """{"error", "device_name", "files", "status_by_id", "options"}: the
        attributes the file at `config_path` sets, read and probed without
        touching this service, so a reload can look at a new file before
        anything live changes. error is "" when the file could be used. A
        setting added to the file later goes in this dict, and both _load
        and reload_if_idle apply it."""
        error, device_name, files, status = "", None, {}, {}
        # The optional keys (show_outputs, gain_db, hostapi) travel with
        # the rest, so a reload carries a hand edit of them too.
        options = parse_options({}, "defaults")
        try:
            with open(config_path, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
        except FileNotFoundError:
            error = (f"There is no announcements file at "
                     f"{_clean(config_path)}.")
        except OSError as e:
            error = (f"The announcements file {_clean(config_path)} "
                     f"cannot be read: {_clean(str(e))}.")
        except ValueError as e:
            error = (f"The announcements file {_clean(config_path)} is not "
                     f"valid JSON: {_clean(str(e))}.")
        else:
            try:
                device_name, raw_files = parse_config(doc, config_path)
                options = parse_options(doc, config_path)
            except ValueError as e:
                error = str(e)
            else:
                base = os.path.dirname(os.path.abspath(config_path))
                for aid in IDS:
                    raw = raw_files[aid]
                    files[aid] = (raw if os.path.isabs(raw) else
                                  os.path.join(base, raw))
        for aid in IDS:
            if error:
                status[aid] = {"available": False, "reason": error,
                               "length_s": None}
            else:
                status[aid] = cls._probe_file(files[aid])
        return {"error": error, "device_name": device_name, "files": files,
                "status_by_id": status, "options": options}

    def reload_if_idle(self, show_busy=False):
        """(True, "") once ltcplay_announce.json (and the operator list)
        have been read again, as at startup (configwatch.py, Jeff
        2026-10-28: the rack screen showed the old value after the file was
        edited). Else (False, a sentence saying why), and nothing changes:
        while a show is running or starting (`show_busy`), while an
        announcement plays, or when the new file cannot be used (missing,
        half written, not valid): then the engine keeps the settings it
        has. The file is read and probed WITHOUT the lock; the new
        settings replace the old ones, under the lock, only when they are
        good and nothing is playing, so a Play press sees either the old
        settings or the new ones, never a mix."""
        if show_busy:
            return False, ("it reads it again by itself once no show is "
                           "running")
        new = self._read_config(self.config_path)
        error = new["error"]
        if error:
            return False, (f"the file is not valid ({error.rstrip('.')}), "
                           f"so the engine keeps the old settings")
        operators, op_why = load_operators(self.operators_folder)
        with self.lock:
            if self.playing is not None:
                return False, ("it reads it again by itself once no "
                               "announcement is playing")
            for k, v in new.items():
                setattr(self, k, v)
            if not op_why:
                self.operators = operators
            said = (f"device {self.device_name}" if self.device_set
                    else self.DEVICE_NOT_SET)
            self._emit(actor="system", action="config", outcome="reloaded",
                       reason="the file changed on disk",
                       text=f"The announcements file {self.config_path} "
                            f"changed on disk and was read again: {said}")
        return True, ""

    @staticmethod
    def _probe_file(path):
        """A missing or unreadable file is decided HERE, once, so a press
        later never has to discover it: the button is already grey with a
        reason by the time anyone could press it. Shares _wav_info with the
        code that actually plays a file (see _decode), so nothing can be
        reported available here and then fail at the press (review:
        audit13_probe_weaker_than_open.py)."""
        if not os.path.exists(path):
            # The recordings may not be on this PC yet (Jeff, 2026-10-26):
            # a plain "recording missing", never a crash, and the press is
            # refused before anything is Held.
            return {"available": False, "missing": True,
                    "reason": f"Recording missing: {_clean(path)} is not "
                              f"on this PC yet. Copy it there, then "
                              f"restart LTC Player.",
                    "length_s": None}
        try:
            _pcm, _channels, _rate, length_s = _wav_info(path, decode=False)
        except ValueError as e:
            return {"available": False, "missing": False, "reason": str(e),
                    "length_s": None}
        return {"available": True, "missing": False, "reason": None,
                "length_s": length_s}

    # -- audio ------------------------------------------------------------
    def _sd(self):
        if self._sd_obj is None:
            from .session import _import_sounddevice
            self._sd_obj = _import_sounddevice()
        return self._sd_obj

    @property
    def device_set(self):
        """False while the config names no output device yet (the first
        run's "device": ""). Never read as the system default."""
        return bool((self.device_name or "").strip())

    PICK_HINT = ("Choose it on the rack screen: Programming, Announcement "
                 "output.")
    DEVICE_NOT_SET = ("No announcement output is picked for when the show "
                      "audio is not running. " + PICK_HINT + " The system "
                      "default is never used.")

    def _probe_device(self):
        """The announcement's OWN output (used while no show audio runs):
        (ok, plain sentence or None)."""
        if self.error:
            return False, self.error
        if not self.device_set:
            return False, self.DEVICE_NOT_SET
        try:
            sd = self._sd()
        except Exception as e:
            return False, (f"The audio system could not be reached: "
                           f"{_clean(str(e))}")
        try:
            self._resolve(sd)
        except ValueError as e:
            return False, str(e)
        except Exception as e:      # noqa: BLE001  PortAudioError and kin
            # The device query itself failed (PortAudio): said, never
            # raised, so status() always answers (final review of #129).
            return False, (f"The audio system could not list its outputs: "
                           f"{_clean(str(e))}")
        return True, None

    def _resolve(self, sd):
        """The announcement's own output, or ValueError with a short
        sentence saying what to pick."""
        try:
            return resolve_output_device(sd, self.device_name,
                                         self.options.get("hostapi"))
        except ValueError as e:
            raise ValueError(f"{e} {self.PICK_HINT}")

    def _show_route(self):
        """The running show audio as a route, or None. Never raises."""
        if self.show_route is None:
            return None
        try:
            return self.show_route()
        except Exception:                        # noqa: BLE001
            return None

    def _outs_text(self):
        outs = self.options["show_outputs"]
        if len(outs) == 2:
            return f"outputs {outs[0]} and {outs[1]}"
        return f"output {outs[0]}"

    def route_check(self, probed=None):
        """Where a press would play now, in plain words, for the rack
        screen's announcement check (labelled apart from the Show audio
        lamp, Jeff's field report 2026-10-28, item 10):
        {"kind": "show" | "device" | None, "ok", "text", "why"}."""
        if self.error:
            return {"kind": None, "ok": False, "text": "",
                    "why": self.error}
        r = self._show_route()
        if r is not None:
            text = (f"the show audio, {self._outs_text()} on "
                    f"{_clean(r.device)}")
            if max(self.options["show_outputs"]) > r.channels:
                return {"kind": "show", "ok": False, "text": text,
                        "why": (f"show_outputs in ltcplay_announce.json "
                                f"names {self._outs_text()}, but the show "
                                f"audio has only {r.channels}.")}
            if not r.ready():
                return {"kind": "show", "ok": False, "text": text,
                        "why": (f"The show audio is running, but "
                                f"{_clean(r.device)} is not open right now "
                                f"(see the Show audio lamp). Announcements "
                                f"play again once it is.")}
            return {"kind": "show", "ok": True, "text": text, "why": None}
        ok, why = probed if probed is not None else self._probe_device()
        text = (f"{_clean(self.device_name)}, its own output while the show "
                f"audio is not running" if self.device_set else "")
        return {"kind": "device", "ok": ok, "text": text, "why": why}

    # -- the picker (rack screen, Programming, Announcement output) -------
    def outputs(self):
        """Every output on this PC, for the picker: [{"name", "hostapi",
        "channels", "label", "picked", "same_name"}], or ValueError."""
        try:
            outs = list_outputs(self._sd())
        except Exception as e:                   # noqa: BLE001
            raise ValueError(f"The audio system could not list its outputs: "
                             f"{_clean(str(e))}")
        counts, per_api = {}, {}
        for d in outs:
            k = d["name"].strip().lower()
            counts[k] = counts.get(k, 0) + 1
            ka = (k, d["hostapi"].strip().lower())
            per_api[ka] = per_api.get(ka, 0) + 1
        want = (self.device_name or "").strip().lower()
        host = (self.options.get("hostapi") or "").strip().lower()
        out = []
        for d in outs:
            k = d["name"].strip().lower()
            same = counts[k] > 1
            twin = per_api[(k, d["hostapi"].strip().lower())] > 1
            out.append({"name": d["name"], "hostapi": d["hostapi"],
                        "channels": d["channels"],
                        "label": describe_output(d) + (
                            ", the same name twice here: rename one in "
                            "Windows Sound settings" if twin else ""),
                        "same_name": same, "cannot_pick": twin,
                        "picked": bool(want) and k == want and (
                            not same or d["hostapi"].strip().lower() ==
                            host)})
        return out

    def save_device(self, name, hostapi=None, who=None, screen=None):
        """Save the announcement's own output to ltcplay_announce.json,
        every other key kept, and use it at once (no restart). `name` ""
        clears it. A name on more than one output needs its `hostapi`; one
        that is not on this PC is refused. Returns the sentence said."""
        name = (name or "").strip() if isinstance(name, str) else None
        if name is None:
            raise ValueError("device has to be an output's name, or empty "
                             "to clear it. Nothing was changed.")
        host = None
        if name:
            outs = list_outputs(self._sd())
            hits = [d for d in outs
                    if d["name"].strip().lower() == name.lower()]
            if not hits:
                raise ValueError(f"{_clean(name)!r} is not an output on this "
                                 f"PC. Nothing was changed.")
            if len(hits) > 1:
                h = (hostapi or "").strip().lower() \
                    if isinstance(hostapi, str) else ""
                pick = [d for d in hits if d["hostapi"].strip().lower() == h]
                if len(pick) > 1:
                    raise ValueError(same_api_sentence(name, pick))
                if len(pick) != 1:
                    raise ValueError(
                        f"{_clean(name)} matches {len(hits)} outputs on "
                        f"this PC: pick one: "
                        f"{'; '.join(describe_output(d) for d in hits)}. "
                        f"Nothing was changed.")
                host = pick[0]["hostapi"]
                name = pick[0]["name"]
            else:
                name = hits[0]["name"]
        with self.lock:
            try:
                with open(self.config_path, encoding="utf-8-sig") as fh:
                    doc = json.load(fh)
            except (OSError, ValueError) as e:
                raise ValueError(f"{_clean(self.config_path)} could not be "
                                 f"read ({_clean(str(e))}), so nothing was "
                                 f"changed.")
            if not isinstance(doc, dict):
                raise ValueError(f"{_clean(self.config_path)} is not an "
                                 f"object, so nothing was changed.")
            doc["device"] = name
            if host:
                doc["hostapi"] = host
            else:
                doc.pop("hostapi", None)
            # Checked BEFORE anything is written: a bad key anywhere in
            # the file refuses the save and leaves the file as it was.
            parse_config(doc, self.config_path)
            options = parse_options(doc, self.config_path)
            _write_json_atomic(self.config_path, doc)
            self.device_name = name
            self.options = options
            cw = self.config_watch
            if cw is not None:
                try:
                    cw.refresh(self.config_path)
                except Exception:                # noqa: BLE001
                    pass
        text = (f"Announcement output saved: "
                f"{_clean(name)}{' on ' + _clean(host) if host else ''}. It "
                f"is used while the show audio is not running." if name else
                "Announcement output cleared: announcements play only "
                "through the show audio.")
        self._emit(actor="operator", action="output", outcome="saved",
                   reason="picker", text=text, who=who, screen=screen)
        return text

    def _decode(self, ann_id):
        """Read and validate the file off disk: the one part of a Play
        press that touches disk. Deliberately called WITHOUT self.lock held
        (see play()): self.files is replaced only by reload_if_idle, under
        the lock and never while an announcement is claimed, so this is
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

    def _request_hold(self, who, screen, detail=None):
        """Ask the scheduler to Hold, through hold_requester, with THIS
        press's own who and screen so the journal attributes it the same
        way an operator's own Hold would (Jeff, 2026-09-26), and `detail`
        so it reads as held FOR this announcement rather than as a plain
        operator Hold press (review round 2, 2026-09-26:
        audit15_journal_noise2.py). Returns (refusal_or_None, epoch):
        refusal is None when Hold took effect, or the scheduler was
        already on Hold, or the show was already paused (see
        Service.hold_for_announcement); an announcement never needs the
        schedule to already be idle before it plays. epoch is used by
        _check_still_held, the SECOND, read-only check, never by this one.
        Called with no scheduler wired is a programming error (play() only
        calls this when hold_requester is not None), so anything
        hold_requester itself raises is still turned into a plain sentence
        rather than a 500 -- with no epoch to compare against, which is
        fine, because a raised exception is refused right here and the
        second check is never reached for this attempt."""
        try:
            return self.hold_requester(who, screen, detail=detail)
        except Exception as e:
            return _clean(str(e)), None

    def _check_still_held(self, claim_epoch):
        """The SECOND check, immediately before the stream opens: read-only,
        never asks for Hold again (review round 2, 2026-09-26:
        audit15_resume_race.py -- the old second hold_requester call here
        could silently re-pause a show the operator had just resumed
        during the file read). True when the claim from checkpoint 1 is
        still good. No hold_still_claimed wired (no scheduler at all) is
        vacuously fine. Anything hold_still_claimed itself raises is
        treated as NOT still held: the announcement then plays with nothing
        held rather than claim a Hold it cannot vouch for."""
        if self.hold_still_claimed is None:
            return True
        try:
            return bool(self.hold_still_claimed(claim_epoch))
        except Exception:
            return False

    def _note_held_failed(self, ann_id, why, epoch=None):
        """The schedule was Held for this press and then the file could not
        play. The Hold stays (only Resume ends it); the rack screen says so
        in a banner rather than leave a held schedule unexplained. Keyed to
        THIS press's own Hold (its claim epoch): the banner goes once that
        Hold is gone, and a later, different Hold never brings it back
        (review of PR #129, second round, 8)."""
        self.held_failed = {"id": ann_id, "name": LABELS[ann_id],
                            "why": _clean(why)}
        self._held_failed_epoch = epoch

    def _held_failed_gone(self, state):
        """True once the Hold the failed press made is over."""
        if self.hold_still_claimed is not None and \
                self._held_failed_epoch is not None:
            return not self._check_still_held(self._held_failed_epoch)
        return state != "HOLD"

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
        self.held_schedule = False
        self.hold_note = ""
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
        never = (self._player.frames_written == 0 and
                 self._clock() - self._player.started_at > START_S)
        lost = getattr(self._player, "lost", None) or (
            f"the output took no audio within {START_S:g} s of opening "
            f"(a wrong or disabled output?), so nothing was heard"
            if never else None)
        if lost:
            elapsed = self._player.elapsed_s
            state = self._current_state()
            self._finish()
            self._emit(actor="system", action="play", outcome="failed",
                      reason=lost,
                      text=f"{label} stopped after {elapsed:.0f} s: {lost}. "
                           f"Marked failed so another announcement can be "
                           f"played.", ann_id=ann_id, state=state)
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

    def on_show_started(self, state=None, reason=None):
        """Called by whoever wires this service to the scheduler (web.py's
        serve(), see its module docstring) the instant the scheduler
        decides to start a show. A push hook, not something this module
        polls for: schedule_service.py ticks on a thread of its own with no
        lock shared with this service, so an announcement that must stop
        the moment a show starts has to be told, not merely discovered on
        the next status() poll (review, blocker 1b).

        `reason` is "resume" when the show did not actually START, it
        merely CONTINUED from a Hold (an operator's Resume while an
        announcement was still playing), or "new" (also the default, for
        an older caller that does not pass it) for a genuinely new show
        beginning. Jeff's own intent is the same either way -- a playing
        announcement still fades and stops, because the show is moving
        again -- only the JOURNAL's wording differs: "the show resumed"
        vs "a show started" (review round 2, 2026-09-26:
        audit15_resume_fires_showstart.py).

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
            self._player.stop_reason = ("the show resumed"
                                        if reason == "resume" else
                                        "a show started")
            self._player.start_fade(fade_frames)

    def play(self, ann_id, who, screen, surface=False, automatic=None):
        """Play one announcement. `surface` True: the press came from a
        screen or the Stream Deck through the remote routes (remote.py),
        and `who` names the person signed in there or the surface itself,
        so it is not looked up on the operator list (the same rule as the
        scheduler's own surface presses, Jeff, 2026-10-07). Every other
        rule still applies.

        It plays whatever state the schedule is in (Jeff's field report,
        2026-10-28: announcements and the schedule are independent).
        Between shows the schedule is Held first; while a show is running
        (SHOW_STATES) nothing is Held, paused or stopped (Jeff,
        2026-10-26). When no Hold can be taken (the night is over, the
        scheduler is starting up, no schedule) it plays with nothing held
        and says so (no_hold_note). Nothing here ever starts a show, or
        disarms, stops or latches anything.

        It plays through the show audio while a show audio process runs
        (show_route), else on its own output (the config's "device").

        `automatic`: a play nobody pressed (the alert hold's Delayed,
        alerthold.py): a callable returning None while it may still play,
        or the sentence why not. Such a play never asks for a Hold (so the
        hold_requester's safety cuts never run: nothing on the rig is
        changed by a timer), is journaled with actor "system", and asks
        `automatic()` once before anything is claimed and again in the
        same locked section that starts the sound, so an Acknowledge or a
        Resume during the file read always wins."""
        actor = "system" if automatic is not None else "operator"
        if automatic is not None:
            surface = True
        who = _clean((who or "").strip())
        screen = _clean((screen or "").strip())
        tone = ann_id == TEST_TONE and surface and automatic is None
        if ann_id not in IDS and not tone:
            raise ValueError(f"{ann_id!r} is not one of {', '.join(IDS)}.")
        label = LABELS[ann_id]
        screen_txt = f" on the {screen}"

        with self.lock:
            self._settle()
            state = self._current_state()
            if tone and state in SHOW_STATES:
                text = (f"{who} pressed Test tone{screen_txt}, but a show "
                        f"is running. A test tone never plays during a "
                        f"show. Nothing was changed.")
                self._emit(actor=actor, action="test tone",
                          outcome="refused", reason="a show is running",
                          text=text, ann_id=ann_id, who=who, screen=screen,
                          state=state)
                raise ValueError(text)
            missing = self._operator_problem(who, screen)
            if missing:
                text = (f"A Play press on {label} did not say {missing}. "
                        f"Nothing was changed.")
                self._emit(actor=actor, action="play",
                          outcome="refused", reason=missing, text=text,
                          ann_id=ann_id, who=who or None,
                          screen=screen or None, state=state)
                raise ValueError(text)
            if not surface and \
                    who.lower() not in {n.lower() for n in self.operators}:
                reason = (f"{who!r} is not on the operator list "
                         f"({', '.join(self.operators) or 'empty'}).")
                text = (f"{who} pressed Play on {label} on the {screen}, "
                        f"but is not on the operator list. Nothing was "
                        f"changed.")
                self._emit(actor=actor, action="play",
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
                self._emit(actor=actor, action="play",
                          outcome="refused",
                          reason="another announcement is already playing",
                          text=text, ann_id=ann_id, who=who, screen=screen,
                          state=state)
                raise ValueError(text)
            st = ({"available": True, "length_s": TONE_S} if tone else
                  self.status_by_id.get(ann_id, {}))
            if not st.get("available"):
                text = (f"{who} pressed Play on {label}{screen_txt}, but "
                        f"it is not available: {st.get('reason')}")
                self._emit(actor=actor, action="play",
                          outcome="refused", reason=st.get("reason"),
                          text=text, ann_id=ann_id, who=who, screen=screen,
                          state=state)
                raise ValueError(text)
            # Where it plays, BEFORE any Hold: a press that cannot play
            # (the show audio's interface not open, no output picked, the
            # picked one unplugged) must not hold the schedule for nothing.
            route = self._show_route()
            dev = None
            if route is not None:
                chk = self.route_check()
                if chk["ok"]:
                    too_long = self._too_long(st.get("length_s"))
                    if too_long:
                        chk = {"ok": False, "why": too_long}
                if not chk["ok"]:
                    why = chk["why"] or "the show audio is not ready."
                    text = (f"{who} pressed Play on {label}{screen_txt}. "
                            f"Refused: no output device to play it on. "
                            f"{why}")
                    self._emit(actor=actor, action="play",
                              outcome="refused", reason=why, text=text,
                              ann_id=ann_id, who=who, screen=screen,
                              state=state)
                    raise ValueError(text)
            elif not self.device_set:
                text = (f"{who} pressed Play on {label}{screen_txt}. "
                        f"Refused: {self.DEVICE_NOT_SET}")
                self._emit(actor=actor, action="play",
                          outcome="refused", reason="no output device set",
                          text=text, ann_id=ann_id, who=who, screen=screen,
                          state=state)
                raise ValueError(text)
            else:
                try:
                    dev = self._resolve(self._sd())
                except Exception as e:
                    text = (f"{who} pressed Play on {label}{screen_txt}. It "
                            f"failed: {_clean(str(e))}")
                    self._emit(actor=actor, action="play",
                              outcome="failed", reason=_clean(str(e)),
                              text=text, ann_id=ann_id, who=who,
                              screen=screen, state=state)
                    raise ValueError(_clean(str(e)))
            # Between shows, every announcement Holds the SCHEDULE first,
            # through the same path the operator's own Hold uses, so the
            # next show does not start until the operator is back (Jeff,
            # 2026-10-26). Already on Hold counts as success. While a show
            # is running (SHOW_STATES) nothing is Held: the announcement
            # plays over the show, and the operator ends the show with
            # Stop if that is what is meant. A Hold the scheduler cannot
            # take (the night is over or closing, it is still starting up,
            # no schedule is loaded) is NOT a reason to refuse: the
            # announcement plays with nothing held and says why (Jeff's
            # field report, 2026-10-28). With no scheduler wired at all
            # (hold_requester None) nothing is held either.
            # claim_epoch is captured here and checked again, read-only,
            # right before the sound starts (_check_still_held): it is
            # what lets an operator's Resume, pressed during the file read
            # below, win outright rather than being silently undone
            # (review round 2, 2026-09-26: audit15_resume_race.py).
            claim_epoch = None
            over_show = state in SHOW_STATES
            held = False
            hold_note = ""
            if automatic is not None:
                why = self._automatic_refusal(automatic)
                if why:
                    text = (f"The {label} announcement was not played: "
                            f"{why}")
                    self._emit(actor=actor, action="play",
                              outcome="refused", reason=why, text=text,
                              ann_id=ann_id, who=who, screen=screen,
                              state=state)
                    raise ValueError(text)
            elif tone:
                hold_note = "A test tone holds nothing."
            elif self.hold_requester is not None and not over_show:
                hold_refusal, claim_epoch = self._request_hold(
                    who, screen,
                    detail=f"played the {label} announcement{screen_txt}")
                if hold_refusal:
                    claim_epoch = None
                    hold_note = no_hold_note(state, hold_refusal)
                else:
                    # Read again under no assumption: the scheduler's own
                    # tick, inside hold_for_announcement, may have started
                    # a show this very moment (pressed at showtime). It
                    # then held nothing, and the announcement plays over
                    # the show; a running show is never paused by one
                    # (Jeff, 2026-10-26).
                    state = self._current_state()
                    if state in SHOW_STATES:
                        over_show = True
                    else:
                        held = True
            if automatic is None and not held and not hold_note:
                hold_note = no_hold_note(state)
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
            self.held_schedule = held
            # The show audio's token for this attempt, taken now so the
            # recording can be sent to the audio process without the lock.
            self._route_token += 1
            token = self._route_token

        # The file read happens WITHOUT the lock held: self.files changes
        # only in reload_if_idle, under the lock and never while an
        # announcement is claimed, so this is safe to do unlocked, and it is
        # the one part of a Play press that touches disk. Holding the lock
        # across it would block Stop, a status poll and every other
        # operator action for as long as the read takes (review, minor).
        try:
            if tone:
                pcm = test_tone_pcm()
                channels, rate = 1, TONE_RATE
            else:
                pcm, channels, rate = self._decode(ann_id)
            # What is about to be sent, measured before anything is: a
            # recording that decoded to silence is never reported as
            # playing (Jeff's field report, 2026-10-28).
            peak = peak_dbfs(pcm)
            if peak < SILENT_DBFS:
                raise ValueError(
                    f"the recording is silent or unreadable: its loudest "
                    f"sample is {_db_text(peak)}, below "
                    f"{SILENT_DBFS:g} dBFS, so nothing was played")
        except Exception as e:
            with self.lock:
                # Only touch shared state if this is still the current
                # claim: a Stop and a fresh Play could have already run
                # while this decode was failing, and that fresh claim's
                # own outcome is not this attempt's to overwrite or log
                # over.
                if self._claim_gen == my_gen and self.playing == ann_id:
                    self.playing = None
                    if held:
                        self._note_held_failed(ann_id, _clean(str(e)),
                                               claim_epoch)
                    text = (f"{who} pressed Play on {label}{screen_txt}. "
                            f"It failed: {_clean(str(e))}")
                    self._emit(actor=actor, action="play",
                              outcome="failed", reason=_clean(str(e)),
                              text=text, ann_id=ann_id, who=who,
                              screen=screen, state=self._current_state())
            raise ValueError(_clean(str(e)))

        # Through the show audio: the recording is made ready for it (48
        # kHz, its outputs) and sent to the audio process in parts here,
        # still WITHOUT the lock (safety review of #158: none of this may
        # hold up Stop, a status poll or a show starting). Only the small
        # start message is sent under the lock below.
        loaded, load_why = self._load_on_show(token, pcm, rate)

        with self.lock:
            if self._claim_gen != my_gen or self.playing != ann_id:
                # A LATER claim (Stop, then Play again, possibly for the
                # same id) has already taken over while this attempt was
                # reading its file. This attempt has built nothing yet
                # (the sound is started further down, after this check),
                # so there is nothing of its own to close; it only needs
                # to say so and get out of the way.
                text = (f"{who} pressed Play on {label}{screen_txt}, but "
                        f"it was stopped and played again before this "
                        f"press finished reading its file. Nothing was "
                        f"changed.")
                self._emit(actor=actor, action="play",
                          outcome="refused",
                          reason="a later claim already started",
                          text=text, ann_id=ann_id, who=who, screen=screen,
                          state=self._current_state())
                raise ValueError(text)
            state = self._current_state()
            if automatic is not None:
                why = self._automatic_refusal(automatic)
                if why:
                    self.playing = None
                    text = (f"The {label} announcement was not played: "
                            f"{why}")
                    self._emit(actor=actor, action="play",
                              outcome="refused", reason=why, text=text,
                              ann_id=ann_id, who=who, screen=screen,
                              state=state)
                    raise ValueError(text)
            # The SECOND look at the Hold, immediately before the sound
            # starts: READ-ONLY, never asks for Hold again. An earlier
            # version re-Held here, which could silently re-pause a show
            # the operator had just RESUMED during the file read (review
            # round 2, 2026-09-26: audit15_resume_race.py). The operator's
            # own Resume wins: the announcement still plays (it is
            # independent of the schedule), with nothing held.
            if held and not self._check_still_held(claim_epoch):
                held = False
                self.held_schedule = False
                claim_epoch = None
                hold_note = ("The schedule was resumed while it loaded, so "
                             "it plays with nothing held.")
            elif over_show and not held and state not in SHOW_STATES:
                hold_note = ("The show ended while it loaded; it plays "
                             "with nothing held.")
            # Through the show audio when it runs NOW (Run may have been
            # pressed, or Stop, while the file was read).
            route_now = self._show_route()
            if route_now is not None or route is not None:
                chk = self.route_check() if route_now is not None else {
                    "ok": False, "why": "The show audio stopped while the "
                                        "announcement was loading."}
                if chk["ok"]:
                    too_long = self._too_long(pcm.shape[0] / float(rate)
                                              if rate else 0.0)
                    if too_long:
                        chk = {"ok": False, "why": too_long}
                    elif loaded is None or \
                            loaded[0].engine is not route_now.engine:
                        chk = {"ok": False, "why": load_why or (
                            "The show audio started or changed while the "
                            "announcement was loading.")}
                if not chk["ok"]:
                    self.playing = None
                    why = chk["why"] or "the show audio is not ready."
                    if held:
                        self._note_held_failed(ann_id, why, claim_epoch)
                    text = (f"{who} pressed Play on {label}{screen_txt}. It "
                            f"failed: {why} Press it again.")
                    self._emit(actor=actor, action="play",
                              outcome="failed", reason=why, text=text,
                              ann_id=ann_id, who=who, screen=screen,
                              state=state)
                    raise ValueError(text)
                started = self._start_on_show(route_now, token, loaded[1],
                                              ann_id, label, who, screen,
                                              screen_txt, held, claim_epoch,
                                              actor, state)
            else:
                started = self._start_on_device(dev, pcm, channels, rate,
                                                ann_id, label, who, screen,
                                                screen_txt, held,
                                                claim_epoch, actor, state)
            player, stream, where, detail = started
            self.held_failed = None
            self._player = player
            self._stream = stream
            self.hold_note = hold_note
            length = (pcm.shape[0] / float(rate)) if rate else 0.0
            detail.update(length_s=round(length, 2),
                          peak_dbfs=(None if peak == float("-inf") else
                                     round(peak, 1)))
            self.sent = detail
            text = (f"{who} played {label}{screen_txt} on {where}"
                    f"{', ' + _clean(detail['hostapi']) if detail.get('hostapi') else ''}"
                    f", {detail['channels']} channel(s), {length:.1f} s "
                    f"sent, peak {_db_text(peak)}. " +
                    ("The schedule is held for it; only Resume carries on."
                     if held else
                     "It plays while the schedule is on the alert hold; "
                     "nothing else was changed." if automatic is not None
                     else
                     hold_note if hold_note else
                     "It plays over the running show; nothing was paused "
                     "or stopped." if over_show else
                     "Nothing was held."))
            self._emit(actor=actor, action="play", outcome="started",
                      reason=f"show state {state or 'no scheduler'}",
                      text=text, ann_id=ann_id, who=who, screen=screen,
                      state=state)
            return self.status()

    def _too_long(self, length_s):
        """The sentence when a recording is too long for the show audio
        (SHOW_ROUTE_MAX_S), or None."""
        if length_s is not None and length_s > SHOW_ROUTE_MAX_S:
            return (f"It is {length_s:.0f} s long; through the show audio "
                    f"an announcement is at most {SHOW_ROUTE_MAX_S:.0f} s. "
                    f"Shorten the recording.")
        return None

    def _load_on_show(self, token, pcm, rate):
        """Called WITHOUT the lock, after the file read: when the show audio
        runs and the recording is not too long for it, resample it for the
        show and send it to the audio process in parts. Returns ((route,
        frames), None) when it went, else (None, why or None)."""
        route = self._show_route()
        if route is None:
            return None, None
        if rate and self._too_long(pcm.shape[0] / float(rate)):
            return None, None
        try:
            mix = to_show_mix(pcm, rate, route.rate,
                              len(self.options["show_outputs"]))
            if not route.load(token, mix):
                return None, ("The show audio process did not take the "
                              "recording.")
            return (route, mix.shape[0]), None
        except Exception as e:                   # noqa: BLE001
            return None, (f"The recording could not be made ready for the "
                          f"show audio: {_clean(str(e))}.")

    def _start_on_show(self, route, token, frames, ann_id, label, who,
                       screen, screen_txt, held, claim_epoch, actor, state):
        """Start the recording _load_on_show sent, on the show audio's
        announcement bus: one small message. Called with the lock held.
        Returns (player, stream, where), or raises ValueError."""
        outs = self.options["show_outputs"]
        try:
            gain = 10.0 ** (self.options["gain_db"] / 20.0)
            sent = route.play(token, [o - 1 for o in outs], gain)
        except Exception as e:                   # noqa: BLE001
            sent, why = False, _clean(str(e))
        else:
            why = "the show audio process did not take it."
        if not sent:
            self.playing = None
            if held:
                self._note_held_failed(ann_id, why, claim_epoch)
            text = (f"{who} pressed Play on {label}{screen_txt}. It failed "
                    f"to play through the show audio: {why}")
            self._emit(actor=actor, action="play", outcome="failed",
                      reason=why, text=text, ann_id=ann_id, who=who,
                      screen=screen, state=state)
            raise ValueError(f"{label} could not be played: {why}")
        self.last_good_device = route.device
        player = _BusPlayer(route, token, frames, route.rate, self._clock)
        return (player, _BusStream(route, token, route.rate),
                f"the show audio ({self._outs_text()} on "
                f"{_clean(route.device)})",
                {"route": "show audio", "device": route.device,
                 "hostapi": None, "channels": len(outs)})

    def _start_on_device(self, dev, pcm, channels, rate, ann_id, label, who,
                         screen, screen_txt, held, claim_epoch, actor,
                         state):
        """Open the announcement's own output and start the file on it.
        Called with the lock held. Returns (player, stream, where), or
        raises ValueError."""
        sd = self._sd()
        if dev is None:
            try:
                dev = self._resolve(sd)
            except Exception as e:               # noqa: BLE001
                self.playing = None
                if held:
                    self._note_held_failed(ann_id, _clean(str(e)),
                                           claim_epoch)
                text = (f"{who} pressed Play on {label}{screen_txt}. It "
                        f"failed: {_clean(str(e))}")
                self._emit(actor=actor, action="play", outcome="failed",
                          reason=_clean(str(e)), text=text, ann_id=ann_id,
                          who=who, screen=screen, state=state)
                raise ValueError(_clean(str(e)))
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
            if held:
                self._note_held_failed(ann_id, _clean(str(e)),
                                       claim_epoch)
            text = (f"{who} pressed Play on {label}{screen_txt}. It "
                    f"failed to open {_clean(self.device_name)}: "
                    f"{_clean(str(e))}")
            self._emit(actor=actor, action="play",
                      outcome="failed", reason=_clean(str(e)),
                      text=text, ann_id=ann_id, who=who, screen=screen,
                      state=state)
            raise ValueError(f"{label} could not be played: "
                             f"{_clean(str(e))}")
        self.last_good_device = dev["name"]
        return (player, stream, _clean(dev["name"]),
                {"route": "own output", "device": dev["name"],
                 "hostapi": dev.get("hostapi") or None, "channels": out_ch})

    @staticmethod
    def _automatic_refusal(check):
        """The sentence why an automatic play may not go on, or None. A
        check that raises is a no."""
        try:
            why = check()
        except Exception as e:                   # noqa: BLE001
            return f"its check could not be made ({_clean(str(e))})."
        return _clean(why) if why else None

    def test_tone(self, who, screen):
        """The rack screen's Test tone: 1 s of a soft 440 Hz at -20 dBFS
        through the same route and output an announcement would use now,
        never during a show and never with a Hold. Returns the status."""
        return self.play(TEST_TONE, who, screen, surface=True)

    def stop(self, who, screen, surface=False):
        """Stop the playing announcement's audio. Only the audio: the
        schedule's Hold (if it held one) stays until an operator's Resume,
        and nothing is disarmed, stopped or latched. `surface`: as play()."""
        with self.lock:
            self._settle()
            who = _clean((who or "").strip())
            screen = _clean((screen or "").strip())
            state = self._current_state()
            missing = self._operator_problem(who, screen)
            if missing:
                raise ValueError(f"The Stop press does not say {missing}. "
                                 f"Nothing was changed.")
            if not surface and \
                    who.lower() not in {n.lower() for n in self.operators}:
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
            if self.held_failed is not None and \
                    self._held_failed_gone(state):
                self.held_failed = None     # its own Hold is over
            device_ok, device_reason = self._probe_device()
            route = self.route_check((device_ok, device_reason))
            items = []
            for aid in IDS:
                st = self.status_by_id.get(
                    aid, {"available": False, "reason": "not loaded",
                         "length_s": None})
                items.append({"id": aid, "name": LABELS[aid],
                             "available": bool(st.get("available")),
                             "missing": bool(st.get("missing")),
                             "reason": st.get("reason"),
                             "length_s": st.get("length_s")})
            playing = None
            if self.playing is not None and self._player is not None:
                playing = {"id": self.playing, "name": LABELS[self.playing],
                          "held_schedule": bool(self.held_schedule),
                          "note": self.hold_note or None,
                          "elapsed_s": self._player.elapsed_s,
                          # Only once audio has actually gone to the
                          # output: until then it is starting, not
                          # playing (Jeff's field report, 2026-10-28).
                          "heard": self._player.frames_written > 0,
                          "sent": self.sent,
                          "length_s": (self.sent or {}).get("length_s")
                          or self.status_by_id.get(self.playing, {})
                          .get("length_s")}
            return {"ok": not self.error, "error": self.error or None,
                    "device": self.device_name or None,
                    "device_set": self.device_set,
                    "device_available": device_ok,
                    "device_reason": device_reason,
                    "hostapi": self.options.get("hostapi"),
                    "route": route,
                    "last_sent": self.sent,
                    "last_good_device": self.last_good_device,
                    "announcements": items, "playing": playing,
                    "show_state": state,
                    "over_show": state in SHOW_STATES,
                    "held_failed": self.held_failed,
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
