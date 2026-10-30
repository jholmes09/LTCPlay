"""The fireball timeline on the flame link (Jeff, 2026-10-30): for a show
set to "timeline", the fireball's channels 55 to 59 come from its
timeline (fireball_timeline.py) at the show cue's own time, and the FSEQ's
values on them are ignored.

Where it sits
=============

Inside the flame cue provider (fire_ice.FlameCues), on the frame it has
just read from the show's render, at the very cue time it read it at.
Nothing else changes: the flame link's seek guard, its landing for
Programming moves, Hold, Resume, Stop and Abort (zero and disarm_all),
the lockout, and flamesafe's own rules (it fires only an ARMED group, and
arms only on a clean rising edge) all apply to these values exactly as to
the FSEQ's. Whenever the FSEQ's cues would be zero (no show playing, a
look over the pixels, a fault, past the render's end) the timeline is zero
too: FlameCues never asks this then.

The rules
=========

  - Source per show file. "fseq" (the default, and any show the file does
    not name): the frame is not touched. "timeline": 55, 56, 57, 58 and 59
    are zero from the FSEQ, always, then each stage's slot is ON_VALUE
    while start <= t < start + duration (t the show cue's own time). Purge
    (59) is never set: purging is manual only. A show's source is taken
    when a cue starts and kept for that play of the cue, so a switch made
    during a show applies from the next cue.
  - The timeline file cannot be read: no fireball from any show (55 to
    59 zero for every show), never the FSEQ's values instead.
  - Stages are independent windows, driven exactly like the FSEQ's flame
    cues: after a Resume or a seek, a stage whose window is still open
    comes back under the flame link's own per channel guard, the same as
    an FSEQ cue would. There is no "whole shot" rule.
  - NEVER A DOUBLE CHARGE (Jeff): the Fuel or Air stage turns its channel
    on only while that tank is EMPTY (fireball_charge.py), or while it is
    carrying on the very charge it started (its channel was on in the
    frame that went out just before). Otherwise it is SKIPPED for the rest
    of that play of the cue, said once in the journal and on the screens:
    "Fuel already charged: the Fuel stage was skipped (no double charge)."
    UNKNOWN, or no charge state in this engine, skips it too. A stage cut
    short (Hold, Stop, a seek, the guard) never charges again in that
    play: it counts as charged. The Fireball page's buttons are the
    operator's own decision and are not limited here.
  - Edits during a show (Jeff: editing is by who, never by when): a new
    revision is taken on the next frame. A stage that has already started
    keeps the window it started with. A stage not started yet takes the
    new times, unless its new start is already behind the cue time: then
    it is not started late ("moved into the past: not fired").

The sender's work here is memory only; the file is read on the flame
cues' reading thread (refresh) and when a save is made (publish).
"""
import os
import threading

from . import fireball_timeline as fbtl

ON_VALUE = 255          # fireball.ON_VALUE (ASSUMED, for Jeff and Andy)
SLOTS = fbtl.FIREBALL_FIRE_SLOTS
CHARGE_STAGES = {"fuel": "fuel", "air": "air"}   # stage -> tank
MESSAGES_KEPT = 8


class _Run:
    """One play of one cue (the clock's cue dict, by identity)."""

    def __init__(self, cue_obj, label, entry, rev):
        self.cue_obj = cue_obj
        self.label = label
        self.source = entry["source"] if entry else fbtl.DEFAULT_SOURCE
        self.cue = (entry or {}).get("cue") or ""
        self.rev = None
        self.stages = {}         # stage -> {"slot", "s0", "s1"} (frames)
        self.started = set()     # stages that have had their channel on
        self.skipped = {}        # stage -> why (said once)
        self.prev_on = set()     # slots this provider set ON last frame
        self.sent_tanks = set()  # tanks whose charge went out in this play
        self.adopt(entry, rev, None)

    def adopt(self, entry, rev, relf):
        """A revision's stages: a started stage keeps its window; one not
        started takes the new one, unless (mid-play, relf given) its new
        start is already behind relf. Returns the stages moved into the
        past."""
        self.rev = rev
        past = []
        new = {}
        for st in (entry or {}).get("stages", ()):
            k = st["stage"]
            s0 = fbtl.parse_tc(st["start"])
            new[k] = {"slot": fbtl.STAGE_SLOT[k], "s0": s0,
                      "s1": s0 + st["dur_s"] * fbtl.FPS}
        for k, w in new.items():
            if k in self.started or k in self.skipped:
                continue
            if relf is not None and w["s0"] < relf:
                self.skipped[k] = "moved into the past: not fired"
                past.append(k)
        for k in list(self.stages):
            if k not in new and k not in self.started:
                del self.stages[k]
        for k, w in new.items():
            if k not in self.started:
                self.stages[k] = w
        return past


class FireballCues:
    """The timeline's half of the flame cue provider.

    store       fireball_timeline.Store (the settings folder's file)
    charge_get  () -> fireball_charge.ChargeTracker, or None
    journal     (text, **fields), the flame cues' own (off the sender)"""

    def __init__(self, store, charge_get=lambda: None, journal=None):
        self.store = store
        self._charge_get = charge_get
        self._journal = journal
        self._plan = None         # (rev, error, shows) published
        self._run = None
        self._lock = threading.Lock()
        self.messages = []        # the last few skips, for the screens
        self.fault = ""
        self._said_fault = ""
        if store is not None:
            store.on_saved.append(lambda *_a: self.refresh())

    # -- off the sender ------------------------------------------------------
    def refresh(self):
        """Read the file and publish it (the flame cues' reading thread,
        and a save). Never raises."""
        try:
            doc, err = self.store.load()
            plan = (doc["rev"], err, dict(doc["shows"]))
        except Exception as e:                   # noqa: BLE001
            plan = (None, f"the fireball timeline could not be read "
                          f"({type(e).__name__})", {})
        self._plan = plan
        return plan

    # -- on the sender -------------------------------------------------------
    def _note(self, text, fault=False, outcome="fireball_timeline"):
        if self._journal is not None:
            try:
                self._journal(text, action="flames", outcome=outcome,
                              **({"fault": True} if fault else {}))
            except Exception:                    # noqa: BLE001
                pass

    def _say(self, text):
        self.messages = (self.messages + [text])[-MESSAGES_KEPT:]
        self._note(text, outcome="fireball_skipped")

    def apply(self, out, session, label, rel, cue_obj):
        """`out` (FlameCues' 512 values for this frame, at cue time `rel`
        seconds of cue `label`) with the fireball's slots from the timeline
        for a show set to it. Memory only. Never raises: anything it
        cannot work out is 55 to 59 zero."""
        try:
            return self._apply(out, session, label, rel, cue_obj)
        except Exception as e:                   # noqa: BLE001
            self._fault(f"the fireball timeline failed "
                        f"({type(e).__name__}: {e}); no fireball")
            return self._masked(out)

    @staticmethod
    def _masked(out):
        if out is None:
            return None          # all zeros already (FlameCues' None)
        out = list(out)
        for s in SLOTS:
            out[s - 1] = 0
        return out

    def _fault(self, text):
        self.fault = text
        if text != self._said_fault:
            self._said_fault = text
            self._note(f"Fireball timeline: {text}.", fault=True)

    def _apply(self, out, session, label, rel, cue_obj):
        plan = self._plan
        if plan is None or plan[1]:
            self._fault(plan[1] if plan else "the fireball timeline is not "
                        "read yet")
            return self._masked(out)
        if self.fault:
            self.fault = ""
            self._said_fault = ""
        rev, _err, shows = plan
        show = os.path.basename(str(getattr(session, "timeline_path", "")
                                    or ""))
        entry = shows.get(show)
        relf = int(round(rel * fbtl.FPS))
        run = self._run
        if run is None or run.cue_obj is not cue_obj or run.label != label:
            run = self._run = _Run(cue_obj, label, entry, rev)
            self.messages = []
        if run.source != "timeline":
            return out
        out = self._masked(out)
        if label != run.cue:
            return out
        if rev != run.rev:
            ent = entry if entry is not None and \
                entry.get("source") == "timeline" else \
                dict(entry or {}, stages=[])
            for k in run.adopt(ent, rev, relf):
                self._say(f"{fbtl.STAGE_WORD[k]} was moved into the past by "
                          f"an edit: not fired in this play of the show.")
        tr = self._charge_get()
        last = getattr(tr, "last_sent", b"") if tr is not None else b""
        for k, tank in CHARGE_STAGES.items():
            w = run.stages.get(k)
            if w is not None and w["slot"] in run.prev_on and \
                    len(last) >= w["slot"] and last[w["slot"] - 1] > 0:
                # This stage's charge went out on the wire: from now on
                # it counts as charged in this play, whatever the tank's
                # record says (Jeff: never re-charge after a Resume).
                run.sent_tanks.add(tank)
        now_on = set()
        for k, w in run.stages.items():
            if k in run.skipped:
                continue
            if not w["s0"] <= relf < w["s1"]:
                continue
            slot = w["slot"]
            tank = CHARGE_STAGES.get(k)
            if tank is not None:
                why = self._charge_refusal(tr, tank, slot, run)
                if why:
                    run.skipped[k] = why
                    self._say(why)
                    continue
            out[slot - 1] = ON_VALUE
            now_on.add(slot)
            run.started.add(k)
        run.prev_on = now_on
        return out

    def _charge_refusal(self, tr, tank, slot, run):
        """"" when the Fuel or Air stage may have its channel on this
        frame, else the sentence that skips it."""
        word = fbtl.STAGE_WORD[tank]
        if tr is None:
            return (f"{word}: the charge state is not running in this engine,"
                    f" so the {word} stage was skipped (no double charge).")
        last = getattr(tr, "last_sent", b"")
        carrying_on = slot in run.prev_on and len(last) >= slot and \
            last[slot - 1] > 0
        if carrying_on:
            return ""
        state = tr.state(tank)
        if tank in run.sent_tanks:
            return (f"{word} already charged in this show: the {word} stage "
                    f"was skipped (no double charge).")
        if state == "empty":
            return ""
        if state == "charged":
            return (f"{word} already charged: the {word} stage was skipped "
                    f"(no double charge).")
        return (f"{word} tank state not known: the {word} stage was skipped "
                f"(no double charge). Purge or fire to reset it.")

    def view(self):
        """For the screens: the fault, and the last skips of this play."""
        run = self._run
        return {"fault": self.fault, "messages": list(self.messages),
                "source": run.source if run is not None else None,
                "rev": self._plan[0] if self._plan else None}

