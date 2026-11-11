#!/usr/bin/env python3
"""The reference receiver for LTC Player's nightly audit upload.

One file, the Python standard library only. Run it on Jeff's server behind
HTTPS; tools/AUDIT RECEIVER README.txt says how in plain words.

    python3 audit_receiver.py --store /srv/ltc-audit --token-file token.txt \
        --cert fullchain.pem --key privkey.pem --port 8443
    python3 audit_receiver.py verify /srv/ltc-audit

What it does with each upload (POST, any path):
  - the bearer token must match the token file (compared in constant time);
  - the file name must be an audit day file (2026-10-20.engine.jsonl);
  - the body's SHA-256 must match X-LTC-SHA256;
  - the hash chain is checked line by line, and against the end of the
    previous day the server already holds for that writer;
  - the file is stored as <store>/<day>/<file name>, written whole and
    then moved into place, and acknowledged with {"ok": true, "sha256":
    ...}. LTC Player marks a day sent only on that answer.
  - the same file sent again is acknowledged again (nothing changes); a
    DIFFERENT file under a name already stored is kept beside it as
    <name>.conflict-<time> and refused with 409, so nothing is ever
    overwritten.
A file whose chain does not verify is still stored (it is evidence) and
acknowledged, and the answer and <store>/index.jsonl say chain_ok false.
"""
import hashlib
import hmac
import json
import os
import re
import sys
import time

NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.(engine|deck|supervisor)\.jsonl$")
ANCHOR_TAG = "LTC Player audit"
MAX_BYTES = 64 * 1024 * 1024


# The chain rules, the same as ltcplay/audit.py (kept here so this file
# stands alone on the server).
def day_anchor(stream, day):
    return hashlib.sha256(f"{ANCHOR_TAG}|{stream}|{day}".encode()).hexdigest()


def record_hash(rec):
    d = {k: v for k, v in rec.items() if k != "hash"}
    h = hashlib.sha256()
    h.update(str(rec.get("prev", "")).encode("ascii", "replace"))
    h.update(json.dumps(d, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False).encode("utf-8"))
    return h.hexdigest()


def verify_bytes(data, stream, day):
    """(ok, count, first line's prev_file_end, end hash, problem)."""
    prev = day_anchor(stream, day)
    closed = False
    start = ""
    n = 0
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return False, 0, "", prev, "not UTF-8 text"
    for line in lines:
        if not line.strip():
            continue
        n += 1
        try:
            r = json.loads(line)
        except ValueError:
            return False, n - 1, start, prev, f"line {n} is not a record"
        if not isinstance(r, dict):
            return False, n - 1, start, prev, f"line {n} is not a record"
        if closed:
            return False, n - 1, start, prev, f"line {n} follows the close"
        if r.get("seq") != n or r.get("prev") != prev or \
                r.get("hash") != record_hash(r) or \
                r.get("stream") != stream or r.get("day") != day:
            return False, n - 1, start, prev, f"the chain breaks at line {n}"
        if n == 1:
            if r.get("action") != "day-open":
                return False, 0, start, prev, "no opening line"
            start = str(r.get("prev_file_end") or "")
        if r.get("action") == "day-close":
            if r.get("count") != n:
                return False, n - 1, start, prev, "the close count is wrong"
            closed = True
        prev = r["hash"]
    if n == 0:
        return False, 0, start, prev, "empty"
    return True, n, start, prev, ""


def _index(store):
    path = os.path.join(store, "index.jsonl")
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    out[r["name"]] = r
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return out


def _append_index(store, rec):
    with open(os.path.join(store, "index.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def receive(store, token, headers, body, now=time.time):
    """(HTTP status, answer dict) for one upload. `headers` is any
    mapping with case-insensitive .get (or a plain dict of lower-case
    names). Never raises for bad input."""
    def h(name):
        v = headers.get(name)
        if v is None:
            v = headers.get(name.lower())
        return str(v or "")
    auth = h("Authorization")
    want = "Bearer " + str(token or "")
    if not token or not hmac.compare_digest(auth.encode(), want.encode()):
        return 401, {"ok": False, "error": "wrong or missing token"}
    name = h("X-LTC-File")
    m = NAME_RE.match(name)
    if not m:
        return 400, {"ok": False, "error": "not an audit day file name"}
    day, stream = m.group(1), m.group(2)
    if not isinstance(body, (bytes, bytearray)) or len(body) > MAX_BYTES:
        return 413, {"ok": False, "error": "too large"}
    sha = hashlib.sha256(body).hexdigest()
    if not hmac.compare_digest(sha, h("X-LTC-SHA256")):
        return 400, {"ok": False, "error": "the checksum does not match"}
    ok, count, start, end, problem = verify_bytes(bytes(body), stream, day)
    # Linked to the day before, from this writer, as stored here.
    idx = _index(store)
    earlier = sorted((r["day"], r) for r in idx.values()
                     if r.get("stream") == stream and r.get("day", "") < day)
    linked = None
    if earlier and ok:
        linked = earlier[-1][1].get("end") == start
        if not linked:
            problem = (f"does not follow on from {earlier[-1][0]} as stored "
                       f"here")
    folder = os.path.join(store, day)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    if os.path.exists(path):
        with open(path, "rb") as fh:
            have = hashlib.sha256(fh.read()).hexdigest()
        if have == sha:
            return 200, {"ok": True, "sha256": sha, "chain_ok": ok,
                         "again": True}
        keep = f"{path}.conflict-{int(now())}"
        with open(keep, "wb") as fh:
            fh.write(body)
        return 409, {"ok": False, "error": "a different file with this "
                                           "name is already stored; this one "
                                           "was kept beside it"}
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(body)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    rec = {"name": name, "day": day, "stream": stream, "sha256": sha,
           "count": count, "start": start, "end": end, "chain_ok": ok,
           "linked": linked, "problem": problem,
           "received": time.strftime("%Y-%m-%d %H:%M:%S",
                                     time.localtime(now()))}
    _append_index(store, rec)
    return 200, {"ok": True, "sha256": sha, "chain_ok": ok,
                 "linked": linked, "problem": problem}


def verify_store(store):
    """Every stored file again, and the links between days. (ok, lines)."""
    out, good = [], True
    last = {}
    files = []
    for day in sorted(os.listdir(store)) if os.path.isdir(store) else []:
        d = os.path.join(store, day)
        if os.path.isdir(d):
            for n in sorted(os.listdir(d)):
                m = NAME_RE.match(n)
                if m:
                    files.append((m.group(2), m.group(1), os.path.join(d, n)))
    for stream, day, path in sorted(files):
        with open(path, "rb") as fh:
            data = fh.read()
        ok, count, start, end, problem = verify_bytes(data, stream, day)
        if ok and stream in last and last[stream][1] != start:
            ok, problem = False, f"does not follow on from {last[stream][0]}"
        good = good and ok
        out.append(f"{'ok ' if ok else 'BAD'} {os.path.basename(path)}: "
                   f"{count} line(s) {problem}".rstrip())
        last[stream] = (day, end)
    return good, out


def serve(store, token, port, cert, key, bind="0.0.0.0"):
    import ssl
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

        def _answer(self, code, doc):
            data = json.dumps(doc).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BYTES:
                return self._answer(413, {"ok": False, "error": "size"})
            body = self.rfile.read(n)
            code, doc = receive(store, token, self.headers, body)
            self._answer(code, doc)

        def do_GET(self):
            self._answer(404, {"ok": False})

    httpd = ThreadingHTTPServer((bind, port), Handler)
    if cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    else:
        print("WARNING: no --cert given, so this answers plain HTTP. LTC "
              "Player sends only to https:// addresses: put this behind a "
              "web server that does HTTPS (Caddy, nginx), or give --cert "
              "and --key.")
    print(f"Receiving LTC Player audit files into {store} on port {port}.")
    httpd.serve_forever()


def main(argv=None):
    import argparse
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["verify"]:
        if len(argv) < 2:
            print("usage: audit_receiver.py verify <store folder>")
            return 2
        ok, lines = verify_store(argv[1])
        print("\n".join(lines) or "nothing stored yet")
        print("Every stored day verifies." if ok else "A stored day does NOT "
              "verify: see above.")
        return 0 if ok else 1
    ap = argparse.ArgumentParser(description="Receive LTC Player's nightly "
                                             "audit upload.")
    ap.add_argument("--store", required=True, help="folder to keep files in")
    ap.add_argument("--token-file", required=True,
                    help="a file holding the shared token (the same text as "
                         "in LTC Player's audit upload settings)")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--cert", help="TLS certificate chain (PEM)")
    ap.add_argument("--key", help="TLS private key (PEM)")
    a = ap.parse_args(argv)
    with open(a.token_file, encoding="utf-8") as fh:
        token = fh.read().strip()
    if len(token) < 16:
        print("The token has to be at least 16 characters.")
        return 2
    os.makedirs(a.store, exist_ok=True)
    serve(a.store, token, a.port, a.cert, a.key, a.bind)
    return 0


if __name__ == "__main__":
    sys.exit(main())
