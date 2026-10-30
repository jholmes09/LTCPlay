"""The alert hold (Jeff's rulings, 2026-10-26 and 2026-10-30): what happens
when a show's time comes while an alert is still open.

Jeff, 2026-10-30: nothing plays on the public PA automatically. "I want to
delete the function where the delay audio plays automatically in any
scenario. We can still notify Andy of issues with the in-booth speaker,
but nothing on the public PA." The countdown and the automatic Delayed
announcement that used to follow it are gone; an announcement on the PA
is only ever an operator's own press (announce.py).

- No timer while idle: an alert raised between shows does nothing
  automatic here (alerts.py re-sounds its booth note until acknowledged).
- At showtime, with any alert open that is not display only
  (alerts.DISPLAY_ONLY), the schedule Holds IMMEDIATELY instead of
  starting the show (schedule_service.Service._hold_at_showtime_for_
  alerts, a system action). The BOOTH speaker plays its "held" note once
  (booth_held.wav, "Show is held for an open alert."), then each open
  alert's own note, and every screen says "Held: alert open. Press
  Resume." Nothing is played on the PA.
- Acknowledge does not resume: the show waits for the operator's own
  Resume (or Start now).
- A running show is never held, stopped or disarmed: the scheduler only
  holds from IDLE or STANDBY, and nothing here ever stops or disarms
  anything. No flame or Tier A path is touched.
- Once the operator Resumes (from any Hold) with a showtime due, that
  showtime is never held again for the same open alerts
  (schedule_service remembers night, show and the alert ids); a NEW alert
  at the same showtime holds again.

Pure logic with an injected clock. The scheduler calls started() through
its on_alert_hold hook after its own tick; the screens read view().
"""
import time


class AlertHold:
    """`alerts` an alerts.Alerts; `schedule` the scheduler service (its
    hold_still_claimed(epoch) and _journal_line). There is no announcement
    service here on purpose: an alert hold never plays anything on the
    PA (Jeff, 2026-10-30)."""

    def __init__(self, alerts, schedule=None, clock=time.monotonic):
        self.alerts = alerts
        self.schedule = schedule
        self._clock = clock
        self.hold = None        # the latest alert hold, or None

    # -- the scheduler's side ---------------------------------------------
    def gate(self):
        """For Service.showtime_gate: (a sentence naming the open alerts,
        their ids), or None when there is none."""
        open_ = self._holding()
        if not open_:
            return None
        return ("; ".join(f"{a.kind}: {a.text}" for a in open_),
                frozenset(a.id for a in open_))

    def _holding(self):
        """The open alerts that hold a show at showtime: every one but a
        display only kind (alerts.DISPLAY_ONLY: the aim slots, MadMapper
        not following)."""
        from .alerts import DISPLAY_ONLY
        return [a for a in self.alerts.open() if a.kind not in DISPLAY_ONLY]

    def started(self, epoch, why):
        """Service.on_alert_hold: the Hold took. Sound the BOOTH now: the
        "held" note once, then each open alert's own note (the booth
        player plays them in that order, one after another). Nothing on
        the PA."""
        open_ = self._holding()
        self.hold = {"epoch": epoch, "why": why, "at": self._clock(),
                     "ids": {a.id for a in open_}}
        play_note = getattr(self.alerts, "play_note", None)
        if play_note is not None:
            try:
                play_note("held")
            except Exception:                    # noqa: BLE001
                pass
        try:
            self.alerts.sound_now()
        except Exception:                        # noqa: BLE001
            pass
        self._journal(
            f"Held at showtime: an alert is open ({why}). The show waits "
            f"for Resume. The booth speaker says the show is held; nothing "
            f"plays on the public PA.", outcome="held")

    def _still_held(self, h):
        svc = self.schedule
        fn = getattr(svc, "hold_still_claimed", None)
        if fn is None:
            return True
        try:
            return bool(fn(h["epoch"]))
        except Exception:                        # noqa: BLE001
            return False

    def _journal(self, text, outcome):
        svc = self.schedule
        fn = getattr(svc, "_journal_line", None)
        if fn is None:
            return
        try:
            with svc._locked():
                fn("system", text, action="alert hold", outcome=outcome)
        except Exception:                        # noqa: BLE001
            pass

    # -- the screens ----------------------------------------------------------
    def view(self):
        """{"held": bool, "text": sentence} for the screens' state card.
        Display only. A hold the operator has since Resumed (any newer
        schedule action that ended it) reads not held."""
        h = self.hold
        if h is None or not self._still_held(h):
            return {"held": False}
        if h["ids"] & {a.id for a in self.alerts.open()}:
            text = "Held: alert open. Press Resume."
        else:
            text = ("Held: the alert was acknowledged. Press Resume when "
                    "ready.")
        return {"held": True, "text": text}
