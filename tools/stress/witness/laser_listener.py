"""The laser listener: an independent receiver standing where BEYOND would.

BENCH TOOL. It only LISTENS on this PC and writes files. It never sends to
BEYOND, the rig, flamesafe or ltcplay.

The show PC has MadMapper but, for now, no BEYOND. LTC Player still sends
its laser commands: the conductor's blank (OSC, `/beyond/master/livecontrol/
brightness` ,f 0.0) on every Abort, Hold, intermission, stopped or failed
show, its restore (,f 100.0) once a show's timecode moves, and the Morning
page's laser test messages (whatever ltcplay_morning.json's `lasers` say for
on, off and lock). With no BEYOND those packets go to nothing. This listener
binds the configured BEYOND OSC port on 127.0.0.1 (the "beyond" block of
ltcplay_fire_ice.json; the default is 127.0.0.1:8100) and every other local
port the lasers in ltcplay_morning.json name, decodes every OSC message
(bundles flattened), timestamps it, and writes

  laser.csv    one row per message: time, mono_ms, source, port, address,
               type tags, arguments, and what it looked like (BLANK,
               UNBLANK, TEST ON <laser>, TEST OFF <laser>, LOCK <laser>,
               OTHER, FORBIDDEN)
  laser.log    the same in words, plus what it listened to and why it
               stopped
  laser.json   what it listened to and the laser messages it knew, so the
               checker (check_witness.py) judges with the same table

in the witness run folder (the one witness-current.txt names; else a run
folder of its own). The same files are written by laser_listener.ps1
(PowerShell, for the Pico, which has no Python).

It REFUSES to start, with one sentence, when the port is already in use:
real BEYOND holds its OSC port while it runs, and ltcplay-soak.exe holds
127.0.0.1:8100 during the soak (it judges BEYOND's port itself). Then it
writes nothing and exits 3. It also refuses while a BEYOND process runs.

    python laser_listener.py
    python laser_listener.py --fire-ice C:\\...\\ltcplay_fire_ice.json
                             --morning C:\\...\\ltcplay_morning.json
    python laser_listener.py --port 8100 --out D:\\witness\\run
    python laser_listener.py --self-test      proves it on a spare port

Stop it with Ctrl-C, or with a file named STOP in its run folder (Stop
witness.cmd puts one there, so the witness and this listener stop
together).
"""
import argparse
import csv
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time

VERSION = "laser listener 1.0 (stress-kit)"
WINDOWS = sys.platform == "win32"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8100                      # beyond.DEFAULT_PORT (bench B8)
BRIGHTNESS_ADDR = "/beyond/master/livecontrol/brightness"
BLANK_VALUE = 0.0                        # beyond.BLANK_VALUE
UNBLANK_VALUE = 100.0                    # beyond.UNBLANK_VALUE
FORBIDDEN_ADDRESSES = ("/beyond/general/BlackOut",
                       "/beyond/general/MasterPause")
VALUE_TOLERANCE = 1e-3
LOG_LIMIT = 20 * 1024 * 1024
LOG_KEEP = 5
REFUSED = 3


# ------------------------------------------------------------ helpers ---
def now_text():
    t = time.time()
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + \
        f".{int((t % 1) * 1000):03d}"


def appdata_dir():
    if WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local")
    else:
        base = os.environ.get("XDG_STATE_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "ltcplay")


def _load_json(path):
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def read_beyond(fire_ice_path):
    """(host, port, how) from the "beyond" block of ltcplay_fire_ice.json.
    Read loosely: this program runs even if the engine would refuse the
    file, and says what it read."""
    if not fire_ice_path or not os.path.isfile(fire_ice_path):
        return DEFAULT_HOST, DEFAULT_PORT, "no ltcplay_fire_ice.json: the " \
            "default 127.0.0.1:8100"
    try:
        doc = _load_json(fire_ice_path)
    except (OSError, ValueError) as e:
        return DEFAULT_HOST, DEFAULT_PORT, (f"{fire_ice_path} could not be "
                                            f"read ({e}): the default "
                                            f"127.0.0.1:8100")
    bey = doc.get("beyond") if isinstance(doc, dict) else None
    mode = doc.get("beyond_blank", "osc") if isinstance(doc, dict) else "osc"
    if not isinstance(bey, dict):
        return DEFAULT_HOST, DEFAULT_PORT, (
            f"{fire_ice_path} has no \"beyond\" block (beyond_blank "
            f"\"{mode}\"): the conductor sends its blanks NOWHERE, the "
            f"engine shows 'Lasers: blanking not set up' (expected while "
            f"BEYOND is absent); listening on the default 127.0.0.1:8100")
    host = str(bey.get("host") or DEFAULT_HOST).strip()
    try:
        port = int(bey.get("port", DEFAULT_PORT))
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    return host, port, f"{fire_ice_path} beyond block (beyond_blank \"{mode}\")"


def read_lasers(morning_path, default_host, default_port):
    """[{"id", "name", "mechanism", "host", "port", "on", "off", "lock"}]
    from ltcplay_morning.json's lasers, each message an [address, value]
    pair. Read loosely, like read_beyond."""
    out = []
    if not morning_path or not os.path.isfile(morning_path):
        return out, "no ltcplay_morning.json: no laser test messages known"
    try:
        doc = _load_json(morning_path)
    except (OSError, ValueError) as e:
        return out, f"{morning_path} could not be read ({e})"
    lasers = doc.get("lasers") if isinstance(doc, dict) else None
    if not isinstance(lasers, list):
        return out, f"{morning_path} names no lasers"
    for i, d in enumerate(lasers):
        if not isinstance(d, dict):
            continue
        lid = str(d.get("id") or f"laser-{i + 1}")
        msgs = {}
        for key in ("on", "off", "lock"):
            lst = []
            for m in d.get(key) or []:
                if isinstance(m, (list, tuple)) and len(m) == 2 and \
                        isinstance(m[0], str):
                    lst.append([m[0], m[1]])
            msgs[key] = lst
        try:
            port = int(d.get("port") or default_port)
        except (TypeError, ValueError):
            port = default_port
        out.append({"id": lid, "name": str(d.get("name") or lid),
                    "mechanism": str(d.get("mechanism", "none")),
                    "host": str(d.get("host") or default_host),
                    "port": port, "on": msgs["on"], "off": msgs["off"],
                    "lock": msgs["lock"]})
    return out, f"{morning_path}: {len(out)} laser(s)"


# -------------------------------------------------------------- OSC ---
def _padded_string(b, i):
    """(text, next index) for the null-terminated, 4-aligned string at i."""
    end = b.index(b"\x00", i)
    s = b[i:end].decode("utf-8", "replace")
    i = end + 1
    while i % 4:
        i += 1
    return s, i


def decode_osc(b):
    """Every message in one datagram, bundles flattened, as a list of
    (address, typetags, args). Raises ValueError for anything that is not
    OSC. Written from the OSC 1.0 spec, never from ltcplay's own encoders:
    this listener shares nothing with the program it judges."""
    if b.startswith(b"#bundle\x00"):
        out = []
        i = 16                                  # "#bundle\0" + 8 byte time tag
        while i + 4 <= len(b):
            size = struct.unpack(">i", b[i:i + 4])[0]
            i += 4
            if size < 0 or i + size > len(b):
                raise ValueError("a bundle element runs past the datagram")
            out.extend(decode_osc(b[i:i + size]))
            i += size
        return out
    if not b.startswith(b"/"):
        raise ValueError("not an OSC message: no leading /")
    address, i = _padded_string(b, 0)
    tags = ""
    args = []
    if i < len(b) and b[i:i + 1] == b",":
        tags, i = _padded_string(b, i)
        tags = tags[1:]
    for tag in tags:
        if tag == "i":
            args.append(struct.unpack(">i", b[i:i + 4])[0]); i += 4
        elif tag == "f":
            args.append(struct.unpack(">f", b[i:i + 4])[0]); i += 4
        elif tag == "s" or tag == "S":
            s, i = _padded_string(b, i)
            args.append(s)
        elif tag == "b":
            n = struct.unpack(">i", b[i:i + 4])[0]
            i += 4
            args.append(f"<blob {n} bytes>")
            i += n
            while i % 4:
                i += 1
        elif tag == "h":
            args.append(struct.unpack(">q", b[i:i + 8])[0]); i += 8
        elif tag == "t":
            args.append(struct.unpack(">Q", b[i:i + 8])[0]); i += 8
        elif tag == "d":
            args.append(struct.unpack(">d", b[i:i + 8])[0]); i += 8
        elif tag in "crm":
            args.append(b[i:i + 4].hex()); i += 4
        elif tag == "T":
            args.append(True)
        elif tag == "F":
            args.append(False)
        elif tag == "N":
            args.append(None)
        elif tag == "I":
            args.append("impulse")
        elif tag in "[]":
            continue
        else:
            raise ValueError(f"unknown OSC type tag {tag!r}")
    return [(address, tags, args)]


def encode_osc(address, value):
    """A test message with one argument, for the self test only: a whole
    number (,i), a decimal (,f) or text (,s)."""
    def pad(x):
        x = x + b"\x00"
        while len(x) % 4:
            x += b"\x00"
        return x
    a = pad(address.encode("utf-8"))
    if isinstance(value, bool):
        return a + pad(b"," + (b"T" if value else b"F"))
    if isinstance(value, int):
        return a + pad(b",i") + struct.pack(">i", value)
    if isinstance(value, float):
        return a + pad(b",f") + struct.pack(">f", value)
    return a + pad(b",s") + pad(str(value).encode("utf-8"))


def encode_bundle(*messages):
    out = b"#bundle\x00" + struct.pack(">Q", 1)
    for m in messages:
        out += struct.pack(">i", len(m)) + m
    return out


def args_text(args):
    """The arguments as one CSV field: a;b;c. A float prints as Python
    does (0.0, 100.0, 10.5); the checker compares numbers numerically."""
    parts = []
    for a in args:
        if isinstance(a, bool):
            parts.append("true" if a else "false")
        elif a is None:
            parts.append("nil")
        elif isinstance(a, float):
            parts.append(repr(float(f"{a:.6g}")))
        else:
            parts.append(str(a).replace(";", ","))
    return ";".join(parts)


# -------------------------------------------------------- classifying ---
def _same_value(got, want):
    if isinstance(want, bool) or isinstance(got, bool):
        return got is want
    if isinstance(want, (int, float)) and isinstance(got, (int, float)):
        return abs(float(got) - float(want)) <= VALUE_TOLERANCE
    return str(got) == str(want)


def classify(address, args, lasers, blank_value=BLANK_VALUE,
             brightness_addr=BRIGHTNESS_ADDR):
    """What one message looked like: ("FORBIDDEN", None), ("TEST ON",
    laser name), ("TEST OFF", name), ("LOCK", name), ("BLANK", None),
    ("UNBLANK", None) or ("OTHER", None). A laser's own messages are
    checked first and exactly (address AND value), so a test ON at any
    other value than the configured low one is OTHER, never a test."""
    if address in FORBIDDEN_ADDRESSES:
        return "FORBIDDEN", None
    one = args[0] if len(args) == 1 else None
    for laser in lasers or []:
        for key, kind in (("on", "TEST ON"), ("off", "TEST OFF"),
                          ("lock", "LOCK")):
            for m in laser.get(key) or []:
                if m[0] == address and len(args) == 1 and \
                        _same_value(one, m[1]):
                    return kind, laser.get("name") or laser.get("id")
    if address == brightness_addr and isinstance(one, (int, float)) and \
            not isinstance(one, bool):
        if abs(float(one) - float(blank_value)) <= VALUE_TOLERANCE:
            return "BLANK", None
        return "UNBLANK", None
    return "OTHER", None


def is_blank(address, args, blank_value=BLANK_VALUE,
             brightness_addr=BRIGHTNESS_ADDR):
    """True for brightness at the blank value, whatever else it also is
    (a laser's off list may well be the same message)."""
    if address != brightness_addr or len(args) != 1:
        return False
    v = args[0]
    return isinstance(v, (int, float)) and not isinstance(v, bool) and \
        abs(float(v) - float(blank_value)) <= VALUE_TOLERANCE


# ----------------------------------------------------------- the files ---
class Files:
    def __init__(self, out):
        self.dir = out
        os.makedirs(out, exist_ok=True)
        self.log_path = os.path.join(out, "laser.log")
        self.lock = threading.Lock()
        p = os.path.join(out, "laser.csv")
        new = not os.path.exists(p)
        self.csv_fh = open(p, "a", newline="", encoding="utf-8")
        self.csv = csv.writer(self.csv_fh)
        if new:
            self.csv.writerow(["time", "mono_ms", "source", "port",
                               "address", "types", "args", "kind"])
            self.csv_fh.flush()

    def row(self, values):
        with self.lock:
            self.csv.writerow(values)
            self.csv_fh.flush()

    def log(self, kind, msg):
        line = f"{now_text()}  {kind}: {msg}"
        with self.lock:
            try:
                print(line, flush=True)
            except Exception:
                pass
            try:
                if os.path.exists(self.log_path) and \
                        os.path.getsize(self.log_path) > LOG_LIMIT:
                    self._rotate()
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass

    def _rotate(self):
        for i in range(LOG_KEEP - 1, 0, -1):
            a, b = f"{self.log_path}.{i}", f"{self.log_path}.{i + 1}"
            if os.path.exists(a):
                os.replace(a, b)
        os.replace(self.log_path, f"{self.log_path}.1")

    def close(self):
        try:
            self.csv_fh.close()
        except OSError:
            pass


# --------------------------------------------------------- the listener ---
class Listener:
    def __init__(self, files, lasers, blank_value=BLANK_VALUE, clock=None):
        self.files = files
        self.lasers = lasers
        self.blank_value = blank_value
        self.clock = clock or time.perf_counter
        self.t0 = self.clock()
        self.lock = threading.Lock()
        self.messages = 0
        self.blanks = 0
        self.unblanks = 0
        self.tests = 0
        self.other = 0
        self.forbidden = 0
        self.bad = 0
        self.last_t = None

    def mono_ms(self, t):
        return round((t - self.t0) * 1000.0, 1)

    def packet(self, data, addr, port, t=None):
        """One datagram heard. Never raises."""
        t = self.clock() if t is None else t
        try:
            msgs = decode_osc(data)
        except Exception as e:                           # noqa: BLE001
            with self.lock:
                self.bad += 1
            self.files.log("NOT OSC", f"{len(data)} bytes from "
                                      f"{addr[0]}:{addr[1]} on port {port} "
                                      f"that are not OSC ({e})")
            return
        for address, tags, args in msgs:
            try:
                self._message(address, tags, args, addr, port, t)
            except Exception as e:                       # noqa: BLE001
                self.files.log("listener", f"a message could not be recorded: "
                                           f"{type(e).__name__}: {e}")

    def _message(self, address, tags, args, addr, port, t):
        kind, who = classify(address, args, self.lasers, self.blank_value)
        label = kind if who is None else f"{kind} {who}"
        with self.lock:
            self.messages += 1
            self.last_t = t
            if kind == "BLANK":
                self.blanks += 1
            elif kind == "UNBLANK":
                self.unblanks += 1
            elif kind in ("TEST ON", "TEST OFF", "LOCK"):
                self.tests += 1
            elif kind == "FORBIDDEN":
                self.forbidden += 1
            else:
                self.other += 1
        self.files.row([now_text(), self.mono_ms(t), f"{addr[0]}:{addr[1]}",
                        port, address, tags, args_text(args), label])
        self.files.log(label, f"{address} ,{tags} {args_text(args)} from "
                              f"{addr[0]}:{addr[1]} on port {port}")

    def summary(self):
        with self.lock:
            return (f"{self.messages} message(s): {self.blanks} blank, "
                    f"{self.unblanks} unblank, {self.tests} laser test, "
                    f"{self.other} other, {self.forbidden} forbidden; "
                    f"{self.bad} datagram(s) that were not OSC")


# ----------------------------------------------------- ports and holders ---
def port_holder(port):
    """Who holds UDP `port` on this Windows PC: "name (pid N)" or ""."""
    if not WINDOWS:
        return ""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "udp"],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return ""
    pid = None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].upper() == "UDP" and \
                parts[1].endswith(f":{port}"):
            pid = parts[-1]
            break
    if not pid:
        return ""
    name = ""
    try:
        text = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO",
                               "CSV", "/NH"], capture_output=True, text=True,
                              timeout=20).stdout
        for row in csv.reader(text.splitlines()):
            if len(row) >= 2 and row[1] == pid:
                name = row[0]
    except Exception:
        pass
    return f"{name or 'an unknown program'} (pid {pid})"


def beyond_running():
    """The name of a running BEYOND process, or ""."""
    if not WINDOWS:
        return ""
    try:
        text = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                              capture_output=True, text=True,
                              timeout=20).stdout
        for row in csv.reader(text.splitlines()):
            if row and row[0].lower().startswith("beyond"):
                return row[0]
    except Exception:
        pass
    return ""


def refusal(host, port, holder):
    """The one sentence a refusal prints."""
    low = holder.lower()
    if "beyond" in low:
        why = ("real BEYOND is running and listening there. This listener "
               "only stands in for an ABSENT BEYOND; nothing was started "
               "and nothing was written. Judge the lasers at BEYOND itself.")
    elif "ltcplay-soak" in low:
        why = ("ltcplay-soak.exe holds BEYOND's port during the soak and "
               "judges the laser commands itself (its block report). "
               "Nothing was started. Start this listener for stages 2 to 4, "
               "once the soak has stopped.")
    elif holder:
        why = ("another program holds it (a second listener?). Nothing was "
               "started.")
    else:
        why = ("something already listens there: real BEYOND when it runs, "
               "ltcplay-soak.exe during the soak, or a second copy of this "
               "listener. Nothing was started and nothing was written.")
    return (f"REFUSED: {host}:{port} is already in use"
            + (f" by {holder}" if holder else "") + f": {why}")


def bind_all(targets):
    """Bind every (host, port); on the first failure close them all and
    return (None, the (host, port) that failed, the error)."""
    socks = []
    for host, port in targets:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((host, port))
        except OSError as e:
            s.close()
            for x, _h, _p in socks:
                x.close()
            return None, (host, port), e
        s.settimeout(0.5)
        socks.append((s, host, port))
    return socks, None, None


def listen_loop(sock, port, listener, stop):
    while not stop.is_set():
        try:
            data, addr = sock.recvfrom(65536)
        except socket.timeout:
            continue
        except OSError:
            if stop.is_set():
                return
            continue
        listener.packet(data, addr, port)


# ------------------------------------------------------------ self test ---
def self_test(out_dir):
    """Proves the listener end to end on a spare loopback port: a blank, an
    unblank, a bundle of two strangers and a laser test ON in, the right
    rows and lines out. Returns 0 or 1."""
    lasers = [{"id": "laser-1", "name": "Laser 1", "mechanism": "osc",
               "host": "127.0.0.1", "port": 0,
               "on": [["/beyond/zone/1/brightness", 10.0]],
               "off": [["/beyond/zone/1/brightness", 0.0]],
               "lock": []}]
    files = Files(out_dir)
    lst = Listener(files, lasers)
    socks, _bad, _err = bind_all([("127.0.0.1", 0)])
    sock, _h, _p = socks[0]
    port = sock.getsockname()[1]
    stop = threading.Event()
    threading.Thread(target=listen_loop, args=(sock, port, lst, stop),
                     daemon=True).start()
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dest = ("127.0.0.1", port)
    out.sendto(encode_osc(BRIGHTNESS_ADDR, 0.0), dest)
    out.sendto(encode_osc(BRIGHTNESS_ADDR, 100.0), dest)
    out.sendto(encode_bundle(encode_osc("/beyond/general/Something", 1),
                             encode_osc("/madmapper/x", "hi")), dest)
    out.sendto(encode_osc("/beyond/zone/1/brightness", 10.0), dest)
    out.sendto(b"not osc at all", dest)
    deadline = time.time() + 5
    while time.time() < deadline and (lst.messages < 5 or lst.bad < 1):
        time.sleep(0.05)
    stop.set()
    sock.close()
    out.close()
    files.close()
    ok = True
    with open(os.path.join(out_dir, "laser.csv"), encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    kinds = [r["kind"] for r in rows]
    if kinds != ["BLANK", "UNBLANK", "OTHER", "OTHER", "TEST ON Laser 1"]:
        print(f"self-test FAILED: laser.csv kinds are {kinds}")
        ok = False
    if ok and (rows[0]["address"] != BRIGHTNESS_ADDR or rows[0]["args"] != "0.0"
               or rows[1]["args"] != "100.0" or rows[2]["args"] != "1"
               or rows[3]["args"] != "hi" or rows[3]["types"] != "s"):
        print(f"self-test FAILED: laser.csv rows are {rows}")
        ok = False
    text = open(files.log_path, encoding="utf-8").read()
    for want in ("BLANK: /beyond/master/livecontrol/brightness ,f 0.0",
                 "UNBLANK: /beyond/master/livecontrol/brightness ,f 100.0",
                 "OTHER: /madmapper/x ,s hi", "TEST ON Laser 1", "NOT OSC"):
        if want not in text:
            print(f"self-test FAILED: laser.log lacks {want!r}")
            ok = False
    print("self-test " + ("passed: a blank, an unblank, a bundle, a laser test "
                          "and a stranger were heard, decoded and written to "
                          "laser.csv and laser.log" if ok else "FAILED"))
    return 0 if ok else 1


# ----------------------------------------------------------------- main ---
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fire-ice", help="ltcplay_fire_ice.json (default: Settings "
                                       "and logs)")
    ap.add_argument("--morning", help="ltcplay_morning.json (default: Settings "
                                      "and logs)")
    ap.add_argument("--host", help="override the host to bind (127.0.0.1)")
    ap.add_argument("--port", type=int, help="override BEYOND's OSC port")
    ap.add_argument("--blank-value", type=float, default=BLANK_VALUE,
                    help="the brightness value that is a blank (0)")
    ap.add_argument("--out", help="run folder (default: the witness's current "
                                  "run folder, else Settings and logs\\witness"
                                  "\\<stamp>)")
    ap.add_argument("--force", action="store_true",
                    help="listen even while a BEYOND process is seen")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--version", action="store_true")
    a = ap.parse_args(argv)
    if a.version:
        print(VERSION)
        return 0
    ad = appdata_dir()
    root = os.path.join(ad, "witness")
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    if a.self_test:
        return self_test(os.path.join(a.out or root, f"laser_selftest_{stamp}"))

    fi = a.fire_ice or os.path.join(ad, "ltcplay_fire_ice.json")
    mo = a.morning or os.path.join(ad, "ltcplay_morning.json")
    host, port, how = read_beyond(fi)
    if a.host:
        host = a.host
    if a.port:
        port = a.port
    lasers, lasers_how = read_lasers(mo, host, port)
    targets = [(host, port)]
    not_local = []
    for l in lasers:
        tgt = (l["host"], l["port"])
        if not l["host"].startswith("127."):
            not_local.append(f"{l['name']} at {l['host']}:{l['port']}")
        elif tgt not in targets:
            targets.append(tgt)
    bad_host = [f"{h}:{p}" for h, p in targets if not h.startswith("127.")]
    if bad_host:
        # BEYOND's address is another machine: nothing it is sent ends here.
        print(f"REFUSED: BEYOND's address is {', '.join(bad_host)}, not this "
              f"PC. A listener here would hear nothing; nothing was started.")
        return REFUSED

    # Refuse before anything is written: real BEYOND, the soak, or a port
    # already taken means this listener must stand aside.
    if not a.force:
        bp = beyond_running()
        if bp:
            print(refusal(host, port, bp))
            return REFUSED
    socks, failed, err = bind_all(targets)
    if socks is None:
        print(refusal(failed[0], failed[1], port_holder(failed[1])))
        print(f"(the socket said: {err})")
        return REFUSED

    out = a.out
    if not out:
        try:
            with open(os.path.join(root, "witness-current.txt"),
                      encoding="utf-8-sig") as fh:
                cur = fh.read().strip()
            if cur and os.path.isdir(cur):
                out = cur
        except OSError:
            pass
    out = out or os.path.join(root, stamp)
    files = Files(out)
    files.log("listener", f"{VERSION} started, pid {os.getpid()}, folder {out}")
    files.log("config", f"BEYOND OSC: {how}")
    files.log("config", f"lasers: {lasers_how}"
              + ("; " + "; ".join(
                  f"{l['name']} ({l['mechanism']}) on {l['on']} off {l['off']} "
                  f"lock {l['lock']}" for l in lasers) if lasers else ""))
    for n in not_local:
        files.log("note", f"{n} is not this PC: its test messages cannot be "
                          f"heard here")
    for s, h, p in socks:
        files.log("listen", f"OSC on {h}:{p}")
    with open(os.path.join(out, "laser.json"), "w", encoding="utf-8") as fh:
        json.dump({"version": VERSION, "started": now_text(), "host": host,
                   "port": port, "ports": [[h, p] for _s, h, p in socks],
                   "fire_ice": fi if os.path.isfile(fi) else None,
                   "fire_ice_how": how, "morning": mo if os.path.isfile(mo)
                   else None, "brightness_addr": BRIGHTNESS_ADDR,
                   "blank_value": a.blank_value, "unblank_value": UNBLANK_VALUE,
                   "lasers": lasers}, fh, indent=1)
    try:
        with open(os.path.join(root, "laser-current.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write(out + "\n")
    except OSError:
        pass
    lst = Listener(files, lasers, a.blank_value)
    stop = threading.Event()
    for s, h, p in socks:
        threading.Thread(target=listen_loop, args=(s, p, lst, stop),
                         daemon=True, name=f"osc-{p}").start()
    stop_file = os.path.join(out, "STOP")
    last_summary = time.time()
    try:
        while not stop.is_set():
            time.sleep(1.0)
            if time.time() - last_summary >= 600.0:
                files.log("summary", lst.summary())
                last_summary = time.time()
            if os.path.exists(stop_file):
                files.log("listener", "STOP file found; stopping")
                break
    except KeyboardInterrupt:
        files.log("listener", "Ctrl-C; stopping")
    finally:
        stop.set()
        for s, _h, _p in socks:
            try:
                s.close()
            except OSError:
                pass
        files.log("summary", lst.summary())
        files.log("listener", "stopped")
        files.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
