"""Is the show's audio interface attached? Asked before anyone presses Run.

The show audio plays on the interface the show file names (clock.audio
.device: the PreSonus, the Scarlett). Once Run is pressed the audio
process owns that device and the show clock reports its loss itself (the
Audio chip reads lost or fault, the Stream Deck says AUDIO LOST). Before
Run, and after a Stop, nobody looked: a PC set up with the interface
unplugged, or plugged into a port Windows gave a new name, sat with a
quiet rack screen until Run failed with a sentence on the Programming
screen (2026-10-15).

This watch, Fire & Ice only (web.serve starts it with a scheduler and a
show conductor), reads the device names every show file in the show
folder asks for and, while no show session is running or starting, lists
the outputs this PC has. A name that is not there raises the rack
screen's "audio" alert once per outage, naming the device and the outputs
that were seen, and the Audio chip reads "not attached". When it comes
back, the log says so; the banner stays until a person acknowledges it
(alerts.py: there is no timer).

The outputs are listed by a short-lived process of its own, never by
re-initialising PortAudio in the engine: the engine's own PortAudio may be
playing the booth note, and on Windows an ASIO driver loaded a second
time while the show's audio process holds it can disturb the show. While
a session runs, nothing is listed at all, for the same reason.

Pure parts (which devices are wanted, which are missing, the sentence)
take plain data, for the selftest.
"""
import glob
import json
import os
import threading
import time

CHECK_S = 30.0
LIST_TIMEOUT_S = 20.0


def wanted_devices(folder):
    """{device name: show file} for every show file in `folder` whose
    clock follows the show audio (source audio_master). Files that are not
    shows, or do not parse, are left out."""
    out = {}
    for p in sorted(glob.glob(os.path.join(folder, "*.json"))):
        try:
            with open(p, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict) or not isinstance(doc.get("cues"),
                                                        list):
            continue
        clk = doc.get("clock")
        if not isinstance(clk, dict) or clk.get("source") != "audio_master":
            continue
        audio = clk.get("audio")
        dev = audio.get("device") if isinstance(audio, dict) else None
        if isinstance(dev, str) and dev.strip():
            out.setdefault(dev.strip(), os.path.basename(p))
    return out


def missing(wanted, outputs):
    """The wanted names (exact, case-insensitive: announce.py's rule) that
    are not among `outputs`."""
    have = {str(o).strip().lower() for o in outputs}
    return [n for n in wanted if n.strip().lower() not in have]


def sentence(name, show_file, outputs, every_s=CHECK_S):
    listed = ", ".join(sorted(outputs)) or "none"
    return (f"The show audio interface {name!r} (named in {show_file}) is "
            f"not attached: there is no audio output by that name on this "
            f"PC. Outputs seen: {listed}. The show cannot start until it is "
            f"plugged in, into the same USB port as always. It is looked "
            f"for again every {every_s:g} seconds.")


def _list_outputs_child(conn):
    """In the fresh process: the output device names PortAudio sees."""
    try:
        from .showaudio import import_sounddevice
        sd = import_sounddevice()
        names = sorted({str(d.get("name", "")).strip()
                        for d in sd.query_devices()
                        if d.get("max_output_channels", 0) > 0})
        conn.send(("ok", names))
    except Exception as e:                   # noqa: BLE001
        conn.send(("error", f"{type(e).__name__}: {e}"))
    finally:
        conn.close()


def list_outputs_in_a_fresh_process(timeout_s=LIST_TIMEOUT_S):
    """The output device names, from a process of its own (see the module
    docstring). Raises OSError with a sentence when it cannot say."""
    import multiprocessing
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    p = ctx.Process(target=_list_outputs_child, args=(child,),
                    name="ltcplay-audio-outputs")
    p.daemon = True
    p.start()
    child.close()
    try:
        if not parent.poll(timeout_s):
            raise OSError(f"the audio device list did not come back within "
                          f"{timeout_s:g} s")
        kind, payload = parent.recv()
    except (EOFError, OSError) as e:
        raise OSError(f"the audio device list could not be read: {e}") from e
    finally:
        parent.close()
        p.join(2.0)
        if p.is_alive():
            p.kill()
    if kind != "ok":
        raise OSError(f"the audio devices could not be listed: {payload}")
    return payload


class AudioInterfaceWatch:
    """The watch itself. `busy` says whether a show session is running or
    starting (then nothing is listed); `lister` lists the outputs (the
    fresh-process one by default); `alerts` is alerts.Alerts or None."""

    def __init__(self, folder, alerts=None, log=None, lister=None,
                 busy=lambda: False, clock=time.monotonic,
                 interval_s=CHECK_S):
        self.folder = folder
        self.alerts = alerts
        self.log = log or (lambda text: None)
        self.lister = lister or list_outputs_in_a_fresh_process
        self.busy = busy
        self.clock = clock
        self.interval_s = interval_s
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._state = {"checked": None, "wanted": {}, "outputs": [],
                       "missing": [], "error": "", "skipped": "",
                       "missing_since": None}

    def check(self):
        """One check; returns the state. Never raises."""
        now = self.clock()
        st = dict(self._state)
        try:
            wanted = wanted_devices(self.folder)
        except Exception as e:                   # noqa: BLE001
            wanted = {}
            st["error"] = f"show files could not be read: {e}"
        st["wanted"] = wanted
        if not wanted:
            st.update(missing=[], outputs=st["outputs"], skipped="",
                      checked=now, missing_since=None)
            self._set(st)
            return st
        if self.busy():
            st["skipped"] = "a show session is running; its own clock " \
                            "reports the device"
            self._set(st)
            return st
        st["skipped"] = ""
        try:
            outputs = list(self.lister())
            st["error"] = ""
        except Exception as e:                   # noqa: BLE001
            st["error"] = str(e)
            self.log(f"Audio interface check: {e}")
            self._set(st)
            return st
        miss = missing(wanted, outputs)
        was = st["missing"]
        st.update(outputs=outputs, missing=miss, checked=now)
        if miss:
            if miss != was:
                st["missing_since"] = now
                text = sentence(miss[0], wanted[miss[0]], outputs,
                                self.interval_s)
                self.log("Audio interface: " + text)
                if self.alerts is not None:
                    try:
                        self.alerts.raise_("audio", text)
                    except Exception as e:       # noqa: BLE001
                        self.log(f"the audio alert could not be raised: {e}")
        else:
            st["missing_since"] = None
            if was:
                self.log(f"Audio interface: {was[0]!r} is attached again. "
                         f"The banner stays until someone acknowledges it.")
        self._set(st)
        return st

    def _set(self, st):
        with self._lock:
            self._state = st

    def view(self):
        """For the rack screen: {"missing", "outputs", "checked_ago_s",
        "skipped", "error"}."""
        with self._lock:
            st = dict(self._state)
        ago = None if st["checked"] is None else round(
            max(0.0, self.clock() - st["checked"]), 1)
        return {"missing": list(st["missing"]), "outputs": list(st["outputs"]),
                "checked_ago_s": ago, "skipped": st["skipped"],
                "error": st["error"], "wanted": dict(st["wanted"])}

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="ltcplay-audio-watch")
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.check()
            except Exception as e:                   # noqa: BLE001
                self.log(f"Audio interface check failed: {e}")
            if self._stop.wait(self.interval_s):
                return
