"""Tests for the witness recorder and the checker. Standard library only.

    python test_witness.py
"""
import csv
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import check_witness as cw  # noqa: E402
import laser_listener as ll  # noqa: E402
import witness as wt  # noqa: E402

BRIGHT = ll.BRIGHTNESS_ADDR
LASERS = [{"id": "laser-1", "name": "Laser 1", "mechanism": "osc",
           "host": "127.0.0.1", "port": 8100,
           "on": [["/beyond/zone/1/brightness", 10.0]],
           "off": [["/beyond/zone/1/brightness", 0.0]],
           "lock": [["/beyond/zone/1/enable", 0]]},
          {"id": "laser-2", "name": "Laser 2", "mechanism": "osc",
           "host": "127.0.0.1", "port": 8100,
           "on": [["/beyond/zone/2/brightness", 10.0]],
           "off": [], "lock": []}]

GROUPS = [{"name": "front row", "safety": 401, "fire": [411, 412],
           "arm_value": 78},
          {"name": "cat-walk", "safety": 402, "fire": [421], "arm_value": 78}]
CFG = {"universe": 1, "dest_ip": "127.0.0.1", "dest_port": 0,
       "arm_value": 78, "groups": GROUPS, "tick_hz": 40, "confirmed": False}
T0 = datetime(2026, 10, 19, 20, 0, 0)


def stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


class WitnessRun:
    """A synthetic witness folder."""

    def __init__(self, folder, companion=False):
        self.dir = folder
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "witness.json"), "w") as fh:
            json.dump({"groups": GROUPS, "arm_value": 78, "universe": 1,
                       "companion": companion}, fh)
        self.nz = open(os.path.join(folder, "nonzero.csv"), "w", newline="")
        self.nzw = csv.writer(self.nz)
        self.nzw.writerow(["time", "mono_ms", "source", "universe", "seq",
                           "priority", "n_nonzero", "slots"])
        self.gp = open(os.path.join(folder, "gaps.csv"), "w", newline="")
        self.gpw = csv.writer(self.gp)
        self.gpw.writerow(["time", "mono_ms", "gap_ms", "last_seq",
                           "next_seq", "last_nonzero"])
        self.st = open(os.path.join(folder, "stream.csv"), "w", newline="")
        self.stw = csv.writer(self.st)
        self.stw.writerow(["time", "packets", "max_gap_ms", "nonzero_packets",
                           "last_seq", "bytes", "other_universe", "multicast"])
        self.stw.writerow([stamp(T0), 400, 30, 0, 1, 1000, 0, 0])
        self.log = open(os.path.join(folder, "witness.log"), "w")
        self.seq = 0

    def packet(self, dt, slots):
        self.seq += 1
        self.nzw.writerow([stamp(dt), 0, "127.0.0.1:5", 1, self.seq, 200,
                           len(slots), ";".join(f"{s}={v}" for s, v in slots)])

    def armed_fire(self, start, seconds, fire=255, group=0):
        """Group armed and firing for `seconds` from `start`, 40 Hz."""
        g = GROUPS[group]
        n = int(seconds * 40)
        for i in range(n):
            slots = [(g["safety"], 78)]
            if fire:
                slots.append((g["fire"][0], fire))
            self.packet(start + timedelta(seconds=i / 40.0), slots)

    def gap(self, dt, ms, last_nonzero=0):
        self.gpw.writerow([stamp(dt), 0, ms, 1, 2, last_nonzero])

    def logline(self, dt, kind, msg):
        self.log.write(f"{stamp(dt)}  {kind}: {msg}\n")

    def close(self):
        for fh in (self.nz, self.gp, self.st, self.log):
            fh.close()


class LaserRun:
    """The laser listener's files inside a witness folder."""

    def __init__(self, folder, lasers=LASERS, blank_value=0.0):
        with open(os.path.join(folder, "laser.json"), "w") as fh:
            json.dump({"host": "127.0.0.1", "port": 8100,
                       "brightness_addr": BRIGHT, "blank_value": blank_value,
                       "lasers": lasers}, fh)
        self.fh = open(os.path.join(folder, "laser.csv"), "w", newline="")
        self.w = csv.writer(self.fh)
        self.w.writerow(["time", "mono_ms", "source", "port", "address",
                         "types", "args", "kind"])

    def osc(self, dt, address, types, args, kind="?"):
        self.w.writerow([stamp(dt), 0, "127.0.0.1:50000", 8100, address,
                         types, args, kind])

    def blank(self, dt):
        self.osc(dt, BRIGHT, "f", "0.0", "BLANK")

    def unblank(self, dt):
        self.osc(dt, BRIGHT, "f", "100.0", "UNBLANK")

    def close(self):
        self.fh.close()


class Journals:
    def __init__(self, folder):
        self.dir = os.path.join(folder, "nights")
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "night_2026-10-19.jsonl")
        self.fs = os.path.join(folder, "flamesafe.log")
        self.state = "IDLE"

    def rec(self, dt, action, text, outcome="done", to_state=None,
            actor="operator", fault=False):
        if to_state:
            self.state = to_state
        r = {"at": dt.isoformat(timespec="milliseconds") + "-06:00",
             "state": self.state, "to_state": self.state, "actor": actor,
             "action": action, "outcome": outcome, "text": text,
             "fault": fault, "line": text}
        with open(self.path, "a") as fh:
            fh.write(json.dumps(r) + "\n")

    def fs_line(self, dt, kind, msg):
        with open(self.fs, "a") as fh:
            fh.write(f"{dt.strftime('%Y-%m-%d %H:%M:%S')}  {kind}: {msg}\n")


def run_check(folder, nights=True, fs=True, **kw):
    w = cw.load_witness(os.path.join(folder, "w"))
    recs = cw.load_nights(os.path.join(folder, "nights")) if nights else []
    fsl = cw.load_flamesafe_log(os.path.join(folder, "flamesafe.log")) \
        if fs else []
    return cw.check(w, recs, fsl, GROUPS, 78, **kw)


def rules(res):
    return sorted({e["rule"] for e in res["episodes"]})


class TestParse(unittest.TestCase):
    def test_sacn_round_trip(self):
        vals = bytearray(512)
        vals[400] = 78
        vals[410] = 255
        b = wt.build_test_packet(7, vals, 42, terminated=True)
        p = wt.parse_sacn(b)
        self.assertEqual(p["universe"], 7)
        self.assertEqual(p["seq"], 42)
        self.assertEqual(p["priority"], 200)
        self.assertTrue(p["terminated"])
        self.assertEqual(p["source"], "witness-test")
        self.assertEqual(wt.nonzero_slots(p["values"]), [(401, 78),
                                                         (411, 255)])
        self.assertIsNone(wt.parse_sacn(b"hello world" * 20))

    def test_artnet(self):
        # opcode 0x5000 LE, protocol 14, seq 1, physical 0, SubUni 3,
        # Net 0, length 0x0200 BE, then 512 data bytes
        b = bytearray(b"Art-Net\x00") + bytes([0x00, 0x50, 0, 14, 1, 0,
                                                3, 0x00, 0x02, 0x00]) + \
            bytes(512)
        p = wt.parse_artnet(bytes(b))
        self.assertEqual(p["universe"], 3)
        self.assertEqual(p["seq"], 1)
        self.assertEqual(len(p["values"]), 512)

    def test_multicast_address(self):
        self.assertEqual(wt.sacn_multicast(1), "239.255.0.1")
        self.assertEqual(wt.sacn_multicast(6999), "239.255.27.87")

    def test_recorder_edges_and_stall(self):
        d = tempfile.mkdtemp()
        try:
            files = wt.Files(d)
            clock = [0.0]
            w = wt.Witness(CFG, files, gap_ms=100, stall_s=2.0,
                           clock=lambda: clock[0])
            vals = bytearray(512)
            w.packet(wt.build_test_packet(1, vals, 1), ("127.0.0.1", 9),
                     t=0.0)
            vals[400] = 78
            w.packet(wt.build_test_packet(1, vals, 2), ("127.0.0.1", 9),
                     t=0.025)
            w.packet(wt.build_test_packet(1, vals, 3), ("127.0.0.1", 9),
                     t=0.300)                       # a 275 ms gap
            w.packet(wt.build_test_packet(2, vals, 4), ("127.0.0.1", 9),
                     t=0.310)                       # another universe
            w.tick(t=0.5)
            self.assertTrue(w.live)
            w.tick(t=3.0)
            self.assertFalse(w.live)
            w.summary_row()
            files.close()
            text = open(files.log_path).read()
            self.assertIn("STREAM START", text)
            self.assertIn("slot 401 -> 78 (safety of front row)", text)
            self.assertIn("gap: 275 ms", text)
            self.assertIn("STREAM STOPPED WITH NONZERO ON THE WIRE", text)
            self.assertIn("other-universe", text)
            rows = list(csv.DictReader(open(os.path.join(d, "nonzero.csv"))))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["slots"], "401=78")
            gaps = list(csv.DictReader(open(os.path.join(d, "gaps.csv"))))
            self.assertEqual(len(gaps), 1)
            self.assertEqual(float(gaps[0]["gap_ms"]), 275.0)
            st = list(csv.DictReader(open(os.path.join(d, "stream.csv"))))
            self.assertEqual(st[0]["packets"], "3")
            self.assertEqual(st[0]["other_universe"], "1")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_recorder_on_a_real_socket(self):
        d = tempfile.mkdtemp()
        try:
            rc = wt.self_test(d)
            self.assertEqual(rc, 0)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_log_rotation(self):
        d = tempfile.mkdtemp()
        try:
            files = wt.Files(d)
            old = wt.LOG_LIMIT
            wt.LOG_LIMIT = 200
            try:
                for i in range(40):
                    files.log("x", "y" * 50)
            finally:
                wt.LOG_LIMIT = old
            files.close()
            self.assertTrue(os.path.exists(files.log_path + ".1"))
            self.assertFalse(os.path.exists(files.log_path + ".6"))
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestChecker(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.w = WitnessRun(os.path.join(self.d, "w"))
        self.j = Journals(self.d)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def good_show(self):
        """A show with a proper arm, fire in SHOW, Abort and Reset."""
        j, w = self.j, self.w
        j.fs_line(T0 - timedelta(minutes=5), "start",
                  "flame universe 1 to 127.0.0.1:5568")
        j.rec(T0, "start", "The show started", to_state="SHOW")
        j.rec(T0 + timedelta(seconds=2), "arm",
              "Stream Deck: front row arm pressed (held 3.0 s) by Andy.",
              actor="operator")
        w.armed_fire(T0 + timedelta(seconds=3), 2.0)
        j.rec(T0 + timedelta(seconds=10), "abort",
              "Andy pressed Abort.", to_state="IDLE")
        j.fs_line(T0 + timedelta(seconds=10), "disarm-all",
                  "the show program's Abort disarmed every group")
        j.rec(T0 + timedelta(seconds=30), "reset", "Andy pressed Reset.")

    def test_a_clean_show_passes(self):
        self.good_show()
        self.w.close()
        res = run_check(self.d)
        self.assertEqual(res["episodes"], [], res["episodes"])
        self.assertTrue(res["has_state"] and res["has_arms"] and res["has_fs"])

    def test_fire_without_arm_on_the_wire(self):
        self.good_show()
        self.w.packet(T0 + timedelta(seconds=4), [(411, 255)])
        self.w.close()
        self.assertIn("FIRE WITHOUT ARM", rules(run_check(self.d)))

    def test_bad_arm_value_and_unknown_slot(self):
        self.good_show()
        self.w.packet(T0 + timedelta(seconds=4), [(401, 79), (499, 5)])
        self.w.close()
        r = rules(run_check(self.d))
        self.assertIn("BAD ARM VALUE", r)
        self.assertIn("UNKNOWN SLOT", r)

    def test_armed_without_press(self):
        self.good_show()
        # cat-walk never pressed, yet armed on the wire
        self.w.armed_fire(T0 + timedelta(seconds=4), 1.0, fire=0, group=1)
        self.w.close()
        res = run_check(self.d)
        self.assertIn("ARMED WITHOUT PRESS", rules(res))
        ep = [e for e in res["episodes"] if e["rule"] == "ARMED WITHOUT PRESS"]
        self.assertEqual(len(ep), 1)          # one episode, 40 packets
        self.assertEqual(ep[0]["n"], 40)

    def test_fire_outside_show_and_while_latched(self):
        self.good_show()
        # fire 20 s after the Abort: latched and IDLE
        self.w.armed_fire(T0 + timedelta(seconds=20), 0.5)
        self.w.close()
        r = rules(run_check(self.d))
        self.assertIn("FIRE OUTSIDE SHOW", r)
        self.assertIn("WHILE ABORT LATCHED", r)
        self.assertIn("ARMED WITHOUT PRESS", r)   # the Abort disarmed it

    def test_a_purge_on_record_allows_fire_between_shows(self):
        j, w = self.j, self.w
        j.fs_line(T0 - timedelta(minutes=5), "start", "flame universe 1")
        j.rec(T0, "show stopped", "The show ended", to_state="IDLE")
        j.rec(T0 + timedelta(seconds=5), "arm",
              "Stream Deck: front row arm pressed (held 3.0 s) by Andy.")
        j.rec(T0 + timedelta(seconds=6), "purge",
              "Andy holds the purge: front row firing", outcome="firing")
        w.armed_fire(T0 + timedelta(seconds=6), 2.0)
        j.rec(T0 + timedelta(seconds=8), "purge",
              "The purge was released", outcome="released")
        w.close()
        self.assertEqual(rules(run_check(self.d)), [])
        # and the same fire 40 s after the release is a violation
        self.w = WitnessRun(os.path.join(self.d, "w2"))
        self.w.armed_fire(T0 + timedelta(seconds=48), 1.0)
        self.w.close()
        wr = cw.load_witness(os.path.join(self.d, "w2"))
        res = cw.check(wr, cw.load_nights(self.j.dir),
                       cw.load_flamesafe_log(self.j.fs), GROUPS, 78)
        self.assertIn("FIRE OUTSIDE SHOW", rules(res))

    def test_locked_slot(self):
        self.good_show()
        self.j.rec(T0 + timedelta(seconds=1), "lock",
                   "Andy locked out front row head 1 (slot 411)")
        self.w.close()
        res = run_check(self.d, locked_slots={411})
        self.assertIn("LOCKED SLOT", rules(res))
        self.j.rec(T0 + timedelta(seconds=2, milliseconds=500), "unlock",
                   "Andy unlocked front row head 1")
        res = run_check(self.d, locked_slots={411})
        self.assertNotIn("LOCKED SLOT", rules(res))

    def test_gaps_stalls_and_left_on_the_wire(self):
        self.good_show()
        self.w.gap(T0 + timedelta(seconds=4), 300)
        self.w.gap(T0 + timedelta(seconds=5), 2600, last_nonzero=2)
        self.w.gap(T0 - timedelta(minutes=10), 5000)   # before flamesafe ran
        self.w.logline(T0 + timedelta(seconds=5),
                       "STREAM STOPPED WITH NONZERO ON THE WIRE",
                       "no packet for 2.6 s; the last one carried 2")
        self.w.close()
        res = run_check(self.d)
        r = rules(res)
        self.assertIn("GAP", r)
        self.assertIn("STALL", r)
        self.assertIn("LEFT ON THE WIRE", r)
        stalls = [e for e in res["episodes"] if e["rule"] == "STALL"]
        self.assertEqual(len(stalls), 1)   # the one before start is skipped

    def test_flamesafe_link_loss_disarms_in_the_timeline(self):
        self.good_show()
        self.j.rec(T0 + timedelta(seconds=40), "arm",
                   "Stream Deck: front row arm pressed (held 3.0 s) by Andy.")
        self.j.fs_line(T0 + timedelta(seconds=45), "link",
                       "show program stopped answering: every group disarmed")
        self.w.armed_fire(T0 + timedelta(seconds=50), 0.5, fire=0)
        self.w.close()
        self.assertIn("ARMED WITHOUT PRESS", rules(run_check(self.d)))

    def test_without_journals_state_rules_are_unverified(self):
        self.good_show()
        self.w.close()
        res = run_check(self.d, nights=False, fs=False)
        self.assertFalse(res["has_state"])
        self.assertEqual(rules(res), [])
        counts = cw.journal_counts([], [])
        _v, text = cw.report(res, counts, cw.load_witness(self.w.dir), "x",
                             (T0, T0))
        self.assertIn("UNVERIFIED", text)

    def test_report_and_main(self):
        self.good_show()
        self.w.packet(T0 + timedelta(seconds=4), [(411, 255)])
        self.w.close()
        rc = cw.main(["--witness", self.w.dir, "--nights", self.j.dir,
                      "--flamesafe-log", self.j.fs])
        self.assertEqual(rc, 1)
        text = open(os.path.join(self.w.dir, "check_report.txt")).read()
        self.assertIn("Verdict: FAIL", text)
        self.assertIn("FIRE WITHOUT ARM", text)
        self.assertNotIn(chr(0x2014), text)
        self.assertNotIn(chr(0x2013), text)

    def test_companion_run_is_marked(self):
        shutil.rmtree(self.w.dir)
        self.w = WitnessRun(self.w.dir, companion=True)
        self.w.logline(T0, "COMPANION", "ltcplay-soak.exe is running")
        self.w.close()
        res = run_check(self.d)
        self.assertTrue(res["companion"])
        _v, text = cw.report(res, cw.journal_counts([], []),
                             cw.load_witness(self.w.dir), "x", (T0, T0))
        self.assertIn("COMPANION RUN", text)

    def test_lockout_file_slots(self):
        p = os.path.join(self.d, "ltcplay_lockout.json")
        with open(p, "w") as fh:
            json.dump({"locked": [{"machine": "head 1", "fire_slots": [411, 412],
                                   "since": "2026-10-19"}], "version": 1}, fh)
        self.assertEqual(cw.load_lockout_slots(p), {411, 412})


class TestLaserListener(unittest.TestCase):
    def test_osc_decode(self):
        m = ll.decode_osc(ll.encode_osc(BRIGHT, 0.0))
        self.assertEqual(m, [(BRIGHT, "f", [0.0])])
        m = ll.decode_osc(ll.encode_osc("/a/b", 7))
        self.assertEqual(m, [("/a/b", "i", [7])])
        m = ll.decode_osc(ll.encode_osc("/s", "hello"))
        self.assertEqual(m, [("/s", "s", ["hello"])])
        m = ll.decode_osc(ll.encode_osc("/t", True))
        self.assertEqual(m, [("/t", "T", [True])])
        b = ll.encode_bundle(ll.encode_osc("/x", 1), ll.encode_osc("/y", 2.5))
        self.assertEqual(ll.decode_osc(b), [("/x", "i", [1]),
                                            ("/y", "f", [2.5])])
        with self.assertRaises(ValueError):
            ll.decode_osc(b"hello world")
        with self.assertRaises(ValueError):
            ll.decode_osc(b"/a\x00\x00,z\x00\x00")
        self.assertEqual(ll.args_text([0.0, 100.0, 10.5, 3, "a;b", True,
                                       None]), "0.0;100.0;10.5;3;a,b;true;nil")

    def test_classify_is_exact_on_address_and_value(self):
        c = ll.classify
        self.assertEqual(c(BRIGHT, [0.0], LASERS), ("BLANK", None))
        self.assertEqual(c(BRIGHT, [0.0004], LASERS), ("BLANK", None))
        self.assertEqual(c(BRIGHT, [100.0], LASERS), ("UNBLANK", None))
        self.assertEqual(c(BRIGHT, [1.0], LASERS), ("UNBLANK", None))
        self.assertEqual(c("/beyond/zone/1/brightness", [10.0], LASERS),
                         ("TEST ON", "Laser 1"))
        # the documented low value only: anything else is a stranger
        self.assertEqual(c("/beyond/zone/1/brightness", [50.0], LASERS),
                         ("OTHER", None))
        self.assertEqual(c("/beyond/zone/1/brightness", [0.0], LASERS),
                         ("TEST OFF", "Laser 1"))
        self.assertEqual(c("/beyond/zone/1/enable", [0], LASERS),
                         ("LOCK", "Laser 1"))
        self.assertEqual(c("/beyond/general/BlackOut", [], LASERS),
                         ("FORBIDDEN", None))
        self.assertEqual(c("/beyond/general/MasterPause", [1], LASERS),
                         ("FORBIDDEN", None))
        self.assertEqual(c("/madmapper/x", ["hi"], LASERS), ("OTHER", None))
        self.assertEqual(c(BRIGHT, [0.0, 1.0], LASERS), ("OTHER", None))
        self.assertEqual(c(BRIGHT, ["0"], LASERS), ("OTHER", None))
        # a different blank value
        self.assertEqual(c(BRIGHT, [0.0], [], blank_value=5.0),
                         ("UNBLANK", None))
        self.assertEqual(c(BRIGHT, [5.0], [], blank_value=5.0),
                         ("BLANK", None))
        self.assertTrue(ll.is_blank(BRIGHT, [0.0]))
        self.assertFalse(ll.is_blank(BRIGHT, [100.0]))
        self.assertFalse(ll.is_blank("/beyond/zone/1/brightness", [0.0]))

    def test_listener_on_a_real_socket(self):
        d = tempfile.mkdtemp()
        try:
            self.assertEqual(ll.self_test(d), 0)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_refuses_a_port_already_in_use_and_writes_nothing(self):
        import socket
        import threading
        d = tempfile.mkdtemp()
        held = None
        try:
            held = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            held.bind(("127.0.0.1", 0))
            port = held.getsockname()[1]
            out = os.path.join(d, "run")
            got = []
            th = threading.Thread(target=lambda: got.append(ll.main(
                ["--port", str(port), "--host", "127.0.0.1", "--out", out,
                 "--fire-ice", os.path.join(d, "no.json"),
                 "--morning", os.path.join(d, "no.json")])), daemon=True)
            th.start()
            th.join(5)
            if th.is_alive():
                # a listener that did not refuse is running: stop it, fail
                os.makedirs(out, exist_ok=True)
                open(os.path.join(out, "STOP"), "w").close()
                th.join(5)
                self.fail("the listener did not refuse a port in use")
            self.assertEqual(got, [ll.REFUSED])
            self.assertFalse(os.path.exists(out))
            self.assertIn("already in use", ll.refusal("127.0.0.1", port, ""))
            self.assertIn("real BEYOND is running",
                          ll.refusal("127.0.0.1", 8100, "BEYOND.exe (pid 4)"))
            self.assertIn("judges the laser commands itself",
                          ll.refusal("127.0.0.1", 8100,
                                     "ltcplay-soak.exe (pid 9)"))
        finally:
            if held is not None:
                held.close()
            shutil.rmtree(d, ignore_errors=True)

    def test_refuses_while_a_beyond_process_runs(self):
        import socket
        import threading
        d = tempfile.mkdtemp()
        old = ll.beyond_running
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            ll.beyond_running = lambda: "BEYOND.exe (pid 4242)"
            out = os.path.join(d, "run")
            got = []
            th = threading.Thread(target=lambda: got.append(ll.main(
                ["--port", str(port), "--out", out,
                 "--fire-ice", os.path.join(d, "no.json"),
                 "--morning", os.path.join(d, "no.json")])), daemon=True)
            th.start()
            th.join(5)
            if th.is_alive():
                os.makedirs(out, exist_ok=True)
                open(os.path.join(out, "STOP"), "w").close()
                th.join(5)
                self.fail("the listener ran beside a BEYOND process")
            self.assertEqual(got, [ll.REFUSED])
            self.assertFalse(os.path.exists(out))
        finally:
            ll.beyond_running = old
            shutil.rmtree(d, ignore_errors=True)

    def test_listener_reads_the_configs_and_records_a_run(self):
        import socket
        import threading
        d = tempfile.mkdtemp()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            fi = os.path.join(d, "ltcplay_fire_ice.json")
            with open(fi, "w") as fh:
                json.dump({"beyond_blank": "osc",
                           "beyond": {"host": "127.0.0.1", "port": port}}, fh)
            mo = os.path.join(d, "ltcplay_morning.json")
            with open(mo, "w") as fh:
                json.dump({"lasers": [{"id": "laser-1", "name": "Laser 1",
                                       "mechanism": "osc",
                                       "on": [["/beyond/zone/1/brightness",
                                               10.0]],
                                       "off": []}]}, fh)
            out = os.path.join(d, "run")
            th = threading.Thread(target=ll.main, args=(
                ["--fire-ice", fi, "--morning", mo, "--out", out],),
                daemon=True)
            th.start()
            deadline = time.time() + 5
            while time.time() < deadline and \
                    not os.path.exists(os.path.join(out, "laser.json")):
                time.sleep(0.05)
            tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            tx.sendto(ll.encode_osc(BRIGHT, 0.0), ("127.0.0.1", port))
            tx.sendto(ll.encode_osc("/beyond/zone/1/brightness", 10.0),
                      ("127.0.0.1", port))
            tx.close()
            deadline = time.time() + 5
            p = os.path.join(out, "laser.csv")
            while time.time() < deadline:
                if os.path.exists(p) and len(open(p).read().splitlines()) >= 3:
                    break
                time.sleep(0.05)
            open(os.path.join(out, "STOP"), "w").close()
            th.join(5)
            self.assertFalse(th.is_alive())
            meta = json.load(open(os.path.join(out, "laser.json")))
            self.assertEqual(meta["port"], port)
            self.assertEqual(meta["lasers"][0]["name"], "Laser 1")
            self.assertEqual(meta["lasers"][0]["port"], port)
            rows = list(csv.DictReader(open(p)))
            self.assertEqual([r["kind"] for r in rows],
                             ["BLANK", "TEST ON Laser 1"])
            log = open(os.path.join(out, "laser.log")).read()
            self.assertIn(f"OSC on 127.0.0.1:{port}", log)
            self.assertIn("STOP file found", log)
            # and the checker reads the same run
            w = cw.load_witness(out)
            self.assertTrue(w["has_laser"])
            self.assertEqual(len(w["laser"]), 2)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_read_beyond_says_when_blanking_is_not_set_up(self):
        d = tempfile.mkdtemp()
        try:
            fi = os.path.join(d, "fi.json")
            with open(fi, "w") as fh:
                json.dump({"beyond_blank": "osc"}, fh)
            host, port, how = ll.read_beyond(fi)
            self.assertEqual((host, port), ("127.0.0.1", 8100))
            self.assertIn("blanking not set up", how)
            with open(fi, "w") as fh:
                json.dump({"beyond": {"host": "127.0.0.2", "port": 9000}}, fh)
            self.assertEqual(ll.read_beyond(fi)[:2], ("127.0.0.2", 9000))
            self.assertEqual(ll.read_beyond(os.path.join(d, "none"))[:2],
                             ("127.0.0.1", 8100))
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestLaserChecker(unittest.TestCase):
    """The laser rules, each with a case that passes and one that fails."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.w = WitnessRun(os.path.join(self.d, "w"))
        self.l = LaserRun(self.w.dir)
        self.j = Journals(self.d)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def base_show(self, blank_at_abort=True, claim=True):
        """A show: IDLE, a laser test on and off before it, SHOW with the
        restore, an accepted Abort with its blank, Reset."""
        j, w, l = self.j, self.w, self.l
        j.fs_line(T0 - timedelta(minutes=5), "start", "flame universe 1")
        j.rec(T0 - timedelta(minutes=4), "program start", "ltcplay started",
              to_state="IDLE", actor="system")
        j.rec(T0 - timedelta(minutes=4), "lasers",
              "Lasers are blanked by OSC: BEYOND's brightness 0 or 100 "
              "(beyond_blank \"osc\").", outcome="blank_mode", actor="system")
        j.rec(T0 - timedelta(seconds=60), "laser-test",
              "Andy turned the Laser 1 calibration test ON.", outcome="on")
        l.osc(T0 - timedelta(seconds=60), "/beyond/zone/1/brightness", "f",
              "10.0")
        j.rec(T0 - timedelta(seconds=50), "laser-test",
              "Andy turned the Laser 1 calibration test OFF.", outcome="off")
        l.osc(T0 - timedelta(seconds=50), "/beyond/zone/1/brightness", "f",
              "0.0")
        j.rec(T0, "start", "The show started", to_state="SHOW")
        l.unblank(T0 + timedelta(seconds=1))
        j.rec(T0 + timedelta(seconds=2), "arm",
              "Stream Deck: front row arm pressed (held 3.0 s) by Andy.")
        w.armed_fire(T0 + timedelta(seconds=3), 2.0)
        t_ab = T0 + timedelta(seconds=10)
        if blank_at_abort:
            l.blank(t_ab + timedelta(milliseconds=30))
        j.rec(t_ab + timedelta(milliseconds=60), "abort",
              "Abort, asked for by Andy on the Rack screen.", to_state="IDLE")
        if claim:
            j.rec(t_ab + timedelta(milliseconds=70), "lasers",
                  "Lasers blanked (osc): OSC brightness 0.", outcome="blanked",
                  actor="system")
        j.fs_line(t_ab, "disarm-all", "the show program's Abort disarmed "
                  "every group")
        j.rec(T0 + timedelta(seconds=30), "reset", "Andy pressed Reset.")

    def run_laser(self, **kw):
        self.w.close()
        self.l.close()
        return run_check(self.d, **kw)

    def fresh(self):
        self.tearDown()
        self.setUp()

    def test_a_clean_laser_show_passes(self):
        self.base_show()
        res = self.run_laser()
        self.assertEqual(res["episodes"], [], res["episodes"])
        lz = res["laser"]
        self.assertTrue(lz["set_up"])
        self.assertEqual(lz["aborts"], 1)
        self.assertEqual(lz["blank_claims"], 1)
        self.assertEqual(lz["counts"]["blanks"], 1)
        self.assertEqual(lz["tests"], 1)
        _v, text = cw.report(res, cw.journal_counts(cw.load_nights(self.j.dir),
                                                    []),
                             cw.load_witness(self.w.dir), "x", (T0, T0))
        self.assertIn("Verdict: PASS", text)
        self.assertIn("LASER LISTENER: 4 OSC message(s)", text)
        self.assertNotIn("UNVERIFIED", text)

    def test_blank_missing_after_abort(self):
        self.base_show(blank_at_abort=False)
        r = rules(self.run_laser())
        self.assertIn("LASER BLANK MISSING", r)
        self.assertIn("LASER BLANK NOT HEARD", r)

    def test_a_late_blank_is_missing_too(self):
        self.base_show(blank_at_abort=False)
        self.l.blank(T0 + timedelta(seconds=14))       # 4 s after the Abort
        res = self.run_laser()
        self.assertIn("LASER BLANK MISSING", rules(res))
        # the same run judged with a 5 s window passes
        self.fresh()
        self.base_show(blank_at_abort=False)
        self.l.blank(T0 + timedelta(seconds=14))
        self.assertNotIn("LASER BLANK MISSING",
                         rules(self.run_laser(blank_within_s=5.0)))

    def test_blank_after_hold_that_paused_a_show(self):
        self.base_show()
        # a Hold in IDLE (schedule on hold, nothing playing): no blank due
        self.j.rec(T0 - timedelta(seconds=120), "HOLD",
                   "Andy pressed Hold: the schedule is on hold.",
                   to_state="IDLE")
        self.assertNotIn("LASER BLANK MISSING", rules(self.run_laser()))
        # a Hold that paused the show: a blank is due
        self.fresh()
        self.base_show()
        self.j.rec(T0 + timedelta(seconds=6), "hold",
                   "Hold, asked for by Andy on the Rack screen.",
                   to_state="PAUSED")
        self.assertIn("LASER BLANK MISSING", rules(self.run_laser()))
        self.fresh()
        self.base_show()
        self.j.rec(T0 + timedelta(seconds=6), "hold",
                   "Hold, asked for by Andy on the Rack screen.",
                   to_state="PAUSED")
        self.l.blank(T0 + timedelta(seconds=6, milliseconds=100))
        self.assertNotIn("LASER BLANK MISSING", rules(self.run_laser()))

    def test_restore_outside_show_and_while_latched(self):
        self.base_show()
        self.l.unblank(T0 - timedelta(seconds=30))     # IDLE
        self.l.unblank(T0 + timedelta(seconds=20))     # latched
        r = rules(self.run_laser())
        self.assertIn("LASER LIT OUTSIDE SHOW", r)
        self.assertIn("LASER LIT WHILE ABORT LATCHED", r)
        # the restore inside SHOW in base_show raised neither
        self.fresh()
        self.base_show()
        r = rules(self.run_laser())
        self.assertNotIn("LASER LIT OUTSIDE SHOW", r)
        self.assertNotIn("LASER LIT WHILE ABORT LATCHED", r)

    def test_laser_test_messages(self):
        self.base_show()
        # ON with no record (long before the recorded one)
        self.l.osc(T0 - timedelta(seconds=200), "/beyond/zone/1/brightness",
                   "f", "10.0")
        # ON at the wrong value
        self.l.osc(T0 - timedelta(seconds=58), "/beyond/zone/1/brightness",
                   "f", "60.0")
        # ON while latched (and no record)
        self.l.osc(T0 + timedelta(seconds=20), "/beyond/zone/2/brightness",
                   "f", "10.0")
        # a forbidden address and a stranger
        self.l.osc(T0 + timedelta(seconds=21), "/beyond/general/BlackOut",
                   "", "")
        self.l.osc(T0 + timedelta(seconds=22), "/madmapper/surfaces/1", "f",
                   "1.0")
        res = self.run_laser()
        r = rules(res)
        self.assertIn("LASER TEST WITHOUT RECORD", r)
        self.assertIn("UNKNOWN LASER MESSAGE", r)
        self.assertIn("LASER LIT WHILE ABORT LATCHED", r)
        self.assertIn("FORBIDDEN LASER ADDRESS", r)
        unknown = [e for e in res["episodes"] if e["rule"] == "UNKNOWN LASER MESSAGE"]
        self.assertEqual(len(unknown), 2)       # wrong value, stranger

    def test_laser_test_while_live_and_on_a_locked_laser(self):
        self.base_show()
        # the journal says Laser 2's test went on during SHOW: live
        self.j.rec(T0 + timedelta(seconds=4), "laser-test",
                   "Andy turned the Laser 2 calibration test ON.", outcome="on",
                   to_state="SHOW")
        self.l.osc(T0 + timedelta(seconds=4), "/beyond/zone/2/brightness", "f",
                   "10.0")
        # Laser 1 locked out before its test
        self.j.rec(T0 - timedelta(seconds=70), "lockout",
                   "Laser 1 (laser) LOCKED OUT by Andy on the Rack screen.",
                   outcome="locked")
        r = rules(self.run_laser())
        self.assertIn("LASER TEST WHILE SHOW LIVE", r)
        self.assertIn("LASER TEST ON LOCKED LASER", r)
        # unlocked again before the test: no episode
        self.fresh()
        self.base_show()
        self.j.rec(T0 - timedelta(seconds=70), "lockout",
                   "Laser 1 (laser) LOCKED OUT by Andy on the Rack screen.",
                   outcome="locked")
        self.j.rec(T0 - timedelta(seconds=65), "lockout",
                   "Laser 1 (laser) UNLOCKED by Andy on the Rack screen.",
                   outcome="unlocked")
        self.assertNotIn("LASER TEST ON LOCKED LASER", rules(self.run_laser()))

    def test_test_not_heard_and_off_missing(self):
        # the journal says Laser 2 went on and the show start turned it off;
        # the wire has neither
        self.base_show()
        self.j.rec(T0 - timedelta(seconds=20), "laser-test",
                   "Andy turned the Laser 2 calibration test ON.", outcome="on")
        self.j.rec(T0, "laser-test", "Laser tests turned off: the show is "
                   "going live.", outcome="all_off", actor="system")
        res = self.run_laser()
        r = rules(res)
        self.assertIn("LASER TEST NOT HEARD", r)
        self.assertIn("LASER TEST OFF MISSING", r)
        offs = [e for e in res["episodes"] if e["rule"] == "LASER TEST OFF MISSING"]
        self.assertTrue(any("show start" in e["detail"] or "its OFF" in
                            e["detail"] for e in offs))
        # Laser 2 has no off list; a brightness blank at the show start
        # counts as dark, and the ON heard clears NOT HEARD
        self.fresh()
        self.base_show()
        self.j.rec(T0 - timedelta(seconds=20), "laser-test",
                   "Andy turned the Laser 2 calibration test ON.", outcome="on")
        self.l.osc(T0 - timedelta(seconds=20), "/beyond/zone/2/brightness", "f",
                   "10.0")
        self.j.rec(T0, "laser-test", "Laser tests turned off: the show is "
                   "going live.", outcome="all_off", actor="system")
        self.l.blank(T0 + timedelta(milliseconds=200))
        r = rules(self.run_laser())
        self.assertNotIn("LASER TEST NOT HEARD", r)
        self.assertNotIn("LASER TEST OFF MISSING", r)

    def test_test_still_on_at_abort_must_go_dark(self):
        """A test the journal says was on when the Abort came (the engine
        turns tests off at a show start, so this is the belt behind that)
        must be darkened by the Abort: its off message or the blank."""
        def on_in_show():
            self.j.rec(T0 + timedelta(seconds=5), "laser-test",
                       "Andy turned the Laser 1 calibration test ON.",
                       outcome="on", to_state="SHOW")
            self.l.osc(T0 + timedelta(seconds=5), "/beyond/zone/1/brightness",
                       "f", "10.0")
        self.base_show()
        on_in_show()
        # the brightness blank at the Abort (base_show) is dark for it too
        self.assertNotIn("LASER TEST OFF MISSING", rules(self.run_laser()))
        self.fresh()
        self.base_show(blank_at_abort=False, claim=False)
        on_in_show()
        self.assertIn("LASER TEST OFF MISSING", rules(self.run_laser()))
        # its own off message is dark enough
        self.fresh()
        self.base_show(blank_at_abort=False, claim=False)
        on_in_show()
        self.l.osc(T0 + timedelta(seconds=10, milliseconds=100),
                   "/beyond/zone/1/brightness", "f", "0.0")
        self.assertNotIn("LASER TEST OFF MISSING", rules(self.run_laser()))

    def test_blanking_not_set_up_is_expected_not_failed(self):
        self.base_show(blank_at_abort=False, claim=False)
        self.j.rec(T0 - timedelta(minutes=4), "lasers",
                   "Lasers: blanking not set up (at startup): beyond_blank is "
                   "\"osc\" but no BEYOND OSC address is set.",
                   outcome="not_set_up", actor="system", fault=True)
        self.j.rec(T0 - timedelta(minutes=3), "output",
                   "The pixel output to 10.0.0.9 failed: unreachable.",
                   outcome="failed", actor="system", fault=True)
        res = self.run_laser()
        r = rules(res)
        self.assertNotIn("LASER BLANK MISSING", r)
        self.assertNotIn("LASER SILENCE", r)
        self.assertFalse(res["laser"]["set_up"])
        counts = cw.journal_counts(cw.load_nights(self.j.dir), [])
        self.assertEqual(counts["faults (engine), 'Lasers: blanking not set up'"
                                " (EXPECTED while BEYOND is absent)"], 1)
        self.assertEqual(counts["faults (engine), other than the standing "
                                "laser fault"], 1)
        _v, text = cw.report(res, counts, cw.load_witness(self.w.dir), "x",
                             (T0, T0))
        self.assertIn("EXPECTED while BEYOND is absent", text)
        self.assertIn("Verdict: PASS", text)

    def test_timecode_mode_is_unverified_not_failed(self):
        self.base_show(blank_at_abort=False, claim=False)
        self.j.rec(T0 - timedelta(minutes=3), "lasers",
                   "Lasers are blanked by timecode (beyond_blank "
                   "\"timecode\").", outcome="blank_mode", actor="system")
        res = self.run_laser()
        self.assertNotIn("LASER BLANK MISSING", rules(res))
        self.assertTrue(any("timecode" in u for u in res["laser"]["unverified"]))

    def test_silence(self):
        self.base_show(blank_at_abort=False)
        # drop every laser row: the journal claims, the wire has nothing
        self.l.close()
        self.l = LaserRun(self.w.dir)
        res = self.run_laser()
        r = rules(res)
        self.assertIn("LASER SILENCE", r)
        self.assertNotIn("LASER BLANK MISSING", r)   # the one rule says it

    def test_no_laser_files_is_unverified(self):
        self.base_show()
        self.l.close()
        os.remove(os.path.join(self.w.dir, "laser.csv"))
        os.remove(os.path.join(self.w.dir, "laser.json"))
        self.w.close()
        res = run_check(self.d)
        self.assertIsNone(res["laser"])
        _v, text = cw.report(res, cw.journal_counts([], []),
                             cw.load_witness(self.w.dir), "x", (T0, T0))
        self.assertIn("no laser listener files", text)

    def test_main_with_a_morning_file(self):
        self.base_show()
        self.w.close()
        self.l.close()
        mo = os.path.join(self.d, "ltcplay_morning.json")
        with open(mo, "w") as fh:
            # a different low value: the heard 10.0 becomes a stranger
            json.dump({"lasers": [{"id": "laser-1", "name": "Laser 1",
                                   "mechanism": "osc",
                                   "on": [["/beyond/zone/1/brightness", 5.0]],
                                   "off": [["/beyond/zone/1/brightness", 0.0]]}]},
                      fh)
        rc = cw.main(["--witness", self.w.dir, "--nights", self.j.dir,
                      "--flamesafe-log", self.j.fs, "--morning", mo])
        self.assertEqual(rc, 1)
        text = open(os.path.join(self.w.dir, "check_report.txt")).read()
        self.assertIn("UNKNOWN LASER MESSAGE", text)
        self.assertIn("LASER TEST NOT HEARD", text)
        self.assertNotIn(chr(0x2014), text)
        rc = cw.main(["--witness", self.w.dir, "--nights", self.j.dir,
                      "--flamesafe-log", self.j.fs])
        self.assertEqual(rc, 0)


def no_dashes_in_sources():
    for name in ("witness.py", "witness.ps1", "check_witness.py",
                 "laser_listener.py", "laser_listener.ps1",
                 "Start laser listener.cmd"):
        text = open(os.path.join(HERE, name), encoding="utf-8").read()
        for ch in (chr(0x2014), chr(0x2013)):
            if ch in text:
                raise AssertionError(f"{name} has an em or en dash")


if __name__ == "__main__":
    no_dashes_in_sources()
    unittest.main(verbosity=1)
