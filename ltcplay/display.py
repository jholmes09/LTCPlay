"""The run display.

Written for someone standing at a console who has about one second to decide
whether the rig is following the timecode.  That means the two numbers that
answer the question are large and adjacent, everything else is one line, and
anything wrong is stated as a sentence at the bottom rather than implied by a
counter that happens to be non-zero.

The two numbers:

  LTC IN    what the timecode source last said.  It FREEZES the moment the
            feed stops, which is the whole point: a number that keeps counting
            cannot tell you the feed died.
  PLAYING   where the show actually is.  It free-rolls through a dropout and
            then stops, so the gap between the two lines is the dropout, in
            frames, without anybody doing arithmetic.
"""
import collections
import re
import time

from .player import LOCKED, FREEWHEEL, LOST, PARKED, SHOW, IDLE, HOLD, BLACK, \
    _now
from .tc import format_tc, format_clock, rate_label

R = "\033[0m"
B = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
AMBER = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"

STANDBY = "STANDBY"
STATE_COLOUR = {LOCKED: GREEN, FREEWHEEL: AMBER, LOST: RED, PARKED: CYAN,
                "FREERUN": AMBER, STANDBY: DIM}
SOURCE_TEXT = {SHOW: "show", IDLE: "preshow loop", HOLD: "holding last frame",
               BLACK: "blacked out"}
SOURCE_COLOUR = {SHOW: GREEN, IDLE: CYAN, HOLD: AMBER, BLACK: DIM}


_ANSI = re.compile(r"\033\[[0-9;?]*[A-Za-z]")


def vis(text):
    """Printable width, ignoring colour codes."""
    return len(_ANSI.sub("", text))


def pad(text, width):
    return text + " " * max(0, width - vis(text))


class Screen:
    def __init__(self, colour=True, cols=80):
        self.colour = colour
        self.cols = max(60, min(cols, 120))
        # Things said at start-up that must not be scrolled or painted away.
        self.standing = []
        self._marks = collections.deque(maxlen=64)

    def c(self, code, text):
        return f"{code}{text}{R}" if self.colour else str(text)

    def ltc_rate(self, frames_in, now):
        """Decoded frames per second over the last few seconds.

        Averaged over the whole run instead, a feed that has been solid for ten
        minutes still reads low after a single dropout, which is exactly when
        someone is looking at it."""
        self._marks.append((now, frames_in))
        while len(self._marks) > 2 and now - self._marks[0][0] > 3.0:
            self._marks.popleft()
        t0, f0 = self._marks[0]
        span = now - t0
        if span < 0.5:
            return None
        return (frames_in - f0) / span


# A fault older than this is history, not a warning. Counters that only ever
# go up were being drawn in red forever: "14 packets failed" stayed on screen
# long after the cable was back, which is how a warning panel stops meaning
# anything. Jeff, 2026-09-14.
RECENT_S = 20.0


def _recent(obj):
    """Did this thing fail within the window that still counts as now?"""
    age = getattr(obj, "seconds_since_error", None)
    return age is not None and age <= RECENT_S


def history_for(p):
    """Faults that have STOPPED. Said once, quietly, not in red."""
    out = []
    s = getattr(p, "sender", None)
    if s is not None and not _recent(s):
        n = getattr(s, "send_errors", 0)
        if n:
            out.append(f"{n} packet send(s) failed earlier in this run. "
                       f"None in the last {int(RECENT_S)}s. "
                       f"Last was: {getattr(s, 'last_error', '')}")
        r = getattr(s, "reopens", 0)
        if r:
            out.append(f"The output socket was rebuilt {r} time(s) earlier "
                       f"in this run.")
    trig = getattr(p, "trigger", None)
    if trig is not None and not _recent(trig):
        n = getattr(trig, "fire_errors", 0) + getattr(trig, "dropped", 0)
        if n:
            out.append(f"{n} scene trigger(s) failed earlier in this run. "
                       f"None in the last {int(RECENT_S)}s. "
                       f"Last was: {getattr(trig, 'last_error', '')}")
    a = getattr(p, "audio", None)
    if a is not None and getattr(a, "attached", True) and not _recent(a):
        if getattr(a, "open_errors", 0):
            out.append(f"The audio input failed to open "
                       f"{a.open_errors} time(s) earlier in this run. "
                       f"It is open now.")
        if getattr(a, "reopens", 0):
            out.append(f"The audio input went silent and was rebuilt "
                       f"{a.reopens} time(s) earlier in this run. A USB "
                       f"interface that does this under load will do it "
                       f"again.")
        if getattr(a, "pa_resets", 0):
            out.append(f"The audio system itself was rebuilt "
                       f"{a.pa_resets} time(s) earlier in this run.")
    return out


CLOCK_QUIET_S = 2.0


def shown_state(p):
    """The state as the operator should read it, on the page and here.

    With this machine as the show clock there is no feed to lose: between
    cues the chase engine reads LOST by design, and drawing that red would
    send the operator looking for a cable. That case, and only that case,
    reads STANDBY. With no clock block (GPL) or a slave clock, LOST is LOST."""
    clk = getattr(p, "clock", None)
    if clk is not None and clk.master and p.state == LOST \
            and not getattr(clk, "playing", False):
        return STANDBY
    return p.state


def clock_warnings(clk):
    """What is wrong with the show clock's Art-Net timecode, as sentences.

    Empty when there is no clock block, which is every GPL run."""
    if clk is None:
        return []
    out = []
    tc = getattr(clk, "out", None)
    ticker = getattr(clk, "ticker", None)
    if tc is not None and _recent(tc):
        failing = getattr(tc, "failing_labels", [])
        if failing:
            out.append(
                f"Art-Net timecode is not reaching {', '.join(failing)}. "
                f"A receiver with no timecode holds its last frame and then "
                f"goes dark on its own. Last error: {tc.last_error}")
        else:
            out.append(f"Art-Net timecode had {tc.send_errors} failed "
                       f"send(s) recently. Last error: {tc.last_error}")
    sending = (getattr(clk, "playing", False) if clk.master
               else getattr(clk, "last_sent", None) is not None)
    age = getattr(tc, "seconds_since_ok", None) if tc is not None else None
    quiet = (age > CLOCK_QUIET_S if age is not None
             else bool(getattr(tc, "send_errors", 0)))
    if tc is not None and sending and quiet:
        out.append(
            "No Art-Net timecode has left this machine "
            + ("since the cue started" if age is None
               else f"for {age:.0f}s")
            + " although a cue is playing. MadMapper and BEYOND are not "
              "being told the time.")
    reader = getattr(clk, "reader", None)
    if reader is not None and reader.freerunning:
        out.append("Timecode from MadMapper was lost during the show. BEYOND "
                   "is being sent this machine's own count, to the end of "
                   "the show.")
    if ticker is not None and ticker.errors:
        out.append(f"The show clock hit {ticker.errors} error(s) and kept "
                   f"going. Last: {ticker.last_error}")
    return out


def warnings_for(p, dec, tl, standing=()):
    """Everything wrong RIGHT NOW, as sentences. Order is worst first.

    Anything that has stopped happening belongs in history_for, not here."""
    out = list(standing)
    rate, drop, confident = dec.detected_rate

    if rate is not None and round(rate) != tl.count:
        out.append(
            f"The source is sending {rate:g} fps but the timeline is built for "
            f"{tl.fps:g}. Every cue will play at the wrong speed. Fix the "
            f"timeline's \"fps\" or the generator, then restart.")
    elif rate is not None and abs(rate - tl.fps) > 0.01:
        # Same count, different real rate: 29.97 against 30. Silent and slow.
        slip = abs(tl.fps - rate) / tl.fps
        out.append(
            f"The source measures {rate:g} fps but the timeline says "
            f"{tl.fps:g}. Same frame numbers, {slip*100:.2f}% different speed: "
            f"about {slip*1800:.1f}s adrift by the end of a 30 minute set. "
            f"Set \"fps\": {rate:g} in the timeline.")

    if rate is not None and drop != tl.drop:
        out.append(
            f"The source is sending {'drop' if drop else 'non-drop'} frame and "
            f"the timeline is {'drop' if tl.drop else 'non-drop'}. The two "
            f"drift apart by 2 frames a minute. Set \"drop\": "
            f"{'true' if drop else 'false'} in the timeline.")

    if p.thread_restarts:
        out.append(f"The output thread stopped and was restarted "
                   f"{p.thread_restarts} time(s). Output paused each time. "
                   f"Check the log before trusting this run.")
    if p.loop_errors:
        out.append(f"{p.loop_errors} error(s) inside the output loop. "
                   f"Last: {p.last_loop_error}")

    rig = getattr(p, "rig", None)
    if rig is not None and rig.baseline and rig.missing:
        if len(rig.missing) == len(rig.baseline):
            out.append(
                f"NOTHING on the rig is answering. All {len(rig.baseline)} "
                f"controllers stopped at once, so packets are leaving this Mac "
                f"and reaching nothing. Check the network cable, the adapter "
                f"and the switch. Stop and Run after it is back, or the socket "
                f"keeps sending into the old route.")
        else:
            out.append(
                f"{len(rig.missing)} of {len(rig.baseline)} controllers "
                f"stopped answering: {', '.join(rig.missing[:6])}"
                + (" ..." if len(rig.missing) > 6 else ""))

    # -- Advatek scene triggers, the alternate playback mode --------------
    trig = getattr(p, "trigger", None)
    if getattr(p, "trigger_armed", False) and trig is not None:
        muted = getattr(p.sender, "muted", []) or []
        if not muted:
            out.append(
                "Scene triggers are armed but NOTHING is muted, so the "
                "Advateks are still receiving live pixel data and will ignore "
                "every trigger. The rig looks right and is not running the "
                "way you think it is. Turn scene triggers off and back on.")
        else:
            out.append(
                f"BACKUP MODE: {len(muted)} controller(s) are muted and "
                f"playing scenes recorded on their own cards, not this "
                f"sequence. This Mac cannot resync, pause or nudge them, and "
                f"a jump restarts a scene from its own beginning. The other "
                f"controllers are still being streamed to normally.")
        if _recent(trig):
            out.append(f"A scene trigger did not go out: "
                       f"{getattr(trig, 'last_error', '')}")
        if getattr(trig, "fired", 0) == 0 and p.current_cue is not None:
            out.append("A cue is running and no scene trigger has gone out "
                       "yet. The Advateks are dark until one does.")

    out.extend(clock_warnings(getattr(p, "clock", None)))

    s = p.sender
    bcast = getattr(s, "broadcast_dests", None)
    if bcast:
        out.append(
            f"{len(bcast)} universe destination(s) are broadcast addresses "
            f"({', '.join(bcast[:4])}). Every device on this network reads "
            f"and discards every frame, {int(1000 / max(1, p.step_ms))} times "
            f"a second. Set those controllers to their own IP in xLights.")
    q = getattr(s, "quiet_destinations", 0)
    if q:
        out.append(
            f"{q} controller address(es) refused packet after packet and are "
            f"being rested between retries. They are not receiving anything. "
            f"Last: {getattr(s, 'last_error', '')}")
    reopens = getattr(s, "reopens", 0)
    if reopens and _recent(s):
        out.append(f"The output socket failed and was rebuilt {reopens} "
                   f"time(s). That is a network fault, not a program fault: "
                   f"check the cable, the switch and the interface.")
    age = getattr(s, "seconds_since_ok", None)
    if age is not None and age > 2.0:
        out.append(f"This Mac has not accepted a packet for output in "
                   f"{age:.0f}s, so nothing is leaving it at all.")
    if getattr(s, "send_errors", 0) and _recent(s):
        out.append(f"{s.send_errors} packet send(s) failed. "
                   f"Last: {getattr(s, 'last_error', '')}")

    # A show file that asks for a preshow look and did not get one: the rig
    # will be BLACK before the show, between the sets and after any loss, and
    # nothing used to say so once the screen had painted over the start-up
    # warning.
    if getattr(p, "idle_path", None) and p.idle_cue is None:
        out.append("The preshow sequence named in this show file did not "
                   "load, so there is no look to fall back to: before "
                   "timecode, between the sets and on a lost feed the rig "
                   "goes BLACK. Fix the file and restart.")
    if getattr(p, "jump_rejects", 0):
        out.append(f"{p.jump_rejects} timecode frame(s) were rejected as "
                   f"impossible jumps (last: {p.last_rejected_tc}). One or "
                   f"two is a noisy cable. A stream of them means the feed "
                   f"is bad enough that the show is running on its own "
                   f"clock, and it will drift.")
    if p.out_of_range_channels:
        out.append(f"{p.out_of_range_channels} channels in the current sequence "
                   f"are addressed past the end of the controller map and are "
                   f"not being sent. Re-render after a controller change.")
    if p.render_errors:
        out.append(f"{p.render_errors} sequence read error(s). "
                   f"Last: {p.last_error}")
        out.append("A read error on a sequence that opened fine almost always "
                   "means the file changed underneath the player: xLights "
                   "re-rendered it while it was playing. Wait for the render "
                   "to finish, then press Reload.")
    a = getattr(p, "audio", None)
    if a is not None:
        named = (a.device.get("name") or "").strip() or "The timecode input"
        attached = getattr(a, "attached", True)
        if getattr(a, "stuck", False):
            out.append(
                f"The timecode input has not opened for "
                f"{a.seconds_down:.0f}s and is not recovering on its own. "
                f"The audio system has been rebuilt "
                f"{getattr(a, 'pa_resets', 0)} time(s) and it still will not "
                f"open. QUIT ltcplay and start it again: a stop and a start "
                f"leave the same process running. Last: {a.last_error}")
        elif not attached:
            # First in the list: with no input there is no timecode, so every
            # other reading below is about a show that is holding a look.
            out.append(f"{named} is not open, so no timecode is arriving and "
                       f"the show is holding the preshow look. The input is "
                       f"retried every second and picked up the moment it "
                       f"appears. Last reason: "
                       f"{getattr(a, 'last_error', '') or 'not attached'}")
        quiet = a.seconds_since_block
        if attached and quiet is not None and quiet > 1.0:
            out.append(f"No audio has arrived from {named} for "
                       f"{quiet:.0f}s. That is the interface, not the timecode "
                       f"generator: check it is still plugged in and powered. "
                       f"The input rebuilds itself once a second until it "
                       f"comes back.")
        if attached and getattr(a, "open_errors", 0) and _recent(a) and \
                getattr(a, "last_error", ""):
            out.append(f"The audio input has failed to open "
                       f"{a.open_errors} time(s). Last: {a.last_error}")
        if attached and a.reopens and _recent(a):
            out.append(f"The audio input went silent and was rebuilt "
                       f"{a.reopens} time(s). A USB interface that does this "
                       f"under load will do it again.")
        if a.level.clipped:
            out.append(f"The input has clipped {a.level.clipped} time(s). "
                       f"Timecode decodes badly when it clips: turn the trim "
                       f"down until the meter stops hitting the end.")
        elif a.blocks > 40 and a.level.hold < 0.02:
            out.append(f"{a.device['name']} input {a.channel} is silent. "
                       f"Either the cable is in the wrong socket or the "
                       f"timecode is on another input of this interface. "
                       f"`ltcplay find` says which.")
        elif a.blocks > 40 and a.level.hold < 0.05:
            out.append(f"The input level is very low ({a.level.hold:.2f}). It "
                       f"may decode and it may not. Turn the trim up.")

    if (getattr(p, "idle_cue", None) is not None and p.gaps != "idle"):
        out.append(f"A preshow sequence is loaded but 'gaps' is '{p.gaps}', "
                   f"so between cues and through the interval the rig is "
                   f"{p.gaps}, not on the preshow sequence. Set "
                   f"\"gaps\": \"idle\" in the show file to use it there.")
    if getattr(p, "freerun_epoch", None) is not None:
        out.append("FREE RUNNING. The show is on this Mac's own clock; the "
                   "timecode feed is read but does not drive the rig. The "
                   "clock keeps counting past the end of this set and into "
                   "the next one. Press Back to timecode on the web page to "
                   "hand the show back to the feed.")
    # With this machine as the show clock there is no feed: between cues
    # the chase engine reads LOST by design, and every sentence below would
    # send the operator looking for a cable that does not exist.
    clk = getattr(p, "clock", None)
    if clk is not None and clk.master:
        return out
    if p.state == LOST and p.source == HOLD:
        out.append("Timecode has stopped and the rig is holding its last frame "
                   "because --on-lost is 'hold'. If this is a show and the feed "
                   "has actually failed, that is the wrong look: stop and "
                   "restart without it.")
    if p.state == LOST and p.ltc_frames_in == 0:
        out.append("No timecode has been decoded yet. Check the input device, "
                   "the cable and the level. `ltcplay monitor` tests the feed "
                   "on its own.")
    elif rate is None and p.ltc_frames_in:
        out.append("Timecode is arriving but no second boundary has passed yet, "
                   "so the frame rate is still unconfirmed.")
    return out


def _meter(level, width=10):
    on = int(round(min(1.0, max(0.0, level)) * width))
    return "[" + "=" * on + " " * (width - on) + "]"


def _bar(frac, width):
    frac = 0.0 if frac is None else max(0.0, min(1.0, frac))
    on = int(round(frac * width))
    return "[" + "#" * on + "-" * (width - on) + "]"


def render(p, dec, tl, sc, started_at):
    now = _now()
    W = sc.cols
    L = []
    add = L.append

    title = tl.name or "ltcplay"
    left = f" {sc.c(B, getattr(sc, 'product', 'ltcplay'))}  {title}"
    right = sc.c(DIM, f"up {format_clock(now - started_at)}   "
                      f"{time.strftime('%H:%M:%S')}")
    add(pad(left, W - vis(right) - 1) + right)
    add("")

    # -- the two numbers ---------------------------------------------------
    shown = shown_state(p)
    col = STATE_COLOUR.get(shown, "")
    ltc_text = p.last_ltc_text or "--:--:--:--"
    if shown == STANDBY:
        age_note = "no cue playing; this machine is the show clock"
    elif p.last_ltc_at is None:
        age_note = "waiting for timecode"
    elif p.state == LOCKED:
        age_note = ""
    elif p.state == "FREERUN":
        # The SHOW is free running; the FEED is whatever it is. Reporting the
        # show's state against the LTC readout said "frozen" beside a number
        # that was visibly counting. Round 3 of the audit, 2026-09-13.
        feed = getattr(p, "feed_state", None)
        age = (now - p.last_ltc_at) if p.last_ltc_at else None
        if feed == LOCKED:
            age_note = "the feed is healthy; the show is not following it"
        elif feed == PARKED:
            age_note = "the feed is parked; the show is not following it"
        elif age is None:
            age_note = "no timecode has arrived"
        else:
            age_note = f"nothing in for {age:.1f}s"
    elif p.state == PARKED:
        age_note = "the source is sending this frame over and over"
    else:
        age_note = f"frozen, nothing in for {now - p.last_ltc_at:.1f}s"
    add("  LTC IN     " + pad(sc.c(B + col, ltc_text), 16)
        + pad(sc.c(col, shown), 16) + sc.c(DIM, age_note))

    play = tl.format(p.tc_seconds) if p.tc_seconds is not None and p.tc_seconds >= 0 \
        else "--:--:--:--"
    scol = SOURCE_COLOUR.get(p.source, "")
    stext = SOURCE_TEXT.get(p.source, p.source)
    extra = ""
    if p.state == PARKED:
        held = now - p.parked_since if p.parked_since else 0.0
        extra = f"paused, holding for {format_clock(held)}"
    elif p.state == LOST and p.source == HOLD:
        extra = "held where timecode stopped"
    elif p.state == FREEWHEEL:
        left = p.hold_s - (now - p.last_ltc_at) if p.last_ltc_at else 0
        extra = f"free-rolling, preshow in {max(0.0, left):.1f}s"
    elif p.state == LOCKED and p.ltc_frames_in > 1:
        extra = f"sync {p.sync_delta_ms:+.0f} ms"
    add("  PLAYING    " + pad(sc.c(B, play), 16)
        + pad(sc.c(scol, stext), 16) + sc.c(DIM, extra))
    add("")

    # -- rate --------------------------------------------------------------
    rate, drop, confident = dec.detected_rate
    if rate is None:
        rate_txt = sc.c(DIM, "not known yet")
    else:
        meas = dec.measured_fps
        body = rate_label(rate, drop)
        if meas:
            body += f"   (measured {meas:.3f} over {dec.measured_span:.0f}s)"
        good = abs(rate - tl.fps) < 0.01 and drop == tl.drop
        rate_txt = sc.c(GREEN if good else RED, body)
    add(f"  rate in    {rate_txt}")
    add(f"  timeline   {tl.rate_label}")
    dec_rate = sc.ltc_rate(dec.frames_decoded, now)
    rate_now = "--" if dec_rate is None else f"{dec_rate:.1f}"
    add(sc.c(DIM, f"  feed       {rate_now}/s now, {dec.frames_decoded} frames "
                  f"in total, {dec.sync_errors} sync errors"))
    a = getattr(p, "audio", None)
    if a is not None:
        lv = a.level
        good = lv.verdict() == "ok"
        add(f"  input      {sc.c(DIM, a.device['name'])} in {a.channel}   "
            + _meter(lv.hold) + f" {lv.hold:4.2f} "
            + sc.c(GREEN if good else AMBER, lv.verdict()))
    add("")

    # -- now ---------------------------------------------------------------
    cue = p.current_cue
    if cue is not None and cue.fseq is not None:
        el = p.tc_seconds - cue.tc_seconds
        import os as _os
        add(f"  NOW        {sc.c(B, cue.name)}   "
            + sc.c(DIM, _os.path.basename(cue.path)))
        # Where xLights is. This is the number an operator writes down when
        # something on the rig looks wrong, and it is not timecode.
        from .tc import format_seq
        add("             " + sc.c(CYAN, f"{format_seq(el)}")
            + sc.c(DIM, f" of {format_seq(cue.duration)} in xLights"
                        f"   frame {p.current_frame} of "
                        f"{cue.fseq.frame_count}"))
        add("             " + pad(f"{cue.tc_text} to {tl.format(cue.end_seconds)}", 32)
            + f"{format_clock(el)} in, {format_clock(cue.duration - el)} left")
        add(sc.c(DIM, "             " + _bar(el / cue.duration if cue.duration else 0,
                                             min(52, W - 20))))
    elif p.source == IDLE:
        held = getattr(p, "override", None) == "preshow"
        add(f"  NOW        {sc.c(CYAN, 'preshow loop')}"
            + (sc.c(B, "   HELD BY YOU") if held else ""))
        add(sc.c(DIM, "             " + ("timecode is being read but is not "
                                         "driving the rig" if held else
                                         "running until timecode starts")))
        add("")
    else:
        add(f"  NOW        {sc.c(DIM, 'nothing (between cues)')}")
        add("")
        add("")
    add("")

    # -- up next -----------------------------------------------------------
    nxt = p.next_cue
    if nxt is not None:
        add(f"  UP NEXT    {sc.c(B, nxt.name)}")
        if p.tc_seconds is not None and p.tc_seconds >= 0:
            add("             " + pad(f"starts {nxt.tc_text}", 32)
                + f"in {format_clock(nxt.tc_seconds - p.tc_seconds)}")
        else:
            add(f"             starts {nxt.tc_text}")
    else:
        add(f"  UP NEXT    {sc.c(DIM, 'nothing, this is the last cue')}")
        add("")
    add("")

    # -- health ------------------------------------------------------------
    s = p.sender
    age = getattr(s, "seconds_since_ok", None)
    age_txt = "never" if age is None else f"{age:.1f}s ago"
    ok = age is not None and age < 1.0 and not getattr(s, "send_errors", 0)
    rig = getattr(p, "rig", None)
    if rig is not None and rig.baseline:
        n_up = len(rig.baseline) - len(rig.missing)
        rig_txt = sc.c(GREEN if not rig.missing else RED,
                       f"{n_up}/{len(rig.baseline)} controllers answering")
    else:
        rig_txt = sc.c(DIM, "controllers not being checked")
    add(f"  output     {getattr(s, 'universe_count', 0)} universes   "
        f"{getattr(s, 'send_errors', 0)} send errors   "
        f"accepted by this Mac {sc.c(GREEN if ok else RED, age_txt)}   "
        f"{rig_txt}")
    add(sc.c(DIM, f"  engine     {p.frames_sent} frames out, {p.jumps} jumps, "
                  f"{p.loop_errors} loop errors, {p.thread_restarts} restarts"))

    warns = warnings_for(p, dec, tl, getattr(sc, "standing", ()))
    if warns:
        add("")
        for w in warns:
            first, rest = _wrap(w, W - 8)
            add("  " + sc.c(RED + B, "!! ") + sc.c(RED, first))
            for line in rest:
                add("     " + sc.c(RED, line))
    add("")
    add(sc.c(DIM, "  ctrl-c to stop"))
    return L


def _wrap(text, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        lines.append(cur)
    return (lines[0] if lines else ""), lines[1:]


def paint(lines, out):
    """Redraw in place without the full-screen clear, which flickers."""
    buf = ["\033[H"]
    for line in lines:
        buf.append(line)
        buf.append("\033[K\n")
    buf.append("\033[J")
    out.write("".join(buf))
    out.flush()


def one_line(p, dec, tl):
    """The --quiet form: one appendable line, safe to pipe to a file."""
    rate, drop, _ = dec.detected_rate
    cue = p.current_cue.name if p.current_cue else SOURCE_TEXT.get(p.source, "-")
    nxt = p.next_cue.name if p.next_cue else "-"
    return (f"{time.strftime('%H:%M:%S')} {shown_state(p):10s} "
            f"in={p.last_ltc_text or '--:--:--:--'} "
            f"play={tl.format(p.tc_seconds) if p.tc_seconds >= 0 else '--:--:--:--'} "
            f"rate={rate if rate else '?'}{'df' if drop else ''} "
            f"now={cue} next={nxt} "
            f"err={getattr(p.sender,'send_errors',0)}/{p.loop_errors}")
