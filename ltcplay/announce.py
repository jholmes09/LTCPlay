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

The output device is never the system default. A show's device is picked by
config, by name, and stays that device even if it briefly disappears: see
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
            f"The operator list {path} could not be used: "
            f"{str(e).rstrip('.')}. Using "
            f"{', '.join(DEFAULT_OPERATORS)} until it is fixed.")
    return names, ""


# ------------------------------------------------------------- the config --
def parse_config(doc, where):
    """{"device": name, "files": {"delayed": path, "cancellation": path,
    "cannot_continue": path}}. Raises ValueError with a sentence."""
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
    """By name, never by index: an index shifts when something else on the
    machine unplugs or replugs, and following the name is what lets a
    device that goes away mid-run come back on its own. No name at all is a
    config error, not a fallback to the system default."""
    if not name or not str(name).strip():
        raise ValueError("No output device is named for announcements. "
                         "There is no default: announcements never use "
                         "the system output, which is the device the show "
                         "audio is on.")
    outputs = list_outputs(sd)
    want = str(name).strip().lower()
    hits = [d for d in outputs if want in d["name"].lower()]
    exact = [d for d in hits if d["name"].strip().lower() == want]
    if exact:
        hits = exact
    if len(hits) == 1:
        return hits[0]
    names = ", ".join(d["name"] for d in outputs) or "nothing"
    if not hits:
        raise ValueError(f"{name!r} is not attached. Nothing else will be "
                         f"used in its place. Outputs on this machine: "
                         f"{names}.")
    raise ValueError(f"{name!r} matches {len(hits)} outputs, so it is not "
                     f"specific enough: {names}.")


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


# ------------------------------------------------------------------ player --
class _Player:
    """How much of a loaded announcement has actually been handed to the
    output device.

    Driven by the real audio callback in production, one block at a time.
    A test drives it the same way, by calling next_block itself: no clock,
    no thread, no sleep, and so no wall time anywhere in what it proves."""

    __slots__ = ("pcm", "channels", "rate", "total_frames", "frames_written",
                "done")

    def __init__(self, pcm, channels, rate):
        self.pcm = pcm
        self.channels = channels
        self.rate = rate
        self.total_frames = pcm.shape[0]
        self.frames_written = 0
        self.done = self.total_frames == 0

    def next_block(self, n):
        import numpy as np
        start = self.frames_written
        end = min(start + n, self.total_frames)
        block = np.zeros((n, self.channels), dtype=self.pcm.dtype)
        if end > start:
            block[:end - start] = self.pcm[start:end]
        self.frames_written = end
        if end >= self.total_frames:
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
                 operators_folder=None):
        self.config_path = config_path
        self._sd_obj = sd
        # A plain callable, or None. See interlock_refusal and the module
        # docstring: this is the whole coupling to the scheduler.
        self.state_provider = state_provider
        # Where to read ltcplay_operators.json from. None means the real
        # machine folder (data_dir()); a test points this at a tempdir so
        # it never touches, or depends on, anything really on disk.
        self.operators_folder = operators_folder
        self.lock = threading.RLock()
        self.journal = deque(maxlen=JOURNAL)
        self.error = ""
        self.device_name = None
        self.files = {}
        self.status_by_id = {}
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
                         f"{self.config_path}.")
        except OSError as e:
            self.error = (f"The announcements file {self.config_path} "
                         f"cannot be read: {e.strerror or e}.")
        except ValueError as e:
            self.error = (f"The announcements file {self.config_path} is "
                         f"not valid JSON: {e}.")
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
        reason by the time anyone could press it."""
        if not os.path.exists(path):
            return {"available": False,
                    "reason": f"{path} does not exist.", "length_s": None}
        try:
            with wave.open(path, "rb") as w:
                frames = w.getnframes()
                rate = w.getframerate()
        except (OSError, EOFError, wave.Error) as e:
            return {"available": False,
                    "reason": f"{path} could not be read as a WAV file: "
                             f"{e}.", "length_s": None}
        return {"available": True, "reason": None,
                "length_s": frames / float(rate) if rate else 0.0}

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
            return False, f"The audio system could not be reached: {e}"
        try:
            resolve_output_device(sd, self.device_name)
        except ValueError as e:
            return False, str(e)
        return True, None

    def _open(self, sd, dev, ann_id):
        import numpy as np
        path = self.files[ann_id]
        with wave.open(path, "rb") as w:
            n = w.getnframes()
            rate = w.getframerate()
            channels = w.getnchannels()
            sampwidth = w.getsampwidth()
            raw = w.readframes(n)
        dtype = {1: np.uint8, 2: np.int16, 4: np.int32}.get(sampwidth)
        if dtype is None:
            raise ValueError(f"{path} is a {sampwidth * 8}-bit WAV, which "
                             f"is not supported.")
        pcm = np.frombuffer(raw, dtype=dtype)
        pcm = pcm.reshape(-1, channels) if channels > 1 else pcm.reshape(-1, 1)
        out_ch = min(channels, dev["channels"]) or 1
        player = _Player(pcm[:, :out_ch] if out_ch < channels else pcm,
                         out_ch, rate)

        def cb(outdata, frames, tinfo, status):
            block = player.next_block(frames)
            outdata[:, :block.shape[1]] = block
            if outdata.shape[1] > block.shape[1]:
                outdata[:, block.shape[1]:] = 0

        stream = sd.OutputStream(device=dev["index"], channels=out_ch,
                                 samplerate=rate, blocksize=1024,
                                 dtype=pcm.dtype.name, callback=cb)
        stream.start()
        return player, stream

    # -- the journal, one call site ---------------------------------------
    def _emit(self, *, actor, action, outcome, reason, text, ann_id=None,
              who=None, screen=None, state=None):
        """The one place a log row is produced. A logging PR is being built
        in parallel to redirect this to the two stream logger in section 9;
        until then it feeds this service's own journal, in the scheduler's
        own shape (actor, action, outcome, reason, a plain sentence)."""
        row = {"at": datetime.now().astimezone().isoformat(
                   timespec="seconds"),
               "actor": actor, "action": action, "outcome": outcome,
               "reason": reason, "text": text, "who": who or None,
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
        """Notice a file that finished on its own since the last look. Real
        playback runs on the audio driver's own thread; ltcplay only finds
        out it ended the next time something asks, the same way the
        scheduler only advances when ticked. The page already polls status
        continuously, which is what drives this in practice."""
        if self._player is not None and self._player.done \
                and self.playing is not None:
            ann_id = self.playing
            elapsed = self._player.elapsed_s
            state = self._current_state()
            self._finish()
            self._emit(actor="system", action="play", outcome="finished",
                      reason="finished normally",
                      text=f"{LABELS[ann_id]} finished playing, "
                           f"{elapsed:.0f} s, finished normally.",
                      ann_id=ann_id, state=state)

    def play(self, ann_id, who, screen):
        with self.lock:
            self._settle()
            who = (who or "").strip()
            screen = (screen or "").strip()
            state = self._current_state()
            if ann_id not in IDS:
                raise ValueError(f"{ann_id!r} is not one of "
                                 f"{', '.join(IDS)}.")
            label = LABELS[ann_id]
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
            screen_txt = f" on the {screen}"
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
                        f"failed: {e}")
                self._emit(actor="operator", action="play",
                          outcome="failed", reason=str(e), text=text,
                          ann_id=ann_id, who=who, screen=screen, state=state)
                raise ValueError(str(e))
            try:
                player, stream = self._open(sd, dev, ann_id)
            except Exception as e:
                text = (f"{who} pressed Play on {label}{screen_txt}. It "
                        f"failed to open {self.device_name}: {e}")
                self._emit(actor="operator", action="play",
                          outcome="failed", reason=str(e), text=text,
                          ann_id=ann_id, who=who, screen=screen, state=state)
                raise ValueError(f"{label} could not be played: {e}")
            self.last_good_device = dev["name"]
            self.playing = ann_id
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
            who = (who or "").strip()
            screen = (screen or "").strip()
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
