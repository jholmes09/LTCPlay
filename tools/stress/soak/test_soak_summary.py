"""Tests for soak_summary.py on a synthetic soak folder. Standard library.

    python test_soak_summary.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import soak_summary as ss  # noqa: E402


def make_block(soak, n, hours=1.83, verdict="PASSED", violations=0,
               leak=False, tc_fail=False, contain=True,
               audio="Focusrite USB ASIO"):
    d = os.path.join(soak, f"2026-10-19_{18 + n:02d}00 block {n}")
    os.makedirs(os.path.join(d, "flamesafe-journal"), exist_ok=True)
    lines = [f"Block {n} of an unattended run", "BENCH ONLY", "",
             f"Result: {verdict}",
             f"Run: finished. Started 2026-10-19 18:00:00, {hours:.2f} of "
             f"1.83 hour(s).", f"Show audio: {audio}", "",
             "[PASS] flamesafe output (sACN, sent to this PC)",
             "    0 violations",
             "[PASS] Art-Net timecode (from the show audio, to MadMapper's address)"
             if not tc_fail else
             "[FAIL] Art-Net timecode (from the show audio, to MadMapper's address)",
             "    longest gap 40 ms" if not tc_fail else "    longest gap 400 ms",
             "[PASS] Memory: engine", "    flat", "",
             "What happened, in order:",
             "  2026-10-19 18:00:05  scheduling protection ON; third-party "
             + ("containment on" if contain else "containment off")]
    with open(os.path.join(d, f"block {n} report.txt"), "w") as fh:
        fh.write("\r\n".join(lines) + "\r\n")
    with open(os.path.join(d, "flame-violations.txt"), "w") as fh:
        for i in range(violations):
            fh.write(f"violation {i}\nslot 411 = 255 while disarmed\n\n")
    with open(os.path.join(d, "samples.csv"), "w") as fh:
        fh.write("time,hours,name,cpu_pct,rss_mb,frames_out,send_errors,"
                 "loop_errors,drift_ms\n")
        for i in range(40):
            h = i * hours / 40
            fh.write(f"t,{h:.3f},engine,1,{100 + (h * 30 if leak else h * 1):.1f},0,0,0,0\n")
            fh.write(f"t,{h:.3f},flamesafe,1,{40 + h * 0.5:.1f},0,0,0,0\n")
    with open(os.path.join(d, "engine-stalls.json"), "w") as fh:
        json.dump({"stalls": []}, fh)
    with open(os.path.join(d, "flamesafe-journal", "flamesafe.log"), "w") as fh:
        fh.write("2026-10-19 18:00:00  start: flame universe 1 to 127.0.0.1\n")
        fh.write("2026-10-19 18:30:00  disarm-all: the show program's Abort\n")
    return d


def make_soak(root, blocks=27, **kw):
    soak = os.path.join(root, "soak")
    os.makedirs(os.path.join(soak, "daily"), exist_ok=True)
    now = time.time()
    with open(os.path.join(soak, "unattended.json"), "w") as fh:
        json.dump({"contain": True, "block_minutes": 110}, fh)
    with open(os.path.join(soak, "status.json"), "w") as fh:
        json.dump({"phase": "finished", "block": blocks, "last_verdict":
                   "PASSED", "free_disk_gb": 120.5, "updated": "x",
                   "run_started": now - 48 * 3600, "until": now}, fh)
    with open(os.path.join(soak, "daily", "2026-10-19.txt"), "w") as fh:
        fh.write("LTC Player unattended soak, 2026-10-19. BENCH ONLY.\n")
        for n in range(1, blocks + 1):
            fh.write(f"20:0{n % 10} block {n} (1.83 h): PASSED; report x\n")
    for n in range(1, blocks + 1):
        make_block(soak, n, **kw)
    return soak


class TestSummary(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def verdicts(self, s):
        return {t[:2]: v for v, t, _d in s["checks"]}

    def test_a_good_run_passes(self):
        soak = make_soak(self.d)
        s = ss.summarize(soak, 48.0)
        self.assertEqual(s["verdict"], "PASS", s["checks"])
        self.assertEqual(len(s["blocks"]), 27)
        text = ss.render(s, soak)
        self.assertIn("(PASS)", text)
        self.assertNotIn(chr(0x2014), text)

    def test_violations_fail(self):
        soak = make_soak(self.d, violations=1)
        s = ss.summarize(soak, 48.0)
        self.assertEqual(self.verdicts(s)["2."], "FAIL")

    def test_leak_fails(self):
        soak = make_soak(self.d, leak=True)
        s = ss.summarize(soak, 48.0)
        self.assertEqual(self.verdicts(s)["3."], "FAIL")
        self.assertIn("engine +30", s["checks"][2][2])

    def test_timecode_fail_fails(self):
        soak = make_soak(self.d, tc_fail=True, verdict="FAILED")
        s = ss.summarize(soak, 48.0)
        v = self.verdicts(s)
        self.assertEqual(v["1."], "FAIL")
        self.assertEqual(v["4."], "FAIL")

    def test_containment_off_fails(self):
        soak = make_soak(self.d, contain=False)
        with open(os.path.join(soak, "unattended.json"), "w") as fh:
            json.dump({"contain": False, "block_minutes": 110}, fh)
        s = ss.summarize(soak, 48.0)
        self.assertEqual(self.verdicts(s)["5."], "FAIL")

    def test_too_few_hours(self):
        soak = make_soak(self.d, blocks=5)
        s = ss.summarize(soak, 48.0)
        self.assertEqual(self.verdicts(s)["6."], "FAIL")
        with open(os.path.join(soak, "unattended_state.json"), "w") as fh:
            json.dump({"block": 5}, fh)
        with open(os.path.join(soak, "status.json"), "w") as fh:
            json.dump({"phase": "block running", "block": 6}, fh)
        s = ss.summarize(soak, 48.0)
        self.assertEqual(s["verdict"], "STILL RUNNING")

    def test_fake_audio_is_said_everywhere_and_is_not_a_pass_alone(self):
        soak = make_soak(self.d, audio="FAKE stand-in device (CI run)")
        s = ss.summarize(soak, 48.0)
        self.assertEqual(s["verdict"], "PASS (AUDIO WAS FAKE)")
        self.assertEqual(len(s["fake_audio"]), 27)
        text = ss.render(s, soak)
        self.assertIn("(PASS (AUDIO WAS FAKE))", text)
        self.assertIn("AUDIO WAS FAKE in block(s) 1, 2", text)
        self.assertIn("NOT the real audio path", text)
        self.assertIn("| FAKE stand-in device (CI run) |", text)
        # a real run says which interface and never the word FAKE
        soak2 = make_soak(os.path.join(self.d, "b"))
        s2 = ss.summarize(soak2, 48.0)
        self.assertEqual(s2["verdict"], "PASS")
        text2 = ss.render(s2, soak2)
        self.assertNotIn("FAKE", text2)
        self.assertIn("real interface: Focusrite USB ASIO", text2)

    def test_no_interface_fails_the_audio_criterion(self):
        soak = make_soak(self.d, audio="NO interface found")
        s = ss.summarize(soak, 48.0)
        self.assertEqual(self.verdicts(s)["7."], "FAIL")
        self.assertTrue(s["verdict"].startswith("FAIL"))
        text = ss.render(s, soak)
        self.assertIn("NO AUDIO INTERFACE in block(s)", text)
        self.assertIn("no show played", text)

    def test_main_writes_the_summary(self):
        soak = make_soak(self.d)
        rc = ss.main(["--soak", soak, "--no-desktop"])
        self.assertEqual(rc, 0)
        self.assertTrue(any(f.startswith("SOAK SUMMARY") for f in
                            os.listdir(soak)))


if __name__ == "__main__":
    unittest.main(verbosity=1)
