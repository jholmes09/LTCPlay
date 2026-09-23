"""The chase engine: incoming LTC in, ArtNet/E1.31 frames out.

Clock model.  Every decoded LTC frame gives a timecode value and the moment it
was captured.  From those we keep one number, `epoch`: the monotonic clock time
at which timecode 00:00:00:00 would have occurred.  Current timecode is then
just `now - epoch`, which is free to evaluate and keeps running when the
timecode feed hiccups.

Small differences are slewed so the output does not jitter on decode noise; a
big difference is a deliberate jump (the stage manager going back to bar 40) and
is snapped immediately.  That distinction is the whole reason this is usable in
a rehearsal room.

What is actually on the rig is tracked separately from the chase state, because
LOCKED does not mean lit and LOST does not mean dark.  `source` says which of
show / preshow / hold / black is going out, and the display prints it, since
from the back of the house a rig chasing timecode and a rig frozen on its last
frame look identical.
"""
import os
import threading
import time
import traceback

from .fseq import FSEQ


class ReloadError(Exception):
    """A reload that changed nothing, and the reasons why."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))

LOST, FREEWHEEL, LOCKED, PARKED = "LOST", "FREEWHEEL", "LOCKED", "PARKED"
FREERUN = "FREERUN"
SHOW, IDLE, HOLD, BLACK = "show", "preshow", "hold", "black"


class Player:
    def __init__(self, timeline, netmap, sender,
                 jump_threshold=0.15, slew=0.12,
                 freewheel_ms=250, hold_ms=2000,
                 offset_ms=0.0, on_end="blackout", gaps=None,
                 idle_path=None, log=None, on_lost=None, park_ms=200,
                 bridge_ms=None, auto_reload=False):
        self.timeline = timeline
        self.netmap = netmap
        self.sender = sender
        self.jump_threshold = jump_threshold
        self.slew = slew
        self.freewheel_s = freewheel_ms / 1000.0
        self.hold_s = hold_ms / 1000.0
        self.offset_s = offset_ms / 1000.0
        self.on_end = on_end
        # What fills the space between cues while timecode is still running.
        # Default follows on_end so the old two-mode behaviour is unchanged.
        self.gaps = gaps or ("hold" if on_end == "hold" else "blackout")
        # What the rig does when timecode disappears while a cue is running.
        # This is a policy call, not a technical one, and it is the opposite in
        # the two rooms: in rehearsal a stop means "hold what you have, we are
        # going again from bar 40", and on a show night it means the feed died
        # and the rig belongs on a safe look. So it is a switch, not a guess.
        self.on_lost = on_lost or "preshow"
        # A generator that is parked keeps transmitting the same frame number.
        # Two of them in a row is noise; a fifth of a second of them is a pause.
        self.park_s = park_ms / 1000.0
        # A cue grid taken from a marker list does not line up to the frame.
        # A render is 295.43s long and the next marker sits one frame later,
        # so there is a 33ms hole between two songs that are meant to touch.
        # Filling it from the gap policy puts one frame of the preshow look,
        # or one frame of black, between them, and on a rig that reads as a
        # glitch in the show rather than as arithmetic. Anything shorter than
        # this holds the last frame of the cue that just ended instead.
        if bridge_ms is None:
            bridge_ms = getattr(timeline, "bridge_ms", None)
        self.bridge_s = (250.0 if bridge_ms is None else float(bridge_ms)) / 1000.0
        self.log = log
        # Set by the runner when there is a live input. The display reads its
        # health; nothing in the engine depends on it existing.
        self.audio = None
        # Set by the session when output is live. The display reads it from
        # here so the terminal screen and the page cannot disagree.
        self.rig = None
        # Advatek SHOWTime scene triggers: the ALTERNATE way to run this show.
        # Direct FSEQ playback stays the primary path and is what happens with
        # these left alone. When armed, the six Advatek addresses are muted on
        # the sender and each cue instead fires the one Art-Net channel that
        # starts that cue's recorded scene; everything else keeps streaming.
        self.trigger = None            # a trigger.SceneTrigger, or None
        self.trigger_armed = False
        self._fired_key = None

        self._lock = threading.Lock()
        self._epoch = None            # monotonic time of timecode zero
        self._last_lock = 0.0         # monotonic time of the last decoded frame
        self._buf = bytearray(netmap.total_channels)
        self._running = False
        self._thread = None
        self._super = None
        self._idle_epoch = time.monotonic()
        self.step_ms = 25

        # the preshow / no-timecode look
        self.idle_path = idle_path or (timeline.idle_fseq if timeline else None)
        self.idle_cue = None
        # An operator override that beats the timecode. "preshow" puts the
        # house look up and keeps it up whatever the feed is doing; "blackout"
        # kills the rig; None hands control back to the clock. This is the
        # button you want at 6pm when the generator is running timecode from
        # yesterday's rehearsal and you just need the preshow on the trees.
        self.override = None
        # Free run: play the show off this machine's own clock, ignoring the
        # timecode feed entirely. Every show controller has a GO and this did
        # not: if the LTC line dies for good ten minutes before the set -- a
        # failed DA, an unpatched output, a Dante subscription that did not
        # come back -- the alternative was a preshow loop for thirty minutes
        # in front of an audience. Added after the 2026-09-13 design audit.
        self.freerun_epoch = None
        # The FEED's own state while a free run is on; the show's
        # state is FREERUN. Kept apart so the display can tell the
        # truth about the feed without logging a flip per frame.
        self.feed_state = None
        self._last_noted_state = None

        # stats, read by the display
        self.state = LOST
        self.source = BLACK
        self.tc_seconds = -1.0        # the free-rolling playback clock
        self.current_cue = None
        self.next_cue = None
        self.current_frame = -1
        self.frames_sent = 0
        self.jumps = 0
        self.out_of_range_channels = 0
        self.last_error = ""
        self.loop_errors = 0
        self.last_loop_error = ""
        self.thread_restarts = 0
        self.render_errors = 0
        self._render_bad_since = None

        # (mtime, size) each render had when it was opened, and the readers
        # a reload has replaced but not yet closed.
        self._opened_at = {}
        self._retired = []
        # Watch the renders and swap them in as xLights finishes writing
        # them. Off unless asked for: on a show night a file changing under
        # the player is a fault, not a feature.
        self.auto_reload = bool(auto_reload)
        self._last_reload_check = 0.0
        self._settling = {}

        # the last timecode actually received, frozen on loss.  This is the
        # number an operator checks sync against, so it must stop moving the
        # moment the feed does, even while the show keeps free-rolling.
        # A jump has to be confirmed by a second frame before the clock
        # moves. See feed_timecode.
        self._pending_jump = None
        self.jump_confirm_s = 0.100
        self.jump_rejects = 0
        self.last_rejected_tc = ""

        self._last_tc_value = None
        self._park_since = None
        self.parked_since = None
        self.last_ltc_seconds = None
        self.last_ltc_text = None
        self.last_ltc_at = None
        self.last_ltc_drop = False
        self.sync_delta_ms = 0.0
        self.ltc_frames_in = 0

    # -- cue loading ------------------------------------------------------
    def _prepare(self, cue):
        """Open one cue's FSEQ, PROVE it reads, and precompute its spans."""
        f = FSEQ(cue.path)
        try:
            # Opening only reads the header and block table, which xLights
            # writes before the channel data. Without this the file looks
            # fine and fails on the first frame, on the rig, in front of
            # people.
            f.verify()
        except Exception:
            try: f.close()
            except Exception: pass
            raise
        cue.fseq = f
        cue.duration = f.duration_ms / 1000.0
        spans = []
        if f.sparse_ranges:
            src = 0
            for start0, length in f.sparse_ranges:
                spans.append((start0, src, length))
                src += length
        else:
            spans.append((0, 0, f.channel_count))
        cue._spans = spans
        cue._gaps = None
        return sum(1 for d, _, ln in spans if d + ln > len(self._buf))

    @staticmethod
    def _stamp(path):
        """(mtime, size) of a render, or None if it is not there."""
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (st.st_mtime, st.st_size)

    def stale_cues(self):
        """Which renders have changed on disk since they were opened.

        xLights writes over the file in place, so a re-render changes the
        bytes under a reader that is already holding it. Noticing is what
        lets the page offer a reload instead of the operator wondering why
        the change they just made is not on the rig."""
        out = []
        for cue in list(self.timeline.cues) + \
                ([self.idle_cue] if self.idle_cue is not None else []):
            was = self._opened_at.get(id(cue))
            now = self._stamp(cue.path)
            if was is not None and now is not None and now != was:
                out.append(cue)
        return out

    def reload(self, only=None):
        """Re-open the renders without stopping the show.

        Rehearsal is a loop: change the sequence, render, watch it again. The
        alternative is stopping and restarting the chase, which drops the rig
        to black and loses the lock.

        All or nothing. A render that is still being written will not open,
        and half-swapping a show would be worse than not swapping it: on any
        failure nothing changes and the old files keep playing.

        `only` is a set of basenames; None means every cue.
        """
        from .timeline import Cue
        targets = list(self.timeline.cues)
        want_idle = self.idle_cue is not None and (
            only is None or os.path.basename(self.idle_cue.path) in only)
        fresh, errors, over = [], [], []
        for cue in targets:
            if only is not None and os.path.basename(cue.path) not in only:
                fresh.append(cue)
                continue
            spare = Cue(cue.tc_text, cue.path, cue.name)
            spare.tc_seconds = cue.tc_seconds
            try:
                n = self._prepare(spare)
            except Exception as e:
                errors.append(f"{cue.name}: {e}")
                continue
            if n:
                over.append(cue.name)
            fresh.append(spare)
        new_idle = None
        if want_idle:
            spare = Cue("00:00:00:00", self.idle_cue.path, "preshow loop")
            try:
                self._prepare(spare)
                new_idle = spare
            except Exception as e:
                errors.append(f"preshow loop: {e}")

        if errors:
            # Nothing has been swapped in, so close whatever did open and
            # leave the running show exactly as it was.
            for c in fresh:
                if c not in targets and c.fseq is not None:
                    try: c.fseq.close()
                    except Exception: pass
            if new_idle is not None and new_idle.fseq is not None:
                try: new_idle.fseq.close()
                except Exception: pass
            raise ReloadError(errors)

        # Swap the LIST, not the contents of each cue. A tick that is already
        # holding a cue object keeps a consistent one; the next tick picks up
        # the new list. Nothing ever sees a cue whose reader and spans
        # disagree.
        changed = [c for c in fresh if c not in targets]
        old = [c for c in targets if c not in fresh]
        fresh.sort(key=lambda c: c.tc_seconds)
        self.timeline.cues = fresh
        if new_idle is not None:
            old_idle, self.idle_cue = self.idle_cue, new_idle
            self._idle_epoch = time.monotonic()
            old.append(old_idle)
        for c in fresh:
            self._opened_at[id(c)] = self._stamp(c.path)

        # Readers are retired, not closed: a tick may still be inside one.
        # The batch from the previous reload is closed now, by which time
        # every tick that could have held it is long finished.
        for c in self._retired:
            try: c.fseq.close()
            except Exception: pass
        self._retired = [c for c in old if c.fseq is not None]
        self._event("reload", f"reloaded {len(changed)} of {len(fresh)} "
                              f"sequences without stopping")
        return {"reloaded": [c.name for c in changed],
                "over_range": over,
                "total": len(fresh)}

    def open_cues(self):
        """Open every FSEQ up front so a missing file fails now, not mid-show."""
        problems = []
        targets = list(self.timeline.cues)
        if self.idle_path:
            from .timeline import Cue
            self.idle_cue = Cue("00:00:00:00", self.idle_path, "preshow loop")
            targets.append(self.idle_cue)
        for cue in targets:
            try:
                over = self._prepare(cue)
            except Exception as e:
                problems.append(f"{cue.name}: {e}")
                if cue is self.idle_cue:
                    self.idle_cue = None
                continue
            self._opened_at[id(cue)] = self._stamp(cue.path)
            if over:
                problems.append(
                    f"{cue.name}: addresses channels past {len(self._buf)}, "
                    f"the end of your current controller map. Those channels "
                    f"will not be output. Re-render after a controller change.")
        return problems

    # -- clock ------------------------------------------------------------
    def feed_timecode(self, tc_seconds, captured_at, drop=False, text=None):
        """Called from the audio thread for each decoded LTC frame."""
        new_epoch = captured_at - tc_seconds + self.offset_s
        with self._lock:
            now = time.monotonic()
            self._last_lock = now
            self.ltc_frames_in += 1
            self.last_ltc_seconds = tc_seconds
            if text:
                self.last_ltc_text = text
            self.last_ltc_at = now
            self.last_ltc_drop = drop
            # Is the source parked? A paused deck or DAW either stops sending
            # entirely (indistinguishable from a pulled cable, handled by the
            # freewheel timer) or keeps sending one frame over and over. This
            # is the second case, and it is worth telling apart: the signal is
            # healthy, the show is simply standing still.
            same = (self._last_tc_value is not None and
                    abs(tc_seconds - self._last_tc_value) < 1e-6)
            if same:
                if self._park_since is None:
                    self._park_since = now
            else:
                if self._park_since is not None:
                    self._event("resume", f"timecode moving again at "
                                          f"{tc_seconds:.3f}s")
                self._park_since = None
            self._last_tc_value = tc_seconds

            if self._epoch is None:
                self._epoch = new_epoch
                self.sync_delta_ms = 0.0
                self.state = LOCKED
                self._event("lock", f"first timecode at {tc_seconds:.3f}s")
                return
            if self._park_since is not None:
                # Leave the epoch alone. Slewing toward a frame number that is
                # not moving would drag the clock forward at real-time speed
                # and log a jump every few frames; freezing it means the one
                # real jump happens once, when the operator hits play again.
                self.state = PARKED
                return
            delta = new_epoch - self._epoch
            self.sync_delta_ms = delta * 1000.0
            if abs(delta) > self.jump_threshold:
                # LTC has no checksum, and the frame digits are not even
                # range-checked by the standard: one flipped bit in the hours
                # reads as a valid timecode an hour away. Taking that on a
                # single frame put Set 2 content on the rig mid-Set 1, once
                # per second, on a marginal cable, while the screen said
                # LOCKED. So a jump has to be said twice: two consecutive
                # frames that agree with each other, and disagree with where
                # we are. A real jump (the stage manager hitting play at bar
                # 40) satisfies that in 33ms and costs nothing.
                want = self._pending_jump
                # Confirmation exists to protect a LIVE lock from one corrupt
                # frame. When the show is not locked to anything -- the feed
                # has been gone, or the deck was parked -- there is nothing to
                # protect and everything to lose: round 2 measured a stale
                # frame of the old clock reaching the rig at the start of
                # every set, because the first good frame of a restart was
                # refused. Take it at once.
                cold = (self.state in (LOST, PARKED)
                        or self.last_ltc_at is None
                        or now - self.last_ltc_at > self.freewheel_s)
                if cold or (want is not None
                            and abs(new_epoch - want) <= self.jump_confirm_s):
                    self._epoch = new_epoch
                    self._pending_jump = None
                    self.jumps += 1
                    self._event("jump", f"{delta:+.3f}s to {tc_seconds:.3f}s")
                else:
                    # Only the SECOND disagreeing frame in a row is evidence
                    # of a bad feed. The first frame of every honest relocate
                    # lands here too, and counting it made the display accuse
                    # a clean feed after a dozen rehearsal locates.
                    if want is not None:
                        self.jump_rejects += 1
                        self.last_rejected_tc = text or f"{tc_seconds:.3f}s"
                    self._pending_jump = new_epoch
                    # Do NOT move the clock, and do not call this LOCKED-with-
                    # a-jump: the show carries on free-rolling through the bad
                    # frame, which is exactly what it should do.
                    self.state = LOCKED
                    return
            else:
                self._pending_jump = None
                self._epoch += delta * self.slew
            self.state = LOCKED

    def _now_tc(self):
        if self._epoch is None:
            return None
        return time.monotonic() - self._epoch

    def _event(self, kind, msg):
        if self.log:
            try:
                self.log.event(kind, msg)
            except Exception:
                pass

    # -- output -----------------------------------------------------------
    def _render_into_buffer(self, cue, frame_index):
        """Write one frame of a cue into the output buffer.

        Every channel this cue does not address is zeroed first. That sounds
        obvious and was not what this did: xLights renders sparsely, so a
        sequence that uses the butterflies writes 5,193 channels and says
        nothing about the 37 bats. Leaving the rest of the buffer alone meant
        the bats kept whatever the PREVIOUS look put there -- for the whole
        of the next song. The Opener ran with 8,572 channels frozen on a
        preshow frame and the program's own `verify` said those props would
        "stay dark". Found by an adversarial audit, 2026-09-13.

        Cost of doing it right: one 36KB memset per frame, about 2 microseconds
        against a 25ms budget.
        """
        data = cue.fseq.frame(frame_index)
        buf = self._buf
        cap = len(buf)
        spans = cue._spans
        # Zero everything outside this cue's spans. The spans are in ascending
        # destination order for every xLights render, but do not trust that:
        # sort a copy once per cue and keep it on the cue.
        gaps = cue._gaps
        if gaps is None:
            gaps = cue._gaps = self._gaps_for(spans, cap)
        for a, b in gaps:
            if a < cap:
                buf[a:min(b, cap)] = bytes(min(b, cap) - a)
        dropped = 0
        for dst, src, length in spans:
            if dst >= cap:
                dropped += length
                continue
            n = min(length, cap - dst)
            buf[dst:dst + n] = data[src:src + n]
            if n < length:
                dropped += length - n
        return dropped

    @staticmethod
    def _gaps_for(spans, cap):
        """The stretches of the output buffer a cue does NOT address."""
        # One sweep in destination order. A separate merge pass was here and
        # a mutation proved it could be deleted without changing a single
        # answer -- `at` already absorbs overlaps -- so it was dead code
        # dressed as a guard, and it is gone.
        gaps, at = [], 0
        for a, b in sorted((d, d + ln) for d, _, ln in spans if ln > 0):
            if a > at:
                gaps.append((at, min(a, cap)))
            at = max(at, b)
        if at < cap:
            gaps.append((at, cap))
        return [(a, b) for a, b in gaps if b > a]

    def _render(self, cue, idx):
        """Render, or keep the last good frame briefly if the file misbehaves.

        A read that fails once is a hiccup; going black for one frame on a
        hiccup is more visible from the seats than holding.  A read that keeps
        failing is a real problem and the rig should go dark so nobody stands
        there believing the cue is running."""
        try:
            dropped = self._render_into_buffer(cue, idx)
        except Exception as e:
            self.render_errors += 1
            self.last_error = f"{cue.name} frame {idx}: {e}"
            now = time.monotonic()
            if self._render_bad_since is None:
                self._render_bad_since = now
                self._event("render", self.last_error)
            if now - self._render_bad_since < 1.0:
                self.source = HOLD
                return self._buf
            return None
        self._render_bad_since = None
        # Per cue, not cumulative and not sticky. It used to be set once and
        # never cleared, so one sequence that overran the controller map put a
        # red line on the screen for the rest of the night, against cues that
        # fit perfectly. A warning that is always on is not a warning.
        self.out_of_range_channels = dropped
        # A successful read ends a hold. HOLD used to be sticky: one read
        # hiccup and the display said "holding last frame" for the rest of
        # the set while the show was in fact playing, and that line is the
        # one the operator is told to trust.
        if self.source == HOLD:
            self.source = SHOW
        return self._buf

    def _idle_frame(self):
        """The preshow look, free-running and looped off the wall clock."""
        cue = self.idle_cue
        if cue is None or cue.fseq is None:
            self.source = BLACK
            return b""
        n = cue.fseq.frame_count
        if n <= 0:
            self.source = BLACK
            return b""
        el = time.monotonic() - self._idle_epoch
        idx = int(el * 1000.0 // cue.fseq.step_time_ms) % n
        out = self._render(cue, idx)
        if out is None:
            self.source = BLACK
            return b""
        self.source = IDLE
        self.current_frame = idx
        return out

    def _gap_frame(self):
        """What goes out between cues, or after the last one."""
        if self.gaps == "idle" and self.idle_cue is not None:
            return self._idle_frame()
        if self.gaps == "hold":
            self.source = HOLD
            return self._buf
        self.source = BLACK
        return b""

    def _tick(self):
        now = time.monotonic()
        with self._lock:
            epoch = self._epoch
            last = self._last_lock

        prev_state, prev_cue, prev_source = self.state, self.current_cue, self.source

        # The override is read before anything else, so it works whether or
        # not timecode is arriving. The state and the LTC readout carry on
        # underneath it: the operator still sees the feed is healthy.
        # Free run beats everything, including the feed. The operator has
        # said "run it from here", and a generator that comes back to life
        # mid-song must not yank the show sideways.
        override = self.override
        if self.freerun_epoch is not None and override not in ("preshow",
                                                               "blackout"):
            self._state_from_feed(now, last, epoch)
            # The FEED's state is kept separately; the SHOW's state is
            # FREERUN. The display needs both: reporting the show's state
            # against the LTC readout said "frozen" beside a number that was
            # visibly counting.
            self.feed_state = self.state
            self.state = FREERUN
            self.tc_seconds = now - self.freerun_epoch
            return self._play_at(self.tc_seconds, prev_state, prev_cue,
                                 prev_source)

        # Blackout and Preshow are the panic buttons and they beat everything,
        # free run included. Round 1 put free run first and the one night GO
        # gets used is the night something else is already wrong; an operator
        # hitting Blackout and watching the rig carry on is the worst thing
        # this program could do. Found by round 2 of the audit, 2026-09-13.
        if override in ("preshow", "blackout"):
            self._state_from_feed(now, last, epoch)
            self.current_cue = None
            self.current_frame = -1
            self.next_cue = (self.timeline.next_cue(self.tc_seconds)
                             if self.tc_seconds is not None
                             and self.tc_seconds >= 0
                             else self._next_from_memory())
            if override == "blackout":
                self.source = BLACK
                self._note_change(prev_state, prev_cue, prev_source)
                return b""
            out = self._idle_frame()
            self._note_change(prev_state, prev_cue, prev_source)
            return out

        if epoch is None:
            self.state = LOST
            self.tc_seconds = -1.0
            self.current_cue = None
            self.next_cue = self._next_from_memory()
            self.current_frame = -1
            out = self._idle_frame()
            self._note_change(prev_state, prev_cue, prev_source)
            return out

        with self._lock:
            park_since = self._park_since
        parked = (park_since is not None and now - park_since >= self.park_s)

        since = now - last
        if since > self.hold_s:
            self.state = LOST
        elif parked:
            self.state = PARKED
        elif since > self.freewheel_s:
            self.state = FREEWHEEL
        else:
            self.state = LOCKED
        self.parked_since = park_since if self.state == PARKED else None

        if self.state == LOST:
            # Timecode is gone for good as far as the rig is concerned.  Back
            # to the preshow look rather than to black: a dark rig between runs
            # reads as a failure to everyone in the room.
            #
            # ...unless the room asked for the other answer. On a show night
            # the music keeps playing whatever the timecode line does, so the
            # lights should keep going too: pick up the Mac's own clock at the
            # frame the feed died on and run the set out. Jeff, 2026-09-14.
            if self.on_lost == "freerun" and self.last_ltc_seconds is not None \
                    and self.freerun_epoch is None:
                # Start from where the feed actually was, not from now: the
                # hold window has already elapsed, and the music did not wait.
                self.freerun_epoch = last - self.last_ltc_seconds
                self._event("freerun",
                            f"timecode lost; running the rest of the set on "
                            f"this Mac's clock from "
                            f"{self.timeline.format(self.last_ltc_seconds)}")
            if self.on_lost == "hold" and prev_cue is not None:
                # Keep the cue and the position exactly where they were, so a
                # rehearsal stop looks like a pause rather than a failure.
                self.source = HOLD
                self._note_change(prev_state, prev_cue, prev_source)
                return self._buf
            self.tc_seconds = -1.0
            self.current_cue = None
            self.current_frame = -1
            self.next_cue = self._next_from_memory()
            if self.on_lost == "blackout":
                self.source = BLACK
                self._note_change(prev_state, prev_cue, prev_source)
                return b""
            out = self._idle_frame()
            self._note_change(prev_state, prev_cue, prev_source)
            return out

        # While parked, the show sits on the frame the source is repeating
        # rather than on the free-rolling clock, which is the whole difference
        # between a pause and a dropout.
        tc = self.last_ltc_seconds if self.state == PARKED and \
            self.last_ltc_seconds is not None else now - epoch
        self.tc_seconds = tc
        return self._play_at(tc, prev_state, prev_cue, prev_source)

    def _play_at(self, tc, prev_state, prev_cue, prev_source):
        """Put the show at this point on the clock, wherever the clock came
        from: the timecode feed, or a free run the operator started."""
        cue = self.timeline.cue_at(tc)
        self.next_cue = self.timeline.next_cue(tc)

        if cue is None or cue.fseq is None:
            self.current_cue = None
            self.current_frame = -1
            out = self._gap_frame()
            self._note_change(prev_state, prev_cue, prev_source)
            return out

        offset = tc - cue.tc_seconds
        idx = int(offset * 1000.0 // cue.fseq.step_time_ms)
        if idx >= cue.fseq.frame_count:
            # This cue has run out. It is no longer the cue that is playing,
            # unless the next one is about to start: see bridge_s above.
            nxt = self.next_cue
            ended_at = cue.tc_seconds + (cue.fseq.frame_count *
                                         cue.fseq.step_time_ms / 1000.0)
            if nxt is not None and self.bridge_s > 0 and \
                    0 < nxt.tc_seconds - tc <= self.bridge_s and \
                    tc - ended_at <= self.bridge_s:
                self.current_cue = cue
                self.current_frame = cue.fseq.frame_count - 1
                out = self._render(cue, self.current_frame)
                if out is not None:
                    if self.source != HOLD:
                        self.source = SHOW
                    self._note_change(prev_state, prev_cue, prev_source)
                    return out
            self.current_cue = None
            self.current_frame = -1
            if self.on_end == "hold":
                self.current_cue = cue
                self.current_frame = cue.fseq.frame_count - 1
                out = self._render(cue, self.current_frame)
                self.source = SHOW if out is not None else BLACK
                self._note_change(prev_state, prev_cue, prev_source)
                return out if out is not None else b""
            out = self._gap_frame()
            self._note_change(prev_state, prev_cue, prev_source)
            return out

        self.current_cue = cue
        self.current_frame = idx
        out = self._render(cue, idx)
        if out is None:
            self.source = BLACK
            self._note_change(prev_state, prev_cue, prev_source)
            return b""
        if self.source != HOLD:
            self.source = SHOW
        self._note_change(prev_state, prev_cue, prev_source)
        return out

    def _next_from_memory(self):
        """What comes next, judged from the last timecode actually seen.

        With the feed stopped this used to reset to the first cue of the show.
        That is wrong every night: the feed stops for the interval, so from
        the moment Set 1 ended until Set 2 started, UP NEXT read "Set 1
        Opener" and the GO button offered to run Set 1 at the top of Set 2.
        Round 3 of the audit, 2026-09-13.
        """
        cues = self.timeline.cues
        if not cues:
            return None
        seen = self.last_ltc_seconds
        if seen is None:
            return cues[0]
        return self.timeline.next_cue(seen)

    # -- free run ---------------------------------------------------------
    def go(self, tc_seconds):
        """Run the show from this point on our own clock. GO.

        The timecode feed keeps being read and displayed so the operator can
        see it come back, but it no longer drives anything until `release()`.
        """
        if tc_seconds is None or tc_seconds < 0:
            raise ValueError("a free run has to start somewhere on the "
                             "show clock")
        self.freerun_epoch = time.monotonic() - tc_seconds
        self.override = None
        self._event("freerun", f"GO from {tc_seconds:.3f}s on this machine's "
                               f"own clock; the timecode feed is being "
                               f"ignored until it is released")
        return tc_seconds

    def nudge(self, seconds):
        """Move a free run forward or back by this many seconds.

        Once the show is on our own clock, skipping is just moving the epoch:
        there is no source to chase, so there is nothing to fight. Asked for
        by Jeff, 2026-09-13 -- if GO is the manual mode, it needs the same
        locate the timecode operator has.
        """
        if self.freerun_epoch is None:
            raise ValueError("The show is following timecode, so this Mac "
                             "cannot move it. Skipping only applies to a free "
                             "run: press GO first.")
        at = max(0.0, (time.monotonic() - self.freerun_epoch) + float(seconds))
        self.freerun_epoch = time.monotonic() - at
        self._event("freerun", f"skipped {float(seconds):+.1f}s to {at:.3f}s")
        return at

    def go_to_cue(self, step):
        """Jump a free run to the previous or next cue, or restart this one.

        `step` of -1 is "the cue before this one", +1 is "the next cue", 0 is
        "the top of the cue that is playing" -- which is the one a designer
        actually wants most of the time.
        """
        if self.freerun_epoch is None:
            raise ValueError("The show is following timecode, so this Mac "
                             "cannot move it. Skipping only applies to a free "
                             "run: press GO first.")
        cues = self.timeline.cues
        if not cues:
            raise ValueError("This show has no cues to skip between.")
        at = time.monotonic() - self.freerun_epoch
        here = self.timeline._index_at(at)
        if step == 0:
            # The top of the current cue -- unless we are only just into it,
            # in which case "restart" almost certainly means the one before.
            if here < 0:
                i = 0
            elif at - cues[here].tc_seconds < 1.5 and here > 0:
                i = here - 1
            else:
                i = here
        elif step < 0:
            i = max(0, (here if here >= 0 else 0) - 1)
        else:
            i = min(len(cues) - 1, (here + 1) if here >= 0 else 0)
        target = cues[i]
        self.freerun_epoch = time.monotonic() - target.tc_seconds
        self._event("freerun", f"skipped to {target.name} at "
                               f"{target.tc_text}")
        return target

    def release(self):
        """Hand the show back to the timecode."""
        if self.freerun_epoch is None:
            return False
        self.freerun_epoch = None
        live = self.feed_state == LOCKED
        self._event("freerun", "released; the show is following timecode "
                               "again" if live else
                               "released while NO timecode was arriving, so "
                               "the show has stopped where it was")
        return True

    def _state_from_feed(self, now, last, epoch):
        """Work out LOST/FREEWHEEL/PARKED/LOCKED and the playback clock,
        without deciding what to output. Used by the override path, which
        wants the readout to stay honest while it holds a look."""
        if epoch is None:
            self.state = LOST
            self.tc_seconds = -1.0
            return
        with self._lock:
            park_since = self._park_since
        parked = (park_since is not None and now - park_since >= self.park_s)
        since = now - last
        if since > self.hold_s:
            self.state = LOST
        elif parked:
            self.state = PARKED
        elif since > self.freewheel_s:
            self.state = FREEWHEEL
        else:
            self.state = LOCKED
        self.parked_since = park_since if self.state == PARKED else None
        if self.state == LOST:
            self.tc_seconds = -1.0
        else:
            self.tc_seconds = (self.last_ltc_seconds
                               if self.state == PARKED
                               and self.last_ltc_seconds is not None
                               else now - epoch)

    def _note_change(self, prev_state, prev_cue, prev_source):
        # The audio thread sets the state from the feed; the tick may override
        # it (FREERUN). Comparing those two produced a LOCKED -> FREERUN line
        # every frame -- 28 a second -- and buried the log the operator is
        # told to trust. Only a real change of the SHOW's state is an event.
        # Round 3 of the audit, 2026-09-13.
        was = self._last_noted_state or prev_state
        if was != self.state:
            self._event("state", f"{was} -> {self.state}")
        self._last_noted_state = self.state
        a = prev_cue.name if prev_cue else "-"
        b = self.current_cue.name if self.current_cue else "-"
        if a != b:
            self._event("cue", f"{a} -> {b}")
        if prev_source != self.source:
            self._event("output", f"{prev_source} -> {self.source}")

    # -- threads ----------------------------------------------------------
    # -- Advatek scene triggers -------------------------------------------
    IDLE_KEY = "\x00idle"

    def _trigger_key(self):
        """What the Advateks should be playing right now, or None.

        Level-triggered on purpose. An edge-triggered version has to be hooked
        into every branch that can change a cue -- there are twelve -- and the
        one that gets missed is the one that costs a cue on a show night.
        Comparing the answer to what was last fired cannot miss a transition
        and cannot fire twice for the same one."""
        if not self.trigger_armed or self.trigger is None:
            return None
        cue = self.current_cue
        if cue is not None:
            return os.path.basename(cue.path)
        # No cue: preshow, a gap between songs, or before the first cue. The
        # box is told once and loops the scene itself from there.
        if self.source == IDLE:
            return self.IDLE_KEY
        return None

    def _service_trigger(self):
        """Fire the scene for whatever is current, once per entry.

        Never raises: this is the backup mode, and it failing must not cost
        the sixteen controllers that are still being streamed to."""
        try:
            key = self._trigger_key()
            if key == self._fired_key:
                return
            self._fired_key = key
            if key is None:
                return
            cfg = self.trigger.cfg
            if key == self.IDLE_KEY:
                ch, label = cfg.idle_channel, "preshow"
            else:
                ch, label = cfg.channel_for(key), (
                    self.current_cue.name if self.current_cue else key)
            if ch is None:
                # Mapped nowhere. Said once per entry, not once per frame.
                self._event("trigger", f"{label} has no trigger channel; the "
                                       f"Advateks have nothing to play for it")
                return
            self.trigger.fire_async(ch, label)
        except Exception as e:
            self.last_error = f"trigger: {e}"
            self._event("trigger", self.last_error)

    def _loop(self, step_ms):
        period = step_ms / 1000.0
        next_at = time.monotonic()
        while self._running:
            try:
                frame = self._tick()
                self.sender.send_frame(frame if frame is not None else b"")
                self.frames_sent += 1
                # After the frame, never before: a trigger is a consequence of
                # what the show just decided to play.
                self._service_trigger()
            except Exception as e:
                # Nothing raised in here may be allowed to end this thread.  A
                # dead output thread is the worst failure this program has: the
                # display keeps updating, the timecode keeps running, and the
                # rig sits frozen on whatever frame arrived last with no sign
                # anywhere that anything is wrong.
                self.loop_errors += 1
                self.last_loop_error = f"{type(e).__name__}: {e}"
                if self.log:
                    try:
                        self.log.event("loop-error", self.last_loop_error +
                                       "\n" + traceback.format_exc())
                    except Exception:
                        pass
                time.sleep(0.01)
            next_at += period
            sleep = next_at - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
            else:
                # Fell behind: give up the missed slots rather than sprinting to
                # catch up, which would burst packets at the controllers.
                next_at = time.monotonic()

    def _supervise(self):
        """Restart the output thread if it ever stops.

        The guard above should make this unreachable.  It exists because the
        cost of being wrong about that is a show that stops, and the cost of
        carrying it is a thread that wakes twice a second."""
        while self._running:
            time.sleep(0.5)
            t = self._thread
            if self._running and (t is None or not t.is_alive()):
                self.thread_restarts += 1
                self._event("restart", f"output thread died, restart "
                                       f"#{self.thread_restarts}")
                self._spawn()
            if self.auto_reload:
                try:
                    self.poll_reload()
                except Exception as e:
                    # Never let a reload take the supervisor down with it;
                    # the supervisor is the last thing standing between a
                    # dead output thread and a dark rig.
                    self.last_error = f"auto reload: {e}"

    RELOAD_CHECK_S = 2.0
    # Longer than the check interval on purpose: a render must be unchanged
    # across two consecutive looks, not one, before it is swapped in. An
    # 87MB sequence takes xLights a while to write and the cost of being
    # early is the show holding a file whose block table has moved.
    RELOAD_SETTLE_S = 3.0

    def poll_reload(self, now=None):
        """Swap in anything xLights has finished re-rendering.

        Two rules make this safe to leave on during a rehearsal. A render is
        only picked up once its (mtime, size) has stopped changing, so a file
        still being written is left alone; and reload is all or nothing, so a
        file that turns out to be half written anyway changes nothing and is
        simply tried again two seconds later."""
        now = time.monotonic() if now is None else now
        if now - self._last_reload_check < self.RELOAD_CHECK_S:
            return None
        self._last_reload_check = now
        stale = self.stale_cues()
        if not stale:
            self._settling.clear()
            return None
        settled = []
        for cue in stale:
            key = cue.path
            stamp = self._stamp(key)
            seen_stamp, seen_at = self._settling.get(key, (None, None))
            if stamp != seen_stamp:
                self._settling[key] = (stamp, now)
                continue
            if now - seen_at >= self.RELOAD_SETTLE_S:
                settled.append(os.path.basename(key))
        if not settled:
            return None
        try:
            r = self.reload(set(settled))
        except ReloadError:
            # Still being written after all. Re-time it and try again.
            for name in settled:
                self._settling.pop(
                    next((c.path for c in stale
                          if os.path.basename(c.path) == name), ""), None)
            return None
        for name in settled:
            self._settling.pop(
                next((c.path for c in stale
                      if os.path.basename(c.path) == name), ""), None)
        return r

    def _spawn(self):
        self._thread = threading.Thread(target=self._loop, args=(self.step_ms,),
                                        daemon=True, name="ltcplay-output")
        self._thread.start()

    def thread_alive(self):
        return bool(self._thread and self._thread.is_alive())

    def start(self, step_ms=None):
        if step_ms is None:
            steps = {c.fseq.step_time_ms for c in self.timeline.cues if c.fseq}
            if self.idle_cue is not None and self.idle_cue.fseq:
                steps.add(self.idle_cue.fseq.step_time_ms)
            step_ms = min(steps) if steps else 25
        self.step_ms = step_ms
        self._idle_epoch = time.monotonic()
        self._running = True
        self._spawn()
        self._super = threading.Thread(target=self._supervise, daemon=True,
                                       name="ltcplay-supervisor")
        self._super.start()
        return step_ms

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._super:
            self._super.join(timeout=2.0)
        self.sender.blackout()
        self.sender.blackout()
