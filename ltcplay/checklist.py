"""The Morning and Shutdown checklists, recorded for audit (Jeff, 2026-10-17).

Steps come from ltcplay_morning.json ("checklists"; Andy supplies them, an
empty sample ships). Ticks reset each night. Every tick, untick and note is
one record in <logs>/checklists/records.jsonl, APPEND ONLY and hash
chained: each record carries `prev` (the hash of the record before it) and
`hash` (SHA-256 over `prev` plus the record's own canonical JSON), so an
edit, an insertion or a deletion anywhere breaks the chain and verify()
says where. Nothing is ever edited in place; a correction is a new record.

A record:
  seq, prev, hash, utc (ISO 8601 Z), local (ISO 8601 with offset), tz,
  show_date (YYYY-MM-DD, the scheduler's night when one is attached),
  checklist, step, step_text (as it read at the time), state ("done",
  "undone", "note" or "routine_complete"), note, who (a name from the
  configured list, required for a tick), device, ip, surface.

A completed routine writes the day's record beside the chain:
  records/<show_date>/checklist_<show_date>.json  every record of the day
  records/<show_date>/checklist_<show_date>.pdf   the readable one (pdfmini)
and writes them again on every later change of that day. These live under
logs/checklists, which journal.prune never touches (it prunes nights/
only) and the diagnostics bundle lists only as a count.

The tally forwarder (TallySender) is OFF until "tally" in ltcplay_morning
.json has "enabled": true and an https "url". The bearer token is read
from "token_file" at send time and never shown or logged. Payloads wait on
disk in outbox/ until delivered and are retried with backoff.
"""
import datetime
import hashlib
import io
import json
import os
import re
import threading
import time
import zipfile

from . import pdfmini

RECORDS_FILE = "records.jsonl"
RECORDS_DIR = "records"
OUTBOX_DIR = "outbox"
GENESIS = "0" * 64
CHECKLIST_KEYS = frozenset(("title", "steps"))
STEP_KEYS = frozenset(("id", "text"))
TALLY_KEYS = frozenset(("enabled", "url", "token_file", "timeout_s"))
STATES = ("done", "undone", "note", "routine_complete")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$")
NOTE_MAX = 500
TEXT_MAX = 300
NAMES_MAX = 50
DEFAULT_CHECKLISTS = {"morning": {"title": "Morning", "steps": []},
                      "shutdown": {"title": "Shutdown", "steps": []}}
TALLY_DEFAULTS = {"enabled": False, "url": "", "token_file": "",
                  "timeout_s": 10.0}
TALLY_BACKOFF_S = (5.0, 15.0, 60.0, 300.0)
PAYLOAD_VERSION = 1


class ChecklistConfigError(ValueError):
    pass


def _only(d, keys, where):
    unknown = sorted(k for k in d if k not in keys)
    if unknown:
        raise ChecklistConfigError(
            f"{where} has no setting {', '.join(repr(k) for k in unknown)}; "
            f"it takes {', '.join(sorted(keys))}.")


def parse_checklists(doc, where):
    """{"morning": {"title": "Morning", "steps": [{"id": "gas", "text":
    "Gas on at the manifold"}, ...]}, "shutdown": {...}}. Any number of
    lists; the two defaults with no steps when the key is absent."""
    if doc is None:
        return json.loads(json.dumps(DEFAULT_CHECKLISTS))
    if not isinstance(doc, dict) or not doc:
        raise ChecklistConfigError(f"{where}: checklists has to be an "
                                   f"object of lists, like morning and "
                                   f"shutdown.")
    out = {}
    for cid, c in doc.items():
        w = f"{where}: checklists[{cid!r}]"
        if not ID_RE.match(str(cid)):
            raise ChecklistConfigError(f"{w}: the id has to be letters, "
                                       f"digits, dot, dash or underscore.")
        if not isinstance(c, dict):
            raise ChecklistConfigError(f"{w} has to be an object with title "
                                       f"and steps.")
        _only(c, CHECKLIST_KEYS, w)
        title = c.get("title", cid)
        if not isinstance(title, str) or not title.strip():
            raise ChecklistConfigError(f"{w}: title has to be words.")
        steps = c.get("steps", [])
        if not isinstance(steps, list):
            raise ChecklistConfigError(f"{w}: steps has to be a list.")
        seen, got = set(), []
        for i, s in enumerate(steps):
            ws = f"{w}: steps[{i + 1}]"
            if isinstance(s, str):
                s = {"id": f"step-{i + 1}", "text": s}
            if not isinstance(s, dict):
                raise ChecklistConfigError(f"{ws} has to be text, or an "
                                           f"object with id and text.")
            _only(s, STEP_KEYS, ws)
            sid = s.get("id", f"step-{i + 1}")
            if not isinstance(sid, str) or not ID_RE.match(sid) or sid in seen:
                raise ChecklistConfigError(f"{ws}: id has to be its own, "
                                           f"letters, digits, dot, dash or "
                                           f"underscore.")
            text = s.get("text")
            if not isinstance(text, str) or not text.strip() or \
                    len(text) > TEXT_MAX:
                raise ChecklistConfigError(f"{ws}: text has to be words, up "
                                           f"to {TEXT_MAX} characters.")
            seen.add(sid)
            got.append({"id": sid, "text": text.strip()})
        out[str(cid)] = {"title": title.strip(), "steps": got}
    return out


def parse_names(doc, where):
    """The names (or initials) a tick may be made under. Empty means
    nobody can tick yet, and the page says so."""
    if doc is None:
        return []
    if not isinstance(doc, list) or len(doc) > NAMES_MAX:
        raise ChecklistConfigError(f"{where}: names has to be a list of up "
                                   f"to {NAMES_MAX} names or initials.")
    out = []
    for n in doc:
        if not isinstance(n, str) or not n.strip() or len(n) > 40:
            raise ChecklistConfigError(f"{where}: every name has to be "
                                       f"words, up to 40 characters.")
        if n.strip() in out:
            raise ChecklistConfigError(f"{where}: {n.strip()!r} is listed "
                                       f"twice.")
        out.append(n.strip())
    return out


def parse_tally(doc, where):
    out = dict(TALLY_DEFAULTS)
    if doc is None:
        return out
    if not isinstance(doc, dict):
        raise ChecklistConfigError(f"{where}: tally has to be an object.")
    _only(doc, TALLY_KEYS, f"{where}: tally")
    en = doc.get("enabled", False)
    if en is not True and en is not False:
        raise ChecklistConfigError(f"{where}: tally.enabled has to be true "
                                   f"or false.")
    url = doc.get("url", "")
    if not isinstance(url, str):
        raise ChecklistConfigError(f"{where}: tally.url has to be text.")
    url = url.strip()
    if url and not url.lower().startswith("https://"):
        raise ChecklistConfigError(f"{where}: tally.url has to start with "
                                   f"https:// (nothing is sent in clear).")
    if en and not url:
        raise ChecklistConfigError(f"{where}: tally.enabled is true but "
                                   f"tally.url is empty. Set the URL, or "
                                   f"leave enabled false.")
    tf = doc.get("token_file", "")
    if not isinstance(tf, str):
        raise ChecklistConfigError(f"{where}: tally.token_file is the path "
                                   f"of a local file holding the bearer "
                                   f"token.")
    to = doc.get("timeout_s", 10.0)
    if isinstance(to, bool) or not isinstance(to, (int, float)) or \
            not 1 <= to <= 120:
        raise ChecklistConfigError(f"{where}: tally.timeout_s has to be 1 "
                                   f"to 120.")
    out.update(enabled=en, url=url, token_file=tf.strip(), timeout_s=float(to))
    return out


# -------------------------------------------------------------- the chain --

def canonical(rec):
    """The bytes a record's hash covers: every field but `hash`, sorted
    keys, no spaces, UTF-8."""
    d = {k: v for k, v in rec.items() if k != "hash"}
    return json.dumps(d, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def record_hash(rec):
    h = hashlib.sha256()
    h.update(str(rec.get("prev", "")).encode("ascii", "replace"))
    h.update(canonical(rec))
    return h.hexdigest()


def verify_chain(records):
    """(ok, count, problem sentence or ""). Every record's hash must be
    right and chain to the one before it, seq must count from 1."""
    prev = GENESIS
    for i, r in enumerate(records, 1):
        if not isinstance(r, dict):
            return False, i - 1, f"record {i} is not an object"
        if r.get("seq") != i:
            return False, i - 1, (f"record {i} has seq {r.get('seq')!r}: a "
                                  f"record is missing or out of order")
        if r.get("prev") != prev:
            return False, i - 1, (f"record {i} does not chain to record "
                                  f"{i - 1}: a record before it was changed, "
                                  f"removed or inserted")
        if r.get("hash") != record_hash(r):
            return False, i - 1, f"record {i} was changed after it was written"
        prev = r["hash"]
    return True, len(records), ""


class Chain:
    """records.jsonl: read once, appended under a lock, each line one
    record. A line that will not parse stops the chain there (verify says
    so) but never loses the lines before it."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self.records = []
        self.read_problem = ""
        self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except FileNotFoundError:
            return
        except OSError as e:
            self.read_problem = f"{self.path} could not be read: {e}"
            return
        for n, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                self.records.append(json.loads(line))
            except ValueError:
                self.read_problem = (f"line {n} of {self.path} is not a "
                                     f"record (a torn write?)")
                break

    def append(self, fields):
        """Chain `fields` (a dict without seq, prev or hash) onto the end,
        write it, return the record. Raises OSError when the disk refuses:
        nothing is then kept in memory either, so the file and the chain
        never disagree."""
        with self._lock:
            prev = self.records[-1]["hash"] if self.records else GENESIS
            rec = dict(fields)
            rec["seq"] = len(self.records) + 1
            rec["prev"] = prev
            rec["hash"] = record_hash(rec)
            line = json.dumps(rec, ensure_ascii=False, sort_keys=True)
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self.records.append(rec)
            return rec

    def verify(self):
        ok, n, why = verify_chain(list(self.records))
        if ok and self.read_problem:
            return False, n, self.read_problem
        return ok, n, why

    def for_date(self, show_date):
        return [r for r in self.records if r.get("show_date") == show_date]

    def dates(self):
        return sorted({r.get("show_date") for r in self.records
                       if isinstance(r.get("show_date"), str)})


# -------------------------------------------------------------- the tally --

def _https_post(url, data, headers, timeout):
    """The default transport: one HTTPS POST. (status, text)."""
    import urllib.error
    import urllib.request
    if not url.lower().startswith("https://"):
        raise ValueError("the tally URL has to be https")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(400).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(400).decode("utf-8", "replace")


class TallySender:
    """Forwards checklist records to Jeff's tally server, once configured.
    `transport(url, data, headers, timeout) -> (status, text)` is
    injectable for the selftest; the default is an HTTPS POST."""

    def __init__(self, cfg, outbox, journal=None, transport=None,
                 wall=time.time, clock=time.monotonic):
        self.cfg = dict(cfg)
        self.outbox = outbox
        self._journal = journal
        self._transport = transport or _https_post
        self._wall = wall
        self._clock = clock
        self._lock = threading.Lock()
        self.sent = 0
        self.failures = 0
        self.last_error = ""
        self.last_sent_at = None
        self._next_try = 0.0
        self._fails_in_a_row = 0
        self._seq = 0

    @property
    def enabled(self):
        return self.cfg.get("enabled") is True and bool(self.cfg.get("url"))

    def _note(self, text, **f):
        if self._journal is None:
            return
        try:
            self._journal(text, **f)
        except Exception:
            pass

    def payload(self, kind, body):
        return {"v": PAYLOAD_VERSION, "kind": kind,
                "sent_at": datetime.datetime.fromtimestamp(
                    self._wall(), datetime.timezone.utc).isoformat()
                .replace("+00:00", "Z"),
                "source": "LTC Player", kind.split("_", 1)[1]: body}

    def enqueue(self, kind, body):
        """Put one payload in the outbox. Nothing is written while the
        tally is off: there is nowhere for it to go, and the chain itself
        is the record."""
        if not self.enabled:
            return False
        doc = self.payload(kind, body)
        with self._lock:
            self._seq += 1
            os.makedirs(self.outbox, exist_ok=True)
            name = f"{int(self._wall() * 1000):013d}_{self._seq:06d}.json"
            tmp = os.path.join(self.outbox, name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, os.path.join(self.outbox, name))
        return True

    def pending(self):
        try:
            return sorted(n for n in os.listdir(self.outbox)
                          if n.endswith(".json"))
        except OSError:
            return []

    def _token(self):
        tf = self.cfg.get("token_file") or ""
        if not tf:
            return ""
        with open(tf, encoding="utf-8") as fh:
            return fh.read().strip()

    def send_pending(self, now=None, force=False):
        """Try the outbox, oldest first, stopping at the first failure.
        Returns how many went. Never raises; never logs the token."""
        if not self.enabled:
            return 0
        now = self._clock() if now is None else now
        if not force and now < self._next_try:
            return 0
        went = 0
        for name in self.pending():
            path = os.path.join(self.outbox, name)
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
                headers = {"Content-Type": "application/json",
                           "User-Agent": "LTC Player checklist tally"}
                tok = self._token()
                if tok:
                    headers["Authorization"] = "Bearer " + tok
                status, text = self._transport(self.cfg["url"], data,
                                               headers, self.cfg["timeout_s"])
            except Exception as e:
                status, text = 0, f"{type(e).__name__}: {e}"
            if 200 <= status < 300:
                try:
                    os.remove(path)
                except OSError:
                    pass
                went += 1
                self.sent += 1
                self.last_sent_at = self._wall()
                self._fails_in_a_row = 0
                if self.last_error:
                    self._note(f"Tally: delivering again to the tally server "
                               f"after {self.failures} failure(s).",
                               action="tally", outcome="recovered")
                    self.last_error = ""
                continue
            self.failures += 1
            why = (f"HTTP {status}: {text[:120]}" if status else text[:200])
            why = re.sub(r"(?i)bearer\s+\S+", "Bearer [hidden]", why)
            if why != self.last_error:
                self.last_error = why
                self._note(f"Tally: a record could not be delivered to the "
                           f"tally server ({why}). It waits in the outbox "
                           f"and is retried.", action="tally",
                           outcome="failed", fault=True)
            self._fails_in_a_row += 1
            back = TALLY_BACKOFF_S[min(self._fails_in_a_row,
                                       len(TALLY_BACKOFF_S)) - 1]
            self._next_try = now + back
            break
        return went

    def status(self):
        return {"enabled": self.enabled,
                "configured": bool(self.cfg.get("url")),
                "url": self.cfg.get("url") or "",
                "token_file_set": bool(self.cfg.get("token_file")),
                "queued": len(self.pending()), "sent": self.sent,
                "failures": self.failures, "last_error": self.last_error,
                "last_sent_at": (time.strftime("%Y-%m-%d %H:%M:%S",
                                               time.localtime(
                                                   self.last_sent_at))
                                 if self.last_sent_at else None)}


# ------------------------------------------------------------ the service --

def _iso_utc(t):
    return datetime.datetime.fromtimestamp(
        t, datetime.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def _iso_local(t):
    return datetime.datetime.fromtimestamp(t).astimezone().isoformat(
        timespec="seconds")


def _local_clock(iso):
    """HH:MM:SS from a local ISO stamp, for the page and the PDF."""
    m = re.search(r"T(\d\d:\d\d:\d\d)", str(iso or ""))
    return m.group(1) if m else str(iso or "")


class ChecklistService:
    """The engine's checklist routes, state and exports."""

    def __init__(self, cfg, folder, night=None, journal=None, wall=time.time,
                 clock=time.monotonic, transport=None, threaded=True):
        self.cfg = cfg                      # MorningConfig (checklists, names)
        self.folder = folder
        self._night = night                 # () -> date or None
        self._journal = journal
        self._wall = wall
        self._clock = clock
        os.makedirs(folder, exist_ok=True)
        self.chain = Chain(os.path.join(folder, RECORDS_FILE))
        self.tally = TallySender(cfg.tally, os.path.join(folder, OUTBOX_DIR),
                                 journal=journal, transport=transport,
                                 wall=wall, clock=clock)
        self.write_errors = []
        self._lock = threading.Lock()

    def _note(self, text, **f):
        if self._journal is None:
            return
        try:
            self._journal(text, **f)
        except Exception:
            pass

    # -- which night --------------------------------------------------------
    def show_date(self):
        d = None
        if self._night is not None:
            try:
                d = self._night()
            except Exception:
                d = None
        if d is None:
            d = datetime.date.fromtimestamp(self._wall())
        return d.isoformat() if hasattr(d, "isoformat") else str(d)

    # -- state --------------------------------------------------------------
    def state(self, show_date):
        """{checklist id: {step id: {"done", "who", "at", "note"}}} replayed
        from the day's records, in order."""
        out = {}
        for r in self.chain.for_date(show_date):
            cl = out.setdefault(r.get("checklist"), {})
            st = cl.setdefault(r.get("step"), {"done": False, "who": "",
                                               "at": "", "note": ""})
            state = r.get("state")
            if state == "done":
                st.update(done=True, who=r.get("who", ""),
                          at=r.get("local", ""))
            elif state == "undone":
                st.update(done=False, who=r.get("who", ""),
                          at=r.get("local", ""))
            elif state == "note":
                st["note"] = r.get("note", "")
                st["note_who"] = r.get("who", "")
        return out

    def complete(self, cid, show_date, st=None):
        c = self.cfg.checklists.get(cid)
        if c is None or not c["steps"]:
            return False
        st = st if st is not None else self.state(show_date).get(cid, {})
        return all(st.get(s["id"], {}).get("done") for s in c["steps"])

    def view(self):
        today = self.show_date()
        state = self.state(today)
        lists = []
        for cid, c in self.cfg.checklists.items():
            st = state.get(cid, {})
            steps = []
            for s in c["steps"]:
                x = st.get(s["id"], {})
                steps.append({"id": s["id"], "text": s["text"],
                              "done": bool(x.get("done")),
                              "who": x.get("who", ""),
                              "at": _local_clock(x.get("at", "")),
                              "note": x.get("note", "")})
            lists.append({"id": cid, "title": c["title"], "steps": steps,
                          "complete": self.complete(cid, today, st),
                          "done": sum(1 for s in steps if s["done"]),
                          "total": len(steps)})
        ok, n, why = self.chain.verify()
        return {"show_date": today, "checklists": lists,
                "names": list(self.cfg.names),
                "verify": {"ok": ok, "records": n, "problem": why},
                "tally": self.tally.status(),
                "dates": self.chain.dates(),
                "write_errors": list(self.write_errors[-3:]),
                "folder": self.folder}

    # -- records ------------------------------------------------------------
    def record(self, cid, sid, state, who, device, ip, surface, note=""):
        """Append one record and forward it. (ok, record or sentence)."""
        c = self.cfg.checklists.get(cid)
        if c is None:
            return False, f"There is no checklist called {cid!r}."
        step = next((s for s in c["steps"] if s["id"] == sid), None)
        if step is None and state != "routine_complete":
            return False, f"{c['title']} has no step {sid!r}."
        if state not in STATES:
            return False, f"{state!r} is not a checklist state."
        who = " ".join(str(who or "").split())
        if who not in self.cfg.names:
            if not self.cfg.names:
                return False, ("No names are set up yet: add the people who "
                               "may tick to names in ltcplay_morning.json.")
            return False, (f"Pick a name from the list before ticking "
                           f"({', '.join(self.cfg.names)}).")
        note = " ".join(str(note or "").split())[:NOTE_MAX]
        if state == "note" and not note:
            return False, "A note needs some words."
        t = self._wall()
        local = _iso_local(t)
        rec = {"utc": _iso_utc(t), "local": local,
               "tz": time.strftime("%Z", time.localtime(t)),
               "show_date": self.show_date(), "checklist": cid,
               "checklist_title": c["title"],
               "step": sid if step else "", "step_text": step["text"] if step
               else "", "state": state, "note": note, "who": who,
               "device": str(device or ""), "ip": str(ip or ""),
               "surface": str(surface or "")}
        try:
            rec = self.chain.append(rec)
        except OSError as e:
            why = f"The checklist record could NOT be written: {e}"
            self.write_errors.append(why)
            self._note(f"Checklist: {why}", action="checklist",
                       outcome="failed", fault=True)
            return False, why
        self.tally.enqueue("checklist_record", rec)
        return True, rec

    def post(self, name, body, device, who_surface, local):
        """The page's routes: checklist-tick, checklist-note,
        checklist-verify, checklist-tally-retry. `who_surface` is the
        surface name the engine gives the press (Rack screen, iPad at ...)."""
        body = body if isinstance(body, dict) else {}
        ip = str(device).rsplit(" at ", 1)[-1] if " at " in str(device) \
            else "127.0.0.1"
        if name == "checklist-verify":
            ok, n, why = self.chain.verify()
            return 200, {"ok": ok, "records": n, "problem": why,
                         "text": (f"The record chain is intact: {n} "
                                  f"record(s)." if ok else
                                  f"The record chain is NOT intact: {why}.")}
        if name == "checklist-tally-retry":
            if not self.tally.enabled:
                return 409, {"error": "The tally server is not switched on "
                                      "(tally.enabled in "
                                      "ltcplay_morning.json)."}
            n = self.tally.send_pending(force=True)
            return 200, {"ok": True, "text": f"{n} record(s) delivered; "
                                             f"{len(self.tally.pending())} "
                                             f"still waiting.",
                         "tally": self.tally.status()}
        if name in ("checklist-tick", "checklist-note"):
            cid = str(body.get("checklist") or "")
            sid = str(body.get("step") or "")
            who = body.get("who")
            if name == "checklist-tick":
                state = "done" if body.get("done") is not False else "undone"
            else:
                state = "note"
            ok, got = self.record(cid, sid, state, who, device, ip,
                                  who_surface, note=body.get("note", ""))
            if not ok:
                return 409, {"error": got}
            text = {"done": "ticked", "undone": "unticked",
                    "note": "noted"}[state]
            self._note(f"Checklist {got['checklist_title']}: {who} {text} "
                       f"\"{got['step_text']}\" on the {device}"
                       f"{': ' + got['note'] if got['note'] else ''}.",
                       action="checklist", outcome=state, who=who,
                       screen=device)
            self._after_change(cid, got["show_date"], who, device, ip,
                               who_surface)
            return 200, {"ok": True, "text": f"{got['step_text']}: {text} by "
                                             f"{who} at "
                                             f"{_local_clock(got['local'])}.",
                         "record": got["seq"]}
        return 404, {"error": "no such thing here"}

    def _after_change(self, cid, show_date, who, device, ip, surface):
        """A completed routine gets its routine_complete record once per
        completion, and the day's files are (re)written on every change
        once the day has a completed routine."""
        if self.complete(cid, show_date):
            already = any(r.get("state") == "routine_complete" and
                          r.get("checklist") == cid for r in
                          self.chain.for_date(show_date))
            if not already:
                ok, rec = self.record(cid, "", "routine_complete", who,
                                      device, ip, surface)
                if ok:
                    self.tally.enqueue("checklist_routine", {
                        "show_date": show_date, "checklist": cid,
                        "title": rec["checklist_title"],
                        "completed_utc": rec["utc"],
                        "completed_local": rec["local"], "who": who,
                        "steps": self._steps_for(cid, show_date)})
                    self._note(f"Checklist {rec['checklist_title']} "
                               f"complete for {show_date}: every step "
                               f"ticked. The day's record is written.",
                               action="checklist", outcome="complete",
                               who=who, screen=device)
        if any(r.get("state") == "routine_complete"
               for r in self.chain.for_date(show_date)):
            self.write_day(show_date)
        try:
            self.tally.send_pending()
        except Exception:
            pass

    def _steps_for(self, cid, show_date):
        st = self.state(show_date).get(cid, {})
        c = self.cfg.checklists.get(cid) or {"steps": []}
        return [{"id": s["id"], "text": s["text"],
                 "done": bool(st.get(s["id"], {}).get("done")),
                 "who": st.get(s["id"], {}).get("who", ""),
                 "at_local": st.get(s["id"], {}).get("at", ""),
                 "note": st.get(s["id"], {}).get("note", "")}
                for s in c["steps"]]

    def day_dir(self, show_date):
        return os.path.join(self.folder, RECORDS_DIR, show_date)

    def day_json(self, show_date):
        recs = self.chain.for_date(show_date)
        ok, n, why = self.chain.verify()
        return {"checklist_record": 1, "show_date": show_date,
                "written": _iso_local(self._wall()),
                "routines": [{"id": cid, "title": c["title"],
                              "complete": self.complete(cid, show_date),
                              "steps": self._steps_for(cid, show_date)}
                             for cid, c in self.cfg.checklists.items()],
                "records": recs,
                "chain_verified": {"ok": ok, "records": n, "problem": why}}

    def write_day(self, show_date):
        """records/<date>/checklist_<date>.json and .pdf. Never raises;
        a failure is journaled and remembered for the page."""
        d = self.day_dir(show_date)
        try:
            os.makedirs(d, exist_ok=True)
            doc = self.day_json(show_date)
            base = os.path.join(d, f"checklist_{show_date}")
            for ext, data in ((".json", (json.dumps(doc, indent=2,
                                                    ensure_ascii=False)
                                         + "\n").encode("utf-8")),
                              (".pdf", self.pdf_for_date(show_date))):
                tmp = base + ext + ".tmp"
                with open(tmp, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, base + ext)
            return base
        except OSError as e:
            why = f"The day record for {show_date} could NOT be written: {e}"
            self.write_errors.append(why)
            self._note(f"Checklist: {why}", action="checklist",
                       outcome="failed", fault=True)
            return None

    def tick(self, now=None):
        try:
            self.tally.send_pending(now)
        except Exception:
            pass

    def close(self):
        pass

    # -- the PDFs -----------------------------------------------------------
    def _day_pages(self, doc, show_date, pages_out):
        """Lay one day out onto pages added to `doc`; returns the number
        of pages used. Pure layout: no disk, no network."""
        w, h = doc.size
        x0, x1 = pdfmini.MARGIN, w - pdfmini.MARGIN
        state = {"page": None, "y": 0.0, "n": 0}

        def new_page():
            p = doc.new_page()
            state["page"], state["n"] = p, state["n"] + 1
            state["y"] = h - pdfmini.MARGIN
            pages_out.append(p)
            p.text(x0, h - 30, 8, f"Checklist record, {show_date}", grey=0.4)
            p.text_right(x1, h - 30, 8, "Jeff Holmes Presents, LTC Player",
                         grey=0.4)
            return p

        def need(dy):
            if state["page"] is None or state["y"] - dy < pdfmini.MARGIN + 20:
                new_page()

        def line(text, size=10, bold=False, indent=0.0, grey=0.0, gap=1.35):
            for ln in pdfmini.wrap(text, size, x1 - x0 - indent):
                need(size * gap)
                state["y"] -= size * gap
                state["page"].text(x0 + indent, state["y"], size, ln,
                                   bold=bold, grey=grey)

        def rule():
            need(8)
            state["y"] -= 5
            state["page"].rule(x0, state["y"], x1, state["y"])
            state["y"] -= 3

        day = self.day_json(show_date)
        new_page()
        line("Checklist record", 20, bold=True, gap=1.1)
        line(f"Show date {show_date}. Written {_local_clock(day['written'])} "
             f"local time on {day['written'][:10]}.", 10, grey=0.3)
        state["y"] -= 6
        for r in day["routines"]:
            rule()
            done = sum(1 for s in r["steps"] if s["done"])
            line(f"{r['title']}: {'COMPLETE' if r['complete'] else 'not complete'}"
                 f" ({done} of {len(r['steps'])} steps ticked)", 13, bold=True)
            if not r["steps"]:
                line("No steps are set up for this routine yet.", 10,
                     indent=12, grey=0.3)
            for i, s in enumerate(r["steps"], 1):
                mark = "[x]" if s["done"] else "[ ]"
                who = (f"  {s['who']} at {_local_clock(s['at_local'])}"
                       if s["done"] and s["who"] else "")
                line(f"{mark} {i}. {s['text']}{who}", 10, indent=12)
                if s["note"]:
                    line(f"Note: {s['note']}", 9, indent=36, grey=0.25)
        rule()
        line("Record trail (every tick, untick and note, in order)", 12,
             bold=True)
        recs = day["records"]
        if not recs:
            line("No records for this date.", 10, indent=12, grey=0.3)
        for r in recs:
            what = {"done": "ticked", "undone": "unticked", "note": "noted",
                    "routine_complete": "routine complete"}.get(
                r.get("state"), str(r.get("state")))
            line(f"#{r.get('seq')}  {_local_clock(r.get('local'))} local "
                 f"({r.get('utc')})  {r.get('checklist_title')}: "
                 f"{what} {r.get('step_text') or ''}  by {r.get('who')} on "
                 f"the {r.get('device')} ({r.get('ip')})"
                 f"{'  Note: ' + r['note'] if r.get('note') else ''}", 8.5,
                 indent=12)
        rule()
        v = day["chain_verified"]
        line(f"Hash chain verification: {'INTACT' if v['ok'] else 'FAILED'}, "
             f"{v['records']} record(s) in the chain"
             f"{'' if v['ok'] else ', ' + v['problem']}.", 10, bold=True)
        if recs:
            line(f"Last record hash: {recs[-1].get('hash')}", 8, grey=0.3)
        return state["n"]

    def pdf_for_date(self, show_date):
        """One day's readable record, as PDF bytes."""
        doc = pdfmini.Document(title=f"Checklist record {show_date}")
        self._day_pages(doc, show_date, [])
        return doc.build()

    def combined_pdf(self, dates):
        """Every date in `dates`, one PDF with a table of contents."""
        doc = pdfmini.Document(title="Checklist records")
        w, h = doc.size
        x0, x1 = pdfmini.MARGIN, w - pdfmini.MARGIN
        toc = doc.new_page()
        toc.text(x0, h - 30, 8, "Checklist records", grey=0.4)
        toc.text(x0, h - 80, 20, "Checklist records", bold=True)
        toc.text(x0, h - 100, 10, f"{len(dates)} day(s): "
                                  f"{dates[0] if dates else ''}"
                                  f"{' to ' + dates[-1] if len(dates) > 1 else ''}",
                 grey=0.3)
        toc.text(x0, h - 130, 12, "Contents", bold=True)
        entries = []
        page_no = 2
        for d in dates:
            n = self._day_pages(doc, d, [])
            entries.append((d, page_no, n))
            page_no += n
        y = h - 150
        for d, start, n in entries:
            if y < pdfmini.MARGIN + 20:
                break
            toc.text(x0, y, 10, f"Show date {d}")
            toc.text_right(x1, y, 10, f"page {start}"
                           + (f" to {start + n - 1}" if n > 1 else ""))
            y -= 14
        ok, cnt, why = self.chain.verify()
        toc.text(x0, pdfmini.MARGIN + 30, 9,
                 f"Hash chain verification for the whole set: "
                 f"{'INTACT' if ok else 'FAILED'}, {cnt} record(s)"
                 f"{'' if ok else ', ' + why}.", bold=True)
        return doc.build()

    def export_zip(self, date_from=None, date_to=None, combined=True):
        """A zip of every day's PDF (and JSON), VERIFY.txt for the whole
        chain, and a combined PDF with a table of contents. `date_from` and
        `date_to` (YYYY-MM-DD) narrow it. (name, bytes)."""
        dates = [d for d in self.chain.dates()
                 if (not date_from or d >= date_from)
                 and (not date_to or d <= date_to)]
        ok, n, why = self.chain.verify()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for d in dates:
                z.writestr(f"checklist_{d}.pdf", self.pdf_for_date(d))
                z.writestr(f"checklist_{d}.json", json.dumps(
                    self.day_json(d), indent=2, ensure_ascii=False) + "\n")
            if combined and dates:
                z.writestr("checklist_records_all.pdf",
                           self.combined_pdf(dates))
            lines = [f"Checklist records exported "
                     f"{_iso_local(self._wall())}",
                     f"Days in this export: {len(dates)}"
                     + (f" ({dates[0]} to {dates[-1]})" if dates else ""),
                     f"Records in the whole chain: {n}",
                     f"Hash chain verification: "
                     f"{'INTACT' if ok else 'FAILED'}"
                     + ("" if ok else f": {why}"),
                     f"Last record hash: "
                     f"{self.chain.records[-1]['hash'] if self.chain.records else 'none'}",
                     "",
                     "Each day's PDF and JSON carry the same records; the "
                     "JSON holds every field and hash. Verify again with: "
                     "ltc checklist-verify"]
            z.writestr("VERIFY.txt", "\n".join(lines) + "\n")
        name = "checklist_records" + (
            f"_{dates[0]}" if dates else "") + (
            f"_to_{dates[-1]}" if len(dates) > 1 else "") + ".zip"
        return name, buf.getvalue()
