"""Play-from cues, part 1: the cue list (PLAY_FROM_CUES design, 2026-10-30).

Per show file, a list of named points in the show to play from later: each
cue has a name, a time counted from the top of the show cue, a lead-in and
an optional note. An admin keeps the list on 2.0's Settings tab.

This module is the model, the validation and the storage. It SENDS
NOTHING: it imports nothing that sends, and nothing that starts a show
reads it in this change. Starting a show from a cue is a later change.

A cue
=====

    name       1 to 40 characters, unique in the show (case does not
               count), no em or en dashes
    tc         HH:MM:SS:FF at 30 frames a second, counted from the top of
               the show cue (00:00:00:00 is the first frame of the show
               audio, as the Art-Net timecode, MadMapper and BEYOND count)
    lead_in_s  whole seconds played before the cue, 1 to 30 (2 when not
               given): at least 1 s, so a cue right on a flame hit clears
               the 0.5 s landing guard
    note       up to 200 characters, optional
    id         given by the store, kept across renames and retimes

The play-from point (tc less the lead-in) must be at or after the top of
the show cue, and the cue must lie inside the show AUDIO (not the longer
show or video: the show audio clock cannot start past its audio's end).
At most 50 cues a show; the list is kept in time order.

Storage (the fireball timeline file's pattern, PR #184)
=======================================================

FILE in the settings folder:

    {"play_from_format": 1, "rev": 4,
     "shows": {"Fire and Ice.json": {
         "cue": "Show", "audio_len_s": 612.4,
         "cues": [{"id": "c3", "name": "Finale", "tc": "00:08:12:00",
                   "lead_in_s": 3, "note": "marketing cut"}],
         "edited_by": "Jeff", "edited_at": "2026-10-30T21:04:11Z",
         "screen": "Rack screen"}}}

  - Read strictly: an unknown key, a wrong type or a bad cue and the WHOLE
    file is refused (`read_doc`); it then reads as no cues for any show.
  - Written whole and atomically (a temporary file, fsync, os.replace).
  - Every save names the revision it was based on; a stale one is refused
    and nothing is written (two editors never overwrite each other).
  - The version before each save is kept in HISTORY_DIR (the last
    HISTORY_KEEP), named by its UTC time and revision.
  - Every save, and every refusal, writes one journal line and one audit
    event: who, from which screen, and what changed, cue by cue.
"""
import datetime
import json
import math
import os
import re
import threading
import time

NAME = "Play-from cues"
FILE = "ltcplay_play_from.json"
FORMAT = 1
HISTORY_DIR = "play_from_history"
HISTORY_KEEP = 50
FPS = 30
LEAD_IN_DEFAULT_S = 2
LEAD_IN_MIN_S = 1
LEAD_IN_MAX_S = 30
MAX_CUES = 50
MAX_NAME = 40
MAX_NOTE = 200
MAX_TEXT = 200
MAX_SHOWS = 500

_TC = re.compile(r"(\d{2}):(\d{2}):(\d{2}):(\d{2})")
_ID = re.compile(r"c[1-9]\d{0,5}")
_DOC_KEYS = {"play_from_format", "rev", "shows"}
_ENTRY_KEYS = {"cue", "audio_len_s", "cues", "edited_by", "edited_at",
               "screen"}
_CUE_KEYS = {"id", "name", "tc", "lead_in_s", "note"}
_DASHES = ("\u2013", "\u2014")


class CueError(ValueError):
    """A cue list, or the file, that is refused. str() is plain words."""


# ------------------------------------------------------------ the parts --

def parse_tc(text):
    """A time HH:MM:SS:FF at FPS frames a second as a whole number of
    frames from 00:00:00:00. CueError otherwise."""
    if not isinstance(text, str):
        raise CueError("the time is written HH:MM:SS:FF")
    m = _TC.fullmatch(text.strip())
    if not m:
        raise CueError(f"{text.strip()!r} is not a time; write it "
                       f"HH:MM:SS:FF, for example 00:01:30:00")
    h, mi, s, f = (int(x) for x in m.groups())
    if mi > 59 or s > 59:
        raise CueError(f"{text.strip()} is not a time: minutes and seconds "
                       f"go up to 59")
    if f >= FPS:
        raise CueError(f"{text.strip()} is not a time: the frames go 00 to "
                       f"{FPS - 1} at {FPS} frames a second")
    return ((h * 60 + mi) * 60 + s) * FPS + f


def tc_text(frames):
    """Whole frames from 00:00:00:00 as HH:MM:SS:FF."""
    frames = int(frames)
    f = frames % FPS
    s = frames // FPS
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{f:02d}"


def parse_lead_in(v):
    """A lead-in in whole seconds, LEAD_IN_MIN_S to LEAD_IN_MAX_S; None (not
    given) is LEAD_IN_DEFAULT_S. CueError otherwise."""
    if v is None:
        return LEAD_IN_DEFAULT_S
    if isinstance(v, bool) or not isinstance(v, (int, float)) or \
            not math.isfinite(v) or float(v) != int(v):
        raise CueError(f"the lead-in is a whole number of seconds, "
                       f"{LEAD_IN_MIN_S} to {LEAD_IN_MAX_S}")
    v = int(v)
    if v < LEAD_IN_MIN_S:
        raise CueError(f"the lead-in is at least {LEAD_IN_MIN_S} s, so a "
                       f"cue right on a flame hit is not cut by the landing "
                       f"guard")
    if v > LEAD_IN_MAX_S:
        raise CueError(f"the lead-in is at most {LEAD_IN_MAX_S} s")
    return v


def _name(v):
    if not isinstance(v, str) or not v.strip():
        raise CueError("a cue needs a name")
    v = " ".join(v.split())
    if len(v) > MAX_NAME:
        raise CueError(f"the name {v[:MAX_NAME]!r}... is longer than "
                       f"{MAX_NAME} characters")
    if any(d in v for d in _DASHES):
        raise CueError("the name has a long dash; use a plain hyphen or "
                       "none")
    return v


def _note(v):
    if v is None:
        return ""
    if not isinstance(v, str):
        raise CueError("the note is text")
    v = v.strip()
    if len(v) > MAX_NOTE:
        raise CueError(f"the note is longer than {MAX_NOTE} characters")
    return v


def _len_text(seconds):
    return tc_text(int(math.floor(seconds * FPS + 1e-9)))


def check_cues(cues, audio_len_s=None, need_ids=False):
    """The cues as given (a list of {"name", "tc", "lead_in_s", "note",
    "id"}) checked and put in time order: a list of normalised cues. With
    `audio_len_s` (the show cue's audio length in seconds) every cue must
    lie inside it. `need_ids`: every cue must carry an id (the file); else
    a cue without one is new. CueError with every problem, one sentence
    each, otherwise."""
    if not isinstance(cues, list):
        raise CueError("the cues are a list")
    if len(cues) > MAX_CUES:
        raise CueError(f"a show has at most {MAX_CUES} cues; this list has "
                       f"{len(cues)}")
    problems, names, ids, out = [], {}, set(), []
    for i, c in enumerate(cues):
        label = f"cue {i + 1}"
        if not isinstance(c, dict):
            problems.append(f"{label} is not a cue")
            continue
        try:
            name = _name(c.get("name"))
            label = name
        except CueError as e:
            problems.append(f"{label}: {e}")
            name = None
        extra = set(c) - _CUE_KEYS
        if extra:
            problems.append(f"{label}: unknown field "
                            f"{', '.join(sorted(map(str, extra)))}")
        cid = c.get("id")
        if cid is None and not need_ids:
            pass
        elif not isinstance(cid, str) or not _ID.fullmatch(cid):
            problems.append(f"{label}: the id {cid!r} is not a cue id")
            cid = None
        elif cid in ids:
            problems.append(f"{label}: the id {cid} is given twice")
            cid = None
        else:
            ids.add(cid)
        if name is not None:
            key = name.casefold()
            if key in names:
                problems.append(f"the name {name!r} is used twice: each cue "
                                f"has its own name")
            names[key] = name
        try:
            fr = parse_tc(c.get("tc"))
        except CueError as e:
            problems.append(f"{label}: {e}")
            fr = None
        try:
            lead = parse_lead_in(c.get("lead_in_s"))
        except CueError as e:
            problems.append(f"{label}: {e}")
            lead = None
        try:
            note = _note(c.get("note"))
        except CueError as e:
            problems.append(f"{label}: {e}")
            note = None
        if fr is not None and audio_len_s is not None and \
                fr / FPS >= audio_len_s:
            problems.append(f"{label}: {tc_text(fr)} is not inside the show "
                            f"audio, which is {_len_text(audio_len_s)} long")
        elif fr is not None and lead is not None and fr < lead * FPS:
            problems.append(f"{label}: with its {lead} s lead-in it would "
                            f"start before the top of the show cue; put it "
                            f"at {tc_text(lead * FPS)} or later, or shorten "
                            f"the lead-in")
        if None not in (name, fr, lead, note):
            out.append({"id": cid, "name": name, "tc": tc_text(fr),
                        "lead_in_s": lead, "note": note})
    if problems:
        raise CueError("; ".join(problems) + ".")
    out.sort(key=lambda c: (parse_tc(c["tc"]), c["name"].casefold()))
    return out


def _text(v, what):
    if not isinstance(v, str) or len(v) > MAX_TEXT:
        raise CueError(f"{what} is not a short piece of text")
    return v


def _audio_len(v, what="the show audio's length"):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or \
            not math.isfinite(v) or v <= 0:
        raise CueError(f"{what} is not a number of seconds above 0")
    return float(v)


def check_entry(show, entry):
    """One show's entry from the file, checked strictly: a normalised copy,
    or CueError naming the show."""
    try:
        if not isinstance(entry, dict):
            raise CueError("it is not an entry")
        extra = set(entry) - _ENTRY_KEYS
        if extra:
            raise CueError(f"unknown field "
                           f"{', '.join(sorted(map(str, extra)))}")
        cue = entry.get("cue")
        if not isinstance(cue, str) or not cue.strip() or \
                len(cue) > MAX_TEXT:
            raise CueError("it names no show cue")
        ln = _audio_len(entry.get("audio_len_s"))
        out = {"cue": cue, "audio_len_s": ln,
               "cues": check_cues(entry.get("cues"), ln, need_ids=True)}
        for k in ("edited_by", "edited_at", "screen"):
            out[k] = _text(entry.get(k, ""), k)
        return out
    except CueError as e:
        raise CueError(f"{show}: {e}")


def empty_doc():
    return {"play_from_format": FORMAT, "rev": 0, "shows": {}}


def _show_name(show):
    if not isinstance(show, str) or not show.strip() or \
            len(show) > MAX_TEXT or os.path.basename(show) != show or \
            show in (".", ".."):
        raise CueError(f"{show!r} is not a show file name")
    return show


def read_doc(doc):
    """The whole file's contents, checked strictly: a normalised copy, or
    CueError. One bad entry refuses the whole file."""
    if not isinstance(doc, dict):
        raise CueError("it is not a play-from cue file")
    extra = set(doc) - _DOC_KEYS
    if extra:
        raise CueError(f"unknown field {', '.join(sorted(map(str, extra)))}")
    if doc.get("play_from_format") != FORMAT:
        raise CueError(f"play_from_format is not {FORMAT}")
    rev = doc.get("rev")
    if isinstance(rev, bool) or not isinstance(rev, int) or rev < 0:
        raise CueError("rev is not a whole number")
    shows = doc.get("shows")
    if not isinstance(shows, dict) or len(shows) > MAX_SHOWS:
        raise CueError("shows is not a list of shows")
    out = {}
    for show, entry in shows.items():
        _show_name(show)
        out[show] = check_entry(show, entry)
    return {"play_from_format": FORMAT, "rev": rev, "shows": out}


def entry_of(doc, show):
    """The entry for `show` in a read doc, or an empty one for a show the
    file does not name."""
    got = (doc or {}).get("shows", {}).get(show)
    if got is not None:
        return got
    return {"cue": "", "audio_len_s": None, "cues": [], "edited_by": "",
            "edited_at": "", "screen": ""}


def give_ids(before, cues):
    """`cues` (checked) with an id for each new one: ids the show's list
    already uses (in `before`) are kept; an id not in `before` is refused
    (a page cannot claim a cue it did not read); a new cue gets the next
    free number."""
    known = {c["id"] for c in before}
    used = [int(c["id"][1:]) for c in before]
    for c in cues:
        if c["id"] is not None and c["id"] not in known:
            raise CueError(f"{c['name']}: the id {c['id']} is not a cue in "
                           f"this list; reload it")
    nxt = max(used, default=0) + 1
    out = []
    for c in cues:
        if c["id"] is None:
            c = dict(c, id=f"c{nxt}")
            nxt += 1
        out.append(c)
    return out


def describe_change(before, after):
    """What changed between two cue lists, in plain words, for the journal
    and the audit log: "" when nothing did."""
    out = []
    b = {c["id"]: c for c in before}
    a = {c["id"]: c for c in after}
    for c in after:
        if c["id"] not in b:
            out.append(f"{c['name']} added at {c['tc']}, lead-in "
                       f"{c['lead_in_s']} s")
    for c in before:
        if c["id"] not in a:
            out.append(f"{c['name']} deleted (was at {c['tc']})")
    for c in after:
        old = b.get(c["id"])
        if old is None:
            continue
        ch = []
        if old["name"] != c["name"]:
            ch.append(f"renamed to {c['name']}")
        if old["tc"] != c["tc"]:
            ch.append(f"time {old['tc']} to {c['tc']}")
        if old["lead_in_s"] != c["lead_in_s"]:
            ch.append(f"lead-in {old['lead_in_s']} s to {c['lead_in_s']} s")
        if old["note"] != c["note"]:
            ch.append("note changed")
        if ch:
            out.append(f"{old['name']} " + ", ".join(ch))
    return "; ".join(out)


def audio_note(entry, audio_len_s):
    """A sentence when the show audio's length now is not the one the list
    was saved against (a new render), naming each cue now past its end;
    "" otherwise."""
    if not entry.get("cues") or entry.get("audio_len_s") is None or \
            audio_len_s is None:
        return ""
    was = entry["audio_len_s"]
    if abs(was - audio_len_s) < 1.0 / FPS:
        return ""
    past = [c["name"] for c in entry["cues"]
            if parse_tc(c["tc"]) / FPS >= audio_len_s]
    text = (f"Check the cues: the show audio is {_len_text(audio_len_s)} "
            f"long now, {_len_text(was)} when the list was saved.")
    if past:
        text += (" Past its end now: " + ", ".join(past) +
                 ". Retime or delete them, then save.")
    return text


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
        self._lock = threading.Lock()

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
                      action="play-from cues", target=target,
                      result=result, reason=reason,
                      source=screen or "engine")
        except Exception:                        # noqa: BLE001
            pass

    def refused(self, who, screen, show, text):
        """A refusal: one journal line and one audit event."""
        self._note(who, screen, "refused",
                   f"{who or 'Someone'}'s play-from cue save for {show!r} "
                   f"on the {screen or 'unknown screen'} was refused. {text}")
        self._audit(who, screen, str(show)[:MAX_TEXT], "refused", text)

    def load(self):
        """(doc, error): the file read strictly. A missing file is an
        empty doc (rev 0) and no error. A file that cannot be read is an
        empty doc (rev 0) and the error in a sentence. Never raises."""
        if not self.path or not os.path.exists(self.path):
            return empty_doc(), ""
        try:
            with open(self.path, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
            return read_doc(doc), ""
        except (OSError, ValueError) as e:
            return empty_doc(), (f"{FILE} could not be read ({e}), so no "
                                 f"show has play-from cues until the list "
                                 f"is saved again")

    def save(self, show, cue, audio_len_s, cues, base_rev, who="",
             screen=""):
        """Save one show's cue list: (ok, sentence, the new entry with its
        revision, or None). Checked first, then refused unless `base_rev`
        is the file's revision now (a broken or missing file is revision
        0). Nothing is written on any refusal."""
        with self._lock:
            doc, err = self.load()
            try:
                _show_name(show)
                if not isinstance(cue, str) or not cue.strip():
                    raise CueError("the show's own cue is not known")
                ln = _audio_len(audio_len_s, "the show audio's length")
                new_cues = check_cues(cues, ln)
                if isinstance(base_rev, bool) or \
                        not isinstance(base_rev, int) or \
                        base_rev != doc["rev"]:
                    raise CueError(
                        "someone saved the play-from cues since this screen "
                        "read them. Reload, check the cues, and save again")
                before = entry_of(doc, show)
                new_cues = give_ids(before["cues"], new_cues)
            except CueError as e:
                text = f"Not saved: {e}".rstrip(".") + "."
                self.refused(who, screen, show, text)
                return False, text, None
            after = {"cue": cue.strip(), "audio_len_s": ln,
                     "cues": new_cues,
                     "edited_by": str(who or "")[:MAX_TEXT],
                     "edited_at": _utc(self._wall()),
                     "screen": str(screen or "")[:MAX_TEXT]}
            change = describe_change(before["cues"], new_cues)
            new = {"play_from_format": FORMAT, "rev": doc["rev"] + 1,
                   "shows": dict(doc["shows"])}
            new["shows"][show] = after
            try:
                read_doc(new)          # never writes what it would refuse
                self._keep_history(doc["rev"], bool(err))
                self._write(new)
            except (OSError, CueError) as e:
                text = f"Not saved: the file could not be written ({e})."
                self._note(who, screen, "save_failed", text, fault=True)
                self._audit(who, screen, show, "error", text)
                return False, text, None
            what = change or "nothing changed"
            text = (f"{who or 'Someone'} saved the play-from cues for "
                    f"{show} on the {screen or 'unknown screen'} "
                    f"(revision {new['rev']}): {what}.")
            self._note(who, screen, "saved", text)
            self._audit(who, screen, show, "accepted", what)
            return True, f"Saved: {what}.", dict(after, rev=new["rev"])

    def _write(self, doc):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def history_dir(self):
        return os.path.join(self.folder, HISTORY_DIR)

    def _keep_history(self, rev, unreadable):
        """The file as it is now, copied into HISTORY_DIR before it is
        replaced, and the oldest beyond HISTORY_KEEP removed. Nothing for
        a file that is not there."""
        if not self.path or not os.path.exists(self.path):
            return
        d = self.history_dir()
        os.makedirs(d, exist_ok=True)
        stamp = datetime.datetime.fromtimestamp(
            self._wall(), datetime.timezone.utc).strftime(
                "%Y%m%dT%H%M%S%fZ")
        name = f"{stamp}-rev{rev}{'-unreadable' if unreadable else ''}.json"
        with open(self.path, "rb") as src:
            data = src.read()
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
        """What the editor reads: the revision, the file's error, the
        rules, and with `show` that show's entry (an empty one when the
        file does not name it)."""
        doc, err = self.load()
        out = {"rev": doc["rev"], "error": err, "fps": FPS,
               "lead_in_default_s": LEAD_IN_DEFAULT_S,
               "lead_in_min_s": LEAD_IN_MIN_S,
               "lead_in_max_s": LEAD_IN_MAX_S, "max_cues": MAX_CUES,
               "max_name": MAX_NAME, "max_note": MAX_NOTE,
               "shows": sorted(doc["shows"])}
        if show is not None:
            out["show"] = show
            out["entry"] = entry_of(doc, show)
        return out
