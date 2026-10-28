"""Booth alert audio: a short WAV note to a PRIVATE speaker in the booth,
never the PA (Jeff, 2026-10-07: "private path only (never the PA); app
picks exact device and channel pair; if device missing, stay silent and
show the on-screen banner; never fall back to show output").

Config, the "booth_alert" block of ltcplay_remote.json (written by hand
until the Settings screen, Phase 5):

    "booth_alert": {
      "device": "StudioLive 16R",      exact name as this computer lists it
      "channels": [15, 16],            the output pair, 1-based
      "response_s": 30
    }

The notes themselves need no setting (Jeff, 2026-10-28): the note for a
name (an alert kind, or a row of HOOKUPS) is the file booth_<name>.wav,
looked for in this order, the first one found plays:

  1. "files" in the block, when it names a path for that name (optional,
     {"deck": "C:\\\\...\\\\deck.wav"}): an explicit entry always wins,
     and when its file is missing the note is silent, never replaced;
  2. <the Settings and logs folder>\\Booth notes\\booth_<name>.wav: a
     person's own copy, which no install or update ever writes;
  3. <the program folder>\\Booth notes\\booth_<name>.wav: the notes the
     installer ships (packaging/windows/booth in this repo), replaced by
     every install and update.

So setting only the device and the channels is enough. EXPECTED_FILES is
the whole recorded set; the rack screen says how many were found
(notes_view) and packaging/windows/check_booth_notes.py checks the set
the installer ships.

Rules:
- the device is found by its EXACT name, case-insensitive, never a
  substring, never an index, never the system default
  (announce.resolve_output_device, the same helper). No device named: the
  player is off (enabled False) and every alert banner says so;
- a missing device at play time: silent, a sentence for the banner, and
  the alert service runs the pulse instead. Nothing else is tried;
- the note goes to the two named channels only; every other channel of
  the stream the note opens is zero, so a pair on the show mixer never
  carries it;
- the file is checked the way announce.py checks (announce._wav_info),
  at load (check_files) and again at play: the same function, so the two
  cannot disagree;
- the stream is this module's own, opened for the note and closed after.
  Whether the StudioLive 16R's driver allows that beside the show's ASIO
  client is unknown until the real PC (Oct 14); if it refuses, the alert
  banner says so and the pulse covers it.

Why not announce.py: announcements are PUBLIC, on their own named output,
and between shows they put the night on Hold first (during a show they play
over it and hold nothing; Jeff, 2026-10-26).
This module never holds anything and never reaches the show output; a
test in selftest.py reads this file to prove it names neither.

Defaults are safe: no device, so the booth is off until configured (the
hotel has no 16R; the soak PC has nothing).
"""
import os
import queue
import sys
import threading

DEFAULT_RESPONSE_S = 30
MAX_NOTE_S = 8.0          # a booth note is a short line plus a tone
# Notes play one after another, never two streams at once (the showtime
# hold's "held" and the alert's own note). More than this many waiting
# (a device that never finishes a note): the next one is refused with a
# sentence, so the alert service runs the pulse instead.
MAX_WAITING = 6
# The alert kinds (alerts.KINDS) that sound a note, and "test". lasers and
# users were recorded on 2026-10-28; "aim" is display only and has none.
NOTE_KINDS = ("gate", "audio", "video", "deck", "start", "flames", "link",
              "timecode", "test", "lasers", "users")
# The recorded notes for things that are not alert kinds here yet, in ONE
# table (Jeff, 2026-10-28): each row is a note's name and who plays it, so
# hooking one up is one line in the PR named. Two ways:
#   - an alert kind of exactly the note's name (alerts.KINDS and
#     SEVERITY): it sounds when raised and again every response_s until
#     acknowledged, like every alert;
#   - Alerts.play_note(name) for a one off event: it plays once, never
#     repeats and never runs the pulse.
HOOKUPS = (
    ("preroll_audio", "PR #162 pre-roll check: the audio check failed"),
    ("preroll_video", "PR #162 pre-roll check: the video check failed"),
    ("preroll_timecode", "PR #162 pre-roll check: the timecode check "
                         "failed"),
    ("preroll_lasers", "PR #162 pre-roll check: the laser check failed"),
    ("preroll_flames", "PR #162 pre-roll check: the flame check failed"),
    ("preroll_network", "PR #162 pre-roll check: the network check failed"),
    ("preroll_hold", "PR #162 pre-roll check: the show is held by it "
                     "(play_note, once)"),
    ("preroll_ok", "PR #162 pre-roll check: every check passed "
                   "(play_note, once)"),
    ("held", "alerthold.AlertHold.started: held at showtime for an open "
             "alert (play_note, once; wired here)"),
    ("continue_anyway", "PR #162: the operator pressed Continue anyway "
                        "(play_note, once)"),
    ("clean_ready", "PR #159 wave flamer cleaning: ready to clean "
                    "(play_note, once)"),
    ("clean_done", "PR #159 wave flamer cleaning: cleaning done "
                   "(play_note, once)"),
)
NOTE_NAMES = NOTE_KINDS + tuple(name for name, _who in HOOKUPS)
# The folder of notes, beside the program and in the Settings and logs
# folder alike (installer.iss ships it as {app}\\Booth notes).
FOLDER = "Booth notes"


def note_file(name):
    """The file a note is looked for by: booth_<name>.wav."""
    return f"booth_{name}.wav"


EXPECTED_FILES = tuple(note_file(n) for n in NOTE_NAMES)


def shipped_folder():
    """The notes the installer ships: <program folder>\\Booth notes in the
    Windows app (beside ltcplay.exe), packaging/windows/booth from this
    repo."""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(os.path.abspath(
            sys.executable)), FOLDER)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "packaging", "windows", "booth")


def note_folders(settings_folder):
    """Where a note is looked for when "files" names none, first match
    wins: a person's own copy in <settings>\\Booth notes, then the shipped
    one. The installer never writes the first, so an update never
    overwrites a note someone replaced there."""
    out = []
    if settings_folder:
        out.append(os.path.join(settings_folder, FOLDER))
    out.append(shipped_folder())
    return tuple(out)


def find_note(name, files, folders):
    """The path of the note for `name`, or None: the explicit entry in
    `files` when there is one (even if its file is missing: an explicit
    entry is never replaced), else booth_<name>.wav in the first of
    `folders` that has it."""
    explicit = (files or {}).get(name)
    if explicit:
        return explicit
    fn = note_file(name)
    for d in folders or ():
        p = os.path.join(d, fn)
        if os.path.isfile(p):
            return p
    return None


def notes_view(files, folders):
    """{"expected": 24, "found": n, "missing": [file names]} for the rack
    screen: which of the recorded notes this machine has."""
    found, missing = 0, []
    for name in NOTE_NAMES:
        p = find_note(name, files, folders)
        if p and os.path.isfile(p):
            found += 1
        else:
            missing.append(note_file(name))
    return {"expected": len(NOTE_NAMES), "found": found,
            "missing": missing}


def parse_config(doc, where="ltcplay_remote.json booth_alert"):
    """{"device": str, "channels": (a, b), "files": {kind: path},
    "response_s": int}. None or {} is the safe default (off). Anything
    malformed raises ValueError with a plain sentence."""
    out = {"device": "", "channels": (1, 2), "files": {},
           "response_s": DEFAULT_RESPONSE_S}
    if doc is None:
        return out
    if not isinstance(doc, dict):
        raise ValueError(f"{where} has to be an object.")
    dev = doc.get("device", "")
    if dev is not None and not isinstance(dev, str):
        raise ValueError(f"{where}: device has to be the output's exact "
                         f"name.")
    out["device"] = (dev or "").strip()
    ch = doc.get("channels", [1, 2])
    if not (isinstance(ch, (list, tuple)) and len(ch) == 2 and
            all(isinstance(c, int) and not isinstance(c, bool) and c >= 1
                for c in ch) and ch[0] != ch[1]):
        raise ValueError(f"{where}: channels has to be two different "
                         f"output numbers counted from 1, like [15, 16].")
    out["channels"] = (int(ch[0]), int(ch[1]))
    files = doc.get("files", {}) or {}
    if not isinstance(files, dict):
        raise ValueError(f"{where}: files has to map an alert kind to a "
                         f"WAV path.")
    for kind, path in files.items():
        if kind not in NOTE_NAMES:
            raise ValueError(f"{where}: {kind!r} is not an alert kind or a "
                             f"booth note. One of: {', '.join(NOTE_NAMES)}.")
        if not isinstance(path, str) or not path.strip():
            raise ValueError(f"{where}: the file for {kind} has to be a "
                             f"path.")
        out["files"][kind] = path.strip()
    rs = doc.get("response_s", DEFAULT_RESPONSE_S)
    if not (isinstance(rs, int) and not isinstance(rs, bool) and
            1 <= rs <= 600):
        raise ValueError(f"{where}: response_s has to be a whole number of "
                         f"seconds between 1 and 600.")
    out["response_s"] = rs
    return out


def _import_sounddevice():
    try:
        import sounddevice
        return sounddevice
    except Exception:                    # noqa: BLE001
        return None


def _announce():
    """announce.py's device resolver and WAV check, imported only when a
    note is checked or played: the GPL path never loads announcements
    (a test proves it), and a booth that is off never needs them."""
    from . import announce
    return announce


def _as_float(pcm):
    import numpy as np
    if pcm.dtype == np.float32:
        return pcm
    if pcm.dtype == np.int16:
        return (pcm.astype(np.float32) / 32768.0)
    if pcm.dtype == np.int32:
        return (pcm.astype(np.float32) / 2147483648.0)
    if pcm.dtype == np.uint8:
        return (pcm.astype(np.float32) - 128.0) / 128.0
    return pcm.astype(np.float32)


class BoothPlayer:
    """play(kind) -> (ok, sentence). Never raises, never blocks on the
    device: the note is written to the stream by a thread of its own,
    one note after another in the order they were played. `kind` is an
    alert kind or any note name (NOTE_NAMES). `sd` is sounddevice or a
    stand-in; None imports the real one when a note is first played.
    `opener(sd, device_index, channels, rate)` is the stream factory, for
    tests. `folders` are where booth_<name>.wav is looked for when the
    config's "files" names none (note_folders; none: only "files")."""

    def __init__(self, config=None, sd=None, journal=None, opener=None,
                 folders=()):
        self.config = parse_config(config)
        self.folders = tuple(folders or ())
        self._sd = sd
        self._journal = journal or (lambda text, **kw: None)
        self._opener = opener or self._open_stream
        self.last = ""
        self.playing = 0
        self._lock = threading.Lock()
        self._queue = queue.Queue()
        self._worker = None

    @property
    def enabled(self):
        return bool(self.config["device"])

    @property
    def device_name(self):
        return self.config["device"]

    def note_path(self, name):
        """The file the note for `name` plays from, or None (find_note)."""
        return find_note(name, self.config["files"], self.folders)

    def notes(self):
        """notes_view for this player's files and folders."""
        return notes_view(self.config["files"], self.folders)

    def check_files(self):
        """{name: problem sentence} for every note file that is there (or
        named in "files") and could not play, as announce's own probe sees
        it. Empty when every such file is sound. A note with no file at all
        is not a problem here: notes() lists it, and it stays silent."""
        out = {}
        for kind in NOTE_NAMES:
            path = self.note_path(kind)
            if not path:
                continue
            try:
                _pcm, _ch, _rate, length = _announce()._wav_info(
                    path, decode=False)
                if length > MAX_NOTE_S:
                    out[kind] = (f"{path} is {length:.1f} s long; a booth "
                                 f"note is at most {MAX_NOTE_S:g} s.")
            except ValueError as e:
                out[kind] = str(e)
        return out

    def play(self, kind):
        ok, why = self._play(kind)
        self.last = why
        return ok, why

    def _play(self, kind):
        name = self.config["device"]
        if not name:
            return False, ("No booth speaker is configured, so the alert "
                           "note was not played.")
        if kind not in NOTE_NAMES:
            return False, (f"{kind} has no booth note, so nothing was "
                           f"played.")
        path = self.note_path(kind)
        if not path:
            where = ""
            if self.folders:
                where = (f", and {note_file(kind)} is not in "
                         f"{' or '.join(self.folders)}")
            return False, (f"No booth note file is set for {kind} alerts"
                           f"{where}, so nothing was played.")
        sd = self._sd if self._sd is not None else _import_sounddevice()
        if sd is None:
            return False, ("sounddevice is not installed here, so the booth "
                           "note was not played.")
        try:
            dev = _announce().resolve_output_device(sd, name)
        except ValueError as e:
            # Missing device: silent. Nothing else is ever used instead.
            return False, f"Booth speaker {name} is not attached. {e}"
        except Exception as e:           # noqa: BLE001
            return False, (f"Booth speaker {name} could not be looked up "
                           f"({type(e).__name__}: {e}).")
        a, b = self.config["channels"]
        hi = max(a, b)
        have = int(dev.get("channels") or 0)
        if have < hi:
            return False, (f"Booth speaker {name} has {have} outputs, so "
                           f"channels {a} and {b} are not there.")
        try:
            pcm, ch, rate, length = _announce()._wav_info(path, decode=True)
        except ValueError as e:
            return False, f"The booth note for {kind} could not play: {e}"
        if length > MAX_NOTE_S:
            return False, (f"The booth note for {kind} is {length:.1f} s "
                           f"long; a booth note is at most {MAX_NOTE_S:g} s.")
        block = self._block(pcm, ch, a, b, hi)
        with self._lock:
            if self.playing >= MAX_WAITING:
                return False, (f"Booth speaker {name} still has "
                               f"{self.playing} notes to play, so the note "
                               f"for {kind} was not played.")
            self.playing += 1
            self._queue.put((sd, dev, block, rate))
            if self._worker is None:
                self._worker = threading.Thread(
                    target=self._run, daemon=True, name="ltcplay-booth-note")
                self._worker.start()
        return True, (f"Booth note for {kind} playing on {name}, outputs "
                      f"{a} and {b} ({length:.1f} s).")

    @staticmethod
    def _block(pcm, ch, a, b, hi):
        """frames x hi, float32: the note on outputs a and b (counted from
        1), zero on every other output of the stream."""
        import numpy as np
        f = _as_float(pcm).reshape(-1, ch)
        block = np.zeros((f.shape[0], hi), dtype=np.float32)
        left = f[:, 0]
        right = f[:, 1] if ch > 1 else f[:, 0]
        block[:, a - 1] = left
        block[:, b - 1] = right
        return block

    @staticmethod
    def _open_stream(sd, index, channels, rate):
        return sd.OutputStream(device=index, channels=channels,
                               samplerate=rate, dtype="float32")

    def _run(self):
        """The one writer: each note in turn, never two at once."""
        while True:
            sd, dev, block, rate = self._queue.get()
            self._write(sd, dev, block, rate)

    def _write(self, sd, dev, block, rate):
        try:
            stream = self._opener(sd, dev["index"], block.shape[1], rate)
            with stream as s:
                s.write(block)
        except Exception as e:           # noqa: BLE001
            self.last = (f"Booth speaker {dev.get('name')} refused the note "
                         f"({type(e).__name__}: {e}).")
            try:
                self._journal(self.last, fault=True, action="booth",
                              outcome="refused")
            except Exception:            # noqa: BLE001
                pass
        finally:
            with self._lock:
                self.playing -= 1
