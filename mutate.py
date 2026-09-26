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
"""
import subprocess
import sys
import os

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
  '  <div class="titles">\n    <h1>ltcplay</h1>',
  '  <div class="nope">\n    <h1>ltcplay</h1>'),

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

 ("output loop exception guard removed", "ltcplay/player.py",
  '''try:
                frame = self._tick()''',
  '''if True:
                frame = self._tick()'''),

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

 ("the network token is never checked", "ltcplay/web.py",
  "        return secrets.compare_digest(str(given or \"\"), token)",
  "        return True"),

 ("serving on the network mints no token", "ltcplay/web.py",
  "    if on_network and token is None:", "    if False:"),

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

 ("the layout report stops saying which props go dark", "ltcplay/cli.py",
  '                detail.append(f"    does not contain ch {s0+1}..{s0+maj_starts[s0]}"',
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

 ("verify dies instead of reporting when it cannot write", "ltcplay/cli.py",
  "        try:\n            with open(mpath, \"w\") as fh:",
  "        if True:\n            with open(mpath, \"w\") as fh:"),

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
  "            next_at += period\n"
  "            sleep = next_at - _now()\n"
  "            if sleep > 0:\n"
  "                time.sleep(sleep)\n"
  "            else:\n"
  "                # Fell behind: give up the missed slots rather than sprinting to\n"
  "                # catch up, which would burst packets at the controllers.\n"
  "                next_at = _now()",
  "            next_at += period\n"
  "            time.sleep(period)"),

 ("the run loop's heartbeat reads the other clock", "ltcplay/cli.py",
  "        # started, and so last_beat, is on sess.started_at's clock:\n"
  "        # player._now() (perf_counter). Reading time.monotonic() here would\n"
  "        # compare it against a clock with an unrelated epoch -- fine on a\n"
  "        # Mac, where the two happen to agree, and nonsense on Windows.\n"
  "        now = _now()",
  "        now = time.monotonic()"),

 ("skipping a free run does nothing", "ltcplay/player.py",
  "        self.freerun_epoch = _now() - at",
  "        pass  # noqa"),

 ("skipping back runs off the front of the show", "ltcplay/player.py",
  "        at = max(0.0, (_now() - self.freerun_epoch) + float(seconds))",
  "        at = (_now() - self.freerun_epoch) + float(seconds)"),

 ("skipping is allowed while following timecode", "ltcplay/player.py",
  "        if self.freerun_epoch is None:\n"
  "            raise ValueError(\"The show is following timecode, so this Mac \"\n"
  "                             \"cannot move it. Skipping only applies to a free \"\n"
  "                             \"run: press GO first.\")\n"
  "        at = max(0.0,",
  "        at = max(0.0,"),

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
  '                    bind_ip=self.bind, on_stop=self.player.drop_clock)',
  '                    bind_ip=self.bind, on_stop=None)'),

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

 ("an announcement never holds the show first", "ltcplay/announce.py",
  '            # never reached inert.\n'
  '            if self.hold_requester is not None:\n'
  '                hold_refusal = self._request_hold(who, screen)\n'
  '                if hold_refusal:',
  '            # never reached inert.\n'
  '            if False:\n'
  '                hold_refusal = self._request_hold(who, screen)\n'
  '                if hold_refusal:'),

 # Deliberately no mutation here disabling the FIRST checkpoint's own
 # "if hold_refusal:" alone (only the "if self.hold_requester is not
 # None:" gate above it, and the SECOND checkpoint's identical check
 # below): the second checkpoint re-asks and re-checks Hold immediately
 # before the stream starts, on purpose (the TOCTOU recheck), so disabling
 # only the first check's own refusal changes nothing a test can observe
 # -- the second one still refuses, with the same sentence. Confirmed
 # equivalent by hand (mutate.py run, 2026-09-26): NOT CAUGHT, correctly.

 ("the second hold request before the stream starts is skipped",
  "ltcplay/announce.py",
  '                raise ValueError(text)\n'
  '            if self.hold_requester is not None:\n'
  '                hold_refusal = self._request_hold(who, screen)\n'
  '                if hold_refusal:\n'
  '                    self.playing = None',
  '                raise ValueError(text)\n'
  '            if False:\n'
  '                hold_refusal = self._request_hold(who, screen)\n'
  '                if hold_refusal:\n'
  '                    self.playing = None'),

 ("the Hold request does not carry the Play press's own operator and "
  "screen", "ltcplay/announce.py",
  '        try:\n'
  '            return self.hold_requester(who, screen)\n'
  '        except Exception as e:\n'
  '            return _clean(str(e))',
  '        try:\n'
  '            return self.hold_requester("", "")\n'
  '        except Exception as e:\n'
  '            return _clean(str(e))'),

 ("Service.hold_for_announcement never refuses, even when Hold itself "
  "was refused", "ltcplay/schedule_service.py",
  '            out = self._apply(sch.Event(sch.HOLD_ON, "operator", who=who,\n'
  '                                        screen=screen))\n'
  '            if out.refused and self.machine.state in (sch.HOLD, sch.PAUSED):\n'
  '                return None\n'
  '            return out.refused or None',
  '            out = self._apply(sch.Event(sch.HOLD_ON, "operator", who=who,\n'
  '                                        screen=screen))\n'
  '            if out.refused and self.machine.state in (sch.HOLD, sch.PAUSED):\n'
  '                return None\n'
  '            return None'),

 ("already being on Hold or paused is treated as a Hold refusal",
  "ltcplay/schedule_service.py",
  '            if out.refused and self.machine.state in (sch.HOLD, sch.PAUSED):\n'
  '                return None\n'
  '            return out.refused or None',
  '            if False:\n'
  '                return None\n'
  '            return out.refused or None'),

 ("announcements never Hold the scheduler in production, web.py never "
  "wires it", "ltcplay/web.py",
  '        httpd.announce.hold_requester = sched.hold_for_announcement',
  '        pass'),

 ("a second announcement is allowed to start while one plays",
  "ltcplay/announce.py",
  '            if self.playing is not None:\n'
  '                other = LABELS[self.playing]',
  '            if False:\n'
  '                other = LABELS[self.playing]'),

 ("an unavailable announcement file plays anyway", "ltcplay/announce.py",
  '            st = self.status_by_id.get(ann_id, {})\n'
  '            if not st.get("available"):',
  '            st = self.status_by_id.get(ann_id, {})\n'
  '            if False:'),

 ("a name not on the operator list can still press Play",
  "ltcplay/announce.py",
  '                raise ValueError(text)\n'
  '            if who.lower() not in {n.lower() for n in self.operators}:\n'
  '                reason = (f"{who!r} is not on the operator list "',
  '                raise ValueError(text)\n'
  '            if False:\n'
  '                reason = (f"{who!r} is not on the operator list "'),

 ("a missing announcement output device falls back to another one",
  "ltcplay/announce.py",
  '    if not hits:\n'
  '        raise ValueError(f"{_clean(name)!r} is not attached. Nothing else "\n'
  '                         f"will be used in its place. Outputs on this "\n'
  '                         f"machine: {_clean(names)}.")',
  '    if not hits:\n'
  '        if outputs:\n'
  '            return outputs[0]'),

 ("a refused announcement play is not written to the journal",
  "ltcplay/announce.py",
  '                self._emit(actor="operator", action="play",\n'
  '                          outcome="refused",\n'
  '                          reason="another announcement is already playing",\n'
  '                          text=text, ann_id=ann_id, who=who, screen=screen,\n'
  '                          state=state)\n'
  '                raise ValueError(text)\n'
  '            st = self.status_by_id.get(ann_id, {})',
  '                raise ValueError(text)\n'
  '            st = self.status_by_id.get(ann_id, {})'),

 ("an announcement's device name matches by substring again",
  "ltcplay/announce.py",
  '    hits = [d for d in outputs if d["name"].strip().lower() == want]',
  '    hits = [d for d in outputs if want in d["name"].lower()]'),

 ("the announcement interlock is never rechecked before the stream starts",
  "ltcplay/announce.py",
  '            state = self._current_state()\n'
  '            refusal = interlock_refusal(state)\n'
  '            if refusal:\n',
  '            state = self._current_state()\n'
  '            refusal = interlock_refusal(state)\n'
  '            if False:\n'),

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
  '    if tag is not None and tag not in (1, 3, 0xFFFE):',
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
  '            state_now, hook = self.machine.state, self.on_show_started\n'
  '            self._pending_hooks.append(lambda: hook(state_now))',
  '            state_now, hook = self.machine.state, self.on_show_started\n'
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
  '            state_now, hook = self.machine.state, self.on_show_started\n'
  '            self._pending_hooks.append(lambda: hook(state_now))',
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
  '            self._paused = False\n'
  '            self._frozen = None\n'
  '            self._frozen_pos = None\n'
  '            t0 = self._clock() - (n_frozen + 1) / MASTER_FPS',
  '            label = self._cue[2]\n'
  '            n_frozen = self._frozen_n\n'
  '            self._paused = False\n'
  '            self._frozen = None\n'
  '            self._frozen_pos = None\n'
  '            self.ticker.stop()\n'
  '            t0 = self._clock() - (n_frozen + 1) / MASTER_FPS'),

 ("pause() sets the paused flag before the frozen frame again",
  'ltcplay/clock.py',
  '            self._frozen = (h, m, s, f)\n'
  '            self._frozen_n = n\n'
  '            self._frozen_pos = position_s + n / MASTER_FPS\n'
  '            self.last_sent = (h, m, s, f)\n'
  '            self._paused = True\n'
  '            self._sync_point("pause")',
  '            self._paused = True\n'
  '            self._sync_point("pause")\n'
  '            self._frozen = (h, m, s, f)\n'
  '            self._frozen_n = n\n'
  '            self._frozen_pos = position_s + n / MASTER_FPS\n'
  '            self.last_sent = (h, m, s, f)'),


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

 ("the second check re-Holds instead of only reading the state",
  "ltcplay/announce.py",
  "            if self.hold_requester is not None \\\n"
  "                    and not self._check_still_held(claim_epoch):",
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
]


# What the last suite run failed on, so a surprising result can be read.
_LAST_FAILS = []


def run_suite():
    r = subprocess.run([sys.executable, "selftest.py"], cwd=HERE,
                       capture_output=True, text=True, timeout=300)
    out = (r.stdout or "") + (r.stderr or "")
    _LAST_FAILS[:] = [l.strip() for l in out.splitlines()
                      if l.startswith("  FAIL") or "Error" in l][:6]
    return r.returncode == 0


LOCK = os.path.join(HERE, ".mutating")


def _read(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def main():
    # While this runs, the working tree is deliberately broken. Anything else
    # that reads it — a capture, a soak, a live run — is measuring a mutant and
    # will produce a confident, entirely false result. The lock exists because
    # that has already happened once.
    if os.path.exists(LOCK):
        print(f"{LOCK} exists: another mutation run is in progress, or one "
              f"died and left the tree broken. Check `git diff` or the "
              f"selftest before deleting it.")
        return 3
    open(LOCK, "w").write(str(os.getpid()))
    try:
        return _run()
    finally:
        os.remove(LOCK)


def _args(argv):
    """Name filters, plus the two CI options. Anything else is a filter, as
    it always was."""
    wants, shard, expected = [], None, None
    it = iter(argv)
    for a in it:
        if a == "--shard":
            i, n = next(it).split("/")
            shard = (int(i), int(n))
        elif a == "--expected":
            expected = next(it)
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


def _run():
    wants, shard, expected_file = _args(sys.argv[1:])
    expected, unknown = ({}, [])
    if expected_file:
        expected, unknown = _load_expected(expected_file)
    # Prove the tree is clean BEFORE breaking it on purpose. A sweep that is
    # killed (a foreground timeout, a closed terminal) skips its restore and
    # leaves a mutation behind; the next sweep then measures that mutant and
    # blames whichever mutation it happens to be applying. Both of those have
    # already happened here.
    if not run_suite():
        print("The suite FAILS with nothing mutated. A previous run was "
              "killed before it restored the tree, or something else is "
              "broken. Fix that first: nothing measured from here would "
              "mean anything.")
        return 2
    caught = missed = 0
    missed_names, caught_names, setup_fails = [], [], []
    for index, (name, rel, old, new) in enumerate(MUTATIONS):
        if wants and not any(w in name.lower() for w in wants):
            continue
        if shard and index % shard[1] != shard[0]:
            continue
        path = os.path.join(HERE, rel)
        # Bytes in, the same bytes out: UTF-8 whatever the OS default is, and
        # no newline translation, so a restore on Windows cannot turn an LF
        # file into a CRLF one and a pattern cannot miss on a line ending.
        src = _read(path)
        if src.count(old) != 1:
            print(f"  SETUP FAIL  {name} "
                  f"(pattern appears {src.count(old)} times in {rel})")
            missed += 1
            setup_fails.append(name)
            continue
        backup = src
        _write(path, src.replace(old, new, 1))
        try:
            green = run_suite()
            # In CI a mutation counts as caught only if the suite fails under
            # it twice running. A test that fails for the runner's reasons
            # would otherwise pass itself off as coverage: an unlisted
            # mutation nothing really catches would read "caught", and a
            # listed one would read "now caught". Caught once and then not is
            # NOT CAUGHT, with what failed the first time printed.
            if not green and expected_file:
                why = list(_LAST_FAILS)
                if run_suite():
                    green = True
                    print(f"  caught once, then not: counted as NOT CAUGHT, "
                          f"and the first failure was a flaky test: {name}")
                    for w in why:
                        print(f"      {w}")
        finally:
            _write(path, backup)
        if green:
            print(f"  NOT CAUGHT  {name}")
            missed += 1
            missed_names.append(name)
        else:
            print(f"  caught      {name}")
            caught += 1
            caught_names.append(name)
            # What caught it. A check that has nothing to do with this
            # mutation is a flaky test passing itself off as coverage, and
            # this is where that shows.
            for w in _LAST_FAILS[:3]:
                print(f"      {w}")
    print(f"\ncaught {caught}, missed {missed}")
    # A mutation runner that leaves a mutation behind is the worst tool in the
    # box: the tree looks fine, the suite is green, and one guarantee is gone.
    # Prove the tree is back the way it started before reporting anything.
    if not run_suite():
        print("\nTHE TREE IS NOT CLEAN: the suite fails with nothing mutated, "
              "so a restore did not land. Fix that before trusting any line "
              "above.")
        return 2
    print("tree restored and green")
    if not expected_file:
        return 1 if missed else 0
    return _against_expected(expected, unknown, missed_names, caught_names,
                             setup_fails)


def _against_expected(expected, unknown, missed_names, caught_names,
                      setup_fails):
    """Pass only if every miss is a listed one, and no listed one is caught.

    The list can only shrink: a listed mutation that is now caught fails the
    run until its line is deleted, so the list never hides a guarantee that
    has started being tested."""
    bad = False
    for n in unknown:
        print(f"  LIST IS STALE  {n!r} is not a mutation in this file")
        bad = True
    for n in setup_fails:
        print(f"  SETUP FAIL is never expected: {n}")
        bad = True
    for n in missed_names:
        if n in expected:
            print(f"  expected miss  {n}  ({expected[n]})")
        else:
            print(f"  UNEXPECTED MISS  {n}")
            bad = True
    for n in caught_names:
        if n in expected:
            print(f"  NOW CAUGHT, delete it from the list  {n}")
            bad = True
    print("\nagainst the expected-miss list: " + ("FAIL" if bad else "ok"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
