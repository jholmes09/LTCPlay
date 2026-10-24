"""The engine's heartbeat, for an external watchdog or a smart power relay
to read (Jeff, 2026-10-24). docs/HEARTBEAT.md says how to poll it.

PUBLISH ONLY. This module answers GET and nothing else: no route here
starts, stops, arms, disarms, power cycles or changes anything, and
nothing here imports or writes to the flame link, flamesafe, sACN or
anything else that reaches the rig. Whether a watchdog acts on what it
reads, and how, is decided outside this program; nothing in it can
interrupt a running show.

Once a second, on its own thread, the beat adds one to `counter` and
takes a fresh snapshot of the engine's state (read without the engine's
locks). GET /heartbeat (or /) on 127.0.0.1:PORT returns that snapshot as
JSON, with beat_age_s, how long ago the beat last ran:

    {"service": "ltcplay-engine", "v": 1, "counter": 1234,
     "ts_utc": "2026-10-24T05:31:55.120Z", "ts_unix": 1793...,
     "mono_s": 8123.4, "beat_age_s": 0.4, "pid": 4120,
     "started_utc": "...", "every_s": 1.0,
     "engine": {"run": true, "state": "Run", "armed_groups": 0, ...},
     "black_box": {"state": "recording", "age_s": 2.1}}

A counter that stops moving while the page still answers means the
beat's thread is stuck; no answer at all within a few seconds means the
whole process (or the PC) is. Optional, both off unless configured in
ltcplay_remote.json "heartbeat": {"show_network": true} also listens on
the show network address on the same port; {"file": true} also writes
the same JSON to ltcplay_heartbeat.json in the settings folder every
beat (a temp file, then a replace).
"""
import datetime
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 7881
EVERY_S = 1.0
FILENAME = "ltcplay_heartbeat.json"
LOOPBACK = "127.0.0.1"
PATHS = ("/", "/heartbeat", "/heartbeat.json")


def _iso_utc(t):
    return datetime.datetime.fromtimestamp(
        t, datetime.timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


def load_config(doc):
    """The "heartbeat" block: ({"off", "port", "show_network", "file"},
    problem sentence or ""). A bad value falls back to its default."""
    out = {"off": False, "port": PORT, "show_network": False, "file": False}
    if doc is None:
        return out, ""
    if not isinstance(doc, dict):
        return out, ('heartbeat has to be an object like {"port": 7881}; '
                     'the defaults are used.')
    bad = []
    for key in ("off", "show_network", "file"):
        if key in doc:
            if doc[key] is True or doc[key] is False:
                out[key] = doc[key]
            else:
                bad.append(f"{key} has to be true or false")
    if "port" in doc:
        v = doc["port"]
        if isinstance(v, int) and not isinstance(v, bool) and \
                1024 <= v <= 65535:
            out["port"] = v
        else:
            bad.append("port has to be a whole number from 1024 to 65535")
    return out, ("heartbeat: " + "; ".join(bad) + ". The defaults are used "
                 "for those." if bad else "")


class _Handler(BaseHTTPRequestHandler):
    server_version = "ltcplay-heartbeat"

    def log_message(self, *a):           # never the console, never the log
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] not in PATHS:
            return self._send(404, {"error": "GET /heartbeat"})
        return self._send(200, self.server.heartbeat.view())

    do_HEAD = do_GET

    def _refuse(self):
        self._send(405, {"error": "The heartbeat is read only: GET "
                                  "/heartbeat."})

    do_POST = do_PUT = do_DELETE = do_PATCH = _refuse


class _Server(ThreadingHTTPServer):
    """One listener. Never shares its port: on Windows SO_REUSEADDR would
    let a second program bind the same port and take the answers, so it
    is off and SO_EXCLUSIVEADDRUSE is on there."""
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET,
                                   socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Heartbeat:
    """`engine_state()` -> dict; `black_box()` -> the recorder's status
    or None; `on_beat()` runs after every beat (the rack screen's black
    box check). Clocks are injected for the tests."""

    def __init__(self, engine_state=None, black_box=None, on_beat=None,
                 port=PORT, binds=(LOOPBACK,), file_path=None,
                 every_s=EVERY_S, clock=time.monotonic, wall=time.time,
                 log=print):
        self._engine_state = engine_state or (lambda: {})
        self._black_box = black_box or (lambda: None)
        self.on_beat = on_beat
        self.port = int(port)
        self.binds = tuple(binds)
        self.file_path = file_path
        self.every_s = float(every_s)
        self._clock = clock
        self._wall = wall
        self._log = log
        self.counter = 0
        self._snap = None
        self._beat_at = None
        self._started = wall()
        self._stop = threading.Event()
        self._thread = None
        self.servers = []
        self.bind_errors = {}

    def beat_once(self):
        """One beat: the counter, a fresh snapshot, the file. Never raises."""
        self.counter += 1
        now = self._wall()
        snap = {"service": "ltcplay-engine", "v": 1,
                "counter": self.counter, "ts_utc": _iso_utc(now),
                "ts_unix": round(now, 3),
                "mono_s": round(self._clock(), 3), "pid": os.getpid(),
                "started_utc": _iso_utc(self._started),
                "every_s": self.every_s}
        try:
            eng = self._engine_state()
            snap["engine"] = dict(eng) if isinstance(eng, dict) else {}
        except Exception as e:                   # noqa: BLE001
            snap["engine"] = {"error": f"{type(e).__name__}: {e}"}
        try:
            bb = self._black_box()
            snap["black_box"] = ({"state": bb.get("state"),
                                  "age_s": bb.get("age_s")}
                                 if isinstance(bb, dict) else None)
        except Exception as e:                   # noqa: BLE001
            snap["black_box"] = {"state": "unknown",
                                 "error": f"{type(e).__name__}"}
        self._snap = snap
        self._beat_at = self._clock()
        if self.file_path:
            try:
                tmp = self.file_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(snap, fh, separators=(",", ":"))
                os.replace(tmp, self.file_path)
            except OSError:
                pass
        cb = self.on_beat
        if cb is not None:
            try:
                cb()
            except Exception:                    # noqa: BLE001
                pass
        return snap

    def view(self):
        """The last snapshot and how old it is. Never raises."""
        snap = dict(self._snap or {"service": "ltcplay-engine", "v": 1,
                                   "counter": self.counter})
        snap["beat_age_s"] = (None if self._beat_at is None else
                              round(self._clock() - self._beat_at, 3))
        return snap

    def _loop(self):
        nxt = self._clock()
        while not self._stop.is_set():
            self.beat_once()
            nxt += self.every_s
            now = self._clock()
            if nxt < now:
                nxt = now
            self._stop.wait(max(0.0, nxt - now))

    def start(self):
        """The beat's thread and one listener per bind address. An address
        that cannot be bound is said once and left out; never raises."""
        if self._thread is not None:
            return self
        self.beat_once()
        for addr in self.binds:
            try:
                srv = _Server((addr, self.port), _Handler)
            except OSError as e:
                self.bind_errors[addr] = str(e)
                self._log(f"Heartbeat: could not listen on {addr}:"
                          f"{self.port}: {e}")
                continue
            srv.heartbeat = self
            self.servers.append(srv)
            threading.Thread(target=srv.serve_forever,
                             kwargs={"poll_interval": 0.5}, daemon=True,
                             name=f"ltcplay-heartbeat-{addr}").start()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="ltcplay-heartbeat")
        self._thread.start()
        return self

    def addresses(self):
        return [f"http://{s.server_address[0]}:{s.server_address[1]}"
                f"/heartbeat" for s in self.servers]

    def stop(self):
        self._stop.set()
        for s in self.servers:
            try:
                s.shutdown()
                s.server_close()
            except Exception:                    # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(2.0)


def start(settings_dir, engine_state=None, black_box=None, on_beat=None,
          config=None, show_network_address=None, log=print):
    """The Heartbeat for the show engine, started, or None when off."""
    cfg, problem = load_config(config)
    if problem:
        log(problem)
    if cfg["off"]:
        log("Heartbeat: off (heartbeat off is true in ltcplay_remote.json).")
        return None
    binds = [LOOPBACK]
    if cfg["show_network"]:
        if show_network_address and show_network_address != LOOPBACK:
            binds.append(show_network_address)
        else:
            log("Heartbeat: show_network is on but the show network address "
                "is not set, so it listens on this machine only.")
    try:
        hb = Heartbeat(engine_state=engine_state, black_box=black_box,
                       on_beat=on_beat, port=cfg["port"], binds=binds,
                       file_path=(os.path.join(settings_dir, FILENAME)
                                  if cfg["file"] else None), log=log).start()
    except Exception as e:                       # noqa: BLE001
        log(f"Heartbeat: could not start: {type(e).__name__}: {e}")
        return None
    if hb.servers:
        log("Heartbeat: " + ", ".join(hb.addresses()))
    return hb
