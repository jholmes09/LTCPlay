"""Booth alert audio: a short WAV note to a PRIVATE speaker in the booth,
never the PA (Jeff, 2026-10-07: "private path only (never the PA); app
picks exact device and channel pair; if device missing, stay silent and
show the on-screen banner; never fall back to show output").

Config, the "booth_alert" block of ltcplay_remote.json (written by hand
until the Settings screen, Phase 5):

    "booth_alert": {
      "device": "StudioLive 16R",      exact name as this computer lists it
      "channels": [15, 16],            the output pair, 1-based
      "files": {"deck": "C:\\\\...\\\\booth_deck.wav", "audio": "...",
                "video": "...", "gate": "...", "start": "...",
                "flames": "...", "link": "...", "test": "..."},
      "response_s": 30
    }

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

Why not announce.py: announcements are PUBLIC and Hold the show first.
This module never holds anything and never reaches the show output; a
test in selftest.py reads this file to prove it names neither.

Defaults are safe: no device, so the booth is off until configured (the
hotel has no 16R; the soak PC has nothing).
"""
import os
import threading

DEFAULT_RESPONSE_S = 30
MAX_NOTE_S = 8.0          # a booth note is a short line plus a tone
NOTE_KINDS = ("gate", "audio", "video", "deck", "start", "flames", "link",
              "test")


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
        if kind not in NOTE_KINDS:
            raise ValueError(f"{where}: {kind!r} is not an alert kind. One "
                             f"of: {', '.join(NOTE_KINDS)}.")
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
    device: the note is written to the stream on a thread of its own.
    `sd` is sounddevice or a stand-in; None imports the real one when a
    note is first played. `opener(sd, device_index, channels, rate)` is
    the stream factory, for tests."""

    def __init__(self, config=None, sd=None, journal=None, opener=None):
        self.config = parse_config(config)
        self._sd = sd
        self._journal = journal or (lambda text, **kw: None)
        self._opener = opener or self._open_stream
        self.last = ""
        self.playing = 0

    @property
    def enabled(self):
        return bool(self.config["device"])

    @property
    def device_name(self):
        return self.config["device"]

    def check_files(self):
        """{kind: problem sentence} for every file that could not play, as
        announce's own probe sees it. Empty when every file is sound."""
        out = {}
        for kind, path in self.config["files"].items():
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
        path = self.config["files"].get(kind)
        if not path:
            return False, (f"No booth note file is set for {kind} alerts, "
                           f"so nothing was played.")
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
        t = threading.Thread(target=self._write, args=(sd, dev, block, rate),
                             daemon=True, name="ltcplay-booth-note")
        self.playing += 1
        t.start()
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
            self.playing -= 1
