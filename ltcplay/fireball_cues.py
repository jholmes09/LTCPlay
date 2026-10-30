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
  - WRITE FIRST (round-5 review): a new automatic charge (the Fuel or
    Air stage, or the FSEQ's guarded 55 or 56, from an EMPTY tank) goes
    out only once the charge record says, on disk, that one may be going
    out (ChargeTracker.charge_ready): while that write is on its way the
    charge is held off frame by frame (asked for as a timeline play
    starts, so it is long done by the first stage); if it cannot be
    written the charge is skipped and said: "the charge record cannot be
    saved: automatic charging is off".
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
import time

from . import fireball_timeline as fbtl

ON_VALUE = 255          # fireball.ON_VALUE (ASSUMED, for Jeff and Andy)
SLOTS = fbtl.FIREBALL_FIRE_SLOTS
CHARGE_STAGES = {"fuel": "fuel", "air": "air"}   # stage -> tank
CHARGE_SLOT = {"fuel": 55, "air": 56}            # tank -> slot
PURGE_SLOT = fbtl.PURGE_SLOT
MESSAGES_KEPT = 8
# _charge_refusal's answer while the charge record's write-ahead mark is
# on its way to disk: this frame holds the charge off, nothing is skipped.
WAIT = "\0wait"
# A charge that went out and then stopped for no longer than this (one
# frame the flame cues could not read, one send that failed, a lockout or
# a Hold undone at once) carries on as the same charge, inside the same
# window, so its total time on never exceeds the window (Jeff accepts
# this, round-4 review); a longer stop (Hold, Stop, a seek, the guard)
# ends it for the rest of that play, said on the screens and in the
# journal (review of #187).
GAP_S = 0.25


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
        self.sent_at = {}        # tank -> clock when last seen on the wire
        self.last_call = None    # clock of the last frame asked of us
        self.fseq_on = {}        # FSEQ show: tank -> its charge allowed
        # The play's first frame (a start, or a landing): a charge held off
        # there while the write-ahead mark was on its way never starts
        # late, as the flame link's landing guard would have held it.
        self.first = True
        self.first_relf = None
        self.fseq_hold = set()   # FSEQ charges on at the landing, held
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
                old = self.stages.get(k)
                self.skipped[k] = "behind the play position: not fired"
                # Said as an edit only when the edit moved it (a stage
                # behind a Play from that an unrelated edit re-read is
                # not "moved by an edit": review of #187).
                if old is None or old["s0"] != w["s0"]:
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

    def __init__(self, store, charge_get=lambda: None, journal=None,
                 clock=time.monotonic, sent_get=None, group_get=None):
        self.store = store
        self._charge_get = charge_get
        self._journal = journal
        self._clock = clock
        # What the SHOW path last sent (512 values), before anything else
        # is merged into the frame (the Fireball page's own frame, later).
        # "Carrying on its own charge" is judged from this alone (review
        # D-F1: Andy's top-off on the composed frame read as the show's
        # charge still on). None: the charge state's last frame.
        self._sent_get = sent_get
        # flamesafe's fireball group's fire slots (off the sender, read in
        # refresh): the channels this module drives must be them.
        self._group_get = group_get
        self._group = None
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
        if self._group_get is not None:
            try:
                g = self._group_get()
                self._group = sorted(g) if g is not None else None
            except Exception:                    # noqa: BLE001
                self._group = None
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

    def _say(self, text, fault=False):
        self.messages = (self.messages + [text])[-MESSAGES_KEPT:]
        self._note(text, fault=fault, outcome="fireball_skipped")

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

    def _sent(self, tr):
        """The show path's last sent values (see __init__)."""
        if self._sent_get is not None:
            try:
                v = self._sent_get()
                return bytes(v) if v is not None else b""
            except Exception:                    # noqa: BLE001
                return b""
        return getattr(tr, "last_sent", b"") if tr is not None else b""

    def _gap_check(self, run):
        """Asked again after more than GAP_S (a Hold, a look over the
        show, a stalled timecode, a read fault): nothing of ours was on
        meanwhile, so nothing carries on (review D-F1)."""
        now = self._clock()
        if run.last_call is not None and now - run.last_call > GAP_S:
            run.prev_on = set()
        run.last_call = now

    def _apply(self, out, session, label, rel, cue_obj):
        group = self._group
        if group is not None and group != list(SLOTS):
            # flamesafe's fireball is not on 55 to 59 (review C-4): what
            # this module would drive is not the fireball. Fail closed:
            # the fireball group's own slots are zero from every show.
            self._fault(f"flamesafe's fireball group is on {group}, not "
                        f"55 to 59, so no show fires its fireball until it "
                        f"is (MORNING.md, The fireball)")
            if out is None:
                return None
            out = list(out)
            for s in group:
                if 1 <= s <= len(out):
                    out[s - 1] = 0
            return out
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
        if entry is None:
            # A case-blind folder (Windows, macOS): the file's entry under
            # another spelling of the same name is this show's (review of
            # #187: "show.json" saved, "Show.json" played, FSEQ fallback).
            same = [k for k in shows if fbtl.show_key(k) == fbtl.show_key(
                show)]
            if len(same) > 1:
                self._fault(f"{show} has a fireball timeline under more "
                            f"than one spelling ({', '.join(sorted(same))}); "
                            f"no fireball for it")
                return self._masked(out)
            if same:
                entry = shows[same[0]]
        relf = int(round(rel * fbtl.FPS))
        run = self._run
        if run is None or run.cue_obj is not cue_obj or run.label != label:
            run = self._run = _Run(cue_obj, label, entry, rev)
            self.messages = []
            if run.source == "timeline" and any(
                    k in run.stages for k in CHARGE_STAGES):
                # The write-ahead mark asked for as the play starts, so it
                # is on disk long before the first charge stage.
                self._ready(self._charge_get())
        self._gap_check(run)
        first, run.first = run.first, False
        if first:
            run.first_relf = relf
        if run.source != "timeline":
            # A landing (the play's first frame past the cue's top), not a
            # start from the top.
            return self._fseq_guarded(out, run, first and relf > 1)
        out = self._masked(out)
        if label != run.cue:
            return out
        if rev != run.rev:
            if entry is None or entry.get("source") != "timeline":
                # Switched to the FSEQ (or its entry removed) during the
                # play: that applies from the next cue; this play keeps
                # its own stages to the end (round-4 review, s40: dropping
                # them left Fuel charged with no shot).
                run.rev = rev
            else:
                for k in run.adopt(entry, rev, relf):
                    self._say(f"{fbtl.STAGE_WORD[k]} was moved into the "
                              f"past by an edit: not fired in this play of "
                              f"the show.")
        tr = self._charge_get()
        last = self._sent(tr)
        now = self._clock()
        for k, tank in CHARGE_STAGES.items():
            w = run.stages.get(k)
            if w is not None and w["slot"] in run.prev_on and \
                    len(last) >= w["slot"] and last[w["slot"] - 1] > 0:
                # This stage's charge went out on the wire: from now on
                # it counts as charged in this play, whatever the tank's
                # record says (Jeff: never re-charge after a Resume).
                run.sent_tanks.add(tank)
                run.sent_at[tank] = now
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
                if why == WAIT:
                    if run.first_relf is not None and \
                            w["s0"] < run.first_relf:
                        # The play landed inside this window: it stays
                        # quiet, as the landing guard keeps a cue on at a
                        # landing (never a late start past the guard).
                        run.skipped[k] = "landed inside its window"
                    continue        # the mark is on its way: next frame
                if why:
                    run.skipped[k] = why
                    self._say(why, fault=self._unsaved(why))
                    continue
            out[slot - 1] = ON_VALUE
            now_on.add(slot)
            run.started.add(k)
        run.prev_on = now_on
        return out

    def _charge_refusal(self, tr, tank, slot, run, what=None):
        """"" when the Fuel or Air stage (or, `what` given, the FSEQ's
        own charge) may have its channel on this frame, else the sentence
        that skips it."""
        word = fbtl.STAGE_WORD[tank]
        fseq = what is not None
        what = what or f"the {word} stage"
        if tr is None:
            return (f"{word}: the charge state is not running in this engine,"
                    f" so {what} was skipped (no double charge).")
        last = self._sent(tr)
        carrying_on = slot in run.prev_on and len(last) >= slot and \
            last[slot - 1] > 0
        if carrying_on:
            return ""
        state = tr.state(tank)
        if fseq and state == "empty":
            # An FSEQ show may charge, fire and charge again: once the
            # shot (or a Purge) has emptied the tank its next charge is a
            # new one.
            run.sent_tanks.discard(tank)
        if tank in run.sent_tanks:
            at = run.sent_at.get(tank)
            if at is not None and 0 <= self._clock() - at <= GAP_S:
                # A short gap (one frame not read or not sent): the same
                # charge carries on; the charge state is told, so the
                # edge is not a double charge.
                tell = getattr(tr, "note_continuing", None)
                if tell is not None:
                    tell(tank)
                return ""
            return (f"{word} charge cut short (it stopped for more than "
                    f"{GAP_S:g} s): {what} is not charged again in this "
                    f"play (no double charge).")
        if state == "empty":
            # A new automatic charge: only once the charge record says, on
            # disk, that one may be going out (round-5 review, write-ahead).
            r = self._ready(tr)
            if r == "ok":
                return ""
            if r == "wait":
                return WAIT
            return (f"{word}: the charge record cannot be saved: automatic "
                    f"charging is off, so {what} was skipped (no double "
                    f"charge).")
        if state == "charged":
            return (f"{word} already charged: {what} was skipped "
                    f"(no double charge).")
        return (f"{word} tank state not known: {what} was skipped "
                f"(no double charge). Purge or fire to reset it.")

    @staticmethod
    def _ready(tr):
        """The charge record's write-ahead mark: "ok", "wait" or "failed"
        (fireball_charge.ChargeTracker.charge_ready). A charge state
        without it (an older one) is "ok". Never raises."""
        ask = getattr(tr, "charge_ready", None)
        if ask is None:
            return "ok"
        try:
            r = ask()
        except Exception:                        # noqa: BLE001
            return "failed"
        return r if r in ("ok", "wait") else "failed"

    @staticmethod
    def _unsaved(why):
        return "charge record cannot be saved" in why

    def _fseq_guarded(self, out, run, landed=False):
        """A show on its FSEQ (review of #187; Jeff: never a double charge
        under any circumstance, Purge by hand only): Purge (59) from the
        FSEQ is always zero, and the FSEQ's Fuel (55) and Air (56) charges
        go out only under the same rule as the timeline's (the tank EMPTY,
        or carrying on the charge it started). Valve Open and Igniter, and
        every other slot, are exactly as read."""
        if out is None:
            return None
        out = list(out)
        out[PURGE_SLOT - 1] = 0
        tr = self._charge_get()
        last = self._sent(tr)
        now = self._clock()
        on_now = set()
        for tank, slot in CHARGE_SLOT.items():
            if slot in run.prev_on and len(last) >= slot and \
                    last[slot - 1] > 0:
                run.sent_tanks.add(tank)
                run.sent_at[tank] = now
            if out[slot - 1] <= 0:
                # The FSEQ itself turned this charge off (review C-2): it
                # has ended. Only frames the engine failed to read or send
                # ride through a short gap; a new FSEQ charge is judged by
                # the tank alone (EMPTY again after a shot or a Purge).
                run.fseq_on.pop(tank, None)
                run.sent_at.pop(tank, None)
                run.sent_tanks.discard(tank)
                run.fseq_hold.discard(tank)
                continue
            if tank in run.fseq_hold:
                out[slot - 1] = 0   # on at the landing: held until it ends
                continue
            word = fbtl.STAGE_WORD[tank]
            why = self._charge_refusal(tr, tank, slot, run,
                                       what=f"the FSEQ's {word} Charge")
            if why == WAIT:
                out[slot - 1] = 0   # the mark is on its way: next frame
                if landed:
                    # On at a landing inside this charge: held until the
                    # FSEQ turns it off.
                    run.fseq_hold.add(tank)
                continue
            if why:
                out[slot - 1] = 0
                if run.fseq_on.get(tank) is not False:
                    self._say(why, fault=self._unsaved(why))
                run.fseq_on[tank] = False
                continue
            run.fseq_on[tank] = True
            on_now.add(slot)
        run.prev_on = on_now
        return out

    def view(self):
        """For the screens: the fault, and the last skips of this play."""
        run = self._run
        return {"fault": self.fault, "messages": list(self.messages),
                "source": run.source if run is not None else None,
                "rev": self._plan[0] if self._plan else None}

