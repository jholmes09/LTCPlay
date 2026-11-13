"""The nightly upload of the audit log and the night journal to Jeff's own
server (Jeff, 2026-10-20; the night journal 2026-11-12). The same shape as
the checklist tally sender (checklist.TallySender): OFF until switched on,
HTTPS only, a bearer token never shown or logged, retried with backoff,
and nothing it does can reach the show.

What it sends: the audit log and the night journal, only to Jeff's own
server; nothing else is read or sent, never a password or token.
Uploader sends the audit day files (audit.py), each closed day once,
whole, with its SHA-256 and the hash chain's anchor and end.
JournalUploader (below) sends each closed night's three night journal
files (journal.py), each whole, with its SHA-256 and the night's logging
check. Nothing else in the settings folder or the nights folder is ever
read by either; incident folders are never sent.

When: every day at "time" (local, 03:30 unless set), and again with
backoff after a failure, until every closed day is acknowledged. A night
the PC was off is caught up the next time: every closed day not yet
acknowledged is sent, oldest first.

Done means acknowledged: a file is marked sent only when the server
answers 2xx with {"ok": true, "sha256": <the same checksum>}. The record
of what was sent (sent.json in the audit folder) keeps each file's
checksum and end hash, so a file changed after it was sent is said, and
the chain still links across a day pruned locally.

Kept: every day file at least keep_days (90 unless set higher), and an
unacknowledged file is never removed at all.

Settings, in the settings folder (never in the repo, like flamesafe's link
key in flamesafe.json):
  ltcplay_audit.json  {"upload": {"enabled": false, "url": "https://...",
                       "token": "...", "time": "03:30", "keep_days": 90}}
The token is read when sending and never put in a status, a log line or
the page.
"""
import datetime
import hashlib
import json
import os
import re
import stat
import threading
import time

from . import audit

CONFIG_FILE = "ltcplay_audit.json"
SENT_FILE = "sent.json"
DEFAULTS = {"enabled": False, "url": "", "token": "", "time": "03:30",
            "keep_days": 90, "timeout_s": 30.0}
KEEP_MIN_DAYS = 90
BACKOFF_S = (60.0, 300.0, 900.0, 3600.0)
TICK_S = 30.0
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
USER_AGENT = "LTC Player audit upload"


def parse_upload(doc, where=CONFIG_FILE):
    """The upload block, checked. Raises ValueError with a sentence."""
    out = dict(DEFAULTS)
    if doc is None:
        return out
    if not isinstance(doc, dict):
        raise ValueError(f"{where}: upload has to be an object.")
    unknown = sorted(k for k in doc if k not in DEFAULTS)
    if unknown:
        raise ValueError(f"{where}: upload has no setting "
                         f"{', '.join(unknown)}.")
    en = doc.get("enabled", False)
    if en is not True and en is not False:
        raise ValueError(f"{where}: upload.enabled has to be true or false.")
    url = str(doc.get("url") or "").strip()
    if url and not url.lower().startswith("https://"):
        raise ValueError(f"{where}: the server address has to start with "
                         f"https:// (nothing is sent in clear).")
    if en and not url:
        raise ValueError(f"{where}: the upload is on but there is no server "
                         f"address.")
    tm = str(doc.get("time") or "03:30").strip()
    if not TIME_RE.match(tm):
        raise ValueError(f"{where}: time has to be HH:MM, like 03:30.")
    keep = doc.get("keep_days", 90)
    if isinstance(keep, bool) or not isinstance(keep, int) or \
            not KEEP_MIN_DAYS <= keep <= 3650:
        raise ValueError(f"{where}: keep_days has to be {KEEP_MIN_DAYS} to "
                         f"3650.")
    to = doc.get("timeout_s", 30.0)
    if isinstance(to, bool) or not isinstance(to, (int, float)) or \
            not 1 <= to <= 300:
        raise ValueError(f"{where}: timeout_s has to be 1 to 300.")
    tok = doc.get("token", "")
    if not isinstance(tok, str):
        raise ValueError(f"{where}: token has to be text.")
    out.update(enabled=en, url=url, token=tok.strip(), time=tm,
               keep_days=keep, timeout_s=float(to))
    return out


def load_config(folder):
    path = os.path.join(folder, CONFIG_FILE)
    if not os.path.exists(path):
        return dict(DEFAULTS), ""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict):
            raise ValueError("not a settings object")
        return parse_upload(doc.get("upload"), path), ""
    except (OSError, ValueError) as e:
        # A broken file: the upload is off, and the page says why.
        return dict(DEFAULTS), str(e)


def save_config(folder, changes):
    """Merge `changes` into the upload block, check it, write the file
    private. A token of None or "" in changes leaves the saved one."""
    path = os.path.join(folder, CONFIG_FILE)
    doc = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                doc = json.load(fh) or {}
        except (OSError, ValueError):
            doc = {}
    up = dict(doc.get("upload") or {})
    for k, v in changes.items():
        if k == "token" and not v:
            continue
        up[k] = v
    parse_upload(up, path)
    doc["upload"] = up
    from .users import _write_private
    os.makedirs(folder, exist_ok=True)
    _write_private(path, doc)
    return load_config(folder)


def _https_post(url, data, headers, timeout):
    """One HTTPS POST: (status, text). The checklist tally's transport,
    reading a little more of the answer (the acknowledgement)."""
    import urllib.error
    import urllib.request
    if not url.lower().startswith("https://"):
        raise ValueError("the server address has to be https")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(4000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(400).decode("utf-8", "replace")


class Uploader:
    """`transport(url, data, headers, timeout) -> (status, text)` is
    injectable for the selftest; the default is an HTTPS POST. tick() is
    called every TICK_S by the background thread; it never raises."""

    def __init__(self, audit_folder, settings_folder, transport=None,
                 wall=time.time, clock=time.monotonic, note=None):
        self.audit_folder = audit_folder
        self.settings_folder = settings_folder
        self._transport = transport or _https_post
        self._wall = wall
        self._clock = clock
        self._note_fn = note
        self._lock = threading.Lock()
        self._busy = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._next_try = 0.0
        self._fails_in_a_row = 0
        self._last_run_day = None
        self.last_error = ""
        self.last_upload_at = None
        self.last_attempt_at = None
        self.sent_this_run = 0

    # -- the record of what was sent --------------------------------------
    def _sent_path(self):
        return os.path.join(self.audit_folder, SENT_FILE)

    def sent(self):
        return audit._load_sent(self.audit_folder)

    def _mark_sent(self, name, info):
        doc = {"files": self.sent()}
        doc["files"][name] = info
        os.makedirs(self.audit_folder, exist_ok=True)
        tmp = self._sent_path() + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._sent_path())

    def _note(self, action, result="accepted", reason="", target=""):
        if self._note_fn is None:
            return
        try:
            self._note_fn(action=action, result=result, reason=reason,
                          target=target)
        except Exception:                    # noqa: BLE001
            pass

    def _today(self):
        return audit.local_day(self._wall())

    def pending(self):
        """Closed day files (before today) not acknowledged, oldest first."""
        today = self._today()
        sent = self.sent()
        out = []
        for n in audit.audit_files(self.audit_folder):
            day, _stream = audit.parse_name(n)
            if day < today and n not in sent:
                out.append(n)
        return sorted(out, key=lambda n: audit.parse_name(n)[0])

    def changed_since_sent(self):
        """Files whose bytes are not what the server acknowledged."""
        out = []
        for n, rec in self.sent().items():
            p = os.path.join(self.audit_folder, n)
            if os.path.exists(p) and isinstance(rec, dict) and \
                    rec.get("sha256") and _sha256(p) != rec["sha256"]:
                out.append(n)
        return out

    # -- when -----------------------------------------------------------------
    def due(self, cfg):
        """True at or after today's upload time, once per day, and after a
        failure again when its backoff is up."""
        if not cfg["enabled"]:
            return False
        if self._fails_in_a_row:
            return self._clock() >= self._next_try
        now = datetime.datetime.fromtimestamp(self._wall())
        h, m = (int(x) for x in cfg["time"].split(":"))
        if (now.hour, now.minute) < (h, m):
            return False
        return self._last_run_day != now.strftime("%Y-%m-%d")

    # -- sending ---------------------------------------------------------------
    def send_one(self, name, cfg):
        """(ok, why). Reads one audit day file and posts it whole."""
        path = os.path.join(self.audit_folder, name)
        parsed = audit.parse_name(name)
        if parsed is None or os.path.dirname(name):
            return False, "not an audit file"
        with open(path, "rb") as fh:
            data = fh.read()
        sha = hashlib.sha256(data).hexdigest()
        v = audit.verify_file(path)
        day, stream = parsed
        headers = {"Content-Type": "application/x-ndjson",
                   "User-Agent": USER_AGENT,
                   "X-LTC-File": name, "X-LTC-Day": day,
                   "X-LTC-Stream": stream, "X-LTC-SHA256": sha,
                   "X-LTC-Count": str(v["count"]),
                   "X-LTC-Chain-Anchor": audit.day_anchor(stream, day),
                   "X-LTC-Chain-Start": v["start_link"],
                   "X-LTC-Chain-End": v["end"],
                   "X-LTC-Chain-OK": "yes" if v["ok"] else "no"}
        if cfg.get("token"):
            headers["Authorization"] = "Bearer " + cfg["token"]
        try:
            status, text = self._transport(cfg["url"], data, headers,
                                           cfg["timeout_s"])
        except Exception as e:               # noqa: BLE001
            status, text = 0, f"{type(e).__name__}: {e}"
        if not 200 <= status < 300:
            why = f"HTTP {status}: {text[:160]}" if status else text[:200]
            return False, re.sub(r"(?i)bearer\s+\S+", "Bearer [hidden]", why)
        try:
            ack = json.loads(text)
        except ValueError:
            ack = None
        if not isinstance(ack, dict) or ack.get("ok") is not True or \
                ack.get("sha256") != sha:
            return False, ("the server answered but did not acknowledge this "
                           "file's checksum")
        self._mark_sent(name, {"sha256": sha, "end": v["end"],
                               "count": v["count"], "chain_ok": v["ok"],
                               "sent_at": audit._iso_local(self._wall())})
        return True, ""

    def run(self, force=False):
        """Send every pending day, oldest first, stopping at the first
        failure. Returns how many went. Never raises."""
        if not self._busy.acquire(blocking=False):
            return 0
        try:
            cfg, err = load_config(self.settings_folder)
            if err:
                self.last_error = err
                return 0
            if not cfg["enabled"] or (not force and not self.due(cfg)):
                return 0
            self.last_attempt_at = self._wall()
            went = 0
            for name in self.pending():
                ok, why = self.send_one(name, cfg)
                if ok:
                    went += 1
                    self.last_upload_at = self._wall()
                    self._note("audit-upload", target=name)
                    continue
                self._fails_in_a_row += 1
                back = BACKOFF_S[min(self._fails_in_a_row,
                                     len(BACKOFF_S)) - 1]
                self._next_try = self._clock() + back
                if why != self.last_error:
                    self._note("audit-upload", "error", why, target=name)
                self.last_error = why
                return went
            self._fails_in_a_row = 0
            self.last_error = ""
            self._last_run_day = self._today()
            self.sent_this_run = went
            self.prune(cfg)
            return went
        except Exception as e:               # noqa: BLE001
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            return 0
        finally:
            self._busy.release()

    def prune(self, cfg):
        """Remove day files acknowledged by the server AND older than
        keep_days. Never an unacknowledged one."""
        keep = max(KEEP_MIN_DAYS, int(cfg.get("keep_days") or KEEP_MIN_DAYS))
        cutoff = (datetime.date.fromtimestamp(self._wall()) -
                  datetime.timedelta(days=keep)).isoformat()
        sent = self.sent()
        gone = []
        for n in audit.audit_files(self.audit_folder):
            day, _s = audit.parse_name(n)
            if day < cutoff and n in sent:
                p = os.path.join(self.audit_folder, n)
                if _sha256(p) != sent[n].get("sha256"):
                    continue          # changed since sent: kept, and said
                try:
                    os.remove(p)
                    gone.append(n)
                except OSError:
                    pass
        return gone

    # -- the thread ------------------------------------------------------------
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="ltcplay-audit-upload")
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(TICK_S):
            self.run()

    def status(self):
        cfg, err = load_config(self.settings_folder)
        host = re.sub(r"^https?://", "", cfg["url"]).split("/", 1)[0]
        pend = self.pending()
        stamps = [str(r.get("sent_at")) for r in self.sent().values()
                  if isinstance(r, dict) and r.get("sent_at")]
        last = (audit._iso_local(self.last_upload_at)
                if self.last_upload_at else (max(stamps) if stamps else None))
        return {"enabled": cfg["enabled"], "host": host,
                "token_set": bool(cfg["token"]), "time": cfg["time"],
                "keep_days": cfg["keep_days"], "config_error": err,
                "pending": len(pend),
                "pending_days": sorted({audit.parse_name(n)[0]
                                        for n in pend}),
                "last_upload_at": last,
                "last_attempt_at": (audit._iso_local(self.last_attempt_at)
                                    if self.last_attempt_at else None),
                "last_error": self.last_error,
                "changed_since_sent": self.changed_since_sent()}


def _sha256(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for b in iter(lambda: fh.read(1 << 20), b""):
                h.update(b)
    except OSError:
        return ""
    return h.hexdigest()


# ------------------------------------------------------- the night journal --
# The night journal goes to the same server, with the same settings, beside
# the audit log (Jeff, 2026-11-12): each CLOSED night's three files
# (journal.py: night_YYYY-MM-DD.journal.txt, .jsonl and .summary.md), each
# whole, with its SHA-256. Its own record of what was sent (sent.json in
# the nights folder), its own backoff, its own thread: the audit upload
# above is exactly as it was. Never an incident folder, never anything
# else in the nights folder.

JOURNAL_SENT_FILE = "sent.json"
JOURNAL_NAME_RE = re.compile(r"^night_(\d{4}-\d{2}-\d{2})"
                             r"\.(journal\.txt|jsonl|summary\.md)\Z")
JOURNAL_KINDS = {"journal.txt": "text/plain; charset=utf-8",
                 "jsonl": "application/x-ndjson",
                 "summary.md": "text/markdown; charset=utf-8"}
JOURNAL_USER_AGENT = "LTC Player night journal upload"
JOURNAL_GAPS_MAX = 20
# The receiver takes up to this much (tools/audit_receiver.py MAX_BYTES).
JOURNAL_MAX_BYTES = 64 * 1024 * 1024
# Answers that will not change by sending the same bytes again: the file
# is marked failed for good (for this checksum), said once, and the later
# nights go on. 401, 403, 408 and 429 are the settings or the moment, and
# are tried again with the backoff.
JOURNAL_RETRY_4XX = (401, 403, 408, 429)


def journal_sent(folder):
    """The night journal's record of what the server acknowledged."""
    return audit._load_sent(folder)


def _journal_doc(folder):
    try:
        with open(os.path.join(folder, JOURNAL_SENT_FILE),
                  encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


class JournalUploader:
    """The night journal's nightly upload. `night()` returns the night the
    scheduler is on now (a date or its ISO text): only nights before it, and
    only once their summary is written, are closed and sent. `checker(night)`
    is the night files' own check (journal.Logbook.check_night), handed in
    so this module never imports the journal. Everything else is the audit Uploader's shape:
    run() never raises, transport is injectable, the token never leaves the
    request.

    A file the server took is sent again only when its bytes changed since
    (a summary written again), once per change, with X-LTC-Replaces naming
    the checksum the server holds. A file the server refuses for good (too
    large, a different file already there, another 4xx) is marked failed
    for that checksum, said once in the audit log (never in the night
    journal, which everyone reads: the upload is a SuperUser's alone), and
    the later nights go on."""

    def __init__(self, folder, settings_folder, night=None, transport=None,
                 wall=time.time, clock=time.monotonic, note=None,
                 checker=None):
        self.folder = folder
        self.settings_folder = settings_folder
        self._night = night
        self._transport = transport or _https_post
        self._wall = wall
        self._clock = clock
        self._note_fn = note
        self._checker = checker
        self._busy = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._next_try = 0.0
        self._fails_in_a_row = 0
        self._last_run_day = None
        self._hashes = {}           # path: ((size, mtime_ns), sha256)
        self.last_error = ""
        self.last_upload_at = None
        self.last_attempt_at = None

    # -- the record ----------------------------------------------------------
    def sent(self):
        return journal_sent(self.folder)

    def failed(self):
        f = _journal_doc(self.folder).get("failed")
        return f if isinstance(f, dict) else {}

    def _write_doc(self, change):
        doc = _journal_doc(self.folder)
        doc.setdefault("files", {})
        doc.setdefault("failed", {})
        change(doc)
        os.makedirs(self.folder, exist_ok=True)
        path = os.path.join(self.folder, JOURNAL_SENT_FILE)
        tmp = path + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def _mark_sent(self, name, info):
        def change(doc):
            doc["files"][name] = info
            doc["failed"].pop(name, None)
        self._write_doc(change)

    def _mark_failed(self, name, info):
        def change(doc):
            doc["failed"][name] = info
        self._write_doc(change)

    def _mark_enabled(self, on):
        """Pruning keeps an unacknowledged night only while the upload is
        on (journal.Logbook.prune reads this)."""
        if _journal_doc(self.folder).get("enabled") is not on and (
                on or os.path.exists(os.path.join(self.folder,
                                                  JOURNAL_SENT_FILE))):
            def change(doc):
                doc["enabled"] = on
            self._write_doc(change)

    def _note(self, action, result="accepted", reason="", target=""):
        if self._note_fn is None:
            return
        try:
            self._note_fn(action=action, result=result, reason=reason,
                          target=target)
        except Exception:                    # noqa: BLE001
            pass

    # -- the files ------------------------------------------------------------
    def _file(self, name):
        """(path, sha256, size) of a night file that may be sent, or None:
        never a link, never more than the receiver takes (None, with the
        size, for that: (path, None, size)). The checksum is read in
        pieces and kept until the file changes."""
        path = os.path.join(self.folder, name)
        try:
            st = os.lstat(path)
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode):
            return None
        if st.st_size > JOURNAL_MAX_BYTES:
            return path, None, st.st_size
        key = (st.st_size, getattr(st, "st_mtime_ns", st.st_mtime))
        had = self._hashes.get(path)
        if had is not None and had[0] == key:
            return path, had[1], st.st_size
        h = hashlib.sha256()
        try:
            with open(path, "rb") as fh:
                for piece in iter(lambda: fh.read(1 << 20), b""):
                    h.update(piece)
        except OSError:
            return None
        self._hashes[path] = (key, h.hexdigest())
        return path, h.hexdigest(), st.st_size

    def open_night(self):
        """The night still open (ISO text), or "" when it cannot be told:
        then nothing counts as closed but nights before yesterday."""
        try:
            n = self._night() if callable(self._night) else self._night
        except Exception:                    # noqa: BLE001
            n = None
        if n:
            return str(n)
        return (datetime.date.fromtimestamp(self._wall())
                - datetime.timedelta(days=1)).isoformat()

    def closed_nights(self):
        """Nights before the open one whose summary has been written."""
        open_n = self.open_night()
        try:
            names = os.listdir(self.folder)
        except OSError:
            return []
        out = set()
        for n in names:
            m = JOURNAL_NAME_RE.match(n)
            if m and m.group(1) < open_n and os.path.isfile(
                    os.path.join(self.folder,
                                 f"night_{m.group(1)}.summary.md")):
                out.add(m.group(1))
        return sorted(out)

    def pending(self):
        """[(name, the checksum the server holds or "")]: files of closed
        nights not acknowledged, or changed since, and not failed for good
        as they are; oldest night first."""
        sent, failed = self.sent(), self.failed()
        out = []
        for night in self.closed_nights():
            for kind in ("journal.txt", "jsonl", "summary.md"):
                name = f"night_{night}.{kind}"
                rec = sent.get(name)
                held = rec.get("sha256") if isinstance(rec, dict) else ""
                f = self._file(name)
                if f is None:
                    # A night file that cannot be read now stays pending:
                    # sending it fails, and waits out the backoff.
                    p = os.path.join(self.folder, name)
                    if os.path.isfile(p) and not os.path.islink(p):
                        out.append((name, held or ""))
                    continue
                sha = f[1] or f"size {f[2]}"
                bad = failed.get(name)
                if isinstance(bad, dict) and bad.get("sha256") == sha:
                    continue
                if held != sha:
                    out.append((name, held or ""))
        return out

    def changed_since_sent(self):
        """Files whose bytes are not what the server acknowledged (each is
        sent again, once)."""
        out = []
        for n, rec in self.sent().items():
            f = self._file(n) if JOURNAL_NAME_RE.match(n) else None
            if f is not None and isinstance(rec, dict) and \
                    rec.get("sha256") and f[1] != rec["sha256"]:
                out.append(n)
        return sorted(out)

    def check(self, night):
        """{"incomplete", "gaps", "problems"} for one night: the files'
        own check (journal.check_files, through `checker`) and the
        summary's Logging line. Without a checker, the summary alone."""
        f = {"complete": True, "gaps": [], "problems": []}
        if self._checker is not None:
            f = self._checker(night)
        logging_line = ""
        try:
            with open(os.path.join(self.folder,
                                   f"night_{night}.summary.md"),
                      encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith("- Logging:"):
                        logging_line = line.strip()
                        break
        except OSError:
            pass
        said_bad = bool(logging_line) and not logging_line.startswith(
            "- Logging: complete")
        return {"incomplete": bool(not f["complete"] or said_bad),
                "gaps": [{"from": g["from"], "to": g["to"],
                          "seconds": g["seconds"], "why": g["why"]}
                         for g in f["gaps"][:JOURNAL_GAPS_MAX]],
                "problems": f["problems"][:JOURNAL_GAPS_MAX],
                "logging": logging_line[2:] if logging_line else ""}

    def due(self, cfg):
        if not cfg["enabled"]:
            return False
        if self._fails_in_a_row:
            return self._next_try <= self._clock()
        now = datetime.datetime.fromtimestamp(self._wall())
        h, m = (int(x) for x in cfg["time"].split(":"))
        if (now.hour, now.minute) < (h, m):
            return False
        return self._last_run_day != now.strftime("%Y-%m-%d")

    # -- sending --------------------------------------------------------------
    def send_one(self, name, cfg, meta=None, replaces=""):
        """(result, why): result "sent", "retry" (tried again with the
        backoff) or "failed" (refused for good as it is). One night file,
        whole, with its checksum and the night's logging check."""
        m = JOURNAL_NAME_RE.match(name)
        if m is None or os.path.dirname(name) or os.path.basename(
                name) != name:
            return "failed", "not a night journal file"
        night, kind = m.group(1), m.group(2)
        f = self._file(name)
        if f is None:
            return "retry", f"{name} could not be read"
        path, want, size = f
        if want is None:
            return "failed", (f"{name} is {size} bytes, more than the "
                              f"{JOURNAL_MAX_BYTES} the server takes")
        try:
            with open(path, "rb") as fh:
                data = fh.read(JOURNAL_MAX_BYTES + 1)
        except OSError as e:
            return "retry", f"{name} could not be read ({e.strerror or e})"
        sha = hashlib.sha256(data).hexdigest()
        meta = meta if meta is not None else self.check(night)
        doc = {"night": night, "kind": kind,
               "incomplete": meta["incomplete"], "gaps": meta["gaps"]}
        headers = {"Content-Type": JOURNAL_KINDS[kind],
                   "User-Agent": JOURNAL_USER_AGENT,
                   "X-LTC-File": name, "X-LTC-Night": night,
                   "X-LTC-Kind": "night-journal", "X-LTC-SHA256": sha,
                   "X-LTC-Incomplete": "true" if meta["incomplete"]
                   else "false",
                   "X-LTC-Meta": json.dumps(doc, separators=(",", ":"),
                                            sort_keys=True)}
        if replaces:
            headers["X-LTC-Replaces"] = replaces
        if cfg.get("token"):
            headers["Authorization"] = "Bearer " + cfg["token"]
        try:
            status, text = self._transport(cfg["url"], data, headers,
                                           cfg["timeout_s"])
        except Exception as e:               # noqa: BLE001
            status, text = 0, f"{type(e).__name__}: {e}"
        if not 200 <= status < 300:
            why = f"HTTP {status}: {text[:160]}" if status else text[:200]
            # Whatever the server echoed, the token is never said.
            why = re.sub(r"(?i)bearer\s+\S+", "Bearer [hidden]", why)
            if 400 <= status < 500 and status not in JOURNAL_RETRY_4XX:
                return "failed", why
            return "retry", why
        try:
            ack = json.loads(text)
        except ValueError:
            ack = None
        took = isinstance(ack, dict) and ack.get("ok") is True and \
            ack.get("sha256") == sha
        if not took:
            return "retry", ("the server answered but did not acknowledge "
                             "this file's checksum")
        self._mark_sent(name, {"sha256": sha, "incomplete": meta["incomplete"],
                               "gaps": len(meta["gaps"]),
                               "replaced": replaces or None,
                               "sent_at": audit._iso_local(self._wall())})
        return "sent", ""

    def run(self, force=False):
        """Send every pending night file, oldest first. A file refused for
        good is marked so and the later nights go on; any other failure
        stops the run and waits out the backoff. Returns how many went.
        Never raises."""
        if not self._busy.acquire(blocking=False):
            return 0
        try:
            cfg, err = load_config(self.settings_folder)
            if err:
                self.last_error = err
                return 0
            self._mark_enabled(bool(cfg["enabled"]))
            if not cfg["enabled"]:
                return 0
            if not force and not self.due(cfg):
                return 0
            self.last_attempt_at = self._wall()
            went = 0
            checked = {}
            for name, held in self.pending():
                night = JOURNAL_NAME_RE.match(name).group(1)
                if night not in checked:
                    checked[night] = self.check(night)
                result, why = self.send_one(name, cfg, checked[night], held)
                if result == "sent":
                    went += 1
                    self.last_upload_at = self._wall()
                    self._note("journal-upload", target=name)
                    continue
                if result == "failed":
                    f = self._file(name)
                    self._mark_failed(name, {
                        "sha256": (f[1] or f"size {f[2]}") if f else "",
                        "why": why, "at": audit._iso_local(self._wall())})
                    # Once: a file failed for good is never tried again.
                    self._note("journal-upload-failed", "error", why,
                               target=name)
                    continue
                self._fails_in_a_row += 1
                back = BACKOFF_S[min(self._fails_in_a_row,
                                     len(BACKOFF_S)) - 1]
                self._next_try = self._clock() + back
                if why != self.last_error:
                    self._note("journal-upload", "error", why, target=name)
                self.last_error = why
                return went
            self._fails_in_a_row = 0
            self.last_error = ""
            self._last_run_day = datetime.datetime.fromtimestamp(
                self._wall()).strftime("%Y-%m-%d")
            return went
        except Exception as e:               # noqa: BLE001
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            self._fails_in_a_row += 1
            self._next_try = self._clock() + BACKOFF_S[
                min(self._fails_in_a_row, len(BACKOFF_S)) - 1]
            return 0
        finally:
            self._busy.release()

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="ltcplay-journal-upload")
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(TICK_S):
            self.run()

    def status(self):
        pend = self.pending()
        sent = self.sent()
        stamps = [str(r.get("sent_at")) for r in sent.values()
                  if isinstance(r, dict) and r.get("sent_at")]
        last = (audit._iso_local(self.last_upload_at)
                if self.last_upload_at else (max(stamps) if stamps else None))
        failed = self.failed()
        return {"pending": len(pend),
                "pending_nights": sorted({JOURNAL_NAME_RE.match(n).group(1)
                                          for n, _h in pend}),
                "incomplete_sent": sorted(
                    n for n, r in sent.items()
                    if isinstance(r, dict) and r.get("incomplete")),
                "failed": sorted(failed),
                "failed_why": {n: str(r.get("why", "")) for n, r in
                               failed.items() if isinstance(r, dict)},
                "last_upload_at": last,
                "last_error": self.last_error,
                "changed_since_sent": self.changed_since_sent()}
