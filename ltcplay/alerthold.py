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
  link, timecode, start, preroll). deck and gate hold quietly.
- Acknowledge cancels the countdown and the announcement once no alert of
  an announcing kind that was open at the Hold is still open (two loud
  alerts and one acknowledged: the other still counts). It does NOT
  resume: the show waits for the operator's own Resume. An Acknowledge
  during the file read stops it from starting (announce.py's `automatic`
  check, in the same locked section that starts the stream); once it has
  started it plays to its end.
- The announcement is made only if the Hold actually took and still
  stands (the scheduler's hold epoch), once per Hold, and journaled as a
  system action. A missing recording or output device: the schedule stays
  held, nothing plays, and the rack screen says why.
- A running show is never held, stopped, disarmed or announced over: the
  scheduler only holds from IDLE or STANDBY, and nothing here ever stops
  or disarms anything. The automatic Delayed asks for no Hold of its own,
  so the safety cuts an operator's announcement makes (lasers disarmed,
  live flame programming ended) never run from a timer: nothing on the
  rig is changed by a timer. No flame or Tier A path is touched.
- Once the operator Resumes (from any Hold) with a showtime due, that
  showtime is never held again for the same open alerts
  (schedule_service remembers night, show and the alert ids); a NEW alert
  at the same showtime holds again.

Pure logic with an injected clock; remote.Remote runs tick() on a worker
thread of its own (never the scheduler's: a slow file read or device
lookup never stalls a tick), with the real clock, together with
alerts.Alerts.tick.
"""
import time

COUNTDOWN_S = 60.0
# "preroll": the rig check before the show failed (preroll.py, Jeff
# 2026-10-28): the audience is waiting, so it is announced like the rest.
ANNOUNCE_KINDS = frozenset(("audio", "video", "flames", "link", "timecode",
                            "start", "preroll"))
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
        """Service.on_alert_hold: the Hold took. Sound the booth now and
        start the countdown when an announcing kind is open."""
        now = self._clock()
        open_ = self._holding()
        loud_ids = {a.id for a in open_ if a.kind in ANNOUNCE_KINDS}
        self.hold = {"epoch": epoch, "why": why, "at": now,
                     "ids": {a.id for a in open_}, "loud": loud_ids,
                     "due": now + COUNTDOWN_S if loud_ids else None,
                     "state": "counting" if loud_ids else "quiet",
                     "note": ""}
        try:
            self.alerts.sound_now()
        except Exception:                        # noqa: BLE001
            pass
        self._journal(
            f"Held at showtime: an alert is open ({why}). The show waits "
            f"for Resume. " + (
                f"If no one acknowledges within {COUNTDOWN_S:g} s, the "
                f"Delayed announcement plays once." if loud_ids else
                "No announcement for this kind of alert."),
            outcome="held")

    # -- the worker thread --------------------------------------------------
    def tick(self):
        h = self.hold
        if h is None:
            return
        if not self._still_held(h):
            self._drop(h)
            return
        if h["state"] != "counting":
            return
        if self._acknowledged(h):
            self._cancel(h)
            return
        if self._clock() < h["due"]:
            return
        # Once per Hold, and only on a Hold that actually took and still
        # stands (checked just above, and again by announce.py right before
        # the stream starts).
        h["state"], h["due"] = "announcing", None
        ann = self._announce()
        if ann is None:
            h["state"] = "failed"
            h["note"] = ("No announcement was played: announcements are not "
                         "running in this engine.")
            self._journal(h["note"], outcome="not played")
            return
        try:
            ann.play(ANNOUNCEMENT, WHO, SCREEN,
                     automatic=lambda: self._may_play(h))
        except Exception as e:                   # noqa: BLE001
            if self._acknowledged(h):
                self._cancel(h)
                return
            if not self._still_held(h):
                self._drop(h)
                return
            h["state"] = "failed"
            h["note"] = (f"The Delayed announcement could not play: {e} "
                         f"The schedule stays held.")
            self._journal(h["note"], outcome="not played")
            return
        h["state"] = "announced"
        self._journal("The Delayed announcement played once: no one "
                      "acknowledged the alert within "
                      f"{COUNTDOWN_S:g} s. The show still waits for Resume.",
                      outcome="announced")

    def stop_for_continue(self, who):
        """Continue anyway was pressed (preroll.py, Jeff 2026-10-28): the
        countdown ends at once and the Delayed announcement this hold
        started, if it is still playing, is stopped; the show is about to
        start. Nothing else changes. Never raises."""
        h = self.hold
        if h is None:
            return
        try:
            if h["state"] == "counting":
                h["state"], h["due"] = "cancelled", None
                self._journal(f"The Delayed announcement was cancelled: "
                              f"{who} chose Continue anyway.",
                              outcome="cancelled")
            elif h["state"] in ("announcing", "announced"):
                ann = self._announce()
                st = ann.status() if ann is not None else {}
                if (st.get("playing") or {}).get("id") == ANNOUNCEMENT:
                    ann.stop(WHO, SCREEN, surface=True)
                    self._journal(f"The Delayed announcement was stopped: "
                                  f"{who} chose Continue anyway.",
                                  outcome="stopped")
        except Exception:                        # noqa: BLE001
            pass

    def _drop(self, h):
        """Forget THIS hold, never a newer one started meanwhile (the
        scheduler's tick inside _still_held can start one)."""
        if self.hold is h:
            self.hold = None

    def _cancel(self, h):
        h["state"], h["due"] = "cancelled", None
        self._journal("The Delayed announcement was cancelled: the alert "
                      "was acknowledged. The show still waits for Resume.",
                      outcome="cancelled")

    def _may_play(self, h):
        """announce.py's `automatic` check: None while the Delayed may
        still start, else why not."""
        if self._acknowledged(h):
            return "the alert was acknowledged."
        if not self._still_held(h):
            return "the schedule is no longer held."
        return None

    def _acknowledged(self, h):
        """No alert of an announcing kind that was open at the Hold is
        open any more."""
        now_ids = {a.id for a in self.alerts.open()}
        return not (h["loud"] & now_ids)

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
        still_open = bool(h["ids"] & {a.id for a in self.alerts.open()})
        left = None
        if h["state"] == "counting":
            left = max(0, int(round(h["due"] - self._clock())))
            text = (f"Held: alert open. The Delayed announcement plays in "
                    f"{left} s unless the alert is acknowledged. The show "
                    f"waits for Resume.")
        elif h["state"] == "announcing":
            text = "Held: alert open. The Delayed announcement is starting."
        elif h["state"] == "announced":
            text = ("Held: the Delayed announcement played. Press Resume "
                    "when ready.")
        elif h["state"] == "failed":
            text = f"Held at showtime. {h['note']}"
        elif h["state"] == "cancelled" or not still_open:
            text = ("Held: the alert was acknowledged, no announcement. "
                    "Press Resume when ready.")
        else:
            text = ("Held: alert open. No announcement for this kind of "
                    "alert. Press Resume when ready.")
        if not still_open and h["state"] in ("quiet", "counting"):
            text = ("Held: the alert was acknowledged. Press Resume when "
                    "ready.")
            left = None
        return {"held": True, "state": h["state"], "countdown_s": left,
                "text": text}
