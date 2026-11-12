"""Pre-show audio (Jeff, 2026-11-12): "play an audio file automatically two
minutes prior to the show. We just wanna be able to specify whether that is
on or off and what the audio file is."

Two settings and nothing else: On or Off (Off unless an admin turns it on)
and which file, one of the WAV files the announcements already know (the
folder beside ltcplay_announce.json and the folders their three recordings
are in). Kept in ltcplay_preshow_audio.json in that same settings folder,
so it survives a restart.

When it plays (the scheduler decides, schedule_service.Service.
_preshow_lookahead, under its own lock, from memory only):
  - For a SCHEDULED show only, at LEAD_S (120 s) before its time, once. A
    show started by hand (Start now, start at a cue) has no time to count
    back from and gets none.
  - Only when the scheduler would start that show by itself at its time:
    between shows (IDLE or STANDBY), show control on (Run pressed), not on
    Hold, no show running, no Stop under way, the show not delayed (Delay
    next included), no Programming Run, auto start on. An alert open at
    that moment (the alert hold would hold the show at its time): skipped,
    journaled (Jeff, 2026-11-12).
  - Never late: first seen with less than LEAD_S - GRACE_S (115 s) to go
    (LTC Player started, or the schedule changed, inside the window), it is
    not played and the journal says so.
  - At most once per show per night, restart safe: the decision is written
    to ltcplay_preshow_audio_played.json BEFORE the sound starts, and a
    restart inside the window reads it back and does not play again. Kept
    by night and show number, so a show whose time is edited after it
    played never plays it again.
  - Never over an announcement already playing: skipped, journaled
    (decided under the announcement player's own lock).
  - An operator always wins: an announcement press (or Test tone) stops
    it at once and plays; Stop sound stops it; a Hold (or a pause) fades
    it at once (announce.on_schedule_held).

How it plays: through the announcement player (announce.AnnounceService.
play_preshow), the same route and fade as an announcement: mixed into the
show audio's front left and right (its "show_outputs", Main L/R) while the
show audio runs, else the announcement's own output. A file is at most
MAX_S (110 s, Jeff 2026-11-12), refused when chosen and again when it is
due, so started 120 s before the show it has ended 10 s before the show
audio opens; a show starting while it still plays fades it over 0.15 s
(announce.on_show_started) all the same. It never Holds, pauses, delays or blocks a show
or the rig check: the scheduler only records a decision in memory under its
lock; the file, the disk write and the player all run on a thread of their
own afterwards (Service._locked's pending hooks), and every failure is a
journal line, never an exception into the scheduler.

Nothing here touches flames, lasers, hazers, the pump or any rig output:
its only output is the announcement player.
"""
import json
import os
import re
import threading

LEAD_S = 120
# The longest pre-show file (Jeff, 2026-11-12): it ends 10 s before the
# show audio opens. The same number as announce.PRESHOW_MAX_S.
MAX_S = 110
# How late after LEAD_S it may still start: a tick or two, never more.
GRACE_S = 5
SETTINGS_FILE = "ltcplay_preshow_audio.json"
PLAYED_FILE = "ltcplay_preshow_audio_played.json"
# The records kept on disk: shows are keyed by night, so old nights only
# need to go eventually.
KEEP = 200
# The most WAV files the picker lists.
CHOICES_MAX = 200
ACTION = "pre-show audio"
# announce.PRESHOW_BUSY: the player's word for "an announcement was
# already playing" (decided under its own lock).
BUSY = "an announcement was already playing"


def key_text(key):
    """The scheduler's show key (night, number, planned time) as one
    string, for the record file."""
    return "|".join(str(k) for k in key)


def _write_json_atomic(path, doc):
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def too_long_words(length_s):
    """The plain sentence when a file is over MAX_S, else ""."""
    if length_s is not None and length_s > MAX_S:
        return (f"That file is {length_s:.0f} s long; pre-show audio is at "
                f"most {MAX_S} s. Choose a shorter file.")
    return ""


# What the announcement player says to a press, turned into the reason
# alone, so the journal line stays one clear sentence.
_PRESS = re.compile(r"^Pre-show audio pressed Play on Pre-show audio on the "
                    r"schedule(?:, but |\. (?:Refused: |It failed[^:]*: ))")


def plain_reason(text):
    t = str(text or "").strip()
    t = _PRESS.sub("", t)
    t = re.sub(r"^Pre-show audio could not be played: ", "", t)
    for tail in (" Press it again.", " Nothing was changed."):
        if t.endswith(tail):
            t = t[:-len(tail)]
    t = t.strip().rstrip(".")
    return t[:1].lower() + t[1:] if t[:2] != t[:2].upper() else t


def _same(a, b):
    return os.path.normcase(os.path.abspath(a)) == \
        os.path.normcase(os.path.abspath(b))


class PreShowAudio:
    """One per engine, made by web.serve when both the announcements and
    the scheduler run. `journal(text, outcome, show)` writes a line to the
    night journal (Service.preshow_journal); `audit` is the engine's
    audit.AuditLog or None."""

    LEAD_S = LEAD_S
    GRACE_S = GRACE_S

    def __init__(self, folder, announce, journal=None, audit=None):
        self.folder = folder
        self.announce = announce
        self.journal = journal
        self.audit = audit
        self._lock = threading.Lock()
        self._disk = threading.Lock()
        self.on = False
        self.file = ""
        self.settings_error = ""
        self._probe = None
        self._recs = {}
        self.last = ""
        self._load_settings()
        self._load_played()

    # -- files ------------------------------------------------------------
    def settings_path(self):
        return os.path.join(self.folder, SETTINGS_FILE)

    def played_path(self):
        return os.path.join(self.folder, PLAYED_FILE)

    def _load_settings(self):
        """Off unless the file says on with a file. A file that cannot be
        used leaves it off and says why (view)."""
        path = self.settings_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
            on, f = doc.get("on"), doc.get("file", "")
            if not isinstance(on, bool) or not isinstance(f, str):
                raise ValueError('it has to be {"on": true or false, '
                                 '"file": a path}')
        except (OSError, ValueError, AttributeError) as e:
            self.settings_error = (f"{SETTINGS_FILE} could not be used "
                                   f"({e}), so pre-show audio is off")
            return
        self.file = f.strip()
        self.on = bool(on and self.file)
        self._probe = self._probe_now(self.file) if self.file else None

    def _load_played(self):
        """Tonight's decisions from before a restart: a show already
        handled is never played again (restart safe)."""
        try:
            with open(self.played_path(), encoding="utf-8-sig") as fh:
                doc = json.load(fh)
            shows = doc.get("shows") or {}
            for k, r in shows.items():
                if isinstance(r, dict):
                    self._recs[str(k)] = dict(r, restored=True, said=False)
        except (OSError, ValueError, AttributeError):
            pass

    def _persist(self):
        """(True, "") once every decision is on disk, else (False, why).
        Called on the play thread, never under the scheduler's lock."""
        with self._disk:
            with self._lock:
                items = list(self._recs.items())[-KEEP:]
                doc = {"_note": "Which shows pre-show audio has already "
                                "handled (played or skipped), so a restart "
                                "never plays it twice. Safe to delete "
                                "between nights.",
                       "shows": {k: {f: r.get(f) for f in
                                     ("n", "outcome", "why", "at", "file")}
                                 for k, r in items}}
            try:
                _write_json_atomic(self.played_path(), doc)
            except OSError as e:
                return False, str(e)
        return True, ""

    # -- the two settings -------------------------------------------------
    def _probe_now(self, path):
        try:
            return self.announce._probe_file(path)
        except Exception as e:                   # noqa: BLE001
            return {"available": False, "reason": f"{type(e).__name__}: "
                                                  f"{e}", "length_s": None}

    def _folders(self):
        out = []
        cfg = getattr(self.announce, "config_path", None)
        if cfg:
            out.append(os.path.dirname(os.path.abspath(cfg)))
        for p in (getattr(self.announce, "files", None) or {}).values():
            out.append(os.path.dirname(os.path.abspath(p)))
        seen, folders = set(), []
        for f in out:
            k = os.path.normcase(f)
            if k not in seen:
                seen.add(k)
                folders.append(f)
        return folders

    def choices(self):
        """Every WAV file the announcements know of: the folder beside
        ltcplay_announce.json and the folders their recordings are in.
        Reads the disk: the picker's GET only, never a status poll."""
        out = []
        for folder in self._folders():
            try:
                names = sorted(os.listdir(folder), key=str.lower)
            except OSError:
                continue
            for name in names:
                path = os.path.join(folder, name)
                if not name.lower().endswith(".wav") or \
                        not os.path.isfile(path) or \
                        any(_same(path, c["path"]) for c in out):
                    continue
                st = self._probe_now(path)
                out.append({"path": path, "name": name,
                            "available": bool(st.get("available")),
                            "reason": st.get("reason"),
                            "length_s": st.get("length_s"),
                            "too_long": bool(too_long_words(
                                st.get("length_s")))})
                if len(out) >= CHOICES_MAX:
                    return out
        return out

    def save(self, on, file, who="", screen=""):
        """Turn it on or off and choose the file. Raises ValueError with a
        sentence; returns the sentence said. Off needs no file."""
        if not isinstance(on, bool):
            raise ValueError("On has to be true or false. Nothing was "
                             "changed.")
        file = (file or "").strip() if isinstance(file, str) else None
        if file is None:
            raise ValueError("The file has to be a file name. Nothing was "
                             "changed.")
        probe = None
        if file:
            hit = [c for c in self.choices() if _same(c["path"], file)]
            if not hit:
                raise ValueError("That file is not one the announcements "
                                 "know. Put it in the Announcements folder "
                                 "and choose it again. Nothing was "
                                 "changed.")
            file = hit[0]["path"]
            probe = self._probe_now(file)
        if on and not file:
            raise ValueError("Choose the audio file first. Nothing was "
                             "changed.")
        if on and not probe.get("available"):
            raise ValueError(f"That file cannot play: "
                             f"{str(probe.get('reason')).rstrip('.')}. "
                             f"Nothing was changed.")
        long_why = too_long_words(probe.get("length_s")) if probe else ""
        if long_why:
            raise ValueError(f"{long_why} Nothing was changed.")
        doc = {"on": on, "file": file,
               "_note": "Pre-show audio: whether the file plays 2 minutes "
                        "before each scheduled show, and which file. Set "
                        "on the rack screen, Schedule tab."}
        _write_json_atomic(self.settings_path(), doc)
        with self._lock:
            self.on, self.file, self._probe = on, file, probe
            self.settings_error = ""
        name = os.path.basename(file) if file else "no file"
        return (f"Pre-show audio is {'on' if on else 'off'}: {name}" +
                (f", played {LEAD_S // 60} minutes before each scheduled "
                 f"show." if on else "."))

    def is_on(self):
        return bool(self.on and self.file)

    def recheck(self):
        """Read the chosen file again (the card's GET, when it is shown):
        a file deleted or replaced since it was chosen is said at once."""
        with self._lock:
            f = self.file
        probe = self._probe_now(f) if f else None
        with self._lock:
            if self.file == f:
                self._probe = probe

    def view(self):
        """The rack screen's card: the file's name, never its path. Memory
        only: safe on the status path."""
        with self._lock:
            pr = self._probe or {}
            return {"available": True, "on": self.is_on(),
                    "name": os.path.basename(self.file) if self.file else "",
                    "too_long": bool(too_long_words(pr.get("length_s")))
                    if self.file else False,
                    "file_ok": bool(pr.get("available")) if self.file
                    else None,
                    "file_why": pr.get("reason") if self.file else None,
                    "length_s": pr.get("length_s"),
                    "error": self.settings_error or None,
                    "lead_s": LEAD_S, "last": self.last or None}

    # -- the scheduler's side ---------------------------------------------
    def record(self, key):
        """The decision already made for this show, or None. Memory only."""
        with self._lock:
            return self._recs.get(key_text(key))

    def restored_note(self, key):
        """Once, for a show handled before a restart: the sentence that
        says it is not played again; else None. Memory only."""
        with self._lock:
            r = self._recs.get(key_text(key))
            if not r or not r.get("restored") or r.get("said") or \
                    r.get("outcome") == "skipped":
                return None
            r["said"] = True
            n = r.get("n")
        return (f"Pre-show audio for show {n} was already handled before "
                f"LTC Player restarted, so it is not played again.")

    def claim(self, key, n, why, now):
        """The scheduler, under its lock, at LEAD_S: decide once for this
        show. `why` is the reason it is not played, or "" to play. Memory
        only; settle() (on a thread of its own) does the rest."""
        with self._lock:
            kt = key_text(key)
            if kt in self._recs:
                return False
            self._recs[kt] = {"n": n, "outcome": "skipped" if why else
                              "started", "why": why or None,
                              "at": now.isoformat() if now else None,
                              "file": self.file}
            return True

    def settle(self, key):
        """On a thread of its own, after the scheduler's lock is free: write
        the decision down, then play (or say why not). Never raises."""
        try:
            self._settle(key)
        except Exception as e:                   # noqa: BLE001
            self._say(f"Pre-show audio could not be handled "
                      f"({type(e).__name__}: {e}). The show carries on.",
                      "error", None)

    def _settle(self, key):
        kt = key_text(key)
        with self._lock:
            rec = self._recs.get(kt)
            if rec is None:
                return
            n = rec["n"]
            if rec["outcome"] == "started" and not self.is_on():
                rec.update(outcome="skipped", why="it was turned off")
            outcome, why, path = rec["outcome"], rec["why"], rec["file"]
        ok, disk_why = self._persist()
        if outcome == "skipped":
            self._say(f"Pre-show audio not played before show {n}: {why}.",
                      "skipped", n)
            self._audit("refused", n, why)
            return
        if not ok:
            why = (f"it could not be written down first ({disk_why}), so a "
                   f"restart could play it twice")
            with self._lock:
                rec.update(outcome="skipped", why=why)
            self._say(f"Pre-show audio not played before show {n}: {why}. "
                      f"The show carries on.", "skipped", n)
            self._audit("refused", n, why)
            return
        name = os.path.basename(path)
        try:
            # The plain reasons first: the player's own refusal of a file
            # is worded for a button press.
            if not os.path.exists(path):
                raise ValueError("the file is not on this PC")
            pr = self._probe_now(path)
            if not pr.get("available"):
                raise ValueError(f"the file cannot play: "
                                 f"{str(pr.get('reason')).rstrip('.')}")
            long_why = too_long_words(pr.get("length_s"))
            if long_why:
                raise ValueError(long_why)
            self.announce.play_preshow(path)
        except Exception as e:                   # noqa: BLE001
            why = (plain_reason(e) if isinstance(e, ValueError) else
                   f"{type(e).__name__}: {str(e).strip().rstrip('.')}")
            why = why or type(e).__name__
            if why == BUSY:
                with self._lock:
                    rec.update(outcome="skipped", why=why)
                self._persist()
                self._say(f"Pre-show audio not played before show {n}: "
                          f"{why}.", "skipped", n)
                self._audit("refused", n, why)
                return
            with self._lock:
                rec.update(outcome="failed", why=why)
            self._persist()
            self._say(f"Pre-show audio did not play before show {n} "
                      f"({name}): {why}. The show carries on.", "failed", n)
            self._audit("error", n, why)
            return
        with self._lock:
            rec.update(outcome="played")
        self._persist()
        self._say(f"Pre-show audio played before show {n}: {name}.",
                  "played", n)
        self._audit("accepted", n, name)

    def _say(self, text, outcome, show):
        self.last = text
        j = self.journal
        if j is None:
            return
        try:
            j(text, outcome, show)
        except Exception:                        # noqa: BLE001
            pass

    def _audit(self, result, n, reason):
        log = self.audit
        if log is None:
            return
        try:
            from . import audit as audit_mod
            log.event(user=audit_mod.SYSTEM, surface="engine",
                      action=ACTION, target=f"show {n}", result=result,
                      reason=str(reason or ""), source="schedule")
        except Exception:                        # noqa: BLE001
            pass
