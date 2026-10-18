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
import witness as wt  # noqa: E402

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


def no_dashes_in_sources():
    for name in ("witness.py", "witness.ps1", "check_witness.py"):
        text = open(os.path.join(HERE, name), encoding="utf-8").read()
        for ch in (chr(0x2014), chr(0x2013)):
            if ch in text:
                raise AssertionError(f"{name} has an em or en dash")


if __name__ == "__main__":
    no_dashes_in_sources()
    unittest.main(verbosity=1)
