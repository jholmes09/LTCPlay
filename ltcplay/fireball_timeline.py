"""The fireball timeline (Jeff, 2026-10-30): per show, a start timecode and
a duration for each stage of the fireball's preparation and ignition.

"We need to be able to dynamically change the timeline for the stages of
preparation and the ignition by altering the start time and duration of
each stage. As a user, I want to set the start time code of each stage and
I want to set a duration for that DMX channel to be active."

This module is the model, the validation and the storage. It SENDS
NOTHING: it imports nothing that sends, and nothing that sends imports it
in this change. flamesafe stays the sole writer of the flame universe.

The stages (Jeff's channels on the flame universe, in his order)
================================================================

    Fuel        Fuel Charge    55
    Air         Air Charge     56
    Igniter     Igniter        58
    Valve Open  Valve Open     57

Purge (59) is manual only: it is never a stage here; the Fireball page
keeps it. "All Fireball Safety" (77) is flamesafe's own slot; nothing here
names it.

The rules (Jeff's decisions, 2026-10-30)
========================================

Only well-formedness is checked, and every refusal is a plain sentence:

  - a start is a timecode HH:MM:SS:FF at 30 frames a second (FF 00 to
    29), at or after 00:00:00:00 and before the end of the show's own cue
    (the show audio's cue time: 00:00:00:00 at the top of the cue);
  - a duration is a number of seconds above 0 (finite). There is NO
    upper limit per stage and none on the total: a stage that would run
    past the end of the show simply ends there;
  - each stage at most once. A stage left out is never on.

There are no interlocks. "Igniter is independent": the stages are
independent windows, with no order and no overlap rule between them.

Source per show
===============

Each show file says "fseq" (today's behaviour, and the default for every
show this file does not name) or "timeline". Nothing changes for any show
until an admin switches it.

Storage
=======

FILE in the settings folder, beside ltcplay_hazers.json:

    {"fireball_timeline_format": 1, "rev": 7,
     "shows": {"Fire and Ice.json": {
         "source": "timeline", "cue": "Show", "show_len_s": 312.5,
         "stages": [{"stage": "fuel", "start": "00:03:12:10",
                     "dur_s": 2.0}, ...],
         "edited_by": "Andy", "edited_at": "2026-10-30T21:04:11Z",
         "screen": "Rack screen"}}}

  - Read strictly: an unknown key, a wrong type, a bad stage or anything
    else wrong and the WHOLE file is refused (`read_doc`). A file that
    cannot be read means no timeline for any show; the engine never falls
    back to the FSEQ for a show set to "timeline" (a later change).
  - Written whole and atomically (a temporary file, fsync, os.replace).
  - Every save names the revision it was based on; a stale one is refused
    and nothing is written (two editors never overwrite each other).
  - The version before each save is kept in HISTORY_DIR (the last
    HISTORY_KEEP), named by its UTC time and revision.
  - Every save writes one journal line and one audit event: who, from
    which screen, and what changed, stage by stage.
"""
import datetime
import json
import math
import os
import re
import threading
import time

NAME = "Fireball timeline"
FILE = "ltcplay_fireball_timeline.json"
FORMAT = 1
HISTORY_DIR = "fireball_timeline_history"
HISTORY_KEEP = 50
FPS = 30
# (key, the word the screens use, the flame universe slot). In Jeff's
# order: Fuel, Air, Igniter, Valve Open.
STAGES = (("fuel", "Fuel", 55), ("air", "Air", 56),
          ("igniter", "Igniter", 58), ("valve", "Valve Open", 57))
STAGE_KEYS = tuple(k for k, _w, _s in STAGES)
STAGE_WORD = {k: w for k, w, _s in STAGES}
STAGE_SLOT = {k: s for k, _w, s in STAGES}
# Every fireball fire slot (Fuel, Air, Valve Open, Igniter, Purge): the
# slots the FSEQ no longer drives for a show set to "timeline".
FIREBALL_FIRE_SLOTS = (55, 56, 57, 58, 59)
PURGE_SLOT = 59
SOURCES = ("fseq", "timeline")
DEFAULT_SOURCE = "fseq"

_TC = re.compile(r"(\d{2}):(\d{2}):(\d{2}):(\d{2})")
_DOC_KEYS = {"fireball_timeline_format", "rev", "shows"}
_ENTRY_KEYS = {"source", "cue", "show_len_s", "stages", "edited_by",
               "edited_at", "screen"}
_STAGE_KEYS = {"stage", "start", "dur_s"}
MAX_SHOWS = 500
MAX_TEXT = 200


class TimelineError(ValueError):
    """A timeline, or the file, that is refused. str() is plain words."""


# ------------------------------------------------------------ the parts --

def parse_tc(text):
    """A start timecode HH:MM:SS:FF at FPS frames a second as a whole
    number of frames from 00:00:00:00. TimelineError otherwise."""
    if not isinstance(text, str):
        raise TimelineError("a start is a timecode written HH:MM:SS:FF")
    m = _TC.fullmatch(text.strip())
    if not m:
        raise TimelineError(f"{text!r} is not a timecode; write it "
                            f"HH:MM:SS:FF, for example 00:01:30:00")
    h, mi, s, f = (int(x) for x in m.groups())
    if mi > 59 or s > 59:
        raise TimelineError(f"{text!r} is not a timecode: minutes and "
                            f"seconds go up to 59")
    if f >= FPS:
        raise TimelineError(f"{text!r} is not a timecode: the frames go "
                            f"00 to {FPS - 1} at {FPS} frames a second")
    return ((h * 60 + mi) * 60 + s) * FPS + f


def tc_text(frames):
    """Whole frames from 00:00:00:00 as HH:MM:SS:FF."""
    frames = int(frames)
    f = frames % FPS
    s = frames // FPS
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{f:02d}"


def parse_duration(v):
    """A duration in seconds: a finite number above 0. TimelineError
    otherwise. No upper limit (Jeff, 2026-10-30)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise TimelineError("a duration is a number of seconds above 0")
    v = float(v)
    if not math.isfinite(v) or v <= 0:
        raise TimelineError("a duration is a number of seconds above 0")
    return v


def _seconds_text(v):
    return f"{v:g} s"


def check_stages(stages, show_len_s=None):
    """The stages as given (a list of {"stage", "start", "dur_s"}) checked
    and put in Jeff's order: a list of {"stage", "start" (HH:MM:SS:FF as
    given, trimmed), "dur_s" (float)}. With `show_len_s` (the show cue's
    length in seconds) every start must lie before it. TimelineError with
    every problem, one sentence each, otherwise."""
    if not isinstance(stages, list):
        raise TimelineError("the stages are a list")
    problems, seen, out = [], set(), {}
    for i, st in enumerate(stages):
        if not isinstance(st, dict):
            problems.append(f"stage {i + 1} is not a stage")
            continue
        key = st.get("stage")
        word = STAGE_WORD.get(key) if isinstance(key, str) else None
        if word is None:
            problems.append(f"{key!r} is not a stage; the stages are "
                            + ", ".join(STAGE_WORD[k] for k in STAGE_KEYS))
            continue
        extra = set(st) - _STAGE_KEYS
        if extra:
            problems.append(f"{word}: unknown field "
                            f"{', '.join(sorted(map(str, extra)))}")
        if key in seen:
            problems.append(f"{word} is given twice; each stage is given "
                            f"once")
            continue
        seen.add(key)
        start = st.get("start")
        try:
            fr = parse_tc(start)
            if show_len_s is not None and fr / FPS >= show_len_s:
                raise TimelineError(
                    f"the start {start.strip()} is not inside the show, "
                    f"which is {tc_text(int(show_len_s * FPS))} long")
        except TimelineError as e:
            problems.append(f"{word}: {e}")
            fr = None
        try:
            dur = parse_duration(st.get("dur_s"))
        except TimelineError as e:
            problems.append(f"{word}: {e}")
            dur = None
        if fr is not None and dur is not None:
            out[key] = {"stage": key, "start": tc_text(fr), "dur_s": dur}
    if problems:
        raise TimelineError("; ".join(problems) + ".")
    return [out[k] for k in STAGE_KEYS if k in out]


def _text(v, what, empty_ok=True):
    if not isinstance(v, str) or len(v) > MAX_TEXT or \
            (not empty_ok and not v.strip()):
        raise TimelineError(f"{what} is not a short piece of text")
    return v


def check_entry(show, entry):
    """One show's entry from the file, checked strictly: a normalised copy,
    or TimelineError naming the show."""
    try:
        if not isinstance(entry, dict):
            raise TimelineError("it is not an entry")
        extra = set(entry) - _ENTRY_KEYS
        if extra:
            raise TimelineError(f"unknown field "
                                f"{', '.join(sorted(map(str, extra)))}")
        src = entry.get("source")
        if src not in SOURCES:
            raise TimelineError(f"the source is {src!r}; it is \"fseq\" "
                                f"or \"timeline\"")
        cue = _text(entry.get("cue", ""), "the cue name")
        ln = entry.get("show_len_s")
        if ln is not None and (isinstance(ln, bool) or
                               not isinstance(ln, (int, float)) or
                               not math.isfinite(ln) or ln <= 0):
            raise TimelineError("the show length is not a number of "
                                "seconds above 0")
        stages = check_stages(entry.get("stages", []),
                              None if ln is None else float(ln))
        out = {"source": src, "cue": cue,
               "show_len_s": None if ln is None else float(ln),
               "stages": stages}
        for k in ("edited_by", "edited_at", "screen"):
            out[k] = _text(entry.get(k, ""), k)
        if src == "timeline" and not cue:
            raise TimelineError("it is set to the timeline but names no cue")
        return out
    except TimelineError as e:
        raise TimelineError(f"{show}: {e}")


def empty_doc():
    return {"fireball_timeline_format": FORMAT, "rev": 0, "shows": {}}


def read_doc(doc):
    """The whole file's contents, checked strictly: a normalised copy, or
    TimelineError. One bad entry refuses the whole file."""
    if not isinstance(doc, dict):
        raise TimelineError("it is not a fireball timeline file")
    extra = set(doc) - _DOC_KEYS
    if extra:
        raise TimelineError(f"unknown field "
                            f"{', '.join(sorted(map(str, extra)))}")
    if doc.get("fireball_timeline_format") != FORMAT:
        raise TimelineError(f"fireball_timeline_format is not {FORMAT}")
    rev = doc.get("rev")
    if isinstance(rev, bool) or not isinstance(rev, int) or rev < 0:
        raise TimelineError("rev is not a whole number")
    shows = doc.get("shows")
    if not isinstance(shows, dict) or len(shows) > MAX_SHOWS:
        raise TimelineError("shows is not a list of shows")
    out = {}
    for show, entry in shows.items():
        _show_name(show)
        out[show] = check_entry(show, entry)
    return {"fireball_timeline_format": FORMAT, "rev": rev, "shows": out}


def _show_name(show):
    if not isinstance(show, str) or not show.strip() or \
            len(show) > MAX_TEXT or os.path.basename(show) != show or \
            show in (".", ".."):
        raise TimelineError(f"{show!r} is not a show file name")
    return show


def show_key(name):
    """A show file name as a case-blind folder sees it (Windows, macOS):
    lower case (str.lower, as the file systems compare: never Unicode
    case folding, which would make "ß" and "ss" one name), and no
    trailing dots or spaces (Windows drops them)."""
    return str(name).rstrip(" .").lower()


def entry_of(doc, show):
    """The entry for `show` in a read doc, or the default (source "fseq",
    no stages) for a show the file does not name."""
    got = (doc or {}).get("shows", {}).get(show)
    if got is not None:
        return got
    return {"source": DEFAULT_SOURCE, "cue": "", "show_len_s": None,
            "stages": [], "edited_by": "", "edited_at": "", "screen": ""}


def describe_change(before, after):
    """What changed between two entries, in plain words, for the journal
    and the audit log: "" when nothing did."""
    out = []
    if before["source"] != after["source"]:
        out.append(f"source {before['source']} to {after['source']}")
    if (before.get("cue") or "") != (after.get("cue") or "") and \
            before.get("cue"):
        out.append(f"cue {before['cue']!r} to {after['cue']!r}")
    b = {s["stage"]: s for s in before["stages"]}
    a = {s["stage"]: s for s in after["stages"]}
    for k in STAGE_KEYS:
        w = STAGE_WORD[k]
        if k in a and k not in b:
            out.append(f"{w} added, start {a[k]['start']} for "
                       f"{_seconds_text(a[k]['dur_s'])}")
        elif k in b and k not in a:
            out.append(f"{w} removed (was start {b[k]['start']} for "
                       f"{_seconds_text(b[k]['dur_s'])})")
        elif k in a:
            ch = []
            if a[k]["start"] != b[k]["start"]:
                ch.append(f"start {b[k]['start']} to {a[k]['start']}")
            if a[k]["dur_s"] != b[k]["dur_s"]:
                ch.append(f"duration {_seconds_text(b[k]['dur_s'])} to "
                          f"{_seconds_text(a[k]['dur_s'])}")
            if ch:
                out.append(f"{w} " + ", ".join(ch))
    return "; ".join(out)


def windows(entry):
    """[(slot, start_s, end_s)] for an entry's stages, in Jeff's order: a
    stage is on while start_s <= t < end_s on the show cue's own time."""
    out = []
    for st in entry["stages"]:
        s0 = parse_tc(st["start"]) / FPS
        out.append((STAGE_SLOT[st["stage"]], s0, s0 + st["dur_s"]))
    return out


def _utc(t):
    return datetime.datetime.fromtimestamp(
        t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------ the store --

class Store:
    """FILE in `folder`: read strictly, written whole and atomically with a
    revision check, the version before each save kept in HISTORY_DIR, and
    each save journaled and audited.

    journal   (who, screen, outcome, text, fault=False) or None
    audit     audit.AuditLog or None
    wall      the wall clock (seconds since the epoch)"""

    def __init__(self, folder, journal=None, audit=None, wall=time.time):
        self.folder = folder
        self.path = os.path.join(folder, FILE) if folder else None
        self._journal = journal
        self.audit = audit
        self._wall = wall
        # Reentrant: save() reads the file under it. Every read and every
        # write of the file is under it, so on Windows a save's os.replace
        # never meets this program's own reader holding the file open.
        self._lock = threading.RLock()
        # Called after every save that was written, with (show, the new
        # entry, the new revision), on the saving thread. The engine (a
        # later change) is told of a new revision this way. A listener that
        # raises is ignored: the save stands.
        self.on_saved = []

    def _note(self, who, screen, outcome, text, fault=False):
        if self._journal is None:
            return
        try:
            self._journal(who, screen, outcome, text, fault=fault)
        except Exception:                        # noqa: BLE001
            pass

    def _audit(self, who, screen, target, result, reason):
        log = self.audit
        if log is None:
            return
        try:
            from . import audit as audit_mod
            log.event(user=who or audit_mod.UNSIGNED, surface="engine",
                      action="fireball timeline", target=target,
                      result=result, reason=reason,
                      source=screen or "engine")
        except Exception:                        # noqa: BLE001
            pass

    def load(self):
        """(doc, error): the file read strictly, under the store's lock.
        A missing file with no history is an empty doc (rev 0) and no
        error: nothing was ever saved. A file that cannot be read, or a
        missing file whose history folder exists (saved once, now gone), is
        an empty doc and the error in a sentence: no show fires its
        fireball then (never an FSEQ fallback), and saves are refused
        until an admin replaces it (save's replace_unreadable). Never
        raises."""
        with self._lock:
            if not self.path or not os.path.exists(self.path):
                if self.path and os.path.isdir(self.history_dir()):
                    return empty_doc(), (
                        f"{FILE} is missing although it was saved before "
                        f"(its history is in {HISTORY_DIR}), so no show "
                        f"fires its fireball until an admin restores it "
                        f"or replaces it")
                return empty_doc(), ""
            try:
                with open(self.path, encoding="utf-8-sig") as fh:
                    doc = json.load(fh)
                return read_doc(doc), ""
            except (OSError, ValueError) as e:
                return empty_doc(), (f"{FILE} could not be read ({e}), so "
                                     f"no show fires its fireball until an "
                                     f"admin replaces it")

    def save(self, show, cue, show_len_s, source, stages, base_rev,
             who="", screen="", replace_unreadable=False):
        """Save one show's entry: (ok, sentence, the new view of that show
        or None). Checked first, then refused unless `base_rev` is the
        file's revision now. A file that cannot be read (or is missing
        after it was saved before) has no revision: every save is refused
        (it would drop every other show's entry), unless the admin says
        `replace_unreadable`: then the broken file is kept in the history
        and a new file holds this show alone. Nothing is written on any
        refusal."""
        with self._lock:
            try:
                _show_name(show)
                if source not in SOURCES:
                    raise TimelineError("choose the source: the show's "
                                        "FSEQ or the timeline")
                if isinstance(show_len_s, bool) or \
                        not isinstance(show_len_s, (int, float)) or \
                        not math.isfinite(show_len_s) or show_len_s <= 0:
                    raise TimelineError("the show's length is not known")
                if not isinstance(cue, str) or not cue.strip():
                    raise TimelineError("the show's own cue is not known")
                new_stages = check_stages(stages, float(show_len_s))
            except TimelineError as e:
                text = f"Not saved: {e}".rstrip(".") + "."
                self._audit(who, screen, show, "refused", text)
                return False, text, None
            doc, err = self.load()
            if err and replace_unreadable is not True:
                text = (f"Not saved: {err}. Replacing it starts a new file "
                        f"with this show only; every other show's timeline "
                        f"in it would be lost.")
                self._audit(who, screen, show, "refused", text)
                return False, text, None
            if err:
                base_rev = doc["rev"]
            if isinstance(base_rev, bool) or \
                    not isinstance(base_rev, int) or base_rev != doc["rev"]:
                text = ("Not saved: someone saved the fireball timeline "
                        "since this screen read it. Reload it, check the "
                        "times, and save again.")
                self._audit(who, screen, show, "refused", text)
                return False, text, None
            before = entry_of(doc, show)
            after = {"source": source, "cue": cue.strip(),
                     "show_len_s": float(show_len_s),
                     "stages": new_stages,
                     "edited_by": _text(str(who or ""), "who"),
                     "edited_at": _utc(self._wall()),
                     "screen": _text(str(screen or "")[:MAX_TEXT],
                                     "the screen")}
            change = describe_change(before, after)
            new = {"fireball_timeline_format": FORMAT, "rev": doc["rev"] + 1,
                   "shows": dict(doc["shows"])}
            new["shows"][show] = after
            try:
                read_doc(new)          # never writes what it would refuse
                old = self._read_bytes()
                self._write(new)
            except (OSError, TimelineError) as e:
                text = f"Not saved: the file could not be written ({e})."
                self._note(who, screen, "save_failed", text, fault=True)
                self._audit(who, screen, show, "error", text)
                return False, text, None
            # The new file is in place: the save stands, whatever happens to
            # the history copy (review of #184: a history copy refused by
            # an antivirus made a save that WAS made say "Not saved").
            warn = ""
            try:
                # The history folder exists from the first save on: the
                # mark that a missing file was once saved (load).
                os.makedirs(self.history_dir(), exist_ok=True)
                # Only once the new file is in place: a save that failed
                # leaves no history copy behind.
                self._keep_history(doc["rev"], bool(err), old)
            except OSError as e:
                warn = (f" The copy of the version before could not be "
                        f"kept in {HISTORY_DIR} ({e}).")
                self._note(who, screen, "history_failed",
                           f"Fireball timeline saved, but{warn}", fault=True)
            what = change or "nothing changed"
            text = (f"{who or 'Someone'} saved the fireball timeline for "
                    f"{show} on the {screen or 'unknown screen'} "
                    f"(revision {new['rev']}): {what}.")
            self._note(who, screen, "saved", text)
            self._audit(who, screen, show, "accepted", what)
            for tell in list(self.on_saved):
                try:
                    tell(show, after, new["rev"])
                except Exception:                # noqa: BLE001
                    pass
            return True, f"Saved: {what}.{warn}", dict(after, rev=new["rev"])

    def _read_bytes(self):
        """The file as it is now, or None when it is not there."""
        try:
            with open(self.path, "rb") as src:
                return src.read()
        except FileNotFoundError:
            return None

    def _write(self, doc):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=1, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            # No stray temporary file after a refused replace (Windows: a
            # reader elsewhere holding the file).
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def history_dir(self):
        return os.path.join(self.folder, HISTORY_DIR)

    def _keep_history(self, rev, unreadable, data):
        """The file as it was before a save (`data`), kept in HISTORY_DIR
        once the save is in place, and the oldest beyond HISTORY_KEEP
        removed. Nothing for a file that was not there."""
        if data is None:
            return
        d = self.history_dir()
        os.makedirs(d, exist_ok=True)
        stamp = datetime.datetime.fromtimestamp(
            self._wall(), datetime.timezone.utc).strftime(
                "%Y%m%dT%H%M%S%fZ")
        name = f"{stamp}-rev{rev}{'-unreadable' if unreadable else ''}.json"
        tmp = os.path.join(d, name + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, os.path.join(d, name))
        kept = sorted(n for n in os.listdir(d) if n.endswith(".json"))
        for old in kept[:-HISTORY_KEEP]:
            try:
                os.remove(os.path.join(d, old))
            except OSError:
                pass

    def history(self):
        """The kept versions, newest first: [file name]."""
        try:
            return sorted((n for n in os.listdir(self.history_dir())
                           if n.endswith(".json")), reverse=True)
        except OSError:
            return []

    def view(self, show=None):
        """What the editor reads: the revision, the file's error, every
        show's entry, and with `show` that show's entry (the default when
        the file does not name it)."""
        doc, err = self.load()
        out = {"rev": doc["rev"], "error": err, "shows": doc["shows"],
               "stages": [{"stage": k, "word": STAGE_WORD[k],
                           "slot": STAGE_SLOT[k]} for k in STAGE_KEYS],
               "fps": FPS, "sources": list(SOURCES),
               "default_source": DEFAULT_SOURCE}
        if show is not None:
            out["show"] = show
            out["entry"] = entry_of(doc, show)
        return out
