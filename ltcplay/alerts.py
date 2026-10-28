"""Alerts: one place for everything that waits for a person (Jeff's
degraded-mode rulings, 2026-10-07 and 2026-10-12; the spec's section 2).

An alert is a plain sentence about something wrong, with the few things a
person may press about it. It is raised by the engine (the scheduler, the
Stream Deck process over loopback, later the pre-start gate), shown on the
rack screen's status strip and on every remote page, sounded in the booth
over a private speaker (boothaudio.py) when raised and again every
resound_s (RESOUND_S, 30 s, or the booth's own response_s) until it is
acknowledged (Jeff, 2026-10-26), and journaled as a fault. It stays until
a person clears it with a deliberate press: Acknowledge on the rack screen
or any page, or any key on the Stream Deck. Nothing on the rig is ever
changed by a timer here; what happens at showtime with an alert open is
alerthold.py's.

Pure logic, injected clocks, no sockets, no threads: web.py's Remote owns
one Alerts and gives it the journal, the booth player and the show state.

Rules (the spec, section 2):
- one open alert per kind; raising a kind again updates its sentence and
  never re-sounds the booth note within RESOUND_S of the last note for
  that kind;
- every raise writes one journal line with fault=True (the soak counts
  those: an alert in a clean soak is a defect); every clear writes one
  line, fault=False, naming the action, the surface and the address;
- the booth note plays once per raise; when it cannot (no device named,
  the device missing, no file), the pulse runs instead and the banner
  says so (BOOTH_UNAVAILABLE);
- the blue floor-pixel pulse (player.py) runs while an alert of kind
  audio is open, and while an alert is open whose booth note could not
  play. Before and between shows it always runs; during a show only for
  kind audio (the audience has already lost the music). Jeff is still to
  confirm the during-show rule;
- Acknowledge and Confirm clear and change NOTHING on the rig. The
  options that choose (Start anyway, Delay, Stop, Disarm all) are not
  acted on here: Disarm all is the existing disarm-all route, the rest
  come with the pre-start gate (Phase 2) and the B rows (Phase 3).

Every alert kind but the display only ones has a recorded booth note,
booth_<kind>.wav (boothaudio.py finds it by that name); a one off note
that is not an alert (the showtime hold's "held") is play_note.

Severity is per kind, in plain words for the strip: a "fault" is
something broken (the deck gone, the audio gone), a "warning" is the gate
asking before an unattended start.
"""
import time

KINDS = ("gate", "audio", "video", "deck", "start", "flames", "link",
         "timecode", "lasers", "users", "aim", "follow", "preroll")
SEVERITY = {"gate": "warning", "audio": "fault", "video": "fault",
            "deck": "fault", "start": "fault", "flames": "fault",
            "link": "fault", "timecode": "fault", "lasers": "fault",
            "users": "fault", "aim": "warning", "follow": "warning",
            # The rig check before a show failed (preroll.py): it holds the
            # show at its time like any fault (alerthold.py).
            "preroll": "fault"}
# Display only (2026-10-28): an alert of these kinds is shown on every
# screen and journaled, and cleared like any other, but it never sounds the
# booth, never runs the pulse, never holds a show at showtime
# (alerthold.py leaves it out) and never reaches the Stream Deck
# (streamdeck.ScreenKeys.alerts leaves it out: no ACK key). "aim": the wave flamer's aim slots differ
# between flamesafe and LTC Player (remote.Remote.aim_view). "follow":
# MadMapper is not following the show's timecode (madmapperwatch.
# FollowCheck; Jeff's field report, 2026-10-28).
DISPLAY_ONLY = frozenset(("aim", "follow"))
OPTIONS = ("Acknowledge", "Continue", "Stop", "Start anyway", "Delay",
           "Confirm", "Disarm all")
# The presses that clear an alert and change nothing on the rig. Any
# other option chooses something and is somebody else's route.
CLEARING = frozenset(("Acknowledge", "Confirm", "Continue"))
# The booth note again every this many seconds while an alert stays open
# (Jeff, 2026-10-26: 60 -> 30, the booth's response_s). A booth config's
# own response_s wins (resound_s()).
RESOUND_S = 30.0
BOOTH_UNAVAILABLE = "Booth speaker not available"
# The kinds whose pulse also runs during a show (section 2).
PULSE_DURING_SHOW = frozenset(("audio",))


class Alert:
    __slots__ = ("id", "kind", "text", "since", "options", "severity",
                 "booth", "booth_ok", "pulse")

    def __init__(self, id, kind, text, since, options):
        self.id = id
        self.kind = kind
        self.text = text
        self.since = since
        self.options = tuple(options)
        self.severity = SEVERITY[kind]
        self.booth = ""          # what happened to the booth note
        self.booth_ok = None     # True played, False could not, None quiet
        self.pulse = False

    def view(self):
        text = self.text
        if self.booth_ok is False:
            text = f"{text} {BOOTH_UNAVAILABLE}."
        return {"id": self.id, "kind": self.kind, "text": text,
                "severity": self.severity, "since": int(self.since * 1000),
                "options": list(self.options), "booth": self.booth,
                "pulse": self.pulse}


class Alerts:
    """`journal(text, fault=, action=, outcome=, who=, screen=)`;
    `booth` has play(kind) -> (ok, sentence) or is None (no booth
    speaker configured: every banner says so); `show_running()` -> True
    while a show is live (SHOW or PAUSED); `on_change()` is called after
    every raise and clear, for the pulse."""

    def __init__(self, journal=None, booth=None, clock=time.monotonic,
                 wall=time.time, show_running=lambda: False,
                 on_change=None):
        self._journal = journal or (lambda text, **kw: None)
        self.booth = booth
        self._clock = clock
        self._wall = wall
        self._show_running = show_running
        self._on_change = on_change
        self._open = {}            # kind -> Alert
        self._sounded = {}         # kind -> clock() of the last booth note
        self._tried = {}           # kind -> clock() tick() last tried it
        self._ids = 0

    # -- raise -------------------------------------------------------------
    def raise_(self, kind, text, options=("Acknowledge",)):
        kind = str(kind or "").strip().lower()
        if kind not in KINDS:
            raise ValueError(f"{kind!r} is not an alert kind. One of: "
                             f"{', '.join(KINDS)}.")
        text = " ".join(str(text or "").split())
        if not text:
            raise ValueError("An alert needs a sentence a person can act "
                             "on.")
        opts = tuple(str(o) for o in (options or ("Acknowledge",)))
        bad = [o for o in opts if o not in OPTIONS]
        if bad:
            raise ValueError(f"{', '.join(bad)}: not an alert option. One "
                             f"of: {', '.join(OPTIONS)}.")
        if not any(o in CLEARING for o in opts):
            # Every alert can be acknowledged (the deck's keys do only
            # that), whatever else it offers.
            opts = ("Acknowledge",) + opts
        a = self._open.get(kind)
        if a is not None:
            a.text = text
            a.options = opts
            outcome = "updated"
        else:
            self._ids += 1
            a = Alert(self._ids, kind, text, self._wall(), opts)
            self._open[kind] = a
            outcome = "raised"
        self._sound(a)
        self._journal(f"Alert ({a.severity}, {kind}): {text}", fault=True,
                      action="alert", outcome=outcome, who="system",
                      screen="engine")
        self._changed()
        return a

    def resound_s(self):
        """The re-sound interval: the booth config's response_s when there
        is one, else RESOUND_S."""
        cfg = getattr(self.booth, "config", None)
        try:
            v = float((cfg or {}).get("response_s") or RESOUND_S)
        except (TypeError, ValueError, AttributeError):
            v = RESOUND_S
        return v if v > 0 else RESOUND_S

    def tick(self):
        """The booth note again for every open alert whose last note is
        resound_s() old: until it is acknowledged (Jeff, 2026-10-26).
        Sound only; nothing else. Called by the remote's alert thread."""
        now = self._clock()
        every = self.resound_s()
        for a in list(self._open.values()):
            # A note that could not play is tried again at the same pace,
            # never on every tick (a missing device is asked once per
            # interval, not four times a second).
            tried = self._tried.get(a.kind)
            if tried is not None and now - tried < every:
                continue
            self._sound(a)

    def play_note(self, name):
        """One booth note, once, for an event that is not an alert
        (boothaudio.HOOKUPS: "held" from the showtime hold, and the notes
        the pre-roll check and the cleaning routine will play). It never
        repeats, never runs the pulse, opens no banner and writes no
        fault line. Never raises. (ok, sentence)."""
        if self.booth is None:
            return False, "No booth speaker is configured."
        try:
            ok, why = self.booth.play(name)
        except Exception as e:           # noqa: BLE001  never the caller's
            ok, why = False, f"{type(e).__name__}: {e}"
        return bool(ok), str(why or "")

    def sound_now(self):
        """Every open alert's booth note at once (the showtime Hold,
        alerthold.py), whatever the interval."""
        for a in list(self._open.values()):
            self._sounded.pop(a.kind, None)
            self._sound(a)

    def _sound(self, a):
        """The booth note, never twice within resound_s() for one kind.
        The pulse flag follows whether it could play."""
        if a.kind in DISPLAY_ONLY:
            a.booth_ok, a.booth = None, "display only: no booth note"
            return
        now = self._clock()
        last = self._sounded.get(a.kind)
        if last is not None and now - last < self.resound_s():
            a.booth = a.booth or (f"quiet: sounded less than "
                                  f"{self.resound_s():g} s ago")
            return
        if self.booth is None:
            a.booth_ok, a.booth = False, "No booth speaker is configured."
            return
        self._tried[a.kind] = now
        try:
            ok, why = self.booth.play(a.kind)
        except Exception as e:           # noqa: BLE001  never the caller's
            ok, why = False, f"{type(e).__name__}: {e}"
        a.booth_ok, a.booth = bool(ok), str(why or "")
        if ok:
            self._sounded[a.kind] = now

    # -- clear -------------------------------------------------------------
    def clear(self, kind=None, id=None, action="Acknowledge", who="",
              screen="", address=""):
        """Clear by kind, by id, or (neither) every open alert: a Stream
        Deck key acknowledges whatever is open. Only a CLEARING option
        clears here; the others choose, and are refused with the sentence
        the page shows. Returns (ok, sentence)."""
        action = str(action or "Acknowledge").strip()
        if action.lower() == "acknowledge":
            action = "Acknowledge"
        if action not in CLEARING:
            return False, (f"{action} is not built here yet: it chooses "
                           f"something, and only Acknowledge, Confirm and "
                           f"Continue clear an alert in this build.")
        picked = []
        for a in list(self._open.values()):
            if id is not None and a.id != id:
                continue
            if kind and a.kind != kind:
                continue
            picked.append(a)
        if not picked:
            return False, "There is no open alert to clear."
        for a in picked:
            del self._open[a.kind]
            where = screen or "unknown screen"
            if address:
                where = f"{where} at {address}"
            self._journal(f"Alert cleared ({a.kind}): {action} by "
                          f"{who or 'someone'} on the {where}. The rig was "
                          f"not changed. It said: {a.text}",
                          fault=False, action="alert", outcome="cleared",
                          who=who, screen=screen)
        self._changed()
        n = len(picked)
        return True, (f"{action}: {n} alert{'s' if n != 1 else ''} "
                      f"cleared. Nothing on the rig was changed.")

    # -- read --------------------------------------------------------------
    def open(self):
        return sorted(self._open.values(), key=lambda a: a.id)

    def pending(self):
        return bool(self._open)

    def pulse_wanted(self):
        """Section 2: the blue pulse runs while an alert of kind audio is
        open, and while any alert is open whose booth note could not
        play; during a show only for the kinds in PULSE_DURING_SHOW."""
        try:
            live = bool(self._show_running())
        except Exception:                # noqa: BLE001
            live = False
        want = False
        for a in self._open.values():
            mine = a.kind in PULSE_DURING_SHOW or a.booth_ok is False
            if live and a.kind not in PULSE_DURING_SHOW:
                mine = False
            a.pulse = mine
            want = want or mine
        return want

    def view(self):
        self.pulse_wanted()
        return [a.view() for a in self.open()]

    def _changed(self):
        if self._on_change is None:
            return
        try:
            self._on_change()
        except Exception:                # noqa: BLE001
            pass
