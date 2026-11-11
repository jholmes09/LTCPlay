"""The audit log: who pressed what, on which surface, and what came of it
(Jeff, 2026-10-20). USERS.md says it in plain words.

One line per event, JSON, in one file per local day and per writer:

    <settings>/logs/audit/2026-10-20.engine.jsonl     the web pages' presses
                                                      and what the engine saw
    <settings>/logs/audit/2026-10-20.deck.jsonl       the Stream Deck's keys
    <settings>/logs/audit/2026-10-20.supervisor.jsonl LTC Player.exe

Each writer is the only writer of its own files (one engine, one deck
program and one supervisor per machine), so no two processes ever append
to the same file.

An event:
    ts_utc, ts_local    when (UTC with Z, and local with its offset)
    user                the signed-in name, "unsigned", or "system" for
                        what the engine or the supervisor did by itself
    surface             web, deck, engine or supervisor
    action              what was pressed or done ("abort", "sign-in", ...)
    target              what it was done to (a group, a machine, a cue)
    result              accepted, refused or error
    reason              why, when it was not accepted (a sentence)
    source              which device or window: "Rack screen w-3f9a",
                        "iPad at 10.20.0.7 w-81c2", "Stream Deck"
and the chain fields: stream, day, seq, prev, hash.

Never logged: a password, a password hash, a token, or anything typed into
a password field. Callers hand over named fields only; scrub() cuts every
field short and drops anything that looks like a secret.

TAMPER EVIDENCE. Every line carries `prev`, the hash of the line before
it, and `hash`, SHA-256 over prev plus the line's own canonical JSON. The
first line of a day is "day-open": its prev is the day's anchor
(day_anchor(stream, day), so a file renamed to another day fails), and it
carries prev_file_end, the last hash of the writer's previous day file, so
a whole day taken out is found too. When a writer moves to a new day it
closes the old file with "day-close" carrying the count, so lines cut off
the end of a closed day are found. verify_file() and verify_folder() say
where a chain breaks; `python -m ltcplay.audit verify [folder]` runs them.
The nightly upload (auditsend.py) puts each closed day's file and its end
hash on Jeff's server, so a day rewritten afterwards no longer matches the
copy there.

NEVER IN THE WAY OF THE SHOW. event() only puts the event on a bounded
queue and returns; a background thread writes. A full queue, a full disk
or any other error loses the event, never the press: the count of lost
events is written as an "audit-gap" line as soon as writing works again,
and status() reports the fault for the rack screen. Nothing here raises
into a caller.

This module imports nothing from ltcplay, so the Windows supervisor, which
does not bundle the ltcplay package, can carry it as a module of its own.
"""
import datetime
import hashlib
import json
import os
import queue
import re
import threading
import time

FOLDER = os.path.join("logs", "audit")
STREAMS = ("engine", "deck", "supervisor")
SURFACES = ("web", "deck", "engine", "supervisor")
RESULTS = ("accepted", "refused", "error")
UNSIGNED = "unsigned"
SYSTEM = "system"
QUEUE_MAX = 2000
FIELD_MAX = 200
ANCHOR_TAG = "LTC Player audit"
NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.(engine|deck|supervisor)\.jsonl$")
# A field whose name says it may hold a secret is never written, and a value
# that looks like one is replaced. Belt and braces: callers already pass
# only named, harmless fields.
SECRET_WORDS = ("pass", "pwd", "token", "secret", "hash", "salt", "key",
                "cookie", "authorization")
_SECRET_VALUE = re.compile(r"(?i)(bearer\s+\S+|password\s*[:=]\s*\S+)")
EVENT_FIELDS = ("ts_utc", "ts_local", "user", "surface", "action", "target",
                "result", "reason", "source")


# ------------------------------------------------------------- the chain --

def day_anchor(stream, day):
    """The prev of a day file's first line: tied to the writer and the
    date, so a file copied in under another name or day does not verify."""
    return hashlib.sha256(f"{ANCHOR_TAG}|{stream}|{day}".encode()).hexdigest()


def canonical(rec):
    d = {k: v for k, v in rec.items() if k != "hash"}
    return json.dumps(d, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def record_hash(rec):
    h = hashlib.sha256()
    h.update(str(rec.get("prev", "")).encode("ascii", "replace"))
    h.update(canonical(rec))
    return h.hexdigest()


def file_name(day, stream):
    return f"{day}.{stream}.jsonl"


def parse_name(name):
    """(day, stream) for an audit file name, else None."""
    m = NAME_RE.match(os.path.basename(str(name)))
    return (m.group(1), m.group(2)) if m else None


def scrub(value, limit=FIELD_MAX):
    """A field as short, single-line text with nothing secret-looking."""
    if value is None:
        return ""
    if isinstance(value, bool):
        value = "yes" if value else "no"
    s = " ".join(str(value).split())
    s = _SECRET_VALUE.sub("[hidden]", s)
    if len(s) > limit:
        s = s[:limit - 3] + "..."
    return s


def secret_name(key):
    k = str(key).lower()
    return any(w in k for w in SECRET_WORDS)


def _iso_utc(t):
    return datetime.datetime.fromtimestamp(
        t, datetime.timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


def _iso_local(t):
    return datetime.datetime.fromtimestamp(t).astimezone().isoformat(
        timespec="milliseconds")


def local_day(t):
    return datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d")


def read_lines(path):
    """(records, problem). A line that does not parse ends the read there,
    with a sentence; the records before it are kept."""
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError as e:
        return out, f"{os.path.basename(path)} could not be read: {e}"
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            return out, (f"line {n} of {os.path.basename(path)} is not a "
                         f"record (cut short or edited)")
        if not isinstance(rec, dict):
            return out, f"line {n} of {os.path.basename(path)} is not a record"
        out.append(rec)
    return out, ""


def verify_records(records, stream, day):
    """(ok, count, last hash, problem). seq counts from 1, the first line is
    day-open on the day's anchor, every line chains to the one before and
    hashes right, and nothing follows a day-close, whose count is right."""
    prev = day_anchor(stream, day)
    closed = False
    for i, r in enumerate(records, 1):
        if closed:
            return False, i - 1, prev, (f"line {i} comes after the day was "
                                        f"closed")
        if r.get("seq") != i:
            return False, i - 1, prev, (f"line {i} has seq {r.get('seq')!r}: "
                                        f"a line is missing or out of order")
        if r.get("stream") != stream or r.get("day") != day:
            return False, i - 1, prev, (f"line {i} belongs to "
                                        f"{r.get('day')} {r.get('stream')}, "
                                        f"not to this file")
        if r.get("prev") != prev:
            return False, i - 1, prev, (f"line {i} does not chain to line "
                                        f"{i - 1}: a line before it was "
                                        f"changed, removed or put in")
        if r.get("hash") != record_hash(r):
            return False, i - 1, prev, f"line {i} was changed after it was written"
        if i == 1 and r.get("action") != "day-open":
            return False, 0, prev, "the first line is not the day's opening line"
        if r.get("action") == "day-close":
            if r.get("count") != i:
                return False, i - 1, prev, (f"the closing line says "
                                            f"{r.get('count')} lines, the "
                                            f"file has {i}")
            closed = True
        prev = r["hash"]
    if not records:
        return False, 0, prev, "the file is empty"
    return True, len(records), prev, ""


def verify_file(path):
    """{"name", "day", "stream", "ok", "count", "end", "start_link",
    "closed", "gaps", "problem"} for one day file."""
    name = os.path.basename(path)
    parsed = parse_name(name)
    out = {"name": name, "day": None, "stream": None, "ok": False,
           "count": 0, "end": "", "start_link": "", "closed": False,
           "gaps": 0, "problem": ""}
    if parsed is None:
        out["problem"] = "not an audit file name"
        return out
    day, stream = parsed
    out.update(day=day, stream=stream)
    recs, problem = read_lines(path)
    ok, n, end, why = verify_records(recs, stream, day)
    out.update(ok=ok and not problem, count=n, end=end,
               problem=problem or why)
    if recs:
        out["start_link"] = str(recs[0].get("prev_file_end") or "")
        out["closed"] = recs[-1].get("action") == "day-close"
        out["gaps"] = sum(1 for r in recs if r.get("action") == "audit-gap")
    return out


def audit_files(folder):
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return sorted((n for n in names if parse_name(n)),
                  key=lambda n: (parse_name(n)[1], parse_name(n)[0]))


def verify_folder(folder, sent=None, today=None):
    """Every day file in `folder`, each verified, and each day linked to the
    one before it of the same writer: its prev_file_end must be that file's
    end hash, from the file itself or, once a sent file has been pruned,
    from `sent` (auditsend's record of what the server took). Days before
    `today` that were never closed are said too. Returns (ok, results,
    problems) with problems as sentences."""
    sent = sent or {}
    results, problems = [], []
    last = {}                       # stream -> (day, end hash)
    pruned = {}
    for name, rec in sent.items():
        p = parse_name(name)
        if p and isinstance(rec, dict) and rec.get("end"):
            pruned.setdefault(p[1], []).append((p[0], rec["end"]))
    for name in audit_files(folder):
        r = verify_file(os.path.join(folder, name))
        results.append(r)
        if not r["ok"]:
            problems.append(f"{name}: {r['problem']}")
            last[r["stream"]] = (r["day"], None)
            continue
        before = last.get(r["stream"])
        if before is None:
            # The writer's first file here: the day before it may have
            # been sent and pruned; then the server's record links it.
            older = sorted(d for d in pruned.get(r["stream"], [])
                           if d[0] < r["day"])
            want = older[-1][1] if older else ""
            if want and r["start_link"] != want:
                problems.append(f"{name}: does not follow on from "
                                f"{older[-1][0]}, as sent to the server")
        elif before[1] is not None and r["start_link"] != before[1]:
            problems.append(f"{name}: does not follow on from {before[0]}: "
                            f"a day file was removed, replaced or edited")
        if today and r["day"] < today and not r["closed"]:
            problems.append(f"{name}: the day was never closed (its writer "
                            f"stopped and has not run since)")
        if r["gaps"]:
            problems.append(f"{name}: {r['gaps']} gap line(s): events were "
                            f"lost while the log could not be written")
        last[r["stream"]] = (r["day"], r["end"])
    hard = [p for p in problems if "gap line" not in p and
            "never closed" not in p]
    return not hard, results, problems


# ------------------------------------------------------------ the writer --

class AuditLog:
    """One writer's audit files. event() never blocks and never raises."""

    def __init__(self, folder, stream, wall=time.time, queue_max=QUEUE_MAX,
                 fsync=True, threaded=True):
        if stream not in STREAMS:
            raise ValueError(f"stream has to be one of {', '.join(STREAMS)}")
        self.folder = folder
        self.stream = stream
        self._wall = wall
        self._fsync = fsync
        self._q = queue.Queue(maxsize=queue_max)
        self._threaded = threaded
        self._thread = None
        self._stop = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._lock = threading.Lock()
        self.dropped = 0            # lost and not yet written as a gap line
        self.lost_total = 0
        self.written = 0
        self.fault = ""             # the last write problem, "" when fine
        self.fault_at = None
        self._day = None
        self._seq = 0
        self._prev = ""

    # -- the callers' side ------------------------------------------------
    def event(self, user="", surface="", action="", target="",
              result="accepted", reason="", source="", **extra):
        """Queue one event. True if queued; never raises, never waits."""
        try:
            t = self._wall()
            rec = {"t": t, "user": scrub(user) or UNSIGNED,
                   "surface": scrub(surface), "action": scrub(action, 80),
                   "target": scrub(target), "result": scrub(result, 20),
                   "reason": scrub(reason), "source": scrub(source, 120)}
            for k, v in extra.items():
                if not secret_name(k) and isinstance(v, (int, float, str,
                                                          bool)):
                    rec[str(k)[:40]] = v if not isinstance(v, str) else \
                        scrub(v)
            if rec["result"] not in RESULTS:
                rec["result"] = "error" if rec["result"] else "accepted"
        except Exception:                    # noqa: BLE001
            with self._lock:
                self.dropped += 1
                self.lost_total += 1
            return False
        if not self._threaded:
            self._write_one(rec)
            return True
        try:
            self._idle.clear()
            self._q.put_nowait(rec)
            return True
        except queue.Full:
            with self._lock:
                self.dropped += 1
                self.lost_total += 1
                if not self.fault:
                    self.fault = ("the audit log fell behind and events were "
                                  "lost")
                    self.fault_at = self._wall()
            return False
        except Exception:                    # noqa: BLE001
            with self._lock:
                self.dropped += 1
                self.lost_total += 1
            return False

    def start(self):
        if self._threaded and self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name=f"ltcplay-audit-{self.stream}")
            self._thread.start()
        return self

    def stop(self, timeout=2.0):
        self._stop.set()
        try:
            self._q.put_nowait(None)
        except Exception:                    # noqa: BLE001
            pass
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)
        self._thread = None

    def flush(self, timeout=5.0):
        """Wait (tests, shutdown) until everything queued is written."""
        if not self._threaded:
            return True
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self._q.empty() and self._idle.is_set():
                return True
            time.sleep(0.005)
        return False

    def status(self):
        with self._lock:
            return {"stream": self.stream, "written": self.written,
                    "lost": self.lost_total, "waiting_gap": self.dropped,
                    "fault": self.fault,
                    "fault_at": (_iso_local(self.fault_at)
                                 if self.fault_at else None),
                    "day": self._day, "folder": self.folder}

    # -- the writer's side --------------------------------------------------
    def _loop(self):
        while True:
            try:
                rec = self._q.get(timeout=0.5)
            except queue.Empty:
                self._idle.set()
                if self._stop.is_set():
                    return
                continue
            if rec is None:
                self._idle.set()
                if self._stop.is_set():
                    return
                continue
            self._write_one(rec)
            if self._q.empty():
                self._idle.set()

    def _path(self, day):
        return os.path.join(self.folder, file_name(day, self.stream))

    def _last_of(self, path):
        """(seq, hash, closed) of a day file's last good line."""
        recs, _problem = read_lines(path)
        if not recs:
            return 0, "", False
        r = recs[-1]
        return int(r.get("seq") or len(recs)), str(r.get("hash") or ""), \
            r.get("action") == "day-close"

    def _previous_file(self, day):
        """The newest earlier day file of this writer, or None."""
        older = [n for n in audit_files(self.folder)
                 if parse_name(n)[1] == self.stream and parse_name(n)[0] < day]
        return os.path.join(self.folder, older[-1]) if older else None

    def _append(self, fields, t):
        """Chain and write one line. Raises OSError; on any failure the
        file is cut back to where it was and the chain does not move."""
        rec = {"ts_utc": _iso_utc(t), "ts_local": _iso_local(t),
               "stream": self.stream, "day": self._day}
        rec.update(fields)
        rec["seq"] = self._seq + 1
        rec["prev"] = self._prev
        rec["hash"] = record_hash(rec)
        line = (json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n"
                ).encode("utf-8")
        path = self._path(self._day)
        with open(path, "ab") as fh:
            at = fh.tell()
            try:
                fh.write(line)
                fh.flush()
                if self._fsync:
                    os.fsync(fh.fileno())
            except BaseException:
                try:
                    fh.truncate(at)
                except OSError:
                    pass
                raise
        self._seq = rec["seq"]
        self._prev = rec["hash"]
        return rec

    def _close_day(self, t, why):
        self._append({"user": SYSTEM, "surface": self.stream,
                      "action": "day-close", "target": self._day,
                      "result": "accepted", "reason": why, "source": "",
                      "count": self._seq + 1}, t)

    def _open_day(self, day, t):
        """Make `day` current: close an earlier open file of this writer
        first, then carry on the day's file or start it."""
        os.makedirs(self.folder, exist_ok=True)
        if self._day is not None and self._day != day:
            self._close_day(t, "the next day began")
        self._day = day
        path = self._path(day)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            self._seq, self._prev, closed = self._last_of(path)
            if closed:
                # A day file closed already (the clock went back over
                # midnight): its seal stays; later events of that day go
                # into it after a reopening line, which verify flags.
                pass
            return
        prev_path = self._previous_file(day)
        prev_end = ""
        if prev_path is not None:
            pday = parse_name(prev_path)[0]
            seq, h, closed = self._last_of(prev_path)
            if seq and not closed:
                # Its writer stopped before midnight: seal it now, so lines
                # cut off its end later are found.
                self._day, self._seq, self._prev = pday, seq, h
                self._close_day(t, "closed late: the writer was not "
                                   "running at the end of the day")
                h = self._prev
                self._day = day
            prev_end = h
        self._seq = 0
        self._prev = day_anchor(self.stream, day)
        self._append({"user": SYSTEM, "surface": self.stream,
                      "action": "day-open", "target": day,
                      "result": "accepted", "reason": "", "source": "",
                      "prev_file_end": prev_end}, t)

    def _write_one(self, rec):
        t = rec.pop("t", None) or self._wall()
        try:
            day = local_day(t)
            if day != self._day or not os.path.exists(self._path(day)):
                self._open_day(day, t)
            with self._lock:
                lost = self.dropped
            if lost:
                self._append({"user": SYSTEM, "surface": self.stream,
                              "action": "audit-gap",
                              "target": f"{lost} event(s)",
                              "result": "error",
                              "reason": (f"{lost} event(s) could not be "
                                         f"written ({self.fault or 'unknown'})"),
                              "source": "", "lost": lost}, t)
                with self._lock:
                    self.dropped -= lost
            fields = {k: rec.get(k, "") for k in EVENT_FIELDS[2:]}
            for k, v in rec.items():
                if k not in fields:
                    fields[k] = v
            self._append(fields, t)
            with self._lock:
                self.written += 1
                if self.fault and not self.dropped:
                    self.fault = ""
                    self.fault_at = None
        except Exception as e:               # noqa: BLE001  disk full and kin
            with self._lock:
                self.dropped += 1
                self.lost_total += 1
                self.fault = f"{type(e).__name__}: {e}"[:200]
                self.fault_at = self._wall()
            # The next write starts over from the file itself.
            self._day = None


def default_folder(settings_dir):
    return os.path.join(settings_dir, FOLDER)


# ----------------------------------------------------------- the command --

def _load_sent(folder):
    try:
        with open(os.path.join(folder, "sent.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        files = doc.get("files") if isinstance(doc, dict) else None
        return files if isinstance(files, dict) else {}
    except (OSError, ValueError):
        return {}


def main(argv=None):
    """python -m ltcplay.audit verify [folder]: checks every day file's
    chain and the links between days. Exit 0 when the chain holds."""
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] != "verify":
        print("usage: python -m ltcplay.audit verify [audit folder]")
        return 2
    if len(argv) > 1:
        folder = argv[1]
    else:
        try:
            from . import remote
            folder = default_folder(remote.settings_folder())
        except ImportError:
            folder = FOLDER
    ok, results, problems = verify_folder(folder, _load_sent(folder),
                                          today=local_day(time.time()))
    for r in results:
        print(f"{'ok  ' if r['ok'] else 'BAD '} {r['name']}: {r['count']} "
              f"line(s){'' if r['ok'] else ', ' + r['problem']}")
    for p in problems:
        print(f"  {p}")
    if not results:
        print(f"No audit files in {folder}.")
    print("The chain holds." if ok else "The chain is BROKEN: see above.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
