"""One show, running: everything from a timeline path to packets on the wire.

This exists so the terminal and the web page cannot drift apart. Two start
paths means two sets of defaults, two places to forget the preshow fallback,
and a web page that behaves subtly unlike the command everyone tested with.
There is one path, and both front ends are views onto it.

Nothing here touches a terminal or a socket for the UI. It raises SessionError
with a sentence a person can act on, and exposes snapshot() for whatever is
drawing.
"""
import os
import time

from . import audio as audio_mod
from . import netmap as netmap_mod
from . import rigwatch as rigwatch_mod
from . import settings as settings_mod
from . import timeline as timeline_mod
from .ltc import LTCDecoder
from .output import Sender
from . import version as version_mod
from .player import Player
from .showlog import ShowLog
from .tc import tc_to_frames

BLOCK = 512


class SessionError(Exception):
    """A failure with a sentence attached, not a stack trace."""


class _NullSender:
    """Stands in for the real one when --no-output is on."""

    def __init__(self, nm):
        self.universe_count = len(nm.universes)
        self.packets_sent = 0
        self.send_errors = 0
        self.reopens = 0
        self.last_error = ""
        self.last_ok_at = time.monotonic()

    @property
    def seconds_since_ok(self):
        return 0.0

    def send_frame(self, channels):
        self.packets_sent += self.universe_count
        self.last_ok_at = time.monotonic()

    def blackout(self):
        pass

    def close(self):
        pass


class WavSource:
    """Feeds a WAV file in real time. Lets you rehearse without the rig."""

    def __init__(self, path, block=BLOCK):
        import wave
        self.w = wave.open(path, "rb")
        self.rate = self.w.getframerate()
        self.channels = self.w.getnchannels()
        self.width = self.w.getsampwidth()
        if self.width not in (2, 3, 4):
            raise SessionError(f"{path}: {self.width*8}-bit WAV not supported")
        self.block = block

    def blocks(self):
        import struct
        scale = float(1 << (self.width * 8 - 1))
        t0 = time.monotonic()
        sent = 0
        while True:
            raw = self.w.readframes(self.block)
            if not raw:
                return
            n = len(raw) // (self.width * self.channels)
            out = []
            for i in range(n):
                off = i * self.width * self.channels
                b = raw[off:off + self.width]
                if self.width == 2:
                    v = struct.unpack_from("<h", b)[0]
                elif self.width == 4:
                    v = struct.unpack_from("<i", b)[0]
                else:
                    v = int.from_bytes(b, "little", signed=True)
                out.append(v / scale)
            sent += n
            due = t0 + sent / self.rate
            slp = due - time.monotonic()
            if slp > 0:
                time.sleep(slp)
            yield out, time.monotonic()


def ltc_seconds(fr, tl):
    """Incoming frame digits to seconds, in the timeline's frame of reference."""
    return tc_to_frames(fr.h, fr.m, fr.s, fr.f, tl.count, tl.drop) / tl.fps


class Session:
    def __init__(self, timeline_path, no_output=False, wav=None, bind=None,
                 networks=None, log_path=None, no_log=False, idle=None,
                 gaps=None, on_lost=None, offset_ms=0.0, jump_threshold=0.15,
                 freewheel_ms=250, hold_ms=2000, on_end="blackout",
                 fps=None, drop=None, device=None, channel=None, rate=None,
                 echo_log=False, sd=None, allow_missing=False,
                 auto_reload=False):
        self.timeline_path = timeline_path
        self.allow_missing = allow_missing
        self.auto_reload = auto_reload
        self.no_output = no_output
        self.wav = wav
        self.bind = bind
        self.networks = networks
        self.log_path = log_path
        self.no_log = no_log
        self.idle_arg = idle
        self.gaps_arg = gaps
        self.on_lost_arg = on_lost
        self.offset_ms = offset_ms
        self.jump_threshold = jump_threshold
        self.freewheel_ms = freewheel_ms
        self.hold_ms = hold_ms
        self.on_end = on_end
        self.fps = fps
        self.drop = drop
        self.cli_in = {k: v for k, v in (("device", device),
                                         ("channel", channel),
                                         ("rate", rate)) if v}
        self.echo_log = echo_log
        self._sd = sd

        self.from_web = False
        self._lock = None
        self.blackout_sent = False
        self.tl = self.nm = self.nm_path = None
        self.log = self.sender = self.player = self.dec = self.audio = None
        self.rig = None
        self.src = None
        # Advatek scene triggers. None until open() reads a show file that
        # describes them; direct FSEQ playback needs none of this.
        self.trigger = None
        self.trigger_problems = []
        # The show clock, from the show file's "clock" block. None, the GPL
        # case, means LTC in on an audio input exactly as it has always been.
        self.clock = None
        self.clock_errors = 0
        self.notes = []          # things worth saying once, at startup
        self.problems = []       # preflight warnings about the cues
        self.decode_errors = 0
        self.started_at = None
        self.banner = ""
        self.input_summary = ""
        self.input_error = ""
        self.input_name = ""
        self._running = False

    # -- building ---------------------------------------------------------
    def open(self):
        """Load and wire everything. Raises SessionError with a readable line."""
        try:
            self.tl = timeline_mod.Timeline.load(self.timeline_path,
                                                 fps=self.fps, drop=self.drop)
        except (ValueError, FileNotFoundError) as e:
            raise SessionError(str(e))
        if self.tl.show_dir_note:
            self.notes.append(self.tl.show_dir_note)

        self.nm_path = self.networks or os.path.join(self.tl.show_dir,
                                                     "xlights_networks.xml")
        if not os.path.exists(self.nm_path):
            raise SessionError(
                f"Could not find the controller map:\n  {self.nm_path}\n"
                f"That is xlights_networks.xml, and it should sit in the show "
                f"folder beside the .fseq files. Either \"show_dir\" in the "
                f"timeline points somewhere else, or give the real path.")
        self.nm = netmap_mod.load(self.nm_path)
        if not self.nm.universes and not self.no_output:
            raise SessionError(f"{self.nm_path} has no ArtNet or E1.31 "
                               f"universes to send to.")

        if not self.no_log:
            p = self.log_path or os.path.join(
                os.path.dirname(os.path.abspath(self.timeline_path)),
                "ltcplay.log")
            try:
                self.log = ShowLog(p, echo=self.echo_log)
            except OSError as e:
                # A bundle on a read-only volume, a team folder with no
                # write permission, a locked card. The show can still run;
                # only the log cannot be written, and a stack trace at a
                # console at 8pm helps nobody. Round 4, 2026-09-13.
                self.log = None
                self.notes.append(
                    f"Could not open the show log ({e.strerror or e}). "
                    f"This folder is not writable, so there will be no "
                    f"record of tonight. Everything else runs normally; "
                    f"copy the show somewhere writable to get the log "
                    f"back.")
            if self.log is not None:
                self.log.timeline = self.tl

        try:
            self.sender = (_NullSender(self.nm) if self.no_output
                           else Sender(self.nm, bind_ip=self.bind, log=self.log))
        except OSError as e:
            raise SessionError(str(e))

        idle = self.idle_arg
        if idle and not os.path.isabs(idle):
            cand = os.path.join(self.tl.show_dir, idle)
            idle = cand if os.path.exists(cand) else idle
        idle = idle or self.tl.idle_fseq
        self.idle = idle

        self.player = Player(self.tl, self.nm, self.sender,
                             jump_threshold=self.jump_threshold,
                             freewheel_ms=self.freewheel_ms,
                             hold_ms=(self.tl.hold_ms
                                      if self.tl.hold_ms is not None
                                      and self.hold_ms == 2000
                                      else self.hold_ms),
                             offset_ms=(self.tl.offset_ms
                                        if self.tl.offset_ms is not None
                                        and not self.offset_ms
                                        else self.offset_ms),
                             on_end=self.on_end,
                             gaps=self.gaps_arg or self.tl.gaps,
                             on_lost=self.on_lost_arg or self.tl.on_lost,
                             idle_path=idle,
                             auto_reload=self.auto_reload,
                             log=self.log)
        if self.log:
            self.log.player = self.player

        # Advatek SHOWTime scene triggers, the ALTERNATE playback mode. Built
        # here so a mapping mistake is a startup problem in daylight rather
        # than a dead rig the first time someone reaches for the backup.
        self.trigger_problems = []
        if self.tl.trigger is not None:
            from .trigger import SceneTrigger
            self.trigger = SceneTrigger(self.tl.trigger, log=self.log)
            self.player.trigger = self.trigger
            self.trigger_problems = self.tl.trigger.check_against(self.tl,
                                                                  self.nm)
            for pr in self.trigger_problems:
                self.notes.append("scene triggers: " + pr)

        self.problems = self.player.open_cues()
        if self.log:
            for pr in self.problems:
                self.log.event("preflight", pr)
        if not any(c.fseq for c in self.tl.cues):
            raise SessionError("No cue loaded, so there is nothing to play. "
                               "Check the show folder still holds the .fseq "
                               "files this timeline names.")
        # A cue that would not open is a hole in the show, not a warning.
        # It happened on 2026-09-12: MonsterMash.fseq was still being written
        # by xLights, preflight said so in one line of the log, the run went
        # ahead, and the rig stood dark for the four minutes and fifty-five
        # seconds that cue was meant to fill. Nothing on screen said why.
        # Refuse to start instead, and make the operator say otherwise.
        dead = [c for c in self.tl.cues if c.fseq is None]
        # If EVERY render fails to decompress, it is not every render: it is
        # zstandard. Sending the operator to wait on xLights when the fix is
        # to run the installer again costs a night. Round 4, 2026-09-13.
        if dead and len(dead) == len(self.tl.cues) and len(dead) > 1:
            decomp = [pr for pr in self.problems if "will not decompress" in pr]
            if len(decomp) == len(dead):
                raise SessionError(
                    f"None of the {len(dead)} sequences will decompress. When "
                    f"it is every one of them it is not the renders, it is "
                    f"the zstandard library this program reads them with. "
                    f"Double-click 'Install ltcplay.command' again to rebuild "
                    f"it.\n  First one said: {decomp[0]}")
        if dead and not self.allow_missing:
            names = "\n  ".join(f"{c.tc_text}  {c.name}  "
                                f"({os.path.basename(c.path)})" for c in dead)
            raise SessionError(
                f"{len(dead)} of {len(self.tl.cues)} cues will not open, so "
                f"the rig would stand dark for their whole slot:\n  {names}\n"
                f"The usual cause is xLights still writing the render. Wait "
                f"for it to finish and run again, or pass --allow-missing to "
                f"run anyway with those slots empty.")

        if self.player.on_lost == "preshow" and (
                not idle or self.player.idle_cue is None):
            self.player.on_lost = "blackout"
            self.notes.append(
                "No preshow sequence is loaded, so a lost feed blacks out "
                "instead of returning to a look."
                + ("" if not idle else
                   f" {os.path.basename(idle)} is named in the show file but "
                   f"would not open."))

        # The clock, and only when the show file names one. Built after the
        # cues open, because free running to the end of a show needs to know
        # how long the show is.
        if self.tl.clock is not None:
            from . import clock as clock_mod
            try:
                self.clock = clock_mod.build(
                    self.tl.clock, self.tl, self.player.feed_timecode,
                    log=self.log, no_output=self.no_output,
                    bind_ip=self.bind, on_stop=self.player.drop_clock)
            except clock_mod.ClockConfigError as e:
                raise SessionError(str(e))
            # The display reads the clock's health from the player, the way
            # it reads the rig watch and the input.
            self.player.clock = self.clock
            self.notes.append(f"Show clock: {self.tl.clock.summary()}.")
            if self.clock.master:
                # on_lost is a policy for a timecode feed that dies. Here the
                # clock owns the pixels: a cue that ends or is halted hands
                # them back to the idle look directly. A clock thread that
                # stalls mid-cue holds the frame until it catches up; it
                # must never start a free run that outranks the clock.
                if self.player.on_lost != "hold":
                    if self.tl.on_lost:
                        self.notes.append(
                            f"'on_lost' is {self.tl.on_lost!r} in the show "
                            f"file. It does not apply while this machine is "
                            f"the show clock: a stopped cue goes to the idle "
                            f"look.")
                    self.player.on_lost = "hold"
                # This machine makes the timecode, so there is nothing to
                # listen to and no input is opened at all.
                if self.wav:
                    raise SessionError(
                        "This show file makes this machine the show clock, "
                        "so a WAV of timecode has nothing to drive. Run it "
                        "without the WAV.")
                self.input_source = {}
                self.dev = None
                self.channel = 1
                self.rate = 48000
                self.input_summary = ("none, this machine is the show clock "
                                      "(Art-Net timecode)")
                self.dec = LTCDecoder(self.rate)
                return self

        inp, source, conflict = settings_mod.resolve(
            settings_mod.load(), self.tl.input, self.cli_in)
        self.input_source = source
        if conflict:
            self.notes.append(conflict)

        if self.wav:
            self.src = WavSource(self.wav)
            self.rate = self.src.rate
            self.dev = None
            self.channel = 1
            self.input_summary = f"WAV file {os.path.basename(self.wav)}"
        else:
            sd = self._sd or _import_sounddevice()
            self._sd = sd
            self.channel = int(inp.get("channel") or 1)
            where = source.get("device", "the system default")
            try:
                self.dev = audio_mod.resolve_device(sd, inp.get("device"))
                if self.channel > self.dev["channels"]:
                    raise audio_mod.DeviceError(
                        f"{self.dev['name']} has {self.dev['channels']} "
                        f"input(s), so there is no input {self.channel}.")
                self.rate = audio_mod.negotiate_rate(sd, self.dev,
                                                     inp.get("rate"),
                                                     channels=self.channel)
                self.input_summary = (f"{self.dev['name']}, in {self.channel} "
                                      f"of {self.dev['channels']}, "
                                      f"{self.rate}Hz (from {where})")
                self.input_name = self.dev["name"]
            except audio_mod.DeviceError as e:
                # A missing interface used to refuse the whole start, which
                # left the rig dark over exactly the problem the preshow loop
                # exists to cover. Run the show, hold the preshow, and keep
                # knocking at the input. Asked for by Jeff, 2026-09-14.
                self.dev = None
                self.input_error = str(e)
                self.input_name = inp.get("device") or ""
                self.rate = int(inp.get("rate") or 48000)
                self.input_summary = (
                    (inp.get("device") or "the system default input")
                    + f", in {self.channel}: not attached")
                # Deliberately NOT a note: notes are permanent for the
                # run, and this condition ends the moment the interface
                # appears. The live warning covers it and withdraws itself.
                if self.log:
                    self.log.event("input", str(e).split("\n")[0])

        self.dec = LTCDecoder(self.rate)
        return self

    # -- running ----------------------------------------------------------
    def _handle(self, samples, captured_at):
        try:
            for fr in self.dec.feed(samples):
                back = (self.dec.position - fr.end_sample) / float(self.rate)
                self.player.feed_timecode(ltc_seconds(fr, self.tl),
                                          captured_at - back,
                                          drop=fr.drop, text=str(fr))
                clk = self.clock
                if clk is not None:
                    # Its own guard: a clock fault must never cost the
                    # chase engine a frame, nor be counted as a decode error.
                    try:
                        clk.ltc_frame(fr.h, fr.m, fr.s, fr.f,
                                      captured_at - back)
                    except Exception as e:
                        self.clock_errors += 1
                        if self.log:
                            self.log.event("clock-error",
                                           f"{type(e).__name__}: {e}",
                                           throttle_s=5.0)
        except Exception as e:
            # This runs on the CoreAudio callback thread. Letting it out stops
            # the stream with no error anywhere a person can see it.
            self.decode_errors += 1
            if self.log:
                self.log.event("decode-error", f"{type(e).__name__}: {e}",
                               throttle_s=5.0)

    def start(self):
        if self._running:
            return self
        if self.player is None:
            self.open()
        # One sender on the rig, across processes. See onlyone.py.
        if not self.no_output and self._lock is None:
            from . import onlyone
            try:
                self._lock = onlyone.OutputLock(
                    note=f"{os.path.basename(self.timeline_path)} "
                         f"({'web' if self.from_web else 'terminal'})"
                ).acquire()
            except onlyone.AlreadyRunning as e:
                who = f"  It says: {e.holder}" if e.holder else ""
                raise SessionError(
                    "Another ltcplay on this Mac is already sending to the "
                    "rig." + who + "\nTwo players on the same universes fight "
                    "frame by frame and the rig looks broken. Stop that one "
                    "first -- it is either the Run window or the Web window.")
        if not self.wav and not (self.clock is not None
                                 and self.clock.master):
            # With no device resolved, hand it a placeholder carrying the NAME
            # the operator chose. That name is what the supervisor follows when
            # the interface appears; an index would be meaningless.
            dev = self.dev or {"name": self.input_name, "index": None,
                               "channels": self.channel, "rate": self.rate}
            self.audio = audio_mod.InputSource(
                self._sd, dev, self.channel, self.rate, BLOCK,
                self._handle, self.log, on_rate_change=self._rate_changed)
            self.player.audio = self.audio
        if not self.no_output:
            # Its own thread, its own beat, nowhere near the output path.
            self.rig = rigwatch_mod.RigWatch(
                [u.ip for u in self.nm.universes], log=self.log).start()
            self.player.rig = self.rig
        step = self.player.start()
        self.started_at = time.monotonic()
        self._running = True
        if self.clock is not None:
            # Run pressed. A master still sends nothing until a cue plays.
            self.clock.start()
        # The show file may ask for this mode to be armed from the start. Off
        # unless it says so: a backup that arms itself is not a backup.
        if self.tl.trigger is not None and self.tl.trigger.enabled:
            armed, msg = self.arm_trigger(True)
            self.notes.append(msg if msg else
                              "Scene triggers armed by the show file.")
            if not armed:
                self.notes.append("Scene triggers did NOT arm; this show is "
                                  "playing sequences directly.")
        mode = ("no output (display only)" if self.no_output
                else f"{self.sender.universe_count} universes")
        self.banner = (
            f"ltcplay: {self.tl.name or os.path.basename(self.timeline_path)}, "
            f"{len(self.tl.cues)} cues, {self.tl.rate_label} fps timecode, "
            f"{step}ms output step, {mode}"
            + (f", preshow {os.path.basename(self.idle)}" if self.idle else ""))
        if self.log:
            from . import brand as brand_mod
            self.log.info("=" * 72)
            self.log.info(brand_mod.contact_line())
            self.log.info(self.banner)
            self.log.info(f"timeline {os.path.abspath(self.timeline_path)}")
            self.log.info(f"networks {self.nm_path}: {self.nm.summary()}")
            self.log.info(f"input {self.input_summary}")
            for n in self.notes:
                self.log.event("note", n)
            for c in self.tl.cues:
                self.log.info(f"  cue {c.tc_text}  {c.name}")
        if self.audio is not None:
            try:
                if not self.audio.start():
                    # Not fatal any more: the show is up on the preshow look
                    # and the supervisor keeps trying. Say so once, in words.
                    self.input_error = self.audio.last_error
                    if self.log:
                        self.log.event("input", self.audio.last_error)
            except Exception:
                # The output thread and the socket are already live at this
                # point, so a failed audio open used to leave an engine
                # driving the rig that NOTHING could stop: the web Control
                # never stored the session, the page said "idle", and the
                # next Start put a second engine on the same universes.
                # Found by an adversarial audit, 2026-09-13. Unwind fully
                # and blackout, so a failed start leaves a dark rig and no
                # threads.
                try:
                    self.stop()
                except Exception:
                    pass
                raise
        return self

    def _rate_changed(self, rate):
        """The input that turned up runs a different clock than we guessed.

        Decoding LTC at the wrong sample rate does not produce silence, it
        produces steady, plausible, wrong timecode. Rebuild the decoder."""
        self.rate = int(rate)
        self.dec = LTCDecoder(self.rate)
        if self.dev:
            self.dev = dict(self.dev, rate=self.rate)
        if self.log:
            self.log.event("input", f"input clock is {self.rate}Hz; "
                                    f"decoder rebuilt to match")

    def reset_input(self):
        """Rebuild the timecode input from nothing, without touching output.

        This is the red button followed by the green button, minus the part
        that blacks out the rig. The show keeps running on whatever it is
        running on; only the listening side is torn down and built again.

        It always rebuilds PortAudio itself, not just the stream. A USB
        interface pulled mid-show leaves PortAudio holding a stale device
        list, and from then on every attempt to open ANY input fails with
        paInternalError for the life of the process; reopening the stream
        cannot clear that. Asked for by Jeff, 2026-09-14, so audio can be
        fixed while the show free-runs.
        """
        if self.wav:
            raise SessionError("This run is fed from a WAV file, not an "
                               "input.")
        if not self._running:
            raise SessionError("Nothing is running.")
        if self.clock is not None and self.clock.master:
            raise SessionError("This machine is the show clock, so there is "
                               "no timecode input to rebuild.")
        sd = self._sd or _import_sounddevice()
        self._sd = sd
        old, self.audio = self.audio, None
        self.player.audio = None
        if old is not None:
            try:
                old.stop()
            except Exception:
                pass
        # The whole point: clear the audio system, not just the stream.
        try:
            sd._terminate()
            sd._initialize()
            rebuilt = True
        except Exception as e:
            rebuilt = False
            if self.log:
                self.log.event("input", f"could not rebuild the audio "
                                        f"system: {e}")
        # Re-resolve by NAME. After a replug the index has almost certainly
        # moved, and the saved name is what the operator actually chose.
        inp, _source, _c = settings_mod.resolve(
            settings_mod.load(), self.tl.input, self.cli_in)
        name = inp.get("device") or self.input_name or ""
        dev = None
        try:
            dev = audio_mod.resolve_device(sd, name or None)
            ch = int(inp.get("channel") or self.channel or 1)
            if ch <= dev["channels"]:
                want = audio_mod.negotiate_rate(sd, dev, inp.get("rate"),
                                                channels=ch)
                if want != self.rate:
                    self._rate_changed(want)
                self.dev, self.channel = dev, ch
                self.input_name = dev["name"]
        except audio_mod.DeviceError as e:
            self.input_error = str(e)
            dev = None
        src = audio_mod.InputSource(
            sd, self.dev or {"name": name, "index": None,
                             "channels": self.channel, "rate": self.rate},
            self.channel, self.rate, BLOCK, self._handle, self.log,
            on_rate_change=self._rate_changed)
        self.audio = src
        self.player.audio = src
        # A fresh decoder too: half a frame left in the old one decodes as a
        # sync error the moment real timecode arrives.
        self.dec = LTCDecoder(self.rate)
        opened = src.start()
        self.input_summary = (
            f"{self.dev['name']}, in {self.channel} of {self.dev['channels']}, "
            f"{self.rate}Hz (rebuilt)" if self.dev else
            f"{name or 'the system default input'}, in {self.channel}: "
            f"not attached")
        if opened:
            self.input_error = ""
        else:
            self.input_error = src.last_error
        if self.log:
            self.log.event("input",
                           f"timecode input rebuilt by hand"
                           + ("" if rebuilt else " (the audio system itself "
                                                "could not be rebuilt)")
                           + f": {self.input_summary}")
        return {"opened": bool(opened), "audio_system_rebuilt": rebuilt,
                "input": self.input_summary, "why": self.input_error}

    def retarget_input(self, device=None, channel=None, rate=None):
        """Point a RUNNING show at a different input, without stopping it.

        The alternative is stopping the engine to fix a cable, which blacks
        the rig out in front of an audience over a problem that has nothing
        to do with what is on the trees."""
        if self.wav:
            raise SessionError("This run is fed from a WAV file, not an "
                               "input.")
        if not self._running or self.audio is None:
            raise SessionError("Nothing is running.")
        sd = self._sd or _import_sounddevice()
        self._sd = sd
        ch = int(channel or self.channel or 1)
        name = device if device is not None else self.input_name
        dev = None
        try:
            dev = audio_mod.resolve_device(sd, name or None)
            if ch > dev["channels"]:
                raise audio_mod.DeviceError(
                    f"{dev['name']} has {dev['channels']} input(s), so there "
                    f"is no input {ch}.")
            want = audio_mod.negotiate_rate(sd, dev, rate, channels=ch)
        except audio_mod.DeviceError as e:
            raise SessionError(str(e))
        old, self.audio = self.audio, None
        self.player.audio = None
        try:
            old.stop()
        except Exception:
            pass
        self.dev, self.channel = dev, ch
        if want != self.rate:
            self._rate_changed(want)
        self.input_name = dev["name"]
        self.input_error = ""
        self.input_summary = (f"{dev['name']}, in {ch} of {dev['channels']}, "
                              f"{self.rate}Hz (changed mid-show)")
        src = audio_mod.InputSource(sd, dev, ch, self.rate, BLOCK,
                                    self._handle, self.log,
                                    on_rate_change=self._rate_changed)
        self.audio = src
        self.player.audio = src
        opened = src.start()
        if not opened:
            self.input_error = src.last_error
        if self.log:
            self.log.event("input", f"switched to {self.input_summary}")
        return opened

    def pump_wav(self, stop_check=None, on_block=None):
        """Drive a WAV source. Only used when there is no live input."""
        for samples, t in self.src.blocks():
            if stop_check and stop_check():
                return
            if not self._running:
                return
            self._handle(samples, t)
            if on_block:
                on_block()

    @property
    def running(self):
        return self._running

    # -- Advatek scene triggers -------------------------------------------
    def arm_trigger(self, on):
        """Turn the alternate playback mode on or off while running.

        Arming does two things together, and they only work together: it mutes
        the Advatek addresses on the pixel sender, and it starts firing a
        scene trigger at the top of each cue. The Mk3 plays a recorded scene
        only while no live pixel data is arriving, so a trigger sent without
        the mute is accepted and ignored -- the box keeps showing the live
        feed and the show looks fine. Returns (armed, message)."""
        if self.tl is None or self.tl.trigger is None:
            return False, ("This show file does not describe any scene "
                           "triggers, so there is nothing to hand over to.")
        if self.trigger is None:
            return False, "Scene triggers are not available in this session."
        if self.no_output:
            return False, ("This session is display only, so there is no "
                           "output to hand over.")
        on = bool(on)
        if on == self.player.trigger_armed:
            return on, ""
        if on:
            if self.trigger_problems:
                return False, ("Not arming: " + self.trigger_problems[0])
            hit = self.sender.set_muted(self.tl.trigger.mute)
            if not hit:
                self.sender.set_muted(())
                return False, ("None of the addresses in 'mute' are "
                               "controllers in this show, so nothing would "
                               "stop receiving live data and every trigger "
                               "would be ignored.")
            self.trigger.start()
            self.player._fired_key = None
            self.player.trigger_armed = True
            msg = (f"Scene triggers armed. {len(hit)} controller(s) muted, "
                   f"the rest still streaming.")
        else:
            self.player.trigger_armed = False
            self.player._fired_key = None
            self.sender.set_muted(())
            self.trigger.stop()
            msg = ("Scene triggers off. Every controller is being streamed "
                   "to again.")
        if self.log:
            self.log.event("trigger", msg)
        return on, msg

    # -- the show clock ---------------------------------------------------
    def clock_play(self, cue=None):
        """Start the show clock at 00:00:00:00 for one cue.

        Only when this machine is the clock. `cue` is a cue's name, its file
        name or its timecode in the show file; None means the first cue.
        Returns the cue."""
        if not self._running:
            raise SessionError("Nothing is running. Press Run first.")
        if self.clock is None or not self.clock.master:
            raise SessionError("This show follows incoming timecode, so this "
                               "machine cannot start the clock.")
        cues = self.tl.cues
        if not cues:
            raise SessionError("This show has no cues to play.")
        if cue is None:
            pick = cues[0]
        else:
            want = str(cue).strip().lower()
            hits = [c for c in cues
                    if want in (c.name.lower(),
                                os.path.basename(c.path).lower(),
                                c.tc_text.lower())]
            if len(hits) != 1:
                raise SessionError(
                    f"{cue!r} " + ("matches more than one cue" if hits else
                                   "is not a cue in this show") +
                    f". The cues are: {', '.join(c.name for c in cues)}.")
            pick = hits[0]
        if pick.fseq is None or not pick.duration:
            raise SessionError(
                f"{pick.name} did not open ({os.path.basename(pick.path)}), "
                f"so there is no length to run its timecode for, and it "
                f"would run forever. Fix the render and restart.")
        # A free run left over from the last cue ending would outrank the
        # clock, and the rig would ignore the cue just started.
        if self.player.freerun_epoch is not None:
            self.player.release()
        try:
            self.clock.play(pick.tc_seconds, pick.duration, pick.name)
        except ValueError as e:
            # A Stop that lands while this is on its way in stops the clock
            # first. The caller gets the same kind of sentence as every other
            # refusal here, not the clock's own exception.
            raise SessionError(str(e))
        return pick

    def clock_halt(self):
        """Stop the show clock now. Nothing is sent until the next cue."""
        if self.clock is not None:
            self.clock.halt()

    def snapshot(self):
        """Everything a display needs, as plain data.

        The warnings come from the same function the terminal screen uses, so
        the page and the console cannot disagree about whether something is
        wrong."""
        from . import display as display_mod
        p, tl, dec = self.player, self.tl, self.dec
        now = time.monotonic()
        # Read the clock ONCE and derive both cues from that one value. Reading
        # p.current_cue, p.next_cue and p.tc_seconds separately lets the engine
        # tick between them, and the page then draws a "next" cue that has
        # already started, counting down through zero into negative numbers.
        tc = p.tc_seconds
        cue = p.current_cue
        nxt = tl.next_cue(tc) if (tc is not None and tc >= 0) else p.next_cue
        if cue is not None and (tc is None or tc < 0):
            cue = None
        rate, drop, confident = dec.detected_rate
        a = self.audio
        s = self.sender
        input_used = not (self.clock is not None and self.clock.master)
        snap = {
            "running": self._running,
            "show": tl.name or os.path.basename(self.timeline_path),
            "timeline": os.path.basename(self.timeline_path),
            "uptime": (now - self.started_at) if self.started_at else 0.0,
            # Between cues on a master clock the chase engine reads LOST,
            # which the page draws red. Nothing is lost: no cue is playing.
            # The terminal reads the same function, so the two agree.
            "state": display_mod.shown_state(p),
            "source": p.source,
            "ltc_in": p.last_ltc_text or "--:--:--:--",
            "ltc_age": (now - p.last_ltc_at) if p.last_ltc_at else None,
            "playing": (tl.format(p.tc_seconds) if p.tc_seconds is not None
                        and p.tc_seconds >= 0 else "--:--:--:--"),
            "sync_ms": p.sync_delta_ms,
            "blackout_in": (max(0.0, p.hold_s - (now - p.last_ltc_at))
                            if p.last_ltc_at and p.state == "FREEWHEEL"
                            else None),
            "rate_in": rate,
            "rate_drop": drop,
            "rate_confident": confident,
            "rate_measured": dec.measured_fps,
            "rate_label": (tl.rate_label if rate is None else None),
            "timeline_rate": tl.rate_label,
            "timeline_fps": tl.fps,
            "timeline_drop": tl.drop,
            "ltc_frames": dec.frames_decoded,
            "sync_errors": dec.sync_errors,
            "now": None,
            "next": None,
            "universes": getattr(s, "universe_count", 0),
            "frames_out": p.frames_sent,
            "send_errors": getattr(s, "send_errors", 0),
            "socket_reopens": getattr(s, "reopens", 0),
            "quiet_dests": getattr(s, "quiet_destinations", 0),
            "broadcast_dests": list(getattr(s, "broadcast_dests", [])),
            "rig_total": (len(self.rig.baseline) if self.rig
                          and self.rig.baseline else 0),
            "rig_missing": (list(self.rig.missing) if self.rig else []),
            "rig_watchable": (None if not self.rig or self.rig.usable is None
                              else bool(self.rig.usable)),
            "rig_checked": (self.rig.seconds_since_check if self.rig else None),
            "since_ok": getattr(s, "seconds_since_ok", None),
            "jumps": p.jumps,
            "loop_errors": p.loop_errors,
            "restarts": p.thread_restarts,
            "no_output": self.no_output,
            "build": version_mod.status(),
            "build_id": version_mod.build()[0],
            "show_build": version_mod.show_status(
                self.tl.show_dir, self.timeline_path),
            "trigger_available": getattr(self.tl, "trigger", None) is not None,
            "trigger_armed": bool(getattr(p, "trigger_armed", False)),
            "trigger_problems": list(getattr(self, "trigger_problems", [])),
            "trigger_summary": (self.tl.trigger.summary(self.nm)
                                if getattr(self.tl, "trigger", None) is not None
                                else ""),
            "trigger_muted": list(getattr(s, "muted", [])),
            "trigger_fired": (self.trigger.fired if self.trigger else 0),
            "trigger_last": (self.trigger.last_fired if self.trigger else ""),
            "trigger_since_fire": (self.trigger.seconds_since_fire
                                   if self.trigger else None),
            "trigger_errors": ((self.trigger.fire_errors + self.trigger.dropped)
                               if self.trigger else 0),
            "trigger_error": (self.trigger.last_error if self.trigger else ""),
            "trigger_since_error": (self.trigger.seconds_since_error
                                    if self.trigger else None),
            "input": self.input_summary,
            "input_attached": (True if self.wav
                               else None if not input_used
                               else bool(a and a.attached)),
            # False when this machine is the show clock: there is no input
            # to be open, and drawing it red would be a false alarm.
            "input_used": input_used,
            # Derived from what is true NOW. Holding the first failure
            # meant the page still showed it long after the input came back.
            "input_error": ("" if (a is not None and a.attached)
                            else (getattr(a, "last_error", "")
                                  or self.input_error)),
            "input_opens": (a.open_errors if a else 0),
            "input_name": self.input_name,
            "input_channel": self.channel,
            "level": (a.level.hold if a else None),
            "level_verdict": (a.level.verdict() if a else None),
            "audio_quiet": (a.seconds_since_block if a else None),
            "audio_reopens": (a.reopens if a else 0),
            "notes": list(self.notes),
            "problems": list(self.problems),
            "warnings": display_mod.warnings_for(p, dec, tl),
            "history": display_mod.history_for(p),
            "on_lost": p.on_lost,
            "hold_ms": int(p.hold_s * 1000),
            "gaps": p.gaps,
            "override": p.override,
            "freerun": p.freerun_epoch is not None,
            "feed_state": p.feed_state,
            "has_preshow": p.idle_cue is not None,
            "stale": [c.name for c in p.stale_cues()],
            "auto_reload": p.auto_reload,
            "cues": [{"tc": c.tc_text, "name": c.name,
                      "seconds": c.tc_seconds,
                      "duration": c.duration} for c in tl.cues],
        }
        if self.clock is not None:
            snap["clock"] = self.clock.snapshot()
        if cue is not None and cue.fseq is not None and tc is not None:
            el = tc - cue.tc_seconds
            # Two clocks, deliberately. "tc" is where the show is; "seq" is
            # where xLights is. A note written against the first cannot be
            # found in the sequence without the second.
            from .tc import format_seq
            snap["now"] = {"name": cue.name, "tc": cue.tc_text,
                           "file": os.path.basename(cue.path),
                           "ends": tl.format(cue.end_seconds),
                           "elapsed": el, "duration": cue.duration,
                           "left": cue.duration - el,
                           "frame": p.current_frame,
                           "seq": format_seq(el),
                           "seq_total": format_seq(cue.duration),
                           "seq_frames": cue.fseq.frame_count,
                           "seq_step_ms": cue.fseq.step_time_ms}
        if nxt is not None:
            left = (nxt.tc_seconds - tc) if (tc is not None and tc >= 0) else None
            snap["next"] = {"name": nxt.name, "tc": nxt.tc_text,
                            "in": left if (left is None or left >= 0) else None}
        return snap

    def stop(self):
        if not self._running and self.player is None:
            return
        # Whatever happens below, the rig must not be left holding a frame.
        # Blacking out BEFORE the socket closes, and saying honestly whether
        # it got through, because "stopped, outputs blacked out" used to be
        # printed even when the socket was down and nothing was sent.
        self.blackout_sent = False
        # Unmute before the blackout. Left armed, the six Advatek addresses
        # would be skipped by the very frame whose job is to make sure nothing
        # is left lit.
        trig = getattr(self, "trigger", None)
        if trig is not None:
            try:
                if self.player is not None:
                    self.player.trigger_armed = False
                if self.sender is not None and hasattr(self.sender, "set_muted"):
                    self.sender.set_muted(())
                trig.stop()
            except Exception:
                pass
        # The timecode stops before the blackout, so no receiver is still
        # chasing a clock while the pixels go dark underneath it.
        clk = getattr(self, "clock", None)
        if clk is not None:
            try:
                clk.stop()
            except Exception:
                pass
        if self.sender is not None and not self.no_output:
            try:
                before = getattr(self.sender, "packets_sent", 0)
                for _ in range(3):          # UDP: say it more than once
                    self.sender.blackout()
                self.blackout_sent = (
                    getattr(self.sender, "packets_sent", 0) > before)
            except Exception:
                self.blackout_sent = False
        self._running = False
        rig = getattr(self, "rig", None)
        if rig is not None:
            rig.stop()
        if self.audio is not None:
            self.audio.stop()
        if self.player is not None:
            self.player.stop()
        if self.sender is not None:
            self.sender.close()
        if self._lock is not None:
            self._lock.release()
            self._lock = None
        if self.log:
            self.log.event(
                "shutdown",
                f"audio_reopens={getattr(self.audio, 'reopens', 0)} "
                f"frames={self.player.frames_sent} "
                f"ltc={self.player.ltc_frames_in} jumps={self.player.jumps} "
                f"decode_err={self.decode_errors} "
                f"send_err={getattr(self.sender, 'send_errors', 0)} "
                f"reopens={getattr(self.sender, 'reopens', 0)} "
                f"loop_err={self.player.loop_errors} "
                f"restarts={self.player.thread_restarts}")


def _import_sounddevice():
    try:
        import sounddevice
        return sounddevice
    except Exception as e:
        raise SessionError(
            "sounddevice is not installed or could not load PortAudio.\n"
            f"  {e}\n"
            "Run the installer again, or: pip install sounddevice numpy")
