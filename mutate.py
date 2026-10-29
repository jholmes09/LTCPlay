#!/usr/bin/env python3
"""Mutation proofing: break one guarantee at a time and require the suite to
notice it.

A test that passes against a deliberately broken program is worse than no test,
because it is evidence of safety that is not there.  Every entry below is a
real failure this program could have on a show night; if the suite stays green
against one, the suite is lying and gets strengthened until it does not.

    python3 mutate.py            run them all
    python3 mutate.py park       run the ones whose name contains "park"

For CI, two options that change nothing about a plain run:

    --shard I/N        run only every Nth mutation, starting at the Ith
                       (0-based), so N machines can share one sweep
    --expected FILE    a list of mutations this machine is known to miss,
                       each with its reason; see mutate_expected_misses.txt.
                       In this mode a mutation counts as caught only when
                       the suite fails under it twice running.

And three that narrow the sweep to what a change can have broken (the
per-PR gate; the nightly sweep and a release tag still run everything):

    --changed BASE     run only the mutations a diff against git revision
                       BASE can have changed the result of: see
                       select_affected() for the rules
    --files a,b,c      the same, with the changed files named by hand
                       (no git needed, e.g. on the show PC)
    --list             print what would run and why, then stop
    --catchers FILE    {mutation name: test that caught it} from an earlier
                       sweep, so a changed test re-runs only what it guards

Speed. None of it changes what a mutation has to survive: the baseline runs
every test in file order, a mutant is caught only when the suite fails under
it (twice, with --expected), and the restore is proved byte for byte.

    (default)          fail fast: a mutated run stops after the first test
                       that failed a check (LTC_SELFTEST_FAILFAST in
                       selftest.py). --no-failfast runs every test.
    --hints FILE       mutate_catchers.json: run the test that caught each
                       mutation last time first. A run counts only when it
                       fails in that test, and that test passes when run
                       first with nothing mutated (checked once per test);
                       anything else runs again in file order. A hinted
                       catch is confirmed by a plain file-order run with no
                       hint. A stale or missing hint costs time, never a
                       verdict.
    --write-catchers F add the test each caught mutation stopped in to F
    --jobs N           N mutations at once, each worker in its own copy of
                       the tree with its own home and app-data
                       folders, and each copy runs its own whole baseline
                       first
"""
import json
import shutil
import subprocess
import sys
import os
import tempfile
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))

# (name, file, exact text to find, replacement)
MUTATIONS = [
 ("the input hunts sample rates on every retry again", "ltcplay/audio.py",
  "        if self._good:\n            want(self._good[0], self._good[1])\n"
  "        want(self.rate, chans)",
  "        if False:\n            want(self._good[0], self._good[1])"),

 ("a working setting is never remembered", "ltcplay/audio.py",
  "            self._good = (rate, ch)",
  "            pass"),

 ("a changed channel count is not followed", "ltcplay/audio.py",
  "            self._open_channels = ch",
  "            pass"),

 ("a failed open says nothing about the device", "ltcplay/audio.py",
  'f". The device reports {have} input(s) at "',
  'f". "'),

 ("a fault that stopped is drawn red forever again", "ltcplay/display.py",
  "def _recent(obj):\n    \"\"\"Did this thing fail within the window that still counts as now?\"\"\"\n    age = getattr(obj, \"seconds_since_error\", None)\n    return age is not None and age <= RECENT_S",
  "def _recent(obj):\n    return True"),

 ("history is dropped instead of shown quietly", "ltcplay/display.py",
  "def history_for(p):",
  "def history_for(p):\n    return []\ndef _unused_history(p):"),

 ("the page never shows the history list", "ltcplay/web/index.html",
  '  (s.history||[]).forEach(t => { const p=document.createElement("p"); p.className="info"; p.textContent=t; w.appendChild(p); });\n',
  ''),

 ("a recovered input keeps reporting its old failure", "ltcplay/session.py",
  """            "input_error": ("" if (a is not None and a.attached)
                            else (getattr(a, "last_error", "")
                                  or self.input_error)),""",
  """            "input_error": (self.input_error
                            or getattr(a, "last_error", "")),"""),

 ("a missing input is filed as a permanent note again",
  "ltcplay/session.py",
  """                if self.log:
                    self.log.event("input", str(e).split("\\n")[0])""",
  """                self.notes.append("not attached: " + str(e))"""),

 ("a lost feed no longer runs the set out", "ltcplay/player.py",
  '            if self.on_lost == "freerun" and self.last_ltc_seconds is not None \\',
  '            if False and self.last_ltc_seconds is not None \\'),

 ("the free run starts from now instead of where the feed died",
  "ltcplay/player.py",
  "                self.freerun_epoch = last - self.last_ltc_seconds",
  "                self.freerun_epoch = now"),

 ("rebuilding the input only reopens the stream", "ltcplay/session.py",
  """        try:
            sd._terminate()
            sd._initialize()
            rebuilt = True""",
  """        try:
            rebuilt = True"""),

 ("rebuilding the input reuses the old decoder", "ltcplay/session.py",
  "        self.dec = LTCDecoder(self.rate)\n        opened = src.start()",
  "        opened = src.start()"),

 ("the page calls a queue acceptance a good send again",
  "ltcplay/web/index.html",
  "` · accepted by this Mac ${s.since_ok.toFixed(1)}s ago` : \"\") +",
  "` · last good send ${s.since_ok.toFixed(1)}s ago` : \"\") +"),

 ("a dead controller is hammered every frame forever", "ltcplay/output.py",
  """            if p["quiet_until"] > now:
                quiet += 1
                continue""",
  """            if False:
                quiet += 1
                continue"""),

 ("a rested controller is never retried", "ltcplay/output.py",
  '                    p["quiet_until"] = now + p["quiet_for"]',
  '                    p["quiet_until"] = now + 1e9'),

 ("one refused packet rests a whole controller", "ltcplay/output.py",
  "    DEST_FAILS_BEFORE_QUIET = 20",
  "    DEST_FAILS_BEFORE_QUIET = 1"),

 ("broadcast is enabled on every socket again", "ltcplay/output.py",
  """            if self.broadcast_dests:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)""",
  """            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)"""),

 ("nothing warns about a broadcast destination", "ltcplay/display.py",
  "    if bcast:",
  "    if False:"),

 ("a poisoned PortAudio is never rebuilt, only the stream retried",
  "ltcplay/audio.py",
  """            if self._fails_since_reset >= self.FAILURES_BEFORE_RESET:
                self._fails_since_reset = 0
                self._reset_portaudio()
            return False
        self._stream = s""",
  """            return False
        self._stream = s"""),

 ("the audio system is rebuilt on every single failed open",
  "ltcplay/audio.py",
  "    FAILURES_BEFORE_RESET = 3",
  "    FAILURES_BEFORE_RESET = 0"),

 ("a good open does not clear the failure count", "ltcplay/audio.py",
  """        self.attached = True
        self._fails_since_reset = 0""",
  """        self.attached = True"""),

 ("a rebuild that failed is reported as having happened",
  "ltcplay/audio.py",
  """            self._event("audio", self.last_error, throttle=10.0)
            return False
        self.pa_resets += 1""",
  """            self._event("audio", self.last_error, throttle=10.0)
        self.pa_resets += 1"""),

 ("an input stuck forever keeps reporting itself as recovering",
  "ltcplay/display.py",
  '        if getattr(a, "stuck", False):',
  "        if False:"),

 ("the installer trusts that python3 exists instead of running it",
  "Install ltcplay.command",
  'if ! PYV=$("$PY" -V 2>&1); then',
  'PYV="assumed"; if false; then'),

 ("the environment error is thrown away again",
  "Install ltcplay.command",
  'if ! VENVLOG=$("$PY" -m venv .venv 2>&1); then',
  'VENVLOG=""; if ! "$PY" -m venv .venv; then'),

 ("the input caption goes back to claiming a missing input fails the run",
  "ltcplay/web/index.html",
  '`A run will start on the preshow look and pick it up when it ` +\n        `appears, or save a different one.`',
  '`The run will fail until it is plugged in, or you save a different one.`'),

 ("the GO button claims it starts from the top again",
  "ltcplay/web/index.html",
  '<button id="btn-go">GO</button>',
  '<button id="btn-go">GO from the top</button>'),

 ("the terminal calls the button by a name it does not have",
  "ltcplay/display.py",
  '"the next one. Press Back to timecode on the web page to "',
  '"the next one. Release it from the web page to "'),

 ("a button named in the copy is renamed out from under it",
  "ltcplay/web/index.html",
  '<button id="btn-release" hidden>Back to timecode</button>',
  '<button id="btn-release" hidden>Follow the feed</button>'),

 ("Run asks a second time again", "ltcplay/web/index.html",
  '$("btn-run").addEventListener("click", () => startShow(false));',
  '$("btn-run").addEventListener("click", () => { if(!confirm("go?")) return; startShow(false); });'),

 ("Stop asks a second time again", "ltcplay/web/index.html",
  '$("btn-stop").addEventListener("click", async () => {\n  try{ await post("/api/stop")',
  '$("btn-stop").addEventListener("click", async () => {\n  if(!confirm("stop?")) return;\n  try{ await post("/api/stop")'),

 ("preflight findings vanish with the modal", "ltcplay/web/index.html",
  '  (s.problems||[]).forEach(t => { const p=document.createElement("p"); p.className="info"; p.textContent=t; w.appendChild(p); });\n',
  ''),

 ("a missing input refuses the start again", "ltcplay/session.py",
  """            except audio_mod.DeviceError as e:
                # A missing interface used to refuse the whole start,""",
  """            except audio_mod.DeviceError as e:
                raise SessionError(str(e))
                # A missing interface used to refuse the whole start,"""),

 ("the input is never retried once it is absent", "ltcplay/audio.py",
  '        if self._opens or self.device.get("index") is None:',
  '        if self._opens and self.device.get("index") is None:'),

 ("a first open that fails is fatal again", "ltcplay/audio.py",
  """        self._running = True
        opened = self._open()""",
  """        self._running = True
        opened = self._open()
        if not opened:
            raise DeviceError(self.last_error)"""),

 ("the decoder ignores the clock the input actually runs at",
  "ltcplay/audio.py",
  """                self.rate = rate
                if self.on_rate_change:""",
  """                if False:"""),

 ("a show with no input reports it as fine", "ltcplay/session.py",
  '            "input_attached": (True if self.wav\n                               else None if not input_used\n                               else bool(a and a.attached)),',
  '            "input_attached": True,'),

 ("the input cannot be changed while the show runs", "ltcplay/web.py",
  "        if s is not None and s.running:",
  "        if False:"),

 ("switching the input leaves the old one feeding the show",
  "ltcplay/session.py",
  """        old, self.audio = self.audio, None
        self.player.audio = None
        try:
            old.stop()""",
  """        old, self.audio = self.audio, None
        self.player.audio = None
        try:
            pass"""),

 ("a refused switch is reported as done", "ltcplay/web.py",
  """            except SessionError as e:
                out = dict(out, live=False, why=str(e))""",
  """            except SessionError as e:
                out = dict(out, live=True, opened=True)"""),

 ("nothing says the input is missing", "ltcplay/display.py",
  "        elif not attached:",
  "        elif False:"),

 ("the header hangs its text on the logo's baseline",
  "ltcplay/web/index.html",
  "header{display:flex;align-items:center;gap:16px;flex-wrap:wrap;",
  "header{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;"),

 ("the title and the status pills lose their groups",
  "ltcplay/web/index.html",
  '  <div class="titles">\n    <h1 id="brandshow">Show control</h1>',
  '  <div class="nope">\n    <h1 id="brandshow">Show control</h1>'),

 ("the folder picker accepts a folder with no sequences in it",
  "ltcplay/cli.py",
  """    if not counts["fseq"]:
        return False,""",
  """    if False:
        return False,"""),

 ("the folder picker accepts a folder with no controller map",
  "ltcplay/cli.py",
  """    if not counts["networks"]:
        return False,""",
  """    if False:
        return False,"""),

 ("changing the folder rewrites the whole show file",
  "ltcplay/web.py",
  '        doc["show_dir"] = (os.path.relpath(chosen,',
  '        doc = {}\n        doc["show_dir"] = (os.path.relpath(chosen,'),

 ("the folder can be changed under a running show", "ltcplay/web.py",
  """        if self._starting or (self.session is not None
                              and self.session.running):""",
  "        if False:"),

 ("the folder can be changed while a start is in flight", "ltcplay/web.py",
  "        if self._starting or (self.session is not None",
  "        if False and self._starting or (self.session is not None"),

 ("a refused folder is written anyway", "ltcplay/web.py",
  """        if not ok:
            raise SessionError(why)""",
  """        if not ok:
            pass"""),

 ("a show file can be named by path, not just by name", "ltcplay/web.py",
  'path = os.path.join(self.folder, os.path.basename(timeline or ""))',
  'path = os.path.join(self.folder, timeline or "")'),

 ("a half-written show file is left where the real one was",
  "ltcplay/cli.py",
  """        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)""",
  """        os.replace(tmp, path)
    except BaseException:
        try:
            pass"""),

 ("the folder picker is drawn before a show file is chosen",
  "ltcplay/web/index.html",
  "  if(andFolder !== false) loadShowDir();",
  "  if(false) loadShowDir();"),

 ('output loop exception guard removed',
  'ltcplay/player.py',
  '            except Exception as e:\n                # Nothing raised in here may be allowed to end this thread.',
  '            except ZeroDivisionError as e:\n                # Nothing raised in here may be allowed to end this thread.'),

 ("supervisor never restarts a dead thread", "ltcplay/player.py",
  "if self._running and (t is None or not t.is_alive()):", "if False:"),

 ("no timecode goes black instead of the preshow loop", "ltcplay/player.py",
  """            self.current_frame = -1
            out = self._idle_frame()""",
  """            self.current_frame = -1
            out = b\"\""""),

 ("LOST falls back to black instead of the preshow loop", "ltcplay/player.py",
  """            out = self._idle_frame()
            self._note_change(prev_state, prev_cue, prev_source)
            return out

        # While parked""",
  """            out = b""
            self._note_change(prev_state, prev_cue, prev_source)
            return out

        # While parked"""),

 ("LTC readout tracks the free-rolling clock instead of freezing",
  "ltcplay/player.py",
  """        self.tc_seconds = tc
        return self._play_at(tc, prev_state, prev_cue, prev_source)""",
  """        self.tc_seconds = tc
        self.last_ltc_text = self.timeline.format(tc)
        return self._play_at(tc, prev_state, prev_cue, prev_source)"""),

 ("up next is off by one at a cue boundary", "ltcplay/timeline.py",
  "if self.cues[mid].tc_seconds <= tc_seconds:",
  "if self.cues[mid].tc_seconds < tc_seconds:"),

 ("up next returns the cue that is playing", "ltcplay/timeline.py",
  "i = self._index_at(tc_seconds) + 1", "i = self._index_at(tc_seconds)"),

 ("a parked source is not detected at all", "ltcplay/player.py",
  """            if same:
                if self._park_since is None:""",
  """            if False:
                if self._park_since is None:"""),

 ("a parked source drags the clock forward", "ltcplay/player.py",
  """            if self._park_since is not None:
                # Leave the epoch alone.""",
  """            if False:
                # Leave the epoch alone."""),

 ("the output thread does something the stepped tests do not",
  "ltcplay/player.py",
  "                self.frames_sent += 1\n",
  "                self.frames_sent += 1\n"
  "                self.frames_sent += 0\n"),

 ("a freewheel runs at half speed", "ltcplay/player.py",
  """        tc = self.last_ltc_seconds if self.state == PARKED and \\
            self.last_ltc_seconds is not None else now - epoch""",
  """        tc = self.last_ltc_seconds if self.state == PARKED and \\
            self.last_ltc_seconds is not None else (
                now - epoch if self.state != FREEWHEEL
                else (last - epoch) + (now - last) / 2)"""),

 ("parked playback uses the free-rolling clock", "ltcplay/player.py",
  """        tc = self.last_ltc_seconds if self.state == PARKED and \\
            self.last_ltc_seconds is not None else now - epoch""",
  """        tc = now - epoch"""),

 ("park threshold so long a real pause never registers", "ltcplay/player.py",
  "self.park_s = park_ms / 1000.0", "self.park_s = 9999.0"),

 ("on-lost hold is ignored", "ltcplay/player.py",
  'if self.on_lost == "hold" and prev_cue is not None:', "if False:"),

 ("on-lost blackout is ignored", "ltcplay/player.py",
  'if self.on_lost == "blackout":', "if False:"),

 ("a sequence that keeps failing holds forever instead of going dark",
  "ltcplay/player.py",
  "if now - self._render_bad_since < 1.0:", "if True:"),

 ("socket never actually reopens", "ltcplay/output.py",
  """        self._last_reopen_at = now
        if self._open():""",
  """        self._last_reopen_at = now
        if False:"""),

 ("failed sends never tear the socket down", "ltcplay/output.py",
  "FAILURES_BEFORE_REOPEN = 3", "FAILURES_BEFORE_REOPEN = 1000000"),

 ("a successful send stops resetting the failure counter", "ltcplay/output.py",
  """        if any_ok:
            self._consecutive_failures = 0""",
  """        if any_ok:
            pass"""),

 ("measured rate reports the integer count, hiding 29.97", "ltcplay/ltc.py",
  "self._measured = d_frames / span", "self._measured = float(n)"),

 ("drop frame correction dropped", "ltcplay/tc.py",
  "total -= 2 * (mins - mins // 10)", "total -= 0"),

 ("a stalled frame number no longer restarts the rate window", "ltcplay/ltc.py",
  "stalled = self._prev_idx is not None and idx <= self._prev_idx",
  "stalled = False"),

 ("the rate window compares against the anchor, not the last frame",
  "ltcplay/ltc.py",
  "stalled = self._prev_idx is not None and idx <= self._prev_idx",
  "stalled = idx <= self._anchor[0] if self._anchor else False"),

 ("a nonsense measurement is snapped to the nearest rate anyway",
  "ltcplay/ltc.py",
  """        if len(near) == 1:
            return (near[0], self.last_drop, True)
        return (float(n), self.last_drop, False)""",
  """        cands = [r for r in COMMON_RATES if abs(round(r) - n) < 0.5]
        best = min(cands, key=lambda r: abs(r - mea)) if cands else float(n)
        return (best, self.last_drop, len(near) == 1)"""),

 ("the rate tolerance is loose enough to admit both candidates",
  "ltcplay/ltc.py", "RATE_TOLERANCE = 0.0004", "RATE_TOLERANCE = 0.002"),

 ("a stale measurement is reported as a live one", "ltcplay/ltc.py",
  """        if self._measured_span < self.MIN_RATE_WINDOW_S:
            return None
        return self._measured""",
  """        return self._measured"""),

 ("the generator counts seconds times the rate", "ltcplay/ltc.py",
  "total_frames = ((h * 60 + m) * 60 + s) * count + f",
  "total_frames = int(round(((h * 60 + m) * 60 + s) * fps)) + f"),

 ("the input stream only ever opens one channel", "ltcplay/audio.py",
  "self._open_channels = max(1, int(channel))", "self._open_channels = 1"),

 ("the callback always reads input 1", "ltcplay/audio.py",
  "col = min(self._open_channels, indata.shape[1]) - 1", "col = 0"),

 ("a silent input is never rebuilt", "ltcplay/audio.py",
  """            quiet = (self.last_block_at is None or
                     now - self.last_block_at > self.SILENCE_BEFORE_REOPEN_S)""",
  """            quiet = False"""),

 ("a healthy input is rebuilt anyway", "ltcplay/audio.py",
  "if not quiet and self._stream is not None:", "if False:"),

 ("an ambiguous device name is guessed at", "ltcplay/audio.py",
  """    if len(hits) == 1:
        return hits[0]""",
  """    if hits:
        return hits[0]"""),

 ("the sample rate is never checked with the device", "ltcplay/audio.py",
  """        try:
            sd.check_input_settings(device=device["index"], channels=channels,
                                    samplerate=r, dtype="float32")
            return r
        except Exception:
            continue""",
  """        return r"""),

 ("clipping is not reported", "ltcplay/audio.py",
  """        if self.clipped:
            return "clipping\"""",
  """        if False:
            return "clipping\""""),

 ("a very low level reads as fine", "ltcplay/audio.py",
  """        if self.hold < 0.05:
            return "very low\"""",
  """        if False:
            return "very low\""""),

 ("find only ever listens to input 1", "ltcplay/audio.py",
  'chans = min(d["channels"], 8)     # past 8 this stops being useful',
  "chans = 1"),

 ("the show file stops validating its input block", "ltcplay/timeline.py",
  """        for k in inp:
            if k not in ("device", "channel", "rate"):""",
  """        for k in ():
            if k not in ("device", "channel", "rate"):"""),

 ("the display stops warning about a dead interface", "ltcplay/display.py",
  "if attached and quiet is not None and quiet > 1.0:", "if False:"),

 ("the display stops warning about a silent input", "ltcplay/display.py",
  "elif a.blocks > 40 and a.level.hold < 0.02:", "elif False:"),

 ("the installer launcher collides with the package directory",
  "Install ltcplay.command", "LAUNCHER=ltc", "LAUNCHER=ltcplay"),

 ("an unrecognised interface is treated as software and demoted",
  "ltcplay/audio.py",
  """    return "hardware\"""",
  """    return "virtual\""""),

 ("software devices are no longer told apart from real ones",
  "ltcplay/audio.py",
  "    for pat in _VIRTUAL:", "    for pat in ():"),

 ("the real interface is no longer scanned first", "ltcplay/audio.py",
  'order = {"hardware": 0, "jack": 1, "built in": 2, "phone": 3, "virtual": 4}',
  'order = {"hardware": 9, "jack": 1, "built in": 2, "phone": 3, "virtual": 4}'),

 ("the scan is no longer narrowed to plausible inputs", "ltcplay/audio.py",
  'return [d for d in hardware_first(inputs) if d["kind"] in CANDIDATE_KINDS]',
  "return hardware_first(inputs)"),

 ("the headphone jack is not recognised", "ltcplay/audio.py",
  """    for pat in _JACK:
        if pat in n:
            return "jack\"""",
  """    for pat in ():
        if pat in n:
            return "jack\""""),

 ("the built-in mic is dropped from the scan", "ltcplay/audio.py",
  'CANDIDATE_KINDS = ("hardware", "jack", "built in")',
  'CANDIDATE_KINDS = ("hardware", "jack")'),

 ("the show file beats the saved input", "ltcplay/settings.py",
  """    sources = (("the command line", cli),
               ("your saved input", saved or {}),
               ("the show file", timeline_input or {}))""",
  """    sources = (("the command line", cli),
               ("the show file", timeline_input or {}),
               ("your saved input", saved or {}))"""),

 ("a channel is inherited across a change of device", "ltcplay/settings.py",
  """        if d.get("device"):
            for k in FIELDS:""",
  """        if True:
            for k in FIELDS:"""),

 ("a disagreement about the input is not reported", "ltcplay/settings.py",
  """    if tl_dev and sv_dev and tl_dev.lower() != sv_dev.lower() \\
            and not cli.get("device"):""",
  """    if False:"""),

 ("a damaged settings file is trusted", "ltcplay/settings.py",
  """    if not isinstance(doc, dict):
        return {}""",
  """    if not isinstance(doc, dict):
        return doc"""),

 ("a stale show folder path is not healed", "ltcplay/timeline.py",
  "            if os.path.isdir(cand):", "            if False:"),

 ("a substituted show folder is substituted silently", "ltcplay/timeline.py",
  "                note = _substitution_note(stored, cand)",
  "                note = None"),

 ("the display reads the clock and the cues at different moments",
  "ltcplay/session.py",
  "        nxt = tl.next_cue(tc) if (tc is not None and tc >= 0) else p.next_cue",
  "        nxt = p.next_cue"),

 ("two shows can run at once on the same universes", "ltcplay/web.py",
  "            if self.session is not None and self.session.running:",
  "            if False:"),

 ("validating sends to the lighting network", "ltcplay/web.py",
  "        kw.update(no_output=True, no_log=True, sd=self._sd)",
  "        kw.update(no_output=False, no_log=True, sd=self._sd)"),

 # -- the iPad remote (2026-10-03; no sign-in since Jeff, 2026-10-07): the
 #    show network, the controls, stale state, no arm route, scrubbing and
 #    the seek guard --
 # -- the show network from another machine (Jeff, 2026-10-28: the page
 #    never opened from his Mac): the RFC 1918 picker, the card's view, the
 #    firewall rule read, the wrong-name page, the fallback to this PC
 #    only, the start line, the supervisor's flag and the installer's rule --
 ("show network: the picker offers any address", "ltcplay/remote.py",
  "        if a not in seen and is_rfc1918(a):",
  "        if a not in seen:"),

 ("show network: a missing firewall rule reads present", "ltcplay/remote.py",
  "    if p.returncode != 0:\n        return (\"missing\",",
  "    if False:\n        return (\"missing\","),

 ("show network: the card gives the address before the engine listens on it",
  "ltcplay/remote.py",
  "        url = page_url(addr, port) if on_saved else None",
  "        url = page_url(addr, port)"),

 ("show network: an API route by another name gets the page, not the refusal",
  "ltcplay/web.py",
  "        if why == HOST_REFUSAL and not route.startswith(\"/api/\"):",
  "        if why == HOST_REFUSAL:"),

 ("show network: a saved address this PC lacks stops the engine",
  "ltcplay/web.py",
  "        if not (network_fallback and on_network):\n            raise",
  "        if True:\n            raise"),

 ("show network: the start line names loopback only", "ltcplay/web.py",
  "        addrs = [\"127.0.0.1\"] + ([httpd.bind_address] if on_network else [])",
  "        addrs = [\"127.0.0.1\"]"),

 ("show network: --network-if-saved ignores the saved address",
  "ltcplay/cli.py",
  "    args.bind = saved\n    return True",
  "    return True"),

 ("show network: the supervisor never asks for the saved address",
  "packaging/windows/supervisor.py",
  "              \"--no-browser\", \"--network-if-saved\"]",
  "              \"--no-browser\"]"),

 ("show network: a saved public, VPN or link-local address is served on",
  "ltcplay/cli.py",
  "    if not remote_mod.is_rfc1918(saved):",
  "    if False:"),

 ("show network: the fallback binds every interface", "ltcplay/web.py",
  "        bind, on_network = \"127.0.0.1\", False\n"
  "        httpd = ThreadingHTTPServer((bind, port), Handler)",
  "        bind, on_network = \"0.0.0.0\", True\n"
  "        httpd = ThreadingHTTPServer((bind, port), Handler)"),

 ("show network: the wrong-name page reflects the Host header",
  "ltcplay/web.py",
  "                getattr(self.server, \"bind_address\", \"127.0.0.1\"),\n"
  "                self.server.server_address[1], self._local()),",
  "                str(self.headers.get(\"Host\", \"\")).split(\":\")[0],\n"
  "                self.server.server_address[1], False),"),

 ("show network: the wrong-name page may be sniffed", "ltcplay/web.py",
  "                         \"X-Content-Type-Options\": \"nosniff\"})",
  "                         \"X-Content-Type-Options\": \"sniff\"})"),

 ("show network: a Public network is not warned about", "ltcplay/remote.py",
  "    if cat == \"Public\":",
  "    if False:"),

 ("show network: the card is silent about a port the rule does not open",
  "ltcplay/remote.py",
  "            if port != FIXED_PORT:\n                # The installer's rule",
  "            if False:\n                # The installer's rule"),

 ("show network: an address with a zone ID or other text is saved",
  "ltcplay/remote.py",
  "    if not PLAIN_IP.fullmatch(text) or getattr(ip, \"scope_id\", None):",
  "    if False:"),

 ("show network: any saved address is looked up in PowerShell",
  "ltcplay/remote.py",
  "    if not is_rfc1918(address):\n        return \"unknown\", \"\"",
  "    if False:\n        return \"unknown\", \"\""),

 ("show network: the address is pasted into the PowerShell command",
  "ltcplay/remote.py",
  "                 CATEGORY_COMMAND],",
  "                 CATEGORY_COMMAND.replace(\"$env:LTC_SHOW_ADDRESS\", "
  "f\"'{ip}'\")],"),

 ("show network: the address is not handed to PowerShell",
  "ltcplay/remote.py",
  "        env[CATEGORY_ENV] = ip",
  "        pass"),

 ("show network: a Windows program is found by its bare name",
  "ltcplay/remote.py",
  "    return path if (exists or os.path.isfile)(path) else None",
  "    return parts[-1]"),

 ("show network: netsh is started without its full path",
  "ltcplay/remote.py",
  "        p = run(f'\"{exe}\" advfirewall firewall show rule '",
  "        p = run(f'netsh advfirewall firewall show rule '"),

 ("show network: PowerShell is started without its full path",
  "ltcplay/remote.py",
  "        p = run([exe, \"-NoProfile\", \"-NonInteractive\", \"-Command\",",
  "        p = run([\"powershell\", \"-NoProfile\", \"-NonInteractive\", "
  "\"-Command\","),

 ("show network: a netsh missing from System32 is run anyway",
  "ltcplay/remote.py",
  "    if exe is None:\n        return (\"unknown\", \"The Windows Firewall rule",
  "    if False:\n        return (\"unknown\", \"The Windows Firewall rule"),

 ("show network: a PowerShell missing from System32 is run anyway",
  "ltcplay/remote.py",
  "    if exe is None:\n        return \"unknown\", (\"Which kind",
  "    if False:\n        return \"unknown\", (\"Which kind"),

 ("show network: the firewall rule is open to any address",
  "packaging/windows/installer.iss",
  "localport=7878 remoteip=localsubnet profile=any",
  "localport=7878 remoteip=any profile=any"),

 ("remote: the show network can be set from the network", "ltcplay/remote.py",
  "        if name in LOCAL_ONLY and not ctx.local:",
  "        if False:"),

 ("remote: the journal names whoever the body says", "ltcplay/remote.py",
  "        return ctx.device, ctx.screen, True",
  "        return body.get(\"who\") or ctx.device, ctx.screen, True"),

 ("remote: a name in the page's body skips the operator list",
  "ltcplay/remote.py",
  "                return who, screen or \"Rack screen\", False",
  "                return who, screen or \"Rack screen\", True"),

 ("scheduler: the operator list is asked of the page and not of others",
  "ltcplay/schedule.py",
  "            not ev.surface and \\\n",
  "            ev.surface and \\\n"),

 # -- fix round 1 of #39 (independent review) --
 ("remote: a network GET reaches the legacy routes", "ltcplay/web.py",
  "        if not self._local() and not network_may_reach(route):\n"
  "            return self._send(403, {\"error\": \"Not from the network. Only \"\n"
  "                                             \"the remote page's own routes \"\n"
  "                                             \"answer here.\"})\n"
  "        c = self.server.control\n        try:\n"
  "            if route in (\"/\", \"/index.html\") and \\",
  "        c = self.server.control\n        try:\n"
  "            if route in (\"/\", \"/index.html\") and \\"),

 ("remote: a network POST reaches the legacy routes", "ltcplay/web.py",
  "        if not self._local() and not network_may_reach(route):\n"
  "            return self._send(403, {\"error\": \"Not from the network. Only \"\n"
  "                                             \"the remote page's own routes \"\n"
  "                                             \"answer here.\"})\n"
  "        if route == \"/api/remote\" or route.startswith(\"/api/remote/\"):\n"
  "            remote = getattr(self.server, \"remote\", None)\n"
  "            if remote is None:\n"
  "                return self._send(404, {\"error\": \"no such thing here\"})\n"
  "            # Read the whole body",
  "        if route == \"/api/remote\" or route.startswith(\"/api/remote/\"):\n"
  "            remote = getattr(self.server, \"remote\", None)\n"
  "            if remote is None:\n"
  "                return self._send(404, {\"error\": \"no such thing here\"})\n"
  "            # Read the whole body"),

 ("remote: Origin null counts as this site", "ltcplay/web.py",
  "            origin = self.headers.get(\"Origin\")\n            if origin is not None:",
  "            origin = self.headers.get(\"Origin\")\n"
  "            if origin is not None and origin.strip().lower() != \"null\":"),

 ("remote: Sec-Fetch-Site is ignored", "ltcplay/web.py",
  "            if sfs is not None and sfs.strip().lower() not in (\"same-origin\",",
  "            if False and sfs.strip().lower() not in (\"same-origin\","),

 ("remote: a press need not be JSON", "ltcplay/web.py",
  "            if ctype.split(\";\")[0].strip().lower() != \"application/json\":",
  "            if False:"),

 ("web: odd spellings of every interface are served", "ltcplay/web.py",
  "    bind = normalize_bind(bind)", "    bind = bind"),

 ("remote: proxied requests are let in", "ltcplay/web.py",
  "        h = remote_mod.looks_proxied(self.headers)\n        if h:",
  "        h = remote_mod.looks_proxied(self.headers)\n        if False:"),

 ("remote: any Host name is served", "ltcplay/web.py",
  "        if not remote_mod.host_ok(self.headers.get(\"Host\"), self._local(),",
  "        if False and not remote_mod.host_ok(self.headers.get(\"Host\"), "
  "self._local(),"),

 ("remote: presses from another site's page are taken", "ltcplay/web.py",
  "                    return \"A press from another site's page was refused.\"",
  "                    pass"),

 ("remote: every interface can be served on", "ltcplay/web.py",
  "    if on_network and bind in WILDCARD:", "    if False:"),

 ("remote: a stale page can still press Start now and Resume",
  "ltcplay/remote.py",
  "        if age > FRESH_S or age < -FRESH_S:", "        if False:"),

 ("remote: a press with no status at all is taken", "ltcplay/remote.py",
  "            return (\"The page has not shown a status yet. Wait for it to \"",
  "            return None\n            return (\"The page has not shown a "
  "status yet. Wait for it to \""),

 ("remote: the page enables stale-state controls", "ltcplay/web/remote.html",
  "  const fresh = !stale && !!st;", "  const fresh = !!st;"),

 ("remote: the page never shows the stale banner", "ltcplay/web/remote.html",
  "  b.hidden = !stale || $(\"main\").hidden;", "  b.hidden = true;"),

 ("remote: the page calls a 2.5 s old status fresh", "ltcplay/web/remote.html",
  "  return (nowMs - lastOkMs) > freshS * 1000;",
  "  return (nowMs - lastOkMs) > freshS * 2000;"),

 # The width guard (Jeff, 2026-10-17): without it the Abort's answer
 # widened the page past the screen and the word ABORT slid off it.
 ("remote: the page's column track may grow past the screen again",
  "ltcplay/web/remote.html",
  "display:grid;grid-template-columns:minmax(0,1fr);\n",
  "display:grid;\n"),

 ("remote: #main no longer clips what is wider than the screen",
  "ltcplay/web/remote.html",
  "#main{min-height:0;min-width:0;position:relative;overflow:hidden}",
  "#main{min-height:0;position:relative}"),

 ("remote: a toast may widen the page again", "ltcplay/web/remote.html",
  ".toast{font-size:clamp(13px,1.6vh,16px);color:var(--cream);min-height:1.3em;min-width:0;\n  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
  ".toast{font-size:clamp(13px,1.6vh,16px);color:var(--cream);min-height:1.3em;\n  white-space:nowrap}"),

 ("remote: the MODE pill is drawn from the screen that is open, not the engine",
  "ltcplay/web/remote.html",
  "  if(show.running && tr.programming)\n",
  "  if(location.hash === \"#programming\")\n"),

 ("remote: the Abort confirm promises a disarm with no flame link",
  "ltcplay/web/remote.html",
  "  if(s.disarm_connected && fl.connected)\n",
  "  if(true)\n"),

 # An Abort with no show live never latches, so the first status poll after
 # it says aborted false; a banner that hides on that is gone in 250 ms and
 # only the one-line toast is left (found rendering bench-build after #68
 # and #70 merged).
 ("remote: the Abort banner goes on the first status poll after an Abort "
  "with no show to stop",
  "ltcplay/web/remote.html",
  "  if(sch.aborted === false && b.dataset.latched){ b.hidden = true;",
  "  if(sch.aborted === false && b.dataset.aborted){ b.hidden = true;"),

 ("remote: the getting-started steps stack at the 950 px window, the third "
  "below the card's own scroll",
  "ltcplay/web/remote.html",
  "#setup .steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px}",
  "#setup .steps{display:grid;grid-template-columns:1fr;gap:10px}"),

 ("remote: a stale flame lamp still reads armed", "ltcplay/remote.py",
  "            for g in groups:\n                g[\"armed\"] = \"unknown\"",
  "            pass"),

 ("remote: Start now and Abort need no confirm", "ltcplay/remote.py",
  "        if name in CONFIRM_ROUTES and body.get(\"confirmed\") is not True:",
  "        if False:"),

 ("remote: an arm route appears", "ltcplay/remote.py",
  "CONTROL_ROUTES = (\"start-now\", \"hold\", \"resume\", \"abort\", \"reset\",\n                  \"disarm-all\", \"stop\")",
  "CONTROL_ROUTES = (\"start-now\", \"hold\", \"resume\", \"abort\", \"reset\",\n                  \"disarm-all\", \"stop\", \"arm\")"),

 ("remote: half a request is acted on", "ltcplay/web.py",
  "            return None if \"/api/remote/\" in self.path else {}",
  "            return {}"),

 ("remote: disarm with no flame link says done", "ltcplay/remote.py",
  "            return 409, {\"ok\": False, \"error\": text}",
  "            return 200, {\"ok\": True, \"error\": text}"),

 ("remote: scrubbing is allowed during a live scheduled show",
  "ltcplay/remote.py",
  "            if m is not None and m.state in self.LIVE_STATES:",
  "            if False:"),

 ("remote: scrubbing is allowed on a show started in Show mode",
  "ltcplay/remote.py",
  "        if not rehearsal:\n            return False, (\"This show was",
  "        if False:\n            return False, (\"This show was"),

 ("scheduler: a remote press from someone off the list is taken",
  "ltcplay/schedule_service.py",
  "        if who.lower() not in names:\n            sentence = (f\"{who or "
  "'Nobody'!r} is not on the operator list \"",
  "        if False:\n            sentence = (f\"{who or "
  "'Nobody'!r} is not on the operator list \""),

 # -- arming from a screen (2026-10-03, its own PR) --
 ("arm: the screen_arming switch is ignored", "ltcplay/remote.py",
  "            return load_settings(self.folder)[\"screen_arming\"] is True",
  "            return True"),

 ("arm: a stale page can hold to arm", "ltcplay/remote.py",
  "        if seen is None or abs(self.wall() - seen / 1000.0) > ARM_FRESH_S:",
  "        if seen is None:"),

 ("arm: a stale flamesafe status can arm", "ltcplay/remote.py",
  "        if not fl or fl[\"stale\"] or fl[\"age_ms\"] is None or \\\n"
  "                fl[\"age_ms\"] > ARM_FRESH_S * 1000:",
  "        if not fl:"),

 ("arm: an armed group can be held again", "ltcplay/remote.py",
  "        if g[\"armed\"] == \"armed\" or g[\"wanted\"]:", "        if False:"),

 ("arm: a second browser can hold the same group", "ltcplay/remote.py",
  "            if h is not None and now - h[\"beat\"] <= BEAT_STALE_S:\n"
  "                theirs = h[\"who\"]",
  "            if False:\n                theirs = h[\"who\"]"),

 ("arm: a second tab on the same device restarts a live hold",
  "ltcplay/remote.py",
  "            if h is not None and now - h[\"beat\"] <= BEAT_STALE_S:\n"
  "                theirs = h[\"who\"]",
  "            if h is not None and h[\"token\"] != token and \\\n"
  "                    now - h[\"beat\"] <= BEAT_STALE_S:\n"
  "                theirs = h[\"who\"]"),

 ("countdown: a held show's time left keeps running", "ltcplay/remote.py",
  "                \"running\": not held}",
  "                \"running\": True}"),

 ("countdown: a show of unknown length is guessed", "ltcplay/remote.py",
  "                   and math.isfinite(x) for x in (dur, el)) or dur <= 0:\n"
  "            return None",
  "                   and math.isfinite(x) for x in (dur, el)) or dur <= 0:\n"
  "            return {\"kind\": \"left\", \"label\": \"Left in this show\", "
  "\"s\": 0.0, \"running\": True}"),

 ("countdown: during a show it counts to the next show", "ltcplay/remote.py",
  "        live = sched.get(\"state\") in (\"SHOW\", \"PAUSED\")",
  "        live = False"),

 ("countdown: the page ages a held show's time left",
  "ltcplay/web/remote.html",
  "  if(!c.running) return {kind:c.kind, label:c.label, running:false, s:s, at:null};",
  "  if(false) return {kind:c.kind, label:c.label, running:false, s:s, at:null};"),

 # -- Art-Net receivers, one address each (soak 2026-10-21) ----------
 ('artnet nodes: 127.0.0.2 is not the same address as 127.0.0.1',
  'ltcplay/artnodes.py',
  '    return LOOPBACK if is_loopback(addr) else addr',
  '    return addr'),

 ('artnet nodes: localhost is not the loopback',
  'ltcplay/artnodes.py',
  '    if s.lower() == "localhost":\n        return LOOPBACK',
  '    if False:\n        return LOOPBACK'),

 ('artnet nodes: two receivers on one address pass',
  'ltcplay/artnodes.py',
  '        if len(members) < 2:\n            continue',
  '        if len(members) < 3:\n            continue'),

 ('artnet nodes: the collision does not say what to do',
  'ltcplay/artnodes.py',
  'never chases timecode. {FIX}")',
  'never chases timecode.")'),

 ('artnet nodes: the duplicate refusal does not say what to do',
  'ltcplay/clock.py',
  'f"so that receiver would get every frame twice. "\n                    f"{artnodes.FIX}")',
  'f"so that receiver would get every frame twice")'),

 ('artnet nodes: a node not on the show PC is only a warning',
  'ltcplay/artnodes.py',
  '        level = "bad" if windows else "warn"',
  '        level = "warn"'),

 ('artnet nodes: adapters that cannot be read are a fault',
  'ltcplay/artnodes.py',
  '        if here is None:\n            out.append(("warn",',
  '        if False:\n            out.append(("warn",'),

 ('artnet nodes: adapters that cannot be read pass as fine',
  'ltcplay/artnodes.py',
  '        if here:\n            continue',
  '        if here or here is None:\n            continue'),

 ('artnet nodes: the adapter list is not asked first',
  'ltcplay/artnodes.py',
  '        if adapters is not None and addr in adapters:\n            continue',
  '        pass'),

 ('artnet nodes: the loopback is probed like an adapter',
  'ltcplay/artnodes.py',
  '        if is_loopback(addr):\n            continue',
  '        pass'),

 ('artnet nodes: the probe cannot tell a missing address',
  'ltcplay/artnodes.py',
  '                getattr(e, "winerror", None) == WSAEADDRNOTAVAIL:\n            return False',
  '                getattr(e, "winerror", None) == WSAEADDRNOTAVAIL:\n            return None'),

 ('artnet nodes: a file that is not a show is checked',
  'ltcplay/artnodes.py',
  '    if not isinstance(doc, dict) or "cues" not in doc:\n        return None\n    clock',
  '    if not isinstance(doc, dict):\n        return None\n    clock'),

 ('artnet nodes: the rack screen alert is not raised',
  'ltcplay/remote.py',
  '        self.artnet_alert()\n        out["artnet_note"] = self.artnet_note()',
  '        out["artnet_note"] = self.artnet_note()'),

 ('artnet nodes: a warning raises the rack screen alert',
  'ltcplay/remote.py',
  '            text = " ".join(t for lv, t in faults if lv == "bad")',
  '            text = " ".join(t for lv, t in faults)'),

 ('artnet nodes: the alert is raised again on every poll',
  'ltcplay/remote.py',
  '            if text and text != getattr(self, "_artnet_said", ""):',
  '            if text:'),

 ('artnet nodes: the Morning card is missing',
  'ltcplay/remote.py',
  '        out.append(self._artnet_check())\n',
  ''),

 ('artnet nodes: the Morning card is never red',
  'ltcplay/remote.py',
  '        level = "bad" if "bad" in levels else \\',
  '        level = "warn" if "bad" in levels else \\'),

 ('artnet nodes: show load says nothing',
  'ltcplay/session.py',
  '                self.notes.extend(t for lv, t in found if lv == "bad")',
  '                pass'),

 # -- soak 2026-10-21 --------------------------------------------------
 ("soak: the countdown restarts from every poll and its digit flickers",
  "ltcplay/web/remote.html",
  "    at = Math.min(prev.at, at);",
  "    at = at;"),

 ("soak: the countdown is sent in whole seconds again", "ltcplay/remote.py",
  "        for key in (\"in_s_exact\", \"in_s\"):",
  "        for key in (\"in_s\",):"),

 ("soak: the scheduler's next show is whole seconds only",
  "ltcplay/schedule.py",
  "                  \"in_s_exact\": (round((nxt.start - now).total_seconds(), 3)",
  "                  \"in_s_exact\": (int((nxt.start - now) // ONE_SECOND)"),

 ("soak: a render older than the layout is not found", "ltcplay/fire_ice.py",
  "        if made < laid:\n            out.append(p)",
  "        if made > laid:\n            out.append(p)"),

 ("soak: a stale render's CUES FAULT is never raised as an alert",
  "ltcplay/fire_ice.py",
  "            tell = self.on_render_fault",
  "            tell = None"),

 ("soak: the screens see only a refusal, never the show-load CUES FAULT",
  "ltcplay/fire_ice.py",
  "        return self.fault or self.render_fault_now()",
  "        return self.fault"),

 ("flame cues: a stale render is not refused while the reader is mid-pass",
  "ltcplay/fire_ice.py",
  "            if stale_s is s and any(",
  "            if False and any("),

 ("flame cues: the reader never hands the stale renders to the refusal",
  "ltcplay/fire_ice.py",
  "        self._stale = (s, frozenset(stale))",
  "        self._stale = (s, frozenset())"),

 ("soak: the reading thread never checks the renders' age",
  "ltcplay/fire_ice.py",
  "            try:\n                self._check_renders_once()\n"
  "            except Exception:\n                pass\n",
  ""),

 ("soak: the Stream Deck's CUES FAULT is only the refusal in force",
  "ltcplay/web.py",
  "                (now() if now is not None else cues.fault) or \"\")}",
  "                cues.fault or \"\")}"),

 ("soak: the rack screen's flames alert is never raised",
  "ltcplay/remote.py",
  "            self.alerts.raise_(\"flames\", text)",
  "            pass"),

 ("soak: ltc serve never hands the flame cues the alert",
  "ltcplay/web.py",
  "            httpd.remote.flame_cues.on_render_fault = \\\n"
  "                httpd.remote.render_fault_alert",
  "            pass"),

 ("soak: the Morning checks call stale renders fine",
  "ltcplay/remote.py",
  "                        \"level\": \"bad\" if faults else \"ok\",",
  "                        \"level\": \"ok\","),

 ("soak: the Morning page gets no engine checks", "ltcplay/remote.py",
  "            v[\"checks\"] = self.engine_checks()\n",
  ""),

 ("soak: an in-memory render is copied whole under the lock again",
  "ltcplay/fseq.py",
  "            fileobj.seek(0, 2)\n            self._file_size = fileobj.tell()",
  "            self._file_size = len(fileobj.getbuffer())"),

 ("soak: the flame cues' reading starts only at the first flame frame",
  "ltcplay/fire_ice.py",
  "            if isinstance(getattr(built_link, \"cues\", None), FlameCues):\n"
  "                built_link.cues.start()",
  "            pass"),

 ("soak: FlameCues.start() starts nothing until a frame asks",
  "ltcplay/fire_ice.py",
  "        if self._bg and self._thread is None and not self._stop.is_set():\n"
  "            self._thread = threading.Thread(\n"
  "                target=self._refresher, daemon=True,",
  "        if self._bg and self._thread is None and not self._stop.is_set() \\\n"
  "                and getattr(self, '_layout', None) is not None:\n"
  "            self._thread = threading.Thread(\n"
  "                target=self._refresher, daemon=True,"),

 ("soak: another show folder in showpc.json goes unsaid",
  "ltcplay/remote.py",
  "    if served and _norm_folder(served) == _norm_folder(wanted):",
  "    if True:"),

 ("soak: the status does not carry the show folder problem",
  "ltcplay/remote.py",
  "                              \"problem\": self.show_folder_problem()}",
  "                              \"problem\": \"\"}"),

 ("soak: the show folder problem is journaled on every poll",
  "ltcplay/remote.py",
  "        if text and text != getattr(self, \"_folder_said\", \"\"):",
  "        if text:"),

 ("soak: the supervisor never sees showpc.json change",
  "packaging/windows/supervisor.py",
  "        if st == self._stamp:\n            return None",
  "        if True:\n            return None"),

 ("soak: a new show folder is no reason to restart the engine",
  "packaging/windows/supervisor.py",
  "    if not new.get(\"show_folder\") or a == b:",
  "    if True:"),

 ("soak: the supervisor never restarts the engine for a new show folder",
  "packaging/windows/supervisor.py",
  "                why = progs[\"engine\"].stop()\n"
  "                log(f\"{folder_change}: \"",
  "                why = \"\"\n"
  "                log(f\"{folder_change}: \""),

 ("soak: a stopped time service with the clock off is not loud",
  "ltcplay/remote.py",
  "    if service == \"stopped\":\n        if off:",
  "    if service == \"stopped\":\n        if False:"),

 ("soak: the clock is loud only past 2 s", "ltcplay/remote.py",
  "CLOCK_LOUD_S = 0.5", "CLOCK_LOUD_S = 2.0"),

 ("soak: the Morning checks read no time server offset",
  "ltcplay/remote.py",
  "            level, text = judge_pc_clock(cc.get(\"offset_s\"),",
  "            level, text = judge_pc_clock(cc.get(\"offset\"),"),

 ("soak: a stopped time service reads as running", "ltcplay/remote.py",
  "    return \"running\" if m.group(1) in (\"2\", \"4\") else \"stopped\"",
  "    return \"running\""),

 ("soak: the bench layout has one universe whatever is asked",
  "packaging/windows/soak.py",
  "        for u in range(1, universes + 1))",
  "        for u in range(1, 2))"),

 ("soak: the bench render ignores its planned size",
  "packaging/windows/soak.py",
  "                      channel_count=plan[\"channels\"],",
  "                      channel_count=1022,"),

 ("soak: the pixel limits stay at 40 frames a second",
  "packaging/windows/soak.py",
  "    scale = period / PIXEL_PERIOD_MS",
  "    scale = 1.0"),

 ("soak: the multi-day soak takes a render size it never plays",
  "packaging/windows/soak.py",
  "    if render and days:\n        return (\"--universes and --fps",
  "    if False:\n        return (\"--universes and --fps"),

 ("beyond blanking: the default is timecode again", "ltcplay/fire_ice.py",
  "BEYOND_BLANK_DEFAULT = \"osc\"",
  "BEYOND_BLANK_DEFAULT = \"timecode\""),

 ("arm: an interrupted hold carries on", "ltcplay/remote.py",
  "                        now - h[\"beat\"] <= BEAT_STALE_S and\n"
  "                        \"wanted_at\" not in h)\n"
  "                if live:",
  "                        True and\n"
  "                        \"wanted_at\" not in h)\n                if live:"),

 ("arm: the deck is told a quiet hold is still fresh", "ltcplay/remote.py",
  "                              \"fresh\": age <= BEAT_STALE_S,",
  "                              \"fresh\": True,"),

 ("arm: an Abort leaves screen holds running", "ltcplay/remote.py",
  "        if name in (\"abort\", \"disarm-all\"):\n            # No screen hold",
  "        if False:\n            # No screen hold"),

 ("arm: a group disarm leaves its hold running", "ltcplay/remote.py",
  "        self._drop_hold(i)\n        with self._arm_lock:\n"
  "            self._disarm_ids += 1",
  "        with self._arm_lock:\n            self._disarm_ids += 1"),

 ("arm: deck-input is served to the network", "ltcplay/remote.py",
  "            if not ctx.local:\n                return 403, {\"error\": "
  "\"Only on the show machine itself.\"}\n            return 200, "
  "self.deck_input(query)",
  "            return 200, self.deck_input(query)"),

 ("arm: the deck fires a screen hold without 1 s of heartbeats",
  "ltcplay/streamdeck.py",
  "                if self._vheld.get(i, 0.0) < SCREEN_HOLD_S:\n"
  "                    continue",
  "                if False:\n                    continue"),

 ("arm: the deck keeps a screen hold the engine let go",
  "ltcplay/streamdeck.py",
  "            if h is None or h.get(\"id\") != self._vholds[i]:\n"
  "                self._screen_release(i)",
  "            if False:\n                self._screen_release(i)"),

 ("arm: the deck trusts an old engine answer", "ltcplay/streamdeck.py",
  "        if last is None or self._clock() - last[1] > SCREEN_STALE_S:\n"
  "            return {}",
  "        if last is None:\n"
  "            return {}"),

 ("arm: the deck keeps the last answer when the engine is unreachable",
  "ltcplay/streamdeck.py",
  "                self._last = None          # unreachable: every hold let go",
  "                pass"),

 ("arm: the deck ignores screen arming switched off",
  "ltcplay/streamdeck.py",
  "        if ans.get(\"enabled\") is not True:\n            return {}",
  "        if False:\n            return {}"),

 ("arm: the deck takes a hold the engine says is not fresh",
  "ltcplay/streamdeck.py",
  "            if isinstance(h, dict) and h.get(\"fresh\") is True and \\",
  "            if isinstance(h, dict) and \\"),

 ("arm: a screen hold skips the refractory window", "ltcplay/streamdeck.py",
  "            left = self._in_rearm_refractory(i, now)\n            if left > 0:\n"
  "                self._log(f\"Stream Deck: {self.names[i]} arm hold on the \"",
  "            left = 0\n            if left > 0:\n"
  "                self._log(f\"Stream Deck: {self.names[i]} arm hold on the \""),

 ("arm: screen holds run while latched", "ltcplay/streamdeck.py",
  "        holds = {} if latched else self.screen.holds()",
  "        holds = self.screen.holds()"),

 ("arm: a screen disarm does nothing", "ltcplay/streamdeck.py",
  "            if self.arm.wanted[i]:\n                self._do_disarm(i, who=str(ev.get(\"who\") or \"\"),",
  "            if False:\n                self._do_disarm(i, who=str(ev.get(\"who\") or \"\"),"),

 ("arm: the page arms on a stale status", "ltcplay/web/remote.html",
  "  if(isStale(nowMs, lastOkMs, a.fresh_s || 1.0)) return",
  "  if(false) return"),

 ("arm: the page arms on an old flamesafe status", "ltcplay/web/remote.html",
  "fl.age_ms === undefined || fl.age_ms > 1000)",
  "fl.age_ms === undefined || fl.age_ms > 100000)"),

 ("arm: the page keeps holding when it goes stale", "ltcplay/web/remote.html",
  "  const hp = holdLapsed();\n  if(hp) armStop(hp);",
  "  const hp = holdLapsed();\n  if(false) armStop(hp);"),

 ("arm: closing the page does not let go", "ltcplay/web/remote.html",
  "window.addEventListener(\"pagehide\", () => armStop(\"the page closed\"));",
  ""),

 ("flame link: aim slots are held by the zero-first rule again",
  "ltcplay/flamelink.py",
  "        return set(range(UNIVERSE_SIZE)) - self._aim",
  "        return set(range(UNIVERSE_SIZE))"),

 ("flame link: no slot is held by the zero-first rule, aim or flame",
  "ltcplay/flamelink.py",
  "        return set(range(UNIVERSE_SIZE)) - self._aim",
  "        return set()"),

 ("flame link: an aim slot that is not a fire slot is freed",
  "ltcplay/flamelink.py",
  "        aim = [a for a in aim if a in fire]",
  "        aim = list(aim)"),

 ("flame link: aim: two aim slots in one fixture are taken",
  "ltcplay/flamelink.py",
  "        if k in block:\n            return f\"slots {block[k]}",
  "        if False:\n            return f\"slots {block[k]}"),

 ("flame link: aim: the aim slots need not be the same channel",
  "ltcplay/flamelink.py",
  "    if len({a - saf[k] for k, a in block.items()}) > 1:\n        return",
  "    if False:\n        return"),

 ("flame link: aim: a wave flamer may aim with any channel",
  "ltcplay/flamelink.py",
  "        if a != saf[k] - 5:\n            return",
  "        if False:\n            return"),

 ("flame link: aim: a group without a safety_profile may aim",
  "ltcplay/flamelink.py",
  "               if \"safety_profile\" in g else",
  "               if True else"),

 ("flame link: aim: an aimed fixture need not be one 6 channel block",
  "ltcplay/flamelink.py",
  "        if {f for f in fire if len(saf) == 1 or lo < f < saf[k]} != set(",
  "        if False and {f for f in fire if len(saf) == 1 or lo < f < saf[k]} != set("),

 ("flame link: aim: an aim slot after the last safety slot is taken",
  "ltcplay/flamelink.py",
  "        if k is None:\n            return f\"slot {a} is after",
  "        if False:\n            return f\"slot {a} is after"),

 ("live flame: aim slots wait to be seen at zero again",
  "ltcplay/flameprog.py",
  "                self._blocked = set(fmap.slots) - self._aim",
  "                self._blocked = set(fmap.slots)"),

 ("live flame: an ended stream holds its aim slots again",
  "ltcplay/flameprog.py",
  "        self._blocked.update(set(range(first, first + count)) - self._aim)",
  "        self._blocked.update(range(first, first + count))"),

 ("flame link: the seek guard is off by default", "ltcplay/flamelink.py",
  "                 tc_fps=TC_FPS_DEFAULT, seek_guard=True):",
  "                 tc_fps=TC_FPS_DEFAULT, seek_guard=False):"),

 ("flame link: a jump is not seen as a seek", "ltcplay/flamelink.py",
  "            elif abs(dtc - dt) > SEEK_JUMP_S:\n                "
  "self._seek(last[0], secs)",
  "            elif False:\n                self._seek(last[0], secs)"),

 ("flame link: a backwards locate is not a seek", "ltcplay/flamelink.py",
  "            if dtc < 0:\n                self._seek(last[0], secs)\n"
  "            elif dt > TC_STILL_S:",
  "            if False:\n                self._seek(last[0], secs)\n"
  "            elif dt > TC_STILL_S:"),

 ("flame link: no settle after a seek", "ltcplay/flamelink.py",
  "                and now - self._steady_since >= SEEK_SETTLE_S)",
  "                and now - self._steady_since >= 0)"),

 ("flame link: a jumped-over cue fires once settled", "ltcplay/flamelink.py",
  "        if blocked:\n            for i in blocked:\n                "
  "vals[i] = 0",
  "        if False:\n            for i in blocked:\n                "
  "vals[i] = 0"),

 ("flame link: a resume needs no settle", "ltcplay/flamelink.py",
  "                else:\n                    self._steady_since = now",
  "                else:\n                    self._steady_since = now - 1.0"),

 ("player: a free-run loop never wraps", "ltcplay/player.py",
  "                if loop is not None and self.tc_seconds >= loop[1]:",
  "                if False:"),

 ("player: a paused free run keeps moving", "ltcplay/player.py",
  "            if paused is not None:\n                self.tc_seconds = paused",
  "            if False:\n                self.tc_seconds = paused"),

 ("flamesafe: the status mirror gets nothing", "flamesafe/service.py",
  "                self._status_tx.sendto(pkt, (self.cfg.link_status_ip, mirror))",
  "                pass"),

 ("flamesafe: a mirror on a link port is accepted", "flamesafe/config.py",
  "            if c.link_status_ip == other_ip and \\\n"
  "                    c.link_status_mirror_port == other_port:",
  "            if False:"),

 ("a wrong device name reads as a program fault", "ltcplay/web.py",
  "USER_ERRORS = (SessionError, audio_mod.DeviceError, ValueError,\n"
  "               FileNotFoundError)",
  "USER_ERRORS = (SessionError,)"),

 ("the timecode lookup stops naming the file it read", "ltcplay/cli.py",
  '    print(f"    path    {path}")', "    pass"),

 ("the timecode lookup answers with the next cue", "ltcplay/cli.py",
  """    cue = tl.cue_at(t)
    nxt = tl.next_cue(t)""",
  """    cue = tl.next_cue(t)
    nxt = tl.next_cue(t)"""),

 ("the media file inside a sequence is never read", "ltcplay/fseq.py",
  '        return self.variables.get("mf") or None', "        return None"),

 ("a sequence rendered from the wrong audio passes verify", "ltcplay/cli.py",
  "            if a != b and a not in b and b not in a:",
  "            if False:"),

 ("verify stops noticing a file that changed", "ltcplay/cli.py",
  '            if was.get("hash") != digest or was.get("size") != size:',
  "            if False:"),

 ("verify stops recording fingerprints at all", "ltcplay/cli.py",
  '    if not args.no_manifest:\n        # A read-only show folder',
  '    if False:\n        # A read-only show folder'),

 ("a missing sequence is passed over in silence", "ltcplay/cli.py",
  """        if not os.path.exists(path):
            row["verdict"].append("FILE MISSING")""",
  """        if False:
            row["verdict"].append("FILE MISSING")"""),

 ("an old render against a different model layout passes verify",
  "ltcplay/cli.py",
  "        if majority and mine and mine != maj_starts:", "        if False:"),

 ('the layout report stops saying which props go dark',
  'ltcplay/cli.py',
  '                detail.append(f"    does not contain ch {s0+1}..{s0+maj_starts[s0]}"\n                              f"{prop(s0 + 1)} -- it stays dark")',
  '                pass  # noqa'),

 ("a track number makes every numbered render look mislabelled",
  "ltcplay/cli.py",
  "            a, b = _norm_stem(mstem), _norm_stem(stem)",
  "            a, b = _norm(mstem), _norm(stem)"),

 ("the set number stops counting as part of the identity", "ltcplay/cli.py",
  '    parts = [p for p in _re.split(r"[_\\-]", s or "") if not p.strip().isdigit()]',
  '    parts = [_re.sub(r"\\d+", "", p) for p in _re.split(r"[_\\-]", s or "")]'),

 ("the shared-file note lists the cue's own timecode back at it",
  "ltcplay/cli.py",
  '                  + ", ".join(t for t in shared if t != r["tc"])',
  '                  + ", ".join(t for t in shared)'),

 ("verify stops saying a file is used twice", "ltcplay/cli.py",
  "        if len(shared) > 1:", "        if False:"),

 ("the one-frame bridge between cues is removed", "ltcplay/player.py",
  "            if nxt is not None and self.bridge_s > 0 and \\",
  "            if False and nxt is not None and self.bridge_s > 0 and \\"),

 ("the bridge holds the first frame instead of the last", "ltcplay/player.py",
  "                self.current_frame = cue.fseq.frame_count - 1\n"
  "                out = self._render(cue, self.current_frame)\n"
  "                if out is not None:",
  "                self.current_frame = 0\n"
  "                out = self._render(cue, self.current_frame)\n"
  "                if out is not None:"),

 ("the bridge swallows a real gap as well as a rounding one",
  "ltcplay/player.py",
  "                    0 < nxt.tc_seconds - tc <= self.bridge_s and \\",
  "                    0 < nxt.tc_seconds - tc and \\"),

 ("bridge_ms is ignored and always takes the default", "ltcplay/player.py",
  "        if bridge_ms is None:\n"
  "            bridge_ms = getattr(timeline, \"bridge_ms\", None)",
  "        bridge_ms = None"),

 ("a misspelled setting loads silently again", "ltcplay/timeline.py",
  "        unknown = [k for k in doc if k not in cls.KEYS]",
  "        unknown = []"),

 ("idle_fseq stops being accepted as a spelling of idle",
  "ltcplay/timeline.py",
  '        idle = doc.get("idle") or doc.get("preshow") or doc.get("idle_fseq")',
  '        idle = doc.get("idle") or doc.get("preshow")'),

 ("bridge_ms accepts a nonsense value", "ltcplay/timeline.py",
  "            if not isinstance(bridge_ms, (int, float)) or bridge_ms < 0:",
  "            if False:"),

 ("a cue that will not open is only a warning again", "ltcplay/session.py",
  "        if dead and not self.allow_missing:", "        if False:"),

 ("--allow-missing stops being honoured", "ltcplay/session.py",
  "        if dead and not self.allow_missing:", "        if dead:"),

 ("the preshow override is ignored", "ltcplay/player.py",
  '        if override in ("preshow", "blackout"):',
  '        if False and override in ("preshow", "blackout"):'),

 ("a held preshow stops reading the feed", "ltcplay/player.py",
  "        if override in (\"preshow\", \"blackout\"):\n"
  "            self._state_from_feed(now, last, epoch)",
  "        if override in (\"preshow\", \"blackout\"):\n"
  "            pass  # noqa"),

 ("the sequence position is shown as timecode seconds",
  "ltcplay/session.py",
  '                           "seq": format_seq(el),',
  '                           "seq": format_seq(tc),'),

 ("the sequence position loses its milliseconds", "ltcplay/tc.py",
  '    out = f"{m}:{s:06.3f}" if ms else f"{m}:{int(s):02d}"',
  '    out = f"{m}:{int(s):02d}"'),

 ("the sequence position rolls over at an hour", "ltcplay/tc.py",
  "    m = int(seconds // 60)", "    m = int(seconds // 60) % 60"),

 ("a failed reload half-swaps the show anyway", "ltcplay/player.py",
  "        if errors:", "        if False:"),

 ("reload keeps the old reader instead of the new one", "ltcplay/player.py",
  "        self.timeline.cues = fresh", "        pass  # noqa"),

 ("a re-rendered file stops reading as stale", "ltcplay/player.py",
  "            if was is not None and now is not None and now != was:",
  "            if False:"),

 ("auto reload swaps a file in while it is still being written",
  "ltcplay/player.py",
  "            if now - seen_at >= self.RELOAD_SETTLE_S:",
  "            if True:"),

 ("the settle time drops below the check interval", "ltcplay/player.py",
  "    RELOAD_SETTLE_S = 3.0", "    RELOAD_SETTLE_S = 0.5"),

 ("auto reload stops rate limiting itself", "ltcplay/player.py",
  "        if now - self._last_reload_check < self.RELOAD_CHECK_S:\n"
  "            return None", "        if False:\n            return None"),

 ("the page and the server drift apart unnoticed", "ltcplay/web.py",
  "API = 7", "API = 8"),

 ("the state stops carrying the API number", "ltcplay/web.py",
  '        snap["api"] = API', '        snap["api"] = None'),

 ("opening a render stops proving it can be read", "ltcplay/player.py",
  "            f.verify()", "            pass  # noqa"),

 ("verify stops checking the block table against the file size",
  "ltcplay/fseq.py",
  "            if off + length > size:", "            if False:"),

 ("verify only looks at the first frame", "ltcplay/fseq.py",
  "        for n in {0, self.frame_count // 2, self.frame_count - 1}:",
  "        for n in {0}:"),

 ("the reload mode only takes effect at the next start", "ltcplay/web.py",
  "            s.player.auto_reload = on", "            pass  # noqa"),

 ("the reload mode is not remembered between runs", "ltcplay/web.py",
  '        settings_mod.save_pref("auto_reload", on)', "        pass  # noqa"),

 ("the page is not told which reload mode it is in", "ltcplay/session.py",
  '            "auto_reload": p.auto_reload,',
  '            "auto_reload": False,'),

 ("the stale-server banner goes back inside the hiding panel",
  "ltcplay/web/index.html",
  '<div class="warn" id="apiwarn" hidden style="margin:0 0 14px"></div>',
  ''),

 ("the web launcher kills a live show to take the port",
  "Web ltcplay.command",
  '    read -r -p "Press return to close. " _\n    exit 1',
  '    echo "(carrying on)"'),

 ("the web launcher stops looking for a stale server",
  "Web ltcplay.command",
  'OLD=$(pgrep -f "ltcplay.cli serve|LTC Player.app/Contents/Resources/boot.py" 2>/dev/null || true)',
  'OLD=""'),

 ("a cue stops owning the channels it does not carry", "ltcplay/player.py",
  "        for a, b in gaps:", "        for a, b in []:"),

 ("the gap map forgets the tail of the rig", "ltcplay/player.py",
  "        if at < cap:\n            gaps.append((at, cap))",
  "        if False:\n            gaps.append((at, cap))"),

 ("a single bad timecode frame is believed again", "ltcplay/player.py",
  "                if cold or (want is not None\n"
  "                            and abs(new_epoch - want) <= self.jump_confirm_s):",
  "                if True:"),

 ("the bridge resurrects a cue that ended long ago", "ltcplay/player.py",
  "                    tc - ended_at <= self.bridge_s:",
  "                    True:"),

 ("a read hiccup sticks on HOLD for the rest of the set",
  "ltcplay/player.py",
  "        if self.source == HOLD:\n            self.source = SHOW",
  "        pass  # noqa"),

 ("the overrun warning never clears again", "ltcplay/player.py",
  "        self.out_of_range_channels = dropped",
  "        self.out_of_range_channels = self.out_of_range_channels or dropped"),

 ("a failed audio start leaves the engine driving the rig",
  "ltcplay/session.py",
  "                try:\n                    self.stop()\n"
  "                except Exception:\n                    pass\n"
  "                raise",
  "                raise"),

 ("the output lock can be garbage collected away", "ltcplay/onlyone.py",
  "        _HELD.add(self)", "        pass  # noqa"),

 ("a second process is allowed onto the rig", "ltcplay/session.py",
  "            except onlyone.AlreadyRunning as e:",
  "            except ZeroDivisionError as e:"),

 ("stop stops claiming the blackout went out", "ltcplay/session.py",
  "                self.blackout_sent = (\n"
  "                    getattr(self.sender, \"packets_sent\", 0) > before)",
  "                self.blackout_sent = True"),

 ("a feed coming back yanks a free run sideways", "ltcplay/player.py",
  "        if self.freerun_epoch is not None and override not in",
  "        if False and override not in"),

 ("release does not hand the show back", "ltcplay/player.py",
  "        self.freerun_epoch = None\n"
  "        self.freerun_paused_at = None\n"
  "        self.loop = None\n"
  "        live = self.feed_state == LOCKED",
  "        live = self.feed_state == LOCKED"),

 ("the bundle points back at the machine that made it", "ltcplay/cli.py",
  '    doc["show_dir"] = "show"', '    pass  # noqa'),

 ("the bundle ships without checking anything is missing", "ltcplay/cli.py",
  "        if not args.force:\n"
  "            return _err(f\"{len(missing)} file(s) the show names are not there, \"",
  "        if False:\n"
  "            return _err(f\"{len(missing)} file(s) the show names are not there, \""),

 ("a relative show folder resolves against the working directory",
  "ltcplay/timeline.py",
  "    if not os.path.isabs(stored):", "    if False:"),

 ("the credit loses the phone number", "ltcplay/brand.py",
  '    if b.get("phone"):\n        bits.append(b["phone"])',
  '    if False:\n        bits.append(b["phone"])'),

 ("free run swallows the panic button again", "ltcplay/player.py",
  '        if self.freerun_epoch is not None and override not in ("preshow",\n'
  '                                                               "blackout"):',
  "        if self.freerun_epoch is not None:"),

 ("a cold lock is second-guessed again", "ltcplay/player.py",
  "                if cold or (want is not None",
  "                if False or (want is not None"),

 ("the first frame of an honest relocate is called a bad feed",
  "ltcplay/player.py",
  "                    if want is not None:\n"
  "                        self.jump_rejects += 1",
  "                    if True:\n                        self.jump_rejects += 1"),

 ("a wedged start is invisible to the page again", "ltcplay/web.py",
  "        with self.lock:\n            self.session = s\n        try:\n            s.start()",
  "        try:\n            s.start()"),

 ("bundle keeps the absolute paths of the machine that made it",
  "ltcplay/cli.py",
  '            c["fseq"] = os.path.basename(c["fseq"])',
  '            pass  # noqa'),

 # NOT MUTATED: disabling the "bundling into your own install" guard makes
 # the suite delete this source tree, twice over, since the runner operates on
 # the live files. The guard is asserted statically in the bundle test instead.

 ("verify pins every render in memory again", "ltcplay/fseq.py",
  "        self._cache_idx = -1\n        self._cache = b\"\"\n        return True",
  "        return True"),

 ("up next resets to the top of the show when the feed stops",
  "ltcplay/player.py",
  "        seen = self.last_ltc_seconds\n        if seen is None:\n"
  "            return cues[0]\n        return self.timeline.next_cue(seen)",
  "        return cues[0]"),

 ("the free run logs a state change every frame", "ltcplay/player.py",
  "        was = self._last_noted_state or prev_state",
  "        was = prev_state"),

 ("the readout reports the show's state, not the feed's",
  "ltcplay/player.py",
  "            self.feed_state = self.state\n            self.state = FREERUN",
  "            self.state = FREERUN"),

 ("verify stops checking the bundle hashes", "ltcplay/cli.py",
  "    problems.extend(bundle_bad)", "    pass  # noqa"),

 ("bundle deletes whatever it finds called ltcplay", "ltcplay/cli.py",
  '        if not os.path.exists(os.path.join(pkg, "player.py")):',
  "        if False:"),

 ("a wedged start keeps refusing every later start", "ltcplay/web.py",
  "            self._starting = False\n            self._start_gen += 1\n"
  "        if s is not None:\n            s.stop()",
  "        if s is not None:\n            s.stop()"),

 ("a read-only folder throws a stack trace again", "ltcplay/cli.py",
  "    except OSError as e:\n"
  "        # A stack trace at a console at 8pm helps nobody.",
  "    except ZeroDivisionError as e:\n"
  "        # A stack trace at a console at 8pm helps nobody."),

 ('verify dies instead of reporting when it cannot write',
  'ltcplay/cli.py',
  '        except OSError as e:\n            print(f"\\n  Could not record the fingerprints',
  '        except ZeroDivisionError as e:\n            print(f"\\n  Could not record the fingerprints'),

 ("the chase engine's clock regresses to the coarse one on Windows",
  "ltcplay/player.py",
  'def _now():\n    """The chase engine\'s one clock. A function, not a bare alias, so\n'
  "    selftest._Stepped can fake it by swapping this module's own `time`\n"
  '    reference -- see the class docstring there."""\n'
  "    return time.perf_counter()",
  'def _now():\n    """The chase engine\'s one clock. A function, not a bare alias, so\n'
  "    selftest._Stepped can fake it by swapping this module's own `time`\n"
  '    reference -- see the class docstring there."""\n'
  "    return time.monotonic()"),

 ("the pixel output thread's pacing accumulates sleep error", "ltcplay/player.py",
  "            due = t0 + n_next * period\n"
  "            now = _now()\n"
  "            if now < due:\n"
  "                time.sleep(min(due - now, 0.05))\n"
  "                continue",
  "            now = _now()\n"
  "            time.sleep(period)"),

 ("the run loop's heartbeat reads the other clock", "ltcplay/cli.py",
  "        # started, and so last_beat, is on sess.started_at's clock:\n"
  "        # player._now() (perf_counter). Reading time.monotonic() here would\n"
  "        # compare it against a clock with an unrelated epoch -- fine on a\n"
  "        # Mac, where the two happen to agree, and nonsense on Windows.\n"
  "        now = _now()",
  "        now = time.monotonic()"),

 ("skipping a free run does nothing", "ltcplay/player.py",
  "        at = max(0.0, here + float(seconds))\n"
  "        self.freerun_epoch = _now() - at",
  "        at = max(0.0, here + float(seconds))\n"
  "        pass  # noqa"),

 ("skipping back runs off the front of the show", "ltcplay/player.py",
  "        at = max(0.0, here + float(seconds))",
  "        at = here + float(seconds)"),

 ("skipping is allowed while following timecode", "ltcplay/player.py",
  "        if self.freerun_epoch is None:\n"
  "            raise ValueError(\"The show is following timecode, so this Mac \"\n"
  "                             \"cannot move it. Skipping only applies to a free \"\n"
  "                             \"run: press GO first.\")\n"
  "        here = (self.freerun_paused_at",
  "        here = (self.freerun_paused_at"),

 ("restart always restarts the cue you just entered", "ltcplay/player.py",
  "            elif at - cues[here].tc_seconds < 1.5 and here > 0:",
  "            elif False:"),

 ("the rate mismatch warning is silenced", "ltcplay/display.py",
  "elif rate is not None and abs(rate - tl.fps) > 0.01:", "elif False:"),

 ("the drop frame mismatch warning is silenced", "ltcplay/display.py",
  "if rate is not None and drop != tl.drop:", "if False:"),
 # -- Advatek SHOWTime scene triggers, the ALTERNATE playback mode --------
 # Every one of these leaves a program that starts, runs and looks right.
 ("armed mode still sends live pixels to the Advateks", "ltcplay/output.py",
  '            if p["addr"][0] in muted_ips:\n                muted += 1\n                continue',
  '            if False:\n                muted += 1\n                continue'),

 ("a cue fires its scene on every frame instead of once", "ltcplay/player.py",
  "            if key == self._fired_key:\n                return",
  "            if False:\n                return"),

 ("the preshow scene is never handed to the boxes", "ltcplay/player.py",
  "        if self.source == IDLE:\n            return self.IDLE_KEY",
  "        if False:\n            return self.IDLE_KEY"),

 ("a trigger failure takes the whole show down", "ltcplay/player.py",
  "        except Exception as e:\n            self.last_error = f\"trigger: {e}\"",
  "        except ZeroDivisionError as e:\n            self.last_error = f\"trigger: {e}\""),

 ("the fire packet lights the wrong scene", "ltcplay/trigger.py",
  "            buf[payload_at + channel - 1] = 255",
  "            buf[payload_at + channel] = 255"),

 ("firing blocks the playback thread again", "ltcplay/trigger.py",
  "            self._q.put_nowait((channel, label))\n            return True",
  "            self.fire(channel, label)\n            return True"),

 # Was: "two scenes may share a trigger channel". Removed 2026-09-15 when
 # sharing became a deliberate, supported thing: an opener that plays at the
 # top of both sets is one recorded scene. What replaced that refusal is
 # "two different songs may share one recorded scene", which checks the
 # RENDERS rather than the numbers.

 ("a config that mutes nothing is accepted", "ltcplay/trigger.py",
  "        if not mute:\n            raise TriggerError(",
  "        if False:\n            raise TriggerError("),

 ("an unmapped cue is not reported at load", "ltcplay/trigger.py",
  "            if self.channel_for(key) is None:\n                bad.append(",
  "            if False:\n                bad.append("),

 ("the panel stops saying which mode is running", "ltcplay/display.py",
  "    if getattr(p, \"trigger_armed\", False) and trig is not None:",
  "    if False and getattr(p, \"trigger_armed\", False) and trig is not None:"),

 ("stop leaves the Advateks muted through the blackout", "ltcplay/session.py",
  "                if self.sender is not None and hasattr(self.sender, \"set_muted\"):\n                    self.sender.set_muted(())",
  "                if False:\n                    self.sender.set_muted(())"),

 # -- LTC Player.app ------------------------------------------------------
 ("the app is written to after it is signed", "Build LTC Player app.command",
  'step "signed"',
  'printf x > "$B/Contents/Resources/late"\nstep "signed"'),

 ("the builder never actually launches the app", "Build LTC Player app.command",
  'open -a "$HERE/$APP" --args --selfcheck',
  'true -a "$HERE/$APP" --args --selfcheck'),

 ("a bundle macOS refuses is left in the folder", "Build LTC Player app.command",
  '  rm -rf "$HERE/$APP"\n  bye "The app was built and signed, but macOS would not run it:',
  '  : rm -rf "$HERE/$APP"\n  bye "The app was built and signed, but macOS would not run it:'),

 ("the app stops asking for the microphone", "Build LTC Player app.command",
  "  <key>NSMicrophoneUsageDescription</key>",
  "  <key>NSMicrophoneUsageDescriptionX</key>"),

 ("LaunchServices arguments crash the app", "Build LTC Player app.command",
  '        if (strncmp(argv[i], "-psn_", 5) != 0) out[n++] = argv[i];',
  '        out[n++] = argv[i];'),

 ("the app runs from the wrong folder", "Build LTC Player app.command",
  '    if (chdir(folder) != 0)',
  '    if (chdir("/") != 0)'),

 ("the app serves the rig to the whole venue network",
  "Build LTC Player app.command",
  '    args = ["serve", "--port", PORT, "--bind", "127.0.0.1", "--no-browser"]',
  '    args = ["serve", "--port", PORT, "--bind", "0.0.0.0", "--no-browser"]'),

 ("the self-check writes inside the signed bundle",
  "Build LTC Player app.command",
  '    with open(os.path.join(folder, ".ltcplay_appcheck"), "w") as fh:',
  '    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ltcplay_appcheck"), "w") as fh:'),

 ("two different songs may share one recorded scene", "ltcplay/trigger.py",
  "            for other in cues[1:]:\n                why = _same_render(first.path, other.path)",
  "            for other in cues[1:]:\n                why = \"\"  # _same_render(first.path, other.path)"),

 ("a length difference between shared renders is ignored",
  "ltcplay/trigger.py",
  "        if a.duration_ms != b.duration_ms:",
  "        if False and a.duration_ms != b.duration_ms:"),

 ("the ends of a shared render are never compared", "ltcplay/trigger.py",
  "        idx = sorted({0, n - 1} |\n                     {int(i * (n - 1) / max(1, samples - 1))\n                      for i in range(samples)})",
  "        idx = sorted({int(i * (n - 1) / max(1, samples - 1))\n                      for i in range(1, samples - 1)})"),

 # -- sACN triggers, 2026-09-15 --------------------------------------------
 ("the trigger ignores the protocol and always sends Art-Net",
  "ltcplay/trigger.py",
  '        if self.cfg.protocol == "sacn":\n            head = _e131_header(self.cfg.universe, CHANNELS_PER_UNIVERSE)\n            seq_at, payload_at = 111, E131_HEADER_LEN',
  '        if False:\n            head = _e131_header(self.cfg.universe, CHANNELS_PER_UNIVERSE)\n            seq_at, payload_at = 111, E131_HEADER_LEN'),

 ("sACN triggers go to the Art-Net port", "ltcplay/trigger.py",
  '        return E131_PORT if self.protocol == "sacn" else ARTNET_PORT',
  '        return ARTNET_PORT'),

 ("only the first controller gets the trigger", "ltcplay/trigger.py",
  "                for ip in self.cfg.dest:\n                    sock.sendto(pkt, (ip, port))",
  "                for ip in self.cfg.dest[:1]:\n                    sock.sendto(pkt, (ip, port))"),

 ("the sACN sequence number never moves", "ltcplay/trigger.py",
  "        self._seq = (self._seq + 1) & 0xFF",
  "        self._seq = 1"),

 ("a protocol nobody implements is accepted", "ltcplay/trigger.py",
  "        if not isinstance(proto, str) or \\\n                str(proto).lower() not in PROTOCOLS:",
  "        if False:"),

 ("multicast triggers die at the first switch", "ltcplay/trigger.py",
  "            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 8)",
  "            pass"),

 ("the multicast group is computed from the wrong byte", "ltcplay/trigger.py",
  '    return f"239.255.{(universe >> 8) & 0xFF}.{universe & 0xFF}"',
  '    return f"239.255.{universe & 0xFF}.{(universe >> 8) & 0xFF}"'),

 ("broadcast is left switched on for every trigger", "ltcplay/trigger.py",
  '        if any(ip.endswith(".255") or ip == "255.255.255.255"\n               for ip in self.cfg.dest):\n            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)',
  '        if True:\n            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)'),

 # -- the show clock, Fire & Ice 2026 --------------------------------------
 ('the LTC slave never hears the decoded timecode', 'ltcplay/session.py',
  '                    try:\n                        clk.ltc_frame(fr.h, fr.m, fr.s, fr.f,\n                                      captured_at - back)\n',
  '                    try:\n                        pass\n'),

 ('a clock fault starves the chase engine of the rest of the block', 'ltcplay/session.py',
  '                    try:\n                        clk.ltc_frame(fr.h, fr.m, fr.s, fr.f,\n                                      captured_at - back)\n                    except Exception as e:\n                        self.clock_errors += 1\n',
  '                    if True:\n                        clk.ltc_frame(fr.h, fr.m, fr.s, fr.f,\n                                      captured_at - back)\n                    if False:\n                        self.clock_errors += 1\n'),

 ('the GPL path imports the clock at startup', 'ltcplay/session.py',
  'from .showlog import ShowLog\n',
  'from .showlog import ShowLog\nfrom . import clock as _clock_mod\n'),

 ('a master clock still opens a timecode input', 'ltcplay/session.py',
  '        if not self.wav and not (self.clock is not None\n                                 and self.clock.master):',
  '        if not self.wav:'),

 ('Stop leaves the timecode running', 'ltcplay/session.py',
  '        if clk is not None:\n            try:\n                clk.stop()\n            except Exception:\n                pass',
  '        pass'),

 ('Stop blacks out the rig before stopping the timecode', 'ltcplay/session.py',
  '        clk = getattr(self, "clock", None)\n        if clk is not None:\n            try:\n                clk.stop()\n            except Exception:\n                pass\n        if self.sender is not None and not self.no_output:\n            try:\n                before = getattr(self.sender, "packets_sent", 0)\n                for _ in range(3):          # UDP: say it more than once\n                    self.sender.blackout()\n                self.blackout_sent = (\n                    getattr(self.sender, "packets_sent", 0) > before)\n            except Exception:\n                self.blackout_sent = False\n',
  '        if self.sender is not None and not self.no_output:\n            try:\n                before = getattr(self.sender, "packets_sent", 0)\n                for _ in range(3):          # UDP: say it more than once\n                    self.sender.blackout()\n                self.blackout_sent = (\n                    getattr(self.sender, "packets_sent", 0) > before)\n            except Exception:\n                self.blackout_sent = False\n        clk = getattr(self, "clock", None)\n        if clk is not None:\n            try:\n                clk.stop()\n            except Exception:\n                pass\n'),

 ('a free run left over from the operator swallows the next cue', 'ltcplay/session.py',
  '        if self.player.freerun_epoch is not None:\n            self.player.release()\n        try:\n',
  '        try:\n'),

 ('a stopped master cue leaves the pixels chasing on their own', 'ltcplay/session.py',
  '                    bind_ip=self.bind, on_stop=self.player.drop_clock,\n'
  '                    on_pause=lambda: self.player.set_hard_park(True),\n'
  '                    on_resume=lambda: self.player.set_hard_park(False))',
  '                    bind_ip=self.bind, on_stop=None,\n'
  '                    on_pause=lambda: self.player.set_hard_park(True),\n'
  '                    on_resume=lambda: self.player.set_hard_park(False))'),

 ('a Hold never tells the pixels it is a real pause, so they wait out '
  'the noise debounce', 'ltcplay/session.py',
  '                    on_pause=lambda: self.player.set_hard_park(True),',
  '                    on_pause=lambda: None,'),

 ('a Resume never tells the pixels the pause is over, so a hard park '
  'can get stuck on', 'ltcplay/session.py',
  '                    on_resume=lambda: self.player.set_hard_park(False))',
  '                    on_resume=lambda: None)'),

 ('a master clock lets on_lost run the show file on its own', 'ltcplay/session.py',
  '                    self.player.on_lost = "hold"',
  '                    pass'),

 ('dropping the clock leaves the old epoch in place', 'ltcplay/player.py',
  '        with self._lock:\n            self._epoch = None\n            self._pending_jump = None',
  '        with self._lock:\n            self._pending_jump = None'),

 ('a master clock draws its unused input red', 'ltcplay/session.py',
  '        input_used = not (self.clock is not None and self.clock.master)',
  '        input_used = True'),

 ('--bind does not reach the timecode socket', 'ltcplay/session.py',
  '                    bind_ip=self.bind, on_stop=',
  '                    bind_ip=None, on_stop='),

 ('a late tick sends the frame that was due, not the current one', 'ltcplay/clock.py',
  '            n = max(frame_at(now - t0, fps), n_next)',
  '            n = n_next'),

 ('a frame is sent twice when the clock reading is large', 'ltcplay/clock.py',
  '            n = max(frame_at(now - t0, fps), n_next)',
  '            n = frame_at(now - t0, fps)'),

 ('a late tick sends the same frame twice', 'ltcplay/clock.py',
  '            n_next = n + 1',
  '            n_next = n_next + 1'),

 ('one exception in a tick ends the clock', 'ltcplay/clock.py',
  '                more = True\n                self.errors += 1',
  '                more = False\n                self.errors += 1'),

 ('a failing tick logs every frame', 'ltcplay/clock.py',
  '                                       throttle_s=5.0)',
  '                                       throttle_s=0.0)'),

 ('halt returns before the clock thread has stopped', 'ltcplay/clock.py',
  '            t.join(timeout=1.0)',
  '            pass'),

 ('the pacer runs on the 15.6 ms Windows clock', 'ltcplay/clock.py',
  '    def __init__(self, fps, tick, clock=time.perf_counter, sleep=time.sleep,',
  '    def __init__(self, fps, tick, clock=time.monotonic, sleep=time.sleep,'),

 ('the timecode opcode goes out high byte first', 'ltcplay/clock.py',
  '    b[8] = OP_TIMECODE & 0xFF          # low byte first\n    b[9] = (OP_TIMECODE >> 8) & 0xFF',
  '    b[9] = OP_TIMECODE & 0xFF          # low byte first\n    b[8] = (OP_TIMECODE >> 8) & 0xFF'),

 ('the master sends drop frame', 'ltcplay/clock.py',
  'MASTER_TYPE = TYPE_SMPTE',
  'MASTER_TYPE = TYPE_DF'),

 ('broadcast is switched on for every timecode socket', 'ltcplay/clock.py',
  '            if self.broadcast:\n                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)',
  '            if True:\n                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)'),

 ('one dead receiver is logged every frame', 'ltcplay/clock.py',
  '        if f[2] is None or now - f[2] >= self.DEST_LOG_EVERY_S:',
  '        if True:'),

 ('a second cue carries on from where the first left off', 'ltcplay/clock.py',
  '            t0 = self._clock()\n            self._mono_t0',
  '            t0 = self.ticker.t0 or self._clock()\n            self._mono_t0'),

 ('the pixels are stamped from the coarse clock every frame', 'ltcplay/clock.py',
  '                      self._mono_t0 + n / MASTER_FPS, False,',
  '                      self._mono() - (now - (self.ticker.t0 + n / MASTER_FPS)), False,'),

 ('a cue with no length runs its timecode forever', 'ltcplay/clock.py',
  '        if length_s is None or not float(length_s) > 0:',
  '        if length_s is None:\n            length_s = 1e6\n        if False:'),

 ('play and halt race each other', 'ltcplay/clock.py',
  '        with self._lock:\n            if not self._live:\n                raise ClockConfigError("Nothing is running. Press Run first.")',
  '        if True:\n            if not self._live:\n                raise ClockConfigError("Nothing is running. Press Run first.")'),

 ('an unknown hour is taken as the show', 'ltcplay/clock.py',
  '    role = zones.get(h)\n',
  '    role = zones.get(h, "show")\n'),

 ('the hour goes to BEYOND unrebased', 'ltcplay/clock.py',
  '    return role, (0, m, s, f)',
  '    return role, (h, m, s, f)'),

 ('one corrupt LTC frame moves the zone', 'ltcplay/clock.py',
  '            if p is not None and self._agree(role, epoch, p[0], p[1],\n                                             self.CONFIRM_FRAMES):',
  '            if True:'),

 ('the first frame after a gap is believed on its own', 'ltcplay/clock.py',
  '            last = self._last\n            if last is not None and self._agree(',
  '            last = self._last\n            if last is None or at - last[2] > self.hold_s:\n                self._pending = None\n                self._last = (role, epoch, at, pos)\n                return role\n            if last is not None and self._agree('),

 ('stamp jitter is taken raw instead of slewed', 'ltcplay/clock.py',
  '    SLEW = 0.1\n',
  '    SLEW = 1.0\n'),

 ('forwarded frames follow every wobble of the reader', 'ltcplay/clock.py',
  '            if abs(cand - (cur - 0.5)) <= self.FLYWHEEL_FRAMES:',
  '            if False:'),

 ('the slave mixes up the audio clock and the pacing clock', 'ltcplay/clock.py',
  '        at = self._clock() - (self._mono() - captured_at)',
  '        at = captured_at'),

 ('timecode lost mid-show stops instead of free running', 'ltcplay/clock.py',
  '        if role == "show":\n            self.freerunning = age > self.hold_s',
  '        if False:\n            self.freerunning = age > self.hold_s'),

 ('the free run goes past the end of the show', 'ltcplay/clock.py',
  '            if end is not None and cur >= end - 1e-9:',
  '            if False:'),

 ('live timecode is cut at the end of the last pixel cue', 'ltcplay/clock.py',
  '                live = (age * self.fps <= self.FRESH_FRAMES',
  '                live = False and (age * self.fps <= self.FRESH_FRAMES'),

 ('fallback 1 loads although it is not built', 'ltcplay/clock.py',
  '        if src == "ltc_audio_master":\n            raise ClockConfigError(LtcAudioMaster.REFUSAL.format(where=where))',
  '        pass'),

 ('a misspelled clock setting is ignored', 'ltcplay/clock.py',
  '    unknown = sorted(k for k in doc if k not in keys)',
  '    unknown = []'),

 ('broadcast and named nodes both get every frame', 'ltcplay/clock.py',
  '            if clean:\n                raise ClockConfigError(',
  '            if False:\n                raise ClockConfigError('),

 ('a master clock between cues is drawn as a lost feed', 'ltcplay/session.py',
  '            "state": display_mod.shown_state(p),',
  '            "state": p.state,'),

 ('a master clock is told to check a cable it does not have', 'ltcplay/display.py',
  '    if clk is not None and clk.master:\n        return out\n',
  ''),

 ('"clock": null loads silently', 'ltcplay/timeline.py',
  '        if "clock" in doc:',
  '        if doc.get("clock") is not None:'),

 ('a second of timecode is invented after the feed stops at the end', 'ltcplay/clock.py',
  '                live = (age * self.fps <= self.FRESH_FRAMES\n                        and got is not None and got >= end)',
  '                live = age <= self.hold_s'),

 ('an invented 07:20:00 goes out at the hand-off', 'ltcplay/clock.py',
  '                        and got is not None and got >= end)',
  '                        )'),

 ('the Run window reads LOST between master cues', 'ltcplay/display.py',
  '        + pad(sc.c(col, shown), 16) + sc.c(DIM, age_note))',
  '        + pad(sc.c(col, p.state), 16) + sc.c(DIM, age_note))'),

 ('GPL: the lost-feed warnings are silenced for every show', 'ltcplay/display.py',
  '    if clk is not None and clk.master:\n        return out\n',
  '    if True:\n        return out\n'),

 ('the lost-feed warnings are silenced for the LTC slave too', 'ltcplay/display.py',
  '    if clk is not None and clk.master:\n        return out\n',
  '    if clk is not None:\n        return out\n'),

 ('GPL: a lost feed reads STANDBY in every mode', 'ltcplay/display.py',
  '    if clk is not None and clk.master and p.state == LOST \\\n            and not getattr(clk, "playing", False):',
  '    if p.state == LOST:'),

 ("the LTC slave's input is marked not used", 'ltcplay/session.py',
  '        input_used = not (self.clock is not None and self.clock.master)',
  '        input_used = self.clock is None'),

 ("a Stop racing clock_play lets the clock's own error escape", 'ltcplay/session.py',
  '        try:\n            self.clock.play(pick.tc_seconds, pick.duration, pick.name)\n        except ValueError as e:',
  '        if True:\n            self.clock.play(pick.tc_seconds, pick.duration, pick.name)\n        if False:'),

 ("tctest defaults to every node in the show file when --to is left off",
  'ltcplay/tctest.py',
  '        names = to\n        if not names:\n            raise TcTestError(\n'
  '                "--show needs --to as well: name at least one node from "\n'
  '                "its clock.artnet.nodes. tctest never defaults to sending "\n'
  '                "to every node in the show file.")',
  '        names = to or list(available)'),

 ("tctest ignores the output lock another ltcplay is holding",
  'ltcplay/tctest.py',
  '    lock = onlyone.OutputLock(where=lock_path, note=note)\n    try:\n        lock.acquire()\n    except onlyone.AlreadyRunning as e:',
  '    lock = onlyone.OutputLock(where=lock_path, note=note)\n    try:\n        pass\n    except onlyone.AlreadyRunning as e:'),

 ("tctest sends something other than Art-Net timecode, as a pixel or "
  "sACN sender would", 'ltcplay/tctest.py',
  '        out_sock.send(arttimecode(h, m, s, f, MASTER_TYPE))',
  '        out_sock.send(bytes(19))'),

 ("tctest skips the every-run destination warning", 'ltcplay/tctest.py',
  '        print(f"Test timecode is about to go to: {dest_label}. "\n'
  '              f"{GENERAL_WARNING}", file=err_stream)',
  '        pass'),

 ("tctest only warns about a laser system named literally BEYOND again",
  'ltcplay/tctest.py',
  '    if is_broadcast:\n        return ["broadcast"]\n'
  '    return [name for name, _ in dests\n'
  '           if "beyond" in name.lower() or "laser" in name.lower()]',
  '    return [name for name, _ in dests\n'
  '           if name.strip().lower() == "beyond"]'),
 # -- the night journal (Fire & Ice logging, handoff section 9) --------
 ("a journal event may have a blank actor", "ltcplay/journal.py",
  '    if actor not in ACTORS:\n        raise ValueError(f"A journal event\'s actor',
  '    if actor and actor not in ACTORS:\n        raise ValueError(f"A journal event\'s actor'),

 ("an operator event may leave out who or which screen",
  "ltcplay/journal.py",
  '    if actor == "operator" and not (who and screen):',
  '    if False:'),

 ("a fault may be logged as just error", "ltcplay/journal.py",
  '    if len(meaningful) < 3:',
  '    if not words:'),

 ("the journal line loses its seconds and its two spaces",
  "ltcplay/journal.py",
  '    line = f"{local:%H:%M:%S}  {text}"',
  '    line = f"{local:%H:%M} {text}"'),

 ("an operator line stops naming the operator", "ltcplay/journal.py",
  '    if actor == "operator" and who.lower() not in text.lower():',
  '    if False:'),

 ("a journal line can run onto a second line", "ltcplay/journal.py",
  '    return " ".join(text.split())',
  '    return text'),

 ("the page shows its own words, not the file's line", "ltcplay/journal.py",
  '            out.append({"line": r["line"], "at": r.get("at"),',
  '            out.append({"line": r["text"], "at": r.get("at"),'),

 ("the page's last lines come oldest first", "ltcplay/journal.py",
  '            rows = list(self.memory)[-n:][::-1]',
  '            rows = list(self.memory)[-n:]'),

 ("a night file is rewritten instead of appended to", "ltcplay/journal.py",
  '    return open(path, "ab", buffering=0)',
  '    return open(path, "wb", buffering=0)'),

 ("a line cut short by a full disk is left unfinished",
  "ltcplay/journal.py",
  '            if cut:\n                # A full disk cut the last line short.',
  '            if False:\n                # A full disk cut the last line short.'),

 ("a record half written is written twice on the retry",
  "ltcplay/journal.py",
  '                e[1].add(stream)\n                self.writes += 1',
  '                self.writes += 1'),

 ("the machine log is not written", "ltcplay/journal.py",
  '                            ("jsonl", machine_name(night), _jsonl),',
  '                            ("jsonl", machine_name(night), lambda r: b""),'),

 ("per-frame state is written to disk", "ltcplay/journal.py",
  '            self.ring.append((at or self.clock(), dict(data)))',
  '            self.ring.append((at or self.clock(), dict(data)))\n'
  '        self.record(actor="system", action="sample", outcome="done",\n'
  '                    reason="sample", text="A state sample.")'),

 ("the ring buffer keeps 30 s instead of 60", "ltcplay/journal.py",
  'RING_SIZE = RING_SECONDS * RING_HZ',
  'RING_SIZE = RING_SECONDS * RING_HZ // 2'),

 ("the state is sampled once a second", "ltcplay/schedule_service.py",
  '    SAMPLE_S = 1.0 / journal.RING_HZ',
  '    SAMPLE_S = 1.0'),

 ("the service lets a journal failure into the scheduler",
  "ltcplay/schedule_service.py",
  '        try:\n            return fn(*args, **kw)\n        except Exception as e:',
  '        if True:\n            return fn(*args, **kw)\n        try:\n            pass\n        except Exception as e:'),

 ("a full disk raises no health flag", "ltcplay/journal.py",
  '        self.stopped_why = why\n        self._retry_at',
  '        self._retry_at'),

 ("the free space floor is ignored", "ltcplay/journal.py",
  '        if self._free_mb is not None and self._free_mb < self.free_floor_mb:',
  '        if False:'),

 ("the night files are written without their lock", "ltcplay/journal.py",
  '            _lock(lk, LOCK_TRIES, LOCK_WAIT_S, self._sleep)\n            try:\n                nights = []',
  '            try:\n                nights = []'),

 ("a stopped disk is tried again on every line", "ltcplay/journal.py",
  '        if self.stopped_why and not force and self._retry_at is not None \\',
  '        if False and self._retry_at is not None \\'),

 ("the lines waiting for a full disk are thrown away", "ltcplay/journal.py",
  '        self._tails_ok.clear()\n        if first:',
  '        self._tails_ok.clear()\n        self._pending.clear()\n        if first:'),

 ("pruning keeps 89 days instead of 90", "ltcplay/journal.py",
  '        cutoff = today - timedelta(days=self.keep_days)',
  '        cutoff = today - timedelta(days=self.keep_days - 1)'),

 ("pruning goes by the file's timestamp, not its name",
  "ltcplay/journal.py",
  '                if keep_from is None or d >= keep_from:\n'
  '                    continue\n',
  '                if keep_from is None or datetime.fromtimestamp(\n'
  '                        os.path.getmtime(os.path.join(root, name)),\n'
  '                        timezone.utc).date() >= keep_from:\n'
  '                    continue\n'),

 ("pruning removes files it did not write", "ltcplay/journal.py",
  '                   r"\\.(journal\\.txt|jsonl|summary\\.md)$")',
  '                   r"\\.(journal\\.txt|jsonl|summary\\.md)")'),

 ("a line goes to the UTC date's file, not the night's",
  "ltcplay/journal.py",
  '        rec = build_event(at=local, night=night or self.current_night(at),',
  '        rec = build_event(at=local, night=night or at.astimezone(\n'
  '                              timezone.utc).date(),'),

 ("a restart reads as a first start", "ltcplay/journal.py",
  '        if prev is None:\n            text = (f"ltcplay started ({build}).',
  '        if True:\n            text = (f"ltcplay started ({build}).'),

 ("the summary leaves out the faults", "ltcplay/journal.py",
  '    out += _bullets(fault_rows, "None.", limit=None)',
  '    out += _bullets([], "None.", limit=None)'),

 ("the summary leaves out the announcements", "ltcplay/journal.py",
  '    out += _bullets([r["line"] for r in anns], "None played.")',
  '    out += _bullets([], "None played.")'),

 ("End night writes no summary", "ltcplay/schedule_service.py",
  '            self._write_summary(how)\n        # A push, not a poll:',
  '            pass\n        # A push, not a poll:'),

 ("the incident bundle leaves out the last 60 s of state",
  "ltcplay/journal.py",
  '        state = self.last_state(now=self.clock())',
  '        state = []'),

 ("the incident bundle claims flame frames it does not have",
  "ltcplay/journal.py",
  '        if self.flames is None:\n            return {"available": False,\n'
  '                    "note": "Not available: there is no flame bus in this "',
  '        if self.flames is None:\n            return {"available": True,\n'
  '                    "note": "Not available: there is no flame bus in this "'),

 ("the incident bundle leaves out the config", "ltcplay/journal.py",
  '        put("config.json", js(config if config is not None else',
  '        put("config.json", js({"note": "none"} if config is not None else'),

 ("the scheduler's own lines stay in memory only, as before", "ltcplay/schedule_service.py",
  "            self._record_logevent(self._reworded(le))\n            # A failure reported too late to be a failed start is a fault",
  "            self.journal.append(self._reworded(le).to_dict())\n            # A failure reported too late to be a failed start is a fault"),

 ("the service never prunes", "ltcplay/schedule_service.py",
  '            if prune:\n                self._log(self.logbook.prune, d, state=state,',
  '            if False:\n                self._log(self.logbook.prune, d, state=state,'),

 ("the GPL path loads the journal", "ltcplay/web.py",
  'from . import brand as brand_mod\n',
  'from . import brand as brand_mod\nfrom . import journal as _journal\n'),
 # -- the journal, after the review of PR 14 ---------------------------
 ("closing the journal waits for a hung disk", "ltcplay/journal.py",
  '            if t.is_alive():\n                return False\n'
  '        self._writer = None\n',
  '        self._writer = None\n        return self.drain(force=True)\n'),

 ("the way out stops the scheduler before the rig", "ltcplay/cli.py",
  '    httpd.control.stop()\n    # Straight after the show',
  '    if httpd.schedule is not None:\n        httpd.schedule.stop()\n'
  '    httpd.control.stop()\n    # Straight after the show'),

 ("a character UTF-8 cannot carry jams the writer", "ltcplay/journal.py",
  '    return text.encode("utf-8", "backslashreplace")',
  '    return text.encode("utf-8")'),

 ("a record that cannot be written blocks every line behind it",
  "ltcplay/journal.py",
  '                data = _encode_safely(encode, e[0])',
  '                data = encode(e[0])'),

 ("a torn last line from a power cut gets the next line glued on",
  "ltcplay/journal.py",
  '        cut = path not in self._tails_ok and not new and \\',
  '        cut = bool(self.stopped_why) and path not in self._tails_ok \\\n'
  '            and not new and \\'),

 ("a line cut short by a full disk is not looked for afterwards",
  "ltcplay/journal.py",
  '        self._retry_at = now + timedelta(seconds=self.retry_s)\n'
  '        self._tails_ok.clear()\n',
  '        self._retry_at = now + timedelta(seconds=self.retry_s)\n'),

 ("a failing tick writes a line four times a second",
  "ltcplay/schedule_service.py",
  '            kinds[key] = kinds.get(key, 0) + 1\n',
  '            kinds[key] = kinds.get(key, 0) + 1\n'
  '            tf["since"] = now - timedelta(seconds=self.FAULT_REPEAT_S)\n'
  '            tf["last_line"] = None\n'),

 ("the journal's own lines go to the calendar day's file",
  "ltcplay/schedule_service.py",
  '            state=self._state_name, night=self._night)',
  '            state=self._state_name)'),

 ("a show past midnight writes its lines to the calendar day's file",
  "ltcplay/schedule_service.py",
  '        if self.machine is not None:\n            return self.machine.date\n'
  '        return self.logbook.night_of(now)',
  '        if False:\n            return self.machine.date\n'
  '        return self.logbook.night_of(now)'),

 ("a trusted clock still applies the newest-120 floor",
  "ltcplay/journal.py",
  '            if floor:\n'
  '                nights = sorted({d for d, _n, _r, inc in found if not inc},\n',
  '            if True:\n'
  '                nights = sorted({d for d, _n, _r, inc in found if not inc},\n'),

 ("an untrusted clock prunes by age", "ltcplay/journal.py",
  '            if floor:\n'
  '                nights = sorted({d for d, _n, _r, inc in found if not inc},\n',
  '            if False:\n'
  '                nights = sorted({d for d, _n, _r, inc in found if not inc},\n'),

 ("pruning trusts a clock nobody has checked",
  "ltcplay/schedule_service.py",
  '    def _prune_allowed(self):\n',
  '    def _prune_allowed(self):\n        return True\n'),

 ("Service.start never starts the journal writer",
  "ltcplay/schedule_service.py",
  '            self.logbook.start_writer()\n        self._safe_tick()',
  '            pass\n        self._safe_tick()'),

 ("the End-night summary is written inside the scheduler tick",
  "ltcplay/schedule_service.py",
  '        if self.logbook.threaded():\n            # Running for real',
  '        if False:\n            # Running for real'),

 ("the waiting-line cap is gone: memory grows without bound",
  "ltcplay/journal.py",
  '            if len(self._pending) >= PENDING_MAX:',
  '            if False:'),

 ("the resume line no longer says lines were lost", "ltcplay/journal.py",
  '        if self._dropped:\n            a, b = self._dropped_span',
  '        if False:\n            a, b = self._dropped_span'),

 ("a clean stop is never written, so it reads as a crash",
  "ltcplay/schedule_service.py",
  '                self._log(self.logbook.stopping, state=self._state_name(),',
  '                self._log(lambda **k: None, state=self._state_name(),'),

 ("an engine fault is not marked as a fault", "ltcplay/schedule_service.py",
  '            fault=le.outcome in self.FAULT_OUTCOMES)',
  '            fault=False)'),

 ("any screen name is taken", "ltcplay/schedule_service.py",
  '        if not screen.strip():\n            return screen\n'
  '        names = {n.lower(): n for n in self.screens}',
  '        if True:\n            return screen\n'
  '        names = {n.lower(): n for n in self.screens}'),

 ("the summary stops listing faults after 25", "ltcplay/journal.py",
  '    out += _bullets(fault_rows, "None.", limit=None)',
  '    out += _bullets(fault_rows, "None.")'),

 ("a summary's temp file left by a crash is never cleared",
  "ltcplay/journal.py",
  '                if _STALE.match(name):',
  '                if False:'),
 # -- the journal, round 3 of the review of PR 14 ----------------------
 ("close() drains with no time limit", "ltcplay/journal.py",
  '        threading.Thread(target=last, daemon=True,\n'
  '                         name="ltcplay-journal-close").start()\n'
  '        done.wait(wait_s * 2)\n'
  '        return box.get("ok", False)',
  '        last()\n'
  '        return box.get("ok", False)'),

 ("a different tick fault in the middle of a flood counted as a repeat",
  "ltcplay/schedule_service.py",
  '            if key not in kinds and len(kinds) < self.FAULT_KINDS_MAX:',
  '            if not kinds:'),

 ("a failing tick is told apart by its message, not where it failed",
  "ltcplay/schedule_service.py",
  '            self._tick_failed(self._fault_key(e), f"{type(e).__name__}: {e}")',
  '            self._tick_failed(f"{type(e).__name__}: {e}",\n'
  '                              f"{type(e).__name__}: {e}")'),

 ("the way out lets the scheduler tick after the rig stops",
  "ltcplay/cli.py",
  '    halt = getattr(httpd.schedule, "halt", None)',
  '    halt = None'),

 ("housekeeping decides its work outside the lock",
  "ltcplay/schedule_service.py",
  '                look = not self._looked_back\n'
  '                self._looked_back = True\n'
  '                prune = self._pruned_for != d and self._prune_allowed()\n'
  '                if prune:\n'
  '                    self._pruned_for = d\n'
  '                # Computed fresh, right here, never from a snapshot taken\n'
  '                # earlier (round 2 review of PR 25): a trusted clock (the\n'
  '                # time server agreed, and _watch_clock has noticed no jump\n'
  '                # since) prunes by age alone; anything else keeps the\n'
  '                # newest nights and incident folders that exist and\n'
  '                # removes nothing by age, so a wrong clock can never call\n'
  '                # good history old.\n'
  '                floor = not self._clock_trusted()\n'
  '            if look:\n'
  '                self._look_back(d, state)\n'
  '            if prune:\n'
  '                self._log(self.logbook.prune, d, state=state, floor=floor)\n',
  '                look = not self._looked_back\n'
  '                prune = self._pruned_for != d and self._prune_allowed()\n'
  '                # Computed fresh, right here, never from a snapshot taken\n'
  '                # earlier (round 2 review of PR 25): a trusted clock (the\n'
  '                # time server agreed, and _watch_clock has noticed no jump\n'
  '                # since) prunes by age alone; anything else keeps the\n'
  '                # newest nights and incident folders that exist and\n'
  '                # removes nothing by age, so a wrong clock can never call\n'
  '                # good history old.\n'
  '                floor = not self._clock_trusted()\n'
  '            if look:\n'
  '                self._look_back(d, state)\n'
  '                self._looked_back = True\n'
  '            if prune:\n'
  '                self._pruned_for = d\n'
  '                self._log(self.logbook.prune, d, state=state, floor=floor)\n'),

 ("a failure while writing is silent", "ltcplay/journal.py",
  '        except Exception as e:\n'
  '            # Nothing that goes wrong while writing is ever silent.\n'
  '            self._stop(self.clock(), self._why(e))\n'
  '            return False',
  '        except Exception:\n'
  '            return False'),

 # ---------------------------------------------------------------------
 # flamesafe/: the flame safety program (handoff section 15, build step
 # 7a). One mutation per rule in flamesafe/rules.py, plus the link, the
 # packet, the clock and the wall. All caught by flamesafe's own suite,
 # which selftest.py runs in a subprocess.
 # ---------------------------------------------------------------------

 # rule 1: the arm value is derived, not chosen
 ("flamesafe: an arm value outside the G-Flame window is accepted",
  "flamesafe/config.py",
  "    if not (lo <= arm_value <= hi):",
  "    if False:"),

 ("flamesafe: a single-bit flip of the arm value reaching 229 is accepted",
  "flamesafe/config.py",
  "        neighbour = arm_value ^ (1 << bit)\n"
  "        if neighbour >= rules.GFLAME_FIRE_AT:",
  "        neighbour = arm_value ^ (1 << bit)\n"
  "        if False:"),

 ("flamesafe: the unsourced Showven risk needs no acknowledgement",
  "flamesafe/config.py",
  "    if above_unsourced and not accept_unsourced_risk:",
  "    if False:"),

 ("flamesafe: any arm value in the window is accepted, not the derived one",
  "flamesafe/config.py",
  "    if arm_value != derived:",
  "    if False:"),

 # aim slots (2026-10-28): the wave flamer's angle is not a flame
 # the hazers at the engine (2026-10-29)
 ("hazers: the FSEQ's data on a hazer slot reaches it",
  "ltcplay/flamelink.py",
  "        for c in self._hazer_chans:\n            out[c] = 0\n",
  "        for c in ():\n            out[c] = 0\n"),

 ("hazers: the flame frame never says which hazers are armed",
  "ltcplay/flamelink.py",
  "        obj[\"hazers_on\"] = sorted(str(h) for h in hazers_on)",
  "        obj[\"hazers_on\"] = []"),

 ("hazers: the flame link's Abort leaves the hazers armed",
  "ltcplay/flamelink.py",
  "                self._hazers_on = frozenset()\n                self._purge = None",
  "                pass\n                self._purge = None"),

 ("hazers: arming is taken while an Abort is being repeated",
  "ltcplay/flamelink.py",
  "            if self.abort_repeating():\n                want &= self._hazers_on",
  "            if False:\n                want &= self._hazers_on"),

 ("hazers: the fuel tracker and the firing flag are told haze values",
  "ltcplay/flamelink.py",
  "            values = self._with_haze(values, False)\n            self.last_values_nonzero",
  "            self.last_values_nonzero"),

 ("hazers: arming while an Abort is standing",
  "ltcplay/hazers.py",
  "            if self._abort_standing():",
  "            if False:"),

 ("hazers: arming with flamesafe's status stale",
  "ltcplay/hazers.py",
  "        if fl.get(\"stale\") or age is None or age > STATUS_FRESH_S * 1000:",
  "        if False:"),

 ("hazers: arming with flamesafe's hazers not the engine's",
  "ltcplay/hazers.py",
  "        if sorted(theirs) != sorted(mine):",
  "        if False:"),

 ("hazers: the watch never disarms them",
  "ltcplay/hazers.py",
  "        if why:\n            return self.all_off(why)\n        return False",
  "        if False:\n            return self.all_off(why)\n        return False"),

 ("hazers: arming from the show network without screen_arming",
  "ltcplay/remote.py",
  "        if on and not self.may_act_from(ctx.local):",
  "        if False:"),

 ("hazers: an Abort press leaves the hazers armed",
  "ltcplay/remote.py",
  "            self.hazers_cut(f\"{who or 'someone'} pressed Abort on the \"",
  "            (f\"{who or 'someone'} pressed Abort on the \""),

 ("hazers: Stop leaves the hazers armed",
  "ltcplay/remote.py",
  "            self.hazers_cut(why, who, screen)",
  "            pass"),

 ("hazers: arming a hazer needs no permission",
  "ltcplay/users.py",
  "    \"hazers-off\": \"safety\", \"hazer\": \"arm\",",
  "    \"hazers-off\": \"safety\", \"hazer\": None,"),

 ("hazers: a hazer disarm needs the arm permission",
  "ltcplay/users.py",
  "        if name == \"hazer\" and body.get(\"on\") is not True:",
  "        if False:"),

 ("deck: arming a hazer needs no operator or arm permission",
  "ltcplay/streamdeck.py",
  "        if on:\n            refusal = operator_gate(who, \"arm\")",
  "        if False:\n            refusal = operator_gate(who, \"arm\")"),

 ("hazers: Stop on the rack screen's /api/stop leaves the hazers armed",
  "ltcplay/web.py",
  "        fn = getattr(rm, \"hazers_cut\", None)",
  "        fn = None"),

 ("hazers: a zero landing mid-read still carries haze",
  "ltcplay/flamelink.py",
  "            wire = self._with_haze(values, run and gen == self._zero_gen)",
  "            wire = self._with_haze(values, run)"),

 ("hazers: haze runs while the show state is not live",
  "ltcplay/flamelink.py",
  "        if not self._haze_live:\n            return False",
  "        if False:\n            return False"),

 ("hazers: haze runs on Hold",
  "ltcplay/hazers.py",
  "        return self.show() == \"playing\"",
  "        return self.show() != \"idle\""),

 ("hazers: the engine takes a haze slot that is the fan slot",
  "ltcplay/flamelink.py",
  "               for x in slots) or slots[0] == slots[1]:",
  "               for x in slots) or False:"),

 ("hazers: the engine takes a hazer slot that is a group's or another hazer's",
  "ltcplay/flamelink.py",
  "        if any(not _is_int(x) or not 1 <= x <= UNIVERSE_SIZE or x in taken",
  "        if any(not _is_int(x) or not 1 <= x <= UNIVERSE_SIZE"),

 ("hazers: a level outside 0 to 100 percent is taken",
  "ltcplay/hazers.py",
  "    if not 0 <= p <= 100:",
  "    if False:"),

 ("hazers: a levels file with a value past 255 is read",
  "ltcplay/hazers.py",
  "                    if isinstance(v, bool) or not isinstance(v, int) or \\\n                            not 0 <= v <= 255:",
  "                    if isinstance(v, bool) or not isinstance(v, int):"),

 ("hazers: arming while live flame programming is on",
  "ltcplay/remote.py",
  "        if lf is not None and getattr(lf, \"on\", False):",
  "        if False:"),

 ("hazers: the screens say armed before flamesafe has taken it",
  "ltcplay/hazers.py",
  "            elif not p or not p.get(\"enabled\"):",
  "            elif False:"),

 ("hazers: an Abort's journal line forgets what was armed",
  "ltcplay/remote.py",
  "            was = hz.cut()",
  "            hz.cut()\n            was = []"),

 ("deck: with announcements a tap of key 5 opens the hazers page",
  "ltcplay/streamdeck.py",
  "                if self.hazers_view():\n                    # Hazers too: a hold opens their page (tick), a tap",
  "                if False:\n                    # Hazers too: a hold opens their page (tick), a tap"),

 ("deck: the key 5 hold never opens the hazers page",
  "ltcplay/streamdeck.py",
  "        if self.page == 1 and self._prev_keys[ANNOUNCE_KEY] and \\\n                self._key5_hold.fired(now):",
  "        if False:"),

 ("deck: the key 5 hold opens the hazers page over an open alert",
  "ltcplay/streamdeck.py",
  "            if self.alert_pending():\n                self._log(\"Stream Deck: the hazers page was not opened: an \"",
  "            if False:\n                self._log(\"Stream Deck: the hazers page was not opened: an \""),

 ("deck: letting go of key 5 early opens nothing",
  "ltcplay/streamdeck.py",
  "            if not pending:\n                self._open_page2(down, now, latched, morning)\n        self._prev_keys = list(down)",
  "            pass\n        self._prev_keys = list(down)"),

 ("deck: a latch leaves the key 5 hold filling",
  "ltcplay/streamdeck.py",
  "            self._key5_hold.release()\n            self._fire_release(\"the rig is aborted\")",
  "            self._fire_release(\"the rig is aborted\")"),

 ("ui 1.0: arming a hazer is not greyed for a role that may not arm",
  "ltcplay/web/remote.html",
  "    b.disabled = !look.armed && !mayArm;",
  "    b.disabled = false;"),

 ("ui 2.0: arming a hazer is not greyed for a role that may not arm",
  "ltcplay/web/remote2.html",
  "    b.disabled = !look.armed && !mayArm;",
  "    b.disabled = false;"),

 ("ui: a hazer on Hold reads plain ARMED",
  "ltcplay/web/remote.html",
  "  if(armed && h.state === \"held\"){ out.state = \"ARMED, paused by Hold\";",
  "  if(false){ out.state = \"ARMED, paused by Hold\";"),

 ("ui: a level stepper goes past 100 percent",
  "ltcplay/web/remote.html",
  "  return Math.max(0, Math.min(100, v));",
  "  return Math.max(0, v);"),

 # passthrough (2026-10-29): the hazers
 ("flamesafe: passthrough: a Composer takes a hazer on a group slot",
  "flamesafe/composer.py",
  "        if overlap:\n            raise ValueError(f\"passthrough slots {overlap} are flame group \"",
  "        if False:\n            raise ValueError(f\"passthrough slots {overlap} are flame group \""),

 ("flamesafe: passthrough: the hazers are written after the groups",
  "flamesafe/composer.py",
  "        # 6. Every raised group is confirmed: each of its safety slots carries",
  "        for i, p in enumerate(self.pt):\n            if pt_passing[i]:\n                for sl in p.slots:\n                    buf[sl - 1] = self._frame[sl - 1]\n        # 6. Every raised group is confirmed: each of its safety slots carries"),

 ("flamesafe: passthrough: hazers resume after a panic without off then on",
  "flamesafe/composer.py",
  "            self._pt_reset(PT_FAULT)",
  "            pass"),

 ("flamesafe: passthrough: a locked hazer slot is not said",
  "flamesafe/composer.py",
  "            if ok and any(locked):",
  "            if False:"),

 ("flamesafe: passthrough: more than 16 hazers load",
  "flamesafe/config.py",
  "    if len(raw) > PASSTHROUGH_MAX:",
  "    if False:"),

 ("flamesafe: passthrough: a hazer name over 64 characters loads",
  "flamesafe/config.py",
  "        if NAME_MAX < len(name):",
  "        if False:"),

 ("flamesafe: passthrough: hazers_on may list more than 16 names",
  "flamesafe/link.py",
  "    if not isinstance(hazers, list) or len(hazers) > HAZERS_MAX \\",
  "    if not isinstance(hazers, list) or len(hazers) > 10 ** 6 \\"),

 ("flamesafe: passthrough: hazers_on may carry a name over 64 characters",
  "flamesafe/link.py",
  "            or any(not isinstance(h, str) or not 1 <= len(h) <= HAZER_NAME_MAX",
  "            or any(not isinstance(h, str) or not 1 <= len(h)"),

 ("flamesafe: passthrough: hazers_on may name a hazer twice",
  "flamesafe/link.py",
  "                   for h in hazers) or len(set(hazers)) != len(hazers):",
  "                   for h in hazers) or False:"),

 ("flamesafe: passthrough: a hazer passes without being switched on",
  "flamesafe/composer.py",
  "            passing = self._pt_on[i] and self._frame is not None",
  "            passing = self._frame is not None"),

 ("flamesafe: passthrough: a stale link leaves the hazers switched on",
  "flamesafe/composer.py",
  "            self._pt_reset(PT_LINK)",
  "            pass"),

 ("flamesafe: passthrough: the Abort leaves the hazers on",
  "flamesafe/composer.py",
  "        self._pt_reset(PT_ABORT)",
  "        pass"),

 ("flamesafe: passthrough: a change of sender leaves the hazers as they were",
  "flamesafe/composer.py",
  "                self._pt_reset(PT_NEW_SENDER)",
  "                pass"),

 ("flamesafe: passthrough: ON enables a hazer not seen OFF since it was taken away",
  "flamesafe/composer.py",
  "                self._pt_on[i] = self._pt_block[i] == \"\"",
  "                self._pt_on[i] = True"),

 ("flamesafe: passthrough: OFF never clears the block",
  "flamesafe/composer.py",
  "            if not on:\n                self._pt_block[i] = \"\"",
  "            if not on:\n                pass"),

 ("flamesafe: passthrough: a hazer slot in the lockout file passes",
  "flamesafe/composer.py",
  "                    if sl not in self._locked:\n                        buf[sl - 1] = self._frame[sl - 1]",
  "                    if True:\n                        buf[sl - 1] = self._frame[sl - 1]"),

 ("flamesafe: passthrough: a hazer slot may be a group's slot",
  "flamesafe/config.py",
  "            if s in group_slots:",
  "            if False:"),

 ("flamesafe: passthrough: two hazers may share a slot",
  "flamesafe/config.py",
  "            if s in owner:",
  "            if False:"),

 ("flamesafe: passthrough: a hazer may take a group's name",
  "flamesafe/config.py",
  "        if name in names:\n            raise ConfigError(f\"passthrough {name!r}: that name",
  "        if False:\n            raise ConfigError(f\"passthrough {name!r}: that name"),

 ("flamesafe: passthrough: a hazer without its haze or fan slot loads",
  "flamesafe/config.py",
  "            if role not in e:",
  "            if False:"),

 ("flamesafe: passthrough: a slot outside 1 to 512 loads",
  "flamesafe/config.py",
  "                    or not (1 <= s <= rules.UNIVERSE_SIZE):\n                raise ConfigError(f\"passthrough",
  "                    or False:\n                raise ConfigError(f\"passthrough"),

 ("flamesafe: passthrough: a malformed hazers_on is not refused",
  "flamesafe/link.py",
  "    if not isinstance(hazers, list) or len(hazers) > HAZERS_MAX \\",
  "    if False and not isinstance(hazers, list) or len(hazers) > HAZERS_MAX \\"),

 ("flamesafe: aim: every fire slot skips the edge check",
  "flamesafe/composer.py",
  "            if f in g.aim:\n                # An aim slot points the head; it never lights it.",
  "            if f in g.fire:\n                # An aim slot points the head; it never lights it."),

 ("flamesafe: aim: the edge check counts the aim slots again",
  "flamesafe/composer.py",
  "                # An aim slot points the head; it never lights it.\n                continue",
  "                # An aim slot points the head; it never lights it.\n                pass"),

 ("flamesafe: aim: an aim slot is held at zero through the edge-quiet window",
  "flamesafe/composer.py",
  "                elif armed_now and (not quiet or f in g.aim):",
  "                elif armed_now and not quiet:"),

 ("flamesafe: aim: every fire slot goes out in the edge-quiet window",
  "flamesafe/composer.py",
  "                elif armed_now and (not quiet or f in g.aim):",
  "                elif armed_now:"),

 ("flamesafe: aim: a slot outside the group's fire slots is accepted",
  "flamesafe/config.py",
  "        if isinstance(a, bool) or not isinstance(a, int) or a not in fire:",
  "        if isinstance(a, bool) or not isinstance(a, int):"),

 ("flamesafe: aim: every fire slot may be an aim slot",
  "flamesafe/config.py",
  "    if len(set(aim)) >= len(set(fire)):",
  "    if False:"),

 ("flamesafe: aim: an aim slot is sent while the group is disarmed",
  "flamesafe/composer.py",
  "                elif armed_now and (not quiet or f in g.aim):",
  "                elif (armed_now and not quiet) or f in g.aim:"),

 ("flamesafe: aim: two aim slots in one fixture are accepted",
  "flamesafe/config.py",
  "        if k in block:\n            raise ConfigError(f\"{name}: aim slots",
  "        if False:\n            raise ConfigError(f\"{name}: aim slots"),

 ("flamesafe: aim: the aim slots need not be the same channel",
  "flamesafe/config.py",
  "    if len({a - saf[k] for k, a in block.items()}) > 1:",
  "    if False:"),

 ("flamesafe: aim: a wave flamer may aim with any channel",
  "flamesafe/config.py",
  "        if a != saf[k] - 5:\n            raise",
  "        if False:\n            raise"),

 ("flamesafe: aim: a group without a safety_profile may aim",
  "flamesafe/config.py",
  "    if profile is None:\n        raise ConfigError(f\"{name}: aim is only",
  "    if False:\n        raise ConfigError(f\"{name}: aim is only"),

 ("flamesafe: aim: an aimed fixture need not be one 6 channel block",
  "flamesafe/config.py",
  "        if mine != set(range(a, a + 5)):",
  "        if False:"),

 ("flamesafe: aim: an aim slot after the last safety slot is accepted",
  "flamesafe/config.py",
  "            if k is None:\n                raise ConfigError(f\"{name}: aim slot {a} is after",
  "            if False:\n                raise ConfigError(f\"{name}: aim slot {a} is after"),

 # rule 2: the rising edge must be clean
 ("flamesafe: a fire slot at exactly 15 no longer blocks the rise",
  "flamesafe/composer.py",
  "            if commanded[f - 1] >= rules.GFLAME_EDGE_BELOW:",
  "            if commanded[f - 1] > rules.GFLAME_EDGE_BELOW:"),

 ("flamesafe: the edge gate is skipped entirely",
  "flamesafe/composer.py",
  "            if self._fire_is_quiet(commanded, g):",
  "            if True:"),

 # rule 3: the edge is held quiet
 ("flamesafe: fire slots are not held quiet after the rise",
  "flamesafe/composer.py",
  "                self._edge_quiet[i] = rules.EDGE_QUIET_FRAMES + 1",
  "                self._edge_quiet[i] = 0"),

 ("flamesafe: the quiet window is one frame instead of three",
  "flamesafe/rules.py",
  "EDGE_QUIET_FRAMES = 3",
  "EDGE_QUIET_FRAMES = 1"),

 # rule 4: the re-arm dwell
 ("flamesafe: the re-arm dwell is never applied",
  "flamesafe/composer.py",
  "            if da is not None and (t - da) * 1000.0 < self.cfg.min_arm_dwell_ms:",
  "            if False:"),

 ("flamesafe: an operator disarm does not start the dwell",
  "flamesafe/composer.py",
  "                    # bounce straight back up, forced or not.\n"
  "                    self._disarmed_at[i] = t\n",
  "                    # bounce straight back up, forced or not.\n"
  "                    pass\n"),

 ("flamesafe: the dwell countdown rounds down and reads 0 with time to go",
  "flamesafe/composer.py",
  "        return int(math.ceil(left_ms / 1000.0))",
  "        return int(left_ms // 1000)"),

 ("flamesafe: the dwell shows flashing amber, telling the operator to cycle",
  "flamesafe/composer.py",
  '                held.append(("re-arm dwell", "steady"))',
  '                held.append(("re-arm dwell", "flashing"))'),

 ("flamesafe: a dirty edge shows steady amber, telling the operator to wait",
  "flamesafe/composer.py",
  '                held.append(("dirty edge", "flashing"))',
  '                held.append(("dirty edge", "steady"))'),

 # rule 5: chatter
 ("flamesafe: chatter is never detected",
  "flamesafe/composer.py",
  "                if len(rt) >= rules.CHATTER_RISES:",
  "                if False:"),

 ("flamesafe: thirty rises in two seconds are fine",
  "flamesafe/rules.py",
  "CHATTER_RISES = 3",
  "CHATTER_RISES = 30"),

 # rule 6: consent
 ("flamesafe: a down edge counts as consent whether or not the input is alive",
  "flamesafe/composer.py",
  "        consent_ok = advanced",
  "        consent_ok = True"),

 ("flamesafe: a group latches without ever having been seen down",
  "flamesafe/composer.py",
  "            elif self._seen_down[i] and consent_ok:",
  "            elif consent_ok:"),

 ("flamesafe: the first assertion counts as proof of life",
  "flamesafe/composer.py",
  "            # synthetic all-down report a booting watcher emits.\n"
  "            advanced = False",
  "            # synthetic all-down report a booting watcher emits.\n"
  "            advanced = True"),


 ("flamesafe: the latches start out set",
  "flamesafe/composer.py",
  "        self._latched = [False] * self.n\n"
  "        self._arm_seq = None",
  "        self._latched = [True] * self.n\n"
  "        self._arm_seq = None"),

 # rule 7: interruptions clear the latches
 ("flamesafe: the arm input never goes stale",
  "flamesafe/composer.py",
  "        return (self._arm_fresh_at is not None and\n"
  "                (t - self._arm_fresh_at) * 1000.0 <= self.cfg.arm_stale_ms)",
  "        return (self._arm_fresh_at is not None and\n"
  "                (t - self._arm_fresh_at) * 1000.0 <= 1e9)"),

 ("flamesafe: a stalled counter still counts as fresh",
  "flamesafe/composer.py",
  "        if advanced:\n            self._arm_fresh_at = t",
  "        if True:\n            self._arm_fresh_at = t"),

 ("flamesafe: an input that restarted keeps every latch",
  "flamesafe/composer.py",
  '            self._reset_latches("arm input restarted")\n'
  "            advanced = False",
  "            advanced = False"),

 ("flamesafe: a stale input keeps every latch and re-arms when it returns",
  "flamesafe/composer.py",
  "        if not live:\n"
  '            self._reset_latches("arm input stale")',
  "        if not live:\n"
  "            pass"),

 ("flamesafe: an overrun is never noticed",
  "flamesafe/composer.py",
  "                (t - self._last_tick) * 1000.0 > self.cfg.overrun_ms:",
  "                (t - self._last_tick) * 1000.0 > 1e9:"),

 ("flamesafe: a malformed arm assertion is taken as a real one",
  "flamesafe/composer.py",
  "            if len(w) != self.n or any(not isinstance(x, bool) for x in w):\n"
  '                raise ValueError("wanted")',
  "            if False:\n"
  '                raise ValueError("wanted")'),

 # rule 8: the table
 ("flamesafe: two groups may share a fire slot",
  "flamesafe/config.py",
  "            if f in fire_owner:",
  "            if False:"),

 ("flamesafe: a fire slot may be its own safety slot",
  "flamesafe/config.py",
  "            if f in safeties:",
  "            if False:"),

 ("flamesafe: a group with no fire slots is accepted",
  "flamesafe/config.py",
  "        if not fire:",
  "        if False:"),

 ("flamesafe: a fire slot may be another group's safety slot",
  "flamesafe/config.py",
  "        if clash:",
  "        if False:"),

 ("flamesafe: two groups may share a safety slot",
  "flamesafe/config.py",
  "            if s in seen:\n"
  "                raise ConfigError(f\"{g.name} and {seen[s]} share safety \"",
  "            if False:\n"
  "                raise ConfigError(f\"{g.name} and {seen[s]} share safety \""),

 ("flamesafe: the link may leave this machine",
  "flamesafe/config.py",
  "    if loopback_only and not ip.is_loopback:",
  "    if False:"),

 ("flamesafe: an overrun limit shorter than a tick is accepted",
  "flamesafe/config.py",
  "    if c.overrun_ms < 2 * period_ms:",
  "    if False:"),

 # rule 9: only the writer
 ("flamesafe: ltcplay's values on channels that belong to no group pass through",
  "flamesafe/composer.py",
  "        buf = bytearray(rules.UNIVERSE_SIZE)\n        # Passthrough (Jeff, 2026-10-29, the hazers), written BEFORE every",
  "        buf = bytearray(commanded if commanded is not None\n"
  "                        else rules.UNIVERSE_SIZE)\n        # Passthrough (Jeff, 2026-10-29, the hazers), written BEFORE every"),

 ("flamesafe: a disarmed group passes its fire values through",
  "flamesafe/composer.py",
  "                elif armed_now and (not quiet or f in g.aim):",
  "                elif not quiet or f in g.aim:"),

 ("flamesafe: fire commanded on a disarmed group is not logged as a fault",
  "flamesafe/composer.py",
  "            if not armed_now and any(v != 0 for v in cf):",
  "            if False:"),

 # rule 10: zero on anything uncertain
 ("flamesafe: a frame from ltcplay never goes stale",
  "flamesafe/composer.py",
  "        return (self._frame_at is not None and\n"
  "                (t - self._frame_at) * 1000.0 <= self.cfg.frame_stale_ms)",
  "        return (self._frame_at is not None and\n"
  "                (t - self._frame_at) * 1000.0 <= 1e9)"),

 ("flamesafe: a compose fault keeps the latches",
  "flamesafe/composer.py",
  '            self._reset_latches("panic")',
  "            pass"),

 ("flamesafe: the shutdown frames carry the last values instead of zeros",
  "flamesafe/service.py",
  "                zeros = bytes(rules.UNIVERSE_SIZE)",
  "                zeros = (self.last_output.universe if self.last_output\n"
  "                         else bytes(rules.UNIVERSE_SIZE))"),

 # the link
 ("flamesafe: a frame with the wrong contract version is accepted",
  "flamesafe/link.py",
  '    if obj.get("v") != CONTRACT_VERSION:\n'
  '        raise LinkError(f"wrong contract version {obj.get(\'v\')!r}, "\n'
  '                        f"this program speaks {CONTRACT_VERSION}")\n'
  '    if not isinstance(obj.get("k"), str) or obj.get("k") != key:\n'
  '        raise LinkError("wrong key")\n'
  '    if obj.get("t") != "flame":',
  '    if False:\n'
  '        raise LinkError(f"wrong contract version {obj.get(\'v\')!r}, "\n'
  '                        f"this program speaks {CONTRACT_VERSION}")\n'
  '    if not isinstance(obj.get("k"), str) or obj.get("k") != key:\n'
  '        raise LinkError("wrong key")\n'
  '    if obj.get("t") != "flame":'),

 ("flamesafe: a frame that is not 512 values is accepted",
  "flamesafe/link.py",
  "    if not isinstance(values, list) or len(values) != rules.UNIVERSE_SIZE:",
  "    if not isinstance(values, list):"),

 ("flamesafe: a frame for another universe is accepted",
  "flamesafe/link.py",
  "    if universe != expect_universe:",
  "    if False:"),

 ("flamesafe: out-of-order frames are accepted",
  "flamesafe/composer.py",
  "                if frame.seq <= self._frame_seq:",
  "                if False:"),

 # the packet
 ("flamesafe: the sACN packet says priority 100",
  "flamesafe/sacn.py",
  "    b[108] = priority",
  "    b[108] = 100"),

 ("flamesafe: the service sends at priority 100",
  "flamesafe/service.py",
  "        pkt = build_packet(self.cfg.universe, values, self.sacn_seq,\n"
  "                           terminated=terminated)",
  "        pkt = build_packet(self.cfg.universe, values, self.sacn_seq,\n"
  "                           priority=100, terminated=terminated)"),

 ("flamesafe: the priority constant is 100",
  "flamesafe/rules.py",
  "SACN_PRIORITY = 200",
  "SACN_PRIORITY = 100"),

 # the clock
 ("flamesafe: the clock is time.monotonic",
  "flamesafe/composer.py",
  "    return time.perf_counter()",
  "    return time.monotonic()"),

 # the wall
 ("flamesafe imports ltcplay",
  "flamesafe/composer.py",
  "import math\nimport time\n",
  "import math\nimport time\nimport ltcplay.player\n"),

 ("ltcplay imports flamesafe",
  "ltcplay/output.py",
  "import uuid\n",
  "import uuid\nimport flamesafe.rules\n"),

 # ---------------------------------------------------------------------
 # flamesafe/: the safety review of 0bcc3c6 (draft PR #12), one mutation
 # per finding, plus the audit's own entries that were not already here.
 # ---------------------------------------------------------------------

 # finding 1: any local process could fire an armed head with one datagram
 ("flamesafe: a frame with the wrong key is accepted",
  "flamesafe/link.py",
  '    if not isinstance(obj.get("k"), str) or obj.get("k") != key:\n'
  '        raise LinkError("wrong key")\n'
  '    if obj.get("t") != "flame":',
  '    if False:\n'
  '        raise LinkError("wrong key")\n'
  '    if obj.get("t") != "flame":'),

 ("flamesafe: a second sender's frames are taken while the link is live",
  'flamesafe/composer.py',
  '                if sender != self._frame_sender:\n                    self._second_sender(sender, t)\n                    raise ValueError("another sender")',
  '                if False:\n                    self._second_sender(sender, t)\n                    raise ValueError("another sender")'),

 ("flamesafe: the status frame carries no key",
  "flamesafe/link.py",
  '    status["k"] = key\n',
  ""),

 # finding 2: the first assertion after an interruption counted as proof
 ("flamesafe: the first assertion after a stale gap counts as proof of life",
  "flamesafe/composer.py",
  "        consent_ok = advanced and was_live",
  "        consent_ok = advanced"),

 ("flamesafe: the first assertion after an input restart counts as proof of life",
  "flamesafe/composer.py",
  '            self._reset_latches("arm input restarted")\n'
  "            advanced = False",
  '            self._reset_latches("arm input restarted")\n'
  "            advanced = True"),

 # finding 3: a surviving consent mutation
 ("flamesafe: a down edge no longer clears the latch",
  "flamesafe/composer.py",
  "                self._seen_down[i] = consent_ok and not f[i]\n"
  "                self._latched[i] = False\n",
  "                self._seen_down[i] = consent_ok and not f[i]\n"),

 # finding 4: send failures while armed showed green
 ("flamesafe: a failed sACN send is not a fault",
  "flamesafe/service.py",
  '            self.composer.note_fault(f"sACN send failed ({self.send_errors} "\n'
  '                                     f"so far): {e}")',
  "            pass"),

 ("flamesafe: a failed status send is not a fault",
  "flamesafe/service.py",
  '            self.composer.note_fault(f"status frame not sent "\n'
  '                                     f"({self.status_errors} so far): {e}")',
  "            pass"),

 # finding 5: a fire value held for frame_stale_ms after ltcplay stops
 ("flamesafe: a fire value is held for frame_stale_ms after ltcplay stops",
  "flamesafe/composer.py",
  "        commanded = self._frame if fire_live else None",
  "        commanded = self._frame if frame_fresh else None"),

 # finding 6: the console could block the tick loop
 ("flamesafe: the journal writes to the console inline",
  "flamesafe/journal.py",
  "            try:\n"
  "                self._q.put_nowait(line)\n"
  "            except queue.Full:\n"
  "                self.dropped += 1",
  "            print(line, file=self.stream, flush=True)"),

 # finding 7: the dwell could be shorter than the spec's second
 ("flamesafe: the dwell may be shorter than a second",
  "flamesafe/config.py",
  "DWELL_MS_MIN, DWELL_MS_MAX = 1000, 10000",
  "DWELL_MS_MIN, DWELL_MS_MAX = 0, 10000"),

 # minor findings
 ("flamesafe: a chatter refusal does not start the dwell",
  "flamesafe/composer.py",
  "                    self._disarmed_at[i] = t\n"
  "                    self._chatter_at[i] = t",
  "                    self._chatter_at[i] = t"),

 ("flamesafe: a chatter hold reads re-arm dwell",
  "flamesafe/composer.py",
  "                if self._chatter_at[i] is not None and self._chatter_at[i] == da:",
  "                if False:"),

 ("flamesafe: a compose fault has no age",
  "flamesafe/composer.py",
  "                self._fault_at = self._clock()\n"
  "            except Exception:                           # noqa: BLE001\n"
  "                self._fault_at = None",
  "                self._fault_at = None\n"
  "            except Exception:                           # noqa: BLE001\n"
  "                self._fault_at = None"),

 ("flamesafe: a panic status calls a live input stale",
  "flamesafe/composer.py",
  "                                  self._held, self._arm_is_live(t),\n"
  "                                  self._frame_is_fresh(t), False,",
  "                                  self._held, False, False, False,"),

 ("flamesafe: group names are unbounded",
  "flamesafe/config.py",
  "        if len(name) > NAME_MAX:",
  "        if False:"),

 ("flamesafe: the flame universe may be sent to a link port",
  "flamesafe/config.py",
  "            c.destination_port in (c.link_listen_port, c.link_status_port,\n"
  "                                   c.link_arm_port,\n"
  "                                   c.link_status_mirror_port):",
  "            False:"),

 ("flamesafe: wrong group names in an assertion are accepted",
  "flamesafe/composer.py",
  "                want_names = [g.name for g in self.groups]\n"
  "                if list(names) != want_names:",
  "                want_names = [g.name for g in self.groups]\n"
  "                if False:"),

 ("flamesafe: a negative arm seq is taken as a real assertion",
  "flamesafe/composer.py",
  "            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:",
  "            if isinstance(seq, bool) or not isinstance(seq, int):"),

 # the audit's own entries, kept
 ("flamesafe: the quiet window is ignored on the fire pass",
  "flamesafe/composer.py",
  "                elif armed_now and (not quiet or f in g.aim):",
  "                elif armed_now:"),

 ("flamesafe: the arm input is live 30 ms longer than arm_stale_ms",
  "flamesafe/composer.py",
  "                (t - self._arm_fresh_at) * 1000.0 <= self.cfg.arm_stale_ms)",
  "                (t - self._arm_fresh_at) * 1000.0 <= self.cfg.arm_stale_ms + 30)"),

 ("flamesafe: the edge gate reads the slot before each fire slot",
  "flamesafe/composer.py",
  "            if commanded[f - 1] >= rules.GFLAME_EDGE_BELOW:",
  "            if commanded[f - 2] >= rules.GFLAME_EDGE_BELOW:"),

 ("flamesafe: the safety value is written one slot high",
  "flamesafe/composer.py",
  "                buf[s - 1] = values[i] if s in self._raised[i] else DISARM",
  "                buf[s] = values[i] if s in self._raised[i] else DISARM"),

 ("flamesafe: the dwell ends 30 ms early",
  "flamesafe/composer.py",
  "            if da is not None and (t - da) * 1000.0 < self.cfg.min_arm_dwell_ms:",
  "            if da is not None and (t - da) * 1000.0 + 30 < self.cfg.min_arm_dwell_ms:"),


 ("flamesafe: the fire hold is 30 ms longer than fire_hold_ms",
  "flamesafe/composer.py",
  "                (t - self._frame_at) * 1000.0 <= self.cfg.fire_hold_ms)",
  "                (t - self._frame_at) * 1000.0 <= self.cfg.fire_hold_ms + 30)"),


 ("flamesafe: the status calls a group armed when it is wanted and latched",
  "flamesafe/composer.py",
  "            if values[i] != DISARM:\n                state = \"armed\"",
  "            if self._wanted[i] and self._latched[i]:\n                state = \"armed\""),

 ("flamesafe: a repeated seq is accepted while live",
  "flamesafe/composer.py",
  "                if frame.seq <= self._frame_seq:",
  "                if frame.seq < self._frame_seq:"),

 ("flamesafe: the service sends the previous tick's universe",
  "flamesafe/service.py",
  "        out = self.composer.tick()\n        self.last_output = out\n        self._send_universe(out.universe)",
  "        prev = self.last_output\n        out = self.composer.tick()\n        self.last_output = out\n        self._send_universe(prev.universe if prev is not None else out.universe)"),



 ("flamesafe: the rise is recorded even when refused for chatter",
  "flamesafe/composer.py",
  "                    values.append(DISARM)\n                    held.append((\"chatter\", \"steady\"))\n                    continue",
  "                    rt.append(t)\n                    values.append(DISARM)\n                    held.append((\"chatter\", \"steady\"))\n                    continue"),

 # ---------------------------------------------------------------------
 # flamesafe/: the review's second pass on ed58b35.
 # ---------------------------------------------------------------------

 # A: a stalled counter defeated the transition-based reset
 ("flamesafe: a counter frozen past arm_stale_ms then advancing is consent",
  "flamesafe/composer.py",
  "        was_live = self._arm_is_live(t)\n        advanced = False",
  "        was_live = True\n        advanced = False"),

 # B: a fault never cleared
 ("flamesafe: a fault never clears",
  "flamesafe/composer.py",
  "                (t - self._fault_at) >= FAULT_CLEAR_S:",
  "                (t - self._fault_at) >= 1e9:"),

 ("flamesafe: a fault clears after one clean second, not five",
  "flamesafe/composer.py",
  "FAULT_CLEAR_S = 5.0",
  "FAULT_CLEAR_S = 1.0"),

 # C: journal drops
 ("flamesafe: a dropped journal line is not counted",
  "flamesafe/journal.py",
  "            except queue.Full:\n                self.dropped += 1",
  "            except queue.Full:\n                pass"),

 ("flamesafe: the status frame does not carry the journal drop count",
  "flamesafe/composer.py",
  "            \"stats\": dict(self.stats,\n"
  "                          journal_dropped=int(getattr(self._log, \"dropped\",\n"
  "                                                      0) or 0)),",
  "            \"stats\": dict(self.stats, journal_dropped=0),"),

 ("flamesafe: the journal never says how many lines it lost",
  "flamesafe/journal.py",
  "            if self._q.empty() and self.dropped > self._reported_dropped:",
  "            if False:"),

 # E: keys and config strictness
 ("flamesafe: a confirmed config may keep the example key",
  "flamesafe/config.py",
  "    if c.confirmed and c.link_key == EXAMPLE_KEY:",
  "    if False:"),

 ("flamesafe: the status frame carries the example key whatever the config says",
  "flamesafe/link.py",
  '    status["k"] = key\n',
  '    status["k"] = EXAMPLE_KEY\n'),

 ("flamesafe: unknown config keys are ignored",
  "flamesafe/config.py",
  "    unknown = sorted(k for k in d if k not in allowed)\n    if unknown:",
  "    unknown = sorted(k for k in d if k not in allowed)\n    if False:"),

 ("an announcement never holds the schedule first between shows",
  "ltcplay/announce.py",
  '            elif self.hold_requester is not None and not over_show:\n'
  '                hold_refusal, claim_epoch = self._request_hold(\n',
  '            elif False:\n'
  '                hold_refusal, claim_epoch = self._request_hold(\n'),

 # Jeff, 2026-10-26: during a show an announcement plays over it and
 # Holds nothing. Asking for the Hold during a show too (the old rule)
 # pauses the show (test_announce_hold_during_show).
 ("an announcement during a show pauses the show again",
  "ltcplay/announce.py",
  '            elif self.hold_requester is not None and not over_show:\n',
  '            elif self.hold_requester is not None:\n'),

 # A press that cannot play (no device set, the device unplugged) must
 # be refused before anything is Held (Jeff, 2026-10-26).
 ("an announcement with no output device set holds the schedule anyway",
  "ltcplay/announce.py",
  '            elif not self.device_set:\n'
  '                text = (f"{who} pressed Play on {label}{screen_txt}. "\n'
  '                        f"Refused: {self.DEVICE_NOT_SET}")',
  '            elif False:\n'
  '                text = (f"{who} pressed Play on {label}{screen_txt}. "\n'
  '                        f"Refused: {self.DEVICE_NOT_SET}")'),

 # Over a running show with no Hold: a show that ended during the file
 # read leaves nothing holding the schedule. It still plays (Jeff's field
 # report, 2026-10-28: announcements are independent of the schedule),
 # and says it holds nothing.
 ("an announcement over a show that ended while loading never says it "
  "holds nothing", "ltcplay/announce.py",
  '            elif over_show and not held and state not in SHOW_STATES:',
  '            elif False:'),

 # The FIRST checkpoint's own "if hold_refusal:" is covered by "a Hold the
 # scheduler refuses refuses the announcement again" (Jeff's field report,
 # 2026-10-28: a refused Hold never refuses the announcement), and the
 # second, read-only look by "the second check re-Holds instead of only
 # reading the state".

 ("the Hold request does not carry the Play press's own operator and "
  "screen", "ltcplay/announce.py",
  '        try:\n'
  '            return self.hold_requester(who, screen, detail=detail)\n'
  '        except Exception as e:\n'
  '            return _clean(str(e)), None',
  '        try:\n'
  '            return self.hold_requester("", "", detail=detail)\n'
  '        except Exception as e:\n'
  '            return _clean(str(e)), None'),

 ("Service.hold_for_announcement never refuses, even when Hold itself "
  "was refused", "ltcplay/schedule_service.py",
  '            out = self._apply(sch.Event(sch.HOLD_ON, "operator", who=who,\n'
  '                                        screen=screen, detail=detail or ""))\n'
  '            return out.refused or None, self.hold_epoch',
  '            out = self._apply(sch.Event(sch.HOLD_ON, "operator", who=who,\n'
  '                                        screen=screen, detail=detail or ""))\n'
  '            return None, self.hold_epoch'),

 ("announcements never Hold the scheduler in production, web.py never "
  "wires it", "ltcplay/web.py",
  '        httpd.announce.hold_requester = httpd.remote.announce_hold(sched)',
  '        pass'),

 # ---------------------------------------------------------------------
 # flamesafe/: Jeff, 2026-09-26: losing the show program disarms.
 # ---------------------------------------------------------------------


 ("flamesafe: losing the show program keeps the latches, so it re-arms when back",
  "flamesafe/composer.py",
  '            self._reset_latches("show program link lost",\n'
  '                                journal=self._link_live)',
  "            pass"),

 ("flamesafe: a lost show program is journaled and counted on every stale tick, 40 lines a second",
  "flamesafe/composer.py",
  "                                journal=self._link_live)",
  "                                journal=True)"),

 ("flamesafe: a group may arm before the show program has ever answered",
  "flamesafe/composer.py",
  "        link_live = frame_fresh\n",
  "        link_live = frame_fresh or self._frame_at is None\n"),

 ("flamesafe: losing the show program is not journaled",
  "flamesafe/composer.py",
  '            self._event("link", "show program stopped answering: every group "\n'
  '                                "disarmed; cycle the arm to re-arm once it "\n'
  '                                "is back")',
  "            pass"),

 ("flamesafe: a lost show program shows flashing amber, telling the operator to cycle now",
  "flamesafe/composer.py",
  "            return (LINK_LOST, \"steady\")",
  "            return (LINK_LOST, \"flashing\")"),

 # "an announcement plays over a running or paused show" (origin/main,
 # PR #23) retargeted here (Jeff, 2026-09-26): its old anchor,
 # BLOCKED_STATES in interlock_refusal, is gone -- superseded by the
 # Hold-first feature on this branch (an announcement Holds the show
 # instead of refusing outright). The bypass PR #23's mutation checked
 # for -- skipping the Hold block entirely -- is already exactly
 # "an announcement never holds the show first" above, so this keeps
 # the same protection (Hold before play) but breaks a different part
 # of it: the paused case of the SECOND claim in hold_for_announcement.
 # An already-paused show must count as already claimed, with no new
 # Hold event and no journal noise (test:
 # test_announce_hold_for_announcement_no_noise_when_already_held);
 # dropping PAUSED from the recognized set here makes that second claim
 # try to re-Hold a show that cannot take a HOLD_ON from PAUSED (see
 # schedule.py's transition table), so it wrongly refuses instead of
 # succeeding.
 ("hold_for_announcement's second claim no longer recognizes an "
  "already-paused show, only an already-held one",
  "ltcplay/schedule_service.py",
  "            if self.machine.state in (sch.HOLD, sch.PAUSED):\n"
  "                return None, self.hold_epoch",
  "            if self.machine.state in (sch.HOLD,):\n"
  "                return None, self.hold_epoch"),

 ("a second announcement is allowed to start while one plays",
  "ltcplay/announce.py",
  '            if self.playing is not None:\n'
  '                other = LABELS[self.playing]',
  '            if False:\n'
  '                other = LABELS[self.playing]'),

 ("an unavailable announcement file plays anyway", "ltcplay/announce.py",
  '                  self.status_by_id.get(ann_id, {}))\n'
  '            if not st.get("available"):',
  '                  self.status_by_id.get(ann_id, {}))\n'
  '            if False:'),

 ("a name not on the operator list can still press Play",
  "ltcplay/announce.py",
  '                raise ValueError(text)\n'
  '            if not surface and \\\n'
  '                    who.lower() not in {n.lower() for n in self.operators}:\n'
  '                reason = (f"{who!r} is not on the operator list "',
  '                raise ValueError(text)\n'
  '            if False:\n'
  '                reason = (f"{who!r} is not on the operator list "'),

 ("a missing announcement output device falls back to another one",
  "ltcplay/announce.py",
  '    if not hits:\n'
  '        raise ValueError(f"{_clean(name)!r} is not attached. Nothing else "\n'
  '                         f"will be used in its place.")',
  '    if not hits:\n'
  '        if outputs:\n'
  '            return outputs[0]'),

 ("a refused announcement play is not written to the journal",
  "ltcplay/announce.py",
  '                self._emit(actor=actor, action="play",\n'
  '                          outcome="refused",\n'
  '                          reason="another announcement is already playing",\n'
  '                          text=text, ann_id=ann_id, who=who, screen=screen,\n'
  '                          state=state)\n'
  '                raise ValueError(text)\n'
  '            st = ({"available": True, "length_s": TONE_S} if tone else',
  '                raise ValueError(text)\n'
  '            st = ({"available": True, "length_s": TONE_S} if tone else'),

 ("an announcement's device name matches by substring again",
  "ltcplay/announce.py",
  '    hits = [d for d in outputs if d["name"].strip().lower() == want]',
  '    hits = [d for d in outputs if want in d["name"].lower()]'),

 # Jeff's field report, 2026-10-28: "It refuses because the night is
 # over. That should not be this way. Those things are independent." A
 # Hold the scheduler refuses never refuses the announcement
 # (test_announce_hold_refused_still_plays,
 # test_announce_plays_whatever_the_schedule_state).
 ("a Hold the scheduler refuses refuses the announcement again",
  "ltcplay/announce.py",
  '                if hold_refusal:\n'
  '                    claim_epoch = None\n'
  '                    hold_note = no_hold_note(state, hold_refusal)',
  '                if hold_refusal:\n'
  '                    raise ValueError(hold_refusal)'),

 ("an announcement that held nothing never says why",
  "ltcplay/announce.py",
  '            if automatic is None and not held and not hold_note:\n'
  '                hold_note = no_hold_note(state)',
  '            if False:\n'
  '                hold_note = no_hold_note(state)'),

 # Jeff's field report, 2026-10-28, item 9: announcements through the
 # show audio, mixed into its front left and right
 # (test_announce_plays_through_the_show_audio,
 # test_announce_bus_mixes_into_the_show_outputs).
 ("show audio: the announcement bus is never mixed into the outputs",
  "ltcplay/showaudio.py",
  "            bus.mix(outdata)",
  "            pass"),

 ("show audio: an announcement replaces the show's own audio instead of "
  "adding to it", "ltcplay/showaudio.py",
  "            out[:k, o] += seg[:, 0] if seg.shape[1] == 1 else seg[:, i]",
  "            out[:k, o] = seg[:, 0] if seg.shape[1] == 1 else seg[:, i]"),

 ("show audio: the soft clip guard never runs",
  "ltcplay/showaudio.py",
  "                self.softened += soft_clip(out[:k, o])",
  "                pass"),

 ("show audio: the soft clip guard bends the show's audio with no "
  "announcement playing", "ltcplay/showaudio.py",
  "        if self.state != PLAYING or self.pcm is None:\n"
  "            return",
  "        if self.state != PLAYING or self.pcm is None:\n"
  "            soft_clip(out)\n"
  "            return"),

 ("show audio: an older announcement's stop cuts a newer one",
  "ltcplay/showaudio.py",
  "        if int(token) != self.token or self.state != PLAYING:",
  "        if self.state != PLAYING:"),

 ("show audio: a dropped stream leaves the announcement bus playing",
  "ltcplay/showaudio.py",
  "        self.mixer.hard_stop()\n"
  "        self.bus.hard_stop()\n"
  "        self.devstate = DEV_CLOSED",
  "        self.mixer.hard_stop()\n"
  "        self.devstate = DEV_CLOSED"),

 ("announcements: with the show audio running, a second device is opened "
  "anyway", "ltcplay/announce.py",
  "            if route_now is not None or route is not None:",
  "            if False:"),

 ("announcements: the show audio's interface not open still plays",
  "ltcplay/announce.py",
  "            if not r.ready():",
  "            if False:"),

 ("announcements: a show_outputs past the show audio's outputs is not "
  "said", "ltcplay/announce.py",
  '            if max(self.options["show_outputs"]) > r.channels:',
  "            if False:"),

 ("announcements: the gain_db setting is ignored", "ltcplay/announce.py",
  '            gain = 10.0 ** (self.options["gain_db"] / 20.0)',
  "            gain = 1.0"),

 ("announcements: web.py never wires the show audio route",
  "ltcplay/web.py",
  "        httpd.announce.show_route = _show_route",
  "        pass"),

 # Item 10: a name on two outputs is said plainly, and the picker chooses.
 ("announcements: a name on two outputs is used anyway",
  "ltcplay/announce.py",
  '    if len(hits) == 1:\n'
  '        return hits[0]\n'
  '    if not hits:',
  '    if hits:\n'
  '        return hits[0]\n'
  '    if not hits:'),

 ("announcements: the picked audio system is ignored",
  "ltcplay/announce.py",
  "    if hostapi and len(hits) > 1:\n"
  "        hits = [d for d in hits",
  "    if False:\n"
  "        hits = [d for d in hits"),

 ("announcements: anyone may pick the announcement output",
  "ltcplay/users.py",
  '    "announce-output": "settings",',
  '    "announce-output": None,'),

 ("announcements: the announcement output can be picked from the network",
  "ltcplay/remote.py",
  'LOCAL_ONLY = frozenset(("network", "screen-arming", "announce-output",\n',
  'LOCAL_ONLY = frozenset(("network", "screen-arming",\n'),

 ("announcements: the test tone answers from the network",
  "ltcplay/remote.py",
  '                        "announce-test-tone", "preroll-check"))',
  '                        "preroll-check"))'),

 ("announcements: anyone may play the test tone", "ltcplay/users.py",
  '"announce-test-tone": "settings",',
  '"announce-test-tone": None,'),

 # Safety review of #158, and Jeff's field report of 2026-10-28 ("the
 # screen said playing and nothing was heard").
 ("announcements: the recording is resampled and sent under the "
  "announcement lock", "ltcplay/announce.py",
  "        loaded, load_why = self._load_on_show(token, pcm, rate)\n"
  "\n"
  "        with self.lock:\n",
  "        with self.lock:\n"
  "            loaded, load_why = self._load_on_show(token, pcm, rate)\n"),

 ("announcements: a recording over 60 s goes through the show audio",
  "ltcplay/announce.py",
  "        if length_s is not None and length_s > SHOW_ROUTE_MAX_S:",
  "        if False:"),

 ("show audio: the callback frees a finished recording",
  "ltcplay/showaudio.py",
  "        if end or self.frame >= self.pcm.shape[0]:\n"
  "            self.state = ENDED\n"
  "            self._retire()",
  "        if end or self.frame >= self.pcm.shape[0]:\n"
  "            self.state = ENDED\n"
  "            self.pcm = None"),

 ("show audio: retired recordings are never let go",
  "ltcplay/showaudio.py",
  "        self.bus.collect()\n",
  ""),

 ("announcements: a restart of the show audio process is not reported "
  "at once", "ltcplay/announce.py",
  "            if self._seen and self.lost is None:",
  "            if False:"),

 ("announcements: a silent recording is reported as playing",
  "ltcplay/announce.py",
  "            if peak < SILENT_DBFS:",
  "            if False:"),

 ("announcements: an output that never takes audio is not failed",
  "ltcplay/announce.py",
  "        never = (self._player.frames_written == 0 and",
  "        never = (False and"),

 ("announcements: the status says playing before any audio has gone",
  "ltcplay/announce.py",
  '"heard": self._player.frames_written > 0,',
  '"heard": True,'),

 ("announcements: a test tone plays during a show", "ltcplay/announce.py",
  "            if tone and state in SHOW_STATES:",
  "            if False:"),

 ("announcements: the test tone takes a Hold", "ltcplay/announce.py",
  '            elif tone:\n'
  '                hold_note = "A test tone holds nothing."',
  '            elif False:\n'
  '                hold_note = "A test tone holds nothing."'),

 ("announcements: the default level in the show mix is 0 dB",
  "ltcplay/announce.py",
  "DEFAULT_GAIN_DB = -6.0",
  "DEFAULT_GAIN_DB = 0.0"),

 ("announcements: two outputs of one audio system are used anyway",
  "ltcplay/announce.py",
  "    if hostapi and len(hits) > 1:\n"
  "        raise ValueError(same_api_sentence(name, hits))",
  "    if hostapi and len(hits) > 1:\n"
  "        return hits[0]"),

 ("announcements: a save writes before checking the whole file",
  "ltcplay/announce.py",
  "            parse_config(doc, self.config_path)\n"
  "            options = parse_options(doc, self.config_path)\n"
  "            _write_json_atomic(self.config_path, doc)",
  "            _write_json_atomic(self.config_path, doc)\n"
  "            parse_config(doc, self.config_path)\n"
  "            options = parse_options(doc, self.config_path)"),

 ("announcements: the picker's save is not told to the settings watch",
  "ltcplay/announce.py",
  "                    cw.refresh(self.config_path)",
  "                    pass"),

 ("announcements: web.py never gives the announcements the settings watch",
  "ltcplay/web.py",
  "            ann.config_watch = watch",
  "            pass"),

 ("announcements: a reload drops show_outputs, gain_db and hostapi",
  "ltcplay/announce.py",
  '                "status_by_id": status, "options": options}',
  '                "status_by_id": status}'),

 ("announcements: an announcement's Hold leaves Programming playback "
  "playing", "ltcplay/remote.py",
  "                self._announce_pauses_programming(who, screen)",
  "                pass"),

 ("a 32-bit float announcement file is read as integers", "ltcplay/announce.py",
  '    if is_float:\n'
  '        # A real 32-bit IEEE float WAV, decoded as float, not reinterpreted',
  '    if False:\n'
  '        # A real 32-bit IEEE float WAV, decoded as float, not reinterpreted'),

 ("a 24-bit announcement file is shifted the wrong way, changing its "
  "level 256x", "ltcplay/announce.py",
  '        n_samples = len(raw) // 3\n'
  '        padded = np.zeros((n_samples, 4), dtype=np.uint8)\n'
  '        padded[:, 1:] = np.frombuffer(raw, dtype=np.uint8)[\n'
  '            :n_samples * 3].reshape(-1, 3)',
  '        n_samples = len(raw) // 3\n'
  '        padded = np.zeros((n_samples, 4), dtype=np.uint8)\n'
  '        padded[:, :3] = np.frombuffer(raw, dtype=np.uint8)[\n'
  '            :n_samples * 3].reshape(-1, 3)'),

 ("24-bit PCM falls through to the wrong dtype lookup", "ltcplay/announce.py",
  '    elif sampwidth == 3:\n'
  '        # 24-bit PCM: 3 bytes per sample, little-endian.',
  '    elif False:\n'
  '        # 24-bit PCM: 3 bytes per sample, little-endian.'),

 ("an unsupported WAV format tag is accepted", "ltcplay/announce.py",
  '    is_float = tag == 3\n'
  '    if tag is not None and tag not in (1, 3):',
  '    is_float = tag == 3\n'
  '    if False:'),

 ("a non-32-bit floating point WAV is accepted as float", "ltcplay/announce.py",
  '        if bits != 32:\n'
  '            raise ValueError(f"{_clean(path)} is a {bits}-bit floating "',
  '        if False:\n'
  '            raise ValueError(f"{_clean(path)} is a {bits}-bit floating "'),

 ("an announcement device that stopped answering is never noticed",
  "ltcplay/announce.py",
  '        if stalled or too_many_errors:',
  '        if False:'),

 ("a show starting never stops a playing announcement", "ltcplay/schedule_service.py",
  '            reason = "resume" if ev.kind == sch.RESUME else "new"\n'
  '            self._pending_hooks.append(lambda: hook(state_now, reason))',
  '            reason = "resume" if ev.kind == sch.RESUME else "new"\n'
  '            pass'),

 ("the show-start hook tears the stream down synchronously",
  "ltcplay/announce.py",
  '        with self.lock:\n'
  '            if not SHOW_START_STOPS_ANNOUNCEMENT:\n'
  '                return\n'
  '            if self.playing is None or self._player is None:\n'
  '                return\n'
  '            if self._player.stop_reason is not None:\n'
  '                return                      # already stopping',
  '        with self.lock:\n'
  '            self._settle()\n'
  '            if not SHOW_START_STOPS_ANNOUNCEMENT:\n'
  '                return\n'
  '            if self.playing is None or self._player is None:\n'
  '                return\n'
  '            if self._player.stop_reason is not None:\n'
  '                return                      # already stopping'),

 ("the announcements hook runs inside Service.lock again",
  "ltcplay/schedule_service.py",
  '            reason = "resume" if ev.kind == sch.RESUME else "new"\n'
  '            self._pending_hooks.append(lambda: hook(state_now, reason))',
  '            self.on_show_started(self.machine.state)'),

 ("the claim check compares the id, not the attempt", "ltcplay/announce.py",
  '            if self._claim_gen != my_gen or self.playing != ann_id:',
  '            if self.playing != ann_id:'),

 ("load_operators' own sentence is not cleaned of dashes",
  "ltcplay/announce.py",
  '            f"The operator list {_clean(path)} could not be used: "\n'
  '            f"{_clean(str(e)).rstrip(\'.\')}. Using "',
  '            f"The operator list {path} could not be used: "\n'
  '            f"{str(e).rstrip(\'.\')}. Using "'),

 ("tick() waits for the show-start hook even after releasing the lock",
  "ltcplay/schedule_service.py",
  '            for hook in pending:\n'
  '                threading.Thread(target=self._run_hook, args=(hook,),\n'
  '                                 daemon=True,\n'
  '                                 name="ltcplay-announce-hook").start()',
  '            for hook in pending:\n'
  '                self._run_hook(hook)'),

 # ---------------------------------------------------------------------
 # Hold / Resume, Fire & Ice handoff section 5. The clock half only:
 # ArtNetMaster.pause()/resume() in clock.py, Session.clock_pause()/
 # clock_resume() in session.py.
 ("Session.clock_pause accepts a clock that only follows timecode",
  'ltcplay/session.py',
  '        if self.clock is None or not self.clock.master:\n'
  '            raise SessionError("This show follows incoming timecode, so "\n'
  '                               "this machine cannot pause the clock.")\n'
  '        try:\n'
  '            self.clock.pause()',
  '        try:\n'
  '            self.clock.pause()'),

 ("Session.clock_resume accepts a clock that only follows timecode",
  'ltcplay/session.py',
  '        if self.clock is None or not self.clock.master:\n'
  '            raise SessionError("This show follows incoming timecode, so "\n'
  '                               "this machine cannot resume the clock.")\n'
  '        try:\n'
  '            self.clock.resume()',
  '        try:\n'
  '            self.clock.resume()'),

 ("a refused Hold raises the clock's own exception, not a sentence",
  'ltcplay/session.py',
  '        try:\n'
  '            self.clock.pause()\n'
  '        except ValueError as e:\n'
  '            raise SessionError(str(e))',
  '        self.clock.pause()'),

 ("a refused Resume raises the clock's own exception, not a sentence",
  'ltcplay/session.py',
  '        try:\n'
  '            self.clock.resume()\n'
  '        except ValueError as e:\n'
  '            raise SessionError(str(e))',
  '        self.clock.resume()'),

 ("Hold pressed twice on a paused show is accepted", 'ltcplay/clock.py',
  '            if self._paused:\n'
  '                raise ClockConfigError("The clock is already paused.")',
  '            if False:\n'
  '                raise ClockConfigError("The clock is already paused.")'),

 ("Resume is accepted on a clock that was never paused", 'ltcplay/clock.py',
  '            if not self._paused:\n'
  '                raise ClockConfigError("The clock is not paused, so there "\n'
  '                                       "is nothing to resume.")',
  '            if False:\n'
  '                raise ClockConfigError("The clock is not paused, so there "\n'
  '                                       "is nothing to resume.")'),

 ("Hold always freezes on frame zero instead of where the show is",
  'ltcplay/clock.py',
  '            n = self._frame_n if self._frame_n is not None else 0',
  '            n = 0'),

 ("a paused clock stops sending instead of repeating the frozen frame",
  'ltcplay/clock.py',
  '        if self._paused:\n'
  '            # Ignore the ticker\'s own frame count entirely',
  '        if False:\n'
  '            # Ignore the ticker\'s own frame count entirely'),

 ("Resume repeats the frozen frame instead of stepping past it",
  'ltcplay/clock.py',
  '            t0 = self._clock() - (n_frozen + 1) / MASTER_FPS',
  '            t0 = self._clock() - n_frozen / MASTER_FPS'),

 # Adversarial review of PR #10 found both of these by construction, then
 # reproduced them with a widened race window; test_pause_does_not_race_
 # its_own_ticker and test_resume_does_not_race_its_own_ticker force a
 # real tick into the exact same gap deterministically, and catch both.
 ("resume() stops the ticker after clearing paused state again, not "
  "before", 'ltcplay/clock.py',
  '            self.ticker.stop()\n'
  '            label = self._cue[2]\n'
  '            n_frozen = self._frozen_n\n'
  '            self._set_paused(False)\n'
  '            self._frozen = None\n'
  '            self._frozen_n = None\n'
  '            self._frozen_pos = None\n'
  '            t0 = self._clock() - (n_frozen + 1) / MASTER_FPS',
  '            label = self._cue[2]\n'
  '            n_frozen = self._frozen_n\n'
  '            self._set_paused(False)\n'
  '            self._frozen = None\n'
  '            self._frozen_n = None\n'
  '            self._frozen_pos = None\n'
  '            self.ticker.stop()\n'
  '            t0 = self._clock() - (n_frozen + 1) / MASTER_FPS'),

 ("pause() sets the paused flag before the frozen frame again",
  'ltcplay/clock.py',
  '            self._frozen = (h, m, s, f)\n'
  '            self._frozen_n = n\n'
  '            self._frozen_pos = position_s + n / MASTER_FPS\n'
  '            self.last_sent = (h, m, s, f)\n'
  '            self._set_paused(True)\n'
  '            self._sync_point("pause")',
  '            self._set_paused(True)\n'
  '            self._sync_point("pause")\n'
  '            self._frozen = (h, m, s, f)\n'
  '            self._frozen_n = n\n'
  '            self._frozen_pos = position_s + n / MASTER_FPS\n'
  '            self.last_sent = (h, m, s, f)'),

 # -- madmapper.py: device layer (OSC transport, watchdog) --

 ("a cancelled ramp keeps sending anyway", "ltcplay/madmapper.py",
  "            if self._gen_current() != gen:\n"
  "                break",
  "            pass"),

 ("restore_levels never touches the surfaces, only the audio",
  "ltcplay/madmapper.py",
  "        self.set_audio(1.0, wait=wait)\n"
  "        self.set_surfaces(1.0, wait=wait)",
  "        self.set_audio(1.0, wait=wait)"),

 ("the watchdog counts MadMapper's stale re-sent value at bank select "
  "as a real position again (S1)", "ltcplay/madmapper.py",
  "            if self._awaiting_start:\n"
  "                near_start = (show_len_s is not None\n"
  "                             and isinstance(value, (int, float))\n"
  "                             and not math.isnan(value)\n"
  "                             and value * show_len_s <= START_WINDOW_S)\n"
  "                if not near_start:\n"
  "                    return\n"
  "                self._awaiting_start = False",
  "            if self._awaiting_start:\n"
  "                self._awaiting_start = False"),

 ("a NaN heartbeat value is read as zero drift", "ltcplay/madmapper.py",
  "            bad = not isinstance(value, (int, float)) or "
  "math.isnan(value)",
  "            bad = False"),

 ("the heartbeat listener can be bound off loopback by accident",
  "ltcplay/madmapper.py",
  "        if not _is_loopback(bind) and not allow_non_loopback:",
  "        if False:"),

 ("a heartbeat bind failure crashes instead of naming the heartbeat "
  "port", "ltcplay/madmapper.py",
  "        try:\n"
  "            self._sock = self._factory()\n"
  "        except OSError as e:\n"
  "            # Its own sentence, naming the heartbeat port "
  "specifically:\n"
  "            # a bind failure here must never read as the web "
  "server's own\n"
  "            # port being unavailable, which is a different problem "
  "with a\n"
  "            # different fix.\n"
  "            self.bind_error = (",
  "        self._sock = self._factory()\n"
  "        if False:\n"
  "            self.bind_error = ("),

 ("a MadMapper command can hang _submit() forever again",
  "ltcplay/madmapper.py",
  "            got = done.wait(self._submit_timeout_s)",
  "            done.wait()\n"
  "            got = True"),

 # -- beyond.py: the laser blank/unblank device layer --

 ("the allow-list accepts any brightness value, not only 0.0/100.0",
  "ltcplay/beyond.py",
  "    return any(value == v for v in ALLOWED_VALUES)",
  "    return True"),

 ("the allow-list no longer checks for OSC special characters",
  "ltcplay/beyond.py",
  "    if any(c in _SPECIAL_CHARS for c in address):\n"
  "        return False\n"
  "    if address != BRIGHTNESS_ADDR:",
  "    if address != BRIGHTNESS_ADDR:"),

 ("_send()'s guard is removed, so anything can reach the socket",
  'ltcplay/beyond.py',
  '        if address in FORBIDDEN_ADDRESSES or not _allowed(address, value):\n'
  '            raise BeyondConfigError(\n'
  '                f"beyond.py refuses to send {address!r} with value "\n'
  '                f"{value!r}: only the brightness address, with 0.0 or "\n'
  '                f"100.0, and EnableLaserOutput or DisableLaserOutput with "\n'
  '                f"no argument, are ever allowed (S5), and "\n'
  '                f"BlackOut/MasterPause are refused by name as well (BlackOut restarts BEYOND\'s "\n'
  '                f"own core and needs a manual recovery; MasterPause "\n'
  '                f"freezes the beams, a static-beam hazard).")\n'
  '        return self._osc.send(address, value, force=force)',
  '        return self._osc.send(address, value, force=force)'),

 ("the socket's own send() no longer enforces the allow-list at all",
  "ltcplay/beyond.py",
  "        if address in FORBIDDEN_ADDRESSES or not _allowed(address, "
  "value):\n"
  "            raise BeyondConfigError(\n"
  "                f\"beyond.py's socket layer refuses to send {address!r} \"\n"
  "                f\"with value {value!r}: only the brightness address, \"\n"
  "                f\"with 0.0 or 100.0, and EnableLaserOutput or \"\n"
  "                f\"DisableLaserOutput with no argument, are ever allowed \"\n"
  "                f\"off this module (S5).\")\n"
  "        now = self._clock()",
  "        now = self._clock()"),

 ("blank() only sends the packet once, not 3 times", "ltcplay/beyond.py",
  "    def _send_retried(self, value):\n        ok = False\n"
  "        for i in range(RETRY_COUNT):",
  "    def _send_retried(self, value):\n        ok = False\n"
  "        for i in range(1):"),

 ("a failed blank is still reported and journaled as a success",
  "ltcplay/beyond.py",
  "        ok = self._send_retried(BLANK_VALUE)\n"
  "        self.last_command = \"blank\"\n"
  "        self.last_result = \"ok\" if ok else \"failed\"\n"
  "        if ok:",
  "        ok = self._send_retried(BLANK_VALUE)\n"
  "        self.last_command = \"blank\"\n"
  "        self.last_result = \"ok\"\n"
  "        ok = True\n"
  "        if ok:"),

 ("build() no longer blanks at construction", "ltcplay/beyond.py",
  "    link = Beyond(cfg, socket_factory=socket_factory, clock=clock,\n"
  "                 sleep=sleep, journal=journal)\n"
  "    link.blank()",
  "    link = Beyond(cfg, socket_factory=socket_factory, clock=clock,\n"
  "                 sleep=sleep, journal=journal)"),

 ("close() no longer blanks before closing the socket",
  "ltcplay/beyond.py",
  "        try:\n"
  "            self.blank()\n"
  "        except Exception:\n"
  "            pass\n"
  "        self._osc.close()",
  "        self._osc.close()"),

 ("BEYOND's port 8000 clash with MadMapper is no longer refused",
  "ltcplay/beyond.py",
  "        if port == 8000:",
  "        if False:"),

 ("unblank()'s in_show type check is removed, so a truthy value like "
  "\"STANDBY\" or 1 unblanks the lasers during intermission",
  "ltcplay/beyond.py",
  "        if not isinstance(in_show, bool):\n"
  "            raise TypeError(\n"
  "                f\"unblank() needs in_show=True or in_show=False, not "
  "\"\n"
  "                f\"{in_show!r}: whether the lasers may come back is "
  "never \"\n"
  "                f\"guessed from a truthy value.\")",
  "        if False:\n"
  "            raise TypeError(\n"
  "                f\"unblank() needs in_show=True or in_show=False, not "
  "\"\n"
  "                f\"{in_show!r}: whether the lasers may come back is "
  "never \"\n"
  "                f\"guessed from a truthy value.\")"),

 ("unblank() ignores in_show=False and unblanks BEYOND during "
  "intermission anyway", "ltcplay/beyond.py",
  "        if in_show is not True:\n"
  "            self.last_command = \"unblank\"\n"
  "            self.last_result = \"refused\"",
  "        if False:\n"
  "            self.last_command = \"unblank\"\n"
  "            self.last_result = \"refused\""),

 ("unblank(in_show=False)'s refusal is never journalled, a silent skip "
  "instead", "ltcplay/beyond.py",
  "            self.last_result = \"refused\"\n"
  "            self._note(\n"
  "                f\"BEYOND stays blanked{_for_show(show)}: unblank() "
  "was \"\n"
  "                f\"called with in_show=False (no lasers during \"\n"
  "                f\"intermission).\", action=\"unblank\", "
  "outcome=\"refused\",\n"
  "                show=show)\n"
  "            return False",
  "            self.last_result = \"refused\"\n"
  "            return False"),

 ("the first heartbeat after a recovery is judged for drift again "
  "(bench B14)", "ltcplay/madmapper.py",
  "            if self._settle_count > 0:\n"
  "                self._settle_count -= 1\n"
  "                skip_drift = True",
  "            pass"),

 ("the perceptual video curve fades down the same as up (no longer "
  "mirrored)", "ltcplay/madmapper.py",
  "        frac = (v_lin - end) / (start - end)\n"
  "        return end + (frac ** 2) * (start - end)\n"
  "    frac = (v_lin - start) / (end - start)\n"
  "    return start + (1 - (1 - frac) ** 2) * (end - start)",
  "        frac = (v_lin - end) / (start - end)\n"
  "        return end + (frac ** 2) * (start - end)\n"
  "    frac = (v_lin - start) / (end - start)\n"
  "    return start + (frac ** 2) * (end - start)"),

 # -- madmapper.py: fade_all() -- audio and surfaces ramped TOGETHER,
 # in one worker job (devices.on_abort's own fade) --

 ("fade_all()'s cancellation check is removed, so a superseded ramp "
  "keeps sending anyway", "ltcplay/madmapper.py",
  "                if self._gen_current() != gen:\n"
  "                    break\n"
  "                self._send(AUDIO_ADDR, float(audio_values[i]))",
  "                self._send(AUDIO_ADDR, float(audio_values[i]))"),

 ("fade_all()'s surfaces are no longer shaped by the configured video "
  "curve, only plain linear", "ltcplay/madmapper.py",
  "            surface_values = shape_values(ramp_values(start, end, steps),\n"
  "                                          start, end, surface_curve)",
  "            surface_values = ramp_values(start, end, steps)"),

 ("fade_all() stops sending the master audio level, only the surfaces",
  "ltcplay/madmapper.py",
  "                self._send(AUDIO_ADDR, float(audio_values[i]))\n"
  "                for addr in addrs:",
  "                for addr in addrs:"),

 # -- devices.py: on_hold()/on_resume()/on_abort(), composing madmapper.py
 # and beyond.py's own primitives with the handoff's ordering built in --

 ("on_hold() no longer blanks BEYOND, only fades the music",
  "ltcplay/devices.py",
  "    if beyond is not None:\n"
  "        blanked = beyond.blank(show=show)\n"
  "    if madmapper is not None:\n"
  "        madmapper.fade_audio(1.0, 0.0, seconds=fade_seconds, wait=wait)",
  "    if madmapper is not None:\n"
  "        madmapper.fade_audio(1.0, 0.0, seconds=fade_seconds, wait=wait)"),

 ("on_hold() fades the music UP instead of down", "ltcplay/devices.py",
  "madmapper.fade_audio(1.0, 0.0, seconds=fade_seconds, wait=wait)",
  "madmapper.fade_audio(0.0, 1.0, seconds=fade_seconds, wait=wait)"),

 ("on_resume() ignores in_show and unblanks BEYOND during intermission "
  "too", "ltcplay/devices.py",
  "        if in_show is True:\n"
  "            return beyond.unblank(show=show, in_show=True)\n"
  "        else:",
  "        if True:\n"
  "            return beyond.unblank(show=show, in_show=True)\n"
  "        else:"),

 ("on_resume() reads any truthy in_show (\"STANDBY\", 1) as a show and "
  "unblanks the lasers during intermission", "ltcplay/devices.py",
  "    if not isinstance(in_show, bool):\n"
  "        raise TypeError(",
  "    if False:\n"
  "        raise TypeError("),

 ("on_resume() checks in_show's type but unblanks on anything truthy",
  "ltcplay/devices.py",
  "        if in_show is True:\n",
  "        if in_show or True:\n"),

 ("on_hold() swallows a failed BEYOND blank and reports nothing",
  "ltcplay/devices.py",
  "        madmapper.fade_audio(1.0, 0.0, seconds=fade_seconds, wait=wait)\n"
  "    return blanked",
  "        madmapper.fade_audio(1.0, 0.0, seconds=fade_seconds, wait=wait)\n"
  "    return True"),

 ("on_abort() swallows a failed BEYOND blank and reports nothing",
  "ltcplay/devices.py",
  "        madmapper.fade_all(1.0, 0.0, **kwargs)\n"
  "    return blanked",
  "        madmapper.fade_all(1.0, 0.0, **kwargs)\n"
  "    return True"),

 ("on_resume()'s intermission refusal is never journalled, a silent "
  "skip instead", "ltcplay/devices.py",
  "            reblanked = beyond.blank(show=show)\n"
  "            if reblanked:\n"
  "                _note(journal,\n"
  "                     f\"BEYOND stays blanked{_for_show(show)} (re-sent "
  "as \"\n"
  "                     f\"a defensive check): Resume is between shows "
  "(no \"\n"
  "                     f\"lasers during intermission), not during a "
  "show.\",\n"
  "                     action=\"unblank\", outcome=\"refused\", "
  "show=show)",
  "            reblanked = beyond.blank(show=show)\n"
  "            if False:\n"
  "                pass"),

 ("on_resume()'s defensive re-blank is sent but a FAILED re-blank is "
  "never journalled as a fault, only the calm refusal wording",
  "ltcplay/devices.py",
  "            else:\n"
  "                _note(journal,\n"
  "                     f\"BEYOND was told to stay blanked{_for_show(show)} "
  "\"\n"
  "                     f\"(Resume is between shows, no lasers during \"\n"
  "                     f\"intermission), but the defensive re-blank "
  "FAILED: \"\n"
  "                     f\"no packet got out. The lasers may still be "
  "live \"\n"
  "                     f\"through intermission.\", action=\"unblank\",\n"
  "                     outcome=\"refused\", show=show, fault=True)\n"
  "            return reblanked",
  "            return reblanked"),

 ("on_resume(in_show=False) no longer re-sends a defensive blank at all",
  "ltcplay/devices.py",
  "            reblanked = beyond.blank(show=show)\n"
  "            if reblanked:",
  "            reblanked = True\n"
  "            if reblanked:"),

 ("on_abort() no longer blanks BEYOND, only fades MadMapper",
  "ltcplay/devices.py",
  "    if beyond is not None:\n"
  "        blanked = beyond.blank(show=show)\n"
  "    if madmapper is not None:\n"
  "        kwargs = {\"wait\": wait}",
  "    if madmapper is not None:\n"
  "        kwargs = {\"wait\": wait}"),

 ("on_abort() fades everything UP to full instead of down to black",
  "ltcplay/devices.py",
  "        madmapper.fade_all(1.0, 0.0, **kwargs)",
  "        madmapper.fade_all(0.0, 1.0, **kwargs)"),

 ("on_abort() ignores an explicit fade_seconds override", "ltcplay/devices.py",
  "        if fade_seconds is not None:\n"
  "            kwargs[\"seconds\"] = fade_seconds",
  "        if False:\n"
  "            kwargs[\"seconds\"] = fade_seconds"),

 ("show length no longer follows the show's own media when nothing is "
  "configured", "ltcplay/clock.py",
  "        if show_len is None:\n"
  "            # Show length follows the show's own media (Jeff, 2026-09-26):",
  "        if False:\n"
  "            # Show length follows the show's own media (Jeff, 2026-09-26):"),

 ("a configured show length shorter than the music is no longer refused",
  "ltcplay/clock.py",
  "        elif derived is not None and show_len < derived:",
  "        elif False:"),

 ("the derived show length takes whichever cue comes first, not the "
  "latest end", "ltcplay/clock.py",
  "        end = max(end or 0.0, c.end_seconds)",
  "        end = c.end_seconds"),

 ("the scheduler's show_len_s is never checked against the show's media",
  "ltcplay/web.py",
  "                configured = schedule.rule.show_len_s\n"
  "                if configured < derived:",
  "                configured = schedule.rule.show_len_s\n"
  "                if False:"),

 ("the show length derivation for the scheduler check always finds "
  "nothing", "ltcplay/clock.py",
  "        if s.tl is None or s.tl.clock is None:",
  "        if True:"),

 ("the hold epoch never bumps, so a stale claim looks still good",
  "ltcplay/schedule_service.py",
  "            if was_held != is_held:\n"
  "                self.hold_epoch += 1",
  "            if False:\n"
  "                self.hold_epoch += 1"),

 # Reachable again (fix round of #30): a night still on Hold is set aside
 # at the 2 AM nightly reset, and the epoch has to move.
 # test_schedule_delayed_night_closes_at_the_2am_reset.
 ("midnight sweeping a held night never bumps the hold epoch",
  "ltcplay/schedule_service.py",
  "        if self.machine.state == sch.HOLD:\n"
  "            self.hold_epoch += 1",
  "        if False:\n"
  "            self.hold_epoch += 1"),

 ("the second check re-Holds instead of only reading the state",
  "ltcplay/announce.py",
  "            if held and not self._check_still_held(claim_epoch):",
  "            if False:"),

 ("hold_still_claimed ignores the epoch, only the state",
  "ltcplay/schedule_service.py",
  "            return (self.hold_epoch == claim_epoch\n"
  "                    and self.machine.state in (sch.HOLD, sch.PAUSED))",
  "            return self.machine.state in (sch.HOLD, sch.PAUSED)"),

 ("on_show_started always says a show started, never that it resumed",
  "ltcplay/announce.py",
  "            self._player.stop_reason = (\"the show resumed\"\n"
  "                                        if reason == \"resume\" else\n"
  "                                        \"a show started\")",
  "            self._player.stop_reason = \"a show started\""),

 ("the resume reason is never computed, on_show_started never learns why",
  "ltcplay/schedule_service.py",
  "            reason = \"resume\" if ev.kind == sch.RESUME else \"new\"",
  "            reason = \"new\""),

 ("hold_for_announcement issues HOLD_ON even when already held or paused",
  "ltcplay/schedule_service.py",
  "            if self.machine.state in (sch.HOLD, sch.PAUSED):\n"
  "                return None, self.hold_epoch",
  "            if False:\n"
  "                return None, self.hold_epoch"),

 ("an announcement's Hold claim never names the announcement in the "
  "journal", "ltcplay/announce.py",
  "                hold_refusal, claim_epoch = self._request_hold(\n"
  "                    who, screen,\n"
  "                    detail=f\"played the {label} announcement{screen_txt}\")",
  "                hold_refusal, claim_epoch = self._request_hold(\n"
  "                    who, screen)"),

 ("schedule.py never uses the announcement's own claim wording, during a "
  "show", "ltcplay/schedule.py",
  "        if ev.detail:\n"
  "            text = (f\"{_operator_name(ev)} {ev.detail}. Show {n} is held \"\n"
  "                    f\"for it: flame cues zeroed, lasers blanked, music \"\n"
  "                    f\"fading out. Resume carries on from there.\")",
  "        if False:\n"
  "            text = (f\"{_operator_name(ev)} {ev.detail}. Show {n} is held \"\n"
  "                    f\"for it: flame cues zeroed, lasers blanked, music \"\n"
  "                    f\"fading out. Resume carries on from there.\")"),

 ("schedule.py never uses the announcement's own claim wording, between "
  "shows", "ltcplay/schedule.py",
  "    if ev.detail:\n"
  "        text = (f\"{_operator_name(ev)} {ev.detail}. No show starts by \"\n"
  "                f\"itself until Resume; a show whose time passes meanwhile \"\n"
  "                f\"is delayed and waits for Start now.\")",
  "    if False:\n"
  "        text = (f\"{_operator_name(ev)} {ev.detail}. No show starts by \"\n"
  "                f\"itself until Resume; a show whose time passes meanwhile \"\n"
  "                f\"is delayed and waits for Start now.\")"),

 ("more than one candidate show file is not treated as ambiguous",
  "ltcplay/clock.py",
  "    if len(candidates) > 1:",
  "    if False:"),

 ("a folder with no derivable show length never warns, it silently skips",
  "ltcplay/web.py",
  "            elif warning:",
  "            elif False:"),

 ("WAVE_FORMAT_EXTENSIBLE float is never resolved, it stays refused with "
  "a raw GUID", "ltcplay/announce.py",
  "                    if tag == 0xFFFE and len(body) >= 40:\n"
  "                        guid = body[24:40]\n"
  "                        if guid[4:] == _EXTENSIBLE_SUBFORMAT_TAIL:\n"
  "                            return int.from_bytes(guid[:4], \"little\")",
  "                    if False:\n"
  "                        guid = body[24:40]\n"
  "                        if guid[4:] == _EXTENSIBLE_SUBFORMAT_TAIL:\n"
  "                            return int.from_bytes(guid[:4], \"little\")"),

 ("the data chunk size sanity check never runs", "ltcplay/announce.py",
  "    problem = _data_chunk_size_problem(path, size)\n"
  "    if problem:\n"
  "        raise ValueError(problem)",
  "    problem = None\n"
  "    if problem:\n"
  "        raise ValueError(problem)"),

 ("a placeholder data chunk size (0 or 0xFFFFFFFF) is accepted as healthy",
  "ltcplay/announce.py",
  "                    if size == 0 or size == 0xFFFFFFFF:",
  "                    if False:"),

 ("a data chunk bigger than the file on disk is accepted, overstating "
  "the length", "ltcplay/announce.py",
  "                    if size > remaining:",
  "                    if False:"),
 # -- the journal, Jeff's settings of 2026-09-26 ------------------------
 ("a batch goes to the disk without an fsync", "ltcplay/journal.py",
  '                self._fsync(fh.fileno())\n                if new:',
  '                pass\n                if new:'),

 ("a line can wait five seconds in memory", "ltcplay/journal.py",
  '            self._wake.wait(MAX_LINE_WAIT_S)',
  '            self._wake.wait(5.0)'),

 ("night files are kept 90 days again", "ltcplay/journal.py",
  'KEEP_DAYS = 120',
  'KEEP_DAYS = 90'),

 ("incident folders are never pruned", "ltcplay/journal.py",
  '            found.append((d, name, inc_root, True))',
  '            pass'),

 ("a checked clock still keeps the newest nights",
  "ltcplay/schedule_service.py",
  '                floor = not self._clock_trusted()',
  '                floor = True'),

 ("an unchecked clock prunes by age alone", "ltcplay/schedule_service.py",
  '                self._log(self.logbook.prune, d, state=state, floor=floor)',
  '                self._log(self.logbook.prune, d, state=state, floor=False)'),

 ("the free space floor is 100 MB again", "ltcplay/journal.py",
  'FREE_FLOOR_MB = 500',
  'FREE_FLOOR_MB = 100'),

 ("a start looks back only one night for a missing summary",
  "ltcplay/schedule_service.py",
  '            if n < d and not os.path.exists(',
  '            if n == d - timedelta(days=1) and not os.path.exists('),

 ("the journal says End night again", "ltcplay/schedule.py",
  '            f"{_operator_name(ev)} pressed Close for the night"',
  '            f"{_operator_name(ev)} pressed End night"'),

 ("the transport panel still says End night", "ltcplay/schedule.py",
  '    {"id": "end_night", "label": "Close for the night", "event": END_NIGHT,',
  '    {"id": "end_night", "label": "End night", "event": END_NIGHT,'),
 # Pixel output pacing, Fire & Ice bench B9, 2026-09-25: a show's pixel
 # timing against the cue stepped once, under load, and never came back.
 # The fix (this file, Player._loop()) is clock.py's Ticker's own
 # technique -- every deadline computed fresh from one origin read once,
 # never from a running total or from when the last frame actually went
 # out -- so these two mutations put back the two ways that can regress.
 ("the pixel loop's origin moves every frame instead of staying fixed",
  'ltcplay/player.py',
  '                time.sleep(0.01)\n'
  '            n_next = n + 1',
  '                time.sleep(0.01)\n'
  '            n_next = n + 1\n'
  '            t0 = now'),

 ("a stalled pixel loop never catches up to the frame that is actually "
  "due", 'ltcplay/player.py',
  '            n = max(int((now - t0) / period + 1e-9), n_next)',
  '            n = n_next'),

 # Opus review of PR #19, two survivors it found with its own repro
 # scripts (scratchpad/pixelstep_failtick.py, pixelstep's sleep-cap
 # check): a failed tick has to move the schedule on regardless, or the
 # loop retries the same already-past slot forever; and the wait for a
 # far-off deadline has to stay capped, or Stop would wait out the whole
 # gap.
 ("a failed pixel tick no longer advances the schedule", 'ltcplay/player.py',
  '                time.sleep(0.01)\n            n_next = n + 1',
  '                time.sleep(0.01)\n                continue\n            n_next = n + 1'),

 ("the pixel loop's wait for a far-off deadline is no longer capped",
  'ltcplay/player.py',
  '                time.sleep(min(due - now, 0.05))',
  '                time.sleep(due - now)'),

 # This fix, 2026-09-26: bench evidence, Fire & Ice, run hold1 (B4). Two
 # findings, two mutations.
 ("resume() forgets which frame the ticker already considers itself at, "
  "so it double-counts a hold as skipped", 'ltcplay/clock.py',
  '            return self.ticker.start(t0, n0=n_frozen + 1)',
  '            return self.ticker.start(t0)'),

 ("a machine-generated pause waits for the same debounce a real LTC "
  "deck's noise needs", 'ltcplay/player.py',
  '        parked = hard_parked or (park_since is not None\n'
  '                                 and now - park_since >= self.park_s)\n'
  '\n'
  '        since = now - last',
  '        parked = (park_since is not None\n'
  '                  and now - park_since >= self.park_s)\n'
  '\n'
  '        since = now - last'),

 # The same bypass, the same debounce, but read by _state_from_feed()
 # instead of _tick(): the override path (Freerun, Blackout, Preshow)
 # keeps the feed's OWN readout honest through _state_from_feed(), a
 # second, separate copy of the same parked computation -- so a
 # machine-generated Hold has to reach this one too, or the display lies
 # about being parked for up to park_s while any override is engaged.
 ("under Freerun, Blackout or Preshow, a machine-generated pause waits "
  "for the same debounce a real LTC deck's noise needs",
  'ltcplay/player.py',
  '        parked = hard_parked or (park_since is not None\n'
  '                                 and now - park_since >= self.park_s)\n'
  '        since = now - last',
  '        parked = (park_since is not None\n'
  '                  and now - park_since >= self.park_s)\n'
  '        since = now - last'),

 # A show file, or another JSON file a person hand-edits, saved by Windows
 # Notepad or PowerShell carries a UTF-8 BOM. "utf-8-sig" strips it if it is
 # there and does nothing if it is not; plain "utf-8" instead reports
 # "Unexpected UTF-8 BOM" and refuses a perfectly good show file. One
 # mutation per loader that was changed to accept one.
 ("a show file with a BOM is refused again", "ltcplay/timeline.py",
  'with open(path, encoding="utf-8-sig") as fh:',
  'with open(path, encoding="utf-8") as fh:'),

 ("the page's show-folder read refuses a BOM show file again",
  "ltcplay/web.py",
  'with open(path, encoding="utf-8-sig") as fh:\n            doc = json.load(fh)\n        if folder is None:',
  'with open(path, encoding="utf-8") as fh:\n            doc = json.load(fh)\n        if folder is None:'),

 ("tctest --show refuses a BOM show file again", "ltcplay/tctest.py",
  'with open(show_path, encoding="utf-8-sig") as fh:',
  'with open(show_path, encoding="utf-8") as fh:'),

 ("showdir refuses a BOM show file again", "ltcplay/cli.py",
  'with open(tl_path, encoding="utf-8-sig") as fh:',
  'with open(tl_path, encoding="utf-8") as fh:'),

 ("a BOM input settings file is refused again", "ltcplay/settings.py",
  'with open(p, encoding="utf-8-sig") as fh:',
  'with open(p, encoding="utf-8") as fh:'),

 ("a BOM brand file is refused again", "ltcplay/brand.py",
  'with open(path(), encoding="utf-8-sig") as fh:',
  'with open(path(), encoding="utf-8") as fh:'),

 # -- B11: the operator page's /api/state cache (web.py's Control.state()) --

 ("the /api/state cache never actually holds for its interval",
  "ltcplay/web.py",
  "    STATE_CACHE_S = 0.2",
  "    STATE_CACHE_S = 0.0"),

 ("concurrent misses of the /api/state cache all rebuild it at once",
  "ltcplay/web.py",
  """        try:
            cached = self._state_cache
            now = time.monotonic()
            if cached is not None and now - cached[1] < self.STATE_CACHE_S:
                return cached[0]             # built while this waited for the lock
            fresh = self._build_state()
            self._state_cache = (fresh, time.monotonic())
            return fresh
        finally:
            self._state_building.release()""",
  """        try:
            fresh = self._build_state()
            self._state_cache = (fresh, time.monotonic())
            return fresh
        finally:
            self._state_building.release()"""),

 ("a cached /api/state answer is the raw cache entry, not its payload",
  "ltcplay/web.py",
  """        now = time.monotonic()
        cached = self._state_cache
        if cached is not None and now - cached[1] < self.STATE_CACHE_S:
            return cached[0]
        if not self._state_building.acquire(blocking=False):""",
  """        now = time.monotonic()
        cached = self._state_cache
        if cached is not None and now - cached[1] < self.STATE_CACHE_S:
            return cached
        if not self._state_building.acquire(blocking=False):"""),

 ("the terminal/GPL build id is cached at module scope, "
  "so it stops updating for the rest of the run",
  "ltcplay/version.py",
  '''def build():
    """(id, file count, newest mtime) for the program as it sits on disk."""
    h = hashlib.sha256()
    newest = 0.0
    files = _files()
    for p in files:
        # Forward slashes whatever the OS, so the same files give the same
        # build id on a Mac and on Windows. On a Mac this changes nothing.
        rel = os.path.relpath(p, folder()).replace(os.sep, "/")
        h.update(rel.encode("utf-8", "replace"))
        h.update(b"\\0")
        try:
            with open(p, "rb") as fh:
                for b in iter(lambda: fh.read(1 << 20), b""):
                    h.update(b)
            newest = max(newest, os.path.getmtime(p))
        except OSError:
            h.update(b"<unreadable>")
    return h.hexdigest()[:10], len(files), newest''',
  '''_BUILD_CACHE = None


def build():
    """(id, file count, newest mtime) for the program as it sits on disk."""
    global _BUILD_CACHE
    if _BUILD_CACHE is not None:
        return _BUILD_CACHE
    h = hashlib.sha256()
    newest = 0.0
    files = _files()
    for p in files:
        # Forward slashes whatever the OS, so the same files give the same
        # build id on a Mac and on Windows. On a Mac this changes nothing.
        rel = os.path.relpath(p, folder()).replace(os.sep, "/")
        h.update(rel.encode("utf-8", "replace"))
        h.update(b"\\0")
        try:
            with open(p, "rb") as fh:
                for b in iter(lambda: fh.read(1 << 20), b""):
                    h.update(b)
            newest = max(newest, os.path.getmtime(p))
        except OSError:
            h.update(b"<unreadable>")
    _BUILD_CACHE = h.hexdigest()[:10], len(files), newest
    return _BUILD_CACHE'''),


 # -- audio_master (handoff section 4a, Jeff 2026-09-27): ltcplay plays the
 # show's multi-track audio and the timecode is read off the audio device.
 # One per rule: the refusals, no Windows shared mixer, the audio's own
 # process, the mix, following the audio, Hold/Resume/Abort, the interface
 # lost and back, the end of the cue, and the wall around GPL.
 ('audio_master: a stem at another sample rate plays',
  'ltcplay/showaudio.py',
  '            if info.rate != RATE:\n',
  '            if False:\n'),

 ('audio_master: stems of different lengths play',
  'ltcplay/showaudio.py',
  '        if len(set(frames)) > 1 and not cue.allow_different_lengths:\n',
  '        if False:\n'),

 ('audio_master: a missing stem is not named as missing',
  'ltcplay/showaudio.py',
  '            if not os.path.exists(path):\n                raise AudioConfigError(\n                    f"The {role} audio file',
  '            if False:\n                raise AudioConfigError(\n                    f"The {role} audio file'),

 ('audio_master: the show file may ask for a rate other than 48000',
  'ltcplay/showaudio.py',
  '        if rate != RATE or isinstance(rate, bool):\n',
  '        if isinstance(rate, bool):\n'),

 ('audio_master: an interface with too few outputs is opened',
  'ltcplay/showaudio.py',
  '    if channels > have:\n',
  '    if False:\n'),

 ('audio_master: Windows falls back to the shared mixer on its own',
  'ltcplay/showaudio.py',
  '        order = list(WINDOWS_APIS) + (list(WINDOWS_SHARED_APIS)\n                                      if allow_shared else [])',
  '        order = list(WINDOWS_APIS) + list(WINDOWS_SHARED_APIS)'),

 ("audio_master: WASAPI is preferred over the interface's ASIO driver",
  'ltcplay/showaudio.py',
  '    ("ASIO", False, "ASIO", True),\n    ("Windows WASAPI", True, "WASAPI exclusive", True),\n',
  '    ("Windows WASAPI", True, "WASAPI exclusive", True),\n    ("ASIO", False, "ASIO", True),\n'),

 ('audio_master: WASAPI is opened in shared mode',
  'ltcplay/showaudio.py',
  '        extra = sd.WasapiSettings(exclusive=True)',
  '        extra = sd.WasapiSettings(exclusive=False)'),

 ('audio_master: sounddevice never sees ASIO',
  'ltcplay/showaudio.py',
  '        os.environ["SD_ENABLE_ASIO"] = "1"\n',
  '        pass\n'),

 ("audio_master: a stem's gain is ignored",
  'ltcplay/showaudio.py',
  '                col = seg[:, 0] * np.float32(gain)\n',
  '                col = seg[:, 0]\n'),

 ('audio_master: stems on one output replace each other',
  'ltcplay/showaudio.py',
  '                    out[:k, o] += seg[:, i] * np.float32(gain)\n',
  '                    out[:k, o] = seg[:, i] * np.float32(gain)\n'),

 ("audio_master: a stem's channels go to the wrong outputs",
  'ltcplay/showaudio.py',
  '                for i, o in enumerate(outs):\n                    out[:k, o]',
  '                for i, o in enumerate(outs[::-1]):\n                    out[:k, o]'),

 ('audio_master: the mix goes over full scale',
  'ltcplay/showaudio.py',
  '            np.clip(out, -1.0, 1.0, out=out)\n',
  '            pass\n'),

 ('audio_master: the audio process is forked, not spawned',
  'ltcplay/showaudio.py',
  '        self._ctx = multiprocessing.get_context("spawn")',
  '        self._ctx = multiprocessing.get_context("fork")'),

 ('audio_master: a dead audio process is never replaced',
  'ltcplay/showaudio.py',
  '                np_, nc, why = self._spawn(stop)\n                if why is None:',
  '                np_, nc, why = None, None, "not replaced"\n                if why is None:'),

 ('audio_master: PortAudio is re-initialised under a working stream',
  'ltcplay/showaudio.py',
  '            if why is None:\n                return\n            self._drop()',
  '            if why is None:\n                self.sd._initialize()\n                return\n            self._drop()'),

 ('audio_master: reopening the interface never backs off',
  'ltcplay/showaudio.py',
  '        self.next_try = now + self.RETRY_S[min(self.fails - 1,\n',
  '        self.next_try = now + self.RETRY_S[min(0,\n'),

 ('audio_master: the clock stops following the audio after the first callback',
  'ltcplay/clock.py',
  '            self._epoch += (e - self._epoch) * self.FOLLOW_SLEW\n',
  '            pass\n'),

 ('audio_master: a frame goes out late instead of when the audio reaches it',
  'ltcplay/clock.py',
  '        return min(self._epoch + (last + 1) / MASTER_FPS,\n                   now + self.MAX_SLEEP_S)',
  '        return now + self.MAX_SLEEP_S'),

 ('audio_master: Hold freezes the timecode before the audio has stopped',
  'ltcplay/clock.py',
  '                self._stop_frame = None\n                self._pause_req = True\n',
  '                self._stop_frame = None\n                self._pause_req = True\n                self._freeze(now)\n'),

 ('audio_master: Hold stops repeating the frozen frame',
  'ltcplay/clock.py',
  '                self._send_frame(self._frozen_frame, now, frozen=True)\n',
  '                self._last_send_at = now\n'),

 ('audio_master: Resume restarts the audio away from where it stopped',
  'ltcplay/clock.py',
  '        start = int(round(self._frozen_sec * self.rate))\n        fade = ',
  '        start = int(round(self._frozen_sec * self.rate)) + 480\n        fade = '),

 ('audio_master: Abort cuts the audio instead of fading it',
  'ltcplay/clock.py',
  '            self._send(("level", 0.0, fade))\n            self._level_down = True\n',
  '            self._end("stopped", now)\n            return\n'),

 ('audio_master: losing the audio jumps the timecode',
  'ltcplay/clock.py',
  '        self._mode = "freerun"\n        self._target = None\n        # A stall seen only from here',
  '        self._epoch = (self._epoch or now) - 0.2\n        self._mode = "freerun"\n        self._target = None\n        # A stall seen only from here'),

 ('audio_master: a dropout says nothing on the page or in the journal',
  'ltcplay/clock.py',
  '        self._set_fault(self._loss_fault, now)\n',
  '        pass\n'),

 ('audio_master: the audio never comes back when the interface does',
  'ltcplay/clock.py',
  '        if now < self._next_return:\n            return\n',
  '        if True:\n            return\n'),

 ('audio_master: the audio comes back where it dropped out, not where the show is',
  'ltcplay/clock.py',
  '        start = now - self._epoch + self.LEAD_S + self._heard_latency()\n',
  '        start = (self._lost_at or now) - self._epoch + self.LEAD_S + self._heard_latency()\n'),

 ('audio_master: the handover jumps instead of slewing',
  'ltcplay/clock.py',
  '        self._epoch += max(-lim, min(lim, d))\n',
  '        self._epoch += d\n'),

 ('audio_master: the audio ending does not end the cue',
  'ltcplay/clock.py',
  '        if frame >= self._end_frames(cue):\n            self._end("finished", now)',
  '        if False:\n            self._end("finished", now)'),

 # Jeff's field report, 2026-10-28, items 12 and 14: the show ends at the
 # longest of its audio, lights file and show length; the timecode holds
 # its last frame at the end; Run says when the audio is short; the
 # MadMapper heartbeat follows the real show length.
 ('audio_master: the show ends with its audio again, not its lights file',
  'ltcplay/clock.py',
  '            want = self.lengths.get(cue["label"])\n            if want:',
  '            want = None\n            if want:'),

 ('audio_master: the configured show length is ignored',
  'ltcplay/clock.py',
  '        if override and c.tc_seconds is not None and show_hour is not None \\',
  '        if False and c.tc_seconds is not None and show_hour is not None \\'),

 ('audio_master: the audio ending before the lights is not the silent tail',
  'ltcplay/clock.py',
  '            self._audio_ended(cue, now)\n',
  '            pass\n'),

 ('audio_master: an Abort in the tail waits for a fade of nothing',
  'ltcplay/clock.py',
  '            if self._paused or self._mode in ("freerun", "wait", "tail") \\',
  '            if self._paused or self._mode in ("freerun", "wait") \\'),

 ('audio_master: a Resume after the audio ended ends the show',
  'ltcplay/clock.py',
  '            if self._frozen_frame is not None and \\\n                    self._frozen_frame + 1 < self._end_frames(cue):',
  '            if False:'),

 ('audio_master: the timecode stops dead at the end of a show again',
  'ltcplay/clock.py',
  '        if (why == "finished" or hold) and self.last_sent is not None:',
  '        if hold and self.last_sent is not None:'),

 ('audio_master: an Abort after the end lets the held frame go again',
  'ltcplay/clock.py',
  '                # frame for MadMapper: an Abort keeps it held.\n                return',
  '                self._drop_held("Abort")\n                return'),

 ('audio_master: Stop leaves the timecode held',
  'ltcplay/clock.py',
  '            self._drop_held("Stop")\n',
  ''),

 ('audio_master: a new cue keeps the last show\'s held frame for its Abort',
  'ltcplay/clock.py',
  '        self._last_send_at = None\n        self._held = None\n        self._held_at = None\n',
  '        self._last_send_at = None\n'),

 ('audio_master: Run says nothing when the audio is shorter than the lights',
  'ltcplay/session.py',
  '            self.notes[:0] = short',
  '            pass'),

 ('madmapper: the heartbeat is read against the configured length only',
  'ltcplay/madmapper.py',
  '        return self._show_len_real if self._show_len_real is not None \\\n            else self.cfg.show_len_s',
  '        return self.cfg.show_len_s'),

 ('madmapper: MadMapper\'s heartbeat track length is never measured',
  'ltcplay/madmapper.py',
  '            self._measure_track(value, now)\n',
  ''),

 ('madmapper: a heartbeat track of the wrong length is never told',
  'ltcplay/madmapperwatch.py',
  '    if got is not None and abs(got - need) > TRACK_TOLERANCE_S:',
  '    if False:'),

 ('madmapper: the rack screen never follows the running show\'s length',
  'ltcplay/madmapperwatch.py',
  '            watchdog.follow_show_length(length)\n',
  ''),

 ('rack screen: the Video lamp is green with a wrong heartbeat track',
  'ltcplay/remote.py',
  '            elif mm.get("heartbeat_track_wrong"):',
  '            elif False:'),

 # Jeff's field report, 2026-10-28 (Abort, Reset, Start now and MadMapper
 # did not follow): the timecode holds for MadMapper after an Abort, the
 # show bank is set following again at every start, a display only alert
 # says when MadMapper is not following, and Reset says how the rig stands.
 ('audio_master: an Abort stops the timecode dead again',
  'ltcplay/clock.py',
  '        if (why == "finished" or hold) and self.last_sent is not None:',
  '        if why == "finished" and self.last_sent is not None:'),

 ('audio_master: an Abort that fades ends without holding',
  'ltcplay/clock.py',
  '        if self._halting and now >= self._halt_end:\n            self._end("stopped", now, hold=True)',
  '        if self._halting and now >= self._halt_end:\n            self._end("stopped", now)'),

 ('audio_master: an Abort before its first frame holds the last show\'s',
  'ltcplay/clock.py',
  '        hold = hold and self._last_frame is not None\n',
  ''),

 ('video: a show start never sets MadMapper\'s show bank following again',
  'ltcplay/fire_ice.py',
  '            follow_show_bank(self.mm, self._note)\n',
  ''),

 ('video: following again sends nothing',
  'ltcplay/fire_ice.py',
  '        mm.play(mm.cfg.show_bank, wait=False)',
  '        pass'),

 ('video: Play from never sets the show bank following again',
  'ltcplay/playhead.py',
  '            follow_show_bank(mm, getattr(cond, "_note", None) or\n                             (lambda *a, **k: None))',
  '            pass'),

 ('madmapper: follow_s in the heartbeat block is ignored',
  'ltcplay/madmapper.py',
  '        follow_s = _positive(doc, "follow_s", where, what, DEFAULT_FOLLOW_S)',
  '        follow_s = DEFAULT_FOLLOW_S'),

 ('video: not following is said at once, without waiting follow_s',
  'ltcplay/madmapperwatch.py',
  '                    now - self._since < follow_s:',
  '                    False:'),

 ('video: the not following alert never clears',
  'ltcplay/madmapperwatch.py',
  '                if following:\n                    self.raised = False\n                    return "clear"',
  '                if False:\n                    return "clear"'),

 ('video: the engine never checks that MadMapper follows',
  'ltcplay/remote.py',
  '        self._follow_work()\n        try:\n            self.alerts.tick()',
  '        try:\n            self.alerts.tick()'),

 ('video: not following is alerted while MadMapper is not running',
  'ltcplay/remote.py',
  '            can = not getattr(wd, "bind_error", None) and link != "lost"',
  '            can = True'),

 ('video: the not following alert stays up once MadMapper follows',
  'ltcplay/remote.py',
  '                    self.alerts.clear(id=aid, who="LTC Player",',
  '                    self.alerts.clear(id=-1, who="LTC Player",'),

 ('video: an acknowledged not following alert never comes back',
  'ltcplay/remote.py',
  '                fc.dropped()\n',
  ''),

 ('reset: Reset says nothing about the rig again',
  'ltcplay/remote.py',
  '                r = self._reset_health(r, who, screen)\n',
  ''),

 ('reset: a lost audio interface reads ready',
  'ltcplay/righealth.py',
  '    if st == "ok":\n        return True, "Audio ready."',
  '    if st in ("ok", "bad"):\n        return True, "Audio ready."'),

 ('reset: MadMapper not running is not said',
  'ltcplay/righealth.py',
  '    if link == "lost":\n        return False, ("MadMapper NOT RUNNING',
  '    if False:\n        return False, ("MadMapper NOT RUNNING'),

 ('reset: an armed flame group is not named',
  'ltcplay/righealth.py',
  '    if armed:\n        return True, (f"Flame link OK',
  '    if False:\n        return True, (f"Flame link OK'),

 ('reset: the frame is said held for MadMapper when nothing takes it',
  'ltcplay/righealth.py',
  '    if clk.get("holding") and held_to:',
  '    if clk.get("holding"):'),

 ('reset: a broken heartbeat listener reads as not seen yet',
  'ltcplay/righealth.py',
  '    if hb == "silent":\n',
  '    if False:\n'),

 ('reset: a laser arm not known reads ready',
  'ltcplay/righealth.py',
  '    if ast == "bad":\n        return False, f"Laser arm NOT READY',
  '    if False:\n        return False, f"Laser arm NOT READY'),

 ('reset: lasers none reads ready',
  'ltcplay/righealth.py',
  '    ok = True if st == "ok" and ast in (None, "ok", "off") else None',
  '    ok = True'),

 ('reset: the rig health never asks where a held frame can go',
  'ltcplay/remote.py',
  '                        clk["held_to"] = [str(lb) for lb, _ip in held()]',
  '                        pass'),

 ('reset: what needs attention is not listed',
  'ltcplay/righealth.py',
  '    if bad:\n        text += f" Needs attention',
  '    if False:\n        text += f" Needs attention'),

 # Safety review of #157.
 ('audio_master: a held end frame goes to receivers not named MadMapper',
  'ltcplay/clock.py',
  '            if "madmapper" not in name or ip in HOLD_NEVER or \\',
  '            if ip in HOLD_NEVER or \\'),

 ('audio_master: a held end frame goes to BEYOND\'s address',
  'ltcplay/clock.py',
  '            if "madmapper" not in name or ip in HOLD_NEVER or \\',
  '            if "madmapper" not in name or \\'),

 ('audio_master: a held end frame goes to an address another receiver shares',
  'ltcplay/clock.py',
  '                    any(o_ip == ip and o_label != label',
  '                    any(False'),

 ('audio_master: a held end frame goes out by broadcast',
  'ltcplay/clock.py',
  '        if self.broadcast:\n            return []\n        out = []',
  '        out = []'),

 ('fire & ice: BEYOND\'s addresses are never kept from the held frame',
  'ltcplay/fire_ice.py',
  '    clock_mod.HOLD_NEVER.update(beyond_addresses(cfg, beyond))',
  '    pass'),

 ('fire & ice: BEYOND\'s OSC host is not one of its addresses',
  'ltcplay/fire_ice.py',
  '              getattr(getattr(beyond, "cfg", None), "host", None)):',
  '              None):'),

 ('audio_master: Run never says the timecode will not hold',
  'ltcplay/clock.py',
  '        if not callable(held) or held():\n            return None',
  '        return None'),

 ('audio_master: any clock.zones.show_len_s is taken, however long',
  'ltcplay/clock.py',
  '        if float(want) > top + SHOW_LEN_OVER_S:',
  '        if False:'),

 ('audio_master: Run never checks clock.zones.show_len_s',
  'ltcplay/clock.py',
  '    check_show_len_cap(cfg, timeline, cues)\n',
  ''),

 ('audio_master: Run says nothing about the LTC track ending with the audio',
  'ltcplay/clock.py',
  '        if ltc is not None:\n            # Safety review of #157, 2',
  '        if False:\n            # Safety review of #157, 2'),

 ('audio_master: Run says nothing about a show running past its lights',
  'ltcplay/clock.py',
  '        if not by_lights and lights > 0:',
  '        if False:'),

 ('fire & ice: a deliberate tail past the lights file is a flame cue fault',
  'ltcplay/fire_ice.py',
  '                if idx >= f.frame_count and _tail_by_design(',
  '                if False and _tail_by_design('),

 ('fire & ice: a render shorter than its audio is no flame cue fault',
  'ltcplay/fire_ice.py',
  '        if not getattr(zones, "show_len_s", None):\n            return False',
  '        pass'),

 ('fire & ice: show_len_s makes the audio running past the render no fault',
  'ltcplay/fire_ice.py',
  '        return show_s > render_s + 1e-6 and show_s > audio_s + 1e-6 and \\',
  '        return show_s > render_s + 1e-6 and \\'),

 ('fire & ice: past the configured end is not a flame cue fault either',
  'ltcplay/fire_ice.py',
  '            rel <= show_s + 1.0 / 30.0',
  '            True'),

 ('audio_master: a Hold at the very end of the audio is lost',
  'ltcplay/clock.py',
  '        if self._pause_req and not self._paused:\n            # A Hold pressed',
  '        if False:\n            # A Hold pressed'),

 ('audio_master: the Run notice can fail Run',
  'ltcplay/session.py',
  '                short = [f"How long the show audio is against the lights "',
  '                raise\n                short = [f"How long the show audio is against the lights "'),

 ('audio_master: an interface that cannot run the show does not stop Run',
  'ltcplay/session.py',
  '            except ValueError as e:\n                self.stop()\n                raise SessionError(str(e))',
  '            except ValueError as e:\n                pass'),

 ('audio_master: the show audio is loaded by every clock',
  'ltcplay/clock.py',
  'from .tc import frames_to_tc, tc_to_frames\n',
  'from .tc import frames_to_tc, tc_to_frames\nfrom . import showaudio as _eager_showaudio  # noqa\n'),

 ('audio_master: artnet_master accepts an audio block',
  'ltcplay/clock.py',
  '        keys = cls.KEYS | {"audio"} if doc.get("source") == "audio_master" \\\n            else cls.KEYS\n',
  '        keys = cls.KEYS | {"audio"}\n'),

 ("audio_master: the page never shows the show audio's faults",
  'ltcplay/display.py',
  '        out.extend(more())\n',
  '        pass\n'),

 ('audio_master: audio that comes back far off the show is slewed for minutes',
  'ltcplay/clock.py',
  '            if self._target is None and abs(e - self._epoch) > self.RESEEK_S:\n',
  '            if False:\n'),


 # -- audio_master, review of PR 26 (2026-09-27): late starts, stale and
 # torn readings, the frozen frame floor, Abort's latency, Hold during a
 # loss, bad WAV sizes, latency spikes, hiccups, the engine's generations,
 # and a main script with no guard.
 ('audio_master: a late cue does not start at 00:00:00:00',
  'ltcplay/clock.py',
  "        if last is None:\n            # Every cue's timecode starts at 00:00:00:00",
  "        if False:\n            # Every cue's timecode starts at 00:00:00:00"),

 ('audio_master: a reading from an older play moves the clock',
  'ltcplay/clock.py',
  "        if self._cue is None or r.token != self._token:\n            return                       # another cue's",
  "        if self._cue is None:\n            return                       # another cue's"),

 ('audio_master: the frozen frame can fall below the last frame sent',
  'ltcplay/clock.py',
  '            frame = max(frame, self._last_frame)\n',
  '            pass\n'),

 ('audio_master: a torn shared-memory read is believed',
  'ltcplay/showaudio.py',
  '            continue                     # a callback wrote meanwhile\n',
  '            pass\n'),

 ('audio_master: Abort stops before its fade has been heard',
  'ltcplay/clock.py',
  '                self._heard_latency() + 0.05\n',
  '                0.0\n'),

 ('audio_master: Hold does nothing while the audio is lost',
  'ltcplay/clock.py',
  '                self._send(("pause", 0, self._token))\n            self._freeze(now)\n',
  '                self._send(("pause", 0, self._token))\n            return\n'),

 ('audio_master: a WAV with a placeholder data size plays',
  'ltcplay/showaudio.py',
  '                if size == 0 or size == 0xFFFFFFFF:\n',
  '                if False:\n'),

 ('audio_master: a WAV cut off short plays',
  'ltcplay/showaudio.py',
  '                if size > left:\n',
  '                if False:\n'),

 ("audio_master: a driver's latency spike moves the clock",
  'ltcplay/clock.py',
  '        if abs(e - ref) <= self.OUTLIER_S:\n',
  '        if True:\n'),

 ('audio_master: a short hiccup is taken for a lost interface',
  'ltcplay/clock.py',
  '    STALL_S = 0.6\n',
  '    STALL_S = 0.3\n'),

 ('audio_master: the same stream playing on is stopped instead of followed',
  'ltcplay/clock.py',
  '        if self._mode == "freerun" and self._stall_loss and \\\n',
  '        if False and self._stall_loss and \\\n'),

 ('audio_master: a stall stops the music straight away',
  'ltcplay/clock.py',
  '        if not stall:\n            self._send(("stop", 0, None))\n',
  '        self._send(("stop", 0, None))\n'),

 ('audio_master: the audio moving for real is never followed',
  'ltcplay/clock.py',
  '        if self._outliers >= self.OUTLIER_RUN:\n            self._resync = True\n',
  '        pass\n'),

 ('audio_master: health stays red after a Resume brings the audio back',
  'ltcplay/clock.py',
  '                self._set_paused(False)\n            self._recovered(now)\n',
  '                self._set_paused(False)\n'),

 ('audio_master: sound that stops by itself is followed quietly',
  'ltcplay/clock.py',
  '                self._stopped_playing(r, now)\n',
  '                pass\n'),

 ('audio_master: errors making the sound never reach the page',
  'ltcplay/clock.py',
  '        if self._render_err_at is not None and \\\n',
  '        if False and \\\n'),

 ('audio_master: Resume at the very end of the audio reads as a dropout',
  'ltcplay/clock.py',
  '        if start >= cue["frames"]:\n',
  '        if False:\n'),

 ("audio_master: Stop waits on the audio's watch thread",
  'ltcplay/showaudio.py',
  '        self._reap(p, c, self.CLOSE_S)\n        self._watch = None\n',
  '        self._reap(p, c, self.CLOSE_S)\n        if self._watch is not None:\n            self._watch.join(2.0)\n        self._watch = None\n'),

 ("audio_master: the audio process re-runs the program's main script",
  'ltcplay/showaudio.py',
  '                main.__dict__.pop("__file__", None)\n                main.__dict__["__spec__"] = None\n',
  '                pass\n'),

 ("the Mac app's boot.py runs the engine without a __main__ guard",
  'Build LTC Player app.command',
  '\nif __name__ == "__main__":\n    # --selfcheck is used',
  '\nif True:\n    # --selfcheck is used'),


 # -- audio_master, Jeff's answers (2026-09-27): no show without its
 # interface, at most 8 outputs and 8 stems, one audio process per user,
 # and liveness from the stream's progress, never from its level.
 ('audio_master: a show starts without its audio interface',
  'ltcplay/clock.py',
  '            if not self._device_ok:\n                # Jeff, 2026-09-27: a show never starts',
  '            if False:\n                # Jeff, 2026-09-27: a show never starts'),

 ('audio_master: a cue may have more than 8 stems',
  'ltcplay/showaudio.py',
  '        if len(stems) > MAX_STEMS:\n',
  '        if False:\n'),

 ('audio_master: the show may use more than 32 outputs',
  'ltcplay/showaudio.py',
  'MAX_OUTPUTS = 32             # outputs',
  'MAX_OUTPUTS = 64             # outputs'),

 # -- audio_master, Jeff (2026-10-20): the show tracks are 19 channels
 # (Music L, C, R and Atmo L, R; Lily, Jack, Narrator, Sound Effects L, C,
 # R; Sound Effects Atmo L, R), with timecode on a 20th output of its own
 # when it is used.
 ('audio_master: 19 show channels and a timecode output are refused (8 outputs)',
  'ltcplay/showaudio.py',
  'MAX_OUTPUTS = 32             # outputs',
  'MAX_OUTPUTS = 8              # outputs'),

 ('audio_master: 19 mono stems and a timecode stem are refused (8 stems)',
  'ltcplay/showaudio.py',
  'MAX_STEMS = 32               # stems',
  'MAX_STEMS = 8                # stems'),

 ("audio_master: a stem's channels follow the output numbers, not the file's order",
  'ltcplay/showaudio.py',
  '                for i, o in enumerate(outs):\n                    out[:k, o]',
  '                for i, o in enumerate(sorted(outs)):\n                    out[:k, o]'),

 ('audio_master: two stems may play on the timecode output',
  'ltcplay/showaudio.py',
  '        if len(on) > 1:\n',
  '        if False:\n'),

 ('audio_master: a silent timecode output is accepted',
  'ltcplay/showaudio.py',
  '        if role == "show" and not on:\n',
  '        if False:\n'),

 ('audio_master: a timecode output past the last output is accepted',
  'ltcplay/showaudio.py',
  '            or not 1 <= ltc <= out_channels:\n',
  '            or not 1 <= ltc:\n'),

 ('audio_master: a mono timecode stem may also play on a program output',
  'ltcplay/showaudio.py',
  '            if ltc is not None and ltc in st.channels and \\\n',
  '            if False and \\\n'),

 ('audio_master: show audio too big for the memory is loaded anyway',
  'ltcplay/showaudio.py',
  '    if total and need > MEMORY_SHARE * total:\n',
  '    if False:\n'),

 ("audio_master: the memory check counts one channel of every stem",
  'ltcplay/showaudio.py',
  '            need += info.frames * info.channels * 4\n',
  '            need += info.frames * 4\n'),

 ('stress kit: the bench track ignores its channel map',
  'packaging/windows/soak.py',
  '    stems = [{"file": "bench.wav", "channels": outs}]\n',
  '    stems = [{"file": "bench.wav", "channels": sorted(outs)}]\n'),

 ('stress kit: the bench channel map may list an output twice',
  'packaging/windows/soak.py',
  '    if len(set(outs)) != len(outs):\n',
  '    if False:\n'),

 ('stress kit: the bench LTC track may sit on a program output',
  'packaging/windows/soak.py',
  '        if ltc_output in outs:\n',
  '        if False:\n'),

 ('audio_master: a second audio process plays alongside the first',
  'ltcplay/showaudio.py',
  '            held = take_lock(spec)\n',
  '            held = type("NoLock", (), {"release": lambda self: None})()\n'),

 ('audio_master: silence in the music is taken for a lost interface',
  'ltcplay/showaudio.py',
  '        try:\n            outdata[:] = m.render(frames)\n        except Exception:\n            self.render_errors += 1\n',
  '        try:\n            outdata[:] = m.render(frames)\n            if outdata.any():\n                self._loud_at = now\n            self.last_cb = getattr(self, "_loud_at", self.last_cb)\n        except Exception:\n            self.render_errors += 1\n'),


 # -- audio_master, Windows bench B23 (the Scarlett): every allowed host
 # API is tried in order until one takes 48 kHz.
 ('audio_master: one host API refusing 48 kHz ends the search (B23)',
  'ltcplay/showaudio.py',
  '            tried.append((label, f"it will not play {channels} output(s) "',
  '            raise Refusal(str(e))\n            tried.append((label, f"it will not play {channels} output(s) "'),

 ('audio_master: WDM-KS is never tried',
  'ltcplay/showaudio.py',
  '    ("Windows WDM-KS", False, "WDM-KS", True),\n',
  ''),

 ('audio_master: an endpoint someone else holds ends the search',
  'ltcplay/showaudio.py',
  '            tried.append((label, f"it would not open ({_clean(e)})"))\n            continue\n',
  '            raise Unavailable(str(e))\n'),

 ('audio_master: the refusal does not say what was tried and why',
  'ltcplay/showaudio.py',
  '    said = "; ".join(f"{label}: {why}" for label, why in tried)\n',
  '    said = "nothing worked"\n'),


 # -- audio_master: loading is all or nothing (Jeff, 2026-09-27).
 ('audio_master: a short read returns a shorter clip',
  'ltcplay/showaudio.py',
  '    if done != info.frames:\n',
  '    if False:\n'),

 ('audio_master: a stem length mismatch is accepted',
  'ltcplay/showaudio.py',
  '                if same_length and len(lengths) > 1:\n',
  '                if False:\n'),

 ('audio_master: a stem shorter than it was when checked is accepted',
  'ltcplay/showaudio.py',
  '                    if pcm.shape[0] != want:\n',
  '                    if False:\n'),

 ('audio_master: a stem with no checked length is accepted',
  'ltcplay/showaudio.py',
  '                    if len(st) <= 3:\n',
  '                    if False:\n'),
 # -- round 1 review of PR 25 -------------------------------------------
 ("a clock jump is not noticed", "ltcplay/schedule_service.py",
  '            if abs(wall_elapsed - perf_elapsed) > CLOCK_JUMP_LIMIT_S:\n',
  '            if False:\n'),

 ("a stale .partial is judged by the date in its name",
  "ltcplay/journal.py",
  '                if name.endswith(".partial"):\n',
  '                if False:\n'),

 ("prune() no longer shares save_incident()'s lock", "ltcplay/journal.py",
  '        removed, problems, stale = [], [], []\n        with self._io:\n',
  '        removed, problems, stale = [], [], []\n'
  '        with threading.Lock():\n'),

 # -- the show conductor (ltcplay/conductor.py). Every name starts with
 # "conductor:" so `mutate.py conductor:` runs just these.
 ("conductor: a step no longer checks it is still the current generation",
  "ltcplay/conductor.py",
  "        with self._lock:\n            self._check(gen)\n"
  "            now = self._applied[output]",
  "        with self._lock:\n            now = self._applied[output]"),

 ("conductor: a wait no longer wakes for a newer press",
  "ltcplay/conductor.py",
  "            while True:\n                self._check(gen)\n"
  "                left = end - self._clock()",
  "            while True:\n                left = end - self._clock()"),

 ("conductor: an announcement plays without checking it was superseded",
  "ltcplay/conductor.py",
  "            self._check(gen)\n            self._announcing = None\n",
  "            self._announcing = None\n"),

 ("conductor: Abort's flame cut waits for the executor",
  'ltcplay/conductor.py',
  '                self._flames_cut()\n'
  '                # And every laser, the same way',
  '                # And every laser, the same way'),

 ("conductor: Abort no longer disarms the flames at once",
  "ltcplay/conductor.py",
  "        self._applied[\"flames\"] = ZERO if r.ok else UNKNOWN\n"
  "        self._disarm(reason)",
  "        self._applied[\"flames\"] = ZERO if r.ok else UNKNOWN"),

 ("conductor: a failed disarm is never sent again",
  "ltcplay/conductor.py",
  "            if not self._applied[\"disarmed\"]:\n                self._disarm()",
  "            if False:\n                self._disarm()"),

 ("conductor: a second Abort starts a second fade",
  "ltcplay/conductor.py",
  "            latched = self._latched\n            if not latched:\n"
  "                # The flames do not wait",
  "            latched = False\n            if not latched:\n"
  "                # The flames do not wait"),

 ("conductor: the Abort latch no longer refuses other presses",
  "ltcplay/conductor.py",
  "        if self._latched:\n            return self._refused(what,",
  "        if False:\n            return self._refused(what,"),

 ("conductor: Reset is taken while the Abort is still fading",
  "ltcplay/conductor.py",
  "                    and self._done_gen != self._gen:\n",
  "                    and False:\n"),

 ("conductor: Abort is taken with nothing playing",
  "ltcplay/conductor.py",
  "                if not self._playing():\n"
  "                    return self._refused(\n"
  "                        \"Abort\", \"nothing is playing",
  "                if False:\n"
  "                    return self._refused(\n"
  "                        \"Abort\", \"nothing is playing"),

 ("conductor: Abort sends no laser blank of its own",
  "ltcplay/conductor.py",
  "            r = self._call(\"lasers blanked\", self.devices.lasers_blank)",
  "            r = done(\"lasers left to the executor\")"),

 ("conductor: Abort never stops the video",
  "ltcplay/conductor.py",
  "        self._step(gen, \"video\", STOPPED, \"video stopped\", progress,\n"
  "                   self.devices.video_stop, force=True)",
  "        pass"),

 ("conductor: the Abort fade is not 1 s",
  "ltcplay/conductor.py",
  "ABORT_FADE_S = 1.0 ",
  "ABORT_FADE_S = 0.5 "),

 ("conductor: the production Hold fade is not 0.25 s",
  "ltcplay/conductor.py",
  "HOLD_FADE_S = 0.25 ",
  "HOLD_FADE_S = 1.0 "),

 ("conductor: an announcement waits no time in the dark",
  "ltcplay/conductor.py",
  "            self._pause(gen, ANNOUNCE_DARK_S)",
  "            self._pause(gen, 0.0)"),

 ("conductor: rehearsal Hold still fades",
  "ltcplay/conductor.py",
  "        return 0.0 if self._mode == REHEARSAL else production_s",
  "        return production_s"),

 ("conductor: an announcement leaves the video and pixels up",
  "ltcplay/conductor.py",
  "        if look == DARK or fade > 0:\n            faded |=",
  "        if fade > 0:\n            faded |="),

 ("conductor: a production Hold leaves the video and pixels up instead of "
  "fading them",
  "ltcplay/conductor.py",
  "        if look == DARK or fade > 0:\n            faded |=",
  "        if look == DARK:\n            faded |="),

 ("conductor: a production Hold and an unfaded Hold both leave the video "
  "and pixels up",
  "ltcplay/conductor.py",
  "        if look == DARK or fade > 0:\n            faded |=",
  "        if False:\n            faded |="),

 ("conductor: a rehearsal Hold fades the video and pixels out instead of "
  "freezing them",
  "ltcplay/conductor.py",
  "        if look == DARK or fade > 0:\n            faded |=",
  "        if look == DARK or fade >= 0:\n            faded |="),

 ("conductor: Hold does not wait for the clock to freeze",
  "ltcplay/conductor.py",
  "lambda: self.show.music_frozen() is True,",
  "lambda: True,"),

 ("conductor: Resume releases lasers and flames before the timecode moves",
  "ltcplay/conductor.py",
  "lambda: self.show.music_frozen() is False,",
  "lambda: True,"),

 ("conductor: a Resume that never sees the timecode move lights the rig "
  "anyway",
  "ltcplay/conductor.py",
  "                return\n            progress.append(\"timecode moving\")",
  "                pass\n            progress.append(\"timecode moving\")"),

 ("conductor: a clock that never freezes is not a fault",
  "ltcplay/conductor.py",
  "timecode may still be moving.\", fault=True,",
  "timecode may still be moving.\", fault=False,"),

 ("conductor: the laser gate is ignored",
  "ltcplay/conductor.py",
  "        if why is not None:\n            self._note(f\"The lasers stay dark",
  "        if False:\n            self._note(f\"The lasers stay dark"),

 ("conductor: a laser gate that raises lets the lasers light",
  "ltcplay/conductor.py",
  "            why = f\"the laser gate failed ({type(e).__name__}: {e})\"",
  "            why = None"),

 ("conductor: an unknown show state lets the lasers light",
  "ltcplay/conductor.py",
  "        if state in LASER_STATES:\n            return None",
  "        if state in LASER_STATES or not state:\n            return None"),

 ("conductor: a show state that cannot be read lets the lasers light",
  "ltcplay/conductor.py",
  "            return (f\"the show state could not be read",
  "            return None\n            return (f\"the show state could not be read"),

 ("conductor: an output that raises stops the effect",
  "ltcplay/conductor.py",
  "        try:\n            r = fn(*args)\n        except Exception as e:\n"
  "            r = failed(f\"{label}: {type(e).__name__}: {e}\")",
  "        r = fn(*args)"),

 ("conductor: an output that returns nothing counts as done",
  "ltcplay/conductor.py",
  "        if not isinstance(r, Result):\n",
  "        if not isinstance(r, Result) and r is not None:\n"
  "            pass\n        elif r is None:\n            r = done()\n"
  "        if False:\n"),

 ("conductor: a failed command is recorded as done",
  "ltcplay/conductor.py",
  "            self._applied[output] = value if r.ok else UNKNOWN",
  "            self._applied[output] = value"),

 ("conductor: a slow output call is not reported",
  "ltcplay/conductor.py",
  "        if took > SLOW_CALL_S:",
  "        if False:"),

 ("conductor: a press does not cancel an announcement that has not started",
  "ltcplay/conductor.py",
  "            self._announcing = None\n        self._gen += 1",
  "        self._gen += 1"),

 ("conductor: a second Hold starts a new effect",
  "ltcplay/conductor.py",
  "            held = self._look in HOLDING_LOOKS\n",
  "            held = False\n"),

 ("conductor: Resume is taken when nothing is held",
  "ltcplay/conductor.py",
  "            if self._look not in HOLDING_LOOKS:\n",
  "            if False:\n"),

 ("conductor: a show start does not mark the music as playing",
  "ltcplay/conductor.py",
  "            self._applied[\"music\"] = MUSIC_PLAYING\n",
  ""),

 ("conductor: a broken journal stops the conductor",
  "ltcplay/conductor.py",
  "        try:\n            self._journal(text, fault=fault, **fields)\n"
  "        except Exception:\n            self.journal_errors += 1",
  "        self._journal(text, fault=fault, **fields)"),

 ("conductor: the stand-in device layer does not say it is one",
  "ltcplay/conductor.py",
  "        if not self.wired:\n",
  "        if False:\n"),

 ("conductor: the flame cues are released before the lasers",
  "ltcplay/conductor.py",
  "        self._restore_lasers(gen, progress)\n"
  "        self._step(gen, \"flames\", LIVE, \"flame cues released\", progress,\n"
  "                   self.show.flames_release)",
  "        self._step(gen, \"flames\", LIVE, \"flame cues released\", progress,\n"
  "                   self.show.flames_release)\n"
  "        self._restore_lasers(gen, progress)"),

 ("conductor: a Hold never zeroes the flame cues",
  "ltcplay/conductor.py",
  "        a = self._applied\n        self._step(gen, \"flames\", ZERO, \"flame cues zeroed\", progress,\n"
  "                   self.show.flames_zero)\n",
  "        a = self._applied\n"),

 ("conductor: a laser gate that says no leaves lit lasers lit",
  "ltcplay/conductor.py",
  "                       outcome=\"refused\")\n"
  "            self._step(gen, \"lasers\", BLACK, \"lasers blanked\", progress,\n"
  "                       self.devices.lasers_blank)\n",
  "                       outcome=\"refused\")\n"),

 ("conductor: leaving the show does not blank the lasers",
  "ltcplay/conductor.py",
  "        self._step(gen, \"lasers\", BLACK, \"lasers blanked for intermission\",\n"
  "                   progress, self.devices.lasers_blank)\n",
  ""),

 ("conductor: intermission cuts an Abort's fade short",
  'ltcplay/conductor.py',
  '            latched = self._latched\n            if not latched:\n                if self._look == STOPPED_DARK:',
  '            latched = False\n            if not latched:\n                if self._look == STOPPED_DARK:'),

 # -- the conductor wired to BEYOND and MadMapper (ConductorDevices, and
 # the lasers-dark re-send that keeps devices.py's "never assume a blank
 # landed" rule). Still "conductor:", so `mutate.py conductor:` runs them.
 ("conductor: lasers already dark are trusted and not blanked again",
  "ltcplay/conductor.py",
  "            if again and not force and (output, value) not in ALWAYS_RESENT:",
  "            if again and not force:"),

 ("conductor: a laser blank re-sent to lasers already dark earns another "
  "0.5 s in the dark",
  "ltcplay/conductor.py",
  "        want[\"changed\"] = any(AGAIN not in p for p in progress)",
  "        want[\"changed\"] = bool(progress)"),

 ("conductor: Abort's laser blank waits for the executor, so the video "
  "fades first (review of PR #29, finding D)",
  "ltcplay/conductor.py",
  "            r = self._call(\"lasers blanked\", self.devices.lasers_blank)",
  "            r = failed(\"left to the executor\")"),

 ("conductor: a Hold fades the music before the lasers go dark",
  "ltcplay/conductor.py",
  "        self._step(gen, \"lasers\", BLACK, \"lasers blanked\", progress,\n"
  "                   self.devices.lasers_blank)\n"
  "        froze = a[\"music\"] in (MUSIC_PLAYING, UNKNOWN)\n"
  "        self._step(gen, \"music\", MUSIC_HELD, \"music fading\", progress,\n"
  "                   self.show.music_hold, fade, only_from=(MUSIC_PLAYING,\n"
  "                                                          UNKNOWN))\n",
  "        froze = a[\"music\"] in (MUSIC_PLAYING, UNKNOWN)\n"
  "        self._step(gen, \"music\", MUSIC_HELD, \"music fading\", progress,\n"
  "                   self.show.music_hold, fade, only_from=(MUSIC_PLAYING,\n"
  "                                                          UNKNOWN))\n"
  "        self._step(gen, \"lasers\", BLACK, \"lasers blanked\", progress,\n"
  "                   self.devices.lasers_blank)\n"),

 ("conductor: the real device layer says it is wired without BEYOND or "
  "MadMapper",
  "ltcplay/conductor.py",
  "        self.wired = madmapper is not None and beyond is not None",
  "        self.wired = True"),

 ("conductor: a BEYOND command that never got out is reported as sent",
  "ltcplay/conductor.py",
  "        if ok is True:\n            return done(f\"{what}: sent to BEYOND.\")",
  "        if True:\n            return done(f\"{what}: sent to BEYOND.\")"),

 ("conductor: a broken BEYOND raises into the conductor",
  "ltcplay/conductor.py",
  "        try:\n"
  "            ok = getattr(self.beyond, method)(show=self.show, **kw)\n"
  "        except Exception as e:\n"
  "            return failed(f\"{what} failed: {type(e).__name__}: {e}. \"\n"
  "                          f\"{failed_means}\")\n",
  "        ok = getattr(self.beyond, method)(show=self.show, **kw)\n"),

 ("conductor: the real device layer's laser blank lights the lasers instead",
  "ltcplay/conductor.py",
  "    def lasers_blank(self):\n"
  "        return self._beyond(\"Laser blank\", \"blank\", self._BLANK_FAILED)",
  "    def lasers_blank(self):\n"
  "        return self._beyond(\"Laser blank\", \"unblank\", self._BLANK_FAILED,\n"
  "                            in_show=True)"),

 ("conductor: a Resume never lights the lasers through the real device layer",
  "ltcplay/conductor.py",
  "        r = self._beyond(\"Laser restore\", \"unblank\",\n"
  "                         \"The lasers stay dark.\", **kw)",
  "        r = self._beyond(\"Laser restore\", \"blank\",\n"
  "                         \"The lasers stay dark.\")"),

 ("conductor: the video fade blocks the conductor until it ends",
  "ltcplay/conductor.py",
  "            self.mm.fade_surfaces(start, end, seconds=seconds, wait=False,\n",
  "            self.mm.fade_surfaces(start, end, seconds=seconds, wait=True,\n"),

 ("conductor: an instant video level does not stop a fade still running",
  "ltcplay/conductor.py",
  "        self.mm.cancel()\n        start = self._from_level(end)\n"
  "        with self._vlock:\n"
  "            self._ramp = (start, end, max(seconds, 0.0), self._clock())\n"
  "        if seconds <= 0 or start == end:\n"
  "            # One level, at once, still cancellable by a newer command.\n"
  "            self.mm.fade_surfaces(end, end, seconds=0.0, steps=1,\n"
  "                                  wait=False, on_done=on_done)\n",
  "        start = self._from_level(end)\n"
  "        with self._vlock:\n"
  "            self._ramp = (start, end, max(seconds, 0.0), self._clock())\n"
  "        if seconds <= 0 or start == end:\n"
  "            self.mm.set_surfaces(end, wait=False)\n"),

 ("conductor: Abort stops the intermission bank instead of the show's",
  "ltcplay/conductor.py",
  "self.mm.stop_bank(self.mm.cfg.show_bank, wait=False,",
  "self.mm.stop_bank(self.mm.cfg.intermission_bank, wait=False,"),

 ("conductor: a closed MadMapper link is reported as sent",
  "ltcplay/conductor.py",
  "        if getattr(self.mm, \"_closed\", False):",
  "        if False:"),

 ("conductor: the video fade to black fades up instead",
  "ltcplay/conductor.py",
  "            lambda cb: self._surfaces(0.0, seconds, cb), BLACK, seconds)",
  "            lambda cb: self._surfaces(1.0, seconds, cb), BLACK, seconds)"),

 # -- the independent review of PR #29 (real UDP, real time probes),
 # findings A to E. Still "conductor:", so `mutate.py conductor` runs them.
 ("conductor: a failed MadMapper send never reaches the conductor "
  "(finding A)",
  "ltcplay/conductor.py",
  "        if not ok:\n            why = why or \"no reason given\"",
  "        if False:\n            why = why or \"no reason given\""),

 ("conductor: a stalled MadMapper sender is never reported (finding A)",
  "ltcplay/conductor.py",
  "            if seq not in self._open:\n                return",
  "            if True:\n                return"),

 ("conductor: a queued video command is recorded as done before MadMapper "
  "sends it (finding A)",
  "ltcplay/conductor.py",
  "                    self._set(output, UNKNOWN)\n"
  "                    self._async_seq[output]",
  "                    self._set(output, value)\n"
  "                    self._async_seq[output]"),

 ("conductor: an older MadMapper command's success overwrites a newer "
  "video record (finding A)",
  "ltcplay/conductor.py",
  "            elif seq is None or seq == self._async_seq.get(output):",
  "            elif True:"),

 ("conductor: the Link's failed send is reported as a success (finding A)",
  "ltcplay/madmapper.py",
  "                    self._tell(on_done, not errors, why)",
  "                    self._tell(on_done, True, why)"),

 ("conductor: web.py builds the MadMapper link with no journal (finding A)",
  "ltcplay/web.py",
  "            madmapper = madmapper_mod.build(\n"
  "                madmapper, journal=_device_journal(httpd_schedule))",
  "            madmapper = madmapper_mod.build(madmapper)"),

 ("conductor: a second Abort sends no blank (finding B)",
  "ltcplay/conductor.py",
  "            return done(f\"Already aborted. {self._reblank('Abort')} Press \"",
  "            return done(f\"Already aborted. Press \""),

 ("conductor: intermission while aborted sends no blank (finding B)",
  "ltcplay/conductor.py",
  "                    f\"Reset. {self._reblank('Intermission')}\")",
  "                    f\"Reset. The rig is already dark.\")"),

 ("conductor: a second Hold sends no blank (finding B)",
  "ltcplay/conductor.py",
  "        return done(f\"Already on hold. {self._reblank('Hold')}\")",
  "        return done(\"Already on hold. Nothing was changed.\")"),

 ("conductor: a re-blank says the lasers are dark whatever happened "
  "(finding B)",
  "ltcplay/conductor.py",
  "        if now == BLACK:\n"
  "            return \"The laser blank was sent again: the lasers are dark.\"",
  "        if True:\n"
  "            return \"The laser blank was sent again: the lasers are dark.\""),

 ("conductor: a video fade starts from a fixed level again (finding C)",
  "ltcplay/conductor.py",
  "        return min(levels) if end <= 0.0 else max(levels)",
  "        return 1.0 if end <= 0.0 else 0.0"),

 ("conductor: Hold does not stop a running video fade at the press "
  "(finding C)",
  "ltcplay/conductor.py",
  "                self._video_cancel()\n"
  "                self._accept(\"Hold\"",
  "                self._accept(\"Hold\""),

 ("conductor: Abort does not stop a running video fade at the press "
  "(finding C)",
  "ltcplay/conductor.py",
  "                self._video_cancel()\n                blanked = threading.Event()",
  "                blanked = threading.Event()"),

 ("conductor: Abort trusts a video record that says black (finding C)",
  "ltcplay/conductor.py",
  "                            self.devices.video_fade_out, fade, force=True)",
  "                            self.devices.video_fade_out, fade)"),

 ("conductor: BEYOND is called with the lock an Abort needs held "
  "(finding D)",
  "ltcplay/conductor.py",
  "UNLOCKED_OUTPUTS = frozenset((\"lasers\",))",
  "UNLOCKED_OUTPUTS = frozenset()"),

 ("conductor: a laser restore ignores the conductor's guard (finding D)",
  "ltcplay/conductor.py",
  "        return (self._restore_gen is not None",
  "        return True or (self._restore_gen is not None"),

 ("conductor: a laser record written outside the lock overwrites a newer "
  "one (finding D)",
  "ltcplay/conductor.py",
  "                if self._ver[output] == ver:\n"
  "                    self._set(output, value if r.ok else UNKNOWN)",
  "                if True:\n"
  "                    self._set(output, value if r.ok else UNKNOWN)"),

 ("conductor: BEYOND's unblank is not stopped before its next packet "
  "(finding D)",
  "ltcplay/beyond.py",
  "                if self._blank_epoch != epoch or \\\n"
  "                        not self._wanted(still_wanted):\n"
  "                    return ok, True",
  "                if False:\n"
  "                    return ok, True"),

 ('conductor: a 100 already on its way out is not followed by a 0 (finding D)',
  'ltcplay/beyond.py',
  '            if late:\n'
  '                # A blank began while this 100',
  '            if False:\n'
  '                # A blank began while this 100'),

 ("conductor: a blank does not stop an unblank on another thread "
  "(finding D)",
  "ltcplay/beyond.py",
  "        with self._lock:\n            self._blank_epoch += 1\n",
  ""),

 ("conductor: the laser gate is asked on the executor again (finding D)",
  "ltcplay/conductor.py",
  "        if not self.threaded:\n            return ask()",
  "        if True:\n            return ask()"),

 ("conductor: the announcement is played on the executor again "
  "(finding D)",
  "ltcplay/conductor.py",
  "        if self.threaded:\n            # announce.play reads",
  "        if False:\n            # announce.play reads"),

 ("conductor: BEYOND takes a host name again (finding D)",
  "ltcplay/beyond.py",
  "        try:\n            ipaddress.IPv4Address(host)\n",
  "        try:\n            pass\n"),

 ("conductor: an announcement after Abort and Reset runs the Abort again "
  "(finding E)",
  "ltcplay/conductor.py",
  "                look = BETWEEN if self._look == ABORTED else self._look",
  "                look = self._look"),

 # -- review round 3 of PR #29: the second independent review's surviving
 # hand mutations, and its two beyond.py fixes.
 ("conductor: the 0 after a late 100 is one packet with no retry again "
  "(round 3)",
  "ltcplay/beyond.py",
  "                self._send_retried(BLANK_VALUE)\n"
  "                return ok, True",
  "                self._send(BRIGHTNESS_ADDR, BLANK_VALUE, force=True)\n"
  "                return ok, True"),

 ("conductor: BEYOND takes an IPv6 address its IPv4 socket cannot reach "
  "(round 3)",
  "ltcplay/beyond.py",
  "            ipaddress.IPv4Address(host)\n",
  "            ipaddress.ip_address(host)\n"),

 ("conductor: an unblank takes a fresh blank count before every packet, "
  "so never sees a blank (round 3)",
  "ltcplay/beyond.py",
  "            with self._lock:\n"
  "                if self._blank_epoch != epoch or \\",
  "            with self._lock:\n"
  "                epoch = self._blank_epoch\n"
  "                if self._blank_epoch != epoch or \\"),

 ("conductor: a blank counts itself only after its packets, so 100s go "
  "out while it is sending (round 3)",
  "ltcplay/beyond.py",
  "        with self._lock:\n            self._blank_epoch += 1\n"
  "        ok = self._send_retried(BLANK_VALUE)\n",
  "        ok = self._send_retried(BLANK_VALUE)\n"
  "        with self._lock:\n            self._blank_epoch += 1\n"),

 ("conductor: a laser gate that never answers counts as a yes (round 3)",
  "ltcplay/conductor.py",
  "        return (f\"the laser gate did not answer within \"",
  "        return None\n"
  "        return (f\"the laser gate did not answer within \""),

 ("conductor: stopping a video fade leaves its success report current, "
  "so a rehearsal Hold records the video lit (round 3)",
  "ltcplay/conductor.py",
  "            self.video_seq += 1      # no older command's success counts "
  "now",
  "            pass"),

 ("conductor: a fade's last value is computed, so a fade to black can end "
  "on 1e-32 instead of 0 (round 3)",
  "ltcplay/madmapper.py",
  "    return [float(start + step * i) for i in range(steps - 1)] + "
  "[float(end)]",
  "    return [float(start + step * i) for i in range(steps)]"),


 # ---------------------------------------------------------------------
 # the arm link (build step 7b, 2026-10-01): the Stream Deck's wire into
 # flamesafe's real ArmInput, and the deck's own pure logic in ltcplay.
 # ---------------------------------------------------------------------

 ("flamesafe: an arm frame with the wrong key is accepted",
  "flamesafe/link.py",
  '    if not isinstance(obj.get("k"), str) or obj.get("k") != key:\n'
  '        raise LinkError("wrong key")\n'
  '    if obj.get("t") != "arm":',
  '    if obj.get("t") != "arm":'),

 ("flamesafe: an arm frame of the wrong contract version is accepted",
  "flamesafe/link.py",
  '    if obj.get("v") != CONTRACT_VERSION:\n'
  '        raise LinkError(f"wrong contract version {obj.get(\'v\')!r}, "\n'
  '                        f"this program speaks {CONTRACT_VERSION}")\n'
  '    if not isinstance(obj.get("k"), str) or obj.get("k") != key:\n'
  '        raise LinkError("wrong key")\n'
  '    if obj.get("t") != "arm":',
  '    if obj.get("t") != "arm":'),

 ("flamesafe: an arm frame with the wrong number of wanted values is accepted",
  "flamesafe/link.py",
  '    wanted = obj.get("wanted")\n'
  '    if not isinstance(wanted, list) or len(wanted) != expect_n \\\n'
  "            or any(not isinstance(w, bool) for w in wanted):",
  '    wanted = obj.get("wanted")\n'
  "    if not isinstance(wanted, list):"),

 ("flamesafe: an arm frame with the wrong number of names is accepted",
  "flamesafe/link.py",
  '    names = obj.get("names")\n'
  '    if not isinstance(names, list) or len(names) != expect_n \\\n'
  "            or any(not isinstance(n, str) for n in names):",
  '    names = obj.get("names")\n'
  "    if not isinstance(names, list):"),

 ("flamesafe: SocketArmInput accepts a frame it could not decode",
  "flamesafe/arminput.py",
  '                    f"arm frame rejected: {msg[:120]} (from {addr[0]}:"\n'
  '                    f"{addr[1]}).")\n'
  "                continue\n",
  '                    f"arm frame rejected: {msg[:120]} (from {addr[0]}:"\n'
  '                    f"{addr[1]}).")\n'
  "                wanted, seq, names = [False] * self._n, 0, None\n"),

 ("flamesafe: SocketArmInput keeps asserting after close()",
  "flamesafe/arminput.py",
  "    def poll(self):\n"
  "        sock = self._sock\n"
  "        if sock is None:\n"
  "            return None",
  "    def poll(self):\n"
  "        sock = self._sock"),

 ("flamesafe: arm_port is allowed to collide with listen_port or status_port",
  "flamesafe/config.py",
  "            if c.link_arm_ip == other_ip and c.link_arm_port == other_port:\n"
  '                raise ConfigError(f"link arm_port is the same as {other_name}; "\n'
  '                                  f"flamesafe would be talking to itself.")',
  "            pass"),

 ("flamesafe: a non-loopback arm_ip is accepted",
  "flamesafe/config.py",
  '        c.link_arm_ip = _ip(link.get("arm_ip", c.link_listen_ip),\n'
  '                            "link arm_ip", loopback_only=True)',
  '        c.link_arm_ip = _ip(link.get("arm_ip", c.link_listen_ip),\n'
  '                            "link arm_ip", loopback_only=False)'),

 # ---------------------------------------------------------------------
 # Safety review of PR #31 (2026-10-01): the arm-link sender lock, the
 # name-mismatch journal lines, and the deck's own journal route.
 # ---------------------------------------------------------------------

 ("flamesafe: a second sender's arm frame is accepted once the first is "
  "locked in",
  "flamesafe/arminput.py",
  """            if self._sender is None:
                self._sender = addr
            elif addr != self._sender:
                # Round 2 of the safety review (item 1): rejected for every
                # purpose EXCEPT disarming -- this sender never becomes the
                # lock holder, never advances seq/names/consent -- but its
                # `wanted` is remembered so a real "disarm" from it still
                # takes effect below, even while a rogue holds the lock.
                self._note_foreign(addr, wanted, now)
                continue
""",
  """            if self._sender is None:
                self._sender = addr
"""),

 ("flamesafe: the arm-link sender lock never releases once stale",
  "flamesafe/arminput.py",
  "        if self._sender is not None and self._sender_at is not None and \\\n"
  "                (now - self._sender_at) * 1000.0 > self._stale_ms:",
  "        if False:"),

 ("flamesafe: a flame-group name mismatch on the arm link is rejected in "
  "total silence again",
  "flamesafe/composer.py",
  """                    self.stats["arm_rejected"] += 1
                    self._name_mismatch_count += 1
                    if not self._name_mismatch_logging:
                        # Round 2 of the safety review, item 10: logged once
                        # per continuous episode, with a running count, not
                        # once per assertion -- at 10 Hz or faster a
                        # misconfigured deck would otherwise flood the
                        # bounded journal queue (1000 lines) within a
                        # couple of minutes, pushing out everything else.
                        self._name_mismatch_logging = True
                        self._event(
                            "arm-link",
                            f"arm assertion rejected: its group names "
                            f"{list(names)!r} do not match this config's "
                            f"{want_names!r}; a deck built against a "
                            f"different group map cannot arm the wrong "
                            f"head. Further rejections for this same "
                            f"reason will not be logged individually "
                            f"until it stops.")
                    return False
""",
  """                    self.stats["arm_rejected"] += 1
                    return False
"""),

 ("flamesafe: service.py drops a raising assert_arm with no journal line",
  "flamesafe/service.py",
  "            self.input_errors += 1\n"
  '            self._event("arm-input", f"assert_arm raised "\n'
  '                                     f"{type(e).__name__}: {e}; this is a "\n'
  '                                     f"bug in the composer, which must "\n'
  '                                     f"never raise here. The assertion "\n'
  '                                     f"was dropped.")',
  "            self.input_errors += 1"),

 ("ltcplay: the deck's own journal route accepts an event with no text",
  "ltcplay/schedule_service.py",
  '        if not text:\n'
  '            raise ValueError("A deck event needs its text. Nothing was "\n'
  '                             "written.")',
  "        if False:\n"
  '            raise ValueError("unreachable")'),

 # ---------------------------------------------------------------------
 # Round 3 of the safety review (2026-10-02): a fourth review re-ran the
 # round 1 and round 2 attacks (still closed) and found round 2's own
 # foreign-disarm fix had opened a new hole (a forced low-then-high edge
 # read as operator consent), plus coverage gaps in the Stream Deck's own
 # safety logic (zero mutations existed for it at all) and in the
 # journal/alarm rate limits. Every name starts with "round3:" so
 # `mutate.py round3:` runs just these.
 # ---------------------------------------------------------------------

 ("round3: a forced low from a foreign sender can still register as "
  "operator consent",
  "flamesafe/composer.py",
  "                self._seen_down[i] = consent_ok and not f[i]\n",
  "                self._seen_down[i] = consent_ok\n"),

 ("round3: SocketArmInput stops marking which bits it forced False",
  "flamesafe/arminput.py",
  "                    forced[i] = True\n                    changed = True\n",
  "                    changed = True\n"),

 ("round3: SocketArmInput.foreign_count always reads zero",
  "flamesafe/arminput.py",
  "        poll() itself had anything new to decode and return.\"\"\"\n"
  "        return len(self._foreign)\n",
  "        poll() itself had anything new to decode and return.\"\"\"\n"
  "        return 0\n"),

 ("round3: the composer's status frame stops carrying foreign_senders",
  "flamesafe/composer.py",
  '            "arm_input": {\n'
  '                "state": ("never" if self._arm_fresh_at is None\n'
  '                          else "live" if live else "stale"),\n'
  '                "seq": self._arm_seq,\n'
  '                "age_ms": arm_age,\n'
  '                "foreign_senders": self._foreign_arm_senders,\n'
  '                "flooded": self._arm_link_flooded,\n'
  '            },\n',
  '            "arm_input": {\n'
  '                "state": ("never" if self._arm_fresh_at is None\n'
  '                          else "live" if live else "stale"),\n'
  '                "seq": self._arm_seq,\n'
  '                "age_ms": arm_age,\n'
  '                "flooded": self._arm_link_flooded,\n'
  '            },\n'),

 ("round3: a sustained foreign-sender flood floods the journal again, one "
  "line per datagram",
  "flamesafe/arminput.py",
  "        ep = self._episodes.get(reason)\n"
  "        if ep is None:\n",
  "        ep = None\n"
  "        if ep is None:\n"),

 ("round3: the deck never alarms on a foreign sender alone",
  "ltcplay/streamdeck.py",
  "        elif isinstance(foreign, int) and not isinstance(foreign, bool) "
  "\\\n                and foreign > 0:\n",
  "        elif False:\n"),

 ("round3: the spoof-alarm dedup compares the changing sentence again, "
  "not a stable category",
  "ltcplay/streamdeck.py",
  "    def _raise_spoof_alarm(self, category, reason):\n"
  "        self._spoof_alarm = reason      # always the LATEST text, for "
  "draw()\n"
  "        if self._spoof_category == category:\n"
  "            return    # already alarming for this exact CATEGORY; no "
  "spam,\n"
  "                      # even though `reason`'s own numbers keep moving\n"
  "        self._spoof_category = category\n",
  "    def _raise_spoof_alarm(self, category, reason):\n"
  "        if self._spoof_alarm == reason:\n"
  "            return\n"
  "        self._spoof_alarm = reason\n"
  "        self._spoof_category = category\n"),

 ("round3: tick() is no longer called from the Stream Deck's main loop",
  "ltcplay/streamdeck.py",
  "                controller.tick()\n"
  "                controller.status.poll(clock)",
  "                controller.status.poll(clock)"),

 ("round3: the arm-hold duration changes without any test noticing",
  "ltcplay/streamdeck.py",
  "ARM_HOLD_S = 0.6",
  "ARM_HOLD_S = 6.0"),

 ("round3: the re-arm refractory window changes without any test noticing",
  "ltcplay/streamdeck.py",
  "REARM_REFRACTORY_S = 2.0",
  "REARM_REFRACTORY_S = 0.02"),

 # ---- round 4 of the safety review (PR #31) ------------------------------
 # A: the deck process keeps the arm link held OFF on ONE socket while no
 # deck is connected.
 ("round4: with no deck the hold-off sends nothing (the link goes silent "
  "and flamesafe's sender lock lapses)",
  "ltcplay/streamdeck.py",
  "        try:\n"
  "            if pacer is not None:\n"
  "                pacer.beat()\n"
  "            else:\n"
  "                controller.arm.send(controller.names)\n"
  "        except Exception:\n"
  "            pass\n"
  "        sleep(1.0 / ARM_SEND_HZ)\n",
  "        sleep(1.0 / ARM_SEND_HZ)\n"),

 ("round4: the deck process sends nothing until the first deck is found",
  "ltcplay/streamdeck.py",
  "            if not controller.arm.is_open:\n"
  "                controller.arm.open()\n"
  "        except Exception:\n"
  "            pass\n"
  "        try:\n"
  "            controller.screen_pass()\n",
  "            if not controller.arm.is_open:\n"
  "                pass\n"
  "        except Exception:\n"
  "            pass\n"
  "        try:\n"
  "            controller.screen_pass()\n"),

 ("round4: a reconnect closes and reopens the arm socket (a new source "
  "port, so the sender lock changes hands)",
  "ltcplay/streamdeck.py",
  "            if not controller.arm.is_open:\n"
  "                controller.arm.open()\n"
  "            controller.reset_on_reconnect()\n",
  "            controller.arm.close()\n"
  "            controller.arm.open()\n"
  "            controller.reset_on_reconnect()\n"),

 ("round4: an unplug closes the arm socket again",
  "ltcplay/streamdeck.py",
  "            controller.reset_on_reconnect()\n"
  "            try:\n"
  "                controller.arm.send(controller.names)\n",
  "            controller.arm.close()\n"
  "            controller.reset_on_reconnect()\n"
  "            try:\n"
  "                controller.arm.send(controller.names)\n"),

 # ---- degraded mode, Phase 0 (Jeff's rulings 2026-10-07 and 2026-10-12) --
 # Ruling 4: a deck unplug and replug keeps the arm state.
 ("an unplugged deck sends every group OFF again", "ltcplay/streamdeck.py",
  "        try:\n"
  "            if pacer is not None:\n"
  "                pacer.beat()\n"
  "            else:\n"
  "                controller.arm.send(controller.names)\n"
  "        except Exception:\n"
  "            pass\n"
  "        sleep(1.0 / ARM_SEND_HZ)\n",
  "        try:\n"
  "            controller.arm.set_all(False)\n"
  "            if pacer is not None:\n"
  "                pacer.beat()\n"
  "            else:\n"
  "                controller.arm.send(controller.names)\n"
  "        except Exception:\n"
  "            pass\n"
  "        sleep(1.0 / ARM_SEND_HZ)\n"),

 ("an unplug sends every group OFF before the hold-off",
  "ltcplay/streamdeck.py",
  "            controller.reset_on_reconnect()\n"
  "            try:\n"
  "                controller.arm.send(controller.names)\n",
  "            controller.reset_on_reconnect()\n"
  "            try:\n"
  "                controller.arm.set_all(False)\n"
  "                controller.arm.send(controller.names)\n"),

 ("a hardware replug restarts the arm counter", "ltcplay/streamdeck.py",
  "            if not controller.arm.is_open:\n"
  "                controller.arm.open()\n"
  "            controller.reset_on_reconnect()\n",
  "            controller.arm.restart()\n"
  "            controller.reset_on_reconnect()\n"),

 ("a hardware replug forgets what was armed", "ltcplay/streamdeck.py",
  "            if not controller.arm.is_open:\n"
  "                controller.arm.open()\n"
  "            controller.reset_on_reconnect()\n",
  "            if not controller.arm.is_open:\n"
  "                controller.arm.open()\n"
  "            controller.arm.set_all(False)\n"
  "            controller.reset_on_reconnect()\n"),

 ("an unplug drops no hold in progress", "ltcplay/streamdeck.py",
  "            controller.reset_on_reconnect()\n"
  "            try:\n"
  "                controller.arm.send(controller.names)\n",
  "            pass\n"
  "            try:\n"
  "                controller.arm.send(controller.names)\n"),

 ("an exception from the screen pass stops the no-deck sender",
  "ltcplay/streamdeck.py",
  "        except Exception as e:\n"
  "            if outage is not None:\n"
  "                outage.screen_failed(e)\n"
  "        try:\n"
  "            if pacer is not None:\n"
  "                pacer.beat()\n"
  "            else:\n"
  "                controller.arm.send(controller.names)\n"
  "        except Exception:\n"
  "            pass\n",
  "            if pacer is not None:\n"
  "                pacer.beat()\n"
  "            else:\n"
  "                controller.arm.send(controller.names)\n"
  "        except Exception as e:\n"
  "            if outage is not None:\n"
  "                outage.screen_failed(e)\n"),

 ("a screen pass that raises is never journaled while the deck is away",
  "ltcplay/streamdeck.py",
  "            if outage is not None:\n"
  "                outage.screen_failed(e)\n",
  "            pass\n"),

 ("a lost alert post is never tried again", "ltcplay/streamdeck.py",
  "        if text == self._alert_text and self._alert_landed():\n",
  "        if text == self._alert_text:\n"),

 ("the engine's answer to an alert post is never recorded",
  "ltcplay/streamdeck.py",
  "        elif \"raise\" in body:\n"
  "            self.alert_delivered = bool(ok)\n",
  "        elif \"raise\" in body:\n"
  "            self.alert_delivered = True\n"),

 ("the ACK key hides an engine fault", "ltcplay/streamdeck.py",
  "        elif engine_fault:\n"
  "            # Fix round 2, E: the engine did not take a press (or cannot be\n",
  "        elif engine_fault and not self.alert_pending():\n"
  "            # Fix round 2, E: the engine did not take a press (or cannot be\n"),

 ("the pulse range is not checked at startup", "ltcplay/fire_ice.py",
  "            refuse_alert_pixels_on_flame_channels(p, cfg.flame_controller,\n"
  "                                                  alert_pixels)\n",
  "            pass\n"),

 ("the outage loop stops reading the screen", "ltcplay/streamdeck.py",
  "            controller.screen_pass()\n"
  "            controller.tick()\n"
  "        except Exception as e:\n"
  "            if outage is not None:\n",
  "            pass\n"
  "        except Exception as e:\n"
  "            if outage is not None:\n"),

 ("the deck raises no alert when the hardware is lost",
  "ltcplay/streamdeck.py",
  "            self._post_alert(DECK_LOST_TEXT)\n",
  "            pass\n"),

 ("a deck that never connected raises no alert", "ltcplay/streamdeck.py",
  "            self._post_alert(self._never_text)\n",
  "            pass\n"),

 ("a replug clears the deck alert without an Acknowledge",
  "ltcplay/streamdeck.py",
  "            self._post_alert(DECK_BACK_TEXT)\n",
  "            self._alert({\"clear\": \"deck\"})\n"),

 # Ruling 2: the key that acknowledges visibly changes.
 ("a deck key no longer acknowledges a pending alert", "ltcplay/streamdeck.py",
  "        if pending and edges(self._prev_keys, down):\n"
  "            self._do_acknowledge(pending)\n",
  "        if False:\n"
  "            self._do_acknowledge(pending)\n"),

 ("the Start key still starts while an alert is open", "ltcplay/streamdeck.py",
  "            if k == TOP_START:\n"
  "                if pending:\n",
  "            if k == TOP_START:\n"
  "                if False:\n"),

 ("the ACK key never changes while an alert is open", "ltcplay/streamdeck.py",
  "        elif self.alert_pending():\n"
  "            # Jeff's ruling 2 (2026-10-12)",
  "        elif False:\n"
  "            # Jeff's ruling 2 (2026-10-12)"),

 ("the ACK key reuses the Abort look", "ltcplay/streamdeck.py",
  "ACK_ON = ((36, 110, 230), (245, 248, 255))",
  "ACK_ON = (RED, CHAMPAGNE)"),

 ("a stale engine answer keeps the ACK key lit", "ltcplay/streamdeck.py",
  "        if last is None or self._clock() - last[1] > SCREEN_STALE_S:\n"
  "            return ()\n",
  "        if last is None:\n"
  "            return ()\n"),

 # The alert service.
 ("an alert re-sounds on every raise", "ltcplay/alerts.py",
  "        if last is not None and now - last < self.resound_s():",
  "        if False:"),

 ("an alert clear is journaled as a fault", "ltcplay/alerts.py",
  "                          fault=False, action=\"alert\", outcome=\"cleared\",",
  "                          fault=True, action=\"alert\", outcome=\"cleared\","),

 ("an alert raise is not journaled as a fault", "ltcplay/alerts.py",
  "        self._journal(f\"Alert ({a.severity}, {kind}): {text}\", fault=True,",
  "        self._journal(f\"Alert ({a.severity}, {kind}): {text}\", fault=False,"),

 ("a second alert of the same kind opens a second banner", "ltcplay/alerts.py",
  "        a = self._open.get(kind)\n        if a is not None:",
  "        a = None\n        if a is not None:"),

 ("Stop clears an alert like Acknowledge", "ltcplay/alerts.py",
  "        if action not in CLEARING:",
  "        if False:"),

 ("the pulse runs during a show for a video alert", "ltcplay/alerts.py",
  "            if live and a.kind not in PULSE_DURING_SHOW:\n"
  "                mine = False",
  "            if False:\n"
  "                mine = False"),

 ("the pulse ignores a booth note that could not play", "ltcplay/alerts.py",
  "            mine = a.kind in PULSE_DURING_SHOW or a.booth_ok is False",
  "            mine = a.kind in PULSE_DURING_SHOW"),

 ("an alert is raised from the network", "ltcplay/remote.py",
  "            if not ctx.local:\n"
  "                return 403, {\"error\": \"Alerts are raised on the show \"",
  "            if False:\n"
  "                return 403, {\"error\": \"Alerts are raised on the show \""),

 # The booth note.
 ("the booth note goes to the show's main outputs", "ltcplay/boothaudio.py",
  "        block[:, a - 1] = left\n        block[:, b - 1] = right",
  "        block[:] = left[:, None]"),

 ("the booth player falls back to the default device",
  "ltcplay/boothaudio.py",
  "        except ValueError as e:\n"
  "            # Missing device: silent. Nothing else is ever used instead.\n"
  "            return False, f\"Booth speaker {name} is not attached. {e}\"",
  "        except ValueError as e:\n"
  "            dev = {\"index\": 0, \"name\": name, \"channels\": 64}"),

 # The booth notes by name (Jeff, 2026-10-28).
 ("booth notes: a note is never looked for by its name",
  "ltcplay/boothaudio.py",
  "    for d in folders or ():\n        p = os.path.join(d, fn)",
  "    for d in ():\n        p = os.path.join(d, fn)"),

 ("booth notes: the shipped note wins over a person's own copy",
  "ltcplay/boothaudio.py",
  "    if settings_folder:\n        out.append(os.path.join(settings_folder, FOLDER))\n"
  "    out.append(shipped_folder())",
  "    out.append(shipped_folder())\n    if settings_folder:\n"
  "        out.append(os.path.join(settings_folder, FOLDER))"),

 ("booth notes: an explicit files entry no longer wins",
  "ltcplay/boothaudio.py",
  "    explicit = (files or {}).get(name)\n    if explicit:\n        return explicit",
  "    explicit = (files or {}).get(name)\n    if False:\n        return explicit"),

 ("booth notes: a missing explicit file falls back to the shipped note",
  "ltcplay/boothaudio.py",
  "    explicit = (files or {}).get(name)\n    if explicit:\n        return explicit",
  "    explicit = (files or {}).get(name)\n"
  "    if explicit and os.path.isfile(explicit):\n        return explicit"),

 ("booth notes: lasers and users stay silent",
  "ltcplay/boothaudio.py",
  "              \"timecode\", \"test\", \"lasers\", \"users\")",
  "              \"timecode\", \"test\")"),

 ("booth notes: any name plays whatever file is there",
  "ltcplay/boothaudio.py",
  "        if kind not in NOTE_NAMES:\n            return False, (f\"{kind} has no booth note",
  "        if False:\n            return False, (f\"{kind} has no booth note"),

 ("booth notes: two notes play at once",
  "ltcplay/boothaudio.py",
  "            self._queue.put((sd, dev, block, rate))\n            if self._worker is None:",
  "            threading.Thread(target=self._write, args=(sd, dev, block, rate),\n"
  "                             daemon=True).start()\n            if False:"),

 ("booth notes: the rack screen counts a missing note as found",
  "ltcplay/boothaudio.py",
  "        if p and os.path.isfile(p):\n            found += 1",
  "        if True:\n            found += 1"),

 ("booth notes: the status carries no note count",
  "ltcplay/remote.py",
  "                        \"notes\": self._booth_notes()}",
  "                        \"notes\": None}"),

 ("booth notes: the rack screen line never says the count",
  "ltcplay/web/remote.html",
  "  device += boothNotesText(b.notes);\n",
  ""),

 ("booth notes: the showtime hold plays no held note",
  "ltcplay/alerthold.py",
  "                play_note(\"held\")",
  "                pass"),

 ("booth notes: an update leaves old notes behind",
  "packaging/windows/installer.iss",
  "Type: filesandordirs; Name: \"{app}\\Booth notes\"\n",
  ""),

 ("booth notes checker: a missing note never fails",
  "packaging/windows/check_booth_notes.py",
  "    if require and (r[\"missing\"] or r[\"extra\"]):\n        failed = True",
  "    if require and r[\"extra\"]:\n        failed = True"),

 ("booth notes checker: a missing note fails before it is required",
  "packaging/windows/check_booth_notes.py",
  "    if require and (r[\"missing\"] or r[\"extra\"]):\n        failed = True",
  "    if r[\"missing\"] or r[\"extra\"]:\n        failed = True"),

 ("booth notes checker: a too long note passes",
  "packaging/windows/check_booth_notes.py",
  "        if length > BA.MAX_NOTE_S:",
  "        if False:"),

 # The blue pulse.
 ("the pulse overlaps the flame controller's channels", "ltcplay/fire_ice.py",
  "    if lo <= f_hi and f_lo <= hi:",
  "    if False:"),

 ("the pulse paints over a blackout", "ltcplay/player.py",
  "        if self.override == \"blackout\":\n            return frame",
  "        if False:\n            return frame"),

 ("the pulse is a square wave, not a fade", "ltcplay/player.py",
  "    up = phase * 2.0 if phase < 0.5 else (1.0 - phase) * 2.0",
  "    up = 1.0 if phase < 0.5 else 0.0"),

 ("the pulse paints the whole frame, not the floor range", "ltcplay/player.py",
  "        start, count, order = self.alert_pixels",
  "        start, count, order = 0, len(self._buf) // 3, self.alert_pixels[2]"),

 ("the output loop sends the frame without the pulse", "ltcplay/player.py",
  "                frame = self._alert_mix(self._tick())",
  "                frame = self._tick()"),

 # B: no consent while anyone else is on the link.
 ('round4: consent ignores another sender on the arm link',
  'flamesafe/composer.py',
  '        disturbed = (self._foreign_arm_senders != 0\n                     or self._arm_link_flooded\n',
  '        disturbed = (self._arm_link_flooded\n'),

 ('round4: consent ignores a flood on the arm link',
  'flamesafe/composer.py',
  '        disturbed = (self._foreign_arm_senders != 0\n                     or self._arm_link_flooded\n',
  '        disturbed = (self._foreign_arm_senders != 0\n'),

 ("round4: a down edge from before another sender turned up can be "
  "finished while it is there",
  "flamesafe/composer.py",
  "            # has gone (the deck keeps re-asserting its own False, so a\n"
  "            # genuine low is re-proved on the first frame after it goes).\n"
  "            self._seen_down = [False] * self.n\n",
  "            # has gone (the deck keeps re-asserting its own False, so a\n"
  "            # genuine low is re-proved on the first frame after it goes).\n"
  "            pass\n"),

 ("round4: a change of locked sender keeps the old sender's down edges",
  "flamesafe/composer.py",
  "        elif sender is not None and self._arm_sender is not None and \\\n"
  "                sender != self._arm_sender:\n",
  "        elif False:\n"),

 ("round4: SocketArmInput never reports a flood",
  "flamesafe/arminput.py",
  "        if n_read > FLOOD_DATAGRAMS_PER_POLL or \\\n"
  "                n_bytes > self.flood_bytes:\n",
  "        if False:\n"),

 ("round4: SocketArmInput stops naming the locked sender",
  "flamesafe/arminput.py",
  "            best = ArmAssertion(wanted, seq, names, sender=addr)\n",
  "            best = ArmAssertion(wanted, seq, names)\n"),

 ("round4: the service never hands the flood flag to the composer",
  "flamesafe/service.py",
  "            self.composer.note_arm_link_flooded(\n"
  "                bool(getattr(self.arm_input, \"flooded\", False)))\n",
  "            self.composer.note_arm_link_flooded(False)\n"),

 ("round4: the deck has no label for the OTHER SENDER reason",
  "ltcplay/streamdeck.py",
  "    \"stops. Cycle the arm again once it has gone.\": \"OTHER SENDER\",\n",
  "    \"stops. Cycle the arm again once it has gone.x\": \"OTHER SENDER\",\n"),

 # C: decode rejections throttled like every other arm-link rejection.
 ("round4: decode rejections are journaled one line per datagram again",
  "flamesafe/arminput.py",
  "                msg = str(e)\n"
  "                self._rejects.note(\n",
  "                msg = str(e)\n"
  "                self._event(\"arm-link\", f\"arm frame rejected: {msg}\")\n"
  "                (lambda *a: None)(\n"),

 ("round4: a decode rejection is throttled under its raw message, so a "
  "sender varying it gets a line every datagram",
  "flamesafe/arminput.py",
  "                    \"decode:\" + _decode_reason(msg), now, addr,\n",
  "                    \"decode:\" + msg, now, addr,\n"),

 ("round4: an arm-link rejection episode ends after half a second again",
  "flamesafe/arminput.py",
  "EPISODE_QUIET_S = 5.0\n",
  "EPISODE_QUIET_S = 0.5\n"),

 ("round4: no per-minute cap on arm-link rejection lines",
  "flamesafe/arminput.py",
  "LINES_PER_MINUTE = 4\n",
  "LINES_PER_MINUTE = 10 ** 6\n"),

 # D: the round-4 review's hand mutations that survived the whole suite.
 ("round4: any forced bit blocks consent on every group",
  "flamesafe/composer.py",
  "                self._seen_down[i] = consent_ok and not f[i]\n",
  "                self._seen_down[i] = consent_ok and not any(f)\n"),

 ("round4: the locked sender's own genuine lows are marked forced",
  "flamesafe/arminput.py",
  "                if not fw[i] and wanted[i]:\n"
  "                    wanted[i] = False\n"
  "                    forced[i] = True\n",
  "                if not fw[i]:\n"
  "                    wanted[i] = False\n"
  "                    forced[i] = True\n"),

 ("round4: the foreign-sender alarm's dedup key embeds the sender count",
  "ltcplay/streamdeck.py",
  "            category = \"foreign-senders\"\n",
  "            category = f\"foreign-senders:{foreign}\"\n"),

 ("round4: a forced low keeps an earlier genuine down edge",
  "flamesafe/composer.py",
  "                self._seen_down[i] = consent_ok and not f[i]\n",
  "                self._seen_down[i] = (consent_ok and not f[i]) or "
  "(f[i] and self._seen_down[i])\n"),

 ("round4: a malformed forced vector is accepted",
  "flamesafe/composer.py",
  "                if len(f) != self.n or any(not isinstance(x, bool) for x "
  "in f):\n                    raise ValueError(\"forced\")\n",
  "                pass\n"),

 ("round4: the foreign-sender count only reaches the composer on ticks "
  "with an assertion",
  "flamesafe/service.py",
  "            self.composer.note_foreign_arm_senders(\n"
  "                getattr(self.arm_input, \"foreign_count\", 0))\n",
  "            a is not None and self.composer.note_foreign_arm_senders(\n"
  "                getattr(self.arm_input, \"foreign_count\", 0))\n"),

 ("round4: an arm-link rejection episode never closes",
  "flamesafe/arminput.py",
  "                       if now - e[\"at\"] > self.quiet_s]:\n",
  "                       if False]:\n"),

 # E: a freshly started deck process gets the reconnect grace.
 ("round4: a freshly started deck process gets no reconnect grace",
  "ltcplay/streamdeck.py",
  "        self._spoof_last_seq = None\n",
  "        self._spoof_last_seq = 0\n"),

 # -- the flame link: flamesafe's disarm_all (2026-10-02) ----------------
 ('flamelink: a disarm_all from another sender is accepted',
  'flamesafe/composer.py',
  '            if sender != self._frame_sender:\n                # A keyed disarm_all from a second sender is a second\n                # sender on the link (second-copy guard, 2026-10-03).\n                self._second_sender(sender, t)\n                raise ValueError("another sender")\n',
  ''),

 ("flamelink: a disarm_all is accepted with no live flame link",
  "flamesafe/composer.py",
  '''            if not self._frame_is_fresh(t):
                raise ValueError("no live flame link to accept it from")''',
  '''            if False:
                raise ValueError("no live flame link to accept it from")'''),

 ("flamelink: a disarm_all out of sequence is accepted",
  "flamesafe/composer.py",
  '''            if msg.seq <= self._frame_seq:
                raise ValueError(f"out of order: seq {msg.seq} after "''',
  '''            if False:
                raise ValueError(f"out of order: seq {msg.seq} after "'''),

 ("flamelink: a disarm_all whose sender clock went backwards is accepted",
  "flamesafe/composer.py",
  '''            if msg.mono < self._frame_mono:
                raise ValueError("sender clock went backwards")
        except Exception as e:                          # noqa: BLE001
            self.stats["disarm_all_rejected"] += 1''',
  '''            pass
        except Exception as e:                          # noqa: BLE001
            self.stats["disarm_all_rejected"] += 1'''),

 ("flamelink: disarm_all leaves the latches standing",
  "flamesafe/composer.py",
  '''        self._latched = [False] * self.n
        self._seen_down = [False] * self.n
        self.stats["disarm_all"] += 1''',
  '''        self._seen_down = [False] * self.n
        self.stats["disarm_all"] += 1'''),

 ("flamelink: disarm_all ARMS every group",
  "flamesafe/composer.py",
  '''        self._latched = [False] * self.n
        self._seen_down = [False] * self.n
        self.stats["disarm_all"] += 1''',
  '''        self._latched = [True] * self.n
        self._seen_down = [False] * self.n
        self.stats["disarm_all"] += 1'''),

 ("flamelink: a consent edge from before the Abort survives it",
  "flamesafe/composer.py",
  '''        self._latched = [False] * self.n
        self._seen_down = [False] * self.n
        self.stats["disarm_all"] += 1''',
  '''        self._latched = [False] * self.n
        self.stats["disarm_all"] += 1'''),

 ("flamelink: the lamp never says the show's Abort disarmed it",
  "flamesafe/composer.py",
  '''            if self._aborted[i]:
                return (ABORT_DISARMED, "flashing")''',
  '''            if False:
                return (ABORT_DISARMED, "flashing")'''),

 ("flamelink: a re-armed group still blames the old Abort",
  "flamesafe/composer.py",
  '''                self._latched[i] = True
                self._aborted[i] = False''',
  '''                self._latched[i] = True'''),

 ("flamelink: every copy of one Abort is journaled",
  "flamesafe/composer.py",
  "        new_abort = key != self._disarm_last_key",
  "        new_abort = True"),

 # -- fix round 1 of PR #34: flamesafe's side ------------------------------
 ("fix1: a new sender's Abort with a repeated id is not journaled",
  "flamesafe/composer.py",
  "        key = (msg.abort_id, sender)",
  "        key = (msg.abort_id, None)"),

 ("fix1: an arm-hold begun before the Abort can complete after it",
  "flamesafe/composer.py",
  "                if in_abort_window and self._low_predates_abort[i]:",
  "                if False and self._low_predates_abort[i]:"),

 ("fix1: disarm_all does not mark the lows going on at the Abort",
  "flamesafe/composer.py",
  "        self._low_predates_abort = [True] * self.n",
  "        self._low_predates_abort = [False] * self.n"),

 ("fix1: the post-Abort window never ends",
  "flamesafe/composer.py",
  "            and (t - self._disarm_at) * 1000.0 < self.cfg.min_arm_dwell_ms)",
  "            and (t - self._disarm_at) * 1000.0 < 10 ** 9)"),

 ("fix1: a fresh low after the Abort is still treated as an old one",
  "flamesafe/composer.py",
  "                        self._low_predates_abort[i] = False",
  "                        pass"),

 ("fix1 H1: disarm_all does not advance the sequence",
  "flamesafe/composer.py",
  "        self._frame_seq = msg.seq\n        self._frame_mono = msg.mono\n"
  "        was_up",
  "        was_up"),

 ("fix1 H2: a disarm_all with the same seq as the last frame is taken",
  "flamesafe/composer.py",
  "            if msg.seq <= self._frame_seq:\n"
  "                raise ValueError(f\"out of order: seq {msg.seq} after \"",
  "            if msg.seq < self._frame_seq:\n"
  "                raise ValueError(f\"out of order: seq {msg.seq} after \""),

 ("fix1 H3: disarm_all refreshes the flame link's liveness",
  "flamesafe/composer.py",
  "        self._disarm_at = t\n",
  "        self._disarm_at = t\n        self._frame_at = t\n"),

 ("fix1: flamesafe takes a timecode with a trailing newline",
  "flamesafe/link.py",
  "isinstance(tc, str) and _TC.fullmatch(tc)):",
  "isinstance(tc, str) and re.match(r\"^\\d{2}:\\d{2}:\\d{2}[:;]\\d{2}$\", tc)):"),

 ("fix1: flamesafe takes a key with a trailing newline",
  "flamesafe/link.py",
  "            and bool(_KEY.fullmatch(key)))",
  "            and bool(re.match(r\"^[\\x21-\\x7e]+$\", key)))"),

 ("flamelink: the service drops every disarm_all on the floor",
  "flamesafe/service.py",
  "                self.composer.disarm_all(msg, sender=tuple(addr[:2]))",
  "                pass"),

 ("flamelink: a disarm_all is decoded as a flame frame",
  "flamesafe/link.py",
  '''    if isinstance(obj, dict) and obj.get("t") == "disarm_all":''',
  '''    if isinstance(obj, dict) and obj.get("t") == "disarm-all":'''),

 ("flamelink: a disarm_all with the wrong key is decoded",
  "flamesafe/link.py",
  '''        raise LinkError("wrong key")
    if obj.get("t") != "disarm_all":''',
  '''        pass
    if obj.get("t") != "disarm_all":'''),

 ("flamelink: a disarm_all with extra fields is decoded",
  "flamesafe/link.py",
  '''    if extra:
        raise LinkError("disarm_all has a field this contract does not "''',
  '''    if False:
        raise LinkError("disarm_all has a field this contract does not "'''),

 ("flamelink: a disarm_all with abort id 0 is decoded",
  "flamesafe/link.py",
  "    if not _is_int(abort_id) or abort_id < 1:",
  "    if not _is_int(abort_id) or abort_id < 0:"),

 ("flamelink: rejections are keyed by the raw message, so a sender varying it floods the journal",
  "flamesafe/composer.py",
  "            reason = _flame_reason(why)",
  "            reason = why"),

 ("flamelink: an episode of rejections never says how many",
  "flamesafe/composer.py",
  "            self._rejects.sweep(",
  "            (lambda *a, **k: None)("),

 ("flamelink: an undecodable datagram is not journaled",
  "flamesafe/composer.py",
  '''        self._last_reject = str(why)
        self._note_reject(self._last_reject, sender)''',
  '''        self._last_reject = str(why)'''),

 ("flamelink: a refused flame frame is not journaled",
  "flamesafe/composer.py",
  '''            self._last_reject = str(e) or type(e).__name__
            self._note_reject(self._last_reject, sender)''',
  '''            self._last_reject = str(e) or type(e).__name__'''),

 ("flamelink: a sender's long text goes into the journal uncut",
  "flamesafe/composer.py",
  "            if len(why) > 200:          # sender-chosen text, kept short",
  "            if False:"),

 # -- the flame link: ltcplay's sender -----------------------------------
 ("flamelink: a new link starts with its cues released",
  "ltcplay/flamelink.py",
  "        self.zeroed = True          # until the conductor releases the cues",
  "        self.zeroed = False"),

 ("flamelink: cues go out while the show is held or stopped",
  "ltcplay/flamelink.py",
  "        if live is not True:\n"
  "            # No show cue playing",
  "        if False:\n"
  "            # No show cue playing"),

 ("flamelink: cues go out after the conductor zeroed them",
  "ltcplay/flamelink.py",
  "        if self.zeroed or tc is None or land:",
  "        if tc is None or land:"),

 ("flamelink: a provider's wrong-sized or out-of-range answer goes out",
  "ltcplay/flamelink.py",
  '''            if len(vals) != UNIVERSE_SIZE or \\
                    any(not _is_int(x) or not 0 <= x <= 255 for x in vals):
                raise ValueError''',
  '''            if False:
                raise ValueError'''),

 ("flamelink: zero() waits for the next tick instead of sending at once",
  "ltcplay/flamelink.py",
  '''            self._zero_gen += 1
            return self._send_zero_frame()''',
  '''            self._zero_gen += 1
            return self._sock is not None'''),

 ("flamelink: disarm_all leaves the cues released",
  "ltcplay/flamelink.py",
  '''                self.zeroed = True
                self._zero_gen += 1
                self._send_zero_frame()
                self.abort_id += 1''',
  '''                self._zero_gen += 1
                self._send_zero_frame()
                self.abort_id += 1'''),

 # -- fix round 1 of PR #34: ltcplay's sender -----------------------------
 ("fix1 H10: disarm_all's immediate frame is built from the providers before zeroing",
  "ltcplay/flamelink.py",
  '''                self.zeroed = True
                self._zero_gen += 1
                self._send_zero_frame()
                self.abort_id += 1''',
  '''                self.send_frame()
                self.zeroed = True
                self._zero_gen += 1
                self.abort_id += 1'''),

 ("fix1 H11: zero()'s immediate frame is built from the providers before zeroing",
  "ltcplay/flamelink.py",
  '''            self.zeroed = True
            self._zero_gen += 1
            return self._send_zero_frame()''',
  '''            ok = self.send_frame()
            self.zeroed = True
            self._zero_gen += 1
            return ok'''),

 ("fix1: zero() asks the providers for its frame",
  "ltcplay/flamelink.py",
  '''            self._zero_gen += 1
            return self._send_zero_frame()''',
  '''            self._zero_gen += 1
            return self.send_frame()'''),

 ("fix1: the sender asks the providers under the link's lock",
  "ltcplay/flamelink.py",
  '''        with self._read_lock:
            tc, values = self.values_now()''',
  '''        with self._lock:
            tc, values = self.values_now()'''),

 ("fix1: a frame read before a zero() goes out with its values",
  "ltcplay/flamelink.py",
  "            if gen != self._zero_gen or (self.zeroed and self._purge is None\n",
  "            if (self.zeroed and self._purge is None\n"),

 ("fix1: an Abort is not repeated",
  "ltcplay/flamelink.py",
  "                self._abort_repeat = (aid, why, self._clock() + repeat_s)",
  "                self._abort_repeat = None"),

 ("fix1: an Abort repeats forever",
  "ltcplay/flamelink.py",
  "        if self._clock() > until:",
  "        if False:"),

 ("fix1: an Abort's repeat ignores flamesafe's frame_stale_ms",
  "ltcplay/flamelink.py",
  "                   self.frame_stale_ms / 1000.0 + ABORT_REPEAT_MARGIN_S)",
  "                   0.0)"),

 ("fix1: any larger last_id confirms the Abort",
  "ltcplay/flamelink.py",
  "                if _is_int(last) and last == pend[0]:",
  "                if _is_int(last) and last >= pend[0]:"),

 ("fix1: seq starts at 0",
  "ltcplay/flamelink.py",
  "        self.seq = _random_start()",
  "        self.seq = 0"),

 ("fix1: abort ids start at 0",
  "ltcplay/flamelink.py",
  "        self.abort_id = _random_start()",
  "        self.abort_id = 0"),

 ("fix1: a timecode that has stopped moving still carries cues",
  "ltcplay/flamelink.py",
  "        if self._tc_moved_at is None or now - self._tc_moved_at > TC_STILL_S:",
  "        if self._tc_moved_at is None:"),

 ("fix1: a sender pass that raises ends the thread",
  "ltcplay/flamelink.py",
  '''                except Exception as e:
                    self._run_failed(e)''',
  '''                except ZeroDivisionError as e:
                    self._run_failed(e)'''),

 ("fix1: a dead sender thread reads as fine",
  "ltcplay/flamelink.py",
  '''        if self._run_dead or (t is not None and not t.is_alive()
                              and not self._stop.is_set()):''',
  '''        if False:'''),

 ("fix1: a failing sender reads as fine",
  "ltcplay/flamelink.py",
  '''                               and sender not in ("failing", "dead",
                                                  "stalled")),''',
  '''                               and sender not in ("dead",
                                                  "stalled")),'''),

 # -- fix round 2 of PR #34 ------------------------------------------------
 ("fix2: flamesafe's frame_stale_ms is not read from its config",
  "ltcplay/flamelink.py",
  '''                         "frame_stale_ms": doc.get("frame_stale_ms")},''',
  '''                         },'''),

 ("fix2: a flame_link block without frame_stale_ms quietly defaults to 500",
  "ltcplay/flamelink.py",
  '''        stale = doc.get("frame_stale_ms")''',
  '''        stale = doc.get("frame_stale_ms", 500)'''),

 ("fix2: a config built in code assumes a short frame_stale_ms",
  "ltcplay/flamelink.py",
  "FRAME_STALE_MS_DEFAULT = STALE_MS_MAX",
  "FRAME_STALE_MS_DEFAULT = 500"),

 ("fix2: a stalled sender reads as running",
  "ltcplay/flamelink.py",
  '''        if self._stalled_now():
            return "stalled"''',
  '''        if False:
            return "stalled"'''),

 ("fix2: a stalled sender reads as sending fine",
  "ltcplay/flamelink.py",
  '''                               and sender not in ("failing", "dead",
                                                  "stalled")),''',
  '''                               and sender not in ("failing", "dead")),'''),

 ("fix2: a stall is never journaled",
  "ltcplay/flamelink.py",
  "                if stalled and not self._stall_noted:",
  "                if False:"),

 ("fix2: a stall is journaled over and over",
  "ltcplay/flamelink.py",
  "                    self._stall_noted = True\n",
  "                    self._stall_noted = False\n"),

 ("fix2: a sender is stalled only after the whole frame_stale_ms",
  "ltcplay/flamelink.py",
  "        return self.cfg.frame_stale_ms / 2000.0",
  "        return self.cfg.frame_stale_ms / 1000.0"),

 ("fix2: disarm_all does not invalidate a frame read before it",
  "ltcplay/flamelink.py",
  "                self.zeroed = True\n                self._zero_gen += 1\n"
  "                self._send_zero_frame()\n                self.abort_id",
  "                self.zeroed = True\n"
  "                self._send_zero_frame()\n                self.abort_id"),

 ("fix2: repeat copies do not restart the post-Abort window",
  "flamesafe/composer.py",
  "        self._disarm_at = t\n        if new_abort:\n",
  "        if new_abort:\n            self._disarm_at = t\n"),

 ("fix2: the deck's arm-hold grows past the post-Abort window",
  "ltcplay/streamdeck.py",
  "ARM_HOLD_S = 0.6\n",
  "ARM_HOLD_S = 0.8\n"),

 ("fix1: an exception whose str() raises escapes the error handling",
  "ltcplay/flamelink.py",
  '''        text = "(its message could not be read)"''',
  '''        raise'''),

 ("fix1: the journal says an unconfirmed Abort was done",
  "ltcplay/flamelink.py",
  '''                       f"for {repeat_s:g} s. Sent, NOT yet confirmed by "''',
  '''                       f"for {repeat_s:g} s. Done, every group disarmed by "'''),

 ("fix1 H13: a late sender bursts to catch up",
  "ltcplay/flamelink.py",
  "                        next_at = now + period",
  "                        pass"),

 ("fix1: ltcplay takes a timecode with a trailing newline",
  "ltcplay/flamelink.py",
  "        if not (isinstance(tc, str) and _TC.fullmatch(tc)):",
  "        if not (isinstance(tc, str) and re.match(r\"^\\d{2}:\\d{2}:\\d{2}[:;]\\d{2}$\", tc)):"),

 ("fix1: ltcplay takes a key with a trailing newline",
  "ltcplay/flamelink.py",
  "            and bool(_KEY.fullmatch(key)))",
  "            and bool(re.match(r\"^[\\x21-\\x7e]+$\", key)))"),

 ("flamelink: one Abort is a single datagram",
  "ltcplay/flamelink.py",
  "DISARM_COPIES = 3",
  "DISARM_COPIES = 1"),

 ("flamelink: a new socket for every frame",
  "ltcplay/flamelink.py",
  "            s.sendto(data, (self.cfg.ip, self.cfg.port))",
  "            socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(\n"
  "                data, (self.cfg.ip, self.cfg.port))"),

 ("flamelink: seq does not advance",
  "ltcplay/flamelink.py",
  '''        self.seq += 1
        if self.first_seq is None:''',
  '''        if self.first_seq is None:'''),

 ("flamelink: mono goes backwards with the clock",
  "ltcplay/flamelink.py",
  '''        if self._mono_last is not None and mono < self._mono_last:
            mono = self._mono_last''',
  '''        if False:
            mono = self._mono_last'''),

 ("flamelink: a failed send is journaled every frame",
  "ltcplay/flamelink.py",
  "            if self._fail_since is None:\n                self._fail_since = self._clock()",
  "            if True:\n                self._fail_since = self._clock()"),

 ("flamelink: the end of a send outage is never journaled",
  "ltcplay/flamelink.py",
  '''        if self._fail_since is not None:
            gone = self._clock() - self._fail_since''',
  '''        if False:
            gone = self._clock() - self._fail_since'''),

 ("flamelink: a send failure is silent",
  "ltcplay/flamelink.py",
  '''                self._note(f"Flame link: a frame to flamesafe could not be "''',
  '''                (lambda *a, **k: None)(f"Flame link: a frame to flamesafe could not be "'''),

 ("flamelink: the lock alarm never fires",
  "ltcplay/flamelink.py",
  "                if now - self._not_ours_since > LOCK_ALARM_S and \\",
  "                if False and \\"),

 ("flamelink: the lock alarm fires on one stray status",
  "ltcplay/flamelink.py",
  "LOCK_ALARM_S = 1.0",
  "LOCK_ALARM_S = 0.0"),

 ("flamelink: an unconfirmed disarm is never reported",
  "ltcplay/flamelink.py",
  "            self._abort_unconfirmed = True\n            return pend[0]",
  "            self._abort_unconfirmed = True\n            return None"),

 ("flamelink: a held clock reads as live",
  "ltcplay/flamelink.py",
  '''        live = (playing and not getattr(clk, "paused", True)''',
  '''        live = (playing'''),

 ("flamelink: a clock fading out on Abort reads as live",
  "ltcplay/flamelink.py",
  '''                and not getattr(clk, "_halting", False) and tc is not None)''',
  '''                and tc is not None)'''),

 ("flamelink: the config accepts a send rate below the contract floor",
  "ltcplay/flamelink.py",
  "SEND_HZ_MIN = 20",
  "SEND_HZ_MIN = 1"),

 ("flamelink: the config accepts a non-loopback address",
  "ltcplay/flamelink.py",
  '''        if not isinstance(ip, str) or not ip.startswith("127.") or \\''',
  '''        if not isinstance(ip, str) or \\'''),

 ("flamelink: a long Abort reason is sent uncut",
  "ltcplay/flamelink.py",
  '''            why = " ".join(str(reason or "Abort").split())[:REASON_MAX] \\''',
  '''            why = " ".join(str(reason or "Abort").split()) \\'''),

 ("flamelink: a status frame with another key is taken",
  "ltcplay/flamelink.py",
  '''            or obj.get("t") != "status" or obj.get("k") != key:''',
  '''            or obj.get("t") != "status":'''),

 ("flamelink: the example key passes without a word",
  "ltcplay/flamelink.py",
  "        if self.cfg.key == EXAMPLE_KEY:",
  "        if False:"),

 ("flamelink: the deck does not say ABORTED",
  "ltcplay/streamdeck.py",
  '''    "Disarmed by the show's Abort. Cycle the arm to re-arm.": "ABORTED",''',
  ""),

 # ---- round 5 of the safety review (PR #31) ------------------------------
 # 1: a long unplug. The round-4 test's no-deck time added up to exactly 6 s,
 # so this one (the review's hand mutation) passed the whole suite.
 ("round5: the no-deck sender stops for good after 6 s",
  "ltcplay/streamdeck.py",
  "        try:\n"
  "            if pacer is not None:\n"
  "                pacer.beat()\n"
  "            else:\n"
  "                controller.arm.send(controller.names)\n"
  "        except Exception:\n"
  "            pass\n"
  "        sleep(1.0 / ARM_SEND_HZ)\n",
  "        try:\n"
  "            _n = controller.__dict__.setdefault('_r5_off', [0])\n"
  "            _n[0] += 1\n"
  "            if _n[0] > 120:\n"
  "                pass\n"
  "            elif pacer is not None:\n"
  "                pacer.beat()\n"
  "            else:\n"
  "                controller.arm.send(controller.names)\n"
  "        except Exception:\n"
  "            pass\n"
  "        sleep(1.0 / ARM_SEND_HZ)\n"),

 ("round5: the no-deck sender stops for good after 60 s",
  "ltcplay/streamdeck.py",
  "        try:\n"
  "            if pacer is not None:\n"
  "                pacer.beat()\n"
  "            else:\n"
  "                controller.arm.send(controller.names)\n"
  "        except Exception:\n"
  "            pass\n"
  "        sleep(1.0 / ARM_SEND_HZ)\n",
  "        try:\n"
  "            _n = controller.__dict__.setdefault('_r5_off', [0])\n"
  "            _n[0] += 1\n"
  "            if _n[0] > 1200:\n"
  "                pass\n"
  "            elif pacer is not None:\n"
  "                pacer.beat()\n"
  "            else:\n"
  "                controller.arm.send(controller.names)\n"
  "        except Exception:\n"
  "            pass\n"
  "        sleep(1.0 / ARM_SEND_HZ)\n"),

 # 2: any error from the deck process is an unplug, never the end of it.
 ("round5: a plain error opening the deck ends run_forever again",
  "ltcplay/streamdeck.py",
  "        except Exception as e:          # round 5, item 2: not only\n"
  "            outage.failed(e, \"Retrying in 2 s;",
  "        except DeckDisconnected as e:\n"
  "            outage.failed(e, \"Retrying in 2 s;"),

 ("round5: a plain error in a main-loop pass ends run_forever again",
  "ltcplay/streamdeck.py",
  "        except Exception as e:          # round 5, item 2: not only\n"
  "            outage.failed(e, \"Every flame group stays as it was on the arm \"",
  "        except DeckDisconnected as e:\n"
  "            outage.failed(e, \"Every flame group stays as it was on the arm \""),

 ("round5: Deck() lets a raw hidapi OSError escape as itself again",
  "ltcplay/streamdeck.py",
  "        except Exception as e:\n"
  "            if h is not None:\n",
  "        except OSError:\n"
  "            raise\n"
  "        except Exception as e:\n"
  "            if h is not None:\n"),

 ("round5: Deck() leaves a half-opened HID handle open",
  "ltcplay/streamdeck.py",
  "                    h.close()     # a half-opened handle must not keep the\n",
  "                    pass          # a half-opened handle must not keep the\n"),

 ("round5: the hold-off dies on an arm socket that will not open",
  "ltcplay/streamdeck.py",
  "            controller.arm.send(controller.names)\n"
  "        except Exception:\n"
  "            pass\n"
  "        sleep(1.0 / ARM_SEND_HZ)\n",
  "            controller.arm.send(controller.names)\n"
  "        except DeckDisconnected:\n"
  "            pass\n"
  "        sleep(1.0 / ARM_SEND_HZ)\n"),

 ("round5: every deck retry is journaled again (no once-per-outage)",
  "ltcplay/streamdeck.py",
  "                else type(e).__name__)\n"
  "        if kind in self._kinds:\n"
  "            self._unlogged += 1\n"
  "            return\n",
  "                else type(e).__name__)\n"),

 ("round5: a deck outage never ends, so a later unplug is never journaled",
  "ltcplay/streamdeck.py",
  "        if not self.active or secs < DECK_STABLE_S:\n",
  "        if True:\n"),

 # 4: a flood is measured in bytes as well as datagrams, and the arm
 # socket asks for a bigger receive buffer. The review's hand mutations
 # ">=" and "50 -> 65" passed the old suite (it flooded with 70).
 ("round5: the flood datagram threshold is off by one (>=)",
  "flamesafe/arminput.py",
  "        if n_read > FLOOD_DATAGRAMS_PER_POLL or \\\n",
  "        if n_read >= FLOOD_DATAGRAMS_PER_POLL or \\\n"),

 ("round5: the flood datagram threshold moves from 50 to 65",
  "flamesafe/arminput.py",
  "FLOOD_DATAGRAMS_PER_POLL = 50\n",
  "FLOOD_DATAGRAMS_PER_POLL = 65\n"),

 ("round5: a flood is counted in datagrams only, never bytes",
  "flamesafe/arminput.py",
  "                n_bytes > self.flood_bytes:\n",
  "                False:\n"),

 ("round5: the flood byte threshold is off by one (>=)",
  "flamesafe/arminput.py",
  "                n_bytes > self.flood_bytes:\n",
  "                n_bytes >= self.flood_bytes:\n"),

 ("round5: the flood byte threshold doubles",
  "flamesafe/arminput.py",
  "FLOOD_BYTES_PER_POLL = 64 * 1024\n",
  "FLOOD_BYTES_PER_POLL = 128 * 1024\n"),

 ("round5: the byte limit ignores a small receive buffer (a flood can "
  "fill a capped or refused buffer without ever reading as one)",
  "flamesafe/arminput.py",
  "    return max(FLOOD_BYTES_FLOOR, min(FLOOD_BYTES_PER_POLL, rcvbuf // 4))\n",
  "    return FLOOD_BYTES_PER_POLL\n"),

 ("round5: the arm socket keeps the kernel's default receive buffer",
  "flamesafe/arminput.py",
  "            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF,\n"
  "                            ARM_RCVBUF_BYTES)\n",
  "            pass\n"),

 # 5: a ceiling across every arm-link rejection reason, and half the
 # journal queue kept free of arm-link lines.
 ("round5: no global ceiling across arm-link rejection reasons",
  "flamesafe/arminput.py",
  "GLOBAL_LINES_PER_MINUTE = 8\n",
  "GLOBAL_LINES_PER_MINUTE = 10 ** 6\n"),

 ("round5: the global ceiling is checked but lines are never counted "
  "against it",
  "flamesafe/arminput.py",
  "        self._all_lines.append(now)\n",
  ""),

 ("round5: arm-link lines can fill the whole journal queue again",
  "flamesafe/journal.py",
  "LOW_PRIORITY_MAX = QUEUE_MAX // 2\n",
  "LOW_PRIORITY_MAX = QUEUE_MAX\n"),

 ("round5: the journal's arm-link limit drops important lines too",
  "flamesafe/journal.py",
  "            if kind in LOW_PRIORITY_KINDS and \\\n",
  "            if True and \\\n"),


 # -- PR #30 fix round (independent review), 2026-10-02. All named
 # "scheduler fix round: ..." so `python3 mutate.py "fix round"` runs them.
 # Item 1: conductor calls after the save, in order, off the lock, faults.
 ('scheduler fix round: the conductor is asked before tonight is saved',
  'ltcplay/schedule_service.py',
  '        self._record(out, now, plan, held, ev)\n        if ev.kind == sch.RESUME and ev.actor == "operator" and \\\n',
  '        self._queue_conductor(plan, ev, calls)\n        self._calls.flush(0.5)\n        plan = []\n        self._record(out, now, plan, held, ev)\n        if ev.kind == sch.RESUME and ev.actor == "operator" and \\\n'),

 ("scheduler fix round: conductor calls are made inside the scheduler's lock",
  "ltcplay/schedule_service.py",
  "            self._calls.put(call)\n\n    def stamp_press",
  "            self._run_conductor_call(call)\n\n    def stamp_press"),

 ("scheduler fix round: conductor calls lose their order",
  "ltcplay/schedule_service.py",
  "                call = self._q.popleft()",
  "                call = self._q.pop()"),

 ("scheduler fix round: a conductor that raises is not a fault",
  "ltcplay/schedule_service.py",
  '            ok, said = False, f"it raised {type(e).__name__}: {e}"',
  '            ok, said = True, ""'),

 ("scheduler fix round: a failed conductor result is not a fault",
  "ltcplay/schedule_service.py",
  '            ok = getattr(r, "ok", None) is True',
  '            ok = True'),

 # Item 2: what the conductor is asked, and when.
 ("scheduler fix round: show_starting on START_SHOW again, before any cue "
  "plays",
  "ltcplay/schedule_service.py",
  "        if ev.kind == sch.SHOW_CONFIRMED and \\",
  "        if sch.START_SHOW in kinds and \\"),

 ("scheduler fix round: the last show and Close for the night never reach "
  "the conductor",
  "ltcplay/schedule_service.py",
  "        if sch.BLACKOUT in kinds:\n"
  "            plan.append((\"Out of the show\", \"intermission\",",
  "        if False:\n"
  "            plan.append((\"Out of the show\", \"intermission\","),

 ("scheduler fix round: the Service's Reset never reaches the conductor",
  "ltcplay/schedule_service.py",
  "                self._calls.put(call)\n        if late:",
  "                call.ok, call.sentence = False, \"not sent\"\n"
  "                call.done.set()\n        if late:"),

 ('scheduler fix round: the Abort line still says nothing was disarmed',
  'ltcplay/schedule_service.py',
  '        if self.conductor is not None and le.action == sch.ABORT and \\',
  '        if False and \\'),

 ("scheduler fix round: effects the conductor does not perform are "
  "journaled as performed",
  "ltcplay/schedule_service.py",
  "            claimed |= set(kinds)",
  "            claimed |= {e.kind for e in out.effects}"),

 # Item 3: a failed start goes dark, no disarm, no latch.
 ("scheduler fix round: a failed start or a cut show is an Abort again",
  "ltcplay/schedule_service.py",
  "            if ev.kind == sch.ABORT:\n"
  "                plan.append((\"Abort\", \"abort\", self._ABORT_EFFECTS))",
  "            if True:\n"
  "                plan.append((\"Abort\", \"abort\", self._ABORT_EFFECTS))"),

 ("scheduler fix round: conductor: a stopped show disarms the flames",
  "ltcplay/conductor.py",
  "        self._step(gen, \"lasers\", BLACK, \"lasers blanked\", progress,\n"
  "                   self.devices.lasers_blank)\n"
  "        faded = False",
  "        self._step(gen, \"lasers\", BLACK, \"lasers blanked\", progress,\n"
  "                   self.devices.lasers_blank)\n"
  "        self._disarm()\n"
  "        faded = False"),

 ("scheduler fix round: conductor: a stopped show latches",
  "ltcplay/conductor.py",
  "            self._accept(\"Show stopped\", STOPPED_DARK, who, screen,",
  "            self._latched = True\n"
  "            self._accept(\"Show stopped\", STOPPED_DARK, who, screen,"),

 ("scheduler fix round: conductor: leaving the show cuts a stopped show's "
  "fade short",
  "ltcplay/conductor.py",
  "            if self._look == STOPPED_DARK:",
  "            if False:"),

 ("scheduler fix round: conductor: a stopped show leaves the video and "
  "pixels up",
  "ltcplay/conductor.py",
  "        faded |= self._step(gen, \"pixels\", BLACK, \"pixels faded\", progress,\n"
  "                            self.show.pixels_fade_out, fade)\n"
  "        faded |= self._step(gen, \"music\", MUSIC_STOPPED, \"music faded\",\n"
  "                            progress, self.show.music_halt, fade)\n"
  "        if faded:\n"
  "            self._pause(gen, fade)\n"
  "        self._step(gen, \"video\", STOPPED, \"video bank stopped\"",
  "        faded |= self._step(gen, \"music\", MUSIC_STOPPED, \"music faded\",\n"
  "                            progress, self.show.music_halt, fade)\n"
  "        if faded:\n"
  "            self._pause(gen, fade)\n"
  "        self._step(gen, \"video\", STOPPED, \"video bank stopped\""),

 # Item 4: a delayed night across midnight (since 2026-10-03 it closes at
 # the 2 AM nightly reset; see the "2 AM reset" mutations at the end).
 ("scheduler fix round: a delayed night never closes at the 2 AM reset",
  "ltcplay/schedule_service.py",
  "        if now < sch.night_reset(m.date, m.tz):\n"
  "            return None",
  "        if True:\n"
  "            return None"),

 ("scheduler fix round: the delayed show is not missed when its night "
  "closes at the reset",
  "ltcplay/schedule.py",
  "    tx.set_slot(d.n, status=MISSED, reason=RESET_MISSED)",
  "    pass"),

 ("scheduler fix round: a start after midnight never picks up last night",
  "ltcplay/schedule_service.py",
  "            old = self._open_night_before(d, now)",
  "            old = None"),

 ("scheduler fix round: a show running after midnight is not picked up on "
  "restart",
  "ltcplay/schedule_service.py",
  "                       if st == sch.RUNNING and back == 1]",
  "                       if False]"),

 ("scheduler fix round: the 2 AM reset miss is written quietly, not as a "
  "fault",
  "ltcplay/schedule.py",
  "    tx.note(\"miss\", \"fault\", RESET_MISSED, text, show=d.n,",
  "    tx.note(\"miss\", \"done\", RESET_MISSED, text, show=d.n,"),

 # Item 5: a restart after a stopped show stays dark.
 ("scheduler fix round: a restart after a stopped show brings the "
  "intermission back",
  "ltcplay/schedule.py",
  "    dark = cut or m.dark",
  "    dark = cut"),

 ("scheduler fix round: dark is never saved, so a restart forgets it",
  "ltcplay/schedule.py",
  "    if m.dark:\n"
  "        doc[\"dark\"] = True",
  "    if False:\n"
  "        doc[\"dark\"] = True"),

 # Item 6: Start now runs an extra show (Jeff, 2026-10-02).
 ("scheduler fix round: Start now jumps the next show early again",
  "ltcplay/schedule.py",
  "        _fire(tx, d, DELAYED_START)\n"
  "    else:",
  "        _fire(tx, d, DELAYED_START)\n"
  "    elif m.next_slot() is not None:\n"
  "        _fire(tx, m.next_slot(), EXTRA_SHOW)\n"
  "    else:"),

 ("scheduler fix round: a show an extra show pushed aside is missed",
  "ltcplay/schedule.py",
  "            if extra:\n"
  "                _hold_back(tx, s, extra=extra)",
  "            if False:\n"
  "                _hold_back(tx, s, extra=extra)"),

 ("scheduler fix round: an extra show's guard does not delay the next show",
  "ltcplay/schedule.py",
  "        if s.ended_at == m.last_end and s.origin == \"operator\":",
  "        if False:"),

 # Coordinator's item A: after an Abort, dark until Reset.
 ("scheduler fix round: a show due while aborted starts anyway",
  "ltcplay/schedule.py",
  "            if tx.ev.latched:\n"
  "                # Jeff",
  "            if False:\n"
  "                # Jeff"),

 ("scheduler fix round: Start now works while aborted",
  "ltcplay/schedule.py",
  "    if ev.latched:\n"
  "        return _refuse(m, ev, now, \"The show was aborted",
  "    if False:\n"
  "        return _refuse(m, ev, now, \"The show was aborted"),

 ("scheduler fix round: the service never tells the engine it is aborted",
  "ltcplay/schedule_service.py",
  "        if ev.kind in self.LATCH_EVENTS and self._aborted():",
  "        if False:"),

 ("scheduler fix round: an Abort still queued does not count yet",
  "ltcplay/schedule_service.py",
  "            self.machine = replace(self.machine, abort_latched=True)",
  "            pass"),

 # The review's own survivors.
 ("scheduler fix round: the tick never moves IDLE to STANDBY at the lead",
  "ltcplay/schedule.py",
  "    elif before == IDLE and st == IDLE and _in_preshow_lead(tx.m, now):",
  "    elif False:"),

 ("scheduler fix round: Resume ignores the preshow lead",
  "ltcplay/schedule.py",
  "    if back == IDLE and (any(s.status != PENDING for s in m.slots) or\n"
  "                          _in_preshow_lead(m, now)):",
  "    if back == IDLE and any(s.status != PENDING for s in m.slots):"),

 # -- PR #30 fix round 2 (independent re-review), 2026-10-02. All named
 # "scheduler fix round 2: ..." so `python3 mutate.py "fix round 2"` runs
 # them. Item 1: the dark sequence is sent again on every dark start.
 ("scheduler fix round 2: a dark restart sends the conductor nothing",
  "ltcplay/schedule_service.py",
  "        if ev.kind == sch.BOOT_DONE and out.machine.dark and \\",
  "        if False and \\"),

 # Item 2: the Abort latch is saved, read back, outlives the night, and
 # only Reset ends it.
 ("scheduler fix round 2: the Abort latch is never written to tonight's file",
  "ltcplay/schedule.py",
  "    if m.abort_latched:\n        doc[\"abort_latched\"] = True",
  "    if False:\n        doc[\"abort_latched\"] = True"),

 ("scheduler fix round 2: the Abort latch is never read back",
  "ltcplay/schedule.py",
  "        abort_latched=doc.get(\"abort_latched\", False))",
  "        abort_latched=False)"),

 ("scheduler fix round 2: the scheduler ignores its own saved Abort latch",
  "ltcplay/schedule_service.py",
  "        if self.machine is not None and self.machine.abort_latched:\n"
  "            return True",
  "        if False:\n            return True"),

 ("scheduler fix round 2: Reset never clears the saved Abort latch",
  "ltcplay/schedule_service.py",
  "        self.machine = replace(m, abort_latched=False)\n"
  "        self._unreadable_night = None\n",
  "        self._unreadable_night = None\n"),

 ("scheduler fix round 2: a Reset after a restart can never end the Abort",
  "ltcplay/schedule_service.py",
  "        if not ok and still:\n            return ok, said",
  "        if not ok:\n            return ok, said"),

 ("scheduler fix round 2: a Reset pressed before an Abort ends it",
  "ltcplay/schedule_service.py",
  "        if call.seq < self._abort_seq or (\n"
  "                pressed is not None and pressed < self._abort_pressed):",
  "        if False:"),

 ("scheduler fix round 2: an unreset Abort does not make the start dark",
  "ltcplay/schedule.py",
  "    dark = cut or m.dark or ev.latched",
  "    dark = cut or m.dark"),

 ("scheduler fix round 2: a latched night with every show to come shows "
  "the preshow look",
  "ltcplay/schedule.py",
  "    elif dark:\n        # Every show is still to come",
  "    elif False:\n        # Every show is still to come"),

 ("scheduler fix round 2: the Abort latch is not carried into the next night",
  "ltcplay/schedule_service.py",
  "        if latched and not m.abort_latched:\n"
  "            m = replace(m, abort_latched=True)",
  "        if False:\n            m = replace(m, abort_latched=True)"),

 ("scheduler fix round 2: a fresh start forgets last night's Abort latch",
  "ltcplay/schedule_service.py",
  "                if fresh and self._latched_before(d):",
  "                if False:"),

 # Item 3: while latched, Hold, Resume and an announcement stay dark.
 ("scheduler fix round 2: Hold while aborted brings the intermission back",
  "ltcplay/schedule.py",
  "    _enter(tx, HOLD, after_stop=ev.latched)",
  "    _enter(tx, HOLD)"),

 ("scheduler fix round 2: Resume while aborted brings the intermission back",
  "ltcplay/schedule.py",
  "    _enter(tx, back, after_stop=ev.latched)",
  "    _enter(tx, back)"),

 ("scheduler fix round 2: Hold and Resume are never told about the latch",
  "ltcplay/schedule_service.py",
  "    LATCH_EVENTS = (sch.TICK, sch.BOOT_DONE, sch.START_NOW, sch.HOLD_ON,\n"
  "                    sch.RESUME)",
  "    LATCH_EVENTS = (sch.TICK, sch.BOOT_DONE, sch.START_NOW)"),

 # Item 4: a stuck or dead line is loud, and Abort does not wait behind it.
 ("scheduler fix round 2: the tick never watches the conductor's line",
  "ltcplay/schedule_service.py",
  "            self._watch_conductor()\n", ""),

 ("scheduler fix round 2: a hung conductor request is silent",
  "ltcplay/schedule_service.py",
  "        elif h[\"stuck\"] is not None and \\\n"
  "                h[\"age_s\"] >= self.CONDUCTOR_STUCK_S:",
  "        elif False:"),

 ("scheduler fix round 2: a hung conductor request is a fault every tick",
  "ltcplay/schedule_service.py",
  "            if was is not None and was[\"key\"] == problem[0]:\n"
  "                return",
  "            if False:\n                return"),

 ("scheduler fix round 2: no line says the conductor is answering again",
  "ltcplay/schedule_service.py",
  "        if was is not None:\n            self._conductor_trouble = None",
  "        if False:\n            self._conductor_trouble = None"),

 ("scheduler fix round 2: a dead line of conductor requests stays dead",
  "ltcplay/schedule_service.py",
  "        if not h[\"alive\"]:\n            self._calls.revive()\n",
  "        if not h[\"alive\"]:\n"),

 ("scheduler fix round 2: the conductor's trouble never reaches the page",
  "ltcplay/schedule_service.py",
  "                    \"trouble\": t[\"text\"] if t else None}",
  "                    \"trouble\": None}"),

 ("scheduler fix round 2: Abort waits behind a hung conductor request",
  "ltcplay/schedule_service.py",
  "            urgent = call.method == \"abort\" and (",
  "            urgent = False and ("),

 ("scheduler fix round 2: requests an Abort supersedes still go out after it",
  "ltcplay/schedule_service.py",
  "            dropped = [c for c in self._q if c.seq < call.seq and\n"
  "                       c.method in self.SUPERSEDED_BY_ABORT]",
  "            dropped = []"),

 ("scheduler fix round 2: an Abort that goes ahead drops later Resets too",
  "ltcplay/schedule_service.py",
  "            dropped = [c for c in self._q if c.seq < call.seq and\n",
  "            dropped = [c for c in self._q if\n"),

 ("scheduler fix round 2: a conductor request raising SystemExit is not a "
  "fault",
  "ltcplay/schedule_service.py",
  "        except BaseException as e:      # SystemExit too: never the thread",
  "        except Exception as e:"),

 # Item 5: Reset refusals are journaled.
 ("scheduler fix round 2: a refused Reset is not journaled",
  "ltcplay/schedule_service.py",
  "    def _refuse_reset(self, who, screen, sentence):\n"
  "        with self._locked():",
  "    def _refuse_reset(self, who, screen, sentence):\n"
  "        if False:"),

 # Item 6: the review's hand mutations that nothing caught.
 ("scheduler fix round 2: show_starting even while the confirmed show is "
  "paused",
  "ltcplay/schedule_service.py",
  "        if ev.kind == sch.SHOW_CONFIRMED and \\\n"
  "                out.machine.state == sch.SHOW:",
  "        if ev.kind == sch.SHOW_CONFIRMED:"),

 ("scheduler fix round 2: conductor: a stopped show leaves the music playing",
  "ltcplay/conductor.py",
  "        faded |= self._step(gen, \"music\", MUSIC_STOPPED, \"music faded\",\n"
  "                            progress, self.show.music_halt, fade)\n"
  "        if faded:\n            self._pause(gen, fade)\n"
  "        self._step(gen, \"video\", STOPPED, \"video bank stopped\"",
  "        if faded:\n            self._pause(gen, fade)\n"
  "        self._step(gen, \"video\", STOPPED, \"video bank stopped\""),

 ("scheduler fix round 2: the 2 AM reset miss is not on the night's fault "
  "list",
  "ltcplay/schedule.py",
  "    tx.m = replace(tx.m, faults=tx.m.faults + (text,))\n"
  "    tx.note(\"miss\", \"fault\", RESET_MISSED",
  "    tx.note(\"miss\", \"fault\", RESET_MISSED"),

 ("scheduler fix round 2: every conductor call is made as the scheduler",
  "ltcplay/schedule_service.py",
  "        who = ev.who if op else \"the scheduler\"\n"
  "        screen = ev.screen if op else \"\"",
  "        who = \"the scheduler\"\n        screen = \"\""),

 ("scheduler fix round 2: stop() does not wait for the conductor's last lines",
  "ltcplay/schedule_service.py",
  "        self._calls.flush(1.0)\n", ""),

 ("scheduler fix round 2: an open night is looked for only one day back",
  "ltcplay/schedule_service.py",
  "    OPEN_NIGHT_LOOK_BACK = 7", "    OPEN_NIGHT_LOOK_BACK = 1"),

 # Item 7: tonight's file format.
 ("scheduler fix round 2: a dark night is written as format 3",
  "ltcplay/schedule.py",
  "        \"format\": TONIGHT_FORMAT if marked else TONIGHT_PLAIN_FORMAT,",
  "        \"format\": TONIGHT_PLAIN_FORMAT,"),

 ("scheduler fix round 2: format 3 files may carry dark and the latch",
  "ltcplay/schedule.py",
  "    allowed = TONIGHT_KEYS | (TONIGHT_OPTIONAL if fmt == TONIGHT_FORMAT\n"
  "                              else frozenset())",
  "    allowed = TONIGHT_KEYS | TONIGHT_OPTIONAL"),

 # -- PR #30 fix round 3 (third independent review), 2026-10-03. All named
 # "scheduler fix round 3: ..." so `python3 mutate.py "fix round 3"` runs
 # them. The first group breaks the Abort latch file and the start-up
 # latch; the second is the review's own hand mutations (R3xx) that the
 # suite did not catch before this round.
 ("scheduler fix round 3: the Abort latch file is never written",
  "ltcplay/schedule_service.py",
  "        marker_error = self._write_latch_marker(m) if latched else None\n"
  "        path = tonight_path(m.date, self.state_dir)\n",
  "        marker_error = None\n"
  "        path = tonight_path(m.date, self.state_dir)\n"),

 ("scheduler fix round 3: the Abort latch file is written after tonight's "
  "list",
  "ltcplay/schedule_service.py",
  "        marker_error = self._write_latch_marker(m) if latched else None\n"
  "        path = tonight_path(m.date, self.state_dir)\n"
  "        try:\n"
  "            write_json_atomic(path, sch.machine_to_doc(m), tries=tries)\n"
  "        except OSError as e:\n",
  "        path = tonight_path(m.date, self.state_dir)\n"
  "        try:\n"
  "            write_json_atomic(path, sch.machine_to_doc(m), tries=tries)\n"
  "            marker_error = self._write_latch_marker(m) if latched else None\n"
  "        except OSError as e:\n"
  "            marker_error = self._write_latch_marker(m) if latched else None\n"),

 ("scheduler fix round 3: the Abort latch file is never read at start",
  "ltcplay/schedule_service.py",
  "        if os.path.exists(marker) and not m.abort_latched:\n",
  "        if False:\n"),

 ("scheduler fix round 3: the Abort latch file latches with no conductor",
  "ltcplay/schedule_service.py",
  "        if self.conductor is None:\n            return m\n        whys = []",
  "        whys = []"),

 ("scheduler fix round 3: an unreadable or set aside list does not latch",
  "ltcplay/schedule_service.py",
  "        if self._unreadable_night == m.date or os.path.exists(aside):\n",
  "        if False:\n"),

 ("scheduler fix round 3: an unreadable earlier night does not latch a "
  "fresh start",
  "ltcplay/schedule_service.py",
  "                action=\"load tonight\", outcome=\"still aborted\", "
  "fault=True)\n            return True",
  "                action=\"load tonight\", outcome=\"still aborted\", "
  "fault=True)\n            return False"),

 ("scheduler fix round 3: Reset leaves the Abort latch file in place",
  "ltcplay/schedule_service.py",
  "            remove_latch_marker(path)\n"
  "            self._marker_clear_pending = False",
  "            self._marker_clear_pending = False"),

 ("scheduler fix round 3: Reset leaves the set aside list latching",
  "ltcplay/schedule_service.py",
  "                os.replace(aside, done)\n",
  "                pass\n"),

 ("scheduler fix round 3: a latch that could not be saved is not said",
  "ltcplay/schedule_service.py",
  "        if text is not None:\n"
  "            self._journal_line(\"system\", text, action=\"save abort latch\",",
  "        if False:\n"
  "            self._journal_line(\"system\", text, action=\"save abort latch\","),

 ("scheduler fix round 3: a failed latch save says the list holds it",
  "ltcplay/schedule_service.py",
  "        elif list_saved:\n            text = self.LATCH_HALF",
  "        elif True:\n            text = self.LATCH_HALF"),

 ("scheduler fix round 3: a latch that could not be saved is never tried "
  "again",
  "ltcplay/schedule_service.py",
  "            self._keep_latch_on_disk()\n            m = self.machine",
  "            m = self.machine"),

 ("scheduler fix round 3: a Reset that overtakes an Abort in flight ends it",
  "ltcplay/schedule_service.py",
  "        if self._calls.aborts_beside():\n",
  "        if False:\n"),

 ('scheduler fix round 3: R303 an Abort that goes ahead still sends a queued show start',
  'ltcplay/schedule_service.py',
  '    SUPERSEDED_BY_ABORT = ("reset", "hold", "resume", "show_starting",\n                           "start_show")',
  '    SUPERSEDED_BY_ABORT = ("reset", "hold", "resume",\n                           "start_show")'),

 ("scheduler fix round 3: R304 an Abort waits behind a dead line",
  "ltcplay/schedule_service.py",
  "                self._busy or len(self._q) > 1 or not self._alive())",
  "                self._busy)"),

 ("scheduler fix round 3: R307 a fresh start reads the oldest earlier night "
  "for the latch",
  "ltcplay/schedule_service.py",
  "        y = max(dates)", "        y = min(dates)"),

 ("scheduler fix round 3: R309 a schedule change rebuild drops the latch",
  "ltcplay/schedule.py",
  "                abort_latched=saved.abort_latched)",
  "                abort_latched=False)"),

 ("scheduler fix round 3: R312 a hung conductor request is a fault only "
  "after 30 s",
  "ltcplay/schedule_service.py",
  "    CONDUCTOR_STUCK_S = 3.0", "    CONDUCTOR_STUCK_S = 30.0"),

 ("scheduler fix round 3: R313 an Abort sent beside the line is never "
  "watched for a hang",
  "ltcplay/schedule_service.py",
  "            running = list(self._side.items())",
  "            running = []"),

 ("scheduler fix round 3: R314 flush does not wait for an Abort sent beside "
  "the line",
  "ltcplay/schedule_service.py",
  "                lambda: not self._q and not self._busy and not self._side,",
  "                lambda: not self._q and not self._busy,"),

 ("scheduler fix round 3: R315 a revived line still thinks it is busy",
  "ltcplay/schedule_service.py",
  "            self._busy = False\n            self._current = self._since = None\n"
  "            self._start()",
  "            self._start()"),

 ("scheduler fix round 3: R317 a conductor that cannot say whether it is "
  "latched counts as not latched in Reset",
  "ltcplay/schedule_service.py",
  "        except Exception:\n            still = True",
  "        except Exception:\n            still = False"),

 ("scheduler fix round 3: R318 a Reset refused while the Abort still fades "
  "clears the scheduler's latch",
  "ltcplay/schedule_service.py",
  "        if not ok and still:\n            return ok, said",
  "        if False:\n            return ok, said"),

 ("scheduler fix round 3: R322 a dark or latch that is not true or false is "
  "accepted",
  "ltcplay/schedule.py",
  "    for k in sorted(TONIGHT_OPTIONAL):\n"
  "        if not isinstance(doc.get(k, False), bool):",
  "    for k in sorted(TONIGHT_OPTIONAL):\n        if False:"),

 # Jeff's decisions, 2026-10-03: the 2 AM nightly reset.
 ("scheduler 2 AM reset: the nightly reset is at 3 AM",
  "ltcplay/schedule.py",
  "NIGHT_RESET = time(2, 0)", "NIGHT_RESET = time(3, 0)"),

 ("scheduler 2 AM reset: the nightly reset is at 1 AM",
  "ltcplay/schedule.py",
  "NIGHT_RESET = time(2, 0)\n", "NIGHT_RESET = time(1, 0)\n"),

 ("scheduler 2 AM reset: 02:00 itself still belongs to last night (<=)",
  "ltcplay/schedule.py",
  "    if local.time() < NIGHT_RESET:",
  "    if local.time() <= NIGHT_RESET:"),

 ("scheduler 2 AM reset: the night is read on UTC, not the local clock",
  "ltcplay/schedule.py",
  "    local = _utc(_aware(now)).astimezone(tz)",
  "    local = _utc(_aware(now))"),

 ("scheduler 2 AM reset: a night ends at its own date's reset, not the "
  "next day's",
  "ltcplay/schedule.py",
  "    return _utc(datetime.combine(d + timedelta(days=1), NIGHT_RESET,",
  "    return _utc(datetime.combine(d, NIGHT_RESET,"),

 ("scheduler 2 AM reset: the service moves on at midnight (calendar date)",
  "ltcplay/schedule_service.py",
  "        return sch.night_of(now, self.rule.tz)",
  "        return now.astimezone(self.rule.tz).date()"),

 ("scheduler 2 AM reset: a first show before 2 AM is accepted",
  "ltcplay/schedule.py",
  "    if first < NIGHT_RESET:",
  "    if False:"),

 ("scheduler 2 AM reset: a show running at the reset is cut by it",
  "ltcplay/schedule_service.py",
  "        if m.state in (sch.SHOW, sch.PAUSED):\n"
  "            return None\n"
  "        words = sch.reset_words()",
  "        words = sch.reset_words()"),

 # Jeff's ruling 1, 2026-10-12: a failed start leaves the flame groups AS
 # THEY ARE (it reverses 2026-10-03's "a failed start disarms every group").
 # It still goes dark, still latches nothing, and now raises an alert.
 ("failed start: the scheduler sends show_stopped (no alert)",
  "ltcplay/schedule_service.py",
  "            elif ev.kind == sch.SHOW_FAILED:",
  "            elif False:"),

 ("a failed start disarms every group", "ltcplay/conductor.py",
  "            self._accept(\"Failed start\", STOPPED_DARK, who, screen,\n"
  "                         fade_s=ABORT_FADE_S)",
  "            self._flames_cut(\"the show failed to start\")\n"
  "            self._accept(\"Failed start\", STOPPED_DARK, who, screen,\n"
  "                         fade_s=ABORT_FADE_S)"),

 ("failed start: the conductor latches a failed start",
  "ltcplay/conductor.py",
  "            self._accept(\"Failed start\", STOPPED_DARK, who, screen,",
  "            self._latched = True\n"
  "            self._accept(\"Failed start\", STOPPED_DARK, who, screen,"),

 ("failed start: the scheduler latches a failed start",
  "ltcplay/schedule_service.py",
  "        if any(c.method == \"abort\" for c in calls):",
  "        if any(c.method in (\"abort\", \"failed_start\") for c in calls):"),

 ("a failed start raises no alert", "ltcplay/schedule_service.py",
  "                    self.alert_sink(\"start\", alert)",
  "                    pass"),

 ("the failed-start line still says the groups were disarmed",
  "ltcplay/schedule.py",
  "    \"No flame group is disarmed: every group stays as it was (Jeff, \"",
  "    \"Every flame group is disarmed because the show failed to start (Jeff, \""),

 # Second-copy guard (Jeff, 2026-10-03; PR #34 open question 10). Part 1:
 # flamesafe's consent check on the flame link.
 ("second-copy: another sender on the flame link no longer blocks consent",
  "flamesafe/composer.py",
  "                     or self._arm_link_flooded\n"
  "                     or self._flame_link_disturbed(t))\n",
  "                     or self._arm_link_flooded)\n"),

 ("second-copy: a refused flame frame's sender is never remembered",
  "flamesafe/composer.py",
  "                    self._second_sender(sender, t)\n"
  "                    raise ValueError(\"another sender\")\n",
  "                    raise ValueError(\"another sender\")\n"),

 ("second-copy: a refused disarm_all's sender is never remembered",
  "flamesafe/composer.py",
  "                self._second_sender(sender, t)\n"
  "                raise ValueError(\"another sender\")\n",
  "                raise ValueError(\"another sender\")\n"),

 # Fix round 1 of PR #40 (the review's hand mutations H1 to H3, and
 # item 4: a second sender disarms every group; the flood flag).
 ("second-copy: a second sender is remembered from its first datagram "
  "only (setdefault)",
  "flamesafe/composer.py",
  "        self._flame_foreign[sender] = t\n",
  "        self._flame_foreign.setdefault(sender, t)\n"),

 ("second-copy: the flame veto blocks consent but no longer clears a down "
  "edge seen before it",
  "flamesafe/composer.py",
  "        disturbed = (self._foreign_arm_senders != 0\n"
  "                     or self._arm_link_flooded\n"
  "                     or self._flame_link_disturbed(t))\n"
  "        consent_ok = advanced and was_live and not disturbed\n",
  "        disturbed = (self._foreign_arm_senders != 0\n"
  "                     or self._arm_link_flooded)\n"
  "        consent_ok = (advanced and was_live and not disturbed\n"
  "                      and not self._flame_link_disturbed(t))\n"),

 ("second-copy: a second sender only blocks new arming again, armed "
  "groups stay armed",
  "flamesafe/composer.py",
  "        self._latched = [False] * self.n\n"
  "        self._seen_down = [False] * self.n\n"
  "        if first:\n",
  "        if first:\n"),

 ("second-copy: the second-sender line is written for every datagram",
  "flamesafe/composer.py",
  "        first = self._flame_foreign_count(t) == 0\n",
  "        first = True\n"),

 ("second-copy: the service never flags a flood on the flame link",
  "flamesafe/service.py",
  "                n_read > FLAME_FLOOD_DATAGRAMS_PER_TICK\n",
  "                False and n_read > FLAME_FLOOD_DATAGRAMS_PER_TICK\n"),

 ("second-copy: a flood on the flame link no longer blocks consent",
  "flamesafe/composer.py",
  "                or self._flame_new_sender(t)\n"
  "                or self._flame_flooded(t))\n",
  "                or self._flame_new_sender(t))\n"),

 ("second-copy: another flame sender is remembered for ever",
  "flamesafe/composer.py",
  "                  if (t - at) * 1000.0 > win]:\n",
  "                  if False]:\n"),

 ("second-copy: the flame link changing hands is not noticed",
  "flamesafe/composer.py",
  "                self._flame_changed_at = t\n",
  "                pass\n"),

 ("second-copy: the new-sender wait on the flame link never ends",
  "flamesafe/composer.py",
  "                (t - self._flame_changed_at) * 1000.0\n"
  "                <= self.cfg.frame_stale_ms)\n",
  "                (t - self._flame_changed_at) * 1000.0\n"
  "                <= 10 ** 12)\n"),

 ("second-copy: the flame link changing hands keeps what was armed",
  "flamesafe/composer.py",
  "                self._reset_latches(\"show program link changed sender\")\n",
  ""),

 ("second-copy: the lamp does not say another sender is on the flame link",
  "flamesafe/composer.py",
  "        if not self._latched[i] and flame_disturbed:\n",
  "        if False:\n"),

 ("second-copy: the status frame never counts other flame senders",
  "flamesafe/composer.py",
  "                \"foreign_senders\": self._flame_foreign_count(t),\n",
  "                \"foreign_senders\": 0,\n"),

 # Part 2: one copy of the show program and of ltc deck per machine.
 ("second-copy: ltc run starts beside a running show program",
  "ltcplay/cli.py",
  "    if refused:\n        return _err(refused)\n    try:\n"
  "        return _cmd_run(args)\n",
  "    if False:\n        return _err(refused)\n    try:\n"
  "        return _cmd_run(args)\n"),

 ("second-copy: ltc run takes a lock of its own, not the show's",
  "ltcplay/cli.py",
  "        onlyone.SHOW_LOCK,\n"
  "        f\"ltc run {os.path.basename(args.timeline)}",
  "        \"ltcplay_run.lock\",\n"
  "        f\"ltc run {os.path.basename(args.timeline)}"),

 ("second-copy: ltc serve starts beside a running show program",
  "ltcplay/cli.py",
  "    if refused:\n        return _err(refused)\n    try:\n"
  "        return _cmd_serve(args)\n",
  "    if False:\n        return _err(refused)\n    try:\n"
  "        return _cmd_serve(args)\n"),

 ("second-copy: ltc serve lets go of its lock before it runs",
  "ltcplay/cli.py",
  "    try:\n        return _cmd_serve(args)\n    finally:\n"
  "        lock.release()\n",
  "    lock.release()\n    return _cmd_serve(args)\n"),

 ("second-copy: ltc deck checks the show's lock instead of its own",
  "ltcplay/streamdeck.py",
  "        only = onlyone.only_copy(\n            onlyone.DECK_LOCK,\n",
  "        only = onlyone.only_copy(\n            onlyone.SHOW_LOCK,\n"),

 ("second-copy: ltc deck lets go of its lock before it runs",
  "ltcplay/streamdeck.py",
  "    try:\n        return _main(args)\n    finally:\n"
  "        only.release()\n",
  "    only.release()\n    return _main(args)\n"),

 ("second-copy: the refusal does not say what is running",
  "ltcplay/onlyone.py",
  "    said = f\"\\nThe copy that is running says: {holder}\" if holder "
  "else \"\"\n",
  "    said = \"\"\n"),


 # -- Fire & Ice: fire_ice.py's ShowOutputs and runner, the per-call
 # AudioMaster fade it needs, and where `ltc serve` builds the conductor.
 ("fire & ice: the Hold's fade is not passed to the show audio",
  "ltcplay/fire_ice.py",
  "lambda c: c.pause(fade_ms=fade_s * 1000.0))",
  "lambda c: c.pause())"),

 ("fire & ice: Abort's music fade is not passed to the show audio",
  "ltcplay/fire_ice.py",
  "lambda c: c.halt(fade_ms=fade_s * 1000.0))",
  "lambda c: c.halt())"),

 ("fire & ice: frozen is read from the Hold request, not the clock",
  "ltcplay/fire_ice.py",
  "        if self._clock() is None:\n            return None\n"
  "        return self._frozen",
  "        if self._clock() is None:\n            return None\n"
  "        return bool(self._clock()._paused or self._hooked is not None)"),

 ("fire & ice: chaining on_pause drops the session's own hard park",
  "ltcplay/fire_ice.py",
  "                try:\n                    if before_p is not None:\n"
  "                        before_p()",
  "                try:\n                    pass"),

 ("fire & ice: an Abort's disarm reports success it did not have",
  "ltcplay/fire_ice.py",
  "        return C.failed(f\"{reason}: {NO_DISARM}{tail}\")",
  "        return C.done(f\"{reason}: {NO_DISARM}{tail}\")"),

 ("fire & ice: an Abort's disarm does not zero the flame cues",
  "ltcplay/fire_ice.py",
  "        z = self.flames_zero()\n",
  "        z = C.done('')\n"),

 ("fire & ice: pixels restore over the operator's own look",
  "ltcplay/fire_ice.py",
  "            if p.override != \"blackout\":",
  "            if False:"),

 ("fire & ice: pixels restore to auto, not the look from before",
  "ltcplay/fire_ice.py",
  "            p.override = self._pix_prev",
  "            p.override = None"),

 ("fire & ice: scheduler_performs accepts anything truthy",
  "ltcplay/fire_ice.py",
  "        if performs is not True and performs is not False:",
  "        performs = bool(performs)\n        if False:"),

 ("fire & ice: the dry run ends without the switch",
  "ltcplay/fire_ice.py",
  "    if cfg.scheduler_performs:\n        runner = ShowRunner(",
  "    if True:\n        runner = ShowRunner("),

 ("fire & ice: a show starts before Run is pressed", "ltcplay/fire_ice.py",
  "        if s is None:\n            return self.RUN_OFF\n        if clk is None:\n            return self.NOT_AUDIO_MASTER",
  "        if s is not None and clk is None:\n            return self.NOT_AUDIO_MASTER"),

 ("fire & ice: a show starts while the conductor is still aborted", "ltcplay/fire_ice.py",
  "            self.LATCHED if self.conductor.latched else \"\")",
  "            \"\")"),

 ("fire & ice: an unconfirmed cue that stops is reported as ended",
  "ltcplay/fire_ice.py",
  "            self.svc.report(\"SHOW_ENDED\" if cue[\"confirmed\"]\n"
  "                            else \"SHOW_FAILED\", how, show=n)",
  "            self.svc.report(\"SHOW_ENDED\", how, show=n)"),

 ("fire & ice: the show is confirmed before the timecode moves",
  "ltcplay/fire_ice.py",
  "        if not cue[\"confirmed\"] and mine and \\\n"
  "                getattr(clk, \"_last_frame\", None) not in (None, 0):",
  "        if not cue[\"confirmed\"] and mine:"),

 ('fire & ice: the laser gate does not follow the scheduler',
  'ltcplay/fire_ice.py',
  '    state_gate = C.laser_gate_for(state)',
  '    state_gate = lambda: None'),

 ("fire & ice: closing is never reported done",
  "ltcplay/fire_ice.py",
  "        self.svc.report(\"CLOSING_DONE\",",
  "        (lambda *a: None)(\"CLOSING_DONE\","),

 # (show-assembly: #32's show start now runs on #30's ordered line, so this
 # one targets where the conductor is told a show started: only once it is
 # confirmed, never at the start.)
 ("fire & ice: the conductor is told a show started before its audio is",
  "ltcplay/schedule_service.py",
  "        if ev.kind == sch.SHOW_CONFIRMED and \\\n",
  "        if (ev.kind == sch.SHOW_CONFIRMED or sch.START_SHOW in kinds) and \\\n"),

 ("fire & ice: a performing scheduler still ends shows on its own clock",
  "ltcplay/schedule_service.py",
  "            if self.dry_run and m.state == sch.SHOW and \\",
  "            if m.state == sch.SHOW and \\"),

 ("fire & ice: a performing scheduler still finishes closing by itself",
  "ltcplay/schedule_service.py",
  "        if self.dry_run and self.machine.state == sch.CLOSING:",
  "        if self.machine.state == sch.CLOSING:"),

 ("fire & ice: a report is applied during a dry run",
  "ltcplay/schedule_service.py",
  "            if self.dry_run or self.machine is None:\n"
  "                return None\n            ev = sch.Event(",
  "            if self.machine is None:\n"
  "                return None\n            ev = sch.Event("),

 ("fire & ice: GPL serve builds the conductor too",
  "ltcplay/cli.py",
  "    fire_ice = None\n    if schedule is not None:\n",
  "    fire_ice = None\n    if True:\n"),

 ("fire & ice: the scheduler is never given the conductor's config",
  "ltcplay/cli.py",
  "                              announce=announce, fire_ice=fire_ice,\n",
  "                              announce=announce,\n"),

 ("audio_master per-call fade: pause ignores the fade it is given",
  "ltcplay/clock.py",
  "            ms = self.audio.hold_fade_ms if fade_ms is None else fade_ms",
  "            ms = self.audio.hold_fade_ms"),

 ("audio_master per-call fade: halt ignores the fade it is given",
  "ltcplay/clock.py",
  "            ms = self.audio.abort_fade_ms if fade_ms is None else fade_ms",
  "            ms = self.audio.abort_fade_ms"),

 ("audio_master per-call fade: a resume's fade is left for the next one",
  "ltcplay/clock.py",
  "            self._resume_fade_ms = fade_ms\n",
  "            if fade_ms is not None:\n"
  "                self._resume_fade_ms = fade_ms\n"),

 ("audio_master per-call fade: resume ignores the fade it is given",
  "ltcplay/clock.py",
  "        fade = self._sa.fade_frames(self.audio.hold_fade_ms\n"
  "                                    if self._resume_fade_ms is None\n"
  "                                    else self._resume_fade_ms)",
  "        fade = self._sa.fade_frames(self.audio.hold_fade_ms)"),

 # -- show-assembly (2026-10-03): the performer on #30's ordered line, the
 # auto_start gate, and the flame link built from flamesafe's config.
 ('show-assembly: the performer starts a show in a dry run',
  'ltcplay/schedule_service.py',
  '        if sch.START_SHOW in kinds and not self.dry_run and \\\n',
  '        if sch.START_SHOW in kinds and \\\n'),

 ('show-assembly: an Abort no longer supersedes a waiting show start',
  'ltcplay/schedule_service.py',
  '    SUPERSEDED_BY_ABORT = ("reset", "hold", "resume", "show_starting",\n                           "start_show")',
  '    SUPERSEDED_BY_ABORT = ("reset", "hold", "resume", "show_starting")'),

 ('show-assembly: the show start goes to the conductor, not the runner',
  'ltcplay/schedule_service.py',
  '            if call.method == "start_show":\n                # The Fire',
  '            if call.method == "start_shoe":\n                # The Fire'),

 ('show-assembly: a refused automatic start is not journaled as refused',
  'ltcplay/schedule_service.py',
  '                    outcome="done" if ok else "refused",',
  '                    outcome="done",'),

 ('show-assembly: auto_start off is ignored',
  'ltcplay/fire_ice.py',
  '        if auto and self.cfg.auto_start == "off":',
  '        if auto and self.cfg.auto_start == "never":'),

 ("show-assembly: an operator's Start now counts as automatic",
  'ltcplay/fire_ice.py',
  '        auto = who == "the scheduler"',
  '        auto = True'),

 ('show-assembly: an Active flame controller is accepted',
  'ltcplay/fire_ice.py',
  '    if c.attrib.get("ActiveState", "Active") == "Active":',
  '    if c.attrib.get("ActiveState", "Active") == "Never":'),

 ('show-assembly: flame channels are counted from 0',
  'ltcplay/fire_ice.py',
  '    import xml.etree.ElementTree as ET\n    root = ET.parse(networks_xml).getroot()\n    chan = 1',
  '    import xml.etree.ElementTree as ET\n    root = ET.parse(networks_xml).getroot()\n    chan = 0'),

 ('show-assembly: flame cues read while nothing is running',
  'ltcplay/fire_ice.py',
  '        if s is None or not getattr(s, "running", False):\n            return self._zero("")',
  '        if s is None:\n            return self._zero("")'),

 ("show-assembly: the screen Abort's disarm skips the flame link",
  'ltcplay/fire_ice.py',
  '        if self.flame_link is not None:\n            self.flames = C.ZERO\n            try:\n                ok = self.flame_link.disarm_all(reason)',
  '        if False:\n            self.flames = C.ZERO\n            try:\n                ok = self.flame_link.disarm_all(reason)'),

 ('show-assembly: a disarm that did not go out counts as done',
  'ltcplay/fire_ice.py',
  '            return C.done(f"{reason}: {DISARM_SENT}") if ok is True else \\',
  '            return C.done(f"{reason}: {DISARM_SENT}") if True else \\'),

 ("show-assembly: the flame link key is not flamesafe's own",
  'ltcplay/fire_ice.py',
  '        return flamelink.FlameLinkConfig.from_flamesafe_config(\n            cfg.flamesafe_config)',
  '        c = flamelink.FlameLinkConfig.from_flamesafe_config(\n            cfg.flamesafe_config)\n        c.key = c.key[::-1]\n        return c'),

 ('show-assembly: closing does not zero and stop the flame link',
  'ltcplay/fire_ice.py',
  '            if fl is not None and hasattr(fl, "stop"):',
  '            if False:'),

 ('show-assembly: flame_controller allowed without a flame link',
  'ltcplay/fire_ice.py',
  '            if fs is None:\n                raise FireIceConfigError(',
  '            if False:\n                raise FireIceConfigError('),

 ('show-assembly: the status mirror does not reach the flame link',
  'ltcplay/web.py',
  '            if obj is not None:\n                _link.note_status(obj)\n',
  '            if obj is not None:\n                pass\n'),

 ('show-assembly: the flame link journals on its own sender thread',
  'ltcplay/fire_ice.py',
  '    journal = OffThreadJournal(journal) if journal is not None else None\n',
  '    journal = journal\n'),


 # -- show-assembly fix round 1 (PR #43 independent review, 2026-10-03).
 ('fix round 1: flame cues follow Blackout, Preshow or a look',
  'ltcplay/fire_ice.py',
  '        if look is not None:\n            return self._zero(',
  '        if False:\n            return self._zero('),

 ('fix round 1: flame cues follow a GO free run',
  'ltcplay/fire_ice.py',
  '        if getattr(p, "freerun_epoch", None) is not None:\n            return self._zero(',
  '        if False:\n            return self._zero('),

 ('fix round 1: flame cues go on while the show audio is paused',
  'ltcplay/fire_ice.py',
  '        if getattr(clk, "source", None) != "audio_master" or not cue or \\\n                getattr(clk, "paused", True):',
  '        if getattr(clk, "source", None) != "audio_master" or not cue:'),

 ("fix round 1: flame cues take any timecode, not the clock's own frame",
  'ltcplay/fire_ice.py',
  '        if not tc or not last or tc != (f"{last[0]:02d}:{last[1]:02d}:"',
  '        if not tc or not last or False and tc != (f"{last[0]:02d}:{last[1]:02d}:"'),

 ('fix round 1: flame cues read whichever cue, not the one playing',
  'ltcplay/fire_ice.py',
  '        hits = [c for c in (getattr(tl, "cues", None) or ())\n                if getattr(c, "name", None) == label]',
  '        hits = [c for c in (getattr(tl, "cues", None) or ())]'),

 ('fix round 1: flame cues read one frame late',
  'ltcplay/fire_ice.py',
  '            idx = int(rel * 1000.0 // f.step_time_ms)',
  '            idx = int(rel * 1000.0 // f.step_time_ms) + 1'),

 ('fix round 1: the page transport works during a live show',
  'ltcplay/web.py',
  '        if route in LIVE_SHOW_REFUSED and self._scheduled_show_live() and \\',
  '        if False and \\'),

 ('fix round 1: a held show is not live for the page transport',
  'ltcplay/web.py',
  'LIVE_SHOW_STATES = ("SHOW", "PAUSED")',
  'LIVE_SHOW_STATES = ("SHOW",)'),

 ('fix round 1: GO is not refused during a live show',
  'ltcplay/web.py',
  'LIVE_SHOW_REFUSED = ("/api/start", "/api/go", "/api/skip",',
  'LIVE_SHOW_REFUSED = ("/api/start", "/api/goo", "/api/skip",'),

 ('fix round 1: Run does not check the show before opening it',
  'ltcplay/web.py',
  '        if check is not None:\n            # Fire & Ice',
  '        if False:\n            # Fire & Ice'),

 ('fix round 1: the session keeps excluded controllers',
  'ltcplay/session.py',
  '        if self.exclude_controllers or self.exclude_destinations:\n            def _out(u):',
  '        if False:\n            def _out(u):'),

 ("fix round 1: Fire & Ice sessions keep the flame controller",
  'ltcplay/fire_ice.py',
  '        defaults["exclude_controllers"] = (cfg.flame_controller,)',
  '        pass'),

 ('fix round 1: an Active flame controller is not refused at Run',
  'ltcplay/fire_ice.py',
  '            except FlameControllerError as e:\n                raise SessionError(f"This show will not start: {e}")',
  '            except FlameControllerError as e:\n                blocked = set()'),

 ('fix round 1: ltc serve starts with an Active flame controller',
  'ltcplay/fire_ice.py',
  '            raise FireIceConfigError(f"{n}: {e}")',
  '            pass'),

 ('fix round 1: no flame_controller is not said at startup',
  'ltcplay/fire_ice.py',
  '    elif cfg.flamesafe_config and journal is not None:',
  '    elif False:'),


 ('fix round 1: the conductor is not told the music started',
  'ltcplay/fire_ice.py',
  '        if told is not None:\n            told()',
  '        if False:\n            told()'),

 ('fix round 1: music_started records nothing',
  'ltcplay/conductor.py',
  '            self._set("music", MUSIC_PLAYING)\n\n    def intermission',
  '            pass\n\n    def intermission'),

 ('fix round 1: an Abort during the start leaves the music playing',
  'ltcplay/fire_ice.py',
  '            self.show.music_halt(C.ABORT_FADE_S)',
  '            pass'),


 ('fix round 1: the flame frames never raise an unconfirmed disarm',
  'ltcplay/flamelink.py',
  '            late = self._abort_overdue()\n        if late is not None:\n            self._note_unconfirmed(late)\n        obs = self.observer',
  '            late = None\n        if late is not None:\n            self._note_unconfirmed(late)\n        obs = self.observer'),

 ('fix round 1: status frames never raise an unconfirmed disarm',
  'ltcplay/flamelink.py',
  '                    with self._lock:\n                        late = self._abort_overdue()',
  '                    with self._lock:\n                        late = None'),

 ('fix round 1: an unconfirmed disarm is overdue only after 10 s',
  'ltcplay/flamelink.py',
  '        if self._clock() - pend[1] > CONFIRM_S:',
  '        if self._clock() - pend[1] > CONFIRM_S * 10:'),


 ('fix round 1: an unknown video level fades down from full',
  'ltcplay/conductor.py',
  '        if not levels:\n            return 0.0',
  '        if not levels:\n            return 1.0 if end <= 0.0 else 0.0'),



 ('fix round 1: fire_ice imports the flame link outside its two builders',
  'ltcplay/fire_ice.py',
  'def _has_status_mirror(path):\n    try:',
  'def _has_status_mirror(path):\n    from . import flamelink  # noqa: F401\n    try:'),


 ("fix round 1: the deck does not read latched from its own Abort",
  'ltcplay/streamdeck.py',
  '        with self._lock:\n            self._local = (True, self._clock())\n        return self._press("abort", who, screen)',
  '        return self._press("abort", who, screen)'),

 ("fix round 1: the deck's Abort to the engine is not confirmed",
  'ltcplay/streamdeck.py',
  '        if name == "abort":\n            body["confirmed"] = True',
  '        if name == "abort":\n            pass'),

 ("fix round 1: an engine answer older than the deck's press wins",
  'ltcplay/streamdeck.py',
  '        if engine is not None and (pressed_at is None or\n                                   engine[1] > pressed_at):',
  '        if engine is not None:'),

 ('fix round 1: an engine refusal of a deck press is not a fault',
  'ltcplay/streamdeck.py',
  '            self._journal(line, fault=not (ok or noop), action=name,',
  '            self._journal(line, fault=False, action=name,'),


 ('fix round 1: the background show log writes on the caller',
  'ltcplay/fire_ice.py',
  "        self._log.handlers[:] = [logging.handlers.QueueHandler(q)]",
  "        pass"),

 ('fix round 1: the background show log echoes on the caller',
  'ltcplay/fire_ice.py',
  "        super().__init__(path, echo=False, **kw)",
  "        super().__init__(path, echo=echo, **kw)"),

 ('fix round 1: Fire & Ice sessions log on the caller',
  'ltcplay/fire_ice.py',
  '    defaults["log_factory"] = BackgroundShowLog',
  '    pass'),

 ('fix round 1: the session drops log_factory',
  'ltcplay/session.py',
  '                self.log = (self.log_factory or ShowLog)(',
  '                self.log = (ShowLog)('),

 # -- PR #43 independent review, fix round 1, item 10: the reviewer's hand
 # mutations that survived the full suite.
 ('review H4: the status mirror feeds the flame link frames with ANY key',
  'ltcplay/web.py',
  '            obj = remote_mod.decode_status(data, fstatus.key)',
  '            import json as _j\n            try:\n                obj = _j.loads(data)\n            except ValueError:\n                obj = None'),

 ('review H5: the runner confirms/ends on a cue that is not the one it started',
  'ltcplay/fire_ice.py',
  '        mine = clk.playing and clk.cues_played == cue["played"]',
  '        mine = clk.playing'),

 ("review H6: the show number never reaches the runner's start_show",
  'ltcplay/schedule_service.py',
  '            if len(entry) > 3:\n                call.show = entry[3]\n',
  ''),

 ('review H7: ltc serve opens the flame link but never starts its sender thread',
  'ltcplay/fire_ice.py',
  '        if threaded:\n            built_link.start()\n            # The flame cues\' reading thread from now on, not from the\n            # first flame frame of a show (soak 2026-10-21).\n            if isinstance(getattr(built_link, "cues", None), FlameCues):\n                built_link.cues.start()\n        else:\n            built_link.open()',
  '        built_link.open()'),

 ('review H9: a release on a closed flame link counts as done',
  'ltcplay/fire_ice.py',
  '        return C.done("Flame cues released.") if ok is True else \\\n            C.failed("Flame cues release did not go out.")',
  '        return C.done("Flame cues released.")'),

 ("review H10: ltc serve no longer checks flamesafe's config before binding",
  'ltcplay/cli.py',
  '            fire_ice_mod.flame_link_config(fire_ice)\n',
  ''),

 ('review H11: a flame controller with no ActiveState attribute',
  'ltcplay/fire_ice.py',
  '    if c.attrib.get("ActiveState", "Active") == "Active":',
  '    if c.attrib.get("ActiveState", "Inactive") == "Active":'),

 ('review H12: closing reports done without zeroing flames, blanking lasers or blacking the pixels',
  'ltcplay/fire_ice.py',
  '        if state == "CLOSING":\n            if not self._closing_reported:\n                self._closing_reported = True\n                self._close()\n            return',
  '        if state == "CLOSING":\n            if not self._closing_reported:\n                self._closing_reported = True\n                self.svc.report("CLOSING_DONE", "x")\n            return'),




 ('fix round 1: an Abort with no operator chosen is refused',
  'ltcplay/schedule.py',
  'ALWAYS_TAKEN = frozenset((ABORT, HOLD_ON, STOP_SHOW))',
  'ALWAYS_TAKEN = frozenset((HOLD_ON, STOP_SHOW))'),

 ('fix round 1: a Hold with no operator chosen is refused',
  'ltcplay/schedule.py',
  'ALWAYS_TAKEN = frozenset((ABORT, HOLD_ON, STOP_SHOW))',
  'ALWAYS_TAKEN = frozenset((ABORT, STOP_SHOW))'),

 ("fix round 1: the service refuses an Abort by a name not on the list",
  'ltcplay/schedule_service.py',
  '        if always and who.lower() not in names:',
  '        if False and who.lower() not in names:'),

 ('fix round 1: an Abort with no operator chosen is journaled as the operator',
  'ltcplay/schedule.py',
  '    return f"{what} pressed{_screen(ev)} with no operator chosen"',
  '    return f"The operator pressed {what}{_screen(ev)}"'),


 # -- PR #43 fix round 2 (second independent review, 2026-10-03).
 ('fix round 2: the flame channels are cached per show folder for good',
  'ltcplay/fire_ice.py',
  '        if self._folder is not None and self._folder[0] is session and \\\n                self._folder[1:] == (path, stamp):',
  '        if self._folder is not None:'),

 ('fix round 2: a render made for another layout is read',
  'ltcplay/fire_ice.py',
  '            if total is None or (have != total if whole',
  '            if False and (have != total if whole'),

 ('R2-H9 a re-rendered FSEQ is never reopened by the flame cues',
  'ltcplay/fire_ice.py',
  '            key = (path, st.st_mtime_ns, st.st_size)',
  '            key = (path, None, None)'),

 ('R2-H11 flame cues take the first of two cues with the playing name',
  'ltcplay/fire_ice.py',
  '        if len(hits) != 1:\n            return self._zero(f"the show audio is playing',
  '        if not hits:\n            return self._zero(f"the show audio is playing'),

 ('fix round 2: a flame controller that is not there is not refused',
  'ltcplay/fire_ice.py',
  '    flame_channels(path, name)\n    blocked = ',
  '    try:\n        flame_channels(path, name)\n    except FlameControllerActive:\n        raise\n    except FlameControllerError:\n        pass\n    blocked = '),

 ("fix round 2: an Active controller at the flame node's address is not refused",
  'ltcplay/fire_ice.py',
  '        if (u.ip, u.universe, u.protocol) in blocked:\n            raise FlameControllerActive(',
  '        if False:\n            raise FlameControllerActive('),

 ("fix round 2: flamesafe's destination is not guarded",
  'ltcplay/fire_ice.py',
  '    if fs_dest:\n        blocked.add(tuple(fs_dest))',
  '    if False:\n        blocked.add(tuple(fs_dest))'),

 ('fix round 2: two controllers with the flame name are taken',
  'ltcplay/fire_ice.py',
  '    if len(found) > 1:',
  '    if len(found) > 99:'),

 ("fix round 2: the session keeps the flame node's address in the pixel map",
  'ltcplay/session.py',
  '                return (u.controller in self.exclude_controllers or\n                        (u.ip, u.universe, u.protocol) in\n                        self.exclude_destinations)',
  '                return u.controller in self.exclude_controllers'),

 ("fix round 2: Run does not hand the session the addresses to leave out",
  'ltcplay/web.py',
  '            if isinstance(extra, dict):\n                kw.update(extra)',
  '            if False:\n                kw.update(extra)'),


 ('fix round 2: a group key does nothing while aborted',
  'ltcplay/streamdeck.py',
  '                    if self.arm.wanted[i] or self._reported_armed(i):\n                        self._do_disarm(i)',
  '                    if False:\n                        self._do_disarm(i)'),

 ('Deck latched: a tap on a group still reported armed does not disarm it',
  'ltcplay/streamdeck.py',
  '                    if self.arm.wanted[i] or self._reported_armed(i):',
  '                    if self.arm.wanted[i]:'),

 ('Deck latched: a group still reported armed greys out',
  'ltcplay/streamdeck.py',
  '                if st is not None and not fault and \\\n                        st.get("armed") != "armed":',
  '                if st is not None and not fault:'),

 ('Deck latched: a group that is off keeps its colour',
  'ltcplay/streamdeck.py',
  '                    look = (look[0], look[1], LATCHED_GREY, DIM_TEXT, False)',
  '                    pass'),

 ('Reset design: the Hold key Resets while aborted',
  'ltcplay/streamdeck.py',
  '                elif k in self._key_group:\n                    i = self._key_group[k]\n                    if self.arm.wanted[i] or self._reported_armed(i):',
  '                elif k == TOP_HOLD:\n                    self._do_reset()\n                elif k in self._key_group:\n                    i = self._key_group[k]\n                    if self.arm.wanted[i] or self._reported_armed(i):'),

 ('fix round 2: a deck Abort waits in line behind other presses',
  'ltcplay/streamdeck.py',
  '            body["confirmed"] = True\n            # Abort never waits',
  '            body["confirmed"] = True\n        if False:\n            # Abort never waits'),

 ("fix round 2: an engine that did not take a press is not shown on the deck",
  'ltcplay/streamdeck.py',
  '            self.fault = "" if (ok or noop) else f"{name.title()}: {text}"',
  '            self.fault = ""'),

 ('fix round 2: the page can bring the rig up while an Abort stands',
  'ltcplay/web.py',
  '                self._abort_latched():\n            return self._send(409, {"error": LATCHED_REFUSAL})',
  '                False:\n            return self._send(409, {"error": LATCHED_REFUSAL})'),

 ("fix round 2: a press's answer says nothing was disarmed",
  'ltcplay/schedule_service.py',
  '        text = " ".join(self._reworded(le).text for le in out.log if le.text)',
  '        text = " ".join(le.text for le in out.log if le.text)'),

 ('fix round 2: a look chosen before the show stays on',
  'ltcplay/fire_ice.py',
  '        self._clear_look(s, n)\n',
  ''),

 ("R2-H1 the page's Blackout/Preshow (override) is not refused during a live show",
  'ltcplay/web.py',
  '"/api/stop", "/api/override", "/api/reload",',
  '"/api/stop", "/api/reload",'),

 ("R2-H2 the page's Stop is not refused during a live show",
  'ltcplay/web.py',
  '"/api/stop", "/api/override", "/api/reload",',
  '"/api/override", "/api/reload",'),

 ("R2-H3 the page's Run (/api/start) is not refused during a live show",
  'ltcplay/web.py',
  'LIVE_SHOW_REFUSED = ("/api/start", "/api/go",',
  'LIVE_SHOW_REFUSED = ("/api/go",'),

 ("R2-H4 the page's showdir/reinput are not refused during a live show",
  'ltcplay/web.py',
  '"/api/showdir", "/api/reinput", "/api/input",',
  '"/api/input",'),


 ('show PC 2026-10-04: a late flame frame is never journaled',
  'ltcplay/flamelink.py',
  '        if gap < self.LATE_S:\n            return',
  '        if True:\n            return'),

 ('show PC 2026-10-04: the idle deck draws every pass',
  'ltcplay/streamdeck.py',
  '                        t0 - last_draw >= DRAW_IDLE_S or motion != drawn:',
  '                        True:'),

 ('show PC 2026-10-04: a held key is drawn only every DRAW_IDLE_S',
  'ltcplay/streamdeck.py',
  '                    if motion != drawn or controller.animating():\n',
  '                    if motion != drawn:\n'),


 ('SSD 2026-10-04: the flame link sender reads the render itself',
  'ltcplay/fire_ice.py',
  '                got = self._by_path.get(hits[0].path)',
  '                got = self._render(hits[0].path)'),

 ("SSD 2026-10-04: ltc serve's flame cues read files on the sender",
  'ltcplay/fire_ice.py',
  '    cues = (FlameCues(control, cfg.flame_controller, journal,\n                      background=True)',
  '    cues = (FlameCues(control, cfg.flame_controller, journal,\n                      background=False)'),


 ('MSIX 2026-10-04: a copy holding the named lock elsewhere is not seen',
  'ltcplay/onlyone.py',
  '        handle, existed = got\n        if existed:',
  '        handle, existed = got\n        if False:'),

 ("flame groups: one channel in two groups passes at start",
  "ltcplay/fire_ice.py",
  "            if s in owner and owner[s] != name:",
  "            if False:"),

 ("flame groups: a head the layout does not have passes at start",
  "ltcplay/fire_ice.py",
  "            if not 1 <= s <= count:",
  "            if False:"),

 ("flame groups: ltc serve never checks them",
  "ltcplay/cli.py",
  "            fire_ice_mod.check_flame_groups(\n",
  "            (lambda *a: None)(\n"),

 ("beyond blanking: the show's timecode sender ignores the divert",
  "ltcplay/clock.py",
  "            if DIVERT:\n                d = DIVERT.get(str(label).lower()) or DIVERT.get(ip)",
  "            if False:\n                d = DIVERT.get(str(label).lower()) or DIVERT.get(ip)"),

 ("beyond blanking: a node at BEYOND's address by another name is not diverted",
  "ltcplay/clock.py",
  "                d = DIVERT.get(str(label).lower()) or DIVERT.get(ip)",
  "                d = DIVERT.get(str(label).lower())"),

 ("beyond blanking: the show's timecode reaches BEYOND while dark",
  "ltcplay/beyondtc.py",
  "            if not self.lit:\n                return True",
  "            if False:\n                return True"),

 ("beyond blanking: a blank waits for the next black frame",
  "ltcplay/beyondtc.py",
  "                self._zone_start = self._clock()\n        return self.send_black()",
  "                self._zone_start = self._clock()\n        return True"),

 ("beyond blanking: the black zone freezes",
  "ltcplay/beyondtc.py",
  "        n = int((self._clock() - self._zone_start) * FPS)",
  "        n = 0"),

 ("beyond blanking: a black frame that could not be sent counts as dark",
  "ltcplay/beyondtc.py",
  "                           f\"not dark.\", fault=True, outcome=\"send_failed\")\n            return False",
  "                           f\"not dark.\", fault=True, outcome=\"send_failed\")\n            return True"),

 ("beyond blanking: timecode mode never moves the stream",
  "ltcplay/beyondtc.py",
  "        if self._uses(\"timecode\"):\n            sent = self.gate is not None and self.gate.dark()",
  "        if False:\n            sent = self.gate is not None and self.gate.dark()"),

 ("beyond blanking: Resume never brings BEYOND's stream back",
  "ltcplay/beyondtc.py",
  "            ok = self.gate is not None and self.gate.light(epoch)",
  "            ok = self.gate is not None"),

 ("beyond blanking: unblank takes something that only looks true",
  "ltcplay/beyondtc.py",
  "        if in_show is not True:\n            return False",
  "        if not in_show:\n            return False"),

 ("beyond blanking: both mode skips the OSC blank",
  "ltcplay/beyondtc.py",
  "        if self._uses(\"osc\"):\n            sent = self._osc_call(\"blank\", show)",
  "        if self.mode == \"osc\":\n            sent = self._osc_call(\"blank\", show)"),

 ("beyond blanking: an unknown beyond_blank is taken",
  "ltcplay/fire_ice.py",
  "        if blank not in (\"timecode\", \"osc\", \"both\"):",
  "        if False:"),

 ("beyond blanking: attach() keeps the plain OSC BEYOND",
  "ltcplay/fire_ice.py",
  "    devices = C.ConductorDevices(link, blanking, journal=journal)",
  "    devices = C.ConductorDevices(link, beyond, journal=journal)"),

 ("beyond blanking: a started gate is not registered by address",
  "ltcplay/beyondtc.py",
  "            _clock().DIVERT[self.ip] = self.divert",
  "            pass"),

 ("fire & ice: / still serves the old operator page",
  "ltcplay/web.py",
  "            if route in (\"/\", \"/index.html\") and \\\n                    getattr(self.server, \"fire_ice_config\", None) is not None:",
  "            if False:"),

 ("deck: a new urllib opener for every request",
  "ltcplay/streamdeck.py",
  "    if not _OPENER:\n        _OPENER.append(",
  "    if True:\n        _OPENER.append("),

 # -- review of PR #43 (2026-10-05): P0-1, the screen Abort disarms first, every time --
 ('review43: the screen Abort sends no disarm of its own',
  'ltcplay/remote.py',
  '            flames = self._disarm_now(who, screen, "Abort")',
  '            flames = (None, "")'),

 ('review43: a disarm that did not go out reads as a done Abort',
  'ltcplay/remote.py',
  '        ok = f_ok is True or (f_ok is None and s_ok)',
  '        ok = True'),


 # -- review of PR #43: P0-5, the screen Abort's disarm never waits on the tonight.json save --
 ("review43: the screen Abort's disarm waits on the scheduler's save",
  'ltcplay/remote.py',
  '            flames = self._disarm_now(who, screen, "Abort")',
  '            with self.schedule.lock:\n                self.schedule.machine and self.schedule._save_tonight()\n            flames = self._disarm_now(who, screen, "Abort")'),

 # -- review of PR #43: P0-1, the deck says what the engine's Abort did --
 ('review43: the deck reads RESET after an Abort that stopped no show',
  'ltcplay/streamdeck.py',
  '        if name == "abort" and doc.get("stopped") is False:',
  '        if False:'),

 # -- review of PR #43: P0-2, the lasers' timecode blank fails closed --
 ('review43: a black zone with no address answers True again',
  'ltcplay/beyondtc.py',
  '            self.send_errors += 1\n            self.last_error = "no address for BEYOND\'s timecode"\n',
  '            return True\n'),

 ('review43: broadcast timecode passes the BEYOND route check',
  'ltcplay/fire_ice.py',
  '    if art.broadcast:\n        raise FireIceConfigError(',
  '    if False:\n        raise FireIceConfigError('),

 ("review43: Run never checks BEYOND's timecode route",
  'ltcplay/fire_ice.py',
  '        opens.append(laser_check)\n',
  '        pass\n'),

 # -- review of PR #43: P0-3, a restore never outruns an Abort's blank --
 ("review43: the gate lights whatever blank ran since the restore's check",
  'ltcplay/beyondtc.py',
  '            if epoch is not None and epoch != self._blank_epoch:',
  '            if False:'),

 ("review43: a blank does not move the gate's blank epoch",
  'ltcplay/beyondtc.py',
  '            self._blank_epoch += 1\n            if self.lit:\n                self.lit = False\n                self._zone_start = self._clock()\n        return self.send_black()',
  '            if self.lit:\n                self.lit = False\n                self._zone_start = self._clock()\n        return self.send_black()'),

 ('review43: the restore reads the blank epoch only as it lights',
  'ltcplay/beyondtc.py',
  '            ok = self.gate is not None and self.gate.light(epoch)',
  '            ok = self.gate is not None and self.gate.light(self.gate.epoch())'),

 ("review43: Abort's executor trusts a laser record that says black",
  'ltcplay/conductor.py',
  '        self._step(gen, "lasers", BLACK, "lasers blanked again", progress,\n                   self.devices.lasers_blank, force=True)\n',
  '        pass\n'),

 # -- review of PR #43: P0-4, screen arming is off unless switched on --
 ('review43: screen arming is on with no settings file again',
  'ltcplay/remote.py',
  '           "screen_arming": False, "purge_arming": True, "booth_alert": None,',
  '           "screen_arming": True, "purge_arming": True, "booth_alert": None,'),

 ('review43: arm-hold looks at the session before the switch',
  'ltcplay/remote.py',
  '        if not self.arming_enabled():\n            # First, before anything else',
  '        if False:\n            # First, before anything else'),

 ("review43: a screen's per-group Disarm is 'sent' with arming off",
  'ltcplay/remote.py',
  '        if not self.arming_enabled():\n            # The Stream Deck reads',
  '        if False:\n            # The Stream Deck reads'),

 ('review43: the engine reads any truthy screen_arming as on',
  'ltcplay/remote.py',
  '        if v is not True and v is not False:\n'
  '            raise ValueError(f"{path}: screen_arming must be true or false.")\n'
  '        out["screen_arming"] = v',
  '        out["screen_arming"] = bool(v)'),

 # -- review of PR #43: P0-6, a render older than the layout, or checked at another flame start, is refused --
 ('review43: a render older than xlights_networks.xml is read as flames',
  'ltcplay/fire_ice.py',
  '            if made is not None and laid is not None and made < laid:',
  '            if False:'),

 ('review43: the flame start a render was checked at is never compared',
  'ltcplay/fire_ice.py',
  '            if checked_at is not None and checked_at != span[0]:',
  '            if False:'),

 ("review43: a render's flame start is recorded at every read, not at its check",
  'ltcplay/fire_ice.py',
  '                f, spans, made, checked_at = got\n',
  '                f, spans, made, checked_at = got\n                checked_at = span[0]\n'),

 ("review43: the layout's date is never kept",
  'ltcplay/fire_ice.py',
  '        self._layout_mtime = stamp[0] if stamp else None\n',
  '        self._layout_mtime = None\n'),

 ('review43: a flame cue refusal that is a fault is not journaled as one',
  'ltcplay/fire_ice.py',
  '                           **({"fault": True} if fault else {}))',
  '                           **{})'),

 # -- review of PR #43: P1-3, flame cue refusals are faults on the screens --
 ('review43: a render that does not fit the layout is a quiet zero',
  'ltcplay/fire_ice.py',
  '                    f"flames cannot be told. Render the show again for this "\n                    f"layout", fault=True)',
  '                    f"flames cannot be told. Render the show again for this "\n                    f"layout")'),

 ("review43: a timecode past the render's end is a quiet zero",
  'ltcplay/fire_ice.py',
  '                                  "show\'s render", fault=True)',
  '                                  "show\'s render")'),

 ('review43: a render that cannot be read is a quiet zero',
  'ltcplay/fire_ice.py',
  '                              f"({type(e).__name__}: {e})", fault=True)',
  '                              f"({type(e).__name__}: {e})")'),

 ('review43: /api/conductor leaves out the flame cue fault',
  'ltcplay/web.py',
  '            out["flame_cues"] = {"fault": str(\n                (now() if now is not None else cues.fault) or "")}',
  '            pass'),

 ('review43: the rack screen is never told the flame cue fault',
  'ltcplay/remote.py',
  '        out["flames"]["cues_fault"] = self.cues_fault()',
  '        out["flames"]["cues_fault"] = ""'),

 ('review43: the deck never reads the flame cue fault',
  'ltcplay/streamdeck.py',
  '            self.cues_fault = str((cues or {}).get("fault") or "") \\\n                if isinstance(cues, dict) else ""',
  '            self.cues_fault = ""'),

 ('review43: the deck never draws the flame cue fault',
  'ltcplay/streamdeck.py',
  '        elif cues_fault:\n            # Review of PR #43, P1-3',
  '        elif False:\n            # Review of PR #43, P1-3'),

 # -- review of PR #43: P1-1, the lasers dark when the engine stops --
 ('review43: closing the BEYOND timecode gate sends no black frames',
  'ltcplay/beyondtc.py',
  '        sent = self.black_burst()\n',
  '        sent = 0\n'),

 ('review43: the closing black frames go out in one burst',
  'ltcplay/beyondtc.py',
  '            if i:\n                sleep(1.0 / FPS)\n',
  ''),

 ("review43: the engine's stop does not blank the lasers",
  'ltcplay/cli.py',
  '    _lasers_dark_first(httpd)\n',
  ''),

 ('review43: the closing black frames leave the gate lit',
  'ltcplay/beyondtc.py',
  '        with self._lock:\n            self._blank_epoch += 1\n            if self.lit:\n                self.lit = False\n                self._zone_start = self._clock()\n        sent = 0\n',
  '        sent = 0\n'),

 # -- review of PR #43: P1-4, one flamesafe config or ltc serve does not start --
 ('review43: differing flamesafe configs are accepted',
  'ltcplay/cli.py',
  '    if len({same(p) for _w, p in named}) > 1:',
  '    if False:'),

 ('review43: ltc serve starts on differing flamesafe configs',
  'ltcplay/cli.py',
  '    flamesafe_config, why = _one_flamesafe_config(args, fire_ice)\n    if why:\n',
  '    flamesafe_config, why = _one_flamesafe_config(args, fire_ice)\n    if False:\n'),

 ("review43: the Windows app's flamesafe config is ignored",
  'ltcplay/cli.py',
  '    env = os.environ.get(FLAMESAFE_ENV)\n',
  '    env = None\n'),

 ("review43: ltcplay_remote.json's flamesafe config is ignored",
  'ltcplay/cli.py',
  '        if rs.get("flamesafe_config"):\n            named.append(',
  '        if False:\n            named.append('),

 # -- review of PR #43: P1-5, the deck reads RESET after an engine restart --
 ("review43: /api/conductor forgets the scheduler's saved Abort latch",
  'ltcplay/web.py',
  '        snap["latched"] = bool(snap.get("latched")) or sched_latched',
  '        snap["latched"] = bool(snap.get("latched"))'),

 # -- review of PR #38: P2, the single-copy lock is machine wide or says so --
 ('review38: the fall back to Local is quiet',
  'ltcplay/onlyone.py',
  '    warn(f"the single-copy lock {name} could not be made machine wide "',
  '    (lambda *a: None)(f"the single-copy lock {name} could not be made machine wide "'),

 ('review38: the named lock tries Local first',
  'ltcplay/onlyone.py',
  '    h = create("Global\\\\" + name)',
  '    h = create("Local\\\\" + name)'),

 # -- review of PR #38 (2026-10-05): P1-2, a Windows shutdown or sign-out stops the show programs cleanly --
 ('review38: a shutdown query stops nothing',
  'packaging/windows/ltcwin.py',
  '            self._stop_and_wait("Windows is shutting down or signing out")',
  '            pass'),

 ('review38: the end of the session is answered before the stop is done',
  'packaging/windows/ltcwin.py',
  '        if not self.done.wait(self.wait_s):',
  '        if False:'),

 ('review38: the end of the session stops flamesafe first',
  'packaging/windows/supervisor.py',
  '    for name in reversed(PROGRAMS):\n        p = progs[name]\n        if p.alive():\n            log(f"Windows is ending',
  '    for name in PROGRAMS:\n        p = progs[name]\n        if p.alive():\n            log(f"Windows is ending'),

 ('review38: programs are restarted while the session ends',
  'packaging/windows/supervisor.py',
  '    ENDING.set()\n    breaker = breaker or send_ctrl_break',
  '    breaker = breaker or send_ctrl_break'),

 ('review38: flamesafe.exe makes no shutdown window',
  'packaging/windows/entry_flamesafe.py',
  '    ending = ltcwin.stop_cleanly_at_shutdown("flamesafe", stopped)\n',
  '    ending = None\n'),

 # -- review of PR #38: P2, the supervisor's lock is machine wide --
 ("review38: the supervisor's lock is per Windows session again",
  'packaging/windows/supervisor.py',
  'MUTEX_NAME = "Global\\\\LTCPlayerSupervisor"',
  'MUTEX_NAME = "LTCPlayerSupervisor"'),

 ("review38: another user's supervisor reads as not running",
  'packaging/windows/supervisor.py',
  '    return ctypes.get_last_error() != ERROR_FILE_NOT_FOUND',
  '    return False'),

 ("review38: a refused Global name is a running supervisor even when it is not there",
  'packaging/windows/supervisor.py',
  '        if err == ERROR_ACCESS_DENIED and _mutex_there(k, name):',
  '        if err == ERROR_ACCESS_DENIED:'),

 ("review38: the supervisor's fall back to Local is quiet",
  'packaging/windows/supervisor.py',
  '            if name == LOCAL_MUTEX_NAME:\n                log(',
  '            if False:\n                log('),

 # -- review of PR #38: P1-4, one flamesafe config for every show program --
 ("review38: the engine is not told the supervisor's flamesafe config",
  'packaging/windows/supervisor.py',
  '            env[ltcwin.FLAMESAFE_ENV] = os.path.abspath(',
  "            env['UNUSED'] = os.path.abspath("),

 ("review38: a flamesafe config left in the supervisor's environment reaches the engine",
  'packaging/windows/supervisor.py',
  '        env.pop(ltcwin.FLAMESAFE_ENV, None)\n',
  ''),

 # -- review of PR #38: P2, CI fix: a refused Global name is a running copy only if it is there; the file's note wins --
 ("review38: another user's copy makes the lock fall back to Local",
  'ltcplay/onlyone.py',
  '    if err == ERROR_ACCESS_DENIED and exists is not None and \\\n            exists("Global\\\\" + name):\n        return None, True\n',
  ''),

 ('review38: a refused Global name is a running copy even when it is not there',
  'ltcplay/onlyone.py',
  '    if err == ERROR_ACCESS_DENIED and exists is not None and \\\n            exists("Global\\\\" + name):',
  '    if err == ERROR_ACCESS_DENIED:'),

 ("review38: the named mutex's sentence hides the running copy's note",
  'ltcplay/onlyone.py',
  '                self._file_holder() or\n',
  '                "" or\n'),

 ('review38: onlyone.py has an invalid escape in a docstring again',
  'ltcplay/onlyone.py',
  '    r"""(handle or None, already_existed)',
  '    """(handle or None, already_existed)'),

 # -- review of PR #38: P1-2 follow-up, TRUE reaches Windows before the program ends --
 ('review38: the shutdown answer is never marked given',
  'packaging/windows/ltcwin.py',
  '            self._stop_and_wait("Windows is shutting down or signing out")\n            self.answered.set()\n',
  '            self._stop_and_wait("Windows is shutting down or signing out")\n'),

 ('review38: flamesafe.exe ends before Windows has its answer',
  'packaging/windows/entry_flamesafe.py',
  '            ending.let_windows_have_its_answer()',
  '            pass'),

 # -- re-review of #46: P1-A, the screen Abort's laser blank goes before the scheduler --
 ("rereview: the screen Abort's laser blank waits for the scheduler",
  'ltcplay/remote.py',
  '            lasers = self._lasers_dark_now()\n',
  '            lasers = (None, "")\n'),

 # -- review of PR #43 P0-2 re-anchored, and re-review P1-B, P2-a, P2-h: the BEYOND route check --
 ('review43: a show file with no BEYOND route passes the check',
  'ltcplay/fire_ice.py',
  '    if not hits and not lasers:\n        return None\n    if not hits:\n        raise',
  '    if not hits and not lasers:\n        return None\n    if not hits:\n        return None\n        raise'),

 ('review43: two BEYOND routes pass the check',
  'ltcplay/fire_ice.py',
  '    if len(hits) == 1:\n        if timecode_ip',
  '    if len(hits) >= 1:\n        if timecode_ip'),

 ("review43: ltc serve never checks BEYOND's timecode routes",
  'ltcplay/cli.py',
  '            for _line in fire_ice_mod.check_beyond_timecode_routes(\n',
  '            for _line in (lambda *a: [])(\n'),

 ("review43: the gate never takes the show file's BEYOND address",
  'ltcplay/fire_ice.py',
  '            _gate.adopt(got[1])\n',
  '            pass\n'),

 ('rereview: a BEYOND route check that cannot be made passes the show',
  'ltcplay/fire_ice.py',
  '            except Exception as e:\n                # Re-review P1-B',
  '            except Exception as e:\n                return None\n                # Re-review P1-B'),

 ('rereview: a show with no Art-Net timecode stops serve again',
  'ltcplay/fire_ice.py',
  '    if art is None:\n        return None\n    if art.broadcast:',
  '    if art is None:\n        raise FireIceConfigError("no timecode")\n    if art.broadcast:'),

 ('rereview: a bench with no lasers stops serve again',
  'ltcplay/fire_ice.py',
  '    if not hits and not lasers:\n        return None\n',
  ''),

 ("rereview: beyond_timecode_ip and the show file's BEYOND may differ",
  'ltcplay/fire_ice.py',
  '        if timecode_ip and hits[0][1] != timecode_ip:',
  '        if False:'),

 ('rereview: a show passed over for its laser route is not journaled',
  'ltcplay/fire_ice.py',
  '                if journal is not None:\n                    journal(no_route_sentence(',
  '                if False:\n                    journal(no_route_sentence('),

 # -- re-review of #46: P2-g, the engine's refusal reaches the supervisor --
 ("rereview: the flamesafe config refusal is only in the engine's log",
  'ltcplay/cli.py',
  '    if why:\n        _say_refused(why)\n',
  '    if why:\n'),

 # -- re-review of #47: P2-e and P2-g in the supervisor --
 ('rereview: the supervisor ends before its shutdown answer reaches Windows',
  'packaging/windows/supervisor.py',
  '    ending.done.wait(wait_s)\n    ending.let_windows_have_its_answer()\n',
  '    ending.done.wait(wait_s)\n'),

 ("rereview: the supervisor's loop skips the shutdown answer wait",
  'packaging/windows/supervisor.py',
  '            leave_at_end_of_session(ending)\n',
  '            pass\n'),

 ("rereview: the engine's refusal is not read",
  'packaging/windows/supervisor.py',
  '    return " ".join(l.strip() for l in lines[1:] if l.strip())',
  '    return ""'),

 ("rereview: the engine's refusal is said at every restart",
  'packaging/windows/supervisor.py',
  '    if not text or text == said[0]:',
  '    if not text:'),

 ("rereview: the supervisor never looks for the engine's refusal",
  'packaging/windows/supervisor.py',
  '                    say_engine_refused(engine_refusal(p.started_wall),\n                                       refused_said)\n',
  '                    pass\n'),

 # -- the deck's requests share the one no-proxy opener --
 ('deck: the screen holds are fetched with a fresh urlopen each time',
  'ltcplay/streamdeck.py',
  '    def _http_fetch(self, path):\n        try:\n            with _opener().open(self.base_url + path,',
  '    def _http_fetch(self, path):\n        try:\n            with urllib.request.urlopen(self.base_url + path,'),

 # -- review of #48: a Reset pressed before an Abort never ends it, whatever order they reach the scheduler --
 ('rereview: a Reset that reached the scheduler after a later-pressed Abort is still sent',
  'ltcplay/schedule_service.py',
  '            late = when < self._abort_pressed\n',
  '            late = False\n'),

 ("rereview: the screen's Abort and Reset carry no press order",
  'ltcplay/remote.py',
  '        pressed = stamp() if stamp is not None else None\n',
  '        pressed = None\n'),

 ("rereview: the Abort's press order is not kept",
  'ltcplay/schedule_service.py',
  '                self._abort_pressed = (self._press_stamp\n',
  '                self._abort_pressed = 0 and (self._press_stamp\n'),

 # -- #46's Windows run, 2026-10-08: a Reset the conductor answered while the
 # Abort pressed after it was still saving tonight.json found the latch set
 # but the Abort not yet numbered, and cleared it. --
 ("abort save: the Abort is numbered only after tonight is saved, not with "
  "its latch",
  'ltcplay/schedule_service.py',
  '        calls = self._decide_conductor(plan, ev)\n'
  '        if ev.kind == sch.STOP_SHOW and calls:',
  '        calls = None\n'
  '        if any(p[1] == "abort" for p in plan):\n'
  '            self.machine = replace(self.machine, abort_latched=True)\n'
  '            self._marker_clear_pending = False\n'
  '        if False:'),

 ("abort save: a step is taken outside the scheduler's lock",
  'ltcplay/schedule_service.py',
  '        with self._locked():\n            return self._apply_locked(ev, now)',
  '        if True:\n            return self._apply_locked(ev, now)'),

 # Look A, the deck artwork Jeff approved on the real deck (2026-09-27).
 ("deck look: the two snakes run opposite ways",
  "ltcplay/streamdeck.py",
  "        for head in (chase, chase + n // 2):\n",
  "        for head in (chase, n // 2 - chase):\n"),

 ("deck look: Abort fills only the deck's outside dots",
  "ltcplay/streamdeck.py",
  "            ring = key_ring(k)\n            for p in ring[:round(abort_frac * len(ring))]:\n",
  "            ring = [p for p in key_ring(k) if p in OUTER_DOTS]\n            for p in ring[:round(abort_frac * len(ring))]:\n"),

 ("deck look: the arm-hold ring fills red, like Abort",
  "ltcplay/streamdeck.py",
  "            for p in ring[:round(frac * len(ring))]:\n                lit[p] = GOLD\n",
  "            for p in ring[:round(frac * len(ring))]:\n                lit[p] = RED\n"),

 ("deck look: the solid outline round every key comes back",
  "ltcplay/streamdeck.py",
  "def draw_marquee(d, chase, abort_frac, arm_fills=None):\n",
  "def draw_marquee(d, chase, abort_frac, arm_fills=None):\n"
  "    for k in range(6):\n"
  "        ox, oy = key_origin(k)\n"
  "        d.rounded_rectangle((ox + 4, oy + 4, ox + K - 4, oy + K - 4),\n"
  "                            radius=7, outline=(92, 72, 32), width=3)\n"),

 ("deck look: the faces keep look B's 8 px margin",
  "ltcplay/streamdeck.py",
  "FACE_MARGIN = 12\n",
  "FACE_MARGIN = 8\n"),

 # The deck's motion is evidence of life (Jeff, 2026-10-05): it never
 # runs on the deck's own clock.
 ("deck liveness: the snakes run on the deck's own clock",
  "ltcplay/streamdeck.py",
  "        return 0 if ms is None else int(ms / (CHASE_STEP_S * 1000.0))\n",
  "        return int(self._clock() / CHASE_STEP_S)\n"),

 ("deck liveness: the top row flashes on the deck's own clock",
  "ltcplay/streamdeck.py",
  "        n = getattr(self, \"_answers\", 0)\n",
  "        n = int(self._clock() * 4)\n"),

 ("deck liveness: a re-read engine answer counts as a fresh one",
  "ltcplay/streamdeck.py",
  "        if at is not None and at != getattr(self, \"_answer_seen\", None):\n",
  "        if at is not None:\n"),

 ("deck liveness: the bottom row flashes on the deck's own clock",
  "ltcplay/streamdeck.py",
  "        return ms is None or int(ms / (BLINK_HALF_S * 1000.0)) % 2 == 0\n",
  "        return int(self._clock() * 2) % 2 == 0\n"),

 # The approved states the deck never drew before (Jeff, 2026-09-27): NOW
 # PLAYING, PAUSED, AUDIO LOST on the Start key; NO / FLAME / LINK.
 ("deck states: NOW PLAYING flashes on the deck's own clock",
  "ltcplay/streamdeck.py",
  "                bg, text = PLAYING_ON if blink_on else PLAYING_OFF\n",
  "                bg, text = PLAYING_ON if int(now * 2) % 2 == 0 "
  "else PLAYING_OFF\n"),

 ("deck states: START NOW stays on the Start key during a show",
  "ltcplay/streamdeck.py",
  "            elif running:\n                # The key says what the show is doing",
  "            elif False:\n                # The key says what the show is doing"),

 ("deck states: a held show reads NOW PLAYING, never PAUSED",
  "ltcplay/streamdeck.py",
  "            elif running and held:\n",
  "            elif False:\n"),

 ("deck states: AUDIO LOST never shows",
  "ltcplay/streamdeck.py",
  "            elif running and self._audio_lost():\n",
  "            elif False:\n"),

 ("deck states: AUDIO LOST is drawn steady",
  "ltcplay/streamdeck.py",
  "                bg, text = AUDIO_LOST_ON if blink_on else AUDIO_LOST_OFF\n",
  "                bg, text = AUDIO_LOST_ON\n"),

 ("deck states: the deck ignores the engine's audio field",
  "ltcplay/streamdeck.py",
  "                snap = dict(snap, audio=got.get(\"audio\"))\n",
  "                snap = dict(snap)\n"),

 ("deck states: a remembered audio loss outlives the engine's answers",
  "ltcplay/streamdeck.py",
  "        if engine is None:\n            return False\n"
  "        audio = engine[0].get(\"audio\")\n",
  "        if engine is None:\n"
  "            return getattr(self, \"_audio_was\", False)\n"
  "        audio = engine[0].get(\"audio\")\n"
  "        self._audio_was = bool(isinstance(audio, dict) "
  "and audio.get(\"lost\"))\n"),

 ("deck states: the missing link reads a dim flashing NO LINK again",
  "ltcplay/streamdeck.py",
  "        return NO_FLAME_LINK[slot % 3], None, RED, CHAMPAGNE, False\n",
  "        return \"NO\", \"LINK\", (26, 24, 21), DIM_TEXT, True\n"),

 ("deck states: the missing link flashes",
  "ltcplay/streamdeck.py",
  "        return NO_FLAME_LINK[slot % 3], None, RED, CHAMPAGNE, False\n",
  "        return NO_FLAME_LINK[slot % 3], None, RED, CHAMPAGNE, True\n"),

 ("deck states: the missing link's words stop at the groups that exist",
  "ltcplay/streamdeck.py",
  "            for i in range(len(self.names), len(GROUP_KEYS)):\n",
  "            for i in range(len(self.names), len(self.names)):\n"),

 ("deck states: the latched deck reads NO / NO / NO",
  "ltcplay/streamdeck.py",
  "                look = group_look(st, fault=fault, slot=slot)\n",
  "                look = group_look(st, fault=fault, slot=0)\n"),

 # The orange dot (Jeff, 2026-10-19): an unconfirmed config draws nothing
 # on a key; it is journaled once by the deck and carried in the engine's
 # status for the rack screen's Info popup instead.
 ("deck: the amber corner mark comes back on an unconfirmed config",
  "ltcplay/streamdeck.py",
  "                    look = (\"PURGE\", \"ARMED\") + tuple(look[2:])\n"
  "            arm_key_image(fonts, d, box, label, look, group_blink)\n",
  "                    look = (\"PURGE\", \"ARMED\") + tuple(look[2:])\n"
  "            arm_key_image(fonts, d, box, label, look, group_blink)\n"
  "            if not _confirmed:\n"
  "                d.rectangle((box[2] - 9, box[3] - 9, box[2] - 2,\n"
  "                             box[3] - 2), fill=AMBER)\n"),

 ("deck: an unconfirmed config is never journaled",
  "ltcplay/streamdeck.py",
  "        self._confirmed_noted = confirmed\n        if not confirmed:\n",
  "        self._confirmed_noted = confirmed\n        if False:\n"),

 ("deck: the unconfirmed config is journaled on every pass",
  "ltcplay/streamdeck.py",
  "        if confirmed == self._confirmed_noted:\n            return\n"
  "        self._confirmed_noted = confirmed\n",
  "        self._confirmed_noted = confirmed\n"),

 ("deck: a stale status still journals the config",
  "ltcplay/streamdeck.py",
  "        if self.status.last is None or self.status.stale(self._clock):\n"
  "            return\n        confirmed = bool(",
  "        if self.status.last is None:\n"
  "            return\n        confirmed = bool("),

 ("remote: the status never says flamesafe's config is unconfirmed",
  "ltcplay/remote.py",
  "                \"confirmed\": (bool(last.get(\"confirmed\", True))\n",
  "                \"confirmed\": (True\n"),

 ("remote: a stale status keeps the old confirmed answer",
  "ltcplay/remote.py",
  "                              if self.last is not None and not stale\n",
  "                              if self.last is not None\n"),

 ("audio field: /api/conductor never says the show audio is lost",
  "ltcplay/web.py",
  "                \"lost\": playing and mode == \"freerun\",\n",
  "                \"lost\": False,\n"),

 ("audio field: /api/conductor says lost between cues too",
  "ltcplay/web.py",
  "                \"lost\": playing and mode == \"freerun\",\n",
  "                \"lost\": mode == \"freerun\",\n"),

 ("audio field: /api/conductor says lost while the clock follows the audio",
  "ltcplay/web.py",
  "                \"lost\": playing and mode == \"freerun\",\n",
  "                \"lost\": playing,\n"),

 # -- lasers: OSC blanking with no OSC target is loud, never a quiet pass --
 ("lasers not set up: no BEYOND block reads as set up",
  "ltcplay/beyondtc.py",
  "        if self.osc is None:\n            return NOT_SET_UP",
  "        if False:\n            return NOT_SET_UP"),

 ("lasers not set up: a beyond block with no host reads as set up",
  "ltcplay/beyondtc.py",
  "        if cfg is not None and (not getattr(cfg, \"host\", None) or",
  "        if False and (not getattr(cfg, \"host\", None) or"),

 ("lasers not set up: the unconfigured OSC blank says done",
  "ltcplay/beyondtc.py",
  "        if self.fault is not None:\n            return False",
  "        if self.fault is not None:\n            return True"),

 ("lasers not set up: the standing fault is journaled as calm",
  "ltcplay/beyondtc.py",
  "                   fault=True, outcome=\"not_set_up\")",
  "                   fault=False, outcome=\"not_set_up\")"),

 ("lasers not set up: a raising OSC send escapes the blanking",
  "ltcplay/beyondtc.py",
  "            return getattr(self.osc, method)(show=show, **kw) is True\n"
  "        except Exception as e:",
  "            return getattr(self.osc, method)(show=show, **kw) is True\n"
  "        except ZeroDivisionError as e:"),

 ('lasers not set up: a raising OSC send is not a fault',
  'ltcplay/beyondtc.py',
  '            self._note(f"The OSC {method} to BEYOND failed "\n'
  '                       f"({type(e).__name__}: {e}).", fault=True,',
  '            self._note(f"The OSC {method} to BEYOND failed "\n'
  '                       f"({type(e).__name__}: {e}).", fault=False,'),

 ("lasers not set up: nothing journaled at startup",
  "ltcplay/fire_ice.py",
  "    blanking.note_not_set_up(\"at startup\")\n",
  ""),

 ("lasers not set up: nothing journaled at a show start",
  "ltcplay/fire_ice.py",
  "            _b.note_not_set_up(f\"show start, \"",
  "            (lambda *a: None)(f\"show start, \""),

 ("lasers not set up: the Lasers lamp reads ok anyway",
  "ltcplay/remote.py",
  "            if key == \"lasers\" and laser_fault:",
  "            if False:"),

 ("lasers not set up: the screen is never told",
  "ltcplay/remote.py",
  "            return str(getattr(b, \"fault\", \"\") or \"\")",
  "            return \"\""),


 # Safety audit of bench-build (2026-10-07): bench hooks never reach a show;
 # no disarm fault on every Abort with no status mirror; an unconfirmed
 # screen Abort still disarms first.
 ("the supervisor passes bench test hooks on to the show programs",
  "packaging/windows/supervisor.py",
  "        env = ltcwin.without_bench_hooks(os.environ)",
  "        env = dict(os.environ)"),

 ("LTC_TRACEMALLOC is no longer stripped by the supervisor",
  "packaging/windows/ltcwin.py",
  'BENCH_EXTRA = ("LTC_TRACEMALLOC",)',
  "BENCH_EXTRA = ()"),

 ("Run is never refused for a bench hook", "ltcplay/web.py",
  "        why = benchhooks.run_refusal()\n        if why:",
  "        why = benchhooks.run_refusal()\n        if False:"),

 ("any soak mark lets the bench hooks through", "ltcplay/benchhooks.py",
  "    return bool(mark) and mark == str(ppid)",
  "    return bool(mark)"),

 ("a bench hook set to anything is ignored unless it is FAKE_AUDIO",
  "ltcplay/benchhooks.py",
  "        if not is_hook(k) or k.upper() == SOAK_ENV:",
  "        if k.upper() != \"LTCPLAY_BENCH_FAKE_AUDIO\":"),

 ("with no status mirror every Abort raises the disarm fault again",
  "ltcplay/flamelink.py",
  "        if not self.status_mirrored:\n            return None\n",
  ""),

 ("with a status mirror an unconfirmed disarm is no longer a fault",
  "ltcplay/fire_ice.py",
  "    link.status_mirrored = _has_status_mirror(cfg.flamesafe_config)",
  "    link.status_mirrored = False"),

 ('an unconfirmed screen Abort no longer disarms the flames',
  'ltcplay/remote.py',
  '            flames = self._disarm_now(who, screen, "Abort")\n'
  '        if name in ("abort", "hold", "start-now"):',
  '            flames = None\n'
  '        if name in ("abort", "hold", "start-now"):'),

 ("MadMapper at High is left at High", "packaging/windows/supervisor.py",
  "HIGH_OR_ABOVE = (0x80, 0x100)", "HIGH_OR_ABOVE = ()"),

 ("the MadMapper priority watcher lowers every program it sees",
  "packaging/windows/supervisor.py",
  '            if c.which(exe)[0] != "MadMapper":\n                continue\n',
  ""),

 ("a refused MadMapper priority change is logged every 5 s",
  "packaging/windows/supervisor.py",
  "            elif pid not in self.refused:",
  "            else:"),

 ("showpc.json \"leave\" still lowers MadMapper",
  "packaging/windows/supervisor.py",
  '    if want == "leave":',
  '    if want == "never":'),

 # -- the Morning and Shutdown page (Jeff, 2026-10-17; MORNING.md) --------
 # The purge, the lockout and the laser tests: each one a way flames could
 # fire unintentionally, or fail to stop (MORNING.md's threat table).
 ("morning: a locked machine can be armed for a purge (T5)",
  "ltcplay/morning.py",
  '''        if self._lockout.is_locked("machine", machine_id):
            return 409, {"error": f"{m.name} is locked out, so it cannot "''',
  '''        if False:
            return 409, {"error": f"{m.name} is locked out, so it cannot "'''),

 # A group button (Jeff, 2026-10-28, show PC: front-row-4 locked out
 # stopped the group at 1 to 3, the button stayed grey and the second
 # click armed again instead of taking the group off).
 ("morning: a group button stops at the first member refused",
  "ltcplay/morning.py",
  """            elif out.get("error") not in refused:
                refused.append(out.get("error"))""",
  """            else:
                refused.append(out.get("error"))
                break"""),

 ("morning: a group button's off is treated as an arm",
  "ltcplay/morning.py",
  """        cfg = self._cfg()
        if not on:
            off = []""",
  """        cfg = self._cfg()
        if False:
            off = []"""),

 ("morning: a group button is green with a member locked out",
  "ltcplay/morning.py",
  """            state = ("on" if ms and len(on) == len(ms) and not waiting else""",
  """            state = ("on" if ms and len(on) >= len(ms) - len(locked) and not waiting else"""),

 ("morning: a group button is green before flamesafe armed it",
  "ltcplay/morning.py",
  """            state = ("on" if ms and len(on) == len(ms) and not waiting else""",
  """            state = ("on" if ms and len(on) == len(ms) else"""),

 ("morning page: a group click arms again unless every member is selected",
  "ltcplay/web/remote.html",
  "  const any = groupAnySelected(ids, MO.purge_state || {});\n"
  "  moPress(\"purge-select\", {machines: ids, group: g, on: !any});",
  "  const any = ids.every(id => ((MO.purge_state || {}).selected || []).includes(id));\n"
  "  moPress(\"purge-select\", {machines: ids, group: g, on: !any});"),

 ("morning page: a group button cannot disarm with purge arming off",
  "ltcplay/web/remote.html",
  "  return !ids.length || (!any && (!mo.purge_enabled || arrange || !mayRow(st, \"arm\")));",
  "  return !ids.length || !mo.purge_enabled || (!any && (arrange || !mayRow(st, \"arm\")));"),

 ("morning page: a group button cannot disarm in Arrange mode or signed out",
  "ltcplay/web/remote.html",
  "  return !ids.length || (!any && (!mo.purge_enabled || arrange || !mayRow(st, \"arm\")));",
  "  return !ids.length || arrange || !mayRow(st, \"arm\") || (!any && !mo.purge_enabled);"),

 ("morning page: a group button's words grow it and push the page down",
  "ltcplay/web/remote.html",
  ".mo-groups button .gd{display:block;max-width:100%;white-space:nowrap;",
  ".mo-groups button .gd{display:block;max-width:100%;white-space:normal;"),

 ("morning page: a group button flamesafe still holds armed is drawn grey",
  "ltcplay/web/remote.html",
  '    b.classList.toggle("part", gs.state === "part" || (any && gs.state !== "on"));',
  '    b.classList.toggle("part", any && gs.state !== "on");'),

 ("morning: a group flamesafe still holds armed shows grey once cleared",
  "ltcplay/morning.py",
  """            if still:
                words.append("flamesafe still armed " + ", ".join(still))""",
  """            still = []
            if still:
                words.append("flamesafe still armed " + ", ".join(still))"""),

 ("morning: a purge is allowed while a show is live or aborted (T1)",
  "ltcplay/morning.py",
  '''        why = self._live()
        if why:
            return why
        if not seen_ok:''',
  '''        why = ""
        if why:
            return why
        if not seen_ok:'''),

 ("morning: a stale page can still arm and fire a purge (T3)",
  "ltcplay/morning.py",
  '''        if not seen_ok:
            return ("The page's status is more than 1 s old. Wait for it "''',
  '''        if False:
            return ("The page's status is more than 1 s old. Wait for it "'''),

 ("morning: a default wave flamer with no pan, tilt and fire map is purged "
  "(raises, or writes a guessed fire value)",
  "ltcplay/morning.py",
  '''                no_map = cfg.purge_refusal(m)
                if no_map:''',
  '''                no_map = cfg.purge_refusal(m)
                if False:'''),

 ("morning: a waver with no pan or tilt slot is not purge-refused",
  "ltcplay/morning.py",
  '''        if self.kind == "waver" and (self.pan is None or self.tilt is None
                                     or not self.fire):''',
  '''        if False:'''),

 ("morning: a fire press on only never-purged machines is not refused",
  "ltcplay/morning.py",
  '''            if ms and all(cfg.purge_refusal(m) for m in ms):''',
  '''            if False:'''),

 ("morning: a fireball is purged with one value on every slot (fuel, air, "
  "valve and ignite at once)",
  "ltcplay/morning.py",
  '''        if self.kind == "fireball":
            return NO_FIREBALL_PURGE
''',
  ''''''),

 ("morning: the default fireball is split into one machine per channel",
  "ltcplay/morning.py",
  '''        if kind == "fireball":
            # One fireball is one machine''',
  '''        if False:
            # One fireball is one machine'''),

 ("morning page: a fireball tile reads like a G-Flame (Off, FIRING)",
  "ltcplay/web/remote.html",
  '''  if(m.kind === "fireball") return''',
  '''  if(false) return'''),

 ("deck: the screen-only fourth group follows the first key",
  "ltcplay/streamdeck.py",
  "        k = self._group_key.get(i)\n        return k is not None and bool(self._prev_keys[k])",
  "        return bool(self._prev_keys[GROUP_KEYS[i % len(GROUP_KEYS)]])"),

 ("deck: the keys follow flamesafe's file order, not the deck's",
  "ltcplay/streamdeck.py",
  "        self._key_group = {GROUP_KEYS[j]: g for j, g in\n                           enumerate(self.key_order[:len(GROUP_KEYS)])}",
  "        self._key_group = {GROUP_KEYS[j]: g for j, g in\n                           enumerate(range(len(self.names))[:len(GROUP_KEYS)])}"),

 ("deck: a key's label is the flamesafe name, never the configured one",
  "ltcplay/streamdeck.py",
  "        labels[i] = lab.strip()\n",
  "        pass\n"),

 ("deck: the keys are drawn under flamesafe's names, not their labels",
  "ltcplay/streamdeck.py",
  "            name, label = self.names[i], self.labels[i]\n",
  "            name, label = self.names[i], self.names[i]\n"),

 ("deck: an over-long label is drawn cut short",
  "ltcplay/streamdeck.py",
  "                len(lab.strip()) > LABEL_MAX:",
  "                len(lab.strip()) > 99:"),

 ("morning: a machine of another kind in a fireball group is accepted "
  "(review of #120, P1)",
  "ltcplay/morning.py",
  '''        if group in fireball_groups and kind != "fireball":''',
  '''        if False:'''),

 ("morning: the never-purge rule goes by machine kind, not by group (P1)",
  "ltcplay/morning.py",
  '''        if m.group in self.fireball_groups or m.kind == "fireball":''',
  '''        if m.kind == "fireball":'''),

 ("morning: fireball_groups is read and ignored",
  "ltcplay/morning.py",
  '''                    f"flamesafe group ({', '.join(sorted(known))}).")
            out.add(x)''',
  '''                    f"flamesafe group ({', '.join(sorted(known))}).")'''),

 ("morning: a group named ... ball is not a fireball",
  "ltcplay/morning.py",
  '''    if "fireball" in n or "ball" in n:''',
  '''    if "fireball" in n:'''),

 ("morning: a declared fireball group is built as G-Flames",
  "ltcplay/morning.py",
  '''        kind = "fireball" if g["name"] in fireball_groups else \\''',
  '''        kind = "fireball" if False else \\'''),

 ("morning page: a purge refused for a group with no deck key reads Arming...",
  "ltcplay/web/remote.html",
  '''  if(g.purge_refused === "screenonly") return''',
  '''  if(false) return'''),

 ("deck: a purge click arms a group with no key (review of #120, P1)",
  "ltcplay/streamdeck.py",
  '''            if i not in self._group_key:
                # A screen-only group''',
  '''            if False:
                # A screen-only group'''),

 ("deck: a file with a typo is partly applied, moving keys (P2-1)",
  "ltcplay/streamdeck.py",
  "    if problems:\n        return d_order, d_labels, problems + [",
  "    if False:\n        return d_order, d_labels, problems + ["),

 ("deck: a label may read like another group (P2-2)",
  "ltcplay/streamdeck.py",
  "            if j != i and mine in (other.lower(), d_labels[j].lower(),",
  "            if False and mine in (other.lower(), d_labels[j].lower(),"),

 ("deck: the layout notes and key map are never journaled",
  "ltcplay/streamdeck.py",
  "    for note in notes:\n        try:\n            journal(f\"Stream Deck: {note}\"",
  "    for note in []:\n        try:\n            journal(f\"Stream Deck: {note}\""),

 ("deck: a settings folder that cannot be found is not said (P2-4)",
  "ltcplay/streamdeck.py",
  '''            notes.append(f"the settings folder could not be found "
                         f"({type(e).__name__}: {e}), so {DECK_CONFIG_FILE} "
                         f"was not read and the deck uses its default "
                         f"layout.")''',
  '''            pass'''),

 ("deck: the Deck chip never hears of a layout set aside",
  "ltcplay/streamdeck.py",
  '''        if dflt:
            path += ("&" if "?" in path else "?") + "layout=default"''',
  '''        if False:
            path += ("&" if "?" in path else "?") + "layout=default"'''),

 ("Deck chip: a layout set aside reads ok",
  "ltcplay/remote.py",
  '''        elif getattr(self, "_deck_layout_default", False):''',
  '''        elif False:'''),

 ("deck: a latched tap disarms by key position, not the key's group",
  "ltcplay/streamdeck.py",
  "                    i = self._key_group[k]\n",
  "                    i = k - GROUP_KEYS[0]\n"),

 ("deck: an early let go cancels by key position, not the key's group",
  "ltcplay/streamdeck.py",
  "                self._arm_holds[self._key_group[k]].release()",
  "                self._arm_holds[k - GROUP_KEYS[0]].release()"),

 ("deck: the audit names keys in flamesafe's order",
  "ltcplay/streamdeck.py",
  '''                k, [self.names[g] for g in
                    self.key_order[:len(GROUP_KEYS)]], latched=latched,''',
  '''                k, self.names, latched=latched,'''),

 ("deck: a screen-only group's arm skips the fire-time latch check",
  "ltcplay/streamdeck.py",
  '''        if self._latched_now():
            self._log(f"Stream Deck: {self.names[i]} arm-hold completed "''',
  '''        if self._latched_now() and i in self._group_key:
            self._log(f"Stream Deck: {self.names[i]} arm-hold completed "'''),

 ("deck: Stop waits on the operator gate",
  "ltcplay/streamdeck.py",
  '''        fn = getattr(self.conductor, "stop_run", None)''',
  '''        if operator_gate(who, "start"):
            return
        fn = getattr(self.conductor, "stop_run", None)'''),

 ("deck: a Stop hold that completes after the run ended sends nothing (review of #130, 1)",
  "ltcplay/streamdeck.py",
  "            self._do_stop()\n            # Never ALSO complete an arm-hold",
  "            if self.start_key_mode() in (\"stop\", \"show\"):\n                self._do_stop()\n            # Never ALSO complete an arm-hold"),

 ("deck: a Stop hold never shows on the key",
  "ltcplay/streamdeck.py",
  "            if stop_frac > 0:\n                # Stop being held: the ring fills; let go early, nothing.\n                # During a scheduled show too",
  "            if False:\n                # Stop being held: the ring fills; let go early, nothing.\n                # During a scheduled show too"),

 # Neighbours of the one above (the full sweep, 2026-10-27):
 # test_streamdeck_stop_hold_face_and_ring_from_the_first_instant.
 ("deck: a Stop hold never fills key 0's ring",
  "ltcplay/streamdeck.py",
  "        if stop_frac > 0:\n            arm_fills[TOP_START] = stop_frac",
  "        if False:\n            arm_fills[TOP_START] = stop_frac"),

 ("deck: an aborted Stop hold reads STOP RUN until STOP_FACE_AFTER_S",
  "ltcplay/streamdeck.py",
  "        elif latched and stop_frac > 0:",
  "        elif False:"),

 ("round3: a Stop can also complete a same-pass arm-hold again",
  "ltcplay/streamdeck.py",
  """            self._do_stop()
            # Never ALSO complete an arm-hold in the SAME pass Stop just
            # disarmed every group in (the ordering rule Abort had on the
            # deck); _do_arm_fire's refractory check, set by the disarm, is
            # the second, independent backstop.
            return
""",
  """            self._do_stop()
"""),

 ("round4: reset_on_reconnect skips only the Stop hold",
  "ltcplay/streamdeck.py",
  "            h.release()\n        self._stop_hold.release()\n        self._vholds.clear()",
  "            h.release()\n        self._vholds.clear()"),

 ("round4: reset_on_reconnect skips only the arm holds",
  "ltcplay/streamdeck.py",
  "        self._prev_keys = [False] * 6\n        for h in self._arm_holds:\n            h.release()\n        self._stop_hold.release()\n",
  "        self._prev_keys = [False] * 6\n        self._stop_hold.release()\n"),

 # The Stream Deck's page 2, the announcements (Jeff, 2026-10-26): it
 # covers the arm keys, so it must give way to them by itself, and nothing
 # on it may arm (test_streamdeck_announcements_page_two).
 ("deck page 2: stays up through an Abort latch",
  "ltcplay/streamdeck.py",
  '        if latched:\n            return "an Abort is latched"',
  '        if False:\n            return "an Abort is latched"'),

 ("deck page 2: never goes back by itself when idle",
  "ltcplay/streamdeck.py",
  "        if now - self._page2_at < PAGE2_IDLE_S:\n            return \"\"",
  "        if True:\n            return \"\""),

 ("deck page 2: stays up through an alarm on the arm link",
  "ltcplay/streamdeck.py",
  '        if self._spoof_alarm:\n            return "the arm link alarm is up"',
  '        if False:\n            return "the arm link alarm is up"'),

 ("deck page 2: opens while another key is down",
  "ltcplay/streamdeck.py",
  "        elif any(down[k] for k in range(len(down)) if k != ANNOUNCE_KEY):",
  "        elif False:"),

 ("deck page 2: a press plays twice",
  "ltcplay/streamdeck.py",
  "            elif k in PAGE2_PLAY:\n"
  "                self._announce_press(PAGE2_PLAY[k])",
  "            elif k in PAGE2_PLAY:\n"
  "                self._announce_press(PAGE2_PLAY[k])\n"
  "                self._announce_press(PAGE2_PLAY[k])"),

 ("deck: key 5 opens page 2 with no announcements running",
  "ltcplay/streamdeck.py",
  '                    (self.announce_view() or {}).get("configured"):',
  "                    True:"),

 # The alert hold (alerthold.py, Jeff 2026-10-26).
 ("alerts: the booth note sounds once and never again",
  "ltcplay/alerts.py",
  "        for a in list(self._open.values()):\n"
  "            # A note that could not play is tried again at the same pace,",
  "        for a in []:\n"
  "            # A note that could not play is tried again at the same pace,"),

 ("alerts: the booth re-sounds every minute again",
  "ltcplay/alerts.py",
  "RESOUND_S = 30.0",
  "RESOUND_S = 60.0"),

 ("alerts: a missing booth device is asked on every tick",
  "ltcplay/alerts.py",
  "            if tried is not None and now - tried < every:\n"
  "                continue",
  "            if False:\n"
  "                continue"),

 ("alert hold: a show starts at its time with an alert open",
  "ltcplay/schedule_service.py",
  "            self._preroll_before_gate(now, ev)\n"
  "            self._hold_at_showtime_for_alerts(now, ev)\n",
  "            self._preroll_before_gate(now, ev)\n"),

 ("alert hold: the same showtime is held again after Resume",
  "ltcplay/schedule_service.py",
  "        if self._alert_handled(key, ids):\n            return",
  "        if False:\n            return"),

 ("alert hold: only the exact alert set Resumed with is handled, not a subset",
  "ltcplay/schedule_service.py",
  "        return any(ids <= rec for rec in self._alert_held.get(key, ()))",
  "        return any(ids == rec for rec in self._alert_held.get(key, ()))"),

 ("alert hold: a Resume before showtime is not the operator's choice",
  "ltcplay/schedule_service.py",
  "                if pending:\n                    shows = [pending[0].n]",
  "                if False:\n                    shows = [pending[0].n]"),

 ("alert hold: a Resume of a paused show speaks for the next show",
  "ltcplay/schedule_service.py",
  "                not out.refused and before is not None and \\\n"
  "                before.state == sch.HOLD:",
  "                not out.refused:"),

 ("alert hold: a delayed show's Resume also covers the show after it",
  "ltcplay/schedule_service.py",
  "            if not shows and m.delayed() is None:",
  "            if not shows:"),

 ("alert hold: two ticks at once can start two hold workers",
  "ltcplay/remote.py",
  "        with self._alert_lock:\n            self._start_alert_workers()",
  "        self._start_alert_workers()"),

 ("alert hold: an old hold clears a newer one",
  "ltcplay/alerthold.py",
  "        if self.hold is h:\n            self.hold = None",
  "        self.hold = None"),

 ("alert hold: the automatic play's journal line hides the alert hold",
  "ltcplay/announce.py",
  '                     "It plays while the schedule is on the alert hold; "\n'
  '                     "nothing else was changed." if automatic is not None\n'
  '                     else\n',
  ""),

 ("alert hold: the server's close never stops the alert workers",
  "ltcplay/web.py",
  "            httpd.remote.close_alerts()",
  "            pass"),

 ("alert hold: a worker never checks for the stop",
  "ltcplay/remote.py",
  "            if self._alert_stop.is_set():\n                return\n            work()",
  "            work()"),

 ("alert hold: a tick after the close starts the workers again",
  "ltcplay/remote.py",
  "        if self._alert_stop.is_set():\n            return\n        self._alert_wake.set()",
  "        self._alert_wake.set()"),

 ("alert hold: the hold worker does the booth's work too",
  "ltcplay/remote.py",
  "                args=(self._alert_wake, self._hold_work),",
  "                args=(self._alert_wake, self._alert_work),"),

 ("alert hold: an operator's own Resume at showtime is not respected",
  "ltcplay/schedule_service.py",
  "            self._note_resumed_at_showtime(now)",
  "            pass"),

 ("alert hold: the booth re-sound has no worker of its own",
  "ltcplay/remote.py",
  "            self._booth_thread.start()",
  "            pass"),

 ("alert hold: the alert workers never stop",
  "ltcplay/remote.py",
  "        self._alert_stop.set()\n        self._alert_wake.set()",
  "        self._alert_wake.set()"),

 ("alert hold: the remote never hooks the scheduler's ticks",
  "ltcplay/remote.py",
  "            schedule.tick_hooks.append(self._alert_tick)",
  "            pass"),

 ("alert hold: the scheduler's run loop never calls its tick hooks",
  "ltcplay/schedule_service.py",
  "            for hook in list(self.tick_hooks):",
  "            for hook in []:"),

 ("alert hold: the alert work runs on the scheduler's thread",
  "ltcplay/remote.py",
  "        self._alert_wake.set()\n        self._booth_wake.set()\n"
  "        with self._alert_lock:",
  "        self._alert_work()\n        self._booth_wake.set()\n"
  "        with self._alert_lock:"),

 ("alert hold: Acknowledge does not cancel the announcement",
  "ltcplay/alerthold.py",
  "        if self._acknowledged(h):\n            self._cancel(h)\n"
  "            return\n        if self._clock() < h[\"due\"]:",
  "        if False:\n            self._cancel(h)\n"
  "            return\n        if self._clock() < h[\"due\"]:"),

 ("alert hold: one Acknowledge cancels while another loud alert is open",
  "ltcplay/alerthold.py",
  "        return not (h[\"loud\"] & now_ids)",
  "        return bool(h[\"ids\"] - now_ids)"),

 ("alert hold: deck and gate alerts announce too",
  "ltcplay/alerthold.py",
  "        loud_ids = {a.id for a in open_ if a.kind in ANNOUNCE_KINDS}",
  "        loud_ids = {a.id for a in open_}"),

 ("alert hold: the announcement plays after the operator resumed",
  "ltcplay/alerthold.py",
  "        if h is None:\n            return\n        if not self._still_held(h):\n"
  "            self._drop(h)\n            return",
  "        if h is None:\n            return\n        if False:\n"
  "            self._drop(h)\n            return"),

 ("alert hold: the announcement plays on every tick after the countdown",
  "ltcplay/alerthold.py",
  '        h["state"], h["due"] = "announcing", None\n',
  ""),

 ("alert hold: the countdown is not waited for",
  "ltcplay/alerthold.py",
  '        if self._clock() < h["due"]:\n            return',
  '        if False:\n            return'),

 ("alert hold: the automatic Delayed asks for a Hold, and its safety cuts",
  "ltcplay/announce.py",
  "            elif self.hold_requester is not None and not over_show:",
  "            if self.hold_requester is not None and not over_show:"),

 ("alert hold: an Acknowledge during the file read does not stop it",
  "ltcplay/announce.py",
  "            if automatic is not None:\n"
  "                why = self._automatic_refusal(automatic)\n"
  "                if why:\n"
  "                    self.playing = None",
  "            if False:\n"
  "                why = self._automatic_refusal(automatic)\n"
  "                if why:\n"
  "                    self.playing = None"),

 # Review of PR #129 (cold review, 2026-10-27).
 ("an announcement pressed at showtime pauses the show that just started",
  "ltcplay/schedule_service.py",
  "            if self.machine.state == sch.SHOW:\n"
  "                # A show is running, possibly one this very tick just",
  "            if False:\n"
  "                # A show is running, possibly one this very tick just"),

 ("announcements: the rack status asks the service on the request path",
  "ltcplay/remote.py",
  "        c = self._ann_cache\n        if c is None:\n"
  '            return {"configured": True, "ok": False,',
  "        self._ann_refresh()\n        c = self._ann_cache\n        if c is None:\n"
  '            return {"configured": True, "ok": False,'),

 ("announcements: a Play skips the laser arm's press interlock",
  "ltcplay/remote.py",
  '            if name == "announce-play" else None\n        if la is None:',
  '            if False else None\n        if la is None:'),

 ("deck page 2: a press right after an automatic close arms a group",
  "ltcplay/streamdeck.py",
  "                elif self._guard_on(now) and \\\n",
  "                elif False and \\\n"),

 ("deck page 2: the guard after an automatic close also swallows a disarm",
  "ltcplay/streamdeck.py",
  "                elif self._guard_on(now) and \\\n"
  "                        not self.arm.wanted[self._key_group[k]]:",
  "                elif self._guard_on(now):"),

 ("deck page 2: the guard ends when page 1 is composed, not written",
  "ltcplay/streamdeck.py",
  "        if shown and shown[0] >= f:\n            return shown[1]\n        return None",
  "        return self._guard_frame_at"),

 ("deck page 2: the guard counts from the return, not from the write",
  "ltcplay/streamdeck.py",
  "        if at is None or now < at + PAGE1_GUARD_S:",
  "        if at is None or now < self._guard + PAGE1_GUARD_S:"),

 ("deck page 2: a stale engine answer closes page 2 for idle",
  "ltcplay/streamdeck.py",
  "        return (isinstance(av, dict) and not av.get(\"stale\") and\n",
  "        return (isinstance(av, dict) and\n"),

 ("deck page 2: an answer that says ok false reads as nothing playing",
  "ltcplay/streamdeck.py",
  "                av.get(\"ok\") is not False and not av.get(\"playing\"))",
  "                not av.get(\"playing\"))"),

 ("announcements: a status that raised is not marked stale",
  "ltcplay/remote.py",
  "            v = {\"configured\": True, \"ok\": False, \"stale\": True,\n"
  "                 \"error\": f\"Announcement status unavailable: it could not \"",
  "            v = {\"configured\": True, \"ok\": False,\n"
  "                 \"error\": f\"Announcement status unavailable: it could not \""),

 ("deck page 2: never gives way while the engine stays silent",
  "ltcplay/streamdeck.py",
  "        if now - max(seen, self._page2_at) >= PAGE2_STALE_CLOSE_S:",
  "        if False:"),

 ("deck page 2: a refusal shows on whichever key was pressed last",
  "ltcplay/streamdeck.py",
  "                refused[said[3]] = announce_refusal_word(said[1])",
  "                refused[self._ann_pressed] = announce_refusal_word(said[1])"),

 ("deck page 2: closes for idle while an announcement plays",
  "ltcplay/streamdeck.py",
  "        if self._fresh_and_quiet(av):\n"
  "            return f\"no key was pressed for {PAGE2_IDLE_S:g} s\"",
  "        if True:\n"
  "            return f\"no key was pressed for {PAGE2_IDLE_S:g} s\""),

 ("deck page 2: an engine refusal is never shown on the key",
  "ltcplay/streamdeck.py",
  "            if fresh:\n                # On the key whose press it answers",
  "            if False:\n                # On the key whose press it answers"),

 ("announcements: a Hold for an announcement leaves the lasers armed",
  "ltcplay/remote.py",
  '                    self._lasers_disarm_now(who, screen, "announcement")',
  "                    pass"),

 ("announcements: the Hold's safety cuts are made outside the scheduler's lock",
  "ltcplay/remote.py",
  "            with (locked() if locked is not None\n"
  "                  else contextlib.nullcontext()):",
  "            with contextlib.nullcontext():"),

 ("announcements: a Hold for an announcement leaves live flame programming on",
  "ltcplay/remote.py",
  "                    self.live_flame_cut(why, who, screen)\n                    self._lasers_disarm_now(who, screen, \"announcement\")",
  "                    self._lasers_disarm_now(who, screen, \"announcement\")"),

 ("announcements: a stale snapshot still reads as fact",
  "ltcplay/remote.py",
  "        if age > ANNOUNCE_STALE_S:",
  "        if False:"),

 ("announcements: held-then-failed is cleared by state, not its own Hold",
  "ltcplay/announce.py",
  "        if self.hold_still_claimed is not None and \\\n"
  "                self._held_failed_epoch is not None:",
  "        if False:"),

 ("deck: announcement presses share Stop's queue",
  "ltcplay/streamdeck.py",
  '        q = self._aq if name.startswith("announce-") else self._q',
  "        q = self._q"),

 ("announcements: a Hold whose announcement failed is never said",
  "ltcplay/announce.py",
  "                    if held:\n"
  "                        self._note_held_failed(ann_id, _clean(str(e)),\n",
  "                    if False:\n"
  "                        self._note_held_failed(ann_id, _clean(str(e)),\n"),

 # The rack screen's announcement routes (remote.announce_post).
 ("announcements: an iPad can play one",
  "ltcplay/remote.py",
  "        if not ctx.local:\n"
  '            return 403, {"error": "Announcements are played from the show "',
  "        if False:\n"
  '            return 403, {"error": "Announcements are played from the show "'),

 ("announcements: a stale page's Play is taken",
  "ltcplay/remote.py",
  '        if name == "announce-play":\n'
  "            # A Play acts on what the page shows",
  '        if False:\n'
  "            # A Play acts on what the page shows"),

 ("morning: a click on a fireball arms its group for a purge",
  "ltcplay/morning.py",
  "        never = cfg.purge_refusal(m)\n        if never in NEVER_SELECTED:",
  "        never = cfg.purge_refusal(m)\n        if False:"),

 ("morning: a fireball in the purge selection asks the deck to arm its group",
  "ltcplay/morning.py",
  "            if m.group in cfg.fireball_groups or \\\n                    cfg.purge_refusal(m) in NEVER_SELECTED:\n                continue",
  "            if False:\n                continue"),

 ("deck: the Start/Stop key never becomes Stop",
  "ltcplay/streamdeck.py",
  "            if self.run_live_provider() is True:\n                return \"stop\"",
  "            if False:\n                return \"stop\""),

 ("deck: the scheduler unknown is not Stop (review of #130, 1)",
  "ltcplay/streamdeck.py",
  "        if running is None:\n            return \"unknown\"\n",
  ""),

 ("deck: Stop is sent during a scheduled show",
  "ltcplay/streamdeck.py",
  "        if running is True:\n            return \"show\"",
  "        if False:\n            return \"show\""),

 ("deck: STOP RUN goes on a single tap again",
  "ltcplay/streamdeck.py",
  "            # show too: Stop is a complete halt, and the show ends.\n            self._stop_hold.press(now)",
  "            # show too: Stop is a complete halt, and the show ends.\n            self._do_stop()"),

 ("deck: letting go of STOP RUN early still stops",
  "ltcplay/streamdeck.py",
  "                # Letting go of Stop before STOP_HOLD_S sends nothing.\n                self._stop_hold.release()",
  "                # Letting go of Stop before STOP_HOLD_S sends nothing.\n                pass"),

 ("deck: the FIRE key keeps firing after it is released (T2)",
  "ltcplay/streamdeck.py",
  "                self._fire_release(\"the key came up\")\n                # Letting go of Stop before STOP_HOLD_S sends nothing.",
  "                pass\n                # Letting go of Stop before STOP_HOLD_S sends nothing."),

 ("deck: Stop leaves the screen-only group armed",
  "ltcplay/streamdeck.py",
  "        self.arm.set_all(False)\n        self.arm.send(self.names)\n        now = self._clock()\n        self._disarmed_at = [now] * len(self.names)\n        self._purge_armed.clear()",
  "        for _g in range(min(len(self.names), len(GROUP_KEYS))):\n            self.arm.set_group(_g, False)\n        self.arm.send(self.names)\n        now = self._clock()\n        self._disarmed_at = [now] * len(self.names)\n        self._purge_armed.clear()"),

 ("deck: Stop never disarms the flame groups",
  "ltcplay/streamdeck.py",
  "        self._disarm_all_groups(\"Stop held\")\n",
  ""),

 ("deck: Stop sets the Abort latch",
  "ltcplay/streamdeck.py",
  "        self._disarm_all_groups(\"Stop held\")\n",
  "        self._disarm_all_groups(\"Stop held\")\n        self._latched = True\n"),

 ("deck: Stop cannot be reached while aborted",
  "ltcplay/streamdeck.py",
  "                    if self.start_key_mode() in STOP_MODES or \\\n                            self._idle_stop():\n                        self._stop_hold.press(now)\n                elif k in self._key_group:",
  "                    if False:\n                        self._stop_hold.press(now)\n                elif k in self._key_group:"),

 ("deck: a held Stop never fires while aborted",
  "ltcplay/streamdeck.py",
  "        if self._prev_keys[TOP_START] and self._stop_hold.fired(now):",
  "        if latched:\n            return\n        if self._prev_keys[TOP_START] and self._stop_hold.fired(now):"),

 ("deck: a Stop hold timer fires with the key up (review of #120, P2-3)",
  "ltcplay/streamdeck.py",
  "        if self._prev_keys[TOP_START] and self._stop_hold.fired(now):",
  "        if self._stop_hold.fired(now):"),

 ("deck: a Reset or Abort from a screen starts no re-arm wait",
  "ltcplay/streamdeck.py",
  "            self._was_latched = latched\n            self._disarmed_at = [now] * len(self.names)",
  "            self._was_latched = latched"),

 ("deck: the Start key's audit name changes (review of #120, P2-6)",
  "ltcplay/streamdeck.py",
  "        return \"ACK\" if ack else (\"FIRE\" if morning else \"Start now\")",
  "        return \"ACK\" if ack else (\"FIRE\" if morning else \"Start/Stop\")"),

 ("deck: the arm keys are not keys 1 to 4",
  "ltcplay/streamdeck.py",
  "GROUP_KEYS = (1, 2, 3, 4)",
  "GROUP_KEYS = (2, 3, 4, 5)"),

 ("deck: the fourth arm key's NO LINK is missing",
  "ltcplay/streamdeck.py",
  "        if slot == 3:\n            # The fourth arm key",
  "        if False:\n            # The fourth arm key"),

 ("morning: an unknown multi-channel group is purged (review of #120, P2-1)",
  "ltcplay/morning.py",
  "        if m.group in self.unknown_groups:\n            return NO_UNKNOWN_PURGE\n",
  ""),

 ("morning: gflame_groups is read and ignored",
  "ltcplay/morning.py",
  "        out.add(x)\n    return frozenset(out)\n\n\ndef unknown_multi_groups",
  "    return frozenset(out)\n\n\ndef unknown_multi_groups"),

 ("morning: an unknown multi-channel group is never journaled",
  "ltcplay/morning.py",
  "        for line in self.cfg.journal_lines():",
  "        for line in []:"),

 ("morning page: an unknown multi-channel group's tile reads Off",
  "ltcplay/web/remote.html",
  "  if(m.never_purged) return",
  "  if(false) return"),

 ("deck: the key order list is ignored",
  "ltcplay/streamdeck.py",
  "        if i in order:\n            problems.append(f\"{where} lists {g!r} twice.\")",
  "        if True:\n            problems.append(f\"{where} lists {g!r} twice.\")"),

 ("deck: Jeff's four are not keyed by name (review of #126, P1)",
  "ltcplay/streamdeck.py",
  "    order = [known.index(k) for k in DECK_ORDER if k in known]\n",
  "    order = []\n"),

 ("deck: the default labels are not Jeff's",
  "ltcplay/streamdeck.py",
  "    return order, [DECK_LABELS.get(k, n) if known.index(k) == i else n\n                   for i, (n, k) in enumerate(zip(names, known))]",
  "    return order, list(names)"),

 ("deck: a ltcplay_deck.json must list every group again (review of #126, P1)",
  "ltcplay/streamdeck.py",
  "    order += [i for i in d_order if i not in order]\n",
  "    if [i for i in d_order if i not in order]:\n        problems.append(\"a group is not listed\")\n    order += [i for i in d_order if i not in order]\n"),

 ("deck: the Rack screen never hears which groups have no key",
  "ltcplay/streamdeck.py",
  "        if nokey:\n            path += (\"&\" if \"?\" in path else \"?\") + f\"nokey={nokey}\"",
  "        if False:\n            path += (\"&\" if \"?\" in path else \"?\") + f\"nokey={nokey}\""),

 ("Rack screen: a group with no deck key is not marked",
  "ltcplay/remote.py",
  "            g[\"deck_key\"] = not (fresh and i in nokey)",
  "            g[\"deck_key\"] = True"),

 ("deck: a purge click arms a group the page never purges (review of #126, P2-4)",
  "ltcplay/streamdeck.py",
  "            if self._never_purged(i):",
  "            if False:"),

 ("deck: a group named fireball is armed for a purge on the deck",
  "ltcplay/streamdeck.py",
  "        if \"fireball\" in n or \"ball\" in n:\n            return True",
  "        if False:\n            return True"),

 ("deck: the page's never-purged groups are ignored",
  "ltcplay/streamdeck.py",
  "        return isinstance(never, list) and i in never",
  "        return False"),

 ("morning: the deck is never told which groups are never purged",
  "ltcplay/morning.py",
  "                          \"firing\": pv[\"firing\"], \"never_groups\": never},",
  "                          \"firing\": pv[\"firing\"], \"never_groups\": []},"),

 ("morning: every default-built group counts as G-Flames (review of #126, P2-3)",
  "ltcplay/morning.py",
  "        if name.strip().lower() in GFLAME_ROW_NAMES and \\\n                len(g.get(\"safeties\") or []) <= 1:\n            continue",
  "        if True:\n            continue"),

 ("morning: Jeff's front row and cat-walk are never purged",
  "ltcplay/morning.py",
  "        if name.strip().lower() in GFLAME_ROW_NAMES and \\\n                len(g.get(\"safeties\") or []) <= 1:\n            continue",
  "        if False:\n            continue"),

 ("morning: a row is any name with row, cat or walk in it again",
  "ltcplay/morning.py",
  "        if name.strip().lower() in GFLAME_ROW_NAMES and \\\n                len(g.get(\"safeties\") or []) <= 1:\n            continue",
  "        if any(w in name.lower() for w in (\"row\", \"cat\", \"walk\")) and \\\n                len(g.get(\"safeties\") or []) <= 1:\n            continue"),

 ("morning: a one-slot group nothing declares is purged",
  "ltcplay/morning.py",
  "    for g in groups or []:\n        name = g[\"name\"]\n        if name in fireball_groups or name in gflame_groups:",
  "    for g in groups or []:\n        name = g[\"name\"]\n        if len(g.get(\"fire\") or []) < 2 and len(g.get(\"safeties\") or []) <= 1:\n            continue\n        if name in fireball_groups or name in gflame_groups:"),

 ("Stop: the engine's Stop sends no flame disarm (review of #126, P2-1)",
  "ltcplay/web.py",
  "                flames = self._stop_flames(body)\n",
  "                flames = None\n"),

 ("Stop: the engine stops playback before the flames are disarmed",
  "ltcplay/web.py",
  "                flames = self._stop_flames(body)\n                out = {\"stopped\": c.stop()}",
  "                out = {\"stopped\": c.stop()}\n                flames = self._stop_flames(body)"),

 ("Stop: live flame programming carries on through a Stop",
  "ltcplay/web.py",
  "        for name in (\"live_flame_cut\",):",
  "        for name in ():"),

 ("deck: a latched press with an alert open only acknowledges again (review of #130, 9)",
  "ltcplay/streamdeck.py",
  "                    if self.start_key_mode() in STOP_MODES or \\\n                            self._idle_stop():\n                        self._stop_hold.press(now)\n                elif k in self._key_group:",
  "                    if not pending and (self.start_key_mode() in STOP_MODES or \\\n                            self._idle_stop()):\n                        self._stop_hold.press(now)\n                elif k in self._key_group:"),

 ('deck: an alert open swallows the first Stop again (review of #130, 9)',
  'ltcplay/streamdeck.py',
  '                    if (not morning or self._stop_wins()) and (\n                            self.start_key_mode() in STOP_MODES or\n                            self._idle_stop()):\n                        self._stop_hold.press(now)',
  '                    if False:\n                        self._stop_hold.press(now)'),

 ("deck: an alert open with a group armed but nothing running swallows the Stop",
  "ltcplay/streamdeck.py",
  "                            self.start_key_mode() in STOP_MODES or\n                            self._idle_stop()):\n                        self._stop_hold.press(now)",
  "                            self.start_key_mode() in STOP_MODES):\n                        self._stop_hold.press(now)"),

 ("deck: a latched press starts a Stop hold whatever the key is (mutant P)",
  "ltcplay/streamdeck.py",
  "                    if self.start_key_mode() in STOP_MODES or \\\n                            self._idle_stop():\n                        self._stop_hold.press(now)\n                elif k in self._key_group:",
  "                    if True:\n                        self._stop_hold.press(now)\n                elif k in self._key_group:"),

 ("deck: Stop's disarm leaves the purge's own arms (mutant O)",
  "ltcplay/streamdeck.py",
  "        self._purge_armed.clear()\n        self._log(f\"Stream Deck: {why}",
  "        self._log(f\"Stream Deck: {why}"),

 ("deck: Stop asks the engine before it disarms the arm link",
  "ltcplay/streamdeck.py",
  "        self._disarm_all_groups(\"Stop held\")\n        who = self._who()\n        fn = getattr(self.conductor, \"stop_run\", None)\n        if fn is None:\n            self._log(\"Stream Deck Stop pressed: no engine is connected in \"\n                      \"this build. Nothing was stopped.\", fault=True,\n                      action=\"stop\", who=who, screen=\"Stream Deck\")\n            return\n        r = fn(who=who, screen=\"Stream Deck\")\n",
  "        who = self._who()\n        fn = getattr(self.conductor, \"stop_run\", None)\n        if fn is None:\n            self._disarm_all_groups(\"Stop held\")\n            self._log(\"Stream Deck Stop pressed: no engine is connected in \"\n                      \"this build. Nothing was stopped.\", fault=True,\n                      action=\"stop\", who=who, screen=\"Stream Deck\")\n            return\n        r = fn(who=who, screen=\"Stream Deck\")\n        self._disarm_all_groups(\"Stop held\")\n"),

 ("supervisor: the first-run flamesafe config is cut to three groups again",
  "packaging/windows/supervisor.py",
  "    doc[\"groups\"] = list(doc.get(\"groups\") or [])\n",
  "    doc[\"groups\"] = list(doc.get(\"groups\") or [])[:3]\n"),

 ("deck: Stop is not the Rack screen's Stop route",
  "ltcplay/streamdeck.py",
  '''        return self._press("stop", who, screen, path="/api/stop", halt=True)''',
  '''        return self._press("stop", who, screen, halt=True)'''),

 ("deck: the run is never seen as live",
  "ltcplay/streamdeck.py",
  '''                self._run_live = bool(play.get("running"))''',
  '''                self._run_live = None'''),

 ("deck: the key never reads STOP RUN",
  "ltcplay/streamdeck.py",
  '''            elif self.start_key_mode() == "stop":''',
  '''            elif False:'''),

 ("deck: nobody is told which flame group has no key",
  "ltcplay/streamdeck.py",
  '''        if extra:
            keyed = [''',
  '''        if False:
            keyed = ['''),

 ("morning: a fire heartbeat gap never ends the press (T2)",
  "ltcplay/morning.py",
  '''            stale = f is not None and now - f["beat"] > BEAT_STALE_S''',
  '''            stale = False'''),

 ("morning: the purge selection outlives the page (T4)",
  "ltcplay/morning.py",
  '''            elif not page_open:
                self.clear("the Morning and Shutdown page is no longer open")''',
  '''            elif False:
                self.clear("the Morning and Shutdown page is no longer open")'''),

 ("morning: another device carries on the owner's fire press (T3)",
  "ltcplay/morning.py",
  '''                if f is not None and f["device"] != device:
                    # Another device's heartbeat never carries this press''',
  '''                if False:
                    # Another device's heartbeat never carries this press'''),

 ("morning: a machine fires before flamesafe reports its group armed (T13)",
  "ltcplay/morning.py",
  '''                if m.group not in armed:''',
  '''                if False:'''),

 ("morning: an unlock reads as a lock problem for flamesafe's poll time",
  "ltcplay/morning.py",
  '''        if self._lock_written_at is not None and \\
                self._clock() - self._lock_written_at < LOCK_SETTLE_S:
            # This engine just wrote the file''',
  '''        if False:
            # This engine just wrote the file'''),

 ("morning page: a Space press whose key repeat events have stopped never "
  "ends (T3)",
  "ltcplay/web/remote.html",
  '''  return nowMs - startAt > graceMs && nowMs - Math.max(repeatAt || 0, startAt) > gapMs;''',
  '''  return false;'''),

 ("morning: a no-op lock (nothing written) restarts flamesafe's settle time",
  "ltcplay/morning.py",
  '''            if ok and self.lockout.wrote:
                # Only a real write of the file earns flamesafe's settle time''',
  '''            if ok:
                # Only a real write of the file earns flamesafe's settle time'''),

 ("morning: a lock or unlock earns no settle time, so flamesafe's old report "
  "reads as a lock problem",
  "ltcplay/morning.py",
  '''                self._lock_written_at = self._clock()
            if ok and on:''',
  '''                pass
            if ok and on:'''),

 ("morning: a Waver fires before its head is up (T12)",
  "ltcplay/morning.py",
  '''                    if now - since < w["head_settle_s"]:''',
  '''                    if False:'''),

 ("morning: unlocking a machine needs no confirm",
  "ltcplay/morning.py",
  '''                if confirmed is not True:
                    return False, (f"Unlocking {label} needs a confirm on "''',
  '''                if False:
                    return False, (f"Unlocking {label} needs a confirm on "'''),

 ("morning: a lock no longer reaches the flame link (T5)",
  "ltcplay/morning.py",
  '''        knows is still zeroed here while the problem stands)."""
        link = self._link_get()''',
  '''        knows is still zeroed here while the problem stands)."""
        return
        link = self._link_get()'''),

 ("morning: Abort leaves a purge selection and the laser tests as they are (T7)",
  "ltcplay/morning.py",
  '''        self.purge.clear(f"Abort pressed by {who or screen or 'someone'}")
        for hook in list(self.abort_hooks):
            try:
                hook(who, screen)
            except Exception:                    # noqa: BLE001
                pass
        self.lasers.all_off("Abort pressed")''',
  '''        for hook in list(self.abort_hooks):
            try:
                hook(who, screen)
            except Exception:                    # noqa: BLE001
                pass'''),

 ("morning: a laser test turns on while a show is live (T10)",
  "ltcplay/morning.py",
  '''            if on:
                why = self._live_or_broken()
                if why:
                    return 409, {"error": f"Laser tests are not allowed now: "''',
  '''            if False:
                why = self._live_or_broken()
                if why:
                    return 409, {"error": f"Laser tests are not allowed now: "'''),

 ("morning: laser tests stay on when a show goes live (T10)",
  "ltcplay/morning.py",
  '''        if self.lasers.on:
            why = self._live_or_broken()
            if why:
                self.lasers.all_off(why)''',
  '''        if False:
            why = self._live_or_broken()
            if why:
                self.lasers.all_off(why)'''),

 ("morning: a locked laser's lock messages are not sent again at show start (T10)",
  "ltcplay/morning.py",
  '''        if live and not self._was_live:''',
  '''        if False:'''),

 ("morning: a Waver whose group arms late fires with its head still down (T12)",
  "ltcplay/morning.py",
  '''                    since = heads.setdefault(m.id, now)''',
  '''                    since = f["start"] if f is not None else now'''),

 ("morning: a machine joins a live fire press (T12)",
  "ltcplay/morning.py",
  '''        if self.press_live():''',
  '''        if False:'''),

 ("morning: the deck's heartbeat is taken for a page's (T3)",
  "ltcplay/morning.py",
  '''            deck = local and screen == "Stream Deck"''',
  '''            deck = False'''),

 ("morning: a lock table that cannot be trusted is written over (T5, T6)",
  "ltcplay/morning.py",
  '''        why = self.problem(cfg)
        if why:
            return False, (f"Nothing was locked or unlocked: {why}. The "''',
  '''        why = self.problem(cfg)
        if False:
            return False, (f"Nothing was locked or unlocked: {why}. The "'''),

 ("morning: another device's open page keeps the owner's purge selection alive (T4)",
  "ltcplay/morning.py",
  '''            self.purge.tick(now, self.device_open(self.purge.owner))''',
  '''            self.purge.tick(now, self.page_open())'''),

 ("morning: locks flamesafe still holds and this engine does not know are written over (T6)",
  "ltcplay/morning.py",
  '''        extra = self._flamesafe_extra()
        if extra:
            return (f"flamesafe holds flame slots {extra} locked that this "''',
  '''        extra = self._flamesafe_extra()
        if False:
            return (f"flamesafe holds flame slots {extra} locked that this "'''),

 ("morning: a config changed under a lock is written over (T5)",
  "ltcplay/morning.py",
  '''        if self.file_channels is not None and \\
                set(self.file_channels) != self.channels(cfg):''',
  '''        if False:'''),

 ("morning: a Waver's head clock survives its group dropping out of ARMED (T12)",
  "ltcplay/morning.py",
  '''                    heads.pop(mid, None)
                    waiting.append(f"{m.name}: group {m.group} is not armed")''',
  '''                    waiting.append(f"{m.name}: group {m.group} is not armed")'''),

 ("morning: any device stops or clears the owner's purge (T3)",
  "ltcplay/morning.py",
  '''            if not mine and (pv["selected"] or pv["firing"]):''',
  '''            if False:'''),

 ("morning: flamesafe reading another lockout file than this engine writes goes unsaid (T6)",
  "ltcplay/morning.py",
  '''        elif os.path.realpath(p) != mine:''',
  '''        elif False:'''),

 ("morning: a Waver head settle time below 0 is accepted (T12)",
  "ltcplay/morning.py",
  '''HEAD_SETTLE_MIN_S = 0.0''',
  '''HEAD_SETTLE_MIN_S = -1.0'''),

 ("morning: a Waver head settle time of 0 is refused again",
  "ltcplay/morning.py",
  '''HEAD_SETTLE_MIN_S = 0.0''',
  '''HEAD_SETTLE_MIN_S = 0.5'''),

 ("morning: a Waver with head settle 0 still waits a frame to fire",
  "ltcplay/morning.py",
  '''                    if now - since < w["head_settle_s"]:''',
  '''                    if now - since <= w["head_settle_s"]:'''),

 ("morning: a purge goes while flamesafe has not confirmed the lock (T5)",
  "ltcplay/morning.py",
  '''        want = sorted(self.lockout.channels(self.cfg))
        if not want:
            return ""''',
  '''        want = sorted(self.lockout.channels(self.cfg))
        if True:
            return ""'''),

 # -- Arrange mode (Jeff, 2026-10-19, bench 2026.10.19-6a32ff9: a picked-up
 # machine followed the mouse for ever, and the next click ARMED it). Each
 # of these takes one guard away; the selftest's node DOM test and the
 # engine's route test must notice every one.
 ("morning page: in Arrange mode a press on a machine still arms it",
  "ltcplay/web/remote.html",
  '''  if(ARRANGE){ dragStart(e, m); return; }''',
  '''  if(ARRANGE){ dragStart(e, m); }'''),

 ("morning page: letting go never ends the drag, so the machine keeps "
  "following the pointer",
  "ltcplay/web/remote.html",
  '''window.addEventListener("pointerup", () => dragEnd("let go"));''',
  ''''''),

 ("morning page: Save layout leaves the machine on the pointer",
  "ltcplay/web/remote.html",
  '''  dragEnd(how === "save" ? "Save layout" : how === "cancel" ? "Cancel" : how === "reset" ? "Reset layout" : "the page was left");''',
  ''''''),

 ("morning page: Escape does not drop a held machine",
  "ltcplay/web/remote.html",
  '''document.addEventListener("keydown", e => { if(e.key === "Escape" && DRAG) dragEnd("Escape", true); });''',
  ''''''),

 ("morning page: the window losing focus leaves the machine on the pointer",
  "ltcplay/web/remote.html",
  '''window.addEventListener("blur", () => dragEnd("the window lost focus"));''',
  ''''''),

 ("morning page: the Space bar fires while arranging",
  "ltcplay/web/remote.html",
  '''  if(arrangeBlocked("purge-fire", {})) return;        // the Space bar, while arranging''',
  ''''''),

 ("morning page: a group button, a lock or a laser test goes while arranging "
  "(the gate every press meets is gone)",
  "ltcplay/web/remote.html",
  '''  if(arrangeBlocked(route, extra)) return;
  try{''',
  '''  try{'''),

 ("morning page: Arrange mode holds nothing back",
  "ltcplay/web/remote.html",
  '''  if(!ARRANGE) return false;
  const off = extra && extra.on === false;''',
  '''  if(true) return false;
  const off = extra && extra.on === false;'''),

 ("windows: the rack screen opens as a kiosk again (no Minimize, and "
  "Explorer opens behind it)",
  "packaging/windows/supervisor.py",
  '''    cmd = [edge, f"--app={url}", "--start-maximized", "--no-first-run",''',
  '''    cmd = [edge, "--kiosk", url, "--edge-kiosk-type=fullscreen", "--no-first-run",'''),

 ("remote: the Explorer window opened for the show folder is never picked "
  "out by its title",
  "ltcplay/remote.py",
  '''    if titled:
        return titled[0][0]
    if len(new) == 1:''',
  '''    if len(new) == 1:'''),

 ("remote: an Explorer window already open on the folder (reused) is left "
  "behind the rack screen",
  "ltcplay/remote.py",
  '''    if reuse and not new:''',
  '''    if False:'''),

 ("morning page: the open beat never tells the engine the screen is arranging",
  "ltcplay/web/remote.html",
  '''Object.assign({arranging: ARRANGE, inst: BEAT_INST''',
  '''Object.assign({arranging: false, inst: BEAT_INST'''),

 ("morning page: Save layout does not leave Arrange mode, so arming stays off "
  "and the banner stays",
  "ltcplay/web/remote.html",
  '''  ARRANGE = false; ARRANGE_POS = {};
  // The page and the engine hear''',
  '''  ARRANGE_POS = {};
  // The page and the engine hear'''),

 ("morning page: pointercancel no longer ends a drag",
  "ltcplay/web/remote.html",
  '''window.addEventListener("pointercancel", () => dragEnd("the pointer was cancelled"));''',
  '''window.addEventListener("pointercancel", () => {});'''),

 ("morning page: pagehide no longer ends a drag",
  "ltcplay/web/remote.html",
  '''window.addEventListener("pagehide", () => dragEnd("the page closed"));''',
  '''window.addEventListener("pagehide", () => {});'''),

 ("morning page: the page going hidden no longer ends a drag",
  "ltcplay/web/remote.html",
  '''document.addEventListener("visibilitychange", () => { if(document.hidden) dragEnd("the page was hidden"); });''',
  '''document.addEventListener("visibilitychange", () => {});'''),

 ("morning page: leaving the Morning page stays in Arrange mode",
  "ltcplay/web/remote.html",
  '''    arrangeExit("left");''',
  '''    void 0;'''),

 ("morning page: entering Arrange does not stop a fire press",
  "ltcplay/web/remote.html",
  '''  fireStop("Arrange mode");''',
  '''  void 0;'''),

 ("morning page: entering Arrange keeps this screen's purge selection armed",
  "ltcplay/web/remote.html",
  '''    post("/api/remote/purge-clear", actor({})).catch(() => {});
  $("mo-msg").textContent = "Press a machine and move it''',
  '''    void 0;
  $("mo-msg").textContent = "Press a machine and move it'''),

 ("morning page: an L lock button locks or unlocks while arranging",
  "ltcplay/web/remote.html",
  '''  if(arrangeBlocked("lock", {})) return;        // before anything is sent''',
  '''  // gate gone'''),

 ("morning page: the group buttons stay live while arranging",
  "ltcplay/web/remote.html",
  '''  return !ids.length || (!any && (!mo.purge_enabled || arrange || !mayRow(st, "arm")));''',
  '''  return !ids.length || (!any && (!mo.purge_enabled || !mayRow(st, "arm")));'''),

 ("morning page: a bare mouse (no button down) still drags the machine",
  "ltcplay/web/remote.html",
  '''  if(ev.pointerType === "mouse" && ev.buttons === 0){ dragEnd("the button was already up"); return; }''',
  '''  if(false){ dragEnd("the button was already up"); return; }'''),

 ("morning page: losing pointer capture does not end the drag",
  "ltcplay/web/remote.html",
  '''    if(DRAG && DRAG.el === el && !(el.hasPointerCapture && el.hasPointerCapture(DRAG.pointerId))) dragEnd("the pointer was released");''',
  '''    void 0;'''),

 ("morning page: the banner still says arranging while Save is in flight",
  "ltcplay/web/remote.html",
  '''  post("/api/remote/morning-open", openBeatBody(how === "left")).catch(() => {});
  renderMorning();
  if(how === "save") await moPress''',
  '''  if(how === "save") await moPress'''),

 ("morning: one window's ordinary beat clears another window's Arrange mode",
  "ltcplay/morning.py",
  '''                mine.pop(inst, None)
                if not mine:''',
  '''                mine.clear()
                if not mine:'''),

 ("morning: an overtaken (older) beat still changes Arrange mode",
  "ltcplay/morning.py",
  '''            if last is not None and seq <= last[0]:
                return                   # an older beat, overtaken''',
  '''            if False:
                return                   # an older beat, overtaken'''),

 ("remote: a device that may not purge is never held back while it arranges",
  "ltcplay/remote.py",
  '''            m.open_beat(device, arranging=body.get("arranging") is True,
                        inst=body.get("inst"), seq=body.get("seq"),
                        mark_open=False)''',
  '''            pass'''),

 ("morning: a view-only device's Arrange beat marks the page open",
  "ltcplay/morning.py",
  '''        if mark_open:
            self._open_at = now''',
  '''        if True:
            self._open_at = now'''),

 ("morning: the engine ignores which window a beat came from",
  "ltcplay/morning.py",
  '''                           inst=body.get("inst"), seq=body.get("seq"),
                           mark_open=body.get("leaving") is not True)''',
  '''                           seq=body.get("seq"),
                           mark_open=body.get("leaving") is not True)'''),

 ("remote: a view-only device's beat ignores which window it came from",
  "ltcplay/remote.py",
  '''                        inst=body.get("inst"), seq=body.get("seq"),
                        mark_open=False)''',
  '''                        seq=body.get("seq"),
                        mark_open=False)'''),

 ("morning: the beat sent on leaving the page holds the page open 2 s more",
  "ltcplay/morning.py",
  '''                           mark_open=body.get("leaving") is not True)''',
  '''                           mark_open=True)'''),

 ("morning page: the beat sent on leaving the page does not say so",
  "ltcplay/web/remote.html",
  '''openBeatBody(how === "left")''',
  '''openBeatBody(false)'''),

 ("morning: a screen in Arrange mode can still arm or unlock at the engine",
  "ltcplay/morning.py",
  '''        if not gated or not self.device_arranging(device):
            return ""''',
  '''        if True:
            return ""'''),

 ("morning: a screen going into Arrange mode keeps its purge selection armed",
  "ltcplay/morning.py",
  '''            if self.purge.owner == device and (self.purge.selected or
                                               self.purge.fire is not None):
                self.purge.clear(f"{device} went into Arrange mode")''',
  '''            pass'''),

 ("morning: the engine's Arrange gate is never consulted by the routes",
  "ltcplay/morning.py",
  '''        why = self.arrange_refusal(name, body, device)
        if why:
            if name == "purge-fire":
                self.purge.stop(f"refused: {why}")
            return 409, {"error": why, "let_go": True}''',
  ''''''),

 # -- Arrange mode disables firing (Jeff, 2026-10-21: "If it's being
 # arranged in any capacity, firing is disabled").
 ("morning: firing is held back only while the requesting device itself "
  "arranges, not while another screen does",
  "ltcplay/morning.py",
  '''        if name == "purge-fire":
            return self.fire_arrange_refusal()''',
  '''        if name == "purge-fire":
            return self.fire_arrange_refusal() \\
                if self.device_arranging(device) else ""'''),

 ("morning: the Stream Deck's FIRE key is exempt from Arrange mode again",
  "ltcplay/morning.py",
  '''        why = self.arrange_refusal(name, body, device)
        if why:
            if name == "purge-fire":''',
  '''        deck_fire = name == "purge-fire" and local and screen == "Stream Deck"
        why = "" if deck_fire else self.arrange_refusal(name, body, device)
        if why:
            if name == "purge-fire":'''),

 ("morning: a fire press keeps going when another screen goes into Arrange "
  "mode",
  "ltcplay/morning.py",
  '''            if self.purge.fire is not None:
                # Whoever is firing, from whatever surface''',
  '''            if False:
                # Whoever is firing, from whatever surface'''),

 ("morning: the watcher never ends a fire press while a screen arranges",
  "ltcplay/morning.py",
  '''            held = self.fire_arrange_refusal()
            if held:
                try:''',
  '''            held = ""
            if held:
                try:'''),

 ("morning: a fire heartbeat refused by Arrange mode leaves the press going",
  "ltcplay/morning.py",
  '''            if name == "purge-fire":
                self.purge.stop(f"refused: {why}")''',
  '''            if False:
                self.purge.stop(f"refused: {why}")'''),

 ("morning: an Arrange state that cannot be read lets the purge fire",
  "ltcplay/morning.py",
  '''            return (f"Firing is off: whether a screen is in Arrange mode "
                    f"could not be read ({type(e).__name__}). Nothing was "
                    f"fired.")''',
  '''            return ""'''),

 ("morning: an Arrange entry whose page crashed holds firing off for ever",
  "ltcplay/morning.py",
  '''                      if any(now - t <= OPEN_STALE_S
                             for t in list(insts.values())))''',
  '''                      if insts)'''),

 ("morning: the fire refusal does not say which screen is arranging",
  "ltcplay/morning.py",
  '''        names = " and ".join(who)''',
  '''        names = "a screen"'''),

 ("morning: Arrange mode holds back a lockout",
  "ltcplay/morning.py",
  '''        gated = (name == "lock" and (off or body.get("kind") == "rewrite")) \\''',
  '''        gated = name == "lock" \\'''),

 ("morning: an unlock goes from a screen in Arrange mode",
  "ltcplay/morning.py",
  '''        gated = (name == "lock" and (off or body.get("kind") == "rewrite")) \\''',
  '''        gated = (name == "lock" and body.get("kind") == "rewrite") \\'''),

 ("morning: a rewrite of the lockout file goes from a screen in Arrange mode",
  "ltcplay/morning.py",
  '''        gated = (name == "lock" and (off or body.get("kind") == "rewrite")) \\''',
  '''        gated = (name == "lock" and off) \\'''),

 # -- a purge fire release can never start a fire (found on PR #82: "on":
 # false skipped the purge arming switch, counted as a safety press, and
 # still fired 240 on slot 411). Each guard on its own.
 ("users: a purge fire asks to fire for any truthy on, not only the JSON "
  "word true (the strict bool removed)",
  "ltcplay/users.py",
  '''    return isinstance(body, dict) and body.get("on") is True''',
  '''    return isinstance(body, dict) and bool(body.get("on"))'''),

 ("users: a purge fire is a safety press by whether on is false again, "
  "not by what the engine does with it",
  "ltcplay/users.py",
  '''            return "arm" if purge_fire_wanted(raw) else "safety"''',
  '''            return "safety" if body.get("on") is False else "arm"'''),

 ("remote: the purge arming switch is skipped by whether on is false again, "
  "not by what the purge fire does",
  "ltcplay/remote.py",
  '''                (name == "purge-fire" and users_mod.purge_fire_wanted(body)):''',
  '''                (name == "purge-fire" and body.get("on") is not False):'''),

 ("remote: the purge arming switch is never asked about a purge fire",
  "ltcplay/remote.py",
  '''                (name == "purge-fire" and users_mod.purge_fire_wanted(body)):''',
  '''                False:'''),

 ("morning: a purge fire release reaches the fire path instead of the stop",
  "ltcplay/morning.py",
  '''        if name == "purge-fire" and not purge_fire_wanted(raw):''',
  '''        if False:'''),

 ("morning: the fire heartbeat starts a press whatever on says",
  "ltcplay/morning.py",
  '''        if not purge_fire_wanted(body):
            stopped = self.stop(''',
  '''        if False:
            stopped = self.stop('''),

 ("deck: the FIRE key's heartbeat stops saying on true",
  "ltcplay/streamdeck.py",
  '''            body["on"] = True''',
  '''            pass'''),

 ("page: the Space bar's fire heartbeat stops saying on true",
  "ltcplay/web/remote.html",
  '''moActor({fire_id:f.id, on:true})''',
  '''moActor({fire_id:f.id})'''),

 ("morning: one window's beat numbers are counted against another window's",
  "ltcplay/morning.py",
  '''        key = (device, inst)''',
  '''        key = (device, "")'''),

 ("remote: an iPad unlocks a machine or lights a laser test without screen_arming (T9, T10)",
  "ltcplay/remote.py",
  '''                not self.may_act_from(ctx.local):''',
  '''                False:'''),

 ("remote: a device that may not purge keeps the page open and the deck re-mapped (T11)",
  "ltcplay/remote.py",
  '''        if name == "morning-open" and not self.purge_enabled(ctx.local):''',
  '''        if False:'''),

 ("flamesafe: a deleted lockout file lifts every lock (T6)",
  "flamesafe/service.py",
  '''            if self._slots:
                # A file that held locks and is gone (review of PR #68,''',
  '''            if False:
                # A file that held locks and is gone (review of PR #68,'''),

 ("flamesafe: a lockout_file left out of the config locks nothing",
  "flamesafe/config.py",
  '''        lockout = "ltcplay_lockout.json" if source != "config" else None''',
  '''        lockout = None'''),

 ("deck: a purge arm skips the hold's own guards (T8)",
  "ltcplay/streamdeck.py",
  '''            self._do_arm_fire(i, now, who=who, screen="Morning page")
            if not self.arm.wanted[i]:
                continue             # refused; _do_arm_fire said why''',
  '''            self.arm.set_group(i, True)
            self.arm.send(self.names)'''),

 ("morning: a purge from the show network needs no screen_arming (T9)",
  "ltcplay/remote.py",
  '''        return bool(local) or rs["screen_arming"] is True''',
  '''        return True'''),

 ("morning: purge_arming false still arms a purge (T9)",
  "ltcplay/remote.py",
  '''        if rs["purge_arming"] is not True:
            return False''',
  '''        if False:
            return False'''),

 ("morning: a purge click arms with no Stream Deck reading the engine",
  "ltcplay/remote.py",
  '''            if not deck_alive:
                return 409, {"error": "The Stream Deck program has not read "''',
  '''            if False:
                return 409, {"error": "The Stream Deck program has not read "'''),

 ("flamelink: a purge frame goes out while a show is live and its cues are zeroed (T1)",
  "ltcplay/flamelink.py",
  '''        if self.zeroed or tc is None or land:
            self._cue_episode("", "")
            return tc, zeros''',
  '''        if self.zeroed or tc is None or land:
            self._cue_episode("", "")
            return tc, self._purge_values(now)'''),

 ("flamelink: a stale purge frame keeps firing (T2)",
  "ltcplay/flamelink.py",
  '''        if now - p[1] > PURGE_HOLD_S or now < p[1]:
            self._purge = None
            return bytes(UNIVERSE_SIZE)''',
  '''        if False:
            self._purge = None
            return bytes(UNIVERSE_SIZE)'''),

 ("flamelink: zero() no longer clears a purge frame (T7)",
  "ltcplay/flamelink.py",
  '''        with self._lock:
            self._purge = None
            # Any zero (a Hold''',
  '''        with self._lock:
            # Any zero (a Hold'''),

 ("flamelink: disarm_all no longer clears a purge frame (T7)",
  "ltcplay/flamelink.py",
  '''                self._purge = None
                # An Abort ends live flame programming''',
  '''                # An Abort ends live flame programming'''),

 ("flamelink: a locked channel's value goes out in a frame (T5)",
  "ltcplay/flamelink.py",
  '''    def _unlocked(self, vals):
        locked = self._locked
        if not locked or not any(vals):
            return vals''',
  '''    def _unlocked(self, vals):
        return vals
        locked = self._locked
        if not locked or not any(vals):
            return vals'''),

 ("flamesafe: a locked fire slot still fires (T5)",
  "flamesafe/composer.py",
  '''                if f in self._locked:
                    out = 0
                    if v != 0:
                        self.stats["lockout_zeroed"] += 1
                elif armed_now and (not quiet or f in g.aim):''',
  '''                if False:
                    out = 0
                    if v != 0:
                        self.stats["lockout_zeroed"] += 1
                elif armed_now and (not quiet or f in g.aim):'''),

 ("flamesafe: a group with every fire slot locked still arms",
  "flamesafe/composer.py",
  '''            if self._group_locked_out(g):''',
  '''            if False:'''),

 # Multi-safety (Jeff, 2026-10-26): one arm group driving several fixtures,
 # each with a safety slot of its own (the wave flamer).
 ("flamesafe: a safety slot listed twice in a group is accepted",
  "flamesafe/config.py",
  '''    if len(set(raw)) != len(raw):
        raise ConfigError(f"{name}: a safety slot is listed twice.")''',
  '''    if False:
        raise ConfigError(f"{name}: a safety slot is listed twice.")'''),

 ("flamesafe: an empty safety list is accepted",
  "flamesafe/config.py",
  '''    if not raw:
        raise ConfigError(f"{name}: safety is an empty list. Give the safety "''',
  '''    if False:
        raise ConfigError(f"{name}: safety is an empty list. Give the safety "'''),

 ("flamesafe: a safety slot in a list is not range checked",
  "flamesafe/config.py",
  '''        if isinstance(s, bool) or not isinstance(s, int) \\
                or not (1 <= s <= rules.UNIVERSE_SIZE):
            raise ConfigError(f"{name}: safety slot {s!r} is not a slot "''',
  '''        if isinstance(s, bool) or not isinstance(s, int):
            raise ConfigError(f"{name}: safety slot {s!r} is not a slot "'''),

 ("flamesafe: only the first safety slot is checked against the group's fire slots",
  "flamesafe/config.py",
  '''            if f in safeties:''',
  '''            if f == safeties[0]:'''),

 ("flamesafe: only the first safety slot of a group is checked for sharing",
  "flamesafe/config.py",
  '''        for s in g.safeties:
            if s in seen:''',
  '''        for s in g.safeties[:1]:
            if s in seen:'''),

 ("flamesafe: another group's fire slot may be a later safety slot of a list",
  "flamesafe/config.py",
  '''    safeties = {s for g in c.groups for s in g.safeties}''',
  '''    safeties = {g.safety for g in c.groups}'''),

 ("flamesafe: a multi-safety group raises only its first safety slot",
  "flamesafe/composer.py",
  '''            for s in g.safeties:
                buf[s - 1] = values[i] if s in self._raised[i] else DISARM''',
  '''            for s in g.safeties[:1]:
                buf[s - 1] = values[i] if s in self._raised[i] else DISARM'''),

 ("flamesafe: a locked safety slot of a multi-safety group still rises",
  "flamesafe/composer.py",
  '''                self._raised[i] = self._raised[i] - locked_saf''',
  '''                self._raised[i] = self._raised[i]'''),

 ("flamesafe: a multi-safety group with every safety slot locked still arms",
  "flamesafe/composer.py",
  '''            if g.multi_safety and len(locked_saf) == len(g.safeties):''',
  '''            if False:'''),

 ("flamesafe: a dropped or overwritten safety slot no longer takes the group down",
  "flamesafe/composer.py",
  '''            if not down and len(locked_saf) < len(g.safeties):
                continue''',
  '''            if True:
                continue'''),

 # safety_profile (Jeff, 2026-10-26): the wave flamer arms at Jeff's own
 # number (178), every guard optional and against his numbers only.
 ("flamesafe: a profile's arm value may be the disarm value 0",
  "flamesafe/config.py",
  "    arm = _int(p, \"arm_value\", rules.DISARM_VALUE + 1, 255,",
  "    arm = _int(p, \"arm_value\", rules.DISARM_VALUE, 255,"),

 ("flamesafe: a profile's arm value may be past 255",
  "flamesafe/config.py",
  "    arm = _int(p, \"arm_value\", rules.DISARM_VALUE + 1, 255,",
  "    arm = _int(p, \"arm_value\", rules.DISARM_VALUE + 1, 511,"),

 ("flamesafe: a profile without arm_value is accepted",
  "flamesafe/config.py",
  "    if \"arm_value\" not in p:\n",
  "    if False:\n"),

 ("flamesafe: a profile with half an enable window is accepted",
  "flamesafe/config.py",
  "    if has_lo != has_hi:",
  "    if False:"),

 ("flamesafe: a null enable_lo is read as a number",
  "flamesafe/config.py",
  "    has_lo = p.get(\"enable_lo\") is not None",
  "    has_lo = \"enable_lo\" in p"),

 ("flamesafe: a profile's enable window may hold the disarm value 0",
  "flamesafe/config.py",
  "        if lo <= rules.DISARM_VALUE:",
  "        if False:"),

 ("flamesafe: a profile's arm value may sit outside its enable window",
  "flamesafe/config.py",
  "        if not (lo <= arm <= hi):",
  "        if False:"),

 ("flamesafe: a profile's fire_at is never read",
  "flamesafe/config.py",
  "    if p.get(\"fire_at\") is not None:",
  "    if False:"),

 ("flamesafe: a profile's arm value may be at or above its fire_at",
  "flamesafe/config.py",
  "        if arm >= fire_at:",
  "        if False:"),

 ("flamesafe: a single-bit flip of a profile's arm value may reach fire_at",
  "flamesafe/config.py",
  "            if neighbour >= fire_at:",
  "            if False:"),

 ("flamesafe: the profile's bit-flip guard skips the top bits",
  "flamesafe/config.py",
  "            neighbour = arm ^ (1 << bit)",
  "            neighbour = arm ^ (1 << (bit % 6))"),

 ("flamesafe: the profile's bit-flip guard misses a flip landing on fire_at",
  "flamesafe/config.py",
  "            if neighbour >= fire_at:",
  "            if neighbour > fire_at:"),

 ("flamesafe: a group arm_value that disagrees with its profile is accepted",
  "flamesafe/config.py",
  "                if other != arm_value:",
  "                if False:"),

 ("flamesafe: an unknown key in a profile is accepted",
  "flamesafe/config.py",
  "    unknown = sorted(k for k in p if k not in PROFILE_KEYS)",
  "    unknown = []"),

 ("witness: the checker judges a profile group at the file's arm value",
  "tools/stress/witness/check_witness.py",
  '        arm_of[g["name"]] = group_arm_value(g, arm_value)',
  '        arm_of[g["name"]] = int(g.get("arm_value", arm_value))'),

 ("witness: the checker never reads a safety_profile",
  "tools/stress/witness/check_witness.py",
  '    if isinstance(prof, dict) and "arm_value" in prof:\n'
  '        return int(prof["arm_value"])\n'
  '    return int(g.get("arm_value", arm))',
  '    return int(g.get("arm_value", arm))'),

 ("witness: the recorder never reads a safety_profile",
  "tools/stress/witness/witness.py",
  '    if isinstance(prof, dict) and "arm_value" in prof:\n'
  '        return int(prof["arm_value"])\n'
  '    return int(g.get("arm_value", arm))',
  '    return int(g.get("arm_value", arm))'),

 ("rack screen: a group's arm value is dropped from the card",
  "ltcplay/remote.py",
  '                           "arm_value": _arm(g.get("arm_value")),',
  '                           "arm_value": None,'),

 ("flamesafe: a profile group arms at the file's arm value",
  "flamesafe/config.py",
  '            arm_value = profile["arm_value"]',
  "            arm_value = c.arm_value"),

 ("flamesafe: a profile also lets 178 through on other groups",
  "flamesafe/config.py",
  "        elif \"arm_value\" in g:\n"
  "            arm_value = _int(g, \"arm_value\", 0, 255, f\"{name}: arm_value\")\n"
  "            _validate_arm_value(arm_value, c.gflame_range,",
  "        elif \"arm_value\" in g:\n"
  "            arm_value = _int(g, \"arm_value\", 0, 255, f\"{name}: arm_value\")\n"
  "            if any('safety_profile' in x for x in groups): pass\n"
  "            else: _validate_arm_value(arm_value, c.gflame_range,"),

 ("flamesafe: the start line says a guarded profile has no fire guard",
  "flamesafe/__main__.py",
  '             if p["fire_at"] is None else',
  '             if True else'),

 ("flamesafe: the status frame gives every group the file's arm value",
  "flamesafe/composer.py",
  '                "arm_value": g.arm_value,',
  '                "arm_value": self.cfg.arm_value,'),

 ("flamesafe: a locked safety slot is not left out of the arm",
  "flamesafe/composer.py",
  '''                    if s not in locked_saf and buf[s - 1] != g.arm_value]''',
  '''                    if buf[s - 1] != g.arm_value]'''),

 ("flamesafe: a lock on a one-safety group's safety slot changes it",
  "flamesafe/composer.py",
  '''        if not g.multi_safety:
            return frozenset()''',
  '''        if False:
            return frozenset()'''),

 ("flamesafe: a group taken down for a dropped safety keeps firing",
  "flamesafe/composer.py",
  '''            for f in g.fire:
                buf[f - 1] = 0
            values[i] = DISARM''',
  '''            values[i] = DISARM'''),

 ("flamesafe: a group taken down for a dropped safety comes back by itself",
  "flamesafe/composer.py",
  '''            self._latched[i] = False
            self._seen_down[i] = False
            self._edge_quiet[i] = 0
            self._unconfirmed[i] = True''',
  '''            self._seen_down[i] = False
            self._edge_quiet[i] = 0
            self._unconfirmed[i] = True'''),

 ("flamesafe: the status says every safety slot is up",
  "flamesafe/composer.py",
  '''                "safeties_up": list(ups[i]),''',
  '''                "safeties_up": [True] * len(g.safeties),'''),

 ("flamesafe: the status never says a safety slot is locked",
  "flamesafe/composer.py",
  '''                "safeties_locked": list(locks[i]),''',
  '''                "safeties_locked": [False] * len(g.safeties),'''),

 ("multi-safety: fire_ice checks only the first safety slot of a list",
  "ltcplay/fire_ice.py",
  '''            slots = list(safety)
        else:''',
  '''            slots = list(safety[:1])
        else:'''),

 ("multi-safety: the rack screen loses the per-safety state",
  "ltcplay/remote.py",
  '''                           "safeties_up": [bool(x) for x in
                                           _list(g.get("safeties_up"))],''',
  '''                           "safeties_up": [],'''),

 ("multi-safety: the card shows the safety line for a one-safety group",
  "ltcplay/web/remote.html",
  '''  if(up.length < 2 || c === "unknown") return "";''',
  '''  if(c === "unknown") return "";'''),

 ("multi-safety: a locked wave flamer machine does not lock its safety slot",
  "ltcplay/morning.py",
  '''                                   pairs[s], bank=bank, notes=notes,
                                   safety=s))''',
  '''                                   pairs[s], bank=bank, notes=notes))'''),

 # Cold review of PR #107: P2-1, a multi-safety machine's safety is paired
 # by the slot layout; P2-3, the witness checker's multi-safety rules.
 ("multi-safety: wave flamer machines pair safety and fire by list order",
  "ltcplay/morning.py",
  '''            order = sorted(pairs, key=lambda s: pairs[s][0])''',
  '''            order = list(pairs)'''),

 ("multi-safety: a pairing that leaves a fire slot out is taken",
  "ltcplay/morning.py",
  '''    return out if seen == fire else None''',
  '''    return out'''),

 ("multi-safety: a whole-group machine does not lock the safety slots",
  "ltcplay/morning.py",
  '''                                   g["name"], list(g["fire"]), bank=bank,
                                   notes=notes, safeties=saf))''',
  '''                                   g["name"], list(g["fire"]), bank=bank,
                                   notes=notes))'''),

 ("multi-safety: a wave flamer machine may leave out its safety slot",
  "ltcplay/morning.py",
  '''        if groups and len(saf_of[group]) > 1 and "safety" not in d:''',
  '''        if False:'''),

 ("multi-safety: a wave flamer machine may name another fixture's safety",
  "ltcplay/morning.py",
  '''                if other:
                    owner = next(''',
  '''                if False:
                    owner = next('''),

 ("witness: a locked safety slot carrying the arm value goes unflagged",
  "tools/stress/witness/check_witness.py",
  '''                    ep.add("LOCKED SLOT", slot, t,
                           f"{name}: locked safety slot {slot} = {v}")''',
  '''                    pass'''),

 ("witness: fire with every safety locked is not fire without arm",
  "tools/stress/witness/check_witness.py",
  '''                if not need:
                    ep.add("FIRE WITHOUT ARM", slot, t,''',
  '''                if False:
                    ep.add("FIRE WITHOUT ARM", slot, t,'''),

 ("witness: a locked one-safety group's safety is left out of fire without arm",
  "tools/stress/witness/check_witness.py",
  '''                need = [s for s in sl if s not in locked] if len(sl) > 1 \\
                    else sl''',
  '''                need = [s for s in sl if s not in locked]'''),

 ("witness: only the first needed safety slot is checked for fire without arm",
  "tools/stress/witness/check_witness.py",
  '''                down = [(s, values.get(s, 0)) for s in need
''',
  '''                down = [(s, values.get(s, 0)) for s in need[:1]
'''),

 ("witness: a locked safety slot is flagged with no lock on record",
  "tools/stress/witness/check_witness.py",
  '''                if slot in locked and st["locked"]:
                    # flamesafe holds''',
  '''                if slot in locked:
                    # flamesafe holds'''),

 ("flamesafe: rubbish lock data locks nothing (T6)",
  "flamesafe/composer.py",
  '''            got = frozenset(range(1, rules.UNIVERSE_SIZE + 1))
            state = "unreadable"''',
  '''            got = frozenset()
            state = "unreadable"'''),

 ("flamesafe: an unreadable lockout file lifts every lock (T6)",
  "flamesafe/service.py",
  '''            self._state = "unreadable"
            return self._slots, self._state
        self._unreadable_said = False''',
  '''            self._slots = frozenset()
            self._state = "unreadable"
            return self._slots, self._state
        self._unreadable_said = False'''),

 ("flamesafe: the lockout file is never read",
  "flamesafe/service.py",
  '''        self._poll_lockout()
        out = self.composer.tick()''',
  '''        out = self.composer.tick()'''),

 ("deck: a purge group stays armed after the page stops asking for it (T4)",
  "ltcplay/streamdeck.py",
  '''            if self.arm.wanted[i]:
                self._do_disarm(i, who="the purge ended",''',
  '''            if False:
                self._do_disarm(i, who="the purge ended",'''),

 ("deck: a purge arm ignores the re-arm refractory window (T8)",
  "ltcplay/streamdeck.py",
  '''        left = self._in_rearm_refractory(i, now)
        if left > 0:
            self._log(f"Stream Deck: {self.names[i]} arm-hold completed "''',
  '''        left = 0
        if left > 0:
            self._log(f"Stream Deck: {self.names[i]} arm-hold completed "'''),

 ("deck: the page closing leaves the purge groups armed (T4, T11)",
  "ltcplay/streamdeck.py",
  '''        if not m or m.get("open") is not True or \\
                m.get("purge_enabled") is not True or self._latched_now():
            return set()''',
  '''        if not m or \\
                m.get("purge_enabled") is not True or self._latched_now():
            return set()'''),

 ("deck: a stale engine keeps the deck re-mapped and its purge groups armed (T4, T11)",
  "ltcplay/streamdeck.py",
  '''        if last is None or self._clock() - last[1] > SCREEN_STALE_S:
            return None
        m = last[0].get("morning")''',
  '''        if last is None:
            return None
        m = last[0].get("morning")'''),

 ("deck: the FIRE heartbeats carry on after the engine refuses one (T3)",
  "ltcplay/streamdeck.py",
  '''            if not ok:
                if press.stop.is_set():
                    return
                # Any refusal ends the press: the operator lets go and''',
  '''            if False:
                if press.stop.is_set():
                    return
                # Any refusal ends the press: the operator lets go and'''),

 ("lasertest: a locked laser's test turns on (T10)",
  "ltcplay/lasertest.py",
  '''            if self._locked(laser.id):
                self.last_result[laser.id] = "refused"''',
  '''            if False:
                self.last_result[laser.id] = "refused"'''),

 ("lasertest: BEYOND's BlackOut becomes an allowed address (T10)",
  "ltcplay/lasertest.py",
  '_REFUSED = frozenset(a.lower() for a in FORBIDDEN_ADDRESSES |\n'
  '                     GLOBAL_ADDRESSES)',
  '_REFUSED = frozenset(a.lower() for a in GLOBAL_ADDRESSES)'),

 ("checklist: a tick needs no name",
  "ltcplay/checklist.py",
  '''        if who not in self.cfg.names:
            if not self.cfg.names:''',
  '''        if False:
            if not self.cfg.names:'''),

 ("checklist: the records no longer chain to the one before (T14)",
  "ltcplay/checklist.py",
  '''            rec["prev"] = prev
            rec["hash"] = record_hash(rec)''',
  '''            rec["prev"] = GENESIS
            rec["hash"] = record_hash(rec)'''),

 ("checklist: verify no longer checks a record's hash (T14)",
  "ltcplay/checklist.py",
  '''        if r.get("hash") != record_hash(r):
            return False, i - 1, f"record {i} was changed after it was written"''',
  '''        if False:
            return False, i - 1, f"record {i} was changed after it was written"'''),

 ("checklist: the tally queues records while it is switched off",
  "ltcplay/checklist.py",
  '''        if not self.enabled:
            return False
        doc = self.payload(kind, body)''',
  '''        if False:
            return False
        doc = self.payload(kind, body)'''),

 ("checklist: the tally accepts a plain http URL",
  "ltcplay/checklist.py",
  '''    if url and not url.lower().startswith("https://"):
        raise ChecklistConfigError(f"{where}: tally.url has to start with "''',
  '''    if False:
        raise ChecklistConfigError(f"{where}: tally.url has to start with "'''),

 # -- users, sign-in and the audit log (Jeff, 2026-10-20; USERS.md). The
 # safety rule first: no press that only lowers risk is ever behind the
 # role matrix; then passwords, the lockout, the last admin, the chain,
 # the never-blocking writer and the nightly upload's acknowledgement.
 ('users: a safety press is restricted like any other', 'ltcplay/users.py',
  '    if perm == "safety" or perm is None:\n        return True\n    if role is None or role not in ROLES:',
  '    if perm is None:\n        return True\n    if role is None or role not in ROLES:'),

 ('users: a purge turned off is an arming press', 'ltcplay/users.py',
  '                body.get("on") is False:\n            return "safety"',
  '                body.get("on") is False:\n            return "arm"'),

 ('users: locking a machine out needs the unlock row', 'ltcplay/users.py',
  '            return "safety" if body.get("kind") != "rewrite" else "settings"',
  '            return "unlock"'),

 ('users: Abort is a show press the matrix can take away', 'ltcplay/users.py',
  '    "abort": "safety", "hold": "safety",',
  '    "abort": "show", "hold": "safety",'),

 ('users: every password gets the same salt', 'ltcplay/users.py',
  '    salt = salt or secrets.token_bytes(16)',
  '    salt = salt or b"\\0" * 16'),

 ('users: the password itself is stored beside its hash', 'ltcplay/users.py',
  '                "salt": salt.hex(), "hash": h.hex()}',
  '                "salt": salt.hex(), "hash": h.hex(), "pw": str(password)}'),

 ('users: any password matches', 'ltcplay/users.py',
  '        return hmac.compare_digest(got["hash"], str(rec["hash"]))',
  '        return True'),

 ('users: scrypt at a toy cost', 'ltcplay/users.py',
  'SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}',
  'SCRYPT = {"n": 2 ** 4, "r": 8, "p": 1}'),

 ('users: wrong passwords never lock anything', 'ltcplay/users.py',
  '            extra = f[0] - FREE_TRIES\n            if extra > 0:',
  '            extra = f[0] - FREE_TRIES\n            if False:'),

 ('users: the lockout grows without end', 'ltcplay/users.py',
  '                f[1] = now + min(LOCK_MAX_S, LOCK_BASE_S * 2 ** (extra - 1))',
  '                f[1] = now + LOCK_BASE_S * 2 ** (extra - 1)'),

 ('users: a right password does not clear the wrong ones', 'ltcplay/users.py',
  '            for k in keys:\n                self._fails.pop(k, None)\n            token = ',
  '            token = '),

 ('users: the last admin can be removed', 'ltcplay/users.py',
  '                raise ValueError("That is the last admin. Make another admin "\n                                 "first.")\n            del users[real]',
  '                pass\n            del users[real]'),

 ('users: the last admin can be demoted', 'ltcplay/users.py',
  '            if users[real].get("role") == "admin" and role != "admin" and \\',
  '            if False and \\'),

 ('users: a temporary password signs in as it is', 'ltcplay/users.py',
  '        if first or must:',
  '        if first:'),

 ('users: a chosen password can be chosen again by anyone', 'ltcplay/users.py',
  '            first = u is not None and rec is None',
  '            first = u is not None'),

 ('users: the seeded people ship with a password', 'ltcplay/users.py',
  '                                   "changed": self._stamp(), "pw": None,',
  '                                   "changed": self._stamp(), "pw": hash_password("changeme", params=self._hash_params),'),

 ('users: a person an admin adds keeps the temporary password', 'ltcplay/users.py',
  '                           "changed": self._stamp(), "must_change": True,',
  '                           "changed": self._stamp(), "must_change": False,'),

 ("users: an admin's reset is not temporary", 'ltcplay/users.py',
  '        real = self._set_pw(name, pw, temporary=True)',
  '        real = self._set_pw(name, pw, temporary=False)'),

 ('users: an admin can be locked out of managing users', 'ltcplay/users.py',
  'FIXED = {("admin", "users"): True}',
  'FIXED = {}'),

 ('users: idle sign out never signs anyone out', 'ltcplay/users.py',
  '                if now - s["seen"] > mins * 60]',
  '                if False]'),

 ('web: a fault before the press is known stops it', 'ltcplay/web.py',
  '        except Exception:                        # noqa: BLE001\n            return False\n        try:\n            who = self._acting_user(body)',
  '        except Exception:                        # noqa: BLE001\n            self._send(403, {"error": "x", "role_refused": True})\n            return True\n        try:\n            who = self._acting_user(body)'),

 ('web: a fault in the role check lets an arm through', 'ltcplay/web.py',
  '        except Exception as e:                   # noqa: BLE001\n            why = (f"The sign-in check could not be made',
  '        except Exception as e:                   # noqa: BLE001\n            return False\n            why = (f"The sign-in check could not be made'),

 ('web: the role matrix is never asked', 'ltcplay/web.py',
  '            if users.may(role, perm):\n                return False',
  '            if True:\n                return False'),

 ("web: the Stream Deck's presses count as nobody", 'ltcplay/web.py',
  '                str(body.get("screen") or "") == "Stream Deck":\n            users = getattr(',
  '                str(body.get("screen") or "") == "Nope":\n            users = getattr('),

 # -- signed out is view only (Jeff's field report, 2026-10-28, Tier A):
 # the rack screen on the machine itself needs a sign-in like any other
 # surface, the journal names the signed-in user and never a body's name,
 # and Sign out ends the session at the engine and lets go of a hold.
 ('users: signed out may do what a stored matrix says', 'ltcplay/users.py',
  '    if role is None or role not in ROLES:\n        return False\n    col = role',
  '    if role is not None and role not in ROLES:\n        return False\n    col = role if role is not None else SIGNED_OUT'),

 ('users: the signed-out column can be ticked', 'ltcplay/users.py',
  'FIXED.update({(SIGNED_OUT, p): False for p in PERM_KEYS})',
  'pass'),

 ("users: tonight's list needs nobody", 'ltcplay/users.py',
  '        if route == "/api/schedule/tonight":\n            return "show_files"',
  '        if False:\n            return "show_files"'),

 ('users: choosing the operator needs nobody', 'ltcplay/users.py',
  '            return "show" if str(body.get("who") or "").strip() else "safety"',
  '            return None'),

 ('users: opening the settings folder needs nobody', 'ltcplay/users.py',
  '    "checklist-tally-retry": None, "open-folder": "show_files",',
  '    "checklist-tally-retry": None, "open-folder": None,'),

 ('users: choosing the show in use needs nobody', 'ltcplay/users.py',
  '    "show-select": "show_files", "show-file-new": "show_files",',
  '    "show-select": None, "show-file-new": "show_files",'),

 ('users: a broken users file opens every control', 'ltcplay/users.py',
  '            return bool(doc.get("users")) or bool(doc.get("broken"))',
  '            return bool(doc.get("users"))'),

 ('users: a lost users file is "not set up"', 'ltcplay/users.py',
  '            if os.path.exists(self.mark):\n                # Users were set up here',
  '            if False:\n                # Users were set up here'),

 ('users: a lost users file is seeded again', 'ltcplay/users.py',
  '        if os.path.exists(self.path) or os.path.exists(self.mark):',
  '        if os.path.exists(self.path):'),

 ('users: a name with no password is claimed from the network', 'ltcplay/users.py',
  '        if first and not local:',
  '        if False:'),

 ('users: setting users up leaves no mark', 'ltcplay/users.py',
  '        if clean.get("users"):\n            self._write_mark()',
  '        pass'),

 ("users: an older install's file never gets the mark", 'ltcplay/users.py',
  '            if doc["users"] and not os.path.exists(self.mark):\n                self._write_mark()',
  '            if False:\n                self._write_mark()'),

 ('users: a failed open is remembered as broken', 'ltcplay/users.py',
  '            self.error_kind = "open"\n            return {"users": {}, "broken": True}',
  '            self.error_kind = "open"\n            self._doc, self._mtime = {"users": {}, "broken": True}, m\n            return self._doc'),

 ('users: a held file after a change brings back the copy before it', 'ltcplay/users.py',
  '        self._good = json.loads(json.dumps(clean))\n',
  ''),

 ('users: a change is made from a held copy', 'ltcplay/users.py',
  '            if self._fail_since is not None:\n                # Only ever changed from a fresh read',
  '            if False:\n                # Only ever changed from a fresh read'),

 ('users: deck can be a name', 'ltcplay/users.py',
  'RESERVED = ("unsigned", "system", "signed out", "nobody", "deck")',
  'RESERVED = ("unsigned", "system", "signed out", "nobody")'),

 ('users: a users file that is not UTF-8 reads as no users', 'ltcplay/users.py',
  '            with open(self.path, "rb") as fh:\n                raw = fh.read()',
  '            with open(self.path, encoding="utf-8-sig") as fh:\n                raw = fh.read().encode("utf-8")'),

 ('users: a failed open locks at once', 'ltcplay/users.py',
  '            if self._good is not None and \\\n                    now - self._fail_since < READ_GRACE_S:',
  '            if False:'),

 ('users: a users file that cannot be used signs everyone out', 'ltcplay/users.py',
  '            if doc.get("broken"):\n                # Locked while the file cannot be used',
  '            if False:\n                # Locked while the file cannot be used'),

 ('users: the latest sign-in here is told by the clock', 'ltcplay/users.py',
  '                        (best is None or s.get("seq", 0) > best.get("seq", 0)):',
  '                        (best is None or s["at"] > best["at"]):'),

 ('users: a sign-out on the rack screen leaves the machine signed in', 'ltcplay/users.py',
  '            if local or (s is not None and s["local"]):',
  '            if False:'),

 ('remote: with users on the journal names whoever the body says', 'ltcplay/remote.py',
  '            return self.signed_in_name(ctx, body), screen, True',
  '            return (str(body.get("who") or "") or self.signed_in_name(ctx, body)), screen, True'),

 ('remote: a name in a body reaches the route', 'ltcplay/remote.py',
  '        if "who" in body and self.users_active():',
  '        if False:'),

 ('remote: sign out leaves a hold to arm going', 'ltcplay/remote.py',
  '                    self.cancel_holds("the screen was signed out",\n                                      device=ctx.device)',
  '                    pass'),

 ("remote: a sign-out lets go of every screen's hold", 'ltcplay/remote.py',
  '                    self.cancel_holds("the screen was signed out",\n                                      device=ctx.device)',
  '                    self.cancel_holds("the screen was signed out")'),

 ('remote: a sign-out that ended nothing lets go of holds', 'ltcplay/remote.py',
  '            if ended:\n                # Only this device',
  '            if True:\n                # Only this device'),

 ('remote: a fault working out the sign-in lets the deck arm', 'ltcplay/remote.py',
  '            return {"name": None, "role": None, "active": True,\n                    "fault": True, "may": {"arm": False, "show": False}}',
  '            return None'),

 ('remote: a good users file back leaves the alert up', 'ltcplay/remote.py',
  '                    self.alerts.clear(kind="users", action="Confirm",\n                                      who="system", screen="engine")',
  '                    pass'),

 ('remote: a held users file is told to be deleted', 'ltcplay/remote.py',
  '        if getattr(u, "error_kind", "") == "open":',
  '        if False:'),

 ('remote: a broken users file raises no alert', 'ltcplay/remote.py',
  '            self.alerts.raise_("users", text)',
  '            pass'),

 ('remote: sign out on the rack screen ends only the page', 'ltcplay/remote.py',
  '            ended = u.sign_out(getattr(ctx, "token", ""), ctx.source,\n                               local=ctx.local)',
  '            ended = u.sign_out(getattr(ctx, "token", ""), ctx.source)'),

 ('remote: a screen hold names the device, not the person', 'ltcplay/remote.py',
  '        if self.users_active():\n            who = self.signed_in_name(ctx, body)\n        token = ctx.device',
  '        token = ctx.device'),

 ('remote: whoami says a signed-out rack screen may act', 'ltcplay/remote.py',
  '            out["view_only"] = bool(out["users_active"] and\n                                    not out["user"])',
  '            out["view_only"] = False'),

 ('remote: the Stream Deck is not told users are on', 'ltcplay/remote.py',
  '                    "active": bool(u.active()),',
  '                    "active": False,'),

 ('web: Stop names whoever the body says', 'ltcplay/web.py',
  '            who = self._authed_who(body)\n            if who is None:\n                who = str(body.get("who")',
  '            who = None\n            if who is None:\n                who = str(body.get("who")'),

 ('web: a deck line about a screen keeps any name', 'ltcplay/web.py',
  '                who = self._deck_screen_who(said)',
  '                who = said'),

 ("web: a deck journal line names the deck's chosen operator", 'ltcplay/web.py',
  '            body = self._authed_body(route, body)',
  '            pass'),

 ('deck: an engine not heard from lets the arm keys arm', 'ltcplay/streamdeck.py',
  '            return None if users_on is False else dict(USER_UNKNOWN)',
  '            return None'),

 ('deck: no users and a quiet engine refuse the arm keys', 'ltcplay/streamdeck.py',
  '            return None if users_on is False else dict(USER_UNKNOWN)',
  '            return dict(USER_UNKNOWN)'),

 ('deck: who is signed in not known names the chosen operator', 'ltcplay/streamdeck.py',
  '        if u is not None and (u.get("active") is True or u.get("stale")):',
  '        if u is not None and u.get("active") is True:'),

 ('deck: who is signed in not known may arm', 'ltcplay/streamdeck.py',
  'USER_UNKNOWN = {"name": None, "role": None, "active": None, "stale": True,\n                "may": {"arm": False, "show": False}}',
  'USER_UNKNOWN = {"name": None, "role": None, "active": None, "stale": False,\n                "may": {"arm": True, "show": True}}'),

 ('deck: unsigned counts as a chosen operator', 'ltcplay/streamdeck.py',
  '    if current_operator == UNSIGNED:',
  '    if False:'),

 ('deck: the journal names the chosen operator with users on', 'ltcplay/streamdeck.py',
  '        if u is not None and (u.get("active") is True or u.get("stale")):',
  '        if False:'),

 ('page: a sign-in leaves Run locked', 'ltcplay/web/remote.html',
  '      el.disabled = el.dataset.dis0 === "1";\n',
  ''),

 ('page: a Stop is locked signed out', 'ltcplay/web/remote.html',
  '  return offs.filter(el => el && el.dataset.mode !== "stop");',
  '  return offs.filter(Boolean);'),

 ("page: a sign-in leaves the sign-in tooltip", 'ltcplay/web/remote.html',
  '    el.title = el.dataset.title0 || "";\n',
  ''),

 ('page: signed out leaves Start now on', 'ltcplay/web/remote.html',
  '    offs.push($("b-start"), $("b-resume"), $("b-reset"), $("b-run"),',
  '    offs.push($("b-resume"), $("b-reset"), $("b-run"),'),

 ('page: signed out may hold to arm', 'ltcplay/web/remote.html',
  '  if(!mayRow(st, "arm")) return {ok:false, signin:true, why:SIGN_IN_WORDS};\n',
  ''),

 ("page: signed out may arm a laser", 'ltcplay/web/remote.html',
  '    offs.push(...[...document.querySelectorAll("#lz-chips button[data-lz]")].filter(b => !b.classList.contains("armed")));',
  '    offs.push(...[...document.querySelectorAll("#lz-chips button[data-lz]")].filter(b => false));'),

 ('page: a laser Disarm is locked signed out', 'ltcplay/web/remote.html',
  '    offs.push(...[...document.querySelectorAll("#lz-chips button[data-lz]")].filter(b => !b.classList.contains("armed")));',
  '    offs.push(...document.querySelectorAll("#lz-chips button[data-lz]"));'),

 ('page: the chip never says view only', 'ltcplay/web/remote.html',
  '  b.textContent = me.user ? me.user : (viewOnly ? "Sign in to use controls" : "Signed out");',
  '  b.textContent = me.user ? me.user : "Signed out";'),

 ("web: the page's open-beats are written to the audit log", 'ltcplay/web.py',
  '                                         "/api/remote/morning-open",\n                                         "/api/schedule/deck-event")):',
  '                                         "/api/schedule/deck-event")):'),

 ('audit: a secret-looking value is written as it is', 'ltcplay/audit.py',
  '    s = _SECRET_VALUE.sub("[hidden]", s)',
  '    s = s'),

 ('audit: lines are not chained', 'ltcplay/audit.py',
  '        rec["prev"] = self._prev\n',
  '        rec["prev"] = ""\n'),

 ('audit: a day does not link to the day before', 'ltcplay/audit.py',
  '                      "prev_file_end": prev_end}, t)',
  '                      "prev_file_end": ""}, t)'),

 ("audit: verify does not check a line's hash", 'ltcplay/audit.py',
  '        if r.get("hash") != record_hash(r):\n            return False, i - 1, prev, f"line {i} was changed',
  '        if False:\n            return False, i - 1, prev, f"line {i} was changed'),

 ('audit: verify does not link the days', 'ltcplay/audit.py',
  '        elif before[1] is not None and r["start_link"] != before[1]:',
  '        elif False:'),

 ('audit: lost lines leave no gap line', 'ltcplay/audit.py',
  '            if lost:\n                self._append(',
  '            if False:\n                self._append('),

 ('audit: an event waits for room on the queue', 'ltcplay/audit.py',
  '            self._q.put_nowait(rec)\n            return True',
  '            self._q.put(rec, timeout=0.002)\n            return True'),

 ("auditsend: a day is marked sent without the server's acknowledgement", 'ltcplay/auditsend.py',
  '        if not isinstance(ack, dict) or ack.get("ok") is not True or \\\n                ack.get("sha256") != sha:',
  '        if False:'),

 ("auditsend: today's open file is sent too", 'ltcplay/auditsend.py',
  '            if day < today and n not in sent:',
  '            if n not in sent:'),

 ('auditsend: the upload runs while it is off', 'ltcplay/auditsend.py',
  '            if not cfg["enabled"] or (not force and not self.due(cfg)):',
  '            if (not force and not self.due(cfg)):'),

 ('auditsend: a failure is retried at once, no backoff', 'ltcplay/auditsend.py',
  '            return self._clock() >= self._next_try',
  '            return True'),

 ('auditsend: a day not acknowledged is pruned', 'ltcplay/auditsend.py',
  '            if day < cutoff and n in sent:',
  '            if day < cutoff:'),

 ('auditsend: a plain http address is accepted', 'ltcplay/auditsend.py',
  '    if url and not url.lower().startswith("https://"):\n        raise ValueError(f"{where}: the server address',
  '    if False:\n        raise ValueError(f"{where}: the server address'),

 ('auditsend: days are kept less than 90 days', 'ltcplay/auditsend.py',
  '            not KEEP_MIN_DAYS <= keep <= 3650:',
  '            not 1 <= keep <= 3650:'),

 ('audit receiver: any token is taken', 'tools/audit_receiver.py',
  '    if not token or not hmac.compare_digest(auth.encode(), want.encode()):',
  '    if not token:'),

 ('audit receiver: a different file overwrites a stored one', 'tools/audit_receiver.py',
  '        if have == sha:',
  '        if True:'),

 ('deck: the role matrix never refuses an arm key', 'ltcplay/streamdeck.py',
  '    if may.get(action) is not False:\n        return None',
  '    if True:\n        return None'),

 ('deck: the role is not asked again when an arm hold completes', 'ltcplay/streamdeck.py',
  '        refusal = (role_gate(self._user(), "arm") if screen == "Stream Deck"\n                   else None)',
  '        refusal = None'),

 ('deck: key presses are not written to the audit log', 'ltcplay/streamdeck.py',
  '        self._audit_keys(down, latched, pending)\n',
  ''),
 # ---- the deck's arm link and key images on threads of their own --------
 # (2026-10-21, the five hour soak on the show PC: 107 "arm input stale"
 # latch-resets while USB writes to the deck stalled for seconds; and Hold
 # drawn as ENGINE FAULT while the Hold had worked.)
 ('deck usb: the arm pacer sends while the input loop is frozen',
  'ltcplay/streamdeck.py',
  '        if not self.live(now):\n            self.silent += 1\n'
  '            return False',
  '        if False:\n            self.silent += 1\n'
  '            return False'),

 ('deck usb: the pacer trusts a beat four times as old',
  'ltcplay/streamdeck.py',
  'INPUT_STALE_S = 0.2       # the input loop',
  'INPUT_STALE_S = 0.8       # the input loop'),

 ('deck usb: the threaded loop never beats the arm pacer',
  'ltcplay/streamdeck.py',
  '                    # is alive (ArmPacer). A frozen loop stops the beat.\n'
  '                    pacer.beat()\n',
  '                    # is alive (ArmPacer). A frozen loop stops the beat.\n'
  '                    pass\n'),

 ('deck usb: ltc deck runs the single loop again',
  'ltcplay/streamdeck.py',
  '        run_forever(controller, journal=journal, threaded=True)',
  '        run_forever(controller, journal=journal)'),

 ('deck usb: the key images are written on the input loop again',
  'ltcplay/streamdeck.py',
  '    if display is None:\n        _draw_deck(controller, deck, fonts, motion)\n'
  '        return',
  '    if True:\n        _draw_deck(controller, deck, fonts, motion)\n'
  '        return'),

 ('deck usb: the oldest waiting frame wins, not the newest',
  'ltcplay/streamdeck.py',
  '            if self._pending is not None:\n                self.dropped += 1\n'
  '            self._pending = (frame_id, img)',
  '            if self._pending is not None:\n                self.dropped += 1\n'
  '                return\n            self._pending = (frame_id, img)'),

 ('deck usb: a key image write that failed is never raised',
  'ltcplay/streamdeck.py',
  '        e = self.error\n        if e is not None:\n            raise e',
  '        e = self.error\n        if False:\n            raise e'),

 ('deck usb: retiring the display waits for a stalled write',
  'ltcplay/streamdeck.py',
  '            self._retire = True\n            self._pending = None\n'
  '            self._cv.notify()\n'
  '        if self._thread is None:\n            _close_quietly(self.deck)',
  '            self._retire = True\n            self._pending = None\n'
  '            self._cv.notify()\n'
  '        if self._thread is not None:\n            self._thread.join(5)\n'
  '        if self._thread is None:\n            _close_quietly(self.deck)'),

 ('deck usb: the arm socket sends without its lock',
  'ltcplay/streamdeck.py',
  '        with self.lock:\n            note = self._send_locked(names)',
  '        note = self._send_locked(names)'),

 ('deck usb: the arm socket journals a failed send under its lock',
  'ltcplay/streamdeck.py',
  '        with self.lock:\n            note = self._send_locked(names)\n',
  '        with self.lock:\n            note = self._send_locked(names)\n'
  '            if note is not None:\n'
  '                self._journal(note[0], **note[1])\n'
  '                note = None\n'),

 ('deck stall: a stuck key image write retires the deck again',
  'ltcplay/streamdeck.py',
  '                    # never an unplug: DECK_STALL_RULING.\n'
  '                    meter.check(clock())\n',
  '                    # never an unplug: DECK_STALL_RULING.\n'
  '                    meter.check(clock())\n'
  '                    if meter.worst_s(clock()) > 1.0:\n'
  '                        raise DeckDisconnected("a write is stuck")\n'),

 ('deck stall: a stuck key image write drops an arm hold again',
  'ltcplay/streamdeck.py',
  '        for i in range(len(self.names)):\n'
  '            if self._key_down(i):\n'
  '                if self._arm_holds[i].fired(now):',
  '        for i in range(len(self.names)):\n'
  '            if self.usb is not None and self.usb.worst_s(now) > 0.25:\n'
  '                self._arm_holds[i].release()\n'
  '            if self._key_down(i):\n'
  '                if self._arm_holds[i].fired(now):'),

 ('deck stall: a stuck key image write releases a held FIRE again',
  'ltcplay/streamdeck.py',
  '        for i in range(len(self.names)):\n'
  '            if self._key_down(i):\n'
  '                if self._arm_holds[i].fired(now):',
  '        if self.usb is not None and self.usb.worst_s(now) > 0.25:\n'
  '            self._fire_release("a key image write is stuck")\n'
  '        for i in range(len(self.names)):\n'
  '            if self._key_down(i):\n'
  '                if self._arm_holds[i].fired(now):'),

 ('deck stall: FIRE heartbeats ignore a frozen input loop',
  'ltcplay/streamdeck.py',
  '            if not fresh and not self._loop_alive():',
  '            if False:'),

 ('deck stall: a FIRE liveness check that raises counts as alive',
  'ltcplay/streamdeck.py',
  '            return bool(f())\n        except Exception:                        # noqa: BLE001\n'
  '            return False',
  '            return bool(f())\n        except Exception:                        # noqa: BLE001\n'
  '            return True'),

 ('deck stall: the threaded loop never gives FIRE its liveness',
  'ltcplay/streamdeck.py',
  '            pf.alive = (lambda p=pacer: p.live(',
  '            _unused_alive = (lambda p=pacer: p.live('),

 ('deck stall: FIRE keeps beating for 5 s of a frozen input loop',
  'ltcplay/streamdeck.py',
  'PURGE_INPUT_STALE_S = 1.0  # a held FIRE',
  'PURGE_INPUT_STALE_S = 5.0  # a held FIRE'),

 ('deck stall: FIRE stops on the arm link\'s 0.2 s stale rule',
  'ltcplay/streamdeck.py',
  '            pf.alive = (lambda p=pacer: p.live(\n'
  '                stale_s=PURGE_INPUT_STALE_S))',
  '            pf.alive = (lambda p=pacer: p.live(\n'
  '                stale_s=None))'),

 ('deck stall: a key read that fails is no longer an unplug',
  'ltcplay/streamdeck.py',
  '                for down in deck.keys_down():\n'
  '                    controller.run_once(down)\n'
  '                    moved = True',
  '                try:\n'
  '                    _snaps = deck.keys_down()\n'
  '                except Exception:\n'
  '                    _snaps = []\n'
  '                for down in _snaps:\n'
  '                    controller.run_once(down)\n'
  '                    moved = True'),

 ('deck usb: a key write the deck did not take is cached as written',
  'ltcplay/streamdeck.py',
  '                if isinstance(n, int) and not isinstance(n, bool) and n <= 0:\n'
  '                    return False',
  '                if False:\n'
  '                    return False'),

 ('deck usb: a frame with a key the deck did not take counts as shown',
  'ltcplay/streamdeck.py',
  '        if unwritten:\n            # Review of PR #83, P2-2',
  '        if False:\n            # Review of PR #83, P2-2'),

 ('deck usb: a frame the deck did not take is never written again',
  'ltcplay/streamdeck.py',
  '                if self._pending is None:\n'
  '                    self._pending = item',
  '                if self._pending is None:\n'
  '                    pass'),

 ('deck usb: failed key writes never ride to the engine',
  'ltcplay/streamdeck.py',
  '                path += f"&usb_fail={n}"',
  '                path += ""'),

 ('deck usb: the Deck chip never shows failed key writes',
  'ltcplay/remote.py',
  '        elif self._deck_usb_fail:',
  '        elif False:'),

 ('deck usb: failed key writes are never journaled',
  'ltcplay/streamdeck.py',
  '            if fails and not self._fail_said:',
  '            if False:'),

 ('deck fire: stop() waits on the heartbeat POST again',
  'ltcplay/streamdeck.py',
  '        if post_here:\n'
  '            threading.Thread(target=self._post_stop',
  '        t_ = self._thread\n'
  '        if t_ is not None and t_ is not threading.current_thread():\n'
  '            t_.join(timeout=1.0)\n'
  '        if post_here:\n'
  '            threading.Thread(target=self._post_stop'),

 ('deck fire: a press still running never posts its own purge-stop',
  'ltcplay/streamdeck.py',
  '                    else:\n'
  '                        press.stop_who = who\n'
  '        if post_here:',
  '                    else:\n'
  '                        pass\n'
  '        if post_here:'),

 ('deck fire: purge-stop goes out before the beat in flight',
  'ltcplay/streamdeck.py',
  '                    if press.exited:\n'
  '                        post_here = True',
  '                    if True:\n'
  '                        post_here = True'),

 ('deck fire: a new press does not wait for the old press to finish',
  'ltcplay/streamdeck.py',
  '            prev.join(timeout=2.0)',
  '            pass'),

 ('deck fire: the press itself is not counted as a beat',
  'ltcplay/streamdeck.py',
  '            fresh = time.monotonic() - press.started <= PURGE_INPUT_STALE_S',
  '            fresh = False'),

 ('deck usb: a slow USB write is never journaled',
  'ltcplay/streamdeck.py',
  '            if worst >= USB_SLOW_S:\n'
  '                self._episode_worst = max(self._episode_worst, worst)',
  '            if False:\n'
  '                self._episode_worst = max(self._episode_worst, worst)'),

 ('deck usb: a write still in progress does not count',
  'ltcplay/streamdeck.py',
  '            if self._inflight_since is not None:\n'
  '                worst = max(worst, now - self._inflight_since)',
  '            if False:\n'
  '                worst = max(worst, now - self._inflight_since)'),

 ('deck usb: the deck never carries its USB worst to the engine',
  'ltcplay/streamdeck.py',
  '            path += f"?usb_ms={ms}"',
  '            pass'),

 ('deck usb: the rack screen chip never turns amber for slow USB',
  'ltcplay/remote.py',
  '        elif usb >= DECK_USB_SLOW_MS:',
  '        elif False:'),

 ('deck usb: a quiet deck reads ok on its last USB word',
  'ltcplay/remote.py',
  '        if age > self.DECK_QUIET_S:\n'
  '            return "bad", "not heard"\n'
  '        hw = self._deck_hw',
  '        if False:\n'
  '            return "bad", "not heard"\n'
  '        hw = self._deck_hw'),

 ('deck lamp: an unplugged Stream Deck reads green while its program runs',
  'ltcplay/remote.py',
  '        if hw is False:\n'
  '            return "bad", "missing"',
  '        if False:\n'
  '            return "bad", "missing"'),

 ('deck lamp: an older deck that says nothing of its USB reads green',
  'ltcplay/remote.py',
  '        if hw is not True:\n'
  '            # An older deck program that does not say: never green.\n'
  '            return "warn", "USB not known"',
  '        if False:\n'
  '            return "warn", "USB not known"'),

 ('deck lamp: a stale arm link reads green',
  'ltcplay/remote.py',
  '        if arm is None or arm > DECK_ARM_FRESH_MS:',
  '        if False:'),

 ('deck lamp: a late deck program reads green',
  'ltcplay/remote.py',
  '        if age > PLAIN_DECK_FRESH_S:\n'
  '            return "warn", f"late {age:.1f} s"',
  '        if False:\n'
  '            return "warn", f"late {age:.1f} s"'),

 ('deck lamp: a deck never heard on a flame rig is grey, not red',
  'ltcplay/remote.py',
  '            return ("bad" if expected else "off"), "not heard"',
  '            return "off", "not heard"'),

 ('deck lamp: the deck stops saying its USB deck is gone',
  'ltcplay/streamdeck.py',
  '            path += "hw=1" if present else "hw=0"',
  '            path += "hw=1"'),

 ('deck lamp: an unplug leaves the deck saying it is present',
  'ltcplay/streamdeck.py',
  '            controller.deck_present = False  # the Deck chip (remote.py)',
  '            pass'),

 ('deck lamp: the arm link age never rides to the engine',
  'ltcplay/streamdeck.py',
  '            path += f"arm_ms={age}"',
  '            path += ""'),

 ('pixels lamp: a sender that stopped without an error reads sending',
  'ltcplay/remote.py',
  '        elif since > 2.0:',
  '        elif since > 2.0 and (cs.get("send_errors") or 0):'),

 ('pixels lamp: no word on the sends reads sending',
  'ltcplay/remote.py',
  '        elif not isinstance(since, (int, float)) or isinstance(since, bool):',
  '        elif False:'),

 ('deck lamp: a deck that sends no arm frames is only amber',
  'ltcplay/remote.py',
  '            return "bad", "not arming"',
  '            return "warn", "not arming"'),

 ('rack screen: a status the page cannot draw is still called fresh',
  'ltcplay/web/remote.html',
  '        LAST_OK = before;',
  '        ;'),

 ('rack screen: a stale page reads the status before greying its chips',
  'ltcplay/web/remote.html',
  '  if(stale){\n'
  '    // The top bar\'s chips first',
  '  if(stale && false){\n'
  '    // The top bar\'s chips first'),

 ('deck lamp: a page that lost the engine keeps its chips green',
  'ltcplay/web/remote.html',
  '    document.querySelectorAll(".chip").forEach(el => { el.className = "chip warn"; const w = el.querySelector("span"); if(w) w.textContent = "no contact"; });',
  '    document.querySelectorAll(".chip").forEach(el => { });'),

 ('deck hold: a Hold the engine found already done is ENGINE FAULT',
  'ltcplay/streamdeck.py',
  '        noop = (not ok and name in ("hold", "resume") and\n'
  '                doc.get("nothing_to_do") is True)',
  '        noop = False'),

 ('deck hold: every refused Hold or Resume is taken as nothing to do',
  'ltcplay/streamdeck.py',
  '        noop = (not ok and name in ("hold", "resume") and\n'
  '                doc.get("nothing_to_do") is True)',
  '        noop = (not ok and name in ("hold", "resume"))'),

 ('deck hold: the engine never says a Hold found nothing to do',
  'ltcplay/schedule_service.py',
  '                    "nothing_to_do": bool(done)}',
  '                    "nothing_to_do": False}'),

 ('deck hold: the engine says any refused Hold found nothing to do',
  'ltcplay/schedule_service.py',
  '            done = ((kind == sch.HOLD_ON and before in self.HOLD_ALREADY) or\n'
  '                    (kind == sch.RESUME and before in self.RESUME_ALREADY))',
  '            done = kind in (sch.HOLD_ON, sch.RESUME)'),

 ('deck hold: the Hold key ignores the scheduler on hold',
  'ltcplay/streamdeck.py',
  '            if self.held_provider() is True:\n                return True',
  '            if False:\n                return True'),

 ('deck hold: LocalSchedule never reads the scheduler on hold',
  'ltcplay/streamdeck.py',
  '                self._held = running.get("state") in ("HOLD", "PAUSED")',
  '                self._held = False'),

 ('deck hold: ltc deck never gives the Hold key the scheduler',
  'ltcplay/streamdeck.py',
  '                            held_provider=sched.held,',
  '                            held_provider=None,'),

 # -- arming from the rack screen (Jeff, 2026-10-22): off said and switchable
 # on the show machine, every refusal named, the deck taking it a success --
 ('screen arm: the switch takes a word that is not true or false',
  'ltcplay/remote.py',
  '        on = body.get("on")\n        if on is not True and on is not False:',
  '        on = body.get("on")\n        if on is None:'),

 ('screen arm: the switch answers from the network',
  'ltcplay/remote.py',
  'LOCAL_ONLY = frozenset(("network", "screen-arming", "announce-output",',
  'LOCAL_ONLY = frozenset(("network", "announce-output",'),

 ('screen arm: switching it off keeps the holds',
  'ltcplay/remote.py',
  '            self.cancel_holds("arming from a screen was switched off")',
  '            pass'),

 ('screen arm: turning it on needs no role',
  'ltcplay/users.py',
  '    "network": "settings", "screen-arming": "settings",',
  '    "network": "settings", "screen-arming": None,'),

 ('screen arm: turning it off waits on the role matrix',
  'ltcplay/users.py',
  '        if name == "screen-arming" and body.get("on") is False:\n'
  '            # Switching arming from a screen OFF only takes risk away.\n'
  '            return "safety"',
  '        if False:\n            return "safety"'),

 ('screen arm: an Abort standing is not said',
  'ltcplay/remote.py',
  '        if self._abort_standing():\n            return ("An Abort is standing',
  '        if False:\n            return ("An Abort is standing'),

 ('screen arm: no Stream Deck reading the engine is not said',
  'ltcplay/remote.py',
  '        if seen is None or self.clock() - seen > self.DECK_QUIET_S:\n'
  '            return (f"The Stream Deck program has not read',
  '        if seen is None:\n            return (f"The Stream Deck program has not read'),

 ('screen arm: this screen arranging still arms',
  'ltcplay/remote.py',
  '            if arranging:\n                return (f"{device} is in Arrange mode',
  '            if False:\n                return (f"{device} is in Arrange mode'),

 ('screen arm: any screen arranging blocks arming',
  'ltcplay/remote.py',
  '                arranging = m.device_arranging(device)',
  '                arranging = bool(m.arranging_devices())'),

 ('screen arm: a locked out group is not said',
  'ltcplay/remote.py',
  '            if locked:\n                return (f"Every machine in {gname}',
  '            if False:\n                return (f"Every machine in {gname}'),

 ('screen arm: one locked machine reads as the whole group locked',
  'ltcplay/morning.py',
  '                return (bool(fire) and fire <= locked) or \\\n'
  '                    (len(saf) > 1 and saf <= locked)',
  '                return bool(fire & locked)'),

 ('screen arm: the deck\'s refusal is never said',
  'ltcplay/remote.py',
  '            if code is not None and code != "taken":\n                # The Stream Deck would not take this hold',
  '            if False:\n                # The Stream Deck would not take this hold'),

 ('screen arm: deck_refused takes any code',
  'ltcplay/remote.py',
  '            if code in DECK_REFUSALS and hid.isdigit():',
  '            if hid.isdigit():'),

 ('screen arm: the deck taking the hold reads as already armed',
  'ltcplay/remote.py',
  '            if body.get("hold_id") is not None:\n'
  '                # This device\'s own hold, and the group is now asked for.',
  '            if False:\n'
  '                # This device\'s own hold, and the group is now asked for.'),

 ('screen arm: another device\'s hold counts as this one\'s success',
  'ltcplay/remote.py',
  '                    mine = (h is not None and h["token"] == token and\n'
  '                            h["id"] == body.get("hold_id"))',
  '                    mine = (h is not None and\n'
  '                            h["id"] == body.get("hold_id"))'),

 ('screen arm: the deck never reports its re-arm wait',
  'ltcplay/streamdeck.py',
  '                self._report_refused(h.get("id"), "refractory")',
  '                pass'),

 ('screen arm: the deck never reports no operator',
  'ltcplay/streamdeck.py',
  '                self._report_refused(h.get("id"), "operator")',
  '                pass'),

 ('screen arm: the deck never reports its own key down',
  'ltcplay/streamdeck.py',
  '                    self._report_refused(h.get("id"), "key")',
  '                    pass'),

 ('screen arm: the deck never reports its latch',
  'ltcplay/streamdeck.py',
  '                    self._report_refused(h.get("id"), "latched")',
  '                    pass'),

 ('screen arm: the deck never reports a refusal at completion',
  'ltcplay/streamdeck.py',
  '                    if why:\n                        self._report_refused(self._vholds.get(i), why)',
  '                    if False:\n                        self._report_refused(self._vholds.get(i), why)'),

 ('screen arm: a completed hold the deck refused reports no reason',
  'ltcplay/streamdeck.py',
  '                      f"hold); refused. Reset, then hold the key again.",\n'
  '                      action="arm-refused")\n            return "latched"',
  '                      f"hold); refused. Reset, then hold the key again.",\n'
  '                      action="arm-refused")\n            return None'),

 ('screen arm: the deck\'s report never reaches the engine',
  'ltcplay/streamdeck.py',
  '        if ref:\n            path += ("&" if "?" in path else "?") + f"refused={ref}"',
  '        if False:\n            path += ("&" if "?" in path else "?") + f"refused={ref}"'),

 ('screen arm: the deck reports a refusal forever',
  'ltcplay/streamdeck.py',
  '                      if now - t > SCREEN_REFUSED_KEEP_S]',
  '                      if False]'),

 ('screen arm: the page hides the button while arming is off',
  'ltcplay/web/remote.html',
  '    ab.hidden = false;\n',
  '    ab.hidden = !on;\n'),

 ('screen arm: the page says arming off nowhere',
  'ltcplay/web/remote.html',
  '  if(!on && !HOLD && $("arm-note").textContent !== ARMING_OFF) armSay(ARMING_OFF, 1);',
  '  if(false) armSay(ARMING_OFF, 1);'),

 ('screen arm: a refusal is said only where a short window hides it',
  'ltcplay/web/remote.html',
  '  if(level >= 2){ $("ctl-msg").textContent = text;',
  '  if(false){ $("ctl-msg").textContent = text;'),

 ('screen arm: the page reads the deck taking the hold as nothing',
  'ltcplay/web/remote.html',
  '    if(j.armed){',
  '    if(false){'),

 ('screen arm: the hold is credited without the deck\'s word',
  'ltcplay/remote.py',
  '                    took = mine and \\\n'
  '                        self._deck_refused.get(h["id"]) == "taken"',
  '                    took = mine'),

 ('screen arm: the deck never says it took the hold',
  'ltcplay/streamdeck.py',
  '                    else:\n'
  '                        self._report_refused(self._vholds.get(i), "taken")',
  '                    else:\n                        pass'),

 ('screen arm: the engine never reads the deck\'s taken',
  'ltcplay/remote.py',
  '            for hid in taken:',
  '            for hid in []:'),

 ('screen arm: a hold asked for then disarmed carries on',
  'ltcplay/remote.py',
  '                        now - h["beat"] <= BEAT_STALE_S and\n'
  '                        "wanted_at" not in h)',
  '                        now - h["beat"] <= BEAT_STALE_S)'),

 ('screen arm: Arrange mode unreadable lets the hold go on',
  'ltcplay/remote.py',
  '            except Exception as e:                   # noqa: BLE001\n'
  '                return (f"Whether {device} is in Arrange mode',
  '            except Exception as e:                   # noqa: BLE001\n'
  '                arranging = False\n'
  '                (f"Whether {device} is in Arrange mode'),

 ('screen arm: save_settings overwrites a file it cannot parse',
  'ltcplay/remote.py',
  '        except (OSError, ValueError) as e:\n'
  '            raise ValueError(f"{path} could not be read ({e}), so it was "',
  '        except (OSError, ValueError) as e:\n'
  '            doc = {}\n'
  '            str(f"{path} could not be read ({e}), so it was "'),

 ('screen arm: save_settings writes before it checks',
  'ltcplay/remote.py',
  '    settings_from_doc(doc, path)            # ValueError: nothing written',
  '    pass'),

 ('screen arm: save_settings keeps no .bak',
  'ltcplay/remote.py',
  '        shutil.copyfile(path, path + ".bak")',
  '        pass'),

 ('screen arm: the switch journals a name from the body',
  'ltcplay/remote.py',
  '        who, screen = (ctx.user or "unsigned"), ctx.screen',
  '        who, screen, _s = self._actor(ctx, body)'),

 ('screen arm: switching off leaves a network purge selection',
  'ltcplay/remote.py',
  '                    pg.clear("arming from a screen was switched off")',
  '                    pass'),

 ('screen arm: the confirm box says less than the switch does',
  'ltcplay/web/remote.html',
  'confirmBox("Turn on arming from a screen?", SCREEN_ARMING_ENABLES,',
  'confirmBox("Turn on arming from a screen?", "Hold to arm.",'),

 ('screen arm: the page says nothing when arming is off',
  'ltcplay/web/remote.html',
  '  if(!a.enabled) return {ok:false, off:true, why:ARMING_OFF};',
  '  if(!a.enabled) return {ok:false, off:true, why:""};'),

 # -- the laser arm (Jeff's rulings, 2026-10-22): BEYOND's laser output
 # switch, arm and disarm by laser and by group, Abort and Hold disarm --
 ('lasers: EnableLaserOutput or DisableLaserOutput is allowed with an argument',
  'ltcplay/beyond.py',
  '        return value is None\n'
  '    if any(c in _SPECIAL_CHARS for c in address):',
  '        return True\n'
  '    if any(c in _SPECIAL_CHARS for c in address):'),
 ('lasers: the allow-list admits an unverified per-zone enable address',
  'ltcplay/beyond.py',
  'OUTPUT_ADDRESSES = frozenset((ENABLE_OUTPUT_ADDR, DISABLE_OUTPUT_ADDR))',
  'OUTPUT_ADDRESSES = frozenset((ENABLE_OUTPUT_ADDR, DISABLE_OUTPUT_ADDR,\n'
  '                              "/beyond/zones/1/enable"))'),
 ('lasers: EnableLaserOutput is sent under a wrong-case address',
  'ltcplay/beyond.py',
  'ENABLE_OUTPUT_ADDR = "/beyond/general/EnableLaserOutput"',
  'ENABLE_OUTPUT_ADDR = "/beyond/general/enableLaserOutput"'),
 ('lasers: DisableLaserOutput is sent under a wrong address',
  'ltcplay/beyond.py',
  'DISABLE_OUTPUT_ADDR = "/beyond/general/DisableLaserOutput"',
  'DISABLE_OUTPUT_ADDR = "/beyond/general/DisableLaserOutput/"'),
 ('lasers: a no-argument message goes out with a float type tag',
  'ltcplay/beyond.py',
  '    return _pad(address.encode("utf-8")) + _pad(b",")',
  '    return _pad(address.encode("utf-8")) + _pad(b",f")'),
 ("lasers: enable_output's armed type check is removed",
  'ltcplay/beyond.py',
  '        if not isinstance(armed, bool):',
  '        if False:'),
 ('lasers: enable_output ignores armed=False',
  'ltcplay/beyond.py',
  '        if armed is not True:',
  '        if False:'),
 ('lasers: a disable no longer cuts an enable under way',
  'ltcplay/beyond.py',
  '            self._disable_epoch += 1',
  '            pass'),
 ('lasers: an enable never asks still_wanted before a packet',
  'ltcplay/beyond.py',
  '                if self._disable_epoch != epoch or \\\n'
  '                        not self._wanted(still_wanted):\n'
  '                    cut = True',
  '                if self._disable_epoch != epoch:\n'
  '                    cut = True'),
 ("lasers: a late disable is not sent again after the enable's packet",
  'ltcplay/beyond.py',
  '                self._send_output_retried(DISABLE_OUTPUT_ADDR)\n'
  '                cut = True',
  '                cut = True'),
 ('lasers: DisableLaserOutput goes once, not 3 times',
  'ltcplay/beyond.py',
  '    def _send_output_retried(self, address):\n'
  '        ok = False\n'
  '        for i in range(RETRY_COUNT):',
  '    def _send_output_retried(self, address):\n'
  '        ok = False\n'
  '        for i in range(1):'),
 ('lasers: a disable that did not get out is reported as done',
  'ltcplay/beyond.py',
  '        self.output_command = "disable"\n'
  '        self.output_result = "ok" if ok else "failed"',
  '        self.output_command = "disable"\n'
  '        self.output_result = "ok"\n'
  '        ok = True'),
 ("lasers: Blanking's unblank ignores the laser arm",
  'ltcplay/beyondtc.py',
  '        if self.arm_check is not None:\n'
  '            # The laser arm (ruling 6)',
  '        if False:\n'
  '            # The laser arm (ruling 6)'),
 ('lasers: an arm check that raises lets the lasers light',
  'ltcplay/beyondtc.py',
  '            return (f"the laser arm could not be read ({type(e).__name__}: "\n'
  '                    f"{e})")',
  '            return None'),
 ('lasers: an arm check answering a non-None sentence-less value is a yes',
  'ltcplay/beyondtc.py',
  '        return str(why) or "the lasers are not armed"',
  '        return str(why) or None'),
 ("lasers: the unblank's packets no longer re-ask the arm",
  'ltcplay/beyondtc.py',
  '                return ((_inner is None or _inner() is True) and\n'
  '                        self._arm_refusal() is None)',
  '                return (_inner is None or _inner() is True)'),
 ('lasers: EnableLaserOutput is not sent before the unblank',
  'ltcplay/beyondtc.py',
  '            if not self.output_enable(show, still_wanted=still_wanted):',
  '            if False:'),
 ('lasers: the output switch passes an OSC target with no host',
  'ltcplay/beyondtc.py',
  '        if cfg is None or not getattr(cfg, "host", None) or \\\n'
  '                not getattr(cfg, "port", None):\n'
  '            return NOT_SET_UP\n'
  '        return None',
  '        if cfg is None or not getattr(cfg, "port", None):\n'
  '            return NOT_SET_UP\n'
  '        return None'),
 ('lasers: a laser test ON ignores the arm',
  'ltcplay/lasertest.py',
  '            if not self.is_armed(laser.id):\n'
  '                self.last_result[laser.id] = "refused"\n'
  '                self._note(f"{label} asked',
  '            if False:\n'
  '                self.last_result[laser.id] = "refused"\n'
  '                self._note(f"{label} asked'),
 ('lasers: a truthy answer from the arm counts as armed',
  'ltcplay/lasertest.py',
  '            return fn(laser_id) is True',
  '            return bool(fn(laser_id))'),
 ('lasers: with no arm wired every laser counts as armed',
  'ltcplay/lasertest.py',
  '        fn = self._armed\n'
  '        if fn is None:\n'
  '            return False',
  '        fn = self._armed\n'
  '        if fn is None:\n'
  '            return True'),
 ('lasers: a per-laser arm placeholder goes to a locked or disarmed laser',
  'ltcplay/lasertest.py',
  '            if locked or not self.is_armed(laser.id):\n'
  '                return False',
  '            if False:\n'
  '                return False'),
 ('lasers: an empty per-laser placeholder still sends something',
  'ltcplay/lasertest.py',
  '        if not msgs or laser.mechanism == "none":\n'
  '            return True',
  '        if not msgs and False:\n'
  '            return True'),
 ('lasers: the engine starts without telling BEYOND every laser is disarmed',
  'ltcplay/laserarm.py',
  '        self._queue("dark", "the engine started: every laser starts "\n'
  '                            "disarmed", "", "engine")',
  '        pass'),
 ('lasers: arming needs no confirm',
  'ltcplay/laserarm.py',
  '        if confirmed is not True:',
  '        if False:'),
 ('lasers: a locked-out laser can be armed',
  'ltcplay/laserarm.py',
  '        locked = [self.names[lid] for lid in ids if self._is_locked(lid)]\n'
  '        if locked:',
  '        locked = []\n'
  '        if locked:'),
 ('lasers: arming is allowed with blanking not set up',
  'ltcplay/laserarm.py',
  '        why = self.output_fault()\n'
  '        if why is not None:\n'
  '            return (f"{why}: BEYOND\'s laser output cannot be switched from "',
  '        why = None\n'
  '        if why is not None:\n'
  '            return (f"{why}: BEYOND\'s laser output cannot be switched from "'),
 ('lasers: arming ignores the Abort latch, a Hold and the night closing',
  'ltcplay/laserarm.py',
  '        why = self._blocked()\n'
  '        if why:\n'
  '            return why\n'
  '        return None',
  '        return None'),
 ('lasers: EnableLaserOutput is allowed with only some lasers armed',
  'ltcplay/laserarm.py',
  '        if missing:\n'
  '            return (f"not every laser is armed',
  '        if False:\n'
  '            return (f"not every laser is armed'),
 ('lasers: a laser locked out under an arm still lets the output on',
  'ltcplay/laserarm.py',
  '        if locked:\n'
  '            return f"{\', \'.join(locked)} locked out"',
  '        if False:\n'
  '            return f"{\', \'.join(locked)} locked out"'),
 ('lasers: the output may come on while blocked (latched, held, closing)',
  'ltcplay/laserarm.py',
  '        blocked = self._blocked()\n'
  '        if blocked:\n'
  '            return blocked',
  '        blocked = self._blocked()\n'
  '        if False:\n'
  '            return blocked'),
 ('lasers: the output may come on with blanking not set up',
  'ltcplay/laserarm.py',
  '            return "no lasers are configured"\n'
  '        why = self.output_fault()\n'
  '        if why is not None:\n'
  '            return why',
  '            return "no lasers are configured"'),
 ('lasers: a show state that cannot be read counts as not blocked',
  'ltcplay/laserarm.py',
  '            return f"the show state could not be read ({err})"',
  '            return ""'),
 ('lasers: a lock that cannot be read counts as unlocked',
  'ltcplay/laserarm.py',
  '        return err is not None or v is not False',
  '        return v is True'),
 ('lasers: an enable queued before a disarm stays queued',
  'ltcplay/laserarm.py',
  '                self._jobs = collections.deque(\n'
  '                    j for j in self._jobs if j["kind"] == "dark")',
  '                pass'),
 ('lasers: an enable is not asked again when it is sent',
  'ltcplay/laserarm.py',
  '        why = self.may_enable()\n'
  '        if why is not None:\n'
  '            self._note(f"BEYOND\'s laser output stays off: {why}.",',
  '        why = None\n'
  '        if why is not None:\n'
  '            self._note(f"BEYOND\'s laser output stays off: {why}.",'),
 ('lasers: a disarm during the enable does not cut it',
  'ltcplay/laserarm.py',
  '        def still():\n'
  '            with self._cv:',
  '        def still():\n'
  '            return True\n'
  '            with self._cv:'),
 ('lasers: a disarm sends no DisableLaserOutput',
  'ltcplay/laserarm.py',
  '                ok, err = _ask(out.output_disable)',
  '                ok, err = True, None'),
 ("lasers: an unreachable BEYOND's disable is not retried",
  'ltcplay/laserarm.py',
  '            for i in range(DARK_RETRIES):',
  '            for i in range(1):'),
 ('lasers: a disarm does not blank',
  'ltcplay/laserarm.py',
  '        blanked = self._blank(why)',
  '        blanked = True'),
 ("lasers: a disarm does not end that laser's calibration test",
  'ltcplay/laserarm.py',
  '                    if lid in getattr(pl, "on", {}):\n'
  '                        pl.set(lid, False, who=f"the engine ({why})")',
  '                    pass'),
 ('lasers: a disarm leaves the laser armed',
  'ltcplay/laserarm.py',
  '            for lid in ids:\n'
  '                self.armed.pop(lid, None)',
  '            pass'),
 ('lasers: the night closing does not disarm',
  'ltcplay/laserarm.py',
  '            self.disarm_all("the night is closing" if closing else\n',
  '            pass\n'
  '            False and self.disarm_all("the night is closing" if closing else\n'),
 ('lasers: arming every laser never tells the conductor',
  'ltcplay/laserarm.py',
  '                    self.on_permitted(who, screen)',
  '                    pass'),
 ('lasers: arming every laser never queues EnableLaserOutput',
  'ltcplay/laserarm.py',
  '            self._queue("enable", f"every laser armed, the last by {label}",\n'
  '                        who, screen)',
  '            pass'),
 ('lasers: closing the engine does not disarm',
  'ltcplay/laserarm.py',
  '        self.disarm_all("the engine is stopping", "", "engine", defer=False)',
  '        pass'),
 ('lasers: arming is all-or-some instead of all-or-none',
  'ltcplay/laserarm.py',
  '        why = self.refusal(ids)\n'
  '        if why is not None:\n'
  '            names = ", ".join(self.names.get(i, i) for i in ids) or "nothing"',
  '        why = self.refusal(ids[:1])\n'
  '        if why is not None:\n'
  '            names = ", ".join(self.names.get(i, i) for i in ids) or "nothing"'),
 ('lasers: Hold no longer disarms the lasers',
  'ltcplay/conductor.py',
  '        self._disarm_lasers("Hold", who, screen)',
  '        pass'),
 ('lasers: Abort no longer disarms the lasers',
  'ltcplay/conductor.py',
  '                self._disarm_lasers("Abort", who, screen)',
  '                pass'),
 ('lasers: Abort disarms the lasers only while a show plays',
  'ltcplay/conductor.py',
  '                self._disarm_lasers("Abort", who, screen)',
  '                if self._playing():\n'
  '                    self._disarm_lasers("Abort", who, screen)'),
 ('lasers: a laser disarm hook that raises stops the Abort',
  'ltcplay/conductor.py',
  '        try:\n'
  '            fn(f"{what} pressed by {self._who(who, screen)}", who, screen)\n'
  '        except Exception as e:',
  '        fn(f"{what} pressed by {self._who(who, screen)}", who, screen)\n'
  '        if False:\n'
  '            e = None'),
 ('lasers: re-arming lights a show whose flame cues are not released',
  'ltcplay/conductor.py',
  '            if self._look != PLAYING or self._done_gen != self._gen or \\\n'
  '                    self._applied["flames"] != LIVE:',
  '            if self._look != PLAYING or self._done_gen != self._gen:'),
 ('lasers: re-arming starts a new sequence over one still running',
  'ltcplay/conductor.py',
  '            if self._look != PLAYING or self._done_gen != self._gen or \\\n'
  '                    self._applied["flames"] != LIVE:',
  '            if self._look != PLAYING or \\\n'
  '                    self._applied["flames"] != LIVE:'),
 ('lasers: re-arming runs the whole up sequence, not only the lasers',
  'ltcplay/conductor.py',
  '        if want.get("lasers_only"):\n'
  '            # lasers_armed(): the show is up already; only the lasers.\n'
  '            self._restore_lasers(gen, progress)\n'
  '            return\n',
  ''),
 ('lasers: re-arming ignores the Abort latch',
  'ltcplay/conductor.py',
  '            refused = self._refuse_if_latched("Lighting the armed lasers")\n'
  '            if refused:\n'
  '                return refused',
  '            pass'),
 ("lasers: the conductor's laser gate ignores the arm",
  'ltcplay/fire_ice.py',
  '        why = arm_gate()\n'
  '        if why is not None:\n'
  '            return f"the lasers are not armed: {why}. They stay dark."',
  '        pass'),
 ("lasers: the Blanking's arm_check is never set",
  'ltcplay/fire_ice.py',
  '    blanking.arm_check = arm_gate',
  '    pass'),
 ('lasers: with no laser arm attached the lasers may light',
  'ltcplay/fire_ice.py',
  '        if arm is None:\n'
  '            return ("laser arming is not set up in this engine (it lives on "',
  '        if arm is None:\n'
  '            return None\n'
  '            return ("laser arming is not set up in this engine (it lives on "'),
 ('lasers: Abort and Hold never reach the laser arm',
  'ltcplay/fire_ice.py',
  '            arm.disarm_all(reason, who, screen, defer=True)',
  '            pass'),
 ('lasers: attaching the arm never tells it about the conductor',
  'ltcplay/fire_ice.py',
  '            arm.on_permitted = self.conductor.lasers_armed',
  '            pass'),
 ('lasers: ltc serve never hands the laser arm to the conductor',
  'ltcplay/web.py',
  '            httpd.fire_ice.attach_laser_arm(httpd.morning.laser_arm)',
  '            pass'),
 ('lasers: a Hold press does not disarm the lasers at the press',
  'ltcplay/remote.py',
  '        if name in ("abort", "hold"):\n'
  '            # Every laser disarmed',
  '        if name == "abort":\n'
  '            # Every laser disarmed'),
 ('lasers: an unconfirmed Abort does not disarm the lasers',
  'ltcplay/remote.py',
  '        if name in ("abort", "hold"):\n'
  '            # Every laser disarmed',
  '        if name == "hold" or (name == "abort" and\n'
  '                               body.get("confirmed") is True):\n'
  '            # Every laser disarmed'),
 ('lasers: arming from the show network needs no screen_arming',
  'ltcplay/remote.py',
  '                (name == "laser-test" and body.get("on") is True) or\n'
  '                (name == "laser-arm" and body.get("on") is True)) and \\',
  '                (name == "laser-test" and body.get("on") is True)) and \\'),
 ('lasers: arming is allowed while the show is on Hold',
  'ltcplay/remote.py',
  '        if state in ("PAUSED", "HOLD") or \\\n'
  '                getattr(cond, "_look", None) in ("HELD", "DARK"):',
  '        if False:'),
 ('lasers: arming is allowed while the Abort latch stands',
  'ltcplay/remote.py',
  '            return "the show is aborted; press Reset first, then arm"',
  '            pass'),
 ('lasers: with no schedule, arming is allowed',
  'ltcplay/remote.py',
  '            return ("the show state is not known (no schedule is loaded), "\n'
  '                    "so no laser may be armed")',
  '            return ""'),
 ('lasers: the rack screen never shows the lasers disarmed',
  'ltcplay/remote.py',
  '                    add("Laser arm", "off", "disarmed")',
  '                    pass'),
 ('lasers: a laser-arm body that is not on:true arms',
  'ltcplay/morning.py',
  '        if body.get("on") is True:\n'
  '            ok, text = arm.arm(',
  '        if body.get("on"):\n'
  '            ok, text = arm.arm('),
 ('lasers: a lockout does not disarm the laser',
  'ltcplay/morning.py',
  '                    self.laser_arm.on_lock(uid, who, device)',
  '                    pass'),
 ('lasers: Arrange mode lets a laser arm through',
  'ltcplay/morning.py',
  '            or (name == "laser-arm" and body.get("on") is True)',
  ''),
 ('lasers: the night closing never reaches the laser arm',
  'ltcplay/morning.py',
  '            self.laser_arm.tick(None if err else state)',
  '            self.laser_arm.tick(None)'),
 ('lasers: arming is allowed while the night closes',
  'ltcplay/morning.py',
  '        if err is None and state in laserarm.CLOSING_STATES:\n'
  '            return "the night is closing"',
  '        if False:\n'
  '            return "the night is closing"'),
 ('lasers: closing the Morning page never closes the laser arm',
  'ltcplay/morning.py',
  '                self.laser_arm.close()',
  '                pass'),
 ('lasers: a laser disarm needs the arm permission',
  'ltcplay/users.py',
  '        if name == "laser-arm" and body.get("on") is not True:',
  '        if False:'),
 ('lasers: Arrange mode on the page lets a laser arm through',
  'ltcplay/web/remote.html',
  ' ||\n'
  '    (route === "laser-arm" && extra && extra.on === true);',
  ';'),
 ("lasers: the page reads ALL ARMED without the engine's word",
  'ltcplay/web/remote.html',
  '  if(la.output_permitted) return "Lasers: ALL ARMED.',
  '  if(true) return "Lasers: ALL ARMED.'),
 ('lasers: an arm racing a disarm still writes armed',
  'ltcplay/laserarm.py',
  '            raced = self._disarm_gen != gen or self._presses',
  '            raced = False'),
 ('lasers: a disarm does not move the disarm generation',
  'ltcplay/laserarm.py',
  '            self._disarm_gen += 1\n'
  '            was = [',
  '            was = ['),
 ('lasers: an Abort or Hold press in flight does not refuse arming',
  'ltcplay/laserarm.py',
  '        if pressing:\n'
  '            return ("an Abort or Hold is being pressed',
  '        if False:\n'
  '            return ("an Abort or Hold is being pressed'),
 ('lasers: the remote never tells the arm an Abort or Hold is under way',
  'ltcplay/remote.py',
  '                arm.press_begin()\n            except Exception:                    # noqa: BLE001\n                arm = None\n        try:\n            return self._press(',
  '                pass\n            except Exception:                    # noqa: BLE001\n                arm = None\n        try:\n            return self._press('),
 ('lasers: the remote never says the Abort or Hold press is over',
  'ltcplay/remote.py',
  '            return self._press(name, body, ctx)\n        finally:\n            if arm is not None:\n                try:\n                    arm.press_end()',
  '            return self._press(name, body, ctx)\n        finally:\n            if arm is not None:\n                try:\n                    pass'),
 ('lasers: the night over does not disarm when CLOSING was never seen',
  'ltcplay/laserarm.py',
  '        off_in = state in OFF_STATES and self._last_state not in OFF_STATES',
  '        off_in = False'),
 ('lasers: CLOSING lets a laser stay armed after its first pass',
  'ltcplay/laserarm.py',
  '        if (closing and (not self._was_closing or self.armed_ids())) or \\\n',
  '        if (closing and not self._was_closing) or \\\n'),
 ('lasers: a blocker appearing while lit does not disarm',
  'ltcplay/laserarm.py',
  '        if self._was_permitted and why is not None and \\\n',
  '        if False and \\\n'),
 ('lasers: one laser disarmed by hand disarms all the others',
  'ltcplay/laserarm.py',
  '                len(self.armed_ids()) == len(self.order):',
  '                True:'),
 ('lasers: an announcement does not disarm the lasers',
  'ltcplay/conductor.py',
  '            self._disarm_lasers("Announcement", who, screen)',
  '            pass'),
 ('lasers: the sender writes its lines before the blank and disable',
  'ltcplay/laserarm.py',
  '        if job["kind"] == "dark":\n'
  '            self._dark(job)\n'
  '        else:\n'
  '            self._enable(job)\n'
  '        for text, fields in job["notes"]:\n'
  '            self._note(text, **fields)',
  '        for text, fields in job["notes"]:\n'
  '            self._note(text, **fields)\n'
  '        if job["kind"] == "dark":\n'
  '            self._dark(job)\n'
  '        else:\n'
  '            self._enable(job)'),
 ('lasers: a test is recorded ON only after it was sent',
  'ltcplay/lasertest.py',
  '            with self._lock:\n'
  '                self.on[laser.id] = {"since": self._clock(), "who": label}\n'
  '            ok = self._send_list(laser, laser.on)',
  '            ok = self._send_list(laser, laser.on)\n'
  '            with self._lock:\n'
  '                self.on[laser.id] = {"since": self._clock(), "who": label}'),
 ('lasers: a test whose laser was disarmed during its ON stays on',
  'ltcplay/lasertest.py',
  '            if not self.is_armed(laser.id):\n'
  '                with self._lock:\n'
  '                    self.on.pop(laser.id, None)',
  '            if False:\n'
  '                with self._lock:\n'
  '                    self.on.pop(laser.id, None)'),
 ("lasers: a laser's own lists may send BEYOND's global controls",
  'ltcplay/lasertest.py',
  '_REFUSED = frozenset(a.lower() for a in FORBIDDEN_ADDRESSES |\n'
  '                     GLOBAL_ADDRESSES)',
  '_REFUSED = frozenset(a.lower() for a in FORBIDDEN_ADDRESSES)'),
 ('lasers: the restore does not read the timecode epoch before the arm check',
  'ltcplay/beyondtc.py',
  '        epoch = self.gate.epoch() if self.gate is not None else None\n'
  '        if self.arm_check is not None:',
  '        epoch = None\n'
  '        if self.arm_check is not None:'),

 # -- the laser arm, second review of PR #87 --
 ('lasers: a merged disarm forgets the lasers it covers',
  'ltcplay/laserarm.py',
  '                        w["ids"] = list(dict.fromkeys(list(w["ids"]) +\n'
  '                                                      list(ids)))',
  '                        pass'),
 ('lasers: Arrange mode on the page holds back a disarm click',
  'ltcplay/web/remote.html',
  '  if(all){ moPress("laser-arm", {lasers: ids, on:false}, "mo-lmsg"); return; }\n'
  '  if(arrangeBlocked("laser-arm", {on:true})) return;',
  '  if(arrangeBlocked("laser-arm", {on:true})) return;\n'
  '  if(all){ moPress("laser-arm", {lasers: ids, on:false}, "mo-lmsg"); return; }'),
 ("lasers: a trailing slash gets a refused address into a laser's lists",
  'ltcplay/lasertest.py',
  '    return a.rstrip("/")',
  '    return a'),
 ("lasers: a case variant gets a refused address into a laser's lists",
  'ltcplay/lasertest.py',
  '    a = address.strip().lower()',
  '    a = address.strip()'),
 ('lasers: a laser disarmed during its placeholder arm hears the arm last',
  'ltcplay/lasertest.py',
  '        if which == "arm" and not self.is_armed(laser.id):',
  '        if False:'),
 ('lasers: the startup dark waits for the Morning page',
  'ltcplay/web.py',
  '        _startup_lasers_dark(wiring)\n',
  '        pass\n'),
 ("lasers: the laser arm's tick runs after a watcher call that can raise",
  'ltcplay/morning.py',
  '        try:\n'
  '            # The laser arm FIRST',
  '        self._check_flamesafe_lockout()\n'
  '        try:\n'
  '            # The laser arm FIRST'),

 # -- LASERS on the show screen (Jeff, 2026-10-22) --
 ('rack lasers: Disarm all lasers waits for a fresh status',
  'ltcplay/web/remote.html',
  '  return lzPress({all:true, on:false});',
  '  if(LZ_STALE) return null;\n  return lzPress({all:true, on:false});'),
 ('rack lasers: Arrange mode holds back Disarm all lasers',
  'ltcplay/web/remote.html',
  '  return lzPress({all:true, on:false});',
  '  if(arrangeBlocked("laser-arm", {on:true})) return null;\n'
  '  return lzPress({all:true, on:false});'),
 ('rack lasers: Disarm all lasers asks for a confirm',
  'ltcplay/web/remote.html',
  '  return lzPress({all:true, on:false});',
  '  return confirmBox("Disarm all lasers?", "", "Disarm", "").then(ok => ok ? '
  'lzPress({all:true, on:false}) : null);'),
 ('rack lasers: the screen arming switch holds back Disarm all lasers',
  'ltcplay/web/remote.html',
  '  return lzPress({all:true, on:false});',
  '  if((LZ || {}).arm_here) return null;\n  return lzPress({all:true, on:false});'),
 ('rack lasers: Disarm all lasers sends a body that is not a disarm',
  'ltcplay/web/remote.html',
  '  return lzPress({all:true, on:false});',
  '  return lzPress({all:true, on:true});'),
 ('rack lasers: Disarm all lasers is not wired',
  'ltcplay/web/remote.html',
  '$("lz-disarm").addEventListener("click", lzDisarmAll);',
  ''),
 ("rack lasers: an armed laser's chip arms instead of disarming",
  'ltcplay/web/remote.html',
  '  if(ls.length && ls.every(x => x.armed)) return lzPress({lasers:ids, on:false});',
  ''),
 ('rack lasers: a chip arms with no confirm',
  'ltcplay/web/remote.html',
  '  if(!(await confirmBox("Arm " + names + "?", LZ_ARM_NOTE, "Arm", "start"))) return null;',
  ''),
 ('rack lasers: Arrange mode lets a chip arm',
  'ltcplay/web/remote.html',
  '  if(arrangeBlocked("laser-arm", {on:true})) return null;\n'
  '  const names',
  '  const names'),
 ('rack lasers: a chip arms on a stale status',
  'ltcplay/web/remote.html',
  '  if(stale) return "no contact with the engine, so the lasers\' state is not known";',
  ''),
 ('rack lasers: a chip asks to arm with the screen arming switch off',
  'ltcplay/web/remote.html',
  '  if(la && la.arm_here) return la.arm_here;',
  ''),
 ('rack lasers: a chip asks to arm a locked laser',
  'ltcplay/web/remote.html',
  '  if(locked.length) return locked.join(", ")',
  '  if(false) return locked.join(", ")'),
 ('rack lasers: the page never asks its own refusals before an arm',
  'ltcplay/web/remote.html',
  '  const why = lzArmRefusal(la, ls, LZ_STALE);',
  '  const why = "";'),
 ('rack lasers: a stale status leaves the chips pressable',
  'ltcplay/web/remote.html',
  '  }).concat(ls.map(x => ({ids:[x.id], off:stale,',
  '  }).concat(ls.map(x => ({ids:[x.id], off:false,'),
 ('rack lasers: the show screen never draws the strip',
  'ltcplay/web/remote.html',
  '  renderLaserStrip(ST && ST.laser_arm, stale);\n',
  ''),
 ('rack lasers: the chips may widen the page',
  'ltcplay/web/remote.html',
  '.lz-chips{display:flex;flex-wrap:wrap;gap:6px;min-width:0;overflow:hidden;',
  '.lz-chips{display:flex;flex-wrap:wrap;gap:6px;'),
 ('rack lasers: the status leaves the strip out',
  'ltcplay/remote.py',
  '            out["laser_arm"] = self.rack_laser_view(m, ctx)',
  '            pass'),
 ('rack lasers: the strip ignores the screen arming switch',
  'ltcplay/remote.py',
  '        v["arm_here"] = "" if self.may_act_from(ctx.local) else (',
  '        v["arm_here"] = "" if True else ('),
 ('rack lasers: Disarm all lasers waits on the show conductor',
  'ltcplay/morning.py',
  '        if not arm._threaded:\n'
  '            arm.run_pending()',
  '        arm.run_pending()'),
 ('rack lasers: Disarm every flame group disarms the lasers too',
  'ltcplay/remote.py',
  '            out = self.disarm_all(who, screen)\n',
  '            self._lasers_disarm_now(who, screen, name)\n'
  '            out = self.disarm_all(who, screen)\n'),
 ('disarm all: the screen Disarm all forces an arm cycle again (Abort way)',
  'ltcplay/remote.py',
  '        if not names or not deck_fresh:\n',
  '        if True:\n'),
 ('disarm all: a plain disarm goes with no deck reading the engine',
  'ltcplay/remote.py',
  '        if not names or not deck_fresh:\n',
  '        if not names:\n'),
 ('disarm all: a plain disarm is never checked by flamesafe',
  'ltcplay/remote.py',
  '        if left == []:\n            self._journal(who or "system"',
  '        if True:\n            self._journal(who or "system"'),
 ('disarm all: a plain disarm is handed to the deck for one group only',
  'ltcplay/remote.py',
  '            for i, gname in enumerate(names):\n'
  '                self._disarm_ids += 1',
  '            for i, gname in enumerate(names[:1]):\n'
  '                self._disarm_ids += 1'),
 ('disarm all: an older press forces after a newer one',
  'ltcplay/remote.py',
  '            if gen != self._plain_gen:\n                return None',
  '            if False:\n                return None'),
 ('lasers lamp: says lit with BEYOND gone',
  'ltcplay/remote.py',
  '                    link["link"] == "lost":\n',
  '                    False:\n'),
 ('lasers lamp: lit reads ok whatever the BEYOND link says',
  'ltcplay/remote.py',
  '                elif link is not None and link["link"] == "up":\n',
  '                elif True:\n'),
 ('beyond watch: a look of any age still counts',
  'ltcplay/beyondwatch.py',
  '        if age > self.stale_s:\n',
  '        if False:\n'),
 ('beyond watch: BEYOND exiting raises no alert',
  'ltcplay/remote.py',
  '                self.alerts.raise_("lasers", text)',
  '                pass'),
 ('beyond watch: a process list without BEYOND reads as running',
  'ltcplay/beyondwatch.py',
  '            return True\n    return False\n',
  '            return True\n    return True\n'),
 ('madmapper watch: a process list without MadMapper reads as running',
  'ltcplay/madmapperwatch.py',
  'IMAGES = ("madmapper*.exe",)',
  'IMAGES = ("*.exe",)'),
 ('madmapper watch: a look of any age still counts',
  'ltcplay/madmapperwatch.py',
  '        if age > self.stale_s:\n',
  '        if False:\n'),
 ('madmapper watch: a list without MadMapper still reads as running',
  'ltcplay/madmapperwatch.py',
  '            return True\n    return False\n',
  '            return True\n    return True\n'),
 ('madmapper watch: MadMapper on another machine is looked for here',
  'ltcplay/madmapperwatch.py',
  '    if windows and on_this_pc(host):\n        return windows_probe\n',
  '    if windows:\n        return windows_probe\n'),
 ('madmapper watch: MadMapper on another machine is not said',
  'ltcplay/madmapperwatch.py',
  '    if not on_this_pc(host):\n',
  '    if False:\n'),
 ('madmapper watch: MadMapper exiting raises no alert',
  'ltcplay/remote.py',
  '                self.alerts.raise_("video", text)',
  '                pass'),
 ('video lamp: says running with MadMapper gone',
  'ltcplay/remote.py',
  '        if mm is not None and mm["link"] == "lost":\n',
  '        if False:\n'),
 ('video lamp: a sent and unconfirmed command reads not sent yet',
  'ltcplay/remote.py',
  '            sent = int(getattr(cond, "_ver", {}).get("video") or 0) > 0\n',
  '            sent = False\n'),
 ('video lamp: a silent heartbeat during a show is never said',
  'ltcplay/madmapperwatch.py',
  '        if playing:\n',
  '        if False:\n'),
 ('video lamp: a silent heartbeat between shows is an alarm',
  'ltcplay/madmapperwatch.py',
  '        if playing:\n',
  '        if True:\n'),
 ('video lamp: no heartbeat block still alarms',
  'ltcplay/madmapperwatch.py',
  '                getattr(cfg, "show_len_s", None) is None:\n',
  '                False:\n'),
 ('video lamp: the heartbeat is expected on Hold and between shows',
  'ltcplay/remote.py',
  '        return bool((cs or {}).get("running")) and \\\n'
  '            (snap or {}).get("look") == "PLAYING"\n',
  '        return bool((cs or {}).get("running"))\n'),
 ('madmapper heartbeat: a packet while disarmed is never heard',
  'ltcplay/madmapper.py',
  '            self._heard_at = now\n',
  '            pass\n'),
 ('madmapper heartbeat: a packet while disarmed is counted',
  'ltcplay/madmapper.py',
  '            self._heard_count += 1\n            if not self.armed:\n'
  '                return\n',
  '            self._heard_count += 1\n            self.packets_in += 1\n'
  '            if not self.armed:\n                return\n'),
 ('artnet: an unused show file in the folder is a live fault again',
  'ltcplay/artnodes.py',
  '        quiet = active is not None and n not in active\n',
  '        quiet = False\n'),
 ('artnet: the loaded show is not the one checked',
  'ltcplay/remote.py',
  '        if name:\n            if name != self._selected_show():',
  '        if False:\n            if name != self._selected_show():'),
 ('artnet: the show last Run is forgotten after a restart',
  'ltcplay/remote.py',
  '                sel = was\n',
  '                sel = None\n'),
 ('artnet: with no show Run yet the files are only notes',
  'ltcplay/remote.py',
  '        return {sel} if sel else None',
  '        return {sel} if sel else set()'),
 ('artnet: a newly loaded show waits for the 15 s cache',
  'ltcplay/remote.py',
  '        if got is not None and now - got[1] < CHECKS_READ_S and \\\n'
  '                got[2] == active:',
  '        if got is not None and now - got[1] < CHECKS_READ_S:'),
 ('disarm all: a group still wanted counts as confirmed off',
  'ltcplay/remote.py',
  '                            (gs[i].get("armed") != "disarmed" or\n'
  '                             gs[i].get("wanted"))]',
  '                            (gs[i].get("armed") != "disarmed")]'),
 ('disarm all: a direct Disarm all leaves screen holds running',
  'ltcplay/remote.py',
  '        self._drop_holds_all()\n        with self._arm_lock:\n'
  '            for i, gname',
  '        with self._arm_lock:\n            for i, gname'),
 ('disarm all: a press on a stale status watches no group',
  'ltcplay/remote.py',
  '            return names, set(range(len(names)))',
  '            return names, set()'),
 ('disarm all: a check that cannot be scheduled leaves it unconfirmed',
  'ltcplay/remote.py',
  '            self.confirm_plain_disarm(gen, who, screen, None)\n',
  '            pass\n'),
 ('disarm all: a deck silent for 30 s still carries the plain disarm',
  'ltcplay/remote.py',
  'PLAIN_DECK_FRESH_S = 0.5',
  'PLAIN_DECK_FRESH_S = 30.0'),
 # A screen's per-group Disarm (2026-10-29, Jeff's "armed in 2.0 but
 # disarmed in 1.0"): confirmed by flamesafe, or forced, never only sent.
 ('group disarm: a deck that stopped reading the engine is waited on',
  'ltcplay/remote.py',
  '        if confirm and not deck_fresh:\n',
  '        if False:\n'),
 ('group disarm: a press with no flamesafe status waits, then forces',
  'ltcplay/remote.py',
  '        elif confirm and not status_fresh:\n',
  '        elif False:\n'),
 ('group disarm: the forced disarm waits for the journal on disk',
  'ltcplay/remote.py',
  '        if forced is not None:\n'
  '            # The disarm first, the disk after (Abort\'s _disarm_now).\n'
  '            sent = self._group_force_send(who, device, gname, forced[1])\n'
  '        self._journal(who, device, "disarm", "sent",\n'
  '                      f"{who or \'Someone\'} pressed Disarm {gname} on the "\n'
  '                      f"{device}. Sent to the Stream Deck, which owns the "\n'
  '                      f"arm link; flamesafe\'s lamp shows when it is off.")\n',
  '        self._journal(who, device, "disarm", "sent",\n'
  '                      f"{who or \'Someone\'} pressed Disarm {gname} on the "\n'
  '                      f"{device}. Sent to the Stream Deck, which owns the "\n'
  '                      f"arm link; flamesafe\'s lamp shows when it is off.")\n'
  '        if forced is not None:\n'
  '            # The disk first, then the disarm (Abort\'s _disarm_now).\n'
  '            sent = self._group_force_send(who, device, gname, forced[1])\n'),
 ('group disarm: answered confirmed without flamesafe\'s word',
  'ltcplay/remote.py',
  '        if self._group_seen_off(i, gname, reads0):\n',
  '        if True:\n'),
 ('group disarm: a status from before the deck read it confirms',
  'ltcplay/remote.py',
  '                        got - read_at >= GROUP_DISARM_SETTLE_S:\n',
  '                        True:\n'),
 ('group disarm: confirmed with no deck read after the press',
  'ltcplay/remote.py',
  '                if read_at is None and self._deck_reads > reads0:\n',
  '                if read_at is None:\n'),
 ('group disarm: a group still wanted counts as off',
  'ltcplay/remote.py',
  '                            g.get("armed") == "disarmed" and \\\n'
  '                            not g.get("wanted"):\n',
  '                            g.get("armed") == "disarmed":\n'),
 ('group disarm: the forced disarm is never sent',
  'ltcplay/remote.py',
  '            r = fn(f"{label} pressed Disarm {gname} on the {device} "',
  '            r = None and fn(f"{label} pressed Disarm {gname} on the '
  '{device} "'),
 ('group disarm: a forced disarm raises no alert',
  'ltcplay/remote.py',
  '            self.alerts.raise_("flames", alert)\n',
  '            pass\n'),
 ('group disarm: a forced disarm that failed is said as forced',
  'ltcplay/remote.py',
  '        if ok:\n            alert = (f"{lead}: ALL groups disarm sent.',
  '        if True:\n            alert = (f"{lead}: ALL groups disarm sent.'),
 ('rack screen: a late Disarm answer covers the Abort pressed after it',
  'ltcplay/web/remote.html',
  '  if(seq >= (SAID_SEQ[el.id] || 0) && (el.id !== "ctl-msg" || seq >= ABORT_SEQ)){',
  '  if(true){'),
 ('ui2 page: a forced Disarm is said as done',
  'ltcplay/web/remote2.html',
  '    const forced = route === "group-disarm" && j.forced === true;\n',
  '    const forced = false;\n'),
 ('disarm all: a forced disarm that failed is said as done',
  'ltcplay/remote.py',
  '        if out.get("ok"):\n            text = (f"Disarm all flame groups was not confirmed',
  '        if True:\n            text = (f"Disarm all flame groups was not confirmed'),
 ('disarm all: the purge selection is cleared after the disarm',
  'ltcplay/remote.py',
  '            purge = getattr(m, "purge", None)\n',
  '            purge = None\n'),
 ('disarm ids: a new engine starts its disarm ids at 0 again',
  'ltcplay/remote.py',
  '            self._disarm_ids = max(0, int(wall() * 1000))',
  '            self._disarm_ids = 0'),
 ('lasers lamp: a restarted BEYOND reads lit with no new unblank',
  'ltcplay/remote.py',
  '        if now is None or now == at[0]:\n            return False',
  '        if False:\n            return False'),
 ('beyond watch: up, unknown, then lost raises no alert',
  'ltcplay/remote.py',
  '        if link == "up":\n            self._beyond_was_up = True\n',
  '        if link == "up":\n            self._beyond_was_up = True\n'
  '        if link == "unknown":\n            self._beyond_was_up = False\n'),
 ('artnet: the show last Run is taken as the selection again',
  'ltcplay/remote.py',
  '            with open(os.path.join(self.folder, self.SELECTED_SHOW_FILE),\n'
  '                      encoding="utf-8") as fh:',
  '            with open(os.path.join(self.folder, self.LAST_SHOW_FILE),\n'
  '                      encoding="utf-8") as fh:'),
 ('artnet: the show picker\'s choice is not kept',
  'ltcplay/remote.py',
  '        self._save_selected_show(f)\n        self._artnet = None',
  '        self._artnet = None'),
 ('artnet: the selection file is read on every status poll',
  'ltcplay/remote.py',
  '        if got is not None and now - got[1] < self.SELECTED_READ_S:',
  '        if False:'),
 ('artnet: why no show file is judged is not said',
  'ltcplay/remote.py',
  '                return "Art-Net receivers, a note: " + self.AMBIGUOUS + \\\n',
  '                return "Art-Net receivers, a note: " + \\\n'),
 ('artnet: with no show in use an unused file is a red FAULT again',
  'ltcplay/remote.py',
  '                text = ""         # a note on the rack screen, not an alert',
  '                pass'),
 ('artnet: with no show in use the rack screen gets no note',
  'ltcplay/remote.py',
  '            if found and self._artnet_unchosen(seen):',
  '            if False:'),
 ('artnet: the only show file in the folder is no longer judged',
  'ltcplay/remote.py',
  '        return active is None and seen > 1',
  '        return active is None'),
 ('artnet: with no show in use the Morning card is red',
  'ltcplay/remote.py',
  '                    "level": "warn",\n'
  '                    "text": self.AMBIGUOUS',
  '                    "level": "bad",\n'
  '                    "text": self.AMBIGUOUS'),
 ('artnet: the selection is written in place, not replaced whole',
  'ltcplay/remote.py',
  '        tmp = path + ".tmp"\n',
  '        tmp = path\n'),
 ('artnet: a stalled disk holds up Run\'s reply again',
  'ltcplay/remote.py',
  '            threading.Thread(target=self._selection_writer, daemon=True,\n'
  '                             name="ltcplay-show-select").start()',
  '            self._selection_writer()'),
 ('artnet: a chosen show is not known until its write lands',
  'ltcplay/remote.py',
  '        if pend:\n            return pend',
  '        if False:\n            return pend'),
 ('artnet: Run does not keep the show in use for a restart',
  'ltcplay/web.py',
  '                    _rm.note_run(getattr(s, "timeline_path", None))',
  '                    pass'),
 ('lasers lamp: a laser command sent while BEYOND was gone counts',
  'ltcplay/remote.py',
  '            self._beyond_lost_ver = ("pending",)',
  '            self._beyond_lost_ver = (self._lasers_ver(\n'
  '                getattr(self.schedule, "conductor", None)),)'),
 ('disarm ids: the range the last engine reserved is ignored',
  'ltcplay/remote.py',
  '                seed = max(seed, int(json.load(fh).get("next", 0)))',
  '                json.load(fh)'),
 ('disarm ids: the show engine never reserves its range',
  'ltcplay/web.py',
  '        httpd.remote.reserve_disarm_ids()',
  '        pass'),
 ('schedule tab: the list is rebuilt on every status poll again',
  'ltcplay/web/remote.html',
  '  if(key === SLOT_KEY) return;',
  '  if(false) return;'),
 ('schedule tab: a tap is read only as a click on one element again',
  'ltcplay/web/remote.html',
  '  if(el) toggleSlot(el.dataset.k);\n});',
  '  if(false) toggleSlot(el.dataset.k);\n});'),
 ('schedule tab: a show with no reason opens to nothing',
  'ltcplay/web/remote.html',
  '(notes || "No notes for this show.")',
  'notes'),
 ('mode switch: Programming is offered during a live show',
  'ltcplay/web/remote.html',
  '  if(want === "programming" && live){',
  '  if(false){'),
 ('mode switch: Run in Programming starts a Show',
  'ltcplay/web/remote.html',
  'post("/api/start", prog ? {timeline:f, on_lost:"hold"} : {timeline:f})',
  'post("/api/start", {timeline:f})'),
 ('arm button: a refused hold greys the state out',
  'ltcplay/web/remote.html',
  '.lamp button.armb:disabled{opacity:1;filter:none;cursor:default}\n',
  ''),
 ('arm again: flamesafe\'s cycle the arm is shown as it is',
  'ltcplay/web/remote.html',
  '  if(REASON_WORDS[r]) return REASON_WORDS[r];',
  '  if(false) return REASON_WORDS[r];'),
 ('arm again: the button does not say Disarm, then hold',
  'ltcplay/web/remote.html',
  '  if(look.held === "cycle") return "Disarm, then hold";\n',
  ''),
 ('time zone: the schedule\'s own zone is not the one chosen',
  'ltcplay/web/remote.html',
  '  sel.value = want || "America/Denver";',
  '  sel.value = "America/Denver";'),
 ('arm again: the Stream Deck key says CYCLE ARM again',
  'ltcplay/streamdeck.py',
  '    "cycle the arm": "ARM AGAIN",',
  '    "cycle the arm": "CYCLE ARM",'),
 # Live flame programming (flameprog.py, LIVE_FLAME_PROGRAMMING.md): one
 ("live flame: the show's cues go out while it is on",
  'ltcplay/flamelink.py',
  "        if self._program_on:\n            # Live flame programming is on: the show's own cues are held",
  "        if False:\n            # Live flame programming is on: the show's own cues are held"),
 ('live flame: a live show sends the program frame',
  'ltcplay/flamelink.py',
  '            # The mode ends as soon as the engine sees the show start.\n            return tc, zeros',
  '            # The mode ends as soon as the engine sees the show start.\n            return tc, self._program_values(now)'),
 ('live flame: the program frame never expires at the link',
  'ltcplay/flamelink.py',
  '        if now - p[1] > PROGRAM_HOLD_S or now < p[1]:\n            self._program = None',
  '        if now < p[1]:\n            self._program = None'),
 ('live flame: an Abort leaves it on at the link',
  'ltcplay/flamelink.py',
  '                self._program = None\n                self._program_on = False\n                self.zeroed = True',
  '                self._program = None\n                self.zeroed = True'),
 ('live flame: the link takes a program frame while it is off',
  'ltcplay/flamelink.py',
  '            if not self._program_on:\n                return False\n            if session != self._program_session:',
  '            if session != self._program_session:'),
 ('live flame: the purge frame is read while it is on',
  'ltcplay/flamelink.py',
  '            if self._program_on:\n                return tc, self._program_values(now)\n            return tc, self._purge_values(now)',
  '            return tc, self._purge_values(now)'),
 ('live flame: the listener binds any address',
  'ltcplay/flameprog.py',
  '    if ip != LISTEN_IP:\n        raise SettingsError(',
  '    if False:\n        raise SettingsError('),
 ('live flame: a packet from off this PC is taken',
  'ltcplay/flameprog.py',
  '        if host.startswith("127.") and via == "loopback":\n            origin = "this PC"',
  '        if True:\n            origin = "this PC"'),
 # Live flame programming from a Mac (allowed_source_ip): one per gate.
 ('live flame Mac: any show network source is taken',
  'ltcplay/flameprog.py',
  '        elif mac is not None and host == mac and via == "listen_ip":',
  '        elif mac is not None and via == "listen_ip":'),
 ('live flame Mac: a public or odd range address is the Mac',
  'ltcplay/flameprog.py',
  '    if not any(a in ipaddress.IPv4Network(f"{n}/{m}")\n               for n, m in PRIVATE_NETS):\n        raise bad',
  '    if False:\n        raise bad'),
 ('live flame Mac: a broadcast address is the Mac',
  'ltcplay/flameprog.py',
  '    if str(a) != ip or ip.split(".")[3] in ("0", "255"):',
  '    if str(a) != ip:'),
 ('live flame Mac: listen_ip may be the Mac',
  'ltcplay/flameprog.py',
  '        if ip == allowed_source_ip:',
  '        if False:'),
 ('live flame Mac: listen_ip need not be this PC',
  'ltcplay/flameprog.py',
  '        if not self._is_local(me):',
  '        if False:'),
 ('live flame Mac: the Mac may be this PC',
  'ltcplay/flameprog.py',
  '        if self._is_local(mac):',
  '        if False:'),
 ('live flame Mac: the Mac may be the flame node',
  'ltcplay/flameprog.py',
  '        if fs is None or fs[0] == mac:',
  '        if fs is None:'),
 ('live flame Mac: the Mac may be an xLights controller',
  'ltcplay/flameprog.py',
  '        if mac in fmap.ips:',
  '        if False:'),
 ('live flame Mac: the Mac may be a configured device',
  'ltcplay/flameprog.py',
  '        if mac in devices:',
  '        if False:'),
 ('live flame Mac: the listener binds the any address',
  'ltcplay/flameprog.py',
  '        return [(LISTEN_IP, self.port), (self.listen_ip, self.port)]',
  '        return [(LISTEN_IP, self.port), ("0.0.0.0", self.port)]'),
 ('live flame Mac: one sender is not one place',
  'ltcplay/flameprog.py',
  '            other = (held[0], held[1], held[3]) != ident',
  '            other = (held[0], held[1]) != ident[:2]'),
 ('live flame Mac: the flame controller may send elsewhere than the listener',
  'ltcplay/flameprog.py',
  '        if getattr(fmap, "dest", LISTEN_IP) != s.listen_ip:',
  '        if False:'),
 ('live flame Mac: another controller may send to listen_ip',
  'ltcplay/flameprog.py',
  '                              dest == want or _multicast(dest)):',
  '                              _multicast(dest)):'),
 ("live flame Mac: flamesafe's output to listen_ip is not a loop back",
  'ltcplay/flameprog.py',
  '             or ip == "0.0.0.0" or ip == settings.listen_ip)',
  '             or ip == "0.0.0.0")'),
 ('live flame Mac: a loopback source is taken on the listen_ip socket',
  'ltcplay/flameprog.py',
  '        if host.startswith("127.") and via == "loopback":',
  '        if host.startswith("127."):'),
 ('live flame Mac: the Mac is taken on the loopback socket',
  'ltcplay/flameprog.py',
  '        elif mac is not None and host == mac and via == "listen_ip":',
  '        elif mac is not None and host == mac:'),
 ('live flame Mac: a packet from no known socket counts as loopback',
  'ltcplay/flameprog.py',
  '        if via is None and mac is None:',
  '        if via is None:'),
 ('live flame Mac: the fingerprint ignores addresses, states and protocols',
  'ltcplay/flameprog.py',
  '    return [el.tag, sorted(el.attrib.items()), [_canonical(c) for c in el]]',
  '    return [el.tag, sorted((k, v) for k, v in el.attrib.items() if k not in ("IP", "ComPort", "ActiveState", "NetworkType")), [_canonical(c) for c in el]]'),
 ('live flame Mac: an address in xlights_networks.xml that is not plain is skipped',
  'ltcplay/flameprog.py',
  '        if bad:\n            return (f"{fmap.path} has an address',
  '        if False:\n            return (f"{fmap.path} has an address'),
 ('live flame Mac: an address in the configs that is not plain is skipped',
  'ltcplay/flameprog.py',
  '            elif quad.match(v) or (_address_key(key) and\n',
  '            elif False and (_address_key(key) and\n'),
 ("live flame Mac: flamesafe's destination as a name is taken",
  'ltcplay/flameprog.py',
  '        if fs is not None and not _plain_ipv4(fs[0]) and fs[0] != "localhost":',
  '        if False:'),
 ('live flame Mac: layout files that differ are taken',
  'ltcplay/flameprog.py',
  '    if dest != LISTEN_IP and any(m.fingerprint != maps[0].fingerprint',
  '    if False and any(m.fingerprint != maps[0].fingerprint'),
 ('live flame Mac: any setting starting with / is skipped',
  'ltcplay/flameprog.py',
  '                                   not (str(key).lower() == "address" and',
  '                                   not (True and'),
 ('live flame Mac: any name in the layout is skipped',
  'ltcplay/flameprog.py',
  '                    if v.upper() == "MULTICAST" or v.lower() == "localhost":',
  '                    if v.upper() == "MULTICAST" or not _plain_ipv4(v):'),
 ('live flame Mac: the helper says a row across 512 is not used',
  'tools/xlights_map_fingerprint.py',
  '        if count > 0 and slot > 512:',
  '        if count > 0 and slot + count - 1 > 512:'),
 ('live flame Mac: the listener reads the first socket only',
  'ltcplay/flameprog.py',
  '                        ready = select.select(socks, [], [], WATCH_S)[0]',
  '                        ready = socks[:1]'),
 ("live flame: flamesafe's own packets are not recognised",
  'ltcplay/flameprog.py',
  '        if p.cid == FLAMESAFE_CID or p.source.strip().lower() == \\\n                FLAMESAFE_NAME:',
  '        if False:'),
 ('live flame: a second sender is taken',
  'ltcplay/flameprog.py',
  '        if other:\n            self.end(f"a second E1.31 source appeared',
  '        if False:\n            self.end(f"a second E1.31 source appeared'),
 ('live flame: no flood rule',
  'ltcplay/flameprog.py',
  '        if len(q) > RATE_MAX:',
  '        if False:'),
 ('live flame: preview data is taken',
  'ltcplay/flameprog.py',
  '        if p.preview:\n            self.counts["preview"] += 1',
  '        if False:\n            self.counts["preview"] += 1'),
 ('live flame: no sACN sequence rule',
  'ltcplay/flameprog.py',
  '            if d == 0 or d > 256 - SEQ_WINDOW:',
  '            if False:'),
 ('live flame: a stale universe keeps its values',
  'ltcplay/flameprog.py',
  '            if now - at > HOLD_S or now < at:\n                del self._univ[u]',
  '            if now < at:\n                del self._univ[u]'),
 ('live flame: no seen-at-zero gate',
  'ltcplay/flameprog.py',
  '            for s in blocked:\n                vals[s - 1] = 0',
  '            for s in ():\n                vals[s - 1] = 0'),
 ('live flame: a stream terminated keeps its values',
  'ltcplay/flameprog.py',
  '            if p.terminated:\n                self._univ.pop(p.universe, None)',
  '            if False:\n                self._univ.pop(p.universe, None)'),
 ('live flame: ending does not disarm',
  'ltcplay/flameprog.py',
  '                ok = bool(self._disarm(f"{NAME} ended: {why}"))',
  '                ok = True'),
 ('live flame: two groups armed do not end it',
  'ltcplay/flameprog.py',
  '        if s is not None and s.one_group and len(armed) > 1:',
  '        if False:'),
 ('live flame: an Abort at the link is not followed',
  'ltcplay/flameprog.py',
  '            if link is None or not getattr(link, "program_on", False):',
  '            if link is None:'),
 ("live flame: the engine's blockers are not watched",
  'ltcplay/flameprog.py',
  '            if not why:\n                why = self._blockers() or ""',
  '            if not why:\n                why = ""'),
 ('live flame: no idle auto off',
  'ltcplay/flameprog.py',
  '            if not why and now - self.last_data > s.idle_off_s:',
  '            if False:'),
 ('live flame: no session limit',
  'ltcplay/flameprog.py',
  '            if not why and now - self.began > s.max_session_s:',
  '            if False:'),
 ('live flame: a layout change is not seen',
  'ltcplay/flameprog.py',
  '            if not why and self.map is not None and self.map.changed():',
  '            if False:'),
 ("live flame: entry ignores the engine's blockers",
  'ltcplay/flameprog.py',
  '        try:\n            why = self._blockers() or ""\n        except Exception as e:',
  '        try:\n            why = ""\n        except Exception as e:'),
 ('live flame: entry takes two armed groups',
  'ltcplay/flameprog.py',
  '        if s.one_group and len(armed) > 1:\n            return (',
  '        if False:\n            return ('),
 ("live flame: entry ignores flamesafe's loopback destination",
  'ltcplay/flameprog.py',
  '            why = feedback_refusal(self._fs_dest(), s)',
  '            why = ""'),
 ('live flame: a multicast flame controller is taken',
  'ltcplay/flameprog.py',
  '                if dest != want:\n                    what = (',
  '                if False:\n                    what = ('),
 ('live flame: ON from the network',
  'ltcplay/remote.py',
  '        if not ctx.local:\n            self._journal(who or ctx.device, ctx.device, "live flame",',
  '        if False:\n            self._journal(who or ctx.device, ctx.device, "live flame",'),
 ('live flame: ON without the confirm',
  'ltcplay/remote.py',
  '        if body.get("confirmed") is not True:\n            return 400, {"error": "Confirm it on the page first. Nothing "\n                                  "was changed."}\n        ok, text = lf.begin(',
  '        ok, text = lf.begin('),
 ('live flame: Abort, Hold and Start now do not end it',
  'ltcplay/remote.py',
  '        if name in ("abort", "hold", "start-now"):',
  '        if False:'),
 ("live flame: a second group's hold is not refused",
  'ltcplay/remote.py',
  '        why = self.live_flame_arm_refusal(gname)\n        if why:\n            return why',
  '        why = self.live_flame_arm_refusal(gname)'),
 ('live flame: Run pressed is not a blocker',
  'ltcplay/remote.py',
  '        if s is not None and getattr(s, "running", False):',
  '        if False:'),
 ("live flame: the engine's starting state is not a blocker",
  'ltcplay/remote.py',
  '        if cs.get("running") or cs.get("starting"):',
  '        if False:'),
 ('live flame: a Hold is not a blocker',
  'ltcplay/remote.py',
  '        if state in ("HOLD", "PAUSED"):\n            return "a Hold is standing',
  '        if False:\n            return "a Hold is standing'),
 ('live flame: the Morning page fires while it is on',
  'ltcplay/remote.py',
  '        if lf is not None and lf.on:\n            return ("live flame programming is on; nothing fires from this "',
  '        if False:\n            return ("live flame programming is on; nothing fires from this "'),
 ('live flame: the status leaves it out',
  'ltcplay/remote.py',
  '        out["live_flame"] = self.live_flame_view()',
  '        pass'),
 ('live flame: ON needs no permission',
  'ltcplay/users.py',
  '    "live-flame": "settings",',
  '    "live-flame": None,'),
 ('live flame: OFF needs the Settings permission',
  'ltcplay/users.py',
  '        if name == "live-flame" and body.get("on") is not True:',
  '        if False:'),
 ('live flame: Run does not end it',
  'ltcplay/web.py',
  '                if _rm is not None and hasattr(_rm, "live_flame_end"):',
  '                if False:'),
 ('live flame: the page never shows the red bar',
  'ltcplay/web/remote.html',
  '  ban.hidden = !on;',
  '  ban.hidden = true;'),
 ('live flame: another controller sending to this PC is taken',
  'ltcplay/flameprog.py',
  '                if sends and (dest.startswith("127.") or',
  '                if False and (dest.startswith("127.") or'),
 ('live flame: another controller on a flame universe is taken',
  'ltcplay/flameprog.py',
  '                if univ is not None and univ in seen:',
  '                if False:'),
 ('live flame: an xLights Only controller is not counted as sending',
  'ltcplay/flameprog.py',
  '            sends = a.get("ActiveState", "Active") != "Inactive"',
  '            sends = a.get("ActiveState", "Active") == "Active"'),
 ('live flame: a group being armed is not counted',
  'ltcplay/remote.py',
  '                if g.get("armed") == "armed" or g.get("wanted")]',
  '                if g.get("armed") == "armed"]'),
 ('live flame: a packet does not check the armed groups',
  'ltcplay/flameprog.py',
  '        why = self._groups_problem()\n        if why:\n            # Seen on the packet',
  '        why = ""\n        if why:\n            # Seen on the packet'),
 ("live flame: an old session's frame is taken",
  'ltcplay/flamelink.py',
  '            if session != self._program_session:\n                return False\n',
  ''),
 ('live flame: a conductor zero leaves it on at the link',
  'ltcplay/flamelink.py',
  '            self._program = None\n            self._program_on = False\n            self.zeroed = True',
  '            self._program = None\n            self.zeroed = True'),
 ('live flame: an Abort press waits for the whole end',
  'ltcplay/remote.py',
  '            self.live_flame_cut(f"{who or \'someone\'} pressed {name} on the "',
  '            self.live_flame_end(f"{who or \'someone\'} pressed {name} on the "'),
 ("live flame: a cut does not shut the link's slot",
  'ltcplay/flameprog.py',
  '        if link is not None:\n            try:\n                link.program_end()\n            except Exception:\n                pass\n        return was_on',
  '        return was_on'),
 ("live flame: the end forgets the cut's reason",
  'ltcplay/flameprog.py',
  '                why = self._cut_reason or (',
  '                why = ('),
 ('live flame: a failed begin is left on',
  'ltcplay/flameprog.py',
  '            except Exception as e:\n                self.on = False\n                self._gen += 1',
  '            except Exception as e:\n                self._gen += 1'),
 ("live flame: a failed begin leaves the link's slot open",
  'ltcplay/flameprog.py',
  '                try:\n                    link.program_end()\n                except Exception:\n                    pass\n                self._reset()',
  '                self._reset()'),
 ('live flame: no watchdog on a dead listener',
  'ltcplay/flameprog.py',
  '        if self._threaded and (t is None or not t.is_alive()):',
  '        if False:'),
 ('live flame: no watchdog on a stalled listener',
  'ltcplay/flameprog.py',
  '        elif beat is not None and now - beat > LISTENER_STALL_S:',
  '        elif False:'),
 ('live flame: no junk flood rule',
  'ltcplay/flameprog.py',
  '            flood = len(q) > TOTAL_MAX',
  '            flood = False'),
 ('live flame: an unreadable flamesafe destination passes',
  'ltcplay/flameprog.py',
  '    if dest is None:\n        return (',
  '    if False:\n        return ('),
 ("live flame: end() leaves the link's slot open",
  'ltcplay/flameprog.py',
  '        if link is not None:\n            try:\n                link.program_end()\n            except Exception:\n                pass\n        for sock in socks:',
  '        for sock in socks:'),
 ('live flame: the link ignores program_on for its frame',
  'ltcplay/flamelink.py',
  '        if p is None or not self._program_on:\n            return bytes(UNIVERSE_SIZE)',
  '        if p is None:\n            return bytes(UNIVERSE_SIZE)'),
 ('live flame: a locked slot fires from xLights',
  'ltcplay/flamelink.py',
  '        self.programs_sent += 1\n        return self._unlocked(p[0])',
  '        self.programs_sent += 1\n        return p[0]'),
 ('live flame: a quiet universe is not zeroed between packets',
  'ltcplay/flameprog.py',
  '        if stale:\n            self._push(now)',
  '        if False:\n            self._push(now)'),
 ('live flame: a screen hold with flamesafe unknown is not refused',
  'ltcplay/remote.py',
  '        if armed is None:\n            return ("Live flame programming is on and flamesafe',
  '        if False:\n            return ("Live flame programming is on and flamesafe'),
 ('live flame: a flame link not running is not a blocker',
  'ltcplay/remote.py',
  '            if sender not in ("running",):',
  '            if False:'),
 ('live flame: a purge selection is not a blocker',
  'ltcplay/remote.py',
  '        if pg is not None and pg.selected:',
  '        if False:'),
 ('live flame: a stale flamesafe is not a blocker',
  'ltcplay/remote.py',
  '        if not fl or fl.get("stale"):\n            return ("flamesafe has not reported in the last second, so its "\n                    "link and armed',
  '        if False:\n            return ("flamesafe has not reported in the last second, so its "\n                    "link and armed'),
 ('live flame: closing the engine leaves it on',
  'ltcplay/web.py',
  '            remote.live_flame_end("the engine is closing")',
  '            pass'),
 ('live flame: entry takes an unknown armed state',
  'ltcplay/flameprog.py',
  '        if armed is None:\n            return ("flamesafe has not reported in the last second, so its "\n                    "armed',
  '        if False:\n            return ("flamesafe has not reported in the last second, so its "\n                    "armed'),
 ('live flame: a begin under way ignores a cut',
  'ltcplay/flameprog.py',
  '                if self._cut_epoch != epoch:\n                    raise _CutWhileStarting()',
  '                if False:\n                    raise _CutWhileStarting()'),
 ("live flame: a cut waits on the mode's lock",
  'ltcplay/flameprog.py',
  '        self._cut_epoch += 1\n        was_on = self.on',
  '        with self._lock:\n            self._cut_epoch += 1\n        was_on = self.on'),
 ("live flame: begin's checks hold the mode's lock",
  'ltcplay/flameprog.py',
  '        why, s, fmap = self._check()\n        if why:\n            return self._refused(who, screen, why)',
  '        with self._lock:\n            why, s, fmap = self._check()\n        if why:\n            return self._refused(who, screen, why)'),
 ("live flame: an old end wipes the new session's listener",
  'ltcplay/flameprog.py',
  '            if self._gen == my_gen:',
  '            if True:'),
 ('live flame: two begins at once',
  'ltcplay/flameprog.py',
  '            if self._starting:\n                return False, f"{NAME} is already being turned on."',
  '            if False:\n                return False, f"{NAME} is already being turned on."'),

 # No auto-Run (Jeff's standing rule; Pico, 2026-10-23: a leftover schedule
 # with Run never pressed faded MadMapper every 15 minutes overnight).
 ("the scheduler's own decisions reach the rig before Run",
  "ltcplay/schedule_service.py",
  'if not plan or ev.actor == "operator" or self._run_pressed():',
  'if True:'),

 ("a scheduler call that finds Run stopped is still sent",
  "ltcplay/schedule_service.py",
  'if call.who == "the scheduler" and call.method != "start_show" and \\\n'
  '                not self._run_pressed():',
  'if False:'),

 ("a Run check that raises counts as Run pressed",
  "ltcplay/schedule_service.py",
  "        except Exception:      # noqa: BLE001  never the scheduler\n"
  "            return False",
  "        except Exception:      # noqa: BLE001  never the scheduler\n"
  "            return True"),

 ("anything truthy counts as Run pressed", "ltcplay/schedule_service.py",
  "            return gate() is True", "            return bool(gate())"),

 ("Fire & Ice never gates the scheduler on Run", "ltcplay/fire_ice.py",
  "    svc.run_gate = lambda: show._session() is not None",
  "    svc.run_gate = None"),

 ("a start refused before anything went out fades the rig again",
  "ltcplay/fire_ice.py",
  'nothing_sent=cue.get("nothing_sent") is True)',
  'nothing_sent=False)'),

 ("a start refused for no Run is reported as one that sent something",
  "ltcplay/fire_ice.py",
  '            # Refused before anything went out: the failed start this is\n'
  '            # reported as sends nothing either (no fade, no MadMapper).\n'
  '            with self._lock:\n'
  '                self._cue = {"show": n, "failed": why, "nothing_sent": True}',
  '            with self._lock:\n'
  '                self._cue = {"show": n, "failed": why}'),

 ("the closing reaches the rig before Run", "ltcplay/fire_ice.py",
  "        if self.show._session() is None:\n"
  "            # Run is not pressed: the closing sends nothing to the rig.",
  "        if False:"),

 ("a failed start that sent nothing still fades", "ltcplay/schedule.py",
  "    if ev.nothing_sent:\n        # Refused before anything went out",
  "    if False:\n        # Refused before anything went out"),

 ("the refused start alert says the rig went dark",
  "ltcplay/schedule_service.py",
  "tail = (self.REFUSED_START_ALERT", "tail = (self.FAILED_START_ALERT"),

 ("a leftover soak schedule loads on a show PC",
  "ltcplay/schedule_service.py",
  "            not benchhooks.soak_started(env, ppid):\n"
  "        raise sch.RuleError([SOAK_ONLY_REFUSED], where=path)",
  "            False:\n"
  "        raise sch.RuleError([SOAK_ONLY_REFUSED], where=path)"),

 ("soak Fire & Ice settings are taken outside the soak",
  "ltcplay/fire_ice.py",
  '    if not getattr(cfg, "soak_only", False) or \\\n'
  '            benchhooks.soak_started(env, ppid):',
  '    if True:'),

 ("the soak's schedule is not marked soak_only",
  "packaging/windows/soak.py",
  '                # on a show PC (Pico, 2026-10-23).\n'
  '                "soak_only": True}',
  '                # on a show PC (Pico, 2026-10-23).\n'
  '                "soak_only": False}'),

 ("the soak's Fire & Ice settings are not marked soak_only",
  "packaging/windows/soak.py",
  '              "preroll_s": 0,\n'
  '              "soak_only": True}',
  '              "preroll_s": 0,\n'
  '              "soak_only": False}'),

 # -- the rack screen's banners never squeeze the flame controls (Jeff,
 # 2026-10-24, the show PC): caught by the page's CSS contract and by the
 # page drawn in headless Chromium (selftest.py, RACK_SIZES).
 ("rack screen: the flame groups' rows give their room to banners again",
  "ltcplay/web/remote.html",
  "  grid-auto-rows:minmax(min-content,1fr);gap:8px}",
  "  grid-auto-rows:minmax(0,1fr);gap:8px}"),

 ("rack screen: the flame block shrinks below its content again",
  "ltcplay/web/remote.html",
  ".flames{flex:1 0 auto;min-height:min-content;",
  ".flames{flex:1 1 auto;min-height:0;"),

 ("rack screen: the screens' row may shrink to nothing again",
  "ltcplay/web/remote.html",
  "  grid-template-rows:auto auto minmax(min-content,1fr) auto;",
  "  grid-template-rows:auto auto minmax(0,1fr) auto;"),

 ("rack screen: the page cannot be scrolled again",
  "ltcplay/web/remote.html",
  "html{height:100%;overflow-x:hidden;overflow-y:auto}",
  "html{height:100%;overflow:hidden}"),

 ("rack screen: the banners are never folded",
  "ltcplay/web/remote.html",
  "  if(list.length < 2) return {fold:false, n:list.length, fresh:fresh, keys:keys};",
  "  if(list.length < 99) return {fold:false, n:list.length, fresh:fresh, keys:keys};"),

 ("rack screen: the fold is never drawn",
  "ltcplay/web/remote.html",
  "  setWarn(\"lock-banner\", lockText);\n  renderAlertFold();",
  "  setWarn(\"lock-banner\", lockText);"),

 ("rack screen: Show all never opens the banners",
  "ltcplay/web/remote.html",
  "  $(\"al-body\").hidden = f.fold && !ALERTS_OPEN;",
  "  $(\"al-body\").hidden = f.fold;"),

 ("rack screen: the Abort stop sign scrolls off the window",
  "ltcplay/web/remote.html",
  "#abortbar{position:fixed;z-index:15;",
  "#abortbar{position:absolute;z-index:15;"),

 # The rack screen, 2026-10-26: ABORT a stop sign in the corner, one
 # button per flame group, Arm all and Disarm all one two-way button, and
 # the nav buttons that were cut off (selftest.py, drawn and pressed in
 # headless Chromium).
 ("rack screen: the Abort stop sign shrinks under a finger's 44 px",
  "ltcplay/web/remote.html",
  "  --abort-size:56px;",
  "  --abort-size:30px;"),

 ("rack screen: the page keeps no gutter beside the Abort stop sign",
  "ltcplay/web/remote.html",
  "padding:clamp(6px,1vh,12px) var(--abort-gutter) var(--abort-room)",
  "padding:clamp(6px,1vh,12px) clamp(10px,1.2vw,24px) var(--abort-room)"),

 ("rack screen: the Abort stop sign has no name for a screen reader",
  "ltcplay/web/remote.html",
  '<button id="b-abort" aria-label="Abort. Activate twice to abort."',
  '<button id="b-abort"'),

 ("rack screen: the finger that armed a group disarms it on the way up",
  "ltcplay/web/remote.html",
  '  return downMode === "disarm" ? "disarm" : "";',
  '  return nowMode === "disarm" ? "disarm" : "";'),

 ("rack screen: an armed group's button stays Hold to arm",
  "ltcplay/web/remote.html",
  '  if(g && (g.armed === "armed" || g.wanted)) return "disarm";',
  '  if(g && g.wanted) return "disarm";'),

 ("rack screen: a group asked for has no Disarm",
  "ltcplay/web/remote.html",
  '  if(g && (g.armed === "armed" || g.wanted)) return "disarm";',
  '  if(g && g.armed === "armed") return "disarm";'),

 ("rack screen: a per-group Disarm offered with screen arming off",
  "ltcplay/web/remote.html",
  '  if(!on) return "lamp";\n  if(c === "unknown") return "disarm";',
  '  if(c === "unknown") return "disarm";'),

 ("rack screen: a Disarm button is held back by a stale page",
  "ltcplay/web/remote.html",
  '    if(el.dataset.mode === "disarm"){ el.disabled = false; return; }\n',
  ''),

 ("rack screen: a mix of armed groups shows Arm all",
  "ltcplay/web/remote.html",
  '  if(gs.some(g => g.wanted || (g.armed !== "disarmed" && g.armed !== "held"))) return "disarm";',
  '  if(gs.every(g => g.wanted || (g.armed !== "disarmed" && g.armed !== "held"))) return "disarm";'),

 ("rack screen: Arm all is offered with screen arming off",
  "ltcplay/web/remote.html",
  '  if(stale || !st || !st.arming || !st.arming.enabled) return "disarm";',
  '  if(stale || !st) return "disarm";'),

 ("rack screen: Arm all holds two groups under live flame programming's one at a time",
  "ltcplay/web/remote.html",
  '  if(lf.on === true && lf.one_group_at_a_time !== false) return ONE_GROUP_WORDS;\n',
  ''),

 ("rack screen: Arm all holds a group in its re-arm wait",
  "ltcplay/web/remote.html",
  '    else if(w > 0) out.push({group:i, name:g.name, why:rearmText(w)});',
  ''),

 ("rack screen: Arm all holds while an Abort stands",
  "ltcplay/web/remote.html",
  '  if(st && st.schedule && st.schedule.aborted) return REFUSED_LATCHED;\n',
  ''),

 # Review of #118: the follow-up's guards.
 ("rack screen: letting go of Arm all early does not let go",
  "ltcplay/web/remote.html",
  '  ALL_BTN.addEventListener(ev, () => { if(HOLD && HOLD.all) armStop("let go"); }));',
  '  ALL_BTN.addEventListener(ev, () => {}));'),

 ("rack screen: a stale page never stops an Arm all hold",
  "ltcplay/web/remote.html",
  '    if(HOLD.all && g && (g.armed === "armed" || g.wanted) && as.why === "Already armed or asked for.") continue;',
  '    if(HOLD.all) continue;'),

 ("rack screen: an Arm all heartbeat goes on a stale status",
  "ltcplay/web/remote.html",
  '    const why = holdLapsed();\n    if(why){ armStop(why); return; }',
  '    const why = "";'),

 ("rack screen: Arm all reads Disarm all while it is still arming",
  "ltcplay/web/remote.html",
  'const mode = (HOLD && HOLD.all) ? "arm" : allButtonMode(ST, stale);',
  'const mode = allButtonMode(ST, stale);'),

 ("rack screen: live flame programming turned on mid-hold does not stop Arm all",
  "ltcplay/web/remote.html",
  '  if(HOLD.all){ const no = armAllRefusal(ST); if(no) return no; }\n',
  ''),

 ("rack screen: live flame programming not readable allows Arm all",
  "ltcplay/web/remote.html",
  '  if(lf.error) return LF_UNKNOWN_WORDS;\n',
  ''),

 ("rack screen: an armed group's button reads DISARM, not ARMED",
  "ltcplay/web/remote.html",
  '  el.querySelector(".act").textContent = "Disarm";',
  '  el.querySelector(".word").textContent = "Disarm";'),

 ("rack screen: Enter on Arm all says nothing",
  "ltcplay/web/remote.html",
  ' else if(e.detail === 0 && ALL_BTN.dataset.mode === "arm") armSay(KEY_HOLD_WORDS, 2);',
  ''),

 ("rack screen: Arm all looks like Disarm all",
  "ltcplay/web/remote.html",
  '#b-disarm.armall{border:3px solid #5FE08A;color:#5FE08A;',
  '#b-disarm.armall{'),

 ("rack screen: the Abort sign's line is never shown",
  "ltcplay/web/remote.html",
  '#abortbar button:hover small,#abortbar button:focus-visible small{display:block}',
  '#abortbar button:hover small,#abortbar button:focus-visible small{display:none}'),

 ("rack screen: letting go of a group's Hold to arm early does not let go",
  "ltcplay/web/remote.html",
  'b.addEventListener(ev, () => { if(holding(i) && !HOLD.all) armStop("let go"); }));',
  'b.addEventListener(ev, () => {}));'),

 ("rack screen: a press on a group's Disarm tries a hold",
  "ltcplay/web/remote.html",
  'if(b.dataset.mode === "hold") armStart(i); });',
  'armStart(i); });'),

 ("rack screen: a group whose state is not known shows Hold to arm",
  "ltcplay/web/remote.html",
  '  if(c === "unknown") return "disarm";\n  if(g && (g.armed',
  '  if(c === "unknown") return "hold";\n  if(g && (g.armed'),

 ("rack screen: a page with no contact leaves its buttons as they were",
  "ltcplay/web/remote.html",
  '    if(stale) setArmMode(el, armOn ? "disarm" : "lamp", "unknown");\n',
  ''),

 ("rack screen: Arm all offered while a group is not known",
  "ltcplay/web/remote.html",
  '(g.armed !== "disarmed" && g.armed !== "held")',
  '(g.armed === "armed")'),

 ("rack screen: live flame programming with its one-group setting unsaid allows Arm all",
  "ltcplay/web/remote.html",
  'lf.one_group_at_a_time !== false',
  'lf.one_group_at_a_time === true'),

 ("rack screen: Enter on a group's Disarm does nothing",
  "ltcplay/web/remote.html",
  'if(keyboard) return nowMode === "disarm" ? "disarm" : "";',
  'if(keyboard) return "";'),

 ("rack screen: the idle Abort sign is drawn invisible",
  "ltcplay/web/remote.html",
  '#abortbar.idle:not(.tap1) .oct{background:#7A1A16}',
  '#abortbar.idle:not(.tap1) .oct{background:transparent}'),

 ("rack screen: the Abort sign drops under the page",
  "ltcplay/web/remote.html",
  '#abortbar{position:fixed;z-index:15;',
  '#abortbar{position:fixed;z-index:0;'),

 ("rack screen: the Abort sign moves to the left corner",
  "ltcplay/web/remote.html",
  'right:calc(10px + env(safe-area-inset-right));',
  'left:calc(10px + env(safe-area-inset-right));'),

 ("rack screen: letting go of Arm all never tells the engine",
  "ltcplay/web/remote.html",
  '    if(h.all){ p.end = "out"; p.why = why ? "Hold stopped: " + why : "Hold stopped"; }\n    post("/api/remote/arm-release", {group:p.group}).catch(() => {});',
  '    if(h.all){ p.end = "out"; p.why = why ? "Hold stopped: " + why : "Hold stopped"; continue; }\n    post("/api/remote/arm-release", {group:p.group}).catch(() => {});'),

 # The Abort double tap and the one laser pill (Jeff, 2026-10-26).
 ("rack screen: one tap on the Abort sign aborts",
  "ltcplay/web/remote.html",
  '    if(nowMs - prev.at <= ABORT_TAP_MS || enlarged) return {fire:true, next:null};\n  }\n  return {fire:false, next:{at:nowMs, kind:kind}};',
  '    if(nowMs - prev.at <= ABORT_TAP_MS || enlarged) return {fire:true, next:null};\n  }\n  return {fire:true, next:null};'),

 ("rack screen: the Abort sign's second tap counts long after the first",
  'ltcplay/web/remote.html',
  'const ABORT_TAP_MS = 2000;',
  'const ABORT_TAP_MS = 5000;'),

 ("rack screen: a finger then the mouse aborts",
  "ltcplay/web/remote.html",
  '  if(prev && prev.kind === kind && nowMs >= prev.at){',
  '  if(prev && nowMs >= prev.at){'),

 ("rack screen: the Abort sign's first tap shows nothing",
  "ltcplay/web/remote.html",
  '    $("abortbar").classList.add("tap1");\n',
  ''),

 ("rack screen: the Abort sign never goes back after its first tap",
  "ltcplay/web/remote.html",
  "    ABORT_TIMER = setTimeout(abortTapDone, ABORT_TAP_MS);\n",
  ""),

 ("rack screen: the two laser pills are shown apart again",
  "ltcplay/web/remote.html",
  "  if(!arm || !out) return subs;",
  "  return subs;"),

 ("rack screen: the laser pill takes the arm's colour over a fault",
  "ltcplay/web/remote.html",
  "  const state = (CHIP_RANK[arm.state] || 0) >= (CHIP_RANK[out.state] || 0) ? arm.state : out.state;",
  "  const state = arm.state;"),

 ("rack screen: the laser pill leaves out LINK LOST",
  "ltcplay/web/remote.html",
  '  const fault = out.state === "bad" ? String(out.word || "fault") + ": " : "";',
  '  const fault = "";'),

 ("rack screen: the laser pill's tooltip leaves out who is armed",
  "ltcplay/web/remote.html",
  '    armed.length ? "Armed: " + armed.map(x => x.name).join(", ") + "." : "",',
  '    "",'),

 # One-tap lockout on the Startup page (Jeff, 2026-10-26).
 ("startup page: an L button asks before it locks again",
  "ltcplay/web/remote.html",
  '    const j = await moPress("lock", lockBody(kind, id, on), msg);',
  '    if(!(await confirmBox("Lock out " + name + "?", "", "Lock out", ""))) return;\n    const j = await moPress("lock", lockBody(kind, id, on), msg);'),

 ("startup page: an unlock is sent without saying it is confirmed",
  "ltcplay/web/remote.html",
  " : {kind:kind, id:id, on:false, confirmed:true}; }",
  " : {kind:kind, id:id, on:false}; }"),

 ("startup page: a lock or unlock offers no Undo",
  "ltcplay/web/remote.html",
  '    if(j) lockUndoOffer(kind, id, name, on);',
  ''),

 ("startup page: Undo does the same again instead of the opposite",
  "ltcplay/web/remote.html",
  '  await lockClick(u.kind, u.id, u.name, !u.on);',
  '  await lockClick(u.kind, u.id, u.name, u.on);'),

 ("startup page: Undo never goes away",
  "ltcplay/web/remote.html",
  "  LOCK_UNDO.timer = setTimeout(lockUndoGone, LOCK_UNDO_MS);",
  ""),

 ("startup page: a refused lock offers an Undo",
  "ltcplay/web/remote.html",
  "  }catch(e){ el.textContent = e.message; return null; }",
  "  }catch(e){ el.textContent = e.message; return {}; }"),

 # Review of #122.
 ("startup page: a second tap on L while the first is under way flips it back",
  'ltcplay/web/remote.html',
  '  if(LOCK_BUSY[key] && want === undefined) return;\n',
  ''),

 ("startup page: an L button is free again at once after its answer",
  "ltcplay/web/remote.html",
  "    setTimeout(() => { delete LOCK_BUSY[key]; }, LOCK_QUIET_MS);",
  "    delete LOCK_BUSY[key];"),

 ("startup page: an L tap never asks the engine before it sends",
  'ltcplay/web/remote.html',
  '    try{ fresh = await getJSON("/api/remote/morning", 1500); }catch(e){ fresh = null; }',
  '    fresh = MO;'),

 ("startup page: a machine is unlocked while its flame group is armed",
  "ltcplay/web/remote.html",
  '  if(g && (g.armed === "armed" || g.wanted)) return "Disarm " + m.group + " first, then unlock " + m.name + ".";',
  ''),

 ("startup page: a laser lock's Undo sits under the machine map",
  "ltcplay/web/remote.html",
  '  if(at && bar.previousElementSibling !== at) at.insertAdjacentElement("afterend", bar);',
  ''),

 ("rack screen: the enlarged Abort sign spills out of its gutter",
  "ltcplay/web/remote.html",
  "#abortbar.tap1 button{width:calc(var(--abort-size) + 10px);",
  "#abortbar.tap1 button{width:calc(var(--abort-size) * 2.1);"),

 ("rack screen: a fast double tap on the Abort sign waits for a third",
  'ltcplay/web/remote.html',
  '  if(prev && prev.kind === kind && nowMs >= prev.at){',
  '  if(prev && prev.kind === kind && nowMs >= prev.at && nowMs - prev.at >= 150){'),

 ("rack screen: a held Enter on the Abort sign counts again and again",
  "ltcplay/web/remote.html",
  '$("b-abort").addEventListener("keydown", e => { if(e.repeat && (e.key === "Enter" || e.key === " ")) e.preventDefault(); });',
  ''),

 ("rack screen: a tap leaves keyboard focus on the Abort sign",
  "ltcplay/web/remote.html",
  '  if(kind !== "key") $("b-abort").blur();',
  ''),

 ("rack screen: a late timer turns a good second Abort tap into a first",
  'ltcplay/web/remote.html',
  'abortTapStep(ABORT_TAP, monoNow(), kind, $("abortbar").classList.contains("tap1"));',
  'abortTapStep(ABORT_TAP, monoNow(), kind, false);'),

 ("rack screen: the second Abort tap leaves a hold under way",
  "ltcplay/web/remote.html",
  '  abortTapDone();\n  armStop("Abort");',
  '  abortTapDone();'),

 ("rack screen: the laser pill says output on when it is only allowed",
  "ltcplay/web/remote.html",
  '  if(ls && la.output_permitted) o = "output allowed";',
  '  if(ls && la.output_permitted) o = "output on";'),

 ("rack screen: a laser warning shows only as a colour",
  'ltcplay/web/remote.html',
  '  const warn = out.state === "warn" || (out.state === "ok" && /\\blit\\b/.test(String(out.word || ""))) ? "; " + String(out.word || "check") : "";',
  '  const warn = "";'),

 # Every flame group's "armed n of m machines" (Jeff, 2026-10-26).
 ("rack screen: a group's machine count leaves out the locked-out ones",
  "ltcplay/web/remote.html",
  "  const m = Math.max(0, x.machines - locked);",
  "  const m = x.machines;"),

 ("rack screen: an armed group reads armed 0 of its machines",
  "ltcplay/web/remote.html",
  '  const n = c === "armed" ? m : 0;',
  "  const n = 0;"),

 ("rack screen: the machine count is never drawn",
  "ltcplay/web/remote.html",
  "    const sl = safetyLine(g, c) || machineCountLine(g, c, ST && ST.morning && ST.morning.group_machines), se",
  "    const sl = safetyLine(g, c), se"),

 ("remote: the group machine count never counts a lockout",
  "ltcplay/remote.py",
  "                if x.id in locked:\n                    g[\"locked\"] += 1",
  "                if False:\n                    g[\"locked\"] += 1"),

 # Second review of #122.
 ("startup page: a tap sends what the engine has, not what the operator saw",
  "ltcplay/web/remote.html",
  "    if(on === isLocked(fresh)){",
  "    if(false){"),

 ("startup page: Undo is dropped inside the quiet second",
  "ltcplay/web/remote.html",
  "  if(LOCK_BUSY[key] && want === undefined) return;",
  "  if(LOCK_BUSY[key]) return;"),

 ("startup page: Undo unlocks a machine whose group is armed",
  "ltcplay/web/remote.html",
  '    if(!on && kind === "machine"){\n      const why = unlockRefusal(',
  '    if(!on && kind === "machine" && want === undefined){\n      const why = unlockRefusal('),

 ("startup page: an unlock goes while flamesafe is not connected",
  "ltcplay/web/remote.html",
  "  if(!fl.connected || fl.stale) return",
  "  if(fl.stale) return"),

 ("morning: a machine is unlocked while its flame group is armed",
  "ltcplay/morning.py",
  "                why = self.unlock_refusal(uid)\n",
  "                why = \"\"\n"),

 ("morning: a machine is unlocked while its group is asked for",
  "ltcplay/morning.py",
  "            if g.get(\"name\") == m.group and (g.get(\"armed\") == \"armed\" or\n                                             g.get(\"wanted\")):",
  "            if g.get(\"name\") == m.group and g.get(\"armed\") == \"armed\":"),

 ("morning: a machine is unlocked while flamesafe is stale",
  "ltcplay/morning.py",
  "        if not fl or fl.get(\"stale\") or not fl.get(\"connected\"):",
  "        if not fl:"),

 ("rack screen: a page with no contact keeps an old machine count",
  "ltcplay/web/remote.html",
  '      if(sf){ sf.hidden = false; sf.textContent = "armed: not known"; sf.title = ""; }\n',
  ''),

 ("rack screen: a lock flamesafe does not hold yet is counted out",
  "ltcplay/web/remote.html",
  '  const locked = lk === "ok" ? k : 0;',
  "  const locked = k;"),

 ("remote: a lock counts as held before flamesafe says so",
  "ltcplay/remote.py",
  '"ok" if sv.get("flamesafe_confirmed") is True else',
  '"ok" if True else'),

 ("remote: the machine count reads the default list on a config error",
  "ltcplay/remote.py",
  '            if getattr(m, "config_error", ""):\n                return None\n',
  ''),

 ("rack screen: the laser pill leaves out lit, unconfirmed",
  "ltcplay/web/remote.html",
  ' || (out.state === "ok" && /\\blit\\b/.test(String(out.word || "")))',
  ''),

 ("rack screen: a laser arm not known is only red",
  "ltcplay/web/remote.html",
  '  if(!ls || arm.state === "bad") a = ',
  '  if(!ls) a = '),

 ("rack screen: some lasers armed reads output dark",
  "ltcplay/web/remote.html",
  '  else if(ls) o = "output off";',
  '  else if(ls) o = "output dark";'),

 ("rack screen: a two-safety group gets the machine count too",
  "ltcplay/web/remote.html",
  '  if(!g || c === "unknown" || (g.safeties_up || []).length >= 2) return "";',
  '  if(!g || c === "unknown" || (g.safeties_up || []).length > 2) return "";'),

 ("rack screen: the nav buttons are cut off at the right edge again",
  "ltcplay/web/remote.html",
  "nav{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:8px;flex:0 1 auto;min-width:0;max-width:100%;",
  "nav{display:flex;gap:8px;flex:0 0 auto;"),

 ("rack screen: the Art-Net note is never shown",
  "ltcplay/web/remote.html",
  "  setWarn(\"an-note\", s.artnet_note || \"\");",
  "  setWarn(\"an-note\", \"\");"),

 # Review of #102: the folded line is red and names every fault, a new
 # fault opens it, and the deck alert's Disarm all stays outside it.
 ("rack screen: a lockout is folded as a note",
  "ltcplay/web/remote.html",
  '  LOCAL_FAULT["lock-banner"] = !!lockText;',
  '  LOCAL_FAULT["lock-banner"] = false;'),

 ("rack screen: the cue fault and the deck not heard are folded as a note",
  "ltcplay/web/remote.html",
  '  LOCAL_FAULT["fl-warn"] = !!fmsg;',
  '  LOCAL_FAULT["fl-warn"] = false;'),

 ("rack screen: the folded line does not name the faults",
  "ltcplay/web/remote.html",
  '      (faults.length === 1 ? "Fault: " : faults.length + " faults: ") + faults.join(", "),',
  '      "Fault",'),

 ("rack screen: a new fault only bumps the count",
  "ltcplay/web/remote.html",
  "  const fresh = !!seen && keys.some(k => !seen.has(k));",
  "  const fresh = false;"),

 ("rack screen: a fresh fault never opens the fold",
  "ltcplay/web/remote.html",
  "  if(f.fresh){ if(HOLD) PENDING_OPEN = true; else ALERTS_OPEN = true; }",
  ""),

 ("rack screen: the deck alert's Disarm all is behind Show all",
  "ltcplay/web/remote.html",
  '  $("al-sum-dis").hidden = !f.fold || ALERTS_OPEN ||',
  '  $("al-sum-dis").hidden = true ||'),

 # -- the render's end (2026-10-24): a tiny overrun is zero and no fault;
 # past the end the output is zero always.
 ("flame cues: no overrun tolerance, the end of every show a fault again",
  "ltcplay/fire_ice.py",
  "RENDER_END_TOLERANCE_FRAMES = 2\n",
  "RENDER_END_TOLERANCE_FRAMES = 0\n"),

 ("flame cues: the overrun tolerance is wider than two frames",
  "ltcplay/fire_ice.py",
  "RENDER_END_TOLERANCE_FRAMES = 2\n",
  "RENDER_END_TOLERANCE_FRAMES = 3\n"),

 ("flame cues: any overrun past the render is no fault",
  "ltcplay/fire_ice.py",
  "                if idx >= f.frame_count and over_ms <= \\\n"
  "                        RENDER_END_TOLERANCE_FRAMES * f.step_time_ms:",
  "                if idx >= f.frame_count:"),

 ("flame cues: exactly two frames past the render is a fault",
  "ltcplay/fire_ice.py",
  "                if idx >= f.frame_count and over_ms <= \\\n",
  "                if idx >= f.frame_count and over_ms < \\\n"),

 ("flame cues: past the render's end the last frame is held",
  "ltcplay/fire_ice.py",
  "                    return self._zero(\n"
  "                        f\"the show's render has ended (the timecode is \"",
  "                    data = f.frame(f.frame_count - 1)\n"
  "                    if data: return [data[0]] * 512\n"
  "                    return self._zero(\n"
  "                        f\"the show's render has ended (the timecode is \""),

 # Review of #117 (cold safety review): one mutation per new guard and
 # per guard it found untested.
 # Programming playback's flame cues (Jeff 2026-10-29: the switch "Fire
 # flames while scrubbing" removed). The protections that were the switch's
 # own, now on the always-on path: every move a landing, who may move it,
 # a stale page's route, the deck, the Programming screen's line and fault.
 ("programming flames: a Programming move is no landing at the flame link",
  "ltcplay/flamelink.py",
  "                self._land_gen += 1\n                self.landings += 1\n"
  "                self._land_tc = tc\n                self._land_open = True\n"
  "                self._seek_reset()",
  "                self.landings += 1"),

 ("programming flames: a landing ends on the frame from before the move",
  "ltcplay/flamelink.py",
  "        if tc is None or tc == self._land_tc:\n            self._seek_reset()\n"
  "            return True",
  "        if tc is None:\n            self._seek_reset()\n            return True"),

 ("programming flames: a frame read across a landing goes out with its values",
  "ltcplay/flamelink.py",
  "            if lgen != self._land_gen:",
  "            if False:"),

 ("programming flames: the playhead never finds the flame link",
  "ltcplay/playhead.py",
  "        fn = getattr(link, \"landing\", None)\n        if fn is None:",
  "        fn = None\n        if fn is None:"),

 ("programming flames: Play from lands only before the move",
  "ltcplay/playhead.py",
  "                return self._play_from(show_s, who, screen)\n"
  "            finally:\n                self._landing()",
  "                return self._play_from(show_s, who, screen)\n"
  "            finally:\n                pass"),

 ("programming flames: Back and Ahead land only before the move",
  "ltcplay/playhead.py",
  "                at = s.clock_seek(max(0.0, here + float(seconds)))\n"
  "            finally:\n                self._landing()",
  "                at = s.clock_seek(max(0.0, here + float(seconds)))\n"
  "            finally:\n                pass"),

 ("programming flames: Continue is no landing",
  "ltcplay/playhead.py",
  "        self._landing()\n        try:\n            if cond is not None:\n"
  "                self._refused(cond.resume(who, screen), \"Continue\")\n"
  "            else:\n                s.clock_resume()\n        finally:\n"
  "            self._landing()",
  "        if cond is not None:\n"
  "            self._refused(cond.resume(who, screen), \"Continue\")\n"
  "        else:\n            s.clock_resume()"),

 ("programming flames: the loop is no landing",
  "ltcplay/playhead.py",
  "        self._landing()\n        try:\n            return s.clock_set_loop(a, b)\n"
  "        finally:\n            self._landing()",
  "        return s.clock_set_loop(a, b)"),

 ("programming flames: moves while flames follow need only the Show permission",
  "ltcplay/remote.py",
  "                    users_mod.allowed(u.matrix(), role, \"settings\"):",
  "                    users_mod.allowed(u.matrix(), role, \"show\"):"),

 ("programming flames: moves from a signed-out iPad go while flames follow",
  "ltcplay/remote.py",
  "        if not self.programming_flames_follow():\n            return \"\"",
  "        if True:\n            return \"\""),

 ("programming flames: Jump is not a move that needs the show PC",
  "ltcplay/remote.py",
  "    PROGRAMMING_MOVES = (\"play-from\", \"jump\", \"continue\", \"loop\", \"mark-a\",",
  "    PROGRAMMING_MOVES = (\"play-from\", \"continue\", \"loop\", \"mark-a\","),

 ("programming flames: the press never asks who may move the position",
  "ltcplay/remote.py",
  "            why = self.programming_move_refusal(name, body, ctx)",
  "            why = \"\""),

 ("programming flames: whether flames follow fails open",
  "ltcplay/remote.py",
  "            return ph is not None and bool(ph.applies())\n"
  "        except Exception:                        # noqa: BLE001\n"
  "            return True",
  "            return ph is not None and bool(ph.applies())\n"
  "        except Exception:                        # noqa: BLE001\n"
  "            return False"),

 ("programming flames: a stale page's scrub-flames route is not answered",
  "ltcplay/remote.py",
  "        if name in REMOVED_ROUTES:",
  "        if False:"),

 ("programming flames: the deck does not count Programming playback as running",
  "ltcplay/remote.py",
  "        for view in (self.live_flame_view, self.programming_flames_view):",
  "        for view in (self.live_flame_view,):"),

 ("programming flames: the Programming screen cuts the cue fault short",
  "ltcplay/web/remote.html",
  "  $(\"pf-warn\").textContent = warn;",
  "  $(\"pf-warn\").textContent = warn.slice(0, 80);"),

 ("programming flames: the Programming screen never shows a cue fault",
  "ltcplay/web/remote.html",
  "  const warn = on && tr.programming && fault ?",
  "  const warn = false && fault ?"),

 ("programming flames: the Programming screen never shows the line",
  "ltcplay/web/remote.html",
  "  $(\"pf-line\").hidden = !on;",
  "  $(\"pf-line\").hidden = true;"),

 # The fuel tracker (fuel.py, FUEL.md, Jeff 2026-10-26).
 ("fuel: the flame link's observer is told before the frame went",
  "ltcplay/flamelink.py",
  "        with self._read_lock:\n            tc, values = self.values_now()\n",
  "        with self._read_lock:\n            tc, values = self.values_now()\n"
  "        if self.observer is not None:\n"
  "            self.observer(tc, values, True)\n"),

 ("fuel: an observer that raises reaches the flame link's sender",
  "ltcplay/flamelink.py",
  "            try:\n                obs(tc, values, ok)\n"
  "            except Exception:\n                pass",
  "            obs(tc, values, ok)"),

 ("fuel: the observer is given the values before a zero was applied",
  "ltcplay/flamelink.py",
  "                obs(tc, values, ok)",
  "                obs(tc, b\"\\xff\" * UNIVERSE_SIZE, ok)"),

 ("fuel: one under the fire value counts as flame",
  "ltcplay/fuel.py",
  "            if not any(0 < s <= n and values[s - 1] >= at\n",
  "            if not any(0 < s <= n and values[s - 1] >= at - 1\n"),

 ("fuel: a Showven's purge counts",
  "ltcplay/fuel.py",
  "            if source == \"purge\" and not kc[\"count_purge\"]:",
  "            if False:"),

 ("fuel: a frame that was not sent burns",
  "ltcplay/fuel.py",
  "        if not ok or not values or dt <= 0 or not any(values):",
  "        if not values or dt <= 0 or not any(values):"),

 ("fuel: a gap in the frames counts in full",
  "ltcplay/fuel.py",
  "            min(max(t - self._last_t, 0.0), MAX_FRAME_DT_S)",
  "            max(t - self._last_t, 0.0)"),

 ("fuel: live flame programming counts as a show",
  "ltcplay/fuel.py",
  "        if rehearse or program or (purge and burn):",
  "        if rehearse or (purge and burn):"),

 ("fuel: a failed start counts as a show",
  "ltcplay/fuel.py",
  "                   and ep.get(\"tc_s\", 0.0) >= self.cfg[\"min_show_s\"])",
  "                   and True)"),

 ("fuel: a programming session counts as a show",
  "ltcplay/fuel.py",
  "        is_show = (not ep.get(\"rehearsal\")",
  "        is_show = (True"),

 ("fuel: a locked out unit counts the show",
  "ltcplay/fuel.py",
  "            if is_show and mid in ids and not self._locked(mid):",
  "            if is_show and mid in ids:"),

 ("fuel: an undone refill still counts",
  "ltcplay/fuel.py",
  "        return sorted((r for r in u[\"refills\"] if not r.get(\"undone\")),",
  "        return sorted((r for r in u[\"refills\"]),"),

 ("fuel: shows left from fewer shows than min_shows",
  "ltcplay/fuel.py",
  "               if len(recent) >= cfg[\"min_shows\"] else None)",
  "               if recent else None)"),

 ("fuel: refill soon never said",
  "ltcplay/fuel.py",
  "                                 sl <= cfg[\"refill_soon_shows\"]))",
  "                                 sl < 0))"),

 ("fuel: the starting estimate is ignored",
  "ltcplay/fuel.py",
  "        if cap is None and est is not None:\n",
  "        if False:\n"),

 ("fuel: a refill is not saved at once",
  "ltcplay/fuel.py",
  "        self._note(f\"Fuel: {who or 'someone'} logged a refill of {names}.\")\n"
  "        ok = self.save()",
  "        self._note(f\"Fuel: {who or 'someone'} logged a refill of {names}.\")\n"
  "        ok = True"),

 ("fuel: a refill during a show forgets the flame so far",
  "ltcplay/fuel.py",
  "            burn[\"show\"] += float(self.episode[\"burn\"].get(mid, 0.0))",
  "            pass"),

 ("fuel: fuel.json is written in place",
  "ltcplay/fuel.py",
  "                tmp = f\"{self.path}.{os.getpid()}.tmp\"",
  "                tmp = self.path"),

 ("fuel: a show under way at a restart is lost",
  "ltcplay/fuel.py",
  "                self._end_episode(self._machines(),\n"
  "                                  \"the engine stopped during it\")",
  "                self.episode = None"),

 ("fuel: a counting failure raises out of the pass",
  "ltcplay/fuel.py",
  "        except Exception as e:                    # noqa: BLE001\n"
  "            why = f\"{type(e).__name__}: {e}\"\n            if not self.run_error:",
  "        except ZeroDivisionError as e:\n"
  "            why = f\"{type(e).__name__}: {e}\"\n            if not self.run_error:"),

 ("fuel: a Fireball is tracked",
  "ltcplay/fuel.py",
  "        return [m for m in ms if getattr(m, \"kind\", None) in TRACKED_KINDS]",
  "        return list(ms)"),

 ("fuel: an unknown fuel setting is accepted",
  "ltcplay/fuel.py",
  "    _only(doc, TOP_KEYS, f\"{where}: fuel\")",
  "    pass"),

 ("fuel: an unknown kind setting is accepted",
  "ltcplay/fuel.py",
  "        _only(kd, KIND_KEYS, w)",
  "        pass"),

 ("fuel: an edited settings file is not read again",
  "ltcplay/fuel.py",
  "        if stamp == self._cfg_stamp and self._cfg_stamp is not None:",
  "        if self._cfg_stamp is not None:"),

 ("fuel: the Morning page refuses a fuel block",
  "ltcplay/morning.py",
  "                      \"names\", \"tally\", \"notes\", \"laser_groups\", \"fuel\",\n",
  "                      \"names\", \"tally\", \"notes\", \"laser_groups\",\n"),

 ("fuel: a viewer may log a refill",
  "ltcplay/fuel.py",
  "    if role not in roles:",
  "    if False:"),

 ("fuel: an operator may undo a refill",
  "ltcplay/fuel.py",
  "    return _may(ctx, users, (\"admin\",), \"undo a refill\")",
  "    return _may(ctx, users, REFILL_ROLES, \"undo a refill\")"),

 ("fuel: without sign-in a network device may refill",
  "ltcplay/fuel.py",
  "        if getattr(ctx, \"local\", False):\n            return True, \"\"",
  "        if True:\n            return True, \"\""),

 ("fuel: refill all needs no confirm",
  "ltcplay/fuel.py",
  "            if body.get(\"confirm\") is not True:",
  "            if False:"),

 ("fuel: the engine never watches the flame link",
  "ltcplay/fuel.py",
  "    if link is not None:\n        tr.attach(link)",
  "    pass"),

 ("fuel: closing the engine leaves the tracker running",
  "ltcplay/web.py",
  "            remote.fuel.close()",
  "            pass"),

 ("fuel: the status does not carry the Fuel button's state",
  "ltcplay/remote.py",
  "                out[\"fuel\"] = self.fuel.summary()",
  "                pass"),

 ("fuel: the page never draws the health bar",
  "ltcplay/web/remote.html",
  "<i style=\"width:${pct}%\"></i>",
  "<i></i>"),

 ("fuel: the page offers Refilled to someone who may not",
  "ltcplay/web/remote.html",
  "<button data-refill=\"${esc(u.id)}\" ${f.may_refill ? \"\" : \"disabled\"}",
  "<button data-refill=\"${esc(u.id)}\""),

 ("fuel: a group flamesafe reports not armed burns",
  "ltcplay/fuel.py",
  "                # group's fire slots at zero, so nothing burned.\n                continue",
  "                # group's fire slots at zero, so nothing burned.\n                pass"),

 ("fuel: a locked out unit burns",
  "ltcplay/fuel.py",
  "            if self._locked(m.id):\n                continue\n"
  "            if armed is not None and m.group not in armed:",
  "            if armed is not None and m.group not in armed:"),

 ("fuel: flame is skipped while flamesafe's armed state is unknown",
  "ltcplay/fuel.py",
  "            if armed is not None and m.group not in armed:",
  "            if armed is None or m.group not in armed:"),

 ("fuel: flame counted while flamesafe's state was unknown is not said",
  "ltcplay/fuel.py",
  "                u[\"unsure_s\"] = u.get(\"unsure_s\", 0.0) + dt",
  "                pass"),

 ("fuel: the wall clock times the flame",
  "ltcplay/fuel.py",
  "        q.append((self._clock(), tc,",
  "        q.append((self._wall(), tc,"),

 ("fuel: scrubbing starts a performance",
  "ltcplay/fuel.py",
  "        secs = tc_seconds(tc) if tc is not None and not rehearse else None",
  "        secs = tc_seconds(tc) if tc is not None else None"),

 ("fuel: a short gap in the show timecode splits one show in two",
  "ltcplay/fuel.py",
  "                    self._clock() - self._ep_last_tc > EPISODE_JOIN_S:",
  "                    self._clock() - self._ep_last_tc > 2.0:"),

 ("fuel: a Hold counts toward a show's length",
  "ltcplay/fuel.py",
  "                ep[\"tc_s\"] += min(max(t - self._ep_moved_at, 0.0),\n"
  "                                  TC_STEP_MAX_S)",
  "                ep[\"tc_s\"] += max(t - self._ep_moved_at, 0.0)"),

 ("fuel: a new performance right after the last joins it",
  "ltcplay/fuel.py",
  "                secs < ep[\"last_tc\"] - 1.0:",
  "                False:"),

 ("fuel: the outlier guard is gone",
  "ltcplay/fuel.py",
  "    kept = [c for c in last if m / ratio <= c <= m * ratio]",
  "    kept = last"),

 ("fuel: every refill ever is learned from",
  "ltcplay/fuel.py",
  "    last = [c for c in cycles if c > 0][-n:]\n    if not last:\n"
  "        return None, 0, False",
  "    last = [c for c in cycles if c > 0]\n    if not last:\n"
  "        return None, 0, False"),

 ("fuel: a forgotten Refilled press inflates a young tank",
  "ltcplay/fuel.py",
  "        return min(last), len(last), False",
  "        return median(last), len(last), False"),

 ("fuel: two tanks count as learned",
  "ltcplay/fuel.py",
  "    if len(last) < LEARN_MIN_CYCLES:",
  "    if len(last) < 2:"),

 ("fuel: a much longer tank is not named",
  "ltcplay/fuel.py",
  "                    if d[\"suspect\"]:",
  "                    if False:"),

 ("fuel: only a tank twice the others is named",
  "ltcplay/fuel.py",
  "        if others and c > SUSPECT_RATIO * median(others):",
  "        if others and c > 2.0 * median(others):"),

 ("fuel: a level it cannot vouch for reads green",
  "ltcplay/fuel.py",
  "        if v[\"color\"] == \"green\" and (rough or v[\"not_sure\"]):",
  "        if False:"),

 ("fuel: shows left rounds up",
  "ltcplay/fuel.py",
  "                v[\"shows_left\"] = int(math.floor(left_s / avg))",
  "                v[\"shows_left\"] = int(round(left_s / avg))"),

 ("fuel: percent left rounds up",
  "ltcplay/fuel.py",
  "                     percent=int(math.floor(100.0 * frac)))",
  "                     percent=int(round(100.0 * frac)))"),

 ("fuel: a save leaves the record marked changed",
  "ltcplay/fuel.py",
  "                text = json.dumps(self._doc(), indent=1, sort_keys=True)\n"
  "                self._dirty = False\n",
  "                text = json.dumps(self._doc(), indent=1, sort_keys=True)\n"),

 ("fuel: a save's text is made before it waits for the file",
  "ltcplay/fuel.py",
  "        with self._save_lock:\n            with self._lock:\n"
  "                text = json.dumps(self._doc(), indent=1, sort_keys=True)\n"
  "                self._dirty = False\n"
  "                self._last_save = self._clock()\n            try:",
  "        with self._lock:\n"
  "            text = json.dumps(self._doc(), indent=1, sort_keys=True)\n"
  "            self._dirty = False\n"
  "            self._last_save = self._clock()\n"
  "        with self._save_lock:\n            try:"),

 ("fuel: a failed save is never tried again",
  "ltcplay/fuel.py",
  "                with self._lock:\n                    self._dirty = True\n"
  "                    if not self.save_error:",
  "                with self._lock:\n                    if not self.save_error:"),

 ("fuel: a save failure is journaled on every try",
  "ltcplay/fuel.py",
  "                    if not self.save_error:\n"
  "                        self._note(f\"Fuel: {self.path} could not be written \"",
  "                    if True:\n"
  "                        self._note(f\"Fuel: {self.path} could not be written \""),

 ("fuel: an unreadable fuel.json is written over",
  "ltcplay/fuel.py",
  "            os.replace(self.path, kept)",
  "            pass"),

 ("fuel: a negative burn in fuel.json is read",
  "ltcplay/fuel.py",
  "            not math.isfinite(v) or v < lo:",
  "            not math.isfinite(v):"),

 ("fuel: half a show in fuel.json is read",
  "ltcplay/fuel.py",
  "    if not _is_int(v) or v < lo:",
  "    if False:"),

 ("fuel: closing the engine during a show loses it",
  "ltcplay/fuel.py",
  "                    if self.episode is not None:\n"
  "                        self._end_episode(",
  "                    if False:\n"
  "                        self._end_episode("),

 ("fuel: close waits on a stuck disk for ever",
  "ltcplay/fuel.py",
  "        th.join(timeout)\n        if th.is_alive():",
  "        th.join()\n        if th.is_alive():"),

 ("fuel: a clean close is not marked clean",
  "ltcplay/fuel.py",
  "                    self._clean = True",
  "                    pass"),

 ("fuel: a stop without closing is not a gap",
  "ltcplay/fuel.py",
  "            if not rec[\"clean\"]:",
  "            if False:"),

 ("fuel: dropped frames are not a gap",
  "ltcplay/fuel.py",
  "        if self.dropped != self._dropped_seen:",
  "        if False:"),

 ("fuel: dropped frames are not counted",
  "ltcplay/fuel.py",
  "        if len(q) >= QUEUE_MAX:\n            self.dropped += 1",
  "        if False:\n            self.dropped += 1"),

 ("fuel: frames lost to an error are not a gap",
  "ltcplay/fuel.py",
  "            except Exception:\n                self.gaps += 1",
  "            except ZeroDivisionError:\n                self.gaps += 1"),

 ("fuel: the frame queue is unbounded",
  "ltcplay/fuel.py",
  "        self._q = collections.deque(maxlen=QUEUE_MAX)",
  "        self._q = collections.deque()"),

 ("fuel: the observer sleeps on the flame link's sender thread",
  "ltcplay/fuel.py",
  "        q = self._q\n        if len(q) >= QUEUE_MAX:",
  "        q = self._q\n        time.sleep(0)\n        if len(q) >= QUEUE_MAX:"),

 ("fuel: the observer takes the record lock on the sender thread",
  "ltcplay/fuel.py",
  "        q = self._q\n        if len(q) >= QUEUE_MAX:",
  "        q = self._q\n        with self._lock:\n            pass\n"
  "        if len(q) >= QUEUE_MAX:"),

 ("fuel: the observer writes fuel.json on the sender thread",
  "ltcplay/fuel.py",
  "        q = self._q\n        if len(q) >= QUEUE_MAX:",
  "        q = self._q\n        self.save()\n        if len(q) >= QUEUE_MAX:"),

 ("fuel: only a RuntimeError from the observer is kept off the sender",
  "ltcplay/flamelink.py",
  "            except Exception:\n                pass\n        return ok",
  "            except RuntimeError:\n                pass\n        return ok"),

 ("fuel: refill all ignores the units the page showed",
  "ltcplay/fuel.py",
  "            if expect is not None and sorted(expect) != sorted(by_id):",
  "            if False:"),

 ("fuel: the refill all route does not pass the page's units",
  "ltcplay/fuel.py",
  "            done, text = tracker.refill(\"all\", who, role, expect=shown)",
  "            done, text = tracker.refill(\"all\", who, role, expect=None)"),

 ("fuel: the fuel record closes before the flame link",
  "ltcplay/web.py",
  "        try:\n            _close_before()\n        finally:\n"
  "            remote.fuel.close()",
  "        try:\n            remote.fuel.close()\n        finally:\n"
  "            _close_before()"),

 ("fuel: the Fuel page cannot scroll, so its lower tiles are cut off",
  "ltcplay/web/remote.html",
  "#v-fuel{overflow:auto;display:flex;",
  "#v-fuel{display:flex;"),

 ("fuel: the Fuel button never turns amber",
  "ltcplay/web/remote.html",
  "  $(\"nav-fuel\").classList.toggle(\"soon\", !!(s.fuel && (s.fuel.refill_soon || s.fuel.fault)));",
  ""),

 # The Weather block (weather.py, WEATHER.md; 2026-10-26). Display only;
 # each is a way the wind readout could mislead an operator.
 ("weather: the mesonet's knots are shown as mph",
  "ltcplay/weather.py",
  "    return _in_range({\"speed_mph\": speed * KNOT_MPH,",
  "    return _in_range({\"speed_mph\": speed,"),

 ("weather: km/h from the NWS are shown as mph",
  "ltcplay/weather.py",
  "    if u in (\"km_h-1\", \"km/h\", \"kmh\"):\n        return v * KMH_MPH",
  "    if u in (\"km_h-1\", \"km/h\", \"kmh\"):\n        return v"),

 ("weather: degrees round down to the compass point",
  "ltcplay/weather.py",
  "    return COMPASS[int(math.floor((float(deg) % 360) / 22.5 + 0.5)) % 16]",
  "    return COMPASS[int(math.floor((float(deg) % 360) / 22.5)) % 16]"),

 ("weather: a measured reading over 45 min old still shows its number",
  "ltcplay/weather.py",
  "    if age > dead_s:\n        return \"none\", age",
  "    if False:\n        return \"none\", age"),

 ("weather: a measured reading over 20 min old is not STALE",
  "ltcplay/weather.py",
  "    return (\"stale\" if age > stale_s else \"ok\"), max(0.0, age)",
  "    return \"ok\", max(0.0, age)"),

 # The forecast judged on when this PC got it, and the measured backups
 # (Jeff, 2026-10-28, field report item 15).
 ("weather: a forecast this PC has not got for over 1 h is not STALE",
  "ltcplay/weather.py",
  "        if got_age > fstale_s:",
  "        if False:"),

 ("weather: a forecast the NWS has not updated for 18 h is not STALE",
  "ltcplay/weather.py",
  "        if issued_age > fissued_s:",
  "        if False:"),

 ("weather: a forecast this PC has not got for 6 h still shows its number",
  "ltcplay/weather.py",
  "got_age > fdead_s or \\\n            issued_age > fidead_s:",
  "False or \\\n            issued_age > fidead_s:"),

 ("weather: a forecast the NWS has not updated for 36 h still shows its number",
  "ltcplay/weather.py",
  "got_age > fdead_s or \\\n            issued_age > fidead_s:",
  "got_age > fdead_s or \\\n            False:"),

 ("weather: a forecast timed in the future is shown",
  "ltcplay/weather.py",
  "        future = got - now > 15 * 60 or d[\"issued\"] - now > 15 * 60",
  "        future = False"),

 ("weather: forecast_every_min longer than the stale limit is taken",
  "ltcplay/weather.py",
  "    if cfg[\"forecast_every_min\"] >= cfg[\"forecast_stale_hours\"] * 60:",
  "    if False:"),

 ("weather: the forecast is judged on its issue time again",
  "ltcplay/weather.py",
  "        got = d.get(\"fetched_at\")\n",
  "        got = d[\"issued\"]\n"),

 ("weather: a stalled station blanks the wind though a backup is current",
  "ltcplay/weather.py",
  "    usable = [r for r in rows if r[\"st\"] in (\"ok\", \"stale\")]",
  "    usable = [r for r in rows[:1] if r[\"st\"] in (\"ok\", \"stale\")]"),

 ("weather: a higher STALE reading is hidden by a current lower one",
  "ltcplay/weather.py",
  "    pick = max(usable, key=",
  "    pick = max([r for r in usable if r[\"st\"] == \"ok\"] or usable,"
  " key="),

 ("weather: the lowest reading leads",
  "ltcplay/weather.py",
  "    pick = max(usable, key=",
  "    pick = min(usable, key="),

 ("weather: a STALE headline is not marked",
  "ltcplay/weather.py",
  "        if pick[\"st\"] == \"stale\" and others:\n            text += (f\" STALE: over",
  "        if False:\n            text += (f\" STALE: over"),

 ("weather: a wind speed of 900 mph is shown",
  "ltcplay/weather.py",
  "        if v is not None and not 0 <= v <= MAX_MPH:",
  "        if False:"),

 ("weather: a millisecond obsTime is read as seconds",
  "ltcplay/weather.py",
  "        if valid is None or not 1e9 <= valid < 1e11:",
  "        if valid is None or valid < 1e9:"),

 ("weather: a backup station's reading is shown as if it were UTALP's",
  "ltcplay/weather.py",
  "\"backup\" if pick[\"i\"] > 0 else \"ok\")",
  "\"ok\")"),

 ("weather: an hourly airport is held to the 5 minute feed's limits",
  "ltcplay/weather.py",
  "hourly=name != \"mesonet\")",
  "hourly=False)"),

 ("weather: station_limits are ignored",
  "ltcplay/weather.py",
  "    own = (cfg.get(\"station_limits\") or {}).get(station) or {}",
  "    own = {}"),

 ("weather: an HTTP error is said as no answer",
  "ltcplay/weather.py",
  "        txt = (f\"{short} did not answer: {err}\" if s.get(\"offline\") else",
  "        txt = (f\"{short} did not answer: {err}\" if True else"),

 ("weather: the page shows a backup reading without amber",
  "ltcplay/web/remote.html",
  "cls:lv === \"backup\" ? \"stale\" : lv,",
  "cls:lv,"),

 ("weather: no current data gives no reason",
  "ltcplay/weather.py",
  "                         + \". \".join(_cap(r[\"why\"]) for r in rows) + \".\")",
  "                         + \"\")"),

 ("weather: a backup reading does not say why the station was passed over",
  "ltcplay/weather.py",
  "        if ahead:\n            text += (\" Using the backup because \"",
  "        if False:\n            text += (\" Using the backup because \""),

 ("weather: a station that stopped reporting is said as not answering",
  "ltcplay/weather.py",
  "    if err and not s.get(\"answered\"):",
  "    if True:"),

 ("weather: a backup that disagrees with the station is not said",
  "ltcplay/weather.py",
  "        if others:\n            text += (\" \" + \"; \".join(",
  "        if False:\n            text += (\" \" + \"; \".join("),

 ("weather: aviationweather.gov's knots are shown as mph",
  "ltcplay/weather.py",
  "    return _in_range({\"speed_mph\": to_mph(wspd, unit),",
  "    return _in_range({\"speed_mph\": wspd,"),

 ("weather: aviationweather.gov's oldest report is used",
  "ltcplay/weather.py",
  "        if best is None or best[0] is None or valid > best[0]:",
  "        if best is None or best[0] is None or valid < best[0]:"),

 ("weather: the backup airports are never asked",
  "ltcplay/weather.py",
  "    for sid in cfg.get(\"backup_stations\") or ():",
  "    for sid in ():"),

 ("weather: sources 30 mph apart do not disagree",
  "ltcplay/weather.py",
  "        if hi[1] - lo[1] > cfg[\"disagree_mph\"]:",
  "        if False:"),

 ("weather: a Wind Chill Warning shows as a wind alert",
  "ltcplay/weather.py",
  "    return (\"wind\" in n and \"chill\" not in n) or n in FIRE_EVENTS",
  "    return (\"wind\" in n) or n in FIRE_EVENTS"),

 ("weather: an alert that has ended is still shown",
  "ltcplay/weather.py",
  "                if a.get(\"ends\") is None or a[\"ends\"] > now]",
  "                ]"),

 ("weather: no internet is never said",
  "ltcplay/weather.py",
  "    if tried and all(x.get(\"error\") and x.get(\"offline\") for x in tried):",
  "    if False:"),

 ("weather: a failed source is hammered every minute",
  "ltcplay/weather.py",
  "        return min(self.every[name], RETRY_FIRST_S * 2 ** max(0, fails - 1))",
  "        return RETRY_FIRST_S"),

 ("weather: a restart forgets the saved values",
  "ltcplay/weather.py",
  "                s[\"data\"] = ent[\"data\"]",
  "                pass"),

 ("weather: another station's saved reading is shown as this one's",
  "ltcplay/weather.py",
  "            if isinstance(ent, dict) and ent.get(\"key\") == self._key(name) \\",
  "            if isinstance(ent, dict) \\"),

 ("weather: a slow drip answer is waited out",
  "ltcplay/weather.py",
  "                if clock() > deadline:",
  "                if False:"),

 ("weather: an unknown setting is quietly ignored",
  "ltcplay/weather.py",
  "    for k in block:\n        if k not in DEFAULTS:\n            raise",
  "    block = {k: v for k, v in block.items() if k in DEFAULTS}\n"
  "    for k in block:\n        if k not in DEFAULTS:\n            raise"),

 ("weather: another venue's engine shows Lehi's wind",
  "ltcplay/weather.py",
  "    if not default_on and not has_block(folder):\n        return None",
  "    if False:\n        return None"),

 ("weather: the page keeps old numbers when the engine goes quiet",
  "ltcplay/web/remote.html",
  "  if(heardAgoS > dead) return one(",
  "  if(false) return one("),

 ("weather: the page drops the STALE word",
  "ltcplay/web/remote.html",
  "(lv === \"stale\" ? \"STALE. \" : \"\")",
  "\"\""),

 ("weather: the engine's route answers no weather",
  "ltcplay/remote.py",
  "            return 200, (w.view() if w is not None\n",
  "            return 200, (w.view() if False\n"),

 # Open BEYOND, Calibrate all lasers (windowfront.py, lasertest.py,
 # morning.py, remote.py; Jeff, 2026-10-26).
 ("open beyond: any program's window counts as BEYOND's",
  "ltcplay/windowfront.py",
  "    return bool(name) and any(fnmatch.fnmatch(name, p) for p in IMAGES)",
  "    return bool(name)"),

 ("open beyond: a BEYOND dialog is picked over its main window",
  "ltcplay/windowfront.py",
  "            if not api.owner(h):\n                return h",
  "            if True:\n                return h"),

 ("open beyond: a minimized BEYOND is not restored",
  "ltcplay/windowfront.py",
  "        if api.minimized(hwnd):\n            api.restore(hwnd)",
  "        if False:\n            api.restore(hwnd)"),

 ("open beyond: no input attach before SetForegroundWindow",
  "ltcplay/windowfront.py",
  "        if other and other != me and api.attach(me, other, True):",
  "        if False:"),

 # Review of PR #125 (2026-10-26), fixed forward.
 ("open beyond: a key is pressed again to take the foreground (P2-1)",
  "ltcplay/windowfront.py",
  "        api.set_foreground(hwnd)\n        api.raise_async(hwnd)",
  "        api.alt_tap()\n        api.set_foreground(hwnd)\n"
  "        api.raise_async(hwnd)"),

 ("open beyond: a hung BEYOND's window is moved anyway (P2-2)",
  "ltcplay/windowfront.py",
  "    if hung:\n        return False, NOT_RESPONDING",
  "    if False:\n        return False, NOT_RESPONDING"),

 ("open beyond: the restore waits on BEYOND's thread (P2-2)",
  "ltcplay/windowfront.py",
  "        self.u.ShowWindowAsync(hwnd, SW_RESTORE)",
  "        self.u.ShowWindow(hwnd, SW_RESTORE)"),

 ("open beyond: a press waits on a hung BEYOND for ever (P2-2)",
  "ltcplay/windowfront.py",
  "        t.join(self.timeout_s)",
  "        t.join()"),

 ("open beyond: presses pile up threads on a hung BEYOND (P2-2)",
  "ltcplay/windowfront.py",
  "            if self._busy:\n                return False, BUSY",
  "            if False:\n                return False, BUSY"),

 ("open beyond: the press calls the window code with no time limit (P2-2)",
  "ltcplay/remote.py",
  "            fn = windowfront.OPENER",
  "            fn = windowfront.open_beyond"),

 ("open beyond: allowed on Hold or between shows (P2-3)",
  "ltcplay/remote.py",
  "        why = self.show_live_refusal() or self._open_beyond_state() or \\",
  "        why = self.show_live_refusal() or \\"),

 ("open beyond: allowed while a purge, a laser test or a hold is on (P2-3)",
  "ltcplay/remote.py",
  "        why = self.show_live_refusal() or self._open_beyond_state() or \\\n"
  "            self.rig_busy()",
  "        why = self.show_live_refusal() or self._open_beyond_state() or \\\n"
  "            ''"),

 ("open beyond: refused while the calibration test is on",
  "ltcplay/remote.py",
  "                if armed and m.lasers.on:",
  "                if m.lasers.on:"),

 ("last show sign out: a laser test on does not hold it back (P2-5)",
  "ltcplay/remote.py",
  "                if armed and m.lasers.on:",
  "                if False:"),

 ("open beyond: a screen's hold to arm does not count as busy (P2-3)",
  "ltcplay/remote.py",
  "                if self._holds:\n                    return \"a hold to arm",
  "                if False:\n                    return \"a hold to arm"),

 ("calibrate all: a show-state check that fails lets it carry on (P2-4)",
  "ltcplay/lasertest.py",
  "                    why = f\"the show state could not be read ({e})\"",
  "                    why = \"\""),

 ("calibrate all: a lockout that cannot be read counts as unlocked (P2-4)",
  "ltcplay/lasertest.py",
  "            except Exception:                    # noqa: BLE001\n"
  "                locked = True\n            if locked:\n"
  "                skipped.append(",
  "            except Exception:                    # noqa: BLE001\n"
  "                locked = False\n            if locked:\n"
  "                skipped.append("),

 ("last show sign out: runs while a laser or flame group is armed (P2-5)",
  "ltcplay/remote.py",
  "            if self.rig_busy(armed=True):\n                return None",
  "            if self.rig_busy(armed=False):\n                return None"),

 ("last show sign out: an armed laser does not hold it back (P2-5)",
  "ltcplay/remote.py",
  "                if armed and m.laser_arm.armed_ids():",
  "                if False:"),

 ("last show sign out: an armed or wanted flame group does not hold it back (P2-5)",
  "ltcplay/remote.py",
  "                        if g.get(\"armed\") == \"armed\" or g.get(\"wanted\"):",
  "                        if False:"),

 ("last show sign out: a rig state that cannot be read lets it go (P2-5)",
  "ltcplay/remote.py",
  "            return f\"the rig's state could not be read ({type(e).__name__})\"",
  "            return \"\""),

 ("last show sign out: the users' own beat never starts (P2-6)",
  "ltcplay/web.py",
  "        r.users.start()\n",
  "        pass\n"),

 ("last show sign out: the beat checks nothing (P2-6)",
  "ltcplay/users.py",
  "            while not self._beat_stop.wait(every_s):\n"
  "                self.after_last_show_tick()",
  "            while not self._beat_stop.wait(every_s):\n"
  "                pass"),

 ("last show sign out: a blank minutes box is saved as off (P2-7)",
  "ltcplay/web/remote.html",
  "  if(!/^[0-9]{1,4}$/.test(t)) return null;",
  "  if(!/^[0-9]{1,4}$/.test(t)) return 0;"),

 ("last show sign out: Save sends minutes it could not read (P2-7)",
  "ltcplay/web/remote.html",
  "  if(lastShow === null){",
  "  if(false){"),

 ("last show sign out: a night closed at its reset loses its sign-out (P2-8)",
  "ltcplay/remote.py",
  "            if seen is not None and seen[\"night\"] != str(m.date):",
  "            if False:"),

 ("last show sign out: a Start now on an unscheduled night counts (P2-8)",
  "ltcplay/remote.py",
  "            if ends and scheduled:",
  "            if ends:"),

 ("last show sign out: a restart's assumed end counts as a show (P2-8)",
  "ltcplay/remote.py",
  "            ends = [s.ended_at for s in slots\n"
  "                    if getattr(s, \"ended_at\", None) is not None]",
  "            ends = [m.last_end] if m.last_end is not None else []"),

 ("open beyond: ltc serve never hands on BEYOND's host (P2-9)",
  "ltcplay/web.py",
  "        httpd.remote.beyond_host = getattr(_bey_cfg, \"host\", None)",
  "        pass"),

 ("open beyond: the input attach is never let go",
  "ltcplay/windowfront.py",
  "                api.attach(attached[0], attached[1], False)",
  "                pass"),

 ("open beyond: a refused foreground reads as done",
  "ltcplay/windowfront.py",
  "        if api.foreground() == hwnd:\n            return True, \"\"",
  "        if True:\n            return True, \"\""),

 ("open beyond: the taskbar button is not flashed when Windows refuses",
  "ltcplay/windowfront.py",
  "        flashed = bool(api.flash(hwnd))",
  "        flashed = False"),

 ("open beyond: BEYOND on another machine is looked for here",
  "ltcplay/windowfront.py",
  "    if host is not None and not beyondwatch.on_this_pc(host):",
  "    if False:"),

 ("open beyond: works from a device on the show network",
  "ltcplay/remote.py",
  "        if not ctx.local:\n            return 403, {\"error\": \"Open BEYOND works",
  "        if False:\n            return 403, {\"error\": \"Open BEYOND works"),

 ("open beyond: works while a show is live or an Abort stands",
  "ltcplay/remote.py",
  "        if why:\n            return 409, {\"error\": f\"Open BEYOND is not allowed",
  "        if False:\n            return 409, {\"error\": f\"Open BEYOND is not allowed"),

 ("calibrate all: a locked-out laser is not skipped and named",
  "ltcplay/lasertest.py",
  "            if locked:\n                skipped.append(",
  "            if False:\n                skipped.append("),

 ("calibrate all: a laser that is not armed is not skipped and named",
  "ltcplay/lasertest.py",
  "            if not self.is_armed(lid):\n                skipped.append(",
  "            if False:\n                skipped.append("),

 ("calibrate all: a show going live part way does not stop it",
  "ltcplay/lasertest.py",
  "                if why:\n                    stopped = why\n                    break",
  "                if False:\n                    stopped = why\n                    break"),

 ("calibrate all: Calibration off leaves tests on",
  "ltcplay/lasertest.py",
  "            for lid in self.order:\n                ok, _t = self.set(lid, False",
  "            for lid in list(self.order)[:1]:\n                ok, _t = self.set(lid, False"),

 ("calibrate all: the engine ignores the all press",
  "ltcplay/morning.py",
  "            if body.get(\"all\") is True:",
  "            if False:"),

 ("calibrate all: the engine does not ask the show state before each laser",
  "ltcplay/morning.py",
  "                    still_ok=self._live_or_broken if on else None)",
  "                    still_ok=None)"),

 ("calibrate all: the page's button sends nothing",
  "ltcplay/web/remote.html",
  "$(\"mo-cal-on\").addEventListener(\"click\", () => moPress(\"laser-test\", {all:true, on:true}, \"mo-lmsg\"));",
  "$(\"mo-cal-on\").addEventListener(\"click\", () => {});"),

 # Sign everyone out after the last show (users.py, remote.py; Jeff,
 # 2026-10-26).
 ("last show sign out: off by default",
  "ltcplay/users.py",
  "LAST_SHOW_SIGNOUT_DEFAULT_MIN = 10",
  "LAST_SHOW_SIGNOUT_DEFAULT_MIN = 0"),

 ("last show sign out: no grace after the last show",
  "ltcplay/users.py",
  "            due = float(info[\"ended\"]) + mins * 60",
  "            due = float(info[\"ended\"])"),

 ("last show sign out: a later sign-in is signed out again that night",
  "ltcplay/users.py",
  "        if night == self._last_show_done or self._wall() < due:",
  "        if self._wall() < due:"),

 ("last show sign out: a sign-in after the show ended is signed out (P2-5)",
  "ltcplay/users.py",
  "                if s.get(\"wall_at\", 0.0) < ended]",
  "                if s.get(\"wall_at\", 0.0) < due]"),

 ("last show sign out: a sign-in after it is signed out after a restart",
  "ltcplay/users.py",
  "                if s.get(\"wall_at\", 0.0) < ended]",
  "                ]"),

 ("last show sign out: never signs anyone out",
  "ltcplay/users.py",
  "            del self._sessions[t]\n            self._note(\"sign-out\", s[\"name\"],\n                       reason=f\"after the last show",
  "            pass\n            self._note(\"sign-out\", s[\"name\"],\n                       reason=f\"after the last show"),

 ("last show sign out: 0 does not turn it off",
  "ltcplay/users.py",
  "        if not mins or fn is None:\n            return\n        try:\n            info = fn()",
  "        if fn is None:\n            return\n        try:\n            info = fn()"),

 ("last show sign out: the deck's user is not checked",
  "ltcplay/users.py",
  "            self._drop_idle(now)\n            self._drop_after_last_show()\n            best = None",
  "            self._drop_idle(now)\n            best = None"),

 ("last show sign out: runs while a show is on Hold",
  "ltcplay/remote.py",
  "            if getattr(m, \"state\", None) in (\"SHOW\", \"PAUSED\", \"HOLD\") or \\",
  "            if getattr(m, \"state\", None) in (\"SHOW\", \"PAUSED\") or \\"),

 ("last show sign out: runs while a show is still to come",
  "ltcplay/remote.py",
  "                if not m.waiting():\n                    return dict(self._last_show_seen)",
  "                if True:\n                    return dict(self._last_show_seen)"),

 ("last show sign out: runs while an Abort stands",
  "ltcplay/remote.py",
  "            if self.show_live_refusal():\n                return None\n            cond = getattr(svc, \"conductor\", None)",
  "            if False:\n                return None\n            cond = getattr(svc, \"conductor\", None)"),

 ("last show sign out: the engine never tells the users",
  "ltcplay/web.py",
  "        r.users.last_show = r.last_show_over\n",
  "        pass\n"),

 # Third review of #126 (deck-followup).
 ("morning: an undeclared multi-safety group is purged again (third review of #126, P2-1)",
  "ltcplay/morning.py",
  "        if name in fireball_groups or name in gflame_groups:\n            continue\n        if _kind_for(name) != \"gflame\":",
  "        if len(g.get(\"safeties\") or []) > 1:\n            continue\n        if name in fireball_groups or name in gflame_groups:\n            continue\n        if _kind_for(name) != \"gflame\":"),

 ("morning: a row name alone makes a multi-safety group purgeable",
  "ltcplay/morning.py",
  "        if name.strip().lower() in GFLAME_ROW_NAMES and \\\n                len(g.get(\"safeties\") or []) <= 1:",
  "        if name.strip().lower() in GFLAME_ROW_NAMES:"),

 ("Rack screen: a failed Stop disarm still says Stopped. (third review of #126, P2-2)",
  "ltcplay/web/remote.html",
  "  if(f && f.disarmed === false) return STOP_DISARM_FAILED;\n",
  ""),

 ("Rack screen: Stop's answer is not read",
  "ltcplay/web/remote.html",
  "el.textContent = (r.ok || (j && j.failed)) ? stopWords(j) :",
  "el.textContent = (r.ok || (j && j.failed)) ? \"Stopped.\" :"),

 ("Rack screen: the Stop confirm does not say it disarms the flames",
  "ltcplay/web/remote.html",
  "\"Stop playback and disarm every flame group. Every laser",
  "\"Stop playback. Every laser"),

 ("deck: a failed Stop disarm is a quiet done",
  "ltcplay/streamdeck.py",
  "            if isinstance(flames, dict) and flames.get(\"disarmed\") is False:",
  "            if False:"),

 ("deck: a failed Stop disarm reads ENGINE FAULT, not DISARM FAILED",
  "ltcplay/streamdeck.py",
  "            self.fault_face = (\"DISARM\", \"FAILED\") if disarm_failed else \\",
  "            self.fault_face = (\"ENGINE\", \"FAULT\") if disarm_failed else \\"),

 ("deck: key 0 never draws the fault face",
  "ltcplay/streamdeck.py",
  "            face = list(getattr(self.conductor, \"fault_face\", None) or\n",
  "            face = list(None or\n"),

 ("deck: cat walk and catwalk are not the cat-walk (third review of #126, P2-3)",
  "ltcplay/streamdeck.py",
  "DECK_ALIASES = {\"cat walk\": \"cat-walk\", \"catwalk\": \"cat-walk\"}",
  "DECK_ALIASES = {}"),

 ("deck: two groups differing only by case both get Jeff's label",
  "ltcplay/streamdeck.py",
  "    return order, [DECK_LABELS.get(k, n) if known.index(k) == i else n\n",
  "    return order, [DECK_LABELS.get(k, n) if True else n\n"),

 ("deck: a json name written exactly is not preferred over a case twin",
  "ltcplay/streamdeck.py",
  "        if g.strip() in stripped:\n            i = stripped.index(g.strip())",
  "        if False:\n            i = stripped.index(g.strip())"),

 ("deck: a json name that could be either case twin is taken as the first",
  "ltcplay/streamdeck.py",
  "        elif known.count(_known(g)) > 1:",
  "        elif False:"),

 ("deck: a case twin's own name reads as a look-alike",
  "ltcplay/streamdeck.py",
  "            if j != i and _known(other) == _known(names[i]) and \\",
  "            if False and _known(other) == _known(names[i]) and \\"),

 ("deck: case twins are not journaled",
  "ltcplay/streamdeck.py",
  "    for t in _case_twins(list(names)):\n",
  "    for t in []:\n"),

 ("deck: the no-key list is off by one (third review of #126)",
  "ltcplay/streamdeck.py",
  "        return list(self.key_order[GROUP_KEY_LIMIT:])",
  "        return list(self.key_order[GROUP_KEY_LIMIT + 1:])"),

 ("deck: ltcplay_deck.json names must match flamesafe's case exactly",
  "ltcplay/streamdeck.py",
  "        if _known(g) not in known:\n",
  "        if g not in names:\n"),

 ("deck: groups ltcplay_deck.json does not list follow flamesafe's order",
  "ltcplay/streamdeck.py",
  "    order += [i for i in d_order if i not in order]\n",
  "    order += [i for i in range(len(names)) if i not in order]\n"),

 ("Stop: the engine's Stop presses the scheduler's Abort (third review of #126)",
  "ltcplay/web.py",
  "        reason = f\"{who or 'Someone'} pressed Stop on the {screen}\"\n",
  "        reason = f\"{who or 'Someone'} pressed Stop on the {screen}\"\n        _s = getattr(self.server, \"schedule\", None)\n        if _s is not None:\n            _s.operator_press(\"abort\", who, screen, confirmed=True)\n"),

 ("Stop: a refused live-show Stop sends its flame half first",
  "ltcplay/web.py",
  "        if route in LIVE_SHOW_REFUSED and self._scheduled_show_live() and \\\n                not (route == \"/api/stop\" and self._halt_asked()):\n",
  "        if route == \"/api/stop\":\n            self._stop_flames(body if isinstance(body, dict) else {})\n        if route in LIVE_SHOW_REFUSED and self._scheduled_show_live() and \\\n                not (route == \"/api/stop\" and self._halt_asked()):\n"),


 # Jeff's ruling (2026-10-26): the deck's Stop is a complete halt, during
 # a scheduled show too; the show ends with no latch.
 ("halt: the deck's Stop is refused during a scheduled show again",
  "ltcplay/web.py",
  "        if route in LIVE_SHOW_REFUSED and self._scheduled_show_live() and \\\n                not (route == \"/api/stop\" and self._halt_asked()):\n",
  "        if route in LIVE_SHOW_REFUSED and self._scheduled_show_live():\n"),

 ("halt: the Rack screen's Stop (no halt) is taken during a show",
  "ltcplay/web.py",
  "        return self._local() and isinstance(body, dict) and \\\n            body.get(\"halt\") is True",
  "        return self._local() and isinstance(body, dict)"),

 ("halt: a halt during a show does not end the show",
  "ltcplay/remote.py",
  "            if svc is not None and state in (\"SHOW\", \"PAUSED\"):\n                try:\n                    r = svc.operator_press(\"stop-show\"",
  "            if False:\n                try:\n                    r = svc.operator_press(\"stop-show\""),

 ("halt: a halt ends the show with the scheduler's Abort",
  "ltcplay/remote.py",
  "                    r = svc.operator_press(\"stop-show\", who, screen,",
  "                    r = svc.operator_press(\"abort\", who, screen,"),

 ("halt: a halt leaves the lasers armed",
  "ltcplay/remote.py",
  "            fn = getattr(m, \"lasers_disarm_now\", None)\n            if fn is not None:\n                try:\n                    r = fn(f\"Stop held",
  "            fn = None\n            if fn is not None:\n                try:\n                    r = fn(f\"Stop held"),

 ("halt: playback stops before the scheduler ends the show", "ltcplay/web.py",
  "        out = self._halt(body)\n        try:\n            out[\"stopped\"] = c.stop()\n",
  "        _s = c.stop()\n        out = self._halt(body)\n        try:\n            out[\"stopped\"] = _s\n"),

 ("halt: the scheduler's Stop latches like an Abort",
  "ltcplay/schedule_service.py",
  "            if ev.kind == sch.ABORT:\n                plan.append((\"Abort\", \"abort\", self._ABORT_EFFECTS))",
  "            if ev.kind in (sch.ABORT, sch.STOP_SHOW):\n                plan.append((\"Abort\", \"abort\", self._ABORT_EFFECTS))"),

 ("halt: the scheduler's Stop needs no confirm",
  "ltcplay/schedule.py",
  "    if ev.kind in (ABORT, END_NIGHT, STOP_SHOW) and not ev.confirmed:",
  "    if ev.kind in (ABORT, END_NIGHT) and not ev.confirmed:"),

 ("halt: the scheduler's Stop is refused with no operator chosen",
  "ltcplay/schedule.py",
  "ALWAYS_TAKEN = frozenset((ABORT, HOLD_ON, STOP_SHOW))",
  "ALWAYS_TAKEN = frozenset((ABORT, HOLD_ON))"),

 ("halt: the scheduler's Stop is refused while the show is paused",
  "ltcplay/schedule.py",
  "    STOP_SHOW: frozenset((SHOW, PAUSED)),",
  "    STOP_SHOW: frozenset((SHOW,)),"),

 ("halt: the deck's Stop hold does nothing during a scheduled show",
  "ltcplay/streamdeck.py",
  "STOP_MODES = (\"stop\", \"show\", \"unknown\")",
  "STOP_MODES = (\"stop\", \"unknown\")"),

 ("halt: the deck's Stop does not ask for a halt",
  "ltcplay/streamdeck.py",
  "        return self._press(\"stop\", who, screen, path=\"/api/stop\", halt=True)",
  "        return self._press(\"stop\", who, screen, path=\"/api/stop\")"),

 ("halt: a show the scheduler did not end is no deck fault",
  "ltcplay/streamdeck.py",
  "            if isinstance(show, dict) and show.get(\"ok\") is False:",
  "            if False:"),

 ("halt: a failed laser disarm is no deck fault",
  "ltcplay/streamdeck.py",
  "            if isinstance(lasers, dict) and lasers.get(\"disarmed\") is False:",
  "            if False:"),



 # Review of #130 (fix forward).
 ("halt: playback stops before the conductor's fade (review of #130, 5)",
  "ltcplay/web.py",
  "        if h.get(\"show_ended\"):\n            svc = getattr(self.server, \"schedule\", None)",
  "        if False:\n            svc = getattr(self.server, \"schedule\", None)"),

 ("halt: the flames are not first (review of #130, 6)",
  "ltcplay/remote.py",
  "            try:\n                f_ok, f_text = self._disarm_now(who, screen, \"Stop\")\n            except Exception as e:               # noqa: BLE001\n                f_ok, f_text = False, f\"{type(e).__name__}: {e}\"\n",
  "            f_ok, f_text = None, \"\"\n"),

 ("halt: the laser arm is not told a press is under way",
  "ltcplay/remote.py",
  "        if arm is not None:\n            try:\n                arm.press_begin()\n            except Exception:                    # noqa: BLE001\n                arm = None\n        try:\n            # 1. The flames first",
  "        if arm is not None:\n            try:\n                pass\n            except Exception:                    # noqa: BLE001\n                arm = None\n        try:\n            # 1. The flames first"),

 ("halt: screen arm holds survive a halt",
  "ltcplay/remote.py",
  "                self.cancel_holds(f\"{who or 'someone'} held Stop on the \"",
  "                (lambda *a: 0)(f\"{who or 'someone'} held Stop on the \""),

 ("halt: the purge selection and laser tests survive a halt",
  "ltcplay/remote.py",
  "                    if fn is not None:\n                        fn(arg)\n",
  "                    pass\n"),

 ("halt: a step that raises stops the rest",
  "ltcplay/remote.py",
  "                except Exception as e:           # noqa: BLE001\n                    l_ok, l_text = False, (f\"The laser disarm did NOT go \"",
  "                except ZeroDivisionError as e:   # noqa: BLE001\n                    l_ok, l_text = False, (f\"The laser disarm did NOT go \""),

 ("halt: a show that ended by itself is a failure (review of #130, 8)",
  "ltcplay/remote.py",
  "                if not s_ok and now_state not in (\"SHOW\", \"PAUSED\"):",
  "                if False:"),

 ("halt: the journal line hides a failed step (review of #130, 4)",
  "ltcplay/remote.py",
  "        self._journal(who, screen, \"stop\", \"fault\" if bad else \"done\",",
  "        self._journal(who, screen, \"stop\", \"done\","),

 ("halt: the scheduler's Stop line claims the disarms again (review of #130, 4)",
  "ltcplay/schedule.py",
  "        f\"{_pressed(ev, 'Stop')} during show {n}. Flame cues zeroed and \"",
  "        f\"{_pressed(ev, 'Stop')} during show {n}. Every flame group and laser disarmed. Flame cues zeroed and \""),

 ("halt: Stop on the last show goes straight to closing (review of #130, 2)",
  "ltcplay/schedule.py",
  "        _abort_effects(n), abort=True)\n\n\ndef _end_night",
  "        _abort_effects(n))\n\n\ndef _end_night"),

 ("deck: only the first failed Stop step is said (review of #130, 3)",
  "ltcplay/streamdeck.py",
  "            text = \"playback stopped, but \" + \"; and \".join(\n                t for _k, t in fails)",
  "            text = \"playback stopped, but \" + fails[0][1]"),

 ("deck: Stop waits on the press queue again (review of #130, 7)",
  "ltcplay/streamdeck.py",
  "        if name == \"stop\":\n            # Stop never waits behind another press either",
  "        if False:\n            # Stop never waits behind another press either"),

 ("deck: the key never reads STOP ALL with the scheduler unknown",
  "ltcplay/streamdeck.py",
  "            elif self.start_key_mode() == \"unknown\" or \\\n                    self._idle_stop():\n",
  "            elif False:\n"),


 # Second review of #130 (fix forward).
 ("halt: the conductor's fade is not waited for (second review of #130, 7)",
  "ltcplay/web.py",
  "                    cond.wait_idle(HALT_FADE_WAIT_S)\n",
  "                    pass\n"),

 ("halt: the conductor's calls are not flushed before playback stops",
  "ltcplay/web.py",
  "                svc.flush_conductor(HALT_FADE_WAIT_S)\n",
  "                pass\n"),

 ("halt: the lasers are not blanked at once (second review of #130, 7)",
  "ltcplay/remote.py",
  "                b_ok, b_text = self._lasers_dark_now(\n                    f\"Stop held on the {screen}\")",
  "                b_ok, b_text = None, \"\""),

 ("halt: a failed laser blank is not reported (second review of #130, 3)",
  "ltcplay/remote.py",
  "                if not b_ok:\n                    out[\"failed\"].append(\"laser blank\")",
  "                if False:\n                    out[\"failed\"].append(\"laser blank\")"),

 ("deck: the alert face hides HOLD TO STOP (second review of #130, 1)",
  "ltcplay/streamdeck.py",
  "        if stop_frac * STOP_HOLD_S >= STOP_FACE_AFTER_S:\n",
  "        if False:\n"),

 ("deck: the Morning page's FIRE swallows Stop during a show (second review of #130, 2)",
  "ltcplay/streamdeck.py",
  "                elif morning and not self._stop_wins():\n",
  "                elif morning:\n"),

 ("deck: the Morning page's face hides Stop during a show",
  "ltcplay/streamdeck.py",
  "        elif self.morning_open() and not self._stop_wins():\n",
  "        elif self.morning_open():\n"),

 ("deck: a purge group's own arm makes Stop win over FIRE",
  "ltcplay/streamdeck.py",
  "            skip = set() if purge else self._purge_own()\n",
  "            skip = set()\n"),

 ("deck: an idle armed hold is a refused Start Now (second review of #130, 6)",
  "ltcplay/streamdeck.py",
  "        if mode == \"start\" and not self._idle_stop():\n",
  "        if mode == \"start\":\n"),

 ("deck: the engine's failed list is ignored (second review of #130, 5)",
  "ltcplay/streamdeck.py",
  "            for step in doc.get(\"failed\") or []:\n",
  "            for step in []:\n"),

 ("deck: a failed laser blank is not said",
  "ltcplay/streamdeck.py",
  "            if isinstance(blank, dict) and blank.get(\"ok\") is False:\n",
  "            if False:\n"),

 ("deck: a slow Stop reads ENGINE FAULT (second review of #130, 5)",
  "ltcplay/streamdeck.py",
  "                (\"CHECK\", \"RACK\") if slow else (\"ENGINE\", \"FAULT\")",
  "                (\"ENGINE\", \"FAULT\") if slow else (\"ENGINE\", \"FAULT\")"),

 ("deck: a Stop that times out is called unreachable",
  "ltcplay/streamdeck.py",
  "        except TimeoutError:\n            # Only a timeout AFTER the request was sent",
  "        except ZeroDivisionError:\n            # Only a timeout AFTER the request was sent"),

 ("deck: one failed scheduler poll flips the key (second review of #130, 6)",
  "ltcplay/streamdeck.py",
  "                if self._sched_misses >= STATE_MISSES:\n",
  "                if True:\n"),

 ("deck: one failed run poll flips the key",
  "ltcplay/streamdeck.py",
  "                if self._play_misses >= STATE_MISSES:\n",
  "                if True:\n"),

 ("deck: an engine with no scheduler leaves the key unknown (second review of #130, 6)",
  "ltcplay/streamdeck.py",
  "            elif isinstance(running, dict) and \\\n                    running.get(\"_http_status\") == 404 and answered(play):",
  "            elif False:"),

 ("deck: an HTTP error reads as no answer at all",
  "ltcplay/streamdeck.py",
  "            return {\"_http_status\": e.code}\n",
  "            return None\n"),

 ("conductor: Stop leaves a waiting show start in the line (second review of #130, 4)",
  "ltcplay/schedule_service.py",
  "        if ev.kind == sch.STOP_SHOW and calls:\n            # Stop: a show start",
  "        if False:\n            # Stop: a show start"),

 ("conductor: Stop drops a Hold too, like an Abort",
  "ltcplay/schedule_service.py",
  "    STOP_DROPS = (\"show_starting\", \"start_show\")",
  "    STOP_DROPS = (\"show_starting\", \"start_show\", \"hold\")"),

 ("conductor: Stop's drop takes calls decided after it",
  "ltcplay/schedule_service.py",
  "                    if c.method in methods and c.seq < before_seq]",
  "                    if c.method in methods]"),


 # Third review of #130 (fix forward).
 ("deck: a run going does not make Stop win over FIRE (third review of #130, 1)",
  "ltcplay/streamdeck.py",
  "        return self.start_key_mode() in STOP_MODES or self._idle_stop()",
  "        return self.start_key_mode() in (\"show\", \"unknown\") or self._idle_stop()"),

 ("deck: a group the purge wants but the operator armed first makes Stop win (third review of #130, 2)",
  "ltcplay/streamdeck.py",
  "            return set(self._purge_armed) | set(self.purge_wanted())",
  "            return set(self._purge_armed)"),

 ("deck: a connect timeout reads as the engine took the Stop (third review of #130, 3)",
  "ltcplay/streamdeck.py",
  "        except urllib.error.URLError as e:\n            # urllib wraps everything up to sending the request (connecting,",
  "        except urllib.error.URLError as e:\n            if \"timed out\" in str(e):\n                return False, \"took it\", {\"timeout\": True}\n            # urllib wraps everything up to sending the request (connecting,"),

 ("deck: a stale CHECK RACK outranks an engine that stopped answering",
  "ltcplay/streamdeck.py",
  "            if gone and face == [\"CHECK\", \"RACK\"]:",
  "            if False:"),

 ("deck: an HTTP error from Stop is not read for its failed steps (third review of #130, 4)",
  "ltcplay/streamdeck.py",
  "        errored = name == \"stop\" and not ok and isinstance(doc, dict) and \\",
  "        errored = False and \\"),

 ("deck: an HTTP error answer is not marked as the engine's",
  "ltcplay/streamdeck.py",
  "            doc.setdefault(\"_http_status\", e.code)\n",
  "            pass\n"),

 ("web: a playback stop that raises loses the halt's steps (third review of #130, 4)", "ltcplay/web.py",
  "            out[\"stopped\"] = c.stop()\n        except Exception as e:       # noqa: BLE001\n",
  "            out[\"stopped\"] = c.stop()\n        except ZeroDivisionError as e:  # noqa: BLE001\n"),


 # Review of #130, low items.
 ("deck: a playback stop that alone failed reads CHECK RACK (review of #130, low 1)",
  "ltcplay/streamdeck.py",
  "                (\"PLAYBACK\", \"FAILED\") if playback_only else \\",
  "                (\"CHECK\", \"RACK\") if playback_only else \\"),

 ("deck: a failed playback stop counts as a failed disarm",
  "ltcplay/streamdeck.py",
  "            told = {\"flames\", \"lasers\", \"show\", \"laser blank\",\n                    \"playback stop\"}",
  "            told = {\"flames\", \"lasers\", \"show\", \"laser blank\"}"),

 ("deck: a playback stop failure does not say the flames were disarmed",
  "ltcplay/streamdeck.py",
  "        if errored and playback_only:\n",
  "        if False:\n"),

 ("web: the playback stop's error overwrites the halt's (review of #130, low 2)", "ltcplay/web.py",
  "            out[\"error\"] = (f\"{out['error']}; and the playback \"\n                            f\"stop: {said}\" if out.get(\"error\")\n                            else said)",
  "            out[\"error\"] = said"),

 ("web: the playback stop's own error is not said apart", "ltcplay/web.py",
  "            out[\"playback_error\"] = said\n",
  ""),

 ("deck: live flames do not count as running (review of #130, low 3)",
  "ltcplay/streamdeck.py",
  "        return self._any_flame_armed(purge=False) or \\\n            self._flames_live() is not False",
  "        return self._any_flame_armed(purge=False)"),

 ("remote: deck-input never says live flames are on",
  "ltcplay/remote.py",
  "            if isinstance(v, dict) and v.get(\"on\") is True:\n                live = True\n",
  "            if False:\n                live = True\n"),

 ("deck: the deck never reads flames_live",
  "ltcplay/streamdeck.py",
  "        v = last[0].get(\"flames_live\")",
  "        v = None"),

 ("deck: FIRE held when a show starts stays FIRE (review of #130, low 4)",
  "ltcplay/streamdeck.py",
  "        if self._prev_keys[TOP_START] and self._key0_fire and \\\n                self._stop_wins():",
  "        if self._prev_keys[TOP_START] and self._key0_fire and \\\n                False:"),

 ("deck: FIRE held when a show starts needs a second press to Stop",
  "ltcplay/streamdeck.py",
  "            self._fire_release(\"Stop wins: something is running\")\n            self._stop_hold.press(now)",
  "            self._fire_release(\"Stop wins: something is running\")"),


 # Review of #132.
 ("deck: FIRE turns into Stop only while PurgeFire still fires (review of #132, 1)",
  "ltcplay/streamdeck.py",
  "        if self._prev_keys[TOP_START] and self._key0_fire and \\\n                self._stop_wins():",
  "        if self._prev_keys[TOP_START] and getattr(self.purge_fire, \"firing\", False) and \\\n                self._stop_wins():"),

 ("deck: key 0 is never remembered as FIRE",
  "ltcplay/streamdeck.py",
  "                    self._key0_fire = True\n                    self._fire_press(now)",
  "                    self._fire_press(now)"),

 ("deck: the FIRE memory outlives the key's release",
  "ltcplay/streamdeck.py",
  "                self._key0_fire = False\n                self._fire_release(\"the key came up\")",
  "                self._fire_release(\"the key came up\")"),

 ("deck: a show the halt did not stop reads ENGINE FAULT (review of #132, 2)",
  "ltcplay/streamdeck.py",
  "                (\"SHOW NOT\", \"STOPPED\") if show_live else \\",
  "                (\"ENGINE\", \"FAULT\") if show_live else \\"),

 ("deck: a deselected purge group lagging in flamesafe's status makes Stop win (review of #132, 3)",
  "ltcplay/streamdeck.py",
  "                         now - at < DISARM_SETTLE_S}",
  "                         False}"),

 ("deck: live flames not known read as off (review of #132, 4)",
  "ltcplay/streamdeck.py",
  "            self._flames_live() is not False",
  "            self._flames_live() is True"),

 ("deck: a live flame view that raises reads as off",
  "ltcplay/streamdeck.py",
  "        try:\n            v = fn()\n        except Exception:                        # noqa: BLE001\n            return None\n",
  "        try:\n            v = fn()\n        except Exception:                        # noqa: BLE001\n            return False\n"),

 ("remote: a live flame view that raises or errs says off (review of #132, 4)",
  "ltcplay/remote.py",
  "                if live is False:\n                    live = None",
  "                pass"),


 # Delta review of #132.
 ("deck: the settle window forgives every deck disarm again (delta review of #132, A)",
  "ltcplay/streamdeck.py",
  "            if not purge and self._key0_fire:\n",
  "            if not purge:\n                self._purge_deselected = {i: at for i, at in enumerate(self._disarmed_at) if at is not None}\n"),

 ("deck: the settle window works with FIRE not held",
  "ltcplay/streamdeck.py",
  "            if not purge and self._key0_fire:\n",
  "            if not purge:\n"),

 ("deck: a deselect disarm is not recorded for the settle window",
  "ltcplay/streamdeck.py",
  "                self._purge_deselected[i] = self._clock()\n",
  ""),

 ("deck: a reconnect keeps the FIRE memory (delta review of #132, B)",
  "ltcplay/streamdeck.py",
  "        self._key0_fire = False\n        self._fire_release(\"the deck reconnected\")",
  "        self._fire_release(\"the deck reconnected\")"),

 ("deck: latched with only a group armed, key 0 is no Stop (delta review of #132, D1)",
  "ltcplay/streamdeck.py",
  "                    if self.start_key_mode() in STOP_MODES or \\\n                            self._idle_stop():\n                        self._stop_hold.press(now)\n                elif k in self._key_group:",
  "                    if self.start_key_mode() in STOP_MODES:\n                        self._stop_hold.press(now)\n                elif k in self._key_group:"),

 ("deck: an acknowledge on the Morning page never becomes Stop (delta review of #132, D2)",
  "ltcplay/streamdeck.py",
  "                    elif morning:\n                        # An acknowledge on the Morning page that Stop does",
  "                    elif False:\n                        # An acknowledge on the Morning page that Stop does"),

 ("make a show file writes over an existing show file", "ltcplay/cli.py",
  "            if os.path.lexists(out):\n                continue\n            try:\n                os.link(tmp, out)\n                return out",
  "            try:\n                os.replace(tmp, out)\n                return out"),

 ("make a show file is not refused while a show file runs", "ltcplay/remote.py",
  "        if getattr(c, \"_starting\", False) or \\\n                (sess is not None and getattr(sess, \"running\", False)):",
  "        if False:"),

 ("make a show file is not refused while a scheduled show is live", "ltcplay/remote.py",
  "        if self._show_live():\n            return 409, (\"A scheduled show is live. Make the show file \"",
  "        if False:\n            return 409, (\"A scheduled show is live. Make the show file \""),

 ("make a show file writes beside the show folder, not where the chooser lists", "ltcplay/remote.py",
  "                made = make_show_file(folder, folder,",
  "                made = make_show_file(folder, os.path.dirname(folder),"),

 ("make a show file lists the launcher folder's renders, not the folder served", "ltcplay/remote.py",
  "                made = make_show_file(folder, folder,",
  "                made = make_show_file(settings_mod.folder(), folder,"),

 ("make a show file never chooses the new file", "ltcplay/remote.py",
  "        else:\n            self._save_selected_show(name)",
  "        else:\n            pass"),

 ("ltc init writes over an existing show file again", "ltcplay/cli.py",
  "        out = write_new_show_file(settings_mod.folder(),\n                                  starter_base(args.show_dir), doc)",
  "        out = os.path.join(settings_mod.folder(),\n                           starter_base(args.show_dir) + \"_timeline.json\")\n        with open(out, \"w\") as fh:\n            json.dump(doc, fh, indent=2)"),

 ("Getting started never offers Make a show file", "ltcplay/web/remote.html",
  "  $(\"su-make\").hidden = !makeOffered(su);",
  "  $(\"su-make\").hidden = true;"),

 ("Getting started always offers Make a show file", "ltcplay/web/remote.html",
  "  $(\"su-make\").hidden = !makeOffered(su);",
  "  $(\"su-make\").hidden = false;"),

 ("Getting started offers Make a show file with no renders", "ltcplay/web/remote.html",
  "  return !!su && su.show_files === 0 && su.show_folder_renders > 0;",
  "  return !!su && su.show_files === 0;"),

 ("the setup view never counts the renders for Make a show file", "ltcplay/remote.py",
  "                \"show_folder_renders\": self._renders_in(folder) if not files",
  "                \"show_folder_renders\": 0 if not files"),

 ("make a show file writes an empty show file when no render reads", "ltcplay/cli.py",
  "    if not doc[\"cues\"]:\n        raise ValueError(f\"None of the",
  "    if False:\n        raise ValueError(f\"None of the"),

 ("make a show file without hard links truncates a file that appeared", "ltcplay/cli.py",
  "os.O_WRONLY | os.O_CREAT | os.O_EXCL |",
  "os.O_WRONLY | os.O_CREAT | os.O_TRUNC |"),

 ("make a show file takes a second press while one is made", "ltcplay/remote.py",
  "        if not self._new_show_lock.acquire(blocking=False):",
  "        if False:"),

 ("make a show file leaves its temporary file behind", "ltcplay/cli.py",
  "    finally:\n        try:\n            os.unlink(tmp)\n        except OSError:\n            pass\n\n\n# Said with every",
  "    finally:\n        pass\n\n\n# Said with every"),

 ("make a show file presents a guessed frame rate as fact", "ltcplay/cli.py",
  "    note = (f\"Check the frame rate: it was set to {doc['fps']:g} fps \"\n            f\"because {why}. {FPS_CHECK}\")",
  "    note = f\"{doc['fps']:g} fps.\""),

 ("make a show file drops the frame rate warning from the reply", "ltcplay/remote.py",
  "                f\"{made['rate_note']} The \\\"Check the show file\\\" button \"",
  "                f\"The \\\"Check the show file\\\" button \""),

 ("make a show file reads a [bracketed] folder name as a pattern again", "ltcplay/timeline.py",
  "    files = [os.path.join(show_dir, n) for n in fseq_names(show_dir)]",
  "    import glob\n    files = sorted(glob.glob(os.path.join(show_dir, \"*.fseq\")))"),

 ("the show file list reads a [bracketed] folder name as a pattern again", "ltcplay/web.py",
  "        pattern = os.path.join(glob.escape(self.folder), \"*.json\")",
  "        pattern = os.path.join(self.folder, \"*.json\")"),

 ("the show length cross-check reads a [bracketed] folder name as a pattern", "ltcplay/clock.py",
  "    for path in sorted(glob.glob(_os.path.join(glob.escape(folder),\n",
  "    for path in sorted(glob.glob(_os.path.join(folder,\n"),

 ("the audio interface watch reads a [bracketed] folder name as a pattern", "ltcplay/audiowatch.py",
  "    for p in sorted(glob.glob(os.path.join(glob.escape(folder),\n",
  "    for p in sorted(glob.glob(os.path.join(folder,\n"),

 ("a marker CSV reads a [bracketed] folder name as a pattern", "ltcplay/markers.py",
  "    files = sorted(glob.glob(os.path.join(glob.escape(show_dir),\n",
  "    files = sorted(glob.glob(os.path.join(show_dir,\n"),

 ("a marker CSV misses renders named .FSEQ", "ltcplay/markers.py",
  "                                          \"*.[fF][sS][eE][qQ]\")))\n",
  "                                          \"*.fseq\")))\n"),

 ("make a show file misses renders named .FSEQ", "ltcplay/timeline.py",
  "    return sorted(n for n in names if n.lower().endswith(\".fseq\") and",
  "    return sorted(n for n in names if n.endswith(\".fseq\") and"),

 ("make a show file is not asked again just before the write", "ltcplay/cli.py",
  "    if before_write is not None:\n        before_write()\n",
  ""),

 ("make a show file chooses the new file under a show that started", "ltcplay/remote.py",
  "        late = self._new_show_refusal()",
  "        late = None"),

 ("the folder card's Make a show file does nothing", "ltcplay/web/remote.html",
  "$(\"fo-make\").addEventListener(\"click\", makeShowFile);\n",
  ""),

 ("Getting started's Make a show file does nothing", "ltcplay/web/remote.html",
  "$(\"su-make\").addEventListener(\"click\", async () => {\n  location.hash = \"programming\";",
  "$(\"su-make-off\") && (async () => {\n  location.hash = \"programming\";"),

 ("the folder card's Check the show file does nothing", "ltcplay/web/remote.html",
  "$(\"fo-check\").addEventListener(\"click\", async () => {",
  "$(\"fo-check-off\") && (async () => {"),

 ("Make a show file's button returns before it posts", "ltcplay/web/remote.html",
  "async function makeShowFile(){\n  if(!(await confirmBox(",
  "async function makeShowFile(){\n  return null;\n  if(!(await confirmBox("),

 ("Getting started hides Make a show file after deciding to offer it", "ltcplay/web/remote.html",
  "  $(\"su-make\").hidden = !makeOffered(su);\n",
  "  $(\"su-make\").hidden = !makeOffered(su);\n  $(\"su-make\").hidden = true;\n"),

 ("Getting started's Make a show file never makes one",
  'ltcplay/web/remote.html',
  '  // Show folder card (fo-made), where this press has gone.\n  await makeShowFile();',
  '  // Show folder card (fo-made), where this press has gone.\n  const j = null;'),

 ("Make a show file does not choose the new file on the page", "ltcplay/web/remote.html",
  "    await loadShowFiles(j.file);\n",
  "    await loadShowFiles();\n"),

 ('the rack screen never says the timecode runs at another rate',
  'ltcplay/remote.py',
  '        if not (count_off or speed_off or drop_off):\n            return ""',
  '        if True:\n            return ""'),

 ('the rack screen calls 29.97 against 30 a mismatch before it is measured',
  'ltcplay/remote.py',
  '        speed_off = sure and abs(rate - fps) > 0.01',
  '        speed_off = abs(rate - fps) > 0.01'),

 ('the rack screen ignores a drop-frame mismatch',
  'ltcplay/remote.py',
  '        drop_off = drop != tdrop',
  '        drop_off = False'),

 ("ltc init no longer asks for the frame rate to be checked", "ltcplay/cli.py",
  "    print(f\"Check the frame rate: it was set to {fps:g} fps because {why}. \"\n          f\"{FPS_CHECK}\")",
  "    pass"),

 ("make a show file's temporary name grows with the folder name again", "ltcplay/cli.py",
  "    tmp = os.path.join(dest, f\"~{os.urandom(4).hex()}.new\")",
  "    tmp = os.path.join(dest, f\".{base}_timeline.{os.getpid()}.new\")"),

 ("the Getting started card pushes Disarm all off the screen when opened", "ltcplay/web/remote.html",
  "  if($(\"v-show\").hidden) return;\n  const key = fitKey();",
  "  return;\n  const key = fitKey();"),

 ("the open Getting started card is moved below the flame groups again", "ltcplay/web/remote.html",
  "#setup.folded .subody{display:none}",
  "#setup.folded .subody{display:none}\n@media (max-height:900px){#setup:not(.folded){order:9}}"),

 ("Make a show file is only in the steps, not the card's header", "ltcplay/web/remote.html",
  "<button id=\"su-make\" hidden>Make a show file</button><button class=\"ghost\" id=\"su-more\"",
  "<button class=\"ghost\" id=\"su-more\""),

 ("the rate note states a rate after the timecode stops", "ltcplay/remote.py",
  "        if not cs.get(\"running\") or rate is None or fps is None or \\\n                not arriving:",
  "        if not cs.get(\"running\") or rate is None or fps is None:"),

 ("the rate note states the show file's rate as the incoming one", "ltcplay/remote.py",
  "        if count_off or speed_off:",
  "        if True:"),

 ("ltc init calls an asked-for 30 fps the default", "ltcplay/cli.py",
  "    why = \"it was asked for\" if args.fps is not None else \"that is the default\"",
  "    why = (\"it was asked for\" if str(fps) != str(INIT_FPS)\n           else \"that is the default\")"),

 ("the Getting started fit runs on every poll and resets the steps' scroll", "ltcplay/web/remote.html",
  "  if(!force && key === FIT_KEY) return;",
  ""),

 ("the open Getting started steps may have no room at all", "ltcplay/web/remote.html",
  "const FIT_SLACK = 30, STEPS_MIN = 120;",
  "const FIT_SLACK = 30, STEPS_MIN = 0;"),

 ("the short-window rules are never used for the Getting started card", "ltcplay/web/remote.html",
  "  if(fitOver() > 0.5) root.classList.add(\"setupafter\");",
  "  root.classList.remove(\"tight\");\n  if(fitOver() > 0.5) root.classList.add(\"setupafter\");"),

 ("Show the steps leaves the page where it was", "ltcplay/web/remote.html",
  "    try{ $(\"setup\").scrollIntoView({block: below || after ? \"start\" : \"nearest\"}); }catch(e){}",
  ""),

 ("Make a show file is 32 px tall on a short window again", "ltcplay/web/remote.html",
  "#setup .surow #su-make{min-height:40px}",
  ""),

 ("the open steps never move after the flame groups", "ltcplay/web/remote.html",
  "      card.style.maxHeight = \"\";\n      stepsHome(false);",
  "      fitCap();"),

 ("warnings that come with the Getting started steps open are never fitted", "ltcplay/web/remote.html",
  "    card.classList.contains(\"folded\"), SETUP_KEY, Math.round(cr.top + scrollY), dis, arm]);",
  "    card.classList.contains(\"folded\"), SETUP_KEY, 0, 0, 0]);"),

 ("the card's 40 percent cap starves the open steps again", "ltcplay/web/remote.html",
  "    if(cr.height < minH - 0.5) card.style.maxHeight = minH + \"px\";",
  ""),

 ("the short-window rules make the controls taller on a short window", "ltcplay/web/remote.html",
  ":where(html.tight) .ctl button{min-height:44px}",
  "html.tight .ctl button{min-height:44px}"),


 # aim slots seen (2026-10-28): the start lines, the status frame, the
 # rack screen's aim line and alert, the Startup page's file; fuel burns
 # a Showven only on its ignition
 ("aim seen: flamesafe's status frame leaves out the aim slots",
  "flamesafe/composer.py",
  "                \"aim_slots\": sorted(g.aim),",
  "                \"aim_slots\": [],"),

 ("aim seen: flamesafe does not journal its aim slots at start",
  "flamesafe/__main__.py",
  "    journal.event(\"config\", aim_line(cfg.groups, argv[0]))\n",
  ""),

 ("aim seen: flamesafe's aim line leaves out the file",
  "flamesafe/__main__.py",
  "            f\"the group is armed), read from {where}\")",
  "            f\"the group is armed)\")"),

 ("aim seen: the flame link does not journal its aim slots at start",
  "ltcplay/fire_ice.py",
  "    if journal is not None:\n        journal(aim_sentence(",
  "    if journal is None:\n        journal(aim_sentence("),

 ("aim seen: the rack screen is not given the engine's aim slots",
  "ltcplay/web.py",
  "            httpd.remote.flame_aim = (",
  "            httpd.remote.flame_aim_unused = ("),

 ("aim seen: the status view drops flamesafe's aim slots",
  "ltcplay/remote.py",
  "                           \"aim_slots\": _ints(g.get(\"aim_slots\")),",
  "                           \"aim_slots\": None,"),

 ("aim seen: the lists are always taken to agree",
  "ltcplay/remote.py",
  "        agree = None if theirs is None or mine is None else theirs == mine",
  "        agree = None if theirs is None or mine is None else True"),

 ("aim seen: a difference raises no alert",
  "ltcplay/remote.py",
  "                a = self.alerts.raise_(\"aim\", text)",
  "                a = None"),

 ("aim seen: the alert is raised again on every poll",
  "ltcplay/remote.py",
  "            if text == self._aim_said:\n                return\n",
  "            if text == self._aim_said:\n                pass\n"),

 ("aim seen: agreeing again never clears the alert",
  "ltcplay/remote.py",
  "                    self.alerts.clear(id=aid, action=\"Confirm\",",
  "                    self.alerts.clear(id=-1, action=\"Confirm\","),

 ("aim seen: loaded only needs flamesafe's list",
  "ltcplay/remote.py",
  "                        \"loaded\": bool(fs) and fs == lt,",
  "                        \"loaded\": bool(fs),"),

 ("aim seen: sent shows what was asked",
  "ltcplay/remote.py",
  "                        \"sent\": at(g.get(\"sent_fire\"))}",
  "                        \"sent\": at(g.get(\"commanded_fire\"))}"),

 ("aim seen: the card always says loaded",
  "ltcplay/web/remote.html",
  "  let t = \"Aim \" + j(a.slots) + \": \" + (a.loaded ? \"loaded\" :",
  "  let t = \"Aim \" + j(a.slots) + \": \" + (true ? \"loaded\" :"),

 ("aim seen: the card never shows the aim line",
  "ltcplay/web/remote.html",
  "    ae.hidden = !al;",
  "    ae.hidden = true;"),

 ("aim seen: the Startup page error leaves out the flamesafe file",
  "ltcplay/morning.py",
  "    return f\" (checked against flamesafe's config {os.path.abspath(path)})\"",
  "    return \"\""),

 ("fuel: a Showven burns on any fire slot again (angle, speed)",
  "ltcplay/fuel.py",
  "                       for s in burn_slots(m)):",
  "                       for s in m.fire):"),

 ("fuel: a Showven's angle counts as flame again",
  "ltcplay/fuel.py",
  "    skip = {c for x in saf for c in (x - 5, x - 4)}",
  "    skip = {c for x in saf for c in (x - 4,)}"),

 ("fuel: a Showven's preset and duration stop counting",
  "ltcplay/fuel.py",
  "    out = [f for f in fire if f not in skip]",
  "    out = [f for f in fire if f not in skip and f + 3 not in saf]"),

 ("fuel: a Showven with no safety slot counts its angle",
  "ltcplay/fuel.py",
  "            skip = {run[0], run[1]}",
  "            skip = {run[1]}"),

 ("aim seen: the aim alert holds a show at showtime",
  "ltcplay/alerthold.py",
  "        return [a for a in self.alerts.open() if a.kind not in DISPLAY_ONLY]",
  "        return list(self.alerts.open())"),

 ("aim seen: the aim alert sounds the booth",
  "ltcplay/alerts.py",
  "        if a.kind in DISPLAY_ONLY:\n            a.booth_ok, a.booth = None, \"display only: no booth note\"\n            return\n",
  ""),

 ("aim seen: the aim alert shares the flames kind",
  "ltcplay/remote.py",
  "                a = self.alerts.raise_(\"aim\", text)",
  "                a = self.alerts.raise_(\"flames\", text)"),

 ("aim seen: the card says held as disarmed",
  "ltcplay/web/remote.html",
  "c === \"held\" ? \"; held\" : ",
  ""),
 # The Programming playhead on the show audio (playhead.py, Jeff
 # 2026-10-28, field report items 7 and 8).
 ("Play from starts the show audio at the top, not at the point asked", "ltcplay/clock.py",
  '            self._send(("play", role, sample, 0, self._token))',
  '            self._send(("play", role, 0, 0, self._token))'),

 ("Play from starts the timecode at 00:00:00:00 whatever the point", "ltcplay/clock.py",
  '            frame = cue.get("first_frame", 0)\n        if last is None or',
  '            frame = 0\n        if last is None or'),

 ("a move while playing never restarts the audio", "ltcplay/clock.py",
  "        if self._device_ok and cue[\"role\"] in self._loaded:\n            self._token += 1\n            self._send((\"play\", cue[\"role\"], sample, 0, self._token))\n            self._epoch = None",
  "        if self._device_ok and cue[\"role\"] in self._loaded:\n            self._token += 1\n            self._epoch = None"),

 ("a move keeps the old token, so the old position is followed", "ltcplay/clock.py",
  "        if self._device_ok and cue[\"role\"] in self._loaded:\n            self._token += 1\n",
  "        if self._device_ok and cue[\"role\"] in self._loaded:\n"),

 ("a move while paused leaves the frozen frame where it was", "ltcplay/clock.py",
  "                self._frozen_frame = frame\n                self._frozen_sec = sample / float(self.rate)",
  "                self._frozen_sec = sample / float(self.rate)"),

 ("the A/B loop never wraps back to A", "ltcplay/clock.py",
  "        if loop is not None and not self._pause_req and \\\n                pos >= loop[1] - 1e-9:",
  "        if False:"),

 ("a loop outlives its cue", "ltcplay/clock.py",
  "        self._cue = None\n        self._loop = None\n        self._reset_cue()",
  "        self._cue = None\n        self._reset_cue()"),

 ("a move past the end of the cue is accepted", "ltcplay/clock.py",
  '        if frame >= cue["tc_frames"] or sample >= cue["frames"]:',
  '        if False:'),

 ("the audio and the timecode start a move on different frames", "ltcplay/clock.py",
  "        sample = int(round(frame * self.rate / float(MASTER_FPS)))",
  "        sample = int(round(start_s * self.rate)) + 7"),

 ("a move with no interface starts the timecode at the top", "ltcplay/clock.py",
  "            self._epoch = now - sample / float(self.rate)\n            self._mode = \"freerun\"",
  "            self._epoch = now\n            self._mode = \"freerun\""),

 ("a move into another cue moves inside the one playing", "ltcplay/session.py",
  '            if cue.name == cur.get("label"):',
  '            if True:'),

 ("a loop may reach into another cue", "ltcplay/session.py",
  "        if a < 0 or self._playhead_cue(float(b_show_s) - 1e-6).name != \\\n                cur.get(\"label\"):",
  "        if a < 0:"),

 ("Play from never starts the show (no conductor show start)", "ltcplay/playhead.py",
  "            r = cond.show_starting(who, screen)\n            if getattr(r, \"ok\", True) is False:",
  "            r = None\n            if getattr(r, \"ok\", True) is False:"),

 ("Play from goes ahead with an Abort standing", "ltcplay/playhead.py",
  '        if cond is not None and getattr(cond, "latched", False):',
  '        if False:'),

 ("Play from leaves MadMapper on whatever bank it was", "ltcplay/playhead.py",
  "                mm.select_bank(mm.cfg.show_bank, wait=False)",
  "                pass"),

 ("a refused show start leaves the music playing", "ltcplay/playhead.py",
  "                s.clock_halt()\n                self._refused(r, \"Play from\")",
  "                self._refused(r, \"Play from\")"),

 ("Pause freezes only the clock, not the show's Hold", "ltcplay/playhead.py",
  '            self._refused(cond.hold(who, screen), "Pause")',
  '            s.clock_pause()'),

 ("Continue skips the show's Resume", "ltcplay/playhead.py",
  '            self._refused(cond.resume(who, screen), "Continue")',
  '            s.clock_resume()'),

 ("Stop leaves the rig in the show look", "ltcplay/playhead.py",
  "            cond.intermission(who, screen)",
  "            pass"),

 ("Back and Ahead go before the start of the show", "ltcplay/playhead.py",
  "            at = s.clock_seek(max(0.0, here + float(seconds)))",
  "            at = s.clock_seek(here + float(seconds))"),

 ("Programming Play from on the show audio is a free run again", "ltcplay/remote.py",
  "        ph = self.playhead\n        if not ph.applies():\n            ph = None",
  "        ph = None"),

 ("the page is not told the show audio is the playhead", "ltcplay/remote.py",
  '            out.update(playhead="show_audio", playing=v["playing"],',
  '            out.update(playing=v["playing"],'),


 # Review of #142.
 ("a Pause before a move is heard freezes at 00:00:00:00", "ltcplay/clock.py",
  '                frame = cue.get("first_frame", 0)\n                sec = cue.get("start_sample", 0) / float(self.rate)',
  '                frame = 0\n                sec = 0.0'),

 ("a check-only Play from starts the cue", "ltcplay/clock.py",
  "            if check_only:\n                return now",
  "            if False:\n                return now"),

 ("Play from asks nothing before it moves MadMapper's bank", "ltcplay/playhead.py",
  "        s.clock_play_from(show_s, check_only=True)",
  "        pass"),

 ("a Stop leaves the conductor recording the flame cues released", "ltcplay/playhead.py",
  '                cond.intermission("the Programming screen", "")',
  '                pass'),

 ("Play from straight after a Stop skips putting the conductor right", "ltcplay/playhead.py",
  "        self.poll()\n        mm = getattr(",
  "        mm = getattr("),

 ("an unwatched Programming playback runs for ever", "ltcplay/playhead.py",
  "        if self._clock() - self._seen > WATCHDOG_S:",
  "        if False:"),

 ("the status poll is not the Programming watchdog's heartbeat", "ltcplay/remote.py",
  "        ph.seen()            # an operator page is open",
  "        pass                 # an operator page is open"),

 ("a screen Hold in Programming playback is the scheduler's Hold", "ltcplay/remote.py",
  "        if ph is not None:                          # hold",
  "        if False:                                   # hold"),

 ("a screen Abort in Programming playback leaves the music playing", "ltcplay/remote.py",
  "            if ph is not None:\n                stopped = True\n                try:\n                    said = ph.abort_press(who, screen)",
  "            if False:\n                stopped = True\n                try:\n                    said = ph.abort_press(who, screen)"),


 # Re-review of #142.
 ("a Show mode Run on this machine's clock counts as Programming", "ltcplay/remote.py",
  '            asked = getattr(s, "on_lost_arg", None) == "hold"',
  '            asked = getattr(p, "on_lost", "") == "hold"'),

 ("Start now goes over Programming playback", "ltcplay/remote.py",
  "            why = self.programming_playback_refusal()\n            if why:",
  "            why = self.programming_playback_refusal()\n            if False:"),

 ("a real show started over Programming playback is still watched as it", "ltcplay/playhead.py",
  "        if self._live() or (s is ours[0] and cue is not None and\n                            cue is not ours[1]):",
  "        if False:"),

 ("any cue on the clock reads as Programming playback", "ltcplay/playhead.py",
  '                getattr(clk, "_cue", None) is ours[1])',
  '                True)'),

 ("a Programming Abort answers that nothing was stopped", "ltcplay/remote.py",
  "                out = dict(out, programming=said, stopped=stopped, ok=ok,",
  "                out = dict(out, programming=said, ok=ok,"),

 # Third review of #142.
 ("a Programming Hold leaves the schedule running", "ltcplay/remote.py",
  '            if svc is not None and getattr(m, "state", None) in ("IDLE",',
  '            if False and getattr(m, "state", None) in ("IDLE",'),

 ("Run goes over Programming playback", "ltcplay/web.py",
  '                if _why:\n                    return self._send(409, {"error": _why})',
  '                if False:\n                    return self._send(409, {"error": _why})'),

 ("a failed Programming stop on Abort is reported as stopped", "ltcplay/remote.py",
  "                ok = stopped and not (flames and flames[0] is False)",
  "                ok = not (flames and flames[0] is False)"),

 ("the watchdog halts a show that took over meanwhile", "ltcplay/playhead.py",
  "            if only is not None and not self._mine(only):\n                return False",
  "            if False:\n                return False"),

 ("a show taking over Programming playback is not journaled", "ltcplay/playhead.py",
  '            self._note("Programming playback was ended by the show "\n                       "starting.", "taken_over")',
  "            pass"),

 # #142 after #144 and #149.
 ("Start now never asks the Programming playhead under the lock", "ltcplay/schedule_service.py",
  "            if kind == sch.START_NOW and self.start_veto is not None:",
  "            if False:"),

 ("Play from leaves a waiting Start now waiting", "ltcplay/playhead.py",
  '                cancel("Play from was pressed on the Programming screen")',
  '                pass'),

 ("a Programming Hold leaves a waiting Start now waiting", "ltcplay/remote.py",
  '                    cancel(f"Hold was pressed on the {screen}")',
  '                    pass'),

 ("Programming playback starts over a Fireball channel that is on", "ltcplay/remote.py",
  '        if ph is not None and name in ("play-from", "loop") and \\\n                fb is not None and fb.active():',
  '        if False:'),

 # Jeff's field report, 2026-10-28: Stop, Reset from Hold, refusal reasons.
 ("a refused start's alert says only that the show did not start",
  "ltcplay/schedule_service.py",
  '        why = str(why or "").strip().rstrip(".")\n        if not why:',
  '        why = ""\n        if not why:'),

 ("Start now with Run off becomes a FAULT slot again",
  "ltcplay/schedule_service.py",
  "        if kind == sch.START_NOW:\n            # Outside this service's lock",
  "        if False:\n            # Outside this service's lock"),

 ("Start now while the show audio loads is not said to wait",
  "ltcplay/fire_ice.py",
  "        if any(role not in loaded for role in roles):\n"
  "            return self.AUDIO_LOADING",
  "        if False:\n            return self.AUDIO_LOADING"),

 ("Reset from Hold does nothing again", "ltcplay/remote.py",
  "                r = fn(who, screen, surface=surface) if fn else None",
  "                r = None"),

 ("Reset from a paused show carries the show on",
  "ltcplay/schedule_service.py",
  "            if m.state == sch.PAUSED:\n                kinds.append(sch.STOP_SHOW)",
  "            if m.state == sch.PAUSED:\n                kinds.append(sch.RESUME)"),

 ("Rack screen: Start now never becomes Stop during a show",
  "ltcplay/web/remote.html",
  'const live = sch.state === "SHOW" || sch.state === "PAUSED" ||',
  'const live = false ||'),

 ("Rack screen: Stop is not the complete halt", "ltcplay/web/remote.html",
  'r = await fetch("/api/remote/stop", ', 'r = await fetch("/api/stop", '),

 ("Stop from a screen does not disarm or end the show", "ltcplay/remote.py",
  "        h = self.halt(who, screen)\n        out = {k: h[k]",
  "        h = {}\n        out = {k: h[k]"),

 ("Reset ends a Hold inside the late grace and the show starts",
  "ltcplay/schedule.py",
  "            if lateness_s(s.start, now) < 0:\n                break\n"
  "            _hold_back(tx, s)",
  "            if True:\n                break\n            _hold_back(tx, s)"),

 ("Reset counts as the operator's Resume for the alert hold",
  "ltcplay/schedule_service.py",
  "                ev.detail != sch.RESET_DETAIL and \\\n",
  ""),

 ("a broken intermission file blocks every show start",
  "ltcplay/fire_ice.py",
  "        roles = [role] if role is not None else []",
  "        roles = [v[0] for v in (getattr(clk, \"cues\", None) or {}).values()]"),

 ("Start now while the show audio loads is refused at once again",
  "ltcplay/schedule_service.py",
  "        if blocked is not None and blocked[\"loading\"]:\n            with self._locked():",
  "        if False:\n            with self._locked():"),

 ("Hold does not end a Start now waiting for the show audio",
  "ltcplay/schedule_service.py",
  "            self.cancel_start_wait(f\"{what} was pressed\")",
  "            pass"),

 ("Stop does not end a Start now waiting for the show audio",
  "ltcplay/remote.py",
  "                cancel(f\"Stop was pressed on the {screen}\")",
  "                pass"),

 ("Rack screen: Run never becomes Stop while Run is starting",
  "ltcplay/web/remote.html",
  "  return (show.running || show.starting) ? {mode:\"stop\"",
  "  return show.running ? {mode:\"stop\""),

 ("Rack screen: a press is not decided by what was seen at touch-down",
  "ltcplay/web/remote.html",
  "  if(seen && seen !== now)\n    return {act:\"none\"",
  "  if(false)\n    return {act:\"none\""),

 ("Rack screen: Start is not locked after Stop turns back into Start",
  "ltcplay/web/remote.html",
  "  if(now !== \"stop\" && flippedAt && nowMs - flippedAt < FLIP_LOCK_MS)",
  "  if(false)"),

 ("Rack screen: Start now works during a halt",
  "ltcplay/web/remote.html",
  "  if(busy) return {act:\"none\", say:\"Stopping: wait until it has finished.\"};\n",
  ""),

 ("an announcement or engine Hold does not end a waiting Start now",
  "ltcplay/schedule_service.py",
  "        if not out.refused and ev.kind in (sch.HOLD_ON, sch.ABORT,\n"
  "                                          sch.STOP_SHOW):",
  "        if not out.refused and ev.kind in (sch.ABORT, sch.STOP_SHOW):"),

 ("Start now goes ahead during a Stop's halt",
  "ltcplay/schedule_service.py",
  "        if kind == sch.START_NOW and self.halting():",
  "        if False:"),

 ("Run goes ahead during a Stop's halt", "ltcplay/web.py",
  '                if getattr(_svc, "halting", lambda: False)() is True:',
  "                if False:"),

 ("a Start now that waited is not re-checked under the lock",
  "ltcplay/schedule_service.py",
  "            if gen is not None and (self._start_cancel[0] != gen or\n"
  "                                    self.halting()):",
  "            if False:"),

 ("Rack screen: Start now is not off while a halt runs",
  "ltcplay/web/remote.html",
  "    (el.disabled || lk.disabled || HALT_BUSY || flipLocked(\"b-start\"));",
  "    (el.disabled || lk.disabled || flipLocked(\"b-start\"));"),

 ("Start now's lock re-check runs before the tick's alert Hold",
  "ltcplay/schedule_service.py",
  "        with self._locked():\n"
  "            # The tick first: a Hold it takes (the engine's own at\n"
  "            # showtime for an open alert) cancels the start below (C1).\n"
  "            self.tick()\n",
  "        with self._locked():\n"),

 ("a late failure report raises a did-not-start alert",
  "ltcplay/schedule_service.py",
  "                    le.outcome != \"recorded as a fault\" and \\\n",
  ""),

 ("Rack screen: Run never becomes Stop", "ltcplay/web/remote.html",
  'return (show.running || show.starting) ? {mode:"stop"',
  'return false ? {mode:"stop"'),

 ("Rack screen: a scheduler refusal reads Conflict again",
  "ltcplay/web/remote.html",
  "  const t = j && (j.error || j.text);", "  const t = j && j.error;"),

 ("Rack screen: no Loading state while the show audio loads",
  "ltcplay/web/remote.html",
  "if(b && b.text && b.loading)", "if(false)"),

 # The Fireball page (fireball.py, OPERATOR.md, Jeff 2026-10-28; design
 # by Andy): manual control of each Fireball channel.
 ("fireball: a channel goes on with the group not armed",
  "ltcplay/fireball.py",
  "            state, why = self.armed_state()\n"
  "            if state != \"armed\":\n"
  "                return why\n",
  "            state, why = self.armed_state()\n"
  "            if False:\n"
  "                return why\n"),

 ("fireball: a stale flamesafe status counts as fresh",
  "ltcplay/fireball.py",
  "                age > STATUS_FRESH_S * 1000:",
  "                age > 10 ** 9:"),

 ("fireball: the frame writes the safety slot",
  "ltcplay/fireball.py",
  "            if key in on:\n"
  "                vals[slot - 1] = ON_VALUE\n"
  "        return bytes(vals)",
  "            if key in on:\n"
  "                vals[slot - 1] = ON_VALUE\n"
  "                vals[self.group[\"safety\"] - 1] = ON_VALUE\n"
  "        return bytes(vals)"),

 ("fireball: a channel goes on at 254",
  "ltcplay/fireball.py",
  "ON_VALUE = 255",
  "ON_VALUE = 254"),

 ("fireball: only the JSON word true is not required",
  "ltcplay/fireball.py",
  "    return {k for k in KEYS if c.get(k) is True}",
  "    return {k for k in KEYS if c.get(k)}"),

 ("fireball: the watcher never cuts a run whose beats stopped",
  "ltcplay/fireball.py",
  "        if now - r[\"beat\"] > BEAT_STALE_S:\n"
  "            why = f\"no heartbeat",
  "        if False:\n"
  "            why = f\"no heartbeat"),

 ("fireball: the watcher does not see the flame link zeroed",
  "ltcplay/fireball.py",
  "            if link is None or self._gen(link) != r[\"gen\"]:",
  "            if link is None:"),

 ("fireball: the watcher does not look at the rules",
  "ltcplay/fireball.py",
  "            else:\n"
  "                why = self.refusal()\n"
  "        if why:",
  "            else:\n"
  "                why = \"\"\n"
  "        if why:"),

 ("fireball: a cut run can come back",
  "ltcplay/fireball.py",
  "        if rid in e[\"ids\"]:\n            return True",
  "        if False:\n            return True"),

 ("fireball: an overtaken beat is used",
  "ltcplay/fireball.py",
  "            elif seq is not None and r[\"seq\"] is not None and \\\n"
  "                    seq <= r[\"seq\"]:",
  "            elif False:"),

 ("fireball: another screen takes over a run",
  "ltcplay/fireball.py",
  "            if r is not None and r[\"owner\"] != owner:\n"
  "                return 409,",
  "            if False:\n"
  "                return 409,"),

 ("fireball: a zero that lands during a beat is ignored",
  "ltcplay/fireball.py",
  "                lost = self._gen(link) != r[\"gen\"]",
  "                lost = False"),

 ("fireball: a disarm seen just after the frame went in is ignored",
  "ltcplay/fireball.py",
  "        state, why = self.armed_state()\n"
  "        if state != \"armed\":\n"
  "            self.cut(",
  "        state, why = self.armed_state()\n"
  "        if False:\n"
  "            self.cut("),

 ("fireball: a cut leaves the frame on the flame link",
  "ltcplay/fireball.py",
  "        if r is not None and link is not None:\n"
  "            try:\n"
  "                link.purge_clear()",
  "        if False:\n"
  "            try:\n"
  "                link.purge_clear()"),

 ("fireball: a refused beat leaves its run on",
  "ltcplay/fireball.py",
  "            if mine:\n"
  "                self.cut(f\"refused: {why}\", who=who, screen=screen)",
  "            if False:\n"
  "                self.cut(f\"refused: {why}\", who=who, screen=screen)"),

 ("fireball: the engine's blockers are not asked",
  "ltcplay/fireball.py",
  "            why = self._blockers() or \"\"",
  "            why = \"\""),

 ("fireball: a locked out slot can fire",
  "ltcplay/fireball.py",
  "        hit = sorted(locked & set(self.group[\"fire\"]))",
  "        hit = []"),

 ("fireball: a stale page may turn a channel on",
  "ltcplay/fireball.py",
  "        if not seen_ok:\n"
  "            why = (\"The page's status",
  "        if False:\n"
  "            why = (\"The page's status"),

 ("fireball: Run pressed does not hold it off",
  "ltcplay/fireball.py",
  "        if cs.get(\"running\") or cs.get(\"starting\"):",
  "        if False:"),

 ("fireball: a purge selection does not hold it off",
  "ltcplay/fireball.py",
  "        if pg.fire is not None or pg.selected:",
  "        if False:"),

 ("fireball: a show live does not hold it off",
  "ltcplay/fireball.py",
  "        why = remote.morning_live_refusal() or m._live_or_broken()",
  "        why = m.config_error"),

 ("fireball: the purge switch does not apply",
  "ltcplay/fireball.py",
  "        if controls_on(body) and not remote.purge_enabled(ctx.local):",
  "        if False:"),

 ("fireball: the group needs no fire slot count",
  "ltcplay/fireball.py",
  "        if len(fire) != len(CONTROLS) or len(set(fire)) != len(fire) or \\\n",
  "        if not fire or len(set(fire)) != len(fire) or \\\n"),

 ("fireball: the buttons follow the file's order, not the channel number",
  "ltcplay/fireball.py",
  "                \"fire\": sorted(fire)}, \"\"",
  "                \"fire\": fire}, \"\""),

 ("fireball: a beat overtaken while its frame is worked out is written",
  "ltcplay/fireball.py",
  "            if self._run is r and seq is not None and r[\"seq\"] != seq:",
  "            if False:"),

 ("fireball: every screen's all off is remembered, so a flood pushes out "
  "a real ended run",
  "ltcplay/fireball.py",
  "            if not make:\n                return\n",
  "            pass\n"),

 ("fireball: a new run may start with an old beat number",
  "ltcplay/fireball.py",
  "        return starting and seq is not None and e[\"seq\"] is not None and \\\n"
  "            seq <= e[\"seq\"]",
  "        return False"),

 ("fireball: the Startup page's purge does not wait for it",
  "ltcplay/morning.py",
  "            why = self._other() or \"\"",
  "            why = \"\""),

 ("fireball: an Abort on the rack screen does not cut it",
  "ltcplay/morning.py",
  "                hook(who, screen)",
  "                pass"),

 ("fireball: a beat that turns a channel on is a safety press",
  "ltcplay/users.py",
  "            return \"arm\" if fireball_on_wanted(raw) else \"safety\"",
  "            return \"safety\""),

 ("fireball: All off needs a sign-in",
  "ltcplay/users.py",
  "    \"fireball-off\": \"safety\",",
  "    \"fireball-off\": \"arm\","),

 ("fireball: the engine closes it last",
  "ltcplay/web.py",
  "        try:\n"
  "            remote.fireball.close()\n"
  "        finally:\n"
  "            _close_before()",
  "        try:\n"
  "            _close_before()\n"
  "        finally:\n"
  "            remote.fireball.close()"),

 ("fireball page: a held button is not let go on the lift",
  "ltcplay/web/remote.html",
  "    const b = e.target.closest && e.target.closest(\".fb-pad\"); if(!b || b.dataset.kind !== \"momentary\") return;\n"
  "    fbPress(b.dataset.k, false);",
  "    const b = e.target.closest && e.target.closest(\".fb-pad\"); if(!b || b.dataset.kind !== \"momentary\") return;"),

 ("fireball page: losing focus holds the valve on",
  "ltcplay/web/remote.html",
  "window.addEventListener(\"blur\", () => fbRelease(\"the page lost focus\"));\n",
  ""),

 ("fireball page: leaving the page leaves the channels on",
  "ltcplay/web/remote.html",
  "  if(!on && FB_OPEN){ FB_OPEN = false; clearTimeout(FB_TIMER); fbCut(\"the page was left\"); }",
  "  if(!on && FB_OPEN){ FB_OPEN = false; clearTimeout(FB_TIMER); }"),

 ("fireball page: a refused beat leaves the channels on",
  "ltcplay/web/remote.html",
  "  catch(e){ if(run === FB_RUN) fbCut(e.message); }",
  "  catch(e){}"),

 ("fireball page: signed out is not view only",
  "ltcplay/web/remote.html",
  "  else if(viewOnly) out.block = \"Signed out: this screen is view only. Sign in to use the Fireball controls.\";\n",
  ""),

 ("fireball page: the Fireball disarming leaves a toggle on",
  "ltcplay/web/remote.html",
  "    if(any && !out.cut) out.cut = \"the Fireball is not armed\";\n",
  ""),

 ("fireball page: a stale page keeps beating",
  "ltcplay/web/remote.html",
  "  if(stale) out.block = \"This page's status is not live, so nothing can be pressed.\";\n"
  "  else if(viewOnly)",
  "  if(false) out.block = \"\";\n"
  "  else if(viewOnly)"),

 ("fireball page: a toggle is on while held only",
  "ltcplay/web/remote.html",
  "  const b = e.target.closest(\".fb-pad\"); if(!b || b.dataset.kind !== \"toggle\") return;\n"
  "  fbPress(b.dataset.k, !FB_ON[b.dataset.k]);",
  "  const b = e.target.closest(\".fb-pad\"); if(!b || b.dataset.kind !== \"toggle\") return;\n"
  "  fbPress(b.dataset.k, true);"),

 # Wave flamer cleaning (cleaning.py, MORNING.md, OPERATOR.md; Andy's
 # design, 2026-10-28): a dead-man sequence on the Shutdown page.
 ("cleaning: the watcher does not look at the rules",
  "ltcplay/cleaning.py",
  "                    why = self.refusal()\n"
  "                    if why:\n"
  "                        self.cut(why)",
  "                    why = \"\"\n"
  "                    if why:\n"
  "                        self.cut(why)"),

 ("cleaning: the flame link zeroed (Stop, Hold, Abort) is not seen",
  "ltcplay/cleaning.py",
  "        if link is None or self._gen(link) != r[\"gen\"]:\n"
  "            return self._stop_run(",
  "        if link is None:\n"
  "            return self._stop_run("),

 ("cleaning: a disarm pressed elsewhere is not seen",
  "ltcplay/cleaning.py",
  "        if self._count() != r[\"disarms\"]:",
  "        if False:"),

 ("cleaning: armed 3 s is not counted from flamesafe's word",
  "ltcplay/cleaning.py",
  "            if now - r[\"step_at\"] >= cfg[\"armed_s\"]:",
  "            if now - r[\"step_at\"] >= cfg[\"armed_s\"] - 0.5:"),

 ("cleaning: the gap is counted from the disarm sent, not from flamesafe",
  "ltcplay/cleaning.py",
  "        if step == \"disarming\":\n"
  "            if state == \"disarmed\":",
  "        if step == \"disarming\":\n"
  "            if True:"),

 ("cleaning: the bursts go on with the group not armed",
  "ltcplay/cleaning.py",
  "        if step == \"burst\":\n"
  "            if state != \"armed\":",
  "        if step == \"burst\":\n"
  "            if False:"),

 ("cleaning: a stop leaves the ignition on",
  "ltcplay/cleaning.py",
  "                        link.purge_set(frame(self.group, r[\"cfg\"][\"aim\"], 0))",
  "                        pass"),

 ("cleaning: a stop's disarm is never confirmed",
  "ltcplay/cleaning.py",
  "        if now - c[\"at\"] > DISARM_CONFIRM_S:",
  "        if False:"),

 ("cleaning: a stopped run can carry on",
  "ltcplay/cleaning.py",
  "        if rid in e[\"ids\"]:\n            return True",
  "        if False:\n            return True"),

 ("cleaning: it starts with the wave flamer already armed",
  "ltcplay/cleaning.py",
  "        if starting and state != \"disarmed\":",
  "        if False:"),

 ("cleaning: another flame group armed is allowed",
  "ltcplay/cleaning.py",
  "            elif g.get(\"armed\") != \"disarmed\" or g.get(\"wanted\"):\n"
  "                others.append(name)",
  "            elif False:\n"
  "                others.append(name)"),

 ("cleaning: a stale flamesafe status counts as fresh",
  "ltcplay/cleaning.py",
  "                age > STATUS_FRESH_S * 1000:",
  "                age > 10 ** 9:"),

 ("cleaning: the ignition is channel 4, not 3",
  "ltcplay/cleaning.py",
  "                             \"ignition\": s - 3,",
  "                             \"ignition\": s - 2,"),

 ("cleaning: aim slots that are not the angles are taken",
  "ltcplay/cleaning.py",
  "        if set(aim) != {f[\"angle\"] for f in fixtures} or \\\n",
  "        if False or \\\n"),

 ("cleaning: the gap may be shorter than the deck's re-arm window",
  "ltcplay/cleaning.py",
  "          \"disarmed_s\": (2.5, 30.0),",
  "          \"disarmed_s\": (1.0, 30.0),"),

 ("cleaning: a stale page status is accepted",
  "ltcplay/cleaning.py",
  "            if not seen_ok:\n"
  "                why = (\"The page's status is more than 1 s old.",
  "            if False:\n"
  "                why = (\"The page's status is more than 1 s old."),

 ("cleaning: signed out runs it at the route",
  "ltcplay/cleaning.py",
  "        if body.get(\"hold\") is True and remote.users_active() and \\\n"
  "                not ctx.user:",
  "        if False:"),

 ("cleaning: arming from a screen switched off is not checked",
  "ltcplay/cleaning.py",
  "        if not (arming or remote.arming_enabled)():\n"
  "            return (\"arming from a screen is switched off",
  "        if False:\n"
  "            return (\"arming from a screen is switched off"),

 ("cleaning: the Stream Deck not reading is not checked",
  "ltcplay/cleaning.py",
  "        if seen is None or remote.clock() - seen > remote.DECK_QUIET_S:",
  "        if False:"),

 ("cleaning: an open deck alert does not stop it",
  "ltcplay/cleaning.py",
  "            if getattr(a, \"kind\", \"\") in BLOCKING_ALERTS:",
  "            if False:"),

 ("cleaning: someone holding to arm another group is allowed",
  "ltcplay/cleaning.py",
  "        if holds:\n"
  "            return (f\"{holds[0]['who'] or holds[0]['device']} is holding to \"",
  "        if False:\n"
  "            return (f\"{holds[0]['who'] or holds[0]['device']} is holding to \""),

 ("cleaning: the Startup page's purge does not wait for it",
  "ltcplay/cleaning.py",
  "        return (\"the wave flamer cleaning is running; stop it first.\"\n"
  "                if panel.active() else \"\")",
  "        return \"\""),

 ("cleaning: an Abort does not stop it",
  "ltcplay/cleaning.py",
  "    morning.abort_hooks.append(panel.on_abort)\n    return panel",
  "    return panel"),

 ("cleaning: the Fireball page does not wait for it",
  "ltcplay/fireball.py",
  "        if cl is not None and cl.active():",
  "        if False:"),

 ("cleaning: a beat that runs it is a safety press",
  "ltcplay/users.py",
  "            return \"arm\" if body.get(\"hold\") is True else \"safety\"",
  "            return \"safety\""),

 ("cleaning: Stop needs a sign-in",
  "ltcplay/users.py",
  "    \"cleaning-stop\": \"safety\",",
  "    \"cleaning-stop\": \"arm\","),

 ("cleaning: its bursts count as a Showven purge (not counted)",
  "ltcplay/fuel.py",
  "        if rehearse or program or (purge and burn):",
  "        if rehearse or program:"),

 ("cleaning: the angle may be far off straight up",
  "ltcplay/cleaning.py",
  "\"aim\": (118, 138),",
  "\"aim\": (0, 255),"),

 ("cleaning: the heads may get no time to come up",
  "ltcplay/cleaning.py",
  "          \"aim_settle_s\": (1.0, 5.0),",
  "          \"aim_settle_s\": (0.0, 5.0),"),

 ("cleaning: an Abort waits on the panel's lock",
  "ltcplay/cleaning.py",
  "        threading.Thread(target=self._abort_cut, args=(why,), daemon=True,\n"
  "                         name=\"ltcplay-cleaning-abort\").start()",
  "        self._abort_cut(why)"),

 ("cleaning: an Abort does not drop the frame at once",
  "ltcplay/cleaning.py",
  "        self._aborted = why\n"
  "        link = self._link()\n"
  "        if link is not None:\n"
  "            try:\n"
  "                link.purge_clear()",
  "        self._aborted = why\n"
  "        link = self._link()\n"
  "        if False:\n"
  "            try:\n"
  "                link.purge_clear()"),

 ("cleaning: a hold whose beats stopped is not ended",
  "ltcplay/cleaning.py",
  "                if h is not None and now - h[\"beat\"] > BEAT_STALE_S:",
  "                if False:"),

 ("cleaning: a release before the hold is complete still starts it",
  "ltcplay/cleaning.py",
  "        if r[\"phase\"] == 1:\n"
  "            self.cut(f\"{why}; nothing started\", outcome=\"not started\")\n"
  "            return",
  "        if r[\"phase\"] == 1:\n"
  "            r[\"authorized\"] = True\n"
  "            return"),

 ("cleaning: the pause goes on to the bursts by itself",
  "ltcplay/cleaning.py",
  "                    self._to(r, \"paused\", now, paused_at=now, asked=False,\n"
  "                             hold_id=None)",
  "                    self._to(r, \"arm\", now, phase=2, asked=False,\n"
  "                             hold_id=None, authorized=True, hold=None)"),

 ("cleaning: a tap is a go-ahead",
  "ltcplay/cleaning.py",
  "        self._to(r, \"paused\", r.get(\"paused_at\", self._clock()),\n"
  "                 asked=False, hold_id=None)",
  "        self._to(r, \"burst\", self._clock(), burst_at=self._clock(),\n"
  "                 aimed_at=None)"),

 ("cleaning: paused never goes back to idle",
  "ltcplay/cleaning.py",
  "            if now - r[\"paused_at\"] >= cfg[\"pause_max_min\"] * 60:",
  "            if False:"),

 ("cleaning: the bursts never end by themselves",
  "ltcplay/cleaning.py",
  "            if now - r[\"step_at\"] >= cfg[\"max_burst_min\"] * 60:",
  "            if False:"),

 ("cleaning: signing out does not stop the walk-away",
  "ltcplay/cleaning.py",
  "            if not live:\n"
  "                return self._stop_run(",
  "            if False:\n"
  "                return self._stop_run("),

 ("cleaning: a lockout change mid-routine does not stop it",
  "ltcplay/cleaning.py",
  "        if self._locked_fixtures() != r[\"locked\"]:",
  "        if False:"),

 ("cleaning: a stop leaves it armed",
  "ltcplay/cleaning.py",
  "            need = r[\"step\"] not in (\"disarmed\", \"paused\") or \\\n"
  "                state != \"disarmed\"",
  "            need = False"),

 ("cleaning: an older beat of a hold arriving late is used",
  "ltcplay/cleaning.py",
  "            if h is not None and seq is not None and h[\"seq\"] is not None \\\n"
  "                    and seq <= h[\"seq\"]:",
  "            if False:"),

 ("cleaning: another screen takes over a hold",
  "ltcplay/cleaning.py",
  "            if h is not None and h[\"owner\"] != owner:\n"
  "                return 409,",
  "            if False:\n"
  "                return 409,"),

 ("cleaning: letting go of a hold does not end it",
  "ltcplay/cleaning.py",
  "                    self._hold_ended(r, f\"{who or owner[0]} let go before the \"",
  "                    (lambda *a: None)(r, f\"{who or owner[0]} let go before the \""),

 ("cleaning: the Stream Deck's key is not Stop while it runs",
  "ltcplay/remote.py",
  "                if cl.active():\n"
  "                    live = True",
  "                if False:\n"
  "                    live = True"),

 ("cleaning: the screens' status does not carry it",
  "ltcplay/remote.py",
  "                out[\"cleaning\"] = self.cleaning.summary()",
  "                pass"),

 ("cleaning page: letting go before the hold completes does not end it",
  "ltcplay/web/remote.html",
  "  $(\"wc-hold\").addEventListener(ev, () => { if(WC_HOLD) wcLetGo(\"you let go before the hold was complete\"); });",
  "  $(\"wc-hold\").addEventListener(ev, () => {});"),

 ("cleaning page: signed out is not view only",
  "ltcplay/web/remote.html",
  "  else if(viewOnly) out.block = \"Signed out: this screen is view only. Sign in to start the cleaning.\";\n",
  ""),

 ("cleaning page: the banner never shows",
  "ltcplay/web/remote.html",
  "  $(\"wc-banner\").hidden = !on;",
  "  $(\"wc-banner\").hidden = true;"),

 ("cleaning page: the page keeps beating after the hold completes",
  "ltcplay/web/remote.html",
  "    if(j.state === \"phase1\" || j.state === \"phase2\"){",
  "    if(false){"),

 ("cleaning: phase 2 starts before flamesafe says armed",
  "ltcplay/cleaning.py",
  "            if r[\"asked\"]:\n"
  "                # The deck confirmed it took THIS hold: wait for ARMED.\n"
  "                if state == \"armed\":",
  "            if r[\"asked\"]:\n"
  "                # The deck confirmed it took THIS hold: wait for ARMED.\n"
  "                if state in (\"armed\", \"wanted\"):"),

 ("cleaning: the settle counts from ARMED, not the first frame written",
  "ltcplay/cleaning.py",
  "                        self._to(r, \"burst\", now, burst_at=now,\n"
  "                                 aimed_at=None)",
  "                        self._to(r, \"burst\", now, burst_at=now,\n"
  "                                 aimed_at=now)"),

 ("cleaning: the start hold presses the arm from the watcher too",
  "ltcplay/cleaning.py",
  "            if h is not None:\n"
  "                if not press:\n"
  "                    return \"\"",
  "            if h is not None:\n"
  "                if False:\n"
  "                    return \"\""),

 ("cleaning: phase 1 fires",
  "ltcplay/cleaning.py",
  "                    if r[\"phase\"] == 1:\n"
  "                        self._to(r, \"armed\", now)",
  "                    if r[\"phase\"] == 1:\n"
  "                        self._to(r, \"burst\", now, burst_at=now, aimed_at=None)"),

 ("cleaning: the walk-away arms phase 2 too",
  "ltcplay/cleaning.py",
  "            if r[\"phase\"] != 1 or not WALK_AWAY_PHASE1 or \\\n"
  "                    not r[\"authorized\"]:",
  "            if not WALK_AWAY_PHASE1 or not r[\"authorized\"]:"),

 ("cleaning: a hold is credited when the group arms some other way",
  "ltcplay/cleaning.py",
  "            if out.get(\"taken\") is not True:",
  "            if False:"),

 ("cleaning: the group armed some other way during a hold is waited out",
  "ltcplay/cleaning.py",
  "                if now - r[\"foreign_at\"] > FOREIGN_GRACE_S:",
  "                if False:"),

 ("cleaning: a go-ahead that ends with the group armed leaves it paused",
  "ltcplay/cleaning.py",
  "        if state != \"disarmed\":\n"
  "            # Armed or asked for during a go-ahead that never completed:",
  "        if False:\n"
  "            # Armed or asked for during a go-ahead that never completed:"),

 ("cleaning page: losing focus does not stop it",
  "ltcplay/web/remote.html",
  "window.addEventListener(\"blur\", () => { if(WC_HOLD) wcLetGo(\"the page lost focus\"); });\n",
  ""),

 ("cleaning page: leaving the page leaves it running",
  "ltcplay/web/remote.html",
  "  if(!on && WC_OPEN){ WC_OPEN = false; clearTimeout(WC_TIMER); wcLetGo(\"the page was left\"); }",
  "  if(!on && WC_OPEN){ WC_OPEN = false; clearTimeout(WC_TIMER); }"),

 ("cleaning page: a refused beat keeps beating",
  "ltcplay/web/remote.html",
  "  }catch(e){ if(run === WC_RUN) wcLetGo(e.message.replace(/[.\\s]+$/, \"\")); }",
  "  }catch(e){}"),

 ("cleaning page: any truthy hold runs it",
  "ltcplay/web/remote.html",
  "  return {hold:hold === true, run:",
  "  return {hold:!!hold, run:"),

 ("cleaning page: a blocked button still starts",
  "ltcplay/web/remote.html",
  "  if(!st.ok){ $(\"wc-msg\").textContent = st.block; return; }\n  WC_HOLD = true;",
  "  WC_HOLD = true;"),

 # Settings changed on disk (Jeff's field report 2026-10-28, item 11):
 # configwatch.py, its notice, the live reload of the announcements, Reload
 # engine and the supervisor's word in programs.json.
 ("settings watch: the engine's copy is never judged changed",
  "ltcplay/configwatch.py",
  "        return e.digest(e.path) != e.value\n",
  "        return False\n"),

 ("settings watch: the deck and flamesafe are never named behind",
  "ltcplay/configwatch.py",
  "                    if theirs != now:\n",
  "                    if False:\n"),

 ("settings watch: a save of a setting read live counts as a change",
  "ltcplay/configwatch.py",
  "    return lambda path: _json_hash(path, lambda d: {k: d.get(k)\n"
  "                                                    for k in keys})\n",
  "    return lambda path: _json_hash(path, lambda d: d)\n"),

 ("settings watch: the notice never says to restart LTC Player",
  "ltcplay/configwatch.py",
  "        text += \" \" + RESTART + \" \" + RUNNING_OLD\n",
  "        pass\n"),

 ("settings watch: the notice is never journaled",
  "ltcplay/configwatch.py",
  "        if text:\n            self._line(text)\n",
  "        if text:\n            pass\n"),

 ("settings watch: the announcements are read again during a show",
  "ltcplay/announce.py",
  "        if show_busy:\n            return False, (\"it reads it again by itself once no show is \"\n",
  "        if False:\n            return False, (\"it reads it again by itself once no show is \"\n"),

 ("settings watch: the announcements are read again while one plays",
  "ltcplay/announce.py",
  "            if self.playing is not None:\n"
  "                return False, (\"it reads it again by itself once no \"\n",
  "            if False:\n"
  "                return False, (\"it reads it again by itself once no \"\n"),

 ("Reload engine: offered while a flame group is armed",
  "ltcplay/remote.py",
  "        if any(g.get(\"armed\") != \"disarmed\" or g.get(\"wanted\")\n"
  "               for g in groups):\n"
  "            return \"A flame group is armed.",
  "        if False:\n"
  "            return \"A flame group is armed."),

 ("Reload engine: offered while flamesafe's state is not seen",
  "ltcplay/remote.py",
  "        if v.get(\"stale\") or not groups:\n",
  "        if v.get(\"stale\"):\n"),

 ("Reload engine: offered with no flamesafe status at all",
  "ltcplay/remote.py",
  "        if fs is None:\n            return (\"flamesafe's status is not connected",
  "        if False:\n            return (\"flamesafe's status is not connected"),

 ("Reload engine: a check that fails lets the restart go",
  "ltcplay/remote.py",
  "            return (f\"Whether the engine may restart could not be checked \"\n"
  "                    f\"({type(e).__name__}).\")\n",
  "            return \"\"\n"),

 ("Reload engine: offered with live flame programming on",
  "ltcplay/remote.py",
  "        if lf is not None and lf.on:\n            return \"Live flame programming is on. Turn it off first.\"\n",
  ""),

 ("Reload engine: offered with the Fireball page in use",
  "ltcplay/remote.py",
  "            if sm.get(\"on\") or sm.get(\"owner\"):\n",
  "            if False:\n"),

 ("Reload engine: offered with a purge selected",
  "ltcplay/remote.py",
  "            if mv.get(\"purge\"):\n",
  "            if False:\n"),

 ("Reload engine: offered with a laser test on",
  "ltcplay/remote.py",
  "            if mv.get(\"laser_tests_on\"):\n",
  "            if False:\n"),

 ("Reload engine: offered with a laser armed",
  "ltcplay/remote.py",
  "            if (mv.get(\"laser_arm\") or {}).get(\"armed\"):\n",
  "            if False:\n"),

 ("Reload engine: the supervisor never asks the engine again",
  "packaging/windows/supervisor.py",
  "    why = engine_restart_refusal(port)\n    return bool(why), why\n",
  "    return False, \"\"\n"),

 ("Reload engine: an engine that does not answer may be restarted",
  "packaging/windows/supervisor.py",
  "        return (f\"the engine did not say whether it may be restarted \"\n"
  "                f\"({type(e).__name__}), so it is left running\")\n",
  "        return \"\"\n"),

 ("settings watch: a half written announcements file is taken",
  "ltcplay/announce.py",
  "        if error:\n            return False, (f\"the file is not valid",
  "        if False:\n            return False, (f\"the file is not valid"),

 ("settings watch: a BOM or Windows line ends count as a change",
  "ltcplay/configwatch.py",
  "    return raw.replace(b\"\\r\\n\", b\"\\n\")\n",
  "    return raw\n"),

 ("supervisor: hashes a file differently from the engine",
  "packaging/windows/supervisor.py",
  "    return hashlib.sha256(raw.replace(b\"\\r\\n\", b\"\\n\")).hexdigest()\n",
  "    return hashlib.sha256(raw).hexdigest()\n"),

 ("supervisor: programs.json never says what a program read",
  "packaging/windows/supervisor.py",
  "                    doc[n][\"read\"] = dict(read)\n",
  "                    pass\n"),

 ("settings watch: the fuel block counts as a Morning change",
  "ltcplay/configwatch.py",
  "                                                    if k not in keys})\n",
  "                                                    if True})\n"),

 ("settings watch: the operators written at first start are a change",
  "ltcplay/configwatch.py",
  "        if missing is None:\n            return None\n",
  "        return None\n"),

 ("settings watch: the notice never says when to restart",
  "ltcplay/configwatch.py",
  "RESTART = (\"Restart LTC Player between shows, when nothing is armed, to \"\n",
  "RESTART = (\"Restart LTC Player to \"\n"),

 ("Reload engine: offered while a show runs",
  "ltcplay/remote.py",
  "        if self.show_busy_for_reload():\n"
  "            return \"A show is running. Reload the engine between shows.\"\n",
  ""),

 ("settings watch: the status never carries the notice",
  "ltcplay/remote.py",
  "        out[\"config_changed\"] = self.config_changed_view(ctx)\n",
  ""),

 ("settings watch: ltc serve never notes flamesafe's config",
  "ltcplay/cli.py",
  "    watch.add(\"flamesafe config\", path, others=(cw.DECK, cw.FLAMESAFE))\n",
  "    pass\n"),

 ("supervisor: programs.json never says when a program started",
  "packaging/windows/supervisor.py",
  "                doc[n][\"started\"] = started\n",
  "                pass\n"),

 ("supervisor: a show folder change counts as a showpc.json change",
  "packaging/windows/supervisor.py",
  "    return sorted(k for k in keys if k != \"show_folder\" and\n",
  "    return sorted(k for k in keys if True and\n"),

 ("supervisor: a restart never names who kept the older copy",
  "packaging/windows/supervisor.py",
  "             progs[n].started_wall < changed]\n",
  "             progs[n].started_wall < 0]\n"),

 ("rack screen: Reload engine shown when not offered",
  "ltcplay/web/remote.html",
  "  $(\"cfg-reload\").hidden = !cn.offer || !mayRow(s, \"settings\");\n",
  "  $(\"cfg-reload\").hidden = false;\n"),

 ("rack screen: the settings note leaves out flamesafe's own restart",
  "ltcplay/web/remote.html",
  "    (r.flamesafe ? \", so for a changed flamesafe.json stop LTC Player and start it again.\" : \".\");\n",
  "    \".\";\n"),

 # Show stays Show (Jeff, 2026-10-28: "even when I press show mode, it
 # turns on programming mode").
 ("Show on the show audio: the engine's mode follows the player's forced hold", "ltcplay/session.py",
  "            asked = self.on_lost_arg\n        elif self.player is not None:",
  "            asked = self.player.on_lost\n        elif self.player is not None:"),

 ("Show or Programming: Rehearse (no output) reads as Show", "ltcplay/session.py",
  '        if self.no_output:\n            return "programming"\n        clk = self.clock',
  "        clk = self.clock"),

 ("Show or Programming: the show status always says Show", "ltcplay/session.py",
  '            "mode": self.run_mode(),',
  '            "mode": "show",'),

 ("Show on the show audio: the rack screen's Programming goes by the player's forced hold", "ltcplay/remote.py",
  '        if getattr(clk, "master", False) is True and \\\n                hasattr(s, "on_lost_arg"):',
  "        if False:"),

 ("old page: the Show or Rehearsal switch reads on_lost again", "ltcplay/web/index.html",
  "    const m = runningMode(s);",
  '    const m = s.on_lost === "hold" ? "rehearsal" : "show";'),

 ("old page: the engine's Show is not believed", "ltcplay/web/index.html",
  '  if(s && s.mode === "show") return "show";\n',
  ""), ("aim seen: the aim alert turns the deck's Start key to ACK",
  "ltcplay/streamdeck.py",
  "                     if isinstance(a, dict)\n                     and a.get(\"kind\") not in DISPLAY_ONLY)",
  "                     if isinstance(a, dict))"),


 # -- the pre-roll (preroll.py, Jeff 2026-10-28): the rig checked before the show, and Continue anyway --
 ('pre-roll: a check with no answer in time counts as passed', 'ltcplay/preroll.py',
  '                items.append(self._item(\n                    name, required, False,\n                    f"no answer within',
  '                items.append(self._item(\n                    name, required, True,\n                    f"no answer within'),

 ('pre-roll: a failed check raises no alert', 'ltcplay/preroll.py',
  '        if alerts and not manual:\n            self.raise_alert(r["alert"])',
  '        if False:\n            self.raise_alert(r["alert"])'),

 ('pre-roll: a failed run reads as passed', 'ltcplay/preroll.py',
  '            return self._finish(r, "failed" if bad else "passed", items,',
  '            return self._finish(r, "passed", items,'),

 ('pre-roll: Hold, Stop and Abort are not seen by the checks', 'ltcplay/preroll.py',
  '            if self._cancel != gen:\n                return True',
  '            if False:\n                return True'),

 ('pre-roll: the audio check needs no new buffer from the interface', 'ltcplay/preroll.py',
  '        if getattr(clk, "_seen_seq", None) != seq0 and \\',
  '        if True or getattr(clk, "_seen_seq", None) != seq0 and \\'),

 ('pre-roll: the flame check passes on an old status frame', 'ltcplay/preroll.py',
  '    while getattr(fstatus, "last_at", None) == t0:',
  '    while False:'),

 ('pre-roll: the video test does not take the screens to black first', 'ltcplay/preroll.py',
  '    r = conductor.preroll_video_dark()\n',
  '    r = __import__("ltcplay.conductor", fromlist=["done"]).done("skipped")\n'),

 ("pre-roll: the video test passes when MadMapper's position does not move", 'ltcplay/preroll.py',
  '    if span < 0.4 or rise <= 0 or any(b[1] < a[1] for a, b in',
  '    if span < 0.0 and any(b[1] < a[1] for a, b in'),

 ('pre-roll: the video test does not set the show bank following timecode', 'ltcplay/preroll.py',
  '        link.play(link.cfg.show_bank, wait=False)\n',
  ''),

 ('pre-roll: the video test does not park MadMapper on frame 0', 'ltcplay/preroll.py',
  '            for _ in range(3):\n                clk.preroll_frame(0)',
  '            for _ in range(0):\n                clk.preroll_frame(0)'),

 ("pre-roll: the scheduler checks the rig only at the show's time", 'ltcplay/schedule_service.py',
  '            if nxt is None or (nxt.start - now).total_seconds() > \\\n                    self.preroll.window_s:',
  '            if nxt is None or (nxt.start - now).total_seconds() > \\\n                    0:'),

 ('pre-roll: an acknowledged failure is not raised again at showtime', 'ltcplay/schedule_service.py',
  '            if pr.verdict(key) == "failed" and not pr.alert_open():',
  '            if False:'),

 ('pre-roll: a failed check lets the scheduler start the show', 'ltcplay/schedule_service.py',
  '            if v == "failed":\n                return True\n',
  '            if v == "failed":\n                return False\n'),

 ('pre-roll: a show due with no check starts unchecked', 'ltcplay/schedule_service.py',
  '            if v is None:\n                self._preroll_kick(n)\n            return True',
  '            return False'),

 ('pre-roll: Hold, Stop and Abort do not cancel the rig check', 'ltcplay/schedule_service.py',
  '        pr = self.preroll\n        if pr is not None:\n            pr.cancel(why)',
  '        pr = self.preroll\n        if pr is not None:\n            pass'),

 ('pre-roll: Start now skips the rig check', 'ltcplay/schedule_service.py',
  '            return self._preroll_for_start_now(who, screen, gen), gen',
  '            return None, gen'),

 ('pre-roll: the late allowance applies to every show', 'ltcplay/schedule.py',
  '        if tx.ev.late_show and tx.ev.late_show == s.n:',
  '        if tx.ev.late_show:'),

 ('pre-roll: the test timecode goes to every receiver, BEYOND too', 'ltcplay/clock.py',
  '            held = getattr(self.out, "send_held", None)\n            if not callable(held):\n                return False\n            return bool(held(',
  '            held = getattr(self.out, "send", None)\n            if not callable(held):\n                return False\n            return bool(held('),

 ('pre-roll: the test timecode is sent while a cue plays', 'ltcplay/clock.py',
  '            if not self._live or self._cue is not None or self.out is None:\n                return False\n            self._drop_held(',
  '            if not self._live or self.out is None:\n                return False\n            self._drop_held('),

 ('pre-roll: the test timecode and a held frame interleave', 'ltcplay/clock.py',
  '            self._drop_held("The pre-roll\'s video test")',
  '            pass'),

 ('pre-roll: the screens are blacked while the show is aborted', 'ltcplay/conductor.py',
  '            if self._latched:\n                return self._refused("Pre-roll video black",',
  '            if False:\n                return self._refused("Pre-roll video black",'),

 ('pre-roll: the alert is display only and never holds the show', 'ltcplay/alerts.py',
  'DISPLAY_ONLY = frozenset(("aim", "follow"))',
  'DISPLAY_ONLY = frozenset(("aim", "follow", "preroll"))'),

 ('pre-roll: a pixel controller that never answered a ping holds every show', 'ltcplay/web.py',
  '            if base is None or ip in base:',
  '            if True:'),

 ("pre-roll: a Continue anyway's MadMapper mute outlives its show", 'ltcplay/clock.py',
  '            if getattr(self.out, "muted", None):\n                # A Continue anyway\'s mute',
  "            if False:\n                # A Continue anyway's mute"),

 ('continue anyway: what it left off stays off after the show', 'ltcplay/fire_ice.py',
  '        if state not in ("SHOW", "PAUSED"):\n            self._degraded_over(off_seq)\n',
  ''),

 ('continue anyway: it can be pressed again for another show', 'ltcplay/schedule_service.py',
  '        if pr is not None and pr.continued is not None:\n            # Once per failed check',
  '        if False:\n            # Once per failed check'),

 ('continue anyway: a used failure can be continued again', 'ltcplay/preroll.py',
  '            if r is None or r["state"] != "failed" or r.get("consumed"):',
  '            if r is None or r["state"] != "failed":'),

 ('continue anyway: a failure of any age is enough', 'ltcplay/preroll.py',
  '                    self._clock() - r["done"] > CONTINUE_MAX_AGE_S:',
  '                    self._clock() - r["done"] > 1e12:'),

 ('continue anyway: a Stop does not clear a Continue not yet started', 'ltcplay/schedule_service.py',
  '            perf.continue_off = None',
  '            pass'),

 ('pre-roll: a Programming Run lets the scheduler start a show', 'ltcplay/schedule_service.py',
  '            if pr.skip():\n                # A Programming Run',
  '            if False:\n                # A Programming Run'),

 ('pre-roll: a broken check lets the show start', 'ltcplay/schedule_service.py',
  '            self._preroll_broken(f"{type(e).__name__}: {e}")\n            return True',
  '            return False'),

 ('pre-roll: a rig check that cannot be built raises no alert', 'ltcplay/web.py',
  '            httpd.remote.alerts.raise_("preroll", text)',
  '            pass'),

 ('continue anyway: the runner clears what a start just set', 'ltcplay/fire_ice.py',
  '                if self._off_seq != off_seq or (',
  '                if False and ('),

 ('pre-roll: a rig check that cannot be built leaves no stand-in', 'ltcplay/web.py',
  '            if httpd.schedule is not None:\n                httpd.schedule.preroll = pr\n',
  ''),

 ("pre-roll: the stand-in's Continue anyway leaves outputs on", 'ltcplay/preroll.py',
  '        if i.get("name") == STANDIN:',
  '        if False:'),

 ('continue anyway: a failed video is still faded up', 'ltcplay/conductor.py',
  '        if "video" not in off:\n            self._step(gen, "video", LIT,',
  '        if True:\n            self._step(gen, "video", LIT,'),

 ('continue anyway: failed lasers are lit through the gate', 'ltcplay/conductor.py',
  '        if "lasers" in self.outputs_off:\n            why = (',
  '        if False:\n            why = ('),

 ('continue anyway: the flame cues are released after the flame link failed', 'ltcplay/conductor.py',
  '        if "flames" in off:\n            self._restore_lasers(gen, progress)\n            progress.append(',
  '        if False:\n            self._restore_lasers(gen, progress)\n            progress.append('),

 ("continue anyway: MadMapper's bank is selected after its video failed", 'ltcplay/fire_ice.py',
  '        if self.mm is not None and "video" not in off:',
  '        if self.mm is not None:'),

 ('continue anyway: MadMapper still gets the show timecode after its video failed', 'ltcplay/clock.py',
  '            if self.muted and str(label).strip().lower() in self.muted:\n                continue\n',
  ''),

 ('continue anyway: what it leaves off lasts past its show', 'ltcplay/fire_ice.py',
  '        cont, self.continue_off = self.continue_off, None\n',
  '        cont = self.continue_off\n'),

 ('continue anyway: a tap is enough', 'ltcplay/preroll.py',
  '        return CONTINUE_HOLD_S <= held <= 30.0',
  '        return True'),

 ('continue anyway: an unsigned screen may continue', 'ltcplay/remote.py',
  '        if not self.users_active() or not who:\n            return 403, {"error": "Continue anyway needs a signed-in "',
  '        if False:\n            return 403, {"error": "Continue anyway needs a signed-in "'),

 ('continue anyway: it goes past an Abort', 'ltcplay/schedule_service.py',
  '        elif self._aborted():\n            why = "the show is aborted; press Reset first"',
  '        elif False:\n            why = "the show is aborted; press Reset first"'),

 ('continue anyway: the Delayed countdown keeps going', 'ltcplay/schedule_service.py',
  '                try:\n                    on_continue()\n',
  '                try:\n                    pass\n'),

 ('continue anyway: the choice is not journaled', 'ltcplay/schedule_service.py',
  '        self.journal_press(\n            who, screen, "continue anyway", "chosen",',
  '        (lambda *a, **k: None)(\n            who, screen, "continue anyway", "chosen",'),

 ("continue anyway: the alert hold's countdown is not cancelled", 'ltcplay/alerthold.py',
  '            if h["state"] == "counting":\n                h["state"], h["due"] = "cancelled", None\n                self._journal(f"The Delayed announcement was cancelled: "',
  '            if False:\n                h["state"], h["due"] = "cancelled", None\n                self._journal(f"The Delayed announcement was cancelled: "'),

 ('continue anyway: a network failure naming MadMapper leaves the video on', 'ltcplay/preroll.py',
  '        if i.get("name") in ("Network", "Timecode"):',
  '        if False:'),

 ("continue anyway: the next show's check does not end the Continue", 'ltcplay/preroll.py',
  "            if not manual:\n                # A new show's check: the last Continue anyway is over.\n                self.continued = None",
  '            if False:\n                self.continued = None'),
 # -- interface 2.0 (Jeff, 2026-10-28): /remote2 beside 1.0, the per-screen
 # switch, the default set on the show machine only, 1.0's own rules served,
 # and the SHOW tab's presses kept 1.0's --
 ("ui2: the show network cannot reach the 2.0 page", "ltcplay/web.py",
  "            or route in UI_ROUTES\n",
  ""),

 ("ui2: a screen's own choice of page is ignored", "ltcplay/web.py",
  '            if k == UI_COOKIE and v in ("1", "2"):',
  "            if False:"),

 ("ui2: a junk choice cookie picks 2.0", "ltcplay/web.py",
  '            if k == UI_COOKIE and v in ("1", "2"):\n                return int(v)',
  '            if k == UI_COOKIE:\n                return 2'),

 ("ui2: the show machine's default interface is ignored", "ltcplay/web.py",
  "            return 2 if rm is not None and rm.ui_default() == 2 else 1",
  "            return 1"),

 ("ui2: a link from another site's page switches the page", "ltcplay/web.py",
  "            return self._send(403, {\"error\": \"A switch from another site's \"\n"
  "                                             \"page was refused.\"})",
  "            pass"),

 ("ui2: /ui sets any value it is given", "ltcplay/web.py",
  '        if v not in ("1", "2"):\n            return self._send(400, {"error": "v has to be 1 or 2."})',
  '        if not v:\n            return self._send(400, {"error": "v has to be 1 or 2."})'),

 ("ui2: /remote-logic.js serves the whole of 1.0", "ltcplay/web.py",
  "    return page[a + len(LOGIC_OPEN):b]\n",
  "    return page\n"),

 ("ui2: the default interface is set from the network", "ltcplay/remote.py",
  "            if not ctx.local:\n                return 403, {\"error\": \"The default interface is set on the \"",
  "            if False:\n                return 403, {\"error\": \"The default interface is set on the \""),

 ("ui2: the default interface is read from the network", "ltcplay/remote.py",
  "        if name == UI_DEFAULT_ROUTE:\n            if not ctx.local:\n                return 403, {\"error\": \"Only on the show machine itself.\"}",
  "        if name == UI_DEFAULT_ROUTE:\n            if False:\n                return 403, {\"error\": \"Only on the show machine itself.\"}"),

 ("ui2: the default interface takes any value", "ltcplay/remote.py",
  "        if not isinstance(v, int) or isinstance(v, bool) or \\\n                v not in UI_VERSIONS:",
  "        if v is None:"),

 ("ui2: a hand-written ui_default that is not 1 or 2 is kept", "ltcplay/remote.py",
  "    out[\"ui_default\"] = v if (isinstance(v, int) and not isinstance(v, bool)\n                              and v in UI_VERSIONS) else 1",
  "    out[\"ui_default\"] = v if v is not None else 1"),

 ("ui2: setting the default interface needs no role", "ltcplay/users.py",
  '    "ui-default": "settings",',
  '    "ui-default": None,'),

 ("ui2 page: Abort fires on its first tap", "ltcplay/web/remote2.html",
  "  const step = abortTapStep2(ABORT_TAP, monoNow(), kind, $(\"abortbar\").classList.contains(\"tap1\"));\n  if(!step.fire){",
  "  const step = abortTapStep2(ABORT_TAP, monoNow(), kind, $(\"abortbar\").classList.contains(\"tap1\"));\n  if(false){"),

 ("ui2 page: Stop is sent with no confirm", "ltcplay/web/remote2.html",
  '  if(!(await confirmBox("Stop the show?",',
  '  if(false && !(await confirmBox("Stop the show?",'),

 ("ui2 page: lifting the finger does not let go of a hold to arm", "ltcplay/web/remote2.html",
  '        b.addEventListener(ev, () => { if(holding(i) && !HOLD.all) armStop("let go"); }));',
  '        b.addEventListener(ev, () => {}));'),

 ("ui2 page: the copied hold beats faster than 1.0's", "ltcplay/web/remote2.html",
  "    p.timer = setTimeout(() => armBeat(h, p), 100);",
  "    p.timer = setTimeout(() => armBeat(h, p), 50);"),

 ("ui2 page: one system to check is not named", "ltcplay/web/remote2.html",
  "  const text = probs.length === 1 ?",
  "  const text = false ?"),

 ("ui2 page: a system not set up here reads Check", "ltcplay/web/remote2.html",
  "  if(state !== \"bad\" && SYS_OFF.test(w.trim())) state = \"off\";\n",
  ""),

 ("ui2 day: Space fires the purge on any DAY step", "ltcplay/web/remote2.html",
  "  if(TAB !== \"day\" || DAY_STEP !== \"purge\") return;",
  "  if(TAB !== \"day\") return;"),

 ("ui2 day: letting go of the cleaning hold keeps it going", "ltcplay/web/remote2.html",
  "      $(\"wc-hold\").addEventListener(ev, () => { if(WC_HOLD) wcLetGo(\"you let go before the hold was complete\"); });\n    }\n    $(\"wc-hold\").addEventListener(\"contextmenu\", e => e.preventDefault());\n    $(\"wc-stop\")",
  "      $(\"wc-hold\").addEventListener(ev, () => { if(false) wcLetGo(\"you let go before the hold was complete\"); });\n    }\n    $(\"wc-hold\").addEventListener(\"contextmenu\", e => e.preventDefault());\n    $(\"wc-stop\")"),

 ("ui2 day: leaving DAY keeps this screen's purge selection", "ltcplay/web/remote2.html",
  "    MO_OPEN = false; clearTimeout(MO_TIMER); fireStop(\"the page was left\");\n    purgeDrop();\n  }",
  "    MO_OPEN = false; clearTimeout(MO_TIMER); fireStop(\"the page was left\");\n  }"),

 ("ui2 day: leaving the Purge step keeps this screen's selection", "ltcplay/web/remote2.html",
  "  if(DAY_STEP === \"purge\"){ fireStop(\"another step was chosen\"); purgeDrop(); }",
  "  if(DAY_STEP === \"purge\"){ fireStop(\"another step was chosen\"); }"),

 ("ui2 day: a double tap on Tick ticks twice", "ltcplay/web/remote2.html",
  "      if(DAY_TICK_BUSY || monoNow() - DAY_TICK_AT < DAY_TICK_QUIET_MS) return;\n",
  ""),

 ("ui2 day: Space on a focused group button presses it instead of firing",
  "ltcplay/web/remote2.html",
  "  if(e.target && /^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) return;\n  e.preventDefault();",
  "  if(e.target && e.target.closest && e.target.closest(\"button\")) return;\n  if(e.target && /^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) return;\n  e.preventDefault();"),

 ("ui2 day: letting go of Space presses the focused group button",
  "ltcplay/web/remote2.html",
  "     !(e.target && /^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName))) e.preventDefault();",
  "     false) e.preventDefault();"),

 ("ui2 day: the Purge step dropping out keeps this screen's selection",
  "ltcplay/web/remote2.html",
  "    if(DAY_STEP === \"purge\"){ fireStop(\"the Purge step went\"); purgeDrop(); }",
  "    if(DAY_STEP === \"purge\"){ fireStop(\"the Purge step went\"); }"),

 ("ui2 page: Programming playback firing flames is not pinned",
  "ltcplay/web/remote2.html",
  "  if(pf.on === true) out.push({key:\"pf\",",
  "  if(false) out.push({key:\"pf\","),

 ("ui2 page: the Programming flames pin's Stop sends nothing",
  "ltcplay/web/remote2.html",
  "  } else if(k === \"pf\"){\n    // 1.0's Hold (its b-hold: press(\"hold\")), the same route and body.\n    press(\"hold\");",
  "  } else if(k === \"pf\"){\n    // 1.0's Hold (its b-hold: press(\"hold\")), the same route and body.\n"),

 ("ui2 day: Space fires the purge over an open sheet", "ltcplay/web/remote2.html",
  "  if(SHEET_DONE || !$(\"sheet\").hidden) return;\n",
  ""),

 ("ui2 day: Next marks Purge done after a look", "ltcplay/web/remote2.html",
  "[\"fuel\", \"lasers\", \"locks\", \"locks-shut\"].includes(cur.key)",
  "[\"fuel\", \"purge\", \"lasers\", \"locks\", \"locks-shut\"].includes(cur.key)"),

 ("ui2 page: announcements with no output picked read Off", "ltcplay/web/remote2.html",
  "const SYS_OFF = /^(none|none set up|not set up|stopped|",
  "const SYS_OFF = /^(none|none set up|not set up|pick an output|stopped|"),

 ("ui2 day: the copied purge fire beats faster than 1.0's", "ltcplay/web/remote2.html",
  "    f.timer = setTimeout(() => fireBeat(f), 100);",
  "    f.timer = setTimeout(() => fireBeat(f), 50);"),

 ("ui2 day: a tick goes in without a name", "ltcplay/web/remote2.html",
  "      if(!dayWho(MO.checklists)){ $(\"mo-cmsg\").textContent = \"Pick who is ticking first.\"; return; }",
  "      dayWho(MO.checklists);"),

 ("ui2: 2.0 is served without 1.0's rules written in", "ltcplay/web.py",
  "    if logic is not None and LOGIC_TAG in page:\n",
  "    if False:\n"),

 ("ui2 page: Abort leans on 1.0's rules", "ltcplay/web/remote2.html",
  "  const kind = abortTapKind2(e);",
  "  const kind = abortTapKind(e);"),

 ("ui2 page: a page whose rules did not load says nothing", "ltcplay/web/remote2.html",
  "window.addEventListener(\"load\", () => { if(!ui2Whole()) $(\"broken\").hidden = false; });\nsetInterval(() => { if(!ui2Whole()) $(\"broken\").hidden = false; }, 1000);",
  ""),

 ("ui2 page: a running cleaning is not pinned", "ltcplay/web/remote2.html",
  "  if(wc && wc.state && wc.state !== \"idle\") out.push({key:\"wc\",",
  "  if(false) out.push({key:\"wc\","),

 ("ui2 page: a live mode reads READY", "ltcplay/web/remote2.html",
  "  if(pins.length) return {cls:\"bad\", icon:\"flame\", text:pins[0].pill};\n",
  ""),

 ("ui2 page: a fault reads READY", "ltcplay/web/remote2.html",
  "  if((items || []).some(i => i.tone === \"bad\")) return {cls:\"bad\", icon:\"warn\", text:\"NOT READY\"};\n",
  ""),

 ("ui2 page: the slot's button acts on whatever shows at the lift", "ltcplay/web/remote2.html",
  "  if(down && (down.key !== ALERT_KEY || down.act !== $(\"al-act\").dataset.act)){",
  "  if(false){"),

 ("ui2 page: no contact leaves the old alert slot", "ltcplay/web/remote2.html",
  "  if(isSt) drawSlotStale(); else",
  "  if(false) drawSlotStale(); else"),

 ("ui2 page: a refused Disarm is said only in the footer", "ltcplay/web/remote2.html",
  "    if(dis) disarmFailed(",
  "    if(false) disarmFailed("),
 ("ui2 page: a Disarm that did not go is said as refused",
  "ltcplay/web/remote2.html",
  "    say(dis ? \"Disarm NOT done. Use Deck or e-stop.\" : short8(",
  "    say(false ? \"Disarm NOT done. Use Deck or e-stop.\" : short8("),

 ("ui2 page: a failed Stop disarm does not say e-stop", "ltcplay/web/remote2.html",
  "  if(bad.some(i => i.key === \"abort\" || i.key === \"stop\")) return",
  "  if(bad.some(i => i.key === \"abort\")) return"),

 ("ui2 page: letting go of Continue anyway early still continues", "ltcplay/web/remote2.html",
  '  if(PR_HOLD){ prHoldEnd(); say("Let go too early: nothing done."',
  '  if(false){ prHoldEnd(); say("Let go too early: nothing done."'),

 ("ui2 page: Continue anyway is held for less than 1.0's hold", "ltcplay/web/remote2.html",
  "  }, PR_HOLD_MS + 100);",
  "  }, PR_HOLD_MS - 300);"),

 ("ui2 page: the cleaning's Stop is not sent", "ltcplay/web/remote2.html",
  '    try{ const j = await post("/api/remote/cleaning-stop", actor({}));',
  '    try{ const j = {};'),

 ("ui2 page: a stale page keeps Hold to arm on the group buttons", "ltcplay/web/remote2.html",
  "    if(isSt) setArmMode(el, armOn ? \"disarm\" : \"lamp\", \"unknown\");",
  ""),

 ("ui2 page: a stale page keeps the big button live", "ltcplay/web/remote2.html",
  "  else if(isHeld(st)) L = {mode:\"resume\", label:\"Resume\", icon:\"play\", tone:\"green\", off:!fresh || !sch.attached,",
  "  else if(isHeld(st)) L = {mode:\"resume\", label:\"Resume\", icon:\"play\", tone:\"green\", off:!sch.attached,"),

]


class _PerThread:
    """A list each thread sees its own copy of, so side-by-side workers
    (--jobs) never read each other's last result."""

    def __init__(self, start):
        self._start = list(start)
        self._tl = threading.local()

    def _v(self):
        if not hasattr(self._tl, "v"):
            self._tl.v = list(self._start)
        return self._tl.v

    def __getitem__(self, i):
        return self._v()[i]

    def __setitem__(self, i, x):
        self._v()[i] = x

    def __iter__(self):
        return iter(self._v())

    def __len__(self):
        return len(self._v())


# What the last suite run failed on, so a surprising result can be read.
_LAST_FAILS = _PerThread([])
# The last run: [seconds it took, whether it was stopped at its limit].
_LAST_RUN = _PerThread([0.0, False])
# The last run: [the test a fail-fast run stopped in ("" when it did not
# say), whether it was a probe that passed].
_LAST_STOP = _PerThread(["", False])

# Two limits, for two different questions.
#
# SUITE_TIMEOUT is the BASELINE's limit: the suite with nothing mutated. It
# only has to catch a runner that is broken, so it is generous. A baseline
# that hits it is an infrastructure failure, never a missed mutation; it is
# run once more before the shard gives up, and says so in its own words.
# (It was one fixed 300 s for everything: once the suite itself took 270 to
# 300 s, loaded runners stopped clean baselines, PR #87 shards 10 and 13.)
#
# A MUTANT's limit is loop protection: a mutation that makes the suite hang
# (an endless loop, a wait nothing ends) is caught by being stopped. Its
# limit is measured, not fixed: MUTANT_FACTOR times the baseline this shard
# just ran on this runner, never under MUTANT_FLOOR (the old 300 s, so no
# mutant is stopped sooner than before) and never over MUTANT_CEILING (so a
# hung mutant costs a bounded time, twice over in CI).
SUITE_TIMEOUT = 1800
MUTANT_FACTOR = 3
MUTANT_FLOOR = 300
MUTANT_CEILING = 1800


def mutant_timeout(baseline_seconds):
    """Seconds one mutant's suite may run, from how long the unmutated suite
    took on this runner."""
    return int(min(MUTANT_CEILING,
                   max(MUTANT_FLOOR, MUTANT_FACTOR * baseline_seconds)))


def _what_failed(out, limit=10):
    """The lines of a suite's output worth reading when it was red: its FAIL
    lines first, then an uncaught exception's last lines, and failing both
    the end of the output. The old rule, any line with "Error" in it, picked
    up test headings that merely mention an error and pushed the FAIL line
    off the end (PR #49 shard 25's baseline annotation showed a heading)."""
    lines = [l.rstrip() for l in out.splitlines()]
    fails = [l.strip() for l in lines if l.lstrip().startswith("FAIL")]
    if fails:
        return fails[:limit]
    if any(l.startswith("Traceback (most recent call last)") for l in lines):
        start = max(i for i, l in enumerate(lines)
                    if l.startswith("Traceback (most recent call last)"))
        tail = [l for l in lines[start:] if l.strip()]
        return ["uncaught exception in the suite:"] + tail[-(limit - 1):]
    tail = [l for l in lines if l.strip()][-limit:]
    return ["the suite ended red without a FAIL line; its last lines:"] + tail


# The settings selftest.py reads for mutate.py's fast path (see the top of
# selftest.py). Every run sets them itself, so none can leak in from the
# environment into a baseline.
_FAST_ENV = ("LTC_SELFTEST_FAILFAST", "LTC_SELFTEST_FIRST", "LTC_SELFTEST_PROBE")
PROBE_EXIT = 3


def _stop_in(out):
    """The test a fail-fast run stopped in, from its own line, or failing
    that (a run stopped at its limit) the test it was running."""
    stop = running = ""
    for l in out.splitlines():
        for pre in ("  fast path: stopped in ", "  fast path: running "):
            if l.startswith(pre) and l[len(pre):].split():
                if "stopped" in pre:
                    stop = l[len(pre):].split()[0]
                else:
                    running = l[len(pre):].split()[0]
    return stop or running


def run_suite(limit=None, failfast=False, first=None, probe=False,
              root=None, extra_env=None):
    """True when the suite is green. `limit` is seconds (SUITE_TIMEOUT when
    None); _LAST_RUN says how long it took and whether it was stopped.

    failfast, first and probe are for mutated runs and probes only (see
    selftest.py): none of them can turn a red suite green. A baseline passes
    none of them and runs every test in file order. root and extra_env are a
    --jobs worker's own tree and folders."""
    limit = SUITE_TIMEOUT if limit is None else limit
    env = dict(os.environ)
    env.update(extra_env or {})
    for k in _FAST_ENV:
        env.pop(k, None)
    if failfast:
        env["LTC_SELFTEST_FAILFAST"] = "1"
    if first:
        env["LTC_SELFTEST_FIRST"] = first
    if probe:
        env["LTC_SELFTEST_PROBE"] = "1"
    started = time.time()
    _LAST_RUN[:] = [0.0, False]
    _LAST_STOP[:] = ["", False]
    try:
        r = subprocess.run([sys.executable, "selftest.py"], cwd=root or HERE,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=limit, env=env)
    except subprocess.TimeoutExpired as e:
        _LAST_RUN[:] = [time.time() - started, True]
        # A suite that does not finish is not a green suite. Under a
        # mutation that counts as caught (the mutation broke a test so
        # badly it hung); with nothing mutated it is a failed baseline.
        # Either way the sweep reports it rather than crashing the shard,
        # which is what CI run 36206230939 did.
        out = ((e.stdout or b"") if isinstance(e.stdout, (bytes, str))
               else b"")
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        _LAST_STOP[0] = _stop_in(out) if failfast else ""
        _LAST_FAILS[:] = [f"FAIL  the suite did not finish inside "
                          f"{limit} s (TimeoutExpired)"] + \
            [l.strip() for l in out.splitlines()
             if l.lstrip().startswith("FAIL")][:5]
        return False
    _LAST_RUN[:] = [time.time() - started, False]
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode == 0:
        _LAST_FAILS[:] = []
        return True
    if probe and r.returncode == PROBE_EXIT and any(
            l.startswith("  fast path: probe passed when run first: ")
            for l in out.splitlines()):
        _LAST_STOP[1] = True
    _LAST_STOP[0] = _stop_in(out) if failfast else ""
    _LAST_FAILS[:] = _what_failed(out)
    return False


LOCK = os.path.join(HERE, ".mutating")


def _read(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    _drop_bytecode(path)


def _drop_bytecode(path):
    """Delete the cached bytecode of a .py file just written.

    Python trusts a .pyc whose recorded source mtime (whole seconds) and
    size match the file. Most mutations are the same size as the line they
    replace (0.0 -> 1.0, True -> False), so a mutant written in the same
    second the original was last compiled ran the ORIGINAL code: the suite
    stayed green, twice, and the mutation read NOT CAUGHT. That is how
    "conductor: the video fade to black fades up instead" survived nightly
    run 40469889985 shard 15 (a catcher probe compiled the restored file
    under a second before the mutant was written), though five tests fail
    under it. A restore can hit the same trap the other way, and leave a
    mutant's bytecode running against restored source."""
    if not path.endswith(".py"):
        return
    import glob
    import importlib.util
    pyc = [importlib.util.cache_from_source(path)]
    stem = os.path.splitext(os.path.basename(path))[0]
    pyc += glob.glob(os.path.join(os.path.dirname(path), "__pycache__",
                                  glob.escape(stem) + ".*.pyc"))
    for p in set(pyc):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


# ------------------------------------------------------- reporting to CI --
# A shard that ends red has to say why somewhere that can be read without
# the job log: PR #46's shards 14 and 28 ran for three hours and ended with
# "Process completed with exit code 1" and nothing else, because every
# verdict below was printed to the log only. So every way out of this file
# with a non-zero code now goes through fail(): it names the shard, the kind
# of failure and the first lines that matter as a GitHub annotation, and
# main() writes the run's table to the job summary on every exit. Outside
# Actions both are silent and the printed lines are as they were.

CI = os.environ.get("GITHUB_ACTIONS") == "true"
# GitHub keeps ten error annotations a step; the last one says there are
# more rather than being one of them.
ANNOTATIONS_MAX = 10
_annotated = [0]


class _Report:
    """Everything the job summary says about this run."""

    def reset(self):
        self.where = "every mutation"
        self.started = time.time()
        self.due = 0
        self.caught = []          # names
        self.missed = []          # (name, file)
        self.flaky = []           # (name, first failure's lines)
        self.setup_fails = []     # (name, what was wrong)
        self.expected = []        # (name, reason)
        self.now_caught = []      # names
        self.stale = []           # names not in MUTATIONS
        self.baseline = "not run"
        self.restored = "not checked"
        self.limits = "not measured"  # baseline took / mutant limit
        self.failures = []        # (kind, lines), one per problem
        return self

    def table(self, code):
        mins = (time.time() - self.started) / 60
        took = f"{mins:.0f} min" if mins >= 1 else f"{mins * 60:.0f} s"
        result = "ok" if code == 0 else f"FAIL (exit {code})"
        rows = [("shard", self.where), ("mutations", str(self.due)),
                ("caught", str(len(self.caught))),
                ("missed", str(len(self.missed))),
                ("expected misses", str(len(self.expected))),
                ("flaky", str(len(self.flaky))),
                ("setup failed", str(len(self.setup_fails))),
                ("baseline", self.baseline), ("tree restored", self.restored),
                ("baseline time / mutant limit", self.limits),
                ("time", took), ("result", result)]
        out = [f"### mutate {self.where}: {result}, {took}", "",
               "| " + " | ".join(r[0] for r in rows) + " |",
               "|" + "---|" * len(rows),
               "| " + " | ".join(r[1] for r in rows) + " |", ""]
        if self.failures:
            out.append("**Why it is red**")
            out.append("")
            for kind, lines in self.failures:
                out.append(f"- **{kind}**")
                for l in lines[:10]:
                    out.append(f"  - `{l}`" if l.strip() else "")
            out.append("")
        for title, items in (
                ("Missed", [f"`{rel}`: {n}" for n, rel in self.missed]),
                ("Flaky (red once, then green)",
                 [f"{n}: " + " | ".join(w) for n, w in self.flaky]),
                ("Setup failed", [f"{n}: {w}" for n, w in self.setup_fails]),
                ("Now caught, delete from the list", list(self.now_caught)),
                ("Expected misses",
                 [f"{n} ({why})" for n, why in self.expected])):
            if items:
                out.append(f"**{title}** ({len(items)})")
                out.append("")
                out.extend(f"- {i}" for i in items)
                out.append("")
        return "\n".join(out) + "\n"


REPORT = _Report().reset()


def _esc(s, prop=False):
    s = s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return s.replace(":", "%3A").replace(",", "%2C") if prop else s


def annotation(title, lines, limit=10, size=3000):
    """One ::error line: the first `limit` of `lines`, at most `size`
    characters before escaping (GitHub cuts a message at 4096)."""
    lines = list(lines)
    text = "\n".join(l.rstrip() for l in lines[:limit])
    if len(text) > size:
        text = text[:size - 12] + "\n... (cut)"
    if len(lines) > limit:
        text += f"\n... and {len(lines) - limit} more lines in the log"
    return f"::error title={_esc(title[:200], prop=True)}::{_esc(text)}"


def fail(kind, lines=()):
    """Record one reason this run is red; in Actions, also say it where it
    can be read without the log."""
    lines = [str(l).rstrip() for l in lines]
    REPORT.failures.append((kind, lines))
    if not CI:
        return
    _annotated[0] += 1
    if _annotated[0] < ANNOTATIONS_MAX:
        print(annotation(f"mutate {REPORT.where}: {kind}", lines))
    elif _annotated[0] == ANNOTATIONS_MAX:
        print(annotation(f"mutate {REPORT.where}: more problems",
                         ["This step's annotations are full. The job summary "
                          "and the mutate log artifact list every one."]))


def _finish(code):
    """Write the run's table to the job summary, whatever the exit."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(REPORT.table(code))
        except OSError as e:
            print(f"could not write the job summary: {e}")
    return code


def main():
    REPORT.reset()
    _annotated[0] = 0
    # While this runs, the working tree is deliberately broken. Anything else
    # that reads it — a capture, a soak, a live run — is measuring a mutant and
    # will produce a confident, entirely false result. The lock exists because
    # that has already happened once.
    if os.path.exists(LOCK):
        why = (f"{LOCK} exists: another mutation run is in progress, or one "
               f"died and left the tree broken. Check `git diff` or the "
               f"selftest before deleting it.")
        print(why)
        fail("another run holds the tree", [why])
        return _finish(3)
    open(LOCK, "w").write(str(os.getpid()))
    try:
        code = _run()
    except Exception:
        # The last lines of the traceback are the ones that say what broke.
        text = traceback.format_exc()
        print(text, end="")
        fail("unexpected exception",
             [l for l in text.rstrip().splitlines() if l.strip()][-10:])
        code = 1
    finally:
        os.remove(LOCK)
    return _finish(code)


OPTS = {"changed": None, "files": None, "list": False, "catchers": None,
        "failfast": True, "hints": None, "write_catchers": None, "jobs": 1}


def _args(argv):
    """Name filters, plus the CI options. Anything else is a filter, as it
    always was."""
    wants, shard, expected = [], None, None
    OPTS.update(changed=None, files=None, list=False, catchers=None,
                failfast=True, hints=None, write_catchers=None, jobs=1)
    it = iter(argv)
    for a in it:
        if a == "--shard":
            i, n = next(it).split("/")
            shard = (int(i), int(n))
        elif a == "--expected":
            expected = next(it)
        elif a == "--changed":
            OPTS["changed"] = next(it)
        elif a == "--files":
            OPTS["files"] = next(it)
        elif a == "--catchers":
            OPTS["catchers"] = next(it)
        elif a == "--list":
            OPTS["list"] = True
        elif a == "--no-failfast":
            OPTS["failfast"] = False
        elif a == "--hints":
            OPTS["hints"] = next(it)
        elif a == "--write-catchers":
            OPTS["write_catchers"] = next(it)
        elif a == "--jobs":
            OPTS["jobs"] = max(1, int(next(it)))
        else:
            wants.append(a.lower())
    return wants or None, shard, expected


def _load_expected(path):
    """{name: reason} of the misses expected ON THIS OS.

    One entry per line: `where | exact mutation name | reason`, where `where`
    is `all` or `windows`. Blank lines and # comments are ignored."""
    here_os = "windows" if sys.platform == "win32" else "posix"
    out, unknown = {}, []
    names = {m[0] for m in MUTATIONS}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            where, name, reason = (x.strip() for x in line.split("|", 2))
            if name not in names:
                unknown.append(name)
            if where == "all" or where == here_os:
                out[name] = reason
    return out, unknown


# ---------------------------------------------------------- affected mode --
# The per-PR gate runs only the mutations whose result the diff can have
# changed. A mutation's result depends on two things: the file it breaks and
# the tests that notice. So a mutation runs when its target file changed, or
# the mutation itself is new or edited, or its expected-miss line changed, or
# the test that caught it last time changed (when a catchers file says
# which; without one, a deleted test means the whole list). Any change to a
# file the flame, laser, Abort or arming paths run through also re-runs the
# whole of flamesafe, the conductor and the flame link, whatever else
# changed: those are the guarantees that must never go quiet. Everything
# else (a doc, a launcher, the brand, an installer script) selects nothing,
# and a shard with nothing to run returns at once. The nightly sweep and
# every release tag run the whole list, so a loss this misses (a test
# weakened without being renamed, a change in one file that silences a test
# of another) shows within a day, and never reaches a tag unseen.

# A change to any of these re-runs every SAFETY_CORE mutation.
SAFETY_TRIGGERS = ("flamesafe/", "ltcplay/conductor.py", "ltcplay/flamelink.py",
                   "ltcplay/morning.py", "ltcplay/lasertest.py",
                   "ltcplay/laserarm.py", "ltcplay/beyondtc.py",
                   "ltcplay/streamdeck.py", "ltcplay/schedule_service.py",
                   "ltcplay/schedule.py", "ltcplay/beyond.py",
                   "ltcplay/madmapper.py", "ltcplay/devices.py",
                   "ltcplay/trigger.py", "ltcplay/output.py",
                   "ltcplay/player.py", "ltcplay/session.py",
                   "ltcplay/clock.py", "ltcplay/announce.py",
                   "ltcplay/showaudio.py", "ltcplay/onlyone.py",
                   # The rig check before a show decides when it starts.
                   "ltcplay/preroll.py")
# The mutations that then always run: the safety program, the conductor
# (Hold, Resume, Abort, the gates) and the link between them.
SAFETY_CORE = ("flamesafe/", "ltcplay/conductor.py", "ltcplay/flamelink.py")
# A change here changes what the whole suite means: run everything.
HARNESS = ("test_show_fixtures.py",)


def _git(*args, base=None):
    r = subprocess.run(["git"] + list(args), cwd=HERE, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def _git_changed(base):
    """Files that differ between the work tree and the merge base of BASE
    and HEAD (so a PR is judged on its own commits, not on what main did
    since it branched). Uncommitted edits count too."""
    try:
        point = _git("merge-base", base, "HEAD").strip() or base
    except RuntimeError:
        point = base
    names = _git("diff", "--name-only", "-z", point).split("\0")
    return point, sorted({n for n in names if n})


def _base_text(base, path):
    """The file at BASE, or None when it did not exist there."""
    try:
        return _git("show", f"{base}:{path}")
    except RuntimeError:
        return None


def _mutations_in(text):
    """The MUTATIONS list of a mutate.py source text, parsed and never run,
    as {name: (file, old, new)}. Raises if it is not a plain list of
    constant 4-tuples (the CI workflow checks the same shape)."""
    import ast
    tree = ast.parse(text)
    lists = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == "MUTATIONS"
                     for t in n.targets)]
    if len(lists) != 1 or not isinstance(lists[0].value, ast.List):
        raise ValueError("expected exactly one plain `MUTATIONS = [...]`")
    out = {}
    for e in lists[0].value.elts:
        if not (isinstance(e, ast.Tuple) and len(e.elts) == 4
                and all(isinstance(x, ast.Constant) for x in e.elts)):
            raise ValueError(f"the entry at line {e.lineno} is not a "
                             f"constant (name, file, old, new)")
        name, rel, old, new = (x.value for x in e.elts)
        out[name] = (rel, old, new)
    return out


def _tests_in(text):
    """{test function name: its source} for a selftest.py text."""
    import ast
    tree = ast.parse(text)
    lines = text.splitlines()
    return {n.name: "\n".join(lines[n.lineno - 1:n.end_lineno])
            for n in tree.body if isinstance(n, ast.FunctionDef)
            and n.name.startswith("test_")}


def _expected_names(text):
    names = set()
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line.count("|") >= 2:
            names.add(line.split("|", 2)[1].strip())
    return names


def _load_catchers(path):
    """{mutation name: test that caught it}, or {} when the file is missing
    or unreadable (then a changed test is judged without it)."""
    import json
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return {str(k): str(v) for k, v in d.items()}
    except (OSError, ValueError, AttributeError):
        return {}


def _save_catchers(path, learned):
    """Merge {mutation name: test} into the catchers file at `path`, one
    entry a line, sorted, so a regenerated file diffs line by line."""
    merged = _load_catchers(path)
    merged.update(learned)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(merged, fh, indent=0, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def select_affected(changed, base=None, catchers=None):
    """(names to run or None for everything, reasons).

    `changed` is the list of changed paths (repo relative, forward
    slashes). `base` is the git revision to read the old mutate.py,
    selftest.py and expected-miss list from; without it (--files) those
    three count as changed in full when they are in the list."""
    reasons = []
    chosen = set()
    by_file = {}
    for name, rel, _old, _new in MUTATIONS:
        by_file.setdefault(rel, set()).add(name)

    def every(why):
        reasons.append(f"EVERYTHING: {why}")
        return None, reasons

    for p in changed:
        if p in by_file:
            chosen |= by_file[p]
            reasons.append(f"{p}: its {len(by_file[p])} mutations")
    def safety(p):
        # Code only: CONTRACT.md and the example config prove nothing by
        # changing, and the config's own tests run with flamesafe's suite.
        return p.startswith(SAFETY_TRIGGERS) and p.endswith(".py")

    if any(safety(p) for p in changed):
        core = {n for n, rel, _o, _n in MUTATIONS if rel.startswith(SAFETY_CORE)}
        hit = sorted(p for p in changed if safety(p))
        new = core - chosen
        chosen |= core
        reasons.append(f"a safety file changed ({', '.join(hit)}): every "
                       f"flamesafe, conductor and flame link mutation "
                       f"({len(new)} more)")
    if any(p in HARNESS for p in changed):
        return every("the shared test fixtures changed")

    if "mutate.py" in changed:
        if base is None:
            return every("mutate.py changed and there is no base to compare")
        old_text = _base_text(base, "mutate.py")
        try:
            old = _mutations_in(old_text) if old_text else {}
        except (ValueError, SyntaxError) as e:
            return every(f"the old mutate.py could not be read ({e})")
        edited = {n for n, rel, o, nw in MUTATIONS if old.get(n) != (rel, o, nw)}
        chosen |= edited
        reasons.append(f"mutate.py: {len(edited)} new or edited mutations")

    if "mutate_expected_misses.txt" in changed:
        here = _expected_names(_read(os.path.join(HERE, "mutate_expected_misses.txt")))
        there = _expected_names(_base_text(base, "mutate_expected_misses.txt")) \
            if base else set()
        listed = (here | there) & {n for n, *_ in MUTATIONS}
        chosen |= listed
        reasons.append(f"the expected-miss list: its {len(listed)} mutations")

    if "selftest.py" in changed or "flamesafe/test_flamesafe.py" in changed:
        if base is None:
            return every("the suite changed and there is no base to compare")
        old_text = _base_text(base, "selftest.py")
        try:
            old_tests = _tests_in(old_text) if old_text else {}
            new_tests = _tests_in(_read(os.path.join(HERE, "selftest.py")))
        except SyntaxError as e:
            return every(f"selftest.py could not be parsed ({e})")
        gone = sorted(set(old_tests) - set(new_tests))
        altered = sorted(n for n in old_tests if n in new_tests
                         and old_tests[n] != new_tests[n])
        if "flamesafe/test_flamesafe.py" in changed:
            # Run by test_flamesafe_in_its_own_process: that is the test
            # that changed, as far as this sweep can tell.
            altered.append("test_flamesafe_in_its_own_process")
        if gone and not catchers:
            return every(f"{len(gone)} test(s) deleted or renamed "
                         f"({', '.join(gone[:3])}{'...' if len(gone) > 3 else ''}) "
                         f"and no catchers file says what they guarded")
        if catchers:
            touched = set(gone) | set(altered)
            # A mutation whose last catcher changed, or that no catcher is
            # recorded for (never swept with the file, or a new entry).
            dep = {n for n, *_ in MUTATIONS
                   if catchers.get(n) in touched or n not in catchers}
            chosen |= dep
            reasons.append(f"selftest.py: {len(gone)} test(s) gone, "
                           f"{len(altered)} changed; {len(dep)} mutations "
                           f"they caught last time, or that no sweep has "
                           f"recorded a catcher for")
        else:
            reasons.append(f"selftest.py: {len(altered)} test(s) changed, "
                           f"none deleted; the nightly sweep re-proves what "
                           f"they catch in files this change did not touch")
    return chosen, reasons


def _run():
    wants, shard, expected_file = _args(sys.argv[1:])
    if shard:
        REPORT.where = f"shard {shard[0]}/{shard[1]}"
    expected, unknown = ({}, [])
    if expected_file:
        expected, unknown = _load_expected(expected_file)
    only = None
    if OPTS["changed"] or OPTS["files"] is not None:
        base = None
        if OPTS["changed"]:
            try:
                base, changed = _git_changed(OPTS["changed"])
            except RuntimeError as e:
                print(f"cannot read the diff: {e}")
                fail("cannot read the diff", [str(e)])
                return 2
        else:
            changed = sorted({p.strip().replace("\\", "/")
                              for p in OPTS["files"].split(",") if p.strip()})
        print(f"changed ({len(changed)} files"
              f"{', against ' + base[:10] if base else ''}):")
        for p in changed:
            print(f"  {p}")
        catchers = _load_catchers(OPTS["catchers"]) if OPTS["catchers"] else {}
        only, reasons = select_affected(changed, base, catchers)
        for r in reasons:
            print(f"  -> {r}")
        if only is None:
            print(f"running every mutation ({len(MUTATIONS)})")
        else:
            print(f"running {len(only)} of {len(MUTATIONS)} mutations")
            if not only:
                print("nothing this change can have broken is mutated "
                      "here: nothing to run")
                return 0
    if OPTS["list"]:
        for index, (name, rel, _o, _n) in enumerate(MUTATIONS):
            if only is not None and name not in only:
                continue
            if wants and not any(w in name.lower() for w in wants):
                continue
            if shard and index % shard[1] != shard[0]:
                continue
            print(f"  would run  {rel}: {name}")
        return 0
    todo = [(name, rel, old, new)
            for index, (name, rel, old, new) in enumerate(MUTATIONS)
            if (only is None or name in only)
            and not (wants and not any(w in name.lower() for w in wants))
            and not (shard and index % shard[1] != shard[0])]
    REPORT.due = len(todo)
    if shard:
        if only is not None and not todo:
            print(f"shard {shard[0]}/{shard[1]}: none of the selected "
                  f"mutations fall in this shard: nothing to run")
            return 0
        print(f"shard {shard[0]}/{shard[1]}: {len(todo)} mutations to run")
    # Prove the tree is clean BEFORE breaking it on purpose. A sweep that is
    # killed (a foreground timeout, a closed terminal) skips its restore and
    # leaves a mutation behind; the next sweep then measures that mutant and
    # blames whichever mutation it happens to be applying. Both of those have
    # already happened here.
    #
    # With --jobs N each worker has its own copy of the tree, and each copy
    # runs its own whole baseline, every check, before any mutation is
    # applied to it: a copy that is red for any reason of its own (a file
    # the copy lacks, a folder it cannot write) would otherwise make every
    # mutation it runs look caught.
    jobs = max(1, min(OPTS["jobs"], len(todo)))
    base_dir = tempfile.mkdtemp(prefix="ltm") if jobs > 1 else None
    try:
        if base_dir:
            print(f"{jobs} workers, each in its own copy of the tree")
            workers = [_worker_tree(base_dir, n) for n in range(jobs)]
        else:
            workers = [(HERE, None)]
        return _sweep(todo, workers, expected, unknown, expected_file)
    finally:
        if base_dir:
            shutil.rmtree(base_dir, ignore_errors=True)


def _worker_tree(base, n):
    """(tree, env) for --jobs worker n: a private copy of the tree, with its
    own home and app-data folders. A mutation edits a file in place, and the
    suite takes a machine-wide output lock (found through the home folder,
    or LOCALAPPDATA on Windows), so two workers can share neither a tree nor
    a home.

    The temp folder stays the machine's own: each suite makes one private
    folder inside it and never looks at the rest (_TempRun in selftest.py),
    and the browser checks put Chrome's profile and scratch folder in there,
    where Chrome cannot start once its socket path passes the 107 bytes a
    Unix socket may have. A temp folder inside the worker's home was
    already too long (every rack screen check red in each copy)."""
    root = os.path.join(base, f"t{n}")
    home = os.path.join(base, f"h{n}")
    os.makedirs(home)
    shutil.copytree(HERE, root, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", ".mutating", ".claude"))
    env = {"HOME": home, "USERPROFILE": home, "LOCALAPPDATA": os.path.join(home, "Local"),
           "APPDATA": os.path.join(home, "Roaming"),
           "XDG_STATE_HOME": os.path.join(home, ".local", "state"),
           "XDG_CONFIG_HOME": os.path.join(home, ".config"),
           "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
           "XDG_CACHE_HOME": os.path.join(home, ".cache")}
    return root, env


def _baseline(root, env):
    """One worker's baseline, every test in file order: (green, timed out
    twice, seconds, what failed, what failed the first time when it was
    red once and then green, else None)."""
    first_red = None
    green = run_suite(root=root, extra_env=env)
    if _LAST_RUN[1]:
        # Stopped at its limit with nothing mutated: the runner, not the
        # code. Once more before giving up, since a loaded runner passes.
        print(f"the baseline did not finish inside {SUITE_TIMEOUT} s with "
              f"nothing mutated; running it once more", flush=True)
        green = run_suite(root=root, extra_env=env)
    elif not green:
        # Red with nothing mutated, inside its limit. One wall-clock check
        # can miss its bound on a busy runner (three copies at once with
        # --jobs; CI shard 12 on PR #55). A leftover mutation or a real
        # break fails again, every check run again, so the baseline gets one
        # more full run, with the first failure printed. No bound in
        # selftest.py is loosened.
        print("  baseline failed once, running it again:", flush=True)
        for _l in _LAST_FAILS:
            print("    " + _l)
        first_red = list(_LAST_FAILS)
        green = run_suite(root=root, extra_env=env)
    return (green, bool(_LAST_RUN[1]), _LAST_RUN[0], list(_LAST_FAILS),
            first_red if green else None)


def _each_worker(workers, fn):
    """fn(root, env) for every worker at once; the results in worker order.
    A worker that raised re-raises here: a dead worker must never look like
    a clean result."""
    if len(workers) == 1:
        return [fn(*workers[0])]
    out, errors = [None] * len(workers), []

    def go(i):
        try:
            out[i] = fn(*workers[i])
        except BaseException as e:
            errors.append(f"worker {i}: {e!r}")

    ts = [threading.Thread(target=go, args=(i,)) for i in range(len(workers))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    if errors:
        raise RuntimeError("a mutation worker died: " + "; ".join(errors))
    return out


def _sweep(todo, workers, expected, unknown, expected_file):
    bases = _each_worker(workers, _baseline)
    timed = [b for b in bases if b[1]]
    if timed:
        for _l in timed[0][3]:
            print("  " + _l)
        REPORT.baseline = "TIMED OUT"
        fail("INFRASTRUCTURE: the suite did not finish with nothing mutated, "
             "twice, so the shard measured nothing. This is the runner, not "
             "a missed mutation; re-run the shard", timed[0][3])
        print(f"INFRASTRUCTURE FAILURE: the suite with nothing mutated did "
              f"not finish inside {SUITE_TIMEOUT} s, twice. No mutation was "
              f"run and none was missed. Re-run the shard.")
        return 2
    red = [b for b in bases if not b[0]]
    if red:
        for _l in red[0][3]:
            print("  " + _l)
        REPORT.baseline = "FAILED"
        fail("the suite FAILS with nothing mutated, so the shard measured "
             "nothing", red[0][3])
        print("The suite FAILS with nothing mutated. A previous run was "
              "killed before it restored the tree, or something else is "
              "broken. Fix that first: nothing measured from here would "
              "mean anything.")
        return 2
    baseline_s = max(b[2] for b in bases)
    limit = mutant_timeout(baseline_s)
    REPORT.baseline = "green" if len(bases) == 1 else f"green x{len(bases)}"
    # Red once and then green with nothing mutated is a flaky test, not a
    # green baseline: it is let through (the second run was whole and
    # green) but never silently. It is named here and in the summary.
    shaky = [b[4] for b in bases if b[4] is not None]
    if shaky:
        REPORT.baseline += f", FLAKY ({len(shaky)} red once, then green)"
        for lines in shaky:
            REPORT.flaky.append(("baseline, nothing mutated", lines))
        print(f"FLAKY BASELINE: {len(shaky)} of {len(bases)} baselines failed "
              f"once with nothing mutated and passed on the second run. "
              f"What failed:")
        for lines in shaky:
            for _l in lines:
                print("    " + _l)
    REPORT.limits = f"{baseline_s:.0f} s / {limit} s"
    print(f"baseline green in {baseline_s:.0f} s"
          f"{f' (slowest of {len(bases)} copies)' if len(bases) > 1 else ''}"
          f"; a mutant's suite is stopped (and counts as caught) after "
          f"{limit} s")
    ff = OPTS["failfast"]
    hints = _load_catchers(OPTS["hints"]) if (ff and OPTS["hints"]) else {}
    if hints:
        print(f"{len(hints)} catcher hints: each mutation runs the test that "
              f"caught it last time first")
    caught = missed = 0
    missed_names, caught_names, setup_fails = [], [], []
    learned = {}
    # The bytes of every file this run breaks, before it does: the restore
    # is proved against them at the end, byte for byte, in every copy.
    originals = {rel: _read(os.path.join(HERE, rel))
                 for rel in sorted({m[1] for m in todo})}
    out_lock = threading.Lock()
    probes, probe_locks = {}, {}

    def probe(test, root, env):
        """Does `test` pass when it runs first, with nothing mutated? Asked
        once per test per sweep. Only then may a mutation's run that
        failed in that test, run first, count: otherwise the order alone
        could be what failed it."""
        with out_lock:
            lk = probe_locks.setdefault(test, threading.Lock())
        with lk:
            if test not in probes:
                run_suite(limit, True, test, True, root, env)
                probes[test] = bool(_LAST_STOP[1])
                if not probes[test]:
                    with out_lock:
                        print(f"  hint {test} does not pass when run first "
                              f"with nothing mutated: its mutations run in "
                              f"file order", flush=True)
            return probes[test]

    def attempt(hint, root, env):
        """One run of the mutant: (green, the test it stopped in, what
        failed, the hint if the run that counts had it first). A run with a
        hint counts only when it failed in the hinted test; any other result
        (green, red elsewhere, stopped without saying where) is run again in
        file order, and that one counts."""
        green = run_suite(limit, ff, hint, False, root, env)
        if hint and not (not green and _LAST_STOP[0] == hint):
            green = run_suite(limit, ff, None, False, root, env)
            hint = None
        return green, _LAST_STOP[0], list(_LAST_FAILS), hint

    def one(item, root, env):
        """Apply one mutation in `root`, run the suite, put the file back.
        Returns (kind, lines, catcher); nothing here touches shared state
        but the probe cache, so workers run it side by side."""
        name, rel, old, new = item
        path = os.path.join(root, rel)
        # Bytes in, the same bytes out: UTF-8 whatever the OS default is, and
        # no newline translation, so a restore on Windows cannot turn an LF
        # file into a CRLF one and a pattern cannot miss on a line ending.
        src = _read(path)
        if src.count(old) != 1:
            return ("setup", f"pattern appears {src.count(old)} times in {rel}",
                    None, None)
        hint = hints.get(name)
        if hint and not probe(hint, root, env):
            hint = None
        flaky = None
        _write(path, src.replace(old, new, 1))
        try:
            green, stop, fails, hinted = attempt(hint, root, env)
            # In CI a mutation counts as caught only if the suite fails under
            # it twice running. A test that fails for the runner's reasons
            # would otherwise pass itself off as coverage: an unlisted
            # mutation nothing really catches would read "caught", and a
            # listed one would read "now caught". Caught once and then not is
            # NOT CAUGHT, with what failed the first time printed.
            # The second run is always plain file order, no hint: a red run
            # with the hinted test first could be the order itself (state one
            # test leaves for the next) while the real suite is green. Only a
            # file-order run that fails confirms it, so no such catch counts
            # or is written to --write-catchers. A hinted red is confirmed
            # this way even outside CI.
            if not green and (expected_file or hinted):
                green2, stop2, fails2, _h = attempt(None, root, env)
                if green2:
                    if hinted:
                        fails = ([f"red only with {hinted} run first; the "
                                  f"suite in file order was green"] + fails)
                    green, flaky = True, fails
                else:
                    stop = stop or stop2
        finally:
            _write(path, src)
        if green:
            return ("missed", None, flaky, None)
        return ("caught", fails, None, stop if ff else None)

    def tally(item, res):
        nonlocal caught, missed
        name, rel = item[0], item[1]
        kind, info, flaky, catcher = res
        if kind == "setup":
            print(f"  SETUP FAIL  {name} ({info})")
            missed += 1
            setup_fails.append(name)
            REPORT.setup_fails.append((name, info))
        elif kind == "missed":
            if flaky is not None:
                print(f"  caught once, then not: counted as NOT CAUGHT, "
                      f"and the first failure was a flaky test: {name}")
                for w in flaky:
                    print(f"      {w}")
                REPORT.flaky.append((name, flaky))
            print(f"  NOT CAUGHT  {name}")
            missed += 1
            missed_names.append(name)
            REPORT.missed.append((name, rel))
        else:
            print(f"  caught      {name}")
            caught += 1
            caught_names.append(name)
            REPORT.caught.append(name)
            # What caught it. A check that has nothing to do with this
            # mutation is a flaky test passing itself off as coverage, and
            # this is where that shows.
            for w in info[:3]:
                print(f"      {w}")
            if catcher:
                learned[name] = catcher
                if OPTS["write_catchers"]:
                    # Written as each one is learned, so a shard stopped at
                    # its limit still leaves what it learned.
                    _save_catchers(OPTS["write_catchers"], {name: catcher})
        sys.stdout.flush()

    if len(workers) == 1:
        for item in todo:
            tally(item, one(item, *workers[0]))
    else:
        queue = list(todo)

        def drain(root, env):
            while True:
                with out_lock:
                    if not queue:
                        return
                    item = queue.pop(0)
                res = one(item, root, env)
                with out_lock:
                    tally(item, res)

        _each_worker(workers, drain)
    print(f"\ncaught {caught}, missed {missed}")
    if OPTS["write_catchers"] and learned:
        print(f"{len(learned)} catchers written to {OPTS['write_catchers']}")
    # A mutation runner that leaves a mutation behind is the worst tool in the
    # box: the tree looks fine, the suite is green, and one guarantee is gone.
    # Prove the tree is back the way it started before reporting anything:
    # every file this run broke is compared with the bytes it had before,
    # which is a stronger proof than a green suite (a restore that landed the
    # wrong text could still pass) and costs nothing. The suite runs once
    # more only when that comparison fails, to say how bad it is. With
    # --jobs the tree itself was never touched; it and every copy are
    # checked all the same.
    dirty = sorted({rel for root, _env in [(HERE, None)] + workers
                    for rel, text in originals.items()
                    if _read(os.path.join(root, rel)) != text})
    if dirty:
        print(f"\nTHE TREE IS NOT CLEAN: a restore did not land in "
              f"{', '.join(dirty)}. Fix that before trusting any line above.")
        why = [f"a restore did not land in {rel}" for rel in dirty]
        if not run_suite():
            print("  and the suite fails with nothing mutated:")
            for w in _LAST_FAILS:
                print(f"      {w}")
            why += ["and the suite fails with nothing mutated:"] + list(_LAST_FAILS)
        REPORT.restored = "NO"
        fail("restore proof failed: the tree is not clean", why)
        return 2
    print(f"tree restored byte for byte ({len(originals)} files"
          f"{f', in each of {len(workers)} copies' if len(workers) > 1 else ''})")
    REPORT.restored = "yes"
    return _verdict(expected if expected_file else None, unknown,
                    missed_names, caught_names, setup_fails)


def _mutation_lines(name):
    """What a miss is about: its file and the first lines of the text it
    replaces and the text it puts there, so the annotation says what to
    look at without opening mutate.py."""
    for n, rel, old, new in MUTATIONS:
        if n == name:
            first = lambda s: [l for l in s.splitlines() if l.strip()][:2]
            return ([f"file: {rel}", f"mutation: {name}"]
                    + [f"was:  {l.strip()}" for l in first(old)]
                    + [f"now:  {l.strip()}" for l in first(new)])
    return [f"mutation: {name}"]


def _verdict(expected, unknown, missed_names, caught_names, setup_fails):
    """The exit code, and why. Without an expected-miss list any miss is a
    failure. With one, pass only if every miss is a listed one, and no
    listed one is caught: the list can only shrink, so a listed mutation
    that is now caught fails the run until its line is deleted, and the
    list never hides a guarantee that has started being tested.

    Every red reason goes through fail(), so a shard that ends 1 always
    says which mutation, in which file, in its annotations and summary."""
    bad = False
    flaky = dict(REPORT.flaky)
    if expected is not None:
        for n in unknown:
            print(f"  LIST IS STALE  {n!r} is not a mutation in this file")
            REPORT.stale.append(n)
            bad = True
        for n in setup_fails:
            print(f"  SETUP FAIL is never expected: {n}")
            bad = True
    if REPORT.stale:
        fail("the expected-miss list is stale: it names mutations that "
             "are not in mutate.py", REPORT.stale)
    if setup_fails:
        bad = True
        fail("mutation setup failed: the text to break no longer matches "
             "the code; refresh the mutation",
             [f"{what}: {n}" for n, what in REPORT.setup_fails])
    for n in missed_names:
        if expected is not None and n in expected:
            print(f"  expected miss  {n}  ({expected[n]})")
            REPORT.expected.append((n, expected[n]))
            continue
        if expected is not None:
            print(f"  UNEXPECTED MISS  {n}")
        bad = True
        why = _mutation_lines(n)
        if n in flaky:
            why = (["caught once, then not: the first failure was a flaky "
                    "test, so it counts as NOT CAUGHT. That failure:"]
                   + flaky[n] + why)
        else:
            why.append("the suite stayed green with this line broken "
                       "(twice in CI): nothing tests this guarantee")
        fail("mutation NOT CAUGHT" + (" (flaky)" if n in flaky else ""), why)
    if expected is not None:
        for n in caught_names:
            if n in expected:
                print(f"  NOW CAUGHT, delete it from the list  {n}")
                REPORT.now_caught.append(n)
                bad = True
        if REPORT.now_caught:
            fail("NOW CAUGHT: listed as an expected miss but the suite "
                 "catches it; delete its line from mutate_expected_misses.txt",
                 REPORT.now_caught)
        print("\nagainst the expected-miss list: " + ("FAIL" if bad else "ok"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
