"""The alert hold (Jeff's rulings, 2026-10-26): what happens when a show's
time comes while an alert is still open.

- No timer while idle: an alert raised between shows does nothing
  automatic here (alerts.py re-sounds its booth note until acknowledged).
- At showtime, with any alert open, the schedule Holds IMMEDIATELY instead
  of starting the show (schedule_service.Service._hold_at_showtime_for_
  alerts, a system action). The booth note sounds at once and the rack
  screen says "Held: alert open".
- Then a COUNTDOWN_S countdown, shown on the rack screen, and then ONE
  "Delayed" announcement on the PA (announce.py), only when an alert of
  a kind in ANNOUNCE_KINDS was open at the Hold (audio, video, flames,
  link, timecode, start). deck and gate hold quietly.
- Acknowledge cancels the countdown and the announcement. It does NOT
  resume: the show waits for the operator's own Resume.
- The announcement is made only if the Hold actually took and still
  stands (the scheduler's hold epoch), once per Hold, and journaled as a
  system action. A missing recording or output device: the schedule stays
  held, nothing plays, and the rack screen says why.
- A running show is never held, stopped, disarmed or announced over: the
  scheduler only holds from IDLE or STANDBY, and nothing here ever stops
  or disarms anything. No flame or Tier A path is touched.

Pure logic with an injected clock; remote.Remote ticks it on a thread of
its own, with the real clock, together with alerts.Alerts.tick.
"""
import time

COUNTDOWN_S = 60.0
ANNOUNCE_KINDS = frozenset(("audio", "video", "flames", "link", "timecode",
                            "start"))
ANNOUNCEMENT = "delayed"
WHO = "LTC Player"
SCREEN = "engine"


class AlertHold:
    """`alerts` an alerts.Alerts; `schedule` the scheduler service (its
    hold_still_claimed(epoch) and _journal_line); `announce` () -> the
    announce.AnnounceService or None."""

    def __init__(self, alerts, schedule=None, announce=None,
                 clock=time.monotonic):
        self.alerts = alerts
        self.schedule = schedule
        self._announce = announce or (lambda: None)
        self._clock = clock
        self.hold = None        # the current alert hold, or None

    # -- the scheduler's side ---------------------------------------------
    def gate(self):
        """For Service.showtime_gate: a sentence naming the open alerts,
        or None when there is none."""
        open_ = self.alerts.open()
        if not open_:
            return None
        return "; ".join(f"{a.kind}: {a.text}" for a in open_)

    def started(self, epoch, why):
        """Service.on_alert_hold: the Hold took. Sound the booth now and
        start the countdown when an announcing kind is open."""
        now = self._clock()
        open_ = self.alerts.open()
        kinds = {a.kind for a in open_}
        loud = bool(kinds & ANNOUNCE_KINDS)
        self.hold = {"epoch": epoch, "why": why, "at": now,
                     "ids": {a.id for a in open_},
                     "due": now + COUNTDOWN_S if loud else None,
                     "state": "counting" if loud else "quiet",
                     "note": ""}
        try:
            self.alerts.sound_now()
        except Exception:                        # noqa: BLE001
            pass
        self._journal(
            f"Held at showtime: an alert is open ({why}). The show waits "
            f"for Resume. " + (
                f"If no one acknowledges within {COUNTDOWN_S:g} s, the "
                f"Delayed announcement plays once." if loud else
                "No announcement for this kind of alert."),
            outcome="held")

    # -- the remote's thread ------------------------------------------------
    def tick(self):
        h = self.hold
        if h is None:
            return
        if not self._still_held(h):
            self.hold = None
            return
        if h["state"] != "counting":
            return
        if self._acknowledged(h):
            h["state"], h["due"] = "cancelled", None
            self._journal("The Delayed announcement was cancelled: the "
                          "alert was acknowledged. The show still waits "
                          "for Resume.", outcome="cancelled")
            return
        if self._clock() < h["due"]:
            return
        # Once per Hold, and only on a Hold that actually took and still
        # stands (checked just above).
        h["state"], h["due"] = "announced", None
        ann = self._announce()
        if ann is None:
            h["note"] = ("No announcement was played: announcements are not "
                         "running in this engine.")
            self._journal(h["note"], outcome="not played")
            return
        try:
            ann.play(ANNOUNCEMENT, WHO, SCREEN, surface=True)
        except Exception as e:                   # noqa: BLE001
            h["state"] = "failed"
            h["note"] = (f"The Delayed announcement could not play: {e} "
                         f"The schedule stays held.")
            self._journal(h["note"], outcome="not played")
            return
        self._journal("The Delayed announcement played once: no one "
                      "acknowledged the alert within "
                      f"{COUNTDOWN_S:g} s. The show still waits for Resume.",
                      outcome="announced")

    def _acknowledged(self, h):
        """Any alert that was open at the Hold has been cleared since."""
        now_ids = {a.id for a in self.alerts.open()}
        return bool(h["ids"] - now_ids)

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

    # -- the rack screen ----------------------------------------------------
    def view(self):
        """{"held": bool, "text": sentence, "countdown_s": n or None,
        "state"} for the rack screen's state card. Display only."""
        h = self.hold
        if h is None or not self._still_held(h):
            return {"held": False}
        left = None
        if h["state"] == "counting":
            left = max(0, int(round(h["due"] - self._clock())))
            text = (f"Held: alert open. The Delayed announcement plays in "
                    f"{left} s unless the alert is acknowledged. Resume "
                    f"starts nothing until you press it.")
        elif h["state"] == "quiet":
            text = ("Held: alert open. No announcement for this kind of "
                    "alert. Press Resume when ready.")
        elif h["state"] == "cancelled":
            text = ("Held: alert acknowledged, no announcement. Press "
                    "Resume when ready.")
        elif h["state"] == "announced":
            text = (h["note"] or "Held: the Delayed announcement played. "
                    "Press Resume when ready.")
        else:
            text = f"Held: alert open. {h['note']}"
        return {"held": True, "state": h["state"], "countdown_s": left,
                "text": text}
