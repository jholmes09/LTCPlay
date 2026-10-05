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

 # -- the iPad remote (2026-10-03): PIN sessions, the show network, the
 #    controls, stale state, no arm route, scrubbing and the seek guard --
 ("remote: a network request needs no PIN session", "ltcplay/web.py",
  "        return self._ctx().session is not None",
  "        return True"),

 # -- fix round 1 of #39 (independent review) --
 ("remote: a network GET reaches the legacy routes", "ltcplay/web.py",
  "        if not self._local() and not network_may_reach(route):\n"
  "            return self._send(403, {\"error\": \"Not from the network. Only \"\n"
  "                                             \"the remote page's own routes \"\n"
  "                                             \"answer here.\"})\n"
  "        authorised = self._authorised()",
  "        authorised = self._authorised()"),

 ("remote: a network POST reaches the legacy routes", "ltcplay/web.py",
  "        if not self._local() and not network_may_reach(route):\n"
  "            return self._send(403, {\"error\": \"Not from the network. Only \"\n"
  "                                             \"the remote page's own routes \"\n"
  "                                             \"answer here.\"})\n"
  "        if not self._authorised() and route not in OPEN_POSTS:",
  "        if not self._authorised() and route not in OPEN_POSTS:"),

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

 ("remote: PIN checks are not serialized", "ltcplay/remote.py",
  "        with self.throttle.serial(keys):",
  "        with threading.Lock():"),

 ("remote: login says which operators have no PIN", "ltcplay/remote.py",
  "            # on the show machine says the truth.\n"
  "            return 403, {\"error\": \"That PIN is not right.\"}, {}",
  "            # on the show machine says the truth.\n"
  "            return 403, {\"error\": f\"{who} has no PIN yet.\"}, {}"),

 ("web: odd spellings of every interface are served", "ltcplay/web.py",
  "    bind = normalize_bind(bind)", "    bind = bind"),

 ("remote: the remote routes skip the session check", "ltcplay/remote.py",
  "        if not ctx.allowed:\n            return 401, {\"error\": \"Sign in "
  "with your PIN first.\"}, {}",
  "        if False:\n            return 401, {\"error\": \"Sign in "
  "with your PIN first.\"}, {}"),

 ("remote: any PIN opens a session", "ltcplay/remote.py",
  "        return hmac.compare_digest(self._hash(pin, salt, n), want)",
  "        return True"),

 ("remote: wrong PINs are never throttled", "ltcplay/remote.py",
  "        wait = self.throttle.wait_s(keys)\n        if wait > 0:",
  "        wait = self.throttle.wait_s(keys)\n        if False:"),

 ("remote: the lock-out never grows", "ltcplay/remote.py",
  "                               LOCK_BASE_S * 2 ** (n - FREE_TRIES - 1))",
  "                               LOCK_BASE_S)"),

 ("remote: a new PIN leaves old sessions signed in", "ltcplay/remote.py",
  "        # A new PIN signs out every device signed in with the old one.\n"
  "        self.sessions.drop_who(who)",
  "        pass"),

 ("remote: PINs can be set from the network", "ltcplay/remote.py",
  "        if name in LOCAL_ONLY and not ctx.local:",
  "        if False:"),

 ("remote: the press names whoever the body says", "ltcplay/remote.py",
  "        if ctx.session is not None:\n            return ctx.session[\"who\"], "
  "ctx.session[\"device\"]",
  "        if False:\n            return ctx.session[\"who\"], "
  "ctx.session[\"device\"]"),

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

 ("remote: a stale flame lamp still reads armed", "ltcplay/remote.py",
  "            for g in groups:\n                g[\"armed\"] = \"unknown\"",
  "            pass"),

 ("remote: Start now and Abort need no confirm", "ltcplay/remote.py",
  "        if name in CONFIRM_ROUTES and body.get(\"confirmed\") is not True:",
  "        if False:"),

 ("remote: an arm route appears", "ltcplay/remote.py",
  "CONTROL_ROUTES = (\"start-now\", \"hold\", \"resume\", \"abort\", \"reset\",\n"
  "                  \"disarm-all\", \"operator\")",
  "CONTROL_ROUTES = (\"start-now\", \"hold\", \"resume\", \"abort\", \"reset\",\n"
  "                  \"disarm-all\", \"operator\", \"arm\")"),

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

 ("remote: a device can pick someone else as the operator",
  "ltcplay/remote.py",
  "                    if want.lower() != who.lower():",
  "                    if False:"),

 ("scheduler: a remote press from someone off the list is taken",
  "ltcplay/schedule_service.py",
  "        if who.lower() not in names:\n            sentence = (f\"{who or "
  "'Nobody'!r} is not on the operator list \"",
  "        if False:\n            sentence = (f\"{who or "
  "'Nobody'!r} is not on the operator list \""),

 # -- arming from a screen (2026-10-03, its own PR) --
 ("arm: a hold needs no PIN session", "ltcplay/remote.py",
  "        s = ctx.session\n        if s is None:\n            return 401, "
  "{\"error\": \"Arming needs your own PIN sign in, even \"",
  "        s = ctx.session or {\"who\": \"Andy\", \"device\": \"iPad\", "
  "\"token\": \"x\"}\n        if s is None:\n            return 401, "
  "{\"error\": \"Arming needs your own PIN sign in, even \""),

 ("arm: the screen_arming switch is ignored", "ltcplay/remote.py",
  "        if not self.arming_enabled():", "        if False:"),

 ("arm: screen arming defaults off", "ltcplay/remote.py",
  "           \"screen_arming\": True, \"path\": path}",
  "           \"screen_arming\": False, \"path\": path}"),

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
  "            if h is not None and h[\"token\"] != token and \\",
  "            if False and h is not None and h[\"token\"] != token and \\"),

 ("arm: an interrupted hold carries on", "ltcplay/remote.py",
  "                        now - h[\"beat\"] <= BEAT_STALE_S)\n"
  "                if live:",
  "                        True)\n                if live:"),

 ("arm: the deck is told a quiet hold is still fresh", "ltcplay/remote.py",
  "                              \"fresh\": age <= BEAT_STALE_S,",
  "                              \"fresh\": True,"),

 ("arm: an Abort leaves screen holds running", "ltcplay/remote.py",
  "        if name in (\"abort\", \"disarm-all\"):\n            # Before",
  "        if False:\n            # Before"),

 ("arm: a group disarm leaves its hold running", "ltcplay/remote.py",
  "        self._drop_hold(i)\n        with self._arm_lock:\n"
  "            self._disarm_ids += 1",
  "        with self._arm_lock:\n            self._disarm_ids += 1"),

 ("arm: signing out leaves your holds running", "ltcplay/remote.py",
  "                for i in [i for i, h in self._holds.items()\n"
  "                          if h[\"token\"] == s[\"token\"]]:\n"
  "                    del self._holds[i]",
  "                pass"),

 ("arm: deck-input is served to the network", "ltcplay/remote.py",
  "            if not ctx.local:\n                return 403, {\"error\": "
  "\"Only on the show machine itself.\"}\n            return 200, "
  "self.deck_input()",
  "            return 200, self.deck_input()"),

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
  "        if last is None or self._clock() - last[1] > SCREEN_STALE_S:",
  "        if last is None:"),

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
  "        holds = {} if self._latched_now() else self.screen.holds()",
  "        holds = self.screen.holds()"),

 ("arm: a screen disarm does nothing", "ltcplay/streamdeck.py",
  "            if self.arm.wanted[i]:\n                self._do_disarm(i, who=",
  "            if False:\n                self._do_disarm(i, who="),

 ("arm: the page arms on a stale status", "ltcplay/web/remote.html",
  "  if(isStale(nowMs, lastOkMs, a.fresh_s || 1.0)) return",
  "  if(false) return"),

 ("arm: the page arms on an old flamesafe status", "ltcplay/web/remote.html",
  "fl.age_ms === undefined || fl.age_ms > 1000)",
  "fl.age_ms === undefined || fl.age_ms > 100000)"),

 ("arm: the page keeps holding when it goes stale", "ltcplay/web/remote.html",
  "  if(HOLD && !armState(ST, Date.now(), LAST_OK, HOLD.group).ok)\n"
  "    armStop(",
  "  if(false)\n    armStop("),

 ("arm: closing the page does not let go", "ltcplay/web/remote.html",
  "window.addEventListener(\"pagehide\", () => armStop(\"the page closed\"));",
  ""),

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

 ("the scheduler's own lines stay in memory only, as before",
  'ltcplay/schedule_service.py',
  '            self._record_logevent(self._reworded(le))\n        claimed = set()',
  '            self.journal.append(self._reworded(le).to_dict())\n        claimed = set()'),

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
  '    httpd.control.stop()\n    announce = getattr(httpd, "announce", None)\n',
  '    if httpd.schedule is not None:\n        httpd.schedule.stop()\n'
  '    httpd.control.stop()\n    announce = getattr(httpd, "announce", None)\n'),

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
  "            if f == safety:",
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
  "        if g.safety in seen:",
  "        if False:"),

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
  "        buf = bytearray(rules.UNIVERSE_SIZE)\n        sent_fire = []",
  "        buf = bytearray(commanded if commanded is not None\n"
  "                        else rules.UNIVERSE_SIZE)\n        sent_fire = []"),

 ("flamesafe: a disarmed group passes its fire values through",
  "flamesafe/composer.py",
  "                if armed_now and not quiet:\n                    out = v",
  "                if not quiet:\n                    out = v"),

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
  "                                  self._frame_is_fresh(t), False)",
  "                                  self._held, False, False, False)"),

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
  "                if armed_now and not quiet:\n                    out = v",
  "                if armed_now:\n                    out = v"),

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
  "            buf[g.safety - 1] = values[i]",
  "            buf[g.safety] = values[i]"),

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

 ("an announcement never holds the show first", "ltcplay/announce.py",
  '            claim_epoch = None\n'
  '            if self.hold_requester is not None:\n'
  '                hold_refusal, claim_epoch = self._request_hold(\n'
  '                    who, screen,\n'
  '                    detail=f"played the {label} announcement{screen_txt}")\n'
  '                if hold_refusal:',
  '            claim_epoch = None\n'
  '            if False:\n'
  '                hold_refusal, claim_epoch = self._request_hold(\n'
  '                    who, screen,\n'
  '                    detail=f"played the {label} announcement{screen_txt}")\n'
  '                if hold_refusal:'),

 # Deliberately no mutation here disabling the FIRST checkpoint's own
 # "if hold_refusal:" alone (only the "if self.hold_requester is not
 # None:" gate above it, and the SECOND checkpoint's own check): the
 # second checkpoint re-checks (now read-only: _check_still_held)
 # immediately before the stream starts, on purpose (the TOCTOU
 # recheck), so disabling only the first check's own refusal changes
 # nothing a test can observe -- the second one still refuses. Confirmed
 # equivalent by hand (mutate.py run, 2026-09-26): NOT CAUGHT, correctly.
 # The second checkpoint's own gate is covered instead by "the second
 # check re-Holds instead of only reading the state" (review round 2).

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
  '        httpd.announce.hold_requester = sched.hold_for_announcement',
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
  "ltcplay/beyond.py",
  "        if address in FORBIDDEN_ADDRESSES or not _allowed(address, "
  "value):\n"
  "            raise BeyondConfigError(\n"
  "                f\"beyond.py refuses to send {address!r} with value \"\n"
  "                f\"{value!r}: only the brightness address, with 0.0 or \"\n"
  "                f\"100.0, is ever allowed (S5), and BlackOut/MasterPause \"\n"
  "                f\"are refused by name as well (BlackOut restarts "
  "BEYOND's \"\n"
  "                f\"own core and needs a manual recovery; MasterPause \"\n"
  "                f\"freezes the beams, a static-beam hazard).\")\n"
  "        return self._osc.send(address, value, force=force)",
  "        return self._osc.send(address, value, force=force)"),

 ("the socket's own send() no longer enforces the allow-list at all",
  "ltcplay/beyond.py",
  "        if address in FORBIDDEN_ADDRESSES or not _allowed(address, "
  "value):\n"
  "            raise BeyondConfigError(\n"
  "                f\"beyond.py's socket layer refuses to send {address!r} \"\n"
  "                f\"with value {value!r}: only the brightness address, \"\n"
  "                f\"with 0.0 or 100.0, is ever allowed off this module "
  "(S5).\")\n"
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
  '        if frame >= cue["tc_frames"]:\n            self._end("finished", now)',
  '        if False:\n            self._end("finished", now)'),

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

 ('audio_master: the show may use more than 8 outputs',
  'ltcplay/showaudio.py',
  'MAX_OUTPUTS = 8              # Jeff',
  'MAX_OUTPUTS = 64             # Jeff'),

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
  "ltcplay/conductor.py",
  "                self._flames_cut()\n                self._video_cancel()\n",
  "                self._video_cancel()\n"),

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
  "                if not self._playing():",
  "            latched = False\n            if not latched:\n"
  "                if not self._playing():"),

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
  "                    return self._refused(\"Abort\",",
  "                if False:\n"
  "                    return self._refused(\"Abort\","),

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
  "                self._flames_cut()\n                self._video_cancel()\n",
  "                self._flames_cut()\n"),

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

 ("conductor: a 100 already on its way out is not followed by a 0 "
  "(finding D)",
  "ltcplay/beyond.py",
  "            if late:\n",
  "            if False:\n"),

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

 ("round3: an Abort can also complete a same-pass arm-hold again",
  "ltcplay/streamdeck.py",
  "            self._do_abort()\n"
  "            # Item 3 (round 2 of the safety review): never ALSO "
  "complete an\n"
  "            # arm-hold in the SAME pass an Abort just fired in. "
  "_do_abort()\n"
  "            # just told every group's wanted false; finishing a hold "
  "a\n"
  "            # moment later in this same pass would re-arm the very "
  "group\n"
  "            # Abort was supposed to clear. _do_arm_fire's own "
  "latched/\n"
  "            # refractory guard (now set by _do_abort, just above) is "
  "a\n"
  "            # second, independent backstop -- this return is the "
  "ordering\n"
  "            # fix itself, not a substitute for that guard, nor the "
  "other\n"
  "            # way round.\n"
  "            return\n",
  "            self._do_abort()\n"),

 ("round3: a Stream Deck reconnect no longer resets in-progress hold "
  "state",
  "ltcplay/streamdeck.py",
  "            controller.arm.restart()\n"
  "            controller.reset_on_reconnect()\n",
  "            controller.arm.restart()\n"),

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
  "            controller.arm.set_all(False)\n"
  "            controller.arm.send(controller.names)\n"
  "        except Exception:\n"
  "            pass\n"
  "        sleep(1.0 / ARM_SEND_HZ)\n",
  "            controller.arm.set_all(False)\n"
  "        except Exception:\n"
  "            pass\n"
  "        sleep(1.0 / ARM_SEND_HZ)\n"),

 ("round4: the deck process sends nothing until the first deck is found",
  "ltcplay/streamdeck.py",
  "            if not controller.arm.is_open:\n"
  "                controller.arm.open()\n"
  "            controller.arm.set_all(False)\n",
  "            if not controller.arm.is_open:\n"
  "                pass\n"
  "            controller.arm.set_all(False)\n"),

 ("round4: a reconnect closes and reopens the arm socket (a new source "
  "port, so the sender lock changes hands)",
  "ltcplay/streamdeck.py",
  "            controller.arm.restart()\n"
  "            controller.reset_on_reconnect()\n",
  "            controller.arm.close()\n"
  "            controller.arm.open()\n"
  "            controller.reset_on_reconnect()\n"),

 ("round4: an unplug closes the arm socket again",
  "ltcplay/streamdeck.py",
  "            # NOT arm.close() (round 4, item A): see _hold_link_off.\n"
  "            try:\n"
  "                controller.arm.set_all(False)\n",
  "            controller.arm.close()\n"
  "            try:\n"
  "                controller.arm.set_all(False)\n"),

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
 ("round4: reset_on_reconnect skips only the Abort hold",
  "ltcplay/streamdeck.py",
  "        self._prev_keys = [False] * 6\n"
  "        self._abort_hold.release()\n"
  "        for h in self._arm_holds:\n"
  "            h.release()\n",
  "        self._prev_keys = [False] * 6\n"
  "        for h in self._arm_holds:\n"
  "            h.release()\n"),

 ("round4: reset_on_reconnect skips only the arm holds",
  "ltcplay/streamdeck.py",
  "        self._prev_keys = [False] * 6\n"
  "        self._abort_hold.release()\n"
  "        for h in self._arm_holds:\n"
  "            h.release()\n",
  "        self._prev_keys = [False] * 6\n"
  "        self._abort_hold.release()\n"),

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
  "        if self.zeroed or live is not True or tc is None:",
  "        if self.zeroed:"),

 ("flamelink: cues go out after the conductor zeroed them",
  "ltcplay/flamelink.py",
  "        if self.zeroed or live is not True or tc is None:",
  "        if live is not True or tc is None:"),

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
  "            if self.zeroed or gen != self._zero_gen:",
  "            if self.zeroed:"),

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
  '''                          "frame_stale_ms": doc.get("frame_stale_ms")},''',
  '''                          },'''),

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
 ("round5: the OFF sender stops for good after 6 s with no deck",
  "ltcplay/streamdeck.py",
  "            controller.arm.set_all(False)\n"
  "            controller.arm.send(controller.names)\n"
  "        except Exception:\n"
  "            pass\n"
  "        sleep(1.0 / ARM_SEND_HZ)\n",
  "            controller.arm.set_all(False)\n"
  "            _n = controller.__dict__.setdefault('_r5_off', [0])\n"
  "            _n[0] += 1\n"
  "            if _n[0] <= 120:\n"
  "                controller.arm.send(controller.names)\n"
  "        except Exception:\n"
  "            pass\n"
  "        sleep(1.0 / ARM_SEND_HZ)\n"),

 ("round5: the OFF sender stops for good after 60 s with no deck",
  "ltcplay/streamdeck.py",
  "            controller.arm.set_all(False)\n"
  "            controller.arm.send(controller.names)\n"
  "        except Exception:\n"
  "            pass\n"
  "        sleep(1.0 / ARM_SEND_HZ)\n",
  "            controller.arm.set_all(False)\n"
  "            _n = controller.__dict__.setdefault('_r5_off', [0])\n"
  "            _n[0] += 1\n"
  "            if _n[0] <= 1200:\n"
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
  "            outage.failed(e, \"Every group is sent OFF",
  "        except DeckDisconnected as e:\n"
  "            outage.failed(e, \"Every group is sent OFF"),

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
  "        if kind in self._kinds:\n"
  "            self._unlogged += 1\n"
  "            return\n",
  ""),

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
  '        self._record(out, now, plan)\n        if self.dry_run and self.machine.state == sch.CLOSING:',
  '        self._queue_conductor(plan, ev)\n        self._calls.flush(0.5)\n        plan = []\n        self._record(out, now, plan)\n        if self.dry_run and self.machine.state == sch.CLOSING:'),

 ("scheduler fix round: conductor calls are made inside the scheduler's lock",
  "ltcplay/schedule_service.py",
  "            self._calls.put(call)\n\n    def _new_call",
  "            self._run_conductor_call(call)\n\n    def _new_call"),

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
  "            self._calls.put(call)\n"
  "        if not call.done.wait(wait_s):",
  "            call.ok, call.sentence = False, \"not sent\"\n"
  "            call.done.set()\n"
  "        if not call.done.wait(wait_s):"),

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
  "        if call.seq < self._abort_seq:",
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

 # Jeff's decisions, 2026-10-03: a failed start disarms every flame group.
 ("failed start disarms: the scheduler sends show_stopped (no disarm)",
  "ltcplay/schedule_service.py",
  "            elif ev.kind == sch.SHOW_FAILED:",
  "            elif False:"),

 ("failed start disarms: the conductor only zeroes the cues at once",
  "ltcplay/conductor.py",
  "            self._flames_cut(self.FAILED_START)",
  "            self.show.flames_zero()"),

 ("failed start disarms: the disarm is never sent at all",
  "ltcplay/conductor.py",
  "            self._flames_cut(self.FAILED_START)\n"
  "            self._accept(\"Failed start\", STOPPED_DARK, who, screen,\n"
  "                         fade_s=ABORT_FADE_S, disarm=self.FAILED_START)",
  "            self.show.flames_zero()\n"
  "            self._accept(\"Failed start\", STOPPED_DARK, who, screen,\n"
  "                         fade_s=ABORT_FADE_S)"),

 ("failed start disarms: a failed disarm is never sent again",
  "ltcplay/conductor.py",
  "                if not self._applied[\"disarmed\"]:\n"
  "                    self._disarm(why)",
  "                if False:\n"
  "                    self._disarm(why)"),

 ("failed start disarms: the conductor latches a failed start",
  "ltcplay/conductor.py",
  "            self._accept(\"Failed start\", STOPPED_DARK, who, screen,",
  "            self._latched = True\n"
  "            self._accept(\"Failed start\", STOPPED_DARK, who, screen,"),

 ("failed start disarms: the scheduler latches a failed start",
  "ltcplay/schedule_service.py",
  "        if any(p[1] == \"abort\" for p in plan):",
  "        if any(p[1] in (\"abort\", \"failed_start\") for p in plan):"),

 ('failed start disarms: the journal never says why the flames were disarmed',
  'ltcplay/schedule_service.py',
  '            le = replace(le, text=le.text.replace(\n                sch.FAILED_START_NOT_DISARMED,\n                self.CONDUCTOR_DISARMS_FAILED_START))',
  '            pass'),

 ("failed start disarms: a cut show disarms too",
  "ltcplay/schedule_service.py",
  "                plan.append((\"Show stopped\", \"show_stopped\",\n"
  "                             self._ABORT_EFFECTS))",
  "                plan.append((\"Failed start\", \"failed_start\",\n"
  "                             self._ABORT_EFFECTS))"),

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

 ("fire & ice: a show starts before Run is pressed",
  "ltcplay/fire_ice.py",
  "        if s is None or clk is None or self.conductor.latched:",
  "        if s is not None and clk is None:"),

 ("fire & ice: a show starts while the conductor is still aborted",
  "ltcplay/fire_ice.py",
  "        if s is None or clk is None or self.conductor.latched:",
  "        if s is None or clk is None:"),

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

 ("fire & ice: the laser gate does not follow the scheduler",
  "ltcplay/fire_ice.py",
  "        devices, show, C.laser_gate_for(state),",
  "        devices, show, lambda: None,"),

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
  '        if route in LIVE_SHOW_REFUSED and self._scheduled_show_live():',
  '        if False:'),

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
  '            late = self._abort_overdue()\n        if late is not None:\n            self._note_unconfirmed(late)\n        return ok',
  '            late = None\n        if late is not None:\n            self._note_unconfirmed(late)\n        return ok'),

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


 ('fix round 1: the GPL remote loads the scheduler for its operator list',
  'ltcplay/remote.py',
  '        return list(read_names(os.path.join(self.folder, OPERATORS_FILE),\n                               "operators", DEFAULT_OPERATORS))',
  '        from . import schedule_service\n        return list(schedule_service.load_operators(self.folder)[0])'),

 ('fix round 1: the GPL remote reads a list with a name on it twice',
  'ltcplay/remote.py',
  '        if n.strip().lower() in seen:\n            return tuple(default)',
  '        if False:\n            return tuple(default)'),

 ('fix round 1: the GPL remote does not strip names',
  'ltcplay/remote.py',
  '        out.append(n.strip())\n    return tuple(out)',
  '        out.append(n)\n    return tuple(out)'),

 ('fix round 1: the GPL remote takes a list with another key beside it',
  'ltcplay/remote.py',
  '    if not isinstance(doc, dict) or set(doc) != {key}:',
  '    if not isinstance(doc, dict) or key not in doc:'),


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
  '            self._journal(line, fault=not ok, action=name,',
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
  '        if threaded:\n            built_link.start()\n        else:\n            built_link.open()',
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
  'ALWAYS_TAKEN = frozenset((ABORT, HOLD_ON))',
  'ALWAYS_TAKEN = frozenset((HOLD_ON,))'),

 ('fix round 1: a Hold with no operator chosen is refused',
  'ltcplay/schedule.py',
  'ALWAYS_TAKEN = frozenset((ABORT, HOLD_ON))',
  'ALWAYS_TAKEN = frozenset((ABORT,))'),

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


 ("fix round 2: the deck's Abort key Resets while aborted",
  'ltcplay/streamdeck.py',
  '                    self._do_abort(again=True)',
  '                    self._do_reset()'),

 ('fix round 2: a group key does nothing while aborted',
  'ltcplay/streamdeck.py',
  '                    if self.arm.wanted[i] or self._reported_armed(i):\n                        self._do_disarm(i)',
  '                    if False:\n                        self._do_disarm(i)'),

 ('Reset design: a deck press Resets before RESET has shown 0.5 s',
  'ltcplay/streamdeck.py',
  '                    if shown is not None and now - shown >= RESET_SHOWN_S:',
  '                    if shown is not None:'),

 ('Reset design: the Abort key never Resets once RESET has shown',
  'ltcplay/streamdeck.py',
  '                        self._do_reset()\n                    else:',
  '                        self._do_abort(again=True)\n                    else:'),

 ('Reset design: drawing the latched deck never starts the RESET clock',
  'ltcplay/streamdeck.py',
  '        elif self._reset_shown_since is None:\n            self._reset_shown_since = now',
  '        elif False:\n            self._reset_shown_since = now'),

 ('Reset design: the RESET clock survives a Reset',
  'ltcplay/streamdeck.py',
  '        if not latched:\n            self._reset_shown_since = None',
  '        if False:\n            self._reset_shown_since = None'),

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
  '                elif k in GROUP_KEYS:\n                    i = k - GROUP_KEYS[0]\n                    if self.arm.wanted[i] or self._reported_armed(i):',
  '                elif k == TOP_HOLD:\n                    self._do_reset()\n                elif k in GROUP_KEYS:\n                    i = k - GROUP_KEYS[0]\n                    if self.arm.wanted[i] or self._reported_armed(i):'),

 ('fix round 2: a deck Abort waits in line behind other presses',
  'ltcplay/streamdeck.py',
  '            body["confirmed"] = True\n            # Abort never waits',
  '            body["confirmed"] = True\n        if False:\n            # Abort never waits'),

 ('R2-H5 the deck asks the engine to Abort before it disarms its own groups',
  'ltcplay/streamdeck.py',
  '        self.arm.set_all(False)\n        self.arm.send(self.names)\n        now = self._clock()\n        self._disarmed_at = [now] * len(self.names)\n        who = self.operator_provider() or ""\n        if again:',
  '        who = self.operator_provider() or ""\n        if not again and self.conductor is not None:\n            self.conductor.abort(who=who, screen="Stream Deck")\n        self.arm.set_all(False)\n        self.arm.send(self.names)\n        now = self._clock()\n        self._disarmed_at = [now] * len(self.names)\n        if again:'),

 ("fix round 2: an engine that did not take a press is not shown on the deck",
  'ltcplay/streamdeck.py',
  '        self.fault = "" if ok else f"{name.title()}: {text}"',
  '        self.fault = ""'),

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

 ("flame groups: more than 3 groups pass at start",
  "ltcplay/fire_ice.py",
  "    if len(groups) > FLAME_GROUP_LIMIT:",
  "    if len(groups) > 99:"),

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
  "            ok = self.gate is not None and self.gate.light()",
  "            ok = self.gate is not None"),

 ("beyond blanking: unblank takes something that only looks true",
  "ltcplay/beyondtc.py",
  "        if in_show is not True:\n            return False",
  "        if not in_show:\n            return False"),

 ("beyond blanking: both mode skips the OSC blank",
  "ltcplay/beyondtc.py",
  "        if self._uses(\"osc\"):\n            sent = self.osc is not None and self.osc.blank(show=show) is True",
  "        if self.mode == \"osc\":\n            sent = self.osc is not None and self.osc.blank(show=show) is True"),

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

]


# What the last suite run failed on, so a surprising result can be read.
_LAST_FAILS = []


def run_suite():
    try:
        r = subprocess.run([sys.executable, "selftest.py"], cwd=HERE,
                           capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired as e:
        # A suite that does not finish is not a green suite. Under a
        # mutation that counts as caught (the mutation broke a test so
        # badly it hung); with nothing mutated it is a failed baseline.
        # Either way the sweep reports it rather than crashing the shard,
        # which is what CI run 36206230939 did.
        out = ((e.stdout or b"") if isinstance(e.stdout, (bytes, str))
               else b"")
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        _LAST_FAILS[:] = ["  FAIL  the suite did not finish inside 300 s "
                          "(TimeoutExpired)"] + \
            [l.strip() for l in out.splitlines()
             if l.startswith("  FAIL")][:5]
        return False
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
        for _l in _LAST_FAILS:
            print("  " + _l)
        if os.environ.get("GITHUB_ACTIONS"):
            print("::error title=mutate baseline::" + " | ".join(
                _LAST_FAILS)[:900].replace("%", "%25").replace("\n", "%0A"))
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
        for w in _LAST_FAILS:
            print(f"      {w}")
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
