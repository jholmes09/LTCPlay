"""The wire witness: an independent recorder of the flame universe.

BENCH TOOL. Nothing here sends to the rig, arms anything or talks to
flamesafe or ltcplay. It only LISTENS on this PC and writes files.

It binds the flame universe's sACN destination port (flamesafe.json
"destination", 5568 in the example; flamesafe sends by unicast only, so
the destination must be this PC, 127.0.0.1 or one of its own addresses, or
nothing can be heard here), joins the universe's multicast group too (the
flame universe must never appear there), and optionally listens for Art-Net
on 6454. Every packet is timestamped; every packet carrying a nonzero slot
goes to nonzero.csv; every gap over --gap-ms goes to gaps.csv; a rolling
witness.log records starts, stalls, each slot's rise and fall, sender
changes and the stream-terminated flag. Every 10 s the LTC Player processes'
memory and the free disk go to procs.csv and disk.csv.

The same files are written by witness.ps1 (PowerShell, for the Pico, which
has no Python) and read by check_witness.py.

    python witness.py                      flamesafe.json from Settings and logs
    python witness.py --config C:\\path\\flamesafe.json --out D:\\witness
    python witness.py --companion          no sACN bind: processes and disk only
                                           (automatic while ltcplay-soak.exe
                                           runs: the soak owns that port and
                                           refuses to start if it is held)
    python witness.py --self-test          proves the recorder on a spare port

Stop it with Ctrl-C, or by putting a file named STOP in its run folder.
"""
import argparse
import csv
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time

VERSION = "witness 1.0 (stress-kit)"
WINDOWS = sys.platform == "win32"
SACN_PORT = 5568
ARTNET_PORT = 6454
LOG_LIMIT = 20 * 1024 * 1024
LOG_KEEP = 5
PROCS = ("flamesafe.exe", "ltcplay.exe", "ltcplay-deck.exe",
         "LTC Player.exe", "ltcplay-soak.exe", "ltcplay-witness")
E131_ID = b"ASC-E1.17\x00\x00\x00"


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


def find_config(given=None):
    """The flamesafe config to read the universe, destination and groups
    from: the one named, else showpc.json's, else Settings and logs'
    flamesafe.json, else the installed example. (path, how found)."""
    if given:
        return given, "named on the command line"
    ad = appdata_dir()
    try:
        with open(os.path.join(ad, "showpc.json"), encoding="utf-8-sig") as fh:
            p = json.load(fh).get("flamesafe_config")
        if p and os.path.isfile(p):
            return p, "showpc.json flamesafe_config"
    except (OSError, ValueError, AttributeError):
        pass
    p = os.path.join(ad, "flamesafe.json")
    if os.path.isfile(p):
        return p, "Settings and logs"
    for d in (r"C:\Program Files\LTC Player",
              os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                  os.path.dirname(os.path.abspath(__file__))))), "flamesafe")):
        p = os.path.join(d, "flamesafe.example.json")
        if os.path.isfile(p):
            return p, "the EXAMPLE config (no flamesafe.json found)"
    return None, "no config found"


def group_arm_value(g, arm):
    """The value a group raises its safety slots to: its safety_profile's
    arm_value (flamesafe, 2026-10-26) over its own arm_value over the
    file's."""
    prof = g.get("safety_profile")
    if isinstance(prof, dict) and "arm_value" in prof:
        return int(prof["arm_value"])
    return int(g.get("arm_value", arm))


def load_groups(path):
    """{"universe", "dest_ip", "dest_port", "arm_value", "groups":
    [{"name", "safety", "fire", "arm_value"}], "tick_hz"} from a flamesafe
    config. Read loosely: this program must run even if flamesafe would
    refuse the file, and say what it read."""
    with open(path, encoding="utf-8-sig") as fh:
        d = json.load(fh)
    arm = int(d.get("arm_value", 0))
    groups = []
    for g in d.get("groups") or []:
        saf = g.get("safety", 0)
        saf = [int(x) for x in saf] if isinstance(saf, list) else [int(saf)]
        # "safety" is the first safety slot, "safeties" every one of them
        # (flamesafe multi-safety, 2026-10-26).
        groups.append({"name": str(g.get("name", "?")),
                       "safety": saf[0] if saf else 0, "safeties": saf,
                       "fire": [int(f) for f in (g.get("fire") or [])],
                       "arm_value": group_arm_value(g, arm)})
    dest = d.get("destination") or {}
    return {"universe": int(d.get("universe", 1)),
            "dest_ip": str(dest.get("ip", "127.0.0.1")),
            "dest_port": int(dest.get("port", SACN_PORT)),
            "arm_value": arm, "groups": groups,
            "tick_hz": float(d.get("tick_hz", 40)),
            "confirmed": bool(d.get("confirmed", False))}


def slot_names(cfg):
    """{slot: "safety of <group>" | "fire of <group>"}."""
    out = {}
    for g in cfg["groups"]:
        for s in g.get("safeties") or [g["safety"]]:
            out[s] = f"safety of {g['name']}"
        for f in g["fire"]:
            out[f] = f"fire of {g['name']}"
    return out


def parse_sacn(b):
    """A decoded E1.31 data packet, or None if `b` is not one.
    {"universe", "seq", "priority", "options", "terminated", "source",
    "start_code", "values": bytes}"""
    if len(b) < 126 or b[4:16] != E131_ID:
        return None
    if b[18:22] != b"\x00\x00\x00\x04" or b[40:44] != b"\x00\x00\x00\x02":
        return None
    name = b[44:108].split(b"\x00", 1)[0].decode("utf-8", "replace")
    universe = (b[113] << 8) | b[114]
    count = ((b[123] << 8) | b[124]) - 1
    values = b[126:126 + max(0, count)]
    return {"universe": universe, "seq": b[111], "priority": b[108],
            "options": b[112], "terminated": bool(b[112] & 0x40),
            "source": name, "start_code": b[125], "values": values}


def parse_artnet(b):
    """A decoded ArtDMX packet or None. Art-Net universes are 0-based;
    the record keeps the 15-bit port-address as sent."""
    if len(b) < 18 or b[0:8] != b"Art-Net\x00":
        return None
    opcode = b[8] | (b[9] << 8)
    if opcode != 0x5000:
        return None
    length = (b[16] << 8) | b[17]
    return {"universe": ((b[15] & 0x7F) << 8) | b[14], "seq": b[12],
            "priority": "", "options": 0, "terminated": False,
            "source": "artnet", "start_code": 0,
            "values": b[18:18 + length]}


def nonzero_slots(values):
    """[(slot 1-based, value)] for every nonzero value."""
    return [(i + 1, v) for i, v in enumerate(values) if v]


def sacn_multicast(universe):
    return f"239.255.{(universe >> 8) & 0xFF}.{universe & 0xFF}"


def soak_running():
    """True when ltcplay-soak.exe is running on this Windows PC."""
    if not WINDOWS:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ltcplay-soak.exe",
                              "/FO", "CSV", "/NH"], capture_output=True,
                             text=True, timeout=15).stdout
        return "ltcplay-soak.exe" in out
    except Exception:
        return False


def process_memory():
    """[(name, pid, ws_mb)] for the LTC Player programs (Windows: tasklist;
    elsewhere nothing, this is a bench tool for the show PC)."""
    if not WINDOWS:
        return []
    out = []
    try:
        text = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                              capture_output=True, text=True,
                              timeout=15).stdout
        for row in csv.reader(text.splitlines()):
            if len(row) < 5:
                continue
            name = row[0]
            if name.lower() not in {p.lower() for p in PROCS}:
                continue
            mem = row[4].replace(",", "").replace(".", "").split()[0]
            try:
                out.append((name, int(row[1]), round(int(mem) / 1024, 1)))
            except ValueError:
                out.append((name, row[1], ""))
    except Exception:
        pass
    return out


# ----------------------------------------------------------- the files ---
class Files:
    """The run folder and everything written into it."""

    def __init__(self, out):
        self.dir = out
        os.makedirs(out, exist_ok=True)
        self.log_path = os.path.join(out, "witness.log")
        self.lock = threading.Lock()
        self.nonzero = self._csv("nonzero.csv", [
            "time", "mono_ms", "source", "universe", "seq", "priority",
            "n_nonzero", "slots"])
        self.gaps = self._csv("gaps.csv", [
            "time", "mono_ms", "gap_ms", "last_seq", "next_seq",
            "last_nonzero"])
        self.stream = self._csv("stream.csv", [
            "time", "packets", "max_gap_ms", "nonzero_packets", "last_seq",
            "bytes", "other_universe", "multicast"])
        self.procs = self._csv("procs.csv", [
            "time", "name", "pid", "ws_mb", "handles", "cpu_s"])
        self.disk = self._csv("disk.csv", ["time", "free_gb"])

    def _csv(self, name, header):
        new = not os.path.exists(os.path.join(self.dir, name))
        fh = open(os.path.join(self.dir, name), "a", newline="",
                  encoding="utf-8")
        w = csv.writer(fh)
        if new:
            w.writerow(header)
            fh.flush()
        return (fh, w)

    def row(self, which, values):
        fh, w = which
        with self.lock:
            w.writerow(values)
            fh.flush()

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
            a = f"{self.log_path}.{i}"
            b = f"{self.log_path}.{i + 1}"
            if os.path.exists(a):
                os.replace(a, b)
        os.replace(self.log_path, f"{self.log_path}.1")

    def close(self):
        for fh, _w in (self.nonzero, self.gaps, self.stream, self.procs,
                       self.disk):
            try:
                fh.close()
            except OSError:
                pass


# ---------------------------------------------------------- the witness ---
class Witness:
    def __init__(self, cfg, files, gap_ms=100.0, stall_s=2.0, clock=None):
        self.cfg = cfg
        self.files = files
        self.gap_ms = gap_ms
        self.stall_s = stall_s
        self.clock = clock or time.perf_counter
        self.t0 = self.clock()
        self.names = slot_names(cfg)
        self.last_t = None
        self.last_seq = None
        self.last_nonzero = 0
        self.last_values = b""
        self.sender = None
        self.live = False
        self.packets = 0
        self.nonzero_packets = 0
        self.bytes = 0
        self.other_universe = 0
        self.multicast_seen = 0
        self.max_gap = 0.0
        self.terminated_seen = 0
        self.lock = threading.Lock()

    def mono_ms(self, t=None):
        return round(((t if t is not None else self.clock()) - self.t0)
                     * 1000.0, 1)

    def packet(self, data, addr, t=None, multicast=False):
        """One datagram heard. Never raises."""
        t = self.clock() if t is None else t
        try:
            self._packet(data, addr, t, multicast)
        except Exception as e:                           # noqa: BLE001
            self.files.log("witness", f"a packet could not be recorded: "
                                      f"{type(e).__name__}: {e}")

    def _packet(self, data, addr, t, multicast):
        p = parse_sacn(data) or parse_artnet(data)
        with self.lock:
            self.bytes += len(data)
            if p is None:
                self.files.log("other", f"{len(data)} bytes from "
                                        f"{addr[0]}:{addr[1]} that are not "
                                        f"sACN or Art-Net")
                return
            if p["universe"] != self.cfg["universe"]:
                self.other_universe += 1
                nz = nonzero_slots(p["values"])
                if nz and self.other_universe <= 20:
                    self.files.log("other-universe",
                                   f"universe {p['universe']} from "
                                   f"{addr[0]}:{addr[1]} ({p['source']}) "
                                   f"carries {len(nz)} nonzero slot(s)")
                return
            if multicast:
                self.multicast_seen += 1
                if self.multicast_seen <= 20:
                    self.files.log("MULTICAST", f"the flame universe "
                                   f"{p['universe']} arrived by multicast "
                                   f"from {addr[0]}:{addr[1]}; flamesafe "
                                   f"never multicasts, so another program "
                                   f"is sending it")
            if p["start_code"] != 0:
                return
            self.packets += 1
            gap = None
            if self.last_t is not None:
                gap = (t - self.last_t) * 1000.0
                if gap > self.max_gap:
                    self.max_gap = gap
                if gap >= self.stall_s * 1000.0:
                    self.files.log("STREAM BACK", f"after {gap / 1000:.1f} s "
                                   f"of silence; the last packet before it "
                                   f"carried {self.last_nonzero} nonzero "
                                   f"slot(s)")
                if gap > self.gap_ms:
                    self.files.row(self.files.gaps, [
                        now_text(), self.mono_ms(t), round(gap, 1),
                        self.last_seq, p["seq"], self.last_nonzero])
                    if gap <= self.stall_s * 1000.0:
                        self.files.log("gap", f"{gap:.0f} ms between seq "
                                              f"{self.last_seq} and {p['seq']}")
            else:
                self.files.log("STREAM START", f"first packet of universe "
                               f"{p['universe']} from {addr[0]}:{addr[1]} "
                               f"({p['source']}, priority {p['priority']})")
            if self.sender != (addr[0], addr[1]):
                if self.sender is not None:
                    self.files.log("SENDER CHANGED", f"now {addr[0]}:{addr[1]} "
                                   f"({p['source']}), was "
                                   f"{self.sender[0]}:{self.sender[1]}")
                self.sender = (addr[0], addr[1])
            if p["priority"] != "" and p["priority"] != 200 and \
                    self.packets <= 3:
                self.files.log("priority", f"sACN priority is "
                               f"{p['priority']}, flamesafe sends 200")
            if p["terminated"]:
                self.terminated_seen += 1
                if self.terminated_seen <= 6:
                    self.files.log("TERMINATED", f"stream-terminated flag on "
                                   f"seq {p['seq']} (a clean stop sends six)")
            nz = nonzero_slots(p["values"])
            self._edges(p["values"])
            if nz:
                self.nonzero_packets += 1
                self.files.row(self.files.nonzero, [
                    now_text(), self.mono_ms(t), f"{addr[0]}:{addr[1]}",
                    p["universe"], p["seq"], p["priority"], len(nz),
                    ";".join(f"{s}={v}" for s, v in nz)])
            self.last_t = t
            self.last_seq = p["seq"]
            self.last_nonzero = len(nz)
            self.last_values = bytes(p["values"])
            self.live = True

    def _edges(self, values):
        """One log line per slot that rises from or falls to zero, and
        per change of value while up."""
        old = self.last_values
        n = max(len(old), len(values))
        for i in range(n):
            a = old[i] if i < len(old) else 0
            b = values[i] if i < len(values) else 0
            if a == b:
                continue
            slot = i + 1
            what = self.names.get(slot, "NOT IN ANY GROUP")
            if a == 0:
                self.files.log("RISE", f"slot {slot} -> {b} ({what})")
            elif b == 0:
                self.files.log("fall", f"slot {slot} -> 0 ({what})")
            else:
                self.files.log("change", f"slot {slot} {a} -> {b} ({what})")

    def tick(self, t=None):
        """Called about once a second: notices a stall."""
        t = self.clock() if t is None else t
        with self.lock:
            if self.live and self.last_t is not None and \
                    (t - self.last_t) >= self.stall_s:
                self.live = False
                self.files.log("STREAM STOPPED" if self.last_nonzero == 0
                               else "STREAM STOPPED WITH NONZERO ON THE WIRE",
                               f"no packet for {t - self.last_t:.1f} s; the "
                               f"last one (seq {self.last_seq}) carried "
                               f"{self.last_nonzero} nonzero slot(s)"
                               + ("" if self.last_nonzero == 0 else
                                  ": the node is holding those values"))

    def summary_row(self):
        with self.lock:
            self.files.row(self.files.stream, [
                now_text(), self.packets, round(self.max_gap, 1),
                self.nonzero_packets, self.last_seq, self.bytes,
                self.other_universe, self.multicast_seen])
            self.packets = self.nonzero_packets = self.bytes = 0
            self.other_universe = self.multicast_seen = 0
            self.max_gap = 0.0


# ----------------------------------------------------------- listeners ---
def open_sacn(bind_ip, port, universe, multicast=True):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    except OSError:
        pass
    s.bind((bind_ip, port))
    joined = ""
    if multicast:
        try:
            grp = sacn_multicast(universe)
            mreq = struct.pack("4s4s", socket.inet_aton(grp),
                               socket.inet_aton("0.0.0.0"))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            joined = grp
        except OSError:
            joined = ""
    s.settimeout(0.5)
    return s, joined


def listen_loop(sock, witness, stop, multicast_group=""):
    while not stop.is_set():
        try:
            data, addr = sock.recvfrom(65536)
        except socket.timeout:
            continue
        except OSError:
            if stop.is_set():
                return
            continue
        witness.packet(data, addr, multicast=False)


def sample_loop(files, folder, stop, every_s=10.0):
    while not stop.wait(every_s):
        try:
            for name, pid, mb in process_memory():
                files.row(files.procs, [now_text(), name, pid, mb, "", ""])
            free = shutil.disk_usage(folder).free / 1e9
            files.row(files.disk, [now_text(), round(free, 1)])
        except Exception:
            pass


# ------------------------------------------------------------ self test ---
def self_test(out_dir):
    """Proves the recorder end to end on a spare loopback port: a crafted
    sACN packet in, the right rows and lines out. Returns 0 or 1."""
    cfg = {"universe": 1, "dest_ip": "127.0.0.1", "dest_port": 0,
           "arm_value": 78, "tick_hz": 40, "confirmed": False,
           "groups": [{"name": "front row", "safety": 401,
                       "fire": [411, 412], "arm_value": 78}]}
    files = Files(out_dir)
    w = Witness(cfg, files, gap_ms=100.0, stall_s=2.0)
    sock, _j = open_sacn("127.0.0.1", 0, 1, multicast=False)
    port = sock.getsockname()[1]
    stop = threading.Event()
    th = threading.Thread(target=listen_loop, args=(sock, w, stop),
                          daemon=True)
    th.start()
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    vals = bytearray(512)
    out.sendto(build_test_packet(1, vals, 1), ("127.0.0.1", port))
    vals[400] = 78
    vals[410] = 255
    out.sendto(build_test_packet(1, vals, 2), ("127.0.0.1", port))
    vals[:] = bytes(512)
    out.sendto(build_test_packet(1, vals, 3, terminated=True),
               ("127.0.0.1", port))
    deadline = time.time() + 5
    while time.time() < deadline and w.terminated_seen < 1:
        time.sleep(0.05)
    stop.set()
    sock.close()
    out.close()
    files.close()
    ok = True
    with open(os.path.join(out_dir, "nonzero.csv"), encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if len(rows) != 1 or rows[0]["slots"] != "401=78;411=255":
        print(f"self-test FAILED: nonzero.csv has {rows}")
        ok = False
    text = open(files.log_path, encoding="utf-8").read()
    for want in ("STREAM START", "slot 401 -> 78 (safety of front row)",
                 "slot 411 -> 255 (fire of front row)", "slot 411 -> 0",
                 "TERMINATED"):
        if want not in text:
            print(f"self-test FAILED: witness.log lacks {want!r}")
            ok = False
    print("self-test " + ("passed: a crafted packet was heard, parsed, "
                          "and written to nonzero.csv and witness.log"
                          if ok else "FAILED"))
    return 0 if ok else 1


def build_test_packet(universe, values, seq, terminated=False, priority=200,
                      source=b"witness-test"):
    """An E1.31 data packet, written here from the standard (never from
    flamesafe's code: this witness shares nothing with it)."""
    values = bytes(values)
    total = 126 + len(values)
    b = bytearray(total)
    b[1] = 0x10
    b[4:16] = E131_ID
    b[16] = 0x70 | (((total - 16) >> 8) & 0x0F)
    b[17] = (total - 16) & 0xFF
    b[18:22] = b"\x00\x00\x00\x04"
    b[22:38] = b"\x01" * 16
    b[38] = 0x70 | (((total - 38) >> 8) & 0x0F)
    b[39] = (total - 38) & 0xFF
    b[40:44] = b"\x00\x00\x00\x02"
    b[44:44 + len(source)] = source
    b[108] = priority
    b[111] = seq & 0xFF
    b[112] = 0x40 if terminated else 0
    b[113] = (universe >> 8) & 0xFF
    b[114] = universe & 0xFF
    b[115] = 0x70 | (((total - 115) >> 8) & 0x0F)
    b[116] = (total - 115) & 0xFF
    b[117] = 0x02
    b[118] = 0xA1
    b[122] = 0x01
    n = len(values) + 1
    b[123] = (n >> 8) & 0xFF
    b[124] = n & 0xFF
    b[126:] = values
    return bytes(b)


# ----------------------------------------------------------------- main ---
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="flamesafe.json to read")
    ap.add_argument("--out", help="run folder (default: Settings and logs"
                                  "\\witness\\<stamp>)")
    ap.add_argument("--bind", default="0.0.0.0",
                    help="address to listen on (default every local address, "
                         "so 127.0.0.1 and this PC's own IP are both heard)")
    ap.add_argument("--port", type=int, help="sACN port (default: the "
                                             "config's destination port)")
    ap.add_argument("--universe", type=int, help="override the universe")
    ap.add_argument("--gap-ms", type=float, default=100.0)
    ap.add_argument("--stall-s", type=float, default=2.0)
    ap.add_argument("--artnet", action="store_true",
                    help="also listen for Art-Net on 6454 (off: the engine, "
                         "MadMapper or the soak usually hold it)")
    ap.add_argument("--no-multicast", action="store_true")
    ap.add_argument("--companion", action="store_true",
                    help="no sACN bind: processes and disk only")
    ap.add_argument("--force", action="store_true",
                    help="bind even while ltcplay-soak.exe runs")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--version", action="store_true")
    a = ap.parse_args(argv)
    if a.version:
        print(VERSION)
        return 0
    root = os.path.join(appdata_dir(), "witness")
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    out = a.out or os.path.join(root, stamp)
    if a.self_test:
        return self_test(os.path.join(a.out or root, f"selftest_{stamp}"))
    files = Files(out)
    files.log("witness", f"{VERSION} started, pid {os.getpid()}, folder {out}")
    cfg_path, how = find_config(a.config)
    cfg = None
    if cfg_path:
        try:
            cfg = load_groups(cfg_path)
            files.log("config", f"{cfg_path} ({how}): universe "
                                f"{cfg['universe']}, destination "
                                f"{cfg['dest_ip']}:{cfg['dest_port']}, "
                                f"arm value {cfg['arm_value']}, confirmed "
                                f"{cfg['confirmed']}, groups "
                                + "; ".join(f"{g['name']} safety "
                                            f"{g.get('safeties') or g['safety']} "
                                            f"fire {g['fire']}"
                                            for g in cfg["groups"]))
        except Exception as e:
            files.log("config", f"{cfg_path} could not be read "
                                f"({type(e).__name__}: {e}); slots will be "
                                f"recorded without group names")
    if cfg is None:
        cfg = {"universe": 1, "dest_ip": "127.0.0.1", "dest_port": SACN_PORT,
               "arm_value": 0, "groups": [], "tick_hz": 40,
               "confirmed": False}
        files.log("config", how + "; using universe 1, port 5568, no groups")
    if a.universe:
        cfg["universe"] = a.universe
    port = a.port or cfg["dest_port"]
    with open(os.path.join(out, "witness.json"), "w", encoding="utf-8") as fh:
        json.dump({"version": VERSION, "started": now_text(),
                   "config": cfg_path, "config_how": how, "universe":
                   cfg["universe"], "port": port, "bind": a.bind,
                   "arm_value": cfg["arm_value"], "groups": cfg["groups"],
                   "gap_ms": a.gap_ms, "stall_s": a.stall_s,
                   "companion": a.companion}, fh, indent=1)
    try:
        with open(os.path.join(root, "witness-current.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write(out + "\n")
    except OSError:
        pass
    w = Witness(cfg, files, a.gap_ms, a.stall_s)
    stop = threading.Event()
    socks = []
    companion = a.companion
    if not companion and not a.force and soak_running():
        companion = True
        files.log("COMPANION", "ltcplay-soak.exe is running: it owns "
                  "127.0.0.1:5568 and refuses to start if anything else "
                  "holds it, so this witness records processes and disk "
                  "only; the soak's own listener judges flamesafe's output "
                  "(flame-violations.txt in the block folder)")
    if not companion:
        dest_local = cfg["dest_ip"].startswith("127.") or a.bind != "0.0.0.0"
        if not dest_local:
            files.log("note", f"the config's destination is {cfg['dest_ip']}; "
                      f"this witness hears it only if that is one of this "
                      f"PC's own addresses. For a bench run with hardware "
                      f"disconnected, set destination to 127.0.0.1 (back up "
                      f"the file first) so the wire ends here.")
        try:
            s, joined = open_sacn(a.bind, port, cfg["universe"],
                                  not a.no_multicast)
            socks.append(s)
            files.log("listen", f"sACN on {a.bind}:{port}"
                      + (f", multicast {joined} joined" if joined else
                         ", no multicast"))
            threading.Thread(target=listen_loop, args=(s, w, stop),
                             daemon=True, name="sacn").start()
        except OSError as e:
            files.log("CANNOT LISTEN", f"{a.bind}:{port}: {e}. Another "
                      f"program holds the flame port (the soak, or a second "
                      f"witness). Nothing is recorded from the wire.")
        if a.artnet:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.bind((a.bind, ARTNET_PORT))
                s.settimeout(0.5)
                socks.append(s)
                files.log("listen", f"Art-Net on {a.bind}:{ARTNET_PORT}")
                threading.Thread(target=listen_loop, args=(s, w, stop),
                                 daemon=True, name="artnet").start()
            except OSError as e:
                files.log("artnet", f"not listening on {ARTNET_PORT}: {e}")
    threading.Thread(target=sample_loop, args=(files, out, stop),
                     daemon=True, name="samples").start()
    stop_file = os.path.join(out, "STOP")
    last_summary = time.time()
    try:
        while not stop.is_set():
            time.sleep(1.0)
            w.tick()
            if time.time() - last_summary >= 10.0:
                w.summary_row()
                last_summary = time.time()
            if os.path.exists(stop_file):
                files.log("witness", "STOP file found; stopping")
                break
    except KeyboardInterrupt:
        files.log("witness", "Ctrl-C; stopping")
    finally:
        stop.set()
        for s in socks:
            try:
                s.close()
            except OSError:
                pass
        w.summary_row()
        files.log("witness", "stopped")
        files.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
