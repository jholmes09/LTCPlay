"""The Fireball page: manual control of each of the fireball's channels
(Jeff, 2026-10-28; design by Andy). OPERATOR.md, "The Fireball page", says
it in plain words.

What it is
==========

A rack screen page, shown when flamesafe's config has a group named
"fireball", with one button per fire slot of that group, by channel
number, lowest first (the group must list exactly five):

    1  Fuel Charge   TOGGLE     on until pressed again
    2  Air Charge    TOGGLE
    3  Valve Open    MOMENTARY  on only while a finger holds it
    4  Igniter       MOMENTARY
    5  Purge         TOGGLE

The slot numbers come from flamesafe's config, read as plain JSON by the
Startup page (morning.load_flamesafe_groups, the wall: ltcplay never
imports flamesafe). Nothing here names a slot number.

The path (nothing new reaches the rig)
======================================

The values go out on the engine's own flame link as the Fireball page's
own frame (FlameLink.manual_set, MANUAL_HOLD_S; Jeff, 2026-10-30: "Andy
may manually start any phase with toggle buttons at any time, including
during a show"). The link carries it ONLY on the fireball group's fire
slots (set_manual_slots, from build), whether or not a show is live, and
on each of those slots sends the HIGHER of the show's own value (its FSEQ,
or its fireball timeline: fireball_cues.py) and this page's, so neither
can turn the other's channel off. flamesafe stays the only program that
writes the flame universe. It sends a fire value only for a group that is
ARMED, and only an operator arms it, by the normal hold to arm (the rack
screen's button or the Stream Deck's key). This page never arms anything
and has no path to the arm link.

A charge pressed here while the tank is already charged is a top-off by
that person, not a double charge (Jeff): the charge state
(fireball_charge.py) counts it as one, and the timeline then skips its own
charge of that tank for the rest of the show.

The rules, each a refusal or a cut
==================================

  - Nothing turns on unless flamesafe reports the fireball group ARMED in
    a status under STATUS_FRESH_S old, and nothing is sent for a group
    that is not: the frame carries zeros on every fireball slot until it
    is armed. So no value is ever up on the arm tick, and flamesafe's
    clean rising edge rule (every fire slot under 15) always holds.
  - The page works during a show (a scheduled show playing, the show
    clock playing a cue, Run pressed, Programming playback) and through a
    Hold (Jeff, 2026-10-30: a Hold zeroes the show's own values, never the
    page's). It is unavailable while: an Abort is standing, live flame programming is on, a purge is selected
    or firing on the Startup page, any screen is in Arrange mode, the lock
    state is not trusted, the flame link is not sending. Each says why.
  - The Startup page's purge rules: purge_arming on, and from the show
    network screen_arming too; the page's status under a second old; a
    machine locked out never fires (any fireball slot locked: nothing
    turns on). Signed out is view only (users.classify: any control ON is
    an "arm" press; everything that turns off is safety).
  - One run at a time: the page names a new run each time something goes
    on from everything off, and numbers its beats (an older beat that
    arrives late is not used). A run belongs to the device and window
    that started it. A beat from a run that has ended (cut, or turned all
    off) is refused and the page turns everything off: nothing ever comes
    back on by itself. Press again.
  - The page beats every 100 ms while anything is on. A run with no beat
    for BEAT_STALE_S is cut (this module's watcher, every WATCH_S), and
    the flame link drops the frame by itself PURGE_HOLD_S after the last
    beat whatever this module does.
  - CUT, all to zero at once, on: the fireball group seen not armed (a
    disarm, a lockout, flamesafe holding it), flamesafe's status stale,
    the flame link disarmed (disarm_all() is Stop and Abort: the link's
    own disarm count says so; a Hold or a show ending does not), Abort
    (Morning.on_abort), any blocker above appearing, a refused beat, a
    stale heartbeat, the page leaving, hidden or closed, its status gone
    stale, sign out (the beat is refused), and All off from any screen.
  - A momentary is ON only while the page reports it held. The page
    releases it on lift, pointer cancel, losing the pointer, blur, the
    page hidden or closed, and every cut. Toggles are cut by everything
    in CUT above, page close and sign out included.
"""
import collections
import threading
import time

GROUP_NAME = "fireball"
# (key, label, kind), one per fire slot of the group, lowest channel
# first (Jeff's: 55, 56, 57, 58, 59).
CONTROLS = (("fuel", "Fuel Charge", "toggle"),
            ("air", "Air Charge", "toggle"),
            ("valve", "Valve Open", "momentary"),
            ("igniter", "Igniter", "momentary"),
            ("purge", "Purge", "toggle"))
KEYS = tuple(k for k, _l, _k in CONTROLS)
TOGGLES = tuple(k for k, _l, kind in CONTROLS if kind == "toggle")
MOMENTARY = tuple(k for k, _l, kind in CONTROLS if kind == "momentary")
# The value a channel carries while it is ON. A G-Flame fires at 229 or
# more (flamesafe/rules.py GFLAME_FIRE_AT, "at least 90%"); the Startup
# page's purge fires a G-Flame at 255 (morning.PURGE_DEFAULTS). 255 for
# every channel, the Valve Open (not a G-Flame channel) included: ASSUMED,
# for Jeff and Andy to confirm.
ON_VALUE = 255
UNIVERSE_SIZE = 512
BEAT_STALE_S = 0.25          # the same as flamelink.PURGE_HOLD_S
STATUS_FRESH_S = 0.5         # flamesafe's status, as arm_stale_ms
PAGE_FRESH_S = 1.0           # the page's own status (seen)
WATCH_S = 0.05
DEAD_RUNS = 64               # ended runs remembered per screen
OWNERS_KEPT = 64             # screens remembered (only ones that ran)


def find_group(groups):
    """({"name", "index", "safety", "fire"}, "") for the group named
    fireball (any case, spaces around ignored) in flamesafe's groups
    (morning.load_flamesafe_groups), or (None, why) in a sentence."""
    if not groups:
        return None, ("flamesafe's config could not be read, so there is no "
                      "fireball group to show.")
    for i, g in enumerate(groups):
        if str(g.get("name", "")).strip().lower() != GROUP_NAME:
            continue
        fire = list(g.get("fire") or [])
        saf = list(g.get("safeties") or [])
        if len(saf) != 1:
            return None, (f"The fireball group in flamesafe's config has "
                          f"{len(saf)} safety slots; this page needs exactly "
                          f"one.")
        if len(fire) != len(CONTROLS) or len(set(fire)) != len(fire) or \
                not all(isinstance(s, int) and 1 <= s <= UNIVERSE_SIZE
                        for s in fire):
            return None, (f"Fireball page unavailable: flamesafe.json "
                          f"fireball group must list five fire slots (five "
                          f"different slots 1 to 512; it lists "
                          f"{len(fire)}).")
        # By channel number, lowest first, whatever order the file lists
        # them in (review of #149, P2): Fuel Charge, Air Charge, Valve
        # Open, Igniter, Purge (Jeff's 55, 56, 57, 58, 59).
        return {"name": g["name"], "index": i, "safety": saf[0],
                "fire": sorted(fire)}, ""
    return None, ("flamesafe's config has no group named fireball, so this "
                  "page has nothing to control.")


def controls_on(body):
    """The control keys a beat's body asks to be ON: only the JSON word
    true counts; anything else, and any key this page does not have, is
    off. users.fireball_on_wanted() is the same test for the role check."""
    c = body.get("controls") if isinstance(body, dict) else None
    if not isinstance(c, dict):
        return set()
    return {k for k in KEYS if c.get(k) is True}


class FireballPanel:
    """The engine side of the Fireball page. Pure apart from the providers
    it is built with:

      link_get()      the flame link, or None
      flames_get()    the remote's FlameStatus view, or None
      blockers()      why nothing may turn on right now (everything but
                      the per-press checks), or ""
      locked_slots()  the fire slots the lockout table holds
    """

    def __init__(self, group, problem="", link_get=lambda: None,
                 flames_get=lambda: None, blockers=lambda: "",
                 locked_slots=lambda: set(), journal=None,
                 clock=time.monotonic, threaded=True):
        self.group = group
        self.problem = problem
        self._link = link_get
        self._flames = flames_get
        self._blockers = blockers
        self._locked = locked_slots
        self._journal = journal
        self._clock = clock
        self._lock = threading.Lock()
        self._run = None        # {"id", "owner", "on", "beat", "gen",
        #                          "start"} while a run is live
        # Per screen that has started a run (review of #149, P3-2): its
        # ended run ids and the highest beat number seen from it. Only a
        # screen's own beats touch its record, and a record is made only
        # by starting a run (an arm press), so no other screen's beats can
        # push a real ended run out.
        self._ended = collections.OrderedDict()
        self.runs = 0
        self.last_cut = ""
        # (on keys, who, screen): told what this page holds ON after each
        # beat that went in, and set() on every cut, so the fireball's
        # charge state (fireball_charge.py) can name who charged or topped
        # off. Display and journal only: it never changes what is sent.
        self.on_controls = None
        self._stop = threading.Event()
        self._thread = None
        self._threaded = threaded
        if threaded and group is not None:
            self._thread = threading.Thread(target=self._watch, daemon=True,
                                            name="ltcplay-fireball-watch")
            self._thread.start()

    # -- plumbing ------------------------------------------------------------
    def _note(self, text, outcome="done", who="", screen="", fault=False):
        if self._journal is None:
            return
        try:
            self._journal(text, outcome=outcome, who=who, screen=screen,
                          fault=fault)
        except Exception:                                # noqa: BLE001
            pass

    def _tell(self, on, who, screen):
        tell = self.on_controls
        if tell is not None:
            try:
                tell(set(on), who, screen)
            except Exception:                            # noqa: BLE001
                pass

    @property
    def available(self):
        return self.group is not None

    def _gen(self, link):
        """The flame link's disarm count (abort_id): every disarm_all()
        (Stop, Abort) bumps it; a zero() (Hold, a show ending) does not.
        None when it cannot be read."""
        try:
            # The disarm count (Stop, Abort): a Hold or a show's end does
            # not end the page's channels (Jeff, 2026-10-30).
            g = getattr(link, "abort_id")
            return g if isinstance(g, int) else None
        except Exception:                                # noqa: BLE001
            return None

    @staticmethod
    def _still(still):
        """Why the run's sign-in may no longer hold a channel, or ""."""
        if still is None:
            return ""
        try:
            return str(still() or "")
        except Exception:                                # noqa: BLE001
            return "the sign-in could not be checked"

    def cut_signed_out(self, why, token="", user="", device="",
                       local=False, who="", screen=""):
        """A sign-out that ended a session (round-5 review F5): the run is
        cut if that sign-in started it (its token), or that user started it
        on that screen, or, on the show machine itself (every sign-in made
        there ended), it was started there. Another screen's run by
        someone else is never cut. True when a run was cut."""
        with self._lock:
            r = self._run
            hit = r is not None and (
                (token and r.get("token") == token) or
                (user and r.get("who") == user and r["owner"][0] == device)
                or (local and r.get("local")))
        if not hit:
            return False
        return self.cut(why, who=who, screen=screen)

    @staticmethod
    def _drops(link):
        """The flame link's count of frames it could not send, or None."""
        d = getattr(link, "manual_drops", None)
        return d if isinstance(d, int) and not isinstance(d, bool) else None

    def _link_why(self, link, r):
        """Why the run `r` may not carry on, from the flame link: a
        disarm_all() (Stop, Abort) since it started, or a frame that could
        not be sent since (round-4 review, s95: a 300 ms outage, and
        Andy's Fuel came back by itself when the link did). Else ""."""
        if link is None or self._gen(link) != r["gen"]:
            return ("the flame link was disarmed (Stop, Abort or a disarm "
                    "through it)")
        if self._drops(link) != r.get("drops"):
            return "a frame to flamesafe could not be sent"
        return ""

    def armed_state(self):
        """("armed" | "off" | "unknown", sentence) for the fireball group
        from flamesafe's status: "armed" only when a status under
        STATUS_FRESH_S old says ARMED."""
        try:
            fl = self._flames() or {}
        except Exception:                                # noqa: BLE001
            fl = {}
        age = fl.get("age_ms")
        if not fl or fl.get("stale") or not fl.get("connected") or \
                not isinstance(age, (int, float)) or \
                age > STATUS_FRESH_S * 1000:
            return "unknown", ("flamesafe has not reported in the last half "
                               "second, so whether the Fireball is armed is "
                               "not known.")
        for g in fl.get("groups") or []:
            if g.get("name") == self.group["name"]:
                if g.get("armed") == "armed":
                    return "armed", ""
                return "off", ("The Fireball is not armed. Arm it with its "
                               "hold to arm (the show screen's Fireball "
                               "button or its Stream Deck key).")
        return "unknown", ("flamesafe's status does not list the fireball "
                           "group.")

    def refusal(self, check_armed=True):
        """Why nothing may turn on right now, or ""."""
        if self.group is None:
            return self.problem or "There is no fireball group."
        try:
            why = self._blockers() or ""
        except Exception as e:                           # noqa: BLE001
            why = (f"the engine's state could not be read "
                   f"({type(e).__name__}).")
        if why:
            return why
        if self._link() is None:
            return ("No flame link is connected in this engine, so nothing "
                    "could reach the Fireball.")
        try:
            locked = set(self._locked() or ())
        except Exception as e:                           # noqa: BLE001
            return (f"the lock state could not be read "
                    f"({type(e).__name__}).")
        hit = sorted(locked & set(self.group["fire"]))
        if hit:
            return (f"The Fireball is locked out (slots {hit}). Unlock it on "
                    f"the Startup page first.")
        if check_armed:
            state, why = self.armed_state()
            if state != "armed":
                return why
        return ""

    def frame(self, on):
        """The 512 slot frame for the controls in `on`: ON_VALUE on each
        one's fire slot, zero everywhere else, the safety slot included
        (flamesafe writes it from its arm state, never from this)."""
        vals = bytearray(UNIVERSE_SIZE)
        if self.group is None:
            return bytes(vals)
        for (key, _l, _k), slot in zip(CONTROLS, self.group["fire"]):
            if key in on:
                vals[slot - 1] = ON_VALUE
        return bytes(vals)

    # -- the routes ------------------------------------------------------------
    def beat(self, body, owner, seen_ok=True, may=(True, ""), who="",
             screen="", token="", local=False, still=None):
        """One heartbeat from the page (every 100 ms while anything is on):
        (code, body). `owner` is (device, window); `may` (ok, why) is the
        purge arming switch for this surface. The body names its run (the
        page makes a new one for each press that starts from everything
        off) and numbers its beats (seq). `token` and `local` are the
        sign-in that pressed (its run is cut when that sign-in ends:
        cut_signed_out); `still` () -> why that sign-in may no longer hold
        a channel (signed out, demoted, removed, the users file unreadable)
        or "": asked as a run starts and by the watcher (round-5
        reviews)."""
        body = body if isinstance(body, dict) else {}
        on = controls_on(body)
        rid = body.get("run")
        rid = rid if isinstance(rid, str) and 0 < len(rid) <= 64 else None
        seq = body.get("seq")
        seq = seq if isinstance(seq, int) and not isinstance(seq, bool) \
            else None
        now = self._clock()
        if not on:
            # Everything off: ends this owner's run, and that run can never
            # start again (a beat of it that comes late is refused). Never
            # refused itself.
            with self._lock:
                r = self._run
                mine = r is not None and r["owner"] == owner and \
                    (rid is None or rid == r["id"])
                self._remember(owner, rid, seq)
            if mine:
                self.cut(f"{owner[0]} turned everything off",
                         who=who, screen=screen, outcome="off")
            return 200, {"ok": True, "on": [], "run": None}
        if rid is None:
            return 400, {"error": "A beat that turns something on has to "
                                  "name its run. Nothing was turned on.",
                         "cut": True}
        # The zero count is read BEFORE the checks (review of #193, 5): a
        # Stop or Abort that lands after them moves it, so the frame
        # this beat would set is refused at the link (manual_set's gen).
        link0 = self._link()
        gen0 = self._gen(link0) if link0 is not None else None
        why = ""
        if not seen_ok:
            why = ("The page's status is more than 1 s old. Wait for it to "
                   "be live, then press again.")
        elif not may[0]:
            why = may[1]
        if not why:
            why = self.refusal()
        link = self._link()
        if why or link is None:
            why = why or "No flame link."
            with self._lock:
                r = self._run
                mine = r is not None and r["owner"] == owner
            if mine:
                self.cut(f"refused: {why}", who=who, screen=screen)
            return 409, {"error": why, "cut": True}
        with self._lock:
            r = self._run
            stale = r is not None and now - r["beat"] > BEAT_STALE_S
        if stale:
            self.cut(f"no heartbeat for {BEAT_STALE_S:g} s", who=who,
                     screen=screen)
        started = False
        with self._lock:
            r = self._run
            if r is not None and r["owner"] != owner:
                return 409, {"error": f"{r['owner'][0]} is using the "
                                      f"Fireball controls. One screen at a "
                                      f"time.", "cut": True}
            if self._is_dead(owner, rid, seq, r):
                return 409, {"error": "Everything was turned off "
                                      f"({self.last_cut or 'the run ended'}). "
                                      "Press again.", "cut": True}
            if r is not None and r["id"] != rid:
                # A new press from this same screen while its last run is
                # still live (its all off is still on the way): the new one
                # takes over, the old one can never come back.
                self._remember(owner, r["id"], None)
                r = self._run = None
            if r is None:
                gen = gen0 if link is link0 else None
                if gen is None:
                    return 409, {"error": "The flame link's state could not "
                                          "be read, so nothing turns on.",
                                 "cut": True}
                # The sign-in checked again as the run starts (round-5
                # review F6: a press that raced a sign-out started a run
                # for up to 0.25 s).
                gone = self._still(still)
                if gone:
                    return 409, {"error": f"Nothing was turned on: {gone}.",
                                 "cut": True}
                r = self._run = {"id": rid, "owner": owner, "on": set(),
                                 "beat": now, "gen": gen, "start": now,
                                 "seq": seq, "drops": self._drops(link0),
                                 "who": who, "token": token or "",
                                 "local": bool(local), "still": still}
                self._remember(owner, None, seq, make=True)
                self.runs += 1
                started = True
            elif seq is not None and r["seq"] is not None and \
                    seq <= r["seq"]:
                # Overtaken by a later beat (separate connections): it says
                # nothing about what is held now, and is not used.
                return 200, {"ok": True, "run": r["id"], "overtaken": True,
                             "on": [k for k in KEYS if k in r["on"]]}
            else:
                r["seq"] = seq
            before = set(r["on"])
            r["on"] = set(on)
            r["beat"] = now
        vals = self.frame(on)
        with self._lock:
            if self._run is r and seq is not None and r["seq"] != seq:
                # A later beat of this run went past this one while its
                # frame was worked out (review of #149, P3-1): that one's
                # frame stands, this one is not written.
                return 200, {"ok": True, "run": r["id"], "overtaken": True,
                             "on": [k for k in KEYS if k in r["on"]]}
            if self._run is not r:
                # A cut that landed while this beat was worked out wins:
                # nothing is set.
                lost = True
            else:
                # The charge state is told who holds what BEFORE the frame
                # can go out (review of #193, 1): the link's sender may
                # send it the moment it is set, and a charge it sees then
                # must already be this person's (a top-off, never a
                # double charge).
                self._tell(on, who, screen)
                # Refused by the link itself when a disarm_all() (Stop,
                # Abort) has moved its disarm count since the run started
                # (a zero(), a Hold or a show's end, does not: Jeff,
                # 2026-10-30).
                took = link.manual_set(vals, r["gen"], r["drops"]) \
                    if r["drops"] is not None else \
                    link.manual_set(vals, r["gen"])
                # A disarm_all() that landed just after the frame went in,
                # or a frame that could not be sent since the run started:
                # the frame comes straight out again. One that lands later
                # clears the frame itself.
                lost = took is False or self._link_why(link, r)
        if lost:
            if self._run is r or self._run is None:
                link.manual_clear()
            if self._run is r:
                self.cut(self._link_why(link, r) or
                         "the flame link was disarmed (Stop, Abort or a "
                         "disarm through it)", who=who, screen=screen)
            return 409, {"error": "Everything was turned off: "
                                  f"{self.last_cut or 'the run ended'}. "
                                  "Press again.", "cut": True}
        # Belt: after the frame went in, the group must still read armed.
        state, why = self.armed_state()
        if state != "armed":
            self.cut(f"the Fireball is not armed: {why}", who=who,
                     screen=screen)
            return 409, {"error": why, "cut": True}
        if started or before != set(on):
            names = [lb for k, lb, _t in CONTROLS if k in on]
            self._note(f"{owner[0]} on the Fireball page: "
                       f"{', '.join(names)} ON"
                       f"{' (a new run)' if started else ''}.",
                       outcome="on", who=who, screen=screen)
        return 200, {"ok": True, "run": r["id"],
                     "on": [k for k in KEYS if k in on]}

    def _remember(self, owner, rid, seq, make=False):
        """Note an ended run id and the highest beat number of `owner`.
        Under self._lock. A record is made only with `make` (a run
        starting); otherwise only an existing one is updated."""
        e = self._ended.get(owner)
        if e is None:
            if not make:
                return
            e = self._ended[owner] = {"ids": collections.deque(
                maxlen=DEAD_RUNS), "seq": None}
            while len(self._ended) > OWNERS_KEPT:
                self._ended.popitem(last=False)
        self._ended.move_to_end(owner)
        if rid is not None and rid not in e["ids"]:
            e["ids"].append(rid)
        if seq is not None and (e["seq"] is None or seq > e["seq"]):
            e["seq"] = seq

    def _is_dead(self, owner, rid, seq, live):
        """True for a beat that may not start or carry on a run: its run
        id ended, or (starting a run) its beat number is not past every
        beat this screen has sent before. Under self._lock."""
        e = self._ended.get(owner)
        if e is None:
            return False
        if rid in e["ids"]:
            return True
        starting = live is None or live["id"] != rid
        return starting and seq is not None and e["seq"] is not None and \
            seq <= e["seq"]

    def cut(self, why, who="", screen="", outcome="cut"):
        """Everything off now: the run ends, the flame link's frame is
        cleared at once (not at its own expiry), and a beat from that run
        is refused from now on. True when a run was live."""
        with self._lock:
            r, self._run = self._run, None
            if r is not None:
                self.last_cut = why
                self._remember(r["owner"], r["id"], r["seq"])
        link = self._link()
        if r is not None and link is not None:
            try:
                link.manual_clear()
            except Exception:                            # noqa: BLE001
                pass
        self._tell(set(), who, screen)
        if r is not None:
            held = self._clock() - r["start"]
            self._note(f"Fireball page: everything off after {held:.1f} s: "
                       f"{why}.", outcome=outcome, who=who, screen=screen)
        return r is not None

    def off(self, why, who="", screen=""):
        """All off, from any screen (safety: never refused)."""
        self.cut(why, who=who, screen=screen, outcome="off")
        return 200, {"ok": True, "on": [], "run": None,
                     "text": "Everything on the Fireball is off."}

    # -- the watcher -----------------------------------------------------------
    def tick(self, now=None):
        """Every WATCH_S: a live run with no beat for BEAT_STALE_S, the
        group not armed, flamesafe stale, the flame link disarmed (Stop,
        Abort) or unable to send a frame, or any blocker is cut. A Hold or
        a show's end does not cut it (Jeff, 2026-10-30)."""
        now = self._clock() if now is None else now
        with self._lock:
            r = self._run
        if r is None:
            return ""
        why = ""
        if now - r["beat"] > BEAT_STALE_S:
            why = f"no heartbeat for {BEAT_STALE_S:g} s"
        else:
            why = self._link_why(self._link(), r) or self.refusal() or \
                self._still(r.get("still"))
        if why:
            with self._lock:
                same = self._run is r
            if same:
                self.cut(why)
        return why

    def _watch(self):
        while not self._stop.wait(WATCH_S):
            try:
                self.tick()
            except Exception:                            # noqa: BLE001
                try:
                    self.cut("the Fireball watcher raised")
                except Exception:                        # noqa: BLE001
                    pass

    def close(self):
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(1.0)
        self.cut("the engine is stopping")

    def on_abort(self, who="", screen="", what="Abort"):
        """An Abort or a Stop (Morning.on_abort, Morning.stop_hooks): every
        fireball channel cut at once, not when the watcher next sees the
        flame link's zero."""
        self.cut(f"{what} pressed by {who or screen or 'someone'}", who=who,
                 screen=screen)

    # -- views -----------------------------------------------------------------
    def active(self):
        with self._lock:
            return self._run is not None

    def summary(self):
        """For every screen's status: the nav button, and whether anything
        is on."""
        with self._lock:
            r = self._run
            on = sorted(r["on"]) if r is not None else []
        return {"available": self.available, "on": on,
                "owner": r["owner"][0] if r is not None else None}

    def view(self, owner=None):
        with self._lock:
            r = self._run
            on = [k for k in KEYS if r is not None and k in r["on"]]
            mine = r is not None and owner is not None and r["owner"] == owner
            run_owner = r["owner"][0] if r is not None else None
        out = {"available": self.available, "problem": self.problem,
               "on": on, "owner": run_owner, "mine": mine,
               "last_cut": self.last_cut, "on_value": ON_VALUE,
               "beat_stale_ms": int(BEAT_STALE_S * 1000)}
        if self.group is None:
            out["controls"] = []
            return out
        out["group"] = {"name": self.group["name"],
                        "index": self.group["index"],
                        "safety": self.group["safety"]}
        out["controls"] = [{"key": k, "label": lb, "kind": kind,
                            "slot": slot}
                           for (k, lb, kind), slot in
                           zip(CONTROLS, self.group["fire"])]
        state, why = self.armed_state()
        out["armed"] = state
        out["armed_why"] = why
        out["blocked"] = self.refusal(check_armed=False)
        return out


# ------------------------------------------------------------ the engine --

def _during_show_blockers(remote, m):
    """The Fireball page's own rules during a show (Jeff, 2026-10-30): a
    show playing, the show clock playing, Run pressed and Programming
    playback and a Hold do NOT hold it off; an Abort standing, live flame
    programming, and the Startup page's settings or lock state not trusted
    do. "" or the sentence. May raise (the caller
    fails closed)."""
    svc = remote.schedule
    mach = getattr(svc, "machine", None)
    cond = getattr(svc, "conductor", None)
    if getattr(mach, "abort_latched", False) or \
            getattr(cond, "latched", False):
        return "the show is aborted; press Reset first."
    # A Hold (the schedule HOLD or PAUSED, the conductor's held look) does
    # NOT hold the page off (Jeff, 2026-10-30): it zeroes the show's own
    # values only.
    lf = getattr(remote, "live_flame", None)
    if lf is not None and lf.on:
        return ("live flame programming is on; nothing fires from this "
                "page until it is off.")
    if m.config_error:
        return (f"ltcplay_morning.json could not be read "
                f"({m.config_error}), so nothing fires from this page "
                f"until it is fixed.")
    why = m.lock_problem()
    if why:
        return f"{why}; nothing fires from this page until then."
    return ""


def engine_blockers(remote, link=None, cleaning=True, during_show=False):
    """Why the engine forbids the Fireball page's controls right now, in a
    sentence, or "": the Startup page's purge rules (a show live or paused,
    an Abort standing, the show clock playing, live flame programming
    on, its settings or lock state not
    trusted, flamesafe not holding the engine's locks, Arrange mode, a
    purge selected or firing), a Hold, Run pressed, the flame link not
    sending, and (with `cleaning`) the wave flamer cleaning running
    (cleaning.py, which asks the same rules for itself). With
    `during_show` (the Fireball page itself, Jeff 2026-10-30) a show
    playing and Run pressed are allowed: _during_show_blockers. The
    cleaning never passes it. Fails closed."""
    try:
        m = remote.morning
        if m is None:
            return "the Startup page is not set up in this engine."
        cl = getattr(remote, "cleaning", None) if cleaning else None
        if cl is not None and cl.active():
            return ("the wave flamer cleaning is running on the Shutdown "
                    "page; stop it first.")
        if during_show:
            why = _during_show_blockers(remote, m)
            if why:
                return why
        else:
            why = remote.morning_live_refusal() or m._live_or_broken()
            if why:
                return why
            svc = remote.schedule
            st = getattr(getattr(svc, "machine", None), "state", None)
            if st == "HOLD":
                return "a Hold is standing; Resume or Reset first."
            s = getattr(remote.control, "session", None)
            if s is not None and getattr(s, "running", False):
                return ("Run is pressed (a show file is running), so the "
                        "show file owns the flame channels; press Stop "
                        "first.")
            try:
                cs = remote.control.state() or {}
            except Exception:                            # noqa: BLE001
                cs = {}
            if cs.get("running") or cs.get("starting"):
                return ("Run is pressed (a show file is running), so the show "
                        "file owns the flame channels; press Stop first.")
        why = m._locks_confirmed()
        if why:
            return why
        why = m.fire_arrange_refusal()
        if why:
            return why
        pg = m.purge
        if pg.fire is not None or pg.selected:
            return ("machines are selected for a purge on the Startup page; "
                    "clear the purge selection first.")
        if link is not None:
            try:
                sender = link.sender_state()
            except Exception:                            # noqa: BLE001
                sender = "unknown"
            if sender != "running":
                return (f"the flame link to flamesafe is not sending "
                        f"({sender}).")
            if getattr(link, "lock_alarm", ""):
                return f"the flame link has an alarm: {link.lock_alarm}"
    except Exception as e:                               # noqa: BLE001
        return f"the engine's state could not be read ({type(e).__name__})."
    return ""


def build(remote, morning, link, journal=None, threaded=True,
          clock=time.monotonic):
    """The engine's panel (web.serve): the fireball group from the Startup
    page's reading of flamesafe's config, its lockout, flamesafe's status
    from the remote, and the flame link."""
    group, problem = find_group(getattr(morning, "groups", None) or [])

    def locked():
        return morning.lockout.channels(morning.cfg) | \
            set(morning.lockout.file_channels or ())

    def flames():
        fs = getattr(remote, "flame_status", None)
        return fs.view() if fs is not None else None

    if group is not None and link is not None:
        # The only slots the page's frame may carry on the flame link.
        try:
            link.set_manual_slots(group["fire"])
        except Exception:                                # noqa: BLE001
            pass
    panel = FireballPanel(group, problem, link_get=lambda: link,
                          flames_get=flames,
                          blockers=lambda: engine_blockers(
                              remote, link, during_show=True),
                          locked_slots=locked, journal=journal,
                          clock=clock, threaded=threaded)
    # The Startup page's purge waits while this has anything on (one
    # source of purge frames at a time), and its Abort cuts this too.
    morning.purge_blocker = lambda: (
        "the Fireball page has a channel on; turn it off there first."
        if panel.active() else "")
    morning.abort_hooks.append(panel.on_abort)
    return panel


def _owner(ctx):
    return (ctx.device, str(getattr(ctx, "window", "") or "")[:64])


def route_get(panel, ctx, wall=time.time):
    """GET /api/remote/fireball: anyone may read it."""
    if panel is None:
        return 404, {"error": "The Fireball page is not set up in this "
                              "engine (it needs the Fire & Ice engine and "
                              "its flame link)."}
    v = panel.view(_owner(ctx))
    v["served_at"] = int(wall() * 1000)
    return 200, v


def route_post(panel, remote, name, body, ctx):
    """POST fireball-beat {"controls": {...}, "run": id|null, "seen": ms}
    and fireball-off. (code, body)."""
    if panel is None:
        return 404, {"error": "The Fireball page is not set up in this "
                              "engine."}
    body = body if isinstance(body, dict) else {}
    who, screen, _s = remote._actor(ctx, body)
    if name == "fireball-off":
        return panel.off(f"{who or 'someone'} pressed All off on the "
                         f"{ctx.device}", who=who, screen=screen)
    if name == "fireball-beat":
        try:
            seen = float(body.get("seen"))
            seen_ok = abs(remote.wall() - seen / 1000.0) <= PAGE_FRESH_S
        except (TypeError, ValueError):
            seen_ok = False
        may = (True, "")
        if controls_on(body) and not remote.purge_enabled(ctx.local):
            may = (False, "The Fireball controls follow the purge's switch, "
                          "which is off from here (purge_arming, and from "
                          "the show network screen_arming, in "
                          "ltcplay_remote.json).")
        token = getattr(ctx, "token", "") or ""
        still = None
        check = getattr(remote, "fireball_signed_out", None)
        if check is not None and getattr(remote, "users_active",
                                         lambda: False)():
            # The fuel pump's rule (round-5 review B 2): signed out,
            # demoted, removed, a password reset or the sign-out after the
            # last show all end the sign-in, or its right to arm.
            def still():
                return check(token, who)
        return panel.beat(body, _owner(ctx), seen_ok=seen_ok, may=may,
                          who=who, screen=screen, token=token,
                          local=bool(getattr(ctx, "local", False)),
                          still=still)
    return 404, {"error": "no such thing here"}
