"""The nightly upload of the audit log to Jeff's server (Jeff, 2026-10-20).
The same shape as the checklist tally sender (checklist.TallySender): OFF
until switched on, HTTPS only, a bearer token never shown or logged,
retried with backoff, and nothing it does can reach the show.

What it sends: ONLY the audit day files (audit.py), each closed day once,
whole, with its SHA-256 and the hash chain's anchor and end. Nothing else
in the settings folder is ever read by it.

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
