"""Scene triggers for Advatek PixLite controllers (ALTERNATE playback mode).

The primary way this player runs a show is the only honest one: read the FSEQ
and stream every frame to every controller. This module is the backup for the
night that stops working.

Six Advatek PixLite A4-S Mk3 boxes can hold the show on their own SD cards as
recorded SHOWTime scenes and play them back with no help from this Mac. A
scene is started by one Art-Net channel going to 255 on a control-only
universe. So the fallback is: stop sending pixels to those six boxes, and at
the top of each cue send the one channel that starts that cue's recorded
scene. The boxes then play their own slice of the show off their own clocks.

Three things about that are worth stating plainly, because each one has
already been a failure somewhere.

  The other sixteen controllers are not Advateks and have no such playback.
  Stopping output altogether would black them out for the whole show. So this
  mode mutes ONLY the Advatek addresses and keeps streaming everything else
  off the same FSEQ. See Sender.set_muted.

  The Mk3 plays a recorded scene only while no live pixel data is arriving on
  its show universes. If the mute is not in force, the trigger is accepted and
  ignored, and the box keeps showing the live feed. That looks like a working
  show and is not the one that was asked for. Muting is therefore part of
  arming this mode, not a separate switch.

  A recorded scene is a photograph of a render. Re-render a sequence and every
  scene on every SD card is stale, while the sixteen live controllers play the
  new one, in sync, looking almost right. Nothing in software can detect this.
  It is written on the panel and in the operator notes instead.

Wire format is Art-Net OpDmx, the same packet the sender already builds, so
the byte layout has one definition in this program and not two.
"""
import queue
import socket
import threading
import time

from .output import (_artnet_header, _e131_header, ARTNET_PORT, E131_PORT,
                     E131_HEADER_LEN)

# Advatek's own UI notes that "Art-Net sources start at 0" while sACN starts
# at 1, so the number shown in a box's trigger config is not guaranteed to be
# the number on the wire. Nothing here guesses: the universe is whatever the
# show file says, and the operator confirms it against one live box by firing
# channel 1 and watching. Section 5 of the SHOWTime hand-off, 2026-09-15.
DEFAULT_UNIVERSE = 6999
DEFAULT_DEST = "10.0.0.255"
# Which wire the trigger goes out on. The Advateks can be configured to
# listen for either, and WHICH one is not a detail: a trigger sent on the
# wrong protocol is not refused, it simply never arrives, and the boxes sit
# dark while the page says the cue fired.
PROTOCOLS = {"artnet": "artnet", "sacn": "sacn", "e131": "sacn"}
DEFAULT_PROTOCOL = "artnet"
# Write "multicast" in the show file rather than an address. E1.31 fixes the
# group for a universe at 239.255.<high byte>.<low byte>, so typing it out by
# hand is a chance to get it wrong for no benefit; and if the universe is
# ever changed, a typed address quietly keeps pointing at the old one.
MULTICAST = "multicast"


def multicast_for(universe):
    """The E1.31 group for a universe. ANSI E1.31-2018, section 9.3.1."""
    if not 1 <= universe <= 63999:
        raise TriggerError(f"universe {universe} has no multicast group; "
                           f"E1.31 universes run 1 to 63999")
    return f"239.255.{(universe >> 8) & 0xFF}.{universe & 0xFF}"
DEFAULT_PULSE_FRAMES = 3
DEFAULT_PULSE_GAP_MS = 50.0
CHANNELS_PER_UNIVERSE = 512


class TriggerError(Exception):
    pass


class TriggerConfig:
    """The trigger block of a show file, validated.

    Kept separate from the sender so a show file can be checked without
    opening a socket, which is what the self-test and the preflight do."""

    KEYS = frozenset((
        "universe", "dest", "mute", "channels", "idle_channel",
        "pulse_frames", "pulse_gap_ms", "release", "enabled", "protocol",
    ))

    def __init__(self, universe=DEFAULT_UNIVERSE, dest=DEFAULT_DEST,
                 mute=(), channels=None, idle_channel=None,
                 pulse_frames=DEFAULT_PULSE_FRAMES,
                 pulse_gap_ms=DEFAULT_PULSE_GAP_MS, release=True,
                 enabled=False, protocol=DEFAULT_PROTOCOL):
        self.universe = universe
        self.protocol = PROTOCOLS.get(str(protocol).lower(), protocol)
        self.dest = [dest] if isinstance(dest, str) else list(dest)
        self.dest_note = ""
        self.mute = tuple(mute)
        self.channels = dict(channels or {})
        self.idle_channel = idle_channel
        self.pulse_frames = pulse_frames
        self.pulse_gap_ms = pulse_gap_ms
        self.release = bool(release)
        # Whether the show file wants this mode armed at startup. Off unless
        # someone says otherwise: a backup that arms itself is not a backup.
        self.enabled = bool(enabled)

    @classmethod
    def parse(cls, doc, where="timeline"):
        if doc is None:
            return None
        if not isinstance(doc, dict):
            raise TriggerError(f"{where}: 'trigger' must be an object")
        unknown = sorted(k for k in doc if k not in cls.KEYS)
        if unknown:
            raise TriggerError(
                f"{where}: 'trigger' has no setting "
                f"{', '.join(repr(k) for k in unknown)}; it takes: "
                f"{', '.join(sorted(cls.KEYS))}.")

        universe = doc.get("universe", DEFAULT_UNIVERSE)
        if not isinstance(universe, int) or isinstance(universe, bool) \
                or not 0 <= universe <= 32767:
            raise TriggerError(f"{where}: 'trigger.universe' is an Art-Net "
                               f"universe, 0 to 32767")

        proto = doc.get("protocol", DEFAULT_PROTOCOL)
        if not isinstance(proto, str) or \
                str(proto).lower() not in PROTOCOLS:
            raise TriggerError(
                f"{where}: 'trigger.protocol' is {proto!r}; it must be one of "
                f"{', '.join(sorted(set(PROTOCOLS)))}. This is not a detail: "
                f"a trigger sent on the wrong one is not refused, it simply "
                f"never arrives.")
        proto = PROTOCOLS[str(proto).lower()]

        dest = doc.get("dest", DEFAULT_DEST)
        if isinstance(dest, str):
            dest = [dest]
        if not isinstance(dest, (list, tuple)) or not dest or \
                not all(isinstance(x, str) and x.strip() for x in dest):
            raise TriggerError(f"{where}: 'trigger.dest' is the address the "
                               f"trigger is sent to, or a list of them. One "
                               f"address per controller is the predictable "
                               f"choice; a broadcast or multicast address "
                               f"also works.")
        dest = [x.strip() for x in dest]
        note = ""
        if any(x.lower() == MULTICAST for x in dest):
            if len(dest) != 1:
                raise TriggerError(
                    f"{where}: 'trigger.dest' mixes {MULTICAST!r} with "
                    f"addresses. Multicast reaches every box that has "
                    f"subscribed to the universe, so listing more is either "
                    f"redundant or a second copy of every trigger.")
            if proto != "sacn":
                raise TriggerError(
                    f"{where}: 'trigger.dest' is {MULTICAST!r} but the "
                    f"protocol is Art-Net, which has no multicast group for "
                    f"a universe. Use the directed broadcast for the show "
                    f"LAN, or list the controllers.")
            dest = [multicast_for(universe)]
            note = f"multicast, group {dest[0]}"

        mute = doc.get("mute", ())
        if isinstance(mute, str):
            mute = [mute]
        if not isinstance(mute, (list, tuple)) or \
                not all(isinstance(x, str) and x.strip() for x in mute):
            raise TriggerError(f"{where}: 'trigger.mute' is the list of "
                               f"controller addresses to stop sending pixels "
                               f"to while this mode is armed")
        mute = tuple(sorted({x.strip() for x in mute}))
        if not mute:
            raise TriggerError(
                f"{where}: 'trigger.mute' is empty. With nothing muted the "
                f"Advateks keep seeing live pixel data and ignore every "
                f"trigger, which looks like a working show and is not one. "
                f"List the six controller addresses.")

        chans = doc.get("channels")
        if not isinstance(chans, dict) or not chans:
            raise TriggerError(f"{where}: 'trigger.channels' maps each cue's "
                               f"sequence file to the channel that starts its "
                               f"recorded scene")
        channels = {}
        seen = {}
        # Two sequences MAY share a channel, and sometimes should: an opener
        # that plays at the top of both sets is one recorded scene, not two
        # copies of the same thing eating two slots on six SD cards. What
        # cannot be allowed is two DIFFERENT sequences pointed at one scene,
        # because then the wrong one plays and nothing here could tell. That
        # is checked against the actual renders in check_against, which is
        # the only place that can see them.
        for k, v in chans.items():
            if not isinstance(v, int) or isinstance(v, bool) \
                    or not 1 <= v <= CHANNELS_PER_UNIVERSE:
                raise TriggerError(f"{where}: 'trigger.channels[{k}]' is "
                                   f"{v!r}; it must be a channel, 1 to "
                                   f"{CHANNELS_PER_UNIVERSE}")
            seen.setdefault(v, []).append(k)
            channels[k] = v

        idle = doc.get("idle_channel")
        if idle is not None:
            if not isinstance(idle, int) or isinstance(idle, bool) \
                    or not 1 <= idle <= CHANNELS_PER_UNIVERSE:
                raise TriggerError(f"{where}: 'trigger.idle_channel' is a "
                                   f"channel, 1 to {CHANNELS_PER_UNIVERSE}")
            if idle in seen:
                # The preshow look is not a song. Sharing here is always a
                # mistake and there is no render comparison that would make
                # it right.
                raise TriggerError(
                    f"{where}: 'trigger.idle_channel' is {idle}, which is "
                    f"already the channel for "
                    f"{', '.join(repr(x) for x in seen[idle])}")

        pf = doc.get("pulse_frames", DEFAULT_PULSE_FRAMES)
        if not isinstance(pf, int) or isinstance(pf, bool) or not 1 <= pf <= 60:
            raise TriggerError(f"{where}: 'trigger.pulse_frames' is how many "
                               f"copies of the fire packet go out, 1 to 60")
        gap = doc.get("pulse_gap_ms", DEFAULT_PULSE_GAP_MS)
        if not isinstance(gap, (int, float)) or isinstance(gap, bool) \
                or not 0 <= gap <= 1000:
            raise TriggerError(f"{where}: 'trigger.pulse_gap_ms' is the wait "
                               f"between those copies, 0 to 1000")
        rel = doc.get("release", True)
        if not isinstance(rel, bool):
            raise TriggerError(f"{where}: 'trigger.release' is true or false")
        en = doc.get("enabled", False)
        if not isinstance(en, bool):
            raise TriggerError(f"{where}: 'trigger.enabled' is true or false")

        cfg = cls(universe=universe, dest=dest, mute=mute, channels=channels,
                  idle_channel=idle, pulse_frames=pf, pulse_gap_ms=float(gap),
                  release=rel, enabled=en, protocol=proto)
        cfg.dest_note = note
        return cfg

    def channel_for(self, key):
        return self.channels.get(key)

    def check_against(self, timeline, netmap):
        """Everything that can be known before a single packet goes out.

        Returns a list of plain-English problems. An empty list means this
        show file could run in trigger mode tonight. This runs at load, not
        at arm time, so a mapping mistake is found in daylight."""
        import os
        bad = []

        named = set()
        for cue in timeline.cues:
            key = os.path.basename(cue.path)
            named.add(key)
            if self.channel_for(key) is None:
                bad.append(f"{cue.name} has no trigger channel, so it would "
                           f"play on the sixteen live controllers and nothing "
                           f"at all on the Advateks")
        extra = sorted(set(self.channels) - named)
        for k in extra:
            bad.append(f"trigger channel {self.channels[k]} is mapped to "
                       f"{k!r}, which is not a cue in this show")

        # Cues that share a scene have to BE the same thing on the rig.
        # Compared against the renders themselves, because a show file
        # cannot know and an operator should not have to remember.
        by_channel = {}
        for cue in timeline.cues:
            ch = self.channel_for(os.path.basename(cue.path))
            if ch is not None:
                by_channel.setdefault(ch, []).append(cue)
        for ch, cues in sorted(by_channel.items()):
            if len(cues) < 2:
                continue
            def label(c):
                # Two cues in this show are both called "GPL Opener". Naming
                # them twice in one sentence tells nobody anything, so the
                # file name goes in wherever the names are not distinct.
                same = sum(1 for k in cues if k.name == c.name)
                return (f"{c.name} ({os.path.basename(c.path)})" if same > 1
                        else c.name)
            first = cues[0]
            for other in cues[1:]:
                why = _same_render(first.path, other.path)
                if why:
                    bad.append(
                        f"{label(first)} and {label(other)} both fire scene "
                        f"{ch}, but they are not the same render ({why}). One "
                        f"of them would play the wrong thing and nothing here "
                        f"could tell which.")

        if timeline.idle_fseq and self.idle_channel is None:
            bad.append("there is a preshow sequence but no 'idle_channel', so "
                       "the Advateks would sit dark through preshow and every "
                       "gap while the rest of the rig plays it")

        known = {u.ip for u in netmap.universes}
        for ip in self.mute:
            if ip not in known:
                bad.append(f"'mute' lists {ip}, which is not a controller in "
                           f"xlights_networks.xml; nothing would be muted for "
                           f"it and a real Advatek may still be receiving")
        live = sorted(known - set(self.mute))
        if not live:
            bad.append("every controller is muted, so this mode would send no "
                       "pixel data at all and anything without SHOWTime "
                       "playback would be dark for the whole show")

        for u in netmap.universes:
            if u.universe == self.universe:
                bad.append(f"universe {self.universe} already carries pixels "
                           f"for {u.controller}; the trigger universe has to "
                           f"be one nothing is patched to")
                break
        return bad

    @property
    def port(self):
        return E131_PORT if self.protocol == "sacn" else ARTNET_PORT

    @property
    def protocol_label(self):
        return "sACN" if self.protocol == "sacn" else "Art-Net"

    def summary(self, netmap=None):
        n = len(self.channels) + (1 if self.idle_channel else 0)
        where = (self.dest_note or (self.dest[0] if len(self.dest) == 1
                 else f"{len(self.dest)} addresses"))
        s = (f"{n} scenes on {self.protocol_label} universe {self.universe} "
             f"to {where}, muting {len(self.mute)} controller(s)")
        if netmap is not None:
            live = len({u.ip for u in netmap.universes} - set(self.mute))
            s += f", still streaming to {live}"
        return s


def _same_render(path_a, path_b, samples=40):
    """"" if these two renders would look the same on the rig, else why not.

    Sampled rather than exhaustive: a full comparison of two 100MB sequences
    at every startup is not worth the seconds, and any real difference shows
    up in forty frames spread across the song plus both ends. The ends are
    always checked, because a difference in the tail is exactly the one that
    matters when a scene hands over to the next cue."""
    import os
    if os.path.abspath(path_a) == os.path.abspath(path_b):
        return ""
    try:
        from .fseq import FSEQ
        a, b = FSEQ(path_a), FSEQ(path_b)
    except Exception as e:
        return f"one of them could not be read: {e}"
    try:
        if a.duration_ms != b.duration_ms:
            return (f"{a.duration_ms / 1000.0:.2f}s against "
                    f"{b.duration_ms / 1000.0:.2f}s")
        n = a.duration_ms // max(1, a.step_time_ms) if hasattr(a, "step_time_ms") \
            else None
        if n is None:
            return ""
        n = int(n)
        if n <= 0:
            return ""
        idx = sorted({0, n - 1} |
                     {int(i * (n - 1) / max(1, samples - 1))
                      for i in range(samples)})
        for i in idx:
            try:
                if a.frame(i) != b.frame(i):
                    return f"they differ at {i * a.step_time_ms / 1000.0:.2f}s"
            except Exception:
                return "one of them could not be read all the way through"
        return ""
    finally:
        for f in (a, b):
            try:
                f.close()
            except Exception:
                pass


class SceneTrigger:
    """Fires one channel to 255 as a short pulse, then releases it.

    Pulse, not hold: the box latches the scene and plays it from its own SD
    card on its own clock, so there is nothing for this Mac to sustain. That
    also means this Mac cannot nudge, pause or resync a scene once it has
    started. A jump mid-song restarts the scene from ITS zero, which will not
    line up with the audio. The page says so where the operator can see it.

    The socket is separate from the pixel sender's on purpose. This is a
    broadcast socket carrying a handful of packets a night; the pixel sender
    is unicast at 40 frames a second and heals itself by being rebuilt. One
    should never be able to take the other down.
    """

    def __init__(self, config, log=None, clock=time.monotonic,
                 sleep=time.sleep, socket_factory=None):
        self.cfg = config
        self.log = log
        self._clock = clock
        self._sleep = sleep
        self._socket_factory = socket_factory or self._default_socket

        self._sock = None
        self._seq = 0
        # A pulse is three packets 50ms apart plus a release: about a fifth of
        # a second. Sending that from the playback thread would stall the
        # 25ms output loop and drop eight frames on the sixteen controllers
        # that are still being streamed to, at the exact moment a cue starts.
        # So fires are queued and sent here instead. One worker, so two fires
        # can never interleave their packets on the wire.
        self._q = queue.Queue(maxsize=32)
        self._pump = None
        self._stop = threading.Event()
        self.dropped = 0
        self.fired = 0
        self.fire_errors = 0
        self.last_fired = ""
        self.last_fired_at = None
        self.last_error = ""
        self.last_error_at = None
        self.history = []

    # -- socket -----------------------------------------------------------
    def _default_socket(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Asked for only when a destination needs it, the same rule the pixel
        # sender follows. Broadcast left on by default is how an address that
        # slipped into a config gets sent to the whole segment without a word.
        if any(ip.endswith(".255") or ip == "255.255.255.255"
               for ip in self.cfg.dest):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if any(ip.startswith("239.") or ip.startswith("224.")
               for ip in self.cfg.dest):
            # Default TTL is 1, which dies at the first switch. One hop is
            # right for a flat show LAN; anything routed needs more.
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 8)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
        return s

    def _ensure(self):
        if self._sock is None:
            self._sock = self._socket_factory()
        return self._sock

    def close(self):
        s, self._sock = self._sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

    # -- queued firing ----------------------------------------------------
    def start(self):
        if self._pump is not None and self._pump.is_alive():
            return
        self._stop.clear()
        self._pump = threading.Thread(target=self._run, name="trigger",
                                      daemon=True)
        self._pump.start()

    def stop(self):
        self._stop.set()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        t = self._pump
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._pump = None
        self.close()

    def _run(self):
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.25)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                self.fire(*item)
            except Exception as e:      # fire() should never raise; belt too
                self.fire_errors += 1
                self.last_error = f"trigger worker: {e}"
                self.last_error_at = self._clock()

    def fire_async(self, channel, label=""):
        """Queue a fire. Never blocks the playback thread, never raises."""
        try:
            self._q.put_nowait((channel, label))
            return True
        except queue.Full:
            self.dropped += 1
            self.last_error = (f"trigger queue full, dropped ch {channel} "
                               f"{label}".strip())
            self.last_error_at = self._clock()
            self._event("trigger", self.last_error)
            return False

    # -- wire -------------------------------------------------------------
    def _frame(self, channel):
        """One frame on whichever wire this show uses, with `channel` at full.

        channel counts from 1, the way the Advatek trigger config numbers it.
        channel 0 means the release frame: everything at zero."""
        if channel and not 1 <= channel <= CHANNELS_PER_UNIVERSE:
            raise TriggerError(f"channel {channel} is outside 1..512")
        if self.cfg.protocol == "sacn":
            head = _e131_header(self.cfg.universe, CHANNELS_PER_UNIVERSE)
            seq_at, payload_at = 111, E131_HEADER_LEN
        else:
            head = _artnet_header(self.cfg.universe, CHANNELS_PER_UNIVERSE)
            seq_at, payload_at = 12, len(head)
        buf = bytearray(head) + bytearray(CHANNELS_PER_UNIVERSE)
        # sACN receivers drop a packet whose sequence goes backwards, and
        # reserve 0 for "no sequence tracking". Art-Net does not care, and a
        # counter costs nothing.
        self._seq = (self._seq + 1) & 0xFF
        if self.cfg.protocol == "sacn" and self._seq == 0:
            self._seq = 1
        buf[seq_at] = self._seq
        if channel:
            buf[payload_at + channel - 1] = 255
        return bytes(buf)

    def packet(self, channel):
        return self._frame(channel)

    def release_packet(self):
        return self._frame(0)

    def _event(self, kind, msg):
        if self.log:
            try:
                self.log.event(kind, msg)
            except Exception:
                pass

    def fire(self, channel, label=""):
        """Start the scene on `channel`. Returns True if the wire took it.

        Never raises. A trigger that throws would take the playback thread
        down with it, and this is the BACKUP mode: it failing must not cost
        the sixteen controllers that are still being streamed to."""
        now = self._clock()
        try:
            sock = self._ensure()
            port = self.cfg.port
            for i in range(self.cfg.pulse_frames):
                if i:
                    self._sleep(self.cfg.pulse_gap_ms / 1000.0)
                # One frame, sent to every destination, so all six boxes see
                # the same sequence number rather than six separate counters
                # drifting apart.
                pkt = self.packet(channel)
                for ip in self.cfg.dest:
                    sock.sendto(pkt, (ip, port))
            if self.cfg.release:
                self._sleep(self.cfg.pulse_gap_ms / 1000.0)
                rel = self.release_packet()
                for ip in self.cfg.dest:
                    sock.sendto(rel, (ip, port))
        except Exception as e:
            self.fire_errors += 1
            self.last_error = f"trigger ch {channel}: {e}"
            self.last_error_at = now
            self.close()          # next fire builds a fresh socket
            self._event("trigger", self.last_error)
            self._note(f"FAILED ch {channel} {label}".strip(), now)
            return False
        self.fired += 1
        self.last_fired = f"ch {channel} {label}".strip()
        self.last_fired_at = now
        self._event("trigger", f"fired scene {channel}"
                               + (f" ({label})" if label else ""))
        self._note(self.last_fired, now)
        return True

    def _note(self, text, at):
        self.history.append((at, text))
        del self.history[:-20]

    @property
    def seconds_since_error(self):
        if self.last_error_at is None:
            return None
        return self._clock() - self.last_error_at

    @property
    def seconds_since_fire(self):
        if self.last_fired_at is None:
            return None
        return self._clock() - self.last_fired_at
